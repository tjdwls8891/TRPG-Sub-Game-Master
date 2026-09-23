"""WP-B — ExtractionMutationPlan이 적용의 유일한 권위임을 증명하는 부정 테스트.

핵심 아키텍처 계약:
  raw result → parse → 코드 검증 → 정규화된 accepted 효과 → ExtractionMutationPlan
  → LegacyCompatibilityApplier(_apply_extraction_plan) → 계획 항목만 적용.

네 가지 증명:
  A. 검증 탈락 후보는 되살아날 수 없다(적용부는 raw result를 보지 않는다).
  B. 동치 중복 후보는 이중 적용될 수 없다(계획이 dedup → 정확히 한 번).
  C. 상호 모순은 last-write-wins가 되지 않는다(계획이 양쪽 제외).
  D. raw result를 폐기해도 계획만으로 동일한 의도 상태가 나온다(계획=권위).
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


# ── A. 검증 탈락 후보는 되살아날 수 없다 ──────────────────────────
async def test_pa_a_rejected_candidate_cannot_resurrect(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.resources = {}
    sess.statuses = {}

    canned = {
        "location": {"name": "마을"},                      # 이동 없음(변수 고정)
        "status_scores": [
            {"target": "없는사람", "status": "부상", "score": 99},   # 무효 캐릭터
            {"target": "테스터", "status": "가짜상태", "score": 99},  # 무효 상태
        ],
        "item_changes": [{"target": "없는사람", "item": "금괴", "delta": 5}],  # 무효 캐릭터
        "npcs_met": [], "companions": {}, "datetime": {}, "situation": {},
    }

    # 계획 단계에서 이미 탈락한다.
    plan = tp.build_extraction_plan(
        sess, canned, transaction_id="tx-A", logical_turn=8, attempt=1)
    assert plan.status_apply == [], "무효 상태 후보가 계획에 남았습니다"
    assert plan.item_deltas == [], "무효 캐릭터 아이템 후보가 계획에 남았습니다"
    assert plan.dropped, "탈락 후보가 dropped에 기록되지 않았습니다"

    # 실제 구동 후에도 canonical 무변화(적용부는 raw result를 보지 않는다).
    await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="tx-A", logical_turn=8, attempt=1)

    assert sess.statuses.get("테스터", []) == [], "무효 상태가 되살아나 적용됐습니다"
    assert "없는사람" not in sess.statuses, "무효 캐릭터 상태가 적용됐습니다"
    assert "없는사람" not in (sess.resources or {}), "무효 캐릭터 아이템이 적용됐습니다"


# ── B. 동치 중복 후보는 이중 적용될 수 없다 ───────────────────────
async def test_pa_b_duplicate_candidate_applies_once(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.resources = {}

    canned = {
        "location": {"name": "마을"},
        "item_changes": [
            {"target": "테스터", "item": "물", "delta": 5},
            {"target": "테스터", "item": "물", "delta": 5},   # 동치 중복
        ],
        "status_scores": [], "npcs_met": [], "companions": {},
        "datetime": {}, "situation": {},
    }

    plan = tp.build_extraction_plan(
        sess, canned, transaction_id="tx-B", logical_turn=8, attempt=1)
    water = [e for e in plan.item_deltas if e["item"] == "물"]
    assert len(water) == 1, "동치 아이템 후보가 dedup되지 않았습니다"

    await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="tx-B", logical_turn=8, attempt=1)

    assert sess.resources.get("테스터", {}).get("물") == 5, (
        "동치 중복 후보가 이중 적용되어 +10이 되었습니다(last-candidate-wins)")


# ── C. 상호 모순은 last-write-wins가 되지 않는다 ──────────────────
async def test_pa_c_conflict_is_not_last_write_wins(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.companions = []
    sess.met_npcs = []

    canned = {
        "location": {"name": "마을"},
        "companions": {"joined": ["백가"], "left": ["백가"]},  # 합류·이탈 모순
        "status_scores": [], "item_changes": [], "npcs_met": [],
        "datetime": {}, "situation": {},
    }

    plan = tp.build_extraction_plan(
        sess, canned, transaction_id="tx-C", logical_turn=8, attempt=1)
    assert "백가" not in plan.companions_joined, "모순 이름이 joined에 남았습니다"
    assert "백가" not in plan.companions_left, "모순 이름이 left에 남았습니다"
    assert any(c["target"] == "백가" for c in plan.conflicts), "모순이 진단되지 않았습니다"

    await _drive_extraction(
        monkeypatch, cog, sess, master_channel, canned,
        transaction_id="tx-C", logical_turn=8, attempt=1)

    # last-write-wins였다면 join의 부수효과로 met_npcs에 흔적이 남는다.
    assert "백가" not in (sess.companions or []), "모순 이름이 동행에 적용됐습니다"
    assert "백가" not in (sess.met_npcs or []), (
        "모순 처리가 last-write-wins(합류 부수효과)로 흔적을 남겼습니다")


# ── D. raw result를 폐기해도 계획만으로 동일한 의도 상태가 나온다 ──
async def test_pa_d_plan_only_application_is_authoritative(
        wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.resources = {}
    sess.statuses = {}
    sess.companions = []
    sess.met_npcs = []

    canned = {
        "location": {"name": "숲길"},                       # 마을 → 숲길(해상도 가능)
        "item_changes": [{"target": "테스터", "item": "물", "delta": 5}],
        "status_scores": [{"target": "테스터", "status": "부상", "score": 99}],
        "companions": {"joined": ["임성진"], "left": []},
        "npcs_met": ["행인"], "datetime": {}, "situation": {},
    }

    # 1) 계획을 만든다.
    plan = tp.build_extraction_plan(
        sess, canned, transaction_id="tx-D", logical_turn=8, attempt=1)

    # 2) raw result를 폐기한다 — 적용부는 raw를 절대 볼 수 없다.
    plan.result = {}
    canned.clear()

    # 3) 계획만으로 적용한다.
    await cog._apply_extraction_plan(sess, plan, master_channel)

    # 4) 의도한 canonical 변화가 모두 반영됐다(계획=유일 권위).
    assert sess.resources.get("테스터", {}).get("물") == 5, "아이템이 계획만으로 반영 안 됨"
    assert "부상" in sess.statuses.get("테스터", []), "상태가 계획만으로 반영 안 됨"
    assert "임성진" in (sess.companions or []), "동행 합류가 계획만으로 반영 안 됨"
    assert "행인" in (sess.met_npcs or []), "만난 NPC가 계획만으로 반영 안 됨"
    assert sess.world_timeline.get("current_location") == "숲길", (
        "장소 이동이 계획만으로 반영 안 됨")
