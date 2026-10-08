# -*- coding: utf-8 -*-
"""H07 런타임 보강 — 영도 이행에 필요한 최소 계약.

  · 컴파일러: phases[*].guide(지시층위 전용 지침) · relevance_places · EMIT_SIGNAL.exposes(summary 필수) ·
    world_start 틀의 무작위 장소·시각 슬롯 금지. 미저작 시나리오의 정의·revision은 그대로.
  · R-1: 본 묘사에 준 관찰 신호의 exposes → 관찰 매니페스트 후보 → 추출이 노출을 보고할 때만 PC CONTENT
    획득(믿음 불변) → $session_pc 인지 조건 사건이 실제로 깨어난다.
  · R-5: 장소에 도착(또는 시작)하면 그 자리의 PUBLIC·LOCAL 출구와 그 너머 장소 이름을 안다(SECRET 제외).
"""
from __future__ import annotations

import copy

import pytest

from core import cognition as CG
from core import definition_compiler as dc
from core import spatial as SP
from core import world_lifecycle as WL
from core import world_refs as RF
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import codes, eq, ev_raw
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, drig, extraction, judgment, obs_key, rig, turn)
from tests.policy.test_h06a_lifecycle import dyn_raw, setup_session

pytestmark = pytest.mark.policy


def _sig(sid, cue, exposes=None, observable=True):
    st = {"step_id": sid, "type": "EMIT_SIGNAL", "signal_id": sid, "cue": cue,
          "interrupt_policy": "IF_OBSERVABLE", "observable_when": {"const": observable}}
    if exposes is not None:
        st["exposes"] = exposes
    return st


def arrive_tpl(exposes=("S40",), observable=True, guide=None):
    ph = {"eligible_when": {"const": True}, "priority": 1}
    if guide is not None:
        ph["guide"] = guide
    return {"version": 1, "spawn_when": eq({"ref": "SCENE_PLACE"}, "시장"),
            "phases": {"on": ph},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": [
                _sig("ledger", "좌판 주인이 '원본 기록은 성당 보관실에 있다'고 중얼거린다",
                     list(exposes), observable)]}]}


def aware_tpl():
    return {"version": 1, "spawn_when": eq({"ref": "COGNITION_AWARENESS", "holder": RF.SESSION_PC, "info": "S40"},
                                           "CONTENT"),
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": []}]}


# ── 컴파일러 ─────────────────────────────────────────────────

def test_phase_guide_and_relevance_compile_only_when_authored():
    tpl = arrive_tpl(guide="좌판 주인은 기록 위치를 흘리지만 이유는 말하지 않는다.")
    tpl["relevance_places"] = ["시장"]
    res = dc.compile_scenario(ev_raw({"a": tpl}), "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    ct = res.definition["event_templates"]["a"]
    assert ct["phases"]["on"]["guide"].startswith("좌판 주인")
    assert ct["relevance_places"] == ["시장"]
    plain = dc.compile_scenario(ev_raw({"a": arrive_tpl()}), "t").definition["event_templates"]["a"]
    assert "guide" not in plain["phases"]["on"] and "relevance_places" not in plain


def test_invalid_guide_relevance_and_exposes_are_errors():
    bad_guide = arrive_tpl(guide="  ")
    assert "EVENT_PHASE_GUIDE_INVALID" in codes({"a": bad_guide})
    rel = arrive_tpl()
    rel["relevance_places"] = ["없는곳"]
    assert "EVENT_RELEVANCE_INVALID" in codes({"a": rel})
    rel_slot = arrive_tpl()
    rel_slot["relevance_places"] = ["$slot:who"]
    rel_slot["slots"] = {"who": {"type": "ACTOR", "candidates": [KANG]}}
    assert "EVENT_RELEVANCE_INVALID" in codes({"a": rel_slot})
    assert "STEP_REF_UNKNOWN" in codes({"a": arrive_tpl(exposes=("없는정보",))})
    assert "STEP_PARAMS_INVALID" in codes({"a": arrive_tpl(exposes=())})


def test_exposed_info_requires_public_safe_summary():
    raw = ev_raw({})
    D = dc.compile_scenario(raw, "t").definition
    inline = next(i for i, r in D["information_definitions"].items() if r["kind"] == "NPC_INLINE_SECRET")
    assert "SIGNAL_EXPOSES_SUMMARY_REQUIRED" in codes({"a": arrive_tpl(exposes=(inline,))})
    nested = arrive_tpl()
    nested["transition_cases"][0]["steps"] = [{"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": 5,
                                               "effect": _sig("x", "소리", [inline])}]
    assert "SIGNAL_EXPOSES_SUMMARY_REQUIRED" in codes({"a": nested})


def test_world_start_frame_rejects_random_place_and_time_slots():
    raw = dyn_raw()
    raw["start_points"] = ["항구", "시장"]
    raw["intro_times"] = ["새벽", "밤"]
    raw["start_frames"][0]["slots"] = {"장소": "place:start"}
    res = dc.compile_scenario(raw, "t")
    assert "WORLD_START_SLOT_RANDOM" in [d.code for d in res.errors]
    raw["start_frames"][0]["slots"] = {"장소": "항구", "때": "list:intro_times"}
    raw["start_frames"][0]["facts"] = {"location": "{장소}", "time": "{때}"}
    assert "WORLD_START_SLOT_RANDOM" in [d.code for d in dc.compile_scenario(raw, "t").errors]
    raw["start_frames"][0]["slots"] = {"장소": "항구", "때": "밤"}
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    # world_start 없는 틀은 무작위 슬롯을 그대로 쓴다(LEGACY 의미 불변)
    raw["start_frames"][1]["slots"] = {"장소": "place:start", "때": "list:intro_times"}
    raw["start_frames"][1]["facts"] = {"location": "{장소}", "time": "{때}"}
    assert dc.compile_scenario(raw, "t").ok


# ── R-1: 신호 노출 → PC 인지 → 인지 조건 사건 ───────────────────

async def test_signal_exposure_reaches_pc_cognition_and_wakes_awareness_event(drig):
    r = drig
    r.make(templates={"arrive": arrive_tpl(), "knows": aware_tpl()})
    r.prov.routes["extraction"] = lambda kw: extraction(
        {"info_exposed": [obs_key(r.prov, "X", "원본 기록 위치") or "NONE"]})
    await turn(r)
    ext = r.prov.prompts["extraction"][-1][0]
    assert "원본 기록 위치" in ext and "성당 제3보관실" not in ext           # 명제 전문은 추출층위에 없음
    rec = CG.record_of(r.sess, CG.actor_holder(PC), "S40")
    assert rec["awareness"] == CG.CONTENT and rec.get("belief") is None    # 노출 ≠ 믿음
    tids = {i["template_id"] for i in r.sess.event_instances.values()}
    assert "knows" in tids                                                  # PC 인지 조건 사건이 깨어남


async def test_unreported_exposure_does_not_grant_cognition(drig):
    r = drig
    r.make(templates={"arrive": arrive_tpl(), "knows": aware_tpl()})
    r.prov.routes["extraction"] = [extraction({"info_exposed": []})]
    await turn(r)
    assert CG.record_of(r.sess, CG.actor_holder(PC), "S40") is None
    assert "knows" not in {i["template_id"] for i in r.sess.event_instances.values()}


async def test_unobservable_signal_offers_no_exposure_candidate(drig):
    r = drig
    r.make(templates={"arrive": arrive_tpl(observable=False)})
    await turn(r)
    assert "원본 기록 위치" not in r.prov.prompts["extraction"][-1][0]


async def test_guide_is_not_sent_to_narration_or_extraction(drig):
    r = drig
    r.make(templates={"arrive": arrive_tpl(guide="GM전용지침-비공개")})
    await turn(r)
    for layer in ("narration", "extraction", "judgment"):
        assert all("GM전용지침" not in p[0] for p in r.prov.prompts.get(layer, []))


# ── R-5: 도착·시작 시 보이는 출구 ─────────────────────────────

def _restrict(raw):
    for e in raw["movement_edges"]:
        if e["id"] == "e_mc":
            e["knowledge_visibility"] = "RESTRICTED"


async def test_arrival_reveals_local_exits_but_not_restricted(drig):
    r = drig
    r.make()
    await turn(r)
    know = SP.actor_knowledge(r.sess, PC)
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")
    assert SP.knows_edge(know, "e_hm#rev")                                  # 돌아가는 길이 보인다
    assert SP.knows_edge(know, "e_mc#fwd") and SP.knows_place(know, "성당")
    assert know["edges"]["e_mc#fwd"]["acquisition_sources"] == ["LOCAL_VIEW"]


async def test_restricted_exit_stays_unknown_on_arrival(drig):
    r = drig
    r.make(raw_patch=_restrict)
    await turn(r)
    know = SP.actor_knowledge(r.sess, PC)
    assert not SP.knows_edge(know, "e_mc#fwd")


async def test_genesis_reveals_start_place_exits_except_secret(rig):
    r = rig
    s = setup_session(r)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    know = SP.actor_knowledge(s, f"pc_{list(s.players)[0]}")
    for eid in ("e_hm#fwd", "e_hh#fwd", "e_hs#fwd"):
        assert SP.knows_edge(know, eid)
    assert not SP.knows_edge(know, "e_secret#fwd")
    assert SP.knows_place(know, "산복도로") and SP.knows_place(know, "해안")
    assert know["edges"]["e_hm#fwd"]["acquisition_sources"] == ["SEED"]     # seed가 이미 준 것은 덮지 않음
