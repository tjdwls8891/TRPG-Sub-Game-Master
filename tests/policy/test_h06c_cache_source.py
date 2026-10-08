# -*- coding: utf-8 -*-
"""H06-C — FrozenCacheBuildSource · 캐시 세대 메타 · revision 변경 시 비활성 · 원본 재로드 없음.

  · 룰북은 짧은 임계 구역에서 캡처한 불변 원천으로 조립된다(생성 중 세션 변화가 섞이지 않음).
  · V20에서 캡처한 캐시는 V21 커밋 뒤 완료돼도 base_world_version=V20으로 남는다.
  · 생성 중 정의 revision이 바뀌면 새 세대를 활성화하지 않고 단일 finalizer로 정리한다.
  · 바인딩 세션의 재발급은 스냅샷 원문을 쓴다(현재 원본·재전개 없음) · world_version 불변 · 세계 불변.
  · provider 생성 실패는 세계를 바꾸지 않는다(기존 정확히-한-번 정산 규칙 그대로).
"""
from __future__ import annotations

import copy
import types

import pytest

import core
from core import cache_lifecycle as CLC
from core import cost_ledger as CL
from core import world_lifecycle as WL
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_cache_lifecycle import _events, _seed, cbot, clock, fresh  # noqa: F401
from tests.policy.test_h06a_lifecycle import dyn_raw

pytestmark = pytest.mark.policy

V20 = {"version_id": "wv_20", "parent_version_id": "wv_19", "ordinal": 20, "cause_kind": "PLAYER_TURN",
       "cause_id": "t20"}
V21 = {"version_id": "wv_21", "parent_version_id": "wv_20", "ordinal": 21, "cause_kind": "PLAYER_TURN",
       "cause_id": "t21"}


@pytest.fixture
def seen(monkeypatch):
    rec = []

    async def _text(bot, model_id, scenario_data, cache_note="", session_id=None, session=None):
        rec.append({"scenario": copy.deepcopy(scenario_data), "session": session,
                    "world_version": copy.deepcopy(getattr(session, "world_version", None)),
                    "npcs": copy.deepcopy(getattr(session, "npcs", None))})
        return "룰북" * 10, 26_268, "룰북"
    monkeypatch.setattr(core.cache, "build_scenario_cache_text", _text)
    return rec


def _created(sid):
    return [e for e in CLC.load_events(sid) if e["type"] == "CREATED"]


def _on_create(cbot, monkeypatch, fn):
    orig = cbot.genai_client.caches.create

    def _create(**kw):
        fn()
        return orig(**kw)
    monkeypatch.setattr(cbot.genai_client.caches, "create", _create)


def _bind(s):
    """바인딩된 동적 세계 세션(스냅샷 + OPEN)."""
    from tests.policy.world_bind import bind_open
    s.scenario_id = "t"
    s.scenario_data = dyn_raw()
    bind_open(s, core.definition_compiler.compile_scenario(s.scenario_data, "t"))


async def test_frozen_source_isolated_from_live_changes_during_provider_create(fresh, cbot, clock, seen, monkeypatch):
    s = fresh
    s.world_version = copy.deepcopy(V20)
    s.compressed_memory = "기억A"
    s.npcs = {"세션인물": {"details": "원래"}}

    def _advance():                                                   # 업로드 도중 플레이어 커밋 V21
        s.world_version = copy.deepcopy(V21)
        s.npcs["세션인물"]["details"] = "바뀜"
        s.compressed_memory = "기억A\n기억B"
    _on_create(cbot, monkeypatch, _advance)
    await CLC.open_window(cbot, s)
    assert seen[0]["session"] is not s and seen[0]["world_version"] == V20      # 고정 사본으로 조립
    (ev,) = _created(s.session_id)
    assert ev["generation"]["base_world_version_id"] == "wv_20"       # V21 이후 완료돼도 V20 기준
    assert s.world_version == V21                                     # 캐시가 정본을 되돌리지 않음
    assert s.cached_session_npcs["세션인물"]["details"] == "원래"        # 기준선 = 캡처 시점
    assert s.cached_compressed_memory == "기억A" and s.compressed_memory == "기억B"   # 새 기억은 런타임에 남음


async def test_definition_revision_change_during_build_is_not_activated(fresh, cbot, clock, seen, monkeypatch):
    s = fresh
    _bind(s)
    await CLC.open_window(cbot, s)
    first = s.cache_name

    def _migrate():
        s.definition_revision = "sha256:" + "e" * 64                   # 생성 중 정의 migration
    _on_create(cbot, monkeypatch, _migrate)
    with pytest.raises(CLC.CacheGenerationStale):
        await CLC.reissue(cbot, s, purpose=CLC.PURPOSE_REISSUE_MANUAL)
    assert s.cache_name is None                                       # 새 세대 미활성
    created = _created(s.session_id)
    assert len(created) == 2 and created[1]["cache_name"] != first
    fin = [e for e in CLC.load_events(s.session_id) if e["type"] == "FINALIZED"
           and e["cache_name"] == created[1]["cache_name"]]
    assert len(fin) == 1 and fin[0]["reason"] == "DEFINITION_CHANGED"
    assert created[1]["cache_name"] in cbot.genai_client.caches.deleted
    creates = _events(cbot, CL.OP_CACHE_CREATE)
    assert len(creates) == 2                                          # 생성 사실은 각각 정확히 한 번


async def test_bound_reissue_uses_snapshot_source_and_preserves_world(fresh, cbot, clock, seen):
    s = fresh
    _bind(s)
    s.world_version = copy.deepcopy(V20)
    s.cognition_state = {"actor:npc_x": {"S40": {"awareness": "CONTENT"}}}
    s.npcs = dict(s.npcs, 세션인물={"details": "세션 중 생성"})
    seed = s.narrative_seed
    await CLC.open_window(cbot, s)
    bound = copy.deepcopy(s.scenario_data)
    s.scenario_data = dict(s.scenario_data, worldview="원본 파일이 바뀌었다")   # 원본 drift
    world_before = {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}
    npcs_before = copy.deepcopy(s.npcs)
    await CLC.reissue(cbot, s, purpose=CLC.PURPOSE_REISSUE_MANUAL)
    assert seen[-1]["scenario"] == bound and seen[-1]["scenario"].get("worldview") != "원본 파일이 바뀌었다"
    assert {f: getattr(s, f) for f in WS.GAMEPLAY_WORLD_FIELDS} == world_before     # world_version 포함 불변
    assert s.npcs == npcs_before and s.narrative_seed == seed
    assert _created(s.session_id)[-1]["generation"]["definition_revision"] == s.definition_revision


async def test_manual_reissue_command_does_not_reload_source_for_bound_session(fresh, cbot, clock, seen, monkeypatch):
    import cogs.system as system_mod
    s = fresh
    _bind(s)
    s.master_ch_id = 424242
    cbot.active_sessions[424242] = s
    await CLC.open_window(cbot, s)
    calls = []
    monkeypatch.setattr(core, "load_scenario_from_file", lambda *a, **k: calls.append("load") or {})
    monkeypatch.setattr(s, "expand_default_npcs", lambda **k: calls.append("expand"))
    sent = []

    async def _send(*a, **k):
        sent.append(a[0] if a else k)
    ctx = types.SimpleNamespace(channel=types.SimpleNamespace(id=424242), send=_send)
    cog = system_mod.SystemCog(cbot)
    await system_mod.SystemCog.manage_cache.callback(cog, ctx, "재발급")
    assert calls == []
    assert any("바인딩된 정의" in str(x) for x in sent)


async def test_failed_provider_create_leaves_world_unchanged(fresh, cbot, clock, seen):
    s = fresh
    _bind(s)
    s.world_version = copy.deepcopy(V20)
    await CLC.open_window(cbot, s)
    world_before = {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}
    cbot.genai_client.caches.fail_create = True
    with pytest.raises(Exception):
        await CLC.reissue(cbot, s, purpose=CLC.PURPOSE_REISSUE_MANUAL)
    assert {f: getattr(s, f) for f in WS.GAMEPLAY_WORLD_FIELDS} == world_before
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN
    settles = [e for e in CLC.load_events(s.session_id) if e["type"] == "WINDOW_SETTLED"]
    assert len(settles) == 1                                          # 기존 정산 규칙 — 정확히 한 번
