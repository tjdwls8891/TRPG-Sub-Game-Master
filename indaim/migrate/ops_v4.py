"""옛 typed 연산(world_mutation) → 커널 효과 번역 · 이식 전 컴포넌트 자리표시.

운영 커널 커밋(indaim/turn/kernel_commit.py)이 플레이어 턴 세계 연산을 이 번역으로 커널 효과로 바꾼다
(계획 엔진이 커널 효과를 직접 낼 때까지). T-6에서 그림자 비교(K-c)는 은퇴했다.
"""
from indaim.kernel import WORLD, Component, Registry, make_effect
from indaim.rules import register_all
from indaim.rules.relations.components import target_of

from . import v4

_PLACEHOLDERS = ( ("info", ("runtime", "status")),
                 ("event", ("core", "vars", "history")),
                 (WORLD, ("pending_signals", "info_series", "group_mind", "group_bond", "unresolved")))


def registry() -> Registry:
    r = register_all(Registry("migrate.v4"))
    for kind, names in _PLACEHOLDERS:
        for n in names:
            if not r.has(kind, n):
                r.register(Component(n, kind, "migrate.v4"))
    return r.freeze()


def _actor_op(comp, op, keep=None):
    def f(p, _op=None):
        args = {k: v for k, v in p.items() if k != "actor_id" and (keep is None or k in keep)}
        return [("actor", p.get("actor_id"), comp, op, args)]
    return f


def _world_op(comp, op):
    return lambda p, _op=None: [(WORLD, "", comp, op, dict(p))]


def _holder(comp_actor, comp_group, op):
    def f(p, _op=None):
        h = p.get("holder") or ""
        args = {k: v for k, v in p.items() if k != "holder"}
        if h.startswith("actor:"):
            return [("actor", h[len("actor:"):], comp_actor, op, args)]
        return [(WORLD, "", comp_group, op, dict(p))]
    return f


def _info_create(p, _op=None):
    iid = (p.get("definition") or {}).get("info_id") if isinstance(p.get("definition"), dict) else None
    return [("info", iid, "runtime", "create", dict(p)), ("info", iid, "status", "init", dict(p))]


def _info_link(p, _op=None):
    return [("info", p.get("predecessor"), "status", "add_successor", dict(p)),
            ("info", p.get("successor"), "status", "add_predecessor", dict(p))]


def _event_create(p, _op=None):
    inst = dict(p.get("instance") or {}) if isinstance(p.get("instance"), dict) else {}
    if isinstance(inst.get("local_state"), dict):
        inst["local_state"] = {k: v for k, v in inst["local_state"].items() if not k.startswith(v4.LEDGER_PREFIXES)}
        # (생성 시 장부 키는 없다 — 있으면 버려진다. 표식은 EVENT_LOCAL_SET으로만 생긴다)
    iid = inst.get("instance_id")
    return [("event", iid, c, op, {"instance": inst}) for c, op in (("core", "create"), ("vars", "init"), ("history", "init"))]


def _event_op(comp, op):
    return lambda p, _op=None: [("event", p.get("instance_id"), comp, op,
                                  {k: v for k, v in p.items() if k != "instance_id"})]


def _event_local(p, op):
    key = p.get("key")
    if isinstance(key, str):
        for pre, side in v4.MARK_PREFIXES.items():          # 사건이 붙이고 푼 인물 상태 → 그 인물의 표식
            if key.startswith(pre):
                return [("actor", key[len(pre):], "vitals", "mark",
                         {"instance_id": p.get("instance_id"), "side": side, "value": p.get("value")})]
        if key.startswith(v4.TRANSIENT_PREFIXES):
            return []                     # 커밋 확정 훅(party·growth·items)이 같은 턴에 반영하는 사본 — 정본에 남기지 않는다
    name = "resolve" if op.source_kind == "PLAYER_RESOLUTION" else "set"
    return [("event", p.get("instance_id"), "vars", name, {k: v for k, v in p.items() if k != "instance_id"})]

TRANSLATE = {
    "CLOCK_INITIALIZE": _world_op("clock", "init"),
    "CLOCK_ADVANCE_TO": _world_op("clock", "advance_to"),
    "SCHEDULE_CREATE": _world_op("schedule", "create"),
    "SCHEDULE_CANCEL": _world_op("schedule", "cancel"),
    "SCHEDULE_CONSUME": _world_op("schedule", "consume"),
    "SCENE_PLACE_SET": _world_op("scene", "set"),
    "ACCESS_STATE_SET": _world_op("access", "set"),
    "ACTOR_PLACEMENT_SET": _actor_op("pos", "place"),
    "ACTOR_TRANSIT_SET": _actor_op("pos", "transit"),
    "ACTOR_PLACEMENT_CLEAR": lambda p, _op=None: [("actor", p.get("actor_id"), "pos", "clear", {}),
                                        ("actor", p.get("actor_id"), "hold", "hold_current", {}),
                                        (WORLD, "", "schedule", "drop_movement_wakeups", {"actor_id": p.get("actor_id")})],
    "SPATIAL_PLACE_KNOWLEDGE_UPSERT": _actor_op("map", "place_know"),
    "SPATIAL_EDGE_KNOWLEDGE_UPSERT": _actor_op("map", "edge_know"),
    "SPATIAL_ACCESS_KNOWLEDGE_UPSERT": _actor_op("map", "access_know"),
    "ACTOR_REGISTER": _actor_op("reg", "register"),
    "ACTOR_RENAME": _actor_op("reg", "rename"),
    "INFORMATION_CREATE": _info_create,
    "INFORMATION_VALIDITY_SET": lambda p, _op=None: [("info", p.get("info_id"), "status", "validity",
                                                       {k: v for k, v in p.items() if k != "info_id"})],
    "INFORMATION_LINK": _info_link,
    "INFORMATION_SERIES_SET": _world_op("info_series", "set"),
    "COGNITION_ACQUIRE": _holder("mind", "group_mind", "acquire"),
    "COGNITION_UPSERT": _holder("mind", "group_mind", "upsert"),
    "COGNITION_BELIEF_SET": _holder("mind", "group_mind", "belief"),
    "RELATIONSHIP_DELTA_APPEND": lambda p, _op=None: [(*target_of(p.get("delta")), "append", dict(p))],
    "EVENT_INSTANCE_CREATE": _event_create,
    "EVENT_PHASE_TRANSITION": _event_op("core", "transition"),
    "EVENT_LIFECYCLE_SET": _event_op("core", "lifecycle"),
    "EVENT_LOCAL_SET": _event_local,
    "EVENT_HISTORY_APPEND": _event_op("history", "append"),
}


def translate(ops) -> list:
    """옛 연산 목록 → 효과 목록. 번역 대상이 아닌 연산은 빠지고, 그 인과 부모는 자식에게 이어진다."""
    out, ids_of = [], {}
    for op in ops:
        parents = []
        for par in op.causal_parent_op_ids:
            parents += [p for p in ids_of.get(par, ()) if p not in parents]
        fn = TRANSLATE.get(op.op_type)
        if fn is None:
            ids_of[op.op_id] = parents                        # 빠진 연산 = 부모를 그대로 물려준다
            continue
        rows = fn(op.payload, op)
        if not rows:
            ids_of[op.op_id] = parents
            continue
        ids = []
        for i, (kind, eid, comp, name, args) in enumerate(rows):
            eff_id = op.op_id if len(rows) == 1 else f"{op.op_id}#{i}"
            out.append(make_effect(eff_id, kind, eid, comp, name, args, cause="PLAN", by=f"v4:{op.op_type}",
                                   parents=parents))
            ids.append(eff_id)
        ids_of[op.op_id] = ids
    return out
