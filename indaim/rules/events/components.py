"""사건 — 인스턴스 핵심(event.core)·저작 변수와 결단(event.vars)·전이 기록(event.history).

  event.core     {instance_id, template_id, template_version, bound_slots, lifecycle, current_phase,
                  phase_entered_at, transition_ordinal, occurrence_ordinal}
  event.vars     저작 변수 + "resolution"(플레이어 명시 결단만, 번복 없음)
  event.history  [전이 기록] (보존 정책 K4는 사건 진행 이식 때)
옛 사건 지역 값의 엔진 장부 키(인물 상태·파티·성장·물건 지급)는 vars가 아니다 — 각 주인 모듈의 효과가 된다.
"""
from indaim.kernel import Component
from indaim.kernel.components import APPEND_DEDUPE, SINGLE_VALUE, write

from .._common import keys, reject

ACTIVE, RESOLVED, CANCELLED = "ACTIVE", "RESOLVED", "CANCELLED"
LIFECYCLES = (ACTIVE, RESOLVED, CANCELLED)
CORE_KEYS = ("instance_id", "template_id", "template_version", "bound_slots", "lifecycle",
             "current_phase", "phase_entered_at", "transition_ordinal", "occurrence_ordinal")
RESOLUTION_KEY = "resolution"


def _iid(e):
    return e.address[1]


def _core(view, e):
    if not view.stored_has("event", _iid(e), "core"):
        reject(e, f"없는 사건 {_iid(e)}")
    return view.read("event", _iid(e), "core")


def validate_instance(inst) -> list:
    if not isinstance(inst, dict) or set(inst) != set(CORE_KEYS) | {"local_state", "history"}:
        return [f"사건 인스턴스 키는 정확히 {list(CORE_KEYS) + ['local_state', 'history']}"]
    errs = []
    if inst["lifecycle"] not in LIFECYCLES:
        errs.append(f"lifecycle은 {list(LIFECYCLES)}")
    for k in ("template_version", "transition_ordinal", "occurrence_ordinal"):
        if not isinstance(inst[k], int) or isinstance(inst[k], bool) or inst[k] < 0:
            errs.append(f"{k}는 0 이상의 정수")
    for k in ("bound_slots", "local_state"):
        if not isinstance(inst[k], dict):
            errs.append(f"{k}는 객체")
    if not isinstance(inst["history"], list):
        errs.append("history는 목록")
    return errs


def _checked_instance(view, e):
    inst = keys(e, ("instance",))["instance"]
    errs = validate_instance(inst)
    if errs:
        reject(e, "; ".join(errs))
    if inst["lifecycle"] != ACTIVE:
        reject(e, "생성은 ACTIVE로만(DORMANT 인스턴스 없음)")
    if inst["instance_id"] != _iid(e):
        reject(e, "대상 id와 instance_id가 다릅니다")
    if view.stored_has("event", _iid(e), "core"):
        reject(e, f"이미 있는 사건 {_iid(e)}")
    return inst


def _create(view, e):
    inst = _checked_instance(view, e)
    return [write(e, (), SINGLE_VALUE, {k: inst[k] for k in CORE_KEYS})]


def _vars_init(view, e):
    """생성의 짝(같은 계층) — 생성 시 지역 값(엔진 장부 키 제외는 생산자 몫)."""
    inst = _checked_instance(view, e)
    return [write(e, (), SINGLE_VALUE, inst["local_state"])] if inst["local_state"] else []


def _history_init(view, e):
    inst = _checked_instance(view, e)
    return [write(e, (), SINGLE_VALUE, inst["history"])] if inst["history"] else []


def _transition(view, e):
    a = keys(e, ("from_phase", "to_phase", "at_minute", "expected_ordinal"))
    cur = view.read("event", _iid(e), "core") if view.stored_has("event", _iid(e), "core") else None
    if cur is None or cur["lifecycle"] != ACTIVE:
        reject(e, f"ACTIVE 사건이 아닙니다: {_iid(e)}")
    if cur["current_phase"] != a["from_phase"] or cur["transition_ordinal"] != a["expected_ordinal"]:
        reject(e, f"{_iid(e)}: 전이 기준이 현재 상태와 다릅니다(낡은 전이)")
    if a["to_phase"] == a["from_phase"]:
        reject(e, "같은 국면으로의 전이")
    return [write(e, ("current_phase",), SINGLE_VALUE, a["to_phase"]),
            write(e, ("phase_entered_at",), SINGLE_VALUE, a["at_minute"]),
            write(e, ("transition_ordinal",), SINGLE_VALUE, a["expected_ordinal"] + 1)]


def _lifecycle(view, e):
    lc = keys(e, ("lifecycle",))["lifecycle"]
    cur = _core(view, e)
    if lc not in (RESOLVED, CANCELLED) or cur["lifecycle"] != ACTIVE:
        reject(e, f"{_iid(e)}: {cur['lifecycle']} → {lc} 전이 불가(ACTIVE → RESOLVED|CANCELLED만)")
    return [write(e, ("lifecycle",), SINGLE_VALUE, lc)]


def _set(view, e):
    a = keys(e, ("key", "value"))
    _core(view, e)
    if not isinstance(a["key"], str) or not a["key"]:
        reject(e, "key는 비어 있지 않은 문자열")
    if a["key"] == RESOLUTION_KEY:
        reject(e, "'resolution'은 플레이어 명시 결단(resolve)만 씁니다")
    return [write(e, (a["key"],), SINGLE_VALUE, a["value"])]


def _resolve(view, e):
    """플레이어 명시 결단 — 진행 중인 사건에 한 번만."""
    a = keys(e, ("key", "value"))
    cur = _core(view, e)
    if a["key"] != RESOLUTION_KEY:
        reject(e, "결단 효과는 'resolution'만 씁니다")
    if cur["lifecycle"] != ACTIVE:
        reject(e, f"진행 중이 아닌 사건 {_iid(e)}의 결단")
    if view.has("event", _iid(e), "vars", (RESOLUTION_KEY,)):
        reject(e, f"사건 {_iid(e)}의 결단은 이미 내려졌습니다(번복 없음)")
    return [write(e, (RESOLUTION_KEY,), SINGLE_VALUE, a["value"])]


def _append(view, e):
    a = keys(e, ("entry",))
    if not isinstance(a["entry"], dict):
        reject(e, "entry는 객체")
    _core(view, e)
    return [write(e, (), APPEND_DEDUPE, a["entry"])]


COMPONENTS = (
    Component("core", "event", "rules.events", {"create": _create, "transition": _transition, "lifecycle": _lifecycle}),
    Component("vars", "event", "rules.events", {"init": _vars_init, "set": _set, "resolve": _resolve}),
    Component("history", "event", "rules.events", {"init": _history_init, "append": _append}, default=[]),
)
