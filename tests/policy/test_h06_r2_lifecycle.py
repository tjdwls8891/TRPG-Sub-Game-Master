# -*- coding: utf-8 -*-
"""H06 R2-01~05 — 생애주기 fail-closed · strict OPEN/CLOSED · 실제 종료 · 시작 상태 단일 writer ·
genesis 정체성 strict 저장 · 저작 행위자 등록.
"""
from __future__ import annotations

import copy
import json

import pytest

import core
from core import canonical_mutation as CM
from core import cache_lifecycle as CLC
from core import player_world as PW
from core import spatial as SP
from core import turn_transaction as tt
from core import world_lifecycle as WL
from core import world_state as WS
from tests.conftest import GAME_CH, PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_cache_lifecycle import _ink_rows, cbot, clock, fresh  # noqa: F401
from tests.policy.test_commit_recovery import _restart
from tests.policy.test_h06a_lifecycle import _cog, dyn_raw, journal_inits, play_intro, setup_session, world_of
from tests.policy.test_h06c_cache_source import _bind, seen  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

PC = f"pc_{PLAYER_UID}"
TWO = {PLAYER_UID: {"name": "가"}, "777": {"name": "나"}}


def _disk(s):
    return json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))


def _stubs(r, monkeypatch, *, intro_ok=True):
    cog = _cog(r)
    calls = {"upload": 0, "intro": 0, "round": 0}

    async def _upload(session, **kw):
        calls["upload"] += 1
        session.cache_name = "caches/ok"
        return True

    async def _intro(session, instruction, **kw):
        calls["intro"] += 1
        return {"ok": intro_ok, "ai_text": "인트로" if intro_ok else "", "error": None if intro_ok else "x"}

    async def _round(session):
        calls["round"] += 1
    monkeypatch.setattr(cog, "upload_cache", _upload)
    monkeypatch.setattr(r.bot.get_cog("GameCog"), "_execute_proceed", _intro)
    monkeypatch.setattr(r.bot.get_cog("GMCog"), "_init_narrative_and_start", _round)
    return cog, calls


async def _drain():
    import asyncio
    for _ in range(3):
        await asyncio.sleep(0)


# ── R2-01 ─────────────────────────────────────────────────────

async def test_r2_01_init_conflict_fails_closed_no_intro_no_legacy_progress(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    s.actor_registry = {PC: {"kind": "PC", "display_name": "x", "source_kind": "PLAYER",
                             "source_key": PLAYER_UID, "aliases": []}}      # 초기화 충돌
    cog, calls = _stubs(r, monkeypatch)
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))
    await _drain()
    assert calls == {"upload": 0, "intro": 0, "round": 0}
    assert WL.init_failed(s) and _disk(s)["world_initialization"]["status"] == "FAILED"
    assert s.world_lifecycle is None and s.definition_revision is None
    await r.gm._process_actions(s, "주변을 둘러본다", r.bot.get_channel(s.master_ch_id))
    assert tt.get_active_transaction(s) is None                           # LEGACY 진행 0회


async def test_r2_01_intro_failure_keeps_initialized_and_no_first_round(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    s.gm_active = True
    cog, calls = _stubs(r, monkeypatch, intro_ok=False)
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))
    await _drain()
    assert calls["intro"] == 1 and calls["round"] == 0
    assert s.world_lifecycle == WL.LIFECYCLE_INITIALIZED and not PW.session_world_context(s).dynamic


# ── R2-02 ─────────────────────────────────────────────────────

async def test_r2_02_legacy_end_session_closes_durably_and_deactivates(fresh, cbot, clock, seen):
    s = fresh
    await CLC.open_window(cbot, s)
    assert any(v is s for v in cbot.active_sessions.values())
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"] and s.world_lifecycle == WL.LIFECYCLE_CLOSED and _disk(s)["world_lifecycle"] == "CLOSED"
    assert not any(v is s for v in cbot.active_sessions.values())          # game/master/display 매핑 제거
    assert WL.blocked_reason(s)


async def test_r2_02_dynamic_close_durable_and_world_unchanged(fresh, cbot, clock, seen):
    s = fresh
    _bind(s)
    s.world_clock = {"minute_index": 1440 * 1000, "start_minute_index": 1440 * 1000}
    await CLC.open_window(cbot, s)
    world = world_of(s)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"] and _disk(s)["world_lifecycle"] == "CLOSED"
    assert world_of(s) == world


async def test_r2_02_strict_closed_save_failure_not_success_and_retry_no_double_refund(
        fresh, cbot, clock, seen, monkeypatch):
    s = fresh
    await CLC.open_window(cbot, s)
    clock.advance(600)
    import core.io as cio
    orig = cio.save_session_data_strict
    state = {"n": 0}

    async def _flaky(bot, session):
        if session.world_lifecycle == WL.LIFECYCLE_CLOSED and state["n"] == 0:   # CLOSED 확정 저장만 1회 실패
            state["n"] += 1
            raise cio.SessionPersistenceError("strict 저장 실패(주입)")
        return await orig(bot, session)
    monkeypatch.setattr(cio, "save_session_data_strict", _flaky)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"] and s.world_lifecycle != WL.LIFECYCLE_CLOSED
    assert any(v is s for v in cbot.active_sessions.values())              # 성공 보고·비활성화 없음
    refunds = [x for x in _ink_rows() if x["direction"] == "CREDIT"]
    finals = [e for e in CLC.load_events(s.session_id) if e["type"] == "FINALIZED"]
    res2 = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res2["ok"] and _disk(s)["world_lifecycle"] == "CLOSED"
    assert [x for x in _ink_rows() if x["direction"] == "CREDIT"] == refunds     # 추가 환급 없음
    assert [e for e in CLC.load_events(s.session_id) if e["type"] == "FINALIZED"] == finals   # 추가 finalize 없음


async def test_r2_02_restart_does_not_reregister_closed_session(rig):
    r = rig
    s = setup_session(r, players=TWO)
    s.cache_name = None
    res = await WL.close_session(r.bot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"]
    import os
    os.makedirs("scenarios", exist_ok=True)
    json.dump(s.scenario_data, open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8"), ensure_ascii=False)
    bot2 = FakeBot(strict=False)
    bot2.cost_ledger = r.bot.cost_ledger
    await core.restore_sessions_from_disk(bot2)
    assert GAME_CH not in bot2.active_sessions and not bot2.active_sessions
    assert _disk(s)["world_lifecycle"] == "CLOSED"                        # 저장 데이터 보존


# ── R2-03 ─────────────────────────────────────────────────────

CONFLICTING = {"id": "harbor", "facts": {"location": "성당", "date": "1999/01/01", "time": "day"}}


async def test_r2_03_dynamic_start_owned_by_world_start_not_legacy_facts(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    cog, calls = _stubs(r, monkeypatch)
    await play_intro(cog, s, CONFLICTING, r.bot.get_channel(s.game_ch_id))
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN
    assert SP.placement_of(s, PC) == SP.at_place("항구")                   # authored world_start
    tl = s.world_timeline
    assert tl["current_location"] == "항구" and "1999" not in str(tl.get("current_date"))
    assert tl["current_date"].startswith("2031-11-03")                    # 정본 시계에서 파생


# ── R2-04 ─────────────────────────────────────────────────────

async def test_r2_04_genesis_id_in_first_strict_persistence(rig):
    r = rig
    s = setup_session(r)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    d = _disk(s)                                                          # 호출자의 추가 저장 없음
    assert d["world_initialization"]["genesis_version_id"] == d["world_version"]["version_id"]
    _, s2 = await _restart(r)
    assert s2.world_initialization["genesis_version_id"] == s2.world_version["version_id"]


async def test_r2_04_genesis_id_survives_post_persist_recovery(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    orig = CM._append

    def _crash_after_persist(sid, entry):
        if entry.get("phase") in ("PERSISTED", "COMMITTED"):
            raise CM.CanonicalMutationJournalError("크래시 모사")
        return orig(sid, entry)
    monkeypatch.setattr(CM, "_append", _crash_after_persist)
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_OK and res.mutation.outcome == CM.OUTCOME_RECOVERY_PENDING
    monkeypatch.setattr(CM, "_append", orig)
    d = _disk(s)
    assert d["world_initialization"]["genesis_version_id"] == d["world_version"]["version_id"]
    _, s2 = await _restart(r)
    assert CM.classify_recovery(s2) == []
    assert s2.world_initialization["genesis_version_id"] == s2.world_version["version_id"]


# ── R2-05 ─────────────────────────────────────────────────────

async def test_r2_05_authored_actors_registered_at_genesis_without_placement(rig):
    r = rig
    s = setup_session(r)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    D = core.definition_snapshot.read_snapshot(s.session_id).definition
    authored = {a: d for a, d in D["actor_definitions"].items() if d["kind"] == "NPC"}
    assert authored
    for aid, d in authored.items():
        e = s.actor_registry[aid]
        assert e == {"kind": "NPC", "display_name": d["display_name"], "source_kind": "AUTHORED_NPC",
                     "source_key": d["legacy_key"], "aliases": list(d.get("aliases") or [])}
        assert SP.placement_of(s, aid) is None                             # 위치 추론 없음
    n = len(s.actor_registry)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_ALREADY
    assert len(s.actor_registry) == n and len(journal_inits(s)) == 1
