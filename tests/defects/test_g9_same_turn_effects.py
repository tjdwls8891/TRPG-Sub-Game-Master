# -*- coding: utf-8 -*-
"""G9 — 같은 턴의 효과는 같은 세계 상태를 기준으로 계산된다(리팩터링 T-7에서 해소 — 명세 v2 V2 가).

치유 대상은 이번 턴 전투 부상까지 본 상태로 고르고(turn_world.party_heal), 코드가 새로 붙이는 상태(전투 부상·비용)는
상태 결정 안에서 붙어 같은 턴 처치가 본다(vitals.logic.decide bumped_new) — 이번 턴에 다친 파티원은 곁의 치유사가
같은 턴에 치유한다.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator

pytestmark = pytest.mark.policy
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "golden" / "scripts" / "균열.json"


async def test_heal_sees_injury_dealt_in_the_same_turn(drig):
    script = json.loads(SCRIPT.read_text(encoding="utf-8"))
    sim = Simulator(drig)
    await sim.boot(script)
    for st in script["turns"][:10]:
        await sim.step(st)
    s = drig.sess
    assert "부상" not in (s.statuses.get("한유진") or ())               # 턴 전: 탱커 무상
    assert "서아름" in s.companions                                     # 치유사 동행, 다른 부상자 없음
    await sim.step(script["turns"][10])                                 # 이번 턴: 막기로 탱커가 부상
    assert "부상" in s.statuses["한유진"]
    assert "부상" in ((s.status_clock.get("한유진") or {}).get("cared") or {}), \
        "같은 턴에 생긴 부상을 같은 턴 치유가 보지 못했다"
