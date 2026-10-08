# -*- coding: utf-8 -*-
"""H06-F — typed 관리자 세계 편집(ADMIN_EDIT).

  · 유휴 typed 편집 → 커밋 1회 · world_version 1회 전진 · 저널 COMMITTED.
  · 완전 no-op → 새 version 없음. 진행 중 턴·stale 기준·typed 아닌 연산 → 거부(부분 변이 없음).
  · 플레이어 턴 수·Settlement·Ink 불변. 정의 바인딩 불변.
  · 되감기: 편집 이후 턴을 되감으면 편집은 남고, 편집 이전으로 되감으면 편집이 사라진다(시간 순서).
  · 턴 이후 편집이 있으면 그 턴의 재서술은 편집을 지우지 않도록 거부한다.
"""
from __future__ import annotations

import copy

import pytest

import core
from core import admin_world_edit as AE
from core import canonical_mutation as CM
from core import turn_history as TH
from core import turn_transaction as tt
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    drig, judgment, rig, turn, world_of)

pytestmark = pytest.mark.policy

OPEN_DOOR = [{"op_type": "ACCESS_STATE_SET", "payload": {"control_id": "door_2", "state": "OPEN"}}]


def _ink(uid=PLAYER_UID):
    from core import ink_transactions as IT
    return IT.InkTransactionLedger(IT.default_ink_tx_ledger_path(uid), user_id=uid).list_all()


async def test_idle_typed_edit_commits_once_and_changes_no_player_state(drig):
    r = drig
    r.make()
    s = r.sess
    v0 = copy.deepcopy(s.world_version)
    turns, gm_turns, ink = s.turn_count, s.gm_turns_done, len(_ink())
    rev = s.definition_revision
    res = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")
    assert res.outcome == CM.OUTCOME_COMMITTED, res.reason
    assert s.access_control_state["door_2"]["state"] == "OPEN"
    assert s.world_version["cause_kind"] == WS.CAUSE_ADMIN_EDIT
    assert s.world_version["parent_version_id"] == WS.version_id_of(v0)
    assert (s.turn_count, s.gm_turns_done, len(_ink())) == (turns, gm_turns, ink)
    assert s.definition_revision == rev
    phases = [e["phase"] for e in CM.read_journal(s.session_id) if e.get("cause_kind") == WS.CAUSE_ADMIN_EDIT
              or e.get("mutation_id") == res.mutation_id]
    assert phases == ["PREPARED", "PERSISTED", "COMMITTED"]


async def test_noop_edit_produces_no_new_version(drig):
    r = drig
    r.make()
    s = r.sess
    await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")
    v1 = copy.deepcopy(s.world_version)
    res = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e2")
    assert res.outcome == CM.OUTCOME_NOOP and s.world_version == v1


async def test_edit_during_active_turn_rejected_without_partial_mutation(drig):
    r = drig
    r.make()
    s = r.sess
    before = world_of(s)
    tx = tt.get_or_begin_turn_transaction(s, "진행 중 선언")
    assert not tt.is_terminal(tx.status)
    res = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")
    assert res.outcome == CM.OUTCOME_REJECTED and world_of(s) == before


async def test_stale_world_or_revision_and_untyped_ops_rejected(drig):
    r = drig
    r.make()
    s = r.sess
    before = world_of(s)
    stale_v = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1", expected_world_version_id="wv_old")
    stale_r = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e2",
                                        expected_definition_revision="sha256:" + "0" * 64)
    untyped = await AE.apply_admin_edit(r.bot, s, [{"op_type": "SET_PATH", "payload": {"path": "x"}}],
                                        edit_id="e3")
    assert {stale_v.outcome, stale_r.outcome, untyped.outcome} == {CM.OUTCOME_REJECTED}
    assert world_of(s) == before


async def test_legacy_or_closed_session_cannot_be_edited(drig):
    r = drig
    r.make()
    s = r.sess
    s.world_lifecycle = None
    assert (await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")).outcome == CM.OUTCOME_REJECTED
    s.world_lifecycle = core.world_lifecycle.LIFECYCLE_CLOSED
    assert (await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")).outcome == CM.OUTCOME_REJECTED


async def test_rewind_is_chronological_over_admin_edit(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd"))
    s = r.sess
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")                                    # 턴 A
    post_a = {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}
    res = await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")
    assert res.outcome == CM.OUTCOME_COMMITTED
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    await turn(r, "10분 기다린다")                                    # 턴 B — 편집을 본 상태에서 진행
    assert (await TH.rewind(r.bot, s, 1))["ok"]                       # 대상 = A 직후 스냅샷(편집 이전)
    assert {f: getattr(s, f) for f in WS.GAMEPLAY_WORLD_FIELDS} == post_a       # 이후 편집은 대상 상태에 없다
    assert "door_2" not in (s.access_control_state or {})
    phases = [e["phase"] for e in CM.read_journal(s.session_id) if e.get("mutation_id") == res.mutation_id]
    assert phases[-1] == "COMMITTED"                                  # 운영 감사 사실은 지워지지 않는다


async def test_rerender_after_admin_edit_is_rejected_not_silently_erasing(drig):
    r = drig
    r.make(edges=("e_hm#fwd",))
    s = r.sess
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")
    await AE.apply_admin_edit(r.bot, s, OPEN_DOOR, edit_id="e1")
    before = world_of(s)
    started, reason = await r.gm.rerender_latest(s)
    assert not started and "세계 편집" in reason
    assert world_of(s) == before
