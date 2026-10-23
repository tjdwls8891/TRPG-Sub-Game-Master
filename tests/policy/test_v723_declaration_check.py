# -*- coding: utf-8 -*-
"""v7.23 DYNAMIC 개연성 시뮬레이션 해체 — 매 발화 서사 방향 시뮬레이션 0 · 판단 뒤 조건부 선언 검증 ·
대기 질문 응답 우선(경로 번호·목적지·판단 ASK) · 지시층위 세계 반응 권위 · 예상 비용 · LEGACY 불변.

provider는 호출 종류별 라우팅 대역(네트워크 없음). 선언 검증 응답은 'simulation' 라우트(같은 게이트 스키마) —
서사 방향 시뮬레이션과는 응답 스키마로 구별한다(directions 유무).
"""
from __future__ import annotations

import json
import os
import re

import pytest

import cogs.gm as gm_mod
import core
from core import logs as L
from core import spatial as SP
from core import turn_transaction as tt
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, drig, judgment, rig, turn)
from tests.policy.test_ready_barrier import _json
from tests.policy.test_v714_discovery_assertion import ambient_ids, droll, jdg, jfind, sim

pytestmark = pytest.mark.policy

ROUTES = ("e_hm#fwd", "e_mc#fwd", "e_hh#fwd", "e_hc#fwd")


@pytest.fixture
def crig(drig, monkeypatch):
    """DYNAMIC rig + 실제 시뮬레이션 메서드 + 캐시 유효(옛 조건이면 매 발화 시뮬레이션이 돌았을 상태)."""
    r = drig
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions",
                        gm_mod.GMCog._simulate_narrative_directions.__get__(r.gm))
    r.sim_calls = []
    real = r.gm._simulate_narrative_directions

    async def spy(*a, **k):
        r.sim_calls.append(a[1] if len(a) > 1 else None)
        return await real(*a, **k)
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions", spy)
    r.make(edges=ROUTES)
    r.sess.cache_name, r.sess.cache_model = "cachedContents/test", core.DEFAULT_MODEL
    r.prov.routes["simulation"] = [sim()]
    return r


def _schemas(r, kind="simulation"):
    return [getattr(cfg, "response_schema", None) or {} for _t, cfg in r.prov.prompts.get(kind, [])]


def _direction_calls(r):
    return sum(1 for sc in _schemas(r) if "directions" in (sc.get("properties") or {}))


def _gate_calls(r):
    return sum(1 for sc in _schemas(r) if set((sc.get("properties") or {})) == {"declaration_gate"})


def _here(r):
    return (SP.placement_of(r.sess, PC) or {}).get("place_id")


def _domain(r, kind):
    p = os.path.join(L.session_dir(r.sess.session_id), "domain.jsonl")
    return [json.loads(x)["data"] for x in open(p, encoding="utf-8") if json.loads(x)["kind"] == kind] \
        if os.path.exists(p) else []


# ══════════════════════════════════════════════════════════════
#  G01·E01 · G02·E11 — DYNAMIC 시뮬레이션 0 / LEGACY 유지
# ══════════════════════════════════════════════════════════════

async def test_G01_E01_dynamic_ordinary_turn_runs_no_simulation(crig):
    r = crig
    r.prov.routes["judgment"] = judgment("시장")
    tx = await turn(r)
    assert tx.status == tt.TurnStatus.COMMITTED and _here(r) == "시장"
    assert r.sim_calls == [] and _direction_calls(r) == 0 and _gate_calls(r) == 0
    ops = {c.get("operation_id", "").split(":")[0] for c in _domain(r, "cost.event")}
    assert "TURN_SIMULATION" not in ops and "TURN_DECLARATION_CHECK" not in ops
    lp = r.prov.prompts["logic"][-1][0]
    assert "[서사 방향성 시뮬레이션" not in lp and "[세계 반응 권위 — 동적 세계]" in lp      # G18·G25


async def test_G02_E11_legacy_keeps_simulation_and_its_instruction_block(drig, monkeypatch):
    r = drig                                                              # make() 없음 = LEGACY 세션
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions",
                        gm_mod.GMCog._simulate_narrative_directions.__get__(r.gm))
    r.sim_calls = []
    real = r.gm._simulate_narrative_directions

    async def spy(*a, **k):
        r.sim_calls.append(a[1] if len(a) > 1 else None)
        return await real(*a, **k)
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions", spy)
    r.sess.cache_name, r.sess.cache_model = r.sess.cache_name or "cachedContents/test", core.DEFAULT_MODEL
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["judgment"] = [jdg()]
    await turn(r, "주변을 둘러본다")
    assert len(r.sim_calls) == 1 and _direction_calls(r) == 1 and _gate_calls(r) == 0
    lp = r.prov.prompts["logic"][-1][0]
    assert "[서사 방향성 시뮬레이션" in lp and "[세계 반응 권위" not in lp
    jp = r.prov.prompts["judgment"][-1][0]
    assert "[선언 검증 — 동적 세계" not in jp and "[이번 턴 대화" not in jp
    ops = {c.get("operation_id", "").split(":")[0] for c in _domain(r, "cost.event")}
    assert "TURN_SIMULATION" in ops


# ══════════════════════════════════════════════════════════════
#  G03·G06·G07·G20 · E06 — 경로 질문 번호 응답
# ══════════════════════════════════════════════════════════════

async def _ask_route(r):
    r.prov.routes["judgment"] = judgment("성당")
    tx = await turn(r, "성당으로 간다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    opts = tx.world_turn.plan.route_options
    assert len(opts) >= 2 and tx.world_turn.pending_route["signatures"] == [o["signature"] for o in opts]
    return tx, opts


async def test_G03_route_number_answer_uses_question_snapshot_without_any_model_call(crig):
    r = crig
    tx, opts = await _ask_route(r)
    n_j = r.prov.calls.count("judgment")
    tx2 = await turn(r, "2번")
    assert tx2.transaction_id == tx.transaction_id
    assert r.prov.calls.count("judgment") == n_j and r.sim_calls == [] and _gate_calls(r) == 0
    assert _here(r) == "성당"
    noted = json.dumps(tx2.judgment_result or {}, ensure_ascii=False)
    assert json.dumps({"route_signature": opts[1]["signature"]}, ensure_ascii=False)[1:-1] in noted


async def test_G06_new_action_during_route_question_goes_to_judgment(crig):
    r = crig
    await _ask_route(r)
    r.prov.routes["judgment"] = judgment(None, action_minutes=0)
    await turn(r, "아니, 주변을 먼저 본다")
    assert _here(r) == "항구"
    jp = r.prov.prompts["judgment"][-1][0]
    assert "[이번 턴 대화" in jp and "1) " in jp                         # 질문 문맥은 판단에 그대로


async def test_G07_E06_number_plus_new_claim_is_not_shortcut_and_is_verified(crig):
    r = crig
    await _ask_route(r)
    n_j = r.prov.calls.count("judgment")
    r.prov.routes["judgment"] = [jfind("비밀 무기", roll=True)]
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "BLOCK", "NOT_AMBIENT")]
    tx = await turn(r, "1번으로 가며 비밀 무기를 발견한다")
    assert r.prov.calls.count("judgment") == n_j + 1 and _gate_calls(r) == 1
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER and _here(r) == "항구"
    assert ambient_ids(r.sess) == [] and not tx.judgment_result.get("roll_records")


async def test_G20_stale_or_out_of_range_number_is_not_applied(crig, monkeypatch):
    r = crig
    tx, _opts = await _ask_route(r)
    r.prov.routes["judgment"] = judgment(None, action_minutes=0)
    await turn(r, "9번")                                                  # 범위 밖 → 일반 판단
    assert _here(r) == "항구" and tx.world_turn.pending_route is None
    tx, _opts = await _ask_route(r)
    real = core.turn_world.move_question_anchor
    monkeypatch.setattr(core.turn_world, "move_question_anchor",
                        lambda ctx, s: dict(real(ctx, s), world_version="other"))
    n_j = r.prov.calls.count("judgment")
    await turn(r, "1번")                                                  # 기준선이 바뀜 → 번호 재사용 금지
    assert r.prov.calls.count("judgment") == n_j + 1 and _here(r) == "항구"


# ══════════════════════════════════════════════════════════════
#  G04·G05 — 목적지 질문 '그냥 간다' · 판단 ASK 짧은 답
# ══════════════════════════════════════════════════════════════

async def test_G04_destination_question_answer_reaches_judgment_without_gate(crig):
    r = crig
    r.make(edges=("e_hm#fwd", "e_hh#fwd", "e_hs#fwd"))
    r.sess.cache_name, r.sess.cache_model = "cachedContents/test", core.DEFAULT_MODEL
    r.prov.routes["judgment"] = judgment(None, movement_kind="DESTINATION", destination="NONE", action_minutes=0)
    tx = await turn(r, "이동한다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    await turn(r, "그냥 간다")
    assert _here(r) in {"시장", "산복도로", "해안"}
    assert r.sim_calls == [] and _gate_calls(r) == 0
    assert "직전에 진행자가 어디로 갈지 물었다" in r.prov.prompts["judgment"][-1][0]


async def test_G05_judgment_ask_answer_sees_question_text(crig):
    r = crig
    r.prov.routes["judgment"] = [_json({"player_intent": "x", "situation_read": "x", "action": "ASK",
                                        "reasoning": "x", "bridge_message": "왼쪽 문입니까, 오른쪽 문입니까?",
                                        "rolls": [], "ambient_proposal": False,
                                        "action_intent": {"movement_kind": "NONE", "time_kind": "NONE"}})]
    tx = await turn(r, "문을 연다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    r.prov.routes["judgment"] = [jdg()]
    await turn(r, "왼쪽")
    jp = r.prov.prompts["judgment"][-1][0]
    assert "[이번 턴 대화" in jp and "왼쪽 문입니까, 오른쪽 문입니까?" in jp and r.sim_calls == []


# ══════════════════════════════════════════════════════════════
#  G08·G13·E04 · G12·E03 · G14 · E14 · G22 — 조건부 선언 검증
# ══════════════════════════════════════════════════════════════

async def test_G13_E04_ordinary_attempt_has_no_check_call(crig):
    r = crig
    r.prov.routes["judgment"] = [jdg(intent="경비를 설득하려 시도한다")]
    await turn(r, "경비를 설득한다")
    assert _gate_calls(r) == 0 and r.sim_calls == []


async def test_G12_E03_assertion_without_ambient_flag_triggers_check(crig):
    r = crig
    r.prov.routes["judgment"] = [jdg(assertion=True)]
    r.prov.routes["simulation"] = [sim("IMPROPER_ASSERTION", "BLOCK", "CANNOT_CONFIRM")]
    tx = await turn(r, "나는 이미 문을 열었다")
    assert _gate_calls(r) == 1 and tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    gp = r.prov.prompts["simulation"][-1][0]
    assert "[판단층위가 읽은 시도]" in gp and "방향성" not in gp


@pytest.mark.parametrize("raw", [_json({}), _json({"declaration_gate": {"classification": "X"}})])
async def test_G14_malformed_check_discovery_held_but_attempt_continues(crig, raw):
    r = crig
    r.prov.routes["simulation"] = [raw]
    r.prov.routes["judgment"] = [jfind("노끈", roll=True)]
    tx = await turn(r, "노끈을 찾아본다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER and ambient_ids(r.sess) == []
    assert gm_mod.GATE_UNVERIFIABLE_NOTICE in "\n".join(m.content or "" for m in r.gch.sent)
    r.prov.routes["simulation"] = [raw]
    r.prov.routes["judgment"] = [jdg(assertion=True, intent="경비의 칼을 빼앗으려 시도한다")]
    tx2 = await turn(r, "경비의 칼을 빼앗았다")
    assert tx2.status == tt.TurnStatus.COMMITTED                          # 결과 선취만 — 시도로 진행
    assert "권위 있는 시도(판단층위 확정): 경비의 칼을 빼앗으려 시도한다" in r.prov.prompts["logic"][-1][0]


async def test_E14_rejudge_does_not_repeat_check_and_G22_master_and_log(crig):
    r = crig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target="나뭇조각", kind="OBJECT")]
    r.prov.routes["judgment"] = [jfind("나뭇조각", roll=True), jfind("나뭇조각")]
    tx = await turn(r, "나뭇조각을 찾는다")
    assert tx.status == tt.TurnStatus.COMMITTED and r.prov.calls.count("judgment") == 2 and _gate_calls(r) == 1
    assert len(ambient_ids(r.sess)) == 1
    assert any("🛡️ **[선언 검증]** AMBIENT_PROPOSAL → ACCEPT_NO_ROLL" in (m.content or "") for m in r.master.sent)
    ops = [c.get("operation_id", "") for c in _domain(r, "cost.event")]
    assert sum(o.startswith("TURN_DECLARATION_CHECK:") for o in ops) == 1                     # 원장 op 분리
    ev = os.path.join(L.session_dir(r.sess.session_id), "events.jsonl")
    assert any((json.loads(x).get("data") or {}).get("stage") == "declaration_check" for x in open(ev, encoding="utf-8"))


# ══════════════════════════════════════════════════════════════
#  E05·G16 — 강제 진행·ROLL 재개 지시에 방향 시뮬레이션 없음
# ══════════════════════════════════════════════════════════════

async def test_E05_forced_proceed_after_ask_limit_has_no_simulation_block(crig):
    r = crig
    r.sess.gm_clarify_count = gm_mod.MAX_CLARIFY_PER_MESSAGE
    r.prov.routes["judgment"] = [_json({"player_intent": "x", "situation_read": "x", "action": "ASK",
                                        "reasoning": "x", "bridge_message": "무엇을?", "rolls": [],
                                        "ambient_proposal": False,
                                        "action_intent": {"movement_kind": "NONE", "time_kind": "NONE"}})]
    await turn(r, "음")
    lp = r.prov.prompts["logic"][-1][0]
    assert "[서사 방향성 시뮬레이션" not in lp and "[세계 반응 권위 — 동적 세계]" in lp and r.sim_calls == []


async def test_G16_roll_resume_instruction_has_no_simulation_block(crig, monkeypatch):
    r = crig
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll(discovery=False)])]
    tx = await turn(r, "문을 민다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_ROLL
    monkeypatch.setattr(core.judgment, "roll_2d50", lambda _rng: (1, 1))
    res = await r.gm._execute_rolls(r.sess, tx.judgment_result["judgment"]["rolls"], r.gch,
                                    transaction_id=tx.transaction_id)
    await r.gm._continue_with_roll_results(r.sess, "문을 민다", res, transaction_id=tx.transaction_id)
    lp = r.prov.prompts["logic"][-1][0]
    assert "[서사 방향성 시뮬레이션" not in lp and "[세계 반응 권위 — 동적 세계]" in lp and r.sim_calls == []


# ══════════════════════════════════════════════════════════════
#  G21 — 예상 비용
# ══════════════════════════════════════════════════════════════

def test_G21_estimate_dynamic_has_conditional_check_not_simulation(crig, monkeypatch):
    r = crig
    est = core.estimate_turn(r.sess, "PROCEED")
    assert "개연성 시뮬레이션" not in est["breakdown"]
    # v7.30(사용자 결정 3.b): 발생률 미상 → 상한 제외(표시만), 관측 발생률 ≥ 기준 → 상한 포함(하한 0)
    assert est["conditional"]["선언 검증(조건부)"]["included"] is False and "선언 검증(조건부)" not in est["breakdown"]
    monkeypatch.setattr(core.estimate, "cond_rate", lambda s, op: 0.3)
    est_r = core.estimate_turn(r.sess, "PROCEED")
    lo, hi = est_r["breakdown"]["선언 검증(조건부)"]
    assert lo == 0.0 and hi > 0
    monkeypatch.setattr(core.estimate, "cond_rate", lambda s, op: 0.1)
    assert "선언 검증(조건부)" not in core.estimate_turn(r.sess, "PROCEED")["breakdown"]
    monkeypatch.setattr(core.turn_world, "session_is_dynamic", lambda s: False)   # LEGACY 판정 — 기존 예상 그대로
    est2 = core.estimate_turn(r.sess, "PROCEED")
    assert "개연성 시뮬레이션" in est2["breakdown"] and "선언 검증(조건부)" not in est2["breakdown"]
