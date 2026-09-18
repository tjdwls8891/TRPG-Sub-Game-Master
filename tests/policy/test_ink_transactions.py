"""WP-SETTLEMENT-01 — InkTransaction 모델·원장·exactly-once executor 검증(§34~§37).

결정적 ink_tx_id, append-only per-user 원장(멱등/충돌/손상/경로-정체성), 그리고
계정 정확히-한-번 적용을 증명한다: 마커-원장 동시 내구성, 실패 주입(계정 쓰기/원장
append), 크래시-후 재적용 금지, 원장 복구, overdraft 1잉크 floor + 운영자 보조 노출,
멀티 유저 부분 배치 재실행 안전. conftest 의 _isolated_cwd 로 accounts/ 는 tmp
작업디렉터리에 격리된다.
"""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import json
import os

import pytest

from core import accounts
from core import ink_transactions as IT
from core import settlement as S
from tests.conftest import source_of

pytestmark = pytest.mark.policy


# ── 헬퍼 ─────────────────────────────────────────────────────

def _mk_settlement(*, sid="turn-settlement:t:attempt:1", per_user_ink=10,
                   users=("u1",), outcome=S.SettlementOutcome.COMMITTED):
    """executor 가 읽는 필드만 채운 최소 TurnSettlement.

    (executor 는 settlement_id / charge_ink_per_user / billing_user_ids 만 읽는다.)
    """
    users = tuple(sorted(str(u) for u in users))
    return S.TurnSettlement(
        settlement_id=sid, session_id="s", transaction_id="t",
        logical_turn=1, attempt=1, outcome=outcome.value,
        included_cost_event_ids=("e1",), billing_user_ids=users,
        provider_cost_usd=0.0, provider_cost_krw=float(per_user_ink * 7),
        player_billable_cost_krw=float(per_user_ink * 7),
        non_player_cost_krw=0.0, system_unbilled_cost_krw=0.0,
        estimated_cost_krw=0.0,
        charge_ink_per_user=int(per_user_ink),
        aggregate_nominal_charge_ink=int(per_user_ink) * len(users),
        policy_version=S.SETTLEMENT_POLICY_VERSION, created_at=0.0)


def _seed_account(uid, balance, spent=0):
    acc = accounts._blank_account(uid)
    acc["registered"] = True
    acc["ink_balance"] = int(balance)
    acc["total_spent_ink"] = int(spent)
    accounts._write_account(acc)


def _balance(uid):
    return int(accounts.load_account_strict(uid)["ink_balance"])


def _marker(uid, ink_tx_id):
    acc = accounts.load_account_strict(uid)
    return (acc.get("applied_ink_transactions") or {}).get(ink_tx_id)


def _ledger(uid):
    return IT.InkTransactionLedger(IT.default_ink_tx_ledger_path(uid), user_id=str(uid))


@pytest.fixture(autouse=True)
def _clean_registry():
    IT._LEDGER_REGISTRY.clear()
    yield
    IT._LEDGER_REGISTRY.clear()


# ══════════════════════════════════════════════════════════════
#  §34 — InkTransaction 정체성/원장 (42~51)
# ══════════════════════════════════════════════════════════════

def test_42_deterministic_ink_tx_id():
    assert IT.expected_ink_tx_id("turn-settlement:t:attempt:1", "u1") == \
        "ink-charge:turn-settlement:t:attempt:1:user:u1"


def test_43_different_user_different_id():
    a = IT.expected_ink_tx_id("sid", "u1")
    b = IT.expected_ink_tx_id("sid", "u2")
    assert a != b


def test_44_immutable_round_trip():
    tx = IT.InkTransaction(
        ink_tx_id="ink-charge:sid:user:u1", settlement_id="sid", user_id="u1",
        kind=IT.KIND_CHARGE, nominal_charge_ink=10, balance_before=100,
        balance_after=90, applied_balance_delta=-10, overdraft=False,
        operator_subsidy_ink=0, reason=IT.CHARGE_REASON, created_at=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        tx.balance_after = 5
    d = dataclasses.asdict(tx)
    assert d["ink_tx_id"] == "ink-charge:sid:user:u1" and d["nominal_charge_ink"] == 10


async def test_45_one_record_appends_and_reloads(tmp_path):
    s = _mk_settlement(per_user_ink=1)
    _seed_account("u1", 100)
    res = await IT.execute_settlement_charge(s, "u1")
    assert res.status == IT.EXEC_APPLIED
    IT._LEDGER_REGISTRY.clear()
    rows = _ledger("u1").list_all()
    assert len(rows) == 1 and rows[0]["ink_tx_id"] == res.ink_tx_id


async def test_46_append_uses_flush_fsync(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=1)
    _seed_account("u1", 100)
    calls = {"n": 0}
    real = os.fsync
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (calls.__setitem__("n", calls["n"] + 1), real(fd))[1])
    await IT.execute_settlement_charge(s, "u1")
    assert calls["n"] >= 1


def test_47_equivalent_replay_returns_canonical(tmp_path):
    led = _ledger("u1")
    tx = IT.InkTransaction(
        ink_tx_id="ink-charge:sid:user:u1", settlement_id="sid", user_id="u1",
        kind=IT.KIND_CHARGE, nominal_charge_ink=10, balance_before=100,
        balance_after=90, applied_balance_delta=-10, overdraft=False,
        operator_subsidy_ink=0, reason=IT.CHARGE_REASON, created_at=1.0)
    assert led.append_strict(tx).created is True
    # created_at 만 다른 동등 replay → 두 번째 줄 없음.
    tx2 = dataclasses.replace(tx, created_at=999.0)
    assert led.append_strict(tx2).created is False
    assert len(led.list_all()) == 1


def test_48_conflicting_replay_rejected(tmp_path):
    led = _ledger("u1")
    tx = IT.InkTransaction(
        ink_tx_id="ink-charge:sid:user:u1", settlement_id="sid", user_id="u1",
        kind=IT.KIND_CHARGE, nominal_charge_ink=10, balance_before=100,
        balance_after=90, applied_balance_delta=-10, overdraft=False,
        operator_subsidy_ink=0, reason=IT.CHARGE_REASON, created_at=1.0)
    led.append_strict(tx)
    conflicting = dataclasses.replace(tx, nominal_charge_ink=999)
    with pytest.raises(IT.InkTransactionConflictError):
        led.append_strict(conflicting)


def test_49_malformed_line_not_skipped(tmp_path):
    led = _ledger("u1")
    os.makedirs(os.path.dirname(led.path), exist_ok=True)
    with open(led.path, "w", encoding="utf-8") as f:
        f.write("{broken\n")
    with pytest.raises(IT.InkTransactionCorruptionError):
        led.list_all()


def test_50_duplicate_durable_id_rejected(tmp_path):
    led = _ledger("u1")
    os.makedirs(os.path.dirname(led.path), exist_ok=True)
    row = json.dumps({
        "ink_tx_id": "dup", "settlement_id": "sid", "user_id": "u1",
        "kind": "CHARGE", "nominal_charge_ink": 1, "balance_before": 10,
        "balance_after": 9, "applied_balance_delta": -1, "overdraft": False,
        "operator_subsidy_ink": 0, "reason": "r", "created_at": 0.0}) + "\n"
    with open(led.path, "w", encoding="utf-8") as f:
        f.write(row)
        f.write(row)
    with pytest.raises(IT.InkTransactionConflictError):
        led.list_all()


def test_51_path_user_mismatch_rejected(tmp_path):
    # 파일 stem 은 u1 인데 라인 user_id 가 u2 → 손상.
    led = IT.InkTransactionLedger(str(tmp_path / "u1.jsonl"))
    os.makedirs(os.path.dirname(led.path) or ".", exist_ok=True)
    row = json.dumps({
        "ink_tx_id": "x", "settlement_id": "sid", "user_id": "u2",
        "kind": "CHARGE", "nominal_charge_ink": 1, "balance_before": 10,
        "balance_after": 9, "applied_balance_delta": -1, "overdraft": False,
        "operator_subsidy_ink": 0, "reason": "r", "created_at": 0.0}) + "\n"
    with open(led.path, "w", encoding="utf-8") as f:
        f.write(row)
    with pytest.raises(IT.InkTransactionCorruptionError):
        led.list_all()


# ══════════════════════════════════════════════════════════════
#  §35 — 계정 정확히-한-번 실행 (52~64)
# ══════════════════════════════════════════════════════════════

async def test_52_ordinary_charge_mutates_once(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    res = await IT.execute_settlement_charge(s, "u1")
    assert res.status == IT.EXEC_APPLIED
    assert _balance("u1") == 90


async def test_53_marker_in_same_durable_state_as_balance(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    res = await IT.execute_settlement_charge(s, "u1")
    m = _marker("u1", res.ink_tx_id)
    assert m is not None
    assert m["balance_before"] == 100 and m["balance_after"] == 90


async def test_54_replay_does_not_deduct_again(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    await IT.execute_settlement_charge(s, "u1")
    r2 = await IT.execute_settlement_charge(s, "u1")
    assert r2.status == IT.EXEC_ALREADY
    assert _balance("u1") == 90


async def test_55_replay_after_reload_does_not_deduct(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    await IT.execute_settlement_charge(s, "u1")
    IT._LEDGER_REGISTRY.clear()          # 새 원장/객체 로드 시뮬
    r2 = await IT.execute_settlement_charge(s, "u1")
    assert r2.status == IT.EXEC_ALREADY
    assert _balance("u1") == 90


async def test_56_conflicting_same_id_rejected(tmp_path):
    _seed_account("u1", 100)
    s1 = _mk_settlement(per_user_ink=10)
    await IT.execute_settlement_charge(s1, "u1")
    # 같은 settlement_id + 같은 유저인데 nominal 이 다른 요청(위조/불일치).
    s2 = _mk_settlement(per_user_ink=20)
    with pytest.raises(IT.InkTransactionConflictError):
        await IT.execute_settlement_charge(s2, "u1")
    assert _balance("u1") == 90          # 무변이


async def test_57_account_write_failure_leaves_state_unchanged(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    # 계정 원자 교체 실패 주입.
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (_ for _ in ()).throw(OSError("replace 실패")))
    with pytest.raises(IT.InkTransactionPersistenceError):
        await IT.execute_settlement_charge(s, "u1")
    # 주의: monkeypatch.undo() 는 autouse _isolated_cwd 의 chdir 까지 되돌리므로
    # 호출하지 않는다. 이후 단언은 os.replace 를 쓰지 않는 읽기뿐이다.
    assert _balance("u1") == 100
    assert _marker("u1", ink_tx_id) is None


async def test_58_no_ledger_record_if_account_write_failed(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (_ for _ in ()).throw(OSError("replace 실패")))
    with pytest.raises(IT.InkTransactionPersistenceError):
        await IT.execute_settlement_charge(s, "u1")
    IT._LEDGER_REGISTRY.clear()
    assert _ledger("u1").get_by_id_strict(ink_tx_id) is None


async def test_59_ledger_fail_after_account_write_raises_recovery(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    ledger_path = os.path.abspath(IT.default_ink_tx_ledger_path("u1"))
    real_open = builtins.open

    def failing_open(path, mode="r", *a, **k):
        # 원장 append 만 실패시킨다(계정 쓰기는 통과).
        if os.path.abspath(str(path)) == ledger_path and "a" in mode:
            raise OSError("원장 디스크 꽉참(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(IT.InkTransactionRecoveryRequired):
        await IT.execute_settlement_charge(s, "u1")
    # 잔액은 한 번 변했고 마커는 존재한다(읽기는 실패 주입 대상이 아님).
    assert _balance("u1") == 90
    assert _marker("u1", ink_tx_id) is not None
    # 원장에는 아직 라인이 없다.
    IT._LEDGER_REGISTRY.clear()
    assert _ledger("u1").get_by_id_strict(ink_tx_id) is None


async def test_60_replay_after_59_repairs_ledger_without_rededuct(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    ledger_path = os.path.abspath(IT.default_ink_tx_ledger_path("u1"))
    real_open = builtins.open
    fail = {"on": True}

    def failing_open(path, mode="r", *a, **k):
        if fail["on"] and os.path.abspath(str(path)) == ledger_path and "a" in mode:
            raise OSError("원장 실패(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(IT.InkTransactionRecoveryRequired):
        await IT.execute_settlement_charge(s, "u1")

    fail["on"] = False                                  # 실패 주입 해제(undo 없이)
    IT._LEDGER_REGISTRY.clear()
    r = await IT.execute_settlement_charge(s, "u1")     # 재실행 = 복구
    assert r.status == IT.EXEC_RECOVERED_LEDGER
    assert _balance("u1") == 90                         # 재차감 없음
    assert _ledger("u1").get_by_id_strict(ink_tx_id) is not None


async def test_61_replay_after_both_durable_is_noop(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    await IT.execute_settlement_charge(s, "u1")
    r = await IT.execute_settlement_charge(s, "u1")
    assert r.status == IT.EXEC_ALREADY
    assert _balance("u1") == 90
    assert len(_ledger("u1").list_all()) == 1


async def test_62_ledger_present_marker_absent_raises(tmp_path):
    # 마커 없이 원장 라인만 존재하는 비정상 상태.
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    led = _ledger("u1")
    led.append_strict(IT.InkTransaction(
        ink_tx_id=ink_tx_id, settlement_id=s.settlement_id, user_id="u1",
        kind=IT.KIND_CHARGE, nominal_charge_ink=10, balance_before=100,
        balance_after=90, applied_balance_delta=-10, overdraft=False,
        operator_subsidy_ink=0, reason=IT.CHARGE_REASON, created_at=0.0))
    with pytest.raises(IT.InkTransactionRecoveryRequired):
        await IT.execute_settlement_charge(s, "u1")
    assert _balance("u1") == 100         # 조용한 재차감 없음


async def test_63_concurrent_duplicate_charges_once(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    results = await asyncio.gather(
        IT.execute_settlement_charge(s, "u1"),
        IT.execute_settlement_charge(s, "u1"),
    )
    assert _balance("u1") == 90          # 정확히 한 번 차감
    statuses = sorted(r.status for r in results)
    # 하나는 신규 적용, 다른 하나는 이미 적용/복구.
    assert IT.EXEC_APPLIED in statuses
    assert len(_ledger("u1").list_all()) == 1


def test_64_existing_lock_is_only_account_mutation_domain():
    # 계정 뮤테이션 락은 여전히 accounts._lock_for 하나뿐이다.
    assert source_of("core/accounts.py").count("asyncio.Lock()") == 1
    # ink_transactions 는 경쟁하는 계정 락 도메인을 도입하지 않는다.
    assert "asyncio.Lock()" not in source_of("core/ink_transactions.py")


# ══════════════════════════════════════════════════════════════
#  §36 — overdraft / 호환 (65~70)
# ══════════════════════════════════════════════════════════════

async def test_65_sufficient_balance_no_subsidy(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 100)
    res = await IT.execute_settlement_charge(s, "u1")
    tx = res.transaction
    assert tx.balance_before == 100 and tx.balance_after == 90
    assert tx.applied_balance_delta == -10
    assert tx.operator_subsidy_ink == 0 and tx.overdraft is False


async def test_66_overdraft_floor_and_explicit_subsidy(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 5)
    res = await IT.execute_settlement_charge(s, "u1", allow_overdraft=True)
    tx = res.transaction
    assert tx.balance_after == 1                  # 1잉크 floor 보존
    assert tx.nominal_charge_ink == 10            # nominal 보존
    assert tx.applied_balance_delta == -4         # 실제 잔액 변화(진실)
    assert tx.operator_subsidy_ink == 6           # 명시적 운영자 보조
    assert tx.overdraft is True


async def test_67_overdraft_replay_no_double(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 5)
    await IT.execute_settlement_charge(s, "u1", allow_overdraft=True)
    r2 = await IT.execute_settlement_charge(s, "u1", allow_overdraft=True)
    assert r2.status == IT.EXEC_ALREADY
    assert _balance("u1") == 1
    assert len(_ledger("u1").list_all()) == 1


async def test_68_overdraft_not_allowed_insufficient_no_mutation(tmp_path):
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 5)
    ink_tx_id = IT.expected_ink_tx_id(s.settlement_id, "u1")
    with pytest.raises(IT.InsufficientInkError):
        await IT.execute_settlement_charge(s, "u1", allow_overdraft=False)
    assert _balance("u1") == 5                    # 무변이
    assert _marker("u1", ink_tx_id) is None
    assert _ledger("u1").get_by_id_strict(ink_tx_id) is None


async def test_69_compat_cumulative_field_preserved(tmp_path):
    # total_spent_ink 는 레거시 deduct_ink 처럼 nominal 기준으로 누적된다.
    s = _mk_settlement(per_user_ink=10)
    _seed_account("u1", 5, spent=3)
    await IT.execute_settlement_charge(s, "u1", allow_overdraft=True)
    acc = accounts.load_account_strict("u1")
    assert acc["total_spent_ink"] == 13           # 3 + nominal(10)


def test_70_legacy_deduct_ink_callers_unchanged():
    gm = source_of("cogs/gm.py")
    # 레거시 턴 결제가 여전히 tolerant deduct_ink(allow_overdraft=True)를 쓴다.
    assert "core.accounts.deduct_ink(_uid, ink, allow_overdraft=True)" in gm
    # 프로덕션 deduct_ink 호출자 수는 변하지 않았다(gm/session/system).
    n = 0
    from tests.conftest import REPO_ROOT
    for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, "cogs")):
        if "__pycache__" in dirpath:
            continue
        for fn in files:
            if fn.endswith(".py"):
                n += source_of(os.path.relpath(os.path.join(dirpath, fn),
                                               REPO_ROOT)).count("deduct_ink(")
    assert n == 3


# ══════════════════════════════════════════════════════════════
#  §37 — 멀티 유저 배치 재실행 (71~75)
# ══════════════════════════════════════════════════════════════

async def test_71_two_users_each_charged_once(tmp_path):
    s = _mk_settlement(per_user_ink=10, users=("u1", "u2"))
    _seed_account("u1", 100)
    _seed_account("u2", 50)
    results = await IT.execute_settlement_charges(s)
    assert set(results) == {"u1", "u2"}
    assert _balance("u1") == 90 and _balance("u2") == 40
    assert len(_ledger("u1").list_all()) == 1
    assert len(_ledger("u2").list_all()) == 1


async def test_72_73_partial_batch_failure_then_retry(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10, users=("u1", "u2"))
    _seed_account("u1", 100)
    _seed_account("u2", 50)
    u2_acct = os.path.abspath(accounts._path("u2"))
    real_replace = os.replace
    fail = {"on": True}

    def failing_replace(a, b):
        if fail["on"] and os.path.abspath(str(b)) == u2_acct:
            raise OSError("u2 계정 교체 실패(시뮬)")
        return real_replace(a, b)

    # 1차: u1 성공, u2 계정 쓰기 전 실패.
    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(IT.InkTransactionPersistenceError):
        await IT.execute_settlement_charges(s)
    assert _balance("u1") == 90          # u1 은 이미 내구화
    assert _balance("u2") == 50          # u2 는 무변이

    # 2차(재실행): u1 은 재차감 없음, u2 는 정확히 한 번.
    fail["on"] = False                   # 실패 주입 해제(undo 없이)
    IT._LEDGER_REGISTRY.clear()
    results = await IT.execute_settlement_charges(s)
    assert _balance("u1") == 90
    assert _balance("u2") == 40
    assert results["u1"].status == IT.EXEC_ALREADY
    assert results["u2"].status == IT.EXEC_APPLIED


async def test_74_partial_ledger_repair_isolated_per_user(tmp_path, monkeypatch):
    s = _mk_settlement(per_user_ink=10, users=("u1", "u2"))
    _seed_account("u1", 100)
    _seed_account("u2", 50)
    # u2 를 정상 완료.
    await IT.execute_settlement_charge(s, "u2")
    # u1 은 계정 적용됐으나 원장 append 실패 상태를 만든다.
    ink_tx_u1 = IT.expected_ink_tx_id(s.settlement_id, "u1")
    led1_path = os.path.abspath(IT.default_ink_tx_ledger_path("u1"))
    real_open = builtins.open
    fail = {"on": True}

    def failing_open(path, mode="r", *a, **k):
        if fail["on"] and os.path.abspath(str(path)) == led1_path and "a" in mode:
            raise OSError("u1 원장 실패(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(IT.InkTransactionRecoveryRequired):
        await IT.execute_settlement_charge(s, "u1")

    fail["on"] = False                                  # 실패 주입 해제(undo 없이)
    IT._LEDGER_REGISTRY.clear()
    results = await IT.execute_settlement_charges(s)     # 배치 재실행
    assert results["u1"].status == IT.EXEC_RECOVERED_LEDGER
    assert results["u2"].status == IT.EXEC_ALREADY
    assert _balance("u1") == 90 and _balance("u2") == 40
    assert _ledger("u1").get_by_id_strict(ink_tx_u1) is not None
    assert len(_ledger("u2").list_all()) == 1


async def test_75_aggregate_nominal_equals_settlement_value(tmp_path):
    s = _mk_settlement(per_user_ink=10, users=("u1", "u2", "u3"))
    assert s.aggregate_nominal_charge_ink == 30      # 10 * 3 (분할 아님)
    for u in ("u1", "u2", "u3"):
        _seed_account(u, 100)
    results = await IT.execute_settlement_charges(s)
    total_nominal = sum(r.transaction.nominal_charge_ink for r in results.values())
    assert total_nominal == s.aggregate_nominal_charge_ink
