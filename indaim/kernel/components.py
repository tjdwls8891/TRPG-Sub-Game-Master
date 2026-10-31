"""컴포넌트 등록 — 한 컴포넌트의 주인은 기능 모듈 하나, 쓰기는 그 컴포넌트의 효과 연산으로만.

연산 함수: op(view, effect) -> [Write]  — view는 읽기 전용(같은 계층 기준 투영), 쓰기는 자기 대상 컴포넌트 안만.
"""
import copy
from dataclasses import dataclass, field

from .canon import CanonicalError, _check, canonical_json
from .errors import KernelError
from .world import ENTITY_KINDS, WORLD, _MISSING, address

SINGLE_VALUE, SET_LIKE, ADDITIVE, APPEND_DEDUPE, DELETE = (
    "SINGLE_VALUE", "SET_LIKE", "ADDITIVE", "APPEND_DEDUPE", "DELETE")
SEMANTICS = (SINGLE_VALUE, SET_LIKE, ADDITIVE, APPEND_DEDUPE, DELETE)


@dataclass(frozen=True)
class Component:
    name: str
    entity: str                       # ENTITY_KINDS 또는 WORLD
    owner: str                        # 주인 모듈 이름(진단·계층 검사용)
    ops: dict = field(default_factory=dict)
    default: object = None            # 정의 기본값: callable(defn, eid) 또는 정적 값(None = {})
    rewind: bool = True               # 되감기 대상(게임 정본)


@dataclass(frozen=True)
class Write:
    address: tuple
    path: tuple
    semantics: str
    value: object
    effect_id: str


def write(effect, path, semantics: str, value) -> Write:
    """연산 함수 전용 쓰기 기술자 — 대상은 언제나 그 효과의 컴포넌트다."""
    if semantics not in SEMANTICS:
        raise KernelError(f"{effect.id}: 알 수 없는 병합 의미 {semantics!r}")
    path = tuple(path)
    if not all(isinstance(p, str) and p for p in path):
        raise KernelError(f"{effect.id}: 경로는 비어 있지 않은 문자열 키의 나열이어야 합니다")
    if semantics == ADDITIVE and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise KernelError(f"{effect.id}: ADDITIVE 값은 수치여야 합니다")
    if semantics == DELETE and value is not None:
        raise KernelError(f"{effect.id}: DELETE 값은 None이어야 합니다")      # 빈 경로 = 저장값을 지워 기본값으로
    if semantics == SET_LIKE and not (isinstance(value, dict) and set(value) <= {"add", "remove"}):
        raise KernelError(f"{effect.id}: SET_LIKE 값은 {{add:[...], remove:[...]}} 이어야 합니다")
    try:
        _check(value)
    except CanonicalError as e:
        raise KernelError(f"{effect.id}: 쓰기 값이 정규 JSON이 아닙니다 — {e}") from e
    return Write(effect.address, path, semantics, copy.deepcopy(value), effect.id)


class Registry:
    def __init__(self, name: str = "kernel"):
        self.name = name
        self._c: dict = {}
        self._frozen = False

    def register(self, comp: Component) -> None:
        if self._frozen:
            raise KernelError(f"{self.name}: 동결된 등록부입니다")
        if comp.entity not in ENTITY_KINDS + (WORLD,):
            raise KernelError(f"{comp.name}: 알 수 없는 엔티티 종류 {comp.entity!r}")
        key = (comp.entity, comp.name)
        if key in self._c:
            raise KernelError(f"이미 등록된 컴포넌트 {comp.entity}.{comp.name}"
                              f"(주인 {self._c[key].owner} · 새 {comp.owner})")
        for op, fn in comp.ops.items():
            if not isinstance(op, str) or not op or not callable(fn):
                raise KernelError(f"{comp.entity}.{comp.name}: 잘못된 연산 {op!r}")
        if not callable(comp.default) and comp.default is not None:
            try:
                _check(comp.default)
            except CanonicalError as e:
                raise KernelError(f"{comp.entity}.{comp.name}: 기본값이 정규 JSON이 아닙니다 — {e}") from e
        self._c[key] = comp

    def freeze(self) -> "Registry":
        self._frozen = True
        return self

    def component(self, entity: str, name: str) -> Component:
        c = self._c.get((entity, name))
        if c is None:
            raise KernelError(f"등록되지 않은 컴포넌트 {entity}.{name}")
        return c

    def has(self, entity: str, name: str) -> bool:
        return (entity, name) in self._c

    def components(self) -> tuple:
        return tuple(self._c[k] for k in sorted(self._c))

    def default(self, defn, addr):
        c = self.component(addr[0], addr[2])
        v = c.default(defn, addr[1]) if callable(c.default) else c.default
        return {} if v is None else copy.deepcopy(v)

    def check_world(self, world) -> None:
        """저장된 모든 컴포넌트가 등록돼 있는가(손상·버전 차이 조기 표면화)."""
        bad = [a for a in world.stored_addresses() if not self.has(a[0], a[2])]
        if bad:
            raise KernelError(f"등록되지 않은 컴포넌트 저장값 {bad[:5]}")


class View:
    """읽기 전용 실효 세계 = 저장 변경분 ∪ 정의 기본값. 반환은 언제나 깊은 사본."""

    def __init__(self, world, registry: Registry, defn):
        self._w, self.registry, self.defn = world, registry, defn

    @property
    def version_id(self):
        return (self._w.version or {}).get("version_id")

    def stored_has(self, kind: str, eid, comp: str) -> bool:
        """기본값이 아닌 저장값이 있는가(빈 경로 DELETE의 '있는 것만 지운다' 판정)."""
        return self._w.stored(address(kind, eid, comp)) is not _MISSING

    def _effective(self, addr):
        v = self._w.stored(addr)
        return self.registry.default(self.defn, addr) if v is _MISSING else v

    def read(self, kind: str, eid, comp: str, path=()):
        cur = self._effective(address(kind, eid, comp))
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return copy.deepcopy(cur)

    def has(self, kind: str, eid, comp: str, path=()) -> bool:
        cur = self._effective(address(kind, eid, comp))
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return False
            cur = cur[p]
        return True

    def same_as_default(self, addr) -> bool:
        v = self._w.stored(addr)
        return v is _MISSING or canonical_json(v) == canonical_json(self.registry.default(self.defn, addr))
