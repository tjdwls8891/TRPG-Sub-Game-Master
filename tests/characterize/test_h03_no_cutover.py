# -*- coding: utf-8 -*-
"""H03 경계 고정 — 시간·공간 서비스와 typed handler는 있지만 플레이어 경로는 그대로다.

의도적으로 바뀌는 특성화 테스트다. 플레이어 자연어 턴 → 이동/시간 연산 배선(H05),
초기화·관리자 배선(H06), legacy places/timeline 권위 은퇴(H07)가 들어올 때 함께 갱신한다.

H03-R40  H03 연산 producer에 플레이어·cogs 호출자 없음
H03-R41  legacy places/timeline production 경로 불변(새 모듈을 읽지 않음)
H03-R42  새 모듈은 provider·Discord·재무·프롬프트를 쓰지 않는다(provider 호출 수 불변)
"""
from __future__ import annotations

import ast
import os

import pytest

from core import spatial as SP
from core import world_schedule as SC
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.characterize

H03_MODULES = ("world_clock", "world_schedule", "spatial", "access", "movement")
OP_TYPES = set(SC.TIME_OP_TYPES) | set(SP.SPATIAL_OP_TYPES)
PRODUCER_NAMES = {"traversal_ops", "placement_at", "plan_actual_traversal", "plan_known_routes",
                  "seed_knowledge"} | {n for n in dir(SC) + dir(SP) if n.startswith("OP_")}


def _production_files():
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


def _imports_module(tree, module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[-1] == module for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[-1] == module:
                return True
            if any(a.name == module for a in node.names):
                return True
        # world_clock·world_schedule은 세션 필드명과 같다 — 모듈 접근(core.X)만 센다
        if isinstance(node, ast.Attribute) and node.attr == module \
                and isinstance(node.value, ast.Name) and node.value.id == "core":
            return True
    return False


def test_h03_modules_are_used_only_by_allowed_core_modules():
    # 허용: 도메인 모듈끼리, 재수출, 등록(world_mutation), READY·정본 변이의 quiescence 증명,
    # 컴파일러의 달력 검증(world_clock).
    from tests.characterize.h04_scope import H04_DOMAIN_FILES
    from tests.characterize.h05_scope import H05_FILES
    # H04 도메인 모듈(자체 경계는 test_h04_no_cutover) + H05 플레이어 계획 모듈(유일한 플레이어 경로 소비자)
    h04 = set(H04_DOMAIN_FILES) | set(H05_FILES)
    allowed = {
        "world_clock": {"world_schedule.py", "spatial.py", "definition_compiler.py"} | h04,
        "world_schedule": {"world_mutation.py", "turn_preparation.py", "canonical_mutation.py", "movement.py"} | h04,
        "spatial": {"world_mutation.py", "movement.py"} | h04,
        "access": {"movement.py", "spatial.py"} | h04,
        "movement": set() | h04,
    }
    hits = []
    for p in _production_files():
        rel = _rel(p)
        tree = _tree(p)
        for m in H03_MODULES:
            if os.path.basename(p) in (f"{m}.py", "__init__.py"):
                continue
            if _imports_module(tree, m) and not (rel.startswith("core") and os.path.basename(p) in allowed[m]):
                hits.append((rel, m))
    assert hits == [], hits


def test_no_production_producer_of_h03_operations():
    """cogs·main·기존 core 경로가 H03 연산을 직접 만들거나 이동 계획을 세우지 않는다(H05 모듈만 경유)."""
    from tests.characterize.h04_scope import H04_DOMAIN_FILES
    from tests.characterize.h05_scope import H05_FILES
    domain = ({f"{m}.py" for m in H03_MODULES} | {"world_mutation.py", "__init__.py"} | set(H04_DOMAIN_FILES)
              | set(H05_FILES))
    hits = []
    for p in _production_files():
        if p.startswith(os.path.join(REPO_ROOT, "core")) and os.path.basename(p) in domain:
            continue
        for node in ast.walk(_tree(p)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in OP_TYPES:
                hits.append((_rel(p), node.value))
            name = getattr(node, "attr", None) if isinstance(node, ast.Attribute) else (
                node.id if isinstance(node, ast.Name) else None)
            if name in PRODUCER_NAMES:
                hits.append((_rel(p), name))
    assert hits == [], hits


def test_legacy_place_and_time_paths_do_not_read_new_domains():
    """legacy 이동(places.py BFS)·시간(timeline.py)·자동 GM 경로는 새 도메인을 읽지 않는다."""
    new_fields = ("world_clock", "world_schedule", "spatial_state", "spatial_knowledge",
                  "access_control_state")
    for rel in ("core/places.py", "core/timeline.py", "cogs/gm.py", "cogs/game.py", "core/extraction.py"):
        tree = _tree(os.path.join(REPO_ROOT, rel))
        for m in H03_MODULES:
            assert not _imports_module(tree, m), (rel, m)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in new_fields, (rel, node.attr)


def test_h03_modules_have_no_provider_discord_finance_or_prompt():
    banned_mod = {"google", "discord", "aiohttp", "requests", "prompts"}
    banned_core = {"cost", "cost_ledger", "settlement", "ink_transactions", "accounts", "cache",
                   "cache_lifecycle", "prompt", "resilience", "dialogue", "extraction", "io",
                   "commit_coordinator", "turn_preparation", "canonical_mutation", "places", "timeline"}
    banned_calls = {"call_with_retry", "accrue", "generate_content", "record_cost_event_strict",
                    "save_session_data", "save_session_data_strict"}
    for m in H03_MODULES:
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
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                assert name not in banned_calls, (m, name)
