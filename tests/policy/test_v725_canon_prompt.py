# -*- coding: utf-8 -*-
"""v7.25 — DYNAMIC 정본·프롬프트 권위 정합화 + 프롬프트 문자 수 절단 제거.

실제 provider 직전에 조립된 요청 패킷(본문·시스템 지시·스키마)과 커밋 결과로 확인한다(라이브 호출 없음).
인트로 정본(A) · 코드 상태창(B) · 동행(C) · 비저작 NPC(D) · 지시/추출 스키마(E) · 기억(F) · 문자 절단(G).
"""
from __future__ import annotations

import pathlib
import re

import pytest

import core
import prompts
from core import spatial as SP
from core import turn_world as TW
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h05bc_turn_wiring import (PC, drig, extraction, judgment, turn,  # noqa: F401
                                                 world_of)
from tests.policy.test_ready_barrier import _json, _usage, rig  # noqa: F401

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]

STATUS_TPL = ("```css\n📅 [#{현재 턴 수}] [{시나리오상 날짜} {요일} {시간대(새벽/아침/오전/낮/오후/저녁/밤)}]\n"
              "📍 {현재 장소}\n상황 요약 : {현재 상황 20자 이내 요약}\n```")
WRONG = "본문 첫 문단.\n\n```css\n📅 [#9] [D+90 토요일 밤]\n📍 비밀기지\n상황 요약 : 배가 부서졌다\n```"


def _status_patch(raw):
    raw["status_code_block"] = STATUS_TPL
    raw["places"]["시장"]["unknown_name"] = "소란한 골목"


def _narr_text(r, i=-1):
    return r.prov.prompts["narration"][i][0]


# ══════════════════════════════════════════════════════════════
# A·B·F — 인트로: genesis 정본 · 코드 상태창 · 정본 기록 줄
# ══════════════════════════════════════════════════════════════

async def test_A01_A02_B01_F01_intro_reads_genesis_canon_not_legacy_place_block(drig):
    r = drig
    r.make(raw_patch=_status_patch)
    s = r.sess
    s.spatial_knowledge[PC]["places"]["시장"]["knows_name"] = False         # 이름을 모르는 출구
    s.world_lifecycle = WL.LIFECYCLE_INITIALIZED                             # 인트로 = OPEN 전
    r.prov.routes["narration"] = [FakeGenAIResponse(WRONG, usage=_usage())]
    vals = TW.status_values(TW.WorldTurn(ctx=WL.intro_world_context(s)), s, int(s.turn_count) + 1)
    res = await r.game._execute_proceed(s, "[인트로 생성 지시]\n시작 상황을 묘사하라")
    assert isinstance(res, dict) and res.get("ok")
    p = _narr_text(r)
    assert "[현재 작중 시각 — 정본]" in p and "[현재 장소 — 정본]" in p and "[인트로 — 시작 틀과 정본]" in p
    assert "[갈 수 있는 곳]" not in p and "연관 인물:" not in p              # 레거시 장소 블록 폴백 없음
    assert "소란한 골목" in p and "· 시장" not in p                          # 숨은 이름 비노출
    assert f"[{vals['date']} {vals['weekday']} {vals['daypart']}]" in p      # 상태창 양식 정본 값 선기입
    model_text = s.raw_logs[-1].parts[0].text
    assert "D+90" not in model_text and "비밀기지" not in model_text         # 모델 상태창 값이 남지 않는다
    assert f"📍 {vals['place']}" in model_text and "상황 요약 : 배가 부서졌다" in model_text
    assert s.uncompressed_logs[-1].startswith(f"[GM 묘사]: [정본 기록 #{vals['turn']}턴 | {vals['date']}")


async def test_A01_bound_session_without_canon_fails_closed(drig, monkeypatch):
    r = drig
    r.make()
    r.sess.world_lifecycle = WL.LIFECYCLE_INITIALIZED
    monkeypatch.setattr(core.player_world, "load_definition", lambda *a, **k: None)
    with pytest.raises(ValueError):
        WL.intro_world_context(r.sess)                                       # 레거시 폴백 없이 진단 가능한 실패


def test_A01_unbound_session_has_no_intro_canon(drig):
    assert WL.intro_world_context(drig.sess) is None                         # LEGACY 인트로는 그대로


# ══════════════════════════════════════════════════════════════
# A04·A06 — 묘사 시스템 지시 §8 · NARRATE 정본 문맥
# ══════════════════════════════════════════════════════════════

def test_A06_section8_triggers_on_any_canon_block_not_only_plan():
    si = prompts.SYSTEM_INSTRUCTION
    sec = si[si.index("8. 동적 세계 세션"):]
    for head in ("[현재 작중 시각 — 정본]", "[현재 장소 — 정본]", "[이번 응답 — 세계 진행 없음(정본 기준)]"):
        assert head in sec
    assert "인트로·진행 없는 턴도 같다" in sec and "상태창의 날짜·요일·시간대·장소·턴 수는 코드가" in sec
    for head in ("[현재 작중 시각 — 정본]", "[현재 장소 — 정본]"):            # 블록 제목이 실제 생성 문자열과 같다
        assert head in (ROOT / "core/turn_world.py").read_text(encoding="utf-8")


async def test_A04_narrate_without_plan_gets_canon_scene_people(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, action="NARRATE")
    r.prov.routes["logic"] = [_json({"narrate_instruction": "주변을 짧게 설명"})]
    r.prov.routes["narration"] = [FakeGenAIResponse("조용하다.", usage=_usage())]
    await turn(r, "여기 어때?")
    p = _narr_text(r)
    assert "[이번 응답 — 세계 진행 없음(정본 기준)]" in p and "[현재 장면 인물 — 정본 위치 기준" in p


# ══════════════════════════════════════════════════════════════
# B — 상태창 렌더링 단위
# ══════════════════════════════════════════════════════════════

VALS = {"turn": "3", "date": "2016-10-02", "weekday": "일요일", "daypart": "새벽", "hhmm": "05:40", "place": "선착장"}


def test_B03_B04_render_keeps_layout_and_model_owned_lines_only():
    out = TW.render_status_block(STATUS_TPL, VALS, WRONG.split("\n\n", 1)[1])
    assert out.startswith("```css") and out.endswith("```")
    assert "📅 [#3] [2016-10-02 일요일 새벽]" in out and "📍 선착장" in out
    assert "D+90" not in out and "비밀기지" not in out and "상황 요약 : 배가 부서졌다" in out


def test_B05_missing_canon_value_is_marked_not_invented():
    out = TW.render_status_block(STATUS_TPL, dict(VALS, place=TW.STATUS_UNKNOWN), "")
    assert f"📍 {TW.STATUS_UNKNOWN}" in out and "상황 요약" not in out     # 모델 값도 지어내지 않는다


async def test_B02_moving_turn_status_shows_arrival_place_and_end_time(drig):
    r = drig
    r.make(raw_patch=lambda raw: raw.__setitem__("status_code_block", STATUS_TPL))
    r.prov.routes["narration"] = [FakeGenAIResponse(WRONG, usage=_usage())]
    await turn(r, "시장으로 간다")
    body = r.sess.raw_logs[-1].parts[0].text
    assert "📍 시장" in body and "D+90" not in body                         # 도착 장소 = 묘사 시점 정본


# ══════════════════════════════════════════════════════════════
# C — 동행: 합류 검증 · 함께 이동 · 이탈
# ══════════════════════════════════════════════════════════════

def _register_kang(s, place="항구"):
    from tests.policy.test_h04a_actor_info import KANG
    s.actor_registry = {**s.actor_registry,
                        KANG: {"kind": "NPC", "display_name": "강지수", "source_kind": "AUTHORED_NPC",
                               "source_key": "강지수", "aliases": []}}
    s.spatial_state = {**s.spatial_state, "placements": {**s.spatial_state["placements"],
                                                         KANG: SP.at_place(place)}}
    return KANG


async def test_C01_C05_join_of_npc_elsewhere_is_rejected_met_kept(drig):
    r = drig
    r.make()
    _register_kang(r.sess, place="성당")                                   # 다른 장소에 있다
    r.prov.routes["judgment"] = judgment(None)
    r.prov.routes["extraction"] = [extraction({}, companions={"joined": ["강지수"], "left": []},
                                              npcs_met=["강지수"])]
    await turn(r, "강지수에게 같이 가자고 한다")
    assert "강지수" not in (r.sess.companions or [])
    assert "강지수" in (r.sess.met_npcs or [])                             # 만남은 만남으로만
    from tests.policy.test_h04a_actor_info import KANG
    assert r.sess.spatial_state["placements"][KANG] == SP.at_place("성당")  # 순간이동 없음


async def test_C02_colocated_join_then_companion_moves_with_pc(drig):
    from tests.policy.test_h04a_actor_info import KANG
    r = drig
    r.make()
    _register_kang(r.sess, place="항구")
    r.prov.routes["judgment"] = judgment(None)
    r.prov.routes["extraction"] = [extraction({}, companions={"joined": ["강지수"], "left": []})]
    await turn(r, "강지수에게 같이 가자고 한다")
    assert "강지수" in r.sess.companions
    r.prov.routes["judgment"] = judgment("시장")
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "시장으로 간다")
    assert r.sess.spatial_state["placements"][PC] == SP.at_place("시장")
    assert r.sess.spatial_state["placements"][KANG] == SP.at_place("시장")   # 같은 커밋으로 따라왔다
    assert "PC 동행" in _narr_text(r)                                         # 묘사 전 계획 투영에 이미 있다


async def test_C04_leaving_companion_without_event_role_returns_to_routine(drig):
    from tests.policy.test_h04a_actor_info import KANG
    r = drig
    r.make()
    _register_kang(r.sess, place="항구")
    r.sess.companions = ["강지수"]
    r.prov.routes["judgment"] = judgment(None)
    r.prov.routes["extraction"] = [extraction({}, companions={"joined": [], "left": ["강지수"]})]
    await turn(r, "여기서 헤어지자")
    assert "강지수" not in r.sess.companions
    assert KANG not in r.sess.spatial_state["placements"]                   # override 해제 → 일과/보류
    assert r.sess.spatial_state["placement_holds"][KANG]["placement"] == SP.at_place("항구")


def test_C06_follow_is_pure_and_skips_when_pc_does_not_move(drig):
    r = drig
    r.make()
    _register_kang(r.sess)
    plan = core.player_world.PlayerWorldPlan(status=core.player_world.PLAN_NONE)
    ctx = core.player_world.session_world_context(r.sess)
    assert TW.companion_follow(ctx, r.sess, plan, ["강지수"]) is plan       # 계획 불변(이동 없음)


# ══════════════════════════════════════════════════════════════
# D — 비저작 NPC: 정본 인물 우선 · 승격 등록
# ══════════════════════════════════════════════════════════════

def test_D01_canonical_actor_is_never_irregular(drig):
    r = drig
    r.make()
    _register_kang(r.sess)
    assert core.irregular_npc.is_regular(r.sess, "강지수")
    assert core.irregular_npc.needs_resolution(r.sess, ["강지수", "낯선 사내"]) == ["낯선 사내"]


def test_D02_D03_promoted_session_npc_registers_once_at_scene(drig):
    r = drig
    r.make()
    s = r.sess
    ctx = core.player_world.session_world_context(s)
    w = core.world_mutation.ProjectedWorld.capture(s)
    ops = TW.session_npc_register_ops(ctx, w, ["낯선 사내", "강지수_없는이름"], narrative_seed="seed",
                                      prefix="t", present=["낯선 사내"])
    regs = [o for o in ops if o.op_type == core.actors.OP_ACTOR_REGISTER]
    places = [o for o in ops if o.op_type == SP.OP_ACTOR_PLACEMENT_SET]
    assert {o.payload["display_name"] for o in regs} == {"낯선 사내", "강지수_없는이름"}
    assert [o.payload["place_id"] for o in places] == ["항구"]               # 이번 턴에 보인 인물만 배치
    _register_kang(s)
    w2 = core.world_mutation.ProjectedWorld.capture(s)
    assert not TW.session_npc_register_ops(ctx, w2, ["강지수"], narrative_seed="seed", prefix="t")  # 정본 인물 중복 없음


def test_D04_dynamic_promotion_does_not_rename_to_model_true_name():
    src = (ROOT / "cogs/gm.py").read_text(encoding="utf-8")
    body = src[src.index("async def _apply_npc_promotion"):src.index("async def _resolve_irregular_npcs")]
    assert "final_name != name and not core.player_world.legacy_narrative_retired(session)" in body


# ══════════════════════════════════════════════════════════════
# E — 지시층위·추출 스키마/본문
# ══════════════════════════════════════════════════════════════

async def test_E01_E05_E07_dynamic_logic_packet_has_no_retired_fields_or_legacy_plan(drig):
    r = drig
    r.make()
    r.sess.narrative_plan = {"current_event": {"title": "옛 계획"}, "mid_plan": {"title": "옛 흐름"}}
    r.sess.compressed_memory = "가" * 900 + "끝부분기억"
    await turn(r, "시장으로 간다")
    text, cfg = r.prov.prompts["logic"][-1]
    props = (cfg.response_schema or {}).get("properties") or {}
    for k in ("info_access", "quest_choice", "event_assessment"):
        assert k not in props
    assert "[현재 서사 계획" not in text and "옛 계획" not in text
    assert "세력/지역 컨텍스트" not in text and "가중치" not in text
    assert "끝부분기억" in text                                              # G01: 압축 기억 800자 절단 제거


async def test_E02_dynamic_extraction_schema_drops_retired_legacy_fields(drig):
    r = drig
    r.make()
    await turn(r, "시장으로 간다")
    text, cfg = r.prov.prompts["extraction"][-1]
    props = cfg.response_schema["properties"]
    for k in core.extraction.DYNAMIC_RETIRED_FIELDS:
        assert k not in props and k not in cfg.response_schema.get("required", [])
    assert "companions" in props and TW.OBS_FIELD in props
    assert "- 날짜 및 시간" not in text and "- 퀘스트 진행도 평가" not in text


def test_E03_E04_threat_rules_need_canon_not_self_omen_and_keep_world_agency():
    gi = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "이전 묘사가 지어낸 분위기·전조 문장만으로는 근거가 되지 않는다" in gi
    assert "세계·NPC·세력은 플레이어를 기다리지 않습니다" in gi                 # 자발성은 그대로
    assert "빠른 발소리가 이쪽으로 달려오고" not in gi and "바닥을 긁는 소리가 가까워지기" not in gi
    assert "[직전 NARRATE 횟수 / 한도] 필드를 참고하여" not in gi
    assert "info_access 필드에 그 판정을 명문화" not in gi


# ══════════════════════════════════════════════════════════════
# F — 기억 압축
# ══════════════════════════════════════════════════════════════

def test_F01_F04_compression_uses_canon_record_and_dynamic_hint_has_no_quest_or_placeid(drig):
    r = drig
    r.make()
    s = r.sess
    src = (ROOT / "prompts.py").read_text(encoding="utf-8")
    assert "[정본 기록 #N턴 | …] 줄이 있으면 반드시 그 값을" in src
    s.memory_plan = "high"
    s.world_timeline = {"current_location": "시장", "current_location_display": "소란한 골목"}
    s.last_extraction = {"quest_progress": {"advance": 80, "deviation": 0}, "situation": {"tag": "탐색"}}
    hint = core.memory_plan.build_context_hint(s)
    assert hint
    assert "소란한 골목" in hint and "시장" not in hint and "서사 진행" not in hint and "퀘스트" not in hint


def test_F06_memory_headers_are_derived_not_absolute():
    assert "절대 참조용" not in (ROOT / "core/prompt.py").read_text(encoding="utf-8").replace(
        "'절대 참조용' 격상 금지", "")
    assert "파생 기억이다" in (ROOT / "core/cache.py").read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# G — 프롬프트 문자 수 절단 제거(전수 처리 대장의 수정 항목)
# ══════════════════════════════════════════════════════════════

CUT_PATTERNS = {
    "cogs/gm.py": ("instr[:200]", "c[:130]", "ai_out[:400]", "full_text[:500]", "_mem[:800]",
                   "(text or '')[:200]", "(recent_text or '')[:1500]", "worldview[:600]", "story_guide[:1200]",
                   "memory_str[:1000]", "v[:20]", "_projs[:30]", "list(prev.items())[:10]"),
    "core/turn_preparation.py": ('(text or "")[:120]',),
    "core/turn_history.py": ('or "")[:120]',),
    "core/profile_ai.py": ("text[:CONTEXT_LIMIT]", "(value or '')[:500]", "list(val.values())[:5]"),
    "core/master.py": ("v[:60]",),
    "core/turn_world.py": ("deltas[-3:]",),
}


@pytest.mark.parametrize("path", sorted(CUT_PATTERNS))
def test_G01_G03_known_prompt_char_cuts_are_gone(path):
    src = (ROOT / path).read_text(encoding="utf-8")
    for pat in CUT_PATTERNS[path]:
        assert pat not in src, (path, pat)


def test_G05_mention_context_keeps_whole_paragraphs():
    long = "가" * 1500
    text = f"첫 문단 {long}\n\n두 번째 문단에 낯선 사내가 나온다 {long} 끝\n\n셋째"
    ctx = core.turn_preparation._mention_context(text, "낯선 사내")
    assert ctx.startswith("두 번째 문단") and ctx.endswith("끝")             # 문단 전체(앞·중간·끝)


def test_G05_profile_validation_context_is_whole():
    sd = {"worldview": "앞" + "중" * 2000 + "끝세계관", "desc_guide": {f"k{i}": f"가이드{i}" for i in range(8)}}
    ctx = core.profile_ai._scenario_context(sd)
    assert ctx.endswith("가이드7") and "끝세계관" in ctx


async def test_G06_proceed_history_is_whole_in_next_logic_packet(drig):
    r = drig
    r.make()
    long_body = "앞부분 " + "묘" * 1200 + " 중간표지 " + "사" * 600 + " 끝부분표지"
    r.prov.routes["narration"] = [FakeGenAIResponse(long_body, usage=_usage())]
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "주변을 살핀다")
    await turn(r, "계속 살핀다")
    text = r.prov.prompts["logic"][-1][0]
    assert "앞부분" in text and "중간표지" in text and "끝부분표지" in text
