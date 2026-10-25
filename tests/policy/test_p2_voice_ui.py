"""P2 — 음성(#9·#10)·마스터 성별(Q1~Q8)·프로필 저장(#3)·대화형 메시지(#4)·SVG 로딩/오류(#6·Q9·Q10)·마스터 슬롯(#7).

실제 공급자 호출 없음 — 합성 함수·렌더 함수는 대역으로 바꾼다.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib

import discord
import pytest

import core
from core import conversational as CV
from core import master_art as MA
from core import speaker_card as SC
from core import voice_profile as VP
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeUser
from tests.policy.test_v715_character_creation import (  # noqa: F401 — 캐릭터 생성 하네스 재사용
    _prefill_start, labels, pc, pick, press, st, through_talk)


# ── #10 음성 프로필 ─────────────────────────────────────────────

def test_master_voice_by_gender_same_persona(session_factory):
    s = session_factory()
    assert VP.master_gender(s) == "male" and VP.master_voice(s) == core.TTS_NARRATOR_VOICE   # 구 세션 = 기존 목소리
    s.master_gender = "female"
    assert VP.master_voice(s) == "Achernar" and VP.VOICE_GENDER["Achernar"] == "female"
    s.master_gender = "bogus"
    assert VP.master_gender(s) == "male"
    assert set(VP.MASTER_VOICES.values()) <= set(core.TTS_VOICES)


def test_voice_gender_table_covers_provider_list():
    assert set(VP.VOICE_GENDER) == set(core.TTS_VOICES)


def test_gender_text_parsing_does_not_guess():
    assert VP.gender_of_text("60대 남성") == "male"
    assert VP.gender_of_text("20대 여성, 어부") == "female"
    assert VP.gender_of_text("나이 불명") is None
    assert VP.gender_of_text(None) is None


def test_npc_pool_excludes_master_voices_and_matches_gender():
    for g in ("male", "female"):
        pool = VP.npc_pool(g)
        assert pool and not set(pool) & set(VP.MASTER_VOICES.values())
        assert all(VP.VOICE_GENDER[v] == g for v in pool)


def test_npc_voice_stable_across_sessions_and_persisted(tmp_path):
    path = str(tmp_path / "va.json")
    a = VP.assigned_voice("영도", "김노인", "male", path=path)
    rec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    assert rec["assignments"]["영도|김노인"]["voice"] == a and VP.VOICE_GENDER[a] == "male"
    assert VP.assigned_voice("영도", "김노인", "male", path=path) == a                 # 재시작·다른 세션
    # 기록이 결정적 값보다 우선한다(목록 변경에도 이미 정한 인물 불변)
    other = next(v for v in VP.npc_pool("male") if v != a)
    rec["assignments"]["영도|김노인"]["voice"] = other
    pathlib.Path(path).write_text(json.dumps(rec), encoding="utf-8")
    assert VP.assigned_voice("영도", "김노인", "male", path=path) == other
    # 시나리오가 다르면 독립
    assert VP.deterministic_voice("무협", "김노인", "male") in VP.npc_pool("male")


def test_npc_key_uses_authored_key_not_display_guess(session_factory, monkeypatch, tmp_path):
    s = session_factory()
    s.scenario_id = "t"
    s.scenario_data = {"default_npcs": {"김노인": {"나이·성별": "70대 남성"},
                                        "a1": {"name": "쌍둥이", "나이·성별": "여성"},
                                        "a2": {"name": "쌍둥이", "나이·성별": "여성"}}}
    assert VP.npc_key_of(s, "김노인") == ("김노인", "70대 남성")
    assert VP.npc_key_of(s, "쌍둥이") == (None, None)                                # 모호 — 추정하지 않음
    real = VP.assigned_voice
    monkeypatch.setattr(VP, "assigned_voice", lambda sc, k, g: real(sc, k, g, path=str(tmp_path / "va.json")))
    v = VP.npc_voice(s, "김노인")
    assert VP.VOICE_GENDER[v] == "male"


def test_paragraph_voice_dialogue_npc_and_narration_master(session_factory, monkeypatch):
    s = session_factory()
    s.master_gender = "female"
    monkeypatch.setattr(VP, "npc_voice", lambda sess, name: "Charon")
    v, st_, spoken, kind = VP.paragraph_voice(s, "@대사:김노인|누구냐.")
    assert (v, kind) == ("Charon", VP.KIND_NPC) and st_ == VP.NPC_STYLE and "누구냐" in spoken
    v, st_, spoken, kind = VP.paragraph_voice(s, "안개가 짙다.")
    assert (v, kind, st_) == ("Achernar", VP.KIND_NARRATION, None)                   # 내레이션 = 마스터(Q8)
    assert VP.style_for(VP.KIND_MASTER) == VP.MASTER_STYLE and "절제" in VP.MASTER_STYLE
    assert "과장" in VP.NPC_STYLE and "중립" in VP.NPC_STYLE                            # 절제된 감정(Q4)


# ── #9 TTS 실효 · OFF = 합성 0 ───────────────────────────────────

def test_tts_status_distinguishes_off_unconnected_live(session_factory, monkeypatch):
    s = session_factory()
    core.media_control.sync_tts_flag(s, False)
    assert core.media_control.tts_status(s) == (False, False, "OFF")
    core.media_control.sync_tts_flag(s, True)
    monkeypatch.setattr(core.audio_mixer, "get_mixer", lambda vc: None)
    assert core.media_control.tts_status(s) == (True, False, "NO_VOICE")
    assert core.media_control.tts_status_label(s) == "미연결"
    monkeypatch.setattr(core.audio_mixer, "get_mixer", lambda vc: object())
    assert core.media_control.tts_status_label(s) == "ON"


async def test_tts_off_or_half_gate_means_zero_synthesis(fake_bot, session_factory, monkeypatch):
    from cogs.game import GameCog
    calls = []

    async def synth(*a, **k):
        calls.append(a)
        return (b"x", 1.0, 1, 1)
    monkeypatch.setattr(core, "synthesize_tts_pcm", synth)
    monkeypatch.setattr(core, "get_mixer", lambda vc: object())
    s = session_factory()
    cog = GameCog(fake_bot)
    core.media_control.sync_tts_flag(s, False)
    r = await cog._synthesize_and_enqueue(s, ["안녕"])
    assert r.get("disabled") and calls == []
    s.tts_enabled = True                                    # 한쪽 게이트만 켜짐 — 여전히 0
    r = await cog._synthesize_and_enqueue(s, ["안녕"])
    assert r.get("disabled") and calls == []


async def test_tuple_items_carry_voice_and_style(fake_bot, session_factory, monkeypatch):
    from cogs.game import GameCog
    seen = []

    class Mixer:
        def enqueue_voice(self, src):
            pass

    async def synth(bot, text, voice_name=None, *, cost_context=None, style=None):
        seen.append((voice_name, style, text))
        return (b"x", 0.0, 0, 0)
    monkeypatch.setattr(core, "synthesize_tts_pcm", synth)
    monkeypatch.setattr(core, "get_mixer", lambda vc: Mixer())
    s = session_factory()
    core.media_control.sync_tts_flag(s, True)
    await GameCog(fake_bot)._synthesize_and_enqueue(s, [("Charon", VP.NPC_STYLE, "대사"), "본문"])
    assert seen == [("Charon", VP.NPC_STYLE, "대사"), (VP.master_voice(s), None, "본문")]


async def test_master_direct_speech_voiced_only_when_live_and_in_transaction(fake_bot, session_factory,
                                                                             monkeypatch):
    import cogs.gm as gm_mod
    got = []

    class Game:
        async def _synthesize_and_enqueue(self, session, items, **kw):
            got.append((items, kw))
            return {"enqueued": len(items), "total": len(items), "cost": 0.0, "in": 0, "out": 0}
    fake_bot.get_cog = lambda name: Game() if name == "GameCog" else None
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = fake_bot
    s = session_factory()
    s.master_gender = "female"
    core.media_control.sync_tts_flag(s, False)
    await gm._voice_lines(s, ["어디로 가시겠습니까?"], transaction_id="tx1")
    assert got == []                                                                   # OFF → 0
    core.media_control.sync_tts_flag(s, True)
    monkeypatch.setattr(core.audio_mixer, "get_mixer", lambda vc: None)
    await gm._voice_lines(s, ["어디로 가시겠습니까?"], transaction_id="tx1")
    assert got == []                                                                   # 미연결 → 0
    monkeypatch.setattr(core.audio_mixer, "get_mixer", lambda vc: object())
    await gm._voice_lines(s, ["어디로 가시겠습니까?"], transaction_id=None)
    assert got == []                                                                   # 정산 귀속 없음 → 0
    await gm._voice_lines(s, ["어디로 가시겠습니까?"], transaction_id="tx1")
    (items, kw), = got
    assert items[0][:2] == ("Achernar", VP.MASTER_STYLE) and kw == {"claim_tx": "tx1"}
    s.master_gender = "male"                                                           # Q6: 이후 출력부터
    await gm._voice_lines(s, ["다음"], transaction_id="tx2")
    assert got[-1][0][0][0] == core.TTS_NARRATOR_VOICE


def test_master_gender_session_field_default_and_migration(session_factory):
    assert core.SESSION_FIELDS["master_gender"] == "male"
    assert session_factory().master_gender == "male"


async def test_display_master_gender_button_owner_only(fake_bot, session_factory, monkeypatch):
    from core import display as D
    s = session_factory()
    s.creation_state = {"owner": "111"}
    fake_bot.active_sessions[s.display_ch_id or 0] = s
    monkeypatch.setattr(core.session_flow, "flow_owner", lambda sess: "111")

    async def not_owner(u):
        return False
    fake_bot.is_owner = not_owner
    saved, refreshed = [], []

    async def save(bot, sess):
        saved.append(1)

    async def refresh(bot, sess, reason=None):
        refreshed.append(reason)
    monkeypatch.setattr(core.io, "save_session_data", save)
    monkeypatch.setattr(D, "refresh", refresh)
    view = D.DisplayView(fake_bot)
    btn = next(c for c in view.children if getattr(c, "custom_id", None) == "disp:master_gender")
    ch = FakeChannel(s.display_ch_id or 0)
    it = FakeInteraction(user=FakeUser(222), channel=ch)
    await btn.callback(it)
    assert VP.master_gender(s) == "male" and not saved                                # 개설자 아님
    it = FakeInteraction(user=FakeUser(111), channel=ch)
    await btn.callback(it)
    assert s.master_gender == "female" and saved and refreshed == ["master_gender"]


# ── #3 불러온 프로필 저장 ─────────────────────────────────────────

FIELDS = {"성별": "남", "이름": "홍길동", "직업": "어부", "스킬": ["그물"], "능력치": {"근력": 12, "민첩": 9},
          "외형": "마른 체형", "복장": "작업복"}


async def test_unchanged_loaded_profile_hides_save(pc, monkeypatch):
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: True)
    await _prefill_start(pc, dict(FIELDS))
    assert core.profile_creation_ui.profile_changed(st(pc).run) is False
    await press(pc, "✅ 이대로 시작")
    await through_talk(pc)                                                   # v7.36 '완성' 한마디 응답
    assert "💾 사전 프로필로 저장" not in labels(st(pc)) and "계속" in labels(st(pc))


async def test_changed_loaded_profile_confirms_once_then_saves_new(pc, monkeypatch):
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: True)
    original = await _prefill_start(pc, dict(FIELDS))
    await pick(pc, "고칠 항목", "직업")
    await press(pc, "목수")
    await press(pc, "확정")
    assert core.profile_creation_ui.profile_changed(st(pc).run) is True
    await press(pc, "✅ 이대로 시작")
    await through_talk(pc)
    await press(pc, "💾 사전 프로필로 저장")
    path = pathlib.Path(f"profiles/{PLAYER_UID}.json")
    assert json.loads(path.read_text(encoding="utf-8")) == original                 # 확인 전 저장 없음
    assert labels(st(pc)) == ["저장", "취소"]
    await press(pc, "취소")
    assert "💾 사전 프로필로 저장" in labels(st(pc))
    await press(pc, "💾 사전 프로필로 저장")
    await press(pc, "저장")
    now = json.loads(path.read_text(encoding="utf-8"))
    assert len(now["profiles"]) == 2 and now["profiles"][0] == original["profiles"][0]   # 원본 불변·새 저장


def test_profile_changed_none_without_loaded():
    assert core.profile_creation_ui.profile_changed({"chosen": {}}) is None


# ── #4 대화형 메시지 ────────────────────────────────────────────

def test_conversational_split_content_and_card():
    e = discord.Embed(title="제목", description="본문")
    e.add_field(name="​", value="이어서", inline=False)
    e.add_field(name="예상 비용", value="3 잉크")
    e.set_footer(text="2/17")
    content, card = CV.split(e)
    assert content.startswith("**제목**") and "이어서" in content and "-# 2/17" in content
    assert card is not None and [f.name for f in card.fields] == ["예상 비용"]
    assert CV.edit_kwargs(discord.Embed(description="x"))["embeds"] == []           # 낡은 Embed 제거
    long = discord.Embed(description=("가" * 100 + "\n\n") * 30)
    c2, card2 = CV.split(long)
    assert len(c2) <= CV.CONTENT_LIMIT and card2.description                         # 넘침은 카드로(자르지 않음)


async def test_initial_knowledge_receipt_found_in_content():
    from core import initial_knowledge as IK

    class M:
        def __init__(self, i, content):
            self.id, self.content, self.embeds, self.author = i, content, [], None

    class Ch:
        async def _gen(self):
            yield M(1, "**t**\n본문\n-# ik-abc · 1/2")
            yield M(2, "**t**\n-# ik-abc · 2/2")

        def history(self, limit=None):
            return self._gen()
    assert await IK.find_receipt(type("B", (), {"user": None})(), Ch(), "ik-abc", 2) == ["1", "2"]


# ── #7 마스터 슬롯 ──────────────────────────────────────────────

BUNDLED = {"indaim_loading_neutral.webp": "a28a590e", "indaim_error_neutral.webp": "75820e4e"}


def test_bundled_ui_assets_unchanged_and_master_art_slots_produced():
    for name, prefix in BUNDLED.items():
        h = hashlib.sha256(pathlib.Path(MA.UI_DIR, name).read_bytes()).hexdigest()
        assert h.startswith(prefix)
    assert sorted(f for f in os.listdir(MA.UI_DIR) if f.endswith(".webp")) == sorted(BUNDLED)
    # 2026-10-19 원화 반영: 6슬롯 전부 저장소 안 WEBP 파일(필기 포즈 2장은 슬롯 미배정 보관본)
    slots = json.loads(pathlib.Path(MA.MANIFEST).read_text(encoding="utf-8"))["slots"]
    assert sorted(slots) == sorted(MA.SLOTS) and all(v and v.endswith(".webp") for v in slots.values())
    assert sorted(os.listdir(MA.MASTER_DIR)) == sorted(
        ["manifest.json", *slots.values(), "master_female_writing.webp", "master_male_writing.webp"])
    MA.reload()
    for slot in MA.SLOTS:
        g, t = slot.split(".")
        path, state = MA.resolve(g, t)
        assert state == MA.PRODUCED and os.path.basename(path) == slots[slot]


def test_slot_states_distinct(monkeypatch, tmp_path):
    MA.reload()
    monkeypatch.setattr(MA, "_manifest", {"female.warning": None})                  # 의도적 미제작 슬롯
    assert MA.resolve("female", "warning") == (MA.LOADING_IMAGE, MA.UNPRODUCED)
    warned = []
    monkeypatch.setattr(core.logs, "warn", lambda *a, **k: warned.append(a))
    monkeypatch.setattr(MA, "_manifest", {"male.default": "missing.webp"})
    assert MA.resolve("male", "default") == (MA.ERROR_IMAGE, MA.ERROR)
    MA.resolve("male", "default")
    assert len(warned) == 1                                                          # 슬롯당 1회 기록
    good = tmp_path / "ok.webp"
    good.write_bytes(pathlib.Path(MA.LOADING_IMAGE).read_bytes())
    monkeypatch.setattr(MA, "MASTER_DIR", str(tmp_path))
    monkeypatch.setattr(MA, "_manifest", {"male.positive": "ok.webp"})
    assert MA.resolve("male", "positive") == (str(good), MA.PRODUCED)
    MA.reload()


def test_tone_from_existing_master_keys():
    assert MA.tone_of("session.cache_failed") == "warning"
    assert MA.tone_of("profile.completed") == "positive"
    assert MA.tone_of("profile.step") == "default"


# ── #6 SVG 로딩/오류(Q9·Q10) ────────────────────────────────────

class _Ctx:
    card, world, actor_id = {"variations": {}}, object(), "a1"


def _name(m):
    return getattr(m.files, "filename", "")


async def test_card_fast_render_sends_card_without_loading(session_factory, monkeypatch):
    async def render(session, ctx, prep=None):
        return b"\x89PNGfast"
    monkeypatch.setattr(SC, "_render_card", render)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, session_factory(), _Ctx(), threshold=1.0) == "CARD"
    assert [_name(m).startswith("speaker_card_") for m in ch.sent] == [True]


async def test_card_slow_render_loading_then_same_message_replaced(session_factory, monkeypatch):
    gate = asyncio.Event()

    async def render(session, ctx, prep=None):
        await gate.wait()
        return b"\x89PNGslow"
    monkeypatch.setattr(SC, "_render_card", render)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, session_factory(), _Ctx(), threshold=0.01) == "PENDING"
    (m,) = ch.sent
    assert _name(m).startswith("card_loading")
    gate.set()
    await asyncio.gather(*list(SC._PENDING))
    (edit,) = m.edit_history
    assert edit["attachments"][0].filename.startswith("speaker_card_") and len(ch.sent) == 1


async def test_card_failure_shows_error_image_no_text_fallback(session_factory, monkeypatch):
    async def render(session, ctx, prep=None):
        raise SC.CardError("down")
    monkeypatch.setattr(SC, "_render_card", render)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, session_factory(), _Ctx(), threshold=1.0) == "ERROR"
    (m,) = ch.sent
    assert _name(m).startswith("card_error") and not m.content


async def test_card_slow_failure_replaces_loading_with_error(session_factory, monkeypatch):
    gate = asyncio.Event()

    async def render(session, ctx, prep=None):
        await gate.wait()
        raise SC.CardError("late")
    monkeypatch.setattr(SC, "_render_card", render)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, session_factory(), _Ctx(), threshold=0.01) == "PENDING"
    gate.set()
    await asyncio.gather(*list(SC._PENDING))
    assert ch.sent[0].edit_history[0]["attachments"][0].filename.startswith("card_error")


async def test_card_replace_failure_falls_back_to_error_once(session_factory, monkeypatch):
    gate = asyncio.Event()

    async def render(session, ctx, prep=None):
        await gate.wait()
        return b"\x89PNGslow"
    monkeypatch.setattr(SC, "_render_card", render)
    ch = FakeChannel(1)
    await SC.deliver_card(ch, session_factory(), _Ctx(), threshold=0.01)
    m = ch.sent[0]
    real_edit = m.edit

    async def flaky(**kw):
        if kw["attachments"][0].filename.startswith("speaker_card_"):
            m.edit_history.append(dict(kw))
            raise RuntimeError("429")
        return await real_edit(**kw)
    m.edit = flaky
    gate.set()
    (res,) = await asyncio.gather(*list(SC._PENDING))
    assert res == "ERROR" and len(m.edit_history) == 2                                 # 유한(카드 1회 + 오류 1회)
    assert m.edit_history[-1]["attachments"][0].filename.startswith("card_error") and len(ch.sent) == 1


def test_profile_changed_ignores_visits_and_reverts():
    run = {"index": 0, "chosen": {"직업": "어부"}, "loaded_profile": {"fields": {"직업": "어부"}}}
    run["chosen"]["직업"] = "목수"
    run["chosen"]["직업"] = "어부"                                                     # 바꿨다가 원복
    assert core.profile_creation_ui.profile_changed(run) is False


async def test_no_card_sends_nothing(session_factory):
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, session_factory(), None) == "NONE" and ch.sent == []


def test_threshold_env(monkeypatch):
    monkeypatch.setenv("SVG_LOADING_THRESHOLD_SEC", "2.5")
    assert SC.loading_threshold_sec() == 2.5
    monkeypatch.setenv("SVG_LOADING_THRESHOLD_SEC", "x")
    assert SC.loading_threshold_sec() == 0.05                                       # v7.37 기본(그리지 않은 카드는 곧바로 로딩)


# ── 경계: 새 공급자 호출 없음 ──────────────────────────────────

def test_new_modules_make_no_provider_calls():
    for mod in ("voice_profile", "master_art", "conversational"):
        src = pathlib.Path(MA.ROOT, "core", f"{mod}.py").read_text(encoding="utf-8")
        assert "generate_content" not in src and "genai" not in src
