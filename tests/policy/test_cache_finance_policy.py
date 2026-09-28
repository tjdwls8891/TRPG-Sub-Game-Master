"""WP-F gate patch — 캐시 재무 정책 정렬 (POLICY-CACHE-01 ~ 03, P1 ~ P8).

POLICY-CACHE-01  시간 해석 비용 = 별도 실제 청구, 환불 없음, 선불/환급 계산과 분리
POLICY-CACHE-02  운영자 조기 종료(!세션종료 · !캐시 삭제)도 실제 선불 payer 에게 미사용분 환급
POLICY-CACHE-03  실제 > 선불 → 추가 청구 없음, 운영자 부담으로 기록, ADDITIONAL_CHARGE 미실행
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib

import pytest

import core
from core import accounts
from core import cache_lifecycle as CLC
from core import cost_ledger as CL
from core import ink_transactions as IT
from tests.conftest import MASTER_CH, PLAYER_UID, REPO_ROOT
from tests.fakes.discord_fakes import FakeInteraction, FakeMessage, FakeUser
from tests.fakes.genai_fakes import FakeGenAIResponse, FakeUsageMetadata
from tests.policy.test_cache_lifecycle import (TOKENS, Clock, _bal, _events, _ink_rows,
                                               _journal, _seed)

pytestmark = pytest.mark.policy

LATE = "555999"


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
def sess(session_factory, cbot):
    s = session_factory(session_id="wpf-policy")
    s.players = {PLAYER_UID: {"name": "테스터", "profile": {}}}
    s.open_minutes = 180
    cbot.register_session(s)
    _seed(PLAYER_UID, 1000)
    return s


def _kinds(uid=PLAYER_UID):
    return [r["kind"] for r in _ink_rows(uid)]


def _rows(kind, uid=PLAYER_UID):
    return [r for r in _ink_rows(uid) if r["kind"] == kind]


def _join_late(s):
    _seed(LATE, 100)
    s.players[LATE] = {"name": "늦참", "profile": {}}


async def _interpret(cbot, s, provider):
    """실제 GMCog.interpret_cache_time — provider 해석 사실 + interpret_cost_krw 누적."""
    import cogs.gm as gm_mod
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = cbot
    provider.outcomes = [FakeGenAIResponse(
        json.dumps({"case": "explicit", "minutes": 180}),
        usage=FakeUsageMetadata(prompt=200_000, candidates=20_000))]
    res = await gm.interpret_cache_time(s, "3시간")
    assert res["minutes"] == 180
    return gm


# ── P1 — 해석 비용이 실제로 청구된다 (실제 재오픈 경로) ─────────

async def test_p1_interpretation_actually_charged_on_reopen_path(sess, cbot, clock, provider,
                                                                  game_channel, display_channel):
    import cogs.gm as gm_mod
    import cogs.session as session_mod
    await _interpret(cbot, sess, provider)
    interp_krw = sess.interpret_cost_krw
    charge, interp_ink = core.should_charge_interpretation(sess)
    assert charge and interp_ink >= 2
    # provider 해석 사실
    ev = _events(cbot, CL.OP_CACHE_TIME_INTERPRET)
    assert len(ev) == 1 and ev[0]["cost_krw"] == pytest.approx(interp_krw)

    scog = session_mod.SessionCog.__new__(session_mod.SessionCog)
    scog.bot = cbot
    cbot.add_cog_stub("SessionCog", scog)
    sess.creation_state = {"step": "done", "history": [], "data": {}}   # 닫았다가 다시 여는 경로
    view = gm_mod.OpenConfirmView(cbot, sess, 180)
    msg = FakeMessage(channel=display_channel, content="열까요?", view=view)
    view.bind(msg)
    inter = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=display_channel, message=msg)
    await view.confirm.callback(inter)

    # durable player financial effect — 정확히 한 번, 캐시 선불과 별개 거래
    interp_rows = _rows(IT.KIND_INTERPRETATION_CHARGE)
    prepay_rows = _rows(IT.KIND_PREPAYMENT)
    assert len(interp_rows) == 1 and interp_rows[0]["nominal_ink"] == interp_ink
    assert interp_rows[0]["reference_kind"] == CLC.INTERPRET_REFERENCE_KIND
    assert len(prepay_rows) == 1
    cache_ink = prepay_rows[0]["nominal_ink"]
    assert _bal() == 1000 - cache_ink - interp_ink
    assert sess.interpret_cost_krw == 0.0
    # UI 의 '청구됨' 문구 = 실제 거래
    assert any(f"시간 해석 **{interp_ink}잉크** 청구" in (m.content or "")
               for m in game_channel.sent)
    assert "청구되었습니다" not in (msg.content or "")


async def test_p1b_below_threshold_is_waived_and_not_charged(sess, cbot, clock):
    sess.interpret_cost_krw = 1.0                       # 1잉크 → 임계(2) 미만 면제(기존 정책)
    res = await CLC.open_window(cbot, sess)
    assert res["interpret_ink"] == 0 and _rows(IT.KIND_INTERPRETATION_CHARGE) == []
    assert sess.interpret_cost_krw == 0.0


async def test_p1c_interpretation_charged_even_if_cache_create_fails(sess, cbot, clock):
    """이미 수행된 서비스 — 캐시 생성 실패와 무관하게 청구(환불 없음)."""
    sess.interpret_cost_krw = 50.0
    interp = core.cost_to_ink(50.0)
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(RuntimeError):
        await CLC.open_window(cbot, sess)
    assert [r["nominal_ink"] for r in _rows(IT.KIND_INTERPRETATION_CHARGE)] == [interp]
    assert _rows(IT.KIND_PREPAYMENT) == [] and _bal() == 1000 - interp


async def test_p1d_interpretation_charge_failure_resumes_exactly_once(sess, cbot, clock, monkeypatch):
    sess.interpret_cost_krw = 50.0
    real = accounts.apply_ink_adjustment_strict
    state = {"fail": True}

    async def _flaky(uid, **kw):
        if state["fail"] and kw["kind"] == IT.KIND_INTERPRETATION_CHARGE:
            raise accounts.AccountPersistenceError("disk")
        return await real(uid, **kw)
    monkeypatch.setattr(accounts, "apply_ink_adjustment_strict", _flaky)
    res = await CLC.open_window(cbot, sess)
    assert res["interpret_charged"] is False
    state["fail"] = False
    await CLC.restore(cbot, sess)
    await CLC.restore(cbot, sess)
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    assert "WINDOW_INTERPRET_CHARGED" in _journal(sess.session_id)


# ── P2 — 해석 비용은 환불되지 않는다 ─────────────────────────

async def test_p2_interpretation_non_refundable_on_early_close(sess, cbot, clock):
    sess.interpret_cost_krw = 50.0
    interp = core.cost_to_ink(50.0)
    res = await CLC.open_window(cbot, sess)
    cache_ink = res["charge_ink"]
    clock.advance(1800)
    out = await CLC.close_window(cbot, sess, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    used = core.cost.cache_usd_to_ink(core.cost.cache_window_responsibility_usd(
        core.DEFAULT_MODEL, create_tokens=TOKENS, storage=[(TOKENS, 1800.0)]))
    assert out["refund"] == {PLAYER_UID: cache_ink - used}          # 캐시 미사용분만
    assert _kinds() == [IT.KIND_INTERPRETATION_CHARGE, IT.KIND_PREPAYMENT, IT.KIND_REFUND]
    assert _bal() == 1000 - interp - used                            # 해석 청구는 유지
    intent = [e for e in CLC.load_events(sess.session_id)
              if e["type"] == "WINDOW_SETTLE_INTENT"][0]
    assert intent["paid"] == {PLAYER_UID: cache_ink}                 # 환급 기준 = 캐시 선불만


# ── P3 · P4 — 운영자 조기 종료도 환급 (실제 명령 경로) ──────────

class _Ctx:
    def __init__(self, bot, channel):
        self.channel = channel
        self.bot = bot

        class _G:
            default_role = "everyone"
        self.guild = _G()

    async def send(self, content=None, **kw):
        return await self.channel.send(content, **kw)


def _system(cbot):
    import cogs.system as sys_mod
    cog = sys_mod.SystemCog.__new__(sys_mod.SystemCog)
    cog.bot = cbot
    return cog


async def _operator(kind, cbot, sess, master_channel):
    cog = _system(cbot)
    ctx = _Ctx(cbot, master_channel)
    if kind == "end":
        await type(cog).end_session.callback(cog, ctx)
    else:
        await type(cog).manage_cache.callback(cog, ctx, "삭제")


@pytest.mark.parametrize("kind", ["end", "delete"])
async def test_p3_p4_operator_close_refunds_payer_only(kind, sess, cbot, clock, master_channel):
    sess.interpret_cost_krw = 50.0
    interp = core.cost_to_ink(50.0)
    res = await CLC.open_window(cbot, sess)
    cache_ink = res["charge_ink"]
    _join_late(sess)                                                  # 선불하지 않은 늦은 참가자
    clock.advance(2400)
    await _operator(kind, cbot, sess, master_channel)
    used = core.cost.cache_usd_to_ink(core.cost.cache_window_responsibility_usd(
        core.DEFAULT_MODEL, create_tokens=TOKENS, storage=[(TOKENS, 2400.0)]))
    ev = CLC.load_events(sess.session_id)
    assert [e["type"] for e in ev].count("FINALIZED") == 1           # finalizer 한 번
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1              # provider 사실 한 번
    assert [r["nominal_ink"] for r in _rows(IT.KIND_REFUND)] == [cache_ink - used]
    assert _ink_rows(LATE) == [] and _bal(LATE) == 100               # 비 payer 환급 없음
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1            # 해석 환급 없음
    assert _bal() == 1000 - interp - used
    reason = CLC.REASON_OPERATOR_END if kind == "end" else CLC.REASON_OPERATOR_DELETE
    intent = [e for e in ev if e["type"] == "WINDOW_SETTLE_INTENT"][0]
    assert intent["reason"] == reason and intent["disposition"] == CLC.WINDOW_SETTLE_REFUND
    assert sess.cache_name is None
    assert any("환급" in (m.content or "") for m in master_channel.sent)


# ── P5 — 반복·동시·재시작 운영자 종료 = 정확히 한 번 ─────────

async def test_p5_repeated_concurrent_and_restart_operator_close_exactly_once(
        sess, cbot, clock, master_channel):
    await CLC.open_window(cbot, sess)
    clock.advance(1200)
    await asyncio.gather(
        _operator("end", cbot, sess, master_channel),
        _operator("delete", cbot, sess, master_channel),
        CLC.close_window(cbot, sess, reason=CLC.REASON_OPERATOR_END,
                         disposition=CLC.WINDOW_SETTLE_REFUND))
    await _operator("end", cbot, sess, master_channel)
    await CLC.restore(cbot, sess)                                     # 재시작 복구 replay
    bal = _bal()
    await CLC.close_window(cbot, sess, reason=CLC.REASON_OPERATOR_DELETE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    assert len(_events(cbot, CL.OP_CACHE_STORAGE)) == 1
    assert len(_rows(IT.KIND_REFUND)) == 1
    assert _bal() == bal
    assert cbot.genai_client.caches.deleted == ["caches/fake-1"]


# ── P6 — 실제 > 선불 → 추가 청구 없음, 운영자 부담 기록 ─────────

async def test_p6_actual_exceeds_prepayment_is_operator_borne(sess, cbot, clock, monkeypatch):
    real_create = cbot.genai_client.caches.create

    def _big(**kw):
        c = real_create(**kw)

        class _UM:
            total_token_count = TOKENS * 3                  # provider 실측 토큰 > 추정 토큰
        c.usage_metadata = _UM()
        return c
    monkeypatch.setattr(cbot.genai_client.caches, "create", _big)
    res = await CLC.open_window(cbot, sess)
    cache_ink = res["charge_ink"]
    clock.advance(180 * 60)
    await CLC.close_window(cbot, sess, reason=CLC.REASON_EXPIRED,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    intent = [e for e in CLC.load_events(sess.session_id)
              if e["type"] == "WINDOW_SETTLE_INTENT"][0]
    assert intent["used_ink"] > cache_ink
    assert intent["refund"] == {}
    assert intent["operator_borne_shortfall"] == {PLAYER_UID: intent["used_ink"] - cache_ink}
    assert IT.KIND_ADDITIONAL_CHARGE not in _kinds()
    assert _bal() == 1000 - cache_ink                                  # 추가 감소 없음
    st = _events(cbot, CL.OP_CACHE_STORAGE)
    assert len(st) == 1 and st[0]["metadata"]["storage_tokens"] == TOKENS * 3   # 실제 사실 완전 기록
    assert st[0]["metadata"]["storage_seconds"] == pytest.approx(180 * 60)


# ── P7 — 예상/실제 canonical pricing parity ─────────────────────

async def test_p7_estimate_and_actual_share_canonical_pricing(sess, cbot, clock):
    planned = 180 * 60
    # (a) UI 예상(estimate_session_open)과 선불액이 같은 공식·같은 반올림 경계
    probe = core.TRPGSession("probe", 1, 2, "t", {})
    probe.cache_tokens = TOKENS
    ui = core.estimate_session_open(probe, planned / 3600)
    est_usd = core.cost.cache_window_estimate_usd(core.DEFAULT_MODEL, tokens=TOKENS,
                                                  planned_seconds=planned)
    res = await CLC.open_window(cbot, sess)
    assert res["charge_ink"] == ui["total_ink"] == core.cost.cache_usd_to_ink(est_usd)
    # (b) 실제 경과 = 계획 TTL 이면 실제 책임액이 예상과 USD 까지 같다(차이는 시간뿐)
    clock.advance(planned)
    await CLC.close_window(cbot, sess, reason=CLC.REASON_PLAYER_CLOSE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)
    intent = [e for e in CLC.load_events(sess.session_id)
              if e["type"] == "WINDOW_SETTLE_INTENT"][0]
    assert intent["used_usd"] == pytest.approx(est_usd, rel=1e-12)
    assert intent["used_ink"] == res["charge_ink"] and intent["refund"] == {}
    # (c) 반올림 경계: 잉크 올림은 합계(USD→KRW) 에 한 번 — 구성요소별 올림과 다를 수 있다.
    create_usd = core.cost.cache_create_cost_usd(core.DEFAULT_MODEL, tokens=TOKENS)
    per_component = (core.cost_to_ink(create_usd * core.EXCHANGE_RATE)
                     + core.cost_to_ink((est_usd - create_usd) * core.EXCHANGE_RATE))
    assert intent["used_ink"] <= per_component
    # (d) 실제 provider 사실(CostEvent)의 USD 합계도 같은 원시 함수로 같은 값
    ev = _events(cbot)
    assert sum(e["cost_usd"] for e in ev) == pytest.approx(est_usd, rel=1e-12)


# ── P8 — 정책 소스 스캔 ─────────────────────────────────────────

def _prod_sources():
    for base in ("core", "cogs"):
        for p in sorted(pathlib.Path(REPO_ROOT, base).glob("*.py")):
            yield p.relative_to(REPO_ROOT).as_posix(), p.read_text(encoding="utf-8")


def test_p8_policy_source_scan():
    add, interp, refund, prepay, ad_hoc, nope = [], [], [], [], [], []
    for rel, src in _prod_sources():
        if "KIND_ADDITIONAL_CHARGE" in src and rel != "core/ink_transactions.py":
            add.append(rel)
        if "KIND_INTERPRETATION_CHARGE" in src and rel != "core/ink_transactions.py":
            interp.append(rel)
        if "KIND_REFUND" in src and rel != "core/ink_transactions.py":
            refund.append(rel)
        if "KIND_PREPAYMENT" in src and rel != "core/ink_transactions.py":
            prepay.append(rel)
        if ("add_ink(" in src or "deduct_ink(" in src) and rel in (
                "cogs/session.py", "core/display.py", "core/cache_lifecycle.py", "cogs/presence.py"):
            ad_hoc.append(rel)
        if "NO_PLAYER_EFFECT" in src:
            nope.append(rel)
    assert add == []                                  # ADDITIONAL_CHARGE production callers: 0
    assert interp == ["core/cache_lifecycle.py"]
    assert refund == ["core/cache_lifecycle.py"]
    assert prepay == ["core/cache_lifecycle.py"]
    assert ad_hoc == [] and nope == []
    # 운영자 종료 경로는 환급 정산 처분을 쓴다
    sysrc = pathlib.Path(REPO_ROOT, "cogs/system.py").read_text(encoding="utf-8")
    assert sysrc.count("disposition=core.cache_lifecycle.WINDOW_SETTLE_REFUND") == 2
    # 예상·실제가 같은 canonical 헬퍼를 쓴다(별도 공식 금지)
    lc = pathlib.Path(REPO_ROOT, "core/cache_lifecycle.py").read_text(encoding="utf-8")
    est = pathlib.Path(REPO_ROOT, "core/estimate.py").read_text(encoding="utf-8")
    assert "cache_window_estimate_usd" in lc and "cache_window_responsibility_usd" in lc
    assert "cache_window_estimate_usd" in est and "cache_usd_to_ink" in est
    assert "calculate_upload_cost" not in lc
    # OpenConfirmView 는 더 이상 업로드 전에 해석 누적값을 지우지 않는다
    gm = pathlib.Path(REPO_ROOT, "cogs/gm.py").read_text(encoding="utf-8")
    i = gm.index("class OpenConfirmView")
    j = gm.index("class InfinityPlanView")
    assert "interpret_cost_krw = 0" not in gm[i:j]
