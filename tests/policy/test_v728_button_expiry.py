"""v7.28 버튼 유효시간·재시작 승계.

A 진행 화면 = 시간 제한 없음 + 재시작 시 같은 메시지에 다시 붙음(resumable_prompts·creation_state),
B 확인창 = 만료 유지, C 보조 화면 = 만료 시 잠금(ExpiringView), 받을 화면이 없는 버튼 = 안내(ui_expiry).
"""
from __future__ import annotations

import pathlib
import re
import types

import discord
import pytest

import core
from core import session_flow as SF
from core import ui_expiry as UX
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeUser

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_save(monkeypatch):
    saved = []

    async def _save(bot, session, *a, **k):
        saved.append(dict(getattr(session, "resumable_prompts", None) or {}))
        return True
    monkeypatch.setattr(core.io, "save_session_data", _save)
    monkeypatch.setattr(core, "save_session_data", _save)
    return saved


def _bot(*channels, cogs=None):
    by_id = {c.id: c for c in channels}
    return types.SimpleNamespace(get_channel=lambda i: by_id.get(i), get_cog=lambda n: (cogs or {}).get(n),
                                 active_sessions={}, user=types.SimpleNamespace(id=999))


def _session(**kw):
    s = types.SimpleNamespace(session_id="s728", players={"7": {"name": "P", "profile": {}}}, scenario_data={},
                              creation_state={}, resumable_prompts={}, start_frame={}, game_ch_id=100,
                              master_gender="male", creator_uid="7")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── A: 시간 제한 없음 · B: 만료 유지 ──────────────────────────

def test_progress_views_have_no_timeout_but_confirm_prompts_still_expire():
    import cogs.session as SM
    s = _session()
    bot = _bot()
    for v in (SM.StartFrameView(bot, s, [{"title": "a"}]), SM.InitialKnowledgeView(bot, s, "7", "START", "FAILED"),
              SM.DynamicOpenRetryView(bot, s), SF.UploadRetryView(bot, s, None), SF.PrivateView(bot, s, None)):
        assert v.timeout is None, type(v).__name__
    from cogs import gm as G                                            # B: 확인창은 의도된 만료 그대로
    assert G.RewindConfirmView(bot, s, 1).timeout == 60


# ── A: 기록 · 재시작 승계 ─────────────────────────────────────

async def test_start_frame_offer_is_recorded_with_options(monkeypatch):
    monkeypatch.setattr(core.display_board, "render_png", lambda svg: b"PNG")
    opts = [{"id": "f1", "title": "틀1", "summary": "요약1"}]
    monkeypatch.setattr(core.start_frame, "offer", lambda data, profile: opts)
    ch = FakeChannel(100)
    s = _session()
    await SF._offer_start_frames(_bot(ch), s, ch)
    rec = s.resumable_prompts[SF.PROMPT_START_FRAMES]
    assert rec["options"] == opts and rec["channel_id"] == 100 and rec["message_id"] == ch.sent[-1].id


async def test_resume_reattaches_prompt_views_to_the_same_messages_and_drops_missing():
    import cogs.session as SM
    ch = FakeChannel(100)
    m1 = await ch.send("시작 상황", view=None)
    m2 = await ch.send("열기 재시도", view=None)
    s = _session(resumable_prompts={
        SF.PROMPT_START_FRAMES: {"channel_id": 100, "message_id": m1.id, "options": [{"title": "틀1"}, {"title": "틀2"}]},
        SF.PROMPT_OPEN_RETRY: {"channel_id": 100, "message_id": m2.id},
        SF.PROMPT_UPLOAD_RETRY: {"channel_id": 100, "message_id": 424242, "quoted_tokens": 5}})
    n = await SF.resume_session(_bot(ch), s)
    assert n == 2
    assert isinstance(m1.view, SM.StartFrameView) and [c.label for c in m1.view.children] == ["1. 틀1", "2. 틀2"]
    assert isinstance(m2.view, SM.DynamicOpenRetryView)
    assert SF.PROMPT_UPLOAD_RETRY not in s.resumable_prompts            # 지워진 메시지 — 기록만 정리


async def test_resume_rerenders_creation_step_screen(monkeypatch):
    calls = []

    async def _render(bot, session, channel):
        calls.append(channel.id)
    monkeypatch.setattr(SF, "render", _render)
    ch = FakeChannel(100)
    s = _session(creation_state={"step": "tts", "history": [], "data": {}, "flow_msg_id": 55})
    assert await SF.resume_session(_bot(ch), s) == 1 and calls == [100]
    s2 = _session(creation_state={"step": "profile", "history": [], "data": {}, "flow_msg_id": 55})
    assert await SF.resume_session(_bot(ch), s2) == 0 and calls == [100]   # 캐릭터 생성은 자기 복원(resume_all)


async def test_start_frame_choice_clears_record_and_blocks_second_choice_after_resume():
    import cogs.session as SM
    played = []

    class _Cog:
        async def play_intro(self, session, option, channel):
            played.append(option["title"])
    ch = FakeChannel(100)
    msg = await ch.send("x")
    s = _session(resumable_prompts={SF.PROMPT_START_FRAMES: {"channel_id": 100, "message_id": msg.id,
                                                             "options": [{"title": "틀1"}]}})
    bot = _bot(ch, cogs={"SessionCog": _Cog()})
    view = SM.StartFrameView(bot, s, [{"title": "틀1"}])
    await view.children[0].callback(FakeInteraction(user=FakeUser(7), channel=ch, message=msg))
    assert played == ["틀1"] and s.resumable_prompts == {}
    again = SM.StartFrameView(bot, s, [{"title": "틀1"}])                 # 재시작 뒤 다시 붙은 옛 화면
    await again.children[0].callback(FakeInteraction(user=FakeUser(7), channel=ch, message=msg))
    assert played == ["틀1"]


@pytest.mark.parametrize("step,expect", [("profile", "profile_done"), ("done", "intro")])
async def test_initial_knowledge_resumed_view_continues_from_saved_state(monkeypatch, step, expect):
    import cogs.session as SM
    seen = []

    async def _profile_done(bot, session, channel):
        seen.append("profile_done")

    class _Cog:
        async def play_intro(self, session, option, channel):
            seen.append("intro")
    monkeypatch.setattr(SF, "on_profile_done", _profile_done)
    s = _session(creation_state={"step": step, "history": [], "data": {}},
                 resumable_prompts={SF.PROMPT_INITIAL_KNOWLEDGE: {"channel_id": 100, "message_id": 1}})
    ch = FakeChannel(100)
    view = SM.InitialKnowledgeView(_bot(ch, cogs={"SessionCog": _Cog()}), s, "7", "PROFILE", "FAILED")
    await view._finish(FakeInteraction(user=FakeUser(7), channel=ch, message=await ch.send("x")))
    assert seen == [expect] and s.resumable_prompts == {}


def test_main_resumes_prompts_once_per_process():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "core.session_flow.resume_all(self)" in src and "_prompts_resumed" in src


# ── 받을 화면이 없는 버튼 · C 잠금 ─────────────────────────────

def _store(views=None, dynamic=()):
    store = types.SimpleNamespace(_views=views or {}, _dynamic_items={re.compile(p): object for p in dynamic})
    return types.SimpleNamespace(_connection=types.SimpleNamespace(_view_store=store))


def _component(custom_id, msg_id=10):
    i = FakeInteraction(user=FakeUser(7), message=types.SimpleNamespace(id=msg_id))
    i.type = discord.InteractionType.component
    i.data = {"custom_id": custom_id, "component_type": 2}
    return i


async def test_unhandled_button_gets_guidance_instead_of_failure():
    dead = _component("abc")
    assert await UX.answer_stale(_store(), dead)
    assert dead.response.messages[0]["ephemeral"] and "만료" in dead.response.messages[0]["content"]
    live = _component("abc")
    assert not await UX.answer_stale(_store({10: {(2, "abc"): object()}}), live)        # 붙어 있는 화면
    assert not await UX.answer_stale(_store({None: {(2, "gmspace:hall"): object()}}), _component("gmspace:hall"))
    assert not await UX.answer_stale(_store(dynamic=[r"x:\d+"]), _component("x:12"))
    other = _component("abc")
    other.type = discord.InteractionType.application_command
    assert not await UX.answer_stale(_store(), other)


async def test_expiring_view_locks_only_while_still_attached():
    edits = []
    msg = types.SimpleNamespace(id=77, edit=None)

    async def _edit(**kw):
        edits.append(kw)
    msg.edit = _edit
    old = UX.ExpiringView(timeout=5)
    old.add_item(discord.ui.Button(label="a"))
    old.bind(msg)
    new = UX.ExpiringView(timeout=5).bind(msg)                         # 같은 메시지가 다른 화면으로 바뀜
    await old.on_timeout()
    assert edits == []
    await new.on_timeout()
    assert len(edits) == 1 and edits[0]["view"] is new
    v = UX.ExpiringView(timeout=5)
    v.add_item(discord.ui.Button(label="b"))
    v.bind(types.SimpleNamespace(id=78, edit=_edit))
    await v.on_timeout()
    assert all(c.disabled for c in v.children)


def test_auxiliary_views_lock_on_timeout():
    from core import gm_space, profile_ui, terms
    for cls in (gm_space.SessionKindView, terms.TermsView, profile_ui.ProfileHomeView, profile_ui.ProfileListView,
                profile_ui.EditFieldView, profile_ui.ProfileSelectView, profile_ui.DeleteConfirmView,
                profile_ui.ScenarioPickView, profile_ui.CommonProfileView):
        assert issubclass(cls, UX.ExpiringView), cls.__name__
