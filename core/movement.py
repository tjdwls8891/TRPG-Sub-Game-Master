# 이동 — 소요시간 계산 · 알려진 경로 후보(KnownRoutePlanner) · 실제 통과(ActualTraversalPlanner) (H03-B)
#
# [권위 — DESIGN 02 §14~22 · §35~64 · H03 결정 게이트 A8·A10·A11]
#   소요시간:
#     moving = distance_m / base_speed_m_per_min × 60초 × (edge × stance × actor × environment)
#     edge_raw = moving + fixed_delay_minutes×60초 + (자동 수행한 접근 행동 지연)
#   배수는 전부 **시간 배수**다(1.2 = 20% 더 걸림). 속도 배수와 섞지 않는다.
#   고정 정밀도: 저작 배수·속도는 소수 6자리 이내 십진수(컴파일러가 강제)이고 계산은
#   Fraction(정확한 유리수)로 한 뒤 구간마다 **정수 마이크로초**로 ROUND_HALF_UP 양자화한다.
#   분 정규화는 경로 전체 누적 마이크로초에서 한 번만: minutes = ROUND_HALF_UP(us / 60e6).
#   경유 checkpoint Ti = T0 + ROUND_HALF_UP(누적_i / 60e6). 구간마다 분으로 반올림하지 않는다.
#
#   KnownRoutePlanner — 행위자의 공간 지식만으로 후보를 만든다. 실제 접근 상태를 인자로
#     받지 않는다(서명 차단). 지식에 없는 구간은 그래프에 없다. NO_KNOWN_ROUTE를 실제
#     그래프로 보완하지 않는다. 지식상 막힘(PERCEIVED_BLOCKED)만 제외하고, 모르는 봉쇄는
#     후보에 남긴다(숨은 봉쇄 비노출).
#     알고리즘: 방향 Yen K-shortest simple paths(정수 마이크로초 비용, 동률은 edge_id
#     서명 사전순) → 비용 상한(최단 × max_cost_ratio) → 가중 Jaccard 중첩 다양성(Fraction)
#     → 최대 max_displayed 개. dict 삽입 순서·PYTHONHASHSEED와 무관하다.
#   ActualTraversalPlanner — 선택된 구간 순서를 그대로 따라간다. 숨은 봉쇄에서 멈추고
#     다른 길로 우회하지 않는다.
#
# [하지 않는 것]
#   · ASK 임계값(시간·거리)을 정하지 않는다(MovementAskPolicy 구조만 — 값은 H05 calibration).
#   · 사건·예약 checkpoint를 끼우지 않는다(H04). 시계·위치를 직접 바꾸지 않는다 —
#     traversal_ops가 typed 연산을 만들 뿐이며 H03 production 호출자는 없다.
#   · provider를 호출하지 않는다.
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from . import access as _acc
from . import spatial as _sp
from . import world_mutation as _wm
from . import world_schedule as _sched

US_PER_SECOND = 1_000_000
US_PER_MINUTE = 60 * US_PER_SECOND
FIXED_DECIMAL_PLACES = 6

EDGE_START = "EDGE_START"
EDGE_END = "EDGE_END"
ACCESS_ACTION = "ACCESS_ACTION"
DESTINATION = "DESTINATION"
CHECKPOINT_KINDS = (EDGE_START, EDGE_END, ACCESS_ACTION, DESTINATION)

ROUTE_OK = "OK"
NO_KNOWN_ROUTE = "NO_KNOWN_ROUTE"
SAME_PLACE = "SAME_PLACE"


class MovementError(ValueError):
    """이동 입력이 정의와 맞지 않을 때(없는 장소·방식·끊긴 경로 등)."""


# ══════════════════════════════════════════════════════════════
#  고정 정밀도 산술
# ══════════════════════════════════════════════════════════════

def as_factor(v) -> Fraction:
    """양의 시간 배수(소수 6자리 이내) → Fraction. float도 str()로 십진 표기를 고정한다."""
    if isinstance(v, Fraction):
        if v <= 0:
            raise MovementError(f"배수는 0보다 커야 합니다: {v}")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        raise MovementError(f"배수 형식이 잘못됐습니다: {v!r}")
    try:
        dv = Decimal(str(v))
    except InvalidOperation:
        raise MovementError(f"배수 형식이 잘못됐습니다: {v!r}") from None
    if not dv.is_finite() or dv <= 0 or dv.as_tuple().exponent < -FIXED_DECIMAL_PLACES:
        raise MovementError(f"배수는 0보다 큰 소수 {FIXED_DECIMAL_PLACES}자리 이내 수여야 합니다: {v!r}")
    return Fraction(dv)


def round_half_up(x: Fraction) -> int:
    """0 이상 유리수의 ROUND_HALF_UP 정수."""
    if x < 0:
        raise MovementError("음수 소요시간")
    return int((x + Fraction(1, 2)).__floor__())


def us_to_minutes(us: int) -> int:
    return round_half_up(Fraction(us, US_PER_MINUTE))


def moving_us(distance_m: int, speed_m_per_min, factor: Fraction) -> int:
    """이동 부분(지연 제외) 정수 마이크로초."""
    seconds = Fraction(distance_m) / as_factor(speed_m_per_min) * 60 * factor
    return round_half_up(seconds * US_PER_SECOND)


@dataclass(frozen=True)
class MovementProfile:
    """이동 방식 + 시간 배수 묶음. 배수는 시나리오 어댑터가 계산해 넘긴다(core는 스탯명을 모름)."""
    mode: dict
    stance_id: str | None = None
    stance_factor: Fraction = Fraction(1)
    actor_factor: Fraction = Fraction(1)
    environment_factor: Fraction = Fraction(1)

    @classmethod
    def build(cls, definition: dict, mode_id: str, *, stance_id=None, stance_factor=1,
              actor_factor=1, environment_factor=1) -> "MovementProfile":
        mode = (definition.get("movement_modes") or {}).get(mode_id)
        if mode is None:
            raise MovementError(f"정의되지 않은 이동 방식 {mode_id!r}")
        return cls(mode, stance_id, as_factor(stance_factor), as_factor(actor_factor),
                   as_factor(environment_factor))

    def edge_factor(self, edge: dict) -> Fraction:
        return as_factor(edge["base_time_factor"]) * self.stance_factor * self.actor_factor \
            * self.environment_factor


def edge_duration_us(edge: dict, profile: MovementProfile, *, distance_m=None,
                     access_delay_minutes: int = 0) -> int:
    """구간 하나의 raw 소요(정수 마이크로초). distance_m을 주면 그 값(지식 추정치)으로 계산."""
    dist = edge["distance_m"] if distance_m is None else distance_m
    if dist is None:
        raise MovementError(f"{edge['edge_id']}: 거리가 저작되지 않은 구간")
    return (moving_us(dist, profile.mode["base_speed_m_per_min"], profile.edge_factor(edge))
            + edge["fixed_delay_minutes"] * US_PER_MINUTE + access_delay_minutes * US_PER_MINUTE)


def route_duration(definition: dict, edge_ids, profile: MovementProfile, start_minute: int = 0) -> dict:
    """경로 전체 소요: 구간 raw 합산 → 한 번만 분 정규화. checkpoint 분 포함(접근 지연 제외)."""
    edges = definition["movement_edges"]
    cum, cps = 0, []
    for eid in edge_ids:
        cum += edge_duration_us(edges[eid], profile)
        cps.append(start_minute + us_to_minutes(cum))
    return {"raw_total_us": cum, "canonical_total_minutes": us_to_minutes(cum),
            "edge_end_minutes": cps, "arrival_minute": start_minute + us_to_minutes(cum)}


# ══════════════════════════════════════════════════════════════
#  K-shortest simple paths (결정적)
# ══════════════════════════════════════════════════════════════

def _dijkstra(adj, cost, src, dst, banned_edges, banned_nodes):
    """(비용, 서명) 최소 경로. adj: node → [(edge_id, to)] edge_id 순."""
    heap = [(0, (), src)]
    done = set()
    while heap:
        c, sig, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u == dst:
            return c, sig
        for eid, v in adj.get(u, ()):
            if eid in banned_edges or v in banned_nodes or v in done:
                continue
            heapq.heappush(heap, (c + cost[eid], sig + (eid,), v))
    return None


def k_shortest_simple_paths(adj, cost, to_of, src, dst, k):
    """Yen 방식. 반환: [(cost, edge_tuple)] (비용, edge_id 서명) 오름차순, 최대 k개."""
    first = _dijkstra(adj, cost, src, dst, frozenset(), frozenset())
    if first is None:
        return []
    found, cand, seen = [first], [], {first[1]}
    while len(found) < k:
        prev = found[-1][1]
        nodes = [src] + [to_of[e] for e in prev]
        for i in range(len(prev)):
            spur, root = nodes[i], prev[:i]
            banned_e = {p[1][i] for p in found if p[1][:i] == root and len(p[1]) > i}
            banned_n = frozenset(nodes[:i])
            r = _dijkstra(adj, cost, spur, dst, frozenset(banned_e), banned_n)
            if r is None:
                continue
            path = root + r[1]
            if path in seen:
                continue
            seen.add(path)
            heapq.heappush(cand, (sum(cost[e] for e in path), path))
        if not cand:
            break
        found.append(heapq.heappop(cand))
    return found


def weighted_overlap(a: tuple, b: tuple, cost: dict) -> Fraction:
    """가중 Jaccard = 공유 구간 비용 / 합집합 비용. 합집합 비용이 0이면 구간 수로."""
    sa, sb = set(a), set(b)
    union = sum(cost[e] for e in sa | sb)
    if union == 0:
        return Fraction(len(sa & sb), max(1, len(sa | sb)))
    return Fraction(sum(cost[e] for e in sa & sb), union)


# ══════════════════════════════════════════════════════════════
#  KnownRoutePlanner
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class RoutePlannerConfig:
    """후보 생성 설정. 수치는 영도 경로 데이터로 calibration 예정(DESIGN 02 §40·46·47)."""
    raw_k: int = 10
    max_displayed: int = 3
    max_cost_ratio: Fraction = Fraction(2)
    max_overlap: Fraction = Fraction(7, 10)
    unknown_edge_cost_us: int = 10 * US_PER_MINUTE      # 비용 모르는 구간의 중립 순위 비용


@dataclass(frozen=True)
class MovementAskPolicy:
    """ASK 임계 구조만(H03). 값은 H05 제품 calibration — None이면 임계 미적용."""
    ask_if_duration_gte: int | None = None
    ask_if_distance_gte: int | None = None
    max_displayed_routes: int = 3


@dataclass(frozen=True)
class KnownRouteCandidate:
    option_id: str                    # ASK 이어받기용 transient id(정본 아님)
    edge_ids: tuple
    place_ids: tuple
    perceived_cost_us: int
    perceived_duration_minutes: int | None
    perceived_distance_m: int | None
    cost_known: bool
    first_divergence_edge: str
    route_label: str
    known_constraints: tuple
    knowledge_basis: str              # ESTIMATED / PARTIAL / UNKNOWN

    @property
    def signature(self) -> tuple:
        return self.edge_ids

    def to_dict(self) -> dict:
        return {"option_id": self.option_id, "edge_ids": list(self.edge_ids),
                "place_ids": list(self.place_ids), "perceived_cost_us": self.perceived_cost_us,
                "perceived_duration_minutes": self.perceived_duration_minutes,
                "perceived_distance_m": self.perceived_distance_m, "cost_known": self.cost_known,
                "first_divergence_edge": self.first_divergence_edge, "route_label": self.route_label,
                "known_constraints": [dict(c) for c in self.known_constraints],
                "knowledge_basis": self.knowledge_basis}


@dataclass(frozen=True)
class KnownRoutePlan:
    status: str
    origin: str
    destination: str
    mode_id: str
    candidates: tuple = ()
    raw_candidate_count: int = 0

    def to_dict(self) -> dict:
        return {"status": self.status, "origin": self.origin, "destination": self.destination,
                "mode_id": self.mode_id, "raw_candidate_count": self.raw_candidate_count,
                "candidates": [c.to_dict() for c in self.candidates]}


def known_graph(definition: dict, knowledge: dict, profile: MovementProfile, config: RoutePlannerConfig):
    """행위자 지식 그래프: (adj, cost, to_of, meta). 실제 접근 상태를 읽지 않는다."""
    edges = definition.get("movement_edges") or {}
    adj, cost, to_of, meta = {}, {}, {}, {}
    for eid in _sp.known_edge_ids(knowledge):
        e = edges.get(eid)
        if e is None:
            continue                                   # 정의에서 사라진 구간 지식(무시)
        ok, _ = _sp.mode_allows_edge(profile.mode, e)
        if not ok:
            continue
        perc = _acc.perceived_access(knowledge, e)
        if perc["perceived"] == _acc.PERCEIVED_BLOCKED:
            continue                                   # 행위자가 막혔다고 아는 길만 제외
        est = (knowledge["edges"][eid] or {}).get("distance_estimate_m")
        if est is not None:
            c, known = edge_duration_us(e, profile, distance_m=est), True
        else:
            c, known = config.unknown_edge_cost_us, False
        adj.setdefault(e["from"], []).append((eid, e["to"]))
        cost[eid], to_of[eid] = c, e["to"]
        meta[eid] = {"cost_known": known, "distance_estimate_m": est, "perceived": perc}
    for k in adj:
        adj[k].sort()
    return adj, cost, to_of, meta


def plan_known_routes(definition: dict, knowledge: dict, origin: str, destination: str, mode_id: str, *,
                      stance_id=None, stance_factor=1, actor_factor=1, environment_factor=1,
                      config: RoutePlannerConfig = RoutePlannerConfig()) -> KnownRoutePlan:
    places = definition.get("place_definitions") or {}
    for p in (origin, destination):
        if p not in places:
            raise MovementError(f"정의되지 않은 장소 {p!r}")
    if origin == destination:
        return KnownRoutePlan(SAME_PLACE, origin, destination, mode_id)
    profile = MovementProfile.build(definition, mode_id, stance_id=stance_id, stance_factor=stance_factor,
                                    actor_factor=actor_factor, environment_factor=environment_factor)
    adj, cost, to_of, meta = known_graph(definition, knowledge or {}, profile, config)
    raw = k_shortest_simple_paths(adj, cost, to_of, origin, destination, config.raw_k)
    if not raw:
        return KnownRoutePlan(NO_KNOWN_ROUTE, origin, destination, mode_id)
    best_cost, best = raw[0]
    chosen = [raw[0]]
    for c, path in raw[1:]:
        if len(chosen) >= config.max_displayed:
            break
        if c > best_cost * config.max_cost_ratio:
            break                                      # 정렬돼 있으므로 이후도 초과
        if max(weighted_overlap(path, p, cost) for _, p in chosen) > config.max_overlap:
            continue
        chosen.append((c, path))
    edges = definition["movement_edges"]
    out = []
    for n, (c, path) in enumerate(chosen, 1):
        div = next((i for i, e in enumerate(path) if i >= len(best) or best[i] != e), 0) if n > 1 else 0
        div_edge = path[div]
        places_seq = (origin,) + tuple(to_of[e] for e in path)
        known = all(meta[e]["cost_known"] for e in path)
        partial = any(meta[e]["cost_known"] for e in path)
        out.append(KnownRouteCandidate(
            option_id=f"route_{n}", edge_ids=path, place_ids=places_seq, perceived_cost_us=c,
            perceived_duration_minutes=us_to_minutes(c) if known else None,
            perceived_distance_m=sum(meta[e]["distance_estimate_m"] for e in path) if known else None,
            cost_known=known, first_divergence_edge=div_edge,
            route_label=_route_label(edges, path, div, best, origin, to_of),
            known_constraints=tuple(
                {"edge_id": e, "perceived": meta[e]["perceived"]["perceived"],
                 "action_policy": meta[e]["perceived"]["action_policy"]}
                for e in path if meta[e]["perceived"]["perceived"] not in (_acc.UNKNOWN, _acc.PERCEIVED_PASSABLE)),
            knowledge_basis="ESTIMATED" if known else ("PARTIAL" if partial else "UNKNOWN")))
    return KnownRoutePlan(ROUTE_OK, origin, destination, mode_id, tuple(out), len(raw))


def _route_label(edges, path, div, best, origin, to_of) -> str:
    """1) 첫 분기 구간 route_hint 2) 최단 경로에 없는 첫 경유지 3) 첫 분기 구간의 도착지 방향."""
    hint = edges[path[div]].get("route_hint")
    if hint:
        return hint
    best_places = {origin} | {to_of[e] for e in best}
    for e in path[div:-1]:
        if to_of[e] not in best_places:
            return f"{to_of[e]} 경유"
    return f"{to_of[path[div]]} 방향"


# ══════════════════════════════════════════════════════════════
#  ActualTraversalPlanner
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ActualTraversalPlan:
    actor_id: str
    selected_option_id: str | None
    intended_destination: str
    edge_sequence: tuple
    traversed_edges: tuple
    mode: str
    stance: str | None
    start_minute: int
    raw_total_us: int
    canonical_total_minutes: int
    end_minute: int
    expected_arrival_minute: int | None
    completed: bool
    end_place: str
    interruption: dict | None
    access_checks: tuple
    checkpoint_times: tuple
    source_world_version: str | None
    definition_revision: str | None

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def plan_actual_traversal(definition: dict, *, actor_id: str, edge_sequence, mode_id: str,
                          start_minute: int, access_state: dict, actor_capabilities=(),
                          stance_id=None, stance_factor=1, actor_factor=1, environment_factor=1,
                          confirmed_policies=(_acc.IMPLICIT,), selected_option_id=None,
                          source_world_version=None, definition_revision=None,
                          registry: _acc.AccessRuleRegistry = _acc.DEFAULT_REGISTRY) -> ActualTraversalPlan:
    """선택된 구간 순서를 실제 세계에서 따라간다. 막히면 그 구간 시작점에서 멈춘다(우회 없음).

    confirmed_policies: 자동 수행할 ACTION_REQUIRED 정책. IMPLICIT는 항상 포함,
    CONFIRM은 플레이어 확인이 있을 때만(H05), SEPARATE_ACTION은 언제나 중단.
    """
    edges = definition.get("movement_edges") or {}
    seq = tuple(edge_sequence)
    if not seq:
        raise MovementError("빈 경로")
    for eid in seq:
        if eid not in edges:
            raise MovementError(f"정의되지 않은 구간 {eid!r}")
    for a, b in zip(seq, seq[1:]):
        if edges[a]["to"] != edges[b]["from"]:
            raise MovementError(f"끊긴 경로: {a} → {b}")
    profile = MovementProfile.build(definition, mode_id, stance_id=stance_id, stance_factor=stance_factor,
                                    actor_factor=actor_factor, environment_factor=environment_factor)
    auto = set(confirmed_policies) | {_acc.IMPLICIT}
    auto.discard(_acc.SEPARATE_ACTION)
    cum, cps, checks, done = 0, [], [], []
    interruption = None
    at = lambda: start_minute + us_to_minutes(cum)
    here = edges[seq[0]]["from"]
    for eid in seq:
        e = edges[eid]
        ok, why = _sp.mode_allows_edge(profile.mode, e)
        if not ok:
            interruption = {"kind": "BLOCKED", "edge_id": eid, "reason": why, "minute": at()}
            break
        dec = _acc.actual_access(definition, access_state, e, actor_capabilities, registry)
        checks.append({"edge_id": eid, "minute": at(), **dec.to_dict()})
        delay = 0
        if dec.result == _acc.IMPASSABLE:
            interruption = {"kind": "BLOCKED", "edge_id": eid, "reason": dec.reason, "minute": at()}
            break
        if dec.result == _acc.ACTION_REQUIRED:
            if dec.action_policy not in auto:
                interruption = {"kind": "ACCESS_ACTION_REQUIRED", "edge_id": eid,
                                "action_policy": dec.action_policy, "minute": at()}
                break
            cps.append({"kind": ACCESS_ACTION, "edge_id": eid, "place_id": e["from"],
                        "minute": at(), "raw_offset_us": cum, "action_policy": dec.action_policy})
            delay = dec.action_delay_minutes
        cum += delay * US_PER_MINUTE
        cps.append({"kind": EDGE_START, "edge_id": eid, "place_id": e["from"], "minute": at(), "raw_offset_us": cum})
        cum += edge_duration_us(e, profile)
        cps.append({"kind": EDGE_END, "edge_id": eid, "place_id": e["to"], "minute": at(), "raw_offset_us": cum})
        done.append(eid)
        here = e["to"]
    completed = interruption is None
    if completed:
        cps.append({"kind": DESTINATION, "edge_id": None, "place_id": here, "minute": at(), "raw_offset_us": cum})
    return ActualTraversalPlan(
        actor_id=actor_id, selected_option_id=selected_option_id,
        intended_destination=edges[seq[-1]]["to"], edge_sequence=seq, traversed_edges=tuple(done),
        mode=mode_id, stance=stance_id, start_minute=start_minute, raw_total_us=cum,
        canonical_total_minutes=us_to_minutes(cum), end_minute=at(),
        expected_arrival_minute=at() if completed else None, completed=completed, end_place=here,
        interruption=interruption, access_checks=tuple(checks), checkpoint_times=tuple(cps),
        source_world_version=source_world_version, definition_revision=definition_revision)


def placement_at(plan: ActualTraversalPlan, minute: int, movement_id: str) -> dict:
    """계획 위 임의 분의 배치(파생). 구간 [EDGE_START, EDGE_END) 안이면 TRANSIT."""
    if minute < plan.start_minute:
        raise MovementError("계획 시작 이전 시각")
    starts = {c["edge_id"]: c for c in plan.checkpoint_times if c["kind"] == EDGE_START}
    ends = {c["edge_id"]: c for c in plan.checkpoint_times if c["kind"] == EDGE_END}
    place = starts[plan.traversed_edges[0]]["place_id"] if plan.traversed_edges else plan.end_place
    for eid in plan.traversed_edges:
        s, e = starts[eid], ends[eid]
        if s["minute"] <= minute < e["minute"]:
            return _sp.transit(movement_id=movement_id, edge_id=eid, from_place=s["place_id"],
                               to_place=e["place_id"], edge_started_at_minute=s["minute"],
                               edge_expected_end_minute=e["minute"])
        if minute >= e["minute"]:
            place = e["place_id"]
    return _sp.at_place(place)


def traversal_ops(plan: ActualTraversalPlan, *, op_prefix: str, source_kind: str = "MOVEMENT") -> list:
    """계획 → typed 연산(시계 진행·도착 배치·통과 구간/도착 장소/관찰한 접근 지식).

    H03에는 production 호출자가 없다(플레이어 이동 배선은 H05). 실제 세계 변화가
    지식을 자동 갱신하지 않도록, 지식 연산은 행위자가 실제로 겪은 것(통과·관찰)만 만든다.
    """
    mk = lambda i, t, p, **kw: _wm.make_op(f"{op_prefix}.{i}", t, source_kind, p, **kw)
    ops = [mk("clock", _sched.OP_CLOCK_ADVANCE_TO, {"target_minute": plan.end_minute}),
           mk("place", _sp.OP_ACTOR_PLACEMENT_SET, {"actor_id": plan.actor_id, "place_id": plan.end_place})]
    ends = {c["edge_id"]: c["minute"] for c in plan.checkpoint_times if c["kind"] == EDGE_END}
    for n, eid in enumerate(plan.traversed_edges):
        ops.append(mk(f"edge{n}", _sp.OP_EDGE_KNOWLEDGE_UPSERT, {
            "actor_id": plan.actor_id, "edge_id": eid,
            "fields": {"last_confirmed_at": ends[eid], "acquisition_sources": ["TRAVERSED"]}}))
    reached = [c["place_id"] for c in plan.checkpoint_times if c["kind"] == EDGE_END]
    for n, pid in enumerate(reached):
        ops.append(mk(f"visit{n}", _sp.OP_PLACE_KNOWLEDGE_UPSERT, {
            "actor_id": plan.actor_id, "place_id": pid, "fields": {"visited": True}}))
    seen = {}
    for chk in plan.access_checks:
        if chk["control_id"] is not None:
            seen[chk["control_id"]] = chk                      # 같은 제어는 마지막 관찰
    for n, ref in enumerate(sorted(seen)):
        chk = seen[ref]
        ops.append(mk(f"access{n}", _sp.OP_ACCESS_KNOWLEDGE_UPSERT, {
            "actor_id": plan.actor_id, "ref": ref,
            "fields": {"observed_result": chk["result"], "observed_state": chk["state"],
                       "action_policy": chk["action_policy"], "confirmed_at_minute": chk["minute"],
                       "source": "OBSERVED"}}))
    return ops
