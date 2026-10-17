# 구조화 행동 의도 — ActionReferenceManifest · ResolvedActionIntent (H05-A)
#
# [권위 — DESIGN 10 · 메인 기준선 §58.2]
#   자연어 플레이어 선언은 정본 정체성이 되지 않는다. 코드가 플레이어가 참조할 수 있는 후보를
#   짧은 transient 키(P1·R1·K1·I1)로 만들어 **기존 판단층위 호출**에 함께 주고(추가 provider 호출 0),
#   판단이 고른 키를 코드가 정본 id로 해석·검증한다.
#     · 후보 풀은 PC 지식을 존중한다 — 이름을 아는 장소·현재 장소·PC가 내용을 아는 정보만.
#       숨은 장소/정보/구간은 후보가 되지 않는다(키도 만들지 않는다).
#     · 없는 키·후보 밖 값·형식 오류는 그 부분만 거부(unresolved)된다. raw 문자열은 어떤 정본
#       필드에도 쓰이지 않는다.
#     · transient 키(P1 등)는 영속하지 않는다. 확정된 의미 선택은 안정 정체성으로만 남긴다
#       (경로 = 구간 id 서명, 접근 확인 = H04 access_action_id, 장소 = PlaceId).
#     · 매니페스트는 world_version·definition_revision에 묶인다 — 해석 시점에 다르면 stale.
#   ResolvedActionIntent 자체는 정본 변이가 아니다(계획 → 준비 → 커밋 경계).
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import cognition as _cog
from . import information as _info
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_state as _ws

NONE = "NONE"
MOVE_DESTINATION = "DESTINATION"
MOVE_LOCAL = "LOCAL_MICRO_MOVE"
MOVE_EXPLORE = "EXPLORE_DIRECTION"
MOVEMENT_KINDS = (NONE, MOVE_DESTINATION, MOVE_LOCAL, MOVE_EXPLORE)

TIME_DURATION = "DURATION"
TIME_DAYPART = "TARGET_DAYPART"
TIME_CLOCK = "TARGET_CLOCK"
TIME_KINDS = (NONE, TIME_DURATION, TIME_DAYPART, TIME_CLOCK)

ACTIVITIES = ("WAIT", "SLEEP", "SEARCH", "WORK", "TREAT", "READ", "OTHER")
STANCE_CLEAR = "CLEAR"
STANCE_BELIEFS = tuple(_cog.BELIEFS) + (STANCE_CLEAR,)

MAX_DURATION_MINUTES = 7 * 1440            # 한 선언의 명시 대기 상한(안전) — 초과는 거부
MAX_ACTION_MINUTES = 240                   # P1(D1): 이동·대기 아닌 행동 소요 시간 상한(구 추출 추가 시간 상한과 같음)
ACTION_MINUTES_FIELD = "action_minutes"
INTENT_FIELD = "action_intent"


def world_version_id(world) -> str | None:
    wv = world.domain("world_version") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "world_version", None)
    return _ws.version_id_of(wv)


UNKNOWN_REVISION = object()                 # ProjectedWorld는 정의 정체성을 싣지 않는다(판정 보류)


def world_definition_revision(world):
    """세션의 definition_revision(비가역 정체성). ProjectedWorld면 UNKNOWN_REVISION."""
    if world is None or isinstance(world, _wm.ProjectedWorld) or not hasattr(world, "definition_revision"):
        return UNKNOWN_REVISION
    return getattr(world, "definition_revision", None)


def is_definition_stale(bound_revision, world) -> bool:
    """결속된 revision ≠ 현재 세션 revision ⇒ stale(R2). 판정할 수 없으면 False."""
    rev = world_definition_revision(world)
    return rev is not UNKNOWN_REVISION and rev != bound_revision


@dataclass(frozen=True)
class ActionReferenceManifest:
    """판단층위에 주는 플레이어 선택 가능 후보(transient 키 → 정본 참조)."""
    world_version_id: str | None
    definition_revision: str | None
    pc_actor_id: str
    origin_place: str | None
    places: tuple = ()           # (key, place_id, label)
    routes: tuple = ()           # (key, signature(tuple), label, perceived_minutes|None)
    access: tuple = ()           # (key, access_action_id, label)
    infos: tuple = ()            # (key, info_id, label)
    dayparts: tuple = ()
    transit: tuple = ()          # H06 R2-06: (from_place, to_place, elapsed_min, total_min) — PC가 구간 안일 때만
    resolutions: tuple = ()      # H07: (key, (instance_id, option_id), label) — 플레이어 명시 결단 후보

    def _map(self, rows):
        return {r[0]: r for r in rows}

    def place_of(self, key):
        r = self._map(self.places).get(key)
        return r[1] if r else None

    def route_of(self, key):
        r = self._map(self.routes).get(key)
        return tuple(r[1]) if r else None

    def access_of(self, key):
        r = self._map(self.access).get(key)
        return r[1] if r else None

    def info_of(self, key):
        r = self._map(self.infos).get(key)
        return r[1] if r else None

    def resolution_of(self, key):
        r = self._map(self.resolutions).get(key)
        return tuple(r[1]) if r else None

    def keys(self) -> dict:
        return {"places": [r[0] for r in self.places], "routes": [r[0] for r in self.routes],
                "access": [r[0] for r in self.access], "infos": [r[0] for r in self.infos],
                "resolutions": [r[0] for r in self.resolutions]}

    def render(self) -> str:
        """판단층위 사용자 프롬프트 블록(정본 id·숨은 정보 없음 — 표시명과 키만)."""
        lines = ["\n[행동 참조 후보 — action_intent에는 아래 키만 쓴다. 후보에 없으면 NONE]"]
        shown = {pid: lbl for _, pid, lbl in self.places}               # H07 §5: PlaceId 대신 PC 지식 수준 표시명
        nm = lambda pid: shown.get(pid, pid)
        if self.origin_place:
            lines.append(f"현재 위치: {nm(self.origin_place)}")
        if self.transit:
            frm, to, el, tot = self.transit
            key = next((k for k, pid, _ in self.places if pid == to), None)
            lines.append(f"현재 상태: {nm(frm)}에서 {nm(to)}(으)로 가는 길 위(장소 밖) — 이 구간 {tot}분 중 {el}분 "
                         f"진행. 계속 가려면 destination={key or nm(to)}(또는 더 먼 목적지). "
                         f"출발지로 되돌아간 것으로 보지 말 것.")
        if self.places:
            lines.append("장소(목적지): " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.places))
        if self.routes:
            lines.append("제시된 경로 선택지: " + ", ".join(
                f"{k}={lbl}" + (f"(약 {m}분)" if m is not None else "") for k, _, lbl, m in self.routes))
        if self.access:
            lines.append("확인 가능한 접근 행동: " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.access))
        if self.infos:
            lines.append("PC가 아는 정보(명시적 태도 대상): " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.infos))
        if self.resolutions:
            lines.append("플레이어가 내릴 수 있는 결단: " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.resolutions))
        if self.dayparts:
            lines.append("시간대: " + ", ".join(self.dayparts))
        lines.append("· 이동 선언이면 movement_kind=DESTINATION과 destination 키. 장소 안의 작은 이동은 "
                     "LOCAL_MICRO_MOVE. 목적지가 후보에 없으면 destination=NONE.")
        lines.append("· 기다림/잠은 time_kind(DURATION·TARGET_DAYPART·TARGET_CLOCK)와 activity. "
                     "시간을 명시하지 않았으면 NONE.")
        lines.append("· action_minutes: 이동·대기·잠이 아닌 이번 행동(대화·조사·작업 등)이 작중에서 걸리는 분. "
                     f"플레이어가 시간을 말했으면 그 값, 아니면 행동에 맞는 현실적인 값(0~{MAX_ACTION_MINUTES}). "
                     "순간적인 행동·질문·이동·대기 선언은 0(이동·대기 시간은 코드가 따로 계산한다).")
        lines.append("· 플레이어가 정보를 믿는다/의심한다/믿지 않는다고 **직접 말한 경우에만** stance_*를 채운다. "
                     "추측으로 채우지 말 것.")
        if self.resolutions:
            lines.append("· resolution은 플레이어가 그 결단을 **이번 선언에서 명시적으로 내린 경우에만** 고른다. "
                         "망설임·질문·추측·다른 행동·GM 권유로는 채우지 않는다(NONE). 결단은 번복되지 않는다.")
        return "\n".join(lines) + "\n"

    def schema(self) -> dict:
        """판단층위 응답 스키마에 붙일 action_intent 속성(키는 enum으로 제한)."""
        def enum(keys):
            return {"type": "string", "enum": [NONE] + list(keys)}
        return {
            "type": "object",
            "description": "코드가 소유하는 이동·시간·접근·PC 태도 의도(후보 키만). 해당 없으면 NONE.",
            "properties": {
                "movement_kind": {"type": "string", "enum": list(MOVEMENT_KINDS)},
                "destination": enum(r[0] for r in self.places),
                "route_choice": enum(r[0] for r in self.routes),
                "access_confirm": enum(r[0] for r in self.access),
                "time_kind": {"type": "string", "enum": list(TIME_KINDS)},
                "duration_minutes": {"type": "integer"},
                "target_daypart": enum(self.dayparts),
                "target_hhmm": {"type": "string"},
                "activity": {"type": "string", "enum": [NONE] + list(ACTIVITIES)},
                "stance_info": enum(r[0] for r in self.infos),
                "stance_belief": {"type": "string", "enum": [NONE] + list(STANCE_BELIEFS)},
                "resolution": enum(r[0] for r in self.resolutions),
                ACTION_MINUTES_FIELD: {"type": "integer"},
            },
            "required": ["movement_kind", "time_kind", ACTION_MINUTES_FIELD],
        }

    def to_record(self) -> dict:
        """관측·진단용(키 → 정본 참조). transient 키는 영속 의미 이력에 쓰지 않는다."""
        return {"world_version_id": self.world_version_id, "definition_revision": self.definition_revision,
                "pc_actor_id": self.pc_actor_id, "keys": self.keys()}


def extend_judgment_schema(base_schema: dict, manifest: ActionReferenceManifest) -> dict:
    """기존 판단 스키마(정본 prompts.py 불변)의 사본에 action_intent 속성을 더한다."""
    out = copy.deepcopy(base_schema)
    out.setdefault("properties", {})[INTENT_FIELD] = manifest.schema()
    return out


def _place_label(definition: dict, pid: str, knowledge=None) -> str:
    """H07 §5: PC 지식 수준 표시명(내부 PlaceId 비노출). 해석 키는 여전히 PlaceId다."""
    from . import place_display as _pd
    return _pd.display_name(definition, knowledge or {}, pid, require_known=False)


def _info_label(definition: dict, world, info_id: str) -> str:
    st = world.domain("information_state") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "information_state", {})
    d = _info.resolve_definition(definition, st or {}, info_id) or {}
    return d.get("summary") or d.get("proposition") or info_id


def build_manifest(definition: dict, world, pc_actor_id: str, *, definition_revision=None,
                   route_options=(), access_options=()) -> ActionReferenceManifest:
    """PC 지식 기반 후보(숨은 장소·정보 제외). route_options/access_options는 호출자가 준비한 선택지."""
    pl = _sp.placement_of(world, pc_actor_id) or {}
    here = pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None
    transit = ()
    if pl.get("kind") == _sp.TRANSIT:
        clock = (world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
                 else getattr(world, "world_clock", {})) or {}
        now = clock.get("minute_index")
        st, en = pl.get("edge_started_at_minute"), pl.get("edge_expected_end_minute")
        if isinstance(now, int) and isinstance(st, int) and isinstance(en, int):
            transit = (pl.get("from_place"), pl.get("to_place"), now - st, en - st)
    knowledge = _sp.actor_knowledge(world, pc_actor_id)
    order = {p: i for i, p in enumerate(definition.get("place_order") or sorted(definition.get("place_definitions") or {}))}
    known = set()
    for pid, rec in (knowledge.get("places") or {}).items():
        if isinstance(rec, dict):                         # 존재를 아는 곳(이름을 모르면 표시명으로 — H07 §5)
            known.add(pid)
    if here:
        known.add(here)
    if transit:
        known |= {transit[0], transit[1]}                 # 지금 지나는 구간의 양 끝은 안다
    known &= set(definition.get("place_definitions") or {})
    places = tuple((f"P{n}", pid, _place_label(definition, pid, knowledge))
                   for n, pid in enumerate(sorted(known, key=lambda p: (order.get(p, 10 ** 9), p)), 1))
    routes = tuple((f"R{n}", tuple(o["signature"]), o["label"], o.get("minutes"))
                   for n, o in enumerate(route_options, 1))
    access = tuple((f"K{n}", o["action_id"], o["label"]) for n, o in enumerate(access_options, 1))
    cs = world.domain("cognition_state") if isinstance(world, _wm.ProjectedWorld) else getattr(world, "cognition_state", {})
    recs = (cs or {}).get(_cog.actor_holder(pc_actor_id)) or {}
    infos = tuple((f"I{n}", iid, _info_label(definition, world, iid))
                  for n, iid in enumerate(sorted(i for i, r in recs.items()
                                                 if isinstance(r, dict) and r.get("awareness") == _cog.CONTENT), 1))
    cal = definition.get("calendar") or {}
    dayparts = tuple(cal.get("daypart_order") or sorted(cal.get("dayparts") or {}))
    from . import events as _ev
    resolutions = tuple((f"D{n}", (iid, oid), label)
                        for n, (iid, oid, label) in enumerate(_ev.open_resolutions(definition, world, here), 1))
    return ActionReferenceManifest(
        world_version_id=world_version_id(world), definition_revision=definition_revision,
        pc_actor_id=pc_actor_id, origin_place=here, places=places, routes=routes, access=access,
        infos=infos, dayparts=dayparts, transit=transit, resolutions=resolutions)


# ══════════════════════════════════════════════════════════════
#  ResolvedActionIntent
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class MovementIntent:
    kind: str
    destination_place: str | None = None


@dataclass(frozen=True)
class TimeAdvanceIntent:
    kind: str
    duration_minutes: int | None = None
    target_daypart: str | None = None
    target_hhmm: str | None = None
    activity: str = "WAIT"


@dataclass(frozen=True)
class CognitionStanceIntent:
    info_id: str
    belief: str | None          # None = CLEAR


@dataclass(frozen=True)
class ResolvedActionIntent:
    movement: MovementIntent | None = None
    time_advance: TimeAdvanceIntent | None = None
    stance: CognitionStanceIntent | None = None
    resolution: tuple | None = None               # H07: (instance_id, option_id) — 플레이어 명시 결단
    route_signature: tuple | None = None          # 안정 의미 선택(구간 id 서명)
    access_action_ids: tuple = ()                 # H04 access_action_id(확인 토큰)
    action_minutes: int | None = None             # P1(D1): 이동·대기 아닌 행동 소요 분(묘사 전 코드 진행) — None = 미제시
    unresolved: tuple = ()                        # (field, code)
    stale: bool = False
    definition_revision: str | None = None        # 해석한 매니페스트의 revision(런타임 결속 — 의미 기록 아님)

    @property
    def empty(self) -> bool:
        return not (self.movement and self.movement.kind == MOVE_DESTINATION) and self.time_advance is None \
            and self.stance is None and self.resolution is None and not self.action_minutes

    @classmethod
    def from_semantic(cls, record: dict, *, definition_revision: str | None) -> "ResolvedActionIntent":
        """H06-E: 이력의 의미 기록 → 의도(재서술 전용). transient 후보 키·매니페스트 없이 같은 선택을 재현한다.

        경로 선택(구간 서명)·접근 확인 토큰을 그대로 보존한다 — 재서술은 경로·접근을 다시 묻지 않는다.
        """
        r = record if isinstance(record, dict) else {}
        mv = r.get("movement")
        ta = r.get("time_advance")
        st = r.get("stance")
        return cls(
            movement=MovementIntent(mv["kind"], mv.get("destination_place")) if isinstance(mv, dict) else None,
            time_advance=TimeAdvanceIntent(**ta) if isinstance(ta, dict) else None,
            stance=CognitionStanceIntent(st["info_id"], st.get("belief")) if isinstance(st, dict) else None,
            resolution=tuple(r["resolution"]) if r.get("resolution") else None,
            route_signature=tuple(r["route_signature"]) if r.get("route_signature") else None,
            access_action_ids=tuple(r.get("access_action_ids") or ()),
            action_minutes=r.get("action_minutes") if isinstance(r.get("action_minutes"), int) else None,
            definition_revision=definition_revision)

    def semantic_record(self) -> dict:
        """영속 가능한 의미 기록(transient 키 없음) — H06 재서술이 재사용한다."""
        return {"movement": None if self.movement is None else
                {"kind": self.movement.kind, "destination_place": self.movement.destination_place},
                "time_advance": None if self.time_advance is None else dict(self.time_advance.__dict__),
                "stance": None if self.stance is None else dict(self.stance.__dict__),
                "resolution": list(self.resolution) if self.resolution else None,
                "route_signature": list(self.route_signature) if self.route_signature else None,
                "access_action_ids": list(self.access_action_ids),
                "action_minutes": self.action_minutes}


def _s(v):
    return v if isinstance(v, str) and v and v != NONE else None


def resolve_intent(manifest: ActionReferenceManifest, raw, *, world=None) -> ResolvedActionIntent:
    """판단 결과의 action_intent → 검증된 의도. 후보 밖 키는 그 부분만 unresolved."""
    rev = manifest.definition_revision
    if world is not None and (world_version_id(world) != manifest.world_version_id
                              or is_definition_stale(rev, world)):
        return ResolvedActionIntent(stale=True, unresolved=(("manifest", "STALE"),), definition_revision=rev)
    if not isinstance(raw, dict):
        return ResolvedActionIntent(definition_revision=rev)
    bad = []
    movement = None
    mk = raw.get("movement_kind") or NONE
    if mk not in MOVEMENT_KINDS:
        bad.append(("movement_kind", "INVALID"))
    elif mk == MOVE_DESTINATION:
        key = _s(raw.get("destination"))
        pid = manifest.place_of(key) if key else None
        if pid is None:
            bad.append(("destination", "INVALID_CANDIDATE" if key else "MISSING"))
        else:
            movement = MovementIntent(MOVE_DESTINATION, pid)
    elif mk in (MOVE_LOCAL, MOVE_EXPLORE):
        movement = MovementIntent(mk)              # 정본 장소 불변(LOCAL)·EXPLORE는 미구조화
        if mk == MOVE_EXPLORE:
            bad.append(("movement_kind", "EXPLORE_UNSTRUCTURED"))
    route_sig = None
    rk = _s(raw.get("route_choice"))
    if rk:
        route_sig = manifest.route_of(rk)
        if route_sig is None:
            bad.append(("route_choice", "INVALID_CANDIDATE"))
    access_ids = ()
    ak = _s(raw.get("access_confirm"))
    if ak:
        aid = manifest.access_of(ak)
        if aid is None:
            bad.append(("access_confirm", "INVALID_CANDIDATE"))
        else:
            access_ids = (aid,)
    time_adv = None
    tk = raw.get("time_kind") or NONE
    activity = raw.get("activity") if raw.get("activity") in ACTIVITIES else "WAIT"
    if tk not in TIME_KINDS:
        bad.append(("time_kind", "INVALID"))
    elif tk == TIME_DURATION:
        d = raw.get("duration_minutes")
        if isinstance(d, int) and not isinstance(d, bool) and 0 < d <= MAX_DURATION_MINUTES:
            time_adv = TimeAdvanceIntent(TIME_DURATION, duration_minutes=d, activity=activity)
        else:
            bad.append(("duration_minutes", "INVALID"))
    elif tk == TIME_DAYPART:
        dp = _s(raw.get("target_daypart"))
        if dp in manifest.dayparts:
            time_adv = TimeAdvanceIntent(TIME_DAYPART, target_daypart=dp, activity=activity)
        else:
            bad.append(("target_daypart", "INVALID_CANDIDATE"))
    elif tk == TIME_CLOCK:
        hh = raw.get("target_hhmm")
        try:
            _clock.parse_hhmm(hh)
            time_adv = TimeAdvanceIntent(TIME_CLOCK, target_hhmm=hh, activity=activity)
        except Exception:
            bad.append(("target_hhmm", "INVALID"))
    stance = None
    sk, sb = _s(raw.get("stance_info")), _s(raw.get("stance_belief"))
    if sk or sb:
        iid = manifest.info_of(sk) if sk else None
        if iid is None or sb not in STANCE_BELIEFS:
            bad.append(("stance", "INVALID_CANDIDATE"))
        else:
            stance = CognitionStanceIntent(iid, None if sb == STANCE_CLEAR else sb)
    resolution = None
    rk = _s(raw.get("resolution"))
    if rk:
        resolution = manifest.resolution_of(rk)
        if resolution is None:
            bad.append(("resolution", "INVALID_CANDIDATE"))
    # P1(D1): 행동 소요 시간 제안 — 정수만, 상한 초과는 상한으로(근거 없는 장시간 강제 방지), 그 밖은 미제시
    action_minutes = None
    am = raw.get(ACTION_MINUTES_FIELD)
    if isinstance(am, int) and not isinstance(am, bool) and am >= 0:
        action_minutes = min(am, MAX_ACTION_MINUTES)
        if am > MAX_ACTION_MINUTES:
            bad.append((ACTION_MINUTES_FIELD, "CLAMPED"))
    else:
        bad.append((ACTION_MINUTES_FIELD, "MISSING" if am is None else "INVALID"))
    return ResolvedActionIntent(movement=movement, time_advance=time_adv, stance=stance, resolution=resolution,
                                route_signature=route_sig, access_action_ids=access_ids,
                                action_minutes=action_minutes,
                                unresolved=tuple(bad), definition_revision=rev)


def target_minute(calendar: dict, now: int, intent: TimeAdvanceIntent) -> int:
    """시간 의도 → 정본 목표 분(항상 now 이후). 시간대는 다음 시작 경계, 시각은 다음 해당 시각."""
    if intent.kind == TIME_DURATION:
        return _clock.add_minutes(now, intent.duration_minutes)
    if intent.kind == TIME_DAYPART:
        start = _clock.parse_hhmm(calendar["dayparts"][intent.target_daypart]["start"])
    elif intent.kind == TIME_CLOCK:
        start = _clock.parse_hhmm(intent.target_hhmm)
    else:
        raise ValueError(f"시간 의도 아님: {intent.kind}")
    base = now - now % _clock.MINUTES_PER_DAY + start
    return base if base > now else base + _clock.MINUTES_PER_DAY
