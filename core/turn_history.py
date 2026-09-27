# core.turn_history — WP-E 정본 턴 이력(선택된 커밋 시도) · 되감기 · 같은 턴 재생성
#
# [소유권]
#   '게임 이력 선택'의 단일 권위. 어떤 논리 턴(gm_turn)에 어느 커밋 시도가 정본으로
#   선택돼 있는지, 되감기/재생성이 그 선택을 어떻게 바꾸는지를 durable하게 기록한다.
#
#   · 재무 이력(CostEvent/Settlement/InkTransaction/계정)은 이 모듈이 절대 건드리지 않는다
#     — 되감기·재생성은 환불/반전이 아니다(BILLING_POLICY BILL-04/08, AUD-029/030/032).
#   · 정상 턴 커밋 owner는 여전히 CommitCoordinator다. 이 모듈은 커밋이 끝난 시도의
#     레코드를 받아 선택 인덱스에 올리고(record_commit), 플레이어 이력 조작을 수행한다.
#   · CommitJournal은 커밋 복구 이력일 뿐 선택 원장이 아니다. 이력 조작 의도는 별도의
#     작은 파일(history_op.json)로 둔다 — 커밋 복구 phase와 의미가 섞이지 않는다.
#
# [저장]
#   sessions/{id}/turn_history/{transaction_id}.json   시도 레코드(불변, strict 원자 쓰기)
#     pre  = 이 턴 적용 직전의 가역 정본(재생성의 턴 이전 상태)
#     post = 이 턴 커밋 직후의 가역 정본(되감기 대상 상태)
#     judgment = {judgment, player_message, roll_results} 구조 전문(재생성 시 판단 보존)
#     canonical/media message IDs
#   sessions/{id}/turn_history.jsonl                   선택 인덱스(append-only, fsync)
#     SELECT {tx, logical_turn, attempt, gm_turn, record, supersedes}
#     REWIND {op_id, target_gm_turn, removed[]}
#     MESSAGES {tx, message_ids[]}
#   sessions/{id}/history_op.json                      진행 중 이력 조작 의도(재시작 정합용)
#
# [되감기 정의] 논리 턴 N으로 되감기 = 선택된 커밋 시도 N의 커밋 직후 정본 상태(post).
#   델타 역적용이 아니라 스냅샷 복원이므로 N의 추출 효과는 정확히 N에 속한다(AUD-020).

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid

from . import io as _io

HISTORY_SCHEMA_VERSION = 1
RECORD_DIR = "turn_history"
INDEX_FILE = "turn_history.jsonl"
OP_FILE = "history_op.json"

# 가역 정본(게임 이력) 필드. 운영/재무 필드는 절대 포함하지 않는다:
#   total_cost / total_usd / total_ink_spent / last_turn_cost / last_turn_ink,
#   캐시·압축 회계, last_compressed_turn/compression_count(기존 규정: 되감기로 재압축 금지).
REVERSIBLE_FIELDS = (
    "quest_state", "info_ledger", "resources", "statuses", "world_timeline",
    "visited_places", "companions", "met_npcs", "irregular_npcs", "npcs",
    "narrative_plan", "pending_ending", "last_extraction", "players",
    "stat_fail_counts", "turn_count", "gm_turns_done", "last_recorded_turn",
    "gm_proceed_history", "player_faction", "main_unlocked_notified",
    "pending_bgm", "last_bgm_situation", "start_day_number", "commit_marker",
    "raw_logs", "uncompressed_logs", "compressed_memory", "current_turn_logs",
    "gm_side_note", "gm_clarify_count", "gm_narrate_count",
)
OPERATIONAL_FIELDS = ("total_cost", "total_usd", "total_ink_spent",
                      "last_turn_cost", "last_turn_ink")
assert not set(REVERSIBLE_FIELDS) & set(OPERATIONAL_FIELDS)


class HistoryError(RuntimeError):
    """이력 파일 손상/영속 실패 — 추정하지 않고 표면화한다."""


# ══════════════════════════════════════════════════════════════
#  경로 / 저수준 durable I/O
# ══════════════════════════════════════════════════════════════

def _sdir(session_id) -> str:
    return os.path.join("sessions", str(session_id))


def record_path(session_id, transaction_id) -> str:
    return os.path.join(_sdir(session_id), RECORD_DIR, f"{transaction_id}.json")


def index_path(session_id) -> str:
    return os.path.join(_sdir(session_id), INDEX_FILE)


def op_path(session_id) -> str:
    return os.path.join(_sdir(session_id), OP_FILE)


def _write_json_strict(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise HistoryError(f"이력 파일 쓰기 실패: {path}") from e


def _read_json(path: str):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HistoryError(f"이력 파일 손상: {path}") from e


def _append_event(session_id, event: dict) -> None:
    path = index_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(dict(event, ts=time.time()), ensure_ascii=False) + "\n"
    pre = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            with open(path, "r+b") as f:
                f.truncate(pre)
        except Exception:
            pass
        raise HistoryError(f"이력 인덱스 append 실패: {path}") from e


def load_events(session_id) -> list:
    path = index_path(session_id)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise HistoryError(f"{path}:{lineno} 이력 인덱스 손상") from e
            if not isinstance(obj, dict) or obj.get("type") not in ("SELECT", "REWIND", "MESSAGES"):
                raise HistoryError(f"{path}:{lineno} 이력 인덱스 레코드 무효")
            out.append(obj)
    return out


# ══════════════════════════════════════════════════════════════
#  선택 인덱스 fold
# ══════════════════════════════════════════════════════════════

class HistoryView:
    """인덱스 이벤트를 순서대로 접은 결과 — 현재 선택 가지(branch)."""

    def __init__(self, events: list):
        self.events = events
        self.selected: dict = {}          # gm_turn -> SELECT 이벤트
        self.all_attempts: dict = {}      # tx -> SELECT 이벤트(과거 포함)
        self.messages: dict = {}          # tx -> [message ids]
        self.rewind_ops: set = set()
        for ev in events:
            t = ev["type"]
            if t == "SELECT":
                self.selected[int(ev["gm_turn"])] = ev
                self.all_attempts[ev["transaction_id"]] = ev
            elif t == "REWIND":
                self.rewind_ops.add(ev.get("op_id"))
                tgt = int(ev["target_gm_turn"])
                for g in [g for g in self.selected if g > tgt]:
                    del self.selected[g]
            elif t == "MESSAGES":
                self.messages.setdefault(ev["transaction_id"], []).extend(
                    ev.get("message_ids") or [])

    @property
    def head(self) -> int:
        return max(self.selected) if self.selected else 0

    def head_entry(self):
        return self.selected.get(self.head) if self.selected else None

    def is_selected(self, tx) -> bool:
        return any(e["transaction_id"] == tx for e in self.selected.values())

    def max_attempt(self, logical_turn) -> int:
        return max([int(e["attempt"]) for e in self.all_attempts.values()
                    if int(e["logical_turn"]) == int(logical_turn)] or [0])


def view(session_id) -> HistoryView:
    return HistoryView(load_events(session_id))


# ══════════════════════════════════════════════════════════════
#  가역 정본 캡처 / 복원
# ══════════════════════════════════════════════════════════════

def capture_reversible(session) -> dict:
    """가역 정본 필드의 JSON 직렬화본(깊은 복사). raw_logs는 {role,text}로."""
    out = {}
    for f in REVERSIBLE_FIELDS:
        v = getattr(session, f, None)
        if f == "raw_logs":
            out[f] = [e for e in (_io._serialize_log_entry(c) for c in (v or [])) if e]
        else:
            out[f] = json.loads(json.dumps(v, ensure_ascii=False, default=str))
    return out


def restore_reversible(session, state: dict) -> None:
    """가역 정본 필드만 복원한다. 운영/재무 필드는 건드리지 않는다."""
    from .cache import _deserialize_log_entry
    for f in REVERSIBLE_FIELDS:
        if f not in state:
            continue
        v = copy.deepcopy(state[f])
        if f == "raw_logs":
            v = [c for c in (_deserialize_log_entry(x) for x in (v or [])) if c is not None]
        setattr(session, f, v)
    from .rewind import capture_state
    session._rewind_snapshot = capture_state(session)


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()[:32]


# ══════════════════════════════════════════════════════════════
#  판단 기록(런타임 tx) — 재생성 시 보존 대상
# ══════════════════════════════════════════════════════════════

def note_judgment(session, transaction_id, *, judgment=None, player_message=None,
                  roll_results=None) -> None:
    """현재 tx에 구조화 판단 기록을 남긴다(판단층위 결과 + 지시층위 입력)."""
    from . import turn_transaction as TT
    if transaction_id is None or not TT.is_current_transaction(session, transaction_id):
        return
    tx = TT.get_active_transaction(session)
    cur = dict(tx.judgment_result or {})
    if judgment is not None:
        cur["judgment"] = copy.deepcopy(judgment)
    if player_message is not None:
        cur["player_message"] = str(player_message)
    if roll_results is not None:
        cur["roll_results"] = list(roll_results)
    cur.setdefault("roll_results", [])
    tx.judgment_result = cur


def judgment_is_complete(j) -> bool:
    return (isinstance(j, dict) and isinstance(j.get("judgment"), dict)
            and isinstance(j.get("player_message"), str))


# ══════════════════════════════════════════════════════════════
#  커밋 연결(CommitCoordinator E-2 단계에서 호출)
# ══════════════════════════════════════════════════════════════

def record_commit(session, plan, payload) -> dict:
    """커밋된 시도를 레코드로 남기고 선택 인덱스에 올린다.

    payload: {"pre", "post", "judgment"} (실행 중 커밋) 또는 None(재시작 복구 — 레코드 없음,
    degraded 선택). 같은 gm_turn의 기존 선택은 supersede된다(재생성 교체). 재무 기록 불변.
    Returns: {"superseded": SELECT 이벤트|None, "record": bool}
    """
    sid = session.session_id
    hv = view(sid)
    if plan.transaction_id in hv.all_attempts:
        prev = hv.all_attempts[plan.transaction_id]
        return {"superseded": None, "record": bool(prev.get("record")), "duplicate": True}
    has_record = False
    if payload is not None:
        rec = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "session_id": sid,
            "transaction_id": plan.transaction_id,
            "logical_turn": plan.logical_turn,
            "attempt": plan.attempt,
            "settlement_id": plan.settlement_id,
            "story_turn": plan.story_turn,
            "gm_turn": plan.gm_turn,
            "plan_fingerprint": plan.fingerprint,
            "player_declaration": plan.player_declaration,
            "judgment": copy.deepcopy(payload.get("judgment")),
            "pre": payload["pre"],
            "post": payload["post"],
            "canonical_message_ids": list(plan.canonical_message_ids),
            "media_message_ids": list(plan.media_message_ids),
            "committed_at": time.time(),
        }
        rec["post_digest"] = _digest(rec["post"])
        _write_json_strict(record_path(sid, plan.transaction_id), rec)
        has_record = True
    old = hv.selected.get(int(plan.gm_turn))
    superseded = old if (old is not None and old["transaction_id"] != plan.transaction_id) else None
    _append_event(sid, {
        "type": "SELECT", "transaction_id": plan.transaction_id,
        "logical_turn": plan.logical_turn, "attempt": plan.attempt,
        "gm_turn": plan.gm_turn, "story_turn": plan.story_turn,
        "settlement_id": plan.settlement_id, "record": has_record,
        "rerenderable": bool(has_record and judgment_is_complete((payload or {}).get("judgment"))),
        "supersedes": superseded["transaction_id"] if superseded else None,
    })
    op = read_op(sid)
    if op and op.get("op") == "RERENDER" and op.get("new_tx") == plan.transaction_id:
        clear_op(sid)
    return {"superseded": superseded, "record": has_record}


def append_messages(session_id, transaction_id, message_ids) -> None:
    ids = [m for m in (message_ids or []) if m is not None]
    if ids:
        _append_event(session_id, {"type": "MESSAGES", "transaction_id": transaction_id,
                                   "message_ids": ids})


def load_record(session_id, transaction_id):
    return _read_json(record_path(session_id, transaction_id))


def attempt_message_ids(session_id, entry, hv=None) -> list:
    """시도의 봇 정본 출력 ID(묘사·미디어·파생 게임 메시지). 플레이어 메시지 없음."""
    hv = hv or view(session_id)
    ids = []
    rec = None
    try:
        rec = load_record(session_id, entry["transaction_id"])
    except HistoryError:
        rec = None
    if rec:
        ids += list(rec.get("canonical_message_ids") or [])
        ids += list(rec.get("media_message_ids") or [])
    ids += list(hv.messages.get(entry["transaction_id"]) or [])
    return [i for i in dict.fromkeys(ids) if i is not None]


# ══════════════════════════════════════════════════════════════
#  진행 중 이력 조작 의도
# ══════════════════════════════════════════════════════════════

def read_op(session_id):
    return _read_json(op_path(session_id))


def write_op(session_id, op: dict) -> None:
    _write_json_strict(op_path(session_id), op)


def clear_op(session_id) -> None:
    p = op_path(session_id)
    if os.path.exists(p):
        os.remove(p)


# ══════════════════════════════════════════════════════════════
#  사용 가능 범위 / 전제조건
# ══════════════════════════════════════════════════════════════

REWIND_MAX_TURNS = 20


def _degraded(session) -> set:
    return {int(t) for t in (getattr(session, "rewind_degraded_turns", None) or [])}


def available_range(session) -> tuple:
    """(oldest, newest) — newest = 선택 head, oldest = 정확히 되돌릴 수 있는 가장 이른 턴.

    대상 N은 레코드(post)가 있어야 하고 (N, head] 구간에 degraded/레코드 없는 선택이
    없어야 한다(공백을 넘어 추정하지 않는다). 없으면 (0, 0).
    """
    try:
        hv = view(session.session_id)
    except HistoryError:
        return (0, 0)
    if not hv.selected:
        return (0, 0)
    newest = hv.head
    deg = _degraded(session)
    oldest = newest
    g = newest
    while g - 1 in hv.selected:
        above = hv.selected[g]
        if g in deg or not above.get("record"):
            break
        cand = hv.selected[g - 1]
        if not cand.get("record"):
            break
        oldest = g - 1
        g -= 1
    oldest = max(oldest, newest - REWIND_MAX_TURNS)
    if oldest == newest:
        return (0, 0) if not hv.selected[newest].get("record") else (newest, newest)
    return (oldest, newest)


def _busy_reason(session):
    from . import turn_preparation as TP
    from . import turn_transaction as TT
    if getattr(session, "is_processing", False):
        return "턴 진행 중에는 사용할 수 없습니다."
    if getattr(session, "commit_recovery", None):
        return "커밋 복구가 끝나지 않았습니다."
    if getattr(session, "extraction_pending", False):
        return "이전 턴 정보 정리(추출)가 끝나지 않았습니다."
    if getattr(session, "is_compressing", False):
        return "기억 압축이 진행 중입니다. 잠시 후 다시 시도해 주십시오."
    if read_op(session.session_id):
        return "다른 이력 조작이 진행 중입니다."
    tx = TT.get_active_transaction(session)
    if tx is not None and not TT.is_terminal(tx.status):
        prep = getattr(tx, "preparation", None)
        if prep is not None and getattr(prep, "phase", None) not in (None, TP.PREP_COLLECTING):
            return "진행 중인 턴 준비가 있습니다."
    return None


def _abort_waiting_tx(session) -> None:
    """ASK/NARRATE/ROLL 대기 중인(묘사 이전) 시도는 이력 조작 전에 ABORTED로 종결한다."""
    from . import turn_transaction as TT
    tx = TT.get_active_transaction(session)
    if tx is not None and not TT.is_terminal(tx.status):
        TT.finalize(session, tx.transaction_id, TT.TurnStatus.ABORTED,
                    failure_stage="HISTORY_OPERATION")


# ══════════════════════════════════════════════════════════════
#  되감기
# ══════════════════════════════════════════════════════════════

async def rewind(bot, session, target_gm_turn: int) -> dict:
    """선택된 커밋 시도 N의 커밋 직후 정본으로 되감는다(게임 이력만).

    순서: 전제조건 → 의도 기록 → (io 락) 복원 + strict 저장 → REWIND 이벤트 → 의도 해제.
    Returns: {"ok", "reason", "removed_turns", "removed": [SELECT…], "target_entry"}
    """
    sid = session.session_id
    fail = {"ok": False, "removed_turns": [], "removed": []}
    reason = _busy_reason(session)
    if reason:
        return dict(fail, reason=reason)
    oldest, newest = available_range(session)
    target = int(target_gm_turn)
    if newest == 0:
        return dict(fail, reason="되감을 수 있는 커밋 이력이 없습니다.")
    if target >= newest:
        return dict(fail, reason=f"현재 턴({newest}) 이전만 되감을 수 있습니다.")
    if target < oldest:
        return dict(fail, reason=f"되감기 가능 범위는 {oldest}~{newest - 1}턴입니다 "
                                 f"(기록 공백·한도 이전은 안전하게 되돌릴 수 없습니다).")
    hv = view(sid)
    entry = hv.selected.get(target)
    rec = load_record(sid, entry["transaction_id"]) if entry else None
    if not rec or rec.get("post_digest") != _digest(rec.get("post")):
        return dict(fail, reason="대상 턴의 정본 기록이 없거나 손상되었습니다.")
    removed = [hv.selected[g] for g in sorted(hv.selected) if g > target]
    _abort_waiting_tx(session)
    op_id = uuid.uuid4().hex
    op = {"op": "REWIND", "op_id": op_id, "target_gm_turn": target,
          "target_tx": entry["transaction_id"],
          "target_marker": rec["post"].get("commit_marker"),
          "from_marker": copy.deepcopy(getattr(session, "commit_marker", None)),
          "removed": [e["transaction_id"] for e in removed], "started_at": time.time()}
    write_op(sid, op)
    backup = capture_reversible(session)
    backup_deg = list(getattr(session, "rewind_degraded_turns", None) or [])
    try:
        async with _io.session_io_lock(bot, session):
            restore_reversible(session, rec["post"])
            session.current_turn_logs = []
            session.gm_side_note = ""
            session.gm_clarify_count = 0
            session.gm_narrate_count = 0
            session.extraction_pending = False
            session.extraction_retry_ctx = {}
            session.rewind_degraded_turns = [t for t in backup_deg if int(t) <= target]
            await _io.write_session_strict_locked(session)
    except Exception as e:  # 영속 전 — 원래 head가 정본으로 남는다
        restore_reversible(session, backup)
        session.rewind_degraded_turns = backup_deg
        try:
            clear_op(sid)
        except Exception:
            pass
        return dict(fail, reason=f"되감기 저장 실패 — 변경 없음: {type(e).__name__}")
    # 여기부터 data.json은 대상 상태 — 선택 인덱스를 맞춘다(실패 시 복구가 재조정).
    try:
        _append_event(sid, {"type": "REWIND", "op_id": op_id, "target_gm_turn": target,
                            "removed": op["removed"],
                            "disposition": "REWOUND_BY_PLAYER"})
        clear_op(sid)
    except Exception as e:
        session.commit_recovery = {"status": "PENDING", "stage": "HISTORY_REWIND",
                                   "transaction_id": entry["transaction_id"],
                                   "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "reason": "이력 인덱스 갱신 보류(복구 대기)",
                "removed_turns": [e["gm_turn"] for e in removed], "removed": removed,
                "target_entry": entry, "pending": True}
    _legacy_logs_truncate(session, target)
    return {"ok": True, "reason": "", "removed_turns": [e["gm_turn"] for e in removed],
            "removed": removed, "target_entry": entry}


def _legacy_logs_truncate(session, target) -> None:
    """레거시 호환 파일(rewind_log/full_logs)도 선택 가지에 맞춘다(비권위, best-effort)."""
    try:
        from . import rewind as _rw
        doomed = [d for d in _rw.read_jsonl(session.session_id, _rw.REWIND_LOG)
                  if d.get("turn", 0) > target]
        if doomed:
            _rw.archive_removed(session, target, doomed)
        _rw._truncate_jsonl(session.session_id, _rw.REWIND_LOG, target)
        _rw._truncate_jsonl(session.session_id, _rw.FULL_LOGS, target)
    except Exception as e:
        print(f"[WP-E] 레거시 되감기 로그 정리 실패(비권위): {e}")


# ══════════════════════════════════════════════════════════════
#  같은 턴 재생성
# ══════════════════════════════════════════════════════════════

def rerender_target(session):
    """(entry, record, reason) — 재생성 대상은 선택 head 시도뿐이다."""
    reason = _busy_reason(session)
    if reason:
        return None, None, reason
    try:
        hv = view(session.session_id)
    except HistoryError as e:
        return None, None, f"이력 손상: {e}"
    entry = hv.head_entry()
    if entry is None:
        return None, None, "재생성할 커밋 턴이 없습니다."
    if int(entry["gm_turn"]) in _degraded(session) or not entry.get("record"):
        return None, None, "이 턴은 기록 공백으로 재생성할 수 없습니다."
    try:
        rec = load_record(session.session_id, entry["transaction_id"])
    except HistoryError as e:
        return None, None, f"이력 레코드 손상: {e}"
    if not rec or not judgment_is_complete(rec.get("judgment")):
        return None, None, ("이 턴에는 보존된 판단 결과가 없어 재생성할 수 없습니다 "
                            "(WP-E 이전 턴 — 판단을 다시 추정하지 않습니다).")
    if (session.commit_marker or {}).get("transaction_id") != entry["transaction_id"]:
        return None, None, "현재 정본 표식이 선택 이력과 다릅니다(복구 필요)."
    return entry, rec, None


async def begin_rerender(bot, session, entry, rec, *, addendum: str = ""):
    """재생성 시작: attempt+1 tx 생성 → 의도 기록 → 턴 이전 상태 복원 + strict 저장.

    이전 시도는 교체 시도가 실제로 COMMITTED될 때까지 선택 정본으로 남는다(선택 인덱스
    불변). data.json의 임시 이전 상태는 의도 파일로 재시작 정합이 보장된다.
    Returns: 새 TurnTransaction. 실패 시 HistoryError(상태 원복됨).
    """
    from . import turn_transaction as TT
    sid = session.session_id
    _abort_waiting_tx(session)
    lt = int(rec["logical_turn"])
    hv = view(sid)
    counters = TT._counters(session)
    counters[lt] = max(int(counters.get(lt, 0)), hv.max_attempt(lt))
    tx = TT.begin_attempt(session, logical_turn=lt,
                          player_declaration=rec.get("player_declaration") or "")
    tx.judgment_result = copy.deepcopy(rec["judgment"])
    backup = capture_reversible(session)
    op = {"op": "RERENDER", "op_id": uuid.uuid4().hex, "old_tx": rec["transaction_id"],
          "new_tx": tx.transaction_id, "logical_turn": lt, "gm_turn": int(rec["gm_turn"]),
          "old_marker": copy.deepcopy(getattr(session, "commit_marker", None)),
          "started_at": time.time()}
    try:
        write_op(sid, op)
        async with _io.session_io_lock(bot, session):
            restore_reversible(session, rec["pre"])
            if addendum:
                note = (session.gm_side_note or "").strip()
                session.gm_side_note = (f"{note}\n" if note else "") + f"[재생성 지시] {addendum}"
            session.extraction_pending = False
            session.extraction_retry_ctx = {}
            await _io.write_session_strict_locked(session)
    except Exception as e:
        restore_reversible(session, backup)
        TT.finalize(session, tx.transaction_id, TT.TurnStatus.ABORTED,
                    failure_stage="RERENDER_BEGIN")
        try:
            clear_op(sid)
        except Exception:
            pass
        raise HistoryError(f"재생성 시작 실패: {type(e).__name__}: {e}") from e
    return tx


async def abort_rerender(bot, session) -> bool:
    """교체 시도가 커밋되지 못함 — 이전 시도의 커밋 직후 정본으로 되돌리고 의도 해제.

    교체 시도가 이미 durable COMMITTED(청구 완료)라면 되돌리지 않는다 — 그 이야기가
    정본이다. 그 경우 HistoryError로 거부하고 settle_rerender/reconcile이 선택을 맞춘다.
    """
    sid = session.session_id
    op = read_op(sid)
    if not op or op.get("op") != "RERENDER":
        return False
    if _journal_committed(sid, op["new_tx"]):
        raise HistoryError("교체 시도가 이미 COMMITTED — 이전 시도로 되돌리지 않습니다")
    rec = load_record(sid, op["old_tx"])
    if not rec:
        raise HistoryError("재생성 이전 시도 레코드 없음")
    async with _io.session_io_lock(bot, session):
        restore_reversible(session, rec["post"])
        session.extraction_pending = False
        session.extraction_retry_ctx = {}
        await _io.write_session_strict_locked(session)
    clear_op(sid)
    return True


async def settle_rerender(bot, session) -> dict:
    """교체 시도 종결 후 남은 RERENDER 의도를 처분한다(런타임 경로).

    · 교체 시도가 durable COMMITTED인데 선택 기록이 실패했다 → 선택 정합(reconcile:
      degraded SELECT, supersedes 포함)으로 교체를 정본으로 확정한다.
    · 커밋되지 않았다 → abort_rerender(이전 시도 커밋 직후 정본 복원).
    Returns: {"action": "SELECTED"|"ABORTED"|"NONE"|<차단 stage>, "superseded": SELECT|None}
    """
    sid = session.session_id
    op = read_op(sid)
    if not op or op.get("op") != "RERENDER":
        return {"action": "NONE", "superseded": None}
    if _journal_committed(sid, op["new_tx"]):
        res = await reconcile(bot, session)
        if not res.get("ok"):
            return {"action": res.get("action"), "superseded": None}
        hv = view(sid)
        ev = hv.all_attempts.get(op["new_tx"])
        sup = hv.all_attempts.get((ev or {}).get("supersedes")) if ev else None
        return {"action": "SELECTED", "superseded": sup}
    await abort_rerender(bot, session)
    return {"action": "ABORTED", "superseded": None}


def rerender_op_for(session_id, transaction_id):
    op = read_op(session_id)
    if op and op.get("op") == "RERENDER" and op.get("new_tx") == transaction_id:
        return op
    return None


# ══════════════════════════════════════════════════════════════
#  재시작 / 복구 정합
# ══════════════════════════════════════════════════════════════

def _plan_for(session_id, transaction_id):
    from . import commit_coordinator as CC
    from . import commit_journal as cj
    for e in CC.journal_for(session_id).history_for_transaction(transaction_id):
        if e.phase == cj.CommitPhase.PREPARED:
            return CC.CommitPlan.from_journal_metadata(e.metadata)
    return None


def _journal_committed(session_id, transaction_id) -> bool:
    from . import commit_coordinator as CC
    from . import commit_journal as cj
    return any(e.phase == cj.CommitPhase.COMMITTED
               for e in CC.journal_for(session_id).history_for_transaction(transaction_id))


async def reconcile(bot, session) -> dict:
    """durable 사실(data.json 표식 · 저널 · 선택 인덱스 · 의도 파일)을 맞춘다.

    D 커밋 복구 이후에 호출된다. 추정하지 않으며 모순이면 차단한다.
    Returns: {"ok": bool, "action": str, "detail": str}
    """
    sid = session.session_id
    try:
        hv = view(sid)
        op = read_op(sid)
    except HistoryError as e:
        return _history_block(session, "HISTORY_CORRUPT", str(e))
    marker = getattr(session, "commit_marker", None) or {}
    mtx = marker.get("transaction_id")
    actions = []
    try:
        # ① 커밋은 됐는데 선택 인덱스에 없는 시도(커밋~이력 기록 사이 크래시) → degraded 선택
        if mtx and mtx not in hv.all_attempts and _journal_committed(sid, mtx):
            plan = _plan_for(sid, mtx)
            if plan is None:
                return _history_block(session, "HISTORY_PLAN_MISSING", mtx)
            record_commit(session, plan, None)
            deg = list(getattr(session, "rewind_degraded_turns", None) or [])
            if plan.gm_turn not in deg:
                deg.append(plan.gm_turn)
                session.rewind_degraded_turns = deg
            actions.append("SELECT_DEGRADED")
            hv = view(sid)
            op = read_op(sid)
        # ② 진행 중이던 이력 조작
        from . import turn_transaction as TT
        _act = TT.get_active_transaction(session)
        if (op and op.get("op") == "RERENDER" and _act is not None
                and _act.transaction_id == op.get("new_tx") and not TT.is_terminal(_act.status)):
            pass            # 교체 시도가 아직 살아 있음(재시도 대기 등) — 건드리지 않는다
        elif op and op.get("op") == "RERENDER":
            if hv.is_selected(op["new_tx"]):
                clear_op(sid)
                actions.append("RERENDER_DONE")
            elif _journal_committed(sid, op["new_tx"]):
                # 교체가 커밋됐는데 표식도 선택도 그 시도가 아니다 — 추정하지 않는다
                return _history_block(session, "HISTORY_RERENDER_CONFLICT",
                                      f"COMMITTED 교체 시도 {op['new_tx']}가 표식/선택과 불일치")
            else:
                await abort_rerender(bot, session)
                actions.append("RERENDER_ABORTED")
        elif op and op.get("op") == "REWIND":
            if op["op_id"] in hv.rewind_ops:
                clear_op(sid)
                _legacy_logs_truncate(session, int(op["target_gm_turn"]))
                actions.append("REWIND_DONE")
            elif _digest(marker) == _digest(op.get("target_marker") or {}):
                _append_event(sid, {"type": "REWIND", "op_id": op["op_id"],
                                    "target_gm_turn": int(op["target_gm_turn"]),
                                    "removed": op.get("removed") or [],
                                    "disposition": "REWOUND_BY_PLAYER"})
                clear_op(sid)
                _legacy_logs_truncate(session, int(op["target_gm_turn"]))
                actions.append("REWIND_FINALIZED")
            elif _digest(marker) == _digest(op.get("from_marker") or {}):
                clear_op(sid)
                actions.append("REWIND_NOT_APPLIED")
            else:
                return _history_block(session, "HISTORY_REWIND_CONFLICT",
                                      "data.json 표식이 되감기 대상/출발 어느 쪽과도 다름")
        # ③ 선택 head와 data.json 표식 일치(E 시기 표식만)
        hv = view(sid)
        head = hv.head_entry()
        if mtx and head is not None and head["transaction_id"] != mtx:
            return _history_block(session, "HISTORY_HEAD_MISMATCH",
                                  f"선택 head {head['transaction_id']} ≠ 표식 {mtx}")
    except HistoryError as e:
        return _history_block(session, "HISTORY_IO", str(e))
    cr = getattr(session, "commit_recovery", None)
    if cr and str(cr.get("stage", "")).startswith("HISTORY"):
        session.commit_recovery = None
    return {"ok": True, "action": ",".join(actions) or "CLEAN", "detail": ""}


def _history_block(session, stage, detail) -> dict:
    session.commit_recovery = {"status": "RECOVERY_REQUIRED" if "CORRUPT" in stage
                               or "CONFLICT" in stage or "MISMATCH" in stage else "PENDING",
                               "stage": stage, "error": detail}
    _io.write_log(session.session_id, "error", f"[WP-E] 이력 정합 차단 {stage} — {detail}")
    print(f"⛔ [WP-E/{session.session_id}] 이력 정합 차단 {stage}: {detail}")
    return {"ok": False, "action": stage, "detail": detail}
