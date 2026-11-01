"""옛 typed 연산(core.world_mutation) → 새 효과 번역 · 이식 전 컴포넌트 자리표시 · 그림자 비교.

그림자 비교(K-c, 테스트 전용): 옛 투영이 성공할 때마다 같은 연산을 새 커널로도 투영해,
'옛 결과를 변환한 세계'와 이식된 모듈 컴포넌트가 같은지 본다. 운영 경로는 이 모듈을 부르지 않는다.
"""
from indaim.kernel import WORLD, Component, EffectBatch, KernelError, Registry, make_effect, project
from indaim.kernel.effects import compact
from indaim.rules import FEATURES, register_all
from indaim.rules.relations.components import target_of

from . import v4

# 옛 typed 연산의 번역으로 비교하는 컴포넌트(투영 그림자). vitals 등 확정 훅 모듈은 커밋 단위 그림자로 따로 비교한다.
_HOOK_FEATURES = ("vitals", "items", "growth", "party")
PORTED = tuple((c.entity, c.name) for f in FEATURES for c in f.COMPONENTS
               if f.__name__.rsplit(".", 1)[-1] not in _HOOK_FEATURES)
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


def _fields(domains: dict) -> dict:
    d = {f: [] if f in v4.NAME_LISTS or f == "world_pending_signals" else {} for f in v4.FIELDS}
    d["world_version"] = None
    d.update({k: v for k, v in domains.items() if k in d})
    return d


def _ported_view(world, reg) -> dict:
    w = compact(world, reg, None).to_dict()
    out = {}
    for eid, comps in (w.get("entities", {}).get("actor") or {}).items():     # 번역되는 vitals 부분 = 사건 표식
        if (comps.get("vitals") or {}).get("event_marks"):
            out[("actor", eid, "vitals.event_marks")] = comps["vitals"]["event_marks"]
    for kind, comp in PORTED:
        if kind == WORLD:
            if comp in w.get("world", {}):
                out[(kind, "", comp)] = w["world"][comp]
        else:
            for eid, comps in (w.get("entities", {}).get(kind) or {}).items():
                if comp in comps:
                    out[(kind, eid, comp)] = comps[comp]
    return out


STATS = {"compared": 0, "effects": 0, "vitals_turns": 0, "vitals_effects": 0, "items_turns": 0, "items_effects": 0, "growth_calls": 0, "growth_effects": 0, "party_calls": 0, "party_effects": 0, "plans": 0, "plan_ops": 0}


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


def shadow_vitals(pre_fields: dict, inputs: dict, post_fields: dict, *, tx: str) -> list:
    """T-1 그림자 — vitals 확정 훅(변환된 턴 전 세계 + 같은 판정 재료) ↔ 변환된 커밋 후 세계의 actor.vitals."""
    from indaim.kernel import View
    from indaim.rules import vitals
    reg = registry()
    pre = v4.convert(_fields(pre_fields), game_keys=set())
    names, _canon, _amb = v4._names(_fields(pre_fields))
    effs = vitals.settle(View(pre.world, reg, None), {**inputs, "names": names}, effect_prefix=tx)
    STATS["vitals_turns"] += 1
    STATS["vitals_effects"] += len(effs)
    b = EffectBatch(batch_id=f"shadow.vitals.{tx}", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    b.extend(effs)
    b.freeze()
    res = project(pre.world, b, reg, None)
    if not res.ok:
        return [f"새 커널 충돌: {[c.message for c in res.conflicts]}"]

    def vit(world):
        w = compact(world, reg, None).to_dict()
        return {aid: {k: v for k, v in c["vitals"].items() if k != "event_marks"}
                for aid, c in (w.get("entities", {}).get("actor") or {}).items() if "vitals" in c}
    want = {a: v for a, v in vit(v4.convert(_fields(post_fields), game_keys=set()).world).items() if v}
    got = {a: v for a, v in vit(res.world).items() if v}
    return [f"{a}: 옛 {want.get(a)!r} ≠ 새 {got.get(a)!r}" for a in sorted(set(want) | set(got)) if want.get(a) != got.get(a)]


def _with_names(pre: dict, post: dict) -> tuple:
    """(그림자 전용) 등록부로 풀리지 않는 소유자 이름에 임시 행위자 항목을 더한다 — 등록부 없는 시험 세션도 비교되게.
    같은 이름은 턴 전후 같은 임시 id다."""
    import hashlib
    pre, post = dict(pre), dict(post)
    to_aid, _c, _a = v4._names(_fields(pre))
    owners = set()
    for d in (pre, post):
        for f in ("resources", "equipment", "statuses", "status_clock"):
            owners |= set((d.get(f) or {}))
        owners |= {r.get("owner") for r in (d.get("item_instances") or {}).values() if isinstance(r, dict) and r.get("owner")}
        for f in v4.NAME_LISTS:
            owners |= set(d.get(f) or ())
    extra = {"x." + hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]:
             {"kind": "NPC", "display_name": n, "source_kind": "SESSION_NPC", "source_key": n, "aliases": []}
             for n in sorted(owners) if isinstance(n, str) and n and n not in to_aid}
    for d in (pre, post):
        d["actor_registry"] = {**(d.get("actor_registry") or {}), **extra}
    return pre, post


def shadow_items(pre_fields: dict, inputs: dict, post_fields: dict, *, tx: str) -> list:
    """T-2 그림자 — items 확정 훅 ↔ 변환된 커밋 후 세계의 물건 컴포넌트(bag·item.state·place.floor·item_lore)."""
    from indaim.kernel import View
    from indaim.rules import items
    reg = registry()
    pre_fields, post_fields = _with_names(pre_fields, post_fields)
    pre = v4.convert(_fields(pre_fields), game_keys=set())
    names, _canon, _amb = v4._names(_fields(pre_fields))
    effs = items.settle(View(pre.world, reg, None), {**inputs, "names": names}, effect_prefix=tx)
    STATS["items_turns"] += 1
    STATS["items_effects"] += len(effs)
    b = EffectBatch(batch_id=f"shadow.items.{tx}", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    b.extend(effs)
    b.freeze()
    res = project(pre.world, b, reg, None)
    if not res.ok:
        return [f"새 커널 충돌: {[c.message for c in res.conflicts]}"]
    comps = (("actor", "bag"), ("item", "state"), ("place", "floor"), (WORLD, "item_lore"))

    def view_of(world):
        w = compact(world, reg, None).to_dict()
        out = {}
        for kind, comp in comps:
            if kind == WORLD:
                if comp in w.get("world", {}):
                    out[(kind, "", comp)] = w["world"][comp]
                continue
            for eid, cs in (w.get("entities", {}).get(kind) or {}).items():
                if comp in cs:
                    out[(kind, eid, comp)] = cs[comp]
        return out
    want = view_of(v4.convert(_fields(post_fields), game_keys=set()).world)
    got = view_of(res.world)
    return [f"{k}: 옛 {want.get(k)!r} ≠ 새 {got.get(k)!r}" for k in sorted(set(want) | set(got)) if want.get(k) != got.get(k)]


def shadow_growth(pre_fields: dict, inputs: dict, post_fields: dict, *, tx: str) -> list:
    """T-3 그림자 — growth 확정 훅 ↔ 변환된 결과 세계의 actor.stats(PC 게임 수치·실패 누적)."""
    from indaim.kernel import View
    from indaim.rules import growth
    reg = registry()
    gk = growth.game_keys(inputs["sd"])
    pre = v4.convert(_fields(pre_fields), game_keys=gk)
    names, _canon, _amb = v4._names(_fields(pre_fields))
    pc_uid = next(iter(pre_fields.get("players") or {}), None)
    pc_actor = next((a for a, r in (pre_fields.get("actor_registry") or {}).items()
                     if r.get("kind") == "PC" and str(r.get("source_key")) == str(pc_uid)), None)
    if pc_actor is None:
        return []                                   # PC 등록이 없는 시험 세션 — 비교 대상 없음
    effs = growth.settle(View(pre.world, reg, None), {**inputs, "names": names, "pc_uid": pc_uid, "pc_actor": pc_actor},
                         effect_prefix=tx)
    STATS["growth_calls"] += 1
    STATS["growth_effects"] += len(effs)
    b = EffectBatch(batch_id=f"shadow.growth.{tx}", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    b.extend(effs)
    b.freeze()
    res = project(pre.world, b, reg, None)
    if not res.ok:
        return [f"새 커널 충돌: {[c.message for c in res.conflicts]}"]

    def stats(world):
        w = compact(world, reg, None).to_dict()
        return {a: c["stats"] for a, c in (w.get("entities", {}).get("actor") or {}).items() if "stats" in c}
    want = stats(v4.convert(_fields(post_fields), game_keys=gk).world)
    got = stats(res.world)
    return [f"{a}: 옛 {want.get(a)!r} ≠ 새 {got.get(a)!r}" for a in sorted(set(want) | set(got)) if want.get(a) != got.get(a)]


def shadow_party(pre_fields: dict, inputs: dict, post_fields: dict, *, tx: str) -> list:
    """T-4 그림자 — party 확정 훅 ↔ 변환된 커밋 후 세계의 동행 순번·만남 순번."""
    from indaim.kernel import View
    from indaim.rules import party
    reg = registry()
    pre_fields, post_fields = _with_names(pre_fields, post_fields)
    pre = v4.convert(_fields(pre_fields), game_keys=set())
    names, _canon, _amb = v4._names(_fields(pre_fields))
    effs = party.settle(View(pre.world, reg, None), {**inputs, "names": names}, effect_prefix=tx)
    STATS["party_calls"] += 1
    STATS["party_effects"] += len(effs)
    b = EffectBatch(batch_id=f"shadow.party.{tx}", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    b.extend(effs)
    b.freeze()
    res = project(pre.world, b, reg, None)
    if not res.ok:
        return [f"새 커널 충돌: {[c.message for c in res.conflicts]}"]

    def rows(world):
        w = compact(world, reg, None).to_dict()
        out = {}
        for a, c in (w.get("entities", {}).get("actor") or {}).items():
            r = {"companion": (c.get("party") or {}).get("companion"), "met": (c.get("bond") or {}).get("met")}
            if r["companion"] is not None or r["met"] is not None:
                out[a] = r
        return out
    want, got = rows(v4.convert(_fields(post_fields), game_keys=set()).world), rows(res.world)
    return [f"{a}: 옛 {want.get(a)!r} ≠ 새 {got.get(a)!r}" for a in sorted(set(want) | set(got)) if want.get(a) != got.get(a)]


def compare_plans(old, new) -> list:
    """T-5 그림자 — 같은 의도로 옛 세계/커널 세계 보기에서 세운 플레이어 턴 계획이 같은가(연산·결과·신호·선후)."""
    STATS["plans"] += 1
    STATS["plan_ops"] += len(old.ops or ())
    out = []
    for f in ("status", "reason", "start_minute", "end_minute", "code_owned_minutes", "scene_place", "outcome",
              "signals", "ties", "route_options", "resolved_choice", "transit", "item_payments", "observer"):
        a, b = getattr(old, f, None), getattr(new, f, None)
        if a != b:
            out.append(f"{f}: 옛 {a!r} ≠ 새 {b!r}"[:400])
    ao = [o.to_dict() for o in old.ops or ()]
    bo = [o.to_dict() for o in new.ops or ()]
    if ao != bo:
        i = next((k for k, (x, y) in enumerate(zip(ao, bo)) if x != y), min(len(ao), len(bo)))
        out.append(f"ops 다름(옛 {len(ao)} · 새 {len(bo)}) 첫 차이 #{i}: "
                   f"{(ao[i] if i < len(ao) else None)!r} ≠ {(bo[i] if i < len(bo) else None)!r}"[:600])
    return out
