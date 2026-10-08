# 공통 정본 변이 coordinator — INITIALIZATION · ADMIN_EDIT 용 저수준 primitive (H02-B)
#
# [위치 — 통합 설계 §46 · DESIGN 05 · DESIGN 07 · DESIGN 11 §6~11]
#   PLAYER_TURN의 정본 owner는 계속 CommitCoordinator다. 이 모듈은 그것을 대체하지 않으며
#   PLAYER_TURN을 거부한다. 향후 INITIALIZATION(세션 초기 세계 확정, 통합 로드맵 H06)과
#   ADMIN_EDIT(typed 관리자 편집, H06)이 같은 WorldMutationBatch·투영·world_version 규칙을
#   재사용하도록 원자 경계와 작은 내구 저널만 제공한다.
#
# [H02 범위 — production 호출자 0]
#   Discord·관리자 명령·세션 생성·캐시·정의 스냅샷 어디서도 부르지 않는다(테스트로 고정).
#   재무(CostEvent·Settlement·Ink)를 만들지 않는다. provider를 부르지 않는다.
#
# [원자 경계]
#   [커밋 직렬화 락 — CommitCoordinator와 같은 락·같은 순서: 직렬화 락 → io 락]
#   검증(활성 턴·복구 대기 없음, 기준 world_version·definition_revision 일치)
#   → 묶음 동결·투영(충돌 = WORLD_PROJECTION_CONFLICT, 정본 불변)
#   → 대상 version 결정 → 저널 PREPARED(복구에서 재생성하지 않는다)
#   → io 락: 롤백 스냅샷 → 투영 반영 + world_version 전진 → strict 저장
#   검증부터 저장까지 한 직렬화 구간이므로, 검증 뒤 다른 변이(PLAYER_TURN 커밋·다른
#   INIT/ADMIN)가 끼어들어 오래된 투영이 더 최신 정본을 덮어쓸 수 없다.
#       · 영속 전 실패: 스냅샷 복원 + ABORTED
#   → PERSISTED → COMMITTED (영속 후 실패는 RECOVERY_PENDING — 이야기는 정본)
#
#   ADMIN_EDIT의 완전 no-op(값 변화 없음)은 mutation도 새 version도 만들지 않는다(DESIGN 11 §8).
#   INITIALIZATION은 항상 genesis version을 만든다(§9). 활성 턴 중 ADMIN_EDIT 금지(§53.4).
from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field

from . import io as _io
from . import turn_transaction as _tx
from . import world_mutation as _wm
from . import world_state as _ws
from .world_ids import stable_digest

JOURNAL_FILE = "canonical_mutation_journal.jsonl"
JOURNAL_SCHEMA_VERSION = 1

PHASE_PREPARED = "PREPARED"
PHASE_PERSISTED = "PERSISTED"
PHASE_COMMITTED = "COMMITTED"
PHASE_ABORTED = "ABORTED"
_TERMINAL = (PHASE_COMMITTED, PHASE_ABORTED)

ALLOWED_CAUSES = (_ws.CAUSE_INITIALIZATION, _ws.CAUSE_ADMIN_EDIT)

OUTCOME_COMMITTED = "COMMITTED"
OUTCOME_NOOP = "NOOP"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_CONFLICT = _wm.WORLD_PROJECTION_CONFLICT
OUTCOME_FAILED_PRE_PERSIST = "FAILED_PRE_PERSIST"
OUTCOME_RECOVERY_PENDING = "RECOVERY_PENDING"


class CanonicalMutationJournalError(RuntimeError):
    """저널 영속 실패 또는 손상."""


@dataclass
class CanonicalMutationResult:
    outcome: str
    mutation_id: str | None = None
    reason: str = ""
    source_world_version: object = None
    target_world_version: object = None
    changed_domains: tuple = ()
    conflicts: tuple = ()
    batch_digest: str | None = None
    diagnostics: list = field(default_factory=list)


# ══════════════════════════════════════════════════════════════
#  작은 내구 저널 (append-only, fsync)
# ══════════════════════════════════════════════════════════════

def journal_path(session_id) -> str:
    return os.path.join("sessions", str(session_id), JOURNAL_FILE)


def _append(session_id, entry: dict) -> None:
    path = journal_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(dict(entry, schema_version=JOURNAL_SCHEMA_VERSION, ts=time.time()),
                      ensure_ascii=False, sort_keys=True) + "\n"
    pre = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            with open(path, "r+b") as f:
                f.truncate(pre)
        except Exception:
            pass
        raise CanonicalMutationJournalError(f"정본 변이 저널 쓰기 실패: {path}") from e


def read_journal(session_id) -> list:
    path = journal_path(session_id)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise CanonicalMutationJournalError(f"{path}:{n} 손상") from e
    return out


def classify_recovery(session) -> list:
    """미종결 변이의 처분을 분류한다(순수). 반환: [(mutation_id, action, prepared_entry)].

    action: COMPLETE(영속됨 — world_version이 대상) · ABORT(미영속 — 기준 그대로) · CONFLICT
    """
    by_id, order = {}, []
    for e in read_journal(session.session_id):
        mid = e.get("mutation_id")
        if mid not in by_id:
            by_id[mid] = []
            order.append(mid)
        by_id[mid].append(e)
    out = []
    cur = getattr(session, "world_version", None)
    for mid in order:
        es = by_id[mid]
        phases = [e["phase"] for e in es]
        if any(p in _TERMINAL for p in phases):
            continue
        prepared = next((e for e in es if e["phase"] == PHASE_PREPARED), None)
        if prepared is None:
            out.append((mid, "CONFLICT", None))
        elif cur == prepared["target_world_version"]:
            out.append((mid, "COMPLETE", prepared))
        elif cur == prepared["source_world_version"]:
            out.append((mid, "ABORT", prepared))
        else:
            out.append((mid, "CONFLICT", prepared))
    return out


def has_unfinished(session) -> bool:
    """미종결(PREPARED만 있고 종결 안 된) INITIALIZATION·ADMIN_EDIT가 있는가 — 생애주기 안전 지점용(H06)."""
    return bool(classify_recovery(session))


def recover(session) -> list:
    """미종결 변이를 저널에 종결한다. 정본 상태는 건드리지 않는다(이미 영속된 쪽이 정본)."""
    done = []
    for mid, action, prepared in classify_recovery(session):
        if action == "COMPLETE":
            if PHASE_PERSISTED not in {e["phase"] for e in read_journal(session.session_id)
                                       if e.get("mutation_id") == mid}:
                _append(session.session_id, {"mutation_id": mid, "phase": PHASE_PERSISTED,
                                             "recovered": True})
            _append(session.session_id, {"mutation_id": mid, "phase": PHASE_COMMITTED,
                                         "recovered": True})
        elif action == "ABORT":
            _append(session.session_id, {"mutation_id": mid, "phase": PHASE_ABORTED,
                                         "recovered": True, "reason": "NOT_PERSISTED"})
        done.append((mid, action))
    return done


# ══════════════════════════════════════════════════════════════
#  coordinator
# ══════════════════════════════════════════════════════════════

def mutation_id_for(session_id, cause_kind: str, cause_id: str) -> str:
    return "cmu_" + stable_digest("canonical_mutation.id.v1", str(session_id), cause_kind, cause_id)


def new_batch(session, *, cause_kind: str, cause_id: str) -> _wm.WorldMutationBatch:
    """현재 world_version·definition_revision을 기준으로 하는 빈 묶음."""
    if cause_kind not in ALLOWED_CAUSES:
        raise ValueError(f"공통 coordinator는 {ALLOWED_CAUSES}만 다룹니다(PLAYER_TURN은 CommitCoordinator)")
    return _wm.WorldMutationBatch(
        batch_id="wmb_" + stable_digest("world_mutation.batch.canonical.v1",
                                        str(session.session_id), cause_kind, cause_id),
        outer_origin=cause_kind,
        source_world_version_id=_ws.version_id_of(getattr(session, "world_version", None)),
        definition_revision=getattr(session, "definition_revision", None))


class CanonicalMutationCoordinator:
    """INITIALIZATION·ADMIN_EDIT 공통 원자 적용. PLAYER_TURN은 거부한다."""

    def __init__(self, bot, *, registry: _wm.OperationRegistry | None = None):
        self.bot = bot
        self.registry = registry or _wm.PRODUCTION_REGISTRY

    def _precheck(self, session, batch, cause_kind) -> str | None:
        if cause_kind not in ALLOWED_CAUSES:
            return f"cause_kind {cause_kind!r}은 이 coordinator 대상이 아닙니다"
        if batch.outer_origin != cause_kind:
            return "묶음 outer_origin과 cause_kind가 다릅니다"
        tx = _tx.get_active_transaction(session)
        if tx is not None and not _tx.is_terminal(tx.status):
            return "진행 중인 플레이어 턴이 있습니다"
        if getattr(session, "commit_recovery", None):
            return "커밋 복구 대기 중입니다"
        if classify_recovery(session):
            return "미종결 정본 변이가 있습니다(복구 먼저)"
        if batch.source_world_version_id != _ws.version_id_of(getattr(session, "world_version", None)):
            return "묶음 기준 world_version이 현재와 다릅니다(stale)"
        if batch.definition_revision != getattr(session, "definition_revision", None):
            return "묶음 기준 definition_revision이 현재와 다릅니다(stale)"
        return None

    async def apply(self, session, batch: _wm.WorldMutationBatch, *, cause_kind: str,
                    cause_id: str) -> CanonicalMutationResult:
        from .commit_coordinator import commit_serialization_lock
        async with commit_serialization_lock(session.session_id):
            return await self._apply_serialized(session, batch, cause_kind=cause_kind,
                                                cause_id=cause_id)

    async def _apply_serialized(self, session, batch, *, cause_kind, cause_id):
        """커밋 직렬화 락 보유 상태 전용. 검증~저장이 한 구간."""
        reason = self._precheck(session, batch, cause_kind)
        if reason:
            return CanonicalMutationResult(OUTCOME_REJECTED, reason=reason)
        digest = batch.freeze()
        source_world = _wm.ProjectedWorld.capture(session)
        result = _wm.project(source_world, batch, self.registry)
        if not result.ok:
            return CanonicalMutationResult(OUTCOME_CONFLICT, reason=_wm.WORLD_PROJECTION_CONFLICT,
                                           conflicts=result.conflicts, batch_digest=digest)
        # H03: 안정 세계 불변식 — INIT/ADMIN 결과도 base quiescent여야 한다.
        from . import world_schedule as _sched
        q = _sched.base_quiescence_of(result.world)
        if not q.quiescent:
            return CanonicalMutationResult(OUTCOME_CONFLICT, reason="NOT_QUIESCENT: " + "; ".join(q.reasons),
                                           batch_digest=digest)
        changed = result.changed_domains(source_world)
        source_wv = copy.deepcopy(getattr(session, "world_version", None))
        if cause_kind == _ws.CAUSE_ADMIN_EDIT and not changed:
            return CanonicalMutationResult(OUTCOME_NOOP, reason="변화 없음 — 새 version 없음",
                                           source_world_version=source_wv, batch_digest=digest)
        mid = mutation_id_for(session.session_id, cause_kind, cause_id)
        target_wv = _ws.next_world_version(session.session_id, source_wv,
                                           cause_kind=cause_kind, cause_id=cause_id)
        sid = session.session_id
        try:
            _append(sid, {"mutation_id": mid, "phase": PHASE_PREPARED, "cause_kind": cause_kind,
                          "cause_id": cause_id, "batch_digest": digest,
                          "definition_revision": batch.definition_revision,
                          "source_world_version": source_wv, "target_world_version": target_wv,
                          "changed_domains": list(changed)})
        except CanonicalMutationJournalError as e:
            return CanonicalMutationResult(OUTCOME_FAILED_PRE_PERSIST, mid, reason=str(e),
                                           batch_digest=digest)
        persisted, err = False, None
        async with _io.session_io_lock(self.bot, session):
            snap = {k: copy.deepcopy(getattr(session, k)) for k in _ws.GAMEPLAY_WORLD_FIELDS}
            try:
                _wm.apply_projection(session, result, source_world)
                session.world_version = copy.deepcopy(target_wv)
                await _io.write_session_strict_locked(session)
                persisted = True
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"
                for k, v in snap.items():
                    setattr(session, k, v)
        if not persisted:
            try:
                _append(sid, {"mutation_id": mid, "phase": PHASE_ABORTED, "reason": err})
            except CanonicalMutationJournalError:
                pass        # PREPARED만 남아도 복구 분류가 기준 version으로 ABORT 판정
            return CanonicalMutationResult(OUTCOME_FAILED_PRE_PERSIST, mid, reason=err,
                                           source_world_version=source_wv, batch_digest=digest)
        try:
            _append(sid, {"mutation_id": mid, "phase": PHASE_PERSISTED})
            _append(sid, {"mutation_id": mid, "phase": PHASE_COMMITTED})
        except CanonicalMutationJournalError as e:
            return CanonicalMutationResult(OUTCOME_RECOVERY_PENDING, mid, reason=str(e),
                                           source_world_version=source_wv,
                                           target_world_version=target_wv,
                                           changed_domains=changed, batch_digest=digest)
        return CanonicalMutationResult(OUTCOME_COMMITTED, mid, source_world_version=source_wv,
                                       target_world_version=target_wv, changed_domains=changed,
                                       batch_digest=digest)
