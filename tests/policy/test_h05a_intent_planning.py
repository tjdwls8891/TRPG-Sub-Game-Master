# -*- coding: utf-8 -*-
"""H05-A — 구조화 행동 의도(ActionReferenceManifest·ResolvedActionIntent) + 플레이어 세계 계획.

순수 계획만 검사한다(정본 세션 불변). 자동 턴 배선은 test_h05c_*.
"""
from __future__ import annotations

import copy

import pytest

import core
from core import action_intent as AI
from core import actors as AR
from core import cognition as CG
from core import definition_compiler as dc
from core import player_world as PW
from core import spatial as SP
from core import world_clock as WC
from core import world_mutation as WM
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_h04b_events_progression import DOOR, K, eq, ev_raw

pytestmark = pytest.mark.policy

PC = f"pc_{PLAYER_UID}"
SEED = "seed_h05a"
ROUTE_A = ("e_hm#fwd", "e_mc#fwd")          # 항구→시장→성당(door_1)
ROUTE_B = ("e_hh#fwd", "e_hc#fwd")          # 항구→산복도로→성당
EXACT = {"e_hm#fwd": 400, "e_mc#fwd": 300, "e_hh#fwd": 600, "e_hc#fwd": 300, "e_hm2#fwd": 400}


def knowledge(edges, places=("항구", "시장", "성당", "산복도로"), access=None):
    return {"places": {p: {"knows_name": True, "visited": False, "rough_location": None, "acquisitions": []}
                       for p in places},
            "edges": {e: {"connection_known": True, "distance_estimate_m": EXACT.get(e),
                          "distance_precision": None, "last_confirmed_at": None,
                          "acquisition_sources": ["SEED"]} for e in edges},
            "access": dict(access or {}), "revision": 0}


def raw_with(templates=None, **extra):
    return ev_raw(templates or {}, **extra)


def make_session(raw=None, *, at="항구", edges=ROUTE_A, places=("항구", "시장", "성당", "산복도로"),
                 access=None, items=None, cognition=None):
    raw = raw or raw_with()
    s = core.TRPGSession("s-h05a", 1, 2, "x", {})
    s.scenario_id = "t"                       # 저작 ActorId는 시나리오 id에서 파생(픽스처와 같게)
    s.scenario_data = raw
    res = dc.compile_scenario(raw, s.scenario_id)
    assert res.ok, [(d.code, d.message) for d in res.errors]
    s.definition_revision = res.revision
    s.players = {PLAYER_UID: {"name": "주인공", "profile": {}}}
    s.resources = {"주인공": dict(items or {})}
    s.world_clock = WC.initial_clock(K)
    s.actor_registry = {PC: {"kind": "PC", "display_name": "주인공", "source_kind": "PLAYER",
                             "source_key": str(PLAYER_UID), "aliases": []}}
    s.spatial_state = {"placements": {PC: SP.at_place(at)}, "scene_place": at}
    s.spatial_knowledge = {PC: knowledge(edges, places, access)}
    if cognition:
        s.cognition_state = copy.deepcopy(cognition)
    return s, res.definition


def ctx_of(s):
    c = PW.session_world_context(s)
    assert c.dynamic, c.reason
    return c


def world_snapshot(s):
    return {f: copy.deepcopy(getattr(s, f, None)) for f in WS.GAMEPLAY_WORLD_FIELDS}


def plan(s, raw_intent, *, route_options=(), access_options=(), caps=None, ties=None):
    c = ctx_of(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision,
                          route_options=route_options, access_options=access_options)
    intent = AI.resolve_intent(m, raw_intent, world=s)
    before = world_snapshot(s)
    p = PW.plan_player_turn(c, s, intent, transaction_id="tx-h05a", narrative_seed=SEED,
                            capabilities=PW.pc_capabilities(s, PLAYER_UID) if caps is None else caps,
                            resolved_tie_orders=ties)
    assert world_snapshot(s) == before                       # 계획은 정본을 바꾸지 않는다
    return p, m, intent


def go(m, place):
    key = next(k for k, pid, _ in m.places if pid == place)
    return {"movement_kind": "DESTINATION", "destination": key, "time_kind": "NONE"}


def manifest(s, **kw):
    c = ctx_of(s)
    return AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision, **kw)


# ── 세계 모드 ─────────────────────────────────────────────────

def test_world_mode_legacy_without_binding_and_dynamic_when_bound():
    s, _ = make_session()
    assert PW.session_world_context(s).dynamic
    s2 = core.TRPGSession("s-legacy", 1, 2, "x", {})
    assert PW.session_world_context(s2).mode == PW.MODE_LEGACY            # 현 운영 세션(바인딩 없음)
    s.world_clock = {}
    assert PW.session_world_context(s).reason == "clock_uninitialized"


# ── 이동 의도 · 목적지 해석 ─────────────────────────────────────

def test_short_registered_destination_movement_arrives():
    s, _ = make_session(edges=("e_hm#fwd",))
    m = manifest(s)
    p, _, intent = plan(s, go(m, "시장"))
    assert intent.movement.destination_place == "시장"
    assert p.status == PW.PLAN_PLANNED and p.outcome["kind"] == PW.OUT_ARRIVED
    w = p.projected_world
    assert SP.placement_of(w, PC) == SP.at_place("시장") and w.domain("spatial_state")["scene_place"] == "시장"
    assert w.domain("world_clock")["minute_index"] == K + 5 and p.code_owned_minutes == 5
    assert p.scene_place == "시장" and p.resolved_choice["route_signature"] == ["e_hm#fwd"]
    k = SP.actor_knowledge(w, PC)
    assert k["places"]["시장"]["visited"] is True and "TRAVERSED" in k["edges"]["e_hm#fwd"]["acquisition_sources"]
    # 합본 연산을 원본에 한 번 투영 = 계획 세계
    b = WM.WorldMutationBatch(batch_id="v", outer_origin="PLAYER_TURN", source_world_version_id=None,
                              definition_revision=None)
    for o in p.ops:
        b.add(o)
    b.freeze()
    r = WM.project(WM.ProjectedWorld.capture(s), b, WM.PRODUCTION_REGISTRY)
    assert r.ok and r.world.digest() == w.digest()


def test_unregistered_or_hidden_destination_never_becomes_canonical():
    s, _ = make_session(places=("항구", "시장"))
    m = manifest(s)
    assert {pid for _, pid, _ in m.places} == {"항구", "시장"}             # 모르는 성당은 후보 없음
    for bad in ({"movement_kind": "DESTINATION", "destination": "P9", "time_kind": "NONE"},
                {"movement_kind": "DESTINATION", "destination": "성당", "time_kind": "NONE"},
                {"movement_kind": "DESTINATION", "destination": "NONE", "time_kind": "NONE"}):
        intent = AI.resolve_intent(m, bad, world=s)
        assert intent.movement is None and intent.unresolved
        p = PW.plan_player_turn(ctx_of(s), s, intent, transaction_id="t", narrative_seed=SEED)
        assert p.status == PW.PLAN_NONE and p.ops == ()
    # 장소 안의 작은 이동은 정본 장소를 바꾸지 않는다
    local = AI.resolve_intent(m, {"movement_kind": "LOCAL_MICRO_MOVE", "time_kind": "NONE"}, world=s)
    assert PW.plan_player_turn(ctx_of(s), s, local, transaction_id="t", narrative_seed=SEED).ops == ()


def test_known_place_without_known_route_does_not_reveal_route():
    s, _ = make_session(edges=())                                        # 이름은 알지만 길을 모름
    p, _, _ = plan(s, go(manifest(s), "성당"))
    assert p.status == PW.PLAN_REJECTED and p.reason == "NO_KNOWN_ROUTE" and p.route_options == ()


def test_stale_manifest_rejected():
    s, _ = make_session(edges=("e_hm#fwd",))
    m = manifest(s)
    s.world_version = WS.next_world_version(s.session_id, None, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="x")
    intent = AI.resolve_intent(m, go(m, "시장"), world=s)
    assert intent.stale


# ── 경로 선택 ───────────────────────────────────────────────

def test_meaningful_known_routes_ask_then_semantic_choice():
    s, _ = make_session(edges=ROUTE_A + ROUTE_B)
    p, m, _ = plan(s, go(manifest(s), "성당"))
    assert p.status == PW.PLAN_NEEDS_CHOICE and p.ops == ()
    sigs = [tuple(o["signature"]) for o in p.route_options]
    assert set(sigs) == {ROUTE_A, ROUTE_B}
    # 플레이어 선택(R 키) → 안정 의미 서명, transient 키는 기록하지 않는다
    m2 = manifest(s, route_options=p.route_options)
    rk = next(k for k, sig, _, _ in m2.routes if tuple(sig) == ROUTE_B)
    p2, _, intent = plan(s, dict(go(m2, "성당"), route_choice=rk), route_options=p.route_options)
    assert intent.route_signature == ROUTE_B
    assert p2.status == PW.PLAN_PLANNED and p2.resolved_choice["route_signature"] == list(ROUTE_B)
    assert "R1" not in str(intent.semantic_record()) and "R2" not in str(intent.semantic_record())
    assert SP.placement_of(p2.projected_world, PC) == SP.at_place("성당")


def test_auto_selectable_route_tie_is_deterministic():
    raw = raw_with()
    raw["movement_edges"].append({"id": "e_hm2", "from": "항구", "to": "시장", "distance_m": 400})
    picks = set()
    for order in (("e_hm#fwd", "e_hm2#fwd"), ("e_hm2#fwd", "e_hm#fwd")):
        s, _ = make_session(raw, edges=order)
        p, _, _ = plan(s, go(manifest(s), "시장"))
        assert p.status == PW.PLAN_PLANNED
        picks.add(tuple(p.resolved_choice["route_signature"]))
    assert picks == {("e_hm#fwd",)}                                      # 지식 삽입 순서 무관(서명 사전순)


def test_hidden_locked_door_not_leaked_and_interrupts_at_observation():
    s, _ = make_session(edges=ROUTE_A)                                   # door_1 실제 LOCKED, PC는 모름
    p, _, _ = plan(s, go(manifest(s), "성당"))
    assert p.status == PW.PLAN_PLANNED
    assert p.route_options and not p.route_options[0]["known_constraints"]   # 제시 단계 누설 없음
    assert p.outcome["kind"] == PW.OUT_INTERRUPTED_BLOCKED and not p.outcome["arrived"]
    w = p.projected_world
    assert SP.placement_of(w, PC) == SP.at_place("시장") and p.scene_place == "시장"
    fact = SP.actor_knowledge(w, PC)["access"]["door_1"]
    assert fact["observed_state"] == "LOCKED" and fact["source"] == "OBSERVED"
    assert w.domain("world_clock")["minute_index"] == K + 5               # 문 앞까지의 시간만


# ── 사건 개입 · 대기/수면 ─────────────────────────────────────

def _scheduled_signal(after, observable_when, sid="sig"):
    eff = {"step_id": "noise", "type": "EMIT_SIGNAL", "signal_id": sid, "cue": "근처에서 비명이 들린다",
           "interrupt_policy": "IF_OBSERVABLE", "observable_when": observable_when}
    return {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
            "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "open", "steps": [
                {"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": after, "effect": eff}]}]}


def _armed(s, D):
    """사건을 미리 spawn시켜 예약 효과를 건 세계(정본 세션에 반영 — 테스트 준비)."""
    r = core.world_progression.project_world_progression(
        D, s, K, base_ops=[WM.make_op("arm", "ACCESS_STATE_SET", "TEST", {"control_id": "door_2", "state": "BARRED"})],
        narrative_seed=SEED)
    assert r.status == "COMPLETED", r.diagnostics
    for f in WS.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(s, f, copy.deepcopy(r.projected_world.domain(f)))
    return s


def test_movement_interrupted_by_scheduled_observable_event():
    raw = raw_with({"ev": _scheduled_signal(3, eq({"ref": "SCENE_PLACE"}, "항구"))})
    s, D = make_session(raw, edges=("e_hm#fwd",))
    _armed(s, D)
    p, _, _ = plan(s, go(manifest(s), "시장"))
    assert p.status == PW.PLAN_PLANNED and p.outcome["kind"] == PW.OUT_INTERRUPTED_SIGNAL
    pl = SP.placement_of(p.projected_world, PC)
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hm#fwd"
    assert p.projected_world.domain("world_clock")["minute_index"] == K + 3 and p.code_owned_minutes == 3
    assert p.transit["to_place"] == "시장" and p.scene_place is None     # 목적지 장면 아님
    assert p.outcome["interrupt"]["cue"] == "근처에서 비명이 들린다"


def test_wait_interrupted_but_sleep_continues_for_quiet_signal():
    quiet = {"all": [eq({"ref": "SCENE_PLACE"}, "항구"), {"not": eq({"ref": "OBSERVER_ACTIVITY"}, "SLEEPING")}]}
    raw = raw_with({"ev": _scheduled_signal(30, quiet)})
    for activity, kind, reached in (("WAIT", PW.OUT_TIME_INTERRUPTED, K + 30), ("SLEEP", PW.OUT_TIME_DONE, K + 60)):
        s, D = make_session(raw, edges=())
        _armed(s, D)
        p, _, _ = plan(s, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 60,
                           "activity": activity})
        assert p.status == PW.PLAN_PLANNED and p.outcome["kind"] == kind, (activity, p.outcome)
        assert p.projected_world.domain("world_clock")["minute_index"] == reached
        assert p.code_owned_minutes == reached - K
    # 수면 중에도 관찰 가능한 큰 신호는 수면을 끊는다
    loud = raw_with({"ev": _scheduled_signal(30, eq({"ref": "SCENE_PLACE"}, "항구"))})
    s, D = make_session(loud, edges=())
    _armed(s, D)
    p, _, _ = plan(s, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 600,
                       "activity": "SLEEP"})
    assert p.outcome["kind"] == PW.OUT_TIME_INTERRUPTED and p.end_minute == K + 30


def test_time_targets_daypart_and_clock_are_code_computed():
    s, D = make_session(edges=())
    cal = D["calendar"]
    assert AI.target_minute(cal, K, AI.TimeAdvanceIntent(AI.TIME_DAYPART, target_daypart="day")) == K + 360
    assert AI.target_minute(cal, K + 400, AI.TimeAdvanceIntent(AI.TIME_CLOCK, target_hhmm="08:00")) == K + 480
    assert AI.target_minute(cal, K + 500, AI.TimeAdvanceIntent(AI.TIME_CLOCK, target_hhmm="08:00")) == K + 1440 + 480
    m = manifest(s)
    assert AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "TARGET_DAYPART",
                                 "target_daypart": "noon"}, world=s).unresolved
    assert AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "DURATION",
                                 "duration_minutes": -5}, world=s).unresolved


# ── 접근 확인 토큰 ───────────────────────────────────────────

def _two_variant_raw():
    raw = raw_with()
    rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
    rule["actor_overrides"].append({"requires_capability": "pass_card", "result": "ACTION_REQUIRED",
                                    "action_policy": "CONFIRM", "action_delay_minutes": 1})
    return raw


def test_access_confirmation_token_uses_variant_identity():
    seen = {"door_1": {"observed_result": "ACTION_REQUIRED", "observed_state": "LOCKED",
                       "action_policy": "CONFIRM", "confirmed_at_minute": K, "source": "OBSERVED"}}
    raw = _two_variant_raw()
    s, D = make_session(raw, at="시장", edges=("e_mc#fwd",), access=seen, items={"key_door": 1})
    key_opts = PW.access_confirm_options(D, s, PC, PW.pc_capabilities(s, PLAYER_UID))
    s2, _ = make_session(raw, at="시장", edges=("e_mc#fwd",), access=seen, items={"pass_card": 1})
    pass_opts = PW.access_confirm_options(D, s2, PC, PW.pc_capabilities(s2, PLAYER_UID))
    assert len(key_opts) == len(pass_opts) == 1 and key_opts[0]["action_id"] != pass_opts[0]["action_id"]
    # 열쇠 보유자가 열쇠 variant 토큰으로 확인 → 통과(행동 지연 1분 + 300m)
    m = manifest(s, access_options=key_opts)
    p, _, intent = plan(s, dict(go(m, "성당"), access_confirm="K1"), access_options=key_opts)
    assert intent.access_action_ids == (key_opts[0]["action_id"],)
    assert p.outcome["kind"] == PW.OUT_ARRIVED and SP.placement_of(p.projected_world, PC) == SP.at_place("성당")
    # 다른 variant 토큰(통행증)으로는 열쇠 variant 행동을 승인하지 못한다
    m = manifest(s, access_options=pass_opts)
    p, _, _ = plan(s, dict(go(m, "성당"), access_confirm="K1"), access_options=pass_opts)
    assert p.outcome["kind"] == PW.OUT_INTERRUPTED_ACCESS and p.scene_place == "시장"
    # 확인은 성공 보장이 아니다 — 실제 상태가 바뀌었으면(BARRED) 토큰이 맞지 않아 멈춘다
    s.access_control_state = {"door_1": {"state": "BARRED"}}
    m = manifest(s, access_options=key_opts)
    p, _, _ = plan(s, dict(go(m, "성당"), access_confirm="K1"), access_options=key_opts)
    assert p.outcome["kind"] == PW.OUT_INTERRUPTED_ACCESS and not p.outcome["arrived"]


# ── PC 명시 태도 ─────────────────────────────────────────────

def _cog_with(belief=None):
    return {f"actor:{PC}": {"S40": {"awareness": "CONTENT", "fragments": [], "belief": belief,
                                    "acquisitions": [], "belief_provenance": [], "secrecy_awareness": None}}}


def test_explicit_pc_stance_is_authorized_belief_candidate():
    s, _ = make_session(edges=(), cognition=_cog_with())
    m = manifest(s)
    assert [iid for _, iid, _ in m.infos] == ["S40"]
    p, _, intent = plan(s, {"movement_kind": "NONE", "time_kind": "NONE", "stance_info": "I1",
                            "stance_belief": "BELIEVED"})
    (op,) = [o for o in p.ops if o.op_type == CG.OP_COGNITION_BELIEF_SET]
    assert op.payload["authority"] == "PLAYER_EXPLICIT" and op.payload["holder"] == f"actor:{PC}"
    assert CG.record_of(p.projected_world, f"actor:{PC}", "S40")["belief"] == "BELIEVED"


def test_hidden_info_cannot_be_stance_target_and_no_stance_without_statement():
    s, _ = make_session(edges=())                                        # PC는 S40을 모른다
    m = manifest(s)
    assert m.infos == ()
    intent = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "NONE", "stance_info": "I1",
                                   "stance_belief": "CERTAIN"}, world=s)
    assert intent.stance is None and intent.unresolved
    s2, _ = make_session(edges=(), cognition=_cog_with())
    p, _, _ = plan(s2, {"movement_kind": "NONE", "time_kind": "NONE"})
    assert p.ops == ()                                                   # 발화 없으면 믿음 변화 없음
