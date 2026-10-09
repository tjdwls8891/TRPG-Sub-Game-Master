# 세계 진행 — DependencyIndex · 같은 분 due 일괄 · 즉시 연쇄 · 정보 계열 · NPC 이동 · 신호 · 순수 투영 (H04-B)
#
# [권위 — DESIGN 04 §24~35·§67~98·§144~157 · DESIGN 02 §65~79 · H04 결정 게이트 A17~A24]
#   project_world_progression()은 정본 세션을 바꾸지 않는 **순수 투영**이다. 결과의 ops를 하나의
#   WorldMutationBatch로 묶어 기존 커밋 경로(CommitCoordinator·canonical_mutation)가 적용한다.
#   H04에는 production 호출자가 없다(플레이어 턴 배선은 H05).
#
# [알고리즘]
#   now = 기준 시계. (선택) base_ops를 먼저 투영한다.
#   분 T마다:
#     1) due 일괄: due_minute ≤ T 항목을 **같은 기준 상태**에서 각각 평가(삽입 순서 무관 — 정렬 순회).
#        owner_epoch가 낡았거나 guard가 FALSE/UNKNOWN이면 소비·no-op(재예약 polling 없음).
#        효과 의도를 모아 한 계층으로 투영. 같은 정본 칸의 비호환 쓰기는
#          · conflict_priority가 다르면 높은 쪽만 채택(낮은 쪽은 소비만)
#          · 같으면: 현재 장면 관련(관찰자·장면 장소의 행위자·장면 장소 구간의 접근) → SameMinuteNarrativeTie
#            (H04는 순서를 고르지 않는다 — resolved_tie_orders가 주어지면 그 순서로 적용)
#          · 장면 밖이면 WORLD_PROJECTION_CONFLICT(안전 중단)
#     2) 즉시 연쇄: 변경 키 → 영향받는 사건 인스턴스·템플릿 spawn·정보 계열만 재평가(전수 재평가 아님).
#        새 즉시 연산·spawn·전이가 없으면 종료. 시간은 증가하지 않는다.
#        안전 상한: 라운드 MAX_CASCADE_ROUNDS, 사건당 같은 분 전이 MAX_TRANSITIONS_PER_EVENT_PER_MINUTE,
#        같은 분 안 세계 digest 반복 → CASCADE_OSCILLATION 충돌. 서사 규칙이 아니라 안전장치다.
#     3) IF_OBSERVABLE 신호가 관찰 가능하면 그 분에서 멈춘다(foreground_interrupt) — cue는 H05가 묘사에 쓴다.
#     다음 due 분(≤ target)으로 시계를 진행하고 반복, 없으면 target으로 진행하고 끝.
#   라운드끼리는 인과 장벽(이전 라운드 전 연산을 부모로)으로 이어 붙여, 합친 묶음을 원본에 한 번
#   투영한 결과가 라운드별 투영과 같다(최종 검증).
#
# [NPC 이동]
#   START_MOVEMENT·MOVEMENT_WAKEUP 모두 인식 그래프 → KnownRoutePlanner(public_graph) → ActualTraversalPlanner.
#   H07(사용자 결정 — H04 R55 '지식 없으면 이동 없음'의 의도적 supersede): PUBLIC/LOCAL 구간은 지식 없이도
#   일반 그래프로 쓴다. RESTRICTED/SECRET은 아는 경우에만(전지적 숨은 길 사용 금지). 저작된 명시 route는 연속성·
#   실제 접근만 검증하고 행위자 지식으로 거부하지 않는다. 경로가 없으면 MOVEMENT_NO_ROUTE. 최종 ETA 순간이동이 아니라 구간마다
#   checkpoint wakeup이며, wakeup에서 실제 상태·지식을 다시 검증한다. 0분 구간은 TRANSIT 없이 같은 분에 처리.
from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field

from . import events as _ev
from . import information as _info
from . import movement as _mv
from . import predicates as _pred
from . import spatial as _sp
from . import world_mutation as _wm
from . import world_refs as _refs
from . import world_schedule as _sched
from .world_ids import stable_digest

MAX_CASCADE_ROUNDS = 32
MAX_TRANSITIONS_PER_EVENT_PER_MINUTE = 8
MAX_DUE_MINUTES = 10_000                 # 한 투영에서 처리하는 서로 다른 due 분 수 상한(안전)

COMPLETED = "COMPLETED"
FOREGROUND_INTERRUPT = "FOREGROUND_INTERRUPT"
TIE_PENDING = "TIE_PENDING"
CONFLICT = _wm.WORLD_PROJECTION_CONFLICT
CLOCK_UNINITIALIZED = "CLOCK_UNINITIALIZED"
INVALID_TARGET = "INVALID_TARGET"
_STRUCTURAL = ("MERGE_CONFLICT", "PATH_OVERLAP", "UNORDERED_INDEPENDENT_WRITES")


# ══════════════════════════════════════════════════════════════
#  변경 키 · DependencyIndex
# ══════════════════════════════════════════════════════════════

def _d(world, name):
    return world.domain(name) or {}


def changed_keys(before: _wm.ProjectedWorld, after: _wm.ProjectedWorld) -> set:
    """두 투영의 정본 차이 → 의존 키(정규 문자열). 시계·예약 변화는 키가 아니다(wakeup이 담당)."""
    out = set()
    b, a = _d(before, "spatial_state"), _d(after, "spatial_state")
    bp, ap = b.get("placements") or {}, a.get("placements") or {}
    for actor in set(bp) | set(ap):
        if bp.get(actor) != ap.get(actor):
            out.add(f"actor:{actor}:placement")
    if b.get("scene_place") != a.get("scene_place"):
        out.add("scene:place")
    b, a = _d(before, "access_control_state"), _d(after, "access_control_state")
    out |= {f"access:{c}:state" for c in set(b) | set(a) if b.get(c) != a.get(c)}
    bi, ai = _info.normalized(_d(before, "information_state")), _info.normalized(_d(after, "information_state"))
    for k in ("status", "runtime_definitions"):
        out |= {f"info:{i}:validity" for i in set(bi[k]) | set(ai[k]) if bi[k].get(i) != ai[k].get(i)}
    out |= {f"series:{s}" for s in set(bi["series"]) | set(ai["series"]) if bi["series"].get(s) != ai["series"].get(s)}
    b, a = _d(before, "cognition_state"), _d(after, "cognition_state")
    for h in set(b) | set(a):
        bh, ah = b.get(h) or {}, a.get(h) or {}
        out |= {f"cognition:{h}:{i}" for i in set(bh) | set(ah) if bh.get(i) != ah.get(i)}
    b, a = _d(before, "event_instances"), _d(after, "event_instances")
    for iid in set(b) | set(a):
        x, y = b.get(iid) or {}, a.get(iid) or {}
        if x.get("lifecycle") != y.get("lifecycle"):
            out.add(f"event:{iid}:lifecycle")
        if x.get("current_phase") != y.get("current_phase") or x.get("transition_ordinal") != y.get("transition_ordinal"):
            out.add(f"event:{iid}:phase")
        xl, yl = x.get("local_state") or {}, y.get("local_state") or {}
        out |= {f"event:{iid}:local:{k}" for k in set(xl) | set(yl) if xl.get(k) != yl.get(k)}
    b, a = _d(before, "spatial_knowledge"), _d(after, "spatial_knowledge")
    out |= {f"knowledge:{x}:spatial" for x in set(b) | set(a) if b.get(x) != a.get(x)}
    b, a = _d(before, "relationship_state"), _d(after, "relationship_state")
    out |= {f"relationship:{k}" for k in set(b) | set(a) if b.get(k) != a.get(k)}
    b, a = _d(before, "actor_registry"), _d(after, "actor_registry")
    out |= {f"actor:{x}:registry" for x in set(b) | set(a) if b.get(x) != a.get(x)}
    return out


@dataclass
class DependencyIndex:
    """의존 키 → 영향받는 템플릿 spawn 검사 · 사건 인스턴스 · 정보 계열.

    정규 경로는 affected(changed)만 쓴다. full_scan()은 초기화·디버그 전용.
    """
    spawn: dict = field(default_factory=dict)        # key → {template_id}
    instance: dict = field(default_factory=dict)     # key → {instance_id}
    series: dict = field(default_factory=dict)       # key → {series_id}
    wildcards: list = field(default_factory=list)    # (pattern, kind, target)

    def _add(self, key, kind, target):
        if _refs.is_wildcard(key):
            self.wildcards.append((key, kind, target))
        else:
            getattr(self, kind).setdefault(key, set()).add(target)

    def affected(self, changed) -> dict:
        out = {"spawn": set(), "instance": set(), "series": set()}
        for k in changed:
            for kind in out:
                out[kind] |= getattr(self, kind).get(k, set())
            for pat, kind, target in self.wildcards:
                if _refs.key_matches(pat, k):
                    out[kind].add(target)
        return {k: sorted(v) for k, v in out.items()}


def build_dependency_index(definition: dict, world) -> DependencyIndex:
    idx = DependencyIndex()
    pc = _refs.session_pc_actor(world)          # H06: "$session_pc" 키는 정본 단일 PC로만 구체화
    for tid, tpl in sorted(_ev.templates(definition).items()):
        dep = tpl.get("dependency") or {}
        for b in _ev.enumerate_bindings(tpl):
            for key in dep.get("spawn") or ():
                k = _refs.materialize_key(key, b, session_pc=pc)
                if k:
                    idx._add(k, "spawn", tid)
    insts = _d(world, "event_instances") if isinstance(world, _wm.ProjectedWorld) else \
        (getattr(world, "event_instances", {}) or {})
    for iid in sorted(insts):
        inst = insts[iid]
        if inst.get("lifecycle") != _ev.ACTIVE:
            continue
        tpl = _ev.template(definition, inst.get("template_id"))
        if tpl is None:
            continue
        for key in (tpl.get("dependency") or {}).get("instance") or ():
            k = _refs.materialize_key(key, inst.get("bound_slots") or {}, iid, session_pc=pc)
            if k:
                idx._add(k, "instance", iid)
    for sid, sdef in sorted((definition.get("information_series") or {}).items()):
        for key in sdef.get("dependency_keys") or _info.series_dependency_keys(sdef):
            k = _refs.materialize_key(key, {}, session_pc=pc) if _refs.SESSION_PC in key else key
            if k:
                idx._add(k, "series", sid)
    return idx


def full_scan(definition: dict, world) -> dict:
    """초기화·디버그 전용 — 모든 템플릿·ACTIVE 인스턴스·계열(정규 진행 경로에서 쓰지 않는다)."""
    insts = (_d(world, "event_instances") if isinstance(world, _wm.ProjectedWorld)
             else getattr(world, "event_instances", {}) or {})
    return {"spawn": sorted(_ev.templates(definition)),
            "instance": sorted(i for i, x in insts.items() if x.get("lifecycle") == _ev.ACTIVE),
            "series": sorted(definition.get("information_series") or {})}


# ══════════════════════════════════════════════════════════════
#  결과 구조
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class SameMinuteNarrativeTie:
    tie_id: str
    minute: int
    effect_ids: tuple
    allowed_orderings: tuple
    affected_targets: tuple
    # 장면에서 관찰 가능한 경쟁 효과 사실(H05 R2 표시용 — tie 의미·정체성에 관여하지 않는다).
    # 각 항목 {"effect_id", "op_type", "target": [domain, path], "payload"} — 장면 칸에 쓰는 연산만.
    effect_facts: tuple = ()

    def to_dict(self):
        return {"tie_id": self.tie_id, "minute": self.minute, "effect_ids": list(self.effect_ids),
                "allowed_orderings": [list(o) for o in self.allowed_orderings],
                "affected_targets": [list(t) for t in self.affected_targets],
                "effect_facts": [copy.deepcopy(f) for f in self.effect_facts]}


@dataclass
class ProgressionResult:
    status: str
    source_minute: int | None
    reached_minute: int | None
    ops: tuple = ()
    projected_world: object = None
    due_processed: list = field(default_factory=list)
    events_changed: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    foreground_interrupt: dict | None = None
    same_minute_ties: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    dependency_trace: list = field(default_factory=list)

    @property
    def committable(self) -> bool:
        return self.status in (COMPLETED, FOREGROUND_INTERRUPT)

    def build_batch(self, *, batch_id: str, outer_origin: str, source_world_version_id,
                    definition_revision) -> _wm.WorldMutationBatch:
        if not self.committable:
            raise _wm.WorldMutationError(f"{self.status} 결과는 묶음으로 만들 수 없습니다")
        b = _wm.WorldMutationBatch(batch_id=batch_id, outer_origin=outer_origin,
                                   source_world_version_id=source_world_version_id,
                                   definition_revision=definition_revision)
        for op in self.ops:
            b.add(op)
        return b


@dataclass
class _Unit:
    unit_id: str
    ops: list
    priority: int = 0
    signals: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    events: list = field(default_factory=list)


class _Stop(Exception):
    def __init__(self, status, **kw):
        super().__init__(status)
        self.status, self.kw = status, kw


# ══════════════════════════════════════════════════════════════
#  NPC 이동
# ══════════════════════════════════════════════════════════════

def movement_id_for(sctx, step_id) -> str:
    return "mv_" + stable_digest("movement.event.v1", sctx.iid, sctx.instance["transition_ordinal"],
                                 sctx.case_id, step_id)


def _movement_ops(definition, world, *, actor, here, dest, mode, stance, stance_factor, capabilities,
                  route, now, movement_id, owner_iid, op_prefix, parents, arrived_edge=None):
    """현재 장소에서 다음 checkpoint까지(0분 구간은 같은 분 처리). 반환 (ops, diagnostics)."""
    ops, diags, n = [], [], [0]
    par = tuple(parents)

    def mk(t, p):
        n[0] += 1
        return _wm.make_op(f"{op_prefix}.{n[0]}", t, "WORLD_PROGRESSION", p, causal_parent_op_ids=par)

    def know_edge(eid, minute):
        ops.append(mk(_sp.OP_EDGE_KNOWLEDGE_UPSERT, {"actor_id": actor, "edge_id": eid, "fields": {
            "last_confirmed_at": minute, "acquisition_sources": ["TRAVERSED"]}}))

    visited = []
    if arrived_edge is not None:
        know_edge(arrived_edge, now)
        visited.append(here)
    final = None
    if here == dest:
        final = _sp.at_place(here)
    else:
        knowledge = _sp.actor_knowledge(world, actor)
        seq = None
        trav = None
        edges = definition.get("movement_edges") or {}
        if route:
            # H07: 저작된 명시 경로 — 저작자가 이 행위자의 이 경로 이용을 이미 큐레이션했다. 연속성·출발/도착은
            #      검증하되 행위자 지식이 없다는 이유로 거부하지 않는다(실제 통과는 아래 ActualTraversal이 판정).
            if not route_is_continuous(edges, route):
                diags.append({"code": "MOVEMENT_ROUTE_INVALID", "actor_id": actor, "route": list(route)})
            elif edges[route[0]]["from"] != here or edges[route[-1]]["to"] != dest:
                diags.append({"code": "MOVEMENT_ROUTE_ENDPOINTS_MISMATCH", "actor_id": actor,
                              "from": here, "to": dest})
            else:
                seq = tuple(route)                           # 끊긴·어긋난 명시 경로는 대체 경로로 바꾸지 않는다
        else:
            try:
                # H07: PUBLIC/LOCAL 일반 그래프 + 아는 RESTRICTED/SECRET(지식 = 경로 품질, 허가 아님)
                plan = _mv.plan_known_routes(definition, knowledge, here, dest, mode, stance_id=stance,
                                             stance_factor=stance_factor, public_graph=True)
            except _mv.MovementError as exc:             # 방어: 동적 데이터(없는 장소·배수) — API 밖으로 내보내지 않는다
                plan = None
                diags.append({"code": "MOVEMENT_PLAN_INVALID", "actor_id": actor, "detail": str(exc)})
            if plan is None:
                pass
            elif plan.status == _mv.ROUTE_OK:
                seq = plan.candidates[0].edge_ids
            else:
                diags.append({"code": "MOVEMENT_NO_ROUTE", "actor_id": actor, "from": here, "to": dest})
        if seq is not None:
            try:
                trav = _mv.plan_actual_traversal(definition, actor_id=actor, edge_sequence=seq, mode_id=mode,
                                                 start_minute=now, access_state=_d(world, "access_control_state"),
                                                 actor_capabilities=tuple(capabilities or ()), stance_id=stance,
                                                 stance_factor=stance_factor)
            except _mv.MovementError as exc:             # 방어: 진행 API 밖으로 예외를 내보내지 않는다
                diags.append({"code": "MOVEMENT_ROUTE_INVALID", "actor_id": actor, "detail": str(exc)})
        if trav is None:
            final = _sp.at_place(here) if arrived_edge is not None else None
        else:
            ends = {c["edge_id"]: c["minute"] for c in trav.checkpoint_times if c["kind"] == _mv.EDGE_END}
            cur = here
            nxt = None
            for eid in trav.traversed_edges:
                if ends[eid] == now:                              # 0분 구간 — TRANSIT 없이 같은 분
                    know_edge(eid, now)
                    cur = definition["movement_edges"][eid]["to"]
                    visited.append(cur)
                else:
                    nxt = eid
                    break
            seen = {}
            for chk in trav.access_checks:
                if chk["control_id"] is not None and chk["minute"] == now:
                    seen[chk["control_id"]] = chk
            for ref in sorted(seen):
                chk = seen[ref]
                ops.append(mk(_sp.OP_ACCESS_KNOWLEDGE_UPSERT, {"actor_id": actor, "ref": ref, "fields": {
                    "observed_result": chk["result"], "observed_state": chk["state"],
                    "action_policy": chk["action_policy"], "confirmed_at_minute": now, "source": "OBSERVED"}}))
            if nxt is not None:
                e = definition["movement_edges"][nxt]
                end = ends[nxt]
                final = _sp.transit(movement_id=movement_id, edge_id=nxt, from_place=e["from"], to_place=e["to"],
                                    edge_started_at_minute=now, edge_expected_end_minute=end)
                ops.append(mk("SCHEDULE_CREATE", {
                    "schedule_id": "mw_" + stable_digest("movement.wakeup.v1", movement_id, nxt, end),
                    "due_minute": end, "kind": "MOVEMENT_WAKEUP", "owner_ref": f"actor:{actor}",
                    "payload_ref": f"movement:{movement_id}",
                    "payload": {"actor_id": actor, "destination_place": dest, "mode_id": mode, "stance": stance,
                                "stance_factor": str(stance_factor), "capabilities": list(capabilities or ()),
                                "owner_event_instance": owner_iid, "movement_id": movement_id, "edge_id": nxt,
                                "route_edge_ids": list(seq), "explicit_route": bool(route)}}))
            else:
                final = _sp.at_place(cur)
                if not trav.completed or cur != dest:
                    diags.append({"code": "MOVEMENT_STOPPED", "actor_id": actor, "place_id": cur,
                                  "interruption": trav.interruption})
    for pid in visited:
        ops.append(mk(_sp.OP_PLACE_KNOWLEDGE_UPSERT, {"actor_id": actor, "place_id": pid,
                                                      "fields": {"visited": True}}))
    if final is not None:
        if final["kind"] == _sp.TRANSIT:
            ops.append(mk(_sp.OP_ACTOR_TRANSIT_SET, dict({"actor_id": actor},
                                                         **{k: final[k] for k in _sp.TRANSIT_KEYS[1:]})))
        else:
            ops.append(mk(_sp.OP_ACTOR_PLACEMENT_SET, {"actor_id": actor, "place_id": final["place_id"]}))
    return ops, diags


def routine_changed_keys(definition, world, before, after) -> set:
    """두 분 사이에 평상시 일과 위치가 달라진 행위자(정본 override 없음)의 'actor:X:placement' 키.

    일과는 정본이 아니라 정의 + 시계의 파생이므로, 시계가 움직이면 그 파생값의 변화를 의존 키 '변경'으로
    공급한다(사건·정보 계열이 정본 위치 변화와 같은 통로로 재평가된다 — polling·materialize 없음).
    """
    from . import npc_position as _npos
    if before is None or after is None or before == after:
        return set()
    pls = (world._peek("spatial_state") if isinstance(world, _wm.ProjectedWorld)
           else world.domain("spatial_state")) or {}
    pls = pls.get("placements") or {}
    out = set()
    for aid, d in sorted((definition.get("actor_definitions") or {}).items()):
        if not (d or {}).get("routine") or aid in pls:
            continue
        a = (_npos.routine_place_at(definition, aid, before) or (None,))[0]
        b = (_npos.routine_place_at(definition, aid, after) or (None,))[0]
        if a != b:
            out.add(f"actor:{aid}:placement")
    return out


def route_is_continuous(edges: dict, route) -> bool:
    """명시 route 구조 검증 — 비어 있지 않은 정의 구간 id 목록이고 edge[i].to == edge[i+1].from."""
    if not isinstance(route, (list, tuple)) or not route or \
            not all(isinstance(e, str) and e in edges for e in route):
        return False
    return all(edges[a]["to"] == edges[b]["from"] for a, b in zip(route, route[1:]))


def start_movement_step(step, sctx, parents):
    """사건 START_MOVEMENT step — 행위자 지식 기반(전지적 경로 대체 없음)."""
    actor, dest = sctx.resolve(step["actor"]), sctx.resolve(step["destination"])
    if not actor or not dest:
        return _ev.StepResult(diagnostics=[{"code": "STEP_UNBOUND", "instance_id": sctx.iid,
                                            "step_id": step["step_id"]}])
    # H07: 출발점 = 유효 위치(정본 override > 평상시 일과). 일과 NPC를 움직이면 그 이동이 정본 override가 된다.
    from . import npc_position as _npos
    pl = _npos.effective_position(sctx.definition, sctx.world, actor)["placement"]
    if not pl or pl.get("kind") != _sp.AT_PLACE:
        return _ev.StepResult(diagnostics=[{"code": "MOVEMENT_ORIGIN_UNKNOWN" if not pl else "MOVEMENT_ACTOR_IN_TRANSIT",
                                            "instance_id": sctx.iid, "step_id": step["step_id"], "actor_id": actor}])
    ops, diags = _movement_ops(
        sctx.definition, sctx.world, actor=actor, here=pl["place_id"], dest=dest, mode=step["mode"],
        stance=step.get("stance"), stance_factor=step.get("stance_factor", 1), capabilities=step.get("capabilities"),
        route=step.get("route"), now=sctx.now, movement_id=movement_id_for(sctx, step["step_id"]),
        owner_iid=sctx.iid, op_prefix=f"{sctx.op_prefix}.{step['step_id']}", parents=parents)
    for dg in diags:
        dg.update({"instance_id": sctx.iid, "step_id": step["step_id"]})
    return _ev.StepResult(ops=ops, diagnostics=diags)


# ══════════════════════════════════════════════════════════════
#  진행 엔진
# ══════════════════════════════════════════════════════════════

class _Engine:
    def __init__(self, definition, source, target_minute, *, base_ops, progress, narrative_seed,
                 resolved_tie_orders, max_rounds, max_transitions, outer_origin, initial_full_scan=False):
        self.definition = definition
        self.initial_full_scan = initial_full_scan    # H06 INITIALIZATION 전용 — 첫 연쇄를 전수 검사로 시작
        self.source = source if isinstance(source, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(source)
        self.world = self.source.clone()
        self.target = target_minute
        self.base_ops = list(base_ops or ())
        self.progress = dict(progress or {})
        self.seed = narrative_seed
        self.tie_orders = dict(resolved_tie_orders or {})
        self.max_rounds, self.max_transitions = max_rounds, max_transitions
        self.outer_origin = outer_origin          # 진행을 담을 바깥 트랜잭션 원인(독립 SCHEDULED 커밋 없음)
        self.ops, self.prev_round_ids = [], ()
        self.round = 0
        self.result = ProgressionResult(COMPLETED, None, None)
        self.transitions_this_minute = {}
        self.minute_digests = set()
        self.unit_order = {}                 # tie 해소 순서(unit_id → 순번)

    # ── 공통 ──
    def now(self):
        c = self.world.domain("world_clock") or {}
        return c.get("minute_index") if isinstance(c.get("minute_index"), int) else None

    def ctx(self, **kw):
        return _refs.EvalContext(self.definition, self.world, progress=self.progress, **kw)

    def prefix(self, unit_n):
        return f"m{self.now()}.r{self.round}.u{unit_n}"

    def scene_targets(self):
        """현재 장면 관련 정본 칸(관찰자·장면 장소 행위자 배치, 장면 장소 구간의 접근 제어, 장면 장소)."""
        scene = self.progress.get("scene_place")
        if scene is None:
            scene = (self.world.domain("spatial_state") or {}).get("scene_place")
        out = {("spatial_state", ("scene_place",))}
        obs = self.progress.get("observer_actor")
        pls = (self.world.domain("spatial_state") or {}).get("placements") or {}
        for actor, pl in pls.items():
            here = pl.get("place_id") if pl.get("kind") == "AT_PLACE" else None
            ends = (pl.get("from_place"), pl.get("to_place")) if pl.get("kind") == "TRANSIT" else ()
            if actor == obs or (scene is not None and (here == scene or scene in ends)):
                out.add(("spatial_state", ("placements", actor)))
        if obs:
            out.add(("spatial_state", ("placements", obs)))
        for e in (self.definition.get("movement_edges") or {}).values():
            if scene is not None and scene in (e["from"], e["to"]) and e.get("access_ref"):
                out.add(("access_control_state", (e["access_ref"],)))
        return out

    # ── 라운드 적용(충돌·우선순위·tie) ──
    def apply_units(self, units, *, kind):
        units = [u for u in units if u.ops or u.signals or u.diagnostics]
        if not units:
            return set()
        tries = 0
        while True:
            tries += 1
            live = [u for u in units if u.ops]
            if not live:
                for u in units:
                    self.result.diagnostics += u.diagnostics
                    self.result.signals += u.signals
                return set()
            ordered = self._ordered(live)
            batch = _wm.WorldMutationBatch(batch_id=f"wp.r{self.round}", outer_origin=self.outer_origin,
                                           source_world_version_id=None, definition_revision=None)
            for op in ordered:
                batch.add(op)
            batch.freeze()
            res = _wm.project(self.world, batch, _wm.PRODUCTION_REGISTRY)
            if res.ok:
                break
            owner = {op.op_id: u.unit_id for u in live for op in u.ops}
            rejected = {owner.get(c.op_ids[0]) for c in res.conflicts if c.code not in _STRUCTURAL and c.op_ids}
            if rejected:
                for u in units:
                    if u.unit_id in rejected:
                        u.diagnostics.append({"code": "UNIT_REJECTED", "unit_id": u.unit_id,
                                              "detail": [c.message for c in res.conflicts
                                                         if c.op_ids and owner.get(c.op_ids[0]) == u.unit_id]})
                        # due 항목은 효과가 거부돼도 소비한다(재시도 polling 없음)
                        consumes = [op for op in u.ops if op.op_type == _sched.OP_SCHEDULE_CONSUME]
                        u.ops = [] if any(c.op_ids and c.op_ids[0] in {o.op_id for o in consumes}
                                          for c in res.conflicts) else consumes
                        u.signals = []
                        u.events = []                       # 사건 연산이 채택되지 않았다 — 변화 보고 없음
                continue
            groups = self._conflict_groups(res.conflicts, owner)
            # ① 명확히 낮은 효과를 먼저 제거하고 재투영한다 — 같은 우선순위라는 이유만으로 tie를 만들지 않는다
            drop = self._priority_drops(res.conflicts, owner, {u.unit_id: u.priority for u in live})
            tie = None
            for g, targets in groups if not drop else ():
                # ② 제거할 것이 없을 때만: 남은 실제 비호환(성분 전체가 같은 최고 우선순위)이 tie 후보
                gu = [u for u in live if u.unit_id in g]
                remain = sorted(u.unit_id for u in gu)
                if len(remain) > 1:
                    tie_id = "tie_" + stable_digest("progression.tie.v1", self.now(), remain)
                    if tie_id in self.tie_orders:
                        if sorted(self.tie_orders[tie_id]) != remain:
                            raise _Stop(CONFLICT, detail=f"{tie_id}: 해소 순서가 효과 집합과 다릅니다")
                        self._sequence(live, self.tie_orders[tie_id])
                        continue
                    consequential = bool(set(targets) & self.scene_targets())
                    tie = (tie_id, remain, targets, consequential)
                    break
            if tie is not None:
                tie_id, remain, targets, consequential = tie
                if not consequential:
                    raise _Stop(CONFLICT, detail="장면 밖 같은 우선순위 비호환 효과", units=remain,
                                targets=[[d, list(p)] for d, p in targets])
                orders = tuple(itertools.permutations(remain)) if len(remain) <= 4 else ()
                raise _Stop(TIE_PENDING, tie=SameMinuteNarrativeTie(
                    tie_id, self.now(), tuple(remain), orders, tuple((d, tuple(p)) for d, p in sorted(targets)),
                    self._tie_facts(live, remain, targets, res.conflicts)))
            for u in units:
                if u.unit_id in drop:
                    u.diagnostics.append({"code": "SUPERSEDED_BY_PRIORITY", "unit_id": u.unit_id})
                    u.ops = [op for op in u.ops if op.op_type == _sched.OP_SCHEDULE_CONSUME]
                    u.signals = []
                    u.events = []                           # 탈락 — 소비만 남고 사건 변화는 채택되지 않는다
            if tries > 64:
                raise _Stop(CONFLICT, detail="충돌 해소 반복 상한")
        before = self.world
        self.world = res.world
        # 합본용: 라운드 장벽(이전 라운드 전 연산)을 부모로 더한다 — 라운드 투영은 내부 부모만 썼다
        accepted = [_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload, source_ref=op.source_ref,
                                causal_parent_op_ids=tuple(sorted(set(op.causal_parent_op_ids)
                                                                  | set(self.prev_round_ids))),
                                expected=op.expected, defines_locals=op.defines_locals) for op in ordered]
        self.ops += accepted
        self.prev_round_ids = tuple(op.op_id for op in accepted)
        for u in units:
            self.result.diagnostics += u.diagnostics
            self.result.signals += u.signals
            self.result.events_changed += u.events
        self.round += 1
        ch = changed_keys(before, self.world)
        self.result.dependency_trace.append({"minute": self.now(), "round": self.round, "kind": kind,
                                             "units": [u.unit_id for u in units if u.ops],
                                             "changed_keys": sorted(ch)})
        return ch

    def _ordered(self, live):
        """라운드 투영 순서: unit_id 정렬(삽입 무관), tie 해소 단위는 해소 순서로 뒤에. 단위 내부 인과 유지."""
        key = lambda u: (1, self.unit_order[u.unit_id], "") if u.unit_id in self.unit_order else (0, 0, u.unit_id)
        return [op for u in sorted(live, key=key) for op in u.ops]

    def _sequence(self, live, order):
        """해소된 tie: 순서대로 인과 사슬(뒤 효과가 앞 효과를 본다 — 묘사가 정한 순서)."""
        by = {u.unit_id: u for u in live}
        prev = ()
        for i, uid in enumerate(order):
            self.unit_order[uid] = i
            u = by[uid]
            u.ops = [_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload, source_ref=op.source_ref,
                                 causal_parent_op_ids=tuple(sorted(set(op.causal_parent_op_ids) | set(prev))),
                                 expected=op.expected, defines_locals=op.defines_locals) for op in u.ops]
            prev = tuple(op.op_id for op in u.ops)

    def _tie_facts(self, live, remain, targets, conflicts):
        """tie 성분의 경쟁 연산 중 장면 칸(scene_targets)에 쓰는 것만 — 장면 밖·숨은 효과는 싣지 않는다."""
        ops = {op.op_id: (u.unit_id, op) for u in live if u.unit_id in remain for op in u.ops}
        visible = set(targets) & self.scene_targets()
        out, seen = [], set()
        for c in conflicts:
            tgt = (c.domain, tuple(c.path))
            if tgt not in visible:
                continue
            for oid in c.op_ids:
                if oid not in ops or (oid, tgt) in seen:
                    continue
                seen.add((oid, tgt))
                uid, op = ops[oid]
                out.append({"effect_id": uid, "op_type": op.op_type, "target": [tgt[0], list(tgt[1])],
                            "payload": copy.deepcopy(dict(op.payload or {}))})
        return tuple(sorted(out, key=lambda f: (f["effect_id"], f["target"][0], f["target"][1], f["op_type"])))

    def _conflict_groups(self, conflicts, owner):
        """충돌 연산 → 단위 그룹(연결 성분) + 대상 칸. 다리 충돌이 기존 두 그룹을 이으면 하나로 합친다."""
        groups = []
        for c in conflicts:
            us = {owner[o] for o in c.op_ids if o in owner}
            tgt = {(c.domain, tuple(c.path))}
            hit = [g for g in groups if g[0] & us]
            merged = [set(us), set(tgt)]
            for g in hit:
                merged[0] |= g[0]
                merged[1] |= g[1]
            groups = [g for g in groups if not any(g is h for h in hit)] + [merged]
        return [(frozenset(g[0]), g[1]) for g in groups]

    @staticmethod
    def _priority_drops(conflicts, owner, prio):
        """이번 패스에 제거할 낮은 우선순위 단위.

        단위 u는 직접 충돌 상대 중 더 높은 우선순위가 있으면 '지배됨'. 지배되지 않은 상대에게 지배되는
        단위만 이번에 제거한다(그 상대는 확실히 남는다). 제거된 결과로 재투영해 충돌을 다시 계산하므로,
        제거된 단위하고만 충돌하던 효과는 살아남는다. 동일 우선순위·동일 효과는 처음부터 충돌이 아니다.
        """
        partners = {}
        for c in conflicts:
            us = sorted({owner[o] for o in c.op_ids if o in owner})
            for a in us:
                partners.setdefault(a, set()).update(b for b in us if b != a)
        dominated = {u for u, ps in partners.items() if any(prio[p] > prio[u] for p in ps)}
        return {u for u in dominated if any(prio[p] > prio[u] and p not in dominated for p in partners[u])}

    # ── due 일괄 ──
    def process_due(self):
        now = self.now()
        sched = self.world.domain("world_schedule") or {}
        due = [_sched.normalized_entry(e) for e in _sched.ordered(sched) if e["due_minute"] <= now]
        if not due:
            return set(), {"spawn": set(), "instance": set()}
        forced = {"spawn": set(), "instance": set()}
        units = []
        for n, e in enumerate(due):
            u = self._due_unit(e, n, forced)
            units.append(u)
            self.result.due_processed.append({"schedule_id": e["schedule_id"], "kind": e["kind"], "minute": now,
                                              "outcome": u.diagnostics[-1]["code"] if u.diagnostics else "APPLIED"})
        return self.apply_units(units, kind="DUE"), forced

    def _consume(self, e, prefix):
        return _wm.make_op(f"{prefix}.consume", _sched.OP_SCHEDULE_CONSUME, "WORLD_PROGRESSION",
                           {"schedule_id": e["schedule_id"]})

    def _owner_instance(self, e):
        ref = e.get("owner_ref") or ""
        return ref[6:] if ref.startswith("event:") else None

    def _due_unit(self, e, n, forced):
        prefix = f"{self.prefix(n)}.due"
        consume = self._consume(e, prefix)
        u = _Unit(f"due:{e['schedule_id']}", [consume], priority=e.get("conflict_priority") or 0)
        iid = self._owner_instance(e)
        inst = (self.world.domain("event_instances") or {}).get(iid) if iid else None
        if e.get("owner_epoch") is not None:
            if not inst or inst.get("lifecycle") != _ev.ACTIVE or _ev.epoch_of(inst) != e["owner_epoch"]:
                u.diagnostics.append({"code": "STALE_OWNER_EPOCH", "schedule_id": e["schedule_id"]})
                return u
        bindings = (inst or {}).get("bound_slots") or {}
        if e.get("guard_ref"):
            found = _ev.find_step(self.definition, e["guard_ref"].rsplit("|", 1)[0])
            guard = (found[2].get("guard") if found else None)
            val = _pred.evaluate(guard, self.ctx(bindings=bindings, self_instance=iid)) if guard else _pred.UNKNOWN
            if val != _pred.TRUE:
                u.diagnostics.append({"code": "GUARD_" + val, "schedule_id": e["schedule_id"]})
                return u
        kind = e["kind"]
        if kind == _sched.KIND_EVENT_WAKEUP:
            ref = e.get("owner_ref") or ""
            if ref.startswith("template:"):
                forced["spawn"].add(ref[9:])
            elif iid:
                forced["instance"].add(iid)
            return u
        if kind == _sched.KIND_DEFERRED_EFFECT:
            found = _ev.find_step(self.definition, e.get("payload_ref") or "")
            if not found or not inst:
                u.diagnostics.append({"code": "EFFECT_UNRESOLVED", "schedule_id": e["schedule_id"]})
                return u
            tpl, case, step = found
            sctx = _ev.StepContext(self.definition, self.world, dict(inst), case["case_id"], self.now(), self.seed,
                                   op_prefix=f"{prefix}.fx", progress=self.progress)
            r = _ev.run_step(step["effect"], sctx, (consume.op_id,))
            u.ops += r.ops
            u.signals += r.signals
            u.diagnostics += r.diagnostics
            return u
        if kind == _sched.KIND_MOVEMENT_WAKEUP:
            p = e.get("payload") or {}
            actor = p.get("actor_id")
            pl = _sp.placement_of(self.world, actor) if actor else None
            if not pl or pl.get("kind") != _sp.TRANSIT or pl.get("movement_id") != p.get("movement_id") \
                    or pl.get("edge_id") != p.get("edge_id"):
                u.diagnostics.append({"code": "MOVEMENT_WAKEUP_STALE", "schedule_id": e["schedule_id"]})
                return u
            route = None
            if p.get("explicit_route"):                   # H07: 저작 명시 경로는 남은 구간 그대로 이어 간다(재계획 없음)
                seq = list(p.get("route_edge_ids") or ())
                route = seq[seq.index(p["edge_id"]) + 1:] if p["edge_id"] in seq else None
            ops, diags = _movement_ops(
                self.definition, self.world, actor=actor, here=pl["to_place"], dest=p["destination_place"],
                mode=p["mode_id"], stance=p.get("stance"), stance_factor=p.get("stance_factor") or 1,
                capabilities=p.get("capabilities"), route=route, now=self.now(), movement_id=p["movement_id"],
                owner_iid=p.get("owner_event_instance"), op_prefix=f"{prefix}.mv", parents=(consume.op_id,),
                arrived_edge=p["edge_id"])
            u.ops += ops
            u.diagnostics += diags
            return u
        return u

    # ── 즉시 연쇄 ──
    def cascade(self, changed, forced):
        rounds = 0
        while True:
            idx = build_dependency_index(self.definition, self.world)
            aff = idx.affected(changed)
            spawn = sorted(set(aff["spawn"]) | set(forced.get("spawn", ())))
            insts = sorted(set(aff["instance"]) | set(forced.get("instance", ())))
            series = sorted(set(aff["series"]) | set(forced.get("series", ())))
            forced = {}
            units = []
            n = 0
            for sid in series:
                ops, diag = _info.reevaluate_series(self.definition, self.world, sid,
                                                    op_prefix=f"{self.prefix(n)}.series")
                n += 1
                if ops:
                    units.append(_Unit(f"series:{sid}", ops))
                elif diag:
                    self.result.diagnostics.append({"code": diag, "series_id": sid})
            for iid in insts:
                u = self._instance_unit(iid, n)
                n += 1
                if u is not None:
                    units.append(u)
            for tid in spawn:
                for u in self._spawn_units(tid, n):
                    n += 1
                    units.append(u)
            if not any(u.ops for u in units):
                for u in units:
                    self.result.diagnostics += u.diagnostics
                    self.result.signals += u.signals
                return
            rounds += 1
            if rounds > self.max_rounds:
                raise _Stop(CONFLICT, detail=f"즉시 연쇄 라운드 상한 {self.max_rounds} 초과")
            changed = self.apply_units(units, kind="CASCADE")
            dig = self.world.digest()
            if dig in self.minute_digests and changed:
                raise _Stop(CONFLICT, detail="CASCADE_OSCILLATION — 같은 분 안에서 세계 상태가 반복됩니다")
            self.minute_digests.add(dig)

    def _wakeup_op(self, inst, tpl, prefix, ctx):
        """현재 상태의 결과를 바꿀 수 있는 시간 조건만으로 다음 wakeup 1건(polling 없음).

        resolve/cancel + (STICKY이고 현재 국면이 유지 중이면) 현재 국면 유지 조건, 아니면 전체 국면 조건.
        유지 조건이 시간 밖 이유로 깨지면 DependencyIndex가 재평가를 부르고 그때 다시 계산한다.
        """
        preds = [tpl.get("resolve_when"), tpl.get("cancel_when")]
        cur = tpl["phases"].get(inst.get("current_phase"))
        if cur is not None and cur.get("terminal"):
            pass
        elif cur is not None and tpl["phase_selector"]["stability"] == "STICKY" \
                and _ev.phase_retained(tpl, ctx, inst["current_phase"]):
            preds.append(cur.get("retain_when") or cur["eligible_when"])
        else:
            for ph in tpl["phases"].values():
                preds += [ph.get("eligible_when"), ph.get("retain_when")] + \
                    [t["when"] for t in ph.get("score_terms") or ()]
        m = _pred.next_wakeup(preds, ctx)
        if m is None:
            return []
        epoch = _ev.epoch_of(inst)
        sid = "wk_" + stable_digest("event.wakeup.v1", epoch, m)
        if sid in (self.world.domain("world_schedule") or {}):
            return []
        return [_wm.make_op(f"{prefix}.wake", "SCHEDULE_CREATE", "WORLD_PROGRESSION", {
            "schedule_id": sid, "due_minute": m, "kind": _sched.KIND_EVENT_WAKEUP,
            "owner_ref": f"event:{inst['instance_id']}", "payload_ref": f"wakeup:{inst['template_id']}",
            "owner_epoch": epoch})]

    def _cancel_epoch_ops(self, inst, prefix):
        epoch = _ev.epoch_of(inst)
        out = []
        for e in _sched.ordered(self.world.domain("world_schedule") or {}):
            ne = _sched.normalized_entry(e)
            if ne["owner_epoch"] == epoch and ne["due_minute"] > self.now():
                out.append(_wm.make_op(f"{prefix}.cancel.{len(out)}", _sched.OP_SCHEDULE_CANCEL, "WORLD_PROGRESSION",
                                       {"schedule_id": ne["schedule_id"]}))
        return out

    def _history(self, inst, prefix, entry, parents=()):
        return _wm.make_op(f"{prefix}.hist", _ev.OP_EVENT_HISTORY_APPEND, "WORLD_PROGRESSION",
                           {"instance_id": inst["instance_id"], "entry": entry}, causal_parent_op_ids=parents)

    def _source_version(self):
        return (self.source.domain("world_version") or {}).get("version_id") \
            if isinstance(self.source.domain("world_version"), dict) else None

    def _instance_unit(self, iid, n):
        inst = (self.world.domain("event_instances") or {}).get(iid)
        if not inst or inst.get("lifecycle") != _ev.ACTIVE:
            return None
        tpl = _ev.template(self.definition, inst["template_id"])
        if tpl is None:
            self.result.diagnostics.append({"code": "TEMPLATE_UNDEFINED", "instance_id": iid})
            return None
        prefix = f"{self.prefix(n)}.ev"
        u = _Unit(f"event:{iid}:{inst['transition_ordinal']}", [])
        ctx = self.ctx(bindings=inst.get("bound_slots") or {}, self_instance=iid)
        for cond, lc in ((tpl.get("resolve_when"), _ev.RESOLVED), (tpl.get("cancel_when"), _ev.CANCELLED)):
            if cond and _pred.holds(cond, ctx):
                set_op = _wm.make_op(f"{prefix}.lc", _ev.OP_EVENT_LIFECYCLE_SET, "WORLD_PROGRESSION",
                                     {"instance_id": iid, "lifecycle": lc})
                u.ops = [set_op, self._history(inst, prefix, {
                    "kind": lc, "at_minute": self.now(), "transition_ordinal": inst["transition_ordinal"],
                    "from_phase": inst["current_phase"], "to_phase": None, "case_id": None, "case_candidates": [],
                    "cause_world_version": self._source_version()})] + self._cancel_epoch_ops(inst, prefix)
                u.events.append({"instance_id": iid, "change": lc, "minute": self.now()})
                return u
        cur = inst["current_phase"]
        phases = tpl["phases"]
        if cur in phases and phases[cur].get("terminal"):
            return None
        stab = tpl["phase_selector"]["stability"]
        if stab == "STICKY" and cur in phases and _ev.phase_retained(tpl, ctx, cur):
            u.ops = self._wakeup_op(inst, tpl, prefix, ctx)
            return u if u.ops else None
        target, diag = _ev.select_phase(tpl, ctx, cur)
        if target is None or target == cur:
            if diag == "NO_ELIGIBLE_PHASE":
                u.diagnostics.append({"code": "NO_ELIGIBLE_PHASE", "instance_id": iid, "phase": cur})
            u.ops = self._wakeup_op(inst, tpl, prefix, ctx)
            return u
        count = self.transitions_this_minute.get(iid, 0) + 1
        if count > self.max_transitions:
            raise _Stop(CONFLICT, detail=f"{iid}: 같은 분 전이 상한 {self.max_transitions} 초과")
        self.transitions_this_minute[iid] = count
        return self._transition(u, inst, tpl, cur, target, prefix, spawn=False)

    def _transition(self, u, inst, tpl, frm, to, prefix, *, spawn):
        iid = inst["instance_id"]
        ordinal = inst["transition_ordinal"] + (0 if spawn else 1)
        virt = dict(inst, current_phase=to, phase_entered_at=self.now(), transition_ordinal=ordinal)
        vctx = self.ctx(bindings=virt.get("bound_slots") or {}, self_instance=iid,
                        instance_overrides={iid: virt})
        case, cands = _ev.choose_case(tpl, vctx, narrative_seed=self.seed, instance_id=iid,
                                      transition_ordinal=ordinal, from_phase=frm, to_phase=to)
        entry = {"kind": "SPAWN" if spawn else "TRANSITION", "at_minute": self.now(), "transition_ordinal": ordinal,
                 "from_phase": frm, "to_phase": to, "case_id": case["case_id"] if case else None,
                 "case_candidates": cands, "cause_world_version": self._source_version()}
        if spawn:
            virt = dict(virt, history=[entry])
            head = [_wm.make_op(f"{prefix}.create", _ev.OP_EVENT_INSTANCE_CREATE, "WORLD_PROGRESSION",
                                {"instance": virt})]
        else:
            head = [_wm.make_op(f"{prefix}.tr", _ev.OP_EVENT_PHASE_TRANSITION, "WORLD_PROGRESSION", {
                "instance_id": iid, "from_phase": frm, "to_phase": to, "at_minute": self.now(),
                "expected_ordinal": inst["transition_ordinal"]}), self._history(inst, prefix, entry)]
            head += self._cancel_epoch_ops(inst, prefix)
        if case is None and tpl.get("case_required"):
            # 필수 케이스를 고를 수 없으면 전이를 조용히 성립시키지 않는다 — 투영 전체 안전 중단(부분 변이 없음)
            raise _Stop(CONFLICT, reason="CASE_REQUIRED_NONE_VALID", instance_id=iid, from_phase=frm, to_phase=to,
                        case_candidates=cands)
        if case is None and case_candidates_exist(tpl, frm, to):
            u.diagnostics.append({"code": "NO_VALID_CASE", "instance_id": iid, "from": frm, "to": to})
        steps = _ev.StepResult()
        if case is not None:
            sctx = _ev.StepContext(self.definition, self.world, virt, case["case_id"], self.now(), self.seed,
                                   op_prefix=f"{prefix}.case", progress=self.progress)
            steps = _ev.run_steps(case.get("steps") or (), sctx, tuple(op.op_id for op in head[:1]),
                                  prelude_ops=head)
            u.priority = max([0] + [st.get("conflict_priority", 0) for st in case.get("steps") or ()])
        wake = self._wakeup_op(virt, tpl, prefix, vctx)
        wake = [_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload,
                            causal_parent_op_ids=(head[0].op_id,)) for op in wake]
        u.ops = head + steps.ops + wake
        u.signals += steps.signals
        u.diagnostics += steps.diagnostics
        u.events.append({"instance_id": iid, "change": "SPAWN" if spawn else "TRANSITION", "from": frm, "to": to,
                         "case_id": entry["case_id"], "minute": self.now()})
        return u

    def _spawn_units(self, tid, n):
        tpl = _ev.template(self.definition, tid)
        if tpl is None:
            return []
        out = []
        woke = False
        for k, b in enumerate(_ev.enumerate_bindings(tpl)):
            allowed, occ, _why = _ev.spawn_allowed(tpl, self.world, b)
            if not allowed:
                continue
            ctx = self.ctx(bindings=b)
            val = _pred.evaluate(tpl["spawn_when"], ctx)
            prefix = f"{self.prefix(n)}.sp{k}"
            if val != _pred.TRUE:
                m = _pred.next_wakeup([tpl["spawn_when"]], ctx)
                if m is not None and not woke:
                    woke = True
                    sid = "wk_" + stable_digest("event.template_wakeup.v1", tid, m)
                    if sid not in (self.world.domain("world_schedule") or {}):
                        out.append(_Unit(f"spawnwake:{tid}:{m}", [_wm.make_op(
                            f"{prefix}.wake", "SCHEDULE_CREATE", "WORLD_PROGRESSION", {
                                "schedule_id": sid, "due_minute": m, "kind": _sched.KIND_EVENT_WAKEUP,
                                "owner_ref": f"template:{tid}", "payload_ref": f"wakeup:{tid}"})]))
                continue
            iid = _ev.event_instance_id(self.seed, tid, tpl["version"], b, occ)
            proto = {"instance_id": iid, "template_id": tid, "template_version": tpl["version"], "bound_slots": b,
                     "lifecycle": _ev.ACTIVE, "current_phase": None, "phase_entered_at": self.now(),
                     "transition_ordinal": 0, "occurrence_ordinal": occ, "local_state": {}, "history": []}
            pctx = self.ctx(bindings=b, self_instance=iid, instance_overrides={iid: proto})
            target, diag = _ev.select_phase(tpl, pctx, None)
            u = _Unit(f"spawn:{tid}:{_ev.binding_key(b)}:{occ}", [])
            if target is None:
                u.diagnostics.append({"code": "SPAWN_NO_ELIGIBLE_PHASE", "template_id": tid, "binding": b})
                out.append(u)
                continue
            out.append(self._transition(u, proto, tpl, None, target, prefix, spawn=True))
            if (tpl.get("repeat_policy") or "ONCE_PER_SESSION") == "ONCE_PER_SESSION":
                break                         # 세션당 1회 — 같은 평가의 다음 binding은 정규 순서상 뒤
        return out

    # ── 주 루프 ──
    def run(self):
        now = self.now()
        self.result.source_minute = now
        if now is None or _sched.base_quiescence(self.world.domain("world_clock"), {}).reasons:
            self.result.status = CLOCK_UNINITIALIZED
            return self.result
        if self.target is None or not isinstance(self.target, int) or self.target < now:
            self.result.status = INVALID_TARGET
            return self.result
        try:
            changed = set()
            if self.base_ops:
                changed = self.apply_units([_Unit("base", list(self.base_ops))], kind="BASE")
            if self.progress.get("activity"):
                changed = set(changed) | {_refs.OBSERVER_ACTIVITY_KEY}    # H07: 활동 시작 = 활동 조건 재평가
            minutes = 0
            forced = {}
            if self.initial_full_scan:
                # 초기화: 의존 키 변화가 아니라 정의 전체에서 시작한다(정규 PLAYER_TURN 경로는 쓰지 않는다)
                forced = full_scan(self.definition, self.world)
            last_minute = None
            while True:
                minutes += 1
                if minutes > MAX_DUE_MINUTES:
                    raise _Stop(CONFLICT, detail="due 분 처리 상한 초과")
                if self.now() != last_minute:                 # 분이 바뀔 때만 분당 안전 상한을 초기화
                    last_minute = self.now()
                    self.transitions_this_minute, self.minute_digests = {}, {self.world.digest()}
                due_changed, forced_due = self.process_due()
                self.cascade(changed | due_changed, {"spawn": set(forced.get("spawn", ())) | forced_due["spawn"],
                                                     "instance": set(forced.get("instance", ())) | forced_due["instance"]})
                # 연쇄가 같은 분 due를 새로 만들었으면 같은 분에서 다시 처리
                if any(e["due_minute"] <= self.now() for e in (self.world.domain("world_schedule") or {}).values()):
                    changed, forced = set(), {}
                    continue
                fg = [s for s in self.result.signals if s["interrupt_policy"] == "IF_OBSERVABLE" and s["observable"]
                      and s["minute"] == self.now()]
                if fg:
                    self.result.status = FOREGROUND_INTERRUPT
                    self.result.foreground_interrupt = sorted(fg, key=lambda s: (s["signal_id"], s["instance_id"]))[0]
                    break
                nxt = _sched.next_due_after(self.world.domain("world_schedule") or {}, self.now())
                if nxt is None or nxt["due_minute"] > self.target:
                    if self.target > self.now():
                        moved = self._advance(self.target)
                        if moved:                              # H07: 목표 분의 일과 위치 변화도 의존 사건·계열에 반영
                            changed, forced = moved, {}
                            continue
                    break
                changed, forced = self._advance(nxt["due_minute"]), {}
        except _Stop as s:
            self.result.status = s.status
            if s.status == TIE_PENDING:
                self.result.same_minute_ties.append(s.kw["tie"])
            else:
                self.result.diagnostics.append(dict({"code": s.status}, **{k: v for k, v in s.kw.items()}))
            self.result.reached_minute = self.now()
            self.result.ops = ()
            self.result.projected_world = None
            return self.result
        self.result.reached_minute = self.now()
        self.result.ops = tuple(self.ops)
        self.result.projected_world = self.world
        self._verify()
        return self.result

    def _advance(self, minute):
        """시계 전진. 반환 = 평상시 일과 위치가 바뀐 행위자의 의존 키(H07 — 정본 쓰기 없음, 파생 변화 통지)."""
        before = self.now()
        op = _wm.make_op(f"m{minute}.clock", _sched.OP_CLOCK_ADVANCE_TO, "WORLD_PROGRESSION",
                         {"target_minute": minute})
        self.apply_units([_Unit("clock", [op])], kind="CLOCK")
        return routine_changed_keys(self.definition, self.world, before, self.now())

    def _verify(self):
        """합친 묶음을 원본에 한 번 투영한 결과 = 라운드별 투영 결과."""
        if not self.ops:
            return
        b = _wm.WorldMutationBatch(batch_id="wp.verify", outer_origin=self.outer_origin,
                                   source_world_version_id=None, definition_revision=None)
        for op in self.ops:
            b.add(op)
        b.freeze()
        res = _wm.project(self.source, b, _wm.PRODUCTION_REGISTRY)
        if not res.ok or res.world.digest() != self.world.digest():
            raise AssertionError("세계 진행 묶음 재투영 불일치(엔진 버그)")


def case_candidates_exist(tpl, frm, to) -> bool:
    return bool(_ev.case_candidates(tpl, frm, to))


def project_world_progression(definition: dict, source, target_minute: int, *, base_ops=(), progress=None,
                              narrative_seed: str, resolved_tie_orders=None, max_rounds: int = MAX_CASCADE_ROUNDS,
                              max_transitions: int = MAX_TRANSITIONS_PER_EVENT_PER_MINUTE,
                              outer_origin: str = "PLAYER_TURN",
                              initial_full_scan: bool = False) -> ProgressionResult:
    """순수 세계 진행 투영. source(세션·ProjectedWorld)를 바꾸지 않는다.

    progress: {"observer_actor"?, "scene_place"?, "activity"?, "movement_context"?, "activity_context"?}
    resolved_tie_orders: {tie_id: [effect_id 순서]} — H05가 묘사 관찰로 정한 같은 분 선후.
    outer_origin: 결과를 담을 바깥 트랜잭션 원인(PLAYER_TURN·INITIALIZATION·ADMIN_EDIT) — 예약 진행은
                  독립 커밋이 아니라 그 트랜잭션 안에서 커밋된다(DESIGN 04 §2~3).
    """
    return _Engine(definition, source, target_minute, base_ops=base_ops, progress=progress,
                   narrative_seed=narrative_seed, resolved_tie_orders=resolved_tie_orders,
                   max_rounds=max_rounds, max_transitions=max_transitions, outer_origin=outer_origin,
                   initial_full_scan=initial_full_scan and outer_origin == "INITIALIZATION").run()


def initial_template_wakeups(definition: dict, world, *, narrative_seed: str, op_prefix: str = "init") -> list:
    """시간 조건 spawn 템플릿의 첫 wakeup 연산(순수 — 세션 초기화 배선은 H06)."""
    out = []
    ctx = _refs.EvalContext(definition, world)
    for tid, tpl in sorted(_ev.templates(definition).items()):
        m = _pred.next_wakeup([tpl["spawn_when"]], ctx)
        if m is None:
            continue
        out.append(_wm.make_op(f"{op_prefix}.{tid}", "SCHEDULE_CREATE", "INITIALIZATION", {
            "schedule_id": "wk_" + stable_digest("event.template_wakeup.v1", tid, m), "due_minute": m,
            "kind": _sched.KIND_EVENT_WAKEUP, "owner_ref": f"template:{tid}", "payload_ref": f"wakeup:{tid}"}))
    return out
