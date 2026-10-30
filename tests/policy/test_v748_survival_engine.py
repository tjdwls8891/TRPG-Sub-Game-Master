# -*- coding: utf-8 -*-
"""v7.48 — 생존물(A) 엔진: E1 상태 조건 · E2 빈사/사망 결말 · D-3 NPC 상태 규칙 · E3 추적자 · E4 소음/숨음.

사용자 결정(계획서 v1): D-1 나(빈사 20분) · D-2 가(영도 적용·설화재 제외) · D-3 나(모든 NPC에 상태 규칙).
"""
from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

import core
from core import definition_compiler as dc
from core import pursuit as PU
from core import status_rules as SR
from core import world_refs as RF

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
M = core.get_merged_status_effects({})


def decide(active=(), clock=None, apply=(), now=1000, npc=False, **kw):
    ctx = SR.Ctx(now=now, start_minute=0, active=frozenset(active), clock=copy.deepcopy(clock or {}), seed="s",
                 npc=npc, **kw)
    return SR.decide(M, ctx, "P", [{"target": "P", "status": s, "score": 90} for s in apply], [], ())


def names(rows):
    return sorted(e["status"] for e in rows)


# ── E1 상태 조건 ─────────────────────────────────────────────

def test_observer_status_ref_resolves_and_is_validated():
    ctx = RF.EvalContext({}, SimpleNamespace(), progress={"observer_statuses": {"부상": 3, "젖음": 1}})
    assert RF.resolve({"ref": "OBSERVER_STATUS", "status": "부상", "min_stage": 3}, ctx).value is True
    assert RF.resolve({"ref": "OBSERVER_STATUS", "status": "부상", "min_stage": 4}, ctx).value is False
    assert RF.resolve({"ref": "OBSERVER_STATUS", "status": "출혈"}, ctx).value is False
    assert RF.resolve({"ref": "OBSERVER_STATUS", "status": "부상"},
                      RF.EvalContext({}, SimpleNamespace())).known is False          # 공급되지 않은 진행 = 모름
    assert RF.dependency_keys({"ref": "OBSERVER_STATUS", "status": "부상"}) == [RF.OBSERVER_STATUSES_KEY]
    scope = RF.RefScope(actors=set(), places=set(), infos=set(), controls=set(), modes=set(), statuses={"부상"})
    assert RF.validate_ref({"ref": "OBSERVER_STATUS", "status": "없는상태"}, scope, "p")
    assert not RF.validate_ref({"ref": "OBSERVER_STATUS", "status": "부상", "min_stage": 2}, scope, "p")


# ── E2 빈사 · 사망 ───────────────────────────────────────────

def test_critical_injury_leads_to_dying_then_death_unless_treated():
    d = decide(active=["부상"], clock={"applied": {"부상": 0}, "stage": {"부상": 2}}, stage_in={"부상": 3})
    assert "빈사" in names(d.apply)                                                  # 위중 = 빈사(코드)
    dying = {"applied": {"부상": 0, "빈사": 1000}, "stage": {"부상": 3}}
    assert "사망" not in names(decide(active=["부상", "빈사"], clock=dying, now=1019).apply)
    assert "사망" in names(decide(active=["부상", "빈사"], clock=dying, now=1020).apply)   # D-1 나: 20분
    saved = decide(active=["부상", "빈사"], clock=dying, now=1010, care=frozenset({"응급처치"}))
    assert names(saved.clear) == ["빈사"] and saved.clock["stage"]["부상"] == 2         # 처치 = 빈사 해제·중상
    assert "빈사" not in names(decide(active=["부상"], clock=saved.clock, now=1011).apply)


def test_death_status_is_a_session_ending_with_scenario_override():
    end = SR.status_ending(M, ["부상", "사망"])
    assert end["id"] == "death" and end["status"] == "사망"
    sess = SimpleNamespace(players={"u": {"name": "P"}}, statuses={"P": ["사망"]},
                           scenario_data={"status_endings": {"사망": {"title": "희생자", "epilogue": "호수는 다시 고요해졌다."}}})
    e = core.turn_world.pc_status_ending(sess)
    assert e["title"] == "희생자" and e["id"] == "death"
    sess.statuses = {"P": [], "엄주섭": ["사망"]}
    assert core.turn_world.pc_status_ending(sess) is None                          # NPC 사망은 결말이 아니다


def test_dying_block_counts_down():
    sess = SimpleNamespace(players={"u": {"name": "P"}}, statuses={"P": ["빈사"]}, scenario_data={},
                           status_clock={"P": {"applied": {"빈사": 100}}}, world_clock={"minute_index": 108})
    assert "약 12분" in core.turn_world.dying_block(sess, None)
    sess.world_clock = {"minute_index": 125}
    assert "이번 턴 안에" in core.turn_world.dying_block(sess, None)


def test_seolhwajae_excludes_dying_and_death():
    SH = json.loads((ROOT / "scenarios" / "설화재.json").read_text(encoding="utf-8"))
    YD = json.loads((ROOT / "scenarios" / "영도.json").read_text(encoding="utf-8"))
    assert {"빈사", "사망"} & set(core.get_merged_status_effects(SH)) == set()
    assert {"빈사", "사망"} <= set(core.get_merged_status_effects(YD))


# ── D-3 NPC 상태 규칙 ─────────────────────────────────────────

def test_npc_rules_skip_untracked_facts_but_keep_time_and_care():
    long = decide(npc=True, now=99999)
    assert "탈수" not in names(long.apply) and "허기" not in names(long.apply)       # NPC 섭취는 추적하지 않음 = 모름
    bleed = decide(active=["출혈"], clock={"applied": {"출혈": 0}, "stage": {"출혈": 1}}, now=200, npc=True)
    assert names(bleed.clear) == ["출혈"]                                           # 시간 규칙은 그대로
    ctx = SR.Ctx(now=10, start_minute=0, active=frozenset({"지침"}), npc=True)
    d = SR.decide(M, ctx, "N", [], [{"target": "N", "status": "지침", "score": 10}], ())
    assert names(d.clear) == ["지침"]                                               # 휴식(활동) 모름 — 추출 해제 막지 않음
    treat = decide(active=["출혈"], clock={"applied": {"출혈": 0}, "stage": {"출혈": 2}}, npc=True,
                   care=frozenset({"지혈"}), now=5)
    assert treat.clock["stage"] == {"출혈": 1}


# ── 숨음 · 소음 ──────────────────────────────────────────────

def test_hiding_breaks_on_moving_or_noise():
    clk = {"applied": {"숨음": 0}}
    assert names(decide(active=["숨음"], clock=clk, moved=True).clear) == ["숨음"]
    assert names(decide(active=["숨음"], clock=clk, moved=False, noise="NORMAL").clear) == ["숨음"]
    assert not decide(active=["숨음"], clock=clk, moved=False, noise="QUIET").clear
    worn_loud = core.turn_world._turn_noise(SimpleNamespace(world_observation_raw={"noise": "NORMAL"}), {"소음"})
    assert worn_loud == "LOUD"                                                      # 입은 '소음'은 한 단계 위
    assert core.turn_world._turn_noise(SimpleNamespace(world_observation_raw={"noise": "QUIET"}), {"소음"}) == "QUIET"


# ── E3 추적자 ────────────────────────────────────────────────

CFG = PU.cfg_of({"pursuers": {"k": {"actor": "a", "mode": "walk", "patrol": ["항구", "시장", "성당"]}}}, "k")


def test_pursuer_decision_table():
    d = lambda loc, **kw: PU.decide(CFG, loc, **{"now": 100, "here": "항구", "pc_place": "성당",  # noqa: E731
                                                 "pc_hidden": False, "sees_pc": False, **kw})
    loc, dest, pace = d({}, sees_pc=True)
    assert loc["mode"] == PU.CHASE and dest == "성당" and pace == "chase"            # 보임 → 추적
    loc, dest, _ = d({}, sees_pc=True, pc_hidden=True)
    assert loc["mode"] == PU.WANDER                                                 # 숨으면 시야 감지 없음
    loc, dest, _ = d({"mode": PU.WANDER, "heard": {"place": "시장", "minute": 99}})
    assert loc["mode"] == PU.ALERT and dest == "시장" and "heard" not in loc          # 소리 → 수상
    loc, dest, _ = d({"mode": PU.ALERT, "target": "항구", "since": 90})
    assert loc["mode"] == PU.SEARCH and dest is None                                # 목표에 닿음 → 수색
    loc, _, _ = d({"mode": PU.SEARCH, "target": "항구", "since": 80}, now=100)
    assert loc["mode"] == PU.WANDER                                                 # 수색 15분 끝 → 배회
    loc, _, _ = d({"mode": PU.CHASE, "target": "성당", "since": 70}, now=100)
    assert loc["mode"] == PU.SEARCH                                                 # 20분 놓침 → 수색
    loc, dest, _ = d({"mode": PU.WANDER, "patrol_i": 0})
    assert dest == "시장" and loc["patrol_i"] == 2                                  # 순회(지금 자리는 건너뜀)


def _killer_raw(raw):
    raw["pursuers"] = {"killer": {"actor": "강지수", "mode": "walk", "patrol": ["시장", "항구"],
                                  "hearing": {"LOUD": 2, "NORMAL": 0}}}


def test_compiler_makes_state_template_and_keeps_other_revisions():
    from tests.policy.test_h04b_events_progression import ev_raw
    from tests.policy.test_h04a_actor_info import KANG
    raw = ev_raw({})
    _killer_raw(raw)
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    assert res.definition["pursuers"]["killer"]["actor"] == KANG                     # 이름 → ActorId
    assert "pursuer_killer" in res.definition["event_templates"]
    bad = ev_raw({})
    bad["pursuers"] = {"killer": {"actor": "없는사람", "mode": "날기", "patrol": ["달"]}}
    assert sum(d.code == "PURSUER_INVALID" for d in dc.compile_scenario(bad, "t").errors) == 3
    assert "pursuers" not in dc.compile_scenario(ev_raw({}), "t").definition       # 미저작 = 키 없음(revision 불변)


# ── 통합 — 실제 턴 ───────────────────────────────────────────

from tests.policy.test_h05bc_turn_wiring import (  # noqa: E402,F401 — 픽스처 재사용
    H05Provider, PC, drig, extraction, judgment, turn)
from tests.policy.test_ready_barrier import _json, rig  # noqa: E402,F401


def _spawn(r, D):
    """genesis 대신 — 추적자 상태 사건을 시작 즉시 생성(initial_full_scan) 결과로 정본에 둔다."""
    from core import world_progression as WP
    res = WP.project_world_progression(D, r.sess, r.sess.world_clock["minute_index"], narrative_seed="s",
                                       outer_origin="INITIALIZATION", initial_full_scan=True)
    r.sess.event_instances = copy.deepcopy(res.projected_world.domain("event_instances"))
    assert PU.instance_of(r.sess, "killer")


async def test_noise_brings_pursuer_and_encounter_is_forced(drig):
    from core import spatial as SP
    from tests.policy.test_h04a_actor_info import KANG
    r = drig
    D = r.make(raw_patch=_killer_raw, at="항구",
               extra_world={"spatial_state": {"placements": {PC: SP.at_place("항구"), KANG: SP.at_place("시장")},
                                              "scene_place": "항구"}})
    _spawn(r, D)
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"noise": "LOUD"})]
    await turn(r, "유리창을 깬다")
    iid = PU.instance_of(r.sess, "killer")
    first = PU.locals_of(r.sess, iid)
    assert (first.get("heard") or {}).get("place") == "항구" or first.get("mode") == PU.CHASE   # 소리가 닿거나 이미 봄
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=30, activity="WAIT")
    r.prov.routes["extraction"] = [extraction({"noise": "QUIET"})]
    await turn(r, "그 자리에서 기다린다")
    loc = PU.locals_of(r.sess, iid)
    assert loc["mode"] in (PU.ALERT, PU.CHASE, PU.SEARCH) and loc.get("heard") is None   # 소리 또는 시야(정적 가시성)
    assert SP.placement_of(r.sess, KANG).get("place_id") == "항구"                      # 소리 난 곳까지 왔다
    th = core.turn_world.threat_block(core.player_world.session_world_context(r.sess), None, r.sess)
    assert "반드시 등장" in th                                                          # 같은 장소 = 등장 강제
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"noise": "QUIET"})]
    await turn(r, "숨을 죽인다")
    assert "[위협 — 코드 확정]" in r.prov.prompts["logic"][-1][0]                       # 지시층위에 실제로 실린다
    assert "위협 — 추적자" in r.prov.prompts["judgment"][-1][0]                         # 판단층위 장면 인물 표시
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    back = SP.placement_of(r.sess, KANG)
    assert res["ok"] and back.get("kind") == "TRANSIT" and back.get("from_place") == "시장"   # 되감기 = 1턴 뒤 상태 복원
    assert PU.locals_of(r.sess, iid) == first
