# CurrentNpcProjection — NPC 현재 상태 읽기 전용 조립 (H04-A)
#
# [권위 — DESIGN 05 §82~95 · H04 결정 게이트 A13]
#   필요할 때 조립하는 파생 묶음이다. 세션·정본에 저장하지 않는다(project_npc는 순수 함수).
#   출처:
#     · 정의: actor_definitions[*].profile_base(정제된 저작 프로필 — legacy_source를 읽지 않는다)
#     · actor_registry(표시 이름·aliases·세션 NPC)
#     · spatial_state.placements(현재 위치/TRANSIT — 기록 없음 = 추적하지 않음, '없음'이 아니다)
#     · H07 유효 위치(core.npc_position): 정본 override > 평상시 일과(정의+시계 파생) > untracked
#     · 레거시 읽기 adapter(resources·statuses·companions·met_npcs — 이름 키, 읽기만)
#     · event_instances(ACTIVE 사건의 bound slot 역할)
#     · cognition_state(relevant_info_ids로 지정한 정보만 — 전부 넣지 않는다)
#     · relationship_state(이 NPC가 끝인 delta)
#     · 미디어 정체성(비정규 NPC 등록부 — 이름 키, 읽기만)
#   장소 연관(places[*].npcs → npc_associations)은 현재 위치가 아니다 — place_associations로 따로 둔다.
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import actors as _actors
from . import cognition as _cog
from . import relationships as _rel
from . import world_mutation as _wm


@dataclass(frozen=True)
class LegacyRuntime:
    """레거시 이름 키 상태의 읽기 전용 사본(세션 객체를 들고 다니지 않는다)."""
    npcs: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    statuses: dict = field(default_factory=dict)
    companions: tuple = ()
    met_npcs: tuple = ()
    irregular_npcs: dict = field(default_factory=dict)

    @classmethod
    def from_session(cls, session) -> "LegacyRuntime":
        g = lambda n, d: copy.deepcopy(getattr(session, n, d) or d)
        return cls(g("npcs", {}), g("resources", {}), g("statuses", {}), tuple(g("companions", [])),
                   tuple(g("met_npcs", [])), g("irregular_npcs", {}))


def _dom(world, name):
    return (world.domain(name) if isinstance(world, _wm.ProjectedWorld) else getattr(world, name, None)) or {}


def project_npc(actor_id: str, definition: dict, world, *, relevant_info_ids=(), legacy_runtime=None) -> dict:
    """한 NPC의 현재 투영(dict). 알 수 없는 ActorId면 None."""
    adef = (definition.get("actor_definitions") or {}).get(actor_id)
    reg = _actors.registry_of(world).get(actor_id)
    if adef is None and reg is None:
        return None
    legacy = legacy_runtime or LegacyRuntime()
    name = _actors.display_name(definition, world, actor_id)
    legacy_name = _actors.legacy_name_of(definition, world, actor_id)
    placement = ((_dom(world, "spatial_state").get("placements")) or {}).get(actor_id)
    from . import npc_position as _npos
    pos = _npos.effective_position(definition, world, actor_id)
    holder = _cog.actor_holder(actor_id)
    roles = []
    events = _dom(world, "event_instances")
    for iid in sorted(events):
        inst = events[iid]
        if not isinstance(inst, dict) or inst.get("lifecycle") != "ACTIVE":
            continue
        for slot in sorted(inst.get("bound_slots") or {}):
            if inst["bound_slots"][slot] == actor_id:
                roles.append({"instance_id": iid, "template_id": inst.get("template_id"),
                              "slot": slot, "phase": inst.get("current_phase")})
    cognition = []
    for iid in sorted(set(relevant_info_ids)):
        rec = _cog.record_of(world, holder, iid)
        cognition.append({"info_id": iid, "awareness": rec["awareness"] if rec else "NONE",
                          "belief": rec["belief"] if rec else None})
    assoc = sorted(pid for pid, p in (definition.get("place_definitions") or {}).items()
                   if any(a.get("actor_id") == actor_id for a in p.get("npc_associations") or []))
    media = legacy.irregular_npcs.get(legacy_name) if legacy_name else None
    return {
        "actor_id": actor_id,
        "display_name": name,
        "legacy_name": legacy_name,                          # v7.12: 판정 능력치(session.npcs) 조회 키 — 표시 금지
        "kind": (reg or {}).get("kind") or (adef or {}).get("kind"),
        "authored_profile": copy.deepcopy((adef or {}).get("profile_base") or {}),
        "placement": copy.deepcopy(placement),             # 정본 배치(override)만 — None = 정본 추적 없음
        "location_tracked": placement is not None,
        # H07: 유효 위치(정본 override > 평상시 일과 파생 > untracked) — 사건 조건과 같은 경계(core.npc_position)
        "effective_placement": copy.deepcopy(pos["placement"]),
        "position_source": pos["source"],
        "place_associations": assoc,                        # 연고·선호 — 현재 위치 아님
        "relevant_resources": copy.deepcopy(legacy.resources.get(legacy_name)) if legacy_name else None,
        "relevant_statuses": copy.deepcopy(legacy.statuses.get(legacy_name)) if legacy_name else None,
        "companionship": {"is_companion": legacy_name in legacy.companions if legacy_name else False,
                          "met": legacy_name in legacy.met_npcs if legacy_name else False},
        "event_roles": roles,
        "relevant_cognition": cognition,
        "relationship_deltas": copy.deepcopy(_rel.deltas_involving(world, holder)),
        "media_identity": copy.deepcopy(media),
    }
