"""관계 — 희소 delta(행렬 없음). 행위자에서 나가는 관계는 actor.bond.relations, 그룹은 world.group_bond.

  actor.bond        {"relations": {"<from>-><to>": {from_holder, to_holder, deltas: [Delta]}}, "met": 순번?}
  world.group_bond  {"<from>-><to>": {...}}   (from이 group:… 인 관계)
같은 delta_id 재적용은 멱등, 같은 id 다른 내용은 거부(옛 core.relationships와 같다).
"""
from indaim.kernel import WORLD, Component
from indaim.kernel.components import APPEND_DEDUPE, SINGLE_VALUE, write

from .._common import keys, reject

DELTA_KEYS = ("delta_id", "from_holder", "to_holder", "kind", "summary", "at_minute", "source_ref")


def _holder_ok(h) -> bool:
    if not isinstance(h, str) or ":" not in h:
        return False
    kind, ident = h.split(":", 1)
    return kind in ("actor", "group") and bool(ident)


def validate_delta(d) -> list:
    if not isinstance(d, dict) or set(d) != set(DELTA_KEYS):
        return [f"delta 키는 정확히 {list(DELTA_KEYS)}"]
    errs = []
    if not isinstance(d["delta_id"], str) or not d["delta_id"]:
        errs.append("delta_id는 비어 있지 않은 문자열")
    for k in ("from_holder", "to_holder"):
        if not _holder_ok(d[k]):
            errs.append(f"{k}는 'actor:<id>'·'group:<id>'")
    if d["from_holder"] == d["to_holder"]:
        errs.append("자기 자신과의 관계 delta")
    if not isinstance(d["kind"], str) or not d["kind"]:
        errs.append("kind는 비어 있지 않은 문자열")
    return errs


def target_of(delta) -> tuple:
    """delta가 놓일 (kind, id, component) — 번역·생산자 공용."""
    h = (delta or {}).get("from_holder") or ""
    return ("actor", h[len("actor:"):], "bond") if h.startswith("actor:") else (WORLD, "", "group_bond")


def _append(view, e):
    d = keys(e, ("delta",))["delta"]
    errs = validate_delta(d)
    if errs:
        reject(e, "; ".join(errs))
    if target_of(d) != e.address:
        reject(e, "관계는 from_holder의 자리에만 둡니다")
    key = f"{d['from_holder']}->{d['to_holder']}"
    pre = ("relations", key) if e.address[0] == "actor" else (key,)
    for old in view.read(*e.address, pre + ("deltas",)) or []:
        if old.get("delta_id") == d["delta_id"]:
            if old != d:
                reject(e, f"delta_id {d['delta_id']} 재사용(다른 내용)")
            return []
    return [write(e, pre + ("from_holder",), SINGLE_VALUE, d["from_holder"]),
            write(e, pre + ("to_holder",), SINGLE_VALUE, d["to_holder"]),
            write(e, pre + ("deltas",), APPEND_DEDUPE, d)]


COMPONENTS = (Component("bond", "actor", "rules.relations", {"append": _append}),
              Component("group_bond", WORLD, "rules.relations", {"append": _append}))
