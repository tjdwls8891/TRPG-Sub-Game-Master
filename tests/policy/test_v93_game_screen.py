# -*- coding: utf-8 -*-
"""v9.3 — 이동 경로 연결도(F1) · 게임 채널 턴 화면(F2, 계획서 v1 D1~D4 가). 가짜 채널 — 네트워크 없음."""
from __future__ import annotations

import json
import pathlib

import pytest

import core
from tests.fakes.discord_fakes import FakeChannel, FakeMessage
from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator

pytestmark = pytest.mark.policy
SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "golden" / "scripts"
GS, TW = core.game_screen, core.turn_world


class HistChannel(FakeChannel):
    """history(after/before) · delete_messages를 가진 게임 채널 대역."""

    def __init__(self, *a, bulk_ok=True, **k):
        super().__init__(*a, **k)
        self.bulk_ok, self.bulk_calls = bulk_ok, []

    async def history(self, *, limit=100, after=None, before=None):
        for m in sorted(self.sent, key=lambda m: m.id):
            if m.deleted or (after and m.id <= after.id) or (before and m.id >= before.id):
                continue
            yield m

    async def delete_messages(self, msgs):
        if not self.bulk_ok:
            raise RuntimeError("Forbidden")
        self.bulk_calls.append([m.id for m in msgs])
        for m in msgs:
            m.deleted = True

    def player(self, text):
        m = FakeMessage(channel=self, content=text)
        self.sent.append(m)
        return m


async def _booted(drig, name="균열", turns=1):
    script = json.loads((SCRIPTS / f"{name}.json").read_text(encoding="utf-8"))
    sim = Simulator(drig)
    s = await sim.boot(script)
    for st in script["turns"][:turns]:
        await sim.step(st)
    return sim, s


# ── F1 이동 경로 ─────────────────────────────────────────────

async def test_route_map_is_exit_candidates_with_known_access_marks(drig, monkeypatch):
    _sim, s = await _booted(drig)
    ctx = core.player_world.session_world_context(s, intro=True)
    rm = TW.route_map(ctx.definition, s, ctx.lead_pc)
    assert [lb for lb, _p in rm["routes"]] == [c[2] for c in core.action_intent.exit_candidates(
        ctx.definition, s, ctx.lead_pc)]                              # 판단 출구 후보와 같은 단일 출처(숨은 길은 발견 뒤만)
    monkeypatch.setattr(core.action_intent, "exit_candidates", lambda *a: (
        ("e1", "p1", "북쪽 계단 → 창고", "PERCEIVED_PASSABLE"),
        ("e2", "p2", "철문 → 뒷마당", "PERCEIVED_ACTION_REQUIRED"),
        ("e3", "p3", "무너진 길 → 언덕", "PERCEIVED_BLOCKED")))
    rm = TW.route_map(ctx.definition, s, ctx.lead_pc)
    text = TW.route_map_text(rm)
    assert text.splitlines()[1:] == [" ├─ 북쪽 계단 → 창고", " ├─ 🔒 철문 → 뒷마당", " └─ ⛔ 무너진 길 → 언덕"]
    assert "[잠김] 철문" in TW.route_map_text(rm, svg=True) and "🔒" not in TW.route_map_text(rm, svg=True)


def test_route_map_text_transit_and_empty():
    assert TW.route_map_text({"transit": "A → B 쪽 이동 중"}) == "🚶 A → B 쪽 이동 중"
    assert TW.route_map_text({"here": "골목", "routes": []}).endswith("(아는 길 없음)")


# ── F2 턴 화면 ───────────────────────────────────────────────

async def test_screen_replaces_code_block_and_sits_above_turn_output(drig):
    _sim, s = await _booted(drig, turns=2)
    top = s.game_screen["message_id"]
    sent = drig.gch.sent
    assert not any("```css" in (m.content or "") for m in sent)                # 상태창 코드블럭은 게임 채널로 안 나간다
    idx = next(i for i, m in enumerate(sent) if m.id == top)
    assert sent[idx].files is not None and sent[idx].content is None          # 이미지 메시지
    assert any("PC가 길을" in (m.content or "") for m in sent[idx + 1:])         # 다음 턴 묘사는 그 아래


async def test_commit_sweeps_previous_turn_keeps_current_declaration(drig):
    _sim, s = await _booted(drig)
    ch = HistChannel(drig.gch.id)
    top = await ch.send(content=None, file=object())
    s.game_screen = {"message_id": top.id}
    old_decl, old_narr = ch.player("둔산 거리로 간다"), await ch.send("지난 묘사")
    ask = await ch.send("> 다음 행동은?")
    decl = ch.player("길드 사무실로 간다")
    narr = await ch.send("이번 묘사")
    s._turn_start_message_id = decl.id
    drig.bot.get_channel = lambda cid: ch
    await GS.on_turn_committed(drig.bot, s, None)
    assert old_decl.deleted and old_narr.deleted and ask.deleted                 # 지난 턴 선언·출력·되묻기
    assert not (top.deleted or decl.deleted or narr.deleted)                     # 상단 SVG·이번 턴만 남는다


async def test_sweep_falls_back_to_single_deletes_and_never_raises(drig):
    _sim, s = await _booted(drig)
    ch = HistChannel(drig.gch.id, bulk_ok=False)
    top = await ch.send(content=None)
    s.game_screen = {"message_id": top.id}
    a, b = await ch.send("a"), await ch.send("b")
    keep = ch.player("선언")
    n = await GS.sweep(drig.bot, s, keep_from=keep.id, channel=ch)
    assert n == 2 and a.deleted and b.deleted and not keep.deleted


async def test_render_failure_falls_back_to_text_on_same_message(drig, monkeypatch):
    _sim, s = await _booted(drig)
    def boom(_svg):
        raise RuntimeError("렌더러 없음")
    monkeypatch.setattr(core.display_board, "render_png", boom)
    s.game_screen = {}
    assert await GS.update(drig.bot, s, summary="고블린과 대치", channel=drig.gch)
    m = await drig.gch.fetch_message(s.game_screen["message_id"])
    assert m.content.startswith("```css") and "상황 요약 : 고블린과 대치" in m.content and "📍" in m.content


async def test_same_svg_is_not_reuploaded(drig):
    _sim, s = await _booted(drig)
    m = await drig.gch.fetch_message(s.game_screen["message_id"])
    n = len(m.edit_history)
    assert await GS.update(drig.bot, s, channel=drig.gch)
    assert len(m.edit_history) == n


async def test_after_rewind_clears_below_and_reposts_head_narration(drig):
    _sim, s = await _booted(drig, turns=2)
    ch = HistChannel(drig.gch.id)
    top = await ch.send(content=None)
    s.game_screen = {"message_id": top.id}
    stale = await ch.send("되감긴 턴 묘사")
    drig.bot.get_channel = lambda cid: ch
    await GS.after_rewind(drig.bot, s, 1)
    assert stale.deleted and not top.deleted
    reposted = [m.content for m in ch.sent if not m.deleted and m.id != top.id]
    assert reposted and reposted[0].startswith("⏪ 1턴으로 되돌렸습니다.") and "```" not in reposted[0]


async def test_submit_records_turn_boundary_message(drig):
    _sim, s = await _booted(drig)
    seen = {}

    async def spy(session, text, master_ch):
        seen["id"] = getattr(session, "_turn_start_message_id", None)
    drig.gm._process_actions = spy
    await drig.gm.submit(s, "주변을 본다", speaker="테스터", reply_channel=drig.gch, message_id=4242)
    assert seen["id"] == 4242
