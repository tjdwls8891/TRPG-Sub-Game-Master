# -*- coding: utf-8 -*-
"""H05-B — 관찰 매니페스트·처리(순수): 누설 방지 · 코드 소유 시간 · PC 믿음 비창작 · 선후 검증."""
from __future__ import annotations

import pytest

from core import cognition as CG
from core import player_world as PW
from core import spatial as SP
from core import turn_world as TW
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h04b_events_progression import K
from tests.policy.test_h05a_intent_planning import PC, ctx_of, make_session

pytestmark = pytest.mark.policy

REC = {"awareness": "CONTENT", "fragments": [], "belief": None, "acquisitions": [], "belief_provenance": [],
       "secrecy_awareness": None}


def _session(kang_at="항구"):
    s, D = make_session(edges=())
    s.spatial_state["placements"][KANG] = SP.at_place(kang_at)
    s.cognition_state = {f"actor:{KANG}": {"S40": dict(REC)}}
    return s, D


def test_exposure_candidates_require_current_turn_grounds():
    # R2: 장면 안팎 NPC가 내용을 안다는 사실만으로는 노출 후보가 아니다(이번 턴 노출 근거 없음)
    s, D = _session("항구")
    assert TW.build_observation_manifest(D, ctx_of(s), None, s).infos == ()
    s2, D2 = _session("외딴곳")
    assert TW.build_observation_manifest(D2, ctx_of(s2), None, s2).infos == ()


def test_observation_never_sets_pc_belief_and_ignores_invalid_keys():
    s, D = _session()
    c = ctx_of(s)
    m = TW.ObservationManifest(infos=(("X1", "S40", "공개 요약"),))      # 노출 근거가 있는 후보 1건
    res = TW.process_observation(c, s, None, m, {"info_exposed": ["X1", "X9"], "belief": "CERTAIN",
                                                 "stance_belief": "CERTAIN"},
                                 transaction_id="t", narrative_seed="seed")
    assert res.status == TW.TW_READY
    assert not [o for o in res.ops if o.op_type == CG.OP_COGNITION_BELIEF_SET]
    assert CG.record_of(res.projected_world, f"actor:{PC}", "S40")["belief"] is None
    assert any(d["code"] == "INFO_EXPOSURE_INVALID" for d in res.diagnostics)
    assert s.cognition_state.get(f"actor:{PC}") is None                    # 정본 불변


def test_extra_time_discarded_when_code_owned_and_clamped_otherwise():
    s, D = _session()
    c = ctx_of(s)
    owned = TW.ObservationManifest(extra_time_allowed=False)
    r = TW.process_observation(c, s, None, owned, {"extra_minutes": 30}, transaction_id="t", narrative_seed="x")
    assert r.extra_minutes == 0 and any(d["code"] == "EXTRA_TIME_CODE_OWNED" for d in r.diagnostics)
    free = TW.ObservationManifest(extra_time_allowed=True)
    r = TW.process_observation(c, s, None, free, {"extra_minutes": 10_000}, transaction_id="t", narrative_seed="x")
    assert r.extra_minutes == TW.MAX_EXTRA_MINUTES
    assert r.projected_world.domain("world_clock")["minute_index"] == K + TW.MAX_EXTRA_MINUTES


def test_tie_plan_requires_single_valid_order():
    s, D = _session()
    c = ctx_of(s)
    tie_plan = PW.PlayerWorldPlan(PW.PLAN_TIE)
    for raw in ({}, {"tie_order": "O7"}, {"tie_order": "NONE"}):
        assert TW.process_observation(c, s, tie_plan, TW.ObservationManifest(), raw, transaction_id="t",
                                      narrative_seed="x").status == TW.TW_TIE_UNRESOLVED


def test_no_pending_delivery_without_new_observable_consequence():
    s, D = _session()
    c = ctx_of(s)
    res = TW.process_observation(c, s, None, TW.ObservationManifest(), {}, transaction_id="t", narrative_seed="x")
    assert res.status == TW.TW_READY and res.pending == []
    assert not hasattr(TW, "continuation_plan") and not hasattr(TW, "ForegroundContinuationPlan")   # P1 은퇴
