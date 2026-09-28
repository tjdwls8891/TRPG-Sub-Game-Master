"""WP-F — 캐시 생애주기 재무 (K-F01 ~ K-F16 + 동시성·실패 주입).

estimate / prepayment / provider create fact / provider storage fact / window settlement /
refund / additional charge 를 분리해 관측한다. 모든 종료 경로가 단일 finalizer를 지나며
재시도·재시작 replay 가 중복 재무 효과를 만들지 않음을 증명한다.
"""

from __future__ import annotations

import asyncio
import ast
import json
import os
import pathlib

import pytest

import core
from core import accounts
from core import cache_lifecycle as CLC
from core import cost_ledger as CL
from core import ink_transactions as IT
from tests.conftest import PLAYER_UID, REPO_ROOT

pytestmark = pytest.mark.policy

TOKENS = 26_268


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(CLC.time, "time", c)
    return c


@pytest.fixture
def cbot(fake_bot, monkeypatch):
    fake_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))

    async def _text(bot, model_id, scenario_data, cache_note="", session_id=None, session=None):
        return "룰북" * 10, TOKENS, "룰북"
    monkeypatch.setattr(core.cache, "build_scenario_cache_text", _text)
    return fake_bot


@pytest.fixture
def fresh(session_factory, cbot):
    s = session_factory(session_id="wpf-cache")
    s.players = {PLAYER_UID: {"name": "테스터", "profile": {}}}
    s.open_minutes = 180
    cbot.register_session(s)
    _seed(PLAYER_UID, 1000)
    return s


def _seed(uid, balance):
    acc = accounts._blank_account(uid)
    acc["registered"] = True
    acc["ink_balance"] = balance
    accounts._write_account_strict(acc)


def _bal(uid=PLAYER_UID):
    return accounts.load_account_strict(uid)["ink_balance"]


def _events(bot, op=None, sid=None):
    rows = bot.cost_ledger.list_cost_events_strict(session_id=sid)
    return [r for r in rows if op is None or r["operation"] == op]


def _ink_rows(uid=PLAYER_UID):
    led = IT.InkTransactionLedger(IT.default_ink_tx_ledger_path(uid), user_id=uid)
    return led.list_all()


def _journal(sid):
    return [e["type"] for e in CLC.load_events(sid)]


# ── K-F01 estimate ≠ provider fact ──────────────────────────

async def test_kf01_estimate_is_not_a_cost_event(fresh, cbot, clock):
    core.estimate_session_open(fresh, 3.0)
    CLC.preview_close(fresh)
    assert _events(cbot) == []
    assert not os.path.exists(CLC.journal_path(fresh.session_id))


# ── K-F02 prepayment ≠ provider cost / K-F04 successful create ──

async def test_kf02_kf04_open_records_prepayment_and_create_fact_separately(fresh, cbot, clock):
    res = await CLC.open_window(cbot, fresh)
    assert res["ok"] and res["prepaid"]
    per_user = res["charge_ink"]
    # 플레이어 재무 사실 — PREPAYMENT
    rows = _ink_rows()
    assert [r["kind"] for r in rows] == [IT.KIND_PREPAYMENT]
    assert rows[0]["nominal_ink"] == per_user and rows[0]["direction"] == "DEBIT"
    assert _bal() == 1000 - per_user
    # provider 사실 — 생성분만(계획 TTL 저장 없음)
    creates = _events(cbot, CL.OP_CACHE_CREATE)
    assert len(creates) == 1
    create_usd = core.cost.cache_create_cost_usd(core.DEFAULT_MODEL, tokens=TOKENS)
    assert creates[0]["cost_usd"] == pytest.approx(create_usd)
    assert creates[0]["transaction_id"] is None           # 턴 Settlement 에 claim 되지 않음
    assert _events(cbot, CL.OP_CACHE_STORAGE) == []
    assert fresh.total_usd == pytest.approx(create_usd)   # 레거시 누적도 실제 생성분만
    planned = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=TOKENS, store_hours=3.0)
    assert fresh.total_cost < planned                     # 예상(계획 TTL) 누적 없음
    # 한 번만
    again = await CLC.open_window(cbot, fresh)
    assert again.get("already")
    assert _journal(fresh.session_id).count("CREATED") == 1
    assert len(cbot.genai_client.caches.created) == 1


# ── K-F03 failed create ─────────────────────────────────────

async def test_kf03_failed_create_fabricates_nothing(fresh, cbot, clock):
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(RuntimeError):
        await CLC.open_window(cbot, fresh)
    assert _events(cbot) == []
    assert fresh.cache_name is None and fresh.total_cost == 0.0
    assert _bal() == 1000 and _ink_rows() == []
    assert _journal(fresh.session_id) == ["WINDOW_OPENED", "WINDOW_ABORTED"]


async def test_create_success_but_journal_failure_compensates(fresh, cbot, clock, monkeypatch):
    real = CLC._append

    def _fail_created(sid, ev):
        if ev.get("type") == "CREATED":
            raise CLC.CacheLifecycleError("disk full")
        return real(sid, ev)
    monkeypatch.setattr(CLC, "_append", _fail_created)
    with pytest.raises(CLC.CacheLifecycleError):
        await CLC.open_window(cbot, fresh)
    # 추적 불가 캐시를 남기지 않는다 — 보상 삭제, 메타데이터·재무 무변이
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]
    assert fresh.cache_name is None and _events(cbot) == [] and _bal() == 1000


# ── K-F05 storage / K-F11 refund / K-F06 repeated finalizer ─

async def test_kf05_kf11_kf06_close_settles_storage_and_refund_exactly_once(fresh, cbot, clock):
    res = await CLC.open_window(cbot, fresh)
    per_user = res["charge_ink"]
    clock.advance(3600)
    out = await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    storage = _events(cbot, CL.OP_CACHE_STORAGE)
    assert len(storage) == 1
    assert storage[0]["metadata"]["storage_seconds"] == pytest.approx(3600.0)
    assert storage[0]["cost_usd"] == pytest.approx(
        core.cost.cache_storage_cost_usd(core.DEFAULT_MODEL, tokens=TOKENS, seconds=3600))
    used_ink = core.cost_to_ink(core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=TOKENS, store_hours=1.0))
    expect_refund = per_user - used_ink
    assert expect_refund > 0
    assert out["refund"] == {PLAYER_UID: expect_refund}
    assert _bal() == 1000 - per_user + expect_refund
    assert fresh.cache_name is None and fresh.cache_created_at == 0.0
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]
    # 반복 호출 — 추가 provider/재무 효과 없음
    await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1
    assert [r["kind"] for r in _ink_rows()] == [IT.KIND_PREPAYMENT, IT.KIND_REFUND]
    assert _bal() == 1000 - per_user + expect_refund
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]


# ── K-F07 remote NotFound ───────────────────────────────────

async def test_kf07_remote_not_found_still_finalizes(fresh, cbot, clock, monkeypatch):
    from google.genai.errors import APIError
    await CLC.open_window(cbot, fresh)
    clock.advance(600)

    def _gone(name=None, **_k):
        raise APIError(404, {"error": {"message": "not found", "status": "NOT_FOUND"}})
    monkeypatch.setattr(cbot.genai_client.caches, "delete", _gone)
    out = await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    assert out["remote"] == CLC.REMOTE_GONE
    st = _events(cbot, CL.OP_CACHE_STORAGE)
    assert len(st) == 1 and st[0]["metadata"]["storage_seconds"] == pytest.approx(600.0)
    assert st[0]["usage_source"] == CL.SOURCE_FIXED_PROVIDER_PRICING
    assert fresh.cache_name is None


async def test_remote_delete_failure_bills_storage_to_expiry_as_estimate(fresh, cbot, clock, monkeypatch):
    await CLC.open_window(cbot, fresh)
    clock.advance(600)

    def _boom(name=None, **_k):
        raise RuntimeError("503 unavailable")
    monkeypatch.setattr(cbot.genai_client.caches, "delete", _boom)
    out = await CLC.close_window(cbot, fresh, reason=CLC.REASON_OPERATOR_DELETE,
                                 disposition=CLC.WINDOW_NO_PLAYER_EFFECT)
    assert out["remote"] == CLC.REMOTE_FAILED
    st = _events(cbot, CL.OP_CACHE_STORAGE)[0]
    assert st["usage_source"] == CL.SOURCE_ESTIMATE
    assert st["metadata"]["storage_seconds"] == pytest.approx(180 * 60)


# ── K-F08 remote delete OK + local failure → replay ─────────

async def test_kf08_remote_deleted_then_local_failure_replays_once(fresh, cbot, clock, monkeypatch):
    res = await CLC.open_window(cbot, fresh)
    per_user = res["charge_ink"]
    clock.advance(1800)
    real = CLC._record_cost
    calls = {"n": 0}

    def _flaky(bot, session, **kw):
        if kw["operation"] == CL.OP_CACHE_STORAGE and calls["n"] == 0:
            calls["n"] += 1
            raise CLC.CacheLifecycleError("ledger fsync failed")
        return real(bot, session, **kw)
    monkeypatch.setattr(CLC, "_record_cost", _flaky)
    with pytest.raises(CLC.CacheLifecycleError):
        await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                               disposition=CLC.WINDOW_SETTLE_REFUND)
    assert fresh.cache_name == "caches/fake-1"            # 아직 종료되지 않음
    assert _journal(fresh.session_id)[-2:] == ["FINALIZE_INTENT", "REMOTE_RESULT"]
    clock.advance(9999)                                    # 재시도는 고정된 closed_at 을 쓴다
    await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    st = _events(cbot, CL.OP_CACHE_STORAGE)
    assert len(st) == 1 and st[0]["metadata"]["storage_seconds"] == pytest.approx(1800.0)
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]   # 재삭제 없음
    refunds = [r for r in _ink_rows() if r["kind"] == IT.KIND_REFUND]
    assert len(refunds) == 1
    used = core.cost_to_ink(core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=TOKENS, store_hours=0.5))
    assert refunds[0]["nominal_ink"] == per_user - used


async def test_refund_account_failure_then_restart_replay(fresh, cbot, clock, monkeypatch):
    await CLC.open_window(cbot, fresh)
    clock.advance(600)
    real = accounts.apply_ink_adjustment_strict
    state = {"fail": True}

    async def _flaky(uid, **kw):
        if state["fail"] and kw["kind"] == IT.KIND_REFUND:
            raise accounts.AccountPersistenceError("disk")
        return await real(uid, **kw)
    monkeypatch.setattr(accounts, "apply_ink_adjustment_strict", _flaky)
    out = await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    assert out["settled"] is False
    assert "WINDOW_SETTLED" not in _journal(fresh.session_id)
    state["fail"] = False
    await CLC.restore(cbot, fresh)                          # 재시작 복구가 정산 의도를 재개
    assert "WINDOW_SETTLED" in _journal(fresh.session_id)
    assert len([r for r in _ink_rows() if r["kind"] == IT.KIND_REFUND]) == 1
    await CLC.restore(cbot, fresh)
    assert len([r for r in _ink_rows() if r["kind"] == IT.KIND_REFUND]) == 1


# ── K-F09 reissue ───────────────────────────────────────────

async def test_kf09_reissue_finalizes_old_once_and_keeps_window(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    window_start = fresh.cache_created_at
    clock.advance(1200)
    r = await CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_AUTO)
    assert r["old"]["cache_name"] == "caches/fake-1" and r["cache_name"] == "caches/fake-2"
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]
    # 창 시작 유지 — 남은 시간만 새 캐시 TTL
    assert fresh.cache_created_at == window_start
    assert cbot.genai_client.caches.created[1]["config"].ttl == f"{180 * 60 - 1200}s"
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1
    assert len(_events(cbot, CL.OP_CACHE_CREATE)) == 2
    auto = _events(cbot, CL.OP_CACHE_CREATE)[1]
    assert (auto["actor_kind"], auto["billing_hint"]) == (CL.ACTOR_SYSTEM, CL.HINT_SYSTEM)
    # 재발급은 플레이어 재무 효과 없음
    assert [r["kind"] for r in _ink_rows()] == [IT.KIND_PREPAYMENT]


async def test_reissue_create_failure_closes_window_with_refund(fresh, cbot, clock):
    res = await CLC.open_window(cbot, fresh)
    clock.advance(600)
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(RuntimeError):
        await CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_AUTO)
    assert fresh.cache_name is None
    assert len(_events(cbot, CL.OP_CACHE_CREATE)) == 1        # 실패한 생성 사실 없음
    assert "WINDOW_SETTLED" in _journal(fresh.session_id)
    assert [r["kind"] for r in _ink_rows()] == [IT.KIND_PREPAYMENT, IT.KIND_REFUND]
    assert _bal() > 1000 - res["charge_ink"]


# ── K-F10 recovery ──────────────────────────────────────────

async def test_kf10_recovery_recreate_records_only_after_success(fresh, cbot, clock, monkeypatch):
    from google.genai.errors import APIError
    await CLC.open_window(cbot, fresh)
    clock.advance(600)

    def _missing(name=None, **_k):
        raise APIError(404, {"error": {"message": "expired", "status": "NOT_FOUND"}})
    monkeypatch.setattr(cbot.genai_client.caches, "get", _missing)
    cbot.genai_client.caches.fail_create = True
    out = await CLC.restore(cbot, fresh)
    assert out["action"] == "RECOVERY_FAILED"
    assert _events(cbot, CL.OP_CACHE_RECOVERY_CREATE) == []
    assert fresh.cache_name is None


async def test_kf10b_recovery_recreate_success(fresh, cbot, clock, monkeypatch):
    from google.genai.errors import APIError
    await CLC.open_window(cbot, fresh)
    clock.advance(600)
    monkeypatch.setattr(cbot.genai_client.caches, "get",
                        lambda name=None, **_k: (_ for _ in ()).throw(
                            APIError(404, {"error": {"message": "gone"}})))
    out = await CLC.restore(cbot, fresh)
    assert out["action"] == "RECREATED"
    assert len(_events(cbot, CL.OP_CACHE_RECOVERY_CREATE)) == 1
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1        # 이전 생애주기 한 번
    assert fresh.cache_name == "caches/fake-2"
    assert cbot.genai_client.caches.deleted == []              # 이미 없는 캐시는 삭제 호출 안 함


async def test_restore_expired_window_finalizes_without_recreate(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    clock.advance(4 * 3600)
    fresh.cache_created_at = clock.t - 4 * 3600
    out = await CLC.restore(cbot, fresh)
    assert out["action"] == "EXPIRED"
    assert len(cbot.genai_client.caches.created) == 1
    st = _events(cbot, CL.OP_CACHE_STORAGE)
    assert st[0]["metadata"]["storage_seconds"] == pytest.approx(180 * 60)  # TTL 상한


# ── K-F12 additional charge ─────────────────────────────────

async def test_kf12_shortfall_is_operator_borne_no_additional_charge(fresh, cbot, clock, monkeypatch):
    await CLC.open_window(cbot, fresh)
    # 사용분이 선불을 넘는 상황(예: 선불액 산정 이후 단가 변화)을 저널 값으로 재현
    evs = CLC.load_events(fresh.session_id)
    for e in evs:
        if e["type"] == "WINDOW_OPENED":
            e["cache_ink"] = 1
    with open(CLC.journal_path(fresh.session_id), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs))
    clock.advance(3600)
    await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    intent = [e for e in CLC.load_events(fresh.session_id) if e["type"] == "WINDOW_SETTLE_INTENT"][0]
    assert intent["refund"] == {}
    assert intent["operator_borne_shortfall"][PLAYER_UID] > 0
    assert [r["kind"] for r in _ink_rows()] == [IT.KIND_PREPAYMENT]   # ADDITIONAL_CHARGE 없음


# ── K-F13 operator actions ─────────────────────────────────

async def test_kf13_operator_actions_have_no_player_effect(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    bal_after_open = _bal()
    await CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_MANUAL)
    ev = _events(cbot, CL.OP_CACHE_CREATE)[-1]
    assert (ev["actor_kind"], ev["billing_hint"]) == (CL.ACTOR_OWNER, CL.HINT_OPERATOR)
    assert ev["transaction_id"] is None and ev["logical_turn"] is None
    clock.advance(600)
    await CLC.close_window(cbot, fresh, reason=CLC.REASON_OPERATOR_DELETE,
                           disposition=CLC.WINDOW_NO_PLAYER_EFFECT)
    assert _bal() == bal_after_open
    assert [r["kind"] for r in _ink_rows()] == [IT.KIND_PREPAYMENT]
    assert fresh.cache_name is None and fresh.cache_created_at == 0.0


# ── 환급 대상 · 해석 비용 정정 ─────────────────────────────

async def test_refund_excludes_interpretation_and_non_payers(fresh, cbot, clock):
    fresh.interpret_cost_krw = 50.0
    interp = core.cost_to_ink(50.0)                          # 임계(2잉크) 이상 → 선불에 합산
    res = await CLC.open_window(cbot, fresh)
    assert res["interpret_ink"] == interp
    per_user = res["charge_ink"]
    late = "555999"
    _seed(late, 100)
    fresh.players[late] = {"name": "늦참", "profile": {}}     # 오픈 후 합류(선불 안 함)
    out = await CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    cache_ink = per_user - interp
    used = core.cost_to_ink(core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=TOKENS))
    assert out["refund"] == {PLAYER_UID: cache_ink - used}
    assert _bal(late) == 100


# ── 레거시 채택 (WP-F 이전에 열린 캐시) ───────────────────

async def test_legacy_open_session_is_adopted_and_settled(session_auto_ready, cbot, clock):
    s = session_auto_ready
    cbot.register_session(s)
    s.cache_created_at = clock.t - 3600
    s.open_prepaid_ink = 50
    _seed(PLAYER_UID, 10)
    out = await CLC.close_window(cbot, s, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    used = core.cost_to_ink(core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=1024, store_hours=1.0))
    assert out["refund"] == {PLAYER_UID: 50 - used}
    assert _bal() == 10 + 50 - used
    assert _events(cbot, CL.OP_CACHE_CREATE) == []           # 레거시 생성분 재기록 없음
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1


# ── K-F15 provenance ────────────────────────────────────────

async def test_kf15_new_cache_receives_history_marker(fresh, cbot, clock):
    fresh.commit_marker = {"transaction_id": "tx-9", "attempt": 1}
    fresh.gm_turns_done = 4
    await CLC.open_window(cbot, fresh)
    fresh.cache_history_stale = True                          # 되감기로 무효화된 상태
    assert not core.turn_history.cache_usable(fresh)
    await CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_AUTO)
    assert fresh.cache_history_marker == core.turn_history.cache_marker_now(fresh)
    assert fresh.cache_history_stale is False and core.turn_history.cache_usable(fresh)


async def test_kf15b_failed_reissue_keeps_stale_cache_unusable(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    fresh.cache_history_stale = True
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(RuntimeError):
        await CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_AUTO)
    assert not core.turn_history.cache_usable(fresh)


# ── 동시성 ─────────────────────────────────────────────────

async def test_reissue_racing_close_is_serialized(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    clock.advance(300)
    await asyncio.gather(
        CLC.reissue(cbot, fresh, purpose=CLC.PURPOSE_REISSUE_AUTO),
        CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                         disposition=CLC.WINDOW_SETTLE_REFUND))
    ev = CLC.load_events(fresh.session_id)
    created = [e["cache_name"] for e in ev if e["type"] == "CREATED"]
    finalized = [e["cache_name"] for e in ev if e["type"] == "FINALIZED"]
    assert sorted(created) == sorted(finalized)               # 모든 생애주기 정확히 한 번 종료
    assert len(finalized) == len(set(finalized))
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == len(created)
    assert fresh.cache_name is None
    assert [e["type"] for e in ev].count("WINDOW_SETTLED") == 1
    assert len([r for r in _ink_rows() if r["kind"] == IT.KIND_REFUND]) <= 1


async def test_concurrent_close_calls_single_financial_result(fresh, cbot, clock):
    await CLC.open_window(cbot, fresh)
    clock.advance(300)
    await asyncio.gather(*[CLC.close_window(cbot, fresh, reason=CLC.REASON_PLAYER_CLOSE,
                                            disposition=CLC.WINDOW_SETTLE_REFUND)
                           for _ in range(3)])
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1
    assert len([r for r in _ink_rows() if r["kind"] == IT.KIND_REFUND]) == 1
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]


# ── 만료 경로 (presence) ───────────────────────────────────

async def test_expiry_path_reaches_finalizer_and_allows_reopen(fresh, cbot, clock):
    from cogs.presence import PresenceCog
    await CLC.open_window(cbot, fresh)
    fresh.cache_created_at = clock.t - 181 * 60              # 창 만료
    cog = PresenceCog.__new__(PresenceCog)
    cog.bot = cbot
    await cog._check_expired()
    assert fresh.cache_name is None                           # 재오픈 가능
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1
    assert "WINDOW_SETTLED" in _journal(fresh.session_id)
    emb = core.display.build_embed(fresh)
    assert "만료" in emb.description


# ── K-F14 직접 provider 호출/재무 우회 스캔 ────────────────

def _calls(path):
    tree = ast.parse(pathlib.Path(REPO_ROOT, path).read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("create", "delete", "get") \
                and isinstance(node.value, ast.Attribute) and node.value.attr == "caches":
            out.append(node.attr)
    return out


def test_kf14_no_lifecycle_bypassing_provider_cache_calls():
    offenders = {}
    for base in ("core", "cogs"):
        for p in sorted(pathlib.Path(REPO_ROOT, base).glob("*.py")):
            rel = p.relative_to(REPO_ROOT).as_posix()
            if rel == "core/cache_lifecycle.py":
                continue
            c = _calls(rel)
            if c:
                offenders[rel] = c
    assert offenders == {}
    # process_cache_deletion 호환 래퍼는 프로덕션 호출자가 없다(테스트 전용 호환 입구).
    for base in ("cogs",):
        for p in pathlib.Path(REPO_ROOT, base).glob("*.py"):
            assert "process_cache_deletion(" not in p.read_text(encoding="utf-8"), p
    # 캐시 오픈/닫기 경로에 ad hoc 잔액 쓰기가 없다.
    for rel in ("cogs/session.py", "core/display.py", "core/cache_lifecycle.py"):
        src = pathlib.Path(REPO_ROOT, rel).read_text(encoding="utf-8")
        assert "deduct_ink(" not in src and "add_ink(" not in src, rel


def test_storage_helper_units_are_explicit():
    # PF-11: 캐시 생애주기는 단위가 이름에 드러난 헬퍼만 쓴다.
    src = pathlib.Path(REPO_ROOT, "core/cache_lifecycle.py").read_text(encoding="utf-8")
    assert "calculate_storage_cost(" not in src and "calculate_storage_cost_usd(" not in src
    h1 = core.cost.cache_storage_cost_usd(core.DEFAULT_MODEL, tokens=TOKENS, seconds=3600)
    assert h1 == pytest.approx(core.calculate_storage_cost_usd(core.DEFAULT_MODEL, TOKENS, 1.0))
