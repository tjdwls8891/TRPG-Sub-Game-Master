"""세계 버전 — 커밋 하나 = 버전 하나. 옛 `world_state.next_world_version`과 같은 id 규칙(연속성)."""
from .canon import stable_digest

CAUSE_PLAYER_TURN, CAUSE_INITIALIZATION, CAUSE_ADMIN_EDIT = "PLAYER_TURN", "INITIALIZATION", "ADMIN_EDIT"
CAUSE_KINDS = (CAUSE_PLAYER_TURN, CAUSE_INITIALIZATION, CAUSE_ADMIN_EDIT)
_NAMESPACE = "world_version.v1"


def is_version(v) -> bool:
    return (isinstance(v, dict) and isinstance(v.get("version_id"), str) and bool(v["version_id"])
            and v.get("cause_kind") in CAUSE_KINDS
            and isinstance(v.get("ordinal"), int) and not isinstance(v.get("ordinal"), bool))


def next_version(session_id, parent, *, cause_kind: str, cause_id: str) -> dict:
    if cause_kind not in CAUSE_KINDS:
        raise ValueError(f"알 수 없는 cause_kind: {cause_kind!r}")
    if not isinstance(cause_id, str) or not cause_id:
        raise ValueError("cause_id가 필요합니다")
    if parent is not None and not is_version(parent):
        raise ValueError(f"부모 버전 형식이 잘못됐습니다: {parent!r}")
    parent_id = parent["version_id"] if parent else None
    return {"version_id": "wv_" + stable_digest(_NAMESPACE, str(session_id), parent_id, cause_kind, cause_id),
            "parent_version_id": parent_id, "ordinal": parent["ordinal"] + 1 if parent else 1,
            "cause_kind": cause_kind, "cause_id": cause_id}
