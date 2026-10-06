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
COMPILER_VERSION = 1

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
VALIDATED_CAPABILITIES = frozenset({"stable_actors"})

# capability 의존성(DESIGN 08 §4). (요구 capability, 심각도)
# dynamic_actor_location → structured_places 는 설계가 "권장/필수"로 열어 두어 WARNING.
CAPABILITY_DEPENDENCIES = {
    "scheduled_world_progression": (("quantitative_time", "ERROR"),),
    "actor_cognition": (("stable_actors", "ERROR"), ("information_registry", "ERROR")),
    "dynamic_events": (("stable_actors", "ERROR"),),
    "dynamic_actor_location": (("structured_places", "WARNING"),),
}

# 새 스키마용으로 예약된 키. 컴파일러 v1은 구조화하지 않으므로, 있으면 경고한다
# (legacy_source 에 원문으로 남지만 어떤 엔진도 읽지 않는다는 뜻).
RESERVED_UNCOMPILED_KEYS = (
    "information_definitions", "event_templates", "calendar", "movement_modes",
    "access_control_definitions", "visibility_relations", "initialization_definitions",
    "spatial_knowledge_seeds",
)

# NPC 인라인 비밀 필드(npc_template.info_fields의 '비밀').
INLINE_SECRET_FIELD = "비밀"

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
            "has_inline_secret": isinstance(secret, str) and bool(secret.strip()),
            "source_ref": ["default_npcs", key],
        }
        order.append(actor_id)
        index[key] = actor_id
    return defs, order, index


def _compile_inline_secrets(raw, actors, d):
    out = {}
    npcs = raw.get("default_npcs") if isinstance(raw.get("default_npcs"), dict) else {}
    for actor_id, a in actors.items():
        secret = npcs.get(a["legacy_key"], {}).get(INLINE_SECRET_FIELD)
        path = f"default_npcs.{a['legacy_key']}.{INLINE_SECRET_FIELD}"
        if secret is None:
            continue
        if not isinstance(secret, str):
            d.add(WARNING, "NPC_SECRET_NOT_TEXT", path, "비밀 필드가 문자열이 아니어서 컴파일하지 않습니다")
            continue
        if not secret.strip():
            continue
        info_id = npc_inline_secret_info_id(actor_id)
        out[info_id] = {
            "info_id": info_id,
            "kind": "NPC_INLINE_SECRET",
            "owner_actor_id": actor_id,
            "proposition": secret,
            "source_ref": ["default_npcs", a["legacy_key"], INLINE_SECRET_FIELD],
        }
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
        ident = (q["id"], q.get("version"))
        if ident in seen:
            d.add(ERROR, "QUEST_IDENTITY_DUPLICATE", path,
                  f"퀘스트 (id={ident[0]}, version={ident[1]})가 quests[{seen[ident]}]와 겹칩니다")
            continue
        seen[ident] = i
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
        info_id = quest_hidden_info_id(scenario_id, q["id"], q.get("version"))
        out[info_id] = {
            "info_id": info_id,
            "kind": "QUEST_HIDDEN_TRUTH",
            "quest_id": q["id"],
            "quest_version": q.get("version"),
            "proposition": hidden["truth"],
            "source_ref": ["quests", i, "hidden"],
        }
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


def _compile_edges(scenario_id, raw, place_defs, caps, d):
    """movement_edges 저작 sugar → 방향 구간. 거리·방향 의미는 저작값을 그대로 옮긴다.

    connected 에서 구간을 만들지 않는다. 숫자를 생성하지 않는다(DESIGN 08 §17).
    구간 속성의 의미 검증(거리 단위·모드·접근)은 H05 몫이다.
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
            out[eid] = {"edge_id": eid, "pair_id": pair_id, "from": f, "to": t,
                        "authored_bidirectional": bidir, "properties": pr,
                        "source_ref": ["movement_edges", i]}
    return out


# ── 시작 틀 → 초기화 정의 ─────────────────────────────────────

def _parse_slot(spec):
    if not isinstance(spec, str):
        return {"spec": spec, "source": "literal", "ref": None}
    for src in ("profile", "place", "list"):
        if spec.startswith(src + ":"):
            return {"spec": spec, "source": src, "ref": spec[len(src) + 1:]}
    return {"spec": spec, "source": "literal", "ref": None}


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
    edges = _compile_edges(scenario_id, raw, places, cap["capabilities"], d)
    init = _compile_initialization(raw, places, d)
    infos = {}
    for src in (_compile_inline_secrets(raw, actors, d),
                _compile_quest_hidden(scenario_id, quests, places, d)):
        for iid, rec in src.items():
            if iid in infos:
                d.add(ERROR, "INFO_ID_COLLISION", ".".join(map(str, rec["source_ref"])),
                      f"InfoId '{iid}'가 겹칩니다")
                continue
            infos[iid] = rec

    diagnostics = d.sorted()
    if any(x.severity == ERROR for x in diagnostics):
        return CompileResult(scenario_id, COMPILER_VERSION, None, None, diagnostics)

    src_copy = copy.deepcopy(legacy_source)
    definition = {
        "format": DEFINITION_FORMAT,
        "format_version": DEFINITION_FORMAT_VERSION,
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
        "information_definitions": infos,
        "initialization_definitions": init,
        # 현행 런타임은 raw 모양을 직접 소비한다. 세션 바인딩(H10) 뒤에도 정의 하나로
        # 그 모양을 복원할 수 있도록 원문을 보존하고, dict 순서(장소 순회·NPC 전개
        # 순서가 의존)까지 순서 지문으로 정의에 묶는다.
        "legacy_source": src_copy,
        "legacy_source_order_digest": order_digest(src_copy),
    }
    return CompileResult(scenario_id, COMPILER_VERSION, definition,
                         definition_revision(definition), diagnostics)


def compile_sources(sources: ScenarioSources) -> CompileResult:
    return compile_scenario(sources.raw, sources.scenario_id, sources.quests)


def definition_revision(definition: dict) -> str:
    """정의의 정규 직렬화 SHA-256(DESIGN 06 §19)."""
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
