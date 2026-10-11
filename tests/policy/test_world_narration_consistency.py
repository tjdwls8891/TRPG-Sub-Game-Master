# -*- coding: utf-8 -*-
"""묘사↔정본 정합 — 장소 고정(NONE·LOCAL)·목적지 되묻기·묘사 세계 블록·장면 인물 독립 블록,
세션 닫힘 중 선언 보관·다시 열기 후 이어서 진행·자동 GM 발언 단일 기록."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core import spatial as SP
from core import turn_transaction as tt
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, drig, judgment, rig, turn)

pytestmark = pytest.mark.policy

LOCK = "PC는 여전히 항구에 있다"


@pytest.mark.parametrize("intent", [{}, {"movement_kind": "LOCAL_MICRO_MOVE"}])
async def test_no_canonical_move_locks_place_for_director_and_narration(drig, intent):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, **intent)
    await turn(r, "계단을 오른다")
    lprompt = r.prov.prompts["logic"][0][0]
    nprompt = r.prov.prompts["narration"][0][0]
    assert LOCK in lprompt and LOCK in nprompt
    # 묘사층위: 정본 장소·출구 블록(레거시 '갈 수 있는 곳' 대체) + 장면 인물 독립 블록(지시문에 접히지 않음)
    assert "[현재 장소 — 정본]" in nprompt and "[갈 수 있는 곳]" not in nprompt
    assert "\n[현재 장면 인물" in nprompt and "목록 밖 정의 인물" in nprompt
    assert SP.placement_of(r.sess, PC) == SP.at_place("항구")


@pytest.mark.parametrize("dest", ["NONE", "P99"])
async def test_unresolved_destination_asks_with_visible_exits_without_narration(drig, dest):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, movement_kind="DESTINATION", destination=dest)
    tx = await turn(r, "사람이 있을 만한 곳으로 간다")
    assert "logic" not in r.prov.calls and "narration" not in r.prov.calls
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    asked = [m.content for m in r.gch.sent if "어디로 가시겠습니까" in (m.content or "")]
    assert asked and "시장" in asked[0]
    assert SP.placement_of(r.sess, PC) == SP.at_place("항구")


def _msg(r, text):
    return SimpleNamespace(content=text, channel=r.gch, author=SimpleNamespace(display_name="테스터", id=0))


async def test_closed_session_holds_declaration_and_reopen_resumes_once(drig):
    r = drig
    r.make()
    r.sess.gm_active = True
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "준비")                                     # 자금·첫 턴 정리
    r.prov.calls.clear()
    logs_before = list(r.sess.current_turn_logs)
    cache = r.sess.cache_name
    r.sess.cache_name = None                                 # 유지 시간 만료로 닫힘
    await r.gm._handle_player_message(r.sess, _msg(r, "칼을 준비한다"))
    assert r.prov.calls == [] and r.sess._held_declaration["content"] == "칼을 준비한다"
    assert r.sess.current_turn_logs == logs_before            # 거부된 선언은 턴 맥락에 기록하지 않는다
    assert any("보관" in (m.content or "") for m in r.gch.sent)
    await r.gm._handle_player_message(r.sess, _msg(r, "칼을 들고 기다린다"))    # 새 선언이 보관분을 교체
    r.sess.cache_name = cache                                # 다시 열기
    assert await r.gm.resume_held_declaration(r.sess)
    assert "judgment" in r.prov.calls and r.sess._held_declaration is None
    jtext = r.prov.prompts["judgment"][-1][0]
    assert "칼을 들고 기다린다" in jtext and "칼을 준비한다" not in jtext
    assert sum("칼을 들고 기다린다" in x for x in r.sess.current_turn_logs) <= 1
    assert not await r.gm.resume_held_declaration(r.sess)    # 한 번만


async def test_game_cog_does_not_duplicate_turn_log_when_auto_gm_active(drig):
    r = drig
    r.make()
    r.sess.gm_active = True
    r.bot.user = object()
    r.bot.active_sessions[r.gch.id] = r.sess
    r.sess.game_ch_id = r.gch.id
    msg = SimpleNamespace(content="문을 연다", channel=r.gch,
                          author=SimpleNamespace(id=0, display_name="테스터", bot=False))
    before = len(r.sess.current_turn_logs)
    await r.game.on_message(msg)
    assert len(r.sess.current_turn_logs) == before
    r.sess.gm_active = False                                  # 자동 GM이 꺼져 있으면 기존대로 기록
    await r.game.on_message(msg)
    assert r.sess.current_turn_logs[-1].endswith("문을 연다")


def test_reopen_view_is_wired_to_resume_held_declaration():
    import inspect
    import cogs.gm as gm
    assert "on_reopen=self.resume_held_declaration" in inspect.getsource(gm.GMCog)
    src = inspect.getsource(gm.OpenConfirmView.confirm)
    assert src.index("upload_cache") < src.index("self.on_reopen(self.session)")
