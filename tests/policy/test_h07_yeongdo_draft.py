# -*- coding: utf-8 -*-
"""H07-B/C 영도 첫 full-capability 초안 — 실제 scenarios/영도.json 기반 표적 검증(체크포인트 게이트).

순수 경로만 쓴다: 컴파일 → InitialWorldPlan(genesis) → ResolvedActionIntent → plan_player_turn → 투영 세계 채택.
(LLM·디스코드 없음. 커밋 경로 전체는 H05/H06 테스트가 이미 고정했다.)
"""
from __future__ import annotations

import copy
import functools
import json
import os

import pytest

import core
from core import action_intent as AI
from core import definition_compiler as dc
from core import player_world as PW
from core import spatial as SP
from core import world_lifecycle as WL
from core import world_mutation as WM

pytestmark = pytest.mark.policy

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UID, PC = "42", "pc_42"


def _raw():
    return json.load(open(os.path.join(ROOT, "scenarios", "영도.json"), encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def _defn():
    res = dc.compile_scenario(_raw(), "영도")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    return res.definition, res.revision


def _session(players=None):
    s = core.TRPGSession("h07-sim", 1, 2, "영도", _raw())
    s.players = players or {UID: {"name": "테스터"}}
    return s


@functools.lru_cache(maxsize=8)
def _genesis(frame="arrive_plain"):
    D, _ = _defn()
    ch, why = WL.resolve_start_choices(D, {"id": frame})
    assert ch is not None, why
    plan = WL.build_initial_world_plan(D, _session(), ch, initialization_id=f"init-{frame}", narrative_seed="seed-h07")
    assert plan.status == "READY", (plan.reason, plan.diagnostics[:5])
    return plan.projected_world.to_dict(), ch


def genesis(frame="arrive_plain"):
    d, ch = _genesis(frame)
    return WM.ProjectedWorld(copy.deepcopy(d)), ch


def ctx():
    D, rev = _defn()
    return PW.WorldContext(PW.MODE_DYNAMIC, "ok", D, rev, (PC,), PC, {PC: UID})


_N = [0]


def turn(world, intent, *, caps=()):
    _N[0] += 1
    p = PW.plan_player_turn(ctx(), world, intent, transaction_id=f"tx{_N[0]}", narrative_seed="seed-h07",
                            capabilities=caps)
    if p.status == PW.PLAN_NEEDS_CHOICE and p.route_options:       # 플레이어가 첫 경로를 고른 것으로
        sig = tuple(p.route_options[0].signature) if hasattr(p.route_options[0], "signature") \
            else tuple(p.route_options[0]["signature"])
        intent = AI.ResolvedActionIntent(movement=intent.movement, route_signature=sig,
                                         definition_revision=intent.definition_revision)
        _N[0] += 1
        p = PW.plan_player_turn(ctx(), world, intent, transaction_id=f"tx{_N[0]}", narrative_seed="seed-h07",
                                capabilities=caps)
    return p


def go(place, **kw):
    return AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_DESTINATION, place),
                                   definition_revision=_defn()[1], **kw)


def wait(minutes, activity="WAIT"):
    return AI.ResolvedActionIntent(time_advance=AI.TimeAdvanceIntent("DURATION", duration_minutes=minutes,
                                                                     activity=activity),
                                   definition_revision=_defn()[1])


def adopt(plan):
    assert plan.status == PW.PLAN_PLANNED, (plan.status, plan.reason, plan.diagnostics[:5])
    return plan.projected_world


def setup(world, place, *, extra_ops=()):
    """테스트 준비: PC를 장소에 두고 그 자리에서 보이는 출구를 알게 한다(typed 연산 투영 — 정본 규칙 그대로)."""
    D, _ = _defn()
    ops = [WM.make_op("t.pl", SP.OP_ACTOR_PLACEMENT_SET, "TEST", {"actor_id": PC, "place_id": place}),
           WM.make_op("t.sc", SP.OP_SCENE_PLACE_SET, "TEST", {"place_id": place})]
    view = PW.local_view_ops("t.v", D, world, [PC], place)
    b = WM.WorldMutationBatch(batch_id="t", outer_origin="ADMIN_EDIT", source_world_version_id=None,
                              definition_revision=None)
    for op in PW.chain(ops, view, list(extra_ops)):
        b.add(op)
    b.freeze()
    r = WM.project(world, b, WM.PRODUCTION_REGISTRY)
    assert r.ok, [c.message for c in r.conflicts]
    return r.world


def lifecycle(world, tid):
    insts = [i for i in (world.domain("event_instances") or {}).values() if i["template_id"] == tid]
    return insts[-1]["lifecycle"] if insts else "NONE"


def phase_of(world, tid):
    insts = [i for i in (world.domain("event_instances") or {}).values() if i["template_id"] == tid]
    return insts[-1]["current_phase"] if insts else None


def signals(plan):
    return [s for s in plan.signals if s.get("observable")]


# ── 정의 ─────────────────────────────────────────────────────

def test_yeongdo_full_capability_definition_compiles_deterministically():
    D, rev = _defn()
    meta = D["scenario_meta"]
    assert sorted(meta["validated_capabilities"]) == sorted(dc.KNOWN_CAPABILITIES)
    assert dc.compile_scenario(_raw(), "영도").revision == rev
    for need in WL.REQUIRED_CAPABILITIES:
        assert need in meta["validated_capabilities"]
    assert len(D["place_definitions"]) == 125 and len(D["actor_definitions"]) == 47
    assert D["calendar"]["calendar_type"] == "GREGORIAN"


def test_every_start_frame_has_literal_place_time_matching_world_start():
    raw = _raw()
    D, _ = _defn()
    by_id = {f["frame_id"]: f for f in D["initialization_definitions"]["start_frames"]}
    for fr in raw["start_frames"]:
        ws = by_id[fr["id"]]["world_start"]
        assert fr["slots"]["장소"] == ws["place"]                         # 인트로 문안 = 정본 시작점
        assert not str(fr["slots"].get("때", "")).startswith("list:")
        assert ws["knowledge_seed"] == "yd_newcomer" and ws["date"] == "2016-10-02"


def test_new_route_places_are_ordinary_place_ids_with_image_inheritance():
    D, _ = _defn()
    g = SP.SpatialGraph(D)
    for pid, parent in (("하단 방어선 아래", "산복도로 상단"), ("산복도로 동측 내리막", "산복도로"),
                        ("방파제 입구", "동삼동 해안로")):
        assert D["place_definitions"][pid]["parent"] == parent
        assert D["place_definitions"][pid]["image"] is None                  # 자기 이미지 없음 → 상위 상속
        assert g.out_edges(pid)


# ── genesis ──────────────────────────────────────────────────

@pytest.mark.parametrize("frame", ["arrive_plain", "arrive_night", "arrive_threat_finding", "arrive_person"])
def test_genesis_places_pc_clock_npcs_and_no_unnarrated_signals(frame):
    w, ch = genesis(frame)
    D, _ = _defn()
    assert SP.placement_of(w, PC) == SP.at_place(ch.start_place)
    assert w.domain("world_clock")["minute_index"] == ch.start_minute
    placed = [a for a, p in (w.domain("spatial_state")["placements"]).items() if a != PC]
    assert placed == []                                               # V2: genesis 정본 NPC 배치 없음(A 분류 0명)
    from core import npc_position as NP
    srcs = {a: NP.effective_position(D, w, a)["source"] for a in D["actor_definitions"]}
    assert sum(1 for a in D["actor_definitions"].values() if a.get("routine")) == 35      # 일과 저작 35 · 미저작 12
    assert all(v in (NP.ROUTINE, NP.UNTRACKED) for v in srcs.values())  # 파생 또는 미추적(정본 아님)
    assert all(srcs[a] == NP.UNTRACKED for a, d in D["actor_definitions"].items() if not d.get("routine"))
    assert lifecycle(w, "yd_ev_first_sighting") == "NONE"            # 시작 분에는 관찰 신호성 사건 없음
    for inst in w.domain("event_instances").values():                 # 시작 분에 생긴 사건은 신호를 내지 않는다(인트로 밖)
        spawn = next(c for c in D["event_templates"][inst["template_id"]]["transition_cases"] if c["from"] is None)
        assert not [st for st in spawn["steps"] if st["type"] == "EMIT_SIGNAL"], inst["template_id"]
    assert w.domain("access_control_state")["jungri_tide_path"]["state"] == "LOW_TIDE"
    know = SP.actor_knowledge(w, PC)
    assert SP.knows_place(know, "봉래산")                               # 공개 지명
    for far in ("해련 자치 마을", "조도 방위 사령부", "장부방", "지하 의료소"):  # 보이지 않는 세력 거점·내부는 모른다
        assert not SP.knows_place(know, far)


def test_genesis_npc_own_secret_cognition():
    w, _ = genesis()
    D, _ = _defn()
    cog = w.domain("cognition_state")
    for aid, a in D["actor_definitions"].items():
        assert cog[f"actor:{aid}"][a["secret_info_id"]]["awareness"] == "CONTENT"


# ── 이동 · 장소 ───────────────────────────────────────────────

def test_newcomer_walks_inland_via_locally_seen_exit():
    w, _ = genesis("arrive_plain")                                     # 청학동 선착장 05:40
    p = turn(w, go("저지대 상업지구"))
    w2 = adopt(p)
    assert SP.placement_of(w2, PC) == SP.at_place("저지대 상업지구")
    assert 5 <= p.code_owned_minutes <= 30
    know = SP.actor_knowledge(w2, PC)
    assert SP.knows_place(know, "남항 중공업 연대")                     # 도착해서 보이는 출구로 알게 됐다


def test_route_place_stop_then_branch_east_descent():
    w, _ = genesis()
    w = setup(w, "산복도로")
    w = adopt(turn(w, go("산복도로 동측 내리막")))
    assert SP.placement_of(w, PC) == SP.at_place("산복도로 동측 내리막")
    w = adopt(turn(w, go("동삼 언덕")))
    assert SP.placement_of(w, PC) == SP.at_place("동삼 언덕")


# ── 세계 주도 진행(오프스크린 NPC 이동) ────────────────────────

def test_salvage_patrol_leaves_at_morning_offscreen():
    from core import npc_position as NP
    D, _ = _defn()
    w, _ = genesis("arrive_plain")                                     # 05:40 — 수습대는 요새 안(일과)
    assert NP.effective_place(D, w, "yd.npc.lee_junhyeok") == "수습대 집결지"
    assert SP.placement_of(w, "yd.npc.lee_junhyeok") is None          # 정본 배치가 아니라 일과
    start = w.domain("world_clock")["minute_index"]
    w2 = w
    for _ in range(8):                                                 # 관찰 신호가 대기를 끊으면 다시 기다린다
        w2 = adopt(turn(w2, wait(120)))
        if w2.domain("world_clock")["minute_index"] - start >= 120:      # 07:40 — 이른 아침 출발 뒤
            break
    assert w2.domain("world_clock")["minute_index"] - start >= 120
    assert NP.effective_place(D, w2, "yd.npc.lee_junhyeok") == "저지대 상업지구"   # 이른 아침 일과
    assert SP.placement_of(w2, "yd.npc.lee_junhyeok") is None          # 일과 전환을 정본 SET으로 만들지 않는다
    assert phase_of(w2, "yd_namhang_salvage_patrol") == "out"


# ── 가입 사건(해련 사다리) — 활동·장소·시간·접근 ─────────────────

def test_haeryeon_ladder_join_by_waiting_then_climbing():
    w, _ = genesis()
    w = setup(w, "산복도로 상단")
    w = adopt(turn(w, go("하단 방어선 아래")))                         # 도착이 사건을 연다
    assert phase_of(w, "yd_ev_haeryeon_ladder") == "watched"
    p = turn(w, wait(30))
    w = adopt(p)
    assert phase_of(w, "yd_ev_haeryeon_ladder") == "tested"
    assert w.domain("access_control_state")["haeryeon_ladder"]["state"] == "LOWERED"
    p = turn(w, go("하단 방어선"))
    w = adopt(p)
    assert SP.placement_of(w, PC) == SP.at_place("하단 방어선")
    assert lifecycle(w, "yd_ev_haeryeon_ladder") == "RESOLVED"
    inst = [i for i in w.domain("event_instances").values() if i["template_id"] == "yd_ev_haeryeon_ladder"][0]
    assert inst["local_state"]["grant_faction"] == "해련 자치 마을"
    assert w.domain("access_control_state")["haeryeon_rear_line"]["state"] == "ADMITTED"
    sig = [s for s in signals(p) if s["signal_id"] == "known"]
    assert sig and sig[0]["exposes"] == ["yd.info.place.haeryeon", "yd.info.q.join_haeryeon_silence"]
    assert lifecycle(w, "yd_ev_haeryeon_gutter") in ("NONE", "CANCELLED")      # 다른 가입 경로는 열리지 않는다


def test_leaving_before_test_cancels_and_ladder_stays_up():
    w, _ = genesis()
    w = setup(w, "산복도로 상단")
    w = adopt(turn(w, go("하단 방어선 아래")))
    w = adopt(turn(w, go("산복도로 상단")))
    assert lifecycle(w, "yd_ev_haeryeon_ladder") == "CANCELLED"
    assert w.domain("access_control_state").get("haeryeon_ladder", {}).get("state", "RAISED") == "RAISED"


def test_namhang_doors_confirm_outer_and_block_inner_for_outsiders():
    w, _ = genesis()
    w = setup(w, "남항 중공업 연대")
    p = turn(w, go("제1방어선"))                                       # 바깥 문: 경비에게 신원을 밝혀야(확인)
    assert p.status == PW.PLAN_PLANNED and SP.placement_of(p.projected_world, PC) == SP.at_place("남항 중공업 연대")
    assert p.outcome["access_interruption"]["kind"] == "ACCESS_ACTION_REQUIRED"
    assert p.outcome["access_interruption"]["action_policy"] == "CONFIRM"
    w = setup(w, "제1방어선")
    p = turn(w, go("수습대 집결지"))                                   # 안쪽 문: 외부인은 별도 행동(검역·판정) 없이 못 지난다
    assert p.status == PW.PLAN_PLANNED and SP.placement_of(p.projected_world, PC) == SP.at_place("제1방어선")
    assert p.outcome["access_interruption"]["action_policy"] == "SEPARATE_ACTION"
    p = turn(w, go("수습대 집결지"), caps=("항생제",))                   # 연대가 목마른 물품 소지 = 거래 변형(확인)
    assert p.outcome["access_interruption"]["action_policy"] == "CONFIRM"


# ── 모드 ─────────────────────────────────────────────────────

def test_multi_pc_yeongdo_stays_legacy():
    s = _session({UID: {"name": "가"}, "77": {"name": "나"}})
    d, why = WL.compile_candidate(s)
    assert d is None and why == "not_single_pc"


# ── 대표 사건(퀘스트 이행) ─────────────────────────────────────

def _day2(world):
    """도착 첫날 사건(yd_arrival_day)이 끝난 뒤로 시간을 보낸다(수면 반복 — 신호가 끊으면 다시)."""
    for _ in range(12):
        if lifecycle(world, "yd_arrival_day") == "RESOLVED":
            return world
        world = adopt(turn(world, wait(600, "SLEEP")))
    raise AssertionError("도착 첫날이 끝나지 않았다")


def test_lost_team_event_sends_squad_leader_out_and_exposes_truth():
    w, _ = genesis()
    w = _day2(setup(w, "은신처"))
    w = setup(w, "저지대 상업지구")
    p = turn(w, go("남항 중공업 연대"))
    w = adopt(p)
    assert phase_of(w, "yd_ev_namhang_lost_team") == "dispute"
    assert any(s.get("exposes") == ["yd.info.namhang.team2_missing"] for s in signals(p))
    p = turn(w, wait(40))
    w = adopt(p)
    assert phase_of(w, "yd_ev_namhang_lost_team") == "search"
    assert any(s.get("exposes") == ["yd.info.q.join_namhang_rescue"] for s in signals(p))
    pl = SP.placement_of(w, "yd.npc.no_sangjin")
    assert pl["kind"] == "TRANSIT" or pl.get("place_id") != "수습대 집결지"      # 사건이 NPC를 움직였다


def test_shop_search_exposes_scavenger_marks_only_while_searching():
    w, _ = genesis()
    w = setup(w, "저지대 상업지구")
    w = adopt(turn(w, go("약국")))
    assert phase_of(w, "yd_ev_shop_marks") == "present"
    p = turn(w, wait(30, "SEARCH"))
    w = adopt(p)
    assert phase_of(w, "yd_ev_shop_marks") == "found"
    assert any(s.get("exposes") == ["yd.info.q.place_shop_scavenge"] for s in signals(p))


def test_first_sighting_study_requires_watching():
    w, _ = genesis()                                                   # 청학동 선착장
    p = turn(w, wait(15))
    w = adopt(p)
    assert phase_of(w, "yd_ev_first_sighting") == "sighting"
    p = turn(w, wait(20))
    w = adopt(p)
    assert phase_of(w, "yd_ev_first_sighting") == "studied"
    assert any(s.get("exposes") == ["yd.info.q.early_first_sight"] for s in signals(p))


def test_every_legacy_quest_has_a_mapping_and_mapped_templates_exist():
    import sys
    sys.path.insert(0, os.path.join(ROOT, "handoff", "H07_YEONGDO_WORKBOOK"))
    try:
        import events_author as EA
    finally:
        sys.path.pop(0)
    quests = json.load(open(os.path.join(ROOT, "scenarios", "영도.quests.json"), encoding="utf-8"))["quests"]
    D, _ = _defn()
    assert sorted(EA.MAP) == sorted(q["id"] for q in quests)              # 무음 누락 0
    for qid, (disp, tids, note) in EA.MAP.items():
        assert disp in ("MIGRATED", "SPLIT", "MERGED", "RETAINED", "RETIRED"), qid
        assert note.strip(), qid
        assert (disp == "RETIRED") == (not tids), qid
        for tid in tids:
            assert tid in D["event_templates"], (qid, tid)
    review = open(os.path.join(ROOT, "handoff", "H07_YEONGDO_AUTHORING_REVIEW.md"), encoding="utf-8").read()
    assert all(f"| {q['id']} |" in review for q in quests)
