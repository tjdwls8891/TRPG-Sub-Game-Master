# -*- coding: utf-8 -*-
"""v8 — LEGACY 진행 방식 은퇴(사용자 결정 R6 나 · L1~L5 가): 1인 세션만 · LEGACY 시작 거부 · 남은 LEGACY 세션은 재시작 때 닫힘."""
from __future__ import annotations

import json
import pathlib

import pytest

import core
from core import cache_lifecycle as CLC
from core import world_lifecycle as WL
from tests.conftest import REPO_ROOT
from tests.policy.test_h06a_lifecycle import _cog, play_intro, setup_session
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


def test_only_dynamic_scenarios_are_shipped():
    names = sorted(p.stem for p in pathlib.Path(REPO_ROOT, "scenarios").glob("*.json"))
    assert names == sorted(["영도", "설화재", "청람호", "균열"])
    cat = json.loads(pathlib.Path(REPO_ROOT, "data", "scenario_catalog.json").read_text(encoding="utf-8"))
    assert sorted(k for k in cat if not k.startswith("_")) == names


def test_one_player_sessions_only(rig):
    s = setup_session(rig, players={})
    assert not WL.joins_blocked(s)
    s.players = {"1": {"name": "가"}}
    assert WL.joins_blocked(s)                                            # 두 번째 참가 거부


async def test_non_dynamic_start_is_refused_without_cache_or_intro(rig, monkeypatch):
    s = setup_session(rig, players={"1": {"name": "가"}, "2": {"name": "나"}})   # 다인 = 동적 세계 후보 아님
    cog = _cog(rig)
    seen = []

    async def _upload(session, **kw):
        seen.append("upload")
        return True
    monkeypatch.setattr(cog, "upload_cache", _upload)
    await play_intro(cog, s, {"id": "harbor"}, rig.bot.get_channel(s.game_ch_id))
    assert seen == [] and s.world_lifecycle is None
    assert any("시작할 수 없어" in str(m.content) for m in rig.gch.sent)


async def test_running_legacy_session_is_retired_through_close_path(rig, monkeypatch):
    s = rig.sess
    s.is_started = True
    assert WL.is_legacy_running(s)
    reasons = []

    async def _close(bot, session, *, reason):
        reasons.append(reason)
        session.world_lifecycle = WL.LIFECYCLE_CLOSED
        return {"ok": True}
    monkeypatch.setattr(WL, "close_session", _close)
    res = await WL.retire_legacy(rig.bot, s)
    assert res["ok"] and reasons == [CLC.REASON_RETIRED] and not WL.is_legacy_running(s)
    assert any("지원이 끝나" in str(m.content) for m in rig.gch.sent)
