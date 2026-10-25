# -*- coding: utf-8 -*-
"""H06-A — 정의 바인딩 · genesis 초기화 · 생애주기 자격 · 재시작 정의 권위(H06-D 일부).

  · 단일 PC + 필수 capability + 저작 world_start → 스냅샷 1개 · revision 바인딩 · genesis 1회.
  · 재시작은 바인딩 스냅샷만 읽는다(원본 drift 무시) · 손상/부재는 BLOCKED(재컴파일 대체 없음).
  · 같은 초기화 정체성 재요청은 genesis를 다시 만들지 않는다.
  · 캐시 실패 → INITIALIZED · 인트로 없음 · 재시도는 genesis 재적용 없이 이어 간다.
  · 다인 세션·레거시 시나리오·world_start 미저작 → LEGACY.
  · 초기화 충돌 → 부분 초기화 없음(정본·바인딩·생애주기 불변).
"""
from __future__ import annotations

import copy
import json
import os

import pytest

import core
from core import canonical_mutation as CM
from core import definition_snapshot as SNAP
from core import player_world as PW
from core import spatial as SP
from core import world_lifecycle as WL
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_commit_recovery import _restart
from tests.policy.test_h04b_events_progression import eq, ev_raw
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

PC = f"pc_{PLAYER_UID}"
START = {"place": "항구", "date": "2031-11-03", "time": "21:00", "knowledge_seed": "outsider"}


def dyn_raw(templates=None, *, world_start=START, **extra):
    raw = ev_raw(templates or {}, **extra)
    raw["start_frames"] = [{"id": "harbor", "title": "항구의 밤", "facts": {"location": "{장소}"},
                            **({"world_start": copy.deepcopy(world_start)} if world_start else {})},
                           {"id": "plain", "title": "평범한 시작", "facts": {}}]
    return raw


def setup_session(r, raw=None, *, players=None):
    s = r.sess
    s.scenario_id = "t"
    s.scenario_data = raw or dyn_raw()
    s.cache_name = None
    s.cache_obj = None
    if players is not None:
        s.players = players
    return s


def _cog(r):
    import cogs.session as session_mod
    cog = session_mod.SessionCog(r.bot)
    r.bot.add_cog_stub("SessionCog", cog)
    return cog


async def play_intro(cog, s, chosen, ch):
    """v7.37 R4: 인트로 앞 인물 소개 박자를 끝까지 넘긴다(응답 버튼 — 마지막 박자가 인트로를 연다)."""
    from core import talk as T
    from tests.fakes.discord_fakes import FakeInteraction, FakeUser
    await cog.play_intro(s, chosen, ch)
    for _ in range(10):
        msg = next((m for m in reversed(getattr(ch, "sent", [])) if isinstance(m.view, T.TalkView)), None)
        if msg is None:
            return
        await msg.view.children[0].callback(FakeInteraction(FakeUser(int(PLAYER_UID)), ch, msg))


def journal_inits(s):
    return [e for e in CM.read_journal(s.session_id) if e.get("cause_kind") == WS.CAUSE_INITIALIZATION]


def world_of(s):
    return {f: copy.deepcopy(getattr(s, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}


# ── 컴파일러: 저작 world_start ──────────────────────────────────

def test_world_start_compiled_only_when_authored_and_validated():
    res = core.definition_compiler.compile_scenario(dyn_raw(), "t")
    assert res.ok
    frames = {f["frame_id"]: f for f in res.definition["initialization_definitions"]["start_frames"]}
    ws = frames["harbor"]["world_start"]
    assert ws["place"] == "항구" and ws["knowledge_seed"] == "outsider" and isinstance(ws["minute_index"], int)
    assert "world_start" not in frames["plain"]                       # 미저작 틀은 키 없음(revision 불변)
    bad = dyn_raw(world_start={"place": "없는곳", "date": "2031-11-03", "time": "21:00"})
    codes = [d.code for d in core.definition_compiler.compile_scenario(bad, "t").errors]
    assert "WORLD_START_PLACE_UNKNOWN" in codes
    bad2 = dyn_raw(world_start={"place": "항구", "date": "2031-13-40", "time": "25:00"})
    assert "WORLD_START_TIME_INVALID" in [d.code for d in core.definition_compiler.compile_scenario(bad2, "t").errors]


def test_existing_scenario_without_world_start_revision_unchanged_by_h06_key():
    raw = dyn_raw(world_start=None)
    r1 = core.definition_compiler.compile_scenario(raw, "t").revision
    raw2 = copy.deepcopy(raw)
    raw2["start_frames"][0]["world_start"] = copy.deepcopy(START)
    r2 = core.definition_compiler.compile_scenario(raw2, "t").revision
    assert r1 != r2                                                   # 저작하면 바뀌고
    assert core.definition_compiler.compile_scenario(copy.deepcopy(raw), "t").revision == r1   # 미저작은 안정


# ── 초기화 ────────────────────────────────────────────────────

async def test_single_pc_full_capability_creates_one_snapshot_and_one_genesis(rig):
    r = rig
    s = setup_session(r)
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_OK, res.reason
    snap = SNAP.read_snapshot(s.session_id)
    assert s.definition_revision == snap.definition_revision
    assert s.world_version["ordinal"] == 1 and s.world_version["cause_kind"] == WS.CAUSE_INITIALIZATION
    assert s.world_lifecycle == WL.LIFECYCLE_INITIALIZED
    assert s.world_initialization["genesis_version_id"] == s.world_version["version_id"]
    assert s.actor_registry[PC]["kind"] == "PC"
    assert SP.placement_of(s, PC) == SP.at_place("항구")
    assert s.world_clock["minute_index"] == snap.definition["initialization_definitions"]["start_frames"][0][
        "world_start"]["minute_index"]
    assert SP.knows_edge(SP.actor_knowledge(s, PC), "e_hm#fwd")        # 저작 seed 적용
    assert SP.actor_knowledge(s, PC)["places"]["항구"]["visited"] is True
    phases = [e["phase"] for e in journal_inits(s)] + [e["phase"] for e in CM.read_journal(s.session_id)
                                                      if e.get("phase") == "COMMITTED"]
    assert [e["phase"] for e in journal_inits(s)] == ["PREPARED"] and phases.count("COMMITTED") == 1
    # 열리기 전에는 DYNAMIC이 아니다
    assert PW.session_world_context(s).mode == PW.MODE_LEGACY
    assert WL.blocked_reason(s)
    assert WL.mark_open(s) and PW.session_world_context(s).dynamic
    # 저장본에도 바인딩·생애주기가 함께 있다(같은 strict 저장)
    data = json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))
    assert data["definition_revision"] == s.definition_revision and data["world_lifecycle"] == WL.LIFECYCLE_INITIALIZED


async def test_repeated_initialization_does_not_double_seed(rig):
    r = rig
    s = setup_session(r)
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    before = world_of(s)
    again = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert again.outcome == WL.INIT_ALREADY
    assert world_of(s) == before and len(journal_inits(s)) == 1
    other = await WL.initialize_world(r.bot, s, {"id": "plain"})       # 다른 선택이어도 이미 바인딩된 세션
    assert other.outcome == WL.INIT_ALREADY and world_of(s) == before


async def test_genesis_spawns_events_true_at_start_via_initial_full_scan(rig):
    tpl = {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "LOCKED"),
           "phases": {"calm": {"eligible_when": {"const": True}, "priority": 1}},
           "transition_cases": [{"case_id": "c", "from": None, "to": "calm", "steps": []}]}
    r = rig
    s = setup_session(r, dyn_raw({"lock": tpl}))
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert [i["template_id"] for i in s.event_instances.values()] == ["lock"]


async def test_multi_pc_session_remains_legacy(rig):
    r = rig
    s = setup_session(r, players={PLAYER_UID: {"name": "가"}, "777": {"name": "나"}})
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_LEGACY and res.reason == "not_single_pc"
    assert s.definition_revision is None and s.world_lifecycle is None
    assert not os.path.exists(SNAP.snapshot_path(s.session_id))
    assert PW.session_world_context(s).mode == PW.MODE_LEGACY


async def test_legacy_scenario_without_capability_and_unauthored_start_stay_legacy(rig):
    r = rig
    s = setup_session(r, {"places": {}, "default_npcs": {}})
    assert (await WL.initialize_world(r.bot, s, {"id": "x"})).outcome == WL.INIT_LEGACY
    s2 = setup_session(r, dyn_raw())
    res = await WL.initialize_world(r.bot, s2, {"id": "plain"})        # world_start 미저작 — facts 해석 안 함
    assert res.outcome == WL.INIT_LEGACY and res.reason == "world_start_unauthored"
    assert s2.definition_revision is None and s2.world_version is None


async def test_initialization_conflict_leaves_no_partial_world(rig):
    r = rig
    s = setup_session(r)
    s.actor_registry = {PC: {"kind": "PC", "display_name": "x", "source_kind": "PLAYER",
                             "source_key": PLAYER_UID, "aliases": []}}   # ACTOR_REGISTER 충돌
    before = world_of(s)
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_FAILED
    assert world_of(s) == before and s.definition_revision is None and s.world_lifecycle is None
    assert s.world_initialization["status"] == "FAILED" and WL.blocked_reason(s)   # R2: fail-closed 표식
    assert not [e for e in CM.read_journal(s.session_id) if e.get("phase") == "COMMITTED"]


async def test_initialization_quiescence_failure_leaves_no_partial_world(rig, monkeypatch):
    from core import world_schedule as SCH
    r = rig
    s = setup_session(r)
    monkeypatch.setattr(SCH, "base_quiescence_of",
                        lambda w: SCH.BaseQuiescenceReport(False, True, 0, 1, ("x",), ("due 남음(주입)",)))
    before = world_of(s)
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_FAILED and "NOT_QUIESCENT" in res.reason
    assert world_of(s) == before and s.definition_revision is None and s.world_lifecycle is None
    assert not [e for e in CM.read_journal(s.session_id) if e.get("phase") == "COMMITTED"]


# ── 캐시 실패 · 인트로 · OPEN ─────────────────────────────────

async def test_initial_cache_failure_after_genesis_stays_initialized_and_retry_does_not_reapply(rig, monkeypatch):
    r = rig
    s = setup_session(r)
    cog = _cog(r)
    calls = {"upload": 0, "intro": 0}

    async def _upload(session, **kw):
        calls["upload"] += 1
        if calls["upload"] == 1:
            return False                                              # 첫 캐시 생성 실패
        session.cache_name = "caches/ok"
        return True

    async def _intro(session, instruction, **kw):
        calls["intro"] += 1
        return {"ok": True, "ai_text": "인트로", "error": None, "finalized": True}
    monkeypatch.setattr(cog, "upload_cache", _upload)
    monkeypatch.setattr(r.bot.get_cog("GameCog"), "_execute_proceed", _intro)
    s.start_frame = {"id": "harbor"}
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))
    assert s.world_lifecycle == WL.LIFECYCLE_INITIALIZED and calls["intro"] == 0
    genesis = copy.deepcopy(s.world_version)
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))   # 재시도
    assert calls == {"upload": 2, "intro": 1}
    assert s.world_version == genesis and len(journal_inits(s)) == 1            # genesis 재적용 없음
    assert s.world_lifecycle == WL.LIFECYCLE_OPEN and PW.session_world_context(s).dynamic


async def test_legacy_play_intro_unchanged_and_deferred_cache_opened(rig, monkeypatch):
    r = rig
    s = setup_session(r, players={PLAYER_UID: {"name": "가"}, "777": {"name": "나"}})
    cog = _cog(r)
    seen = []

    result = {"ok": False}

    async def _upload(session, **kw):
        seen.append("upload")
        return result["ok"]

    async def _intro(session, instruction, **kw):
        seen.append("intro")
    monkeypatch.setattr(cog, "upload_cache", _upload)
    monkeypatch.setattr(r.bot.get_cog("GameCog"), "_execute_proceed", _intro)
    # v7.33(②, 사용자 승인): 레거시도 캐시 업로드가 실패하면 인트로로 넘어가지 않고 재시도 대기(옛 의미: 실패해도 계속)
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))
    assert seen == ["upload"] and s.world_lifecycle is None and s._intro_pending
    result["ok"] = True                                               # [세션 다시 열기] → 같은 경로로 재시도
    await play_intro(cog, s, {"id": "harbor"}, r.bot.get_channel(s.game_ch_id))
    assert seen == ["upload", "upload", "intro"] and s.world_lifecycle is None


async def test_join_rejected_after_world_bound(rig):
    r = rig
    s = setup_session(r)
    await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert WL.world_bound(s)
    s.players["888"] = {"name": "난입"}                               # 우회로 들어와도
    assert PW.session_world_context(s).mode == PW.MODE_BLOCKED        # LEGACY로 조용히 넘어가지 않는다


# ── 재시작(H06-D) ─────────────────────────────────────────────

async def _open(r, s):
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    WL.mark_open(s)
    await core.save_session_data(r.bot, s)


async def test_restart_with_source_drift_keeps_bound_snapshot(rig):
    r = rig
    s = setup_session(r)
    await _open(r, s)
    rev, seed = s.definition_revision, s.narrative_seed
    drift = copy.deepcopy(s.scenario_data)
    drift["movement_edges"][0]["distance_m"] = 999                    # 원본 파일 변경
    s.scenario_data = drift
    PW._DEF_CACHE.clear()
    _, s2 = await _restart(r)
    assert json.load(open(f"scenarios/{s.scenario_id}.json", encoding="utf-8"))["movement_edges"][0][
        "distance_m"] == 999                                          # 원본 파일은 바뀌었지만
    assert s2.scenario_data["movement_edges"][0]["distance_m"] != 999  # 세션은 바인딩 원문(스냅샷)으로 진행
    assert s2.scenario_data == SNAP.read_snapshot(s2.session_id).definition["legacy_source"]["scenario"]
    ctx = PW.session_world_context(s2)
    assert ctx.dynamic and ctx.definition_revision == rev             # 정의는 바인딩 스냅샷
    assert ctx.definition == SNAP.read_snapshot(s2.session_id).definition
    assert core.definition_compiler.compile_scenario(drift, "t").revision != rev
    assert s2.narrative_seed == seed
    assert s2.world_clock == s.world_clock and s2.world_schedule == s.world_schedule   # 오프라인 시간 없음


async def test_corrupt_or_missing_snapshot_blocks_instead_of_recompiling(rig):
    r = rig
    s = setup_session(r)
    await _open(r, s)
    PW._DEF_CACHE.clear()
    path = SNAP.snapshot_path(s.session_id)
    with open(path, "a", encoding="utf-8") as f:
        f.write("garbage")
    ctx = PW.session_world_context(s)
    assert ctx.mode == PW.MODE_BLOCKED and WL.blocked_reason(s)
    os.remove(path)
    PW._DEF_CACHE.clear()
    assert PW.session_world_context(s).mode == PW.MODE_BLOCKED       # scenario_data 재컴파일로 대체하지 않음


async def test_restart_recovers_unfinished_initialization_journal(rig):
    r = rig
    s = setup_session(r)
    await _open(r, s)
    mid = s.world_initialization["mutation_id"]
    # 영속 후 PERSISTED/COMMITTED 기록 전 크래시 모사: 종결 기록 제거
    path = CM.journal_path(s.session_id)
    rows = [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]
    with open(path, "w", encoding="utf-8") as f:
        for e in rows:
            if not (e.get("mutation_id") == mid and e["phase"] in ("PERSISTED", "COMMITTED")):
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    assert [a for _, a, _ in CM.classify_recovery(s)] == ["COMPLETE"]
    _, s2 = await _restart(r)
    assert CM.classify_recovery(s2) == []
    assert [e["phase"] for e in CM.read_journal(s2.session_id) if e.get("mutation_id") == mid][-1] == "COMMITTED"
    assert s2.world_version == s.world_version and s2.world_lifecycle == WL.LIFECYCLE_OPEN
