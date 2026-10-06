# 세션 정의 스냅샷 — 컴파일된 세계 정의의 내구 저장·검증 (H01)
#
# [왜 필요한가]
#   재시작 때 현재 디스크의 시나리오 파일을 다시 읽으면, 세션 시작 뒤 파일이
#   수정됐을 때 진행 중인 세션의 정의가 조용히 바뀐다. revision 해시만 저장해도
#   그 내용을 복원할 수 없다. 그래서 정의 바이트 자체를 세션에 묶는다(DESIGN 06 §14~21).
#
#   sessions/{id}/definition_snapshot.json
#
# [H01 범위 — production 미배선]
#   이 모듈은 쓰기·읽기·검증 프리미티브만 제공한다. 세션 생성·재시작 경로에서
#   부르지 않는다(세션 바인딩·restart 권위 전환은 H10). 세션 필드도 추가하지 않는다.
#
# [불변식]
#   · 같은 세션의 스냅샷은 한 번 쓰면 다른 revision으로 덮어쓰지 않는다.
#     정의 교체는 별도의 명시적 DefinitionMigration 이다(DESIGN 06 §23).
#   · 같은 revision 재기록은 no-op(멱등).
#   · 읽기는 형식·revision·순서 지문을 모두 재계산해 대조한다. 하나라도 어긋나면
#     조용히 넘어가지 않고 예외를 던진다.
#   · provider cache 객체·비용 사실은 넣지 않는다(DESIGN 06 §18).
#   · revision 은 정의(definition) 전체를 덮는다 — 정의에 든 compiler_version 포함.
#     봉투의 compiler_version은 그 값과 같아야 한다. 원본 파일 해시·기록 시각은
#     출처(provenance) — 같은 정의를 다른 공백의 원본에서 만들어도 revision은 같다.
import json
import os
import time
from dataclasses import dataclass

from .definition_compiler import (
    DEFINITION_FORMAT,
    DEFINITION_FORMAT_VERSION,
    CompileResult,
    definition_revision,
)
from .world_ids import CanonicalSerializationError, order_digest

SNAPSHOT_FILE = "definition_snapshot.json"
SNAPSHOT_FORMAT = "indaim.definition_snapshot"
SNAPSHOT_FORMAT_VERSION = 1


class DefinitionSnapshotError(RuntimeError):
    """정의 스냅샷 계열 실패의 공통 상위."""


class DefinitionSnapshotMissing(DefinitionSnapshotError):
    """스냅샷 파일이 없다."""


class DefinitionSnapshotCorruption(DefinitionSnapshotError):
    """파일을 해석할 수 없거나 형식·해시가 내용과 맞지 않는다."""


class DefinitionSnapshotCompilerVersionMismatch(DefinitionSnapshotCorruption):
    """봉투의 compiler_version이 정의(revision이 덮는 값)의 compiler_version과 다르다."""


class DefinitionSnapshotMismatch(DefinitionSnapshotError):
    """스냅샷은 온전하나 호출자가 기대한 revision과 다르다."""


class DefinitionSnapshotConflict(DefinitionSnapshotError):
    """이미 다른 revision의 스냅샷이 있다 — 덮어쓰기 거부."""


class DefinitionSnapshotPersistenceError(DefinitionSnapshotError):
    """쓰기 내구성 경계(write→flush→fsync→replace) 실패."""


@dataclass(frozen=True)
class SessionDefinitionSnapshot:
    session_id: str
    definition_revision: str
    compiler_version: int
    definition: dict
    source: dict
    written_at: float

    @property
    def scenario_id(self) -> str:
        return self.definition["scenario_meta"]["scenario_id"]


def snapshot_path(session_id: str) -> str:
    return os.path.join("sessions", str(session_id), SNAPSHOT_FILE)


def _verify_definition(definition) -> str:
    """정의 자체의 형식·순서 지문을 확인하고 revision을 재계산한다."""
    if not isinstance(definition, dict):
        raise DefinitionSnapshotCorruption("definition이 객체가 아닙니다")
    if definition.get("format") != DEFINITION_FORMAT:
        raise DefinitionSnapshotCorruption(f"정의 형식이 다릅니다: {definition.get('format')!r}")
    if definition.get("format_version") != DEFINITION_FORMAT_VERSION:
        raise DefinitionSnapshotCorruption(
            f"지원하지 않는 정의 형식 버전: {definition.get('format_version')!r}")
    cv = definition.get("compiler_version")
    if not isinstance(cv, int) or isinstance(cv, bool) or cv <= 0:
        raise DefinitionSnapshotCorruption(f"정의에 compiler_version이 없거나 잘못됐습니다: {cv!r}")
    try:
        if order_digest(definition.get("legacy_source")) != definition.get("legacy_source_order_digest"):
            raise DefinitionSnapshotCorruption("legacy_source 순서 지문이 내용과 다릅니다")
        return definition_revision(definition)
    except CanonicalSerializationError as e:
        raise DefinitionSnapshotCorruption(f"정의를 정규 직렬화할 수 없습니다: {e}") from e


def build_envelope(definition: dict, *, source: dict | None = None,
                   compiler_version: int | None = None) -> dict:
    """스냅샷 파일 봉투. revision은 정의에서 재계산한다(호출자 값을 믿지 않는다).

    봉투의 compiler_version은 정의의 compiler_version(revision이 덮는 값)과 같아야 한다.
    호출자가 다른 값을 주면 거부한다.
    """
    rev = _verify_definition(definition)
    if compiler_version is None:
        compiler_version = definition["compiler_version"]
    elif compiler_version != definition["compiler_version"]:
        raise DefinitionSnapshotCompilerVersionMismatch(
            f"봉투 compiler_version {compiler_version!r} ≠ 정의 compiler_version "
            f"{definition['compiler_version']!r}")
    return {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "definition_revision": rev,
        "compiler_version": compiler_version,
        "source": source or {},
        "written_at": time.time(),
        "definition": definition,
    }


def _write_json_durable(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            # 삽입 순서를 보존해 쓴다 — legacy_source 순서 지문이 이것에 의존한다.
            json.dump(obj, f, ensure_ascii=False, indent=1, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise DefinitionSnapshotPersistenceError(f"정의 스냅샷 쓰기 실패: {path}") from e


def write_snapshot(session_id: str, compiled, *, source: dict | None = None) -> SessionDefinitionSnapshot:
    """세션 정의 스냅샷을 원자적으로 쓴다.

    compiled: CompileResult(성공) 또는 정의 dict.
    기존 스냅샷이 같은 revision이면 쓰지 않고 그것을 돌려준다(멱등).
    다른 revision이면 DefinitionSnapshotConflict — 덮어쓰지 않는다.
    기존 파일이 손상돼 있으면 DefinitionSnapshotCorruption — 역시 덮어쓰지 않는다
    (손상 판정·복구는 운영자 몫이며 조용한 재생성은 정의를 바꿀 수 있다).
    """
    if isinstance(compiled, CompileResult):
        definition = compiled.require_definition()
        compiler_version = compiled.compiler_version
    else:
        definition = compiled
        compiler_version = None          # 정의에서 읽는다
    envelope = build_envelope(definition, source=source, compiler_version=compiler_version)
    path = snapshot_path(session_id)
    if os.path.exists(path):
        existing = read_snapshot(session_id)
        if existing.definition_revision == envelope["definition_revision"]:
            return existing
        raise DefinitionSnapshotConflict(
            f"{session_id}: 기존 정의 {existing.definition_revision} ≠ 새 정의 "
            f"{envelope['definition_revision']} — 정의 교체는 명시적 migration이 필요합니다")
    _write_json_durable(path, envelope)
    # 쓴 바이트를 다시 읽어 검증한다(쓰기 성공 = 읽기 검증 성공).
    return read_snapshot(session_id, expected_revision=envelope["definition_revision"])


def _reject_constant(name):
    raise ValueError(f"비표준 JSON 상수 {name}")


def read_snapshot(session_id: str, *, expected_revision: str | None = None) -> SessionDefinitionSnapshot:
    """스냅샷을 읽고 검증한다. 실패는 모두 예외다."""
    path = snapshot_path(session_id)
    if not os.path.exists(path):
        raise DefinitionSnapshotMissing(f"정의 스냅샷이 없습니다: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            env = json.load(f, parse_constant=_reject_constant)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise DefinitionSnapshotCorruption(f"정의 스냅샷 해석 실패: {path}") from e
    if not isinstance(env, dict) or env.get("format") != SNAPSHOT_FORMAT:
        raise DefinitionSnapshotCorruption(f"스냅샷 형식이 아닙니다: {path}")
    if env.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise DefinitionSnapshotCorruption(f"지원하지 않는 스냅샷 형식 버전: {env.get('format_version')!r}")
    stored = env.get("definition_revision")
    actual = _verify_definition(env.get("definition"))
    if stored != actual:
        raise DefinitionSnapshotCorruption(
            f"{session_id}: 저장된 revision {stored} ≠ 내용 재계산 {actual}")
    if env.get("compiler_version") != env["definition"]["compiler_version"]:
        raise DefinitionSnapshotCompilerVersionMismatch(
            f"{session_id}: 봉투 compiler_version {env.get('compiler_version')!r} ≠ 정의 "
            f"compiler_version {env['definition']['compiler_version']!r}")
    if expected_revision is not None and actual != expected_revision:
        raise DefinitionSnapshotMismatch(
            f"{session_id}: 스냅샷 revision {actual} ≠ 기대 {expected_revision}")
    return SessionDefinitionSnapshot(
        session_id=str(session_id),
        definition_revision=actual,
        compiler_version=env.get("compiler_version"),
        definition=env["definition"],
        source=env.get("source") or {},
        written_at=env.get("written_at") or 0.0,
    )


def verify_snapshot(session_id: str, *, expected_revision: str | None = None) -> dict:
    """도구용 검증 보고(예외 대신 dict). DESIGN 08 §51."""
    try:
        snap = read_snapshot(session_id, expected_revision=expected_revision)
    except DefinitionSnapshotError as e:
        return {"session_id": str(session_id), "ok": False,
                "error": type(e).__name__, "message": str(e)}
    return {
        "session_id": snap.session_id,
        "ok": True,
        "definition_revision": snap.definition_revision,
        "compiler_version": snap.compiler_version,
        "scenario_id": snap.scenario_id,
        "source": snap.source,
    }
