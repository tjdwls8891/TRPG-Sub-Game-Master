# 관계 — 희소 RelationshipState delta (H04-A)
#
# [권위 — DESIGN 05 §96~98 · H04 결정 게이트 A12]
#   저작 관계 prose는 정의(프로필)에 남고, 플레이 중 의미 있는 변화만 sparse delta로 append한다.
#   전체 NxN 수치 matrix를 만들지 않는다.
#   relationship_state = {relation_key: {"from_holder", "to_holder", "deltas": [Delta]}}
#     relation_key = "<from_holder>-><to_holder>" (방향 있음)
#     Delta = {"delta_id", "from_holder", "to_holder", "kind", "summary", "at_minute", "source_ref"}
#   같은 delta_id 재적용은 멱등, 같은 id 다른 내용은 거부.
from __future__ import annotations

from . import cognition as _cog
from . import world_mutation as _wm

DELTA_KEYS = ("delta_id", "from_holder", "to_holder", "kind", "summary", "at_minute", "source_ref")
OP_RELATIONSHIP_DELTA_APPEND = "RELATIONSHIP_DELTA_APPEND"
RELATIONSHIP_OP_TYPES = (OP_RELATIONSHIP_DELTA_APPEND,)


def relation_key(from_holder: str, to_holder: str) -> str:
    return f"{from_holder}->{to_holder}"


def delta_record(delta_id, from_holder, to_holder, kind, *, summary=None, at_minute=None, source_ref=None):
    return {"delta_id": delta_id, "from_holder": from_holder, "to_holder": to_holder, "kind": kind,
            "summary": summary, "at_minute": at_minute, "source_ref": source_ref}


def _state(world) -> dict:
    return (world.domain("relationship_state") if isinstance(world, _wm.ProjectedWorld)
            else getattr(world, "relationship_state", {})) or {}


def deltas_involving(world, holder: str) -> list:
    """holder가 한쪽 끝인 delta(관계 키 정렬 → 저장 순서)."""
    out = []
    for key in sorted(_state(world)):
        rel = _state(world)[key]
        if isinstance(rel, dict) and holder in (rel.get("from_holder"), rel.get("to_holder")):
            out.extend(rel.get("deltas") or [])
    return out


def validate_delta(d) -> list:
    if not isinstance(d, dict) or set(d) != set(DELTA_KEYS):
        return [f"delta 키는 정확히 {list(DELTA_KEYS)}"]
    errs = []
    if not isinstance(d["delta_id"], str) or not d["delta_id"]:
        errs.append("delta_id는 비어 있지 않은 문자열")
    for k in ("from_holder", "to_holder"):
        if _cog.parse_holder(d[k]) is None:
            errs.append(f"{k}는 'actor:<id>'·'group:<id>'")
    if d["from_holder"] == d["to_holder"]:
        errs.append("자기 자신과의 관계 delta")
    if not isinstance(d["kind"], str) or not d["kind"]:
        errs.append("kind는 비어 있지 않은 문자열")
    return errs


def h_relationship_delta_append(view, op, payload):
    if set(payload) != {"delta"}:
        raise _wm.WorldMutationError(f"{op.op_id}: payload 키는 delta")
    d = payload["delta"]
    errs = validate_delta(d)
    if errs:
        raise _wm.WorldMutationError(f"{op.op_id}: " + "; ".join(errs))
    key = relation_key(d["from_holder"], d["to_holder"])
    for old in view.get("relationship_state", (key, "deltas")) or []:
        if old.get("delta_id") == d["delta_id"]:
            if old != d:
                raise _wm.WorldMutationError(f"{op.op_id}: delta_id {d['delta_id']} 재사용(다른 내용)")
            return []
    return [_wm.write(op, "relationship_state", (key, "from_holder"), _wm.MERGE_SINGLE_VALUE, d["from_holder"]),
            _wm.write(op, "relationship_state", (key, "to_holder"), _wm.MERGE_SINGLE_VALUE, d["to_holder"]),
            _wm.write(op, "relationship_state", (key, "deltas"), _wm.MERGE_APPEND_DEDUPE, d)]


def register(registry: _wm.OperationRegistry) -> None:
    registry.register(OP_RELATIONSHIP_DELTA_APPEND, h_relationship_delta_append)
