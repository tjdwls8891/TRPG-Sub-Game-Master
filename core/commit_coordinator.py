# core.commit_coordinator — WP-D 권위적 커밋 · 복구 · 청구 전환
#
# [소유권]
#   정상 자동 턴의 '정본 게임 상태 커밋'과 '플레이어 청구'를 결정하는 단일 owner.
#   READY_TO_COMMIT(WP-C 증명) 이후 이 모듈만이 스테이징 효과를 정본에 적용하고,
#   이미 VERIFIED된 기반을 다음 순서로 조합한다(WP_D_MASTER_HANDOFF §7):
#
#     A  현재 tx·정체성·READY 증명 재검증 → COMMITTING → 불변 CommitPlan 동결
#        → 동결 exact CostEvent-ID(prep.frozen_cost_event_ids)만으로 후보 COMMITTED
#          Settlement를 '메모리에서만' 구성·검증(§6.1 — 아직 영속하지 않는다)
#     B  CommitJournal PREPARED (복구 입력: 정체성·settlement_id·exact ID·청구 유저·
#        기준선/대상 커밋 표식·연결 정보)
#     C  롤백 스냅샷 → 부수효과 없는 메모리 적용(Discord/통계/tolerant save 없음)
#     D  (C와 같은 io 임계구역) strict 세션 저장 → SESSION_PERSISTED
#     E  최종 Settlement 영속 → 되감기/전체 로그 연결(실패 시 공백 표식)
#        → InkTransaction(유저별 결정적 ID) → BILLING_APPLIED → COMMITTED
#     F  (호출자) 파생 알림·보고·통계·디스플레이 → 다음 라운드
#
#   영속 이전 실패: 메모리 복원 + FAILED_SYSTEM Settlement(청구 0) + 런타임 FAILED_SYSTEM.
#   영속 이후 실패: 롤백하지 않는다. 복구 대기(세션 차단) → 같은 결정적 입력으로 재개.
#
# [재시작 복구] recover_session()이 저널·Settlement·계정 사실과 data.json의
#   commit_marker를 대조해 처분을 '실행'한다(CommitJournal.classify_recovery는 순수 분류).
#
# 기반 스키마(Settlement/InkTransaction/CommitJournal/CostLedger/accounts)는 바꾸지 않는다.

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum

from . import accounts as _accounts
from . import commit_journal as cj
from . import cost_ledger as _cl
from . import ink_transactions as _ink
from . import io as _io
from . import rewind as _rw
from . import settlement as _st
from . import turn_preparation as TP
from . import turn_transaction as TT

# 추출 재시도 컨텍스트 표식(cogs/gm.py와 공유) — 이 표식이 있으면 같은 tx 준비 owner로 재개.
RETRY_MODE_PREPARATION = "wp_c_preparation"

COMMIT_PLAN_VERSION = 1


# ══════════════════════════════════════════════════════════════
#  결과 어휘
# ══════════════════════════════════════════════════════════════

class CommitOutcome(str, Enum):
    COMMITTED = "COMMITTED"                    # durable 저널 COMMITTED까지 완료
    FAILED_PRE_PERSIST = "FAILED_PRE_PERSIST"  # 정본 영속 전 실패 — 복원·청구 0
    RECOVERY_PENDING = "RECOVERY_PENDING"      # 정본은 영속됨 — 재무/확정 복구 대기(차단)
    REJECTED = "REJECTED"                      # stale/중복/READY 아님 — 아무것도 바꾸지 않음


class DerivedEffects:
    """커밋 적용 중 수집되는 파생 부수효과(COMMITTED 이후에만 방출).

    적용 헬퍼는 Discord 송신·통계 쓰기 대신 여기에 기술자만 쌓는다.
    """

    def __init__(self):
        self.items: list = []

    def master(self, content=None, *, embed=None):
        self.items.append(("master", content, embed))

    def game(self, content, *, view_factory=None):
        self.items.append(("game", content, view_factory))

    def stats(self, uid, **deltas):
        self.items.append(("stats", str(uid), dict(deltas)))

    def npcs(self, uid, names):
        self.items.append(("npcs", str(uid), list(names or [])))

    def kinds(self) -> list:
        return [it[0] for it in self.items]


@dataclass
class CommitResult:
    outcome: CommitOutcome
    reason: str = ""
    settlement: object = None
    plan: object = None
    derived: DerivedEffects = field(default_factory=DerivedEffects)
    error: str | None = None
    audit_error: str | None = None


# ══════════════════════════════════════════════════════════════
#  불변 CommitPlan (§6.2 / §12)
# ══════════════════════════════════════════════════════════════

def _digest(obj) -> str:
    try:
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        blob = repr(obj)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _markers_equal(a, b) -> bool:
    return _digest(a) == _digest(b)


@dataclass(frozen=True)
class CommitPlan:
    """READY 시도 1건의 불변 커밋 입력. PREPARED 저널 metadata로 그대로 영속된다.

    재시작 후 old in-memory tx/prep 없이 이 값만으로 Settlement 재구성·청구 재개·
    정본 신원 대조·WP-E 연결 재구성이 가능해야 한다.
    """
    session_id: str
    transaction_id: str
    logical_turn: int
    attempt: int
    settlement_id: str
    frozen_cost_event_ids: tuple        # 정확한 비용 멤버십(WP-C 동결) — 유일 입력
    billing_user_ids: tuple             # 청구 유저 스냅샷(정규화)
    story_turn: int                     # 커밋 후 turn_count
    gm_turn: int                        # 커밋 후 gm_turns_done(되감기 델타 턴 번호)
    baseline_marker: object             # 커밋 전 commit_marker(dict|None)
    player_declaration: str
    judgment_ref: str | None
    narration_digest: str
    canonical_message_ids: tuple
    media_message_ids: tuple
    ready_fingerprint_digest: str
    task_states: tuple

    def to_dict(self) -> dict:
        return {
            "plan_version": COMMIT_PLAN_VERSION,
            "session_id": self.session_id,
            "transaction_id": self.transaction_id,
            "logical_turn": self.logical_turn,
            "attempt": self.attempt,
            "settlement_id": self.settlement_id,
            "frozen_cost_event_ids": list(self.frozen_cost_event_ids),
            "billing_user_ids": list(self.billing_user_ids),
            "story_turn": self.story_turn,
            "gm_turn": self.gm_turn,
            "baseline_marker": copy.deepcopy(self.baseline_marker),
            "player_declaration": self.player_declaration,
            "judgment_ref": self.judgment_ref,
            "narration_digest": self.narration_digest,
            "canonical_message_ids": list(self.canonical_message_ids),
            "media_message_ids": list(self.media_message_ids),
            "ready_fingerprint_digest": self.ready_fingerprint_digest,
            "task_states": [list(t) for t in self.task_states],
        }

    @property
    def fingerprint(self) -> str:
        return _digest(self.to_dict())

    def target_marker(self) -> dict:
        """이 시도가 커밋되면 data.json에 함께 영속되는 대상 신원."""
        return {
            "transaction_id": self.transaction_id,
            "attempt": self.attempt,
            "logical_turn": self.logical_turn,
            "settlement_id": self.settlement_id,
            "plan_fingerprint": self.fingerprint,
            "story_turn": self.story_turn,
        }

    def journal_metadata(self) -> dict:
        md = self.to_dict()
        md["plan_fingerprint"] = self.fingerprint
        md["target_marker"] = self.target_marker()
        return md

    @classmethod
    def from_journal_metadata(cls, md: dict) -> "CommitPlan":
        if not isinstance(md, dict) or md.get("plan_version") != COMMIT_PLAN_VERSION:
            raise ValueError("PREPARED metadata가 CommitPlan이 아님")
        plan = cls(
            session_id=md["session_id"],
            transaction_id=md["transaction_id"],
            logical_turn=int(md["logical_turn"]),
            attempt=int(md["attempt"]),
            settlement_id=md["settlement_id"],
            frozen_cost_event_ids=tuple(md["frozen_cost_event_ids"]),
            billing_user_ids=tuple(md["billing_user_ids"]),
            story_turn=int(md["story_turn"]),
            gm_turn=int(md["gm_turn"]),
            baseline_marker=copy.deepcopy(md.get("baseline_marker")),
            player_declaration=md.get("player_declaration", ""),
            judgment_ref=md.get("judgment_ref"),
            narration_digest=md.get("narration_digest", ""),
            canonical_message_ids=tuple(md.get("canonical_message_ids") or ()),
            media_message_ids=tuple(md.get("media_message_ids") or ()),
            ready_fingerprint_digest=md.get("ready_fingerprint_digest", ""),
            task_states=tuple(tuple(t) for t in (md.get("task_states") or ())),
        )
        if md.get("plan_fingerprint") != plan.fingerprint:
            raise ValueError("PREPARED plan_fingerprint 불일치(손상)")
        return plan

    def identity(self) -> "_st.TransactionIdentity":
        return _st.TransactionIdentity(session_id=self.session_id,
                                       transaction_id=self.transaction_id,
                                       logical_turn=self.logical_turn,
                                       attempt=self.attempt)


def build_commit_plan(session, tx, prep) -> CommitPlan:
    """READY 증명과 준비 객체에서 불변 CommitPlan을 만든다(가변 합계 미사용)."""
    proof = prep.ready_proof
    staged = prep.staged_log if isinstance(prep.staged_log, dict) else {}
    users = _st.normalize_billing_user_ids(list((getattr(session, "players", {}) or {}).keys()))
    jr = getattr(tx, "judgment_result", None)
    return CommitPlan(
        session_id=session.session_id,
        transaction_id=prep.transaction_id,
        logical_turn=int(prep.logical_turn),
        attempt=int(prep.attempt),
        settlement_id=_st.settlement_id_for(prep.transaction_id, prep.attempt),
        frozen_cost_event_ids=tuple(prep.frozen_cost_event_ids),
        billing_user_ids=users,
        story_turn=int(staged.get("turn_no") or int(session.turn_count) + 1),
        gm_turn=int(getattr(session, "gm_turns_done", 0) or 0) + 1,
        baseline_marker=copy.deepcopy(getattr(session, "commit_marker", None)),
        player_declaration=str(getattr(tx, "player_declaration", "") or ""),
        judgment_ref=_digest(jr) if jr else None,
        narration_digest=_digest(getattr(prep.narration, "text", "") or ""),
        canonical_message_ids=tuple(proof.delivered_message_ids),
        media_message_ids=tuple(getattr(prep.delivery, "media_message_ids", ()) or ()),
        ready_fingerprint_digest=_digest(proof.canonical_ready_fingerprint),
        task_states=tuple(proof.task_states),
    )


# ══════════════════════════════════════════════════════════════
#  롤백 스냅샷 (§7 Phase C / §9)
# ══════════════════════════════════════════════════════════════

_ABSENT = object()

# 커밋이 쓰는 세션 필드 전수(정본 도메인 + 로그/게이트/라운드 카운터/멱등 기록).
#   tests: 실제 적용 전후 직렬화 차이가 이 집합의 부분집합임을 검사한다.
COMMIT_OWNED_FIELDS = tuple(dict.fromkeys(TP.CANONICAL_DOMAINS + (
    "raw_logs", "uncompressed_logs", "current_turn_logs",
    "extraction_pending", "extraction_retry_ctx",
    "gm_clarify_count", "gm_narrate_count", "gm_side_note",
    "_extraction_applied_tx", "_narrative_replan_applied",
)))


def capture_rollback_snapshot(session, prep) -> dict:
    fields = {}
    for f in COMMIT_OWNED_FIELDS:
        v = getattr(session, f, _ABSENT)
        if v is _ABSENT:
            fields[f] = _ABSENT
        elif f == "raw_logs":
            fields[f] = list(v)            # Content 객체는 변이되지 않는다(목록만 바뀜)
        else:
            fields[f] = copy.deepcopy(v)
    pending = TP.pending_for(session)
    prep_state = {
        "staged_applied": (prep.staged_log.get("applied")
                           if isinstance(prep.staged_log, dict) else None),
        "plans": {nm: getattr(getattr(prep, nm), "applied", False)
                  for nm in ("extraction_plan", "irregular_plan", "replan_candidate")
                  if getattr(prep, nm, None) is not None},
        "pending_applied": getattr(pending, "applied", None) if pending is not None else None,
        "growth": (copy.deepcopy(prep.growth_events),
                   copy.deepcopy(prep.growth_fail_counts),
                   copy.deepcopy(prep.growth_players)),
    }
    return {"fields": fields, "prep": prep_state, "pending": pending}


def restore_rollback_snapshot(session, prep, snap) -> None:
    for f, v in snap["fields"].items():
        if v is _ABSENT:
            if f in getattr(session, "__dict__", {}):
                try:
                    delattr(session, f)
                except Exception:
                    pass
            continue
        cur = getattr(session, f, None)
        if isinstance(cur, list) and isinstance(v, list):
            cur[:] = v                     # 목록 정체성 보존(압축 등 외부 참조)
        else:
            setattr(session, f, v)
    ps = snap["prep"]
    if isinstance(prep.staged_log, dict) and ps["staged_applied"] is not None:
        prep.staged_log["applied"] = ps["staged_applied"]
    for nm, flag in ps["plans"].items():
        obj = getattr(prep, nm, None)
        if obj is not None:
            obj.applied = flag
    if snap["pending"] is not None and ps["pending_applied"] is not None:
        snap["pending"].applied = ps["pending_applied"]
    prep.growth_events, prep.growth_fail_counts, prep.growth_players = ps["growth"]


# ══════════════════════════════════════════════════════════════
#  공용 조립 헬퍼
# ══════════════════════════════════════════════════════════════

class _NullLedger:
    """비용 원장이 부착되지 않은 봇(테스트 등) — 빈 멤버십만 허용."""

    def get_cost_events_by_ids_strict(self, event_ids, **_k):
        ids = list(event_ids)
        if ids:
            raise _cl.CostEventNotFoundError(f"원장 없음 — event_id 확인 불가: {ids}")
        return []


def _ledger_of(bot):
    led = _cl.get_ledger(bot)
    return led if led is not None else _NullLedger()


def journal_for(session_id) -> "cj.CommitJournal":
    return cj.CommitJournal(cj.default_journal_path(str(session_id)))


def settlement_store_for(session_id) -> "_st.SettlementStore":
    return _st.SettlementStore(_st.default_settlement_path(str(session_id)))


def _entry(plan_or_ident, phase, *, settlement_id, metadata=None, error_code=None):
    return cj.new_entry(
        transaction_id=plan_or_ident.transaction_id,
        session_id=str(plan_or_ident.session_id),
        logical_turn=plan_or_ident.logical_turn,
        attempt=plan_or_ident.attempt,
        phase=phase, settlement_id=settlement_id,
        metadata=metadata or {}, error_code=error_code)


def build_committed_settlement(bot, plan: CommitPlan):
    """PREPARED의 exact 입력으로 COMMITTED Settlement를 (재)구성한다 — 결정적."""
    return _st.build_turn_settlement(
        cost_ledger=_ledger_of(bot), transaction_identity=plan.identity(),
        outcome=_st.SettlementOutcome.COMMITTED,
        cost_event_ids=plan.frozen_cost_event_ids,
        billing_user_ids=plan.billing_user_ids)


def persist_failed_settlement(bot, session_id, identity, cost_event_ids, billing_user_ids):
    """FAILED_SYSTEM Settlement(청구 0)를 exact ID로 구성·영속한다. 실패는 전파."""
    if cost_event_ids is None:
        raise _st.SettlementError("비용 멤버십이 동결되지 않아 exact FAILED_SYSTEM 정산 불가")
    s = _st.build_turn_settlement(
        cost_ledger=_ledger_of(bot), transaction_identity=identity,
        outcome=_st.SettlementOutcome.FAILED_SYSTEM,
        cost_event_ids=tuple(cost_event_ids), billing_user_ids=billing_user_ids)
    assert s.charge_ink_per_user == 0
    return settlement_store_for(session_id).record_strict(s)


def settle_failed_attempt(bot, session, prep):
    """사전 READY 종료 실패(전달/생성/READY 불가) → FAILED_SYSTEM Settlement(§10).

    Returns: (settlement | None, error | None). 계정은 절대 건드리지 않는다.
    """
    try:
        ident = _st.TransactionIdentity(session_id=session.session_id,
                                        transaction_id=prep.transaction_id,
                                        logical_turn=int(prep.logical_turn),
                                        attempt=int(prep.attempt))
        users = _st.normalize_billing_user_ids(
            list((getattr(session, "players", {}) or {}).keys()))
        s = persist_failed_settlement(bot, session.session_id, ident,
                                      prep.frozen_cost_event_ids, users)
        return s, None
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        _io.write_log(session.session_id, "error",
                      f"[WP-D] FAILED_SYSTEM 정산 기록 실패 tx={prep.transaction_id} — {msg}")
        return None, msg


_COMMIT_LOCKS: dict = {}


def _commit_lock(session_id) -> asyncio.Lock:
    key = str(session_id)
    if key not in _COMMIT_LOCKS:
        _COMMIT_LOCKS[key] = asyncio.Lock()
    return _COMMIT_LOCKS[key]


def _mark_rewind_degraded(session, turn) -> None:
    lst = list(getattr(session, "rewind_degraded_turns", None) or [])
    if turn not in lst:
        lst.append(turn)
    session.rewind_degraded_turns = lst
    print(f"⚠️ [WP-D/{session.session_id}] 턴 {turn} 되감기 기록 공백 — 이 구간 되감기 비안전")


# ══════════════════════════════════════════════════════════════
#  CommitCoordinator
# ══════════════════════════════════════════════════════════════

class CommitCoordinator:
    """정상 자동 턴의 유일한 커밋 owner."""

    def __init__(self, bot):
        self.bot = bot

    # ── 공개 진입점 ──────────────────────────────────────────
    async def commit_ready_turn(self, session, prep, *, apply_effects,
                                state_before=None) -> CommitResult:
        """READY_TO_COMMIT 시도를 권위적으로 커밋한다.

        apply_effects: async (session, prep, derived) — 도메인 스테이징 효과를 부수효과
            없이 메모리에 적용한다(Discord/통계/세션 저장 금지 — 파생은 derived에 기술).
        """
        tx = TT.get_active_transaction(session)
        if (prep is None or tx is None or not prep.matches(tx)
                or getattr(tx, "preparation", None) is not prep):
            return CommitResult(CommitOutcome.REJECTED, reason="stale_or_not_current")
        if (prep.phase != TP.PREP_READY or prep.ready_proof is None
                or tx.status != TT.TurnStatus.READY_TO_COMMIT):
            return CommitResult(CommitOutcome.REJECTED,
                                reason=f"not_ready:{prep.phase}/{tx.status.value}")
        # 동기 claim — 같은 이벤트 루프의 중복 호출은 이 시점 이후 REJECTED.
        prep.phase = TP.PREP_COMMITTING
        async with _commit_lock(session.session_id):
            return await self._commit_locked(session, tx, prep, apply_effects, state_before)

    # ── 본체 ────────────────────────────────────────────────
    async def _commit_locked(self, session, tx, prep, apply_effects, state_before):
        tid = prep.transaction_id
        # ── Phase A: 재검증 + 동결 ──
        reasons = []
        applied = prep.turn_effects_unapplied(tx)
        if applied:
            reasons.append("effects_already_applied:" + ",".join(applied))
        now_fp = TP.canonical_fingerprint(session)
        ready_fp = prep.ready_proof.canonical_ready_fingerprint
        changed = sorted(k for k in now_fp if ready_fp.get(k) != now_fp.get(k))
        if changed:
            reasons.append("canonical_changed_since_ready:" + ",".join(changed))
        if prep.frozen_cost_event_ids is None:
            reasons.append("cost_membership_not_frozen")
        if reasons:
            return await self._fail_pre_persist(session, prep, stage="COMMIT_VALIDATION",
                                                error="; ".join(reasons), prepared=False)
        TT.mark_transaction_status(session, tid, TT.TurnStatus.COMMITTING)
        try:
            plan = build_commit_plan(session, tx, prep)
            candidate = build_committed_settlement(self.bot, plan)   # 메모리 후보만
        except Exception as e:  # noqa: BLE001
            return await self._fail_pre_persist(
                session, prep, stage="SETTLEMENT_VALIDATION",
                error=f"{type(e).__name__}: {e}", prepared=False)

        # ── Phase B: durable intent ──
        journal = journal_for(session.session_id)
        try:
            journal.append_entry(_entry(plan, cj.CommitPhase.PREPARED,
                                        settlement_id=plan.settlement_id,
                                        metadata=plan.journal_metadata()))
        except Exception as e:  # noqa: BLE001
            return await self._fail_pre_persist(
                session, prep, stage="PREPARED_APPEND",
                error=f"{type(e).__name__}: {e}", prepared=False, plan=plan)

        # ── Phase C+D: 롤백 스냅샷 → 부수효과 없는 적용 → strict 저장(한 임계구역) ──
        before = state_before
        if before is None:
            before = getattr(session, "_rewind_snapshot", None) or _rw.capture_state(session)
        snap = capture_rollback_snapshot(session, prep)
        derived = DerivedEffects()
        stage, err, rewind = "APPLY", None, None
        persisted = False
        async with _io.session_io_lock(self.bot, session):
            session._commit_io_task = asyncio.current_task()
            try:
                rewind = await self._apply_in_memory(session, prep, plan, candidate,
                                                     derived, apply_effects, before)
                stage = "STRICT_SAVE"
                await _io.write_session_strict_locked(session)
                persisted = True
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                restore_rollback_snapshot(session, prep, snap)
            finally:
                session._commit_io_task = None
        if not persisted:
            return await self._fail_pre_persist(session, prep, stage=stage, error=err,
                                                prepared=True, plan=plan)

        # ── SESSION_PERSISTED — 여기부터 이야기는 정본이다(롤백 금지) ──
        pending = {"plan": plan, "derived": derived, "rewind": rewind,
                   "rewind_done": False, "settlement": candidate}
        session._commit_pending = pending
        try:
            journal.append_entry(_entry(plan, cj.CommitPhase.SESSION_PERSISTED,
                                        settlement_id=plan.settlement_id,
                                        metadata={"plan_fingerprint": plan.fingerprint}))
        except Exception as e:  # noqa: BLE001
            return self._recovery_pending(session, prep, plan, derived,
                                          "SESSION_PERSISTED_APPEND", e)
        try:
            settlement, _created = await self._complete_from_persisted(
                session, plan, journal, {cj.CommitPhase.PREPARED,
                                         cj.CommitPhase.SESSION_PERSISTED},
                pending=pending)
        except Exception as e:  # noqa: BLE001
            return self._recovery_pending(session, prep, plan, derived, "FINALIZE", e)
        self._finalize_runtime(session, tid, prep)
        return CommitResult(CommitOutcome.COMMITTED, settlement=settlement,
                            plan=plan, derived=derived)

    async def _apply_in_memory(self, session, prep, plan, candidate, derived,
                               apply_effects, before):
        """부수효과 없는 정본 적용(Discord/통계/계정/tolerant save 없음). 되감기 델타 반환."""
        TP.apply_staged_narration_log(session, prep.staged_log)
        TP.apply_staged_growth(session, prep)
        await apply_effects(session, prep, derived)
        session.gm_clarify_count = 0
        session.gm_narrate_count = 0
        session.gm_turns_done = int(getattr(session, "gm_turns_done", 0) or 0) + 1
        session.gm_side_note = ""
        # Settlement 파생 호환 미러 — 이야기와 같은 원자 쓰기로 영속(재시도 시 이중 증가 없음)
        charge = int(candidate.charge_ink_per_user)
        session.total_ink_spent = int(getattr(session, "total_ink_spent", 0) or 0) + charge
        session.last_turn_cost = float(candidate.player_billable_cost_krw)
        session.last_turn_ink = charge
        # 되감기 델타(파일 기록은 영속 이후) — 계산 실패는 공백 표식(이야기와 함께 영속)
        rewind = None
        turn_no = session.gm_turns_done
        if turn_no > int(getattr(session, "last_recorded_turn", 0) or 0):
            try:
                after = _rw.capture_state(session)
                changes = _rw.diff_state(before, after)
                entries = _rw.serialize_log_entries(session.raw_logs[-2:])
                rewind = (turn_no, changes, entries)
                session._rewind_snapshot = after
                session.last_recorded_turn = turn_no
            except Exception as e:  # noqa: BLE001
                print(f"[WP-D] 되감기 델타 계산 실패: {e}")
                _mark_rewind_degraded(session, turn_no)
        session.commit_marker = plan.target_marker()
        return rewind

    async def _complete_from_persisted(self, session, plan, journal, phases, *,
                                       pending=None, settlement=None):
        """SESSION_PERSISTED 이후 공통 완료(커밋 경로·복구 경로 공유). 실패는 전파."""
        store = settlement_store_for(session.session_id)
        # E-1 최종 Settlement — 결정적 재구성(후보가 있으면 동일 payload)
        if settlement is None:
            cand = (pending or {}).get("settlement") or build_committed_settlement(self.bot, plan)
            settlement = store.record_strict(cand)
        if settlement.outcome != _st.SettlementOutcome.COMMITTED.value:
            raise _st.SettlementConflictError("영속 이후 Settlement가 COMMITTED가 아님")
        # E-2 되감기/전체 로그 연결(WP-E 소비) — 실패는 공백 표식, 이야기 롤백 없음
        if cj.CommitPhase.REWIND_RECORDED not in phases:
            done = bool((pending or {}).get("rewind_done"))
            rewind = (pending or {}).get("rewind")
            if not done and rewind is not None:
                turn, changes, entries = rewind
                ok1 = _rw.record_delta(session, turn, changes,
                                       cost_krw=settlement.player_billable_cost_krw)
                ok2 = _rw.record_full_log(session, turn, entries)
                if pending is not None:
                    pending["rewind_done"] = True
                if ok1 and ok2:
                    done = True
                else:
                    _mark_rewind_degraded(session, turn)
            elif not done:
                _mark_rewind_degraded(session, plan.gm_turn)
            if done:
                try:
                    journal.append_entry(_entry(plan, cj.CommitPhase.REWIND_RECORDED,
                                                settlement_id=plan.settlement_id,
                                                metadata={"turn": plan.gm_turn}))
                except Exception as e:  # noqa: BLE001
                    print(f"[WP-D] REWIND_RECORDED 기록 실패(비결정 마커): {e}")
        # E-3 InkTransaction — 유저별 결정적 ID, 재실행 시 이중 차감 없음
        if cj.CommitPhase.BILLING_APPLIED not in phases:
            results = await _ink.execute_settlement_charges(settlement)
            d = (pending or {}).get("derived")
            od = [u for u, res in (results or {}).items()
                  if res.status == _ink.EXEC_APPLIED
                  and getattr(res.transaction, "overdraft", False)]
            if d is not None and od:
                # 기존 제품 안내 보존(legacy 루프의 1잉크 하한 알림) — InkTransaction 사실에서 파생.
                d.master(f"ℹ️ 예상 초과분이 발생해 잔액을 1잉크로 맞췄습니다. "
                         f"(차감 {settlement.charge_ink_per_user}잉크 · {len(od)}명 · "
                         f"초과분 운영자 부담)")
            journal.append_entry(_entry(plan, cj.CommitPhase.BILLING_APPLIED,
                                        settlement_id=plan.settlement_id,
                                        metadata={"charge_ink_per_user": settlement.charge_ink_per_user,
                                                  "billing_user_ids": list(settlement.billing_user_ids)}))
        # E-4 durable COMMITTED
        created = journal.append_entry(_entry(plan, cj.CommitPhase.COMMITTED,
                                              settlement_id=plan.settlement_id,
                                              metadata={"plan_fingerprint": plan.fingerprint,
                                                        "story_turn": plan.story_turn,
                                                        "gm_turn": plan.gm_turn}))
        if pending is not None:
            pending["settlement"] = settlement
            d = pending.get("derived")
            if d is not None and settlement.charge_ink_per_user > 0:
                for uid in settlement.billing_user_ids:
                    d.stats(uid, ink_spent=settlement.charge_ink_per_user)
        return settlement, created

    def _finalize_runtime(self, session, tid, prep) -> None:
        """durable COMMITTED 이후에만 런타임 COMMITTED·활성 포인터 정리."""
        TT.finalize(session, tid, TT.TurnStatus.COMMITTED)
        if prep is not None:
            prep.phase = TP.PREP_CONTINUED
        session.commit_recovery = None
        session._commit_pending = None

    def _recovery_pending(self, session, prep, plan, derived, stage, exc) -> CommitResult:
        """정본 영속 이후 실패 — 롤백·FAILED_SYSTEM 금지. 세션 차단 + 복구 대기."""
        msg = f"{type(exc).__name__}: {exc}"
        session.commit_recovery = {"transaction_id": plan.transaction_id,
                                   "attempt": plan.attempt, "stage": stage,
                                   "error": msg, "status": "PENDING"}
        _io.write_log(session.session_id, "error",
                      f"[WP-D] 영속 이후 커밋 미완료 tx={plan.transaction_id} stage={stage} — {msg}")
        print(f"⚠️ [WP-D/{session.session_id}] 커밋 복구 대기 stage={stage}: {msg}")
        return CommitResult(CommitOutcome.RECOVERY_PENDING, reason=stage, plan=plan,
                            derived=derived, error=msg)

    async def _fail_pre_persist(self, session, prep, *, stage, error, prepared,
                                plan=None) -> CommitResult:
        """정본 영속 전 실패 — (메모리는 이미 복원됨) FAILED_SYSTEM·청구 0."""
        tid = prep.transaction_id
        settlement, audit = settle_failed_attempt(self.bot, session, prep)
        if audit is not None and prepared:
            # PREPARED는 있는데 종결 사실(FAILED_SYSTEM Settlement)이 없다 — 미해결 시도를
            # 남긴 채 다음 커밋이 진행되면 복구가 모호해지므로 차단하고 재시도한다.
            session.commit_recovery = {"transaction_id": tid, "attempt": prep.attempt,
                                       "stage": "FAILED_SETTLEMENT_PENDING",
                                       "error": audit, "status": "PENDING"}
        code = (TT.FailureCode.PERSISTENCE_FAILURE
                if stage in ("STRICT_SAVE", "PREPARED_APPEND")
                else TT.FailureCode.COMMIT_VALIDATION_FAILURE)
        TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                    failure_stage=f"COMMIT_{stage}", failure_code=code,
                    failure_message=error)
        prep.phase = TP.PREP_FAILED
        prep.failure_stage = f"COMMIT_{stage}"
        _io.write_log(session.session_id, "error",
                      f"[WP-D] 커밋 영속 전 실패 tx={tid} stage={stage} — {error}")
        print(f"⚠️ [WP-D/{session.session_id}] 커밋 실패(영속 전) stage={stage}: {error}")
        return CommitResult(CommitOutcome.FAILED_PRE_PERSIST, reason=stage,
                            settlement=settlement, plan=plan, error=error,
                            audit_error=audit)


# ══════════════════════════════════════════════════════════════
#  복구 실행기 (§8 / §17)
# ══════════════════════════════════════════════════════════════

@dataclass
class RecoveryReport:
    status: str                       # CLEAN | RESOLVED | PENDING | RECOVERY_REQUIRED
    transaction_id: str | None = None
    action: str = ""                  # DISCARDED | FINALIZED | ...
    detail: str = ""
    settlement: object = None
    derived: object = None
    committed_now: bool = False

    @property
    def blocked(self) -> bool:
        return self.status in ("PENDING", "RECOVERY_REQUIRED")


class _RecoveryRequired(Exception):
    pass


def _group_attempts(entries) -> list:
    groups: dict = {}
    for e in entries:
        groups.setdefault((e.transaction_id, e.attempt), []).append(e)
    return list(groups.values())


def _append_recovery_required(journal, first_entry, code) -> None:
    try:
        journal.append_entry(cj.new_entry(
            transaction_id=first_entry.transaction_id, session_id=first_entry.session_id,
            logical_turn=first_entry.logical_turn, attempt=first_entry.attempt,
            phase=cj.CommitPhase.RECOVERY_REQUIRED,
            settlement_id=first_entry.settlement_id, error_code=str(code)[:120]))
    except Exception as e:  # noqa: BLE001
        print(f"[WP-D] RECOVERY_REQUIRED 기록 실패(증거는 기존 저널에 보존): {e}")


def _block(session, report: RecoveryReport) -> RecoveryReport:
    session.commit_recovery = {"transaction_id": report.transaction_id,
                               "status": report.status, "stage": report.action,
                               "error": report.detail}
    _io.write_log(session.session_id, "error",
                  f"[WP-D] 커밋 복구 {report.status} tx={report.transaction_id} — {report.detail}")
    print(f"⚠️ [WP-D/{session.session_id}] 커밋 복구 {report.status}: {report.detail}")
    return report


async def recover_session(bot, session, *, context: str = "restart") -> RecoveryReport:
    """저널·Settlement·계정·data.json 표식을 대조해 미완료 커밋 처분을 실행한다.

    재시작 시에는 복원된 세션(=data.json)과 durable 사실만 쓴다(구 런타임 tx 불신).
    실행 중(in-process) 호출은 같은 규칙에 더해, 남아 있는 런타임 커밋 스태시를
    파생 효과·되감기 기록 재개에 쓴다.
    """
    journal = journal_for(session.session_id)
    store = settlement_store_for(session.session_id)
    try:
        entries = journal.list_entries()
    except Exception as e:  # noqa: BLE001 — 손상/상충: 추정 금지
        return _block(session, RecoveryReport("RECOVERY_REQUIRED", action="JOURNAL_UNREADABLE",
                                              detail=f"{type(e).__name__}: {e}"))
    incomplete = []
    try:
        for g in _group_attempts(entries):
            disp = cj.classify_disposition(g)
            if disp == cj.RecoveryDisposition.COMPLETE:
                continue
            if disp == cj.RecoveryDisposition.RECOVERY_REQUIRED:
                return _block(session, RecoveryReport(
                    "RECOVERY_REQUIRED", transaction_id=g[0].transaction_id,
                    action="JOURNAL_RECOVERY_REQUIRED",
                    detail="저널에 RECOVERY_REQUIRED 사실이 있습니다(운영자 확인 필요)"))
            prepared = next((e for e in g if e.phase == cj.CommitPhase.PREPARED), None)
            existing = store.find_settlement(prepared.settlement_id) if prepared else None
            if (existing is not None
                    and existing.outcome == _st.SettlementOutcome.FAILED_SYSTEM.value):
                if disp == cj.RecoveryDisposition.DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT:
                    continue                      # 종결된 폐기 시도
                _append_recovery_required(journal, g[0], "FAILED_SETTLEMENT_AFTER_PERSIST")
                return _block(session, RecoveryReport(
                    "RECOVERY_REQUIRED", transaction_id=g[0].transaction_id,
                    action="CONFLICT", detail="영속된 이야기에 FAILED_SYSTEM Settlement가 있음"))
            incomplete.append((g, disp, prepared, existing))
    except Exception as e:  # noqa: BLE001
        return _block(session, RecoveryReport("RECOVERY_REQUIRED", action="STORE_UNREADABLE",
                                              detail=f"{type(e).__name__}: {e}"))
    if not incomplete:
        if getattr(session, "commit_recovery", None) and context != "restart":
            session.commit_recovery = None
        return RecoveryReport("CLEAN")
    if len(incomplete) > 1:
        for g, *_ in incomplete:
            _append_recovery_required(journal, g[0], "MULTIPLE_INCOMPLETE_ATTEMPTS")
        return _block(session, RecoveryReport(
            "RECOVERY_REQUIRED", action="MULTIPLE_INCOMPLETE",
            detail=f"미완료 시도 {len(incomplete)}건"))

    g, disp, prepared, existing = incomplete[0]
    tid = g[0].transaction_id
    try:
        if prepared is None:
            raise _RecoveryRequired("PREPARED 없는 이력")
        try:
            plan = CommitPlan.from_journal_metadata(prepared.metadata)
        except Exception as e:  # noqa: BLE001
            raise _RecoveryRequired(f"PREPARED 계획 손상: {e}") from e
        phases = {e.phase for e in g}
        marker = getattr(session, "commit_marker", None)
        target = plan.target_marker()
        pending = getattr(session, "_commit_pending", None)
        if not (isinstance(pending, dict) and pending.get("plan") is not None
                and pending["plan"].transaction_id == tid):
            pending = None
        coord = CommitCoordinator(bot)

        if disp == cj.RecoveryDisposition.DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT:
            if _markers_equal(marker, target):
                # 크래시 창: data.json 교체 후 SESSION_PERSISTED 미기록 → 수리 후 재개
                journal.append_entry(_entry(plan, cj.CommitPhase.SESSION_PERSISTED,
                                            settlement_id=plan.settlement_id,
                                            metadata={"plan_fingerprint": plan.fingerprint}))
                phases.add(cj.CommitPhase.SESSION_PERSISTED)
            elif _markers_equal(marker, plan.baseline_marker):
                if existing is not None:
                    raise _RecoveryRequired("미영속 시도에 COMMITTED Settlement 존재")
                s = persist_failed_settlement(bot, session.session_id, plan.identity(),
                                              plan.frozen_cost_event_ids,
                                              plan.billing_user_ids)
                if context != "restart":
                    session.commit_recovery = None
                session._commit_pending = None
                return RecoveryReport("RESOLVED", transaction_id=tid, action="DISCARDED",
                                      detail="정본 미영속 시도 폐기(청구 0)", settlement=s)
            else:
                raise _RecoveryRequired("data.json 커밋 표식이 기준선/대상 어느 쪽과도 불일치")

        # 여기부터 SESSION_PERSISTED — 이야기는 정본. 대상 신원을 재확인한다.
        if not _markers_equal(marker, target):
            raise _RecoveryRequired("SESSION_PERSISTED인데 data.json 표식이 대상과 불일치")
        settlement = None
        if cj.CommitPhase.BILLING_APPLIED in phases:
            try:
                settlement = store.get_settlement_strict(plan.settlement_id)
            except _st.SettlementNotFoundError as e:
                raise _RecoveryRequired("BILLING_APPLIED인데 Settlement 없음") from e
            if settlement.charge_ink_per_user > 0:
                for uid in settlement.billing_user_ids:
                    m = await _accounts.get_applied_ink_marker(
                        uid, _ink.expected_ink_tx_id(settlement.settlement_id, uid))
                    if m is None:
                        raise _RecoveryRequired(
                            f"BILLING_APPLIED인데 유저 {uid} 적용 마커 없음(재차감 금지)")
                await _ink.execute_settlement_charges(settlement)   # 원장 복구만(멱등)
        try:
            settlement, created = await coord._complete_from_persisted(
                session, plan, journal, phases, pending=pending, settlement=settlement)
        except (_st.SettlementConflictError, _st.CostEventAlreadySettledError,
                _ink.InkTransactionConflictError, _ink.InkTransactionCorruptionError,
                cj.CommitJournalConflictError) as e:
            raise _RecoveryRequired(f"{type(e).__name__}: {e}") from e
    except _RecoveryRequired as e:
        if tid:
            _append_recovery_required(journal, g[0], str(e))
        return _block(session, RecoveryReport("RECOVERY_REQUIRED", transaction_id=tid,
                                              action="CONFLICT", detail=str(e)))
    except Exception as e:  # noqa: BLE001 — 일시 실패: 차단 유지·재시도
        return _block(session, RecoveryReport("PENDING", transaction_id=tid,
                                              action="RETRY", detail=f"{type(e).__name__}: {e}"))

    derived = pending.get("derived") if pending is not None else None
    if created and derived is None and settlement.charge_ink_per_user > 0:
        # 재시작 복구 — 파생 통계(청구 잉크)는 COMMITTED를 여기서 처음 기록한 경우에만
        derived = DerivedEffects()
        for uid in settlement.billing_user_ids:
            derived.stats(uid, turns=1, ink_spent=settlement.charge_ink_per_user)
    # 런타임 정리(in-process) — 활성 tx가 이 시도면 durable COMMITTED 이후에만 표기
    active = TT.get_active_transaction(session)
    if active is not None and active.transaction_id == tid:
        coord._finalize_runtime(session, tid, getattr(active, "preparation", None))
    session.commit_recovery = None
    session._commit_pending = None
    try:
        await _io.save_session_data(bot, session)   # 공백 표식 등 파생 영속(비권위)
    except Exception:
        pass
    return RecoveryReport("RESOLVED", transaction_id=tid, action="FINALIZED",
                          detail="영속 이후 커밋 재개·확정", settlement=settlement,
                          derived=derived, committed_now=bool(created))


async def emit_stats_effects(derived) -> None:
    """파생 통계 방출(재시작 복구 등 Discord 채널 없이 가능한 부분)."""
    if derived is None:
        return
    from . import stats as _stats
    for it in derived.items:
        try:
            if it[0] == "stats":
                await _stats.bump(it[1], **it[2])
            elif it[0] == "npcs":
                await _stats.add_npcs(it[1], it[2])
        except Exception as e:  # noqa: BLE001
            print(f"[통계] 파생 기록 실패(커밋 무관): {e}")


# ══════════════════════════════════════════════════════════════
#  RETRY_PENDING 재시작 처분 (§11)
# ══════════════════════════════════════════════════════════════

def retry_pending_breadcrumb(session, prep) -> dict:
    """RETRY_PENDING 진입 시 영속할 정확한 복구 입력(최종 Settlement 아님).

    join 이후라 claim된 오퍼레이션의 관측은 종결 상태다. 멤버십 자체는 OPEN 유지.
    """
    ids = []
    for op in prep.cost_operations:
        for eid in getattr(op, "event_ids", ()) or ():
            if eid not in ids:
                ids.append(eid)
    return {
        "text": prep.extraction_text or "",
        "transaction_id": prep.transaction_id,
        "mode": RETRY_MODE_PREPARATION,
        "logical_turn": int(prep.logical_turn),
        "attempt": int(prep.attempt),
        "claimed_cost_event_ids": ids,
        "billing_user_ids": list(_st.normalize_billing_user_ids(
            list((getattr(session, "players", {}) or {}).keys()))),
    }


async def persist_retry_breadcrumb(bot, session, prep) -> None:
    """RETRY_PENDING 진입 — breadcrumb을 extraction_retry_ctx에 담아 strict 영속한다."""
    session.extraction_pending = True
    session.extraction_retry_ctx = retry_pending_breadcrumb(session, prep)
    try:
        await _io.save_session_data_strict(bot, session)
    except Exception as e:  # noqa: BLE001
        print(f"[WP-D] RETRY_PENDING breadcrumb strict 저장 실패(tolerant 재시도): {e}")
        await _io.save_session_data(bot, session)


async def abandon_retry_pending(bot, session) -> str:
    """런타임 준비가 소실된 RETRY_PENDING 시도를 종결한다(재시작 규정).

    exact 클레임 ID로 FAILED_SYSTEM Settlement(청구 0)를 영속한 뒤 차단을 해제한다.
    Returns: "NONE" | "ABANDONED" | "PENDING"(정산 기록 실패 — 차단 유지)
    """
    ctx = getattr(session, "extraction_retry_ctx", {}) or {}
    if not getattr(session, "extraction_pending", False) or ctx.get("mode") != RETRY_MODE_PREPARATION:
        return "NONE"
    ids = ctx.get("claimed_cost_event_ids")
    if ids is not None and ctx.get("transaction_id"):
        try:
            ident = _st.TransactionIdentity(session_id=session.session_id,
                                            transaction_id=ctx["transaction_id"],
                                            logical_turn=int(ctx["logical_turn"]),
                                            attempt=int(ctx["attempt"]))
            persist_failed_settlement(bot, session.session_id, ident, ids,
                                      ctx.get("billing_user_ids") or [])
        except Exception as e:  # noqa: BLE001
            _io.write_log(session.session_id, "error",
                          f"[WP-D] RETRY_PENDING 폐기 정산 실패 tx={ctx.get('transaction_id')} — "
                          f"{type(e).__name__}: {e}")
            return "PENDING"
    else:
        print(f"[WP-D/{session.session_id}] RETRY_PENDING breadcrumb 없음(WP-D 이전 기록) — 차단만 해제")
    session.extraction_pending = False
    session.extraction_retry_ctx = {}
    try:
        await _io.save_session_data_strict(bot, session)
    except Exception:
        await _io.save_session_data(bot, session)
    return "ABANDONED"
