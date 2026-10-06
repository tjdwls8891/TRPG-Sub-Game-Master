# 동적 세계 정본 필드 레지스트리 · world_version 정체성 (H02)
#
# [단일 출처]
#   H02가 추가한 세션 필드의 이름·기본값·권위 분류는 이 모듈 한 곳에서만 정의한다.
#   TRPGSession 생성자, core.io.SESSION_FIELDS, TurnPreparation.CANONICAL_DOMAINS,
#   turn_history.REVERSIBLE_FIELDS가 모두 여기서 읽는다 — 목록을 손으로 이중 관리하면
#   한쪽에서 필드가 빠져 저장·롤백·되감기 중 하나가 조용히 깨진다.
#
# [권위 분류]
#   GAMEPLAY_WORLD_FIELDS  되감기 대상 게임 정본(커밋 소유·READY 지문·턴 이력 스냅샷)
#   IDENTITY_FIELDS        영속되지만 되감기 대상이 아닌 세션 정체성
#     definition_revision  세션이 쓰는 세계 정의 정체성(H01 revision). H02는 값을 쓰지 않는다
#                          (스냅샷 바인딩은 H06). 기본 None.
#     narrative_seed       결정적 난수의 불변 시드. 세션 id에서 안정적으로 도출(DESIGN 11 §14).
#
# [H02 범위]
#   빈 구조({}·None)는 새 엔진 활성화 근거가 아니다. 시간·공간·사건·정보 의미는 H03 이후.
#   world_version 은 모든 커밋된 PLAYER_TURN이 전진시키는 인과 정체성이다(DESIGN 11 §6).
import copy

from .world_ids import stable_digest

# 되감기 대상 게임 정본 — 이름: 기본값(삽입 순서 = 저장 순서)
GAMEPLAY_WORLD_FIELDS = {
    "world_version": None,
    "world_clock": {},
    "actor_registry": {},
    "spatial_state": {},
    "spatial_knowledge": {},
    "access_control_state": {},
    "event_instances": {},
    "world_schedule": {},
    "information_state": {},
    "cognition_state": {},
    "relationship_state": {},
}

# 영속·비가역 세션 정체성. narrative_seed 기본값 ""은 '아직 도출 안 됨' 표식이다 —
# 생성자·마이그레이션이 세션 id에서 채운다(빈 시드가 저장되지 않는다).
IDENTITY_FIELDS = {
    "definition_revision": None,
    "narrative_seed": "",
}

ALL_WORLD_FIELDS = {**GAMEPLAY_WORLD_FIELDS, **IDENTITY_FIELDS}

NARRATIVE_SEED_NAMESPACE = "session.narrative_seed.v1"


def gameplay_defaults() -> dict:
    """게임 정본 필드 기본값의 깊은 사본(공유 가변 객체 금지)."""
    return copy.deepcopy(GAMEPLAY_WORLD_FIELDS)


def derive_narrative_seed(session_id) -> str:
    """세션 id에서 결정적으로 도출한 시드. Python hash()·난수를 쓰지 않는다."""
    return "seed_" + stable_digest(NARRATIVE_SEED_NAMESPACE, str(session_id), length=32)


def migrate_world_fields(data: dict) -> int:
    """저장 dict에 H02 필드를 채운다(v3 → v4). 멱등. 채운 필드 수를 반환.

    · 게임 정본 필드는 안전한 빈 기본값 — 레거시 세션은 새 엔진을 켜지 않는다.
    · definition_revision 은 None(아직 정의 미바인딩).
    · narrative_seed 는 session_id에서 도출 — 몇 번 마이그레이션해도 같은 값.
      이미 값이 있으면 절대 바꾸지 않는다(불변).
    """
    filled = 0
    for name, default in GAMEPLAY_WORLD_FIELDS.items():
        if name not in data:
            data[name] = copy.deepcopy(default)
            filled += 1
    if "definition_revision" not in data:
        data["definition_revision"] = None
        filled += 1
    if not data.get("narrative_seed"):
        data["narrative_seed"] = derive_narrative_seed(data.get("session_id"))
        filled += 1
    return filled


# ══════════════════════════════════════════════════════════════
#  world_version — 인과 정체성 (H02-B)
# ══════════════════════════════════════════════════════════════
#   {"version_id", "parent_version_id", "ordinal", "cause_kind", "cause_id"}
#   version_id = stable_digest(세션, 부모, 원인 종류, 원인 id) — 재시작·재계산에도 같다.
#   벽시계 시각은 넣지 않는다(인과 정체성과 혼동 금지).

CAUSE_PLAYER_TURN = "PLAYER_TURN"
CAUSE_INITIALIZATION = "INITIALIZATION"
CAUSE_ADMIN_EDIT = "ADMIN_EDIT"
CAUSE_KINDS = (CAUSE_PLAYER_TURN, CAUSE_INITIALIZATION, CAUSE_ADMIN_EDIT)

WORLD_VERSION_NAMESPACE = "world_version.v1"


def version_id_of(world_version) -> str | None:
    return world_version.get("version_id") if isinstance(world_version, dict) else None


def is_world_version(value) -> bool:
    return (isinstance(value, dict)
            and isinstance(value.get("version_id"), str) and value["version_id"]
            and value.get("cause_kind") in CAUSE_KINDS
            and isinstance(value.get("ordinal"), int) and not isinstance(value.get("ordinal"), bool))


def next_world_version(session_id, parent, *, cause_kind: str, cause_id: str) -> dict:
    """부모 version에서 다음 version을 결정적으로 만든다.

    parent None = 레거시(H02 이전 계보) 또는 첫 version. 같은 입력 → 같은 version_id.
    """
    if cause_kind not in CAUSE_KINDS:
        raise ValueError(f"알 수 없는 cause_kind: {cause_kind!r}")
    if not isinstance(cause_id, str) or not cause_id:
        raise ValueError("cause_id가 필요합니다")
    if parent is not None and not is_world_version(parent):
        raise ValueError(f"부모 world_version 형식이 잘못됐습니다: {parent!r}")
    parent_id = version_id_of(parent)
    ordinal = (parent["ordinal"] + 1) if parent is not None else 1
    vid = "wv_" + stable_digest(WORLD_VERSION_NAMESPACE, str(session_id), parent_id,
                                cause_kind, cause_id)
    return {"version_id": vid, "parent_version_id": parent_id, "ordinal": ordinal,
            "cause_kind": cause_kind, "cause_id": cause_id}
