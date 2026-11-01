"""동행 — actor.party {"companion": 순번}(지금 함께 움직이는 인물, 순번 = 동행 명단 순서).

만난 인물은 관계 컴포넌트(actor.bond.met = 순번)에 둔다(relations 소유 — 이 모듈은 그 연산을 낸다).
확정(settle): 이번 턴 합류·이탈·만남(관찰이 검증한 목록) → 새 명단 → 순번이 바뀐 인물마다 효과.
합류 판정(같은 장소·join_when·인원 한도)과 사건 합류·이탈 판정은 관찰 단계 몫이다(계획 엔진과 함께 이식).
"""
from indaim.kernel import Component, make_effect
from indaim.kernel.components import DELETE, SINGLE_VALUE, write

from .._common import is_int, keys, reject


def _set_order(field):
    def op(view, e):
        n = keys(e, ("order",))["order"]
        if not is_int(n) or n < 0:
            reject(e, "order는 0 이상의 정수")
        return [write(e, (field,), SINGLE_VALUE, n)]
    return op


def _clear(field):
    def op(view, e):
        keys(e, ())
        return [write(e, (field,), DELETE, None)] if view.has(*e.address, (field,)) else []
    return op


COMPONENTS = (Component("party", "actor", "rules.party", {"set_companion": _set_order("companion"),
                                                          "clear_companion": _clear("companion")}),)


def apply_lists(companions: list, met: list, joined, left, npcs_met) -> tuple:
    """(새 동행, 새 만난 인물) — 옛 extraction.apply_normalized_companions와 같은 의미."""
    current, met = list(companions or ()), list(met or ())
    for name in npcs_met or ():
        if name not in met:
            met.append(name)
    for name in joined or ():
        if name not in current:
            current.append(name)
            if name not in met:
                met.append(name)
    for name in left or ():
        if name in current:
            current.remove(name)
    return current, met


def _ordered(view, names, comp, field):
    rows = []
    for name, aid in names.items():
        v = (view.read("actor", aid, comp) or {}).get(field)
        if v is not None:
            rows.append((v, name))
    return [n for _i, n in sorted(rows)]


def settle(view, inp: dict, *, effect_prefix: str) -> list:
    """inp: names {이름: ActorId} · joined · left · npcs_met."""
    names = inp["names"]
    comp0, met0 = _ordered(view, names, "party", "companion"), _ordered(view, names, "bond", "met")
    comp, met = apply_lists(comp0, met0, inp.get("joined"), inp.get("left"), inp.get("npcs_met"))
    out = []

    def eff(aid, c, op, args):
        out.append(make_effect(f"{effect_prefix}.party.{c}.{aid}", "actor", aid, c, op, args,
                               cause="SETTLE", by="rules.party"))
    for name in sorted(set(comp) | set(comp0)):
        aid = names.get(name)
        if aid is None:
            continue
        if name not in comp:
            eff(aid, "party", "clear_companion", {})
        elif name not in comp0 or comp0.index(name) != comp.index(name):
            eff(aid, "party", "set_companion", {"order": comp.index(name)})
    for name in met:
        aid = names.get(name)
        if aid is not None and (name not in met0 or met0.index(name) != met.index(name)):
            eff(aid, "bond", "set_met", {"order": met.index(name)})
    return out
