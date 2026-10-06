# 세계 변이 묶음 · 투영 세계 · 병합/충돌 기반 (H02-B)
#
# [위치 — DESIGN 01 §25 · DESIGN 05 · DESIGN 11]
#   한 인과 단위의 정본 세계 변화는 WorldMutationBatch 하나로 묶는다. 묶음은
#     구성(add) → 동결(freeze: 검증·LocalRef 해석·digest) → 투영(project: 정본 미변경)
#   순서를 지나고, 정본 적용은 각 outer owner가 자기 원자 경계 안에서만 한다:
#     PLAYER_TURN           → CommitCoordinator(기존 단일 owner)
#     INITIALIZATION/ADMIN  → core.canonical_mutation(H02: production 호출자 0)
#
# [하지 않는 것]
#   · 실제 시간·공간·사건·정보 연산을 정의하지 않는다(H03 이후 typed handler로 등록).
#     H02 production 레지스트리는 비어 있다 — production 턴의 묶음은 항상 빈 묶음이다.
#   · 임의 경로 setter("어떤 dotted path든 써라")를 연산으로 제공하지 않는다. 쓰기는
#     등록된 typed handler만 만들 수 있고, handler가 선언한 병합 의미로만 합쳐진다.
#   · 같은 단일값 칸에 서로 다른 값을 쓰면 마지막 쓰기를 채택하지 않는다 —
#     WORLD_PROJECTION_CONFLICT 로 결정적으로 거부한다(DESIGN 11 §28~29).
#   · world_version 은 연산이 쓰지 못한다. 커밋 프레임워크가 자동 생성한다(DESIGN 11 §11).
#   · callable·lambda·eval 문자열·임의 Python 객체를 payload에 담지 않는다(정규 JSON만).
#   · provider를 부르지 않는다. 투영 충돌은 provider 재시도 대상이 아니다.
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from . import world_state as _ws
from .world_ids import CanonicalSerializationError, canonical_json, content_digest, stable_digest

BATCH_SCHEMA_VERSION = 1

# 투영·연산이 다룰 수 있는 정본 도메인 = 게임 정본 필드 − world_version(프레임워크 소유)
WRITABLE_DOMAINS = tuple(f for f in _ws.GAMEPLAY_WORLD_FIELDS if f != "world_version")
PROJECTED_DOMAINS = tuple(_ws.GAMEPLAY_WORLD_FIELDS)

# 병합 의미(DESIGN 05 · H02 handoff §5.6)
MERGE_SINGLE_VALUE = "SINGLE_VALUE"     # 같은 칸의 서로 다른 값 = 충돌
MERGE_SET_LIKE = "SET_LIKE"             # 원소 추가/제거 — 같은 원소의 추가·제거 동시 = 충돌
MERGE_ADDITIVE = "ADDITIVE"             # 수치 증분의 합
MERGE_APPEND_DEDUPE = "APPEND_DEDUPE"   # 묶음 순서대로 덧붙이되 정규 동일값은 한 번
# H03: 키 삭제(예약 취소·소비 등 live 집합에서 제거). handler만 낼 수 있는 typed 의미이며
#   임의 경로 삭제 기능이 아니다. 같은 칸의 삭제끼리는 멱등, 삭제와 다른 쓰기의 혼용은 충돌,
#   없는 키의 삭제도 충돌이다(조용한 무시 금지).
MERGE_DELETE = "DELETE"
MERGE_SEMANTICS = (MERGE_SINGLE_VALUE, MERGE_SET_LIKE, MERGE_ADDITIVE, MERGE_APPEND_DEDUPE,
                   MERGE_DELETE)
_DELETED = object()

# 충돌·실패 분류 — provider 일시 실패와 다른 결정적 세계 충돌 계열
WORLD_PROJECTION_CONFLICT = "WORLD_PROJECTION_CONFLICT"

LOCAL_REF_KEY = "$local"


class WorldMutationError(RuntimeError):
    """묶음 구성·동결 규칙 위반(호출자 버그)."""


class BatchFrozenError(WorldMutationError):
    """동결된 묶음에 연산을 더하거나 고치려 했다."""


class WorldProjectionConflict(RuntimeError):
    """결정적 세계 투영 충돌. 재시도해도 해결되지 않는다(RETRY_PENDING 금지)."""

    failure_class = WORLD_PROJECTION_CONFLICT

    def __init__(self, conflicts):
        self.conflicts = tuple(conflicts)
        super().__init__("; ".join(c.message for c in self.conflicts) or WORLD_PROJECTION_CONFLICT)


def _canonical_copy(value, what: str):
    """정규 JSON으로 표현 가능한 값의 깊은 사본. 아니면 WorldMutationError."""
    try:
        canonical_json(value)
    except CanonicalSerializationError as e:
        raise WorldMutationError(f"{what}: 정규 JSON이 아닙니다 — {e}") from e
    return copy.deepcopy(value)


# ══════════════════════════════════════════════════════════════
#  LocalRef
# ══════════════════════════════════════════════════════════════

def local_ref(name: str) -> dict:
    """묶음 안에서 새로 만들어질 대상의 임시 참조. 동결 때 결정적 id로 해석된다."""
    if not isinstance(name, str) or not name:
        raise WorldMutationError("LocalRef 이름이 필요합니다")
    return {LOCAL_REF_KEY: name}


def _is_local_ref(v) -> bool:
    return isinstance(v, dict) and set(v) == {LOCAL_REF_KEY} and isinstance(v[LOCAL_REF_KEY], str)


def _collect_local_refs(value, out: list):
    if _is_local_ref(value):
        out.append(value[LOCAL_REF_KEY])
    elif isinstance(value, dict):
        for v in value.values():
            _collect_local_refs(v, out)
    elif isinstance(value, list):
        for v in value:
            _collect_local_refs(v, out)


def _resolve_local_refs(value, table: dict):
    if _is_local_ref(value):
        return table[value[LOCAL_REF_KEY]]
    if isinstance(value, dict):
        return {k: _resolve_local_refs(v, table) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_local_refs(v, table) for v in value]
    return value


# ══════════════════════════════════════════════════════════════
#  연산 · 묶음
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WorldMutationOp:
    """직렬화 가능한 typed 연산 봉투. 의미는 op_type에 등록된 handler가 정한다."""
    op_id: str
    op_type: str
    source_kind: str
    payload: dict = field(default_factory=dict)
    source_ref: str | None = None
    causal_parent_op_ids: tuple = ()
    expected: dict | None = None        # {"domain", "path", "value"} — 투영 전 원본 대조
    defines_locals: tuple = ()          # 이 연산이 새로 만드는 LocalRef 이름

    def to_dict(self) -> dict:
        return {"op_id": self.op_id, "op_type": self.op_type, "source_kind": self.source_kind,
                "source_ref": self.source_ref,
                "causal_parent_op_ids": list(self.causal_parent_op_ids),
                "expected": copy.deepcopy(self.expected), "payload": copy.deepcopy(self.payload),
                "defines_locals": list(self.defines_locals)}

    @classmethod
    def from_dict(cls, d: dict) -> "WorldMutationOp":
        return make_op(d["op_id"], d["op_type"], d["source_kind"], d.get("payload") or {},
                       source_ref=d.get("source_ref"),
                       causal_parent_op_ids=d.get("causal_parent_op_ids") or (),
                       expected=d.get("expected"), defines_locals=d.get("defines_locals") or ())


def make_op(op_id, op_type, source_kind, payload=None, *, source_ref=None,
            causal_parent_op_ids=(), expected=None, defines_locals=()) -> WorldMutationOp:
    """검증된 연산을 만든다(payload는 정규 JSON 사본 — 이후 바깥 변경과 분리)."""
    for name, v in (("op_id", op_id), ("op_type", op_type), ("source_kind", source_kind)):
        if not isinstance(v, str) or not v:
            raise WorldMutationError(f"{name}는 비어 있지 않은 문자열이어야 합니다")
    if expected is not None:
        if not (isinstance(expected, dict) and expected.get("domain") in PROJECTED_DOMAINS
                and isinstance(expected.get("path"), (list, tuple))):
            raise WorldMutationError(f"{op_id}: expected 형식이 잘못됐습니다")
    return WorldMutationOp(
        op_id=op_id, op_type=op_type, source_kind=source_kind,
        payload=_canonical_copy(payload or {}, f"{op_id}.payload"),
        source_ref=source_ref,
        causal_parent_op_ids=tuple(str(x) for x in causal_parent_op_ids),
        expected=_canonical_copy(expected, f"{op_id}.expected") if expected is not None else None,
        defines_locals=tuple(str(x) for x in defines_locals))


class WorldMutationBatch:
    """한 인과 단위의 정본 변이 묶음. 동결 후에는 바꿀 수 없다.

    [실질적 불변성]
      · add()는 받은 연산을 정규 dict 스냅샷(깊은 사본)으로 저장한다 — 호출자가 이후 원래
        연산의 payload를 고쳐도 묶음은 바뀌지 않는다.
      · operations는 매번 새 사본을 돌려준다 — 돌려받은 연산을 고쳐도 묶음은 바뀌지 않는다.
      · 묶음 정체성(batch_id·origin·기준 version·정의 revision·tx)은 읽기 전용이다.
      · 투영은 내부 스냅샷만 읽고(resolved_payload는 op_id로 내부 스냅샷을 찾는다),
        동결 digest와 현재 내용의 일치를 확인한다(digest_matches).
    """

    def __init__(self, *, batch_id: str, outer_origin: str, source_world_version_id,
                 definition_revision, transaction_id: str | None = None):
        if outer_origin not in _ws.CAUSE_KINDS:
            raise WorldMutationError(f"알 수 없는 outer_origin: {outer_origin!r}")
        if not isinstance(batch_id, str) or not batch_id:
            raise WorldMutationError("batch_id가 필요합니다")
        self._meta = _canonical_copy({
            "batch_id": batch_id, "outer_origin": outer_origin,
            "transaction_id": transaction_id,
            "source_world_version_id": source_world_version_id,
            "definition_revision": definition_revision}, "batch identity")
        self._ops: list = []                 # 정규 dict 스냅샷(WorldMutationOp.to_dict 형식)
        self.diagnostics: list = []          # 진단 전용 — digest·투영에 쓰지 않는다
        self._frozen = False
        self._digest = None
        self._resolved_locals: dict = {}

    # ── 읽기 전용 정체성 ──
    batch_id = property(lambda self: self._meta["batch_id"])
    outer_origin = property(lambda self: self._meta["outer_origin"])
    transaction_id = property(lambda self: self._meta["transaction_id"])
    source_world_version_id = property(lambda self: self._meta["source_world_version_id"])
    definition_revision = property(lambda self: self._meta["definition_revision"])

    # ── 구성 ──
    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def operations(self) -> tuple:
        """연산의 새 사본(호출자 수정이 묶음에 닿지 않는다)."""
        return tuple(WorldMutationOp.from_dict(d) for d in self._ops)

    def add(self, op: WorldMutationOp) -> None:
        if self._frozen:
            raise BatchFrozenError(f"동결된 묶음 {self.batch_id}에 연산을 더할 수 없습니다")
        if not isinstance(op, WorldMutationOp):
            raise WorldMutationError("WorldMutationOp만 더할 수 있습니다")
        # make_op 재검증을 거친 깊은 사본 — 생성 뒤 바뀐 payload(비정규 값 포함)도 여기서 걸린다
        self._ops.append(WorldMutationOp.from_dict(op.to_dict()).to_dict())

    # ── 동결 ──
    def validate(self) -> list:
        """구조 규칙 위반 목록(비면 정상). 연산 의미는 handler 몫이라 보지 않는다."""
        errs, seen, defined = [], set(), {}
        ops = self.operations
        for op in ops:
            if op.op_id in seen:
                errs.append(f"OP_ID_DUPLICATE:{op.op_id}")
            for par in op.causal_parent_op_ids:
                if par not in seen:
                    errs.append(f"CAUSAL_PARENT_NOT_EARLIER:{op.op_id}->{par}")
            seen.add(op.op_id)
            for name in op.defines_locals:
                if name in defined:
                    errs.append(f"LOCAL_REF_REDEFINED:{name}")
                defined[name] = op.op_id
        anc = causal_ancestors(ops) if not errs else {}
        for op in ops:
            refs = []
            _collect_local_refs(op.payload, refs)
            for name in refs:
                if name not in defined:
                    errs.append(f"LOCAL_REF_UNDEFINED:{op.op_id}->{name}")
                elif defined[name] != op.op_id and defined[name] not in anc.get(op.op_id, ()):
                    errs.append(f"LOCAL_REF_NOT_CAUSAL:{op.op_id}->{name}")
        return errs

    def freeze(self) -> str:
        """검증 → LocalRef 결정적 해석 → digest 고정. 멱등. 반환: digest."""
        if self._frozen:
            return self._digest
        errs = self.validate()
        if errs:
            raise WorldMutationError(f"{self.batch_id}: 묶음 구조 오류 — " + ", ".join(errs))
        self._resolved_locals = {
            name: "local_" + stable_digest("world_mutation.local_ref.v1", self.batch_id, name)
            for d in self._ops for name in d["defines_locals"]}
        self._frozen = True
        self._digest = content_digest(self.to_dict())
        return self._digest

    @property
    def digest(self) -> str | None:
        return self._digest

    def digest_matches(self) -> bool:
        """동결 digest가 현재 내부 내용과 같은가(내부 상태 변조 방어)."""
        return self._frozen and content_digest(self.to_dict()) == self._digest

    def resolved_payload(self, op) -> dict:
        """op_id의 내부 스냅샷 payload를 LocalRef 해석해 돌려준다(전달된 객체 내용은 무시)."""
        if not self._frozen:
            raise WorldMutationError("동결 전에는 LocalRef를 해석하지 않습니다")
        op_id = op.op_id if isinstance(op, WorldMutationOp) else op
        for d in self._ops:
            if d["op_id"] == op_id:
                return _resolve_local_refs(copy.deepcopy(d["payload"]), self._resolved_locals)
        raise WorldMutationError(f"묶음에 없는 연산: {op_id}")

    def to_dict(self) -> dict:
        return {"batch_schema_version": BATCH_SCHEMA_VERSION, **copy.deepcopy(self._meta),
                "operations": copy.deepcopy(self._ops),
                "resolved_locals": dict(sorted(self._resolved_locals.items()))}

    @classmethod
    def from_dict(cls, d: dict) -> "WorldMutationBatch":
        b = cls(batch_id=d["batch_id"], outer_origin=d["outer_origin"],
                source_world_version_id=d.get("source_world_version_id"),
                definition_revision=d.get("definition_revision"),
                transaction_id=d.get("transaction_id"))
        for od in d.get("operations") or ():
            b.add(WorldMutationOp.from_dict(od))
        return b

    def is_empty(self) -> bool:
        return not self._ops


def causal_ancestors(ops) -> dict:
    """op_id → 전이적 인과 조상 집합(부모는 항상 앞선 연산 — validate가 보장)."""
    anc = {}
    for op in ops:
        a = set()
        for par in op.causal_parent_op_ids:
            a.add(par)
            a |= anc.get(par, set())
        anc[op.op_id] = a
    return anc


def causal_layers(ops) -> dict:
    """op_id → 인과 계층(부모 없음 0, 그 외 1 + 부모 계층 최대). 삽입 순서와 무관한 의미."""
    layer = {}
    for op in ops:
        layer[op.op_id] = (1 + max(layer[par] for par in op.causal_parent_op_ids)
                           if op.causal_parent_op_ids else 0)
    return layer


def player_turn_batch(session, transaction_id: str) -> WorldMutationBatch:
    """PLAYER_TURN 준비가 소유하는 (H02에서는 항상 빈) 묶음. id는 tx에서 결정적."""
    return WorldMutationBatch(
        batch_id="wmb_" + stable_digest("world_mutation.batch.player_turn.v1",
                                        str(session.session_id), str(transaction_id)),
        outer_origin=_ws.CAUSE_PLAYER_TURN,
        source_world_version_id=_ws.version_id_of(getattr(session, "world_version", None)),
        definition_revision=getattr(session, "definition_revision", None),
        transaction_id=str(transaction_id))


# ══════════════════════════════════════════════════════════════
#  연산 handler 레지스트리
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class WorldWrite:
    """handler가 만드는 내부 쓰기 기술자. 레지스트리 밖에서 만들어 적용할 경로는 없다."""
    domain: str
    path: tuple
    semantics: str
    value: object
    op_id: str


class OperationRegistry:
    """op_type → handler(view, op, payload) -> [WorldWrite] 의 명시적 등록부.

    handler는 typed 연산의 의미를 아는 도메인 코드(H03 이후)만 등록한다.
    production 레지스트리는 H02에서 비어 있다.
    """

    def __init__(self, name: str):
        self.name = name
        self._handlers: dict = {}

    def register(self, op_type: str, handler) -> None:
        if not isinstance(op_type, str) or not op_type:
            raise WorldMutationError("op_type이 필요합니다")
        if op_type in self._handlers:
            raise WorldMutationError(f"이미 등록된 op_type: {op_type}")
        if not callable(handler):
            raise WorldMutationError("handler는 호출 가능해야 합니다")
        self._handlers[op_type] = handler

    def handler_for(self, op_type: str):
        return self._handlers.get(op_type)

    def op_types(self) -> tuple:
        return tuple(sorted(self._handlers))


# production 레지스트리 — H03부터 시간·예약·공간·접근 typed handler가 등록된다(파일 끝).
#   등록 ≠ 사용: H03에서 이 연산을 만드는 production 플레이어 경로(producer)는 없다.
PRODUCTION_REGISTRY = OperationRegistry("production")


def write(op: WorldMutationOp, domain: str, path, semantics: str, value) -> WorldWrite:
    """handler 전용 쓰기 기술자 생성(도메인·의미 검증)."""
    if domain not in WRITABLE_DOMAINS:
        raise WorldMutationError(f"{op.op_id}: 쓸 수 없는 도메인 {domain!r}")
    if semantics not in MERGE_SEMANTICS:
        raise WorldMutationError(f"{op.op_id}: 알 수 없는 병합 의미 {semantics!r}")
    path = tuple(path)
    if not path or not all(isinstance(p, str) and p for p in path):
        raise WorldMutationError(f"{op.op_id}: 경로는 비어 있지 않은 문자열 키의 나열이어야 합니다")
    if semantics == MERGE_ADDITIVE and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise WorldMutationError(f"{op.op_id}: ADDITIVE 값은 수치여야 합니다")
    if semantics == MERGE_DELETE and value is not None:
        raise WorldMutationError(f"{op.op_id}: DELETE 값은 None이어야 합니다")
    if semantics == MERGE_SET_LIKE and not (isinstance(value, dict) and set(value) <= {"add", "remove"}):
        raise WorldMutationError(f"{op.op_id}: SET_LIKE 값은 {{add:[...], remove:[...]}} 이어야 합니다")
    return WorldWrite(domain, path, semantics, _canonical_copy(value, f"{op.op_id}.write"), op.op_id)


# ══════════════════════════════════════════════════════════════
#  ProjectedWorld · 투영
# ══════════════════════════════════════════════════════════════

class ProjectedWorld:
    """게임 정본 도메인만 캡처한 투영 전용 사본. 세션 전체 sandbox가 아니다."""

    def __init__(self, domains: dict):
        self._d = {k: copy.deepcopy(domains.get(k, copy.deepcopy(_ws.GAMEPLAY_WORLD_FIELDS[k])))
                   for k in PROJECTED_DOMAINS}

    @classmethod
    def capture(cls, session) -> "ProjectedWorld":
        return cls({k: getattr(session, k, None) if hasattr(session, k)
                    else copy.deepcopy(_ws.GAMEPLAY_WORLD_FIELDS[k]) for k in PROJECTED_DOMAINS})

    def get(self, domain: str, path=()):
        cur = self._d[domain]
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return copy.deepcopy(cur)

    def has(self, domain: str, path=()) -> bool:
        cur = self._d[domain]
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return False
            cur = cur[p]
        return True

    def domain(self, name: str):
        return copy.deepcopy(self._d[name])

    def to_dict(self) -> dict:
        return copy.deepcopy(self._d)

    def digest(self) -> str:
        return content_digest(self._d)

    def _set(self, domain, path, value):
        cur = self._d[domain]
        if not isinstance(cur, dict):
            raise WorldProjectionConflict([Conflict(
                "DOMAIN_NOT_MAPPING", domain, path, (), f"{domain}가 객체가 아니어서 쓸 수 없습니다")])
        for p in path[:-1]:
            nxt = cur.get(p)
            if nxt is None:
                nxt = cur[p] = {}
            if not isinstance(nxt, dict):
                raise WorldProjectionConflict([Conflict(
                    "PATH_NOT_MAPPING", domain, path, (), f"{domain}.{'.'.join(path)} 중간이 객체가 아닙니다")])
            cur = nxt
        if value is _DELETED:
            cur.pop(path[-1], None)
        else:
            cur[path[-1]] = value


@dataclass(frozen=True)
class Conflict:
    code: str
    domain: str
    path: tuple
    op_ids: tuple
    message: str

    def to_dict(self) -> dict:
        return {"failure_class": WORLD_PROJECTION_CONFLICT, "code": self.code,
                "domain": self.domain, "path": list(self.path),
                "op_ids": list(self.op_ids), "message": self.message}


@dataclass(frozen=True)
class ProjectionResult:
    source_digest: str
    world: ProjectedWorld | None
    conflicts: tuple
    batch_digest: str

    @property
    def ok(self) -> bool:
        return not self.conflicts

    @property
    def changed(self) -> bool:
        return self.world is not None and self.world.digest() != self.source_digest

    def changed_domains(self, source: ProjectedWorld) -> tuple:
        if self.world is None:
            return ()
        return tuple(k for k in WRITABLE_DOMAINS
                     if content_digest(self.world.domain(k)) != content_digest(source.domain(k)))


def _canon_order(writes: list) -> list:
    """같은 계층 쓰기의 삽입 순서를 의미에서 지운다 — 값의 정규 직렬화 순으로 정렬."""
    return sorted(writes, key=lambda w: (canonical_json(w.value), w.op_id))


def _merge(writes: list, base):
    """같은 계층·같은 (domain, path)의 쓰기들을 병합한다. 반환: (값, 충돌 메시지|None).

    결과는 쓰기 삽입 순서와 무관하다(정규 순서로 처리).
    """
    sems = {w.semantics for w in writes}
    if len(sems) > 1:
        return None, f"병합 의미 혼용 {sorted(sems)}"
    sem = sems.pop()
    writes = _canon_order(writes)
    if sem == MERGE_DELETE:
        return _DELETED, None
    if sem == MERGE_SINGLE_VALUE:
        vals = {canonical_json(w.value) for w in writes}
        if len(vals) > 1:
            return None, f"단일값 칸에 서로 다른 값 {len(vals)}개(마지막 쓰기 채택 금지)"
        return copy.deepcopy(writes[0].value), None
    if sem == MERGE_ADDITIVE:
        if base is not None and (not isinstance(base, (int, float)) or isinstance(base, bool)):
            return None, "ADDITIVE 대상의 기존 값이 수치가 아닙니다"
        return (base or 0) + sum(w.value for w in writes), None
    if sem == MERGE_SET_LIKE:
        adds, removes = [], set()
        for w in writes:
            adds += [canonical_json(x) for x in w.value.get("add", [])]
            removes |= {canonical_json(x) for x in w.value.get("remove", [])}
        if set(adds) & removes:
            return None, "같은 원소의 추가와 제거가 동시에 있습니다"
        cur = list(base or []) if isinstance(base, list) or base is None else None
        if cur is None:
            return None, "SET_LIKE 대상의 기존 값이 목록이 아닙니다"
        out = [x for x in cur if canonical_json(x) not in removes]
        have = {canonical_json(x) for x in out}
        for k in sorted(set(adds)):
            if k not in have:
                out.append(json.loads(k))
                have.add(k)
        return out, None
    # APPEND_DEDUPE — 같은 계층의 독립 추가는 정규 순서로 덧붙인다
    cur = list(base or []) if isinstance(base, list) or base is None else None
    if cur is None:
        return None, "APPEND_DEDUPE 대상의 기존 값이 목록이 아닙니다"
    have = {canonical_json(x) for x in cur}
    for w in writes:
        k = canonical_json(w.value)
        if k not in have:
            cur.append(copy.deepcopy(w.value))
            have.add(k)
    return cur, None


def _overlaps(p1: tuple, p2: tuple) -> bool:
    return p1[:len(p2)] == p2 or p2[:len(p1)] == p1


def _cross_layer_compatible(w1: WorldWrite, w2: WorldWrite) -> bool:
    """인과 관계없는 두 쓰기가 다른 계층에서 같은 칸을 써도 순서와 무관한 결과인가."""
    if w1.domain != w2.domain or w1.path != w2.path or w1.semantics != w2.semantics:
        return False
    if w1.semantics == MERGE_ADDITIVE:
        return True
    if w1.semantics == MERGE_SINGLE_VALUE:
        return canonical_json(w1.value) == canonical_json(w2.value)
    if w1.semantics == MERGE_SET_LIKE:
        a = {canonical_json(x) for x in w1.value.get("add", []) + w2.value.get("add", [])}
        r = {canonical_json(x) for x in w1.value.get("remove", []) + w2.value.get("remove", [])}
        return not (a & r)
    return False      # APPEND_DEDUPE: 독립 추가의 계층 순서가 결과 순서가 되므로 금지


def project(source, batch: WorldMutationBatch, registry: OperationRegistry) -> ProjectionResult:
    """동결된 묶음을 원본 투영 사본 위에 인과 계층 순서로 적용한다. 원본은 바뀌지 않는다.

    [인과 순서 — causal_parent_op_ids]
      · 계층 0 = 부모 없는 연산, 계층 k = 1 + 부모 계층 최대.
      · 같은 계층의 연산은 모두 같은 계층 기준 투영(이전 계층까지 적용된 상태)을 읽고,
        그 쓰기들은 선언된 병합 의미로 합쳐진다(충돌 판정 포함). 삽입 순서는 의미가 없다.
      · 다음 계층은 이전 계층의 결과를 읽는다 — 자식은 부모의 결과를 본다.
      · 인과 관계없는(조상이 아닌) 두 연산이 다른 계층에서 같은 칸(또는 겹치는 경로)을
        쓰면, 순서와 무관한 결과일 때(가산·동일 단일값·모순 없는 집합)만 허용하고 나머지는
        WORLD_PROJECTION_CONFLICT(UNORDERED_INDEPENDENT_WRITES)다.

    source: TRPGSession 또는 ProjectedWorld. 충돌은 예외가 아니라 결과의 conflicts로 돌려준다.
    """
    if not batch.frozen:
        raise WorldMutationError("동결된 묶음만 투영할 수 있습니다")
    if not batch.digest_matches():
        raise WorldMutationError(f"{batch.batch_id}: 동결 digest와 내용이 다릅니다(변조)")
    base = source if isinstance(source, ProjectedWorld) else ProjectedWorld.capture(source)
    src_digest = base.digest()
    world = ProjectedWorld(base.to_dict())
    ops = batch.operations
    layers = causal_layers(ops)
    anc = causal_ancestors(ops)
    conflicts, record = [], []          # record: (layer, WorldWrite)
    for L in sorted(set(layers.values())):
        layer_base = ProjectedWorld(world.to_dict())
        grouped = {}
        for op in (o for o in ops if layers[o.op_id] == L):
            handler = registry.handler_for(op.op_type)
            if handler is None:
                conflicts.append(Conflict("UNKNOWN_OPERATION", "", (), (op.op_id,),
                                          f"{op.op_id}: 등록되지 않은 op_type {op.op_type!r}"))
                continue
            if op.expected is not None:
                exp = op.expected
                if canonical_json(layer_base.get(exp["domain"], tuple(exp["path"]))) \
                        != canonical_json(exp.get("value")):
                    conflicts.append(Conflict("EXPECTATION_FAILED", exp["domain"], tuple(exp["path"]),
                                              (op.op_id,), f"{op.op_id}: 기대값과 투영 상태가 다릅니다"))
                    continue
            view = ProjectedWorld(layer_base.to_dict())     # handler가 view를 고쳐도 무해
            try:
                writes = list(handler(view, op, batch.resolved_payload(op)) or ())
            except WorldMutationError as e:
                conflicts.append(Conflict("HANDLER_REJECTED", "", (), (op.op_id,), str(e)))
                continue
            for w in writes:
                if not isinstance(w, WorldWrite) or w.op_id != op.op_id:
                    conflicts.append(Conflict("FOREIGN_WRITE", "", (), (op.op_id,),
                                              f"{op.op_id}: handler가 자기 연산의 쓰기만 낼 수 있습니다"))
                    continue
                grouped.setdefault((w.domain, w.path), []).append(w)
                record.append((L, w))
        if conflicts:
            break
        keys = sorted(grouped)
        for i, (d1, p1) in enumerate(keys):
            for d2, p2 in keys[i + 1:]:
                if d1 == d2 and _overlaps(p1, p2):
                    ops_ = tuple(w.op_id for w in grouped[(d1, p1)] + grouped[(d2, p2)])
                    conflicts.append(Conflict("PATH_OVERLAP", d1, p2, ops_,
                                              f"{d1}: {'.'.join(p1)} 와 {'.'.join(p2)}를 같은 계층에서 씁니다"))
        if conflicts:
            break
        for key in keys:
            ws = grouped[key]
            value, err = _merge(ws, layer_base.get(key[0], key[1]))
            if value is _DELETED and not layer_base.has(key[0], key[1]):
                value, err = None, "없는 키를 삭제하려 합니다"
            if err:
                conflicts.append(Conflict("MERGE_CONFLICT", key[0], key[1],
                                          tuple(sorted({w.op_id for w in ws})),
                                          f"{key[0]}.{'.'.join(key[1])}: {err}"))
                continue
            try:
                world._set(key[0], key[1], value)
            except WorldProjectionConflict as e:
                conflicts.extend(e.conflicts)
        if conflicts:
            break
    if not conflicts:
        for i, (l1, w1) in enumerate(record):
            for l2, w2 in record[i + 1:]:
                if l1 == l2 or w1.domain != w2.domain or not _overlaps(w1.path, w2.path):
                    continue
                first, second = (w1, w2) if l1 < l2 else (w2, w1)
                if first.op_id in anc.get(second.op_id, ()):
                    continue                                  # 인과 순서가 있는 쓰기
                if not _cross_layer_compatible(first, second):
                    conflicts.append(Conflict(
                        "UNORDERED_INDEPENDENT_WRITES", second.domain, second.path,
                        (first.op_id, second.op_id),
                        f"{second.domain}.{'.'.join(second.path)}: 인과 관계없는 연산이 다른 계층에서 "
                        f"같은 칸을 순서 의존적으로 씁니다"))
    if conflicts:
        return ProjectionResult(src_digest, None, tuple(conflicts), batch.digest)
    return ProjectionResult(src_digest, world, (), batch.digest)


def apply_projection(session, result: ProjectionResult, source: ProjectedWorld) -> tuple:
    """(owner 원자 경계 안 전용) 투영 결과의 바뀐 도메인만 세션에 반영한다.

    world_version 은 쓰지 않는다(owner가 대상 version을 따로 설정). 반환: 바뀐 도메인.
    충돌 결과는 적용할 수 없다.
    """
    if not result.ok or result.world is None:
        raise WorldProjectionConflict(result.conflicts)
    changed = result.changed_domains(source)
    for k in changed:
        setattr(session, k, result.world.domain(k))
    return changed


# ── H03: production typed handler 등록(도메인 모듈이 world_mutation을 import하므로 맨 끝) ──
def _register_production_handlers() -> None:
    from . import world_schedule as _sched
    _sched.register(PRODUCTION_REGISTRY)


_register_production_handlers()
