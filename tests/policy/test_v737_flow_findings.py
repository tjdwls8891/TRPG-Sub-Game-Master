# -*- coding: utf-8 -*-
"""v7.37 테스트 세션 7b9cfeb5 개선(사용자 승인: D1 가 · D2 가 · D3 가 · D4 · D6 · A3 · A6).

카드 즉시 판별 · 초기 지식 대화 박자 · 인물 소개 박자 · 물건 낱말 대조·정체 확인 · 사건 지침 반복 억제.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import core
from cogs.session import SessionCog
from core import initial_knowledge as IK
from core import items as IT
from core import speaker_card as SC
from core import start_frame as SF
from core import talk as T
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeUser
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v717_initial_knowledge import Ch, ai, deliver, setup  # noqa: F401
from tests.policy.test_v717_items import S, man
from tests.policy.test_v734_prompt_assembly import ydyn  # noqa: F401

pytestmark = pytest.mark.policy


class _Ctx:
    card, world, actor_id, status, known, speaker = {"variations": {}}, object(), "a1", "KNOWN", frozenset(), "x"


def _name(m):
    return getattr(m.files, "filename", "")


async def _press(msg, ch):
    await msg.view.children[0].callback(FakeInteraction(FakeUser(int(PLAYER_UID)), ch, msg))


# ── R5: 카드 즉시 판별 ─────────────────────────────────────────────

async def test_cached_card_goes_out_at_once_without_render(monkeypatch):
    monkeypatch.setattr(SC, "cached_png", lambda *a, **k: b"\x89PNGcached")

    async def never(*a, **k):
        raise AssertionError("그려 둔 카드는 다시 렌더하지 않는다")
    monkeypatch.setattr(SC, "_render_card", never)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, SimpleNamespace(session_id="s"), _Ctx()) == "CARD"
    assert _name(ch.sent[0]).startswith("speaker_card_")


async def test_uncached_card_shows_loading_without_waiting(monkeypatch):
    monkeypatch.setattr(SC, "cached_png", lambda *a, **k: None)
    gate = asyncio.Event()

    async def slow(*a, **k):
        await gate.wait()
        return b"\x89PNGslow"
    monkeypatch.setattr(SC, "_render_card", slow)
    ch = FakeChannel(1)
    assert await SC.deliver_card(ch, SimpleNamespace(session_id="s"), _Ctx()) == "PENDING"
    assert _name(ch.sent[0]).startswith("card_loading") and SC.loading_threshold_sec() == 0.05
    gate.set()
    await asyncio.gather(*list(SC._PENDING))
    assert ch.sent[0].edit_history[0]["attachments"][0].filename.startswith("speaker_card_")


# ── D1: 초기 지식 대화 박자 ────────────────────────────────────────

async def test_initial_knowledge_beats_receipt_only_on_last_beat_and_restart_is_uncertain(rig, ai):
    r = rig
    s = setup(r)
    ch = Ch()
    from tests.policy.test_v717_initial_knowledge import defn
    st = await IK.run_phase(r.bot, s, PLAYER_UID, "PROFILE", ch, defn(s))
    rec = IK.record(s, PLAYER_UID, "PROFILE")
    assert st == IK.PRESENTING and len(ch.sent) == 1 and isinstance(ch.sent[0].view, T.TalkView)
    assert rec["delivery_nonce"] not in ch.sent[0].content                  # 영수증은 마지막 박자에만
    assert not any(c.get("kind") for c in (s.cognition_state or {}).values() if isinstance(c, dict))
    # 재시작(박자 위치 유실) — 마지막 박자를 그린 증거가 없으니 자동 인지·재전송 없이 UNCERTAIN
    T._POS.clear()
    assert await IK.run_phase(r.bot, s, PLAYER_UID, "PROFILE", ch, defn(s)) == IK.UNCERTAIN
    assert len(ch.sent) == 1


async def test_initial_knowledge_last_beat_marks_delivered_with_message_id(rig, ai):
    r = rig
    s = setup(r)
    ch = Ch()
    assert await deliver(r, s, ch, "PROFILE") == IK.DELIVERED
    rec = IK.record(s, PLAYER_UID, "PROFILE")
    assert rec["message_ids"] == [str(ch.sent[-1].id)] and rec["delivery_nonce"] in ch.sent[-1].content
    beats = T.parse(IK.talk_text(rec, "PROFILE"))
    assert len(beats) >= 2 and all(rec["delivery_nonce"] not in b.text for b in beats[:-1])


# ── R4·R1: 인물 소개 박자 ──────────────────────────────────────────

PROFILE = {"이름": "김이서", "나이": "23", "직업": "대학생", "외형": "마른 체격에 짧게 자른 머리. " * 6,
           "복장": "해진 바람막이와 젖은 운동화"}


def test_briefing_four_beats_uncut_and_skips_empty():
    beats = SF.briefing_beats({}, PROFILE, "물통 하나, 성냥 둘")
    assert len(beats) == 4 and "김이서" in beats[0]
    assert PROFILE["외형"].strip() in beats[1] and "해진 바람막이" in beats[2] and "성냥 둘" in beats[3]
    assert len(SF.briefing_beats({}, {"이름": "김이서"}, "")) == 1                    # 빈 항목 박자는 건너뜀
    sc = {"briefing_formats": {"identity": ["{이름}이라는 사람입니다."], "items": ["가진 것: {소지품}"]}}
    assert SF.briefing_beats(sc, PROFILE, "칼 하나")[0] == "김이서라는 사람입니다."       # 시나리오 박자 구조 우선
    assert "{" not in "".join(SF.briefing_beats({"briefing_formats": ["{이름}. {스킬}만 남았다."]}, PROFILE, ""))


def test_items_phrase_reads_canon_naturally():
    text = SF.items_phrase({"물통": 1, "성냥": 2, "물[ml]": 500, "칼〈고장〉": 1, "없는것": 0},
                           [{"name": "무전기"}])
    assert text == "물 500ml, 물통 하나, 성냥 둘, 칼(고장) 하나, 무전기"


def test_intro_instruction_no_longer_carries_briefing():
    text = SF.build_intro_instruction({"briefing_formats": ["{이름}입니다."]}, PROFILE, {"summary": "부두"})
    assert "김이서" not in text and "브리핑" not in text and "부두" in text


async def test_briefing_beats_then_enter_beat_opens_intro():
    ch = FakeChannel(1)
    s = SimpleNamespace(session_id="b1", scenario_data={}, resources={"김이서": {"물통": 1}}, item_instances={},
                        world_lifecycle=None, creation_state={})
    opened = []

    async def then():
        opened.append(True)
    cog = SimpleNamespace(bot=SimpleNamespace())
    assert await SessionCog._present_briefing(cog, s, PROFILE, ch, then)
    msg = ch.sent[0]
    seen = [msg.content]
    while isinstance(msg.view, T.TalkView):
        assert not opened
        await _press(msg, ch)
        seen.append(msg.content)
    assert opened == [True] and len(ch.sent) == 1 and "첫 장면을 열겠습니다" in seen[-1]
    assert any("물통 하나" in c for c in seen)


# ── A3·D6: 물건 이름 낱말 대조 · 정체 확인 ────────────────────────────

def test_new_item_name_matched_word_by_word():
    s = S({"P": {}})
    op = {"op": "ACQUIRE", "owner": "O1", "item": IT.NEW_KEY, "name": "금속 물체", "quantity": 1}
    p = IT.build_plan(s, man(s), [op], pc_name="P", transaction_id="t",
                      narration_text="바위 틈에서 금속의 질감이 느껴지는 물체를 낚아챈다")       # 세션 12턴 실측
    IT.apply_plan(s, p)
    assert s.resources["P"] == {"금속 물체": 1}
    with pytest.raises(IT.ItemContractError, match="본문에 없음"):
        IT.build_plan(S({"P": {}}), man(S({"P": {}})), [{**op, "name": "금속 상자"}], pc_name="P",
                      transaction_id="t", narration_text="금속의 질감")


def test_identify_renames_held_item_only_when_narrated():
    s = S({"P": {"금속 물체": 1, "칼": 1}})
    m = man(s)
    h = next(r[0] for r in m.holdings if "금속" in r[5])
    op = {"op": "IDENTIFY", "owner": "O1", "item": h, "name": "손전등", "quantity": 1}
    IT.apply_plan(s, IT.build_plan(s, m, [op], pc_name="P", transaction_id="t",
                                   narration_text="불을 비춰 보니 낡은 소형 손전등이다"))
    assert s.resources["P"] == {"손전등": 1, "칼": 1}                                   # 같은 물건 — 수량 불변
    s2 = S({"P": {"금속 물체": 1}})
    with pytest.raises(IT.ItemContractError, match="본문에 없음"):
        IT.build_plan(s2, man(s2), [op], pc_name="P", transaction_id="t", narration_text="물체를 살핀다")
    assert "IDENTIFY" in m.render() and "IDENTIFY" in m.schema()["items"]["properties"]["op"]["enum"]


# ── A6: 사건 지침 반복 억제 ─────────────────────────────────────────

async def test_event_guide_block_carries_repeat_rule(ydyn):
    r = ydyn
    await r.gm._process_actions(r.sess, "주변을 둘러본다", r.master)
    logic = "\n".join(p[0] for p in r.prov.prompts["logic"])
    assert "[진행 중인 사건 — 지시층위 전용" in logic and core.turn_world.GUIDE_REPEAT_RULE in logic
