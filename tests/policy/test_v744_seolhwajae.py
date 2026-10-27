# -*- coding: utf-8 -*-
"""v7.44 — 추리 시나리오 '설화재'(저작표 1~3 승인) 통합 검사.

세션마다 진상 케이스 8개 중 하나가 시드로 고정되고, 그 케이스의 단서·오해 단서만 열리며,
체포는 장면의 용의자만 지목할 수 있고 결과(해결·석연찮은 해결·오인 체포)는 저작 결말 국면이 정한다.
"""
from __future__ import annotations

import json
import pathlib

import pytest

import core
from core import definition_compiler as dc
from core import events as EV
from core import player_world as PW
from core import turn_world as TW
from core import variant_check as VC
from tests.conftest import REPO_ROOT
from tests.policy.test_h04b_events_progression import K
from tests.policy.test_h05a_intent_planning import PC, make_session, manifest, plan

pytestmark = pytest.mark.policy

RAW = json.loads(pathlib.Path(REPO_ROOT, "scenarios", "설화재.json").read_text(encoding="utf-8"))
CASES = {f"{k}{n}": who for k, who in (("S", "윤서진"), ("D", "한도윤"), ("M", "마정호"), ("O", "오세린"))
         for n in range(1, 5)}                                              # v7.45.3: 진범 4 × 수법 4 = 16
AID = {n: d["actor_id"] for n, d in RAW["default_npcs"].items()}


def _definition():
    res = dc.compile_scenario(RAW, "설화재")
    assert res.ok, [(d.code, d.path) for d in res.errors]
    return res.definition


def _world(seed):
    s, D = make_session(RAW, at="2층 복도", places=tuple(RAW["places"]))
    r = core.world_progression.project_world_progression(
        D, s, K, progress={"observer_actor": PC, "scene_place": "2층 복도"}, outer_origin="INITIALIZATION",
        initial_full_scan=True, narrative_seed=seed)
    for f in ("event_instances", "world_schedule", "information_state", "cognition_state"):
        setattr(s, f, r.projected_world.domain(f))
    return s, D


def _truth(s):
    return next(i for i in s.event_instances.values() if i["template_id"] == "sh_truth")


def _seed_for(case):
    for n in range(400):
        s, D = _world(f"seed{n}")
        if _truth(s)["local_state"]["case"] == case:
            return s, D
    raise AssertionError(case)


def test_scenario_compiles_and_every_case_is_solvable():
    D = _definition()
    assert VC.check(RAW, D) == []
    cfg = RAW["variant_check"]["clues"]
    for case, who in CASES.items():
        finds = D["event_templates"][f"sh_case_{case}"]["phases"]["open"]["finds"]
        mine = [f["info"] for f in finds if f["info"] in cfg]
        assert all(cfg[i]["points_to"] == who for i in mine)
        assert len(mine) >= 3 and sum(cfg[i]["strength"] == "decisive" for i in mine) == 1, case


def test_each_session_fixes_one_case_and_all_sixteen_occur():
    seen = {}
    for n in range(160):
        s, D = _world(f"seed{n}")
        loc = _truth(s)["local_state"]
        assert CASES[loc["case"]] == loc["culprit"]
        seen.setdefault(loc["case"], n)
    assert set(seen) == set(CASES)
    again, _ = _world(f"seed{seen['M2']}")
    assert _truth(again)["local_state"]["case"] == "M2"                     # 같은 시드 = 같은 진상


def test_only_this_cases_clues_and_innocent_herrings_open():
    s, D = _seed_for("S1")
    live = {i["template_id"] for i in s.event_instances.values()}
    assert "sh_case_S1" in live and not ({f"sh_case_{c}" for c in CASES if c != "S1"} & live)
    assert "sh_herring_syj" not in live and {"sh_herring_hdy", "sh_herring_mjh", "sh_herring_osr"} <= live
    here = {f["info"] for _i, f in EV.active_finds(D, s, "별채")}
    assert "sh.s1.wrapper" in here                                          # 결정 단서는 별채에
    assert "sh.s1.wrapper" not in {f["info"] for _i, f in EV.active_finds(D, s, "201호")}


def _arrest(s, name):
    s.spatial_state["placements"][AID[name]] = {"kind": "AT_PLACE", "place_id": "2층 복도"}
    key = next(k for k, (_, oid), _ in manifest(s).resolutions if oid == f"arrest:{name}")
    p, _, _ = plan(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    assert p.status == PW.PLAN_PLANNED
    return [e["id"] for e in EV.new_endings(s and PW.session_world_context(s).definition, s, p.projected_world)]


@pytest.mark.parametrize("knows,who,ending", [
    (True, "윤서진", "solved_syj"), (False, "윤서진", "uneasy_syj"), (True, "한도윤", "wrong")])
def test_arrest_outcomes(knows, who, ending):
    s, D = _seed_for("S1")
    if knows:
        s.cognition_state = {core.cognition.actor_holder(PC): {"sh.s1.wrapper": {"awareness": "CONTENT"}}}
    assert _arrest(s, who) == [ending]


def test_arrest_needs_suspect_in_scene_and_notebook_groups_clues():
    s, D = _seed_for("D2")
    for n in AID.values():
        s.spatial_state["placements"][n] = {"kind": "AT_PLACE", "place_id": "별채"}
    assert manifest(s).resolutions == ()                                    # 장면에 용의자가 없다
    s.cognition_state = {core.cognition.actor_holder(PC): {"sh.d2.scratch": {"awareness": "CONTENT"},
                                                           "sh.c.alive_2210": {"awareness": "CONTENT"}}}
    from tests.conftest import PLAYER_UID
    note = TW.notebook_text(s, PLAYER_UID)
    assert "🔎 증거" in note and "손목" in note and "🗣️ 증언" in note


def test_display_button_reads_notebook_only_when_authored(fake_bot):
    from types import SimpleNamespace
    from core import display as DP

    def label(sd):
        v = DP.build_view(fake_bot, SimpleNamespace(scenario_data=sd, is_processing=False, cache_name=None))
        return next(c.label for c in v.children if getattr(c, "custom_id", "") == "disp:knowledge")
    assert label(RAW) == "📜 수첩" and label({}) == "📜 초기 지식"


def test_scenario_picker_shows_genre_line():
    from core import session_flow as SF
    sel = SF.ScenarioSelect(["설화재", "영도", "빈시나리오"])
    desc = {o.value: o.description for o in sel.options}
    assert desc == {"설화재": "고립 추리", "영도": "포스트 아포칼립스 생존", "빈시나리오": None}
