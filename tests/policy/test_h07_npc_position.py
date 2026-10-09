# -*- coding: utf-8 -*-
"""H07 NPC 유효 위치 — 선택적 평상시 일과(routine) · 정본 override 우선 · 명시 해제(ACTOR_PLACEMENT_CLEAR) ·
일과 경계 wakeup · 장면/사건 조건 단일 경계(core.npc_position) · materialize 금지.

H07 최종 통합 구현 지시 §3·§4.
"""
from __future__ import annotations

import copy

import pytest

import core
from core import definition_compiler as dc
from core import npc_position as NP
from core import predicates as PR
from core import spatial as SP
from core import turn_world as TW
from core import world_clock as WC
from core import world_mutation as WM
from core import world_progression as WP
from core import world_refs as RF
from tests.policy.test_h04a_actor_info import KANG, PARK
from tests.policy.test_h04b_events_progression import K, SIMPLE, at, ctx, ev_raw, op, run, world

pytestmark = pytest.mark.policy

DAY = 1440
# K = 일련번호 1000일 00:00 — (1000 + 6) % 7 = 5 → 토요일
SAT0 = K
MON0 = K + 2 * DAY
ROUTINE = {"entries": [
    {"place": "시장", "days": ["MON", "TUE", "WED", "THU", "FRI"], "dayparts": ["day"]},   # 평일 06:00~18:00
    {"place": "해안", "from": "22:00", "to": "02:00"},                                    # 매일 밤(자정 넘김)
], "default_place": "항구"}
PLACE = {"ref": "ACTOR_PLACE", "actor": KANG}


def _raw(routine=ROUTINE, templates=None, **extra):
    raw = ev_raw(templates, **extra)
    if routine is not None:
        raw["default_npcs"]["강지수"]["routine"] = copy.deepcopy(routine)
    return raw


def _defn(routine=ROUTINE, templates=None, **extra):
    res = dc.compile_scenario(_raw(routine, templates, **extra), "t")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    return res.definition


def _codes(raw):
    return [d.code for d in dc.compile_scenario(raw, "t").diagnostics if d.severity == "ERROR"]


def _at(minute, **fields):
    w = world(**fields)
    w.world_clock = WC.initial_clock(minute)
    return w


def _proj(w, *ops):
    src = w if isinstance(w, WM.ProjectedWorld) else WM.ProjectedWorld.capture(w)
    b = WM.WorldMutationBatch(batch_id="b", outer_origin="ADMIN_EDIT", source_world_version_id=None,
                              definition_revision=None)
    for o in ops:
        b.add(o)
    b.freeze()
    r = WM.project(src, b, WM.PRODUCTION_REGISTRY)
    assert r.ok, [c.message for c in r.conflicts]
    return r.world


# ── 컴파일 ─────────────────────────────────────────────────────

def test_routine_compiles_to_normalized_windows_and_is_excluded_from_profile():
    D = _defn()
    r = D["actor_definitions"][KANG]["routine"]
    assert r == {"entries": [{"place": "시장", "days": [0, 1, 2, 3, 4], "windows": [[360, 1080]]},
                             {"place": "해안", "days": None, "windows": [[1320, 120]]}],
                 "default_place": "항구"}
    assert "routine" not in D["actor_definitions"][KANG]["profile_base"]
    assert "routine" not in D["actor_definitions"][PARK]                     # 미저작 인물엔 키가 없다


def test_unauthored_routine_keeps_definition_identical():
    a = dc.compile_scenario(_raw(routine=None), "t")
    b = dc.compile_scenario(ev_raw(), "t")
    assert a.revision == b.revision


@pytest.mark.parametrize("routine,code", [
    ({"entries": [{"place": "없는곳", "dayparts": ["day"]}]}, "ROUTINE_PLACE_UNKNOWN"),
    ({"entries": [{"place": "시장", "dayparts": ["저녁"]}]}, "ROUTINE_DAYPART_UNKNOWN"),
    ({"entries": [{"place": "시장", "dayparts": ["day"], "from": "01:00", "to": "02:00"}]}, "ROUTINE_WINDOW_CONFLICT"),
    ({"entries": [{"place": "시장"}]}, "ROUTINE_ENTRY_UNCONDITIONAL"),
    ({"entries": []}, "ROUTINE_EMPTY"),
    ({"entries": [{"place": "시장", "days": ["MONDAY"]}]}, "ROUTINE_DAYS_INVALID"),
    ({"entries": [{"place": "시장", "from": "09:00", "to": "09:00"}]}, "ROUTINE_TIME_INVALID"),
    ({"entries": [{"place": "시장", "dayparts": ["day"], "who": 1}]}, "ROUTINE_ENTRY_INVALID"),
    ({"slots": []}, "ROUTINE_INVALID"),
])
def test_routine_validation_errors(routine, code):
    assert code in _codes(_raw(routine))


def test_routine_requires_calendar_and_weekdays_need_week_calendar():
    raw = _raw()
    del raw["calendar"]
    raw["scenario_schema"]["capabilities"].pop("quantitative_time")
    assert "ROUTINE_REQUIRES_CALENDAR" in _codes(raw)
    raw = _raw()
    raw["calendar"] = {"calendar_id": "c2", "type": "FIXED_CUSTOM", "month_lengths": [30] * 12,
                       "dayparts": copy.deepcopy(raw["calendar"]["dayparts"])}
    assert "ROUTINE_WEEKDAY_UNSUPPORTED" in _codes(raw)
    raw["default_npcs"]["강지수"]["routine"] = {"entries": [{"place": "시장", "dayparts": ["day"]}]}
    assert _codes(raw) == []                                            # 시간대 일과는 주 없는 달력에서도 유효


def test_place_npc_association_keeps_relation_and_frequency_as_metadata_only():
    raw = _raw()
    raw["places"]["시장"]["npcs"] = [{"name": "강지수", "relation": "관리인", "frequency": "상주"}]
    D = dc.compile_scenario(raw, "t").definition
    assert D["place_definitions"]["시장"]["npc_associations"] == [
        {"legacy_name": "강지수", "actor_id": KANG, "relation": "관리인", "frequency": "상주"}]
    w = _at(SAT0 + 12 * 60)
    assert NP.effective_position(D, w, PARK)["source"] == NP.UNTRACKED     # 연고 ≠ 현재 위치
    raw["default_npcs"]["박노인"]["routine"] = None
    del raw["default_npcs"]["박노인"]["routine"]
    D = dc.compile_scenario(raw, "t").definition
    assert NP.effective_position(D, _at(SAT0 + 600), KANG)["placement"] == SP.at_place("항구")  # 일과(default)


# ── 해석 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("minute,place,entry", [
    (SAT0, "해안", 1),                            # 토 00:00 — 전날(금) 22:00 시작 창(요일 조건 없음)
    (SAT0 + 12 * 60, "항구", "default"),         # 토 12:00 — 주말이라 시장 아님
    (MON0 + 6 * 60, "시장", 0),                   # 월 06:00 — 창 시작(반열림 포함)
    (MON0 + 18 * 60, "항구", "default"),         # 월 18:00 — 창 끝(반열림 제외)
    (MON0 + 23 * 60, "해안", 1),
])
def test_routine_resolution_is_first_match_then_default(minute, place, entry):
    D = _defn()
    assert NP.routine_place_at(D, KANG, minute) == (place, entry)
    pos = NP.effective_position(D, _at(minute), KANG)
    assert pos == {"source": NP.ROUTINE, "placement": SP.at_place(place), "routine_entry": entry}


def test_weekday_window_crossing_midnight_uses_start_day():
    D = _defn({"entries": [{"place": "해안", "days": ["FRI"], "from": "22:00", "to": "02:00"}]})
    assert NP.routine_place_at(D, KANG, SAT0 + 60) == ("해안", 0)         # 토 01:00 — 금요일 밤 창
    assert NP.routine_place_at(D, KANG, SAT0 + 23 * 60) is None           # 토 23:00 — 토요일 창 아님
    assert NP.effective_position(D, _at(SAT0 + 23 * 60), KANG)["source"] == NP.UNTRACKED


def test_canonical_override_beats_routine_and_untracked_without_either():
    D = _defn()
    w = _at(MON0 + 12 * 60, spatial_state=at(KANG, "성당"))
    assert NP.effective_position(D, w, KANG) == {"source": NP.CANONICAL, "placement": SP.at_place("성당"),
                                                 "routine_entry": None}
    assert NP.effective_position(D, w, PARK)["source"] == NP.UNTRACKED
    c = ctx(D, w)
    assert RF.resolve(PLACE, c) == RF.known("성당")
    assert RF.resolve({"ref": "ACTOR_POSITION_SOURCE", "actor": KANG}, c) == RF.known("CANONICAL")
    assert RF.resolve({"ref": "ACTOR_PLACE", "actor": PARK}, c) == RF.UNKNOWN


def test_predicate_sees_routine_position_and_source():
    D = _defn()
    c = ctx(D, _at(MON0 + 12 * 60))
    assert RF.resolve(PLACE, c) == RF.known("시장")
    assert RF.resolve({"ref": "ACTOR_PLACEMENT_KIND", "actor": KANG}, c) == RF.known("AT_PLACE")
    assert RF.resolve({"ref": "ACTOR_POSITION_SOURCE", "actor": KANG}, c) == RF.known("ROUTINE")
    assert PR.evaluate({"cmp": "EQ", "ref": PLACE, "value": "시장"}, c) == PR.TRUE


def test_scene_and_predicate_share_effective_position_without_materializing():
    D = _defn()
    pc = "pc_1"
    w = _at(MON0 + 12 * 60, spatial_state={"placements": {pc: SP.at_place("시장")}})

    class Ctx:
        definition, pcs, lead_pc = D, (pc,), pc
    scene, projs = TW.scene_npc_projections(Ctx, None, w)
    assert scene == "시장" and [p["actor_id"] for p in projs] == [KANG]
    assert projs[0]["position_source"] == "ROUTINE" and projs[0]["location_tracked"] is False
    assert projs[0]["effective_placement"] == SP.at_place("시장") and projs[0]["placement"] is None
    assert set(w.spatial_state["placements"]) == {pc}                     # 장면 등장 = 정본 배치 아님
    w.world_clock = WC.initial_clock(MON0 + 20 * 60)                       # 저녁 — 일과상 항구
    assert TW.scene_npc_projections(Ctx, None, w)[1] == []
    w.spatial_state["placements"][KANG] = SP.at_place("시장")              # override: 시장에 붙잡혀 있다
    assert [p["actor_id"] for p in TW.scene_npc_projections(Ctx, None, w)[1]] == [KANG]


# ── 해제(ACTOR_PLACEMENT_CLEAR) ───────────────────────────────

def test_clear_op_returns_actor_to_routine_and_is_noop_when_untracked():
    D = _defn()
    w = _at(MON0 + 12 * 60, spatial_state=at(KANG, "성당"))
    w2 = _proj(w, op("c", SP.OP_ACTOR_PLACEMENT_CLEAR, {"actor_id": KANG}))
    assert KANG not in (w2.domain("spatial_state").get("placements") or {})
    assert NP.effective_position(D, w2, KANG)["placement"] == SP.at_place("시장")
    w3 = _proj(w2, op("c2", SP.OP_ACTOR_PLACEMENT_CLEAR, {"actor_id": KANG}))  # 이미 일과 — no-op
    assert w3.digest() == w2.digest()
    with pytest.raises(Exception):
        _proj(w, op("bad", SP.OP_ACTOR_PLACEMENT_CLEAR, {"actor_id": KANG, "why": "x"}))


def _know(edges):
    return {"places": {}, "access": {}, "revision": 0,
            "edges": {e: dict({k: None for k in SP.EDGE_KNOWLEDGE_KEYS}, connection_known=True,
                              acquisition_sources=["SEED"]) for e in edges}}


def test_clear_during_transit_removes_movement_wakeup_and_stale_wakeup_cannot_reoccupy():
    go = dict(copy.deepcopy(SIMPLE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
        {"step_id": "go", "type": "START_MOVEMENT", "actor": KANG, "destination": "성당", "mode": "walk",
         "route": ["e_hh#fwd", "e_hc#fwd"]}]}])
    D = _defn(templates={"go": go})
    w = _at(MON0 + 20 * 60, spatial_state=at(KANG, "항구"),
            spatial_knowledge={KANG: _know(("e_hh#fwd", "e_hc#fwd"))})
    r = run(D, w, MON0 + 20 * 60, base_ops=[op("b", "ACCESS_STATE_SET", {"control_id": "door_1", "state": "BARRED"})])
    pw = r.projected_world
    assert pw.domain("spatial_state")["placements"][KANG]["kind"] == "TRANSIT"
    assert any(e["kind"] == "MOVEMENT_WAKEUP" for e in pw.domain("world_schedule").values())
    cleared = _proj(pw, op("clr", SP.OP_ACTOR_PLACEMENT_CLEAR, {"actor_id": KANG}))
    assert not any(e["kind"] == "MOVEMENT_WAKEUP" for e in cleared.domain("world_schedule").values())
    later = run(D, cleared, MON0 + 21 * 60).projected_world
    assert KANG not in (later.domain("spatial_state").get("placements") or {})
    assert NP.effective_position(D, later, KANG)["placement"] == SP.at_place("항구")   # 저녁 일과(default)
    # 깨우기를 남겨 둔 채(정리 전 상태) 배치만 사라져도 STALE — 재오염 없음
    raw_world = pw.to_dict()
    raw_world["spatial_state"]["placements"].pop(KANG)
    r2 = run(D, WM.ProjectedWorld(raw_world), MON0 + 21 * 60)
    assert KANG not in (r2.projected_world.domain("spatial_state").get("placements") or {})
    assert any(x.get("outcome") == "MOVEMENT_WAKEUP_STALE" for x in r2.due_processed)


def test_clear_step_compiles_and_runs_from_event_case():
    tpl = {"version": 1, "spawn_when": {"cmp": "EQ", "ref": {"ref": "ACCESS_STATE", "control": "door_1"},
                                        "value": "OPEN"},
           "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}},
           "transition_cases": [{"case_id": "c", "from": None, "to": "open", "steps": [
               {"step_id": "free", "type": "CLEAR_ACTOR_PLACEMENT", "actor": KANG}]}]}
    D = _defn(templates={"release": tpl})
    w = _at(MON0 + 12 * 60, spatial_state=at(KANG, "성당"))
    r = run(D, w, MON0 + 12 * 60, base_ops=[op("b", "ACCESS_STATE_SET", {"control_id": "door_1", "state": "OPEN"})])
    assert KANG not in (r.projected_world.domain("spatial_state").get("placements") or {})
    assert NP.effective_position(D, r.projected_world, KANG)["placement"] == SP.at_place("시장")
    bad = copy.deepcopy(tpl)
    bad["transition_cases"][0]["steps"][0]["actor"] = "nobody"
    assert "STEP_REF_UNKNOWN" in [d.code for d in dc.compile_scenario(_raw(templates={"x": bad}), "t").diagnostics]


# ── 일과 경계 wakeup · 의존 사건 ───────────────────────────────

MEET = {"version": 1, "spawn_when": {"cmp": "EQ", "ref": PLACE, "value": "시장"},
        "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}


def test_next_wakeup_includes_only_read_actor_routine_boundary():
    D = _defn()
    c = ctx(D, _at(SAT0 + 12 * 60))
    assert PR.next_wakeup([MEET["spawn_when"]], c) == SAT0 + 22 * 60      # 장소가 바뀌는 다음 분 = 토 22:00
    # 토 18:00(평일 창 끝)은 주말엔 장소가 그대로라 깨우지 않는다
    assert NP.next_routine_boundary(D, KANG, SAT0 + 23 * 60) == SAT0 + DAY + 2 * 60   # 일 02:00 해안 → 항구
    assert NP.next_routine_boundary(D, KANG, SAT0 + DAY + 12 * 60) == SAT0 + DAY + 22 * 60
    other = {"cmp": "EQ", "ref": {"ref": "ACTOR_PLACE", "actor": PARK}, "value": "시장"}
    assert PR.next_wakeup([other], c) is None                              # 일과 없는 인물 — 경계 없음
    c2 = ctx(D, _at(SAT0 + 12 * 60, spatial_state=at(KANG, "성당")))
    assert PR.next_wakeup([MEET["spawn_when"]], c2) is None                # override 동안 일과 경계 없음


def test_long_wait_wakes_event_at_routine_boundary_without_canonical_sets():
    D = _defn(templates={"meet": MEET})
    w = _at(SAT0 + 12 * 60)
    start = WP.initial_template_wakeups(D, WM.ProjectedWorld.capture(w), narrative_seed="s")
    w0 = _proj(w, *start)
    r = run(D, w0, MON0 + 13 * 60)
    insts = [i for i in r.projected_world.domain("event_instances").values() if i["template_id"] == "meet"]
    assert len(insts) == 1 and insts[0]["phase_entered_at"] == MON0 + 6 * 60     # 월 06:00 경계에서 깨어났다
    assert KANG not in (r.projected_world.domain("spatial_state").get("placements") or {})
    assert not [o for o in r.ops if o.op_type in (SP.OP_ACTOR_PLACEMENT_SET, SP.OP_ACTOR_TRANSIT_SET)]


def test_routine_change_reevaluates_information_series():
    from core import information as IN
    D = _defn()
    w = _at(SAT0 + 12 * 60)                                                # 토 12:00 — 일과상 항구
    ops, _ = IN.build_series_init_ops(D, w, "kang_loc", op_prefix="si")
    r0 = run(D, w, SAT0 + 12 * 60, base_ops=ops)
    st0 = r0.projected_world.domain("information_state")
    i0 = st0["series"]["kang_loc"]["current_info_id"]
    assert st0["runtime_definitions"][i0]["proposition"] == "강지수는 항구에 있다"
    keys = WP.routine_changed_keys(D, r0.projected_world, SAT0 + 12 * 60, MON0 + 12 * 60)
    assert keys == {f"actor:{KANG}:placement"}
    assert WP.routine_changed_keys(D, r0.projected_world, SAT0 + 12 * 60, SAT0 + 13 * 60) == set()
    r = run(D, r0.projected_world, MON0 + 12 * 60)                       # 월 12:00 — 일과상 시장(정본 SET 없음)
    st = r.projected_world.domain("information_state")
    cur = st["series"]["kang_loc"]["current_info_id"]
    assert st["runtime_definitions"][cur]["proposition"] == "강지수는 시장에 있다"
    assert IN.effective_validity(D, st, i0) == "SUPERSEDED"
    assert KANG not in (r.projected_world.domain("spatial_state").get("placements") or {})


def test_event_movement_starts_from_routine_position_and_becomes_override():
    go = dict(copy.deepcopy(SIMPLE), transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
        {"step_id": "go", "type": "SET_ACTOR_PLACEMENT", "actor": KANG, "place": "성당"}]}])
    D = _defn(templates={"go": go})
    w = _at(MON0 + 12 * 60)
    r = run(D, w, MON0 + 12 * 60, base_ops=[op("b", "ACCESS_STATE_SET", {"control_id": "door_1", "state": "BARRED"})])
    pos = NP.effective_position(D, r.projected_world, KANG)
    assert pos["source"] == NP.CANONICAL and pos["placement"] == SP.at_place("성당")
    later = run(D, r.projected_world, MON0 + 20 * 60).projected_world
    assert NP.effective_position(D, later, KANG)["placement"] == SP.at_place("성당")    # 일과가 override를 덮지 않음


def test_effective_position_is_pure_and_deterministic():
    D = _defn()
    w = WM.ProjectedWorld.capture(_at(MON0 + 12 * 60))
    before = w.digest()
    a = [NP.effective_position(D, w, KANG) for _ in range(3)]
    assert a[0] == a[1] == a[2] and w.digest() == before
    assert NP.candidate_actors(D, w) == [KANG]
