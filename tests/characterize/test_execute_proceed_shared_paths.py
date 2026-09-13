"""C-004 — `_execute_proceed` 공유 호출부 특성화.

WP00_EXECUTABLE_TEST_PLAN.md §5 기준.

목적 — AI 작업자가 비(非)턴 호출부를 인지하지 못한 채 트랜잭션 생성을
`_execute_proceed` 안으로 옮기는 것을 막는다.
"""

from __future__ import annotations

import ast

import pytest

from tests.conftest import source_of

pytestmark = pytest.mark.characterize

WATCHED = (
    "cogs/gm.py",
    "cogs/game.py",
    "cogs/session.py",
    "cogs/character.py",
)


def _callsites(func_name: str) -> list[tuple[str, int, str]]:
    """호출부를 (파일, 줄, 소속 함수)로 반환한다. 정의는 제외한다."""
    out = []
    for path in WATCHED:
        src = source_of(path)
        if func_name not in src:
            continue
        tree = ast.parse(src)
        lines = src.splitlines()

        def owner(ln: int) -> str:
            best = None
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if n.lineno <= ln <= (n.end_lineno or 0):
                        if best is None or n.lineno > best.lineno:
                            best = n
            return best.name if best else "<module>"

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name != func_name:
                continue
            out.append((path, node.lineno, owner(node.lineno)))
    return sorted(out)


def test_c004_execute_proceed_has_three_distinct_callers():
    """C-004 — 자동 GM · 수동 · 인트로 세 경로가 같은 함수를 공유한다.

    이 사실이 WP-01의 `transaction_id=None` 경로 요구와 직결된다.
    """
    sites = _callsites("_execute_proceed")
    owners = {owner for _, _, owner in sites}

    assert "_dispatch_proceed" in owners, "자동 GM 경로가 사라졌습니다"
    assert "proceed_turn" in owners, "수동 !진행 경로가 사라졌습니다"
    assert "play_intro" in owners, "인트로 경로가 사라졌습니다"

    files = {path for path, _, _ in sites}
    assert files == {"cogs/gm.py", "cogs/game.py", "cogs/session.py"}, (
        f"호출 파일 집합이 달라졌습니다: {sorted(files)}")


def test_c004b_execute_proceed_is_defined_once():
    """정의가 하나여야 공유 사실이 성립한다."""
    src = source_of("cogs/game.py")
    tree = ast.parse(src)
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_execute_proceed"]
    assert len(defs) == 1


def test_c004c_only_automatic_path_reaches_finish_proceed():
    """특성화 — 수동·인트로 경로는 `_finish_proceed_and_continue`를 거치지 않는다.

    따라서 턴 카운터·되감기 델타·잉크 차감도 그 경로에서는 일어나지 않는다.
    WP-01은 이 비대칭을 유지해야 한다.
    """
    sites = _callsites("_finish_proceed_and_continue")
    owners = {owner for _, _, owner in sites}

    assert owners <= {"_run_gm_logic_loop", "_continue_with_roll_results",
                      "_finish_proceed_and_continue"}, (
        f"예상 밖 호출부: {sorted(owners)}")
    assert "proceed_turn" not in owners
    assert "play_intro" not in owners


def test_c004d_dispatch_proceed_is_the_only_automatic_wrapper():
    """`_dispatch_proceed`가 자동 경로의 단일 진입점이다."""
    sites = _callsites("_dispatch_proceed")
    owners = {owner for _, _, owner in sites}
    assert owners == {"_finish_proceed_and_continue"}, (
        f"자동 묘사 진입점이 늘었습니다: {sorted(owners)}")


def test_c004e_run_extraction_callers():
    """추출층위 호출부를 고정한다.

    D-002(커밋 배리어)와 WP-01 식별자 전파의 기준점이다.
    """
    sites = _callsites("_run_extraction")
    owners = {owner for _, _, owner in sites}
    # v5.33.0 — 자동 묘사 후처리와 추출 재시도 버튼 두 곳.
    assert "_dispatch_proceed" in owners, "자동 추출 경로가 사라졌습니다"
    assert len(owners) >= 1
