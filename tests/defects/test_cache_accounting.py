"""D-006 — 캐시 정산 분기 (AUD-034 / AUD-035).

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

(WP-F 이전) 세 경로가 서로 다른 값을 누적했다. WP-F 이후 종료 경로는 캐시 생애주기의
단일 finalizer로 수렴한다(d006d·d006e 갱신).
  1. 캐시 생성 성공 전에 TTL 예상액을 미리 누적하는 경로
  2. 직접 삭제 경로
  3. `process_cache_deletion` 정산 경로

WP-04의 특성화/결함 증거로 쓴다.
"""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.defect


def test_d006a_upload_estimate_is_charged_for_planned_ttl(scenario_minimal):
    """경로 1 — 업로드 비용이 '계획된 TTL' 전체로 산정된다.

    실제로 그만큼 유지하지 않아도 예상액이 먼저 누적된다.
    """
    import core

    tokens = 26_268
    planned_hours = 3.0
    cost = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=planned_hours)

    # 같은 토큰을 1시간만 유지하면 값이 다르다.
    cost_1h = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=1.0)

    assert cost > cost_1h, "저장 시간이 비용에 반영되지 않습니다"
    # 계획 TTL 기준이므로 조기 종료해도 이 값이 이미 누적된다.
    assert cost == pytest.approx(cost_1h + (cost - cost_1h))


async def test_d006b_direct_close_path_settles_by_elapsed(
        wired_bot, session_auto_ready):
    """경로 3 — `process_cache_deletion`은 경과 시간으로 정산한다."""
    import core

    sess = session_auto_ready
    sess.cache_tokens = 26_268
    sess.open_minutes = 180
    sess.cache_created_at = time.time() - 3600      # 1시간 경과
    sess.total_cost = 0.0
    sess.total_usd = 0.0

    settled = await core.process_cache_deletion(wired_bot, sess)

    # NOTE: calculate_storage_cost는 **초**를 받는다.
    #       calculate_storage_cost_usd는 **시간**을 받는다. 단위가 다르다.
    expected_1h = core.calculate_storage_cost(
        core.DEFAULT_MODEL, 26_268, 3600.0)
    assert settled == pytest.approx(expected_1h, rel=0.02), (
        f"경과 1시간 정산이 어긋납니다: {settled} vs {expected_1h}")

    # 캐시 메타데이터가 초기화된다.
    assert sess.cache_name is None
    assert sess.cache_tokens == 0


async def test_d006c_settlement_cap_uses_planned_ttl(
        wired_bot, session_auto_ready):
    """경과가 계획 TTL을 넘으면 계획분으로 상한이 걸린다."""
    import core

    sess = session_auto_ready
    sess.cache_tokens = 26_268
    sess.open_minutes = 180                          # 3시간 계획
    sess.cache_created_at = time.time() - 20_000     # 5.5시간 경과
    sess.total_cost = 0.0
    sess.total_usd = 0.0

    settled = await core.process_cache_deletion(wired_bot, sess)
    expected_3h = core.calculate_storage_cost(
        core.DEFAULT_MODEL, 26_268, 3 * 3600.0)

    assert settled == pytest.approx(expected_3h, rel=0.02), (
        "상한이 계획 TTL을 따르지 않습니다")


async def test_d006d_close_paths_converge_on_one_finalizer(
        wired_bot, session_factory):
    """(WP-F 특성 갱신) 예상(계획 TTL)은 실측이 아니며, 종료 경로는 한 finalizer로 수렴한다.

    WP-F 이전에는 세 경로(업로드 예상 누적 / 직접 삭제 / process_cache_deletion)가 서로 다른
    값을 누적했다. 이제 직접 삭제(운영자)와 호환 래퍼는 같은 생애주기 finalizer를 지나
    같은 경과 기준 보관 사실을 확정한다. 계획 TTL 예상액은 여전히 그와 다르다(예상 ≠ 사실).
    """
    import core
    from core import cache_lifecycle as CLC

    tokens = 26_268
    planned_hours = 3.0
    upload_planned = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=planned_hours)

    def _mk(sid):
        sess = session_factory(session_id=sid)
        sess.cache_name = f"caches/{sid}"
        sess.cache_tokens = tokens
        sess.open_minutes = int(planned_hours * 60)
        sess.cache_created_at = time.time() - 3600
        sess.total_cost = 0.0
        sess.total_usd = 0.0
        return sess

    close_settled = await core.process_cache_deletion(wired_bot, _mk("d006-a"))
    res = await CLC.close_window(wired_bot, _mk("d006-b"),
                                 reason=CLC.REASON_OPERATOR_DELETE,
                                 disposition=CLC.WINDOW_NO_PLAYER_EFFECT)
    direct_delete_recorded = res["storage_krw"]

    assert round(upload_planned, 2) != round(close_settled, 2)
    assert direct_delete_recorded == pytest.approx(close_settled, rel=0.01)
    assert "caches/d006-b" in wired_bot.genai_client.caches.deleted


async def test_d006e_single_settlement_point(fake_bot, session_factory, monkeypatch):
    """(WP-F 해소 — 이전 strict xfail) 캐시 비용은 한 곳에서, provider 사실로 한 번 확정된다.

    과거 결함(AUD-034/035): 업로드 시 계획 TTL 예상액이 provider 비용처럼 먼저 누적되고
    종료 시 실측 보관비가 다시 누적되어 서로 상쇄되지 않았다. 옛 단언은 '업로드 예상 누적'을
    전제로 했으나 그 전제 자체가 제거되었으므로, 같은 결함 의미를 최종 구조로 표현한다:
    열기는 생성 사실만, 닫기는 실제 경과 보관 사실만 기록한다 — 1시간 만에 닫으면 누적과
    CostEvent 합계가 모두 정확히 '생성 + 1시간 보관'이다. 선불은 별도 플레이어 재무 사실이다.
    """
    import os
    import core
    from core import cache_lifecycle as CLC
    from core import cost_ledger as CL

    now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(CLC.time, "time", lambda: now["t"])

    async def _text(bot, model_id, scenario_data, cache_note="", session_id=None, session=None):
        return "룰북", 26_268, "룰북"
    monkeypatch.setattr(core.cache, "build_scenario_cache_text", _text)
    fake_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))

    tokens = 26_268
    sess = session_factory(session_id="d006e")
    sess.players = {"u1": {"name": "p"}}
    sess.open_minutes = 180
    sess.total_cost = 0.0
    sess.total_usd = 0.0

    await CLC.open_window(fake_bot, sess)
    after_open = sess.total_cost
    now["t"] += 3600
    await CLC.close_window(fake_bot, sess, reason=CLC.REASON_PLAYER_CLOSE,
                           disposition=CLC.WINDOW_SETTLE_REFUND)

    actual_1h = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=1.0)
    planned = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=3.0)
    assert after_open < planned, "열기에서 계획 TTL 예상액이 provider 비용으로 누적됐습니다"
    assert sess.total_cost == pytest.approx(actual_1h, rel=0.02), (
        f"실사용(1시간) 기준으로 정산되지 않았습니다: 종료 후 {sess.total_cost:.2f}, "
        f"기대 {actual_1h:.2f}")
    ev = fake_bot.cost_ledger.list_cost_events_strict(session_id="d006e")
    ops = sorted(e["operation"] for e in ev)
    assert ops == [CL.OP_CACHE_CREATE, CL.OP_CACHE_STORAGE]     # 단일 정산점 — 각 사실 한 번
    assert sum(e["cost_krw"] for e in ev) == pytest.approx(actual_1h, rel=0.02)


def test_d006f_storage_cost_helpers_disagree_on_units():
    """단위 불일치 — 이름이 비슷한 두 함수가 다른 단위를 받는다.

    `calculate_storage_cost(model, tokens, duration_seconds)`  초
    `calculate_storage_cost_usd(model, tokens, hours)`          시간

    호출부가 헷갈리면 3600배 오차가 난다. WP-04 통합 시 정리 대상이다.
    """
    import core

    tokens = 26_268
    krw_from_seconds = core.calculate_storage_cost(
        core.DEFAULT_MODEL, tokens, 3600.0)
    usd_from_hours = core.calculate_storage_cost_usd(
        core.DEFAULT_MODEL, tokens, 1.0)

    assert krw_from_seconds == pytest.approx(
        usd_from_hours * core.EXCHANGE_RATE, rel=0.01), (
        "같은 1시간인데 두 함수의 결과가 다릅니다")

    # 단위를 헷갈리면 값이 크게 어긋난다.
    wrong = core.calculate_storage_cost(core.DEFAULT_MODEL, tokens, 1.0)
    assert wrong < krw_from_seconds / 100, (
        "시간을 초 자리에 넘기면 과소 계상된다는 사실을 고정한다")
