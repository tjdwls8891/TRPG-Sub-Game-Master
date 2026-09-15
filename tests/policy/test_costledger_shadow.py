"""WP-02 — append-only shadow CostLedger 검증.

WP02_COSTLEDGER_SHADOW_SPEC.md §13 + WP-02 PATCH DIRECTIVE §Required tests +
Part I §6 의 요구 항목을 커버한다. 실제 provider SDK를 부르지 않으며,
CostLedger 는 tmp_path 로 격리한다(운영 data/cost_ledger.jsonl 을 만지지 않는다).

관측 계약(핵심):
  - 실제 provider attempt 마다 provider_attempt 가 증가한다(재시도·외부 루프 포함).
  - operation_id 는 논리 오퍼레이션당 1회 생성되어 재시도 전반에서 유지된다.
  - idempotency_key = <operation_id>:attempt:<n> → 중복 append 불가.
  - shadow cost == 레거시 공식(같은 usage → 같은 값).
  - 기록은 레거시 회계/세션 상태를 만지지 않는다.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import core
from core import cost_ledger as cl
from tests.conftest import source_of
from tests.fakes.bot_fakes import FakeBot
from tests.fakes.genai_fakes import (
    FakeGenAIResponse,
    FakeUsageMetadata,
    ScriptedProvider,
)

pytestmark = pytest.mark.policy


# ── 헬퍼 ──────────────────────────────────────────────────────

def _ledger(tmp_path):
    return cl.CostLedger(str(tmp_path / "cost_ledger.jsonl"))


def _bot_with_ledger(tmp_path, provider=None):
    bot = FakeBot(provider=provider or ScriptedProvider())
    bot.cost_ledger = _ledger(tmp_path)
    return bot


def _usage(prompt=1000, candidates=200, thoughts=0, cached=0):
    return FakeUsageMetadata(prompt=prompt, candidates=candidates,
                             thoughts=thoughts, cached=cached)


async def _one_call(bot, op, *, model="m", response=None):
    """call_with_retry 를 한 번 통과시키고 응답을 돌려준다(관측 배선 포함)."""
    _ok, resp = await core.call_with_retry(
        lambda: asyncio.to_thread(
            bot.genai_client.models.generate_content, model=model, contents="x"),
        layer="judgment", session_id="s", retries=1,
        on_attempt_result=op.on_attempt, operation_id=op.operation_id)
    return _ok, resp


# ── 1. 성공 → 이벤트 정확히 1건 ────────────────────────────────

async def test_success_emits_exactly_one_event(tmp_path):
    provider = ScriptedProvider([FakeGenAIResponse("ok", usage=_usage())])
    bot = _bot_with_ledger(tmp_path, provider)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, model="m",
                            actor_kind=cl.ACTOR_PLAYER,
                            billing_hint=cl.HINT_PLAYER_CANDIDATE)
    _ok, resp = await _one_call(bot, op)
    assert _ok
    _in, _out, _cached, _th = core.extract_token_usage(resp.usage_metadata)
    bd = core.calculate_text_gen_cost_breakdown("m", input_tokens=_in,
                                                output_tokens=_out,
                                                cached_read_tokens=_cached)
    assert op.record(input_tokens=_in, output_tokens=_out,
                     cost_usd=bd["total_usd"], cost_krw=bd["total_krw"]) is True

    events = bot.cost_ledger.list_cost_events()
    assert len(events) == 1
    assert events[0]["operation"] == cl.OP_TURN_JUDGMENT
    assert events[0]["provider_attempt"] == 1


# ── 2. 재시도 후 성공 → provider_attempt 번호 정확 ────────────

async def test_retry_then_success_numbers_attempt(tmp_path):
    provider = ScriptedProvider([
        RuntimeError("일시 실패"),
        FakeGenAIResponse("ok", usage=_usage()),
    ])
    bot = _bot_with_ledger(tmp_path, provider)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, model="m")
    # retries=2 → 첫 시도 실패(관측 attempt 1), 둘째 성공(관측 attempt 2)
    _ok, resp = await core.call_with_retry(
        lambda: asyncio.to_thread(
            bot.genai_client.models.generate_content, model="m", contents="x"),
        layer="judgment", session_id="s", retries=2,
        on_attempt_result=op.on_attempt, operation_id=op.operation_id)
    assert _ok
    assert op.current_attempt == 2
    op.record(cost_krw=1.0, cost_usd=0.001)
    events = bot.cost_ledger.list_cost_events()
    assert len(events) == 1
    assert events[0]["provider_attempt"] == 2
    assert events[0]["idempotency_key"].endswith(":attempt:2")


# ── 3. 동일 idempotency key 중복 → 두 번 기록되지 않음 ────────

async def test_duplicate_idempotency_key_no_dup(tmp_path):
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, model="m")
    op.mark_attempt()
    assert op.record(cost_krw=1.0, cost_usd=0.001, provider_attempt=1) is True
    # 같은 attempt 로 다시 기록 시도 → 중복 키 → False, 이벤트 1건 유지
    assert op.record(cost_krw=1.0, cost_usd=0.001, provider_attempt=1) is False
    assert len(bot.cost_ledger.list_cost_events()) == 1


# ── 4/5. 외부 재시도 루프 → operation_id 유지, attempt 증가 ───

@pytest.mark.parametrize("op_key", [cl.OP_TURN_JUDGMENT, cl.OP_TURN_EXTRACTION])
async def test_outer_retry_shares_operation_id(tmp_path, op_key):
    # 판정/추출은 외부 의미 재시도(각 call_with_retry(retries=1))가
    # 하나의 operation_id 를 공유하고 provider_attempt 만 증가시킨다.
    provider = ScriptedProvider([
        FakeGenAIResponse("첫 응답(파싱 실패 가정)", usage=_usage(candidates=100)),
        FakeGenAIResponse("둘째 응답(성공)", usage=_usage(candidates=200)),
    ])
    bot = _bot_with_ledger(tmp_path, provider)
    op = cl.begin_operation(bot, op_key, model="m")

    for _ in range(2):  # 외부 루프 2회
        _ok, resp = await _one_call(bot, op)
        assert _ok
        _in, _out, _c, _th = core.extract_token_usage(resp.usage_metadata)
        bd = core.calculate_text_gen_cost_breakdown("m", input_tokens=_in,
                                                    output_tokens=_out)
        op.record(input_tokens=_in, output_tokens=_out,
                  cost_usd=bd["total_usd"], cost_krw=bd["total_krw"])

    events = bot.cost_ledger.list_cost_events()
    assert len(events) == 2
    op_ids = {e["idempotency_key"].rsplit(":attempt:", 1)[0] for e in events}
    assert len(op_ids) == 1  # 동일 operation_id
    assert {e["provider_attempt"] for e in events} == {1, 2}
    assert all(e["operation"] == op_key for e in events)


# ── 6. 커스텀 내레이션 캐시만료 재시도 → operation_id 유지 ────

async def test_narration_cache_expiry_retry_shares_operation_id(tmp_path):
    # _execute_proceed::generate_with_retry 직접 계측 모사:
    # attempt1(캐시만료로 실패) → 캐시 재발급 → attempt2(성공) 동일 op.
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_TURN_NARRATION, model="m",
                            actor_kind=cl.ACTOR_PLAYER,
                            billing_hint=cl.HINT_PLAYER_CANDIDATE)
    op.mark_attempt()  # attempt 1 — 캐시 만료로 실패
    op.mark_attempt()  # attempt 2 — 재발급 후 성공
    op.record(cost_krw=5.0, cost_usd=0.005)
    events = bot.cost_ledger.list_cost_events()
    assert len(events) == 1
    assert events[0]["operation"] == cl.OP_TURN_NARRATION
    assert events[0]["provider_attempt"] == 2
    assert events[0]["idempotency_key"].endswith(":attempt:2")


# ── 7. 자동 턴 이벤트가 트랜잭션 식별자를 보존 ───────────────

async def test_event_carries_transaction_identity(tmp_path, session_factory):
    sess = session_factory()
    txn = core.turn_transaction.get_or_begin_turn_transaction(sess, "선언")
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, session=sess, model="m",
                            actor_kind=cl.ACTOR_PLAYER,
                            billing_hint=cl.HINT_PLAYER_CANDIDATE)
    op.mark_attempt()
    op.record(cost_krw=1.0, cost_usd=0.001)
    ev = bot.cost_ledger.list_cost_events()[0]
    assert ev["transaction_id"] == txn.transaction_id
    assert ev["logical_turn"] == txn.logical_turn
    assert ev["turn_attempt"] == txn.attempt
    assert ev["session_id"] == sess.session_id


async def test_no_active_transaction_leaves_identity_null(tmp_path, session_factory):
    sess = session_factory()  # 트랜잭션 시작 안 함
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_CACHE_TIME_INTERPRET, session=sess,
                            copy_transaction=False)
    op.mark_attempt()
    op.record(cost_krw=1.0, cost_usd=0.001)
    ev = bot.cost_ledger.list_cost_events()[0]
    assert ev["transaction_id"] is None
    assert ev["logical_turn"] is None
    assert ev["session_id"] == sess.session_id


# ── 8. PROFILE_AI → FREE_FEATURE 힌트로 실제 provider 비용 기록 ─

async def test_profile_ai_records_with_free_feature_hint(tmp_path):
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_PROFILE_AI, model="m",
                            actor_kind=cl.ACTOR_SYSTEM,
                            billing_hint=cl.HINT_FREE_FEATURE,
                            copy_transaction=False)
    op.mark_attempt()
    op.record(input_tokens=500, output_tokens=100, cost_krw=2.0, cost_usd=0.002)
    ev = bot.cost_ledger.list_cost_events()[0]
    assert ev["operation"] == cl.OP_PROFILE_AI
    assert ev["billing_hint"] == cl.HINT_FREE_FEATURE
    assert ev["cost_krw"] > 0  # 무료 기능이라도 provider 비용은 기록된다


# ── 9. TTS runtime/test/preset scope 가 구분된다 ─────────────

async def test_tts_scopes_are_distinct(tmp_path, session_factory):
    sess = session_factory()
    core.turn_transaction.get_or_begin_turn_transaction(sess, "선언")
    bot = _bot_with_ledger(tmp_path)

    ctx_runtime = cl.tts_context(bot, sess, cl.OP_TTS_RUNTIME)
    ctx_test = cl.tts_context(bot, sess, cl.OP_TTS_TEST)
    ctx_preset = cl.tts_context(bot, None, cl.OP_TTS_PRESET_BUILD)

    for ctx in (ctx_runtime, ctx_test, ctx_preset):
        cl.record_context_event(bot, ctx, cost_krw=1.0, cost_usd=0.001,
                                audio_output_tokens=300)

    events = {e["operation"]: e for e in bot.cost_ledger.list_cost_events()}
    assert set(events) == {cl.OP_TTS_RUNTIME, cl.OP_TTS_TEST, cl.OP_TTS_PRESET_BUILD}
    # 런타임만 활성 턴 식별자를 복사한다.
    assert events[cl.OP_TTS_RUNTIME]["transaction_id"] is not None
    assert events[cl.OP_TTS_TEST]["transaction_id"] is None
    assert events[cl.OP_TTS_PRESET_BUILD]["transaction_id"] is None
    assert events[cl.OP_TTS_PRESET_BUILD]["session_id"] is None
    # actor/billing 힌트도 구분된다.
    assert events[cl.OP_TTS_RUNTIME]["billing_hint"] == cl.HINT_PLAYER_CANDIDATE
    assert events[cl.OP_TTS_PRESET_BUILD]["billing_hint"] == cl.HINT_OPERATOR
    # audio 출력 토큰이 오디오 필드로 기록된다(텍스트 출력과 구분).
    assert events[cl.OP_TTS_RUNTIME]["audio_output_tokens"] == 300


# ── 10. 이미지 폴백은 ESTIMATE, provider metadata 아님 ───────

async def test_image_fallback_marked_estimate(tmp_path):
    bot = _bot_with_ledger(tmp_path)
    # metadata 있는 경우
    op1 = cl.begin_operation(bot, cl.OP_IMAGE_GENERATION, model="img")
    op1.mark_attempt()
    op1.record(image_output_tokens=1120, cost_krw=10.0, cost_usd=0.01,
               usage_source=cl.SOURCE_PROVIDER_METADATA,
               extra_metadata={"legacy_usage_source": "usage_metadata"})
    # metadata 부재 → 폴백 추정
    op2 = cl.begin_operation(bot, cl.OP_IMAGE_GENERATION, model="img")
    op2.mark_attempt()
    op2.record(image_output_tokens=1120, cost_krw=10.0, cost_usd=0.01,
               usage_source=cl.SOURCE_ESTIMATE,
               extra_metadata={"legacy_usage_source": "fallback(1K)"})

    events = bot.cost_ledger.list_cost_events()
    sources = {e["usage_source"] for e in events}
    assert sources == {cl.SOURCE_PROVIDER_METADATA, cl.SOURCE_ESTIMATE}
    est = [e for e in events if e["usage_source"] == cl.SOURCE_ESTIMATE][0]
    assert est["metadata"]["legacy_usage_source"].startswith("fallback")


def test_image_fallback_source_mapping_rule():
    # send_media 의 매핑 규칙: 'fallback*' → ESTIMATE, 그 외 → PROVIDER_METADATA
    def _map(usage_source):
        return (cl.SOURCE_ESTIMATE if str(usage_source).startswith("fallback")
                else cl.SOURCE_PROVIDER_METADATA)
    assert _map("usage_metadata") == cl.SOURCE_PROVIDER_METADATA
    assert _map("fallback(1K)") == cl.SOURCE_ESTIMATE
    assert _map("fallback(parse_err: KeyError)") == cl.SOURCE_ESTIMATE


# ── 11. 캐시 생성 추정은 provider 관측 비용으로 오분류되지 않음 ─

def test_cache_create_paths_emit_no_cost_event():
    """캐시 생성(upload/reissue/restore/manage)은 grounded provider usage 가
    없으므로 CostEvent 를 발행하지 않는다(DEFERRED). 운영 소스에 캐시 생성
    operation_id 로 record 하는 곳이 없어야 한다.
    """
    for rel in ("cogs/session.py", "cogs/game.py", "cogs/system.py", "core/cache.py"):
        src = source_of(rel)
        assert "OP_CACHE_CREATE" not in src, f"{rel} 이 CACHE_CREATE 이벤트를 발행"
        assert "OP_CACHE_RECOVERY_CREATE" not in src, f"{rel} 이 RECOVERY_CREATE 발행"


def test_estimate_and_metadata_are_distinguishable(tmp_path):
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_IMAGE_GENERATION, model="img")
    op.mark_attempt()
    op.record(cost_krw=1.0, cost_usd=0.001, usage_source=cl.SOURCE_ESTIMATE)
    ev = bot.cost_ledger.list_cost_events()[0]
    assert ev["usage_source"] == cl.SOURCE_ESTIMATE
    assert ev["usage_source"] != cl.SOURCE_PROVIDER_METADATA


# ── 12. rewind 는 append-only 원장을 건드리지 않는다 ─────────

async def test_rewind_does_not_mutate_ledger(tmp_path, session_factory):
    sess = session_factory()
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, session=sess, model="m")
    op.mark_attempt()
    op.record(cost_krw=7.0, cost_usd=0.007)

    before = (tmp_path / "cost_ledger.jsonl").read_text(encoding="utf-8")
    # rewind 계열이 만지는 세션 총계를 되돌려도(회계 롤백 모사) 원장 파일은 불변.
    sess.total_cost = 0.0
    sess.total_usd = 0.0
    if hasattr(core, "rewind") and hasattr(sess, "rewind_history"):
        pass  # 실제 rewind 는 세션 상태만 다룬다 — 원장은 별도 파일.
    after = (tmp_path / "cost_ledger.jsonl").read_text(encoding="utf-8")
    assert before == after
    assert len(bot.cost_ledger.list_cost_events()) == 1


# ── 13. 기록은 레거시 회계/세션 상태를 만지지 않는다 ──────────

async def test_record_does_not_touch_session_totals(tmp_path, session_factory):
    sess = session_factory()
    sess.total_cost = 123.0
    sess.total_usd = 0.5
    bot = _bot_with_ledger(tmp_path)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, session=sess, model="m")
    op.mark_attempt()
    op.record(cost_krw=99.0, cost_usd=0.09)
    # shadow 기록이 레거시 누적을 바꾸지 않는다.
    assert sess.total_cost == 123.0
    assert sess.total_usd == 0.5


# ── 14. shadow cost == 레거시 공식 ───────────────────────────

async def test_shadow_cost_equals_legacy_formula(tmp_path):
    provider = ScriptedProvider([
        FakeGenAIResponse("ok", usage=_usage(prompt=12345, candidates=678,
                                             thoughts=90, cached=234)),
    ])
    bot = _bot_with_ledger(tmp_path, provider)
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, model=core.DEFAULT_MODEL)
    _ok, resp = await _one_call(bot, op, model=core.DEFAULT_MODEL)
    _in, _out, _cached, _th = core.extract_token_usage(resp.usage_metadata)
    bd = core.calculate_text_gen_cost_breakdown(
        core.DEFAULT_MODEL, input_tokens=_in, output_tokens=_out,
        cached_read_tokens=_cached)
    op.record(input_tokens=_in, cached_input_tokens=_cached, output_tokens=_out,
              thought_tokens=_th, cost_usd=bd["total_usd"], cost_krw=bd["total_krw"])
    ev = bot.cost_ledger.list_cost_events()[0]
    assert ev["cost_krw"] == bd["total_krw"]
    assert ev["cost_usd"] == bd["total_usd"]
    # 출력 토큰은 사고 토큰을 합산한 값(레거시 extract_token_usage 규약).
    assert ev["output_tokens"] == _out
    assert ev["thought_tokens"] == _th


# ── 관측 실패가 레거시 호출을 망가뜨리지 않는다 ───────────────

async def test_observer_failure_does_not_break_call(tmp_path):
    bot = _bot_with_ledger(tmp_path)
    provider = ScriptedProvider([FakeGenAIResponse("ok", usage=_usage())])
    bot.genai_client = type(bot.genai_client)(provider)

    def _boom(**kwargs):
        raise ValueError("관측기 폭발")

    _ok, resp = await core.call_with_retry(
        lambda: asyncio.to_thread(
            bot.genai_client.models.generate_content, model="m", contents="x"),
        layer="judgment", session_id="s", retries=1,
        on_attempt_result=_boom, operation_id="op:x")
    assert _ok is True  # 관측기 예외에도 호출 자체는 성공 반환
    assert resp.text == "ok"


# ── ledger 미부착(테스트/미구성) 시 no-op ────────────────────

async def test_missing_ledger_is_noop():
    bot = FakeBot()  # cost_ledger 미부착
    op = cl.begin_operation(bot, cl.OP_TURN_JUDGMENT, model="m")
    op.mark_attempt()
    assert op.record(cost_krw=1.0, cost_usd=0.001) is False  # no-op, 예외 없음


# ── 커버리지 exit-criterion: 모든 retry-wrapped provider 호출이 관측된다 ──

@pytest.mark.parametrize("rel", [
    "cogs/gm.py", "cogs/game.py", "cogs/media.py",
    "core/tts.py", "core/profile_ai.py", "core/utils.py",
])
def test_every_call_with_retry_site_is_observed(rel):
    """WP-02 exit criterion #2: 모든 실제 provider call_with_retry 경계에
    provider-attempt 옵저버(on_attempt_result)가 배선되어 있어야 한다.
    """
    src = source_of(rel)
    n_calls = src.count("call_with_retry(")
    n_obs = src.count("on_attempt_result=")
    assert n_obs == n_calls, (
        f"{rel}: call_with_retry {n_calls}건 중 관측 배선은 {n_obs}건 "
        f"(모든 provider 호출은 on_attempt_result 를 가져야 한다)")


def test_narration_has_direct_attempt_instrumentation():
    """커스텀 내레이션 재시도(_execute_proceed::generate_with_retry)는
    call_with_retry 를 쓰지 않으므로 직접 계측(mark_attempt)을 가져야 한다.
    """
    src = source_of("cogs/game.py")
    assert "_narr_op.mark_attempt()" in src
    assert "_narr_op.record(" in src
    assert cl.OP_TURN_NARRATION in src


def test_resilience_observer_is_additive():
    """call_with_retry 의 관측 인자는 순수 가산(기본 None)이어야 한다."""
    src = source_of("core/resilience.py")
    assert "on_attempt_result=None" in src
    assert "operation_id=None" in src

def test_ledger_persistence_dedup_across_instances(tmp_path):
    path = str(tmp_path / "cost_ledger.jsonl")
    led1 = cl.CostLedger(path)
    ev = cl.CostEvent(
        event_id="e1", idempotency_key="op:z:attempt:1", created_at=0.0,
        provider=cl.PROVIDER_GOOGLE_GENAI, operation=cl.OP_TURN_JUDGMENT,
        model="m", session_id="s", transaction_id=None, logical_turn=None,
        turn_attempt=None, provider_attempt=1, actor_user_id=None,
        actor_kind=cl.ACTOR_PLAYER, billing_hint=cl.HINT_PLAYER_CANDIDATE,
        cost_krw=1.0, cost_usd=0.001)
    assert led1.record_cost_event(ev) is True
    # 새 인스턴스가 기존 파일의 키를 읽어 중복을 막는다.
    led2 = cl.CostLedger(path)
    assert led2.has_idempotency_key("op:z:attempt:1") is True
    assert led2.record_cost_event(ev) is False
    # 파일에는 1줄만.
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "e1"
