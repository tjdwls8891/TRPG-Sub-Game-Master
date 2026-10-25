# -*- coding: utf-8 -*-
"""v7.21.4 — 세션 첫 생성의 유지 시간 답변은 게임 채널에서 그대로 받는다(디스플레이 이동 없음).
닫았던 세션의 재오픈(disp:open)은 기존대로 디스플레이 채널에서 받는다."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core
from core import session_flow as SF
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeInteraction, FakeMessage, FakeUser

pytestmark = pytest.mark.policy


def _msg(author_id, channel, mid, content="3시간"):
    m = FakeMessage(channel=channel, content=content)
    m.id = mid
    m.author = FakeUser(author_id)
    m.author.bot = False
    return m


def _gm(bot, monkeypatch, handled):
    import cogs.gm as gm_mod
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = bot

    async def _handle(session, message):
        handled.append((message.channel.id, message.id))
    monkeypatch.setattr(gm, "_handle_open_time_input", _handle)
    return gm


async def test_first_creation_asks_and_consumes_answer_in_game_channel(
        wired_bot, session_auto_ready, game_channel, display_channel, monkeypatch):
    s = session_auto_ready
    s.gm_active = False
    # v7.37(R1): [시간 입력] 버튼 단계 없이 대화 메시지가 묻고 바로 답을 기다린다
    s.creator_uid = PLAYER_UID
    s.creation_state = {"step": "open", "talk_started": {"open": True}}       # 설명 박자는 지난 상태
    await SF.render(wired_bot, s, game_channel)
    q = next(m for m in reversed(game_channel.sent) if not m.files)
    assert "이 채널에 바로 답해" in q.content and q.view is None              # 질문이 게임 채널 대화 메시지에
    assert not display_channel.sent                                         # 디스플레이로 보내지 않음
    assert SF.awaiting_open_here(s, game_channel.id) and not SF.awaiting_open_here(s, display_channel.id)

    handled = []
    gm = _gm(wired_bot, monkeypatch, handled)
    await gm.on_message(_msg(int(PLAYER_UID), display_channel, 1))           # 디스플레이 답변은 받지 않음
    await gm.on_message(_msg(424242, game_channel, 2))                      # 비참가자 무시
    assert handled == [] and s.awaiting_display_input
    await gm.on_message(_msg(int(PLAYER_UID), game_channel, 3))
    assert handled == [(game_channel.id, 3)] and not s.awaiting_display_input
    await gm.on_message(_msg(int(PLAYER_UID), game_channel, 4))             # 1회만 소비
    assert handled == [(game_channel.id, 3)]


async def test_answer_is_not_recorded_as_turn_context(wired_bot, session_auto_ready, game_channel):
    import cogs.game as game_mod
    s = session_auto_ready
    s.gm_active = False
    wired_bot.user = FakeUser(1)                                             # 봇 자신(작성자와 다름)
    s.awaiting_display_input, s._open_input_ch_id = True, game_channel.id
    before = list(s.current_turn_logs)
    await game_mod.GameCog(wired_bot).on_message(_msg(int(PLAYER_UID), game_channel, 5))
    assert s.current_turn_logs == before
    # GMCog가 먼저 소비해 플래그가 내려간 뒤에도(리스너 순서 무관) 기록하지 않는다
    s.awaiting_display_input, s._display_input_msg_id = False, 6
    await game_mod.GameCog(wired_bot).on_message(_msg(int(PLAYER_UID), game_channel, 6))
    assert s.current_turn_logs == before


async def test_reopen_stays_on_display_channel(
        wired_bot, session_auto_ready, game_channel, display_channel, monkeypatch):
    s = session_auto_ready
    s._open_input_ch_id = game_channel.id                                    # 이전 첫 생성 표식이 남아 있어도
    s.cache_name = None
    disp = core.display.DisplayView(wired_bot)
    it = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=display_channel)
    monkeypatch.setattr(core.display.DisplayView, "_busy", lambda self, sess: False, raising=False)
    await disp.session_open.callback(it)
    assert s.awaiting_display_input and s._open_input_ch_id is None
    assert not SF.awaiting_open_here(s, game_channel.id)
    handled = []
    gm = _gm(wired_bot, monkeypatch, handled)
    await gm.on_message(_msg(int(PLAYER_UID), game_channel, 7))
    assert handled == []                                                     # 게임 채널 발언은 시간 답변 아님
    await gm.on_message(_msg(int(PLAYER_UID), display_channel, 8))
    assert handled == [(display_channel.id, 8)]


async def test_display_guard_rejects_while_answer_expected_in_game_channel(
        wired_bot, session_auto_ready, game_channel, display_channel):
    s = session_auto_ready
    s.awaiting_display_input, s._open_input_ch_id = True, game_channel.id
    m = _msg(int(PLAYER_UID), display_channel, 9)
    m.guild = object()
    assert await core.chat_guard(wired_bot, m) is False
    s._open_input_ch_id = None                                               # 재오픈 대기(디스플레이)는 통과
    m2 = _msg(int(PLAYER_UID), display_channel, 10)
    m2.guild = object()
    assert await core.chat_guard(wired_bot, m2) is True
