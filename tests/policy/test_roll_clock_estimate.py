"""세션 e9d6a7ae 후속 결함 — A 묘사 정본 시각 · B 상태이상 판정 보정 · C 예상 비용 · D 판정 기록 중복."""
from __future__ import annotations

import json
import os
import random
from types import SimpleNamespace

import pytest

import core
from core import definition_compiler as dc
from core import player_world as pw
from core import turn_transaction as tt
from core import turn_world as TW
from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용

pytestmark = pytest.mark.policy


# ── A: 묘사층위 정본 날짜·요일·시각 ─────────────────────────────

def _yd_definition():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "scenarios", "영도.json"), encoding="utf-8") as f:
        return dc.compile_scenario(json.load(f), "영도").definition


def test_narration_clock_block_gives_canonical_date_weekday_every_turn():
    D = _yd_definition()
    cal = TW._calendar(D)
    now = cal.to_minute_index(2016, 10, 2, 7, 30)
    assert TW.clock_view(D, now)["weekday"] == "일요일"         # 2016-10-02 = 일요일
    wt = SimpleNamespace(plan=None, ctx=SimpleNamespace(definition=D))
    block = TW.narration_clock_block(wt, SimpleNamespace(world_clock={"minute_index": now}))
    assert "2016-10-02 일요일 07:30" in block
    assert "상태 블록" in block                                   # 상태 블록 날짜 표기도 이 값을 따른다
    # 이동 없는 턴(NONE)도 시각을 준다 — 세계 블록에 포함
    wt2 = SimpleNamespace(plan=pw.PlayerWorldPlan(status=pw.PLAN_NONE), ctx=SimpleNamespace(definition=D))
    assert "2016-10-02 일요일" in TW.narration_clock_block(wt2, SimpleNamespace(world_clock={"minute_index": now}))
    # PLANNED는 계획 종료 시각
    plan2 = pw.PlayerWorldPlan(status=pw.PLAN_PLANNED, end_minute=now + 24 * 60)
    wt3 = SimpleNamespace(plan=plan2, ctx=SimpleNamespace(definition=D))
    assert "2016-10-03 월요일 07:30" in TW.narration_clock_block(wt3, SimpleNamespace(world_clock={}))


# ── B: 상태이상 가중치 = 코드 합산 ─────────────────────────────

def _status_setup(sess):
    sess.scenario_data = dict(sess.scenario_data or {})
    sess.scenario_data["status_effects"] = [{"name": "감염 의심", "weight": -1},
                                           {"name": "멀쩡", "weight": 0}]
    sess.statuses = {"테스터": ["감염 의심", "멀쩡"]}


async def test_roll_target_includes_status_weight_and_shows_breakdown(rig, monkeypatch):
    r = rig
    _status_setup(r.sess)
    base = r.sess.players[next(iter(r.sess.players))]["profile"]["근력"]
    monkeypatch.setattr(random, "randint", lambda a, b: base + 2)     # 상태 보정 없으면 성공하는 눈
    tx = tt.get_or_begin_turn_transaction(r.sess, "문을 민다")
    res = await r.gm._execute_rolls(r.sess, [{"char_name": "테스터", "stat": "근력", "sides": 20, "weight": 2}],
                                    r.gch, transaction_id=tx.transaction_id)
    assert f"기준치 {base}-1(감염 의심)+2={base + 1}" in res[0]
    assert "실패" in res[0]                                         # base+2 > base+1


def test_judgment_prompt_says_status_weight_is_auto_applied(rig):
    import cogs.gm as gm_mod
    _status_setup(rig.sess)
    p = gm_mod._build_judgment_user_prompt(rig.sess, "문을 민다", [])
    assert "감염 의심 -1" in p and "자동 반영" in p and "멀쩡 +0" not in p


def test_status_roll_modifier_is_scenario_data_only():
    assert core.status_roll_modifier({"status_effects": [{"name": "X", "weight": -3}]}, ["X", "Y"]) == (-3, [("X", -3)])
    assert core.status_roll_modifier({}, ["X"]) == (0, [])


# ── C: 예상 비용 — 시뮬레이션·비캐시 입력 (판정 뒤에는 지시층위만 재호출 — 재진입 항목 없음) ──

def test_estimate_includes_simulation_and_reentry_when_cache_usable(rig, monkeypatch):
    s = rig.sess
    s.cache_read_tokens = 73000
    monkeypatch.setattr(core.turn_history, "cache_usable", lambda _s: True)
    est = core.estimate_turn(s, "PROCEED")
    assert "개연성 시뮬레이션" in est["breakdown"] and "판정 재진입(상한)" not in est["breakdown"]
    # 지시층위 하한 > 캐시 읽기만의 비용(비캐시 입력이 0으로 계산되지 않는다)
    i_lo = est["breakdown"]["지시층위"][0]
    lo_out = core.estimate._out_range(s, "instruction", "instruction_out")[0]
    cache_only = core.calculate_text_gen_cost_breakdown(core.DEFAULT_MODEL, input_tokens=73000,
                                                        output_tokens=lo_out, cached_read_tokens=73000)["total_krw"]
    assert est["input_tokens"]["instruction"] > 0 and i_lo > round(cache_only, 2)
    ask = core.estimate_turn(s, "ASK")
    assert "개연성 시뮬레이션" in ask["breakdown"]


def test_estimate_has_no_simulation_without_usable_cache(rig):
    s = rig.sess
    s.cache_name = None
    est = core.estimate_turn(s, "PROCEED")
    assert "개연성 시뮬레이션" not in est["breakdown"]


# ── D: 판정 줄 이중 기록 없음 ─────────────────────────────────

async def test_roll_line_not_logged_twice(rig, monkeypatch):
    r = rig
    logged = []
    orig = core.write_log
    monkeypatch.setattr(core, "write_log", lambda sid, kind, text, *a, **k: (logged.append((kind, text)),
                                                                            orig(sid, kind, text, *a, **k)))
    tx = tt.get_or_begin_turn_transaction(r.sess, "문을 민다")
    await r.gm._execute_rolls(r.sess, [{"char_name": "테스터", "stat": "근력", "sides": 20}],
                              r.gch, transaction_id=tx.transaction_id)
    assert not any(kind == "game_chat" and text.startswith("[판정]:") for kind, text in logged)
