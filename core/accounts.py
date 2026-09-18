# 유저 계정 저장 계층 — 계정 등록, 약관 동의 버전, 잉크 잔액 원장
#
# [설계 원칙]
#   - 세션 저장(SESSION_FIELDS / SCHEMA_VERSION)과 완전히 분리된 독립 저장소.
#     계정 스키마 변경이 세션 스키마 호환성에 영향을 주지 않게 하기 위함.
#   - 계정 1개 = 파일 1개 (accounts/{user_id}.json). 동시성 충돌 범위를 유저 단위로 국한.
#   - 원자적 쓰기(.tmp -> os.replace)로 중간 크래시 시 이전 파일 보존.
#   - 잔액은 정수 잉크로만 보관한다. 원화 환산은 core/ink.py 소관.
import asyncio
import json
import os
from datetime import datetime, timezone

ACCOUNTS_DIR = "accounts"

# 계정 저장 구조 버전. 세션의 SCHEMA_VERSION과 별개로 관리한다.
ACCOUNT_SCHEMA_VERSION = 1

# 현재 약관 버전. 이 값을 올리면 기존 동의자는 재동의 대상이 된다.
CURRENT_TERMS_VERSION = 1

# 가입 선물로 지급할 잉크. 기획: "동의하면 가입선물 제공".
# 가입선물 잉크. core/terms.py의 SIGNUP_GIFT_INK가 실제 지급을 담당하므로
# 여기서는 0으로 두어 이중 지급을 막는다.
SIGNUP_BONUS_INK = 0

_locks = {}


# ══════════════════════════════════════════════════════════════
#  strict 계정 영속화 예외 (WP-ACCOUNTING-PREREQ-01 / AUD-061)
# ══════════════════════════════════════════════════════════════
#  레거시 load_account/_write_account 는 실패를 흡수한다(읽기 실패→빈 계정,
#  쓰기 실패→False). 미래 authoritative InkTransaction 은 모호한 영속화 경계를
#  쓸 수 없으므로, strict 경로는 성공/명시적 실패만 신호한다.

class AccountError(RuntimeError):
    """strict 계정 계열 예외의 베이스."""


class AccountPersistenceError(AccountError):
    """strict 계정 읽기/직렬화/쓰기/flush/fsync/replace 실패(관측 가능)."""


class AccountCorruptionError(AccountError):
    """malformed JSON, 매핑 아님, 또는 user_id/경로 정체성 불일치."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _lock_for(user_id):
    key = str(user_id)
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _path(user_id) -> str:
    return os.path.join(ACCOUNTS_DIR, f"{user_id}.json")


def _blank_account(user_id) -> dict:
    return {
        "schema_version": ACCOUNT_SCHEMA_VERSION,
        "user_id": str(user_id),
        "registered": False,
        "registered_at": None,
        "terms_version": None,
        "terms_agreed_at": None,
        "ink_balance": 0,
        "total_charged_ink": 0,
        "total_spent_ink": 0,
        # WP-SETTLEMENT-01: 멱등 InkTransaction 적용 마커(§18). 잔액과 '같은 원자적
        # 계정 파일 교체'로 함께 내구화되어, 크래시 후 재적용 이중 차감을 막는다.
        # 레거시 계정은 로드 시 이 기본값으로 채워진다(하위 호환).
        "applied_ink_transactions": {},
    }


def load_account(user_id) -> dict:
    """계정을 읽는다. 파일이 없으면 미등록 상태의 빈 계정을 반환한다(생성하지 않음)."""
    path = _path(user_id)
    if not os.path.exists(path):
        return _blank_account(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] 계정 로드 실패 ({user_id}): {e} — 빈 계정으로 대체")
        return _blank_account(user_id)

    # 필드 누락 방지: 신규 필드가 추가돼도 기본값으로 채워 로드한다.
    base = _blank_account(user_id)
    base.update(data)
    return base


def _write_account(account: dict) -> bool:
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    path = _path(account["user_id"])
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(account, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[ERROR] 계정 저장 실패 ({account.get('user_id')}): {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


# ══════════════════════════════════════════════════════════════
#  strict 계정 프리미티브 (WP-ACCOUNTING-PREREQ-01)
# ══════════════════════════════════════════════════════════════
#  레거시 load_account/_write_account/register_account/add_ink/set_balance/
#  deduct_ink 는 위에서 그대로 유지된다. 아래는 미래 authoritative
#  InkTransaction 이 소비할 명시적 프리미티브다. 현재 어떤 프로덕션 금전
#  호출자도 쓰지 않으며, 기존 per-user _lock_for 규율을 그대로 공유한다(§6.4).

def load_account_strict(user_id) -> dict:
    """계정을 strict 로 읽는다. 손상을 조용히 빈 계정으로 대체하지 않는다(§6.2).

    - 파일 부재 → 미등록 빈 계정(기존 의미 유지).
    - 읽기 실패 → AccountPersistenceError.
    - malformed JSON / 매핑 아님 → AccountCorruptionError.
    - 저장된 user_id 가 요청 경로와 상충 → AccountCorruptionError(조용히 사용 금지).
    기본 필드 채움은 tolerant load 와 동일한 규칙을 쓴다.
    """
    path = _path(user_id)
    if not os.path.exists(path):
        return _blank_account(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        raise AccountPersistenceError(f"계정 읽기 실패: {path}") from e
    try:
        data = json.loads(raw)
    except Exception as e:
        raise AccountCorruptionError(f"계정 JSON 손상: {path}") from e
    if not isinstance(data, dict):
        raise AccountCorruptionError(f"계정 레코드가 매핑이 아님: {path}")
    stored_uid = data.get("user_id")
    if stored_uid is not None and str(stored_uid) != str(user_id):
        raise AccountCorruptionError(
            f"계정 user_id 불일치: 요청={user_id!s} 저장={stored_uid!s}")
    base = _blank_account(user_id)
    base.update(data)
    base["user_id"] = str(user_id)   # 경로 정체성을 신뢰(canonical)
    return base


def _write_account_strict(account: dict) -> None:
    """계정을 strict 로 원자적 저장한다. 실패를 삼키지 않는다(§6.3).

    같은 canonical 스키마 + temp 파일 + flush/fsync + os.replace. 직렬화/쓰기/
    fsync/replace 실패는 AccountPersistenceError 로 올린다. 교체는 temp 가
    내구화된 뒤에만 일어나므로, 실패 시 기존 유효 계정 파일이 보존된다. 임시
    산출물은 기존 계약대로 정리한다. 성공 시 None, 거짓 성공은 없다.
    """
    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    path = _path(account["user_id"])
    tmp = path + ".tmp"
    try:
        try:
            payload = json.dumps(account, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as e:
            raise AccountPersistenceError(
                f"계정 직렬화 실패: {account.get('user_id')}") from e
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())             # replace 이전 내구성 경계
        except OSError as e:
            raise AccountPersistenceError(f"임시 계정 쓰기/fsync 실패: {tmp}") from e
        try:
            os.replace(tmp, path)                # 원자적 교체
        except OSError as e:
            raise AccountPersistenceError(f"계정 원자적 교체 실패: {path}") from e
    finally:
        # 실패 시 임시 산출물 정리(교체 성공 시 tmp 는 이미 없다).
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ══════════════════════════════════════════════════════════════
#  멱등 InkTransaction 계정 적용 프리미티브 (WP-SETTLEMENT-01 / 핸드오프 §17~§22)
# ══════════════════════════════════════════════════════════════
#  authoritative InkTransaction executor(core/ink_transactions.py)가 소비하는
#  '계정측' 원자 연산이다. 계정 파일·스키마·per-user 락은 계정 모듈 소관이므로,
#  잔액 변이 + 누적 필드 + applied 마커를 '하나의 strict 원자 교체'로 함께
#  내구화하는 책임을 여기에 둔다. 예외 계열 결합을 피하려고, 잔액 부족/마커 상충은
#  raise 대신 status 로 신호한다(I/O·손상만 Account* 예외). 어떤 프로덕션 금전
#  호출자도 아직 쓰지 않는다.

APPLY_NEW = "APPLIED_NEW"           # 신규 적용(잔액+마커 내구화됨)
APPLY_ALREADY = "ALREADY_APPLIED"   # 동일 fingerprint 마커 존재 — 무변이
APPLY_CONFLICT = "CONFLICT"         # 동일 ink_tx_id + 상충 fingerprint(무변이)
APPLY_INSUFFICIENT = "INSUFFICIENT" # overdraft 불허 + 잔액 부족(무변이)


async def get_applied_ink_marker(user_id, ink_tx_id: str):
    """계정에 durable 하게 남은 applied 마커를 strict 로 읽는다(없으면 None).

    executor 의 복구 라우팅(§20)이 '계정이 이미 적용했는가'를 판정하는 근거다.
    """
    async with _lock_for(user_id):
        acc = load_account_strict(user_id)
        applied = acc.get("applied_ink_transactions") or {}
        marker = applied.get(str(ink_tx_id))
        return dict(marker) if isinstance(marker, dict) else None


async def apply_ink_charge_strict(user_id, *, ink_tx_id: str, fingerprint: str,
                                  settlement_id: str, nominal_charge_ink: int,
                                  allow_overdraft: bool = True) -> dict:
    """하나의 CHARGE 를 계정에 '정확히 한 번' 원자 적용한다(§17).

    순서: per-user 락 획득 → strict 로드 → applied 마커 검사 → 신규면 불변 결과
    계산 → 잔액 변이 + 누적 필드 + 마커를 '같은 strict 원자 교체'로 함께 기록.
    그 마커가 크래시 후 재적용(이중 차감)을 막는 단일 근거다(별도 ledger 에만
    의존하지 않는다, §18).

    overdraft 규약은 레거시 deduct_ink 와 동일하다: allow_overdraft 이면 잔액이
    1 미만이 되는 경우 결과 잔액을 1 로 맞추고 초과분은 운영자 부담으로 노출한다.
    allow_overdraft 불허 + nominal>잔액이면 무변이로 INSUFFICIENT 를 돌려준다.

    Returns:
        {"status": APPLY_*, "marker": {...}}  (INSUFFICIENT 는 marker 없이 balance_before)
    """
    nominal = int(nominal_charge_ink)
    if nominal < 0:
        raise AccountError(f"nominal_charge_ink 는 음수일 수 없다: {nominal}")
    key = str(ink_tx_id)

    async with _lock_for(user_id):
        acc = load_account_strict(user_id)
        applied = acc.get("applied_ink_transactions")
        if not isinstance(applied, dict):
            applied = {}

        existing = applied.get(key)
        if isinstance(existing, dict):
            if existing.get("fingerprint") == fingerprint:
                return {"status": APPLY_ALREADY, "marker": dict(existing)}
            # 마커를 조용히 덮어쓰지 않는다(§18).
            return {"status": APPLY_CONFLICT, "marker": dict(existing)}

        balance_before = int(acc.get("ink_balance", 0))

        # 잔액 부족 + overdraft 불허 → 무변이 명시적 신호(§22).
        if nominal > balance_before and not allow_overdraft:
            return {"status": APPLY_INSUFFICIENT, "balance_before": balance_before}

        # 레거시 deduct_ink 와 동일한 floor 규약(§21).
        remaining = balance_before - nominal
        overdraft = remaining < 1
        if overdraft:
            remaining = 1
        balance_after = remaining
        actual_reduction = balance_before - balance_after
        operator_subsidy = nominal - actual_reduction
        applied_balance_delta = balance_after - balance_before

        marker = {
            "fingerprint": fingerprint,
            "settlement_id": str(settlement_id),
            "nominal_charge_ink": nominal,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "applied_balance_delta": applied_balance_delta,
            "overdraft": bool(overdraft),
            "operator_subsidy_ink": int(operator_subsidy),
        }

        # 잔액 + 레거시 호환 누적 필드 + 마커를 '한 번의 원자 교체'로 함께 기록.
        acc["ink_balance"] = balance_after
        # total_spent_ink 는 레거시 deduct_ink 와 같이 nominal 기준으로 누적한다
        # (호환 필드). 실제 잔액 변화는 마커의 applied_balance_delta 로 별도 노출.
        acc["total_spent_ink"] = int(acc.get("total_spent_ink", 0)) + nominal
        applied[key] = marker
        acc["applied_ink_transactions"] = applied

        _write_account_strict(acc)   # 실패 시 AccountPersistenceError — 무변이 보존
        return {"status": APPLY_NEW, "marker": dict(marker)}


def is_registered(user_id) -> bool:
    """계정 등록 여부. 미등록자는 GM 홈 UI 접근이 차단된다."""
    return bool(load_account(user_id).get("registered"))


def needs_terms_reagreement(user_id) -> bool:
    """동의한 약관 버전이 현재 버전보다 낮은지 판정한다.

    기획 규정: 세션 생성·오픈 시점에 현 버전과 비교해 재동의가 필요하면 DM에서 진행.
    미등록자는 재동의가 아니라 신규 등록 대상이므로 False를 반환한다.
    """
    acc = load_account(user_id)
    if not acc.get("registered"):
        return False
    return (acc.get("terms_version") or 0) < CURRENT_TERMS_VERSION


async def register_account(user_id, terms_version: int = None) -> dict:
    """약관 동의를 받아 계정을 등록한다. 이미 등록된 계정은 약관 버전만 갱신한다.

    Returns:
        갱신된 계정 dict
    """
    version = CURRENT_TERMS_VERSION if terms_version is None else terms_version
    async with _lock_for(user_id):
        acc = load_account(user_id)
        first_time = not acc.get("registered")
        if first_time:
            acc["registered"] = True
            acc["registered_at"] = _now()
            if SIGNUP_BONUS_INK:
                acc["ink_balance"] = int(acc.get("ink_balance", 0)) + SIGNUP_BONUS_INK
                acc["total_charged_ink"] = int(acc.get("total_charged_ink", 0)) + SIGNUP_BONUS_INK
        acc["terms_version"] = version
        acc["terms_agreed_at"] = _now()
        _write_account(acc)
        return acc


def get_balance(user_id) -> int:
    """현재 잉크 잔액."""
    return int(load_account(user_id).get("ink_balance", 0))


async def add_ink(user_id, amount: int, reason: str = "충전") -> int:
    """잉크를 적립한다(충전·환급 공용). 갱신된 잔액을 반환한다."""
    amount = int(amount)
    if amount <= 0:
        return get_balance(user_id)
    async with _lock_for(user_id):
        acc = load_account(user_id)
        acc["ink_balance"] = int(acc.get("ink_balance", 0)) + amount
        if reason == "충전":
            acc["total_charged_ink"] = int(acc.get("total_charged_ink", 0)) + amount
        _write_account(acc)
        return acc["ink_balance"]


async def set_balance(user_id, amount: int, reason: str = "운영자 조정") -> dict:
    """잔액을 지정 값으로 맞춘다(오너 전용 경로에서만 쓴다).

    add_ink·deduct_ink는 증감만 다루므로, 잘못된 잔액을 바로잡거나
    테스트 계정을 특정 값으로 세팅할 때 쓸 수단이 없었다.

    Returns:
        {"ok": bool, "before": int, "after": int, "delta": int}
    """
    amount = max(0, int(amount))
    async with _lock_for(user_id):
        acc = load_account(user_id)
        before = int(acc.get("ink_balance", 0))
        acc["ink_balance"] = amount
        acc.setdefault("history", []).append({
            "at": _now(),
            "delta": amount - before,
            "balance": amount,
            "reason": reason,
        })
        ok = _write_account(acc)
    return {"ok": ok, "before": before, "after": amount,
            "delta": amount - before}


async def deduct_ink(user_id, amount: int, allow_overdraft: bool = False) -> dict:
    """잉크를 차감한다.

    기획 규정:
      - 선불식이므로 잔액이 부족하면 차감하지 않고 실패를 반환한다.
      - 단, 사전 예상 최대금액을 통과해 플레이가 허용된 뒤 실제 비용이 이를
        초과한 경우(allow_overdraft=True)는 차감을 허용하되,
        결과 잔액을 1잉크로 맞춘다(초과분은 운영자 부담).

    Returns:
        {"ok": bool, "balance": int, "deducted": int, "overdraft": bool}
    """
    amount = int(amount)
    if amount <= 0:
        return {"ok": True, "balance": get_balance(user_id), "deducted": 0, "overdraft": False}

    async with _lock_for(user_id):
        acc = load_account(user_id)
        balance = int(acc.get("ink_balance", 0))

        if amount > balance and not allow_overdraft:
            return {"ok": False, "balance": balance, "deducted": 0, "overdraft": False}

        remaining = balance - amount
        overdraft = remaining < 1
        if overdraft:
            # 초과 차감 발생 — 잔액을 1잉크로 보정한다.
            remaining = 1

        acc["ink_balance"] = remaining
        acc["total_spent_ink"] = int(acc.get("total_spent_ink", 0)) + amount
        _write_account(acc)
        return {"ok": True, "balance": remaining, "deducted": amount, "overdraft": overdraft}
