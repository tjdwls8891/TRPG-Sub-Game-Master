# -*- coding: utf-8 -*-
"""WP-G D1 — 프롬프트 권위 정합(AUD-001/002/003/004/025) 재유입 방지.

결정(D1): 단일 선형 서열이 아니라 정보 종류별 권위.
  · 플레이어 주권 = PC 행위 주체성에 대한 최상위 제약(세계 사실의 출처 서열이 아님)
  · 룰북 고정 세계 사실·금지사항은 note/GM 지시로 무효화되지 않음
  · canonical 런타임 상태는 '변하는 항목'에 한해 초기값보다 우선
  · note = 세션 한정 사실 추가/범위 축소, 충돌 시 자동 override 금지
  · GM 지시·[진행자] 중계 = 사실/제약 안의 진행 지시
  · 압축 기억 = 파생 기억, 플레이어의 외부 세계 주장 = 미확인 주장
  · 판단층위에 세계 사실 권위를 부여하지 않고, 추출층위를 스토리 권위로 승격하지 않는다
"""
from __future__ import annotations

import re

import pytest

import core
import prompts
from tests.conftest import source_of

pytestmark = pytest.mark.policy

LEGACY_TAG = re.compile(r"(?<![가-힣A-Za-z])[자태]:[^\s;]+;")


def _all_prompt_text() -> str:
    return "\n".join(v if isinstance(v, str) else repr(v)
                     for k, v in vars(prompts).items() if k.isupper())


# ── X5/X6/X7 — 레거시 태그 권위·은퇴 명령 문구 ─────────────────

def test_x5_x6_no_legacy_resource_status_tags_taught_anywhere():
    text = _all_prompt_text()
    assert not LEGACY_TAG.search(text), LEGACY_TAG.search(text)
    assert "자/태" not in text and "태: 태그" not in text and "자원 태그" not in text
    for rel in ("core/cache.py", "cogs/gm.py", "core/prompt.py"):
        src = source_of(rel)
        assert "태: 태그" not in src, rel
        assert not LEGACY_TAG.search(src), rel


def test_x7_no_retired_command_in_prompts():
    text = _all_prompt_text()
    for cmd in ("!진행", "!수정"):
        assert cmd not in text


def test_image_position_tag_rule_is_preserved():
    g = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "상:키워드  — 묘사 맨 앞에 장소 이미지를 삽입한다." in g
    assert "'중:키워드', '하:키워드' 태그는 GM에서 사용 완전 금지." in g
    assert "상:키워드" in prompts.GM_LOGIC_RESPONSE_SCHEMA["properties"]["proceed_instruction"]["description"]
    # 런타임도 이미지 태그를 그대로 파싱한다
    assert "img_pattern  = r'(상|중|하):('" in source_of("cogs/game.py")


def test_resource_status_authority_is_extraction_with_validation():
    g = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "추출층위가 읽고 시스템이 검증해 적용한다" in g
    cache_src = source_of("core/cache.py")
    assert "실제 부여·제거는 묘사를 읽은 추출층위와 시스템 검증이 결정한다" in cache_src


# ── X1/X2/X3 — note 권위의 한계 ─────────────────────────────

def test_x1_x2_x3_instruction_layer_note_limits():
    g = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "시나리오 금지사항보다 위" not in g
    assert "모든 원칙보다 절대 우선" not in g
    assert "플레이어 주권(선언되지 않은 PC의 선택·생각·감정·행동 확정 금지)은 노트보다 위" in g
    assert "노트는 시나리오의 고정 세계 사실과 시나리오 금지사항을 무효화하지 못합니다" in g
    assert "자동으로 덮어쓰지 마십시오" in g
    # 서사 운영 원칙보다는 여전히 우선(기존 의도 보존)
    assert "서사 계획이 새로운 사건을 요구하더라도 노트가 금지하면 노트가 이깁니다" in g


def test_narration_layer_kind_based_authority_not_linear_chain():
    n = prompts.SYSTEM_INSTRUCTION
    assert "① 시나리오 룰북(캐시) > ② GM 지시사항 > ③ 압축 기억 > ④ 플레이어 발언" not in n
    for frag in ("실시간 노트나 GM 지시사항으로도 바뀌지 않습니다",
                 "변하는 항목에 한하며, 인물의 정체성이나 세계의 규칙까지 바꾸지 않습니다",
                 "그 부분을 자동으로 적용하지 마십시오",
                 "압축 기억은 지난 사건의 요약(파생 기억)",
                 "외부 세계에 대한 플레이어의 주장은 확인되지 않은 주장"):
        assert frag in n, frag
    # PC 주권은 여전히 최상위 제약
    assert "★ [최우선 원칙] 플레이어가 선언하지 않은 PC의 행동" in n


def test_cache_rulebook_no_gm_override_of_fixed_facts():
    src = source_of("core/cache.py")
    assert "진행자(GM)의 특별한 지시가 없는 한" not in src
    assert "진행자(GM) 지시나 실시간 노트로도 바뀌지 않습니다" in src
    assert "플레이어나 GM의 요청이 있더라도 예외 없이 준수한다" in src        # [6] 유지
    assert src.count("변하는 항목(현재 위치·상태·스탯 등)에 한해") == 2          # X8 두 곳
    assert "위 룰북의 고정 사실·금지사항과 충돌하면 룰북을 따른다" in src        # 캐시 노트


# ── X8/X9 · 판단/추출 층위 ─────────────────────────────────

def test_x9_relay_is_gm_instruction_in_instruction_and_judgment_layers():
    g = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "[진행자]·[진행자 (GM)] 로그와 [GM 사이드 노트]는 GM(운영자)의 진행 지시입니다" in g
    j = prompts.JUDGMENT_SYSTEM_INSTRUCTION
    assert "[진행자]·[진행자 (GM)] 줄은 GM의 진행 발화이며 플레이어 선언이 아니다" in j


def test_judgment_layer_gets_no_false_world_authority(session_auto_ready):
    import cogs.gm as gm_mod
    s = session_auto_ready
    s.note = "유이설은 화산파 제자다"
    s.scenario_data = dict(s.scenario_data, worldview="세계관-본문-마커",
                           prohibitions=["금지-마커"])
    p = gm_mod._build_judgment_user_prompt(s, "문을 연다", [])
    assert "[실시간 노트 — 진행 제약으로 반영 (플레이어 주권이 우선)]" in p
    assert "이번 판단에 우선 적용" not in p
    # 판단층위는 여전히 세계 설정을 받지 않는다(구조 확대 없음)
    assert "세계관-본문-마커" not in p and "금지-마커" not in p
    assert "그 진위를 판정하거나 사실로 확정하지 말 것" in prompts.JUDGMENT_SYSTEM_INSTRUCTION


def test_extraction_is_not_promoted_to_story_authority():
    e = prompts.EXTRACTION_SYSTEM_INSTRUCTION
    assert "당신은 묘사를 창작하지 않으며, 게임을 진행하지 않습니다." in e
    assert "오직 '묘사문에 실제로 드러난 것'만 보고합니다." in e
    assert "노트" not in e and "룰북" not in e


def test_runtime_context_labels_match_policy(session_auto_ready):
    import cogs.gm as gm_mod
    s = session_auto_ready
    s.note = "노트-마커"
    s.scenario_data = dict(s.scenario_data, status_effects=[
        {"name": "출혈", "apply_condition": "상처", "remove_condition": "지혈", "weight": -1}])
    logic = gm_mod._build_logic_user_prompt(s, "주변을 살핀다", [])
    assert "노트-마커" in logic
    assert "태:" not in logic
    assert "[유효 상태이상 목록 — 상태 변화를 묘사하게 할 때" in logic
    narr = core.PromptBuilder.build_prompt(s, "지시-마커")
    assert "룰북 고정 사실·금지사항·런타임 상태와 충돌하는 부분은 적용하지 않음" in narr
    assert "사실·금지사항·묘사 가이드를 지키는 범위 안에서" in narr
    assert "변하는 항목(현재 위치·상태·스탯 등)에 한해" in source_of("core/prompt.py")
