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
MERGE_SEMANTICS = (MERGE_SINGLE_VALUE, MERGE_SET_LIKE, MERGE_ADDITIVE, MERGE_APPEND_DEDUPE)

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
    """한 인과 단위의 정본 변이 묶음. 동결 후에는 바꿀 수 없다."""

    def __init__(self, *, batch_id: str, outer_origin: str, source_world_version_id,
                 definition_revision, transaction_id: str | None = None):
        if outer_origin not in _ws.CAUSE_KINDS:
            raise WorldMutationError(f"알 수 없는 outer_origin: {outer_origin!r}")
        if not isinstance(batch_id, str) or not batch_id:
            raise WorldMutationError("batch_id가 필요합니다")
        self.batch_id = batch_id
        self.outer_origin = outer_origin
        self.transaction_id = transaction_id
        self.source_world_version_id = source_world_version_id
        self.definition_revision = definition_revision
        self._ops: list = []
        self.diagnostics: list = []
        self._frozen = False
        self._digest = None
        self._resolved_locals: dict = {}

    # ── 구성 ──
    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def operations(self) -> tuple:
        return tuple(self._ops)

    def add(self, op: WorldMutationOp) -> None:
        if self._frozen:
            raise BatchFrozenError(f"동결된 묶음 {self.batch_id}에 연산을 더할 수 없습니다")
        if not isinstance(op, WorldMutationOp):
            raise WorldMutationError("WorldMutationOp만 더할 수 있습니다")
        self._ops.append(op)

    # ── 동결 ──
    def validate(self) -> list:
        """구조 규칙 위반 목록(비면 정상). 연산 의미는 handler 몫이라 보지 않는다."""
        errs, seen, defined = [], set(), {}
        for i, op in enumerate(self._ops):
            if op.op_id in seen:
                errs.append(f"OP_ID_DUPLICATE:{op.op_id}")
            for p in op.causal_parent_op_ids:
                if p not in seen:
                    errs.append(f"CAUSAL_PARENT_NOT_EARLIER:{op.op_id}->{p}")
            seen.add(op.op_id)
            for name in op.defines_locals:
                if name in defined:
                    errs.append(f"LOCAL_REF_REDEFINED:{name}")
                defined[name] = op.op_id
        for op in self._ops:
            refs = []
            _collect_local_refs(op.payload, refs)
            for name in refs:
                if name not in defined:
                    errs.append(f"LOCAL_REF_UNDEFINED:{op.op_id}->{name}")
                elif defined[name] != op.op_id and defined[name] not in op.causal_parent_op_ids:
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
            for op in self._ops for name in op.defines_locals}
        self._frozen = True
        self._digest = content_digest(self.to_dict())
        return self._digest

    @property
    def digest(self) -> str | None:
        return self._digest

    def resolved_payload(self, op: WorldMutationOp) -> dict:
        if not self._frozen:
            raise WorldMutationError("동결 전에는 LocalRef를 해석하지 않습니다")
        return _resolve_local_refs(op.payload, self._resolved_locals)

    def to_dict(self) -> dict:
        return {"batch_schema_version": BATCH_SCHEMA_VERSION, "batch_id": self.batch_id,
                "outer_origin": self.outer_origin, "transaction_id": self.transaction_id,
                "source_world_version_id": self.source_world_version_id,
                "definition_revision": self.definition_revision,
                "operations": [op.to_dict() for op in self._ops],
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


# H02 production 레지스트리 — 비어 있다. 실제 도메인 연산은 H03 이후 typed handler로 등록.
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


def _merge(writes: list, base):
    """같은 (domain, path)의 쓰기들을 병합한다. 반환: (값, 충돌 메시지|None)."""
    sems = {w.semantics for w in writes}
    if len(sems) > 1:
        return None, f"병합 의미 혼용 {sorted(sems)}"
    sem = sems.pop()
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
        both = set(adds) & removes
        if both:
            return None, "같은 원소의 추가와 제거가 동시에 있습니다"
        cur = list(base or []) if isinstance(base, list) or base is None else None
        if cur is None:
            return None, "SET_LIKE 대상의 기존 값이 목록이 아닙니다"
        keys = [canonical_json(x) for x in cur]
        out = [x for x, k in zip(cur, keys) if k not in removes]
        have = {canonical_json(x) for x in out}
        for w in writes:
            for x in w.value.get("add", []):
                k = canonical_json(x)
                if k not in have:
                    out.append(copy.deepcopy(x))
                    have.add(k)
        return out, None
    # APPEND_DEDUPE
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


def project(source, batch: WorldMutationBatch, registry: OperationRegistry) -> ProjectionResult:
    """동결된 묶음을 원본 투영 사본 위에 적용한다. 원본(세션·ProjectedWorld)은 바뀌지 않는다.

    source: TRPGSession 또는 ProjectedWorld. 충돌은 예외가 아니라 결과의 conflicts로 돌려준다.
    """
    if not batch.frozen:
        raise WorldMutationError("동결된 묶음만 투영할 수 있습니다")
    base = source if isinstance(source, ProjectedWorld) else ProjectedWorld.capture(source)
    src_digest = base.digest()
    world = ProjectedWorld(base.to_dict())
    conflicts, grouped, order = [], {}, []
    for op in batch.operations:
        handler = registry.handler_for(op.op_type)
        if handler is None:
            conflicts.append(Conflict("UNKNOWN_OPERATION", "", (), (op.op_id,),
                                      f"{op.op_id}: 등록되지 않은 op_type {op.op_type!r}"))
            continue
        if op.expected is not None:
            exp = op.expected
            if canonical_json(base.get(exp["domain"], tuple(exp["path"]))) != canonical_json(exp.get("value")):
                conflicts.append(Conflict("EXPECTATION_FAILED", exp["domain"], tuple(exp["path"]),
                                          (op.op_id,), f"{op.op_id}: 기대값과 원본이 다릅니다"))
                continue
        try:
            writes = list(handler(base, op, batch.resolved_payload(op)) or ())
        except WorldMutationError as e:
            conflicts.append(Conflict("HANDLER_REJECTED", "", (), (op.op_id,), str(e)))
            continue
        for w in writes:
            if not isinstance(w, WorldWrite) or w.op_id != op.op_id:
                conflicts.append(Conflict("FOREIGN_WRITE", "", (), (op.op_id,),
                                          f"{op.op_id}: handler가 자기 연산의 쓰기만 낼 수 있습니다"))
                continue
            key = (w.domain, w.path)
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(w)
    # 경로 접두 겹침(상위 칸과 하위 칸을 동시에 씀)은 순서 의존이므로 충돌로 본다
    keys = sorted(order)
    for i, (d1, p1) in enumerate(keys):
        for d2, p2 in keys[i + 1:]:
            if d1 == d2 and p2[:len(p1)] == p1:
                ops = tuple(w.op_id for w in grouped[(d1, p1)] + grouped[(d2, p2)])
                conflicts.append(Conflict("PATH_OVERLAP", d1, p2, ops,
                                          f"{d1}: {'.'.join(p1)} 와 {'.'.join(p2)}를 동시에 씁니다"))
    if not conflicts:
        for key in order:
            ws = grouped[key]
            value, err = _merge(ws, base.get(key[0], key[1]))
            if err:
                conflicts.append(Conflict("MERGE_CONFLICT", key[0], key[1],
                                          tuple(w.op_id for w in ws), f"{key[0]}.{'.'.join(key[1])}: {err}"))
                continue
            try:
                world._set(key[0], key[1], value)
            except WorldProjectionConflict as e:
                conflicts.extend(e.conflicts)
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
