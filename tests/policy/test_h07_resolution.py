# -*- coding: utf-8 -*-
"""H07 — 플레이어 명시 결단(resolution) · 사건 국면 지침(R-2) · 레거시 조건 의미 보존(소지품·스탯·결말 이력).

  · 결단은 판단층위가 저작 선택지 키를 고를 때만 — 코드가 예약 사건 지역 상태 'resolution'에 typed
    EVENT_LOCAL_SET(PLAYER_RESOLUTION)으로 쓰고, 기존 국면·케이스가 결과를 적용한다(새 분기 엔진 없음).
  · 저작 step·GM 지침·다른 출처는 결단을 만들 수 없고, 한 번 정한 결단은 번복되지 않는다.
"""
from __future__ import annotations

import copy

import pytest

from core import action_intent as AI
from core import definition_compiler as dc
from core import events as EV
from core import player_world as PW
from core import turn_world as TW
from core import world_mutation as WM
from core import world_refs as RF
from core import predicates as PR
from tests.policy.test_h04b_events_progression import K, ev_raw, op, run, world
from tests.policy.test_h05a_intent_planning import PC, ctx_of, make_session, manifest, plan

pytestmark = pytest.mark.policy

ROOM = "성당"
DOOR = {"ref": "ACCESS_STATE", "control": "door_2"}
SELF_RES = {"ref": "EVENT_LOCAL", "instance": "$self", "key": "resolution"}


def _crisis():
    """장면(성당)에서 열리는 결단 국면 — 결과는 케이스가 적용한다."""
    return {"version": 1, "name": "결정의 밤",
            "spawn_when": {"cmp": "EQ", "ref": {"ref": "SCENE_PLACE"}, "value": ROOM},
            "relevance_places": [ROOM],
            "phases": {
                "standoff": {"eligible_when": {"const": True}, "priority": 1, "guide": "GM지침-대치",
                             "retain_when": {"not": {"exists": SELF_RES}},
                             "resolution_options": [{"option_id": "keep", "label": "질서를 지킨다"},
                                                    {"option_id": "open", "label": "문을 연다"}]},
                "decided": {"eligible_when": {"exists": SELF_RES}, "priority": 2, "terminal": True}},
            "transition_cases": [
                {"case_id": "spawn", "from": None, "to": "standoff", "steps": []},
                {"case_id": "keep", "from": "standoff", "to": "decided",
                 "valid_when": {"cmp": "EQ", "ref": SELF_RES, "value": "keep"},
                 "steps": [{"step_id": "e", "type": "SET_EVENT_LOCAL", "key": "ending", "value": "order"}]},
                {"case_id": "open", "from": "standoff", "to": "decided",
                 "valid_when": {"cmp": "EQ", "ref": SELF_RES, "value": "open"},
                 "steps": [{"step_id": "e", "type": "SET_EVENT_LOCAL", "key": "ending", "value": "opened"},
                           {"step_id": "d", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}]}],
            "case_selection": "FIRST_VALID", "resolve_when": {"cmp": "EQ", "ref": {"ref": "EVENT_PHASE",
                                                                                   "instance": "$self"},
                                                              "value": "decided"}}


def _raw(**extra):
    return ev_raw({"crisis": _crisis()}, **extra)


def _session_at_room():
    s, D = make_session(_raw(), at=ROOM, places=("항구", ROOM))
    r = run(D, s, K, progress={"observer_actor": PC, "scene_place": ROOM}, outer_origin="INITIALIZATION",
            initial_full_scan=True)
    for f in ("event_instances", "world_schedule", "access_control_state"):
        setattr(s, f, r.projected_world.domain(f))
    return s, D


def _inst(w):
    ev = w.domain("event_instances") if isinstance(w, WM.ProjectedWorld) else w.event_instances
    return next(i for i in ev.values() if i["template_id"] == "crisis")


# ── 컴파일 ─────────────────────────────────────────────────────

def test_resolution_options_compile_and_reserved_key_is_rejected_for_authored_steps():
    D = dc.compile_scenario(_raw(), "t").definition
    assert D["event_templates"]["crisis"]["phases"]["standoff"]["resolution_options"][1] == \
        {"option_id": "open", "label": "문을 연다"}
    bad = _crisis()
    bad["transition_cases"][1]["steps"].append({"step_id": "x", "type": "SET_EVENT_LOCAL", "key": "resolution",
                                                "value": "keep"})
    assert "EVENT_LOCAL_RESERVED_KEY" in [d.code for d in dc.compile_scenario(ev_raw({"c": bad}), "t").errors]
    bad = _crisis()
    bad["phases"]["standoff"]["resolution_options"] = [{"option_id": "a", "label": "x"}, {"option_id": "a", "label": "y"}]
    assert "EVENT_RESOLUTION_OPTIONS_INVALID" in [d.code for d in dc.compile_scenario(ev_raw({"c": bad}), "t").errors]


# ── 매니페스트 · 의도 · 계획 ───────────────────────────────────

def test_manifest_offers_open_resolutions_only_in_relevant_scene():
    s, D = _session_at_room()
    m = manifest(s)
    assert [lbl for _, _, lbl in m.resolutions] == ["질서를 지킨다", "문을 연다"]
    assert "플레이어가 내릴 수 있는 결단" in m.render() and "resolution" in m.schema()["properties"]
    s.spatial_state = {"placements": {PC: {"kind": "AT_PLACE", "place_id": "항구"}}, "scene_place": "항구"}
    assert manifest(s).resolutions == ()                                  # 장면과 닿지 않으면 후보 없음


def test_no_resolution_without_explicit_player_choice_and_guide_creates_nothing():
    s, D = _session_at_room()
    p, _, intent = plan(s, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 30,
                            "activity": "WAIT"})
    assert intent.resolution is None
    inst = _inst(p.projected_world)
    assert inst["current_phase"] == "standoff" and "resolution" not in inst["local_state"]
    assert "ending" not in inst["local_state"]                            # 지침·시간만으로 결말 사실 없음


def test_explicit_resolution_applies_outcome_through_state_machine():
    s, D = _session_at_room()
    m = manifest(s)
    key = next(k for k, (_, oid), _ in m.resolutions if oid == "open")
    p, _, intent = plan(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    assert intent.resolution[1] == "open" and p.status == PW.PLAN_PLANNED
    inst = _inst(p.projected_world)
    assert inst["local_state"]["resolution"] == "open" and inst["local_state"]["ending"] == "opened"
    assert inst["lifecycle"] == "RESOLVED"
    assert p.projected_world.domain("access_control_state")["door_2"]["state"] == "OPEN"
    res_ops = [o for o in p.ops if o.op_type == EV.OP_EVENT_LOCAL_SET and o.payload["key"] == "resolution"]
    assert len(res_ops) == 1 and res_ops[0].source_kind == EV.RESOLUTION_SOURCE
    sem = intent.semantic_record()
    assert AI.ResolvedActionIntent.from_semantic(sem, definition_revision=intent.definition_revision).resolution == \
        intent.resolution                                                  # 재서술은 같은 결단을 재현


def test_resolution_write_is_player_only_and_irreversible():
    s, D = _session_at_room()
    iid = _inst(s)["instance_id"]
    src = WM.ProjectedWorld.capture(s)

    def proj(o):
        b = WM.WorldMutationBatch(batch_id="b", outer_origin="PLAYER_TURN", source_world_version_id=None,
                                  definition_revision=None)
        b.add(o)
        b.freeze()
        return WM.project(src, b, WM.PRODUCTION_REGISTRY)
    forged = WM.make_op("f", EV.OP_EVENT_LOCAL_SET, "WORLD_PROGRESSION",
                        {"instance_id": iid, "key": "resolution", "value": "keep"})
    assert not proj(forged).ok                                            # 사건·진행 출처는 결단을 쓸 수 없다
    ok = proj(WM.make_op("r", EV.OP_EVENT_LOCAL_SET, EV.RESOLUTION_SOURCE,
                         {"instance_id": iid, "key": "resolution", "value": "keep"}))
    assert ok.ok
    src = ok.world
    again = proj(WM.make_op("r2", EV.OP_EVENT_LOCAL_SET, EV.RESOLUTION_SOURCE,
                            {"instance_id": iid, "key": "resolution", "value": "open"}))
    assert not again.ok                                                   # 번복 없음
    stale = AI.ResolvedActionIntent(resolution=(iid, "nope"))
    c = ctx_of(s)
    p = PW.plan_player_turn(c, s, stale, transaction_id="t", narrative_seed="x")
    assert any(d["code"] == "RESOLUTION_NOT_OPEN" for d in p.diagnostics)


# ── R-2 지침 ───────────────────────────────────────────────────

def test_event_guide_block_is_instruction_only_and_lists_options_without_choosing():
    s, D = _session_at_room()
    blk = TW.event_guide_block(ctx_of(s), None, s)
    assert "GM지침-대치" in blk and "질서를 지킨다 / 문을 연다" in blk and "대신 고르거나" in blk
    s.spatial_state = {"placements": {PC: {"kind": "AT_PLACE", "place_id": "항구"}}, "scene_place": "항구"}
    assert TW.event_guide_block(ctx_of(s), None, s) == ""                # 장면과 닿지 않으면 싣지 않는다


# ── 레거시 조건 의미 보존 ───────────────────────────────────────

def test_event_local_template_ref_and_observer_item_stat_refs():
    s, D = _session_at_room()
    w = WM.ProjectedWorld.capture(s)
    c = RF.EvalContext(D, w)
    ref = {"ref": "EVENT_LOCAL", "template": "crisis", "key": "ending"}
    assert RF.resolve(ref, c) == RF.UNKNOWN
    assert RF.dependency_keys(ref) == ["event:*:local:ending"]
    c2 = RF.EvalContext(D, w, progress={"observer_items": ("항생제",), "observer_stats": {"민첩": 11}})
    assert RF.resolve({"ref": "OBSERVER_HAS_ITEM", "item": "항생제"}, c2) == RF.known(True)
    assert RF.resolve({"ref": "OBSERVER_HAS_ITEM", "item": "연료"}, c2) == RF.known(False)
    assert PR.evaluate({"cmp": "GTE", "ref": {"ref": "OBSERVER_STAT", "stat": "민첩"}, "value": 11}, c2) == PR.TRUE
    assert RF.resolve({"ref": "OBSERVER_STAT", "stat": "기술"}, c2) == RF.UNKNOWN
    assert RF.resolve({"ref": "OBSERVER_HAS_ITEM", "item": "항생제"}, c) == RF.UNKNOWN   # 공급 안 된 진행


def test_item_gated_event_spawns_only_when_player_turn_supplies_the_item():
    tpl = {"version": 1, "spawn_when": {"all": [{"cmp": "EQ", "ref": {"ref": "SCENE_PLACE"}, "value": ROOM},
                                                {"cmp": "EQ", "ref": {"ref": "OBSERVER_HAS_ITEM", "item": "항생제"},
                                                 "value": True}]},
           "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}}}
    s, D = make_session(ev_raw({"barter": tpl}), at=ROOM, places=("항구", ROOM))
    wait = {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 5, "activity": "WAIT"}
    p, _, _ = plan(s, wait, caps=())
    assert not [i for i in (p.projected_world.domain("event_instances") or {}).values()]
    p, _, _ = plan(s, wait, caps=("항생제",))
    assert [i["template_id"] for i in p.projected_world.domain("event_instances").values()] == ["barter"]
