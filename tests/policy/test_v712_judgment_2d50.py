"""v7.12.0 — 판정 2d50 통합(사용자 확정 설계 2026-10-13).

R = 1d50 + 1d50, T = 52 + A + S + M + H + D, 성공 = R < T. A = 2×(원스탯−10)은 코드, M·H·D는 판단층위 계약값.
S는 이번 판정에서 0(사용자 결정 — 스킬 보너스·고유 규칙 추후 통합). 대성공·대실패 없음.
"""
from __future__ import annotations

import random

import pytest

import core
from core import judgment as J
from core import turn_history as TH
from core import turn_transaction as tt
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_ready_barrier import _force_fail_growth, _usage, rig  # noqa: F401 — 픽스처 재사용
from tests.policy.test_turn_history import JUDGMENT, _fake_logic, _loc
from tests.policy.test_wp_e_patch import inject  # noqa: F401

pytestmark = pytest.mark.policy


def _spec(**kw):
    base = {"kind": "GENERAL", "char_name": "테스터", "stat": "근력", "difficulty": "보통",
            "method_mod": 0, "method_basis": "", "status_mod": 0, "status_basis": ""}
    base.update(kw)
    return base


def _dice(monkeypatch, *values):
    seq = iter(values)
    monkeypatch.setattr(random, "randint", lambda a, b: next(seq))


# ── 순수 수식 ─────────────────────────────────────────────

def test_distribution_matches_2500_combination_table():
    table = {2: 0.0, 3: 0.0004, 32: 0.186, 42: 0.328, 52: 0.51, 62: 0.688, 72: 0.826, 82: 0.924, 101: 1.0}
    for t, p in table.items():
        assert J.success_chance(t) == pytest.approx(p, abs=1e-9), t


def test_tie_fails_and_formula():
    assert not J.is_success(52, 52) and J.is_success(51, 52)
    assert J.target(10, 0, -3, 2, J.DIFFICULTY["어려움"]) == 52 + 10 - 3 + 2 - 15
    assert J.DIFFICULTY == {"쉬움": 20, "약간 쉬움": 10, "보통": 0, "어려움": -15,
                            "매우 어려움": -30, "극도로 어려움": -40}


def test_ability_mod_from_raw_stat_only_on_1_to_20_scale():
    assert [J.ability_mod(v) for v in (1, 10, 15, 20, "12")] == [-18, 0, 10, 20, 4]
    assert [J.ability_mod(v) for v in (0, 21, "x", None, True, 7.5)] == [None] * 6


def test_roll_is_two_independent_d50(monkeypatch):
    calls = []
    monkeypatch.setattr(random, "randint", lambda a, b: calls.append((a, b)) or 7)
    assert J.roll_2d50(random) == (7, 7) and calls == [(1, 50), (1, 50)]


def test_contest_outcomes():
    assert J.contest_outcome(5, 3) == "A" and J.contest_outcome(2, 9) == "B"
    assert J.contest_outcome(4, 4) == "TIE"                    # 같은 여유 = 교착
    assert J.contest_outcome(3, -2) == "A" and J.contest_outcome(0, 1) == "B"
    assert J.contest_outcome(0, -5) == "NONE"                  # 쌍방 실패 — 승자 없음


def test_validate_rolls_rejects_out_of_range_and_missing_without_correcting():
    assert J.validate_rolls([_spec()]) == []
    assert J.validate_rolls([_spec(method_mod=13)])
    assert J.validate_rolls([_spec(status_mod=-16)])
    assert J.validate_rolls([_spec(difficulty="불가능")])
    assert J.validate_rolls([_spec(method_mod=True)])
    assert J.validate_rolls([_spec(kind="CONTEST")])           # opponent 누락
    assert J.validate_rolls([_spec(kind="CONTEST", opponent=_spec(name="강도", method_mod=99))])
    assert J.validate_rolls([_spec(kind="CONTEST", opponent={"name": "강도", "stat": "근력", "difficulty": "보통",
                                                               "method_mod": 0, "status_mod": 0})]) == []


# ── 판단층위 계약 검증 → 기존 재시도 경로 ─────────────────────

async def test_invalid_roll_contract_retries_then_skips_without_rolling(rig):
    r = rig
    bad = {"player_intent": "x", "situation_read": "x", "action": "ROLL", "reasoning": "x",
           "bridge_message": "", "self_check": "x", "rolls": [_spec(method_mod=40)]}
    r.prov.routes["narration"] = [FakeGenAIResponse(core_json(bad), usage=_usage())]
    tx = tt.get_or_begin_turn_transaction(r.sess, "문을 민다")
    out = await r.gm._call_judgment(r.sess, "문을 민다", [], r.master, transaction_id=tx.transaction_id)
    assert out is None and r.prov.calls.count("narration") == 2      # JUDGMENT_MAX_RETRIES


async def test_valid_roll_contract_passes(rig):
    r = rig
    ok = {"player_intent": "x", "situation_read": "x", "action": "ROLL", "reasoning": "x",
          "bridge_message": "", "self_check": "x", "rolls": [_spec(method_mod=-4)]}
    r.prov.routes["narration"] = [FakeGenAIResponse(core_json(ok), usage=_usage())]
    tx = tt.get_or_begin_turn_transaction(r.sess, "문을 민다")
    out = await r.gm._call_judgment(r.sess, "문을 민다", [], r.master, transaction_id=tx.transaction_id)
    assert out and out["rolls"][0]["method_mod"] == -4 and r.prov.calls.count("narration") == 1


def core_json(payload):
    import json
    return json.dumps(payload, ensure_ascii=False)


# ── 실행: 일반·대결·제작 ───────────────────────────────────

async def test_general_roll_uses_server_assembled_target(rig, monkeypatch):
    r = rig
    _dice(monkeypatch, 20, 21)                                       # R = 41
    tx = tt.get_or_begin_turn_transaction(r.sess, "문을 민다")
    res = await r.gm._execute_rolls(
        r.sess, [_spec(stat="기술", difficulty="어려움", method_mod=6, method_basis="지렛대")],
        r.gch, transaction_id=tx.transaction_id)
    # 기술 8 → A −4, M +6, D −15 → T 39, R 41 → 실패
    assert "목표 T=52-4(능력)+6(행동)-15(난이도 어려움)=39" in res[0] and "실패(여유 -2)" in res[0]
    assert "행동 근거: 지렛대" in res[0]
    rec = tx.judgment_result["roll_records"][0]["actor"]
    assert (rec["T"], rec["R"], rec["success"], rec["S"]) == (39, 41, False, 0)


def _npc(r, name, stats):
    r.sess.npcs = dict(r.sess.npcs or {})
    r.sess.npcs[name] = {"stats": stats}


@pytest.mark.parametrize("dice,verdict", [
    ((10, 10, 20, 20), "테스터 우세"),        # 둘 다 성공, 여유 32 > 22
    ((30, 30, 10, 10), "강도 우세"),          # 한쪽만 성공
    ((40, 40, 45, 45), "쌍방 실패"),          # 둘 다 실패 — 승자 없음
    ((10, 10, 15, 15), "교착"),               # 같은 여유
])
async def test_contest_both_sides_roll_independently(rig, monkeypatch, dice, verdict):
    r = rig
    _npc(r, "강도", {"근력": 15})                          # 테스터 근력 10 → T 52 / 강도 → T 62
    _dice(monkeypatch, *dice)
    tx = tt.get_or_begin_turn_transaction(r.sess, "강도와 힘겨룬다")
    spec = _spec(kind="CONTEST", opponent=_spec(name="강도", stat="근력"))
    res = await r.gm._execute_rolls(r.sess, [spec], r.gch, transaction_id=tx.transaction_id)
    assert any(verdict in x for x in res)
    game = "\n".join(m.content or "" for m in r.gch.sent)
    assert "[강도] 판정 — 2d50" in game and "62" not in game.split("[강도]")[1].split("\n")[0]  # NPC 목표치 비공개
    assert any("62" in (m.content or "") for m in r.master.sent)                      # 운영자는 본다


async def test_contest_npc_without_stats_is_not_fabricated(rig, monkeypatch):
    r = rig
    _dice(monkeypatch, 10, 10)
    tx = tt.get_or_begin_turn_transaction(r.sess, "유령과 겨룬다")
    spec = _spec(kind="CONTEST", opponent=_spec(name="유령", stat="근력"))
    res = await r.gm._execute_rolls(r.sess, [spec], r.gch, transaction_id=tx.transaction_id)
    assert any("유령의 판정 능력치를 확인할 수 없음" in x for x in res)
    assert not any("대결 결과:" in x for x in res)


async def test_unknown_actor_stat_does_not_roll(rig, monkeypatch):
    r = rig
    monkeypatch.setattr(random, "randint", lambda a, b: (_ for _ in ()).throw(AssertionError("굴리면 안 됨")))
    tx = tt.get_or_begin_turn_transaction(r.sess, "x")
    res = await r.gm._execute_rolls(r.sess, [_spec(stat="없는능력")], r.gch, transaction_id=tx.transaction_id)
    assert "판정 불가(능력치 미확인)" in res[0]


async def test_craft_passes_margin_to_director(rig, monkeypatch):
    r = rig
    _dice(monkeypatch, 5, 5)
    tx = tt.get_or_begin_turn_transaction(r.sess, "창을 만든다")
    res = await r.gm._execute_rolls(r.sess, [_spec(kind="CRAFT", stat="기술")], r.gch,
                                    transaction_id=tx.transaction_id)
    assert any("제작 판정: 성공 여유 +38" in x for x in res)            # T 48 − R 10


async def test_judgment_prompt_carries_npc_stats_as_hidden_input(rig):
    import cogs.gm as gm_mod
    _npc(rig, "강도", {"근력": 15})
    p = gm_mod._build_judgment_user_prompt(rig.sess, "강도를 밀친다", [])
    assert "강도(능력치 근력 15)" in p and "비공개" in p


# ── 행운 · 성장 ─────────────────────────────────────────────

def _sess(**sd):
    from types import SimpleNamespace
    return SimpleNamespace(scenario_data=sd, players={"1": {"name": "가", "profile": {"근력": 12}}},
                           stat_fail_counts={})


def test_luck_works_without_authored_luck_stat(monkeypatch):
    s = _sess(luck={"chance_on_fail": 1.0})
    monkeypatch.setattr(core.growth.random, "random", lambda: 0.0)
    monkeypatch.setattr(core.growth.random, "randint", lambda a, b: 10)
    res = core.growth.check_luck(s, "가", failed=True)
    assert res and res["target"] == 10 and "행운" not in s.players["1"]["profile"]   # 프로필에 쓰지 않음
    s.players["1"]["profile"]["행운"] = 4                                            # 명시값 우선
    assert core.growth.check_luck(s, "가", failed=True) is None


def test_luck_does_not_change_success_and_growth_has_priority(monkeypatch):
    s = _sess(growth={"fail_threshold": 1}, luck={"chance_on_fail": 1.0})
    monkeypatch.setattr(core.growth.random, "random", lambda: 0.0)
    monkeypatch.setattr(core.growth.random, "randint", lambda a, b: 1)
    out = core.process_roll_outcome(s, "가", "근력", failed=True)
    assert out["growth"] is not None and out["luck"] is None


def test_growth_cap_follows_scenario_scale_and_d20_die(monkeypatch):
    seen = []
    monkeypatch.setattr(core.growth.random, "randint", lambda a, b: seen.append(b) or 20)
    s = _sess(growth={"fail_threshold": 1}, ability_stat_max=20)
    g = core.growth.check_growth(s, "가", "근력")
    assert g["grew"] and s.players["1"]["profile"]["근력"] == 13 and seen == [20]   # 10 이상도 성장
    s2 = _sess(growth={"fail_threshold": 1, "stat_max": 12}, ability_stat_max=20)
    assert core.growth.check_growth(s2, "가", "근력")["reason"] == "상한 도달"       # 명시 상한 우선


# ── 재서술: 같은 판정의 성장·실패 누적 보존 ─────────────────────

async def _roll_turn_with_growth(r, monkeypatch):
    s = r.sess
    _force_fail_growth(monkeypatch, s)
    r.prov.routes["extraction"] = [_loc("숲길")]
    tx = tt.get_or_begin_turn_transaction(s, "문을 부순다")
    TH.note_judgment(s, tx.transaction_id, judgment=dict(JUDGMENT), player_message="문을 부순다",
                     roll_results=[])
    res = await r.gm._execute_rolls(s, [_spec(stat="기술")], r.gch, transaction_id=tx.transaction_id)
    TH.note_judgment(s, tx.transaction_id, roll_results=res)
    await r.gm._finish_proceed_and_continue(s, "지시", r.master, event_assessment="ongoing",
                                            transaction_id=tx.transaction_id)
    assert tx.status == tt.TurnStatus.COMMITTED
    return dict(s.stat_fail_counts)


async def test_rerender_keeps_growth_and_fail_counts_without_rerolling(rig, monkeypatch, inject):
    r = rig
    counts = await _roll_turn_with_growth(r, monkeypatch)
    assert r.sess.players["555001"]["profile"]["기술"] == 9
    monkeypatch.setattr(random, "randint", lambda a, b: (_ for _ in ()).throw(AssertionError("재추첨 금지")))
    _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("숲길")]
    started, reason = await r.gm.rerender_latest(r.sess)
    assert started, reason
    assert r.sess.players["555001"]["profile"]["기술"] == 9                  # 성장 보존(이중 적용 없음)
    assert r.sess.stat_fail_counts == counts


async def test_rerender_without_restage_loses_growth_counterexample(rig, monkeypatch, inject):
    """반례 — 효과 재스테이징이 없으면 재서술 커밋이 원 턴 성장을 잃는다(v7.11 이전 동작)."""
    r = rig
    await _roll_turn_with_growth(r, monkeypatch)
    inject.setattr(core.turn_preparation, "restage_roll_effects", lambda *a, **k: False)
    _fake_logic(r, inject)
    r.prov.routes["extraction"] = [_loc("숲길")]
    started, _ = await r.gm.rerender_latest(r.sess)
    assert started and r.sess.players["555001"]["profile"]["기술"] == 8
