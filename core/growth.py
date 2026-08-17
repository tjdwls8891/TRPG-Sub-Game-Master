# 능력치 성장 · 행운 스탯 — 판정 실패를 성장 기회 또는 행운으로 전환한다
#
# [처리 순서] 능력치 판정이 실패했을 때만 개입한다.
#   1. 성장 시점인가?  (실패 횟수 누적 또는 확률)
#      예   → 동일 스탯으로 재판정 → 재실패 시 능력치 +1 (상한 적용)
#      아니오 → 2로
#   2. 백그라운드 확률 판정
#      통과 → 행운 능력치 판정 → 성공 시 실패를 행운으로 완충
#
#   판정에 성공한 경우에도 백그라운드 확률을 '낮춰' 동일하게 진행한다.
#
# [판정은 코드가 담당한다]
#   4층위 설계 확정 사항이다. 주사위·성패·성장·행운 판정은 모두 여기서
#   처리하고, 결과만 지시층위에 전달한다.
import random

# 성장 판정 기본값 — 시나리오별로 덮어쓸 수 있다.
DEFAULT_GROWTH = {
    "enabled": True,
    "fail_threshold": 3,    # 이 횟수만큼 실패가 누적되면 성장 판정
    "chance": 0.0,          # 0보다 크면 실패 시마다 이 확률로도 성장 판정
    "stat_max": 10,         # 능력치 상한
}

# 행운 판정 기본값.
DEFAULT_LUCK = {
    "enabled": True,
    "stat_name": "행운",
    "chance_on_fail": 0.10,     # 판정 실패 시 백그라운드 확률 (기본 10%)
    "chance_on_success": 0.03,  # 판정 성공 시에는 낮춘다
    "sides": 20,
}


def get_growth_config(session) -> dict:
    """시나리오 오버라이드를 반영한 성장 설정."""
    cfg = dict(DEFAULT_GROWTH)
    try:
        override = (session.scenario_data or {}).get("growth") or {}
        for k, v in override.items():
            if k in cfg and isinstance(v, type(cfg[k])):
                cfg[k] = v
    except Exception:
        pass
    return cfg


def get_luck_config(session) -> dict:
    """시나리오 오버라이드를 반영한 행운 설정."""
    cfg = dict(DEFAULT_LUCK)
    try:
        override = (session.scenario_data or {}).get("luck") or {}
        for k, v in override.items():
            if k in cfg and isinstance(v, type(cfg[k])):
                cfg[k] = v
    except Exception:
        pass
    return cfg


def _fail_counts(session) -> dict:
    if not isinstance(getattr(session, "stat_fail_counts", None), dict):
        session.stat_fail_counts = {}
    return session.stat_fail_counts


def _stat_value(session, char_name: str, stat_name: str):
    """플레이어 프로필에서 스탯 값을 읽는다. 없으면 None."""
    from .utils import get_uid_by_char_name

    uid = get_uid_by_char_name(session, char_name)
    if not uid:
        return None
    profile = (session.players.get(uid) or {}).get("profile") or {}
    if not isinstance(profile, dict) or stat_name not in profile:
        return None
    try:
        return int(profile[stat_name])
    except (TypeError, ValueError):
        return None


def _bump_stat(session, char_name: str, stat_name: str, delta: int = 1) -> int | None:
    """스탯을 증감하고 새 값을 반환한다. 상한을 넘지 않는다."""
    from .utils import get_uid_by_char_name

    uid = get_uid_by_char_name(session, char_name)
    if not uid:
        return None
    player = session.players.get(uid)
    if not isinstance(player, dict):
        return None
    profile = player.get("profile")
    if not isinstance(profile, dict) or stat_name not in profile:
        return None
    cfg = get_growth_config(session)
    try:
        cur = int(profile[stat_name])
    except (TypeError, ValueError):
        return None
    new = min(cfg["stat_max"], cur + delta)
    if new == cur:
        return None
    profile[stat_name] = new
    return new


def check_growth(session, char_name: str, stat_name: str, sides: int = 20) -> dict | None:
    """성장 판정. 판정 실패 직후에만 호출한다.

    기획 규정 — 실패 시 성장판정을 실시하고, 동일 스탯으로 주사위를 굴려
    '재실패'하면 능력치가 1 오른다. 잘 굴리는 사람이 아니라 계속 실패하는
    사람이 성장한다는 설계다.

    Returns:
        성장 판정을 수행했으면 결과 dict, 시점이 아니면 None.
        {"rolled": int, "target": int, "grew": bool, "new_value": int|None}
    """
    cfg = get_growth_config(session)
    if not cfg["enabled"]:
        return None

    counts = _fail_counts(session)
    key = f"{char_name}:{stat_name}"
    counts[key] = counts.get(key, 0) + 1

    triggered = counts[key] >= cfg["fail_threshold"]
    if not triggered and cfg["chance"] > 0:
        triggered = random.random() < cfg["chance"]
    if not triggered:
        return None

    counts[key] = 0   # 성장 판정을 했으면 누적을 초기화한다

    value = _stat_value(session, char_name, stat_name)
    if value is None:
        return None
    if value >= cfg["stat_max"]:
        return {"rolled": 0, "target": value, "grew": False,
                "new_value": value, "reason": "상한 도달"}

    roll = random.randint(1, sides)
    # 재실패(roll > target) 시 성장한다.
    grew = roll > value
    new_value = _bump_stat(session, char_name, stat_name) if grew else None
    return {"rolled": roll, "target": value, "grew": bool(new_value),
            "new_value": new_value, "reason": ""}


def check_luck(session, char_name: str, *, failed: bool) -> dict | None:
    """행운 판정. 성장 시점이 아닐 때 호출한다.

    기획 규정 — 백그라운드 확률로 확률 판정을 실행하고, 통과하면 행운
    능력치로 주사위를 굴린다. 성공하면 선언 결과의 실패를 '행운 발생'으로
    완충한다. 판정에 성공한 경우에는 백그라운드 확률을 낮춰 동일 진행한다.

    Returns:
        행운이 발생했으면 결과 dict, 아니면 None.
        {"rolled": int, "target": int, "occurred": True}
    """
    cfg = get_luck_config(session)
    if not cfg["enabled"]:
        return None

    chance = cfg["chance_on_fail"] if failed else cfg["chance_on_success"]
    if random.random() >= chance:
        return None

    value = _stat_value(session, char_name, cfg["stat_name"])
    if value is None:
        return None

    sides = int(cfg["sides"])
    roll = random.randint(1, sides)
    if roll > value:
        return None
    return {"rolled": roll, "target": value, "sides": sides, "occurred": True}


def process_roll_outcome(session, char_name: str, stat_name: str,
                         *, failed: bool, sides: int = 20) -> dict:
    """판정 결과 후처리 — 성장·행운을 순서대로 판정한다.

    Returns:
        {"growth": dict|None, "luck": dict|None}
    """
    growth = None
    if failed and stat_name:
        growth = check_growth(session, char_name, stat_name, sides)

    # 성장 판정이 일어난 턴에는 행운 판정을 하지 않는다(기획 규정 —
    # '성장시점이 아닐 때' 행운 판정을 실행한다).
    luck = None
    if growth is None:
        luck = check_luck(session, char_name, failed=failed)

    return {"growth": growth, "luck": luck}


def format_growth(char_name: str, stat_name: str, result: dict) -> str:
    """성장 결과 표시 문자열."""
    if result.get("reason") == "상한 도달":
        return f"> 📈 [{char_name}] {stat_name} 성장 판정 — 이미 상한입니다."
    if result.get("grew"):
        return (f"> 📈 **[{char_name}] {stat_name} 성장!** "
                f"(성장 판정 {result['rolled']} / 기준 {result['target']}) "
                f"→ **{result['new_value']}**")
    return (f"> 📈 [{char_name}] {stat_name} 성장 판정 "
            f"{result['rolled']} / 기준 {result['target']} — 아직입니다.")


def format_luck(char_name: str, result: dict) -> str:
    """행운 발생 표시 문자열."""
    return (f"> 🍀 **[{char_name}] 행운 발생!** "
            f"(행운 판정 {result['rolled']} / 기준 {result['target']})")
