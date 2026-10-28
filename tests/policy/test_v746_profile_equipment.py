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

def test_wear_traits_follow_authored_session_reference_then_extraction():
    s = run_ops(S({"P": {"쇠파이프": 1, "우비": 1, "검은 롱패딩": 1, "이상한 망토": 1}}),
                [{"op": "WEAR", "item": "쇠파이프", "slot": "오른손"},
                 {"op": "WEAR", "item": "우비", "slot": "겉옷", "tags": ["보온"]},              # 저작 태그가 권위
                 {"op": "WEAR", "item": "검은 롱패딩", "slot": "상의", "tags": ["방수"]},       # 참고 묶음(두꺼운 외투)
                 {"op": "WEAR", "item": "이상한 망토", "slot": "머리", "tags": ["은폐", "방호(강)"]}])  # 추출 — curated 제외
    tr = s.item_traits
    assert "우비" not in tr and "쇠파이프" not in tr                               # 저작 물건은 기록하지 않는다
    assert tr["검은 롱패딩"]["tags"] == ["보온"] and tr["검은 롱패딩"]["source"] == "REFERENCE"
    assert tr["이상한 망토"] == {"slots": ["머리"], "tags": ["은폐"], "source": "EXTRACTION"}
    assert IT.worn_tags(s, "P", DEFS) == {"근접무기", "방수", "보온", "은폐"}
    assert s.resources["P"]["이상한 망토"] == 1                                    # 입어도 소지 수량 그대로
    run_ops(s, [{"op": "REMOVE", "item": "이상한 망토"}])
    run_ops(s, [{"op": "WEAR", "item": "이상한 망토", "slot": "겉옷", "tags": ["보온"]}])  # 이미 정해짐 — 새 태그 무시
    assert IT.key_tags("이상한 망토", DEFS, s.item_traits) == ["은폐"]


def test_wear_rejects_unowned_wrong_authored_slot_and_unknown_new():
    s = S({"P": {"쇠파이프": 1}})
    with pytest.raises(IT.ItemContractError):
        run_ops(s, [{"op": "WEAR", "item": "쇠파이프", "slot": "머리"}])              # 저작 자리 밖
    with pytest.raises(IT.ItemContractError):
        run_ops(s, [{"op": "WEAR", "item": "NEW", "name": "털모자", "slot": "머리"}], "아무것도 없다")


def test_new_item_picked_up_and_worn_same_turn():
    s = run_ops(S({"P": {}}), [{"op": "ACQUIRE", "item": "NEW", "name": "털모자"},
                               {"op": "WEAR", "item": "NEW", "name": "털모자", "slot": "머리"}],
                "선반에서 털모자를 집어 썼다")
    assert s.equipment["P"]["머리"]["key"] == "털모자" and IT.worn_tags(s, "P", DEFS) == {"보온"}


def test_dropping_or_damaging_worn_item_updates_equipment():
    s = run_ops(S({"P": {"쇠파이프": 1}}), [{"op": "WEAR", "item": "쇠파이프", "slot": "오른손"}])
    run_ops(s, [{"op": "DAMAGE", "item": "쇠파이프", "condition": "손상"}])
    assert s.equipment["P"]["오른손"]["key"] == "쇠파이프〈손상〉"                  # 같은 물건을 따라간다
    s2 = S({"P": {"쇠파이프": 1}}, {"P": {"오른손": {"key": "쇠파이프"}}})
    m = IT.build_manifest(s2, pc_name="P", defs=DEFS)
    p = IT.build_plan(s2, m, [{"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}], defs=DEFS,
                      pc_name="P", transaction_id="t")
    IT.apply_plan(s2, p)
    assert s2.equipment == {}                                                    # 없어진 물건은 벗겨진다


def test_manifest_tells_extraction_known_effects_and_references():
    s = S({"P": {"검은 패딩": 1, "수상한 상자": 1}})
    text = IT.build_manifest(s, pc_name="P", defs=DEFS).render()
    assert "검은 패딩: 보온" in text and "수상한 상자:" not in text.split("효과가 이미 정해진")[1].split("\n")[0]
    assert "두꺼운 외투(패딩·파카·코트)=보온" in text


def test_equip_rows_and_domain_mod_across_scenarios():
    s = S({"P": {"쇠파이프": 1, "우비": 1, "손전등": 1}}, {"P": {"겉옷": {"key": "우비"}}})
    rows = IT.equip_rows(s, "P", DEFS)
    assert [(r[0], r[2]) for r in rows] == [("E1", "손전등"), ("E2", "쇠파이프")]   # 효과 mod 없는 우비는 후보 아님
    assert IT.equip_mod(rows[1][3], "근력") == 4 and IT.equip_mod(rows[1][3], "민첩") == 0
    assert IT.equip_mod(rows[0][3], "감각") == 3 and IT.equip_mod(rows[0][3], "관찰") == 3   # 설화재 스탯도 같은 분야
    assert IT.equip_mod(rows[1][3], "무공") == 4                                            # 무협 스탯
    assert IT.equip_mod(rows[1][3], "근력", {"item_effect_domains": {"근접": ["힘"]}}) == 0  # 시나리오가 덮어씀
    assert core.judgment.target(0, 0, 0, 0, 0, 4) == core.judgment.BASE + 4
    assert "method_mod에 다시 넣지 말 것" in IT.equip_block(rows)


def _view(eq=None, npc_res=None):
    return SimpleNamespace(players={"u": {"name": "P", "profile": {"근력": 10}}},
                           resources={"P": {"쇠파이프": 1}, **(npc_res or {})}, equipment=eq or {}, item_traits={},
                           npcs={}, gm_target_char="P", scenario_data={})


def test_judge_side_adds_equipment_for_pc_and_contest_npc(monkeypatch):
    import cogs.gm as G
    wt = SimpleNamespace(ctx=SimpleNamespace(definition={"item_definitions": DEFS}))
    side = {"stat": "근력", "difficulty": "보통", "method_mod": 0, "status_mod": 0, "item": "E1"}
    out = G._judge_side(_view(), side, "P", wt)
    assert out["E"] == 4 and out["T"] == core.judgment.BASE + 4 and "장비 쇠파이프" in out["breakdown"]
    assert G._judge_side(_view(), dict(side, stat="민첩"), "P", wt)["E"] == 0       # 스탯이 맞지 않으면 0
    monkeypatch.setattr(G, "_npc_stats", lambda *a, **k: {"근력": 10})
    v = _view(npc_res={"깡패": {"각목": 1, "손전등": 1}})
    assert G._judge_side(v, dict(side, item=None), "깡패", wt, contest=True)["E"] == 4   # 대결 상대 — 둔기
    assert G._judge_side(v, dict(side, item=None), "깡패", wt)["E"] == 0                # 대결이 아니면 0


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


# ── A1·A2 저장 프로필·최종 검토 ─────────────────────────────

def test_old_saved_profile_values_are_rejected_by_content():
    steps = {s["field"]: s for s in YD["profile_creation"]}
    chosen = {"직업": "어부", "능력치": {"근력": 7, "민첩": 8, "지성": 12, "감각": 16, "정신": 10}}
    assert not R.expected_type_ok(steps["능력치"], {"근력": 10, "민첩": 10, "기술": 10}, YD, chosen)   # 옛 스탯
    assert not R.expected_type_ok(steps["직업"], "약사", YD, chosen)                                   # 없는 직업
    assert R.expected_type_ok(steps["직업"], "어부", YD, chosen)
    good = ["배 몰기", "물고기 잡기", "은신", "응급 처치", "제작", "수색", "경계", "추적", "설득"]
    assert R.expected_type_ok(steps["스킬"], good, YD, chosen)
    assert not R.expected_type_ok(steps["스킬"], ["매듭·로프", "근력 작업"], YD, chosen)                 # 옛 스킬
    assert not R.expected_type_ok(steps["스킬"], good[:-1], YD, chosen)                                # 구간 개수 부족


def test_editing_job_or_stats_clears_skills():
    assert set(R.dependents_of(YD, "직업")) >= {"스킬", "직업 물건"}
    assert "스킬" in R.dependents_of(YD, "능력치")


# ── A3·A4·C2·C4 ─────────────────────────────────────────────

def test_briefing_and_prompts_follow_equipment():
    from core import start_frame as SF
    beats = SF.briefing_beats({"briefing_formats": {"identity": ["{스킬}뿐"]}}, {"스킬": "가, 나, 다, 라, 마"})
    assert beats[0] == "가, 나, 다 외 2가지뿐"
    s = SimpleNamespace(equipment={"P": {"겉옷": {"key": "검은 패딩"}}}, scenario_data=YD,
                        resources={"P": {"검은 패딩": 1, "물통": 1}})
    prof = IT.prompt_profile(s, "P", {"복장": "검은 패딩, 청바지", "직업": "어부"})
    assert prof["복장"].startswith("(처음 차림") and prof["직업"] == "어부"
    assert IT.prompt_profile(SimpleNamespace(equipment={}, scenario_data=YD), "P", {"복장": "x"})["복장"] == "x"
    from core import display_board as DB
    assert DB._resource_chips(s, "P", s.resources["P"]) == ["검은 패딩 1(착용)", "물통 1"]


def test_briefing_items_exclude_worn_clothes():
    from cogs import session as S_
    src = pathlib.Path(S_.__file__).read_text(encoding="utf-8")
    assert "입은 옷은 차림 박자로" in src


# ── B2 결말 표시 ────────────────────────────────────────────

def test_endings_show_in_personal_stats():
    from core import gm_board as GB
    st = {"endings": {"설화재": {"solved_syj": 2, "wrong": 1}}}
    assert dict(GB.stat_items(st))["본 결말"] == "2종 (3회)"
    assert "본 결말 2종" in core.stats.format_summary(st)


# ── D 통합 — 착용이 실제 턴(추출 → READY → 커밋)을 지나고 되감긴다 · 판단 스키마 열거 ──

from tests.policy.test_h05bc_turn_wiring import (  # noqa: E402,F401 — 픽스처 재사용
    H05Provider, drig, extraction, judgment, turn)
from tests.policy.test_ready_barrier import _json, rig  # noqa: E402,F401


async def test_wear_commits_through_turn_and_rewinds(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"검은 패딩": 1}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "숨을 고른다")
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "WEAR", "owner": "O1", "item": "H1", "quantity": 1, "slot": "겉옷", "tags": ["방수"]}]})]
    tx = await turn(r, "패딩을 입는다")
    assert tx.status == core.turn_transaction.TurnStatus.COMMITTED
    assert r.sess.equipment == {"테스터": {"겉옷": {"key": "검은 패딩"}}}
    assert r.sess.item_traits["검은 패딩"]["tags"] == ["보온"]                       # 참고 묶음이 추출 태그보다 우선
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert r.sess.equipment == {} and r.sess.item_traits == {}


async def test_judgment_schema_lists_only_owned_skills_and_equipment(drig):
    r = drig
    r.make()
    pc = next(iter(r.sess.players.values()))
    pc["profile"]["스킬"] = "수색, 은신"
    r.sess.resources = {pc["name"]: {"쇠파이프": 1}}
    seen = {}
    base = judgment()

    def route(kwargs):
        seen["schema"] = kwargs["config"].response_schema
        seen["text"] = H05Provider._text(kwargs)
        return base(kwargs)
    r.prov.routes["judgment"] = route
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "주변을 살핀다")
    rp = seen["schema"]["properties"]["rolls"]["items"]["properties"]
    assert rp["skill"]["enum"] == ["NONE", "수색", "은신"]
    assert rp["item"]["enum"] == ["NONE", "E1"] and "E1=쇠파이프" in seen["text"]


async def test_finish_reads_outfit_into_items_and_equipment(monkeypatch):
    from core import profile_ai
    from core import profile_creation_ui as PCU

    async def fake_outfit(bot, session, *, text):
        return [{"name": "검은 패딩", "slot": "겉옷", "tags": []}, {"name": "기묘한 두건", "slot": "머리", "tags": ["은폐"]}]
    monkeypatch.setattr(profile_ai, "outfit", fake_outfit)
    sess = SimpleNamespace(players={}, scenario_data=YD, resources={}, equipment={}, item_traits={},
                           session_id="", profile_creation_progress={})
    state = SimpleNamespace(session=sess, uid="u", char_name="a", bot=None,
                            run={"chosen": {"이름": "a", "직업": "교사", "복장": "검은 패딩, 기묘한 두건", "기본 물건": [],
                                            "직업 물건": []}, "history": [], "index": 0, "pending": None, "notes": []})
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(PCU, "_show_save", _noop)
    monkeypatch.setattr(PCU, "_persistent", lambda s: True)
    await PCU.finish(state, None)
    assert sess.resources["a"]["검은 패딩"] == 1 and sess.equipment["a"]["머리"] == {"key": "기묘한 두건"}
    assert sess.item_traits["검은 패딩"]["tags"] == ["보온"] and sess.item_traits["기묘한 두건"]["source"] == "OUTFIT"


def test_tool_items_count_as_tool_capability_and_scenario_follow_ups():
    D = dc.compile_scenario(YD, "영도").definition
    assert "공구" in IT.capabilities({"멀티툴": 1}, IT.definitions_of(D))           # B1 남항 교역 '공구'
    assert "항생제[알]" not in YD["starting_item_choices"]["직업"]["간호사"]           # C6
    assert "해열제" in YD["intake_groups"]["medical"]
    assert "5(해당 분야에 매우 취약함)에서 최대 15" not in YD["stat_system"]           # C3
    SH = json.loads((ROOT / "scenarios" / "설화재.json").read_text(encoding="utf-8"))   # B4
    assert PG.skill_note(SH, "현장 감식") and any((s.get("args") or {}).get("wear_items") for s in SH["profile_creation"])
    assert IT.equip_mod(IT.key_tags("손전등"), "관찰") == 3                           # 설화재 스탯으로 장비 보정
