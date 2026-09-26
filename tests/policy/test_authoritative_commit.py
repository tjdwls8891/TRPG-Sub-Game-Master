"""WP-D — 권위적 커밋(CommitCoordinator) · Settlement 청구 권위 · 중복/동시/stale.

WP_D_MASTER_HANDOFF §21/§22/§27. 실제 GMCog 자동 경로(_finish_proceed_and_continue)를
WP-C rig(실 CostLedger·라우팅 provider)로 돌리고, 저널·Settlement·계정·data.json을
디스크에서 직접 읽어 판정한다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os

import pytest

import core
from core import commit_coordinator as CC
from core import commit_journal as CJ
from core import turn_preparation as tp
from core import turn_transaction as tt
from tests.conftest import PLAYER_UID, source_of
from tests.policy.test_ready_barrier import (  # noqa: F401 — 픽스처 재사용
    _canon, _run_turn, rig,
)

pytestmark = pytest.mark.policy


# ══════════════════════════════════════════════════════════════
# 헬퍼
# ══════════════════════════════════════════════════════════════

def _journal(s):
    return CJ.CommitJournal(CJ.default_journal_path(s.session_id))


def _phases(s, tx):
    return [e.phase.value for e in _journal(s).history_for_transaction(
        tx.transaction_id, attempt=tx.attempt)]


def _store(s):
    return CC.settlement_store_for(s.session_id)


def _disk(s):
    with open(f"sessions/{s.session_id}/data.json", encoding="utf-8") as f:
        return json.load(f)


async def _fund(uid, amount):
    await core.accounts.register_account(uid)
    await core.accounts.add_ink(uid, amount)


def _balance(uid):
    return core.accounts.load_account_strict(uid)["ink_balance"]


def _ink_rows(uid):
    return core.ink_transactions.InkTransactionLedger(
        core.ink_transactions.default_ink_tx_ledger_path(uid), user_id=str(uid)).list_all()


def _story(data: dict) -> dict:
    """data.json의 커밋 소유(정본 이야기) 필드만. 운영 집계(total_cost 등)는 사전 READY
    tolerant 저장이 정당하게 바꾸므로 비교에서 제외한다."""
    import types
    out = {k: data.get(k) for k in CC.COMMIT_OWNED_FIELDS
           if not k.startswith("_") and k in data}
    # quest_state는 리더(get_state)가 빈 dict를 기본 형태로 지연 정규화한다(의미 변화 아님).
    out["quest_state"] = core.quest.clone_state(
        types.SimpleNamespace(quest_state=copy.deepcopy(data.get("quest_state") or {})))
    return out


def _save_baseline(r):
    """커밋 전 data.json 기준선을 만든다(디스크 정본 불변 판정용)."""
    core.io._atomic_write_session(r.sess, core.io._serialize_session(r.sess))
    return _story(_disk(r.sess))


# ══════════════════════════════════════════════════════════════
# §27.1 정상 경로
# ══════════════════════════════════════════════════════════════

async def test_d_happy_path_full_durable_order(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    order = []
    _orig_strict = core.io.write_session_strict_locked

    async def _strict(session):
        order.append("strict_save")
        return await _orig_strict(session)
    r_mp = pytest.MonkeyPatch()
    r_mp.setattr(core.io, "write_session_strict_locked", _strict)
    try:
        tx = await _run_turn(r)
    finally:
        r_mp.undo()

    prep = tx.preparation
    assert order == ["strict_save"], "strict 저장은 정확히 한 번"
    # 저널 durable 순서
    assert _phases(s, tx) == ["PREPARED", "SESSION_PERSISTED", "REWIND_RECORDED",
                              "BILLING_APPLIED", "COMMITTED"]
    # 런타임은 durable COMMITTED 이후에만
    assert tx.status == tt.TurnStatus.COMMITTED and tt.get_active_transaction(s) is None
    assert prep.phase == tp.PREP_CONTINUED
    # 정본 1회 전진
    assert s.turn_count == start["turn_count"] + 1 and s.gm_turns_done == 1
    assert len(s.raw_logs) == start["raw_logs_len"] + 2
    # exact 멤버십 → 영속 COMMITTED Settlement
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    assert st.outcome == "COMMITTED"
    assert set(st.included_cost_event_ids) == set(prep.frozen_cost_event_ids)
    assert len(prep.frozen_cost_event_ids) >= 2       # 묘사 + 추출(재시도·늦은 관측 포함 가능)
    ops = {e["operation"] for e in r.ledger.list_cost_events()
           if e["event_id"] in st.included_cost_event_ids}
    assert {"TURN_NARRATION", "TURN_EXTRACTION"} <= ops
    assert st.billing_user_ids == (PLAYER_UID,)
    # 계정 = InkTransaction 1건, 잔액 효과 일치
    rows = _ink_rows(PLAYER_UID)
    assert len(rows) == 1 and rows[0]["settlement_id"] == st.settlement_id
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user == rows[0]["balance_after"]
    # 미러·표식이 Settlement에서 파생되어 이야기와 함께 영속
    d = _disk(s)
    assert d["last_turn_ink"] == st.charge_ink_per_user == s.last_turn_ink
    assert d["total_ink_spent"] == st.charge_ink_per_user
    assert d["commit_marker"]["transaction_id"] == tx.transaction_id
    assert d["turn_count"] == s.turn_count
    # 파생: 비용 보고는 Settlement 값, 다음 라운드
    assert any("Settlement" in str([f.name for f in e.fields])
               for m in r.master.sent for e in m.embeds)
    assert r.rec["start_round"] == 1 and r.rec["legacy_deduct"] == []
    # 되감기 기록 1회(WP-E 연결)
    assert len(core.read_jsonl(s.session_id, core.rewind.FULL_LOGS)) == 1


async def test_d_linkage_reconstructible_from_committed_identity(rig):
    """§12 — committed tx 정체성에서 결정적으로 연결 기록을 재구성한다."""
    r = rig
    s = r.sess
    tx = await _run_turn(r)
    prepared = next(e for e in _journal(s).history_for_transaction(tx.transaction_id)
                    if e.phase == CJ.CommitPhase.PREPARED)
    plan = CC.CommitPlan.from_journal_metadata(prepared.metadata)
    assert plan.transaction_id == tx.transaction_id and plan.attempt == tx.attempt
    assert plan.logical_turn == tx.logical_turn
    assert plan.player_declaration == tx.player_declaration
    assert plan.settlement_id == prepared.settlement_id
    assert tuple(plan.canonical_message_ids) == tuple(tx.preparation.ready_proof.delivered_message_ids)
    assert plan.story_turn == s.turn_count and plan.gm_turn == s.gm_turns_done
    assert _store(s).get_settlement_strict(plan.settlement_id).transaction_id == tx.transaction_id
    committed = [e for e in _journal(s).history_for_transaction(tx.transaction_id)
                 if e.phase == CJ.CommitPhase.COMMITTED][0]
    assert committed.metadata["plan_fingerprint"] == plan.fingerprint
    assert s.commit_marker["plan_fingerprint"] == plan.fingerprint
    delta = core.read_jsonl(s.session_id, core.rewind.REWIND_LOG)
    assert delta and delta[-1]["turn"] == plan.gm_turn   # 되감기 참조 = gm_turn


# ══════════════════════════════════════════════════════════════
# §27.4 청구·보고 권위
# ══════════════════════════════════════════════════════════════

async def test_d_billing_ignores_total_cost_and_foreign_tx_event(rig, monkeypatch):
    """누적 total_cost 변조·같은 tx 귀속 비클레임 이벤트는 청구에 영향이 없다(F4)."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    orig = CC.CommitCoordinator.commit_ready_turn
    stray = {}

    async def _hook(self, session, prep, **k):
        session.total_cost = 987654.0          # 가변 합계 변조
        op = core.cost_ledger.begin_operation(   # 같은 tx 귀속, 멤버 아님(수동 활동 모사)
            r.bot, "MANUAL_TOOL", session=session, model=core.DEFAULT_MODEL,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        op.record(cost_usd=5.0, cost_krw=7000.0)
        stray["id"] = op.event_ids[0]
        return await orig(self, session, prep, **k)
    monkeypatch.setattr(CC.CommitCoordinator, "commit_ready_turn", _hook)
    tx = await _run_turn(r)
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    ev = [e for e in r.ledger.list_cost_events() if e["event_id"] == stray["id"]][0]
    assert ev["transaction_id"] == tx.transaction_id          # 귀속은 같지만
    assert stray["id"] not in st.included_cost_event_ids      # 멤버가 아니다
    billable = sum(e["cost_krw"] for e in r.ledger.list_cost_events()
                   if e["event_id"] in st.included_cost_event_ids)
    assert st.charge_ink_per_user == core.cost_to_ink(billable)
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user


async def test_d_multiplayer_full_nominal_charge_each(rig):
    """§15 — 모든 청구 유저가 같은 명목 청구(분할 없음), 1잉크 하한·보조금 구분."""
    r = rig
    s = r.sess
    s.players["555002"] = {"name": "둘째", "profile": {"근력": 5}, "fields": {}}
    await _fund(PLAYER_UID, 100)          # 555002는 잔액 0 → 하한 1잉크 + 운영자 부담
    tx = await _run_turn(r)
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    assert st.billing_user_ids == tuple(sorted((PLAYER_UID, "555002")))
    assert st.aggregate_nominal_charge_ink == 2 * st.charge_ink_per_user
    a, b = _ink_rows(PLAYER_UID)[0], _ink_rows("555002")[0]
    assert a["nominal_charge_ink"] == b["nominal_charge_ink"] == st.charge_ink_per_user
    assert b["balance_after"] == 1 and b["overdraft"] is True
    # 잔액 0 → 결과 1(하한). 실제 잔액 변화 +1, 운영자 부담 = 명목 + 1.
    assert b["applied_balance_delta"] == 1
    assert b["operator_subsidy_ink"] == st.charge_ink_per_user + 1
    # 기존 제품 안내(1잉크 하한) 보존 — InkTransaction 사실에서 파생, COMMITTED 이후 방출
    assert any("1잉크로 맞췄습니다" in (m.content or "") for m in r.master.sent)


async def test_d_display_and_session_close_do_not_reconvert(rig, monkeypatch):
    """AUD-022/028 — 표시·세션 종료 통계는 Settlement 파생값만 쓴다(재환산 없음)."""
    r = rig
    s = r.sess
    tx = await _run_turn(r)
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    s.last_turn_cost = 123456.0                   # KRW 미러를 흔들어도 표시 불변
    emb = core.build_embed(s) if hasattr(core, "build_embed") else None
    if emb is not None:
        cost_field = [f.value for f in emb.fields if f.name == "비용"][0]
        assert f"직전 턴 {st.charge_ink_per_user}잉크" in cost_field
    disp = source_of("core/display.py")
    assert "cost_to_ink(last" not in disp
    # 세션 종료: ink_spent를 total_cost로 재환산해 더하지 않는다
    calls = []

    async def _bump(uid, **d):
        calls.append(d)
    monkeypatch.setattr(core.stats, "bump", _bump)
    from core.ui import _cleanup_session_memory
    r.bot.active_sessions[s.game_ch_id] = s
    try:
        _cleanup_session_memory(r.bot, s.game_ch_id)
    except Exception:
        pass
    await asyncio.sleep(0)
    assert calls and all("ink_spent" not in c for c in calls)
    assert "cost_to_ink(getattr(session, \"total_cost\"" not in source_of("core/ui.py")


async def test_d_no_independent_cost_to_ink_on_commit_path():
    gm = source_of("cogs/gm.py")
    assert "cost_to_ink" not in gm.split("async def _commit_ready_turn")[1].split(
        "async def _cleanup_uncommitted_output")[0]
    cc = source_of("core/commit_coordinator.py")
    assert "cost_to_ink" not in cc
    assert "total_cost" not in cc.replace("total_cost 차분", "")


# ══════════════════════════════════════════════════════════════
# §21 영속 전 실패 — 복원·청구 0·FAILED_SYSTEM Settlement
# ══════════════════════════════════════════════════════════════

async def _assert_pre_persist_failure(r, tx, start, disk_before):
    s = r.sess
    assert _canon(s) == start, "영속 전 실패인데 메모리 정본이 남았습니다"
    assert _story(_disk(s)) == disk_before, "디스크 정본이 바뀌었습니다"
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tt.get_active_transaction(s) is None
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    assert st.outcome == "FAILED_SYSTEM" and st.charge_ink_per_user == 0
    assert set(st.included_cost_event_ids) == set(tx.preparation.frozen_cost_event_ids)
    assert _balance(PLAYER_UID) == 100 and _ink_rows(PLAYER_UID) == []
    assert r.ledger.list_cost_events(), "CostEvent는 운영 사실로 남아야 합니다"
    assert "COMMITTED" not in _phases(s, tx)
    assert r.rec["start_round"] == 1            # 재선언 UX(다음 라운드)
    assert getattr(s, "commit_recovery", None) is None


@pytest.mark.parametrize("where", ["settlement_validation", "prepared_append"])
async def test_d_failure_before_mutation(rig, monkeypatch, where):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    disk_before = _save_baseline(r)
    if where == "settlement_validation":
        orig = core.settlement.build_turn_settlement

        def _bad(**k):
            if k["outcome"] == core.settlement.SettlementOutcome.COMMITTED:
                raise core.settlement.SettlementPolicyError("검증 실패(테스트)")
            return orig(**k)
        monkeypatch.setattr(core.settlement, "build_turn_settlement", _bad)
    else:
        orig = CJ.CommitJournal.append_entry

        def _bad(self, entry):
            if entry.phase == CJ.CommitPhase.PREPARED:
                raise CJ.CommitJournalPersistenceError("PREPARED 실패(테스트)")
            return orig(self, entry)
        monkeypatch.setattr(CJ.CommitJournal, "append_entry", _bad)
    tx = await _run_turn(r)
    assert "PREPARED" not in _phases(s, tx)
    await _assert_pre_persist_failure(r, tx, start, disk_before)


@pytest.mark.parametrize("where", ["apply_throws", "write_fails", "replace_fails"])
async def test_d_failure_during_apply_or_strict_save(rig, monkeypatch, where):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    disk_before = _save_baseline(r)
    if where == "apply_throws":
        orig = r.gm._apply_commit_effects

        async def _bad(session, prep, derived):
            await orig(session, prep, derived)      # 도메인 변이까지 끝낸 뒤 실패
            raise RuntimeError("적용 실패(테스트)")
        monkeypatch.setattr(r.gm, "_apply_commit_effects", _bad)
    elif where == "write_fails":
        def _bad(session, data):
            raise OSError("디스크 가득(테스트)")
        monkeypatch.setattr(core.io, "_atomic_write_session", _bad)
    else:
        def _bad(session, data):
            tmp = f"sessions/{session.session_id}/data.json.x.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            raise OSError("원자적 교체 실패(테스트)")
        monkeypatch.setattr(core.io, "_atomic_write_session", _bad)
    tx = await _run_turn(r)
    assert _phases(s, tx) == ["PREPARED"]
    await _assert_pre_persist_failure(r, tx, start, disk_before)
    assert not [f for f in os.listdir(f"sessions/{s.session_id}") if f.endswith(".tmp")]
    # 전달된 묘사는 정본처럼 남지 않는다(정리됨) + 실패 안내
    assert all(m.deleted for m in r.gch.sent if m.id in tx.preparation.delivery.canonical_message_ids)


async def test_d_pre_ready_failure_persists_failed_system_settlement(rig):
    """§10 — 사전 READY 종료 실패(전달 실패)도 exact ID FAILED_SYSTEM Settlement(청구 0)."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    r.ev["stream_fail_at"] = 1
    tx = await _run_turn(r)
    st = _store(s).get_settlement_strict(core.settlement.settlement_id_for(
        tx.transaction_id, tx.attempt))
    assert st.outcome == "FAILED_SYSTEM" and st.charge_ink_per_user == 0
    assert set(st.included_cost_event_ids) == set(tx.preparation.frozen_cost_event_ids)
    assert st.provider_cost_krw > 0 and _balance(PLAYER_UID) == 100
    assert not os.path.exists(CJ.default_journal_path(s.session_id))


async def test_d_commit_owned_fields_cover_every_commit_write(rig, monkeypatch):
    """롤백 스냅샷 완전성 — 커밋이 바꾸는 직렬화 필드 ⊆ COMMIT_OWNED_FIELDS."""
    r = rig
    s = r.sess
    before = {}
    orig = CC.CommitCoordinator._apply_in_memory

    async def _spy(self, session, *a, **k):
        before.update(json.loads(json.dumps(core.io._serialize_session(session), default=str)))
        return await orig(self, session, *a, **k)
    monkeypatch.setattr(CC.CommitCoordinator, "_apply_in_memory", _spy)
    await _run_turn(r)
    after = _disk(s)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed and changed <= set(CC.COMMIT_OWNED_FIELDS), changed - set(CC.COMMIT_OWNED_FIELDS)


# ══════════════════════════════════════════════════════════════
# §22 중복 · 동시 · stale
# ══════════════════════════════════════════════════════════════

def _single_facts(r, tx):
    s = r.sess
    ph = _phases(s, tx)
    assert ph.count("COMMITTED") == 1 and ph.count("PREPARED") == 1
    assert len(_store(s).list_for_transaction(tx.transaction_id)) == 1
    assert len(_ink_rows(PLAYER_UID)) == 1


async def test_d_duplicate_commit_call_is_rejected(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx = await _run_turn(r)
    turn, bal = s.turn_count, _balance(PLAYER_UID)
    res = await CC.CommitCoordinator(r.bot).commit_ready_turn(
        s, tx.preparation, apply_effects=r.gm._apply_commit_effects)
    assert res.outcome == CC.CommitOutcome.REJECTED
    assert s.turn_count == turn and _balance(PLAYER_UID) == bal
    _single_facts(r, tx)


async def test_d_concurrent_duplicate_commit_calls(rig, monkeypatch):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    results = []
    orig = r.gm._commit_ready_turn

    async def _race(session, prep, **k):
        coord = CC.CommitCoordinator(r.bot)
        a, b = await asyncio.gather(
            coord.commit_ready_turn(session, prep, apply_effects=r.gm._apply_commit_effects,
                                    state_before=k.get("state_before")),
            coord.commit_ready_turn(session, prep, apply_effects=r.gm._apply_commit_effects,
                                    state_before=k.get("state_before")))
        results.extend([a, b])
        return a if a.outcome == CC.CommitOutcome.COMMITTED else b
    monkeypatch.setattr(r.gm, "_commit_ready_turn", _race)
    start = _canon(s)
    tx = await _run_turn(r)
    outs = sorted(x.outcome.value for x in results)
    assert outs == ["COMMITTED", "REJECTED"]
    assert s.turn_count == start["turn_count"] + 1 and s.gm_turns_done == 1
    _single_facts(r, tx)


async def test_d_stale_transaction_commit_rejected(rig, monkeypatch):
    r = rig
    s = r.sess
    seen = {}

    async def _stale(session, prep, **k):
        newer = tt.begin_attempt(session, logical_turn=prep.logical_turn,
                                 player_declaration="재선언")
        seen["newer"] = newer
        before = _canon(session)
        res = await CC.CommitCoordinator(r.bot).commit_ready_turn(
            session, prep, apply_effects=r.gm._apply_commit_effects)
        seen["res"], seen["unchanged"] = res, _canon(session) == before
        return res
    monkeypatch.setattr(r.gm, "_commit_ready_turn", _stale)
    tx = await _run_turn(r)
    assert seen["res"].outcome == CC.CommitOutcome.REJECTED and seen["unchanged"]
    assert tt.get_active_transaction(s) is seen["newer"]
    assert seen["newer"].status == tt.TurnStatus.CREATED
    assert not os.path.exists(CJ.default_journal_path(s.session_id)) or \
        _phases(s, tx) == []
    assert r.rec["start_round"] == 0


async def test_d_next_declaration_blocked_while_committing(rig):
    """COMMITTING 동안 새 선언은 게이트에서 차단된다(다음 턴 unlock 권위)."""
    r = rig
    s = r.sess
    tx = tt.get_or_begin_turn_transaction(s, "선언")
    prep = tp.ensure_preparation(s, tx.transaction_id)
    prep.phase = tp.PREP_COMMITTING
    s.gm_active = True
    await r.gm._process_actions(s, "새 선언", r.master)
    assert any("준비 중인" in (m.content or "") or "이전 턴" in (m.content or "")
               for m in r.master.sent)
    assert tt.get_active_transaction(s) is tx
