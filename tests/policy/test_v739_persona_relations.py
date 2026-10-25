# -*- coding: utf-8 -*-
"""v7.39 인물 체계 개편(계획서 v1 — B 데이터 구조 + C 관계·기억, 사용자 결정 N1 가·N2 조정·N3 가·N4 넣음).

N2(사용자 지시): 관계는 느리게 — 결정적 사건이면 한 턴에 바뀔 수 있고, 큰 변화량은 이어지지 않는다.
"""
from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

import core
from core import relationships as REL
from core import turn_world as TW
from core import world_refs as WR
from core.cache import NPC_PART_ALL, NPC_PART_EXTRA, NPC_PART_FIXED, npc_field_lines
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v734_prompt_assembly import ydyn  # noqa: F401
from tests.policy.test_h05bc_turn_wiring import extraction

pytestmark = pytest.mark.policy

YD = json.load(open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8"))
NPC, PC = "yd.npc.jin_bokrye", f"pc_{PLAYER_UID}"


def world(*deltas):
    key = REL.relation_key(f"actor:{NPC}", f"actor:{PC}")
    return SimpleNamespace(relationship_state={key: {"from_holder": f"actor:{NPC}", "to_holder": f"actor:{PC}",
                                                     "deltas": list(deltas)}} if deltas else {})


def trust(amount, at, kind=REL.KIND_TRUST):
    return REL.delta_record(f"d{at}{amount}", f"actor:{NPC}", f"actor:{PC}", kind, summary=str(amount), at_minute=at)


MET = REL.delta_record("met", f"actor:{NPC}", f"actor:{PC}", REL.KIND_MET, at_minute=0)


# ── N2: 느린 변화 · 결정적 사건 · 감쇠 ─────────────────────────────

def test_score_mapping_is_slow_with_decisive_extremes():
    assert [REL.base_change(s) for s in (50, 72, 90, 97, 25, 10, 3)] == [0, 1, 2, 5, -1, -2, -5]


def test_small_changes_capped_per_day_and_big_changes_do_not_repeat():
    w = world(MET, trust(2, 100))
    assert REL.trust_change(w, NPC, PC, 90, 110) == 1               # 직전 +2 → 이번 큰 변화는 +1
    w = world(MET, trust(2, 100), trust(1, 110))
    assert REL.trust_change(w, NPC, PC, 80, 120) == 0               # 하루 작은 변화 합 3 도달
    assert REL.trust_change(w, NPC, PC, 80, 100 + REL.DAY_MINUTES) == 1   # 다음 날은 다시 쌓인다
    assert REL.trust_change(w, NPC, PC, 20, 120) == -1              # 반대 방향은 따로 센다


def test_decisive_event_can_flip_stage_in_one_turn_but_only_once_a_day():
    w = world(MET)
    assert REL.trust_state(w, NPC, PC)["stage"] == "known"
    c = REL.trust_change(w, NPC, PC, 98, 10)
    assert c == 5
    w2 = world(MET, trust(c, 10))
    assert REL.trust_state(w2, NPC, PC)["stage"] == "trusted"        # 목숨을 구한 턴에 바로 믿는 사이
    assert REL.trust_change(w2, NPC, PC, 99, 20) == 1                # 같은 날 두 번째 결정적 사건 → ±2 → 직후 감쇠 ±1
    assert REL.trust_change(world(MET, trust(5, 10)), NPC, PC, 2, 20) == -5   # 배신은 별개(반대 방향)


def test_stage_needs_met_and_honours_initial_trust():
    assert REL.trust_state(world(), NPC, PC)["stage"] == "stranger"
    assert REL.trust_state(world(MET, trust(1, 1), trust(2, 2)), NPC, PC, initial_trust=2)["stage"] == "trusted"
    assert REL.trust_state(world(MET, trust(-2, 1)), NPC, PC, initial_trust=-2)["stage"] == "known"   # −4
    assert REL.trust_state(world(MET, trust(-5, 1)), NPC, PC)["stage"] == "hostile"


# ── 관찰 → 관계 연산 ───────────────────────────────────────────────

def _manifest():
    return TW.ObservationManifest(npcs=(("N1", NPC, "진복례"),))


def test_relation_ops_met_trust_memory_and_guards():
    raw = {"npc_reactions": [{"actor": "N1", "trust": 80, "memory": "물통을 나눠 준 외지인"},
                             {"actor": "N1", "trust": 10}, {"actor": "N9", "trust": 90}]}
    ops, diags = TW.relation_ops({}, world(), _manifest(), raw, PC, prefix="p", transaction_id="t1", at_minute=5)
    kinds = [o.payload["delta"]["kind"] for o in ops]
    assert kinds == ["MET", "TRUST", "MEMORY"] and ops[1].payload["delta"]["summary"] == "1"
    assert all(REL.validate_delta(o.payload["delta"]) == [] for o in ops)
    assert {"code": "NPC_REACTION_INVALID", "key": "N9"} in diags            # 장면 밖·후보 밖 인물
    long = {"npc_reactions": [{"actor": "N1", "trust": 50, "memory": "가" * 61}]}
    ops2, d2 = TW.relation_ops({}, world(MET), _manifest(), long, PC, prefix="p", transaction_id="t2", at_minute=5)
    assert ops2 == [] and d2[0]["code"] == "NPC_MEMORY_TOO_LONG"            # 자르지 않고 버린다


async def test_turn_commits_relation_and_next_prompts_use_stance_memory_and_stage(ydyn):
    r = ydyn
    r.prov.routes["extraction"] = [extraction({"npc_reactions": [
        {"actor": "N1", "trust": 80, "memory": "줄을 같이 당겨 준 외지인"}]})]
    await r.gm._process_actions(r.sess, "줄을 같이 당긴다", r.master)
    assert "N1=진복례" in r.prov.prompts["extraction"][-1][0]
    st = REL.trust_state(r.sess, NPC, PC)
    assert st == {"met": True, "trust": 1, "stage": "known", "memories": ["줄을 같이 당겨 준 외지인"]}
    await r.gm._process_actions(r.sess, "배를 살펴본다", r.master)
    narr = r.prov.prompts["narration"][-1][0]
    assert "반말로 일을 시키고" in narr and "줄을 같이 당겨 준 외지인" in narr          # 아는 사이 태도 · 기억
    assert "쓸모부터 잰다" not in narr                                          # 낯선 사람 태도는 빠진다
    judg = r.prov.prompts["judgment"][-1][0]
    assert "- 진복례: 아는 사이" in judg


# ── 데이터 구조 · 렌더 ─────────────────────────────────────────────

def test_persona_render_layers():
    tmpl, data = YD["npc_template"], YD["default_npcs"]["진복례"]
    fixed = "\n".join(npc_field_lines(tmpl, data, part=NPC_PART_FIXED))
    assert "첫인상: 바다에서 막 나온" in fixed and "대사 견본" not in fixed and "핵심 기질" not in fixed
    extra = "\n".join(npc_field_lines(tmpl, data, part=NPC_PART_EXTRA, bound=True))
    assert "「줄 쪽으로 오지 마라" in extra and "균열" in extra and "지금:" not in extra
    legacy = "\n".join(npc_field_lines(tmpl, data, part=NPC_PART_ALL))
    assert "지금: 목표 — 떠밀려 온 거룻배" in legacy                            # LEGACY는 기본 '지금'
    plain = {"name": "옛 인물", "행동 방식": "벽에 붙어 걷는다"}          # persona 없는 인물은 옛 렌더
    assert "행동 방식: 벽에 붙어 걷는다" in "\n".join(npc_field_lines(tmpl, plain, part=NPC_PART_EXTRA))


def test_persona_now_switches_by_date(ydyn):
    r = ydyn
    p = YD["default_npcs"]["진복례"]["persona"]
    D = core.player_world.load_definition(r.sess, r.sess.definition_revision)
    assert TW._persona_now(r.sess, D, p)["goal"].startswith("떠밀려 온 거룻배")
    r.sess.world_clock = dict(r.sess.world_clock, minute_index=r.sess.world_clock["minute_index"] + 3 * 1440)
    assert TW._persona_now(r.sess, D, p)["goal"].startswith("막내 해녀에게")          # 감염 은폐 제거 뒤 문안


def test_persona_compile_requires_core_but_not_dark_sides():
    raw = copy.deepcopy(YD)
    pr = raw["default_npcs"]["장우진"]["persona"]
    for k in ("crack", "fear", "rivals"):
        pr["core"].pop(k)                                          # 어두운 면·균열은 선택(사용자 지시)
    pr["stances"] = {"stranger": pr["stances"]["stranger"]}
    assert core.definition_compiler.compile_scenario(raw, "영도").ok
    pr["now"] = [{"when": {"const": True}, "goal": "x"}]
    pr["surface"].pop("lines")
    codes = {d.code for d in core.definition_compiler.compile_scenario(raw, "영도").errors}
    assert {"PERSONA_INVALID", "PERSONA_NOW_INVALID"} <= codes


def test_relation_stage_ref_validates_and_resolves():
    D = core.definition_compiler.compile_scenario(YD, "영도").definition
    scope = WR.RefScope(actors=set(D["actor_definitions"]), places=set(), infos=set(), controls=set(), modes=set())
    ref = {"ref": "RELATION_STAGE", "actor": NPC}
    assert WR.validate_ref(ref, scope, "p") == []
    assert WR.dependency_keys(ref) == [f"relationship:actor:{NPC}->actor:$session_pc"]
    w = world(MET)
    w.actor_registry = {PC: {"kind": "PC"}}
    assert WR.resolve(ref, WR.EvalContext(D, w)) == WR.known("known")


def test_master_report_shows_stage_transition():
    pre, post = world(MET), world(MET, trust(5, 10))
    lines = core.master_report.relation_lines({}, {"relationship_state": pre.relationship_state},
                                              {"relationship_state": post.relationship_state})
    assert len(lines) == 1 and "신뢰 +0 → +5" in lines[0] and "아는 사이 → 믿는 사이" in lines[0]
