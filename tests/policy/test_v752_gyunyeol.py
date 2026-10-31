# -*- coding: utf-8 -*-
"""v7.52 — 게이트 헌터물 시나리오 균열(저작표 1 · 파티 기준). 실제 계획 경로(plan_player_turn)로 게이트 회차·공략·승급을 돌린다.

· 게이트 = 반복 사건(봉인 → 회차 → 공략): 입구에 들어서면 몬스터 되살림·배치(케이스 A/B), 보스가 쓰러지면 공략 + 성장 지급
· 등급 통제(D급 게이트는 등급 2부터) · 길드 가입 = 사건 합류(JOIN_PARTY) · 1장 승급 심사 = 장 결말 + 등급 2
"""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

import core
from core import action_intent as AI
from core import definition_compiler as dc
from core import events as EV
from core import npc_position as NP
from core import party as PA
from core import player_world as PW
from core import spatial as SP
from core import world_clock as WC
from core import world_mutation as WM
from core import world_progression as WPG
from core import world_state as WS
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.policy.test_h05a_intent_planning import PC, knowledge, make_session
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

RAW = json.loads(pathlib.Path(REPO_ROOT, "scenarios", "균열.json").read_text(encoding="utf-8"))
AID = {n: d["actor_id"] for n, d in RAW["default_npcs"].items()}
LOOK = {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 1, "activity": "OTHER"}


def _apply(s, pw):
    for f in WS.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(s, f, copy.deepcopy(pw.domain(f)))


def _city(at="협회 로비", rank=1, seed="seed0"):
    s, D = make_session(RAW, at=at, places=tuple(RAW["places"]))
    t0 = WC.Calendar(D["calendar"]).to_minute_index(2031, 3, 3, 9, 30)
    s.world_clock = {"minute_index": t0, "start_minute_index": t0}
    s.narrative_seed = seed
    s.spatial_knowledge = {PC: knowledge(tuple(D["movement_edges"]), tuple(RAW["places"]))}
    s.players[PLAYER_UID]["profile"] = {"근력": 12, "민첩": 10, "감각": 10, "정신": 10, "등급": rank}
    for n, a in AID.items():
        s.actor_registry[a] = {"kind": "NPC", "display_name": n, "source_kind": "AUTHORED_NPC", "source_key": n,
                               "aliases": []}
    w1 = WM.ProjectedWorld.capture(s)
    wake = WPG.initial_template_wakeups(D, w1, narrative_seed=seed, op_prefix="init.wk")
    r = WPG.project_world_progression(D, w1, t0, base_ops=wake, progress={"observer_actor": PC, "scene_place": at},
                                      outer_origin="INITIALIZATION", initial_full_scan=True, narrative_seed=seed)
    _apply(s, r.projected_world)
    return s, D


def _turn(s, raw=LOOK, combat_deaths=()):
    c = PW.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = AI.resolve_intent(m, raw, world=s)
    p = PW.plan_player_turn(c, s, intent, transaction_id=f"tx{s.world_clock['minute_index']}",
                            narrative_seed=s.narrative_seed, capabilities=PW.pc_capabilities(s, PLAYER_UID),
                            statuses={}, combat_deaths=combat_deaths,
                            stats=PW.pc_stats(s, c.uid_of.get(c.lead_pc)))
    assert p.status == PW.PLAN_PLANNED, (p.status, p.reason)
    endings = [e for e in EV.new_endings(c.definition, s, p.projected_world)]
    _apply(s, p.projected_world)
    return p, endings


def _go(s, place):
    """실제 이동(계획 경로) — 장면이 바뀌어야 게이트 국면이 다시 판정된다."""
    c = PW.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    key = next(k for k, pid, _ in m.places if pid == place)
    p, endings = _turn(s, {"movement_kind": "DESTINATION", "destination": key, "time_kind": "NONE"})
    assert SP.placement_of(s, PC) == SP.at_place(place), SP.placement_of(s, PC)
    return p, endings


def _walk(s, *places):
    for pl in places:
        _go(s, pl)


BACK = ("환기 갱도", "무너진 터널", "공사장 입구", "둔산 거리")


def _phase(s, tid):
    return next((i["current_phase"] for i in s.event_instances.values() if i["template_id"] == tid), None)


def _points(s):
    return sum(EV.growth_ledger(s.event_instances)[0].values())


def _resolve(s, option):
    c = PW.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    keys = [k for k, (_iid, oid), _ in m.resolutions if oid == option]
    return keys[0] if keys else None


def test_scenario_compiles_with_party_and_gates():
    res = dc.compile_scenario(RAW, "균열")
    assert res.ok, [(d.code, d.path) for d in res.errors]
    D = res.definition
    caps = set(D["scenario_meta"]["validated_capabilities"])
    assert {"party", "combat_wounds", "growth", "mortality"} <= caps and "pursuit" not in caps
    assert set(D["party"]["roles"]) == {"탱커", "근접 딜러", "마법사", "힐러", "서포터"}
    assert sorted(PA.role_of(D, AID[n])[0] for n in ("한유진", "서아름", "도재경", "백소은", "노태민")) == \
        sorted(["탱커", "힐러", "근접 딜러", "마법사", "서포터"])
    assert sum(PA.is_hostile(D, a) for a in D["actor_definitions"]) == 11
    assert {"gy_gate_e", "gy_gate_d", "gy_gate_c"} <= set(D["event_templates"])
    assert core.growth.check_spec(RAW) == []
    assert json.loads(pathlib.Path(REPO_ROOT, "data", "scenario_catalog.json").read_text(encoding="utf-8"))["균열"]


def test_gate_round_places_monsters_clear_grants_and_next_round_revives():
    s, D = _city()
    boss = AID["고블린 우두머리"]
    assert NP.effective_place(D, s, boss) is None                                # 회차 전 = 세계에 없음
    _go(s, "공사장 입구")
    assert _phase(s, "gy_gate_e") == "run" and NP.effective_place(D, s, boss) == "굴착기 둥지"
    assert {NP.effective_place(D, s, AID[n]) for n in ("고블린 척후", "고블린 전사", "고블린 투척병")} == \
        {"무너진 터널", "자재 창고", "환기 갱도"}
    for room in ("무너진 터널", "환기 갱도", "굴착기 둥지"):                     # 갈림길은 경로를 묻는다 — 한 칸씩
        _go(s, room)
    _turn(s, combat_deaths=[(boss, "사망", 3)])                                   # 우두머리 쓰러짐(PC 처치 3)
    assert NP.effective_place(D, s, boss) is None
    _turn(s)
    assert _phase(s, "gy_gate_e") == "cleared" and _points(s) == 3 + 2           # 처치 3 + 공략 2
    _walk(s, *BACK)
    assert _phase(s, "gy_gate_e") == "sealed"
    _go(s, "공사장 입구")                                                          # 새 회차 — 우두머리 되살아남
    assert _phase(s, "gy_gate_e") == "run" and NP.effective_place(D, s, boss) == "굴착기 둥지"
    for room in ("무너진 터널", "환기 갱도", "굴착기 둥지"):                     # 갈림길은 경로를 묻는다 — 한 칸씩
        _go(s, room)
    _turn(s, combat_deaths=[(boss, "사망", 3)])
    _turn(s)
    assert _phase(s, "gy_gate_e") == "cleared" and _points(s) == 10              # 회차마다 지급이 쌓인다(v7.52)


def test_higher_gate_needs_rank():
    s, D = _city(rank=1)
    _go(s, "폐교 정문")
    assert _phase(s, "gy_gate_d") == "sealed" and NP.effective_place(D, s, AID["강당의 망령"]) is None
    s, D = _city(rank=2)
    _go(s, "폐교 정문")
    assert _phase(s, "gy_gate_d") == "run" and NP.effective_place(D, s, AID["강당의 망령"]) == "폐교 강당"
    s, D = _city(rank=2)
    _go(s, "강변 입구")
    assert _phase(s, "gy_gate_c") == "sealed"                                     # 폐교 공략 기록이 먼저


def test_guild_join_brings_tank_and_healer_into_the_party():
    s, D = _city(at="은하수 길드 사무실")
    key = _resolve(s, "join")
    assert key
    _turn(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    marks = EV.party_marks(s.event_instances)
    assert marks.get(AID["한유진"]) is True and marks.get(AID["서아름"]) is True
    assert NP.effective_place(D, s, AID["한유진"]) == "은하수 길드 사무실"


def test_free_agent_joins_only_after_meeting():
    s, D = _city()
    ctx = PW.session_world_context(s)
    from core import turn_world as TW
    kept, _j, _l, diags = TW.party_outcome(ctx, s, WM.ProjectedWorld.capture(s), ["도재경"])
    assert kept == [] and diags[0]["reason"] == "JOIN_CONDITION"                 # 처음 보는 사이(관계 known↑에서 합류)


def test_chapter_one_exam_after_first_clear():
    s, D = _city()
    _go(s, "등급 심사장")
    assert _resolve(s, "exam") is None                                            # 공략 기록 없음
    _go(s, "공사장 입구")
    boss = AID["고블린 우두머리"]
    for room in ("무너진 터널", "환기 갱도", "굴착기 둥지"):                     # 갈림길은 경로를 묻는다 — 한 칸씩
        _go(s, room)
    _turn(s, combat_deaths=[(boss, "사망", 3)])
    _turn(s)
    _walk(s, *BACK)
    _go(s, "등급 심사장")
    key = _resolve(s, "exam")
    assert key
    _p, endings = _turn(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    _p, endings2 = _turn(s)
    got = endings + endings2
    assert [e["id"] for e in got] == ["ch1"] and got[0].get("chapter") is True
    assert EV.growth_ledger(s.event_instances)[1]["등급"][2] == 2


async def test_genesis_sets_rank_and_shop_stock(rig):
    from core import world_lifecycle as WL
    from tests.policy.test_h06a_lifecycle import setup_session
    s = setup_session(rig, copy.deepcopy(RAW))
    res = await WL.initialize_world(rig.bot, s, {"id": "first_day"})
    assert res.outcome == WL.INIT_OK, res.reason
    assert EV.growth_ledger(s.event_instances)[1]["등급"][2] == 1
    assert _phase(s, "gy_gate_e") == "sealed" and _phase(s, "gy_guild") == "open"
