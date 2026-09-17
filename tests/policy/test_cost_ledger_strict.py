"""WP-ACCOUNTING-PREREQ-01 — strict CostLedger 경로 검증(핸드오프 §10, 1-16).

strict append/idempotency/durability/corruption/exact-ID 와, tolerant shadow
경로가 그대로 보존됨을 함께 증명한다. 실제 provider SDK 를 부르지 않으며 tmp
경로로 격리한다(운영 data/cost_ledger.jsonl 을 만지지 않는다).
"""

from __future__ import annotations

import builtins
import json
import os
import threading

import pytest

from core import cost_ledger as cl

pytestmark = pytest.mark.policy


# ── 헬퍼 ─────────────────────────────────────────────────────

def _ledger(tmp_path, name="ledger.jsonl"):
    return cl.CostLedger(str(tmp_path / name))


def _ev(*, event_id="e1", key="op:x:attempt:1", **over):
    base = dict(
        event_id=event_id, idempotency_key=key, created_at=0.0,
        provider=cl.PROVIDER_GOOGLE_GENAI, operation=cl.OP_TURN_JUDGMENT,
        model="m", session_id="s", transaction_id="t", logical_turn=1,
        turn_attempt=1, provider_attempt=1, actor_user_id=None,
        actor_kind=cl.ACTOR_PLAYER, billing_hint=cl.HINT_PLAYER_CANDIDATE,
        cost_krw=1.0, cost_usd=0.001,
    )
    base.update(over)
    return cl.CostEvent(**base)


def _lines(path):
    with open(path, encoding="utf-8") as f:
        return [ln for ln in f if ln.strip()]


@pytest.fixture(autouse=True)
def _clean_registry():
    cl._LEDGER_REGISTRY.clear()
    yield
    cl._LEDGER_REGISTRY.clear()


# ── 1-2. strict append / durability ─────────────────────────

def test_1_strict_append_writes_reloadable_event(tmp_path):
    led = _ledger(tmp_path)
    assert led.record_cost_event_strict(_ev()) is True
    cl._LEDGER_REGISTRY.clear()
    rows = _ledger(tmp_path).list_cost_events_strict()
    assert len(rows) == 1
    assert rows[0]["event_id"] == "e1"
    assert rows[0]["idempotency_key"] == "op:x:attempt:1"
    assert rows[0]["cost_krw"] == 1.0


def test_2_strict_append_performs_flush_fsync(tmp_path, monkeypatch):
    calls = {"n": 0}
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.__setitem__("n", calls["n"] + 1), real(fd))[1])
    _ledger(tmp_path).record_cost_event_strict(_ev())
    assert calls["n"] >= 1


# ── 3-5. payload-aware idempotency ──────────────────────────

def test_3_same_key_same_payload_is_noop(tmp_path):
    led = _ledger(tmp_path)
    assert led.record_cost_event_strict(_ev(event_id="e1", created_at=1.0)) is True
    # 다른 envelope(event_id/created_at), 같은 canonical payload → no-op
    assert led.record_cost_event_strict(_ev(event_id="e2", created_at=2.0)) is False
    assert len(_lines(str(tmp_path / "ledger.jsonl"))) == 1


def test_4_same_key_conflicting_payload_is_rejected(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    led = _ledger(tmp_path)
    assert led.record_cost_event_strict(_ev(cost_krw=1.0)) is True
    for conflicting in (_ev(event_id="c1", cost_krw=999.0),
                        _ev(event_id="c2", session_id="other"),
                        _ev(event_id="c3", provider_attempt=2),
                        _ev(event_id="c4", metadata={"pricing_basis": "vX"})):
        with pytest.raises(cl.CostLedgerConflictError):
            led.record_cost_event_strict(conflicting)
    assert len(_lines(path)) == 1
    assert json.loads(_lines(path)[0])["cost_krw"] == 1.0


def test_5_idempotency_survives_reload(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    _ledger(tmp_path).record_cost_event_strict(_ev(cost_krw=1.0))
    cl._LEDGER_REGISTRY.clear()                       # 프로세스 재시작 모사
    led2 = _ledger(tmp_path)
    assert led2.record_cost_event_strict(_ev(event_id="e9", cost_krw=1.0)) is False
    with pytest.raises(cl.CostLedgerConflictError):
        led2.record_cost_event_strict(_ev(event_id="e9", cost_krw=2.0))
    assert len(_lines(path)) == 1


# ── 6. concurrency ──────────────────────────────────────────

def test_6_concurrent_equivalent_replay_one_durable_event(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    a, b = _ledger(tmp_path), _ledger(tmp_path)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name, led):
        barrier.wait()
        results[name] = led.record_cost_event_strict(_ev(event_id=name, cost_krw=1.0))

    t1 = threading.Thread(target=worker, args=("A", a))
    t2 = threading.Thread(target=worker, args=("B", b))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(results.values()) == [False, True]
    assert len(_lines(path)) == 1


# ── 7-9. failure injection ──────────────────────────────────

def test_7_write_failure_is_observable(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    real_open = builtins.open

    def failing_open(path, mode="r", *a, **k):
        if "a" in mode:
            raise OSError("디스크 꽉참(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(cl.CostLedgerPersistenceError):
        led.record_cost_event_strict(_ev())


def test_8_fsync_failure_is_observable(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync 실패")))
    with pytest.raises(cl.CostLedgerPersistenceError):
        led.record_cost_event_strict(_ev())
    # 실패는 성공(False)으로 위장되지 않는다: 키가 등재되지 않는다.
    assert led.has_idempotency_key("op:x:attempt:1") is False


def test_9_failed_append_leaves_no_malformed_line(tmp_path, monkeypatch):
    path = str(tmp_path / "ledger.jsonl")
    led = _ledger(tmp_path)
    assert led.record_cost_event_strict(_ev(event_id="e1", key="op:x:attempt:1")) is True
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(cl.CostLedgerPersistenceError):
        led.record_cost_event_strict(_ev(event_id="e2", key="op:x:attempt:2"))
    monkeypatch.undo()
    cl._LEDGER_REGISTRY.clear()
    rows = _ledger(tmp_path).list_cost_events_strict()      # 손상 없이 재파싱
    assert [r["event_id"] for r in rows] == ["e1"]
    assert len(_lines(path)) == 1


# ── 10-11. corruption ───────────────────────────────────────

def test_10_malformed_json_line_raises_corruption(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": "e1", "idempotency_key": "k"}) + "\n")
        f.write("NOT JSON\n")
    led = _ledger(tmp_path)
    with pytest.raises(cl.CostLedgerCorruptionError):
        led.list_cost_events_strict()


def test_11_structurally_invalid_event_raises_corruption(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    # 유효 JSON 이지만 event_id 누락
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"idempotency_key": "k", "cost_krw": 1}) + "\n")
    with pytest.raises(cl.CostLedgerCorruptionError):
        _ledger(tmp_path).list_cost_events_strict()
    # 유효 JSON 이지만 객체가 아님
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(["not", "an", "object"]) + "\n")
    cl._LEDGER_REGISTRY.clear()
    with pytest.raises(cl.CostLedgerCorruptionError):
        _ledger(tmp_path).list_cost_events_strict()


# ── 12-14. exact-ID lookup ──────────────────────────────────

def test_12_exact_id_lookup_returns_requested_records(tmp_path):
    led = _ledger(tmp_path)
    led.record_cost_event_strict(_ev(event_id="e1", key="op:x:attempt:1"))
    led.record_cost_event_strict(_ev(event_id="e2", key="op:x:attempt:2"))
    led.record_cost_event_strict(_ev(event_id="e3", key="op:x:attempt:3"))
    got = led.get_cost_events_by_ids_strict(["e3", "e1"])
    assert [o["event_id"] for o in got] == ["e3", "e1"]     # 요청 순서 보존


def test_13_missing_requested_id_raises(tmp_path):
    led = _ledger(tmp_path)
    led.record_cost_event_strict(_ev(event_id="e1", key="op:x:attempt:1"))
    with pytest.raises(cl.CostEventNotFoundError):
        led.get_cost_events_by_ids_strict(["e1", "does-not-exist"])


def test_14_duplicate_stored_event_id_is_rejected(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": "dup", "idempotency_key": "k1"}) + "\n")
        f.write(json.dumps({"event_id": "dup", "idempotency_key": "k2"}) + "\n")
    led = _ledger(tmp_path)
    with pytest.raises(cl.CostLedgerConflictError):
        led.get_cost_events_by_ids_strict(["dup"])


# ── 15-16. tolerant shadow 경로 보존 ─────────────────────────

def test_15_tolerant_record_still_best_effort(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    assert led.record_cost_event(_ev()) is True           # 정상 기록
    # 쓰기 실패를 주입해도 shadow 는 예외를 던지지 않고 False 로 흡수한다.
    real_open = builtins.open

    def failing_open(path, mode="r", *a, **k):
        if "a" in mode:
            raise OSError("디스크 꽉참(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    assert led.record_cost_event(_ev(event_id="e2", key="op:x:attempt:2")) is False


def test_16_tolerant_reader_skips_malformed_but_strict_raises(tmp_path):
    path = str(tmp_path / "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": "e1", "idempotency_key": "k1",
                            "session_id": "s", "cost_krw": 2.0}) + "\n")
        f.write("corrupt line\n")
        f.write(json.dumps({"event_id": "e2", "idempotency_key": "k2",
                            "session_id": "s", "cost_krw": 3.0}) + "\n")
    led = _ledger(tmp_path)
    # tolerant: malformed 라인을 조용히 건너뛰고 유효 2건 반환(기존 계약).
    tolerant = led.list_cost_events()
    assert [o["event_id"] for o in tolerant] == ["e1", "e2"]
    # strict: 같은 파일에서 손상을 표면화.
    with pytest.raises(cl.CostLedgerCorruptionError):
        led.list_cost_events_strict()
