# 행위자 등록부 — ActorId 정본 · 레거시 이름 해석 · 등록/개명 typed 연산 (H04-A)
#
# [권위 — DESIGN 01 §7 · DESIGN 05 §82·§99 · H04 결정 게이트 A4·A5]
#   actor_registry = {ActorId: {"kind": "PC"|"NPC", "display_name": str,
#                               "source_kind": "PLAYER"|"AUTHORED_NPC"|"SESSION_NPC",
#                               "source_key": str|None, "aliases": [str]}}
#   전체 NPC 프로필을 복제하지 않는다 — 저작 프로필은 컴파일된 actor_definitions[*].profile_base,
#   현재 상태는 다른 정본 도메인(공간·인지·관계·사건)에서 CurrentNpcProjection이 조립한다.
#   개명은 ActorId를 유지하고 이전 표시 이름을 aliases에 append+dedupe한다(외래 키 rekey 없음).
#
# [레거시 호환]
#   session.npcs·resources·statuses·companions·met_npcs는 아직 이름 키다. 일괄 변환하지 않고
#   resolve_actor_id()가 이름 → ActorId 후보를 결정적으로 해석한다(모호하면 None + 후보).
#
# [하지 않는 것]
#   · production 플레이어 경로가 등록·개명 연산을 만들지 않는다(H04 producer 0, 배선은 H05·H06).
#   · 세션 NPC id 생성 정책의 production 적용은 H05/H06이다(session_npc_actor_id는 순수 함수).
from __future__ import annotations

import re
from dataclasses import dataclass

from . import world_mutation as _wm
from .world_ids import is_valid_explicit_id, stable_digest

KINDS = ("PC", "NPC")
SOURCE_KINDS = ("PLAYER", "AUTHORED_NPC", "SESSION_NPC")
ENTRY_KEYS = ("kind", "display_name", "source_kind", "source_key", "aliases")
_PC_ID = re.compile(r"^pc_\d+$")

OP_ACTOR_REGISTER = "ACTOR_REGISTER"
OP_ACTOR_RENAME = "ACTOR_RENAME"
ACTOR_OP_TYPES = (OP_ACTOR_REGISTER, OP_ACTOR_RENAME)


def is_pc_actor_id(actor_id) -> bool:
    return isinstance(actor_id, str) and bool(_PC_ID.match(actor_id))


def session_npc_actor_id(narrative_seed: str, source_key: str) -> str:
    """플레이 중 생긴 NPC의 결정적 ActorId(세션 시드 + 최초 표시 키). 개명과 무관하다."""
    return "npc_s" + stable_digest("actor.session_npc.v1", narrative_seed, source_key)


def validate_entry(actor_id, e) -> list:
    if not isinstance(actor_id, str) or not is_valid_explicit_id(actor_id):
        return [f"ActorId {actor_id!r} 형식이 잘못됐습니다"]
    if not isinstance(e, dict) or set(e) != set(ENTRY_KEYS):
        return [f"{actor_id}: 항목 키는 정확히 {list(ENTRY_KEYS)}여야 합니다"]
    errs = []
    if e["kind"] not in KINDS:
        errs.append(f"{actor_id}: kind는 {list(KINDS)} 중 하나")
    if e["source_kind"] not in SOURCE_KINDS:
        errs.append(f"{actor_id}: source_kind는 {list(SOURCE_KINDS)} 중 하나")
    if (e["kind"] == "PC") != (e["source_kind"] == "PLAYER"):
        errs.append(f"{actor_id}: PC ⇔ source_kind PLAYER")
    if e["kind"] == "PC" and not is_pc_actor_id(actor_id):
        errs.append(f"{actor_id}: PC ActorId는 'pc_<uid>' 형식")
    if not isinstance(e["display_name"], str) or not e["display_name"].strip():
        errs.append(f"{actor_id}: display_name은 비어 있지 않은 문자열")
    if e["source_key"] is not None and not isinstance(e["source_key"], str):
        errs.append(f"{actor_id}: source_key는 문자열 또는 null")
    if not isinstance(e["aliases"], list) or not all(isinstance(x, str) and x for x in e["aliases"]):
        errs.append(f"{actor_id}: aliases는 문자열 목록")
    return errs


def validate_registry(reg) -> list:
    if not isinstance(reg, dict):
        return ["actor_registry는 객체여야 합니다"]
    errs = []
    for aid, e in reg.items():
        errs += validate_entry(aid, e)
    return errs


def registry_of(world) -> dict:
    return (world.domain("actor_registry") if isinstance(world, _wm.ProjectedWorld)
            else getattr(world, "actor_registry", {})) or {}


def known_actor_ids(definition: dict, world) -> set:
    return set(definition.get("actor_definitions") or {}) | set(registry_of(world))


def display_name(definition: dict, world, actor_id: str):
    e = registry_of(world).get(actor_id)
    if isinstance(e, dict):
        return e.get("display_name")
    a = (definition.get("actor_definitions") or {}).get(actor_id)
    return a.get("display_name") if a else None


@dataclass(frozen=True)
class ActorResolution:
    actor_id: str | None
    matched_by: str | None = None
    candidates: tuple = ()


def resolve_actor_id(definition: dict, world, name: str) -> ActorResolution:
    """레거시 이름 → ActorId. 순서: 등록부 표시 이름 → 등록부 alias → 저작 사전 키 → 저작 표시 이름.

    같은 단계에 후보가 둘 이상이면 모호(None + 후보 정렬). ActorId 자체도 그대로 인정한다.
    """
    reg = registry_of(world)
    defs = definition.get("actor_definitions") or {}
    if isinstance(name, str) and (name in reg or name in defs):
        return ActorResolution(name, "actor_id", (name,))
    stages = (
        ("registry_display", lambda: [a for a, e in reg.items() if e.get("display_name") == name]),
        ("registry_alias", lambda: [a for a, e in reg.items() if name in (e.get("aliases") or [])]),
        ("authored_key", lambda: [a for a, d in defs.items() if d.get("legacy_key") == name]),
        ("authored_display", lambda: [a for a, d in defs.items() if d.get("display_name") == name]),
    )
    for label, fn in stages:
        hits = sorted(set(fn()))
        if len(hits) == 1:
            return ActorResolution(hits[0], label, tuple(hits))
        if len(hits) > 1:
            return ActorResolution(None, label, tuple(hits))
    return ActorResolution(None, None, ())


def legacy_name_of(definition: dict, world, actor_id: str):
    """ActorId → 레거시 이름 키(session.npcs 등). 저작 NPC는 사전 키, 세션 NPC는 source_key."""
    d = (definition.get("actor_definitions") or {}).get(actor_id)
    if d:
        return d.get("legacy_key")
    e = registry_of(world).get(actor_id)
    if isinstance(e, dict):
        return e.get("source_key") or e.get("display_name")
    return None


# ══════════════════════════════════════════════════════════════
#  typed 연산
# ══════════════════════════════════════════════════════════════

def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def _append_dedupe(old, new):
    out = []
    for x in list(old) + list(new):
        if x not in out:
            out.append(x)
    return out


def h_actor_register(view, op, payload):
    required = {"actor_id", "kind", "display_name", "source_kind", "source_key"}
    if not required <= set(payload) or set(payload) - required - {"aliases"}:
        _reject(op, f"payload 키는 {sorted(required)} + aliases(선택)")
    aid = payload["actor_id"]
    entry = {k: payload.get(k) for k in ENTRY_KEYS}
    entry["aliases"] = _append_dedupe([], payload.get("aliases") or [])
    errs = validate_entry(aid, entry)
    if errs:
        _reject(op, "; ".join(errs))
    if view.has("actor_registry", (aid,)):
        _reject(op, f"이미 등록된 ActorId {aid}")
    return [_wm.write(op, "actor_registry", (aid,), _wm.MERGE_SINGLE_VALUE, entry)]


def h_actor_rename(view, op, payload):
    if set(payload) != {"actor_id", "display_name"}:
        _reject(op, "payload 키는 actor_id·display_name")
    aid, new = payload["actor_id"], payload["display_name"]
    cur = view.get("actor_registry", (aid,))
    if cur is None:
        _reject(op, f"등록되지 않은 ActorId {aid}")
    if not isinstance(new, str) or not new.strip():
        _reject(op, "display_name은 비어 있지 않은 문자열")
    if new == cur["display_name"]:
        return []
    aliases = _append_dedupe(cur.get("aliases") or [], [cur["display_name"]])
    aliases = [a for a in aliases if a != new]
    return [_wm.write(op, "actor_registry", (aid, "display_name"), _wm.MERGE_SINGLE_VALUE, new),
            _wm.write(op, "actor_registry", (aid, "aliases"), _wm.MERGE_SINGLE_VALUE, aliases)]


_HANDLERS = {OP_ACTOR_REGISTER: h_actor_register, OP_ACTOR_RENAME: h_actor_rename}


def register(registry: _wm.OperationRegistry) -> None:
    for t, h in _HANDLERS.items():
        registry.register(t, h)
