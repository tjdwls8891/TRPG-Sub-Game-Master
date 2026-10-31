# -*- coding: utf-8 -*-
"""v7.34 캐시·프롬프트 조립 정합(전수조사 v1 — 사용자 결정 1 승인 · 2 '가' · 3 완화 · 4 NPC 고정/추가 분리 · 5 정리).

실제 영도 정의로 genesis를 거쳐 OPEN한 DYNAMIC 세션에서 한 턴을 돌려, 조립된 캐시 본문과 층위별 입력을 검사한다.
"""
from __future__ import annotations

import json
import os

import pytest

import core
from core import cache as CA
from core import prompt_census as PC
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h05bc_turn_wiring import H05Provider, _fund, _json, _usage, extraction
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


def _raw(name):
    with open(os.path.join(REPO_ROOT, "scenarios", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
async def ydyn(rig, monkeypatch):
    r = rig
    s = r.sess
    s.scenario_id, s.scenario_data = "영도", _raw("영도")
    s.npcs = {}
    s.players = {PLAYER_UID: {"name": "테스터", "profile": {"성별": "여", "이름": "테스터", "나이": "29", "직업": "어부"}}}
    monkeypatch.setattr(WL, "pick_start_npc",                                  # v7.40 후보 중 진복례로 고정
                        lambda rows, seed, fid: next((x for x in rows or () if x["npc"] == "진복례"), None))
    res = await WL.initialize_world(r.bot, s, {"id": "arrive_person"})          # 시작 장면 인물 = 진복례
    assert res.outcome == WL.INIT_OK, res.reason
    WL.apply_compat_view(s, {"id": "arrive_person"})
    assert await WL.open_durable(r.bot, s)
    prov = H05Provider()
    r.bot.genai_client.models._provider = prov
    prov.routes.update({
        "narration": [FakeGenAIResponse("물양장은 조용하다.", usage=_usage())],
        "logic": [_json({"proceed_instruction": "선언대로 진행한다", "event_assessment": "ongoing"})],
        "irregular": [_json({"assignments": []})], "extraction": [extraction({})],
        "judgment": lambda kw: _json({"player_intent": "선언", "situation_read": "상황", "action": "PROCEED",
                                      "reasoning": "t", "bridge_message": "", "rolls": [],
                                      "action_intent": {"movement_kind": "NONE", "time_kind": "NONE"},
                                      "action_minutes": 5})})
    await _fund(PLAYER_UID, 10000)
    r.prov = prov
    return r


async def _cache_text(r):
    text, _t, _b, _f = await core.cache_lifecycle._build_text(r.bot, r.sess, "", False)
    return text


# ── 2(가) · A3 · 4: 캐시 본문 ─────────────────────────────────────

async def test_dynamic_cache_drops_auto_event_sections_status_weights_and_npc_extras(ydyn):
    text = await _cache_text(ydyn)
    sg = text[text.index("[2. 스토리 진행 가이드]"):text.index("[3. NPC 사전")]
    assert "[WE-01]" not in sg and "[T-01]" not in sg                          # 자동 진행 사건·긴장 트리거 절 제외
    assert "1. 서사 주도권" in sg and "6. 서사 종결" in sg and "동적 세계 세션" in sg
    assert "판정 가중치" not in text                                           # A3
    npc = text[text.index("[3. NPC 사전"):text.index("[4. 게임 스탯")]
    assert "외모:" in npc and "첫인상:" in npc                                  # 고정(정체·인상 — v7.39 persona 겉)
    for extra in ("비밀:", "동기·욕구:", "행동 방식:", "스탯:", "대사 견본", "균열"):
        assert extra not in npc, extra                                         # 추가 설정은 캐시에 없다
    assert "[장면 인물 상세]" in npc                                           # 어디서 오는지 안내


async def test_legacy_cache_keeps_full_story_guide_and_npc_dictionary(rig):
    s = rig.sess
    s.scenario_id, s.scenario_data, s.definition_revision = "영도", _raw("영도"), None
    text = await _cache_text(rig)
    assert "[WE-01]" in text and "[T-01]" in text and "비밀:" in text and "스탯:" in text
    assert "판정 가중치" not in text


def test_story_guide_filter_keeps_unnumbered_and_other_sections():
    out = CA.dynamic_story_guide("머리\n1. 원칙\nKEEP1\n2. 현재 진행 중인 세계 사건\nDROP1\n3. 긴장 트리거 규칙\nDROP2\n4. 엔딩\nKEEP2")
    assert "KEEP1" in out and "KEEP2" in out and "DROP1" not in out and "DROP2" not in out
    assert out.startswith("(동적 세계 세션")


# ── 4 · A5 · B3: 매 턴 입력 ──────────────────────────────────────

async def test_scene_npc_extras_and_items_are_injected_only_for_the_scene(ydyn):
    r = ydyn
    await r.gm._process_actions(r.sess, "주변을 둘러보며 말을 건다", r.master)
    logic, narr = r.prov.prompts["logic"][0][0], r.prov.prompts["narration"][0][0]
    d = r.sess.scenario_data["default_npcs"]
    for text in (logic, narr):
        detail = text[text.index("[장면 인물 상세"):]
        assert "진복례:" in detail and d["진복례"]["비밀"][:20] in detail     # 장면 인물(정본 위치)만
        assert d["강철수"]["비밀"][:20] not in text                            # 장면 밖 인물의 비밀은 들어가지 않는다
    assert "[물건 — 정본 소지" in narr and "거룻배" in narr                    # A5: 묘사층위도 정본 물건을 본다
    assert "[PC 자원]: (아래 [물건 — 정본 소지] 블록 참조)" in logic          # B3


# ── 1 · 3 · 5 · C3 ────────────────────────────────────────────

def test_narration_directive_is_genre_neutral_and_wuxia_rules_moved():
    s = core.TRPGSession("v734", 1, 2, "t", {})
    narr = core.PromptBuilder.build_prompt(s, "지시")
    for w in ("문파", "사매", "정파", "검 두 자루", "레비", "강철수"):
        assert w not in narr, w
    with open(os.path.join(REPO_ROOT, "prompts.py"), encoding="utf-8") as f:
        src = f.read()
    assert "@대사:레비" not in src and "@대사:강철수" not in src


def test_yeongdo_rulebook_relaxed_and_unused_keys_removed():
    y = _raw("영도")
    assert "근거 없이 시간을 지어내" in y["stat_system"] and "현실적으로 추적되어야" not in y["stat_system"]
    assert "날씨는 별도로 기록되지 않습니다" in y["desc_guide"]
    for k in ("intro_times", "intro_people", "intro_threats", "intro_findings", "affiliations", "origin_warnings"):
        assert k not in y, k


def test_dead_instruction_verifier_removed():
    import cogs.gm as gm
    import prompts
    assert not hasattr(gm.GMCog, "_verify_proceed_instruction")
    assert not hasattr(prompts, "PROCEED_VERIFY_SCHEMA") and not hasattr(prompts, "PROCEED_VERIFIER_SYSTEM_INSTRUCTION")


@pytest.mark.parametrize("header,cls", [
    ("시나리오 금지사항 — 이 결정과 지시문 작성 시 반드시 준수", PC.FIXED),
    ("진행 중인 사건 — 지시층위 전용 지침(플레이어의 자유 행동을 …)", PC.FIXED),
    ("플레이어 프로필", PC.FIXED),
    ("플레이어 신규 발언", PC.VARIABLE),
    ("최근 플레이어 행동 및 대화 (판정 완료됨)", PC.VARIABLE),
    ("최근 5턴 컨텍스트 (온전 원문)", PC.LIKELY)])
def test_census_classifies_by_header_head(header, cls):
    assert PC.classify(header) == cls
