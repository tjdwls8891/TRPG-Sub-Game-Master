# -*- coding: utf-8 -*-
"""H03 보정 회귀 — 사용자 보정 요구 1~6.

H03-C1  KnownRoutePlanner 인식 경계: 행위자 지식에 없는 실제 구간 이동 방식 제한·시간 배수·
        고정 지연 때문에 후보가 제거·재정렬되지 않는다. 인지 통과 정보는 EdgeKnowledge에 명시.
H03-C2  CONFIRM 승인은 정책 전체가 아니라 특정 접근 행동(edge·control·state·policy) 단위.
H03-C3  world_schedule 정본 검증 — 항목 형태·타입, falsey 비-dict를 빈 큐로 보지 않음.
H03-C4  world_clock 정본 분 범위 = 달력 계약(일련번호 ≥ 1 → minute_index ≥ 1440).
H03-C5  TRANSIT은 start < end, start ≤ now < end. 0분 이동은 TRANSIT을 만들지 않는다.
H03-C6  지식 provenance(acquisition_sources·acquisitions) append+dedupe, 초기 접근 사실은
        control_id 또는 edge_id 참조.
"""
from __future__ import annotations

import inspect
from fractions import Fraction

import pytest

import core
from core import access as AC
from core import definition_compiler as dc
from core import movement as MV
from core import spatial as SP
from core import world_clock as WC
from core import world_mutation as WM
from core import world_schedule as SC
from core import world_state as WS
from tests.fakes.spatial_fixture import spatial_raw

pytestmark = pytest.mark.policy

T0 = 1440 * 740_000                       # 유효 정본 분(달력 일련번호 ≥ 1)
ROUTE_A = ("e_hm#fwd", "e_mc#fwd")
ROUTE_B = ("e_hh#fwd", "e_hc#fwd")
ROUTE_C = ("e_hs#fwd", "e_sc#fwd")
ALL_ABC = ROUTE_A + ROUTE_B + ROUTE_C
EXACT = {"e_hm#fwd": 400, "e_mc#fwd": 300, "e_hh#fwd": 600, "e_hc#fwd": 300,
         "e_hs#fwd": 500, "e_sc#fwd": 500}


def _compile(raw):
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.path) for d in res.errors]
    return res.definition


def _patched(**edge_patches):
    raw = spatial_raw()
    for e in raw["movement_edges"]:
        e.update(edge_patches.get(e["id"], {}))
    return _compile(raw)


def _know(edges=ALL_ABC, est=EXACT, extra=None):
    out = {"places": {}, "access": {}, "revision": 0, "edges": {}}
    for e in edges:
        rec = {"connection_known": True, "distance_estimate_m": (est or {}).get(e),
               "distance_precision": None, "last_confirmed_at": None, "acquisition_sources": []}
        rec.update((extra or {}).get(e, {}))
        out["edges"][e] = rec
    return out


def _sigs(plan):
    return [c.edge_ids for c in plan.candidates]


def _sess():
    return core.TRPGSession("s-h03c", 1, 2, "x", {})


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


# ══════════════════════════════════════════════════════════════
# C1 — KnownRoutePlanner 인식 경계
# ══════════════════════════════════════════════════════════════

def test_c1_hidden_mode_restriction_does_not_remove_known_candidate():
    # 실제: 산복도로 구간은 boat 전용, 산복도로→성당은 수로(walk 금지 태그). 행위자는 모른다.
    D = _patched(e_hh={"allowed_modes": ["boat"]}, e_hc={"movement_tags": ["water"]})
    plan = MV.plan_known_routes(D, _know(), "항구", "성당", "walk")
    assert ROUTE_B in _sigs(plan)
    # 실제 통과만 실제 구간 진실을 쓴다
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_B, mode_id="walk",
                                 start_minute=T0, access_state={})
    assert not t.completed and t.interruption["reason"] == "EDGE_MODE_NOT_ALLOWED"


def test_c1_hidden_time_factor_and_delay_do_not_reorder():
    # 실제: 시장 구간은 10배 느리고(숨은 잔해), 해안→성당은 2분 지연(숨은 검문)
    D = _patched(e_hm={"base_time_factor": 10})
    plan = MV.plan_known_routes(D, _know(), "항구", "성당", "walk")
    assert _sigs(plan) == [ROUTE_A, ROUTE_B, ROUTE_C]
    a, b, c = plan.candidates
    assert a.perceived_cost_us == 525_000_000                  # 지식 거리 × 지식 배수(모름 = 1)
    assert c.perceived_cost_us == 750_000_000                  # 375초 + 375초 — 실제 1.2 배수·2분 지연 미반영
    # 실제 통과는 숨은 배수를 그대로 쓴다
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk",
                                 start_minute=T0, access_state={"door_1": {"state": "OPEN"}})
    assert t.raw_total_us == (3000 + 225) * MV.US_PER_SECOND


def test_c1_perceived_traversal_info_lives_in_edge_knowledge():
    D = _compile(spatial_raw())
    known = _know(extra={"e_hs#fwd": {"known_time_factor": "1.2"},
                         "e_sc#fwd": {"known_fixed_delay_minutes": 2}})
    c = next(x for x in MV.plan_known_routes(D, known, "항구", "성당", "walk").candidates
             if x.edge_ids == ROUTE_C)
    assert c.perceived_cost_us == 945_000_000
    # 행위자가 아는 이동 방식 제한·구간 태그만 후보를 거른다
    known = _know(extra={"e_hh#fwd": {"known_allowed_modes": ["boat"]},
                         "e_sc#fwd": {"known_movement_tags": ["water"]}})
    assert _sigs(MV.plan_known_routes(D, known, "항구", "성당", "walk")) == [ROUTE_A]
    for f in ("known_allowed_modes", "known_movement_tags", "known_time_factor",
              "known_fixed_delay_minutes"):
        assert f in SP.EDGE_KNOWLEDGE_KEYS


def test_c1_known_graph_never_reads_actual_edge_truth(monkeypatch):
    D = _compile(spatial_raw())
    calls = []
    real = SP.mode_allows_edge
    monkeypatch.setattr(SP, "mode_allows_edge", lambda *a: calls.append(a) or real(*a))
    MV.plan_known_routes(D, _know(), "항구", "성당", "walk")
    assert calls == []


# ══════════════════════════════════════════════════════════════
# C2 — CONFIRM은 접근 행동 단위
# ══════════════════════════════════════════════════════════════

def _two_door_defn():
    raw = spatial_raw()
    raw["access_control_definitions"]["controls"].update({
        "door_2": {"rule_ref": "door_rule", "initial_state": "LOCKED"},
        "door_3": {"rule_ref": "door_rule", "initial_state": "LOCKED"}})
    for e in raw["movement_edges"]:
        if e["id"] == "e_hh":
            e["access_ref"] = "door_2"
        if e["id"] == "e_hc":
            e["access_ref"] = "door_3"
    return _compile(raw)


def test_c2_confirm_is_per_action_not_per_policy():
    D = _two_door_defn()
    assert "confirmed_policies" not in inspect.signature(MV.plan_actual_traversal).parameters
    go = lambda confirmed=(): MV.plan_actual_traversal(
        D, actor_id="pc", edge_sequence=ROUTE_B, mode_id="walk", start_minute=T0, access_state={},
        actor_capabilities=("key_door",), confirmed_actions=confirmed)
    first = go()
    a1 = first.interruption["action_id"]
    assert first.interruption["kind"] == "ACCESS_ACTION_REQUIRED" and first.interruption["edge_id"] == "e_hh#fwd"
    second = go((a1,))
    a2 = second.interruption["action_id"]
    assert not second.completed and second.interruption["edge_id"] == "e_hc#fwd" and a2 != a1
    assert second.traversed_edges == ("e_hh#fwd",)
    assert go((a1, a2)).completed
    # 행동 id는 구간·제어·상태·정책을 묶는다 — 다른 구간의 승인은 통하지 않는다
    assert a1 == MV.access_action_id("e_hh#fwd", "door_2", "LOCKED", AC.CONFIRM)
    assert not go((MV.access_action_id("e_hh#fwd", "door_2", "CLOSED", AC.CONFIRM),)).traversed_edges


def test_c2_separate_action_never_auto_and_implicit_always():
    D = _compile(spatial_raw())
    barred = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk",
                                      start_minute=T0, access_state={"door_1": {"state": "BARRED"}})
    aid = barred.interruption["action_id"]
    again = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk",
                                     start_minute=T0, access_state={"door_1": {"state": "BARRED"}},
                                     confirmed_actions=(aid,))
    assert not again.completed and again.interruption["action_policy"] == AC.SEPARATE_ACTION
    closed = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=ROUTE_A, mode_id="walk",
                                      start_minute=T0, access_state={"door_1": {"state": "CLOSED"}})
    assert closed.completed


# ══════════════════════════════════════════════════════════════
# C3 — world_schedule 정본 검증
# ══════════════════════════════════════════════════════════════

def _entry(sid, due, **kw):
    e = {"schedule_id": sid, "due_minute": due, "kind": SC.KIND_EVENT_WAKEUP, "owner_ref": None,
         "payload_ref": None, "created_by_version": None, "order_key": sid}
    e.update(kw)
    return e


@pytest.mark.parametrize("bad", [[], None, (), 0, "", [{"x": 1}]])
def test_c3_falsey_or_non_mapping_schedule_is_invalid(bad):
    assert SC.validate_schedule(bad)
    clk = WC.initial_clock(T0)
    assert not SC.base_quiescence(clk, bad).quiescent
    assert not SC.base_quiescence({}, bad).quiescent


@pytest.mark.parametrize("patch", [
    {"order_key": 5}, {"order_key": ""}, {"order_key": None}, {"schedule_id": 7},
    {"owner_ref": 3}, {"payload_ref": ["x"]}, {"created_by_version": 9}, {"due_minute": True},
])
def test_c3_entry_field_types_are_validated(patch):
    e = _entry("w1", T0 + 10, **patch)
    assert SC.validate_schedule({"w1": e})
    assert not SC.base_quiescence(WC.initial_clock(T0), {"w1": e}).quiescent


def test_c3_entry_shape_must_be_complete_and_keys_strings():
    e = _entry("w1", T0 + 10)
    del e["owner_ref"]
    assert SC.validate_schedule({"w1": e})
    assert SC.validate_schedule({1: _entry(1, T0 + 10)})
    assert SC.validate_schedule({"w1": _entry("w1", T0 + 10)}) == []
    # 생성 연산도 같은 타입 규칙
    s = _sess()
    s.world_clock = WC.initial_clock(T0)
    for bad in ({"order_key": 5}, {"owner_ref": 1}, {"payload_ref": {"a": 1}}):
        r = _project(s, _op("c", SC.OP_SCHEDULE_CREATE, dict(
            {"schedule_id": "w1", "due_minute": T0 + 5, "kind": SC.KIND_EVENT_WAKEUP}, **bad)))
        assert r.conflicts and r.conflicts[0].code == "HANDLER_REJECTED", bad


# ══════════════════════════════════════════════════════════════
# C4 — world_clock 범위 = 달력 계약
# ══════════════════════════════════════════════════════════════

def test_c4_clock_minute_range_matches_calendar_ordinals():
    assert WC.MIN_MINUTE_INDEX == WC.MINUTES_PER_DAY
    for m in (0, 1, 1439, -5):
        assert WC.validate_clock({"minute_index": m, "start_minute_index": m})
        with pytest.raises(WC.ClockError):
            WC.initial_clock(m)
    assert WC.validate_clock({"minute_index": 2000, "start_minute_index": 1000})   # 시작이 범위 밖
    assert WC.validate_clock(WC.initial_clock(1440)) == []
    cal = WC.Calendar({"calendar_type": "GREGORIAN", "dayparts": {}})
    with pytest.raises(WC.ClockError):
        cal.from_minute_index(1439)
    assert cal.from_minute_index(1440).iso() == "0001-01-01 00:00"
    s = _sess()
    for m in (0, 1439):
        r = _project(s, _op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": m}))
        assert r.conflicts and r.conflicts[0].code == "HANDLER_REJECTED"
    assert _project(s, _op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": 1440})).ok
    assert not SC.base_quiescence({"minute_index": 100, "start_minute_index": 100}, {}).quiescent


# ══════════════════════════════════════════════════════════════
# C5 — TRANSIT 구간 규칙
# ══════════════════════════════════════════════════════════════

def _transit(s, e):
    return SP.transit(movement_id="mv", edge_id="e_hm#fwd", from_place="항구", to_place="시장",
                      edge_started_at_minute=s, edge_expected_end_minute=e)


def test_c5_transit_requires_positive_duration_and_half_open_window():
    assert SP.validate_placement(_transit(T0, T0))                  # 0분 TRANSIT 금지
    assert SP.validate_placement(_transit(T0, T0 + 5)) == []
    s = _sess()
    p = {"actor_id": "pc", "movement_id": "mv", "edge_id": "e_hm#fwd", "from_place": "항구",
         "to_place": "시장", "edge_started_at_minute": T0, "edge_expected_end_minute": T0 + 5}
    s.world_clock = WC.initial_clock(T0 + 5)
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, p)).conflicts          # now == end → 도착
    s.world_clock = WC.initial_clock(T0)
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, p)).ok                 # now == start
    s.world_clock = {"minute_index": T0 + 4, "start_minute_index": T0}
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, p)).ok
    s.world_clock = WC.initial_clock(T0)
    zero = dict(p, edge_expected_end_minute=T0)
    assert _project(s, _op("t", SP.OP_ACTOR_TRANSIT_SET, zero)).conflicts


def test_c5_zero_minute_edges_never_become_transit():
    raw = spatial_raw()
    raw["movement_edges"].append({"id": "door", "from": "시장", "to": "산복도로", "distance_m": 0})
    D = _compile(raw)
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=("e_hm#fwd", "door#fwd"),
                                 mode_id="walk", start_minute=T0, access_state={})
    assert t.completed and t.end_minute == T0 + 5
    for m in range(T0, T0 + 6):
        pl = MV.placement_at(t, m, "mv")
        assert not (pl["kind"] == "TRANSIT" and pl["edge_id"] == "door#fwd")
        if pl["kind"] == "TRANSIT":
            assert SP.validate_placement(pl) == []
    assert MV.placement_at(t, T0 + 5, "mv") == SP.at_place("산복도로")


# ══════════════════════════════════════════════════════════════
# C6 — provenance 보존 · 초기 접근 사실 참조
# ══════════════════════════════════════════════════════════════

def test_c6_upsert_appends_and_dedupes_provenance():
    s = _sess()
    s.spatial_knowledge = {"pc": {
        "places": {"시장": {"knows_name": True, "visited": False, "rough_location": None,
                            "acquisitions": ["SEED"]}},
        "edges": {"e_hm#fwd": {"connection_known": True, "distance_estimate_m": 400,
                               "distance_precision": None, "last_confirmed_at": None,
                               "acquisition_sources": ["SEED", "MAP"]}},
        "access": {}, "revision": 1}}
    r = _project(s,
                 _op("e", SP.OP_EDGE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "edge_id": "e_hm#fwd",
                     "fields": {"acquisition_sources": ["TRAVERSED", "SEED"], "last_confirmed_at": T0}}),
                 _op("p", SP.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": "pc", "place_id": "시장",
                     "fields": {"visited": True, "acquisitions": ["VISITED", "VISITED"]}}))
    assert r.ok, r.conflicts
    k = r.world.domain("spatial_knowledge")["pc"]
    assert k["edges"]["e_hm#fwd"]["acquisition_sources"] == ["SEED", "MAP", "TRAVERSED"]
    assert k["edges"]["e_hm#fwd"]["distance_estimate_m"] == 400          # 언급 안 한 필드 보존
    assert k["places"]["시장"]["acquisitions"] == ["SEED", "VISITED"]
    assert k["places"]["시장"]["knows_name"] is True


def test_c6_seed_access_facts_accept_control_or_edge_refs():
    raw = spatial_raw()
    raw["spatial_knowledge_seeds"]["outsider"]["known_access_facts"] = [
        {"ref": "e_hs#fwd", "observed_result": "IMPASSABLE"},           # 제어 없는 구간 = edge_id 키
        {"ref": "door_1", "observed_result": "ACTION_REQUIRED", "observed_state": "CLOSED"}]
    D = _compile(raw)
    k = SP.seed_knowledge(D, "outsider")
    assert set(k["access"]) == {"e_hs#fwd", "door_1"}
    assert AC.perceived_access(k, D["movement_edges"]["e_hs#fwd"])["perceived"] == AC.PERCEIVED_BLOCKED
    assert AC.perceived_access(k, D["movement_edges"]["e_mc#fwd"])["perceived"] == AC.PERCEIVED_ACTION_REQUIRED
    # 제어가 있는 구간의 edge_id는 런타임 사실 키(control_id)로 정규화
    raw["spatial_knowledge_seeds"]["outsider"]["known_access_facts"] = [
        {"ref": "e_mc#fwd", "observed_result": "IMPASSABLE"}]
    k = SP.seed_knowledge(_compile(raw), "outsider")
    assert set(k["access"]) == {"door_1"}
    raw["spatial_knowledge_seeds"]["outsider"]["known_access_facts"] = [
        {"ref": "nowhere", "observed_result": "IMPASSABLE"}]
    assert "SEED_ACCESS_FACT_INVALID" in [d.code for d in dc.compile_scenario(raw, "t").errors]
    raw["spatial_knowledge_seeds"]["outsider"]["known_access_facts"] = [
        {"ref": "e_mc#fwd", "observed_result": "IMPASSABLE"},
        {"ref": "door_1", "observed_result": "PASSABLE"}]
    assert "SEED_ACCESS_FACT_DUPLICATE" in [d.code for d in dc.compile_scenario(raw, "t").errors]
