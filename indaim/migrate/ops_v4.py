"""옛 typed 연산(core.world_mutation) → 새 효과 번역 · 이식 전 컴포넌트 자리표시 · 그림자 비교.

그림자 비교(K-c, 테스트 전용): 옛 투영이 성공할 때마다 같은 연산을 새 커널로도 투영해,
'옛 결과를 변환한 세계'와 이식된 모듈 컴포넌트가 같은지 본다. 운영 경로는 이 모듈을 부르지 않는다.
"""
from indaim.kernel import WORLD, Component, EffectBatch, KernelError, Registry, make_effect, project
from indaim.kernel.effects import compact
from indaim.rules import FEATURES, register_all

from . import v4

PORTED = tuple((c.entity, c.name) for f in FEATURES for c in f.COMPONENTS)
_PLACEHOLDERS = (("actor", ("reg", "mind", "vitals", "bag", "party", "bond", "stats")),
                 ("item", ("state",)), ("place", ("floor",)), ("info", ("runtime", "status")),
                 ("event", ("core", "vars", "history")),
                 (WORLD, ("pending_signals", "item_lore", "info_series", "group_mind", "group_bond", "unresolved")))


def registry() -> Registry:
    r = register_all(Registry("migrate.v4"))
    for kind, names in _PLACEHOLDERS:
        for n in names:
            if not r.has(kind, n):
                r.register(Component(n, kind, "migrate.v4"))
    return r.freeze()


def _actor_op(comp, op, keep=None):
    def f(p):
        args = {k: v for k, v in p.items() if k != "actor_id" and (keep is None or k in keep)}
        return [("actor", p.get("actor_id"), comp, op, args)]
    return f


def _world_op(comp, op):
    return lambda p: [(WORLD, "", comp, op, dict(p))]


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
    "ACTOR_PLACEMENT_CLEAR": lambda p: [("actor", p.get("actor_id"), "pos", "clear", {}),
                                        ("actor", p.get("actor_id"), "hold", "hold_current", {}),
                                        (WORLD, "", "schedule", "drop_movement_wakeups", {"actor_id": p.get("actor_id")})],
    "SPATIAL_PLACE_KNOWLEDGE_UPSERT": _actor_op("map", "place_know"),
    "SPATIAL_EDGE_KNOWLEDGE_UPSERT": _actor_op("map", "edge_know"),
    "SPATIAL_ACCESS_KNOWLEDGE_UPSERT": _actor_op("map", "access_know"),
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
        rows = fn(op.payload)
        ids = []
        for i, (kind, eid, comp, name, args) in enumerate(rows):
            eff_id = op.op_id if len(rows) == 1 else f"{op.op_id}#{i}"
            out.append(make_effect(eff_id, kind, eid, comp, name, args, cause="PLAN", by=f"v4:{op.op_type}",
                                   parents=parents))
            ids.append(eff_id)
        ids_of[op.op_id] = ids
    return out


def _fields(domains: dict) -> dict:
    d = {f: [] if f in v4.NAME_LISTS or f == "world_pending_signals" else {} for f in v4.FIELDS}
    d["world_version"] = None
    d.update({k: v for k, v in domains.items() if k in d})
    return d


def _ported_view(world, reg) -> dict:
    w = compact(world, reg, None).to_dict()
    out = {}
    for kind, comp in PORTED:
        if kind == WORLD:
            if comp in w.get("world", {}):
                out[(kind, "", comp)] = w["world"][comp]
        else:
            for eid, comps in (w.get("entities", {}).get(kind) or {}).items():
                if comp in comps:
                    out[(kind, eid, comp)] = comps[comp]
    return out


STATS = {"compared": 0, "effects": 0}


def shadow_compare(source_domains: dict, old_batch, result_domains: dict) -> list:
    """같은 연산의 새 커널 투영 ↔ 옛 결과 변환 — 이식 컴포넌트 차이 목록(비면 일치)."""
    STATS["compared"] += 1
    reg = registry()
    base = v4.convert(_fields(source_domains), game_keys=set()).world
    want = _ported_view(v4.convert(_fields(result_domains), game_keys=set()).world, reg)
    try:
        b = EffectBatch(batch_id=old_batch.batch_id, origin=old_batch.outer_origin,
                        base_version_id=old_batch.source_world_version_id,
                        definition_revision=old_batch.definition_revision, transaction_id=old_batch.transaction_id)
        effs = translate(old_batch.operations)
        STATS["effects"] += len(effs)
        b.extend(effs)
        b.freeze()
        res = project(base, b, reg, None)
    except KernelError as e:
        return [f"번역·투영 오류: {e}"]
    if not res.ok:
        return [f"새 커널 충돌: {[c.message for c in res.conflicts]}"]
    got = _ported_view(res.world, reg)
    return [f"{k}: 옛 {want.get(k)!r} ≠ 새 {got.get(k)!r}" for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]
