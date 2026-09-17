# core.commit_journal — WP-JOURNAL-01 커밋 저널 파운데이션
#
# 목적(핸드오프 §4): 후속 authoritative commit 코드가 안전하게 소비할 수 있는
# '내구성 있는 커밋 저널 프리미티브 + 복구 분류 스캐폴드'만 제공한다.
# 이 패키지는 저널을 라이브 턴 파이프라인에 배선하지 않는다(§11/§12/§14).
#
# 설계 근거:
#   - 동시성/idempotency 구조는 이미 검증된 CostLedger(core/cost_ledger.py)의
#     '경로 단위 공유 {lock, keys} 레지스트리 + check→append→dedup 단일 임계구역'
#     패턴을 계승한다(핸드오프 §7.2).
#   - 그러나 저널은 크래시 복구의 근거가 되므로 CostLedger보다 엄격하다:
#       · append 는 write→flush→fsync 내구성 경계를 지나며(§7.3),
#         실패를 삼키지 않고 타입드 예외로 명확히 알린다.
#       · reader 는 malformed 라인을 조용히 skip 하지 않고 손상을 명시적으로
#         구분한다(§8.1). 손상된 저널 위에서 복구를 추론하지 않기 위함이다.
#   - 저장 대상은 되돌릴 수 없는 운영 내구성 이력이다. rewind/rerender/세션 롤백/
#     세션 저장은 이 파일을 재작성하지 못한다(§7). 세션 JSON(data.json) 안에 저장하지
#     않고 별도 append-only JSONL(turn_commit_journal.jsonl)로 둔다.

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

# 명시적 스키마 버전 — 미래 마이그레이션의 모호함을 줄인다(§6.2 권장).
JOURNAL_SCHEMA_VERSION = 1


# ══════════════════════════════════════════════════════════════
#  커밋 페이즈 (§6.1)
# ══════════════════════════════════════════════════════════════
#  안정적인 기계값을 갖는다. 재시작 후에도 의미가 보존되어야 한다.
#  주의(§6.1/§12/§19): 저널 COMMITTED 는 WP-01 TurnStatus.COMMITTED 와 다르다.
#  저널 COMMITTED 는 내구성 커밋 워크플로가 종단 커밋 페이즈에 도달했음을 뜻하며,
#  그 워크플로는 이 패키지에서 아직 배선되지 않는다.

class CommitPhase(str, Enum):
    PREPARED = "PREPARED"
    GAME_STATE_APPLIED_IN_MEMORY = "GAME_STATE_APPLIED_IN_MEMORY"
    SESSION_PERSISTED = "SESSION_PERSISTED"
    REWIND_RECORDED = "REWIND_RECORDED"
    BILLING_APPLIED = "BILLING_APPLIED"
    COMMITTED = "COMMITTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


_VALID_PHASE_VALUES = frozenset(p.value for p in CommitPhase)


# ══════════════════════════════════════════════════════════════
#  복구 처분 (§9) — 분류 결과 어휘
# ══════════════════════════════════════════════════════════════
#  분류기는 복구를 '실행'하지 않는다. 이후 복구 오케스트레이션이 무엇을 해야
#  하는지를 부수효과 없이 '반환'만 한다.

class RecoveryDisposition(str, Enum):
    # 저널 없음 / 해당 시도의 이력 없음 (§9.1)
    NO_JOURNAL = "NO_JOURNAL"
    # PREPARED/GAME_STATE_APPLIED_IN_MEMORY 뿐, SESSION_PERSISTED 미도달 (§9.2)
    # 캐노니컬 data.json 이 이긴다. 여기서 청구/세션변이 금지.
    DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT = "DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT"
    # SESSION_PERSISTED 도달, 그러나 청구 미적용 (§9.3) — 분류만.
    RESUME_BILLING = "RESUME_BILLING"
    # BILLING_APPLIED, 그러나 COMMITTED 마커 없음 (§9.4) — 검증·확정 필요.
    VERIFY_AND_FINALIZE = "VERIFY_AND_FINALIZE"
    # COMMITTED (§9.5)
    COMPLETE = "COMPLETE"
    # 명시적 비성공 처분 (§9.6). 다른 페이즈로 조용히 재해석 금지.
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


# ══════════════════════════════════════════════════════════════
#  예외 (§7.3 / §8.1)
# ══════════════════════════════════════════════════════════════

class CommitJournalError(RuntimeError):
    """저널 계열 예외의 베이스."""


class CommitJournalPersistenceError(CommitJournalError):
    """append 의 내구성 경계(write→flush→fsync) 실패를 호출자에게 명확히 알린다.

    원인 예외는 ``raise ... from`` 으로 ``__cause__`` 에 보존된다. 이 예외가
    발생하면 해당 저널 사실은 '내구적으로 기록되지 않았다'. dedup 키 집합에도
    등재되지 않으며, 부분 기록은 롤백된다(§7.3 / §13.8~10).
    """


class CommitJournalCorruptionError(CommitJournalError):
    """저널 파일이 malformed/구조적으로 무효임을 명시적으로 알린다(§8.1).

    reader 가 손상을 조용히 건너뛰고 완전한 이력인 척하지 않도록, 손상된
    라인을 만나면 '빈 이력'과 구분하여 이 예외를 던진다. 손상된 저널 위에서
    복구를 추론하는 것은 안전하지 않기 때문이다.
    """


# ══════════════════════════════════════════════════════════════
#  JournalEntry — 불변 이벤트 레코드 (§6.2)
# ══════════════════════════════════════════════════════════════
#  요구: 불변/frozen, JSON 직렬화 가능, mutable Session/Discord/task 참조 금지.
#  식별 필드만으로 재시작 후 '하나의 논리 턴 시도'를 추론할 수 있어야 한다.

@dataclass(frozen=True)
class JournalEntry:
    # 정체성 (재시작 후 시도 추론에 충분한 최소 식별자)
    transaction_id: str
    session_id: str
    logical_turn: int
    attempt: int
    phase: CommitPhase

    # 결정적 중복 억제 정체성(§7.1). 불변 커밋 정체성에서만 파생된다.
    idempotency_key: str

    # 진행 상태/진단
    completed_steps: list = field(default_factory=list)
    settlement_id: str | None = None   # Settlement 미구현 → None 허용(§6.2)
    error_code: str | None = None

    # 이벤트 메타
    entry_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = JOURNAL_SCHEMA_VERSION
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON 직렬화 가능한 dict. phase 는 안정 문자열값으로 기록한다."""
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "idempotency_key": self.idempotency_key,
            "transaction_id": self.transaction_id,
            "session_id": self.session_id,
            "logical_turn": self.logical_turn,
            "attempt": self.attempt,
            "phase": self.phase.value,
            "completed_steps": list(self.completed_steps),
            "settlement_id": self.settlement_id,
            "error_code": self.error_code,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, obj: object) -> "JournalEntry":
        """직렬화 dict → JournalEntry. 구조적으로 무효면 손상으로 간주해 raise.

        조용히 기본값으로 때우지 않는다(§8.1 / §13.12). 필수 정체성 필드 누락,
        미지의 phase 값, int 로 해석 불가한 turn/attempt 는 모두 손상이다.
        """
        if not isinstance(obj, dict):
            raise CommitJournalCorruptionError(
                f"저널 레코드가 객체가 아닙니다: {type(obj).__name__}")

        required = ("transaction_id", "session_id", "logical_turn",
                    "attempt", "phase")
        missing = [k for k in required if k not in obj]
        if missing:
            raise CommitJournalCorruptionError(
                f"저널 레코드 필수 필드 누락: {missing}")

        phase_val = obj.get("phase")
        if phase_val not in _VALID_PHASE_VALUES:
            raise CommitJournalCorruptionError(
                f"저널 레코드 phase 값이 무효합니다: {phase_val!r}")

        try:
            logical_turn = int(obj["logical_turn"])
            attempt = int(obj["attempt"])
        except (TypeError, ValueError) as e:
            raise CommitJournalCorruptionError(
                "저널 레코드 logical_turn/attempt 가 정수가 아닙니다") from e

        tx = obj["transaction_id"]
        sid = obj["session_id"]
        if not isinstance(tx, str) or not isinstance(sid, str) or not tx:
            raise CommitJournalCorruptionError(
                "저널 레코드 transaction_id/session_id 가 무효합니다")

        phase = CommitPhase(phase_val)
        key = obj.get("idempotency_key") or make_idempotency_key(tx, attempt, phase)
        completed = obj.get("completed_steps")
        if completed is None:
            completed = []
        elif not isinstance(completed, list):
            raise CommitJournalCorruptionError(
                "저널 레코드 completed_steps 가 리스트가 아닙니다")
        meta = obj.get("metadata") or {}
        if not isinstance(meta, dict):
            raise CommitJournalCorruptionError(
                "저널 레코드 metadata 가 객체가 아닙니다")

        return cls(
            transaction_id=tx,
            session_id=sid,
            logical_turn=logical_turn,
            attempt=attempt,
            phase=phase,
            idempotency_key=key,
            completed_steps=list(completed),
            settlement_id=obj.get("settlement_id"),
            error_code=obj.get("error_code"),
            entry_id=obj.get("entry_id") or uuid.uuid4().hex,
            schema_version=int(obj.get("schema_version", JOURNAL_SCHEMA_VERSION)),
            timestamp=float(obj.get("timestamp", 0.0)),
            metadata=dict(meta),
        )


# ══════════════════════════════════════════════════════════════
#  정체성/경로 헬퍼
# ══════════════════════════════════════════════════════════════

def make_idempotency_key(transaction_id: str, attempt: int,
                         phase: "CommitPhase | str") -> str:
    """<transaction_id>:attempt:<attempt>:phase:<phase> (§7.1).

    벽시계/응답해시/무작위 UUID/가변 카운터가 아닌, 불변 커밋 정체성에서만
    결정적으로 파생한다. 같은 (tx, attempt, phase) 는 '같은 논리 저널 사실'이며
    한 줄만 남아야 한다.
    """
    pv = phase.value if isinstance(phase, CommitPhase) else str(phase)
    return f"{transaction_id}:attempt:{attempt}:phase:{pv}"


def default_journal_path(session_id: str) -> str:
    """저장소의 세션 경로 규칙(core/io.py: sessions/{id}/data.json)과 정합.

    병렬 루트 레이아웃을 새로 만들지 않는다(§5).
    """
    return f"sessions/{session_id}/turn_commit_journal.jsonl"


def new_entry(*, transaction_id: str, session_id: str, logical_turn: int,
              attempt: int, phase: "CommitPhase | str",
              completed_steps: list | None = None,
              settlement_id: str | None = None,
              error_code: str | None = None,
              metadata: dict | None = None,
              timestamp: float | None = None) -> JournalEntry:
    """JournalEntry 생성 팩토리. idempotency_key 를 정체성에서 파생한다."""
    ph = phase if isinstance(phase, CommitPhase) else CommitPhase(phase)
    key = make_idempotency_key(transaction_id, int(attempt), ph)
    return JournalEntry(
        transaction_id=transaction_id,
        session_id=session_id,
        logical_turn=int(logical_turn),
        attempt=int(attempt),
        phase=ph,
        idempotency_key=key,
        completed_steps=list(completed_steps or []),
        settlement_id=settlement_id,
        error_code=error_code,
        metadata=dict(metadata or {}),
        timestamp=timestamp if timestamp is not None else time.time(),
    )


def entry_from_transaction(tx, phase: "CommitPhase | str", **kwargs) -> JournalEntry:
    """TurnTransaction(또는 동형 객체)의 불변 식별자로 엔트리를 만든다.

    tx 에 대한 참조를 저장하지 않고 불변 값만 읽는다(중립 생성 유틸 — §14 허용).
    이것은 라이브 파이프라인 배선이 아니다.
    """
    return new_entry(
        transaction_id=tx.transaction_id,
        session_id=tx.session_id,
        logical_turn=tx.logical_turn,
        attempt=tx.attempt,
        phase=phase,
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════
#  경로 단위 공유 레지스트리 (CostLedger 패턴 계승 — §7.2)
# ══════════════════════════════════════════════════════════════
#  같은 저널 파일 경로를 가리키는 서로 다른 CommitJournal 인스턴스가 단일
#  프로세스 안에서 동시에 기록해도, check→append→dedup 갱신 전체가 하나의
#  임계구역이 되도록 lock 과 dedup 키 집합을 경로 단위로 공유한다.

_JOURNAL_REGISTRY: dict = {}
_REGISTRY_GUARD = threading.Lock()


def _registry_for(path: str) -> dict:
    """정규화된 절대경로 기준 공유 {lock, keys, loaded} 엔트리를 반환."""
    key = os.path.abspath(path)
    with _REGISTRY_GUARD:
        entry = _JOURNAL_REGISTRY.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "keys": set(), "loaded": False}
            _JOURNAL_REGISTRY[key] = entry
        return entry


# ══════════════════════════════════════════════════════════════
#  순수 복구 분류기 (§9) — 부수효과 없음
# ══════════════════════════════════════════════════════════════

def classify_disposition(entries: "list[JournalEntry]") -> RecoveryDisposition:
    """저널 이력 → 복구 처분. 순수 함수(I/O·변이·청구 없음).

    도달한 '내구성 마일스톤'의 집합으로 판정한다. 승인되지 않은 순서를 강제하는
    경직된 상태기계를 만들지 않는다(§10). REWIND_RECORDED 등 비결정 마커는
    처분 경계가 아니므로 판정에 사용하지 않는다.
    """
    if not entries:
        return RecoveryDisposition.NO_JOURNAL

    phases = {e.phase for e in entries}

    # 명시적 비성공은 다른 페이즈로 재해석하지 않는다(§9.6).
    if CommitPhase.RECOVERY_REQUIRED in phases:
        return RecoveryDisposition.RECOVERY_REQUIRED
    if CommitPhase.COMMITTED in phases:            # §9.5
        return RecoveryDisposition.COMPLETE
    if CommitPhase.BILLING_APPLIED in phases:      # §9.4
        return RecoveryDisposition.VERIFY_AND_FINALIZE
    if CommitPhase.SESSION_PERSISTED in phases:    # §9.3
        return RecoveryDisposition.RESUME_BILLING
    if phases & {CommitPhase.PREPARED,
                 CommitPhase.GAME_STATE_APPLIED_IN_MEMORY}:   # §9.2
        return RecoveryDisposition.DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT
    # 내구성 마일스톤 없이 비결정 마커만 있는 비정상/불완전 이력:
    # 조용히 성공 처리하지 않고 명시적 비성공으로 표면화한다(§10).
    return RecoveryDisposition.RECOVERY_REQUIRED


# ══════════════════════════════════════════════════════════════
#  CommitJournal — append-only JSONL 내구성 저널
# ══════════════════════════════════════════════════════════════

class CommitJournal:
    """세션 단위 append-only 커밋 저널.

    - path 를 주입 가능하게 두어(seam) 테스트는 tmp 경로로 실세션을 만지지 않는다.
    - 운영 기본 경로는 default_journal_path(session_id) 로 얻는다(§5).
    - 동일 idempotency_key 는 두 번 append 되지 않으며(§7.1),
      append 는 write→flush→fsync 내구성 경계를 지난다(§7.3).
    - reader 는 손상을 조용히 넘기지 않고 CommitJournalCorruptionError 로 알린다(§8.1).
    """

    def __init__(self, path: str):
        self.path = path
        # 같은 경로를 가리키는 모든 인스턴스가 lock/dedup 상태를 공유한다.
        self._reg = _registry_for(path)

    # ── 내부: 엄격한 JSONL 파서(손상 구분) ──────────────────────

    def _read_all_locked(self) -> "list[JournalEntry]":
        """파일 전체를 엄격 파싱해 JournalEntry 리스트로 반환.

        빈 파일/부재 → []. 공백 라인(말미 개행 등)은 관용하되, 비공백 malformed
        라인은 CommitJournalCorruptionError 로 올린다. 반드시 lock 하에 호출한다.
        """
        if not os.path.exists(self.path):
            return []
        out: list[JournalEntry] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    raise CommitJournalCorruptionError(
                        f"{self.path}:{lineno} JSON 파싱 실패") from e
                out.append(JournalEntry.from_dict(obj))   # 구조 무효 시 raise
        return out

    def _load_keys_locked(self) -> None:
        """dedup 키 집합을 파일에서 1회 로드. 손상 시 raise(안전 우선).

        손상된 저널 위에서는 append 도 안전하지 않으므로 조용히 로드를 건너뛰지
        않는다. 성공적으로 로드해야 loaded 플래그를 세운다.
        """
        if self._reg["loaded"]:
            return
        for e in self._read_all_locked():
            self._reg["keys"].add(e.idempotency_key)
        self._reg["loaded"] = True

    def _truncate_to(self, size: int) -> None:
        """부분 기록 롤백 — 실패 지점 이전 크기로 파일을 되돌린다(best-effort).

        내구성 경계 실패 후 malformed '성공한 척' 라인을 남기지 않기 위함(§13.10).
        """
        try:
            with open(self.path, "r+b") as f:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    # ── 공개 API ────────────────────────────────────────────────

    def has_idempotency_key(self, key: str) -> bool:
        with self._reg["lock"]:
            self._load_keys_locked()
            return key in self._reg["keys"]

    def append_entry(self, entry: JournalEntry) -> bool:
        """엔트리를 내구적으로 append 한다. 중복 키면 False, 신규 성공이면 True.

        check→append→flush→fsync→dedup갱신 전체가 경로 단위 공유 lock 하의 단일
        임계구역이다(§7.2). 내구성 경계(write→flush→fsync) 실패는 삼키지 않고
        부분 기록을 롤백한 뒤 CommitJournalPersistenceError 로 올린다(§7.3).
        성공은 예외 부재 + True 로만 신호한다(로그-후-성공 금지).
        """
        key = entry.idempotency_key
        with self._reg["lock"]:
            self._load_keys_locked()                 # 손상 시 여기서 raise
            if key in self._reg["keys"]:
                return False                         # 안정적 중복 억제(§7.1)

            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)

            line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
            pre_size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())             # 내구성 경계(§7.3)
            except Exception as e:
                # 부분 기록 롤백 후 명확히 실패 신호. dedup 키는 등재하지 않는다.
                self._truncate_to(pre_size)
                raise CommitJournalPersistenceError(
                    f"저널 append 실패: {self.path} key={key}") from e

            self._reg["keys"].add(key)               # 내구 성공 후에만 등재
            return True

    def list_entries(self, *, transaction_id: str | None = None,
                     attempt: int | None = None) -> "list[JournalEntry]":
        """append 순서를 보존한 이력. 정확한 transaction/attempt 정체성으로 필터.

        손상 라인이 있으면 CommitJournalCorruptionError 를 올린다(§8.1).
        """
        with self._reg["lock"]:
            entries = self._read_all_locked()
        out = []
        for e in entries:
            if transaction_id is not None and e.transaction_id != transaction_id:
                continue
            if attempt is not None and e.attempt != attempt:
                continue
            out.append(e)
        return out

    def latest_entry(self, *, transaction_id: str | None = None,
                     attempt: int | None = None) -> "JournalEntry | None":
        """필터에 맞는 마지막(append 순) 엔트리. 없으면 None.

        정확한 transaction+attempt 로 필터하므로 다른 시도의 'latest' 를 잘못
        반환하지 않는다(§19).
        """
        entries = self.list_entries(transaction_id=transaction_id, attempt=attempt)
        return entries[-1] if entries else None

    def history_for_transaction(self, transaction_id: str,
                                attempt: int | None = None) -> "list[JournalEntry]":
        """하나의 트랜잭션(시도) 이력. attempt 를 주면 정확히 그 시도만."""
        return self.list_entries(transaction_id=transaction_id, attempt=attempt)

    def classify_recovery(self, transaction_id: str,
                          attempt: int) -> RecoveryDisposition:
        """정확한 transaction+attempt 이력에 대한 복구 처분(부수효과 없음).

        이력을 읽어 순수 분류기 classify_disposition 에 위임한다. 복구를 실행하지
        않으며 청구/세션변이/Settlement 접근을 하지 않는다(§9). 손상 시
        CommitJournalCorruptionError 가 전파된다.
        """
        history = self.history_for_transaction(transaction_id, attempt=attempt)
        return classify_disposition(history)
