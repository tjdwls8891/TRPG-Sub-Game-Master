# -*- coding: utf-8 -*-
"""G9 — 같은 턴의 효과가 서로 다른 세계 상태를 기준으로 계산된다(리팩터링 명세 v2 V2 가: T-7에서 정리).

치유 대상은 턴 전 상태로 고르고, 같은 턴 전투 부상은 커밋에서 붙는다 — 이번 턴에 새로 다친 파티원은
치유할 수 있는 치유사가 곁에 있어도 다음 턴에야 치유된다. 효과 결정을 '계획 → 확정' 두 곳으로 모으면
(확정의 치유가 계획의 부상을 본다) 이 테스트가 XPASS가 되어 실패한다 — 그때 xfail을 걷고 골든을 다시 만든다.
이 테스트는 T-7(의도된 동작 변경)을 잊지 않게 하는 장치다. 지우지 말 것.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator

pytestmark = pytest.mark.defect
SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "golden" / "scripts" / "균열.json"


@pytest.mark.xfail(strict=True, reason="G9 — 리팩터링 T-7(효과 결정 시점 계획·확정 두 곳)에서 해소")
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
