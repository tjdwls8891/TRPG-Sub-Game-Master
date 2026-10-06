# -*- coding: utf-8 -*-
"""H03-A — 달력 · canonical WorldClock · pending world_schedule · base quiescence · 컴파일러 v2.

H03-A01  GREGORIAN 일련번호 왕복 · 윤년 · 존재하지 않는 날짜 거부
H03-A02  FIXED_CUSTOM 달력 왕복
H03-A03  시간대 [start,end) · 자정 넘김 · 대표 시각 결정성
H03-A04  시간 후퇴 거부 · 0분 진행 허용
H03-A05  예약 순서 = (due, order_key, id) — 삽입 순서 무관
H03-A06  CREATE / CANCEL / CONSUME — live 큐는 pending만(tombstone 없음)
H03-A07  base quiescence 판정표
H03-A08  DELETE 병합 의미 — 같은 계층 멱등, 혼용·없는 키·교차 계층은 충돌, 삽입 순서 무관
H03-A09  production 레지스트리 = 시간/예약 typed 연산(+H03-B 공간) — 임의 경로 연산 없음
H03-A10  PLAYER_TURN에서 typed 시간 연산 투영·커밋·되감기
H03-A11  due == now pending → READY 실패(WORLD_PROJECTION_CONFLICT, RETRY_PENDING 아님)
H03-A12  레거시 빈 시간 상태는 quiescent — 기존 턴 그대로
H03-A13  canonical_mutation도 quiescence를 증명한다
H03-A14  컴파일러 v2 — quantitative_time 달력 필수·검증 진단·결정적 revision
"""
from __future__ import annotations

import pytest

import core
from core import canonical_mutation as CM
from core import definition_compiler as dc
from core import turn_preparation as TP
from core import turn_transaction as tt
from core import world_clock as WC
from core import world_mutation as WM
from core import world_schedule as SC
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _disk, _fund, _phases
from tests.policy.test_h01_definition_compiler import _codes, _mini

pytestmark = pytest.mark.policy

GREG = {"calendar_id": "cal_main", "calendar_type": "GREGORIAN", "month_lengths": None,
        "dayparts": {"dawn": {"start": "05:00", "end": "09:00", "representative": "06:00"},
                     "day": {"start": "09:00", "end": "18:00", "representative": "12:00"},
                     "evening": {"start": "18:00", "end": "21:00", "representative": "19:00"},
                     "night": {"start": "21:00", "end": "05:00", "representative": "23:00"}}}


@pytest.fixture
def inject():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _sess(sid="s-h03a"):
    return core.TRPGSession(sid, 1, 2, "x", {})


def _batch(sess, bid="b1"):
    return WM.WorldMutationBatch(batch_id=bid, outer_origin=WS.CAUSE_ADMIN_EDIT,
                                 source_world_version_id=WS.version_id_of(sess.world_version),
                                 definition_revision=sess.definition_revision)


def _project(sess, *ops, registry=None):
    b = _batch(sess)
    for op in ops:
        b.add(op)
    b.freeze()
    return WM.project(sess, b, registry or WM.PRODUCTION_REGISTRY)


def _op(op_id, op_type, payload, **kw):
    return WM.make_op(op_id, op_type, "TEST", payload, **kw)


def _entry(sid, due, order_key=None, kind=SC.KIND_EVENT_WAKEUP):
    return {"schedule_id": sid, "due_minute": due, "kind": kind, "owner_ref": None,
            "payload_ref": None, "created_by_version": None, "order_key": order_key or sid}


# ══════════════════════════════════════════════════════════════
# 달력 · 시계 (순수)
# ══════════════════════════════════════════════════════════════

def test_gregorian_ordinal_roundtrip_and_leap_years():
    cal = WC.Calendar(GREG)
    assert cal.to_ordinal(1, 1, 1) == 1
    assert cal.days_in_month(2024, 2) == 29 and cal.days_in_month(2023, 2) == 28
    assert cal.days_in_month(1900, 2) == 28 and cal.days_in_month(2000, 2) == 29
    # 연속 일련번호 왕복(윤년 경계 포함)
    start = cal.to_ordinal(1999, 12, 25)
    for o in range(start, start + 800):
        assert cal.to_ordinal(*cal.from_ordinal(o)) == o
    assert cal.to_ordinal(2024, 3, 1) - cal.to_ordinal(2024, 2, 28) == 2
    # 파이썬 proleptic Gregorian과 일치(독립 기준)
    import datetime
    for y, m, d in ((1, 1, 1), (1600, 2, 29), (2031, 3, 1), (9999, 12, 31)):
        assert cal.to_ordinal(y, m, d) == datetime.date(y, m, d).toordinal()
    for bad in ((2023, 2, 29), (2024, 13, 1), (2024, 0, 1), (0, 1, 1), (2024, 4, 31)):
        with pytest.raises(WC.ClockError):
            cal.to_ordinal(*bad)
    idx = cal.to_minute_index(2031, 3, 1, 8, 30)
    dt = cal.from_minute_index(idx)
    assert dt.to_dict() == {"year": 2031, "month": 3, "day": 1, "hour": 8, "minute": 30}
    assert dt.iso() == "2031-03-01 08:30"
    with pytest.raises(WC.ClockError):
        cal.to_minute_index(2031, 3, 1, 24, 0)


def test_fixed_custom_calendar_roundtrip():
    cal = WC.Calendar({"calendar_id": "cal_x", "calendar_type": "FIXED_CUSTOM",
                       "month_lengths": [10, 20, 5], "dayparts": {}})
    assert cal.year_length == 35 and cal.months_in_year(3) == 3
    assert cal.to_ordinal(1, 1, 1) == 1 and cal.to_ordinal(1, 3, 5) == 35 and cal.to_ordinal(2, 1, 1) == 36
    for o in range(1, 200):
        assert cal.to_ordinal(*cal.from_ordinal(o)) == o
    with pytest.raises(WC.ClockError):
        cal.to_ordinal(1, 3, 6)
    with pytest.raises(WC.ClockError):
        WC.Calendar({"calendar_type": "FIXED_CUSTOM", "month_lengths": [10, 0]})
    with pytest.raises(WC.ClockError):
        WC.Calendar({"calendar_type": "LUNAR"})


def test_dayparts_half_open_wraparound_and_representative():
    cal = WC.Calendar(GREG)
    base = cal.to_minute_index(2031, 3, 1)
    at = lambda h, m=0: cal.daypart_of(base + h * 60 + m)
    assert at(5) == "dawn" and at(8, 59) == "dawn" and at(9) == "day"
    assert at(17, 59) == "day" and at(18) == "evening" and at(21) == "night"
    assert at(0) == "night" and at(4, 59) == "night"           # 자정 넘김
    assert cal.daypart_order == ("dawn", "day", "evening", "night")
    assert cal.minute_index_at_daypart(2031, 3, 1, "night") == base + 23 * 60
    assert cal.minute_index_at_daypart(2031, 3, 1, "dawn") == base + 6 * 60
    with pytest.raises(WC.ClockError):
        cal.representative_minute_of_day("noon")
    gap = WC.Calendar({"calendar_type": "GREGORIAN",
                       "dayparts": {"day": {"start": "09:00", "end": "18:00", "representative": "12:00"}}})
    assert gap.daypart_of(base + 60) is None
    for bad in ("24:00", "9:00", "09:60", None):
        with pytest.raises(WC.ClockError):
            WC.parse_hhmm(bad)


def test_clock_never_moves_backward_and_zero_minute_is_allowed():
    assert WC.advance_to(100, 100) == 100 and WC.advance_to(100, 160) == 160
    with pytest.raises(WC.ClockError):
        WC.advance_to(100, 99)
    with pytest.raises(WC.ClockError):
        WC.add_minutes(100, -1)
    assert WC.add_minutes(100, 0) == 100
    c = WC.initial_clock(500)
    assert c == {"minute_index": 500, "start_minute_index": 500}
    assert WC.is_initialized(c) and not WC.is_initialized({})
    assert WC.validate_clock({}) == [] and WC.validate_clock(c) == []
    assert WC.validate_clock({"minute_index": 1})
    assert WC.validate_clock({"minute_index": 1, "start_minute_index": 2})
    assert WC.validate_clock({"minute_index": True, "start_minute_index": 0})
    assert WC.elapsed_since_start({"minute_index": 530, "start_minute_index": 500}) == 30
    with pytest.raises(WC.ClockError):
        WC.current_minute({})


# ══════════════════════════════════════════════════════════════
# 예약 · quiescence (순수)
# ══════════════════════════════════════════════════════════════

def test_schedule_order_is_independent_of_insertion_order():
    es = [_entry("z", 100, "a"), _entry("a", 100, "b"), _entry("m", 50), _entry("b", 100, "a")]
    s1 = {e["schedule_id"]: e for e in es}
    s2 = {e["schedule_id"]: e for e in reversed(es)}
    want = ["m", "b", "z", "a"]
    assert [e["schedule_id"] for e in SC.ordered(s1)] == want == [e["schedule_id"] for e in SC.ordered(s2)]
    assert [e["schedule_id"] for e in SC.same_minute_batch(s1, 100)] == ["b", "z", "a"]
    assert [e["schedule_id"] for e in SC.due_at_or_before(s1, 99)] == ["m"]
    assert SC.next_due_after(s1, 50)["schedule_id"] == "b" and SC.next_due_after(s1, 100) is None
    assert SC.validate_schedule(s1) == []
    assert SC.validate_schedule({"x": _entry("y", 1)})
    assert SC.validate_schedule({"x": dict(_entry("x", 1), kind="NOPE")})
    assert SC.validate_schedule({"x": dict(_entry("x", 1), extra=1)})


def test_base_quiescence_truth_table():
    clk = WC.initial_clock(100)
    q = SC.base_quiescence({}, {})
    assert q.quiescent and not q.clock_initialized and q.current_minute is None
    q = SC.base_quiescence({}, {"e": _entry("e", 200)})
    assert not q.quiescent and "미초기화" in q.reasons[0]
    assert SC.base_quiescence(clk, {}).quiescent
    assert SC.base_quiescence(clk, {"e": _entry("e", 101)}).quiescent
    q = SC.base_quiescence(clk, {"e": _entry("e", 100), "f": _entry("f", 99), "g": _entry("g", 300)})
    assert not q.quiescent and q.overdue_ids == ("f", "e") and q.pending_count == 3
    assert not SC.base_quiescence({"minute_index": 5}, {}).quiescent        # 잘못된 시계
    d = q.to_dict()
    assert d["quiescent"] is False and d["overdue_ids"] == ["f", "e"]
    # 세션·투영 세계 모두에서 증명
    s = _sess()
    assert SC.base_quiescence_of(s).quiescent
    assert SC.base_quiescence_of(WM.ProjectedWorld.capture(s)).quiescent


# ══════════════════════════════════════════════════════════════
# typed 연산 · DELETE 병합
# ══════════════════════════════════════════════════════════════

def test_production_registry_holds_only_typed_ops():
    names = set(WM.PRODUCTION_REGISTRY.op_types())
    assert set(SC.TIME_OP_TYPES) <= names
    assert all(n.isupper() for n in names)
    assert not any("PATH" in n or "SET_ANY" in n or "GENERIC" in n for n in names)


def test_time_ops_initialize_advance_and_schedule_lifecycle():
    s = _sess()
    # 미초기화 시계에는 예약도 진행도 없다
    res = _project(s, _op("s", SC.OP_SCHEDULE_CREATE, {"schedule_id": "e1", "due_minute": 5,
                                                       "kind": SC.KIND_EVENT_WAKEUP}))
    assert res.conflicts[0].code == "HANDLER_REJECTED"
    assert _project(s, _op("a", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 5})).conflicts
    res = _project(s, _op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": 1000}))
    assert res.ok and res.world.domain("world_clock") == WC.initial_clock(1000)
    assert s.world_clock == {}                                    # 투영은 세션 불변
    s.world_clock = WC.initial_clock(1000)
    assert _project(s, _op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": 5})).conflicts   # 재초기화 금지
    # 후퇴 거부 · 0분 허용
    assert _project(s, _op("a", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 999})).conflicts
    assert _project(s, _op("a", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1000})).ok
    # 과거 예약 거부, 같은 분 예약은 생성 가능(단 READY에서 quiescence 실패)
    assert _project(s, _op("c", SC.OP_SCHEDULE_CREATE, {"schedule_id": "e0", "due_minute": 999,
                                                        "kind": SC.KIND_EVENT_WAKEUP})).conflicts
    res = _project(s, _op("c", SC.OP_SCHEDULE_CREATE, {"schedule_id": "e1", "due_minute": 1060,
                                                       "kind": SC.KIND_MOVEMENT_WAKEUP,
                                                       "owner_ref": "actor:pc"}))
    assert res.ok
    e = res.world.domain("world_schedule")["e1"]
    assert e == {"schedule_id": "e1", "due_minute": 1060, "kind": "MOVEMENT_WAKEUP",
                 "owner_ref": "actor:pc", "payload_ref": None, "created_by_version": None,
                 "order_key": "e1"}
    s.world_schedule = {"e1": e}
    assert _project(s, _op("c", SC.OP_SCHEDULE_CREATE, {"schedule_id": "e1", "due_minute": 1070,
                                                        "kind": SC.KIND_EVENT_WAKEUP})).conflicts
    assert _project(s, _op("c", SC.OP_SCHEDULE_CREATE, {"schedule_id": "e2", "due_minute": 1070,
                                                        "kind": "BOGUS"})).conflicts
    # 취소 → live 큐에서 삭제(tombstone 없음)
    res = _project(s, _op("x", SC.OP_SCHEDULE_CANCEL, {"schedule_id": "e1"}))
    assert res.ok and res.world.domain("world_schedule") == {}
    assert _project(s, _op("x", SC.OP_SCHEDULE_CANCEL, {"schedule_id": "nope"})).conflicts
    # 소비 — 도래 전 거부, 진행(부모) 후 자식이 소비
    assert _project(s, _op("u", SC.OP_SCHEDULE_CONSUME, {"schedule_id": "e1"})).conflicts
    res = _project(s, _op("adv", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1060}),
                   _op("use", SC.OP_SCHEDULE_CONSUME, {"schedule_id": "e1"},
                       causal_parent_op_ids=("adv",)))
    assert res.ok, res.conflicts
    assert res.world.domain("world_schedule") == {} and res.world.domain("world_clock")["minute_index"] == 1060
    assert SC.base_quiescence_of(res.world).quiescent
    # 부모 없이 진행만 하면 due==now 예약이 남아 quiescent가 아니다
    res = _project(s, _op("adv", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1060}))
    assert res.ok and not SC.base_quiescence_of(res.world).quiescent
    # payload 키 검증
    assert _project(s, _op("x", SC.OP_SCHEDULE_CANCEL, {"schedule_id": "e1", "zz": 1})).conflicts


def test_delete_semantic_conflicts_and_insertion_order():
    s = _sess()
    s.world_clock = WC.initial_clock(0)
    s.world_schedule = {"a": _entry("a", 10), "b": _entry("b", 20)}
    reg = WM.OperationRegistry("t")
    reg.register("DEL", lambda v, op, p: [WM.write(op, "world_schedule", (p["id"],), WM.MERGE_DELETE, None)])
    reg.register("PUT", lambda v, op, p: [WM.write(op, "world_schedule", (p["id"],),
                                                   WM.MERGE_SINGLE_VALUE, _entry(p["id"], 30))])
    # DELETE 값은 None이어야 한다
    with pytest.raises(WM.WorldMutationError):
        WM.write(_op("q", "DEL", {}), "world_schedule", ("a",), WM.MERGE_DELETE, 1)
    # 같은 계층 이중 삭제는 멱등
    r = _project(s, _op("d1", "DEL", {"id": "a"}), _op("d2", "DEL", {"id": "a"}), registry=reg)
    assert r.ok and set(r.world.domain("world_schedule")) == {"b"}
    # 삭제 + 다른 의미 혼용 → 충돌
    r = _project(s, _op("d1", "DEL", {"id": "a"}), _op("p1", "PUT", {"id": "a"}), registry=reg)
    assert r.conflicts[0].code == "MERGE_CONFLICT"
    # 없는 키 삭제 → 충돌
    r = _project(s, _op("d1", "DEL", {"id": "zz"}), registry=reg)
    assert r.conflicts[0].code == "MERGE_CONFLICT" and "없는 키" in r.conflicts[0].message
    # 인과 관계없는 교차 계층 삭제 → UNORDERED_INDEPENDENT_WRITES 또는 없는 키 충돌
    r = _project(s, _op("p", "PUT", {"id": "c"}), _op("d1", "DEL", {"id": "a"}),
                 _op("d2", "DEL", {"id": "a"}, causal_parent_op_ids=("p",)), registry=reg)
    assert not r.ok
    # 인과 순서가 있으면 생성 → 삭제 가능
    r = _project(s, _op("p", "PUT", {"id": "c"}),
                 _op("d", "DEL", {"id": "c"}, causal_parent_op_ids=("p",)), registry=reg)
    assert r.ok and set(r.world.domain("world_schedule")) == {"a", "b"}
    # 삽입 순서 무관(같은 결과 · 같은 digest)
    ops = [_op("d1", "DEL", {"id": "a"}), _op("p1", "PUT", {"id": "c"}), _op("d2", "DEL", {"id": "b"})]
    r1 = _project(s, *ops, registry=reg)
    r2 = _project(s, *reversed(ops), registry=reg)
    assert r1.ok and r2.ok and r1.world.digest() == r2.world.digest()
    assert set(r1.world.domain("world_schedule")) == {"c"}


# ══════════════════════════════════════════════════════════════
# PLAYER_TURN 통합 (rig)
# ══════════════════════════════════════════════════════════════

def _hook_ops(inject, *ops):
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        for op in ops:
            prep.world_mutation_batch.add(op)
    inject.setattr(TP, "capture_world_baseline", _hook)


async def test_legacy_empty_time_state_is_quiescent_in_ready(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx = await _run_turn(r)
    assert tx.status == tt.TurnStatus.COMMITTED
    assert s.world_clock == {} and s.world_schedule == {}
    bq = tx.preparation.ready_proof.base_quiescence
    assert bq == {"quiescent": True, "clock_initialized": False, "current_minute": None,
                  "pending_count": 0, "overdue_ids": [], "reasons": []}


async def test_typed_time_ops_commit_and_rewind_in_player_turn(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(1000)
    _hook_ops(inject,
              _op("adv", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1030}),
              _op("mk", SC.OP_SCHEDULE_CREATE, {"schedule_id": "w1", "due_minute": 1100,
                                                "kind": SC.KIND_EVENT_WAKEUP},
                  causal_parent_op_ids=("adv",)))
    tx1 = await _run_turn(r)
    inject.undo()
    assert tx1.status == tt.TurnStatus.COMMITTED
    assert s.world_clock == {"minute_index": 1030, "start_minute_index": 1000}
    assert s.world_schedule["w1"]["due_minute"] == 1100
    assert s.world_schedule["w1"]["created_by_version"] is None or isinstance(
        s.world_schedule["w1"]["created_by_version"], str)
    assert _disk(s)["world_clock"]["minute_index"] == 1030
    assert set(tx1.preparation.ready_proof.world_changed_domains) == {"world_clock", "world_schedule"}
    assert tx1.preparation.ready_proof.base_quiescence["quiescent"] is True
    # 두 번째 턴: 도래까지 진행 + 소비
    _hook_ops(inject,
              _op("adv", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1100}),
              _op("use", SC.OP_SCHEDULE_CONSUME, {"schedule_id": "w1"}, causal_parent_op_ids=("adv",)))
    tx2 = await _run_turn(r)
    inject.undo()
    assert tx2.status == tt.TurnStatus.COMMITTED
    assert s.world_clock["minute_index"] == 1100 and s.world_schedule == {}
    # 되감기 → 턴 1 상태(예약 복원, 시계 복원)
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    assert s.world_clock["minute_index"] == 1030 and set(s.world_schedule) == {"w1"}


async def test_due_now_pending_blocks_ready_as_projection_conflict(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(1000)
    s.world_schedule = {"w1": _entry("w1", 1050)}
    # 소비 없이 도래 시각까지 진행 → READY의 base quiescence 실패
    _hook_ops(inject, _op("adv", SC.OP_CLOCK_ADVANCE_TO, {"target_minute": 1050}))
    turn_before, wv_before = s.turn_count, s.world_version
    tx = await _run_turn(r)
    prep = tx.preparation
    assert prep.failure_code == "WORLD_PROJECTION_CONFLICT"
    assert prep.world_conflicts[0]["code"] == "NOT_QUIESCENT"
    assert prep.world_quiescence.overdue_ids == ("w1",)
    assert prep.phase != TP.PREP_RETRY_PENDING
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_code == "WORLD_PROJECTION_CONFLICT"
    assert not s.extraction_retry_ctx and "PREPARED" not in _phases(s, tx)
    assert s.turn_count == turn_before and s.world_version == wv_before
    assert s.world_clock["minute_index"] == 1000 and set(s.world_schedule) == {"w1"}


async def test_invalid_base_state_blocks_ready(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_schedule = {"w1": _entry("w1", 10)}               # 미초기화 시계 + 예약
    tx = await _run_turn(r)
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.preparation.world_conflicts[0]["code"] == "NOT_QUIESCENT"


async def test_canonical_mutation_requires_quiescence():
    bot = FakeBot()
    s = _sess("cm-h03a")
    coord = CM.CanonicalMutationCoordinator(bot)              # production 레지스트리
    b = CM.new_batch(s, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    b.add(_op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": 600}))
    b.add(_op("w", SC.OP_SCHEDULE_CREATE, {"schedule_id": "w1", "due_minute": 600,
                                           "kind": SC.KIND_EVENT_WAKEUP}, causal_parent_op_ids=("i",)))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    assert res.outcome == CM.OUTCOME_CONFLICT and res.reason.startswith("NOT_QUIESCENT")
    assert s.world_clock == {} and s.world_version is None
    b = CM.new_batch(s, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init2")
    b.add(_op("i", SC.OP_CLOCK_INITIALIZE, {"minute_index": 600}))
    b.add(_op("w", SC.OP_SCHEDULE_CREATE, {"schedule_id": "w1", "due_minute": 660,
                                           "kind": SC.KIND_EVENT_WAKEUP}, causal_parent_op_ids=("i",)))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init2")
    assert res.outcome == CM.OUTCOME_COMMITTED, res
    assert s.world_clock == WC.initial_clock(600) and set(s.world_schedule) == {"w1"}
    assert s.world_schedule["w1"]["created_by_version"] is None    # 생성 시점 source version(초기 = 없음)


# ══════════════════════════════════════════════════════════════
# 컴파일러 v2 — quantitative_time
# ══════════════════════════════════════════════════════════════

RAW_CAL = {"calendar_id": "cal_main", "type": "GREGORIAN",
           "dayparts": {"day": {"start": "06:00", "end": "18:00", "representative": "12:00"},
                        "night": {"start": "18:00", "end": "06:00", "representative": "22:00"}}}


def _qt(**extra):
    return _mini(scenario_schema={"version": 1, "capabilities": {"quantitative_time": 1}}, **extra)


def test_compiler_version_2_and_quantitative_time_is_validated():
    assert dc.COMPILER_VERSION == 2
    assert "quantitative_time" in dc.VALIDATED_CAPABILITIES
    assert "calendar" not in dc.RESERVED_UNCOMPILED_KEYS
    res = dc.compile_scenario(_qt(), "t")
    assert not res.ok and "CALENDAR_REQUIRED" in _codes(res, "ERROR")
    res = dc.compile_scenario(_qt(calendar=RAW_CAL), "t")
    assert res.ok, _codes(res)
    cal = res.definition["calendar"]
    assert cal == {"calendar_id": "cal_main", "calendar_type": "GREGORIAN", "month_lengths": None,
                   "dayparts": RAW_CAL["dayparts"], "daypart_order": ["day", "night"]}
    WC.Calendar(cal)
    # legacy(달력·capability 없음) — 달력 None, 오류 없음
    legacy = dc.compile_scenario(_mini(), "t")
    assert legacy.ok and legacy.definition["calendar"] is None
    # capability 없이 저작된 달력 → 경고
    res = dc.compile_scenario(_mini(calendar=RAW_CAL), "t")
    assert res.ok and "CALENDAR_WITHOUT_CAPABILITY" in _codes(res, "WARNING")


@pytest.mark.parametrize("patch, code", [
    ({"calendar_id": ""}, "CALENDAR_ID_INVALID"),
    ({"type": "LUNAR"}, "CALENDAR_TYPE_INVALID"),
    ({"type": "FIXED_CUSTOM"}, "CALENDAR_MONTH_LENGTHS_INVALID"),
    ({"type": "FIXED_CUSTOM", "month_lengths": [30, 0]}, "CALENDAR_MONTH_LENGTHS_INVALID"),
    ({"month_lengths": [30]}, "CALENDAR_MONTH_LENGTHS_UNEXPECTED"),
    ({"dayparts": {}}, "CALENDAR_DAYPARTS_REQUIRED"),
    ({"dayparts": {"d": {"start": "06:00", "end": "18:00"}}}, "DAYPART_INVALID"),
    ({"dayparts": {"d": {"start": "6:00", "end": "18:00", "representative": "12:00"}}}, "DAYPART_INVALID"),
    ({"dayparts": {"d": {"start": "06:00", "end": "06:00", "representative": "06:00"}}}, "DAYPART_EMPTY"),
    ({"dayparts": {"d": {"start": "06:00", "end": "18:00", "representative": "19:00"}}},
     "DAYPART_REPRESENTATIVE_OUTSIDE"),
    ({"dayparts": {"a": {"start": "06:00", "end": "18:00", "representative": "12:00"},
                   "b": {"start": "17:00", "end": "05:00", "representative": "22:00"}}}, "DAYPART_OVERLAP"),
])
def test_calendar_validation_errors(patch, code):
    res = dc.compile_scenario(_qt(calendar=dict(RAW_CAL, **patch)), "t")
    assert not res.ok and code in _codes(res, "ERROR")


def test_calendar_invalid_shape_and_unknown_key():
    assert "CALENDAR_INVALID" in _codes(dc.compile_scenario(_qt(calendar="2031"), "t"), "ERROR")
    res = dc.compile_scenario(_qt(calendar=dict(RAW_CAL, start="2031-03-01")), "t")
    assert res.ok and "CALENDAR_UNKNOWN_KEY" in _codes(res, "WARNING")


def test_calendar_revision_is_deterministic_and_sensitive():
    a = dc.compile_scenario(_qt(calendar=RAW_CAL), "t").revision
    assert dc.compile_scenario(_qt(calendar=RAW_CAL), "t").revision == a
    # 컴파일된 달력은 저작 순서와 무관하다(원문 순서는 H01 legacy_source_order_digest가 따로 묶는다)
    reordered = dict(RAW_CAL, dayparts=dict(reversed(list(RAW_CAL["dayparts"].items()))))
    assert (dc.compile_scenario(_qt(calendar=reordered), "t").definition["calendar"]
            == dc.compile_scenario(_qt(calendar=RAW_CAL), "t").definition["calendar"])
    moved = dict(RAW_CAL, dayparts={"day": {"start": "06:00", "end": "18:00", "representative": "13:00"},
                                    "night": RAW_CAL["dayparts"]["night"]})
    assert dc.compile_scenario(_qt(calendar=moved), "t").revision != a
