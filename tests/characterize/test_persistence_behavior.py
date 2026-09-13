"""C-005 — 영속화 실패 흡수 특성화.

WP00_EXECUTABLE_TEST_PLAN.md §5 · WP00_TEST_HARNESS_SPEC.md §4 기준.

v5.33.0의 `save_session_data()`는 실패를 삼키고 성공/실패 신호를
돌려주지 않는다. 엄격 영속화(strict persistence) 작업이 이를 바꾼다.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.characterize


async def test_c005_save_failure_is_swallowed(
        monkeypatch, capsys, wired_bot, session_auto_ready):
    """C-005 — 쓰기 경계가 실패해도 예외가 올라오지 않는다."""
    import core

    def _boom(*args, **kwargs):
        raise OSError("디스크 실패 시뮬레이션")

    monkeypatch.setattr(os, "replace", _boom)

    # 예외가 전파되지 않는다.
    result = await core.save_session_data(wired_bot, session_auto_ready)

    # 성공/실패를 구분할 반환값이 없다.
    assert result is None, (
        "v5.33.0은 저장 결과를 돌려주지 않는다 — 엄격 영속화 도입 시 이 단언이 바뀐다")

    out = capsys.readouterr().out
    assert "세션 저장 실패" in out, "실패가 로그에도 남지 않으면 관측 불가다"


async def test_c005b_successful_save_also_returns_none(
        wired_bot, session_auto_ready):
    """성공해도 같은 값(None)을 돌려준다 — 호출부가 구분할 수 없다."""
    import core
    result = await core.save_session_data(wired_bot, session_auto_ready)
    assert result is None
    assert os.path.exists(f"sessions/{session_auto_ready.session_id}/data.json")


async def test_c005c_partial_write_leaves_no_corrupt_data_json(
        monkeypatch, wired_bot, session_auto_ready):
    """원자적 쓰기 — 실패해도 기존 data.json이 손상되지 않는다.

    이것은 현재 설계가 지키고 있는 좋은 성질이므로 회귀 방지로 고정한다.
    """
    import core

    await core.save_session_data(wired_bot, session_auto_ready)
    path = f"sessions/{session_auto_ready.session_id}/data.json"
    before = open(path, encoding="utf-8").read()

    def _boom(*args, **kwargs):
        raise OSError("replace 실패")

    monkeypatch.setattr(os, "replace", _boom)
    session_auto_ready.turn_count = 999
    await core.save_session_data(wired_bot, session_auto_ready)

    after = open(path, encoding="utf-8").read()
    assert after == before, "실패한 저장이 기존 스냅샷을 훼손했습니다"


async def test_c005d_tmp_files_are_cleaned_after_failure(
        monkeypatch, wired_bot, session_auto_ready):
    """특성화 — 실패 시 남은 tmp 파일을 정리한다."""
    import core

    def _boom(*args, **kwargs):
        raise OSError("replace 실패")

    monkeypatch.setattr(os, "replace", _boom)
    await core.save_session_data(wired_bot, session_auto_ready)

    session_dir = f"sessions/{session_auto_ready.session_id}"
    leftovers = [f for f in os.listdir(session_dir) if f.endswith(".tmp")]
    assert leftovers == [], f"tmp 파일이 남았습니다: {leftovers}"


async def test_c005e_session_lock_is_per_session(wired_bot, session_auto_ready):
    """세션별 락이 생성된다 — 동시 저장 경합 방지 설계."""
    import core
    await core.save_session_data(wired_bot, session_auto_ready)
    assert session_auto_ready.session_id in wired_bot.session_io_locks
