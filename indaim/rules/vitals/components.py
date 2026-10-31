"""생체 상태 — actor.vitals {statuses: [상태], clock: 상태 시계, by_event: {상태: 분}}.

  statuses  붙은 상태(저작 순서가 아니라 붙은 순서 — 옛 session.statuses 의미 그대로, 빈 목록도 기록)
  clock     {applied, cleared, intake, stage, cared, eased, latent} (옛 status_clock 항목)
  by_event  사건이 붙이고 아직 풀지 않은 상태(제거 판정 근거 — 사건 진행 이식 때 효과가 된다)
확정(settle): 판정 로직(logic.decide_turn) → 부여/해제 반영 → 바뀐 인물마다 효과.
"""
from indaim.kernel import WORLD, Component, make_effect
from indaim.kernel.components import SINGLE_VALUE, write

from .._common import keys, reject
from . import logic


def _set(field):
    def op(view, e):
        v = keys(e, (field,))[field]
        if field == "statuses" and not (isinstance(v, list) and all(isinstance(x, str) and x for x in v)):
            reject(e, "statuses는 상태 이름 목록")
        if field == "clock" and not isinstance(v, dict):
            reject(e, "clock은 객체")
        return [write(e, (field,), SINGLE_VALUE, v)]
    return op


COMPONENTS = (Component("vitals", "actor", "rules.vitals", {"set_statuses": _set("statuses"), "set_clock": _set("clock")}),)


def settle(view, inp: dict, *, effect_prefix: str) -> list:
    """한 턴의 상태 확정 효과.

    inp: merged·pc_name·facts·apply_in·clear_in·consumed·used·status_scores·places·place_groups·seed (logic.decide_turn
    인자) + names {이름: ActorId}(옛 이름 키 ↔ 행위자 — T-6에서 키가 ActorId로 바뀌면 사라진다)."""
    names = inp["names"]
    statuses, clocks = {}, {}
    for name, aid in names.items():
        v = view.read("actor", aid, "vitals") or {}
        if "statuses" in v:
            statuses[name] = v["statuses"]
        if "clock" in v:
            clocks[name] = v["clock"]
    un = view.read(WORLD, "", "unresolved") or {}
    statuses.update(un.get("statuses") or {})
    clocks.update(un.get("status_clock") or {})
    td = logic.decide_turn(inp["merged"], pc_name=inp["pc_name"], statuses=statuses, clocks=clocks, facts=inp["facts"],
                           apply_in=inp["apply_in"], clear_in=inp["clear_in"], consumed=inp["consumed"], used=inp["used"],
                           status_scores=inp["status_scores"], places=inp["places"], place_groups=inp["place_groups"],
                           seed=inp["seed"])
    new_st = logic.apply_lists(statuses, td.apply, td.clear)
    out = []
    for name in sorted(set(new_st) | set(td.clocks)):
        aid = names.get(name)
        if aid is None:
            continue
        for field, new, old in (("statuses", new_st, statuses), ("clock", td.clocks, clocks)):
            if name in new and (name not in old or new[name] != old[name]):
                out.append(make_effect(f"{effect_prefix}.vitals.{field}.{aid}", "actor", aid, "vitals", f"set_{field}",
                                       {field: new[name]}, cause="SETTLE", by="rules.vitals"))
    return out
