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


# ══════════════════════════════════════════════════════════════
#  v7.39 인물 → PC 관계(사용자 결정 N2: 느리게, 결정적 사건이면 한 턴 전환 · 큰 변화가 이어지지 않게)
# ══════════════════════════════════════════════════════════════
#   기록: 인물 → PC 방향 delta. kind MET(첫 상호작용) · TRUST(summary = 정수 변화량) · MEMORY(summary = 한 줄).
#   단계(저장하지 않는 파생): MET 없음 = stranger, 신뢰 합 ≥ +5 = trusted, ≤ −5 = hostile, 그 밖 known.
#   변화량: 추출 점수(0~100, 50 = 변화 없음) → 코드 임계. 96↑ / 4↓는 결정적 사건(±5 — 한 턴에 단계가 바뀔 수 있다).
#   감쇠: 같은 방향 작은 변화는 하루(1440분)에 합 3까지 · 결정적 사건은 같은 방향 하루 한 번(두 번째부터 ±2) ·
#         직전 기록이 같은 방향 ±2 이상이면 이번 ±2는 ±1(큰 변화가 이어지지 않는다).
KIND_MET, KIND_TRUST, KIND_MEMORY = "MET", "TRUST", "MEMORY"
STAGES = ("stranger", "known", "trusted", "hostile")
STAGE_LABELS = {"stranger": "낯선 사람", "known": "아는 사이", "trusted": "믿는 사이", "hostile": "적대"}
STAGE_EDGE = 5
DECISIVE = 5
DAY_MINUTES = 1440
DAILY_SMALL_CAP = 3
MEMORY_MAX = 60


def _rel_deltas(world, npc_actor, pc_actor) -> list:
    rel = _state(world).get(relation_key(_cog.actor_holder(npc_actor), _cog.actor_holder(pc_actor))) or {}
    return list(rel.get("deltas") or [])


def _amount(d) -> int:
    try:
        return int(d.get("summary"))
    except (TypeError, ValueError):
        return 0


def trust_state(world, npc_actor, pc_actor, initial_trust: int = 0) -> dict:
    """{"met", "trust", "stage", "memories": [최근 순서대로 summary]} — 파생(순수)."""
    ds = _rel_deltas(world, npc_actor, pc_actor)
    met = any(d.get("kind") == KIND_MET for d in ds)
    trust = int(initial_trust or 0) + sum(_amount(d) for d in ds if d.get("kind") == KIND_TRUST)
    stage = "stranger" if not met else ("trusted" if trust >= STAGE_EDGE else
                                        "hostile" if trust <= -STAGE_EDGE else "known")
    mem = [d.get("summary") for d in ds if d.get("kind") == KIND_MEMORY and d.get("summary")]
    return {"met": met, "trust": trust, "stage": stage, "memories": mem}


def base_change(score) -> int:
    if not isinstance(score, int) or isinstance(score, bool):
        return 0
    if score >= 96:
        return DECISIVE
    if score >= 86:
        return 2
    if score >= 71:
        return 1
    if score <= 4:
        return -DECISIVE
    if score <= 14:
        return -2
    if score <= 29:
        return -1
    return 0


def trust_change(world, npc_actor, pc_actor, score, now) -> int:
    """이번 턴 신뢰 변화량(감쇠 적용 — 순수)."""
    c = base_change(score)
    if c == 0:
        return 0
    sign = 1 if c > 0 else -1
    recent = [d for d in _rel_deltas(world, npc_actor, pc_actor) if d.get("kind") == KIND_TRUST
              and isinstance(d.get("at_minute"), int) and isinstance(now, int) and now - d["at_minute"] < DAY_MINUTES]
    same = [_amount(d) for d in recent if _amount(d) * sign > 0]
    if abs(c) == DECISIVE:
        if any(abs(a) == DECISIVE for a in same):
            c = 2 * sign                                   # 같은 방향 결정적 사건은 하루 한 번
        else:
            return c
    if recent and abs(_amount(recent[-1])) >= 2 and _amount(recent[-1]) * sign > 0 and abs(c) >= 2:
        c = sign                                           # 큰 변화가 이어지지 않는다
    small = sum(abs(a) for a in same if abs(a) != DECISIVE)
    return sign * max(0, min(abs(c), DAILY_SMALL_CAP - small))
