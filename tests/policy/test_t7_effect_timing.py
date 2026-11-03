# -*- coding: utf-8 -*-
"""리팩터링 T-7 — 같은 턴 효과 시점(G9)·잠복 판정 근거(G12).

G9 통합 흐름은 tests/defects/test_g9_same_turn_effects.py(균열 골든 스크립트)가 고정한다.
"""
from __future__ import annotations

import pytest

from core import status_rules as SR
from indaim.rules.vitals import logic as VL
from tests.policy.test_v741_status_extraction import MERGED, START

pytestmark = pytest.mark.policy
LAT = MERGED["감염 의심"]["latent"]


def test_g12_latent_roll_keyed_by_actor_id_not_name():
    a = SR.latent_roll("seed", "김철수", "감염 의심", 100, LAT, actor_id="pc.1")
    b = SR.latent_roll("seed", "이영희", "감염 의심", 100, LAT, actor_id="pc.1")
    assert a == b                                                     # 이름(개명)과 무관
    rolls = [SR.latent_roll(f"s{i}", "P", "감염 의심", 100, LAT, actor_id="pc.1") for i in range(40)]
    olds = [SR.latent_roll(f"s{i}", "P", "감염 의심", 100, LAT) for i in range(40)]
    assert rolls != olds                                             # id가 없을 때만 옛 이름 근거(v1)


def test_g12_decide_turn_uses_actor_id_from_facts():
    facts = {"now": START + 10, "before": START, "start": START, "place": None, "daypart": None, "activity": None,
             "actor_ids": {"P": "pc.1"}}
    td = VL.decide_turn(MERGED, pc_name="P", statuses={}, clocks={}, facts=facts,
                        apply_in=[{"target": "P", "status": "감염 의심", "score": 90}], clear_in=[], seed="s3")
    want = SR.latent_roll("s3", "다른 이름", "감염 의심", START + 10, LAT, actor_id="pc.1")
    assert td.clocks["P"]["latent"]["감염"] == want


def test_g9_code_applied_status_is_cared_in_the_same_decision():
    """코드가 새로 붙이는 상태(전투 부상)도 같은 결정 안에서 붙어 같은 턴 처치가 본다."""
    assert MERGED["부상"]["care"]["응급처치"] == "treat"
    facts = {"now": START, "before": START, "start": START, "place": None, "daypart": None, "activity": None,
             "hits": {"N": 1}, "care_by": {"N": frozenset({"응급처치"})}}
    td = VL.decide_turn(MERGED, pc_name="P", statuses={}, clocks={}, facts=facts, apply_in=[], clear_in=[], seed="s")
    assert {"target": "N", "status": "부상", "score": 100} in td.apply
    assert td.clocks["N"]["cared"] == {"부상": START} and td.clocks["N"]["stage"] == {"부상": 1}
