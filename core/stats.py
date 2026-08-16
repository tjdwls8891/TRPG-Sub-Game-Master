# 통계 서비스 — 누적 플레이 기록. 계정 파일과 분리된 저장소.
#
# [저장소 분리 근거]
#   항목이 10종 이상이라 계정 파일(accounts/{uid}.json)에 넣으면 비대해진다.
#   월드보드는 전 서버 통합 집계라 별도 파일이 필요하다.
#
#   stats/{user_id}.json   개인 누적 통계
#   stats/_world.json      월드보드 집계
#
# [되감기와의 정합]
#   되감기를 해도 통계는 되돌리지 않는다. 통계는 '실제로 플레이한 양'의
#   기록이며, 되감아도 이미 발생한 API 비용은 사라지지 않기 때문이다.
import asyncio
import json
import os

STATS_DIR = "stats"
WORLD_FILE = "_world.json"
STATS_SCHEMA_VERSION = 1

# 누적 카운터 항목. 값은 전부 정수 또는 실수.
COUNTERS = {
    "turns": 0,              # 플레이 턴 수
    "sessions": 0,           # 세션 수
    "session_seconds": 0.0,  # 세션 온 시간(초)
    "quests_cleared": 0,     # 클리어 퀘스트 수
    "profiles_created": 0,   # 만든 프로필 수
    "npcs_met": 0,           # 만난 NPC 수(중복 제외)
    "sessions_cleared": 0,   # 클리어로 종료
    "sessions_failed": 0,    # 실패로 종료
    "sessions_free": 0,      # 자유 세션으로 종료
    "status_applied": 0,     # 획득한 상태이상 수
    "status_cleared": 0,     # 해제한 상태이상 수
    "dice_rolled": 0,        # 굴린 주사위 수
    "chars_in": 0,           # 입력 글자 수
    "chars_out": 0,          # 출력 글자 수
    "ink_spent": 0,          # 소모 잉크
}

_locks = {}


def _lock_for(key):
    k = str(key)
    if k not in _locks:
        _locks[k] = asyncio.Lock()
    return _locks[k]


def _path(user_id) -> str:
    return os.path.join(STATS_DIR, f"{user_id}.json")


def _blank(user_id) -> dict:
    return {
        "schema_version": STATS_SCHEMA_VERSION,
        "user_id": str(user_id),
        "public": False,          # 월드보드 공개 여부 (기본 비공개)
        "hall_registered": False,  # 명예의 전당 등록 여부
        "npc_names": [],           # 중복 집계 방지용
        "played_scenarios": [],    # 사전 프로필 생성 가능 여부 판정에 사용
        **dict(COUNTERS),
    }


def load_stats(user_id) -> dict:
    """개인 통계를 읽는다. 없으면 빈 통계를 반환한다(파일 생성하지 않음)."""
    path = _path(user_id)
    if not os.path.exists(path):
        return _blank(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[통계] 로드 실패 ({user_id}): {e}")
        return _blank(user_id)
    base = _blank(user_id)
    base.update(data)
    return base


def _write(stats: dict) -> bool:
    os.makedirs(STATS_DIR, exist_ok=True)
    path = _path(stats["user_id"])
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[통계] 저장 실패 ({stats.get('user_id')}): {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


async def bump(user_id, **deltas) -> dict:
    """카운터를 증가시킨다. 알 수 없는 키는 무시한다.

    예: await bump(uid, turns=1, dice_rolled=2)
    """
    async with _lock_for(user_id):
        st = load_stats(user_id)
        for k, v in deltas.items():
            if k in COUNTERS and isinstance(v, (int, float)):
                st[k] = (st.get(k) or 0) + v
        _write(st)
        return st


async def add_npcs(user_id, names: list) -> int:
    """만난 NPC를 중복 없이 누적한다. 새로 추가된 수를 반환한다."""
    names = [n for n in (names or []) if isinstance(n, str) and n]
    if not names:
        return 0
    async with _lock_for(user_id):
        st = load_stats(user_id)
        known = set(st.get("npc_names") or [])
        # 입력 자체에 중복이 있을 수 있으므로 집합으로 처리한다.
        new = {n for n in names if n not in known}
        if new:
            st["npc_names"] = sorted(known | new)
            st["npcs_met"] = len(st["npc_names"])
            _write(st)
        return len(new)


async def mark_played(user_id, scenario_id: str) -> bool:
    """플레이한 시나리오를 기록한다. 사전 프로필 생성 가능 판정에 쓰인다."""
    if not scenario_id:
        return False
    async with _lock_for(user_id):
        st = load_stats(user_id)
        played = set(st.get("played_scenarios") or [])
        if scenario_id in played:
            return False
        played.add(scenario_id)
        st["played_scenarios"] = sorted(played)
        _write(st)
        return True


def has_played(user_id, scenario_id: str) -> bool:
    """해당 시나리오를 플레이한 적이 있는지."""
    return scenario_id in (load_stats(user_id).get("played_scenarios") or [])


async def set_visibility(user_id, *, public: bool = None,
                         hall_registered: bool = None) -> dict:
    """월드보드 공개 여부·명예의 전당 등록 여부를 설정한다."""
    async with _lock_for(user_id):
        st = load_stats(user_id)
        if public is not None:
            st["public"] = bool(public)
        if hall_registered is not None:
            st["hall_registered"] = bool(hall_registered)
        _write(st)
        return st


def leaderboard(user_ids: list, *, key: str = "turns",
                only_registered: bool = False, limit: int = 20) -> list:
    """순위표를 만든다.

    Args:
        user_ids: 대상 유저 (서버 멤버 목록 등). 서버를 나간 인원 제거에 사용.
        key: 정렬 기준 카운터
        only_registered: 명예의 전당 등록자만
    """
    rows = []
    for uid in (user_ids or []):
        st = load_stats(uid)
        if only_registered and not st.get("hall_registered"):
            continue
        rows.append({"user_id": str(uid), "value": st.get(key) or 0, "stats": st})
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[:limit]


def format_summary(stats: dict) -> str:
    """통계 표시용 문자열."""
    hours = (stats.get("session_seconds") or 0) / 3600
    return (
        f"플레이 턴 {stats.get('turns', 0):,} · 세션 {stats.get('sessions', 0)} "
        f"({hours:.1f}시간)\n"
        f"클리어 퀘스트 {stats.get('quests_cleared', 0)} · "
        f"만난 NPC {stats.get('npcs_met', 0)} · "
        f"주사위 {stats.get('dice_rolled', 0):,}\n"
        f"상태이상 획득 {stats.get('status_applied', 0)} / "
        f"해제 {stats.get('status_cleared', 0)} · "
        f"소모 {stats.get('ink_spent', 0):,}잉크"
    )
