# 플레이어 세계 계획 — 세션 세계 모드 · 경로 선택 정책 · 이동/대기/태도 계획 (H05-A)
#
# [권위 — DESIGN 10·09 §12~22·11 §20~22·33 · H05 handoff §3]
#   · 순수 계획이다. 세션(정본)을 바꾸지 않고, H03 KnownRoutePlanner/ActualTraversalPlanner와
#     H04 project_world_progression만 호출한다. 결과 ops는 같은 PLAYER_TURN 묶음에 담겨 기존
#     CommitCoordinator 경로로만 커밋된다. 묘사·READY가 실패하면 계획은 그대로 버려진다.
#   · 세계 모드: 정의가 세션에 바인딩(definition_revision)돼 있고 정량 시간·구조화 장소가 검증됐으며
#     시계가 초기화되고 PC 행위자가 배치된 세션만 DYNAMIC이다. 그 밖(현 운영 세션 전부 — 바인딩·seed는
#     H06)은 LEGACY이며 자동 턴은 기존 경로를 그대로 쓴다.
#   · 이동: 플레이어가 아는 경로만 후보. 의미 있는 차이가 있으면 ASK(RouteChoiceContext), 자동 선택은
#     자동 선택이 허용된 후보 사이에서만 (지각 비용, 구간 서명) 사전순. 실제 진행은 구간마다 실제 접근을
#     그 시점 투영 세계로 확인하고, 구간 사이에서 세계 진행(H04)을 돌려 관찰 가능한 신호에서 멈춘다.
#     숨은 봉쇄는 그 구간 시작에 도달해 처음 관찰된다(우회 없음). 목적지 시간을 미리 커밋하지 않는다.
#   · 대기/수면: 목표 분까지 세계 진행 — ActivityContext로 관찰 가능 신호만 중단.
#   · PC 태도: 플레이어 명시 발화의 stance만 PLAYER_EXPLICIT 권위 COGNITION_BELIEF_SET 후보가 된다.
from __future__ import annotations

from dataclasses import dataclass, field

from . import access as _acc
from . import action_intent as _ai
from . import cognition as _cog
from . import movement as _mv
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_progression as _wp
from .world_ids import stable_digest

MODE_LEGACY, MODE_DYNAMIC = "LEGACY", "DYNAMIC"
REQUIRED_CAPABILITIES = ("structured_places", "quantitative_time")

PLAN_NONE = "NONE"                 # 코드 소유 세계 행동 없음(0분 턴)
PLAN_PLANNED = "PLANNED"
PLAN_NEEDS_CHOICE = "NEEDS_CHOICE"
PLAN_REJECTED = "REJECTED"         # 목적지 모름·경로 모름 등 — 세계 변화 없이 묘사가 안내
PLAN_TIE = "TIE"                   # 장면 관련 같은 분 서사 선후 미정(묘사 관찰이 정함)
PLAN_CONFLICT = "CONFLICT"         # WORLD_PROJECTION_CONFLICT(결정적 실패 — 재시도 없음)

OUT_ARRIVED = "ARRIVED"
OUT_INTERRUPTED_SIGNAL = "INTERRUPTED_SIGNAL"
OUT_INTERRUPTED_ACCESS = "INTERRUPTED_ACCESS"
OUT_INTERRUPTED_BLOCKED = "INTERRUPTED_BLOCKED"
OUT_TIME_DONE = "TIME_COMPLETED"
OUT_TIME_INTERRUPTED = "TIME_INTERRUPTED"

# ActivityContext → 진행 관찰 문맥(OBSERVER_ACTIVITY ref가 읽는 값). 장르 규칙을 하드코딩하지 않는다 —
#   어떤 신호가 어떤 활동 중 관찰 가능한지는 저작된 observable_when이 정한다.
ACTIVITY_STATE = {"SLEEP": "SLEEPING", "WAIT": "WAITING", "SEARCH": "SEARCHING", "WORK": "WORKING",
                  "TREAT": "TREATING", "READ": "READING", "OTHER": "OTHER"}


# ══════════════════════════════════════════════════════════════
#  세계 모드 · 정의
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WorldContext:
    mode: str
    reason: str
    definition: dict | None = None
    definition_revision: str | None = None
    pcs: tuple = ()                    # 이동·관찰 주체 PC ActorId(정렬)
    lead_pc: str | None = None
    uid_of: dict = field(default_factory=dict)

    @property
    def dynamic(self) -> bool:
        return self.mode == MODE_DYNAMIC


_DEF_CACHE: dict = {}


def load_definition(session, revision: str):
    """바인딩된 정의(스냅샷 우선, 없으면 같은 revision으로 재컴파일되는 경우만). 실패 시 None."""
    key = (getattr(session, "session_id", None), revision)
    if key in _DEF_CACHE:
        return _DEF_CACHE[key]
    from . import definition_snapshot as _snap
    d = None
    try:
        d = _snap.read_snapshot(session.session_id, expected_revision=revision).definition
    except Exception:
        d = None
    if d is None:
        try:
            from . import definition_compiler as _dc
            res = _dc.compile_scenario(getattr(session, "scenario_data", {}) or {},
                                       getattr(session, "scenario_id", None) or "scenario")
            if res.ok and res.revision == revision:
                d = res.definition
        except Exception:
            d = None
    if d is not None:
        _DEF_CACHE[key] = d
    return d


def pc_actor_for(uid) -> str:
    return f"pc_{uid}"


def session_world_context(session, world=None) -> WorldContext:
    """세션의 세계 모드. DYNAMIC 조건을 하나라도 못 채우면 LEGACY(사유 기록)."""
    rev = getattr(session, "definition_revision", None)
    if not rev:
        return WorldContext(MODE_LEGACY, "definition_unbound")
    definition = load_definition(session, rev)
    if definition is None:
        return WorldContext(MODE_LEGACY, "definition_unavailable", definition_revision=rev)
    caps = set((definition.get("scenario_meta") or {}).get("validated_capabilities") or ())
    if not set(REQUIRED_CAPABILITIES) <= caps:
        return WorldContext(MODE_LEGACY, "capability_absent", definition, rev)
    w = world if world is not None else session
    clock = w.domain("world_clock") if isinstance(w, _wm.ProjectedWorld) else getattr(w, "world_clock", {})
    if not _clock.is_initialized(clock):
        return WorldContext(MODE_LEGACY, "clock_uninitialized", definition, rev)
    reg = w.domain("actor_registry") if isinstance(w, _wm.ProjectedWorld) else getattr(w, "actor_registry", {})
    pcs, uid_of = [], {}
    for uid in sorted((getattr(session, "players", None) or {}), key=str):
        aid = pc_actor_for(uid)
        pl = _sp.placement_of(w, aid)
        if aid in (reg or {}) and isinstance(pl, dict) and pl.get("kind") == _sp.AT_PLACE:
            pcs.append(aid)
            uid_of[aid] = uid
    if not pcs:
        return WorldContext(MODE_LEGACY, "pc_unplaced", definition, rev)
    return WorldContext(MODE_DYNAMIC, "ok", definition, rev, tuple(pcs), pcs[0], uid_of)


def pc_capabilities(session, uid) -> tuple:
    """PC capability = 그 PC가 가진 수량 > 0 소지품 이름(접근 규칙 requires_capability와 대조)."""
    name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
    items = ((getattr(session, "resources", None) or {}).get(name) or {}) if name else {}
    return tuple(sorted(k for k, v in items.items() if isinstance(v, (int, float)) and v > 0))


# ══════════════════════════════════════════════════════════════
#  경로 선택 정책
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RouteChoicePolicy:
    """자동 선택 허용 조건(DESIGN 11 §22) — 짧고·지각 비용 차이가 작고·알려진 제약 차이가 없을 때만.

    값은 H05 기본 calibration(영도 정량 저작 전 — 저작 후 조정 대상).
    """
    auto_if_best_minutes_lt: int = 10
    auto_if_cost_ratio_lte: tuple = (5, 4)        # 2등 비용 ≤ 1등 × 5/4
    max_displayed: int = 3


DEFAULT_ROUTE_POLICY = RouteChoicePolicy()


def route_option(c: _mv.KnownRouteCandidate) -> dict:
    """선택지(플레이어 지식만). signature가 의미 정체성 — option_id는 transient."""
    return {"signature": list(c.signature), "label": c.route_label,
            "minutes": c.perceived_duration_minutes,
            "known_constraints": [dict(k) for k in c.known_constraints]}


def choose_route(plan: _mv.KnownRoutePlan, *, chosen_signature=None,
                 policy: RouteChoicePolicy = DEFAULT_ROUTE_POLICY):
    """(decision, candidate|None, options). decision = AUTO|CHOSEN|ASK|NO_ROUTE|SAME_PLACE."""
    if plan.status == _mv.SAME_PLACE:
        return "SAME_PLACE", None, ()
    if plan.status != _mv.ROUTE_OK or not plan.candidates:
        return "NO_ROUTE", None, ()
    cands = list(plan.candidates)[:policy.max_displayed]
    options = tuple(route_option(c) for c in cands)
    if chosen_signature is not None:
        for c in plan.candidates:
            if tuple(c.signature) == tuple(chosen_signature):
                return "CHOSEN", c, options
        return "ASK", None, options               # 낡은 선택(지식 변화) — 다시 묻는다
    if len(cands) == 1:
        return "AUTO", cands[0], options
    ranked = sorted(cands, key=lambda c: (c.perceived_cost_us, tuple(c.signature)))
    best, second = ranked[0], ranked[1]
    num, den = policy.auto_if_cost_ratio_lte
    minor = (best.cost_known and second.cost_known
             and best.perceived_duration_minutes is not None
             and best.perceived_duration_minutes < policy.auto_if_best_minutes_lt
             and second.perceived_cost_us * den <= best.perceived_cost_us * num
             and not any(c.known_constraints for c in cands))
    if minor:
        return "AUTO", best, options               # 자동 허용 후보 사이 결정적 동률 해소
    return "ASK", None, options


# ══════════════════════════════════════════════════════════════
#  계획 결과
# ══════════════════════════════════════════════════════════════

@dataclass
class PlayerWorldPlan:
    status: str
    source_world_version_id: str | None = None
    start_minute: int | None = None
    end_minute: int | None = None
    ops: tuple = ()
    projected_world: object = None
    code_owned_minutes: int | None = None          # 코드가 소유한 경과(이동·대기) — 추출이 다시 더하지 않음
    outcome: dict = field(default_factory=dict)
    signals: list = field(default_factory=list)
    ties: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    route_options: tuple = ()
    resolved_choice: dict = field(default_factory=dict)   # 안정 의미 선택(H06 재사용)
    scene_place: str | None = None                  # 묘사가 표현할 투영 장면 장소
    transit: dict | None = None                     # 중단 시 TRANSIT 맥락
    reason: str | None = None

    @property
    def has_world_ops(self) -> bool:
        return bool(self.ops)


def namespace_ops(ops, prefix: str, barrier=()) -> list:
    """한 진행 호출의 연산을 고유 id로 옮기고(내부 부모 재매핑) 앞 단계 전체를 인과 장벽으로 더한다."""
    ids = {op.op_id for op in ops}
    m = {i: f"{prefix}.{i}" for i in ids}
    bar = tuple(barrier)
    out = []
    for op in ops:
        parents = tuple(m.get(p, p) for p in op.causal_parent_op_ids if p in ids)
        out.append(_wm.make_op(m[op.op_id], op.op_type, op.source_kind, op.payload, source_ref=op.source_ref,
                               causal_parent_op_ids=tuple(sorted(set(parents) | set(bar))),
                               expected=op.expected, defines_locals=op.defines_locals))
    return out


class _Run:
    """진행 호출들을 이어 붙이는 누적기(정본 불변 — ProjectedWorld만 갱신)."""

    def __init__(self, definition, world, *, seed, tie_orders, prefix, outer_origin="PLAYER_TURN"):
        self.definition = definition
        self.world = world if isinstance(world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(world)
        self.seed, self.tie_orders, self.prefix = seed, dict(tie_orders or {}), prefix
        self.outer_origin = outer_origin
        self.ops, self.signals, self.diags, self.ties = [], [], [], []
        self.n = 0
        self.stop = None                   # (status, detail)
        self.events_changed = []

    def now(self):
        c = self.world.domain("world_clock") or {}
        return c.get("minute_index")

    def step(self, base_ops, target, progress) -> _wp.ProgressionResult | None:
        r = _wp.project_world_progression(self.definition, self.world, target, base_ops=base_ops,
                                          progress=progress, narrative_seed=self.seed,
                                          resolved_tie_orders=self.tie_orders, outer_origin=self.outer_origin)
        self.diags += r.diagnostics
        if r.status == _wp.TIE_PENDING:
            self.ties += list(r.same_minute_ties)
            self.stop = (PLAN_TIE, None)
            return None
        if not r.committable:
            self.stop = (PLAN_CONFLICT, r.status)
            return None
        self.ops += namespace_ops(r.ops, f"{self.prefix}.s{self.n}", [op.op_id for op in self.ops])
        self.n += 1
        self.world = r.projected_world
        self.signals += r.signals
        self.events_changed += r.events_changed
        return r


def _mk(prefix, i, t, p):
    return _wm.make_op(f"{prefix}.{i}", t, "PLAYER_ACTION", p)


def chain(*groups) -> list:
    """같은 분의 기반 연산 묶음을 순서대로 인과 사슬로 잇는다(앞 묶음 결과를 뒤 묶음이 본다)."""
    out, prev = [], []
    for g in groups:
        g = list(g or ())
        for op in g:
            out.append(_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload, source_ref=op.source_ref,
                                   causal_parent_op_ids=tuple(sorted(set(op.causal_parent_op_ids) | set(prev))),
                                   expected=op.expected, defines_locals=op.defines_locals))
        if g:
            prev = [op.op_id for op in out]
    return out


def _arrival_ops(prefix, pcs, edge_id, to_place, minute, *, scene=True):
    ops = []
    for n, pc in enumerate(pcs):
        ops.append(_mk(prefix, f"arr{n}", _sp.OP_ACTOR_PLACEMENT_SET, {"actor_id": pc, "place_id": to_place}))
        ops.append(_mk(prefix, f"edge{n}", _sp.OP_EDGE_KNOWLEDGE_UPSERT, {
            "actor_id": pc, "edge_id": edge_id,
            "fields": {"last_confirmed_at": minute, "acquisition_sources": ["TRAVERSED"]}}))
        ops.append(_mk(prefix, f"visit{n}", _sp.OP_PLACE_KNOWLEDGE_UPSERT, {
            "actor_id": pc, "place_id": to_place, "fields": {"visited": True}}))
    if scene:
        ops.append(_mk(prefix, "scene", _sp.OP_SCENE_PLACE_SET, {"place_id": to_place}))
    return ops


def _access_obs_ops(prefix, pcs, checks, minute):
    seen = {}
    for chk in checks:
        if chk.get("control_id") is not None and chk.get("minute", minute) <= minute:
            seen[chk["control_id"]] = chk
    ops = []
    for n, ref in enumerate(sorted(seen)):
        chk = seen[ref]
        for k, pc in enumerate(pcs):
            ops.append(_mk(prefix, f"acc{n}_{k}", _sp.OP_ACCESS_KNOWLEDGE_UPSERT, {
                "actor_id": pc, "ref": ref, "fields": {
                    "observed_result": chk["result"], "observed_state": chk["state"],
                    "action_policy": chk["action_policy"], "confirmed_at_minute": minute,
                    "source": "OBSERVED"}}))
    return ops


def _progress(lead, scene, **extra):
    p = {"observer_actor": lead, "scene_place": scene}
    p.update({k: v for k, v in extra.items() if v is not None})
    return p


def access_confirm_options(definition: dict, world, pc: str, capabilities=()) -> tuple:
    """현재 장소에서 나가는 알려진 구간 중 PC가 관찰한 CONFIRM 접근 → 확인 토큰(관찰 상태 기준).

    토큰은 관찰한 상태·정책·capability variant로 만든 H04 access_action_id다. 실제 상태가 그 뒤
    바뀌었으면 토큰이 일치하지 않아 자동 통과하지 않는다(확인 ≠ 성공 보장).
    """
    pl = _sp.placement_of(world, pc) or {}
    if pl.get("kind") != _sp.AT_PLACE:
        return ()
    here = pl["place_id"]
    k = _sp.actor_knowledge(world, pc)
    acc_defs = definition.get("access_control_definitions") or {"rules": {}, "controls": {}}
    out = []
    for eid in _sp.known_edge_ids(k):
        e = (definition.get("movement_edges") or {}).get(eid)
        if not e or e["from"] != here or not e.get("access_ref"):
            continue
        fact = (k.get("access") or {}).get(e["access_ref"]) or {}
        if fact.get("observed_result") != _acc.ACTION_REQUIRED or fact.get("action_policy") != _acc.CONFIRM:
            continue
        ctl = acc_defs["controls"].get(e["access_ref"]) or {}
        rule = acc_defs["rules"].get(e.get("access_rule_ref") or ctl.get("rule_ref")) or {}
        try:
            dec = _acc.evaluate_state_table(rule, fact["observed_state"], capabilities)
        except Exception:
            continue
        if dec.get("result") != _acc.ACTION_REQUIRED or dec.get("action_policy") != _acc.CONFIRM:
            continue
        aid = _mv.access_action_id(eid, e["access_ref"], fact["observed_state"], _acc.CONFIRM, dec.get("variant_id"))
        out.append({"action_id": aid, "label": f"{e['to']} 쪽 {e['access_ref']}({fact['observed_state']}) 통과"})
    return tuple(sorted(out, key=lambda o: o["action_id"]))


# ══════════════════════════════════════════════════════════════
#  계획
# ══════════════════════════════════════════════════════════════

def default_mode(definition: dict) -> str | None:
    modes = sorted(definition.get("movement_modes") or {})
    return "walk" if "walk" in modes else (modes[0] if modes else None)


def plan_player_turn(ctx: WorldContext, world, intent: _ai.ResolvedActionIntent, *, transaction_id: str,
                     narrative_seed: str, capabilities=(), resolved_tie_orders=None,
                     policy: RouteChoicePolicy = DEFAULT_ROUTE_POLICY) -> PlayerWorldPlan:
    """ResolvedActionIntent → PlayerWorldPlan(순수). world는 세션 또는 ProjectedWorld."""
    D = ctx.definition
    src = world if isinstance(world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(world)
    lead = ctx.lead_pc
    here = (_sp.placement_of(src, lead) or {}).get("place_id")
    now = (src.domain("world_clock") or {}).get("minute_index")
    prefix = "h05." + stable_digest("player.turn.v1", transaction_id, length=10)
    plan = PlayerWorldPlan(PLAN_NONE, _ai.world_version_id(src), now, now, scene_place=here)
    if intent.stale:
        plan.status, plan.reason = PLAN_REJECTED, "MANIFEST_STALE"
        return plan
    run = _Run(D, src, seed=narrative_seed, tie_orders=resolved_tie_orders, prefix=prefix)
    pre_ops = []
    if intent.stance is not None:
        holder = _cog.actor_holder(lead)
        rec = _cog.record_of(src, holder, intent.stance.info_id)
        if rec is None or rec.get("awareness") != _cog.CONTENT:
            plan.diagnostics.append({"code": "STANCE_TARGET_NOT_KNOWN", "info_id": intent.stance.info_id})
        elif rec.get("belief") != intent.stance.belief:
            pre_ops.append(_mk(prefix, "stance", _cog.OP_COGNITION_BELIEF_SET, {
                "holder": holder, "info_id": intent.stance.info_id, "belief": intent.stance.belief,
                "authority": "PLAYER_EXPLICIT", "cause_ref": f"turn:{transaction_id}:stance"}))
    mv = intent.movement
    if mv is not None and mv.kind == _ai.MOVE_DESTINATION:
        return _plan_movement(ctx, plan, run, src, here, now, mv.destination_place, intent, pre_ops,
                              capabilities=capabilities, policy=policy, transaction_id=transaction_id)
    if intent.time_advance is not None:
        return _plan_time(ctx, plan, run, here, now, intent.time_advance, pre_ops)
    if pre_ops:
        run.step(pre_ops, now, _progress(lead, here))
        return _finish(plan, run, None)
    return plan


def _finish(plan, run, outcome_kind):
    if run.stop is not None:
        plan.status, plan.reason = run.stop
        plan.ties = list(run.ties)
        plan.diagnostics += run.diags
        return plan
    plan.status = PLAN_PLANNED
    plan.ops = tuple(run.ops)
    plan.projected_world = run.world
    plan.end_minute = run.now()
    plan.signals = list(run.signals)
    plan.diagnostics += run.diags
    plan.outcome.setdefault("events_changed", list(run.events_changed))
    if outcome_kind:
        plan.outcome.setdefault("kind", outcome_kind)
    return plan


def _plan_time(ctx, plan, run, here, now, ta: _ai.TimeAdvanceIntent, pre_ops):
    D = ctx.definition
    try:
        target = _ai.target_minute(D.get("calendar") or {}, now, ta)
    except Exception as e:
        plan.status, plan.reason = PLAN_REJECTED, f"TIME_TARGET_INVALID:{e}"
        return plan
    plan.code_owned_minutes = None
    r = run.step(pre_ops, target, _progress(
        ctx.lead_pc, here, activity=ACTIVITY_STATE.get(ta.activity, ta.activity),
        activity_context={"kind": ta.activity, "actor_ids": list(ctx.pcs), "interruptibility": "INTERRUPTIBLE"}))
    if r is None:
        return _finish(plan, run, None)
    reached = r.reached_minute
    plan.code_owned_minutes = reached - now
    plan.outcome.update({"activity": ta.activity, "target_minute": target, "reached_minute": reached,
                         "interrupt": r.foreground_interrupt})
    return _finish(plan, run, OUT_TIME_INTERRUPTED if reached < target else OUT_TIME_DONE)


def _plan_movement(ctx, plan, run, src, here, now, dest, intent, pre_ops, *, capabilities, policy,
                   transaction_id):
    D, lead = ctx.definition, ctx.lead_pc
    mode = default_mode(D)
    plan.outcome.update({"destination": dest, "origin": here, "mode": mode})
    if mode is None or here is None:
        plan.status, plan.reason = PLAN_REJECTED, "NO_MOVEMENT_MODE"
        return plan
    try:
        known = _mv.plan_known_routes(D, _sp.actor_knowledge(src, lead), here, dest, mode)
    except _mv.MovementError as e:
        plan.status, plan.reason = PLAN_REJECTED, f"ROUTE_PLAN_INVALID:{e}"
        return plan
    decision, cand, options = choose_route(known, chosen_signature=intent.route_signature, policy=policy)
    plan.route_options = options
    if decision == "SAME_PLACE":
        plan.reason = "SAME_PLACE"
        if pre_ops:
            run.step(pre_ops, now, _progress(lead, here))
            return _finish(plan, run, None)
        return plan
    if decision == "NO_ROUTE":
        plan.status, plan.reason = PLAN_REJECTED, "NO_KNOWN_ROUTE"       # 실제 경로를 알려 주지 않는다
        return plan
    if decision == "ASK":
        plan.status, plan.reason = PLAN_NEEDS_CHOICE, "ROUTE_CHOICE"
        return plan
    plan.resolved_choice = {"route_signature": list(cand.signature),
                            "access_action_ids": list(intent.access_action_ids)}
    seq = tuple(cand.signature)
    edges = D["movement_edges"]
    pcs = tuple(p for p in ctx.pcs if (_sp.placement_of(src, p) or {}).get("place_id") == here)
    minute, cur, pending = now, here, chain(pre_ops)
    confirmed = tuple(intent.access_action_ids)
    outcome = OUT_ARRIVED
    for i, eid in enumerate(seq):
        e = edges[eid]
        acc_state = run.world.domain("access_control_state") or {}
        try:
            trav = _mv.plan_actual_traversal(D, actor_id=lead, edge_sequence=(eid,), mode_id=mode,
                                             start_minute=minute, access_state=acc_state,
                                             actor_capabilities=tuple(capabilities), confirmed_actions=confirmed)
        except _mv.MovementError as ex:
            plan.diagnostics.append({"code": "MOVEMENT_ROUTE_INVALID", "detail": str(ex)})
            outcome = OUT_INTERRUPTED_BLOCKED
            break
        obs = _access_obs_ops(f"{run.prefix}.e{i}", pcs, trav.access_checks, minute)
        if trav.interruption is not None:
            # 숨은 봉쇄·확인 필요 — 이 구간 시작에서 처음 관찰(우회 없음)
            run.step(chain(pending, obs), minute, _progress(lead, cur))
            outcome = (OUT_INTERRUPTED_ACCESS if trav.interruption.get("kind") == "ACCESS_ACTION_REQUIRED"
                       else OUT_INTERRUPTED_BLOCKED)
            plan.outcome["access_interruption"] = {"edge_id": eid, "kind": trav.interruption.get("kind"),
                                                   "action_policy": trav.interruption.get("action_policy")}
            pending = []
            break
        starts = {c["edge_id"]: c["minute"] for c in trav.checkpoint_times if c["kind"] == _mv.EDGE_START}
        start, end = starts[eid], trav.end_minute
        if start > minute:                                  # 접근 행동 지연 — 장소에서 행동한다
            r = run.step(chain(pending, obs), start, _progress(lead, cur))
            pending, obs = [], []
            if r is None:
                break
            if r.status == _wp.FOREGROUND_INTERRUPT:
                outcome = OUT_INTERRUPTED_SIGNAL
                plan.outcome["interrupt"] = r.foreground_interrupt
                break
            minute = start
        if end == start:                                    # 0분 구간 — 같은 분 도착
            pending = chain(pending, obs, _arrival_ops(f"{run.prefix}.a{i}", pcs, eid, e["to"], minute))
            cur = e["to"]
            continue
        mid = "mv_" + stable_digest("player.movement.v1", transaction_id, eid, minute, length=16)
        transit = [_mk(f"{run.prefix}.t{i}", f"tr{n}", _sp.OP_ACTOR_TRANSIT_SET, {
            "actor_id": pc, "movement_id": f"{mid}_{n}", "edge_id": eid, "from_place": e["from"],
            "to_place": e["to"], "edge_started_at_minute": minute, "edge_expected_end_minute": end})
            for n, pc in enumerate(pcs)]
        r = run.step(chain(pending, obs, transit), end, _progress(lead, cur, movement={"edge_id": eid}))
        pending = []
        if r is None:
            break
        if r.status == _wp.FOREGROUND_INTERRUPT and r.reached_minute < end:
            outcome = OUT_INTERRUPTED_SIGNAL
            plan.transit = {"edge_id": eid, "from_place": e["from"], "to_place": e["to"],
                            "minute": r.reached_minute}
            plan.outcome["interrupt"] = r.foreground_interrupt
            break
        pending = _arrival_ops(f"{run.prefix}.a{i}", pcs, eid, e["to"], end)
        cur, minute = e["to"], end
        if r.status == _wp.FOREGROUND_INTERRUPT:            # 도착 분의 신호 — 도착 후 멈춘다
            outcome = OUT_INTERRUPTED_SIGNAL if i < len(seq) - 1 else OUT_ARRIVED
            plan.outcome["interrupt"] = r.foreground_interrupt
            if i < len(seq) - 1:
                break
    if pending and run.stop is None:
        run.step(pending, minute, _progress(lead, cur))
    if run.stop is None:
        final = _sp.placement_of(run.world, lead) or {}
        plan.scene_place = final.get("place_id") if final.get("kind") == _sp.AT_PLACE else None
        if final.get("kind") == _sp.TRANSIT and plan.transit is None:
            plan.transit = {k: final.get(k) for k in ("edge_id", "from_place", "to_place")}
        plan.code_owned_minutes = (run.now() or now) - now
        plan.outcome.update({"final_place": plan.scene_place, "arrived": plan.scene_place == dest,
                             "route_signature": list(seq)})
    return _finish(plan, run, outcome)
