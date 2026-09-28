"""WP-F — 플레이어 채널 메시지 수명주기 (M-F01 ~ M-F12 + 경합).

CANONICAL_DISPLAY 단일 표면, INTERACTION_PROMPT 종결 정확히 한 번, TRANSIENT_GAME_STATUS
종결 정리(성공·실패·예외·supersede), 커밋된 사실만의 정본 알림, OPERATOR_LOG 분리.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import core
from core import commit_coordinator as CC
from core import message_lifecycle as ML
from core import turn_history as TH
from core import turn_transaction as tt
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeInteraction, FakeMessage, FakeUser
from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용
from tests.policy.test_turn_history import _commit, _hv

pytestmark = pytest.mark.policy


@pytest.fixture
def fast_sleep(monkeypatch):
    real = asyncio.sleep

    async def _fast(_s=0, *a, **k):
        await real(0)
    monkeypatch.setattr(asyncio, "sleep", _fast)
    return real


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


def _live(channel):
    return [m for m in channel.sent if not m.deleted]


# ── M-F01 · M-F02 CANONICAL_DISPLAY ─────────────────────────

async def test_mf01_refresh_edits_same_display_message(wired_bot, session_auto_ready, display_channel):
    s = session_auto_ready
    assert await core.display.refresh(wired_bot, s, reason="t")
    first = s.display_msg_id
    assert await core.display.refresh(wired_bot, s, reason="t")
    assert s.display_msg_id == first and len(display_channel.sent) == 1
    assert display_channel.sent[0].edit_history


async def test_mf02_missing_display_recreated_and_id_persisted(wired_bot, session_auto_ready, display_channel):
    s = session_auto_ready
    await core.display.refresh(wired_bot, s)
    old = display_channel.sent[0]
    await old.delete()
    assert await core.display.refresh(wired_bot, s)
    assert s.display_msg_id != old.id and len(_live(display_channel)) == 1
    data = json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))
    assert data["display_msg_id"] == s.display_msg_id


async def test_mf02b_transient_failure_does_not_create_second_surface(
        wired_bot, session_auto_ready, display_channel, monkeypatch):
    s = session_auto_ready
    await core.display.refresh(wired_bot, s)

    async def _boom(mid):
        raise RuntimeError("503 Service Unavailable")
    monkeypatch.setattr(display_channel, "fetch_message", _boom)
    assert await core.display.refresh(wired_bot, s) is False
    assert len(display_channel.sent) == 1                      # 중복 상태판 없음


# ── M-F03 · M-F04 · M-F05 INTERACTION_PROMPT ────────────────

class _GMStub:
    def __init__(self):
        self.rewinds = []

    async def history_rewind(self, session, target):
        self.rewinds.append(target)
        return {"ok": True, "removed_turns": [target + 1], "removed_messages": 0}


def _prompt(bot, session, channel):
    from cogs.gm import RewindConfirmView
    stub = _GMStub()
    bot.add_cog_stub("GMCog", stub)
    view = RewindConfirmView(bot, session, 3)
    msg = FakeMessage(channel=channel, content="확인?", view=view)
    channel.sent.append(msg)
    view.bind(msg)
    return view, msg, stub


async def test_mf03_confirm_resolves_and_removes_prompt(wired_bot, session_auto_ready, display_channel, fast_sleep):
    view, msg, stub = _prompt(wired_bot, session_auto_ready, display_channel)
    await view.confirm.callback(FakeInteraction(channel=display_channel, message=msg))
    await _drain()
    assert stub.rewinds == [3] and msg.view is None and msg.deleted
    assert view.terminal == ML.PROMPT_CONFIRM and view.is_finished()


async def test_mf04_cancel_resolves_and_removes_prompt(wired_bot, session_auto_ready, display_channel, fast_sleep):
    view, msg, stub = _prompt(wired_bot, session_auto_ready, display_channel)
    await view.cancel.callback(FakeInteraction(channel=display_channel, message=msg))
    await _drain()
    assert stub.rewinds == [] and msg.deleted and view.terminal == ML.PROMPT_CANCEL


async def test_mf05_timeout_disables_and_collapses_prompt(wired_bot, session_auto_ready, display_channel, fast_sleep):
    view, msg, stub = _prompt(wired_bot, session_auto_ready, display_channel)
    await view.on_timeout()
    await _drain()
    assert all(c.disabled for c in view.children)
    assert msg.edit_history[-1]["view"] is None and msg.deleted
    assert view.terminal == ML.PROMPT_TIMEOUT


async def test_mf05b_timeout_then_confirm_race_has_single_outcome(
        wired_bot, session_auto_ready, display_channel, fast_sleep):
    view, msg, stub = _prompt(wired_bot, session_auto_ready, display_channel)
    await view.on_timeout()
    await view.confirm.callback(FakeInteraction(channel=display_channel, message=msg))
    assert stub.rewinds == [] and view.terminal == ML.PROMPT_TIMEOUT


async def test_mf05c_confirm_then_timeout_race_has_single_outcome(
        wired_bot, session_auto_ready, display_channel, fast_sleep):
    view, msg, stub = _prompt(wired_bot, session_auto_ready, display_channel)
    await view.confirm.callback(FakeInteraction(channel=display_channel, message=msg))
    edits = len(msg.edit_history)
    await view.on_timeout()
    assert stub.rewinds == [3] and view.terminal == ML.PROMPT_CONFIRM
    assert len(msg.edit_history) == edits                      # 만료 문구로 덮어쓰지 않음


async def test_open_confirm_timeout_resets_selection(wired_bot, session_auto_ready, display_channel, fast_sleep):
    from cogs.gm import OpenConfirmView
    s = session_auto_ready
    s.open_minutes = 120
    view = OpenConfirmView(wired_bot, s, 120)
    msg = FakeMessage(channel=display_channel, content="열까요?", view=view)
    view.bind(msg)
    await view.on_timeout()
    assert s.open_minutes == 0 and view.terminal == ML.PROMPT_TIMEOUT


def test_all_display_confirmation_views_use_prompt_lifecycle():
    from cogs.gm import OpenConfirmView, RerenderConfirmView, RewindConfirmView
    for cls in (OpenConfirmView, RerenderConfirmView, RewindConfirmView,
                core.display.CloseConfirmView):
        assert issubclass(cls, ML.LifecyclePromptView), cls


# ── M-F06 · M-F07 TRANSIENT_GAME_STATUS ─────────────────────

async def test_mf06_waiting_status_success_cleanup(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    st = await core.WaitingStatus.begin(game_channel, "narration", session=s)
    assert ML.pending(s.session_id)
    await st.done()
    await st.done()                                             # 멱등
    assert _live(game_channel) == [] and ML.pending(s.session_id) == {}


async def test_mf07_judgment_exception_leaves_no_waiting_status(rig, monkeypatch):
    r = rig
    s = r.sess
    core.accounts._write_account_strict(dict(core.accounts._blank_account(PLAYER_UID),
                                             ink_balance=10_000))

    async def _boom(*a, **k):
        raise RuntimeError("판단층위 예외")
    monkeypatch.setattr(r.gm, "_call_judgment", _boom, raising=False)
    with pytest.raises(RuntimeError):
        await r.gm._run_gm_logic_loop(s, "숲으로 간다", r.master)
    judgment_texts = set(core.LAYER_STATUS_MESSAGES["judgment"])
    sent_status = [m for m in r.gch.sent
                   if (m.content or "").split("\n")[0] in judgment_texts]
    assert sent_status and all(m.deleted for m in sent_status)   # 보냈고, 예외에도 정리됨
    assert not any(k.startswith(ML.WAITING_PREFIX) for k in ML.pending(s.session_id))


async def test_mf07b_narration_cancel_leaves_no_waiting_status(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    st = await core.WaitingStatus.begin(game_channel, "narration", session=s)

    async def _work():
        async with st:
            await asyncio.Event().wait()
    t = asyncio.create_task(_work())
    await asyncio.sleep(0)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert _live(game_channel) == [] and ML.pending(s.session_id) == {}


async def test_committed_turn_leaves_no_transient(rig):
    r = rig
    await _commit(r, "숲길")
    assert ML.pending(r.sess.session_id) == {}
    status_texts = set(sum(core.LAYER_STATUS_MESSAGES.values(), []))
    sent_status = [m for m in r.gch.sent if (m.content or "").split("\n")[0] in status_texts]
    assert sent_status and all(m.deleted for m in sent_status)


async def test_restart_sweep_removes_orphaned_waiting_status(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    st = await core.WaitingStatus.begin(game_channel, "judgment", session=s)
    st._task.cancel()                                           # 프로세스 종료 모사(정리 없음)
    s._transient_handles = {}                                   # 런타임 핸들 소실
    n = await ML.sweep(wired_bot, s, prefix=ML.WAITING_PREFIX)
    assert n == 1 and _live(game_channel) == [] and ML.pending(s.session_id) == {}


async def test_transient_not_written_to_game_log(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    await ML.send_transient(wired_bot, s, game_channel, "⏸️ 세션이 닫혀 있습니다.",
                            key=ML.KEY_TURN_NOTICE)
    path = f"sessions/{s.session_id}/game_chat_log.txt"
    assert not os.path.exists(path) or "세션이 닫혀" not in open(path, encoding="utf-8").read()


# ── M-F08 supersede ─────────────────────────────────────────

async def test_mf08_supersede_clears_bot_notice_but_not_player_declaration(
        wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    await ML.send_transient(wired_bot, s, game_channel, core.build_failed_turn_notice("동굴로 간다"),
                            key=ML.KEY_TURN_NOTICE)
    player_msg = FakeMessage(channel=game_channel, content="동굴로 간다")
    player_msg.author = FakeUser(int(PLAYER_UID))
    game_channel.sent.append(player_msg)
    # 같은 키 새 안내 → 이전 안내 교체
    await ML.send_transient(wired_bot, s, game_channel, "⏸️ 잠시 후", key=ML.KEY_TURN_NOTICE)
    assert len([m for m in _live(game_channel) if m is not player_msg]) == 1
    # 다음 턴 시작(supersede) → 남은 안내 정리, 플레이어 선언은 그대로
    await ML.clear(wired_bot, s, ML.KEY_TURN_NOTICE)
    assert _live(game_channel) == [player_msg] and not player_msg.deleted


async def test_mf08b_clear_is_idempotent_on_already_deleted(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    m = await ML.send_transient(wired_bot, s, game_channel, "x", key=ML.KEY_TURN_NOTICE)
    await m.delete()
    m.raise_on_second_delete = True
    assert await ML.clear(wired_bot, s, ML.KEY_TURN_NOTICE) == 1
    assert await ML.clear(wired_bot, s, ML.KEY_TURN_NOTICE) == 0
    assert ML.pending(s.session_id) == {}


# ── M-F09 · M-F10 · M-F12 CANONICAL_GAME_EVENT ──────────────

def _inject_notice(r, monkeypatch, text):
    orig = r.gm._apply_prepared_extraction

    async def _with_notice(session, prep, *, derived=None):
        await orig(session, prep, derived=derived)
        if derived is not None:
            derived.game(text)
    monkeypatch.setattr(r.gm, "_apply_prepared_extraction", _with_notice)


async def test_mf09_mf12_committed_notice_emitted_after_commit_and_mapped(rig, monkeypatch):
    r = rig
    _inject_notice(r, monkeypatch, "🏛️ **길드**의 일원이 되었습니다.")
    tx = await _commit(r, "숲길")
    sent = [m for m in r.gch.sent if m.content == "🏛️ **길드**의 일원이 되었습니다."]
    assert len(sent) == 1
    assert TH._journal_committed(r.sess.session_id, tx.transaction_id)
    entry = _hv(r.sess).selected[1]
    assert sent[0].id in TH.attempt_message_ids(r.sess.session_id, entry)   # WP-E 정리 대상


async def test_mf10_failed_commit_emits_no_durable_notice(rig, monkeypatch):
    r = rig
    _inject_notice(r, monkeypatch, "🏛️ 거짓 소속 알림")

    async def _fail(session):
        raise core.SessionPersistenceError("strict save 실패(모사)")
    monkeypatch.setattr(core.io, "write_session_strict_locked", _fail)
    decl = "숲길로 간다"
    tx = tt.get_or_begin_turn_transaction(r.sess, decl)
    await r.gm._finish_proceed_and_continue(
        r.sess, f"{decl} 묘사", r.master, event_assessment="ongoing",
        transaction_id=tx.transaction_id)
    assert tx.status != tt.TurnStatus.COMMITTED
    assert not any(m.content == "🏛️ 거짓 소속 알림" for m in r.gch.sent)


# ── M-F11 OPERATOR_LOG ──────────────────────────────────────

async def test_mf11_operator_log_never_falls_back_to_player_channel(fake_bot, session_factory, game_channel):
    s = session_factory(session_id="op-sep")
    s.master_ch_id = 0                                          # 마스터 채널 없음
    assert await ML.send_operator(fake_bot, s, "내부 비용 보고") is None
    assert game_channel.sent == []


def test_mf11b_player_notices_route_through_lifecycle_source_scan():
    from tests.conftest import source_of
    gm = source_of("cogs/gm.py")
    # 턴 실패/차단 안내는 transient 수명주기로만 나간다(스트리밍 게임 로그·영구 잔존 금지).
    assert "send_streamed(self.bot, game_ch, core.build_failed_turn_notice" not in gm
    assert gm.count("KEY_TURN_NOTICE") >= 7
    presence = source_of("cogs/presence.py")
    assert "game_ch.send(" not in presence
    assert source_of("core/dialogue.py").count("_ml.register(") == 1
