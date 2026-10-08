# -*- coding: utf-8 -*-
"""H04 경계 고정 — 행위자·정보·인지·사건·세계 진행 엔진은 있지만 플레이어 경로는 그대로다.

의도적으로 바뀌는 특성화 테스트다. 플레이어 턴 orchestration(H05), 초기화·관리자 배선(H06),
legacy 퀘스트·info_ledger 권위 은퇴(H07)가 들어올 때 함께 갱신한다.
H05 갱신: 플레이어 경로 소비자는 core.action_intent·player_world·turn_world뿐이며 cogs는 H04 모듈을
직접 부르지 않는다(H05 경계는 test_h05_wiring).

H04-R21  CurrentNpcProjection 정본 저장 없음(production 사용처 없음)
H04-R25  자유 조건·eval 없음
H04-R40  일반 dotted 정본 writer 없음
H04-R62  플레이어·cogs H04 producer 없음
H04-R63  provider 호출 없음
H04-R65  legacy 퀘스트·narrative_plan 권위 불변
"""
from __future__ import annotations

import ast
import os

import pytest

from tests.characterize.h04_scope import H04_DOMAIN_MODULES, H04_DOMAIN_FILES, expected_production_ops
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.characterize


def _files():
    out = [os.path.join(REPO_ROOT, "main.py"), os.path.join(REPO_ROOT, "prompts.py")]
    for base in ("core", "cogs"):
        for fn in sorted(os.listdir(os.path.join(REPO_ROOT, base))):
            if fn.endswith(".py"):
                out.append(os.path.join(REPO_ROOT, base, fn))
    return out


def _rel(p):
    return os.path.relpath(p, REPO_ROOT)


def _tree(p):
    return ast.parse(open(p, encoding="utf-8").read())


def _imports(tree, module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == module for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if (node.module and node.module.split(".")[-1] == module) or any(a.name == module for a in node.names):
                return True
        if isinstance(node, ast.Attribute) and node.attr == module and isinstance(node.value, ast.Name) \
                and node.value.id == "core":
            return True
    return False


def test_h04_modules_have_no_player_or_cogs_caller():
    """cogs·main은 H04 모듈을 직접 부르지 않는다 — H05 플레이어 계획 모듈만 경유한다."""
    from tests.characterize.h05_scope import H05_FILES
    # H06: 초기 세계 계획(world_lifecycle)이 시작 시점 전수 검사·wakeup을 만든다(INITIALIZATION owner)
    allowed = (set(H04_DOMAIN_FILES) | {"__init__.py", "world_mutation.py", "definition_compiler.py"}
               | set(H05_FILES) | {"world_lifecycle.py"})
    hits = []
    for p in _files():
        base = os.path.basename(p)
        if p.startswith(os.path.join(REPO_ROOT, "core")) and base in allowed:
            continue
        tree = _tree(p)
        hits += [(_rel(p), m) for m in H04_DOMAIN_MODULES if _imports(tree, m)]
    assert hits == [], hits


def test_no_production_producer_of_h04_operations_or_progression():
    import core
    h04_ops = expected_production_ops() - set(core.world_schedule.TIME_OP_TYPES) - set(core.spatial.SPATIAL_OP_TYPES)
    names = {"project_world_progression", "build_dependency_index", "project_npc", "build_supersede_ops",
             "reevaluate_series", "build_series_init_ops", "initial_template_wakeups", "start_movement_step"}
    from tests.characterize.h05_scope import H05_FILES
    domain = (set(H04_DOMAIN_FILES) | {"__init__.py", "world_mutation.py", "definition_compiler.py"}
              | set(H05_FILES) | {"world_lifecycle.py"})                # H06: INITIALIZATION producer
    hits = []
    for p in _files():
        if p.startswith(os.path.join(REPO_ROOT, "core")) and os.path.basename(p) in domain:
            continue
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in h04_ops:
                hits.append((_rel(p), node.value))
            n = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else None
            if n in names:
                hits.append((_rel(p), n))
    assert hits == [], hits


def test_h04_modules_have_no_provider_eval_or_generic_writer():
    banned_mod = {"google", "discord", "aiohttp", "requests", "prompts", "random"}
    banned_core = {"cost", "cost_ledger", "settlement", "ink_transactions", "accounts", "cache", "cache_lifecycle",
                   "prompt", "resilience", "dialogue", "extraction", "io", "commit_coordinator", "turn_preparation",
                   "canonical_mutation", "quest", "quest_filter", "places", "timeline"}
    banned_calls = {"eval", "exec", "compile", "__import__", "setattr", "call_with_retry", "generate_content",
                    "save_session_data", "save_session_data_strict", "accrue", "hash"}
    for m in H04_DOMAIN_MODULES:
        for node in ast.walk(_tree(os.path.join(REPO_ROOT, "core", f"{m}.py"))):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned_mod, m
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    assert node.module.split(".")[0] not in banned_mod, (m, node.module)
                if node.level == 1:
                    names = {node.module} if node.module else {a.name for a in node.names}
                    assert not names & banned_core, (m, names)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or (node.func.attr if isinstance(node.func, ast.Attribute)
                                                          and node.func.attr in ("eval", "exec", "setattr") else None)
                assert name not in banned_calls, (m, name)


def test_production_registry_has_no_generic_path_writer():
    import core
    names = set(core.world_mutation.PRODUCTION_REGISTRY.op_types())
    assert names == expected_production_ops()
    assert not any(t in n for n in names for t in ("PATH", "SET_ANY", "GENERIC", "DOTTED", "SCRIPT", "MUTATE_WORLD"))


def test_legacy_quest_and_narrative_plan_authority_untouched():
    """퀘스트·narrative_plan·info_ledger 경로는 H04 모듈을 읽지 않고, H04 모듈도 그것을 읽지 않는다."""
    for rel in ("core/quest.py", "core/quest_filter.py", "cogs/gm.py", "cogs/game.py", "core/extraction.py",
                "core/prompt.py", "core/cache.py"):
        tree = _tree(os.path.join(REPO_ROOT, rel))
        for m in H04_DOMAIN_MODULES:
            assert not _imports(tree, m), (rel, m)
    legacy = {"info_ledger", "known_secrets", "secret_known", "narrative_plan", "quest_state", "active_quest"}
    for m in H04_DOMAIN_MODULES:
        for node in ast.walk(_tree(os.path.join(REPO_ROOT, "core", f"{m}.py"))):
            if isinstance(node, ast.Attribute):
                assert node.attr not in legacy, (m, node.attr)
