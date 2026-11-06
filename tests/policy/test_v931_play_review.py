# -*- coding: utf-8 -*-
"""v9.3.1 — 운영 세션 검토 결함(사용자 결정): ① 진행 중 유지 시간 만료 → 묘사 전 중단(청구 0, 무상 창 없음)
② 키 없는 물건 열기·읽기·쓰기는 그 연산만 뺀다 ③ 추출 상태 블록은 PC·동행·장면 인물만."""
from __future__ import annotations

import types

import pytest

import core
from core import turn_transaction as tt
from indaim.rules.items import items as IT
from tests.policy.test_h05bc_turn_wiring import drig, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


async def test_expiry_during_turn_aborts_before_narration_without_reissue(drig, monkeypatch):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment("시장")
    orig = core.player_world.plan_player_turn
    reissued = []

    def expire_then_plan(*a, **k):
        r.sess.cache_name = None                       # 판단 뒤 만료 감지(프레즌스 finalize와 같은 상태)
        return orig(*a, **k)
    monkeypatch.setattr(core.player_world, "plan_player_turn", expire_then_plan)

    async def no_reissue(*a, **k):
        reissued.append(1)
    monkeypatch.setattr(core.cache_lifecycle, "reissue", no_reissue)
    tx = await turn(r, "시장으로 간다")
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_code == tt.FailureCode.SESSION_NOT_OPEN.value
    assert "narration" not in r.prov.calls and not reissued                # 묘사·재발급(무상 창) 없음
    game = [m.content or "" for m in r.bot.get_channel(r.sess.game_ch_id).sent]
    assert any("세션 열기" in t and "시장으로 간다" in t for t in game)       # 재오픈 + 선언 복사 안내 하나로
    assert any("청구 0" in (m.content or "") for m in r.master.sent)


def test_unkeyed_open_read_use_are_dropped_alone():
    m = IT.ItemManifest(owners=(("O1", "PC", "PC"),))
    text = "배낭을 열자 공구 세트와 통조림이 보였다."
    raw = [{"op": "OPEN", "owner": "O1", "item": "NEW", "name": "방수 배낭"},
           {"op": "READ", "owner": "O1", "item": "NONE"},
           {"op": "USE", "owner": "O1", "item": "NEW", "name": "약통"},
           {"op": "ACQUIRE", "owner": "O1", "item": "NEW", "name": "공구 세트", "quantity": 1},
           {"op": "ACQUIRE", "owner": "O1", "item": "NEW", "name": "통조림", "quantity": 3}]
    ops = IT.normalize_raw_ops(m, raw, pc_name="PC", narration_text=text)
    assert [(o["op"], o["source"].get("new")) for o in ops] == [("ACQUIRE", "공구 세트"), ("ACQUIRE", "통조림")]
    with pytest.raises(IT.ItemContractError):                               # 상태를 바꾸는 연산은 그대로 거부(재추출)
        IT.normalize_raw_ops(m, [{"op": "DROP", "owner": "O1", "item": "NEW"}], pc_name="PC", narration_text=text)


def test_status_block_lists_only_targets():
    s = types.SimpleNamespace(scenario_data={"status_effects": {"불안": {"apply_condition": "a", "remove_condition": "b"}}},
                              statuses={"PC": ["불안"], "섬 반대편 인물": ["불안"]}, world_clock={}, status_clock={},
                              irregular_npcs={})
    full = core.extraction.build_extraction_limits(s)
    assert "- 섬 반대편 인물: 불안" in full
    narrow = core.extraction.build_extraction_limits(s, status_targets={"PC"})
    assert "- PC: 불안" in narrow and "섬 반대편 인물: 불안" not in narrow
