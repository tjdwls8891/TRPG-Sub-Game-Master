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


async def _interpret(cbot, s, provider, *, prompt=200_000):
    """실제 GMCog.interpret_cache_time — provider 해석 사실 → durable 청구(임계 이상이면 즉시)."""
    import cogs.gm as gm_mod
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = cbot
    provider.outcomes = [FakeGenAIResponse(
        json.dumps({"case": "explicit", "minutes": 180}),
        usage=FakeUsageMetadata(prompt=prompt, candidates=prompt // 10))]
    res = await gm.interpret_cache_time(s, "3시간")
    assert res["minutes"] == 180
    return gm, res


async def _interpret_fact(cbot, s, krw=50.0, iid="op-1"):
    """해석 사실을 직접 주입(provider 호출 대역) — 기록 + 청구 정산."""
    core.interpretation_billing.record_interpretation(s, interp_id=iid, cost_krw=krw,
                                                      cost_usd=krw / core.EXCHANGE_RATE)
    return await core.interpretation_billing.settle(cbot, s)


def _confirm_view(cbot, sess, display_channel, minutes=180):
    import cogs.gm as gm_mod
    import cogs.session as session_mod
    scog = session_mod.SessionCog.__new__(session_mod.SessionCog)
    scog.bot = cbot
    cbot.add_cog_stub("SessionCog", scog)
    sess.creation_state = {"step": "done", "history": [], "data": {}}   # 닫았다가 다시 여는 경로
    view = gm_mod.OpenConfirmView(cbot, sess, minutes)
    msg = FakeMessage(channel=display_channel, content="열까요?", view=view)
    view.bind(msg)
    inter = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=display_channel, message=msg)
    return view, msg, inter


def _interp_ink_of(res):
    return int((res.get("interpret_charge") or {}).get("ink") or 0)


# ── P1 — 해석 비용이 해석 직후 실제로 청구된다 (실제 재오픈 경로) ──

async def test_p1_interpretation_charged_at_interpretation_then_open(
        sess, cbot, clock, provider, game_channel, display_channel):
    _gm, res = await _interpret(cbot, sess, provider)
    interp_ink = _interp_ink_of(res)
    assert interp_ink >= 2 and res["interpret_charge"]["complete"]
    # provider 사실 ↔ 청구 레코드 감사 연결(interp_id)
    ev = _events(cbot, CL.OP_CACHE_TIME_INTERPRET)
    assert len(ev) == 1 and ev[0]["metadata"]["interpretation_billing"] == 2
    bv = core.interpretation_billing.view(sess.session_id)
    assert list(bv.interpreted) == [ev[0]["metadata"]["interp_id"]]
    # 확인 화면 이전에 이미 durable 계정 효과
    rows = _rows(IT.KIND_INTERPRETATION_CHARGE)
    assert len(rows) == 1 and rows[0]["nominal_ink"] == interp_ink
    assert rows[0]["reference_kind"] == core.interpretation_billing.REFERENCE_KIND
    assert _bal() == 1000 - interp_ink
    assert sess.interpret_cost_krw == 0.0
    # 세션 열기 — 캐시 선불만 새로 결제
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.confirm.callback(inter)
    prepay = _rows(IT.KIND_PREPAYMENT)
    assert len(prepay) == 1 and len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    assert _bal() == 1000 - interp_ink - prepay[0]["nominal_ink"]
    assert any("캐시 선결제" in (m.content or "") for m in game_channel.sent)


async def test_p1_ui_note_matches_committed_charge(sess, cbot, clock, provider, display_channel):
    import cogs.gm as gm_mod
    _gm, res = await _interpret(cbot, sess, provider)
    note = gm_mod._interp_note(res)
    assert f"**{_interp_ink_of(res)}잉크**가 청구되었습니다" in note
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1          # 문구 = 거래


# ── P2 — 해석 비용은 환불되지 않는다 ─────────────────────────

async def test_p2_interpretation_non_refundable_on_early_close(sess, cbot, clock):
    st = await _interpret_fact(cbot, sess, 50.0)
    interp = st["charged_ink"]
    assert interp == core.cost_to_ink(50.0)
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
    interp = (await _interpret_fact(cbot, sess, 50.0))["charged_ink"]
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
    assert interp == ["core/interpretation_billing.py"]       # 캐시 창과 분리된 단일 owner
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
    assert "interpret" not in lc.replace("interpretation_billing", "")  # 캐시 창은 해석을 모른다
    # OpenConfirmView 는 더 이상 업로드 전에 해석 누적값을 지우지 않는다
    gm = pathlib.Path(REPO_ROOT, "cogs/gm.py").read_text(encoding="utf-8")
    i = gm.index("class OpenConfirmView")
    j = gm.index("class InfinityPlanView")
    assert "interpret_cost_krw = 0" not in gm[i:j]


# ══════════════════════════════════════════════════════════════
#  RE-GATE PATCH 2 — 해석 청구 경계·내구성 (P9 ~ P14)
# ══════════════════════════════════════════════════════════════

async def test_p9_interpretation_then_cancel_keeps_single_charge(
        sess, cbot, clock, provider, display_channel):
    _gm, res = await _interpret(cbot, sess, provider)
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.cancel.callback(inter)
    rows = _rows(IT.KIND_INTERPRETATION_CHARGE)
    assert len(rows) == 1 and _bal() == 1000 - rows[0]["nominal_ink"]   # debit 정확히 1회
    assert _rows(IT.KIND_PREPAYMENT) == [] and _rows(IT.KIND_REFUND) == []
    assert cbot.genai_client.caches.created == []


async def test_p10_interpretation_then_timeout_keeps_single_charge(
        sess, cbot, clock, provider, display_channel):
    _gm, res = await _interpret(cbot, sess, provider)
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.on_timeout()
    rows = _rows(IT.KIND_INTERPRETATION_CHARGE)
    assert len(rows) == 1 and _bal() == 1000 - rows[0]["nominal_ink"]
    assert _rows(IT.KIND_PREPAYMENT) == [] and _rows(IT.KIND_REFUND) == []
    assert cbot.genai_client.caches.created == []
    await core.interpretation_billing.settle(cbot, sess)             # 재정산에도 불변
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1


async def test_p11a_crash_after_cost_event_before_billing_record(sess, cbot, clock, provider):
    """provider CostEvent 는 durable, 청구 기록 전 프로세스 종료 → 재시작이 CostLedger 로 재구성."""
    def _crash(*a, **k):
        raise SystemExit("hard crash after CostEvent")
    inject = pytest.MonkeyPatch()                                     # 격리 cwd 픽스처와 분리
    inject.setattr(core.interpretation_billing, "record_interpretation", _crash)
    with pytest.raises(SystemExit):
        await _interpret(cbot, sess, provider)
    inject.undo()
    assert len(_events(cbot, CL.OP_CACHE_TIME_INTERPRET)) == 1
    assert _rows(IT.KIND_INTERPRETATION_CHARGE) == []
    restored = core.TRPGSession(sess.session_id, 1, 2, "t", {})      # 재시작 후 새 객체(메모리 누적 없음)
    restored.players = dict(sess.players)
    st = await core.interpretation_billing.settle(cbot, restored)
    await core.interpretation_billing.settle(cbot, restored)
    rows = _rows(IT.KIND_INTERPRETATION_CHARGE)
    assert len(rows) == 1 and rows[0]["nominal_ink"] == st["charged_ink"] > 0
    assert core.interpretation_billing.view(sess.session_id).interpreted[
        _events(cbot, CL.OP_CACHE_TIME_INTERPRET)[0]["metadata"]["interp_id"]]["adopted"]


@pytest.mark.parametrize("boundary", ["after_intent", "after_account_effect"])
async def test_p11b_crash_around_account_effect_resumes_exactly_once(boundary, sess, cbot, clock):
    IB = core.interpretation_billing
    core.interpretation_billing.record_interpretation(sess, interp_id="op-crash", cost_krw=50.0,
                                                      cost_usd=50.0 / core.EXCHANGE_RATE)
    real_exec = IB._execute_intent

    async def _crash(intent):
        if boundary == "after_account_effect":
            await real_exec(intent)                                   # 계정·원장 적용 후
        raise SystemExit("hard crash")
    inject = pytest.MonkeyPatch()
    inject.setattr(IB, "_execute_intent", _crash)
    with pytest.raises(SystemExit):
        await IB.settle(cbot, sess)
    inject.undo()
    ev = [e["type"] for e in IB.load_events(sess.session_id)]
    assert "CHARGE_INTENT" in ev and "CHARGED" not in ev
    expected_rows = 1 if boundary == "after_account_effect" else 0
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == expected_rows
    restored = core.TRPGSession(sess.session_id, 1, 2, "t", {})
    restored.players = dict(sess.players)
    await IB.settle(cbot, restored)
    await IB.settle(cbot, restored)
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    assert _bal() == 1000 - core.cost_to_ink(50.0)                   # double debit 없음
    assert [e["type"] for e in IB.load_events(sess.session_id)].count("CHARGE_INTENT") == 1


async def test_p12_already_charged_then_open_and_early_close(sess, cbot, clock):
    interp = (await _interpret_fact(cbot, sess, 50.0))["charged_ink"]
    res = await CLC.open_window(cbot, sess)                          # 새 창 ID
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1            # 재청구 없음
    assert len(_rows(IT.KIND_PREPAYMENT)) == 1
    clock.advance(600)
    out = await CLC.close_window(cbot, sess, reason=CLC.REASON_PLAYER_CLOSE,
                                 disposition=CLC.WINDOW_SETTLE_REFUND)
    assert list(out["refund"].values())[0] <= res["charge_ink"]      # 캐시 미사용분만
    assert _kinds() == [IT.KIND_INTERPRETATION_CHARGE, IT.KIND_PREPAYMENT, IT.KIND_REFUND]
    # 다음 열기(새 창)도 과거 해석을 다시 청구하지 않는다
    await CLC.open_window(cbot, sess)
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    assert _bal() == 1000 - interp - (res["charge_ink"] - out["refund"][PLAYER_UID]) - res["charge_ink"]


async def test_p13_affordability_after_interpretation(sess, cbot, clock, provider, display_channel,
                                                      game_channel):
    need = core.estimate_session_open(sess, 3.0)["total_ink"]
    # 해석 청구 후 캐시 선불에는 1잉크 모자라게
    _gm, res = await _interpret(cbot, sess, provider)
    interp = _interp_ink_of(res)
    _seed(PLAYER_UID, 0)
    acc = accounts.load_account_strict(PLAYER_UID)
    acc["ink_balance"] = need - 1
    accounts._write_account_strict(acc)
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.confirm.callback(inter)
    assert "잔액이 부족" in (msg.content or "") and f"{need}잉크" in (msg.content or "")
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1            # 해석 청구 유지
    assert _rows(IT.KIND_PREPAYMENT) == [] and cbot.genai_client.caches.created == []
    assert _bal() == need - 1                                         # overdraft/보조 우회 없음
    # 서비스 계층도 스스로 거부한다(다른 입구 방어)
    with pytest.raises(CLC.CacheOpenInsufficientFunds):
        await CLC.open_window(cbot, sess, quoted_tokens=int(
            core.estimate_session_open(sess, 3.0)["cache_tokens"]))
    assert _rows(IT.KIND_PREPAYMENT) == [] and cbot.genai_client.caches.created == []
    assert "WINDOW_OPENED" not in _journal(sess.session_id)


async def test_p13b_displayed_need_equals_actual_prepayment(sess, cbot, clock, display_channel):
    need = core.estimate_session_open(sess, 3.0)["total_ink"]
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.confirm.callback(inter)
    assert [r["nominal_ink"] for r in _rows(IT.KIND_PREPAYMENT)] == [need]


async def test_p14_below_threshold_preserved(sess, cbot, clock, provider, display_channel):
    # 1잉크 해석 — 청구 없이 누적, 취소해도 유지, 두 번째 해석으로 임계 도달 시 합산 청구
    st1 = await _interpret_fact(cbot, sess, 5.0, iid="op-a")
    assert st1["charged_ink"] == 0 and _rows(IT.KIND_INTERPRETATION_CHARGE) == []
    assert sess.interpret_cost_krw == pytest.approx(5.0)
    st2 = await _interpret_fact(cbot, sess, 5.0, iid="op-b")
    assert st2["charged_ink"] == core.cost_to_ink(10.0) >= 2
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    # 임계 미만 누적이 남은 채로 세션을 열면 면제(기존 규정)
    await _interpret_fact(cbot, sess, 5.0, iid="op-c")
    assert core.cost_to_ink(5.0) < 2
    view, msg, inter = _confirm_view(cbot, sess, display_channel)
    await view.confirm.callback(inter)
    assert len(_rows(IT.KIND_INTERPRETATION_CHARGE)) == 1
    assert sess.interpret_cost_krw == 0.0
    assert "WAIVED" in [e["type"] for e in core.interpretation_billing.load_events(sess.session_id)]


async def test_p1c_interpretation_charge_survives_cache_create_failure(sess, cbot, clock):
    interp = (await _interpret_fact(cbot, sess, 50.0))["charged_ink"]
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(RuntimeError):
        await CLC.open_window(cbot, sess)
    assert [r["nominal_ink"] for r in _rows(IT.KIND_INTERPRETATION_CHARGE)] == [interp]
    assert _rows(IT.KIND_PREPAYMENT) == [] and _bal() == 1000 - interp
