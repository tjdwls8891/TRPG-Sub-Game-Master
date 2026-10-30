# 상태이상 조건 환경 (v7.38.0, 사용자 결정 D5 — 계획서 v1 §2)
#
# 공용 상태(data/common_status_effects.json)와 시나리오 상태(status_effects)를 구분하고, 상태별 선택 키 `rules`를
# 코드가 평가한다. DYNAMIC 세션(정본 시계)의 PC만 대상이다 — LEGACY와 NPC는 지금처럼 추출 점수 임계만 쓴다.
#
#   rules = {"apply_gate": C, "apply_auto": C, "clear_auto": C, "clear_gate": C}
#     apply_gate  추출 점수 71↑ 부여는 C가 TRUE일 때만(모름 = 부여 안 함)
#     apply_auto  C가 TRUE면 코드가 부여
#     clear_auto  C가 TRUE면 코드가 해제(코드 해제가 같은 턴 부여보다 우선)
#     clear_gate  추출 점수 30↓ 해제는 C가 TRUE일 때만 — rules가 있는데 clear_gate가 없으면 추출 해제 금지(D5-a 가)
#
#   조건 C(JSON AST, eval 없음 — 3값):
#     {"all": [C…]} · {"any": [C…]} · {"not": C} · {"const": bool}
#     {"status_time": "SINCE_INTAKE_GTE", "group": g, "minutes": n}   마지막 섭취(없으면 세션 시작) 뒤 n분↑
#     {"status_time": "SINCE_APPLIED_GTE", "minutes": n}              이 상태가 붙은 뒤 n분↑
#     {"status_time": "SINCE_STATUS_GTE", "status": s, "minutes": n}  다른 상태가 n분↑ 이어짐
#     {"status_time": "SINCE_CLEARED_GTE", "status": s, "minutes": n} 그 상태가 없고, 풀린 지 n분↑(v7.41 — 붙은 적 없으면 세션 시작부터)
#     {"latent_due": s}          잠복 판정(`latent`)이 s를 정했고 발현 시각이 됨(v7.41)
#     {"consumed": g}            이번 턴 그 묶음 물건 CONSUME
#     {"used": g}                이번 턴 그 묶음 물건 USE 또는 CONSUME
#     {"place_within": PlaceId}  장면 장소가 그곳이거나 그 아래
#     {"place_group": g}         장면 장소나 그 상위가 시나리오 `status_places[g]` 목록에 있음(v7.40 — 미선언 묶음은 모름)
#     {"daypart_in": [시간대…]}  현재 시간대
#     {"activity_in": [활동…], "min_minutes": n?}   이번 턴 활동(SLEEP·WAIT·TREAT…)과 그 턴의 경과 분
#     {"status_active": s}       그 상태가 지금 붙어 있음
#     {"wearing": 태그}          PC가 그 효과 태그의 물건을 입거나 들고 있음(v7.46 — 이번 턴 입기·벗기 반영, 공용 어휘)
#
#   시나리오 최상위 `intake_groups: {묶음: [물건 낱말…]}` — 물건 종류의 낱말 하나가 목록과 같으면 그 묶음.
#   `status_common_exclude: [이름…]` — 공용 상태 빼기. 상태 `excludes: [이름…]` — 상호 배타(붙은 쪽이 다른 쪽을 푼다).
#   상태 시계 `status_clock`(세션 필드, 되감기 대상): {PC 이름: {"applied": {상태: 분}, "cleared": {상태: 분},
#     "intake": {묶음: 분}, "latent": {대상 상태: {"from", "at", "due"|None}}}}.
#   v7.41 상태 선택 키: `deep: true` — 심화 상태(코드가 시간으로만 정한다, 추출 점수 무시 — 규칙에 gate const false).
#     `latent: {"target": 상태, "chance": 0~100, "onset_minutes": [최소, 최대]}` — 이 상태가 붙는 순간 코드가 (세션 시드,
#     PC, 시각)으로 대상 상태의 발현 여부·시각을 결정적으로 정한다.
#
#   v7.47 단계·불이익·처치(사용자 결정 S1~S6 — 계획서 v1):
#     `stages: [이름…]` 단계(1부터). `stage_mods: [{분야: 값}…]`(단계별) 또는 `mods: {분야: 값}` — 판정 상태 보정 H
#       ('전체'는 모든 판정, 분야 이름은 그 분야 판정에 더한다 — 분야 = 공용 효과 어휘 domains).
#     `care: {사용 효과: down|treat|ease}` — 이번 턴 PC에게 쓴 물건의 사용 효과(core.items)로 코드가 처치한다.
#       down = 한 단계 낮춤(1단계면 해제) · treat = 한 단계 낮춤(1단계면 처치 기록만 — 시간 규칙이 마무리) ·
#       ease = EASE_MINUTES 동안 판정 불이익을 한 단계 낮은 값으로(원인은 그대로).
#     `lower_by: "care"` — 추출이 단계를 낮추거나 해제하지 못한다(처치·시간 규칙만).
#     `latent.cancel_by: 사용 효과` — 발현 전 그 효과로 처치하면 잠복 취소 · `latent.min_stage: n` — 그 단계 이상에서만 판정.
#     rules 키 `raise_auto: C` — C가 TRUE면 코드가 한 단계 올린다.
#     조건 원자: {"stage_min": n, "status"?: s}(그 상태 — 없으면 이 상태 — 가 n단계 이상) ·
#       {"status_time": "SINCE_CARED_GTE", "minutes": n, "status"?: s}(마지막 처치 뒤 n분↑, 처치 없으면 FALSE).
#     상태 시계 추가 키: "stage" {상태: 단계} · "cared" {상태: 분} · "eased" {상태: 끝나는 분}(비면 키 없음).
#
#   v7.48(생존물 A — 계획서 v1 · D-3 나):
#     조건 원자 {"moved": true}(이번 턴 PC가 장소를 옮김) · {"noise_at_least": QUIET|NORMAL|LOUD}(이번 턴 PC가 낸 소리).
#     상태 선택 키 `ending: {id, title, epilogue, tone?}` — PC에게 붙으면 그 결말로 세션이 끝난다(사망 등).
#     NPC도 같은 규칙으로 판정한다(D-3 나). NPC는 섭취·활동·착용·장소·이번 턴 사용을 추적하지 않으므로 그 조건은 '모름' —
#     자동 부여·해제(auto)는 모름이면 일어나지 않고, 추출 부여·해제의 근거 조건(gate)은 모름이면 막지 않는다.
from __future__ import annotations

import copy
from dataclasses import dataclass, field

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
SCOPE_COMMON, SCOPE_SCENARIO = "공용", "시나리오"
RULE_KEYS = ("apply_gate", "apply_auto", "clear_auto", "clear_gate", "raise_auto")
STATUS_TIMES = ("SINCE_INTAKE_GTE", "SINCE_APPLIED_GTE", "SINCE_STATUS_GTE", "SINCE_CLEARED_GTE", "SINCE_CARED_GTE")
CARE_ACTIONS = ("down", "treat", "ease")
NOISE_LEVELS = ("QUIET", "NORMAL", "LOUD")       # v7.48(E4) 이번 턴 PC 행동의 소리(작은 것부터)
EASE_MINUTES = 240
ALL_DOMAIN = "전체"


@dataclass
class Ctx:
    """한 턴 커밋 시점의 판정 재료(정본 투영 기준 — 순수 값)."""
    now: int
    start_minute: int
    turn_minutes: int = 0
    place: str | None = None
    place_chain: tuple = ()                   # 장면 장소와 그 상위(계층)
    place_groups: dict = field(default_factory=dict)   # 시나리오 status_places {묶음: {장소…}}
    daypart: str | None = None
    activity: str | None = None
    consumed: frozenset = frozenset()         # 이번 턴 CONSUME한 묶음
    used: frozenset = frozenset()             # 이번 턴 USE·CONSUME한 묶음
    active: frozenset = frozenset()           # 판정 전 PC 상태
    clock: dict = field(default_factory=dict)  # 이 PC의 status_clock 항목
    seed: str = ""                            # 잠복 판정의 결정적 근거(세션 narrative_seed + PC)
    worn: frozenset = frozenset()             # v7.46 PC 착용 효과 태그(없으면 빈 집합 — 입은 것이 없다는 사실)
    care: frozenset = frozenset()             # v7.47 이번 턴 PC에게 쓴 물건의 사용 효과(실패·물건 상태로 꺼진 것 제외)
    stage_in: dict = field(default_factory=dict)   # v7.47 추출이 본 PC 상태 단계 {상태: 단계}
    moved: object = None                      # v7.48 이번 턴 장소를 옮겼는가(None = 모름)
    noise: object = None                      # v7.48 이번 턴 소리(NOISE_LEVELS, None = 모름)
    npc: bool = False                         # v7.48(D-3) NPC 판정 — 추적하지 않는 사실은 모름


def _t_all(vals):
    vals = list(vals)
    return FALSE if FALSE in vals else (UNKNOWN if UNKNOWN in vals else TRUE)


def _t_any(vals):
    vals = list(vals)
    return TRUE if TRUE in vals else (UNKNOWN if UNKNOWN in vals else FALSE)


def _b(x):
    return TRUE if x else FALSE


def evaluate(cond, ctx: Ctx, status: str) -> str:
    if not isinstance(cond, dict):
        return UNKNOWN
    if "all" in cond:
        return _t_all(evaluate(c, ctx, status) for c in cond["all"])
    if "any" in cond:
        return _t_any(evaluate(c, ctx, status) for c in cond["any"])
    if "not" in cond:
        return {TRUE: FALSE, FALSE: TRUE}.get(evaluate(cond["not"], ctx, status), UNKNOWN)
    if "const" in cond:
        return _b(cond["const"] is True)
    if "status_time" in cond:
        k, n = cond["status_time"], int(cond.get("minutes") or 0)
        if k == "SINCE_INTAKE_GTE" and ctx.npc:
            return UNKNOWN
        if k == "SINCE_INTAKE_GTE":
            last = (ctx.clock.get("intake") or {}).get(cond.get("group"), ctx.start_minute)
            return _b(ctx.now - int(last) >= n)
        if k == "SINCE_CARED_GTE":
            at = (ctx.clock.get("cared") or {}).get(cond.get("status") or status)
            return FALSE if at is None else _b(ctx.now - int(at) >= n)
        if k == "SINCE_CLEARED_GTE":
            name = cond.get("status")
            if name in ctx.active:
                return FALSE
            last = (ctx.clock.get("cleared") or {}).get(name, ctx.start_minute)
            return _b(ctx.now - int(last) >= n)
        name = status if k == "SINCE_APPLIED_GTE" else cond.get("status")
        if name not in ctx.active:
            return FALSE
        at = (ctx.clock.get("applied") or {}).get(name)
        return UNKNOWN if at is None else _b(ctx.now - int(at) >= n)
    if "latent_due" in cond:
        rec = (ctx.clock.get("latent") or {}).get(cond["latent_due"]) or {}
        return _b(rec.get("due") is not None and ctx.now >= int(rec["due"]))
    if "consumed" in cond:
        return UNKNOWN if ctx.npc else _b(cond["consumed"] in ctx.consumed)
    if "used" in cond:
        return UNKNOWN if ctx.npc else _b(cond["used"] in ctx.used)
    if "moved" in cond:
        return UNKNOWN if ctx.moved is None else _b(bool(ctx.moved) == bool(cond["moved"]))
    if "noise_at_least" in cond:
        if ctx.noise not in NOISE_LEVELS:
            return UNKNOWN
        return _b(NOISE_LEVELS.index(ctx.noise) >= NOISE_LEVELS.index(cond["noise_at_least"]))
    if "place_within" in cond:
        return UNKNOWN if ctx.place is None else _b(cond["place_within"] in ctx.place_chain)
    if "place_group" in cond:
        members = ctx.place_groups.get(cond["place_group"])
        return UNKNOWN if ctx.place is None or members is None else _b(bool(members & set(ctx.place_chain)))
    if "daypart_in" in cond:
        return UNKNOWN if ctx.daypart is None else _b(ctx.daypart in (cond["daypart_in"] or ()))
    if "activity_in" in cond:
        if ctx.npc:
            return UNKNOWN
        ok = ctx.activity in (cond["activity_in"] or ())
        return _b(ok and ctx.turn_minutes >= int(cond.get("min_minutes") or 0))
    if "status_active" in cond:
        return _b(cond["status_active"] in ctx.active)
    if "stage_min" in cond:
        name = cond.get("status") or status
        if name not in ctx.active:
            return FALSE
        return _b(int((ctx.clock.get("stage") or {}).get(name, 1)) >= int(cond["stage_min"]))
    if "wearing" in cond:
        return UNKNOWN if ctx.npc else _b(cond["wearing"] in ctx.worn)
    return UNKNOWN


def check_condition(cond, *, groups=(), statuses=(), path="", place_groups=None) -> list:
    """저작 검사 — 오류 문구 목록. groups=None이면 섭취 묶음 참조는, place_groups=None이면 장소 묶음 참조는 검사하지 않는다."""
    errs = []
    if not isinstance(cond, dict) or len(cond) == 0:
        return [f"{path}: 조건은 객체여야 합니다"]
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            return [f"{path}.{key}: 비어 있지 않은 목록이어야 합니다"]
        for i, c in enumerate(cond[key]):
            errs += check_condition(c, groups=groups, statuses=statuses, path=f"{path}.{key}[{i}]",
                                    place_groups=place_groups)
        return errs
    if "not" in cond:
        return check_condition(cond["not"], groups=groups, statuses=statuses, path=f"{path}.not",
                               place_groups=place_groups)
    if "const" in cond:
        return [] if isinstance(cond["const"], bool) else [f"{path}.const: true/false"]
    if "status_time" in cond:
        k = cond["status_time"]
        if k not in STATUS_TIMES:
            errs.append(f"{path}: 알 수 없는 status_time {k!r}")
        if not isinstance(cond.get("minutes"), int) or cond["minutes"] < 0:
            errs.append(f"{path}: minutes는 0 이상 정수")
        if k == "SINCE_INTAKE_GTE" and groups is not None and cond.get("group") not in groups:
            errs.append(f"{path}: 정의되지 않은 섭취 묶음 {cond.get('group')!r}(intake_groups)")
        if k in ("SINCE_STATUS_GTE", "SINCE_CLEARED_GTE") and cond.get("status") not in statuses:
            errs.append(f"{path}: 알 수 없는 상태 {cond.get('status')!r}")
        if k == "SINCE_CARED_GTE" and cond.get("status") is not None and cond["status"] not in statuses:
            errs.append(f"{path}: 알 수 없는 상태 {cond.get('status')!r}")
        return errs
    for key in ("consumed", "used"):
        if key in cond:
            ok = groups is None or cond[key] in groups
            return [] if ok else [f"{path}: 정의되지 않은 섭취 묶음 {cond[key]!r}(intake_groups)"]
    if "status_active" in cond:
        return [] if cond["status_active"] in statuses else [f"{path}: 알 수 없는 상태 {cond['status_active']!r}"]
    if "moved" in cond:
        return [] if isinstance(cond["moved"], bool) else [f"{path}: moved는 true/false"]
    if "noise_at_least" in cond:
        return [] if cond["noise_at_least"] in NOISE_LEVELS else [f"{path}: noise_at_least는 {NOISE_LEVELS}"]
    if "stage_min" in cond:
        ok = isinstance(cond["stage_min"], int) and cond["stage_min"] >= 1 \
            and (cond.get("status") is None or cond["status"] in statuses)
        return [] if ok else [f"{path}: stage_min은 1 이상 정수(status는 알려진 상태)"]
    if "wearing" in cond:
        from .items import effect_vocab
        return [] if cond["wearing"] in effect_vocab()["tags"] else [f"{path}: 알 수 없는 효과 태그 {cond['wearing']!r}"]
    if "latent_due" in cond:
        return [] if cond["latent_due"] in statuses else [f"{path}: 알 수 없는 상태 {cond['latent_due']!r}"]
    if "place_within" in cond:
        return [] if isinstance(cond["place_within"], str) else [f"{path}: place_within은 장소 이름"]
    if "place_group" in cond:
        g = cond["place_group"]
        if not isinstance(g, str):
            return [f"{path}: place_group은 묶음 이름"]
        ok = place_groups is None or g in place_groups
        return [] if ok else [f"{path}: 정의되지 않은 장소 묶음 {g!r}(status_places)"]
    for key in ("daypart_in", "activity_in"):
        if key in cond:
            v = cond[key]
            ok = isinstance(v, list) and v and all(isinstance(x, str) for x in v)
            return [] if ok else [f"{path}: {key}는 문자열 목록"]
    return [f"{path}: 알 수 없는 조건 {sorted(cond)}"]


# ══════════════════════════════════════════════════════════════
#  정의
# ══════════════════════════════════════════════════════════════

def groups_of(scenario_data) -> dict:
    g = (scenario_data or {}).get("intake_groups")
    return {k: [str(w) for w in v] for k, v in g.items() if isinstance(v, list)} if isinstance(g, dict) else {}


def place_groups_of(scenario_data) -> dict:
    g = (scenario_data or {}).get("status_places")
    return {k: {str(x) for x in v} for k, v in g.items() if isinstance(v, list)} if isinstance(g, dict) else {}


def groups_for_kind(kind: str, groups: dict) -> set:
    """물건 종류 → 묶음(종류의 낱말 하나가 목록 낱말과 같을 때)."""
    words = set(str(kind or "").split())
    return {g for g, ws in groups.items() if words & set(ws)}


def check_definitions(scenario_data, merged: dict) -> list:
    """저작 검사 — (코드, 문구) 목록. rules·excludes·intake_groups."""
    out = []
    raw = (scenario_data or {}).get("intake_groups")
    groups = groups_of(scenario_data) if raw is not None else None   # 묶음 미저작 = 묶음 조건은 거짓(부여 없음)
    if raw is not None and (not isinstance(raw, dict) or any(
            not isinstance(v, list) or not all(isinstance(w, str) and w.strip() for w in v) for v in raw.values())):
        out.append(("STATUS_INTAKE_GROUPS", "intake_groups는 {묶음: [낱말…]}"))
    raw_p = (scenario_data or {}).get("status_places")
    pgroups = place_groups_of(scenario_data) if raw_p is not None else None   # 미저작 = 장소 묶음 조건은 모름
    places = (scenario_data or {}).get("places") or {}
    if raw_p is not None and (not isinstance(raw_p, dict) or any(
            not isinstance(v, list) or not all(isinstance(x, str) and x in places for x in v) for v in raw_p.values())):
        out.append(("STATUS_PLACES", "status_places는 {묶음: [실재 장소…]}"))
    names = set(merged)
    for name, eff in merged.items():
        rules = eff.get("rules")
        if rules is not None:
            if not isinstance(rules, dict) or set(rules) - set(RULE_KEYS):
                out.append(("STATUS_RULES", f"{name}: rules 키는 {RULE_KEYS}만"))
            else:
                for k, c in rules.items():
                    out += [("STATUS_RULES", f"{name}: {e}")
                            for e in check_condition(c, groups=groups, statuses=names, path=k, place_groups=pgroups)]
        if "deep" in eff and not isinstance(eff["deep"], bool):
            out.append(("STATUS_DEEP", f"{name}: deep은 true/false"))
        lat = eff.get("latent")
        if lat is not None:
            ok = isinstance(lat, dict) and lat.get("target") in names and isinstance(lat.get("chance"), int) \
                and 0 <= lat["chance"] <= 100 and isinstance(lat.get("onset_minutes"), list) \
                and len(lat["onset_minutes"]) == 2 and all(isinstance(x, int) and x >= 0 for x in lat["onset_minutes"]) \
                and lat["onset_minutes"][0] <= lat["onset_minutes"][1]
            if not ok:
                out.append(("STATUS_LATENT", f"{name}: latent는 {{target(상태), chance(0~100), onset_minutes[최소,최대]}}"))
            elif lat.get("cancel_by") is not None and lat["cancel_by"] not in _use_effects():
                out.append(("STATUS_LATENT", f"{name}: latent.cancel_by {lat['cancel_by']!r}는 공용 사용 효과가 아님"))
            elif lat.get("min_stage") is not None and not (isinstance(lat["min_stage"], int) and lat["min_stage"] >= 1):
                out.append(("STATUS_LATENT", f"{name}: latent.min_stage는 1 이상 정수"))
        out += [("STATUS_STAGES", f"{name}: {e}") for e in _check_stage_keys(eff)]
        for other in eff.get("excludes") or ():
            if other not in names:
                out.append(("STATUS_EXCLUDES", f"{name}: 알 수 없는 배타 상태 {other!r}"))
            elif name not in (merged[other].get("excludes") or ()):
                out.append(("STATUS_EXCLUDES", f"{name}↔{other}: excludes는 양쪽에 적어야 합니다"))
    for x in (scenario_data or {}).get("status_common_exclude") or ():
        if not isinstance(x, str):
            out.append(("STATUS_COMMON_EXCLUDE", "status_common_exclude는 이름 목록"))
    return out


# ══════════════════════════════════════════════════════════════
#  판정
# ══════════════════════════════════════════════════════════════

def _use_effects() -> dict:
    from .items import effect_vocab
    return effect_vocab().get("use_effects") or {}


def _domains() -> set:
    from .items import effect_vocab
    return set(effect_vocab().get("domains") or {}) | {ALL_DOMAIN}


def _check_stage_keys(eff: dict) -> list:
    """v7.47 stages·stage_mods·mods·care·lower_by 저작 검사."""
    errs = []
    st = eff.get("stages")
    if st is not None and (not isinstance(st, list) or len(st) < 2 or not all(isinstance(x, str) and x for x in st)):
        errs.append("stages는 이름 2개 이상의 목록")
    dom = _domains()

    def bad_mods(m):
        return not isinstance(m, dict) or any(k not in dom or not isinstance(v, int) for k, v in m.items())
    sm = eff.get("stage_mods")
    if sm is not None and (not isinstance(st, list) or not isinstance(sm, list) or len(sm) != len(st)
                           or any(bad_mods(m) for m in sm)):
        errs.append("stage_mods는 단계 수만큼의 {분야: 정수} 목록(분야 = 공용 domains 또는 '전체')")
    if eff.get("mods") is not None and bad_mods(eff["mods"]):
        errs.append("mods는 {분야: 정수}(분야 = 공용 domains 또는 '전체')")
    care = eff.get("care")
    if care is not None and (not isinstance(care, dict) or any(
            k not in _use_effects() or v not in CARE_ACTIONS for k, v in care.items())):
        errs.append(f"care는 {{공용 사용 효과: {'|'.join(CARE_ACTIONS)}}}")
    if eff.get("lower_by") not in (None, "care"):
        errs.append("lower_by는 'care'만")
    end = eff.get("ending")
    if end is not None and not (isinstance(end, dict) and all(isinstance(end.get(k), str) and end[k].strip()
                                                               for k in ("id", "title", "epilogue"))):
        errs.append("ending은 {id, title, epilogue, tone?} 문자열")
    return errs


def status_ending(merged: dict, statuses) -> dict | None:
    """v7.48(E2): 그 인물에게 붙은 상태 중 결말을 가진 첫 상태의 결말(저작 순서), 없으면 None."""
    have = set(statuses or ())
    for name, eff in (merged or {}).items():
        end = (eff or {}).get("ending")
        if name in have and isinstance(end, dict):
            return {**end, "status": name}
    return None


def stages_of(eff) -> list:
    st = (eff or {}).get("stages")
    return list(st) if isinstance(st, list) else []


def stage_of(clock, name: str) -> int:
    return int(((clock or {}).get("stage") or {}).get(name, 1))


def mods_at(eff, stage: int) -> dict:
    """그 단계의 판정 불이익 {분야: 값}(단계 0 = 없음)."""
    if stage < 1:
        return {}
    sm, st = (eff or {}).get("stage_mods"), stages_of(eff)
    if isinstance(sm, list) and st:
        return dict(sm[min(stage, len(sm)) - 1] or {})
    return dict((eff or {}).get("mods") or {})


def effective_stage(eff, clock, name: str, now) -> int:
    """일시 완화(ease) 중이면 한 단계 낮은 값으로 계산한다."""
    st = stage_of(clock, name) if stages_of(eff) else 1
    until = ((clock or {}).get("eased") or {}).get(name)
    return st - 1 if until is not None and now is not None and int(until) > int(now) else st


def status_mod(merged: dict, statuses, clock, now, domain) -> tuple:
    """v7.47(S2) PC 판정 상태 보정 H — (값, 근거 문구). '전체' + 그 분야 값의 합, 판정 범위(-15~+15)로 자른다."""
    from .judgment import STATUS_RANGE
    total, parts = 0, []
    for s in statuses or ():
        eff = (merged or {}).get(s)
        if not isinstance(eff, dict):
            continue
        m = mods_at(eff, effective_stage(eff, clock, s, now))
        v = int(m.get(ALL_DOMAIN, 0)) + (int(m.get(domain, 0)) if domain and domain != ALL_DOMAIN else 0)
        if v:
            total += v
            parts.append(f"{label(merged, s, clock, now)} {v:+d}")
    lo, hi = STATUS_RANGE
    return max(lo, min(hi, total)), " · ".join(parts)


def label(merged: dict, name: str, clock, now=None) -> str:
    """표시 — '부상(중상)' · 완화 중이면 '부상(중상·완화)'."""
    eff = (merged or {}).get(name) or {}
    st = stages_of(eff)
    bits = [st[min(stage_of(clock, name), len(st)) - 1]] if st else []
    until = ((clock or {}).get("eased") or {}).get(name)
    if until is not None and now is not None and int(until) > int(now):
        bits.append("완화")
    return name + (f"({'·'.join(bits)})" if bits else "")


def labels_for(session, name: str) -> list:
    """표시용 — 그 인물의 상태 이름(단계·완화 표기, 단계 기록이 없으면 이름 그대로)."""
    from .utils import get_merged_status_effects
    sts = (getattr(session, "statuses", None) or {}).get(name) or []
    clock = (getattr(session, "status_clock", None) or {}).get(name)
    if not clock:
        return [str(x) for x in sts]
    merged = get_merged_status_effects(getattr(session, "scenario_data", None) or {})
    now = (getattr(session, "world_clock", None) or {}).get("minute_index")
    return [label(merged, str(x), clock, now) for x in sts]


def mods_text(m: dict) -> str:
    return ", ".join(f"{k} {int(v):+d}" for k, v in m.items() if v) or "판정 영향 없음"


@dataclass
class Decision:
    apply: list                 # 추출 DTO 형식 [{target,status,score}] (코드 판정은 score=None)
    clear: list
    clock: dict                 # 이 PC의 새 status_clock 항목
    notes: list                 # 마스터 보고용 한 줄들


def decide(merged: dict, ctx: Ctx, pc_name: str, apply_in, clear_in, groups_consumed=()) -> Decision:
    """PC 대상 추출 후보 + 처치 + 코드 규칙 → 최종 부여·해제·단계. NPC 대상 후보는 손대지 않는다(호출부가 그대로 둔다).

    순서(v7.47): ① 처치(사용 효과 → down·treat·ease, 잠복 취소) ② 추출 단계(올림 허용, 처치 전용 상태는 내림 거부)
    ③ 규칙(gate·auto·raise_auto·배타·잠복)."""
    notes = []
    clock = copy.deepcopy(ctx.clock or {})
    applied = dict(clock.get("applied") or {})
    for s in ctx.active:                      # 시계 없이 붙어 있던 상태(이전 버전) — 지금부터 잰다
        applied.setdefault(s, ctx.now)
    clock["applied"] = applied
    stage = {k: int(v) for k, v in (clock.get("stage") or {}).items() if k in ctx.active}
    cared = {k: v for k, v in (clock.get("cared") or {}).items() if k in ctx.active}
    eased = {k: v for k, v in (clock.get("eased") or {}).items() if k in ctx.active and int(v) > ctx.now}
    for s in ctx.active:
        if stages_of(merged.get(s)):
            stage.setdefault(s, 1)
    clock["stage"], clock["cared"], clock["eased"] = stage, cared, eased
    latent = dict(clock.get("latent") or {})
    ctx = Ctx(**{**ctx.__dict__, "clock": clock})

    def rules(s):
        r = (merged.get(s) or {}).get("rules")
        return r if isinstance(r, dict) else None

    def nstages(s):
        return len(stages_of(merged.get(s)))

    def roll_latent(s, at_stage):
        lat = (merged.get(s) or {}).get("latent")
        if not (isinstance(lat, dict) and lat.get("target")):
            return
        if at_stage < int(lat.get("min_stage") or 1):
            return
        cur = latent.get(lat["target"])
        if cur and cur.get("due") is not None and int(cur["due"]) > ctx.now:
            return                                # 이미 발현 예정 — 다시 굴리지 않는다
        rec = latent_roll(ctx.seed, pc_name, s, ctx.now, lat)
        latent[lat["target"]] = rec
        notes.append(f"{lat['target']} 잠복 판정(코드) — " + ("발현 예정" if rec["due"] is not None else "발현 없음"))

    want_apply, want_clear, cared_now = {}, set(), set()

    def apply_care(s):
        """① 처치 — 이번 턴 PC에게 쓴 물건의 사용 효과. True면 이 처치로 해제(down의 1단계)."""
        done = False
        for eff_name, act in ((merged.get(s) or {}).get("care") or {}).items():
            if eff_name not in ctx.care:
                continue
            cared_now.add(s)
            if act == "ease":
                eased[s] = ctx.now + EASE_MINUTES
                notes.append(f"{s} 증상 완화 {EASE_MINUTES // 60}시간({eff_name}, 코드)")
                continue
            cared[s] = ctx.now
            cur = stage.get(s, 1)
            if nstages(s) and cur > 1:
                stage[s] = cur - 1
                notes.append(f"{label(merged, s, clock)} ← 한 단계 내려감({eff_name} 처치, 코드)")
            elif act == "down":
                done = True
                notes.append(f"{s} 해제({eff_name} 처치, 코드)")
                break
            else:
                notes.append(f"{s} 처치 기록({eff_name}) — 시간이 마무리한다(코드)")
        return done

    def cancel_latents():
        for tgt, rec in list(latent.items()):        # 잠복 취소 — 발현 전 그 효과로 처치
            src = (merged.get(rec.get("from")) or {}).get("latent") or {}
            if src.get("cancel_by") in ctx.care and tgt not in ctx.active and rec.get("due") is not None:
                latent.pop(tgt)
                notes.append(f"{tgt} 잠복 취소({src['cancel_by']} 처치, 코드)")

    for s in sorted(ctx.active):
        if apply_care(s):
            want_clear.add(s)

    # ② 추출 단계 — 붙어 있는 상태(이번 턴 처치한 상태는 처치 결과가 우선 — 처치 전 단계를 다시 올리지 않는다)
    for s, n in (ctx.stage_in or {}).items():
        if s not in ctx.active or s in want_clear or s in cared_now or not nstages(s):
            continue
        n, cur = max(1, min(int(n), nstages(s))), stage.get(s, 1)
        if n > cur:
            stage[s] = n
            notes.append(f"{label(merged, s, clock)} ← 단계 올라감(추출)")
            roll_latent(s, n)
        elif n < cur:
            if (merged.get(s) or {}).get("lower_by") == "care":
                notes.append(f"{s} 단계 내림 보류 — 처치·시간으로만(코드)")
            else:
                stage[s] = n
                notes.append(f"{label(merged, s, clock)} ← 단계 내려감(추출)")

    # ③ 규칙
    def gate_ok(v):
        return v == TRUE or (ctx.npc and v == UNKNOWN)

    for e in apply_in:
        r = rules(e["status"])
        if r and "apply_gate" in r and not gate_ok(evaluate(r["apply_gate"], ctx, e["status"])):
            notes.append(f"{e['status']} 부여 보류 — 근거 조건 미충족(코드)")
            continue
        want_apply[e["status"]] = e
    for e in clear_in:
        s = e["status"]
        if s in want_clear:
            continue
        if (merged.get(s) or {}).get("lower_by") == "care":
            notes.append(f"{s} 해제 보류 — 처치·시간으로만(코드)")
            continue
        r = rules(s)
        if r is not None:
            gate = r.get("clear_gate")
            if gate is None or not gate_ok(evaluate(gate, ctx, s)):
                notes.append(f"{s} 해제 보류 — 해소 근거 미충족(코드)")
                continue
        want_clear.add(s)
    for s in merged:
        r = rules(s)
        if not r:
            continue
        if s in ctx.active or s in want_apply:
            if "clear_auto" in r and evaluate(r["clear_auto"], ctx, s) == TRUE:
                want_clear.add(s)
                want_apply.pop(s, None)
                notes.append(f"{s} 해제(코드 확정)")
                continue
        if s in ctx.active and s not in want_clear and "raise_auto" in r and nstages(s) \
                and stage.get(s, 1) < nstages(s) and evaluate(r["raise_auto"], ctx, s) == TRUE:
            stage[s] = stage.get(s, 1) + 1
            notes.append(f"{label(merged, s, clock)} ← 단계 올라감(코드 확정)")
            roll_latent(s, stage[s])
        if s not in ctx.active and s not in want_apply and "apply_auto" in r \
                and evaluate(r["apply_auto"], ctx, s) == TRUE:
            want_apply[s] = {"target": pc_name, "status": s, "score": None}
            notes.append(f"{s} 부여(코드 확정)")
    for s in list(want_apply):
        if s in want_clear:
            want_apply.pop(s)
    final = (set(ctx.active) - want_clear) | set(want_apply)
    for s in list(want_apply):                # 상호 배타 — 새로 붙은 쪽이 다른 쪽을 푼다
        for o in (merged.get(s) or {}).get("excludes") or ():
            if o in final and o not in want_apply:
                want_clear.add(o)
                final.discard(o)
                notes.append(f"{o} 해제 — {s}와(과) 배타(코드)")
    cleared = dict(clock.get("cleared") or {})
    for s in want_apply:
        if s not in ctx.active:
            applied[s] = ctx.now
            latent.pop(s, None)                   # 발현한 대상의 잠복 기록은 끝
            n = nstages(s)
            st = max(1, min(int((ctx.stage_in or {}).get(s) or 1), n)) if n else 1
            if n:
                stage[s] = st
            roll_latent(s, st)
    for s in [x for x in want_apply if x not in ctx.active]:   # 같은 턴에 다치고 바로 처치한 경우도 처치한다
        if apply_care(s):
            want_apply.pop(s)
            for k in (applied, stage, cared, eased):
                k.pop(s, None)
    cancel_latents()                                           # 같은 턴에 걸린 잠복도 같은 턴 처치로 취소
    for s in want_clear:
        applied.pop(s, None)
        stage.pop(s, None)
        cared.pop(s, None)
        eased.pop(s, None)
        if s in ctx.active:
            cleared[s] = ctx.now
    clock["cleared"] = cleared
    for k, v in (("latent", latent), ("stage", stage), ("cared", cared), ("eased", eased)):
        if v:
            clock[k] = v
        else:
            clock.pop(k, None)
    intake = dict(clock.get("intake") or {})
    for g in groups_consumed:
        intake[g] = ctx.now
    clock["intake"] = intake
    apply_out = [e for s, e in want_apply.items() if s not in ctx.active]
    clear_out = [{"target": pc_name, "status": s, "score": None} for s in sorted(want_clear) if s in ctx.active]
    return Decision(apply_out, clear_out, clock, notes)


def latent_roll(seed: str, pc_name: str, status: str, at: int, lat: dict) -> dict:
    """잠복 판정 — (세션 시드, PC, 원인 상태, 시각)의 결정적 함수(재시도·재시작·재생성에도 같은 결과)."""
    import hashlib
    h = hashlib.sha256(f"status.latent.v1|{seed}|{pc_name}|{status}|{at}".encode("utf-8")).digest()
    roll, span = int.from_bytes(h[:4], "big") % 100, int.from_bytes(h[4:8], "big")
    lo, hi = int(lat["onset_minutes"][0]), int(lat["onset_minutes"][1])
    hit = roll < int(lat.get("chance") or 0)
    return {"from": status, "at": int(at), "due": int(at) + lo + span % (hi - lo + 1) if hit else None}


def deep_statuses(merged: dict) -> set:
    return {n for n, e in (merged or {}).items() if (e or {}).get("deep") is True}


# ══════════════════════════════════════════════════════════════
#  턴 커밋 배선(DYNAMIC) — 추출 계획 적용 직전, 같은 커밋 단위
# ══════════════════════════════════════════════════════════════

def _place_chain(scenario_data, place) -> tuple:
    if not place:
        return ()
    places = (scenario_data or {}).get("places") or {}
    out, x = [], place
    while x and x not in out:
        out.append(x)
        x = ((places.get(x) or {}) if isinstance(places, dict) else {}).get("parent")
    return tuple(out)


def _turn_groups(item_plan, groups) -> tuple:
    from .items import parse_key
    consumed, used = set(), set()
    for op in getattr(item_plan, "ops", None) or ():
        if op.get("op") not in ("CONSUME", "USE") or not op.get("stock"):
            continue
        gs = groups_for_kind(parse_key(op["stock"], getattr(item_plan, "defs", None))[0].kind, groups)
        used |= gs
        if op["op"] == "CONSUME":
            consumed |= gs
    return frozenset(consumed), frozenset(used)


def _stage_in(plan, pc_name) -> dict:
    """v7.47 추출 status_scores의 PC 단계(stage ≥ 1만 — 0·없음은 단계 판단 없음)."""
    out = {}
    for e in ((getattr(plan, "result", None) or {}).get("status_scores") or []):
        if isinstance(e, dict) and e.get("target") == pc_name and isinstance(e.get("stage"), int) \
                and not isinstance(e.get("stage"), bool) and e["stage"] >= 1:
            out[e.get("status")] = e["stage"]
    return out


def resolve_turn(session, prep) -> list:
    """DYNAMIC 턴의 상태 후보를 규칙으로 확정하고 status_clock을 갱신한다. 반환: 보고 줄(빈 목록 = 변화 없음).

    PC와 NPC(v7.48 D-3 나 — 추적하지 않는 사실은 모름)를 판정한다. LEGACY·정본 시계 없음이면 아무것도 안 한다."""
    from . import turn_world as _tw
    from .utils import get_merged_status_effects
    plan = getattr(prep, "extraction_plan", None)
    facts = _tw.status_turn_facts(session, prep)      # H05 경계: 세계 읽기는 turn_world가 한다
    if plan is None or facts is None:
        return []
    sd = getattr(session, "scenario_data", None) or {}
    merged = get_merged_status_effects(sd)
    if not any(isinstance(e.get("rules"), dict) or e.get("care") or e.get("stages") for e in merged.values()):
        return []
    pc_name = next((p.get("name") for p in (session.players or {}).values() if p.get("name")), None)
    if not pc_name:
        return []
    now, before, place = facts["now"], facts["before"], facts["place"]
    groups = groups_of(sd)
    consumed, used = _turn_groups(getattr(plan, "item_plan", None), groups)
    consumed, used = consumed | facts.get("care_consumed", frozenset()), used | facts.get("care_used", frozenset())
    store = getattr(session, "status_clock", None) or {}
    ctx = Ctx(now=now, start_minute=facts["start"], turn_minutes=max(0, now - before),
              place=place, place_chain=_place_chain(sd, place), place_groups=place_groups_of(sd), daypart=facts["daypart"], activity=facts["activity"],
              consumed=consumed, used=used, active=frozenset((session.statuses or {}).get(pc_name) or ()),
              clock=copy.deepcopy(store.get(pc_name) or {}),
              seed=f"{getattr(session, 'narrative_seed', '') or ''}", worn=facts.get("worn") or frozenset(),
              care=facts.get("care") or frozenset(), stage_in=_stage_in(plan, pc_name),
              moved=facts.get("moved"), noise=facts.get("noise"))
    mine = lambda e: e.get("target") == pc_name  # noqa: E731
    d = decide(merged, ctx, pc_name, [e for e in plan.status_apply if mine(e)],
               [e for e in plan.status_clear if mine(e)], consumed)
    plan.status_apply = [e for e in plan.status_apply if not mine(e)] + d.apply
    plan.status_clear = [e for e in plan.status_clear if not mine(e)] + d.clear
    new_store = {**store, pc_name: d.clock}
    scored = {e.get("status") for e in ((getattr(plan, "result", None) or {}).get("status_scores") or [])
              if isinstance(e, dict) and e.get("target") == pc_name}
    deep = deep_statuses(merged)
    for s in sorted(ctx.active - scored - deep):          # v7.41(D4 가): 붙어 있는데 판단이 빠진 표면 상태 — 유지
        d.notes.append(f"{s} 판단 누락(유지)")
    notes = list(d.notes)
    # v7.48(D-3 나): NPC도 같은 규칙 — 이번 턴 추출 후보가 있거나 상태가 붙어 있는 인물
    touched = {e.get("target") for e in plan.status_apply + plan.status_clear} \
        | {n for n, st in (session.statuses or {}).items() if st}
    for npc in sorted(t for t in touched if t and t != pc_name):
        own = lambda e, _n=npc: e.get("target") == _n  # noqa: E731
        nctx = Ctx(now=now, start_minute=facts["start"], turn_minutes=max(0, now - before), daypart=facts["daypart"],
                   active=frozenset((session.statuses or {}).get(npc) or ()), clock=copy.deepcopy(store.get(npc) or {}),
                   seed=f"{getattr(session, 'narrative_seed', '') or ''}",
                   care=(facts.get("care_by") or {}).get(npc) or frozenset(), stage_in=_stage_in(plan, npc), npc=True)
        dn = decide(merged, nctx, npc, [e for e in plan.status_apply if own(e)],
                    [e for e in plan.status_clear if own(e)], ())
        plan.status_apply = [e for e in plan.status_apply if not own(e)] + dn.apply
        plan.status_clear = [e for e in plan.status_clear if not own(e)] + dn.clear
        new_store[npc] = dn.clock
        notes += [f"{npc}: {n}" for n in dn.notes]
    session.status_clock = new_store
    if notes:
        plan.diagnostics["status_rules"] = list(notes)
    return notes
