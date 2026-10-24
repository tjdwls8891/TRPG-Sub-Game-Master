# -*- coding: utf-8 -*-
"""H05-B·C·P1 — 실제 자동 턴(GMCog/GameCog) 배선: 판단 매니페스트(+행동 소요 시간) → 계획(묘사 전 진행·선후 확정)
→ 지시·단일 묘사 오버레이(필수 서사) → 관찰 → 같은 분 보충 진행 → READY → CommitCoordinator(+전달 대기) → 다음 행동 질문.

provider는 호출 종류별 라우팅 대역(네트워크 없음). DYNAMIC 세션 = 정의 바인딩 + 시계 + PC 배치.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

import core
from core import player_world as PW
from core import spatial as SP
from core import turn_transaction as tt
from core import turn_world as TW
from core import world_clock as WC
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K, eq, ev_raw
from tests.policy.test_h05a_intent_planning import knowledge
from tests.policy.test_ready_barrier import _json, _usage, rig  # noqa: F401
from tests.policy.test_authoritative_commit import _fund

from tests.policy.world_bind import bind_open  # noqa: E402

pytestmark = pytest.mark.policy

PC = f"pc_{PLAYER_UID}"


class H05Provider:
    """판단·지시·시뮬·묘사·후속 묘사·추출을 설정/본문으로 구분해 라우팅."""

    def __init__(self):
        self.routes, self.calls, self.prompts = {}, [], {}

    @staticmethod
    def _text(kwargs):
        c = kwargs.get("contents")
        if isinstance(c, str):
            return c
        try:
            return c[-1].parts[0].text or ""
        except Exception:
            return ""

    def kind_of(self, kwargs):
        import cogs.gm as gm
        cfg = kwargs.get("config")
        sysi = getattr(cfg, "system_instruction", None)
        schema = getattr(cfg, "response_schema", None)
        if isinstance(kwargs.get("contents"), str):
            return "compression"
        if sysi == gm.EXTRACTION_SYSTEM_INSTRUCTION:
            return "extraction"
        if sysi == gm.JUDGMENT_SYSTEM_INSTRUCTION:
            return "judgment"
        if sysi == gm.IRREGULAR_NPC_SYSTEM_INSTRUCTION:
            return "irregular"
        if isinstance(schema, dict) and "proceed_instruction" in (schema.get("properties") or {}):
            return "logic"
        if schema is not None:
            return "simulation"
        if "[후속 묘사" in self._text(kwargs):
            return "continuation"                                         # P1: 생기면 안 된다(단일 묘사)
        return "narration"

    def generate_content(self, **kwargs):
        kind = self.kind_of(kwargs)
        self.calls.append(kind)
        self.prompts.setdefault(kind, []).append((self._text(kwargs), kwargs.get("config")))
        route = self.routes.get(kind)
        if callable(route):
            item = route(kwargs)
        else:
            seq = route or [RuntimeError(f"no route for {kind}")]
            item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item


def judgment(prompt_place=None, *, action="PROCEED", **intent):
    """판단 응답 — destination은 프롬프트 매니페스트에서 장소 키를 찾는다."""
    def route(kwargs):
        text = H05Provider._text(kwargs)
        ai = {"movement_kind": "NONE", "time_kind": "NONE"}
        if prompt_place:
            m = re.search(r"(P\d+)=" + re.escape(prompt_place) + r"\b", text)
            ai.update({"movement_kind": "DESTINATION", "destination": m.group(1) if m else "NONE"})
        ai.update(intent)
        return _json({"player_intent": "선언", "situation_read": "상황", "action": action,
                      "reasoning": "테스트", "bridge_message": "", "rolls": [], "action_intent": ai})
    return route


def extraction(obs=None, **legacy):
    body = {"situation": {}, **legacy}
    if obs is not None:
        body[TW.OBS_FIELD] = obs
    return _json(body)


def obs_key(provider, kind, label_part):
    text = provider.prompts["extraction"][-1][0]
    m = re.search(r"([XSO]\d+)=" + re.escape(label_part), text)
    return m.group(1) if m else None


@pytest.fixture
def drig(rig, monkeypatch):
    """DYNAMIC 세션으로 바꾼 rig(정본: 시계 K, PC 항구, 지식 = 항구→시장)."""
    r = rig
    prov = H05Provider()
    prov.routes.update({
        "narration": [FakeGenAIResponse("PC가 길을 따라 걸어간다.", usage=_usage())],
        "logic": [_json({"proceed_instruction": "선언대로 진행한다", "event_assessment": "ongoing"})],
        "simulation": [_json({})], "irregular": [_json({"assignments": []})],
        "extraction": [extraction({})], "judgment": judgment("시장")})
    r.bot.genai_client.models._provider = prov
    r.prov = prov

    async def _nosim(*a, **k):
        return None
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions", _nosim)
    r.make = lambda **kw: make_dynamic(r.sess, **kw)
    return r


def make_dynamic(s, *, templates=None, edges=("e_hm#fwd",), at="항구", extra_world=None, raw_patch=None):
    raw = ev_raw(templates or {})
    if raw_patch:
        raw_patch(raw)
    s.scenario_id = "t"
    s.scenario_data = raw
    res = core.definition_compiler.compile_scenario(raw, "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    bind_open(s, res)                         # H06: 스냅샷 바인딩 + OPEN
    s.world_clock = WC.initial_clock(K)
    s.actor_registry = {PC: {"kind": "PC", "display_name": "테스터", "source_kind": "PLAYER",
                             "source_key": str(PLAYER_UID), "aliases": []}}
    s.spatial_state = {"placements": {PC: SP.at_place(at)}, "scene_place": at}
    s.spatial_knowledge = {PC: knowledge(edges)}
    s.world_timeline = {"current_location": at, "time_of_day": "night"}
    for k, v in (extra_world or {}).items():
        setattr(s, k, copy.deepcopy(v))
    PW._DEF_CACHE.clear()
    assert PW.session_world_context(s).dynamic
    return res.definition


def world_of(s):
    return {f: copy.deepcopy(getattr(s, f, None)) for f in WS.GAMEPLAY_WORLD_FIELDS}


async def turn(r, text="시장으로 간다"):
    if not getattr(r, "_funded", False):
        await _fund(PLAYER_UID, 10_000)                                   # 잔액 검사 통과(기존 정책 그대로)
        r._funded = True
    orig, seen = tt.get_or_begin_turn_transaction, []

    def cap(session, decl):
        t = orig(session, decl)
        seen.append(t)
        return t
    tt.get_or_begin_turn_transaction = cap
    try:
        await r.gm._process_actions(r.sess, text, r.master)
    finally:
        tt.get_or_begin_turn_transaction = orig
    return seen[-1] if seen else tt.get_active_transaction(r.sess)


# ══════════════════════════════════════════════════════════════
# 배선 증명 · 정상 이동
# ══════════════════════════════════════════════════════════════

async def test_dynamic_turn_plans_with_manifest_and_commits_movement(drig):
    r = drig
    r.make()
    wv0 = copy.deepcopy(r.sess.world_version)
    r.prov.routes["extraction"] = [extraction({}, location={"name": "성당", "faction_context": "미확인"},
                                              datetime={"date": "1999-01-01", "time_of_day": "낮"})]
    await turn(r)
    jprompt, jcfg = r.prov.prompts["judgment"][0]
    assert "action_intent" in jcfg.response_schema["properties"]            # 같은 판단 호출(추가 호출 0)
    assert "P" in jprompt and "=시장" in jprompt and "성당" in jprompt     # PC가 이름을 아는 장소만
    nprompt = r.prov.prompts["narration"][0][0]
    assert "시장에 도착했다" in nprompt and "[세계 진행" in nprompt
    assert r.prov.calls.count("judgment") == 1 and "continuation" not in r.prov.calls
    s = r.sess
    assert SP.placement_of(s, PC) == SP.at_place("시장") and s.world_clock["minute_index"] == K + 5
    assert s.world_version != wv0
    # 레거시 장소·날짜는 정본 미러 — 추출의 '성당'·'1999'는 권위가 없다(평행 권위 없음)
    assert s.world_timeline["current_location"] == "시장" and "1999" not in str(s.world_timeline)
    assert r.rec["start_round"] == 1


async def test_legacy_session_path_unchanged(drig):
    r = drig
    r.sess.definition_revision = None                                    # LEGACY(현 운영 세션 전부)
    r.prov.routes["judgment"] = [_json({"player_intent": "x", "situation_read": "x", "action": "PROCEED",
                                        "reasoning": "x", "bridge_message": "", "rolls": []})]
    r.last_tx = await turn(r)
    jcfg = r.prov.prompts["judgment"][0][1]
    assert "action_intent" not in jcfg.response_schema["properties"]
    assert r.last_tx.world_turn is None
    assert r.rec["start_round"] == 1


async def test_zero_mutation_turn_still_advances_world_version(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None)
    before, wv0 = world_of(r.sess), copy.deepcopy(r.sess.world_version)
    await turn(r, "주변을 둘러본다")
    after = world_of(r.sess)
    assert r.sess.world_version != wv0 and WS.is_world_version(r.sess.world_version)
    assert {k: v for k, v in after.items() if k != "world_version"} == \
        {k: v for k, v in before.items() if k != "world_version"}


async def test_route_choice_asks_before_instruction_and_resumes_same_transaction(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd", "e_hh#fwd", "e_hc#fwd"))
    r.prov.routes["judgment"] = judgment("성당")
    tx = await turn(r, "성당으로 간다")
    assert "logic" not in r.prov.calls and "narration" not in r.prov.calls
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    assert any("1) " in (m.content or "") for m in r.gch.sent)       # v7.13: 경로 선택지(질문 문장은 마스터 대사)
    assert SP.placement_of(r.sess, PC) == SP.at_place("항구")
    # 플레이어가 두 번째 길을 고름 → 같은 트랜잭션, 매니페스트에 R 키 → 의미 서명으로 이동
    def pick(kwargs):
        text = H05Provider._text(kwargs)
        m = re.search(r"(R\d+)=산복도로", text)
        return judgment("성당", route_choice=m.group(1) if m else "NONE")(kwargs)
    r.prov.routes["judgment"] = pick
    tx2 = await turn(r, "산복도로 쪽으로")
    assert tx2.transaction_id == tx.transaction_id
    assert SP.placement_of(r.sess, PC) == SP.at_place("성당")
    # 이력에 남는 판단 의도는 의미 기록(경로 서명) — transient 키 없음
    noted = json.dumps(tx2.judgment_result or {}, ensure_ascii=False)
    assert '"R1"' not in noted and '"R2"' not in noted and '"P1"' not in noted
    assert '"route_signature": ["e_hh#fwd", "e_hc#fwd"]' in noted


# ══════════════════════════════════════════════════════════════
# H05-B 관찰 · 시간 · 실패
# ══════════════════════════════════════════════════════════════

async def test_code_owned_duration_not_double_counted(drig):
    r = drig
    r.make()
    r.prov.routes["extraction"] = [extraction({"extra_minutes": 30})]
    await turn(r)
    assert r.sess.world_clock["minute_index"] == K + 5                   # 이동 5분만(추출 30분 버림)
    ext_prompt = r.prov.prompts["extraction"][0][0]
    assert "코드가 정했다" in ext_prompt


async def test_action_minutes_advance_before_narration_and_extraction_time_is_ignored(drig):
    # P1(D1): 이동·대기 아닌 행동의 소요 시간은 판단층위가 제안하고 묘사 전에 코드가 진행한다(이중 가산 없음)
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, action_minutes=20)
    r.prov.routes["extraction"] = [extraction({"extra_minutes": 45})]
    await turn(r, "벽의 낙서를 천천히 읽는다")
    assert r.sess.world_clock["minute_index"] == K + 20                  # 판단 제안 20분만(추출 45분 버림)
    assert "경과 시간: 20분" in r.prov.prompts["narration"][0][0]         # 묘사가 이미 흐른 시간을 안다
    assert "코드가 정했다" in r.prov.prompts["extraction"][0][0]
    assert "action_minutes" in r.prov.prompts["judgment"][0][0]          # 판단층위 안내(런타임 블록)


@pytest.mark.parametrize("ai", [{}, {"action_minutes": -5}, {"action_minutes": True}])
async def test_missing_or_invalid_action_minutes_never_invent_time(drig, ai):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, **ai)
    r.prov.routes["extraction"] = [extraction({"extra_minutes": 30})]
    await turn(r, "주위를 둘러본다")
    assert r.sess.world_clock["minute_index"] == K                       # 근거 없는 시간 없음(추출 시간도 안 씀)


async def test_explicit_wait_wins_over_action_minutes(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT",
                                         action_minutes=40)
    await turn(r, "10분 기다린다")
    assert r.sess.world_clock["minute_index"] == K + 10                  # 명시 대기만(행동 시간 중복 없음)


def _npc_knows_world():
    return {"spatial_state": {"placements": {PC: SP.at_place("항구"), KANG: SP.at_place("항구")},
                              "scene_place": "항구"},
            "cognition_state": {f"actor:{KANG}": {"S40": {
                "awareness": "CONTENT", "fragments": [], "belief": None, "acquisitions": [],
                "belief_provenance": [], "secrecy_awareness": None}}}}


def _arm_evidence(r):
    """이번 턴 노출 근거 — 활성 사건이 3분 뒤 장면(항구)에 벽보 정보를 만든다(R2: 사건이 만든 정보만 후보)."""
    eff = {"type": "CREATE_INFORMATION", "proposition": "벽보: 오늘 밤 항구 폐쇄", "summary": "벽보의 공지",
           "subject_refs": ["place:항구"]}
    tpl = {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
           "phases": {"calm": {"eligible_when": {"const": True}, "priority": 1}},
           "transition_cases": [{"case_id": "c", "from": None, "to": "calm", "steps": [
               {"step_id": "post", "type": "SCHEDULE_EFFECT", "after_minutes": 3, "effect": eff}]}]}
    D = r.make(templates={"ev": tpl}, extra_world=_npc_knows_world())
    arm = core.world_progression.project_world_progression(
        D, r.sess, K, base_ops=[core.world_mutation.make_op(
            "arm", "ACCESS_STATE_SET", "TEST", {"control_id": "door_2", "state": "BARRED"})],
        narrative_seed=r.sess.narrative_seed or r.sess.session_id)
    for f in WS.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(r.sess, f, copy.deepcopy(arm.projected_world.domain(f)))
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")


async def test_observation_is_staged_until_commit_and_never_sets_pc_belief(drig, monkeypatch):
    r = drig
    _arm_evidence(r)

    def ext(kwargs):
        text = H05Provider._text(kwargs)
        x = re.search(r"(X\d+)=벽보의 공지", text).group(1)
        return extraction({"info_exposed": [x]})
    r.prov.routes["extraction"] = ext
    seen = {}
    orig = core.turn_preparation.transition_to_ready

    def _ready(session, prep):
        seen["cog_at_ready"] = copy.deepcopy(session.cognition_state)
        return orig(session, prep)
    monkeypatch.setattr(core.turn_preparation, "transition_to_ready", _ready)
    await turn(r, "벽보 앞에서 10분 기다린다")
    assert f"actor:{PC}" not in seen["cog_at_ready"]                     # READY까지 정본 미변경
    recs = r.sess.cognition_state.get(f"actor:{PC}") or {}
    assert len(recs) == 1 and "S40" not in recs                           # 같은 장면 NPC의 정보는 노출 근거 아님
    (rec,) = recs.values()
    assert rec["awareness"] == "CONTENT" and rec["belief"] is None       # 노출 ≠ 믿음


async def test_hidden_truth_not_in_prompts(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd"))
    r.sess.spatial_knowledge[PC]["places"].pop("성당")                     # PC가 성당을 모름
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r)
    jprompt = r.prov.prompts["judgment"][0][0]
    nprompt = r.prov.prompts["narration"][0][0]
    eprompt = r.prov.prompts["extraction"][0][0]
    assert "=성당" not in jprompt                                         # 모르는 장소는 후보 아님
    for p in (jprompt, nprompt, eprompt):
        assert "LOCKED" not in p and "door_1" not in p and "원본 기록은 성당 제3보관실" not in p


def _due(sid, due, ctl, state, prio=0):
    return {"schedule_id": sid, "due_minute": due, "kind": "DEFERRED_EFFECT", "owner_ref": "event:evt_fx",
            "payload_ref": f"fx|c|{sid}", "created_by_version": None, "order_key": sid,
            "payload": {"instance_id": "evt_fx", "case_id": "c", "step_id": sid}, "guard_ref": None,
            "owner_epoch": None, "conflict_priority": prio}


def _fx(r, ctl, states=("OPEN", "BARRED"), at="시장"):
    steps = {"a": {"type": "SET_ACCESS_STATE", "control": ctl, "state": states[0]},
             "b": {"type": "SET_ACCESS_STATE", "control": ctl, "state": states[1]}}
    tpl = {"version": 1, "spawn_when": {"const": False},
           "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}},
           "transition_cases": [{"case_id": "c", "from": None, "to": "open", "steps": [
               {"step_id": sid, "type": "SCHEDULE_EFFECT", "after_minutes": 1, "effect": eff, "epoch_bound": False}
               for sid, eff in steps.items()]}]}
    inst = {"instance_id": "evt_fx", "template_id": "fx", "template_version": 1, "bound_slots": {},
            "lifecycle": "ACTIVE", "current_phase": "open", "phase_entered_at": K, "transition_ordinal": 0,
            "occurrence_ordinal": 1, "local_state": {}, "history": []}
    r.make(templates={"fx": tpl}, at=at, edges=("e_mc#fwd",), extra_world={
        "event_instances": {"evt_fx": inst},
        "world_schedule": {"a": _due("a", K + 5, ctl, states[0]), "b": _due("b", K + 5, ctl, states[1])}})


def _logic_tie(pick):
    """지시층위 응답 — 프롬프트의 선후 블록에서 O키를 읽어 pick(keys)을 tie_order로 낸다."""
    def route(kwargs):
        keys = re.findall(r"- (O\d+): ", H05Provider._text(kwargs))
        out = {"proceed_instruction": "선언대로 진행한다", "event_assessment": "ongoing"}
        val = pick(keys)
        if val is not None:
            out["tie_order"] = val
        return _json(out)
    return route


async def test_foreground_same_minute_tie_order_chosen_by_instruction_before_narration(drig):
    # P1(D2): 남은 자유 경합은 묘사 전에 지시층위가 고른다 — 묘사는 확정된 결과만 받는다
    r = drig
    _fx(r, "door_1")                                                      # door_1 = 시장 장면 관련
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    r.prov.routes["logic"] = _logic_tie(lambda keys: keys[1])            # 두 번째 허용 순서
    await turn(r, "10분 기다린다")
    lprompt = r.prov.prompts["logic"][0][0]
    assert "tie_order" in lprompt and "- O1: " in lprompt and "- O2: " in lprompt
    assert "같은 순간" not in r.prov.prompts["narration"][0][0]           # 묘사에 선후 선택을 넘기지 않는다
    assert r.sess.world_clock["minute_index"] == K + 10 and r.sess.world_schedule == {}
    # O2 = (due:b → due:a) — 뒤 효과(a: OPEN)가 최종값. 정본 분은 바뀌지 않는다(K+5 일괄, K+10 종료)
    assert r.sess.access_control_state["door_1"] == {"state": "OPEN"}


@pytest.mark.parametrize("pick", [lambda keys: "NONE", lambda keys: "O9", lambda keys: None])
async def test_invalid_tie_choice_falls_back_to_deterministic_code_order(drig, pick):
    r = drig
    _fx(r, "door_1")
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    r.prov.routes["logic"] = _logic_tie(pick)
    tx = await turn(r, "10분 기다린다")
    assert tx.status == tt.TurnStatus.COMMITTED                          # 무효 선택은 코드가 정한다(실패 아님)
    # O1 = (due:a → due:b) — 정렬 첫 허용 순서(재시도 결정적): 뒤 효과 b(BARRED)가 최종값
    assert r.sess.access_control_state["door_1"] == {"state": "BARRED"}


async def test_offscreen_conflict_is_not_a_tie_and_stops_before_narration(drig):
    r = drig
    _fx(r, "door_2")                                                      # door_2 = 장면 밖
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    before = world_of(r.sess)
    tx = await turn(r, "10분 기다린다")
    assert "narration" not in r.prov.calls and "extraction" not in r.prov.calls   # 묘사 전 중단
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.failure_code == tt.FailureCode.WORLD_PROJECTION_CONFLICT
    assert world_of(r.sess) == before and not r.sess.extraction_pending


async def test_action_time_conflict_stops_before_narration(drig):
    # P1(D1): 행동 시간도 묘사 전에 진행한다 — 그 시간이 넘는 장면 밖 충돌은 묘사 호출 없이 끝난다
    r = drig
    _fx(r, "door_2")
    r.prov.routes["judgment"] = judgment(None, action_minutes=10)
    before = world_of(r.sess)
    tx = await turn(r, "한참 생각에 잠긴다")
    assert "narration" not in r.prov.calls and "extraction" not in r.prov.calls
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.failure_code == tt.FailureCode.WORLD_PROJECTION_CONFLICT
    assert world_of(r.sess) == before and not r.sess.extraction_pending


async def test_main_narration_failure_discards_plan(drig):
    r = drig
    r.make()
    r.prov.routes["narration"] = [RuntimeError("묘사 실패")]
    before = world_of(r.sess)
    tx = await turn(r)
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and world_of(r.sess) == before


async def test_extraction_failure_keeps_pre_state_then_retry_commits(drig):
    r = drig
    r.make()
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]
    before = world_of(r.sess)
    tx = await turn(r)
    assert r.sess.extraction_pending and world_of(r.sess) == before
    assert tx.preparation.phase == core.turn_preparation.PREP_RETRY_PENDING
    r.prov.routes["extraction"] = [extraction({})]
    out = await r.gm._retry_prepared_extraction(r.sess)
    assert out in ("ready", "committed", True) or SP.placement_of(r.sess, PC) == SP.at_place("시장")
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")


# ══════════════════════════════════════════════════════════════
# P1 #1 — 후속 묘사 없음 · 단일 본문 필수 서사 · 전달 대기
# ══════════════════════════════════════════════════════════════

def _alert_tpl(observable_place="항구", after=5):
    """활성 사건의 예약 효과 — after분 뒤 현재 장면 신호. 행동 소요 시간이 이 시각을 넘으면 새 결과."""
    sig = {"step_id": "draw", "type": "EMIT_SIGNAL", "signal_id": "guard_draws", "cue": "경비가 칼을 뽑아 든다",
           "interrupt_policy": "IF_OBSERVABLE", "observable_when": eq({"ref": "SCENE_PLACE"}, observable_place)}
    return {"version": 1, "spawn_when": eq({"ref": "ACCESS_STATE", "control": "door_2"}, "BARRED"),
            "phases": {"calm": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "calm", "steps": [
                {"step_id": "later", "type": "SCHEDULE_EFFECT", "after_minutes": after, "effect": sig}]}]}


def _arm_alert(r, **kw):
    D = r.make(templates={"alert": _alert_tpl(**kw)})
    res = core.world_progression.project_world_progression(
        D, r.sess, K, base_ops=[core.world_mutation.make_op(
            "arm", "ACCESS_STATE_SET", "TEST", {"control_id": "door_2", "state": "BARRED"})],
        narrative_seed=r.sess.narrative_seed or r.sess.session_id)
    assert res.status == "COMPLETED"
    for f in WS.GAMEPLAY_WORLD_FIELDS:
        if f != "world_version":
            setattr(r.sess, f, copy.deepcopy(res.projected_world.domain(f)))
    r.prov.routes["judgment"] = judgment(None, action_minutes=10)       # 대화 10분(판단 제안 — 묘사 전 진행)


def _expose(represent=False):
    def ext(kwargs):
        text = H05Provider._text(kwargs)
        obs = {}
        if represent:
            obs["represented_signals"] = [re.search(r"(S\d+)=경비가", text).group(1)]
        return extraction(obs)
    return ext


async def test_action_time_reaction_is_in_the_single_narration(drig, monkeypatch):
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=True)
    order = []

    async def start_round(session):
        order.append("next_prompt")
    monkeypatch.setattr(r.gm, "_start_round", start_round)
    tx = await turn(r, "한동안 강지수와 이야기한다")
    nprompt = r.prov.prompts["narration"][0][0]
    assert "경비가 칼을 뽑아 든다" in nprompt and "필수 서사" in nprompt      # 반응이 본문 입력에 필수로
    assert r.prov.calls.count("narration") == 1 and "continuation" not in r.prov.calls
    assert r.sess.world_clock["minute_index"] == K + 5                   # 신호가 대화를 끊음(5분)
    assert r.sess.world_pending_signals == []                            # 표현 확인 — 대기 없음
    ev = [e for e in r.ledger.list_cost_events() if e.get("transaction_id") == tx.transaction_id]
    assert not any(e.get("operation") == "TURN_FOREGROUND_CONTINUATION" for e in ev)
    assert order == ["next_prompt"] and r.prov.calls.count("judgment") == 1 and r.sess.gm_turns_done == 1


async def test_unrepresented_plan_signal_is_not_carried_to_next_body(drig):
    # v7.21.1(사용자 정책 A): 묘사 전 계획 신호는 이번 지시·묘사에 이미 주었다 — 표현 판정과 무관하게 이월 없음.
    #   묘사 후 보충 진행 신호의 1회 전달·반복 이월 제거는 tests/policy/test_v7211_pending_lifecycle.py.
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=False)              # 추출이 본문 표현을 확인하지 못함
    await turn(r, "한동안 강지수와 이야기한다")
    assert "continuation" not in r.prov.calls
    assert r.sess.world_pending_signals == []
    r.prov.routes["judgment"] = judgment(None, action_minutes=0)
    await turn(r, "주위를 살핀다")
    nprompt = r.prov.prompts["narration"][-1][0]
    assert "아직 전하지 못한 일" not in nprompt and "경비가 칼을 뽑아 든다" not in nprompt


@pytest.mark.parametrize("kw", [{"observable_place": "외딴곳"}, {"after": 30}])
async def test_offscreen_transition_or_future_schedule_creates_no_obligation(drig, kw):
    r = drig
    _arm_alert(r, **kw)
    r.prov.routes["extraction"] = _expose()
    await turn(r, "한동안 강지수와 이야기한다")
    assert "경비가 칼을 뽑아 든다" not in r.prov.prompts["narration"][0][0], kw   # 비가시·미래 사건 비노출
    assert r.sess.world_clock["minute_index"] == K + 10 and r.sess.world_pending_signals == []


async def test_already_represented_signal_has_no_duplicate_delivery(drig):
    # R2: 표현된 신호 후보는 본 묘사에 실제로 준 신호뿐 — 미래 사건 신호는 후보가 아니다
    r = drig
    _arm_alert(r)
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=10, activity="WAIT")
    r.prov.routes["extraction"] = _expose(represent=True)
    await turn(r, "10분 기다린다")
    assert "경비가 칼을 뽑아 든다" in r.prov.prompts["narration"][0][0]
    assert re.search(r"S\d+=경비가", r.prov.prompts["extraction"][0][0])
    assert "continuation" not in r.prov.calls and r.sess.world_pending_signals == []
    assert r.sess.world_clock["minute_index"] == K + 5                   # 신호가 대기를 끊음 — 커밋됨(공허한 통과 아님)


async def test_commit_failure_keeps_pending_and_world_unchanged(drig, monkeypatch):
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=False)

    def boom(*a, **k):
        raise OSError("디스크 실패(테스트)")
    monkeypatch.setattr(core.io, "write_session_strict_locked", boom, raising=False)
    monkeypatch.setattr(core.commit_coordinator, "write_session_strict_locked", boom, raising=False)
    before = world_of(r.sess)
    await turn(r, "한동안 강지수와 이야기한다")
    assert world_of(r.sess) == before and r.sess.world_pending_signals == []
    main = [m for m in r.gch.sent if "길을 따라" in (m.content or "")]
    assert main and all(m.deleted for m in main)                         # 커밋 전 본 묘사는 기존 수명주기로 정리


def test_pending_delivery_field_is_reversible_canonical():
    # 되감기·재서술이 그 턴 이전 대기로 돌아간다(같은 이력 단위)
    assert "world_pending_signals" in core.turn_history.REVERSIBLE_FIELDS
    assert "world_pending_signals" in core.turn_preparation.CANONICAL_DOMAINS
    assert core.SESSION_FIELDS["world_pending_signals"] == []


# ══════════════════════════════════════════════════════════════
# H05-C 투영 미디어 · 디스플레이
# ══════════════════════════════════════════════════════════════

def _with_images(raw):
    raw["places"]["시장"]["image"] = "market.png"
    raw["places"]["성당"]["image"] = "church.png"


async def test_successful_move_media_uses_projected_destination(drig):
    r = drig
    r.make(raw_patch=_with_images)
    tx = await turn(r)
    lprompt = r.prov.prompts["logic"][0][0]
    assert "시장" in lprompt.split("[사용 가능한 장소 이미지 목록")[1][:200]
    import cogs.game as game
    picked = game._projected_media_filter(r.sess, tx.preparation, [], None)
    assert picked == ["시장"]


async def test_interrupted_move_cannot_reveal_destination_image(drig):
    r = drig
    r.make(raw_patch=_with_images, edges=("e_hm#fwd", "e_mc#fwd"))
    r.prov.routes["judgment"] = judgment("성당")
    tx = await turn(r, "성당으로 간다")                                   # door_1 숨은 잠금 → 시장에서 멈춤
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")
    lprompt = r.prov.prompts["logic"][0][0]
    # 이미지 목록 블록만(다음 '[' 머리글 전까지) — 고정 길이로 자르면 뒤 블록(플레이어 발언)까지 섞인다
    imgs = lprompt.split("[사용 가능한 장소 이미지 목록")[1].split("\n[")[0] if "[사용 가능한 장소 이미지 목록" in lprompt else ""
    assert "성당" not in imgs
    import cogs.game as game
    assert "성당" not in game._projected_media_filter(r.sess, tx.preparation, ["성당"], None)
    assert "시장" in game._projected_media_filter(r.sess, tx.preparation, ["시장"], None)


async def test_display_reads_canonical_until_commit(drig, monkeypatch):
    r = drig
    r.make()
    seen = {}
    orig = core.turn_preparation.transition_to_ready

    def _ready(session, prep):
        seen["tl"] = copy.deepcopy(session.world_timeline)
        seen["pl"] = copy.deepcopy(SP.placement_of(session, PC))
        return orig(session, prep)
    monkeypatch.setattr(core.turn_preparation, "transition_to_ready", _ready)
    await turn(r)
    assert seen["tl"]["current_location"] == "항구" and seen["pl"] == SP.at_place("항구")
    assert r.sess.world_timeline["current_location"] == "시장"


# ══════════════════════════════════════════════════════════════
# 배선 소스 증명
# ══════════════════════════════════════════════════════════════

def test_automatic_turn_production_wiring_exists():
    import inspect
    import cogs.gm as gm
    src = inspect.getsource(gm.GMCog)
    for needle in ("core.player_world.plan_player_turn", "core.turn_world.process_observation",
                   "core.action_intent.extend_judgment_schema", "core.turn_preparation.stage_world_ops",
                   "core.turn_world.narration_overlay", "core.turn_world.resolve_plan_tie",
                   "core.turn_world.pending_delivery_block"):
        assert needle in src, needle
    assert "_generate_continuation" not in src and "TURN_FOREGROUND_CONTINUATION" not in src   # P1: 후속 묘사 은퇴
    # H02~H04 도메인 엔진은 cogs에서 직접 부르지 않는다(H05 모듈 경유)
    for banned in ("project_world_progression", "plan_known_routes", "plan_actual_traversal"):
        assert banned not in src, banned


@pytest.mark.parametrize("display_fails", [False, True])
async def test_committed_display_refresh_precedes_next_prompt_and_failure_is_bounded(drig, monkeypatch, display_fails):
    # P1 W11: COMMITTED 뒤 정본 기반 표시 갱신 → 다음 행동 질문. 표시 실패는 재커밋·재청구·무한 대기가 아니다.
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=True)
    order = []
    orig_commit = r.gm._commit_ready_turn

    async def commit(*a, **k):
        res = await orig_commit(*a, **k)
        order.append("committed")
        return res
    monkeypatch.setattr(r.gm, "_commit_ready_turn", commit)

    async def display(bot, session, reason=None, **kw):
        order.append(f"display:{reason}")
        if display_fails:
            raise RuntimeError("디스코드 편집 실패(테스트)")
    monkeypatch.setattr(core, "refresh_display", display)

    async def start_round(session):
        order.append("next_prompt")
    monkeypatch.setattr(r.gm, "_start_round", start_round)
    tx = await turn(r, "한동안 강지수와 이야기한다")
    assert tx.status == tt.TurnStatus.COMMITTED
    assert order == ["committed", "display:turn_end", "next_prompt"]
    assert r.sess.world_clock["minute_index"] == K + 5                    # 표시 실패가 정본을 되돌리지 않음
    assert sum(1 for e in r.ledger.list_cost_events() if e.get("operation") == "TURN_NARRATION"
               and e.get("transaction_id") == tx.transaction_id) == 1             # 재생성·재청구 없음
