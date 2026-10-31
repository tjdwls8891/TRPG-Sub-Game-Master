# -*- coding: utf-8 -*-
"""v7.53 — 사건 물건 지급 GRANT_ITEM {key, quantity, actor|place}. 정본은 사건 기록, 커밋이 이번 턴 새 기록만 반영."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from core import definition_compiler as dc
from core import events as EV
from core import items as IT
from core import turn_world as TW
from core import world_mutation as WM
from core import world_progression as WPG
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import ev_raw
from tests.policy.test_h05a_intent_planning import PC, make_session

pytestmark = pytest.mark.policy


def _raw(steps):
    return ev_raw({"loot": {
        "version": 1, "name": "전리품", "spawn_when": {"const": True},
        "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}},
        "transition_cases": [{"case_id": "spawn", "from": None, "to": "open", "steps": steps}],
        "repeat_policy": "ONCE_PER_SESSION", "resolve_when": {"const": False}}})


STEPS = [{"type": "GRANT_ITEM", "step_id": "pc", "key": "마석", "quantity": 2, "actor": "$session_pc"},
         {"type": "GRANT_ITEM", "step_id": "npc", "key": "붕대", "quantity": 1, "actor": KANG},
         {"type": "GRANT_ITEM", "step_id": "floor", "key": "마석", "quantity": 3, "place": "항구"}]


def test_compile_accepts_targets_and_rejects_bad_steps():
    assert dc.compile_scenario(_raw(STEPS), "t").ok
    for bad in ({"type": "GRANT_ITEM", "step_id": "x", "key": "마석", "quantity": 1},
                {"type": "GRANT_ITEM", "step_id": "x", "key": "마석", "quantity": 1, "actor": "$session_pc", "place": "항구"},
                {"type": "GRANT_ITEM", "step_id": "x", "key": "마석", "quantity": 0, "actor": "$session_pc"},
                {"type": "GRANT_ITEM", "step_id": "x", "key": "마석", "quantity": 1, "place": "없는 곳"}):
        assert not dc.compile_scenario(_raw([bad]), "t").ok, bad


def test_spawn_grant_is_recorded_and_commit_applies_it_once():
    s, D = make_session(_raw(STEPS), at="항구")
    s.resources = {"주인공": {}, "강지수": {}}
    r = WPG.project_world_progression(D, WM.ProjectedWorld.capture(s), s.world_clock["minute_index"],
                                      progress={"observer_actor": PC}, outer_origin="INITIALIZATION",
                                      initial_full_scan=True, narrative_seed="s")
    prep = SimpleNamespace(world_projection=SimpleNamespace(world=r.projected_world))
    grants = TW.item_grant_changes(s, prep)
    assert sorted((g["key"], g["quantity"], g.get("actor") or g.get("place")) for g in grants) == \
        [("마석", 2, "$session_pc"), ("마석", 3, "항구"), ("붕대", 1, KANG)]
    rows = IT.apply_event_grants(s, grants, definition=D, pc_name="주인공")
    assert s.resources["주인공"]["마석"] == 2 and s.resources["강지수"]["붕대"] == 1
    floor = [v for v in s.place_items.values() if v["origin"] == "EVENT"]
    assert floor == [{"place": "항구", "key": "마석", "quantity": 3, "origin": "EVENT", "by": None, "tx": None}]
    assert len(rows) == 3
    s.event_instances = copy.deepcopy(r.projected_world.domain("event_instances"))
    assert TW.item_grant_changes(s, prep) == []                                   # 이미 반영된 기록은 다시 없다
