"""엔티티 세계 — '정의 위의 변경분'만 저장한다.

  {"version": 버전|None,
   "entities": {kind: {id: {component: 값}}},     # kind ∈ ENTITY_KINDS
   "world": {component: 값}}                      # 엔티티에 속하지 않는 세계 단위(시계·예약·접근 …)

컴포넌트 값이 저장돼 있지 않으면 그 컴포넌트가 등록한 정의 기본값이 곧 값이다(읽기는 View).
저장 형식은 투영이 끝날 때 정리된다 — 기본값과 같은 저장값은 지운다(같은 세계 = 같은 저장 = 같은 digest).
"""
import copy

from .canon import CanonicalError, _check, content_digest
from .errors import KernelError

ENTITY_KINDS = ("actor", "place", "item", "info", "event")
WORLD = "world"                        # 세계 단위 컴포넌트의 kind 표식(id는 "")
_MISSING = object()


def address(kind: str, eid: str, component: str) -> tuple:
    if kind == WORLD:
        if eid not in ("", None):
            raise KernelError("세계 단위 컴포넌트에는 id가 없습니다")
        eid = ""
    elif kind not in ENTITY_KINDS:
        raise KernelError(f"알 수 없는 엔티티 종류 {kind!r}")
    elif not isinstance(eid, str) or not eid:
        raise KernelError(f"{kind} id가 필요합니다")
    if not isinstance(component, str) or not component:
        raise KernelError("컴포넌트 이름이 필요합니다")
    return (kind, eid, component)


class World:
    def __init__(self, data: dict | None = None):
        data = data or {}
        try:
            _check(data)
        except CanonicalError as e:
            raise KernelError(f"세계가 정규 JSON이 아닙니다 — {e}") from e
        unknown = set(data) - {"version", "entities", "world"}
        if unknown:
            raise KernelError(f"세계에 알 수 없는 키 {sorted(unknown)}")
        ents = data.get("entities") or {}
        if set(ents) - set(ENTITY_KINDS):
            raise KernelError(f"알 수 없는 엔티티 종류 {sorted(set(ents) - set(ENTITY_KINDS))}")
        self._d = {"version": copy.deepcopy(data.get("version")),
                   "entities": {k: copy.deepcopy(ents.get(k) or {}) for k in ENTITY_KINDS},
                   "world": copy.deepcopy(data.get("world") or {})}

    # ── 정체성 ──
    @property
    def version(self):
        return copy.deepcopy(self._d["version"])

    def with_version(self, version) -> "World":
        """버전만 바꾼 사본(커밋 owner 전용 — 투영은 버전을 바꾸지 않는다)."""
        w = self.clone()
        w._d["version"] = copy.deepcopy(version)
        return w

    # ── 저장값(내부) ──
    def _bucket(self, addr):
        kind, eid, comp = addr
        if kind == WORLD:
            return self._d["world"]
        return self._d["entities"][kind].get(eid)

    def stored(self, addr):
        b = self._bucket(addr)
        return _MISSING if b is None or addr[2] not in b else b[addr[2]]

    def _store(self, addr, value):
        kind, eid, comp = addr
        if kind == WORLD:
            self._d["world"][comp] = value
        else:
            self._d["entities"][kind].setdefault(eid, {})[comp] = value

    def _drop(self, addr):
        kind, eid, comp = addr
        b = self._bucket(addr)
        if b is not None:
            b.pop(comp, None)
            if kind != WORLD and not b:
                self._d["entities"][kind].pop(eid, None)

    def stored_addresses(self):
        for comp in sorted(self._d["world"]):
            yield (WORLD, "", comp)
        for kind in ENTITY_KINDS:
            for eid in sorted(self._d["entities"][kind]):
                for comp in sorted(self._d["entities"][kind][eid]):
                    yield (kind, eid, comp)

    def entity_ids(self, kind: str) -> tuple:
        """변경분이 저장된 엔티티 id(정의에만 있는 엔티티는 포함하지 않는다)."""
        return tuple(sorted(self._d["entities"][kind]))

    # ── 사본·직렬화 ──
    def clone(self) -> "World":
        w = World.__new__(World)
        w._d = copy.deepcopy(self._d)
        return w

    def to_dict(self) -> dict:
        out = {"version": copy.deepcopy(self._d["version"]),
               "entities": {k: copy.deepcopy(v) for k, v in self._d["entities"].items() if v},
               "world": copy.deepcopy(self._d["world"])}
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "World":
        return cls(d)

    def digest(self) -> str:
        return content_digest(self.to_dict())
