# -*- coding: utf-8 -*-
"""H07-E/F — cutover·호환: 영도 DYNAMIC 후보·나머지 LEGACY, 선택 키 미저작 정의(과거 스냅샷 형태)의 유효성,
스냅샷 재시작이 원본 drift를 읽지 않음, revision 결정성, 재서술의 결단 재현.
"""
from __future__ import annotations

import copy
import json
import os

import pytest

import core
from core import definition_compiler as dc
from core import definition_snapshot as SNAP
from core import npc_position as NP
from core import place_display as PD
from core import player_world as PW
from core import world_lifecycle as WL
from tests.policy.test_h07_yeongdo_draft import ROOT, _raw, _session
from tests.policy.world_bind import bind_open

pytestmark = pytest.mark.policy




def _strip_h07_optional(raw):
    """H07 선택 키(일과·표시명·결단·reveals)를 걷어 낸 원본 = 그 키가 없던 시절 정의의 형태."""
    raw = copy.deepcopy(raw)
    for prof in raw["default_npcs"].values():
        prof.pop("routine", None)
    for node in raw["places"].values():
        node.pop("unknown_name", None)
        node.pop("display_name", None)
    for body in raw.get("information_definitions", {}).values():
        body.pop("reveals", None)
    for t in raw.get("event_templates", {}).values():
        for ph in t["phases"].values():
            ph.pop("resolution_options", None)
    return raw


def test_cutover_targets_yeongdo_dynamic_and_others_legacy():
    D = dc.compile_scenario(_raw(), "영도").definition
    assert set(WL.REQUIRED_CAPABILITIES) <= set(D["scenario_meta"]["validated_capabilities"])
    assert all(f.get("world_start") for f in D["initialization_definitions"]["start_frames"])
    for sid in ("설화재", "청람호", "균열"):                        # v8: 출하 시나리오는 모두 동적 세계 시작점이 있다
        raw = json.load(open(os.path.join(ROOT, "scenarios", f"{sid}.json"), encoding="utf-8"))
        d = dc.compile_scenario(raw, sid).definition
        assert all(f.get("world_start") for f in d["initialization_definitions"]["start_frames"]), sid


def test_definition_without_h07_optional_keys_stays_valid_at_runtime():
    raw = _strip_h07_optional(_raw())
    res = dc.compile_scenario(raw, "영도")
    assert res.ok, [(d.code, d.path) for d in res.errors[:5]]
    D = res.definition
    assert not any(a.get("routine") for a in D["actor_definitions"].values())
    s = _session()
    ch, why = WL.resolve_start_choices(D, {"id": "arrive_plain"})
    plan = WL.build_initial_world_plan(D, s, ch, initialization_id="init-old", narrative_seed="seed")
    assert plan.status == "READY"
    w = plan.projected_world
    assert all(NP.effective_position(D, w, a)["source"] == NP.UNTRACKED for a in D["actor_definitions"]
               if a != ch.npc)                                          # v7.40: 시작 장면 인물만 정본 배치
    assert PD.display_name(D, {"places": {"남항 중공업 연대": {"knows_name": False}}}, "남항 중공업 연대") \
        == "남항 중공업 연대"                                            # unknown_name 없음 = 공개 지명


def test_compile_and_genesis_are_deterministic():
    a, b = dc.compile_scenario(_raw(), "영도"), dc.compile_scenario(_raw(), "영도")
    assert a.revision == b.revision
    D = a.definition
    ch, _ = WL.resolve_start_choices(D, {"id": "arrive_person"})
    w1 = WL.build_initial_world_plan(D, _session(), ch, initialization_id="i", narrative_seed="s").projected_world
    w2 = WL.build_initial_world_plan(D, _session(), ch, initialization_id="i", narrative_seed="s").projected_world
    assert w1.digest() == w2.digest()


def test_restart_reads_snapshot_not_drifted_source():
    res = dc.compile_scenario(_raw(), "영도")
    s = core.TRPGSession("h07-compat-snap", 1, 2, "영도", _raw())
    s.players = {"42": {"name": "테스터"}}
    bind_open(s, res)
    snap = SNAP.read_snapshot(s.session_id, expected_revision=res.revision)
    drifted = copy.deepcopy(_raw())
    drifted["default_npcs"]["이선희"]["routine"] = {"default_place": "장부방"}       # 원본 파일이 바뀌어도
    s.scenario_data = drifted
    D = PW.load_definition(s, res.revision)
    assert D["actor_definitions"]["yd.npc.lee_seonhui"]["routine"]["default_place"] == "중리 해변"
    assert snap.definition_revision == res.revision


def test_rerender_semantic_record_replays_resolution_and_route():
    from core import action_intent as AI
    i = AI.ResolvedActionIntent(resolution=("evt_x", "run"), route_signature=("a#fwd",), definition_revision="r")
    j = AI.ResolvedActionIntent.from_semantic(json.loads(json.dumps(i.semantic_record())), definition_revision="r")
    assert j.resolution == ("evt_x", "run") and j.route_signature == ("a#fwd",)
