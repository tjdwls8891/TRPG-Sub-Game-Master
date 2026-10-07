# -*- coding: utf-8 -*-
"""H05 R3 — DYNAMIC 판단·지시·묘사 문맥의 현재 장면 NPC = 정본 배치 + H04 CurrentNpcProjection.

  · 현재 위치 권위는 spatial_state.placements 뿐(place.npcs·session.npcs는 현재 존재 권위가 아니다).
  · 기록 없음(untracked)·TRANSIT은 장면에 있다고 추론하지 않는다(H04 계약).
  · 투영은 기존 core.npc_projection.project_npc를 재사용(읽기 전용) — 숨은 인지·명제는 싣지 않는다.
  · LEGACY 경로 불변.
"""
from __future__ import annotations

import copy
import re

import pytest

import core
from core import spatial as SP
from core import world_mutation as WM
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K, eq
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, _npc_knows_world, drig, extraction, judgment, rig, turn)

pytestmark = pytest.mark.policy

SECRET_TEXTS = ("원본 기록은 성당 제3보관실", "원본 기록 위치")


def _judg_npcs(r, i=0):
    """판단 프롬프트의 NPC 섹션(다음 빈 줄까지)."""
    p = r.prov.prompts["judgment"][i][0]
    m = re.search(r"\[(?:세션 등장 NPC|현재 장면 NPC[^\]]*)\]\n(.*?)(?:\n\n|$)", p, re.S)
    return m.group(0) if m else ""


def _scene_block(text):
    i = text.find("[현재 장면 인물")
    if i < 0:
        return ""
    j = text.find("\n\n", i)
    return text[i:j if j > 0 else None]


def _world(kang=None, pc_at="항구", **extra):
    pls = {PC: SP.at_place(pc_at)}
    if kang is not None:
        pls[KANG] = kang
    w = {"spatial_state": {"placements": pls, "scene_place": pc_at}}
    w.update(extra)
    return w


async def test_r3_canonical_placement_not_association_decides_scene_npc(drig):
    # 강지수의 저작 연고(places.시장.npcs) = 시장(A), 정본 배치 = 항구(B)
    r = drig
    r.make(at="항구", extra_world=_world(SP.at_place("항구"), "항구"))
    r.sess.npcs = {"강지수": {"name": "강지수"}}
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    assert "강지수" in _judg_npcs(r)
    assert "강지수" in _scene_block(r.prov.prompts["narration"][0][0])
    assert "강지수" in _scene_block(r.prov.prompts["logic"][0][0])


async def test_r3_association_scene_without_placement_shows_no_npc(drig):
    r = drig
    r.make(at="시장", extra_world=_world(SP.at_place("항구"), "시장"))   # 연고 A=시장에 PC, NPC는 B=항구
    r.sess.npcs = {"강지수": {"name": "강지수"}}
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    assert "강지수" not in _judg_npcs(r)                                  # session.npcs 전부를 현재 인물로 제시하지 않음
    narr = r.prov.prompts["narration"][0][0]
    blk = _scene_block(narr)
    assert blk and "강지수" not in blk
    assert "연관 인물: 강지수" not in narr                                 # 정적 연고를 현재 존재처럼 싣지 않음


async def test_r3_untracked_npc_never_inferred_present(drig):
    r = drig
    r.make(at="시장", extra_world=_world(None, "시장"))                    # 강지수 배치 기록 없음(추적 안 함)
    r.sess.npcs = {"강지수": {"name": "강지수"}}
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    assert "강지수" not in _judg_npcs(r)
    blk = _scene_block(r.prov.prompts["narration"][0][0])
    assert blk and "강지수" not in blk


async def test_r3_transit_npc_not_present_at_scene(drig):
    tr = SP.transit(movement_id="mv1", edge_id="e_hm#fwd", from_place="항구", to_place="시장",
                    edge_started_at_minute=K - 5, edge_expected_end_minute=K + 30)
    r = drig
    r.make(at="항구", extra_world=_world(tr, "항구"))
    r.sess.npcs = {"강지수": {"name": "강지수"}}
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    assert "강지수" not in _judg_npcs(r)
    blk = _scene_block(r.prov.prompts["narration"][0][0])
    assert blk and "강지수" not in blk


def _mover_tpl():
    return {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
            "phases": {"go": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "go", "steps": [
                {"step_id": "mv", "type": "SCHEDULE_EFFECT", "after_minutes": 3,
                 "effect": {"type": "SET_ACTOR_PLACEMENT", "actor": KANG, "place": "항구"}}]}]}


async def test_r3_event_moves_npc_and_next_turn_prompt_reflects_new_placement(drig):
    r = drig
    D = r.make(templates={"mv": _mover_tpl()}, at="항구", extra_world=_world(SP.at_place("시장"), "항구"))
    arm = core.world_progression.project_world_progression(
        D, r.sess, K, base_ops=[WM.make_op("arm", "ACCESS_STATE_SET", "TEST",
                                           {"control_id": "door_2", "state": "BARRED"})],
        narrative_seed=r.sess.narrative_seed or r.sess.session_id)
    for f in core.world_state.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(r.sess, f, copy.deepcopy(arm.projected_world.domain(f)))
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    await turn(r, "10분 기다린다")
    assert "강지수" not in _judg_npcs(r, 0)                                # 판단 시점엔 아직 시장
    assert SP.placement_of(r.sess, KANG) == SP.at_place("항구")            # 사건이 A→B 이동(정본)
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    assert "강지수" in _judg_npcs(r, 1)
    assert "강지수" in _scene_block(r.prov.prompts["narration"][-1][0])


def _role_tpl():
    return {"version": 1, "slots": {"watcher": {"type": "ACTOR", "candidates": ["강지수"]}},
            "spawn_when": {"const": False},
            "phases": {"lookout": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "lookout", "steps": []}]}


async def test_r3_event_role_and_status_reach_prompt_via_projection(drig):
    inst = {"instance_id": "evt_role", "template_id": "role", "template_version": 1,
            "bound_slots": {"watcher": KANG}, "lifecycle": "ACTIVE", "current_phase": "lookout",
            "phase_entered_at": K, "transition_ordinal": 0, "occurrence_ordinal": 1, "local_state": {},
            "history": []}
    r = drig
    r.make(templates={"role": _role_tpl()}, at="항구",
           extra_world=_world(SP.at_place("항구"), "항구", event_instances={"evt_role": inst}))
    r.sess.statuses = {"강지수": ["부상"]}
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 둘러본다")
    blk = _scene_block(r.prov.prompts["narration"][0][0])
    assert "강지수" in blk and "부상" in blk
    assert "watcher" in blk and "lookout" in blk                          # 현재 사건 역할(슬롯·국면)
    assert "evt_role" not in blk                                          # 내부 인스턴스 id 미노출


async def test_r3_projection_does_not_leak_hidden_cognition(drig):
    r = drig
    r.make(extra_world=_npc_knows_world())                                # 같은 장면 강지수가 S40 내용을 안다
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "강지수에게 말을 건다")
    narr = r.prov.prompts["narration"][0][0]
    assert "강지수" in _scene_block(narr)
    for p in (narr, r.prov.prompts["extraction"][0][0], r.prov.prompts["judgment"][0][0]):
        for t in SECRET_TEXTS:
            assert t not in p, t
        assert "S40" not in _scene_block(p)
    assert not re.search(r"X\d+=", r.prov.prompts["extraction"][0][0])


def test_r3_legacy_npc_prompt_unchanged():
    from cogs.gm import _build_judgment_user_prompt
    s = core.TRPGSession("s-legacy-r3", 1, 2, "x", {})
    s.npcs = {"강지수": {"name": "강지수"}, "김노인": {"name": "김노인"}}
    p = _build_judgment_user_prompt(s, "둘러본다", [], world_turn=None)
    assert "[세션 등장 NPC]\n강지수, 김노인" in p and "[현재 장면 NPC" not in p
    s.scenario_data = {"places": {"시장": {"npcs": [{"name": "강지수", "relation": "상인", "frequency": "상주"}]}}}
    s.world_timeline = {"current_location": "시장"}
    blk = core.places.build_place_block(s)
    assert "연관 인물: 강지수(상인·상주)" in blk and "[현재 장면 인물" not in blk
