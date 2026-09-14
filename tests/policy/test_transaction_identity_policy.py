"""TID-001 ~ TID-008 — 트랜잭션 식별자 정책.

WP00_EXECUTABLE_TEST_PLAN.md §7 기준.

WP-01이 `core.turn_transaction`을 만들면 실행 가능해진다. 그 전까지는
skip한다. **프로덕션 구현이 없는 상태에서 통과하도록 꾸미지 않는다.**

각 테스트는 WP-01이 만족해야 할 계약을 기술한다.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.policy

turn_transaction = pytest.importorskip(
    "core.turn_transaction",
    reason="WP-01이 core/turn_transaction.py를 만들기 전까지 skip")


def test_tid001_first_automatic_logical_turn(session_auto_ready):
    """TID-001 — turn_count=7, 활성 트랜잭션 없음 → 논리 턴 8, 시도 1."""
    sess = session_auto_ready
    sess.turn_count = 7

    txn = turn_transaction.get_or_begin_turn_transaction(sess)
    assert txn.logical_turn == 8
    assert txn.attempt == 1


def test_tid002_ask_continuation_reuses_identity(session_auto_ready):
    """TID-002 — ASK 후 재진입이 같은 ID·같은 시도를 재사용한다."""
    sess = session_auto_ready
    first = turn_transaction.get_or_begin_turn_transaction(sess)
    turn_transaction.mark_transaction_status(sess, first.transaction_id, turn_transaction.TurnStatus.WAITING_FOR_PLAYER)

    second = turn_transaction.get_or_begin_turn_transaction(sess)
    assert second.transaction_id == first.transaction_id
    assert second.attempt == 1, "재진입이 새 시도를 만들었습니다"
    assert second.logical_turn == first.logical_turn


def test_tid003_narrate_continuation_reuses_identity(session_auto_ready):
    """TID-003 — NARRATE도 같다."""
    sess = session_auto_ready
    first = turn_transaction.get_or_begin_turn_transaction(sess)
    turn_transaction.mark_transaction_status(sess, first.transaction_id, turn_transaction.TurnStatus.WAITING_FOR_PLAYER)

    second = turn_transaction.get_or_begin_turn_transaction(sess)
    assert second.transaction_id == first.transaction_id
    assert second.attempt == 1


def test_tid004_roll_view_carries_identity(session_auto_ready):
    """TID-004 — ROLL View가 불변 ID를 async UI 경계 너머로 옮긴다."""
    sess = session_auto_ready
    txn = turn_transaction.get_or_begin_turn_transaction(sess)

    # WP-01은 GMRollView에 transaction_id를 심는다.
    import cogs.gm as gm_mod
    view = gm_mod.GMRollView.__new__(gm_mod.GMRollView)
    view.transaction_id = txn.transaction_id

    assert view.transaction_id == txn.transaction_id
    active = turn_transaction.get_active_transaction(sess)
    assert active is not None
    assert active.transaction_id == view.transaction_id


def test_tid005_provider_retry_does_not_create_attempt(session_auto_ready):
    """TID-005 — 제공자 재시도 두 번이 한 TurnTransaction 시도에 속한다."""
    sess = session_auto_ready
    txn = turn_transaction.get_or_begin_turn_transaction(sess)

    # call_with_retry가 두 번 시도해도 턴 시도는 오르지 않는다.
    again = turn_transaction.get_or_begin_turn_transaction(sess)
    assert again.attempt == txn.attempt == 1


def test_tid006_player_rerender_increments_attempt(session_auto_ready):
    """TID-006 — 플레이어 재요청만이 같은 논리 턴의 시도를 올린다."""
    sess = session_auto_ready
    first = turn_transaction.get_or_begin_turn_transaction(sess)
    logical = first.logical_turn

    second = turn_transaction.begin_attempt(sess, logical_turn=logical)
    assert second.logical_turn == logical
    assert second.attempt == 2
    assert second.transaction_id != first.transaction_id


def test_tid007_stale_id_cannot_clear_newer(session_auto_ready):
    """TID-007 — 낡은 ID로는 더 새로운 활성 트랜잭션을 지울 수 없다."""
    sess = session_auto_ready
    old = turn_transaction.get_or_begin_turn_transaction(sess)
    turn_transaction.clear_active_transaction(sess, old.transaction_id)

    new = turn_transaction.get_or_begin_turn_transaction(sess)
    turn_transaction.clear_active_transaction(sess, old.transaction_id)   # 낡은 ID

    active = turn_transaction.get_active_transaction(sess)
    assert active is not None, "낡은 ID가 새 트랜잭션을 지웠습니다"
    assert active.transaction_id == new.transaction_id


def test_tid008_runtime_only_serialization(wired_bot, session_auto_ready):
    """TID-008 — 활성 트랜잭션 상태가 세션 JSON에 저장되지 않는다.

    AUD-051 정정 — WP-01 활성 트랜잭션은 런타임 전용이다.
    """
    import core

    sess = session_auto_ready
    turn_transaction.get_or_begin_turn_transaction(sess)

    assert "active_turn_transaction" not in core.SESSION_FIELDS
    assert "turn_attempt_counters" not in core.SESSION_FIELDS
