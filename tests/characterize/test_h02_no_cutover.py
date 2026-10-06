# -*- coding: utf-8 -*-
"""H02 경계 고정 — 세계 변이 lane은 배선됐지만 실제 도메인 연산·관리자/초기화 경로는 없다.

의도적으로 바뀌는 특성화 테스트다. 시간·공간 handler(통합 로드맵 H03), INITIALIZATION·
ADMIN_EDIT 배선(H06)이 들어올 때 이 테스트를 함께 갱신한다.

H02-R24  공통 정본 변이 primitive(canonical_mutation)의 production 호출자 0
H02-R26  새 모듈은 provider·Discord를 쓰지 않는다(판단층위 캐시·provider 호출 수 불변)
         + world_version 직접 writer = CommitCoordinator·canonical_mutation(·world_state 초기화)뿐
         + production 연산 레지스트리는 비어 있다(임의 경로 setter 없음)
"""
from __future__ import annotations

import ast
import os

import pytest

from core import world_mutation as WM
from core import world_state as WS
from tests.conftest import REPO_ROOT, source_of

pytestmark = pytest.mark.characterize

NEW_MODULES = ("world_state", "world_mutation", "canonical_mutation")


def _production_files():
    out = [os.path.join(REPO_ROOT, "main.py")]
    for base in ("core", "cogs"):
        for fn in sorted(os.listdir(os.path.join(REPO_ROOT, base))):
            if fn.endswith(".py"):
                out.append(os.path.join(REPO_ROOT, base, fn))
    return out


def _rel(p):
    return os.path.relpath(p, REPO_ROOT)


def _references(path, module: str) -> bool:
    """주석이 아닌 실제 참조(import·속성 접근·이름)만 본다."""
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.Import) and any(module in a.name for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (
                (node.module and module in node.module) or any(a.name == module for a in node.names)):
            return True
        if isinstance(node, ast.Attribute) and node.attr == module:
            return True
        if isinstance(node, ast.Name) and node.id == module:
            return True
    return False


def test_canonical_mutation_has_no_production_caller():
    allowed = {os.path.join("core", "canonical_mutation.py"), os.path.join("core", "__init__.py")}
    hits = [_rel(p) for p in _production_files()
            if _rel(p) not in allowed and _references(p, "canonical_mutation")]
    assert hits == [], hits


def test_world_mutation_users_are_the_commit_owners_only():
    allowed = {os.path.join("core", f) for f in (
        "world_mutation.py", "canonical_mutation.py", "turn_preparation.py",
        "commit_coordinator.py", "__init__.py")}
    hits = [_rel(p) for p in _production_files()
            if _rel(p) not in allowed and _references(p, "world_mutation")]
    assert hits == [], hits


def test_world_version_direct_writers():
    allowed = {os.path.join("core", f) for f in (
        "commit_coordinator.py", "canonical_mutation.py", "world_state.py")}
    writers = []
    for p in _production_files():
        for node in ast.walk(ast.parse(open(p, encoding="utf-8").read())):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute) and t.attr in WS.GAMEPLAY_WORLD_FIELDS
                            and not (isinstance(t.value, ast.Name) and t.value.id == "self")):
                        writers.append((_rel(p), t.attr))
    assert {w for w in writers if w[0] not in allowed} == set(), writers
    assert {f for p, f in writers} <= {"world_version"}, writers   # 다른 도메인은 apply_projection만


def test_production_registry_is_empty_and_never_registered():
    assert WM.PRODUCTION_REGISTRY.op_types() == ()
    for p in _production_files():
        assert "PRODUCTION_REGISTRY.register" not in open(p, encoding="utf-8").read(), _rel(p)


def test_new_modules_have_no_provider_or_discord_or_finance():
    banned_mod = {"google", "discord", "aiohttp", "requests"}
    banned_core = {"cost", "cost_ledger", "settlement", "ink_transactions", "accounts",
                   "cache", "cache_lifecycle", "prompt", "resilience", "definition_compiler",
                   "definition_snapshot"}
    banned_calls = {"call_with_retry", "accrue", "generate_content", "record_cost_event_strict"}
    for m in NEW_MODULES:
        tree = ast.parse(source_of(f"core/{m}.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned_mod, m
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    assert node.module.split(".")[0] not in banned_mod, (m, node.module)
                if node.level == 1:
                    names = {node.module} if node.module else {a.name for a in node.names}
                    assert not names & banned_core, (m, names)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in banned_calls, (m, name)
