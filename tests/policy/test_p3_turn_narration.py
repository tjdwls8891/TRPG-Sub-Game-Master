"""P3 — NARRATE 비진행 경계(G1)·지시층위 세계 시점 구별(G2)·지시층위 계약 정돈(C-a·b·c)·같은 턴 실제 공개 표시(G3).

가짜 provider 응답 + fixture — 코드 경로 검증이며 실제 Gemini의 준수·서사 품질 보장이 아니다.
"""
from __future__ import annotations

import asyncio
import re

import pytest

import core
import prompts
from cogs.game import GameCog
from core import speaker_card as SC
from core import turn_world as TW
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401 — DYNAMIC 턴 하네스 재사용
    _arm_alert, _expose, drig, judgment, rig, turn)
from tests.policy.test_ready_barrier import _json
from tests.policy.test_svg_speaker_card import (  # noqa: F401 — 카드 하네스 재사용
    AID, DEFINITION, _deliver, _known, _narr, card_session)

CUE = "경비가 칼을 뽑아 든다"


def _narrate_logic():
    return [_json({"narrate_instruction": "짧게 답한다", "proceed_instruction": "", "event_assessment": "ongoing"})]


# ── G1 · NARRATE 비진행 경계 ─────────────────────────────────────

async def test_A1_narrate_gets_no_unexecuted_plan_and_world_is_unchanged(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["judgment"] = judgment(None, action="NARRATE", action_minutes=10)
    r.prov.routes["logic"] = _narrate_logic()
    clock0, pending0 = dict(r.sess.world_clock), list(r.sess.world_pending_signals)
    await turn(r, "강지수에게 뭘 하느냐고 묻는다")
    lp = r.prov.prompts["logic"][-1][0]
    assert "경과 시간" not in lp and "필수 서사" not in lp and CUE not in lp      # 미실행 계획 비노출
    assert "[세계 진행" not in lp and "이번 턴 진행 후" not in lp
    assert "[이번 응답 — 세계 진행 없음" in lp and TW.NARRATE_HOLD_TEXT in lp
    np_ = r.prov.prompts["narration"][-1][0]
    assert "[이번 응답 — 세계 진행 없음" in np_ and CUE not in np_                # 경량 묘사에도 같은 경계
    assert r.sess.world_clock == clock0 and r.sess.world_pending_signals == pending0
    assert "continuation" not in r.prov.calls


async def test_A2_proceed_keeps_world_progress_in_the_same_turn(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=True)
    await turn(r, "한동안 강지수와 이야기한다")
    lp, np_ = r.prov.prompts["logic"][-1][0], r.prov.prompts["narration"][-1][0]
    assert CUE in lp and CUE in np_ and "경과 시간" in lp and "경과 시간" in np_
    assert "[이번 응답 — 세계 진행 없음" not in lp


async def test_A4_narrate_does_not_touch_pending_delivery(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=False)
    await turn(r, "한동안 강지수와 이야기한다")                          # 미표현 → 전달 대기 1건
    (before,) = [dict(x) for x in r.sess.world_pending_signals]
    r.prov.routes["judgment"] = judgment(None, action="NARRATE", action_minutes=0)
    r.prov.routes["logic"] = _narrate_logic()
    await turn(r, "지금 몇 시쯤이지?")
    assert "아직 전하지 못한 일" not in r.prov.prompts["logic"][-1][0]
    assert [dict(x) for x in r.sess.world_pending_signals] == [before]          # 소모·횟수 변화 없음
    assert TW.MAX_PENDING_CARRY == 3


# ── G2 · 지시층위 세계 시점 ──────────────────────────────────────

async def test_B1_instruction_separates_start_and_after_states_matching_narration(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["judgment"] = judgment(None, action_minutes=30)
    r.prov.routes["extraction"] = _expose(represent=True)
    await turn(r, "한동안 강지수와 이야기한다")
    lp, np_ = r.prov.prompts["logic"][-1][0], r.prov.prompts["narration"][-1][0]
    assert "[현재 세계 상태" not in lp                                          # 표시 없는 턴 전 '현재' 없음
    pre = re.search(r"\[세계 상태 — 행동 시작 전\(정본\)\]\n날짜·시각: ([^\n]+)", lp).group(1)
    post = re.search(r"\[세계 상태 — 이번 턴 진행 후[^\n]*\]\n날짜·시각: ([^\n]+)", lp).group(1)
    assert pre != post
    assert f"· {post}" in np_                                                   # 묘사층위 정본 시각과 같은 종료 투영


def test_A3_legacy_world_block_unchanged(session_auto_ready):
    s = session_auto_ready
    s.world_timeline = {"current_date": "D1", "time_of_day": "낮", "current_location": "항구",
                        "faction_context": "x"}
    lp = core_logic_prompt(s)
    assert "[현재 세계 상태 — 세력 배치·지역 규칙 기반 개연성 판단의 기준]" in lp and "날짜: D1" in lp
    assert "[세계 상태 — 행동 시작 전" not in lp


def core_logic_prompt(s, action="PROCEED"):
    import cogs.gm as gm_mod
    return gm_mod._build_logic_user_prompt(s, "둘러본다", [], world_turn=None, action=action)


# ── C · 지시층위 계약 ───────────────────────────────────────────

def test_C1_thinking_procedure_matches_schema():
    sysi = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    props = prompts.GM_LOGIC_RESPONSE_SCHEMA["properties"]
    assert "player_intent" not in props and "situation_read" not in props       # 필드 증설 없음
    assert "**player_intent**" not in sysi and "**situation_read**" not in sysi
    assert "situation_read" not in sysi and "JSON 필드 선언 순서와 일치" not in sysi
    assert "1·2는 응답 JSON에 별도 필드가 없습니다" in sysi


def test_C2_no_instruction_to_reselect_action(session_auto_ready):
    lp = core_logic_prompt(session_auto_ready)
    assert "다음 단일 action" not in lp and "진행 유형은 바꾸지 않습니다" in lp
    assert "action" not in prompts.GM_LOGIC_RESPONSE_SCHEMA["properties"]


def test_C3_weaving_guidance_and_boundaries_present():
    sysi = prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "행동 → 결과 → 주변의 반응 → 다음 행동의 여지" in sysi
    assert "세계의 일을 목록처럼 나열하지 말 것" in sysi
    assert "NARRATE는 세계를 진행시키지 않습니다" in sysi
    for banned in ("필수 사건", "선택 사건"):                                   # 새 분류 없음
        assert banned not in sysi


# ── G3 · 같은 턴 실제 공개 ─────────────────────────────────────

class _Prep:
    def __init__(self, fut, names=(), faces=(), reveals=None):
        self.fut = fut
        self.card_variations = None
        self.identity_reveals = reveals
        self.world_turn = TW.WorldTurn(ctx=None, identified_names=tuple(names), identified_faces=tuple(faces))

    def task_future(self, name):
        return self.fut if name == "extraction" else None


def _headers(ch):
    return [m.group(1) for m in (re.search(r"## ▍(\S+)", str(x.content)) for x in ch.sent) if m]


PARAS = ["@대사:김노인|누구냐.", "@대사:김노인|나는 김노인이다, 이 항구를 지키지.", "@대사:김노인|따라오게."]


async def _run(wired_bot, s, game_channel, master_channel, prep):
    return await _deliver(GameCog(wired_bot), s, _narr(PARAS), game_channel, master_channel, prep)


async def test_D2_name_shown_only_after_actual_reveal_in_same_body(wired_bot, card_session, game_channel,
                                                                  master_channel):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(None)
    prep = _Prep(fut, names=(AID,), reveals={"i.name": 1})                      # 두 번째 문단에서 공개
    res = await _run(wired_bot, card_session, game_channel, master_channel, prep)
    assert res.ok and _headers(game_channel) == ["???", "???", "김노인"]          # 공개 문단 자신·이전은 익명
    assert "i.name" not in SC.known_infos(card_session, "pc_x")                  # 정본 인지 조기 변경 없음


async def test_D1_D4_candidate_without_reveal_stays_masked(wired_bot, card_session, game_channel, master_channel):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(None)
    prep = _Prep(fut, names=(AID,), faces=(AID,), reveals={})
    await _run(wired_bot, card_session, game_channel, master_channel, prep)
    assert _headers(game_channel) == ["???", "???", "???"]


async def test_D3_face_reveal_does_not_reveal_name(wired_bot, card_session, game_channel, master_channel):
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(None)
    prep = _Prep(fut, names=(AID,), faces=(AID,), reveals={"i.face": 0})
    seen = []
    real = SC.deliver_card

    async def spy(channel, session, ctx, preparation=None, collector=None, **kw):
        seen.append(ctx.known)
        return await real(channel, session, ctx, preparation, collector, **kw)
    import unittest.mock as um
    with um.patch.object(SC, "deliver_card", spy):
        await _run(wired_bot, card_session, game_channel, master_channel, prep)
    assert _headers(game_channel) == ["???", "???", "???"]                      # 이름은 여전히 미공개
    assert ["i.face" in k for k in seen] == [False, True, True]                  # 얼굴만, 공개 뒤부터


async def test_D6_already_known_needs_no_wait(wired_bot, card_session, game_channel, master_channel):
    _known(card_session, "i.name", "i.face")
    never = asyncio.get_running_loop().create_future()                         # 기다리면 끝나지 않는다
    prep = _Prep(never, names=(AID,), faces=(AID,), reveals=None)
    await asyncio.wait_for(_run(wired_bot, card_session, game_channel, master_channel, prep), timeout=5)
    assert _headers(game_channel) == ["김노인"] * 3


async def test_D7_extraction_failure_keeps_mask(wired_bot, card_session, game_channel, master_channel):
    fut = asyncio.get_running_loop().create_future()
    fut.set_exception(RuntimeError("추출 실패"))
    prep = _Prep(fut, names=(AID,), reveals=None)
    await _run(wired_bot, card_session, game_channel, master_channel, prep)
    assert _headers(game_channel) == ["???", "???", "???"]


def test_D5_parse_reveals_is_conservative(card_session):
    man = {"R1": (AID, "name", "i.name"), "R2": (AID, "face", "i.face")}
    paras = list(PARAS)
    ok = SC.parse_reveals(man, [{"key": "R1", "quote": "나는 김노인이다, 이 항구를"}], paras, DEFINITION, card_session)
    assert ok == {"i.name": 1}
    assert SC.parse_reveals(man, [{"key": "R1", "quote": "따라오게."}], paras, DEFINITION, card_session) == {}  # 이름 없음
    assert SC.parse_reveals(man, [{"key": "R1", "quote": "김노인이 죽었다는 소문"}], paras, DEFINITION,
                            card_session) == {}                                  # 본문에 없는 인용
    assert SC.parse_reveals(man, [{"key": "R9", "quote": "나는 김노인이다"}], paras, DEFINITION, card_session) == {}
    assert SC.parse_reveals(man, [{"key": "R2", "quote": "누구"}], paras, DEFINITION, card_session) == {}  # 너무 짧음
    assert SC.parse_reveals(man, "x", paras, DEFINITION, card_session) == {}


def test_reveal_manifest_skips_known_and_non_candidates(card_session):
    _known(card_session, "i.face")
    world = SC.card_world(card_session)
    wt = TW.WorldTurn(ctx=None, identified_names=(AID,), identified_faces=(AID,))
    assert list(SC.reveal_manifest(world, wt, card_session).values()) == [(AID, "name", "i.name")]
    assert SC.reveal_manifest(world, TW.WorldTurn(ctx=None), card_session) == {}
    block = SC.reveal_block({"R1": (AID, "name", "i.name")}, DEFINITION, card_session)
    assert "소문·추측·부정" in block and "R1: 김노인 — 이름" in block


def test_commit_only_takes_confirmed_reveals(card_session, monkeypatch):
    import cogs.gm as gm_mod
    monkeypatch.setattr(core.speaker_card, "config_of", lambda sd: SC.card_world(card_session).config)
    prep = type("P", (), {"identity_reveals": {"i.name": 1}})()
    assert gm_mod.GMCog._revealed_identity_ids(card_session, prep, (AID,), "name") == ("i.name",)
    assert gm_mod.GMCog._revealed_identity_ids(card_session, prep, (AID,), "face") == ()   # 후보만 — 정본 아님
    prep.identity_reveals = None
    assert gm_mod.GMCog._revealed_identity_ids(card_session, prep, (AID,), "name") == ()


def test_E1_no_continuation_callers():
    import pathlib
    root = pathlib.Path(core.__file__).resolve().parents[1]
    for rel in ("cogs/gm.py", "cogs/game.py", "core/turn_world.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "_generate_continuation" not in src and "ForegroundContinuationPlan" not in src
