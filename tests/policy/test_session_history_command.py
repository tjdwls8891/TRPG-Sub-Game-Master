# -*- coding: utf-8 -*-
"""`!세션이력` — 세션 폴더 zip 묶음(분할·한도 초과 제외)과 오너 DM 전송."""
from __future__ import annotations

import asyncio
import io
import os
import zipfile
from types import SimpleNamespace

import pytest

from cogs import system as SYS

pytestmark = pytest.mark.policy


def _names(parts):
    out = []
    for _n, data in parts:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            out += z.namelist()
    return out


def _make(tmp_path, files):
    root = tmp_path / "sessions" / "s1"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return str(root)


def test_pack_keeps_every_file_and_nested_dirs_in_one_zip(tmp_path):
    path = _make(tmp_path, {"data.json": b'{"a":1}', "turn_history/t1.json": b"{}", "api_log.txt": b"x" * 1000})
    parts, skipped = SYS.pack_session_dir(path, "s1")
    assert [n for n, _ in parts] == ["session_s1.zip"] and skipped == []
    assert sorted(_names(parts)) == ["s1/_index/LOG_README.md", "s1/_index/manifest.json",   # P3 G6 자기 서술
                                     "s1/api_log.txt", "s1/data.json", "s1/turn_history/t1.json"]
    with zipfile.ZipFile(io.BytesIO(parts[0][1])) as z:
        assert z.read("s1/data.json") == b'{"a":1}'


def test_pack_splits_by_file_and_skips_file_over_limit(tmp_path):
    big = os.urandom(5000)                                  # 압축되지 않는 데이터
    path = _make(tmp_path, {"a.bin": big, "b.bin": big, "huge.bin": os.urandom(9000)})
    parts, skipped = SYS.pack_session_dir(path, "s1", limit=8000)   # 한도 > 동봉 README 압축 크기
    assert len(parts) > 1 and [n for n, _ in parts][0] == f"session_s1_part1of{len(parts)}.zip"
    assert all(len(d) <= 8000 for _n, d in parts)
    assert sorted(n for n in _names(parts) if "/_index/" not in n) == ["s1/a.bin", "s1/b.bin"]
    assert skipped == [("s1/huge.bin", 9000)]


class _Author:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, file=None):
        self.sent.append((content, file.filename if file else None))


class _Ctx:
    def __init__(self, channel_id=1):
        self.author, self.channel, self.guild, self.replies = _Author(), SimpleNamespace(id=channel_id), object(), []

    async def send(self, content):
        self.replies.append(content)


def _cog(sessions=None):
    return SYS.SystemCog(SimpleNamespace(active_sessions=sessions or {}))


def test_command_dms_current_session_and_rejects_unknown_or_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make(tmp_path, {"data.json": b"{}"})
    cog = _cog({1: SimpleNamespace(session_id="s1")})
    ctx = _Ctx()
    asyncio.run(SYS.SystemCog.session_history.callback(cog, ctx))
    assert ctx.author.sent == [("📦 세션 `s1` 이력", "session_s1.zip")] and ctx.replies == ["📬 DM으로 보냈습니다."]
    for bad in ("../sessions", "nope"):
        ctx = _Ctx()
        asyncio.run(SYS.SystemCog.session_history.callback(cog, ctx, bad))
        assert ctx.author.sent == [] and "찾을 수 없습니다" in ctx.replies[0]


def test_command_is_owner_only():
    checks = SYS.SystemCog.session_history.checks
    assert any("is_owner" in getattr(c, "__qualname__", "") for c in checks)
