"""WP-JOURNAL-01 — 커밋 저널 파운데이션 검증.

핸드오프 §13 의 24개 시나리오를 커버한다:
  write/read(1-4) · idempotency(5-7) · durability/failure(8-10) ·
  corruption(11-12) · recovery classifier(13-19) · boundary preservation(20-22).
(23/24 기존 스위트/xfail 불변은 전체 러너 실행으로 확인한다.)

실패 주입 + tmp 경로만 사용하며 실제 세션 저널을 만지지 않는다.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

import core
import core.commit_journal as cj

pytestmark = pytest.mark.policy


# ── 헬퍼 ─────────────────────────────────────────────────────

def _jpath(tmp_path) -> str:
    return str(tmp_path / "turn_commit_journal.jsonl")


def _entry(phase, *, tx="tx-1", sid="s-1", lt=7, attempt=1, **kw):
    return cj.new_entry(transaction_id=tx, session_id=sid, logical_turn=lt,
                        attempt=attempt, phase=phase, **kw)


@pytest.fixture(autouse=True)
def _clean_registry():
    """경로 단위 공유 레지스트리를 테스트 간 초기화(프로세스 격리 모사)."""
    cj._JOURNAL_REGISTRY.clear()
    yield
    cj._JOURNAL_REGISTRY.clear()


# ══════════════════════════════════════════════════════════════
#  1-4. Journal write/read
# ══════════════════════════════════════════════════════════════

def test_1_single_entry_appends_and_reloads_exactly(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    e = _entry("PREPARED", completed_steps=["a", "b"], metadata={"k": 1})
    assert j.append_entry(e) is True

    reloaded = cj.CommitJournal(_jpath(tmp_path)).list_entries()
    assert len(reloaded) == 1
    r = reloaded[0]
    assert r.transaction_id == e.transaction_id
    assert r.session_id == e.session_id
    assert r.logical_turn == e.logical_turn
    assert r.attempt == e.attempt
    assert r.phase is cj.CommitPhase.PREPARED
    assert r.completed_steps == ["a", "b"]
    assert r.metadata == {"k": 1}
    assert r.idempotency_key == e.idempotency_key


def test_2_multiple_phases_preserve_append_order(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    seq = ["PREPARED", "GAME_STATE_APPLIED_IN_MEMORY", "SESSION_PERSISTED",
           "REWIND_RECORDED", "BILLING_APPLIED", "COMMITTED"]
    for ph in seq:
        assert j.append_entry(_entry(ph)) is True
    got = [e.phase.value for e in j.list_entries()]
    assert got == seq


def test_3_missing_journal_returns_empty_no_history(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))  # 파일 생성 전
    assert j.list_entries() == []
    assert j.latest_entry() is None
    assert j.history_for_transaction("tx-1", attempt=1) == []
    # 빈 이력은 손상이 아니라 NO_JOURNAL 로 분류된다.
    assert j.classify_recovery("tx-1", 1) is cj.RecoveryDisposition.NO_JOURNAL


def test_4_history_lookup_uses_exact_transaction_and_attempt(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    j.append_entry(_entry("PREPARED", tx="txA", attempt=1))
    j.append_entry(_entry("SESSION_PERSISTED", tx="txA", attempt=2))
    j.append_entry(_entry("PREPARED", tx="txB", attempt=1))

    a1 = j.history_for_transaction("txA", attempt=1)
    assert [e.phase for e in a1] == [cj.CommitPhase.PREPARED]
    a2 = j.history_for_transaction("txA", attempt=2)
    assert [e.phase for e in a2] == [cj.CommitPhase.SESSION_PERSISTED]
    # 다른 시도의 latest 를 잘못 반환하지 않는다.
    assert j.latest_entry(transaction_id="txA", attempt=1).phase is cj.CommitPhase.PREPARED
    assert j.latest_entry(transaction_id="txB", attempt=1).phase is cj.CommitPhase.PREPARED


# ══════════════════════════════════════════════════════════════
#  5-7. Idempotency
# ══════════════════════════════════════════════════════════════

def test_5_duplicate_same_key_appends_once(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    e1 = _entry("PREPARED")
    e2 = _entry("PREPARED")  # 다른 entry_id/timestamp, 같은 (tx,attempt,phase)
    assert e1.idempotency_key == e2.idempotency_key
    assert j.append_entry(e1) is True
    assert j.append_entry(e2) is False  # 중복 억제
    assert len(j.list_entries()) == 1
    assert sum(1 for _ in open(_jpath(tmp_path))) == 1  # 파일에도 한 줄만


def test_6_dedup_survives_new_object_and_reload(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    assert j.append_entry(_entry("PREPARED")) is True

    # 프로세스 재시작 모사: 인메모리 dedup 상태를 비우고 파일에서 재구성.
    cj._JOURNAL_REGISTRY.clear()
    j2 = cj.CommitJournal(_jpath(tmp_path))
    assert j2.has_idempotency_key(_entry("PREPARED").idempotency_key) is True
    assert j2.append_entry(_entry("PREPARED")) is False  # 파일 기반 중복 인지
    assert sum(1 for _ in open(_jpath(tmp_path))) == 1


def test_7_two_objects_same_path_concurrent_append_once(tmp_path):
    path = _jpath(tmp_path)
    j1, j2 = cj.CommitJournal(path), cj.CommitJournal(path)
    barrier = threading.Barrier(2)
    results = {}

    def worker(name, journal):
        barrier.wait()
        results[name] = journal.append_entry(_entry("PREPARED"))

    t1 = threading.Thread(target=worker, args=("a", j1))
    t2 = threading.Thread(target=worker, args=("b", j2))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 정확히 하나만 True, 파일엔 한 줄만.
    assert sorted(results.values()) == [False, True]
    assert sum(1 for _ in open(path)) == 1


# ══════════════════════════════════════════════════════════════
#  8-10. Durability / failure
# ══════════════════════════════════════════════════════════════

def test_8_fsync_is_invoked_on_successful_append(tmp_path, monkeypatch):
    """성공 append 가 실제로 fsync 를 지난다(§19 'flush without fsync' 방지)."""
    calls = {"n": 0}
    real = os.fsync

    def counting(fd):
        calls["n"] += 1
        return real(fd)

    monkeypatch.setattr(os, "fsync", counting)
    cj.CommitJournal(_jpath(tmp_path)).append_entry(_entry("PREPARED"))
    assert calls["n"] >= 1


def test_9_fsync_failure_is_observable_and_not_success(tmp_path, monkeypatch):
    def boom(fd):
        raise OSError("fsync 실패 시뮬레이션")

    monkeypatch.setattr(os, "fsync", boom)
    j = cj.CommitJournal(_jpath(tmp_path))
    with pytest.raises(cj.CommitJournalPersistenceError) as ei:
        j.append_entry(_entry("PREPARED"))
    assert isinstance(ei.value.__cause__, OSError)  # 원인 보존
    # 실패한 append 는 dedup 키로 등재되지 않는다(성공으로 처리되지 않음).
    assert j.has_idempotency_key(_entry("PREPARED").idempotency_key) is False


def test_10_no_malformed_successful_line_after_injected_failure(tmp_path, monkeypatch):
    path = _jpath(tmp_path)
    j = cj.CommitJournal(path)
    # 먼저 유효 라인 1건 기록.
    assert j.append_entry(_entry("PREPARED")) is True

    # 이후 fsync 실패를 주입하고 두 번째 append 시도 → 롤백되어야 한다.
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(cj.CommitJournalPersistenceError):
        j.append_entry(_entry("SESSION_PERSISTED"))

    # 파일엔 유효한 한 줄만 남고, 재파싱이 손상 없이 성공한다.
    monkeypatch.undo()
    fresh = cj.CommitJournal(path)
    cj._JOURNAL_REGISTRY.clear()
    entries = fresh.list_entries()
    assert [e.phase for e in entries] == [cj.CommitPhase.PREPARED]
    assert sum(1 for _ in open(path)) == 1


# ══════════════════════════════════════════════════════════════
#  11-12. Corruption
# ══════════════════════════════════════════════════════════════

def test_11_malformed_json_line_raises_corruption(tmp_path):
    path = _jpath(tmp_path)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"transaction_id":"tx","session_id":"s","logical_turn":7,'
                '"attempt":1,"phase":"PREPARED","idempotency_key":"k"}\n')
        f.write("this is not json\n")  # 손상 라인
    j = cj.CommitJournal(path)
    with pytest.raises(cj.CommitJournalCorruptionError):
        j.list_entries()
    # 손상을 빈 이력으로 오인해 조용히 분류하지 않는다.
    with pytest.raises(cj.CommitJournalCorruptionError):
        j.classify_recovery("tx", 1)


def test_12_structurally_invalid_record_not_accepted(tmp_path):
    path = _jpath(tmp_path)
    # (a) 미지의 phase 값
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"transaction_id":"tx","session_id":"s","logical_turn":7,'
                '"attempt":1,"phase":"NOT_A_PHASE"}\n')
    with pytest.raises(cj.CommitJournalCorruptionError):
        cj.CommitJournal(path).list_entries()

    # (b) 필수 정체성 필드 누락(transaction_id 없음)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"session_id":"s","logical_turn":7,"attempt":1,"phase":"PREPARED"}\n')
    cj._JOURNAL_REGISTRY.clear()
    with pytest.raises(cj.CommitJournalCorruptionError):
        cj.CommitJournal(path).list_entries()


# ══════════════════════════════════════════════════════════════
#  13-19. Recovery classifier (순수·부수효과 없음)
# ══════════════════════════════════════════════════════════════

def test_13_no_history_is_no_journal(tmp_path):
    assert cj.CommitJournal(_jpath(tmp_path)).classify_recovery("tx", 1) \
        is cj.RecoveryDisposition.NO_JOURNAL


def test_14_prepared_only_is_unpersisted_disposition(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    j.append_entry(_entry("PREPARED"))
    assert j.classify_recovery("tx-1", 1) \
        is cj.RecoveryDisposition.DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT


def test_15_game_state_without_persist_is_unpersisted(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    j.append_entry(_entry("PREPARED"))
    j.append_entry(_entry("GAME_STATE_APPLIED_IN_MEMORY"))
    assert j.classify_recovery("tx-1", 1) \
        is cj.RecoveryDisposition.DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT


def test_16_session_persisted_without_billing_is_resume_billing(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    for ph in ("PREPARED", "GAME_STATE_APPLIED_IN_MEMORY", "SESSION_PERSISTED"):
        j.append_entry(_entry(ph))
    assert j.classify_recovery("tx-1", 1) is cj.RecoveryDisposition.RESUME_BILLING


def test_17_billing_applied_without_committed_is_verify_and_finalize(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    for ph in ("PREPARED", "SESSION_PERSISTED", "BILLING_APPLIED"):
        j.append_entry(_entry(ph))
    assert j.classify_recovery("tx-1", 1) is cj.RecoveryDisposition.VERIFY_AND_FINALIZE


def test_18_committed_is_complete(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    for ph in ("PREPARED", "SESSION_PERSISTED", "BILLING_APPLIED", "COMMITTED"):
        j.append_entry(_entry(ph))
    assert j.classify_recovery("tx-1", 1) is cj.RecoveryDisposition.COMPLETE


def test_19_recovery_required_is_explicit(tmp_path):
    j = cj.CommitJournal(_jpath(tmp_path))
    j.append_entry(_entry("SESSION_PERSISTED"))
    j.append_entry(_entry("RECOVERY_REQUIRED"))
    # 다른 페이즈로 조용히 재해석하지 않는다(§9.6).
    assert j.classify_recovery("tx-1", 1) is cj.RecoveryDisposition.RECOVERY_REQUIRED


def test_19b_classifier_is_pure_no_side_effects():
    """분류기는 입력 이력을 변이하지 않고 enum 만 반환한다."""
    entries = [_entry("SESSION_PERSISTED")]
    snapshot = list(entries)
    out = cj.classify_disposition(entries)
    assert out is cj.RecoveryDisposition.RESUME_BILLING
    assert entries == snapshot  # 입력 불변


# ══════════════════════════════════════════════════════════════
#  20-22. Boundary preservation
# ══════════════════════════════════════════════════════════════

async def test_20_composes_with_strict_save(tmp_path, wired_bot, session_auto_ready):
    """append PREPARED → save_session_data_strict → append SESSION_PERSISTED (J5).

    격리 세션 + tmp 저널 경로로 합성 가능성만 증명한다. 라이브 턴 오케스트레이터에
    배선하지 않는다.
    """
    j = cj.CommitJournal(_jpath(tmp_path))
    sid = session_auto_ready.session_id
    lt = session_auto_ready.turn_count + 1
    common = dict(tx="txC", sid=sid, lt=lt, attempt=1)

    assert j.append_entry(_entry("PREPARED", **common)) is True
    # 이미 검증된 strict 프리미티브를 그대로 사용(재구현하지 않음).
    result = await core.save_session_data_strict(wired_bot, session_auto_ready)
    assert result is None  # strict 성공은 예외 부재로 신호
    assert os.path.exists(f"sessions/{sid}/data.json")
    assert j.append_entry(_entry("SESSION_PERSISTED", **common)) is True

    hist = j.history_for_transaction("txC", attempt=1)
    assert [e.phase for e in hist] == [cj.CommitPhase.PREPARED,
                                       cj.CommitPhase.SESSION_PERSISTED]
    assert j.classify_recovery("txC", 1) is cj.RecoveryDisposition.RESUME_BILLING


def test_21_no_production_turn_orchestration_caller():
    """프로덕션 턴 파이프라인에 CommitJournal 호출자가 0 인지 소스 스캔(J6/§14)."""
    from tests.conftest import source_of, REPO_ROOT

    # 중립 모듈 import 는 허용(배선 아님). 호출/구성 패턴만 금지한다.
    call_patterns = ("CommitJournal(", ".append_entry(", ".classify_recovery(",
                     "commit_journal.CommitJournal", "commit_journal.new_entry",
                     "commit_journal.append", "commit_journal.classify")

    offenders = []
    for base in ("core", "cogs"):
        root = os.path.join(REPO_ROOT, base)
        for dirpath, _dirs, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT)
                if rel.replace("\\", "/") == "core/commit_journal.py":
                    continue  # 정의 파일 자체는 제외
                text = source_of(rel)
                for pat in call_patterns:
                    if pat in text:
                        offenders.append((rel, pat))
    assert offenders == [], f"프로덕션 저널 호출자 발견: {offenders}"


def test_22_journal_module_does_not_touch_legacy_billing():
    """저널 모듈이 청구/계정/정산 모듈을 import 하거나 호출하지 않는다(레거시 청구 불변).

    분류기는 enum 만 반환하며(§9), 청구/세션변이/Settlement 접근을 하지 않는다.
    주의: settlement_id 는 §6.2 가 요구하는 필수 '필드명'이며 항상 None 을 허용한다.
    따라서 원시 문자열이 아니라 실제 import 문/청구 호출 패턴만 금지한다.
    """
    from tests.conftest import source_of
    src = source_of("core/commit_journal.py")

    # (a) 실제 import 문에 청구/계정/정산/원장/비용 모듈이 없어야 한다.
    import_lines = [ln.strip() for ln in src.splitlines()
                    if ln.strip().startswith(("import ", "from "))]
    for ln in import_lines:
        for mod in ("accounts", "ink", "cost", "settlement", "cost_ledger",
                    "session_flow", "quest", "extraction"):
            assert mod not in ln, f"저널 모듈이 {mod!r} 를 import 함: {ln!r}"

    # (b) 청구/차감/정산 '호출·구성' 패턴이 없어야 한다(필드명 settlement_id 는 무관).
    for call in ("Settlement(", "InkTransaction(", ".charge(", ".deduct(",
                 "deduct_ink", "apply_billing"):
        assert call not in src, f"저널 모듈에 청구 호출 패턴: {call!r}"

    # (c) 필드는 존재하나 항상 None 허용이며 청구 로직이 없다.
    e = _entry("SESSION_PERSISTED")
    assert e.settlement_id is None
