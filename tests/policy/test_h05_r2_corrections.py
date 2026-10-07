# -*- coding: utf-8 -*-
"""H05 R2 보정 — 최소 반례.

  R2-1 ActionReferenceManifest·WorldTurn·계획은 definition_revision에도 결속된다(stale 거부).
  R2-2 추출 관찰 후보는 이번 턴 노출 근거가 있는 것만 — 같은 장면 NPC의 숨은 정보·미래 사건 신호 미노출.
  R2-3 같은 분 선후 선택지는 관찰 가능한 사실의 순서로 표현된다(내부 id만으로 표현하지 않음).
"""
from __future__ import annotations

import copy
import re
import types as _types

import pytest

import core
from core import action_intent as AI
from core import cognition as CG
from core import player_world as PW
from core import spatial as SP
from core import turn_transaction as tt
from core import turn_world as TW
from core import world_mutation as WM
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K, eq
from tests.policy.test_h05a_intent_planning import ROUTE_A, ROUTE_B, ctx_of, go, make_session, manifest
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    H05Provider, _alert_tpl, _arm_alert, _fx, _npc_knows_world, drig, extraction, judgment, rig, turn, world_of)

pytestmark = pytest.mark.policy

SECRET_TEXTS = ("원본 기록은 성당 제3보관실", "원본 기록 위치")


# ══════════════════════════════════════════════════════════════
# R2-1 revision stale guard
# ══════════════════════════════════════════════════════════════

def test_r2_1_manifest_from_old_revision_is_stale_and_plans_no_ops():
    s, _ = make_session(edges=("e_hm#fwd",))
    c = ctx_of(s)
    m = manifest(s)
    wv = copy.deepcopy(s.world_version)
    s.definition_revision = "sha256:" + "b" * 64                     # world_version 그대로, revision만 B
    assert s.world_version == wv
    intent = AI.resolve_intent(m, go(m, "시장"), world=s)
    assert intent.stale and intent.movement is None
    p = PW.plan_player_turn(c, s, intent, transaction_id="t", narrative_seed="x")
    assert p.status == PW.PLAN_REJECTED and p.ops == ()


def test_r2_1_plan_with_old_context_rejected_even_if_intent_resolved_before_change():
    s, _ = make_session(edges=("e_hm#fwd",))
    c = ctx_of(s)
    m = manifest(s)
    intent = AI.resolve_intent(m, go(m, "시장"), world=s)            # A에서 정상 해석
    assert not intent.stale
    s.definition_revision = "sha256:" + "c" * 64
    p = PW.plan_player_turn(c, s, intent, transaction_id="t", narrative_seed="x")
    assert p.status == PW.PLAN_REJECTED and p.reason == "DEFINITION_STALE" and p.ops == ()


def test_r2_1_same_revision_path_unchanged():
    s, _ = make_session(edges=("e_hm#fwd",))
    m = manifest(s)
    intent = AI.resolve_intent(m, go(m, "시장"), world=s)
    p = PW.plan_player_turn(ctx_of(s), s, intent, transaction_id="t", narrative_seed="x")
    assert p.status == PW.PLAN_PLANNED and p.ops
    assert m.definition_revision == s.definition_revision


async def test_r2_1_pending_route_choice_does_not_reuse_old_revision_world_turn(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd", "e_hh#fwd", "e_hc#fwd"))
    r.prov.routes["judgment"] = judgment("성당")
    tx = await turn(r, "성당으로 간다")
    old_wt = tx.world_turn
    assert old_wt is not None and old_wt.plan.status == PW.PLAN_NEEDS_CHOICE
    # 같은 world_version, 정의만 B로(구간 거리 변경 → 새 revision)
    raw = copy.deepcopy(r.sess.scenario_data)
    raw["movement_edges"][0]["distance_m"] = 410
    r.sess.scenario_data = raw
    res = core.definition_compiler.compile_scenario(raw, "t")
    assert res.ok and res.revision != old_wt.ctx.definition_revision
    r.sess.definition_revision = res.revision
    PW._DEF_CACHE.clear()

    def pick(kwargs):
        text = H05Provider._text(kwargs)
        m = re.search(r"(R\d+)=산복도로", text)
        return judgment("성당", route_choice=m.group(1) if m else "NONE")(kwargs)
    r.prov.routes["judgment"] = pick
    tx2 = await turn(r, "산복도로 쪽으로")
    assert tx2.transaction_id == tx.transaction_id
    assert tx2.world_turn is not old_wt                                # 옛 revision WorldTurn 재사용 금지
    assert tx2.world_turn is None or tx2.world_turn.ctx.definition_revision == res.revision
    assert SP.placement_of(r.sess, f"pc_{list(r.sess.players)[0]}") == SP.at_place("항구")   # 옛 선택으로 이동 없음


def test_r2_1_old_revision_plan_cannot_enter_new_revision_preparation(drig):
    r = drig
    r.make(edges=("e_hm#fwd",))
    s = r.sess
    c = ctx_of(s)
    m = manifest(s)
    plan = PW.plan_player_turn(c, s, AI.resolve_intent(m, go(m, "시장"), world=s),
                               transaction_id="t", narrative_seed="x")
    assert plan.status == PW.PLAN_PLANNED and plan.ops
    result = TW.TurnWorldResult(TW.TW_READY, plan.ops, plan.projected_world, plan)
    batch = WM.player_turn_batch(s, "t")
    prep = _types.SimpleNamespace(
        world_turn=TW.WorldTurn(ctx=c, plan=plan, result=result), transaction_id="t",
        baseline_world_version=copy.deepcopy(s.world_version),
        baseline_definition_revision="sha256:" + "d" * 64,          # 준비 기준선은 새 revision
        world_mutation_batch=batch, violations=[])
    code = r.gm._stage_world_turn(s, prep)
    assert code == tt.FailureCode.COMMIT_VALIDATION_FAILURE.value
    assert batch.operations == () or len(batch.operations) == 0


# ══════════════════════════════════════════════════════════════
# R2-2 추출 노출 후보 · 신호 후보
# ══════════════════════════════════════════════════════════════

async def test_r2_2_same_scene_npc_hidden_info_not_disclosed_to_extraction(drig):
    r = drig
    r.make(extra_world=_npc_knows_world())                            # 강지수(같은 장면)가 S40 내용을 안다
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    eprompt = r.prov.prompts["extraction"][0][0]
    for t in SECRET_TEXTS:
        assert t not in eprompt, t
    assert not re.search(r"X\d+=", eprompt)                          # 노출 근거 없음 → 후보 없음


async def test_r2_2_future_event_signal_cue_not_in_extraction_prompt(drig):
    r = drig
    _arm_alert(r)                                                     # 활성 사건 — 5분 뒤 신호(아직 미발생)
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "가만히 숨을 고른다")
    eprompt = r.prov.prompts["extraction"][0][0]
    assert "경비가 칼을 뽑아 든다" not in eprompt


async def test_r2_2_signal_given_to_main_narration_stays_represented_candidate(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "10분 기다린다")
    assert "경비가 칼을 뽑아 든다" in r.prov.prompts["narration"][0][0]   # 묘사 전 관찰 신호
    eprompt = r.prov.prompts["extraction"][0][0]
    assert re.search(r"S\d+=경비가 칼을 뽑아 든다", eprompt)


def _evidence_tpl():
    """3분 뒤 장면에 벽보가 붙는다(이번 턴 확정 노출 근거 — 사건이 만든 정보)."""
    eff = {"type": "CREATE_INFORMATION", "proposition": "벽보: 오늘 밤 항구 폐쇄", "summary": "벽보의 공지",
           "subject_refs": ["place:항구"]}
    return {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
            "phases": {"calm": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "calm", "steps": [
                {"step_id": "post", "type": "SCHEDULE_EFFECT", "after_minutes": 3, "effect": eff}]}]}


async def test_r2_2_observed_exposure_grants_content_without_belief(drig):
    r = drig
    D = r.make(templates={"ev": _evidence_tpl()}, extra_world=_npc_knows_world())
    arm = core.world_progression.project_world_progression(
        D, r.sess, K, base_ops=[WM.make_op("arm", "ACCESS_STATE_SET", "TEST",
                                           {"control_id": "door_2", "state": "BARRED"})],
        narrative_seed=r.sess.narrative_seed or r.sess.session_id)
    for f in core.world_state.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(r.sess, f, copy.deepcopy(arm.projected_world.domain(f)))
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")

    def ext(kwargs):
        text = H05Provider._text(kwargs)
        m = re.search(r"(X\d+)=벽보의 공지", text)
        return extraction({"info_exposed": [m.group(1)] if m else []})
    r.prov.routes["extraction"] = ext
    await turn(r, "10분 기다린다")
    eprompt = r.prov.prompts["extraction"][0][0]
    assert "벽보의 공지" in eprompt and "오늘 밤 항구 폐쇄" not in eprompt   # 요약만(명제 전문 미전달)
    for t in SECRET_TEXTS:
        assert t not in eprompt
    pc = f"actor:pc_{list(r.sess.players)[0]}"
    recs = r.sess.cognition_state.get(pc) or {}
    assert len(recs) == 1
    (rec,) = recs.values()
    assert rec["awareness"] == "CONTENT" and rec["belief"] is None


def test_r2_2_continuation_skips_signals_already_given_to_narration():
    sig = {"signal_id": "s", "instance_id": "i", "cue": "c", "interrupt_policy": "IF_OBSERVABLE",
           "observable": True}
    assert TW.unrepresented_signals([sig], {("s", "i")}) == []
    assert TW.unrepresented_signals([sig], set()) == [sig]


# ══════════════════════════════════════════════════════════════
# R2-3 같은 분 선후 — 관찰 가능한 사실 표현
# ══════════════════════════════════════════════════════════════

def _tie_section(text):
    i = text.find("같은 순간")
    return text[i:i + 600] if i >= 0 else ""


async def test_r2_3_tie_options_name_competing_observable_facts(drig):
    r = drig
    _fx(r, "door_1")                                                  # 시장→성당 통로 출입: 열림 vs 막힘(같은 우선순위)
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    r.prov.routes["extraction"] = [extraction({"tie_order": "NONE"})]
    await turn(r, "10분 기다린다")
    sec = _tie_section(r.prov.prompts["narration"][0][0])
    opts = dict(re.findall(r"(O\d+): (.+?)(?= - O\d+:| \[|\n|$)", sec))
    assert set(opts) == {"O1", "O2"}
    for label in opts.values():
        assert "열" in label and "막" in label                         # 두 경쟁 사실이 각각 식별된다
        assert "→" in label
    assert opts["O1"] != opts["O2"]                                   # 두 순서의 의미가 구분된다
    first = {k: v.split("→")[0] for k, v in opts.items()}
    assert ("열" in first["O1"]) != ("열" in first["O2"])
    for bad in ("due:", "evt_fx", "door_1", "예약된 일"):              # 내부 opaque id로만 표현하지 않는다
        assert bad not in sec
    esec = r.prov.prompts["extraction"][0][0]
    assert all(v in esec for v in opts.values())                       # 추출도 같은 의미 라벨


async def test_r2_3_offscreen_conflict_never_presented_as_tie(drig):
    r = drig
    _fx(r, "door_2")
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    tx = await turn(r, "10분 기다린다")
    assert "narration" not in r.prov.calls
    assert all("같은 순간" not in p for p, _ in r.prov.prompts.get("logic", []))
    assert tx.failure_code == tt.FailureCode.WORLD_PROJECTION_CONFLICT
