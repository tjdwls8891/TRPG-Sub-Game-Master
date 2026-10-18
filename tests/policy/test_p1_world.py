# -*- coding: utf-8 -*-
"""P1 — 묘사 전 세계 진행 · 단일 묘사 · 같은 턴 보충 평가(순수 경로).

  · D1: 행동 소요 시간(action_minutes) — 판단 제안을 코드가 검증(상한·정수·미제시는 시간 없음), 의미 기록 재현.
  · W5: 묘사 후 보충 진행이 묘사 전 계획과 같은 관찰자 문맥(소지품·능력치·지급)을 쓴다 — 판정 불가로 떨어지지 않는다.
  · W8: PC 인지 조건 사건(합성 — 영도 저작 0건과 무관하게 엔진 수준 검증) — 묘사 후 인지로 성립한 가시 사건은
        유실되지 않고 전달 대기(pending)로 남는다(v7.21.1: 다음 본문 1회).
  · D4: 이번 턴 지급·사용 사실은 같은 턴에 평가되고 다음 턴에 다시 평가되지 않는다(이중 반응 금지).
"""
from __future__ import annotations

import pytest

from core import action_intent as AI
from core import items as IT
from core import movement as MV
from core import player_world as PW
from core import turn_world as TW
from core import world_refs as RF
from tests.policy.test_h04b_events_progression import eq
from tests.policy.test_h05a_intent_planning import ctx_of, make_session, plan, raw_with

pytestmark = pytest.mark.policy


def _tpl(cond, cue="문이 삐걱 열린다", sig="door_creak"):
    return {"version": 1, "spawn_when": cond,
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": [
                {"step_id": "sig", "type": "EMIT_SIGNAL", "signal_id": sig, "cue": cue,
                 "interrupt_policy": "IF_OBSERVABLE", "observable_when": eq({"ref": "SCENE_PLACE"}, "항구")}]}]}


AWARE = eq({"ref": "COGNITION_AWARENESS", "holder": RF.SESSION_PC, "info": "S40"}, "CONTENT")
HAS_KEY = eq({"ref": "OBSERVER_HAS_ITEM", "item": "열쇠"}, True)
EXPOSE = TW.ObservationManifest(infos=(("X1", "S40", "공개 요약"),))
NONE_INTENT = {"movement_kind": "NONE", "time_kind": "NONE", "action_minutes": 0}


def _observe(s, p, raw, **kw):
    return TW.process_observation(ctx_of(s), s, p, EXPOSE, raw, transaction_id="tx-p1", narrative_seed="seed", **kw)


# ── D1 ──

def test_action_minutes_schema_required_and_validated():
    s, _ = make_session()
    c = ctx_of(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    sch = m.schema()
    assert AI.ACTION_MINUTES_FIELD in sch["required"] and sch["properties"]["action_minutes"] == {"type": "integer"}
    ok = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "NONE", "action_minutes": 15}, world=s)
    assert ok.action_minutes == 15 and not ok.unresolved
    big = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "NONE", "action_minutes": 9999}, world=s)
    assert big.action_minutes == AI.MAX_ACTION_MINUTES and ("action_minutes", "CLAMPED") in big.unresolved
    for bad in (-1, True, "10", None):
        r = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "NONE", "action_minutes": bad}, world=s)
        assert r.action_minutes is None                                  # 근거 없는 시간 없음
    # 재서술 재현: 의미 기록에 남고 그대로 복원된다
    back = AI.ResolvedActionIntent.from_semantic(ok.semantic_record(), definition_revision=c.definition_revision)
    assert back.action_minutes == 15


def test_action_minutes_planned_before_narration_as_code_owned_time():
    s, _ = make_session()
    p, _, _ = plan(s, dict(NONE_INTENT, action_minutes=25))
    assert p.status == PW.PLAN_PLANNED and p.code_owned_minutes == 25
    assert p.outcome["kind"] == PW.OUT_ACTION_DONE
    assert "경과 시간: 25분" in TW.narration_overlay(p, ctx_of(s).definition, ctx_of(s), world=s)
    # 이동 선언이 있으면 이동 규칙이 우선 — 행동 시간은 더하지 않는다
    s2, _ = make_session()
    m = AI.build_manifest(ctx_of(s2).definition, s2, ctx_of(s2).lead_pc,
                          definition_revision=ctx_of(s2).definition_revision)
    key = next(k for k, pid, _ in m.places if pid == "시장")
    p2, _, _ = plan(s2, {"movement_kind": "DESTINATION", "destination": key, "time_kind": "NONE",
                         "action_minutes": 90})
    assert p2.code_owned_minutes == 5


# ── W5 · W8 ──

def test_pc_awareness_event_after_narration_is_kept_pending_not_lost():
    s, _ = make_session(raw_with({"react": _tpl(AWARE)}))
    p, _, _ = plan(s, NONE_INTENT)
    res = _observe(s, p, {"info_exposed": ["X1"]})
    assert res.status == TW.TW_READY
    (pend,) = res.pending                                                # 같은 분 보충 진행 → 본문 밖 가시 사건
    assert pend["signal_id"] == "door_creak" and "carried" not in pend   # v7.21.1: 횟수 없음(1회 전달)
    assert any(o.op_type == "EVENT_INSTANCE_CREATE" or "EVENT" in o.op_type for o in res.ops)
    # 이번 본문에 이미 준 대기(같은 키)는 다시 남지 않는다 — 표현 판정과 무관(재이월 없음)
    m = TW.ObservationManifest(infos=EXPOSE.infos, signals=(("S1", ("door_creak", pend["instance_id"]), "x"),))
    res2 = TW.process_observation(ctx_of(s), s, p, m, {"info_exposed": ["X1"]},
                                  transaction_id="tx-p1", narrative_seed="seed", pending=[pend])
    assert res2.pending == []


def test_post_narration_progression_uses_plan_observer_context():
    # 소지품 조건(OBSERVER_HAS_ITEM)이 보충 진행에서 판정 불가(UNKNOWN)로 떨어지지 않는다
    tpl = _tpl({"all": [AWARE, HAS_KEY]})
    s, _ = make_session(raw_with({"react": tpl}), items={"열쇠": 1})
    p, _, _ = plan(s, NONE_INTENT)
    assert "열쇠" in p.observer["observer_items"]
    assert [r["signal_id"] for r in _observe(s, p, {"info_exposed": ["X1"]}).pending] == ["door_creak"]
    # 열쇠가 없으면 성립하지 않는다(임의 통과 아님)
    s2, _ = make_session(raw_with({"react": tpl}))
    p2, _, _ = plan(s2, NONE_INTENT)
    assert _observe(s2, p2, {"info_exposed": ["X1"]}).pending == []


# ── D4 ──

def test_same_turn_paid_fact_is_evaluated_in_supplemental_progression():
    tpl = _tpl(eq({"ref": "OBSERVER_PAID_ITEM", "item": "연료"}, True), cue="경비가 문을 열어 준다", sig="gate")
    s, _ = make_session(raw_with({"barter": tpl}))
    p, _, _ = plan(s, NONE_INTENT)
    assert _observe(s, p, {}).pending == []                              # 지급 없음 → 반응 없음
    res = _observe(s, p, {}, item_facts={"used": [], "paid": ["연료"]})
    assert [r["signal_id"] for r in res.pending] == ["gate"]


def test_same_turn_evaluated_facts_are_not_re_evaluated_next_turn():
    s, _ = make_session()
    s.item_facts = {"tx": "t1", "used": ["붕대"], "paid": ["연료"], "evaluated_same_turn": True}
    s.world_version = dict(s.world_version or {}, cause_id="t1")
    assert IT.current_facts(s) == {"used": [], "paid": []}
    s.item_facts = {"tx": "t1", "used": ["붕대"], "paid": ["연료"]}            # P1 이전 기록 — 기존 의미 유지
    assert IT.current_facts(s) == {"used": ["붕대"], "paid": ["연료"]}


def test_access_payment_is_visible_to_later_steps_of_the_same_plan():
    s, D = make_session()
    run = PW._Run(D, s, seed="x", tie_orders=None, prefix="p", observer={"observer_items": ()})
    pl = PW.PlayerWorldPlan(PW.PLAN_NONE)

    class Trav:
        checkpoint_times = [{"kind": MV.ACCESS_ACTION, "action_id": "a1",
                             "payment": {"capability": "연료", "quantity": 1}}]
    PW._note_payments(pl, run, Trav)
    assert run.observer["observer_paid_items"] == ("연료",)
    assert pl.item_payments == [{"capability": "연료", "quantity": 1, "action_id": "a1"}]
