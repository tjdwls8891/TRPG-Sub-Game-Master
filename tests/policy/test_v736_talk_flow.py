# -*- coding: utf-8 -*-
"""v7.36 세션·캐릭터 생성 대화 박자(사용자 결정: 메시지 하나를 고쳐 씀 · [[답]] 표기 · 소개문 자동 분할 ·
소개 주제 분할 · 응답은 억지로 둘로 나누지 않음).
"""
from __future__ import annotations

import json
import os

import pytest

import core
from core import creation
from core import intro as intro_mod
from core import session_flow as SF
from core import talk as T
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.fakes.discord_fakes import FakeInteraction, FakeUser

pytestmark = pytest.mark.policy


# ── 표기 ─────────────────────────────────────────────────────

def test_parse_marks_and_final_beat():
    assert T.parse("한 번에") == [T.Beat("한 번에", None)]                                   # 표기 없음 = 기존 동작
    b = T.parse("가\n[[네.|그렇군요.]]\n나\n[[]]\n다")
    assert [x.text for x in b] == ["가", "나", "다"] and b[0].answers == ("네.", "그렇군요.")
    assert b[1].answers == () and b[2].answers is None
    end = T.parse("가\n[[네.]]")                                                          # 표기로 끝남 = 빈 결정 박자
    assert end[-1] == T.Beat("", None) and end[0].answers == ("네.",)
    assert T.parse("문장 [[중간]] 표기") == [T.Beat("문장 [[중간]] 표기", None)]             # 줄 단독일 때만
    assert T.plain("가\n[[네.]]\n나") == "가\n\n나"


def test_check_and_auto_split():
    errs = T.check("가\n[[하나|둘|셋]]\n나 [[x]]\n[[" + "가" * 31 + "]]")
    assert len(errs) == 3
    long = "\n\n".join(["가" * 200, "나" * 200, "다" * 50])
    beats = T.parse(T.auto_split(long))
    assert len(beats) == 2 and beats[0].answers == ()                                   # 공용 응답
    assert T.auto_split("가\n[[네.]]\n나") == "가\n[[네.]]\n나"                            # 저작 표기 우선


def test_authored_texts_pass_check_and_flow_keys_are_beats():
    with open(os.path.join(REPO_ROOT, "assets", "master_dialogues_v1.json"), encoding="utf-8") as f:
        d = json.load(f)
    for k, ev in d["events"].items():
        for v in ev["variants"]:
            assert T.check(v) == [], k
    for k in ("session.private_select", "session.memory_select", "session.open_time", "profile.completed"):
        assert all(len(T.parse(v)) >= 2 for v in d["events"][k]["variants"]), k
    assert core.master.acks() and all(1 <= len(a) <= 2 for a in core.master.acks())
    for key, t in intro_mod.TOPICS.items():
        for txt in [t["body"]] + list(t.get("alts") or []):
            assert T.check(txt) == [] and len(T.parse(txt)) >= 2, key


# ── 세션 준비 화면 ────────────────────────────────────────────

@pytest.fixture
def flow(wired_bot, session_auto_ready, game_channel):
    s = session_auto_ready
    s.creator_uid = PLAYER_UID
    s.creation_state = {"step": "private"}
    return wired_bot, s, game_channel


def _flow(ch):
    """대화 메시지 — v7.37부터 그 위에 마스터 이미지 전용 메시지가 따로 있다."""
    return next(m for m in reversed(ch.sent) if not m.files)


def _press_answer(msg, i=0):
    btn = msg.view.children[i]
    return btn.callback(FakeInteraction(user=FakeUser(int(PLAYER_UID)), message=msg))


async def test_private_step_beats_in_one_message_then_decision(flow):
    bot, s, ch = flow
    await SF.render(bot, s, ch)
    assert len(ch.sent) == 2 and ch.sent[0].files and not ch.sent[1].files      # v7.37: 이미지 메시지가 위
    msg = _flow(ch)
    assert isinstance(msg.view, T.TalkView) and "공개" in msg.content
    await _press_answer(msg)
    assert len(ch.sent) == 2                                                            # 같은 메시지를 고쳐 쓴다
    assert isinstance(msg.view, SF.PrivateView) and "비공개로" in msg.content
    assert "-# ↳" in msg.content                                                        # 직전 응답 한 줄
    assert creation.current_step(s) == "private" and "private" not in (s.creation_state.get("data") or {})


async def test_either_answer_reaches_the_same_next_beat(flow, monkeypatch):
    bot, s, ch = flow
    monkeypatch.setattr(SF, "_visible_scenarios", lambda _s: ["영도"])
    monkeypatch.setattr(core.master, "line", lambda *_a, **_k: None)                    # 대체 문구(두 응답) 고정
    s.creation_state["step"] = "scenario"
    await SF.render(bot, s, ch)
    msg = _flow(ch)
    labels = [c.label for c in msg.view.children]
    assert labels == ["네.", "어떤 세계가 있죠?"]
    await _press_answer(msg, len(labels) - 1)
    import re
    strip_echo = lambda c: re.sub(r"-# ↳ [^\n]*\n", "", c)                              # noqa: E731 — 응답 한 줄 제외
    after = strip_echo(msg.content)
    T.reset(s, "scenario", s.creation_state.get("talk_started"))
    await SF.render(bot, s, ch)
    await _press_answer(msg, 0)
    assert strip_echo(msg.content) == after and isinstance(msg.view, SF.ScenarioView)


async def test_non_owner_cannot_answer(flow):
    bot, s, ch = flow
    await SF.render(bot, s, ch)
    msg = _flow(ch)
    other = FakeInteraction(user=FakeUser(999), message=msg)
    assert await msg.view.interaction_check(other) is False
    assert isinstance(msg.view, T.TalkView)


async def test_restart_resumes_at_decision_beat(flow):
    bot, s, ch = flow
    s.creation_state["step"] = "memory"
    await SF.render(bot, s, ch)
    assert isinstance(_flow(ch).view, T.TalkView)
    T._POS.clear()                                                                      # 재시작(메모리 소실)
    await SF.resume_session(bot, s)
    msg = _flow(ch)
    assert isinstance(msg.view, SF.MemoryPlanView) and msg.embeds and msg.embeds[0].fields   # 플랜 카드는 결정 박자에만


async def test_scenario_intro_auto_split_then_confirm_with_cost(flow, monkeypatch):
    bot, s, ch = flow
    data = {"scenario_intro": "\n\n".join(["가" * 200, "나" * 200]), "worldview": "w"}
    await SF.show_scenario_confirm(bot, s, ch, "테스트", data)
    msg = _flow(ch)
    assert isinstance(msg.view, T.TalkView) and "가" * 50 in msg.content
    while isinstance(msg.view, T.TalkView):
        await _press_answer(msg)
    assert isinstance(msg.view, SF.ScenarioConfirmView)
    assert "들어가시겠습니까" in msg.content or "시작하시겠습니까" in msg.content


async def test_streaming_edits_partials_before_full_beat(flow, monkeypatch):
    bot, s, ch = flow
    monkeypatch.setattr(T, "TICK", 0.001)
    await SF.render(bot, s, ch)
    msg = _flow(ch)
    partial = [e for e in msg.edit_history if e.get("view") is None and "▍" in (e.get("content") or "")]
    assert partial and isinstance(msg.view, T.TalkView) and "▍" not in msg.content
    assert all("attachments" not in e for e in msg.edit_history)                       # 박자 편집은 이미지 재업로드 없음


async def test_greeting_beats_precede_private_decision(flow):
    bot, s, ch = flow
    s.creation_state["greeting"] = SF.greeting_text("<@1>", "<#2>")
    await SF.render(bot, s, ch)
    msg = _flow(ch)
    assert msg.content.startswith("**공개 설정**") and "<@1> 님" in msg.content
    seen = [msg.content]
    while isinstance(msg.view, T.TalkView):
        await _press_answer(msg)
        seen.append(msg.content)
    assert any("<#2>" in c for c in seen) and isinstance(msg.view, SF.PrivateView)


# ── 소개 주제 ───────────────────────────────────────────────

async def test_intro_topic_beats_keep_existing_buttons_at_topic_end(flow):
    bot, s, ch = flow
    view = SF.IntroView(bot, s, ch, intro_mod.LEVEL_KNOWN)
    msg = await ch.send(content="x", view=view)
    await view.go(FakeInteraction(user=FakeUser(int(PLAYER_UID)), message=msg), "next")
    labels = [c.label for c in view.children]
    assert "계속 들을게요" not in labels and "모든 설명 건너뛰기" in labels               # 중간 박자: 응답 + 건너뛰기
    n = len(T.parse(view._bodies[view._key]))
    for _ in range(n - 1):
        await view.go(FakeInteraction(user=FakeUser(int(PLAYER_UID)), message=msg), "ack", "네.")
    labels = [c.label for c in view.children]
    assert "계속 들을게요" in labels and "◀ 이전" in labels                              # 주제 끝: 기존 버튼
