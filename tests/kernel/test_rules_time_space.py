# -*- coding: utf-8 -*-
"""이식 모듈 time·space — 옛 handler와 같은 거부·no-op 규칙(그림자 비교가 못 보는 거부 경로 위주)."""
from __future__ import annotations

import pytest

from indaim import kernel as K
from indaim.migrate import ops_v4

pytestmark = pytest.mark.policy
REG = ops_v4.registry()
T0 = 1440 * 400


def run(rows, base=None):
    b = K.EffectBatch(batch_id="b", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    for i, (kind, eid, comp, op, args, *par) in enumerate(rows):
        b.add(K.make_effect(f"e{i}", kind, eid, comp, op, args, cause="PLAN", by="t", parents=par[0] if par else ()))
    b.freeze()
    return K.project(base or K.World(), b, REG, None)


def clock_world():
    return run([(K.WORLD, "", "clock", "init", {"minute_index": T0})]).world


def test_clock_rules():
    w = clock_world()
    assert run([(K.WORLD, "", "clock", "init", {"minute_index": T0})], w).conflicts[0].code == "HANDLER_REJECTED"
    assert not run([(K.WORLD, "", "clock", "advance_to", {"target_minute": T0 - 1})], w).ok      # 후퇴 거부
    assert run([(K.WORLD, "", "clock", "advance_to", {"target_minute": T0})], w).ok               # 0분 진행 허용
    assert not run([(K.WORLD, "", "clock", "init", {"minute_index": 10})]).ok                     # 달력 범위 밖


def test_schedule_rules():
    w = clock_world()
    mk = {"schedule_id": "s1", "due_minute": T0 + 5, "kind": "MOVEMENT_WAKEUP", "payload": {"actor_id": "a1"}}
    w2 = run([(K.WORLD, "", "schedule", "create", mk)], w).world
    assert not run([(K.WORLD, "", "schedule", "create", dict(mk, due_minute=T0 - 1, schedule_id="s2"))], w).ok
    assert not run([(K.WORLD, "", "schedule", "consume", {"schedule_id": "s1"})], w2).ok         # 도래 전
    assert not run([(K.WORLD, "", "schedule", "cancel", {"schedule_id": "nope"})], w2).ok
    gone = run([(K.WORLD, "", "schedule", "drop_movement_wakeups", {"actor_id": "a1"})], w2).world
    assert K.View(gone, REG, None).read(K.WORLD, "", "schedule") == {}


def test_placement_clear_hold_and_noop():
    w = run([(K.WORLD, "", "clock", "init", {"minute_index": T0}), ("actor", "a1", "pos", "place", {"place_id": "p"})]).world
    p = run([("actor", "a1", "pos", "clear", {}), ("actor", "a1", "hold", "hold_current", {})], w)
    v = K.View(p.world, REG, None)
    assert v.read("actor", "a1", "pos") == {} and v.read("actor", "a1", "hold") == \
        {"placement": {"kind": "AT_PLACE", "place_id": "p"}, "at_minute": T0}
    noop = run([("actor", "a9", "pos", "clear", {}), ("actor", "a9", "hold", "hold_current", {})], w)
    assert noop.ok and not noop.changed


def test_transit_and_knowledge_rules():
    w = clock_world()
    tr = {"movement_id": "m", "edge_id": "e", "from_place": "a", "to_place": "b",
          "edge_started_at_minute": T0, "edge_expected_end_minute": T0 + 3}
    assert run([("actor", "a1", "pos", "transit", tr)], w).ok
    assert not run([("actor", "a1", "pos", "transit", dict(tr, edge_started_at_minute=T0 + 1))], w).ok
    assert not run([("actor", "a1", "pos", "transit", dict(tr, to_place="a"))], w).ok
    k = run([("actor", "a1", "map", "edge_know", {"edge_id": "e", "fields": {"acquisition_sources": ["x"]}}),
             ("actor", "a1", "map", "edge_know", {"edge_id": "e", "fields": {"acquisition_sources": ["x", "y"]}}, ("e0",))])
    rec = K.View(k.world, REG, None).read("actor", "a1", "map")
    assert rec["edges"]["e"]["acquisition_sources"] == ["x", "y"] and rec["revision"] == 2
    assert not run([("actor", "a1", "map", "access_know", {"ref": "r", "fields": {"observed_result": "MAYBE"}})]).ok
