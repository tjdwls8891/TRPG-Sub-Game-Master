# -*- coding: utf-8 -*-
"""H06 종료 durability — 환급·finalize가 durable하게 끝난 뒤 CLOSED 저장이 실패해도
세션은 다시 플레이 가능한 OPEN이 되지 않는다. 재시도·재시작은 CLOSED만 완성하고 환급을 반복하지 않는다.

OPEN → CLOSING(strict) → 캐시 단일 finalizer(정확히 한 번) → CLOSED(strict)
"""
from __future__ import annotations

import json
import os

import pytest

import core
from core import cache_lifecycle as CLC
from core import turn_transaction as tt
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_cache_lifecycle import _ink_rows, cbot, clock, fresh  # noqa: F401
from tests.policy.test_h06c_cache_source import _bind, seen  # noqa: F401

pytestmark = pytest.mark.policy


def _disk(s):
    return json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))


def _refunds():
    return [x for x in _ink_rows() if x["direction"] == "CREDIT"]


def _finals(s):
    return [e for e in CLC.load_events(s.session_id) if e["type"] == "FINALIZED"]


def _fail_strict_after_refund(monkeypatch):
    """캐시 finalize·환급이 끝난 뒤의 strict 저장(= CLOSED 확정)만 실패시킨다."""
    import core.io as cio
    orig = cio.save_session_data_strict
    state = {"armed": True, "failed": 0}

    async def _strict(bot, session):
        if state["armed"] and getattr(session, "world_lifecycle", None) == WL.LIFECYCLE_CLOSED:
            state["failed"] += 1
            raise cio.SessionPersistenceError("CLOSED strict 저장 실패(주입)")
        return await orig(bot, session)
    monkeypatch.setattr(cio, "save_session_data_strict", _strict)
    return state


async def _opened(fresh, cbot):
    s = fresh
    _bind(s)                                                              # DYNAMIC OPEN 세션
    await CLC.open_window(cbot, s)
    return s


async def test_post_refund_closed_save_failure_never_reopens_and_retry_completes(fresh, cbot, clock, seen,
                                                                                 monkeypatch):
    s = await _opened(fresh, cbot)
    clock.advance(600)
    state = _fail_strict_after_refund(monkeypatch)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"] and state["failed"] == 1
    assert len(_refunds()) == 1 and len(_finals(s)) == 1                   # 환급·finalize는 이미 durable
    # 플레이 가능한 OPEN으로 돌아가지 않는다(메모리·디스크 모두)
    assert s.world_lifecycle != WL.LIFECYCLE_OPEN and _disk(s)["world_lifecycle"] != WL.LIFECYCLE_OPEN
    assert WL.blocked_reason(s)                                           # 새 턴 차단
    assert not core.player_world.session_world_context(s).dynamic
    with pytest.raises(Exception):
        await CLC.open_window(cbot, s)                                    # 재개(캐시 재오픈) 차단
    assert len(_refunds()) == 1
    # 재시도 → CLOSED 확정, 추가 환급·finalize 0
    state["armed"] = False
    res2 = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res2["ok"] and _disk(s)["world_lifecycle"] == WL.LIFECYCLE_CLOSED
    assert len(_refunds()) == 1 and len(_finals(s)) == 1
    assert not any(v is s for v in cbot.active_sessions.values())


async def test_post_refund_closed_save_failure_blocks_join_and_turn(fresh, cbot, clock, seen, monkeypatch):
    import types
    import cogs.character as char_mod
    s = await _opened(fresh, cbot)
    s.world_initialization = {}                                           # LEGACY 세션으로도 성립해야 한다
    s.world_lifecycle = None
    _fail_strict_after_refund(monkeypatch)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"] and s.world_lifecycle is not None
    assert WL.blocked_reason(s)
    sent = []

    async def _send(*a, **k):
        sent.append(a[0] if a else k)
    ctx = types.SimpleNamespace(channel=types.SimpleNamespace(id=s.game_ch_id), send=_send,
                                author=types.SimpleNamespace(id=999001))
    cbot.active_sessions[s.game_ch_id] = s
    await char_mod.CharacterCog.join_session.callback(char_mod.CharacterCog(cbot), ctx, "난입")
    assert "999001" not in (s.players or {})                              # 추가 참가 차단
    assert tt.get_active_transaction(s) is None


async def test_restart_recovers_closing_to_closed_without_second_refund(fresh, cbot, clock, seen, monkeypatch):
    s = await _opened(fresh, cbot)
    clock.advance(600)
    state = _fail_strict_after_refund(monkeypatch)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"]
    assert _disk(s)["world_lifecycle"] == WL.LIFECYCLE_CLOSING            # durable 중간 상태
    refunds, finals = len(_refunds()), len(_finals(s))
    state["armed"] = False
    os.makedirs("scenarios", exist_ok=True)
    json.dump(s.scenario_data, open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8"), ensure_ascii=False)
    bot2 = FakeBot(strict=False)
    bot2.cost_ledger = cbot.cost_ledger
    await core.restore_sessions_from_disk(bot2)
    assert not any(getattr(v, "session_id", None) == s.session_id for v in bot2.active_sessions.values())
    assert _disk(s)["world_lifecycle"] == WL.LIFECYCLE_CLOSED
    assert len(_refunds()) == refunds == 1 and len(_finals(s)) == finals == 1


async def test_closing_entry_save_failure_rolls_back_safely_before_refund(fresh, cbot, clock, seen, monkeypatch):
    s = await _opened(fresh, cbot)
    import core.io as cio
    orig = cio.save_session_data_strict

    async def _strict(bot, session):
        if getattr(session, "world_lifecycle", None) == WL.LIFECYCLE_CLOSING:
            raise cio.SessionPersistenceError("CLOSING strict 저장 실패(주입)")
        return await orig(bot, session)
    monkeypatch.setattr(cio, "save_session_data_strict", _strict)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"]
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN and s.cache_name           # 환급 시작 전 — 안전 복귀
    assert _refunds() == [] and _finals(s) == []
