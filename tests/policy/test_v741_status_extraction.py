# -*- coding: utf-8 -*-
"""v7.41 상태이상 — 추출 판단 강화 · 심화 상태 · 감염 잠복(계획서 v1, 사용자 결정 D1 70% · D2~D5 승인).

표면 상태(추위·더위·젖음 등)는 추출이 붙이고 뗀다. 심화 상태(저체온·열탈진·감염)는 코드가 시간으로만 정한다.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import core
from core import status_rules as SR
from core import turn_world as TW
from core.utils import get_merged_status_effects
from tests.conftest import REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

YD = json.load(open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8"))
MERGED = get_merged_status_effects(YD)
START = 1000


def ctx(now, active=(), applied=None, **kw):
    return SR.Ctx(now=START + now, start_minute=START, active=frozenset(active),
                  clock={"applied": {k: START + v for k, v in (applied or {}).items()}, **kw.pop("clock", {})}, **kw)


def run(c, apply=(), clear=()):
    a = [{"target": "P", "status": s, "score": 80} for s in apply]
    cl = [{"target": "P", "status": s, "score": 10} for s in clear]
    return SR.decide(MERGED, c, "P", a, cl, ())


def names(rows):
    return [e["status"] for e in rows]


def test_definitions_are_valid_and_deep_statuses_are_marked():
    assert SR.check_definitions(YD, MERGED) == []
    assert {"추위", "더위", "젖음", "열탈진"} <= set(MERGED)
    assert SR.deep_statuses(MERGED) == {"저체온", "열탈진", "감염"}
    assert "status_places" not in YD                                     # D3(가): 실내 판정 데이터 정리


def test_hypothermia_follows_lasting_cold_and_clears_only_after_cold_is_gone():
    assert "저체온" not in names(run(ctx(100, active={"추위"}, applied={"추위": 0}), apply=["저체온"]).apply)  # 추출 불가
    assert "저체온" in names(run(ctx(120, active={"추위"}, applied={"추위": 0})).apply)          # 추위 120분
    wet = run(ctx(45, active={"추위", "젖음"}, applied={"추위": 0, "젖음": 0})).apply
    assert "저체온" in names(wet)                                                                # 젖으면 45분
    cold = {"저체온", "추위"}
    assert run(ctx(300, active=cold, applied={"추위": 0, "저체온": 120}), clear=["저체온"]).clear == []
    warmed = run(ctx(130, active={"저체온"}, applied={"저체온": 120}, clock={"cleared": {"추위": START + 100}}))
    assert warmed.clear == []                                                                    # 추위 뗀 지 30분
    later = run(ctx(160, active={"저체온"}, applied={"저체온": 120}, clock={"cleared": {"추위": START + 100}}))
    assert names(later.clear) == ["저체온"]                                                       # 60분 → 코드 해제


def test_clearing_cold_records_its_time_for_the_deep_status():
    d = run(ctx(50, active={"추위"}, applied={"추위": 0}), clear=["추위"])
    assert names(d.clear) == ["추위"] and d.clock["cleared"]["추위"] == START + 50


def test_heat_exhaustion_and_heat_shortens_dehydration():
    assert "열탈진" in names(run(ctx(180, active={"더위"}, applied={"더위": 0})).apply)
    both = run(ctx(60, active={"더위", "탈수"}, applied={"더위": 0, "탈수": 0})).apply
    assert "열탈진" in names(both)
    assert "탈수" not in names(run(ctx(200), apply=["탈수"]).apply)                              # 서늘하면 6시간
    assert "탈수" in names(run(ctx(200, active={"더위"}, applied={"더위": 0}), apply=["탈수"]).apply)  # 더위 3시간


def test_panic_no_longer_clears_by_waiting_and_wounds_can_heal_without_supplies():
    assert run(ctx(30, active={"공황"}, applied={"공황": 0}, turn_minutes=30, activity="WAIT")).clear == []
    assert run(ctx(60, active={"출혈"}, applied={"출혈": 0}), clear=["출혈"]).clear == []
    assert names(run(ctx(130, active={"출혈"}, applied={"출혈": 0}), clear=["출혈"]).clear) == ["출혈"]
    assert run(ctx(2000, active={"부상"}, applied={"부상": 0}), clear=["부상"]).clear == []
    assert names(run(ctx(4400, active={"부상"}, applied={"부상": 0}), clear=["부상"]).clear) == ["부상"]


# ── D1: 감염 잠복(70%, 12~48시간) ────────────────────────────────

def test_latent_roll_is_deterministic_and_matches_authored_chance_and_onset():
    lat = MERGED["감염 의심"]["latent"]
    assert lat == {"target": "감염", "chance": 70, "onset_minutes": [720, 2880]}
    rolls = [SR.latent_roll(f"s{i}", "P", "감염 의심", 100, lat) for i in range(400)]
    hits = [r for r in rolls if r["due"] is not None]
    assert 0.6 < len(hits) / len(rolls) < 0.8
    assert all(100 + 720 <= r["due"] <= 100 + 2880 for r in hits)
    assert SR.latent_roll("s1", "P", "감염 의심", 100, lat) == rolls[1]


def test_bite_records_latent_and_infection_follows_only_if_rolled():
    seeds = {True: None, False: None}
    for i in range(50):
        r = SR.latent_roll(f"s{i}", "P", "감염 의심", START + 10, MERGED["감염 의심"]["latent"])
        seeds.setdefault(r["due"] is not None, f"s{i}")
        if seeds[r["due"] is not None] is None:
            seeds[r["due"] is not None] = f"s{i}"
    bitten = run(ctx(10, seed=seeds[True]), apply=["감염 의심"])
    rec = bitten.clock["latent"]["감염"]
    assert names(bitten.apply) == ["감염 의심"] and rec["due"] is not None
    clock = {**bitten.clock}
    onset = rec["due"] - START
    sus = {"감염 의심"}
    early = SR.decide(MERGED, SR.Ctx(now=rec["due"] - 1, start_minute=START, active=frozenset(sus), clock=clock), "P",
                      [], [], ())
    assert "감염" not in names(early.apply)
    hit = SR.decide(MERGED, SR.Ctx(now=rec["due"], start_minute=START, active=frozenset(sus), clock=clock), "P",
                    [], [], ())
    assert "감염" in names(hit.apply) and "감염" not in (hit.clock.get("latent") or {})
    assert 720 <= onset - 10 <= 2880
    lucky = run(ctx(10, seed=seeds[False]), apply=["감염 의심"])
    assert lucky.clock["latent"]["감염"]["due"] is None
    gone = SR.decide(MERGED, SR.Ctx(now=START + 10 + 2880, start_minute=START, active=frozenset(sus),
                                    clock=lucky.clock), "P", [], [], ())
    assert names(gone.clear) == ["감염 의심"] and "감염" not in names(gone.apply)       # 48시간 무증상 — 해제


# ── 추출 프롬프트 · 판단 누락(D4 가) ─────────────────────────────────

def test_extraction_prompt_lists_every_active_status_with_clear_condition_and_elapsed_time():
    s = SimpleNamespace(scenario_data=YD, statuses={"테스터": ["추위", "저체온"]}, npcs={},
                        world_clock={"minute_index": START + 130},
                        status_clock={"테스터": {"applied": {"추위": START, "저체온": START + 120}}})
    text = core.build_extraction_limits(s)
    assert "하나도 빠짐없이" in text
    assert "테스터: 추위(130분째) — 해제=[추위를 덜 수단을 무엇이든" in text
    assert "테스터: 저체온(10분째) — 코드 판정" in text
    assert "- 저체온 (코드 판정 — 점수를 넣지 말 것)" in text


def test_missing_judgment_keeps_status_and_is_reported(monkeypatch):
    plan = SimpleNamespace(status_apply=[], status_clear=[], diagnostics={}, item_plan=None,
                           result={"status_scores": [{"target": "테스터", "status": "젖음", "score": 50}]})
    sess = SimpleNamespace(scenario_data=YD, players={"1": {"name": "테스터"}},
                           statuses={"테스터": ["추위", "젖음", "저체온"]}, status_clock={}, narrative_seed="x")
    monkeypatch.setattr(TW, "status_turn_facts", lambda s, p: {"now": START, "before": START, "start": START,
                                                               "place": None, "daypart": None, "activity": None})
    notes = SR.resolve_turn(sess, SimpleNamespace(extraction_plan=plan))
    assert "추위 판단 누락(유지)" in notes
    assert not any("젖음 판단 누락" in n or "저체온 판단 누락" in n for n in notes)   # 점수 있음 · 심화는 코드 판정
    assert plan.status_clear == [] and plan.diagnostics["status_rules"] == notes
    lines = core.master_report._extraction_lines(SimpleNamespace(extraction_plan=plan, extraction_text=None), [])
    assert any("추위 판단 누락" in x for x in lines)


async def test_dynamic_turn_applies_cold_from_extraction_and_tracks_it(drig):
    r = drig
    r.make()
    r.sess.statuses = {"테스터": []}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({}, status_scores=[{"target": "테스터", "status": "추위", "score": 85}])]
    tx = await turn(r, "젖은 채로 바닷바람을 맞으며 서 있는다")
    assert tx.status == core.turn_transaction.TurnStatus.COMMITTED
    assert "추위" in r.sess.statuses["테스터"]
    assert "추위" in r.sess.status_clock["테스터"]["applied"]
