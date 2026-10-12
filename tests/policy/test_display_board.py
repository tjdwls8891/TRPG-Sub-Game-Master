"""v7.10 디스플레이 상태판 SVG — 데이터 출처·조립·전송(이미지 대체·Embed 대체 경로)·버튼 갱신."""
from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest

import core
from core import display_board as DB
from tests.fakes.discord_fakes import FakeInteraction

pytestmark = pytest.mark.policy

REPO = pathlib.Path(__file__).resolve().parents[2]
PNG = b"\x89PNG-fake"


@pytest.fixture
def fake_render(monkeypatch):
    calls = []

    def _render(svg):
        calls.append(svg)
        return PNG
    monkeypatch.setattr(DB, "render_png", _render)
    core.display._SHOWN_KEY.clear()
    return calls


def _player(s, name="테스터"):
    uid = next(iter(s.players))
    s.players[uid]["name"] = name
    s.players[uid]["profile"] = {"이름": name, "성별": "여", "나이": "22", "근력": 13,
                                 "외형": "아주 긴 외형 묘사가 이어지는 문장입니다. 스무 자를 넘깁니다."}
    return uid


# ── 데이터: build_embed와 같은 출처 ─────────────────────────

def test_board_data_matches_embed_sources(session_auto_ready):
    s = session_auto_ready
    _player(s)
    s.turn_count = 7
    s.world_timeline = {"current_date": "2016-10-02", "time_of_day": "밤", "elapsed_days": 3,
                        "current_location_display": "감지해변"}
    s.total_ink_spent, s.last_turn_ink, s.last_estimate = 1234, 0, {}
    s.statuses = {"테스터": ["저체온"]}
    s.resources = {"테스터": {"배낭": 1}}
    s.last_extraction = {"situation": {"tag": "대화", "tension": 65}, "npcs_met": ["윤점순"]}
    d = DB.board_data(s)
    assert (d["turn"], d["date_tod"], d["elapsed"], d["place"]) == ("7", "2016-10-02 · 밤", "3일", "감지해변")
    assert d["cost"] == {"total": "1,234잉크", "last": "—", "next": "—", "tts": None}
    p = d["players"][0]
    # D1: 20자 이하·이름과 다른 값만(이름·장문 외형 제외), 원래 순서
    assert p["fields"] == [("성별", "여"), ("나이", "22"), ("근력", "13")]
    assert p["statuses"] == ["저체온"] and p["resources"] == ["배낭 1"]
    assert ("등장 NPC", "윤점순") in d["narrative"]
    assert all("**" not in v for _k, v in d["narrative"])
    assert d["warning"] is False


def test_session_state_three_kinds(session_auto_ready):
    s = session_auto_ready
    s.cache_name, s.cache_expired_notified = None, False
    assert DB.session_state(s) == DB.STATE_CLOSED
    s.cache_expired_notified = True
    assert DB.session_state(s) == DB.STATE_EXPIRED


def test_latin_from_scenario_name_or_optional_key(session_auto_ready):
    s = session_auto_ready
    s.scenario_id = "영도"
    assert DB.latin_name(s) == "YEONGDO"
    s.scenario_data = dict(s.scenario_data or {}, display_latin="Shadow Island")
    assert DB.latin_name(s) == "SHADOW ISLAND"


# ── 조립 ──────────────────────────────────────────────────

def test_svg_is_valid_escaped_and_grows_with_players(session_auto_ready):
    s = session_auto_ready
    _player(s, name="A&B<x>")
    one = DB.build_svg(DB.board_data(s))
    root = ET.fromstring(one)                                     # 파싱 가능 = 이스케이프됨
    assert any(t.text == "A&B<x>" for t in root.iter("{http://www.w3.org/2000/svg}text"))
    d = DB.board_data(s)
    d["players"] = d["players"] * 3
    three = ET.fromstring(DB.build_svg(d))
    assert int(three.get("height")) > int(root.get("height"))


def test_chip_overflow_and_ellipsis():
    rows = DB._flow_capped([f"아이템{i} 1" for i in range(40)], lambda t: 200, 126, 1128, DB.CHIP_ROWS_MAX)
    assert len(rows) == DB.CHIP_ROWS_MAX and rows[-1][-1][2].startswith("+")
    assert DB.ellipsize("가" * 50, 20, 100).endswith("…")
    assert DB.text_width(DB.ellipsize("가" * 50, 20, 100), 20) <= 100


def test_conditional_sections(session_auto_ready):
    s = session_auto_ready
    s.extraction_pending = True
    s.cache_name, s.cache_expired_notified = None, True
    svg = DB.build_svg(DB.board_data(s)).decode("utf-8")
    assert "다음 턴이 차단됩니다" in svg and "캐시 만료" in svg
    s.extraction_pending, s.cache_expired_notified = False, False
    svg = DB.build_svg(DB.board_data(s)).decode("utf-8")
    assert "다음 턴이 차단됩니다" not in svg and "클로즈" in svg


def test_engine_has_no_scenario_specific_strings():
    src = (REPO / "core" / "display_board.py").read_text(encoding="utf-8")
    for word in ("영도", "YEONGDO", "감지해변", "김이서"):
        assert word not in src


# ── 전송: 이미지가 Embed를 대체, 실패 시 Embed ─────────────────

async def test_refresh_sends_image_then_skips_unchanged(wired_bot, session_auto_ready, display_channel, fake_render):
    s = session_auto_ready
    s.display_msg_id = None
    assert await core.display.refresh(wired_bot, s, reason="t")
    msg = display_channel.sent[0]
    assert msg.files is not None and not msg.embeds and msg.view is not None
    assert await core.display.refresh(wired_bot, s, reason="t")          # 같은 내용
    assert len(fake_render) == 1 and "attachments" not in msg.edit_history[-1]
    s.turn_count += 1                                                      # 내용 변경
    assert await core.display.refresh(wired_bot, s, reason="t")
    last = msg.edit_history[-1]
    assert len(fake_render) == 2 and last["embed"] is None and len(last["attachments"]) == 1
    assert len(display_channel.sent) == 1


async def test_render_failure_falls_back_to_embed(wired_bot, session_auto_ready, display_channel, monkeypatch):
    s = session_auto_ready
    s.display_msg_id = None
    core.display._SHOWN_KEY.clear()

    def _boom(svg):
        raise core.speaker_card.RendererUnavailable("없음")
    monkeypatch.setattr(DB, "render_png", _boom)
    assert await core.display.refresh(wired_bot, s, reason="t")
    msg = display_channel.sent[0]
    assert msg.embeds and not msg.files


async def test_toggle_defers_then_refreshes_board(wired_bot, session_auto_ready, display_channel, fake_render):
    s = session_auto_ready
    s.display_msg_id = None
    await core.display.refresh(wired_bot, s, reason="t")
    view = core.DisplayView(wired_bot)
    it = FakeInteraction(channel=display_channel, message=display_channel.sent[0])
    await view._toggle(it, "image")
    assert it.response.deferred and not it.response.edits
    assert len(fake_render) == 2 and display_channel.sent[0].edit_history[-1]["attachments"]
