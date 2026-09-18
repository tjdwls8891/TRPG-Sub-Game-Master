"""WP-SETTLEMENT-01 — CommitJournal 합성 증명(§38) + S8 하드스톱/호출자 스캔.

라이브 배선 없이(테스트 전용) 다음을 증명한다:
  - 저장된 Settlement 의 settlement_id 를 저널 스키마 변경 없이 JournalEntry 에 담고,
  - 재시작 시뮬 후 settlement_id 로 Settlement 을 재로드하고,
  - RESUME_BILLING 식 재실행이 같은 재무 executor 를 두 번 불러도 계정 효과는 한 번임을,
  - 프로덕션 CommitJournal 청구 호출자가 도입되지 않았음을.
그리고 게이트 하드스톱(핸드오프 §40, §44 S8): cogs 에 Settlement/InkTransaction/
CommitJournal-청구 호출자 0, 레거시 _finish_proceed_and_continue 결제 유지.
"""

from __future__ import annotations

import os

import pytest

from core import accounts
from core import commit_journal as CJ
from core import cost_ledger as cl
from core import ink_transactions as IT
from core import settlement as S
from tests.conftest import REPO_ROOT, source_of

pytestmark = pytest.mark.policy


# ── 헬퍼 ─────────────────────────────────────────────────────

def _mk_settlement(*, per_user_ink=10, users=("u1",)):
    users = tuple(sorted(users))
    return S.TurnSettlement(
        settlement_id="turn-settlement:t:attempt:1", session_id="s",
        transaction_id="t", logical_turn=1, attempt=1,
        outcome=S.SettlementOutcome.COMMITTED.value,
        included_cost_event_ids=("e1",), billing_user_ids=users,
        provider_cost_usd=0.0, provider_cost_krw=float(per_user_ink * 7),
        player_billable_cost_krw=float(per_user_ink * 7),
        non_player_cost_krw=0.0, system_unbilled_cost_krw=0.0,
        estimated_cost_krw=0.0, charge_ink_per_user=per_user_ink,
        aggregate_nominal_charge_ink=per_user_ink * len(users),
        policy_version=S.SETTLEMENT_POLICY_VERSION, created_at=0.0)


def _seed_account(uid, balance):
    acc = accounts._blank_account(uid)
    acc["registered"] = True
    acc["ink_balance"] = int(balance)
    accounts._write_account(acc)


@pytest.fixture(autouse=True)
def _clean_registry():
    S._STORE_REGISTRY.clear()
    IT._LEDGER_REGISTRY.clear()
    CJ._JOURNAL_REGISTRY.clear()
    yield
    S._STORE_REGISTRY.clear()
    IT._LEDGER_REGISTRY.clear()
    CJ._JOURNAL_REGISTRY.clear()


# ══════════════════════════════════════════════════════════════
#  §38 — CommitJournal 합성 (76~79)
# ══════════════════════════════════════════════════════════════

def test_76_settlement_id_fits_journal_entry_without_schema_change(tmp_path):
    s = _mk_settlement()
    store = S.SettlementStore(str(tmp_path / "turn_settlements.jsonl"))
    store.record_strict(s)
    # 저널 스키마 변경 없이 settlement_id 를 담는다(JournalEntry.settlement_id 기존 필드).
    entry = CJ.new_entry(transaction_id="t", session_id="s", logical_turn=1,
                         attempt=1, phase=CJ.CommitPhase.BILLING_APPLIED,
                         settlement_id=s.settlement_id)
    assert entry.settlement_id == s.settlement_id
    journal = CJ.CommitJournal(str(tmp_path / "turn_commit_journal.jsonl"))
    assert journal.append_entry(entry) is True


def test_77_reload_settlement_by_journal_id_after_restart(tmp_path):
    s = _mk_settlement()
    store_path = str(tmp_path / "turn_settlements.jsonl")
    S.SettlementStore(store_path).record_strict(s)
    journal_path = str(tmp_path / "turn_commit_journal.jsonl")
    CJ.CommitJournal(journal_path).append_entry(
        CJ.new_entry(transaction_id="t", session_id="s", logical_turn=1, attempt=1,
                     phase=CJ.CommitPhase.SESSION_PERSISTED, settlement_id=s.settlement_id))

    # 재시작 시뮬: 모든 레지스트리/객체를 버리고 디스크에서만 복원.
    S._STORE_REGISTRY.clear()
    CJ._JOURNAL_REGISTRY.clear()
    entries = CJ.CommitJournal(journal_path).list_entries(transaction_id="t", attempt=1)
    sid = entries[-1].settlement_id
    reloaded = S.SettlementStore(store_path).get_settlement_strict(sid)
    assert reloaded.settlement_id == s.settlement_id
    assert reloaded.charge_ink_per_user == s.charge_ink_per_user


async def test_78_resume_billing_replay_one_account_effect(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    store_path = str(tmp_path / "turn_settlements.jsonl")
    S.SettlementStore(store_path).record_strict(s)
    _seed_account("u1", 100)
    led_path = str(tmp_path / "u1.jsonl")

    # 1차 청구(정상 커밋 경로 상당).
    r1 = await IT.execute_settlement_charge(
        s, "u1", ledger=IT.InkTransactionLedger(led_path, user_id="u1"))
    assert r1.status == IT.EXEC_APPLIED
    assert accounts.get_balance("u1") == 90

    # 재시작 시뮬 후 RESUME_BILLING 처럼 같은 executor 를 다시 호출.
    S._STORE_REGISTRY.clear()
    IT._LEDGER_REGISTRY.clear()
    reloaded = S.SettlementStore(store_path).get_settlement_strict(s.settlement_id)
    r2 = await IT.execute_settlement_charge(
        reloaded, "u1", ledger=IT.InkTransactionLedger(led_path, user_id="u1"))
    assert r2.status == IT.EXEC_ALREADY
    assert accounts.get_balance("u1") == 90          # 계정 효과는 정확히 한 번

    # 참고: RESUME_BILLING 분류값이 존재한다(라이브 배선은 하지 않는다).
    assert CJ.RecoveryDisposition.RESUME_BILLING.value == "RESUME_BILLING"


def test_79_no_production_commit_journal_billing_caller():
    # cogs 어디에도 CommitJournal 청구/append 라이브 호출자가 없다.
    n = 0
    for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, "cogs")):
        if "__pycache__" in dirpath:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            text = source_of(os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT))
            n += text.count("append_entry(") + text.count("CommitJournal(")
    assert n == 0


# ══════════════════════════════════════════════════════════════
#  S8 — 하드스톱 / 프로덕션 호출자 스캔 (핸드오프 §40, §44)
# ══════════════════════════════════════════════════════════════

def _walk_cogs():
    for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, "cogs")):
        if "__pycache__" in dirpath:
            continue
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT)


def test_s8_no_settlement_or_ink_executor_callers_in_cogs():
    # 새 Settlement 빌더/스토어, InkTransaction executor/원장 라이브 호출자 = 0.
    syms = ("build_turn_settlement(", "SettlementStore(", "record_strict(",
            "execute_settlement_charge(", "execute_settlement_charges(",
            "InkTransactionLedger(", "apply_ink_charge_strict(")
    offenders = []
    for rel in _walk_cogs():
        text = source_of(rel)
        for sym in syms:
            if sym in text:
                offenders.append((rel, sym))
    assert offenders == [], f"cogs 에 foundation 라이브 호출자 발견: {offenders}"


def test_s8_legacy_finish_proceed_billing_remains():
    # 레거시 결제 경로(_finish_proceed_and_continue)가 그대로 살아 있다.
    gm = source_of("cogs/gm.py")
    assert "async def _finish_proceed_and_continue" in gm
    assert "ink = core.cost_to_ink(turn_cost)" in gm
    assert "core.accounts.deduct_ink(_uid, ink, allow_overdraft=True)" in gm


def test_s8_foundation_not_wired_into_turn_pipeline():
    # gm.py 가 Settlement/InkTransaction foundation 을 턴 파이프라인에 배선하지 않았다.
    gm = source_of("cogs/gm.py")
    for sym in ("build_turn_settlement", "execute_settlement_charge",
                "SettlementStore", "InkTransactionLedger"):
        assert sym not in gm
