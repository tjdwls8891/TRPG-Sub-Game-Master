# -*- coding: utf-8 -*-
"""`!게이트` — 오너 전용 지시층위 품질 게이트 실행(상한 검사·별도 프로세스·결과 첨부). 실제 게이트 대신 대역 스크립트."""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

pytestmark = pytest.mark.policy

FAKE = r'''
import sys, os
out = sys.argv[sys.argv.index("--out") + 1]
cap = sys.argv[sys.argv.index("--cap-krw") + 1]
os.makedirs(out, exist_ok=True)
print("[게이트] 영도 턴0 · 90원 · 누적 90원", flush=True)
open(os.path.join(out, "summary.md"), "w", encoding="utf-8").write("# 지시층위 품질 게이트 결과\n상한 " + cap + "\n")
open(os.path.join(out, "results.jsonl"), "w", encoding="utf-8").write("{}\n")
'''


class Msg:
    def __init__(self, log):
        self.log = log

    async def edit(self, content=None, **k):
        self.log.append(("edit", content))


class Ctx:
    def __init__(self):
        self.log = []

    async def send(self, content=None, **k):
        self.log.append(("send", content, [f.filename for f in k.get("files") or ()]))
        return Msg(self.log)


def _cog():
    import cogs.system as S
    return S, S.SystemCog.__new__(S.SystemCog)


async def test_gate_rejects_cap_over_approved_limit():
    S, cog = _cog()
    ctx = Ctx()
    await S.SystemCog.instruction_gate.callback(cog, ctx, 5000)
    assert "상한은 1~3,000원" in ctx.log[-1][1] and not getattr(cog, "_gate_running", False)


async def test_gate_rejects_unknown_scenario():
    S, cog = _cog()
    ctx = Ctx()
    await S.SystemCog.instruction_gate.callback(cog, ctx, 1000, 3, "무협")
    assert "알 수 없는 시나리오: 무협" in ctx.log[-1][1]


async def test_gate_runs_tool_in_subprocess_and_attaches_results(tmp_path, monkeypatch):
    S, cog = _cog()
    fake = tmp_path / "fake_gate.py"
    fake.write_text(FAKE, encoding="utf-8")
    real = asyncio.create_subprocess_exec
    seen = {}

    async def spawn(exe, script, *args, **kw):
        seen["args"] = (exe, script, args)
        return await real(sys.executable, str(fake), *args, **{**kw, "cwd": str(tmp_path)})
    monkeypatch.setattr(S.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(S, "__file__", str(tmp_path / "cogs" / "system.py"))     # 결과 폴더를 tmp 아래로
    ctx = Ctx()
    await S.SystemCog.instruction_gate.callback(cog, ctx, 3000, 3, "설화재", "균열")
    assert seen["args"][2][-3:] == ("--scenarios", "설화재", "균열") and "3" in seen["args"][2]
    assert seen["args"][0] == sys.executable and seen["args"][1].endswith("instruction_gate.py")
    assert "--cap-krw" in seen["args"][2] and "3000" in seen["args"][2]
    final = [e for e in ctx.log if e[0] == "send" and e[2]][-1]
    assert final[2] == ["summary.md", "results.jsonl"] and "상한 3000" in final[1]
    assert ("edit", "✅ 게이트 완료") in ctx.log and cog._gate_running is False
    assert (tmp_path / "data" / "instruction_gate").is_dir()
