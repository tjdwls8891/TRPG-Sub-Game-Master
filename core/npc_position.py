# NPC 유효 위치(effective position) — 정본 override > 평상시 일과(routine) > untracked (H07)
#
# [권위 — H07 최종 통합 구현 지시 §3·§4]
#   유효 위치 해석의 단일 경계다. 장면 인물(turn_world)·CurrentNpcProjection·사건 조건(world_refs의
#   ACTOR_PLACE·ACTOR_PLACEMENT_KIND·ACTOR_POSITION_SOURCE)·사건 이동 출발점이 모두 이 함수를 쓴다.
#     1) 정본 spatial_state.placements[actor](AT_PLACE·TRANSIT)가 있으면 그것 — CANONICAL(override).
#        플레이어·사건 영향으로 생긴 typed 배치다. 있는 동안 일과는 무시한다.
#     2) 없으면 정의 actor_definitions[actor].routine을 정본 world_clock으로 해석 — ROUTINE(파생).
#        정의 스냅샷 + 정본 시계의 순수 함수다. 정본에 쓰지 않는다(경계마다 SET 없음 · materialize 없음).
#     3) 둘 다 없으면 UNTRACKED — '없음'이 아니라 '위치를 추적·해석하지 않음'.
#   places[*].npcs 연고(relation·frequency)는 현재 위치가 아니다 — 여기서 읽지 않는다.
#   일과 해석 = 저작 순서 첫 일치 항목, 없으면 default_place. 창 [start, end) 반열림, start > end는 자정을
#   넘으며 요일은 창이 시작한 날 기준이다(달력에 주가 없으면 컴파일러가 요일 항목을 거부한다).
from __future__ import annotations

from . import world_clock as _wc
from . import world_mutation as _wm

CANONICAL = "CANONICAL"
ROUTINE = "ROUTINE"
UNTRACKED = "UNTRACKED"
POSITION_SOURCES = (CANONICAL, ROUTINE, UNTRACKED)


def _dom(world, name):
    if isinstance(world, _wm.ProjectedWorld):
        return world._peek(name) or {}
    dom = getattr(world, "domain", None)
    if callable(dom) and not isinstance(world, type):
        return dom(name) or {}
    return getattr(world, name, None) or {}


def _now(world):
    c = _dom(world, "world_clock")
    m = c.get("minute_index")
    return m if isinstance(m, int) and not isinstance(m, bool) else None


def routine_of(definition: dict, actor_id: str):
    return ((definition.get("actor_definitions") or {}).get(actor_id) or {}).get("routine")


def _calendar(definition):
    cal = definition.get("calendar")
    return _wc.Calendar(cal) if cal else None


def _entry_matches(entry, cal, minute):
    mod = minute % _wc.MINUTES_PER_DAY
    wd = cal.weekday_of(minute)
    days = entry.get("days")
    wins = entry.get("windows")
    if wins is None:
        return days is None or wd in days
    for st, en in wins:
        if st < en:
            if st <= mod < en and (days is None or wd in days):
                return True
        else:                                   # 자정을 넘는 창 — 요일은 창이 시작한 날
            if mod >= st and (days is None or wd in days):
                return True
            if mod < en and (days is None or (wd - 1) % 7 in days):
                return True
    return False


def routine_place_at(definition: dict, actor_id: str, minute: int):
    """(place_id, entry_index|'default') 또는 None. 순수 — 정의·시각만 본다."""
    r = routine_of(definition, actor_id)
    if not r or minute is None:
        return None
    cal = _calendar(definition)
    if cal is None:
        return None
    for i, e in enumerate(r.get("entries") or ()):
        if _entry_matches(e, cal, minute):
            return e["place"], i
    if r.get("default_place"):
        return r["default_place"], "default"
    return None


def effective_position(definition: dict, world, actor_id: str) -> dict:
    """{"source": CANONICAL|ROUTINE|UNTRACKED, "placement": dict|None, "routine_entry": idx|'default'|None}."""
    pl = (_dom(world, "spatial_state").get("placements") or {}).get(actor_id)
    if pl is not None:
        return {"source": CANONICAL, "placement": dict(pl), "routine_entry": None}
    hit = routine_place_at(definition, actor_id, _now(world))
    if hit is not None:
        return {"source": ROUTINE, "placement": {"kind": "AT_PLACE", "place_id": hit[0]}, "routine_entry": hit[1]}
    return {"source": UNTRACKED, "placement": None, "routine_entry": None}


def effective_place(definition: dict, world, actor_id: str):
    """AT_PLACE 유효 장소 id(TRANSIT·untracked면 None)."""
    pl = effective_position(definition, world, actor_id)["placement"]
    return pl.get("place_id") if pl and pl.get("kind") == "AT_PLACE" else None


def candidate_actors(definition: dict, world) -> list:
    """유효 위치를 가질 수 있는 행위자(정본 배치 보유 ∪ 일과 저작 NPC) — 정렬."""
    pls = _dom(world, "spatial_state").get("placements") or {}
    routine = [a for a, d in (definition.get("actor_definitions") or {}).items() if (d or {}).get("routine")]
    return sorted(set(pls) | set(routine))


def actors_at(definition: dict, world, place_id: str, *, exclude=()) -> list:
    """유효 위치가 AT_PLACE place_id인 행위자 [(actor_id, source)] — 정렬. 장면 인물의 단일 기준."""
    out = []
    ex = set(exclude or ())
    for aid in candidate_actors(definition, world):
        if aid in ex:
            continue
        pos = effective_position(definition, world, aid)
        pl = pos["placement"]
        if pl and pl.get("kind") == "AT_PLACE" and pl.get("place_id") == place_id:
            out.append((aid, pos["source"]))
    return out


def next_routine_boundary(definition: dict, actor_id: str, now: int, *, horizon_days: int = 8):
    """일과 해석 장소가 실제로 바뀌는 다음 분(> now). 없으면 None.

    후보 = 창 경계 + (요일 항목이 있으면) 자정. 후보를 시간순으로 보며 장소가 지금과 달라지는 첫 분을 고른다
    (같은 장소로 이어지는 경계는 깨우지 않는다). 주 단위 반복이므로 8일 안에 변화가 없으면 None.
    시간 원자의 wakeup과 같은 방식 — 매분 polling 없이 예약 한 건으로 충분하다.
    """
    r = routine_of(definition, actor_id)
    if not r or now is None:
        return None
    marks = set()
    for e in r.get("entries") or ():
        for st, en in e.get("windows") or ():
            marks.update((st, en))
        if e.get("days") is not None:
            marks.add(0)
    if not marks:
        return None
    cur = (routine_place_at(definition, actor_id, now) or (None,))[0]
    base = now - now % _wc.MINUTES_PER_DAY
    for day in range(horizon_days + 1):
        for t in sorted(marks):
            m = base + day * _wc.MINUTES_PER_DAY + t
            if m <= now:
                continue
            if (routine_place_at(definition, actor_id, m) or (None,))[0] != cur:
                return m
    return None
