# WP-SETTLEMENT-01 — InkTransaction (불변 계정 재무 사실 + exactly-once 실행)
#
# [소유권 경계]
#   ink transaction module = 불변 InkTransaction, append-only per-user 원장,
#                            그리고 Settlement 청구를 계정에 '정확히 한 번' 적용하는
#                            멱등·복구 가능 executor 를 담당한다.
#   계정 파일/스키마/per-user 락/원자 교체는 core/accounts.py 소관이며, 이 모듈은
#   그 strict 프리미티브(apply_ink_charge_strict/get_applied_ink_marker)를 쓴다.
#
# [핵심 불변식 — 핸드오프 §16~§24]
#   - 파일시스템 전역 원자 트랜잭션은 없다. 그래서 executor 는 '원자적인 척'하지
#     않고, 계정 내구 마커를 근거로 멱등·복구 가능하게 동작한다.
#   - 안전 순서: (1) per-user 락 (2) strict 로드 (3) applied 마커 검사
#     (4) 신규면 잔액+누적+마커를 한 번의 원자 계정 교체로 기록 (5) InkTransaction
#     원장 append. 계정 교체 직후~원장 append 사이 크래시는 재적용 없이 복구된다.

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field

from . import accounts


# 이 패키지가 다루는 유일한 종류. 캐시 선불/환불/관리자 크레딧/구매 등은 후속 패키지.
KIND_CHARGE = "CHARGE"
CHARGE_REASON = "turn_settlement_charge"

# 기본 원장 위치(§19). 유저당 결정적 JSONL.
INK_TX_DIR = os.path.join("accounts", "ink_transactions")


# ══════════════════════════════════════════════════════════════
#  예외 (핸드오프 §29)
# ══════════════════════════════════════════════════════════════

class InkTransactionError(RuntimeError):
    """InkTransaction 계열 예외의 베이스."""


class InkTransactionPersistenceError(InkTransactionError):
    """원장/계정 적용의 내구성 경계(write→flush→fsync)/열기/직렬화 실패."""


class InkTransactionCorruptionError(InkTransactionError):
    """malformed 원장 라인, 구조 무효, user_id/경로 불일치, 마커-원장 불일치."""


class InkTransactionConflictError(InkTransactionError):
    """같은 ink_tx_id 에 상충하는 durable 사실(settlement/user/금액/kind)."""


class InkTransactionRecoveryRequired(InkTransactionError):
    """계정 변이는 내구화됐으나 원장 append 가 실패 — 재실행으로 이력 복구 필요.

    계정은 이미 바뀌었으므로 잔액을 되돌리지 않는다. 재실행은 잔액을 다시
    적용하지 않고 원장만 복구한다(§20.3).
    """


class InsufficientInkError(InkTransactionError):
    """overdraft 불허 + 잔액 부족 — 계정 무변이(§22)."""


# ══════════════════════════════════════════════════════════════
#  InkTransaction — 불변 계정 재무 사실 (핸드오프 §15)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class InkTransaction:
    ink_tx_id: str
    settlement_id: str
    user_id: str

    kind: str                    # 이 패키지에선 CHARGE
    nominal_charge_ink: int

    balance_before: int
    balance_after: int
    applied_balance_delta: int

    overdraft: bool
    operator_subsidy_ink: int

    reason: str
    created_at: float


# 정상 replay 마다 자연히 달라지는 envelope 만 canonical 비교에서 제외한다.
_INK_TX_ENVELOPE_FIELDS = ("created_at",)


def _canonical_ink_tx_payload(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _INK_TX_ENVELOPE_FIELDS}


def _ink_tx_fingerprint(obj: dict) -> str:
    return json.dumps(_canonical_ink_tx_payload(obj), sort_keys=True,
                      ensure_ascii=False)


# ══════════════════════════════════════════════════════════════
#  결정적 식별자 / 요청 지문 (핸드오프 §16)
# ══════════════════════════════════════════════════════════════

def expected_ink_tx_id(settlement_id: str, user_id) -> str:
    """하나의 Settlement 를 한 유저에게 청구할 때의 결정적 ink_tx_id.

    재시작 후에도 settlement_id + canonical user 로 재구성된다. replay 마다 새
    랜덤 ID 를 만들지 않는다.
    """
    if not settlement_id:
        raise InkTransactionError("ink_tx_id 파생 실패: settlement_id 없음")
    return f"ink-charge:{settlement_id}:user:{user_id}"


def charge_request_fingerprint(settlement_id: str, user_id, nominal_charge_ink: int,
                               kind: str = KIND_CHARGE) -> str:
    """계정 마커 상충 판정용 '요청' 지문(§16).

    같은 ink_tx_id 라도 settlement/user/nominal/kind 가 다르면 상충으로 거부된다.
    잔액 결과(before/after 등)는 최초 적용 시 고정되어 마커에서 재구성되므로 요청
    지문에는 넣지 않는다.
    """
    return json.dumps(
        {"settlement_id": str(settlement_id), "user_id": str(user_id),
         "nominal_charge_ink": int(nominal_charge_ink), "kind": str(kind)},
        sort_keys=True, ensure_ascii=False)


def _ink_tx_from_marker(settlement_id: str, user_id, ink_tx_id: str,
                        marker: dict, *, created_at: float | None = None) -> InkTransaction:
    """계정 applied 마커에서 canonical InkTransaction 을 재구성한다(§20).

    created_at 은 envelope(비교 제외)이므로 복구 시 새로 부여해도 canonical
    동등성이 유지된다.
    """
    return InkTransaction(
        ink_tx_id=ink_tx_id,
        settlement_id=str(settlement_id),
        user_id=str(user_id),
        kind=KIND_CHARGE,
        nominal_charge_ink=int(marker["nominal_charge_ink"]),
        balance_before=int(marker["balance_before"]),
        balance_after=int(marker["balance_after"]),
        applied_balance_delta=int(marker["applied_balance_delta"]),
        overdraft=bool(marker["overdraft"]),
        operator_subsidy_ink=int(marker["operator_subsidy_ink"]),
        reason=CHARGE_REASON,
        created_at=time.time() if created_at is None else created_at,
    )


# ══════════════════════════════════════════════════════════════
#  Per-path 동기화 레지스트리
# ══════════════════════════════════════════════════════════════

_LEDGER_REGISTRY: dict = {}
_REGISTRY_GUARD = threading.Lock()


def _registry_for(path: str) -> dict:
    key = os.path.abspath(path)
    with _REGISTRY_GUARD:
        entry = _LEDGER_REGISTRY.get(key)
        if entry is None:
            entry = {"lock": threading.Lock()}
            _LEDGER_REGISTRY[key] = entry
        return entry


def default_ink_tx_ledger_path(user_id) -> str:
    return os.path.join(INK_TX_DIR, f"{user_id}.jsonl")


# ══════════════════════════════════════════════════════════════
#  InkTransactionLedger — append-only per-user JSONL (핸드오프 §19)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class LedgerAppendResult:
    created: bool
    ink_tx_id: str


def _validate_ink_tx_row(obj: object, where: str) -> dict:
    if not isinstance(obj, dict):
        raise InkTransactionCorruptionError(f"{where}: InkTransaction 레코드가 객체가 아님")
    tid = obj.get("ink_tx_id")
    uid = obj.get("user_id")
    if not isinstance(tid, str) or not tid:
        raise InkTransactionCorruptionError(f"{where}: ink_tx_id 무효")
    if not isinstance(uid, str) or not uid:
        raise InkTransactionCorruptionError(f"{where}: user_id 무효")
    return obj


class InkTransactionLedger:
    """유저 단위 append-only InkTransaction 원장.

    - path 주입 가능(테스트 격리). 운영 기본은 default_ink_tx_ledger_path(user_id).
    - 경로 파일명 stem 을 이 원장의 소속 user_id 로 신뢰하고, 라인의 user_id 가
      이와 다르면 손상으로 본다(§19).
    - 같은 ink_tx_id 는 두 번 append 되지 않는다. append 는 write→flush→fsync.
    - reader 는 손상을 조용히 skip 하지 않는다.
    """

    def __init__(self, path: str, user_id: str | None = None):
        self.path = path
        stem = os.path.splitext(os.path.basename(path))[0]
        self.user_id = str(user_id) if user_id is not None else stem
        self._reg = _registry_for(path)

    def _read_all_locked(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            f = open(self.path, "r", encoding="utf-8")
        except OSError as e:
            raise InkTransactionPersistenceError(f"원장 열기 실패: {self.path}") from e
        rows: list[dict] = []
        seen_ids: set = set()
        with f:
            for lineno, raw in enumerate(f, start=1):
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    raise InkTransactionCorruptionError(
                        f"{self.path}:{lineno} JSON 파싱 실패") from e
                row = _validate_ink_tx_row(obj, f"{self.path}:{lineno}")
                if row["user_id"] != self.user_id:
                    raise InkTransactionCorruptionError(
                        f"{self.path}:{lineno} user_id 불일치: "
                        f"라인={row['user_id']!r} 원장={self.user_id!r}")
                tid = row["ink_tx_id"]
                if tid in seen_ids:
                    raise InkTransactionConflictError(
                        f"중복 ink_tx_id(원장 손상): {tid}")
                seen_ids.add(tid)
                rows.append(row)
        return rows

    def _truncate_to(self, size: int) -> None:
        try:
            with open(self.path, "r+b") as f:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    def get_by_id_strict(self, ink_tx_id: str):
        """ink_tx_id 로 정확히 하나의 레코드 dict 를 돌려준다(없으면 None)."""
        with self._reg["lock"]:
            rows = self._read_all_locked()
        for o in rows:
            if o["ink_tx_id"] == ink_tx_id:
                return o
        return None

    def append_strict(self, tx: InkTransaction) -> LedgerAppendResult:
        """InkTransaction 을 내구적으로 append 한다.

        같은 ink_tx_id +
          - 동등 canonical payload → append 없이 created=False.
          - 상충 canonical payload → InkTransactionConflictError, append 없음.
        내구성 경계 실패는 부분 기록 롤백 후 InkTransactionPersistenceError.
        """
        if str(tx.user_id) != self.user_id:
            raise InkTransactionCorruptionError(
                f"원장 user 불일치: tx={tx.user_id!r} 원장={self.user_id!r}")
        incoming = asdict(tx)
        fingerprint = _ink_tx_fingerprint(incoming)
        tid = tx.ink_tx_id
        with self._reg["lock"]:
            rows = self._read_all_locked()
            for o in rows:
                if o["ink_tx_id"] != tid:
                    continue
                if _ink_tx_fingerprint(o) == fingerprint:
                    return LedgerAppendResult(False, tid)
                raise InkTransactionConflictError(
                    f"ink_tx_id 상충(append 거부): {tid}")

            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            line = json.dumps(incoming, ensure_ascii=False) + "\n"
            pre_size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                self._truncate_to(pre_size)
                raise InkTransactionPersistenceError(
                    f"원장 append 실패: {self.path} id={tid}") from e
            return LedgerAppendResult(True, tid)

    def list_all(self) -> list[dict]:
        with self._reg["lock"]:
            return list(self._read_all_locked())


# ══════════════════════════════════════════════════════════════
#  Executor — exactly-once / 복구 가능 (핸드오프 §17, §20, §24)
# ══════════════════════════════════════════════════════════════

# 실행 결과 상태
EXEC_APPLIED = "APPLIED"                     # 신규 적용 + 원장 기록
EXEC_ALREADY = "ALREADY_APPLIED"             # 마커+원장 모두 존재, 재확인 no-op
EXEC_RECOVERED_LEDGER = "RECOVERED_LEDGER"   # 마커 존재+원장 누락 → 재적용 없이 원장 복구
EXEC_NO_MUTATION = "NO_MUTATION"             # 0원 청구 등 재무 변이 불필요(§23)


@dataclass(frozen=True)
class InkChargeResult:
    status: str
    ink_tx_id: str
    transaction: InkTransaction | None
    balance_after: int | None = None


def _ledger_for(user_id, ledger) -> InkTransactionLedger:
    if ledger is None:
        return InkTransactionLedger(default_ink_tx_ledger_path(user_id),
                                    user_id=str(user_id))
    return ledger


async def execute_settlement_charge(settlement, user_id, *, ledger=None,
                                    accounts_module=accounts,
                                    allow_overdraft: bool = True) -> InkChargeResult:
    """Settlement 의 한 유저 청구를 계정에 정확히 한 번 적용/복구한다.

    - 0원(예: FAILED_SYSTEM) → 계정/원장 무변이, NO_MUTATION(§23).
    - 정상 신규 → 잔액+마커 원자 기록 후 원장 append, APPLIED.
    - 이미 적용됨(마커+원장) → 무변이 no-op, ALREADY_APPLIED.
    - 마커 존재 + 원장 누락(§20.2) → 재적용 없이 원장 복구, RECOVERED_LEDGER.
    - 계정 기록 후 원장 append 실패(§20.3) → InkTransactionRecoveryRequired.
    - 마커 부재 + 원장 존재(§20.5) → 조용한 재차감 금지, 명시적 복구 필요 오류.
    """
    uid = str(user_id)
    billing = tuple(getattr(settlement, "billing_user_ids", ()) or ())
    if uid not in billing:
        raise InkTransactionError(
            f"user_id {uid} 는 이 Settlement 의 billing_user 가 아님")

    nominal = int(getattr(settlement, "charge_ink_per_user", 0))
    settlement_id = getattr(settlement, "settlement_id")
    ink_tx_id = expected_ink_tx_id(settlement_id, uid)

    # 0원 청구는 잔액 변이 CHARGE 를 만들지 않는다(§23).
    if nominal == 0:
        return InkChargeResult(EXEC_NO_MUTATION, ink_tx_id, None)

    led = _ledger_for(uid, ledger)
    fingerprint = charge_request_fingerprint(settlement_id, uid, nominal)

    # ── 사전 라우팅: 계정 마커와 원장 상태를 함께 본다(§20) ──
    marker = await accounts_module.get_applied_ink_marker(uid, ink_tx_id)
    ledger_row = led.get_by_id_strict(ink_tx_id)

    if marker is not None:
        # 마커의 요청 지문이 이번 요청과 다르면 상충(§16).
        if marker.get("fingerprint") != fingerprint:
            raise InkTransactionConflictError(
                f"applied 마커 상충: ink_tx_id={ink_tx_id}")
        rec = _ink_tx_from_marker(settlement_id, uid, ink_tx_id, marker)
        if ledger_row is None:
            # §20.2: 계정은 적용됐으나 원장 라인 누락 → 재적용 없이 복구.
            led.append_strict(rec)
            return InkChargeResult(EXEC_RECOVERED_LEDGER, ink_tx_id, rec,
                                   balance_after=rec.balance_after)
        # 마커와 원장이 모두 존재 — canonical 일치해야 한다.
        if _ink_tx_fingerprint(ledger_row) != _ink_tx_fingerprint(asdict(rec)):
            raise InkTransactionCorruptionError(
                f"마커-원장 불일치: ink_tx_id={ink_tx_id}")
        return InkChargeResult(EXEC_ALREADY, ink_tx_id, rec,
                               balance_after=rec.balance_after)

    # 마커 부재
    if ledger_row is not None:
        # §20.5: 계정 마커 없이 원장 라인만 존재 — 어느 쪽이 권위인지 추정 금지.
        raise InkTransactionRecoveryRequired(
            f"원장에 라인이 있으나 계정 applied 마커가 없음(복구 필요): "
            f"ink_tx_id={ink_tx_id}")

    # ── 신규 적용: 계정 원자 기록(잔액+누적+마커) ──
    try:
        applied = await accounts_module.apply_ink_charge_strict(
            uid, ink_tx_id=ink_tx_id, fingerprint=fingerprint,
            settlement_id=settlement_id, nominal_charge_ink=nominal,
            allow_overdraft=allow_overdraft)
    except accounts.AccountError as e:
        # 계정 strict 영속화 실패 → 무변이 명시적 실패(§20.1).
        raise InkTransactionPersistenceError(
            f"계정 청구 적용 실패: ink_tx_id={ink_tx_id}") from e

    status = applied["status"]
    if status == accounts.APPLY_INSUFFICIENT:
        raise InsufficientInkError(
            f"잔액 부족(overdraft 불허): ink_tx_id={ink_tx_id}")
    if status == accounts.APPLY_CONFLICT:
        raise InkTransactionConflictError(
            f"applied 마커 상충: ink_tx_id={ink_tx_id}")

    # APPLY_NEW 또는 (경합으로) APPLY_ALREADY — 마커에서 canonical 재구성.
    rec = _ink_tx_from_marker(settlement_id, uid, ink_tx_id, applied["marker"])

    # 계정 변이는 이미 내구화됨 → 원장 append. 실패 시 잔액을 되돌리지 않고
    # 복구 필요를 명시적으로 올린다(§20.3).
    try:
        led.append_strict(rec)
    except InkTransactionPersistenceError as e:
        raise InkTransactionRecoveryRequired(
            f"계정 변이는 내구화됐으나 원장 append 실패(복구 필요): "
            f"ink_tx_id={ink_tx_id}") from e

    result_status = EXEC_APPLIED if status == accounts.APPLY_NEW else EXEC_RECOVERED_LEDGER
    return InkChargeResult(result_status, ink_tx_id, rec,
                           balance_after=rec.balance_after)


async def execute_settlement_charges(settlement, *, ledger_for=None,
                                     accounts_module=accounts,
                                     allow_overdraft: bool = True) -> dict:
    """Settlement 의 모든 billing_user 에 대해 청구를 실행한다(§24).

    - 각 유저는 독립적 결정적 ink_tx_id 를 가지며 기존 per-user 락으로 직렬화된다.
    - 부분 실패(예: 한 유저의 계정 기록 실패)는 같은 배치를 다시 호출해 복구한다:
      이미 청구된 유저는 재차감되지 않고, 남은 유저만 정확히 한 번 완료된다.
    - 유저 간 인위적 파일시스템 원자 트랜잭션을 시도하지 않는다.

    ledger_for: (user_id) -> InkTransactionLedger 팩토리(테스트 격리용). None 이면
    유저별 기본 경로 원장을 쓴다.

    Returns:
        {user_id: InkChargeResult}  — 이번 호출에서 성공적으로 처리된 유저들.
    실패한 유저가 있으면 그 예외를 전파한다(성공분은 이미 내구화되어 재호출 시
    멱등 복구된다).
    """
    results: dict = {}
    for uid in tuple(getattr(settlement, "billing_user_ids", ()) or ()):
        led = ledger_for(uid) if ledger_for is not None else None
        results[uid] = await execute_settlement_charge(
            settlement, uid, ledger=led, accounts_module=accounts_module,
            allow_overdraft=allow_overdraft)
    return results
