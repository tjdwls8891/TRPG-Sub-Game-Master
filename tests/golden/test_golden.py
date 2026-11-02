# -*- coding: utf-8 -*-
"""골든 — 네 시나리오 대표 대본의 턴별 의미 스냅샷이 기준과 같아야 한다(리팩터링 안전망).

기준 갱신(의도한 변경일 때만): GOLDEN_UPDATE=1 python3 -m pytest tests/golden -q
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator

pytestmark = pytest.mark.policy
HERE = pathlib.Path(__file__).parent
SCRIPTS = sorted(p.stem for p in (HERE / "scripts").glob("*.json"))


@pytest.mark.parametrize("name", SCRIPTS)
async def test_golden(drig, name):
    script = json.loads((HERE / "scripts" / f"{name}.json").read_text(encoding="utf-8"))
    got = await Simulator(drig).run(script)
    exp_path = HERE / "expected" / f"{name}.json"
    snap = [{"say": t["say"], "view": t["view"], "out": t["out"]} for t in got]
    metrics = [{"say": t["say"], "calls": t["calls"]} for t in got]
    # 호출 원문 지문(T-5 게이트) — 같은 턴 안의 동시 호출은 순서가 흔들리므로 턴마다 정렬한 다중집합으로 본다
    raw = [{"say": t["say"], "calls": sorted(t["raw"], key=lambda d: json.dumps(d, sort_keys=True))} for t in got]
    raw_path = HERE / "expected" / f"{name}.calls.json"
    if os.environ.get("GOLDEN_UPDATE") or not exp_path.exists():
        exp_path.write_text(json.dumps(snap, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (HERE / "expected" / f"{name}.metrics.json").write_text(
            json.dumps(metrics, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if os.environ.get("GOLDEN_UPDATE") or not raw_path.exists():
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    exp = json.loads(exp_path.read_text(encoding="utf-8"))
    for i, (a, b) in enumerate(zip(snap, exp)):
        assert a == b, f"{name} 턴 {i} ({a['say']}) 불일치"
    assert len(snap) == len(exp)
    exp_raw = json.loads(raw_path.read_text(encoding="utf-8"))
    for i, (a, b) in enumerate(zip(raw, exp_raw)):
        assert a == b, f"{name} 턴 {i} ({a['say']}) provider 호출 원문 불일치"
    assert len(raw) == len(exp_raw)
