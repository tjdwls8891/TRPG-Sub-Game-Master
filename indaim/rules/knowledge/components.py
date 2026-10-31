"""지식 — 런타임 정보(info.runtime)·정보 유효성/계보(info.status)·정보 계열(world.info_series)·인지(actor.mind·world.group_mind).

  info.runtime  런타임 정보 정의(12키, 명제 불변) — 저작 정보는 정의에만
  info.status   {} = CURRENT | {validity, successor_ids, predecessor_ids, changed_at_minute, changed_by_version}(부분 허용)
  actor.mind    {InfoId: 인지 기록 6키}  (기록 없음 = 미인지)
  world.group_mind {"group:<id>": {InfoId: 인지 기록}}
의미는 옛 core.information·cognition handler와 같다(PC 믿음은 PLAYER_EXPLICIT·RULE_FORCED만 등).
옛 INFORMATION_CREATE·LINK(여러 칸 쓰기)는 정보마다 효과 하나로 나뉜다.
"""
import re

from indaim.kernel import WORLD, Component
from indaim.kernel.components import SET_LIKE, SINGLE_VALUE, write

from .._common import keys, reject

CURRENT, SUPERSEDED, REFUTED = "CURRENT", "SUPERSEDED", "REFUTED"
VALIDITIES = (CURRENT, SUPERSEDED, REFUTED)
_ALLOWED = {CURRENT: {SUPERSEDED, REFUTED}, SUPERSEDED: {REFUTED}, REFUTED: set()}
DEF_KEYS = ("info_id", "kind", "proposition", "summary", "subject_refs", "tags", "fragments",
            "sensitivity", "source_ref", "derived_from", "series_ref", "created_at_minute")

EXISTENCE, CONTENT = "EXISTENCE", "CONTENT"
AWARENESS = (EXISTENCE, CONTENT)
_RANK = {EXISTENCE: 1, CONTENT: 2}
BELIEFS = ("SUSPECTED", "PROBABLE", "BELIEVED", "CERTAIN")
ROUTES = ("WITNESS", "PARTICIPATION", "DOCUMENT", "EVIDENCE", "TESTIMONY", "RUMOR", "OFFICIAL",
          "INFERENCE", "FORCED_STATEMENT", "DISINFORMATION", "MAP", "SIGNAGE", "SYSTEM_GRANTED")
PC_BELIEF_AUTHORITIES = ("PLAYER_EXPLICIT", "RULE_FORCED")
BELIEF_AUTHORITIES = ("PLAYER_EXPLICIT", "RULE_FORCED", "RULE", "AUTHORED")
RECORD_KEYS = ("awareness", "fragments", "belief", "acquisitions", "belief_provenance", "secrecy_awareness")
ACQ_KEYS = ("acquisition_id", "at_minute", "route", "source_actor", "source_group", "source_info",
            "evidence_ref", "event_instance", "place_id", "fragments", "note")


def _dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _now(view):
    c = view.read(WORLD, "", "clock") or {}
    m = c.get("minute_index")
    return m if isinstance(m, int) and not isinstance(m, bool) else None


# ── 정보 ──

def validate_definition(d) -> list:
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


def _definition(view, e):
    a = keys(e, ("definition",))
    d = dict(a["definition"]) if isinstance(a["definition"], dict) else a["definition"]
    if isinstance(d, dict):
        d.setdefault("created_at_minute", _now(view))
    errs = validate_definition(d)
    if errs:
        reject(e, "; ".join(errs))
    if d["info_id"] != e.address[1]:
        reject(e, "대상 InfoId와 정의 info_id가 다릅니다")
    if view.stored_has("info", e.address[1], "runtime") or view.stored_has("info", e.address[1], "status"):
        reject(e, f"이미 있는 InfoId {e.address[1]} — 명제는 불변(새 InfoId를 만드십시오)")
    return d


def _create(view, e):
    return [write(e, (), SINGLE_VALUE, _definition(view, e))]


def _status_init(view, e):
    """런타임 정보 생성의 짝(같은 계층) — 생성 시각·버전의 CURRENT 기록."""
    d = _definition(view, e)
    return [write(e, (), SINGLE_VALUE, {"validity": CURRENT, "successor_ids": [], "predecessor_ids": [],
                                        "changed_at_minute": d["created_at_minute"], "changed_by_version": view.version_id})]


def _status(view, iid) -> dict:
    return view.read("info", iid, "status") or {}


def _validity(view, e):
    a = keys(e, ("validity",))
    new = a["validity"]
    if new not in VALIDITIES:
        reject(e, f"validity는 {list(VALIDITIES)} 중 하나")
    cur = _status(view, e.address[1]).get("validity") or CURRENT
    if new == cur:
        return []
    if new not in _ALLOWED[cur]:
        reject(e, f"{e.address[1]}: {cur} → {new} 전이는 허용되지 않습니다(CURRENT로 되돌리지 않음)")
    return [write(e, ("validity",), SINGLE_VALUE, new), write(e, ("changed_at_minute",), SINGLE_VALUE, _now(view)),
            write(e, ("changed_by_version",), SINGLE_VALUE, view.version_id)]


def _reachable(view, start, target) -> bool:
    stack, seen = [start], set()
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(_status(view, cur).get("successor_ids") or [])
    return False


def _link_args(view, e):
    a = keys(e, ("predecessor", "successor"))
    p, s = a["predecessor"], a["successor"]
    if not (isinstance(p, str) and isinstance(s, str) and p and s) or p == s:
        reject(e, "predecessor·successor는 서로 다른 InfoId")
    if _reachable(view, s, p):
        reject(e, f"계보 순환 {p} → {s}")
    return p, s


def _add_successor(view, e):
    _p, s = _link_args(view, e)
    return [write(e, ("successor_ids",), SET_LIKE, {"add": [s], "remove": []})]


def _add_predecessor(view, e):
    p, _s = _link_args(view, e)
    return [write(e, ("predecessor_ids",), SET_LIKE, {"add": [p], "remove": []})]


def _series_set(view, e):
    a = keys(e, ("series_id", "current_info_id", "value_fingerprint", "revision_ordinal"))
    sid, rev = a["series_id"], a["revision_ordinal"]
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        reject(e, "revision_ordinal은 0 이상의 정수")
    cur = view.read(WORLD, "", "info_series", (sid,))
    expect = 0 if cur is None else cur["revision_ordinal"] + 1
    if rev != expect:
        reject(e, f"{sid}: revision_ordinal {rev} ≠ 기대값 {expect}")
    if not isinstance(a["current_info_id"], str) or not a["current_info_id"] \
            or not view.stored_has("info", a["current_info_id"], "runtime"):
        reject(e, f"{sid}: current_info_id가 런타임 정의에 없습니다(같은 묶음의 생성 효과를 인과 부모로)")
    return [write(e, (sid,), SINGLE_VALUE, {k: a[k] for k in ("current_info_id", "value_fingerprint", "revision_ordinal")})]


# ── 인지 ──

def empty_record() -> dict:
    return {"awareness": EXISTENCE, "fragments": [], "belief": None, "acquisitions": [],
            "belief_provenance": [], "secrecy_awareness": None}


def validate_record(rec) -> list:
    if not isinstance(rec, dict) or set(rec) != set(RECORD_KEYS):
        return [f"인지 기록 키는 정확히 {list(RECORD_KEYS)}"]
    errs = []
    if rec["awareness"] not in AWARENESS:
        errs.append(f"awareness는 {list(AWARENESS)} 중 하나")
    if rec["belief"] is not None and rec["belief"] not in BELIEFS:
        errs.append(f"belief는 null 또는 {list(BELIEFS)} 중 하나")
    if rec["belief"] is not None and rec["awareness"] != CONTENT:
        errs.append("믿음은 CONTENT 인지에만 둘 수 있습니다")
    for k in ("fragments", "acquisitions", "belief_provenance"):
        if not isinstance(rec[k], list):
            errs.append(f"{k}는 목록")
    ids = [x.get("acquisition_id") for x in rec["acquisitions"] if isinstance(x, dict)]
    if len(ids) != len(set(ids)):
        errs.append("acquisition_id 중복")
    return errs


def _mind_target(view, e):
    """(경로 앞부분, 기존 기록, PC holder인가). actor.mind = (info,), world.group_mind = (holder, info)."""
    iid = e.args.get("info_id")
    if not isinstance(iid, str) or not iid:
        reject(e, "info_id는 비어 있지 않은 문자열")
    kind, eid, comp = e.address
    if kind == "actor":
        path = (iid,)
        reg = view.read("actor", eid, "reg") or {}
        is_pc = reg.get("kind") == "PC" if reg else bool(re.match(r"^pc_\d+$", eid))
    else:
        holder = e.args.get("holder")
        if not isinstance(holder, str) or not holder.startswith("group:") or len(holder) <= len("group:"):
            reject(e, f"holder {holder!r}: 'group:<id>' 형식")
        path, is_pc = (holder, iid), False
    return path, view.read(kind, eid, comp, path), is_pc


def _write_rec(e, path, rec):
    errs = validate_record(rec)
    if errs:
        reject(e, "; ".join(errs))
    return [write(e, path, SINGLE_VALUE, rec)]


def _group_keys(e, base):
    return base + (("holder",) if e.address[0] == WORLD else ())


def _acquire(view, e):
    a = e.args
    if set(a) != set(_group_keys(e, ("info_id", "awareness", "acquisition"))):
        reject(e, "인자 키는 info_id·awareness·acquisition(+그룹이면 holder)")
    path, cur, _pc = _mind_target(view, e)
    aw, acq = a["awareness"], a["acquisition"]
    if aw not in AWARENESS:
        reject(e, f"awareness는 {list(AWARENESS)} 중 하나")
    if not isinstance(acq, dict) or set(acq) != set(ACQ_KEYS):
        reject(e, f"acquisition 키는 정확히 {list(ACQ_KEYS)}")
    if not isinstance(acq["acquisition_id"], str) or not acq["acquisition_id"]:
        reject(e, "acquisition_id는 안정 문자열(같은 원인 = 같은 id)")
    if acq["route"] not in ROUTES:
        reject(e, f"route는 {list(ROUTES)} 중 하나")
    rec = dict(cur) if cur else empty_record()
    for old in rec["acquisitions"]:
        if old.get("acquisition_id") == acq["acquisition_id"]:
            if old != acq:
                reject(e, f"acquisition_id {acq['acquisition_id']} 재사용(다른 내용)")
            return []
    if cur is None or _RANK[aw] > _RANK[rec["awareness"]]:
        rec["awareness"] = aw
    rec["fragments"] = _dedupe(list(rec["fragments"]) + list(acq["fragments"]))
    rec["acquisitions"] = list(rec["acquisitions"]) + [acq]
    return _write_rec(e, path, rec)


def _check_authority(e, is_pc, auth):
    if is_pc and auth not in PC_BELIEF_AUTHORITIES:
        reject(e, f"PC 믿음은 {list(PC_BELIEF_AUTHORITIES)} 권위로만 바뀝니다(받은 권위 {auth})")
    if auth not in BELIEF_AUTHORITIES:
        reject(e, f"authority는 {list(BELIEF_AUTHORITIES)} 중 하나")


def _upsert(view, e):
    a = e.args
    req = set(_group_keys(e, ("info_id", "fields")))
    if not req <= set(a) or set(a) - req - {"cause_ref", "authority"}:
        reject(e, "인자 키는 info_id·fields(+cause_ref, authority, 그룹이면 holder)")
    path, cur, is_pc = _mind_target(view, e)
    fields = a["fields"]
    allowed = {"awareness", "fragments", "secrecy_awareness"}
    if not isinstance(fields, dict) or not fields or set(fields) - allowed:
        reject(e, f"fields는 {sorted(allowed)}의 부분집합(믿음은 belief)")
    rec = dict(cur) if cur else empty_record()
    if "awareness" in fields:
        aw = fields["awareness"]
        if aw not in AWARENESS:
            reject(e, f"awareness는 {list(AWARENESS)} 중 하나")
        if cur and _RANK[aw] < _RANK[rec["awareness"]]:
            if not a.get("cause_ref"):
                reject(e, "인지 하향에는 cause_ref가 필요합니다")
            if rec["belief"] is not None:
                auth = a.get("authority")
                if auth is not None or is_pc:
                    _check_authority(e, is_pc, auth)
                    rec["belief_provenance"] = _dedupe(list(rec["belief_provenance"]) +
                                                       [{"belief": None, "authority": auth, "cause_ref": a["cause_ref"]}])
                rec["belief"] = None
        rec["awareness"] = aw
    if "fragments" in fields:
        if not isinstance(fields["fragments"], list):
            reject(e, "fragments는 목록")
        rec["fragments"] = _dedupe(list(rec["fragments"]) + list(fields["fragments"]))
    if "secrecy_awareness" in fields:
        rec["secrecy_awareness"] = fields["secrecy_awareness"]
    return _write_rec(e, path, rec)


def _belief(view, e):
    a = e.args
    if set(a) != set(_group_keys(e, ("info_id", "belief", "authority", "cause_ref"))):
        reject(e, "인자 키는 info_id·belief·authority·cause_ref(+그룹이면 holder)")
    path, cur, is_pc = _mind_target(view, e)
    belief, auth, cause = a["belief"], a["authority"], a["cause_ref"]
    if belief is not None and belief not in BELIEFS:
        reject(e, f"belief는 null 또는 {list(BELIEFS)}")
    _check_authority(e, is_pc, auth)
    if not isinstance(cause, str) or not cause:
        reject(e, "cause_ref는 비어 있지 않은 문자열")
    if cur is None or cur["awareness"] != CONTENT:
        reject(e, "믿음은 CONTENT 인지 기록에만 설정합니다(먼저 획득)")
    if cur["belief"] == belief:
        return []
    rec = dict(cur)
    rec["belief"] = belief
    rec["belief_provenance"] = _dedupe(list(rec["belief_provenance"]) + [{"belief": belief, "authority": auth, "cause_ref": cause}])
    return _write_rec(e, path, rec)


MIND_OPS = {"acquire": _acquire, "upsert": _upsert, "belief": _belief}
COMPONENTS = (
    Component("runtime", "info", "rules.knowledge", {"create": _create}),
    Component("status", "info", "rules.knowledge", {"init": _status_init, "validity": _validity,
                                                    "add_successor": _add_successor, "add_predecessor": _add_predecessor}),
    Component("info_series", WORLD, "rules.knowledge", {"set": _series_set}),
    Component("mind", "actor", "rules.knowledge", dict(MIND_OPS)),
    Component("group_mind", WORLD, "rules.knowledge", dict(MIND_OPS)),
)
