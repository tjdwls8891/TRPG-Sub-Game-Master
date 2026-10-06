# 세계 예약(pending queue) · base quiescence · 시간/예약 typed 연산 (H03-A)
#
# [권위 — DESIGN 03 §43~48 · DESIGN 11 §1~5 · H03 결정 게이트 A16·A17]
#   world_schedule = {schedule_id: entry}  — **pending 항목만**. 취소·소비된 항목은 live
#   큐에서 지운다(tombstone을 정본에 남기지 않는다). entry:
#     {"schedule_id", "due_minute", "kind", "owner_ref", "payload_ref",
#      "created_by_version", "order_key"}
#   같은 분의 순서 = (due_minute, order_key, schedule_id). dict 삽입 순서는 의미가 없다.
#
# [base quiescence — READY의 실제 증명]
#   world_clock 미초기화 + 빈 예약          → quiescent(레거시)
#   world_clock 미초기화 + 예약 있음        → NOT quiescent(무효 상태)
#   초기화된 clock + 모든 pending이 due > now → quiescent
#   due ≤ now pending이 하나라도 남으면 READY 불가. H04가 사건 cascade 조건을 더한다.
#
# [하지 않는 것]
#   · 예약의 의미(effect)를 처리하지 않는다 — SCHEDULE_CONSUME은 live 큐에서 제거만 한다.
#     사건·세계 진행 결합은 H04.
#   · production 플레이어 경로가 이 연산들을 만들지 않는다(H03 producer 0).
from __future__ import annotations

from dataclasses import dataclass, field

from . import world_clock as _wc
from . import world_mutation as _wm

KIND_EVENT_WAKEUP = "EVENT_WAKEUP"
KIND_DEFERRED_EFFECT = "DEFERRED_EFFECT"
KIND_MOVEMENT_WAKEUP = "MOVEMENT_WAKEUP"
SCHEDULE_KINDS = (KIND_EVENT_WAKEUP, KIND_DEFERRED_EFFECT, KIND_MOVEMENT_WAKEUP)

OP_CLOCK_INITIALIZE = "CLOCK_INITIALIZE"
OP_CLOCK_ADVANCE_TO = "CLOCK_ADVANCE_TO"
OP_SCHEDULE_CREATE = "SCHEDULE_CREATE"
OP_SCHEDULE_CANCEL = "SCHEDULE_CANCEL"
OP_SCHEDULE_CONSUME = "SCHEDULE_CONSUME"
TIME_OP_TYPES = (OP_CLOCK_INITIALIZE, OP_CLOCK_ADVANCE_TO, OP_SCHEDULE_CREATE,
                 OP_SCHEDULE_CANCEL, OP_SCHEDULE_CONSUME)

_ENTRY_KEYS = ("schedule_id", "due_minute", "kind", "owner_ref", "payload_ref",
               "created_by_version", "order_key")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# ══════════════════════════════════════════════════════════════
#  순수 질의
# ══════════════════════════════════════════════════════════════

def sort_key(entry: dict) -> tuple:
    return (entry["due_minute"], entry.get("order_key") or "", entry["schedule_id"])


def ordered(schedule: dict) -> list:
    """pending 항목의 결정적 순서(삽입 순서 무관)."""
    return sorted((schedule or {}).values(), key=sort_key)


def due_at_or_before(schedule: dict, minute: int) -> list:
    return [e for e in ordered(schedule) if e["due_minute"] <= minute]


def next_due_after(schedule: dict, minute: int):
    for e in ordered(schedule):
        if e["due_minute"] > minute:
            return e
    return None


def same_minute_batch(schedule: dict, minute: int) -> list:
    return [e for e in ordered(schedule) if e["due_minute"] == minute]


def validate_schedule(schedule) -> list:
    if schedule == {}:
        return []
    if not isinstance(schedule, dict):
        return ["world_schedule은 객체여야 합니다"]
    errs = []
    for sid, e in schedule.items():
        if not isinstance(e, dict) or e.get("schedule_id") != sid:
            errs.append(f"예약 {sid!r}: 키와 schedule_id가 다릅니다")
            continue
        if not _is_int(e.get("due_minute")):
            errs.append(f"예약 {sid!r}: due_minute가 정수가 아닙니다")
        if e.get("kind") not in SCHEDULE_KINDS:
            errs.append(f"예약 {sid!r}: 알 수 없는 kind {e.get('kind')!r}")
        if set(e) - set(_ENTRY_KEYS):
            errs.append(f"예약 {sid!r}: 알 수 없는 필드 {sorted(set(e) - set(_ENTRY_KEYS))}")
    return errs


@dataclass(frozen=True)
class BaseQuiescenceReport:
    """READY의 기본 안정상태 증명(H03). 사건 cascade 조건은 H04가 확장한다."""
    quiescent: bool
    clock_initialized: bool
    current_minute: int | None
    pending_count: int
    overdue_ids: tuple = ()
    reasons: tuple = ()

    def to_dict(self) -> dict:
        return {"quiescent": self.quiescent, "clock_initialized": self.clock_initialized,
                "current_minute": self.current_minute, "pending_count": self.pending_count,
                "overdue_ids": list(self.overdue_ids), "reasons": list(self.reasons)}


def base_quiescence(world_clock, world_schedule) -> BaseQuiescenceReport:
    schedule = world_schedule or {}
    reasons = list(_wc.validate_clock(world_clock if world_clock is not None else {}))
    reasons += validate_schedule(schedule)
    init = _wc.is_initialized(world_clock)
    now = world_clock["minute_index"] if init else None
    overdue = ()
    if not init and schedule:
        reasons.append("world_clock 미초기화 상태에 pending 예약이 있습니다")
    if init and not reasons:
        overdue = tuple(e["schedule_id"] for e in due_at_or_before(schedule, now))
        if overdue:
            reasons.append(f"현재 시각({now}) 이하로 도래한 pending 예약 {len(overdue)}건")
    return BaseQuiescenceReport(not reasons, init, now, len(schedule), overdue, tuple(reasons))


def base_quiescence_of(world) -> BaseQuiescenceReport:
    """ProjectedWorld 또는 세션에서 증명."""
    if isinstance(world, _wm.ProjectedWorld):
        return base_quiescence(world.domain("world_clock"), world.domain("world_schedule"))
    return base_quiescence(getattr(world, "world_clock", {}), getattr(world, "world_schedule", {}))


# ══════════════════════════════════════════════════════════════
#  typed 연산 handler (production 레지스트리에 등록 — producer는 아직 없다)
# ══════════════════════════════════════════════════════════════

def _reject(op, msg):
    raise _wm.WorldMutationError(f"{op.op_id}: {msg}")


def _require_keys(op, payload, required, optional=()):
    extra = set(payload) - set(required) - set(optional)
    if extra:
        _reject(op, f"알 수 없는 payload 키 {sorted(extra)}")
    missing = [k for k in required if k not in payload]
    if missing:
        _reject(op, f"payload 누락 {missing}")


def _view_clock(view):
    return view.domain("world_clock")


def h_clock_initialize(view, op, payload):
    """미초기화 시계에 시작 시각을 둔다(H06 초기화 전용 — H03 production caller 0)."""
    _require_keys(op, payload, ("minute_index",))
    if not _is_int(payload["minute_index"]):
        _reject(op, "minute_index는 정수여야 합니다")
    if _view_clock(view) != {}:
        _reject(op, "world_clock이 이미 초기화돼 있습니다")
    m = payload["minute_index"]
    return [_wm.write(op, "world_clock", ("minute_index",), _wm.MERGE_SINGLE_VALUE, m),
            _wm.write(op, "world_clock", ("start_minute_index",), _wm.MERGE_SINGLE_VALUE, m)]


def h_clock_advance_to(view, op, payload):
    _require_keys(op, payload, ("target_minute",))
    clock = _view_clock(view)
    if not _wc.is_initialized(clock):
        _reject(op, "world_clock이 초기화되지 않았습니다")
    try:
        target = _wc.advance_to(clock["minute_index"], payload["target_minute"])
    except _wc.ClockError as e:
        _reject(op, str(e))
    return [_wm.write(op, "world_clock", ("minute_index",), _wm.MERGE_SINGLE_VALUE, target)]


def h_schedule_create(view, op, payload):
    _require_keys(op, payload, ("schedule_id", "due_minute", "kind"),
                  ("owner_ref", "payload_ref", "order_key"))
    sid = payload["schedule_id"]
    if not isinstance(sid, str) or not sid:
        _reject(op, "schedule_id는 비어 있지 않은 문자열이어야 합니다")
    if payload["kind"] not in SCHEDULE_KINDS:
        _reject(op, f"알 수 없는 예약 kind {payload['kind']!r}")
    if not _is_int(payload["due_minute"]):
        _reject(op, "due_minute는 정수여야 합니다")
    clock = _view_clock(view)
    if not _wc.is_initialized(clock):
        _reject(op, "world_clock 미초기화 상태에서는 예약을 만들 수 없습니다")
    if payload["due_minute"] < clock["minute_index"]:
        _reject(op, "과거 시각 예약은 만들 수 없습니다(DESIGN 11 §4)")
    if view.has("world_schedule", (sid,)):
        _reject(op, f"이미 있는 예약 {sid}")
    entry = {"schedule_id": sid, "due_minute": payload["due_minute"], "kind": payload["kind"],
             "owner_ref": payload.get("owner_ref"), "payload_ref": payload.get("payload_ref"),
             "created_by_version": view.get("world_version", ("version_id",)),
             "order_key": payload.get("order_key") or sid}
    return [_wm.write(op, "world_schedule", (sid,), _wm.MERGE_SINGLE_VALUE, entry)]


def h_schedule_cancel(view, op, payload):
    _require_keys(op, payload, ("schedule_id",))
    sid = payload["schedule_id"]
    if not view.has("world_schedule", (sid,)):
        _reject(op, f"없는 예약 {sid}")
    return [_wm.write(op, "world_schedule", (sid,), _wm.MERGE_DELETE, None)]


def h_schedule_consume(view, op, payload):
    """도래한 예약을 live 큐에서 제거한다(의미 처리는 H04)."""
    _require_keys(op, payload, ("schedule_id",))
    sid = payload["schedule_id"]
    entry = view.get("world_schedule", (sid,))
    if entry is None:
        _reject(op, f"없는 예약 {sid}")
    clock = _view_clock(view)
    if not _wc.is_initialized(clock) or entry["due_minute"] > clock["minute_index"]:
        _reject(op, f"아직 도래하지 않은 예약 {sid}")
    return [_wm.write(op, "world_schedule", (sid,), _wm.MERGE_DELETE, None)]


_HANDLERS = {
    OP_CLOCK_INITIALIZE: h_clock_initialize,
    OP_CLOCK_ADVANCE_TO: h_clock_advance_to,
    OP_SCHEDULE_CREATE: h_schedule_create,
    OP_SCHEDULE_CANCEL: h_schedule_cancel,
    OP_SCHEDULE_CONSUME: h_schedule_consume,
}


def register(registry: _wm.OperationRegistry) -> None:
    for op_type, h in _HANDLERS.items():
        registry.register(op_type, h)
