# WP-SETTLEMENT-01 — TurnSettlement (immutable financial decision + durable store)
#
# [소유권 경계]
#   settlement module = 하나의 TurnTransaction attempt 에 대한 '불변 재무 결정'과
#                       그 결정의 append-only 영속화(SettlementStore)만 담당한다.
#   실제 계정 잔액 변이/멱등 실행은 core/ink_transactions.py 소관이다.
#
# [정책 근거 — 핸드오프 §2, §7~§14]
#   - CostEvent(실측 provider 사실)는 append-only 이며 rewind/rerender 로 사라지지
#     않는다. 플레이어 부담은 트랜잭션 결과가 확정된 뒤 Settlement 경계에서 '한 번'
#     결정한다(BILL-02/BILL-05/BILL-06, C-05~C-07).
#   - Settlement 는 가변 session.total_cost/session.players 를 읽지 않는다. 호출자가
#     명시적 CostEvent-ID 집합과 billing-user 스냅샷을 공급한다.
#   - 이 패키지는 재무 '기반(foundation)'만 만든다. 라이브 턴 파이프라인에 배선하지
#     않는다(핸드오프 §27, §40).

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum

from .ink import cost_to_ink
from .cost_ledger import (
    HINT_PLAYER_CANDIDATE, HINT_SYSTEM, HINT_FREE_FEATURE, HINT_OPERATOR,
    HINT_UNKNOWN,
    SOURCE_PROVIDER_METADATA, SOURCE_FIXED_PROVIDER_PRICING, SOURCE_ESTIMATE,
    CostLedgerError, CostEventNotFoundError, CostLedgerConflictError,
)

# 이 Settlement 빌더가 적용한 정책 버전. 저장 payload 에 박아 두어 미래 정책
# 변경 시 과거 Settlement 의 산정 근거를 구분할 수 있게 한다.
SETTLEMENT_POLICY_VERSION = "WP-SETTLEMENT-01"


# ══════════════════════════════════════════════════════════════
#  예외 (핸드오프 §29)
# ══════════════════════════════════════════════════════════════

class SettlementError(RuntimeError):
    """Settlement 계열 예외의 베이스."""


class SettlementPersistenceError(SettlementError):
    """strict 저장의 내구성 경계(write→flush→fsync)/열기/직렬화 실패."""


class SettlementCorruptionError(SettlementError):
    """malformed JSON, 구조 무효, 또는 디스크상 중복 settlement_id."""


class SettlementConflictError(SettlementError):
    """같은 settlement_id 에 상충하는 canonical payload 가 도착함."""


class SettlementPolicyError(SettlementError):
    """분류/정체성/billing-user 정책 위반(UNKNOWN 힌트, 식별자 불일치 등)."""


class SettlementUnresolvedEstimateError(SettlementError):
    """COMMITTED + PLAYER_CANDIDATE 인데 usage_source 가 추정/무효라 확정 불가."""


class CostEventAlreadySettledError(SettlementError):
    """하나의 CostEvent 를 서로 다른 두 Settlement 이 소유하려 함(§14)."""


class SettlementNotFoundError(SettlementError):
    """strict get 에서 요청한 settlement_id 를 찾지 못함."""


# ══════════════════════════════════════════════════════════════
#  결과 enum / 식별자
# ══════════════════════════════════════════════════════════════

class SettlementOutcome(str, Enum):
    COMMITTED = "COMMITTED"
    FAILED_SYSTEM = "FAILED_SYSTEM"


def settlement_id_for(transaction_id: str, attempt: int) -> str:
    """불변 attempt 정체성에서 결정적으로 파생하는 settlement_id(§7.1).

    같은 트랜잭션 attempt 를 재구성하면 항상 같은 값이 나온다. 시각/랜덤/서사
    해시/가변 세션 총액/가변 턴 카운터를 절대 쓰지 않는다.
    """
    if not transaction_id:
        raise SettlementPolicyError("settlement_id 파생 실패: transaction_id 없음")
    return f"turn-settlement:{transaction_id}:attempt:{int(attempt)}"


@dataclass(frozen=True)
class TransactionIdentity:
    """Settlement 이 소비하는 불변 트랜잭션 attempt 정체성.

    가변 session/TurnTransaction 객체를 직접 들고 다니지 않도록, 빌더는 이
    작은 불변 스냅샷만 받는다(replay 안정성).
    """
    session_id: str
    transaction_id: str
    logical_turn: int
    attempt: int

    @classmethod
    def from_transaction(cls, tx) -> "TransactionIdentity":
        return cls(
            session_id=getattr(tx, "session_id"),
            transaction_id=getattr(tx, "transaction_id"),
            logical_turn=getattr(tx, "logical_turn"),
            attempt=getattr(tx, "attempt"),
        )


# ══════════════════════════════════════════════════════════════
#  TurnSettlement — 불변 재무 결정 (핸드오프 §7)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class TurnSettlement:
    settlement_id: str

    session_id: str
    transaction_id: str
    logical_turn: int
    attempt: int

    outcome: str                       # SettlementOutcome value

    included_cost_event_ids: tuple     # tuple[str, ...] — canonical 정렬·중복 제거
    billing_user_ids: tuple            # tuple[str, ...] — canonical 정렬·중복 제거

    provider_cost_usd: float
    provider_cost_krw: float

    player_billable_cost_krw: float
    non_player_cost_krw: float
    system_unbilled_cost_krw: float

    estimated_cost_krw: float

    charge_ink_per_user: int
    aggregate_nominal_charge_ink: int

    policy_version: str
    created_at: float


# 정상 replay 마다 자연히 달라지는 envelope 필드만 동등성 비교에서 제외한다.
# 재무 사실은 절대 제외하지 않는다(§13.2).
_SETTLEMENT_ENVELOPE_FIELDS = ("created_at",)


def _canonical_settlement_payload(obj: dict) -> dict:
    out = {k: v for k, v in obj.items() if k not in _SETTLEMENT_ENVELOPE_FIELDS}
    # 리스트/튜플 표기 차이를 흡수(로드 후 list, 생성 시 tuple).
    for key in ("included_cost_event_ids", "billing_user_ids"):
        if key in out and out[key] is not None:
            out[key] = list(out[key])
    return out


def _settlement_fingerprint(obj: dict) -> str:
    return json.dumps(_canonical_settlement_payload(obj), sort_keys=True,
                      ensure_ascii=False)


def settlement_to_dict(s: TurnSettlement) -> dict:
    return asdict(s)


# ══════════════════════════════════════════════════════════════
#  billing-user 스냅샷 정규화 (핸드오프 §12)
# ══════════════════════════════════════════════════════════════

def normalize_billing_user_ids(user_ids) -> tuple:
    """문자열화 → 공백 거부 → 중복 제거 → 정렬. 결정적 tuple 을 돌려준다."""
    seen = []
    for uid in (user_ids or []):
        s = str(uid).strip()
        if not s:
            raise SettlementPolicyError("billing_user_id 가 비어 있음")
        if s not in seen:
            seen.append(s)
    return tuple(sorted(seen))


# ══════════════════════════════════════════════════════════════
#  Settlement 빌더 (핸드오프 §8~§11)
# ══════════════════════════════════════════════════════════════

def _validate_event_identity(ev: dict, ident: TransactionIdentity) -> None:
    """CostEvent 가 이 트랜잭션 attempt 에 속하는지 엄격 검증(§8).

    session_id/transaction_id 는 항상 일치해야 한다. logical_turn/turn_attempt 는
    이벤트에 채워진 경우에만 일치를 요구한다(관측 시점에 미상일 수 있음).
    """
    if ev.get("session_id") != ident.session_id:
        raise SettlementPolicyError(
            f"CostEvent session_id 불일치: {ev.get('session_id')!r} != {ident.session_id!r}")
    if ev.get("transaction_id") != ident.transaction_id:
        raise SettlementPolicyError(
            f"CostEvent transaction_id 불일치: {ev.get('transaction_id')!r} != {ident.transaction_id!r}")
    lt = ev.get("logical_turn")
    if lt is not None and lt != ident.logical_turn:
        raise SettlementPolicyError(
            f"CostEvent logical_turn 불일치: {lt!r} != {ident.logical_turn!r}")
    ta = ev.get("turn_attempt")
    if ta is not None and ta != ident.attempt:
        raise SettlementPolicyError(
            f"CostEvent turn_attempt 불일치: {ta!r} != {ident.attempt!r}")


def _classify_committed(ev: dict, buckets: dict) -> None:
    hint = ev.get("billing_hint")
    src = ev.get("usage_source")
    cost = float(ev.get("cost_krw") or 0.0)

    if hint == HINT_PLAYER_CANDIDATE:
        if src in (SOURCE_PROVIDER_METADATA, SOURCE_FIXED_PROVIDER_PRICING):
            buckets["player_billable"] += cost
        elif src == SOURCE_ESTIMATE:
            # 추정을 조용히 확정 청구로 바꾸지 않는다(§10).
            raise SettlementUnresolvedEstimateError(
                f"COMMITTED PLAYER_CANDIDATE 이벤트가 추정(usage_source=ESTIMATE) 이라 "
                f"확정 청구 불가: event_id={ev.get('event_id')}")
        else:
            # 양수 비용 + 무효/미상 출처는 조용히 플레이어 부담이 되면 안 된다(§10).
            if cost > 0:
                raise SettlementUnresolvedEstimateError(
                    f"COMMITTED PLAYER_CANDIDATE 이벤트의 usage_source 가 무효/미상"
                    f"({src!r}) 이라 확정 청구 불가: event_id={ev.get('event_id')}")
            # 0원 이벤트는 청구 영향이 없으므로 통과.
    elif hint in (HINT_FREE_FEATURE, HINT_OPERATOR, HINT_SYSTEM):
        # 운영/무료/시스템 부담 — 플레이어 비청구.
        buckets["non_player"] += cost
    elif hint == HINT_UNKNOWN:
        raise SettlementPolicyError(
            f"HINT_UNKNOWN 은 조용히 청구될 수 없다: event_id={ev.get('event_id')}")
    else:
        raise SettlementPolicyError(
            f"미지의 billing_hint({hint!r}): event_id={ev.get('event_id')}")


def _classify_failed_system(ev: dict, buckets: dict) -> None:
    hint = ev.get("billing_hint")
    src = ev.get("usage_source")
    cost = float(ev.get("cost_krw") or 0.0)

    # FAILED_SYSTEM 은 플레이어 청구를 절대 만들 수 없다(§9.2). 실측 provider 비용은
    # 운영 이력으로 남되, player-candidate 부담은 system_unbilled 로 흡수한다.
    if hint == HINT_PLAYER_CANDIDATE:
        buckets["system_unbilled"] += cost
        if src == SOURCE_ESTIMATE:
            buckets["estimated"] += cost   # 추정 노출은 별도 보존(§10), 청구 무관
    elif hint in (HINT_FREE_FEATURE, HINT_OPERATOR, HINT_SYSTEM):
        # 이미 무료/운영/시스템 부담인 비용을 '실패 손실'로 재분류하지 않는다(§9.2).
        buckets["non_player"] += cost
    else:
        # UNKNOWN/미지 힌트라도 실패턴은 플레이어 청구가 없다. 손실 노출로만 기록.
        buckets["system_unbilled"] += cost


def build_turn_settlement(*, cost_ledger, transaction_identity: TransactionIdentity,
                          outcome, cost_event_ids, billing_user_ids) -> TurnSettlement:
    """명시적 CostEvent-ID 집합 + billing-user 스냅샷에서 불변 Settlement 을 만든다.

    - 이벤트는 이미 검증된 strict exact-ID 리더로 읽는다(가변 세션 합계 추론 금지).
    - outcome 별 billing_hint/usage_source 분류로 플레이어 부담을 정한다.
    - cost_to_ink 는 이 경계에서 '정확히 한 번' 호출된다(§11). 이후 어떤 소비자도
      재계산하지 않는다.
    """
    outcome_val = outcome.value if isinstance(outcome, SettlementOutcome) else str(outcome)
    if outcome_val not in (SettlementOutcome.COMMITTED.value,
                           SettlementOutcome.FAILED_SYSTEM.value):
        raise SettlementPolicyError(f"미지의 outcome: {outcome_val!r}")

    ident = transaction_identity

    # ── 1) CostEvent-ID 입력 검증: 중복 금지(§14), 그 뒤 canonical 정렬 ──
    raw_ids = list(cost_event_ids or [])
    seen = set()
    for eid in raw_ids:
        if eid in seen:
            raise SettlementPolicyError(f"입력 CostEvent-ID 중복: {eid}")
        seen.add(eid)
    canonical_ids = tuple(sorted(seen))

    # ── 2) billing-user 스냅샷 정규화(§12) ──
    billing_users = normalize_billing_user_ids(billing_user_ids)

    # ── 3) strict exact-ID 로 이벤트를 읽는다(누락→CostEventNotFoundError,
    #        디스크 중복 event_id→CostLedgerConflictError). 정체성은 아래에서
    #        명시적으로 검증한다(§8). ──
    events = cost_ledger.get_cost_events_by_ids_strict(list(canonical_ids))

    # ── 4) 정체성 검증 + 분류 ──
    buckets = {"player_billable": 0.0, "non_player": 0.0,
               "system_unbilled": 0.0, "estimated": 0.0}
    provider_usd = 0.0
    provider_krw = 0.0
    for ev in events:
        _validate_event_identity(ev, ident)
        provider_usd += float(ev.get("cost_usd") or 0.0)
        provider_krw += float(ev.get("cost_krw") or 0.0)
        if outcome_val == SettlementOutcome.COMMITTED.value:
            _classify_committed(ev, buckets)
        else:
            _classify_failed_system(ev, buckets)

    player_billable_krw = buckets["player_billable"]
    non_player_krw = buckets["non_player"]
    system_unbilled_krw = buckets["system_unbilled"]
    estimated_krw = buckets["estimated"]

    # ── 5) 단 한 번의 반올림 경계(§11) ──
    charge_ink_per_user = int(cost_to_ink(player_billable_krw))

    # ── 6) billing-user 정책(§12): COMMITTED + 청구>0 인데 대상이 없으면 오류 ──
    if (outcome_val == SettlementOutcome.COMMITTED.value
            and charge_ink_per_user > 0 and not billing_users):
        raise SettlementPolicyError(
            "COMMITTED 이고 per-user 청구가 양수인데 billing_user_ids 가 비어 있음")

    aggregate_nominal = charge_ink_per_user * len(billing_users)

    return TurnSettlement(
        settlement_id=settlement_id_for(ident.transaction_id, ident.attempt),
        session_id=ident.session_id,
        transaction_id=ident.transaction_id,
        logical_turn=ident.logical_turn,
        attempt=ident.attempt,
        outcome=outcome_val,
        included_cost_event_ids=canonical_ids,
        billing_user_ids=billing_users,
        provider_cost_usd=provider_usd,
        provider_cost_krw=provider_krw,
        player_billable_cost_krw=player_billable_krw,
        non_player_cost_krw=non_player_krw,
        system_unbilled_cost_krw=system_unbilled_krw,
        estimated_cost_krw=estimated_krw,
        charge_ink_per_user=charge_ink_per_user,
        aggregate_nominal_charge_ink=aggregate_nominal,
        policy_version=SETTLEMENT_POLICY_VERSION,
        created_at=time.time(),
    )


# ══════════════════════════════════════════════════════════════
#  Per-path 동기화 레지스트리 (동시 기록 멱등 안전)
# ══════════════════════════════════════════════════════════════

_STORE_REGISTRY: dict = {}
_REGISTRY_GUARD = threading.Lock()


def _registry_for(path: str) -> dict:
    key = os.path.abspath(path)
    with _REGISTRY_GUARD:
        entry = _STORE_REGISTRY.get(key)
        if entry is None:
            entry = {"lock": threading.Lock()}
            _STORE_REGISTRY[key] = entry
        return entry


def default_settlement_path(session_id: str) -> str:
    """운영 기본 경로. 세션 루트 하위의 append-only JSONL(§13)."""
    return os.path.join("sessions", str(session_id), "turn_settlements.jsonl")


# ══════════════════════════════════════════════════════════════
#  SettlementStore — append-only, 되감기 불가 (핸드오프 §13, §14)
# ══════════════════════════════════════════════════════════════

def _validate_settlement_row(obj: object, where: str) -> dict:
    if not isinstance(obj, dict):
        raise SettlementCorruptionError(f"{where}: Settlement 레코드가 객체가 아님")
    sid = obj.get("settlement_id")
    if not isinstance(sid, str) or not sid:
        raise SettlementCorruptionError(f"{where}: settlement_id 무효")
    ev_ids = obj.get("included_cost_event_ids")
    if not isinstance(ev_ids, list):
        raise SettlementCorruptionError(f"{where}: included_cost_event_ids 무효")
    return obj


class SettlementStore:
    """세션 단위 append-only Settlement 저장소.

    - path 주입 가능(테스트 격리). 운영 기본은 default_settlement_path(session_id).
    - 같은 settlement_id 는 두 번 append 되지 않는다. append 는
      write→flush→fsync 내구성 경계를 지난다.
    - 하나의 CostEvent 는 하나의 Settlement 에만 속한다(§14).
    - reader 는 손상을 조용히 skip 하지 않는다.
    """

    def __init__(self, path: str):
        self.path = path
        self._reg = _registry_for(path)

    # ── 내부 strict 리더 ────────────────────────────────────────

    def _read_all_locked(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            f = open(self.path, "r", encoding="utf-8")
        except OSError as e:
            raise SettlementPersistenceError(f"Settlement 원장 열기 실패: {self.path}") from e
        rows: list[dict] = []
        with f:
            for lineno, raw in enumerate(f, start=1):
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    raise SettlementCorruptionError(
                        f"{self.path}:{lineno} JSON 파싱 실패") from e
                rows.append(_validate_settlement_row(obj, f"{self.path}:{lineno}"))
        self._assert_unique_ids(rows)
        return rows

    @staticmethod
    def _assert_unique_ids(rows: list[dict]) -> None:
        """디스크상 중복 settlement_id 는 payload 동등 여부와 무관하게 손상이다(§13.3)."""
        seen: set = set()
        for o in rows:
            sid = o["settlement_id"]
            if sid in seen:
                raise SettlementCorruptionError(f"중복 settlement_id(원장 손상): {sid}")
            seen.add(sid)

    @staticmethod
    def _ownership_index(rows: list[dict]) -> dict:
        """cost_event_id → settlement_id 소유 맵을 만든다(§14).

        strict-only 원장은 event 당 하나의 소유 settlement 만 존재해야 하므로,
        같은 event 를 서로 다른 settlement 이 이미 소유했다면 손상으로 본다.
        """
        owner: dict = {}
        for o in rows:
            sid = o["settlement_id"]
            for eid in (o.get("included_cost_event_ids") or []):
                prev = owner.get(eid)
                if prev is not None and prev != sid:
                    raise SettlementCorruptionError(
                        f"CostEvent {eid} 가 둘 이상의 settlement 소유(원장 손상): "
                        f"{prev} / {sid}")
                owner[eid] = sid
        return owner

    def _truncate_to(self, size: int) -> None:
        try:
            with open(self.path, "r+b") as f:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    # ── 공개 API ────────────────────────────────────────────────

    def record_strict(self, settlement: TurnSettlement) -> TurnSettlement:
        """Settlement 을 내구적으로 append 하고 canonical 영속본을 돌려준다.

        같은 settlement_id +
          - 동등 canonical payload → append 없이 canonical 영속본 반환(멱등 replay).
          - 상충 canonical payload → SettlementConflictError, append 없음.
        새 settlement 이 다른 settlement 소유의 CostEvent 를 주장하면
        CostEventAlreadySettledError. 내구성 경계 실패는 부분 기록을 롤백한 뒤
        SettlementPersistenceError 로 올린다.
        """
        incoming = settlement_to_dict(settlement)
        fingerprint = _settlement_fingerprint(incoming)
        sid = settlement.settlement_id
        with self._reg["lock"]:
            rows = self._read_all_locked()

            # 같은 id 존재 여부 판정(멱등/상충).
            for o in rows:
                if o["settlement_id"] != sid:
                    continue
                if _settlement_fingerprint(o) == fingerprint:
                    return _row_to_settlement(o)     # 동등 replay → 영속본 반환
                raise SettlementConflictError(
                    f"settlement_id 상충(append 거부): {sid}")

            # CostEvent 단일 소유 불변식(§14): 다른 settlement 이 이미 소유했는가?
            owner = self._ownership_index(rows)
            for eid in settlement.included_cost_event_ids:
                prev = owner.get(eid)
                if prev is not None and prev != sid:
                    raise CostEventAlreadySettledError(
                        f"CostEvent {eid} 는 이미 settlement {prev} 소유 — "
                        f"{sid} 이 재주장 불가")

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
                raise SettlementPersistenceError(
                    f"Settlement append 실패: {self.path} id={sid}") from e
            return settlement

    def get_settlement_strict(self, settlement_id: str) -> TurnSettlement:
        """settlement_id 로 정확히 하나의 canonical Settlement 을 돌려준다.

        없으면 SettlementNotFoundError(None 모호성 금지, §13.3).
        """
        with self._reg["lock"]:
            rows = self._read_all_locked()
        for o in rows:
            if o["settlement_id"] == settlement_id:
                return _row_to_settlement(o)
        raise SettlementNotFoundError(f"settlement_id 없음: {settlement_id}")

    def find_settlement(self, settlement_id: str):
        """편의용 비예외 조회. 없으면 None."""
        try:
            return self.get_settlement_strict(settlement_id)
        except SettlementNotFoundError:
            return None

    def list_for_transaction(self, transaction_id: str,
                             attempt: int | None = None) -> list[TurnSettlement]:
        with self._reg["lock"]:
            rows = self._read_all_locked()
        out = []
        for o in rows:
            if o.get("transaction_id") != transaction_id:
                continue
            if attempt is not None and o.get("attempt") != attempt:
                continue
            out.append(_row_to_settlement(o))
        return out


_SETTLEMENT_FIELDS = tuple(TurnSettlement.__dataclass_fields__.keys())


def _row_to_settlement(o: dict) -> TurnSettlement:
    """저장 dict → TurnSettlement. 리스트는 tuple 로 되돌린다."""
    data = dict(o)
    data["included_cost_event_ids"] = tuple(data.get("included_cost_event_ids") or ())
    data["billing_user_ids"] = tuple(data.get("billing_user_ids") or ())
    try:
        return TurnSettlement(**{k: data.get(k) for k in _SETTLEMENT_FIELDS})
    except TypeError as e:
        raise SettlementCorruptionError(f"Settlement 레코드 구조 무효: {e}") from e
