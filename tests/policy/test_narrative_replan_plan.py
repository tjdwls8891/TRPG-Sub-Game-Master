"""WP-B — 자동 서사 재계획(AUD-065)을 result-only + 정규화 후보 + stale/멱등 경계로.

자동 경로:
  _finish_proceed_and_continue → _update_narrative_progress
  → (스케줄 시점 원인 tx 정체성 복사) → create_task(_auto_replan_narrative)
  → _generate_narrative_plan_candidate(result-only) → build_narrative_plan(정규화)
  → stale guard → 멱등 guard → _apply_narrative_plan(단일 호환 소비부)
setup(init)·manual 경로는 _plan_narrative로 기존 의미를 유지하고 자동 tx 의미를
요구하지 않는다.

N-B01 producer purity / N-B02 valid applies (+code metadata) / N-B03 invalid rejected /
N-B04 stale rejected (+schedule-time identity) / N-B05 duplicate idempotent /
N-B06 setup preserved / N-B07 manual preserved / N-B08 future prompt reads /
N-B09 provider CostEvent preserved.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

import core
from core import cost_ledger as cl
from core import turn_preparation as tp
from tests.fakes.genai_fakes import FakeGenAIResponse, FakeUsageMetadata

pytestmark = pytest.mark.policy

PLAN = {
    "mid_plan": {"title": "숲의 비밀", "overview": "숲 깊은 곳의 유적을 향한다",
                 "milestones": ["숲 입구", "폐허", "유적"], "end_condition": "유적 도달"},
    "current_event": {"title": "갈림길", "summary": "두 갈래 길에서 멈춘다",
                      "resolution_direction": "길잡이를 찾는다", "progress": ""},
    "next_event": {"title": "폐허의 그림자", "summary": "폐허에 누군가 있다",
                   "trigger": "갈림길을 넘어선다"},
    "planner_notes": "긴장도를 서서히 올린다",
    "plan_version": 99,             # provider가 보낸 코드 소유 metadata — 무시돼야 함
    "last_planned_turn": -5,
}
EXISTING = {"current_event": {"title": "이전 사건"}, "next_event": {}, "mid_plan": {},
            "plan_version": 3, "last_planned_turn": 2}


def _cog(bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = bot
    return cog


def _resp(payload):
    return FakeGenAIResponse(json.dumps(payload, ensure_ascii=False),
                             usage=FakeUsageMetadata(prompt=1000, candidates=200))


def _free(sess, plan=None):
    sess.narrative_mode = "free"
    sess.narrative_plan = copy.deepcopy(EXISTING if plan is None else plan)
    return sess


def _supersede(sess, mode):
    """실제 흐름대로 더 새로운 논리 시도를 연다.

    · "attempt": 같은 논리 턴의 새 시도(재요청/재렌더) — begin_attempt, (N,a)→(N,a+1)
    · "turn":    턴 N 종료(finalize) 후 다음 턴 시작 — (N,1)→(N+1,1)
    """
    tt = core.turn_transaction
    active = tt.get_active_transaction(sess)
    if mode == "attempt":
        return tt.begin_attempt(sess, logical_turn=active.logical_turn,
                                player_declaration="재요청")
    tt.finalize(sess, active.transaction_id, tt.TurnStatus.COMMITTED)
    sess.turn_count += 1
    return tt.begin_turn_transaction(sess, "다음 턴")


def _spy_stale(monkeypatch):
    calls = []
    real = tp.superseded_by_newer_attempt

    def _spy(session, **kw):
        r = real(session, **kw)
        calls.append(r)
        return r

    monkeypatch.setattr(tp, "superseded_by_newer_attempt", _spy)
    return calls


def _origin(tx):
    return {"transaction_id": tx.transaction_id,
            "logical_turn": tx.logical_turn, "attempt": tx.attempt}


# ── N-B01 producer purity ────────────────────────────────────────
async def test_nb01_producer_is_pure(provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    before = copy.deepcopy(sess.narrative_plan)
    provider.outcomes = [_resp(PLAN)]
    cand = await _cog(wired_bot)._generate_narrative_plan_candidate(
        sess, "deviated", full_replan=True,
        transaction_id="tx-1", logical_turn=8, attempt=1)
    assert cand is not None and cand.normalized is not None
    assert sess.narrative_plan == before, "producer가 canonical narrative_plan을 변경했습니다"
    # 순수 정규화도 세션을 보지 않는다.
    norm, reasons, _ = tp.normalize_narrative_plan(PLAN)
    assert not reasons and sess.narrative_plan == before
    # 코드 소유 metadata는 provider 권위가 아니다.
    assert "plan_version" not in cand.normalized
    assert "last_planned_turn" not in cand.normalized


# ── N-B02 valid automatic candidate applies (+code-derived metadata) ──
async def test_nb02_valid_auto_replan_applies(provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [_resp(PLAN)]
    ok = await _cog(wired_bot)._auto_replan_narrative(
        sess, "deviated", full_replan=True, **_origin(tx))
    assert ok is True
    np_ = sess.narrative_plan
    assert np_["current_event"]["title"] == "갈림길"
    assert np_["mid_plan"]["milestones"] == ["숲 입구", "폐허", "유적"]
    assert np_["next_event"]["trigger"] == "갈림길을 넘어선다"
    assert np_["planner_notes"] == "긴장도를 서서히 올린다"
    # 기존 의미: 버전 = 이전+1, last_planned_turn = 현재 turn_count (provider 값 무시)
    assert np_["plan_version"] == 4
    assert np_["last_planned_turn"] == sess.turn_count


# ── N-B03 invalid candidate rejected ─────────────────────────────
@pytest.mark.parametrize("bad", [
    ["not", "an", "object"],
    {"current_event": {"title": "x"}, "next_event": {}},            # mid_plan 누락
    {"mid_plan": {}, "current_event": "문자열", "next_event": {}},    # 객체 아님
])
async def test_nb03_invalid_candidate_no_mutation(provider, wired_bot, session_auto_ready, bad):
    sess = _free(session_auto_ready)
    before = copy.deepcopy(sess.narrative_plan)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [_resp(bad)]
    ok = await _cog(wired_bot)._auto_replan_narrative(
        sess, "deviated", full_replan=True, **_origin(tx))
    assert ok is False
    assert sess.narrative_plan == before, "구조 불량 후보가 적용됐습니다"


async def test_nb03b_unparseable_text_no_mutation(provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    before = copy.deepcopy(sess.narrative_plan)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [FakeGenAIResponse("{ 깨진 json", usage=FakeUsageMetadata())]
    ok = await _cog(wired_bot)._auto_replan_narrative(
        sess, "completed", full_replan=False, **_origin(tx))
    assert ok is False and sess.narrative_plan == before


# ── N-B04 stale automatic replan rejected ────────────────────────
@pytest.mark.parametrize("mode", ["attempt", "turn"])
async def test_nb04_stale_replan_discarded(monkeypatch, provider, wired_bot,
                                           session_auto_ready, mode):
    sess = _free(session_auto_ready)
    before = copy.deepcopy(sess.narrative_plan)
    tx_n = core.turn_transaction.begin_turn_transaction(sess, "턴 N")
    origin = _origin(tx_n)                          # 스케줄 시점 복사
    stale_calls = _spy_stale(monkeypatch)

    real = provider.generate_content

    def _late(**kw):                                # provider 응답 전 더 새로운 시도가 열린다
        _supersede(sess, mode)
        return real(**kw)

    provider.generate_content = _late
    provider.outcomes = [_resp(PLAN)]
    ok = await _cog(wired_bot)._auto_replan_narrative(
        sess, "deviated", full_replan=True, **origin)
    assert provider.attempt_count == 1, "provider가 첫 시도에 성공하지 못했습니다(다른 이유로 실패)"
    assert stale_calls == [True], "stale 판정이 호출되지 않았거나 stale로 판정되지 않았습니다"
    assert ok is False
    assert sess.narrative_plan == before, "stale 재계획이 narrative_plan을 덮어썼습니다"


async def test_nb04b_identity_captured_at_schedule_time(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = _free(session_auto_ready)
    cog = _cog(wired_bot)
    tx_n = core.turn_transaction.begin_turn_transaction(sess, "턴 N")
    seen = []

    async def _rec(session, reason, **kw):
        seen.append((reason, kw))
        return True

    async def _forbidden(*a, **k):
        raise AssertionError("자동 경로가 setup/manual 소비부(_plan_narrative)를 불렀습니다")

    monkeypatch.setattr(cog, "_auto_replan_narrative", _rec, raising=False)
    monkeypatch.setattr(cog, "_plan_narrative", _forbidden, raising=False)

    await cog._update_narrative_progress(sess, "deviated", master_channel)
    _supersede(sess, "turn")                        # 태스크 실행 전 다음 턴 시작
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(seen) == 1
    reason, kw = seen[0]
    assert reason == "deviated" and kw["full_replan"] is True
    assert kw["transaction_id"] == tx_n.transaction_id, "태스크가 원인 tx가 아닌 정체성을 받았습니다"
    assert kw["logical_turn"] == tx_n.logical_turn and kw["attempt"] == tx_n.attempt


# ── N-B05 duplicate apply idempotent ─────────────────────────────
async def test_nb05_duplicate_same_identity_applies_once(provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    cog = _cog(wired_bot)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [_resp(PLAN), _resp(PLAN)]
    first = await cog._auto_replan_narrative(sess, "deviated", full_replan=True, **_origin(tx))
    second = await cog._auto_replan_narrative(sess, "deviated", full_replan=True, **_origin(tx))
    assert (first, second) == (True, False)
    assert sess.narrative_plan["plan_version"] == 4, "plan_version이 두 번 증가했습니다"


async def test_nb05b_concurrent_duplicate_tasks_apply_once(provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    cog = _cog(wired_bot)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [_resp(PLAN), _resp(PLAN)]
    res = await asyncio.gather(
        cog._auto_replan_narrative(sess, "completed", full_replan=False, **_origin(tx)),
        cog._auto_replan_narrative(sess, "completed", full_replan=False, **_origin(tx)))
    assert sorted(res) == [False, True]
    assert sess.narrative_plan["plan_version"] == 4


# ── N-B06 setup path preserved ───────────────────────────────────
async def test_nb06_setup_init_needs_no_turn_transaction(
        monkeypatch, provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready, plan={})
    sess.active_turn_transaction = None
    cog = _cog(wired_bot)

    def _no_tx_semantics(*a, **k):
        raise AssertionError("setup 경로가 자동 TurnTransaction stale 판정을 호출했습니다")

    started = []

    async def _start_round(session):
        started.append(True)

    monkeypatch.setattr(tp, "superseded_by_newer_attempt", _no_tx_semantics)
    monkeypatch.setattr(cog, "_start_round", _start_round, raising=False)
    provider.outcomes = [_resp(PLAN)]
    await cog._init_narrative_and_start(sess)
    assert started == [True]
    assert sess.narrative_plan["current_event"]["title"] == "갈림길"
    assert sess.narrative_plan["plan_version"] == 1
    assert sess.narrative_plan["last_planned_turn"] == sess.turn_count


# ── N-B07 manual/operator replan preserved ───────────────────────
async def test_nb07_manual_replan_not_treated_as_automatic(
        monkeypatch, provider, wired_bot, session_auto_ready):
    import cogs.gm as gm_mod
    sess = _free(session_auto_ready)
    cog = _cog(wired_bot)
    core.turn_transaction.begin_turn_transaction(sess, "턴 N")
    _supersede(sess, "attempt")                     # 진행 중(새 시도 활성) 운영자 개입

    def _no_tx_semantics(*a, **k):
        raise AssertionError("manual 경로가 자동 stale 판정을 호출했습니다")

    monkeypatch.setattr(tp, "superseded_by_newer_attempt", _no_tx_semantics)
    sent = []

    async def _send(msg=None, **kw):
        sent.append(msg)

    ctx = SimpleNamespace(channel=SimpleNamespace(id=sess.master_ch_id), send=_send)
    provider.outcomes = [_resp(PLAN)]
    await gm_mod.GMCog.replan_narrative.callback(cog, ctx, memo="적에게 합류했다")
    assert sess.narrative_plan["current_event"]["title"] == "갈림길"
    assert sess.narrative_plan["plan_version"] == 4
    assert not any(m and "실패" in m for m in sent), "manual 재계획이 실패로 보고됐습니다"
    prompt = provider.calls[-1].kwargs["contents"][0].parts[0].text
    assert "적에게 합류했다" in prompt, "운영자 메모가 계획 프롬프트에 반영되지 않았습니다"


# ── N-B08 future prompt sees applied plan ────────────────────────
async def test_nb08_future_gm_prompt_reads_applied_plan(provider, wired_bot, session_auto_ready):
    import cogs.gm as gm_mod
    sess = _free(session_auto_ready)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")
    provider.outcomes = [_resp(PLAN)]
    assert await _cog(wired_bot)._auto_replan_narrative(
        sess, "deviated", full_replan=True, **_origin(tx))
    prompt = gm_mod._build_logic_user_prompt(sess, "주변을 살핀다", [])
    for frag in ("갈림길", "길잡이를 찾는다", "폐허의 그림자", "갈림길을 넘어선다",
                 "숲의 비밀", "숲 입구 → 폐허 → 유적", "유적 도달"):
        assert frag in prompt, f"후속 GM 프롬프트에 적용된 계획이 없습니다: {frag}"


# ── N-B09 provider CostEvent preservation ────────────────────────
async def test_nb09_cost_event_once_per_operation_with_retry(
        tmp_path, provider, wired_bot, session_auto_ready):
    sess = _free(session_auto_ready)
    wired_bot.cost_ledger = cl.CostLedger(str(tmp_path / "cost_ledger.jsonl"))
    cog = _cog(wired_bot)
    tx = core.turn_transaction.begin_turn_transaction(sess, "이번 턴")

    # 적용되는 재계획: 첫 시도 실패 → 재시도 성공 = 논리 오퍼레이션 1, 이벤트 1(attempt 2)
    provider.outcomes = [RuntimeError("일시 실패"), _resp(PLAN)]
    assert await cog._auto_replan_narrative(sess, "deviated", full_replan=True, **_origin(tx))
    ev = [e for e in wired_bot.cost_ledger.list_cost_events()
          if e["operation"] == cl.OP_TURN_NARRATIVE_PLANNING]
    assert len(ev) == 1, f"서사 계획 CostEvent가 중복되거나 사라졌습니다: {len(ev)}"
    assert ev[0]["provider_attempt"] == 2
    assert provider.attempt_count == 2

    # stale로 폐기되는 재계획도 실제 provider 호출이므로 관측은 정확히 1건 추가된다.
    real = provider.generate_content

    def _late(**kw):
        _supersede(sess, "attempt")
        return real(**kw)

    provider.generate_content = _late
    provider.outcomes = [_resp(PLAN)]
    before = copy.deepcopy(sess.narrative_plan)
    tx2 = core.turn_transaction.get_active_transaction(sess)
    assert not await cog._auto_replan_narrative(
        sess, "completed", full_replan=False, **_origin(tx2))
    assert sess.narrative_plan == before, "stale 결과가 적용됐습니다"
    ev = [e for e in wired_bot.cost_ledger.list_cost_events()
          if e["operation"] == cl.OP_TURN_NARRATIVE_PLANNING]
    assert len(ev) == 2
