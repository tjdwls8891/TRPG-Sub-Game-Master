"""D-006 — 캐시 정산 분기 (AUD-034 / AUD-035).

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

세 경로가 서로 다른 값을 누적한다.
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


async def test_d006d_three_paths_produce_divergent_totals(
        wired_bot, session_factory):
    """세 경로의 누적값이 서로 다르다 — WP-04가 통합해야 할 증거."""
    import core

    tokens = 26_268
    planned_hours = 3.0

    # 경로 1: 업로드 시 계획 TTL 전체를 누적
    upload_planned = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=planned_hours)

    # 경로 3: 1시간 만에 닫으면 그만큼만 정산
    sess = session_factory(session_id="d006")
    sess.cache_name = "caches/x"
    sess.cache_tokens = tokens
    sess.open_minutes = int(planned_hours * 60)
    sess.cache_created_at = time.time() - 3600
    sess.total_cost = 0.0
    sess.total_usd = 0.0
    close_settled = await core.process_cache_deletion(wired_bot, sess)

    # 경로 2: 직접 삭제 — 정산 함수를 거치지 않으면 0이다.
    direct_delete_recorded = 0.0

    totals = {
        "upload_planned_ttl": round(upload_planned, 2),
        "close_by_elapsed": round(close_settled, 2),
        "direct_delete": direct_delete_recorded,
    }
    assert len(set(totals.values())) == 3, (
        f"세 경로가 같은 값을 낸다면 이 결함이 해소된 것입니다: {totals}")


@pytest.mark.xfail(strict=True,
                   reason="AUD-034/035 캐시 회계가 단일 정산점을 갖지 않는다")
async def test_d006e_single_settlement_point(wired_bot, session_factory):
    """바람직한 동작 — 캐시 비용은 한 곳에서 한 번만 확정되어야 한다.

    현재는 업로드 시 예상 누적과 종료 시 실측 정산이 따로 존재하며
    서로를 상쇄하지 않는다.
    """
    import core

    tokens = 26_268
    sess = session_factory(session_id="d006e")
    sess.cache_name = "caches/x"
    sess.cache_tokens = tokens
    sess.open_minutes = 180
    sess.cache_created_at = time.time() - 3600
    sess.total_cost = 0.0
    sess.total_usd = 0.0

    # 업로드 시 계획분이 이미 누적됐다고 가정한다.
    planned = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=3.0)
    core.accrue(sess, planned, planned / core.EXCHANGE_RATE)
    after_upload = sess.total_cost

    # 1시간 만에 닫는다.
    await core.process_cache_deletion(wired_bot, sess)

    actual_1h = core.calculate_upload_cost(
        core.DEFAULT_MODEL, input_tokens=tokens, store_hours=1.0)
    assert sess.total_cost == pytest.approx(actual_1h, rel=0.02), (
        f"실사용(1시간) 기준으로 정산되지 않았습니다: "
        f"업로드 후 {after_upload:.2f} → 종료 후 {sess.total_cost:.2f}, "
        f"기대 {actual_1h:.2f}")


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
