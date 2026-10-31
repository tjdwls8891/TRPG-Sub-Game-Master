"""공간 — 행위자 위치(actor.pos)·해제 보류(actor.hold)·장면(world.scene)·접근 상태(world.access)·공간 지식(actor.map).

  actor.pos    {} 정본 배치 없음(유효 위치는 일과/untracked) | AT_PLACE{kind, place_id} | TRANSIT{7키}
  actor.hold   {} | {"placement", "at_minute"} — 배치 해제 시각의 위치(같은 분 순간이동 방지)
  world.scene  {} | {"place": PlaceId}
  world.access {control_id: {"state"}}
  actor.map    {"places": {pid: 4키}, "edges": {eid: 9키}, "access": {ref: 5키}, "revision": n} — 희소(기록 없음 = 모름)
의미는 옛 core.spatial handler와 같다. 옛 ACTOR_PLACEMENT_CLEAR(세 도메인 쓰기)는 효과 셋으로 나뉜다:
  actor.pos clear · actor.hold hold_current · world.schedule drop_movement_wakeups.
"""
from decimal import Decimal, InvalidOperation

from indaim.kernel import WORLD, Component, canonical_json
from indaim.kernel.components import ADDITIVE, DELETE, SINGLE_VALUE, write

from .._common import ident, is_int, keys, reject
from ..time.components import clock_of, ready_clock, valid

AT_PLACE, TRANSIT = "AT_PLACE", "TRANSIT"
TRANSIT_KEYS = ("kind", "movement_id", "edge_id", "from_place", "to_place",
                "edge_started_at_minute", "edge_expected_end_minute")
PLACE_KNOWLEDGE_KEYS = ("knows_name", "visited", "rough_location", "acquisitions")
EDGE_KNOWLEDGE_KEYS = ("connection_known", "distance_estimate_m", "distance_precision", "last_confirmed_at",
                       "acquisition_sources", "known_allowed_modes", "known_movement_tags", "known_time_factor",
                       "known_fixed_delay_minutes")
ACCESS_FACT_KEYS = ("observed_result", "observed_state", "action_policy", "confirmed_at_minute", "source")
PROVENANCE_KEYS = ("acquisitions", "acquisition_sources")
ACCESS_RESULTS = ("PASSABLE", "ACTION_REQUIRED", "IMPASSABLE")


def _actor(e):
    return e.address[1]


# ── 위치 ──

def _place(view, e):
    keys(e, ("place_id",))
    return [write(e, (), SINGLE_VALUE, {"kind": AT_PLACE, "place_id": ident(e, "place_id")})]


def _transit(view, e):
    a = keys(e, TRANSIT_KEYS[1:])
    pl = {"kind": TRANSIT, **{k: a[k] for k in TRANSIT_KEYS[1:]}}
    errs = [f"{k}는 비어 있지 않은 문자열이어야 합니다" for k in ("movement_id", "edge_id", "from_place", "to_place")
            if not isinstance(pl[k], str) or not pl[k]]
    s, end = pl["edge_started_at_minute"], pl["edge_expected_end_minute"]
    if not (is_int(s) and is_int(end)) or end <= s:
        errs.append("edge_started_at_minute < edge_expected_end_minute 정수여야 합니다(0분 이동은 TRANSIT을 만들지 않는다)")
    if pl["from_place"] == pl["to_place"]:
        errs.append("TRANSIT 양 끝이 같습니다")
    if errs:
        reject(e, "; ".join(errs))
    now = ready_clock(view, e, "TRANSIT")["minute_index"]
    if not s <= now < end:
        reject(e, f"현재 시각 {now}이 구간 [{s}, {end}) 밖입니다(도착 시각이면 AT_PLACE)")
    return [write(e, (), SINGLE_VALUE, pl)]


def _clear(view, e):
    """정본 배치를 지운다. 배치가 없으면 no-op(이미 일과/untracked)."""
    keys(e, ())
    return [write(e, (), DELETE, None)] if view.stored_has("actor", _actor(e), "pos") else []


def _hold_current(view, e):
    """같은 계층의 배치 해제와 짝 — 해제 시각의 위치를 보류로 남긴다(배치가 없거나 시계가 없으면 no-op)."""
    keys(e, ())
    if not view.stored_has("actor", _actor(e), "pos"):
        return []
    c = clock_of(view)
    if not valid(c):
        return []
    return [write(e, (), SINGLE_VALUE, {"placement": view.read("actor", _actor(e), "pos"), "at_minute": c["minute_index"]})]


def _scene(view, e):
    keys(e, ("place_id",))
    return [write(e, ("place",), SINGLE_VALUE, ident(e, "place_id"))]


def _access_set(view, e):
    keys(e, ("control_id", "state"))
    return [write(e, (ident(e, "control_id"),), SINGLE_VALUE, {"state": ident(e, "state")})]


# ── 공간 지식 ──

def _dedupe(old, new):
    out, seen = [], set()
    for x in list(old) + list(new):
        k = canonical_json(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _fixed_decimal(v):
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        dv = Decimal(str(v))
    except InvalidOperation:
        return None
    if not dv.is_finite() or dv <= 0 or dv.as_tuple().exponent < -6:
        return None
    return format(dv.normalize(), "f")


def _v_place(rec):
    for k in ("knows_name", "visited"):
        if rec[k] is None:
            rec[k] = False
        if not isinstance(rec[k], bool):
            return f"{k}는 bool이어야 합니다"
    if rec["acquisitions"] is None:
        rec["acquisitions"] = []
    if not isinstance(rec["acquisitions"], list):
        return "acquisitions는 목록이어야 합니다"
    return None


def _v_edge(rec):
    if rec["connection_known"] is not True:
        return "connection_known=true만 기록합니다(모름 = 기록 없음)"
    d = rec["distance_estimate_m"]
    if d is not None and (not is_int(d) or d < 0):
        return "distance_estimate_m은 0 이상의 정수여야 합니다"
    if rec["acquisition_sources"] is None:
        rec["acquisition_sources"] = []
    if not isinstance(rec["acquisition_sources"], list):
        return "acquisition_sources는 목록이어야 합니다"
    for k in ("known_allowed_modes", "known_movement_tags"):
        v = rec[k]
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return f"{k}는 문자열 목록 또는 null이어야 합니다"
    f = rec["known_time_factor"]
    if f is not None:
        norm = _fixed_decimal(f)
        if norm is None:
            return "known_time_factor는 0보다 큰 소수 6자리 이내 수여야 합니다"
        rec["known_time_factor"] = norm
    dl = rec["known_fixed_delay_minutes"]
    if dl is not None and (not is_int(dl) or dl < 0):
        return "known_fixed_delay_minutes는 0 이상의 정수여야 합니다"
    return None


def _v_access(rec):
    if rec["observed_result"] not in ACCESS_RESULTS:
        return f"observed_result는 {list(ACCESS_RESULTS)} 중 하나여야 합니다"
    return None


def _upsert(view, e, section, key, fields, allowed, validate):
    if not isinstance(fields, dict):
        reject(e, "fields는 객체여야 합니다")
    extra = set(fields) - set(allowed)
    if extra:
        reject(e, f"알 수 없는 지식 필드 {sorted(extra)}")
    cur = view.read("actor", _actor(e), "map", (section, key)) or {}
    rec = dict(cur)
    for k, v in fields.items():
        if k in PROVENANCE_KEYS:
            if v is not None and not isinstance(v, list):
                reject(e, f"{k}는 목록이어야 합니다")
            rec[k] = _dedupe(cur.get(k) or [], v or [])
        else:
            rec[k] = v
    for k in allowed:
        rec.setdefault(k, None)
    err = validate(rec)
    if err:
        reject(e, err)
    return [write(e, (section, key), SINGLE_VALUE, rec), write(e, ("revision",), ADDITIVE, 1)]


def _place_know(view, e):
    a = keys(e, ("place_id", "fields"))
    return _upsert(view, e, "places", ident(e, "place_id"), a["fields"], PLACE_KNOWLEDGE_KEYS, _v_place)


def _edge_know(view, e):
    a = keys(e, ("edge_id", "fields"))
    fields = dict(a["fields"], connection_known=True) if isinstance(a["fields"], dict) else a["fields"]
    return _upsert(view, e, "edges", ident(e, "edge_id"), fields, EDGE_KNOWLEDGE_KEYS, _v_edge)


def _access_know(view, e):
    a = keys(e, ("ref", "fields"))
    return _upsert(view, e, "access", ident(e, "ref"), a["fields"], ACCESS_FACT_KEYS, _v_access)


COMPONENTS = (
    Component("pos", "actor", "rules.space", {"place": _place, "transit": _transit, "clear": _clear}),
    Component("hold", "actor", "rules.space", {"hold_current": _hold_current}),
    Component("scene", WORLD, "rules.space", {"set": _scene}),
    Component("access", WORLD, "rules.space", {"set": _access_set}),
    Component("map", "actor", "rules.space", {"place_know": _place_know, "edge_know": _edge_know,
                                              "access_know": _access_know}),
)
