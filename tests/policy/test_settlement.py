"""WP-SETTLEMENT-01 — TurnSettlement 모델·빌더·저장소 검증(핸드오프 §30~§33).

결정적 settlement 정체성, 명시적 strict CostEvent-ID 소비, billing_hint/usage_source
분류, FAILED_SYSTEM 0 청구, 단일 반올림 경계, append-only 저장/멱등/충돌/손상,
그리고 하나의 CostEvent 단일-settlement 소유를 증명한다. conftest 의 _isolated_cwd 로
sessions/ 는 tmp 작업디렉터리에 격리된다.
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from core import cost_ledger as cl
from core import settlement as S

pytestmark = pytest.mark.policy


# ── 헬퍼 ─────────────────────────────────────────────────────

def _ledger(tmp_path, name="cost.jsonl"):
    return cl.CostLedger(str(tmp_path / name))


def _ev(event_id, *, key=None, session_id="s", transaction_id="t",
        logical_turn=1, turn_attempt=1, billing_hint=cl.HINT_PLAYER_CANDIDATE,
        usage_source=cl.SOURCE_PROVIDER_METADATA, cost_krw=7.0, cost_usd=0.005,
        success=True):
    return cl.CostEvent(
        event_id=event_id, idempotency_key=key or f"op:{event_id}:attempt:1",
        created_at=0.0, provider=cl.PROVIDER_GOOGLE_GENAI,
        operation=cl.OP_TURN_JUDGMENT, model="m",
        session_id=session_id, transaction_id=transaction_id,
        logical_turn=logical_turn, turn_attempt=turn_attempt, provider_attempt=1,
        actor_user_id=None, actor_kind=cl.ACTOR_PLAYER,
        billing_hint=billing_hint, usage_source=usage_source,
        cost_krw=cost_krw, cost_usd=cost_usd, success=success)


def _seed(led, *events):
    for ev in events:
        led.record_cost_event_strict(ev)


def _ident(session_id="s", transaction_id="t", logical_turn=1, attempt=1):
    return S.TransactionIdentity(session_id=session_id, transaction_id=transaction_id,
                                 logical_turn=logical_turn, attempt=attempt)


def _store(tmp_path, name="turn_settlements.jsonl"):
    return S.SettlementStore(str(tmp_path / name))


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln for ln in f if ln.strip()]


@pytest.fixture(autouse=True)
def _clean_registry():
    cl._LEDGER_REGISTRY.clear()
    S._STORE_REGISTRY.clear()
    yield
    cl._LEDGER_REGISTRY.clear()
    S._STORE_REGISTRY.clear()


# ══════════════════════════════════════════════════════════════
#  §30 — Settlement 정체성/모델 (1~7)
# ══════════════════════════════════════════════════════════════

def test_01_deterministic_settlement_identity_from_attempt():
    assert S.settlement_id_for("tx-A", 3) == "turn-settlement:tx-A:attempt:3"


def test_02_identical_attempt_reconstructs_same_id(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1"))
    a = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    b = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert a.settlement_id == b.settlement_id


def test_03_different_attempt_different_id():
    assert S.settlement_id_for("tx", 1) != S.settlement_id_for("tx", 2)


def test_04_model_is_frozen(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1"))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.charge_ink_per_user = 999


def test_05_billing_users_normalize_deterministically():
    assert S.normalize_billing_user_ids([3, "1", 2, "1"]) == ("1", "2", "3")


def test_06_blank_billing_user_rejected():
    with pytest.raises(S.SettlementPolicyError):
        S.normalize_billing_user_ids(["u1", "  "])


def test_07_duplicate_cost_event_ids_rejected(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1"))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1", "e1"], billing_user_ids=["u1"])


# ══════════════════════════════════════════════════════════════
#  §31 — 정확한 CostEvent 집합 + 분류 (8~20)
# ══════════════════════════════════════════════════════════════

def test_08_exact_events_loaded_via_strict_lookup(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", cost_krw=7.0), _ev("e2", cost_krw=7.0), _ev("e3", cost_krw=7.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1", "e2"], billing_user_ids=["u1"])
    # e3 는 요청하지 않았으므로 흡수되지 않는다.
    assert s.included_cost_event_ids == ("e1", "e2")
    assert s.provider_cost_krw == 14.0


def test_09_missing_event_id_propagates_failure(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1"))
    with pytest.raises(cl.CostEventNotFoundError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1", "missing"], billing_user_ids=["u1"])


def test_10_transaction_id_mismatch_rejected(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", transaction_id="OTHER"))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_11_session_id_mismatch_rejected(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", session_id="OTHER"))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_12_logical_turn_mismatch_rejected_when_populated(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", logical_turn=9))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led,
                                transaction_identity=_ident(logical_turn=1),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_12b_null_logical_turn_is_tolerated(tmp_path):
    # 미채워진(None) logical_turn 은 일치를 강요하지 않는다.
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", logical_turn=None, turn_attempt=None))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.included_cost_event_ids == ("e1",)


def test_13_turn_attempt_mismatch_rejected_when_populated(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", turn_attempt=5))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led,
                                transaction_identity=_ident(attempt=1),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_14_committed_player_candidate_is_billable(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", billing_hint=cl.HINT_PLAYER_CANDIDATE, cost_krw=14.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.player_billable_cost_krw == 14.0
    assert s.charge_ink_per_user == 2      # ceil(14/7)


def test_15_free_feature_is_non_player(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", billing_hint=cl.HINT_FREE_FEATURE, cost_krw=14.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.player_billable_cost_krw == 0.0
    assert s.non_player_cost_krw == 14.0
    assert s.charge_ink_per_user == 0


def test_16_operator_is_non_player(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", billing_hint=cl.HINT_OPERATOR, cost_krw=21.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.non_player_cost_krw == 21.0
    assert s.player_billable_cost_krw == 0.0


def test_17_system_is_non_player(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", billing_hint=cl.HINT_SYSTEM, cost_krw=21.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.non_player_cost_krw == 21.0
    assert s.player_billable_cost_krw == 0.0


def test_18_unknown_hint_not_silently_charged(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", billing_hint=cl.HINT_UNKNOWN, cost_krw=7.0))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_19_internal_retry_events_all_included(tmp_path):
    # 같은 트랜잭션의 여러 provider attempt 비용을 모두 공급하면 전부 합산된다.
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", cost_krw=7.0), _ev("e2", cost_krw=7.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1", "e2"], billing_user_ids=["u1"])
    assert s.player_billable_cost_krw == 14.0
    assert s.charge_ink_per_user == 2


def test_20_failed_success_flag_event_not_discarded(tmp_path):
    # success=False 지만 실측 authoritative 비용을 가진 이벤트도 공급되면 반영된다.
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", success=False, usage_source=cl.SOURCE_PROVIDER_METADATA,
                   cost_krw=7.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.provider_cost_krw == 7.0
    assert s.player_billable_cost_krw == 7.0


# ══════════════════════════════════════════════════════════════
#  §32 — FAILED_SYSTEM / 추정 / 반올림 (21~30)
# ══════════════════════════════════════════════════════════════

def _failed(tmp_path, **ev_over):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", **ev_over))
    return S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                   outcome=S.SettlementOutcome.FAILED_SYSTEM,
                                   cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_21_failed_system_zero_player_billable(tmp_path):
    s = _failed(tmp_path, billing_hint=cl.HINT_PLAYER_CANDIDATE, cost_krw=70.0)
    assert s.player_billable_cost_krw == 0.0


def test_22_failed_system_zero_ink_charge(tmp_path):
    s = _failed(tmp_path, billing_hint=cl.HINT_PLAYER_CANDIDATE, cost_krw=70.0)
    assert s.charge_ink_per_user == 0
    assert s.aggregate_nominal_charge_ink == 0


def test_23_failed_system_player_candidate_goes_to_system_unbilled(tmp_path):
    s = _failed(tmp_path, billing_hint=cl.HINT_PLAYER_CANDIDATE, cost_krw=70.0)
    assert s.system_unbilled_cost_krw == 70.0


def test_24_provider_costs_remain_represented(tmp_path):
    s = _failed(tmp_path, billing_hint=cl.HINT_PLAYER_CANDIDATE, cost_krw=70.0,
                cost_usd=0.05)
    assert s.provider_cost_krw == 70.0
    assert s.provider_cost_usd == 0.05


def test_25_provider_metadata_billed_on_committed(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", usage_source=cl.SOURCE_PROVIDER_METADATA, cost_krw=7.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.charge_ink_per_user == 1


def test_26_fixed_pricing_billed_on_committed(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", usage_source=cl.SOURCE_FIXED_PROVIDER_PRICING, cost_krw=7.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])
    assert s.charge_ink_per_user == 1


def test_27_estimate_cannot_finalize_committed_charge(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", usage_source=cl.SOURCE_ESTIMATE, cost_krw=7.0))
    with pytest.raises(S.SettlementUnresolvedEstimateError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_27b_invalid_source_positive_cost_not_silently_billed(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", usage_source=cl.SOURCE_NONE, cost_krw=7.0))
    with pytest.raises(S.SettlementUnresolvedEstimateError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1"])


def test_28_failed_system_estimate_recorded_without_charge(tmp_path):
    s = _failed(tmp_path, billing_hint=cl.HINT_PLAYER_CANDIDATE,
                usage_source=cl.SOURCE_ESTIMATE, cost_krw=70.0)
    assert s.charge_ink_per_user == 0
    assert s.estimated_cost_krw == 70.0
    assert s.system_unbilled_cost_krw == 70.0


def test_29_cost_to_ink_called_exactly_once(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", cost_krw=7.0))
    calls = {"n": 0}
    real = S.cost_to_ink

    def _counting(x):
        calls["n"] += 1
        return real(x)

    monkeypatch.setattr(S, "cost_to_ink", _counting)
    S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                            outcome=S.SettlementOutcome.COMMITTED,
                            cost_event_ids=["e1"], billing_user_ids=["u1", "u2"])
    assert calls["n"] == 1


def test_30_per_user_and_aggregate_consistent(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", cost_krw=14.0))
    s = S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=["u1", "u2", "u3"])
    assert s.charge_ink_per_user == 2
    assert s.aggregate_nominal_charge_ink == 6      # 2 * 3 (분할 아님)


def test_30b_committed_positive_charge_empty_users_rejected(tmp_path):
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", cost_krw=7.0))
    with pytest.raises(S.SettlementPolicyError):
        S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                outcome=S.SettlementOutcome.COMMITTED,
                                cost_event_ids=["e1"], billing_user_ids=[])


# ══════════════════════════════════════════════════════════════
#  §33 — Settlement 저장소 (31~41)
# ══════════════════════════════════════════════════════════════

def _committed(tmp_path, ids=("e1",), users=("u1",), seed=True, **ev_over):
    led = _ledger(tmp_path)
    if seed:
        _seed(led, *[_ev(e, **ev_over) for e in ids])
    return S.build_turn_settlement(cost_ledger=led, transaction_identity=_ident(),
                                   outcome=S.SettlementOutcome.COMMITTED,
                                   cost_event_ids=list(ids), billing_user_ids=list(users))


def test_31_settlement_appends_and_reloads(tmp_path):
    s = _committed(tmp_path)
    store = _store(tmp_path)
    store.record_strict(s)
    S._STORE_REGISTRY.clear()
    loaded = _store(tmp_path).get_settlement_strict(s.settlement_id)
    assert loaded.settlement_id == s.settlement_id
    assert loaded.included_cost_event_ids == s.included_cost_event_ids
    assert loaded.charge_ink_per_user == s.charge_ink_per_user


def test_32_append_performs_flush_fsync(tmp_path, monkeypatch):
    s = _committed(tmp_path)
    calls = {"n": 0}
    real = os.fsync
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (calls.__setitem__("n", calls["n"] + 1), real(fd))[1])
    _store(tmp_path).record_strict(s)
    assert calls["n"] >= 1


def test_33_equivalent_replay_no_second_line(tmp_path):
    s = _committed(tmp_path)
    store = _store(tmp_path)
    store.record_strict(s)
    returned = store.record_strict(s)          # 동등 replay
    assert returned.settlement_id == s.settlement_id
    assert len(_lines(store.path)) == 1


def test_34_conflicting_payload_rejected(tmp_path):
    s = _committed(tmp_path)
    store = _store(tmp_path)
    store.record_strict(s)
    conflicting = dataclasses.replace(s, charge_ink_per_user=s.charge_ink_per_user + 5)
    with pytest.raises(S.SettlementConflictError):
        store.record_strict(conflicting)
    assert len(_lines(store.path)) == 1


def test_35_replay_semantics_survive_reload(tmp_path):
    s = _committed(tmp_path)
    _store(tmp_path).record_strict(s)
    S._STORE_REGISTRY.clear()
    # 새 store 객체로 동등 replay → 두 번째 줄 없음.
    store2 = _store(tmp_path)
    store2.record_strict(s)
    assert len(_lines(store2.path)) == 1


def test_36_malformed_json_raises_corruption(tmp_path):
    store = _store(tmp_path)
    os.makedirs(os.path.dirname(store.path), exist_ok=True)
    with open(store.path, "w", encoding="utf-8") as f:
        f.write("{not valid json\n")
    with pytest.raises(S.SettlementCorruptionError):
        store.get_settlement_strict("x")


def test_37_structurally_invalid_raises_corruption(tmp_path):
    store = _store(tmp_path)
    os.makedirs(os.path.dirname(store.path), exist_ok=True)
    with open(store.path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"settlement_id": "", "included_cost_event_ids": []}) + "\n")
    with pytest.raises(S.SettlementCorruptionError):
        store.get_settlement_strict("x")


def test_38_duplicate_durable_id_rejected(tmp_path):
    s = _committed(tmp_path)
    store = _store(tmp_path)
    os.makedirs(os.path.dirname(store.path), exist_ok=True)
    row = json.dumps(S.settlement_to_dict(s), ensure_ascii=False) + "\n"
    with open(store.path, "w", encoding="utf-8") as f:
        f.write(row)
        f.write(row)               # 같은 settlement_id 두 줄(외부 조작/손상)
    with pytest.raises(S.SettlementCorruptionError):
        store.get_settlement_strict(s.settlement_id)


def test_39_one_cost_event_cannot_belong_to_two_settlements(tmp_path):
    # 서로 다른 두 트랜잭션 attempt 가 같은 CostEvent e1 을 주장한다.
    led = _ledger(tmp_path)
    _seed(led, _ev("e1", transaction_id="t", turn_attempt=1),
          _ev("e1b", transaction_id="t2", turn_attempt=1, key="op:e1b:attempt:1"))
    s1 = S.build_turn_settlement(cost_ledger=led,
                                 transaction_identity=_ident(transaction_id="t", attempt=1),
                                 outcome=S.SettlementOutcome.COMMITTED,
                                 cost_event_ids=["e1"], billing_user_ids=["u1"])
    # 두 번째 settlement 이 이미 소유된 e1 을 주장(다른 transaction_id/attempt).
    s2 = dataclasses.replace(
        s1, settlement_id="turn-settlement:t2:attempt:1", transaction_id="t2")
    store = _store(tmp_path)
    store.record_strict(s1)
    with pytest.raises(S.CostEventAlreadySettledError):
        store.record_strict(s2)


def test_40_strict_get_returns_single_canonical(tmp_path):
    s = _committed(tmp_path)
    store = _store(tmp_path)
    store.record_strict(s)
    got = store.get_settlement_strict(s.settlement_id)
    assert isinstance(got, S.TurnSettlement)
    assert got.settlement_id == s.settlement_id


def test_41_missing_settlement_id_is_explicit(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(S.SettlementNotFoundError):
        store.get_settlement_strict("turn-settlement:nope:attempt:1")
    assert store.find_settlement("turn-settlement:nope:attempt:1") is None
