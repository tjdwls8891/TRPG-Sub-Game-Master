# -*- coding: utf-8 -*-
"""v7.43 — 세션 제작 중 끝난 화면이 다음 단계 위에 남지 않는다(사용자 지적: 인물 완성·초기 지식 막바지 잔류).

인물 완성 화면은 저장 여부를 고르면 지워지고(남길 정보는 본인에게만), 초기 지식은 전달이 기록되면 지워진다.
초기 지식의 마무리 한마디는 따로 마지막 박자 — 마지막 항목에도 응답 버튼이 붙어 읽고 넘어간다.
"""
from __future__ import annotations

import pytest

from core import initial_knowledge as IK
from core import talk as T
from tests.conftest import PLAYER_UID
from tests.policy import test_v715_character_creation as C
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v715_character_creation import pc  # noqa: F401
from tests.policy.test_v717_initial_knowledge import Ch, ai, deliver, setup  # noqa: F401

pytestmark = pytest.mark.policy


async def test_knowledge_message_removed_after_delivery_and_last_item_gets_answer(rig, ai):
    s = setup(rig)
    ch = Ch()
    assert await deliver(rig, s, ch, "PROFILE") == IK.DELIVERED
    rec = IK.record(s, PLAYER_UID, "PROFILE")
    beats = T.parse(IK.talk_text(rec, "PROFILE"))
    assert rec["delivery_nonce"] in beats[-1].text and beats[-1].text.startswith("*")  # 마무리 한마디만
    assert "· " in beats[-2].text.splitlines()[-1][:2]                                 # 마지막 항목은 응답 박자
    assert rec["message_ids"] == [str(ch.sent[-1].id)] and ch.sent[-1].deleted


async def _to_save(r):
    await C.to_appearance(r)
    await C.guided_appearance(r)
    await C.press(r, "✅ 이대로 확정")
    await C.chat(r, "낡은 작업복")
    await C.press(r, "✅ 이대로 확정")
    await C.press(r, "✅ 이대로 시작")
    await C.through_talk(r)
    return C.st(r).message


async def test_completion_screen_removed_on_skip_without_leftover_note(pc):
    done = await _to_save(pc)
    before = len(pc.ch.sent)
    await C.press(pc, "저장 안 함")
    assert done.deleted
    assert not any("준비" in str(m.content or "") for m in pc.ch.sent[before:])


async def test_completion_screen_removed_on_save_tag_told_privately(pc):
    done = await _to_save(pc)
    await C.press(pc, "💾 사전 프로필로 저장")
    item = next(c for c in done.view.children if getattr(c, "label", None) == "저장")
    from tests.fakes.discord_fakes import FakeInteraction, FakeUser
    it = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=pc.ch, message=done)
    await item.callback(it)
    inter_sent = it.followup.sent
    assert done.deleted
    assert len(inter_sent) == 1 and inter_sent[0].get("ephemeral") and "태그" in inter_sent[0]["content"]
