# 인지 — 희소 CognitionState · 획득 provenance · 믿음 권위 (H04-A)
#
# [권위 — DESIGN 05 §52~68 · H04 결정 게이트 A8~A11]
#   cognition_state = {HolderRef: {InfoId: CognitionRecord}}   HolderRef = "actor:<ActorId>" | "group:<GroupId>"
#   기록 없음 = 미인지(NONE을 저장하지 않는다).
#   CognitionRecord = {"awareness": "EXISTENCE"|"CONTENT", "fragments": [FragmentId],
#                      "belief": None|"SUSPECTED"|"PROBABLE"|"BELIEVED"|"CERTAIN",
#                      "acquisitions": [AcquisitionRecord], "belief_provenance": [..],
#                      "secrecy_awareness": None|bool}
#   · 노출·획득은 믿음을 자동으로 올리지 않는다(acquire는 belief를 건드리지 않는다).
#   · 믿음은 진실(validity)과 별개 — REFUTED 정보를 CERTAIN으로 믿을 수 있다(자동 교정 없음).
#   · PC 믿음 변경 권위는 PLAYER_EXPLICIT·RULE_FORCED뿐. 사건·묘사의 단순 노출로 PC 믿음을 바꾸지 않는다.
#     믿음을 바꾸는 모든 연산(BELIEF_SET, 믿음을 지우는 UPSERT 인지 하향)이 같은 관문 _check_belief_authority를 지난다.
#   · AcquisitionRecord는 안정 acquisition_id로 append+dedupe(같은 id 다른 내용은 거부). 임의 cap·삭제 없음.
#   · group 인지는 저장·질의만 — 구성원에게 자동 전파하지 않는다(소속·역할 정책은 후속 adapter).
from __future__ import annotations

from . import actors as _actors
from . import world_mutation as _wm

EXISTENCE, CONTENT = "EXISTENCE", "CONTENT"
AWARENESS = (EXISTENCE, CONTENT)
_AWARE_RANK = {EXISTENCE: 1, CONTENT: 2}
BELIEFS = ("SUSPECTED", "PROBABLE", "BELIEVED", "CERTAIN")
ROUTES = ("WITNESS", "PARTICIPATION", "DOCUMENT", "EVIDENCE", "TESTIMONY", "RUMOR", "OFFICIAL",
          "INFERENCE", "FORCED_STATEMENT", "DISINFORMATION", "MAP", "SIGNAGE", "SYSTEM_GRANTED")
PC_BELIEF_AUTHORITIES = ("PLAYER_EXPLICIT", "RULE_FORCED")
BELIEF_AUTHORITIES = ("PLAYER_EXPLICIT", "RULE_FORCED", "RULE", "AUTHORED")
RECORD_KEYS = ("awareness", "fragments", "belief", "acquisitions", "belief_provenance", "secrecy_awareness")
ACQ_KEYS = ("acquisition_id", "at_minute", "route", "source_actor", "source_group", "source_info",
            "evidence_ref", "event_instance", "place_id", "fragments", "note")

OP_COGNITION_ACQUIRE = "COGNITION_ACQUIRE"
OP_COGNITION_UPSERT = "COGNITION_UPSERT"
OP_COGNITION_BELIEF_SET = "COGNITION_BELIEF_SET"
COGNITION_OP_TYPES = (OP_COGNITION_ACQUIRE, OP_COGNITION_UPSERT, OP_COGNITION_BELIEF_SET)


def actor_holder(actor_id: str) -> str:
    return f"actor:{actor_id}"


def group_holder(group_id: str) -> str:
    return f"group:{group_id}"


def parse_holder(holder):
    """("actor"|"group", id) 또는 None."""
    if not isinstance(holder, str) or ":" not in holder:
        return None
    kind, ident = holder.split(":", 1)
    if kind not in ("actor", "group") or not ident:
        return None
    return kind, ident


def is_pc_holder(holder, world=None) -> bool:
    p = parse_holder(holder)
    if not p or p[0] != "actor":
        return False
    if world is not None:
        e = _actors.registry_of(world).get(p[1])
        if isinstance(e, dict):
            return e.get("kind") == "PC"
    return _actors.is_pc_actor_id(p[1])


def _state(world) -> dict:
    return (world.domain("cognition_state") if isinstance(world, _wm.ProjectedWorld)
            else getattr(world, "cognition_state", {})) or {}


def record_of(world, holder: str, info_id: str):
    """인지 기록(없으면 None = 미인지)."""
    return (_state(world).get(holder) or {}).get(info_id)


def awareness_of(world, holder, info_id) -> str:
    rec = record_of(world, holder, info_id)
    return rec["awareness"] if rec else "NONE"


def holders_aware_of(world, info_id) -> list:
    """정보를 아는 holder(정렬). group 기록은 group으로만 — 구성원 자동 확장 없음."""
    return sorted(h for h, recs in _state(world).items() if info_id in (recs or {}))


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
    ids = [a.get("acquisition_id") for a in rec["acquisitions"] if isinstance(a, dict)]
    if len(ids) != len(set(ids)):
        errs.append("acquisition_id 중복")
    return errs


def validate_state(state) -> list:
    if not isinstance(state, dict):
        return ["cognition_state는 객체여야 합니다"]
    errs = []
    for holder, recs in state.items():
        if parse_holder(holder) is None:
            errs.append(f"holder {holder!r}: 'actor:<id>'·'group:<id>' 형식이어야 합니다")
            continue
        if not isinstance(recs, dict):
            errs.append(f"{holder}: 객체여야 합니다")
            continue
        for iid, rec in recs.items():
            errs += [f"{holder}/{iid}: {e}" for e in validate_record(rec)]
    return errs


def acquisition_record(acquisition_id: str, route: str, *, at_minute=None, source_actor=None,
                       source_group=None, source_info=None, evidence_ref=None, event_instance=None,
                       place_id=None, fragments=(), note=None) -> dict:
    return {"acquisition_id": acquisition_id, "at_minute": at_minute, "route": route,
            "source_actor": source_actor, "source_group": source_group, "source_info": source_info,
            "evidence_ref": evidence_ref, "event_instance": event_instance, "place_id": place_id,
            "fragments": list(fragments), "note": note}


# ══════════════════════════════════════════════════════════════
#  typed 연산
# ══════════════════════════════════════════════════════════════

def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def _dedupe(seq):
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def _target(op, payload):
    holder, iid = payload.get("holder"), payload.get("info_id")
    if parse_holder(holder) is None:
        _reject(op, f"holder {holder!r}: 'actor:<id>'·'group:<id>' 형식")
    if not isinstance(iid, str) or not iid:
        _reject(op, "info_id는 비어 있지 않은 문자열")
    return holder, iid


def _write(op, holder, iid, rec):
    errs = validate_record(rec)
    if errs:
        _reject(op, "; ".join(errs))
    return [_wm.write(op, "cognition_state", (holder, iid), _wm.MERGE_SINGLE_VALUE, rec)]


def h_cognition_acquire(view, op, payload):
    """획득 기록 append+dedupe, 인지 수준은 올리기만, 믿음은 그대로."""
    if set(payload) != {"holder", "info_id", "awareness", "acquisition"}:
        _reject(op, "payload 키는 holder·info_id·awareness·acquisition")
    holder, iid = _target(op, payload)
    aw = payload["awareness"]
    if aw not in AWARENESS:
        _reject(op, f"awareness는 {list(AWARENESS)} 중 하나")
    acq = payload["acquisition"]
    if not isinstance(acq, dict) or set(acq) != set(ACQ_KEYS):
        _reject(op, f"acquisition 키는 정확히 {list(ACQ_KEYS)}")
    if not isinstance(acq["acquisition_id"], str) or not acq["acquisition_id"]:
        _reject(op, "acquisition_id는 안정 문자열(같은 원인 = 같은 id)")
    if acq["route"] not in ROUTES:
        _reject(op, f"route는 {list(ROUTES)} 중 하나")
    cur = view.get("cognition_state", (holder, iid))
    rec = dict(cur) if cur else empty_record()
    for old in rec["acquisitions"]:
        if old.get("acquisition_id") == acq["acquisition_id"]:
            if old != acq:
                _reject(op, f"acquisition_id {acq['acquisition_id']} 재사용(다른 내용)")
            return []                                        # 같은 원인의 재적용 = 멱등
    if cur is None or _AWARE_RANK[aw] > _AWARE_RANK[rec["awareness"]]:
        rec["awareness"] = aw
    rec["fragments"] = _dedupe(list(rec["fragments"]) + list(acq["fragments"]))
    rec["acquisitions"] = list(rec["acquisitions"]) + [acq]
    return _write(op, holder, iid, rec)


def _check_belief_authority(op, view, holder, auth):
    """믿음 변경 권위의 단일 관문(A9) — PC는 PLAYER_EXPLICIT·RULE_FORCED만."""
    if is_pc_holder(holder, view) and auth not in PC_BELIEF_AUTHORITIES:
        _reject(op, f"PC 믿음은 {list(PC_BELIEF_AUTHORITIES)} 권위로만 바뀝니다(받은 권위 {auth})")
    if auth not in BELIEF_AUTHORITIES:
        _reject(op, f"authority는 {list(BELIEF_AUTHORITIES)} 중 하나")


def h_cognition_upsert(view, op, payload):
    """인지 수준·조각·비밀성 인지 갱신(믿음 제외). 하향도 허용하되 cause_ref를 요구한다.

    하향으로 기존 믿음이 사라지면 그것도 믿음 변경이다 — PC는 authority(PLAYER_EXPLICIT·RULE_FORCED)를
    함께 줘야 하고 provenance에 남는다. NPC는 기존 동작(cause_ref만, authority 생략 가능) 그대로.
    """
    if not {"holder", "info_id", "fields"} <= set(payload) or \
            set(payload) - {"holder", "info_id", "fields", "cause_ref", "authority"}:
        _reject(op, "payload 키는 holder·info_id·fields(+cause_ref, authority)")
    holder, iid = _target(op, payload)
    fields = payload["fields"]
    allowed = {"awareness", "fragments", "secrecy_awareness"}
    if not isinstance(fields, dict) or not fields or set(fields) - allowed:
        _reject(op, f"fields는 {sorted(allowed)}의 부분집합(믿음은 COGNITION_BELIEF_SET)")
    cur = view.get("cognition_state", (holder, iid))
    rec = dict(cur) if cur else empty_record()
    if "awareness" in fields:
        aw = fields["awareness"]
        if aw not in AWARENESS:
            _reject(op, f"awareness는 {list(AWARENESS)} 중 하나")
        if cur and _AWARE_RANK[aw] < _AWARE_RANK[rec["awareness"]]:
            if not payload.get("cause_ref"):
                _reject(op, "인지 하향에는 cause_ref가 필요합니다")
            if rec["belief"] is not None:                    # 내용을 잃으면 믿음 대상도 없다 = 믿음 변경
                auth = payload.get("authority")
                if auth is not None or is_pc_holder(holder, view):
                    _check_belief_authority(op, view, holder, auth)
                    prov = {"belief": None, "authority": auth, "cause_ref": payload["cause_ref"]}
                    rec["belief_provenance"] = _dedupe(list(rec["belief_provenance"]) + [prov])
                rec["belief"] = None
        rec["awareness"] = aw
    if "fragments" in fields:
        if not isinstance(fields["fragments"], list):
            _reject(op, "fragments는 목록")
        rec["fragments"] = _dedupe(list(rec["fragments"]) + list(fields["fragments"]))
    if "secrecy_awareness" in fields:
        rec["secrecy_awareness"] = fields["secrecy_awareness"]
    return _write(op, holder, iid, rec)


def h_cognition_belief_set(view, op, payload):
    """믿음 설정. PC는 PLAYER_EXPLICIT·RULE_FORCED만(A9). 기록·CONTENT 인지 필요."""
    if set(payload) != {"holder", "info_id", "belief", "authority", "cause_ref"}:
        _reject(op, "payload 키는 holder·info_id·belief·authority·cause_ref")
    holder, iid = _target(op, payload)
    belief, auth, cause = payload["belief"], payload["authority"], payload["cause_ref"]
    if belief is not None and belief not in BELIEFS:
        _reject(op, f"belief는 null 또는 {list(BELIEFS)}")
    _check_belief_authority(op, view, holder, auth)
    if not isinstance(cause, str) or not cause:
        _reject(op, "cause_ref는 비어 있지 않은 문자열")
    cur = view.get("cognition_state", (holder, iid))
    if cur is None or cur["awareness"] != CONTENT:
        _reject(op, "믿음은 CONTENT 인지 기록에만 설정합니다(먼저 획득)")
    if cur["belief"] == belief:
        return []
    rec = dict(cur)
    rec["belief"] = belief
    prov = {"belief": belief, "authority": auth, "cause_ref": cause}
    rec["belief_provenance"] = _dedupe(list(rec["belief_provenance"]) + [prov])
    return _write(op, holder, iid, rec)


_HANDLERS = {OP_COGNITION_ACQUIRE: h_cognition_acquire,
             OP_COGNITION_UPSERT: h_cognition_upsert,
             OP_COGNITION_BELIEF_SET: h_cognition_belief_set}


def register(registry: _wm.OperationRegistry) -> None:
    for t, h in _HANDLERS.items():
        registry.register(t, h)
