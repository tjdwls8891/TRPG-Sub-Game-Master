# -*- coding: utf-8 -*-
"""v7.26 시작 상황 재저작 — 시작 틀별 정본(시작 물건 · 시작 장면 인물 사건).

· 엔진: world_start.items(장소 물건/인물 소지) — 컴파일 검증 · genesis와 같은 strict 저장 · 그 시작에서만 생김.
· 영도 저작: 기존 NPC만 쓴다(신규 NPC 0). 시작 장면 인물은 시작 첫 분에만 생기는 사건이 배치하고, 자기 일을 마친 뒤
  걸어서(TRANSIT) 떠나며, 목적지에서 PC가 곁에 없을 때만 정본 배치를 풀어 평상시 일과로 돌아간다.
"""
from __future__ import annotations

import copy
import json

import pytest

import core
from core import definition_compiler as dc
from core import items as IT
from core import npc_position as NP
from core import spatial as SP
from core import world_lifecycle as WL
from tests.policy import test_h07_yeongdo_draft as Y
from tests.policy.test_h06a_lifecycle import dyn_raw, setup_session, START
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v717_items import key_of

pytestmark = pytest.mark.policy


def _ws(**kw):
    return dict(copy.deepcopy(START), **kw)


# ── 컴파일러 ─────────────────────────────────────────────────

def test_world_start_items_compile_and_validate():
    items = [{"key": "작은 배", "quantity": 1, "place": "항구"}, {"key": "양초", "quantity": 2, "owner": "강지수"}]
    res = dc.compile_scenario(dyn_raw(world_start=_ws(items=items)), "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    ws = res.definition["initialization_definitions"]["start_frames"][0]["world_start"]
    assert ws["items"] == items
    plain = dc.compile_scenario(dyn_raw(), "t").definition["initialization_definitions"]["start_frames"][0]
    assert "items" not in plain["world_start"]                       # 미저작 틀은 키 없음(revision 불변)
    for bad, code in (
            ([{"key": "x", "quantity": 1}], "WORLD_START_ITEMS_INVALID"),                         # 장소·소유자 없음
            ([{"key": "x", "quantity": 1, "place": "항구", "owner": "강지수"}], "WORLD_START_ITEMS_INVALID"),
            ([{"key": "x", "quantity": 0, "place": "항구"}], "WORLD_START_ITEMS_INVALID"),
            ([], "WORLD_START_ITEMS_INVALID"),
            ([{"key": "x", "quantity": 1, "place": "없는곳"}], "WORLD_START_PLACE_UNKNOWN"),
            ([{"key": "x", "quantity": 1, "owner": "모르는사람"}], "WORLD_START_ITEM_OWNER_UNKNOWN")):
        codes = [d.code for d in dc.compile_scenario(dyn_raw(world_start=_ws(items=bad)), "t").errors]
        assert code in codes, (bad, codes)


def test_start_choices_identity_unchanged_without_items():
    ch = WL.StartChoices("항구", 10, "outsider", "harbor")
    assert "items" not in ch.to_dict()                               # 시작 물건 없는 틀의 초기화 정체성 그대로
    ch2 = WL.StartChoices("항구", 10, "outsider", "harbor", ({"key": "x", "quantity": 1, "place": "항구"},))
    assert ch2.to_dict()["items"] == [{"key": "x", "quantity": 1, "place": "항구"}]


def test_genesis_item_state_places_and_gives_start_items_only_when_given():
    s = core.TRPGSession("v726", 1, 2, "t", {})
    s.resources = {"강지수": {"hp": 3}}
    assert "place_items" not in IT.genesis_item_state({}, s)
    out = IT.genesis_item_state({}, s, ({"key": "작은 배〈손상〉", "quantity": 1, "place": "항구"},
                                        {"key": "양초 두 개", "quantity": 1, "owner": "강지수"}), source_prefix="start.f")
    assert out["place_items"] == {"start.f.0": {"place": "항구", "key": "작은 배〈손상〉", "quantity": 1,
                                                "origin": "START_FRAME", "by": None, "tx": None}}
    assert out["resources"]["강지수"] == {"hp": 3, "양초[개]": 2}
    assert s.resources == {"강지수": {"hp": 3}} and not getattr(s, "place_items", None)   # 순수(세션 불변)


async def test_initialize_world_saves_start_items_with_genesis(rig):
    r = rig
    items = [{"key": "작은 배", "quantity": 1, "place": "항구"}, {"key": "양초", "quantity": 1, "owner": "강지수"}]
    s = setup_session(r, dyn_raw(world_start=_ws(items=items)))
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_OK, res.reason
    assert s.place_items == {"start.harbor.0": {"place": "항구", "key": "작은 배", "quantity": 1,
                                                "origin": "START_FRAME", "by": None, "tx": None}}
    assert s.resources["강지수"]["양초"] == 1
    data = json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))
    assert data["place_items"] == s.place_items                      # genesis와 같은 strict 저장
    assert s.world_initialization["start_choices"]["items"] == items


# ── 영도 저작 ────────────────────────────────────────────────

FRAMES = {   # 시작 틀 → (시작 장면 인물 사건, 인물, 첫 배치, 마지막 목적지)
    "arrive_finding": ("yd_start_stash", "yd.npc.lee_seonhui", "동삼동 해안로", "중리 해변"),
    "arrive_person": ("yd_start_haul", "yd.npc.jin_bokrye", "물양장", "해녀 작업장"),
    "arrive_person_threat": ("yd_start_rinse", "yd.npc.ryu_gaeun", "영선동 방파제", "저지대 상업지구"),
    "arrive_threat_finding": ("yd_start_pipes", "yd.npc.shin_daeho", "흰여울 절벽길", "집수 설비"),
    "arrive_night": ("yd_start_candle", "yd.npc.baek_inha", "감지해변", "태종대 방면 숲"),
}


def test_yeongdo_start_frames_use_only_existing_npcs_and_literal_starts():
    raw = Y._raw()
    D, _ = Y._defn()
    assert [f["id"] for f in raw["start_frames"]] == ["arrive_plain", "arrive_finding", "arrive_person",
                                                     "arrive_person_threat", "arrive_threat_finding", "arrive_night"]
    assert len(D["actor_definitions"]) == 47                         # 신규 NPC 없음
    names = set(raw["default_npcs"])
    for fr in raw["start_frames"]:
        assert not any(str(v).startswith(("list:", "place:")) for v in fr["slots"].values())
        for it in fr["world_start"].get("items") or ():
            assert it.get("owner") is None or it["owner"] in names
    for tid, actor, *_ in FRAMES.values():
        assert actor in D["actor_definitions"] and tid in D["event_templates"]


@pytest.mark.parametrize("frame", sorted(FRAMES))
def test_yeongdo_start_npc_appears_only_in_its_frame_then_walks_back_to_routine(frame):
    tid, actor, first, dest = FRAMES[frame]
    D, _ = Y._defn()
    w, ch = Y.genesis(frame)
    assert SP.placement_of(w, actor) == SP.at_place(first)            # 시작 순간 그 자리(정본)
    for other in FRAMES:                                               # 다른 시작에서는 생기지 않는다
        if other != frame:
            assert Y.lifecycle(Y.genesis(other)[0], tid) == "NONE"
    seen_transit, sigs = False, []
    for _ in range(40):
        p = Y.turn(w, Y.wait(15))
        w = Y.adopt(p)
        sigs += [s.get("cue") for s in Y.signals(p)]
        seen_transit |= (SP.placement_of(w, actor) or {}).get("kind") == SP.TRANSIT
        if Y.lifecycle(w, tid) != "ACTIVE":
            break
    assert Y.lifecycle(w, tid) == "RESOLVED"
    assert seen_transit                                                # 걸어서 떠난다(순간이동 없음)
    assert SP.placement_of(w, actor) is None                           # 정본 배치 해제 → 일과/미추적
    e = NP.effective_position(D, w, actor)
    assert e["source"] in (NP.ROUTINE, NP.UNTRACKED)
    leave = [st["cue"] for c in D["event_templates"][tid]["transition_cases"] for st in c["steps"]
             if st["type"] == "EMIT_SIGNAL"]
    assert set(leave) & set(sigs)                                      # 시작 장소에 남은 PC는 떠나는 것을 본다


def test_yeongdo_start_npc_does_not_vanish_in_front_of_following_pc():
    tid, actor, first, dest = FRAMES["arrive_person"]
    w, _ = Y.genesis("arrive_person")
    w = Y.setup(w, dest)                                               # PC가 목적지(그녀가 돌아갈 곳)에 먼저 가 있다
    for _ in range(12):
        w = Y.adopt(Y.turn(w, Y.wait(15)))
        if SP.placement_of(w, actor) == SP.at_place(dest):
            break
    assert SP.placement_of(w, actor) == SP.at_place(dest)
    w = Y.adopt(Y.turn(w, Y.wait(30)))
    assert Y.lifecycle(w, tid) == "ACTIVE" and SP.placement_of(w, actor) == SP.at_place(dest)   # 눈앞에서 풀지 않는다
    w = Y.adopt(Y.turn(w, Y.go("중리 해변")))                           # PC가 걸어서 자리를 뜨면
    assert Y.lifecycle(w, tid) == "RESOLVED" and SP.placement_of(w, actor) is None


def test_yeongdo_start_items_are_scene_objects_and_boats_cannot_be_pocketed():
    D, _ = Y._defn()
    defs = IT.definitions_of(D)
    s = Y._session()
    out = IT.genesis_item_state(D, s, WL.resolve_start_choices(D, {"id": "arrive_finding"})[0].items,
                                source_prefix="start.arrive_finding")
    here = {r["key"]: r for r in out["place_items"].values() if r["place"] == "동삼동 갯가"}
    assert sorted(here) == sorted(["작은 배", "파란 플라스틱 상자", "합성섬유 밧줄[타래]", "작업 장갑[켤레]〈손상〉",
                                   "낚싯바늘〈손상〉"])
    assert here["파란 플라스틱 상자"]["closed"] is True                  # v7.29: 보관함 = 닫힌 용기, 내용물은 그 안
    assert all(here[k].get("inside") for k in ("합성섬유 밧줄[타래]", "작업 장갑[켤레]〈손상〉", "낚싯바늘〈손상〉"))
    s.resources, s.place_items = out["resources"], out["place_items"]
    m = IT.build_manifest(s, pc_name="테스터", scene_place="동삼동 갯가", defs=defs)
    take = lambda label: [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, label), "quantity": 1}]
    with pytest.raises(IT.ItemContractError):                          # 배는 주머니에 넣을 수 없다(운반 불가 등급)
        IT.build_plan(s, m, take("작은 배"), defs=defs, pc_name="테스터", transaction_id="t1")
    with pytest.raises(IT.ItemContractError):                          # 닫힌 상자 안의 물건은 열기 전에는 못 꺼낸다
        IT.build_plan(s, m, take("합성섬유 밧줄"), defs=defs, pc_name="테스터", transaction_id="t2")
    box = key_of(m, "파란 플라스틱 상자")
    IT.apply_plan(s, IT.build_plan(s, m, [{"op": "OPEN", "owner": "O1", "item": box, "quantity": 1}] + take("합성섬유 밧줄"),
                                   defs=defs, pc_name="테스터", transaction_id="t3"))
    assert s.resources["테스터"]["합성섬유 밧줄[타래]"] == 1                 # 열고 꺼낸 것만 소지 — 나머지는 그 자리
    assert "작업 장갑[켤레]〈손상〉" not in s.resources["테스터"]
    pipes = IT.genesis_item_state(D, Y._session(), WL.resolve_start_choices(D, {"id": "arrive_threat_finding"})[0].items)
    assert pipes["resources"]["신대호"]["아연도금 파이프"] == 2         # 그 시작에서만 인물 소지
    assert "아연도금 파이프" not in (IT.genesis_item_state(D, Y._session())["resources"].get("신대호") or {})
