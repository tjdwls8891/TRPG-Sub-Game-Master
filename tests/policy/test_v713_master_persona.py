"""v7.13.0 — 마스터 인물화 UI 표현 승격(사전 저작 대사·기존 문구 폴백) + 범위 밖 발견 수정.

새 이벤트·메시지·AI 호출 없이 기존 플레이어 대상 고정 안내만 대사 데이터로 바꾼다.
"""
from __future__ import annotations

import json
import pathlib
import re
from types import SimpleNamespace

import pytest

import core
from core import master
from tests.fakes.discord_fakes import FakeInteraction, FakeMessage, FakeUser
from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용

pytestmark = pytest.mark.policy

REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def first_variant(monkeypatch):
    """무작위 선택을 첫 바리에이션으로 고정."""
    monkeypatch.setattr(master.random, "choice", lambda seq: seq[0])


def _events():
    return json.loads((REPO / "assets" / "master_dialogues_v1.json").read_text(encoding="utf-8"))["events"]


# ── 자산 · 참조 키 ───────────────────────────────────────────

def test_asset_shape_and_slots():
    data = json.loads((REPO / "assets" / "master_dialogues_v1.json").read_text(encoding="utf-8"))
    slots = set(data["reserved_placeholders"])
    for key, ev in data["events"].items():
        vs = ev["variants"]
        assert len(vs) >= 2 and all(isinstance(v, str) and v.strip() for v in vs), key
        assert set(re.findall(r"\{([^{}]+)\}", " ".join(vs))) <= slots, key


def test_every_key_used_in_code_exists_in_asset():
    events = _events()
    used = set()
    for path in list((REPO / "core").glob("*.py")) + list((REPO / "cogs").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"""(?:master\.|_master\.|\b)(?:say|line|lead)\(\s*["']([a-z_]+\.[a-z_.]+)["']""", src))
    used |= {f"intro.topic_{k}" for k in core.intro.FULL_ORDER}
    used |= {"profile.stat_roll", "profile.ai_input", "profile.choice_warning", "profile.choice_confirm",
             "profile.begin", "profile.ai_warned", "profile.ai_rejected"}
    assert len(used) >= 60
    assert not (used - set(events)), sorted(used - set(events))


def test_no_provider_or_network_call_in_master_module():
    src = (REPO / "core" / "master.py").read_text(encoding="utf-8")
    for bad in ("genai", "generate_content", "requests", "urllib", "aiohttp", "http"):
        assert bad not in src, bad


def test_gm_home_untouched():
    src = (REPO / "core" / "gm_space.py").read_text(encoding="utf-8")
    body = src[src.index("async def refresh_home"):]
    body = body[:body.index("\nasync def ", 10) if "\nasync def " in body[10:] else len(body)]
    assert "master" not in body                                              # GM 홈 게시물은 그대로(A-01)


# ── 폴백 · 슬롯 안전 ─────────────────────────────────────────

def test_missing_key_or_slot_falls_back_to_original():
    assert master.line("no.such.key") is None
    assert master.say("no.such.key", "원래 문구") == "원래 문구"
    assert master.line("turn.next_player") is None                          # 필수 슬롯 없음
    assert master.line("turn.next_player", slots={"player_name": "   "}) is None
    assert master.lead("no.such.key", "정보", default="원래 안내") == "원래 안내\n정보"
    assert master.lead("no.such.key", "정보") == "정보"


def test_corrupt_asset_falls_back(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(master, "ASSET_PATH", str(bad))
    monkeypatch.setattr(master, "_cache", None)
    assert master.say("turn.busy", "원래 문구") == "원래 문구"
    monkeypatch.setattr(master, "_cache", None)


def test_slot_value_is_escaped_and_josa_fixed(first_variant, monkeypatch):
    out = master.line("turn.next_player", slots={"player_name": "@everyone`x`"})
    assert "@everyone" not in out and "\\`" in out
    assert out.endswith(_events()["turn.next_player"]["variants"][0].split("}", 1)[1])   # 슬롯 뒤 대사는 그대로
    for pick in (0, 1):                                                      # "{field_label}을 …" — 조사 보정
        monkeypatch.setattr(master.random, "choice", lambda seq, i=pick: seq[i])
        assert "외모를 " in master.line("profile.field_prompt", slots={"field_label": "외모"})
        assert "성별을 " in master.line("profile.field_prompt", slots={"field_label": "성별"})


# ── 대표 장면: 정보 보존 ─────────────────────────────────────

def test_info_blocks_preserved_in_representative_outputs(first_variant):
    # 잔액 부족 확인(수치 유지)
    s = core.master.lead("cache.insufficient_ink", "⚠️ 잔액이 부족합니다. 필요 **30잉크** / 보유 **5잉크**")
    assert "30잉크" in s and "5잉크" in s and s.startswith(_events()["cache.insufficient_ink"]["variants"][0])
    # 유지 시간 확인(잉크만, 마스터 질문)
    c = core.format_confirmation({"minutes": 180, "notes": []}, {"total_ink": 30})
    assert "30잉크" in c and _events()["cache.time_confirm"]["variants"][0] in c


def test_intro_new_greeting_keeps_seven_topic_guide(first_variant):
    g = core.intro.greeting(core.intro.LEVEL_NEW)
    assert g.startswith(_events()["intro.start_new"]["variants"][0]) and "일곱 가지" in g


async def test_next_player_uses_character_name_not_mention(rig, first_variant):
    r = rig
    r.sess.gm_pending_players = ["테스터"]
    r.sess.gm_collected_actions = {}
    await r.gm._ask_next_player(r.sess)
    text = "\n".join(m.content or "" for m in r.gch.sent)
    assert "테스터님" in text and "<@" not in text                            # 캐릭터 이름으로 부른다


# ── 범위 밖 발견 수정 ────────────────────────────────────────

def test_failed_turn_notice_does_not_point_to_missing_declaration():
    empty = core.build_failed_turn_notice("")
    assert "아래 선언" not in empty and "다시 입력" in empty
    full = core.build_failed_turn_notice("동굴로 간다")
    assert "아래 선언" in full and "```\n동굴로 간다\n```" in full


def test_close_failure_does_not_expose_exception_text():
    src = (REPO / "core" / "display.py").read_text(encoding="utf-8")
    assert "다시 시도해 주십시오. ({e})" not in src


async def test_auto_roll_disables_roll_button(rig):
    import cogs.gm as gm_mod
    r = rig

    async def _noop(*_a, **_k):
        return []
    r.gm._execute_rolls = _noop
    r.gm._continue_with_roll_results = _noop
    view = gm_mod.GMRollView(r.gm, r.sess, [], "x", [], None)
    view.message = FakeMessage(channel=r.gch)
    await view.on_timeout()
    assert all(c.disabled for c in view.children) and view.message.edit_history


async def test_common_profile_reachable_without_play_history(monkeypatch):
    from core import profile_ui
    monkeypatch.setattr(core.stats, "load_stats", lambda uid: {})
    view = profile_ui.ProfileHomeView(SimpleNamespace(), 7)
    inter = FakeInteraction(user=FakeUser(7))
    await view.create.callback(inter)
    sent = inter.response.messages[0]
    assert isinstance(sent.get("view"), profile_ui.ScenarioPickView)


async def test_upload_failure_message_suppressed_when_caller_reports(session_auto_ready, monkeypatch):
    import cogs.session as session_mod
    said = []

    async def _notify(text, **_k):
        said.append(text)

    async def _boom(*_a, **_k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(core.cache_lifecycle, "open_window", _boom)
    async def _noop(*_a, **_k):
        return None
    monkeypatch.setattr(core.interpretation_billing, "settle", _noop)
    s = session_auto_ready
    s.cache_name = None
    s.open_minutes = 60
    cog = session_mod.SessionCog.__new__(session_mod.SessionCog)
    cog.bot = SimpleNamespace(get_channel=lambda _i: None)
    assert await cog.upload_cache(s, notify=_notify, report_failure=False) is False
    assert not any("열지 못했" in t for t in said)
    said.clear()
    assert await cog.upload_cache(s, notify=_notify) is False
    assert any("⚠️" in t for t in said)


def test_profile_title_not_stuck_on_placeholder():
    from core import profile_creation_ui as PCU
    st = SimpleNamespace(char_name="(이름 미정)", run={"chosen": {}})
    assert PCU._title(st) == "캐릭터 만들기"
    st.run = {"chosen": {"이름": "임성진"}}
    assert PCU._title(st) == "임성진 만들기"


async def test_dynamic_retry_not_silent_when_not_retryable(session_auto_ready):
    import cogs.session as session_mod
    s = session_auto_ready
    s.world_lifecycle = core.world_lifecycle.LIFECYCLE_OPEN
    view = session_mod.DynamicOpenRetryView(SimpleNamespace(get_cog=lambda _n: None), s)
    inter = FakeInteraction(user=FakeUser(1))
    await view.retry.callback(inter)
    assert inter.followup.sent and "다시 열 상태가 아닙니다" in inter.followup.sent[0]["content"]
