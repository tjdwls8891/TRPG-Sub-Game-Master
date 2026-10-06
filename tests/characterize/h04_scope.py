# -*- coding: utf-8 -*-
"""H04 도메인 모듈 목록(특성화 스캔 공유). 플레이어·cogs 경로 배선은 H05."""
H04_DOMAIN_MODULES = ("world_refs", "actors", "information", "cognition", "relationships",
                      "npc_projection", "predicates", "events", "world_progression")
H04_DOMAIN_FILES = tuple(f"{m}.py" for m in H04_DOMAIN_MODULES)


def expected_production_ops() -> set:
    """production 레지스트리가 담아야 할 typed 연산 = 도메인 모듈 *_OP_TYPES 합집합(H03 + H04)."""
    import core
    out = set(core.world_schedule.TIME_OP_TYPES) | set(core.spatial.SPATIAL_OP_TYPES)
    out |= set(core.actors.ACTOR_OP_TYPES) | set(core.information.INFORMATION_OP_TYPES)
    out |= set(core.cognition.COGNITION_OP_TYPES) | set(core.relationships.RELATIONSHIP_OP_TYPES)
    events = getattr(core, "events", None)
    if events is not None:
        out |= set(events.EVENT_OP_TYPES)
    return out
