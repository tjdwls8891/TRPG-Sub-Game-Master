# -*- coding: utf-8 -*-
"""H02 보정 — coordinator 직렬화 · 묶음 실질 불변성 · 인과 계층 투영.

보정 1  CanonicalMutationCoordinator는 검증(stale·활성 턴)부터 저장까지 커밋 직렬화 락
        안에서 수행한다(락 순서: 직렬화 → io). 동시 정본 변이·PLAYER_TURN과 경합해도
        오래된 투영이 더 최신 정본을 덮어쓰지 못한다.
보정 2  WorldMutationBatch는 외부 연산·반환 연산·정체성 변경과 분리되고, 동결 digest와
        실제 내용이 갈라질 수 없다.
보정 3  causal_parent_op_ids가 투영 인과 순서다 — 계층 단위 평가, 같은 계층은 같은 기준,
        다음 계층은 이전 결과를 읽고, 독립 쓰기의 삽입 순서는 의미가 없다.
(보정 4 — failure code 전달 — 는 test_h02b_world_mutation의 충돌·가드 테스트에서 검증)
"""
from __future__ import annotations

import asyncio

import pytest

import core
from core import canonical_mutation as CM
from core import commit_coordinator as CC
from core import turn_transaction as tt
from core import world_mutation as WM
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _fund
from tests.policy.test_h02b_world_mutation import _registry

pytestmark = pytest.mark.policy


def _sess(sid):
    return core.TRPGSession(sid, 1, 2, "x", {})


def _reg_with_reader():
    reg = _registry()

    def read_clock(view, op, payload):
        seen = view.get("information_state", ("probe",))
        return [WM.write(op, "event_instances", ("seen", payload["slot"]), WM.MERGE_SINGLE_VALUE,
                         "none" if seen is None else seen)]
    reg.register("read_clock", read_clock)
    return reg


def _project(ops, base=None):
    s = base or _sess("p")
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT,
                              source_world_version_id=None, definition_revision=None)
    for op in ops:
        b.add(op)
    b.freeze()
    return WM.project(s, b, _reg_with_reader())


# ══════════════════════════════════════════════════════════════
# 보정 2 — 실질적 불변성
# ══════════════════════════════════════════════════════════════

def test_external_op_mutation_after_add_does_not_reach_batch():
    op = WM.make_op("o1", "set_clock", "T", {"minute": 10},
                    expected={"domain": "information_state", "path": ["probe"], "value": None})
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT,
                              source_world_version_id=None, definition_revision=None)
    b.add(op)
    op.payload["minute"] = 99                       # frozen dataclass지만 dict는 가변
    op.expected["value"] = 5
    got = b.operations[0]
    assert got.payload == {"minute": 10} and got.expected["value"] is None
    d = b.freeze()
    op.payload["minute"] = 77
    assert b.digest_matches() and b.digest == d
    res = WM.project(_sess("e"), b, _registry())
    assert res.ok and res.world.get("information_state", ("probe",)) == 10


def test_returned_operations_are_copies_and_identity_is_read_only():
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT,
                              source_world_version_id=None, definition_revision=None)
    b.add(WM.make_op("o1", "set_clock", "T", {"minute": 10}))
    b.operations[0].payload["minute"] = 99
    assert b.operations[0].payload == {"minute": 10}
    d = b.freeze()
    ops = b.operations
    ops[0].payload["minute"] = 55
    assert b.operations[0].payload == {"minute": 10} and b.digest_matches() and b.digest == d
    assert WM.project(_sess("r"), b, _registry()).world.get("information_state", ("probe",)) == 10
    # resolved_payload는 전달된 객체 내용이 아니라 내부 스냅샷을 쓴다
    assert b.resolved_payload(ops[0]) == {"minute": 10}
    for attr in ("batch_id", "outer_origin", "transaction_id", "source_world_version_id",
                 "definition_revision"):
        with pytest.raises(AttributeError):
            setattr(b, attr, "forged")
    d2 = b.to_dict()
    d2["operations"][0]["payload"]["minute"] = 1   # to_dict도 사본
    assert b.digest_matches()


def test_internal_tamper_is_detected_and_non_canonical_late_mutation_rejected():
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT,
                              source_world_version_id=None, definition_revision=None)
    b.add(WM.make_op("o1", "set_clock", "T", {"minute": 10}))
    b.freeze()
    b._ops[0]["payload"]["minute"] = 1              # 사설 상태 직접 변조(방어 확인)
    assert not b.digest_matches()
    with pytest.raises(WM.WorldMutationError, match="변조"):
        WM.project(_sess("t"), b, _registry())
    op = WM.make_op("o2", "set_clock", "T", {"minute": 1})
    op.payload["bad"] = {1, 2}                      # 생성 뒤 비정규 값 주입
    b2 = WM.WorldMutationBatch(batch_id="b2", outer_origin=WS.CAUSE_ADMIN_EDIT,
                               source_world_version_id=None, definition_revision=None)
    with pytest.raises(WM.WorldMutationError):
        b2.add(op)


# ══════════════════════════════════════════════════════════════
# 보정 3 — 인과 계층 투영
# ══════════════════════════════════════════════════════════════

def test_child_sees_parent_result():
    res = _project([WM.make_op("p", "set_clock", "T", {"minute": 10}),
                    WM.make_op("c", "read_clock", "T", {"slot": "c"}, causal_parent_op_ids=("p",))])
    assert res.ok and res.world.get("event_instances", ("seen", "c")) == 10


def test_independent_ops_read_the_same_base_regardless_of_insertion_order():
    a = WM.make_op("a", "set_clock", "T", {"minute": 10})
    r = WM.make_op("r", "read_clock", "T", {"slot": "r"})
    res1, res2 = _project([a, r]), _project([r, a])
    for res in (res1, res2):
        assert res.ok
        assert res.world.get("event_instances", ("seen", "r")) == "none"   # 형제의 쓰기를 못 본다
        assert res.world.get("information_state", ("probe",)) == 10
    assert res1.world.digest() == res2.world.digest()


def test_causal_same_key_write_is_ordered_not_conflict():
    res = _project([WM.make_op("p", "set_clock", "T", {"minute": 10}),
                    WM.make_op("c", "set_clock", "T", {"minute": 20}, causal_parent_op_ids=("p",))])
    assert res.ok and res.world.get("information_state", ("probe",)) == 20


def test_independent_same_key_conflicts_in_same_and_different_layers():
    same = _project([WM.make_op("a", "set_clock", "T", {"minute": 10}),
                     WM.make_op("b", "set_clock", "T", {"minute": 20})])
    assert not same.ok and same.conflicts[0].code == "MERGE_CONFLICT"
    # b는 x의 자식(계층 1)이지만 a와는 인과 관계가 없다 — 계층 순서가 결과를 정하면 안 된다
    cross = _project([WM.make_op("a", "set_clock", "T", {"minute": 10}),
                      WM.make_op("x", "log_event", "T", {"entry": {"k": 1}}),
                      WM.make_op("b", "set_clock", "T", {"minute": 20}, causal_parent_op_ids=("x",))])
    assert not cross.ok and cross.conflicts[0].code == "UNORDERED_INDEPENDENT_WRITES"
    assert set(cross.conflicts[0].op_ids) == {"a", "b"}
    # 순서와 무관한 결과(가산)는 계층이 달라도 허용
    add = _project([WM.make_op("a", "bump_rel", "T", {"a": "p", "b": "n", "delta": 2}),
                    WM.make_op("x", "log_event", "T", {"entry": {"k": 1}}),
                    WM.make_op("b", "bump_rel", "T", {"a": "p", "b": "n", "delta": 3},
                               causal_parent_op_ids=("x",))])
    assert add.ok and add.world.get("relationship_state", ("p", "n")) == 5
    # 독립 추가(append)는 계층 순서가 결과 순서가 되므로 다른 계층이면 충돌
    app = _project([WM.make_op("a", "log_event", "T", {"entry": {"k": "a"}}),
                    WM.make_op("x", "set_clock", "T", {"minute": 1}),
                    WM.make_op("b", "log_event", "T", {"entry": {"k": "b"}}, causal_parent_op_ids=("x",))])
    assert not app.ok and app.conflicts[0].code == "UNORDERED_INDEPENDENT_WRITES"


def test_same_layer_append_and_set_results_do_not_depend_on_insertion_order():
    ops = [WM.make_op("a", "log_event", "T", {"entry": {"k": "zeta"}}),
           WM.make_op("b", "log_event", "T", {"entry": {"k": "alpha"}}),
           WM.make_op("c", "tag_place", "T", {"add": ["y"]}),
           WM.make_op("d", "tag_place", "T", {"add": ["b"]})]
    r1, r2 = _project(ops), _project(list(reversed(ops)))
    assert r1.ok and r2.ok and r1.world.digest() == r2.world.digest()


# ══════════════════════════════════════════════════════════════
# 보정 1 — coordinator 직렬화
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _fresh_commit_locks():
    """CommitCoordinator의 세션별 직렬화 락은 모듈 수준 캐시다. 테스트마다 이벤트 루프가
    다르므로(운영은 단일 루프) 이전 테스트 루프에 묶인 락을 재사용하지 않게 비운다."""
    CC._COMMIT_LOCKS.clear()
    yield
    CC._COMMIT_LOCKS.clear()


def _gate_strict_save(monkeypatch):
    """첫 strict 저장에서 멈추는 게이트(락 보유 구간을 실제로 겹치게 한다)."""
    entered, release = asyncio.Event(), asyncio.Event()
    orig = core.io.write_session_strict_locked
    state = {"n": 0}

    async def _gated(session):
        state["n"] += 1
        if state["n"] == 1:
            entered.set()
            await release.wait()
        return await orig(session)
    monkeypatch.setattr(core.io, "write_session_strict_locked", _gated)
    return entered, release


async def _ticks(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def test_two_concurrent_canonical_mutations_cannot_overwrite_newer_state(monkeypatch):
    bot = FakeBot()
    s = _sess("cc-1")
    coord = CM.CanonicalMutationCoordinator(bot, registry=_registry())
    ba = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="A")
    ba.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 100}))
    bb = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="B")   # 같은 기준 version
    bb.add(WM.make_op("b", "set_clock", "ADMIN", {"minute": 200}))
    entered, release = _gate_strict_save(monkeypatch)
    ta = asyncio.create_task(coord.apply(s, ba, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="A"))
    await asyncio.wait_for(entered.wait(), 5)
    tb = asyncio.create_task(coord.apply(s, bb, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="B"))
    await _ticks()
    assert not tb.done(), "두 번째 변이는 첫 변이의 직렬화 구간이 끝날 때까지 검증도 하면 안 된다"
    assert not bb.frozen                         # 검증·동결 전 대기 중
    release.set()
    ra, rb = await asyncio.gather(ta, tb)
    assert ra.outcome == CM.OUTCOME_COMMITTED
    assert rb.outcome == CM.OUTCOME_REJECTED and "stale" in rb.reason
    assert s.information_state == {"probe": 100}
    assert s.world_version == ra.target_world_version


async def test_precheck_runs_under_commit_serialization_lock():
    bot = FakeBot()
    s = _sess("cc-2")
    coord = CM.CanonicalMutationCoordinator(bot, registry=_registry())
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="A")
    b.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 5}))
    lock = CC.commit_serialization_lock(s.session_id)
    await lock.acquire()                          # 다른 owner(PLAYER_TURN 커밋 등)가 구간 보유
    t = asyncio.create_task(coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="A"))
    await _ticks()
    assert not t.done() and not b.frozen
    # 보유 중 정본이 전진(커밋 모사) → 해제 후 검증이 최신 상태를 본다
    s.world_version = WS.next_world_version(s.session_id, None, cause_kind=WS.CAUSE_PLAYER_TURN,
                                            cause_id="tx-other")
    lock.release()
    res = await t
    assert res.outcome == CM.OUTCOME_REJECTED and "stale" in res.reason
    assert s.information_state == {}


async def test_canonical_mutation_racing_a_player_turn_commit(rig, monkeypatch):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    coord = CM.CanonicalMutationCoordinator(r.bot, registry=_registry())
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="race")   # 턴 이전 기준
    b.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 300}))
    entered, release = _gate_strict_save(monkeypatch)
    turn = asyncio.create_task(_run_turn(r))
    await asyncio.wait_for(entered.wait(), 5)     # PLAYER_TURN이 직렬화·io 락 안 strict 저장 지점
    admin = asyncio.create_task(coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="race"))
    await _ticks()
    assert not admin.done() and not b.frozen
    release.set()
    tx = await turn
    res = await admin
    assert tx.status == tt.TurnStatus.COMMITTED
    assert res.outcome == CM.OUTCOME_REJECTED
    assert s.information_state == {}                    # 오래된 투영이 커밋을 덮어쓰지 않았다
    assert s.world_version["cause_kind"] == "PLAYER_TURN" and s.world_version["cause_id"] == tx.transaction_id
    assert CM.read_journal(s.session_id) == []    # PREPARED조차 남기지 않음
