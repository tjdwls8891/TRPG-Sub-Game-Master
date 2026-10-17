# -*- coding: utf-8 -*-
"""H07-D — prompts.py 최종 권위 정합(동적 세계 세션 조건부 · LEGACY 의미 보존) + provider 호출 단계 불변."""
from __future__ import annotations

import ast
import os

import pytest

import prompts
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.policy


def test_dynamic_authority_rules_present_in_each_layer():
    assert "동적 세계 세션" in prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    for phrase in ("레거시 퀘스트·정보 원장은 이 세션의 서사 권위가 아닙니다", "억지 해석하지 마십시오",
                   "플레이어 대신 고르거나", "독립적인 기회·압력·결과", "표시된 이름으로만", "노출은 믿음이 아닙니다"):
        assert phrase in prompts.GM_LOGIC_SYSTEM_INSTRUCTION, phrase
    assert "resolution(결단)은 플레이어가 이번 발언에서" in prompts.JUDGMENT_SYSTEM_INSTRUCTION
    assert "결단 선택지를 제시·권유하지 마십시오" in prompts.JUDGMENT_SYSTEM_INSTRUCTION
    assert "노출은 믿음이 아닙니다" in prompts.EXTRACTION_SYSTEM_INSTRUCTION
    assert "[세계 진행 — 코드 확정 결과] 블록이 있을 때만" in prompts.SYSTEM_INSTRUCTION


def test_legacy_quest_meaning_preserved():
    assert "[퀘스트 선택 — quest_choice]" in prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "quest_choice" in prompts.GM_LOGIC_RESPONSE_SCHEMA["properties"]      # 원본 스키마 불변(런타임 사본만 축소)
    assert "quest_progress.advance" in prompts.EXTRACTION_SYSTEM_INSTRUCTION


def test_provider_production_callsites_stay_18():
    n = 0
    for base in ("cogs", "core"):
        for f in sorted(os.listdir(os.path.join(REPO_ROOT, base))):
            if f.endswith(".py") and (base, f) != ("core", "logs.py"):
                tree = ast.parse(open(os.path.join(REPO_ROOT, base, f), encoding="utf-8").read())
                n += sum(1 for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr == "generate_content")
    tree = ast.parse(open(os.path.join(REPO_ROOT, "main.py"), encoding="utf-8").read())
    n += sum(1 for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr == "generate_content")
    assert n == 18                                                              # H07은 늘리지 않는다 · P1 후속 묘사 은퇴(19 → 18)
    # core/logs.py 기록 프록시는 호출 단계가 아니라 통과 전달 1회뿐이다(새 provider 호출 없음)
    tree = ast.parse(open(os.path.join(REPO_ROOT, "core", "logs.py"), encoding="utf-8").read())
    assert sum(1 for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr == "generate_content") == 1
