"""P-004 ~ P-005 — 메시지 생명주기 / 정상성 정책.

WP00_TEST_HARNESS_SPEC.md §6 기준.

P-004는 순수 정책 모델로 지금 검증한다(WP-01이 프로덕션 프리미티브를
만들면 TID-007이 같은 계약을 실제 코드에 대해 검증한다).
P-005는 현행 디스플레이/임시 메시지 동작을 대조한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

pytestmark = pytest.mark.policy


# ── P-004 낡은 트랜잭션 가드 (순수 모델) ────────────────────

@dataclass
class ActiveSlot:
    """활성 트랜잭션 슬롯. 낡은 ID의 조작을 거부한다."""
    current_id: str | None = None

    def begin(self, txn_id: str) -> None:
        self.current_id = txn_id

    def clear(self, txn_id: str) -> bool:
        """자기 ID일 때만 지운다."""
        if self.current_id == txn_id:
            self.current_id = None
            return True
        return False

    def mutate(self, txn_id: str, fn) -> bool:
        if self.current_id != txn_id:
            return False
        fn()
        return True


def test_p004_stale_id_cannot_clear_newer_transaction():
    """P-004 — 낡은 ID로 더 새로운 활성 트랜잭션을 지울 수 없다."""
    slot = ActiveSlot()
    slot.begin("txn-old")
    assert slot.clear("txn-old") is True

    slot.begin("txn-new")
    assert slot.clear("txn-old") is False, "낡은 ID가 새 트랜잭션을 지웠습니다"
    assert slot.current_id == "txn-new"


def test_p004b_stale_id_cannot_mutate():
    slot = ActiveSlot()
    slot.begin("txn-new")
    touched = []
    assert slot.mutate("txn-old", lambda: touched.append(1)) is False
    assert touched == []
    assert slot.mutate("txn-new", lambda: touched.append(1)) is True
    assert touched == [1]


def test_p004c_late_async_task_is_rejected():
    """늦게 도착한 비동기 결과가 새 트랜잭션을 오염시키지 않는다."""
    slot = ActiveSlot()
    slot.begin("txn-1")
    captured = "txn-1"          # 태스크가 출발 시점에 든 ID

    slot.begin("txn-2")         # 그 사이 턴이 넘어갔다

    applied = slot.mutate(captured, lambda: None)
    assert applied is False, "낡은 태스크가 새 트랜잭션에 기록했습니다"


# ── P-005 메시지 생명주기 ───────────────────────────────────

@dataclass
class TransientRegistry:
    """트랜잭션이 소유한 임시 메시지 등록부."""
    owned: dict = field(default_factory=dict)

    def register(self, txn_id: str, message) -> None:
        self.owned.setdefault(txn_id, []).append(message)

    async def cleanup(self, txn_id: str) -> int:
        msgs = self.owned.pop(txn_id, [])
        for m in msgs:
            await m.delete()
        return len(msgs)


async def test_p005_transient_owner_termination_cleans_up(game_channel):
    """P-005 — 소유자가 끝나면 임시 메시지가 정리된다."""
    reg = TransientRegistry()
    a = await game_channel.send("대기 중…")
    b = await game_channel.send("주사위를 굴리십시오")
    reg.register("txn-1", a)
    reg.register("txn-1", b)

    removed = await reg.cleanup("txn-1")
    assert removed == 2
    assert a.deleted and b.deleted
    assert "txn-1" not in reg.owned


async def test_p005b_canonical_display_is_not_transient(
        display_channel, game_channel):
    """P-005 — 정규 디스플레이는 임시물로 취급되어 삭제되지 않는다."""
    reg = TransientRegistry()
    canonical = await display_channel.send("상태판")
    transient = await game_channel.send("잠깐 안내")
    reg.register("txn-1", transient)

    await reg.cleanup("txn-1")
    assert transient.deleted is True
    assert canonical.deleted is False, "정규 디스플레이가 삭제됐습니다"


async def test_p005c_current_display_refresh_edits_single_message(
        monkeypatch, wired_bot, session_auto_ready, display_channel):
    """대조 — 현행 디스플레이는 메시지 하나를 편집해 유지한다.

    이 성질이 유지되어야 '단일 정규 표면' 불변식이 성립한다.
    """
    import core

    sess = session_auto_ready
    sess.display_msg_id = None

    await core.refresh_display(wired_bot, sess, reason="test")
    assert len(display_channel.sent) == 1, "첫 갱신이 메시지를 만들지 않았습니다"
    first = display_channel.sent[0]

    await core.refresh_display(wired_bot, sess, reason="test2")
    assert len(display_channel.sent) == 1, (
        "두 번째 갱신이 새 메시지를 보냈습니다 — 단일 표면이 깨집니다")
    assert first.edit_history, "기존 메시지를 편집하지 않았습니다"


async def test_p005d_notify_records_self_expiry(game_channel):
    """대조 — 임시 알림은 스스로 사라질 시한을 갖는다."""
    msg = await game_channel.send("잠시 후 사라짐", delete_after=12)
    assert msg.delete_after == 12
