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
#     {"consumed": g}            이번 턴 그 묶음 물건 CONSUME
#     {"used": g}                이번 턴 그 묶음 물건 USE 또는 CONSUME
#     {"place_within": PlaceId}  장면 장소가 그곳이거나 그 아래
#     {"place_group": g}         장면 장소나 그 상위가 시나리오 `status_places[g]` 목록에 있음(v7.40 — 미선언 묶음은 모름)
#     {"daypart_in": [시간대…]}  현재 시간대
#     {"activity_in": [활동…], "min_minutes": n?}   이번 턴 활동(SLEEP·WAIT·TREAT…)과 그 턴의 경과 분
#     {"status_active": s}       그 상태가 지금 붙어 있음
#
#   시나리오 최상위 `intake_groups: {묶음: [물건 낱말…]}` — 물건 종류의 낱말 하나가 목록과 같으면 그 묶음.
#   `status_common_exclude: [이름…]` — 공용 상태 빼기. 상태 `excludes: [이름…]` — 상호 배타(붙은 쪽이 다른 쪽을 푼다).
#   상태 시계 `status_clock`(세션 필드, 되감기 대상): {PC 이름: {"applied": {상태: 분}, "intake": {묶음: 분}}}.
from __future__ import annotations

import copy
from dataclasses import dataclass, field

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
SCOPE_COMMON, SCOPE_SCENARIO = "공용", "시나리오"
RULE_KEYS = ("apply_gate", "apply_auto", "clear_auto", "clear_gate")
STATUS_TIMES = ("SINCE_INTAKE_GTE", "SINCE_APPLIED_GTE", "SINCE_STATUS_GTE")


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
        if k == "SINCE_INTAKE_GTE":
            last = (ctx.clock.get("intake") or {}).get(cond.get("group"), ctx.start_minute)
            return _b(ctx.now - int(last) >= n)
        name = status if k == "SINCE_APPLIED_GTE" else cond.get("status")
        if name not in ctx.active:
            return FALSE
        at = (ctx.clock.get("applied") or {}).get(name)
        return UNKNOWN if at is None else _b(ctx.now - int(at) >= n)
    if "consumed" in cond:
        return _b(cond["consumed"] in ctx.consumed)
    if "used" in cond:
        return _b(cond["used"] in ctx.used)
    if "place_within" in cond:
        return UNKNOWN if ctx.place is None else _b(cond["place_within"] in ctx.place_chain)
    if "place_group" in cond:
        members = ctx.place_groups.get(cond["place_group"])
        return UNKNOWN if ctx.place is None or members is None else _b(bool(members & set(ctx.place_chain)))
    if "daypart_in" in cond:
        return UNKNOWN if ctx.daypart is None else _b(ctx.daypart in (cond["daypart_in"] or ()))
    if "activity_in" in cond:
        ok = ctx.activity in (cond["activity_in"] or ())
        return _b(ok and ctx.turn_minutes >= int(cond.get("min_minutes") or 0))
    if "status_active" in cond:
        return _b(cond["status_active"] in ctx.active)
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
        if k == "SINCE_STATUS_GTE" and cond.get("status") not in statuses:
            errs.append(f"{path}: 알 수 없는 상태 {cond.get('status')!r}")
        return errs
    for key in ("consumed", "used"):
        if key in cond:
            ok = groups is None or cond[key] in groups
            return [] if ok else [f"{path}: 정의되지 않은 섭취 묶음 {cond[key]!r}(intake_groups)"]
    if "status_active" in cond:
        return [] if cond["status_active"] in statuses else [f"{path}: 알 수 없는 상태 {cond['status_active']!r}"]
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

@dataclass
class Decision:
    apply: list                 # 추출 DTO 형식 [{target,status,score}] (코드 판정은 score=None)
    clear: list
    clock: dict                 # 이 PC의 새 status_clock 항목
    notes: list                 # 마스터 보고용 한 줄들


def decide(merged: dict, ctx: Ctx, pc_name: str, apply_in, clear_in, groups_consumed=()) -> Decision:
    """PC 대상 추출 후보 + 코드 규칙 → 최종 부여·해제. NPC 대상 후보는 손대지 않는다(호출부가 그대로 둔다)."""
    notes = []
    clock = copy.deepcopy(ctx.clock or {})
    applied = dict(clock.get("applied") or {})
    for s in ctx.active:                      # 시계 없이 붙어 있던 상태(이전 버전) — 지금부터 잰다
        applied.setdefault(s, ctx.now)
    clock["applied"] = applied
    ctx = Ctx(**{**ctx.__dict__, "clock": clock})

    def rules(s):
        r = (merged.get(s) or {}).get("rules")
        return r if isinstance(r, dict) else None

    want_apply, want_clear = {}, set()
    for e in apply_in:
        r = rules(e["status"])
        if r and "apply_gate" in r and evaluate(r["apply_gate"], ctx, e["status"]) != TRUE:
            notes.append(f"{e['status']} 부여 보류 — 근거 조건 미충족(코드)")
            continue
        want_apply[e["status"]] = e
    for e in clear_in:
        r = rules(e["status"])
        if r is not None:
            gate = r.get("clear_gate")
            if gate is None or evaluate(gate, ctx, e["status"]) != TRUE:
                notes.append(f"{e['status']} 해제 보류 — 해소 근거 미충족(코드)")
                continue
        want_clear.add(e["status"])
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
    for s in want_apply:
        if s not in ctx.active:
            applied[s] = ctx.now
    for s in want_clear:
        applied.pop(s, None)
    intake = dict(clock.get("intake") or {})
    for g in groups_consumed:
        intake[g] = ctx.now
    clock["intake"] = intake
    apply_out = [e for s, e in want_apply.items() if s not in ctx.active]
    clear_out = [{"target": pc_name, "status": s, "score": None} for s in sorted(want_clear) if s in ctx.active]
    return Decision(apply_out, clear_out, clock, notes)


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


def resolve_turn(session, prep) -> list:
    """DYNAMIC 턴의 PC 상태 후보를 규칙으로 확정하고 status_clock을 갱신한다. 반환: 보고 줄(빈 목록 = 변화 없음).

    plan.status_apply/status_clear의 PC 항목만 바꾼다(NPC 항목은 그대로). LEGACY·정본 시계 없음이면 아무것도 안 한다."""
    from . import turn_world as _tw
    from .utils import get_merged_status_effects
    plan = getattr(prep, "extraction_plan", None)
    facts = _tw.status_turn_facts(session, prep)      # H05 경계: 세계 읽기는 turn_world가 한다
    if plan is None or facts is None:
        return []
    sd = getattr(session, "scenario_data", None) or {}
    merged = get_merged_status_effects(sd)
    if not any(isinstance(e.get("rules"), dict) for e in merged.values()):
        return []
    pc_name = next((p.get("name") for p in (session.players or {}).values() if p.get("name")), None)
    if not pc_name:
        return []
    now, before, place = facts["now"], facts["before"], facts["place"]
    groups = groups_of(sd)
    consumed, used = _turn_groups(getattr(plan, "item_plan", None), groups)
    store = getattr(session, "status_clock", None) or {}
    ctx = Ctx(now=now, start_minute=facts["start"], turn_minutes=max(0, now - before),
              place=place, place_chain=_place_chain(sd, place), place_groups=place_groups_of(sd), daypart=facts["daypart"], activity=facts["activity"],
              consumed=consumed, used=used, active=frozenset((session.statuses or {}).get(pc_name) or ()),
              clock=copy.deepcopy(store.get(pc_name) or {}))
    mine = lambda e: e.get("target") == pc_name  # noqa: E731
    d = decide(merged, ctx, pc_name, [e for e in plan.status_apply if mine(e)],
               [e for e in plan.status_clear if mine(e)], consumed)
    plan.status_apply = [e for e in plan.status_apply if not mine(e)] + d.apply
    plan.status_clear = [e for e in plan.status_clear if not mine(e)] + d.clear
    session.status_clock = {**store, pc_name: d.clock}
    if d.notes:
        plan.diagnostics["status_rules"] = list(d.notes)
    return d.notes
