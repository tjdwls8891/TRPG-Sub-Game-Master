"""커널 — ID·정규 직렬화·엔티티 세계·컴포넌트 등록·효과·투영·버전·스냅샷·턴 기록.

표준 라이브러리 밖의 어떤 모듈도 import하지 않는다(계층 테스트로 고정). 규칙의 의미는 모른다 —
컴포넌트를 등록한 기능 모듈이 효과 연산의 뜻을 정하고, 커널은 그 결과 쓰기를 합치고 검증한다.
"""
from .canon import CanonicalError, canonical_json, content_digest, stable_digest
from .components import Component, Registry, View, Write, write
from .effects import (Conflict, Effect, EffectBatch, Projection, make_effect, project,
                      PROJECTION_CONFLICT)
from .errors import KernelError, KernelStoreError
from .snapshot import SnapshotStore
from .turn_record import TurnLog, TurnRecord
from .version import CAUSE_KINDS, next_version
from .world import ENTITY_KINDS, WORLD, World

__all__ = [
    "CanonicalError", "canonical_json", "content_digest", "stable_digest",
    "Component", "Registry", "View", "Write", "write",
    "Conflict", "Effect", "EffectBatch", "Projection", "make_effect", "project", "PROJECTION_CONFLICT",
    "KernelError", "KernelStoreError", "SnapshotStore", "TurnLog", "TurnRecord",
    "CAUSE_KINDS", "next_version", "ENTITY_KINDS", "WORLD", "World",
]
