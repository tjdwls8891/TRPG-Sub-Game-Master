# 턴 세계 — 묘사 투영·관찰·같은 분 선후·보충 진행·후속 묘사·투영 미디어 (H05-B·C)
#
# [권위 — DESIGN 12 · 11 §28~33 · 09 §18~22 · H05 handoff §4~5]
#   전부 순수 함수다(세션 불변). 결과 ops는 PLAYER_TURN 묶음에 담겨 READY → CommitCoordinator로만 커밋된다.
#   · 묘사 입력: 준비된 계획의 **투영 장면**(목적지 도착이면 목적지, 중단이면 중간 장소/TRANSIT)과
#     PC가 관찰할 수 있는 결과만 준다. 실제 숨은 경로·접근·미래 진실은 넣지 않는다.
#   · 추출(NarrationObservation): 확정 묘사를 관찰할 뿐 두 번째 세계 시뮬레이터가 아니다.
#       허용: 실제로 묘사된 정보 노출(후보 키) · 코드가 소유하지 않은 추가 시간 · 같은 분 선후 선택 ·
#             이미 표현된 신호 표시.
#       금지: 사건 국면/케이스 선택 · 숨은 경로/접근 발견 · 코드 소유 이동/시간 덮어쓰기 · PC 믿음 창작.
#   · 코드 소유 경과(이동·대기)가 있으면 추출의 추가 시간은 버린다(이중 계상 금지).
#   · 같은 분 서사 선후: 묘사가 고른 **허용 순서 하나**만 받는다 — 모호·무효면 READY 불가(임의 last-write 없음).
#     장면 밖 충돌은 H04가 이미 WORLD_PROJECTION_CONFLICT로 끝낸다(선택지로 보내지 않는다).
#   · 보충 진행의 결정적 충돌은 재시도 사유가 아니다(provider 재호출·RETRY_PENDING 없음).
#   · ForegroundContinuationPlan: 추출이 만든 새 결과가 현재 장면에서 관찰 가능(IF_OBSERVABLE)하고
#     아직 표현되지 않았을 때만. 같은 논리 턴·추가 시간 0·재추출 없음.
from __future__ import annotations

from dataclasses import dataclass, field

from . import cognition as _cog
from . import information as _info
from . import player_world as _pw
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_progression as _wp
from .world_ids import stable_digest

OBS_FIELD = "world_observation"
MAX_EXTRA_MINUTES = 240                # 추출이 주는 비코드 소유 추가 시간 상한(안전)

TW_READY = "READY"
TW_TIE_UNRESOLVED = "TIE_UNRESOLVED"   # 묘사 선후가 모호·무효 — READY 불가
TW_CONFLICT = "CONFLICT"               # WORLD_PROJECTION_CONFLICT — 결정적, 재시도 없음


# ══════════════════════════════════════════════════════════════
#  런타임 턴 세계 기록(준비 객체에 붙는다 — 영속하지 않는다)
# ══════════════════════════════════════════════════════════════

@dataclass
class WorldTurn:
    ctx: object                                   # player_world.WorldContext
    manifest: object = None                       # ActionReferenceManifest(판단 시점)
    intent: object = None                         # ResolvedActionIntent
    plan: object = None                           # PlayerWorldPlan(묘사 전)
    obs_manifest: object = None                   # ObservationManifest(추출 시점)
    result: object = None                         # TurnWorldResult(추출 후)
    continuation_text: str | None = None          # READY 전 생성, COMMITTED 후 전달
    continuation_delivered: bool = False
    replan: object = None                         # 선후 확정 재계획 호출(callable(orders) -> plan)


# ══════════════════════════════════════════════════════════════
#  투영 장면 · 묘사 오버레이
# ══════════════════════════════════════════════════════════════

def _calendar(definition):
    cal = definition.get("calendar")
    return _clock.Calendar(cal) if cal else None


def clock_view(definition, minute) -> dict:
    cal = _calendar(definition)
    if cal is None or minute is None:
        return {}
    dt = cal.from_minute_index(minute)
    return {"current_date": f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}",
            "time_of_day": cal.daypart_of(minute) or "미확인", "hhmm": f"{dt.hour:02d}:{dt.minute:02d}"}


def mirror_timeline(prev_tl: dict, definition: dict, world, pc: str) -> dict:
    """정본 세계 → 레거시 world_timeline 표시 미러(파생값 — 권위 아님).

    DYNAMIC 세션에서 장소·시간 권위는 정본 spatial_state·world_clock이다. 레거시 소비자(장소 블록·
    퀘스트 필터·디스플레이)는 이 미러를 읽는다. 추출의 장소/날짜 값은 미러에 들어가지 않는다.
    """
    tl = dict(prev_tl or {})
    pl = _sp.placement_of(world, pc) or {}
    if pl.get("kind") == _sp.AT_PLACE:
        tl["current_location"] = pl["place_id"]
    elif pl.get("kind") == _sp.TRANSIT:
        tl["current_location"] = pl["from_place"]
    minute = ((world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
               else getattr(world, "world_clock", {})) or {}).get("minute_index")
    cv = clock_view(definition, minute)
    if cv:
        tl["current_date"], tl["time_of_day"] = cv["current_date"], cv["time_of_day"]
    return tl


def narration_overlay(plan, definition: dict) -> str:
    """묘사층위에 주는 코드 확정 세계 결과(PC가 관찰 가능한 것만)."""
    if plan is None:
        return ""
    lines = ["\n[세계 진행 — 코드 확정 결과. 아래와 다른 시간·장소·결과를 묘사하지 말 것]"]
    o = plan.outcome or {}
    kind = o.get("kind")
    if plan.status == _pw.PLAN_REJECTED:
        if plan.reason == "NO_KNOWN_ROUTE":
            lines.append("· PC는 그곳으로 가는 길을 알지 못한다. 이동은 일어나지 않는다(길을 지어내지 말 것).")
        else:
            lines.append("· 선언한 이동·시간 행동은 성립하지 않았다. 장소와 시간은 그대로다.")
        return "\n".join(lines) + "\n"
    if plan.status == _pw.PLAN_TIE:
        lines.append("· 같은 순간에 아래 일들이 겹친다. 어느 쪽이 먼저인지 **하나의 순서**로 정해 묘사하되 "
                     "결과 자체·시각은 바꾸지 말 것:")
        for k, label in tie_option_labels(plan):
            lines.append(f"  - {k}: {label}")
        return "\n".join(lines) + "\n"
    if plan.status != _pw.PLAN_PLANNED:
        return ""
    if plan.code_owned_minutes:
        lines.append(f"· 경과 시간: {plan.code_owned_minutes}분")
    cv = clock_view(definition, plan.end_minute)
    if cv:
        lines.append(f"· 현재 시각: {cv['current_date']} {cv['hhmm']} ({cv['time_of_day']})")
    if kind == _pw.OUT_ARRIVED:
        lines.append(f"· PC는 {o.get('final_place')}에 도착했다.")
    elif kind == _pw.OUT_INTERRUPTED_SIGNAL:
        if plan.transit:
            lines.append(f"· PC는 {plan.transit['from_place']}에서 {plan.transit['to_place']} 쪽으로 가던 길 "
                         f"위에서 멈췄다(아직 도착하지 않았다 — 목적지 모습을 묘사하지 말 것).")
        else:
            lines.append(f"· PC는 {plan.scene_place}에서 멈췄다.")
    elif kind in (_pw.OUT_INTERRUPTED_ACCESS, _pw.OUT_INTERRUPTED_BLOCKED):
        lines.append(f"· PC는 {plan.scene_place}에서 더 나아가지 못했다 — 앞길이 막혀 있음을 이제 처음 확인했다. "
                     f"우회로를 지어내지 말 것.")
    elif kind in (_pw.OUT_TIME_DONE, _pw.OUT_TIME_INTERRUPTED):
        act = o.get("activity")
        lines.append(f"· PC의 활동({act})이 " + ("중간에 끊겼다." if kind == _pw.OUT_TIME_INTERRUPTED else "끝났다."))
    for sig in observable_signals(plan.signals):
        lines.append(f"· 지금 관찰되는 일: {sig['cue']}")
    return "\n".join(lines) + "\n"


def observable_signals(signals) -> list:
    out, seen = [], set()
    for s in signals or ():
        if s.get("observable") and s.get("interrupt_policy") == "IF_OBSERVABLE":
            k = (s.get("signal_id"), s.get("instance_id"))
            if k not in seen:
                seen.add(k)
                out.append(s)
    return out


def projected_scene(plan) -> dict:
    """ProjectedMediaContext의 장면 부분 — 묘사가 실제로 표현하는 장소."""
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return {"scene_place_id": None, "transit": None}
    return {"scene_place_id": plan.scene_place, "transit": plan.transit}


# ══════════════════════════════════════════════════════════════
#  같은 분 서사 선후
# ══════════════════════════════════════════════════════════════

def _unit_label(uid: str) -> str:
    if uid.startswith("due:"):
        return f"예약된 일({uid[4:]})"
    if uid.startswith("event:"):
        return f"사건 전개({uid.split(':')[1]})"
    if uid == "base":
        return "PC의 행동"
    return uid


def tie_option_labels(plan) -> list:
    """[(O키, 설명)] — 허용 순서마다 하나. 키는 transient(영속하지 않는다)."""
    out = []
    n = 0
    for tie in plan.ties or ():
        for order in tie.allowed_orderings:
            n += 1
            out.append((f"O{n}", " → ".join(_unit_label(u) for u in order)))
    return out


def tie_orders_for(plan, key) -> dict | None:
    """O키 → {tie_id: [order]}. 무효·모호(복수 tie)면 None."""
    if not plan.ties or len(plan.ties) != 1:
        return None
    n = 0
    for tie in plan.ties:
        for order in tie.allowed_orderings:
            n += 1
            if f"O{n}" == key:
                return {tie.tie_id: list(order)}
    return None


# ══════════════════════════════════════════════════════════════
#  관찰 매니페스트(추출) · 처리
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ObservationManifest:
    infos: tuple = ()             # (X키, info_id, 설명) — 이번 장면에서 묘사로 노출될 수 있는 정보
    signals: tuple = ()           # (S키, (signal_id, instance_id), cue)
    ties: tuple = ()              # (O키, 설명)
    extra_time_allowed: bool = True

    def schema(self) -> dict:
        def enum(rows):
            return {"type": "string", "enum": ["NONE"] + [r[0] for r in rows]}
        return {"type": "object",
                "description": "확정 묘사에서 실제로 관찰된 세계 사실(후보 키만). 없으면 빈 값.",
                "properties": {
                    "info_exposed": {"type": "array", "items": enum(self.infos)},
                    "extra_minutes": {"type": "integer"},
                    "tie_order": enum(self.ties),
                    "represented_signals": {"type": "array", "items": enum(self.signals)}}}

    def render(self) -> str:
        lines = ["\n[세계 관찰 — world_observation]"]
        if self.infos:
            lines.append("묘사에서 PC가 실제로 듣거나 본 정보만 info_exposed에(추측 금지): "
                         + ", ".join(f"{k}={lbl}" for k, _, lbl in self.infos))
        if self.signals:
            lines.append("묘사에 이미 나온 장면 사건만 represented_signals에: "
                         + ", ".join(f"{k}={cue}" for k, _, cue in self.signals))
        if self.ties:
            lines.append("묘사가 택한 선후 순서 하나를 tie_order에: " + ", ".join(f"{k}={lbl}" for k, lbl in self.ties))
        if self.extra_time_allowed:
            lines.append("이동·대기가 아닌 행동으로 묘사상 분명히 흐른 추가 시간이 있으면 extra_minutes(분, 없으면 0).")
        else:
            lines.append("이번 턴 경과 시간은 코드가 정했다 — extra_minutes는 0.")
        lines.append("PC의 믿음·국면·경로·접근은 판정하지 말 것.")
        return "\n".join(lines) + "\n"

    def info_of(self, key):
        return next((r[1] for r in self.infos if r[0] == key), None)

    def signal_of(self, key):
        return next((r[1] for r in self.signals if r[0] == key), None)


def _actors_at(world, place) -> list:
    pls = (world.domain("spatial_state") or {}).get("placements") or {}
    return sorted(a for a, p in pls.items() if p.get("kind") == _sp.AT_PLACE and p.get("place_id") == place)


def build_observation_manifest(definition: dict, ctx, plan, base_world) -> ObservationManifest:
    """노출 후보 = 장면에 함께 있는 행위자가 내용을 아는 정보 중 PC가 아직 내용을 모르는 것.

    장면 밖·아무도 모르는 정보는 후보가 아니다(추출 문맥에 불필요한 숨은 진실을 주지 않는다).
    """
    world = plan.projected_world if plan is not None and plan.projected_world is not None else (
        base_world if isinstance(base_world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(base_world))
    lead = ctx.lead_pc
    scene = (plan.scene_place if plan is not None and plan.status == _pw.PLAN_PLANNED else None) or \
        (_sp.placement_of(world, lead) or {}).get("place_id")
    cs = world.domain("cognition_state") or {}
    pc_recs = cs.get(_cog.actor_holder(lead)) or {}
    cand = set()
    for a in _actors_at(world, scene) if scene else ():
        if a in ctx.pcs:
            continue
        for iid, rec in (cs.get(_cog.actor_holder(a)) or {}).items():
            if isinstance(rec, dict) and rec.get("awareness") == _cog.CONTENT:
                mine = pc_recs.get(iid)
                if not mine or mine.get("awareness") != _cog.CONTENT:
                    cand.add(iid)
    st = world.domain("information_state") or {}
    infos = tuple((f"X{n}", iid, ((_info.resolve_definition(definition, st, iid) or {}).get("summary")
                                  or (_info.resolve_definition(definition, st, iid) or {}).get("proposition") or iid))
                  for n, iid in enumerate(sorted(cand), 1))
    sigs, seen = [], set()
    for s in observable_signals(plan.signals if plan is not None else ()):
        k = (s["signal_id"], s["instance_id"])
        seen.add(k)
        sigs.append((k, s["cue"]))
    for k, cue in _active_signal_candidates(definition, world):
        if k not in seen:
            seen.add(k)
            sigs.append((k, cue))
    signals = tuple((f"S{n}", k, cue) for n, (k, cue) in enumerate(sigs, 1))
    ties = tuple(tie_option_labels(plan)) if plan is not None and plan.status == _pw.PLAN_TIE else ()
    extra = not (plan is not None and plan.code_owned_minutes)
    return ObservationManifest(infos, signals, ties, extra)


def _active_signal_candidates(definition, world) -> list:
    """현재 ACTIVE 사건 템플릿의 EMIT_SIGNAL(cue만) — 묘사가 이미 표현한 반응의 중복 방지용."""
    out = []
    for iid, inst in sorted((world.domain("event_instances") or {}).items()):
        if inst.get("lifecycle") != "ACTIVE":
            continue
        tpl = (definition.get("event_templates") or {}).get(inst.get("template_id")) or {}
        for case in tpl.get("transition_cases") or ():
            for st in case.get("steps") or ():
                eff = st.get("effect") if st.get("type") == "SCHEDULE_EFFECT" else st
                if isinstance(eff, dict) and eff.get("type") == "EMIT_SIGNAL":
                    out.append(((eff["signal_id"], iid), eff.get("cue") or ""))
    return out


@dataclass
class TurnWorldResult:
    status: str
    ops: tuple = ()
    projected_world: object = None
    plan: object = None                     # 확정된(선후 반영) 묘사 전 계획
    signals: list = field(default_factory=list)            # 보충 진행 신호
    continuation_signals: list = field(default_factory=list)
    represented: tuple = ()
    diagnostics: list = field(default_factory=list)
    exposures: tuple = ()
    extra_minutes: int = 0
    conflict: dict | None = None


def process_observation(ctx, base_world, plan, obs_manifest: ObservationManifest, raw_obs, *,
                        transaction_id: str, narrative_seed: str, replan=None) -> TurnWorldResult:
    """확정 묘사 관찰 → 보충 typed 연산 + 보충 세계 진행 → 최종 묶음 연산(순수)."""
    D, lead = ctx.definition, ctx.lead_pc
    raw = raw_obs if isinstance(raw_obs, dict) else {}
    diags = []
    # ① 같은 분 선후(묘사 전 계획이 TIE였다면 필수)
    if plan is not None and plan.status == _pw.PLAN_TIE:
        orders = tie_orders_for(plan, raw.get("tie_order"))
        if orders is None or replan is None:
            return TurnWorldResult(TW_TIE_UNRESOLVED, plan=plan,
                                   diagnostics=[{"code": "TIE_ORDER_INVALID", "value": raw.get("tie_order")}])
        plan = replan(orders)
        if plan.status != _pw.PLAN_PLANNED:
            return TurnWorldResult(TW_TIE_UNRESOLVED if plan.status == _pw.PLAN_TIE else TW_CONFLICT, plan=plan,
                                   diagnostics=[{"code": "TIE_ORDER_NOT_RESOLVING", "status": plan.status}])
    elif raw.get("tie_order") not in (None, "", "NONE"):
        diags.append({"code": "TIE_ORDER_IGNORED"})
    if plan is not None and plan.status == _pw.PLAN_CONFLICT:
        return TurnWorldResult(TW_CONFLICT, plan=plan, conflict={"stage": "pre_narration"})
    start_world = plan.projected_world if plan is not None and plan.projected_world is not None else (
        base_world if isinstance(base_world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(base_world))
    stage1 = tuple(plan.ops) if plan is not None and plan.status == _pw.PLAN_PLANNED else ()
    # ② 정보 노출 → PC 획득(믿음 불변)
    prefix = "h05o." + stable_digest("player.observe.v1", transaction_id, length=10)
    exposures, acq = [], []
    for n, key in enumerate(sorted(set(k for k in (raw.get("info_exposed") or ()) if isinstance(k, str)))):
        iid = obs_manifest.info_of(key)
        if iid is None:
            if key != "NONE":
                diags.append({"code": "INFO_EXPOSURE_INVALID", "key": key})
            continue
        exposures.append(iid)
        acq.append(_wm.make_op(f"{prefix}.acq{n}", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
            "holder": _cog.actor_holder(lead), "info_id": iid, "awareness": _cog.CONTENT,
            "acquisition": _cog.acquisition_record(f"turn:{transaction_id}:obs:{iid}", "TESTIMONY",
                                                   at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))}))
    # ③ 추가 시간(코드 소유 경과가 있으면 버린다)
    extra = raw.get("extra_minutes")
    extra = extra if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0 else 0
    if extra and not obs_manifest.extra_time_allowed:
        diags.append({"code": "EXTRA_TIME_CODE_OWNED", "ignored": extra})
        extra = 0
    if extra > MAX_EXTRA_MINUTES:
        diags.append({"code": "EXTRA_TIME_CLAMPED", "value": extra})
        extra = MAX_EXTRA_MINUTES
    represented = set()
    for k in raw.get("represented_signals") or ():
        sk = obs_manifest.signal_of(k) if isinstance(k, str) else None
        if sk:
            represented.add(tuple(sk))
    for s in observable_signals(plan.signals if plan is not None else ()):
        represented.add((s["signal_id"], s["instance_id"]))          # 묘사 전 신호는 본 묘사가 표현했다
    # ④ 보충 진행(노출로 인한 연쇄 + 추가 시간)
    scene = (plan.scene_place if plan is not None and plan.status == _pw.PLAN_PLANNED else None) or \
        (_sp.placement_of(start_world, lead) or {}).get("place_id")
    now = (start_world.domain("world_clock") or {}).get("minute_index")
    ops, world, sigs = list(stage1), start_world, []
    if acq or extra:
        r = _wp.project_world_progression(D, start_world, now + extra, base_ops=acq,
                                          progress={"observer_actor": lead, "scene_place": scene},
                                          narrative_seed=narrative_seed, outer_origin="PLAYER_TURN")
        diags += r.diagnostics
        if r.status == _wp.TIE_PENDING:
            return TurnWorldResult(TW_TIE_UNRESOLVED, plan=plan, diagnostics=diags + [
                {"code": "SUPPLEMENTAL_TIE", "ties": [t.to_dict() for t in r.same_minute_ties]}])
        if not r.committable:
            return TurnWorldResult(TW_CONFLICT, plan=plan, diagnostics=diags,
                                   conflict={"stage": "post_narration", "status": r.status})
        ops += _pw.namespace_ops(r.ops, f"{prefix}.sup", [o.op_id for o in stage1])
        world, sigs = r.projected_world, list(r.signals)
    cont = [s for s in observable_signals(sigs) if (s["signal_id"], s["instance_id"]) not in represented]
    return TurnWorldResult(TW_READY, tuple(ops), world, plan, sigs, cont, tuple(sorted(represented)),
                           diags, tuple(exposures), extra)


# ══════════════════════════════════════════════════════════════
#  ForegroundContinuationPlan · 투영 미디어
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ForegroundContinuationPlan:
    source_transaction_id: str
    at_minute: int | None
    projected_scene: str | None
    signals: tuple

    def prompt(self) -> str:
        cues = "\n".join(f"- {s['cue']}" for s in self.signals)
        return ("[후속 묘사 — 같은 턴의 마지막 세계 반응]\n"
                "[이미 확정된 즉시 세계 반응 — 아래만 짧게 묘사한다]\n" + cues + "\n"
                f"[현재 장면] {self.projected_scene or '현재 장소'}\n"
                "[금지] 새 사건·새 이동·시간 경과·새 인물 계획을 만들지 말 것. PC의 선택·생각·감정·행동을 "
                "정하지 말 것. 앞으로 일어날 일을 암시하거나 밝히지 말 것.")


def continuation_plan(transaction_id: str, result: TurnWorldResult) -> ForegroundContinuationPlan | None:
    if result is None or result.status != TW_READY or not result.continuation_signals:
        return None
    w = result.projected_world
    minute = (w.domain("world_clock") or {}).get("minute_index") if w is not None else None
    scene = result.plan.scene_place if result.plan is not None else None
    return ForegroundContinuationPlan(transaction_id, minute, scene, tuple(result.continuation_signals))


def media_allowed_places(definition: dict, plan) -> set | None:
    """투영 장면이 허용하는 장소 이미지 키(장면 장소 + 상위 상속). None = 제한 없음(LEGACY).

    TRANSIT(중단) — 구간 저작 미디어 → 출발 장소 문맥. 도착 전 목적지 이미지는 금지.
    """
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return None
    g = _sp.SpatialGraph(definition)
    scene = plan.scene_place
    if scene is None and plan.transit:
        scene = plan.transit.get("from_place")
    if scene is None:
        return set()
    return set(g.path_of(scene))


def image_for_scene(definition: dict, plan) -> str | None:
    """투영 장면의 장소 이미지 키(정의 image, 상위 상속). TRANSIT은 구간 미디어 우선."""
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return None
    places = definition.get("place_definitions") or {}
    if plan.scene_place is None and plan.transit:
        e = (definition.get("movement_edges") or {}).get(plan.transit.get("edge_id")) or {}
        if e.get("media"):
            return e["media"]
    allowed = media_allowed_places(definition, plan) or set()
    g = _sp.SpatialGraph(definition)
    scene = plan.scene_place or (plan.transit or {}).get("from_place")
    for pid in reversed(g.path_of(scene)) if scene else ():
        if pid in allowed and (places.get(pid) or {}).get("image"):
            return places[pid]["image"]
    return None
