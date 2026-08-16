# 되감기 델타 로그 — 턴별 상태 변화를 append-only로 기록하고 역순 복원한다
#
# [설계 근거]
#   SESSION_FIELDS는 최신 상태만 보관하므로 과거 시점을 재구성할 수 없다.
#   턴별 전체 스냅샷은 용량이 턴 수에 비례해 커지므로 델타만 기록한다.
#
# [파일 구성] 세션 JSON과 분리해 평상시 로드 비용에 영향을 주지 않는다.
#   sessions/{id}/rewind_log.jsonl      턴별 델타 (append-only)
#   sessions/{id}/rewind_archive.jsonl  되감기로 제거된 정보 보존
#   sessions/{id}/full_logs.jsonl       전 턴 대화 로그 (raw_logs 20개 캡 우회)
import json
import os
import time

REWIND_LOG = "rewind_log.jsonl"
REWIND_ARCHIVE = "rewind_archive.jsonl"
FULL_LOGS = "full_logs.jsonl"

# 되감기 가능한 최대 턴 수. 저장 용량과 복원 시간의 상한을 둔다.
REWIND_MAX_TURNS = 20

# 델타 추적 대상. 점 표기 경로의 최상위 키.
# NOTE: 새 상태 필드를 추가할 때 이곳에 등록해야 되감기 대상이 된다.
TRACKED_PATHS = [
    "resources",
    "statuses",
    "world_timeline",
    "info_ledger",
    "ability_stats",
    "total_cost",
    "auto_gm_turns_done",
    "compressed_memory",
    "last_extraction",
    "narrative_plan",
]


def _session_dir(session_id: str) -> str:
    return os.path.join("sessions", str(session_id))


def _append_jsonl(session_id: str, filename: str, payload: dict) -> bool:
    """JSONL 파일에 한 줄 추가. 실패해도 게임 진행을 막지 않는다."""
    try:
        d = _session_dir(session_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, filename), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"⚠️ [되감기] {filename} 기록 실패 ({session_id}): {e}")
        return False


def read_jsonl(session_id: str, filename: str) -> list:
    """JSONL 전체를 리스트로 읽는다. 손상된 줄은 건너뛴다."""
    path = os.path.join(_session_dir(session_id), filename)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"⚠️ [되감기] {filename} 읽기 실패 ({session_id}): {e}")
    return out


def capture_state(session) -> dict:
    """추적 대상 필드의 현재 값을 깊은 복사로 스냅샷한다.

    턴 시작 시 호출해 메모리에 들고 있다가, 턴 종료 시 diff_state로 비교한다.
    """
    import copy

    snap = {}
    for key in TRACKED_PATHS:
        try:
            snap[key] = copy.deepcopy(getattr(session, key, None))
        except Exception:
            snap[key] = None
    return snap


def diff_state(before: dict, after: dict) -> list:
    """두 스냅샷을 비교해 변경 목록을 만든다.

    dict는 하위 키 단위로, 그 외 타입은 최상위 단위로 비교한다.
    경로는 점 표기(resources.홍길동)로 기록한다.

    Returns:
        [{"path": str, "before": Any, "after": Any}, ...]
    """
    changes = []
    for key in TRACKED_PATHS:
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        if isinstance(b, dict) and isinstance(a, dict):
            for sub in set(b) | set(a):
                bv, av = b.get(sub), a.get(sub)
                if bv != av:
                    changes.append({"path": f"{key}.{sub}", "before": bv, "after": av})
        else:
            changes.append({"path": key, "before": b, "after": a})
    return changes


def record_delta(session, turn: int, changes: list,
                 compression: dict | None = None,
                 cost_krw: float = 0.0) -> bool:
    """턴 델타를 rewind_log.jsonl에 append한다.

    Args:
        turn: 이 델타가 속한 턴 번호
        changes: diff_state 결과
        compression: 이번 턴에 압축이 발생했다면
            {"occurred": True, "range": [start, end], "before": "이전 압축 원문"}
            압축 발생 시점과 이전 원본을 함께 남기면, 압축 주기를 몰라도
            목표 턴 이후의 압축만 골라 정확히 롤백할 수 있다.
        cost_krw: 이번 턴 비용 (되감기 시 정산 참고용)
    """
    if not changes and not compression:
        return True
    payload = {
        "turn": turn,
        "ts": time.time(),
        "changes": changes,
        "cost_krw": round(float(cost_krw or 0.0), 4),
    }
    if compression:
        payload["compression"] = compression
    return _append_jsonl(session.session_id, REWIND_LOG, payload)


def record_full_log(session, turn: int, entries: list) -> bool:
    """전 턴 대화 로그를 full_logs.jsonl에 보존한다.

    raw_logs는 최근 20개 캡이 걸려 있어 그것만으로는 과거 턴을 복원할 수 없다.
    여기에 남긴 원문으로 되감기 시 raw_logs를 재구성한다.
    통계(입출력 글자 수)와 압축 재계산에도 재사용된다.

    Args:
        entries: [{"role": "user"|"model", "text": str}, ...]
    """
    if not entries:
        return True
    return _append_jsonl(session.session_id, FULL_LOGS, {
        "turn": turn,
        "ts": time.time(),
        "entries": entries,
    })


def archive_removed(session, target_turn: int, removed: list) -> bool:
    """되감기로 제거된 정보를 rewind_archive.jsonl로 이관한다.

    기획 규정상 삭제가 아니라 '턴 되감기 로그로 이동'이다.
    """
    return _append_jsonl(session.session_id, REWIND_ARCHIVE, {
        "rewound_to": target_turn,
        "ts": time.time(),
        "removed": removed,
    })


def available_range(session) -> tuple:
    """되감기 가능한 턴 범위를 (최소, 최대)로 반환한다.

    REWIND_MAX_TURNS 상한과 실제 기록된 델타 중 좁은 쪽을 따른다.
    기록이 없으면 (0, 0).
    """
    deltas = read_jsonl(session.session_id, REWIND_LOG)
    if not deltas:
        return (0, 0)
    turns = [d.get("turn", 0) for d in deltas]
    newest = max(turns)
    oldest = max(min(turns), newest - REWIND_MAX_TURNS)
    return (oldest, newest)


def serialize_log_entries(contents) -> list:
    """types.Content 리스트를 full_logs 저장용 dict 리스트로 변환한다."""
    out = []
    for c in (contents or []):
        try:
            texts = [p.text for p in (c.parts or []) if getattr(p, "text", None)]
            if texts:
                out.append({"role": getattr(c, "role", "user"), "text": "\n".join(texts)})
        except Exception:
            continue
    return out
