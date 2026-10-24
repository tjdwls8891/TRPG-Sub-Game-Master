# -*- coding: utf-8 -*-
"""v7.33 예외 케이스 전수조사 결함 수정(사용자 승인: 보고 전체 · F4 '가' · F5 '재사용').

F1 묘사 이전 예외 누출 · F2 비밀 원문 버튼 등록부 · F3 판단·지시층위 실패 무안내 · F4 판정 대기 중 새 선언 ·
F5 굴림 뒤 실패 결과 보관·재사용 · F6 판정 화면 만료 안내 · F7 손상 계정 · F8 이동 중 표시 · F9 보고 갈래 분리 ·
① 초기화 실패 뒤 참가 · ⑤ 이미지 키워드 저장.
"""
from __future__ import annotations

import json
import types

import discord
import pytest

import core
from core import master_report as MR
from core import turn_transaction as tt
from core import ui_expiry as UX
from tests.fakes.discord_fakes import FakeInteraction, FakeUser
from tests.policy.test_h05bc_turn_wiring import drig, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_transaction_wiring import _make_gm_cog

pytestmark = pytest.mark.policy


def _texts(ch):
    return [m.content or "" for m in getattr(ch, "sent", [])]


# ── F1 · F3: 묘사 이전 실패는 청구 0 종결 + 플레이어 재선언 안내 ─────────────

async def test_unexpected_exception_before_narration_is_contained_and_announced(drig, monkeypatch):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment("시장")

    def boom(*a, **k):
        raise KeyError("계획 버그")
    monkeypatch.setattr(core.player_world, "plan_player_turn", boom)
    tx = await turn(r, "시장으로 간다")                                    # 예외가 입력 태스크 밖으로 새지 않는다
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_code == tt.FailureCode.UNEXPECTED_EXCEPTION.value
    assert tt.get_active_transaction(r.sess) is None and not r.sess.is_processing
    game = r.bot.get_channel(r.sess.game_ch_id)
    assert any("다시 입력" in t and "시장으로 간다" in t for t in _texts(game))
    assert any("청구 0" in t for t in _texts(r.master))


async def test_judgment_failure_tells_player_to_redeclare(drig):
    r = drig
    r.make()

    def down(kwargs):
        raise RuntimeError("provider down")
    r.prov.routes["judgment"] = down
    tx = await turn(r, "숨을 고른다")
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_code == tt.FailureCode.JUDGMENT_PROVIDER_FAILURE.value
    assert any("다시 입력" in t for t in _texts(r.bot.get_channel(r.sess.game_ch_id)))


# ── F4 · F5: 판정 대기 중 새 선언 차단 · 실패 결과 보관 뒤 재사용 ──────────────

async def test_new_declaration_is_blocked_while_roll_is_open(monkeypatch, wired_bot, session_auto_ready,
                                                             master_channel, game_channel):
    sess, cog = session_auto_ready, _make_gm_cog(wired_bot)
    tx = tt.get_or_begin_turn_transaction(sess, "문을 연다")
    tt.mark_waiting_for_roll(sess, tx.transaction_id)
    tx.roll_open = True
    entered = []

    async def loop(*a, **k):
        entered.append(1)
    monkeypatch.setattr(cog, "_run_gm_logic_loop", loop, raising=False)
    await cog._process_actions(sess, "다른 행동을 한다", master_channel)
    assert entered == [] and any("주사위 굴리기" in t for t in _texts(game_channel))
    tx.roll_open = False                                                  # 화면이 사라진(굴린) 판정은 막지 않는다
    await cog._process_actions(sess, "다른 행동을 한다", master_channel)
    assert entered == [1]


async def test_roll_results_are_kept_after_instruction_failure_and_reused(monkeypatch, wired_bot, session_auto_ready,
                                                                         master_channel, game_channel):
    sess, cog = session_auto_ready, _make_gm_cog(wired_bot)
    sess.gm_active = True
    tx = tt.get_or_begin_turn_transaction(sess, "문을 연다")
    tt.mark_waiting_for_roll(sess, tx.transaction_id)
    calls, finished = [], []

    async def gm_logic(session, msg, rolls, mch, **k):
        calls.append(list(rolls))
        return None if len(calls) == 1 else {"action": "PROCEED", "proceed_instruction": "x", "event_assessment": "ongoing"}

    async def fin(session, instr, mch, *, event_assessment=None, transaction_id=None):
        finished.append(transaction_id)
    monkeypatch.setattr(cog, "_call_gm_logic", gm_logic, raising=False)
    monkeypatch.setattr(cog, "_finish_proceed_and_continue", fin, raising=False)
    monkeypatch.setattr(core, "is_session_open", lambda s: True)
    await cog._continue_with_roll_results(sess, "문을 연다", ["[근력] 15 성공"], transaction_id=tx.transaction_id)
    assert tx.pending_roll == {"player_message": "문을 연다", "roll_results": ["[근력] 15 성공"]}
    view = next(m.view for m in game_channel.sent if isinstance(m.view, __import__("cogs.gm", fromlist=["x"]).RollContinueView))
    entered = []

    async def loop(*a, **k):
        entered.append(1)
    monkeypatch.setattr(cog, "_run_gm_logic_loop", loop, raising=False)
    await cog._process_actions(sess, "다시 굴리고 싶다", master_channel)    # 재선언·재굴림 차단
    assert entered == [] and any("이어서 진행" in t for t in _texts(game_channel))
    inter = FakeInteraction(user=FakeUser(int(next(iter(sess.players)))), channel=game_channel)
    await view.children[0].callback(inter)
    import asyncio
    await asyncio.sleep(0.05)
    assert calls == [["[근력] 15 성공"], ["[근력] 15 성공"]]                # 같은 결과 재사용(재굴림 없음)
    assert finished == [tx.transaction_id] and tx.pending_roll is None


async def test_roll_buttons_after_restart_get_redeclare_guidance():
    store = types.SimpleNamespace(_views={}, _dynamic_items={})
    bot = types.SimpleNamespace(_connection=types.SimpleNamespace(_view_store=store))
    for cid, expect in ((UX.ROLL_PREFIX + "dice", UX.ROLL_STALE_TEXT), ("x", UX.STALE_TEXT)):
        i = FakeInteraction(user=FakeUser(7), message=types.SimpleNamespace(id=1))
        i.type = discord.InteractionType.component
        i.data = {"custom_id": cid, "component_type": 2}
        assert await UX.answer_stale(bot, i)
        assert i.response.messages[0]["content"] == expect


# ── F7: 손상 계정은 '잉크 부족'이 아니다 ─────────────────────────────

async def test_corrupt_account_blocks_turn_with_account_message(drig):
    r = drig
    r.make()
    await turn(r, "숨을 고른다")                                          # 계정 시드
    import os
    path = core.accounts._path(next(iter(r.sess.players)))
    with open(path, "w", encoding="utf-8") as f:
        f.write("{깨진")
    assert os.path.exists(path)
    r.bot.get_channel(r.sess.game_ch_id).sent.clear()
    await turn(r, "숨을 고른다")
    texts = _texts(r.bot.get_channel(r.sess.game_ch_id))
    assert any("계정 정보를 읽지 못해" in t for t in texts) and not any("잉크가 부족" in t for t in texts)


# ── F2 · F8 · F9: 전수보고 ───────────────────────────────────────

async def test_secret_button_rejects_other_sessions_master_channel(monkeypatch):
    from tests.policy.test_v730_master_report import _record
    monkeypatch.setattr(core.turn_history, "load_record", lambda sid, tx: _record())
    a = types.SimpleNamespace(session_id="sa", master_ch_id=5, definition_revision=None)
    b = types.SimpleNamespace(session_id="sb", master_ch_id=7, definition_revision=None)
    bot = types.SimpleNamespace(active_sessions={5: a, 7: b})
    i = FakeInteraction(user=FakeUser(1), channel=types.SimpleNamespace(id=7))
    i.client = bot
    await MR.SecretButton("sa", "abc").callback(i)                      # b의 마스터 채널에서 a의 비밀
    assert "마스터 채널" in i.response.messages[0]["content"]


def test_transit_placement_uses_canonical_keys_and_scene_classification():
    from core import spatial as SP
    tr = SP.transit(movement_id="m", edge_id="e", from_place="항구", to_place="시장",
                    edge_started_at_minute=0, edge_expected_end_minute=10)
    assert MR._place(tr) == "이동 중 항구→시장"
    pre = {"spatial_state": {"scene_place": "항구", "placements": {"npc.a": {"kind": "AT_PLACE", "place_id": "창고"}}}}
    post = {"spatial_state": {"scene_place": "항구", "placements": {"npc.a": tr}}}
    secs, _ = MR.delta_sections(types.SimpleNamespace(definition_revision=None), {"pre": pre, "post": post})
    line = dict((n, "\n".join(ls)) for n, ls in secs)["🧍 인물"]
    assert "[원격]" not in line and "이동 중 항구→시장" in line                 # PC 곁을 지나는 인물


def test_report_branches_fail_independently(monkeypatch):
    def boom(*a, **k):
        raise ValueError("델타 버그")
    monkeypatch.setattr(MR, "delta_sections", boom)
    embeds, n = MR.build(types.SimpleNamespace(turn_count=1), transaction_id="t",
                         cost_embed=discord.Embed(title="x").add_field(name="비용", value="1원"), record={})
    titles = [e.title for e in embeds]
    assert any(t.startswith("🎲 턴 비용") for t in titles) and n == 0
    assert any("델타 버그" in f.value for e in embeds for f in e.fields)


# ── ① · ⑤ ───────────────────────────────────────────────────

def test_joins_blocked_while_dynamic_init_failed():
    s = types.SimpleNamespace(world_lifecycle=None, world_initialization={"status": "FAILED"}, definition_revision=None)
    assert core.world_lifecycle.joins_blocked(s)
    s.world_initialization = {}
    assert not core.world_lifecycle.joins_blocked(s)


@pytest.mark.parametrize("indent,trail", [(2, ""), (4, "\n")])
def test_image_keyword_save_edits_only_that_key_and_keeps_format(tmp_path, indent, trail):
    from cogs.media import write_media_keyword
    p = tmp_path / "s.json"
    orig = {"id": "s", "media_keywords": {"a": "a.png"}, "big": {"x": [1, 2]}}
    p.write_text(json.dumps(orig, ensure_ascii=False, indent=indent) + trail, encoding="utf-8")
    write_media_keyword(str(p), "새키", "새키.png")
    raw = p.read_text(encoding="utf-8")
    assert raw == json.dumps(dict(orig, media_keywords={"a": "a.png", "새키": "새키.png"}),
                             ensure_ascii=False, indent=indent) + trail
