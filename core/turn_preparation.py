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
