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
import itertools
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
    # v7.48: 생존물 기능 — 선언한 시나리오에만 붙는다(공용 상태 requires · 추출 소음 · 인물 행동 유형)
    "mortality": 1,     # 빈사 → 사망(상태 결말)
    "stealth": 1,       # 숨음 · 추출 소음(noise) 판단
    "pursuit": 1,       # 인물 행동 유형 pursuer(추적자)
    # v7.50: 헌터물 기능
    "combat_wounds": 1,  # 공격 대결 성공 여유 → 진 쪽 부상 단계(코드) · 쓰러짐
    "growth": 1,         # 성장 포인트·등급(사건 지급 · 플레이어 선언 사용)
    "party": 1,          # v7.51 NPC 파티(역할 행동 · 합류 조건 · 적 stance)
}

# 이 컴파일러 버전이 도메인 검증까지 끝내는 capability. 나머지는 선언을 기록하되
# WARNING(CAP_VALIDATION_DEFERRED)으로 표시한다 — 해당 WP가 검증기를 넣을 때 옮긴다.
VALIDATED_CAPABILITIES = frozenset({"stable_actors", "quantitative_time", "structured_places",
                                    "information_registry", "actor_cognition", "dynamic_actor_location",
                                    "dynamic_events", "scheduled_world_progression",
                                    "mortality", "stealth", "pursuit", "combat_wounds", "growth", "party"})
# 주의: '검증됨'은 저작 정의의 컴파일·정적 검증이 끝났다는 뜻이다. 세션 초기화(H06)·플레이어 턴
# 배선(H05)·프롬프트 공개 경계(H05)·시나리오 실제 저작 완성도(H07)는 별도 게이트다.

# v7.48: 세계 기반이 아닌 '선택 기능' capability — 필요한 시나리오만 선언한다(세계 기반 capability와 구분).
FEATURE_CAPABILITIES = frozenset({"mortality", "stealth", "pursuit", "combat_wounds", "growth", "party"})

# capability 의존성(DESIGN 08 §4). (요구 capability, 심각도)
# dynamic_actor_location → structured_places 는 사용자 결정(H01 보정)으로 ERROR.
CAPABILITY_DEPENDENCIES = {
    "scheduled_world_progression": (("quantitative_time", "ERROR"),),
    "actor_cognition": (("stable_actors", "ERROR"), ("information_registry", "ERROR")),
    "dynamic_events": (("stable_actors", "ERROR"),),
    "dynamic_actor_location": (("structured_places", "ERROR"),),
    "pursuit": (("stealth", "ERROR"), ("dynamic_events", "ERROR"), ("dynamic_actor_location", "ERROR")),
    "combat_wounds": (("mortality", "ERROR"), ("dynamic_events", "ERROR")),
    "growth": (("dynamic_events", "ERROR"),),
    "party": (("combat_wounds", "ERROR"), ("dynamic_events", "ERROR")),
}

# 새 스키마용으로 예약된 키. 이 컴파일러가 구조화하지 않으므로, 있으면 경고한다
# (v2/H03: calendar·movement_modes·access_control_definitions·spatial_knowledge_seeds는 컴파일 대상)
# (legacy_source 에 원문으로 남지만 어떤 엔진도 읽지 않는다는 뜻).
RESERVED_UNCOMPILED_KEYS = (
    "visibility_relations", "initialization_definitions",
)

# NPC 인라인 비밀 필드(npc_template.info_fields의 '비밀').
INLINE_SECRET_FIELD = "비밀"
# profile_base에서 빼는 필드: 구조화된 별도 권위(actor_id·name·비밀) + 레거시 런타임 초기값.
PROFILE_BASE_EXCLUDED = ("actor_id", "name", INLINE_SECRET_FIELD, "resources", "statuses", "knowledge_seed",
                         "routine", "behavior", "party", "stance")
# H07: default_npcs[*].knowledge_seed — 저작 NPC의 초기 공간 지식 seed id(genesis가 적용). 지식은 이동 허가가 아니라
#      지름길·숨은 길·위험 회피 같은 경로 선택 품질의 근거다(H07 최종 지시 §6). 저작된 경우에만 정의에 키가 생긴다.
# H07: default_npcs[*].routine — 선택적 평상시 일과(요일·시간대 → 장소). 정본 배치가 아니라 정의 + 정본 시계의
#      순수 함수로 해석되는 파생 위치다(core.npc_position). 저작된 경우에만 정의에 키가 생긴다.
INFO_EXPLICIT_KEYS = ("proposition", "summary", "subject_refs", "tags", "fragments", "sensitivity", "reveals",
                      "initial_knowledge")
# v7.17 초기 지식(선택 opt-in) — 미표기 = 초기 전달 금지
IK_KEYS = ("tier", "phases", "access", "start_frame_ids", "kind")
IK_TIERS = ("required", "optional")
IK_PHASES = ("PROFILE", "START")
IK_KINDS = ("fact", "reported_claim")
IK_OPS = ("equals", "one_of")
IK_SUMMARY_MAX = 300
# v7.17 아이템 정의(선택) — 저작한 시나리오만 정의에 키가 생긴다(미저작 revision 불변)
ITEM_DEF_KEYS = ("aliases", "categories", "units", "mass_grade", "bulk_grade", "parts", "info_refs",
                 "special", "unique", "recipe_only", "instances", "name", "wear", "use")
ITEM_GRADE_MAX = 5
# H07: information_definitions[*].reveals = {"places": [PlaceId], "edges": [EdgeId|pair id]} — PC가 이 정보를 실제로
#      알게 되면(CONTENT 획득) 함께 얻는 공간 지식(장소 이름·숨은 길 발견: NPC 제보·문서·지도·수색·사건 효과).
#      노출 ≠ 믿음 원칙은 그대로 — 공간 지식은 '그런 길·이름이 있다고 들었다'는 인지다. 저작된 경우에만 키가 생긴다.

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
        if "knowledge_seed" in prof:
            defs[actor_id]["knowledge_seed"] = prof["knowledge_seed"]
        if "routine" in prof:
            defs[actor_id]["routine"] = copy.deepcopy(prof["routine"])     # _compile_routines가 정규화·검증
        if "behavior" in prof:
            defs[actor_id]["behavior"] = copy.deepcopy(prof["behavior"])   # v7.48: _compile_behaviors가 검증
        for k in ("party", "stance"):                                       # v7.51: _compile_party가 검증
            if k in prof:
                defs[actor_id][k] = copy.deepcopy(prof[k])
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


def _compile_explicit_infos(raw, actors, places, d, items=None):
    """top-level information_definitions {InfoId: {proposition, summary?, subject_refs?, tags?,
    fragments?: [{fragment_id, proposition}], sensitivity?}} (v3).

    v7.17: subject_refs에 'item:<종류>'(저작 아이템 정의가 있을 때만) — 정보가 그 물건에 관한 것임을 표시할 뿐
    소유·읽음·인지 권한을 만들지 않는다. initial_knowledge는 _compile_initial_knowledge가 검증·정규화한다."""
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
            pool = {"actor": actors, "place": places, "item": items or {}}.get(kind)
            if pool is None or ident not in pool:
                d.add(ERROR, "INFO_SUBJECT_REF_UNKNOWN", f"{p}.subject_refs",
                      f"subject_ref {r!r}는 'actor:<ActorId>'·'place:<PlaceId>'·'item:<아이템 종류>' 실재 참조여야 합니다")
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


def _compile_info_reveals(raw, infos, places, edges, d):
    """information_definitions[*].reveals 검증·정규화(H07). pair id는 저작된 양방향 모두로 확장한다."""
    src = raw.get("information_definitions") if isinstance(raw.get("information_definitions"), dict) else {}
    pair_index = {}
    for eid, e in edges.items():
        pair_index.setdefault(e["pair_id"], []).append(eid)
    for iid, body in src.items():
        if not isinstance(body, dict) or "reveals" not in body or iid not in infos:
            continue
        p = f"information_definitions.{iid}.reveals"
        rv = body["reveals"]
        if not isinstance(rv, dict) or set(rv) - {"places", "edges"} or not rv:
            d.add(ERROR, "INFO_REVEALS_INVALID", p, "reveals는 {places?, edges?} 객체입니다")
            continue
        out = {"places": [], "edges": []}
        for v in _str_list(rv.get("places")) or []:
            if v not in places:
                d.add(ERROR, "INFO_REVEALS_UNKNOWN", f"{p}.places", f"정의되지 않은 장소 {v!r}")
            out["places"].append(v)
        exp = set()
        for v in _str_list(rv.get("edges")) or []:
            if v in edges:
                exp.add(v)
            elif v in pair_index:
                exp.update(pair_index[v])
            else:
                d.add(ERROR, "INFO_REVEALS_UNKNOWN", f"{p}.edges", f"정의되지 않은 구간 {v!r}")
        if _str_list(rv.get("places")) is None or _str_list(rv.get("edges")) is None:
            d.add(ERROR, "INFO_REVEALS_INVALID", p, "places·edges는 문자열 목록입니다")
        out["places"] = sorted(set(out["places"]))
        out["edges"] = sorted(exp)
        infos[iid]["reveals"] = out


_SERIES_KEYS = ("value_ref", "proposition_template", "tracking_policy", "subject_refs", "tags", "sensitivity")
SERIES_TRACKING_POLICIES = ("ON_CHANGE",)


def _ref_scope(actors, places, infos, access, modes, *, slots=None, allow_self=False, templates=(), calendar=None):
    from . import world_refs as _refs
    from . import world_clock as _wc
    return _refs.RefScope(actors=set(actors), places=set(places), infos=set(infos),
                          controls=set((access or {}).get("controls", {})), modes=set(modes),
                          slots=dict(slots or {}), allow_self=allow_self, templates=set(templates),
                          has_calendar=calendar is not None,
                          dayparts=set((calendar or {}).get("dayparts") or {}),
                          calendar_obj=_wc.Calendar(calendar) if calendar else None,
                          statuses=set(_STATUS_NAMES) if _STATUS_NAMES is not None else None)


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
        # H07: 플레이어 표시명(내부 PlaceId와 분리). display_name = 이름을 아는 PC에게 보이는 이름(기본 PlaceId),
        #      unknown_name = 이름을 모르는 PC에게 보이는 서술형 이름(세력·비밀을 누설하지 않는 저작 문구).
        #      unknown_name이 있는 장소는 보거나 가 보는 것만으로 이름을 알지 못한다(명시 발견 필요 — core.place_display).
        #      저작된 경우에만 키가 생긴다(미저작 시나리오의 정의·revision 불변).
        for fk in ("display_name", "unknown_name"):
            if fk in node:
                v = node[fk]
                if not isinstance(v, str) or not v.strip():
                    d.add(ERROR, "PLACE_DISPLAY_NAME_INVALID", f"{path}.{fk}", f"{fk}는 비어 있지 않은 문자열입니다")
                else:
                    rec[fk] = v.strip()
        if rec.get("unknown_name") is not None and rec["unknown_name"] in (pid, rec.get("display_name")):
            d.add(ERROR, "PLACE_UNKNOWN_NAME_LEAKS", f"{path}.unknown_name",
                  "unknown_name이 실제 이름과 같습니다(모르는 PC에게 이름이 누설됩니다)")
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
            rec_a = {"legacy_name": nm, "actor_id": aid}
            if isinstance(entry, dict):
                # H07: 연고 근거 메타데이터(relation·frequency) 보존 — 현재 위치·확률 위치로 해석하지 않는다.
                for mk in ("relation", "frequency"):
                    if isinstance(entry.get(mk), str) and entry[mk]:
                        rec_a[mk] = entry[mk]
            assoc.append(rec_a)
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
            # v3(H04): 안정 variant id — 같은 문·상태·정책의 서로 다른 capability 행동을 구분한다(A25)
            norm["variant_id"] = f"{rid}:{st}:base"
            seen_ov = set()
            for j, ov in enumerate(norm["actor_overrides"]):
                vid = ov.pop("override_id", None) or f"{rid}:{st}:override:{j}"
                if vid in seen_ov or vid == norm["variant_id"]:
                    d.add(ERROR, "ACCESS_OVERRIDE_ID_DUPLICATE", f"{sp}.actor_overrides[{j}]",
                          f"override variant id {vid!r} 중복")
                    ok = False
                seen_ov.add(vid)
                ov["variant_id"] = vid
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
        {"actor_overrides"} if allow_overrides else {"requires_capability", "override_id", "payment"})
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
        oid = oc.get("override_id")
        if oid is not None and not is_valid_explicit_id(oid):
            d.add(ERROR, "ACCESS_OVERRIDE_ID_INVALID", f"{p}.override_id", "override_id는 명시 id 형식")
            return None
        if oid is not None:
            out["override_id"] = oid
        pay = oc.get("payment")
        if pay is not None:
            # v7.17(R4): 확인 행동이 실제 물건 인계를 요구한다 — 보유 이름(requires_capability)을 quantity만큼 치른다
            if not (isinstance(pay, dict) and set(pay) == {"quantity"} and isinstance(pay["quantity"], int)
                    and not isinstance(pay["quantity"], bool) and pay["quantity"] > 0) \
                    or res != "ACTION_REQUIRED" or pol != "CONFIRM":
                d.add(ERROR, "ACCESS_PAYMENT_INVALID", f"{p}.payment",
                      "payment는 CONFIRM 행동의 {quantity: 양의 정수}만")
                return None
            out["payment"] = {"quantity": pay["quantity"]}
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


# ── 사건 템플릿 (H04-B — dynamic_events) ─────────────────────

_TPL_KEYS = ("version", "name", "slots", "distinct", "spawn_when", "repeat_policy", "max_occurrences",
             "access_when", "phase_selector", "phases", "transition_cases", "case_selection", "case_required",
             "resolve_when", "cancel_when", "relevance_places")
_PHASE_KEYS = ("eligible_when", "retain_when", "priority", "base_score", "score_terms", "rank", "terminal",
               "guide", "resolution_options", "finds", "ending")
# H07(사용자 결정 — 메인 결말·플레이어 명시 결단): phases[*].resolution_options = [{option_id, label}] — 이 국면에서
#      플레이어가 **명시적으로** 내릴 수 있는 결단. 판단층위가 플레이어 선언에서 키만 고르고, 코드가 예약 사건 지역
#      상태 'resolution'에 typed EVENT_LOCAL_SET(PLAYER_RESOLUTION)으로 쓴다. 이후 결과는 기존 국면·케이스가 적용한다
#      (새 분기 엔진 없음). 'resolution' 키는 저작 step이 쓸 수 없다(EVENT_LOCAL_RESERVED_KEY).
# H07: phases[*].guide — 지시층위(GM 권한) 전용 상황 지침. 묘사·추출·판단층위에는 주지 않는다.
#      relevance_places — 장면 장소(또는 그 상위)가 목록·바인딩된 PLACE 슬롯과 닿을 때만 지침을 싣는다.
#      둘 다 저작된 경우에만 컴파일 결과에 키가 생긴다(미저작 시나리오의 정의·revision 불변).
_CASE_KEYS = ("case_id", "from", "to", "valid_when", "weight", "steps")
_STEP_PARAMS = {
    "SET_ACCESS_STATE": (("control", "state"), ()),
    "SET_ACTOR_PLACEMENT": (("actor", "place"), ()),
    "CLEAR_ACTOR_PLACEMENT": (("actor",), ()),
    "START_MOVEMENT": (("actor", "destination", "mode"), ("stance", "stance_factor", "capabilities", "route")),
    "CREATE_INFORMATION": (("proposition",), ("summary", "subject_refs", "tags")),
    "SUPERSEDE_INFORMATION": (("info", "proposition"), ("summary", "validity")),
    "ACQUIRE_INFORMATION": (("holder", "info"), ("awareness", "route", "fragments")),
    "SET_COGNITION_BELIEF": (("holder", "info", "belief"), ("authority",)),
    "APPEND_RELATIONSHIP_DELTA": (("from", "to", "kind"), ("summary",)),
    "SET_EVENT_LOCAL": (("key", "value"), ()),
    "SCHEDULE_WAKEUP": (("after_minutes",), ()),
    "SCHEDULE_EFFECT": (("after_minutes", "effect"), ("guard", "epoch_bound")),
    "EMIT_SIGNAL": (("signal_id", "cue"), ("interrupt_policy", "observable_when", "exposes")),
    "SET_ACTOR_STATUS": (("actor", "status"), ()),
    "CLEAR_ACTOR_STATUS": (("actor",), ("status",)),
    "GRANT_GROWTH": (("points",), ()),
    "SET_PC_RECORD": (("field", "value"), ()),
    "JOIN_PARTY": (("actor",), ()),
    "LEAVE_PARTY": (("actor",), ()),
    "GRANT_ITEM": (("key", "quantity"), ("actor", "place")),
}
# H07: EMIT_SIGNAL.exposes — 이 신호가 묘사되면 PC가 알게 될 수 있는 저작 정보(공개 안전 summary 필수).
#      관찰 매니페스트 후보가 될 뿐, 노출 확정은 확정 묘사의 추출 관찰(info_exposed)만 한다.
# H07: CLEAR_ACTOR_PLACEMENT — 정본 override 해제(이후 평상시 일과/untracked). 명시 저작으로만 해제한다.
_EFFECT_STEP_TYPES = ("SET_ACCESS_STATE", "SET_ACTOR_PLACEMENT", "CLEAR_ACTOR_PLACEMENT", "START_MOVEMENT", "CREATE_INFORMATION",
                      "SUPERSEDE_INFORMATION", "ACQUIRE_INFORMATION", "SET_COGNITION_BELIEF",
                      "APPEND_RELATIONSHIP_DELTA", "SET_EVENT_LOCAL", "EMIT_SIGNAL", "SET_ACTOR_STATUS",
                      "CLEAR_ACTOR_STATUS", "GRANT_GROWTH", "SET_PC_RECORD", "JOIN_PARTY", "LEAVE_PARTY", "GRANT_ITEM")


PERSONA_KEYS = ("surface", "core", "stances", "now", "initial_trust")
PERSONA_SURFACE = ("impression", "voice", "lines")
PERSONA_CORE = ("value", "crack", "fear", "ties", "rivals")
PERSONA_STANCES = ("stranger", "known", "trusted", "hostile")
PERSONA_NOW = ("when", "goal", "worry", "mood", "body")
PERSONA_REQUIRED = {"surface": ("impression", "voice", "lines"), "core": ("value",), "stances": ("stranger",)}


PERSONA_LIMITS = {"impression": 60, "voice": 80, "lines": 50, "core": 80, "stances": 100, "now": 60}


def _persona_texts(p, ps):
    sur = p.get("surface") if isinstance(p.get("surface"), dict) else {}
    for k in ("impression", "voice"):
        if isinstance(sur.get(k), str):
            yield f"{ps}.surface.{k}", sur[k], PERSONA_LIMITS[k]
    for i, x in enumerate(sur.get("lines") or () if isinstance(sur.get("lines"), list) else ()):
        if isinstance(x, str):
            yield f"{ps}.surface.lines[{i}]", x, PERSONA_LIMITS["lines"]
    for sec in ("core", "stances"):
        for k, x in (p.get(sec) or {}).items() if isinstance(p.get(sec), dict) else ():
            if isinstance(x, str):
                yield f"{ps}.{sec}.{k}", x, PERSONA_LIMITS[sec]
    for i, r in enumerate(p.get("now") or () if isinstance(p.get("now"), list) else ()):
        for k in ("goal", "worry", "mood", "body"):
            if isinstance(r, dict) and isinstance(r.get(k), str):
                yield f"{ps}.now[{i}].{k}", r[k], PERSONA_LIMITS["now"]


def _compile_personas(raw, scope, d):
    """v7.39 인물 persona(겉·속·관계별 태도·지금) 검증. 값은 profile_base에 그대로 실린다(별도 저장 없음).

    필수는 겉 3항목·가치·낯선 사람 태도·기본 '지금'(when 없는 항목)뿐이다 — 균열·두려움·비밀·적대 태도는 선택
    (모든 인물에 어두운 면을 강제하지 않는다 · 사용자 지시)."""
    for key, prof in ((raw.get("default_npcs") or {}) if isinstance(raw.get("default_npcs"), dict) else {}).items():
        if not isinstance(prof, dict) or "persona" not in prof:
            continue
        p, ps = prof["persona"], f"default_npcs.{key}.persona"
        if not isinstance(p, dict) or set(p) - set(PERSONA_KEYS):
            d.add(ERROR, "PERSONA_INVALID", ps, f"persona 키는 {list(PERSONA_KEYS)}")
            continue
        for sec, allowed in (("surface", PERSONA_SURFACE), ("core", PERSONA_CORE), ("stances", PERSONA_STANCES)):
            v = p.get(sec)
            if not isinstance(v, dict) or set(v) - set(allowed) \
                    or any(r not in v for r in PERSONA_REQUIRED[sec]):
                d.add(ERROR, "PERSONA_INVALID", f"{ps}.{sec}",
                      f"{sec}은 {list(allowed)} 중 키(필수 {list(PERSONA_REQUIRED[sec])})")
                continue
            for k, x in v.items():
                ok = (isinstance(x, list) and 1 <= len(x) <= 5 and all(isinstance(t, str) and t.strip() for t in x)) \
                    if k == "lines" else (isinstance(x, str) and x.strip())
                if not ok:
                    d.add(ERROR, "PERSONA_INVALID", f"{ps}.{sec}.{k}",
                          "lines는 문자열 1~5개, 나머지는 비어 있지 않은 문자열")
        now = p.get("now")
        if not isinstance(now, list) or not now or not any(isinstance(r, dict) and "when" not in r for r in now):
            d.add(ERROR, "PERSONA_NOW_INVALID", f"{ps}.now", "now는 목록이고 when 없는 기본 항목이 하나 있어야 합니다")
        else:
            for i, r in enumerate(now):
                rp = f"{ps}.now[{i}]"
                if not isinstance(r, dict) or set(r) - set(PERSONA_NOW) or not isinstance(r.get("goal"), str) \
                        or not r["goal"].strip():
                    d.add(ERROR, "PERSONA_NOW_INVALID", rp, f"항목 키는 {list(PERSONA_NOW)}(goal 필수)")
                elif "when" in r:
                    if not scope.has_calendar:      # 정량 시간 없는(LEGACY) 정의 — 조건부 '지금'은 쓰이지 않는다
                        d.add(WARNING, "PERSONA_WHEN_UNUSED", f"{rp}.when", "달력 없는 정의에서는 조건부 '지금'을 쓰지 않습니다")
                    else:
                        _pred_check(r["when"], scope, f"{rp}.when", d, phase_time=False)
        for path_, text, limit in _persona_texts(p, ps):
            if len(text) > limit:
                d.add(WARNING, "PERSONA_LONG", path_, f"{len(text)}자 — 권장 {limit}자 이내(자르지 않습니다)")
        it = p.get("initial_trust", 0)
        if not isinstance(it, int) or isinstance(it, bool) or not -2 <= it <= 2:
            d.add(ERROR, "PERSONA_INVALID", f"{ps}.initial_trust", "initial_trust는 −2~+2 정수")


def _pred_check(pred, scope, path, d, *, phase_time):
    from . import predicates as _pr
    errs = _pr.validate(pred, scope, path, allow_phase_time=phase_time)
    for code, ep, msg in errs:
        d.add(ERROR, code, ep, msg)
    return not errs


def _is_int_ge(v, lo):
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def _compile_step(st, p, scope, access, edges, created, d, *, nested=False):
    """typed step 하나 검증·정규화. 일반 dotted mutation step 없음."""
    from . import cognition as _cog
    from . import events as _ev
    from . import world_refs as _refs
    if not isinstance(st, dict) or st.get("type") not in _STEP_PARAMS:
        d.add(ERROR, "STEP_TYPE_UNKNOWN", p, f"step type은 {sorted(_STEP_PARAMS)} 중 하나(자유 변이 step 없음)")
        return None
    t = st["type"]
    if nested and t not in _EFFECT_STEP_TYPES:
        d.add(ERROR, "STEP_EFFECT_TYPE_INVALID", p, f"예약 효과로 쓸 수 없는 step {t}")
        return None
    req, opt = _STEP_PARAMS[t]
    sid = st.get("step_id")
    if not is_valid_explicit_id(sid):
        d.add(ERROR, "STEP_ID_INVALID", f"{p}.step_id", "step_id는 명시 id 형식")
        return None
    extra = set(st) - {"type", "step_id", "conflict_priority"} - set(req) - set(opt)
    missing = [k for k in req if k not in st]
    if extra or missing:
        d.add(ERROR, "STEP_PARAMS_INVALID", p, f"{t}: 알 수 없는 {sorted(extra)} / 누락 {missing}")
        return None
    ok = True

    def bad(code, msg):
        nonlocal ok
        d.add(ERROR, code, p, msg)
        ok = False

    def ref_id(v, kind, pool, slot_type, allow_step=False):
        if isinstance(v, str) and v.startswith("$step:") and allow_step:
            if v[6:] not in created:
                bad("STEP_REF_UNKNOWN", f"$step:{v[6:]}는 같은 케이스의 앞선 정보 생성 step이어야 합니다")
            return
        if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX):
            name = v[len(_refs.SLOT_PREFIX):]
            if scope.slots.get(name) != slot_type:
                bad("STEP_REF_UNKNOWN", f"{kind} 슬롯 {name!r}이 없거나 타입이 {slot_type}이 아닙니다")
            return
        if not isinstance(v, str) or v not in pool:
            bad("STEP_REF_UNKNOWN", f"정의되지 않은 {kind} {v!r}")

    def holder(v):
        if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX):
            ref_id(v, "holder", None, "ACTOR")
        elif _cog.parse_holder(v) is None:
            bad("STEP_REF_UNKNOWN", f"holder {v!r}는 'actor:<id>'·'group:<id>'·'$slot:<name>'")
        elif v.startswith("actor:") and v[6:] not in scope.actors:
            bad("STEP_REF_UNKNOWN", f"정의되지 않은 ActorId {v[6:]!r}")

    pr = st.get("conflict_priority", 0)
    if not isinstance(pr, int) or isinstance(pr, bool):
        bad("STEP_PRIORITY_INVALID", "conflict_priority는 정수")
    out = {k: copy.deepcopy(v) for k, v in st.items()}
    out["conflict_priority"] = pr if isinstance(pr, int) else 0
    if t == "SET_ACCESS_STATE":
        c = (access.get("controls") or {}).get(st["control"])
        if c is None:
            bad("STEP_REF_UNKNOWN", f"정의되지 않은 접근 제어 {st['control']!r}")
        elif st["state"] not in access["rules"][c["rule_ref"]]["states"]:
            bad("STEP_REF_UNKNOWN", f"제어 {st['control']}의 규칙에 없는 상태 {st['state']!r}")
    elif t == "SET_ACTOR_PLACEMENT":
        ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
        ref_id(st["place"], "PlaceId", scope.places, "PLACE")
    elif t == "CLEAR_ACTOR_PLACEMENT":
        ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
    elif t in ("SET_ACTOR_STATUS", "CLEAR_ACTOR_STATUS"):
        ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
        if st.get("status") is not None and scope.statuses is not None and st["status"] not in scope.statuses:
            bad("STEP_REF_UNKNOWN", f"정의되지 않은 상태 {st['status']!r}(공용·시나리오 상태, 기능 선언 포함)")
    elif t == "GRANT_GROWTH":
        if "growth" not in (_CAPS or ()):
            bad("STEP_CAPABILITY_REQUIRED", "GRANT_GROWTH에는 growth capability 선언이 필요합니다")
        if not (isinstance(st["points"], int) and not isinstance(st["points"], bool) and st["points"] > 0):
            bad("STEP_PARAMS_INVALID", "points는 양의 정수")
    elif t == "SET_PC_RECORD":
        if "growth" not in (_CAPS or ()):
            bad("STEP_CAPABILITY_REQUIRED", "SET_PC_RECORD에는 growth capability 선언이 필요합니다")
        if st["field"] not in (_PC_RECORDS or {}):
            bad("STEP_REF_UNKNOWN", f"growth.records에 선언되지 않은 기록 {st['field']!r}")
        elif not (isinstance(st["value"], int) and not isinstance(st["value"], bool)):
            bad("STEP_PARAMS_INVALID", "value는 정수(등급은 1부터 — growth.records 이름표 순서)")
    elif t == "GRANT_ITEM":
        if not (isinstance(st["key"], str) and st["key"].strip()):
            bad("STEP_PARAMS_INVALID", "key는 물건 이름(문자열)")
        if not (isinstance(st["quantity"], int) and not isinstance(st["quantity"], bool) and st["quantity"] > 0):
            bad("STEP_PARAMS_INVALID", "quantity는 양의 정수")
        if ("actor" in st) == ("place" in st):
            bad("STEP_PARAMS_INVALID", "actor(받는 인물 — ActorId 또는 $session_pc)와 place(놓을 장소) 중 하나만")
        elif "place" in st:
            ref_id(st["place"], "PlaceId", scope.places, "PLACE")
        elif st["actor"] != _refs.SESSION_PC:
            ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
    elif t in ("JOIN_PARTY", "LEAVE_PARTY"):
        if "party" not in (_CAPS or ()):
            bad("STEP_CAPABILITY_REQUIRED", f"{t}에는 party capability 선언이 필요합니다")
        ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
        if isinstance(st["actor"], str) and st["actor"] in (_PARTY_ACTORS or {}) and not _PARTY_ACTORS[st["actor"]]:
            bad("STEP_REF_UNKNOWN", f"인물 {st['actor']!r}에게 party(역할)가 없습니다")
    elif t == "START_MOVEMENT":
        ref_id(st["actor"], "ActorId", scope.actors, "ACTOR")
        ref_id(st["destination"], "PlaceId", scope.places, "PLACE")
        ref_id(st["mode"], "mode_id", scope.modes, None)
        if "stance_factor" in st and fixed_decimal(st["stance_factor"]) is None:
            bad("STEP_PARAMS_INVALID", "stance_factor는 양의 소수 6자리 이내")
        route = st.get("route") or []
        for e in route:
            if e not in edges:
                bad("STEP_REF_UNKNOWN", f"정의되지 않은 구간 {e!r}")
        if route and all(e in edges for e in route):
            for a, b in zip(route, route[1:]):
                if edges[a]["to"] != edges[b]["from"]:
                    bad("STEP_ROUTE_DISCONTINUOUS", f"route가 끊겼습니다: {a}(→{edges[a]['to']}) 다음 "
                                                    f"{b}({edges[b]['from']}→)")
            dest = st["destination"]
            if isinstance(dest, str) and not dest.startswith(_refs.SLOT_PREFIX) and edges[route[-1]]["to"] != dest:
                bad("STEP_ROUTE_DISCONTINUOUS", f"route 끝 {edges[route[-1]]['to']!r}이 목적지 {dest!r}가 아닙니다")
    elif t in ("CREATE_INFORMATION", "SUPERSEDE_INFORMATION"):
        if not isinstance(st["proposition"], str) or not st["proposition"].strip():
            bad("STEP_PARAMS_INVALID", "proposition은 비어 있지 않은 문자열")
        for r in st.get("subject_refs") or []:
            kind, _, ident = r.partition(":") if isinstance(r, str) else ("", "", "")
            if not ((kind == "actor" and ident in scope.actors) or (kind == "place" and ident in scope.places)):
                bad("STEP_REF_UNKNOWN", f"subject_ref {r!r}는 실재 'actor:'·'place:' 참조")
        if t == "SUPERSEDE_INFORMATION":
            ref_id(st["info"], "InfoId", scope.infos, "INFO", allow_step=True)
            if st.get("validity", "SUPERSEDED") not in ("SUPERSEDED", "REFUTED"):
                bad("STEP_PARAMS_INVALID", "validity는 SUPERSEDED|REFUTED")
        created.add(sid)
    elif t == "ACQUIRE_INFORMATION":
        holder(st["holder"])
        ref_id(st["info"], "InfoId", scope.infos, "INFO", allow_step=True)
        if st.get("awareness", "CONTENT") not in _cog.AWARENESS:
            bad("STEP_PARAMS_INVALID", f"awareness는 {list(_cog.AWARENESS)}")
        if st.get("route", "PARTICIPATION") not in _cog.ROUTES:
            bad("STEP_PARAMS_INVALID", f"route는 {list(_cog.ROUTES)}")
    elif t == "SET_COGNITION_BELIEF":
        holder(st["holder"])
        ref_id(st["info"], "InfoId", scope.infos, "INFO", allow_step=True)
        if st["belief"] is not None and st["belief"] not in _cog.BELIEFS:
            bad("STEP_PARAMS_INVALID", f"belief는 null 또는 {list(_cog.BELIEFS)}")
        auth = st.get("authority", "RULE")
        if auth not in _cog.BELIEF_AUTHORITIES:
            bad("STEP_PARAMS_INVALID", f"authority는 {list(_cog.BELIEF_AUTHORITIES)}")
        elif isinstance(st["holder"], str) and st["holder"].startswith("actor:pc_") \
                and auth not in _cog.PC_BELIEF_AUTHORITIES:
            bad("STEP_PC_BELIEF_AUTHORITY", "PC 믿음은 PLAYER_EXPLICIT·RULE_FORCED로만 바꿉니다(사건 노출 불가)")
    elif t == "APPEND_RELATIONSHIP_DELTA":
        holder(st["from"])
        holder(st["to"])
        if not isinstance(st["kind"], str) or not st["kind"]:
            bad("STEP_PARAMS_INVALID", "kind는 비어 있지 않은 문자열")
    elif t == "SET_EVENT_LOCAL":
        if not isinstance(st["key"], str) or not st["key"]:
            bad("STEP_PARAMS_INVALID", "key는 비어 있지 않은 문자열")
        elif st["key"] == "resolution":
            bad("EVENT_LOCAL_RESERVED_KEY", "'resolution'은 플레이어 명시 결단만 쓰는 예약 키입니다(저작 step 금지)")
        try:
            canonical_json(st["value"])
        except CanonicalSerializationError:
            bad("STEP_PARAMS_INVALID", "value는 정규 JSON 값")
    elif t in ("SCHEDULE_WAKEUP", "SCHEDULE_EFFECT"):
        if not _is_int_ge(st["after_minutes"], 1):
            bad("STEP_PARAMS_INVALID", "after_minutes는 1 이상의 정수(같은 분 효과는 즉시 step으로)")
        if t == "SCHEDULE_EFFECT":
            eff = dict(st["effect"]) if isinstance(st["effect"], dict) else st["effect"]
            if isinstance(eff, dict):
                eff.setdefault("step_id", f"{sid}.effect")
            ne = _compile_step(eff, f"{p}.effect", scope, access, edges, set(created), d, nested=True)
            if ne is None:
                ok = False
            out["effect"] = ne
            if st.get("guard") is not None and not _pred_check(st["guard"], scope, f"{p}.guard", d, phase_time=True):
                ok = False
            if "epoch_bound" in st and not isinstance(st["epoch_bound"], bool):
                bad("STEP_PARAMS_INVALID", "epoch_bound는 true/false")
    elif t == "EMIT_SIGNAL":
        if not is_valid_explicit_id(st["signal_id"]) or not isinstance(st["cue"], str) or not st["cue"].strip():
            bad("STEP_PARAMS_INVALID", "signal_id(명시 id)·cue(문자열) 필요")
        if st.get("interrupt_policy", "NONE") not in _ev.INTERRUPT_POLICIES:
            bad("STEP_PARAMS_INVALID", f"interrupt_policy는 {list(_ev.INTERRUPT_POLICIES)}")
        if st.get("observable_when") is not None and not _pred_check(
                st["observable_when"], scope, f"{p}.observable_when", d, phase_time=True):
            ok = False
        if "exposes" in st:
            ex = st["exposes"]
            if not isinstance(ex, list) or not ex or len(set(map(str, ex))) != len(ex):
                bad("STEP_PARAMS_INVALID", "exposes는 중복 없는 InfoId(또는 INFO 슬롯) 목록")
            else:
                for v in ex:
                    ref_id(v, "InfoId", scope.infos, "INFO")
    return out if ok else None


def _exposed_infos(steps):
    """케이스 step(중첩 예약 효과 포함)의 EMIT_SIGNAL.exposes 중 슬롯이 아닌 InfoId."""
    for st in steps or ():
        if st.get("type") == "SCHEDULE_EFFECT" and isinstance(st.get("effect"), dict):
            yield from _exposed_infos([st["effect"]])
        elif st.get("type") == "EMIT_SIGNAL":
            for v in st.get("exposes") or ():
                if isinstance(v, str) and not v.startswith("$"):
                    yield v


def _fixed_write_key(st):
    t = st["type"]
    if t == "SET_ACCESS_STATE":
        return ("access", st["control"]), st["state"]
    if t == "SET_ACTOR_PLACEMENT" and not str(st["actor"]).startswith("$"):
        return ("placement", st["actor"]), st["place"]
    if t == "CLEAR_ACTOR_PLACEMENT" and not str(st["actor"]).startswith("$"):
        return ("placement", st["actor"]), "\x00CLEAR"
    if t == "SET_EVENT_LOCAL":
        return ("local", st["key"]), canonical_json(st["value"])
    return None, None


def _compile_behaviors(actors, places, modes, caps, d, *, calendar=None, templates=()) -> list:
    """v7.48 default_npcs[*].behavior — 인물 행동 유형(routine 옆 자리). 유형마다 그 기능 선언이 필요하다.
    검증에 실패한 behavior는 정의에서 뺀다(routine과 같은 처리). 반환: 추적자 ActorId 목록(정렬)."""
    from . import pursuit as _pu
    from . import predicates as _pr
    out = []
    for aid in sorted(actors):
        a = actors[aid]
        if "behavior" not in a:
            continue
        b, p = a["behavior"], f"default_npcs.{a['legacy_key']}.behavior"
        errs = []
        if not isinstance(b, dict) or b.get("type") != _pu.BEHAVIOR_TYPE:
            errs.append((f"{p}.type", f"행동 유형은 {_pu.BEHAVIOR_TYPE!r}만 있습니다"))
        elif _pu.CAPABILITY not in caps:
            errs.append((p, f"행동 유형 {_pu.BEHAVIOR_TYPE}에는 {_pu.CAPABILITY} capability 선언이 필요합니다"))
        else:
            errs += _pu.check(b, path=p, places=set(places), modes=set(modes or {}))
            if b.get("active_when") is not None:
                errs += [(path, msg) for _code, path, msg in _pr.validate(
                    b["active_when"], _ref_scope(actors, places, {}, {}, modes, calendar=calendar, templates=templates),
                    f"{p}.active_when", allow_phase_time=False)]
        for path, msg in errs:
            d.add(ERROR, "BEHAVIOR_INVALID", path, msg)
        if errs:
            del a["behavior"]
        else:
            out.append(aid)
    return out


def _compile_party(raw, actors, caps, d, scope) -> dict | None:
    """v7.51 NPC 파티 — 시나리오 party_roles·party_limit, 인물 party·stance 검증. 실패한 인물 키는 정의에서 뺀다.
    반환: 정의 키 party {roles, limit}(기능 party 선언 시) 또는 None."""
    from . import party as _pa
    from . import predicates as _pr
    from .items import effect_vocab
    vocab = effect_vocab()
    roles = raw.get("party_roles")
    cfg = None
    if "party" in caps:
        errs = _pa.check_roles(roles, statuses=set(_STATUS_DEFS or {}), domains=set(vocab.get("domains") or {}) | {"기타"},
                               effects=set(vocab.get("use_effects") or {}))
        lim = raw.get("party_limit", _pa.DEFAULT_LIMIT)
        if not (isinstance(lim, int) and not isinstance(lim, bool) and 1 <= lim <= 8):
            errs.append(("party_limit", "1~8 정수"))
        for path, msg in errs:
            d.add(ERROR, "PARTY_INVALID", path, msg)
        if not errs:
            cfg = {"roles": copy.deepcopy(roles), "limit": lim}
    elif roles is not None or "party_limit" in raw:
        d.add(ERROR, "PARTY_INVALID", "party_roles", "party_roles·party_limit에는 party capability 선언이 필요합니다")
    for aid in sorted(actors):
        a = actors[aid]
        base = f"default_npcs.{a['legacy_key']}"
        if "party" in a:
            p = f"{base}.party"
            errs = ([(p, "party에는 party capability 선언이 필요합니다")] if "party" not in caps
                    else _pa.check_member(a["party"], (cfg or {}).get("roles") or {}, p))
            if not errs and a["party"].get("join_when") is not None:
                errs = [(path, msg) for _c, path, msg in _pr.validate(a["party"]["join_when"], scope, f"{p}.join_when",
                                                                       allow_phase_time=False)]
            for path, msg in errs:
                d.add(ERROR, "PARTY_MEMBER_INVALID", path, msg)
            if errs:
                del a["party"]
        if "stance" in a:
            p = f"{base}.stance"
            errs = ([(p, "stance에는 combat_wounds capability 선언이 필요합니다")] if "combat_wounds" not in caps
                    else _pa.check_stance(a["stance"], p))
            for path, msg in errs:
                d.add(ERROR, "STANCE_INVALID", path, msg)
            if errs:
                del a["stance"]
    return cfg


def _compile_event_templates(raw, caps, actors, actor_index, places, infos, access, modes, edges, calendar, d):
    from . import events as _ev
    from . import predicates as _pr
    from . import world_refs as _refs
    src = raw.get("event_templates")
    out = {}
    if src is None:
        return out
    if not isinstance(src, dict):
        d.add(ERROR, "EVENT_TEMPLATES_INVALID", "event_templates", "{template_id: 템플릿} 객체여야 합니다")
        return out
    if "dynamic_events" not in caps:
        d.add(WARNING, "EVENTS_WITHOUT_CAPABILITY", "event_templates",
              "dynamic_events capability 없이 사건 템플릿이 저작됐습니다")
    tids = set(src)
    for tid, body in src.items():
        p = f"event_templates.{tid}"
        if not is_valid_explicit_id(tid) or not isinstance(body, dict):
            d.add(ERROR, "EVENT_TEMPLATE_INVALID", p, "template_id는 명시 id, 템플릿은 객체")
            continue
        n_err = len([x for x in d.items if x.severity == ERROR])
        for k in body:
            if k not in _TPL_KEYS:
                d.add(ERROR, "EVENT_TEMPLATE_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 템플릿 키(자유 스크립트 없음)")
        ver = body.get("version", 1)
        if not _is_int_ge(ver, 1):
            d.add(ERROR, "EVENT_VERSION_INVALID", f"{p}.version", "version은 1 이상의 정수")
        # 슬롯
        slots, slot_types = {}, {}
        for name, sd in (body.get("slots") or {}).items():
            sp = f"{p}.slots.{name}"
            if not is_valid_explicit_id(name) or not isinstance(sd, dict) or set(sd) != {"type", "candidates"} \
                    or sd.get("type") not in _ev.SLOT_TYPES or not isinstance(sd.get("candidates"), list) \
                    or not sd["candidates"]:
                d.add(ERROR, "EVENT_SLOT_INVALID", sp,
                      f"슬롯은 {{type: {list(_ev.SLOT_TYPES)}, candidates: [비어 있지 않은 목록]}} (LLM binding 없음)")
                continue
            pool = {"ACTOR": actors, "PLACE": places, "INFO": infos}[sd["type"]]
            cands = []
            for c in sd["candidates"]:
                cid = actor_index.get(c, c) if sd["type"] == "ACTOR" else c
                if cid not in pool:
                    d.add(ERROR, "EVENT_SLOT_CANDIDATE_UNKNOWN", sp, f"정의되지 않은 {sd['type']} 후보 {c!r}")
                    continue
                cands.append(cid)
            slots[name] = {"type": sd["type"], "candidates": sorted(set(cands))}
            slot_types[name] = sd["type"]
        distinct = []
        dist = body.get("distinct") or []
        if dist == "all":
            dist = [list(x) for x in itertools.combinations(sorted(slots), 2)]
        for pair in dist if isinstance(dist, list) else [None]:
            if not (isinstance(pair, list) and len(pair) == 2 and all(x in slots for x in pair) and pair[0] != pair[1]):
                d.add(ERROR, "EVENT_DISTINCT_INVALID", f"{p}.distinct", "distinct는 서로 다른 슬롯 이름 쌍 목록 또는 'all'")
                continue
            distinct.append(sorted(pair))
        scope = _ref_scope(actors, places, infos, access, modes, slots=slot_types, allow_self=True,
                           templates=tids, calendar=calendar)
        if "spawn_when" not in body:
            d.add(ERROR, "EVENT_SPAWN_REQUIRED", p, "spawn_when(타입 조건)이 필요합니다")
        else:
            _pred_check(body["spawn_when"], scope, f"{p}.spawn_when", d, phase_time=False)
        pol = body.get("repeat_policy", "ONCE_PER_SESSION")
        if pol not in _ev.REPEAT_POLICIES:
            d.add(ERROR, "EVENT_REPEAT_INVALID", f"{p}.repeat_policy", f"repeat_policy는 {list(_ev.REPEAT_POLICIES)}")
        mx = body.get("max_occurrences")
        if mx is not None and (pol != "REPEATABLE" or not _is_int_ge(mx, 1)):
            d.add(ERROR, "EVENT_MAX_OCCURRENCES_INVALID", f"{p}.max_occurrences",
                  "max_occurrences는 REPEATABLE의 1 이상 정수")
        rel = body.get("relevance_places")
        if rel is not None:
            if not isinstance(rel, list) or not rel or len(set(map(str, rel))) != len(rel):
                d.add(ERROR, "EVENT_RELEVANCE_INVALID", f"{p}.relevance_places",
                      "relevance_places는 중복 없는 PlaceId(또는 PLACE 슬롯) 목록")
            else:
                for v in rel:
                    if isinstance(v, str) and v.startswith(_refs.SLOT_PREFIX):
                        if slot_types.get(v[len(_refs.SLOT_PREFIX):]) != "PLACE":
                            d.add(ERROR, "EVENT_RELEVANCE_INVALID", f"{p}.relevance_places",
                                  f"{v!r}는 PLACE 슬롯이 아닙니다")
                    elif v not in places:
                        d.add(ERROR, "EVENT_RELEVANCE_INVALID", f"{p}.relevance_places", f"정의되지 않은 장소 {v!r}")
        for k in ("access_when", "resolve_when", "cancel_when"):
            if body.get(k) is not None:
                _pred_check(body[k], scope, f"{p}.{k}", d, phase_time=True)
        sel = body.get("phase_selector") or {}
        if not isinstance(sel, dict) or set(sel) - {"mode", "stability", "fallback_phase"}:
            d.add(ERROR, "EVENT_SELECTOR_INVALID", f"{p}.phase_selector", "phase_selector는 {mode, stability, fallback_phase}")
            sel = {}
        mode = sel.get("mode", "PRIORITY")
        stab = sel.get("stability", "STICKY")                 # 사용자 결정: 기본 STICKY
        if mode not in _ev.SELECTOR_MODES:
            d.add(ERROR, "EVENT_SELECTOR_INVALID", f"{p}.phase_selector.mode", f"mode는 {list(_ev.SELECTOR_MODES)}")
        if stab not in _ev.STABILITIES:
            d.add(ERROR, "EVENT_SELECTOR_INVALID", f"{p}.phase_selector.stability", f"stability는 {list(_ev.STABILITIES)}")
        phases_raw = body.get("phases")
        phases = {}
        if not isinstance(phases_raw, dict) or not phases_raw:
            d.add(ERROR, "EVENT_PHASES_REQUIRED", f"{p}.phases", "phases가 필요합니다")
            phases_raw = {}
        for pid, ph in phases_raw.items():
            pp = f"{p}.phases.{pid}"
            if not is_valid_explicit_id(pid) or not isinstance(ph, dict) or set(ph) - set(_PHASE_KEYS):
                d.add(ERROR, "EVENT_PHASE_INVALID", pp, f"국면 키는 {list(_PHASE_KEYS)}")
                continue
            if "eligible_when" not in ph:
                d.add(ERROR, "EVENT_PHASE_INVALID", pp, "eligible_when이 필요합니다")
                continue
            _pred_check(ph["eligible_when"], scope, f"{pp}.eligible_when", d, phase_time=True)
            if ph.get("retain_when") is not None:
                _pred_check(ph["retain_when"], scope, f"{pp}.retain_when", d, phase_time=True)
            if not isinstance(ph.get("priority"), int) or isinstance(ph.get("priority"), bool):
                d.add(ERROR, "EVENT_PHASE_PRIORITY_REQUIRED", pp, "priority(정수)가 필요합니다 — 동점 해소에 JSON 순서를 쓰지 않습니다")
            terms = []
            for j, tm in enumerate(ph.get("score_terms") or []):
                if not isinstance(tm, dict) or set(tm) != {"when", "points"} or not isinstance(tm["points"], int) \
                        or isinstance(tm["points"], bool):
                    d.add(ERROR, "EVENT_SCORE_TERM_INVALID", f"{pp}.score_terms[{j}]", "{when, points(정수)}")
                    continue
                _pred_check(tm["when"], scope, f"{pp}.score_terms[{j}].when", d, phase_time=True)
                terms.append(copy.deepcopy(tm))
            if terms and mode != "SCORE":
                d.add(WARNING, "EVENT_SCORE_TERMS_UNUSED", pp, "PRIORITY 모드에서 score_terms는 쓰이지 않습니다")
            if ph.get("base_score") is not None and (not isinstance(ph["base_score"], int) or isinstance(ph["base_score"], bool)):
                d.add(ERROR, "EVENT_SCORE_TERM_INVALID", f"{pp}.base_score", "base_score는 정수")
            if stab == "MONOTONIC" and not isinstance(ph.get("rank"), int):
                d.add(ERROR, "EVENT_PHASE_RANK_REQUIRED", pp, "MONOTONIC 안정성에는 모든 국면의 rank가 필요합니다")
            if "terminal" in ph and not isinstance(ph["terminal"], bool):
                d.add(ERROR, "EVENT_PHASE_INVALID", f"{pp}.terminal", "terminal은 true/false")
            phases[pid] = {"eligible_when": copy.deepcopy(ph["eligible_when"]),
                           "retain_when": copy.deepcopy(ph.get("retain_when")),
                           "priority": ph.get("priority"), "base_score": ph.get("base_score") or 0,
                           "score_terms": terms, "rank": ph.get("rank"), "terminal": bool(ph.get("terminal"))}
            if "guide" in ph:
                if not isinstance(ph["guide"], str) or not ph["guide"].strip():
                    d.add(ERROR, "EVENT_PHASE_GUIDE_INVALID", f"{pp}.guide", "guide는 비어 있지 않은 문자열")
                else:
                    phases[pid]["guide"] = ph["guide"]
            if "finds" in ph:                       # v7.38(A3): 이 국면에서 찾을 수 있는 물건 후보(코드가 하나를 고른다)
                fs = ph["finds"]                    # v7.44(E4): {info, object?, place?} — 조사하면 드러나는 저작 단서(정본)

                def _find_ok(x):
                    if isinstance(x, str):
                        return bool(x.strip())
                    summ = (infos.get(x.get("info")) or {}).get("summary") if isinstance(x, dict) else None
                    return (isinstance(x, dict) and set(x) - {"object", "place", "actor"} == {"info"}
                            and isinstance(summ, str) and bool(summ.strip())
                            and ("object" not in x or (isinstance(x["object"], str) and bool(x["object"].strip())))
                            and ("place" not in x or x["place"] in places)
                            and ("actor" not in x or x["actor"] in actor_index))
                if not (isinstance(fs, list) and fs and all(_find_ok(x) for x in fs)):
                    d.add(ERROR, "EVENT_PHASE_FINDS_INVALID", f"{pp}.finds",
                          "finds는 비어 있지 않은 목록 — 문자열 또는 {info(summary 있는 정보), object?, place?}")
                else:
                    # v7.44 B1: actor = 그 인물이 장면에 있을 때 묻거나 살피면 드러나는 단서(증언·인물 관찰)
                    phases[pid]["finds"] = [x if isinstance(x, str) else
                                            dict(x, **({"actor_id": actor_index[x["actor"]]} if "actor" in x else {}))
                                            for x in fs]
            if "ending" in ph:                      # v7.44(E3): 이 국면에 들어가면 이야기가 끝난다(에필로그 → 세션 종료)
                en = ph["ending"]
                if not (isinstance(en, dict) and set(en) - {"tone", "chapter"} == {"id", "title", "epilogue"}
                        and is_valid_explicit_id(en["id"])
                        and all(isinstance(en[k], str) and en[k].strip() for k in ("title", "epilogue"))
                        and isinstance(en.get("tone", ""), str) and isinstance(en.get("chapter", False), bool)):
                    d.add(ERROR, "EVENT_PHASE_ENDING_INVALID", f"{pp}.ending",
                          "ending은 {id(명시 id), title, epilogue(지시층위 전용 맺음 지침), tone?, chapter?(v7.50 장 결말)}")
                else:
                    phases[pid]["ending"] = dict(en)
            if "resolution_options" in ph:
                ro = ph["resolution_options"]
                okr = isinstance(ro, list) and ro and all(
                    isinstance(o, dict) and set(o) - {"target"} == {"option_id", "label"}
                    and is_valid_explicit_id(o["option_id"])
                    and isinstance(o["label"], str) and o["label"].strip() for o in ro)
                if not okr or len({o["option_id"] for o in ro}) != len(ro):
                    d.add(ERROR, "EVENT_RESOLUTION_OPTIONS_INVALID", f"{pp}.resolution_options",
                          "resolution_options는 중복 없는 [{option_id(명시 id), label(문자열), target?}] 목록")
                else:
                    opts = []
                    for oi, o in enumerate(ro):
                        row = {"option_id": o["option_id"], "label": o["label"]}
                        if "target" in o:
                            tg = _compile_resolution_target(o["target"], actor_index,
                                                            f"{pp}.resolution_options[{oi}].target", d)
                            if tg is None:
                                continue
                            row["target"] = tg
                        opts.append(row)
                    phases[pid]["resolution_options"] = opts
        prios = {}
        for pid in sorted(phases):
            pr = phases[pid]["priority"]
            if isinstance(pr, int) and pr in prios:
                d.add(ERROR, "EVENT_PHASE_PRIORITY_TIE", f"{p}.phases",
                      f"국면 {prios[pr]}·{pid}의 priority {pr} 동점 — 삽입 순서로 결정하지 않습니다")
            prios.setdefault(pr, pid)
        fb = sel.get("fallback_phase")
        if fb is not None and fb not in phases:
            d.add(ERROR, "EVENT_FALLBACK_UNKNOWN", f"{p}.phase_selector.fallback_phase", f"없는 국면 {fb!r}")
        if fb is None and not any(ph["eligible_when"] == {"const": True} for ph in phases.values()):
            d.add(WARNING, "EVENT_NO_FALLBACK", p, "fallback_phase·항상 참 국면이 없어 실행 중 '선택 불가' 진단이 날 수 있습니다")
        # 케이스
        cases, seen = [], set()
        for j, c in enumerate(body.get("transition_cases") or []):
            cp = f"{p}.transition_cases[{j}]"
            if not isinstance(c, dict) or set(c) - set(_CASE_KEYS) or not is_valid_explicit_id(c.get("case_id")):
                d.add(ERROR, "EVENT_CASE_INVALID", cp, f"케이스 키는 {list(_CASE_KEYS)}, case_id는 명시 id")
                continue
            if c["case_id"] in seen:
                d.add(ERROR, "EVENT_CASE_DUPLICATE", cp, f"case_id {c['case_id']} 중복")
                continue
            seen.add(c["case_id"])
            frm, to = c.get("from", "*"), c.get("to")
            if to not in phases or not (frm is None or frm == "*" or frm in phases):
                d.add(ERROR, "EVENT_CASE_PHASE_UNKNOWN", cp, f"from {frm!r}/to {to!r}가 국면이 아닙니다(from null = spawn 진입)")
                continue
            w = c.get("weight", 1)
            if not _is_int_ge(w, 1):
                d.add(ERROR, "EVENT_CASE_WEIGHT_INVALID", cp, "weight는 양의 정수(0으로 비활성화하지 말고 valid_when 사용)")
                continue
            if c.get("valid_when") is not None:
                _pred_check(c["valid_when"], scope, f"{cp}.valid_when", d, phase_time=True)
            steps, ids, created, fixed = [], set(), set(), {}
            for k, st in enumerate(c.get("steps") or []):
                ns = _compile_step(st, f"{cp}.steps[{k}]", scope, access, edges, created, d)
                if ns is None:
                    continue
                if ns["step_id"] in ids:
                    d.add(ERROR, "STEP_ID_DUPLICATE", f"{cp}.steps[{k}]", f"step_id {ns['step_id']} 중복")
                    continue
                ids.add(ns["step_id"])
                key, val = _fixed_write_key(ns)
                if key is not None:
                    if key in fixed and fixed[key] != val:
                        d.add(ERROR, "STEP_FIXED_WRITE_CONFLICT", f"{cp}.steps[{k}]",
                              f"같은 케이스에서 {key[0]} {key[1]!r}에 서로 다른 고정값을 씁니다")
                    fixed.setdefault(key, val)
                steps.append(ns)
            for iid in _exposed_infos(steps):
                summ = (infos.get(iid) or {}).get("summary")
                if not isinstance(summ, str) or not summ.strip():
                    d.add(ERROR, "SIGNAL_EXPOSES_SUMMARY_REQUIRED", cp,
                          f"노출 정보 {iid!r}에 공개 안전 summary가 없습니다(추출층위에 명제 전문을 주지 않습니다)")
            cases.append({"case_id": c["case_id"], "from": frm, "to": to,
                          "valid_when": copy.deepcopy(c.get("valid_when")), "weight": w, "steps": steps})
        case_sel = body.get("case_selection", "DETERMINISTIC_WEIGHTED")
        if case_sel not in _ev.CASE_SELECTIONS:
            d.add(ERROR, "EVENT_CASE_SELECTION_INVALID", f"{p}.case_selection", f"{list(_ev.CASE_SELECTIONS)}")
        if body.get("case_required"):
            for frm, to in _uncovered_transitions(phases, cases, stab):
                d.add(ERROR, "EVENT_CASE_REQUIRED_UNSATISFIABLE", f"{p}.transition_cases",
                      f"case_required인데 전이 {frm or 'spawn'} → {to}의 케이스 후보가 없습니다(충족 불가)")
        if len([x for x in d.items if x.severity == ERROR]) > n_err:
            continue
        inst_preds = [body.get("resolve_when"), body.get("cancel_when")]
        for ph in phases.values():
            inst_preds += [ph["eligible_when"], ph["retain_when"]] + [t["when"] for t in ph["score_terms"]]
        inst_preds += [c["valid_when"] for c in cases]
        inst_keys = sorted({k for pr in inst_preds if pr for k in _pr.dependency_keys(pr)})
        spawn_keys = _pr.dependency_keys(body["spawn_when"])
        wild = sorted({k for k in inst_keys + spawn_keys if _refs.is_wildcard(k)})
        if wild:
            d.add(WARNING, "DEPENDENCY_WILDCARD", p, f"넓은 의존 키 {wild} — 영향 범위가 커집니다")
        out[tid] = {"template_id": tid, "version": ver, "name": body.get("name"), "slots": slots,
                    "distinct": distinct, "spawn_when": copy.deepcopy(body["spawn_when"]), "repeat_policy": pol,
                    "max_occurrences": mx, "access_when": copy.deepcopy(body.get("access_when")),
                    "phase_selector": {"mode": mode, "stability": stab, "fallback_phase": fb},
                    "phases": phases, "transition_cases": cases, "case_selection": case_sel,
                    "case_required": bool(body.get("case_required")),
                    "resolve_when": copy.deepcopy(body.get("resolve_when")),
                    "cancel_when": copy.deepcopy(body.get("cancel_when")),
                    **({"relevance_places": list(rel)} if rel is not None else {}),
                    "dependency": {"spawn": spawn_keys, "instance": inst_keys, "wildcards": wild},
                    "has_time_predicates": any(_pr.time_atoms(x) for x in inst_preds + [body["spawn_when"]] if x),
                    "source_ref": ["event_templates", tid]}
    return out


def _uncovered_transitions(phases: dict, cases: list, stability: str) -> list:
    """구조상 일어날 수 있는 전이(spawn 진입 + 비종결 국면 → 다른 국면, MONOTONIC은 rank 비감소) 중
    케이스 후보가 하나도 없는 (from, to). case_required에서 이런 전이는 런타임에 반드시 실패한다."""
    out = []
    for to in sorted(phases):
        sources = [None] + [f for f in sorted(phases) if f != to and not phases[f]["terminal"]]
        for frm in sources:
            if frm is not None and stability == "MONOTONIC" and \
                    (phases[to]["rank"] or 0) < (phases[frm]["rank"] or 0):
                continue
            if not any(c["to"] == to and (c["from"] == "*" or c["from"] == frm) for c in cases):
                out.append((frm, to))
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


_ROUTINE_KEYS = ("entries", "default_place")
_ROUTINE_ENTRY_KEYS = ("place", "days", "dayparts", "from", "to")


def _compile_routines(actors, places, calendar, d):
    """default_npcs[*].routine → 정규화(H07). 선택 키 — 미저작 인물·시나리오의 정의는 그대로다.

    raw:  {"entries": [{"place": PlaceId, "days"?: ["MON".."SUN"], "dayparts"?: [DaypartId] |
                        "from"?: "HH:MM", "to"?: "HH:MM"}], "default_place"?: PlaceId}
    정규: {"entries": [{"place", "days": [0..6]|None, "windows": [[start,end],...]|None}], "default_place"|None}
      · 해석 = 저작 순서 첫 일치(결정적). 일치가 없으면 default_place, 그것도 없으면 untracked.
      · windows는 하루 분 [start, end) 반열림, start > end는 자정을 넘는다. 요일은 창이 시작한 날 기준.
      · 요일은 주(週)가 정의된 달력(GREGORIAN)에서만 — FIXED_CUSTOM에 요일을 쓰면 ERROR.
      · 정본 배치를 만들지 않는다(routine은 파생 위치 — core.npc_position).
    """
    from . import world_clock as _wc
    for aid in sorted(actors):
        a = actors[aid]
        if "routine" not in a:
            continue
        r = a["routine"]
        p = f"default_npcs.{a['legacy_key']}.routine"
        if calendar is None:
            d.add(ERROR, "ROUTINE_REQUIRES_CALENDAR", p, "routine에는 정량 시간 달력(calendar)이 필요합니다")
            del a["routine"]
            continue
        cal = _wc.Calendar(calendar)
        if not isinstance(r, dict) or set(r) - set(_ROUTINE_KEYS) or not isinstance(r.get("entries", []), list):
            d.add(ERROR, "ROUTINE_INVALID", p, f"routine은 {list(_ROUTINE_KEYS)} 키의 객체여야 합니다")
            del a["routine"]
            continue
        bad = False
        entries = []
        for i, e in enumerate(r.get("entries") or []):
            ep = f"{p}.entries[{i}]"
            if not isinstance(e, dict) or set(e) - set(_ROUTINE_ENTRY_KEYS) or "place" not in e:
                d.add(ERROR, "ROUTINE_ENTRY_INVALID", ep, f"항목은 place와 {list(_ROUTINE_ENTRY_KEYS[1:])} 키만 씁니다")
                bad = True
                continue
            if e["place"] not in places:
                d.add(ERROR, "ROUTINE_PLACE_UNKNOWN", f"{ep}.place", f"정의되지 않은 장소 {e['place']!r}")
                bad = True
            days = None
            if "days" in e:
                if not cal.has_weekdays():
                    d.add(ERROR, "ROUTINE_WEEKDAY_UNSUPPORTED", f"{ep}.days",
                          "이 달력에는 주(週) 정의가 없습니다 — 요일 일과를 쓸 수 없습니다(시간대만 쓰십시오)")
                    bad = True
                elif not (isinstance(e["days"], list) and e["days"] and
                          all(x in _wc.WEEKDAY_IDS for x in e["days"]) and len(set(e["days"])) == len(e["days"])):
                    d.add(ERROR, "ROUTINE_DAYS_INVALID", f"{ep}.days", f"days는 {list(_wc.WEEKDAY_IDS)}의 중복 없는 목록입니다")
                    bad = True
                else:
                    days = sorted(_wc.WEEKDAY_IDS.index(x) for x in e["days"])
            windows = None
            has_dp, has_range = "dayparts" in e, ("from" in e or "to" in e)
            if has_dp and has_range:
                d.add(ERROR, "ROUTINE_WINDOW_CONFLICT", ep, "dayparts와 from/to는 함께 쓰지 않습니다")
                bad = True
            elif has_dp:
                dps = e["dayparts"]
                if not (isinstance(dps, list) and dps and len(set(dps)) == len(dps)):
                    d.add(ERROR, "ROUTINE_DAYPARTS_INVALID", f"{ep}.dayparts", "dayparts는 중복 없는 시간대 id 목록입니다")
                    bad = True
                else:
                    windows = []
                    for dp in dps:
                        if dp not in cal.dayparts:
                            d.add(ERROR, "ROUTINE_DAYPART_UNKNOWN", f"{ep}.dayparts", f"정의되지 않은 시간대 {dp!r}")
                            bad = True
                            continue
                        st, en, _ = cal.dayparts[dp]
                        windows.append([st, en])
            elif has_range:
                try:
                    st, en = _wc.parse_hhmm(e.get("from")), _wc.parse_hhmm(e.get("to"))
                except (_wc.ClockError, TypeError, AttributeError):
                    d.add(ERROR, "ROUTINE_TIME_INVALID", ep, "from·to는 둘 다 'HH:MM'이어야 합니다")
                    bad = True
                else:
                    if st == en:
                        d.add(ERROR, "ROUTINE_TIME_INVALID", ep, "from과 to가 같습니다(하루 종일은 창을 생략)")
                        bad = True
                    windows = [[st, en]]
            if days is None and windows is None and not bad:
                d.add(ERROR, "ROUTINE_ENTRY_UNCONDITIONAL", ep,
                      "요일·시간 조건이 없는 항목입니다 — 항상 그곳이면 default_place를 쓰십시오")
                bad = True
            entries.append({"place": e["place"], "days": days, "windows": windows})
        dflt = r.get("default_place")
        if dflt is not None and dflt not in places:
            d.add(ERROR, "ROUTINE_PLACE_UNKNOWN", f"{p}.default_place", f"정의되지 않은 장소 {dflt!r}")
            bad = True
        if not entries and dflt is None and not bad:
            d.add(ERROR, "ROUTINE_EMPTY", p, "routine에 항목도 default_place도 없습니다(미저작이면 키를 빼십시오)")
            bad = True
        if bad:
            del a["routine"]
            continue
        a["routine"] = {"entries": entries, "default_place": dflt}


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


_WORLD_START_KEYS = ("place", "date", "time", "knowledge_seed", "items", "npc_choices")


def _compile_start_items(items, place_defs, actor_index, p, d):
    """v7.26 world_start.items — [{key, quantity, place|owner}] → 정규 목록(오류면 None).

    이 시작에서만 생기는 물건: place는 장소 id(현장 물건), owner는 저작 NPC 레거시 이름(그 인물 소지).
    v7.29 contains: 장소 물건(수량 1)을 닫힌 용기로 — [{key, quantity}] 내용물은 열기 전에는 꺼낼 수 없다.
    """
    if not isinstance(items, list) or not items:
        d.add(ERROR, "WORLD_START_ITEMS_INVALID", p, "items는 비어 있지 않은 목록이어야 합니다")
        return None
    out = []
    for i, it in enumerate(items):
        ip = f"{p}[{i}]"
        if not isinstance(it, dict) or set(it) - {"key", "quantity", "place", "owner", "contains"} \
                or not isinstance(it.get("key"), str) or not it["key"].strip() \
                or not isinstance(it.get("quantity"), int) or isinstance(it.get("quantity"), bool) \
                or it["quantity"] <= 0 or ("place" in it) == ("owner" in it):
            d.add(ERROR, "WORLD_START_ITEMS_INVALID", ip,
                  "물건은 {key, quantity(양의 정수), place|owner 중 하나}여야 합니다")
            return None
        if "place" in it and it["place"] not in place_defs:
            d.add(ERROR, "WORLD_START_PLACE_UNKNOWN", f"{ip}.place", f"정의되지 않은 장소 {it['place']!r}")
            return None
        if "owner" in it and it["owner"] not in actor_index:
            d.add(ERROR, "WORLD_START_ITEM_OWNER_UNKNOWN", f"{ip}.owner", f"저작 NPC가 아닙니다: {it['owner']!r}")
            return None
        if "contains" in it:
            con = it["contains"]
            if "place" not in it or it["quantity"] != 1 or not isinstance(con, list) or not con or not all(
                    isinstance(c, dict) and set(c) == {"key", "quantity"} and isinstance(c["key"], str)
                    and c["key"].strip() and isinstance(c["quantity"], int) and not isinstance(c["quantity"], bool)
                    and c["quantity"] > 0 for c in con):
                d.add(ERROR, "WORLD_START_ITEMS_INVALID", f"{ip}.contains",
                      "contains는 장소 물건(수량 1)에만, [{key, quantity(양의 정수)}] 비어 있지 않은 목록")
                return None
        out.append({k: it[k] for k in ("key", "quantity", "place", "owner", "contains") if k in it})
    return out


def _compile_resolution_target(tg, actor_index, p, d):
    """v7.44(E2) 결단 대상 — {"type": "ACTOR", "from": [저작 NPC 이름], "present"?: bool} → {type, actors, present}.

    대상이 있는 선택지는 후보 인물마다 하나의 결단 후보(option_id:저작 이름)로 펼쳐진다. present면 장면에 있는 인물만."""
    ok = (isinstance(tg, dict) and set(tg) - {"present"} == {"type", "from"} and tg["type"] == "ACTOR"
          and isinstance(tg["from"], list) and tg["from"] and len(set(map(str, tg["from"]))) == len(tg["from"])
          and all(isinstance(n, str) and n in actor_index for n in tg["from"])
          and isinstance(tg.get("present", False), bool))
    if not ok:
        d.add(ERROR, "EVENT_RESOLUTION_TARGET_INVALID", p,
              "target은 {type: ACTOR, from: [저작 NPC 이름(중복 없음)], present?: true/false}")
        return None
    return {"type": "ACTOR", "actors": [actor_index[n] for n in tg["from"]], "names": list(tg["from"]),
            "present": bool(tg.get("present", False))}


def _compile_npc_choices(rows, place_defs, actor_index, p, d):
    """v7.38(R3) world_start.npc_choices — [{npc(저작 NPC 레거시 이름), hidden?, items?}] → 정규 목록(오류면 None).

    hidden=true면 시작 장면에 있지만 보이지 않는 인물(얼굴 인지 시드 없음 — A4). items는 그 인물이 뽑혔을 때만 생긴다."""
    if not isinstance(rows, list) or not rows:
        d.add(ERROR, "WORLD_START_NPC_CHOICES_INVALID", p, "npc_choices는 비어 있지 않은 목록이어야 합니다")
        return None
    out, seen = [], set()
    for i, r in enumerate(rows):
        ip = f"{p}[{i}]"
        if not isinstance(r, dict) or set(r) - {"npc", "hidden", "items"} or r.get("npc") not in actor_index \
                or not isinstance(r.get("hidden", False), bool) or r["npc"] in seen:
            d.add(ERROR, "WORLD_START_NPC_CHOICES_INVALID", ip,
                  "후보는 {npc(저작 NPC 이름, 중복 없음), hidden?(true/false), items?}여야 합니다")
            return None
        seen.add(r["npc"])
        row = {"npc": r["npc"], "actor_id": actor_index[r["npc"]], "hidden": bool(r.get("hidden", False))}
        if "items" in r:
            items = _compile_start_items(r["items"], place_defs, actor_index, f"{ip}.items", d)
            if items is None:
                return None
            row["items"] = items
        out.append(row)
    return out


def _compile_world_starts(raw, init, place_defs, seeds, calendar, caps, d, actor_index=None):
    """시작 틀의 저작 정량 시작점 `world_start` → 컴파일된 초기화 시작점(H06).

    facts의 시간·장소는 슬롯 템플릿이라 기계 값으로 해석하지 않는다(DESIGN 08 §20). 동적 세계 초기화의
    시작 장소·시각은 사람이 저작한 world_start에서만 온다:
      {"place": 장소 id, "date": "YYYY-MM-DD", "time": "HH:MM", "knowledge_seed"?: seed id,
       "items"?: [{key, quantity, place|owner}]}
    저작된 틀에만 world_start 키가 생긴다(미저작 시나리오의 컴파일 결과·revision은 그대로).
    """
    from . import world_clock as _wc
    frames = raw.get("start_frames") if isinstance(raw.get("start_frames"), list) else []
    by_id = {f["frame_id"]: f for f in init["start_frames"]}
    for i, fr in enumerate(frames):
        if not isinstance(fr, dict) or "world_start" not in fr or fr.get("id") not in by_id:
            continue
        p = f"start_frames[{i}].world_start"
        ws = fr["world_start"]
        if not ("quantitative_time" in caps and "structured_places" in caps) or calendar is None:
            d.add(ERROR, "WORLD_START_WITHOUT_CAPABILITY", p,
                  "world_start는 quantitative_time·structured_places(달력 포함) 선언이 필요합니다")
            continue
        if not isinstance(ws, dict) or set(ws) - set(_WORLD_START_KEYS) or not {"place", "date", "time"} <= set(ws):
            d.add(ERROR, "WORLD_START_INVALID", p, "world_start는 {place, date, time, knowledge_seed?, items?}여야 합니다")
            continue
        if ws["place"] not in place_defs:
            d.add(ERROR, "WORLD_START_PLACE_UNKNOWN", f"{p}.place", f"정의되지 않은 장소 {ws['place']!r}")
            continue
        # H07: 인트로 문안의 장소·시각이 정본 시작점과 갈라지지 않게 한다 — world_start 틀에서는
        # 무작위 시작 지점 슬롯(place:)과, facts의 시간·장소를 채우는 무작위 목록 슬롯(list:)을 쓰지 않는다.
        frame = by_id[fr["id"]]
        random_slots = {n for n, s in frame["slots"].items() if s["source"] in ("place", "list")}
        bad_slots = sorted(n for n, s in frame["slots"].items() if s["source"] == "place")
        for fk in ("time", "location", "date"):
            tpl = (frame.get("fact_templates") or {}).get(fk)
            if isinstance(tpl, str):
                bad_slots += sorted(n for n in random_slots if "{" + n + "}" in tpl and n not in bad_slots)
        if bad_slots:
            d.add(ERROR, "WORLD_START_SLOT_RANDOM", p,
                  f"world_start 틀의 무작위 슬롯 {bad_slots}이 인트로 장소·시각을 정본 시작점과 다르게 만들 수 있습니다"
                  " — 고정 문안(literal)으로 저작하십시오")
            continue
        seed = ws.get("knowledge_seed")
        if seed is not None and seed not in (seeds or {}):
            d.add(ERROR, "WORLD_START_SEED_UNKNOWN", f"{p}.knowledge_seed", f"정의되지 않은 seed {seed!r}")
            continue
        try:
            y, m, dd = (int(x) for x in str(ws["date"]).split("-"))
            minute = _wc.Calendar(calendar).to_minute_index(y, m, dd) + _wc.parse_hhmm(ws["time"])
            _wc.initial_clock(minute)
        except Exception as e:  # noqa: BLE001
            d.add(ERROR, "WORLD_START_TIME_INVALID", p, f"시작 날짜·시각을 달력으로 해석할 수 없습니다: {e}")
            continue
        items = None
        if "items" in ws:
            items = _compile_start_items(ws["items"], place_defs, actor_index or {}, f"{p}.items", d)
            if items is None:
                continue
        choices = None
        if "npc_choices" in ws:                      # v7.38(R3): 시작 장면 인물 후보 — genesis가 세션 시드로 한 명
            choices = _compile_npc_choices(ws["npc_choices"], place_defs, actor_index or {}, f"{p}.npc_choices", d)
            if choices is None:
                continue
        by_id[fr["id"]]["world_start"] = {"place": ws["place"], "date": str(ws["date"]), "time": ws["time"],
                                          "minute_index": minute, "knowledge_seed": seed}
        if items:
            by_id[fr["id"]]["world_start"]["items"] = items
        if choices:
            by_id[fr["id"]]["world_start"]["npc_choices"] = choices


# ── v7.17 아이템 정의 · 제작법 · 운반 한도 ─────────────────────

def _grade_ok(v) -> bool:
    return v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= ITEM_GRADE_MAX)


def _compile_items(raw, actor_index, places, d) -> tuple:
    """item_definitions {종류: {...}} · item_recipes {제작법 id: {inputs, tools?, outputs, name?}} · carry_limits.

    모두 선택 저작 — 없으면 ({}, {}, None). 수량 환산(units.*.contains)은 저작된 정확한 수만(R1).
    instances(특수 물건 초기 위치)는 저작 NPC 레거시 키 또는 PlaceId 하나.
    """
    defs, recipes, limits = {}, {}, None
    src = raw.get("item_definitions")
    if src is not None and not isinstance(src, dict):
        d.add(ERROR, "ITEM_DEFINITIONS_INVALID", "item_definitions", "{종류: 정의} 객체여야 합니다")
        src = {}
    seen_inst, alias_owner = set(), {}
    for kind, body in (src or {}).items():
        p = f"item_definitions.{kind}"
        if not isinstance(kind, str) or not kind.strip() or any(c in kind for c in "[]〈〉") or not isinstance(body, dict):
            d.add(ERROR, "ITEM_DEFINITION_INVALID", p, "종류 이름(괄호 기호 없음)과 객체 정의가 필요합니다")
            continue
        for k in body:
            if k not in ITEM_DEF_KEYS:
                d.add(WARNING, "ITEM_UNKNOWN_KEY", f"{p}.{k}", "알 수 없는 아이템 정의 키(무시)")
        rec = {"kind": kind}
        ok = True
        for k in ("aliases", "categories", "info_refs"):
            vals = _str_list(body.get(k))
            if vals is None:
                d.add(ERROR, "ITEM_FIELD_INVALID", f"{p}.{k}", f"{k}는 문자열 목록이어야 합니다")
                ok = False
                continue
            rec[k] = vals
        for a in rec.get("aliases") or ():
            if a in alias_owner and alias_owner[a] != kind:
                d.add(ERROR, "ITEM_ALIAS_DUPLICATE", f"{p}.aliases", f"별칭 {a!r}이(가) {alias_owner[a]}와 겹칩니다")
                ok = False
            alias_owner[a] = kind
        for g in ("mass_grade", "bulk_grade"):
            if not _grade_ok(body.get(g)):
                d.add(ERROR, "ITEM_GRADE_INVALID", f"{p}.{g}", f"{g}는 0~{ITEM_GRADE_MAX} 정수")
                ok = False
            elif body.get(g) is not None:
                rec[g] = body[g]
        units = body.get("units") or {}
        if not isinstance(units, dict):
            d.add(ERROR, "ITEM_UNITS_INVALID", f"{p}.units", "{단위: {contains?}} 객체")
            ok, units = False, {}
        rec_units = {}
        for u, ub in units.items():
            con = (ub or {}).get("contains") if isinstance(ub, dict) else None
            if con is not None and not (isinstance(con, dict) and isinstance(con.get("unit"), str)
                                        and isinstance(con.get("count"), int) and con["count"] > 0):
                d.add(ERROR, "ITEM_CONTAINS_INVALID", f"{p}.units.{u}", "contains는 {unit, count(양의 정수)}")
                ok = False
                continue
            rec_units[u] = {"contains": dict(con)} if con else {}
        if rec_units:
            rec["units"] = rec_units
        parts = body.get("parts")
        if parts is not None:
            if not isinstance(parts, list) or not all(isinstance(x, dict) and isinstance(x.get("kind"), str)
                                                      and isinstance(x.get("count", 1), int) for x in parts):
                d.add(ERROR, "ITEM_PARTS_INVALID", f"{p}.parts", "parts는 [{kind, unit?, count}]")
                ok = False
            else:
                rec["parts"] = [{"kind": x["kind"], "unit": x.get("unit"), "count": x.get("count", 1)} for x in parts]
        for flag in ("special", "unique", "recipe_only"):
            if body.get(flag) is not None:
                if not isinstance(body[flag], bool):
                    d.add(ERROR, "ITEM_FLAG_INVALID", f"{p}.{flag}", "참/거짓")
                    ok = False
                elif body[flag]:
                    rec[flag] = True
        if isinstance(body.get("name"), str):
            rec["name"] = body["name"]
        if body.get("wear") is not None:             # v7.46 착용·들기 — 자리와 효과 태그(공용 어휘)
            from .items import effect_vocab
            voc = effect_vocab()
            w = body["wear"]
            slots = _str_list((w or {}).get("slots")) if isinstance(w, dict) else None
            tags = _str_list((w or {}).get("tags") or []) if isinstance(w, dict) else None
            if not slots or tags is None or set(slots) - set(voc["slots"]) or set(tags) - set(voc["tags"]):
                d.add(ERROR, "ITEM_WEAR_INVALID", f"{p}.wear",
                      "wear는 {slots: [공용 자리], tags: [공용 효과 태그]}(data/common_item_effects.json)")
                ok = False
            else:
                rec["wear"] = {"slots": slots, "tags": tags}
        if body.get("use") is not None:              # v7.47 사용 효과(공용 어휘 use_effects)
            from .items import effect_vocab
            uv = effect_vocab().get("use_effects") or {}
            use = _str_list(body["use"]) if isinstance(body["use"], list) else None
            if use is None or set(use) - set(uv):
                d.add(ERROR, "ITEM_USE_INVALID", f"{p}.use", "use는 [공용 사용 효과](data/common_item_effects.json)")
                ok = False
            elif use:
                rec["use"] = use
        insts = body.get("instances") or []
        if insts and not rec.get("unique"):
            d.add(ERROR, "ITEM_INSTANCES_NOT_UNIQUE", f"{p}.instances", "instances는 unique 물건에만 저작합니다")
            ok = False
        norm_insts = []
        for j, it in enumerate(insts if isinstance(insts, list) else []):
            ip = f"{p}.instances[{j}]"
            iid = (it or {}).get("instance_id") if isinstance(it, dict) else None
            owner, place = (it or {}).get("owner"), (it or {}).get("place")
            if not is_valid_explicit_id(iid) or iid in seen_inst:
                d.add(ERROR, "ITEM_INSTANCE_ID_INVALID", ip, "instance_id는 고유한 명시 id")
                ok = False
                continue
            if (owner is None) == (place is None) or (owner is not None and owner not in actor_index) \
                    or (place is not None and place not in places):
                d.add(ERROR, "ITEM_INSTANCE_LOCATION_INVALID", ip, "owner(저작 NPC 키) 또는 place(장소) 중 하나만")
                ok = False
                continue
            seen_inst.add(iid)
            norm_insts.append({"instance_id": iid, "owner": owner, "place": place})
        if norm_insts:
            rec["instances"] = norm_insts
        if ok:
            defs[kind] = rec
    rsrc = raw.get("item_recipes")
    if rsrc is not None and not isinstance(rsrc, dict):
        d.add(ERROR, "ITEM_RECIPES_INVALID", "item_recipes", "{제작법 id: 정의} 객체여야 합니다")
        rsrc = {}
    for rid, body in (rsrc or {}).items():
        p = f"item_recipes.{rid}"
        if not is_valid_explicit_id(rid) or not isinstance(body, dict):
            d.add(ERROR, "ITEM_RECIPE_INVALID", p, "명시 id와 객체가 필요합니다")
            continue

        def _rows(v):
            return isinstance(v, list) and v and all(
                isinstance(x, dict) and isinstance(x.get("kind"), str) and isinstance(x.get("count", 1), int)
                and x.get("count", 1) > 0 for x in v)
        if not _rows(body.get("inputs")) or not _rows(body.get("outputs")) \
                or (body.get("tools") is not None and _str_list(body.get("tools")) is None):
            d.add(ERROR, "ITEM_RECIPE_INVALID", p, "inputs·outputs는 [{kind, unit?, count}], tools는 종류 목록")
            continue
        recipes[rid] = {"recipe_id": rid, "name": body.get("name") if isinstance(body.get("name"), str) else rid,
                        "inputs": [{"kind": x["kind"], "unit": x.get("unit"), "count": x.get("count", 1)}
                                   for x in body["inputs"]],
                        "outputs": [{"kind": x["kind"], "unit": x.get("unit"), "count": x.get("count", 1)}
                                    for x in body["outputs"]],
                        "tools": list(body.get("tools") or [])}
    lim = raw.get("carry_limits")
    if lim is not None:
        if not isinstance(lim, dict) or set(lim) - {"mass", "bulk"} or not all(
                isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in lim.values()):
            d.add(ERROR, "CARRY_LIMITS_INVALID", "carry_limits", "{mass?, bulk?: 양의 정수}")
        else:
            limits = dict(lim)
    return defs, recipes, limits


def _check_item_info_refs(items, infos, d) -> None:
    for kind, rec in items.items():
        for iid in rec.get("info_refs") or ():
            if iid not in infos or infos[iid].get("kind") != "EXPLICIT":
                d.add(ERROR, "ITEM_INFO_REF_UNKNOWN", f"item_definitions.{kind}.info_refs",
                      f"{iid!r}는 information_definitions의 명시 정보여야 합니다(NPC 비밀·이면 금지)")


# ── v7.17.2 소속 → 접근 capability(선택 저작) ─────────────────────

def _compile_faction_capabilities(raw, event_tpls, access, d) -> dict:
    """faction_capabilities {<grant_faction 값>: [<capability>]} 검증·정규화. 없으면 {}.

    키는 사건 템플릿이 실제로 기록하는 SET_EVENT_LOCAL grant_faction 값, 값은 접근 규칙 override가 실제로
    요구하는 capability여야 한다(오타·존재하지 않는 세력은 오류 — 조용히 권한이 사라지거나 생기지 않게).
    """
    src = raw.get("faction_capabilities")
    if src is None:
        return {}
    if not isinstance(src, dict):
        d.add(ERROR, "FACTION_CAPABILITIES_INVALID", "faction_capabilities", "{소속 값: [capability]} 객체여야 합니다")
        return {}
    granted = {st.get("value") for t in (event_tpls or {}).values() for c in t.get("transition_cases") or ()
               for st in c.get("steps") or () if st.get("type") == "SET_EVENT_LOCAL"
               and st.get("key") == "grant_faction" and isinstance(st.get("value"), str)}
    required = {o.get("requires_capability") for r in ((access or {}).get("rules") or {}).values()
                for stt in (r.get("states") or {}).values() for o in stt.get("actor_overrides") or ()
                if isinstance(o.get("requires_capability"), str)}
    out = {}
    for name, caps in src.items():
        p = f"faction_capabilities.{name}"
        vals = _str_list(caps)
        if not isinstance(name, str) or not name.strip() or vals is None or not vals or len(set(vals)) != len(vals):
            d.add(ERROR, "FACTION_CAPABILITIES_INVALID", p, "소속 값 하나에 중복 없는 capability 문자열 목록")
            continue
        if name not in granted:
            d.add(ERROR, "FACTION_UNKNOWN", p, f"{name!r}는 어떤 사건도 grant_faction으로 기록하지 않습니다")
            continue
        missing = [c for c in vals if c not in required]
        if missing:
            d.add(ERROR, "FACTION_CAPABILITY_UNUSED", p, f"접근 규칙이 요구하지 않는 capability {missing}")
            continue
        out[name] = sorted(vals)
    return out


# ── v7.17 초기 지식(Information 선택 opt-in) ─────────────────────

def _ik_access_ok(node, path, d, depth=0, fields=None) -> bool:
    if depth > 3 or not isinstance(node, dict):
        d.add(ERROR, "IK_ACCESS_INVALID", path, "access는 {all|any: [조건]} 또는 조건(깊이 3 이하)")
        return False
    if set(node) == {"all"} or set(node) == {"any"}:
        items = node.get("all") if "all" in node else node.get("any")
        if not isinstance(items, list) or not items:
            d.add(ERROR, "IK_ACCESS_INVALID", path, "all/any는 비어 있지 않은 목록")
            return False
        return all(_ik_access_ok(x, f"{path}[{i}]", d, depth + 1, fields) for i, x in enumerate(items))
    op = node.get("op")
    if op not in IK_OPS or not isinstance(node.get("field"), str) or not node["field"]:
        d.add(ERROR, "IK_ACCESS_OP_INVALID", path, f"조건은 {{field, op: {list(IK_OPS)}, value|values}}")
        return False
    if fields is not None and node["field"] not in fields:
        d.add(ERROR, "IK_ACCESS_FIELD_UNKNOWN", path, f"field {node['field']!r}는 승인 프로필 항목이 아닙니다")
        return False
    if op == "equals" and (set(node) != {"field", "op", "value"} or not isinstance(node["value"], (str, int))
                           or isinstance(node["value"], bool)):
        d.add(ERROR, "IK_ACCESS_VALUE_INVALID", path, "equals는 value(문자열·정수) 하나")
        return False
    if op == "one_of" and (set(node) != {"field", "op", "values"} or not isinstance(node["values"], list)
                           or not node["values"]
                           or not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in node["values"])):
        d.add(ERROR, "IK_ACCESS_VALUE_INVALID", path, "one_of는 values(문자열·정수 목록)")
        return False
    return True


def _compile_initial_knowledge(raw, infos, init, d) -> None:
    """information_definitions[*].initial_knowledge 검증·정규화 → 정보 기록의 initial_knowledge.

    미표기·null = 초기 전달 금지(opt-in). summary 없는 정보는 opt-in할 수 없다. 정의가 잘못되면 공개가 아니라 오류.
    """
    src = raw.get("information_definitions") if isinstance(raw.get("information_definitions"), dict) else {}
    frame_ids = {f["frame_id"] for f in (init or {}).get("start_frames") or ()}
    # 접근 조건 field = 승인 프로필의 평면 항목(생성 단계 field ∪ pc_template 키). 둘 다 없으면 이름 검사 생략
    pf = {s_.get("field") for s_ in (raw.get("profile_creation") or ()) if isinstance(s_, dict)} - {None}
    pf |= set(raw.get("pc_template") or {}) if isinstance(raw.get("pc_template"), dict) else set()
    allowed_fields = pf or None
    for iid, body in src.items():
        ik = (body or {}).get("initial_knowledge") if isinstance(body, dict) else None
        if ik is None:
            continue
        p = f"information_definitions.{iid}.initial_knowledge"
        rec = infos.get(iid)
        if rec is None or rec.get("kind") != "EXPLICIT":
            continue
        if not isinstance(ik, dict) or set(ik) - set(IK_KEYS) or ik.get("tier") not in IK_TIERS:
            d.add(ERROR, "IK_INVALID", p, f"initial_knowledge는 {{tier: {list(IK_TIERS)}, ...}} (키 {list(IK_KEYS)})")
            continue
        summ = rec.get("summary")
        if not isinstance(summ, str) or not summ.strip():
            d.add(ERROR, "IK_SUMMARY_REQUIRED", p, "초기 지식에는 플레이어가 봐도 되는 완결 summary가 필요합니다")
            continue
        if len(summ) > IK_SUMMARY_MAX:
            d.add(ERROR, "IK_SUMMARY_TOO_LONG", p, f"summary {len(summ)}자 > {IK_SUMMARY_MAX}자(정본 원문을 자르지 않는다)")
            continue
        phases = ik.get("phases", list(IK_PHASES))
        if not isinstance(phases, list) or not phases or len(set(phases)) != len(phases) \
                or any(x not in IK_PHASES for x in phases):
            d.add(ERROR, "IK_PHASES_INVALID", f"{p}.phases", f"phases는 {list(IK_PHASES)} 중 중복 없는 목록")
            continue
        sf = ik.get("start_frame_ids")
        if sf is not None:
            if _str_list(sf) is None or not sf or any(x not in frame_ids for x in sf):
                d.add(ERROR, "IK_START_FRAME_UNKNOWN", f"{p}.start_frame_ids", "실재하는 시작 틀 id 목록이어야 합니다")
                continue
            if phases != ["START"]:
                d.add(ERROR, "IK_START_FRAME_PHASE", f"{p}.start_frame_ids", "start_frame_ids는 phases=[START]에만")
                continue
        kind = ik.get("kind", "fact")
        if kind not in IK_KINDS:
            d.add(ERROR, "IK_KIND_INVALID", f"{p}.kind", f"kind는 {list(IK_KINDS)}")
            continue
        if ik.get("access") is not None and not _ik_access_ok(ik["access"], f"{p}.access", d,
                                                              fields=allowed_fields):
            continue
        rec["initial_knowledge"] = {"tier": ik["tier"], "phases": sorted(phases), "kind": kind,
                                    "access": copy.deepcopy(ik.get("access")),
                                    "start_frame_ids": sorted(sf) if sf else None}


# ── 진입점 ────────────────────────────────────────────────────

_STATUS_NAMES = None   # v7.48(E1): 컴파일 중인 시나리오의 상태 이름(공용 + 시나리오) — OBSERVER_STATUS 검증용
_STATUS_DEFS = None    # v7.49: 같은 출처의 상태 정의(역할 dead 판별)
_CAPS = None           # v7.50: 컴파일 중인 시나리오의 선언 capability(사건 단계 검증용)
_PC_RECORDS = None     # v7.50: growth.records(프로필 기록 선언)
_PARTY_ACTORS = None   # v7.51: {ActorId: 파티 역할 있음} — JOIN_PARTY·LEAVE_PARTY 대상 검증


def compile_scenario(raw: dict, scenario_id: str, quests: dict | None = None) -> CompileResult:
    """raw 시나리오(+퀘스트 부속)를 컴파일한다. 입력을 변경하지 않는다."""
    global _STATUS_NAMES, _STATUS_DEFS, _CAPS, _PC_RECORDS, _PARTY_ACTORS
    try:
        from .utils import scenario_capabilities
        _CAPS = scenario_capabilities(raw if isinstance(raw, dict) else {})
        _PC_RECORDS = (((raw or {}).get("growth") or {}).get("records") or {}) if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        _CAPS, _PC_RECORDS = frozenset(), {}
    try:
        from .utils import get_merged_status_effects
        _STATUS_DEFS = get_merged_status_effects(raw if isinstance(raw, dict) else {})
        _STATUS_NAMES = set(_STATUS_DEFS)
    except Exception:  # noqa: BLE001 — 상태 목록을 못 읽으면 이름 검사만 건너뛴다
        _STATUS_NAMES = _STATUS_DEFS = None
    try:
        return _compile_scenario(raw, scenario_id, quests)
    finally:
        _STATUS_NAMES = _PARTY_ACTORS = None


def _compile_scenario(raw: dict, scenario_id: str, quests: dict | None = None) -> CompileResult:
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
    for aid, a in actors.items():
        if "knowledge_seed" in a and (not isinstance(a["knowledge_seed"], str) or a["knowledge_seed"] not in seeds):
            d.add(ERROR, "NPC_KNOWLEDGE_SEED_UNKNOWN", f"default_npcs.{a['legacy_key']}.knowledge_seed",
                  f"정의되지 않은 공간 지식 seed {a['knowledge_seed']!r}")
    init = _compile_initialization(raw, places, d)
    calendar = _compile_calendar(raw, cap["capabilities"], d)
    _compile_routines(actors, places, calendar, d)
    _compile_world_starts(raw, init, places, seeds, calendar, cap["capabilities"], d, actor_index)
    items, recipes, carry_limits = _compile_items(raw, actor_index, places, d)
    explicit = _compile_explicit_infos(raw, actors, places, d, items)
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
    _compile_info_reveals(raw, infos, places, edges, d)
    _compile_initial_knowledge(raw, infos, init, d)
    _check_item_info_refs(items, infos, d)
    series = _compile_info_series(raw, _ref_scope(actors, places, infos, access, modes, calendar=calendar), d)
    _compile_personas(raw, _ref_scope(actors, places, infos, access, modes, calendar=calendar,
                                                    templates=(raw.get("event_templates") or {}).keys()), d)
    pursuers = _compile_behaviors(actors, places, modes, cap["capabilities"], d, calendar=calendar,
                                  templates=(raw.get("event_templates") or {}).keys())
    global _PARTY_ACTORS
    party_cfg = _compile_party(raw, actors, cap["capabilities"], d,
                               _ref_scope(actors, places, infos, access, modes, calendar=calendar,
                                          templates=(raw.get("event_templates") or {}).keys()))
    _PARTY_ACTORS = {aid: "party" in a for aid, a in actors.items()}
    _raw_ev = raw
    if "combat_wounds" in cap["capabilities"]:      # v7.50(E6): 전투 기록 사건(공격 대결로 쓰러진 인물의 상태)
        from . import events as _ev0
        if _ev0.COMBAT_TEMPLATE in (raw.get("event_templates") or {}):
            d.add(ERROR, "COMBAT_TEMPLATE_COLLISION", f"event_templates.{_ev0.COMBAT_TEMPLATE}", "전투 기록 템플릿 이름과 겹칩니다")
        _raw_ev = {**raw, "event_templates": {**(raw.get("event_templates") or {}),
                                              _ev0.COMBAT_TEMPLATE: _ev0.synth_holder("전투 기록")}}
    if pursuers:                       # v7.48(E3): 추적자마다 상태를 담는 사건 템플릿(시작 즉시 생성)
        from . import pursuit as _pu
        synth = _pu.synth_templates(pursuers)
        clash = sorted(set(synth) & set(raw.get("event_templates") or {}))
        for tid in clash:
            d.add(ERROR, "BEHAVIOR_TEMPLATE_COLLISION", f"event_templates.{tid}", "추적자 상태 템플릿 이름과 겹칩니다")
        _raw_ev = {**_raw_ev, "event_templates": {**(_raw_ev.get("event_templates") or {}), **synth}}
    event_tpls = _compile_event_templates(_raw_ev, cap["capabilities"], actors, actor_index, places, infos, access,
                                          modes, edges, calendar, d)

    faction_caps = _compile_faction_capabilities(raw, event_tpls, access, d)

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
        # H04(compiler v3): 사건 템플릿(구조화·정적 검증 완료). 퀘스트를 자동 변환하지 않는다.
        "event_templates": event_tpls,
        "initialization_definitions": init,
        # H03(compiler v2): 정량 시간 달력. 레거시는 None(새 시간 엔진 근거 아님).
        "calendar": calendar,
        # 현행 런타임은 raw 모양을 직접 소비한다. 세션 바인딩(H10) 뒤에도 정의 하나로
        # 그 모양을 복원할 수 있도록 원문을 보존하고, dict 순서(장소 순회·NPC 전개
        # 순서가 의존)까지 순서 지문으로 정의에 묶는다.
        "legacy_source": src_copy,
        "legacy_source_order_digest": order_digest(src_copy),
    }
    # v7.17: 아이템 정의·제작법·운반 한도는 저작한 시나리오에만 키가 생긴다(미저작 시나리오 revision 불변)
    if items:
        definition["item_definitions"] = items
    if recipes:
        definition["item_recipes"] = recipes
    if carry_limits:
        definition["carry_limits"] = carry_limits
    # v7.17.2: 소속(사건 결과 grant_faction) → 접근 capability 매핑 — 저작한 시나리오에만 키가 생긴다
    if faction_caps:
        definition["faction_capabilities"] = faction_caps
    # v7.49: 사건이 붙이는 역할 dead 상태(SET_ACTOR_STATUS) — 그 상태가 붙은 인물은 유효 위치를 잃는다.
    #   그런 step을 저작한 시나리오에만 키가 생긴다(미저작 시나리오 revision 불변)
    if party_cfg is not None:                   # v7.51: 기능 party를 선언한 시나리오에만 키가 생긴다
        definition["party"] = party_cfg
    removing = _removing_statuses(event_tpls, combat="combat_wounds" in cap["capabilities"])
    if removing:
        definition["actor_removing_statuses"] = removing
    return CompileResult(scenario_id, definition["compiler_version"], definition,
                         definition_revision(definition), diagnostics)


def _removing_statuses(event_tpls, combat=False) -> list:
    from .status_rules import ROLE_DEAD, with_role
    used = set(with_role(_STATUS_DEFS or {}, ROLE_DEAD)) if combat else set()

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == "SET_ACTOR_STATUS" and isinstance(x.get("status"), str):
                used.add(x["status"])
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(event_tpls)
    defs = _STATUS_DEFS or {}
    return sorted(s for s in used if ROLE_DEAD in ((defs.get(s) or {}).get("roles") or ()))


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
