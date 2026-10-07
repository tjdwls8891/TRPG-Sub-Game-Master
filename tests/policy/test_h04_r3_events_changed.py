# -*- coding: utf-8 -*-
"""H04 R3 micro-patch — events_changed는 최종 투영에 실제 채택된 사건 변화만 담는다.

  · 우선순위로 탈락한 사건 단위의 전이는 events_changed에 없다(소비·진단 의미는 유지)
  · 연산이 거부된 사건 단위의 변화만 빠지고 UNIT_REJECTED 진단은 남는다
  · 공유 픽스처 SIMPLE은 다른 테스트가 고쳐 써도 오염되지 않는다(실행 순서 독립)
"""
from __future__ import annotations

import copy

import pytest

from core import world_progression as WP
from tests.policy import test_h04b_events_progression as H04B
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import DOOR, K, _set, defn, eq, run, world

pytestmark = pytest.mark.policy

BASE = {"version": 1, "spawn_when": eq(DOOR, "BARRED"),
        "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}


def _tpl(*steps):
    return dict(copy.deepcopy(BASE), transition_cases=[{"case_id": "c", "from": None, "to": "open",
                                                         "steps": list(steps)}])


def _changed_templates(r):
    insts = {i["instance_id"]: i["template_id"]
             for i in (r.projected_world.domain("event_instances") or {}).values()} if r.projected_world else {}
    return insts, [e["instance_id"] for e in r.events_changed]


def test_r3_lower_priority_event_dropped_is_not_in_events_changed():
    hi = _tpl({"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN",
               "conflict_priority": 5})
    lo = _tpl({"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "BARRED",
               "conflict_priority": 1})
    D = defn({"hi": hi, "lo": lo})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED, r.diagnostics
    assert r.projected_world.domain("access_control_state")["door_2"] == {"state": "OPEN"}
    insts, changed = _changed_templates(r)
    assert sorted(insts.values()) == ["hi"]                                   # lo는 실제로 생성되지 않았다
    assert changed and all(c in insts for c in changed)                       # 채택된 변화만
    assert [e["change"] for e in r.events_changed] == ["SPAWN"]
    assert any(d["code"] == "SUPERSEDED_BY_PRIORITY" and d["unit_id"].startswith("spawn:lo:")
               for d in r.diagnostics)                                        # 진단 의미 유지


def test_r3_rejected_event_change_removed_but_diagnostic_kept():
    ok = _tpl({"step_id": "s", "type": "SET_ACCESS_STATE", "control": "door_2", "state": "OPEN"})
    # KANG에게 S40 인지 기록이 없으므로 믿음 설정 연산은 handler가 거부한다
    bad = _tpl({"step_id": "s", "type": "SET_COGNITION_BELIEF", "holder": f"actor:{KANG}", "info": "S40",
                "belief": "BELIEVED"})
    D = defn({"ok": ok, "bad": bad})
    r = run(D, world(), K, base_ops=[_set("b", "door_1", "BARRED")])
    assert r.status == WP.COMPLETED, r.diagnostics
    insts, changed = _changed_templates(r)
    assert sorted(insts.values()) == ["ok"]
    assert sorted(insts[c] for c in changed) == ["ok"]                        # bad 변화만 빠짐
    assert any(d["code"] == "UNIT_REJECTED" and d["unit_id"].startswith("spawn:bad:") for d in r.diagnostics)


def test_r3_shared_simple_fixture_not_mutated_by_h04b_tests():
    snap = copy.deepcopy(H04B.SIMPLE)
    H04B.test_r44_r45_stale_epoch_and_guard_consume_noop()                     # 중첩 phases를 고치던 테스트
    assert H04B.SIMPLE == snap
