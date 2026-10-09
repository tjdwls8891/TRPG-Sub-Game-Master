# 사건 — EventTemplate 해석 · EventInstance 정체성 · 국면 선택 · 결정적 케이스 · typed step · 사건 연산 (H04-B)
#
# [권위 — DESIGN 04 §5~66·§132~143 · H04 결정 게이트 A14~A16·A22·A28]
#   event_instances = {instance_id: EventInstance}
#     EventInstance = {"instance_id", "template_id", "template_version", "bound_slots",
#                      "lifecycle": "ACTIVE"|"RESOLVED"|"CANCELLED", "current_phase", "phase_entered_at",
#                      "transition_ordinal", "occurrence_ordinal", "local_state", "history"}
#   spawn 전 DORMANT 인스턴스 없음. 현재 세계 사실(위치·인지·관계)을 인스턴스에 복제하지 않는다.
#   instance_id = "evt_" + stable_digest(narrative_seed, template_id, template_version,
#                                       canonical binding, occurrence_ordinal)
#   국면 epoch = "<instance_id>:<transition_ordinal>" — 예약 owner_epoch로 낡은 타이머를 판정한다.
#
#   국면 선택: mode PRIORITY(eligible 중 최고 priority) | SCORE(base_score + 충족 항 점수, 동점은 priority).
#     stability 기본 STICKY: retain_when(없으면 eligible_when) TRUE면 유지, FALSE·UNKNOWN이면 selector.
#     BEST_MATCH: 항상 selector. MONOTONIC: 현재 rank 이상만. terminal 국면은 selector로 이탈하지 않는다.
#     eligible 국면이 없으면 fallback_phase(명시) — 없으면 임의로 고르지 않는다(진단).
#   전이가 케이스보다 먼저다: from→to가 확정된 뒤 케이스를 고른다(케이스 실패가 전이를 바꾸지 않는다).
#   케이스 선택 기본 DETERMINISTIC_WEIGHTED: seed = stable_digest(narrative_seed, instance_id,
#     transition_ordinal, from, to, 정렬된 후보 case_id) → 정수 weight 누적(case_id 순). FIRST_VALID는 명시 override.
#     Python hash·random 상태를 쓰지 않는다. 선택된 case_id는 history에 저장(재추첨 없음).
#
# [step — 일반 dotted mutation 없음]
#   typed step만: SET_ACCESS_STATE · SET_ACTOR_PLACEMENT · START_MOVEMENT · CREATE_INFORMATION ·
#   SUPERSEDE_INFORMATION · ACQUIRE_INFORMATION · SET_COGNITION_BELIEF · APPEND_RELATIONSHIP_DELTA ·
#   SET_EVENT_LOCAL · SCHEDULE_WAKEUP · SCHEDULE_EFFECT · EMIT_SIGNAL. 각 step은 안정 step_id를 가진다.
from __future__ import annotations

import copy

import itertools
from dataclasses import dataclass, field

from . import cognition as _cog
from . import information as _info
from . import predicates as _pred
from . import relationships as _rel
from . import world_mutation as _wm
from . import world_refs as _refs
from .world_ids import canonical_json, stable_digest

ACTIVE, RESOLVED, CANCELLED = "ACTIVE", "RESOLVED", "CANCELLED"
LIFECYCLES = (ACTIVE, RESOLVED, CANCELLED)
REPEAT_POLICIES = ("ONCE_PER_SESSION", "ONCE_PER_BINDING", "REPEATABLE")
SELECTOR_MODES = ("PRIORITY", "SCORE")
STABILITIES = ("STICKY", "BEST_MATCH", "MONOTONIC")
CASE_SELECTIONS = ("DETERMINISTIC_WEIGHTED", "FIRST_VALID")
SLOT_TYPES = ("ACTOR", "PLACE", "INFO")
STEP_TYPES = ("SET_ACCESS_STATE", "SET_ACTOR_PLACEMENT", "CLEAR_ACTOR_PLACEMENT", "START_MOVEMENT", "CREATE_INFORMATION",
              "SUPERSEDE_INFORMATION", "ACQUIRE_INFORMATION", "SET_COGNITION_BELIEF",
              "APPEND_RELATIONSHIP_DELTA", "SET_EVENT_LOCAL", "SCHEDULE_WAKEUP", "SCHEDULE_EFFECT",
              "EMIT_SIGNAL")
INTERRUPT_POLICIES = ("NONE", "IF_OBSERVABLE")
INSTANCE_KEYS = ("instance_id", "template_id", "template_version", "bound_slots", "lifecycle",
                 "current_phase", "phase_entered_at", "transition_ordinal", "occurrence_ordinal",
                 "local_state", "history")

OP_EVENT_INSTANCE_CREATE = "EVENT_INSTANCE_CREATE"
OP_EVENT_PHASE_TRANSITION = "EVENT_PHASE_TRANSITION"
OP_EVENT_LIFECYCLE_SET = "EVENT_LIFECYCLE_SET"
OP_EVENT_LOCAL_SET = "EVENT_LOCAL_SET"
# H07(사용자 결정): 플레이어 명시 결단 — 예약 지역 상태 키. 플레이어 턴(source_kind PLAYER_RESOLUTION)만 쓸 수 있고
# 한 인스턴스에 한 번만 정해진다. 결과는 저작된 국면·케이스(EVENT_LOCAL resolution 조건)가 적용한다.
RESOLUTION_KEY = "resolution"
RESOLUTION_SOURCE = "PLAYER_RESOLUTION"
OP_EVENT_HISTORY_APPEND = "EVENT_HISTORY_APPEND"
EVENT_OP_TYPES = (OP_EVENT_INSTANCE_CREATE, OP_EVENT_PHASE_TRANSITION, OP_EVENT_LIFECYCLE_SET,
                  OP_EVENT_LOCAL_SET, OP_EVENT_HISTORY_APPEND)


# ══════════════════════════════════════════════════════════════
#  정의·정체성
# ══════════════════════════════════════════════════════════════

def templates(definition: dict) -> dict:
    return definition.get("event_templates") or {}


def template(definition: dict, template_id: str):
    return templates(definition).get(template_id)


def binding_key(bound_slots: dict) -> str:
    return canonical_json(bound_slots or {})


def event_instance_id(narrative_seed: str, template_id: str, template_version: int, bound_slots: dict,
                      occurrence_ordinal: int) -> str:
    return "evt_" + stable_digest("event.instance.v1", narrative_seed, template_id, template_version,
                                  binding_key(bound_slots), occurrence_ordinal)


def epoch_of(instance: dict) -> str:
    return f"{instance['instance_id']}:{instance['transition_ordinal']}"


def enumerate_bindings(tpl: dict) -> list:
    """명시 후보의 정규 순서 데카르트 곱(슬롯 이름 정렬·후보 정렬) − distinct 위반. 슬롯 없으면 [{}]."""
    slots = tpl.get("slots") or {}
    names = sorted(slots)
    if not names:
        return [{}]
    out = []
    for combo in itertools.product(*[sorted(slots[n]["candidates"]) for n in names]):
        b = dict(zip(names, combo))
        if any(b[x] == b[y] for x, y in tpl.get("distinct") or ()):
            continue
        out.append(b)
    return out


def instances_of(world, template_id: str) -> list:
    insts = (world.domain("event_instances") if isinstance(world, _wm.ProjectedWorld)
             else getattr(world, "event_instances", {})) or {}
    return [insts[i] for i in sorted(insts) if insts[i].get("template_id") == template_id]


def spawn_allowed(tpl: dict, world, bound_slots: dict):
    """(허용 여부, occurrence_ordinal, 사유). repeat policy만 본다(spawn_when은 호출자)."""
    existing = instances_of(world, tpl["template_id"])
    same = [i for i in existing if binding_key(i.get("bound_slots")) == binding_key(bound_slots)]
    pol = tpl.get("repeat_policy") or "ONCE_PER_SESSION"
    if pol == "ONCE_PER_SESSION":
        return (not existing, len(existing) + 1, "ONCE_PER_SESSION")
    if pol == "ONCE_PER_BINDING":
        return (not same, len(same) + 1, "ONCE_PER_BINDING")
    if any(i.get("lifecycle") == ACTIVE for i in same):
        return (False, len(same) + 1, "REPEATABLE_ACTIVE_EXISTS")
    cap = tpl.get("max_occurrences")
    if cap is not None and len(same) >= cap:
        return (False, len(same) + 1, "REPEATABLE_MAX_OCCURRENCES")
    return (True, len(same) + 1, "REPEATABLE")


# ══════════════════════════════════════════════════════════════
#  국면 선택 · 케이스 선택
# ══════════════════════════════════════════════════════════════

def select_phase(tpl: dict, ctx: _refs.EvalContext, current_phase=None):
    """selector 실행(stability와 무관). 반환 (phase_id|None, diagnostic|None)."""
    sel = tpl["phase_selector"]
    phases = tpl["phases"]
    cur_rank = phases.get(current_phase, {}).get("rank") if current_phase else None
    elig = []
    for pid in sorted(phases):
        ph = phases[pid]
        if sel["stability"] == "MONOTONIC" and cur_rank is not None and (ph.get("rank") or 0) < cur_rank:
            continue
        if _pred.holds(ph["eligible_when"], ctx):
            elig.append(pid)
    if not elig:
        fb = sel.get("fallback_phase")
        return (fb, "FALLBACK_PHASE") if fb else (None, "NO_ELIGIBLE_PHASE")
    if sel["mode"] == "PRIORITY":
        return max(elig, key=lambda p: (phases[p]["priority"], p)), None
    def score(p):
        """SCORE는 코드 쪽 정수 합(LLM 점수 아님): base_score + 충족(TRUE) 항의 points."""
        ph = phases[p]
        return (ph.get("base_score") or 0) + sum(
            t["points"] for t in ph.get("score_terms") or () if _pred.holds(t["when"], ctx))
    return max(elig, key=lambda p: (score(p), phases[p]["priority"], p)), None


def phase_retained(tpl: dict, ctx, phase_id: str) -> bool:
    ph = tpl["phases"][phase_id]
    cond = ph.get("retain_when") or ph["eligible_when"]
    return _pred.evaluate(cond, ctx) == _pred.TRUE        # UNKNOWN은 유지가 아니다


def case_candidates(tpl: dict, from_phase, to_phase) -> list:
    out = []
    for c in tpl.get("transition_cases") or ():
        if c["to"] != to_phase:
            continue
        if c["from"] == "*" or c["from"] == from_phase:
            out.append(c)
    return out


def choose_case(tpl: dict, ctx, *, narrative_seed: str, instance_id: str, transition_ordinal: int,
                from_phase, to_phase):
    """(case|None, 유효 후보 case_id 목록). ctx는 대상 국면이 적용된 투영."""
    valid = [c for c in case_candidates(tpl, from_phase, to_phase)
             if not c.get("valid_when") or _pred.holds(c["valid_when"], ctx)]
    ids = sorted(c["case_id"] for c in valid)
    if not valid:
        return None, []
    if tpl.get("case_selection") == "FIRST_VALID":
        return valid[0], ids                                 # 저작 순서
    by_id = {c["case_id"]: c for c in valid}
    total = sum(by_id[i]["weight"] for i in ids)
    seed = stable_digest("event.case.v1", narrative_seed, instance_id, transition_ordinal,
                         from_phase, to_phase, ids, length=16)
    pick = int(seed, 16) % total
    acc = 0
    for i in ids:
        acc += by_id[i]["weight"]
        if pick < acc:
            return by_id[i], ids
    return by_id[ids[-1]], ids                               # 도달하지 않음(방어)


# ══════════════════════════════════════════════════════════════
#  step 실행 문맥 · 결과
# ══════════════════════════════════════════════════════════════

@dataclass
class StepContext:
    definition: dict
    world: object                      # 기준 투영(ProjectedWorld)
    instance: dict                     # 전이가 적용된(가상) 인스턴스
    case_id: str | None
    now: int
    narrative_seed: str
    op_prefix: str
    progress: dict = field(default_factory=dict)
    created_infos: dict = field(default_factory=dict)      # step_id → InfoId (같은 케이스 안 참조)

    @property
    def iid(self):
        return self.instance["instance_id"]

    @property
    def epoch(self):
        return epoch_of(self.instance)

    def eval_ctx(self):
        return _refs.EvalContext(self.definition, self.world, bindings=self.instance.get("bound_slots") or {},
                                 self_instance=self.iid, progress=self.progress,
                                 instance_overrides={self.iid: self.instance})

    def resolve(self, v):
        """슬롯·$self·$step 치환(정본 id)."""
        if isinstance(v, str):
            if v.startswith("$step:"):
                return self.created_infos.get(v[6:])
            if v == _refs.SELF:
                return self.iid
            if v.startswith(_refs.SLOT_PREFIX):
                return (self.instance.get("bound_slots") or {}).get(v[len(_refs.SLOT_PREFIX):])
        return v

    def holder(self, v):
        if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX):
            b = self.resolve(v)
            return f"actor:{b}" if b else None
        return v

    def cause(self, step_id):
        return f"event:{self.iid}:t{self.instance['transition_ordinal']}:case:{self.case_id}:step:{step_id}"


@dataclass
class StepResult:
    ops: list = field(default_factory=list)
    signals: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)


def _mk(sctx: StepContext, step_id, n, op_type, payload, parents=()):
    return _wm.make_op(f"{sctx.op_prefix}.{step_id}.{n}", op_type, "EVENT", payload,
                       source_ref=sctx.cause(step_id), causal_parent_op_ids=tuple(parents))


def _diag(code, sctx, step, **kw):
    return dict({"code": code, "instance_id": sctx.iid, "step_id": step.get("step_id")}, **kw)


def run_step(step: dict, sctx: StepContext, parents=()) -> StepResult:
    fn = STEP_HANDLERS.get(step.get("type"))
    if fn is None:
        return StepResult(diagnostics=[_diag("STEP_UNKNOWN", sctx, step)])
    return fn(step, sctx, tuple(parents))


def _interim_world(world, ops):
    """계획용 순수 임시 투영 — ops를 world 위에 한 번 적용(정본·최종 반환 연산 불변).

    임시 묶음에는 연산 사본을 쓰되 부모는 같은 묶음 안의 것만 남긴다(바깥 부모는 이미 world에 반영됨).
    실패하면 None — 최종 라운드 투영이 같은 충돌을 기존 정책(UNIT_REJECTED)으로 처리한다.
    """
    src = world if isinstance(world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(world)
    ids = {op.op_id for op in ops}
    try:
        b = _wm.WorldMutationBatch(batch_id="event.steps.interim", outer_origin="PLAYER_TURN",
                                   source_world_version_id=None, definition_revision=None)
        for op in ops:
            b.add(_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload, source_ref=op.source_ref,
                              causal_parent_op_ids=tuple(x for x in op.causal_parent_op_ids if x in ids),
                              expected=op.expected, defines_locals=op.defines_locals))
        b.freeze()
        res = _wm.project(src, b, _wm.PRODUCTION_REGISTRY)
    except _wm.WorldMutationError:
        return None
    return res.world if res.ok else None


def _observe(sctx: StepContext, world) -> None:
    """뒤 step의 계획 기준을 갱신 — 세계 + (step이 바꿀 수 있는) 인스턴스 지역 상태."""
    sctx.world = world
    inst = (world.domain("event_instances") or {}).get(sctx.iid)
    if isinstance(inst, dict):
        sctx.instance = dict(sctx.instance, local_state=copy.deepcopy(inst.get("local_state") or {}))


def run_steps(steps, sctx: StepContext, parents=(), *, prelude_ops=()) -> StepResult:
    """케이스의 step을 저작 순서대로 인과 사슬로 실행 — 뒤 step은 앞 step이 만든 세계를 계획 시점에 본다.

    prelude_ops: 케이스보다 먼저 확정된 전이·생성 연산(최종 결과에는 포함하지 않는다 — 호출자 소유).
    각 step의 연산을 sctx.world에 순수 임시 투영해 다음 step의 StepContext.world로 쓴다. 반환 연산의
    op_id·인과 부모는 임시 투영과 무관하게 그대로다(최종 커밋 경계는 바깥 묶음).
    """
    out = StepResult()
    prev = tuple(parents)
    if prelude_ops:
        w = _interim_world(sctx.world, prelude_ops)
        if w is None:
            out.diagnostics.append(_diag("STEP_PROJECTION_FAILED", sctx, {"step_id": None}))
            return out
        _observe(sctx, w)
    for step in steps or ():
        r = run_step(step, sctx, prev)
        out.ops += r.ops
        out.signals += r.signals
        out.diagnostics += r.diagnostics
        if r.ops:
            prev = tuple(parents) + tuple(op.op_id for op in r.ops)
            w = _interim_world(sctx.world, r.ops)
            if w is None:                                    # 이후 step의 계획 기준이 없다 — 중단
                out.diagnostics.append(_diag("STEP_PROJECTION_FAILED", sctx, step))
                break
            _observe(sctx, w)
    return out


def _s_access(step, sctx, parents):
    return StepResult([_mk(sctx, step["step_id"], 0, "ACCESS_STATE_SET",
                           {"control_id": step["control"], "state": step["state"]}, parents)])


def _s_placement(step, sctx, parents):
    actor, place = sctx.resolve(step["actor"]), sctx.resolve(step["place"])
    if not actor or not place:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    return StepResult([_mk(sctx, step["step_id"], 0, "ACTOR_PLACEMENT_SET",
                           {"actor_id": actor, "place_id": place}, parents)])


def _s_clear_placement(step, sctx, parents):
    """H07: 정본 override 해제 — 이후 유효 위치는 평상시 일과/untracked(core.npc_position)."""
    actor = sctx.resolve(step["actor"])
    if not actor:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    return StepResult([_mk(sctx, step["step_id"], 0, "ACTOR_PLACEMENT_CLEAR", {"actor_id": actor}, parents)])


def _s_movement(step, sctx, parents):
    from . import world_progression as _wp
    return _wp.start_movement_step(step, sctx, parents)


def event_info_id(sctx: StepContext, step_id: str) -> str:
    return "info_" + stable_digest("info.event.v1", sctx.iid, sctx.instance["transition_ordinal"],
                                   sctx.case_id, step_id)


def _s_create_info(step, sctx, parents):
    iid = event_info_id(sctx, step["step_id"])
    sctx.created_infos[step["step_id"]] = iid
    rec = _info.runtime_definition_record(
        iid, step["proposition"], summary=step.get("summary"),
        subject_refs=[sctx.resolve(r) if not str(r).startswith(("actor:", "place:")) else r
                      for r in step.get("subject_refs") or ()],
        tags=step.get("tags") or (), source_ref=sctx.cause(step["step_id"]), created_at_minute=sctx.now)
    return StepResult([_mk(sctx, step["step_id"], 0, _info.OP_INFORMATION_CREATE, {"definition": rec}, parents)])


def _s_supersede(step, sctx, parents):
    old = sctx.resolve(step["info"])
    if not old:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    iid = event_info_id(sctx, step["step_id"])
    sctx.created_infos[step["step_id"]] = iid
    rec = _info.runtime_definition_record(iid, step["proposition"], summary=step.get("summary"),
                                          derived_from=[old], source_ref=sctx.cause(step["step_id"]),
                                          created_at_minute=sctx.now)
    ops = _info.build_supersede_ops(old, rec, op_prefix=f"{sctx.op_prefix}.{step['step_id']}",
                                    source_kind="EVENT", validity=step.get("validity", _info.SUPERSEDED),
                                    parents=parents)
    return StepResult(ops)


def _s_acquire(step, sctx, parents):
    holder, info = sctx.holder(step["holder"]), sctx.resolve(step["info"])
    if not holder or not info:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    acq = _cog.acquisition_record(f"{sctx.cause(step['step_id'])}:{holder}", step.get("route", "PARTICIPATION"),
                                  at_minute=sctx.now, event_instance=sctx.iid,
                                  fragments=step.get("fragments") or ())
    return StepResult([_mk(sctx, step["step_id"], 0, _cog.OP_COGNITION_ACQUIRE,
                           {"holder": holder, "info_id": info, "awareness": step.get("awareness", "CONTENT"),
                            "acquisition": acq}, parents)])


def _s_belief(step, sctx, parents):
    holder, info = sctx.holder(step["holder"]), sctx.resolve(step["info"])
    if not holder or not info:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    return StepResult([_mk(sctx, step["step_id"], 0, _cog.OP_COGNITION_BELIEF_SET,
                           {"holder": holder, "info_id": info, "belief": step["belief"],
                            "authority": step.get("authority", "RULE"), "cause_ref": sctx.cause(step["step_id"])},
                           parents)])


def _s_relationship(step, sctx, parents):
    a, b = sctx.holder(step["from"]), sctx.holder(step["to"])
    if not a or not b:
        return StepResult(diagnostics=[_diag("STEP_UNBOUND", sctx, step)])
    d = _rel.delta_record(sctx.cause(step["step_id"]), a, b, step["kind"], summary=step.get("summary"),
                          at_minute=sctx.now, source_ref=sctx.iid)
    return StepResult([_mk(sctx, step["step_id"], 0, _rel.OP_RELATIONSHIP_DELTA_APPEND, {"delta": d}, parents)])


def _s_local(step, sctx, parents):
    return StepResult([_mk(sctx, step["step_id"], 0, OP_EVENT_LOCAL_SET,
                           {"instance_id": sctx.iid, "key": step["key"], "value": step["value"]}, parents)])


def schedule_id_for(sctx: StepContext, step_id: str) -> str:
    return "sch_" + stable_digest("event.schedule.v1", sctx.iid, sctx.instance["transition_ordinal"],
                                  sctx.case_id, step_id)


def payload_ref(template_id, case_id, step_id) -> str:
    return f"{template_id}|{case_id}|{step_id}"


def _s_wakeup(step, sctx, parents):
    return StepResult([_mk(sctx, step["step_id"], 0, "SCHEDULE_CREATE", {
        "schedule_id": schedule_id_for(sctx, step["step_id"]), "due_minute": sctx.now + step["after_minutes"],
        "kind": "EVENT_WAKEUP", "owner_ref": f"event:{sctx.iid}",
        "payload_ref": payload_ref(sctx.instance["template_id"], sctx.case_id, step["step_id"]),
        "owner_epoch": sctx.epoch, "conflict_priority": step.get("conflict_priority", 0)}, parents)])


def _s_effect(step, sctx, parents):
    pref = payload_ref(sctx.instance["template_id"], sctx.case_id, step["step_id"])
    return StepResult([_mk(sctx, step["step_id"], 0, "SCHEDULE_CREATE", {
        "schedule_id": schedule_id_for(sctx, step["step_id"]), "due_minute": sctx.now + step["after_minutes"],
        "kind": "DEFERRED_EFFECT", "owner_ref": f"event:{sctx.iid}", "payload_ref": pref,
        "payload": {"instance_id": sctx.iid, "case_id": sctx.case_id, "step_id": step["step_id"]},
        "guard_ref": pref + "|guard" if step.get("guard") else None,
        "owner_epoch": sctx.epoch if step.get("epoch_bound", True) else None,
        "conflict_priority": step.get("conflict_priority", 0)}, parents)])


def _s_signal(step, sctx, parents):
    cond = step.get("observable_when")
    obs = _pred.evaluate(cond, sctx.eval_ctx()) if cond else _pred.FALSE
    sig = {"signal_id": step["signal_id"], "instance_id": sctx.iid, "minute": sctx.now, "cue": step["cue"],
           "interrupt_policy": step.get("interrupt_policy", "NONE"), "observability": obs,
           "observable": obs == _pred.TRUE, "source_ref": sctx.cause(step["step_id"])}
    if step.get("exposes"):
        # H07: 묘사되면 PC가 알 수 있는 저작 정보(관찰 매니페스트 후보). 노출 확정은 추출 관찰만 한다.
        sig["exposes"] = sorted({x for x in (sctx.resolve(v) for v in step["exposes"]) if x})
    return StepResult(signals=[sig])


STEP_HANDLERS = {
    "SET_ACCESS_STATE": _s_access, "SET_ACTOR_PLACEMENT": _s_placement,
    "CLEAR_ACTOR_PLACEMENT": _s_clear_placement, "START_MOVEMENT": _s_movement,
    "CREATE_INFORMATION": _s_create_info, "SUPERSEDE_INFORMATION": _s_supersede,
    "ACQUIRE_INFORMATION": _s_acquire, "SET_COGNITION_BELIEF": _s_belief,
    "APPEND_RELATIONSHIP_DELTA": _s_relationship, "SET_EVENT_LOCAL": _s_local,
    "SCHEDULE_WAKEUP": _s_wakeup, "SCHEDULE_EFFECT": _s_effect, "EMIT_SIGNAL": _s_signal,
}


def instance_relevant(definition: dict, inst: dict, scene) -> bool:
    """H07: 사건이 현재 장면과 닿는가 — relevance_places(슬롯은 binding) 중 하나가 장면 장소이거나 그 상위.
    relevance_places가 없으면 장소에 묶이지 않은 세계 압력으로 보고 닿는다고 본다."""
    tpl = template(definition, inst.get("template_id")) or {}
    rel = tpl.get("relevance_places")
    if not rel:
        return True
    if scene is None:
        return False
    places = definition.get("place_definitions") or {}
    chain, cur, seen = set(), scene, set()
    while cur and cur not in seen:
        seen.add(cur)
        chain.add(cur)
        cur = (places.get(cur) or {}).get("parent")
    b = inst.get("bound_slots") or {}
    for v in rel:
        pid = b.get(v[len("$slot:"):]) if isinstance(v, str) and v.startswith("$slot:") else v
        if pid in chain:
            return True
    return False


def open_resolutions(definition: dict, world, scene) -> list:
    """[(instance_id, option_id, label)] — 진행 중이고 현재 국면이 결단 선택지를 가지며 아직 결단이 없고 장면과 닿는
    사건의 선택지(정렬 · 결정적). 판단층위 후보(매니페스트)·지시층위 안내의 단일 출처."""
    ev = world.domain("event_instances") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "event_instances", {})
    out = []
    for iid in sorted(ev or {}):
        inst = ev[iid]
        if not isinstance(inst, dict) or inst.get("lifecycle") != ACTIVE:
            continue
        if RESOLUTION_KEY in (inst.get("local_state") or {}):
            continue
        tpl = template(definition, inst.get("template_id")) or {}
        ph = (tpl.get("phases") or {}).get(inst.get("current_phase")) or {}
        if not ph.get("resolution_options") or not instance_relevant(definition, inst, scene):
            continue
        for o in ph["resolution_options"]:
            out.append((iid, o["option_id"], o["label"]))
    return out


def find_step(definition: dict, ref: str):
    """payload_ref 'template|case|step' → (template, case, step). SCHEDULE_EFFECT면 (…, effect step)."""
    try:
        tid, cid, sid = ref.split("|")
    except (AttributeError, ValueError):
        return None
    tpl = template(definition, tid)
    if tpl is None:
        return None
    for c in tpl.get("transition_cases") or ():
        if c["case_id"] == cid:
            for st in c.get("steps") or ():
                if st["step_id"] == sid:
                    return tpl, c, st
    return None


# ══════════════════════════════════════════════════════════════
#  typed 연산
# ══════════════════════════════════════════════════════════════

def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def validate_instance(inst) -> list:
    if not isinstance(inst, dict) or set(inst) != set(INSTANCE_KEYS):
        return [f"사건 인스턴스 키는 정확히 {list(INSTANCE_KEYS)}"]
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


def h_event_create(view, op, payload):
    if set(payload) != {"instance"}:
        _reject(op, "payload 키는 instance")
    inst = payload["instance"]
    errs = validate_instance(inst)
    if errs:
        _reject(op, "; ".join(errs))
    if inst["lifecycle"] != ACTIVE:
        _reject(op, "생성은 ACTIVE로만(DORMANT 인스턴스 없음)")
    if view.has("event_instances", (inst["instance_id"],)):
        _reject(op, f"이미 있는 사건 {inst['instance_id']}")
    return [_wm.write(op, "event_instances", (inst["instance_id"],), _wm.MERGE_SINGLE_VALUE, inst)]


def h_event_transition(view, op, payload):
    keys = {"instance_id", "from_phase", "to_phase", "at_minute", "expected_ordinal"}
    if set(payload) != keys:
        _reject(op, f"payload 키는 {sorted(keys)}")
    iid = payload["instance_id"]
    cur = view.get("event_instances", (iid,))
    if cur is None or cur["lifecycle"] != ACTIVE:
        _reject(op, f"ACTIVE 사건이 아닙니다: {iid}")
    if cur["current_phase"] != payload["from_phase"] or cur["transition_ordinal"] != payload["expected_ordinal"]:
        _reject(op, f"{iid}: 전이 기준이 현재 상태와 다릅니다(낡은 전이)")
    if payload["to_phase"] == payload["from_phase"]:
        _reject(op, "같은 국면으로의 전이")
    return [_wm.write(op, "event_instances", (iid, "current_phase"), _wm.MERGE_SINGLE_VALUE, payload["to_phase"]),
            _wm.write(op, "event_instances", (iid, "phase_entered_at"), _wm.MERGE_SINGLE_VALUE, payload["at_minute"]),
            _wm.write(op, "event_instances", (iid, "transition_ordinal"), _wm.MERGE_SINGLE_VALUE,
                      payload["expected_ordinal"] + 1)]


def h_event_lifecycle(view, op, payload):
    if set(payload) != {"instance_id", "lifecycle"}:
        _reject(op, "payload 키는 instance_id·lifecycle")
    iid, lc = payload["instance_id"], payload["lifecycle"]
    cur = view.get("event_instances", (iid,))
    if cur is None:
        _reject(op, f"없는 사건 {iid}")
    if lc not in (RESOLVED, CANCELLED) or cur["lifecycle"] != ACTIVE:
        _reject(op, f"{iid}: {cur['lifecycle']} → {lc} 전이 불가(ACTIVE → RESOLVED|CANCELLED만)")
    return [_wm.write(op, "event_instances", (iid, "lifecycle"), _wm.MERGE_SINGLE_VALUE, lc)]


def h_event_local(view, op, payload):
    if set(payload) != {"instance_id", "key", "value"}:
        _reject(op, "payload 키는 instance_id·key·value")
    iid, key = payload["instance_id"], payload["key"]
    if not view.has("event_instances", (iid,)):
        _reject(op, f"없는 사건 {iid}")
    if not isinstance(key, str) or not key:
        _reject(op, "key는 비어 있지 않은 문자열")
    if (key == RESOLUTION_KEY) != (op.source_kind == RESOLUTION_SOURCE):
        _reject(op, "'resolution'은 플레이어 명시 결단(PLAYER_RESOLUTION)만, 결단 연산은 'resolution'만 씁니다")
    if key == RESOLUTION_KEY:
        if view.get("event_instances", (iid, "lifecycle")) != ACTIVE:
            _reject(op, f"진행 중이 아닌 사건 {iid}의 결단")
        if view.has("event_instances", (iid, "local_state", RESOLUTION_KEY)):
            _reject(op, f"사건 {iid}의 결단은 이미 내려졌습니다(번복 없음)")
    return [_wm.write(op, "event_instances", (iid, "local_state", key), _wm.MERGE_SINGLE_VALUE, payload["value"])]


def h_event_history(view, op, payload):
    if set(payload) != {"instance_id", "entry"} or not isinstance(payload["entry"], dict):
        _reject(op, "payload 키는 instance_id·entry(객체)")
    if not view.has("event_instances", (payload["instance_id"],)):
        _reject(op, f"없는 사건 {payload['instance_id']}")
    return [_wm.write(op, "event_instances", (payload["instance_id"], "history"), _wm.MERGE_APPEND_DEDUPE,
                      payload["entry"])]


_HANDLERS = {OP_EVENT_INSTANCE_CREATE: h_event_create, OP_EVENT_PHASE_TRANSITION: h_event_transition,
             OP_EVENT_LIFECYCLE_SET: h_event_lifecycle, OP_EVENT_LOCAL_SET: h_event_local,
             OP_EVENT_HISTORY_APPEND: h_event_history}


def register(registry: _wm.OperationRegistry) -> None:
    for t, h in _HANDLERS.items():
        registry.register(t, h)
