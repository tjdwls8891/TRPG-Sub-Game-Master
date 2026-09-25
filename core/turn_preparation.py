"""WP-B — TurnPreparation: 트랜잭션 스코프 스테이징 모델.

목적(설계 불변식):
    AI/provider 결과 생산자·파서는 canonical 게임 상태를 직접 변경하지 않는다.
    자동 턴의 AI-파생 효과는 먼저 트랜잭션 스코프의 스테이징 데이터로 표현되고,
    묘사(narration)가 성립한 뒤에야 **단일 호환 적용 경계**에서 canonical에 반영된다.

이 모듈이 담당하는 것(WP-B):
    · 지시층위 효과 스테이징 — quest choice(단일 owner로 통합)·intended_case·info_ledger.
    · quest_state 투영(projection) — 스테이징 상태를 canonical 변경 없이 읽기 전용으로 제공.
    · 단일 호환 적용부(compatibility applier) — idempotent, 묘사 성공 후에만 적용.

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

from . import quest as _quest
from . import turn_transaction as _tx


# quest_state 투영 뷰·복제는 core.quest가 소유한다(순환 임포트 회피):
#   _quest.projection_view(session, quest_state), _quest.clone_state(session).


# ── info_ledger 순수 병합 ────────────────────────────────────────
_MAX_LEDGER_ITEMS = 12


def _merge_info_ledger(ledger: list, info_access: dict, turn: int) -> list:
    """info_access(new_secrets/new_leaks) 델타를 ledger 복제본에 병합해 반환한다.

    cogs.gm.GMCog._update_info_ledger의 병합 규약을 canonical 변경 없이 재현한다.
    입력 ledger는 이미 복제본이어야 한다(이 함수는 받은 리스트를 제자리 변경한다).
    """
    if not isinstance(info_access, dict):
        return ledger

    def _norm(s):
        return re.sub(r"\s+", "", (s or "")).lower()

    def _find(info):
        ni = _norm(info)
        if not ni:
            return None
        for it in ledger:
            ei = _norm(it.get("info", ""))
            if ei and (ni == ei or ni in ei or ei in ni):
                return it
        return None

    for sec in (info_access.get("new_secrets") or []):
        if not isinstance(sec, dict):
            continue
        info = (sec.get("info") or "").strip()
        if not info or _find(info):
            continue
        ledger.append({
            "info": info,
            "known_by": list(dict.fromkeys(sec.get("known_by") or [])),
            "suspected_by": list(dict.fromkeys(sec.get("suspected_by") or [])),
            "origin": (sec.get("origin") or "").strip(),
            "leaks": [],
            "turn_added": turn,
        })

    for lk in (info_access.get("new_leaks") or []):
        if not isinstance(lk, dict):
            continue
        info = (lk.get("info") or "").strip()
        to = (lk.get("to") or "").strip()
        how = (lk.get("how") or "").strip()
        if not info or not to:
            continue
        item = _find(info)
        if item is None:
            if not how:
                continue
            item = {"info": info, "known_by": [], "suspected_by": [],
                    "origin": "", "leaks": [], "turn_added": turn}
            ledger.append(item)
        if to not in item["known_by"]:
            item["known_by"].append(to)
        if to in item.get("suspected_by", []):
            item["suspected_by"] = [x for x in item["suspected_by"] if x != to]
        item.setdefault("leaks", []).append(
            f"턴{turn}: {to} — {how}" if how else f"턴{turn}: {to}")

    if len(ledger) > _MAX_LEDGER_ITEMS:
        del ledger[:len(ledger) - _MAX_LEDGER_ITEMS]
    return ledger


# ── 스테이징 데이터 ──────────────────────────────────────────────
@dataclass
class PendingInstructionEffects:
    """지시층위 결정에서 파생된, 아직 canonical에 반영되지 않은 스테이징 효과.

    검사·테스트 가능한 데이터만 담는다(임의 콜백 금지).

    필드:
        projected_quest_state: 이 지시효과가 반영된 quest_state 복제본.
            투영(narration/logic 프롬프트 읽기)과 적용의 공통 근거다. None이면 미변경.
        quest_action / quest_reason / quest_active_name: 적용 시 안내 메시지·진단용.
        info_ledger: 병합된 info_ledger 복제본. None이면 미변경.
        narrative_progress: 묘사 산출 요약에서 파생된 진행도 문자열. None이면 미변경.
        applied: 호환 적용부가 canonical에 반영했는지(중복 적용 방지).
    """
    transaction_id: str | None = None
    logical_turn: int | None = None
    attempt: int | None = None

    projected_quest_state: dict | None = None
    quest_action: str = "none"          # start | switch | keep | ignored | none
    quest_reason: str = ""
    quest_active_name: str = ""

    info_ledger: list | None = None
    narrative_progress: str | None = None

    applied: bool = False
    diagnostics: dict = field(default_factory=dict)

    def has_state_effect(self) -> bool:
        """canonical에 반영할 게임 상태 효과가 있는지(진단·메시지 제외)."""
        return (self.projected_quest_state is not None
                or self.info_ledger is not None
                or self.narrative_progress is not None)


def _stage_quest(session, decision, pending: PendingInstructionEffects,
                 base_state=None) -> None:
    """quest choice(통합 단일 owner) + intended_case를 quest_state 복제본에 계산한다.

    기존 두 경로를 통합한다:
      · core.quest.apply_choice — ctx(active/none/switch) 기반 start/switch/keep/ignored.
      · cogs.gm._apply_quest_choice — narrative_mode 가드 + 시나리오 random 선정.
    canonical quest_state는 건드리지 않고 복제본 위 투영 뷰에서 기존 함수를 재사용한다.
    """
    # WP-C(B-C2): 같은 tx의 이전 스테이징 투영이 있으면 그 위에 순차 합성한다
    #   (덮어쓰기로 앞선 결정의 효과를 잃지 않는다 — 결정을 순서대로 적용한 결과와 등가).
    proj = copy.deepcopy(base_state) if base_state is not None else _quest.clone_state(session)
    view = _quest.projection_view(session, proj)

    changed = base_state is not None

    # 풀자유(free) 세션은 서사설계자가 주도한다 — quest 선택 스테이징 안 함.
    if getattr(session, "narrative_mode", "quest") == "quest":
        choice = decision.get("quest_choice")

        # 시나리오가 quest_select=random이고 진행 중 퀘스트가 없으면 코드가 무작위 선정.
        mode = (getattr(session, "scenario_data", {}) or {}).get("quest_select") or "logic"
        if mode == "random" and not proj.get("active"):
            offered = _quest.offered_ids(view)
            if offered:
                import random as _r
                choice = {"id": _r.choice(offered), "reason": "무작위 선정"}

        if choice:
            res = _quest.apply_choice(view, choice)
            pending.quest_action = res.get("action", "none")
            pending.quest_reason = res.get("reason", "")
            if res.get("applied"):
                changed = True
                active = (view.quest_state.get("active") or {})
                pending.quest_active_name = active.get("name", "")

    # 지시층위가 지정한 진전 방향(intended_case) — 진전 자체는 추출 수치가 결정.
    if decision.get("quest_case"):
        key = _quest.set_intended_case(view, decision["quest_case"])
        if key is not None:
            changed = True
        pending.diagnostics["intended_case"] = key

    if changed:
        pending.projected_quest_state = view.quest_state


def _stage_info_ledger(session, decision, pending: PendingInstructionEffects,
                       base_ledger=None) -> None:
    """info_access 델타를 info_ledger 복제본에 병합해 스테이징한다.

    WP-C(B-C2): base_ledger(같은 tx의 이전 스테이징)가 있으면 그 위에 합성한다.
    """
    if base_ledger is not None:
        pending.info_ledger = copy.deepcopy(base_ledger)
    ia = decision.get("info_access") or {}
    if not isinstance(ia, dict) or not ia:
        return
    base = base_ledger if base_ledger is not None else getattr(session, "info_ledger", None)
    if not isinstance(base, list):
        base = []
    ledger = copy.deepcopy(base)
    turn = int(getattr(session, "turn_count", 0) or 0) + 1
    merged = _merge_info_ledger(ledger, ia, turn)
    # 실제 변화가 있을 때만 스테이징(불필요한 적용/저장 방지).
    if merged != base:
        pending.info_ledger = merged


def stage_instruction_effects(session, decision, *, transaction_id=None) -> PendingInstructionEffects:
    """지시층위 decision을 canonical 변경 없이 스테이징한다(단일 진입점).

    quest choice/intended_case/info_ledger를 복제본 위에서 계산해 반환한다.
    이 함수 호출만으로는 어떤 canonical 게임 상태도 바뀌지 않는다.
    """
    active = _tx.get_active_transaction(session)
    pending = PendingInstructionEffects(
        transaction_id=transaction_id,
        logical_turn=getattr(active, "logical_turn", None),
        attempt=getattr(active, "attempt", None),
    )
    # WP-C(B-C2) 합성 규칙: 같은 활성 시도에 아직 적용되지 않은 이전 스테이징이 있으면
    #   그 투영(quest_state/info_ledger)을 기준으로 이번 결정을 순차 합성한다.
    #   · quest: 이전 투영 위에서 apply_choice/intended_case(후행 결정이 우선, 무변경이면 유지)
    #   · info_ledger: 이전 병합본 위에 이번 델타 병합
    #   · 안내 메시지(quest_action/reason/name): 이번 결정이 적용되면 이번 것, 아니면 이전 것
    prev = getattr(active, "instruction_result", None) if active is not None else None
    if not (isinstance(prev, PendingInstructionEffects) and not prev.applied
            and (prev.logical_turn, prev.attempt) == (pending.logical_turn, pending.attempt)):
        prev = None
    try:
        _stage_quest(session, decision or {}, pending,
                     base_state=(prev.projected_quest_state if prev is not None else None))
    except Exception as e:  # 스테이징 실패는 canonical을 오염시키지 않는다.
        pending.diagnostics["quest_error"] = str(e)
    try:
        _stage_info_ledger(session, decision or {}, pending,
                           base_ledger=(prev.info_ledger if prev is not None else None))
    except Exception as e:
        pending.diagnostics["info_error"] = str(e)
    if prev is not None:
        if (pending.quest_action not in ("start", "switch")
                and prev.quest_action in ("start", "switch")):
            pending.quest_action = prev.quest_action
            pending.quest_reason = prev.quest_reason
            pending.quest_active_name = prev.quest_active_name
        if pending.narrative_progress is None:
            pending.narrative_progress = prev.narrative_progress
        pending.diagnostics = {**prev.diagnostics, **pending.diagnostics,
                               "composed_with_previous": True}
    # 활성 트랜잭션에 최신 스테이징 상태를 얹는다(소유권 조기 고정용 슬롯).
    # 같은 논리 턴의 후속 프롬프트(투영)와 묘사 성공 후 적용이 이 값을 읽는다.
    if active is not None:
        active.instruction_result = pending
    return pending


def pending_for(session) -> "PendingInstructionEffects | None":
    """세션의 활성 트랜잭션에 스테이징된 지시효과(없으면 None)."""
    tx = _tx.get_active_transaction(session)
    p = getattr(tx, "instruction_result", None) if tx is not None else None
    return p if isinstance(p, PendingInstructionEffects) else None


def projected_quest_state(session):
    """활성 트랜잭션의 스테이징을 반영한 quest_state 투영(없으면 canonical)."""
    return project_quest_state(session, pending_for(session))


def project_quest_state(session, pending: PendingInstructionEffects | None):
    """narration/logic 프롬프트가 읽을 quest_state 투영(읽기 전용)을 반환한다.

    스테이징된 quest 효과가 있으면 그 투영본을, 없으면 canonical get_state 결과를
    돌려준다. 어느 경우에도 canonical은 변경하지 않는다.
    """
    if pending is not None and pending.projected_quest_state is not None:
        return pending.projected_quest_state
    return _quest.get_state(session)


# ── 단일 호환 적용 경계 ──────────────────────────────────────────
def apply_instruction_effects(session, pending: PendingInstructionEffects | None) -> dict:
    """스테이징된 지시층위 효과를 canonical에 반영한다(단일 owner, idempotent).

    묘사가 성립한 뒤에만 호출한다. 같은 pending을 두 번 적용해도 이중 반영되지
    않는다(applied 마커). 안내 메시지는 이 함수가 아니라 호출부가 반환값으로 낸다.

    Returns:
        {"applied": bool, "quest_action": str, "quest_active_name": str,
         "info_changed": bool, "narrative_progress": bool}
    """
    result = {"applied": False, "quest_action": "none", "quest_reason": "",
              "quest_active_name": "", "info_changed": False,
              "narrative_progress": False}
    if pending is None or pending.applied or not pending.has_state_effect():
        if pending is not None:
            pending.applied = True
        return result

    if pending.projected_quest_state is not None:
        session.quest_state = pending.projected_quest_state
        result["quest_action"] = pending.quest_action
        result["quest_reason"] = pending.quest_reason
        result["quest_active_name"] = pending.quest_active_name

    if pending.info_ledger is not None:
        session.info_ledger = pending.info_ledger
        result["info_changed"] = True

    if pending.narrative_progress is not None:
        apply_narrative_progress(session, pending.narrative_progress)
        result["narrative_progress"] = True

    pending.applied = True
    result["applied"] = True
    return result


def apply_narrative_progress(session, progress_text: str) -> bool:
    """narrative_plan.current_event.progress 갱신의 단일 owner.

    canonical narrative_plan은 persisted·rewind-tracked·future-read 상태이므로,
    AI-파생 진행도 갱신도 이 경계를 통해서만 반영한다.
    """
    plan = getattr(session, "narrative_plan", None)
    if isinstance(plan, dict) and plan.get("current_event"):
        plan["current_event"]["progress"] = (progress_text or "")[:150]
        return True
    return False


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
    item_deltas: list = _field(default_factory=list)      # [{target,item,delta}]
    npcs_met: list = _field(default_factory=list)
    companions_joined: list = _field(default_factory=list)
    companions_left: list = _field(default_factory=list)
    world_new_tl: dict | None = None
    location_before: str | None = None
    location_after: str | None = None
    location_moved: bool = False
    quest_progress: dict = _field(default_factory=dict)   # 정규화된 좁은 DTO
    secret_awareness: object = None
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
        from .utils import get_merged_status_effects
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
    from . import extraction as _ex

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

    # 퀘스트 진전(좁은 정규화 DTO) — 진전/완료 판정은 적용부의 advance_quest.
    qp = r.get("quest_progress")
    plan.quest_progress = qp if isinstance(qp, dict) else ({} if qp is None else {"value": qp})

    # 이면정보 인지 점수.
    plan.secret_awareness = r.get("secret_awareness")

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
    if plan.quest_progress:
        plan.entries.append({"domain": "quest", "target": "active",
                             "op": "progress", "payload": {}})
    if plan.secret_awareness is not None:
        plan.entries.append({"domain": "secret", "target": "active",
                             "op": "awareness", "payload": {}})

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
            "context": (text or "")[:120], "turn": turn,
        })
    if plan.rejected:
        plan.diagnostics["rejected"] = list(plan.rejected)
    return plan


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
#  자동 서사 재계획 — narrative_plan 교체 변이 계획 (AUD-065)
#  provider가 만든 서사 계획 raw JSON이 곧바로 session.narrative_plan이 되지
#  않도록, 구조 검증·정규화된 후보(NarrativePlanMutationPlan)를 만든다.
#  plan_version / last_planned_turn은 코드 소유 metadata이므로 provider 값은
#  폐기하고, 호환 적용 직전에 코드가 부여한다.
#  (narrative_plan.current_event.progress 갱신은 apply_narrative_progress가
#   계속 단일 owner다 — 이 계획은 '전체 계획 교체' 경로만 다룬다.)
# ══════════════════════════════════════════════════════════════════════

# 현재 제품 스키마(NARRATIVE_PLAN_SCHEMA)와 실제 리더가 읽는 필드.
_NARRATIVE_OBJECT_FIELDS = {
    "mid_plan": ("title", "overview", "milestones", "end_condition"),
    "current_event": ("title", "summary", "resolution_direction", "progress"),
    "next_event": ("title", "summary", "trigger"),
}
_NARRATIVE_REQUIRED = ("mid_plan", "current_event", "next_event")
_NARRATIVE_CODE_OWNED = ("plan_version", "last_planned_turn")


@dataclass
class NarrativePlanMutationPlan:
    """서사 계획 교체의 검증·정규화된 후보 — 적용의 유일 권위.

    · normalized: 구조 검증을 통과한 계획(스키마 필드만). 코드 소유 metadata 없음.
    · rejected: 구조 탈락 사유(있으면 normalized=None, 적용부 도달 불가).
    · trigger_reason / full_replan: 재계획 사유·모드(멱등 키 구성 요소).
    · transaction_id / logical_turn / attempt: 원인 시도 정체성(스케줄 시점 복사).
    """
    trigger_reason: str = ""
    full_replan: bool = True
    transaction_id: str | None = None
    logical_turn: int | None = None
    attempt: int | None = None
    normalized: dict | None = None
    rejected: list = _field(default_factory=list)
    diagnostics: dict = _field(default_factory=dict)
    applied: bool = False
    rejected_stale: bool = False


def normalize_narrative_plan(raw) -> tuple:
    """provider 서사 계획 raw dict를 구조 검증·정규화한다(순수 — 세션 미참조).

    · 최상위가 dict가 아니거나 필수 객체(mid_plan/current_event/next_event)가
      dict가 아니면 거부한다.
    · 각 객체는 스키마 필드만 통과시킨다. 문자열 필드는 str일 때만, milestones는
      list일 때 문자열 항목만 남긴다. 형이 틀린 필드는 버린다(리더 기본값 유지).
    · planner_notes는 str일 때만. 그 밖의 최상위 키(코드 소유 metadata 포함)는 버린다.

    Returns:
        (normalized: dict | None, reasons: list[str], dropped: list[str])
    """
    if not isinstance(raw, dict):
        return None, ["not_object"], []
    reasons = [f"missing_or_invalid:{k}" for k in _NARRATIVE_REQUIRED
               if not isinstance(raw.get(k), dict)]
    if reasons:
        return None, reasons, []

    out, dropped = {}, []
    for key, fields in _NARRATIVE_OBJECT_FIELDS.items():
        src = raw.get(key) or {}
        obj = {}
        for f in fields:
            if f not in src:
                continue
            v = src[f]
            if f == "milestones":
                if isinstance(v, list):
                    obj[f] = [m for m in v if isinstance(m, str)]
                else:
                    dropped.append(f"{key}.{f}")
            elif isinstance(v, str):
                obj[f] = v
            else:
                dropped.append(f"{key}.{f}")
        out[key] = obj
    if isinstance(raw.get("planner_notes"), str):
        out["planner_notes"] = raw["planner_notes"]
    for k in raw:
        if k not in _NARRATIVE_OBJECT_FIELDS and k != "planner_notes":
            dropped.append(k)   # 코드 소유 metadata·미지 키는 provider 권위가 아니다
    return out, [], dropped


def build_narrative_plan(raw, *, trigger_reason, full_replan, transaction_id=None,
                         logical_turn=None, attempt=None) -> NarrativePlanMutationPlan:
    """raw 서사 계획 → 검증·정규화된 NarrativePlanMutationPlan(무변이)."""
    normalized, reasons, dropped = normalize_narrative_plan(raw)
    plan = NarrativePlanMutationPlan(
        trigger_reason=trigger_reason, full_replan=bool(full_replan),
        transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt,
        normalized=normalized, rejected=list(reasons))
    if dropped:
        plan.diagnostics["dropped"] = dropped
    return plan


def narrative_replan_key(plan: NarrativePlanMutationPlan) -> str | None:
    """자동 재계획 변이의 안정 정체성 — (원인 시도, 사유, 모드).

    같은 원인 시도에서 같은 사유·모드로 만든 재계획은 하나의 논리 변이다.
    원인 시도 정체성이 없으면 None(멱등 판정 불가 — 자동 경로에선 발생하지 않음).
    """
    if plan.transaction_id:
        origin = f"tx:{plan.transaction_id}"
    elif plan.logical_turn is not None:
        origin = f"lt:{plan.logical_turn}:{plan.attempt or 0}"
    else:
        return None
    return f"{origin}|{plan.trigger_reason}|{'full' if plan.full_replan else 'moment'}"


# ══════════════════════════════════════════════════════════════════════
#  WP-C — 트랜잭션 소유 동시 준비(Concurrent Preparation) + READY_TO_COMMIT 배리어
#
#  확정 묘사(finalized narration) 이후 전달/스트리밍·추출·비정규 NPC·자동 재계획 등
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
TASK_NARRATIVE_REPLAN = "NARRATIVE_REPLAN"

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
PREP_CONTINUED = "CONTINUED"            # READY 이후 legacy continuation 완료
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


# 되감기/커밋 소유 정본 도메인(§27). 백그라운드 압축 도메인(compressed_memory,
# uncompressed_logs 앞부분 삭제)과 운영 집계(total_cost 등)는 의도적으로 제외한다.
CANONICAL_DOMAINS = (
    "quest_state", "info_ledger", "resources", "statuses", "world_timeline",
    "visited_places", "companions", "met_npcs", "irregular_npcs", "npcs",
    "narrative_plan", "pending_ending", "last_extraction", "players",
    "stat_fail_counts", "turn_count", "gm_turns_done", "last_recorded_turn",
    "gm_proceed_history",
)


def _domain_digest(value) -> str:
    try:
        blob = _json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        blob = repr(value)
    return _hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def canonical_fingerprint(session) -> dict:
    """리뷰된 커밋 소유 도메인의 지문(전체 Session 깊은 비교가 아님)."""
    fp = {d: _domain_digest(getattr(session, d, None)) for d in CANONICAL_DOMAINS}
    # quest_state는 리더(get_state)가 빈 dict를 기본 형태로 지연 정규화한다 —
    # 의미 변화가 아니므로 정규화된 형태로 비교한다.
    fp["quest_state"] = _domain_digest(_quest.clone_state(session))
    raw = getattr(session, "raw_logs", None) or []
    fp["raw_logs"] = f"{len(raw)}:{id(raw[-1]) if raw else 0}"
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
        # ── 스테이징 결과 ──
        self.event_assessment = None
        self.narration = None               # NarrationResult(확정 묘사)
        self.staged_log = None              # dict: raw_entries/uncompressed_entries/consumed/turn_no
        self.proceed_history_entry = None   # gm_proceed_history 스테이징
        self.narrative_progress = None      # current_event.progress 텍스트
        self.narrative_marker = False       # last_planned_turn 마커 스테이징
        self.replan_candidate = None        # NarrativePlanMutationPlan
        self.extraction_plan = None         # ExtractionMutationPlan
        self.extraction_text = None
        self.irregular_plan = None          # IrregularNpcMutationPlan
        self.irregular_text = None
        self.promotions: dict = {}          # name -> normalized detail
        self.growth_players = None          # ROLL 성장 투영(players 복제)
        self.growth_fail_counts = None      # ROLL 성장 투영(stat_fail_counts 복제)
        self.growth_events: list = []       # [(uid, stat, new_value)]
        self.delivery = None                # DeliveryResult
        self.delivery_error = None
        self.failure_code = None
        self.failure_stage = None
        self.ready_proof = None
        self.processing_held = False        # 자동 턴 owner가 is_processing 해제 책임 보유

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
        """claim된 오퍼레이션 중 아직 terminal이 아닌 실제 provider 호출(future)."""
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

    # ── 적용 전 여부(턴 소유 정본 효과) ──
    def turn_effects_unapplied(self, tx) -> list:
        """이미 정본에 적용된 스테이징 효과 목록(비어 있어야 READY 가능)."""
        applied = []
        pending = getattr(tx, "instruction_result", None)
        if isinstance(pending, PendingInstructionEffects) and pending.applied:
            applied.append("instruction_effects")
        for nm in ("extraction_plan", "irregular_plan", "replan_candidate"):
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
            from .irregular_npc import pick_voice, DEFAULT_GENDER, DEFAULT_AGE
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
    from . import irregular_npc as _irr
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
    from . import irregular_npc as _irr
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
    # 비용
    if prep.cost_membership != COST_CLOSED or prep.frozen_cost_event_ids is None:
        reasons.append("cost:membership_not_closed")
    if prep.violations:
        reasons.append("violations:" + ",".join(prep.violations))
    return reasons


def transition_to_ready(session, prep):
    """유일한 READY_TO_COMMIT 전이 경로. 증명 객체 없이 상태만 바꾸지 않는다.

    Returns: (ReadyProof | None, reasons)
    """
    reasons = evaluate_ready(session, prep)
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


def apply_staged_growth(session, prep) -> bool:
    """스테이징된 ROLL 성장(profile +N, stat_fail_counts)을 정본에 적용(READY 이후)."""
    if prep.growth_fail_counts is None and not prep.growth_events:
        return False
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
