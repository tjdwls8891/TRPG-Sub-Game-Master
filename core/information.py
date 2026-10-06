# 정보 — InformationDefinition 해석 · InformationState 유효성/계보 · InformationSeries (H04-A)
#
# [권위 — DESIGN 05 §28~51 · H04 결정 게이트 A6·A7·A26]
#   정의(무엇인가)와 상태(지금 유효한가)를 분리한다.
#     · 저작 정의: 컴파일된 definition["information_definitions"] (명시 registry + NPC 비밀 + 퀘스트 hidden)
#     · 런타임 정의: information_state.runtime_definitions — 한 번 만들면 명제를 바꾸지 않는다
#       (갱신 연산이 없다). 세계가 바뀌면 새 InfoId를 만들고 옛 정보를 SUPERSEDED로 둔다.
#   information_state (논리적 빈 값 {} 허용 — normalized()가 아래 3키로 읽는다):
#     {"runtime_definitions": {InfoId: def},
#      "status": {InfoId: {"validity", "successor_ids", "predecessor_ids",
#                          "changed_at_minute", "changed_by_version"}},
#      "series": {SeriesId: {"current_info_id", "value_fingerprint", "revision_ordinal"}}}
#   status 기록이 없는 저작·런타임 정보 = CURRENT. 수백 개의 CURRENT를 복제하지 않는다.
#   validity: CURRENT → SUPERSEDED|REFUTED, SUPERSEDED → REFUTED. CURRENT로 되돌리지 않는다
#   (A→B→A 계열 값도 새 revision InfoId가 된다).
#
# [하지 않는 것]
#   · 정보 유효성 변화로 인지(cognition)를 자동 교정하지 않는다 — 옛 InfoId를 믿는 행위자가 남는다.
#   · 레거시 info_ledger·known_secrets를 새 정본에 조용히 쓰지 않는다(이행은 H07).
#   · provider를 부르지 않는다. 계열 명제는 저작 템플릿 + 결정적 표시 해석으로 만든다.
from __future__ import annotations

from . import world_mutation as _wm
from . import world_refs as _refs
from .world_ids import content_digest, stable_digest

CURRENT, SUPERSEDED, REFUTED = "CURRENT", "SUPERSEDED", "REFUTED"
VALIDITIES = (CURRENT, SUPERSEDED, REFUTED)
_ALLOWED = {CURRENT: {SUPERSEDED, REFUTED}, SUPERSEDED: {REFUTED}, REFUTED: set()}

DEF_KEYS = ("info_id", "kind", "proposition", "summary", "subject_refs", "tags", "fragments",
            "sensitivity", "source_ref", "derived_from", "series_ref", "created_at_minute")
STATUS_KEYS = ("validity", "successor_ids", "predecessor_ids", "changed_at_minute", "changed_by_version")

OP_INFORMATION_CREATE = "INFORMATION_CREATE"
OP_INFORMATION_VALIDITY_SET = "INFORMATION_VALIDITY_SET"
OP_INFORMATION_LINK = "INFORMATION_LINK"
OP_INFORMATION_SERIES_SET = "INFORMATION_SERIES_SET"
INFORMATION_OP_TYPES = (OP_INFORMATION_CREATE, OP_INFORMATION_VALIDITY_SET,
                        OP_INFORMATION_LINK, OP_INFORMATION_SERIES_SET)


def _state_of(world) -> dict:
    return (world.domain("information_state") if isinstance(world, _wm.ProjectedWorld)
            else getattr(world, "information_state", {})) or {}


def normalized(state) -> dict:
    s = state or {}
    return {"runtime_definitions": dict(s.get("runtime_definitions") or {}),
            "status": dict(s.get("status") or {}),
            "series": dict(s.get("series") or {})}


def normalized_status(rec) -> dict:
    r = rec or {}
    return {"validity": r.get("validity") or CURRENT,
            "successor_ids": list(r.get("successor_ids") or []),
            "predecessor_ids": list(r.get("predecessor_ids") or []),
            "changed_at_minute": r.get("changed_at_minute"),
            "changed_by_version": r.get("changed_by_version")}


# ══════════════════════════════════════════════════════════════
#  질의
# ══════════════════════════════════════════════════════════════

def authored_definition(definition: dict, info_id: str):
    return (definition.get("information_definitions") or {}).get(info_id)


def runtime_definition(state, info_id: str):
    return normalized(state)["runtime_definitions"].get(info_id)


def resolve_definition(definition: dict, state, info_id: str):
    return authored_definition(definition, info_id) or runtime_definition(state, info_id)


def exists(definition: dict, state, info_id: str) -> bool:
    return resolve_definition(definition, state, info_id) is not None


def effective_validity(definition: dict, state, info_id: str):
    """정보의 현재 유효성. 정의가 없는 InfoId는 None(모름)."""
    if not exists(definition, state, info_id):
        return None
    return normalized_status(normalized(state)["status"].get(info_id))["validity"]


def successors(state, info_id) -> list:
    return normalized_status(normalized(state)["status"].get(info_id))["successor_ids"]


def predecessors(state, info_id) -> list:
    return normalized_status(normalized(state)["status"].get(info_id))["predecessor_ids"]


def lineage_forward(state, info_id, limit: int = 1000) -> list:
    """info_id부터 successor를 따라간 결정적 사슬(분기 시 정렬 첫 항목)."""
    out, cur, seen = [info_id], info_id, {info_id}
    while len(out) < limit:
        nxt = sorted(successors(state, cur))
        if not nxt or nxt[0] in seen:
            break
        cur = nxt[0]
        seen.add(cur)
        out.append(cur)
    return out


def _reachable(state, start, target) -> bool:
    stack, seen = [start], set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(successors(state, cur))
    return False


def validate_definition_record(d) -> list:
    if not isinstance(d, dict) or set(d) != set(DEF_KEYS):
        return [f"런타임 정보 정의 키는 정확히 {list(DEF_KEYS)}여야 합니다"]
    errs = []
    if not isinstance(d["info_id"], str) or not d["info_id"]:
        errs.append("info_id는 비어 있지 않은 문자열")
    if d["kind"] != "RUNTIME":
        errs.append("런타임 정의 kind는 RUNTIME")
    if not isinstance(d["proposition"], str) or not d["proposition"].strip():
        errs.append("proposition은 비어 있지 않은 문자열")
    for k in ("subject_refs", "tags", "fragments", "derived_from"):
        if not isinstance(d[k], list):
            errs.append(f"{k}는 목록")
    return errs


def validate_state(definition: dict, state) -> list:
    """information_state 정본 검사(정의·상태·계보·계열)."""
    if state != {} and not isinstance(state, dict):
        return ["information_state는 객체여야 합니다"]
    if isinstance(state, dict) and set(state) - {"runtime_definitions", "status", "series"}:
        return [f"information_state 알 수 없는 키 {sorted(set(state) - {'runtime_definitions', 'status', 'series'})}"]
    n = normalized(state)
    errs = []
    for iid, d in n["runtime_definitions"].items():
        errs += [f"{iid}: {e}" for e in validate_definition_record(d)]
        if isinstance(d, dict) and d.get("info_id") != iid:
            errs.append(f"{iid}: 키와 info_id가 다릅니다")
        if authored_definition(definition, iid):
            errs.append(f"{iid}: 저작 InfoId와 충돌하는 런타임 정의")
    for iid, rec in n["status"].items():
        if not exists(definition, state, iid):
            errs.append(f"status {iid}: 정의가 없는 InfoId")
        if not isinstance(rec, dict) or set(rec) - set(STATUS_KEYS):
            errs.append(f"status {iid}: 알 수 없는 필드")
            continue
        if normalized_status(rec)["validity"] not in VALIDITIES:
            errs.append(f"status {iid}: validity는 {list(VALIDITIES)} 중 하나")
        for other in normalized_status(rec)["successor_ids"]:
            if iid not in predecessors(state, other):
                errs.append(f"계보 {iid} → {other}: 역방향 predecessor 누락")
            if _reachable(state, other, iid):
                errs.append(f"계보 순환 {iid} ↔ {other}")
    for sid, ss in n["series"].items():
        if not isinstance(ss, dict) or set(ss) != {"current_info_id", "value_fingerprint", "revision_ordinal"}:
            errs.append(f"series {sid}: 형태가 잘못됐습니다")
        elif ss["current_info_id"] not in n["runtime_definitions"]:
            errs.append(f"series {sid}: current_info_id가 런타임 정의에 없습니다")
    return errs


# ══════════════════════════════════════════════════════════════
#  typed 연산
# ══════════════════════════════════════════════════════════════

def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def _now(view):
    c = view.domain("world_clock") or {}
    return c.get("minute_index") if isinstance(c.get("minute_index"), int) else None


def h_information_create(view, op, payload):
    if set(payload) != {"definition"}:
        _reject(op, "payload 키는 definition")
    d = dict(payload["definition"])
    d.setdefault("created_at_minute", _now(view))
    errs = validate_definition_record(d)
    if errs:
        _reject(op, "; ".join(errs))
    iid = d["info_id"]
    if view.has("information_state", ("runtime_definitions", iid)) or view.has("information_state", ("status", iid)):
        _reject(op, f"이미 있는 InfoId {iid} — 명제는 불변(새 InfoId를 만드십시오)")
    status = {"validity": CURRENT, "successor_ids": [], "predecessor_ids": [],
              "changed_at_minute": d["created_at_minute"],
              "changed_by_version": view.get("world_version", ("version_id",))}
    return [_wm.write(op, "information_state", ("runtime_definitions", iid), _wm.MERGE_SINGLE_VALUE, d),
            _wm.write(op, "information_state", ("status", iid), _wm.MERGE_SINGLE_VALUE, status)]


def h_information_validity_set(view, op, payload):
    if set(payload) != {"info_id", "validity"}:
        _reject(op, "payload 키는 info_id·validity")
    iid, new = payload["info_id"], payload["validity"]
    if new not in VALIDITIES:
        _reject(op, f"validity는 {list(VALIDITIES)} 중 하나")
    cur = normalized_status(view.get("information_state", ("status", iid)))["validity"]
    if new == cur:
        return []
    if new not in _ALLOWED[cur]:
        _reject(op, f"{iid}: {cur} → {new} 전이는 허용되지 않습니다(CURRENT로 되돌리지 않음)")
    return [_wm.write(op, "information_state", ("status", iid, "validity"), _wm.MERGE_SINGLE_VALUE, new),
            _wm.write(op, "information_state", ("status", iid, "changed_at_minute"), _wm.MERGE_SINGLE_VALUE,
                      _now(view)),
            _wm.write(op, "information_state", ("status", iid, "changed_by_version"), _wm.MERGE_SINGLE_VALUE,
                      view.get("world_version", ("version_id",)))]


def h_information_link(view, op, payload):
    if set(payload) != {"predecessor", "successor"}:
        _reject(op, "payload 키는 predecessor·successor")
    a, b = payload["predecessor"], payload["successor"]
    if not (isinstance(a, str) and isinstance(b, str) and a and b) or a == b:
        _reject(op, "predecessor·successor는 서로 다른 InfoId")
    state = view.domain("information_state")
    if _reachable(state, b, a):
        _reject(op, f"계보 순환 {a} → {b}")
    return [_wm.write(op, "information_state", ("status", a, "successor_ids"), _wm.MERGE_SET_LIKE,
                      {"add": [b], "remove": []}),
            _wm.write(op, "information_state", ("status", b, "predecessor_ids"), _wm.MERGE_SET_LIKE,
                      {"add": [a], "remove": []})]


def h_information_series_set(view, op, payload):
    keys = {"series_id", "current_info_id", "value_fingerprint", "revision_ordinal"}
    if set(payload) != keys:
        _reject(op, f"payload 키는 {sorted(keys)}")
    sid, rev = payload["series_id"], payload["revision_ordinal"]
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        _reject(op, "revision_ordinal은 0 이상의 정수")
    cur = view.get("information_state", ("series", sid))
    expect = 0 if cur is None else cur["revision_ordinal"] + 1
    if rev != expect:
        _reject(op, f"{sid}: revision_ordinal {rev} ≠ 기대값 {expect}")
    if not view.has("information_state", ("runtime_definitions", payload["current_info_id"])):
        _reject(op, f"{sid}: current_info_id가 런타임 정의에 없습니다(같은 묶음의 생성 연산을 인과 부모로)")
    rec = {k: payload[k] for k in ("current_info_id", "value_fingerprint", "revision_ordinal")}
    return [_wm.write(op, "information_state", ("series", sid), _wm.MERGE_SINGLE_VALUE, rec)]


_HANDLERS = {OP_INFORMATION_CREATE: h_information_create,
             OP_INFORMATION_VALIDITY_SET: h_information_validity_set,
             OP_INFORMATION_LINK: h_information_link,
             OP_INFORMATION_SERIES_SET: h_information_series_set}


def register(registry: _wm.OperationRegistry) -> None:
    for t, h in _HANDLERS.items():
        registry.register(t, h)


# ══════════════════════════════════════════════════════════════
#  고수준 연산 빌더 (같은 묶음 안 원자 조합)
# ══════════════════════════════════════════════════════════════

def runtime_definition_record(info_id, proposition, *, summary=None, subject_refs=(), tags=(),
                              fragments=(), sensitivity=None, source_ref=None, derived_from=(),
                              series_ref=None, created_at_minute=None) -> dict:
    return {"info_id": info_id, "kind": "RUNTIME", "proposition": proposition, "summary": summary,
            "subject_refs": list(subject_refs), "tags": list(tags), "fragments": list(fragments),
            "sensitivity": sensitivity, "source_ref": source_ref, "derived_from": list(derived_from),
            "series_ref": series_ref, "created_at_minute": created_at_minute}


def build_supersede_ops(old_id: str, new_definition: dict, *, op_prefix: str, source_kind: str,
                        validity: str = SUPERSEDED, parents=()) -> list:
    """옛 정보 SUPERSEDED/REFUTED + 새 런타임 정의 + 계보 링크. 링크는 생성의 인과 자식."""
    mk = lambda i, t, p, par=(): _wm.make_op(f"{op_prefix}.{i}", t, source_kind, p,
                                              causal_parent_op_ids=tuple(parents) + tuple(par))
    create = mk("create", OP_INFORMATION_CREATE, {"definition": new_definition})
    return [create,
            mk("validity", OP_INFORMATION_VALIDITY_SET, {"info_id": old_id, "validity": validity}),
            mk("link", OP_INFORMATION_LINK, {"predecessor": old_id, "successor": new_definition["info_id"]},
               (create.op_id,))]


# ── InformationSeries ──

def series_definition(definition: dict, series_id: str):
    return (definition.get("information_series") or {}).get(series_id)


def value_fingerprint(value) -> str:
    return content_digest({"series_value": value})


def series_info_id(series_id: str, revision_ordinal: int, fingerprint: str) -> str:
    return "info_" + stable_digest("info.series.v1", series_id, revision_ordinal, fingerprint)


def render_series_proposition(sdef: dict, value, definition: dict, world) -> str:
    return sdef["proposition_template"].replace("{value}", _refs.display_value(value, definition, world))


def series_dependency_keys(sdef: dict) -> list:
    return _refs.dependency_keys(sdef["value_ref"])


def _series_ops(definition, world, sid, sdef, value, rev, prev_id, *, op_prefix, source_kind, parents=()):
    fp = value_fingerprint(value)
    new_id = series_info_id(sid, rev, fp)
    now = _refs.EvalContext(definition, world).now()
    rec = runtime_definition_record(
        new_id, render_series_proposition(sdef, value, definition, world),
        subject_refs=sdef.get("subject_refs") or (), tags=sdef.get("tags") or (),
        sensitivity=sdef.get("sensitivity"), source_ref=["information_series", sid],
        derived_from=[prev_id] if prev_id else [], series_ref={"series_id": sid, "revision_ordinal": rev},
        created_at_minute=now)
    mk = lambda i, t, p, par=(): _wm.make_op(f"{op_prefix}.{i}", t, source_kind, p,
                                              causal_parent_op_ids=tuple(parents) + tuple(par))
    if prev_id is None:
        create = mk("create", OP_INFORMATION_CREATE, {"definition": rec})
        ops = [create]
    else:
        ops = build_supersede_ops(prev_id, rec, op_prefix=op_prefix, source_kind=source_kind, parents=parents)
        create = ops[0]
    ops.append(mk("series", OP_INFORMATION_SERIES_SET,
                  {"series_id": sid, "current_info_id": new_id, "value_fingerprint": fp,
                   "revision_ordinal": rev}, (create.op_id,)))
    return ops


def build_series_init_ops(definition: dict, world, series_id: str, *, op_prefix: str,
                          source_kind: str = "INITIALIZATION"):
    """계열 초기 상태(revision 0). 세션 적용 배선은 H06 — 여기서는 순수 빌더."""
    sdef = series_definition(definition, series_id)
    if sdef is None:
        return [], "SERIES_UNDEFINED"
    if series_id in normalized(_state_of(world))["series"]:
        return [], "SERIES_ALREADY_INITIALIZED"
    r = _refs.resolve(sdef["value_ref"], _refs.EvalContext(definition, world))
    if not r.known:
        return [], "SERIES_VALUE_UNKNOWN"
    return _series_ops(definition, world, series_id, sdef, r.value, 0, None,
                       op_prefix=op_prefix, source_kind=source_kind), None


def reevaluate_series(definition: dict, world, series_id: str, *, op_prefix: str,
                      source_kind: str = "WORLD_PROGRESSION", parents=()):
    """값이 바뀌었으면 successor 연산(같은 지문이면 no-op). 인지는 건드리지 않는다.

    반환: (ops, diagnostic|None). 초기화 전 계열·UNKNOWN 값은 바꾸지 않는다.
    """
    sdef = series_definition(definition, series_id)
    if sdef is None:
        return [], "SERIES_UNDEFINED"
    st = normalized(_state_of(world))["series"].get(series_id)
    if st is None:
        return [], "SERIES_UNINITIALIZED"
    r = _refs.resolve(sdef["value_ref"], _refs.EvalContext(definition, world))
    if not r.known:
        return [], "SERIES_VALUE_UNKNOWN"
    if value_fingerprint(r.value) == st["value_fingerprint"]:
        return [], None
    return _series_ops(definition, world, series_id, sdef, r.value, st["revision_ordinal"] + 1,
                       st["current_info_id"], op_prefix=op_prefix, source_kind=source_kind,
                       parents=parents), None
