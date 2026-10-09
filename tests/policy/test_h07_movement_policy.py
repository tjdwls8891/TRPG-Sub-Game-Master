# -*- coding: utf-8 -*-
"""H07 이동 지식 정책(T-7) — PC·NPC 공통 원칙(사용자 결정 2026-10-09, H04 R55 의도적 supersede).

  · PUBLIC/LOCAL: 개인 지식에 없어도 일반 이동 그래프로 출발·이동할 수 있다.
  · RESTRICTED/SECRET: 지식이 없으면 자율 경로가 쓰지 않는다. 알아도 통과는 실제 접근이 판정한다.
  · 아는 길이 있으면 그것이 먼저다(지식 = 경로 선택 품질).
  · 저작된 명시 사건 route: 연속성·실제 접근만 검증하고 행위자 지식으로 거부하지 않는다.
"""
from __future__ import annotations

import copy

import pytest

from core import movement as MV
from core import player_world as PW
from core import spatial as SP
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K, SIMPLE, at, defn, ev_raw, op, run, world
from tests.policy.test_h05a_intent_planning import PC, go, make_session, manifest, plan

pytestmark = pytest.mark.policy

PLACES = ("항구", "시장", "성당", "산복도로", "외딴곳")


def _raw():
    raw = ev_raw()
    for e in raw["movement_edges"]:
        if e["id"] == "e_secret":
            e["access_ref"] = "door_2"                    # 비밀 통로 끝의 잠긴 문(LOCKED → IMPASSABLE)
    raw["movement_edges"].append({"id": "e_rest", "from": "항구", "to": "외딴곳", "distance_m": 700,
                                  "knowledge_visibility": "RESTRICTED"})
    return raw


def _know(edges):
    return {"places": {}, "access": {}, "revision": 0,
            "edges": {e: dict({k: None for k in SP.EDGE_KNOWLEDGE_KEYS}, connection_known=True,
                              acquisition_sources=["SEED"]) for e in edges}}


def _tpl(dest, route=None):
    st = {"step_id": "go", "type": "START_MOVEMENT", "actor": KANG, "destination": dest, "mode": "walk"}
    if route:
        st["route"] = list(route)
    return dict(copy.deepcopy(SIMPLE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [st]}])


def _npc(dest, knows=(), route=None):
    D = defn({"go": _tpl(dest, route)}, **{k: v for k, v in _raw().items() if k == "movement_edges"})
    w = world(spatial_state=at(KANG, "항구"), spatial_knowledge={KANG: _know(knows)} if knows else {})
    r = run(D, w, K + 120, base_ops=[op("b", "ACCESS_STATE_SET", {"control_id": "door_1", "state": "BARRED"})])
    return r, r.projected_world.domain("spatial_state")["placements"][KANG]


def _pc(dest, knows=()):
    s, _ = make_session(_raw(), edges=knows, places=PLACES)
    return plan(s, go(manifest(s), dest))[0]


# ── 플레이어 ──────────────────────────────────────────────────

def test_pc_unknown_local_route_departs_and_arrives():
    p = _pc("산복도로")
    assert p.status == PW.PLAN_PLANNED and p.outcome["destination"] == "산복도로"
    assert SP.placement_of(p.projected_world, PC) == SP.at_place("산복도로")


def test_pc_unknown_restricted_route_is_not_used():
    p = _pc("외딴곳")
    assert p.status == PW.PLAN_REJECTED and p.reason == "NO_KNOWN_ROUTE" and p.route_options == ()


def test_pc_known_restricted_route_is_used():
    p = _pc("외딴곳", knows=("e_rest#fwd",))
    assert p.status == PW.PLAN_PLANNED


def test_pc_known_secret_route_still_needs_actual_access():
    p = _pc("성당", knows=("e_secret#fwd",))
    assert p.outcome["route_signature"] == ["e_secret#fwd"]               # 아는 비밀 통로를 골랐지만
    assert p.outcome["kind"] == "INTERRUPTED_BLOCKED" and p.outcome["final_place"] == "항구"   # 잠긴 문 — 지식 ≠ 허가
    assert SP.placement_of(p.projected_world, PC) == SP.at_place("항구")


def test_pc_familiar_route_is_preferred_over_public_graph():
    s, D = make_session(_raw(), edges=("e_hh#fwd", "e_hc#fwd"), places=PLACES)
    knowledge = SP.actor_knowledge(s, PC)
    pl = MV.plan_known_routes(D, knowledge, "항구", "성당", "walk", public_graph=True)
    assert [c.edge_ids for c in pl.candidates] == [("e_hh#fwd", "e_hc#fwd")]
    pub = MV.plan_known_routes(D, {}, "항구", "성당", "walk", public_graph=True)
    assert len(pub.candidates) > 1 and all("e_secret#fwd" not in c.edge_ids for c in pub.candidates)
    assert MV.plan_known_routes(D, {}, "항구", "성당", "walk").status == MV.NO_KNOWN_ROUTE   # 순수 지식 질의


# ── NPC ──────────────────────────────────────────────────────

def test_npc_unknown_local_route_moves():
    r, pl = _npc("산복도로")
    assert pl == SP.at_place("산복도로")


def test_npc_unknown_restricted_route_is_not_used():
    r, pl = _npc("외딴곳")
    assert pl == SP.at_place("항구") and any(d["code"] == "MOVEMENT_NO_ROUTE" for d in r.diagnostics)


def test_npc_known_restricted_route_is_used():
    r, pl = _npc("외딴곳", knows=("e_rest#fwd",))
    assert pl == SP.at_place("외딴곳")


def test_npc_known_secret_route_blocked_by_access_without_detour():
    r, pl = _npc("성당", knows=("e_secret#fwd",))
    assert pl == SP.at_place("항구")                                      # 잠긴 문에서 멈춤 — 다른 길로 우회 없음
    assert any(d["code"] == "MOVEMENT_STOPPED" for d in r.diagnostics)
    k = SP.actor_knowledge(r.projected_world, KANG)
    assert k["access"]["door_2"]["observed_result"] == "IMPASSABLE"


def test_npc_explicit_event_route_without_knowledge_moves_when_continuous_and_accessible():
    r, pl = _npc("외딴곳", route=("e_rest#fwd",))
    assert pl == SP.at_place("외딴곳")                                    # 저작자가 큐레이션한 경로


def test_npc_explicit_event_route_still_checks_actual_access():
    r, pl = _npc("성당", route=("e_secret#fwd",))
    assert pl == SP.at_place("항구") and any(d["code"] == "MOVEMENT_STOPPED" for d in r.diagnostics)
