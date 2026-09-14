"""WP-01 — TurnTransaction 식별자 오케스트레이션 배선 검증.

정책 계약(TID-001~008, test_transaction_identity_policy.py)이 core.turn_transaction
모듈 자체를 검증한다면, 이 파일은 그 식별자가 **실제 자동 턴 오케스트레이션 경로를
통해** 배선되었음을 검증한다:

  W-01  실제 ROLL View 왕복에서 하나의 트랜잭션 ID가 살아남는다
        (_dispatch_rolls -> GMRollView -> _process_roll -> _continue_with_roll_results).
  W-02  ROLL 재개가 정규 턴 종료(_finish_proceed_and_continue)에 같은 ID를 전달한다.
  W-03  _process_actions 재진입(ASK/NARRATE 대기)이 같은 트랜잭션을 재사용한다.
  W-04  stale ROLL 콜백은 더 새로운 트랜잭션을 되살리지 않고 조용히 종료한다.

라이브 SDK·네트워크는 conftest가 차단한다. 모델 호출은 전부 스텁이다.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.policy


def _make_gm_cog(fake_bot):
    """생성자 부작용 없이 GMCog을 만든다. 락 레지스트리를 직접 채운다."""
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    cog._session_locks = {}
    return cog


_ROLL_SPEC = [{"char_name": "테스터", "stat": "근력", "sides": 20, "weight": 0}]


# ── W-01 ─────────────────────────────────────────────────────

async def test_w01_roll_view_round_trip_preserves_single_id(
        monkeypatch, wired_bot, session_auto_ready, game_channel):
    """W-01 — 창설된 ID가 ROLL View 왕복 전체에서 동일하게 유지된다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    tx = core.turn_transaction.get_or_begin_turn_transaction(sess)
    origin_id = tx.transaction_id

    # 실제 _dispatch_rolls가 View를 만들어 게임 채널에 전송한다.
    await cog._dispatch_rolls(sess, _ROLL_SPEC, "문을 연다", [],
                              transaction_id=origin_id)

    view = game_channel.sent[-1].view
    assert view is not None, "ROLL 프롬프트에 View가 실리지 않았습니다"
    assert view.transaction_id == origin_id, "View가 식별자를 운반하지 않습니다"

    # 버튼 콜백 경로(_process_roll)가 같은 ID를 재개 함수로 넘기는지 확인한다.
    forwarded = {}

    async def _fake_execute_rolls(session, rolls, game_ch):
        return ["[근력] 판정: 15 (성공)"]

    async def _rec_continue(session, player_message, roll_results, *,
                            transaction_id=None):
        forwarded["transaction_id"] = transaction_id

    monkeypatch.setattr(cog, "_execute_rolls", _fake_execute_rolls, raising=False)
    monkeypatch.setattr(cog, "_continue_with_roll_results", _rec_continue,
                        raising=False)

    await view._process_roll(game_channel)   # 내부 1.5s 하한 후 create_task
    await asyncio.sleep(0.05)                 # 스케줄된 재개 태스크 실행 대기

    assert forwarded.get("transaction_id") == origin_id, (
        "View 재개가 원 트랜잭션 ID를 전달하지 않았습니다")


# ── W-02 ─────────────────────────────────────────────────────

async def test_w02_continue_forwards_id_to_finish(
        monkeypatch, wired_bot, session_auto_ready):
    """W-02 — ROLL 재개가 같은 ID를 정규 턴 종료 경로로 넘긴다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    tx = core.turn_transaction.get_or_begin_turn_transaction(sess)
    core.turn_transaction.mark_waiting_for_roll(sess, tx.transaction_id)

    seen = {}

    async def _fake_gm_logic(session, player_message, roll_results, master_ch,
                             *, sim_result=None, action="PROCEED",
                             transaction_id=None):
        return {"action": "PROCEED", "proceed_instruction": "묘사를 이어간다",
                "event_assessment": "ongoing"}

    async def _rec_finish(session, instruction, master_ch, *,
                          event_assessment=None, transaction_id=None):
        seen["transaction_id"] = transaction_id

    monkeypatch.setattr(cog, "_call_gm_logic", _fake_gm_logic, raising=False)
    monkeypatch.setattr(cog, "_finish_proceed_and_continue", _rec_finish,
                        raising=False)

    await cog._continue_with_roll_results(
        sess, "문을 연다", ["[근력] 판정: 15"], transaction_id=tx.transaction_id)

    assert seen.get("transaction_id") == tx.transaction_id, (
        "ROLL 재개가 턴 종료에 원 ID를 전달하지 않았습니다")


# ── W-03 ─────────────────────────────────────────────────────

async def test_w03_process_actions_reentry_reuses_identity(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """W-03 — ASK/NARRATE 대기 후 재진입이 같은 트랜잭션을 재사용한다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    seen_ids = []

    async def _stub_loop(session, player_message, master_ch, *,
                         transaction_id=None):
        # 루프가 ASK로 대기 상태에 든 것처럼 표기한다(비종료).
        seen_ids.append(transaction_id)
        core.turn_transaction.mark_waiting_for_player(session, transaction_id)

    monkeypatch.setattr(cog, "_run_gm_logic_loop", _stub_loop, raising=False)

    await cog._process_actions(sess, "주변을 살핀다", master_channel)
    await cog._process_actions(sess, "다시 살핀다", master_channel)

    assert len(seen_ids) == 2
    assert seen_ids[0] is not None
    assert seen_ids[0] == seen_ids[1], "재진입이 새 트랜잭션을 만들었습니다"

    active = core.turn_transaction.get_active_transaction(sess)
    assert active is not None
    assert active.transaction_id == seen_ids[0]
    assert active.attempt == 1, "재진입이 시도를 올렸습니다"


# ── W-04 ─────────────────────────────────────────────────────

async def test_w04_stale_roll_callback_does_not_resume(
        monkeypatch, wired_bot, session_auto_ready):
    """W-04 — 낡은 ROLL 콜백은 더 새로운 트랜잭션을 되살리지 않는다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    old = core.turn_transaction.get_or_begin_turn_transaction(sess)
    core.turn_transaction.mark_waiting_for_roll(sess, old.transaction_id)

    # 플레이어 재요청 등으로 같은 논리 턴의 새 시도가 열려 활성이 교체된다.
    new = core.turn_transaction.begin_attempt(sess, logical_turn=old.logical_turn)
    assert new.transaction_id != old.transaction_id

    called = {"gm_logic": False}

    async def _fail_gm_logic(*a, **k):
        called["gm_logic"] = True
        return {"action": "PROCEED"}

    monkeypatch.setattr(cog, "_call_gm_logic", _fail_gm_logic, raising=False)

    # 낡은 old.transaction_id로 재개 시도 → stale 가드가 조용히 종료해야 한다.
    await cog._continue_with_roll_results(
        sess, "문을 연다", ["[근력] 판정: 15"], transaction_id=old.transaction_id)

    assert called["gm_logic"] is False, "stale 콜백이 지시층위를 재호출했습니다"
    active = core.turn_transaction.get_active_transaction(sess)
    assert active is not None and active.transaction_id == new.transaction_id, (
        "stale 콜백이 활성 트랜잭션을 바꿨습니다")
