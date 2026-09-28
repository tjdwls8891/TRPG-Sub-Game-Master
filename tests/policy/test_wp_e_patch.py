"""WP-E gate patch — E-E1(선택 시점) · E-E2(출력 정리 부채) · E-E3(캐시 이력 출처).

실제 자동 경로(CommitCoordinator)와 재시작(restore_sessions_from_disk)을 쓴다.
"""

from __future__ import annotations

import json
import os

import pytest

import core
from core import commit_coordinator as CC
from core import turn_history as TH
from core import turn_transaction as tt
from tests.conftest import GAME_CH, PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_authoritative_commit import (
    _balance, _disk, _fund, _ink_rows, _phases, _store,
)
from tests.policy.test_commit_recovery import _sid
from tests.policy.test_ready_barrier import _usage, rig  # noqa: F401 — 픽스처 재사용
from tests.policy.test_turn_history import _commit, _fake_logic, _hv, _loc

pytestmark = pytest.mark.policy


@pytest.fixture
def inject():
    """고장 주입 전용 MonkeyPatch. undo()가 rig/cwd 격리를 되돌리지 않게 분리한다."""
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture
def select_timing(rig):
    """모든 SELECT append 시점에 그 시도의 durable COMMITTED가 있었는지 기록한다."""
    seen = []
    orig = TH._append_event

    def _spy(sid, ev):
        if ev.get("type") == "SELECT":
            seen.append((ev["transaction_id"], TH._journal_committed(sid, ev["transaction_id"])))
        return orig(sid, ev)
    mp = pytest.MonkeyPatch()
    mp.setattr(TH, "_append_event", _spy)
    yield seen
    mp.undo()


async def _restart_with_channels(r):
    """재시작 모사 — 같은 Discord 채널(메시지 보존)을 새 봇에 등록한 뒤 복원한다."""
    s = r.sess
    os.makedirs("scenarios", exist_ok=True)
    with open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8") as f:
        json.dump(s.scenario_data, f, ensure_ascii=False)
    bot2 = FakeBot(strict=False)
    bot2._channels = dict(r.bot._channels)
    bot2.cost_ledger = core.cost_ledger.CostLedger(r.ledger.path)
    await core.restore_sessions_from_disk(bot2)
    s2 = bot2.active_sessions[GAME_CH]
    assert s2 is not s and tt.get_active_transaction(s2) is None
    return bot2, s2


def _selects(s, tx_id):
    return [e for e in TH.load_events(s.session_id)
            if e["type"] == "SELECT" and e["transaction_id"] == tx_id]


def _fail_charges_once(inject):
    orig = core.ink_transactions.execute_settlement_charges
    state = {"n": 0}

    async def _bad(settlement, **k):
        if state["n"] == 0:                         # 주입 이후 첫 청구(= 교체 시도)만 실패
            state["n"] += 1
            raise OSError("계정 쓰기 실패(청구 단계 모사)")
        return await orig(settlement, **k)
    inject.setattr(core.ink_transactions, "execute_settlement_charges", _bad)
    return state


# ══════════════════════════════════════════════════════════════
# E-E1 — SELECT는 durable COMMITTED 이후에만
# ══════════════════════════════════════════════════════════════

async def test_ee1_billing_failure_keeps_old_selected_until_inprocess_recovery(
        rig, inject, select_timing):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    st_old = _store(s).get_settlement_strict(_sid(tx_old))
    _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("숲 속 오두막")]
    _fail_charges_once(inject)

    await r.gm.rerender_latest(s)
    new_tx = TH.read_op(s.session_id)["new_tx"]
    # 영속 이후 청구 실패 — 레코드는 durable, 선택은 여전히 이전 시도
    assert os.path.exists(TH.record_path(s.session_id, new_tx))
    hv = _hv(s)
    assert hv.head_entry()["transaction_id"] == tx_old.transaction_id
    assert new_tx not in hv.all_attempts and _selects(s, new_tx) == []
    assert TH.read_op(s.session_id)["op"] == "RERENDER"
    ph = [e.phase.value for e in CC.journal_for(s.session_id).history_for_transaction(new_tx)]
    assert "SESSION_PERSISTED" in ph and "COMMITTED" not in ph
    assert s.commit_recovery is not None
    # 재생성·되감기 모두 거부(복구 대기)
    assert TH.rerender_target(s)[0] is None

    # in-process 복구 → BILLING_APPLIED/COMMITTED 완료 → 정확히 그때 SELECT 1회
    assert await r.gm._resume_commit_recovery(s, r.master)
    ph = [e.phase.value for e in CC.journal_for(s.session_id).history_for_transaction(new_tx)]
    assert ph.count("COMMITTED") == 1
    assert len(_selects(s, new_tx)) == 1
    head = _hv(s).head_entry()
    assert head["transaction_id"] == new_tx and head["supersedes"] == tx_old.transaction_id
    assert head["record"] and head["attempt"] == 2
    assert TH.read_op(s.session_id) is None and s.commit_recovery is None
    assert s.world_timeline["current_location"] == "숲 속 오두막"
    # 재무: 이전 사실 불변 + 교체 청구 1건
    assert _store(s).get_settlement_strict(_sid(tx_old)) == st_old
    assert len(_ink_rows(PLAYER_UID)) == 2
    assert select_timing and all(ok for _, ok in select_timing), select_timing


async def test_ee1_billing_failure_then_restart_selects_exactly_once(rig, inject, select_timing):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("숲 속 오두막")]
    _fail_charges_once(inject)
    await r.gm.rerender_latest(s)
    new_tx = TH.read_op(s.session_id)["new_tx"]
    assert _hv(s).head_entry()["transaction_id"] == tx_old.transaction_id
    inject.undo()

    bot2, s2 = await _restart_with_channels(r)
    assert s2.commit_recovery is None
    assert len(_selects(s2, new_tx)) == 1
    head = _hv(s2).head_entry()
    assert head["transaction_id"] == new_tx and head["record"]
    assert s2.commit_marker["transaction_id"] == new_tx
    assert s2.world_timeline["current_location"] == "숲 속 오두막"
    assert TH.read_op(s2.session_id) is None
    assert len(_ink_rows(PLAYER_UID)) == 2
    assert all(ok for _, ok in select_timing), select_timing


async def test_ee1_crash_after_committed_before_select_restart_selects_once(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    st_old = _store(s).get_settlement_strict(_sid(tx_old))
    rows_before = _ink_rows(PLAYER_UID)
    _fake_logic(r, inject)

    def _crash(session, plan):
        raise TH.HistoryError("SELECT append 직전 크래시(모사)")
    inject.setattr(TH, "select_committed", _crash)
    await r.gm.rerender_latest(s)
    new_tx = TH.read_op(s.session_id)["new_tx"]
    ph = [e.phase.value for e in CC.journal_for(s.session_id).history_for_transaction(new_tx)]
    assert "COMMITTED" in ph and _selects(s, new_tx) == []
    inject.undo()

    bot2, s2 = await _restart_with_channels(r)
    assert len(_selects(s2, new_tx)) == 1
    assert _hv(s2).head_entry()["transaction_id"] == new_tx
    assert s2.commit_recovery is None and TH.read_op(s2.session_id) is None
    # 재무 사실 불변(이전) + 교체 1건, 재시작이 재청구하지 않음
    assert _store(s2).get_settlement_strict(_sid(tx_old)) == st_old
    rows = _ink_rows(PLAYER_UID)
    assert rows[:len(rows_before)] == rows_before and len(rows) == len(rows_before) + 1
    # 재시작 재정합 1회 뒤 다시 정합해도 SELECT가 늘지 않는다
    assert (await TH.reconcile(bot2, s2))["ok"]
    assert len(_selects(s2, new_tx)) == 1


async def test_ee1_replacement_failed_pre_persist_keeps_old_selection(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    rec_old = TH.load_record(s.session_id, tx_old.transaction_id)
    rows_before = _ink_rows(PLAYER_UID)
    _fake_logic(r, inject)
    orig = core.io.write_session_strict_locked
    calls = {"n": 0}

    async def _strict(session):
        calls["n"] += 1
        if calls["n"] == 2:                         # 1=begin_rerender, 2=교체 커밋 strict 저장
            raise OSError("커밋 strict 저장 실패(모사)")
        return await orig(session)
    inject.setattr(core.io, "write_session_strict_locked", _strict)
    await r.gm.rerender_latest(s)
    hv = _hv(s)
    assert hv.head_entry()["transaction_id"] == tx_old.transaction_id
    assert [e for e in hv.all_attempts.values() if e["attempt"] == 2] == []
    assert TH.read_op(s.session_id) is None
    assert TH.capture_reversible(s)["commit_marker"] == rec_old["post"]["commit_marker"]
    assert _disk(s)["commit_marker"]["transaction_id"] == tx_old.transaction_id
    assert _ink_rows(PLAYER_UID) == rows_before


async def test_ee1_select_committed_refuses_uncommitted_attempt(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    plan = type("P", (), dict(transaction_id="not-committed", logical_turn=9, attempt=1,
                              settlement_id="x", story_turn=9, gm_turn=2, fingerprint="f",
                              player_declaration="", canonical_message_ids=(),
                              media_message_ids=()))()
    with pytest.raises(TH.HistoryError):
        TH.select_committed(s, plan)
    assert "not-committed" not in _hv(s).all_attempts


# ══════════════════════════════════════════════════════════════
# E-E2 — durable 출력 정리 부채
# ══════════════════════════════════════════════════════════════

async def _no_drain(*a, **k):
    return 0


async def test_ee2_rerender_crash_before_old_output_cleanup_restart_resumes(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx_old = await _commit(r, "숲길")
    player_msg = await r.gch.send("플레이어: 숲길로 간다")        # 플레이어 선언(봇 출력 아님)
    old_ids = set(TH.attempt_message_ids(s.session_id, _hv(s).head_entry()))
    assert old_ids and player_msg.id not in old_ids
    _fake_logic(r, inject)
    inject.setattr(r.gm, "_drain_history_cleanup", _no_drain)     # 삭제 직전 하드 크래시 모사
    await r.gm.rerender_latest(s)
    head = _hv(s).head_entry()
    assert head["supersedes"] == tx_old.transaction_id
    debts = TH.pending_cleanups(s.session_id)
    assert len(debts) == 1 and debts[0]["reason"] == "RERENDER_SUPERSEDE"
    assert set(debts[0]["message_ids"]) == old_ids
    assert not any(m.deleted for m in r.gch.sent if m.id in old_ids)
    new_ids = set(TH.attempt_message_ids(s.session_id, head))
    inject.undo()

    bot2, s2 = await _restart_with_channels(r)
    assert TH.pending_cleanups(s2.session_id) == []
    assert all(m.deleted for m in r.gch.sent if m.id in old_ids)
    assert not any(m.deleted for m in r.gch.sent if m.id in new_ids)
    assert not player_msg.deleted


async def test_ee2_rewind_crash_before_cleanup_restart_resumes(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    await _commit(r, "폭포", "폭포로 간다")
    hv = _hv(s)
    keep = set(TH.attempt_message_ids(s.session_id, hv.selected[1], hv))
    gone = set(TH.attempt_message_ids(s.session_id, hv.selected[2], hv)) | set(
        TH.attempt_message_ids(s.session_id, hv.selected[3], hv))
    inject.setattr(r.gm, "_drain_history_cleanup", _no_drain)
    assert (await r.gm.history_rewind(s, 1))["ok"]
    debts = TH.pending_cleanups(s.session_id)
    assert len(debts) == 1 and debts[0]["reason"] == "REWIND"
    assert set(debts[0]["message_ids"]) == gone
    assert not any(m.deleted for m in r.gch.sent if m.id in gone)
    inject.undo()

    bot2, s2 = await _restart_with_channels(r)
    assert TH.pending_cleanups(s2.session_id) == []
    assert all(m.deleted for m in r.gch.sent if m.id in gone)
    assert not any(m.deleted for m in r.gch.sent if m.id in keep)


async def test_ee2_rewind_crash_before_event_restart_finalizes_debt_and_cleans(rig, inject):
    """REWIND 이벤트 이전 크래시 — 의도(op)의 부채가 재정합으로 이벤트에 실려 실행된다."""
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    gone = set(TH.attempt_message_ids(s.session_id, _hv(s).selected[2]))
    orig = TH._append_event

    def _bad(sid, ev):
        if ev.get("type") == "REWIND":
            raise TH.HistoryError("REWIND append 실패(크래시 모사)")
        return orig(sid, ev)
    inject.setattr(TH, "_append_event", _bad)
    assert (await TH.rewind(r.bot, s, 1))["ok"]
    inject.undo()
    bot2, s2 = await _restart_with_channels(r)
    assert _hv(s2).head == 1 and TH.pending_cleanups(s2.session_id) == []
    assert all(m.deleted for m in r.gch.sent if m.id in gone)


async def test_ee2_partial_failures_retry_idempotently_and_never_touch_current(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    hv = _hv(s)
    cur_ids = TH.attempt_message_ids(s.session_id, hv.selected[1], hv)
    doomed = TH.attempt_message_ids(s.session_id, hv.selected[2], hv)
    assert len(doomed) >= 1
    # 부채에 현재 선택 시도 ID가 섞여 있어도(스테일 부채) 삭제되면 안 된다
    stale_debt = {"op_id": "stale-op", "reason": "REWIND", "transaction_ids": ["x"],
                  "message_ids": [cur_ids[0]], "unmapped": False}
    TH._append_event(s.session_id, {"type": "REWIND", "op_id": "noop",
                                    "target_gm_turn": 99, "removed": [],
                                    "cleanup": stale_debt})
    inject.setattr(r.gm, "_drain_history_cleanup", _no_drain)
    assert (await r.gm.history_rewind(s, 1))["ok"]
    inject.undo()
    # 하나는 이미 사라짐(NotFound = 성공), 하나는 일시 fetch 실패(부채 유지)
    already = next(m for m in r.gch.sent if m.id == doomed[0])
    already.deleted = True
    flaky = doomed[-1] if len(doomed) > 1 else None
    orig_fetch = r.gch.fetch_message

    async def _fetch(mid):
        if flaky is not None and mid == flaky:
            raise RuntimeError("일시 오류")
        return await orig_fetch(mid)
    if flaky is not None:
        inject.setattr(r.gch, "fetch_message", _fetch)
        await r.gm._drain_history_cleanup(s)
        assert any(d["reason"] == "REWIND" and d["op_id"] != "stale-op"
                   for d in TH.pending_cleanups(s.session_id)), "실패한 부채는 남아야 합니다"
        inject.undo()
    await r.gm._drain_history_cleanup(s)
    assert TH.pending_cleanups(s.session_id) == []
    assert all(m.deleted for m in r.gch.sent if m.id in set(doomed))
    assert not any(m.deleted for m in r.gch.sent if m.id in set(cur_ids)), \
        "현재 선택 시도 출력은 어떤 부채로도 삭제되면 안 됩니다"
    before = sum(m.delete_count for m in r.gch.sent)
    await r.gm._drain_history_cleanup(s)
    assert sum(m.delete_count for m in r.gch.sent) == before


async def test_ee2_derived_message_mapping_failure_is_not_silently_lost(rig, inject):
    r = rig
    s = r.sess
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    tx2_id = _hv(s).selected[2]["transaction_id"]
    orig = TH._append_event

    def _bad(sid, ev):
        if ev.get("type") == "MESSAGES":
            raise TH.HistoryError("MESSAGES append 실패(모사)")
        return orig(sid, ev)
    inject.setattr(TH, "_append_event", _bad)
    d = CC.DerivedEffects()
    d.transaction_id = tx2_id
    d.game("📜 퀘스트 알림(파생 정본 출력)")
    await r.gm._emit_commit_derived(s, d)
    inject.undo()
    sent = r.gch.sent[-1]
    # 인덱스엔 없지만 대체 파일 + 런타임 차단으로 durable 보전
    pend = json.load(open(TH.pending_messages_path(s.session_id), encoding="utf-8"))
    assert any(sent.id in b["message_ids"] for b in pend.values())
    assert TH.needs_reconcile(s) and s.commit_recovery is None     # 게임 진행은 막지 않음
    res = await TH.rewind(r.bot, s, 1)
    assert not res["ok"] and "매핑" in res["reason"], "매핑 재정합 전 이력 조작 금지"
    # 재시작해도 대체 파일은 durable — 재정합이 인덱스로 옮긴다
    bot2, s2 = await _restart_with_channels(r)
    assert not TH.needs_reconcile(s2)
    assert not os.path.exists(TH.pending_messages_path(s2.session_id))
    assert sent.id in TH.attempt_message_ids(s2.session_id, _hv(s2).selected[2])
    r.gm.bot = bot2                                  # 재시작 후 봇으로 되감기
    assert (await r.gm.history_rewind(s2, 1))["ok"]
    assert sent.deleted and tx1


async def test_ee2_mapping_total_failure_blocks_with_payload_then_recovers(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    tx_id = _hv(s).selected[1]["transaction_id"]
    orig = TH._append_event

    def _bad(sid, ev):
        if ev.get("type") == "MESSAGES":
            raise TH.HistoryError("MESSAGES append 실패(모사)")
        return orig(sid, ev)

    def _bad_write(path, obj):
        raise TH.HistoryError("대체 파일 쓰기 실패(모사)")
    inject.setattr(TH, "_append_event", _bad)
    inject.setattr(TH, "_write_json_strict", _bad_write)
    d = CC.DerivedEffects()
    d.transaction_id = tx_id
    d.game("파생 알림")
    await r.gm._emit_commit_derived(s, d)
    inject.undo()
    assert s.commit_recovery is None
    assert any(r.gch.sent[-1].id in p["message_ids"] for p in s._history_unmapped_payload)
    assert TH.needs_reconcile(s) and not TH.rerender_target(s)[0]
    # 다음 입력 admission이 페이로드를 인덱스로 옮긴다
    await r.gm._recovery_admission(s)
    assert not TH.needs_reconcile(s)
    assert r.gch.sent[-1].id in TH.attempt_message_ids(s.session_id, _hv(s).selected[1])


async def test_ee2_emit_begin_without_mapping_surfaces_unmapped_debt(rig):
    """송출 의도(BEGIN)만 있고 매핑이 없는 시도(송출~매핑 사이 크래시)를 되감으면
    부채가 unmapped로 드러나 마스터에 알린다(조용한 유실 없음)."""
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    tx2 = _hv(s).selected[2]["transaction_id"]
    TH.begin_emit(s.session_id, tx2)
    assert (await r.gm.history_rewind(s, 1))["ok"]
    rewinds = [e for e in TH.load_events(s.session_id) if e["type"] == "REWIND"]
    assert rewinds[-1]["cleanup"]["unmapped"] is True
    assert any("매핑 기록이 없어" in (m.content or "") for m in r.master.sent)


# ══════════════════════════════════════════════════════════════
# E-E3 — 캐시 이력 출처
# ══════════════════════════════════════════════════════════════

def _capture_calls(r, inject, *, logic=None):
    """provider 호출 kwargs 전수 캡처 + 지시층위(JSON) 응답 라우팅."""
    calls = []
    orig = r.prov.generate_content

    def _gen(**kwargs):
        calls.append(kwargs)
        return orig(**kwargs)
    inject.setattr(r.prov, "generate_content", _gen)

    def _narr(kwargs):
        cfg = kwargs.get("config")
        if getattr(cfg, "response_mime_type", None) == "application/json":
            return FakeGenAIResponse(json.dumps(logic or {
                "action": "PROCEED", "proceed_instruction": "숲길을 다시 묘사하라",
                "event_assessment": "ongoing"}, ensure_ascii=False), usage=_usage())
        return FakeGenAIResponse("숲길로 조용히 걸어간다.", usage=_usage())
    r.prov.routes["narration"] = _narr
    return calls


def _call_text(kwargs) -> str:
    cfg = kwargs.get("config")
    return " ".join([str(kwargs.get("contents")), str(getattr(cfg, "system_instruction", ""))])


def _cached_names(calls):
    return [getattr(c.get("config"), "cached_content", None) for c in calls]


def _set_cache(r, name):
    s = r.sess
    core.update_session_cache_state(s)
    s.cache_name = name
    s.cache_obj = object()


async def test_ee3_rewind_blocks_future_cache_and_future_memory(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    _set_cache(r, "caches/turn0")                     # 출처 = 첫 턴 이전
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    # 턴 2 이후 압축 + 재발급 → 미래 기억이 캐시와 캐시 파생 필드에 들어감
    s.compressed_memory = "미래사건XYZ: 동굴 붕괴"
    _set_cache(r, "caches/future")
    assert "미래사건XYZ" in s.cached_compressed_memory
    assert s.cache_history_marker["gm_turn"] == 2
    await core.io.save_session_data_strict(r.bot, s)
    rows, bal = _ink_rows(PLAYER_UID), _balance(PLAYER_UID)
    events = r.ledger.list_cost_events()
    st1 = _store(s).get_settlement_strict(_sid(tx1))

    assert (await r.gm.history_rewind(s, 1))["ok"]
    assert s.cache_history_stale is True and not TH.cache_usable(s)
    assert "미래사건XYZ" not in (s.cached_compressed_memory or "")
    assert "미래사건XYZ" not in (s.compressed_memory or "")
    assert _disk(s)["cache_history_stale"] is True        # durable
    # 재무 사실 불변(E-E3 처리 포함)
    assert _ink_rows(PLAYER_UID) == rows and _balance(PLAYER_UID) == bal
    assert r.ledger.list_cost_events() == events
    assert _store(s).get_settlement_strict(_sid(tx1)) == st1

    calls = _capture_calls(r, inject)
    decision = await r.gm._call_gm_logic(s, "해안으로 간다", [], r.master, action="PROCEED")
    assert decision
    await _commit(r, "해안", "해안으로 간다")
    assert calls, "provider 호출이 캡처되지 않았습니다"
    assert "caches/future" not in _cached_names(calls)
    assert not any("미래사건XYZ" in _call_text(c) for c in calls)
    created = r.bot.genai_client.caches.created
    assert created and not any("미래사건XYZ" in str(c) for c in created), "재발급 캐시에 미래 기억"
    # 재발급 후 출처 갱신·사용 재개
    assert s.cache_history_stale is False and s.cache_history_marker["gm_turn"] == 1


async def test_ee3_rerender_instruction_does_not_read_old_outcome(rig, inject):
    r = rig
    s = r.sess
    _set_cache(r, "caches/turn0")
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    s.compressed_memory = "구시도결과ABC: 동굴에서 보물 발견"
    _set_cache(r, "caches/after-old")
    await core.io.save_session_data_strict(r.bot, s)

    async def _no_judgment(*a, **k):
        raise AssertionError("판단층위 재호출 금지")
    inject.setattr(r.gm, "_call_judgment", _no_judgment, raising=False)
    calls = _capture_calls(r, inject)
    started, reason = await r.gm.rerender_latest(s)
    assert started, reason
    logic_calls = [c for c in calls
                   if getattr(c.get("config"), "response_mime_type", None) == "application/json"]
    assert logic_calls, "지시층위가 실제로 호출돼야 합니다"
    assert "caches/after-old" not in _cached_names(calls)
    assert not any("구시도결과ABC" in _call_text(c) for c in calls)
    assert not any("구시도결과ABC" in str(c) for c in r.bot.genai_client.caches.created)
    assert _hv(s).head_entry()["attempt"] == 2


async def test_ee3_compatible_cache_kept_after_rewind(rig):
    """복원 상태 이전에 만든 캐시(출처가 선택 이력 안)는 계속 쓴다 — 불필요한 재발급 없음."""
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    _set_cache(r, "caches/at1")                       # 출처 = 턴 1
    await _commit(r, "동굴", "동굴로 간다")
    await _commit(r, "폭포", "폭포로 간다")
    assert (await r.gm.history_rewind(s, 1))["ok"]
    assert TH.cache_usable(s) and s.cache_name == "caches/at1"


async def test_ee3_unknown_cache_provenance_is_not_trusted(rig):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    s.cache_history_marker = None                     # 구 캐시(출처 불명)
    assert (await r.gm.history_rewind(s, 1))["ok"]
    assert not TH.cache_usable(s)


def test_ee3_d006e_remains_strict_xfail_for_wp_f():
    from tests.conftest import source_of
    src = source_of("tests/defects/test_cache_accounting.py")
    i = src.index("def test_d006e_single_settlement_point")
    assert "strict=True" in src[max(0, i - 400):i]


# ══════════════════════════════════════════════════════════════
# E-E2a — durable 송출 의도 없이는 게임 채널 파생 출력 금지
# ══════════════════════════════════════════════════════════════

def _intent_durable(sid, tx_id) -> bool:
    """지금 디스크에 이 시도의 송출 의도(인덱스 BEGIN 또는 대체 파일 항목)가 있는가."""
    if any(e["type"] == "MESSAGES_BEGIN" and e["transaction_id"] == tx_id
           for e in TH.load_events(sid)):
        return True
    p = TH.pending_messages_path(sid)
    if os.path.exists(p):
        pend = json.load(open(p, encoding="utf-8"))
        return any(b["transaction_id"] == tx_id for b in pend.values())
    return False


def _fail_begin_append(inject):
    orig = TH._append_event

    def _bad(sid, ev):
        if ev.get("type") == "MESSAGES_BEGIN":
            raise TH.HistoryError("MESSAGES_BEGIN append 실패(모사)")
        return orig(sid, ev)
    inject.setattr(TH, "_append_event", _bad)


async def test_ee2a_begin_failure_falls_back_durably_and_crash_before_mapping_is_traceable(
        rig, inject):
    """BEGIN 인덱스 실패 → 대체 파일에 의도 확보 후에만 송출 → 매핑 전 크래시 → 재시작 후
    BEGIN이 인덱스로 fold되어 그 출력은 unmapped 부채로 추적된다(orphan 없음)."""
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    tx2 = _hv(s).selected[2]["transaction_id"]
    _fail_begin_append(inject)
    seen = []
    orig_send = r.gch.send

    async def _send(content=None, **kw):
        seen.append(_intent_durable(s.session_id, tx2))    # 송출 순간의 durable 의도
        return await orig_send(content, **kw)
    inject.setattr(r.gch, "send", _send)

    def _crash(*a, **k):                                      # 송출 후 매핑 직전 하드 크래시
        raise KeyboardInterrupt("크래시 모사")
    inject.setattr(TH, "record_emitted_messages", _crash)
    d = CC.DerivedEffects()
    d.transaction_id = tx2
    d.game("📜 파생 정본 알림")
    with pytest.raises(KeyboardInterrupt):
        await r.gm._emit_commit_derived(s, d)
    inject.undo()
    orphan = r.gch.sent[-1]
    assert seen == [True], "durable 의도 없이 송출됨"
    assert orphan.content == "📜 파생 정본 알림"

    bot2, s2 = await _restart_with_channels(r)
    assert not TH.needs_reconcile(s2)
    hv = _hv(s2)
    assert hv.open_emits.get(tx2), "재시작 후 송출 의도가 인덱스에 없음(추적 불가)"
    # 그 시도를 되감으면 매핑 유실 가능 부채로 드러나고 수동 정리 안내가 나간다
    r.gm.bot = bot2
    assert (await r.gm.history_rewind(s2, 1))["ok"]
    rw = [e for e in TH.load_events(s2.session_id) if e["type"] == "REWIND"][-1]
    assert rw["cleanup"]["unmapped"] is True
    assert any("매핑 기록이 없어" in (m.content or "") for m in r.master.sent)


async def test_ee2a_no_durable_intent_means_no_game_output(rig, inject):
    """BEGIN 인덱스·대체 파일 모두 실패 → 게임 채널 파생 출력은 보내지 않는다(fail-closed).
    이야기·재무는 유지되고, 재시작 후에도 추적 불가 출력(orphan)이 존재하지 않는다."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx1 = await _commit(r, "숲길")
    tx_id = tx1.transaction_id
    story = TH.capture_reversible(s)
    rows, bal = _ink_rows(PLAYER_UID), _balance(PLAYER_UID)
    _fail_begin_append(inject)

    def _bad_write(path, obj):
        raise TH.HistoryError("대체 파일 쓰기 실패(모사)")
    inject.setattr(TH, "_write_json_strict", _bad_write)
    before = list(r.gch.sent)
    d = CC.DerivedEffects()
    d.transaction_id = tx_id
    d.game("📜 보내면 안 되는 알림")
    d.master("마스터 보고")
    await r.gm._emit_commit_derived(s, d)
    inject.undo()
    assert r.gch.sent == before, "durable 의도 없이 게임 채널 출력이 나갔습니다"
    assert any("보내지 않았습니다" in (m.content or "") for m in r.master.sent)
    assert any(m.content == "마스터 보고" for m in r.master.sent)
    # 이야기·재무 롤백 없음
    assert TH.capture_reversible(s) == story
    assert _ink_rows(PLAYER_UID) == rows and _balance(PLAYER_UID) == bal
    assert _hv(s).head_entry()["transaction_id"] == tx_id

    bot2, s2 = await _restart_with_channels(r)
    hv = _hv(s2)
    bot_game_msgs = [m for m in r.gch.sent if m.content and "보내면 안 되는" in m.content]
    assert bot_game_msgs == []
    assert not hv.open_emits.get(tx_id)


async def test_ee2a_pending_begin_fold_is_idempotent_and_order_safe(rig):
    """대체 파일 BEGIN이 인덱스 MESSAGES보다 늦게 fold되거나 중복 flush돼도 열린 의도가
    되살아나지 않는다(순서 역전·중복·재시작 멱등)."""
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    tx = _hv(s).selected[1]["transaction_id"]
    sid = s.session_id
    # 대체 파일 begin-only + 인덱스 MESSAGES(같은 emit) — 역전 상태
    with open(TH.pending_messages_path(sid), "w", encoding="utf-8") as f:
        json.dump({"e1": {"transaction_id": tx, "message_ids": None, "begin": True}}, f)
    TH.append_messages(sid, tx, [123], "e1")
    assert TH.try_flush(s)
    assert TH.try_flush(s)
    hv = _hv(s)
    assert not hv.open_emits.get(tx) and "e1" in hv.closed_emits
    assert [e for e in TH.load_events(sid) if e["type"] == "MESSAGES_BEGIN"] == []
    # begin-only 두 번 flush(재시작 중복) → BEGIN 1건
    with open(TH.pending_messages_path(sid), "w", encoding="utf-8") as f:
        json.dump({"e2": {"transaction_id": tx, "message_ids": None, "begin": True}}, f)
    assert TH.try_flush(s)
    with open(TH.pending_messages_path(sid), "w", encoding="utf-8") as f:
        json.dump({"e2": {"transaction_id": tx, "message_ids": None, "begin": True}}, f)
    assert TH.try_flush(s)
    assert len([e for e in TH.load_events(sid)
                if e["type"] == "MESSAGES_BEGIN" and e["emit_id"] == "e2"]) == 1
    # 뒤이은 매핑이 닫는다
    TH.append_messages(sid, tx, [456], "e2")
    assert not _hv(s).open_emits.get(tx)


# ══════════════════════════════════════════════════════════════
# E-E2b — 수동 정리 안내 성공 전 unmapped 부채 종결 금지
# ══════════════════════════════════════════════════════════════

async def test_ee2b_manual_cleanup_debt_survives_warning_failure_until_surfaced(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    tx2 = _hv(s).selected[2]["transaction_id"]
    TH.begin_emit(s.session_id, tx2)                       # BEGIN만 남은 시도(매핑 유실 가능)
    known = set(TH.attempt_message_ids(s.session_id, _hv(s).selected[2]))
    orig_send = r.master.send
    state = {"n": 0}

    async def _flaky(content=None, **kw):
        if content and "매핑 기록이 없어" in content and state["n"] == 0:
            state["n"] += 1
            raise RuntimeError("마스터 채널 전송 실패(모사)")
        return await orig_send(content, **kw)
    inject.setattr(r.master, "send", _flaky)

    assert (await r.gm.history_rewind(s, 1))["ok"]          # 첫 drain — 안내 실패
    debts = TH.pending_cleanups(s.session_id)
    assert len(debts) == 1 and debts[0]["unmapped"] is True, "안내 실패 후 부채가 사라졌습니다"
    assert all(m.deleted for m in r.gch.sent if m.id in known), "알려진 출력은 정리돼야 합니다"
    assert not [e for e in TH.load_events(s.session_id) if e["type"] == "CLEANUP_DONE"]
    assert not any("매핑 기록이 없어" in (m.content or "") for m in r.master.sent)
    assert s._history_cleanup_clear is False
    inject.undo()

    # 재시작 — restore drain은 안내 없이 부채 유지(수동 안내는 어댑터만)
    bot2, s2 = await _restart_with_channels(r)
    assert len(TH.pending_cleanups(s2.session_id)) == 1
    # 다음 입력 admission이 다시 발견 → 안내 성공 → 그때에만 manual_ack로 종결
    r.gm.bot = bot2
    await r.gm._recovery_admission(s2)
    warns = [m for m in r.master.sent if "매핑 기록이 없어" in (m.content or "")]
    assert len(warns) == 1
    done = [e for e in TH.load_events(s2.session_id) if e["type"] == "CLEANUP_DONE"]
    assert len(done) == 1 and done[0].get("manual_ack") is True
    assert TH.pending_cleanups(s2.session_id) == []
    # 종결 후에는 재안내 없음
    await r.gm._recovery_admission(s2)
    assert len([m for m in r.master.sent if "매핑 기록이 없어" in (m.content or "")]) == 1


async def test_ee2b_no_master_channel_keeps_manual_debt(rig, inject):
    r = rig
    s = r.sess
    await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    TH.begin_emit(s.session_id, _hv(s).selected[2]["transaction_id"])
    orig_get = r.bot.get_channel
    inject.setattr(r.bot, "get_channel",
                   lambda cid: None if cid == s.master_ch_id else orig_get(cid))
    assert (await r.gm.history_rewind(s, 1))["ok"]
    assert len(TH.pending_cleanups(s.session_id)) == 1
    inject.undo()
    await r.gm._drain_history_cleanup(s)
    assert TH.pending_cleanups(s.session_id) == []
