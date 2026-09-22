"""WP-B — 추출 결과 스테이징(result-only) + 검증 계획 + stale/idempotency 보증.

T-B17 동치 중복 제거 / T-B19 stale 거부(변이 없음) / T-B21 이중 적용 금지.
추출은 provider 호출·파싱까지 result-only이며, canonical 반영은 단일 호환 적용
경계에서만 일어난다. 이 경계는 stale guard와 idempotency로 보호된다.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import core
from core import turn_preparation as tp

pytestmark = pytest.mark.policy


def _make_gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


async def _drive_extraction(monkeypatch, cog, session, master_ch, canned, *,
                            transaction_id, logical_turn, attempt):
    """provider 호출·파싱을 결정적으로 대체해 적용 경계까지 구동한다."""
    async def _fake_cwr(fn, **kw):
        return True, SimpleNamespace(text="{}", usage_metadata=None)

    def _fake_parse(*_a, **_k):
        return {k: (v.copy() if isinstance(v, (dict, list)) else v)
                for k, v in canned.items()}

    monkeypatch.setattr(core, "call_with_retry", _fake_cwr, raising=True)
    monkeypatch.setattr(core, "parse_extraction", _fake_parse, raising=True)
    return await cog._run_extraction(
        session, "묘사 전문", master_ch, transaction_id=transaction_id,
        logical_turn=logical_turn, attempt=attempt)


# ── T-B17 동치 중복 제거 / 모순 진단 ──────────────────────────────
def test_b17_equivalent_effects_deduped(session_auto_ready):
    result = {
        "location": {"name": "숲"},
        "npcs_met": ["임성진", "임성진"],                  # 동치 중복
        "companions": {"joined": ["백가", "백가"], "left": []},
        "status_scores": [],
        "item_changes": [],
    }
    plan = tp.build_extraction_plan(
        session_auto_ready, result, transaction_id="tx", logical_turn=1, attempt=1)
    npc = [e for e in plan.entries if e["domain"] == "npc_met"]
    comp = [e for e in plan.entries if e["domain"] == "companion" and e["op"] == "join"]
    assert len(npc) == 1, "동치 npc_met 중복이 제거되지 않았습니다"
    assert len(comp) == 1, "동치 companion join 중복이 제거되지 않았습니다"


def test_b17b_join_and_leave_conflict_flagged(session_auto_ready):
    result = {"companions": {"joined": ["백가"], "left": ["백가"]}}
    plan = tp.build_extraction_plan(
        session_auto_ready, result, transaction_id="tx", logical_turn=1, attempt=1)
    assert any(c["domain"] == "companion" and c["target"] == "백가"
               for c in plan.conflicts), "동행 합류·이탈 모순이 진단되지 않았습니다"


# ── stale guard 결정 로직 ─────────────────────────────────────────
def test_b19_extraction_is_stale_decision(session_auto_ready):
    sess = session_auto_ready   # turn_count == 7
    # active 없음(커밋 후 새 턴 없음) → stale 아님.
    assert core.turn_transaction.get_active_transaction(sess) is None
    assert tp.extraction_is_stale(sess, logical_turn=7, attempt=1) is False

    # 더 새로운 논리 턴이 활성(logical_turn=8) → 이전 턴(7,1) 추출은 stale.
    newer = core.turn_transaction.begin_turn_transaction(sess, "다음 턴")
    assert tp.extraction_is_stale(sess, logical_turn=7, attempt=1) is True
    # 동일 논리 시도는 stale 아님.
    assert tp.extraction_is_stale(
        sess, logical_turn=newer.logical_turn, attempt=newer.attempt) is False


# ── T-B19 stale 추출은 canonical을 변경하지 않는다 ────────────────
async def test_b19b_stale_extraction_applies_no_mutation(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.world_timeline = {"current_location": "마을", "time_of_day": "낮"}
    sess.resources = {}
    # 더 새로운 논리 턴을 연다(추출보다 최신).
    core.turn_transaction.begin_turn_transaction(sess, "다음 턴")  # logical_turn=8 active

    canned = {"location": {"name": "숲"},
              "item_changes": [{"target": "테스터", "item": "물", "delta": 5}],
              "status_scores": [], "npcs_met": [], "companions": {},
              "datetime": {}, "situation": {}}
    out = await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="old-tx", logical_turn=7, attempt=1)

    assert out is None, "stale 추출이 거부되지 않았습니다"
    assert sess.world_timeline.get("current_location") == "마을", (
        "stale 추출이 canonical world_timeline을 변경했습니다")
    assert sess.resources.get("테스터", {}).get("물") is None, (
        "stale 추출이 canonical 자원을 변경했습니다")


# ── T-B21 같은 트랜잭션 결과의 이중 적용 금지 ─────────────────────
async def test_b21_compatibility_apply_at_most_once(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.resources = {}
    # active 없음 → 비-stale 적용 대상.
    canned = {"location": {"name": "마을"},
              "item_changes": [{"target": "테스터", "item": "물", "delta": 5}],
              "status_scores": [], "npcs_met": [], "companions": {},
              "datetime": {}, "situation": {}}

    await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="tx-1", logical_turn=8, attempt=1)
    first = sess.resources.get("테스터", {}).get("물")

    # 같은 트랜잭션 결과를 리플레이한다(이중 적용 시도).
    await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="tx-1", logical_turn=8, attempt=1)
    second = sess.resources.get("테스터", {}).get("물")

    assert first == 5, f"첫 적용에서 자원이 반영되지 않았습니다(={first})"
    assert second == 5, f"같은 트랜잭션 결과가 이중 적용되었습니다(={second})"
