# -*- coding: utf-8 -*-
"""H07-C — 동적 세계 세션의 레거시 서사 권위 은퇴.

DYNAMIC(정의 바인딩) 세션에서 레거시 퀘스트 선택·진행·intended_case·info_ledger·hidden 인지 조건·정적 상주
동행 해제가 사건 상태기계·정보/인지와 병렬 권위로 경쟁하지 않는다. LEGACY 세션은 그대로다.
"""
from __future__ import annotations

import pytest

import core
from core import player_world as PW
from core import turn_preparation as TP
from core import turn_world as TW
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    H05Provider, drig, extraction, judgment, rig, turn)
from tests.policy.test_h05a_intent_planning import PC, ctx_of, make_session

pytestmark = pytest.mark.policy


def test_retirement_predicate_is_binding_based():
    s, _ = make_session()
    assert PW.legacy_narrative_retired(s)
    assert not PW.legacy_narrative_retired(core.TRPGSession("legacy", 1, 2, "x", {}))


def test_instruction_effects_are_not_staged_for_dynamic_sessions():
    s, _ = make_session()
    s.info_ledger = []
    decision = {"quest_choice": {"id": "anything", "reason": "x"}, "quest_case": "c1",
                "info_access": {"new_leaks": [{"info": "비밀", "known_by": ["누군가"], "how": "엿들음"}]}}
    p = TP.stage_instruction_effects(s, decision)
    assert p.projected_quest_state is None and p.info_ledger is None
    assert p.diagnostics.get("legacy_narrative") == "RETIRED"


def test_prompt_builder_drops_legacy_quest_block_for_dynamic():
    s, _ = make_session()
    b = core.prompt.PromptBuilder(s, "지시")
    b.add_quest_block()
    assert "퀘스트" not in b.manifest


def test_faction_compat_view_derives_from_event_results():
    s, _ = make_session()
    s.event_instances = {
        "e1": {"template_id": "j1", "lifecycle": "RESOLVED", "phase_entered_at": 10,
               "local_state": {"grant_faction": "남항 중공업 연대"}},
        "e2": {"template_id": "j2", "lifecycle": "RESOLVED", "phase_entered_at": 50,
               "local_state": {"grant_faction": "중리 교역소"}},
        "e3": {"template_id": "j3", "lifecycle": "ACTIVE", "phase_entered_at": 90,
               "local_state": {"grant_faction": "조도 방위 사령부"}}}
    assert TW.derived_player_faction(s) == "중리 교역소"            # 진행 중 사건은 소속을 주지 않는다
    s.event_instances = {}
    assert TW.derived_player_faction(s) is None


def test_pc_knowledge_block_replaces_info_ledger_with_cognition_summaries():
    s, D = make_session()
    s.cognition_state = {f"actor:{PC}": {"S40": {"awareness": "CONTENT", "belief": None}}}
    blk = TW.pc_knowledge_block(ctx_of(s), None, s)
    assert "원본 기록 위치" in blk and "제3보관실" not in blk          # 공개 안전 요약만(명제 원문 없음)


async def test_dynamic_turn_has_no_legacy_quest_or_ledger_authority(drig):
    r = drig
    r.make()
    r.sess.info_ledger = [{"info": "레거시비밀", "known_by": ["누군가"]}]
    await turn(r)
    logic_prompt, logic_cfg = r.prov.prompts["logic"][-1]
    props = (logic_cfg.response_schema or {}).get("properties") or {}
    assert "quest_choice" not in props and "info_access" not in props
    assert "레거시비밀" not in logic_prompt and "[정보 인지 원장" not in logic_prompt
    assert all("숨겨진 사실의 인지 조건" not in p[0] for p in r.prov.prompts.get("extraction", []))
    assert all("레거시비밀" not in p[0] for p in r.prov.prompts.get("narration", []))
