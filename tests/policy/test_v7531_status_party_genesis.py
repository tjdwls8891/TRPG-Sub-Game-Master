# -*- coding: utf-8 -*-
"""v7.53.1 — 네 시나리오 작동 검토 결함(F1~F4)을 실제 자동 턴(가짜 provider) 커밋 경로로 고정한다.

F1 코드 단계 상승으로 새로 붙는 상태(첫 전투 부상·첫 마력 비용)가 적용된다 · F2 시작 사건 기록(PC 기록 등)이 반영된다 ·
F3 치유는 효과 없는 처치를 고르지 않는다 · F4 PC와 같은 장소의 파티원은 PC와 함께 쉬며 회복한다.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

import core
from core import party as PA
from core import player_world as PW
from core import world_lifecycle as WL
from tests.conftest import REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import H05Provider, _json, drig, extraction, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


def _judge(*, wait=None, place=None):
    def route(kwargs):
        text = H05Provider._text(kwargs)
        ai = {"movement_kind": "NONE", "time_kind": "NONE"}
        if place:
            m = re.search(r"(P\d+)=" + re.escape(place) + r"\b", text)
            ai.update({"movement_kind": "DESTINATION", "destination": m.group(1) if m else "NONE"})
        if wait:
            ai.update({"time_kind": "DURATION", "duration_minutes": wait, "activity": "WAIT"})
        return _json({"player_intent": "선언", "situation_read": "상황", "action": "PROCEED", "reasoning": "t",
                      "bridge_message": "", "rolls": [], "action_intent": ai})
    return route


async def _boot(r, name="균열"):
    raw = json.loads(pathlib.Path(REPO_ROOT, "scenarios", f"{name}.json").read_text(encoding="utf-8"))
    s = r.sess
    s.scenario_id, s.scenario_data = name, raw
    uid = next(iter(s.players))
    s.players[uid]["name"] = "테스터"
    s.players[uid]["profile"] = {**{k: 11 for k in raw.get("ability_stats") or ()}, "이름": "테스터", "스킬": "",
                                 "직업": next(iter(raw.get("jobs") or {}), "")}
    res = await WL.initialize_world(r.bot, s, raw["start_frames"][0])
    assert res.outcome == WL.INIT_OK, res.reason
    WL.mark_open(s)
    PW._DEF_CACHE.clear()
    return s


async def _turn(r, route):
    r.prov.routes["judgment"] = route
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "선언")


async def test_f2_genesis_pc_record_reaches_profile(drig):
    s = await _boot(drig)
    assert s.players[next(iter(s.players))]["profile"]["등급"] == 1          # gy_hunter 시작 기록(등급 1)


async def test_f1_first_combat_wound_is_applied(drig, monkeypatch):
    s = await _boot(drig)
    import core.turn_world as TW
    monkeypatch.setattr(TW, "combat_hits", lambda d, ss, rolls: {"테스터": 1})
    await _turn(drig, _judge(wait=1))
    assert "부상" in s.statuses["테스터"] and s.status_clock["테스터"]["stage"]["부상"] == 1
    await _turn(drig, _judge(wait=1))
    assert s.status_clock["테스터"]["stage"]["부상"] == 2                       # 쌓인다


def _join_guild(s):
    s.companions = ["서아름"]
    s.spatial_state["placements"]["gy.npc.seo_areum"] = dict(s.spatial_state["placements"][PW.session_world_context(s).lead_pc])


async def test_f1_f3_f4_healer_spends_once_then_rests_back(drig):
    s = await _boot(drig)
    _join_guild(s)
    now = s.world_clock["minute_index"]
    s.statuses["테스터"] = ["부상"]
    s.status_clock["테스터"] = {"applied": {"부상": now}, "stage": {"부상": 2}}
    await _turn(drig, _judge(wait=1))
    assert s.status_clock["테스터"]["stage"]["부상"] == 1                       # 처치 2 → 1
    assert "마력 소모" in s.statuses["서아름"]                                    # F1: 첫 비용이 붙는다
    await _turn(drig, _judge(wait=1))
    assert s.status_clock["서아름"]["stage"]["마력 소모"] == 1                   # F3: 처치한 1단계는 다시 고르지 않는다
    await _turn(drig, _judge(wait=30))
    assert "마력 소모" not in (s.statuses.get("서아름") or [])                   # F4: PC와 함께 쉬어 회복(1단계 → 해제)


def test_f3_heal_target_skips_treated_first_stage():
    merged = core.get_merged_status_effects({"scenario_schema": {"capabilities": {"mortality": 1}}})
    sts = {"P": ["부상"]}
    assert PA.heal_target(["P"], "응급처치", merged, sts, {"P": {"stage": {"부상": 1}}}) == "P"
    assert PA.heal_target(["P"], "응급처치", merged, sts, {"P": {"stage": {"부상": 1}, "cared": {"부상": 5}}}) is None
    assert PA.heal_target(["P"], "응급처치", merged, sts, {"P": {"stage": {"부상": 2}, "cared": {"부상": 5}}}) == "P"
