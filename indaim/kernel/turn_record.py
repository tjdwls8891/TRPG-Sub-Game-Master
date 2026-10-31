"""턴 기록 — 커밋 하나의 입력·효과·결과(`turns.jsonl`, 추가만).

재무 사실은 담지 않는다 — 정산 저널을 `settlement_ref`로 가리킬 뿐이다(재무 권위는 그대로).
"""
import copy
import json
import os
from dataclasses import dataclass, field

from ._store import append_line
from .canon import CanonicalError, _check
from .errors import KernelError, KernelStoreError

RECORD_SCHEMA = 1
OUTCOMES = ("COMMITTED", "FAILED_SYSTEM", "FAILED_CONFLICT")
_FIELDS = ("turn_id", "base_version", "new_version", "input", "effects_plan", "effects_settle",
           "story_ref", "settlement_ref", "outcome")


@dataclass(frozen=True)
class TurnRecord:
    turn_id: str
    base_version: str | None
    new_version: str | None
    input: dict = field(default_factory=dict)            # {declaration, intent, rolls}
    effects_plan: tuple = ()                              # Effect.to_dict 목록
    effects_settle: tuple = ()
    story_ref: str | None = None
    settlement_ref: str | None = None
    outcome: dict = field(default_factory=lambda: {"kind": "COMMITTED"})

    def __post_init__(self):
        if not isinstance(self.turn_id, str) or not self.turn_id:
            raise KernelError("turn_id가 필요합니다")
        kind = (self.outcome or {}).get("kind")
        if kind not in OUTCOMES:
            raise KernelError(f"{self.turn_id}: 알 수 없는 결과 {kind!r}")
        if kind == "COMMITTED" and not self.new_version:
            raise KernelError(f"{self.turn_id}: 커밋된 턴에는 새 버전이 필요합니다")
        if kind != "COMMITTED" and (self.new_version or self.effects_plan or self.effects_settle):
            raise KernelError(f"{self.turn_id}: 실패한 턴은 버전·효과를 남기지 않습니다")
        try:
            _check(self.to_dict())
        except CanonicalError as e:
            raise KernelError(f"{self.turn_id}: 정규 JSON이 아닙니다 — {e}") from e

    def to_dict(self) -> dict:
        d = {k: copy.deepcopy(getattr(self, k)) for k in _FIELDS}
        d["effects_plan"], d["effects_settle"] = list(d["effects_plan"]), list(d["effects_settle"])
        return {"schema": RECORD_SCHEMA, **d}

    @classmethod
    def from_dict(cls, d: dict) -> "TurnRecord":
        if d.get("schema") != RECORD_SCHEMA:
            raise KernelError(f"알 수 없는 턴 기록 형식 {d.get('schema')!r}")
        kw = {k: d.get(k) for k in _FIELDS}
        kw["effects_plan"] = tuple(kw["effects_plan"] or ())
        kw["effects_settle"] = tuple(kw["effects_settle"] or ())
        kw["input"] = kw["input"] or {}
        return cls(**kw)


class TurnLog:
    def __init__(self, root: str):
        self.path = os.path.join(root, "turns.jsonl")

    def read_all(self) -> list:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    out.append(TurnRecord.from_dict(json.loads(line)))
                except Exception as e:
                    raise KernelStoreError(f"{self.path}:{n} 손상된 턴 기록") from e
        return out

    def append(self, record: TurnRecord) -> None:
        if any(r.turn_id == record.turn_id for r in self.read_all()):
            raise KernelStoreError(f"이미 기록된 턴 {record.turn_id}")
        append_line(self.path, record.to_dict())

    def find(self, turn_id: str) -> TurnRecord | None:
        return next((r for r in self.read_all() if r.turn_id == turn_id), None)
