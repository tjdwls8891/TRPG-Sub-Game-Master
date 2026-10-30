# -*- coding: utf-8 -*-
"""v7.49 — 생존물 시나리오 청람호(저작표 1·2, 결정 2 나 · 3 나 · 4 나).

· 진상 케이스 3종(현실 범죄) · 추적자 '비옷의 남자'(인물 행동 유형) · 생존자 사망(사건 SET_ACTOR_STATUS 사망 → 세계에서 빠짐)
· 앞 희생자가 살아남으면 다음 희생자로(3 나) · 탈출 결단 뒤 마지막 추격 — 빈사면 결말 보류(4 나)
· 실제 계획 경로(plan_player_turn — 추적자 판단·세계 진행)로 밤을 돌린다.
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
from core import player_world as PW
from core import pursuit as PU
from core import world_clock as WC
from core import world_mutation as WM
from core import world_progression as WPG
from core import world_state as WS
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.policy.test_h05a_intent_planning import PC, make_session
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

RAW = json.loads(pathlib.Path(REPO_ROOT, "scenarios", "청람호.json").read_text(encoding="utf-8"))
AID = {n: d["actor_id"] for n, d in RAW["default_npcs"].items()}
NAME = {v: k for k, v in AID.items()}
KILLER = "chr.npc.killer"
WAIT10 = {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 10, "activity": "WAIT"}
LOOK = {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 1, "activity": "OTHER"}


def _apply(s, pw):
    for f in WS.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(s, f, copy.deepcopy(pw.domain(f)))


def _night(seed, at="전망대", hhmm=(21, 40), items=None):
    """genesis와 같은 순서(시계 → 시작 wakeup → 전수 검사)로 시작 세계를 만든다."""
    s, D = make_session(RAW, at=at, places=tuple(RAW["places"]), items=items)
    t0 = WC.Calendar(D["calendar"]).to_minute_index(2027, 8, 13, *hhmm)
    s.world_clock = {"minute_index": t0, "start_minute_index": t0}
    s.narrative_seed = seed
    for n, a in AID.items():
        s.actor_registry[a] = {"kind": "NPC", "display_name": n, "source_kind": "AUTHORED_NPC", "source_key": n,
                               "aliases": []}
    w1 = WM.ProjectedWorld.capture(s)
    wake = WPG.initial_template_wakeups(D, w1, narrative_seed=seed, op_prefix="init.wk")
    r = WPG.project_world_progression(D, w1, t0, base_ops=wake, progress={"observer_actor": PC, "scene_place": at},
                                      outer_origin="INITIALIZATION", initial_full_scan=True, narrative_seed=seed)
    _apply(s, r.projected_world)
    return s, D


def _turn(s, raw, statuses=None, hidden=False, keep=True):
    c = PW.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = AI.resolve_intent(m, raw, world=s)
    p = PW.plan_player_turn(c, s, intent, transaction_id=f"tx{s.world_clock['minute_index']}",
                            narrative_seed=s.narrative_seed, capabilities=PW.pc_capabilities(s, PLAYER_UID),
                            statuses=statuses or {}, pursuit={"hidden": hidden, "disabled": []})
    assert p.status == PW.PLAN_PLANNED, (p.status, p.reason)
    endings = [e["id"] for e in EV.new_endings(c.definition, s, p.projected_world)]
    if keep:
        _apply(s, p.projected_world)
    return p, endings


def _phase(s, tid):
    return next((i["current_phase"] for i in s.event_instances.values() if i["template_id"] == tid), None)


def _case(s):
    return next(i for i in s.event_instances.values() if i["template_id"] == "chr_truth")["local_state"]["case"]


def _seed_for(case):
    for n in range(60):
        s, D = _night(f"seed{n}")
        if _case(s) == case:
            return f"seed{n}"
    raise AssertionError(case)


def test_scenario_compiles_with_declared_survival_features():
    res = dc.compile_scenario(RAW, "청람호")
    assert res.ok, [(d.code, d.path) for d in res.errors]
    D = res.definition
    assert {"mortality", "stealth", "pursuit"} <= set(D["scenario_meta"]["validated_capabilities"])
    assert list(PU.configs(D)) == [KILLER] and D["actor_removing_statuses"] == ["사망"]
    for c in ("a", "b", "c"):                                     # 케이스마다 정체 단서 3개 이상 + 격퇴 경로
        finds = D["event_templates"][f"chr_case_{c}"]["phases"]["open"]["finds"]
        assert len(finds) >= 4 and any(f.get("object") for f in finds)
        assert D["event_templates"][f"chr_repel_{c}"]["phases"]["ready"]["resolution_options"]
    assert {"빈사", "사망", "숨음"} <= set(core.get_merged_status_effects(RAW))
    assert json.loads(pathlib.Path(REPO_ROOT, "data", "scenario_catalog.json").read_text(encoding="utf-8"))["청람호"]


def test_each_session_fixes_one_case_and_all_three_occur():
    seen = {}
    for n in range(30):
        s, _ = _night(f"seed{n}")
        seen.setdefault(_case(s), n)
    assert set(seen) == {"A", "B", "C"}
    again, _ = _night(f"seed{seen['B']}")
    assert _case(again) == "B"                                     # 같은 시드 = 같은 진상


@pytest.mark.parametrize("case,order", [("A", ["박태식", "문정애", "한경수"]), ("B", ["정우빈", "한경수", "이서윤"]),
                                        ("C", ["문정애", "최다현", "박태식"])])
def test_night_runs_kills_in_order_and_dawn_ends(case, order):
    s, D = _night(_seed_for(case))
    died, endings = [], []
    for _ in range(60):
        _p, endings = _turn(s, WAIT10)
        for a, marks in EV.actor_status_marks(s.event_instances).items():
            if "사망" in marks and NAME[a] not in died:
                died.append(NAME[a])
        if endings:
            break
    assert died == order                                           # 저작 순서대로, PC가 없는 곳에서
    assert endings == ["dawn"] and _phase(s, "chr_blackout") == "dark"
    victim = AID[order[0]]
    assert NP.effective_position(D, s, victim)["placement"] is None          # 죽은 인물은 장면·조건에서 빠진다
    corpse = {f["info"] for _i, f in EV.active_finds(D, s, "주방" if case == "C" else
                                                     {"A": "정비고", "B": "교사 숙소"}[case])}
    assert corpse or case == "C"


def test_victim_kept_with_pc_survives_and_next_victim_follows():
    """결정 3 나: 앞 희생자가 살아남으면(PC 곁) 시간이 지나 다음 희생자로 넘어간다."""
    s, D = _night(_seed_for("A"))
    first = AID["박태식"]
    for _ in range(60):
        s.spatial_state["placements"][first] = copy.deepcopy(s.spatial_state["placements"][PC])   # 동행(PC 곁)
        _p, endings = _turn(s, WAIT10)
        if endings:
            break
    assert _phase(s, "chr_kill_a1") == "missed" and "사망" not in EV.actor_status_marks(s.event_instances).get(first, {})
    assert _phase(s, "chr_kill_a2") == "killed"


def _resolve(s, option):
    c = PW.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    keys = [k for k, (_iid, oid), _ in m.resolutions if oid == option]
    return keys[0] if keys else None


@pytest.mark.parametrize("dying", [False, True])
def test_van_escape_needs_parts_and_a_last_moment_unharmed(dying):
    """결정 4 나: 결단 뒤 한 턴(마지막 추격)을 넘겨야 결말 — 빈사면 떠나지 못한다."""
    seed = _seed_for("A")
    s, D = _night(seed, at="정문·주차장")
    assert _resolve(s, "drive") is None                            # 부품 없음 = 선택지 없음
    s, D = _night(seed, at="정문·주차장", items={"승합차 열쇠": 1, "자동차 배터리": 1, "휘발유통": 1})
    _turn(s, LOOK)
    key = _resolve(s, "drive")
    assert key
    _p, endings = _turn(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    assert endings == [] and _phase(s, "chr_escape_van") == "going"
    _p, endings = _turn(s, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 2, "activity": "OTHER"},
                        statuses={"빈사": 1} if dying else {})
    assert endings == ([] if dying else ["escape_van"])


def test_repel_option_needs_proof_and_the_killer_in_scene():
    seed = _seed_for("A")
    s, D = _night(seed, at="원장실", hhmm=(22, 30), items={"당직 일지 원본": 1})
    _turn(s, LOOK)
    assert _resolve(s, "repel") is None                            # 그가 장면에 없다
    s.spatial_state["placements"][KILLER] = copy.deepcopy(s.spatial_state["placements"][PC])
    _turn(s, LOOK)
    key = _resolve(s, "repel")
    assert key
    _turn(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    _p, endings = _turn(s, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 1, "activity": "OTHER"})
    assert endings == ["repel_a"]


async def test_genesis_places_parts_and_starts_the_killer(rig):
    from core import world_lifecycle as WL
    from tests.policy.test_h06a_lifecycle import setup_session
    r = rig
    s = setup_session(r, copy.deepcopy(RAW))
    res = await WL.initialize_world(r.bot, s, {"id": "last_night"})
    assert res.outcome == WL.INIT_OK, res.reason
    stock = {(v["place"], v["key"]) for v in s.place_items.values()}
    assert {("정비고", "자동차 배터리"), ("원장실", "승합차 열쇠"), ("무전실", "무전기"), ("보트 창고", "보트 열쇠")} <= stock
    assert PU.instance_of(s, KILLER) and _phase(s, "chr_blackout") == "lit"
    assert NP.effective_place(PW.session_world_context(s).definition, s, KILLER) == "옛 숙소동"
