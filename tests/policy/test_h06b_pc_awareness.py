# -*- coding: utf-8 -*-
"""H06-B — 세션 PC 인지(awareness) 사건 조건 · 선택적 의존 깨우기 · PC 믿음 조건 금지.

  · holder "$session_pc"는 정본 actor_registry의 단일 PC ActorId로만 해석(이름 매칭 없음).
  · EXISTENCE / CONTENT 구분 · PC 인지 변화는 그 키에 의존하는 사건만 깨운다.
  · COGNITION_BELIEF + $session_pc는 컴파일 오류. NPC 인지/믿음 조건은 기존 그대로.
  · PC가 여럿이면(LEGACY) 해석하지 않는다(UNKNOWN · 의존 키 구체화 없음).
"""
from __future__ import annotations

import pytest

import core
from core import cognition as CG
from core import definition_compiler as dc
from core import world_mutation as WM
from core import world_progression as WP
from core import world_refs as RF
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K, eq, ev_raw, world

pytestmark = pytest.mark.policy

PC = "pc_555001"
REG = {PC: {"kind": "PC", "display_name": "주인공", "source_kind": "PLAYER", "source_key": "555001",
            "aliases": []}}


def _tpl(cond):
    return {"version": 1, "spawn_when": cond,
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": []}]}


def aware(holder, value, ref="COGNITION_AWARENESS"):
    return eq({"ref": ref, "holder": holder, "info": "S40"}, value)


TEMPLATES = {"knows": _tpl(aware(RF.SESSION_PC, "CONTENT")),
             "heard": _tpl(aware(RF.SESSION_PC, "EXISTENCE")),
             "other": _tpl(eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"))}


def defn():
    res = dc.compile_scenario(ev_raw(TEMPLATES), "t")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    return res.definition


def acquire(holder_actor, awareness, n=0):
    return WM.make_op(f"acq{n}", CG.OP_COGNITION_ACQUIRE, "TEST", {
        "holder": CG.actor_holder(holder_actor), "info_id": "S40", "awareness": awareness,
        "acquisition": CG.acquisition_record(f"a{n}", "TESTIMONY", at_minute=K)})


def run(D, w, ops):
    r = WP.project_world_progression(D, w, K, base_ops=ops, narrative_seed="seed")
    assert r.committable, (r.status, r.diagnostics)
    return sorted(i["template_id"] for i in r.projected_world.domain("event_instances").values())


def test_pc_awareness_wakes_only_dependent_event_and_distinguishes_existence_content():
    D = defn()
    w = world(actor_registry=REG)
    idx = WP.build_dependency_index(D, w)
    aff = idx.affected({f"cognition:actor:{PC}:S40"})
    assert aff["spawn"] == ["heard", "knows"]                         # 선택적: door 사건은 깨우지 않음
    assert run(D, w, [acquire(PC, "EXISTENCE")]) == ["heard"]         # EXISTENCE ≠ CONTENT
    assert run(D, w, [acquire(PC, "CONTENT")]) == ["knows"]


def test_pc_rename_does_not_change_binding():
    D = defn()
    reg = {PC: dict(REG[PC], display_name="개명한 이름", aliases=["주인공"])}
    assert run(D, world(actor_registry=reg), [acquire(PC, "CONTENT")]) == ["knows"]


def test_pc_belief_as_event_condition_is_rejected_at_compile():
    raw = ev_raw({"belief": _tpl(aware(RF.SESSION_PC, "CERTAIN", ref="COGNITION_BELIEF"))})
    res = dc.compile_scenario(raw, "t")
    assert not res.ok
    assert any("$session_pc" in d.message for d in res.errors)


def test_npc_awareness_and_belief_conditions_unchanged():
    raw = ev_raw({"npc_knows": _tpl(aware(f"actor:{KANG}", "CONTENT")),
                  "npc_believes": _tpl(aware(f"actor:{KANG}", "CERTAIN", ref="COGNITION_BELIEF"))})
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]


def test_multi_pc_world_does_not_resolve_session_pc():
    D = defn()
    reg = dict(REG, pc_777={"kind": "PC", "display_name": "둘째", "source_kind": "PLAYER",
                             "source_key": "777", "aliases": []})
    w = world(actor_registry=reg)
    assert RF.session_pc_actor(w) is None
    ctx = RF.EvalContext(D, w)
    assert RF.resolve({"ref": "COGNITION_AWARENESS", "holder": RF.SESSION_PC, "info": "S40"}, ctx) == RF.UNKNOWN
    assert WP.build_dependency_index(D, w).affected({f"cognition:actor:{PC}:S40"})["spawn"] == []
    assert run(D, w, [acquire(PC, "CONTENT")]) == []


def test_session_pc_not_resolved_by_display_name_when_registry_lacks_pc():
    D = defn()
    w = world(actor_registry={})
    ctx = RF.EvalContext(D, w)
    assert RF.resolve({"ref": "COGNITION_AWARENESS", "holder": RF.SESSION_PC, "info": "S40"}, ctx) == RF.UNKNOWN
