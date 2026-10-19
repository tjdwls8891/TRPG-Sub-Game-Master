# -*- coding: utf-8 -*-
"""v7.21.1 — 미전달 서사 신호(world_pending_signals) 수명주기(사용자 확정 정책 A~D).

  · A(T1): 묘사 전 계획 신호는 표현 판정과 무관하게 다음 턴 대기를 만들지 않는다(이번 턴 지시·묘사 입력은 유지).
  · B(T2·T3): 묘사 후 보충 진행이 처음 만든 신호만 다음 커밋된 PROCEED 본문 1회 — 표현 여부와 무관하게 재이월 없음.
  · C(T4): 대기 신호의 exposes 보존 → 다음 턴 관찰 매니페스트 후보. 인지는 추출이 노출을 보고할 때만.
  · T5: NARRATE·ASK·ROLL 대기·커밋 실패에서는 소모하지 않는다. T6: 저장·되감기·재서술·옛 행 호환.
"""
from __future__ import annotations

import json

import pytest

import core
from core import action_intent as AI
from core import cognition as CG
from core import turn_history as TH
from core import turn_transaction as tt
from core import turn_world as TW
from core import world_refs as RF
from tests.policy.test_h04b_events_progression import eq
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, _arm_alert, _expose, drig, extraction, judgment, obs_key, rig, turn)
from tests.policy.test_h07_runtime_support import _sig, arrive_tpl
from tests.policy.test_ready_barrier import _json
from tests.policy.test_v714_discovery_assertion import droll, jdg
from tests.policy.test_h07_yeongdo_draft import _defn, ctx as yd_ctx, genesis, setup, turn as yd_turn

pytestmark = pytest.mark.policy

REACT = "좌판 주인이 목소리를 낮춰 성당 쪽을 턱으로 가리킨다"


# ── A · T1 계획 신호 ──────────────────────────────────────────

async def test_T1_unrepresented_plan_signal_creates_no_pending(drig):
    r = drig
    _arm_alert(r)
    r.prov.routes["extraction"] = _expose(represent=False)          # 추출이 표현을 인정하지 않음
    await turn(r, "한동안 강지수와 이야기한다")
    assert "경비가 칼을 뽑아 든다" in r.prov.prompts["narration"][-1][0]   # 이번 턴 입력은 그대로
    assert "경비가 칼을 뽑아 든다" in r.prov.prompts["logic"][-1][0]
    assert r.sess.world_pending_signals == []
    assert not hasattr(TW, "MAX_PENDING_CARRY")                         # 반복 이월·은퇴 정책 제거


# ── B·C · T2~T4 영도 남항 교환 입장(실제 정의) ───────────────────

def _yd_pay():
    w, _ = genesis("arrive_plain")
    w = setup(w, "수습대 집결지")
    p = yd_turn(w, AI.ResolvedActionIntent(action_minutes=1, definition_revision=_defn()[1]))
    man = TW.build_observation_manifest(yd_ctx().definition, yd_ctx(), p, w)
    res = TW.process_observation(yd_ctx(), w, p, man, {}, transaction_id="txpay", narrative_seed="s",
                                 item_facts={"used": [], "paid": ["항생제"]})
    return res


def _next(world, pending, raw):
    p = yd_turn(world, AI.ResolvedActionIntent(action_minutes=1, definition_revision=_defn()[1]))
    man = TW.build_observation_manifest(yd_ctx().definition, yd_ctx(), p, world, pending=pending)
    raw = raw(man) if callable(raw) else raw
    return man, TW.process_observation(yd_ctx(), world, p, man, raw, transaction_id="txnext",
                                       narrative_seed="s", pending=pending)


def test_T2_T4_namhang_barter_signal_once_with_exposes_preserved():
    res = _yd_pay()
    assert res.status == TW.TW_READY
    (row,) = res.pending                                                 # 지급 뒤 보충 진행 신호 1건
    assert row["signal_id"] == "ledger" and "명부" in row["cue"]
    assert row["exposes"] == ["yd.info.q.join_namhang_trade"] and row["scene_place"] == "수습대 집결지"
    assert "carried" not in row
    w = res.projected_world
    # 다음 본문: 블록에 발생 장소·시각과 함께 1회 · 매니페스트에 노출 후보(공개 안전 요약)
    man, nxt = _next(w, res.pending, {})                                 # 추출이 노출을 보고하지 않음
    assert any(iid == "yd.info.q.join_namhang_trade" for _k, iid, _l in man.infos)
    assert any(cue == row["cue"] for _k, _sk, cue in man.signals)
    assert nxt.status == TW.TW_READY and nxt.pending == []               # T3: 한 번 준 뒤 재이월 없음
    assert "yd.info.q.join_namhang_trade" not in nxt.exposures           # T4: 표현 증거 없으면 인지 없음
    assert not any(o.op_type == CG.OP_COGNITION_ACQUIRE and o.payload.get("info_id") == "yd.info.q.join_namhang_trade"
                   for o in nxt.ops)


def test_T3_T4_represented_and_exposed_pending_signal_grants_info_and_ends():
    res = _yd_pay()

    def raw(man):
        x = next(k for k, iid, _l in man.infos if iid == "yd.info.q.join_namhang_trade")
        s = next(k for k, _sk, cue in man.signals if "명부" in cue)
        return {"info_exposed": [x], "represented_signals": [s]}
    _man, nxt = _next(res.projected_world, res.pending, raw)
    assert nxt.status == TW.TW_READY and nxt.pending == []
    assert "yd.info.q.join_namhang_trade" in nxt.exposures               # 추출 노출 확인 → 인지 반영
    assert any(o.op_type == CG.OP_COGNITION_ACQUIRE and o.payload.get("info_id") == "yd.info.q.join_namhang_trade"
               for o in nxt.ops)


def test_T6_legacy_rows_without_exposes_are_delivered_once_safely():
    res = _yd_pay()
    legacy = [{"signal_id": "guard", "instance_id": "evt_old", "cue": "옛 대기 신호", "at_minute": None,
               "scene_place": None, "source_tx": "old", "carried": 2},
              {"signal_id": "bad", "instance_id": "evt_bad", "cue": "형식 이상", "exposes": "not-a-list"}]
    man, nxt = _next(res.projected_world, legacy, {})
    assert {cue for _k, _sk, cue in man.signals} >= {"옛 대기 신호", "형식 이상"}
    assert nxt.status == TW.TW_READY and nxt.pending == []               # 옛 행도 1회 전달 후 소멸


# ── 통합(rig): 1회 소비 · 장소 표기 · 비진행/실패 경로 · 되감기·재서술 ──────────

def _react_tpl():
    """S40을 PC가 알게 되면(묘사 후 보충 진행) 같은 분 장면 신호 — exposes S41."""
    return {"version": 1, "spawn_when": eq({"ref": "COGNITION_AWARENESS", "holder": RF.SESSION_PC, "info": "S40"},
                                           "CONTENT"),
            "phases": {"on": {"eligible_when": {"const": True}, "priority": 1}},
            "transition_cases": [{"case_id": "c", "from": None, "to": "on",
                                  "steps": [_sig("react", REACT, ["S41"])]}]}


def _s41(raw):
    raw["information_definitions"]["S41"] = {"proposition": "성당 뒷문은 밤에 잠기지 않는다",
                                             "summary": "성당 뒷문 사정", "subject_refs": []}


async def _first(r):
    """시장 도착(계획 신호 S40 노출 보고) → 보충 진행 신호(react) 대기 1건."""
    r.make(templates={"arrive": arrive_tpl(), "knows": _react_tpl()}, raw_patch=_s41)
    r.prov.routes["judgment"] = judgment("시장")
    r.prov.routes["extraction"] = lambda kw: extraction(
        {"info_exposed": [obs_key(r.prov, "X", "원본 기록 위치") or "NONE"]})
    await turn(r)
    (row,) = r.sess.world_pending_signals
    assert row["signal_id"] == "react" and row["exposes"] == ["S41"] and row["scene_place"] == "시장"
    assert REACT not in r.prov.prompts["narration"][-1][0]               # 묘사 후 신호 — 그 턴 본문에 없음
    r.prov.routes["extraction"] = [extraction({})]
    r.prov.routes["judgment"] = judgment(None, action_minutes=0)
    return row


async def test_T2_T3_supplemental_signal_delivered_once_with_origin_place(drig):
    r = drig
    await _first(r)
    await turn(r, "주위를 살핀다")
    np_, lp = r.prov.prompts["narration"][-1][0], r.prov.prompts["logic"][-1][0]
    assert REACT in np_ and REACT in lp
    line = next(x for x in np_.splitlines() if REACT in x)
    assert "장소: 시장" in line and "작중" in line                       # 발생 시각·장소 보존 표기
    assert "성당 뒷문 사정" in r.prov.prompts["extraction"][-1][0]        # 노출 후보(공개 요약)
    assert CG.record_of(r.sess, CG.actor_holder(PC), "S41") is None      # 노출 보고 없음 → 인지 없음
    assert r.sess.world_pending_signals == []                            # 1회 소비
    await turn(r, "주위를 살핀다")
    assert REACT not in r.prov.prompts["narration"][-1][0]               # 재등장 없음


@pytest.mark.parametrize("action", ["NARRATE", "ASK"])
async def test_T5_non_body_paths_do_not_consume(drig, action):
    r = drig
    row = await _first(r)
    r.prov.routes["judgment"] = judgment(None, action=action, action_minutes=0)
    r.prov.routes["logic"] = [_json({"narrate_instruction": "짧게 답한다", "proceed_instruction": "",
                                     "event_assessment": "ongoing"})]
    await turn(r, "지금 몇 시쯤이지?")
    assert [dict(x) for x in r.sess.world_pending_signals] == [row]


async def test_T5_roll_wait_does_not_consume(drig):
    r = drig
    row = await _first(r)
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll(discovery=False)])]
    tx = await turn(r, "벽을 타고 오른다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_ROLL, tx.status
    assert [dict(x) for x in r.sess.world_pending_signals] == [row]


async def test_T5_commit_failure_does_not_consume(drig, monkeypatch):
    r = drig
    row = await _first(r)

    def boom(*a, **k):
        raise OSError("디스크 실패(테스트)")
    monkeypatch.setattr(core.commit_coordinator, "write_session_strict_locked", boom, raising=False)
    monkeypatch.setattr(core.io, "write_session_strict_locked", boom, raising=False)
    await turn(r, "주위를 살핀다")
    assert REACT in r.prov.prompts["narration"][-1][0]                   # 본문은 나갔으나 커밋 실패
    assert [dict(x) for x in r.sess.world_pending_signals] == [row]


async def test_T6_rewind_and_rerender_do_not_duplicate_or_resurrect(drig):
    r = drig
    row = await _first(r)
    await turn(r, "주위를 살핀다")                                       # 소비(gm_turn 2)
    assert r.sess.world_pending_signals == []
    n = sum(REACT in p[0] for p in r.prov.prompts["narration"])
    started, reason = await r.gm.rerender_latest(r.sess)                 # 재서술: 턴 이전 대기로 돌아가 다시 1회
    assert started, reason
    assert sum(REACT in p[0] for p in r.prov.prompts["narration"]) == n + 1
    assert r.sess.world_pending_signals == []                            # 재서술 커밋 뒤에도 부활 없음
    res = await TH.rewind(r.bot, r.sess, 1)                              # gm_turn 1 직후 = 대기 1건
    assert res.get("ok"), res
    assert [dict(x) for x in r.sess.world_pending_signals] == [row]
    # 저장·복구: JSON 왕복에도 행 그대로(exposes 포함)
    assert json.loads(json.dumps(r.sess.world_pending_signals, ensure_ascii=False)) == [row]
