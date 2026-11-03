# -*- coding: utf-8 -*-
"""지시층위 품질 게이트(T-7 V1 가) — 표본 요청 수집. 평소에는 건너뛴다(GATE_CAPTURE_OUT이 있을 때만).

골든 대본 4종을 가짜 provider로 돌리며, 지시층위·묘사층위를 모두 부른 턴마다 실제로 보낸 요청(내용·설정)과
그 세션의 캐시 본문(룰북 + 지시층위 지시문)·캐시 시스템 지시를 남긴다. 실제 호출은 tools/instruction_gate.py가 한다.
"""
from __future__ import annotations

import json
import os
import pathlib
import types

import pytest

from tests.conftest import REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator, _plain

pytestmark = pytest.mark.skipif(not os.environ.get("GATE_CAPTURE_OUT"), reason="게이트 표본 수집 전용")
SCRIPTS = pathlib.Path(REPO_ROOT, "tests", "golden", "scripts")
FAKE_INSTRUCTION = "선언대로 진행한다"           # 하네스 가짜 지시층위의 proceed_instruction


def _req(kwargs) -> dict:
    cfg = _plain(kwargs.get("config")) or {}
    for k in ("cached_content", "http_options"):
        cfg.pop(k, None)
    return {"model": kwargs.get("model"), "config": cfg, "contents": _plain(kwargs.get("contents"))}


async def _rulebook(session) -> str:
    from indaim.ai import cache as C
    stub = types.SimpleNamespace(genai_client=types.SimpleNamespace(models=types.SimpleNamespace(
        count_tokens=lambda **k: types.SimpleNamespace(total_tokens=10 ** 6))))
    _padded, _n, text = await C.build_scenario_cache_text(stub, C.DEFAULT_MODEL, session.scenario_data, session=session)
    return text


@pytest.mark.parametrize("name", ["영도", "설화재", "청람호", "균열"])
async def test_capture(drig, name):
    import prompts
    script = json.loads((SCRIPTS / f"{name}.json").read_text(encoding="utf-8"))
    sim = Simulator(drig)
    s = await sim.boot(script)
    out = {"scenario": name, "cache_system": prompts.SYSTEM_INSTRUCTION, "rulebook": await _rulebook(s), "cases": []}
    for i, st in enumerate(script["turns"]):
        s.current_turn_logs.append(f"[{sim.pc}]: {st.get('say', '')}")      # 운영의 채팅 기록과 같게
        log0 = len(drig.prov.raw)
        await sim.step(st)
        new = drig.prov.raw[log0:]
        logic = [k for k in new if drig.prov.kind_of(k) == "logic"]
        narr = [k for k in new if drig.prov.kind_of(k) == "narration"]
        if logic and narr:
            out["cases"].append({"turn": i, "say": st.get("say", ""), "logic": _req(logic[-1]),
                                 "narration": _req(narr[-1]), "fake_instruction": FAKE_INSTRUCTION})
    path = pathlib.Path(os.environ["GATE_CAPTURE_OUT"])
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{name}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    assert out["cases"]
