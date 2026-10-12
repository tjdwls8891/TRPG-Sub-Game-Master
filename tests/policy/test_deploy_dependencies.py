"""!배포 — requirements.txt가 바뀌면 실행 중인 파이썬의 pip로 설치하고, 실패하면 재시작하지 않는다."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import cogs.system as system_mod

pytestmark = pytest.mark.policy


class _Msg:
    def __init__(self):
        self.contents = []

    async def edit(self, content=None, **_k):
        self.contents.append(content)


class _Ctx:
    def __init__(self):
        self.msg = _Msg()

    async def send(self, content=None, **_k):
        self.msg.contents.append(content)
        return self.msg


def _fake_run(changed: str, pip_ok: bool, calls: list):
    heads = iter(["aaa111", "bbb222"])

    def run(args, **_k):
        calls.append(list(args))
        if args[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="")
        if args[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout=next(heads))
        if args[:2] == ["git", "pull"]:
            return SimpleNamespace(returncode=0, stdout="ok")
        if args[:2] == ["git", "diff"]:
            return SimpleNamespace(returncode=0, stdout=changed)
        if args[1:3] == ["-m", "pip"]:
            return SimpleNamespace(returncode=0 if pip_ok else 1, stdout="pip out")
        raise AssertionError(args)
    return run


async def _deploy(monkeypatch, changed, pip_ok, mode):
    calls, restarted = [], []
    monkeypatch.setattr(system_mod.subprocess, "run", _fake_run(changed, pip_ok, calls))
    monkeypatch.setattr(system_mod.os, "execv", lambda *a: restarted.append(a))

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(system_mod.asyncio, "sleep", _noop)
    bot = SimpleNamespace(close=_noop)
    cog = system_mod.SystemCog.__new__(system_mod.SystemCog)
    cog.bot = bot
    ctx = _Ctx()
    await system_mod.SystemCog.deploy.callback(cog, ctx, mode)
    return calls, restarted, ctx.msg.contents[-1]


async def test_requirements_change_installs_with_running_python_then_restarts(monkeypatch):
    calls, restarted, last = await _deploy(monkeypatch, "core/x.py\nrequirements.txt", True, "재시작")
    assert [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"] in calls
    assert restarted and "의존성 설치 완료" in last


async def test_pip_failure_blocks_restart(monkeypatch):
    calls, restarted, last = await _deploy(monkeypatch, "requirements.txt", False, "재시작")
    assert not restarted and "의존성 설치 실패" in last


async def test_no_requirements_change_no_pip(monkeypatch):
    calls, restarted, _ = await _deploy(monkeypatch, "cogs/game.py", True, None)
    assert not any(c[1:3] == ["-m", "pip"] for c in calls) and not restarted


async def test_dependency_change_without_restart_mode_warns(monkeypatch):
    _calls, restarted, last = await _deploy(monkeypatch, "requirements.txt", True, None)
    assert not restarted and "재시작이 필요" in last
