# -*- coding: utf-8 -*-
"""H03-B — 방향 공간 그래프 · 이동 방식 · 접근 · 정적 가시성 · 공간 지식 · 경로 · TRANSIT · 컴파일러.

H03-R18  structured_places 구간 거리 검증(추론 없음)
H03-R19  parent/connected 자동 구간 금지
H03-R20  방향 구간 의미
H03-R21  이동 속도는 정의 저작값
H03-R22  배수는 전부 시간 배수
H03-R23  경로 전체 raw 합산 후 HALF_UP 분
H03-R24  고정 정밀도 · 해시 무관 결정성
H03-R25  KnownRoute는 실제 숨은 상태를 쓰지 않는다
H03-R26  NO_KNOWN_ROUTE를 실제 경로로 보완하지 않는다
H03-R27  K-shortest simple path + 다양성 결정성
H03-R28  경로 서명 동률 규칙
H03-R29  PASSABLE / ACTION_REQUIRED / IMPASSABLE
H03-R30  IMPLICIT / CONFIRM / SEPARATE_ACTION
H03-R31  실제 접근 ≠ 인지 접근
H03-R32  숨은 봉쇄 후보 누출 금지
H03-R33  PlaceKnowledge ≠ 경로 지식
H03-R34  실제 세계 변화가 지식을 자동 갱신하지 않음
H03-R35  familiarity가 SECRET 구간을 드러내지 않음
H03-R36  선택 경로 실제 통과 — 숨은 우회 없음
H03-R37  TRANSIT에 progress_ratio 없음
H03-R38  정적 가시성은 저작 관계(거리 미사용)
H03-R39  공간·접근 연산 typed 등록만
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from fractions import Fraction

import pytest

import core
from core import access as AC
from core import definition_compiler as dc
from core import movement as MV
from core import spatial as SP
from core import turn_preparation as TP
from core import turn_transaction as tt
from core import world_clock as WC
from core import world_mutation as WM
from core import world_schedule as SC
from core import world_state as WS
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.fakes.spatial_fixture import spatial_raw
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _disk, _fund

pytestmark = pytest.mark.policy

K = 1440 * 1000          # 정본 분 기준(H03 보정 4: minute_index ≥ 1440)

ROUTE_A = ("e_hm#fwd", "e_mc#fwd")          # 시장 경유(문)
ROUTE_B = ("e_hh#fwd", "e_hc#fwd")          # 산복도로
ROUTE_C = ("e_hs#fwd", "e_sc#fwd")          # 해안


@pytest.fixture
def inject():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _defn(**patch):
    res = dc.compile_scenario(spatial_raw(**patch), "t")
    assert res.ok, [(d.code, d.path) for d in res.errors]
    return res.definition


def _codes(res, sev=None):
    return [d.code for d in res.diagnostics if sev is None or d.severity == sev]


def _know(edges=(), estimates=None, places=(), access=None):
    """행위자 지식(희소). estimates: {edge_id: distance_m} — 없으면 비용 모름."""
    est = estimates or {}
    return {"places": {p: {"knows_name": True, "visited": False, "rough_location": None,
                           "acquisitions": []} for p in places},
            "edges": {e: {"connection_known": True, "distance_estimate_m": est.get(e),
                          "distance_precision": None, "last_confirmed_at": None,
                          "acquisition_sources": []} for e in edges},
            "access": dict(access or {}), "revision": 0}


ALL_ABC = ROUTE_A + ROUTE_B + ROUTE_C
EXACT = {"e_hm#fwd": 400, "e_mc#fwd": 300, "e_hh#fwd": 600, "e_hc#fwd": 300,
         "e_hs#fwd": 500, "e_sc#fwd": 500}


def _sigs(plan):
    return [c.edge_ids for c in plan.candidates]


# ══════════════════════════════════════════════════════════════
# 컴파일러 — structured_places
# ══════════════════════════════════════════════════════════════

def test_structured_places_is_validated_and_edges_normalized():
    res = dc.compile_scenario(spatial_raw(), "t")
    assert res.ok and res.compiler_version >= 2
    assert "structured_places" in dc.VALIDATED_CAPABILITIES
    assert res.definition["scenario_meta"]["validated_capabilities"] == ["structured_places"]
    for k in ("movement_modes", "access_control_definitions", "spatial_knowledge_seeds"):
        assert k not in dc.RESERVED_UNCOMPILED_KEYS
    D = res.definition
    e = D["movement_edges"]
    assert set(e) == {"e_hm#fwd", "e_hm#rev", "e_mc#fwd", "e_hh#fwd", "e_hc#fwd", "e_hs#fwd",
                      "e_sc#fwd", "e_secret#fwd", "e_ferry#fwd"}
    assert e["e_hs#fwd"]["base_time_factor"] == "1.2" and e["e_hs#fwd"]["movement_tags"] == ["road"]
    assert e["e_sc#fwd"]["fixed_delay_minutes"] == 2
    assert e["e_mc#fwd"]["access_ref"] == "door_1" and e["e_mc#fwd"]["knowledge_visibility"] == "LOCAL"
    assert e["e_secret#fwd"]["knowledge_visibility"] == "SECRET"
    assert "e_mc#rev" not in e                                   # 방향 구간 — 역방향 자동 생성 없음
    assert D["movement_modes"]["walk"] == {"mode_id": "walk", "base_speed_m_per_min": "80",
                                           "allowed_edge_tags": None, "forbidden_edge_tags": ["water"]}
    acc = D["access_control_definitions"]
    assert acc["controls"]["door_1"] == {"control_id": "door_1", "rule_ref": "door_rule", "initial_state": "LOCKED"}
    assert acc["rules"]["door_rule"]["states"]["LOCKED"]["actor_overrides"][0]["requires_capability"] == "key_door"
    assert D["spatial_knowledge_seeds"]["outsider"]["known_edges"] == ["e_hm#fwd", "e_hm#rev"]   # pair id 확장
    # connected·parent는 구간이 아니다 — 계층만 있는 장소 '외딴곳'은 구간 0
    g = SP.SpatialGraph(D)
    assert g.out_edges("외딴곳") == () and g.out_edges("섬") == ()


def test_legacy_connected_is_never_promoted_to_edges():
    d = json.load(open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8"))
    # H07: 영도는 구간을 사람이 저작했다 — 저작 구간을 걷어 내면 connected만으로는 구간이 하나도 생기지 않는다
    h07 = ("scenario_schema", "calendar", "movement_modes", "movement_edges", "access_control_definitions",
           "spatial_knowledge_seeds", "information_definitions", "event_templates", "start_frames")
    bare = {k: v for k, v in d.items() if k not in h07}
    bare["default_npcs"] = {n: {k: v for k, v in prof.items() if k not in ("knowledge_seed", "routine")}
                            for n, prof in d["default_npcs"].items()}
    res0 = dc.compile_scenario(bare, "영도")
    assert res0.ok and res0.definition["movement_edges"] == {}
    g0 = SP.SpatialGraph(res0.definition)
    assert all(g0.out_edges(p) == () for p in g0.places)
    res = dc.compile_scenario(d, "영도")
    assert res.ok
    assert all(e["source_ref"][0] == "movement_edges" for e in res.definition["movement_edges"].values())
    g = SP.SpatialGraph(res.definition)
    # 정적 가시성 = 기존 places.is_visible_from과 같은 저작 의미(전 장소 쌍)
    legacy = core.places.load_places(d)
    names = sorted(g.places)
    for t in names:
        for c in names:
            assert g.is_visible_from(t, c) == core.places.is_visible_from(legacy, t, c), (t, c)


def test_static_visibility_is_authored_not_distance_derived():
    D = _defn()
    g = SP.SpatialGraph(D)
    # 성당은 visible_within=[시장] — 구간 거리(산복도로→성당 300m)와 무관하게 시장 안에서만 보인다
    assert g.is_visible_from("성당", "시장")
    assert not g.is_visible_from("성당", "산복도로") and not g.is_visible_from("성당", "항구")
    assert g.is_visible_from("시장", "외딴곳")                  # 게이트 없는 장소는 늘 인지 가능
    # 거리가 0이어도 가시성은 바뀌지 않는다
    raw = spatial_raw()
    raw["movement_edges"].append({"id": "z", "from": "산복도로", "to": "성당", "distance_m": 0})
    D2 = dc.compile_scenario(raw, "t").definition
    assert not SP.SpatialGraph(D2).is_visible_from("성당", "산복도로")


@pytest.mark.parametrize("edge_patch, code", [
    ({"distance_m": None}, "EDGE_DISTANCE_REQUIRED"),
    ({"distance_m": -1}, "EDGE_DISTANCE_INVALID"),
    ({"distance_m": 1.5}, "EDGE_DISTANCE_INVALID"),
    ({"distance_m": True}, "EDGE_DISTANCE_INVALID"),
    ({"base_time_factor": 0}, "EDGE_FACTOR_INVALID"),
    ({"base_time_factor": -1}, "EDGE_FACTOR_INVALID"),
    ({"base_time_factor": "abc"}, "EDGE_FACTOR_INVALID"),
    ({"base_time_factor": 1.0000001}, "EDGE_FACTOR_INVALID"),
    ({"fixed_delay_minutes": -2}, "EDGE_DELAY_INVALID"),
    ({"movement_tags": "road"}, "EDGE_TAGS_INVALID"),
    ({"allowed_modes": ["fly"]}, "EDGE_MODE_UNKNOWN"),
    ({"access_ref": "door_x"}, "EDGE_ACCESS_REF_UNKNOWN"),
    ({"access_rule_ref": "door_rule"}, "EDGE_ACCESS_RULE_WITHOUT_CONTROL"),
    ({"access_rule_ref": "nope", "access_ref": "door_1"}, "EDGE_ACCESS_RULE_UNKNOWN"),
    ({"route_hint": ""}, "EDGE_ROUTE_HINT_INVALID"),
    ({"knowledge_visibility": "HIDDEN"}, "EDGE_KNOWLEDGE_VISIBILITY_INVALID"),
])
def test_edge_property_validation(edge_patch, code):
    edge = {"id": "x", "from": "항구", "to": "외딴곳", "distance_m": 10}
    edge.update(edge_patch)
    edge = {k: v for k, v in edge.items() if v is not None}
    raw = spatial_raw()
    raw["movement_edges"].append(edge)
    res = dc.compile_scenario(raw, "t")
    assert not res.ok and code in _codes(res, "ERROR"), _codes(res)


def test_access_rule_override_must_cover_control_states():
    raw = spatial_raw()
    raw["access_control_definitions"]["rules"]["inside"] = {"rule_type": "STATE_TABLE", "states": {
        "OPEN": {"result": "PASSABLE"}, "LOCKED": {"result": "ACTION_REQUIRED", "action_policy": "IMPLICIT"}}}
    raw["movement_edges"].append({"id": "x", "from": "성당", "to": "시장", "distance_m": 300,
                                  "access_ref": "door_1", "access_rule_ref": "inside"})
    res = dc.compile_scenario(raw, "t")
    assert "ACCESS_RULE_STATE_MISMATCH" in _codes(res, "ERROR")       # CLOSED·BARRED 미정의
    for st in ("CLOSED", "BARRED"):
        raw["access_control_definitions"]["rules"]["inside"]["states"][st] = {"result": "PASSABLE"}
    res = dc.compile_scenario(raw, "t")
    assert res.ok, _codes(res)
    # 방향별 규칙: 같은 문(LOCKED)이 안쪽에서는 IMPLICIT로 열린다
    D = res.definition
    inside = AC.actual_access(D, {}, D["movement_edges"]["x#fwd"])
    outside = AC.actual_access(D, {}, D["movement_edges"]["e_mc#fwd"])
    assert (inside.result, inside.action_policy) == ("ACTION_REQUIRED", "IMPLICIT")
    assert outside.result == "IMPASSABLE"


@pytest.mark.parametrize("patch, code", [
    ({"movement_modes": None}, "MODES_REQUIRED"),
    ({"movement_modes": {"walk": {"base_speed_m_per_min": 0}}}, "MODE_SPEED_INVALID"),
    ({"movement_modes": {"walk": {"base_speed_m_per_min": 80, "allowed_edge_tags": ["a"],
                                  "forbidden_edge_tags": ["a"]}}}, "MODE_TAGS_CONTRADICT"),
    ({"movement_modes": {"걷기": {"base_speed_m_per_min": 80}}}, "MODE_ID_INVALID"),
])
def test_mode_validation(patch, code):
    raw = spatial_raw()
    for k, v in patch.items():
        if v is None:
            raw.pop(k)
        else:
            raw[k] = v
    res = dc.compile_scenario(raw, "t")
    assert not res.ok and code in _codes(res, "ERROR"), _codes(res)


@pytest.mark.parametrize("rule_patch, code", [
    ({"rule_type": "SCRIPT"}, "ACCESS_RULE_TYPE_UNKNOWN"),
    ({"states": {"OPEN": {"result": "ACTION_REQUIRED"}}}, "ACCESS_POLICY_INVALID"),
    ({"states": {"OPEN": {"result": "PASSABLE", "action_policy": "IMPLICIT"}}}, "ACCESS_POLICY_UNEXPECTED"),
    ({"states": {"OPEN": {"result": "MAYBE"}}}, "ACCESS_RESULT_INVALID"),
    ({"states": {}}, "ACCESS_RULE_STATES_REQUIRED"),
])
def test_access_definition_validation(rule_patch, code):
    raw = spatial_raw()
    raw["access_control_definitions"]["rules"]["door_rule"].update(rule_patch)
    res = dc.compile_scenario(raw, "t")
    assert not res.ok and code in _codes(res, "ERROR"), _codes(res)


def test_access_initial_state_and_seed_refs():
    raw = spatial_raw()
    raw["access_control_definitions"]["controls"]["door_1"]["initial_state"] = "AJAR"
    assert "ACCESS_INITIAL_STATE_INVALID" in _codes(dc.compile_scenario(raw, "t"), "ERROR")
    raw = spatial_raw()
    raw["spatial_knowledge_seeds"]["outsider"]["known_edges"] = ["nope"]
    assert "SEED_EDGE_UNKNOWN" in _codes(dc.compile_scenario(raw, "t"), "ERROR")
    raw = spatial_raw()
    raw["spatial_knowledge_seeds"]["outsider"]["familiarity_scopes"] = ["없는곳"]
    assert "SEED_PLACE_UNKNOWN" in _codes(dc.compile_scenario(raw, "t"), "ERROR")


def test_zero_cost_cycle_warning_and_zero_distance_allowed():
    raw = spatial_raw()
    raw["movement_edges"] += [{"id": "z1", "from": "시장", "to": "산복도로", "distance_m": 0},
                              {"id": "z2", "from": "산복도로", "to": "시장", "distance_m": 0}]
    res = dc.compile_scenario(raw, "t")
    assert res.ok and "ZERO_COST_CYCLE" in _codes(res, "WARNING")
    raw["movement_edges"][-1]["fixed_delay_minutes"] = 1             # 지연이 있으면 순환 비용 > 0
    assert "ZERO_COST_CYCLE" not in _codes(dc.compile_scenario(raw, "t"))


def test_compiler_never_invents_distance_without_capability():
    raw = spatial_raw()
    raw.pop("scenario_schema")
    raw["movement_edges"].append({"id": "nd", "from": "항구", "to": "외딴곳"})
    res = dc.compile_scenario(raw, "t")
    assert res.ok and "EDGES_WITHOUT_CAPABILITY" in _codes(res, "WARNING")
    assert res.definition["movement_edges"]["nd#fwd"]["distance_m"] is None
    with pytest.raises(MV.MovementError):
        MV.edge_duration_us(res.definition["movement_edges"]["nd#fwd"],
                            MV.MovementProfile.build(res.definition, "walk"))


# ══════════════════════════════════════════════════════════════
# 소요시간 · 고정 정밀도 · 분 정규화
# ══════════════════════════════════════════════════════════════

def test_duration_formula_factors_are_time_multipliers():
    D = _defn()
    e = D["movement_edges"]
    walk = MV.MovementProfile.build(D, "walk")
    assert MV.edge_duration_us(e["e_hm#fwd"], walk) == 300 * MV.US_PER_SECOND      # 400m / 80m/min = 5분
    assert MV.edge_duration_us(e["e_hs#fwd"], walk) == 450 * MV.US_PER_SECOND      # 500/80 × 1.2
    assert MV.edge_duration_us(e["e_sc#fwd"], walk) == 495 * MV.US_PER_SECOND      # 375초 + 지연 2분(배수 미적용)
    slow = MV.MovementProfile.build(D, "walk", stance_id="stealth", stance_factor="1.5", actor_factor=1.2)
    assert MV.edge_duration_us(e["e_hm#fwd"], slow) == 540 * MV.US_PER_SECOND      # 300 × 1.5 × 1.2
    fast = MV.MovementProfile.build(D, "walk", environment_factor="0.8")
    assert MV.edge_duration_us(e["e_hm#fwd"], fast) == 240 * MV.US_PER_SECOND      # 0.8 = 20% 덜 걸림
    assert MV.as_factor(1.1) == Fraction(11, 10)
    for bad in (0, -1, 0.1 + 0.2, "x", True, None):
        with pytest.raises(MV.MovementError):
            MV.as_factor(bad)
    with pytest.raises(MV.MovementError):
        MV.MovementProfile.build(D, "teleport")


def test_full_route_rounding_half_up_not_per_edge():
    places = {f"p{i}": {} for i in range(7)}
    edges = [{"id": f"s{i}", "from": f"p{i}", "to": f"p{i + 1}", "distance_m": 20} for i in range(6)]
    res = dc.compile_scenario({"scenario_schema": {"version": 1, "capabilities": {"structured_places": 1}},
                               "places": places, "movement_edges": edges,
                               "movement_modes": {"walk": {"base_speed_m_per_min": 60}}}, "t")
    assert res.ok
    D = res.definition
    walk = MV.MovementProfile.build(D, "walk")
    out = MV.route_duration(D, [f"s{i}#fwd" for i in range(6)], walk, start_minute=100)
    assert out["raw_total_us"] == 120 * MV.US_PER_SECOND
    assert out["canonical_total_minutes"] == 2                     # 20초 × 6 → 2분(구간별 올림이면 6분)
    assert out["edge_end_minutes"] == [100, 101, 101, 101, 102, 102]   # Ti = T0 + HALF_UP(누적/60)
    assert MV.us_to_minutes(29_999_999) == 0 and MV.us_to_minutes(30_000_000) == 1
    assert MV.us_to_minutes(89_999_999) == 1 and MV.us_to_minutes(90_000_000) == 2
    assert MV.us_to_minutes(0) == 0
    assert MV.round_half_up(Fraction(5, 2)) == 3 and MV.round_half_up(Fraction(7, 2)) == 4


_HASH_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from core import definition_compiler as dc, movement as MV, spatial as SP
from tests.fakes.spatial_fixture import spatial_raw
r = dc.compile_scenario(spatial_raw(), "t")
D = r.definition
k = SP.seed_knowledge(D, "local")
p = MV.plan_known_routes(D, k, "항구", "성당", "walk")
t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=list(p.candidates[1].edge_ids),
                             mode_id="walk", start_minute=1000, access_state={})
print(json.dumps([r.revision, p.to_dict(), t.to_dict()], ensure_ascii=False, sort_keys=True))
"""


def test_plans_are_hashseed_and_process_independent():
    outs = []
    for seed in ("0", "777"):
        cp = subprocess.run([sys.executable, "-c", _HASH_SCRIPT, REPO_ROOT], capture_output=True,
                            text=True, env=dict(os.environ, PYTHONHASHSEED=seed), timeout=120)
        assert cp.returncode == 0, cp.stderr
        outs.append(json.loads(cp.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1]


# ══════════════════════════════════════════════════════════════
# 접근
# ══════════════════════════════════════════════════════════════

def test_access_three_results_three_policies_and_actor_overrides():
    D = _defn()
    edge = D["movement_edges"]["e_mc#fwd"]
    q = lambda st, caps=(): AC.actual_access(D, {"door_1": {"state": st}} if st else {}, edge, caps)
    assert q("OPEN").result == AC.PASSABLE
    c = q("CLOSED")
    assert (c.result, c.action_policy) == (AC.ACTION_REQUIRED, AC.IMPLICIT)
    assert q(None).result == AC.IMPASSABLE and q(None).state == "LOCKED"     # 상태 없음 = 정의 초기값
    k = q("LOCKED", ("key_door",))
    assert (k.result, k.action_policy, k.action_delay_minutes, k.matched_capability) == \
        (AC.ACTION_REQUIRED, AC.CONFIRM, 1, "key_door")
    b = q("BARRED")
    assert (b.result, b.action_policy) == (AC.ACTION_REQUIRED, AC.SEPARATE_ACTION)
    assert AC.actual_access(D, {}, D["movement_edges"]["e_hm#fwd"]).result == AC.PASSABLE
    assert set(AC.ACCESS_RESULTS) == {"PASSABLE", "ACTION_REQUIRED", "IMPASSABLE"}
    assert set(AC.ACTION_POLICIES) == {"IMPLICIT", "CONFIRM", "SEPARATE_ACTION"}
    reg = AC.AccessRuleRegistry()
    with pytest.raises(AC.AccessError):
        reg.evaluator_for("STATE_TABLE")
    with pytest.raises(AC.AccessError):
        AC.DEFAULT_REGISTRY.register("STATE_TABLE", AC.evaluate_state_table)


def test_perceived_access_is_separate_from_actual():
    D = _defn()
    edge = D["movement_edges"]["e_mc#fwd"]
    assert "access_state" not in inspect.signature(AC.perceived_access).parameters
    assert AC.actual_access(D, {}, edge).result == AC.IMPASSABLE
    assert AC.perceived_access(_know(), edge)["perceived"] == AC.UNKNOWN
    stale = _know(access={"door_1": {"observed_result": "PASSABLE", "observed_state": "OPEN",
                                     "confirmed_at_minute": 10, "source": "OBSERVED"}})
    assert AC.perceived_access(stale, edge)["perceived"] == AC.PERCEIVED_PASSABLE   # 낡은 지식 그대로
    fact = AC.known_access_fact(AC.actual_access(D, {}, edge), confirmed_at_minute=20, source="OBSERVED")
    assert fact["observed_result"] == "IMPASSABLE" and fact["observed_state"] == "LOCKED"


# ══════════════════════════════════════════════════════════════
# KnownRoutePlanner
# ══════════════════════════════════════════════════════════════

def test_known_routes_rank_diverse_candidates_with_labels():
    D = _defn()
    plan = MV.plan_known_routes(D, _know(ALL_ABC, EXACT), "항구", "성당", "walk")
    assert plan.status == MV.ROUTE_OK and _sigs(plan) == [ROUTE_A, ROUTE_B, ROUTE_C]
    a, b, c = plan.candidates
    assert [x.option_id for x in plan.candidates] == ["route_1", "route_2", "route_3"]
    # 인지 비용 = 지식 거리 × 지식 배수(모름 = 1) + 지식 지연(모름 = 0) — 실제 e_hs 1.2배·e_sc 2분 지연은 모른다(H03 보정 1)
    assert (a.perceived_cost_us, b.perceived_cost_us, c.perceived_cost_us) == (525_000_000, 675_000_000, 750_000_000)
    assert (a.perceived_duration_minutes, b.perceived_duration_minutes, c.perceived_duration_minutes) == (9, 11, 13)
    assert a.perceived_distance_m == 700 and a.knowledge_basis == "ESTIMATED"
    assert (b.route_label, c.route_label) == ("산복도로 쪽", "해안도로 쪽")       # route_hint 우선
    assert a.place_ids == ("항구", "시장", "성당") and b.first_divergence_edge == "e_hh#fwd"
    # 비용 상한
    tight = MV.plan_known_routes(D, _know(ALL_ABC, EXACT), "항구", "성당", "walk",
                                 config=MV.RoutePlannerConfig(max_cost_ratio=Fraction(7, 5)))
    assert _sigs(tight) == [ROUTE_A, ROUTE_B]
    assert MV.plan_known_routes(D, _know(), "항구", "항구", "walk").status == MV.SAME_PLACE


def test_diversity_filter_drops_overlapping_route_deterministically():
    raw = spatial_raw()
    raw["places"]["광장"] = {"parent": "섬"}
    raw["movement_edges"] += [{"id": "e_mg", "from": "시장", "to": "광장", "distance_m": 10},
                              {"id": "e_gc", "from": "광장", "to": "성당", "distance_m": 10}]
    D = dc.compile_scenario(raw, "t").definition
    ks = ALL_ABC + ("e_mg#fwd", "e_gc#fwd")
    est = dict(EXACT, **{"e_mg#fwd": 10, "e_gc#fwd": 10})
    A2 = ("e_hm#fwd", "e_mg#fwd", "e_gc#fwd")
    wide = MV.RoutePlannerConfig(max_cost_ratio=Fraction(3))
    # 최단 A2 = 315초. 기본 상한(2배 = 630초)이면 B(675초)부터 잘린다
    assert _sigs(MV.plan_known_routes(D, _know(ks, est), "항구", "성당", "walk")) == [A2, ROUTE_A]
    plan = MV.plan_known_routes(D, _know(ks, est), "항구", "성당", "walk", config=wide)
    assert plan.raw_candidate_count == 4 and _sigs(plan) == [A2, ROUTE_A, ROUTE_B]   # 중첩 300/540 ≤ 0.7
    assert plan.candidates[1].first_divergence_edge == "e_mc#fwd"
    assert plan.candidates[1].route_label == "성당 방향"               # hint·새 경유지 없음 → 방향 fallback
    strict = MV.plan_known_routes(D, _know(ks, est), "항구", "성당", "walk",
                                  config=MV.RoutePlannerConfig(max_cost_ratio=Fraction(3),
                                                               max_overlap=Fraction(1, 2)))
    assert _sigs(strict) == [A2, ROUTE_B, ROUTE_C]
    assert MV.weighted_overlap(A2, ROUTE_A, {"e_hm#fwd": 300, "e_mg#fwd": 7, "e_gc#fwd": 8,
                                             "e_mc#fwd": 225}) == Fraction(300, 540)


def test_unknown_cost_routes_are_valid_and_tie_break_is_signature():
    D = _defn()
    plan = MV.plan_known_routes(D, _know(ALL_ABC), "항구", "성당", "walk")
    assert _sigs(plan) == sorted([ROUTE_A, ROUTE_B, ROUTE_C])            # 같은 중립 비용 → 서명 사전순
    assert all(c.perceived_duration_minutes is None and c.knowledge_basis == "UNKNOWN" for c in plan.candidates)
    partial = MV.plan_known_routes(D, _know(ALL_ABC, {"e_hm#fwd": 400}), "항구", "성당", "walk")
    a = next(c for c in partial.candidates if c.edge_ids == ROUTE_A)
    assert a.knowledge_basis == "PARTIAL" and a.perceived_duration_minutes is None


def test_planner_order_is_insertion_order_independent():
    D1 = _defn()
    raw = spatial_raw()
    raw["movement_edges"] = list(reversed(raw["movement_edges"]))
    raw["places"] = dict(reversed(list(raw["places"].items())))
    D2 = dc.compile_scenario(raw, "t").definition
    k1 = _know(ALL_ABC, EXACT)
    k2 = _know(tuple(reversed(ALL_ABC)), EXACT)
    p1 = MV.plan_known_routes(D1, k1, "항구", "성당", "walk").to_dict()
    p2 = MV.plan_known_routes(D2, k2, "항구", "성당", "walk").to_dict()
    assert p1 == p2


def test_no_known_route_is_not_filled_from_actual_graph():
    D = _defn()
    outsider = SP.seed_knowledge(D, "outsider")                 # 항구↔시장만 안다
    plan = MV.plan_known_routes(D, outsider, "항구", "성당", "walk")
    assert plan.status == MV.NO_KNOWN_ROUTE and plan.candidates == ()
    # 목적지를 '아는' 것(PlaceKnowledge)은 경로 지식이 아니다
    knows_dest = _know(places=("성당", "항구"))
    assert SP.knows_place(knows_dest, "성당")
    assert MV.plan_known_routes(D, knows_dest, "항구", "성당", "walk").status == MV.NO_KNOWN_ROUTE


def test_hidden_actual_block_never_filters_known_candidates(inject):
    D = _defn()

    def _boom(*a, **k):
        raise AssertionError("KnownRoutePlanner가 실제 접근을 조회했습니다")
    inject.setattr(AC, "actual_access", _boom)
    assert "access_state" not in inspect.signature(MV.plan_known_routes).parameters
    # 실제 문은 LOCKED(정의 초기값)지만 행위자는 모른다 → 경로 A가 그대로 1순위
    plan = MV.plan_known_routes(D, _know(ALL_ABC, EXACT), "항구", "성당", "walk")
    assert _sigs(plan)[0] == ROUTE_A and plan.candidates[0].known_constraints == ()
    # 행위자가 '잠김'을 알면 그때만 빠진다
    known_block = _know(ALL_ABC, EXACT, access={"door_1": {"observed_result": "IMPASSABLE"}})
    assert ROUTE_A not in _sigs(MV.plan_known_routes(D, known_block, "항구", "성당", "walk"))
    known_toll = _know(ALL_ABC, EXACT, access={"door_1": {"observed_result": "ACTION_REQUIRED",
                                                          "action_policy": "CONFIRM"}})
    a = MV.plan_known_routes(D, known_toll, "항구", "성당", "walk").candidates[0]
    assert a.edge_ids == ROUTE_A and a.known_constraints == (
        {"edge_id": "e_mc#fwd", "perceived": "PERCEIVED_ACTION_REQUIRED", "action_policy": "CONFIRM"},)


def test_secret_edges_need_explicit_knowledge_and_modes_filter_graph():
    D = _defn()
    local = SP.seed_knowledge(D, "local")
    assert "e_secret#fwd" not in local["edges"]                       # familiarity가 SECRET을 주지 않는다
    assert "e_secret#fwd" not in SP.familiarity_edges(D, ["섬"])
    assert "e_ferry#fwd" in local["edges"] and "외딴곳" not in local["places"]
    assert all(c.edge_ids != ("e_secret#fwd",)
               for c in MV.plan_known_routes(D, local, "항구", "성당", "walk").candidates)
    smuggler = _know(ALL_ABC + ("e_secret#fwd",), dict(EXACT, **{"e_secret#fwd": 100}))
    assert _sigs(MV.plan_known_routes(D, smuggler, "항구", "성당", "walk"))[0] == ("e_secret#fwd",)
    # 이동 방식: 행위자가 아는 구간 태그로만 거른다(수로는 boat만, 도로는 walk만)
    k = _know(("e_hs#fwd", "e_ferry#fwd"), {"e_hs#fwd": 500, "e_ferry#fwd": 1000})
    k["edges"]["e_hs#fwd"]["known_movement_tags"] = ["road"]
    k["edges"]["e_ferry#fwd"]["known_movement_tags"] = ["water"]
    assert _sigs(MV.plan_known_routes(D, k, "항구", "해안", "walk")) == [("e_hs#fwd",)]
    assert _sigs(MV.plan_known_routes(D, k, "항구", "해안", "boat")) == [("e_ferry#fwd",)]
    assert SP.mode_allows_edge(D["movement_modes"]["boat"], D["movement_edges"]["e_hm#fwd"]) == \
        (False, "MODE_TAG_NOT_ALLOWED")


# ══════════════════════════════════════════════════════════════
# ActualTraversalPlanner · TRANSIT
# ══════════════════════════════════════════════════════════════

def _go(D, seq=ROUTE_A, state=None, **kw):
    return MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=seq, mode_id=kw.pop("mode", "walk"),
                                    start_minute=1000, access_state=state or {}, **kw)


def test_traversal_follows_selected_route_and_stops_at_hidden_block():
    D = _defn()
    t = _go(D)                                                      # 문 LOCKED(숨은 상태)
    assert not t.completed and t.end_place == "시장" and t.traversed_edges == ("e_hm#fwd",)
    assert t.interruption["kind"] == "BLOCKED" and t.interruption["edge_id"] == "e_mc#fwd"
    assert t.end_minute == 1005 and t.expected_arrival_minute is None
    # 우회 없음: 다른 길(B)이 열려 있어도 선택하지 않는다
    assert not set(t.traversed_edges) & set(ROUTE_B + ROUTE_C)
    with pytest.raises(MV.MovementError):
        _go(D, ("e_hm#fwd", "e_hc#fwd"))                            # 끊긴 경로
    with pytest.raises(MV.MovementError):
        _go(D, ())


def test_traversal_eta_and_checkpoints():
    D = _defn()
    t = _go(D, state={"door_1": {"state": "OPEN"}})
    assert t.completed and t.raw_total_us == 525_000_000 and t.canonical_total_minutes == 9
    assert t.expected_arrival_minute == 1009 and t.end_place == "성당"
    assert [(c["kind"], c["edge_id"], c["minute"]) for c in t.checkpoint_times] == [
        ("EDGE_START", "e_hm#fwd", 1000), ("EDGE_END", "e_hm#fwd", 1005),
        ("EDGE_START", "e_mc#fwd", 1005), ("EDGE_END", "e_mc#fwd", 1009),
        ("DESTINATION", None, 1009)]
    assert set(c["kind"] for c in t.checkpoint_times) <= set(MV.CHECKPOINT_KINDS)


def test_traversal_access_policies():
    D = _defn()
    closed = _go(D, state={"door_1": {"state": "CLOSED"}})          # IMPLICIT → 자동 수행
    assert closed.completed
    assert [c["kind"] for c in closed.checkpoint_times][2] == "ACCESS_ACTION"
    keyed = _go(D, actor_capabilities=("key_door",))                # CONFIRM → 확인 없으면 중단
    aid = MV.access_action_id("e_mc#fwd", "door_1", "LOCKED", "CONFIRM", "door_rule:LOCKED:override:0")
    assert not keyed.completed and keyed.interruption == {
        "kind": "ACCESS_ACTION_REQUIRED", "edge_id": "e_mc#fwd", "action_policy": "CONFIRM",
        "action_id": aid, "minute": 1005}
    ok = _go(D, actor_capabilities=("key_door",), confirmed_actions=(aid,))   # 그 행동만 확인
    assert ok.completed and ok.raw_total_us == 585_000_000 and ok.expected_arrival_minute == 1010   # +1분 행동
    acc = [c for c in ok.checkpoint_times if c["kind"] == "ACCESS_ACTION"]
    assert acc == [{"kind": "ACCESS_ACTION", "edge_id": "e_mc#fwd", "place_id": "시장", "minute": 1005,
                    "raw_offset_us": 300_000_000, "action_policy": "CONFIRM", "action_id": aid}]
    barred = _go(D, state={"door_1": {"state": "BARRED"}},
                 confirmed_actions=(MV.access_action_id("e_mc#fwd", "door_1", "BARRED", "SEPARATE_ACTION", "door_rule:BARRED:base"),))
    # SEPARATE_ACTION은 확인 목록에 있어도 언제나 중단
    assert barred.interruption["action_policy"] == "SEPARATE_ACTION" and not barred.completed
    boat = _go(D, mode="boat")
    assert boat.interruption["reason"] == "MODE_TAG_NOT_ALLOWED" and boat.traversed_edges == ()


def test_transit_shape_has_no_progress_ratio_and_is_derived():
    D = _defn()
    t = _go(D, state={"door_1": {"state": "OPEN"}})
    pl = MV.placement_at(t, 1002, "mv1")
    assert tuple(pl) == SP.TRANSIT_KEYS and "progress_ratio" not in pl
    assert pl == {"kind": "TRANSIT", "movement_id": "mv1", "edge_id": "e_hm#fwd", "from_place": "항구",
                  "to_place": "시장", "edge_started_at_minute": 1000, "edge_expected_end_minute": 1005}
    assert SP.validate_placement(pl) == []
    assert SP.transit_progress(pl, 1002) == Fraction(2, 5)
    assert MV.placement_at(t, 1005, "mv1")["edge_id"] == "e_mc#fwd"
    assert MV.placement_at(t, 1009, "mv1") == SP.at_place("성당")
    assert MV.placement_at(t, 1000, "mv1")["kind"] == "TRANSIT"
    with pytest.raises(MV.MovementError):
        MV.placement_at(t, 999, "mv1")
    assert SP.validate_placement(dict(pl, progress_ratio=0.4))
    assert SP.transit_progress(dict(pl, edge_expected_end_minute=1000), 1000) is None


# ══════════════════════════════════════════════════════════════
# typed 연산 · 지식
# ══════════════════════════════════════════════════════════════

def _sess(sid="s-h03b"):
    return core.TRPGSession(sid, 1, 2, "x", {})


def _project(sess, *ops):
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT,
                              source_world_version_id=WS.version_id_of(sess.world_version),
                              definition_revision=sess.definition_revision)
    for op in ops:
        b.add(op)
    b.freeze()
    return WM.project(sess, b, WM.PRODUCTION_REGISTRY)


def _op(i, t, p, **kw):
    return WM.make_op(i, t, "TEST", p, **kw)


def test_production_registry_is_typed_time_and_spatial_only():
    names = set(WM.PRODUCTION_REGISTRY.op_types())
    from tests.characterize.h04_scope import expected_production_ops
    assert set(SC.TIME_OP_TYPES) | set(SP.SPATIAL_OP_TYPES) <= names
    assert names == expected_production_ops()                      # H04 도메인 typed 연산 포함
    assert set(SP.SPATIAL_OP_TYPES) == {
        "SCENE_PLACE_SET", "ACTOR_PLACEMENT_SET", "ACTOR_TRANSIT_SET", "ACCESS_STATE_SET",
        "SPATIAL_PLACE_KNOWLEDGE_UPSERT", "SPATIAL_EDGE_KNOWLEDGE_UPSERT", "SPATIAL_ACCESS_KNOWLEDGE_UPSERT",
        "ACTOR_PLACEMENT_CLEAR"}                                   # H07: 정본 override 해제(typed — 임의 경로 아님)


def test_spatial_ops_project_and_knowledge_is_sparse():
    s = _sess()
    r = _project(s, _op("a", SP.OP_SCENE_PLACE_SET, {"place_id": "항구"}),
                 _op("b", SP.OP_ACTOR_PLACEMENT_SET, {"actor_id": "pc", "place_id": "항구"}),
                 _op("c", SP.OP_ACCESS_STATE_SET, {"control_id": "door_1", "state": "OPEN"}),
                 _op("d", SP.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "place_id": "성당",
                                                          "fields": {"knows_name": True}}),
                 _op("e", SP.OP_EDGE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "edge_id": "e_hm#fwd",
                                                         "fields": {"distance_estimate_m": 400}}),
                 _op("f", SP.OP_ACCESS_KNOWLEDGE_UPSERT, {"actor_id": "pc", "ref": "door_1",
                                                           "fields": {"observed_result": "PASSABLE"}}))
    assert r.ok, r.conflicts
    assert r.world.domain("spatial_state") == {"scene_place": "항구", "placements": {"pc": SP.at_place("항구")}}
    assert r.world.domain("access_control_state") == {"door_1": {"state": "OPEN"}}
    k = r.world.domain("spatial_knowledge")["pc"]
    assert k["revision"] == 3                                           # 같은 계층 가산
    assert k["places"]["성당"] == {"knows_name": True, "visited": False, "rough_location": None, "acquisitions": []}
    assert k["edges"]["e_hm#fwd"]["connection_known"] is True
    assert set(k) == {"places", "edges", "access", "revision"}
    assert s.spatial_state == {} and s.spatial_knowledge == {}          # 투영은 세션 불변
    # 같은 칸 서로 다른 값 → 결정적 충돌(마지막 쓰기 채택 없음)
    r = _project(s, _op("x", SP.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "place_id": "성당",
                                                            "fields": {"visited": True}}),
                 _op("y", SP.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "place_id": "성당",
                                                         "fields": {"knows_name": True}}))
    assert r.conflicts[0].code == "MERGE_CONFLICT"
    # 형식 위반
    for bad in (_op("q", SP.OP_EDGE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "edge_id": "e", "fields": {"zz": 1}}),
                _op("q", SP.OP_ACCESS_KNOWLEDGE_UPSERT, {"actor_id": "pc", "ref": "d", "fields": {"observed_result": "?"}}),
                _op("q", SP.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "place_id": "p", "fields": {"visited": "y"}}),
                _op("q", SP.OP_ACTOR_PLACEMENT_SET, {"actor_id": "", "place_id": "p"})):
        assert _project(s, bad).conflicts[0].code == "HANDLER_REJECTED"


def test_actual_access_change_does_not_update_knowledge():
    D = _defn()
    s = _sess()
    s.spatial_knowledge = {"pc": _know(ALL_ABC, EXACT, access={
        "door_1": {"observed_result": "PASSABLE", "observed_state": "OPEN"}})}
    before = s.spatial_knowledge
    r = _project(s, _op("lock", SP.OP_ACCESS_STATE_SET, {"control_id": "door_1", "state": "LOCKED"}))
    assert r.ok and r.changed_domains(WM.ProjectedWorld.capture(s)) == ("access_control_state",)
    assert r.world.domain("spatial_knowledge") == before
    k = SP.actor_knowledge(r.world, "pc")
    plan = MV.plan_known_routes(D, k, "항구", "성당", "walk")
    assert plan.candidates[0].edge_ids == ROUTE_A                      # 여전히 열려 있다고 믿는다
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk", start_minute=0,
                                 access_state=r.world.domain("access_control_state"))
    assert not t.completed                                             # 실제로는 막힘


def test_transit_op_requires_clock_window_and_rejects_progress_ratio():
    s = _sess()
    p = {"actor_id": "pc", "movement_id": "mv1", "edge_id": "e_hm#fwd", "from_place": "항구",
         "to_place": "시장", "edge_started_at_minute": K + 1000, "edge_expected_end_minute": K + 1005}
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, p)).conflicts            # 시계 미초기화
    s.world_clock = WC.initial_clock(K + 1002)
    r = _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, p))
    assert r.ok and r.world.domain("spatial_state")["placements"]["pc"]["kind"] == "TRANSIT"
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, dict(p, progress_ratio=0.4))).conflicts
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, dict(p, edge_started_at_minute=K + 1003))).conflicts
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, dict(p, edge_expected_end_minute=K + 999))).conflicts
    # 시계 진행을 인과 부모로 두면 자식이 진행된 시각을 본다
    s.world_clock = WC.initial_clock(K + 990)
    r = _project(s, _op("c", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": K + 1003}),
                 _op("t", SP.OP_ACTOR_TRANSIT_SET, p, causal_parent_op_ids=("c",)))
    assert r.ok, r.conflicts


async def test_traversal_ops_commit_and_rewind_in_player_turn(rig, inject):
    D = _defn()
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(K + 1000)
    s.access_control_state = {"door_1": {"state": "OPEN"}}
    await _run_turn(r)                                                   # 기준 턴(빈 묶음)
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk",
                                 start_minute=s.world_clock["minute_index"],
                                 access_state=s.access_control_state)
    ops = MV.traversal_ops(t, op_prefix="mv1")
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        for op in ops:
            prep.world_mutation_batch.add(op)
    inject.setattr(TP, "capture_world_baseline", _hook)
    tx = await _run_turn(r)
    inject.undo()
    assert tx.status == tt.TurnStatus.COMMITTED
    assert s.world_clock["minute_index"] == K + 1009
    assert s.spatial_state["placements"]["pc"] == SP.at_place("성당")
    k = SP.actor_knowledge(s, "pc")
    assert SP.known_edge_ids(k) == ROUTE_A
    assert k["edges"]["e_mc#fwd"]["last_confirmed_at"] == K + 1009
    assert k["edges"]["e_mc#fwd"]["acquisition_sources"] == ["TRAVERSED"]
    assert k["places"]["시장"]["visited"] and k["places"]["성당"]["visited"]
    assert k["places"]["성당"]["knows_name"] is False                   # visited ⇒ knows_name 강제 없음
    assert k["access"]["door_1"]["observed_result"] == "PASSABLE"
    assert _disk(s)["spatial_knowledge"]["pc"]["revision"] == k["revision"]
    assert tx.preparation.ready_proof.base_quiescence["quiescent"] is True
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    assert s.world_clock["minute_index"] == K + 1000 and s.spatial_state == {} and s.spatial_knowledge == {}


async def test_blocked_traversal_learns_block_only_by_observation(rig, inject):
    D = _defn()
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(K + 1000)
    s.spatial_knowledge = {"pc": _know(ALL_ABC, EXACT)}
    plan0 = MV.plan_known_routes(D, SP.actor_knowledge(s, "pc"), "항구", "성당", "walk")
    assert _sigs(plan0)[0] == ROUTE_A                                     # 숨은 봉쇄 비노출
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=plan0.candidates[0].edge_ids,
                                 mode_id="walk", start_minute=K + 1000, access_state=s.access_control_state)
    assert not t.completed
    ops = MV.traversal_ops(t, op_prefix="mv2")
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        for op in ops:
            prep.world_mutation_batch.add(op)
    inject.setattr(TP, "capture_world_baseline", _hook)
    tx = await _run_turn(r)
    inject.undo()
    assert tx.status == tt.TurnStatus.COMMITTED
    assert s.spatial_state["placements"]["pc"] == SP.at_place("시장") and s.world_clock["minute_index"] == K + 1005
    k = SP.actor_knowledge(s, "pc")
    assert k["access"]["door_1"]["observed_result"] == "IMPASSABLE"     # 직접 관찰 → 지식
    plan1 = MV.plan_known_routes(D, k, "시장", "성당", "walk")
    assert all("e_mc#fwd" not in c.edge_ids for c in plan1.candidates)
