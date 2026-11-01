"""성장 포인트·등급·성장 사용·실패 누적 성장 반영의 순수 계산(리팩터링 T-3 — core.growth에서 옮김).

시나리오 growth 문법은 core.growth 머리 주석이 정본 설명이다. 입력은 시나리오 원문(sd)·PC 게임 프로필 사전,
출력은 새 프로필과 보고 줄 — 세션·준비 객체를 모른다.
"""
from __future__ import annotations

import copy

from indaim.rules.checks.logic import SKILL_FIELD, owned_skills

POINTS_FIELD = "성장 포인트"
DEFAULT_COSTS = {"stat": 2, "skill": 3}
DEFAULT_STAT_CAP = 20


def capabilities(sd) -> frozenset:
    caps = ((sd or {}).get("scenario_schema") or {}).get("capabilities") if isinstance(sd, dict) else None
    return frozenset(caps) if isinstance(caps, dict) else frozenset()


def growth_spec(sd) -> dict | None:
    return ((sd or {}).get("growth") or {}) if "growth" in capabilities(sd) else None


def stat_cap(sd, stat_name: str) -> int:
    """성장 상한 — growth.stat_max(명시) > ability_stat_max(정수 또는 스탯별 dict) > 20."""
    sd = sd or {}
    for src in ((sd.get("growth") or {}).get("stat_max"), sd.get("ability_stat_max")):
        if isinstance(src, dict):
            src = src.get(stat_name)
        if isinstance(src, int) and not isinstance(src, bool) and src > 0:
            return src
    return DEFAULT_STAT_CAP


def points(profile) -> int:
    try:
        return max(0, int((profile or {}).get(POINTS_FIELD) or 0))
    except (TypeError, ValueError):
        return 0


def spend_options(sd, prof, scene_place=None) -> list:
    """[(키, 설명, 비용)] — 지금 쓸 수 있는 성장(능력치:이름 / 기술:이름). 포인트·장소·상한을 거른다."""
    spec = growth_spec(sd)
    if spec is None:
        return []
    places = spec.get("places")
    if places and scene_place not in places:
        return []
    prof = prof or {}
    pts = points(prof)
    costs = {**DEFAULT_COSTS, **(spec.get("costs") or {})}
    out = []
    if pts >= costs["stat"]:
        for st in (sd or {}).get("ability_stats") or ():
            try:
                cur = int(prof.get(st))
            except (TypeError, ValueError):
                continue
            if cur < stat_cap(sd, st):
                out.append((f"능력치:{st}", f"{st} {cur} → {cur + 1}", costs["stat"]))
    if pts >= costs["skill"]:
        owned = set(owned_skills(prof))
        job = prof.get("직업")
        pool = list(((spec.get("learnable") or {}).get(job) or
                     ((((sd or {}).get("skills") or {}).get("직업") or {}).get(job) or {})))
        for n in pool:
            if n not in owned:
                out.append((f"기술:{n}", f"{n} 익히기", costs["skill"]))
    return out


def apply_profile(sd, prof: dict, changes: dict, choice, scene_place=None) -> tuple:
    """(새 프로필, 보고 줄) — 사건 지급·기록(changes = {points, records}) + 이번 턴 성장 사용(choice 키 또는 None)."""
    spec = growth_spec(sd)
    if spec is None:
        return prof, []
    prof, lines = copy.deepcopy(prof), []
    if changes.get("points"):
        prof[POINTS_FIELD] = points(prof) + changes["points"]
        lines.append(f"성장 포인트 +{changes['points']} → {prof[POINTS_FIELD]}")
    for f, v in sorted((changes.get("records") or {}).items()):
        prof[f] = v
        labels = ((spec.get("records") or {}).get(f) or {}).get("labels") or []
        lines.append(f"{f} → {labels[v - 1] if isinstance(v, int) and 0 < v <= len(labels) else v}")
    if choice and choice != "NONE":
        opt = next((o for o in spend_options(sd, prof, scene_place) if o[0] == choice), None)
        if opt is None:
            lines.append(f"성장 {choice} — 지금 쓸 수 없음(포인트·장소·상한)")
        else:
            kind, _, name = choice.partition(":")
            prof[POINTS_FIELD] = points(prof) - opt[2]
            if kind == "능력치":
                prof[name] = int(prof[name]) + 1
            else:
                raw = prof.get(SKILL_FIELD)
                cur = owned_skills(prof) + [name]
                prof[SKILL_FIELD] = cur if isinstance(raw, list) else ", ".join(cur)
            lines.append(f"성장: {opt[1]} (−{opt[2]}점, 남은 {prof[POINTS_FIELD]})")
    return prof, lines


def apply_staged(prof: dict, events, uid) -> dict:
    """판정 실패 누적 성장(굴림 때 정한 결과) — 그 PC의 (스탯, 새 값)을 프로필에 반영한 새 프로필."""
    prof = copy.deepcopy(prof)
    for u, stat, new_value in events or ():
        if str(u) == str(uid) and stat in prof:
            prof[stat] = new_value
    return prof
