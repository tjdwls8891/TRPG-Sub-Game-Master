# -*- coding: utf-8 -*-
"""H06 R2-07 — PLAYER_TURN의 필수 세계 연산이 handler에서 거부되면 성공 묘사·커밋으로 진행하지 않는다.

진행 엔진은 거부된 단위를 진단(UNIT_REJECTED)으로 남기고 버린다. 플레이어 행동의 기반 연산(base)이
버려진 채 계획이 PLANNED로 남으면 '도착했다'는 묘사와 함께 이동 없는 커밋이 된다(조용한 탈락).
보정: 기반 단위 거부 = 결정적 계획 충돌(묘사 전 FAILED_SYSTEM, 청구 0) — INITIALIZATION과 같은 규칙.
"""
from __future__ import annotations

import pytest

from core import player_world as PW
from core import spatial as SP
from core import turn_transaction as tt
from core import world_mutation as WM
from tests.policy.test_h05a_intent_planning import ctx_of, go, make_session, manifest
from tests.policy.test_h05bc_turn_wiring import PC, drig, judgment, rig, turn, world_of  # noqa: F401
from core import action_intent as AI

pytestmark = pytest.mark.policy


@pytest.fixture
def reject_transit(monkeypatch):
    """ACTOR_TRANSIT_SET handler 거부 주입(데이터 불일치·검증 강화 모사)."""
    reg = WM.PRODUCTION_REGISTRY
    orig = reg.handler_for(SP.OP_ACTOR_TRANSIT_SET)

    def _boom(view, op, payload):
        raise WM.WorldMutationError(f"{op.op_id}: 주입된 거부")
    monkeypatch.setitem(reg._handlers, SP.OP_ACTOR_TRANSIT_SET, _boom)
    yield orig


def test_rejected_base_op_makes_plan_conflict_not_silent_success(reject_transit):
    s, _ = make_session(edges=("e_hm#fwd",))
    m = manifest(s)
    p = PW.plan_player_turn(ctx_of(s), s, AI.resolve_intent(m, go(m, "시장"), world=s),
                            transaction_id="t", narrative_seed="x")
    assert p.status == PW.PLAN_CONFLICT and p.ops == ()


async def test_rejected_base_op_fails_turn_before_narration(drig, reject_transit):
    r = drig
    r.make(edges=("e_hm#fwd",))
    before = world_of(r.sess)
    r.prov.routes["judgment"] = judgment("시장")
    tx = await turn(r, "시장으로 간다")
    assert "narration" not in r.prov.calls                                 # '도착했다' 묘사 없음
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert world_of(r.sess) == before and SP.placement_of(r.sess, PC) == SP.at_place("항구")


def test_rejected_observation_acquire_is_conflict_not_dropped(monkeypatch):
    from core import cognition as CG
    from core import turn_world as TW

    def _boom(view, op, payload):
        raise WM.WorldMutationError(f"{op.op_id}: 주입된 거부")
    monkeypatch.setitem(WM.PRODUCTION_REGISTRY._handlers, CG.OP_COGNITION_ACQUIRE, _boom)
    s, _ = make_session(edges=())
    c = ctx_of(s)
    m = TW.ObservationManifest(infos=(("X1", "S40", "공개 요약"),))
    res = TW.process_observation(c, s, None, m, {"info_exposed": ["X1"]}, transaction_id="t", narrative_seed="x")
    assert res.status == TW.TW_CONFLICT and res.conflict["base_rejected"]
