# -*- coding: utf-8 -*-
"""H06-G — 세션 종료·환급 안전 지점.

  · 진행 중 턴(재시도 대기 포함)·재서술/되감기·미종결 세계 변이 중 종료 → 거부(강제 취소 없음, 재무 효과 없음).
  · 유휴 종료 → 캐시 단일 finalizer · 미사용 선불 환급 정확히 한 번 · CLOSED 영속 · 세계 불변.
  · 실제 비용이 선불을 넘어도 추가 플레이어 청구 없음(운영자 부담).
  · 환급이 durable하게 끝난 뒤 저장/표시가 실패해도 재시도는 중복 환급하지 않는다.
  · 되감기(가역 복원)는 종료·환급 이력을 되돌리지 않는다.
"""
from __future__ import annotations

import copy

import pytest

import core
from core import cache_lifecycle as CLC
from core import canonical_mutation as CM
from core import ink_transactions as IT
from core import turn_history as TH
from core import turn_transaction as tt
from core import world_lifecycle as WL
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_cache_lifecycle import _bal, _ink_rows, cbot, clock, fresh  # noqa: F401
from tests.policy.test_h06c_cache_source import _bind, seen  # noqa: F401

pytestmark = pytest.mark.policy


def _world(s):
    return {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}


def _refunds():
    return [r for r in _ink_rows() if r["direction"] == "CREDIT"]


async def _opened(fresh, cbot):
    s = fresh
    _bind(s)
    s.world_clock = {"minute_index": 1440 * 1000, "start_minute_index": 1440 * 1000}
    s.world_schedule = {"later": {"schedule_id": "later", "due_minute": 1440 * 1000 + 60, "kind": "EVENT_WAKEUP",
                                  "owner_ref": "template:none", "payload_ref": "x", "created_by_version": None,
                                  "order_key": "later"}}
    await CLC.open_window(cbot, s)
    return s


async def test_close_rejected_during_active_turn_without_cancelling(fresh, cbot, clock, seen):
    s = await _opened(fresh, cbot)
    tx = tt.get_or_begin_turn_transaction(s, "선언")
    rows = len(_ink_rows())
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"] and "턴" in res["reason"]
    assert s.cache_name and len(_ink_rows()) == rows                  # 캐시·재무 효과 없음
    assert tt.get_active_transaction(s) is tx and not tt.is_terminal(tx.status)   # 강제 취소 없음
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN


async def test_close_rejected_during_history_op_or_unfinished_world_mutation(fresh, cbot, clock, seen):
    s = await _opened(fresh, cbot)
    TH.write_op(s.session_id, {"op": "RERENDER", "op_id": "x", "old_tx": "a", "new_tx": "b",
                               "logical_turn": 1, "gm_turn": 1, "old_marker": None, "started_at": 0})
    assert not (await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END))["ok"]
    TH.clear_op(s.session_id)
    CM._append(s.session_id, {"mutation_id": "cmu_x", "phase": "PREPARED", "cause_kind": "ADMIN_EDIT",
                              "cause_id": "admin:x", "batch_digest": "d", "definition_revision": None,
                              "source_world_version": s.world_version, "target_world_version": {"version_id": "z"},
                              "changed_domains": []})
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert not res["ok"] and "세계 변이" in res["reason"] and s.cache_name


async def test_idle_close_refunds_once_marks_closed_and_preserves_world(fresh, cbot, clock, seen):
    s = await _opened(fresh, cbot)
    world = _world(s)
    clock.advance(600)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"] and res["refund"].get(PLAYER_UID, 0) > 0
    assert s.world_lifecycle == WL.LIFECYCLE_CLOSED and s.cache_name is None
    assert _world(s) == world                                         # 시계·예약·정본 불변(따라잡기 없음)
    n = len(_refunds())
    again = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert again["ok"] and len(_refunds()) == n == 1                  # 정확히 한 번


async def test_cost_above_prepayment_is_operator_shortfall_not_extra_charge(fresh, cbot, clock, seen):
    s = await _opened(fresh, cbot)
    debits = [r for r in _ink_rows() if r["direction"] == "DEBIT"]
    clock.advance(10 * 24 * 3600)                                      # 선불 창을 크게 넘김
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"]
    assert [r for r in _ink_rows() if r["direction"] == "DEBIT"] == debits   # 추가 청구 없음
    assert not res["refund"] or max(res["refund"].values()) == 0


async def test_failure_after_durable_refund_does_not_refund_twice_on_retry(fresh, cbot, clock, seen, monkeypatch):
    s = await _opened(fresh, cbot)
    clock.advance(600)
    import core.io as cio
    orig = cio.save_session_data
    state = {"n": 0}

    async def _flaky(bot, session):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("저장 실패(크래시 모사)")
        return await orig(bot, session)
    monkeypatch.setattr(cio, "save_session_data", _flaky)
    with pytest.raises(RuntimeError):
        await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert len(_refunds()) == 1                                       # 환급은 이미 durable
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    assert res["ok"] and len(_refunds()) == 1 and s.world_lifecycle == WL.LIFECYCLE_CLOSED


async def test_rewind_restore_cannot_undo_close_financial_history(fresh, cbot, clock, seen):
    s = await _opened(fresh, cbot)
    snapshot = TH.capture_reversible(s)
    clock.advance(600)
    await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)
    rows, bal = _ink_rows(), _bal()
    TH.restore_reversible(s, snapshot)                                # 가역 정본 복원(되감기 적용 경로)
    assert s.world_lifecycle == WL.LIFECYCLE_CLOSED
    assert _ink_rows() == rows and _bal() == bal


async def test_end_session_command_rejects_during_active_turn_before_locking(fresh, cbot, clock, seen):
    import types
    import cogs.system as system_mod
    s = await _opened(fresh, cbot)
    s.master_ch_id = 515151
    cbot.active_sessions[515151] = s
    tt.get_or_begin_turn_transaction(s, "선언")
    sent = []

    async def _send(*a, **k):
        sent.append(a[0] if a else k)
    ctx = types.SimpleNamespace(channel=types.SimpleNamespace(id=515151), send=_send, guild=None)
    await system_mod.SystemCog.end_session.callback(system_mod.SystemCog(cbot), ctx)
    assert any("종료할 수 없습니다" in str(x) for x in sent)
    assert s.cache_name and s.world_lifecycle == WL.LIFECYCLE_OPEN
