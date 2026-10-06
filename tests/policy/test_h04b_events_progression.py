# -*- coding: utf-8 -*-
"""H04-B — 조건식 · DependencyIndex · 사건 템플릿/인스턴스 · 국면/케이스 · 예약 v2 · 같은 분 일괄 ·
즉시 연쇄 · 정보 계열 · NPC 이동 wakeup · 신호 · 순수 WorldProgressionProjection.

H04-R23~R61 (매트릭스 참조 — 각 테스트 docstring에 ID 표기)
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

import pytest

import core
from core import access as AC
from core import cognition as CG
from core import definition_compiler as dc
from core import events as EV
from core import information as IN
from core import movement as MV
from core import predicates as PR
from core import spatial as SP
from core import turn_preparation as TP
from core import turn_transaction as tt
from core import world_clock as WC
from core import world_mutation as WM
from core import world_progression as WP
from core import world_refs as RF
from core import world_schedule as SC
from core import world_state as WS
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.policy.test_h04a_actor_info import KANG, PARK, info_raw
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _disk, _fund

pytestmark = pytest.mark.policy

K = 1440 * 1000                      # 정본 분(00:00, 시간대 night)
SEED = "seed_h04b"
CAL = {"calendar_id": "cal_main", "type": "GREGORIAN",
       "dayparts": {"day": {"start": "06:00", "end": "18:00", "representative": "12:00"},
                    "night": {"start": "18:00", "end": "06:00", "representative": "22:00"}}}
DOOR = {"ref": "ACCESS_STATE", "control": "door_1"}


def ev_raw(templates=None, **extra):
    raw = info_raw()
    raw["scenario_schema"]["capabilities"].update({"quantitative_time": 1, "dynamic_events": 1,
                                                   "scheduled_world_progression": 1})
    raw["calendar"] = copy.deepcopy(CAL)
    raw["access_control_definitions"]["controls"]["door_2"] = {"rule_ref": "door_rule", "initial_state": "LOCKED"}
    raw["event_templates"] = copy.deepcopy(templates or {})
    raw.update(copy.deepcopy(extra))
    return raw


def defn(templates=None, **extra):
    res = dc.compile_scenario(ev_raw(templates, **extra), "t")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    return res.definition


def codes(templates=None, sev="ERROR", **extra):
    res = dc.compile_scenario(ev_raw(templates, **extra), "t")
    return [d.code for d in res.diagnostics if d.severity == sev]


def world(**fields):
    s = core.TRPGSession("s-h04b", 1, 2, "x", {})
    s.world_clock = WC.initial_clock(K)
    for k, v in fields.items():
        setattr(s, k, copy.deepcopy(v))
    return s


def op(i, t, p, **kw):
    return WM.make_op(i, t, "TEST", p, **kw)


def run(D, w, target, **kw):
    kw.setdefault("narrative_seed", SEED)
    return WP.project_world_progression(D, w, target, **kw)


def ctx(D, w, **kw):
    src = w if isinstance(w, WM.ProjectedWorld) else WM.ProjectedWorld.capture(w)
    return RF.EvalContext(D, src, **kw)


def eq(ref, value):
    return {"cmp": "EQ", "ref": ref, "value": value}


def at(actor, place):
    return {"placements": {actor: {"kind": "AT_PLACE", "place_id": place}}}


def insts(r):
    return r.projected_world.domain("event_instances")


def one_inst(r):
    (i,) = insts(r).values()
    return i


# 단순 템플릿: 문이 BARRED가 되면 spawn, 단일 국면
SIMPLE = {"version": 1, "spawn_when": eq(DOOR, "BARRED"),
          "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}


# ══════════════════════════════════════════════════════════════
# 조건식 (R23~R26)
# ══════════════════════════════════════════════════════════════

def test_r23_r24_three_valued_logic_and_unknown_is_not_false():
    D = defn()
    w = world(spatial_state=at(PARK, "항구"))
    c = ctx(D, w)
    T, F, U = {"const": True}, {"const": False}, {"is_unknown": {"ref": "ACTOR_PLACE", "actor": PARK}}
    unk = eq({"ref": "ACTOR_PLACE", "actor": KANG}, "시장")           # KANG 위치 미추적 → UNKNOWN
    assert PR.evaluate(unk, c) == PR.UNKNOWN
    assert PR.evaluate({"not": unk}, c) == PR.UNKNOWN                  # NOT UNKNOWN = UNKNOWN
    assert PR.evaluate({"all": [T, unk]}, c) == PR.UNKNOWN
    assert PR.evaluate({"all": [F, unk]}, c) == PR.FALSE
    assert PR.evaluate({"any": [T, unk]}, c) == PR.TRUE
    assert PR.evaluate({"any": [F, unk]}, c) == PR.UNKNOWN
    assert PR.evaluate(U, c) == PR.FALSE and PR.evaluate({"is_unknown": unk["ref"]}, c) == PR.TRUE
    assert not PR.holds(unk, c)                                        # 충족 = TRUE만
    # 정본이 '없음'을 뜻하는 경우는 known
    assert PR.evaluate(eq({"ref": "COGNITION_AWARENESS", "holder": f"actor:{KANG}", "info": "S40"}, "NONE"), c) == PR.TRUE
    assert PR.evaluate(eq({"ref": "ACTOR_PLACE", "actor": PARK}, "항구"), c) == PR.TRUE
    assert PR.evaluate(eq(DOOR, "LOCKED"), c) == PR.TRUE                 # 정의 초기 상태
    assert PR.evaluate({"exists": {"ref": "EVENT_LOCAL", "instance": "evt_none", "key": "x"}}, c) == PR.UNKNOWN
    assert PR.evaluate({"cmp": "LT", "ref": DOOR, "value": 3}, c) == PR.UNKNOWN   # 순서 비교 불성립
    w2 = world(spatial_state={"placements": {PARK: {"kind": "TRANSIT", "movement_id": "m", "edge_id": "e",
                                                    "from_place": "항구", "to_place": "시장",
                                                    "edge_started_at_minute": K, "edge_expected_end_minute": K + 5}}})
    assert PR.evaluate(eq({"ref": "ACTOR_PLACE", "actor": PARK}, None), ctx(D, w2)) == PR.TRUE   # 이동 중 = 장소 없음(known)


@pytest.mark.parametrize("pred, code", [
    ("door_1 == BARRED", "PREDICATE_FREEFORM"),
    ({"cmp": "EQ", "ref": {"ref": "CLOCK_MINUTE"}, "value": K}, "PREDICATE_TIME_POLLING"),
    ({"cmp": "LT", "ref": {"ref": "CLOCK_MINUTE"}, "value": K}, "PREDICATE_TIME_POLLING"),
    ({"time": "ELAPSED_SINCE_PHASE_GTE", "minutes": 5}, "PREDICATE_ELAPSED_OUTSIDE_PHASE"),
    ({"time": "DAYPART_IS", "daypart": "noon"}, "PREDICATE_DAYPART_UNKNOWN"),
    ({"cmp": "EQ", "ref": {"ref": "ACCESS_STATE", "control": "door_9"}, "value": "OPEN"}, "PREDICATE_REF_INVALID"),
    ({"cmp": "EQ", "ref": {"ref": "ACTOR_PLACE", "actor": "$slot:ghost"}, "value": "x"}, "PREDICATE_REF_INVALID"),
    ({"eval": "1+1"}, "PREDICATE_INVALID"),
    ({"all": []}, "PREDICATE_INVALID"),
])
def test_r25_r26_freeform_and_polling_predicates_rejected(pred, code):
    assert code in codes({"t1": dict(SIMPLE, spawn_when=pred)})


def test_r26_daypart_requires_calendar_and_time_compiles_to_wakeup():
    raw_codes = [d.code for d in dc.compile_scenario(ev_raw(
        {"t1": dict(SIMPLE, spawn_when={"time": "DAYPART_IS", "daypart": "day"})}, calendar=None), "t").errors]
    assert "PREDICATE_TIME_NO_CALENDAR" in raw_codes or "CALENDAR_REQUIRED" in raw_codes
    D = defn({"t1": dict(SIMPLE, spawn_when={"all": [{"time": "DAYPART_IS", "daypart": "day"}, eq(DOOR, "LOCKED")]})})
    tpl = D["event_templates"]["t1"]
    assert tpl["dependency"]["spawn"] == ["access:door_1:state"]        # 시간은 의존 키가 아니다
    assert tpl["has_time_predicates"]
    c = ctx(D, world())
    assert PR.next_wakeup([tpl["spawn_when"]], c) == K + 6 * 60        # 다음 day 시작(06:00)
    m = WC.Calendar(D["calendar"]).to_minute_index(3, 9, 27, 8, 0)
    assert PR.next_wakeup([{"time": "AT_OR_AFTER_DATETIME", "date": "0003-09-27", "hhmm": "08:00"}], c) == \
        (m if m > K else None)
    assert PR.next_wakeup([{"cmp": "GTE", "ref": {"ref": "CLOCK_MINUTE"}, "value": K + 30}], c) == K + 30


def test_r26_time_spawn_is_woken_not_polled():
    """시간 조건 spawn: 매분 평가하지 않고 다음 경계 wakeup 1건으로 spawn."""
    D = defn({"dawn": dict(SIMPLE, spawn_when={"time": "DAYPART_IS", "daypart": "day"})})
    w = world()
    init = WP.initial_template_wakeups(D, w, narrative_seed=SEED)
    assert [o.payload["due_minute"] for o in init] == [K + 360]
    r = run(D, w, K + 600, base_ops=init)
    assert r.status == WP.COMPLETED
    inst = one_inst(r)
    assert inst["phase_entered_at"] == K + 360                          # 06:00에 spawn
    minutes = sorted({t["minute"] for t in r.dependency_trace})
    assert minutes == [K, K + 360, K + 600]                             # 1분 단위 polling 없음


# ══════════════════════════════════════════════════════════════
# DependencyIndex (R27·R28)
# ══════════════════════════════════════════════════════════════

def test_r27_r28_dependency_index_affected_only_and_wildcard_warning():
    T = {"on_door1": dict(SIMPLE, spawn_when=eq(DOOR, "BARRED")),
         "on_door2": dict(SIMPLE, spawn_when=eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"))}
    D = defn(T)
    idx = WP.build_dependency_index(D, world())
    assert idx.affected({"access:door_1:state"})["spawn"] == ["on_door1"]
    assert idx.affected({"access:door_2:state"})["spawn"] == ["on_door2"]
    assert idx.affected({"actor:x:placement"}) == {"spawn": [], "instance": [], "series": []}
    assert idx.affected({f"actor:{KANG}:placement"})["series"] == ["kang_loc"]
    W = {"after": dict(SIMPLE, spawn_when=eq({"ref": "EVENT_LIFECYCLE", "template": "on_door1"}, "RESOLVED"))}
    assert "DEPENDENCY_WILDCARD" in codes(dict(T, **W), sev="WARNING")
    D2 = defn(dict(T, **W))
    idx2 = WP.build_dependency_index(D2, world())
    assert idx2.affected({"event:evt_abc:lifecycle"})["spawn"] == ["after"]
    assert WP.full_scan(D2, world())["spawn"] == ["after", "on_door1", "on_door2"]
    # 변경 키는 정본 차이에서 결정적으로 나온다(시계·예약은 키가 아님)
    a = WM.ProjectedWorld.capture(world())
    b = WM.ProjectedWorld(dict(a.to_dict(), access_control_state={"door_1": {"state": "OPEN"}},
                               world_clock={"minute_index": K + 5, "start_minute_index": K}))
    assert WP.changed_keys(a, b) == {"access:door_1:state"}


# ══════════════════════════════════════════════════════════════
# spawn · repeat · 인스턴스 정체성 (R29·R30)
# ══════════════════════════════════════════════════════════════

def test_r29_repeat_policies():
    slot = {"who": {"type": "ACTOR", "candidates": ["강지수", "박노인"]}}
    barred = op("b", "ACCESS_STATE_SET", {"control_id": "door_1", "state": "BARRED"})
    for pol, expect in (("ONCE_PER_SESSION", 1), ("ONCE_PER_BINDING", 2)):
        D = defn({"t": dict(SIMPLE, slots=slot, repeat_policy=pol)})
        r = run(D, world(), K, base_ops=[barred])
        assert r.status == WP.COMPLETED and len(insts(r)) == expect, pol
        # 재진행해도 더 spawn하지 않는다
        r2 = run(D, r.projected_world, K + 10, base_ops=[op("o", "ACCESS_STATE_SET",
                                                          {"control_id": "door_1", "state": "OPEN"}), ])
        r3 = run(D, r2.projected_world, K + 20, base_ops=[op("b2", "ACCESS_STATE_SET",
                                                           {"control_id": "door_1", "state": "BARRED"})])
        assert len(insts(r3)) == expect
    # REPEATABLE + max_occurrences: ACTIVE가 있으면 새로 만들지 않고, 해결 뒤 재발생(상한까지)
    rep = dict(SIMPLE, repeat_policy="REPEATABLE", max_occurrences=2,
               resolve_when=eq(DOOR, "OPEN"))
    D = defn({"t": rep})
    w = world()
    states = ["BARRED", "OPEN", "BARRED", "OPEN", "BARRED"]
    for n, st in enumerate(states):
        r = run(D, w, K + n, base_ops=[op(f"s{n}", "ACCESS_STATE_SET", {"control_id": "door_1", "state": st})])
        assert r.status == WP.COMPLETED, r.diagnostics
        w = r.projected_world
    lst = sorted(insts(r).values(), key=lambda i: i["occurrence_ordinal"])
    assert [i["occurrence_ordinal"] for i in lst] == [1, 2] and [i["lifecycle"] for i in lst] == ["RESOLVED", "RESOLVED"]


_ID_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from core import events as EV
print(json.dumps([EV.event_instance_id("seed_x", "tpl", 3, {"b": "npc_2", "a": "p"}, 2),
                  EV.choose_case.__name__]))
"""


def test_r30_event_instance_id_is_process_independent():
    outs = []
    for seed in ("0", "777"):
        cp = subprocess.run([sys.executable, "-c", _ID_SCRIPT, REPO_ROOT], capture_output=True, text=True,
                            env=dict(os.environ, PYTHONHASHSEED=seed), timeout=120)
        assert cp.returncode == 0, cp.stderr
        outs.append(json.loads(cp.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1]
    assert outs[0][0] == EV.event_instance_id("seed_x", "tpl", 3, {"a": "p", "b": "npc_2"}, 2)   # 키 순서 무관
    assert EV.event_instance_id("seed_x", "tpl", 3, {"a": "p"}, 2) != EV.event_instance_id("seed_y", "tpl", 3, {"a": "p"}, 2)


# ══════════════════════════════════════════════════════════════
# 국면 (R31~R35)
# ══════════════════════════════════════════════════════════════

def _phased(stability="STICKY", mode="PRIORITY", **kw):
    t = {"version": 1, "spawn_when": eq(DOOR, "BARRED"),
         "phase_selector": {"mode": mode, "stability": stability},
         "phases": {
             "watch": {"eligible_when": {"const": True}, "priority": 1, "rank": 0,
                       "retain_when": {"all": [{"not": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN")},
                                               {"not": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "CLOSED")}]}},
             "breach": {"eligible_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN"), "priority": 5,
                        "rank": 1, "retain_when": {"not": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED")}},
             "panic": {"eligible_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "CLOSED"), "priority": 9,
                       "rank": 2}}}
    t.update(kw)
    return t


def _set(n, ctl, st):
    return op(n, "ACCESS_STATE_SET", {"control_id": ctl, "state": st})


def _phase_after(D, steps):
    w = world()
    for n, (ctl, st) in enumerate(steps):
        r = run(D, w, K + n, base_ops=[_set(f"s{n}", ctl, st)])
        assert r.status == WP.COMPLETED, (r.status, r.diagnostics)
        w = r.projected_world
    return one_inst(r)


def test_r32_r33_sticky_retains_true_and_reselects_on_false_or_unknown():
    D = defn({"t": _phased()})
    i = _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN")])
    assert i["current_phase"] == "breach"
    # door_2 CLOSED: panic(priority 9)이 eligible이지만 breach retain(not BARRED) TRUE → 유지(STICKY)
    i = _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "CLOSED")])
    assert i["current_phase"] == "breach"
    # retain FALSE → selector
    i = _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "BARRED")])
    assert i["current_phase"] == "watch"
    # BEST_MATCH는 즉시 갈아탄다
    D2 = defn({"t": _phased("BEST_MATCH")})
    assert _phase_after(D2, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "CLOSED")])["current_phase"] == "panic"
    # retain UNKNOWN도 유지가 아니다(EVENT_LOCAL 미기록 = UNKNOWN)
    t = _phased()
    t["phases"]["breach"]["retain_when"] = eq({"ref": "EVENT_LOCAL", "instance": "$self", "key": "hold"}, True)
    D3 = defn({"t": t})
    assert _phase_after(D3, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "CLOSED")])["current_phase"] == "panic"


def test_r31_resolve_cancel_before_selector_and_terminal_and_monotonic():
    t = _phased(resolve_when=eq({"ref": "ACCESS_STATE", "control": "door_2"}, "CLOSED"),
                cancel_when=eq({"ref": "ACCESS_STATE", "control": "door_2"}, "CLOSED"))
    D = defn({"t": t})
    i = _phase_after(D, [("door_1", "BARRED"), ("door_2", "CLOSED")])
    assert i["lifecycle"] == "RESOLVED" and i["current_phase"] == "watch"   # panic으로 가기 전에 해결
    assert i["history"][-1]["kind"] == "RESOLVED"
    t = _phased()
    t["phases"]["breach"]["terminal"] = True
    D = defn({"t": t})
    assert _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "CLOSED"),
                            ("door_2", "BARRED")])["current_phase"] == "breach"   # terminal은 이탈 없음
    D = defn({"t": _phased("MONOTONIC")})
    assert _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN"), ("door_2", "BARRED")])["current_phase"] == "breach"


def test_r34_r35_priority_tie_rejected_and_score_is_code_side():
    t = _phased()
    t["phases"]["panic"]["priority"] = 5
    assert "EVENT_PHASE_PRIORITY_TIE" in codes({"t": t})
    assert "EVENT_PHASE_RANK_REQUIRED" in codes({"t": dict(SIMPLE, phase_selector={"stability": "MONOTONIC"})})
    sc = _phased("BEST_MATCH", "SCORE")
    sc["phases"]["watch"].update({"base_score": 10})
    sc["phases"]["breach"].update({"base_score": 0, "score_terms": [
        {"when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN"), "points": 20}]})
    sc["phases"]["panic"].update({"base_score": 0})
    D = defn({"t": sc})
    assert _phase_after(D, [("door_1", "BARRED")])["current_phase"] == "watch"       # 10 > 0
    assert _phase_after(D, [("door_1", "BARRED"), ("door_2", "OPEN")])["current_phase"] == "breach"   # 20 > 10
    assert "EVENT_SCORE_TERMS_UNUSED" in codes({"t": dict(_phased(), phases=sc["phases"])}, sev="WARNING")


def test_spawn_without_eligible_phase_is_diagnosed_not_guessed():
    t = dict(SIMPLE, phases={"p": {"eligible_when": {"const": False}, "priority": 1}})
    D = defn({"t": t})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert insts(r) == {} and any(dg["code"] == "SPAWN_NO_ELIGIBLE_PHASE" for dg in r.diagnostics)


# ══════════════════════════════════════════════════════════════
# 케이스 (R36~R39)
# ══════════════════════════════════════════════════════════════

def _cased(selection="DETERMINISTIC_WEIGHTED", weights=(1, 1, 1), valid_c=True):
    cases = []
    for cid, w_ in zip(("ca", "cb", "cc"), weights):
        cases.append({"case_id": cid, "from": None, "to": "open", "weight": w_,
                      "valid_when": {"const": True} if (cid != "cc" or valid_c) else {"const": False},
                      "steps": [{"step_id": "mark", "type": "SET_EVENT_LOCAL", "key": "case", "value": cid}]})
    return dict(SIMPLE, case_selection=selection, transition_cases=cases)


def test_r36_r37_transition_established_and_weighted_case_stable():
    D = defn({"t": _cased()})
    r1 = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    r2 = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    i1, i2 = one_inst(r1), one_inst(r2)
    assert i1["local_state"]["case"] == i2["local_state"]["case"] == i1["history"][0]["case_id"]
    assert i1["history"][0]["case_candidates"] == ["ca", "cb", "cc"]
    tpl = D["event_templates"]["t"]
    proto = dict(i1, current_phase="open")
    c = ctx(D, world(), self_instance=i1["instance_id"], instance_overrides={i1["instance_id"]: proto})
    case, ids = EV.choose_case(tpl, c, narrative_seed=SEED, instance_id=i1["instance_id"], transition_ordinal=0,
                               from_phase=None, to_phase="open")
    assert case["case_id"] == i1["local_state"]["case"]
    # 케이스가 모두 무효여도 전이(spawn 진입)는 성립한다
    none = dict(SIMPLE, transition_cases=[{"case_id": "x", "from": None, "to": "open",
                                           "valid_when": {"const": False}, "steps": []}])
    r = run(defn({"t": none}), world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert one_inst(r)["current_phase"] == "open" and one_inst(r)["history"][0]["case_id"] is None
    assert any(dg["code"] == "NO_VALID_CASE" for dg in r.diagnostics)


def test_r38_r39_weights_and_pool_and_first_valid():
    for bad in (0, -1, 1.5, True):
        assert "EVENT_CASE_WEIGHT_INVALID" in codes({"t": _cased(weights=(bad, 1, 1))})
    tpl = defn({"t": _cased(weights=(1, 2, 3))})["event_templates"]["t"]
    c = ctx(defn({"t": _cased()}), world())
    picks = {}
    for pool_c in (True, False):
        t2 = dict(tpl, transition_cases=[dict(x, valid_when={"const": pool_c}) if x["case_id"] == "cc" else x
                                         for x in tpl["transition_cases"]])
        case, ids = EV.choose_case(t2, c, narrative_seed=SEED, instance_id="evt_q", transition_ordinal=4,
                                   from_phase=None, to_phase="open")
        picks[pool_c] = (case["case_id"], ids)
        # 공식 재현: seed % 총 weight → case_id 순 누적
        seed = int(core.world_ids.stable_digest("event.case.v1", SEED, "evt_q", 4, None, "open", ids, length=16), 16)
        ws = {"ca": 1, "cb": 2, "cc": 3}
        acc, want = 0, None
        for i in ids:
            acc += ws[i]
            if seed % sum(ws[x] for x in ids) < acc:
                want = i
                break
        assert case["case_id"] == want
    assert picks[True][1] == ["ca", "cb", "cc"] and picks[False][1] == ["ca", "cb"]
    # 저작 순서를 바꿔도 같은 결과(정렬된 후보)
    t3 = dict(tpl, transition_cases=list(reversed(tpl["transition_cases"])))
    assert EV.choose_case(t3, c, narrative_seed=SEED, instance_id="evt_q", transition_ordinal=4, from_phase=None,
                          to_phase="open")[0]["case_id"] == picks[True][0]
    fv = dict(tpl, case_selection="FIRST_VALID")
    assert EV.choose_case(fv, c, narrative_seed=SEED, instance_id="evt_q", transition_ordinal=4, from_phase=None,
                          to_phase="open")[0]["case_id"] == "ca"


# ══════════════════════════════════════════════════════════════
# step registry (R40)
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("step, code", [
    ({"step_id": "s", "type": "MUTATE_PATH", "path": "npcs.강지수.hp", "value": 0}, "STEP_TYPE_UNKNOWN"),
    ({"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_1", "state": "AJAR"}, "STEP_REF_UNKNOWN"),
    ({"step_id": "s", "type": "SET_COGNITION_BELIEF", "holder": f"actor:pc_{PLAYER_UID}", "info": "S40",
      "belief": "CERTAIN"}, "STEP_PC_BELIEF_AUTHORITY"),
    ({"step_id": "s", "type": "ACQUIRE_INFORMATION", "holder": f"actor:{KANG}", "info": "$step:nope"}, "STEP_REF_UNKNOWN"),
    ({"step_id": "s", "type": "SCHEDULE_WAKEUP", "after_minutes": 0}, "STEP_PARAMS_INVALID"),
    ({"step_id": "s", "type": "SCHEDULE_EFFECT", "after_minutes": 5,
      "effect": {"type": "SCHEDULE_WAKEUP", "after_minutes": 1}}, "STEP_EFFECT_TYPE_INVALID"),
    ({"step_id": "s", "type": "START_MOVEMENT", "actor": KANG, "destination": "성당", "mode": "fly"}, "STEP_REF_UNKNOWN"),
])
def test_r40_typed_steps_only(step, code):
    t = dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [step]}])
    assert code in codes({"t": t})


def test_r40_fixed_write_conflict_is_static_error():
    steps = [{"step_id": "a", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"},
             {"step_id": "b", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "BARRED"}]
    t = dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])
    assert "STEP_FIXED_WRITE_CONFLICT" in codes({"t": t})


def test_event_steps_information_cognition_relationship_chain():
    """한 케이스의 step은 순서대로 인과 사슬 — 생성 정보 획득 → NPC 믿음(PC 믿음은 불가)."""
    steps = [{"step_id": "mk", "type": "CREATE_INFORMATION", "proposition": "문이 강제로 잠겼다",
              "subject_refs": ["place:시장"]},
             {"step_id": "tell", "type": "ACQUIRE_INFORMATION", "holder": "$slot:who", "info": "$step:mk",
              "route": "WITNESS"},
             {"step_id": "trust", "type": "SET_COGNITION_BELIEF", "holder": "$slot:who", "info": "$step:mk",
              "belief": "BELIEVED"},
             {"step_id": "old", "type": "SUPERSEDE_INFORMATION", "info": "S40", "proposition": "기록이 옮겨졌다"},
             {"step_id": "rel", "type": "APPEND_RELATIONSHIP_DELTA", "from": "$slot:who", "to": "group:경비대",
              "kind": "SUSPICION"}]
    t = dict(SIMPLE, slots={"who": {"type": "ACTOR", "candidates": ["강지수"]}},
             transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": steps}])
    D = defn({"t": t})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED, r.diagnostics
    w = r.projected_world
    inst = one_inst(r)
    sctx = EV.StepContext(D, w, inst, "c", K, SEED, op_prefix="x")
    new_info = EV.event_info_id(sctx, "mk")
    rec = CG.record_of(w, f"actor:{KANG}", new_info)
    assert rec["awareness"] == "CONTENT" and rec["belief"] == "BELIEVED"
    assert rec["acquisitions"][0]["event_instance"] == inst["instance_id"]
    st = w.domain("information_state")
    assert IN.effective_validity(D, st, "S40") == "SUPERSEDED"
    assert list(w.domain("relationship_state")) == [f"actor:{KANG}->group:경비대"]


# ══════════════════════════════════════════════════════════════
# 예약 v2 (R41~R45)
# ══════════════════════════════════════════════════════════════

def _legacy_entry(sid, due, owner, kind="EVENT_WAKEUP"):
    return {"schedule_id": sid, "due_minute": due, "kind": kind, "owner_ref": owner, "payload_ref": None,
            "created_by_version": None, "order_key": sid}


def _eff_tpl(effect_steps, *, guard=None, priority=0, after=5, epoch_bound=True):
    st = {"step_id": "fx", "type": "SCHEDULE_EFFECT", "after_minutes": after, "effect": effect_steps,
          "conflict_priority": priority, "epoch_bound": epoch_bound}
    if guard is not None:
        st["guard"] = guard
    return dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [st]}])


def test_r41_r42_r43_legacy_7key_readable_and_v2_normalized_with_source_version():
    D = defn({"t": _eff_tpl({"type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"})})
    w = world()
    w.world_version = WS.next_world_version("s-h04b", None, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="tx0")
    src_vid = w.world_version["version_id"]
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    (e,) = r.projected_world.domain("world_schedule").values()
    assert set(e) == set(SC.V2_KEYS) and len(e) == 11
    assert e["created_by_version"] == src_vid                          # 생성 투영의 source/baseline version
    assert e["kind"] == "DEFERRED_EFFECT" and e["owner_epoch"] == f"{one_inst(r)['instance_id']}:0"
    assert e["payload"] == {"instance_id": one_inst(r)["instance_id"], "case_id": "c", "step_id": "fx"}
    r2 = run(D, r.projected_world, K + 10)
    assert r2.projected_world.domain("access_control_state")["door_2"] == {"state": "OPEN"}
    assert r2.due_processed == [{"schedule_id": e["schedule_id"], "kind": "DEFERRED_EFFECT", "minute": K + 5,
                                 "outcome": "APPLIED"}]
    # H03 7키 항목(EVENT_WAKEUP)도 그대로 읽고 소비한다
    inst = one_inst(r)
    legacy = _legacy_entry("old7", K + 3, f"event:{inst['instance_id']}")
    assert SC.validate_schedule({"old7": legacy}) == [] and SC.normalized_entry(legacy)["conflict_priority"] == 0
    w3 = WM.ProjectedWorld(dict(r.projected_world.to_dict(),
                                world_schedule=dict(r.projected_world.domain("world_schedule"), old7=legacy)))
    r3 = run(D, w3, K + 4)
    assert r3.status == WP.COMPLETED and "old7" not in r3.projected_world.domain("world_schedule")
    assert any(d["schedule_id"] == "old7" for d in r3.due_processed)


def test_r44_r45_stale_epoch_and_guard_consume_noop():
    eff = {"type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}
    # guard FALSE / UNKNOWN → 소비·no-op(재예약 없음)
    for guard in (eq(DOOR, "OPEN"), eq({"ref": "EVENT_LOCAL", "instance": "$self", "key": "nope"}, 1)):
        D = defn({"t": _eff_tpl(eff, guard=guard)})
        r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
        r2 = run(D, r.projected_world, K + 30)
        assert r2.projected_world.domain("world_schedule") == {}
        assert "door_2" not in r2.projected_world.domain("access_control_state")
        assert r2.due_processed[0]["outcome"] in ("GUARD_FALSE", "GUARD_UNKNOWN")
    # 국면이 바뀌면(epoch 낡음) 예약은 취소되거나, 남아 있어도 due에서 소비·no-op
    t = _eff_tpl(eff)
    t["phases"]["second"] = {"eligible_when": eq({"ref": "ACCESS_STATE", "control": "door_1"}, "OPEN"),
                             "priority": 2}
    t["phase_selector"] = {"stability": "BEST_MATCH"}
    D = defn({"t": t})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    sid = list(r.projected_world.domain("world_schedule"))[0]
    r2 = run(D, r.projected_world, K + 1, base_ops=[_set("o", "door_1", "OPEN")])
    assert one_inst(r2)["current_phase"] == "second"
    assert sid not in r2.projected_world.domain("world_schedule")       # 전이가 옛 epoch 예약을 취소
    # 취소가 누락된 경우의 방어선: due 시 owner_epoch 자기 검사
    stale = dict(r.projected_world.domain("world_schedule")[sid])
    w = WM.ProjectedWorld(dict(r2.projected_world.to_dict(), world_schedule={sid: stale}))
    r3 = run(D, w, K + 30)
    assert r3.due_processed[0]["outcome"] == "STALE_OWNER_EPOCH"
    assert "door_2" not in r3.projected_world.domain("access_control_state")


# ══════════════════════════════════════════════════════════════
# 같은 분 일괄 (R46~R50)
# ══════════════════════════════════════════════════════════════

def _deferred(sid, due, ctl, state, prio=0):
    return {"schedule_id": sid, "due_minute": due, "kind": "DEFERRED_EFFECT", "owner_ref": "event:evt_fx",
            "payload_ref": f"fx|c|{sid}", "created_by_version": None, "order_key": sid,
            "payload": {"instance_id": "evt_fx", "case_id": "c", "step_id": sid}, "guard_ref": None,
            "owner_epoch": None, "conflict_priority": prio}


def _fx_world(entries, *, steps):
    """픽스처 사건 evt_fx(템플릿 fx)의 예약 효과들. steps: {step_id: effect}."""
    t = dict(SIMPLE, spawn_when={"const": False},
             transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
                 {"step_id": sid, "type": "SCHEDULE_EFFECT", "after_minutes": 1, "effect": eff,
                  "epoch_bound": False} for sid, eff in steps.items()]}])
    D = defn({"fx": t})
    inst = {"instance_id": "evt_fx", "template_id": "fx", "template_version": 1, "bound_slots": {},
            "lifecycle": "ACTIVE", "current_phase": "open", "phase_entered_at": K, "transition_ordinal": 0,
            "occurrence_ordinal": 1, "local_state": {}, "history": []}
    w = world(event_instances={"evt_fx": inst}, world_schedule={e["schedule_id"]: e for e in entries})
    return D, w


def test_r46_same_base_evaluation_and_r47_insertion_order_irrelevant():
    steps = {"a": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "OPEN"},
             "b": {"type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}}
    e = [_deferred("a", K + 5, "door_1", "OPEN"), _deferred("b", K + 5, "door_2", "OPEN")]
    D, w1 = _fx_world(e, steps=steps)
    _, w2 = _fx_world(list(reversed(e)), steps=steps)
    r1, r2 = run(D, w1, K + 5), run(D, w2, K + 5)
    assert r1.status == r2.status == WP.COMPLETED
    assert r1.projected_world.digest() == r2.projected_world.digest()
    assert [d["schedule_id"] for d in r1.due_processed] == ["a", "b"]
    # 같은 기준 상태: b의 guard(door_1 LOCKED)는 a가 같은 분에 연 것을 보지 않는다
    t_steps = {"a": steps["a"], "b": steps["b"]}
    D2, w3 = _fx_world([_deferred("a", K + 5, "", ""), dict(_deferred("b", K + 5, "", ""), guard_ref="fx|c|b|guard")],
                       steps=t_steps)
    D2["event_templates"]["fx"]["transition_cases"][0]["steps"][1]["guard"] = eq(DOOR, "LOCKED")
    r3 = run(D2, w3, K + 5)
    acs = r3.projected_world.domain("access_control_state")
    assert acs["door_1"] == {"state": "OPEN"} and acs["door_2"] == {"state": "OPEN"}


def test_r48_priority_r49_offscreen_abort_r50_scene_tie_not_guessed():
    steps = {"a": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "OPEN"},
             "b": {"type": "SET_ACCESS_STATE", "control": "door_1", "state": "BARRED"}}
    # 우선순위가 다르면 높은 쪽
    D, w = _fx_world([_deferred("a", K + 5, "", "", prio=1), _deferred("b", K + 5, "", "", prio=7)], steps=steps)
    r = run(D, w, K + 5)
    assert r.status == WP.COMPLETED
    assert r.projected_world.domain("access_control_state")["door_1"] == {"state": "BARRED"}
    assert r.projected_world.domain("world_schedule") == {}                   # 진 쪽도 소비
    assert any(d["code"] == "SUPERSEDED_BY_PRIORITY" for d in r.diagnostics)
    # 같은 우선순위 + 장면 밖 → 안전 중단
    D, w = _fx_world([_deferred("a", K + 5, "", ""), _deferred("b", K + 5, "", "")], steps=steps)
    r = run(D, w, K + 5, progress={"scene_place": "외딴곳"})
    assert r.status == WP.CONFLICT and r.ops == () and r.projected_world is None
    # 같은 우선순위 + 장면 관련(door_1 = 시장→성당 구간) → tie, H04는 순서를 고르지 않는다
    r = run(D, w, K + 5, progress={"scene_place": "시장"})
    assert r.status == WP.TIE_PENDING and not r.committable and r.ops == ()
    (tie,) = r.same_minute_ties
    assert tie.minute == K + 5 and tie.effect_ids == ("due:a", "due:b")
    assert sorted(tie.allowed_orderings) == [("due:a", "due:b"), ("due:b", "due:a")]
    assert tie.affected_targets == (("access_control_state", ("door_1",)),)
    with pytest.raises(WM.WorldMutationError):
        r.build_batch(batch_id="x", outer_origin="PLAYER_TURN", source_world_version_id=None, definition_revision=None)
    # H05가 묘사 관찰로 순서를 주면 그 순서대로(뒤 효과가 최종)
    for order, final in ((["due:a", "due:b"], "BARRED"), (["due:b", "due:a"], "OPEN")):
        rr = run(D, w, K + 5, progress={"scene_place": "시장"}, resolved_tie_orders={tie.tie_id: order})
        assert rr.status == WP.COMPLETED, rr.diagnostics
        assert rr.projected_world.domain("access_control_state")["door_1"] == {"state": final}


# ══════════════════════════════════════════════════════════════
# 즉시 연쇄 · 안전 상한 (R51·R52) · 계열 재평가 (R53)
# ══════════════════════════════════════════════════════════════

def test_r51_immediate_cascade_fixed_point_same_minute():
    first = dict(SIMPLE, resolve_when=eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN"),
                 transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
                     {"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}]}])
    second = dict(SIMPLE, spawn_when=eq({"ref": "EVENT_LIFECYCLE", "template": "first"}, "RESOLVED"))
    D = defn({"first": first, "second": second})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED
    by_tpl = {i["template_id"]: i for i in insts(r).values()}
    assert by_tpl["first"]["lifecycle"] == "RESOLVED" and by_tpl["second"]["lifecycle"] == "ACTIVE"
    assert r.reached_minute == K                                       # 시간 증가 없음
    kinds = [t["kind"] for t in r.dependency_trace]
    assert kinds[0] == "BASE" and kinds.count("CASCADE") >= 3


def test_r52_oscillation_and_caps_abort():
    flip = {"version": 1, "spawn_when": eq(DOOR, "BARRED"), "phase_selector": {"stability": "BEST_MATCH"},
            "phases": {"a": {"eligible_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "LOCKED"), "priority": 1},
                       "b": {"eligible_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN"), "priority": 2}},
            "transition_cases": [
                {"case_id": "to_a", "from": "*", "to": "a", "steps": [
                    {"step_id": "o", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"}]},
                {"case_id": "to_b", "from": "*", "to": "b", "steps": [
                    {"step_id": "l", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "LOCKED"}]}]}
    D = defn({"flip": flip})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.CONFLICT and r.ops == ()
    detail = " ".join(str(d.get("detail")) for d in r.diagnostics)
    assert "CASCADE_OSCILLATION" in detail or "상한" in detail
    assert WP.MAX_CASCADE_ROUNDS == 32 and WP.MAX_TRANSITIONS_PER_EVENT_PER_MINUTE == 8
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")], max_transitions=1)
    assert r.status == WP.CONFLICT


def test_r53_series_reevaluated_by_dependency_inside_progression():
    move = dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
        {"step_id": "mv", "type": "SET_ACTOR_PLACEMENT", "actor": KANG, "place": "성당"}]}])
    D = defn({"move": move})
    w = world(spatial_state=at(KANG, "시장"))
    ops, _ = IN.build_series_init_ops(D, w, "kang_loc", op_prefix="si")
    r0 = run(D, w, K, base_ops=ops)
    i0 = r0.projected_world.domain("information_state")["series"]["kang_loc"]["current_info_id"]
    r = run(D, r0.projected_world, K, base_ops=[_set("b", "door_1", "BARRED")])
    st = r.projected_world.domain("information_state")
    s = st["series"]["kang_loc"]
    assert s["revision_ordinal"] == 1 and s["current_info_id"] != i0
    assert st["runtime_definitions"][s["current_info_id"]]["proposition"] == "강지수는 성당에 있다"
    assert IN.effective_validity(D, st, i0) == "SUPERSEDED"
    assert any(t["units"] == ["series:kang_loc"] for t in r.dependency_trace)


# ══════════════════════════════════════════════════════════════
# NPC 이동 (R54~R57)
# ══════════════════════════════════════════════════════════════

def _know(edges, est=None):
    est = est or {}
    return {"places": {}, "access": {}, "revision": 0,
            "edges": {e: dict({k: None for k in SP.EDGE_KNOWLEDGE_KEYS}, connection_known=True,
                              distance_estimate_m=est.get(e), acquisition_sources=["SEED"]) for e in edges}}


def _go_tpl(dest="성당", **extra):
    st = dict({"step_id": "go", "type": "START_MOVEMENT", "actor": KANG, "destination": dest, "mode": "walk"}, **extra)
    return dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [st]}])


ROUTE_B = ("e_hh#fwd", "e_hc#fwd")                                   # 600m(7.5분) + 300m(3.75분)


def test_r54_r56_npc_moves_by_known_route_with_checkpoint_wakeups():
    D = defn({"go": _go_tpl()})
    w = world(spatial_state=at(KANG, "항구"), spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    pl = r.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hh#fwd"
    assert (pl["edge_started_at_minute"], pl["edge_expected_end_minute"]) == (K, K + 8)   # 450초 → 8분(HALF_UP)
    (wk,) = [e for e in r.projected_world.domain("world_schedule").values() if e["kind"] == "MOVEMENT_WAKEUP"]
    assert wk["due_minute"] == K + 8 and wk["payload"]["destination_place"] == "성당"
    assert wk["payload"]["actor_id"] == KANG and wk["payload"]["mode_id"] == "walk"
    # 다음 checkpoint까지만 — 최종 순간이동 아님
    mid = run(D, r.projected_world, K + 9)
    pl = mid.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hc#fwd" and pl["edge_started_at_minute"] == K + 8
    end = run(D, mid.projected_world, K + 30)
    assert end.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("성당")
    k = SP.actor_knowledge(end.projected_world, KANG)
    assert k["edges"]["e_hc#fwd"]["acquisition_sources"] == ["SEED", "TRAVERSED"]
    assert k["places"]["성당"]["visited"] is True
    assert [d["kind"] for d in mid.due_processed + end.due_processed] == ["MOVEMENT_WAKEUP", "MOVEMENT_WAKEUP"]


def test_r55_no_known_route_never_uses_omniscient_path():
    D = defn({"go": _go_tpl()})
    w = world(spatial_state=at(KANG, "항구"), spatial_knowledge={})       # 실제 길은 있지만 모른다
    r = run(D, w, K + 60, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED
    assert r.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("항구")
    assert any(d["code"] == "MOVEMENT_NO_KNOWN_ROUTE" for d in r.diagnostics)
    assert not any(e["kind"] == "MOVEMENT_WAKEUP" for e in r.projected_world.domain("world_schedule").values())


def test_hidden_block_stops_npc_without_detour_and_records_observation():
    D = defn({"go": _go_tpl()})
    route_a = ("e_hm#fwd", "e_mc#fwd")
    w = world(spatial_state=at(KANG, "항구"),
              spatial_knowledge={KANG: _know(route_a + ROUTE_B, {"e_hm#fwd": 400, "e_mc#fwd": 300,
                                                                "e_hh#fwd": 600, "e_hc#fwd": 300})})
    r = run(D, w, K + 60, base_ops=[_set("b", "door_1", "BARRED")])
    pls = r.projected_world.domain("spatial_state")["placements"]
    assert pls[KANG] == SP.at_place("시장")                              # 숨은 봉쇄(door_1 BARRED)에서 멈춤, B로 우회 없음
    k = SP.actor_knowledge(r.projected_world, KANG)
    assert (k["access"]["door_1"]["observed_result"], k["access"]["door_1"]["observed_state"]) == \
        ("ACTION_REQUIRED", "BARRED")                                  # SEPARATE_ACTION은 자동 수행하지 않는다
    assert any(d["code"] == "MOVEMENT_STOPPED" for d in r.diagnostics)


def test_r57_zero_minute_edge_has_no_transit():
    raw_edges = ev_raw()["movement_edges"] + [{"id": "gate", "from": "항구", "to": "산복도로", "distance_m": 0}]
    D = defn({"go": _go_tpl()}, movement_edges=raw_edges)
    w = world(spatial_state=at(KANG, "항구"),
              spatial_knowledge={KANG: _know(("gate#fwd", "e_hc#fwd"), {"gate#fwd": 0, "e_hc#fwd": 300})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    pl = r.projected_world.domain("spatial_state")["placements"][KANG]
    assert pl["kind"] == "TRANSIT" and pl["edge_id"] == "e_hc#fwd" and pl["from_place"] == "산복도로"
    k = SP.actor_knowledge(r.projected_world, KANG)
    assert k["edges"]["gate#fwd"]["last_confirmed_at"] == K and k["places"]["산복도로"]["visited"]


def test_movement_wakeup_stale_when_placement_changed():
    D = defn({"go": _go_tpl()})
    w = world(spatial_state=at(KANG, "항구"), spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
    r = run(D, w, K, base_ops=[_set("b", "door_1", "BARRED")])
    moved = run(D, r.projected_world, K + 1, base_ops=[op("tp", "ACTOR_PLACEMENT_SET",
                                                         {"actor_id": KANG, "place_id": "외딴곳"})])
    r2 = run(D, moved.projected_world, K + 20)
    assert r2.projected_world.domain("spatial_state")["placements"][KANG] == SP.at_place("외딴곳")
    assert r2.due_processed[0]["outcome"] == "MOVEMENT_WAKEUP_STALE"


# ══════════════════════════════════════════════════════════════
# 접근 variant (R58)
# ══════════════════════════════════════════════════════════════

def test_r58_access_action_id_distinguishes_override_variants():
    raw = ev_raw()
    raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]["actor_overrides"].append(
        {"requires_capability": "pass_card", "result": "ACTION_REQUIRED", "action_policy": "CONFIRM",
         "override_id": "card_swipe"})
    D = dc.compile_scenario(raw, "t").definition
    edge = D["movement_edges"]["e_mc#fwd"]
    key = AC.actual_access(D, {}, edge, ("key_door",))
    card = AC.actual_access(D, {}, edge, ("pass_card",))
    assert (key.result, key.action_policy, key.state) == (card.result, card.action_policy, card.state)
    assert key.matched_variant == "door_rule:LOCKED:override:0" and card.matched_variant == "card_swipe"
    ids = {MV.access_action_id("e_mc#fwd", d.control_id, d.state, d.action_policy, d.matched_variant)
           for d in (key, card)}
    assert len(ids) == 2                                              # 같은 확인 토큰을 공유하지 않는다
    t = MV.plan_actual_traversal(D, actor_id="pc", edge_sequence=("e_hm#fwd", "e_mc#fwd"), mode_id="walk",
                                 start_minute=K, access_state={}, actor_capabilities=("pass_card",),
                                 confirmed_actions=(MV.access_action_id("e_mc#fwd", "door_1", "LOCKED", "CONFIRM",
                                                                        "door_rule:LOCKED:override:0"),))
    assert not t.completed and t.interruption["action_id"].endswith("|card_swipe")


# ══════════════════════════════════════════════════════════════
# 신호 · foreground (R59·R60) · 순수성 (R61)
# ══════════════════════════════════════════════════════════════

def _signal_tpl(policy):
    eff = {"step_id": "boom", "type": "EMIT_SIGNAL", "signal_id": "alarm", "cue": "멀리서 경보가 울린다",
           "interrupt_policy": policy, "observable_when": eq({"ref": "SCENE_PLACE"}, "시장")}
    return dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
        {"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": 10, "effect": eff}]}])


def test_r59_r60_observable_signal_stops_background_does_not():
    D = defn({"s": _signal_tpl("IF_OBSERVABLE")})
    r0 = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    seen = run(D, r0.projected_world, K + 60, progress={"scene_place": "시장"})
    assert seen.status == WP.FOREGROUND_INTERRUPT and seen.reached_minute == K + 10 and seen.committable
    assert seen.foreground_interrupt["cue"] == "멀리서 경보가 울린다"
    away = run(D, r0.projected_world, K + 60, progress={"scene_place": "외딴곳"})
    assert away.status == WP.COMPLETED and away.reached_minute == K + 60
    assert away.signals[0]["observable"] is False and away.foreground_interrupt is None
    unk = run(D, r0.projected_world, K + 60)                           # 장면 모름 → 관찰 불가 취급
    assert unk.status == WP.COMPLETED and unk.signals[0]["observability"] in ("UNKNOWN", "FALSE")
    D2 = defn({"s": _signal_tpl("NONE")})
    r1 = run(D2, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert run(D2, r1.projected_world, K + 60, progress={"scene_place": "시장"}).status == WP.COMPLETED


def test_r61_projection_is_pure_and_batch_reproduces_world():
    D = defn({"go": _go_tpl(), "s": _signal_tpl("NONE")})
    w = world(spatial_state=at(KANG, "항구"), spatial_knowledge={KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})})
    before = copy.deepcopy({k: getattr(w, k) for k in WS.GAMEPLAY_WORLD_FIELDS})
    r = run(D, w, K + 40, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED
    assert {k: getattr(w, k) for k in WS.GAMEPLAY_WORLD_FIELDS} == before   # 세션 불변
    b = r.build_batch(batch_id="b1", outer_origin="ADMIN_EDIT", source_world_version_id=None, definition_revision=None)
    b.freeze()
    res = WM.project(w, b, WM.PRODUCTION_REGISTRY)
    assert res.ok and res.world.digest() == r.projected_world.digest()
    assert SC.base_quiescence_of(res.world).quiescent                  # due ≤ now 잔여 없음


async def test_progression_ops_commit_and_rewind_through_player_turn(rig, inject_h04b):
    D = defn({"go": _go_tpl()})
    r_ = rig
    s = r_.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(K)
    s.spatial_state = at(KANG, "항구")
    s.spatial_knowledge = {KANG: _know(ROUTE_B, {e: 1 for e in ROUTE_B})}
    await _run_turn(r_)
    res = run(D, s, K + 9, base_ops=[_set("b", "door_1", "BARRED")])
    assert res.status == WP.COMPLETED
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        for o in res.ops:
            prep.world_mutation_batch.add(o)
    inject_h04b.setattr(TP, "capture_world_baseline", _hook)
    tx = await _run_turn(r_)
    inject_h04b.undo()
    assert tx.status == tt.TurnStatus.COMMITTED, tx.failure_code
    for k in WS.GAMEPLAY_WORLD_FIELDS:
        if k != "world_version":
            assert getattr(s, k) == res.projected_world.domain(k), k
    assert _disk(s)["event_instances"] == s.event_instances
    assert tx.preparation.ready_proof.base_quiescence["quiescent"] is True
    rw = await r_.gm.history_rewind(s, 1)
    assert rw["ok"], rw
    assert s.event_instances == {} and s.world_clock["minute_index"] == K


@pytest.fixture
def inject_h04b():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def test_r26_phase_elapsed_timer_is_one_wakeup_with_owner_epoch():
    """국면 경과 조건은 국면 진입 때 wakeup 1건으로 컴파일 — 매분 재평가 없음, epoch 소유."""
    elapsed = {"time": "ELAPSED_SINCE_PHASE_GTE", "minutes": 30}
    t = dict(SIMPLE, phases={"wait": {"eligible_when": {"const": True}, "priority": 1,
                                      "retain_when": {"not": elapsed}},
                             "late": {"eligible_when": elapsed, "priority": 2, "retain_when": {"const": True}}})
    D = defn({"t": t})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    (wk,) = r.projected_world.domain("world_schedule").values()
    inst = one_inst(r)
    assert (wk["kind"], wk["due_minute"], wk["owner_epoch"]) == ("EVENT_WAKEUP", K + 30, f"{inst['instance_id']}:0")
    r2 = run(D, r.projected_world, K + 100)
    i2 = one_inst(r2)
    assert (i2["current_phase"], i2["phase_entered_at"], i2["transition_ordinal"]) == ("late", K + 30, 1)
    assert sorted({t_["minute"] for t_ in r2.dependency_trace}) == [K + 30, K + 100]


def test_progress_context_activity_controls_observability():
    eff = {"step_id": "noise", "type": "EMIT_SIGNAL", "signal_id": "knock", "cue": "문 두드리는 소리",
           "interrupt_policy": "IF_OBSERVABLE",
           "observable_when": {"all": [eq({"ref": "SCENE_PLACE"}, "시장"),
                                       {"not": eq({"ref": "OBSERVER_ACTIVITY"}, "SLEEPING")}]}}
    t = dict(SIMPLE, transition_cases=[{"case_id": "c", "from": None, "to": "open", "steps": [
        {"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": 5, "effect": eff}]}])
    D = defn({"t": t})
    r0 = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    awake = run(D, r0.projected_world, K + 60, progress={"scene_place": "시장", "activity": "AWAKE"})
    asleep = run(D, r0.projected_world, K + 60, progress={"scene_place": "시장", "activity": "SLEEPING"})
    assert awake.status == WP.FOREGROUND_INTERRUPT and awake.reached_minute == K + 5
    assert asleep.status == WP.COMPLETED and asleep.signals[0]["observable"] is False
