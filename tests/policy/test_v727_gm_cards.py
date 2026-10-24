"""v7.27 GM 스페이스 카드(홈·명예의 전당·서버보드·월드보드·내 통계)·시작 상황 선택 카드 — SVG 이미지.

표시 정보는 기존 Embed와 같은 출처·규칙, 렌더 실패는 기존 Embed/글로 대체, 선택은 기존 버튼.
"""
from __future__ import annotations

import types
import xml.etree.ElementTree as ET

import pytest

import core
from core import display_board as DB
from core import gm_board as GB
from core import gm_space as GS
from core import stats as stats_mod

pytestmark = pytest.mark.policy
PNG = b"\x89PNG-fake"


@pytest.fixture
def fake_render(monkeypatch):
    calls = []
    monkeypatch.setattr(DB, "render_png", lambda svg: calls.append(svg) or PNG)
    GS._SHOWN_CARD.clear()
    return calls


@pytest.fixture
def broken_render(monkeypatch):
    def _boom(svg):
        raise RuntimeError("renderer missing")
    monkeypatch.setattr(DB, "render_png", _boom)
    GS._SHOWN_CARD.clear()


ST = {"turns": 1532, "sessions": 12, "session_seconds": 7200, "quests_cleared": 4, "npcs_met": 23,
      "dice_rolled": 311, "status_applied": 7, "status_cleared": 5, "ink_spent": 4210}


def _rows(flags):
    return [{"user_id": uid, "value": 100 - i, "stats": dict(ST, turns=100 - i, **f)}
            for i, (uid, f) in enumerate(flags)]


# ── 데이터: 기존 Embed와 같은 출처·규칙 ─────────────────────────

def test_stat_items_match_format_summary():
    text = stats_mod.format_summary(ST)                              # 기존 Embed 문구와 같은 값
    assert GB.stat_items(ST) == [("플레이 턴", "1,532"), ("세션", "12 (2.0시간)"), ("클리어 퀘스트", "4"),
                                 ("만난 NPC", "23"), ("주사위", "311"), ("상태이상 획득/해제", "7 / 5"),
                                 ("소모", "4,210잉크")]
    for frag in ("플레이 턴 1,532", "세션 12 (2.0시간)", "클리어 퀘스트 4", "만난 NPC 23", "주사위 311",
                 "상태이상 획득 7 / 해제 5", "소모 4,210잉크"):
        assert frag in text


def test_hall_and_boards_keep_visibility_rules_and_use_display_names(monkeypatch):
    seen = []

    def fake_lb(ids, *, key="turns", only_registered=False, limit=20):
        seen.append(only_registered)
        return _rows([("1", {"hall_registered": True, "public": False}), ("2", {"hall_registered": False,
                                                                                 "public": True})])
    monkeypatch.setattr(stats_mod, "leaderboard", fake_lb)
    names = {"1": "바다건너온자"}
    hall = GB.hall_data(["1", "2"], names, "서버")
    assert seen[-1] is True and hall["rows"][0]["name"] == "바다건너온자"
    assert hall["rows"][1]["name"] == GB.UNKNOWN_NAME                  # 이름을 모르면 멘션 대신 고정 문구
    server = GB.board_data(["1", "2"], names)
    world = GB.board_data(["1", "2"], names, world=True)
    assert [len(r["items"]) for r in server["rows"]] == [3, 1]          # 서버보드: 명전 등록자만 상세
    assert [len(r["items"]) for r in world["rows"]] == [1, 3]           # 월드보드: 공개 선택자만 상세


# ── SVG ──────────────────────────────────────────────────────

def test_cards_are_valid_svg_escape_names_and_only_hall_is_gold():
    rows = [{"rank": i, "name": n, "items": GB.stat_items(ST)} for i, n in enumerate(["<b>&", "둘", "셋", "넷"], 1)]
    hall = GB.build_rank_svg({"kind": "hall", "title": "명예의 전당", "latin": "HALL OF FAME", "basis": "서버",
                              "subtitle": "플레이 턴 순위", "empty": "", "rows": rows})
    ET.fromstring(hall)
    assert b"&lt;b&gt;&amp;" in hall
    for medal in (b'fill="url(#gold)"', b'fill="url(#silver)"', b'fill="url(#bronze)"'):
        assert medal in hall
    board = GB.build_rank_svg({"kind": "server", "title": "서버보드", "latin": "SERVER BOARD", "basis": "기준",
                               "subtitle": "플레이 턴 순위", "empty": "없음", "rows": rows})
    ET.fromstring(board)
    assert b"url(#gold)" not in board
    empty = GB.build_rank_svg({"kind": "world", "title": "월드보드", "latin": "WORLD BOARD", "basis": "기준",
                               "subtitle": "플레이 턴 순위", "empty": "기록이 없습니다", "rows": []})
    assert "기록이 없습니다".encode() in empty
    for svg in (GB.build_home_svg(GB.home_data("1.2.3")), GB.build_personal_svg(GB.personal_data("나", ST))):
        ET.fromstring(svg)
    assert b"v1.2.3" in GB.build_home_svg(GB.home_data("1.2.3"))


def test_frames_card_numbers_titles_and_wrapped_summaries():
    opts = [{"title": "계단의 빈 고리", "summary": "새벽. " + "아주 긴 요약 문장이 이어진다. " * 12},
            {"title": "헹구는 손", "summary": "한낮."}, {"title": "불이 꺼지는 이유", "summary": "밤."}]
    svg = GB.build_frames_svg(GB.frames_data(opts))
    ET.fromstring(svg)
    for o in opts[1:]:
        assert o["title"].encode() in svg
    lines = GB.wrap(opts[0]["summary"], 22, 900, 3)
    assert len(lines) == 3 and lines[-1].endswith("…")
    assert all(DB.text_width(x, 22) <= 900 + 22 for x in lines)


# ── 송출: 이미지 · 같은 그림 재업로드 생략 · 렌더 실패 대체 ─────────

class _Msg:
    def __init__(self, author):
        self.author, self.edits = author, []

    async def edit(self, **kw):
        self.edits.append(kw)


class _Chan:
    def __init__(self, cid, bot_user):
        self.id, self.bot_user, self.sent, self.msgs = cid, bot_user, [], []

    def history(self, limit=10):
        async def gen():
            for m in self.msgs:
                yield m
        return gen()

    async def send(self, content=None, **kw):
        self.sent.append(dict(kw, content=content))
        m = _Msg(self.bot_user)
        self.msgs.append(m)
        return m


def _bot():
    return types.SimpleNamespace(user=types.SimpleNamespace(id=1))


async def test_upsert_card_sends_image_then_skips_unchanged(fake_render):
    bot = _bot()
    ch = _Chan(10, bot.user)
    svg = GB.build_home_svg(GB.home_data("1.0.0"))
    await GS._upsert_card(bot, ch, svg, "gm_home.png", "EMBED", view="VIEW", content="초대 링크: x")
    assert ch.sent[0]["file"].filename == "gm_home.png" and ch.sent[0]["view"] == "VIEW"
    assert ch.sent[0]["content"] == "초대 링크: x" and "embed" not in ch.sent[0]
    await GS._upsert_card(bot, ch, svg, "gm_home.png", "EMBED", view="VIEW2")
    assert len(fake_render) == 1 and ch.msgs[0].edits == [{"content": None, "view": "VIEW2"}]   # 같은 그림 — 본문·버튼만
    await GS._upsert_card(bot, ch, GB.build_home_svg(GB.home_data("1.0.1")), "gm_home.png", "EMBED")
    e = ch.msgs[0].edits[-1]
    assert e["embed"] is None and e["attachments"][0].filename == "gm_home.png"


async def test_upsert_card_falls_back_to_embed_when_render_fails(broken_render):
    bot = _bot()
    ch = _Chan(11, bot.user)
    await GS._upsert_card(bot, ch, GB.build_home_svg(GB.home_data("1.0.0")), "gm_home.png", "EMBED")
    assert ch.sent[0]["embed"] == "EMBED" and "file" not in ch.sent[0]


async def test_start_frame_choice_is_one_card_with_existing_buttons(fake_render, monkeypatch):
    opts = [{"id": f"f{i}", "title": f"틀{i}", "summary": f"요약{i}"} for i in range(1, 4)]
    monkeypatch.setattr(core.start_frame, "offer", lambda data, profile: opts)
    s = types.SimpleNamespace(players={"7": {"name": "P", "profile": {}}}, scenario_data={})
    ch = _Chan(12, None)
    await core.session_flow._offer_start_frames(_bot(), s, ch)
    sent = ch.sent[0]
    assert sent["file"].filename == "start_frames.png" and "embed" not in sent
    labels = [c.label for c in sent["view"].children]
    assert labels == ["1. 틀1", "2. 틀2", "3. 틀3"]                     # 선택은 기존 버튼(카드 번호와 같음)
    for o in opts:
        assert o["title"].encode() in fake_render[0]


async def test_start_frame_choice_falls_back_to_list_when_render_fails(broken_render, monkeypatch):
    opts = [{"id": "f1", "title": "틀1", "summary": "요약1"}]
    monkeypatch.setattr(core.start_frame, "offer", lambda data, profile: opts)
    s = types.SimpleNamespace(players={"7": {"name": "P", "profile": {}}}, scenario_data={}, master_gender="male")
    ch = _Chan(13, None)
    await core.session_flow._offer_start_frames(_bot(), s, ch)
    sent = ch.sent[0]
    assert sent.get("embed") is not None or "틀1" in (sent.get("content") or "")
    assert [c.label for c in sent["view"].children] == ["1. 틀1"]


def test_home_invite_is_a_link_button_not_a_raw_url():
    url = "https://discord.com/oauth2/authorize?client_id=1&scope=bot"
    bot = types.SimpleNamespace(invite_url=url)
    links = [c for c in GS.GMHomeView(bot).children if getattr(c, "url", None)]
    assert len(links) == 1 and links[0].url == url and links[0].label == "INDAIM 초대하기"
    assert not [c for c in GS.GMHomeView(types.SimpleNamespace(invite_url="")).children if getattr(c, "url", None)]
    field = next(f for f in GS.build_home_embed(bot).fields if f.name == "초대")
    assert field.value == f"[✉️ INDAIM을 내 서버에 초대하기]({url})"
