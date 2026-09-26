"""WP-D — 영속 이후 실패·재시작 복구 실행기·RETRY_PENDING 재시작 처분.

WP_D_MASTER_HANDOFF §8/§11/§17/§21/§27.3. 재시작은 실제 직렬화된 파일(data.json·저널·
Settlement·계정·InkTransaction)만 남기고 새 봇/새 세션 객체로 restore_sessions_from_disk를
돌려 재현한다 — 구 런타임 tx/준비 객체에 기대지 않는다.
"""

from __future__ import annotations

import json
import os

import pytest

import core
from core import commit_coordinator as CC
from core import commit_journal as CJ
from core import turn_transaction as tt
from tests.conftest import GAME_CH, PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_ready_barrier import (  # noqa: F401 — 픽스처 재사용
    _canon, _run_turn, rig,
)
from tests.policy.test_authoritative_commit import (
    _balance, _disk, _fund, _ink_rows, _journal, _phases, _store,
)

pytestmark = pytest.mark.policy


@pytest.fixture
def inject():
    """고장 주입 전용 MonkeyPatch. undo()가 rig/cwd 격리를 되돌리지 않게 분리한다."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _sid(tx):
    return core.settlement.settlement_id_for(tx.transaction_id, tx.attempt)


async def _restart(r):
    """프로세스 재시작 모사 — 디스크 사실만으로 새 봇·새 세션을 복원한다."""
    s = r.sess
    os.makedirs("scenarios", exist_ok=True)
    with open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8") as f:
        json.dump(s.scenario_data, f, ensure_ascii=False)
    bot2 = FakeBot(strict=False)
    bot2.cost_ledger = core.cost_ledger.CostLedger(r.ledger.path)
    await core.restore_sessions_from_disk(bot2)
    s2 = bot2.active_sessions[GAME_CH]
    assert s2 is not s and tt.get_active_transaction(s2) is None
    return bot2, s2


def _fail_phase_once(mp, phase):
    orig = CJ.CommitJournal.append_entry
    state = {"n": 0}

    def _bad(self, entry):
        if entry.phase == phase and state["n"] == 0:
            state["n"] += 1
            raise CJ.CommitJournalPersistenceError(f"{phase.value} 실패(크래시 모사)")
        return orig(self, entry)
    mp.setattr(CJ.CommitJournal, "append_entry", _bad)


def _assert_committed_once(s, tx, *, charge_expected=True):
    ph = _phases(s, tx)
    assert ph.count("COMMITTED") == 1 and ph.count("BILLING_APPLIED") == 1
    st = _store(s).get_settlement_strict(_sid(tx))
    assert st.outcome == "COMMITTED"
    assert len(_ink_rows(PLAYER_UID)) == (1 if charge_expected else 0)
    return st


# ══════════════════════════════════════════════════════════════
# 크래시 창 — data.json 교체 후 SESSION_PERSISTED 이전(§8)
# ══════════════════════════════════════════════════════════════

async def test_r_crash_after_replace_before_session_persisted_restart(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.SESSION_PERSISTED)
    start = _canon(r.sess)
    tx = await _run_turn(r)
    assert _phases(r.sess, tx) == ["PREPARED"]
    assert _disk(r.sess)["commit_marker"]["transaction_id"] == tx.transaction_id
    assert _balance(PLAYER_UID) == 100 and r.rec["start_round"] == 0   # 차단(no unlock)
    assert r.sess.commit_recovery["stage"] == "SESSION_PERSISTED_APPEND"
    inject.undo()
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None
    st = _assert_committed_once(s2, tx)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user
    assert s2.turn_count == start["turn_count"] + 1        # 이야기는 대상 상태 그대로(롤백 없음)
    # 재시작 한 번 더 — 결정적 no-op
    bot3, s3 = await _restart(r)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user and len(_ink_rows(PLAYER_UID)) == 1
    assert s3.commit_recovery is None


async def test_r_session_persisted_append_fails_in_process_gate_recovers(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.SESSION_PERSISTED)
    tx = await _run_turn(r)
    assert r.sess.commit_recovery and r.rec["start_round"] == 0
    assert tt.get_active_transaction(r.sess) is tx and tx.status == tt.TurnStatus.COMMITTING
    await r.gm._process_actions(r.sess, "다음 행동", r.master)
    st = _assert_committed_once(r.sess, tx)
    assert tx.status == tt.TurnStatus.COMMITTED and tt.get_active_transaction(r.sess) is None
    assert r.sess.commit_recovery is None and r.rec["start_round"] == 1
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user


# ══════════════════════════════════════════════════════════════
# 영속 이후 실패 — 롤백 금지 · 차단 · 결정적 재개(§9 이후 / §17 C·D)
# ══════════════════════════════════════════════════════════════

async def test_r_settlement_persist_fails_after_persist(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    orig = core.settlement.SettlementStore.record_strict
    fail = {"on": True}

    def _bad(self, settlement):
        if fail["on"] and settlement.outcome == "COMMITTED":
            raise core.settlement.SettlementPersistenceError("Settlement 쓰기 실패(테스트)")
        return orig(self, settlement)
    inject.setattr(core.settlement.SettlementStore, "record_strict", _bad)
    tx = await _run_turn(r)
    assert _phases(s, tx) == ["PREPARED", "SESSION_PERSISTED"]
    assert _disk(s)["turn_count"] == start["turn_count"] + 1      # 이야기 durable
    assert tx.status != tt.TurnStatus.FAILED_SYSTEM                  # FAILED_SYSTEM 금지
    assert _balance(PLAYER_UID) == 100 and r.rec["start_round"] == 0
    # 복구 전에는 차단 유지
    await r.gm._process_actions(s, "다음", r.master)
    assert r.rec["start_round"] == 0 and s.commit_recovery
    fail["on"] = False
    await r.gm._process_actions(s, "다음", r.master)
    st = _assert_committed_once(s, tx)
    assert set(st.included_cost_event_ids) == set(tx.preparation.frozen_cost_event_ids)
    assert r.rec["start_round"] == 1 and _balance(PLAYER_UID) == 100 - st.charge_ink_per_user
    assert s.turn_count == start["turn_count"] + 1                   # 이중 적용 없음


async def test_r_first_user_account_write_fails_then_restart(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)

    def _bad(account):
        raise core.accounts.AccountPersistenceError("계정 strict 쓰기 실패(테스트)")
    inject.setattr(core.accounts, "_write_account_strict", _bad)
    tx = await _run_turn(r)
    assert "BILLING_APPLIED" not in _phases(r.sess, tx)
    assert _balance(PLAYER_UID) == 100 and _ink_rows(PLAYER_UID) == []
    assert r.rec["start_round"] == 0 and r.sess.commit_recovery
    inject.undo()
    bot2, s2 = await _restart(r)
    st = _assert_committed_once(s2, tx)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user


async def test_r_user_a_charged_user_b_fails_replay(rig, inject):
    r = rig
    s = r.sess
    B = "555002"
    s.players[B] = {"name": "둘째", "profile": {"근력": 5}, "fields": {}}
    await _fund(PLAYER_UID, 100)
    await _fund(B, 100)
    orig = core.accounts._write_account_strict
    state = {"fail_b": True}

    def _bad(account):
        if account["user_id"] == B and state["fail_b"]:
            raise core.accounts.AccountPersistenceError("B 실패(테스트)")
        return orig(account)
    inject.setattr(core.accounts, "_write_account_strict", _bad)
    tx = await _run_turn(r)
    st = _store(s).get_settlement_strict(_sid(tx))
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user and _balance(B) == 100
    state["fail_b"] = False
    inject.undo()
    bot2, s2 = await _restart(r)
    _assert_committed_once(s2, tx)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user     # A 재차감 없음
    assert _balance(B) == 100 - st.charge_ink_per_user              # B 정확히 한 번
    assert len(_ink_rows(PLAYER_UID)) == 1 and len(_ink_rows(B)) == 1


async def test_r_account_written_ledger_append_fails(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    orig = core.ink_transactions.InkTransactionLedger.append_strict
    state = {"n": 0}

    def _bad(self, tx):
        if state["n"] == 0:
            state["n"] += 1
            raise core.ink_transactions.InkTransactionPersistenceError("원장 실패(테스트)")
        return orig(self, tx)
    inject.setattr(core.ink_transactions.InkTransactionLedger, "append_strict", _bad)
    tx = await _run_turn(r)
    st = _store(r.sess).get_settlement_strict(_sid(tx))
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user     # 계정은 되돌리지 않음
    assert _ink_rows(PLAYER_UID) == [] and r.sess.commit_recovery
    bot2, s2 = await _restart(r)
    _assert_committed_once(s2, tx)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user     # 두 번째 차감 없음


@pytest.mark.parametrize("phase", [CJ.CommitPhase.BILLING_APPLIED, CJ.CommitPhase.COMMITTED])
async def test_r_crash_after_billing_restart_no_recharge(rig, inject, phase):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, phase)
    tx = await _run_turn(r)
    st = _store(r.sess).get_settlement_strict(_sid(tx))
    charged = 100 - st.charge_ink_per_user
    assert _balance(PLAYER_UID) == charged and "COMMITTED" not in _phases(r.sess, tx)
    assert r.rec["start_round"] == 0
    inject.undo()
    bot2, s2 = await _restart(r)
    _assert_committed_once(s2, tx)
    assert _balance(PLAYER_UID) == charged and len(_ink_rows(PLAYER_UID)) == 1


async def test_r_committed_append_fails_in_process_finalizes_without_recharge(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.COMMITTED)
    tx = await _run_turn(r)
    bal = _balance(PLAYER_UID)
    await r.gm._process_actions(r.sess, "다음", r.master)
    _assert_committed_once(r.sess, tx)
    assert _balance(PLAYER_UID) == bal and r.rec["start_round"] == 1


async def test_r_billing_applied_without_account_marker_is_recovery_required(rig, inject):
    """BILLING_APPLIED인데 계정 마커가 없다 → 추정 금지, 재차감 금지, RECOVERY_REQUIRED."""
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.COMMITTED)
    tx = await _run_turn(r)
    inject.undo()
    acc = core.accounts.load_account_strict(PLAYER_UID)
    acc["applied_ink_transactions"] = {}                 # 마커 손상(외부 조작 모사)
    core.accounts._write_account_strict(acc)
    bal = _balance(PLAYER_UID)
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery["status"] == "RECOVERY_REQUIRED"
    assert _balance(PLAYER_UID) == bal
    assert "RECOVERY_REQUIRED" in _phases(s2, tx) and "COMMITTED" not in _phases(s2, tx)


# ══════════════════════════════════════════════════════════════
# 파생 실패 · 되감기 공백 — 커밋 유지
# ══════════════════════════════════════════════════════════════

async def test_r_rewind_append_failure_degrades_without_rollback(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    inject.setattr(core.rewind, "record_delta", lambda *a, **k: False)
    tx = await _run_turn(r)
    st = _assert_committed_once(r.sess, tx)
    assert "REWIND_RECORDED" not in _phases(r.sess, tx)
    assert r.sess.rewind_degraded_turns == [r.sess.gm_turns_done]
    assert _disk(r.sess)["rewind_degraded_turns"] == [r.sess.gm_turns_done]
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user and r.rec["start_round"] == 1


@pytest.mark.parametrize("what", ["discord", "stats"])
async def test_r_derived_failures_after_committed_do_not_uncommit(rig, inject, what):
    r = rig
    await _fund(PLAYER_UID, 100)
    if what == "discord":
        orig = r.master.send

        async def _bad(content=None, **kw):
            if kw.get("embed") is not None or (content and "추출층위" in content):
                raise RuntimeError("Discord 전송 실패(테스트)")
            return await orig(content, **kw)
        inject.setattr(r.master, "send", _bad)
    else:
        async def _bad(*a, **k):
            raise RuntimeError("통계 쓰기 실패(테스트)")
        inject.setattr(core.stats, "bump", _bad)
    tx = await _run_turn(r)
    _assert_committed_once(r.sess, tx)
    assert tx.status == tt.TurnStatus.COMMITTED and r.rec["start_round"] == 1


# ══════════════════════════════════════════════════════════════
# 재시작 — 각 durable phase의 결정적 처분
# ══════════════════════════════════════════════════════════════

async def test_r_restart_after_prepared_with_baseline_disk_discards(rig, inject):
    """PREPARED만 있고 data.json이 기준선 → 폐기·청구 0(FAILED_SYSTEM Settlement)."""
    r = rig
    await _fund(PLAYER_UID, 100)
    start = _canon(r.sess)

    async def _crash(session):
        raise OSError("저장 전 크래시(테스트)")
    inject.setattr(core.io, "write_session_strict_locked", _crash)
    inject.setattr(CC, "settle_failed_attempt", lambda *a, **k: (None, "크래시"))
    tx = await _run_turn(r)
    assert _phases(r.sess, tx) == ["PREPARED"]
    assert r.sess.commit_recovery["stage"] == "FAILED_SETTLEMENT_PENDING"
    assert r.rec["start_round"] == 0          # 미해결 시도가 남으면 다음 턴 불가
    inject.undo()
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None
    st = _store(s2).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and st.charge_ink_per_user == 0
    assert set(st.included_cost_event_ids) == set(tx.preparation.frozen_cost_event_ids)
    assert _balance(PLAYER_UID) == 100 and s2.turn_count == start["turn_count"]
    # 재시작 반복 — 종결 사실로 인식(추가 사실 없음)
    bot3, s3 = await _restart(r)
    assert s3.commit_recovery is None and _phases(s3, tx) == ["PREPARED"]


async def test_r_restart_after_complete_commit_is_noop(rig):
    r = rig
    await _fund(PLAYER_UID, 100)
    tx = await _run_turn(r)
    bal, n = _balance(PLAYER_UID), len(_journal(r.sess).list_entries())
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None
    assert _balance(PLAYER_UID) == bal and len(_journal(s2).list_entries()) == n
    assert s2.commit_marker["transaction_id"] == tx.transaction_id


async def test_r_restart_without_journal_is_clean(rig):
    r = rig
    core.io._atomic_write_session(r.sess, core.io._serialize_session(r.sess))
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None


async def test_r_corrupted_journal_blocks_gameplay(rig):
    r = rig
    tx = await _run_turn(r)
    with open(CJ.default_journal_path(r.sess.session_id), "a", encoding="utf-8") as f:
        f.write("{손상된 라인\n")
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery["status"] == "RECOVERY_REQUIRED"
    # 게이트: 새 자동 턴 차단(새 트랜잭션도 만들지 않음)
    import cogs.gm as gm_mod
    gm2 = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm2.bot = bot2
    gm2._session_locks = {}
    s2.gm_active = True
    await gm2._process_actions(s2, "새 선언", None)
    assert tt.get_active_transaction(s2) is None and s2.commit_recovery


async def test_r_conflicting_settlement_is_recovery_required(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    orig = core.settlement.SettlementStore.record_strict
    fail = {"on": True}

    def _bad(self, settlement):
        if fail["on"] and settlement.outcome == "COMMITTED":
            raise core.settlement.SettlementPersistenceError("쓰기 실패(테스트)")
        return orig(self, settlement)
    inject.setattr(core.settlement.SettlementStore, "record_strict", _bad)
    tx = await _run_turn(r)
    fail["on"] = False
    import dataclasses
    plan = CC.CommitPlan.from_journal_metadata(
        _journal(r.sess).history_for_transaction(tx.transaction_id)[0].metadata)
    good = CC.build_committed_settlement(r.bot, plan)
    _store(r.sess).record_strict(dataclasses.replace(good, charge_ink_per_user=999))
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery["status"] == "RECOVERY_REQUIRED"
    assert _balance(PLAYER_UID) == 100 and _ink_rows(PLAYER_UID) == []
    assert "RECOVERY_REQUIRED" in _phases(s2, tx)


# ══════════════════════════════════════════════════════════════
# RETRY_PENDING 재시작(§11)
# ══════════════════════════════════════════════════════════════

async def test_r_retry_pending_restart_abandons_with_exact_claimed_ids(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]
    tx = await _run_turn(r)
    prep = tx.preparation
    assert prep.phase == core.turn_preparation.PREP_RETRY_PENDING
    ctx = _disk(s)["extraction_retry_ctx"]
    claimed = [eid for op in prep.cost_operations for eid in op.event_ids]
    assert ctx["claimed_cost_event_ids"] == claimed and claimed          # durable exact 입력
    assert _store(s).find_settlement(_sid(tx)) is None                   # 진입 시 최종 정산 없음
    assert not os.path.exists(CJ.default_journal_path(s.session_id))     # 저널 오염 없음
    bot2, s2 = await _restart(r)
    st = _store(s2).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and st.charge_ink_per_user == 0
    assert set(st.included_cost_event_ids) == set(claimed)
    assert s2.extraction_pending is False and _disk(s2)["extraction_pending"] is False
    assert _balance(PLAYER_UID) == 100 and s2.turn_count == s.turn_count


async def test_r_retry_pending_in_process_retry_still_same_attempt(rig):
    """보존 — 프로세스 내 재시도는 같은 tx/attempt로 성공하고 커밋된다(WP-C 규정)."""
    r = rig
    await _fund(PLAYER_UID, 100)
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]
    tx = await _run_turn(r)
    from tests.policy.test_ready_barrier import _json
    r.prov.routes["extraction"] = [_json({"situation": {}})]
    out = await r.gm._retry_prepared_extraction(r.sess)
    assert out == "ready"
    _assert_committed_once(r.sess, tx)
    st = _store(r.sess).get_settlement_strict(_sid(tx))
    assert st.attempt == tx.attempt and r.sess.extraction_pending is False
