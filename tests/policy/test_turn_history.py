"""WP-E — 정본 턴 이력 선택 · 되감기 · 같은 턴 재생성.

WP_E_MASTER_HANDOFF §19. 실제 GMCog 자동 경로(_finish_proceed_and_continue → CommitCoordinator)
로 커밋한 뒤, 이력 인덱스·레코드·data.json·Settlement·InkTransaction을 디스크에서 읽어
판정한다. 되감기/재생성은 게임 이력만 바꾸고 재무 이력은 불가역이어야 한다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import core
from core import turn_history as TH
from core import turn_transaction as tt
from tests.conftest import PLAYER_UID
from tests.policy.test_ready_barrier import (  # noqa: F401 — 픽스처 재사용
    _json, rig,
)
from tests.policy.test_authoritative_commit import (
    _balance, _disk, _fund, _ink_rows, _phases, _store,
)
from tests.policy.test_commit_recovery import _restart, _sid

pytestmark = pytest.mark.policy

JUDGMENT = {"action": "PROCEED", "reason": "이동 선언", "event_assessment": "ongoing"}


@pytest.fixture
def inject():
    """고장 주입 전용 MonkeyPatch. undo()가 rig/cwd 격리를 되돌리지 않게 분리한다."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _loc(name):
    return _json({"location": {"name": name, "faction_context": "미확인"},
                  "situation": {"tag": "이동", "tension": 0}})


async def _commit(r, loc, decl="숲길로 간다", *, judged=True):
    """자동 경로로 한 턴을 커밋한다. judged면 판단층위 기록(note_judgment)을 남긴다."""
    r.prov.routes["extraction"] = [_loc(loc)]
    tx = tt.get_or_begin_turn_transaction(r.sess, decl)
    if judged:
        TH.note_judgment(r.sess, tx.transaction_id, judgment=dict(JUDGMENT),
                         player_message=decl, roll_results=[])
    await r.gm._finish_proceed_and_continue(
        r.sess, f"{decl} 묘사", r.master, event_assessment="ongoing",
        transaction_id=tx.transaction_id)
    assert tx.status == tt.TurnStatus.COMMITTED, (tx.status, tx.failure_stage)
    return tx


def _hv(s):
    return TH.view(s.session_id)


def _fake_logic(r, inject, *, result=None, calls=None):
    """지시층위 대역 — 호출 인자를 기록하고 고정 지시를 돌려준다."""
    calls = calls if calls is not None else []

    async def _logic(session, player_message, roll_results, master_ch, sim_result=None,
                     action="PROCEED", *, transaction_id=None):
        calls.append({"player_message": player_message, "roll_results": roll_results,
                      "action": action, "tid": transaction_id})
        return result if result is not None else {
            "action": "PROCEED", "proceed_instruction": "다시 숲길을 묘사하라",
            "event_assessment": "ongoing"}
    inject.setattr(r.gm, "_call_gm_logic", _logic)

    async def _no_judgment(*a, **k):
        raise AssertionError("재생성은 판단층위를 다시 호출하면 안 됩니다")
    inject.setattr(r.gm, "_call_judgment", _no_judgment, raising=False)
    return calls


# ══════════════════════════════════════════════════════════════
# 커밋 → 이력 레코드
# ══════════════════════════════════════════════════════════════

async def test_e_commit_records_selected_attempt_with_pre_post_and_judgment(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx = await _commit(r, "숲길")
    hv = _hv(s)
    assert hv.head == 1 and hv.head_entry()["transaction_id"] == tx.transaction_id
    e = hv.head_entry()
    assert e["record"] and e["rerenderable"] and e["attempt"] == 1
    rec = TH.load_record(s.session_id, tx.transaction_id)
    assert rec["post"]["world_timeline"]["current_location"] == "숲길"
    assert rec["pre"]["gm_turns_done"] == 0 and rec["post"]["gm_turns_done"] == 1
    assert rec["judgment"]["player_message"] == "숲길로 간다"
    assert rec["settlement_id"] == _sid(tx)
    assert rec["post"]["commit_marker"]["transaction_id"] == tx.transaction_id
    assert rec["canonical_message_ids"], "정본 묘사 메시지 ID가 레코드에 남아야 합니다"
    # 운영/재무 필드는 이력 레코드에 없다
    assert not set(rec["post"]) & set(TH.OPERATIONAL_FIELDS)
    # 저널 순서는 D와 같다(E-2 단계 = REWIND_RECORDED)
    assert _phases(s, tx) == ["PREPARED", "SESSION_PERSISTED", "REWIND_RECORDED",
                              "BILLING_APPLIED", "COMMITTED"]


async def test_e_unjudged_turn_is_selected_but_not_rerenderable(rig):
    r = rig
    await _commit(r, "숲길", judged=False)
    e = _hv(r.sess).head_entry()
    assert e["record"] and not e["rerenderable"]
    entry, rec, reason = TH.rerender_target(r.sess)
    assert entry is None and "판단" in reason


# ══════════════════════════════════════════════════════════════
# 되감기
# ══════════════════════════════════════════════════════════════

async def test_e_rewind_one_turn_restores_exact_post_state_finance_untouched(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx1 = await _commit(r, "숲길")
    rec1 = TH.load_record(s.session_id, tx1.transaction_id)
    tx2 = await _commit(r, "동굴", "동굴로 간다")
    assert s.world_timeline["current_location"] == "동굴"
    bal, rows = _balance(PLAYER_UID), _ink_rows(PLAYER_UID)
    cost, usd, ink = s.total_cost, s.total_usd, s.total_ink_spent
    events = r.ledger.list_cost_events()
    st2 = _store(s).get_settlement_strict(_sid(tx2))

    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    assert res["removed_turns"] == [2]
    # 정확히 턴 1 커밋 직후 정본(추출 효과 포함)
    assert TH.capture_reversible(s) == rec1["post"]
    assert s.world_timeline["current_location"] == "숲길"
    assert s.gm_turns_done == 1 and s.commit_marker["transaction_id"] == tx1.transaction_id
    d = _disk(s)
    assert d["commit_marker"]["transaction_id"] == tx1.transaction_id
    assert d["world_timeline"]["current_location"] == "숲길"
    # 재무 이력 불변(환불/반전 없음)
    assert _balance(PLAYER_UID) == bal and _ink_rows(PLAYER_UID) == rows
    assert (s.total_cost, s.total_usd, s.total_ink_spent) == (cost, usd, ink)
    assert d["total_cost"] == cost and d["total_ink_spent"] == ink
    assert r.ledger.list_cost_events() == events
    assert _store(s).get_settlement_strict(_sid(tx2)) == st2
    # 선택 head
    hv = _hv(s)
    assert hv.head == 1 and hv.head_entry()["transaction_id"] == tx1.transaction_id
    assert TH.read_op(s.session_id) is None
    # 제거된 턴의 봇 출력만 정리
    rec2_ids = TH.load_record(s.session_id, tx2.transaction_id)["canonical_message_ids"]
    for m in r.gch.sent:
        if m.id in rec2_ids:
            assert m.deleted
        elif m.id in rec1["canonical_message_ids"]:
            assert not m.deleted


async def test_e_rewind_multiple_turns_and_raw_logs_follow_selected_branch(rig):
    r = rig
    s = r.sess
    tx1 = await _commit(r, "숲길")
    rec1 = TH.load_record(s.session_id, tx1.transaction_id)
    await _commit(r, "동굴", "동굴로 간다")
    await _commit(r, "폭포", "폭포로 간다")
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"] and res["removed_turns"] == [2, 3]
    assert len(s.raw_logs) == len(rec1["post"]["raw_logs"])
    assert [core.io._serialize_log_entry(c) for c in s.raw_logs] == rec1["post"]["raw_logs"]
    assert s.uncompressed_logs == rec1["post"]["uncompressed_logs"]
    # 되감기 이후 새 턴은 정상적으로 2턴이 된다
    tx4 = await _commit(r, "해안", "해안으로 간다")
    hv = _hv(s)
    assert hv.head == 2 and hv.head_entry()["transaction_id"] == tx4.transaction_id
    assert s.gm_turns_done == 2


async def test_e_rewind_survives_restart(rig):
    r = rig
    s = r.sess
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    assert (await r.gm.history_rewind(s, 1))["ok"]
    bot2, s2 = await _restart(r)
    assert s2.commit_marker["transaction_id"] == tx1.transaction_id
    assert s2.world_timeline["current_location"] == "숲길"
    assert s2.commit_recovery is None
    assert _hv(s2).head == 1


async def test_e_rewind_rejects_invalid_and_busy_states(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    assert not (await TH.rewind(r.bot, s, 2))["ok"]        # head 자신
    assert not (await TH.rewind(r.bot, s, 5))["ok"]
    s.commit_recovery = {"status": "PENDING", "stage": "X"}
    res = await TH.rewind(r.bot, s, 1)
    assert not res["ok"] and "복구" in res["reason"]
    s.commit_recovery = None
    s.is_processing = True
    assert not (await TH.rewind(r.bot, s, 1))["ok"]
    s.is_processing = False
    s.extraction_pending = True
    assert not (await TH.rewind(r.bot, s, 1))["ok"]
    s.extraction_pending = False
    TH.write_op(s.session_id, {"op": "REWIND", "op_id": "other"})
    assert not (await TH.rewind(r.bot, s, 1))["ok"]
    TH.clear_op(s.session_id)
    assert _hv(s).head == 2 and s.gm_turns_done == 2


async def test_e_duplicate_and_concurrent_rewind(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    await _commit(r, "폭포", "폭포로 간다")
    a, b = await asyncio.gather(r.gm.history_rewind(s, 2), r.gm.history_rewind(s, 2))
    assert [a["ok"], b["ok"]].count(True) == 1, (a, b)
    assert _hv(s).head == 2
    rewinds = [e for e in TH.load_events(s.session_id) if e["type"] == "REWIND"]
    assert len(rewinds) == 1


async def test_e_rewind_crash_before_strict_save_keeps_original_head(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    tx2 = await _commit(r, "동굴", "동굴로 간다")

    async def _boom(session):
        raise OSError("디스크 실패(테스트)")
    inject.setattr(core.io, "write_session_strict_locked", _boom)
    res = await TH.rewind(r.bot, s, 1)
    assert not res["ok"]
    assert s.world_timeline["current_location"] == "동굴"
    assert s.commit_marker["transaction_id"] == tx2.transaction_id
    assert _disk(s)["commit_marker"]["transaction_id"] == tx2.transaction_id
    assert _hv(s).head == 2 and TH.read_op(s.session_id) is None


async def test_e_rewind_crash_after_persist_before_index_is_reconciled(rig, inject):
    r = rig
    s = r.sess
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    orig = TH._append_event

    def _bad(sid, ev):
        if ev.get("type") == "REWIND":
            raise TH.HistoryError("인덱스 append 실패(크래시 모사)")
        return orig(sid, ev)
    inject.setattr(TH, "_append_event", _bad)
    res = await TH.rewind(r.bot, s, 1)
    assert res["ok"] and res.get("pending")
    assert s.commit_recovery and s.commit_recovery["stage"] == "HISTORY_REWIND"
    assert TH.read_op(s.session_id)["op"] == "REWIND"
    assert _disk(s)["commit_marker"]["transaction_id"] == tx1.transaction_id
    inject.undo()
    bot2, s2 = await _restart(r)
    assert TH.read_op(s2.session_id) is None
    hv = _hv(s2)
    assert hv.head == 1 and len(hv.rewind_ops) == 1
    assert s2.commit_recovery is None
    assert s2.world_timeline["current_location"] == "숲길"


async def test_e_restart_with_rewind_intent_but_unchanged_state_discards_intent(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    tx2 = await _commit(r, "동굴", "동굴로 간다")
    # 의도 기록 직후(저장 이전) 크래시 모사
    rec1_marker = TH.load_record(
        s.session_id, _hv(s).selected[1]["transaction_id"])["post"]["commit_marker"]
    TH.write_op(s.session_id, {"op": "REWIND", "op_id": "x1", "target_gm_turn": 1,
                               "target_tx": "t", "target_marker": rec1_marker,
                               "from_marker": dict(s.commit_marker), "removed": []})
    bot2, s2 = await _restart(r)
    assert TH.read_op(s2.session_id) is None and s2.commit_recovery is None
    assert _hv(s2).head == 2 and s2.commit_marker["transaction_id"] == tx2.transaction_id


async def test_e_degraded_gap_blocks_rewind_across_it(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    await _commit(r, "폭포", "폭포로 간다")
    s.rewind_degraded_turns = [2]
    assert TH.available_range(s) == (2, 3)
    res = await TH.rewind(r.bot, s, 1)
    assert not res["ok"] and "범위" in res["reason"]
    assert (await TH.rewind(r.bot, s, 2))["ok"]


async def test_e_compression_only_rewind_log_rows_do_not_create_turns(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    # 압축 경로가 남기는 턴 아닌 델타 행(레거시 rewind_log) — 선택 이력과 무관해야 한다
    core.rewind.record_delta(s, 7, [], compression={"occurred": True, "before": ""})
    core.rewind.record_delta(s, 1, [], compression={"occurred": True, "before": ""})
    assert _hv(s).head == 1
    assert TH.available_range(s) == (1, 1)


async def test_e_corrupt_record_rejects_rewind(rig):
    r = rig
    s = r.sess
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    p = TH.record_path(s.session_id, tx1.transaction_id)
    rec = json.load(open(p, encoding="utf-8"))
    rec["post"]["turn_count"] = 999
    json.dump(rec, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    res = await TH.rewind(r.bot, s, 1)
    assert not res["ok"] and "손상" in res["reason"]
    assert s.gm_turns_done == 2


# ══════════════════════════════════════════════════════════════
# 같은 턴 재생성
# ══════════════════════════════════════════════════════════════

async def test_e_rerender_same_logical_turn_attempt_plus_one(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _commit(r, "숲길")
    tx_old = await _commit(r, "동굴", "동굴로 간다")
    rec_old = TH.load_record(s.session_id, tx_old.transaction_id)
    st_old = _store(s).get_settlement_strict(_sid(tx_old))
    rows_before = _ink_rows(PLAYER_UID)
    bal_before = _balance(PLAYER_UID)
    calls = _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("동굴 깊은 곳")]
    old_ids = set(rec_old["canonical_message_ids"])

    started, reason = await r.gm.rerender_latest(s, addendum="더 어둡게")
    assert started, reason
    # 지시층위부터 — 보존된 선언/판단 입력
    assert len(calls) == 1 and calls[0]["player_message"] == "동굴로 간다"
    assert calls[0]["roll_results"] == [] and calls[0]["action"] == "PROCEED"
    hv = _hv(s)
    head = hv.head_entry()
    assert hv.head == 2 and head["logical_turn"] == tx_old.logical_turn
    assert head["attempt"] == tx_old.attempt + 1
    assert head["supersedes"] == tx_old.transaction_id
    new_tid = head["transaction_id"]
    rec_new = TH.load_record(s.session_id, new_tid)
    assert rec_new["player_declaration"] == rec_old["player_declaration"]
    assert rec_new["judgment"] == rec_old["judgment"]
    _strip = lambda st: {k: v for k, v in st.items() if k != "gm_side_note"}
    assert _strip(rec_new["pre"]) == _strip(rec_old["pre"]), "같은 턴 이전 상태에서 다시 적용해야 합니다"
    assert "[재생성 지시] 더 어둡게" in rec_new["pre"]["gm_side_note"]
    assert s.world_timeline["current_location"] == "동굴 깊은 곳"
    assert s.gm_turns_done == 2 and s.commit_marker["transaction_id"] == new_tid
    # 재무: 이전 청구 유지 + 교체 청구 추가(환불 없음)
    assert _store(s).get_settlement_strict(_sid(tx_old)) == st_old
    rows = _ink_rows(PLAYER_UID)
    assert rows[:len(rows_before)] == rows_before and len(rows) == len(rows_before) + 1
    st_new = _store(s).get_settlement_strict(core.settlement.settlement_id_for(new_tid, 2))
    assert st_new.outcome == "COMMITTED"
    assert _balance(PLAYER_UID) == bal_before - st_new.charge_ink_per_user
    # 이전 출력은 교체 커밋 이후 정리, 다른 턴 출력·새 출력은 유지
    for m in r.gch.sent:
        if m.id in old_ids:
            assert m.deleted
    assert all(not m.deleted for m in r.gch.sent
               if m.id in set(rec_new["canonical_message_ids"]))
    assert TH.read_op(s.session_id) is None


async def test_e_rerender_failure_keeps_old_selection_zero_charge(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    rec_old = TH.load_record(s.session_id, tx_old.transaction_id)
    rows_before, bal_before = _ink_rows(PLAYER_UID), _balance(PLAYER_UID)
    _fake_logic(r, inject)
    r.ev["stream_fail_at"] = r.ev["streamed"] + 1       # 교체 묘사 전달 실패

    started, _ = await r.gm.rerender_latest(s)
    hv = _hv(s)
    assert hv.head_entry()["transaction_id"] == tx_old.transaction_id
    assert TH.read_op(s.session_id) is None
    assert TH.capture_reversible(s) == rec_old["post"]
    assert _disk(s)["commit_marker"]["transaction_id"] == tx_old.transaction_id
    assert _ink_rows(PLAYER_UID) == rows_before and _balance(PLAYER_UID) == bal_before
    for m in r.gch.sent:
        if m.id in set(rec_old["canonical_message_ids"]):
            assert not m.deleted, "이전 시도 출력은 교체 커밋 전 삭제되면 안 됩니다"


async def test_e_rerender_instruction_failure_is_failed_system_zero_charge(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    rows_before = _ink_rows(PLAYER_UID)

    async def _none(*a, **k):
        return None
    inject.setattr(r.gm, "_call_gm_logic", _none)
    started, reason = await r.gm.rerender_latest(s)
    assert not started
    assert _hv(s).head_entry()["transaction_id"] == tx_old.transaction_id
    assert s.commit_marker["transaction_id"] == tx_old.transaction_id
    assert _ink_rows(PLAYER_UID) == rows_before
    assert TH.read_op(s.session_id) is None
    assert tt.get_active_transaction(s) is None


async def test_e_rerender_rejected_for_non_head_or_busy(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    _fake_logic(r, inject)
    s.commit_recovery = {"status": "PENDING", "stage": "X"}
    started, reason = await r.gm.rerender_latest(s)
    assert not started and "복구" in reason
    s.commit_recovery = None
    # 선택 head와 표식이 다르면 거부
    s.commit_marker = {"transaction_id": "someone-else", "attempt": 1}
    started, reason = await r.gm.rerender_latest(s)
    assert not started and "표식" in reason


async def test_e_concurrent_rerender_creates_single_replacement(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    _fake_logic(r, inject)
    res = await asyncio.gather(r.gm.rerender_latest(s), r.gm.rerender_latest(s))
    hv = _hv(s)
    attempts = [e for e in hv.all_attempts.values()
                if e["logical_turn"] == tx_old.logical_turn]
    # 두 번째 요청은 첫 교체 이후 head(attempt 2)를 다시 재생성할 수 있을 뿐,
    # 같은 이전 시도를 두 번 교체하지 않는다.
    supers = [e["supersedes"] for e in attempts if e.get("supersedes")]
    assert len(supers) == len(set(supers))
    assert len({e["attempt"] for e in attempts}) == len(attempts)
    assert hv.head == 1 and res[0][0]


async def test_e_rerender_stale_old_attempt_callbacks_rejected(rig, inject):
    r = rig
    s = r.sess
    tx_old = await _commit(r, "숲길")
    _fake_logic(r, inject)
    assert (await r.gm.rerender_latest(s))[0]
    assert not tt.is_current_transaction(s, tx_old.transaction_id)
    # 이전 시도 식별자로 판단 기록을 남기려 해도 무시된다
    TH.note_judgment(s, tx_old.transaction_id, judgment={"x": 1}, player_message="stale")
    assert tt.get_active_transaction(s) is None


async def test_e_rerender_after_restart_without_runtime_tx(rig, inject):
    r = rig
    s = r.sess
    tx_old = await _commit(r, "숲길")
    bot2, s2 = await _restart(r)
    entry, rec, reason = TH.rerender_target(s2)
    assert entry is not None, reason
    assert entry["transaction_id"] == tx_old.transaction_id
    assert rec["judgment"]["player_message"] == "숲길로 간다"
    # 재시작 후 attempt 카운터가 비어도 이력 max(attempt)+1이 된다
    tx = await TH.begin_rerender(bot2, s2, entry, rec)
    assert tx.logical_turn == tx_old.logical_turn and tx.attempt == tx_old.attempt + 1
    assert TH.read_op(s2.session_id)["new_tx"] == tx.transaction_id
    assert await TH.abort_rerender(bot2, s2)
    assert s2.commit_marker["transaction_id"] == tx_old.transaction_id


async def test_e_crash_during_rerender_restart_restores_old_post(rig, inject):
    r = rig
    s = r.sess
    tx_old = await _commit(r, "숲길")
    rec_old = TH.load_record(s.session_id, tx_old.transaction_id)
    entry, rec, _ = TH.rerender_target(s)
    await TH.begin_rerender(r.bot, s, entry, rec)
    # 턴 이전 상태가 data.json에 있는 채로 크래시
    assert _disk(s)["gm_turns_done"] == 0
    bot2, s2 = await _restart(r)
    assert TH.read_op(s2.session_id) is None
    assert s2.commit_recovery is None
    assert TH.capture_reversible(s2)["commit_marker"] == rec_old["post"]["commit_marker"]
    assert s2.gm_turns_done == 1 and s2.world_timeline["current_location"] == "숲길"
    assert _hv(s2).head_entry()["transaction_id"] == tx_old.transaction_id


async def test_e_crash_after_replacement_commit_before_history_select(rig, inject):
    """교체 시도가 COMMITTED 직전(E-2 기록 전) 크래시 → degraded 선택으로 정합, 차단 없음."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    _fake_logic(r, inject)
    orig = TH.record_commit
    state = {"n": 0}

    def _bad(session, plan, payload):
        if plan.attempt == 2 and state["n"] == 0:
            state["n"] += 1
            raise TH.HistoryError("이력 기록 실패(모사, 1회)")
        return orig(session, plan, payload)
    inject.setattr(TH, "record_commit", _bad)
    old_ids = set(TH.attempt_message_ids(s.session_id, _hv(s).head_entry()))
    await r.gm.rerender_latest(s)
    inject.undo()
    # 런타임에서 이미 교체가 정본(청구된 이야기를 되돌리지 않음), 선택은 degraded
    hv = _hv(s)
    assert hv.head_entry()["attempt"] == 2 and not hv.head_entry()["record"]
    assert hv.head_entry()["supersedes"] == tx_old.transaction_id
    assert s.commit_marker["transaction_id"] == hv.head_entry()["transaction_id"]
    assert 1 in s.rewind_degraded_turns and s.commit_recovery is None
    assert TH.read_op(s.session_id) is None
    assert all(m.deleted for m in r.gch.sent if m.id in old_ids)
    assert len(_ink_rows(PLAYER_UID)) == 2
    bot2, s2 = await _restart(r)
    hv = _hv(s2)
    head = hv.head_entry()
    assert head["attempt"] == 2 and head["logical_turn"] == tx_old.logical_turn
    assert s2.commit_marker["transaction_id"] == head["transaction_id"]
    assert TH.read_op(s2.session_id) is None
    assert s2.commit_recovery is None


async def test_e_rerender_command_and_display_entrypoints_use_history_authority():
    from tests.conftest import source_of
    game = source_of("cogs/game.py")
    disp = source_of("core/display.py")
    assert "rerender_latest" in game and "RerenderConfirmView" in disp
    assert "rewind_to" not in source_of("cogs/gm.py")
    assert not hasattr(core, "rewind_to")


async def test_e_cleanup_missing_messages_is_idempotent(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    tx2 = await _commit(r, "동굴", "동굴로 간다")
    entry = _hv(s).selected[2]
    n1 = await r.gm._cleanup_attempt_output(s, entry)
    n2 = await r.gm._cleanup_attempt_output(s, entry)
    assert n1 >= 1 and n2 == 0
    ids = set(TH.attempt_message_ids(s.session_id, entry))
    assert all(m.deleted for m in r.gch.sent if m.id in ids)
    assert any(not m.deleted for m in r.gch.sent), "다른 시도 출력은 남아야 합니다"
    assert tx2.transaction_id == entry["transaction_id"]


async def test_e_persistent_history_failure_after_replacement_commit_blocks_not_reverts(rig, inject):
    """선택 기록이 계속 실패하면 차단한다 — 청구된 교체 이야기를 이전 시도로 되돌리지 않는다."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _commit(r, "숲길")
    _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("숲 속 오두막")]

    def _bad(session, plan, payload):
        raise TH.HistoryError("이력 기록 영구 실패(모사)")
    inject.setattr(TH, "record_commit", _bad)
    await r.gm.rerender_latest(s)
    assert s.commit_recovery and str(s.commit_recovery["stage"]).startswith("HISTORY")
    assert s.world_timeline["current_location"] == "숲 속 오두막"
    assert _disk(s)["world_timeline"]["current_location"] == "숲 속 오두막"
    assert len(_ink_rows(PLAYER_UID)) == 2
    # 이력 조작은 차단 상태에서 거부된다
    assert TH.rerender_target(s)[0] is None
    # 고장 해소 후 복구 재개 → 교체가 선택 head(degraded)로 확정, 차단 해제
    inject.undo()
    assert await r.gm._resume_commit_recovery(s, r.master)
    head = _hv(s).head_entry()
    assert head["attempt"] == 2 and s.commit_recovery is None
    assert TH.read_op(s.session_id) is None
