"""WP-B — TurnPreparation: 트랜잭션 스코프 스테이징 모델.

목적(설계 불변식):
    AI/provider 결과 생산자·파서는 canonical 게임 상태를 직접 변경하지 않는다.
    자동 턴의 AI-파생 효과는 먼저 트랜잭션 스코프의 스테이징 데이터로 표현되고,
    묘사(narration)가 성립한 뒤에야 **단일 호환 적용 경계**에서 canonical에 반영된다.

이 모듈이 담당하는 것(WP-B~C):
    · 추출·비정규 인물·승격 결과의 스테이징(변이 계획 데이터) · 동시 준비 작업·READY 배리어.
    · (T-6: 레거시 지시효과 — 퀘스트 선택·intended_case·info_ledger·서사 계획 — 는 은퇴했다)

이 모듈이 하지 않는 것(상위 WP 소관):
    · 권위적 commit / READY_TO_COMMIT / CommitJournal·Settlement·InkTransaction 배선(WP-C/D).
    · durable 커밋 이력 이동(WP-D).

스테이징 원칙:
    · canonical Session을 임시 변경했다 되돌리는 방식 금지.
    · 전체 Session deepcopy 금지 — 영향받는 도메인만 복제한다.
    · 스테이징 효과는 임의 콜백이 아니라 검사·테스트 가능한 데이터여야 한다.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from indaim.turn import turn_transaction as _tx
from indaim.turn import world_state as _world_state
from indaim.rules.items import items as _items_fields






# ══════════════════════════════════════════════════════════════════
#  추출 결과 스테이징(result-only) + 검증 변이 계획 + stale guard
# ══════════════════════════════════════════════════════════════════
from dataclasses import field as _field  # noqa: E402


@dataclass
class ExtractionMutationPlan:
    """추출 결과에서 파생된, 검증·정규화된 변이 계획 — **적용의 유일한 권위**.

    provider 결과(raw)는 이 계획을 빌드하는 입력일 뿐이며, 빌드 이후에는 어떤
    canonical mutator의 입력도 되지 않는다. 호환 적용부는 아래 정규화된 typed
    필드(및 이를 반영한 entries)만 소비한다.

    · result: 파싱된 추출 결과(증거 스냅샷용; mutator 입력 아님).
    · entries: 정규화된 accepted 항목(검사·dedup·conflict 표시용, typed 필드와 일치).
    · conflicts: 상호 모순 진단(적용부는 모순 항목을 적용하지 않음).
    · dropped: 검증에서 탈락한 후보(적용부에 도달 불가).
    """
    transaction_id: str | None = None
    logical_turn: int | None = None
    attempt: int | None = None
    result: dict = _field(default_factory=dict)
    # ── 정규화된 도메인 DTO (적용 권위) ──
    status_apply: list = _field(default_factory=list)     # [{target,status,score}]
    status_clear: list = _field(default_factory=list)
    item_deltas: list = _field(default_factory=list)      # [{target,item,delta}] — LEGACY 적용 권위
    item_plan: object = None    # v7.17 DYNAMIC: core.items.ItemPlan(검증된 의미 연산) — 있으면 item_deltas 대신 이것만 적용
    npcs_met: list = _field(default_factory=list)
    companions_joined: list = _field(default_factory=list)
    companions_left: list = _field(default_factory=list)
    world_new_tl: dict | None = None
    world_player_faction: str | None = None   # H07-C: 사건 결과(grant_faction) 파생 호환 뷰(DYNAMIC)
    location_before: str | None = None
    location_after: str | None = None
    location_moved: bool = False
    situation: dict = _field(default_factory=dict)
    # ── 검사/진단 ──
    entries: list = _field(default_factory=list)
    conflicts: list = _field(default_factory=list)
    diagnostics: dict = _field(default_factory=dict)
    dropped: list = _field(default_factory=list)
    applied: bool = False
    rejected_stale: bool = False


def extraction_is_stale(session, *, logical_turn, attempt) -> bool:
    """추출의 (logical_turn, attempt)보다 더 새로운 논리 시도가 활성화됐으면 True.

    §26/§38 — 늦게 도착한 추출이 더 새로운 논리 턴/시도에 기록되는 것을 막는다.
    커밋 후 새 턴이 아직 없으면(active=None) stale이 아니다(정상 적용 대상).
    판정 본체는 비동기 provider 결과 공통 판정(superseded_by_newer_attempt)이다.
    """
    return superseded_by_newer_attempt(
        session, logical_turn=logical_turn, attempt=attempt)


def superseded_by_newer_attempt(session, *, logical_turn, attempt) -> bool:
    """캡처된 (logical_turn, attempt)보다 더 새로운 논리 시도가 활성이면 True.

    비동기 provider 결과(추출·비정규 NPC·자동 서사 재계획) 공통 stale 판정.
    결과를 만든 원인 시도의 정체성은 **스케줄/호출 시점에 복사**해 두어야 하며,
    적용 직전에 현재 활성 시도와 비교한다. 활성 시도가 없으면(active=None)
    stale이 아니다.
    """
    tx = _tx.get_active_transaction(session)
    if tx is None or logical_turn is None:
        return False
    try:
        return ((int(tx.logical_turn), int(tx.attempt))
                > (int(logical_turn), int(attempt or 0)))
    except Exception:
        return False


def _valid_char_names(session) -> set:
    valid = set()
    try:
        valid |= {p.get("name") for p in (getattr(session, "players", {}) or {}).values()
                  if p.get("name")}
        valid |= set((getattr(session, "npcs", {}) or {}).keys())
    except Exception:
        pass
    return valid


def _valid_status_names(session):
    try:
        from indaim.app.utils import get_merged_status_effects
        eff = get_merged_status_effects(getattr(session, "scenario_data", {}) or {})
        if isinstance(eff, dict):
            return set(eff.keys())
        if isinstance(eff, list):
            return {e.get("name") for e in eff if isinstance(e, dict) and e.get("name")}
    except Exception:
        pass
    return None


def build_extraction_plan(session, result, *, transaction_id=None,
                          logical_turn=None, attempt=None) -> ExtractionMutationPlan:
    """추출 결과를 canonical 변경 없이 검증·정규화된 변이 계획으로 만든다(result-only).

    도메인별 순수 정규화기(core.extraction.normalize_*)로 등록 캐릭터·병합 상태이상
    검증, 임계 판정, 동치 중복 제거, 상호 모순 처리를 마친 accepted DTO를 계획의
    typed 필드에 담는다. 이 계획이 이후 적용의 **유일한 권위**이며, raw result는
    빌드 이후 어떤 mutator의 입력도 되지 않는다.
    """
    from indaim.turn import extraction as _ex

    r = result if isinstance(result, dict) else {}
    plan = ExtractionMutationPlan(
        transaction_id=transaction_id, logical_turn=logical_turn,
        attempt=attempt, result=r)

    # 상태이상/소지품/만난 NPC — 검증·임계·dedup(순수).
    si = _ex.normalize_status_item_effects(session, r)
    plan.status_apply = si["status_apply"]
    plan.status_clear = si["status_clear"]
    plan.item_deltas = si["item_deltas"]
    plan.npcs_met = list(si["npcs"])
    if si.get("dropped"):
        plan.dropped.extend(si["dropped"])
        plan.diagnostics["dropped_status_item"] = list(si["dropped"])

    # 동행 — dedup + join&leave 모순 이름 양쪽 제외(순수).
    comp = _ex.normalize_companions(session, r)
    plan.companions_joined = comp["joined"]
    plan.companions_left = comp["left"]
    # 만난 NPC는 status_item.npcs 와 companions.npcs_met를 합집합으로 유지.
    for n in comp["npcs_met"]:
        if n not in plan.npcs_met:
            plan.npcs_met.append(n)
    for t in comp["conflicts"]:
        plan.conflicts.append({"domain": "companion", "target": t,
                               "reason": "join_and_leave"})

    # 세계 타임라인/장소 — to_world_timeline + resolve + hops(순수).
    prev_tl = getattr(session, "world_timeline", {}) or {}
    loc = _ex.normalize_location(session, r, prev_tl)
    plan.world_new_tl = loc["new_tl"]
    plan.location_before = loc["before"]
    plan.location_after = loc["after"]
    plan.location_moved = loc["moved"]

    # 상황(BGM).
    sit = r.get("situation")
    plan.situation = sit if isinstance(sit, dict) else {}

    # ── entries: typed 필드를 그대로 반영(검사·dedup·T-B17 표시용, 적용 권위와 일치) ──
    for e in plan.status_apply:
        plan.entries.append({"domain": "status", "target": e["target"],
                             "op": "apply", "payload": {"status": e["status"]}})
    for e in plan.status_clear:
        plan.entries.append({"domain": "status", "target": e["target"],
                             "op": "clear", "payload": {"status": e["status"]}})
    for e in plan.item_deltas:
        plan.entries.append({"domain": "item", "target": e["target"],
                             "op": "delta", "payload": {"item": e["item"], "delta": e["delta"]}})
    for n in plan.npcs_met:
        plan.entries.append({"domain": "npc_met", "target": n, "op": "meet", "payload": {}})
    for n in plan.companions_joined:
        plan.entries.append({"domain": "companion", "target": n, "op": "join", "payload": {}})
    for n in plan.companions_left:
        plan.entries.append({"domain": "companion", "target": n, "op": "leave", "payload": {}})
    if plan.location_after:
        plan.entries.append({"domain": "location", "target": plan.location_after,
                             "op": "move", "payload": {"moved": plan.location_moved}})

    return plan


# ══════════════════════════════════════════════════════════════════════
#  비정규 NPC 경로 — 미디어 배정(register) / 승격(promote) 변이 계획
#  두 오퍼레이션 모두 provider-boundary를 가지며 canonical NPC 상태
#  (session.irregular_npcs / session.npcs)를 변경한다. raw 모델 payload가
#  register/promote에 직접 들어가지 않도록, result-only 생산 → 검증·정규화 →
#  이 계획/후보 → stale guard → 단일 호환 적용부(register/promote 1회) 순서로
#  소비한다.
# ══════════════════════════════════════════════════════════════════════

@dataclass
class IrregularNpcMutationPlan:
    """비정규 NPC 미디어 배정의 검증·정규화된 등록 계획 — 적용의 유일 권위.

    · result: raw 미디어 배정 payload(증거 스냅샷; mutator 입력 아님).
    · registrations: 검증·정규화된 등록 항목([{name,image_key,gender,age,context,turn}]).
      이름은 코드 파생 후보(names) 안에 있어야 하고, image_key는 유효 풀 안이어야 한다.
    · rejected: 후보 목록 밖 등 탈락 항목(적용부에 도달 불가).
    """
    transaction_id: str | None = None
    logical_turn: int | None = None
    attempt: int | None = None
    result: dict = _field(default_factory=dict)
    registrations: list = _field(default_factory=list)
    rejected: list = _field(default_factory=list)
    diagnostics: dict = _field(default_factory=dict)
    applied: bool = False
    rejected_stale: bool = False


def build_irregular_npc_plan(session, data, *, names, valid_pool, use_image,
                             text, turn, transaction_id=None, logical_turn=None,
                             attempt=None) -> IrregularNpcMutationPlan:
    """비정규 NPC 미디어 배정 raw 결과를 검증·정규화한 등록 계획으로 만든다(무변이).

    · 이름은 코드가 뽑은 후보 목록(names) 안에 있어야 한다(모델이 새 인물을 못 만듦).
    · image_key는 유효 이미지 풀 안이어야 하며(이미지 off면 버림), 그 외엔 "".
    · gender/age는 문자열만 통과(그 외 None → register가 기본값 적용).
    · 동일 이름 후보는 하나로 접는다(dedup).
    """
    allow = set(names or [])
    pool = set(valid_pool or [])
    plan = IrregularNpcMutationPlan(
        transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt,
        result=data if isinstance(data, dict) else {})
    seen = set()
    for item in ((data or {}).get("npcs") or []):
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name or name not in allow:
            if name:
                plan.rejected.append(f"{name}(후보 목록 밖)")
            continue
        if name in seen:                       # 동치 후보 dedup
            continue
        seen.add(name)
        key = (item.get("image_key") or "").strip()
        if not use_image or key not in pool:
            key = ""                           # 이미지 off이거나 풀 밖 값은 버린다
        gender = item.get("gender") if isinstance(item.get("gender"), str) else None
        age = item.get("age") if isinstance(item.get("age"), str) else None
        plan.registrations.append({
            "name": name, "image_key": key, "gender": gender, "age": age,
            "context": _mention_context(text, name), "turn": turn,
        })
    if plan.rejected:
        plan.diagnostics["rejected"] = list(plan.rejected)
    return plan


def _mention_context(text, name) -> str:
    """v7.25(CUT): 등장 맥락 = 그 이름이 나오는 문단 전부(앞 120자 절단 대신 의미 단위). 없으면 묘사 전문."""
    paras = [p.strip() for p in str(text or "").split("\n\n") if p.strip()]
    hit = [p for p in paras if name and name in p]
    return "\n\n".join(hit) if hit else str(text or "")


def normalize_npc_detail(data, *, fallback_name) -> dict:
    """비정규 NPC 승격 세부설정 raw 결과를 정규화한다(무변이).

    raw payload가 promote에 직접 들어가지 않도록, 승격에 필요한 정규화된 필드만
    추린다. 고유명이 확정되면 final_name으로 넘긴다.

    Returns:
        {"final_name": str, "details": {"details","role","attitude"[,"birth_year"]}}
    """
    d = data if isinstance(data, dict) else {}
    final_name = ((d.get("name") or fallback_name) or "").strip() or fallback_name
    details = {
        "details": d.get("details") or "",
        "role": d.get("role") or "미상",
        "attitude": d.get("attitude") or "중립",
    }
    try:
        by = int(d.get("birth_year") or 0)
        if by > 0:
            details["birth_year"] = by
    except (TypeError, ValueError):
        pass
    return {"final_name": final_name, "details": details}


# ══════════════════════════════════════════════════════════════════════
#  WP-C — 트랜잭션 소유 동시 준비(Concurrent Preparation) + READY_TO_COMMIT 배리어
#
#  확정 묘사(finalized narration) 이후 전달/스트리밍·추출·비정규 NPC 등
#  필수 준비 작업을 동시에 돌리고, 모든 필수 작업과 비용 멤버십이 닫힌 뒤에만
#  READY_TO_COMMIT에 도달한다.
#
#  READY_TO_COMMIT은 authoritative commit이 아니다:
#    · CommitJournal / TurnSettlement / InkTransaction / strict save 와 무관(WP-D).
#    · 이 모듈은 런타임 전용 준비 상태와 증명만 소유한다. durable 영속 없음.
#  READY 이후의 호환 적용(legacy continuation)은 cogs.gm이 소유하며 WP-D가 치환한다.
# ══════════════════════════════════════════════════════════════════════
import asyncio as _asyncio
import hashlib as _hashlib
import json as _json


class BarrierViolationError(RuntimeError):
    """봉인(seal)/비용 동결 이후 필수 턴 작업·비용 오퍼레이션을 등록하려 했다.

    조용히 무시하지 않는다(§14/§25). 이 예외가 나면 provider 호출 자체가 시작되지
    않으므로 동결 이후 새 멤버 CostEvent가 생기지 않는다.
    """


# ── 작업 scope (§9) ──
SCOPE_AUTOMATIC_TURN = "AUTOMATIC_TURN"
SCOPE_TURN_DERIVED_OPTIONAL = "TURN_DERIVED_OPTIONAL"
SCOPE_SESSION_BACKGROUND = "SESSION_BACKGROUND"
SCOPE_FREE_FEATURE = "FREE_FEATURE"
SCOPE_MANUAL_GAMEPLAY = "MANUAL_GAMEPLAY"
SCOPE_ADMIN_OPERATOR = "ADMIN_OPERATOR"
SCOPE_SETUP = "SETUP"
SCOPE_GLOBAL = "GLOBAL"

# ── 작업 종류 ──
TASK_DELIVERY = "NARRATION_DELIVERY"
TASK_EXTRACTION = "EXTRACTION"
TASK_IRREGULAR_NPC = "IRREGULAR_NPC_RESOLUTION"
TASK_NPC_PROMOTION = "NPC_PROMOTION_DETAIL"

# ── 작업 상태 ──
TASK_RUNNING = "RUNNING"
TASK_SUCCEEDED = "SUCCEEDED"
TASK_FAILED = "FAILED"
TASK_CANCELLED = "CANCELLED"
_TASK_TERMINAL = frozenset({TASK_SUCCEEDED, TASK_FAILED, TASK_CANCELLED})

# ── 레지스트리/비용 멤버십 상태 (§14/§24) ──
REGISTRY_OPEN = "OPEN"
REGISTRY_CLOSING = "CLOSING"
REGISTRY_CLOSED = "CLOSED"
COST_OPEN = "OPEN"
COST_CLOSED = "CLOSED"

# ── 준비 단계 ──
PREP_COLLECTING = "COLLECTING"          # 판단/지시/ROLL 등 묘사 이전(비용 claim만)
PREP_PREPARING = "PREPARING"            # 확정 묘사 이후 동시 준비 진행
PREP_READY = "READY"                    # READY_TO_COMMIT 도달(증명 보유)
PREP_CONTINUED = "CONTINUED"            # READY 이후 authoritative commit 완료(durable COMMITTED)
PREP_COMMITTING = "COMMITTING"          # WP-D: CommitCoordinator 진행 중(새 선언 차단)
PREP_RETRY_PENDING = "RETRY_PENDING"    # 필수 준비 실패 — 같은 tx로 재시도 대기
PREP_FAILED = "FAILED"                  # 사전 READY 시스템 실패(종료)


@dataclass
class PreparationTaskRecord:
    """등록된 준비 작업 1건. 축(axis)은 서로 독립이다(§9)."""
    name: str
    kind: str
    transaction_id: str | None
    logical_turn: int | None
    attempt: int | None
    scope: str = SCOPE_AUTOMATIC_TURN
    blocks_preparation_ready: bool = True      # READY 전 terminal 필수
    required_success: bool = False             # 실패 시 READY 불가
    blocks_cost_closure: bool = True           # 비용 동결 전 terminal 필수
    required_player_output: bool = False
    may_spawn_required_child_work: bool = False
    turn_cost_membership: bool = True
    parent: str | None = None
    state: str = TASK_RUNNING
    result: object = None
    error: str | None = None
    task: object = field(default=None, repr=False, compare=False)

    @property
    def terminal(self) -> bool:
        return self.state in _TASK_TERMINAL


@dataclass(frozen=True)
class ReadyProof:
    """READY_TO_COMMIT 전이 시점에 캡처되는 불변 증명(§26/§27)."""
    transaction_id: str
    logical_turn: int
    attempt: int
    task_states: tuple
    frozen_cost_event_ids: tuple
    canonical_baseline_fingerprint: dict
    canonical_ready_fingerprint: dict
    canonical_unchanged: bool
    changed_domains: tuple
    delivered_message_ids: tuple
    extraction_entries: int
    # H02-B: 동결된 세계 변이 묶음 증명(빈 묶음 포함). quiescence 증명은 H03 몫 — 여기 없다.
    world_mutation_digest: str | None = None
    baseline_world_version_id: str | None = None
    definition_revision: str | None = None
    world_changed_domains: tuple = ()
    # H03: 투영된 세계의 base quiescence 증명(시계·예약). 사건 cascade 조건은 H04가 확장.
    base_quiescence: dict | None = None


# 되감기/커밋 소유 정본 도메인(§27). 백그라운드 압축 도메인(compressed_memory,
# uncompressed_logs 앞부분 삭제)과 운영 집계(total_cost 등)는 의도적으로 제외한다.
CANONICAL_DOMAINS = (
    "resources", "statuses", "world_timeline",
    "visited_places", "companions", "met_npcs", "irregular_npcs", "npcs",
    "last_extraction", "players",
    "stat_fail_counts", "turn_count", "gm_turns_done", "last_recorded_turn",
    "gm_proceed_history",
    # B-C6 — 전수 대조로 추가된 턴 소유 필드(모두 READY 이후 legacy continuation만 씀)
    "player_faction",           # 소속 호환 뷰(추출 적용)
    "pending_bgm",              # 추출 적용이 설정 / 이전 턴 확정분 소비는 rebaseline(아래)
    "last_bgm_situation",       # select_bgm(추출 적용)
    "start_day_number",         # timeline.quantify(추출 적용)
    "total_ink_spent",          # Settlement 파생 미러(WP-D CommitCoordinator만 씀)
    "last_turn_cost",           # Settlement 파생 미러(WP-D CommitCoordinator만 씀)
    "_rewind_snapshot",         # 되감기 델타 기준 스냅샷(continuation)
    # WP-D — CommitCoordinator만 쓰는 커밋 소유 필드
    "commit_marker", "last_turn_ink", "rewind_degraded_turns",
) + tuple(_world_state.GAMEPLAY_WORLD_FIELDS) + tuple(_items_fields.ITEM_FIELDS) \
    + tuple(_world_state.DELIVERY_FIELDS) \
    + tuple(_world_state.STATUS_FIELDS)       # P1: 전달 대기 · v7.38 상태 시계 — 커밋 경로만 쓴다
# H02 — 동적 세계 게임 정본(world_version 포함) 11필드. definition_revision·narrative_seed는
#   되감기 대상이 아닌 세션 정체성이라 넣지 않는다(definition_revision은 별도 명시 가드).
# 명시적 제외(배경/운영) — 근거는 completion bundle §23 필드 커버리지 매트릭스:
#   compressed_memory / last_compressed_turn / compression_count
#   (SESSION_BACKGROUND 압축), total_cost / total_usd / turn_cost_log (append-only 운영 집계),
#   extraction_pending / extraction_retry_ctx (운영 게이트), gm_clarify_count /
#   gm_narrate_count / gm_* 라운드 상태 (운영 카운터), cache_* (인프라),
#   rewind_log.jsonl 파일 (백그라운드 압축도 기록).
#   uncompressed_logs / current_turn_logs 는 지문 대신 별도 불변식으로 검사한다.


def _domain_digest(value) -> str:
    try:
        blob = _json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        blob = repr(value)
    return _hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def list_digest(items) -> str:
    """문자열 리스트의 안정적 내용 digest."""
    return _domain_digest([str(x) for x in (items or [])])


def _raw_logs_digest(raw) -> str:
    """raw_logs(types.Content 목록)의 안정적 내용 digest — role + 모든 part 텍스트.

    (B-C6) 길이·마지막 객체 id만 보면 in-place 내용 변경을 놓치므로 전 내용을 본다.
    """
    rows = []
    for c in (raw or []):
        role = getattr(c, "role", None)
        texts = []
        for part in (getattr(c, "parts", None) or []):
            texts.append(str(getattr(part, "text", "") or ""))
        rows.append([role, texts])
    return _domain_digest(rows)


def _full_logs_file_state(session) -> str:
    """sessions/{id}/full_logs.jsonl 크기(턴 로그 영속 기록 — continuation만 append)."""
    try:
        import os as _os
        from indaim.turn.rewind import FULL_LOGS
        path = _os.path.join("sessions", str(getattr(session, "session_id", "")), FULL_LOGS)
        return str(_os.path.getsize(path)) if _os.path.exists(path) else "absent"
    except Exception:
        return "unknown"


def canonical_fingerprint(session) -> dict:
    """리뷰된 커밋 소유 도메인의 지문(전체 Session 깊은 비교가 아님)."""
    fp = {d: _domain_digest(getattr(session, d, None)) for d in CANONICAL_DOMAINS}
    fp["raw_logs"] = _raw_logs_digest(getattr(session, "raw_logs", None))
    fp["full_logs_file"] = _full_logs_file_state(session)
    return fp


class PreparationView:
    """정본을 바꾸지 않는 읽기 전용 투영 뷰(WP-B quest 투영의 일반화).

    · overrides의 키는 뷰 로컬 값으로 읽기/쓰기(정본 미변경).
    · forward_writes의 키는 실제 세션에 쓴다(운영 필드 전용 — 예: manifest).
    · 그 밖의 쓰기는 거부한다(투영 경로에서 정본 쓰기 탐지).
    """

    def __init__(self, session, overrides=None, forward_writes=()):
        self.__dict__["_session"] = session
        self.__dict__["_overrides"] = dict(overrides or {})
        self.__dict__["_forward"] = frozenset(forward_writes or ())

    def __getattr__(self, name):
        ov = self.__dict__["_overrides"]
        if name in ov:
            return ov[name]
        return getattr(self.__dict__["_session"], name)

    def __setattr__(self, name, value):
        if name in self.__dict__["_overrides"]:
            self.__dict__["_overrides"][name] = value
        elif name in self.__dict__["_forward"]:
            setattr(self.__dict__["_session"], name, value)
        else:
            raise AttributeError(
                f"준비 투영은 정본 필드에 쓸 수 없습니다(읽기 전용): {name}")


class TurnPreparation:
    """하나의 자동 논리 턴 시도가 소유하는 런타임 준비/배리어 상태(§13).

    TurnTransaction.preparation 에만 매달리며 정확한 (transaction_id, logical_turn,
    attempt)를 복사해 둔다. 다른 시도의 결과는 이 객체에 붙을 수 없다.
    """

    def __init__(self, tx, *, baseline=None):
        self.transaction_id = tx.transaction_id
        self.logical_turn = tx.logical_turn
        self.attempt = tx.attempt
        self.phase = PREP_COLLECTING
        self.registry_state = REGISTRY_OPEN
        self.cost_membership = COST_OPEN
        self.frozen_cost_event_ids = None
        self.tasks: dict = {}
        self.cost_operations: list = []
        self.violations: list = []
        self.baseline_fingerprint = dict(baseline or {})
        self.baseline_uncompressed_ids = None   # B-C6: uncompressed_logs 접미 불변식 기준
        self.rebaseline_notes: list = []        # 정당한 운영 쓰기의 기준선 재설정 기록
        # ── 스테이징 결과 ──
        self.event_assessment = None
        self.narration = None               # NarrationResult(확정 묘사)
        self.staged_log = None              # dict: raw_entries/uncompressed_entries/consumed/turn_no
        self.proceed_history_entry = None   # gm_proceed_history 스테이징
        self.extraction_plan = None         # ExtractionMutationPlan
        self.extraction_text = None
        self.irregular_plan = None          # IrregularNpcMutationPlan
        self.irregular_text = None
        self.promotions: dict = {}          # name -> normalized detail
        self.growth_players = None          # ROLL 성장 투영(players 복제)
        self.growth_fail_counts = None      # ROLL 성장 투영(stat_fail_counts 복제)
        self.growth_events: list = []       # [(uid, stat, new_value)]
        # WP-F: 성장 '확정' 알림 문구 — 커밋된 성장 사실에서만 게임 채널로 방출(derived).
        self.growth_notices: list = []
        self.delivery = None                # DeliveryResult
        self.delivery_error = None
        self.failure_code = None
        self.failure_stage = None
        self.ready_proof = None
        self.processing_held = False        # 자동 턴 owner가 is_processing 해제 책임 보유
        # WP-D(D-D3): 재시도 중 새 provider 오퍼레이션 claim을 provider 호출 전에 내구 기록하는
        #   훅(op) -> None. 실패 시 예외로 claim(=호출)을 막는다. 재시도 동안에만 설정된다.
        self.durable_claim = None
        # ── H02-B: 세계 변이 lane(정본 미변경 투영) ──
        #   기준선은 ensure_preparation이 캡처한다. production 묶음은 H02에서 항상 비어 있다.
        self.world_baseline_captured = False
        self.baseline_world_version = None       # 준비 시작 시 world_version(dict|None)
        self.baseline_definition_revision = None
        self.world_mutation_batch = None         # WorldMutationBatch(PLAYER_TURN)
        self.world_registry = None               # None = production 레지스트리
        self.world_projection = None             # ProjectionResult(READY에서 동결·투영)
        self.world_projection_source = None      # 투영 원본 ProjectedWorld
        self.world_mutation_digest = None
        self.world_conflicts: list = []          # WORLD_PROJECTION_CONFLICT 진단(재시도 대상 아님)
        self.world_quiescence = None             # H03: BaseQuiescenceReport(READY 증명)
        self.world_turn = None                   # H05: core.turn_world.WorldTurn(DYNAMIC 세션만)
        self.card_variations = None              # SVG 카드: 추출층위가 고른 {ActorId: variation}(턴 로컬)
        self.identity_reveals = None             # P3 G3: 본문에서 실제 공개된 이름·얼굴 {InfoId: 문단 번호}(턴 로컬)
        self.world_observation_raw = None        # H05: 추출의 world_observation(원본 — 처리 전)
        self.world_diagnostics = []              # v7.30: 마스터 진단 기록(전수보고 층위활동 — 비권위)

    # ── 정체성 ──
    def identity(self) -> tuple:
        return (self.transaction_id, self.logical_turn, self.attempt)

    def matches(self, tx) -> bool:
        return (tx is not None and tx.transaction_id == self.transaction_id
                and tx.logical_turn == self.logical_turn and tx.attempt == self.attempt)

    # ── 작업 레지스트리 (§14) ──
    def register_task(self, name, kind, *, coro=None, **axes) -> PreparationTaskRecord:
        """필수 턴 작업을 등록한다. 봉인 이후 등록은 배리어 위반(예외)."""
        if self.registry_state != REGISTRY_OPEN:
            if coro is not None:
                coro.close()
            self.violations.append(f"late_task:{name}")
            raise BarrierViolationError(
                f"레지스트리 봉인 이후 필수 작업 등록 시도: {name} (tx={self.transaction_id})")
        if name in self.tasks and not self.tasks[name].terminal:
            if coro is not None:
                coro.close()
            raise BarrierViolationError(f"같은 이름의 작업이 이미 실행 중: {name}")
        rec = PreparationTaskRecord(
            name=name, kind=kind, transaction_id=self.transaction_id,
            logical_turn=self.logical_turn, attempt=self.attempt, **axes)
        self.tasks[name] = rec
        if coro is not None:
            rec.task = _asyncio.ensure_future(self._run(rec, coro))
        return rec

    async def _run(self, rec, coro):
        try:
            rec.result = await coro
            if rec.state == TASK_RUNNING:
                rec.state = TASK_SUCCEEDED
        except _asyncio.CancelledError:
            rec.state = TASK_CANCELLED
            rec.error = "cancelled"
        except Exception as e:  # 작업 실패는 기록만(분류에 따라 READY 판단)
            rec.state = TASK_FAILED
            rec.error = f"{type(e).__name__}: {e}"
            print(f"[WP-C/{self.transaction_id[:8]}] 준비 작업 실패 {rec.name}: {rec.error}")
        return rec.result

    def mark_task(self, name, state, *, result=None, error=None) -> None:
        rec = self.tasks.get(name)
        if rec is None:
            return
        rec.state = state
        if result is not None:
            rec.result = result
        if error is not None:
            rec.error = error

    def task_future(self, name):
        rec = self.tasks.get(name)
        return rec.task if rec is not None else None

    def running_tasks(self) -> list:
        return [r for r in self.tasks.values() if not r.terminal]

    async def join(self) -> None:
        """등록된 모든 작업이 terminal이 될 때까지 기다린다.

        부모 작업이 실행 중 자식 작업을 등록하면 다음 반복에서 함께 기다린다
        (부모가 terminal이 된 뒤에도 자식 등록이 선행되므로 누락이 없다).
        """
        while True:
            pending = [r.task for r in self.tasks.values()
                       if not r.terminal and r.task is not None]
            # B-C4: 논리 타임아웃 뒤에도 살아 있는 claim된 provider 호출도 기다린다.
            pending += self.inflight_provider_calls()
            if not pending:
                break
            await _asyncio.wait(pending)
        for r in self.tasks.values():
            if not r.terminal:     # task 없이 인라인 관리되는 기록이 비종결로 남음
                self.violations.append(f"unterminated:{r.name}")

    def inflight_provider_calls(self) -> list:
        """claim된 오퍼레이션 중 늦은 관측이 아직 끝나지 않은 provider attempt의 완료 토큰.

        (B-C5) provider future 자체가 아니라 '관측 완료' 토큰이다 — 실제 usage가 CostEvent로
        기록된 뒤에야 토큰이 완료되므로, 그 전에는 합류·동결·READY가 모두 불가하다.
        """
        out = []
        for op in self.cost_operations:
            for f in list(getattr(op, "inflight", ()) or ()):
                if not f.done():
                    out.append(f)
        return out

    def seal(self) -> None:
        """레지스트리 봉인: OPEN → CLOSING → CLOSED. 이후 등록은 위반."""
        self.registry_state = REGISTRY_CLOSING
        if any((not r.terminal) for r in self.tasks.values()):
            self.violations.append("seal_with_running_task")
        self.registry_state = REGISTRY_CLOSED

    def reopen_for_retry(self) -> None:
        """사전 READY 필수 준비 실패 후 같은 tx로의 재시도(비용 멤버십은 OPEN 유지)."""
        if self.cost_membership == COST_CLOSED:
            raise BarrierViolationError("비용 멤버십 동결 이후 재시도 불가")
        self.registry_state = REGISTRY_OPEN
        self.phase = PREP_PREPARING

    # ── 비용 멤버십 (§23~§25) ──
    def claim_cost_operation(self, op) -> bool:
        """자동 턴 provider 오퍼레이션을 이 시도의 비용 멤버로 명시 등록한다."""
        if self.cost_membership == COST_CLOSED:
            self.violations.append(f"late_cost_op:{getattr(op, 'operation', '?')}")
            raise BarrierViolationError(
                f"비용 멤버십 동결 이후 필수 provider 오퍼레이션 시작 시도: "
                f"{getattr(op, 'operation', '?')} (tx={self.transaction_id})")
        if getattr(op, "transaction_id", None) != self.transaction_id:
            # 귀속 불일치 — 멤버로 들이지 않는다(추정 금지). 진단은 남긴다.
            self.violations.append(
                f"attribution_mismatch:{getattr(op, 'operation', '?')}")
            return False
        if op not in self.cost_operations:
            if self.durable_claim is not None:
                self.durable_claim(op)          # 실패는 전파 — 내구 claim 없이 호출하지 않는다
            self.cost_operations.append(op)
        return True

    def close_cost_membership(self) -> tuple:
        """비용 관련 작업이 모두 terminal일 때만 정확한 event_id 튜플을 동결한다."""
        if self.cost_membership == COST_CLOSED:
            return self.frozen_cost_event_ids
        open_cost = [r.name for r in self.tasks.values()
                     if r.blocks_cost_closure and not r.terminal]
        if open_cost:
            raise BarrierViolationError(f"비용 관련 작업 미종결: {open_cost}")
        live = self.inflight_provider_calls()
        if live:
            # B-C4: 래퍼 타임아웃으로 논리 작업은 끝났어도 실제 호출이 살아 있으면 동결 금지.
            raise BarrierViolationError(
                f"진행 중인 provider 호출 {len(live)}건 — 비용 멤버십 동결 불가")
        if self.registry_state != REGISTRY_CLOSED:
            raise BarrierViolationError("레지스트리 봉인 전 비용 동결 불가")
        ids = []
        for op in self.cost_operations:
            for eid in getattr(op, "event_ids", ()) or ():
                if eid not in ids:
                    ids.append(eid)
        self.frozen_cost_event_ids = tuple(ids)
        self.cost_membership = COST_CLOSED
        return self.frozen_cost_event_ids

    def rebaseline(self, session, domain: str, reason: str) -> None:
        """정당한 사전 준비 운영 쓰기(예: 이전 턴 확정 pending_bgm 소비) 이후 해당 도메인의
        기준선을 재설정한다. 이유와 시점을 기록한다(무기록 완화 금지)."""
        self.baseline_fingerprint[domain] = canonical_fingerprint(session)[domain]
        import time as _time
        self.rebaseline_notes.append({"domain": domain, "reason": reason,
                                      "phase": self.phase, "at": _time.time()})

    # ── 적용 전 여부(턴 소유 정본 효과) ──
    def turn_effects_unapplied(self, tx) -> list:
        """이미 정본에 적용된 스테이징 효과 목록(비어 있어야 READY 가능)."""
        applied = []
        for nm in ("extraction_plan", "irregular_plan"):
            obj = getattr(self, nm)
            if obj is not None and getattr(obj, "applied", False):
                applied.append(nm)
        if isinstance(self.staged_log, dict) and self.staged_log.get("applied"):
            applied.append("staged_log")
        return applied


# ── 세션/트랜잭션 접근 헬퍼 ──
def get_preparation(session, transaction_id=None):
    """활성 트랜잭션의 준비 객체(없거나 id 불일치면 None)."""
    tx = _tx.get_active_transaction(session)
    if tx is None:
        return None
    if transaction_id is not None and tx.transaction_id != transaction_id:
        return None
    prep = getattr(tx, "preparation", None)
    return prep if isinstance(prep, TurnPreparation) else None


def ensure_preparation(session, transaction_id):
    """현재 활성 트랜잭션이 transaction_id이면 준비 객체를 (없으면 만들어) 반환.

    새 트랜잭션을 만들지 않는다(PF-18). stale/None이면 None.
    """
    if transaction_id is None:
        return None
    tx = _tx.get_active_transaction(session)
    if tx is None or tx.transaction_id != transaction_id:
        return None
    prep = getattr(tx, "preparation", None)
    if not isinstance(prep, TurnPreparation):
        prep = TurnPreparation(tx, baseline=canonical_fingerprint(session))
        prep.baseline_uncompressed_ids = tuple(
            id(x) for x in (getattr(session, "uncompressed_logs", None) or []))
        capture_world_baseline(session, prep)
        tx.preparation = prep
    return prep


def claim_cost_operation(session, transaction_id, op) -> bool:
    """자동 경로 provider 오퍼레이션 claim. transaction_id가 없으면(수동/인트로) no-op.

    stale(현재 활성 시도가 아님)이면 멤버로 들이지 않는다. 동결 이후면 예외.
    """
    if transaction_id is None or op is None:
        return False
    prep = ensure_preparation(session, transaction_id)
    if prep is None:
        return False
    return prep.claim_cost_operation(op)


# ── 비정규 NPC 스테이징 투영(읽기 전용) ──
def staged_irregular_entry(session, name):
    """활성 준비 객체의 스테이징 등록에서 name 항목(정본에 없을 때만)."""
    prep = get_preparation(session)
    plan = getattr(prep, "irregular_plan", None) if prep is not None else None
    if plan is None or getattr(plan, "applied", False):
        return None
    for reg in getattr(plan, "registrations", ()) or ():
        if reg.get("name") == name:
            from indaim.narrative.irregular_npc import pick_voice, DEFAULT_GENDER, DEFAULT_AGE
            return {
                "image_key": reg.get("image_key") or "",
                "voice": pick_voice(reg.get("gender"), reg.get("age")),
                "gender": reg.get("gender") or DEFAULT_GENDER,
                "age": reg.get("age") or DEFAULT_AGE,
                "context": reg.get("context") or "",
                "first_turn": reg.get("turn", 0),
            }
    return None


def projected_irregular_registry(session, prep) -> dict:
    """정본 등록부 + 스테이징 등록(register 의미: 기존 항목 유지) − 스테이징 승격."""
    from indaim.narrative import irregular_npc as _irr
    reg = copy.deepcopy(_irr.get_registry(session))
    plan = getattr(prep, "irregular_plan", None)
    if plan is not None:
        for r in plan.registrations:
            if r["name"] not in reg:
                reg[r["name"]] = staged_irregular_entry(session, r["name"]) or {}
    for name in (getattr(prep, "promotions", {}) or {}):
        reg.pop(name, None)
    return reg


def projected_npcs(session, prep) -> dict:
    """정본 npcs + 스테이징 승격(promote 의미 재현, 고유명 개명 포함)."""
    from indaim.narrative import irregular_npc as _irr
    npcs = dict(getattr(session, "npcs", {}) or {})
    base_reg = _irr.get_registry(session)
    for name, norm in (getattr(prep, "promotions", {}) or {}).items():
        entry = base_reg.get(name) or staged_irregular_entry(session, name) or {}
        merged = dict(norm["details"] or {})
        merged.setdefault("voice", entry.get("voice"))
        merged.setdefault("image_key", entry.get("image_key"))
        npcs[norm.get("final_name") or name] = merged
    return npcs


# ── ROLL 성장 스테이징 투영 ──
def growth_projection(session, prep) -> PreparationView:
    """ROLL 성장 판정이 정본 대신 스테이징 복제본을 변경하도록 하는 뷰."""
    if prep.growth_players is None:
        prep.growth_players = copy.deepcopy(getattr(session, "players", {}) or {})
    if prep.growth_fail_counts is None:
        fc = getattr(session, "stat_fail_counts", None)
        prep.growth_fail_counts = copy.deepcopy(fc) if isinstance(fc, dict) else {}
    view = PreparationView(session, overrides={
        "players": prep.growth_players,
        "stat_fail_counts": prep.growth_fail_counts,
    })
    return view


def projected_players(session):
    """활성 준비 객체에 ROLL 성장 투영이 있으면 그 players, 없으면 정본."""
    prep = get_preparation(session)
    if prep is not None and prep.growth_players is not None:
        return prep.growth_players
    return getattr(session, "players", {}) or {}


# ── H02-B: 세계 변이 lane ──
def capture_world_baseline(session, prep) -> None:
    """준비 시작 시 world_version·definition_revision 기준선과 (빈) PLAYER_TURN 묶음을 붙인다."""
    from indaim.turn import world_mutation as _wm
    prep.baseline_world_version = copy.deepcopy(getattr(session, "world_version", None))
    prep.baseline_definition_revision = getattr(session, "definition_revision", None)
    prep.world_mutation_batch = _wm.player_turn_batch(session, prep.transaction_id)
    prep.world_baseline_captured = True


def stage_world_ops(prep, ops) -> None:
    """H05 — 이 시도의 PLAYER_TURN 묶음에 typed 연산을 담는다(READY 동결 전에만, 멱등).

    준비 객체가 묶음의 유일한 owner다. 동결 이후 추가는 배리어 위반이다.
    """
    b = prep.world_mutation_batch
    if b is None or b.frozen:
        raise BarrierViolationError("동결됐거나 없는 세계 변이 묶음에는 연산을 담을 수 없습니다")
    have = {op.op_id for op in b.operations}
    for op in ops:
        if op.op_id not in have:
            b.add(op)


def _world_ready_reasons(session, prep) -> list:
    """READY 전 세계 가드(부수효과 없음): 기준선 대조 + 묶음 구조 검증."""
    reasons = []
    if not prep.world_baseline_captured or prep.world_mutation_batch is None:
        reasons.append("world:baseline_not_captured")
        return reasons
    if getattr(session, "world_version", None) != prep.baseline_world_version:
        reasons.append("world:version_changed")
    if getattr(session, "definition_revision", None) != prep.baseline_definition_revision:
        reasons.append("world:definition_revision_changed")
    b = prep.world_mutation_batch
    if b.source_world_version_id != _world_state.version_id_of(prep.baseline_world_version):
        reasons.append("world:batch_source_mismatch")
    if not b.frozen:
        errs = b.validate()
        if errs:
            reasons.append("world:batch_invalid:" + ",".join(errs))
    return reasons


def _freeze_and_project_world(session, prep) -> list:
    """묶음 동결 + 정본 미변경 투영. 충돌은 WORLD_PROJECTION_CONFLICT(재시도 대상 아님)."""
    from indaim.turn import world_mutation as _wm
    registry = prep.world_registry or _wm.PRODUCTION_REGISTRY
    digest = prep.world_mutation_batch.freeze()
    source = _wm.ProjectedWorld.capture(session)
    result = _wm.project(source, prep.world_mutation_batch, registry)
    if not result.ok:
        prep.world_conflicts = [c.to_dict() for c in result.conflicts]
        prep.failure_code = _wm.WORLD_PROJECTION_CONFLICT
        return ["world:projection_conflict:" + ",".join(c.code for c in result.conflicts)]
    # H03: 실제 base quiescence 증명 — 투영된 세계에 due ≤ now pending 예약이 남으면 READY 불가.
    #   결정적 세계 상태 문제이므로 WORLD_PROJECTION_CONFLICT 계열(재시도 대상 아님).
    from indaim.rules.time import world_schedule as _sched
    q = _sched.base_quiescence_of(result.world)
    prep.world_quiescence = q
    if not q.quiescent:
        prep.world_conflicts = [{"failure_class": _wm.WORLD_PROJECTION_CONFLICT,
                                 "code": "NOT_QUIESCENT", "reasons": list(q.reasons),
                                 "overdue_ids": list(q.overdue_ids)}]
        prep.failure_code = _wm.WORLD_PROJECTION_CONFLICT
        return ["world:not_quiescent"]
    prep.world_projection = result
    prep.world_projection_source = source
    prep.world_mutation_digest = digest
    return []


# ── READY 술어 / 전이 (단일 owner, §26) ──
def evaluate_ready(session, prep) -> list:
    """READY_TO_COMMIT 조건을 검사하고 미충족 사유 목록을 반환한다(비면 충족)."""
    reasons = []
    tx = _tx.get_active_transaction(session)
    # 정체성
    if tx is None or not prep.matches(tx):
        reasons.append("identity:not_current")
    elif _tx.is_terminal(tx.status) or tx.status == _tx.TurnStatus.READY_TO_COMMIT:
        reasons.append(f"identity:status_{tx.status.value}")
    if getattr(tx, "preparation", None) is not prep:
        reasons.append("identity:preparation_not_owned")
    # 서사
    if prep.narration is None or not getattr(prep.narration, "text", ""):
        reasons.append("narrative:no_finalized_narration")
    if not isinstance(prep.staged_log, dict):
        reasons.append("narrative:log_not_staged")
    # 출력
    if prep.delivery is None or not getattr(prep.delivery, "ok", False):
        reasons.append("output:narration_not_delivered")
    # 상태 준비
    if prep.extraction_plan is None:
        reasons.append("state:no_extraction_plan")
    elif getattr(prep.extraction_plan, "rejected_stale", False):
        reasons.append("state:extraction_stale")
    for r in prep.tasks.values():
        if r.blocks_preparation_ready and not r.terminal:
            reasons.append(f"task:running:{r.name}")
        if r.required_success and r.state != TASK_SUCCEEDED:
            reasons.append(f"task:failed:{r.name}")
        if (r.transaction_id, r.logical_turn, r.attempt) != prep.identity():
            reasons.append(f"task:foreign_identity:{r.name}")
    if tx is not None:
        applied = prep.turn_effects_unapplied(tx)
        if applied:
            reasons.append("state:effects_already_applied:" + ",".join(applied))
    # 작업 종결
    if prep.registry_state != REGISTRY_CLOSED:
        reasons.append("tasks:registry_not_closed")
    if prep.inflight_provider_calls():
        reasons.append("cost:provider_call_in_flight")
    # 정본(B-C1): 리뷰된 커밋 소유 도메인이 기준선과 다르면 READY 불가 —
    #   외부(관리자/수동 포함) 쓰기로 이 시도의 기준선은 더 이상 안정적이지 않다.
    if prep.baseline_fingerprint:
        _now = canonical_fingerprint(session)
        _changed = sorted(k for k in _now
                          if prep.baseline_fingerprint.get(k) != _now.get(k))
        if _changed:
            reasons.append("state:canonical_changed:" + ",".join(_changed))
    # B-C6: uncompressed_logs — 백그라운드 압축은 앞부분만 지울 수 있고(접미 유지),
    #   턴 로그 append는 READY 이후에만 있다 → 현재 목록은 기준선의 (동일 객체) 접미여야 한다.
    if prep.baseline_uncompressed_ids is not None:
        cur = tuple(id(x) for x in (getattr(session, "uncompressed_logs", None) or []))
        base = prep.baseline_uncompressed_ids
        if len(cur) > len(base) or cur != base[len(base) - len(cur):]:
            reasons.append("state:uncompressed_logs_changed")
    # B-C6: current_turn_logs — 이번 턴이 소비(스테이징)한 선언 로그 접두부는 READY 이후
    #   삭제될 대상이므로 그 사이 내용이 바뀌면 안 된다(뒤쪽 추가 입력은 보존 대상).
    staged = prep.staged_log
    if isinstance(staged, dict) and "consumed_digest" in staged and not staged.get("applied"):
        n = int(staged.get("consumed_turn_logs") or 0)
        if list_digest((getattr(session, "current_turn_logs", None) or [])[:n]) \
                != staged["consumed_digest"]:
            reasons.append("state:turn_input_log_changed")
    # 비용
    if prep.cost_membership != COST_CLOSED or prep.frozen_cost_event_ids is None:
        reasons.append("cost:membership_not_closed")
    if prep.violations:
        reasons.append("violations:" + ",".join(prep.violations))
    # H02-B: 세계 기준선(world_version·definition_revision)·묶음 구조
    reasons.extend(_world_ready_reasons(session, prep))
    return reasons


def transition_to_ready(session, prep):
    """유일한 READY_TO_COMMIT 전이 경로. 증명 객체 없이 상태만 바꾸지 않는다.

    Returns: (ReadyProof | None, reasons)
    """
    reasons = evaluate_ready(session, prep)
    if reasons:
        return None, reasons
    # H02-B: 세계 변이 묶음 동결·투영(정본 미변경). 충돌이면 READY 불가 — 결정적 실패이므로
    #   호출자는 NOT_READY(종료 실패)로 처리한다(추출 RETRY_PENDING 경로가 아니다).
    reasons = _freeze_and_project_world(session, prep)
    if reasons:
        return None, reasons
    ready_fp = canonical_fingerprint(session)
    base = prep.baseline_fingerprint or {}
    changed = tuple(sorted(k for k in ready_fp if base.get(k) != ready_fp.get(k)))
    delivered = tuple(getattr(prep.delivery, "canonical_message_ids", ()) or ())
    proof = ReadyProof(
        transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
        attempt=prep.attempt,
        task_states=tuple((r.name, r.kind, r.state) for r in prep.tasks.values()),
        frozen_cost_event_ids=tuple(prep.frozen_cost_event_ids),
        canonical_baseline_fingerprint=dict(base),
        canonical_ready_fingerprint=ready_fp,
        canonical_unchanged=not changed, changed_domains=changed,
        delivered_message_ids=delivered,
        extraction_entries=len(getattr(prep.extraction_plan, "entries", ()) or ()),
        world_mutation_digest=prep.world_mutation_digest,
        baseline_world_version_id=_world_state.version_id_of(prep.baseline_world_version),
        definition_revision=prep.baseline_definition_revision,
        world_changed_domains=prep.world_projection.changed_domains(prep.world_projection_source),
        base_quiescence=prep.world_quiescence.to_dict() if prep.world_quiescence else None,
    )
    assert not changed, "evaluate_ready가 정본 변화를 거부했어야 함"   # B-C1 방어
    _tx.mark_transaction_status(session, prep.transaction_id,
                                _tx.TurnStatus.READY_TO_COMMIT)
    prep.ready_proof = proof
    prep.phase = PREP_READY
    return proof, []


# ── READY 이후 legacy continuation용 스테이징 적용 헬퍼 ──
def apply_staged_narration_log(session, staged: dict) -> bool:
    """스테이징된 묘사 로그/카운터를 적용한다(READY 이후 전용, 멱등).

    기존 _execute_proceed의 순서·의미를 그대로 재현한다:
    raw_logs×2 → uncompressed×2 → 소비한 current_turn_logs 제거 → turn_count+1 → 20개 캡.
    """
    if not isinstance(staged, dict) or staged.get("applied"):
        return False
    session.raw_logs.extend(staged["raw_entries"])
    session.uncompressed_logs.extend(staged["uncompressed_entries"])
    n = int(staged.get("consumed_turn_logs") or 0)
    if n:
        del session.current_turn_logs[:n]
    session.turn_count += 1
    if len(session.raw_logs) > 20:
        session.raw_logs = session.raw_logs[-20:]
    staged["applied"] = True
    return True


def restage_roll_effects(session, transaction_id, effects) -> bool:
    """v7.12: 재서술 — 원 턴 굴림의 성장·실패 누적 효과를 새 시도의 준비 객체에 다시 스테이징한다.

    재서술은 턴 이전 상태(pre)로 복원한 뒤 같은 굴림 결과를 재사용하므로, 기록된 효과(굴림 뒤 값)를
    그대로 다시 얹으면 원 턴과 같은 정본이 된다. 주사위를 다시 굴리지 않고, 커밋 뒤 알림도 다시 내지 않는다.
    """
    if not isinstance(effects, dict) or not (effects.get("growth_events") or effects.get("fail_counts")):
        return False
    prep = ensure_preparation(session, transaction_id)
    if prep is None:
        return False
    growth_projection(session, prep)
    for key, val in (effects.get("fail_counts") or {}).items():
        prep.growth_fail_counts[key] = val
    for uid, stat, new_value in effects.get("growth_events") or []:
        prof = (prep.growth_players.get(uid) or {}).get("profile")
        if isinstance(prof, dict):
            prof[stat] = new_value
        prep.growth_events.append((uid, stat, new_value))
    return True


def apply_staged_growth(session, prep) -> bool:
    """스테이징된 ROLL 성장(profile +N, stat_fail_counts)을 정본에 적용(READY 이후)."""
    if prep.growth_fail_counts is None and not prep.growth_events:
        return False
    from indaim.turn import settle_capture as _sc
    _sc.note("growth_staged", {"events": prep.growth_events, "fail_counts": prep.growth_fail_counts})
    for uid, stat, new_value in prep.growth_events:
        player = (getattr(session, "players", {}) or {}).get(uid)
        profile = player.get("profile") if isinstance(player, dict) else None
        if isinstance(profile, dict) and stat in profile:
            profile[stat] = new_value
    if prep.growth_fail_counts is not None:
        session.stat_fail_counts = copy.deepcopy(prep.growth_fail_counts)
    prep.growth_events = []
    prep.growth_fail_counts = None
    prep.growth_players = None
    return True
