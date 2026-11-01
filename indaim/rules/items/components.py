"""물건 — 소지(actor.bag)·고유 물건(item.state)·현장 물건(place.floor)·물건 특성(world.item_lore).

  actor.bag        {"items": {정규 키: 수량}, "equipped": {자리: {"key"…}}}
  item.state       {kind, owner(ActorId)|None, place|None, condition…}
  place.floor      {원천 id: {key, quantity, origin, by, tx, closed?, inside?}}
  world.item_lore  {물건 키: 특성} — 처음 다룬 물건의 착용·사용 특성(세션에서 유지, 되감기 대상)
확정(settle): 사건 지급 → 이번 턴 물건 연산(logic.run_ops) → 바뀐 칸마다 효과. 옛 키는 이름이라 names로 잇는다(T-6에서 사라짐).
"""
import types

from indaim.kernel import WORLD, Component, make_effect
from indaim.kernel.components import DELETE, SINGLE_VALUE, write

from .._common import keys, reject
from . import logic


def _set(path, kind=dict):
    def op(view, e):
        v = keys(e, ("value",))["value"]
        if not isinstance(v, kind):
            reject(e, f"{'.'.join(path) or '값'}은 {kind.__name__}")
        return [write(e, path, SINGLE_VALUE, v)]
    return op


def _drop(path):
    def op(view, e):
        keys(e, ())
        if path and not view.has(*e.address, path):
            return []
        if not path and not view.stored_has(*e.address):
            return []
        return [write(e, path, DELETE, None)]
    return op


COMPONENTS = (
    Component("bag", "actor", "rules.items", {"set_items": _set(("items",)), "set_equipped": _set(("equipped",)),
                                              "clear_equipped": _drop(("equipped",))}),
    Component("state", "item", "rules.items", {"set": _set(()), "remove": _drop(())}),
    Component("floor", "place", "rules.items", {"set": _set(())}),
    Component("item_lore", WORLD, "rules.items", {"set": _set(())}),
)


def state_of(view, names: dict, place_ids) -> types.SimpleNamespace:
    """세계 → 옛 물건 상태 객체(이름 키). place_ids = 현장 물건을 읽을 장소(정의 장소 + 저장된 장소)."""
    aid_name = {a: n for n, a in names.items()}
    res, equip = {}, {}
    for name, aid in names.items():
        bag = view.read("actor", aid, "bag") or {}
        if "items" in bag:
            res[name] = bag["items"]
        if "equipped" in bag:
            equip[name] = bag["equipped"]
    un = view.read(WORLD, "", "unresolved") or {}
    res.update(un.get("resources") or {})
    equip.update(un.get("equipment") or {})
    inst = {}
    for iid in view.ids("item", "state"):
        r = dict(view.read("item", iid, "state"))
        if r.get("owner"):
            r["owner"] = aid_name.get(r["owner"], r["owner"])
        inst[iid] = r
    inst.update(un.get("item_instances") or {})
    places = {}
    for pid in place_ids:
        for sid, r in (view.read("place", pid, "floor") or {}).items():
            places[sid] = {"place": pid, **r}
    places.update(un.get("place_items") or {})
    return types.SimpleNamespace(resources=res, item_instances=inst, place_items=places, equipment=equip,
                                 item_traits=view.read(WORLD, "", "item_lore") or {})


def settle(view, inp: dict, *, effect_prefix: str) -> list:
    """inp: names {이름: ActorId} · grants(사건 지급 행)·definition · plan(ItemPlan의 ops·defs·pc_name·scene_place·tx·recipes)."""
    names = inp["names"]
    place_ids = set(view.ids("place", "floor"))
    s0 = state_of(view, names, place_ids)
    s = types.SimpleNamespace(**{k: logic.copy.deepcopy(v) for k, v in vars(s0).items()})
    if inp.get("grants"):
        logic.apply_event_grants(s, inp["grants"], definition=inp.get("definition"), pc_name=inp.get("pc_name"))
    p = inp.get("plan")
    if p is not None:
        sim = logic.run_ops(s, p["ops"], defs=p["defs"], pc_name=p["pc_name"], scene_place=p["scene_place"],
                            transaction_id=p["tx"], recipes=p["recipes"])
        res = dict(s.resources)
        for owner in sim.touched:
            res[owner] = sim.res.get(owner) or {}
        s.resources, s.item_instances, s.place_items, s.equipment, s.item_traits = \
            res, sim.inst, sim.places, sim.equip, sim.traits
    out, n = [], [0]

    def eff(kind, eid, comp, op, args=None):
        n[0] += 1
        out.append(make_effect(f"{effect_prefix}.items.{n[0]}", kind, eid, comp, op, args or {},
                               cause="SETTLE", by="rules.items"))
    for name in sorted(set(s.resources) | set(s0.resources) | set(s.equipment) | set(s0.equipment)):
        aid = names.get(name)
        if aid is None:
            continue
        if name in s.resources and s.resources[name] != s0.resources.get(name, object()):
            eff("actor", aid, "bag", "set_items", {"value": s.resources[name]})
        if name in s.equipment and s.equipment[name] != s0.equipment.get(name, object()):
            eff("actor", aid, "bag", "set_equipped", {"value": s.equipment[name]})
        elif name not in s.equipment and name in s0.equipment:
            eff("actor", aid, "bag", "clear_equipped")
    for iid in sorted(set(s.item_instances) | set(s0.item_instances)):
        if iid not in s.item_instances:
            eff("item", iid, "state", "remove")
        elif s.item_instances[iid] != s0.item_instances.get(iid):
            r = dict(s.item_instances[iid])
            if r.get("owner"):
                r["owner"] = names.get(r["owner"], r["owner"])
            eff("item", iid, "state", "set", {"value": r})
    by_place = {}
    for sid, r in s.place_items.items():
        by_place.setdefault(r.get("place"), {})[sid] = {k: v for k, v in r.items() if k != "place"}
    before = {}
    for sid, r in s0.place_items.items():
        before.setdefault(r.get("place"), {})[sid] = {k: v for k, v in r.items() if k != "place"}
    for pid in sorted(set(by_place) | set(before), key=str):
        if pid and by_place.get(pid, {}) != before.get(pid, {}):
            eff("place", pid, "floor", "set", {"value": by_place.get(pid, {})})
    if s.item_traits != s0.item_traits:
        eff(WORLD, "", "item_lore", "set", {"value": s.item_traits})
    return out
