# FrozenCacheBuildSource — provider 캐시 생성 직전에 고정한 불변 원천 (H06-C)
#
# [권위 — 통합 설계 §50 · DESIGN 11 FrozenCacheBuildSource · H06 handoff §5]
#   캐시는 provider 최적화이지 정본이 아니다. 생성·재발급은
#     짧은 임계 구역(io 락)에서 불변 DTO 캡처 → 락 해제 → DTO로 룰북 조립 → provider 생성
#   순서로만 한다. 네트워크 호출(토큰 계산·생성) 동안 정본 변이 락을 쥐지 않으며, 그동안 세계가
#   V20 → V21로 나아가도 캐시는 V20 기준(base_world_version)으로 남고 V21 런타임 투영이 덮는다.
#   · 동적 세계가 바인딩된 세션의 원문은 정의 스냅샷의 legacy_source(현재 원본 파일·재로드 아님).
#   · DTO는 세션 상태를 바꾸지 않는다(기본 NPC 재전개·세계 리셋·world_version 전진 없음).
#   · 생성 완료 시 정의 revision이 DTO와 다르면 그 세대는 활성화하지 않는다(cache_lifecycle).
from __future__ import annotations

import copy
import types
from dataclasses import dataclass, field

from . import world_state as _ws

# 룰북 조립(build_scenario_cache_text)·출처 표식이 읽는 세션 속성 — 깊은 사본으로 고정
_CORE_ATTRS = ("session_id", "scenario_id", "players", "npcs", "resources", "statuses",
               "compressed_memory", "cached_compressed_memory", "player_faction", "turn_count")


@dataclass(frozen=True)
class FrozenCacheBuildSource:
    session_id: str
    definition_revision: str | None
    source_world_version: dict | None
    history_marker: object
    scenario_data: dict
    view: object                                      # 읽기 전용 세션 사본(SimpleNamespace)
    compressed_memory: str = ""
    cached_compressed_memory: str = ""
    session_npc_base: dict = field(default_factory=dict)

    @property
    def base_world_version_id(self):
        return _ws.version_id_of(self.source_world_version)

    def meta(self) -> dict:
        """CacheGenerationMeta(생성 사실에 함께 기록)."""
        return {"definition_revision": self.definition_revision,
                "base_world_version_id": self.base_world_version_id,
                "history_marker": copy.deepcopy(self.history_marker)}


def bound_scenario_data(session) -> dict:
    """세션이 쓰는 시나리오 원문. 동적 세계가 바인딩된 세션은 정의 스냅샷의 legacy_source(현재 파일 아님)."""
    from . import world_lifecycle as _wl
    rev = getattr(session, "definition_revision", None)
    if rev and _wl.world_bound(session):
        from . import definition_snapshot as _snap
        snap = _snap.read_snapshot(session.session_id, expected_revision=rev)   # 실패는 예외(조용한 대체 없음)
        return copy.deepcopy(snap.definition["legacy_source"]["scenario"])
    return copy.deepcopy(getattr(session, "scenario_data", None) or {})


def _copy(v):
    try:
        return copy.deepcopy(v)
    except Exception:  # noqa: BLE001 — provider 객체 등 복사 불가 값은 원천에 필요 없다
        return None


def capture(session) -> FrozenCacheBuildSource:
    """동기 캡처(await 없음). 호출자가 짧은 임계 구역 안에서 부른다."""
    from .io import SESSION_FIELDS
    from .turn_history import cache_marker_now
    scenario = bound_scenario_data(session)
    attrs = {k: _copy(getattr(session, k, None)) for k in set(SESSION_FIELDS) | set(_CORE_ATTRS)}
    attrs["scenario_data"] = scenario
    view = types.SimpleNamespace(**attrs)
    default = set((scenario.get("default_npcs") or {}))
    npc_base = {name: {**dict(data), "resources": dict((attrs["resources"] or {}).get(name, {})),
                       "statuses": list((attrs["statuses"] or {}).get(name, []))}
                for name, data in (attrs["npcs"] or {}).items() if name not in default and isinstance(data, dict)}
    return FrozenCacheBuildSource(
        session_id=str(session.session_id),
        definition_revision=getattr(session, "definition_revision", None),
        source_world_version=_copy(getattr(session, "world_version", None)),
        history_marker=cache_marker_now(session),
        scenario_data=scenario, view=view,
        compressed_memory=attrs.get("compressed_memory") or "",
        cached_compressed_memory=attrs.get("cached_compressed_memory") or "",
        session_npc_base=npc_base)


async def capture_locked(bot, session) -> FrozenCacheBuildSource:
    """짧은 임계 구역 — 세션 io 락(정본 반영과 같은 락) 안에서만 캡처하고 즉시 놓는다."""
    from . import io as _io
    async with _io.session_io_lock(bot, session):
        return capture(session)
