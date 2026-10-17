# -*- coding: utf-8 -*-
"""P1 #8 — 비용 계측·운영 보고·로그 용어·통계 차원·예측(외부 호출 없음).

  · F2: TTS usage는 오디오 가공 실패와 무관하게 1회 기록된다(0원 혼동 없음).
  · F3: 타임아웃 뒤 늦은 응답은 같은 attempt id로 1회만 · 응답이 없으면 UNKNOWN(CostEvent 없음).
  · F4: 마스터 턴 비용 분해 — 항목은 자기 모델(TTS) 단가, 줄의 합 = 항목 합, 토큰 합계 = 항목 합.
  · F5: 로그 사용량 용어 — out(가시) · thought · out_billed(과금 출력 = CostEvent.output_tokens).
  · F6: CostEvent 통계 차원(시나리오·정의 revision·모드) — 원장만으로 시나리오 통계(세션 폴더 불필요).
  · F7/F8: 원장 통계 기반 예측(시나리오 → 전역 → 기본값) · 부가 호출 · TTS는 가시 본문만 · 표시 형식 불변.
"""
from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

import core
from core import cost_ledger as CL
from core import estimate as ES
from core import tts as TTS
from tests.fakes.genai_fakes import FakeUsageMetadata

pytestmark = pytest.mark.policy


class _Resp:
    def __init__(self, prompt=40, audio=500, parts=()):
        self.usage_metadata = FakeUsageMetadata(prompt=prompt, candidates=audio)
        self.candidates = [SimpleNamespace(content=SimpleNamespace(parts=list(parts)))]
        self.parts = list(parts)


def _bot(models):
    return SimpleNamespace(genai_client=SimpleNamespace(models=models), cost_ledger=CL.CostLedger())


def _sess(sid="p1-cost", scenario="영도", rev="rev1"):
    return SimpleNamespace(session_id=sid, scenario_id=scenario, definition_revision=rev)


def _events(bot, op=None):
    return [e for e in bot.cost_ledger.list_cost_events() if op is None or e["operation"] == op]


# ── F2 ──

def test_tts_usage_recorded_even_when_audio_extraction_fails():
    class M:
        def generate_content(self, **kw):
            return _Resp()                                               # usage 있음 · 오디오 없음
    bot = _bot(M())
    ctx = CL.tts_context(bot, _sess(), CL.OP_TTS_RUNTIME)
    pcm, cost, in_t, out_t = asyncio.run(TTS.synthesize_tts_pcm(bot, "안녕하세요", cost_context=ctx))
    assert pcm == b"" and cost > 0 and (in_t, out_t) == (40, 500)          # 오디오 실패 ≠ 비용 0
    (ev,) = _events(bot, CL.OP_TTS_RUNTIME)
    assert ev["audio_output_tokens"] == 500 and ev["cost_krw"] == pytest.approx(cost)
    krw, _usd, _i, _o = TTS.tts_usage_cost(_Resp())
    assert krw == pytest.approx(cost)                                    # TTS 모델 단가(오디오 $10/1M)
    rates = core.PRICING_1M[core.TTS_MODEL]
    assert cost == pytest.approx((40 * rates["INPUT"] + 500 * rates["OUTPUT"]) / 1e6 * core.EXCHANGE_RATE)


# ── F3 ──

def test_tts_late_response_after_timeout_recorded_once(monkeypatch):
    monkeypatch.setenv("TIMEOUT_MEDIA", "0.05")
    calls = {"n": 0}

    class M:
        def generate_content(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(0.2)                                          # 첫 시도: 래퍼 타임아웃 뒤 도착
            return _Resp()
    bot = _bot(M())
    ctx = CL.tts_context(bot, _sess(), CL.OP_TTS_RUNTIME)

    async def run():
        out = await TTS.synthesize_tts_pcm(bot, "안녕", cost_context=ctx)
        await asyncio.sleep(0.4)                                         # 늦은 첫 시도 완료 대기
        return out
    asyncio.run(run())
    evs = _events(bot, CL.OP_TTS_RUNTIME)
    assert sorted(e["provider_attempt"] for e in evs) == [1, 2]            # 두 실제 호출 각 1회
    late = [e for e in evs if (e["metadata"] or {}).get("late_after_wrapper_timeout")]
    assert [e["provider_attempt"] for e in late] == [1]


def test_tts_duplicate_late_callback_and_unknown(monkeypatch):
    seen = []
    monkeypatch.setattr(core.logs, "domain", lambda kind, data, **kw: seen.append((kind, data)))
    bot = _bot(None)
    ctx = CL.tts_context(bot, _sess(), CL.OP_TTS_RUNTIME)
    obs = TTS._TTSObserver(bot, ctx)

    async def run():
        loop = asyncio.get_running_loop()
        ok = loop.create_future()
        ok.set_result(_Resp())
        obs.track_inflight(ok, attempt=1)
        obs.track_inflight(ok, attempt=1)                                # 중복 콜백 — 1회만
        bad = loop.create_future()
        bad.set_exception(RuntimeError("응답 없음"))
        obs.track_inflight(bad, attempt=2)
    asyncio.run(run())
    assert [e["provider_attempt"] for e in _events(bot, CL.OP_TTS_RUNTIME)] == [1]
    unk = [d for k, d in seen if k == "cost.unknown"]
    assert len(unk) == 1 and unk[0]["attempt"] == 2 and unk[0]["reason"] == "LATE_ATTEMPT_NO_RESPONSE"


def test_text_late_attempt_without_response_is_unknown_not_zero(monkeypatch):
    seen = []
    monkeypatch.setattr(core.logs, "domain", lambda kind, data, **kw: seen.append((kind, data)))
    bot = _bot(None)
    op = CL.begin_operation(bot, CL.OP_TURN_NARRATION, session=_sess(), model=core.DEFAULT_MODEL,
                            actor_kind=CL.ACTOR_PLAYER, billing_hint=CL.HINT_PLAYER_CANDIDATE)

    async def run():
        fut = asyncio.get_running_loop().create_future()
        fut.cancel()
        op.track_inflight(fut, attempt=1)
    asyncio.run(run())
    assert _events(bot) == []                                            # 0원 이벤트를 만들지 않는다
    assert any(k == "cost.unknown" and d["op"] == CL.OP_TURN_NARRATION for k, d in seen)


# ── F4 ──

def test_turn_cost_report_uses_each_entry_model_rate_and_sums_match():
    krw, _usd, i, o = TTS.tts_usage_cost(_Resp(prompt=100, audio=2000))
    nb = core.calculate_text_gen_cost_breakdown(core.DEFAULT_MODEL, input_tokens=1000, output_tokens=300,
                                                cached_read_tokens=600)
    log = [{"label": "묘사층위", "cost": nb["total_krw"], "in": 1000, "cached": 600, "out": 300},
           {"label": "TTS", "cost": krw, "in": i, "cached": 0, "out": o, "model": core.TTS_MODEL}]
    for entry in log:
        lines = core.cost.format_breakdown(entry)
        parts = [float(x) for x in re.findall(r"= ([\d,]+\.\d+)원", lines.replace(",", ""))]
        assert sum(parts) == pytest.approx(entry["cost"], abs=0.01)      # 줄의 합 = 합(표시 반올림 내)
    tts_lines = core.cost.format_breakdown(log[1])
    assert "오디오 출력" in tts_lines and f"${core.PRICING_1M[core.TTS_MODEL]['OUTPUT']}" in tts_lines
    st = SimpleNamespace(billing_user_ids=("1",), provider_cost_krw=nb["total_krw"] + krw,
                         player_billable_cost_krw=nb["total_krw"] + krw, charge_ink_per_user=1,
                         non_player_cost_krw=0.0)
    emb = core.build_turn_cost_embed(3, log, 0.0, total_ink=1, settlement=st,
                                     estimate={"min_ink": 1, "max_ink": 2, "confidence": "mid", "basis": "전역 원장"})
    f = {x.name: x.value for x in emb.fields}
    totals = sum(float(v) for v in re.findall(r"= ([\d.]+)원", f["🧾 토큰 합계"].replace(",", "")))
    assert totals == pytest.approx(nb["total_krw"] + krw, abs=0.02)      # 토큰 합계 = 항목 합(모델별 단가)
    assert "차이" not in f["🧮 턴 청구(Settlement)"]                        # 원장 원가 = 호출 내역
    assert "예상 1~2잉크" in f["🔮 예상 대비"] and "실제 1잉크" in f["🔮 예상 대비"]


# ── F5 · F6 ──

def test_log_usage_terms_and_dimensions(monkeypatch):
    seen = []
    monkeypatch.setattr(core.logs, "domain", lambda kind, data, **kw: seen.append((kind, data)))
    assert core.extract_token_usage(FakeUsageMetadata(prompt=10, candidates=7, thoughts=5, cached=2)) == (10, 12, 2, 5)
    bot = _bot(None)
    op = CL.begin_operation(bot, CL.OP_TURN_JUDGMENT, session=_sess(), model=core.DEFAULT_MODEL,
                            actor_kind=CL.ACTOR_PLAYER, billing_hint=CL.HINT_PLAYER_CANDIDATE)
    op.record(input_tokens=10, cached_input_tokens=2, output_tokens=12, thought_tokens=5, cost_usd=0.1, cost_krw=150)
    (ev,) = _events(bot)
    assert ev["metadata"]["scenario_id"] == "영도" and ev["metadata"]["world_mode"] == "DYNAMIC"
    assert ev["metadata"]["definition_revision"] == "rev1"
    (d,) = [d for k, d in seen if k == "cost.event"]
    assert (d["out"], d["thought"], d["out_billed"]) == (7, 5, 12) and d["scenario"] == "영도"


def test_derived_dimensions_do_not_break_idempotent_replay():
    # 배포 전(차원 없음)에 기록된 같은 멱등키를 배포 후(차원 있음) 다시 strict 기록해도 상충이 아니다
    led = CL.CostLedger()
    base = dict(event_id="e1", idempotency_key="cache:s:c:create", created_at=1.0, provider="google",
                operation=CL.OP_CACHE_CREATE, model=core.DEFAULT_MODEL, session_id="s", transaction_id=None,
                logical_turn=None, turn_attempt=None, provider_attempt=1, actor_user_id=None,
                actor_kind=CL.ACTOR_PLAYER, billing_hint=CL.HINT_PLAYER_CANDIDATE, input_tokens=10,
                cost_usd=0.1, cost_krw=150.0, metadata={"cache_name": "c"})
    assert led.record_cost_event_strict(CL.CostEvent(**base)).created
    again = CL.CostEvent(**dict(base, event_id="e2", created_at=2.0,
                                metadata={"cache_name": "c", **CL.session_dims(_sess())}))
    res = led.record_cost_event_strict(again)
    assert res.created is False and res.event_id == "e1"


# ── F7 · F8 ──

def _seed_ledger(n=6, scenario="영도", out=900, fresh=4000, aux=True, tts=True):
    bot = _bot(None)
    for k in range(n):
        s = _sess(f"s{k}", scenario)
        tx = f"tx{scenario}{k}"
        for opname, o in ((CL.OP_TURN_NARRATION, out), (CL.OP_TURN_JUDGMENT, 150)):
            op = CL.ProviderOperation(bot.cost_ledger, operation=opname, session=s, model=core.DEFAULT_MODEL,
                                      actor_kind=CL.ACTOR_PLAYER, billing_hint=CL.HINT_PLAYER_CANDIDATE,
                                      copy_transaction=False)
            op.transaction_id = tx
            op.record(input_tokens=fresh + 2000, cached_input_tokens=2000, output_tokens=o, cost_krw=1.0)
        if aux and k % 2 == 0:
            op = CL.ProviderOperation(bot.cost_ledger, operation=CL.OP_TURN_IRREGULAR_NPC, session=s,
                                      model=core.DEFAULT_MODEL, actor_kind=CL.ACTOR_PLAYER,
                                      billing_hint=CL.HINT_PLAYER_CANDIDATE, copy_transaction=False)
            op.transaction_id = tx
            op.record(input_tokens=3000, output_tokens=300, cost_krw=1.0)
        if tts:
            ctx = CL.tts_context(bot, s, CL.OP_TTS_RUNTIME)
            ctx.transaction_id = tx
            CL.record_context_event(bot, ctx, cost_krw=1.0, input_tokens=50, audio_output_tokens=3000)
    ES._STATS_CACHE.clear()
    return bot


def _est_session(scenario="영도"):
    s = core.TRPGSession("p1-est", 1, 2, "x", {})
    s.scenario_id = scenario
    s.cost_stats = {}
    return s


def test_estimate_uses_ledger_statistics_then_falls_back():
    s = _est_session()
    base = core.estimate_turn(s, "PROCEED")
    assert base["basis"] == "기본값"
    _seed_ledger()
    est = core.estimate_turn(s, "PROCEED")
    assert est["basis"] == "시나리오 원장" and est["confidence"] == "mid"
    assert "부가 호출" in est["breakdown"] and est["breakdown"]["부가 호출"][1] > 0
    assert est["input_tokens"]["narration"] >= 4000                       # 실측 비캐시 입력보다 작게 잡지 않음
    lo, hi = ES._out_range(s, "narration", "narration_out")
    assert (lo, hi) == (900, int(900 * ES.CONSERVATIVE_FACTOR))
    other = core.estimate_turn(_est_session("무협"), "PROCEED")            # 다른 시나리오 — 전역 원장
    assert other["basis"] == "전역 원장"
    # 표시 형식 불변(F8): 숫자만 바뀐다
    assert re.fullmatch(r"이번 턴 예상: \d+~\d+잉크", core.format_estimate(est))


def test_tts_estimate_excludes_thinking_and_uses_ledger_when_available(monkeypatch):
    monkeypatch.setattr(core.media_control, "is_enabled", lambda s, k: True)
    s = _est_session("없음")
    s.cost_stats = {"narration": {"n": 5, "mean": 2000.0, "min": 1800, "max": 2200, "thought_mean": 1500.0}}
    ES._STATS_CACHE.clear()
    no_ledger = core.estimate_tts(s)
    visible_hi = int((int(2200 * ES.CONSERVATIVE_FACTOR) - 1500) / ES.CHARS_TO_TOKENS)
    assert no_ledger["max_ink"] == core.cost_to_ink(visible_hi * ES.TTS_KRW_PER_CHAR)
    _seed_ledger(tts=True)
    with_ledger = core.estimate_tts(s)
    per_turn = TTS.tts_usage_cost(_Resp(prompt=50, audio=3000))[0]
    assert with_ledger["min_ink"] == core.cost_to_ink(per_turn)


def test_ledger_statistics_survive_session_folder_removal(tmp_path):
    import os
    import shutil
    _seed_ledger()
    shutil.rmtree("sessions", ignore_errors=True)                         # 세션 게임 폴더(로그 포함) 완전 제거
    ES._STATS_CACHE.clear()
    assert not os.path.exists("sessions")
    prof = ES.ledger_profile()                                            # 원장만으로 시나리오 연결 유지
    assert len(prof["ops"]["영도"][CL.OP_TURN_NARRATION]["out"]) == 6
