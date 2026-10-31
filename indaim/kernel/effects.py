"""효과(Effect) — 세계를 바꾸는 유일한 언어. 묶음 → 동결(검증·digest) → 투영(원본 불변) → 커밋 owner 적용.

옛 `core.world_mutation`(WorldMutationBatch·인과 계층 투영·병합 5종·충돌 판정)을 대상만
'정본 도메인 11개'에서 '엔티티·컴포넌트'로 넓혀 옮겼다. 의미는 같다:
  · 계층 0 = 부모 없는 효과, 계층 k = 1 + 부모 계층 최대. 같은 계층은 같은 기준을 읽고 병합 의미로 합친다.
  · 같은 단일값 칸에 서로 다른 값 = 충돌(마지막 쓰기 채택 금지). 같은 계층의 겹치는 경로 = 충돌.
  · 인과 관계없는 효과가 다른 계층에서 같은 칸을 순서 의존적으로 쓰면 충돌.
  · 충돌은 결정적 실패(PROJECTION_CONFLICT) — provider 재시도 대상이 아니다.
옛 LocalRef·expected는 production 생산자가 없어 옮기지 않았다(생산자가 결정적 id를 직접 만든다).
"""
import copy
import json
from dataclasses import dataclass, field

from .canon import CanonicalError, _check, canonical_json, content_digest
from .components import ADDITIVE, APPEND_DEDUPE, DELETE, SET_LIKE, SINGLE_VALUE, View, Write
from .errors import KernelError
from .version import CAUSE_KINDS
from .world import _MISSING, World, address

PROJECTION_CONFLICT = "WORLD_PROJECTION_CONFLICT"
EFFECT_CAUSES = ("PLAN", "SETTLE", "EVENT", "GENESIS", "ADMIN")
BATCH_SCHEMA = 1
_DELETED = object()


@dataclass(frozen=True)
class Effect:
    id: str
    address: tuple                    # (kind, id, component) — 세계 단위는 ("world", "", component)
    op: str
    args: dict = field(default_factory=dict)
    cause: dict = field(default_factory=dict)     # {kind, by, ref?}
    parents: tuple = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "target": list(self.address), "op": self.op,
                "args": copy.deepcopy(self.args), "cause": copy.deepcopy(self.cause),
                "parents": list(self.parents)}

    @classmethod
    def from_dict(cls, d: dict) -> "Effect":
        kind, eid, comp = d["target"]
        c = d.get("cause") or {}
        return make_effect(d["id"], kind, eid, comp, d["op"], d.get("args") or {},
                           cause=c.get("kind"), by=c.get("by"), ref=c.get("ref"), parents=d.get("parents") or ())


def make_effect(effect_id, kind, eid, component, op, args=None, *, cause, by, ref=None, parents=()) -> Effect:
    if not isinstance(effect_id, str) or not effect_id:
        raise KernelError("효과 id가 필요합니다")
    if not isinstance(op, str) or not op:
        raise KernelError(f"{effect_id}: 연산 이름이 필요합니다")
    if cause not in EFFECT_CAUSES:
        raise KernelError(f"{effect_id}: 알 수 없는 원인 {cause!r}")
    if not isinstance(by, str) or not by:
        raise KernelError(f"{effect_id}: 생산자(by)가 필요합니다")
    try:
        _check(args or {})
        _check(ref)
    except CanonicalError as e:
        raise KernelError(f"{effect_id}: 인자가 정규 JSON이 아닙니다 — {e}") from e
    cause_d = {"kind": cause, "by": by}
    if ref is not None:
        cause_d["ref"] = copy.deepcopy(ref)
    # 인자는 정규 JSON 형태로 보관한다(튜플 → 목록) — 기록·재생 왕복이 같은 값이 되게
    return Effect(effect_id, address(kind, eid, component), op, json.loads(canonical_json(args or {})), cause_d,
                  tuple(str(p) for p in parents))


class EffectBatch:
    """한 인과 단위(턴 단계·genesis·관리 편집)의 효과 묶음. 동결 뒤에는 바꿀 수 없다.

    받은 효과는 정규 dict 사본으로 보관한다 — 호출자가 원래 객체를 고쳐도 묶음은 그대로다.
    """

    def __init__(self, *, batch_id: str, origin: str, base_version_id, definition_revision,
                 transaction_id: str | None = None):
        if origin not in CAUSE_KINDS:
            raise KernelError(f"알 수 없는 origin {origin!r}")
        if not isinstance(batch_id, str) or not batch_id:
            raise KernelError("batch_id가 필요합니다")
        self._meta = {"batch_id": batch_id, "origin": origin, "transaction_id": transaction_id,
                      "base_version_id": base_version_id, "definition_revision": definition_revision}
        _check(self._meta)
        self._effects: list = []
        self._frozen = False
        self._digest = None

    batch_id = property(lambda self: self._meta["batch_id"])
    origin = property(lambda self: self._meta["origin"])
    base_version_id = property(lambda self: self._meta["base_version_id"])
    definition_revision = property(lambda self: self._meta["definition_revision"])
    transaction_id = property(lambda self: self._meta["transaction_id"])
    frozen = property(lambda self: self._frozen)
    digest = property(lambda self: self._digest)

    @property
    def effects(self) -> tuple:
        return tuple(Effect.from_dict(d) for d in self._effects)

    def add(self, effect: Effect) -> None:
        if self._frozen:
            raise KernelError(f"동결된 묶음 {self.batch_id}에 효과를 더할 수 없습니다")
        if not isinstance(effect, Effect):
            raise KernelError("Effect만 더할 수 있습니다")
        self._effects.append(Effect.from_dict(effect.to_dict()).to_dict())

    def extend(self, effects) -> None:
        for e in effects:
            self.add(e)

    def validate(self) -> list:
        errs, seen = [], set()
        for e in self.effects:
            if e.id in seen:
                errs.append(f"EFFECT_ID_DUPLICATE:{e.id}")
            errs += [f"PARENT_NOT_EARLIER:{e.id}->{p}" for p in e.parents if p not in seen]
            seen.add(e.id)
        return errs

    def freeze(self) -> str:
        if self._frozen:
            return self._digest
        errs = self.validate()
        if errs:
            raise KernelError(f"{self.batch_id}: 묶음 구조 오류 — " + ", ".join(errs))
        self._frozen = True
        self._digest = content_digest(self.to_dict())
        return self._digest

    def digest_matches(self) -> bool:
        return self._frozen and content_digest(self.to_dict()) == self._digest

    def to_dict(self) -> dict:
        return {"schema": BATCH_SCHEMA, **copy.deepcopy(self._meta), "effects": copy.deepcopy(self._effects)}

    @classmethod
    def from_dict(cls, d: dict) -> "EffectBatch":
        b = cls(batch_id=d["batch_id"], origin=d["origin"], base_version_id=d.get("base_version_id"),
                definition_revision=d.get("definition_revision"), transaction_id=d.get("transaction_id"))
        for ed in d.get("effects") or ():
            b.add(Effect.from_dict(ed))
        return b

    def is_empty(self) -> bool:
        return not self._effects


def _ancestors(effects) -> dict:
    anc = {}
    for e in effects:
        a = set()
        for p in e.parents:
            a |= {p} | anc.get(p, set())
        anc[e.id] = a
    return anc


def _layers(effects) -> dict:
    layer = {}
    for e in effects:
        layer[e.id] = 1 + max(layer[p] for p in e.parents) if e.parents else 0
    return layer


@dataclass(frozen=True)
class Conflict:
    code: str
    address: tuple
    path: tuple
    effect_ids: tuple
    message: str

    def to_dict(self) -> dict:
        return {"failure_class": PROJECTION_CONFLICT, "code": self.code, "target": list(self.address),
                "path": list(self.path), "effect_ids": list(self.effect_ids), "message": self.message}


@dataclass(frozen=True)
class Projection:
    base_digest: str
    world: World | None
    conflicts: tuple
    batch_digest: str

    @property
    def ok(self) -> bool:
        return not self.conflicts

    @property
    def changed(self) -> bool:
        return self.world is not None and self.world.digest() != self.base_digest


def _merge(writes, base):
    sems = {w.semantics for w in writes}
    if len(sems) > 1:
        return None, f"병합 의미 혼용 {sorted(sems)}"
    sem = sems.pop()
    writes = sorted(writes, key=lambda w: (canonical_json(w.value), w.effect_id))
    if sem == DELETE:
        return _DELETED, None
    if sem == SINGLE_VALUE:
        if len({canonical_json(w.value) for w in writes}) > 1:
            return None, f"단일값 칸에 서로 다른 값 {len({canonical_json(w.value) for w in writes})}개"
        return copy.deepcopy(writes[0].value), None
    if sem == ADDITIVE:
        if base is not None and (not isinstance(base, (int, float)) or isinstance(base, bool)):
            return None, "ADDITIVE 대상의 기존 값이 수치가 아닙니다"
        return (base or 0) + sum(w.value for w in writes), None
    if base is not None and not isinstance(base, list):
        return None, f"{sem} 대상의 기존 값이 목록이 아닙니다"
    cur = list(base or [])
    if sem == SET_LIKE:
        adds = [canonical_json(x) for w in writes for x in w.value.get("add", [])]
        removes = {canonical_json(x) for w in writes for x in w.value.get("remove", [])}
        if set(adds) & removes:
            return None, "같은 원소의 추가와 제거가 동시에 있습니다"
        out = [x for x in cur if canonical_json(x) not in removes]
        have = {canonical_json(x) for x in out}
        for k in sorted(set(adds) - have):
            out.append(json.loads(k))
        return out, None
    have = {canonical_json(x) for x in cur}                        # APPEND_DEDUPE
    for w in writes:
        k = canonical_json(w.value)
        if k not in have:
            cur.append(copy.deepcopy(w.value))
            have.add(k)
    return cur, None


def _overlaps(a, b) -> bool:
    return a[:len(b)] == b or b[:len(a)] == a


def _cross_layer_ok(w1: Write, w2: Write) -> bool:
    if w1.path != w2.path or w1.semantics != w2.semantics:
        return False
    if w1.semantics == ADDITIVE:
        return True
    if w1.semantics == SINGLE_VALUE:
        return canonical_json(w1.value) == canonical_json(w2.value)
    if w1.semantics == SET_LIKE:
        a = {canonical_json(x) for x in w1.value.get("add", []) + w2.value.get("add", [])}
        r = {canonical_json(x) for x in w1.value.get("remove", []) + w2.value.get("remove", [])}
        return not (a & r)
    return False


def _set(world: World, view: View, addr, path, value):
    if not path:
        if value is _DELETED:
            raise KernelError("컴포넌트 전체 삭제는 없습니다")
        world._store(addr, value)
        return
    comp = world.stored(addr)
    comp = view.registry.default(view.defn, addr) if comp is _MISSING else comp
    if not isinstance(comp, dict):
        raise _PathError(f"{'.'.join(addr)}가 객체가 아니어서 하위 경로를 쓸 수 없습니다")
    cur = comp
    for p in path[:-1]:
        nxt = cur.get(p)
        if nxt is None:
            nxt = cur[p] = {}
        if not isinstance(nxt, dict):
            raise _PathError(f"{'.'.join(addr)}.{'.'.join(path)} 중간이 객체가 아닙니다")
        cur = nxt
    if value is _DELETED:
        cur.pop(path[-1], None)
    else:
        cur[path[-1]] = value
    world._store(addr, comp)


class _PathError(Exception):
    pass


def compact(world: World, registry, defn) -> World:
    """기본값과 같은 저장값을 지운 사본(정규 저장 형식)."""
    w = world.clone()
    view = View(w, registry, defn)
    for addr in list(w.stored_addresses()):
        if view.same_as_default(addr):
            w._drop(addr)
    return w


def project(base: World, batch: EffectBatch, registry, defn) -> Projection:
    """동결된 묶음을 base 사본 위에 인과 계층 순서로 적용한다. base는 바뀌지 않는다."""
    if not batch.frozen:
        raise KernelError("동결된 묶음만 투영할 수 있습니다")
    if not batch.digest_matches():
        raise KernelError(f"{batch.batch_id}: 동결 digest와 내용이 다릅니다(변조)")
    registry.check_world(base)
    base_digest = base.digest()
    world = base.clone()
    effects = batch.effects
    layers, anc = _layers(effects), _ancestors(effects)
    conflicts, record = [], []
    for L in sorted(set(layers.values())):
        layer_base = world.clone()
        view = View(layer_base, registry, defn)
        grouped = {}
        for e in (x for x in effects if layers[x.id] == L):
            kind, _, comp = e.address
            fn = registry.component(kind, comp).ops.get(e.op) if registry.has(kind, comp) else None
            if fn is None:
                conflicts.append(Conflict("UNKNOWN_OPERATION", e.address, (), (e.id,),
                                          f"{e.id}: 등록되지 않은 연산 {kind}.{comp}.{e.op}"))
                continue
            try:
                writes = list(fn(view, e) or ())
            except KernelError as err:
                conflicts.append(Conflict("HANDLER_REJECTED", e.address, (), (e.id,), str(err)))
                continue
            for w in writes:
                if not isinstance(w, Write) or w.effect_id != e.id or w.address != e.address:
                    conflicts.append(Conflict("FOREIGN_WRITE", e.address, (), (e.id,),
                                              f"{e.id}: 자기 대상 컴포넌트의 쓰기만 낼 수 있습니다"))
                    continue
                grouped.setdefault((w.address, w.path), []).append(w)
                record.append((L, w))
        if conflicts:
            break
        keys = sorted(grouped)
        for i, (a1, p1) in enumerate(keys):
            for a2, p2 in keys[i + 1:]:
                if a1 == a2 and _overlaps(p1, p2):
                    ids = tuple(w.effect_id for w in grouped[(a1, p1)] + grouped[(a2, p2)])
                    conflicts.append(Conflict("PATH_OVERLAP", a1, p2, ids,
                                              f"{'.'.join(a1)}: {'.'.join(p1)} 와 {'.'.join(p2)}를 같은 계층에서 씁니다"))
        if conflicts:
            break
        for key in keys:
            addr, path = key
            ws = grouped[key]
            value, err = _merge(ws, view.read(addr[0], addr[1], addr[2], path))
            if value is _DELETED and not view.has(addr[0], addr[1], addr[2], path):
                value, err = None, "없는 키를 삭제하려 합니다"
            if err:
                conflicts.append(Conflict("MERGE_CONFLICT", addr, path, tuple(sorted({w.effect_id for w in ws})),
                                          f"{'.'.join(addr)}.{'.'.join(path)}: {err}"))
                continue
            try:
                _set(world, view, addr, path, value)
            except _PathError as pe:
                conflicts.append(Conflict("PATH_NOT_MAPPING", addr, path, tuple(w.effect_id for w in ws), str(pe)))
        if conflicts:
            break
    if not conflicts:
        for i, (l1, w1) in enumerate(record):
            for l2, w2 in record[i + 1:]:
                if l1 == l2 or w1.address != w2.address or not _overlaps(w1.path, w2.path):
                    continue
                first, second = (w1, w2) if l1 < l2 else (w2, w1)
                if first.effect_id in anc.get(second.effect_id, ()):
                    continue
                if not _cross_layer_ok(first, second):
                    conflicts.append(Conflict(
                        "UNORDERED_INDEPENDENT_WRITES", second.address, second.path,
                        (first.effect_id, second.effect_id),
                        f"{'.'.join(second.address)}.{'.'.join(second.path)}: 인과 관계없는 효과가 다른 계층에서 "
                        f"같은 칸을 순서 의존적으로 씁니다"))
    if conflicts:
        return Projection(base_digest, None, tuple(conflicts), batch.digest)
    return Projection(base_digest, compact(world, registry, defn), (), batch.digest)
