# -*- coding: utf-8 -*-
"""v7.47 — 상태 단계·판정 불이익·처치(사용 효과)·물건 상태·염증 + 굴림 분야(W5·W6).

사용자 결정(계획서 v1): S1 단계 · S2 저작 불이익 코드 합산 · S3 추위 착용 차단 제거(추출 판단) · S4 처치 → 단계↓ → 시간 ·
S5 물건 상태(젖음·오염) 효과 차단 · S6 맞물림(허기·탈수 중 부상 자연 회복 정지 · 미소독 → 염증) · S8 동적 세계 PC만 ·
Q1 '염증' 신설 · Q2 진통·해열 = 일시 완화 · Q3 입은 옷 젖음은 PC 젖음 단계에서 파생 · 3.1 참고 묶음 이름 일치 폐지.
"""
from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

import core
from core import items as IT
from core import status_rules as SR

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
YD = json.loads((ROOT / "scenarios" / "영도.json").read_text(encoding="utf-8"))
M = core.get_merged_status_effects({})


def decide(active=(), clock=None, apply=(), clear=(), care=(), stage_in=None, now=1000, consumed=()):
    ctx = SR.Ctx(now=now, start_minute=0, active=frozenset(active), clock=copy.deepcopy(clock or {}),
                 care=frozenset(care), stage_in=dict(stage_in or {}), seed="seed", consumed=frozenset(consumed))
    return SR.decide(M, ctx, "P", [{"target": "P", "status": s, "score": 90} for s in apply],
                     [{"target": "P", "status": s, "score": 10} for s in clear], consumed)


def applied_names(d):
    return {e["status"] for e in d.apply}


def cleared_names(d):
    return {e["status"] for e in d.clear}


# ── 저작 ─────────────────────────────────────────────────────

def test_common_definitions_valid_and_bad_authoring_is_caught():
    assert SR.check_definitions(YD, core.get_merged_status_effects(YD)) == []
    assert M["염증"]["stages"] == ["국소", "전신"] and M["염증"]["deep"] is True
    assert M["부상"]["stages"] == ["경상", "중상", "위중"] and M["부상"]["lower_by"] == "care"
    assert core.get_merged_status_effects(YD)["감염"]["mods"] == {"전체": -8}
    bad = {"x": {"stages": ["a", "b"], "stage_mods": [{"근접": -1}], "care": {"없는효과": "down"}, "lower_by": "time"},
           "y": {"mods": {"없는분야": -2}, "latent": {"target": "x", "chance": 5, "onset_minutes": [1, 2],
                                                      "cancel_by": "없는효과"}}}
    codes = [c for c, _ in SR.check_definitions({}, bad)]
    assert codes.count("STATUS_STAGES") == 4 and "STATUS_LATENT" in codes


# ── S1 단계 · 추출 올림/내림 ─────────────────────────────────

def test_new_status_takes_extraction_stage_and_label_shows_it():
    d = decide(apply=["부상"], stage_in={"부상": 2})
    assert "부상" in applied_names(d) and d.clock["stage"] == {"부상": 2}
    assert SR.label(M, "부상", d.clock) == "부상(중상)"
    d2 = decide(apply=["지침"])
    assert "stage" not in d2.clock                                                    # 단계 없는 상태는 기록 없음


def test_extraction_may_raise_but_not_lower_care_only_status():
    clock = {"applied": {"부상": 900, "추위": 900}, "stage": {"부상": 2, "추위": 2}}
    d = decide(active=["부상", "추위"], clock=clock, stage_in={"부상": 1, "추위": 1})
    assert d.clock["stage"] == {"부상": 2, "추위": 1}                                  # 부상은 처치·시간으로만
    assert any("단계 내림 보류" in n for n in d.notes)
    d = decide(active=["부상"], clock=clock, stage_in={"부상": 3})
    assert d.clock["stage"]["부상"] == 3
    d = decide(active=["부상"], clock=clock, clear=["부상"])
    assert not d.clear and any("해제 보류" in n for n in d.notes)                    # 추출 해제도 거부


# ── S4 처치 ─────────────────────────────────────────────────

def test_bandage_lowers_bleeding_then_clears_it():
    clock = {"applied": {"출혈": 990}, "stage": {"출혈": 2}}
    d = decide(active=["출혈"], clock=clock, care={"지혈", "응급처치"})
    assert d.clock["stage"] == {"출혈": 1} and not d.clear
    d2 = decide(active=["출혈"], clock=d.clock, care={"지혈"}, now=1001)
    assert cleared_names(d2) == {"출혈"} and "stage" not in d2.clock


def test_first_aid_lowers_injury_and_time_finishes_it_unless_starving():
    clock = {"applied": {"부상": 0}, "stage": {"부상": 2}}
    d = decide(active=["부상"], clock=clock, care={"응급처치"}, now=100)
    assert d.clock["stage"] == {"부상": 1} and d.clock["cared"] == {"부상": 100}
    assert not decide(active=["부상"], clock=d.clock, now=100 + 719).clear
    assert cleared_names(decide(active=["부상"], clock=d.clock, now=100 + 720)) == {"부상"}   # 처치 뒤 12시간
    light = {"applied": {"부상": 0, "허기": 0}, "stage": {"부상": 1}}
    assert not decide(active=["부상", "허기"], clock=light, now=4400).clear              # S6: 허기 중 자연 회복 정지
    assert "부상" in cleared_names(decide(active=["부상"], clock=light, now=4400))       # 3일 자연 회복
    heavy = {"applied": {"부상": 0}, "stage": {"부상": 2}}
    assert not decide(active=["부상"], clock=heavy, now=99999).clear                    # 중상은 처치 없이 낫지 않음


def test_painkiller_eases_penalty_for_four_hours_only():
    clock = {"applied": {"부상": 0}, "stage": {"부상": 2}}
    d = decide(active=["부상"], clock=clock, care={"진통"}, now=100)
    assert d.clock["eased"] == {"부상": 100 + SR.EASE_MINUTES} and d.clock["stage"] == {"부상": 2}
    assert SR.status_mod(M, ["부상"], d.clock, 101, "근접") == (-2, "부상(중상·완화) -2")   # 경상 값으로
    assert SR.status_mod(M, ["부상"], d.clock, 100 + SR.EASE_MINUTES, "근접")[0] == -6   # 끝나면 원래대로


def test_warming_lowers_cold_and_effect_intake_counts_as_water():
    d = decide(active=["추위"], clock={"applied": {"추위": 0}, "stage": {"추위": 2}}, care={"덥히기"})
    assert d.clock["stage"] == {"추위": 1}
    d = decide(active=["탈수"], clock={"applied": {"탈수": 0}}, consumed={"water"})
    assert cleared_names(d) == {"탈수"}


# ── Q1 염증 · 잠복 취소 · 악화 · 항생 ──────────────────────────

def _infected_seed():
    lat = M["출혈"]["latent"]
    for i in range(400):
        if SR.latent_roll(f"s{i}", "P", "출혈", 0, lat)["due"] is not None:
            return f"s{i}"
    raise AssertionError("발현 시드 없음")


def test_untreated_wound_becomes_inflammation_unless_disinfected():
    seed = _infected_seed()
    ctx = SR.Ctx(now=0, start_minute=0, seed=seed)
    d = SR.decide(M, ctx, "P", [{"target": "P", "status": "출혈", "score": 90}], [], ())
    due = d.clock["latent"]["염증"]["due"]
    assert due is not None
    # 소독하면 발현 전 잠복 취소
    c = SR.decide(M, SR.Ctx(now=10, start_minute=0, seed=seed, active=frozenset({"출혈"}), clock=d.clock,
                            care=frozenset({"소독"})), "P", [], [], ())
    assert "latent" not in c.clock and any("잠복 취소" in n for n in c.notes)
    # 처치 없이 발현 → 국소, 24시간 방치 → 전신, 항생 → 국소 + 처치, 24시간 뒤 해제
    e = SR.decide(M, SR.Ctx(now=due, start_minute=0, seed=seed, clock=d.clock), "P", [], [], ())
    assert "염증" in applied_names(e) and e.clock["stage"]["염증"] == 1
    act = frozenset({"염증"})
    f = SR.decide(M, SR.Ctx(now=due + 1440, start_minute=0, seed=seed, active=act, clock=e.clock), "P", [], [], ())
    assert f.clock["stage"]["염증"] == 2
    g = SR.decide(M, SR.Ctx(now=due + 1500, start_minute=0, seed=seed, active=act, clock=f.clock,
                            care=frozenset({"항생"})), "P", [], [], ())
    assert g.clock["stage"]["염증"] == 1 and g.clock["cared"]["염증"] == due + 1500
    assert not SR.decide(M, SR.Ctx(now=due + 2000, start_minute=0, active=act, clock=g.clock), "P", [], [], ()).clear
    h = SR.decide(M, SR.Ctx(now=due + 1500 + 1440, start_minute=0, active=act, clock=g.clock), "P", [], [], ())
    assert cleared_names(h) == {"염증"}


def test_light_injury_does_not_roll_inflammation_but_serious_does():
    assert "latent" not in decide(apply=["부상"], stage_in={"부상": 1}).clock
    d = decide(apply=["부상"], stage_in={"부상": 2})
    assert "염증" in d.clock["latent"]


# ── S2 판정 불이익 · W5·W6 굴림 분야 ──────────────────────────

def test_status_penalty_sums_all_plus_domain_and_is_clamped():
    clock = {"stage": {"부상": 2}}
    assert SR.status_mod(M, ["부상"], clock, 0, "근접")[0] == -6
    assert SR.status_mod(M, ["부상"], clock, 0, "기타")[0] == -3
    assert SR.status_mod(M, ["부상", "저체온", "출혈"], {"stage": {"부상": 3, "출혈": 2}}, 0, "기타")[0] == -15


def _view():
    return SimpleNamespace(
        players={"u": {"name": "P", "profile": {"근력": 10, "민첩": 10}}},
        resources={"P": {"쇠파이프": 1, "장화": 1}}, equipment={"P": {"발": {"key": "장화"}}},
        item_traits={"장화": {"slots": ["발"], "tags": ["방수", "무거운 차림"], "use": []}},
        npcs={}, gm_target_char="P", scenario_data={}, statuses={"P": ["부상"]},
        status_clock={"P": {"stage": {"부상": 2}}}, world_clock={"minute_index": 10})


def test_judge_side_uses_roll_domain_for_equipment_and_code_status_penalty():
    import cogs.gm as G
    wt = SimpleNamespace(ctx=SimpleNamespace(definition={"item_definitions": {
        "쇠파이프": {"wear": {"slots": ["오른손", "왼손"], "tags": ["근접무기"]}}}}))
    side = {"stat": "근력", "difficulty": "보통", "method_mod": 0, "status_mod": 7, "item": "E2", "domain": "근접"}
    assert [r[2] for r in IT.equip_rows(_view(), "P", IT.definitions_of(wt.ctx.definition))] == ["장화", "쇠파이프"]
    hit = G._judge_side(_view(), side, "P", wt)
    assert hit["E"] == 4 and hit["H"] == -6 and "코드 확정" in hit["status_basis"]     # 판단층위 status_mod 무시
    guard = G._judge_side(_view(), dict(side, domain="방어"), "P", wt)
    assert guard["E"] == 0                                                         # 공격 무기를 막기에 지목해도 0
    run = G._judge_side(_view(), dict(side, stat="민첩", item="NONE", domain="기동"), "P", wt)
    assert run["E"] == -2 and "장화(-)" in run["item"] and run["H"] == -6          # 장화 불리는 지목 없이 자동
    legacy = G._judge_side(_view(), {k: v for k, v in side.items() if k != "domain"}, "P", wt)
    assert legacy["H"] == 7 and legacy["E"] == 4                                   # 분야 없는 옛 계약은 그대로


# ── S5·Q3 물건 상태 · 젖은 몸 ─────────────────────────────────

TR = {"성냥": {"slots": [], "tags": [], "use": ["덥히기"]},
      "소독약": {"slots": [], "tags": [], "use": ["소독"]},
      "붕대": {"slots": [], "tags": [], "use": ["지혈", "응급처치"]},
      "패딩": {"slots": ["겉옷"], "tags": ["보온"], "use": []},
      "방한 우의": {"slots": ["겉옷"], "tags": ["보온", "방수"], "use": []}}


def _sim(res, ops, eq=None, statuses=None, clock=None, owners=("P", "N")):
    s = SimpleNamespace(resources=copy.deepcopy(res), item_instances={}, place_items={}, item_facts={},
                        equipment=copy.deepcopy(eq or {}), item_traits=copy.deepcopy(TR),
                        statuses=statuses or {}, status_clock=clock or {})
    m = IT.build_manifest(s, pc_name="P", scene_owner_names=owners[1:])
    key = {(r[1], r[2]): r[0] for r in m.holdings}
    raw = [dict(o, owner="O1", item=key.get(("P", o["item"]), o["item"]), quantity=1) for o in ops]
    p = IT.build_plan(s, m, raw, pc_name="P", transaction_id="t")
    return s, IT.run_ops(s, p.ops, pc_name="P", scene_place=None, transaction_id="t")


def test_use_records_care_for_target_and_failure_and_condition_blocks():
    _s, sim = _sim({"P": {"붕대": 2, "성냥〈젖음〉": 1, "소독약〈오염〉": 1}, "N": {}},
                   [{"op": "USE", "item": "붕대"}, {"op": "CONSUME", "item": "붕대", "on": "O2"},
                    {"op": "CONSUME", "item": "성냥〈젖음〉"}, {"op": "USE", "item": "소독약〈오염〉", "failed": True}])
    care = [(c["kind"], c["effects"], c["on"], c["failed"]) for c in sim.care]
    assert care == [("붕대", ["지혈", "응급처치"], "P", False), ("붕대", ["지혈", "응급처치"], "N", False)]
    # 젖은 성냥(덥히기)·오염된 소독약(소독)은 효과가 꺼져 처치 기록 자체가 없다


def test_soaked_body_turns_off_warmth_except_waterproof_gear():
    eq = {"P": {"겉옷": {"key": "패딩"}}}
    s = SimpleNamespace(equipment=eq, item_traits=TR, statuses={"P": ["젖음"]}, status_clock={"P": {"stage": {"젖음": 2}}})
    assert IT.worn_tags(s, "P") == frozenset()
    s.status_clock = {"P": {"stage": {"젖음": 1}}}
    assert IT.worn_tags(s, "P") == {"보온"}                                         # 축축함은 그대로
    s.status_clock, s.equipment = {"P": {"stage": {"젖음": 2}}}, {"P": {"겉옷": {"key": "방한 우의"}}}
    assert IT.worn_tags(s, "P") == {"보온", "방수"}                                   # 방수 소재는 보온 유지
    m = IT.build_manifest(SimpleNamespace(resources={"P": {"패딩": 1}}, item_instances={}, place_items={},
                                          equipment=eq, item_traits=TR, statuses={"P": ["젖음"]},
                                          status_clock={"P": {"stage": {"젖음": 2}}}), pc_name="P")
    assert "보온(젖거나 더러워 꺼짐)" in m.render()


def test_reference_name_matching_is_abolished_and_compiler_validates_use():
    assert IT.traits_of("검은 롱패딩") == (None, None) and not hasattr(IT, "reference_of")
    D = core.definition_compiler.compile_scenario(YD, "영도").definition
    assert IT.use_of("붕대", IT.definitions_of(D)) == ["지혈", "응급처치"]
    bad = copy.deepcopy(YD)
    bad["item_definitions"]["붕대"]["use"] = ["치유"]
    assert any(d.code == "ITEM_USE_INVALID" for d in core.definition_compiler.compile_scenario(bad, "영도").errors)


# ── 통합 — 실제 턴(추출 → READY → 커밋)과 되감기 ─────────────────

from tests.policy.test_h05bc_turn_wiring import (  # noqa: E402,F401 — 픽스처 재사용
    H05Provider, drig, extraction, judgment, turn)
from tests.policy.test_ready_barrier import _json, rig  # noqa: E402,F401


async def test_bandage_and_water_treat_through_turn_and_rewind(drig):
    r = drig
    r.make()
    now = r.sess.world_clock["minute_index"]
    r.sess.resources = {"테스터": {"붕대": 1, "생수": 2}}
    r.sess.item_traits = {k: TR[k] for k in ("붕대",)}
    r.sess.statuses = {"테스터": ["탈수", "출혈"]}
    r.sess.status_clock = {"테스터": {"applied": {"탈수": now, "출혈": now}, "stage": {"출혈": 2}}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "숨을 고른다")
    assert r.sess.status_clock["테스터"]["stage"] == {"출혈": 2}
    scores = [{"target": "테스터", "status": "탈수", "score": 50, "stage": 0},
              {"target": "테스터", "status": "출혈", "score": 50, "stage": 2}]
    r.prov.routes["extraction"] = [extraction({
        "item_ops": [{"op": "USE", "owner": "O1", "item": "H1", "quantity": 1},
                     {"op": "CONSUME", "owner": "O1", "item": "H2", "quantity": 1}],
        "item_traits": [{"item": "H2", "slots": [], "tags": [], "use": ["수분 보충"]}]}, status_scores=scores)]
    tx = await turn(r, "붕대를 감고 물을 마신다")
    assert tx.status == core.turn_transaction.TurnStatus.COMMITTED
    assert r.sess.statuses["테스터"] == ["출혈"]                                      # 수분 보충 → 탈수 해제
    assert r.sess.status_clock["테스터"]["stage"] == {"출혈": 1}                      # 지혈 → 한 단계
    assert r.sess.item_traits["생수"]["use"] == ["수분 보충"]                          # 처음 다룬 턴에 정해짐
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert sorted(r.sess.statuses["테스터"]) == ["출혈", "탈수"]
    assert r.sess.status_clock["테스터"]["stage"] == {"출혈": 2} and "생수" not in r.sess.item_traits


async def test_judgment_schema_has_roll_domain_and_code_status_line(drig):
    r = drig
    r.make()
    r.sess.statuses = {"테스터": ["부상"]}
    r.sess.status_clock = {"테스터": {"applied": {"부상": 0}, "stage": {"부상": 2}}}
    seen = {}
    base = judgment()

    def route(kwargs):
        seen["schema"] = kwargs["config"].response_schema
        seen["text"] = H05Provider._text(kwargs)
        return base(kwargs)
    r.prov.routes["judgment"] = route
    r.prov.routes["extraction"] = [extraction({}, status_scores=[
        {"target": "테스터", "status": "부상", "score": 50, "stage": 2}])]
    await turn(r, "주변을 살핀다")
    items = seen["schema"]["properties"]["rolls"]["items"]
    assert "domain" in items["required"] and "기타" in items["properties"]["domain"]["enum"]
    assert "부상(중상): 전체 -3, 근접 -3, 기동 -3" in seen["text"]


def test_same_turn_wound_and_treatment():
    # 병합 전 시뮬레이션 발견: 같은 턴에 다치고 바로 처치하면 처치·잠복 취소가 새로 붙은 상태에도 적용된다
    lat = M["출혈"]["latent"]
    seed = next(f"s{i}" for i in range(400) if SR.latent_roll(f"s{i}", "P", "출혈", 100, lat)["due"] is not None)
    ctx = SR.Ctx(now=100, start_minute=0, seed=seed, care=frozenset({"소독", "지혈"}), stage_in={"출혈": 2})
    d = SR.decide(M, ctx, "P", [{"target": "P", "status": "출혈", "score": 90}], [], ())
    assert applied_names(d) == {"출혈"} and d.clock["stage"] == {"출혈": 1} and "latent" not in d.clock
    ctx = SR.Ctx(now=100, start_minute=0, seed=seed, care=frozenset({"지혈"}))
    d = SR.decide(M, ctx, "P", [{"target": "P", "status": "출혈", "score": 90}], [], ())
    assert not d.apply and "염증" in d.clock["latent"]          # 바로 멎은 가벼운 출혈 — 소독 안 했으면 잠복은 남음


def test_worn_body_gear_applies_automatically_hand_items_by_choice():
    # D2 나: 몸에 입은 것은 유리·불리 모두 자동, 손에 든 것은 불리만 자동(유리는 지목)
    import cogs.gm as G
    v = SimpleNamespace(
        players={"u": {"name": "P", "profile": {"근력": 10}}},
        resources={"P": {"안전모": 1, "쇠파이프": 1, "확성기": 1}},
        equipment={"P": {"머리": {"key": "안전모"}, "오른손": {"key": "쇠파이프"}, "왼손": {"key": "확성기"}}},
        item_traits={"안전모": {"slots": ["머리"], "tags": ["방호"], "use": []},
                     "확성기": {"slots": ["왼손"], "tags": ["소음"], "use": []}},
        npcs={}, gm_target_char="P", scenario_data={}, statuses={}, status_clock={}, world_clock={"minute_index": 1})
    wt = SimpleNamespace(ctx=SimpleNamespace(definition={"item_definitions": {
        "쇠파이프": {"wear": {"slots": ["오른손", "왼손"], "tags": ["근접무기"]}}}}))
    rows = {r[2]: r[0] for r in IT.equip_rows(v, "P", IT.definitions_of(wt.ctx.definition))}
    base = {"stat": "근력", "difficulty": "보통", "method_mod": 0, "status_mod": 0}
    guard = G._judge_side(v, dict(base, item="NONE", domain="방어"), "P", wt)
    assert guard["E"] == 3 and "안전모(+)" in guard["item"]                        # 지목 없이 헬멧 방호
    twice = G._judge_side(v, dict(base, item=rows["안전모"], domain="방어"), "P", wt)
    assert twice["E"] == 3                                                         # 지목해도 두 번 더하지 않음
    hit = G._judge_side(v, dict(base, item="NONE", domain="근접"), "P", wt)
    assert hit["E"] == 0                                                           # 손에 든 무기는 지목해야
    sneak = G._judge_side(v, dict(base, item="NONE", domain="은밀"), "P", wt)
    assert sneak["E"] == -3 and "확성기(-)" in sneak["item"]                        # 손에 든 것의 불리는 자동


def test_scene_npc_items_get_traits_when_handled_and_count_in_contest():
    # D1 나: 장면 인물의 효과 미정 물건도 처음 다룰 때 추출이 정한다 → 대결 보정
    import cogs.gm as G
    s = SimpleNamespace(resources={"P": {}, "깡패": {"각목": 1}}, item_instances={}, place_items={}, item_facts={},
                        equipment={}, item_traits={}, statuses={}, status_clock={})
    m = IT.build_manifest(s, pc_name="P", scene_owner_names=["깡패"])
    key = next(r[0] for r in m.holdings if r[1] == "깡패")
    assert f"{key}=각목" in m.unknown and key in m.traits_schema()["items"]["properties"]["item"]["enum"]
    p = IT.build_plan(s, m, [], pc_name="P", transaction_id="t",
                      raw_traits=[{"item": key, "slots": ["오른손", "왼손"], "tags": ["근접무기"], "use": []}])
    IT.apply_plan(s, p)
    assert s.item_traits["각목"]["tags"] == ["근접무기"]
    v = SimpleNamespace(players={}, resources=s.resources, equipment={}, item_traits=s.item_traits, npcs={},
                        gm_target_char="P", scenario_data={}, statuses={}, status_clock={}, world_clock={})
    wt = SimpleNamespace(ctx=SimpleNamespace(definition={"item_definitions": {}}))
    import unittest.mock as um
    with um.patch.object(G, "_npc_stats", lambda *a, **k: {"근력": 10}):
        n = G._judge_side(v, {"stat": "근력", "difficulty": "보통", "method_mod": 0, "status_mod": 0, "domain": "근접"},
                          "깡패", wt, contest=True)
    assert n["E"] == 4
