"""D-004 / D-005 — 되감기 귀속 오류와 운영 비용 되감기.

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

D-004 (AUD-020) 늦게 도착한 추출 결과가 다음 턴 델타에 귀속된다. — WP-E 해소:
                되감기 권위가 선택된 커밋 시도 스냅샷으로 바뀌었다(d004c 전환).
D-005 (AUD-029) `total_cost`가 되감기 추적 대상이라 제공자 비용 이력이
                게임 상태와 함께 되돌아간다. — WP-E 해소(d005 계열 전환).
"""

from __future__ import annotations

import pytest

from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용

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
    # WP-C: 델타 기록은 READY 이후로 이동했다. WP-D: 그 owner는 CommitCoordinator의
    #   부수효과 없는 적용 단계다(스냅샷 이월 방식 자체는 불변 — AUD-020은 WP-E).
    csrc = source_of("core/commit_coordinator.py")
    ctree = ast.parse(csrc)
    cont = next(n for n in ast.walk(ctree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == "_apply_in_memory")
    cont_body = "\n".join(csrc.splitlines()[cont.lineno - 1:cont.end_lineno])

    assert "_rewind_snapshot" in body
    assert "session._rewind_snapshot = after" in cont_body, (
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


async def test_d004c_rewind_to_n_preserves_n_extraction(rig):
    """AUD-020 해소(WP-E) — 턴 N으로 되감으면 N의 추출 결과가 남는다.

    과거 xfail은 '델타 역적용 + 늦은 추출이 N+1 델타에 귀속'되던 legacy 경로를
    모델링했다. WP-D부터 추출은 같은 커밋의 적용 단계에서 반영되고, WP-E 되감기는
    선택된 커밋 시도 N의 커밋 직후 스냅샷(post)을 복원한다. 이 테스트는 실제 자동 경로
    (CommitCoordinator → durable COMMITTED → SELECT)로 두 턴을 커밋한 뒤 production 권위
    (core.turn_history)로 같은 의미를 증명한다: N에서 커밋된 추출 효과(위치)는 N으로
    되감아도 남고, N+1의 변화만 사라진다.
    """
    import core
    from tests.policy.test_turn_history import _commit

    tx1 = await _commit(rig, "숲길")          # 턴 N=1의 추출 결과 = 숲길
    await _commit(rig, "동굴", "동굴로 간다")   # 턴 N+1
    sess = rig.sess
    res = await core.turn_history.rewind(rig.bot, sess, 1)
    assert res["ok"], res
    assert sess.world_timeline.get("current_location") == "숲길", (
        "턴 N의 추출 결과가 되감기로 사라졌습니다")
    assert sess.gm_turns_done == 1
    assert sess.commit_marker["transaction_id"] == tx1.transaction_id


# ──────────────────────────────────────────────────────────
# D-005 운영 비용의 되감기
# ──────────────────────────────────────────────────────────

def test_d005_total_cost_is_currently_rewindable():
    """WP-E(AUD-029) — `total_cost`는 더 이상 되감기 추적 대상이 아니다(운영 이력).

    이력 선택의 가역 필드에도 운영/재무 필드가 없다.
    """
    import core
    assert "total_cost" not in core.TRACKED_PATHS
    assert not set(core.turn_history.REVERSIBLE_FIELDS) & {
        "total_cost", "total_usd", "total_ink_spent", "last_turn_cost", "last_turn_ink"}


def test_d005b_ink_and_usd_are_not_rewindable():
    """특성화 — 실제 결제액은 되감기 대상이 아니다.

    사용자 확정 정책: 총 소모금액은 되감기 대상이 아니다.
    그 결과 `total_cost`와 `total_ink_spent`가 되감기 후 어긋난다.
    """
    import core
    assert "total_ink_spent" not in core.TRACKED_PATHS
    assert "total_usd" not in core.TRACKED_PATHS


def test_d005c_rewind_makes_cost_fields_diverge(session_auto_ready):
    """WP-E — 되감기 델타에 비용 필드가 잡히지 않는다(원화·잉크 모두 운영 이력)."""
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

    assert not cost_changed, "total_cost가 여전히 되감기 델타에 잡힙니다(AUD-029)"
    assert not ink_changed, (
        "total_ink_spent가 델타에 잡혔습니다 — 정책과 다릅니다")


def test_d005d_provider_history_is_irreversible():
    """바람직한 동작 — 제공자/회계 이력은 되감기로 변하지 않아야 한다.

    게임 이력(가역)과 운영/회계 이력(불가역)은 분리되어야 한다.
    """
    import core
    assert "total_cost" not in core.TRACKED_PATHS, (
        "제공자 비용 누적이 여전히 되감기 대상입니다")
