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

import hashlib
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
#  Pricing basis 보존 (BILL-07)
#
#  CostEvent 는 최종 cost_usd/cost_krw 뿐 아니라, 그 비용을 산정한
#  가격 근거(가격표 버전 · 환율 · 모델 단가 스냅샷)를 함께 남겨야
#  나중에 과거 이벤트의 비용을 재현·감사할 수 있다. 아래 스냅샷을
#  기록 시점에 metadata["pricing_basis"] 로 immutable 하게 박아 넣는다.
#  (레거시 회계/accrue/pricing 계산 자체는 건드리지 않는다.)
# ══════════════════════════════════════════════════════════════

_PRICING_BASIS_VERSION_CACHE: dict = {"v": None}


def _pricing_tables():
    """(EXCHANGE_RATE, PRICING_1M) 를 lazy import 한다(순환참조 회피)."""
    from .constants import EXCHANGE_RATE, PRICING_1M
    return EXCHANGE_RATE, PRICING_1M


def pricing_basis_version() -> str:
    """현재 가격표+환율의 immutable 식별자.

    가격표(PRICING_1M) 또는 환율이 바뀌면 값이 바뀌는 결정적 지문.
    별도의 명시적 버전 상수가 없으므로 이에 준하는 식별자로 사용한다.
    """
    if _PRICING_BASIS_VERSION_CACHE["v"] is None:
        try:
            rate, table = _pricing_tables()
            blob = (json.dumps(table, sort_keys=True, ensure_ascii=False)
                    + f"|exchange_rate={rate}")
            digest = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
            _PRICING_BASIS_VERSION_CACHE["v"] = f"pt-{digest}"
        except Exception:  # noqa: BLE001
            _PRICING_BASIS_VERSION_CACHE["v"] = "pt-unknown"
    return _PRICING_BASIS_VERSION_CACHE["v"]


def pricing_basis_for(model) -> dict:
    """기록 시점의 immutable pricing basis 스냅샷.

    반환:
        {pricing_version, exchange_rate, model, rates, rate_unit}
        rates 는 해당 모델의 USD/1M 단가 스냅샷(PRICING_1M[model]).
        가격표에 없는 모델(예: 이미지)이면 rates=None 이나, version+환율로
        버전 고정이 되어 재현 근거는 유지된다.
    """
    try:
        rate, table = _pricing_tables()
    except Exception:  # noqa: BLE001
        return {"pricing_version": "pt-unknown", "exchange_rate": None,
                "model": model, "rates": None, "rate_unit": "USD_per_1M_tokens"}
    entry = table.get(model) if model else None
    return {
        "pricing_version": pricing_basis_version(),
        "exchange_rate": rate,
        "model": model,
        "rates": dict(entry) if isinstance(entry, dict) else None,
        "rate_unit": "USD_per_1M_tokens",
    }


# ══════════════════════════════════════════════════════════════
#  Per-path 동기화 레지스트리 (동시 기록 멱등 안전)
#
#  같은 ledger 파일 경로를 가리키는 서로 다른 CostLedger 인스턴스가
#  단일 프로세스 안에서 동시에 기록해도, check→append→dedup 갱신
#  전체가 하나의 임계구역이 되도록 lock 과 dedup 키 집합을 경로 단위로
#  공유한다. 그 결과 동일 idempotency_key 는 파일에 정확히 한 줄만 남는다.
# ══════════════════════════════════════════════════════════════

_LEDGER_REGISTRY: dict = {}
_REGISTRY_GUARD = threading.Lock()


def _registry_for(path: str) -> dict:
    """정규화된 절대경로 기준으로 공유 {lock, keys, loaded} 엔트리를 반환."""
    key = os.path.abspath(path)
    with _REGISTRY_GUARD:
        entry = _LEDGER_REGISTRY.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "keys": set(), "loaded": False}
            _LEDGER_REGISTRY[key] = entry
        return entry


# ══════════════════════════════════════════════════════════════
#  strict 재무 I/O 예외 (WP-ACCOUNTING-PREREQ-01 / AUD-062)
# ══════════════════════════════════════════════════════════════
#  shadow API(record_cost_event 등)는 관측용이라 실패를 삼키지만, 미래의
#  authoritative Settlement 는 "손상/누락 CostEvent 가 조용히 0/생략" 되면 안 된다.
#  strict 경로는 성공을 SUCCESS 로만, 실패를 아래 타입드 예외로만 신호한다.

class CostLedgerError(RuntimeError):
    """strict CostLedger 계열 예외의 베이스."""


class CostLedgerPersistenceError(CostLedgerError):
    """strict append 의 내구성 경계(write→flush→fsync) 또는 열기/읽기 실패."""


class CostLedgerCorruptionError(CostLedgerError):
    """malformed/구조적으로 무효한 CostEvent 라인(조용히 skip 하지 않는다)."""


class CostLedgerConflictError(CostLedgerError):
    """같은 idempotency_key 에 상충 payload, 또는 디스크상 중복 event_id."""


class CostEventNotFoundError(CostLedgerError):
    """strict exact-ID 조회에서 요청한 event_id 를 찾지 못함."""


# canonical payload = 재생성되는 envelope 필드를 제외한 durable 사실 전체.
# 정상 replay 마다 자연히 달라지는 event_id/created_at 은 동등성 비교에서 뺀다.
# 나머지(provider/operation/model/session/transaction/turn/attempt/actor/
# billing_hint/usage·token/cost/usage_source/success/metadata)는 attribution·
# billing·audit·가격 재구성에 영향을 주므로 전부 포함한다(§5.3).
_COST_ENVELOPE_FIELDS = ("event_id", "created_at")


def _canonical_cost_payload(obj: dict) -> dict:
    return {k: v for k, v in obj.items() if k not in _COST_ENVELOPE_FIELDS}


def _cost_fingerprint(obj: dict) -> str:
    """canonical payload 의 결정적 직렬화(중첩 dict 포함 키 정렬)."""
    return json.dumps(_canonical_cost_payload(obj), sort_keys=True,
                      ensure_ascii=False)


def _validate_cost_row_strict(obj: object, where: str) -> dict:
    """strict 파서용 구조 검증. envelope 필수 필드 부재/무효는 손상으로 본다."""
    if not isinstance(obj, dict):
        raise CostLedgerCorruptionError(f"{where}: CostEvent 레코드가 객체가 아님")
    eid = obj.get("event_id")
    key = obj.get("idempotency_key")
    if not isinstance(eid, str) or not eid:
        raise CostLedgerCorruptionError(f"{where}: event_id 무효")
    if not isinstance(key, str) or not key:
        raise CostLedgerCorruptionError(f"{where}: idempotency_key 무효")
    return obj


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
        # 같은 경로를 가리키는 모든 인스턴스가 lock/dedup 상태를 공유한다.
        self._reg = _registry_for(path)

    @property
    def _lock(self):
        return self._reg["lock"]

    @property
    def _keys(self) -> set:
        return self._reg["keys"]

    def _load_keys_locked(self):
        if self._reg["loaded"]:
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
                            self._reg["keys"].add(k)
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] 기존 원장 로드 실패(무시): {type(e).__name__} - {e}")
        self._reg["loaded"] = True

    def has_idempotency_key(self, key: str) -> bool:
        with self._reg["lock"]:
            self._load_keys_locked()
            return key in self._reg["keys"]

    def record_cost_event(self, event: CostEvent) -> bool:
        """새 이벤트를 append 한다. 중복 키면 False, 성공 시 True.

        check→append→dedup 갱신 전체가 경로 단위 공유 lock 안의 단일
        임계구역이므로, 동일 경로의 다른 인스턴스/스레드가 동시에 같은
        키를 기록하려 해도 파일에는 정확히 한 줄만 남는다.
        관측 실패가 레거시 호출 동작을 바꾸지 않도록 예외는 삼킨다.
        """
        try:
            with self._reg["lock"]:
                self._load_keys_locked()
                if event.idempotency_key in self._reg["keys"]:
                    return False
                directory = os.path.dirname(self.path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                line = json.dumps(asdict(event), ensure_ascii=False)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._reg["keys"].add(event.idempotency_key)
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

    # ── strict 경로 (WP-ACCOUNTING-PREREQ-01) ─────────────────────
    #   shadow 메서드(record_cost_event/list_cost_events/sum_cost_events)는
    #   위에서 그대로 유지된다. 아래는 미래 authoritative Settlement 가 소비할
    #   내구·명시적 프리미티브다. 현재 어떤 프로덕션 호출자도 쓰지 않는다.

    def _truncate_to(self, size: int) -> None:
        """부분 append 롤백 — 실패 지점 이전 크기로 되돌린다(best-effort)."""
        try:
            with open(self.path, "r+b") as f:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    def _read_all_strict_locked(self) -> list[dict]:
        """파일 전체를 strict 파싱. 손상은 조용히 skip 하지 않고 raise 한다.

        부재/빈 파일 → []. 열기 실패 → CostLedgerPersistenceError. malformed JSON
        또는 구조 무효 → CostLedgerCorruptionError. 반드시 lock 하에 호출한다.
        """
        if not os.path.exists(self.path):
            return []
        try:
            f = open(self.path, "r", encoding="utf-8")
        except OSError as e:
            raise CostLedgerPersistenceError(f"원장 열기 실패: {self.path}") from e
        rows: list[dict] = []
        with f:
            for lineno, raw in enumerate(f, start=1):
                s = raw.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception as e:
                    raise CostLedgerCorruptionError(
                        f"{self.path}:{lineno} JSON 파싱 실패") from e
                rows.append(_validate_cost_row_strict(obj, f"{self.path}:{lineno}"))
        return rows

    def record_cost_event_strict(self, event: CostEvent) -> bool:
        """CostEvent 를 내구적으로 append 한다. 신규 성공이면 True.

        payload 인지 idempotency(§5.3): 같은 idempotency_key +
          - 동등 canonical payload → append 없이 False (idempotent no-op)
          - 상충 canonical payload → CostLedgerConflictError, append 없음
        디스크상 중복 event_id 또는 같은 key 상충 payload 를 발견하면 손상/상충으로
        간주해 append 하지 않는다. 내구성 경계(write→flush→fsync) 실패는 삼키지 않고
        부분 기록을 롤백한 뒤 CostLedgerPersistenceError 로 올린다(§5.2). 성공은
        예외 부재 + True 로만 신호한다. shadow record_cost_event 와 달리 실패에
        False 를 반환하지 않는다(§9).
        """
        key = event.idempotency_key
        incoming = asdict(event)
        fingerprint = _cost_fingerprint(incoming)
        with self._reg["lock"]:
            rows = self._read_all_strict_locked()   # 손상/열기 실패 시 raise
            by_key: dict[str, str] = {}
            seen_ids: set[str] = set()
            for o in rows:
                oid = o["event_id"]
                if oid in seen_ids:
                    raise CostLedgerConflictError(f"중복 event_id(원장 손상): {oid}")
                seen_ids.add(oid)
                k = o["idempotency_key"]
                ofp = _cost_fingerprint(o)
                prev = by_key.get(k)
                if prev is not None and prev != ofp:
                    raise CostLedgerConflictError(f"디스크상 idempotency_key 상충: {k}")
                by_key[k] = ofp

            existing = by_key.get(key)
            if existing is not None:
                if existing == fingerprint:
                    return False                     # 동등 → idempotent no-op
                raise CostLedgerConflictError(       # 상충 → 명시적 거부
                    f"idempotency_key 상충(strict append 거부): {key}")

            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            line = json.dumps(incoming, ensure_ascii=False) + "\n"
            pre_size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())             # 내구성 경계(§5.2)
            except Exception as e:
                self._truncate_to(pre_size)
                raise CostLedgerPersistenceError(
                    f"strict append 실패: {self.path} key={key}") from e

            # shadow dedup 캐시 일관성: tolerant 경로가 같은 키를 중복 기록하지
            # 않도록 성공 후 공유 키 집합에도 등재한다.
            self._reg["keys"].add(key)
            return True

    def list_cost_events_strict(self, *, session_id=None,
                                transaction_id=None) -> list[dict]:
        """strict 파싱된 이력(손상 시 raise). 선택적 session/transaction 필터."""
        with self._reg["lock"]:
            rows = self._read_all_strict_locked()
        out = []
        for o in rows:
            if session_id is not None and o.get("session_id") != session_id:
                continue
            if transaction_id is not None and o.get("transaction_id") != transaction_id:
                continue
            out.append(o)
        return out

    def get_cost_events_by_ids_strict(self, event_ids, *, session_id=None,
                                      transaction_id=None) -> list[dict]:
        """요청한 event_id 집합 → 정확히 대응하는 CostEvent 레코드(§5.5).

        미래 Settlement 가 불변 이벤트 집합을 명시적으로 소비하기 위한 API다.
        각 요청 ID 는 정확히 한 번 존재해야 하며, 누락은 CostEventNotFoundError,
        디스크상 중복 event_id 는 CostLedgerConflictError 다. 가변 세션 합계에서
        settlement 소속을 추론하지 않는다.
        """
        requested = list(event_ids)
        with self._reg["lock"]:
            rows = self._read_all_strict_locked()
        index: dict[str, dict] = {}
        for o in rows:
            oid = o["event_id"]
            if oid in index:
                raise CostLedgerConflictError(f"중복 event_id(원장 손상): {oid}")
            index[oid] = o
        out = []
        for rid in requested:
            o = index.get(rid)
            if o is None:
                raise CostEventNotFoundError(f"요청 event_id 없음: {rid}")
            if session_id is not None and o.get("session_id") != session_id:
                raise CostEventNotFoundError(f"event_id {rid} 가 요청 session 범위 밖")
            if transaction_id is not None and o.get("transaction_id") != transaction_id:
                raise CostEventNotFoundError(f"event_id {rid} 가 요청 transaction 범위 밖")
            out.append(o)
        return out


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
        _model = model or self.model
        md = dict(self.metadata)
        if extra_metadata:
            md.update(extra_metadata)
        # BILL-07: 비용 산정 근거(가격표 버전·환율·모델 단가)를 함께 보존.
        md["pricing_basis"] = pricing_basis_for(_model)
        event = CostEvent(
            event_id=uuid.uuid4().hex,
            idempotency_key=f"{self.operation_id}:attempt:{pa}",
            created_at=time.time(),
            provider=PROVIDER_GOOGLE_GENAI,
            operation=self.operation,
            model=_model,
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
    # BILL-07: 비용 산정 근거(가격표 버전·환율·모델 단가)를 함께 보존.
    md["pricing_basis"] = pricing_basis_for(ctx.model)
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
