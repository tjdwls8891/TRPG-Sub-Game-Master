# -*- coding: utf-8 -*-
"""v7.23.1 — 판단 프롬프트 계약 충돌 해소(I01~I14 중 프롬프트 계약 항목).

v7.23.0은 판단 **시스템 지시**에 '[선언 게이트] 블록이 없으면 discovery 굴림을 만들지 않는다'를 남겨 둔 채
DYNAMIC **사용자 프롬프트**가 '이 블록을 따른다'로 우회했다. v7.23.1은 시스템 지시 자체를 새 순서(판단 초안 →
조건부 검증 → 승인된 초안만 굴림)로 고치고 우회 문구를 없앴다. 아래 테스트는 실제 provider에 전달된 요청 패킷
(system_instruction·사용자 본문·응답 스키마)으로 계약을 확인한다 — 모델 응답의 우연한 준수에 기대지 않는다.
"""
from __future__ import annotations

import pytest

import prompts
from tests.policy.test_h05bc_turn_wiring import drig, judgment, rig, turn  # noqa: F401
from tests.policy.test_v714_discovery_assertion import jdg, jfind, sim
from tests.policy.test_v723_declaration_check import crig  # noqa: F401

pytestmark = pytest.mark.policy

OLD_RULE = "블록이 없으면 discovery=true 굴림을 만들지 않는다"
OVERRIDE = "이 블록을 따른다"


def _jpacket(r, i=-1):
    text, cfg = r.prov.prompts["judgment"][i]
    return text, cfg.system_instruction, cfg.response_schema


def test_I01_system_instruction_owns_the_new_draft_contract():
    si = prompts.JUDGMENT_SYSTEM_INSTRUCTION
    assert OLD_RULE not in si
    start = si.index("[선언 검증 — 주변 발견·결과 선취]")
    sec = si[start:si.index("[결정 우선순위]", start)]
    assert "[선언 게이트] 블록이 없으면(검증 전)" in sec and "**초안**" in sec          # 검증 전 = 초안
    assert "코드가 검증한 뒤에만 굴리거나 빼거나 다시 묻는다" in sec
    assert "블록이 있으면(검증이 이미 끝남) 그 판정을 따른다" in sec                    # 검증 후 = 판정 집행
    assert "assertion_claim=true" in sec and "discovery_ref" in sec


async def test_I02_I03_I12_dynamic_packet_has_no_conflict_or_role_inversion(crig):
    r = crig
    r.prov.routes["judgment"] = [jdg()]
    await turn(r, "주변을 둘러본다")
    text, si, schema = _jpacket(r)
    assert si == prompts.JUDGMENT_SYSTEM_INSTRUCTION and OLD_RULE not in si           # 실제 전송 시스템 지시
    assert OVERRIDE not in text and "[선언 검증 — 동적 세계" not in text              # 사용자 본문 우회·중복 없음
    props = schema["properties"]
    assert {"assertion_claim", "discovery_target", "discovery_ref"} <= set(props)    # 시스템 지시가 말하는 필드 = 스키마
    assert props["rolls"]["items"]["properties"].get("discovery") is not None or "discovery" in str(props["rolls"])


async def test_I12_rejudge_packet_carries_gate_and_no_draft_fields(crig):
    r = crig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jfind("노끈"), jfind("노끈", roll=True)]            # 초안 누락 → 예외 복구 1회
    await turn(r, "노끈을 찾아본다")
    assert r.prov.calls.count("judgment") == 2
    t0, _si0, s0 = _jpacket(r, 0)
    t1, _si1, s1 = _jpacket(r, 1)
    assert "[선언 게이트" not in t0 and "[선언 게이트 — 코드 확정(공개 범주)]" in t1
    assert "assertion_claim" in s0["properties"] and "assertion_claim" not in s1["properties"]   # 검증 후 판정 집행


async def test_I08_ordinary_and_answer_turns_do_not_rejudge(crig):
    r = crig
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r)
    assert r.prov.calls.count("judgment") == 1 and "simulation" not in r.prov.calls


def test_instruction_threat_clause_grounds_on_canon_only():
    gi = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "서사 방향성 시뮬레이션" not in gi                     # T-6: 시뮬레이션 은퇴
    i = gi.index("해당 위협의 접근·전조가 정본([진행 중인 사건]·[현재 장면 인물]·[세계 진행] 신호)에 이미 있는 경우")
    assert "근거가 되지 않는다" in gi[i:i + 200]
