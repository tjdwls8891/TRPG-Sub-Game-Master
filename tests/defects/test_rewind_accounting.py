"""D-004 / D-005 — 되감기 귀속 오류와 운영 비용 되감기.

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

D-004 (AUD-020) 늦게 도착한 추출 결과가 다음 턴 델타에 귀속된다.
D-005 (AUD-029) `total_cost`가 되감기 추적 대상이라 제공자 비용 이력이
                게임 상태와 함께 되돌아간다.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.defect


# ──────────────────────────────────────────────────────────
# D-004 늦은 추출 결과의 귀속
# ──────────────────────────────────────────────────────────

def test_d004_snapshot_is_carried_not_retaken(session_auto_ready):
    """현재 설계 — 세션이 '마지막 기록 시점 스냅샷'을 들고 다닌다.

    주석이 밝힌 의도는 유실 방지다. 그 대가로 늦게 도착한 변화가
    다음 턴 델타에 귀속된다.
    """
    import ast

    from tests.conftest import source_of

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_finish_proceed_and_continue")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    # WP-C: 델타 기록은 READY 이후 legacy continuation으로 이동했다(구현 불변).
    cont = next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_post_ready_legacy_continuation")
    cont_body = "\n".join(src.splitlines()[cont.lineno - 1:cont.end_lineno])

    assert "_rewind_snapshot" in body
    assert "session._rewind_snapshot = state_after" in cont_body, (
        "스냅샷 이월 방식이 바뀌었습니다 — AUD-020 성격을 재확인하십시오")


def test_d004b_late_mutation_lands_in_next_turn_delta(session_auto_ready):
    """현재 동작 — 턴 N 델타 기록 뒤 도착한 변화는 N+1 델타에 들어간다."""
    import core

    sess = session_auto_ready

    # 턴 N: 기준 스냅샷
    snap_n = core.capture_state(sess)
    sess._rewind_snapshot = snap_n

    # 턴 N 델타 기록 시점 — 아직 추출 결과가 오지 않았다.
    after_n = core.capture_state(sess)
    delta_n = core.diff_state(snap_n, after_n)
    sess._rewind_snapshot = after_n
    assert delta_n == [], "이 시점에는 변화가 없어야 한다"

    # 턴 N의 추출 결과가 늦게 도착한다.
    sess.world_timeline = dict(sess.world_timeline)
    sess.world_timeline["current_location"] = "숲길"

    # 턴 N+1 델타가 그것을 잡는다.
    after_n1 = core.capture_state(sess)
    delta_n1 = core.diff_state(sess._rewind_snapshot, after_n1)

    paths = {d.get("path") for d in delta_n1 if isinstance(d, dict)}
    assert any("world_timeline" in str(p) for p in paths), (
        "늦은 변화가 N+1 델타에 잡히지 않았습니다")


@pytest.mark.xfail(strict=True,
                   reason="AUD-020 되감기가 턴 N의 추출 결과를 보존하지 않는다")
def test_d004c_rewind_to_n_preserves_n_extraction(session_auto_ready):
    """바람직한 동작 — 턴 N으로 되감으면 N의 추출 결과는 남아야 한다.

    현재는 그 결과가 N+1 델타에 귀속되므로, N으로 되감으면 함께 사라진다.
    """
    import core

    sess = session_auto_ready
    snap_n = core.capture_state(sess)
    sess._rewind_snapshot = snap_n

    after_n = core.capture_state(sess)
    sess._rewind_snapshot = after_n

    # 턴 N의 추출 결과가 늦게 도착
    sess.world_timeline = dict(sess.world_timeline)
    sess.world_timeline["current_location"] = "숲길"
    late_value = sess.world_timeline["current_location"]

    # 턴 N+1 델타에 귀속됨
    after_n1 = core.capture_state(sess)
    delta_n1 = core.diff_state(sess._rewind_snapshot, after_n1)

    # 턴 N으로 되감기 = N+1 델타를 되돌린다
    restored = {k: v for k, v in after_n1.items()}
    for d in delta_n1:
        if not isinstance(d, dict):
            continue
        path = d.get("path")
        if path and "world_timeline" in str(path):
            restored["world_timeline"] = after_n.get("world_timeline")

    assert restored["world_timeline"].get("current_location") == late_value, (
        "턴 N의 추출 결과가 되감기로 사라졌습니다")


# ──────────────────────────────────────────────────────────
# D-005 운영 비용의 되감기
# ──────────────────────────────────────────────────────────

def test_d005_total_cost_is_currently_rewindable():
    """현재 동작 — `total_cost`가 되감기 추적 대상이다."""
    import core
    assert "total_cost" in core.TRACKED_PATHS, (
        "total_cost가 추적 대상에서 빠졌다면 AUD-029 상태가 달라졌습니다")


def test_d005b_ink_and_usd_are_not_rewindable():
    """특성화 — 실제 결제액은 되감기 대상이 아니다.

    사용자 확정 정책: 총 소모금액은 되감기 대상이 아니다.
    그 결과 `total_cost`와 `total_ink_spent`가 되감기 후 어긋난다.
    """
    import core
    assert "total_ink_spent" not in core.TRACKED_PATHS
    assert "total_usd" not in core.TRACKED_PATHS


def test_d005c_rewind_makes_cost_fields_diverge(session_auto_ready):
    """현재 동작 — 되감기가 원화 누적만 되돌려 표기가 어긋난다."""
    import core

    sess = session_auto_ready
    sess.total_cost = 100.0
    sess.total_usd = 100.0 / core.EXCHANGE_RATE
    sess.total_ink_spent = 15

    snap = core.capture_state(sess)

    # 한 턴 진행 — 세 값이 함께 오른다.
    core.accrue(sess, 30.0, 30.0 / core.EXCHANGE_RATE)
    sess.total_ink_spent += core.cost_to_ink(30.0)
    after = core.capture_state(sess)

    delta = core.diff_state(snap, after)
    cost_changed = any("total_cost" in str(d.get("path"))
                       for d in delta if isinstance(d, dict))
    ink_changed = any("total_ink_spent" in str(d.get("path"))
                      for d in delta if isinstance(d, dict))

    assert cost_changed, "total_cost가 델타에 잡히지 않았습니다"
    assert not ink_changed, (
        "total_ink_spent가 델타에 잡혔습니다 — 정책과 다릅니다")


@pytest.mark.xfail(strict=True,
                   reason="AUD-029 제공자 비용 이력이 되감기로 바뀐다")
def test_d005d_provider_history_is_irreversible():
    """바람직한 동작 — 제공자/회계 이력은 되감기로 변하지 않아야 한다.

    게임 이력(가역)과 운영/회계 이력(불가역)은 분리되어야 한다.
    """
    import core
    assert "total_cost" not in core.TRACKED_PATHS, (
        "제공자 비용 누적이 여전히 되감기 대상입니다")
