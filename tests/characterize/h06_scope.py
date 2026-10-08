# -*- coding: utf-8 -*-
"""H06 모듈 목록(특성화 스캔 공유). 동적 세계 생애주기의 유일한 owner.

  world_lifecycle   정의 바인딩(유일한 definition_revision writer) · 초기 세계 계획 · INITIALIZATION ·
                    생애주기(INITIALIZED/OPEN/CLOSED) · 재시작 검증 · 종료 안전 지점
  cache_source      FrozenCacheBuildSource(캐시 원천 고정 — 바인딩 스냅샷 원문)
  admin_world_edit  typed ADMIN_EDIT 서비스

CanonicalMutationCoordinator의 production 호출자는 world_lifecycle(INITIALIZATION)·admin_world_edit(ADMIN_EDIT)뿐이다.
"""
H06_MODULES = ("world_lifecycle", "cache_source", "admin_world_edit")
H06_FILES = tuple(f"{m}.py" for m in H06_MODULES)
CANONICAL_MUTATION_CALLERS = {"world_lifecycle.py": "CAUSE_INITIALIZATION", "admin_world_edit.py": "CAUSE_ADMIN_EDIT"}
