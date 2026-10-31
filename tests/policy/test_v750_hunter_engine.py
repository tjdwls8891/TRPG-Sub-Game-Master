# -*- coding: utf-8 -*-
"""v7.50 — 게이트 헌터물(B) 엔진: E5 상시·발동 기술(D-B3 수정) · E6 공격 대결 부상(D-B1 가) · E7 사건 상태 해제 ·
E8 성장 포인트·등급(D-B2 가) · E9 장(章) 결말. 모든 기능은 선언·저작한 시나리오에만 — 기존 시나리오 그대로.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import core
from core import definition_compiler as dc
from core import events as EV
from core import judgment as JD
from core import npc_position as NP
from core import status_rules as SR
from core import turn_world as TW
from tests.policy.test_h04a_actor_info import KANG

pytestmark = pytest.mark.policy

SKILLS = {"직업": {"화염 계열": {
    "화염구": {"desc": "불덩이를 날린다", "bonus": 10, "domains": ["원거리"], "cost": {"status": "마력 소모", "stages": 1}},
    "불의 감각": {"desc": "열기로 주변을 읽는다", "kind": "상시", "bonus": 4, "domains": ["탐색"]},
    "불꽃 갑옷": {"desc": "몸에 불을 두른다", "kind": "상시", "bonus": 6, "domains": ["방어", "탐색"]}}},
    "공통": {"감각": {"관찰": "살핀다"}}}
SD = {"skills": SKILLS}
PROF = {"스킬": "화염구, 불의 감각, 불꽃 갑옷, 관찰"}
MANA = {"name": "마력 소모", "stages": ["경미", "심함", "고갈"], "stage_mods": [{"전체": 0}, {"전체": -5}, {"전체": -10}],
        "rules": {"lower_auto": {"activity_in": ["SLEEP", "WAIT"], "min_minutes": 30}}}


# ── E5 상시·발동 기술 ─────────────────────────────────────────

def test_always_skills_apply_by_domain_and_invoked_ones_only_when_named():
    assert JD.skill_terms(PROF, SD, "", "탐색") == (6, "불꽃 갑옷", {})          # 상시 — 가장 큰 하나, 지목 불필요
    assert JD.skill_terms(PROF, SD, "", "근접")[0] == 0                          # 분야가 아니면 없음
    s, label, spend = JD.skill_terms(PROF, SD, "화염구", "원거리")
    assert (s, spend) == (10, {"마력 소모": 1}) and label == "화염구"            # 발동 — 비용
    assert JD.skill_terms(PROF, SD, "화염구", "근접") == (0, "", {})             # 분야 밖 발동 = 0
    assert JD.skill_terms(PROF, SD, "화염구", "원거리", lambda st: True) == (0, "화염구(고갈)", {})
    assert JD.skill_terms(PROF, SD, "관찰", None) == (6, "관찰", {})             # 옛 문자열 = 옛 동작(+6)
    assert JD.skill_terms({"스킬": "없는기술"}, {}, "없는기술", None)[0] == 6   # 표 밖 스킬도 옛 동작
    assert JD.is_invoked(SD, "화염구") and not JD.is_invoked(SD, "불의 감각")


def test_skill_table_is_checked():
    bad = {"skills": {"직업": {"x": {"a": {"desc": "d", "kind": "상시"},
                                     "b": {"desc": "d", "kind": "상시", "domains": ["근접"], "cost": {"status": "마력 소모"}},
                                     "c": {"desc": "d", "cost": {"status": "없는상태"}}, "e": {"desc": "d", "kind": "늘"}}}}}
    errs = JD.check_skills(bad, {"마력 소모"})
    assert len(errs) == 4
    assert JD.check_skills(SD, {"마력 소모"}) == []


def test_mana_recovers_with_rest_by_lower_auto():
    merged = {"마력 소모": MANA}
    ctx = SR.Ctx(now=100, start_minute=0, turn_minutes=30, activity="SLEEP", active=frozenset({"마력 소모"}),
                 clock={"applied": {"마력 소모": 0}, "stage": {"마력 소모": 2}})
    d = SR.decide(merged, ctx, "P", [], [], ())
    assert d.clock["stage"]["마력 소모"] == 1
    ctx = SR.Ctx(now=100, start_minute=0, turn_minutes=30, activity="SLEEP", active=frozenset({"마력 소모"}),
                 clock={"applied": {"마력 소모": 0}, "stage": {"마력 소모": 1}})
    assert [e["status"] for e in SR.decide(merged, ctx, "P", [], [], ()).clear] == ["마력 소모"]
    ctx = SR.Ctx(now=100, start_minute=0, turn_minutes=10, activity="SLEEP", active=frozenset({"마력 소모"}),
                 clock={"applied": {"마력 소모": 0}, "stage": {"마력 소모": 2}})
    assert SR.decide(merged, ctx, "P", [], [], ()).clock["stage"]["마력 소모"] == 2     # 30분 미만 = 그대로


def test_code_bumps_raise_existing_stage_before_rules_and_apply_new_status():
    caps = {"scenario_schema": {"capabilities": {"mortality": 1}}}
    merged = core.get_merged_status_effects(caps)
    ctx = SR.Ctx(now=50, start_minute=0, active=frozenset({"부상"}), clock={"applied": {"부상": 0}, "stage": {"부상": 2}})
    SR.pre_bump(merged, ctx, {"부상": 1})
    d = SR.decide(merged, ctx, "P", [], [], ())
    assert "빈사" in [e["status"] for e in d.apply]                          # 같은 턴 중상 → 빈사(규칙이 본다)
    plan = SimpleNamespace(status_apply=[])
    ctx2 = SR.Ctx(now=50, start_minute=0, active=frozenset(), clock={})
    d2 = SR.decide({"마력 소모": MANA}, ctx2, "P", [], [], ())
    SR.post_bump({"마력 소모": MANA}, ctx2, d2, "P", {"마력 소모": 2}, plan)
    assert {"target": "P", "status": "마력 소모", "score": 100} in d2.apply and plan.status_apply == [] and \
        d2.clock["stage"]["마력 소모"] == 2                                       # v7.53.1(F1): d.apply로(호출자가 합친다)


# ── E6 공격 대결 부상 ───────────────────────────────────────────

def _contest(win_margin, lose_margin, *, pc_wins=True, domain="근접", foe="강지수"):
    pc = {"name": "주인공", "is_pc": True, "margin": win_margin if pc_wins else lose_margin,
          "domain": domain if pc_wins else "방어"}
    npc = {"name": foe, "is_pc": False, "margin": lose_margin if pc_wins else win_margin,
           "domain": "방어" if pc_wins else domain}
    pc["contest"] = npc["contest"] = "A" if pc_wins else "B"
    return {"kind": "CONTEST", "actor": pc, "opponent": npc}


CW = {"scenario_data": {"scenario_schema": {"capabilities": {"combat_wounds": 1, "mortality": 1}}}}


def test_combat_hits_bands_both_sides_and_toughness():
    D = {"actor_definitions": {KANG: {"legacy_key": "강지수", "display_name": "강지수", "profile_base": {}}}}
    s = SimpleNamespace(**CW, statuses={}, status_clock={})
    assert TW.combat_hits(D, s, [_contest(10, 2)]) == {"강지수": 1}                 # 여유 차 8
    assert TW.combat_hits(D, s, [_contest(20, -5)]) == {"강지수": 2}                # 25
    assert TW.combat_hits(D, s, [_contest(25, -10)]) == {"강지수": 3}               # 35
    assert TW.combat_hits(D, s, [_contest(25, -10, pc_wins=False)]) == {"주인공": 3}  # D-B1 가: PC도
    assert TW.combat_hits(D, s, [_contest(25, -10, domain="설득")]) == {}           # 공격 분야가 아님
    D2 = copy.deepcopy(D)
    D2["actor_definitions"][KANG]["profile_base"]["toughness"] = 2
    assert TW.combat_hits(D2, s, [_contest(25, -10)]) == {"강지수": 2}              # 내구 2 = 3/2 올림
    assert TW.combat_hits(D, SimpleNamespace(scenario_data={}), [_contest(25, -10)]) == {}   # 기능 미선언


def test_combat_death_when_wounds_pass_the_last_stage():
    D = {"actor_definitions": {KANG: {"legacy_key": "강지수", "display_name": "강지수", "profile_base": {}}}}
    s = SimpleNamespace(**CW, statuses={"강지수": ["부상"]}, status_clock={"강지수": {"stage": {"부상": 3}}})
    assert TW.combat_deaths(D, s, [_contest(10, 2)]) == [(KANG, "사망", 0)]
    s.status_clock = {"강지수": {"stage": {"부상": 1}}}
    assert TW.combat_deaths(D, s, [_contest(10, 2)]) == []


# ── E7 사건 상태 해제(게이트 회차) ─────────────────────────────

def test_clear_overlays_marks_in_time_order():
    def inst(**loc):
        return {"local_state": loc}
    ev = {"i1": inst(**{"actor_status:a": {"status": "사망", "minute": 10}}),
          "i2": inst(**{"actor_status_clear:a": {"status": None, "minute": 20}})}
    assert EV.actor_status_marks(ev) == {}                                          # 회차 시작이 되살림
    ev["i3"] = inst(**{"actor_status:a": {"status": "사망", "minute": 30}})
    assert EV.actor_status_marks(ev) == {"a": {"사망": 30}}                        # 다시 쓰러짐


def test_compiler_combat_and_growth_steps():
    from tests.policy.test_h04b_events_progression import ev_raw
    raw = ev_raw({})
    raw["scenario_schema"]["capabilities"].update({"combat_wounds": 1, "mortality": 1, "growth": 1})
    raw["growth"] = {"records": {"등급": {"labels": ["E", "D", "C"]}}}
    raw["event_templates"]["gate"] = {
        "version": 1, "name": "게이트", "spawn_when": {"const": True},
        "phases": {"run": {"eligible_when": {"const": True}, "priority": 1,
                           "ending": {"id": "ch1", "title": "1장", "epilogue": "장이 끝난다", "chapter": True}}},
        "transition_cases": [{"case_id": "spawn", "from": None, "to": "run", "steps": [
            {"type": "CLEAR_ACTOR_STATUS", "step_id": "revive", "actor": KANG},
            {"type": "GRANT_GROWTH", "step_id": "pts", "points": 3},
            {"type": "SET_PC_RECORD", "step_id": "rank", "field": "등급", "value": 2}]}],
        "repeat_policy": "ONCE_PER_SESSION", "resolve_when": {"const": False}}
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    D = res.definition
    assert EV.COMBAT_TEMPLATE in D["event_templates"] and D["actor_removing_statuses"] == ["사망"]
    assert "combat_record" not in (raw.get("event_templates") or {})               # 원문은 그대로(합성은 정의만)
    nocap = copy.deepcopy(raw)
    nocap["scenario_schema"]["capabilities"].pop("growth")
    assert "STEP_CAPABILITY_REQUIRED" in [d.code for d in dc.compile_scenario(nocap, "t").errors]
    badrec = copy.deepcopy(raw)
    badrec["event_templates"]["gate"]["transition_cases"][0]["steps"][2]["field"] = "레벨"
    assert "STEP_REF_UNKNOWN" in [d.code for d in dc.compile_scenario(badrec, "t").errors]
    nodep = copy.deepcopy(raw)
    nodep["scenario_schema"]["capabilities"].pop("mortality")
    assert "CAP_DEPENDENCY_MISSING" in [d.code for d in dc.compile_scenario(nodep, "t").errors]


# ── E8 성장 포인트·등급 ─────────────────────────────────────────

def _growth_session(points=5, place="협회"):
    sd = {"scenario_schema": {"capabilities": {"growth": 1}}, "ability_stats": ["근력", "감각"], "skills": SKILLS,
          "growth": {"costs": {"stat": 2, "skill": 3}, "places": ["협회"],
                     "records": {"등급": {"labels": ["E", "D", "C"]}}}, "places": {"협회": {}}}
    prof = {"근력": 12, "감각": 20, "직업": "화염 계열", "스킬": "화염구", "성장 포인트": points}
    return SimpleNamespace(scenario_data=sd, players={"u": {"name": "주인공", "profile": prof}},
                           spatial_state={"scene_place": place})


def test_spend_options_follow_points_place_and_caps():
    s = _growth_session()
    keys = [k for k, _d, _c in core.growth.spend_options(s, "u", "협회")]
    assert "능력치:근력" in keys and "능력치:감각" not in keys                    # 상한 20
    assert {"기술:불의 감각", "기술:불꽃 갑옷"} <= set(keys) and "기술:화염구" not in keys
    assert core.growth.spend_options(s, "u", "거리") == []                         # 장소 밖
    assert core.growth.spend_options(_growth_session(points=1), "u", "협회") == []   # 포인트 부족
    assert core.growth.get_growth_config(s)["enabled"] is False                     # 실패 누적 성장은 끈다
    assert core.growth.check_spec(s.scenario_data) == []


def test_apply_turn_adds_grants_records_and_the_chosen_growth(monkeypatch):
    s = _growth_session(points=1)
    prep = SimpleNamespace(transaction_id="tx1", world_projection=None)
    monkeypatch.setattr(TW, "growth_changes", lambda _s, _p: {"points": 3, "records": {"등급": 2}})
    monkeypatch.setattr(TW, "turn_scene", lambda _s, _p=None: "협회")
    tx = SimpleNamespace(transaction_id="tx1", judgment_result={"judgment": {"growth": "기술:불꽃 갑옷"}})
    monkeypatch.setattr(core.turn_transaction, "get_active_transaction", lambda _s: tx)
    lines = core.growth.apply_turn(s, prep)
    prof = s.players["u"]["profile"]
    assert prof["성장 포인트"] == 1 and prof["등급"] == 2 and "불꽃 갑옷" in prof["스킬"]
    assert any("D" in x for x in lines)
    tx.judgment_result = {"judgment": {"growth": "능력치:근력"}}                    # 포인트 부족 — 반영 안 함
    monkeypatch.setattr(TW, "growth_changes", lambda _s, _p: {"points": 0, "records": {}})
    assert "지금 쓸 수 없음" in core.growth.apply_turn(s, prep)[0] and prof["근력"] == 12


# ── E9 장 결말 ──────────────────────────────────────────────────

def test_chapter_endings_do_not_close_the_session(monkeypatch):
    world = SimpleNamespace()
    monkeypatch.setattr(EV, "endings_in", lambda _d, _w: {"i": {"id": "ch1", "title": "1장", "epilogue": "e",
                                                                 "chapter": True}})
    monkeypatch.setattr(TW._pw, "session_world_context",
                        lambda _s: SimpleNamespace(mode=TW._pw.MODE_DYNAMIC, definition={}))
    monkeypatch.setattr(TW, "pc_status_ending", lambda _s: None)
    assert TW.session_ending(world) is None
    monkeypatch.setattr(EV, "new_endings", lambda _d, _b, _p: [{"id": "ch1", "title": "1장", "epilogue": "e",
                                                                  "chapter": True}])
    plan = SimpleNamespace(status=TW._pw.PLAN_PLANNED, projected_world=object())
    block = TW.ending_block(SimpleNamespace(definition={}), plan, world)
    assert "장의 맺음" in block and "다음 행동은 평소처럼" in block
    assert TW.new_chapter_endings({}, world, object())[0]["id"] == "ch1"


# ── 통합: 계획 경로의 전투 쓰러짐 ───────────────────────────────

def test_plan_records_combat_death_and_removes_the_actor():
    from core import world_mutation as WM
    from core import world_progression as WPG
    from tests.policy.test_h04b_events_progression import ev_raw
    from tests.policy.test_h05a_intent_planning import PC, make_session
    from core import spatial as SP
    raw = ev_raw({})
    raw["scenario_schema"]["capabilities"].update({"combat_wounds": 1, "mortality": 1})
    s, D = make_session(raw, at="항구")
    s.spatial_state["placements"][KANG] = SP.at_place("항구")
    w1 = WM.ProjectedWorld.capture(s)
    r = WPG.project_world_progression(D, w1, s.world_clock["minute_index"], progress={"observer_actor": PC},
                                      outer_origin="INITIALIZATION", initial_full_scan=True, narrative_seed="s")
    s.event_instances = copy.deepcopy(r.projected_world.domain("event_instances"))
    c = core.player_world.session_world_context(s)
    m = core.action_intent.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = core.action_intent.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "DURATION",
                                                   "duration_minutes": 1, "activity": "OTHER"}, world=s)
    p = core.player_world.plan_player_turn(c, s, intent, transaction_id="tx", narrative_seed="s",
                                           combat_deaths=[(KANG, "사망")])
    assert NP.effective_position(D, p.projected_world, KANG)["placement"] is None
    assert EV.actor_status_marks(p.projected_world.domain("event_instances"))[KANG] == {"사망": s.world_clock["minute_index"]}
