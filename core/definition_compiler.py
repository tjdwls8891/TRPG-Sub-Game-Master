# 세계 정의 컴파일러 — capability 감지·저작 sugar 정규화·안정 ID·검증 (H01)
#
# [위치]
#   raw scenario JSON → 컴파일된 세계 정의(WorldDefinitionSet) → revision.
#   DESIGN 08 §6 파이프라인: parse → capability detect → sugar normalize →
#   stable ID → refs → validation → canonical serialization → revision hash.
#
# [H01 범위 — production 미배선]
#   이 모듈은 아직 어떤 플레이 경로에서도 호출되지 않는다. 현행 런타임은 여전히
#   core.io.load_scenario_from_file 이 읽은 raw scenario_data 를 직접 쓴다.
#   세션 바인딩·재시작 권위 전환은 H10, 새 capability 엔진은 H04~H09 몫이다.
#   진입점은 운영자 도구 tools/scenario_check.py 다.
#
# [하지 않는 것]
#   · 원본 시나리오 파일을 고치지 않는다(읽기 전용, silent fix 금지 — DESIGN 08 §50).
#   · 산문에서 기계 진실을 추론하지 않는다 — 세계관의 "약 185km"를 거리로,
#     NPC 설명의 "주로 항구에 있음"을 현재 위치로 만들지 않는다(DESIGN 08 §10).
#   · connected 비대칭을 자동 역방향 보정하지 않는다. 보고만 한다(§15).
#   · connected 를 이동 구간(edge)으로 승격하지 않는다. 구간은 거리 저작이 필요하다.
#   · places[*].npcs 를 현재 위치로 읽지 않는다. 연고·선호 연관일 뿐이다(BLK-06).
#   · provider 호출·세션 상태 변경·파일 쓰기를 하지 않는다(순수 함수).
#
# [legacy]
#   scenario_schema 메타데이터가 없는 시나리오는 legacy capability set(빈 집합)으로
#   통과한다. 안전한 sugar(안정 ActorId·NPC 인라인 비밀·퀘스트 hidden)는 legacy
#   에서도 컴파일되지만 런타임 효과는 없다(DESIGN 08 §27~29).
import copy
import json
import os
from dataclasses import dataclass, field

from .world_ids import (
    CanonicalSerializationError,
    authored_npc_actor_id,
    canonical_json,
    content_digest,
    is_valid_explicit_id,
    movement_edge_pair_id,
    npc_inline_secret_info_id,
    order_digest,
    quest_hidden_info_id,
    sha256_hex,
)

# 컴파일러 규칙 버전. 같은 입력의 컴파일 출력이 바뀌는 변경을 하면 올린다.
# 시나리오 스키마 버전·세션 SCHEMA_VERSION·정의 revision과는 별개 의미다(DESIGN 08 §94).
# v2(H03): quantitative_time(달력)·structured_places(이동 구간·이동 방식·접근) 도메인 검증.
# v3(H04): 행위자 profile_base·명시 정보 registry·인라인 info_ref·정보 계열(H04-A),
#          사건 템플릿·조건·진행 검증·접근 override 안정 variant id(H04-B).
COMPILER_VERSION = 3

DEFINITION_FORMAT = "indaim.world_definition"
DEFINITION_FORMAT_VERSION = 1

# 시나리오 정의 스키마 메타데이터 키. 세션 데이터 스키마(core.io.SCHEMA_VERSION)와
# 섞이지 않게 이름을 분리했다(DESIGN 08 §93).
SCENARIO_SCHEMA_KEY = "scenario_schema"
SUPPORTED_SCENARIO_SCHEMA_VERSION = 1

# 이 컴파일러가 인식하는 capability와 최고 버전(DESIGN 08 §2).
KNOWN_CAPABILITIES = {
    "structured_places": 1,
    "quantitative_time": 1,
    "stable_actors": 1,
    "dynamic_actor_location": 1,
    "information_registry": 1,
    "actor_cognition": 1,
    "dynamic_events": 1,
    "scheduled_world_progression": 1,
}

# 이 컴파일러 버전이 도메인 검증까지 끝내는 capability. 나머지는 선언을 기록하되
# WARNING(CAP_VALIDATION_DEFERRED)으로 표시한다 — 해당 WP가 검증기를 넣을 때 옮긴다.
VALIDATED_CAPABILITIES = frozenset({"stable_actors", "quantitative_time", "structured_places",
                                    "information_registry", "actor_cognition", "dynamic_actor_location"})

# capability 의존성(DESIGN 08 §4). (요구 capability, 심각도)
# dynamic_actor_location → structured_places 는 사용자 결정(H01 보정)으로 ERROR.
CAPABILITY_DEPENDENCIES = {
    "scheduled_world_progression": (("quantitative_time", "ERROR"),),
    "actor_cognition": (("stable_actors", "ERROR"), ("information_registry", "ERROR")),
    "dynamic_events": (("stable_actors", "ERROR"),),
    "dynamic_actor_location": (("structured_places", "ERROR"),),
}

# 새 스키마용으로 예약된 키. 이 컴파일러가 구조화하지 않으므로, 있으면 경고한다
# (v2/H03: calendar·movement_modes·access_control_definitions·spatial_knowledge_seeds는 컴파일 대상)
# (legacy_source 에 원문으로 남지만 어떤 엔진도 읽지 않는다는 뜻).
RESERVED_UNCOMPILED_KEYS = (
    "event_templates", "visibility_relations", "initialization_definitions",
)

# NPC 인라인 비밀 필드(npc_template.info_fields의 '비밀').
INLINE_SECRET_FIELD = "비밀"
# profile_base에서 빼는 필드: 구조화된 별도 권위(actor_id·name·비밀) + 레거시 런타임 초기값.
PROFILE_BASE_EXCLUDED = ("actor_id", "name", INLINE_SECRET_FIELD, "resources", "statuses")
INFO_EXPLICIT_KEYS = ("proposition", "summary", "subject_refs", "tags", "fragments", "sensitivity")

ERROR, WARNING, INFO = "ERROR", "WARNING", "INFO"
_SEVERITY_RANK = {ERROR: 0, WARNING: 1, INFO: 2}


class DefinitionCompileError(RuntimeError):
    """ERROR 진단이 있는 결과에서 정의를 요구했을 때."""


class ScenarioSourceError(RuntimeError):
    """시나리오 원본을 읽거나 해석할 수 없을 때."""


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "path": self.path, "message": self.message}


class _Diags:
    def __init__(self):
        self.items = []

    def add(self, severity, code, path, message):
        self.items.append(Diagnostic(severity, code, path, message))

    def sorted(self) -> tuple:
        # 심각도 순, 같은 심각도 안에서는 발생 순(원본 순회 순서 = 결정적).
        return tuple(d for _, d in sorted(enumerate(self.items),
                                          key=lambda t: (_SEVERITY_RANK[t[1].severity], t[0])))


@dataclass(frozen=True)
class ScenarioSources:
    """컴파일 입력. provenance는 정의 밖(스냅샷 봉투)에 둔다 — revision에 넣지 않는다."""
    scenario_id: str
    raw: dict
    quests: dict | None
    provenance: dict


@dataclass(frozen=True)
class CompileResult:
    scenario_id: str
    compiler_version: int
    definition: dict | None
    revision: str | None
    diagnostics: tuple = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.definition is not None

    def by_severity(self, severity: str) -> list:
        return [d for d in self.diagnostics if d.severity == severity]

    @property
    def errors(self) -> list:
        return self.by_severity(ERROR)

    @property
    def warnings(self) -> list:
        return self.by_severity(WARNING)

    def require_definition(self) -> dict:
        if self.definition is None:
            raise DefinitionCompileError(
                f"{self.scenario_id}: 컴파일 ERROR {len(self.errors)}건 — 정의를 만들 수 없습니다")
        return self.definition


# ── 원본 로드 (읽기 전용) ──────────────────────────────────────

def _reject_constant(name):
    raise ScenarioSourceError(f"JSON에 비표준 상수 {name}가 있습니다")


def _read_json_bytes(path: str):
    with open(path, "rb") as f:
        data = f.read()
    try:
        obj = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ScenarioSourceError(f"{path}: JSON 해석 실패 — {e}") from e
    return obj, sha256_hex(data)


QUEST_SIDECAR_SUFFIX = ".quests.json"


def load_scenario_sources_from_path(path: str) -> ScenarioSources:
    """시나리오 파일 경로에서 원본과 퀘스트 부속 파일을 읽는다. 아무것도 쓰지 않는다."""
    path = os.fspath(path)
    base = os.path.basename(path)
    if base.endswith(QUEST_SIDECAR_SUFFIX):
        raise ScenarioSourceError(f"{path}: 퀘스트 부속 파일은 시나리오가 아닙니다")
    if not base.endswith(".json"):
        raise ScenarioSourceError(f"{path}: .json 시나리오 파일이 아닙니다")
    scenario_id = base[:-len(".json")]
    if not os.path.isfile(path):
        raise ScenarioSourceError(f"{path}: 파일이 없습니다")
    raw, raw_hash = _read_json_bytes(path)
    if not isinstance(raw, dict):
        raise ScenarioSourceError(f"{path}: 최상위가 객체가 아닙니다")
    provenance = {"scenario": {"file": base, "sha256": raw_hash}, "quests": None}
    quests = None
    qpath = os.path.join(os.path.dirname(path), scenario_id + QUEST_SIDECAR_SUFFIX)
    if os.path.isfile(qpath):
        quests, q_hash = _read_json_bytes(qpath)
        provenance["quests"] = {"file": os.path.basename(qpath), "sha256": q_hash}
    return ScenarioSources(scenario_id, raw, quests, provenance)


def load_scenario_sources(scenario_id: str, scenarios_dir: str = "scenarios") -> ScenarioSources:
    return load_scenario_sources_from_path(os.path.join(scenarios_dir, f"{scenario_id}.json"))


# ── capability ────────────────────────────────────────────────

def _is_pos_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def detect_capabilities(raw: dict, diags: _Diags | None = None) -> dict:
    """capability 선언을 해석한다. 메타데이터가 없으면 legacy(빈 집합).

    반환: {"mode": "legacy"|"declared", "scenario_schema_version": int|None,
           "capabilities": {name: version}, "validated": [...], "deferred": [...]}
    """
    d = diags or _Diags()
    meta = raw.get(SCENARIO_SCHEMA_KEY, None) if isinstance(raw, dict) else None
    if meta is None:
        return {"mode": "legacy", "scenario_schema_version": None,
                "capabilities": {}, "validated": [], "deferred": []}
    p = SCENARIO_SCHEMA_KEY
    caps = {}
    version = None
    if not isinstance(meta, dict):
        d.add(ERROR, "SCHEMA_META_INVALID", p, "scenario_schema는 객체여야 합니다")
    else:
        version = meta.get("version")
        if not _is_pos_int(version):
            d.add(ERROR, "SCHEMA_VERSION_INVALID", f"{p}.version", "양의 정수 버전이 필요합니다")
            version = None
        elif version > SUPPORTED_SCENARIO_SCHEMA_VERSION:
            d.add(ERROR, "SCHEMA_VERSION_UNSUPPORTED", f"{p}.version",
                  f"시나리오 스키마 v{version}은 이 컴파일러(v{COMPILER_VERSION})가 지원하는 "
                  f"v{SUPPORTED_SCENARIO_SCHEMA_VERSION}보다 새롭습니다")
        for extra in meta:
            if extra not in ("version", "capabilities"):
                d.add(WARNING, "SCHEMA_META_UNKNOWN_KEY", f"{p}.{extra}", "알 수 없는 메타데이터 키")
        declared = meta.get("capabilities", {})
        if not isinstance(declared, dict):
            d.add(ERROR, "CAP_MAP_INVALID", f"{p}.capabilities", "capabilities는 {이름: 버전} 객체여야 합니다")
            declared = {}
        for name, ver in declared.items():
            cp = f"{p}.capabilities.{name}"
            if name not in KNOWN_CAPABILITIES:
                d.add(ERROR, "CAP_UNKNOWN", cp, f"알 수 없는 capability '{name}'")
                continue
            if not _is_pos_int(ver):
                d.add(ERROR, "CAP_VERSION_INVALID", cp, "capability 버전은 양의 정수여야 합니다")
                continue
            if ver > KNOWN_CAPABILITIES[name]:
                d.add(ERROR, "CAP_VERSION_UNSUPPORTED", cp,
                      f"{name} v{ver}은 지원 최고 v{KNOWN_CAPABILITIES[name]}보다 새롭습니다")
                continue
            caps[name] = ver
        for name in caps:
            for req, sev in CAPABILITY_DEPENDENCIES.get(name, ()):
                if req not in caps:
                    d.add(sev, "CAP_DEPENDENCY_MISSING", f"{p}.capabilities.{name}",
                          f"{name}에는 {req} capability가 필요합니다")
    validated = sorted(n for n in caps if n in VALIDATED_CAPABILITIES)
    deferred = sorted(n for n in caps if n not in VALIDATED_CAPABILITIES)
    for name in deferred:
        d.add(WARNING, "CAP_VALIDATION_DEFERRED", f"{p}.capabilities.{name}",
              f"컴파일러 v{COMPILER_VERSION}은 {name} 도메인 검증을 아직 하지 않습니다 — "
              "선언은 기록되지만 런타임 활성화 근거가 될 수 없습니다")
    return {"mode": "declared", "scenario_schema_version": version,
            "capabilities": caps, "validated": validated, "deferred": deferred}


# ── 인물 ──────────────────────────────────────────────────────

def _compile_actors(scenario_id, raw, d):
    npcs = raw.get("default_npcs")
    defs, order, index = {}, [], {}
    if npcs is None:
        return defs, order, index
    if not isinstance(npcs, dict):
        d.add(ERROR, "NPCS_INVALID", "default_npcs", "default_npcs는 {이름: 인물상} 객체여야 합니다")
        return defs, order, index
    for key, prof in npcs.items():
        path = f"default_npcs.{key}"
        if not isinstance(prof, dict):
            d.add(ERROR, "NPC_INVALID", path, "인물상은 객체여야 합니다")
            continue
        explicit = prof.get("actor_id")
        if explicit is not None and not is_valid_explicit_id(explicit):
            d.add(ERROR, "ACTOR_ID_INVALID", f"{path}.actor_id",
                  "명시 actor_id 형식이 잘못됐습니다(영숫자 시작, [A-Za-z0-9_.:-] 64자 이내)")
            continue
        actor_id = authored_npc_actor_id(scenario_id, key, explicit)
        if actor_id in defs:
            d.add(ERROR, "ACTOR_ID_DUPLICATE", path,
                  f"ActorId '{actor_id}'가 '{defs[actor_id]['legacy_key']}'와 겹칩니다")
            continue
        name = prof.get("name")
        if name is not None and name != key:
            d.add(INFO, "NPC_NAME_KEY_MISMATCH", f"{path}.name",
                  f"표시 이름 '{name}'이 사전 키와 다릅니다(레거시 이름키는 사전 키)")
        secret = prof.get(INLINE_SECRET_FIELD)
        defs[actor_id] = {
            "actor_id": actor_id,
            "kind": "NPC",
            "source_kind": "AUTHORED_NPC",
            "legacy_key": key,
            "display_name": name if isinstance(name, str) and name else key,
            "explicit_id": explicit is not None,
            "has_inline_secret": (isinstance(secret, str) and bool(secret.strip())) or isinstance(secret, dict),
            "secret_info_id": None,          # _compile_inline_secrets가 채운다(생성 id 또는 info_ref)
            # v3: 새 엔진(CurrentNpcProjection)이 legacy_source를 읽지 않도록 정제된 저작 프로필.
            # 별도 권위가 있는 필드(정체성·비밀)와 레거시 런타임 초기값(resources·statuses)은 제외한다.
            "profile_base": {k: copy.deepcopy(v) for k, v in prof.items() if k not in PROFILE_BASE_EXCLUDED},
            "source_ref": ["default_npcs", key],
        }
        order.append(actor_id)
        index[key] = actor_id
    return defs, order, index


def _info_record(info_id, kind, proposition, source_ref, *, summary=None, subject_refs=(), tags=(),
                 fragments=(), sensitivity=None, **extra):
    rec = {"info_id": info_id, "kind": kind, "proposition": proposition, "summary": summary,
           "subject_refs": list(subject_refs), "tags": list(tags), "fragments": list(fragments),
           "sensitivity": sensitivity, "source_ref": source_ref}
    rec.update(extra)
    return rec


def _compile_inline_secrets(raw, actors, d, explicit_infos=None):
    """NPC '비밀' sugar. 문자열 → 구조 위치 기반 생성 InfoId, {info_ref: id} → 명시 정보 참조(v3)."""
    out = {}
    explicit_infos = explicit_infos or {}
    npcs = raw.get("default_npcs") if isinstance(raw.get("default_npcs"), dict) else {}
    for actor_id, a in actors.items():
        secret = npcs.get(a["legacy_key"], {}).get(INLINE_SECRET_FIELD)
        path = f"default_npcs.{a['legacy_key']}.{INLINE_SECRET_FIELD}"
        if secret is None:
            continue
        if isinstance(secret, dict):
            ref = secret.get("info_ref")
            if set(secret) != {"info_ref"} or not isinstance(ref, str):
                d.add(ERROR, "NPC_SECRET_REF_INVALID", path, "비밀 객체는 {\"info_ref\": <InfoId>}만 허용합니다")
            elif ref not in explicit_infos:
                d.add(ERROR, "NPC_SECRET_REF_UNKNOWN", path, f"info_ref {ref!r}가 information_definitions에 없습니다")
            else:
                a["secret_info_id"] = ref
            continue
        if not isinstance(secret, str):
            d.add(WARNING, "NPC_SECRET_NOT_TEXT", path, "비밀 필드가 문자열이 아니어서 컴파일하지 않습니다")
            continue
        if not secret.strip():
            continue
        info_id = npc_inline_secret_info_id(actor_id)
        a["secret_info_id"] = info_id
        out[info_id] = _info_record(info_id, "NPC_INLINE_SECRET", secret,
                                    ["default_npcs", a["legacy_key"], INLINE_SECRET_FIELD],
                                    subject_refs=[f"actor:{actor_id}"], owner_actor_id=actor_id)
    return out


def _compile_explicit_infos(raw, actors, places, d):
    """top-level information_definitions {InfoId: {proposition, summary?, subject_refs?, tags?,
    fragments?: [{fragment_id, proposition}], sensitivity?}} (v3)."""
    src = raw.get("information_definitions")
    out = {}
    if src is None:
        return out
    if not isinstance(src, dict):
        d.add(ERROR, "INFO_DEFINITIONS_INVALID", "information_definitions", "{InfoId: 정의} 객체여야 합니다")
        return out
    for iid, body in src.items():
        p = f"information_definitions.{iid}"
        if not is_valid_explicit_id(iid):
            d.add(ERROR, "INFO_ID_INVALID", p, "InfoId는 명시 id 형식이어야 합니다")
            continue
        if not isinstance(body, dict) or not isinstance(body.get("proposition"), str) \
                or not body["proposition"].strip():
            d.add(ERROR, "INFO_PROPOSITION_REQUIRED", p, "proposition(비어 있지 않은 문자열)이 필요합니다")
            continue
        for k in body:
            if k not in INFO_EXPLICIT_KEYS:
                d.add(WARNING, "INFO_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 정보 키(무시)")
        ok = True
        refs = body.get("subject_refs") or []
        if not isinstance(refs, list):
            d.add(ERROR, "INFO_SUBJECT_REFS_INVALID", f"{p}.subject_refs", "목록이어야 합니다")
            ok, refs = False, []
        for r in refs:
            kind, _, ident = (r.partition(":") if isinstance(r, str) else ("", "", ""))
            pool = {"actor": actors, "place": places}.get(kind)
            if pool is None or ident not in pool:
                d.add(ERROR, "INFO_SUBJECT_REF_UNKNOWN", f"{p}.subject_refs",
                      f"subject_ref {r!r}는 'actor:<ActorId>'·'place:<PlaceId>' 실재 참조여야 합니다")
                ok = False
        tags = _str_list(body.get("tags"))
        if tags is None or isinstance(body.get("tags"), str):
            d.add(ERROR, "INFO_TAGS_INVALID", f"{p}.tags", "문자열 목록이어야 합니다")
            ok, tags = False, []
        frags, seen = [], set()
        for j, f in enumerate(body.get("fragments") or []):
            if not isinstance(f, dict) or set(f) != {"fragment_id", "proposition"} \
                    or not is_valid_explicit_id(f.get("fragment_id")) or not isinstance(f.get("proposition"), str):
                d.add(ERROR, "INFO_FRAGMENT_INVALID", f"{p}.fragments[{j}]",
                      "fragment는 {fragment_id(명시 id), proposition}")
                ok = False
                continue
            if f["fragment_id"] in seen:
                d.add(ERROR, "INFO_FRAGMENT_DUPLICATE", f"{p}.fragments[{j}]", f"fragment_id {f['fragment_id']} 중복")
                ok = False
                continue
            seen.add(f["fragment_id"])
            frags.append(dict(f))
        sens = body.get("sensitivity")
        if sens is not None and not isinstance(sens, str):
            d.add(ERROR, "INFO_SENSITIVITY_INVALID", f"{p}.sensitivity", "문자열 또는 null")
            ok = False
        if ok:
            out[iid] = _info_record(iid, "EXPLICIT", body["proposition"], ["information_definitions", iid],
                                    summary=body.get("summary"), subject_refs=refs, tags=sorted(set(tags)),
                                    fragments=frags, sensitivity=sens)
    return out


_SERIES_KEYS = ("value_ref", "proposition_template", "tracking_policy", "subject_refs", "tags", "sensitivity")
SERIES_TRACKING_POLICIES = ("ON_CHANGE",)


def _ref_scope(actors, places, infos, access, modes, *, slots=None, allow_self=False, templates=(), calendar=None):
    from . import world_refs as _refs
    return _refs.RefScope(actors=set(actors), places=set(places), infos=set(infos),
                          controls=set((access or {}).get("controls", {})), modes=set(modes),
                          slots=dict(slots or {}), allow_self=allow_self, templates=set(templates),
                          has_calendar=calendar is not None)


def _compile_info_series(raw, scope, d):
    """information_series {SeriesId: {value_ref, proposition_template('{value}' 포함), tracking_policy?,
    subject_refs?, tags?, sensitivity?}} (v3). 초기 상태 적용은 H06."""
    from . import world_refs as _refs
    src = raw.get("information_series")
    out = {}
    if src is None:
        return out
    if not isinstance(src, dict):
        d.add(ERROR, "INFO_SERIES_INVALID", "information_series", "{SeriesId: 정의} 객체여야 합니다")
        return out
    for sid, body in src.items():
        p = f"information_series.{sid}"
        if not is_valid_explicit_id(sid) or not isinstance(body, dict):
            d.add(ERROR, "INFO_SERIES_INVALID", p, "SeriesId는 명시 id, 정의는 객체")
            continue
        for k in body:
            if k not in _SERIES_KEYS:
                d.add(WARNING, "INFO_SERIES_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 계열 키(무시)")
        errs = _refs.validate_ref(body.get("value_ref"), scope, f"{p}.value_ref")
        if body.get("value_ref", {}).get("ref") in ("CLOCK_MINUTE", "OBSERVER_ACTIVITY", "SCENE_PLACE") \
                if isinstance(body.get("value_ref"), dict) else False:
            errs.append((f"{p}.value_ref", "계열 값은 추적 가능한 정본 사실이어야 합니다(시간·관찰 문맥 불가)"))
        for ep, msg in errs:
            d.add(ERROR, "INFO_SERIES_VALUE_REF_INVALID", ep, msg)
        tpl = body.get("proposition_template")
        if not isinstance(tpl, str) or "{value}" not in tpl:
            d.add(ERROR, "INFO_SERIES_TEMPLATE_INVALID", f"{p}.proposition_template",
                  "proposition_template은 '{value}'를 포함한 문자열이어야 합니다")
            errs.append(None)
        pol = body.get("tracking_policy", "ON_CHANGE")
        if pol not in SERIES_TRACKING_POLICIES:
            d.add(ERROR, "INFO_SERIES_POLICY_INVALID", f"{p}.tracking_policy",
                  f"tracking_policy는 {list(SERIES_TRACKING_POLICIES)}")
            errs.append(None)
        refs = body.get("subject_refs") or []
        tags = _str_list(body.get("tags"))
        if not isinstance(refs, list) or tags is None:
            d.add(ERROR, "INFO_SERIES_INVALID", p, "subject_refs·tags는 목록")
            errs.append(None)
        if errs:
            continue
        out[sid] = {"series_id": sid, "value_ref": copy.deepcopy(body["value_ref"]),
                    "proposition_template": tpl, "tracking_policy": pol, "subject_refs": list(refs),
                    "tags": sorted(set(tags)), "sensitivity": body.get("sensitivity"),
                    "dependency_keys": sorted(set(_refs.dependency_keys(body["value_ref"]))),
                    "source_ref": ["information_series", sid]}
    return out


# ── 퀘스트 hidden ─────────────────────────────────────────────

def _compile_quest_hidden(scenario_id, quests, places, d):
    out, seen = {}, {}
    if quests is None:
        return out
    if not isinstance(quests, dict) or not isinstance(quests.get("quests"), list):
        d.add(ERROR, "QUESTS_INVALID", "quests", "퀘스트 부속 파일은 {\"quests\": [...]} 형식이어야 합니다")
        return out
    for i, q in enumerate(quests["quests"]):
        path = f"quests[{i}]"
        if not isinstance(q, dict) or not isinstance(q.get("id"), str) or not q.get("id"):
            d.add(ERROR, "QUEST_ID_MISSING", path, "퀘스트 id가 없습니다")
            continue
        # 현행 런타임(quest._find_quest)은 id만으로 첫 항목을 찾는다. 같은 id의 다른
        # version은 도달 불가·모호하므로 정체성 충돌로 막는다. version은 메타데이터다.
        qid = q["id"]
        if qid in seen:
            d.add(ERROR, "QUEST_ID_DUPLICATE", path,
                  f"퀘스트 id '{qid}'가 quests[{seen[qid]}]와 겹칩니다"
                  f"(version {quests['quests'][seen[qid]].get('version')!r} / {q.get('version')!r})")
            continue
        seen[qid] = i
        filters = q.get("filters") if isinstance(q.get("filters"), dict) else {}
        for fk in ("place", "within"):
            f = filters.get(fk)
            vals = f.get("any", []) if isinstance(f, dict) else (f if isinstance(f, list) else [])
            for v in vals:
                if places and v not in places:
                    d.add(WARNING, "QUEST_PLACE_UNKNOWN", f"{path}.filters.{fk}",
                          f"장소 '{v}'가 places에 없습니다")
        hidden = q.get("hidden")
        if not hidden:     # None·{} — 런타임(quest.py)도 이면정보 없음으로 본다
            continue
        if not isinstance(hidden, dict) or not isinstance(hidden.get("truth"), str) \
                or not hidden["truth"].strip():
            d.add(WARNING, "QUEST_HIDDEN_NO_TRUTH", f"{path}.hidden", "hidden.truth가 없어 정보로 컴파일하지 않습니다")
            continue
        info_id = quest_hidden_info_id(scenario_id, q["id"])
        out[info_id] = _info_record(info_id, "QUEST_HIDDEN_TRUTH", hidden["truth"], ["quests", i, "hidden"],
                                    quest_id=q["id"], quest_version=q.get("version"))
    return out


# ── 장소 ──────────────────────────────────────────────────────

def _str_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return list(v)
    return None


def _compile_places(raw, actor_index, d):
    places = raw.get("places")
    defs, order = {}, []
    if places is None:
        return defs, order
    if not isinstance(places, dict):
        d.add(ERROR, "PLACES_INVALID", "places", "places는 {PlaceId: 노드} 객체여야 합니다")
        return defs, order
    for pid, node in places.items():
        path = f"places.{pid}"
        if not isinstance(node, dict):
            d.add(ERROR, "PLACE_INVALID", path, "장소 노드는 객체여야 합니다")
            continue
        rec = {"place_id": pid, "source_ref": ["places", pid]}
        parent = node.get("parent")
        if parent is not None and not isinstance(parent, str):
            d.add(ERROR, "PLACE_PARENT_INVALID", f"{path}.parent", "parent는 PlaceId 문자열이어야 합니다")
            parent = None
        rec["parent"] = parent
        for fk in ("extra_parents", "aliases", "connected", "visible_within", "inherit", "reachable"):
            vals = _str_list(node.get(fk))
            if vals is None:
                d.add(ERROR, "PLACE_FIELD_INVALID", f"{path}.{fk}", f"{fk}는 문자열 목록이어야 합니다")
                vals = []
            rec[fk] = vals
        rec["traversable"] = bool(node.get("traversable"))
        rec["image"] = node.get("image") if isinstance(node.get("image"), str) else None
        # places[*].npcs 는 연고·선호 연관이다. 현재 위치(presence)가 아니다(BLK-06).
        assoc = []
        for entry in (node.get("npcs") or []):
            nm = entry.get("name") if isinstance(entry, dict) else entry
            if not isinstance(nm, str):
                continue
            aid = actor_index.get(nm)
            if aid is None:
                d.add(WARNING, "PLACE_NPC_UNRESOLVED", f"{path}.npcs",
                      f"연관 인물 '{nm}'이 default_npcs에 없습니다")
            assoc.append({"legacy_name": nm, "actor_id": aid})
        rec["npc_associations"] = assoc
        defs[pid] = rec
        order.append(pid)

    # 참조 무결 — 런타임 의미가 정해지지 않는 결손만 ERROR.
    for pid, rec in defs.items():
        path = f"places.{pid}"
        for p in [rec["parent"]] + rec["extra_parents"]:
            if p is not None and p not in defs:
                d.add(ERROR, "PLACE_PARENT_MISSING", path, f"상위 '{p}'가 places에 없습니다")
        for fk, code in (("connected", "PLACE_CONNECTED_MISSING"),
                         ("visible_within", "PLACE_VISIBLE_WITHIN_MISSING"),
                         ("inherit", "PLACE_INHERIT_MISSING"),
                         ("reachable", "PLACE_REACHABLE_MISSING")):
            for ref in rec[fk]:
                if ref not in defs:
                    d.add(WARNING, code, f"{path}.{fk}", f"'{ref}'가 places에 없습니다")
    # parent 순환
    for pid in defs:
        seen, cur = [], pid
        while cur is not None and cur in defs:
            if cur in seen:
                d.add(ERROR, "PLACE_PARENT_CYCLE", f"places.{pid}",
                      "parent 순환: " + " → ".join(seen[seen.index(cur):] + [cur]))
                break
            seen.append(cur)
            cur = defs[cur]["parent"]
    # 별칭 충돌 — 자동 rename 하지 않는다(DESIGN 08 §50).
    owners = {}
    for pid, rec in defs.items():
        for al in rec["aliases"]:
            owners.setdefault(al, []).append(pid)
    for al, ps in owners.items():
        if al in defs:
            d.add(WARNING, "PLACE_ALIAS_SHADOWED", f"places.{ps[0]}.aliases",
                  f"별칭 '{al}'이 다른 장소 키와 같아 해석되지 않습니다(정확 일치가 우선)")
        if len(ps) > 1:
            d.add(WARNING, "PLACE_ALIAS_AMBIGUOUS", f"places.{ps[0]}.aliases",
                  f"별칭 '{al}'을 여러 장소가 공유합니다: {', '.join(ps)}")
    asym = asymmetric_connected(defs)
    if asym:
        d.add(WARNING, "PLACE_CONNECTED_ASYMMETRIC", "places",
              f"한쪽 방향만 적힌 connected {len(asym)}쌍 — 자동 보정하지 않음(--migration-report 참고)")
    return defs, order


def asymmetric_connected(place_defs: dict) -> list:
    """A의 connected에 B가 있는데 B의 connected에 A가 없는 쌍(원본 순서)."""
    out = []
    for a, rec in place_defs.items():
        for b in rec["connected"]:
            if b in place_defs and a not in place_defs[b]["connected"]:
                out.append([a, b])
    return out


# ── 이동 구간 (명시 저작분만) ─────────────────────────────────

_EDGE_CONTROL_KEYS = ("id", "from", "to", "bidirectional", "reverse")


def _compile_edges(scenario_id, raw, place_defs, caps, d, modes=None, access=None):
    """movement_edges 저작 sugar → 방향 구간. 거리·방향 의미는 저작값을 그대로 옮긴다.

    connected 에서 구간을 만들지 않는다. 숫자를 생성하지 않는다(DESIGN 08 §17).
    v2(H03): 방향 구간마다 속성을 검증·정규화한다(_normalize_edge_props). 원 저작
    속성은 properties 에 그대로 남는다.
    """
    edges = raw.get("movement_edges")
    out = {}
    if edges is None:
        return out
    if not isinstance(edges, list):
        d.add(ERROR, "EDGES_INVALID", "movement_edges", "movement_edges는 목록이어야 합니다")
        return out
    if "structured_places" not in caps:
        d.add(WARNING, "EDGES_WITHOUT_CAPABILITY", "movement_edges",
              "structured_places capability 선언 없이 이동 구간이 저작됐습니다")
    pair_seen = {}
    for i, e in enumerate(edges):
        path = f"movement_edges[{i}]"
        if not isinstance(e, dict):
            d.add(ERROR, "EDGE_INVALID", path, "구간은 객체여야 합니다")
            continue
        a, b = e.get("from"), e.get("to")
        if not isinstance(a, str) or not isinstance(b, str):
            d.add(ERROR, "EDGE_ENDPOINT_INVALID", path, "from/to는 PlaceId 문자열이어야 합니다")
            continue
        bad = [x for x in (a, b) if x not in place_defs]
        if bad:
            d.add(ERROR, "EDGE_ENDPOINT_MISSING", path, f"places에 없는 끝점: {', '.join(bad)}")
            continue
        if a == b:
            d.add(ERROR, "EDGE_SELF_LOOP", path, "자기 자신으로 가는 구간")
            continue
        bidir = e.get("bidirectional", False)
        if not isinstance(bidir, bool):
            d.add(ERROR, "EDGE_BIDIRECTIONAL_INVALID", f"{path}.bidirectional", "true/false여야 합니다")
            continue
        reverse = e.get("reverse")
        if reverse is not None and (not bidir or not isinstance(reverse, dict)):
            d.add(ERROR, "EDGE_REVERSE_INVALID", f"{path}.reverse",
                  "reverse 덮어쓰기는 bidirectional 구간의 객체여야 합니다")
            continue
        explicit = e.get("id")
        if explicit is not None:
            if not is_valid_explicit_id(explicit):
                d.add(ERROR, "EDGE_ID_INVALID", f"{path}.id", "명시 id 형식이 잘못됐습니다")
                continue
            pair_id = explicit
        else:
            key = (a, b)
            if key in pair_seen:
                d.add(ERROR, "EDGE_IDENTITY_AMBIGUOUS", path,
                      f"id 없는 {a}→{b} 구간이 movement_edges[{pair_seen[key]}]와 겹칩니다 — 명시 id가 필요합니다")
                continue
            pair_seen[key] = i
            pair_id = movement_edge_pair_id(scenario_id, a, b)
        props = {k: copy.deepcopy(v) for k, v in e.items() if k not in _EDGE_CONTROL_KEYS}
        directed = [(f"{pair_id}#fwd", a, b, props)]
        if bidir:
            rprops = copy.deepcopy(props)
            rprops.update(copy.deepcopy(reverse or {}))
            directed.append((f"{pair_id}#rev", b, a, rprops))
        for eid, f, t, pr in directed:
            if eid in out:
                d.add(ERROR, "EDGE_ID_DUPLICATE", path, f"구간 id '{eid}'가 겹칩니다")
                continue
            rec = {"edge_id": eid, "pair_id": pair_id, "from": f, "to": t,
                   "authored_bidirectional": bidir, "properties": pr,
                   "source_ref": ["movement_edges", i]}
            rec.update(_normalize_edge_props(pr, f"{path}" + ("(rev)" if eid.endswith("#rev") else ""),
                                             caps, modes or {}, access, d))
            out[eid] = rec
    if out and "structured_places" in caps:
        _check_zero_cost_cycles(out, d)
    return out


# ── 구간 속성 · 이동 방식 · 접근 (H03 — structured_places) ────

EDGE_PROPERTY_KEYS = ("distance_m", "movement_tags", "allowed_modes", "base_time_factor",
                      "fixed_delay_minutes", "access_ref", "access_rule_ref", "route_hint",
                      "authored_notes", "knowledge_visibility")
KNOWLEDGE_VISIBILITY = ("PUBLIC", "LOCAL", "RESTRICTED", "SECRET")
DEFAULT_KNOWLEDGE_VISIBILITY = "LOCAL"
FIXED_DECIMAL_PLACES = 6          # 배수·속도의 고정 정밀도(소수 6자리 — 정수 마이크로 단위로 계산)
ACCESS_RESULTS = ("PASSABLE", "ACTION_REQUIRED", "IMPASSABLE")
ACCESS_ACTION_POLICIES = ("IMPLICIT", "CONFIRM", "SEPARATE_ACTION")
ACCESS_RULE_TYPES = ("STATE_TABLE",)


def _is_nonneg_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def fixed_decimal(v):
    """양의 고정 정밀도 십진수 → 정규 문자열('1.2', '80'). 부적합하면 None.

    float 입력도 str()로 십진 표기를 고정한 뒤 해석한다 — 이진 부동소수 오차가
    정의·경로 순위를 정하지 않게 한다.
    """
    from decimal import Decimal, InvalidOperation
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return None
    try:
        dv = Decimal(str(v))
    except InvalidOperation:
        return None
    if not dv.is_finite() or dv <= 0 or dv.as_tuple().exponent < -FIXED_DECIMAL_PLACES:
        return None
    return format(dv.normalize(), "f")


def _normalize_edge_props(pr, path, caps, modes, access, d):
    structured = "structured_places" in caps
    for k in pr:
        if k not in EDGE_PROPERTY_KEYS:
            d.add(WARNING, "EDGE_UNKNOWN_PROPERTY", f"{path}.{k}", "알 수 없는 구간 속성(무시)")
    out = {}
    dist = pr.get("distance_m")
    if dist is None:
        if structured:
            d.add(ERROR, "EDGE_DISTANCE_REQUIRED", path,
                  "structured_places 구간에는 distance_m(정수 m) 저작이 필요합니다 — 추론하지 않습니다")
    elif not _is_nonneg_int(dist):
        d.add(ERROR, "EDGE_DISTANCE_INVALID", f"{path}.distance_m", "distance_m은 0 이상의 정수(m)여야 합니다")
        dist = None
    out["distance_m"] = dist
    tags = _str_list(pr.get("movement_tags"))
    if tags is None or isinstance(pr.get("movement_tags"), str):
        d.add(ERROR, "EDGE_TAGS_INVALID", f"{path}.movement_tags", "movement_tags는 문자열 목록이어야 합니다")
        tags = []
    out["movement_tags"] = sorted(set(tags))
    am = pr.get("allowed_modes")
    if am is None:
        out["allowed_modes"] = None
    else:
        vals = _str_list(am)
        if vals is None or isinstance(am, str):
            d.add(ERROR, "EDGE_ALLOWED_MODES_INVALID", f"{path}.allowed_modes", "allowed_modes는 목록이어야 합니다")
            vals = []
        for m in vals:
            if m not in modes:
                d.add(ERROR, "EDGE_MODE_UNKNOWN", f"{path}.allowed_modes", f"정의되지 않은 이동 방식 '{m}'")
        out["allowed_modes"] = sorted(set(vals))
    f = pr.get("base_time_factor", 1)
    fd = fixed_decimal(f)
    if fd is None:
        d.add(ERROR, "EDGE_FACTOR_INVALID", f"{path}.base_time_factor",
              f"base_time_factor는 0보다 큰 소수 {FIXED_DECIMAL_PLACES}자리 이내 수여야 합니다(시간 배수)")
        fd = "1"
    out["base_time_factor"] = fd
    delay = pr.get("fixed_delay_minutes", 0)
    if not _is_nonneg_int(delay):
        d.add(ERROR, "EDGE_DELAY_INVALID", f"{path}.fixed_delay_minutes", "fixed_delay_minutes는 0 이상의 정수여야 합니다")
        delay = 0
    out["fixed_delay_minutes"] = delay
    controls = (access or {}).get("controls", {})
    rules = (access or {}).get("rules", {})
    aref, rref = pr.get("access_ref"), pr.get("access_rule_ref")
    if aref is not None and (not isinstance(aref, str) or aref not in controls):
        d.add(ERROR, "EDGE_ACCESS_REF_UNKNOWN", f"{path}.access_ref", f"정의되지 않은 접근 제어 {aref!r}")
        aref = None
    if rref is not None:
        if not isinstance(rref, str) or rref not in rules:
            d.add(ERROR, "EDGE_ACCESS_RULE_UNKNOWN", f"{path}.access_rule_ref", f"정의되지 않은 접근 규칙 {rref!r}")
            rref = None
        elif pr.get("access_ref") is None:
            d.add(ERROR, "EDGE_ACCESS_RULE_WITHOUT_CONTROL", f"{path}.access_rule_ref",
                  "access_rule_ref는 access_ref(상태를 가진 접근 제어)와 함께 써야 합니다")
            rref = None
        elif aref is not None:
            need = set(rules[controls[aref]["rule_ref"]]["states"])
            if not need <= set(rules[rref]["states"]):
                d.add(ERROR, "ACCESS_RULE_STATE_MISMATCH", f"{path}.access_rule_ref",
                      f"규칙 '{rref}'이 제어 '{aref}'의 상태 {sorted(need - set(rules[rref]['states']))}를 다루지 않습니다")
                rref = None
    out["access_ref"], out["access_rule_ref"] = aref, rref
    for k, code in (("route_hint", "EDGE_ROUTE_HINT_INVALID"), ("authored_notes", "EDGE_NOTES_INVALID")):
        v = pr.get(k)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            d.add(ERROR, code, f"{path}.{k}", f"{k}는 비어 있지 않은 문자열이어야 합니다")
            v = None
        out[k] = v
    kv = pr.get("knowledge_visibility", DEFAULT_KNOWLEDGE_VISIBILITY)
    if kv not in KNOWLEDGE_VISIBILITY:
        d.add(ERROR, "EDGE_KNOWLEDGE_VISIBILITY_INVALID", f"{path}.knowledge_visibility",
              f"knowledge_visibility는 {list(KNOWLEDGE_VISIBILITY)} 중 하나여야 합니다")
        kv = DEFAULT_KNOWLEDGE_VISIBILITY
    out["knowledge_visibility"] = kv
    return out


def _check_zero_cost_cycles(edges, d):
    """distance 0·지연 0 구간만으로 닫히는 방향 순환(경로 알고리즘은 단순 경로라 안전하지만 저작 오류 신호)."""
    adj = {}
    for eid in sorted(edges):
        e = edges[eid]
        if e.get("distance_m") == 0 and e.get("fixed_delay_minutes") == 0:
            adj.setdefault(e["from"], []).append(e["to"])
    color, found = {}, []

    def visit(u, stack):
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, ()):
            if color.get(v) == 1 and not found:
                found.append(stack[stack.index(v):] + [v])
            elif v not in color:
                visit(v, stack)
        stack.pop()
        color[u] = 2

    for u in sorted(adj):
        if u not in color:
            visit(u, [])
    if found:
        d.add(WARNING, "ZERO_COST_CYCLE", "movement_edges",
              "거리·지연 0 구간만으로 순환합니다: " + " → ".join(found[0]))


_MODE_KEYS = ("base_speed_m_per_min", "allowed_edge_tags", "forbidden_edge_tags")


def _compile_movement_modes(raw, caps, d):
    """raw movement_modes {mode_id: {base_speed_m_per_min, allowed_edge_tags?, forbidden_edge_tags?}}.

    속도 숫자는 저작값만 쓴다 — core에 장르별 보행 속도를 두지 않는다.
    allowed_edge_tags가 있으면 구간 태그가 그중 하나 이상을 가져야 하고,
    forbidden_edge_tags와 하나라도 겹치면 그 방식으로 통과할 수 없다.
    """
    modes = raw.get("movement_modes")
    if modes is None:
        return {}
    if not isinstance(modes, dict):
        d.add(ERROR, "MODES_INVALID", "movement_modes", "movement_modes는 {mode_id: 정의} 객체여야 합니다")
        return {}
    if "structured_places" not in caps:
        d.add(WARNING, "MODES_WITHOUT_CAPABILITY", "movement_modes",
              "structured_places capability 없이 이동 방식이 저작됐습니다")
    out = {}
    for mid, m in modes.items():
        p = f"movement_modes.{mid}"
        if not is_valid_explicit_id(mid):
            d.add(ERROR, "MODE_ID_INVALID", p, "mode_id는 명시 id 형식이어야 합니다")
            continue
        if not isinstance(m, dict):
            d.add(ERROR, "MODE_INVALID", p, "이동 방식은 객체여야 합니다")
            continue
        for k in m:
            if k not in _MODE_KEYS:
                d.add(WARNING, "MODE_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 이동 방식 키(무시)")
        speed = fixed_decimal(m.get("base_speed_m_per_min"))
        if speed is None:
            d.add(ERROR, "MODE_SPEED_INVALID", f"{p}.base_speed_m_per_min",
                  f"base_speed_m_per_min은 0보다 큰 소수 {FIXED_DECIMAL_PLACES}자리 이내 수여야 합니다")
            continue
        rec = {"mode_id": mid, "base_speed_m_per_min": speed}
        bad = False
        for k in ("allowed_edge_tags", "forbidden_edge_tags"):
            v = m.get(k)
            vals = _str_list(v)
            if vals is None or isinstance(v, str):
                d.add(ERROR, "MODE_TAGS_INVALID", f"{p}.{k}", f"{k}는 문자열 목록이어야 합니다")
                bad = True
                continue
            rec[k] = sorted(set(vals)) if (v is not None or k == "forbidden_edge_tags") else None
        if bad:
            continue
        if rec["allowed_edge_tags"] is not None and set(rec["allowed_edge_tags"]) & set(rec["forbidden_edge_tags"]):
            d.add(ERROR, "MODE_TAGS_CONTRADICT", p, "같은 태그가 allowed와 forbidden에 모두 있습니다")
            continue
        out[mid] = rec
    return out


def _compile_access(raw, caps, d):
    """raw access_control_definitions → {"rules": {...}, "controls": {...}}.

    rules:    {rule_id: {"rule_type": "STATE_TABLE", "states": {state: Outcome}}}
              Outcome = {"result", "action_policy"(ACTION_REQUIRED 필수),
                         "action_delay_minutes"?, "actor_overrides"?: [Outcome + "requires_capability"]}
    controls: {control_id: {"rule_ref": rule_id, "initial_state": state}}
    장르별 문 상태를 core에 두지 않는다 — 상태 이름·결과는 전부 저작값이다.
    actor_overrides 의 capability 이름은 시나리오 어댑터가 행위자에게 부여한다(열쇠·통행증 등).
    """
    acd = raw.get("access_control_definitions")
    empty = {"rules": {}, "controls": {}}
    if acd is None:
        return empty
    if not isinstance(acd, dict) or set(acd) - {"rules", "controls"}:
        d.add(ERROR, "ACCESS_DEFINITIONS_INVALID", "access_control_definitions",
              "access_control_definitions는 {rules, controls} 객체여야 합니다")
        return empty
    if "structured_places" not in caps:
        d.add(WARNING, "ACCESS_WITHOUT_CAPABILITY", "access_control_definitions",
              "structured_places capability 없이 접근 정의가 저작됐습니다")
    rules, controls = {}, {}
    raw_rules = acd.get("rules") or {}
    if not isinstance(raw_rules, dict):
        d.add(ERROR, "ACCESS_RULES_INVALID", "access_control_definitions.rules", "rules는 객체여야 합니다")
        raw_rules = {}
    for rid, r in raw_rules.items():
        p = f"access_control_definitions.rules.{rid}"
        if not is_valid_explicit_id(rid) or not isinstance(r, dict):
            d.add(ERROR, "ACCESS_RULE_INVALID", p, "rule_id는 명시 id, 규칙은 객체여야 합니다")
            continue
        if r.get("rule_type") not in ACCESS_RULE_TYPES:
            d.add(ERROR, "ACCESS_RULE_TYPE_UNKNOWN", f"{p}.rule_type",
                  f"평가기가 없는 rule_type {r.get('rule_type')!r} — 지원: {list(ACCESS_RULE_TYPES)}")
            continue
        states = r.get("states")
        if not isinstance(states, dict) or not states:
            d.add(ERROR, "ACCESS_RULE_STATES_REQUIRED", f"{p}.states", "STATE_TABLE에는 states가 필요합니다")
            continue
        out_states, ok = {}, True
        for st, oc in states.items():
            sp = f"{p}.states.{st}"
            if not isinstance(st, str) or not st:
                d.add(ERROR, "ACCESS_STATE_INVALID", sp, "상태 이름은 비어 있지 않은 문자열이어야 합니다")
                ok = False
                continue
            norm = _access_outcome(oc, sp, d, allow_overrides=True)
            if norm is None:
                ok = False
                continue
            out_states[st] = norm
        if ok:
            rules[rid] = {"rule_id": rid, "rule_type": r["rule_type"], "states": out_states}
    raw_controls = acd.get("controls") or {}
    if not isinstance(raw_controls, dict):
        d.add(ERROR, "ACCESS_CONTROLS_INVALID", "access_control_definitions.controls", "controls는 객체여야 합니다")
        raw_controls = {}
    for cid, c in raw_controls.items():
        p = f"access_control_definitions.controls.{cid}"
        if not is_valid_explicit_id(cid) or not isinstance(c, dict) or set(c) - {"rule_ref", "initial_state"}:
            d.add(ERROR, "ACCESS_CONTROL_INVALID", p, "control_id는 명시 id, 제어는 {rule_ref, initial_state}여야 합니다")
            continue
        rr = c.get("rule_ref")
        if rr not in rules:
            d.add(ERROR, "ACCESS_CONTROL_RULE_UNKNOWN", f"{p}.rule_ref", f"정의되지 않은 규칙 {rr!r}")
            continue
        if c.get("initial_state") not in rules[rr]["states"]:
            d.add(ERROR, "ACCESS_INITIAL_STATE_INVALID", f"{p}.initial_state",
                  f"초기 상태 {c.get('initial_state')!r}가 규칙 '{rr}'의 상태가 아닙니다")
            continue
        controls[cid] = {"control_id": cid, "rule_ref": rr, "initial_state": c["initial_state"]}
    return {"rules": rules, "controls": controls}


def _access_outcome(oc, p, d, *, allow_overrides):
    keys = {"result", "action_policy", "action_delay_minutes"} | (
        {"actor_overrides"} if allow_overrides else {"requires_capability"})
    if not isinstance(oc, dict) or set(oc) - keys:
        d.add(ERROR, "ACCESS_OUTCOME_INVALID", p, f"결과 정의 키는 {sorted(keys)} 안이어야 합니다")
        return None
    res = oc.get("result")
    if res not in ACCESS_RESULTS:
        d.add(ERROR, "ACCESS_RESULT_INVALID", f"{p}.result", f"result는 {list(ACCESS_RESULTS)} 중 하나")
        return None
    pol = oc.get("action_policy")
    if res == "ACTION_REQUIRED":
        if pol not in ACCESS_ACTION_POLICIES:
            d.add(ERROR, "ACCESS_POLICY_INVALID", f"{p}.action_policy",
                  f"ACTION_REQUIRED에는 action_policy {list(ACCESS_ACTION_POLICIES)}가 필요합니다")
            return None
    elif pol is not None:
        d.add(ERROR, "ACCESS_POLICY_UNEXPECTED", f"{p}.action_policy", "action_policy는 ACTION_REQUIRED에만 씁니다")
        return None
    delay = oc.get("action_delay_minutes", 0)
    if not _is_nonneg_int(delay) or (delay and res != "ACTION_REQUIRED"):
        d.add(ERROR, "ACCESS_DELAY_INVALID", f"{p}.action_delay_minutes",
              "action_delay_minutes는 ACTION_REQUIRED의 0 이상 정수여야 합니다")
        return None
    out = {"result": res, "action_policy": pol, "action_delay_minutes": delay}
    if allow_overrides:
        ovs = oc.get("actor_overrides") or []
        if not isinstance(ovs, list):
            d.add(ERROR, "ACCESS_OVERRIDES_INVALID", f"{p}.actor_overrides", "actor_overrides는 목록이어야 합니다")
            return None
        norm = []
        for j, ov in enumerate(ovs):
            op = f"{p}.actor_overrides[{j}]"
            cap = ov.get("requires_capability") if isinstance(ov, dict) else None
            if not isinstance(cap, str) or not cap:
                d.add(ERROR, "ACCESS_OVERRIDE_CAPABILITY_REQUIRED", op, "requires_capability가 필요합니다")
                return None
            n = _access_outcome(ov, op, d, allow_overrides=False)
            if n is None:
                return None
            n["requires_capability"] = cap
            norm.append(n)
        out["actor_overrides"] = norm          # 저작 순서 = 평가 순서(첫 일치)
    else:
        out["requires_capability"] = oc.get("requires_capability")
    return out


_SEED_KEYS = ("known_places", "known_edges", "familiarity_scopes", "known_access_facts", "route_sets")


def _compile_spatial_seeds(raw, place_defs, edges, access, caps, d):
    """raw spatial_knowledge_seeds {seed_id: SpatialKnowledgeSeed} — 참조 검증만(적용은 H06)."""
    seeds = raw.get("spatial_knowledge_seeds")
    if seeds is None:
        return {}
    if not isinstance(seeds, dict):
        d.add(ERROR, "SEEDS_INVALID", "spatial_knowledge_seeds", "spatial_knowledge_seeds는 객체여야 합니다")
        return {}
    pair_index = {}
    for eid, e in edges.items():
        pair_index.setdefault(e["pair_id"], []).append(eid)
    out = {}
    for sid, sd in seeds.items():
        p = f"spatial_knowledge_seeds.{sid}"
        if not is_valid_explicit_id(sid) or not isinstance(sd, dict):
            d.add(ERROR, "SEED_INVALID", p, "seed_id는 명시 id, seed는 객체여야 합니다")
            continue
        for k in sd:
            if k not in _SEED_KEYS:
                d.add(WARNING, "SEED_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 seed 키(무시)")
        if sd.get("route_sets"):
            d.add(WARNING, "SEED_ROUTE_SETS_DEFERRED", f"{p}.route_sets", "route_sets는 아직 컴파일하지 않습니다")
        rec, ok = {"seed_id": sid}, True
        for k, pool, code in (("known_places", place_defs, "SEED_PLACE_UNKNOWN"),
                              ("familiarity_scopes", place_defs, "SEED_PLACE_UNKNOWN")):
            vals = _str_list(sd.get(k))
            if vals is None:
                d.add(ERROR, "SEED_FIELD_INVALID", f"{p}.{k}", f"{k}는 문자열 목록이어야 합니다")
                ok = False
                continue
            for v in vals:
                if v not in pool:
                    d.add(ERROR, code, f"{p}.{k}", f"'{v}'가 places에 없습니다")
                    ok = False
            rec[k] = sorted(set(vals))
        ke = _str_list(sd.get("known_edges"))
        if ke is None:
            d.add(ERROR, "SEED_FIELD_INVALID", f"{p}.known_edges", "known_edges는 문자열 목록이어야 합니다")
            ok, ke = False, []
        expanded = set()
        for v in ke:
            if v in edges:
                expanded.add(v)
            elif v in pair_index:
                expanded.update(pair_index[v])           # pair id = 저작된 양방향 모두
            else:
                d.add(ERROR, "SEED_EDGE_UNKNOWN", f"{p}.known_edges", f"정의되지 않은 구간 '{v}'")
                ok = False
        rec["known_edges"] = sorted(expanded)
        facts = sd.get("known_access_facts") or []
        norm = []
        if not isinstance(facts, list):
            d.add(ERROR, "SEED_FIELD_INVALID", f"{p}.known_access_facts", "목록이어야 합니다")
            ok, facts = False, []
        seen_refs = {}
        for j, f in enumerate(facts):
            fp = f"{p}.known_access_facts[{j}]"
            ref = f.get("ref") if isinstance(f, dict) else None
            # 참조 = control_id 또는 edge_id(DESIGN 02 §81). 런타임 사실 키(access.access_fact_key)로
            # 정규화한다: 제어가 있는 구간의 edge_id → 그 control_id, 제어 없는 구간 → edge_id.
            if isinstance(ref, str) and ref in access["controls"]:
                key = ref
            elif isinstance(ref, str) and ref in edges:
                key = edges[ref].get("access_ref") or ref
            else:
                key = None
            if key is None or f.get("observed_result") not in ACCESS_RESULTS or \
                    not (f.get("observed_state") is None or isinstance(f.get("observed_state"), str)):
                d.add(ERROR, "SEED_ACCESS_FACT_INVALID", fp,
                      "{ref: control_id|edge_id, observed_result, observed_state?}가 필요합니다")
                ok = False
                continue
            if key in seen_refs:
                d.add(ERROR, "SEED_ACCESS_FACT_DUPLICATE", fp,
                      f"같은 접근 사실 키 '{key}'가 known_access_facts[{seen_refs[key]}]와 겹칩니다")
                ok = False
                continue
            seen_refs[key] = j
            norm.append({"ref": key, "authored_ref": ref, "observed_result": f["observed_result"],
                         "observed_state": f.get("observed_state")})
        rec["known_access_facts"] = sorted(norm, key=lambda x: x["ref"])
        if ok:
            out[sid] = rec
    return out


# ── 시작 틀 → 초기화 정의 ─────────────────────────────────────

def _parse_slot(spec):
    if not isinstance(spec, str):
        return {"spec": spec, "source": "literal", "ref": None}
    for src in ("profile", "place", "list"):
        if spec.startswith(src + ":"):
            return {"spec": spec, "source": src, "ref": spec[len(src) + 1:]}
    return {"spec": spec, "source": "literal", "ref": None}


# ── 달력 (H03 — quantitative_time) ───────────────────────────

_CALENDAR_KEYS = ("calendar_id", "type", "month_lengths", "dayparts")


def _compile_calendar(raw, caps, d):
    """raw `calendar` → 컴파일된 달력. quantitative_time 선언 시 필수 — 12×30 암묵 달력 없음.

    {"calendar_id": str, "type": "GREGORIAN"|"FIXED_CUSTOM", "month_lengths": [int,...](FIXED_CUSTOM),
     "dayparts": {id: {"start": "HH:MM", "end": "HH:MM", "representative": "HH:MM"}}}
    시간대 구간은 [start, end) 반열림, start > end는 자정을 넘는다. 구간 겹침은 ERROR.
    시작 날짜·시각의 세션 적용은 초기화(H06) 몫이다.
    """
    from . import world_clock as _wc
    cal = raw.get("calendar")
    if cal is None:
        if "quantitative_time" in caps:
            d.add(ERROR, "CALENDAR_REQUIRED", "calendar",
                  "quantitative_time에는 명시 달력이 필요합니다(legacy 12×30을 암묵 달력으로 쓰지 않음)")
        return None
    p = "calendar"
    if not isinstance(cal, dict):
        d.add(ERROR, "CALENDAR_INVALID", p, "calendar는 객체여야 합니다")
        return None
    for k in cal:
        if k not in _CALENDAR_KEYS:
            d.add(WARNING, "CALENDAR_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 달력 키")
    errs_before = len([x for x in d.items if x.severity == ERROR])
    cid = cal.get("calendar_id")
    if not is_valid_explicit_id(cid):
        d.add(ERROR, "CALENDAR_ID_INVALID", f"{p}.calendar_id", "calendar_id가 필요합니다(명시 id 형식)")
    ctype = cal.get("type")
    if ctype not in _wc.CALENDAR_TYPES:
        d.add(ERROR, "CALENDAR_TYPE_INVALID", f"{p}.type", f"type은 {list(_wc.CALENDAR_TYPES)} 중 하나여야 합니다")
    ml = cal.get("month_lengths")
    if ctype == _wc.CALENDAR_FIXED_CUSTOM:
        if not (isinstance(ml, list) and ml and all(_is_pos_int(x) for x in ml)):
            d.add(ERROR, "CALENDAR_MONTH_LENGTHS_INVALID", f"{p}.month_lengths",
                  "FIXED_CUSTOM은 양의 정수 month_lengths 목록이 필요합니다")
    elif ml is not None:
        d.add(ERROR, "CALENDAR_MONTH_LENGTHS_UNEXPECTED", f"{p}.month_lengths",
              "GREGORIAN 달력에는 month_lengths를 쓰지 않습니다")
    dayparts = cal.get("dayparts")
    compiled_dp, covered = {}, {}
    if not isinstance(dayparts, dict) or not dayparts:
        d.add(ERROR, "CALENDAR_DAYPARTS_REQUIRED", f"{p}.dayparts", "시간대(dayparts) 정의가 필요합니다")
    else:
        for dp_id, dp in dayparts.items():
            dpp = f"{p}.dayparts.{dp_id}"
            if not isinstance(dp, dict) or set(dp) != {"start", "end", "representative"}:
                d.add(ERROR, "DAYPART_INVALID", dpp, "start·end·representative('HH:MM')가 필요합니다")
                continue
            try:
                st, en, rep = (_wc.parse_hhmm(dp["start"]), _wc.parse_hhmm(dp["end"]),
                               _wc.parse_hhmm(dp["representative"]))
            except _wc.ClockError as e:
                d.add(ERROR, "DAYPART_INVALID", dpp, str(e))
                continue
            if st == en:
                d.add(ERROR, "DAYPART_EMPTY", dpp, "start와 end가 같습니다")
                continue
            if not _wc.daypart_contains(st, en, rep):
                d.add(ERROR, "DAYPART_REPRESENTATIVE_OUTSIDE", dpp, "대표 시각이 구간 밖입니다")
            for minute in range(1440):
                if _wc.daypart_contains(st, en, minute):
                    if minute in covered:
                        d.add(ERROR, "DAYPART_OVERLAP", dpp,
                              f"'{covered[minute]}'과 {_wc.format_hhmm(minute)}에서 겹칩니다")
                        break
                    covered[minute] = dp_id
            compiled_dp[dp_id] = {"start": dp["start"], "end": dp["end"],
                                  "representative": dp["representative"]}
    if len([x for x in d.items if x.severity == ERROR]) > errs_before:
        return None
    if "quantitative_time" not in caps:
        d.add(WARNING, "CALENDAR_WITHOUT_CAPABILITY", p,
              "quantitative_time capability 없이 달력이 저작됐습니다(컴파일되지만 런타임 근거 아님)")
    compiled = {
        "calendar_id": cid,
        "calendar_type": ctype,
        "month_lengths": list(ml) if ctype == _wc.CALENDAR_FIXED_CUSTOM else None,
        "dayparts": compiled_dp,
        "daypart_order": sorted(compiled_dp, key=lambda k: (_wc.parse_hhmm(compiled_dp[k]["start"]), k)),
    }
    _wc.Calendar(compiled)          # 런타임 계약으로 생성 가능한지(실패 시 컴파일러 버그)
    return compiled


def _compile_initialization(raw, place_defs, d):
    """start_frames/start_points → 초기화 정의.

    facts 의 time/location 은 슬롯 템플릿('{때}', '{장소}')이다. 기계 시각·위치로
    해석하지 않는다 — 정량 시간 시작점은 사람이 저작한다(DESIGN 08 §20).
    """
    out = {"start_frames": [], "start_points": []}
    sp = raw.get("start_points")
    if sp is not None:
        vals = _str_list(sp)
        if vals is None or isinstance(sp, str):
            d.add(ERROR, "START_POINTS_INVALID", "start_points", "start_points는 장소 이름 목록이어야 합니다")
            vals = []
        for v in vals:
            if place_defs and v not in place_defs:
                d.add(WARNING, "START_POINT_UNKNOWN_PLACE", "start_points", f"'{v}'가 places에 없습니다")
        out["start_points"] = vals
    frames = raw.get("start_frames")
    if frames is None:
        return out
    if not isinstance(frames, list):
        d.add(ERROR, "START_FRAMES_INVALID", "start_frames", "start_frames는 목록이어야 합니다")
        return out
    seen = {}
    for i, fr in enumerate(frames):
        path = f"start_frames[{i}]"
        if not isinstance(fr, dict) or not isinstance(fr.get("id"), str) or not fr.get("id"):
            d.add(ERROR, "START_FRAME_ID_MISSING", path, "시작 틀 id가 없습니다")
            continue
        fid = fr["id"]
        if fid in seen:
            d.add(ERROR, "START_FRAME_ID_DUPLICATE", path, f"id '{fid}'가 start_frames[{seen[fid]}]와 겹칩니다")
            continue
        seen[fid] = i
        slots = {}
        raw_slots = fr.get("slots") if isinstance(fr.get("slots"), dict) else {}
        for name, spec in raw_slots.items():
            s = _parse_slot(spec)
            if s["source"] == "list":
                pool = raw.get(s["ref"])
                if not isinstance(pool, (list, dict)) or not pool:
                    d.add(WARNING, "START_SLOT_LIST_MISSING", f"{path}.slots.{name}",
                          f"목록 '{s['ref']}'이 없거나 비어 런타임이 '미상'으로 채웁니다")
            elif s["source"] == "place" and not out["start_points"]:
                d.add(WARNING, "START_SLOT_NO_START_POINTS", f"{path}.slots.{name}",
                      "start_points가 비어 런타임이 '미상'으로 채웁니다")
            slots[name] = s
        out["start_frames"].append({
            "frame_id": fid,
            "title": fr.get("title") if isinstance(fr.get("title"), str) else fid,
            "conditions": copy.deepcopy(fr.get("conditions") or {}),
            "slots": slots,
            "fact_templates": copy.deepcopy(fr.get("facts") or {}),
            "source_ref": ["start_frames", i],
        })
    return out


# ── 진입점 ────────────────────────────────────────────────────

def compile_scenario(raw: dict, scenario_id: str, quests: dict | None = None) -> CompileResult:
    """raw 시나리오(+퀘스트 부속)를 컴파일한다. 입력을 변경하지 않는다."""
    d = _Diags()
    if not isinstance(raw, dict):
        d.add(ERROR, "SOURCE_INVALID", "$", "시나리오 최상위가 객체가 아닙니다")
        return CompileResult(scenario_id, COMPILER_VERSION, None, None, d.sorted())
    if not isinstance(scenario_id, str) or not scenario_id:
        d.add(ERROR, "SCENARIO_ID_INVALID", "$", "scenario_id가 필요합니다")
        return CompileResult(str(scenario_id), COMPILER_VERSION, None, None, d.sorted())

    # 원본이 정규 직렬화 가능한지 먼저 본다(정의 revision의 전제).
    legacy_source = {"scenario": raw, "quests": quests}
    try:
        canonical_json(legacy_source)
    except CanonicalSerializationError as e:
        d.add(ERROR, "SOURCE_NOT_CANONICAL", "$", str(e))
        return CompileResult(scenario_id, COMPILER_VERSION, None, None, d.sorted())

    cap = detect_capabilities(raw, d)
    for k in RESERVED_UNCOMPILED_KEYS:
        if k in raw:
            d.add(WARNING, "RESERVED_KEY_UNCOMPILED", k,
                  f"컴파일러 v{COMPILER_VERSION}은 '{k}'를 구조화하지 않습니다(원문만 보존)")

    actors, actor_order, actor_index = _compile_actors(scenario_id, raw, d)
    places, place_order = _compile_places(raw, actor_index, d)
    modes = _compile_movement_modes(raw, cap["capabilities"], d)
    access = _compile_access(raw, cap["capabilities"], d)
    edges = _compile_edges(scenario_id, raw, places, cap["capabilities"], d, modes, access)
    if edges and "structured_places" in cap["capabilities"] and not modes:
        d.add(ERROR, "MODES_REQUIRED", "movement_modes",
              "structured_places 구간을 쓰려면 movement_modes(속도 저작)가 필요합니다")
    seeds = _compile_spatial_seeds(raw, places, edges, access, cap["capabilities"], d)
    init = _compile_initialization(raw, places, d)
    calendar = _compile_calendar(raw, cap["capabilities"], d)
    explicit = _compile_explicit_infos(raw, actors, places, d)
    infos = {}
    for src in (explicit, _compile_inline_secrets(raw, actors, d, explicit),
                _compile_quest_hidden(scenario_id, quests, places, d)):
        for iid, rec in src.items():
            if iid in infos:
                d.add(ERROR, "INFO_ID_COLLISION", ".".join(map(str, rec["source_ref"])),
                      f"InfoId '{iid}'가 겹칩니다")
                continue
            infos[iid] = rec
    # 같은 사실의 이중 정의를 조용히 합치지 않는다 — 같은 명제 문장이면 경고(info_ref 사용 권장).
    by_text = {}
    for iid in sorted(infos):
        by_text.setdefault(infos[iid]["proposition"].strip(), []).append(iid)
    for text, ids in sorted(by_text.items()):
        if len(ids) > 1:
            d.add(WARNING, "INFO_DUPLICATE_PROPOSITION", "information_definitions",
                  f"같은 명제가 여러 InfoId로 정의됐습니다: {', '.join(ids)} — 합치지 않습니다(info_ref 사용 권장)")
    series = _compile_info_series(raw, _ref_scope(actors, places, infos, access, modes, calendar=calendar), d)

    diagnostics = d.sorted()
    if any(x.severity == ERROR for x in diagnostics):
        return CompileResult(scenario_id, COMPILER_VERSION, None, None, diagnostics)

    src_copy = copy.deepcopy(legacy_source)
    definition = {
        "format": DEFINITION_FORMAT,
        "format_version": DEFINITION_FORMAT_VERSION,
        # 컴파일 의미 버전은 정의의 일부다 — 규칙이 바뀌면 같은 원본도 다른 revision.
        "compiler_version": COMPILER_VERSION,
        "scenario_meta": {
            "scenario_id": scenario_id,
            "schema_mode": cap["mode"],
            "scenario_schema_version": cap["scenario_schema_version"],
            "capabilities": dict(cap["capabilities"]),
            "validated_capabilities": cap["validated"],
            "deferred_capabilities": cap["deferred"],
            "has_quest_sidecar": quests is not None,
        },
        "actor_definitions": actors,
        "actor_order": actor_order,
        "actor_index": actor_index,
        "place_definitions": places,
        "place_order": place_order,
        "movement_edges": edges,
        # H03(compiler v2): 이동 방식·접근 정의·공간 지식 seed. 없으면 빈 값.
        "movement_modes": modes,
        "access_control_definitions": access,
        "spatial_knowledge_seeds": seeds,
        "information_definitions": infos,
        # H04(compiler v3): 정보 계열 정의. 초기 상태 적용은 H06.
        "information_series": series,
        "initialization_definitions": init,
        # H03(compiler v2): 정량 시간 달력. 레거시는 None(새 시간 엔진 근거 아님).
        "calendar": calendar,
        # 현행 런타임은 raw 모양을 직접 소비한다. 세션 바인딩(H10) 뒤에도 정의 하나로
        # 그 모양을 복원할 수 있도록 원문을 보존하고, dict 순서(장소 순회·NPC 전개
        # 순서가 의존)까지 순서 지문으로 정의에 묶는다.
        "legacy_source": src_copy,
        "legacy_source_order_digest": order_digest(src_copy),
    }
    return CompileResult(scenario_id, definition["compiler_version"], definition,
                         definition_revision(definition), diagnostics)


def compile_sources(sources: ScenarioSources) -> CompileResult:
    return compile_scenario(sources.raw, sources.scenario_id, sources.quests)


def definition_revision(definition: dict) -> str:
    """정의의 정규 직렬화 SHA-256(DESIGN 06 §19). compiler_version을 포함한 정의 전체."""
    return content_digest(definition)


def legacy_scenario_view(definition: dict) -> tuple:
    """정의에서 현행 런타임이 쓰는 (scenario_data, quests) 원문을 복원한다(사본)."""
    src = definition["legacy_source"]
    return copy.deepcopy(src["scenario"]), copy.deepcopy(src["quests"])


# ── 보고 ──────────────────────────────────────────────────────

def summarize(result: CompileResult) -> dict:
    """사람이 읽는 요약(결정적)."""
    out = {
        "scenario_id": result.scenario_id,
        "compiler_version": result.compiler_version,
        "ok": result.ok,
        "revision": result.revision,
        "counts": {"ERROR": len(result.errors), "WARNING": len(result.warnings),
                   "INFO": len(result.by_severity(INFO))},
    }
    if result.definition is not None:
        df = result.definition
        out["schema_mode"] = df["scenario_meta"]["schema_mode"]
        out["capabilities"] = df["scenario_meta"]["capabilities"]
        out["validated_capabilities"] = df["scenario_meta"]["validated_capabilities"]
        out["deferred_capabilities"] = df["scenario_meta"]["deferred_capabilities"]
        infos = df["information_definitions"].values()
        out["sizes"] = {
            "actors": len(df["actor_definitions"]),
            "places": len(df["place_definitions"]),
            "movement_edges": len(df["movement_edges"]),
            "movement_modes": len(df["movement_modes"]),
            "access_controls": len(df["access_control_definitions"]["controls"]),
            "info_npc_inline_secret": sum(1 for i in infos if i["kind"] == "NPC_INLINE_SECRET"),
            "info_quest_hidden": sum(1 for i in infos if i["kind"] == "QUEST_HIDDEN_TRUTH"),
            "start_frames": len(df["initialization_definitions"]["start_frames"]),
            "start_points": len(df["initialization_definitions"]["start_points"]),
        }
    return out


def capability_matrix(result: CompileResult) -> dict:
    """capability별 상태: declared(버전)·validated·deferred·absent."""
    caps = (result.definition or {}).get("scenario_meta", {}).get("capabilities", {})
    out = {}
    for name in KNOWN_CAPABILITIES:
        if name not in caps:
            out[name] = "absent"
        elif name in VALIDATED_CAPABILITIES:
            out[name] = f"v{caps[name]} validated"
        else:
            out[name] = f"v{caps[name]} deferred"
    return out


def migration_report(sources: ScenarioSources) -> dict:
    """이행 분석(읽기 전용). 무엇을 사람이 저작·분류해야 하는지 나열한다(DESIGN 08 §49).

    아무것도 고치지 않는다. 컴파일 ERROR가 있어도 분석 가능한 범위는 보고한다.
    """
    raw, quests, sid = sources.raw, sources.quests, sources.scenario_id
    d = _Diags()
    actors, _, actor_index = _compile_actors(sid, raw, d)
    places, _ = _compile_places(raw, actor_index, d)

    implicit = []
    for pid, rec in places.items():
        for p in [rec["parent"]] + rec["extra_parents"]:
            if p is None or p not in places:
                continue
            # core.places.is_container 와 같은 판정: connected 없는 상위(traversable 아님)는 범위.
            # (p는 pid의 상위이므로 하위가 있다는 조건은 이미 성립한다.)
            prec = places[p]
            container = (not prec["traversable"]) and not prec["connected"]
            if not container and p not in rec["connected"] and pid not in prec["connected"]:
                implicit.append([pid, p])

    alias_owners = {}
    for pid, rec in places.items():
        for al in rec["aliases"]:
            alias_owners.setdefault(al, []).append(pid)
    alias_issues = [{"alias": al, "owners": ps, "shadowed_by_place_key": al in places}
                    for al, ps in alias_owners.items() if len(ps) > 1 or al in places]

    connected_pairs = set()
    for a, rec in places.items():
        for b in rec["connected"]:
            if b in places:
                connected_pairs.add(tuple(sorted((a, b))))

    secrets = _compile_inline_secrets(raw, actors, d)
    hidden = _compile_quest_hidden(sid, quests, places, _Diags())
    static_assoc = {pid: [x["legacy_name"] for x in rec["npc_associations"]]
                    for pid, rec in places.items() if rec["npc_associations"]}
    return {
        "scenario_id": sid,
        "compiler_version": COMPILER_VERSION,
        "capabilities": detect_capabilities(raw, _Diags()),
        "places": {
            "count": len(places),
            "asymmetric_connected": asymmetric_connected(places),
            "implicit_parent_child_routes": implicit,
            "alias_issues": alias_issues,
            "connected_pairs_without_authored_distance": len(connected_pairs),
            "authored_movement_edges": len(raw.get("movement_edges") or [])
            if isinstance(raw.get("movement_edges"), list) else 0,
            "static_npc_associations_not_presence": static_assoc,
        },
        "actors": {
            "count": len(actors),
            "id_table": [{"legacy_key": a["legacy_key"], "actor_id": aid,
                          "explicit_id": a["explicit_id"]} for aid, a in actors.items()],
            "inline_secrets_compiled": len(secrets),
        },
        "quests": {
            "present": quests is not None,
            "count": len(quests["quests"]) if isinstance(quests, dict)
            and isinstance(quests.get("quests"), list) else 0,
            "hidden_truth_info_candidates": [
                {"quest_id": h["quest_id"], "quest_version": h["quest_version"], "info_id": iid}
                for iid, h in hidden.items()],
            "event_template_conversion": "사람 검토 필요 — 기계적 tree 복사 금지(DESIGN 08 §24)",
        },
        "start_frames": [
            {"frame_id": f["frame_id"],
             "slot_sources": {k: s["source"] for k, s in f["slots"].items()},
             "fact_template_keys": sorted(f["fact_templates"])}
            for f in _compile_initialization(raw, places, _Diags())["start_frames"]],
        "human_authoring_required": [
            "이동 구간 거리·방향·route_hint (connected 쌍에서 자동 생성 금지)",
            "비대칭 connected 각각: 의도된 일방향인지 양방향 누락인지 분류",
            "암묵 상하위 이동 후보 각각: 물리 구간으로 저작할지 결정",
            "동적 접근(문·검문·봉쇄) 대상과 상태 의미",
            "정확한 시작 날짜·달력·시작 틀별 시각(정량 시간 capability)",
            "시작 틀별 플레이어 공간 지식 seed",
            "퀘스트 → 사건 원형의 최종 국면·조건·케이스",
        ],
        "diagnostics": [x.to_dict() for x in d.sorted()],
    }
