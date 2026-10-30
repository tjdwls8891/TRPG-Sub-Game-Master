# -*- coding: utf-8 -*-
"""H01 경계 고정 — 정의 컴파일러·스냅샷은 아직 어떤 플레이 경로에도 배선되지 않는다.

의도적으로 바뀌는 특성화 테스트다. 세션 바인딩·재시작 권위 전환(통합 로드맵 H06)과
새 엔진 cutover WP가 호출자를 추가할 때 이 테스트를 함께 갱신한다.
H06 갱신: 정의 컴파일·스냅샷 쓰기·바인딩 owner = core.world_lifecycle 하나. 읽기 소비자(player_world·
cache_source)는 스냅샷만 읽는다(재컴파일 대체 없음).

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
# H04: 행위자·정보·사건 도메인 모듈도 안정 digest(ActorId·InfoId·EventInstanceId)를 재사용한다.
from tests.characterize.h04_scope import H04_DOMAIN_MODULES  # noqa: E402
WORLD_IDS_REUSERS = {os.path.join("core", f"{m}.py")
                     for m in ("world_state", "world_mutation", "canonical_mutation") + H04_DOMAIN_MODULES
                     + ("player_world", "turn_world", "pursuit")}   # v7.48 추적자(사건 지역 상태 도메인)
# H05: 바인딩된 정의의 읽기 전용 소비자(쓰기·바인딩 없음 — 아래 writer 테스트가 계속 고정)
# H06: 생애주기 owner(world_lifecycle — 컴파일·스냅샷 쓰기·바인딩)와 캐시 원천(cache_source — 스냅샷 읽기)
DEFINITION_READERS = {os.path.join("core", "player_world.py"), os.path.join("core", "cache_source.py")}
DEFINITION_BINDER = os.path.join("core", "world_lifecycle.py")
WORLD_IDS_REUSERS = WORLD_IDS_REUSERS | {DEFINITION_BINDER}


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
            if rel == DEFINITION_BINDER and m in ("definition_compiler", "definition_snapshot"):
                continue
            if rel in DEFINITION_READERS and m == "definition_snapshot":
                continue                    # 읽기 소비자는 스냅샷만(컴파일러 재컴파일 대체 없음 — 아래로 걸린다)
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


def test_definition_revision_single_writer_is_lifecycle_binder():
    """H06: 기본 None. production writer는 world_lifecycle(bind_definition) 하나 — 복원 레지스트리 외 직접 쓰기 없음."""
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
    assert sorted(set(writers)) == [DEFINITION_BINDER], writers
    # 스냅샷 쓰기 API는 생애주기 owner만 부른다
    for path in _production_files():
        rel = os.path.relpath(path, REPO_ROOT)
        if rel in (DEFINITION_BINDER, os.path.join("core", "definition_snapshot.py")):
            continue
        called = {getattr(n.func, "attr", None) for n in ast.walk(ast.parse(open(path, encoding="utf-8").read()))
                  if isinstance(n, ast.Call)}
        assert "write_snapshot" not in called, rel


def test_h05_definition_reader_is_read_only_and_gated_on_binding():
    """H05 player_world는 스냅샷 쓰기 API를 부르지 않고, 바인딩 없는 세션은 LEGACY다."""
    tree = ast.parse(source_of("core/player_world.py"))
    called = {getattr(n.func, "attr", None) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & {"write_snapshot", "build_envelope", "_write_json_durable"}
    assert "compile_scenario" not in called            # H06: 스냅샷만 읽는다(재컴파일 대체 없음)
    assert core.player_world.session_world_context(core.TRPGSession("s", 1, 2, "x", {})).mode == "LEGACY"
