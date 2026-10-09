# 타입 조건식 — 3값 평가 · 정적 검증 · 의존 키 · 시간 wakeup (H04-B)
#
# [권위 — DESIGN 04 §16~28·§88~90 · H04 결정 게이트 A20·A29]
#   자유 문자열 조건·eval 없음. JSON AST만:
#     {"all": [P...]} · {"any": [P...]} · {"not": P} · {"const": true|false}
#     {"cmp": "EQ"|"NE"|"LT"|"LTE"|"GT"|"GTE", "ref": ValueRef, "value": literal}
#     {"cmp": ..., "ref": ValueRef, "value_ref": ValueRef}
#     {"exists": ValueRef} · {"is_unknown": ValueRef}
#     {"time": "ELAPSED_SINCE_PHASE_GTE", "minutes": int}
#     {"time": "DAYPART_IS", "daypart": DaypartId}
#     {"time": "AT_OR_AFTER_DATETIME", "date": "YYYY-MM-DD", "hhmm": "HH:MM"}
#   결과: TRUE / FALSE / UNKNOWN. ALL·ANY·NOT은 Kleene 3값 논리. UNKNOWN을 FALSE로 축약하지 않는다
#   (조건 '충족'은 TRUE일 때만 — 호출자가 판단). 모름 자체를 조건으로 쓰려면 is_unknown.
#
# [시간 — 매분 polling 금지]
#   시간 원자(time·CLOCK_MINUTE GTE/GT 리터럴)는 의존 키를 만들지 않는다. 대신 next_wakeup()이
#   '참값이 바뀔 수 있는 다음 분'을 계산하고 호출자가 EVENT_WAKEUP 예약을 만든다.
#   CLOCK_MINUTE의 다른 비교(EQ·LT 등)는 polling이 필요하므로 컴파일 ERROR.
#   H07: 행위자 위치 ref(ACTOR_PLACE 등)가 평상시 일과로 해석되는 행위자를 읽으면, 그 일과의 다음 경계도
#   wakeup 후보다(npc_position.next_routine_boundary). 일과 전환을 정본 배치 SET으로 만들지 않는다.
from __future__ import annotations

import re

from . import world_clock as _wc
from . import world_refs as _refs

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"
CMP_OPS = ("EQ", "NE", "LT", "LTE", "GT", "GTE")
TIME_KINDS = ("ELAPSED_SINCE_PHASE_GTE", "DAYPART_IS", "AT_OR_AFTER_DATETIME")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def t_all(vals):
    vals = list(vals)
    if FALSE in vals:
        return FALSE
    return UNKNOWN if UNKNOWN in vals else TRUE


def t_any(vals):
    vals = list(vals)
    if TRUE in vals:
        return TRUE
    return UNKNOWN if UNKNOWN in vals else FALSE


def t_not(v):
    return {TRUE: FALSE, FALSE: TRUE}.get(v, UNKNOWN)


def _calendar(definition):
    cal = definition.get("calendar")
    return _wc.Calendar(cal) if cal else None


def _absolute_minute(definition, p):
    """AT_OR_AFTER_DATETIME → 정본 분(달력 필요)."""
    cal = _calendar(definition)
    m = _DATE.match(p.get("date") or "")
    if cal is None or not m:
        return None
    try:
        hm = _wc.parse_hhmm(p.get("hhmm"))
    except _wc.ClockError:
        return None
    return cal.to_minute_index(int(m.group(1)), int(m.group(2)), int(m.group(3)), hm // 60, hm % 60)


def _cmp(op, a, b):
    if op == "EQ":
        return TRUE if a == b else FALSE
    if op == "NE":
        return TRUE if a != b else FALSE
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float))) or isinstance(a, bool) or isinstance(b, bool):
        return UNKNOWN                                  # 순서 비교가 성립하지 않는 값
    return {"LT": a < b, "LTE": a <= b, "GT": a > b, "GTE": a >= b}[op] and TRUE or FALSE


def evaluate(p, ctx: _refs.EvalContext) -> str:
    if "all" in p:
        return t_all(evaluate(x, ctx) for x in p["all"])
    if "any" in p:
        return t_any(evaluate(x, ctx) for x in p["any"])
    if "not" in p:
        return t_not(evaluate(p["not"], ctx))
    if "const" in p:
        return TRUE if p["const"] else FALSE
    if "cmp" in p:
        a = _refs.resolve(p["ref"], ctx)
        if "value_ref" in p:
            b = _refs.resolve(p["value_ref"], ctx)
        else:
            v = p.get("value")
            if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX):     # H07: 비교값의 슬롯 = binding 값
                v = _refs._subst(v, ctx)
                b = _refs.UNKNOWN if v is _refs.UNSET else _refs.known(v)
            else:
                b = _refs.known(v)
        if not (a.known and b.known):
            return UNKNOWN
        return _cmp(p["cmp"], a.value, b.value)
    if "exists" in p:
        r = _refs.resolve(p["exists"], ctx)
        return UNKNOWN if not r.known else (TRUE if r.value not in (None, "NONE") else FALSE)
    if "is_unknown" in p:
        return TRUE if not _refs.resolve(p["is_unknown"], ctx).known else FALSE
    if "time" in p:
        return _eval_time(p, ctx)
    return UNKNOWN


def _eval_time(p, ctx):
    now = ctx.now()
    if now is None:
        return UNKNOWN
    k = p["time"]
    if k == "ELAPSED_SINCE_PHASE_GTE":
        inst = ctx.instance(ctx.self_instance) if ctx.self_instance else None
        entered = (inst or {}).get("phase_entered_at")
        if not _is_int(entered):
            return UNKNOWN
        return TRUE if now - entered >= p["minutes"] else FALSE
    if k == "DAYPART_IS":
        cal = _calendar(ctx.definition)
        if cal is None:
            return UNKNOWN
        return TRUE if cal.daypart_of(now) == p["daypart"] else FALSE
    if k == "AT_OR_AFTER_DATETIME":
        m = _absolute_minute(ctx.definition, p)
        return UNKNOWN if m is None else (TRUE if now >= m else FALSE)
    return UNKNOWN


def holds(p, ctx) -> bool:
    """조건 충족 = TRUE일 때만(UNKNOWN은 충족이 아니다 — 명시 표현)."""
    return evaluate(p, ctx) == TRUE


# ══════════════════════════════════════════════════════════════
#  의존 키 · 시간 원자 · wakeup
# ══════════════════════════════════════════════════════════════

def _walk(p):
    yield p
    if not isinstance(p, dict):
        return
    for k in ("all", "any"):
        for x in p.get(k) or ():
            yield from _walk(x)
    if "not" in p:
        yield from _walk(p["not"])


def _refs_in(p):
    for node in _walk(p):
        if not isinstance(node, dict):
            continue
        for k in ("ref", "value_ref", "exists", "is_unknown"):
            if isinstance(node.get(k), dict):
                yield node[k]


def dependency_keys(p) -> list:
    out = set()
    for ref in _refs_in(p):
        out.update(_refs.dependency_keys(ref))
    return sorted(out)


def time_atoms(p) -> list:
    out = []
    for node in _walk(p):
        if isinstance(node, dict):
            if "time" in node:
                out.append(node)
            elif "cmp" in node and isinstance(node.get("ref"), dict) and node["ref"].get("ref") == "CLOCK_MINUTE":
                out.append(node)
    return out


def next_wakeup(predicates, ctx: _refs.EvalContext):
    """시간 원자의 참값이 바뀔 수 있는 다음 분(> now). 없으면 None. polling 없이 예약 1건으로 충분."""
    now = ctx.now()
    if now is None:
        return None
    cands = []
    for p in predicates:
        if not p:
            continue
        for a in time_atoms(p):
            m = _atom_next(a, ctx, now)
            if m is not None and m > now:
                cands.append(m)
        m = _routine_next(p, ctx, now)
        if m is not None:
            cands.append(m)
    return min(cands) if cands else None


POSITION_REFS = ("ACTOR_PLACE", "ACTOR_PLACEMENT_KIND", "ACTOR_POSITION_SOURCE", "ROUTE_KNOWN_EXISTS")


def _routine_next(p, ctx, now):
    """H07: 조건이 읽는 행위자 위치가 평상시 일과 파생이면 그 일과의 다음 경계(> now).

    정본 override가 있는 행위자는 경계가 없다(배치 변화는 의존 키 actor:X:placement가 깨운다).
    조건이 실제로 읽는 행위자만 — 모든 NPC를 보지 않는다(polling 없음).
    """
    from . import npc_position as _npos
    best = None
    for ref in _refs_in(p):
        if ref.get("ref") not in POSITION_REFS or "actor" not in ref:
            continue
        actor = _refs.param(ref, "actor", ctx)
        if actor is _refs.UNSET or not isinstance(actor, str):
            continue
        if _npos.effective_position(ctx.definition, ctx, actor)["source"] == _npos.CANONICAL:
            continue
        m = _npos.next_routine_boundary(ctx.definition, actor, now)
        if m is not None and m > now and (best is None or m < best):
            best = m
    return best


def _atom_next(a, ctx, now):
    if "cmp" in a:
        v = a.get("value")
        if not _is_int(v):
            return None
        return v if a["cmp"] == "GTE" else v + 1 if a["cmp"] == "GT" else None
    k = a["time"]
    if k == "ELAPSED_SINCE_PHASE_GTE":
        inst = ctx.instance(ctx.self_instance) if ctx.self_instance else None
        entered = (inst or {}).get("phase_entered_at")
        return entered + a["minutes"] if _is_int(entered) else None
    if k == "AT_OR_AFTER_DATETIME":
        return _absolute_minute(ctx.definition, a)
    if k == "DAYPART_IS":
        cal = _calendar(ctx.definition)
        if cal is None or a["daypart"] not in cal.dayparts:
            return None
        start, end, _ = cal.dayparts[a["daypart"]]
        base = now - now % _wc.MINUTES_PER_DAY
        out = []
        for t in (start, end):
            m = base + t
            while m <= now:
                m += _wc.MINUTES_PER_DAY
            out.append(m)
        return min(out)
    return None


# ══════════════════════════════════════════════════════════════
#  정적 검증(컴파일러)
# ══════════════════════════════════════════════════════════════

def validate(p, scope: _refs.RefScope, path: str, *, allow_phase_time: bool) -> list:
    """[(code, path, message)]. allow_phase_time=False면 ELAPSED_SINCE_PHASE 금지(spawn 등)."""
    errs = []
    if isinstance(p, str):
        return [("PREDICATE_FREEFORM", path, "자유 문자열 조건은 허용되지 않습니다(타입 AST만)")]
    if not isinstance(p, dict) or len([k for k in p if k in _NODE_KEYS]) != 1:
        return [("PREDICATE_INVALID", path, f"조건 노드는 {sorted(_NODE_KEYS)} 중 정확히 하나의 키를 가진 객체")]
    if "all" in p or "any" in p:
        k = "all" if "all" in p else "any"
        if set(p) != {k} or not isinstance(p[k], list) or not p[k]:
            return [("PREDICATE_INVALID", path, f"{k}는 비어 있지 않은 목록")]
        for i, x in enumerate(p[k]):
            errs += validate(x, scope, f"{path}.{k}[{i}]", allow_phase_time=allow_phase_time)
        return errs
    if "not" in p:
        if set(p) != {"not"}:
            return [("PREDICATE_INVALID", path, "not 노드에 다른 키")]
        return validate(p["not"], scope, f"{path}.not", allow_phase_time=allow_phase_time)
    if "const" in p:
        return [] if set(p) == {"const"} and isinstance(p["const"], bool) else \
            [("PREDICATE_INVALID", path, "const는 true/false")]
    if "cmp" in p:
        if p["cmp"] not in CMP_OPS or set(p) - {"cmp", "ref", "value", "value_ref"} \
                or ("value" in p) == ("value_ref" in p):
            return [("PREDICATE_INVALID", path, f"cmp는 {list(CMP_OPS)}, ref와 value|value_ref 중 하나")]
        errs += [("PREDICATE_REF_INVALID", ep, m) for ep, m in _refs.validate_ref(p.get("ref"), scope, f"{path}.ref")]
        if "value_ref" in p:
            errs += [("PREDICATE_REF_INVALID", ep, m)
                     for ep, m in _refs.validate_ref(p["value_ref"], scope, f"{path}.value_ref")]
        v = p.get("value")
        if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX) and v[len(_refs.SLOT_PREFIX):] not in scope.slots:
            # H07: 슬롯 문자열을 리터럴로 조용히 비교하지 않는다(정의되지 않은 슬롯은 오류)
            errs.append(("PREDICATE_SLOT_UNKNOWN", f"{path}.value", f"정의되지 않은 슬롯 {v!r}"))
        if isinstance(p.get("ref"), dict) and p["ref"].get("ref") == "CLOCK_MINUTE":
            if p["cmp"] not in ("GTE", "GT") or not _is_int(p.get("value")):
                errs.append(("PREDICATE_TIME_POLLING", path,
                             "CLOCK_MINUTE는 GTE/GT 정수 리터럴만(wakeup 컴파일) — 다른 비교는 매분 polling이 필요합니다"))
        elif "value_ref" in p and isinstance(p["value_ref"], dict) and p["value_ref"].get("ref") == "CLOCK_MINUTE":
            errs.append(("PREDICATE_TIME_POLLING", path, "CLOCK_MINUTE를 value_ref로 비교할 수 없습니다"))
        return errs
    if "exists" in p or "is_unknown" in p:
        k = "exists" if "exists" in p else "is_unknown"
        if set(p) != {k}:
            return [("PREDICATE_INVALID", path, f"{k} 노드에 다른 키")]
        return [("PREDICATE_REF_INVALID", ep, m) for ep, m in _refs.validate_ref(p[k], scope, f"{path}.{k}")]
    # time
    k = p.get("time")
    if k not in TIME_KINDS:
        return [("PREDICATE_INVALID", path, f"time은 {list(TIME_KINDS)} 중 하나")]
    if k == "ELAPSED_SINCE_PHASE_GTE":
        if set(p) != {"time", "minutes"} or not _is_int(p["minutes"]) or p["minutes"] < 0:
            return [("PREDICATE_INVALID", path, "ELAPSED_SINCE_PHASE_GTE는 minutes(0 이상 정수)")]
        if not allow_phase_time:
            return [("PREDICATE_ELAPSED_OUTSIDE_PHASE", path, "국면 경과 시간은 국면을 가진 사건 조건에서만")]
        return []
    if not scope.has_calendar:
        return [("PREDICATE_TIME_NO_CALENDAR", path, "날짜·시간대 조건에는 컴파일된 달력이 필요합니다")]
    if k == "DAYPART_IS":
        if set(p) != {"time", "daypart"} or not isinstance(p["daypart"], str):
            return [("PREDICATE_INVALID", path, "DAYPART_IS는 daypart")]
        if p["daypart"] not in scope.dayparts:
            return [("PREDICATE_DAYPART_UNKNOWN", path, f"정의되지 않은 시간대 {p['daypart']!r}")]
        return []
    if set(p) != {"time", "date", "hhmm"} or not _DATE.match(p.get("date") or "") \
            or not isinstance(p.get("hhmm"), str):
        return [("PREDICATE_INVALID", path, "AT_OR_AFTER_DATETIME은 date('YYYY-MM-DD')·hhmm('HH:MM')")]
    try:
        _wc.parse_hhmm(p["hhmm"])
        y, mo, d = (int(x) for x in _DATE.match(p["date"]).groups())
        if scope.calendar_obj is not None:
            scope.calendar_obj.to_ordinal(y, mo, d)
    except _wc.ClockError as e:
        return [("PREDICATE_INVALID", path, f"AT_OR_AFTER_DATETIME: {e}")]
    return []


_NODE_KEYS = {"all", "any", "not", "const", "cmp", "exists", "is_unknown", "time"}
