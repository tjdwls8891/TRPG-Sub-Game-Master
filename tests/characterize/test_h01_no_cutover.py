# -*- coding: utf-8 -*-
"""H01 경계 고정 — 정의 컴파일러·스냅샷은 아직 어떤 플레이 경로에도 배선되지 않는다.

의도적으로 바뀌는 특성화 테스트다. 세션 바인딩·재시작 권위 전환(통합 로드맵 H06)과
새 엔진 cutover WP가 호출자를 추가할 때 이 테스트를 함께 갱신한다.

고정하는 것
  · production 코드가 정의 컴파일러·스냅샷을 부르지 않는다(H02 이후에도 0)
    → 캐시·프롬프트·판단층위·청구 경로에 정의 결합이 생기지 않았다.
  · world_ids(안정 digest)는 H02 정본 모듈만 재사용한다(narrative_seed·world_version·
    mutation digest). 그 밖의 production 코드는 부르지 않는다.
  · H01 모듈은 provider·Discord를 import하지 않고, 세션 저장 경로를 건드리지 않는다.
  · H02가 definition_revision·narrative_seed 세션 필드를 만들었지만 definition_revision은
    아직 어떤 정의에도 바인딩되지 않는다(기본 None, production writer 0).
"""
from __future__ import annotations

import ast
import os

import pytest

import core
from tests.conftest import REPO_ROOT, source_of

pytestmark = pytest.mark.characterize

H01_MODULES = ("world_ids", "definition_compiler", "definition_snapshot")
ALLOWED = {os.path.join("core", f"{m}.py") for m in H01_MODULES} | {os.path.join("core", "__init__.py")}


def _production_files():
    out = [os.path.join(REPO_ROOT, "main.py")]
    for base in ("core", "cogs"):
        for fn in sorted(os.listdir(os.path.join(REPO_ROOT, base))):
            if fn.endswith(".py"):
                out.append(os.path.join(REPO_ROOT, base, fn))
    return out


# H02: world_ids를 재사용해도 되는 정본 기반 모듈(정의 컴파일러·스냅샷은 여전히 금지)
WORLD_IDS_REUSERS = {os.path.join("core", f"{m}.py")
                     for m in ("world_state", "world_mutation", "canonical_mutation")}


def test_no_production_caller_of_h01_modules():
    hits = []
    for path in _production_files():
        rel = os.path.relpath(path, REPO_ROOT)
        if rel in ALLOWED:
            continue
        src = open(path, encoding="utf-8").read()
        for m in H01_MODULES:
            if m == "world_ids" and rel in WORLD_IDS_REUSERS:
                continue
            if m in src:
                hits.append((rel, m))
    assert hits == [], hits


def test_h01_modules_are_pure_no_provider_discord_or_session_save():
    banned_imports = {"google", "discord", "aiohttp", "requests"}
    banned_calls = {"save_session_data", "save_session_data_strict", "write_session_strict_locked",
                    "accrue", "call_with_retry", "append_cost_event"}
    for m in H01_MODULES:
        tree = ast.parse(source_of(f"core/{m}.py"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned_imports, m
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned_imports, m
                assert node.module not in ("io", "cache", "cost", "cost_ledger", "settlement",
                                           "commit_coordinator", "cache_lifecycle"), (m, node.module)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in banned_calls, (m, name)


def test_compiler_does_no_file_writes():
    """컴파일러는 읽기 전용이다 — 쓰기 모드 open이 없다(쓰기는 snapshot 모듈·도구만)."""
    tree = ast.parse(source_of("core/definition_compiler.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [k.value.value for k in node.keywords
                      if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            assert all(set(str(md)) <= {"r", "b"} for md in modes), modes


def test_definition_revision_field_exists_but_is_not_bound():
    """H02가 필드를 만들었으나 바인딩(H06)은 없다 — 기본 None, production writer 0."""
    assert core.SESSION_FIELDS["definition_revision"] is None
    assert core.TRPGSession("s", 1, 2, "x", {}).definition_revision is None
    assert "definition_snapshot" not in core.SESSION_FIELDS
    writers = []
    for path in _production_files():
        rel = os.path.relpath(path, REPO_ROOT)
        if rel == os.path.join("core", "world_state.py"):
            continue                    # 기본값(None) 초기화·마이그레이션만
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    # 세션 대상 쓰기만 본다(묶음·DTO 자기 속성 self.definition_revision 제외)
                    if (isinstance(t, ast.Attribute) and t.attr == "definition_revision"
                            and not (isinstance(t.value, ast.Name) and t.value.id == "self")):
                        writers.append(rel)
    assert writers == [], writers
