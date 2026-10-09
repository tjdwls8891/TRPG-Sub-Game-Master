# -*- coding: utf-8 -*-
"""H04 2차 독립 검수 보정(R2-01~R2-05) — 최소 반례.

각 테스트는 보정 전 코드(bc559c4)에서 실패하고 보정 후 통과하는 단언을 갖는다.
  R2-01 PC 믿음 권위 — COGNITION_UPSERT 인지 하향이 PC 믿음을 암묵 변경하지 않는다
  R2-02 case_required — 정적 위반은 컴파일 ERROR, 런타임 유효 케이스 0은 안전 중단
  R2-03 같은 케이스 step의 순차 가시성 — 뒤 step이 앞 step의 세계 상태를 계획 시점에 관찰
  R2-04 명시 이동 route 연속성 — 컴파일 거부 + 런타임 MovementError 비누출·부분 변이 없음
  R2-05 같은 분 중재 — 낮은 우선순위 제거 후 재투영, 동일 우선순위·동일 효과는 호환, 연결 성분 병합
"""
from __future__ import annotations

import copy

import pytest

from core import cognition as CG
from core import definition_compiler as dc
from core import events as EV
from core import spatial as SP
from core import world_mutation as WM
from core import world_progression as WP
from tests.policy.test_h04a_actor_info import KANG, PC, _acq, _ok, _op, _project, _sess
from tests.policy.test_h04b_events_progression import (
    K, SEED, DOOR, ROUTE_B, _deferred, _fx_world, _go_tpl, _know, _set, defn, eq, ev_raw, one_inst,
    run, world)

pytestmark = pytest.mark.policy

# 공유 픽스처 SIMPLE은 다른 모듈 테스트가 중첩 phases를 고쳐 쓸 수 있어 여기서는 독립 사본을 쓴다
BASE = {"version": 1, "spawn_when": eq(DOOR, "BARRED"),
        "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}


def _err_codes(raw):
    return [d.code for d in dc.compile_scenario(raw, "t").diagnostics if d.severity == "ERROR"]


# ══════════════════════════════════════════════════════════════
# R2-01 PC 믿음 권위 우회
# ══════════════════════════════════════════════════════════════

def _believing(holder, auth):
    w = _ok(_sess(), _op("a", CG.OP_COGNITION_ACQUIRE, {"holder": holder, "info_id": "S40", "awareness": "CONTENT",
                                                        "acquisition": _acq("x1")}))
    return _ok(w, _op("b", CG.OP_COGNITION_BELIEF_SET, {"holder": holder, "info_id": "S40", "belief": "CERTAIN",
                                                        "authority": auth, "cause_ref": "turn:1"}))


def _down(holder, **extra):
    return _op("d", CG.OP_COGNITION_UPSERT, dict({"holder": holder, "info_id": "S40",
                                                  "fields": {"awareness": "EXISTENCE"}, "cause_ref": "rule:x"},
                                                 **extra))


def test_r2_01_pc_awareness_downgrade_cannot_clear_belief_without_pc_authority():
    pc = f"actor:{PC}"
    w = _believing(pc, "PLAYER_EXPLICIT")
    # 비허용 권위(없음 · RULE · AUTHORED)로는 하향이 PC 믿음을 지울 수 없다 — 거부, 기존 믿음 보존
    for extra in ({}, {"authority": "RULE"}, {"authority": "AUTHORED"}):
        r = _project(w, _down(pc, **extra))
        assert not r.ok and r.conflicts and "PC 믿음" in r.conflicts[0].message, extra
    assert CG.record_of(w, pc, "S40")["belief"] == "CERTAIN"
    assert CG.record_of(w, pc, "S40")["awareness"] == "CONTENT"
    # 알 수 없는 권위 문자열도 거부
    assert not _project(w, _down(pc, authority="WHATEVER")).ok


@pytest.mark.parametrize("auth", ["PLAYER_EXPLICIT", "RULE_FORCED"])
def test_r2_01_pc_belief_cleared_only_through_pc_authority_with_provenance(auth):
    pc = f"actor:{PC}"
    w = _ok(_believing(pc, "PLAYER_EXPLICIT"), _down(pc, authority=auth))
    rec = CG.record_of(w, pc, "S40")
    assert rec["awareness"] == "EXISTENCE" and rec["belief"] is None
    assert rec["belief_provenance"][-1] == {"belief": None, "authority": auth, "cause_ref": "rule:x"}
    # 기존 경로: BELIEF_SET(None, PC 권위)로 먼저 지우면 cause_ref만으로 하향 가능
    w2 = _ok(_believing(pc, "PLAYER_EXPLICIT"),
             _op("n", CG.OP_COGNITION_BELIEF_SET, {"holder": pc, "info_id": "S40", "belief": None,
                                                   "authority": auth, "cause_ref": "turn:2"}))
    w2 = _ok(w2, _down(pc))
    assert CG.record_of(w2, pc, "S40")["awareness"] == "EXISTENCE"


def test_r2_01_pc_without_belief_and_npc_downgrade_unchanged():
    pc, npc = f"actor:{PC}", f"actor:{KANG}"
    # PC: 믿음이 없으면 믿음 변경이 아니다 — 기존대로 cause_ref만 요구
    w = _ok(_sess(), _op("a", CG.OP_COGNITION_ACQUIRE, {"holder": pc, "info_id": "S40", "awareness": "CONTENT",
                                                        "acquisition": _acq("x1")}))
    assert _project(w, _down(pc)).ok
    assert not _project(w, _op("d", CG.OP_COGNITION_UPSERT, {"holder": pc, "info_id": "S40",
                                                             "fields": {"awareness": "EXISTENCE"}})).ok
    # NPC: 기존 동작(cause_ref로 하향 · 믿음 대상 소멸) 회귀 없음 — 권위 지정 불요
    w = _believing(npc, "RULE")
    rec0 = CG.record_of(w, npc, "S40")
    w = _ok(w, _down(npc))
    rec = CG.record_of(w, npc, "S40")
    assert rec["awareness"] == "EXISTENCE" and rec["belief"] is None
    assert rec["belief_provenance"] == rec0["belief_provenance"]      # NPC 기록 형태 불변


# ══════════════════════════════════════════════════════════════
# R2-02 case_required
# ══════════════════════════════════════════════════════════════

def _req_tpl(cases, **kw):
    return dict(copy.deepcopy(BASE), case_required=True, transition_cases=cases, **kw)


def test_r2_02_case_required_without_cases_is_compile_error():
    assert "EVENT_CASE_REQUIRED_UNSATISFIABLE" in _err_codes(ev_raw({"t": _req_tpl([])}))
    assert "EVENT_CASE_REQUIRED_UNSATISFIABLE" in _err_codes(ev_raw({"t": dict(copy.deepcopy(BASE), case_required=True)}))
    # 구조상 진입 가능한 전이 중 케이스 후보가 없는 것이 있으면 충족 불가 — 정적 거부
    two = {"version": 1, "spawn_when": eq(DOOR, "BARRED"), "case_required": True,
           "phases": {"a": {"eligible_when": {"const": True}, "priority": 1},
                      "b": {"eligible_when": eq(DOOR, "OPEN"), "priority": 2}},
           "transition_cases": [{"case_id": "c", "from": "*", "to": "a", "steps": []}]}
    assert "EVENT_CASE_REQUIRED_UNSATISFIABLE" in _err_codes(ev_raw({"t": two}))
    two["transition_cases"].append({"case_id": "d", "from": "*", "to": "b", "steps": []})
    assert _err_codes(ev_raw({"t": two})) == []


def test_r2_02_case_required_runtime_no_valid_case_is_safe_abort():
    steps = [{"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}]
    D = defn({"t": _req_tpl([{"case_id": "c", "from": None, "to": "open",
                              "valid_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
                              "steps": steps}])})
    w = world()
    before = copy.deepcopy(w.__dict__.get("event_instances")), copy.deepcopy(w.__dict__.get("access_control_state"))
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.CONFLICT and not r.committable
    assert r.ops == () and r.projected_world is None                    # 부분 변이 없음
    assert any(d.get("reason") == "CASE_REQUIRED_NONE_VALID" for d in r.diagnostics), r.diagnostics
    assert (w.__dict__.get("event_instances"), w.__dict__.get("access_control_state")) == before


def test_r2_02_optional_no_case_and_required_valid_case_unchanged():
    # case_required=false, 케이스 없음 → 전이 성립(기존 선택적 동작)
    D = defn({"t": dict(copy.deepcopy(BASE))})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED and one_inst(r)["current_phase"] == "open"
    # case_required=false, 유효 케이스 0 → 진단만, 전이 유지
    D = defn({"t": dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open",
                                                    "valid_when": {"const": False}, "steps": []}])})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED and any(d["code"] == "NO_VALID_CASE" for d in r.diagnostics)
    # case_required=true, 유효 케이스 → 결정적 선택 그대로
    cases = [{"case_id": f"c{n}", "from": None, "to": "open", "weight": n + 1,
              "steps": [{"step_id": "s", "type": "SET_EVENT_LOCAL", "key": "picked", "value": f"c{n}"}]}
             for n in range(3)]
    D = defn({"t": _req_tpl(cases)})
    picks = {one_inst(run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")]))["history"][0]["case_id"]
             for _ in range(3)}
    assert len(picks) == 1
    inst = one_inst(run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")]))
    assert inst["local_state"]["picked"] == inst["history"][0]["case_id"]


# ══════════════════════════════════════════════════════════════
# R2-03 같은 케이스 step의 순차 가시성
# ══════════════════════════════════════════════════════════════

def test_r2_03_signal_step_observes_event_local_set_by_previous_step():
    steps = [{"step_id": "flag", "type": "SET_EVENT_LOCAL", "key": "flag", "value": True},
             {"step_id": "sig", "type": "EMIT_SIGNAL", "signal_id": "sg", "cue": "깃발이 올랐다",
              "interrupt_policy": "IF_OBSERVABLE",
              "observable_when": eq({"ref": "EVENT_LOCAL", "instance": "$self", "key": "flag"}, True)}]
    D = defn({"t": dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])})
    r = run(D, world(), K + 30, base_ops=[_set("b", "door_1", "BARRED")])
    (sig,) = r.signals
    assert sig["observability"] == "TRUE" and sig["observable"] is True
    assert r.status == WP.FOREGROUND_INTERRUPT and r.reached_minute == K


def test_r2_03_movement_step_plans_from_placement_set_by_previous_step():
    steps = [{"step_id": "put", "type": "SET_ACTOR_PLACEMENT", "actor": KANG, "place": "항구"},
             {"step_id": "go", "type": "START_MOVEMENT", "actor": KANG, "destination": "성당", "mode": "walk"}]
    D = defn({"t": dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])})
    w = world(spatial_state={"placements": {KANG: SP.at_place("시장")}},
              spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})   # 항구발 경로만 안다
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED, r.diagnostics
    pl = r.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["from_place"] == "항구" and pl["edge_id"] == "e_hh#fwd"
    assert not any(d["code"] == "MOVEMENT_NO_KNOWN_ROUTE" for d in r.diagnostics)
    assert w.spatial_state["placements"][KANG] == SP.at_place("시장")      # 정본 세션 불변


def test_r2_03_interim_projection_keeps_final_op_identity_and_parents():
    steps = [{"step_id": "flag", "type": "SET_EVENT_LOCAL", "key": "flag", "value": 1},
             {"step_id": "put", "type": "SET_ACTOR_PLACEMENT", "actor": KANG, "place": "항구"},
             {"step_id": "go", "type": "START_MOVEMENT", "actor": KANG, "destination": "성당", "mode": "walk"}]
    D = defn({"t": dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])})
    w = world(spatial_state={"placements": {KANG: SP.at_place("시장")}},
              spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED
    ops = {o.op_id: o for o in r.ops}
    case_ops = [o for o in r.ops if ".case." in o.op_id]
    (create,) = [o for o in r.ops if o.op_type == EV.OP_EVENT_INSTANCE_CREATE]
    flag = [o for o in case_ops if ".case.flag." in o.op_id]
    put = [o for o in case_ops if ".case.put." in o.op_id]
    go = [o for o in case_ops if ".case.go." in o.op_id]
    assert flag and put and go
    assert create.op_id in flag[0].causal_parent_op_ids                 # 첫 step ← 전이(생성)
    assert flag[0].op_id in put[0].causal_parent_op_ids                 # 인과 사슬 유지
    assert all(put[0].op_id in o.causal_parent_op_ids for o in go)
    assert len(set(ops)) == len(r.ops)                                  # 임시 사본이 결과에 섞이지 않음
    # 합본을 원본에 한 번 투영 = 결과 세계(엔진 _verify와 같은 조건을 테스트에서도 확인)
    b = WM.WorldMutationBatch(batch_id="v", outer_origin="PLAYER_TURN", source_world_version_id=None,
                              definition_revision=None)
    for o in r.ops:
        b.add(o)
    b.freeze()
    res = WM.project(WM.ProjectedWorld.capture(w), b, WM.PRODUCTION_REGISTRY)
    assert res.ok and res.world.digest() == r.projected_world.digest()


def test_r2_03_information_chain_still_sequential():
    steps = [{"step_id": "mk", "type": "CREATE_INFORMATION", "proposition": "문이 강제로 잠겼다"},
             {"step_id": "tell", "type": "ACQUIRE_INFORMATION", "holder": f"actor:{KANG}", "info": "$step:mk"},
             {"step_id": "trust", "type": "SET_COGNITION_BELIEF", "holder": f"actor:{KANG}", "info": "$step:mk",
              "belief": "BELIEVED"}]
    D = defn({"t": dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED, r.diagnostics
    recs = r.projected_world.domain("cognition_state")[f"actor:{KANG}"]
    (rec,) = recs.values()
    assert rec["awareness"] == "CONTENT" and rec["belief"] == "BELIEVED"


# ══════════════════════════════════════════════════════════════
# R2-04 명시 route 연속성 · MovementError 비누출
# ══════════════════════════════════════════════════════════════

BROKEN = ["e_hm#fwd", "e_hh#fwd", "e_hc#fwd"]          # 항구→시장 / 항구→산복도로 / 산복도로→성당 (중간 단절)


def test_r2_04_discontinuous_explicit_route_is_compile_error():
    assert "STEP_ROUTE_DISCONTINUOUS" in _err_codes(ev_raw({"go": _go_tpl(route=BROKEN)}))
    # 고정 목적지와 끝이 맞지 않는 route도 정적 거부
    assert "STEP_ROUTE_DISCONTINUOUS" in _err_codes(ev_raw({"go": _go_tpl(route=["e_hm#fwd"])}))
    assert _err_codes(ev_raw({"go": _go_tpl(route=list(ROUTE_B))})) == []


def _broken_runtime_world():
    D = defn({"go": _go_tpl(route=list(ROUTE_B))})
    D["event_templates"]["go"]["transition_cases"][0]["steps"][0]["route"] = list(BROKEN)   # 런타임 유입
    w = world(spatial_state={"placements": {KANG: SP.at_place("항구")}},
              spatial_knowledge={KANG: _know(BROKEN + list(ROUTE_B), {e: 1 for e in BROKEN + list(ROUTE_B)})})
    return D, w


def test_r2_04_runtime_broken_route_contained_without_partial_mutation():
    D, w = _broken_runtime_world()
    snap = copy.deepcopy((w.spatial_state, w.spatial_knowledge, w.__dict__.get("world_schedule")))
    r = run(D, w, K + 60, base_ops=[_set("b", "door_1", "BARRED")])     # 예외가 API 밖으로 나오지 않는다
    assert r.status in (WP.COMPLETED, WP.CONFLICT)
    assert any(d["code"] == "MOVEMENT_ROUTE_INVALID" for d in r.diagnostics), r.diagnostics
    if r.projected_world is not None:
        pls = r.projected_world.domain("spatial_state")["placements"]
        assert pls[KANG] == SP.at_place("항구")                         # 이동·전지적 대체 없음
        assert not any(e["kind"] == "MOVEMENT_WAKEUP"
                       for e in r.projected_world.domain("world_schedule").values())
        assert not [o for o in r.ops if o.op_type in (SP.OP_ACTOR_TRANSIT_SET, SP.OP_EDGE_KNOWLEDGE_UPSERT)]
    assert (w.spatial_state, w.spatial_knowledge, w.__dict__.get("world_schedule")) == snap


def test_r2_04_runtime_invalid_destination_or_factor_contained():
    """경로 계획 단계의 MovementError(없는 장소·잘못된 배수)도 진행 API 밖으로 나오지 않는다."""
    for patch in ({"destination": "없는장소"}, {"stance_factor": "0"}):
        D = defn({"go": _go_tpl()})
        D["event_templates"]["go"]["transition_cases"][0]["steps"][0].update(patch)
        w = world(spatial_state={"placements": {KANG: SP.at_place("항구")}},
                  spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
        snap = copy.deepcopy((w.spatial_state, w.spatial_knowledge))
        r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
        assert any(d["code"] in ("MOVEMENT_PLAN_INVALID", "MOVEMENT_ROUTE_INVALID") for d in r.diagnostics), patch
        assert r.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("항구")
        assert (w.spatial_state, w.spatial_knowledge) == snap


def test_r2_04_valid_route_and_unknown_route_policy_unchanged():
    D = defn({"go": _go_tpl(route=list(ROUTE_B))})
    w = world(spatial_state={"placements": {KANG: SP.at_place("항구")}},
              spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    pl = r.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hh#fwd"
    # H07 supersede(사용자 결정): 저작된 명시 route는 행위자 지식이 없어도 연속성·실제 접근이 유효하면 이동한다
    # (저작자가 그 경로 이용을 큐레이션했다). 다른 길만 알아도 명시 경로를 그대로 따른다 — 대체 경로 없음.
    w = world(spatial_state={"placements": {KANG: SP.at_place("항구")}},
              spatial_knowledge={KANG: _know(("e_hm#fwd", "e_mc#fwd"), {"e_hm#fwd": 1, "e_mc#fwd": 1})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    pl = r.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hh#fwd"
    assert not any(d["code"] == "MOVEMENT_ROUTE_CONSTRAINT_UNKNOWN" for d in r.diagnostics)
    (wk,) = [e for e in r.projected_world.domain("world_schedule").values() if e["kind"] == "MOVEMENT_WAKEUP"]
    assert wk["payload"]["explicit_route"] is True and wk["payload"]["route_edge_ids"] == list(ROUTE_B)
    end = run(D, r.projected_world, K + 30)                              # 남은 명시 구간 그대로(재계획 없음)
    assert end.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("성당")
    # 행위자가 명시 경로의 출발점에 없으면 거부(대체 경로로 바꾸지 않는다 — 도착점 불일치는 컴파일 ERROR)
    w = world(spatial_state={"placements": {KANG: SP.at_place("시장")}}, spatial_knowledge={})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("시장")
    assert any(d["code"] == "MOVEMENT_ROUTE_ENDPOINTS_MISMATCH" for d in r.diagnostics)


# ══════════════════════════════════════════════════════════════
# R2-05 같은 분 중재(3개 이상 효과 · 연결 성분)
# ══════════════════════════════════════════════════════════════

ST = {"open": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "OPEN"},
      "barred": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "BARRED"},
      "locked": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "LOCKED"}}


def _three(c_state, ctl="door_1"):
    steps = {"a": dict(ST["open"], control=ctl), "b": dict(ST["barred"], control=ctl),
             "c": dict(ST[c_state], control=ctl)}
    return _fx_world([_deferred("a", K + 5, "", "", prio=2), _deferred("b", K + 5, "", "", prio=1),
                      _deferred("c", K + 5, "", "", prio=2)], steps=steps)


def test_r2_05_case_a_lower_dropped_then_equal_top_effects_are_compatible():
    D, w = _three("open")
    r = run(D, w, K + 5, progress={"scene_place": "시장"})
    assert r.status == WP.COMPLETED, (r.status, r.diagnostics, r.same_minute_ties)
    assert r.same_minute_ties == []
    assert r.projected_world.domain("access_control_state")["door_1"] == {"state": "OPEN"}
    assert [d["unit_id"] for d in r.diagnostics if d["code"] == "SUPERSEDED_BY_PRIORITY"] == ["due:b"]
    assert r.projected_world.domain("world_schedule") == {}                   # 셋 다 소비


def test_r2_05_case_b_real_top_conflict_after_drop_is_foreground_tie():
    D, w = _three("locked")
    r = run(D, w, K + 5, progress={"scene_place": "시장"})
    assert r.status == WP.TIE_PENDING and r.ops == ()
    (tie,) = r.same_minute_ties
    assert tie.effect_ids == ("due:a", "due:c")                               # 낮은 b는 tie에 없다
    assert tie.affected_targets == (("access_control_state", ("door_1",)),)
    rr = run(D, w, K + 5, progress={"scene_place": "시장"}, resolved_tie_orders={tie.tie_id: ["due:c", "due:a"]})
    assert rr.status == WP.COMPLETED
    assert rr.projected_world.domain("access_control_state")["door_1"] == {"state": "OPEN"}


def test_r2_05_case_d_offscreen_real_top_conflict_still_safe_abort():
    D, w = _three("locked", ctl="door_2")
    r = run(D, w, K + 5, progress={"scene_place": "외딴곳"})
    assert r.status == WP.CONFLICT and r.ops == () and r.projected_world is None
    D, w = _three("open", ctl="door_2")                                       # 같은 효과면 장면 밖이어도 호환
    r = run(D, w, K + 5, progress={"scene_place": "외딴곳"})
    assert r.status == WP.COMPLETED
    assert r.projected_world.domain("access_control_state")["door_2"] == {"state": "OPEN"}


class _C:
    def __init__(self, ops, path, code="MERGE_CONFLICT"):
        self.op_ids, self.domain, self.path, self.code = tuple(ops), "access_control_state", (path,), code


def test_r2_05_case_c_conflict_groups_are_connected_components():
    eng = WP._Engine.__new__(WP._Engine)
    owner = {"a": "A", "b": "B", "c": "C", "d": "D", "e": "E", "f": "F"}
    # A–B, C–D, B–C(다리): 하나의 성분이어야 한다. E–F는 별개.
    groups = eng._conflict_groups([_C("ab", "x"), _C("cd", "y"), _C("bc", "z"), _C("ef", "w")], owner)
    comps = sorted((sorted(g), sorted(t)) for g, t in groups)
    assert comps == [(["A", "B", "C", "D"], [("access_control_state", ("x",)), ("access_control_state", ("y",)),
                                             ("access_control_state", ("z",))]),
                     (["E", "F"], [("access_control_state", ("w",))])]
    # 각 단위는 정확히 한 성분
    seen = [u for g, _ in groups for u in g]
    assert len(seen) == len(set(seen))


def _engine(D, w, progress, ties=None):
    return WP._Engine(D, w, K, base_ops=(), progress=progress, narrative_seed=SEED, resolved_tie_orders=ties,
                      max_rounds=WP.MAX_CASCADE_ROUNDS, max_transitions=WP.MAX_TRANSITIONS_PER_EVENT_PER_MINUTE,
                      outer_origin="PLAYER_TURN")


def _bridge_units():
    """같은 계층의 다리 충돌: door_1(A,B) · door_2(C,D) · door_3(B,C) — 다리가 정렬상 마지막."""
    def u(uid, *writes, prio=0):
        return WP._Unit(uid, [WM.make_op(f"{uid}.{n}", "ACCESS_STATE_SET", "TEST", {"control_id": c, "state": s})
                              for n, (c, s) in enumerate(writes)], priority=prio)
    return lambda **pr: [u("A", ("door_1", "OPEN"), prio=pr.get("A", 0)),
                         u("B", ("door_1", "LOCKED"), ("door_3", "OPEN"), prio=pr.get("B", 0)),
                         u("C", ("door_2", "OPEN"), ("door_3", "LOCKED"), prio=pr.get("C", 0)),
                         u("D", ("door_2", "BARRED"), prio=pr.get("D", 0))]


def _bridge_defn():
    raw = ev_raw()
    raw["access_control_definitions"]["controls"]["door_3"] = {"rule_ref": "door_rule", "initial_state": "LOCKED"}
    res = dc.compile_scenario(raw, "t")
    assert res.ok
    return res.definition


def test_r2_05_case_c_bridge_component_through_real_projection_single_tie():
    D, units = _bridge_defn(), _bridge_units()
    with pytest.raises(WP._Stop) as e:
        _engine(D, world(), {"scene_place": "시장"}).apply_units(units(), kind="TEST")
    assert e.value.status == WP.TIE_PENDING
    tie = e.value.kw["tie"]
    assert tie.effect_ids == ("A", "B", "C", "D")                            # 하나의 연결 성분
    assert [t[1][0] for t in tie.affected_targets] == ["door_1", "door_2", "door_3"]
    assert len(tie.allowed_orderings) == 24
    # 성분 안에서 낮은 우선순위만 제거: B(1)가 A(2)·C(2)에 지배 → 제거 후 C–D 동일 우선순위 실제 충돌만 남는다
    with pytest.raises(WP._Stop) as e:
        _engine(D, world(), {"scene_place": "시장"}).apply_units(units(A=2, B=1, C=2, D=2), kind="TEST")
    assert e.value.status == WP.CONFLICT and e.value.kw["units"] == ["C", "D"]   # door_2는 장면 밖 → 안전 중단
    # 지배 사슬 A(3)>B(2)>C(1): B만 이번에 제거 → C는 제거된 B하고만 충돌했으므로 살아남는다
    eng = _engine(D, world(), {"scene_place": "시장"})
    us = units(A=3, B=2, C=1, D=1)
    us[3].ops = []                                                            # D 없음
    eng.apply_units(us, kind="TEST")
    acs = eng.world.domain("access_control_state")
    assert acs["door_1"] == {"state": "OPEN"} and acs["door_2"] == {"state": "OPEN"} and \
        acs["door_3"] == {"state": "LOCKED"}
    assert [d["unit_id"] for d in eng.result.diagnostics if d["code"] == "SUPERSEDED_BY_PRIORITY"] == ["B"]
