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
