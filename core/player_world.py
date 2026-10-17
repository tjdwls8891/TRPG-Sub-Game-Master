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
from . import place_display as _pd
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_progression as _wp
from .world_ids import stable_digest

MODE_LEGACY, MODE_DYNAMIC = "LEGACY", "DYNAMIC"
MODE_BLOCKED = "BLOCKED"           # H06: 동적 세계가 초기화된 세션인데 바인딩 정의를 읽을 수 없음 — 턴 거부(LEGACY 대체 금지)
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
OUT_ACTION_DONE = "ACTION_DONE"                  # P1(D1): 이동·대기 아닌 행동의 소요 시간 진행
OUT_ACTION_INTERRUPTED = "ACTION_INTERRUPTED"

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
    """바인딩된 정의 — 세션 정의 스냅샷만 읽는다(H06). 실패 시 None.

    현재 원본 파일·scenario_data 재컴파일로 대체하지 않는다(원본 drift가 진행 중 세션의 의미를 바꾸지 못한다).
    """
    key = (getattr(session, "session_id", None), revision)
    if key in _DEF_CACHE:
        return _DEF_CACHE[key]
    from . import definition_snapshot as _snap
    try:
        d = _snap.read_snapshot(session.session_id, expected_revision=revision).definition
    except Exception:
        return None
    _DEF_CACHE[key] = d
    return d


def pc_actor_for(uid) -> str:
    return f"pc_{uid}"


def session_world_context(session, world=None) -> WorldContext:
    """세션의 세계 모드. DYNAMIC 조건을 하나라도 못 채우면 LEGACY(사유 기록).

    H06: DYNAMIC = 단일 PC + 정의 바인딩(스냅샷) + 필수 capability + 생애주기 OPEN + 초기화된 시계 + PC 배치.
    동적 세계가 초기화된 세션에서 정의를 읽을 수 없으면 BLOCKED(조용히 LEGACY로 넘어가지 않는다).
    """
    from . import world_lifecycle as _lc
    rev = getattr(session, "definition_revision", None)
    bound = _lc.world_bound(session)
    if not rev:
        return WorldContext(MODE_BLOCKED if bound else MODE_LEGACY, "definition_unbound")
    if len(getattr(session, "players", None) or {}) != 1:
        return WorldContext(MODE_BLOCKED if bound else MODE_LEGACY, "not_single_pc", definition_revision=rev)
    definition = load_definition(session, rev)
    if definition is None:
        return WorldContext(MODE_BLOCKED if bound else MODE_LEGACY, "definition_unavailable",
                            definition_revision=rev)
    if getattr(session, "world_lifecycle", None) != _lc.LIFECYCLE_OPEN:
        return WorldContext(MODE_LEGACY, "lifecycle_not_open", definition, rev)
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
        # H06 R2-06: TRANSIT도 동적 세계 정본 공간 상태다(LEGACY로 강등하지 않는다)
        if aid in (reg or {}) and isinstance(pl, dict) and pl.get("kind") in (_sp.AT_PLACE, _sp.TRANSIT):
            pcs.append(aid)
            uid_of[aid] = uid
    if not pcs:
        return WorldContext(MODE_LEGACY, "pc_unplaced", definition, rev)
    return WorldContext(MODE_DYNAMIC, "ok", definition, rev, tuple(pcs), pcs[0], uid_of)


def legacy_narrative_retired(session) -> bool:
    """H07-C: 동적 세계에 바인딩된 세션(정의 revision 바인딩 또는 세계 생애주기 초기화 — DYNAMIC·BLOCKED)에서는
    레거시 서사 권위(퀘스트 선택·진행·intended_case·info_ledger·hidden/known_secrets·정적 상주 동행 해제)를 은퇴한다.
    권위는 사건 상태기계 + 정보/인지 + 정본 세계다. 바인딩이 없는 LEGACY 세션(4개 시나리오·현 운영)은 그대로다."""
    from . import world_lifecycle as _lc
    return bool(getattr(session, "definition_revision", None)) or bool(_lc.world_bound(session))


def pc_stats(session, uid) -> dict:
    """H07: 관찰자 PC 능력치(프로필의 정수 항목) — 레거시 min_stat 조건 의미 보존용 진행 문맥."""
    prof = ((getattr(session, "players", None) or {}).get(uid) or {}).get("profile") or {}
    out = {}
    for k, v in (prof.items() if isinstance(prof, dict) else ()):
        try:
            if isinstance(v, bool):
                continue
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def pc_capabilities(session, uid, definition=None) -> tuple:
    """PC capability = 그 PC가 가진 수량 > 0 소지품의 보유 이름(접근 규칙 requires_capability·OBSERVER_HAS_ITEM과 대조).

    v7.17: 원문 키 ∪ 정규 종류(예: '항생제 두 알' → 항생제) ∪ 저작 범주 — 보유(HAS)만 뜻한다(사용·지급과 다름, R2).
    이름 부분 문자열로 넓히지 않는다(C10).
    """
    from . import items as _items
    name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
    bag = ((getattr(session, "resources", None) or {}).get(name) or {}) if name else {}
    owned = [r.get("kind") for r in (getattr(session, "item_instances", None) or {}).values()
             if name and r.get("owner") == name and r.get("kind")]
    return tuple(sorted(set(_items.capabilities(bag, _items.definitions_of(definition))) | set(owned)))


def faction_access_capabilities(definition, world) -> tuple:
    """v7.17.2: 정본 소속 → 접근 capability(시나리오 저작 faction_capabilities가 있을 때만).

    소속 근거 = RESOLVED 사건 인스턴스의 지역 상태 grant_faction 값 '전부'(최근 하나만 쓰는 표시 뷰
    derived_player_faction·session.player_faction 미러가 아니다). 관계·프로필 서술·소지품 이름으로 추론하지 않는다.
    """
    mapping = (definition or {}).get("faction_capabilities") or {}
    if not mapping:
        return ()
    ev = (world.domain("event_instances") if isinstance(world, _wm.ProjectedWorld)
          else getattr(world, "event_instances", None)) or {}
    out = set()
    for inst in ev.values():
        if not isinstance(inst, dict) or inst.get("lifecycle") != "RESOLVED":
            continue
        f = (inst.get("local_state") or {}).get("grant_faction")
        if isinstance(f, str) and f in mapping:
            out.update(mapping[f])
    return tuple(sorted(out))


def pc_access_capabilities(session, uid, definition=None, world=None) -> tuple:
    """접근 판정용 capability = (소지품 capability − 소속 전용 토큰) ∪ 정본 소속 capability.

    출처 분리(v7.17.3): 시나리오가 faction_capabilities 값으로 선언한 토큰은 정본 소속(RESOLVED 사건 grant_faction)
    에서 파생될 때만 인정한다 — 같은 이름의 물건을 들고 있어도 소속 권한이 되지 않는다. 매핑이 없으면 보호 집합은
    비어 기존 아이템 접근 의미 그대로. 사건 조건의 보유(OBSERVER_HAS_ITEM) 입력에는 쓰지 않는다(실물 보유는 그대로).
    """
    protected = {c for caps in ((definition or {}).get("faction_capabilities") or {}).values() for c in caps}
    items = set(pc_capabilities(session, uid, definition)) - protected
    return tuple(sorted(items | set(faction_access_capabilities(definition, world if world is not None else session))))


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


def route_option(c: _mv.KnownRouteCandidate, *, definition=None, knowledge=None, best=None, n=1) -> dict:
    """선택지(플레이어 지식만). signature가 의미 정체성 — option_id는 transient.

    H07 §5: definition·knowledge가 주어지면 라벨을 PC 지식 수준 표시명으로 만든다(모르는 장소 이름 비노출)."""
    label = c.route_label
    if definition is not None and knowledge is not None:
        label = _pd.route_label(definition, knowledge, c.edge_ids, best if best is not None else c.edge_ids,
                                c.place_ids[0] if c.place_ids else None, n)
    return {"signature": list(c.signature), "label": label,
            "minutes": c.perceived_duration_minutes,
            "known_constraints": [dict(k) for k in c.known_constraints]}


def choose_route(plan: _mv.KnownRoutePlan, *, chosen_signature=None,
                 policy: RouteChoicePolicy = DEFAULT_ROUTE_POLICY, definition=None, knowledge=None):
    """(decision, candidate|None, options). decision = AUTO|CHOSEN|ASK|NO_ROUTE|SAME_PLACE."""
    if plan.status == _mv.SAME_PLACE:
        return "SAME_PLACE", None, ()
    if plan.status != _mv.ROUTE_OK or not plan.candidates:
        return "NO_ROUTE", None, ()
    cands = list(plan.candidates)[:policy.max_displayed]
    best = cands[0].edge_ids
    options = tuple(route_option(c, definition=definition, knowledge=knowledge, best=best, n=i)
                    for i, c in enumerate(cands, 1))
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
    definition_revision: str | None = None          # 계획을 만든 정의 revision(R2 — 준비 기준선과 대조)
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
    item_payments: list = field(default_factory=list)   # v7.17(R4): 통과한 확인 행동의 실제 지급 {capability, quantity, action_id}
    observer: dict = field(default_factory=dict)        # P1: 계획 진행이 쓴 관찰자 문맥 — 묘사 후 보충 진행도 같은 기준선

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

    def __init__(self, definition, world, *, seed, tie_orders, prefix, outer_origin="PLAYER_TURN", observer=None):
        self.definition = definition
        self.observer = dict(observer or {})      # H07: 관찰자 PC 소지품·능력치(진행 문맥 — 조건 의미 보존)
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
                                          progress=dict(progress, **self.observer), narrative_seed=self.seed,
                                          resolved_tie_orders=self.tie_orders, outer_origin=self.outer_origin)
        self.diags += r.diagnostics
        if base_rejected(r.diagnostics):
            # H06 R2-07: 플레이어 행동의 필수 연산(base 단위)이 거부되면 버린 채 계속하지 않는다 — 결정적 충돌
            self.stop = (PLAN_CONFLICT, "BASE_REJECTED")
            return None
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


def base_rejected(diagnostics) -> bool:
    """진행 엔진이 기반 연산 단위(base)를 거부해 버렸는가(UNIT_REJECTED). 조용한 탈락을 막는 판정."""
    return any(isinstance(d, dict) and d.get("code") == "UNIT_REJECTED" and d.get("unit_id") == "base"
               for d in diagnostics or ())


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


LOCAL_VIEW_SOURCE = "LOCAL_VIEW"


def local_view_ops(prefix, definition, world, actors, place_id, *, knowledge=None) -> list:
    """H07: 장소에 있는 행위자가 그 자리에서 보는 출구(PUBLIC·LOCAL 구간)와 그 너머 장소(존재·표시명)를 안다.

    RESTRICTED·SECRET 구간은 보이지 않는다(저작 분류). 실제 접근 상태·구간 성질은 알려 주지 않는다 —
    연결의 존재만(connection_known). 이미 아는 것은 다시 쓰지 않는다(world가 주어질 때).
    """
    if not definition or not place_id:
        return []
    edges = definition.get("movement_edges") or {}
    out = []
    for n, actor in enumerate(actors):
        know = knowledge if knowledge is not None else (_sp.actor_knowledge(world, actor) if world is not None else {})
        for eid in sorted(edges):
            e = edges[eid]
            if e.get("from") != place_id or e.get("knowledge_visibility") not in _sp.FAMILIARITY_VISIBILITY:
                continue
            if not _sp.knows_edge(know, eid):
                out.append(_mk(prefix, f"lv{n}.e.{eid}", _sp.OP_EDGE_KNOWLEDGE_UPSERT, {
                    "actor_id": actor, "edge_id": eid,
                    "fields": {"connection_known": True, "acquisition_sources": [LOCAL_VIEW_SOURCE]}}))
            if not _sp.knows_place(know, e["to"]):
                # H07 §5: 출구 너머 장소의 존재를 안다. 이름은 스스로 드러나는 곳(unknown_name 미저작)만 —
                #        세력·비밀 이름은 명시 발견 전까지 표시명(unknown_name)으로만 보인다(core.place_display).
                out.append(_mk(prefix, f"lv{n}.p.{e['to']}", _sp.OP_PLACE_KNOWLEDGE_UPSERT, {
                    "actor_id": actor, "place_id": e["to"],
                    "fields": dict(_pd.seen_place_fields(definition, e["to"]),
                                   acquisitions=[LOCAL_VIEW_SOURCE])}))
    seen, uniq = set(), []
    for op in out:
        if op.op_id not in seen:
            seen.add(op.op_id)
            uniq.append(op)
    return uniq


def _arrival_ops(prefix, pcs, edge_id, to_place, minute, *, scene=True, definition=None, world=None):
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
    ops += local_view_ops(prefix, definition, world, pcs, to_place)
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
        label = f"{e['to']} 쪽 {e['access_ref']}({fact['observed_state']}) 통과"
        if dec.get("payment"):                     # v7.17(R4): 보여 주기가 아니라 실제로 건네야 통과
            label += f" — {dec.get('requires_capability')} {dec['payment']['quantity']}개를 실제로 건넨다"
        out.append({"action_id": aid, "label": label})
    return tuple(sorted(out, key=lambda o: o["action_id"]))


# ══════════════════════════════════════════════════════════════
#  계획
# ══════════════════════════════════════════════════════════════

def default_mode(definition: dict) -> str | None:
    modes = sorted(definition.get("movement_modes") or {})
    return "walk" if "walk" in modes else (modes[0] if modes else None)


def plan_player_turn(ctx: WorldContext, world, intent: _ai.ResolvedActionIntent, *, transaction_id: str,
                     narrative_seed: str, capabilities=(), resolved_tie_orders=None,
                     policy: RouteChoicePolicy = DEFAULT_ROUTE_POLICY, stats=None,
                     item_facts=None, access_capabilities=None) -> PlayerWorldPlan:
    """ResolvedActionIntent → PlayerWorldPlan(순수). world는 세션 또는 ProjectedWorld."""
    D = ctx.definition
    src = world if isinstance(world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(world)
    lead = ctx.lead_pc
    lead_pl = _sp.placement_of(src, lead) or {}
    here = lead_pl.get("place_id") if lead_pl.get("kind") == _sp.AT_PLACE else None
    transit = lead_pl if lead_pl.get("kind") == _sp.TRANSIT else None      # H06 R2-06: 구간 안(장소 아님)
    now = (src.domain("world_clock") or {}).get("minute_index")
    prefix = "h05." + stable_digest("player.turn.v1", transaction_id, length=10)
    plan = PlayerWorldPlan(PLAN_NONE, _ai.world_version_id(src), ctx.definition_revision, now, now, scene_place=here)
    if intent.stale:
        plan.status, plan.reason = PLAN_REJECTED, "MANIFEST_STALE"
        return plan
    if _ai.is_definition_stale(ctx.definition_revision, world) or (
            intent.definition_revision is not None and intent.definition_revision != ctx.definition_revision):
        plan.status, plan.reason = PLAN_REJECTED, "DEFINITION_STALE"     # 옛 정의 문맥·의도로 계획하지 않는다
        return plan
    observer = {"observer_items": tuple(sorted(capabilities or ()))}
    if item_facts is not None:
        # v7.17: 직전 커밋 턴의 사용·지급 사실(보유와 구별 — R2·R4). 이번 턴 계획에서만 재평가 입력(Q11)
        observer["observer_used_items"] = tuple(sorted(set((item_facts or {}).get("used") or ())))
        observer["observer_paid_items"] = tuple(sorted(set((item_facts or {}).get("paid") or ())))
    if stats is not None:
        observer["observer_stats"] = {k: v for k, v in dict(stats).items()
                                      if isinstance(v, int) and not isinstance(v, bool)}
    run = _Run(D, src, seed=narrative_seed, tie_orders=resolved_tie_orders, prefix=prefix, observer=observer)
    plan.observer = dict(observer)                 # P1: 진행 없이 끝나는 계획도 보충 진행에 같은 관찰자 문맥을 준다
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
    if intent.resolution is not None:
        # H07(사용자 결정): 플레이어 명시 결단 → 예약 사건 지역 상태(typed EVENT_LOCAL_SET, PLAYER_RESOLUTION).
        # 지금도 열려 있는 선택지만(진행 중 · 현재 국면 · 미결단 · 장면 관련). 결과는 저작 국면·케이스가 적용한다.
        from . import events as _ev
        iid, oid = intent.resolution
        if (iid, oid) not in {(a, b) for a, b, _ in _ev.open_resolutions(D, src, here)}:
            plan.diagnostics.append({"code": "RESOLUTION_NOT_OPEN", "instance_id": iid, "option_id": oid})
        else:
            pre_ops.append(_wm.make_op(f"{prefix}.resolution", _ev.OP_EVENT_LOCAL_SET, _ev.RESOLUTION_SOURCE, {
                "instance_id": iid, "key": _ev.RESOLUTION_KEY, "value": oid}))
            plan.outcome["resolution"] = {"instance_id": iid, "option_id": oid}
    mv = intent.movement
    # v7.17.2: 실제 접근 판정은 소속 capability까지(access_capabilities), 사건 보유 조건(observer_items)은 소지품만
    move_caps = tuple(access_capabilities) if access_capabilities is not None else capabilities
    if mv is not None and mv.kind == _ai.MOVE_DESTINATION:
        if transit is not None:
            return _plan_transit_movement(ctx, plan, run, transit, now, mv.destination_place, intent, pre_ops,
                                          capabilities=move_caps, policy=policy, transaction_id=transaction_id)
        return _plan_movement(ctx, plan, run, src, here, now, mv.destination_place, intent, pre_ops,
                              capabilities=move_caps, policy=policy, transaction_id=transaction_id)
    if intent.time_advance is not None:
        if transit is not None:
            try:
                target = _ai.target_minute(D.get("calendar") or {}, now, intent.time_advance)
            except Exception:
                target = None
            if target is None or target >= transit["edge_expected_end_minute"]:
                # 구간 안에서 구간 끝을 넘겨 머무를 수 없다(TRANSIT은 start ≤ now < end) — 이동을 이어 가야 한다
                plan.status, plan.reason = PLAN_REJECTED, "IN_TRANSIT_TIME"
                plan.transit = {k: transit.get(k) for k in ("edge_id", "from_place", "to_place")}
                return plan
        return _plan_time(ctx, plan, run, here, now, intent.time_advance, pre_ops)
    if intent.action_minutes:
        if transit is not None:
            # 구간 안(TRANSIT)에서는 구간 끝을 넘겨 머물 수 없다 — 행동 시간은 이동을 이어 갈 때 흐른다
            plan.diagnostics.append({"code": "ACTION_TIME_IN_TRANSIT", "minutes": intent.action_minutes})
        else:
            return _plan_action(ctx, plan, run, here, now, intent.action_minutes, pre_ops)
    if pre_ops:
        run.step(pre_ops, now, _progress(lead, here))
        return _finish(plan, run, None)
    return plan


def _finish(plan, run, outcome_kind):
    plan.observer = dict(run.observer)
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


def _note_payments(plan, run, trav) -> None:
    """v7.17(R4) 확인 행동의 실제 인계를 이 턴 아이템 연산으로 남기고, P1(D4) 이후 진행 단계가 같은 턴에
    그 지급 사실(OBSERVER_PAID_ITEM)을 보게 한다 — 다음 턴으로 미루지 않는다."""
    pays = [dict(c["payment"], action_id=c.get("action_id")) for c in trav.checkpoint_times
            if c["kind"] == _mv.ACCESS_ACTION and c.get("payment")]
    if not pays:
        return
    plan.item_payments += pays
    paid = set(run.observer.get("observer_paid_items") or ()) | {p["capability"] for p in pays if p.get("capability")}
    run.observer["observer_paid_items"] = tuple(sorted(paid))


def _plan_action(ctx, plan, run, here, now, minutes, pre_ops):
    """P1(D1): 이동·대기 아닌 행동(대화·조사·작업)의 소요 시간을 묘사 전에 진행한다(코드 소유 경과).

    활동 문맥(OBSERVER_ACTIVITY)은 주지 않는다 — 대기·수면 같은 저작 활동이 아니다(이동 단계와 같은 규칙).
    관찰 신호가 끼어들면 그 분에 멈춘다(행동이 끊김)."""
    r = run.step(pre_ops, now + minutes, _progress(ctx.lead_pc, here))
    if r is None:
        return _finish(plan, run, None)
    reached = r.reached_minute
    plan.code_owned_minutes = reached - now
    plan.outcome.update({"action_minutes": minutes, "reached_minute": reached, "interrupt": r.foreground_interrupt})
    return _finish(plan, run, OUT_ACTION_INTERRUPTED if reached < now + minutes else OUT_ACTION_DONE)


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


def _plan_transit_movement(ctx, plan, run, tr, now, dest, intent, pre_ops, *, capabilities, policy,
                           transaction_id):
    """H06 R2-06: PC가 구간 안(TRANSIT)일 때의 이동 — 현재 구간의 **남은 시간만** 진행해 끝 장소에 도착한 뒤,
    목적지가 더 멀면 그 장소에서 일반 계획(알려진 경로·실제 접근·경로 선택 규칙)을 이어 간다.

    · 이미 지난 구간·이미 소비한 시간을 다시 계산하지 않는다(재개 = edge_expected_end까지).
    · 출발지로 되돌리거나 목적지로 자동 완료하지 않는다. 임의 장소를 만들지 않는다.
    · 현재 구간의 실제 접근이 그 사이 막혔으면(세계 변화) 강행하지 않는다 — TRANSIT 유지, 계획 거부.
    """
    D, lead = ctx.definition, ctx.lead_pc
    eid, frm, to, end = tr["edge_id"], tr["from_place"], tr["to_place"], tr["edge_expected_end_minute"]
    mode = default_mode(D)
    plan.transit = {"edge_id": eid, "from_place": frm, "to_place": to}
    plan.outcome.update({"destination": dest, "origin": None, "mode": mode,
                         "transit_resume": {"edge_id": eid, "from_place": frm, "to_place": to,
                                            "remaining_minutes": end - now}})
    if mode is None:
        plan.status, plan.reason = PLAN_REJECTED, "NO_MOVEMENT_MODE"
        return plan
    try:
        trav = _mv.plan_actual_traversal(D, actor_id=lead, edge_sequence=(eid,), mode_id=mode, start_minute=now,
                                         access_state=run.world.domain("access_control_state") or {},
                                         actor_capabilities=tuple(capabilities),
                                         confirmed_actions=tuple(intent.access_action_ids))
    except _mv.MovementError as ex:
        plan.status, plan.reason = PLAN_REJECTED, "TRANSIT_ROUTE_STALE"
        plan.diagnostics.append({"code": "TRANSIT_ROUTE_STALE", "detail": str(ex)})
        return plan
    if trav.interruption is not None:
        plan.status, plan.reason = PLAN_REJECTED, "TRANSIT_ROUTE_STALE"
        plan.outcome["access_interruption"] = {"edge_id": eid, "kind": trav.interruption.get("kind")}
        return plan
    _note_payments(plan, run, trav)
    pcs = tuple(p for p in ctx.pcs if (_sp.placement_of(run.world, p) or {}).get("edge_id") == eid)
    r = run.step(chain(pre_ops), end, _progress(lead, None, movement={"edge_id": eid}))
    if r is None:
        return _finish(plan, run, None)
    if r.status == _wp.FOREGROUND_INTERRUPT and r.reached_minute < end:
        plan.transit["minute"] = r.reached_minute                  # 아직 구간 안 — TRANSIT 유지
        plan.outcome["interrupt"] = r.foreground_interrupt
        plan.code_owned_minutes = (run.now() or now) - now
        return _finish(plan, run, OUT_INTERRUPTED_SIGNAL)
    r2 = run.step(_arrival_ops(f"{run.prefix}.c", pcs, eid, to, end, definition=D, world=run.world), end, _progress(lead, to))
    if r2 is None:
        return _finish(plan, run, None)
    plan.transit = None
    stopped = r.status == _wp.FOREGROUND_INTERRUPT or r2.status == _wp.FOREGROUND_INTERRUPT
    if dest == to or stopped:
        if stopped:
            plan.outcome["interrupt"] = (r2 if r2.status == _wp.FOREGROUND_INTERRUPT else r).foreground_interrupt
        plan.scene_place = to
        plan.code_owned_minutes = (run.now() or now) - now
        plan.outcome.update({"final_place": to, "arrived": dest == to})
        return _finish(plan, run, OUT_ARRIVED if dest == to else OUT_INTERRUPTED_SIGNAL)
    out = _plan_movement(ctx, plan, run, run.world, to, end, dest, intent, [],
                         capabilities=capabilities, policy=policy, transaction_id=transaction_id)
    if out.status == PLAN_PLANNED:
        out.code_owned_minutes = (out.end_minute if out.end_minute is not None else end) - now
        out.start_minute = now
    return out


def _plan_movement(ctx, plan, run, src, here, now, dest, intent, pre_ops, *, capabilities, policy,
                   transaction_id):
    D, lead = ctx.definition, ctx.lead_pc
    mode = default_mode(D)
    plan.outcome.update({"destination": dest, "origin": here, "mode": mode})
    if mode is None or here is None:
        plan.status, plan.reason = PLAN_REJECTED, "NO_MOVEMENT_MODE"
        return plan
    try:
        # H07(사용자 결정): PUBLIC/LOCAL 일반 그래프 + 아는 RESTRICTED/SECRET — 경로 세부를 몰라도 출발할 수 있다
        pk = _sp.actor_knowledge(src, lead)
        known = _mv.plan_known_routes(D, pk, here, dest, mode, public_graph=True)
    except _mv.MovementError as e:
        plan.status, plan.reason = PLAN_REJECTED, f"ROUTE_PLAN_INVALID:{e}"
        return plan
    decision, cand, options = choose_route(known, chosen_signature=intent.route_signature, policy=policy,
                                           definition=D, knowledge=pk)
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
        # v7.17(R4): 확인 행동을 마쳐 구간에 들어섰다 — 그 행동이 요구한 실제 인계를 이 턴의 아이템 연산으로 남긴다
        _note_payments(plan, run, trav)
        if end == start:                                    # 0분 구간 — 같은 분 도착
            pending = chain(pending, obs, _arrival_ops(f"{run.prefix}.a{i}", pcs, eid, e["to"], minute,
                                                                    definition=D, world=run.world))
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
        pending = _arrival_ops(f"{run.prefix}.a{i}", pcs, eid, e["to"], end, definition=D, world=run.world)
        cur, minute = e["to"], end
        if r.status == _wp.FOREGROUND_INTERRUPT:            # 도착 분의 신호 — 도착 후 멈춘다
            outcome = OUT_INTERRUPTED_SIGNAL if i < len(seq) - 1 else OUT_ARRIVED
            plan.outcome["interrupt"] = r.foreground_interrupt
            if i < len(seq) - 1:
                break
        if i < len(seq) - 1:
            # H06 R2-06: 중간 장소 도착은 그 자체로 한 번의 진행이다 — 도착이 촉발한 관찰 사건은 다음 구간
            #   진입(TRANSIT)과 섞이지 않고 그 장소(AT_PLACE)에서 플레이어 진행을 멈춘다.
            r2 = run.step(pending, end, _progress(lead, cur))
            pending = []
            if r2 is None:
                break
            if r2.status == _wp.FOREGROUND_INTERRUPT:
                outcome = OUT_INTERRUPTED_SIGNAL
                plan.outcome["interrupt"] = r2.foreground_interrupt
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
