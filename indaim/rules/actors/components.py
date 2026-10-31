"""행위자 등록(actor.reg) — {kind, display_name, source_kind, source_key, aliases}. 개명 = id 유지·옛 이름은 별칭."""
import re

from indaim.kernel import Component
from indaim.kernel.components import SINGLE_VALUE, write

from .._common import keys, reject

KINDS = ("PC", "NPC")
SOURCE_KINDS = ("PLAYER", "AUTHORED_NPC", "SESSION_NPC")
ENTRY_KEYS = ("kind", "display_name", "source_kind", "source_key", "aliases")
_EXPLICIT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")
_PC_ID = re.compile(r"^pc_[0-9]+$")


def _dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def validate_entry(aid, e) -> list:
    if not isinstance(aid, str) or not _EXPLICIT_ID.match(aid):
        return [f"ActorId {aid!r} 형식이 잘못됐습니다"]
    if not isinstance(e, dict) or set(e) != set(ENTRY_KEYS):
        return [f"{aid}: 항목 키는 정확히 {list(ENTRY_KEYS)}여야 합니다"]
    errs = []
    if e["kind"] not in KINDS:
        errs.append(f"{aid}: kind는 {list(KINDS)} 중 하나")
    if e["source_kind"] not in SOURCE_KINDS:
        errs.append(f"{aid}: source_kind는 {list(SOURCE_KINDS)} 중 하나")
    if (e["kind"] == "PC") != (e["source_kind"] == "PLAYER"):
        errs.append(f"{aid}: PC ⇔ source_kind PLAYER")
    if e["kind"] == "PC" and not _PC_ID.match(aid):
        errs.append(f"{aid}: PC ActorId는 'pc_<uid>' 형식")
    if not isinstance(e["display_name"], str) or not e["display_name"].strip():
        errs.append(f"{aid}: display_name은 비어 있지 않은 문자열")
    if e["source_key"] is not None and not isinstance(e["source_key"], str):
        errs.append(f"{aid}: source_key는 문자열 또는 null")
    if not isinstance(e["aliases"], list) or not all(isinstance(x, str) and x for x in e["aliases"]):
        errs.append(f"{aid}: aliases는 문자열 목록")
    return errs


def _register(view, e):
    a = e.args
    required = {"kind", "display_name", "source_kind", "source_key"}
    if not required <= set(a) or set(a) - required - {"aliases"}:
        reject(e, f"인자 키는 {sorted(required)} + aliases(선택)")
    aid = e.address[1]
    entry = {k: a.get(k) for k in ENTRY_KEYS}
    entry["aliases"] = _dedupe(a.get("aliases") or [])
    errs = validate_entry(aid, entry)
    if errs:
        reject(e, "; ".join(errs))
    if view.stored_has("actor", aid, "reg"):
        reject(e, f"이미 등록된 ActorId {aid}")
    return [write(e, (), SINGLE_VALUE, entry)]


def _rename(view, e):
    a = keys(e, ("display_name",))
    aid, new = e.address[1], a["display_name"]
    if not view.stored_has("actor", aid, "reg"):
        reject(e, f"등록되지 않은 ActorId {aid}")
    cur = view.read("actor", aid, "reg")
    if not isinstance(new, str) or not new.strip():
        reject(e, "display_name은 비어 있지 않은 문자열")
    if new == cur["display_name"]:
        return []
    aliases = [x for x in _dedupe((cur.get("aliases") or []) + [cur["display_name"]]) if x != new]
    return [write(e, ("display_name",), SINGLE_VALUE, new), write(e, ("aliases",), SINGLE_VALUE, aliases)]


COMPONENTS = (Component("reg", "actor", "rules.actors", {"register": _register, "rename": _rename}),)
