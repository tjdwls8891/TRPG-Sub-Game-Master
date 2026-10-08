# 타입 값 참조(ValueRef) 레지스트리 — 해석 · 검증 · 의존 키 (H04)
#
# [권위 — DESIGN 04 §21~24 · H04 결정 게이트 A20·A29]
#   사건 조건·정보 계열이 세계를 읽는 유일한 통로다. 자유 문자열·dotted path·eval 없음.
#   ValueRef = {"ref": <TYPE>, ...typed params}
#   해석 결과 = Resolved(known, value). known=False 는 UNKNOWN(추적되지 않는 사실)이며
#   조용히 False/None으로 축약하지 않는다. 반대로 정본이 '없음'을 의미하는 경우
#   (인지 기록 없음 = 미인지, 사건 없음)는 known=True 와 명시 값으로 돌려준다.
#
#   파라미터의 슬롯 참조: "$slot:<name>"(사건 binding) · "$self"(현재 사건 인스턴스).
#   holder: "actor:<ActorId>" · "group:<GroupId>" · "$slot:<name>"(ACTOR 슬롯 → actor:<id>)
#           · "$session_pc"(H06 — COGNITION_AWARENESS 전용). 정본 actor_registry의 PC가 정확히 하나일 때만
#             그 ActorId로 해석하고, 아니면 UNKNOWN(이름 검색 대체 없음). PC 믿음(COGNITION_BELIEF)은 사건
#             조건이 될 수 없다(사용자 결정 2026-10-08 — 컴파일 오류).
#
# [의존 키]
#   정규 문자열. "actor:<id>:placement" · "access:<cid>:state" · "info:<id>:validity" ·
#   "cognition:<holder>:<info>" · "event:<iid>:lifecycle|phase|local:<k>" ·
#   "knowledge:<actor>:spatial" · "series:<sid>" · "scene:place".
#   슬롯이 남은 키는 템플릿 패턴("$slot:x")이며 binding으로 구체화한다. "*" 세그먼트는 와일드카드.
#   시간(CLOCK_MINUTE)은 의존 키가 아니다 — 매분 polling 대신 wakeup으로 컴파일한다(predicates).
from __future__ import annotations

from dataclasses import dataclass, field

from . import world_mutation as _wm

UNSET = object()

REF_TYPES = (
    "CLOCK_MINUTE", "SCENE_PLACE", "OBSERVER_ACTIVITY",
    "ACTOR_PLACE", "ACTOR_PLACEMENT_KIND", "ACCESS_STATE", "INFO_VALIDITY",
    "COGNITION_AWARENESS", "COGNITION_BELIEF",
    "EVENT_LIFECYCLE", "EVENT_PHASE", "EVENT_LOCAL", "ROUTE_KNOWN_EXISTS",
)
_PARAMS = {
    "CLOCK_MINUTE": ((), ()),
    "SCENE_PLACE": ((), ()),
    "OBSERVER_ACTIVITY": ((), ()),
    "ACTOR_PLACE": (("actor",), ()),
    "ACTOR_PLACEMENT_KIND": (("actor",), ()),
    "ACCESS_STATE": (("control",), ()),
    "INFO_VALIDITY": (("info",), ()),
    "COGNITION_AWARENESS": (("holder", "info"), ()),
    "COGNITION_BELIEF": (("holder", "info"), ()),
    "EVENT_LIFECYCLE": ((), ("instance", "template")),
    "EVENT_PHASE": (("instance",), ()),
    "EVENT_LOCAL": (("instance", "key"), ()),
    "ROUTE_KNOWN_EXISTS": (("actor", "to", "mode"), ("from",)),
}
SLOT_PREFIX = "$slot:"
SELF = "$self"
SESSION_PC = "$session_pc"


def session_pc_actor(world):
    """정본 actor_registry에서 단일 세션 PC ActorId. PC가 0명·여러 명이면 None(모호성 해석 금지)."""
    reg = (world.domain("actor_registry") if isinstance(world, _wm.ProjectedWorld)
           else getattr(world, "actor_registry", None)) or {}
    pcs = sorted(a for a, e in reg.items() if isinstance(e, dict) and e.get("kind") == "PC")
    return pcs[0] if len(pcs) == 1 else None


@dataclass(frozen=True)
class Resolved:
    known: bool
    value: object = None


UNKNOWN = Resolved(False, None)


def known(value) -> Resolved:
    return Resolved(True, value)


@dataclass
class EvalContext:
    """해석 문맥(순수). world 는 ProjectedWorld 또는 세션. 세션을 바꾸지 않는다.

    instance_overrides: {iid: {field: value}} — 아직 커밋되지 않은 전이(대상 국면 적용 투영)를
    평가할 때 쓰는 가상 덮어쓰기(case valid_when 등). 정본 쓰기가 아니다.
    """
    definition: dict
    world: object
    bindings: dict = field(default_factory=dict)
    self_instance: str | None = None
    progress: dict = field(default_factory=dict)
    instance_overrides: dict = field(default_factory=dict)

    def domain(self, name):
        if isinstance(self.world, _wm.ProjectedWorld):
            return self.world.domain(name)
        return getattr(self.world, name, None)

    def now(self):
        clock = self.domain("world_clock") or {}
        return clock.get("minute_index") if isinstance(clock.get("minute_index"), int) else None

    def instance(self, iid):
        inst = (self.domain("event_instances") or {}).get(iid)
        ov = self.instance_overrides.get(iid)
        if ov is not None:
            inst = dict(inst or {}, **ov)
        return inst


# ══════════════════════════════════════════════════════════════
#  파라미터 해석
# ══════════════════════════════════════════════════════════════

def _subst(v, ctx: EvalContext):
    """슬롯·$self 치환. 해석 불가면 UNSET."""
    if isinstance(v, str):
        if v == SELF:
            return ctx.self_instance if ctx.self_instance else UNSET
        if v.startswith(SLOT_PREFIX):
            b = ctx.bindings.get(v[len(SLOT_PREFIX):])
            return b if b is not None else UNSET
    return v


def _holder(v, ctx):
    if v == SESSION_PC:
        pc = session_pc_actor(ctx.world)
        return f"actor:{pc}" if pc else UNSET
    if isinstance(v, str) and v.startswith(SLOT_PREFIX):
        b = _subst(v, ctx)
        return UNSET if b is UNSET else f"actor:{b}"
    return v


def param(ref: dict, name: str, ctx: EvalContext):
    raw = ref.get(name)
    return _holder(raw, ctx) if name == "holder" else _subst(raw, ctx)


# ══════════════════════════════════════════════════════════════
#  해석
# ══════════════════════════════════════════════════════════════

def resolve(ref: dict, ctx: EvalContext) -> Resolved:
    t = ref.get("ref")
    fn = _RESOLVERS.get(t)
    if fn is None:
        return UNKNOWN
    params = {}
    req, opt = _PARAMS[t]
    for k in req + opt:
        if k in ref:
            v = param(ref, k, ctx)
            if v is UNSET:
                return UNKNOWN                      # 묶이지 않은 슬롯 = 모름
            params[k] = v
    return fn(ctx, **params)


def _r_clock(ctx):
    now = ctx.now()
    return known(now) if now is not None else UNKNOWN


def _r_scene(ctx):
    if "scene_place" in ctx.progress:
        return known(ctx.progress["scene_place"])
    st = ctx.domain("spatial_state") or {}
    return known(st["scene_place"]) if "scene_place" in st else UNKNOWN


def _r_activity(ctx):
    return known(ctx.progress["activity"]) if "activity" in ctx.progress else UNKNOWN


def _placement(ctx, actor):
    return ((ctx.domain("spatial_state") or {}).get("placements") or {}).get(actor)


def _r_actor_place(ctx, actor):
    pl = _placement(ctx, actor)
    if pl is None:
        return UNKNOWN                              # 위치를 정본으로 추적하지 않음 ≠ 없음
    return known(pl.get("place_id") if pl.get("kind") == "AT_PLACE" else None)


def _r_actor_kind(ctx, actor):
    pl = _placement(ctx, actor)
    return known(pl.get("kind")) if pl is not None else UNKNOWN


def _r_access(ctx, control):
    from . import access as _acc
    try:
        return known(_acc.current_state(ctx.definition, ctx.domain("access_control_state") or {}, control))
    except _acc.AccessError:
        return UNKNOWN


def _r_info_validity(ctx, info):
    from . import information as _info
    v = _info.effective_validity(ctx.definition, ctx.domain("information_state") or {}, info)
    return known(v) if v is not None else UNKNOWN


def _cog_record(ctx, holder, info):
    return ((ctx.domain("cognition_state") or {}).get(holder) or {}).get(info)


def _r_awareness(ctx, holder, info):
    rec = _cog_record(ctx, holder, info)
    return known(rec.get("awareness") if rec else "NONE")    # 기록 없음 = 미인지(정본 의미)


def _r_belief(ctx, holder, info):
    rec = _cog_record(ctx, holder, info)
    return known(rec.get("belief") if rec else None)


def _latest_instance_of(ctx, template):
    best = None
    for iid in sorted(ctx.domain("event_instances") or {}):
        inst = ctx.instance(iid)
        if inst and inst.get("template_id") == template:
            key = (inst.get("occurrence_ordinal") or 0, iid)
            if best is None or key > best[0]:
                best = (key, inst)
    return best[1] if best else None


def _r_lifecycle(ctx, instance=None, template=None):
    if instance is not None:
        inst = ctx.instance(instance)
    elif template is not None:
        inst = _latest_instance_of(ctx, template)
    else:
        return UNKNOWN
    return known(inst.get("lifecycle") if inst else "NONE")


def _r_phase(ctx, instance):
    inst = ctx.instance(instance)
    return known(inst.get("current_phase") if inst else None)


def _r_local(ctx, instance, key):
    inst = ctx.instance(instance)
    if not inst or key not in (inst.get("local_state") or {}):
        return UNKNOWN                              # 사건이 아직 기록하지 않은 국소 상태
    return known(inst["local_state"][key])


def _r_route(ctx, actor, to, mode, **kw):
    from . import movement as _mv
    from . import spatial as _sp
    origin = kw.get("from")
    if origin is None:
        pl = _placement(ctx, actor)
        if not pl or pl.get("kind") != "AT_PLACE":
            return UNKNOWN
        origin = pl["place_id"]
    try:
        plan = _mv.plan_known_routes(ctx.definition, _sp.actor_knowledge(ctx.world, actor), origin, to, mode)
    except _mv.MovementError:
        return UNKNOWN
    return known(plan.status in (_mv.ROUTE_OK, _mv.SAME_PLACE))


_RESOLVERS = {
    "CLOCK_MINUTE": _r_clock, "SCENE_PLACE": _r_scene, "OBSERVER_ACTIVITY": _r_activity,
    "ACTOR_PLACE": _r_actor_place, "ACTOR_PLACEMENT_KIND": _r_actor_kind,
    "ACCESS_STATE": _r_access, "INFO_VALIDITY": _r_info_validity,
    "COGNITION_AWARENESS": _r_awareness, "COGNITION_BELIEF": _r_belief,
    "EVENT_LIFECYCLE": _r_lifecycle, "EVENT_PHASE": _r_phase, "EVENT_LOCAL": _r_local,
    "ROUTE_KNOWN_EXISTS": _r_route,
}


# ══════════════════════════════════════════════════════════════
#  의존 키
# ══════════════════════════════════════════════════════════════

def _key_param(ref, name):
    v = ref.get(name)
    if v == SELF:
        return SELF
    if name == "holder" and isinstance(v, str) and v.startswith(SLOT_PREFIX):
        return "actor:" + v
    if name == "holder" and v == SESSION_PC:
        return "actor:" + SESSION_PC                 # 패턴 — 의존 색인이 정본 단일 PC로 구체화
    return v


def dependency_keys(ref: dict) -> list:
    """ValueRef가 읽는 정본 키(슬롯·$self는 패턴으로 남는다). 시간은 키가 아니다."""
    t = ref.get("ref")
    a = lambda n: _key_param(ref, n)
    if t == "ACTOR_PLACE" or t == "ACTOR_PLACEMENT_KIND":
        return [f"actor:{a('actor')}:placement"]
    if t == "SCENE_PLACE":
        return ["scene:place"]
    if t == "ACCESS_STATE":
        return [f"access:{a('control')}:state"]
    if t == "INFO_VALIDITY":
        return [f"info:{a('info')}:validity"]
    if t in ("COGNITION_AWARENESS", "COGNITION_BELIEF"):
        return [f"cognition:{a('holder')}:{a('info')}"]
    if t == "EVENT_LIFECYCLE":
        return [f"event:{a('instance')}:lifecycle"] if "instance" in ref else ["event:*:lifecycle"]
    if t == "EVENT_PHASE":
        return [f"event:{a('instance')}:phase"]
    if t == "EVENT_LOCAL":
        return [f"event:{a('instance')}:local:{ref.get('key')}"]
    if t == "ROUTE_KNOWN_EXISTS":
        return [f"actor:{a('actor')}:placement", f"knowledge:{a('actor')}:spatial"]
    return []                                       # CLOCK_MINUTE·OBSERVER_ACTIVITY


def materialize_key(key: str, bindings: dict, self_instance: str | None = None, *, session_pc=None):
    """패턴 키의 슬롯·$self·$session_pc를 구체 값으로. 묶이지 않으면 None."""
    out = key
    if SESSION_PC in out:
        if not session_pc:
            return None
        out = out.replace(SESSION_PC, session_pc)
    if SELF in out:
        if not self_instance:
            return None
        out = out.replace(SELF, self_instance)
    while SLOT_PREFIX in out:
        i = out.index(SLOT_PREFIX)
        j = i + len(SLOT_PREFIX)
        k = j
        while k < len(out) and out[k] != ":":
            k += 1
        name = out[j:k]
        if bindings.get(name) is None:
            return None
        out = out[:i] + str(bindings[name]) + out[k:]
    return out


def key_matches(pattern: str, key: str) -> bool:
    """와일드카드('*' 세그먼트) 일치. 세그먼트 수가 같아야 한다."""
    ps, ks = pattern.split(":"), key.split(":")
    return len(ps) == len(ks) and all(p == "*" or p == k for p, k in zip(ps, ks))


def is_wildcard(key: str) -> bool:
    return "*" in key.split(":")


# ══════════════════════════════════════════════════════════════
#  검증(컴파일러용)
# ══════════════════════════════════════════════════════════════

@dataclass
class RefScope:
    """컴파일 시 검증 범위. slots: {name: type}."""
    actors: set
    places: set
    infos: set
    controls: set
    modes: set
    slots: dict = field(default_factory=dict)
    allow_self: bool = False
    templates: set = field(default_factory=set)
    has_calendar: bool = False
    dayparts: set = field(default_factory=set)
    calendar_obj: object = None


def _check_id(kind, v, pool, scope, slot_type, errs, path):
    if isinstance(v, str) and v.startswith(SLOT_PREFIX):
        name = v[len(SLOT_PREFIX):]
        if name not in scope.slots:
            errs.append((path, f"정의되지 않은 슬롯 {name!r}"))
        elif slot_type and scope.slots[name] != slot_type:
            errs.append((path, f"슬롯 {name!r}의 타입({scope.slots[name]})이 {slot_type}이 아닙니다"))
        return
    if not isinstance(v, str) or not v:
        errs.append((path, f"{kind}는 비어 있지 않은 문자열이어야 합니다"))
    elif pool is not None and v not in pool:
        errs.append((path, f"정의되지 않은 {kind} {v!r}"))


def validate_ref(ref, scope: RefScope, path: str) -> list:
    """[(path, message)] — 빈 목록이면 유효."""
    errs = []
    if not isinstance(ref, dict) or ref.get("ref") not in REF_TYPES:
        return [(path, f"ValueRef는 {{'ref': {list(REF_TYPES)} 중 하나, ...}} 객체여야 합니다")]
    t = ref["ref"]
    req, opt = _PARAMS[t]
    extra = set(ref) - {"ref"} - set(req) - set(opt)
    if extra:
        errs.append((path, f"{t}: 알 수 없는 파라미터 {sorted(extra)}"))
    for k in req:
        if k not in ref:
            errs.append((path, f"{t}: 파라미터 {k!r} 누락"))
    if t == "EVENT_LIFECYCLE" and ("instance" in ref) == ("template" in ref):
        errs.append((path, "EVENT_LIFECYCLE은 instance 또는 template 중 하나만"))
    for k in req + opt:
        if k not in ref:
            continue
        v, p = ref[k], f"{path}.{k}"
        if k in ("actor",):
            _check_id("ActorId", v, scope.actors, scope, "ACTOR", errs, p)
        elif k in ("to", "from"):
            _check_id("PlaceId", v, scope.places, scope, "PLACE", errs, p)
        elif k == "info":
            _check_id("InfoId", v, scope.infos, scope, "INFO", errs, p)
        elif k == "control":
            _check_id("control_id", v, scope.controls, scope, None, errs, p)
        elif k == "mode":
            _check_id("mode_id", v, scope.modes, scope, None, errs, p)
        elif k == "holder":
            if v == SESSION_PC:
                if t != "COGNITION_AWARENESS":
                    errs.append((p, "$session_pc는 COGNITION_AWARENESS에만 씁니다 — PC 믿음은 사건 조건이 될 수 없습니다"))
            elif isinstance(v, str) and v.startswith(SLOT_PREFIX):
                _check_id("holder", v, None, scope, "ACTOR", errs, p)
            elif not (isinstance(v, str) and (v.startswith("actor:") or v.startswith("group:"))
                      and len(v.split(":", 1)[1]) > 0):
                errs.append((p, "holder는 'actor:<ActorId>'·'group:<GroupId>'·'$slot:<name>'이어야 합니다"))
            elif v.startswith("actor:") and v[6:] not in scope.actors:
                errs.append((p, f"정의되지 않은 ActorId {v[6:]!r}"))
        elif k == "instance":
            if v == SELF:
                if not scope.allow_self:
                    errs.append((p, "$self는 사건 템플릿 안에서만 씁니다"))
            elif not isinstance(v, str) or not v:
                errs.append((p, "instance는 문자열이어야 합니다"))
        elif k == "template":
            if v not in scope.templates:
                errs.append((p, f"정의되지 않은 사건 템플릿 {v!r}"))
        elif k == "key":
            if not isinstance(v, str) or not v:
                errs.append((p, "key는 비어 있지 않은 문자열이어야 합니다"))
    return errs


def display_value(value, definition: dict, world=None) -> str:
    """정보 계열 명제 렌더용 결정적 표시(provider 없음)."""
    if value is None:
        return "없음"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, str):
        reg = {}
        if world is not None:
            reg = (world.domain("actor_registry") if isinstance(world, _wm.ProjectedWorld)
                   else getattr(world, "actor_registry", {})) or {}
        if value in reg and isinstance(reg[value], dict):
            return reg[value].get("display_name") or value
        a = (definition.get("actor_definitions") or {}).get(value)
        if a:
            return a.get("display_name") or value
        return value
    return str(value)
