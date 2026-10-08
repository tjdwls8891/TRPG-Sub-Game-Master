# -*- coding: utf-8 -*-
"""H06 R2-06 — TRANSIT은 DYNAMIC 정본 공간 상태 · 플레이 가능한 이동 공간은 일반 PlaceId.

경로 항구 → 산복도로 → 성당에서 산복도로는 저작된 일반 장소(‘골목’ 역할)다. 테스트용 raw 패치만 쓰며
시나리오 파일은 바꾸지 않는다.
"""
from __future__ import annotations

import pytest

import core
from core import player_world as PW
from core import spatial as SP
from tests.policy.test_h04b_events_progression import K, eq
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, drig, extraction, judgment, rig, turn)

pytestmark = pytest.mark.policy

ROUTE_B = ("e_hh#fwd", "e_hc#fwd")                 # 항구 →(8분) 산복도로 →(4분) 성당
BRANCH = "e_sh#fwd"                                # 산복도로 → 해안(테스트 raw 패치)


def _branch(raw):
    raw["movement_edges"].append({"id": "e_sh", "from": "산복도로", "to": "해안", "distance_m": 400})


def _signal_step(sid, cue, observable):
    return {"step_id": sid, "type": "EMIT_SIGNAL", "signal_id": sid, "cue": cue,
            "interrupt_policy": "IF_OBSERVABLE", "observable_when": observable}


def alley_tpl():
    """산복도로(일반 장소)에 장면이 도착하면 즉시 관찰 사건."""
    return {"version": 1, "spawn_when": eq({"ref": "SCENE_PLACE"}, "산복도로"),
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": [
                _signal_step("alley_noise", "골목 안쪽에서 유리 깨지는 소리가 난다",
                             eq({"ref": "SCENE_PLACE"}, "산복도로"))]}]}


def midway_tpl(after=3, extra_steps=()):
    """출발 직후(구간 안) after분에 관찰 가능한 사건 — 아직 어떤 장소에도 닿지 않았다."""
    return {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "LOCKED"),
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on", "steps": [
                {"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": after,
                 "effect": _signal_step("dog", "길 위에서 들개 떼가 앞을 가로막는다", {"const": True})},
                *extra_steps]}]}


def _spawn(r, D):
    """정의상 지금 성립하는 사건을 정본에 미리 둔다(초기화 전수 검사와 같은 효과)."""
    res = core.world_progression.project_world_progression(
        D, r.sess, K, narrative_seed=r.sess.narrative_seed or r.sess.session_id,
        outer_origin="INITIALIZATION", initial_full_scan=True)
    for f in core.world_state.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(r.sess, f, res.projected_world.domain(f))


async def _go(r, place, text=None):
    r.prov.routes["judgment"] = judgment(place)
    return await turn(r, text or f"{place}(으)로 간다")


# ── 중간 장소(일반 PlaceId) ───────────────────────────────────

async def test_intermediate_place_consumed_normally_on_route(drig):
    r = drig
    r.make(edges=ROUTE_B)
    await _go(r, "성당")
    assert SP.placement_of(r.sess, PC) == SP.at_place("성당")
    assert SP.actor_knowledge(r.sess, PC)["places"]["산복도로"]["visited"] is True     # 정상 중간 노드로 소비
    assert r.sess.world_clock["minute_index"] == K + 12


async def test_event_on_arrival_at_intermediate_place_stops_at_place_and_next_turn_branches(drig):
    r = drig
    D = r.make(templates={"alley": alley_tpl()}, edges=ROUTE_B + (BRANCH,), raw_patch=_branch)
    r.sess.spatial_knowledge[PC]["places"]["해안"] = {"knows_name": True, "visited": False,
                                                     "rough_location": None, "acquisitions": []}
    await _go(r, "성당")
    assert SP.placement_of(r.sess, PC) == SP.at_place("산복도로")          # AT_PLACE(골목)에서 중단
    assert r.sess.world_clock["minute_index"] == K + 8
    assert "유리 깨지는 소리" in r.prov.prompts["narration"][-1][0]
    ctx = PW.session_world_context(r.sess)
    assert ctx.dynamic
    await _go(r, "해안")                                                  # 다음 턴은 골목에서 시작 — 다른 장소로
    assert SP.placement_of(r.sess, PC) == SP.at_place("해안")
    assert "현재 위치: 산복도로" in r.prov.prompts["judgment"][-1][0]


# ── 진짜 구간 안 중단 · 재개 ──────────────────────────────────

async def test_midway_event_keeps_transit_dynamic_without_snap(drig):
    r = drig
    D = r.make(templates={"dog": midway_tpl()}, edges=ROUTE_B)
    _spawn(r, D)
    await _go(r, "산복도로")
    pl = SP.placement_of(r.sess, PC)
    assert pl["kind"] == SP.TRANSIT and pl["from_place"] == "항구" and pl["to_place"] == "산복도로"   # snap 없음
    assert r.sess.world_clock["minute_index"] == K + 3
    assert PW.session_world_context(r.sess).dynamic                       # TRANSIT ≠ LEGACY
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 살핀다")
    jp = r.prov.prompts["judgment"][-1][0]
    assert "항구에서 산복도로(으)로 가는 길 위" in jp and "8분 중 3분" in jp     # 판단층위가 TRANSIT 문맥을 안다
    assert SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT              # 0분 턴도 구간 안에 그대로


async def test_continue_from_transit_resumes_remaining_without_double_time(drig):
    r = drig
    D = r.make(templates={"dog": midway_tpl()}, edges=ROUTE_B)
    _spawn(r, D)
    await _go(r, "성당")
    assert SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT and r.sess.world_clock["minute_index"] == K + 3
    await _go(r, "성당", "계속 간다")
    assert SP.placement_of(r.sess, PC) == SP.at_place("성당")
    assert r.sess.world_clock["minute_index"] == K + 12                   # 3 + 남은 5 + 4 = 처음부터 12분
    assert "들개 떼" not in r.prov.prompts["narration"][-1][0]               # 이미 관찰한 사건 재발생 없음


async def test_stale_route_after_world_change_is_not_forced(drig):
    r = drig
    bar = {"step_id": "bar", "type": "SCHEDULE_EFFECT", "after_minutes": 2,
           "effect": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "BARRED"}}
    D = r.make(templates={"dog": midway_tpl(after=2, extra_steps=(bar,))}, at="시장", edges=("e_mc#fwd",),
               extra_world={"access_control_state": {"door_1": {"state": "OPEN"}}})
    _spawn(r, D)
    await _go(r, "성당")
    pl = SP.placement_of(r.sess, PC)
    assert pl["kind"] == SP.TRANSIT and pl["edge_id"] == "e_mc#fwd"
    assert r.sess.access_control_state["door_1"]["state"] == "BARRED"     # 이동 중 세계 변화
    clock = dict(r.sess.world_clock)
    await _go(r, "성당", "계속 간다")
    assert "가는 길 위" in r.prov.prompts["judgment"][-1][0]                # DYNAMIC 턴으로 처리됐다(공허한 통과 아님)
    assert SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT              # 무효 경로 강행 없음 · snap 없음
    assert r.sess.world_clock == clock


async def test_waiting_past_edge_end_in_transit_is_rejected(drig):
    r = drig
    D = r.make(templates={"dog": midway_tpl()}, edges=ROUTE_B)
    _spawn(r, D)
    await _go(r, "산복도로")
    clock = dict(r.sess.world_clock)
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=30, activity="WAIT")
    await turn(r, "길 위에서 30분 기다린다")
    assert "가는 길 위" in r.prov.prompts["judgment"][-1][0]                # DYNAMIC 턴으로 처리됐다
    assert SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT and r.sess.world_clock == clock
