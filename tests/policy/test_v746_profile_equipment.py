# -*- coding: utf-8 -*-
"""v7.46 — 영도 프로필 재저작(5스탯·스탯 구간 스킬·역할별 시작 물건) + 착용 아이템(입기·들기·벗기 → 장비 보정 E·상태 규칙).

사용자 결정: 스킬 범위 저작(A) · 스탯 구간마다 고를 수 · 착용은 혼합형(입거나 든 물건만 코드 E, 그 밖 도구는 M).
"""
from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

import core
from core import definition_compiler as dc
from core import items as IT
from core import profile_gen as PG
from core import profile_runner as R
from core import status_rules as SR

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
YD = json.loads((ROOT / "scenarios" / "영도.json").read_text(encoding="utf-8"))
DEFS = {"쇠파이프": {"wear": {"slots": ["오른손", "왼손"], "tags": ["근접무기"]}},
        "우비": {"wear": {"slots": ["겉옷"], "tags": ["방수"]}}}


def S(res=None, eq=None):
    return SimpleNamespace(resources=copy.deepcopy(res or {}), item_instances={}, place_items={}, item_facts={},
                           equipment=copy.deepcopy(eq or {}))


def run_ops(s, ops, narration=""):
    m = IT.build_manifest(s, pc_name="P", defs=DEFS)
    key = {r[2]: r[0] for r in m.holdings}
    raw = [dict(o, owner="O1", item=key.get(o.get("item"), o.get("item")), quantity=o.get("quantity", 1))
           for o in ops]
    p = IT.build_plan(s, m, raw, defs=DEFS, pc_name="P", transaction_id="tx", narration_text=narration)
    IT.apply_plan(s, p)
    return s


# ── 프로필 생성 ─────────────────────────────────────────────

def test_yeongdo_profile_reauthored_and_valid():
    assert YD["ability_stats"] == ["근력", "민첩", "지성", "감각", "정신"]
    assert len(YD["jobs"]) == 8 and PG.validate_steps(YD) == []
    tops = {j: v.get("top_stat") for j, v in YD["jobs"].items() if v.get("top_stat")}
    assert tops == {"소방관": "근력", "간호사": "지성", "정비공": "지성"}           # 꼭 필요한 직업에만
    assert all(set(n["stats"]) == set(YD["ability_stats"]) for n in YD["default_npcs"].values())
    assert dc.compile_scenario(YD, "영도").ok


def test_skill_count_follows_stat_band_and_job_skills_are_given():
    run = R.new_run(YD, prefill={"성별": "남", "이름": "a", "나이": "30", "직업": "어부",
                                 "능력치": {"근력": 7, "민첩": 8, "지성": 12, "감각": 16, "정신": 10}})
    seen = []
    r = R.step(YD, run)
    while r["type"] == R.ASK and r["field"] == "스킬":
        seen.append((r["message"].split("**")[1], r["multi"]))
        assert all(r["option_desc"][o] for o in r["options"])               # 쓰임 한 줄이 선택지에 보인다
        r = R.step(YD, run, r["options"][:r["multi"]])
    assert seen == [("민첩 8", 1), ("지성 12", 2), ("감각 16", 3), ("정신 10", 1)]   # 근력 7 → 0개(구간)
    assert r["type"] == R.CONFIRM and r["value"][:2] == ["배 몰기", "물고기 잡기"] and len(r["value"]) == 9


def test_role_groups_pick_one_each_and_go_to_items_only():
    run = R.new_run(YD, prefill={"성별": "남", "이름": "a", "나이": "30", "직업": "교사", "스킬": [],
                                 "능력치": {k: 10 for k in YD["ability_stats"]}})
    r = R.step(YD, run)
    picks = []
    while r["type"] == R.ASK and r["field"] == "기본 물건":
        assert r["multi"] == 1
        picks.append(r["options"][-1])
        r = R.step(YD, run, [r["options"][-1]])
    assert len(picks) == len(YD["starting_item_groups"]) and r["value"] == picks
    R.confirm(YD, run)
    items = PG.starting_items(YD, "교사", PG.granted_choices(YD, run["chosen"]))
    assert items["통조림"] == 3 and items["낡은 배낭"] == 1                  # 수량 저작 반영


def test_job_select_lists_personality_before_choice():
    run = R.new_run(YD, prefill={"성별": "남", "이름": "a", "나이": "30"})
    r = R.step(YD, run)
    assert r["field"] == "직업" and all(f"**{j}** — {v['desc']}" in r["message"] for j, v in YD["jobs"].items())


# ── 착용 ────────────────────────────────────────────────────

def test_wear_hold_remove_and_tags():
    s = run_ops(S({"P": {"쇠파이프": 1, "우비": 1, "패딩": 1}}),
                [{"op": "WEAR", "item": "쇠파이프", "slot": "오른손"},
                 {"op": "WEAR", "item": "우비", "slot": "겉옷", "tags": ["보온"]},       # 저작 태그가 권위(보온 무시)
                 {"op": "WEAR", "item": "패딩", "slot": "상의", "tags": ["보온", "방호(강)"]}])  # authored_only 제외
    eq = s.equipment["P"]
    assert IT.entry_tags(eq["겉옷"], DEFS) == ["방수"] and IT.entry_tags(eq["상의"], DEFS) == ["보온"]
    assert IT.worn_tags(s, "P", DEFS) == {"근접무기", "방수", "보온"}
    assert s.resources["P"]["패딩"] == 1                                          # 입어도 소지 수량 그대로
    run_ops(s, [{"op": "REMOVE", "item": "패딩"}])
    assert "상의" not in s.equipment["P"] and s.resources["P"]["패딩"] == 1       # 벗어 소지


def test_wear_rejects_unowned_wrong_slot_and_unknown_new():
    s = S({"P": {"쇠파이프": 1}})
    with pytest.raises(IT.ItemContractError):
        run_ops(s, [{"op": "WEAR", "item": "쇠파이프", "slot": "머리"}])              # 저작 자리 밖
    with pytest.raises(IT.ItemContractError):
        run_ops(s, [{"op": "WEAR", "item": "NEW", "name": "털모자", "slot": "머리"}], "아무것도 없다")


def test_new_item_picked_up_and_worn_same_turn():
    s = run_ops(S({"P": {}}), [{"op": "ACQUIRE", "item": "NEW", "name": "털모자"},
                               {"op": "WEAR", "item": "NEW", "name": "털모자", "slot": "머리", "tags": ["보온"]}],
                "선반에서 털모자를 집어 썼다")
    assert s.equipment["P"]["머리"]["key"] == "털모자" and IT.worn_tags(s, "P", DEFS) == {"보온"}


def test_dropping_or_damaging_worn_item_updates_equipment():
    s = run_ops(S({"P": {"쇠파이프": 1}}), [{"op": "WEAR", "item": "쇠파이프", "slot": "오른손"}])
    run_ops(s, [{"op": "DAMAGE", "item": "쇠파이프", "condition": "손상"}])
    assert s.equipment["P"]["오른손"]["key"] == "쇠파이프〈손상〉"                  # 같은 물건을 따라간다
    s2 = S({"P": {"쇠파이프": 1}}, {"P": {"오른손": {"key": "쇠파이프", "tags": []}}})
    m = IT.build_manifest(s2, pc_name="P", defs=DEFS)
    p = IT.build_plan(s2, m, [{"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}], defs=DEFS,
                      pc_name="P", transaction_id="t")
    IT.apply_plan(s2, p)
    assert s2.equipment == {}                                                    # 없어진 물건은 벗겨진다


def test_equip_rows_and_mod_feed_judgment_E():
    s = S({"P": {"쇠파이프": 1, "우비": 1}}, {"P": {"겉옷": {"key": "우비", "tags": []}}})
    rows = IT.equip_rows(s, "P", DEFS)
    assert [(r[0], r[2]) for r in rows] == [("E1", "쇠파이프")]                   # 효과 mod 없는 우비는 판정 후보 아님
    assert IT.equip_mod(rows[0][3], "근력") == 4 and IT.equip_mod(rows[0][3], "민첩") == 0
    assert core.judgment.target(0, 0, 0, 0, 0, 4) == core.judgment.BASE + 4
    assert "method_mod에 다시 넣지 말 것" in IT.equip_block(rows)


def test_judge_side_adds_equipment_for_pc_roll():
    from cogs.gm import _judge_side
    view = SimpleNamespace(players={"u": {"name": "P", "profile": {"근력": 10}}}, resources={"P": {"쇠파이프": 1}},
                           equipment={}, npcs={}, gm_target_char="P")
    wt = SimpleNamespace(ctx=SimpleNamespace(definition={"item_definitions": DEFS}))
    side = {"stat": "근력", "difficulty": "보통", "method_mod": 0, "status_mod": 0, "item": "E1"}
    out = _judge_side(view, side, "P", wt)
    assert out["E"] == 4 and out["T"] == core.judgment.BASE + 4 and "장비 쇠파이프" in out["breakdown"]
    assert _judge_side(view, dict(side, stat="민첩"), "P", wt)["E"] == 0          # 스탯이 맞지 않으면 0


def test_cold_rule_respects_warm_clothing_and_wetness():
    merged = core.get_merged_status_effects({})
    def decide(worn, active=()):
        ctx = SR.Ctx(now=100, start_minute=0, active=frozenset(active), worn=frozenset(worn))
        return SR.decide(merged, ctx, "P", [{"target": "P", "status": "추위"}], [], ())
    assert not [e for e in decide({"보온"}).apply if e.get("status") == "추위"]
    assert [e for e in decide({"보온"}, {"젖음"}).apply if e.get("status") == "추위"]
    assert [e for e in decide(set()).apply if e.get("status") == "추위"]          # 착용 기록 없는 시나리오 = 기존대로
    assert SR.check_condition({"wearing": "보온"}) == [] and SR.check_condition({"wearing": "없는태그"})


def test_compiler_validates_wear_and_keeps_other_revisions():
    bad = copy.deepcopy(YD)
    bad["item_definitions"]["쇠파이프"]["wear"] = {"slots": ["꼬리"], "tags": []}
    assert any(d.code == "ITEM_WEAR_INVALID" for d in dc.compile_scenario(bad, "영도").errors)


async def test_outfit_reading_validates_slots_and_tags(monkeypatch):
    from core import profile_ai

    async def fake(*a, **k):
        return True, {"items": [{"name": "검은 패딩", "slot": "겉옷", "tags": ["보온", "방호(강)"]},
                                {"name": "팬티", "slot": "하의", "tags": []},
                                {"name": "칼", "slot": "오른손", "tags": []},           # 손은 옷 자리가 아님
                                {"name": "모자[개]", "slot": "머리", "tags": []}]}       # 괄호 기호 거부
    monkeypatch.setattr(profile_ai, "_call", fake)
    out = await profile_ai.outfit(None, None, text="검은 패딩, 속옷")
    assert out == [{"name": "검은 패딩", "slot": "겉옷", "tags": ["보온"]}, {"name": "팬티", "slot": "하의", "tags": []}]
