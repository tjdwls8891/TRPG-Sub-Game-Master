# -*- coding: utf-8 -*-
"""v7.38 상태이상 조건 환경(D5) · 시작 장면 인물 후보(R3) · 얼굴 인지 시드(A4) · 표류물 후보(A3).

사용자 승인: 계획서 v1 §2·§3·§5(D5-a 가 — 규칙 있는 상태의 추출 해제는 clear_gate가 참일 때만).
"""
from __future__ import annotations

import copy
import json
import os

import pytest

import core
from core import status_rules as SR
from core import turn_history as TH
from core import turn_transaction as tt
from core import world_lifecycle as WL
from core.utils import get_merged_status_effects
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

YD = json.load(open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8"))
MERGED = get_merged_status_effects(YD)
START = 1000


def ctx(now, **kw):
    base = dict(now=START + now, start_minute=START, active=frozenset(), clock={})
    base.update(kw)
    return SR.Ctx(**base)


def run(c, apply=(), clear=(), consumed=()):
    a = [{"target": "P", "status": s, "score": 80} for s in apply]
    cl = [{"target": "P", "status": s, "score": 10} for s in clear]
    return SR.decide(MERGED, c, "P", a, cl, consumed)


# ── 공용·시나리오 구분 ──────────────────────────────────────────────

def test_common_and_scenario_statuses_are_separated_and_excludable():
    assert {n for n, e in MERGED.items() if e["scope"] == "시나리오"} == {"감염 의심", "감염"}
    assert {"탈수", "허기", "저체온", "지침", "배부름", "출혈", "부상", "공황"} <= {
        n for n, e in MERGED.items() if e["scope"] == "공용"}
    assert "탈수" not in get_merged_status_effects({"status_common_exclude": ["탈수"]})
    assert SR.check_definitions(YD, MERGED) == []
    bad = {"intake_groups": {"water": ["물"]},
           "status_effects": [{"name": "갈증", "rules": {"apply_gate": {"consumed": "soda"}}, "excludes": ["허기"]}]}
    codes = {c for c, _m in SR.check_definitions(bad, get_merged_status_effects(bad))}
    assert {"STATUS_RULES", "STATUS_EXCLUDES"} <= codes


# ── A1·A2: 시간 근거 부여 · 섭취 해제 ────────────────────────────────

def test_dehydration_not_applied_minutes_after_landing_but_after_hours():
    d = run(ctx(2), apply=["탈수"])                                      # 세션 7b9cfeb5 3턴(시작 +2분)
    assert d.apply == [] and "탈수 부여 보류" in d.notes[0]
    assert [e["status"] for e in run(ctx(400), apply=["탈수"]).apply] == ["탈수"]
    auto = run(ctx(720))                                                # 추출 없이도 12시간이면 코드가 부여
    assert "탈수" in [e["status"] for e in auto.apply] and auto.clock["applied"]["탈수"] == START + 720


def test_drinking_clears_dehydration_even_if_extraction_says_lingering():
    c = ctx(500, active=frozenset({"탈수"}), clock={"applied": {"탈수": START + 400}}, consumed=frozenset({"water"}))
    d = run(c, apply=["탈수"], consumed=("water",))                    # 세션 6턴 재생성: 마셨는데 35·높은 점수
    assert [e["status"] for e in d.clear] == ["탈수"] and "탈수" not in [e["status"] for e in d.apply]
    assert d.clock["intake"]["water"] == START + 500 and "탈수" not in d.clock["applied"]
    later = run(ctx(600, clock=d.clock), apply=["탈수"])                # 마신 뒤 100분 — 다시 붙지 않는다
    assert "탈수" not in [e["status"] for e in later.apply]


def test_extraction_cannot_clear_ruled_status_without_its_evidence():
    c = ctx(500, active=frozenset({"탈수"}), clock={"applied": {"탈수": START + 400}})
    d = run(c, clear=["탈수"])                                          # 물 소모 없이 '징후 없음'
    assert d.clear == [] and "해제 보류" in d.notes[0]
    plain = {**MERGED, "멀미": {"name": "멀미", "scope": "시나리오"}}     # 규칙 없는 상태는 지금처럼(v7.40: 저체온도 규칙)
    legacy = SR.decide(plain, ctx(10, active=frozenset({"멀미"})), "P", [],
                       [{"target": "P", "status": "멀미", "score": 10}], ())
    assert [e["status"] for e in legacy.clear] == ["멀미"]


def test_hunger_auto_and_fullness_exclusive():
    assert [e["status"] for e in run(ctx(480)).apply] == ["허기"]
    c = ctx(500, active=frozenset({"허기"}), clock={"applied": {"허기": START + 480}}, consumed=frozenset({"food"}))
    d = run(c, apply=["배부름"], consumed=("food",))
    assert [e["status"] for e in d.apply] == ["배부름"] and [e["status"] for e in d.clear] == ["허기"]
    gone = run(ctx(700, active=frozenset({"배부름"}), clock={"applied": {"배부름": START + 500}}))
    assert [e["status"] for e in gone.clear] == ["배부름"]


def test_rest_clears_fatigue_by_activity_and_minutes():
    act = frozenset({"지침"})
    assert run(ctx(30, active=act, activity="WAIT", turn_minutes=10)).clear == []
    d = run(ctx(60, active=act, activity="SLEEP", turn_minutes=45))
    assert [e["status"] for e in d.clear] == ["지침"]


def test_infection_suspicion_and_infection_timeline():
    sus = frozenset({"감염 의심"})
    assert run(ctx(100, active=sus, clock={"applied": {"감염 의심": START}}), apply=["감염"]).apply == []
    d = run(ctx(800, active=sus, clock={"applied": {"감염 의심": START}}), apply=["감염"])
    assert "감염" in [e["status"] for e in d.apply]
    ok = run(ctx(2900, active=sus, clock={"applied": {"감염 의심": START}}))
    assert [e["status"] for e in ok.clear] == ["감염 의심"]                # 48시간 무증상 — 자동 해제
    inf = frozenset({"감염"})
    assert run(ctx(5000, active=inf, clock={"applied": {"감염": START}}), clear=["감염"]).clear == []


def test_unknown_applied_time_starts_counting_now():
    d = run(ctx(100, active=frozenset({"감염 의심"})))                    # v7.37 이전에 붙은 상태
    assert d.clear == [] and d.clock["applied"]["감염 의심"] == START + 100


# ── 커밋 경로 통합(DYNAMIC) ─────────────────────────────────────────

async def test_dynamic_turn_commit_clears_dehydration_on_drinking_and_records_clock(drig):
    r = drig
    r.make(raw_patch=lambda raw: raw.update(intake_groups={"water": ["물통", "물"]}))
    r.sess.resources = {"테스터": {"반쯤 남은 물통": 1}}
    r.sess.statuses = {"테스터": ["탈수"]}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}]}, status=[("테스터", "탈수", 40)])]
    tx = await turn(r, "물통의 물을 다 마신다")
    assert tx.status == tt.TurnStatus.COMMITTED
    assert "탈수" not in (r.sess.statuses.get("테스터") or [])
    assert "water" in r.sess.status_clock["테스터"]["intake"]
    assert "status_clock" in TH.REVERSIBLE_FIELDS                          # 되감기 단위


async def test_dynamic_turn_withholds_early_dehydration(drig):
    r = drig
    r.make(raw_patch=lambda raw: raw.update(intake_groups={"water": ["물"]}))
    r.sess.statuses = {"테스터": []}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({}, status=[("테스터", "탈수", 80)])]
    tx = await turn(r, "주변을 둘러본다")
    assert tx.status == tt.TurnStatus.COMMITTED and "탈수" not in (r.sess.statuses.get("테스터") or [])


def test_legacy_turn_untouched():
    class P:
        extraction_plan = type("X", (), {"status_apply": [{"target": "P", "status": "탈수", "score": 90}],
                                          "status_clear": [], "diagnostics": {}})()
        world_turn = None
    s = type("S", (), {"scenario_data": YD, "players": {"1": {"name": "P"}}, "statuses": {}, "status_clock": {}})()
    assert SR.resolve_turn(s, P()) == [] and P.extraction_plan.status_apply[0]["status"] == "탈수"


def test_extraction_limits_carry_clear_conditions_and_current_statuses():
    s = type("S", (), {"scenario_data": YD, "statuses": {"테스터": ["탈수"]}, "npcs": {}})()
    text = core.build_extraction_limits(s)
    assert "탈수: 부여=[" in text and "해제=[물 마시기]" in text and "테스터: 탈수" in text


# ── R3·A4: 시작 장면 인물 후보 · 얼굴 인지 ──────────────────────────

def _yd_with_choices(rows):
    raw = copy.deepcopy(YD)
    fr = next(f for f in raw["start_frames"] if f["id"] == "arrive_person")
    fr["world_start"]["npc_choices"] = rows
    return raw


def test_npc_choices_compile_and_pick_deterministically():
    raw = _yd_with_choices([{"npc": "진복례"}, {"npc": "장철웅"}, {"npc": "황민석", "hidden": True}])
    res = core.definition_compiler.compile_scenario(raw, "영도")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    fr = next(f for f in res.definition["initialization_definitions"]["start_frames"] if f["frame_id"] == "arrive_person")
    rows = fr["world_start"]["npc_choices"]
    picks = {WL.pick_start_npc(rows, f"seed{i}", "arrive_person")["npc"] for i in range(40)}
    assert picks == {"진복례", "장철웅", "황민석"}
    assert WL.pick_start_npc(rows, "s1", "arrive_person") == WL.pick_start_npc(rows, "s1", "arrive_person")
    bad = core.definition_compiler.compile_scenario(_yd_with_choices([{"npc": "없는사람"}]), "영도")
    assert "WORLD_START_NPC_CHOICES_INVALID" in {d.code for d in bad.errors}


@pytest.mark.parametrize("hidden", [False, True])
async def test_genesis_places_chosen_npc_and_seeds_face_only_when_visible(rig, hidden):
    r = rig
    s = r.sess
    s.scenario_id, s.scenario_data = "영도", _yd_with_choices([{"npc": "장철웅", "hidden": hidden}])
    s.npcs = {}
    s.players = {PLAYER_UID: {"name": "테스터", "profile": {"이름": "테스터", "직업": "어부"}}}
    res = await WL.initialize_world(r.bot, s, {"id": "arrive_person"})
    assert res.outcome == WL.INIT_OK, res.reason
    assert core.spatial.placement_of(s, "yd.npc.jang_cheolung")["place_id"] == "물양장"
    pc = (s.cognition_state or {}).get(core.cognition.actor_holder(f"pc_{PLAYER_UID}")) or {}
    face = "yd.info.card.jang_cheolung.face"
    assert (face in pc) is (not hidden)
    assert "yd.info.card.jang_cheolung.name" not in pc                 # 이름은 시드하지 않는다
    assert s.world_initialization["start_choices"]["npc"] == "yd.npc.jang_cheolung"


# ── A3 ③: 표류물 후보 ──────────────────────────────────────────────

def test_flotsam_finds_compiled_and_validated():
    res = core.definition_compiler.compile_scenario(YD, "영도")
    ph = res.definition["event_templates"]["yd_ev_flotsam"]["phases"]["drift"]
    assert "녹슨 소형 손전등〈고장〉" in ph["finds"]
    raw = copy.deepcopy(YD)
    raw["event_templates"]["yd_ev_flotsam"]["phases"]["drift"]["finds"] = []
    assert "EVENT_PHASE_FINDS_INVALID" in {d.code for d in core.definition_compiler.compile_scenario(raw, "영도").errors}


def test_yeongdo_briefing_beats_and_intro_markers():
    beats = core.start_frame.briefing_beats(YD, {"이름": "김이서", "나이": "23", "직업": "대학생", "성별": "여",
                                                 "스킬": ["무전 조작"], "외형": "마른 체격", "복장": "해진 바람막이"},
                                            "물통 하나")
    assert len(beats) == 4 and "마른 체격" in beats[1] and "물통 하나" in beats[3]
    assert len(core.talk.parse(YD["scenario_intro"])) == 7 and core.talk.check(YD["scenario_intro"]) == []


# ── v7.40: 저체온 — 장소 묶음(status_places) ──────────────────────────

INDOOR = {"indoor": set(YD["status_places"]["indoor"])}


def test_place_group_atom_uses_hierarchy_and_unknown_when_undeclared():
    chain = SR._place_chain(YD, "해련사")
    assert SR.evaluate({"place_group": "indoor"}, ctx(0, place="해련사", place_chain=chain, place_groups=INDOOR), "x") \
        == SR.TRUE
    out = SR._place_chain(YD, "감지해변")
    assert SR.evaluate({"place_group": "indoor"}, ctx(0, place="감지해변", place_chain=out, place_groups=INDOOR), "x") \
        == SR.FALSE
    assert SR.evaluate({"place_group": "indoor"}, ctx(0, place="해련사", place_chain=chain), "x") == SR.UNKNOWN
    bad = {"places": {"방": {}}, "status_places": {"indoor": ["없는 곳"]},
           "status_effects": [{"name": "한기", "rules": {"apply_gate": {"place_group": "outdoor"}}}]}
    msgs = SR.check_definitions(bad, get_merged_status_effects(bad))
    assert {"STATUS_PLACES", "STATUS_RULES"} <= {c for c, _m in msgs}
    assert any("outdoor" in m for _c, m in msgs)


def test_hypothermia_applies_only_outdoors_and_clears_after_indoor_rest():
    beach = dict(place="감지해변", place_chain=SR._place_chain(YD, "감지해변"), place_groups=INDOOR)
    room = dict(place="방음실", place_chain=SR._place_chain(YD, "방음실"), place_groups=INDOOR)
    assert [e["status"] for e in run(ctx(30, **beach), apply=["저체온"]).apply] == ["저체온"]
    assert run(ctx(30, **room), apply=["저체온"]).apply == []           # 실내에서는 추출이 붙이지 못한다
    on = dict(active=frozenset({"저체온"}), clock={"applied": {"저체온": START + 30}})
    short = run(ctx(60, turn_minutes=10, activity="WAIT", **room, **on))
    assert short.clear == []                                             # 실내라도 쉰 시간이 모자라면 그대로
    rest = run(ctx(100, turn_minutes=40, activity="SLEEP", **room, **on))
    assert [e["status"] for e in rest.clear] == ["저체온"]              # 실내 휴식 30분↑ → 코드 해제
    outside = run(ctx(100, turn_minutes=40, activity="SLEEP", **beach, **on), clear=["저체온"])
    assert outside.clear == []                                           # 바깥에서는 추출 해제도 막힌다(D5-a)
