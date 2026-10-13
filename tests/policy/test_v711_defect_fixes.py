"""v7.11.0 — 플레이어 상호작용 전수조사 발견 결함 수정(사용자 결정 D1~D6 포함)."""
from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

import core
from core import session_flow as SF
from tests.conftest import DISPLAY_CH, MASTER_CH, PLAYER_UID
from tests.fakes.discord_fakes import FakeInteraction, FakeUser

pytestmark = pytest.mark.policy

REPO = pathlib.Path(__file__).resolve().parents[2]


# ── 원화 미노출 · `!사용량` 채널 제한 ─────────────────────────

def test_open_confirmation_shows_ink_only():
    text = core.format_confirmation({"minutes": 180, "notes": []},
                                    {"total_ink": 30, "create_krw": 12.3, "store_krw": 45.6})
    assert "30잉크" in text and "원" not in text.replace("원하", "")


async def test_usage_report_only_in_master_channel_or_dm(wired_bot, session_auto_ready, game_channel):
    import cogs.system as system_mod
    sent = []

    async def _send(content=None, **_k):
        sent.append(content)
    ctx = SimpleNamespace(channel=game_channel, guild=object(), author=FakeUser(1), send=_send)
    cog = system_mod.SystemCog.__new__(system_mod.SystemCog)
    cog.bot = wired_bot
    await system_mod.SystemCog.usage_report.callback(cog, ctx, "")
    assert sent and "마스터 채널" in sent[0]


# ── 효과음 호출 인자 ───────────────────────────────────────

def test_play_dice_sfx_calls_pass_bot_and_guild():
    for path in ("cogs/gm.py", "cogs/character.py", "core/ui.py"):
        tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "play_dice_sfx":
                assert len(node.args) >= 2, f"{path}:{node.lineno}"


# ── 디스플레이 입력: 대기 중 참가자 1건만 해석 ──────────────────

def _msg(author_id, channel, mid=1, content="3시간"):
    return SimpleNamespace(id=mid, author=SimpleNamespace(id=author_id, bot=False), channel=channel,
                           content=content)


async def test_display_text_interpreted_only_when_awaiting_and_participant(
        wired_bot, session_auto_ready, display_channel, monkeypatch):
    import cogs.gm as gm_mod
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = wired_bot
    handled = []

    async def _handle(session, message):
        handled.append(message.id)
    monkeypatch.setattr(gm, "_handle_open_time_input", _handle)
    s = session_auto_ready
    s.awaiting_display_input = False
    await gm.on_message(_msg(int(PLAYER_UID), display_channel, 1))
    assert handled == []                                     # 대기 아님 → 해석·청구 없음
    s.awaiting_display_input = True
    await gm.on_message(_msg(999999, display_channel, 2))
    assert handled == [] and s.awaiting_display_input        # 비참가자 → 무시, 대기 유지
    await gm.on_message(_msg(int(PLAYER_UID), display_channel, 3))
    assert handled == [3] and not s.awaiting_display_input
    # chat_guard는 소비된 메시지를 순서와 무관하게 통과시킨다(거부 안내 없음)
    assert await core.chat_guard(wired_bot, SimpleNamespace(
        id=3, author=SimpleNamespace(id=int(PLAYER_UID), bot=False), channel=display_channel, content="3시간",
        guild=None, delete=None))


# ── 세션 플로우: 업로드 실패 시 정지·재시도, 버튼 세션 통계 ─────────

class _Chan:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))
        return SimpleNamespace(id=len(self.sent))


async def _open_time_done(monkeypatch, session, ok):
    calls = {"bump": [], "played": [], "render": 0}

    class _Cog:
        async def upload_cache(self, *_a, **_k):
            return ok
    bot = SimpleNamespace(get_cog=lambda name: _Cog() if name == "SessionCog" else None)

    async def _bump(uid, **d):
        calls["bump"].append((uid, d))

    async def _played(uid, sid):
        calls["played"].append((uid, sid))

    async def _render(*_a, **_k):
        calls["render"] += 1

    async def _save(*_a, **_k):
        return True
    monkeypatch.setattr(core.stats, "bump", _bump)
    monkeypatch.setattr(core.stats, "mark_played", _played)
    monkeypatch.setattr(SF, "render", _render)
    monkeypatch.setattr(core.io, "save_session_data", _save)
    monkeypatch.setattr(core.world_lifecycle, "is_dynamic_candidate", lambda s: False)
    ch = _Chan()
    session.is_started = False
    await SF.on_open_time_done(bot, session, ch)
    return calls, ch


async def test_upload_failure_stops_and_offers_retry(session_auto_ready, monkeypatch):
    calls, ch = await _open_time_done(monkeypatch, session_auto_ready, ok=False)
    assert session_auto_ready.is_started is False and calls["render"] == 0
    assert isinstance(ch.sent[-1][1].get("view"), SF.UploadRetryView)


async def test_button_flow_records_play_stats_once(session_auto_ready, monkeypatch):
    calls, _ch = await _open_time_done(monkeypatch, session_auto_ready, ok=True)
    assert session_auto_ready.is_started is True
    assert (PLAYER_UID, {"sessions": 1}) in calls["bump"]
    assert calls["played"] == [(PLAYER_UID, session_auto_ready.scenario_id)]
    assert getattr(session_auto_ready, "started_at", 0) > 0


# ── 프로필 생성 집계 ───────────────────────────────────────

async def test_profile_create_counts_profiles_created():
    before = core.stats.load_stats("77").get("profiles_created", 0)
    assert await core.profiles.create("77", name="테스트", scenario_id=None, fields={})
    assert core.stats.load_stats("77").get("profiles_created", 0) == before + 1


# ── D1 충전 안내 → 운영자 문의 ─────────────────────────────

def test_no_charge_guidance_in_player_texts():
    for path in ("core/terms.py", "core/gm_space.py", "core/display.py", "core/tts_preset.py"):
        src = (REPO / path).read_text(encoding="utf-8")
        body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert "충전" not in body, path
    assert "운영자에게 문의" in core.terms.TERMS_TEXT


# ── D4 시작 틀 없는 시나리오 숨김 ────────────────────────────

def test_scenarios_without_start_frames_hidden(monkeypatch):
    data = {"a": {"start_frames": [{}]}, "b": {}, "c": {"start_frames": [{}], "blocked": True}}
    monkeypatch.setattr(SF, "get_available_scenarios", lambda: list(data))
    monkeypatch.setattr(SF, "load_scenario_from_file", lambda sid: data[sid])
    assert SF._visible_scenarios(SimpleNamespace(session_kind="solo")) == ["a"]
    assert SF._visible_scenarios(SimpleNamespace(session_kind="master")) == ["a", "c"]


# ── D5 누를 수 있는 사람 ───────────────────────────────────

async def test_flow_views_owner_only(wired_bot, session_auto_ready):
    s = session_auto_ready
    s.creator_uid = PLAYER_UID
    other = FakeInteraction(user=FakeUser(424242))
    assert await SF.owner_only(wired_bot, s, other) is False
    assert other.response.messages and other.response.messages[0].get("ephemeral")
    assert await SF.owner_only(wired_bot, s, FakeInteraction(user=FakeUser(int(PLAYER_UID))))


async def test_confirm_prompts_participants_only(wired_bot, session_auto_ready):
    import cogs.gm as gm_mod
    view = gm_mod.RewindConfirmView(wired_bot, session_auto_ready, 1)
    stranger = FakeInteraction(user=FakeUser(424242))
    assert await view.interaction_check(stranger) is False
    assert await view.interaction_check(FakeInteraction(user=FakeUser(int(PLAYER_UID))))


# ── D3 자동 턴 더빙 비용 claim ──────────────────────────────

async def test_runtime_tts_cost_context_claimed_into_turn(session_auto_ready, monkeypatch, tmp_path):
    import cogs.game as game_mod
    from core.cost_ledger import CostLedger
    s = session_auto_ready
    s.tts_enabled = True
    bot = SimpleNamespace(cost_ledger=CostLedger(str(tmp_path / "l.jsonl")))
    monkeypatch.setattr(core, "get_mixer", lambda vc: SimpleNamespace(enqueue_voice=lambda src: None))

    async def _synth(_bot, text, voice_name=None, cost_context=None):
        core.cost_ledger.record_context_event(bot, cost_context, cost_usd=0.001, cost_krw=1.5,
                                              audio_output_tokens=10)
        return b"pcm", 1.5, 1, 10
    monkeypatch.setattr(core, "synthesize_tts_pcm", _synth)
    claimed = []
    prep = SimpleNamespace(claim_cost_operation=lambda op: claimed.append(op) or True)
    cog = game_mod.GameCog.__new__(game_mod.GameCog)
    cog.bot = bot
    res = await cog._synthesize_and_enqueue(s, ["문단 하나", "문단 둘"], preparation=prep)
    assert res["enqueued"] == 2 and len(claimed) == 2
    assert all(op.event_ids for op in claimed)                       # Settlement가 읽을 event id


def test_auto_turn_dubbing_not_excluded_by_gm_prefix():
    src = (REPO / "cogs" / "game.py").read_text(encoding="utf-8")
    assert "_dub_turn = (not cost_log_prefix) or preparation is not None" in src


# ── `!세션종료` → 세션 시간 통계·명예의 전당 갱신 ─────────────────

async def test_end_session_updates_stats_and_boards(wired_bot, session_auto_ready, master_channel, monkeypatch):
    import cogs.system as system_mod
    s = session_auto_ready
    s.started_at = 1.0
    bumps, boards = [], []

    async def _close(bot, session, reason=None):
        return {"ok": True}

    async def _bump(uid, **d):
        bumps.append((uid, d))

    async def _boards(bot, guild):
        boards.append(guild)
        return True

    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(core.world_lifecycle, "close_session", _close)
    monkeypatch.setattr(core.world_lifecycle, "close_blocked_reason", lambda session: None)
    monkeypatch.setattr(core.stats, "bump", _bump)
    monkeypatch.setattr(core, "refresh_boards", _boards)
    monkeypatch.setattr(core, "send_streamed", _noop)
    monkeypatch.setattr(core, "save_session_data", _noop)
    sent = []

    async def _send(content=None, **_k):
        sent.append(content)
    guild = SimpleNamespace(default_role=object())
    ctx = SimpleNamespace(channel=master_channel, guild=guild, author=FakeUser(1), send=_send)
    cog = system_mod.SystemCog.__new__(system_mod.SystemCog)
    cog.bot = wired_bot
    await system_mod.SystemCog.end_session.callback(cog, ctx)
    assert any(uid == PLAYER_UID and d.get("session_seconds", 0) > 0 for uid, d in bumps)
    assert boards == [guild]
