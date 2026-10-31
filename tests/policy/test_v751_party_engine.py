# -*- coding: utf-8 -*-
"""v7.51 — NPC 파티 엔진(계획서 v1 · D-P1~P6 가). 파티 = 동행 + 인물 정의 `party` + 시나리오 `party_roles`.
역할 행동 4종(막기·공격·치유·보조) · 합류 조건·인원 한도·사건 합류 · 적 stance · PC 처치 성장. 선언한 시나리오에만."""
from __future__ import annotations

import copy
import json
import pathlib
import random
from types import SimpleNamespace

import pytest

import core
from cogs import gm as GM
from core import definition_compiler as dc
from core import events as EV
from core import party as PA
from core import spatial as SP
from core import turn_world as TW
from tests.conftest import REPO_ROOT
from tests.policy.test_h04a_actor_info import KANG, PARK
from tests.policy.test_h04b_events_progression import ev_raw
from tests.policy.test_h05a_intent_planning import PC, make_session

pytestmark = pytest.mark.policy

ROLES = {"탱커": {"act": "막기"}, "딜러": {"act": "공격", "stat": "근력", "domains": ["근접"]},
         "힐러": {"act": "치유", "cost": {"status": "부상", "stages": 1}}, "서포터": {"act": "보조", "bonus": 5}}


def party_raw(**over):
    raw = ev_raw({})
    raw["scenario_schema"]["capabilities"].update({"combat_wounds": 1, "mortality": 1, "growth": 1, "party": 1})
    raw["party_roles"] = copy.deepcopy(ROLES)
    raw["growth"] = {"kill_points": 1}
    raw["default_npcs"]["강지수"].update({"party": {"role": "탱커"}, "stats": {"근력": 14}})
    raw["default_npcs"]["박노인"].update({"party": {"role": "딜러", "join_when": {"const": True}}, "stats": {"근력": 12},
                                        "스킬": "강타"})
    raw["default_npcs"]["고블린"] = {"name": "고블린", "stats": {"근력": 8}, "stance": {"type": "hostile", "kill_points": 2}}
    raw["skills"] = {"직업": {"x": {"강타": {"desc": "세게 친다", "bonus": 8, "domains": ["근접"]}}}}
    raw.update(over)
    return raw


GOB = dc.authored_npc_actor_id("t", "고블린")


def test_compile_party_keys_and_errors():
    res = dc.compile_scenario(party_raw(), "t")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    D = res.definition
    assert D["party"]["limit"] == 4 and PA.role_of(D, KANG) == ("탱커", ROLES["탱커"])
    assert PA.is_hostile(D, GOB) and PA.kill_points(D, GOB) == 2 and not PA.is_hostile(D, KANG)
    assert "party" not in D["actor_definitions"][KANG]["profile_base"]
    bad = party_raw()
    bad["party_roles"]["힐러"]["act"] = "회복"
    bad["default_npcs"]["박노인"]["party"]["role"] = "없음"
    bad["default_npcs"]["고블린"]["stance"] = "적"
    codes = {d.code for d in dc.compile_scenario(bad, "t").errors}
    assert {"PARTY_INVALID", "PARTY_MEMBER_INVALID", "STANCE_INVALID"} <= codes
    nocap = party_raw()
    nocap["scenario_schema"]["capabilities"].pop("party")
    assert "PARTY_INVALID" in {d.code for d in dc.compile_scenario(nocap, "t").errors}
    nodep = party_raw()
    nodep["scenario_schema"]["capabilities"].pop("combat_wounds")
    assert "CAP_DEPENDENCY_MISSING" in {d.code for d in dc.compile_scenario(nodep, "t").errors}


def test_existing_scenarios_have_no_party_and_keep_revisions():
    want = {"영도": "f58e57e0", "설화재": "cd8c62c0", "청람호": "809ff6c2"}     # v8: 영도는 퀘스트 부속 은퇴로 갱신
    for n, rev in want.items():
        raw = json.loads(pathlib.Path(REPO_ROOT, "scenarios", f"{n}.json").read_text(encoding="utf-8"))
        qp = pathlib.Path(REPO_ROOT, "scenarios", f"{n}.quests.json")
        res = dc.compile_scenario(raw, n, json.loads(qp.read_text(encoding="utf-8")) if qp.exists() else None)
        assert res.revision.split(":")[1].startswith(rev) and "party" not in res.definition


def _world(at_npcs=(KANG, PARK, GOB)):
    s, D = make_session(party_raw(), at="항구")
    for aid in at_npcs:
        s.spatial_state["placements"][aid] = SP.at_place("항구")
    s.npcs = {"강지수": {"stats": {"근력": 14}}, "박노인": {"stats": {"근력": 12}}, "고블린": {"stats": {"근력": 8}}}
    return s, D


def test_join_needs_condition_and_limit_and_events_join_directly():
    s, D = _world()
    from core import world_mutation as WM
    ctx = core.player_world.session_world_context(s)
    kept, je, le, diags = TW.party_outcome(ctx, s, WM.ProjectedWorld.capture(s), ["강지수", "박노인"])
    assert kept == ["박노인"] and [d["reason"] for d in diags] == ["EVENT_ONLY"]      # 탱커는 사건으로만
    s.event_instances = {"i": {"local_state": {EV.PARTY_PREFIX + KANG: {"in": True, "minute": 1}}}}
    end = WM.ProjectedWorld.capture(s)
    s.event_instances = {}
    kept, je, le, _ = TW.party_outcome(ctx, s, end, [])
    assert je == ["강지수"]                                                            # 사건 합류
    s.companions = ["강지수"]
    s.event_instances = {"i": {"local_state": {EV.PARTY_PREFIX + KANG: {"in": True, "minute": 1}}}}
    s2 = copy.deepcopy(s)
    s2.event_instances["i"]["local_state"][EV.PARTY_PREFIX + KANG] = {"in": False, "minute": 5}
    _k, _j, le, _ = TW.party_outcome(ctx, s, WM.ProjectedWorld.capture(s2), [])
    assert le == ["강지수"]                                                            # 사건 이탈
    assert PA.join_allowed(D, s, PARK, ["a", "b", "c", "d"])[1] == "PARTY_FULL"


def test_party_members_present_and_down():
    s, D = _world()
    s.companions = ["강지수", "박노인"]
    ctx = core.player_world.session_world_context(s)
    ms = TW.party_members(ctx, s)
    assert [(m["name"], m["act"], m["down"]) for m in ms] == [("강지수", "막기", False), ("박노인", "공격", False)]
    s.statuses = {"박노인": ["기절"]}
    assert [m["down"] for m in TW.party_members(ctx, s)] == [False, True]
    s.spatial_state["placements"][PARK] = SP.at_place("시장")
    assert [m["name"] for m in TW.party_members(ctx, s)] == ["강지수"]                # 다른 장소는 행동 없음


def _contest(win, lose, *, pc_wins, foe="고블린"):
    pc = {"name": "주인공", "is_pc": True, "margin": win if pc_wins else lose, "domain": "근접" if pc_wins else "방어"}
    npc = {"name": foe, "is_pc": False, "margin": lose if pc_wins else win, "domain": "방어" if pc_wins else "근접"}
    pc["contest"] = npc["contest"] = "A" if pc_wins else "B"
    return {"kind": "CONTEST", "actor": pc, "opponent": npc}


def test_guard_takes_the_hit_meant_for_the_pc():
    s, D = _world()
    rolls = [_contest(20, -5, pc_wins=False)]
    assert TW.combat_hits(D, s, rolls) == {"주인공": 2}
    assert TW.combat_hits(D, s, rolls + [{"kind": "PARTY", "guard": "강지수"}]) == {"강지수": 2}


def test_kill_growth_only_for_pc_kills():
    s, D = _world()
    s.statuses, s.status_clock = {"고블린": ["부상"]}, {"고블린": {"stage": {"부상": 3}}}
    assert TW.combat_deaths(D, s, [_contest(10, 2, pc_wins=True)]) == [(GOB, "사망", 2)]
    ally = _contest(10, 2, pc_wins=True)
    ally["actor"].update(name="박노인", is_pc=False)
    assert TW.combat_deaths(D, s, [ally]) == [(GOB, "사망", 0)]                       # 파티원 처치 = 0(혼자만 성장)


def test_plan_writes_kill_grant_into_growth_ledger():
    s, D = _world()
    from core import world_mutation as WM
    from core import world_progression as WPG
    r = WPG.project_world_progression(D, WM.ProjectedWorld.capture(s), s.world_clock["minute_index"],
                                      progress={"observer_actor": PC}, outer_origin="INITIALIZATION",
                                      initial_full_scan=True, narrative_seed="s")
    s.event_instances = copy.deepcopy(r.projected_world.domain("event_instances"))
    c = core.player_world.session_world_context(s)
    m = core.action_intent.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = core.action_intent.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "DURATION",
                                                   "duration_minutes": 1, "activity": "OTHER"}, world=s)
    p = core.player_world.plan_player_turn(c, s, intent, transaction_id="tx", narrative_seed="s",
                                           combat_deaths=[(GOB, "사망", 2)])
    grants, _ = EV.growth_ledger(p.projected_world.domain("event_instances"))
    assert sum(grants.values()) == 2


def test_heal_targets_the_most_endangered():
    merged = core.get_merged_status_effects({"scenario_schema": {"capabilities": {"mortality": 1}}})
    sts = {"주인공": ["부상"], "강지수": ["부상"], "박노인": ["빈사"]}
    clk = {"주인공": {"stage": {"부상": 1}}, "강지수": {"stage": {"부상": 2}}}
    assert PA.heal_target(["주인공", "강지수", "박노인"], "응급처치", merged, sts, clk) == "박노인"
    assert PA.heal_target(["주인공", "강지수"], "응급처치", merged, sts, clk) == "강지수"
    assert PA.heal_target(["주인공"], "응급처치", merged, {}, {}) is None


def _wt(s):
    return SimpleNamespace(ctx=core.player_world.session_world_context(s))


def test_party_round_strikes_guards_and_skips_commanded(monkeypatch):
    s, D = _world()
    s.companions = ["강지수", "박노인"]
    random.seed(3)

    def roll_side(side):
        side["R"] = 50
        side["dice"] = [25, 25]
        side["success"] = side["R"] < side["T"]
        side["margin"] = side["T"] - side["R"]
    recs, game, logic = GM._party_round(s, _wt(s), [_contest(10, 2, pc_wins=True)], roll_side, lambda sd: "line")
    strike = [r for r in recs if r.get("party")]
    assert len(strike) == 1 and strike[0]["actor"]["name"] == "박노인" and strike[0]["opponent"]["name"] == "고블린"
    assert strike[0]["actor"]["S"] == 8 and "강타" in strike[0]["actor"]["skill"]          # 발동 기술 자동(비용 없음)
    assert [r for r in recs if r.get("kind") == "PARTY"][0]["guard"] == "강지수"
    cmd = _contest(10, 2, pc_wins=True)
    cmd["actor"]["name"] = "박노인"
    recs2, _g, _l = GM._party_round(s, _wt(s), [_contest(10, 2, pc_wins=True), cmd], roll_side, lambda sd: "")
    assert not [r for r in recs2 if r.get("party")]                                    # 명령받은 파티원은 자동 행동 없음
    peace = [{"kind": "CONTEST", "actor": {"name": "주인공"}, "opponent": {"name": "강지수"}}]
    assert GM._party_round(s, _wt(s), peace, roll_side, lambda sd: "") == ([], [], [])   # 적이 없으면 전투 턴 아님


def test_assist_adds_bonus_to_pc_attack_against_foe(monkeypatch):
    raw = party_raw()
    raw["default_npcs"]["박노인"]["party"] = {"role": "서포터", "join_when": {"const": True}}
    s, D = make_session(raw, at="항구")
    for aid in (KANG, PARK, GOB):
        s.spatial_state["placements"][aid] = SP.at_place("항구")
    s.npcs = {"고블린": {"stats": {"근력": 8}}}
    s.players[next(iter(s.players))]["profile"] = {"근력": 12}
    s.companions = ["박노인"]
    spec = {"kind": "CONTEST", "char_name": "주인공", "stat": "근력", "difficulty": "보통", "domain": "근접",
            "opponent": {"name": "고블린", "stat": "근력", "difficulty": "보통"}}
    plan = GM._judge_plan(s, spec, _wt(s))
    assert plan["actor"]["P"] == 5 and "파티 보조" in plan["actor"]["breakdown"]
    s.companions = []
    assert not GM._judge_plan(s, spec, _wt(s))["actor"].get("P")


def test_party_heal_picks_healer_target_and_stops_when_exhausted():
    raw = party_raw()
    raw["default_npcs"]["박노인"]["party"] = {"role": "힐러", "join_when": {"const": True}}
    s, D = make_session(raw, at="항구")
    for aid in (KANG, PARK):
        s.spatial_state["placements"][aid] = SP.at_place("항구")
    s.companions = ["강지수", "박노인"]
    s.statuses = {"강지수": ["부상"]}
    s.status_clock = {"강지수": {"stage": {"부상": 2}}}
    ctx = core.player_world.session_world_context(s)
    healer, target, eff = TW.party_heal(ctx, s)
    assert (healer["name"], target, eff) == ("박노인", "강지수", "응급처치")
    s.statuses["박노인"] = ["부상"]
    s.status_clock["박노인"] = {"stage": {"부상": 3}}                                # 비용 상태(부상) 마지막 단계 = 고갈
    assert TW.party_heal(ctx, s) is None
    assert "파티 — 코드 확정" in TW.party_block(ctx, None, s) and "파티원" in TW.party_block(ctx, None, s, for_judgment=True)
