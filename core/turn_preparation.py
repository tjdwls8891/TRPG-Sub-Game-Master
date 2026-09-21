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


# ── quest_state 읽기 전용 투영 뷰 ────────────────────────────────
class _QuestProjectionView:
    """quest_state만 복제본으로 대체하고, 그 외 읽기는 실제 세션에 위임하는 뷰.

    기존 core.quest 함수(apply_choice/set_intended_case/start_quest 등)를 복제본
    위에서 그대로 재사용하기 위한 얇은 프록시다. canonical session.quest_state를
    변경하지 않으며(§14 금지된 mutate-then-undo 아님), quest_state 외의 canonical
    필드 쓰기는 거부해 투영이 읽기 전용임을 강제한다.
    """

    def __init__(self, session, quest_state):
        # __setattr__ 우회 — 내부 슬롯 직접 설정.
        self.__dict__["_session"] = session
        self.__dict__["quest_state"] = quest_state

    def __getattr__(self, name):
        # __dict__에 없을 때만 호출된다 → 실제 세션으로 위임(읽기).
        return getattr(self.__dict__["_session"], name)

    def __setattr__(self, name, value):
        if name == "quest_state":
            self.__dict__["quest_state"] = value
            return
        raise AttributeError(
            f"quest 투영 뷰는 canonical 필드에 쓸 수 없습니다(읽기 전용): {name}")


def _clone_quest_state(session) -> dict:
    """현재 canonical quest_state의 정규화된 깊은 복제본을 만든다.

    core.quest.get_state의 초기화 규약(active/cleared/known_secrets/occurrences)을
    복제본에 적용하되 canonical은 건드리지 않는다.
    """
    st = getattr(session, "quest_state", None)
    st = copy.deepcopy(st) if isinstance(st, dict) else {}
    st.setdefault("active", None)
    st.setdefault("cleared", [])
    st.setdefault("known_secrets", [])
    st.setdefault("occurrences", {})
    return st


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
    proj = _clone_quest_state(session)
    view = _QuestProjectionView(session, proj)

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
    return pending


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
    result = {"applied": False, "quest_action": "none",
              "quest_active_name": "", "info_changed": False,
              "narrative_progress": False}
    if pending is None or pending.applied or not pending.has_state_effect():
        if pending is not None:
            pending.applied = True
        return result

    if pending.projected_quest_state is not None:
        session.quest_state = pending.projected_quest_state
        result["quest_action"] = pending.quest_action
        result["quest_active_name"] = pending.quest_active_name

    if pending.info_ledger is not None:
        session.info_ledger = pending.info_ledger
        result["info_changed"] = True

    if pending.narrative_progress is not None:
        _apply_narrative_progress(session, pending.narrative_progress)
        result["narrative_progress"] = True

    pending.applied = True
    result["applied"] = True
    return result


def _apply_narrative_progress(session, progress_text: str) -> bool:
    """narrative_plan.current_event.progress 갱신의 단일 owner.

    canonical narrative_plan은 persisted·rewind-tracked·future-read 상태이므로,
    AI-파생 진행도 갱신도 이 경계를 통해서만 반영한다.
    """
    plan = getattr(session, "narrative_plan", None)
    if isinstance(plan, dict) and plan.get("current_event"):
        plan["current_event"]["progress"] = (progress_text or "")[:150]
        return True
    return False
