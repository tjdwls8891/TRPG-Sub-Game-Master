# 시작 상황 틀 — 세션 시작을 프로필에 맞춰 다양화한다
#
# [기획 규정]
#   틀별로 후보 등록 조건을 설정해 유저에게 부적합한 상황을 선택지로 주지 않되,
#   조건을 범위로 설정하고 빈칸을 유저 프로필과 정해진 목록에서 랜덤 선택한
#   요소로 채운다. 여러 틀을 겪고 같은 틀이 걸리더라도 색다른 경험이 되게 한다.
#   어떤 프로필 조합에도 5가지 이상이 준비되어야 하며,
#   유저에게는 랜덤 선택한 삼지선다 중 고르게 한다.
#
# [지시사항 분리]
#   틀은 '인트로 생성 지시'와 '사전 확정 정보'를 함께 갖는다.
#   추출 정보를 미리 정해두면 인트로 직후의 세계 상태가 흔들리지 않는다.
import random

# 유저에게 제시할 선택지 수 (기획 규정 — 삼지선다).
CHOICE_COUNT = 3

# 어떤 프로필 조합에도 최소 이만큼은 후보가 남아야 한다(기획 규정).
MIN_CANDIDATES = 5


def get_frames(scenario_data: dict) -> list:
    """시나리오의 시작 틀 목록."""
    frames = (scenario_data or {}).get("start_frames")
    return frames if isinstance(frames, list) else []


def _matches(profile: dict, cond: dict) -> bool:
    """틀의 후보 등록 조건을 프로필이 만족하는지.

    조건은 범위로 설정한다(기획 규정) — 값 목록이면 포함 여부,
    min_/max_ 접두는 수치 비교.
    """
    for key, want in (cond or {}).items():
        if key.startswith("min_"):
            field = key[4:]
            try:
                if int(_stat_of(profile, field)) < int(want):
                    return False
            except (TypeError, ValueError):
                return False
        elif key.startswith("max_"):
            field = key[4:]
            try:
                if int(_stat_of(profile, field)) > int(want):
                    return False
            except (TypeError, ValueError):
                return False
        else:
            val = profile.get(key)
            if isinstance(want, list):
                if val not in want:
                    return False
            elif val != want:
                return False
    return True


def _stat_of(profile: dict, field: str):
    """프로필에서 수치 항목을 꺼낸다. 능력치 하위 항목도 본다."""
    if field in profile:
        return profile[field]
    stats = profile.get("능력치")
    if isinstance(stats, dict) and field in stats:
        return stats[field]
    return None


def filter_frames(scenario_data: dict, profile: dict) -> list:
    """프로필에 맞는 틀만 추린다.

    조건에 맞는 틀이 MIN_CANDIDATES 미만이면 조건 없는 범용 틀로 채운다.
    부적합한 상황을 주지 않되 선택지가 고갈되지도 않게 하기 위함이다.
    """
    frames = get_frames(scenario_data)
    matched = [f for f in frames if _matches(profile, f.get("conditions") or {})]
    if len(matched) >= MIN_CANDIDATES:
        return matched

    universal = [f for f in frames
                 if not (f.get("conditions") or {}) and f not in matched]
    return matched + universal


# 조사 보정과 슬롯 치환은 koreantext가 담당한다.
# 퀘스트 가이드도 같은 처리를 하므로 공용 모듈로 분리했다.
from .koreantext import substitute, strip_unfilled


def _fill_slot(scenario_data: dict, profile: dict, spec) -> str:
    """빈칸 하나를 채운다.

    'profile:필드'  → 프로필 값
    'list:키'       → 시나리오 목록에서 랜덤
    그 외           → 문자열 그대로
    """
    if not isinstance(spec, str):
        return str(spec)
    if spec.startswith("profile:"):
        field = spec[8:]
        val = _stat_of(profile, field)
        return str(val) if val is not None else "미상"
    if spec.startswith("place:"):
        # 시작 지점 — 표기는 평범한 장소여야 한다. 특별 취급하면
        # 프롬프트에 '시작 지점'이라는 메타 정보가 새어 변질된다.
        pool = (scenario_data or {}).get("start_points") or []
        return str(random.choice(pool)) if pool else "미상"
    if spec.startswith("list:"):
        key = spec[5:]
        pool = (scenario_data or {}).get(key)
        if isinstance(pool, dict):
            pool = list(pool.keys())
        if isinstance(pool, list) and pool:
            return str(random.choice(pool))
        return "미상"
    return spec


def realize(scenario_data: dict, profile: dict, frame: dict) -> dict:
    """틀의 빈칸을 채워 하나의 시작 상황으로 만든다.

    같은 틀이 다시 걸려도 빈칸이 달라져 색다른 경험이 된다(기획 규정).

    Returns:
        {"id", "title", "summary", "instruction", "facts", "slots"}
    """
    slots = {k: _fill_slot(scenario_data, profile, v)
             for k, v in (frame.get("slots") or {}).items()}

    def _apply(text: str) -> str:
        return substitute(text, slots)

    facts = {k: _apply(v) if isinstance(v, str) else v
             for k, v in (frame.get("facts") or {}).items()}

    # 문안이 여럿이면 그중 하나를 고른다. 틀당 문안 하나면 골격이 반복된다.
    pool = frame.get("summaries")
    raw_summary = (random.choice(pool) if isinstance(pool, list) and pool
                   else frame.get("summary"))

    return {
        "id": frame.get("id"),
        "title": _apply(frame.get("title")),
        "summary": _apply(raw_summary),
        "instruction": _apply(frame.get("instruction")),
        "facts": facts,
        "slots": slots,
    }


def offer(scenario_data: dict, profile: dict, *, count: int = CHOICE_COUNT) -> list:
    """제시할 시작 상황 후보를 만든다.

    조건에 맞는 틀 중 랜덤으로 count개를 골라 각각 빈칸을 채운다.
    """
    pool = filter_frames(scenario_data, profile)
    if not pool:
        return []
    picked = random.sample(pool, min(count, len(pool)))
    return [realize(scenario_data, profile, f) for f in picked]


# ── 프로필 브리핑 ─────────────────────────────────────────
def get_briefings(scenario_data: dict) -> list:
    """브리핑 양식 목록. 인트로 초반에 플레이어 정보를 읊는 데 쓴다."""
    b = (scenario_data or {}).get("briefing_formats")
    return b if isinstance(b, list) else []


def build_briefing(scenario_data: dict, profile: dict) -> str:
    """브리핑 양식 중 하나를 랜덤 선택해 채운다(기획 규정).

    양식이 없으면 기본 형식으로 조립한다.
    """
    formats = get_briefings(scenario_data)
    if formats:
        text = random.choice(formats)
        flat = {}
        for key, val in (profile or {}).items():
            if isinstance(val, dict):
                val = ", ".join(f"{k} {v}" for k, v in val.items())
            elif isinstance(val, list):
                val = ", ".join(str(x) for x in val)
            flat[key] = str(val)
        return substitute(text, flat)

    parts = []
    for key in ("이름", "성별", "나이", "출신", "소속"):
        if profile.get(key):
            parts.append(f"{key} {profile[key]}")
    return " · ".join(parts)


def build_intro_instruction(scenario_data: dict, profile: dict,
                            chosen: dict) -> str:
    """인트로 생성 지시문을 조립한다.

    기획 규정 — 브리핑 이후 인트로를 연결해 한 번에 스트리밍하고,
    원경에서 근경으로 배경·상황에 포커스를 맞추며 몰입되게 시작한다.
    """
    briefing = build_briefing(scenario_data, profile)
    return (
        "[인트로 생성 지시]\n"
        "아래 브리핑으로 시작해, 이어서 시작 상황을 묘사하십시오.\n"
        "브리핑과 묘사는 하나의 흐름으로 이어져야 합니다.\n\n"
        f"[플레이어 브리핑 — 이 내용으로 시작할 것]\n{briefing}\n\n"
        f"[시작 상황]\n{chosen.get('summary', '')}\n\n"
        f"[묘사 지시]\n{chosen.get('instruction', '')}\n\n"
        "[연출 원칙]\n"
        "- 원경에서 근경으로 좁혀 들어갑니다. 섬과 하늘, 그다음 거리, 그다음 손끝.\n"
        "- 배경과 상황에 포커스를 맞춰 몰입되게 시작합니다.\n"
        "- 플레이어가 선언하지 않은 행동·감정을 넣지 마십시오.\n"
        "- 마지막은 플레이어가 행동을 정할 수 있는 지점에서 멈춥니다.\n"
    )


def apply_facts(session, chosen: dict):
    """사전 확정 정보를 세계 상태에 반영한다.

    기획 규정 — 추출 정보는 사전 확정 정보로 미리 정해둔다.
    인트로 직후 추출층위가 다시 판단하지 않아도 되도록 못박는다.
    """
    facts = chosen.get("facts") or {}
    tl = dict(getattr(session, "world_timeline", {}) or {})
    for key, target in (("date", "current_date"), ("time", "time_of_day"),
                        ("location", "current_location"),
                        ("faction", "faction_context")):
        if facts.get(key):
            tl[target] = facts[key]
    session.world_timeline = tl

    if facts.get("player_faction"):
        session.player_faction = facts["player_faction"]
    return tl


def format_choice(index: int, chosen: dict) -> str:
    """선택지 표시 문자열."""
    return f"**{index}. {chosen.get('title', '(제목 없음)')}**\n> {chosen.get('summary', '')}"
