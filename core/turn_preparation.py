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


def _stage_quest(session, decision, pending: PendingInstructionEffects) -> None:
    """quest choice(통합 단일 owner) + intended_case를 quest_state 복제본에 계산한다.

    기존 두 경로를 통합한다:
      · core.quest.apply_choice — ctx(active/none/switch) 기반 start/switch/keep/ignored.
      · cogs.gm._apply_quest_choice — narrative_mode 가드 + 시나리오 random 선정.
    canonical quest_state는 건드리지 않고 복제본 위 투영 뷰에서 기존 함수를 재사용한다.
    """
    proj = _quest.clone_state(session)
    view = _quest.projection_view(session, proj)

    changed = False

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


def _stage_info_ledger(session, decision, pending: PendingInstructionEffects) -> None:
    """info_access 델타를 info_ledger 복제본에 병합해 스테이징한다."""
    ia = decision.get("info_access") or {}
    if not isinstance(ia, dict) or not ia:
        return
    base = getattr(session, "info_ledger", None)
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
    try:
        _stage_quest(session, decision or {}, pending)
    except Exception as e:  # 스테이징 실패는 canonical을 오염시키지 않는다.
        pending.diagnostics["quest_error"] = str(e)
    try:
        _stage_info_ledger(session, decision or {}, pending)
    except Exception as e:
        pending.diagnostics["info_error"] = str(e)
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
    """추출 결과에서 파생된, 검증·정규화된 변이 계획.

    · result: 파싱된 추출 결과(원본, result-only 생산물).
    · entries: 정규화·검증된 후보 변이 항목(도메인/타깃/연산). 검사·dedup·conflict용.
    · conflicts: 상호 모순 항목 진단.
    실제 canonical 반영은 단일 호환 적용 경계(_run_extraction 내 표시된 구획)에서
    기존 도메인 mutator를 재사용해 수행한다(WP-D가 대체할 임시 경계).
    """
    transaction_id: str | None = None
    logical_turn: int | None = None
    attempt: int | None = None
    result: dict = _field(default_factory=dict)
    entries: list = _field(default_factory=list)
    conflicts: list = _field(default_factory=list)
    diagnostics: dict = _field(default_factory=dict)
    applied: bool = False
    rejected_stale: bool = False


def extraction_is_stale(session, *, logical_turn, attempt) -> bool:
    """추출의 (logical_turn, attempt)보다 더 새로운 논리 시도가 활성화됐으면 True.

    §26/§38 — 늦게 도착한 추출이 더 새로운 논리 턴/시도에 기록되는 것을 막는다.
    커밋 후 새 턴이 아직 없으면(active=None) stale이 아니다(정상 적용 대상).
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

    도메인별 후보를 정규화하고, 등록 캐릭터·병합 상태이상 목록으로 1차 검증하며,
    동치 중복을 제거하고 상호 모순(같은 타깃·상태의 부여/해제 동시 등)을 진단한다.
    실제 임계 비교·적용은 단일 호환 적용 경계의 기존 mutator가 수행한다(권위 검증 보존).
    """
    plan = ExtractionMutationPlan(
        transaction_id=transaction_id, logical_turn=logical_turn,
        attempt=attempt, result=result if isinstance(result, dict) else {})
    r = plan.result
    valid_chars = _valid_char_names(session)
    valid_status = _valid_status_names(session)
    seen = set()

    def _add(domain, target, op, payload):
        key = (domain, target, op, repr(payload))
        if key in seen:               # 동치 중복 제거(T-B17)
            return
        seen.add(key)
        plan.entries.append({"domain": domain, "target": target,
                             "op": op, "payload": payload})

    # 위치(장소 이동) — 이름만 정규화(해상도/방문은 적용부가 처리).
    loc = (r.get("location") or {}).get("name") if isinstance(r.get("location"), dict) else None
    if loc:
        _add("location", loc, "move", {})

    # 상태이상 후보(점수는 적용부가 임계 비교) — 등록 캐릭터·병합 상태이상만.
    status_targets = {}
    for e in (r.get("status_scores") or []):
        if not isinstance(e, dict):
            continue
        t, s = e.get("target"), e.get("status")
        if not t or not s:
            continue
        if valid_chars and t not in valid_chars:
            plan.diagnostics.setdefault("dropped_status", []).append(f"{t};{s}")
            continue
        if valid_status is not None and s not in valid_status:
            plan.diagnostics.setdefault("dropped_status", []).append(f"{t};{s}")
            continue
        _add("status", t, "score", {"status": s, "score": e.get("score")})
        status_targets.setdefault((t, s), []).append(e.get("score"))

    # 소지품 증감 후보 — 등록 캐릭터만.
    for e in (r.get("item_changes") or []):
        if not isinstance(e, dict):
            continue
        t = e.get("target")
        if not t or (valid_chars and t not in valid_chars):
            continue
        _add("item", t, "delta",
             {"item": e.get("item"), "delta": e.get("delta")})

    # 만난 NPC.
    for n in (r.get("npcs_met") or []):
        if isinstance(n, str) and n:
            _add("npc_met", n, "meet", {})

    # 동행 합류/이탈.
    comp = r.get("companions") or {}
    if isinstance(comp, dict):
        for n in (comp.get("joined") or []):
            if isinstance(n, str) and n:
                _add("companion", n, "join", {})
        for n in (comp.get("left") or []):
            if isinstance(n, str) and n:
                _add("companion", n, "leave", {})

    # 퀘스트 진전 의도(진전 여부·완료는 적용부가 판정).
    qp = r.get("quest_progress")
    if qp:
        _add("quest", "active", "progress", {"quest_progress": qp})

    # 이면정보 인지 점수(임계 비교는 적용부).
    if "secret_awareness" in r:
        _add("secret", "active", "awareness", {"score": r.get("secret_awareness")})

    # 상호 모순 진단: 같은 (타깃, 동행) join & leave 동시.
    joined = {e["target"] for e in plan.entries
              if e["domain"] == "companion" and e["op"] == "join"}
    left = {e["target"] for e in plan.entries
            if e["domain"] == "companion" and e["op"] == "leave"}
    for t in (joined & left):
        plan.conflicts.append({"domain": "companion", "target": t,
                               "reason": "join_and_leave"})

    return plan
