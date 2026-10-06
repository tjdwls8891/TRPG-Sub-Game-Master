# 공간 — 방향 그래프 · 정적 가시성 · 배치/TRANSIT · 공간 지식 · 공간 typed 연산 (H03-B)
#
# [권위 — DESIGN 02 · DESIGN 09 §1~8 · H03 결정 게이트 A9·A13·A14]
#   실제 이동 그래프 = 컴파일된 정의의 movement_edges(방향 구간)뿐이다.
#     · parent/child 계층은 구간이 아니다(포함 지름길 없음).
#     · legacy connected를 구간으로 승격하지 않는다.
#   가시성 = 저작된 visible_within 정적 관계(core.places.is_visible_from과 같은 의미).
#     거리·홉으로 가시성을 만들지 않는다. 접근과 가시성은 다른 질의다.
#
# [canonical 형태]
#   spatial_state = {"scene_place": PlaceId?, "placements": {actor_id: Placement}}
#     Placement AT_PLACE = {"kind": "AT_PLACE", "place_id"}
#     Placement TRANSIT  = {"kind": "TRANSIT", "movement_id", "edge_id", "from_place", "to_place",
#                           "edge_started_at_minute", "edge_expected_end_minute"}
#       진행률(progress_ratio)은 저장하지 않는다 — 시계에서 파생한다.
#   spatial_knowledge = {actor_id: {"places": {pid: PlaceKnowledge},
#                                   "edges": {eid: EdgeKnowledge},
#                                   "access": {ref: KnownAccessFact},
#                                   "revision": int}}
#     희소: 기록 없음 = 모름. connection_known=false 기록은 만들지 않는다.
#     실제 세계 변화(문 잠김·위치 이동)는 지식을 자동 갱신하지 않는다.
#
# [하지 않는 것]
#   · production 플레이어 경로가 이 연산을 만들지 않는다(H03 producer 0, 배선은 H05).
#   · 정의 스냅샷을 세션에 바인딩하지 않는다(H06) — handler는 형태·시계·인과만 검증하고
#     정의 참조(PlaceId·EdgeId 실재)는 producer가 정의를 들고 검증한다.
from __future__ import annotations

from . import world_clock as _wc
from . import world_mutation as _wm

AT_PLACE = "AT_PLACE"
TRANSIT = "TRANSIT"
TRANSIT_KEYS = ("kind", "movement_id", "edge_id", "from_place", "to_place",
                "edge_started_at_minute", "edge_expected_end_minute")

FAMILIARITY_VISIBILITY = ("PUBLIC", "LOCAL")      # familiarity로 획득 가능한 구간 분류

PLACE_KNOWLEDGE_KEYS = ("knows_name", "visited", "rough_location", "acquisitions")
EDGE_KNOWLEDGE_KEYS = ("connection_known", "distance_estimate_m", "distance_precision",
                       "last_confirmed_at", "acquisition_sources")
ACCESS_FACT_KEYS = ("observed_result", "observed_state", "action_policy",
                    "confirmed_at_minute", "source")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ══════════════════════════════════════════════════════════════
#  실제 그래프 · 계층 · 정적 가시성
# ══════════════════════════════════════════════════════════════

class SpatialGraph:
    """컴파일된 정의의 방향 구간 그래프. 인접 목록은 edge_id 순(삽입 순서 무관)."""

    def __init__(self, definition: dict):
        self.places = definition.get("place_definitions") or {}
        self.edges = definition.get("movement_edges") or {}
        self.modes = definition.get("movement_modes") or {}
        out, inc = {}, {}
        for eid in sorted(self.edges):
            e = self.edges[eid]
            out.setdefault(e["from"], []).append(eid)
            inc.setdefault(e["to"], []).append(eid)
        self._out, self._in = out, inc

    def out_edges(self, place_id: str) -> tuple:
        return tuple(self._out.get(place_id, ()))

    def in_edges(self, place_id: str) -> tuple:
        return tuple(self._in.get(place_id, ()))

    def edge(self, edge_id: str) -> dict:
        return self.edges[edge_id]

    def neighbors(self, place_id: str) -> tuple:
        return tuple(self.edges[e]["to"] for e in self.out_edges(place_id))

    # ── 계층(구간 아님) ──
    def parent_of(self, place_id: str):
        return (self.places.get(place_id) or {}).get("parent")

    def parents_of(self, place_id: str) -> list:
        rec = self.places.get(place_id) or {}
        out = [rec["parent"]] if rec.get("parent") else []
        for p in rec.get("extra_parents") or ():
            if p not in out:
                out.append(p)
        return out

    def path_of(self, place_id: str) -> list:
        """최상위 → 자신(주 parent 사슬). core.places.path_of와 같은 의미."""
        chain, cur, seen = [], place_id, set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            ps = self.parents_of(cur)
            cur = ps[0] if ps else None
        return list(reversed(chain))

    def descendants_of(self, place_id: str) -> tuple:
        """하위 전체(parent·extra_parents 기준) + 자신. 정렬된 튜플."""
        kids = {}
        for pid in sorted(self.places):
            for p in self.parents_of(pid):
                kids.setdefault(p, []).append(pid)
        seen, stack = {place_id}, [place_id]
        while stack:
            for k in kids.get(stack.pop(), ()):
                if k not in seen:
                    seen.add(k)
                    stack.append(k)
        return tuple(sorted(seen))

    # ── 정적 가시성 ──
    def is_visible_from(self, target: str, current: str) -> bool:
        """저작된 visible_within 게이트만 본다(거리·홉 미사용)."""
        gate = (self.places.get(target) or {}).get("visible_within") or []
        if not gate:
            return True
        scope = set(self.path_of(current)) | {current}
        return any(g in scope for g in gate)


def mode_allows_edge(mode: dict, edge: dict) -> tuple:
    """(허용 여부, 사유). 구간 allowed_modes · 방식 allowed/forbidden 태그를 본다."""
    if edge.get("allowed_modes") is not None and mode["mode_id"] not in edge["allowed_modes"]:
        return False, "EDGE_MODE_NOT_ALLOWED"
    tags = set(edge.get("movement_tags") or ())
    if tags & set(mode.get("forbidden_edge_tags") or ()):
        return False, "MODE_FORBIDDEN_TAG"
    allowed = mode.get("allowed_edge_tags")
    if allowed is not None and not (tags & set(allowed)):
        return False, "MODE_TAG_NOT_ALLOWED"
    return True, None


# ══════════════════════════════════════════════════════════════
#  배치 · TRANSIT
# ══════════════════════════════════════════════════════════════

def at_place(place_id: str) -> dict:
    return {"kind": AT_PLACE, "place_id": place_id}


def transit(*, movement_id, edge_id, from_place, to_place, edge_started_at_minute,
            edge_expected_end_minute) -> dict:
    return {"kind": TRANSIT, "movement_id": movement_id, "edge_id": edge_id,
            "from_place": from_place, "to_place": to_place,
            "edge_started_at_minute": edge_started_at_minute,
            "edge_expected_end_minute": edge_expected_end_minute}


def validate_placement(pl) -> list:
    if not isinstance(pl, dict):
        return ["배치는 객체여야 합니다"]
    if pl.get("kind") == AT_PLACE:
        if set(pl) != {"kind", "place_id"} or not isinstance(pl["place_id"], str) or not pl["place_id"]:
            return [f"AT_PLACE 형태가 잘못됐습니다: {pl!r}"]
        return []
    if pl.get("kind") == TRANSIT:
        if set(pl) != set(TRANSIT_KEYS):
            return [f"TRANSIT 키는 {list(TRANSIT_KEYS)}여야 합니다(progress_ratio 저장 금지)"]
        errs = [f"{k}는 비어 있지 않은 문자열이어야 합니다"
                for k in ("movement_id", "edge_id", "from_place", "to_place")
                if not isinstance(pl[k], str) or not pl[k]]
        s, e = pl["edge_started_at_minute"], pl["edge_expected_end_minute"]
        if not (_is_int(s) and _is_int(e)) or e < s:
            errs.append("edge_started_at_minute ≤ edge_expected_end_minute 정수여야 합니다")
        if pl["from_place"] == pl["to_place"]:
            errs.append("TRANSIT 양 끝이 같습니다")
        return errs
    return [f"알 수 없는 배치 kind {pl.get('kind')!r}"]


def transit_progress(pl: dict, now: int):
    """파생 진행률(Fraction 0~1). 0분 구간은 None — 저장하지 않는다."""
    from fractions import Fraction
    s, e = pl["edge_started_at_minute"], pl["edge_expected_end_minute"]
    if e == s:
        return None
    return min(Fraction(1), max(Fraction(0), Fraction(now - s, e - s)))


def placement_of(world, actor_id: str):
    st = world.domain("spatial_state") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "spatial_state", {})
    return ((st or {}).get("placements") or {}).get(actor_id)


# ══════════════════════════════════════════════════════════════
#  공간 지식 (읽기 모델)
# ══════════════════════════════════════════════════════════════

def actor_knowledge(world, actor_id: str) -> dict:
    sk = world.domain("spatial_knowledge") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "spatial_knowledge", {})
    rec = (sk or {}).get(actor_id) or {}
    return {"places": dict(rec.get("places") or {}), "edges": dict(rec.get("edges") or {}),
            "access": dict(rec.get("access") or {}), "revision": rec.get("revision", 0)}


def knows_place(knowledge: dict, place_id: str) -> bool:
    return place_id in (knowledge.get("places") or {})


def knows_edge(knowledge: dict, edge_id: str) -> bool:
    rec = (knowledge.get("edges") or {}).get(edge_id)
    return isinstance(rec, dict) and rec.get("connection_known") is True


def known_edge_ids(knowledge: dict) -> tuple:
    return tuple(sorted(e for e in (knowledge.get("edges") or {}) if knows_edge(knowledge, e)))


def familiarity_edges(definition: dict, scopes) -> tuple:
    """familiarity scope(PlaceId + 하위) 안의 PUBLIC/LOCAL 구간. RESTRICTED/SECRET 제외."""
    g = SpatialGraph(definition)
    inside = set()
    for s in scopes or ():
        if s in g.places:
            inside.update(g.descendants_of(s))
    return tuple(eid for eid in sorted(g.edges)
                 if g.edges[eid]["from"] in inside and g.edges[eid]["to"] in inside
                 and g.edges[eid].get("knowledge_visibility") in FAMILIARITY_VISIBILITY)


def seed_knowledge(definition: dict, seed_id: str, *, source: str = "SEED") -> dict:
    """컴파일된 SpatialKnowledgeSeed → 행위자 지식 레코드(순수 — 세션 적용은 H06)."""
    seed = (definition.get("spatial_knowledge_seeds") or {}).get(seed_id)
    if seed is None:
        raise KeyError(f"정의되지 않은 seed {seed_id!r}")
    g = SpatialGraph(definition)
    places = set(seed["known_places"])
    for s in seed["familiarity_scopes"]:
        places.update(g.descendants_of(s))
    edges = set(seed["known_edges"]) | set(familiarity_edges(definition, seed["familiarity_scopes"]))
    return {
        "places": {p: {"knows_name": True, "visited": False, "rough_location": None,
                       "acquisitions": [source]} for p in sorted(places)},
        "edges": {e: {"connection_known": True, "distance_estimate_m": None, "distance_precision": None,
                      "last_confirmed_at": None, "acquisition_sources": [source]} for e in sorted(edges)},
        "access": {f["ref"]: {"observed_result": f["observed_result"], "observed_state": f["observed_state"],
                              "action_policy": None, "confirmed_at_minute": None, "source": source}
                   for f in seed["known_access_facts"]},
        "revision": 0,
    }


# ══════════════════════════════════════════════════════════════
#  typed 연산 handler (production 레지스트리 — H03 producer 0)
# ══════════════════════════════════════════════════════════════

OP_SCENE_PLACE_SET = "SCENE_PLACE_SET"
OP_ACTOR_PLACEMENT_SET = "ACTOR_PLACEMENT_SET"
OP_ACTOR_TRANSIT_SET = "ACTOR_TRANSIT_SET"
OP_ACCESS_STATE_SET = "ACCESS_STATE_SET"
OP_PLACE_KNOWLEDGE_UPSERT = "SPATIAL_PLACE_KNOWLEDGE_UPSERT"
OP_EDGE_KNOWLEDGE_UPSERT = "SPATIAL_EDGE_KNOWLEDGE_UPSERT"
OP_ACCESS_KNOWLEDGE_UPSERT = "SPATIAL_ACCESS_KNOWLEDGE_UPSERT"
SPATIAL_OP_TYPES = (OP_SCENE_PLACE_SET, OP_ACTOR_PLACEMENT_SET, OP_ACTOR_TRANSIT_SET,
                    OP_ACCESS_STATE_SET, OP_PLACE_KNOWLEDGE_UPSERT, OP_EDGE_KNOWLEDGE_UPSERT,
                    OP_ACCESS_KNOWLEDGE_UPSERT)


def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def _keys(op, payload, required, optional=()):
    extra = set(payload) - set(required) - set(optional)
    if extra:
        _reject(op, f"알 수 없는 payload 키 {sorted(extra)}")
    missing = [k for k in required if k not in payload]
    if missing:
        _reject(op, f"payload 누락 {missing}")


def _id(op, payload, key):
    v = payload[key]
    if not isinstance(v, str) or not v:
        _reject(op, f"{key}는 비어 있지 않은 문자열이어야 합니다")
    return v


def h_scene_place_set(view, op, payload):
    _keys(op, payload, ("place_id",))
    return [_wm.write(op, "spatial_state", ("scene_place",), _wm.MERGE_SINGLE_VALUE, _id(op, payload, "place_id"))]


def h_actor_placement_set(view, op, payload):
    _keys(op, payload, ("actor_id", "place_id"))
    actor = _id(op, payload, "actor_id")
    return [_wm.write(op, "spatial_state", ("placements", actor), _wm.MERGE_SINGLE_VALUE,
                      at_place(_id(op, payload, "place_id")))]


def h_actor_transit_set(view, op, payload):
    _keys(op, payload, ("actor_id",) + TRANSIT_KEYS[1:])
    actor = _id(op, payload, "actor_id")
    pl = transit(**{k: payload[k] for k in TRANSIT_KEYS[1:]})
    errs = validate_placement(pl)
    if errs:
        _reject(op, "; ".join(errs))
    clock = view.domain("world_clock")
    if not _wc.is_initialized(clock):
        _reject(op, "TRANSIT은 초기화된 world_clock이 필요합니다")
    now = clock["minute_index"]
    if not pl["edge_started_at_minute"] <= now <= pl["edge_expected_end_minute"]:
        _reject(op, f"현재 시각 {now}이 구간 [{pl['edge_started_at_minute']}, "
                    f"{pl['edge_expected_end_minute']}] 밖입니다(시계 진행을 인과 부모로 두십시오)")
    return [_wm.write(op, "spatial_state", ("placements", actor), _wm.MERGE_SINGLE_VALUE, pl)]


def h_access_state_set(view, op, payload):
    _keys(op, payload, ("control_id", "state"))
    cid, state = _id(op, payload, "control_id"), _id(op, payload, "state")
    return [_wm.write(op, "access_control_state", (cid,), _wm.MERGE_SINGLE_VALUE, {"state": state})]


def _upsert(view, op, actor, section, key, fields, allowed, validate):
    extra = set(fields) - set(allowed)
    if extra:
        _reject(op, f"알 수 없는 지식 필드 {sorted(extra)}")
    cur = view.get("spatial_knowledge", (actor, section, key)) or {}
    rec = dict(cur)
    rec.update(fields)
    for k in allowed:
        rec.setdefault(k, None)
    err = validate(rec)
    if err:
        _reject(op, err)
    return [_wm.write(op, "spatial_knowledge", (actor, section, key), _wm.MERGE_SINGLE_VALUE, rec),
            _wm.write(op, "spatial_knowledge", (actor, "revision"), _wm.MERGE_ADDITIVE, 1)]


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
    if d is not None and (not _is_int(d) or d < 0):
        return "distance_estimate_m은 0 이상의 정수여야 합니다"
    if rec["acquisition_sources"] is None:
        rec["acquisition_sources"] = []
    if not isinstance(rec["acquisition_sources"], list):
        return "acquisition_sources는 목록이어야 합니다"
    return None


def _v_access(rec):
    from .access import ACCESS_RESULTS
    if rec["observed_result"] not in ACCESS_RESULTS:
        return f"observed_result는 {list(ACCESS_RESULTS)} 중 하나여야 합니다"
    return None


def h_place_knowledge_upsert(view, op, payload):
    _keys(op, payload, ("actor_id", "place_id", "fields"))
    return _upsert(view, op, _id(op, payload, "actor_id"), "places", _id(op, payload, "place_id"),
                   payload["fields"], PLACE_KNOWLEDGE_KEYS, _v_place)


def h_edge_knowledge_upsert(view, op, payload):
    _keys(op, payload, ("actor_id", "edge_id", "fields"))
    fields = dict(payload["fields"], connection_known=True)
    return _upsert(view, op, _id(op, payload, "actor_id"), "edges", _id(op, payload, "edge_id"),
                   fields, EDGE_KNOWLEDGE_KEYS, _v_edge)


def h_access_knowledge_upsert(view, op, payload):
    _keys(op, payload, ("actor_id", "ref", "fields"))
    return _upsert(view, op, _id(op, payload, "actor_id"), "access", _id(op, payload, "ref"),
                   payload["fields"], ACCESS_FACT_KEYS, _v_access)


_HANDLERS = {
    OP_SCENE_PLACE_SET: h_scene_place_set,
    OP_ACTOR_PLACEMENT_SET: h_actor_placement_set,
    OP_ACTOR_TRANSIT_SET: h_actor_transit_set,
    OP_ACCESS_STATE_SET: h_access_state_set,
    OP_PLACE_KNOWLEDGE_UPSERT: h_place_knowledge_upsert,
    OP_EDGE_KNOWLEDGE_UPSERT: h_edge_knowledge_upsert,
    OP_ACCESS_KNOWLEDGE_UPSERT: h_access_knowledge_upsert,
}


def register(registry: _wm.OperationRegistry) -> None:
    for op_type, h in _HANDLERS.items():
        registry.register(op_type, h)
