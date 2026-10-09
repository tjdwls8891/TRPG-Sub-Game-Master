# 동적 세계 세션 생애주기 — 정의 바인딩 · 초기 세계 계획 · INITIALIZATION · 열기 상태 (H06-A)
#
# [권위 — 통합 설계 §49·§53 · DESIGN 11 §6~11 · H06 handoff §3]
#   새 DYNAMIC 세션의 유일한 순서:
#     scenario source(세션에 실린 scenario_data) → compile → durable 정의 스냅샷 → definition_revision 바인딩
#     → 시작 선택 해석 → InitialWorldPlan(typed 연산) → CanonicalMutationCoordinator(INITIALIZATION)
#     → genesis world_version → (캐시 열기) → 인트로 → OPEN
#   · DYNAMIC 대상은 PC가 정확히 한 명이고 컴파일러가 정량 시간·구조화 장소를 검증한 세션뿐이다
#     (사용자 결정 2026-10-08 — 다인 세션은 LEGACY, 다인 동적 의미를 만들지 않는다).
#   · 정의 바인딩 writer는 이 모듈의 bind_definition 하나다. 재시작은 스냅샷만 읽는다(현재 원본 파일·
#     scenario_data 재컴파일로 대체하지 않는다). 스냅샷 손상·부재는 BLOCKED(운영자 확인) — 조용한 재컴파일 없음.
#   · 초기 세계는 정의와 시작 선택에 있는 것만 심는다(산문에서 새 의미를 추측하지 않는다):
#     PC ActorId 등록 · 시계 · PC 배치/장면 · PC 공간 지식(시작 장소 + 선택된 seed) · 시간 조건 spawn wakeup
#     · 전수 검사 1회로 시작 시점에 성립하는 사건/정보 계열. 접근 상태는 정의 initial_state가 기본값이다.
#   · 초기화는 생애주기 수준에서 멱등이다 — 같은 initialization_id로 다시 불러도 genesis를 두 번 만들지 않는다.
#     캐시(provider) 실패는 세계를 되돌리지 않는다 — INITIALIZED에 머물고 인트로를 하지 않는다.
#   · provider 호출·재무 사실을 만들지 않는다.
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from . import canonical_mutation as _cm
from . import spatial as _sp
from . import world_mutation as _wm
from . import world_schedule as _sched
from . import world_state as _ws
from .world_ids import stable_digest

LIFECYCLE_INITIALIZED = "INITIALIZED"     # genesis 커밋됨 — 아직 열리지 않음(인트로 전)
LIFECYCLE_OPEN = "OPEN"                   # 인트로 완료 — DYNAMIC 자동 턴 가능
LIFECYCLE_CLOSING = "CLOSING"             # 종료 진행 중(strict) — 모든 진행 차단, 재무 finalize 뒤 CLOSED로만 간다
LIFECYCLE_CLOSED = "CLOSED"               # 세션 종료(세계 상태는 그대로 보존)
LIFECYCLES = (LIFECYCLE_INITIALIZED, LIFECYCLE_OPEN, LIFECYCLE_CLOSING, LIFECYCLE_CLOSED)
ENDING = (LIFECYCLE_CLOSING, LIFECYCLE_CLOSED)

# 영속 생애주기 필드는 core.world_state.LIFECYCLE_FIELDS(단일 출처)
LIFECYCLE_FIELDS = _ws.LIFECYCLE_FIELDS

from .player_world import REQUIRED_CAPABILITIES  # noqa: E402 — 단일 출처(H05 세계 모드와 같은 집합)

INIT_OK = "INITIALIZED"
INIT_ALREADY = "ALREADY_INITIALIZED"
INIT_LEGACY = "LEGACY"                # DYNAMIC 대상 아님 — 세계 미초기화, 기존 경로
INIT_FAILED = "FAILED"                # 대상이었으나 초기화 실패 — 정본·바인딩 불변(LEGACY 유지)


class WorldLifecycleError(RuntimeError):
    pass


# ══════════════════════════════════════════════════════════════
#  자격 · 정의
# ══════════════════════════════════════════════════════════════

def player_uids(session) -> list:
    return sorted((getattr(session, "players", None) or {}), key=str)


def pc_actor_id(uid) -> str:
    return f"pc_{uid}"


def world_bound(session) -> bool:
    """동적 세계가 초기화된(또는 열린·닫힌) 세션인가 — 다인 참가·레거시 전환을 막는 근거.

    R2: CLOSED는 LEGACY 세션도 갖는다 — genesis 정체성이 있는 세션만 동적 세계 바인딩이다.
    """
    rec = getattr(session, "world_initialization", None) or {}
    return getattr(session, "world_lifecycle", None) in LIFECYCLES and bool(rec.get("genesis_version_id"))


def is_closed(session) -> bool:
    return getattr(session, "world_lifecycle", None) == LIFECYCLE_CLOSED


def is_ending(session) -> bool:
    """종료 진행 중(CLOSING) 또는 종료(CLOSED) — 턴·참가·재개를 모두 막는다(LEGACY·DYNAMIC 공통)."""
    return getattr(session, "world_lifecycle", None) in ENDING


def joins_blocked(session) -> bool:
    """추가 참가 금지 — 동적 세계 바인딩(단일 PC) 또는 종료 진행·종료."""
    return world_bound(session) or is_ending(session)


def init_failed(session) -> bool:
    """DYNAMIC 대상이었으나 초기화가 실패해 재시도를 기다리는 상태(fail-closed — LEGACY 진행 금지)."""
    rec = getattr(session, "world_initialization", None) or {}
    return getattr(session, "world_lifecycle", None) is None and rec.get("status") == "FAILED"


def compile_candidate(session):
    """(정의 dict | None, reason). DYNAMIC 후보 = 단일 PC + 컴파일 성공 + 필수 capability 검증.

    R2: 이전 시도가 이미 durable 정의 스냅샷을 남겼다면 그것을 쓴다 — 현재 원본으로 다시 컴파일해
    스냅샷을 조용히 교체하지 않는다(스냅샷이 손상이면 후보가 아니라 실패로 남는다).
    """
    if world_bound(session) or is_ending(session):
        return None, "already_bound"
    if len(player_uids(session)) != 1:
        return None, "not_single_pc"
    import os
    from . import definition_snapshot as _snap
    if os.path.exists(_snap.snapshot_path(session.session_id)):
        try:
            definition = _snap.read_snapshot(session.session_id).definition
        except _snap.DefinitionSnapshotError as e:
            return None, f"snapshot_unreadable:{type(e).__name__}"
    else:
        from . import definition_compiler as _dc
        try:
            res = _dc.compile_scenario(getattr(session, "scenario_data", {}) or {},
                                       getattr(session, "scenario_id", None) or "scenario")
        except Exception as e:  # noqa: BLE001 — 레거시 시나리오는 컴파일 실패해도 LEGACY로 정상 진행
            return None, f"compile_error:{type(e).__name__}"
        if not res.ok:
            return None, "compile_failed"
        definition = res.definition
    caps = set((definition.get("scenario_meta") or {}).get("validated_capabilities") or ())
    if not set(REQUIRED_CAPABILITIES) <= caps:
        return None, "capability_absent"
    return definition, "ok"


def is_dynamic_candidate(session) -> bool:
    return compile_candidate(session)[0] is not None


def bind_definition(session, compiled) -> str:
    """durable 정의 스냅샷을 쓰고(같은 revision이면 멱등) 세션 definition_revision을 묶는다.

    유일한 바인딩 writer. 다른 revision 스냅샷이 이미 있으면 DefinitionSnapshotConflict(덮어쓰지 않는다 —
    정의 교체는 명시적 migration). 세션 저장은 INITIALIZATION 커밋의 strict 저장이 함께 한다.
    """
    from . import definition_snapshot as _snap
    cur = getattr(session, "definition_revision", None)
    snap = _snap.write_snapshot(session.session_id, compiled,
                                source={"scenario_id": getattr(session, "scenario_id", None)})
    if cur is not None and cur != snap.definition_revision:
        raise WorldLifecycleError(f"세션이 이미 다른 정의 {cur}에 묶여 있습니다")
    session.definition_revision = snap.definition_revision
    return snap.definition_revision


# ══════════════════════════════════════════════════════════════
#  시작 선택 → StartChoices (결정적 해석, 산문 추측 없음)
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class StartChoices:
    start_place: str
    start_minute: int
    knowledge_seed: str | None = None
    frame_id: str | None = None

    def to_dict(self) -> dict:
        return {"start_place": self.start_place, "start_minute": self.start_minute,
                "knowledge_seed": self.knowledge_seed, "frame_id": self.frame_id}


def resolve_start_choices(definition: dict, chosen: dict):
    """선택된 시작 틀 → (StartChoices | None, reason).

    시작 장소·시각은 그 틀에 사람이 저작한 world_start(컴파일러 검증)에서만 온다. 틀의 facts(슬롯 템플릿)를
    기계 시각·위치로 해석하지 않는다(DESIGN 08 §20). world_start가 없으면 None — 세션은 LEGACY로 남는다.
    """
    fid = (chosen or {}).get("id")
    frame = next((f for f in (definition.get("initialization_definitions") or {}).get("start_frames") or ()
                  if f.get("frame_id") == fid), None)
    if frame is None:
        return None, "start_frame_unknown"
    ws = frame.get("world_start")
    if not ws:
        return None, "world_start_unauthored"
    return StartChoices(ws["place"], ws["minute_index"], ws.get("knowledge_seed"), fid), "ok"


def initialization_id_for(session_id, revision: str, choices: StartChoices) -> str:
    return "init_" + stable_digest("world.initialization.v1", str(session_id), revision,
                                   json.dumps(choices.to_dict(), sort_keys=True, ensure_ascii=False))


# ══════════════════════════════════════════════════════════════
#  InitialWorldPlan (순수)
# ══════════════════════════════════════════════════════════════

@dataclass
class InitialWorldPlan:
    status: str                       # READY | CONFLICT
    ops: tuple = ()
    diagnostics: list = field(default_factory=list)
    projected_world: object = None
    reason: str | None = None


def _mk(prefix, name, op_type, payload):
    return _wm.make_op(f"{prefix}.{name}", op_type, "INITIALIZATION", payload)


def build_initial_world_plan(definition: dict, session, choices: StartChoices, *,
                             initialization_id: str, narrative_seed: str) -> InitialWorldPlan:
    """정의 + 시작 선택 → INITIALIZATION typed 연산(세션 불변)."""
    from . import player_world as _pw
    from . import world_progression as _wp
    uids = player_uids(session)
    if len(uids) != 1:
        return InitialWorldPlan("CONFLICT", reason="not_single_pc")
    uid = uids[0]
    pc = pc_actor_id(uid)
    name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name") or "PC"
    prefix = "h06i." + stable_digest("world.initialization.ops.v1", initialization_id, length=10)
    clock_op = _mk(prefix, "clock", _sched.OP_CLOCK_INITIALIZE, {"minute_index": choices.start_minute})
    # R2-05: 저작 행위자를 같은 INITIALIZATION 묶음에서 결정적으로 등록한다(프로필 복제·위치 추론 없음)
    authored = []
    for aid in sorted(definition.get("actor_definitions") or {}):
        adef = definition["actor_definitions"][aid]
        if adef.get("kind") != "NPC":
            continue
        authored.append(_mk(prefix, f"npc.{aid}", "ACTOR_REGISTER", {
            "actor_id": aid, "kind": "NPC", "display_name": adef.get("display_name") or aid,
            "source_kind": "AUTHORED_NPC", "source_key": adef.get("legacy_key"),
            "aliases": list(adef.get("aliases") or [])}))
    base = authored + [
        _mk(prefix, "pc", "ACTOR_REGISTER", {"actor_id": pc, "kind": "PC", "display_name": name,
                                            "source_kind": "PLAYER", "source_key": str(uid)}),
        _mk(prefix, "place", _sp.OP_ACTOR_PLACEMENT_SET, {"actor_id": pc, "place_id": choices.start_place}),
        _mk(prefix, "scene", _sp.OP_SCENE_PLACE_SET, {"place_id": choices.start_place}),
    ]
    know = []
    rec = {}
    if choices.knowledge_seed:
        rec = _sp.seed_knowledge(definition, choices.knowledge_seed)
        for pid, f in rec["places"].items():
            know.append(_mk(prefix, f"kp.{pid}", _sp.OP_PLACE_KNOWLEDGE_UPSERT,
                            {"actor_id": pc, "place_id": pid, "fields": f}))
        for eid, f in rec["edges"].items():
            know.append(_mk(prefix, f"ke.{eid}", _sp.OP_EDGE_KNOWLEDGE_UPSERT,
                            {"actor_id": pc, "edge_id": eid, "fields": f}))
        for ref, f in rec["access"].items():
            know.append(_mk(prefix, f"ka.{ref}", _sp.OP_ACCESS_KNOWLEDGE_UPSERT,
                            {"actor_id": pc, "ref": ref, "fields": f}))
    # H07: 저작 NPC의 초기 공간 지식(knowledge_seed) — NPC 이동 계획의 근거(행위자 지식 기반)
    for aid in sorted(definition.get("actor_definitions") or {}):
        sid = (definition["actor_definitions"][aid] or {}).get("knowledge_seed")
        if not sid:
            continue
        nrec = _sp.seed_knowledge(definition, sid)
        for pid, f in nrec["places"].items():
            know.append(_mk(prefix, f"nkp.{aid}.{pid}", _sp.OP_PLACE_KNOWLEDGE_UPSERT,
                            {"actor_id": aid, "place_id": pid, "fields": f}))
        for eid, f in nrec["edges"].items():
            know.append(_mk(prefix, f"nke.{aid}.{eid}", _sp.OP_EDGE_KNOWLEDGE_UPSERT,
                            {"actor_id": aid, "edge_id": eid, "fields": f}))
        for ref, f in nrec["access"].items():
            know.append(_mk(prefix, f"nka.{aid}.{ref}", _sp.OP_ACCESS_KNOWLEDGE_UPSERT,
                            {"actor_id": aid, "ref": ref, "fields": f}))
    visit = [_mk(prefix, "visit", _sp.OP_PLACE_KNOWLEDGE_UPSERT, {
        "actor_id": pc, "place_id": choices.start_place,
        "fields": {"knows_name": True, "visited": True}})]
    # H07: 시작 장소에서 보이는 출구(PUBLIC·LOCAL)와 그 너머 장소 이름 — 도착 규칙과 같다
    view = _pw.local_view_ops(f"{prefix}.view", definition, None, [pc], choices.start_place,
                              knowledge={"places": dict(rec.get("places") or {}, **{choices.start_place: {}}),
                                         "edges": rec.get("edges") or {}})
    # ① 시계만 먼저 투영(진행 엔진은 초기화된 시계 위에서만 돈다)
    clock_batch = _wm.WorldMutationBatch(batch_id=f"{prefix}.clock", outer_origin=_ws.CAUSE_INITIALIZATION,
                                         source_world_version_id=None, definition_revision=None)
    clock_batch.add(clock_op)
    clock_batch.freeze()
    pr = _wm.project(_wm.ProjectedWorld.capture(session), clock_batch, _wm.PRODUCTION_REGISTRY)
    if not pr.ok:
        return InitialWorldPlan("CONFLICT", reason=_wm.WORLD_PROJECTION_CONFLICT,
                                diagnostics=[c.message for c in pr.conflicts])
    w1 = pr.world
    wake = _wp.initial_template_wakeups(definition, w1, narrative_seed=narrative_seed, op_prefix=f"{prefix}.wk")
    base_ops = _pw.chain(base, know, visit, view, wake)
    # ② 시작 분에서 전수 검사 1회 — 성립하는 spawn·정보 계열만(정규 진행 규칙 그대로)
    r = _wp.project_world_progression(definition, w1, choices.start_minute, base_ops=base_ops,
                                      progress={"observer_actor": pc, "scene_place": choices.start_place},
                                      narrative_seed=narrative_seed, outer_origin=_ws.CAUSE_INITIALIZATION,
                                      initial_full_scan=True)
    if not r.committable:
        return InitialWorldPlan("CONFLICT", reason=r.status, diagnostics=list(r.diagnostics))
    if _pw.base_rejected(r.diagnostics):
        rejected = [x for x in r.diagnostics if x.get("code") == "UNIT_REJECTED"]
        # 진행 엔진은 거부된 단위를 진단으로만 남긴다 — 초기 세계의 기반 연산은 전부 적용돼야 한다
        return InitialWorldPlan("CONFLICT", reason="BASE_REJECTED", diagnostics=rejected)
    ops = [clock_op] + _pw.namespace_ops(r.ops, f"{prefix}.p", [clock_op.op_id])
    return InitialWorldPlan("READY", tuple(ops), list(r.diagnostics), r.projected_world)


# ══════════════════════════════════════════════════════════════
#  INITIALIZATION 커밋 (생애주기 멱등)
# ══════════════════════════════════════════════════════════════

@dataclass
class InitResult:
    outcome: str
    reason: str = ""
    initialization_id: str | None = None
    mutation: object = None


def _restore(session, saved):
    for k, v in saved.items():
        setattr(session, k, v)


async def initialize_world(bot, session, chosen: dict) -> InitResult:
    """시작 선택 후 DYNAMIC 세션의 정의 바인딩 + genesis.

    INIT_LEGACY: DYNAMIC 대상이 아니다(아무것도 바꾸지 않음 — 기존 LEGACY 시작).
    INIT_FAILED: 대상이었으나 실패 — 정본·바인딩은 되돌리고 실패 표식(world_initialization.status=FAILED)을
                 strict 저장한다. 이 표식이 있는 동안 LEGACY 진행·인트로는 막힌다(재시도 가능).
    """
    if world_bound(session):
        rec = getattr(session, "world_initialization", None) or {}
        return InitResult(INIT_ALREADY, initialization_id=rec.get("initialization_id"))
    definition, why = compile_candidate(session)
    if definition is None:
        if why.startswith("snapshot_unreadable"):
            return await _fail(bot, session, why)
        return InitResult(INIT_LEGACY, why)
    choices, why = resolve_start_choices(definition, chosen)
    if choices is None:
        if init_failed(session):
            return await _fail(bot, session, why)        # 이미 DYNAMIC 대상으로 실패한 세션은 LEGACY로 내리지 않는다
        return InitResult(INIT_LEGACY, why)
    saved = {k: copy.deepcopy(getattr(session, k, None))
             for k in ("definition_revision", "world_lifecycle", "world_initialization")}
    try:
        rev = bind_definition(session, definition)
    except Exception as e:  # noqa: BLE001 — 바인딩 실패 = 초기화 안 함(정본 불변)
        _restore(session, saved)
        return await _fail(bot, session, f"bind:{type(e).__name__}: {e}")
    init_id = initialization_id_for(session.session_id, rev, choices)
    seed = getattr(session, "narrative_seed", None) or _ws.derive_narrative_seed(session.session_id)
    plan = build_initial_world_plan(definition, session, choices, initialization_id=init_id,
                                    narrative_seed=seed)
    if plan.status != "READY":
        _restore(session, saved)
        return await _fail(bot, session, f"plan:{plan.reason}", init_id)
    batch = _cm.new_batch(session, cause_kind=_ws.CAUSE_INITIALIZATION, cause_id=init_id)
    for op in plan.ops:
        batch.add(op)
    # R2-04: genesis 정체성은 원인·기준에서 결정적으로 계산된다 — 첫 strict 저장에 이미 들어간다
    genesis = _ws.next_world_version(session.session_id, copy.deepcopy(getattr(session, "world_version", None)),
                                     cause_kind=_ws.CAUSE_INITIALIZATION, cause_id=init_id)
    session.world_lifecycle = LIFECYCLE_INITIALIZED
    session.world_initialization = {"initialization_id": init_id,
                                    "mutation_id": _cm.mutation_id_for(session.session_id,
                                                                       _ws.CAUSE_INITIALIZATION, init_id),
                                    "definition_revision": rev, "start_choices": choices.to_dict(),
                                    "genesis_version_id": genesis["version_id"]}
    mres = await _cm.CanonicalMutationCoordinator(bot).apply(
        session, batch, cause_kind=_ws.CAUSE_INITIALIZATION, cause_id=init_id)
    if mres.outcome not in (_cm.OUTCOME_COMMITTED, _cm.OUTCOME_RECOVERY_PENDING):
        _restore(session, saved)
        return await _fail(bot, session, f"commit:{mres.outcome}:{mres.reason}", init_id, mres)
    if _ws.version_id_of(mres.target_world_version) != genesis["version_id"]:   # 계산과 coordinator가 같아야 한다
        raise WorldLifecycleError("genesis world_version 정체성 불일치")
    # 바인딩 세션은 이후 정의 원문(스냅샷)으로 진행 — 재시작과 같은 의미
    session.scenario_data = copy.deepcopy(definition["legacy_source"]["scenario"])
    return InitResult(INIT_OK, initialization_id=init_id, mutation=mres)


async def _fail(bot, session, reason: str, init_id=None, mutation=None) -> InitResult:
    """초기화 실패 표식을 strict 저장한다(LEGACY 진행·인트로를 막는 fail-closed 근거)."""
    from . import io as _io
    session.world_lifecycle = None
    session.world_initialization = {"status": "FAILED", "reason": str(reason)[:500], "initialization_id": init_id}
    try:
        await _io.save_session_data_strict(bot, session)
    except Exception as e:  # noqa: BLE001 — 표식은 메모리에 남아 이 프로세스의 진행을 막는다
        print(f"[H06] 초기화 실패 표식 저장 실패(메모리 표식 유지): {e}")
    return InitResult(INIT_FAILED, str(reason), init_id, mutation)


def mark_open(session) -> bool:
    """인트로 완료 → OPEN(메모리만). 영속이 필요한 호출자는 open_durable을 쓴다."""
    if getattr(session, "world_lifecycle", None) == LIFECYCLE_INITIALIZED:
        session.world_lifecycle = LIFECYCLE_OPEN
        return True
    return False


async def open_durable(bot, session) -> bool:
    """R2-02: INITIALIZED → OPEN을 strict 저장으로 확정한다. 실패하면 INITIALIZED로 되돌리고 False.

    LEGACY(생애주기 없음) 세션은 전이가 없으므로 True. 이미 OPEN이면 True.
    """
    from . import io as _io
    lc = getattr(session, "world_lifecycle", None)
    if lc is None:
        return not init_failed(session)
    if lc == LIFECYCLE_OPEN:
        return True
    if lc != LIFECYCLE_INITIALIZED:
        return False
    session.world_lifecycle = LIFECYCLE_OPEN
    try:
        await _io.save_session_data_strict(bot, session)
    except Exception as e:  # noqa: BLE001
        session.world_lifecycle = LIFECYCLE_INITIALIZED
        print(f"[H06] OPEN 영속 실패 — INITIALIZED 유지: {e}")
        return False
    return True


def blocked_reason(session) -> str | None:
    """동적 세계 세션이 자동 턴을 받을 수 없는 사유(운영자 표시용). None = 막지 않음."""
    lc = getattr(session, "world_lifecycle", None)
    if lc is None:
        if init_failed(session):
            rec = getattr(session, "world_initialization", None) or {}
            return f"동적 세계 초기화가 실패했습니다({rec.get('reason')}) — 재시도가 필요합니다"
        return None
    if lc == LIFECYCLE_INITIALIZED:
        return "세계가 초기화됐지만 세션이 아직 열리지 않았습니다(캐시·인트로 대기)"
    if lc == LIFECYCLE_CLOSED:
        return "세션이 종료됐습니다"
    if lc == LIFECYCLE_CLOSING:
        return "세션 종료가 진행 중입니다(운영자 재시도로 완료)"
    from . import player_world as _pw
    ctx = _pw.session_world_context(session)
    if ctx.mode == _pw.MODE_BLOCKED:
        return f"동적 세계 정의를 읽을 수 없습니다({ctx.reason}) — 운영자 확인 필요"
    return None


# ══════════════════════════════════════════════════════════════
#  재시작 복구 (H06-D)
# ══════════════════════════════════════════════════════════════

def restore(session) -> dict:
    """재시작 — 미종결 INITIALIZATION·ADMIN_EDIT 저널 종결 + 바인딩 정의 검증. 정본을 바꾸지 않는다.

    · 저널 처분은 canonical_mutation.recover(이미 영속된 world_version이 정본) — 재적용·재생성 없음.
    · 오프라인 시간 진행·예약 따라잡기 없음(같은 world_clock에서 재개).
    · 바인딩 스냅샷이 없거나 손상이면 BLOCKED(운영자 표시) — 현재 원본으로 재컴파일하지 않는다.
    """
    from . import definition_snapshot as _snap
    out = {"journal": _cm.recover(session), "definition": "UNBOUND", "lifecycle": getattr(session, "world_lifecycle", None)}
    rev = getattr(session, "definition_revision", None)
    if rev:
        try:
            _snap.read_snapshot(session.session_id, expected_revision=rev)
            out["definition"] = "OK"
        except _snap.DefinitionSnapshotError as e:
            out["definition"] = f"BLOCKED:{type(e).__name__}"
    return out


# ══════════════════════════════════════════════════════════════
#  세션 종료 · 환급 안전 지점 (H06-G)
# ══════════════════════════════════════════════════════════════

def close_blocked_reason(session) -> str | None:
    """종료를 막는 활성 작업. 진행 중인 턴을 강제 취소하지 않는다 — 끝날 때까지 거부/연기.

    TurnPreparation/CommitCoordinator(비종결 턴 — RETRY_PENDING 포함) · 커밋 복구 · 재서술/되감기 ·
    미종결 INITIALIZATION/ADMIN_EDIT(정의 migration 포함) · 턴 처리 중.
    """
    from . import turn_history as _th
    from . import turn_transaction as _tx
    tx = _tx.get_active_transaction(session)
    if tx is not None and not _tx.is_terminal(tx.status):
        return "진행 중인 턴이 있습니다(재시도 대기 포함) — 턴이 끝난 뒤 다시 시도하십시오"
    if getattr(session, "is_processing", False):
        return "턴 처리 중입니다"
    if getattr(session, "commit_recovery", None):
        return "커밋 복구가 끝나지 않았습니다"
    if _th.read_op(session.session_id):
        return "재서술·되감기가 진행 중입니다"
    if _cm.has_unfinished(session):
        return "미종결 세계 변이(초기화·관리자 편집)가 있습니다"
    return None


async def close_session(bot, session, *, reason: str) -> dict:
    """운영자 세션 종료 — 생애주기 + 캐시/재무 작업(세계 변이 아님). LEGACY·DYNAMIC 모두 CLOSED가 된다.

    1 정본 직렬화 락(배타) → 2 안전 지점 검사 → 3 CLOSING strict 영속(이후 모든 진행·참가·재개 차단)
    → 4·5 캐시 생애주기 단일 finalizer(실제 보관비 정산 · 미사용 선불 환급 정확히 한 번 · 초과분 운영자 부담)
    → 6 CLOSED strict 영속 → 8 활성 등록 제거. (7 보고·채널 잠금은 호출자.)
    · CLOSING 진입 저장 실패: 환급 전이므로 이전 상태로 안전 복귀.
    · finalize 뒤 CLOSED 저장 실패: durable CLOSING 유지 — OPEN으로 되돌리지 않는다(재시도·재시작이 완성).
    · 재시도는 같은 finalizer의 멱등성으로 환급·finalize를 반복하지 않는다.
    세계(시계·사건·예약·공간·인지·정보)·게임 이력은 바꾸지 않는다.
    """
    from .commit_coordinator import commit_serialization_lock
    async with commit_serialization_lock(session.session_id):
        lc = getattr(session, "world_lifecycle", None)
        if lc not in ENDING:
            why = close_blocked_reason(session)
            if why:
                return {"ok": False, "reason": why}
            from . import io as _io
            session.world_lifecycle = LIFECYCLE_CLOSING
            try:
                await _io.save_session_data_strict(bot, session)
            except Exception as e:  # noqa: BLE001 — 환급 시작 전: 안전 복귀
                session.world_lifecycle = lc
                return {"ok": False, "reason": f"CLOSING 영속 실패(재시도): {type(e).__name__}"}
        return await _complete_closing_locked(bot, session, reason=reason)


async def _complete_closing_locked(bot, session, *, reason: str) -> dict:
    """CLOSING(또는 CLOSED) 세션의 종료를 완성한다 — 기존 finalizer 재사용, CLOSED strict 영속만 추가."""
    from . import cache_lifecycle as _cl
    from . import io as _io
    res = {"storage_krw": 0.0, "refund": {}, "settled": True, "remote": None}
    if getattr(session, "cache_name", None) or _cl.view(session.session_id).open_window_id():
        res = await _cl.close_window(bot, session, reason=reason, disposition=_cl.WINDOW_SETTLE_REFUND)
    if not res.get("settled", True) and _cl.view(session.session_id).open_window_id():
        return {**res, "ok": False, "reason": "환급 기록이 완료되지 않았습니다(재시도)"}
    if getattr(session, "world_lifecycle", None) != LIFECYCLE_CLOSED:
        session.world_lifecycle = LIFECYCLE_CLOSED
        try:
            await _io.save_session_data_strict(bot, session)
        except Exception as e:  # noqa: BLE001 — finalize 뒤: durable CLOSING 유지(OPEN 복귀 금지)
            session.world_lifecycle = LIFECYCLE_CLOSING
            return {**res, "ok": False, "reason": f"CLOSED 영속 실패(재시도): {type(e).__name__}"}
    deactivate(bot, session)
    return {**res, "ok": True}


async def recover_closing(bot, session) -> dict:
    """재시작 — durable CLOSING 세션의 종료를 완성한다(활성 등록 전). 환급·finalize는 멱등 재사용."""
    from . import cache_lifecycle as _cl
    from .commit_coordinator import commit_serialization_lock
    async with commit_serialization_lock(session.session_id):
        return await _complete_closing_locked(bot, session, reason=_cl.REASON_OPERATOR_END)


def deactivate(bot, session) -> list:
    """종료된 세션을 활성 게임 표면(active_sessions의 game/master/display 매핑)에서 내린다."""
    reg = getattr(bot, "active_sessions", None)
    if not isinstance(reg, dict):
        return []
    gone = [k for k, v in list(reg.items()) if v is session]
    for k in gone:
        reg.pop(k, None)
    return gone


def apply_compat_view(session, chosen: dict) -> dict:
    """R2-03: DYNAMIC 세션의 레거시 표시값(world_timeline)을 정본 genesis에서 파생한다.

    장소·날짜·시간대는 정본(spatial_state·world_clock)에서만 온다 — 시작 틀 facts의 장소·시간은 쓰지 않는다.
    정본 세계 모델에 없는 세력 표시(faction/player_faction)만 기존 의미대로 옮긴다(충돌할 정본이 없다).
    """
    from . import player_world as _pw
    from . import turn_world as _tw
    rec = getattr(session, "world_initialization", None) or {}
    definition = _pw.load_definition(session, rec.get("definition_revision") or getattr(session, "definition_revision", None))
    pc = pc_actor_id(player_uids(session)[0]) if len(player_uids(session)) == 1 else None
    tl = dict(getattr(session, "world_timeline", {}) or {})
    for k in ("current_location", "current_date", "time_of_day"):
        tl.pop(k, None)
    if definition is not None and pc:
        tl = _tw.mirror_timeline(tl, definition, session, pc)
    facts = (chosen or {}).get("facts") or {}
    if facts.get("faction"):
        tl["faction_context"] = facts["faction"]
    if facts.get("player_faction"):
        session.player_faction = facts["player_faction"]
    session.world_timeline = tl
    return tl
