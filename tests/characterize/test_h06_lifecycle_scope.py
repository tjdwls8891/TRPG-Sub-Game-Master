# -*- coding: utf-8 -*-
"""H06 경계 고정 — 동적 세계 생애주기 owner · provider/재무 직접 호출 없음 · DYNAMIC 판정 단일 관문.

의도적으로 바뀌는 특성화 테스트다(H07 cutover가 호출자를 바꿀 때 함께 갱신).
  · H06 모듈은 provider를 부르지 않고(google·generate_content 없음) 잉크·CostEvent를 직접 만들지 않는다
    (세션 종료 재무는 기존 캐시 생애주기 단일 finalizer close_window를 거친다).
  · DYNAMIC 판정은 player_world.session_world_context 하나 — 호출처는 gm(턴 세계·재서술)·turn_world·
    world_lifecycle뿐이다(레거시 경로는 판정하지 않는다).
  · 정의 원문(scenario_data)을 현재 파일에서 재로드하는 경로는 바인딩 세션을 건너뛴다(!캐시 재발급).
"""
from __future__ import annotations

import ast
import os

import pytest

from tests.characterize.h06_scope import H06_FILES
from tests.conftest import REPO_ROOT, source_of

pytestmark = pytest.mark.characterize


def _prod():
    out = [os.path.join(REPO_ROOT, "main.py")]
    for base in ("core", "cogs"):
        for fn in sorted(os.listdir(os.path.join(REPO_ROOT, base))):
            if fn.endswith(".py"):
                out.append(os.path.join(REPO_ROOT, base, fn))
    return out


def test_h06_modules_have_no_provider_or_direct_finance():
    banned_calls = {"generate_content", "add_ink", "deduct_ink", "append_cost_event", "accrue",
                    "call_with_retry", "_provider_create"}
    for f in H06_FILES:
        tree = ast.parse(source_of(f"core/{f}"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if getattr(node, "module", None) else [])
                assert not any(n and n.split(".")[0] in ("google", "discord") for n in names), f
            if isinstance(node, ast.Call):
                n = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert n not in banned_calls, (f, n)


def test_dynamic_eligibility_has_single_gate_and_known_callers():
    callers = set()
    for p in _prod():
        rel = os.path.relpath(p, REPO_ROOT)
        if rel == os.path.join("core", "player_world.py"):
            continue
        if "session_world_context(" in open(p, encoding="utf-8").read():
            callers.add(rel)
    assert callers == {os.path.join("cogs", "gm.py"), os.path.join("core", "turn_world.py"),
                       os.path.join("core", "world_lifecycle.py")}, callers


def test_bound_session_skips_source_reload_on_manual_reissue():
    src = source_of("cogs/system.py")
    i = src.index("core.world_lifecycle.world_bound(session)")
    j = src.index("core.load_scenario_from_file(session.scenario_id)")
    assert i < j                                      # 바인딩 판정이 재로드보다 먼저


def test_prompts_untouched_by_h06_runtime():
    for f in H06_FILES:
        assert "prompts" not in {getattr(n, "module", None) for n in ast.walk(ast.parse(source_of(f"core/{f}")))
                                 if isinstance(n, ast.ImportFrom)}
