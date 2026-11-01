"""성장 — actor.stats {profile: PC 게임 수치(능력치·스킬·직업·성장 포인트·등급 기록), fail: {스탯: 실패 누적}}.

확정(settle): 판정 실패 누적 성장(굴림 때 정한 결과 — staged) → 사건 지급·기록 + 이번 턴 성장 사용(turn).
옛 실패 누적 키는 '이름:스탯'이라 names로 잇는다(T-6에서 사라짐).
"""
from indaim.kernel import Component, make_effect
from indaim.kernel.components import SINGLE_VALUE, write

from .._common import keys, reject
from . import logic

GAME_KEYS_EXTRA = ("직업",)


def game_keys(sd) -> set:
    """PC 프로필 중 게임 수치 키(규칙이 읽고 쓰는 것) — 그 밖은 서술 프로필."""
    from indaim.rules.checks.logic import SKILL_FIELD
    g = (sd or {}).get("growth") or {}
    return set((sd or {}).get("ability_stats") or ()) | {SKILL_FIELD, *GAME_KEYS_EXTRA} | \
        ({logic.POINTS_FIELD} | set(g.get("records") or ()) if logic.growth_spec(sd) is not None else set())


def _set(field):
    def op(view, e):
        v = keys(e, ("value",))["value"]
        if not isinstance(v, dict):
            reject(e, f"{field}는 객체")
        return [write(e, (field,), SINGLE_VALUE, v)]
    return op


COMPONENTS = (Component("stats", "actor", "rules.growth", {"set_profile": _set("profile"), "set_fail": _set("fail")}),)


def settle(view, inp: dict, *, effect_prefix: str) -> list:
    """inp: names {이름: ActorId} · sd · pc_uid · pc_actor · staged {events, fail_counts|None}? · turn {changes, choice, scene_place}?"""
    names, aid_pc = inp["names"], inp["pc_actor"]
    st = view.read("actor", aid_pc, "stats") or {}
    prof0 = st.get("profile") or {}
    prof = prof0
    out = []
    staged = inp.get("staged")
    if staged:
        prof = logic.apply_staged(prof, staged.get("events"), inp["pc_uid"])
        fc = staged.get("fail_counts")
        if fc is not None:
            per = {}
            for k, n in fc.items():
                name, _, stat = k.rpartition(":")
                if name in names:
                    per.setdefault(names[name], {})[stat] = n
            touched = set(per) | {a for a in names.values() if "fail" in (view.read("actor", a, "stats") or {})}
            for aid in sorted(touched):
                new = per.get(aid, {})
                if new != ((view.read("actor", aid, "stats") or {}).get("fail") or {}):
                    out.append(make_effect(f"{effect_prefix}.growth.fail.{aid}", "actor", aid, "stats", "set_fail",
                                           {"value": new}, cause="SETTLE", by="rules.growth"))
    turn = inp.get("turn")
    if turn:
        prof, _lines = logic.apply_profile(inp["sd"], prof, turn["changes"], turn["choice"], turn["scene_place"])
    if prof != prof0:
        out.append(make_effect(f"{effect_prefix}.growth.profile.{aid_pc}", "actor", aid_pc, "stats", "set_profile",
                               {"value": prof}, cause="SETTLE", by="rules.growth"))
    return out
