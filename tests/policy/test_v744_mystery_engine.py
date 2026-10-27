# -*- coding: utf-8 -*-
"""v7.44 — 목적 시나리오(추리) 대비 엔진 범용 개선(사용자 결정: 무대 나·진상 가·엔딩 가·마감 나·체포 가).

  E2 결단 대상(누구를) · E3 결말 국면(맺음 지침 → 기존 종료 경로) · E4 저작 단서의 정본화(finds 구조화)
  · E5 수첩(knowledge_notebook) · E6 진상 변형별 풀이 가능성 검사(variant_check).
모두 선택 저작 키 — 쓰지 않는 시나리오의 정의는 그대로다.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import core
from core import definition_compiler as dc
from core import events as EV
from core import player_world as PW
from core import turn_world as TW
from core import variant_check as VC
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeChannel
from tests.policy.test_h04a_actor_info import KANG, PARK
from tests.policy.test_h04b_events_progression import K, ev_raw, run
from tests.policy.test_h05a_intent_planning import PC, ctx_of, make_session, manifest, plan

pytestmark = pytest.mark.policy

ROOM = "성당"
SELF_RES = {"ref": "EVENT_LOCAL", "instance": "$self", "key": "resolution"}
CLUE = {"proposition": "제단 밑 쓰레기통에 피 묻은 장갑이 버려져 있다", "summary": "제단 밑의 피 묻은 장갑",
        "subject_refs": ["place:성당"], "tags": ["증거"]}


def _mystery(culprit=KANG, *, present=True):
    arrest = {"option_id": "arrest", "label": "체포",
              "target": {"type": "ACTOR", "from": ["강지수", "박노인"], "present": present}}
    right = {"cmp": "EQ", "ref": SELF_RES, "value": f"arrest:{culprit}"}
    return {"version": 1, "name": "성당 사건",
            "spawn_when": {"const": True},
            "phases": {
                "investigate": {"eligible_when": {"const": True}, "priority": 1, "guide": "GM지침-수사",
                                "retain_when": {"not": {"exists": SELF_RES}},
                                "resolution_options": [arrest],
                                "finds": [{"info": "C1", "object": "피 묻은 장갑", "place": ROOM}]},
                "solved": {"eligible_when": right, "priority": 2, "terminal": True,
                           "ending": {"id": "solved", "title": "해결", "epilogue": "진범이 자백한다"}},
                "wrong": {"eligible_when": {"all": [{"exists": SELF_RES}, {"not": right}]}, "priority": 3,
                          "terminal": True,
                          "ending": {"id": "wrong", "title": "오인 체포", "epilogue": "진범이 밤사이 사라진다",
                                     "tone": "씁쓸"}}},
            "transition_cases": [
                {"case_id": "spawn", "from": None, "to": "investigate",
                 "steps": [{"step_id": "c", "type": "SET_EVENT_LOCAL", "key": "culprit", "value": culprit}]},
                {"case_id": "s", "from": "investigate", "to": "solved", "steps": []},
                {"case_id": "w", "from": "investigate", "to": "wrong", "steps": []}],
            "case_selection": "FIRST_VALID",
            "resolve_when": {"any": [{"cmp": "EQ", "ref": {"ref": "EVENT_PHASE", "instance": "$self"}, "value": "solved"},
                                     {"cmp": "EQ", "ref": {"ref": "EVENT_PHASE", "instance": "$self"}, "value": "wrong"}]}}


def _raw(tpl=None, **extra):
    raw = ev_raw({"mystery": tpl or _mystery()}, **extra)
    raw["information_definitions"]["C1"] = copy.deepcopy(CLUE)
    return raw


def _session(tpl=None, *, kang_here=True, **extra):
    s, D = make_session(_raw(tpl, **extra), at=ROOM, places=("항구", ROOM))
    s.spatial_state["placements"][KANG] = {"kind": "AT_PLACE", "place_id": ROOM if kang_here else "항구"}
    s.spatial_state["placements"][PARK] = {"kind": "AT_PLACE", "place_id": "항구"}
    s.actor_registry.update({a: {"kind": "NPC", "display_name": n, "source_kind": "AUTHORED_NPC", "source_key": n,
                                 "aliases": []} for a, n in ((KANG, "강지수"), (PARK, "박노인"))})
    r = run(D, s, K, progress={"observer_actor": PC, "scene_place": ROOM}, outer_origin="INITIALIZATION",
            initial_full_scan=True)
    for f in ("event_instances", "world_schedule", "access_control_state"):
        setattr(s, f, r.projected_world.domain(f))
    return s, D


def _inst(w):
    ev = w.domain("event_instances") if hasattr(w, "domain") else w.event_instances
    return next(i for i in ev.values() if i["template_id"] == "mystery")


# ── 컴파일 · 미저작 불변 ───────────────────────────────────────

def test_new_keys_compile_only_when_authored():
    D = dc.compile_scenario(_raw(), "t").definition
    ph = D["event_templates"]["mystery"]["phases"]
    assert ph["investigate"]["resolution_options"][0]["target"] == {"type": "ACTOR", "actors": [KANG, PARK],
                                                                     "present": True}
    assert ph["wrong"]["ending"]["tone"] == "씁쓸" and ph["investigate"]["finds"][0]["object"] == "피 묻은 장갑"
    plain = dc.compile_scenario(ev_raw({"m": {**_mystery(), "phases": {
        "investigate": {"eligible_when": {"const": True}, "priority": 1,
                        "resolution_options": [{"option_id": "a", "label": "x"}], "finds": ["밧줄"]}},
        "transition_cases": [{"case_id": "spawn", "from": None, "to": "investigate", "steps": []}],
        "resolve_when": {"const": False}}}), "t").definition["event_templates"]["m"]["phases"]["investigate"]
    assert plain["resolution_options"] == [{"option_id": "a", "label": "x"}] and plain["finds"] == ["밧줄"]
    assert "ending" not in plain


@pytest.mark.parametrize("patch,code", [
    (lambda t: t["phases"]["investigate"]["resolution_options"][0]["target"].update({"from": ["없는사람"]}),
     "EVENT_RESOLUTION_TARGET_INVALID"),
    (lambda t: t["phases"]["solved"]["ending"].pop("epilogue"), "EVENT_PHASE_ENDING_INVALID"),
    (lambda t: t["phases"]["investigate"]["finds"].append({"info": "없는정보"}), "EVENT_PHASE_FINDS_INVALID"),
])
def test_invalid_authoring_is_rejected(patch, code):
    t = _mystery()
    patch(t)
    assert code in [d.code for d in dc.compile_scenario(_raw(t), "t").errors]


# ── E2 결단 대상 ─────────────────────────────────────────────

def test_target_resolution_lists_present_suspects_only():
    s, D = _session()
    assert [lbl for _, _, lbl in manifest(s).resolutions] == ["체포 — 강지수"]          # 박노인은 장면에 없다
    s2, _ = _session(_mystery(present=False))
    assert [lbl for _, _, lbl in manifest(s2).resolutions] == ["체포 — 강지수", "체포 — 박노인"]


@pytest.mark.parametrize("culprit,ending", [(KANG, "solved"), (PARK, "wrong")])
def test_arrest_reaches_authored_ending_and_guides_the_last_narration(culprit, ending):
    s, D = _session(_mystery(culprit))
    key = next(k for k, (_, oid), _ in manifest(s).resolutions if oid == f"arrest:{KANG}")
    p, _, intent = plan(s, {"movement_kind": "NONE", "time_kind": "NONE", "resolution": key})
    assert p.status == PW.PLAN_PLANNED and _inst(p.projected_world)["current_phase"] == ending
    assert [e["id"] for e in EV.new_endings(D, s, p.projected_world)] == [ending]
    blk = TW.ending_block(ctx_of(s), p, s)
    assert "마지막" in blk and ("자백" if ending == "solved" else "사라진다") in blk
    assert TW.ending_block(ctx_of(s), None, s) == ""                                   # 계획 없으면 없음


# ── E4 저작 단서 ─────────────────────────────────────────────

def test_clue_is_guided_exposed_and_becomes_takeable_object():
    s, D = _session()
    c = ctx_of(s)
    assert "제단 밑의 피 묻은 장갑" in TW.event_guide_block(c, None, s)
    p, _, _ = plan(s, {"movement_kind": "NONE", "time_kind": "NONE"})
    man = TW.build_observation_manifest(D, c, p, s)
    key = next(k for k, iid, _ in man.infos if iid == "C1")
    res = TW.process_observation(c, s, p, man, {"info_exposed": [key]}, transaction_id="tx-e4", narrative_seed="x")
    assert "C1" in res.exposures
    w = res.projected_world
    assert w.domain("cognition_state")[core.cognition.actor_holder(PC)]["C1"]["awareness"] == "CONTENT"
    rows = TW.discoveries_at(D, w, PC, ROOM)
    assert [r[2] for r in rows] == ["피 묻은 장갑"] and rows[0][3] == "OBJECT"           # 이후 D키로 줍는다
    assert TW.unknown_finds(D, w, PC, ROOM) == []                                       # 안 뒤엔 다시 안내 없음


def test_clue_not_offered_elsewhere():
    s, D = _session()
    s.spatial_state["placements"][PC] = {"kind": "AT_PLACE", "place_id": "항구"}
    s.spatial_state["scene_place"] = "항구"
    assert TW.unknown_finds(D, s, PC, "항구") == []


# ── E3 결말 처리(커밋 뒤) ────────────────────────────────────

async def test_conclusion_closes_through_existing_lifecycle_and_records(monkeypatch):
    import cogs.gm as gm_mod
    s, D = _session()
    inst = _inst(s)
    inst["current_phase"], inst["lifecycle"] = "solved", "RESOLVED"
    s.game_ch_id, s.gm_active = 111, True
    ch = FakeChannel(111)
    closed = []

    async def fake_close(bot, session, *, reason):
        closed.append(reason)
        return {"ok": True, "refund": {}}
    monkeypatch.setattr(WL, "close_session", fake_close)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(core, "refresh_boards", _noop)
    monkeypatch.setattr(core, "save_session_data", _noop)
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = SimpleNamespace(get_channel=lambda cid: ch if cid == 111 else None)
    assert await gm._conclude_if_ending(s, None)
    assert closed == [core.cache_lifecycle.REASON_ENDING] and s.gm_active is False
    assert "해결" in ch.sent[0].embeds[0].title
    assert core.stats.load_stats(PLAYER_UID)["endings"] == {"t": {"solved": 1}}


async def test_no_conclusion_without_ending_or_when_close_blocked(monkeypatch):
    import cogs.gm as gm_mod
    s, D = _session()
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = SimpleNamespace(get_channel=lambda cid: None)
    assert not await gm._conclude_if_ending(s, None)                                     # 결말 국면 아님
    _inst(s)["current_phase"] = "wrong"

    async def blocked(bot, session, *, reason):
        return {"ok": False, "reason": "진행 중인 턴"}
    monkeypatch.setattr(WL, "close_session", blocked)
    s.gm_active = True
    assert not await gm._conclude_if_ending(s, None) and s.gm_active is True             # 다음 커밋에서 다시


# ── E5 수첩 ─────────────────────────────────────────────────

def test_notebook_groups_known_clues_by_tag_only_when_authored():
    s, D = _session()
    s.cognition_state = {core.cognition.actor_holder(PC): {"C1": {"awareness": "CONTENT"}}}
    assert TW.notebook_text(s, PLAYER_UID) is None                                      # 미저작
    s.scenario_data = dict(s.scenario_data, knowledge_notebook={"title": "수사 수첩",
                                                                 "groups": [{"tag": "증거", "label": "증거"}]})
    text = TW.notebook_text(s, PLAYER_UID)
    assert text.startswith("**수사 수첩**") and "· 제단 밑의 피 묻은 장갑" in text
    assert TW.notebook_text(s, "999") is None                                           # 다른 사람


# ── E6 풀이 가능성 ───────────────────────────────────────────

def _check_raw(n_clues=3, mislead=False):
    tpl = _mystery()
    tpl["transition_cases"].insert(1, {"case_id": "spawn2", "from": None, "to": "investigate",
                                       "steps": [{"step_id": "c", "type": "SET_EVENT_LOCAL", "key": "culprit",
                                                  "value": PARK}]})
    tpl["case_selection"] = "DETERMINISTIC_WEIGHTED"
    clues = {}
    infos = {}
    for who, name, tag in ((KANG, "강지수", "kang"), (PARK, "박노인", "park")):
        finds = []
        for i in range(n_clues):
            iid = f"c_{tag}_{i}"
            infos[iid] = dict(CLUE, summary=f"{name} 단서 {i}")
            finds.append({"info": iid})
            clues[iid] = {"points_to": name, "strength": "support"}
        tpl["phases"][f"clues_{tag}"] = {
            "eligible_when": {"cmp": "EQ", "ref": {"ref": "EVENT_LOCAL", "template": "mystery", "key": "culprit"},
                              "value": who}, "priority": 10 if tag == "kang" else 11, "finds": finds}
    if mislead:
        clues["C1"] = {"points_to": "박노인", "strength": "decisive"}                     # 모든 진상에서 도달
    raw = _raw(tpl)
    raw["information_definitions"].update(infos)
    raw["variant_check"] = {"template": "mystery", "key": "culprit", "min_support": 3, "clues": clues}
    return raw


def test_variant_check_passes_solvable_and_flags_failures():
    raw = _check_raw()
    res = dc.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    D = res.definition
    assert VC.check(raw, D) == []
    raw = _check_raw(n_clues=2)
    assert {c for _s, c, _m in VC.check(raw, dc.compile_scenario(raw, "t").definition)} == {"VARIANT_UNDER_SUPPORTED"}
    raw = _check_raw(mislead=True)
    out = VC.check(raw, dc.compile_scenario(raw, "t").definition)
    assert [c for _s, c, _m in out] == ["VARIANT_DECISIVE_MISLEAD"] and KANG in out[0][2]
    assert VC.check({}, D) == []                                                        # 미저작
