# -*- coding: utf-8 -*-
"""H06-D — 재시작: 오프라인 시간 없음 · 캐시 복구가 동적 세계를 되돌리지 않음.

(정의 스냅샷 권위·저널 복구·손상 스냅샷 BLOCKED는 test_h06a_lifecycle에 있다.)
"""
from __future__ import annotations

import copy
import time

import pytest

import core
from core import cache_lifecycle as CLC
from core import world_lifecycle as WL
from core import world_state as WS
from tests.policy.test_cache_lifecycle import cbot, clock, fresh  # noqa: F401
from tests.policy.test_commit_recovery import _restart
from tests.policy.test_h06a_lifecycle import setup_session
from tests.policy.test_h06c_cache_source import _bind, seen  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


async def test_restart_does_not_advance_clock_or_consume_schedules(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    WL.mark_open(s)
    now = s.world_clock["minute_index"]
    s.world_schedule = {"later": {"schedule_id": "later", "due_minute": now + 30, "kind": "EVENT_WAKEUP",
                                  "owner_ref": "template:none", "payload_ref": "x",
                                  "created_by_version": None, "order_key": "later"}}
    await core.save_session_data(r.bot, s)
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 7 * 24 * 3600)   # 일주일 뒤 재시작
    _, s2 = await _restart(r)
    assert s2.world_clock == s.world_clock and s2.world_schedule == s.world_schedule
    assert s2.world_version == s.world_version


async def test_cache_recovery_recreate_does_not_reset_dynamic_world(fresh, cbot, clock, seen):
    s = fresh
    _bind(s)
    s.world_version = {"version_id": "wv_9", "parent_version_id": None, "ordinal": 9,
                       "cause_kind": "PLAYER_TURN", "cause_id": "t9"}
    s.cognition_state = {"actor:npc_x": {"S40": {"awareness": "CONTENT"}}}
    await CLC.open_window(cbot, s)
    before = {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}

    def _gone(**kw):
        from google.genai.errors import APIError
        raise APIError(404, {"error": {"message": "gone"}})
    cbot.genai_client.caches.get = _gone
    res = await CLC.restore(cbot, s)
    assert res["action"] == "RECREATED"
    assert {f: getattr(s, f) for f in WS.GAMEPLAY_WORLD_FIELDS} == before
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN
