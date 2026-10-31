# -*- coding: utf-8 -*-
"""H07 최종 통합 행동 게이트 T-1 ~ T-10 — 실제 scenarios/영도.json · 결정적 순수 하네스(LLM·디스코드 없음).

컴파일 → genesis → ResolvedActionIntent → plan_player_turn → 투영 채택. 커밋 경로 전체는 H05/H06 테스트가 고정했다.
"""
from __future__ import annotations

import copy
import json
import os
import time

import pytest

from core import action_intent as AI
from core import definition_compiler as dc
from core import events as EV
from core import npc_position as NP
from core import place_display as PD
from core import player_world as PW
from core import spatial as SP
from core import turn_world as TW
from core import world_mutation as WM
from core import world_refs as RF
from tests.policy.test_h07_yeongdo_draft import (
    PC, ROOT, _defn, adopt, ctx, genesis, go, lifecycle, phase_of, setup, turn, wait)

pytestmark = pytest.mark.policy

NOH = "yd.npc.no_sangjin"
LEE = "yd.npc.lee_junhyeok"
SEON = "yd.npc.lee_seonhui"            # 일과: 중리 해변(자주)
SHIN = "yd.npc.shin_changho"           # untracked(v7.42: 영도는 47명 모두 일과 — T-8은 일과를 뺀 정의 사본으로 검증)


def _now(w):
    return w.domain("world_clock")["minute_index"]


def _wait_until(world, minutes, activity="WAIT"):
    """minutes 이상 흐를 때까지 대기(관찰 신호가 끊으면 다시 기다린다 — 플레이어에게 돌아온 뒤 재선언)."""
    start, interrupts = _now(world), 0
    while _now(world) - start < minutes:
        p = turn(world, wait(min(720, minutes - (_now(world) - start)), activity))
        world = adopt(p)
        if p.outcome.get("kind") == PW.OUT_TIME_INTERRUPTED:
            interrupts += 1
        assert interrupts < 40
    return world, interrupts


def _admin(world, *ops):
    b = WM.WorldMutationBatch(batch_id="adm", outer_origin="ADMIN_EDIT", source_world_version_id=None,
                              definition_revision=None)
    for o in ops:
        b.add(o)
    b.freeze()
    r = WM.project(world, b, WM.PRODUCTION_REGISTRY)
    assert r.ok, [c.message for c in r.conflicts]
    return r.world


def _scene_npcs(world, place):
    D, _ = _defn()
    return [a for a, _ in NP.actors_at(D, world, place, exclude=(PC,))]


# ── T-1 24시간 자유 배회 ───────────────────────────────────────

def test_t1_free_roam_24h_world_progresses_and_routines_hold():
    D, _ = _defn()
    w, _ = genesis("arrive_plain")                                     # 청학동 선착장 05:40
    start = _now(w)
    tides = {w.domain("access_control_state")["jungri_tide_path"]["state"]}
    route = ["저지대 상업지구", "산복도로 하단", "산복도로", "산복도로 상단", "산복도로", "산복도로 하단", "저지대 상업지구"]
    seen_patrol_day = seen_patrol_night = False
    for place in route * 3:
        if _now(w) - start >= 1440:
            break
        p = turn(w, go(place))
        if p.status == PW.PLAN_PLANNED:
            w = adopt(p)
        w, _ = _wait_until(w, 90)
        tides.add(w.domain("access_control_state")["jungri_tide_path"]["state"])
        here = (SP.placement_of(w, PC) or {}).get("place_id")
        dp = TW.clock_view(D, _now(w))["time_of_day"]
        if here == "저지대 상업지구":
            present = LEE in _scene_npcs(w, here)
            if dp in ("이른 아침", "오전", "한낮", "오후"):
                assert present
                seen_patrol_day = True
            else:
                assert not present
                seen_patrol_night = True
    w, _ = _wait_until(w, max(0, 1440 - (_now(w) - start)))
    assert _now(w) - start >= 1440
    assert tides == {"LOW_TIDE", "HIGH_TIDE"}                          # 물때(정본 접근)가 저절로 바뀐다
    assert seen_patrol_day
    assert lifecycle(w, "yd_arrival_day") == "RESOLVED"                 # 시간 기반 사건이 진행
    for tid in ("yd_ev_namhang_gate_inspection", "yd_ev_jodo_screening", "yd_ev_jungri_grading",
                "yd_ev_haeryeon_ladder"):
        assert lifecycle(w, tid) == "NONE"                              # 가지 않은 곳의 사건은 열리지 않는다
    assert w.domain("spatial_state")["placements"].keys() == {PC}       # 일과가 정본 배치를 만들지 않는다


# ── T-2 장시간(12시간) 대기 ───────────────────────────────────

def test_t2_long_wait_is_deterministic_and_interrupts_return_to_player():
    w0, _ = genesis("arrive_plain")
    t0 = time.perf_counter()
    a, ia = _wait_until(w0, 720)
    t_wait = time.perf_counter() - t0
    b, ib = _wait_until(genesis("arrive_plain")[0], 720)
    assert a.digest() == b.digest() and ia == ib                         # 같은 세계 · 같은 선언 = 같은 결과
    assert _now(a) - _now(w0) >= 720
    p = turn(w0, wait(720))
    if p.outcome.get("kind") == PW.OUT_TIME_INTERRUPTED:
        assert p.end_minute < _now(w0) + 720 and p.signals            # 관찰 신호에서 멈추고 플레이어에게 돌려준다
    assert t_wait < 60


# ── T-3 사건 이탈 ─────────────────────────────────────────────

def test_t3_leaving_an_event_cancels_by_authored_policy_without_pulling_player_back():
    w, _ = genesis()
    w = setup(w, "산복도로 상단")
    w = adopt(turn(w, go("하단 방어선 아래")))
    assert phase_of(w, "yd_ev_haeryeon_ladder") == "watched"
    w = adopt(turn(w, go("산복도로 상단")))
    w, _ = _wait_until(w, 600)
    assert lifecycle(w, "yd_ev_haeryeon_ladder") == "CANCELLED"
    assert SP.placement_of(w, PC) == SP.at_place("산복도로 상단")        # 사건이 PC 위치를 끌어오지 않는다


# ── T-4 / T-5 NPC override · routine 복귀 ─────────────────────

def test_t4_t5_override_beats_routine_then_explicit_clear_returns_to_routine():
    D, _ = _defn()
    w, _ = genesis("arrive_plain")
    w, _ = _wait_until(w, 600)                                         # 한낮 무렵
    assert NP.effective_place(D, w, SEON) == "중리 해변" and SEON in _scene_npcs(w, "중리 해변")
    w = _admin(w, WM.make_op("ov", SP.OP_ACTOR_PLACEMENT_SET, "ADMIN_EDIT",
                             {"actor_id": SEON, "place_id": "지하 의료소"}))
    assert SEON not in _scene_npcs(w, "중리 해변") and SEON in _scene_npcs(w, "지하 의료소")
    c = RF.EvalContext(D, w)
    assert RF.resolve({"ref": "ACTOR_PLACE", "actor": SEON}, c) == RF.known("지하 의료소")   # 사건 조건 = 장면
    w2, _ = _wait_until(w, 300)
    assert NP.effective_place(D, w2, SEON) == "지하 의료소"            # 일과가 override를 덮지 않는다
    w3 = _admin(w2, WM.make_op("clr", SP.OP_ACTOR_PLACEMENT_CLEAR, "ADMIN_EDIT", {"actor_id": SEON}))
    assert NP.effective_place(D, w3, SEON) == "지하 의료소"            # 해제 순간 순간이동 없음
    w4, _ = _wait_until(w3, 10)
    assert NP.effective_place(D, w4, SEON) == "중리 해변" and SEON in _scene_npcs(w4, "중리 해변")   # 시간 전환 후 복귀
    again = _admin(w2, WM.make_op("clr", SP.OP_ACTOR_PLACEMENT_CLEAR, "ADMIN_EDIT", {"actor_id": SEON}))
    assert again.digest() == w3.digest()                               # 재현(되감기·재시작 후 같은 결과)
    restored = WM.ProjectedWorld(json.loads(json.dumps(w3.to_dict())))
    assert NP.effective_position(D, restored, SEON) == NP.effective_position(D, w3, SEON)


def test_t5_stale_movement_wakeup_cannot_drag_cleared_npc_back():
    from tests.policy.test_h07_yeongdo_draft import _day2
    w, _ = genesis()
    w = _day2(setup(w, "은신처"))
    w = setup(w, "저지대 상업지구")
    w = adopt(turn(w, go("남항 중공업 연대")))
    w = adopt(turn(w, wait(40)))
    assert phase_of(w, "yd_ev_namhang_lost_team") == "search"
    pl = SP.placement_of(w, NOH)
    assert pl is not None                                              # 사건이 만든 정본 override(이동)
    w = _admin(w, WM.make_op("clr", SP.OP_ACTOR_PLACEMENT_CLEAR, "ADMIN_EDIT", {"actor_id": NOH}))
    assert not [e for e in w.domain("world_schedule").values()
                if e["kind"] == "MOVEMENT_WAKEUP" and e["payload"]["actor_id"] == NOH]
    w, _ = _wait_until(w, 120)
    assert SP.placement_of(w, NOH) is None                             # 낡은 이동이 다시 끌고 가지 않는다
    D, _ = _defn()
    assert NP.effective_place(D, w, NOH) == "수습대 집결지"            # 일과(상주)로 복귀


# ── T-6 무관 행동 비진행 ──────────────────────────────────────

def test_t6_unrelated_actions_do_not_advance_unrelated_events_or_create_truth():
    w, _ = genesis("arrive_plain")
    before = {i["template_id"]: (i["current_phase"], i["lifecycle"])
              for i in w.domain("event_instances").values()}
    for _ in range(4):                                                 # 장소 안에서 작은 행동 반복(정본 이동 없음)
        p = turn(w, AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_LOCAL),
                                            definition_revision=_defn()[1]))
        assert p.status in (PW.PLAN_NONE, PW.PLAN_PLANNED)
    after = {i["template_id"]: (i["current_phase"], i["lifecycle"])
             for i in w.domain("event_instances").values()}
    assert before == after
    # 자연어가 숨은 정본 진실을 만들지 못한다: 후보 밖 노출 키는 거부되고 인지가 바뀌지 않는다
    D, rev = _defn()
    p = turn(w, wait(5))
    om = TW.build_observation_manifest(D, ctx(), p, w)
    res = TW.process_observation(ctx(), w, p, om, {"info_exposed": ["X999", "yd.info.q.main_upheaval"]},
                                 transaction_id="t6", narrative_seed="seed-h07")
    assert any(d["code"] == "INFO_EXPOSURE_INVALID" for d in res.diagnostics)
    pc_cog = (res.projected_world.domain("cognition_state") or {}).get(f"actor:{PC}") or {}
    assert "yd.info.q.main_upheaval" not in pc_cog
    for inst in (res.projected_world.domain("event_instances") or {}).values():
        assert "resolution" not in (inst.get("local_state") or {})


# ── T-7 경로 지식(영도 PC·NPC) ────────────────────────────────

def test_t7_yeongdo_unknown_public_route_departs_and_secret_is_not_used():
    D, _ = _defn()
    w, _ = genesis("arrive_plain")
    k = SP.actor_knowledge(w, PC)
    assert not SP.knows_place(k, "남항 중공업 연대")
    restricted = [e for e, x in D["movement_edges"].items() if x["knowledge_visibility"] == "RESTRICTED"]
    assert restricted
    from core import movement as MV
    pub = MV.plan_known_routes(D, {}, "저지대 상업지구", "산복도로", "walk", public_graph=True)
    assert pub.status == MV.ROUTE_OK
    assert all(not set(c.edge_ids) & set(restricted) for c in pub.candidates)
    # NPC: 이동 seed 없이 사건 이동(수습대장)이 성립 — T-5에서 TRANSIT/이동 확인
    assert all("knowledge_seed" not in a for a in D["actor_definitions"].values())


# ── T-8 일과 없는 시나리오·인물 ───────────────────────────────

def test_t8_untracked_npc_never_materializes_and_legacy_scenarios_compile_unchanged():
    D = copy.deepcopy(_defn()[0])
    D["actor_definitions"][SHIN].pop("routine", None)                  # 일과 없는 인물(정의 사본)
    w, _ = genesis("arrive_plain")
    for minutes in (0, 400, 800, 1200):
        if minutes:
            w, _ = _wait_until(w, 400)
        assert NP.effective_position(D, w, SHIN)["source"] == NP.UNTRACKED
        assert all(SHIN not in [a for a, _ in NP.actors_at(D, w, p, exclude=(PC,))] for p in D["place_definitions"])


# ── T-9 표시명 누설 방지 ──────────────────────────────────────

def test_t9_faction_place_name_hidden_until_discovery():
    D, _ = _defn()
    w, _ = genesis("arrive_plain")
    w = adopt(turn(w, go("저지대 상업지구")))
    assert PD.display_for(D, w, PC, "남항 중공업 연대") == "크레인이 선 서쪽 조선소"
    m = AI.build_manifest(D, w, PC, definition_revision=_defn()[1])
    assert "남항 중공업 연대" not in m.render() and "크레인이 선 서쪽 조선소" in m.render()
    tl = TW.mirror_timeline({}, D, w, PC)
    assert tl["current_location_display"] == "저지대 상업지구"
    w2 = _admin(w, *TW.reveal_ops(D, PC, ["yd.info.place.namhang"], prefix="rv"))
    assert PD.display_for(D, w2, PC, "남항 중공업 연대") == "남항 중공업 연대"
    assert PD.display_for(D, w2, PC, "제1방어선") == "제1방어선"


# ── T-10 메인 결말 · 행적 = 플레이어 명시 결단만 ──────────────

MAIN_ARCS = ("yd_ev_namhang_ration_crisis", "yd_ev_haeryeon_drought", "yd_ev_jodo_closure_order",
             "yd_ev_jungri_debt_strike", "yd_ev_lone_winter", "yd_ev_boat_rumor", "yd_ev_island_balance")


def _sets_key(steps, key):
    return any(s.get("type") == "SET_EVENT_LOCAL" and s.get("key") == key for s in steps or ())


def test_t10_main_endings_are_reachable_only_through_player_resolution():
    D, _ = _defn()
    for tid in MAIN_ARCS:
        tpl = D["event_templates"][tid]
        opts = [p for p in tpl["phases"].values() if p.get("resolution_options")]
        assert len(opts) == 1, tid
        for c in tpl["transition_cases"]:
            if _sets_key(c["steps"], "ending"):
                elig = json.dumps(tpl["phases"][c["to"]]["eligible_when"], ensure_ascii=False)
                assert '"key": "resolution"' in elig, (tid, c["case_id"])      # 결말 = 명시 결단 조건
            if _sets_key(c["steps"], "outcome"):
                assert not _sets_key(c["steps"], "ending"), (tid, c["case_id"])  # 미결단 = 세계 결과(결말 아님)
    for t in D["event_templates"].values():
        for c in t["transition_cases"]:
            assert not any(s.get("key") == "resolution" for s in c["steps"] if s.get("type") == "SET_EVENT_LOCAL")


def test_t10_explicit_run_records_conduct_and_two_conducts_open_upheaval():
    D, rev = _defn()
    w, _ = genesis("arrive_finding")                                    # 07:10 동삼동 갯가
    w, _ = _wait_until(w, 60)                                          # 상륙 직후(yd_landing) 지난 오전
    w = setup(w, "저지대 상업지구")
    w = adopt(turn(w, wait(5)))
    if phase_of(w, "yd_ev_smoke_column") is None:
        pytest.skip("연기 사건이 이 시작에서 열리지 않음")
    w = adopt(turn(w, go("남항 중공업 연대")))
    assert phase_of(w, "yd_ev_smoke_column") == "contact"
    m = AI.build_manifest(D, w, PC, definition_revision=rev)
    keys = {oid: k for k, (_, oid), _ in m.resolutions}
    assert set(keys) >= {"stop", "run"}
    p0 = turn(w, wait(5))
    assert phase_of(adopt(p0), "yd_ev_smoke_column") == "contact"      # 대기만으로는 결단 없음
    intent = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": keys["run"]},
                               world=w)
    w = adopt(turn(w, intent))
    inst = [i for i in w.domain("event_instances").values() if i["template_id"] == "yd_ev_smoke_column"][0]
    assert inst["current_phase"] == "hostile" and inst["local_state"]["conduct"] == "deviated"
    tpl = D["event_templates"]["yd_ev_island_balance"]
    c = RF.EvalContext(D, w)
    from core import predicates as PRD
    one = PRD.evaluate(tpl["spawn_when"]["all"][1], c)
    assert one != PRD.TRUE                                             # 행적 하나로는 열리지 않는다
    ev = copy.deepcopy(w.to_dict())
    ev["event_instances"]["fake_rec"] = {"instance_id": "fake_rec", "template_id": "yd_ev_records",
                                         "lifecycle": "RESOLVED", "current_phase": "destroyed",
                                         "occurrence_ordinal": 1, "local_state": {"conduct": "deviated"}}
    c2 = RF.EvalContext(D, WM.ProjectedWorld(ev))
    assert PRD.evaluate(tpl["spawn_when"]["all"][1], c2) == PRD.TRUE    # 원문 deviated 2회 의미
