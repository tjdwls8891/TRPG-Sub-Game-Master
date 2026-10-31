"""시간 — 세계 시계(world.clock)·예약 큐(world.schedule).

  world.clock    {} 미초기화 | {"minute_index", "start_minute_index"}(달력 원점부터 절대 분, 값 ≥ 1440)
  world.schedule {schedule_id: 예약 11키} — pending만(빈 큐는 {})
의미는 옛 core.world_schedule handler와 같다(후퇴 거부·0분 진행 허용·과거 예약 금지·도래 전 소비 금지).
"""
from indaim.kernel import WORLD, Component
from indaim.kernel.components import DELETE, SINGLE_VALUE, write

from .._common import ident, is_int, keys, reject

MIN_MINUTE_INDEX = 1440
SCHEDULE_KINDS = ("EVENT_WAKEUP", "DEFERRED_EFFECT", "MOVEMENT_WAKEUP")
ENTRY_KEYS = ("schedule_id", "due_minute", "kind", "owner_ref", "payload_ref", "created_by_version", "order_key")
EXT_KEYS = ("payload", "guard_ref", "owner_epoch", "conflict_priority")


def clock_of(view) -> dict:
    return view.read(WORLD, "", "clock")


def initialized(c) -> bool:
    """초기화 '형태'(두 키가 정수) — 옛 world_clock.is_initialized."""
    return isinstance(c, dict) and is_int(c.get("minute_index")) and is_int(c.get("start_minute_index"))


def valid(c) -> bool:
    """초기화됐고 정본 범위 안(옛 is_initialized ∧ validate_clock 무오류)."""
    return (initialized(c) and set(c) == {"minute_index", "start_minute_index"}
            and c["start_minute_index"] >= MIN_MINUTE_INDEX and c["minute_index"] >= c["start_minute_index"])


def ready_clock(view, effect, what) -> dict:
    c = clock_of(view)
    if not valid(c):
        reject(effect, f"world_clock이 초기화되지 않았거나 정본 범위 밖이라 {what}할 수 없습니다")
    return c


def _init(view, e):
    a = keys(e, ("minute_index",))
    m = a["minute_index"]
    if not is_int(m):
        reject(e, "minute_index는 정수여야 합니다")
    if m < MIN_MINUTE_INDEX:
        reject(e, f"minute_index가 달력 범위 밖입니다(≥ {MIN_MINUTE_INDEX}, 일련번호 ≥ 1)")
    if clock_of(view) != {}:
        reject(e, "world_clock이 이미 초기화돼 있습니다")
    return [write(e, ("minute_index",), SINGLE_VALUE, m), write(e, ("start_minute_index",), SINGLE_VALUE, m)]


def _advance_to(view, e):
    a = keys(e, ("target_minute",))
    c = ready_clock(view, e, "진행")
    t = a["target_minute"]
    if not is_int(t):
        reject(e, f"target은 정수여야 합니다: {t!r}")
    if t < c["minute_index"]:
        reject(e, f"시간은 후퇴하지 않습니다: {c['minute_index']} → {t}")
    return [write(e, ("minute_index",), SINGLE_VALUE, t)]


def _opt_str(v):
    return v is None or isinstance(v, str)


def _create(view, e):
    a = keys(e, ("schedule_id", "due_minute", "kind"), ("owner_ref", "payload_ref", "order_key") + EXT_KEYS)
    sid = ident(e, "schedule_id")
    if a["kind"] not in SCHEDULE_KINDS:
        reject(e, f"알 수 없는 예약 kind {a['kind']!r}")
    if not is_int(a["due_minute"]):
        reject(e, "due_minute는 정수여야 합니다")
    if "order_key" in a and not (isinstance(a["order_key"], str) and a["order_key"]):
        reject(e, "order_key는 비어 있지 않은 문자열이어야 합니다")
    for k in ("owner_ref", "payload_ref", "guard_ref", "owner_epoch"):
        if not _opt_str(a.get(k)):
            reject(e, f"{k}는 문자열 또는 null이어야 합니다")
    if a.get("payload") is not None and not isinstance(a["payload"], dict):
        reject(e, "payload는 객체 또는 null")
    if not is_int(a.get("conflict_priority", 0)):
        reject(e, "conflict_priority는 정수")
    c = ready_clock(view, e, "예약")
    if a["due_minute"] < c["minute_index"]:
        reject(e, "과거 시각 예약은 만들 수 없습니다")
    if view.has(WORLD, "", "schedule", (sid,)):
        reject(e, f"이미 있는 예약 {sid}")
    entry = {"schedule_id": sid, "due_minute": a["due_minute"], "kind": a["kind"],
             "owner_ref": a.get("owner_ref"), "payload_ref": a.get("payload_ref"),
             "created_by_version": view.version_id, "order_key": a.get("order_key", sid),
             "payload": a.get("payload"), "guard_ref": a.get("guard_ref"), "owner_epoch": a.get("owner_epoch"),
             "conflict_priority": a.get("conflict_priority", 0)}
    return [write(e, (sid,), SINGLE_VALUE, entry)]


def _cancel(view, e):
    keys(e, ("schedule_id",))
    sid = ident(e, "schedule_id")
    if not view.has(WORLD, "", "schedule", (sid,)):
        reject(e, f"없는 예약 {sid}")
    return [write(e, (sid,), DELETE, None)]


def _consume(view, e):
    keys(e, ("schedule_id",))
    sid = ident(e, "schedule_id")
    entry = view.read(WORLD, "", "schedule", (sid,))
    if entry is None:
        reject(e, f"없는 예약 {sid}")
    c = clock_of(view)
    if not initialized(c) or entry["due_minute"] > c["minute_index"]:
        reject(e, f"아직 도래하지 않은 예약 {sid}")
    return [write(e, (sid,), DELETE, None)]


def _drop_movement_wakeups(view, e):
    """그 행위자의 pending MOVEMENT_WAKEUP을 모두 지운다(배치 해제의 짝 — 낡은 깨우기 정리)."""
    keys(e, ("actor_id",))
    actor = ident(e, "actor_id")
    sched = view.read(WORLD, "", "schedule") or {}
    return [write(e, (sid,), DELETE, None) for sid, x in sorted(sched.items())
            if isinstance(x, dict) and x.get("kind") == "MOVEMENT_WAKEUP" and (x.get("payload") or {}).get("actor_id") == actor]


COMPONENTS = (
    Component("clock", WORLD, "rules.time", {"init": _init, "advance_to": _advance_to}),
    Component("schedule", WORLD, "rules.time", {"create": _create, "cancel": _cancel, "consume": _consume,
                                                  "drop_movement_wakeups": _drop_movement_wakeups}),
)
