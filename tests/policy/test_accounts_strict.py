"""WP-ACCOUNTING-PREREQ-01 — strict account 경로 검증(핸드오프 §11, 17-30).

strict load/write, 실패 주입(직렬화/쓰기/fsync/replace), 이전 유효 파일 보존,
그리고 tolerant load 와 per-user 락 규율이 그대로 보존됨을 증명한다.
conftest 의 _isolated_cwd 로 accounts/ 는 tmp 작업디렉터리에 격리된다.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os

import pytest

from core import accounts
from tests.conftest import source_of

pytestmark = pytest.mark.policy


def _valid_account(uid="u1", **over):
    acc = accounts._blank_account(uid)
    acc["registered"] = True
    acc["ink_balance"] = 50
    acc.update(over)
    return acc


# ── 17-20. strict load ──────────────────────────────────────

def test_17_strict_load_preserves_values_and_default_fills(tmp_path):
    # 신규 필드가 빠진 과거 스키마라도 기본값으로 채워 로드한다.
    accounts._write_account(_valid_account("u1", ink_balance=77))
    # total_spent_ink 를 일부러 제거해 default-fill 을 확인.
    path = accounts._path("u1")
    data = json.loads(open(path, encoding="utf-8").read())
    data.pop("total_spent_ink", None)
    open(path, "w", encoding="utf-8").write(json.dumps(data))

    acc = accounts.load_account_strict("u1")
    assert acc["ink_balance"] == 77
    assert acc["registered"] is True
    assert acc["total_spent_ink"] == 0          # 기본값 채움
    assert acc["user_id"] == "u1"


def test_18_absent_file_keeps_blank_semantics(tmp_path):
    acc = accounts.load_account_strict("nobody")
    assert acc["registered"] is False
    assert acc["ink_balance"] == 0
    assert acc["user_id"] == "nobody"
    assert not os.path.exists(accounts._path("nobody"))   # 생성하지 않음


def test_19_malformed_json_raises_corruption(tmp_path):
    os.makedirs(accounts.ACCOUNTS_DIR, exist_ok=True)
    open(accounts._path("u1"), "w", encoding="utf-8").write("{not valid json")
    with pytest.raises(accounts.AccountCorruptionError):
        accounts.load_account_strict("u1")


def test_20_user_id_path_mismatch_is_rejected(tmp_path):
    os.makedirs(accounts.ACCOUNTS_DIR, exist_ok=True)
    open(accounts._path("u1"), "w", encoding="utf-8").write(
        json.dumps({"user_id": "SOMEONE_ELSE", "ink_balance": 1}))
    with pytest.raises(accounts.AccountCorruptionError):
        accounts.load_account_strict("u1")


# ── 21. strict write round-trip ─────────────────────────────

def test_21_strict_write_saves_reloadable_account(tmp_path):
    accounts._write_account_strict(_valid_account("u1", ink_balance=123))
    acc = accounts.load_account_strict("u1")
    assert acc["ink_balance"] == 123 and acc["registered"] is True


# ── 22-25. write failure injection ──────────────────────────

def test_22_serialization_failure_is_observable(tmp_path):
    bad = {"user_id": "u1", "ink_balance": {1, 2, 3}}   # set → JSON 직렬화 불가
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(bad)


def test_23_write_failure_is_observable(tmp_path, monkeypatch):
    real_open = builtins.open

    def failing_open(path, mode="r", *a, **k):
        if "w" in mode:
            raise OSError("디스크 꽉참(시뮬)")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(_valid_account("u1"))


def test_24_fsync_failure_is_observable(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync 실패")))
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(_valid_account("u1"))


def test_25_replace_failure_is_observable(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("replace 실패")))
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(_valid_account("u1"))


# ── 26-27. durability guarantees ────────────────────────────

def test_26_prior_valid_account_survives_failed_replace(tmp_path, monkeypatch):
    accounts._write_account_strict(_valid_account("u1", ink_balance=50))   # 이전 유효본
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("replace 실패")))
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(_valid_account("u1", ink_balance=999))
    # load_account_strict 는 읽기 전용이므로 replace 패치를 되돌릴 필요가 없다.
    acc = accounts.load_account_strict("u1")
    assert acc["ink_balance"] == 50            # 교체 실패 → 이전 값 보존


def test_27_temp_artifact_cleaned_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("replace 실패")))
    with pytest.raises(accounts.AccountPersistenceError):
        accounts._write_account_strict(_valid_account("u1"))
    assert not os.path.exists(accounts._path("u1") + ".tmp")


# ── 28. tolerant load 보존 ──────────────────────────────────

def test_28_tolerant_load_retains_fallback(tmp_path):
    os.makedirs(accounts.ACCOUNTS_DIR, exist_ok=True)
    open(accounts._path("u1"), "w", encoding="utf-8").write("{broken")
    # tolerant load 는 손상 시 빈 계정으로 대체하는 기존 계약을 유지(예외 없음).
    acc = accounts.load_account("u1")
    assert acc["registered"] is False and acc["ink_balance"] == 0


# ── 29-30. 레거시/락 보존 ───────────────────────────────────

def test_29_legacy_billing_callers_unchanged_and_no_strict_cutover():
    # (a) 레거시 금전 뮤테이터는 여전히 tolerant _write_account 를 쓴다.
    src = source_of("core/accounts.py")
    for fn in ("register_account", "add_ink", "set_balance", "deduct_ink"):
        assert f"def {fn}" in src
    assert "_write_account(acc)" in src            # tolerant 경로 유지
    # (b) 프로덕션(core/+cogs/, accounts.py 정의부 제외)에 strict 금전
    #     프리미티브 호출자가 0 이어야 한다(§12, live billing cutover 없음).
    from tests.conftest import REPO_ROOT
    strict_syms = ("_write_account_strict", "load_account_strict",
                   "record_cost_event_strict", "list_cost_events_strict",
                   "get_cost_events_by_ids_strict")
    offenders = []
    for base in ("core", "cogs"):
        for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT).replace("\\", "/")
                # 정의 파일 + WP-SETTLEMENT-01 이 인가한 재무 foundation 정의 모듈 제외.
                # settlement 빌더는 strict exact-ID 리더를 '설계상' 소비한다(핸드오프
                # §8/§40). 이는 라이브 청구 cutover 가 아니며 cogs/ 호출자는 0 이다
                # (별도 caller-scan 테스트가 이를 직접 증명한다).
                if rel in ("core/accounts.py", "core/cost_ledger.py",
                           "core/settlement.py", "core/ink_transactions.py",
                           # WP-D 권위 owner — foundation을 조합만 하며 strict 프리미티브는
                           # 원장 부재 봇용 _NullLedger 인터페이스 정의뿐이다.
                           "core/commit_coordinator.py"):
                    continue                        # 정의 파일 제외
                text = source_of(rel)
                for sym in strict_syms:
                    if sym + "(" in text:
                        offenders.append((rel, sym))
    assert offenders == [], f"strict 프리미티브 프로덕션 호출자 발견: {offenders}"


def test_30_per_user_locking_not_weakened():
    l1 = accounts._lock_for("u1")
    l2 = accounts._lock_for("u1")
    assert l1 is l2 and isinstance(l1, asyncio.Lock)      # 유저당 안정적 단일 락
    assert accounts._lock_for("u2") is not l1
    # 두 번째 락 시스템을 도입하지 않았다: Lock 생성지점은 _lock_for 하나뿐.
    assert source_of("core/accounts.py").count("asyncio.Lock()") == 1
