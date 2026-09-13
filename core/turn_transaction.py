# core.turn_transaction — WP-01 TurnTransaction 식별자 셸
#
# 하나의 '정규 논리 턴 시도(canonical logical turn attempt)'의 불변 식별자와
# 생명주기 상태만 소유한다. 커밋/청구/변이 로직은 없다(후속 WP 소관).
#
# 활성 트랜잭션은 런타임 전용이다: TRPGSession.active_turn_transaction /
# turn_attempt_counters 는 SESSION_FIELDS에 등록되지 않으며 세션 JSON에 저장되지
# 않는다. 재시작 시 진행 중 트랜잭션은 존재하지 않는다.
#
# 생명주기 경계(설계 근거 — WP01_SIGNATURE_CHANGE_MAP.md §1):
#   하나의 트랜잭션은 _process_actions() 1회 호출이 아니라, ASK->재입력,
#   NARRATE->재입력, ROLL->View 재개를 거쳐 PROCEED(정규 완료)에 이르는
#   '대기 중인 논리 턴 시도' 전체에 대응한다. 따라서 자동 턴 진입점은
#   get_or_begin()으로 활성 비종료 트랜잭션을 재사용한다.

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TurnStatus(str, Enum):
    CREATED = "CREATED"
    JUDGING = "JUDGING"
    INSTRUCTING = "INSTRUCTING"
    WAITING_FOR_PLAYER = "WAITING_FOR_PLAYER"
    WAITING_FOR_ROLL = "WAITING_FOR_ROLL"
    NARRATING = "NARRATING"
    STREAMING_EXTRACTING = "STREAMING_EXTRACTING"
    READY_TO_COMMIT = "READY_TO_COMMIT"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED_SYSTEM = "FAILED_SYSTEM"
    ABORTED = "ABORTED"
    SUPERSEDED = "SUPERSEDED"


# 종료 상태 — 이 상태의 트랜잭션만 활성 포인터를 내려놓을 수 있다.
_TERMINAL = frozenset({
    TurnStatus.COMMITTED,
    TurnStatus.FAILED_SYSTEM,
    TurnStatus.ABORTED,
    TurnStatus.SUPERSEDED,
})


# 실패 코드 네임스페이스 — 후속 WP가 확장한다. 사람이 읽는 디스코드 문구가
# 실패 처리의 기계 식별자가 되지 않도록 안정 코드를 먼저 둔다.
class FailureCode(str, Enum):
    JUDGMENT_PROVIDER_FAILURE = "JUDGMENT_PROVIDER_FAILURE"
    INSTRUCTION_PROVIDER_FAILURE = "INSTRUCTION_PROVIDER_FAILURE"
    NARRATION_PROVIDER_FAILURE = "NARRATION_PROVIDER_FAILURE"
    EXTRACTION_PROVIDER_FAILURE = "EXTRACTION_PROVIDER_FAILURE"
    EXTRACTION_INVALID_RESULT = "EXTRACTION_INVALID_RESULT"
    COMMIT_VALIDATION_FAILURE = "COMMIT_VALIDATION_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    MESSAGE_DELIVERY_FAILURE = "MESSAGE_DELIVERY_FAILURE"


def is_terminal(status) -> bool:
    return status in _TERMINAL


@dataclass
class TurnTransaction:
    """하나의 논리 턴 시도의 불변 식별자 + 진단 상태.

    WP-01은 정체성(식별자)과 상태 표기만 다룬다. instruction_result 등 후속 필드는
    소유권을 조기에 고정하기 위해 미리 선언하되 이 패키지에서는 채우지 않는다.
    """

    transaction_id: str
    session_id: str
    logical_turn: int
    attempt: int
    status: TurnStatus = TurnStatus.CREATED

    player_declaration: str = ""
    judgment_result: dict | None = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # 진단 / 비동기 식별
    failure_stage: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    # 후속 WP가 채운다 — 소유권 조기 고정용(WP-01은 건드리지 않음)
    instruction_result: dict | None = None
    narration_result: object | None = None
    extraction_result: object | None = None
    pending_effects: dict = field(default_factory=dict)
    transient_message_ids: list = field(default_factory=list)
    canonical_message_ids: list = field(default_factory=list)

    # ASK/NARRATE 재입력의 진단 보존용. 최초 선언은 덮어쓰지 않는다.
    interaction_inputs: list = field(default_factory=list)

    @property
    def short_id(self) -> str:
        return self.transaction_id[:8]

    def log_prefix(self, stage: str = "") -> str:
        s = stage or self.status.value
        return (f"[TURN tx={self.short_id} logical={self.logical_turn} "
                f"attempt={self.attempt} stage={s}]")


# ── 세션 런타임 필드 접근 ────────────────────────────────────
# active_turn_transaction / turn_attempt_counters 는 TRPGSession.__init__에서
# 초기화된다. 구버전 직렬화 세션이 역직렬화되며 __init__을 거치므로 항상 존재하나,
# 방어적으로 getattr 기본값을 둔다.

def _counters(session) -> dict:
    c = getattr(session, "turn_attempt_counters", None)
    if c is None:
        c = {}
        session.turn_attempt_counters = c
    return c


def get_active(session) -> "TurnTransaction | None":
    return getattr(session, "active_turn_transaction", None)


def is_current(session, transaction_id) -> bool:
    """transaction_id가 세션의 현재 활성 트랜잭션과 일치하는가."""
    if transaction_id is None:
        return False
    active = get_active(session)
    return active is not None and active.transaction_id == transaction_id


def require_current(session, transaction_id) -> "TurnTransaction | None":
    """현재 활성과 일치하면 그 트랜잭션을, 아니면 None을 반환한다(예외 없음).

    stale 비동기 콜백이 더 새로운 트랜잭션을 조작하지 못하게 하는 가드에 쓴다.
    """
    if transaction_id is None:
        return None
    active = get_active(session)
    if active is not None and active.transaction_id == transaction_id:
        return active
    return None


def _new_id() -> str:
    return uuid.uuid4().hex


def _begin(session, *, logical_turn: int, player_declaration: str) -> TurnTransaction:
    counters = _counters(session)
    attempt = int(counters.get(logical_turn, 0)) + 1
    counters[logical_turn] = attempt
    tx = TurnTransaction(
        transaction_id=_new_id(),
        session_id=str(getattr(session, "session_id", "")),
        logical_turn=logical_turn,
        attempt=attempt,
        status=TurnStatus.CREATED,
        player_declaration=player_declaration or "",
    )
    session.active_turn_transaction = tx
    return tx


def begin(session, player_declaration: str = "") -> TurnTransaction:
    """새 논리 턴 시도를 연다. 비종료 활성 트랜잭션이 있으면 거부(감사 규율).

    보통의 자동 턴 진입은 get_or_begin()을 쓴다. begin()은 활성 트랜잭션이 없음을
    이미 아는 경로에서만 직접 호출한다.
    """
    active = get_active(session)
    if active is not None and not is_terminal(active.status):
        raise RuntimeError(
            f"비종료 활성 트랜잭션이 이미 존재합니다: {active.transaction_id}")
    logical_turn = int(getattr(session, "turn_count", 0)) + 1
    return _begin(session, logical_turn=logical_turn,
                  player_declaration=player_declaration)


def get_or_begin(session, player_declaration: str = "") -> TurnTransaction:
    """자동 턴 진입 헬퍼(_process_actions 정규 진입점).

    - 호환되는 비종료 활성 트랜잭션이 있으면 그대로 재사용(같은 ID·같은 시도).
      ASK/NARRATE 재입력, 제공자 재시도가 여기에 해당한다.
    - 없으면 논리 턴 = turn_count + 1 로 새로 시작한다.

    재사용 시 최초 player_declaration은 덮어쓰지 않는다(진단 입력만 누적).
    """
    active = get_active(session)
    if active is not None and not is_terminal(active.status):
        if player_declaration:
            active.interaction_inputs.append(player_declaration)
            active.updated_at = time.time()
        return active
    logical_turn = int(getattr(session, "turn_count", 0)) + 1
    return _begin(session, logical_turn=logical_turn,
                  player_declaration=player_declaration)


def begin_attempt(session, *, logical_turn: int,
                  player_declaration: str = "") -> TurnTransaction:
    """같은 논리 턴의 새 시도를 명시적으로 연다(플레이어 재요청/재렌더).

    기존 활성 비종료 트랜잭션은 SUPERSEDED로 종료한 뒤 attempt를 1 올린 새
    트랜잭션을 시작한다. 제공자 재시도는 이 경로가 아니다(시도를 올리지 않는다).
    """
    active = get_active(session)
    if active is not None and not is_terminal(active.status):
        active.status = TurnStatus.SUPERSEDED
        active.updated_at = time.time()
    return _begin(session, logical_turn=logical_turn,
                  player_declaration=player_declaration)


def mark_status(session, transaction_id, status, *,
                failure_stage=None, failure_code=None,
                failure_message=None) -> bool:
    """현재 활성 트랜잭션의 상태를 갱신한다.

    transaction_id가 현재 활성이 아니면 아무것도 하지 않는다(no-op). 이로써 지연된
    stale 콜백이 더 새로운 트랜잭션의 상태를 덮어쓰지 못한다. transaction_id가
    None이면(수동/인트로 경로) 조용히 False를 반환한다.
    """
    active = require_current(session, transaction_id)
    if active is None:
        return False
    active.status = status if isinstance(status, TurnStatus) else TurnStatus(status)
    if failure_stage is not None:
        active.failure_stage = failure_stage
    if failure_code is not None:
        active.failure_code = (failure_code.value
                               if isinstance(failure_code, FailureCode)
                               else failure_code)
    if failure_message is not None:
        active.failure_message = failure_message
    active.updated_at = time.time()
    return True


def mark_waiting_for_player(session, transaction_id) -> bool:
    """ASK/NARRATE 후 플레이어 입력 대기(비종료). 재입력이 같은 트랜잭션을 재사용한다."""
    return mark_status(session, transaction_id, TurnStatus.WAITING_FOR_PLAYER)


def mark_waiting_for_roll(session, transaction_id) -> bool:
    """ROLL View 전송 후 버튼/타임아웃 재개 대기(비종료)."""
    return mark_status(session, transaction_id, TurnStatus.WAITING_FOR_ROLL)


def clear(session, transaction_id) -> bool:
    """활성 포인터를 비운다. ID가 현재 활성과 일치할 때만 비운다.

    낡은 ID로는 더 새로운 활성 트랜잭션을 지울 수 없다(TID-007). transaction_id가
    None이거나 현재가 아니면 no-op으로 False를 반환한다.
    """
    if transaction_id is None:
        return False
    active = get_active(session)
    if active is not None and active.transaction_id == transaction_id:
        session.active_turn_transaction = None
        return True
    return False


def finalize(session, transaction_id, status, *,
             failure_stage=None, failure_code=None,
             failure_message=None) -> bool:
    """종료 상태로 표기한 뒤 활성 포인터를 비운다(성공 커밋/시스템 실패 공통).

    현재 활성이 아니면 no-op. WP-01은 여기서 청구/변이/롤백을 하지 않는다.
    식별자 정리만 수행한다.
    """
    if require_current(session, transaction_id) is None:
        return False
    mark_status(session, transaction_id, status,
                failure_stage=failure_stage, failure_code=failure_code,
                failure_message=failure_message)
    return clear(session, transaction_id)
