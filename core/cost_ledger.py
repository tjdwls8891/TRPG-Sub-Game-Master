# WP-02 — Append-only shadow CostLedger (provider usage observation)
#
# 목적: 실제 provider(Google GenAI) 호출의 사용량/비용을 "관측"만 한다.
#   - 레거시 회계(core.accrue / session.total_cost / total_usd), 플레이어 잉크,
#     턴/커밋/추출 타이밍, 캐시 선불 정책을 일절 바꾸지 않는다(shadow 모드).
#   - 기록은 append-only이며, mutable session JSON과 완전히 분리된 별도 파일
#     (기본 data/cost_ledger.jsonl)에 남는다. 따라서 rewind/rerender/세션 롤백이
#     이 원장을 수정하거나 삭제하지 못한다(BILL-01/BILL-08).
#
# 핵심 구분(Part XII):
#   - observed provider fact  vs  estimate
#   - CostEvent                vs  플레이어 청구/Settlement
#   - TurnTransaction attempt  vs  provider attempt
#   - legacy authoritative     vs  new shadow ledger
#
# billing_hint 은 최종 청구 책임이 아니라 힌트일 뿐이다. 최종 플레이어 부담은
# 트랜잭션 결과가 확정된 뒤 Settlement(후속 WP)이 정한다.

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field


# ══════════════════════════════════════════════════════════════
#  Taxonomy 상수 (UI 라벨이 아닌 안정적 식별자)
# ══════════════════════════════════════════════════════════════

# actor_kind
ACTOR_PLAYER = "PLAYER"
ACTOR_SYSTEM = "SYSTEM"
ACTOR_OWNER = "OWNER"
ACTOR_UNKNOWN = "UNKNOWN"

# billing_hint — NOT settlement, NOT 최종 책임
HINT_PLAYER_CANDIDATE = "PLAYER_CANDIDATE"
HINT_SYSTEM = "SYSTEM"
HINT_FREE_FEATURE = "FREE_FEATURE"
HINT_OPERATOR = "OPERATOR"
HINT_UNKNOWN = "UNKNOWN"

# usage_source — 관측 출처(추정과 실측을 절대 섞지 않는다)
SOURCE_PROVIDER_METADATA = "PROVIDER_METADATA"
SOURCE_FIXED_PROVIDER_PRICING = "FIXED_PROVIDER_PRICING"
SOURCE_ESTIMATE = "ESTIMATE"
SOURCE_NONE = "NONE"

# operation keys
OP_TURN_JUDGMENT = "TURN_JUDGMENT"
OP_TURN_SIMULATION = "TURN_SIMULATION"
OP_TURN_INSTRUCTION = "TURN_INSTRUCTION"
OP_TURN_INSTRUCTION_VALIDATION = "TURN_INSTRUCTION_VALIDATION"
OP_TURN_NARRATION = "TURN_NARRATION"
OP_TURN_LIGHT_NARRATE = "TURN_LIGHT_NARRATE"
OP_TURN_EXTRACTION = "TURN_EXTRACTION"
OP_TURN_NARRATIVE_PLANNING = "TURN_NARRATIVE_PLANNING"
OP_TURN_NPC_PROFILE = "TURN_NPC_PROFILE"
OP_TURN_IRREGULAR_NPC = "TURN_IRREGULAR_NPC"
OP_MEMORY_AUTO_COMPRESSION = "MEMORY_AUTO_COMPRESSION"
OP_MEMORY_MANUAL_COMPRESSION = "MEMORY_MANUAL_COMPRESSION"
OP_PROFILE_AI = "PROFILE_AI"
OP_CACHE_TIME_INTERPRET = "CACHE_TIME_INTERPRET"
OP_CACHE_CREATE = "CACHE_CREATE"
OP_CACHE_STORAGE = "CACHE_STORAGE"
OP_CACHE_RECOVERY_CREATE = "CACHE_RECOVERY_CREATE"
OP_IMAGE_GENERATION = "IMAGE_GENERATION"
OP_TTS_RUNTIME = "TTS_RUNTIME"
OP_TTS_TEST = "TTS_TEST"
OP_TTS_PRESET_BUILD = "TTS_PRESET_BUILD"
OP_CHARACTER_DETAIL_GENERATION = "CHARACTER_DETAIL_GENERATION"

PROVIDER_GOOGLE_GENAI = "google_genai"

DEFAULT_LEDGER_PATH = os.path.join("data", "cost_ledger.jsonl")


# ══════════════════════════════════════════════════════════════
#  CostEvent — 불변 사실 레코드
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class CostEvent:
    event_id: str
    idempotency_key: str
    created_at: float

    provider: str
    operation: str
    model: str | None

    session_id: str | None
    transaction_id: str | None
    logical_turn: int | None
    turn_attempt: int | None
    provider_attempt: int | None

    actor_user_id: str | None
    actor_kind: str
    billing_hint: str

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    thought_tokens: int = 0
    image_output_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0

    cost_usd: float = 0.0
    cost_krw: float = 0.0

    usage_source: str = SOURCE_PROVIDER_METADATA
    success: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class CostTotals:
    count: int = 0
    cost_usd: float = 0.0
    cost_krw: float = 0.0


# ══════════════════════════════════════════════════════════════
#  CostLedger — append-only JSONL sink
# ══════════════════════════════════════════════════════════════

class CostLedger:
    """append-only JSONL 원장.

    - path 를 주입 가능하게 두어(seam) 테스트는 tmp_path 등 격리 경로를 쓴다.
    - 운영은 기본 data/cost_ledger.jsonl 을 쓰며 이 파일은 .gitignore 로 제외한다.
    - 동일 idempotency_key 는 두 번 append 되지 않는다.
    """

    def __init__(self, path: str = DEFAULT_LEDGER_PATH):
        self.path = path
        self._keys: set[str] = set()
        self._loaded = False
        self._lock = threading.Lock()

    def _load_keys_locked(self):
        if self._loaded:
            return
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except Exception:
                            continue
                        k = obj.get("idempotency_key")
                        if k:
                            self._keys.add(k)
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] 기존 원장 로드 실패(무시): {type(e).__name__} - {e}")
        self._loaded = True

    def has_idempotency_key(self, key: str) -> bool:
        with self._lock:
            self._load_keys_locked()
            return key in self._keys

    def record_cost_event(self, event: CostEvent) -> bool:
        """새 이벤트를 append 한다. 중복 키면 False, 성공 시 True.

        관측 실패가 레거시 호출 동작을 바꾸지 않도록 예외는 삼킨다.
        """
        try:
            with self._lock:
                self._load_keys_locked()
                if event.idempotency_key in self._keys:
                    return False
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                line = json.dumps(asdict(event), ensure_ascii=False)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._keys.add(event.idempotency_key)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] 기록 실패(무시): {type(e).__name__} - {e}")
            return False

    def list_cost_events(self, *, session_id=None, transaction_id=None) -> list[dict]:
        out: list[dict] = []
        try:
            if not os.path.exists(self.path):
                return out
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if session_id is not None and obj.get("session_id") != session_id:
                        continue
                    if transaction_id is not None and obj.get("transaction_id") != transaction_id:
                        continue
                    out.append(obj)
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] 조회 실패(무시): {type(e).__name__} - {e}")
        return out

    def sum_cost_events(self, *, session_id=None, transaction_id=None,
                        operation=None, billing_hint=None) -> CostTotals:
        totals = CostTotals()
        for obj in self.list_cost_events(session_id=session_id,
                                         transaction_id=transaction_id):
            if operation is not None and obj.get("operation") != operation:
                continue
            if billing_hint is not None and obj.get("billing_hint") != billing_hint:
                continue
            totals.count += 1
            totals.cost_usd += float(obj.get("cost_usd") or 0.0)
            totals.cost_krw += float(obj.get("cost_krw") or 0.0)
        return totals


# ══════════════════════════════════════════════════════════════
#  헬퍼
# ══════════════════════════════════════════════════════════════

def new_operation_id(operation: str) -> str:
    """논리 오퍼레이션 1건당 한 번, 재시도 루프 시작 전에 생성한다."""
    return f"{operation}:{uuid.uuid4().hex}"


def get_ledger(bot):
    """best-effort ledger 접근자. 없으면 None → shadow no-op."""
    if bot is None:
        return None
    return getattr(bot, "cost_ledger", None)


def _attr_from_session(session):
    """(session_id, transaction_id, logical_turn, turn_attempt) — 비파괴적.

    현재 활성 TurnTransaction 이 있으면 불변 식별자를 복사한다(WP-01 의도).
    없으면 transaction 계열은 None.
    """
    session_id = getattr(session, "session_id", None) if session is not None else None
    txn_id = logical_turn = turn_attempt = None
    if session is not None:
        try:
            from .turn_transaction import get_active_transaction
            txn = get_active_transaction(session)
            if txn is not None:
                txn_id = getattr(txn, "transaction_id", None)
                logical_turn = getattr(txn, "logical_turn", None)
                turn_attempt = getattr(txn, "attempt", None)
        except Exception:
            pass
    return session_id, txn_id, logical_turn, turn_attempt


@dataclass
class ProviderCostContext:
    """호출자가 공급하는 관측 컨텍스트(특히 TTS 공유 헬퍼).

    호출자가 operation key / session / transaction / actor / billing_hint 를
    정하고, SDK 를 소유한 헬퍼가 실제 provider attempt 를 관측한다.
    """
    operation: str
    session_id: str | None = None
    transaction_id: str | None = None
    logical_turn: int | None = None
    turn_attempt: int | None = None
    actor_kind: str = ACTOR_UNKNOWN
    billing_hint: str = HINT_UNKNOWN
    model: str | None = None
    actor_user_id: str | None = None
    metadata: dict = field(default_factory=dict)
    operation_id: str = ""

    def ensure_operation_id(self) -> str:
        if not self.operation_id:
            self.operation_id = new_operation_id(self.operation)
        return self.operation_id


class ProviderOperation:
    """하나의 논리 provider 오퍼레이션.

    - 실제 provider attempt 를 센다(재시도·외부 루프 포함).
    - 성공한 provider 응답에 한해 레거시가 이미 계산한 usage/cost 로
      CostEvent 를 post-hoc 기록한다(같은 공식 → shadow == legacy).
    - 실패/메타데이터 부재 attempt 는 CostEvent 를 날조하지 않는다.
    - 귀속(attribution)은 생성 시점의 활성 TurnTransaction 에서 복사한다.
    """

    def __init__(self, ledger, *, operation, session=None, model=None,
                 actor_kind=ACTOR_UNKNOWN, billing_hint=HINT_UNKNOWN,
                 actor_user_id=None, operation_id=None, metadata=None,
                 copy_transaction=True):
        self.ledger = ledger
        self.operation = operation
        self.model = model
        self.actor_kind = actor_kind
        self.billing_hint = billing_hint
        self.actor_user_id = actor_user_id
        self.operation_id = operation_id or new_operation_id(operation)
        self.metadata = dict(metadata or {})
        self._attempt = 0
        self.last_success = None

        sid, tid, lt, ta = (
            _attr_from_session(session) if session is not None else (None, None, None, None)
        )
        self.session_id = sid
        self.transaction_id = tid if copy_transaction else None
        self.logical_turn = lt if copy_transaction else None
        self.turn_attempt = ta if copy_transaction else None

    def mark_attempt(self) -> int:
        """실제 SDK 호출 1회를 카운트한다(직접 계측용)."""
        self._attempt += 1
        return self._attempt

    def on_attempt(self, *, attempt=None, success=True, response=None,
                   exception=None, operation_id=None):
        """call_with_retry 콜백. provider attempt 1건 완료 시 호출된다.

        여기서는 attempt 계수만 한다. 실패/무메타 attempt 로 CostEvent 를
        만들지 않는다(정책). 실제 CostEvent 는 호출부가 성공 응답의
        레거시 계산값으로 record() 한다.
        """
        self.mark_attempt()
        self.last_success = success

    @property
    def current_attempt(self) -> int:
        return self._attempt or 1

    def record(self, *, cost_usd=0.0, cost_krw=0.0,
               usage_source=SOURCE_PROVIDER_METADATA, provider_attempt=None,
               success=True, model=None,
               input_tokens=0, cached_input_tokens=0, output_tokens=0,
               thought_tokens=0, image_output_tokens=0, audio_input_tokens=0,
               audio_output_tokens=0, extra_metadata=None) -> bool:
        """성공한 provider 응답 1건을 CostEvent 로 기록한다(멱등)."""
        if self.ledger is None:
            return False
        pa = provider_attempt if provider_attempt is not None else self.current_attempt
        md = dict(self.metadata)
        if extra_metadata:
            md.update(extra_metadata)
        event = CostEvent(
            event_id=uuid.uuid4().hex,
            idempotency_key=f"{self.operation_id}:attempt:{pa}",
            created_at=time.time(),
            provider=PROVIDER_GOOGLE_GENAI,
            operation=self.operation,
            model=model or self.model,
            session_id=self.session_id,
            transaction_id=self.transaction_id,
            logical_turn=self.logical_turn,
            turn_attempt=self.turn_attempt,
            provider_attempt=pa,
            actor_user_id=self.actor_user_id,
            actor_kind=self.actor_kind,
            billing_hint=self.billing_hint,
            input_tokens=int(input_tokens or 0),
            cached_input_tokens=int(cached_input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            thought_tokens=int(thought_tokens or 0),
            image_output_tokens=int(image_output_tokens or 0),
            audio_input_tokens=int(audio_input_tokens or 0),
            audio_output_tokens=int(audio_output_tokens or 0),
            cost_usd=float(cost_usd or 0.0),
            cost_krw=float(cost_krw or 0.0),
            usage_source=usage_source,
            success=success,
            metadata=md,
        )
        try:
            return self.ledger.record_cost_event(event)
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] record 실패(무시): {type(e).__name__} - {e}")
            return False


def begin_operation(bot, operation, *, session=None, model=None,
                    actor_kind=ACTOR_UNKNOWN, billing_hint=HINT_UNKNOWN,
                    actor_user_id=None, operation_id=None, metadata=None,
                    copy_transaction=True) -> ProviderOperation:
    """호출부 편의 팩토리. bot 에서 ledger 를 찾아 ProviderOperation 을 만든다.

    ledger 가 없으면(예: 테스트 fake bot 에 미부착) record() 는 no-op 이며
    레거시 동작에 영향을 주지 않는다.
    """
    return ProviderOperation(
        get_ledger(bot), operation=operation, session=session, model=model,
        actor_kind=actor_kind, billing_hint=billing_hint,
        actor_user_id=actor_user_id, operation_id=operation_id,
        metadata=metadata, copy_transaction=copy_transaction)


def tts_context(bot, session, scope) -> ProviderCostContext:
    """synthesize_tts_pcm 용 caller 공급 컨텍스트 생성.

    scope: OP_TTS_RUNTIME | OP_TTS_TEST | OP_TTS_PRESET_BUILD.
    런타임 더빙만 활성 턴 식별자를 복사한다. 테스트/프리셋은 복사하지 않는다.
    """
    try:
        from .constants import TTS_MODEL
    except Exception:
        TTS_MODEL = None

    if scope == OP_TTS_RUNTIME:
        sid, tid, lt, ta = _attr_from_session(session)
        actor, hint = ACTOR_PLAYER, HINT_PLAYER_CANDIDATE
    elif scope == OP_TTS_TEST:
        sid = getattr(session, "session_id", None) if session is not None else None
        tid = lt = ta = None
        actor, hint = ACTOR_OWNER, HINT_SYSTEM
    else:  # OP_TTS_PRESET_BUILD
        sid = getattr(session, "session_id", None) if session is not None else None
        tid = lt = ta = None
        actor, hint = ACTOR_OWNER, HINT_OPERATOR

    ctx = ProviderCostContext(
        operation=scope, session_id=sid, transaction_id=tid,
        logical_turn=lt, turn_attempt=ta, actor_kind=actor,
        billing_hint=hint, model=TTS_MODEL)
    ctx.ensure_operation_id()
    return ctx


def record_context_event(bot, ctx, *, cost_usd=0.0, cost_krw=0.0,
                         usage_source=SOURCE_PROVIDER_METADATA,
                         provider_attempt=1, success=True,
                         extra_metadata=None, **token_fields) -> bool:
    """caller 공급 ProviderCostContext 로 CostEvent 를 기록한다(TTS 헬퍼).

    ledger/ctx 가 없으면 no-op.
    """
    ledger = get_ledger(bot)
    if ledger is None or ctx is None:
        return False
    op_id = ctx.ensure_operation_id()
    md = dict(ctx.metadata)
    if extra_metadata:
        md.update(extra_metadata)
    event = CostEvent(
        event_id=uuid.uuid4().hex,
        idempotency_key=f"{op_id}:attempt:{provider_attempt}",
        created_at=time.time(),
        provider=PROVIDER_GOOGLE_GENAI,
        operation=ctx.operation,
        model=ctx.model,
        session_id=ctx.session_id,
        transaction_id=ctx.transaction_id,
        logical_turn=ctx.logical_turn,
        turn_attempt=ctx.turn_attempt,
        provider_attempt=provider_attempt,
        actor_user_id=ctx.actor_user_id,
        actor_kind=ctx.actor_kind,
        billing_hint=ctx.billing_hint,
        input_tokens=int(token_fields.get("input_tokens", 0) or 0),
        cached_input_tokens=int(token_fields.get("cached_input_tokens", 0) or 0),
        output_tokens=int(token_fields.get("output_tokens", 0) or 0),
        thought_tokens=int(token_fields.get("thought_tokens", 0) or 0),
        image_output_tokens=int(token_fields.get("image_output_tokens", 0) or 0),
        audio_input_tokens=int(token_fields.get("audio_input_tokens", 0) or 0),
        audio_output_tokens=int(token_fields.get("audio_output_tokens", 0) or 0),
        cost_usd=float(cost_usd or 0.0),
        cost_krw=float(cost_krw or 0.0),
        usage_source=usage_source,
        success=success,
        metadata=md,
    )
    try:
        return ledger.record_cost_event(event)
    except Exception as e:  # noqa: BLE001
        print(f"[CostLedger] record_context 실패(무시): {type(e).__name__} - {e}")
        return False
