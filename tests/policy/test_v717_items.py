# -*- coding: utf-8 -*-
"""v7.17 아이템 — 인수 A01~F09(원본/아이템/04_인수_검증표.md).

순수 서비스(core.items) 단위 + DYNAMIC 자동 턴 통합(추출 world_observation.item_ops → READY → CommitCoordinator).
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
from types import SimpleNamespace

import pytest

import core
import prompts
from core import items as IT
from core import information as INFO
from core import turn_transaction as tt
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401 — 픽스처 재사용
    PC, drig, extraction, judgment, turn)
from tests.policy.test_ready_barrier import _json, _usage, rig  # noqa: F401

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]


def S(res=None, inst=None, places=None):
    return SimpleNamespace(resources=copy.deepcopy(res or {}), item_instances=copy.deepcopy(inst or {}),
                           place_items=copy.deepcopy(places or {}), item_facts={})


def man(s, *, pc="P", npcs=(), place=None, defs=None, disc=(), recipes=None):
    return IT.build_manifest(s, pc_name=pc, scene_owner_names=npcs, scene_place=place, defs=defs,
                             discovery_rows=disc, recipes=recipes)


def key_of(m, label):
    """매니페스트 키 — 표시 문구 일부로 찾는다(H·L)."""
    for r in m.holdings:
        if label in r[5]:
            return r[0]
    for r in m.places:
        if label in r[5]:
            return r[0]
    raise AssertionError(label)


def own(m, name):
    return next(k for k, n, _l in m.owners if n == name)


def plan(s, m, ops, **kw):
    kw.setdefault("pc_name", "P")
    return IT.build_plan(s, m, ops, transaction_id=kw.pop("tx", "tx1"), **kw)


def apply(s, m, ops, **kw):
    p = plan(s, m, ops, **kw)
    IT.apply_plan(s, p)
    return p


# ══════════════════════════════════════════════════════════════
# A. 수량·양도
# ══════════════════════════════════════════════════════════════

def test_A01_two_consumes_same_turn_both_count():
    s = S({"P": {"사과": 3}})
    m = man(s)
    h = key_of(m, "사과")
    p = apply(s, m, [{"op": "CONSUME", "owner": "O1", "item": h, "quantity": 1}] * 2)
    assert s.resources["P"] == {"사과": 1} and [r["delta"] for r in p.report] == [-1, -1]


def test_A02_bad_order_never_creates_two():
    s = S({"P": {"사과": 1}, "N": {}})
    m = man(s, npcs=["N"])
    h = key_of(m, "사과")
    with pytest.raises(IT.ItemContractError):          # 1개로 2개를 쓸 수 없다(0 절삭 없음)
        plan(s, m, [{"op": "CONSUME", "owner": "O1", "item": h, "quantity": 2}])
    # item_changes만 온 옛 형태 — 원천 없는 +1은 거부, −1만 소모
    p = apply(s, m, None, legacy_item_deltas=[{"target": "P", "item": "사과", "delta": 1},
                                              {"target": "P", "item": "사과", "delta": -1}])
    assert s.resources["P"].get("사과", 0) == 0
    assert any(d["code"] == "ITEM_GAIN_UNSOURCED_REJECTED" for d in p.diagnostics)


def test_A03_chained_transfer_preserves_total():
    s = S({"P": {"사과": 1}, "A": {}, "B": {}})
    m = man(s, npcs=["A", "B"])
    h = key_of(m, "사과")
    apply(s, m, [{"op": "TRANSFER", "owner": "O1", "to": own(m, "A"), "item": h, "quantity": 1},
                 {"op": "TRANSFER", "owner": own(m, "A"), "to": own(m, "B"), "item": h, "quantity": 1}])
    assert s.resources["P"] == {} and s.resources["A"] == {} and s.resources["B"] == {"사과": 1}
    # 순서가 뒤집히면 A가 아직 없으므로 전체 실패
    s2 = S({"P": {"사과": 1}, "A": {}, "B": {}})
    m2 = man(s2, npcs=["A", "B"])
    with pytest.raises(IT.ItemContractError):
        plan(s2, m2, [{"op": "TRANSFER", "owner": own(m2, "A"), "to": own(m2, "B"), "item": h, "quantity": 1},
                      {"op": "TRANSFER", "owner": "O1", "to": own(m2, "A"), "item": h, "quantity": 1}])


def test_A04_swap_two_items():
    s = S({"P": {"사과": 1, "칼": 1}, "N": {"빵": 2}})
    m = man(s, npcs=["N"])
    apply(s, m, [{"op": "TRANSFER", "owner": "O1", "to": own(m, "N"), "item": key_of(m, "P(PC)의 사과"), "quantity": 1},
                 {"op": "TRANSFER", "owner": own(m, "N"), "to": "O1", "item": key_of(m, "N의 빵"), "quantity": 1}])
    assert s.resources["P"] == {"칼": 1, "빵": 1} and s.resources["N"] == {"빵": 1, "사과": 1}


def test_A05_double_give_of_single_rejected_whole():
    s = S({"P": {"사과": 1}, "A": {}, "B": {}})
    before = copy.deepcopy(s.__dict__)
    m = man(s, npcs=["A", "B"])
    h = key_of(m, "사과")
    with pytest.raises(IT.ItemContractError):
        plan(s, m, [{"op": "TRANSFER", "owner": "O1", "to": own(m, "A"), "item": h, "quantity": 1},
                    {"op": "TRANSFER", "owner": "O1", "to": own(m, "B"), "item": h, "quantity": 1}])
    assert s.__dict__ == before                               # 부분 복제·조용한 절삭 없음


def test_A06_identical_independent_events_counted():
    s = S({"P": {}}, places={"src1": {"place": "광장", "key": "돌", "quantity": 3}})
    m = man(s, place="광장")
    l1 = key_of(m, "돌")
    apply(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": l1, "quantity": 1}] * 2)
    assert s.resources["P"] == {"돌": 2} and s.place_items["src1"]["quantity"] == 1


async def test_A07_same_transaction_applies_once(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 2}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}]})]
    tx = await turn(r, "붕대를 감는다")
    assert tx.status == tt.TurnStatus.COMMITTED and r.sess.resources["테스터"] == {"붕대": 1}
    prep = tx.preparation
    prep.extraction_plan.applied = False                      # 재처리 시도
    await r.gm._apply_prepared_extraction(r.sess, prep)
    assert r.sess.resources["테스터"] == {"붕대": 1}


def test_A08_owner_by_key_not_name_similarity():
    s = S({"P": {}, "김철수": {"빵": 1}, "김철수2": {"빵": 1}})
    m = man(s, npcs=["김철수", "김철수2"])
    apply(s, m, [{"op": "TRANSFER", "owner": own(m, "김철수2"), "to": "O1",
                  "item": key_of(m, "김철수2의 빵"), "quantity": 1}])
    assert s.resources["김철수"] == {"빵": 1} and s.resources["김철수2"] == {}


# ══════════════════════════════════════════════════════════════
# B. 발견·보관
# ══════════════════════════════════════════════════════════════

DISC = (("D1", "rt.ambient.a", "노끈", "OBJECT"), ("D2", "rt.ambient.b", "노끈", "OBJECT"))


def test_B01_B02_B04_discovery_take_once_per_source():
    s = S({"P": {}})
    m = man(s, place="창고", disc=DISC)
    p = apply(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": "D1", "quantity": 1}], discovery_taken=["D1"])
    assert p.discovery_takes == ["D1"] and s.resources["P"] == {"노끈": 1}    # D키와 ACQUIRE는 한 수거
    with pytest.raises(IT.ItemContractError):                                # 같은 D키 두 번
        plan(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": "D1", "quantity": 1}] * 2)
    assert "D2" not in p.discovery_takes                                     # 동명 다른 원천 불변


def test_B03_unsourced_plus_one_cannot_take_current_discovery():
    s = S({"P": {}})
    m = man(s, place="창고", disc=DISC)
    p = apply(s, m, None, legacy_item_deltas=[{"target": "P", "item": "노끈", "delta": 1}])
    assert p.discovery_takes == [] and s.resources["P"] == {}


def test_B05_B06_drop_then_partial_pickup():
    s = S({"P": {"사과": 3}})
    m = man(s, place="광장")
    apply(s, m, [{"op": "DROP", "owner": "O1", "item": key_of(m, "사과"), "quantity": 2}])
    assert s.resources["P"] == {"사과": 1}
    (sid, rec), = s.place_items.items()
    assert rec == {"place": "광장", "key": "사과", "quantity": 2, "origin": "DROP", "by": "P", "tx": "tx1"}
    m = man(s, place="광장")
    apply(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, "바닥에 놓인"), "quantity": 1}], tx="tx2")
    assert s.resources["P"] == {"사과": 2} and s.place_items[sid]["quantity"] == 1


def test_B07_taken_by_other_then_pc_cannot_duplicate():
    s = S({"P": {}, "N": {}}, places={"src": {"place": "광장", "key": "칼", "quantity": 1}})
    m = man(s, npcs=["N"], place="광장")
    l1 = key_of(m, "칼")
    with pytest.raises(IT.ItemContractError):
        plan(s, m, [{"op": "ACQUIRE", "owner": own(m, "N"), "item": l1, "quantity": 1},
                    {"op": "ACQUIRE", "owner": "O1", "item": l1, "quantity": 1}])
    apply(s, m, [{"op": "ACQUIRE", "owner": own(m, "N"), "item": l1, "quantity": 1}])
    assert s.resources["N"] == {"칼": 1} and s.place_items == {}


def test_B08_drop_does_not_touch_cognition():
    s = S({"P": {"쪽지": 1}})
    s.cognition_state = {"actor:x": {"i": {"awareness": "CONTENT"}}}
    before = copy.deepcopy(s.cognition_state)
    apply(s, man(s, place="광장"), [{"op": "DROP", "owner": "O1", "item": "H1", "quantity": 1}])
    assert s.cognition_state == before                       # 소유 변화는 인지와 분리


# ══════════════════════════════════════════════════════════════
# C. 종류·단위·상태·특수 객체
# ══════════════════════════════════════════════════════════════

DEFS = {"항생제": {"kind": "항생제", "units": {"상자": {"contains": {"unit": "알", "count": 10}}, "알": {}}},
        "진통제": {"kind": "진통제"},
        "손전등": {"kind": "손전등", "categories": ["조명"]},
        "공구": {"kind": "공구", "categories": ["공구"]},
        "열쇠": {"kind": "열쇠", "unique": True, "info_refs": ["doc.key"],
                 "instances": [{"instance_id": "key.a", "owner": "N", "place": None}]},
        "무전기": {"kind": "무전기", "parts": [{"kind": "배터리", "unit": None, "count": 2}]},
        "궤짝": {"kind": "궤짝", "mass_grade": 5},
        "벽돌": {"kind": "벽돌", "mass_grade": 4, "bulk_grade": 2},
        "솜": {"kind": "솜", "mass_grade": 1, "bulk_grade": 4}}


def test_C01_phrase_with_count_parses_exactly():
    assert IT.canonical_bag({"항생제 두 알": 1}, DEFS) == {"항생제[알]": 2}


def test_C02_C03_open_requires_authored_contents():
    s = S({"P": {"항생제[상자]": 1, "진통제[상자]": 1}})
    m = man(s, defs=DEFS)
    with pytest.raises(IT.ItemContractError):
        plan(s, m, [{"op": "OPEN", "owner": "O1", "item": key_of(m, "진통제[상자]"), "quantity": 1}], defs=DEFS)
    apply(s, m, [{"op": "OPEN", "owner": "O1", "item": key_of(m, "항생제[상자]"), "quantity": 1}], defs=DEFS)
    assert s.resources["P"] == {"항생제[알]": 10, "진통제[상자]": 1}


def test_C04_C05_no_unit_merge_and_vague_kept_opaque():
    bag = IT.canonical_bag({"진통제[판]": 1, "진통제[알]": 3, "말린 생선 몇 조각": 1}, DEFS)
    assert bag == {"진통제[판]": 1, "진통제[알]": 3, "말린 생선 몇 조각": 1}


def test_C06_C07_damage_then_use_blocked_repair_restores():
    s = S({"P": {"손전등": 2}})
    m = man(s, defs=DEFS)
    apply(s, m, [{"op": "DAMAGE", "owner": "O1", "item": "H1", "quantity": 1, "condition": "고장"}], defs=DEFS)
    assert s.resources["P"] == {"손전등": 1, "손전등〈고장〉": 1}
    m = man(s, defs=DEFS)
    broken = key_of(m, "손전등〈고장〉")
    with pytest.raises(IT.ItemContractError):
        plan(s, m, [{"op": "USE", "owner": "O1", "item": broken, "quantity": 1}], defs=DEFS)
    apply(s, m, [{"op": "REPAIR", "owner": "O1", "item": broken, "quantity": 1}], defs=DEFS, tx="t2")
    assert s.resources["P"] == {"손전등": 2}


def test_C08_C09_unique_instance_not_in_stock_and_keeps_identity():
    defn = {"item_definitions": DEFS}
    s = S({"N": {"열쇠": 1, "빵": 1}, "P": {}})
    out = IT.genesis_item_state(defn, s)
    assert out["resources"]["N"] == {"빵": 1} and out["item_instances"]["key.a"]["owner"] == "N"
    s.resources, s.item_instances = out["resources"], out["item_instances"]
    m = man(s, npcs=["N"], place="광장", defs=DEFS)
    apply(s, m, [{"op": "TRANSFER", "owner": own(m, "N"), "to": "O1", "item": key_of(m, "N의 열쇠"), "quantity": 1}],
          defs=DEFS)
    m = man(s, place="광장", defs=DEFS)
    apply(s, m, [{"op": "DROP", "owner": "O1", "item": key_of(m, "열쇠"), "quantity": 1}], defs=DEFS, tx="t2")
    m = man(s, place="광장", defs=DEFS)
    apply(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, "열쇠"), "quantity": 1}], defs=DEFS, tx="t3")
    r = s.item_instances["key.a"]
    assert r["owner"] == "P" and r["previous_owners"] == ["N"] and r["info_refs"] == ["doc.key"]
    assert "열쇠" not in s.resources["P"]


def test_C10_category_only_from_authoring():
    assert "공구" in IT.capabilities({"공구": 1}, DEFS)
    assert "공구" not in IT.capabilities({"쇠막대": 1}, DEFS)


# ══════════════════════════════════════════════════════════════
# D. 제작·판정·운반
# ══════════════════════════════════════════════════════════════

RECIPES = {"rc.torch": {"recipe_id": "rc.torch", "name": "횃불", "inputs": [{"kind": "막대", "unit": None, "count": 1},
                                                                         {"kind": "천", "unit": None, "count": 1}],
                        "outputs": [{"kind": "횃불", "unit": None, "count": 1}], "tools": []}}


def test_D01_D04_recipe_exact_consumption_and_shortage_rejected():
    s = S({"P": {"막대": 2, "천": 1}})
    m = man(s, recipes=RECIPES)
    apply(s, m, [{"op": "CRAFT", "owner": "O1", "item": "NONE", "recipe": "C1", "quantity": 1}], recipes=RECIPES)
    assert s.resources["P"] == {"막대": 1, "횃불": 1}
    m = man(s, recipes=RECIPES)
    with pytest.raises(IT.ItemContractError):                    # 천 부족 — 성공 서술이어도 정본 생성 불가
        plan(s, m, [{"op": "CRAFT", "owner": "O1", "item": "NONE", "recipe": "C1", "quantity": 1}], recipes=RECIPES)


def test_D02_D03_free_craft_general_only_no_special():
    s = S({"P": {"천": 2, "막대": 1}})
    m = man(s, defs=DEFS)
    apply(s, m, [{"op": "CRAFT", "owner": "O1", "item": "NONE", "quantity": 1,
                  "inputs": [{"item": key_of(m, "천"), "quantity": 1}], "outputs": [{"name": "붕대", "quantity": 1}]}],
          defs=DEFS)
    assert s.resources["P"] == {"천": 1, "막대": 1, "붕대": 1}
    m = man(s, defs=DEFS)
    with pytest.raises(IT.ItemContractError):                    # 저작 특수 물건은 자유 제작 불가
        plan(s, m, [{"op": "CRAFT", "owner": "O1", "item": "NONE", "quantity": 1,
                     "inputs": [{"item": key_of(m, "막대"), "quantity": 1}], "outputs": [{"name": "열쇠", "quantity": 1}]}],
             defs=DEFS)


def test_D05_D06_D11_failed_craft_records_only_described_loss():
    s = S({"P": {"천": 2}})
    m = man(s)
    apply(s, m, [{"op": "CONSUME", "owner": "O1", "item": key_of(m, "천"), "quantity": 1}])   # 실패 중 실제 손실만
    assert s.resources["P"] == {"천": 1}
    with pytest.raises(IT.ItemContractError):                    # 결과 없는 '진행 중' 제작은 정본 없음
        plan(s, man(s), [{"op": "CRAFT", "owner": "O1", "item": "NONE", "quantity": 1,
                          "inputs": [{"item": "H1", "quantity": 1}], "outputs": []}])


def test_D07_disassemble_authored_parts_no_duplication():
    s = S({"P": {"무전기": 1}})
    m = man(s, defs=DEFS)
    with pytest.raises(IT.ItemContractError):
        plan(s, m, [{"op": "DISASSEMBLE", "owner": "O1", "item": "H1", "quantity": 1,
                     "outputs": [{"name": "배터리", "quantity": 3}]}], defs=DEFS)
    apply(s, m, [{"op": "DISASSEMBLE", "owner": "O1", "item": "H1", "quantity": 1,
                  "outputs": [{"name": "배터리", "quantity": 2}]}], defs=DEFS)
    assert s.resources["P"] == {"배터리": 2}


def test_D08_item_tool_only_via_existing_method_mod():
    src = (ROOT / "core/judgment.py").read_text(encoding="utf-8")
    assert "items" not in src and "resources" not in src        # 판정 수식에 아이템 별도 보너스 없음(M 한 곳)


def test_D09_D10_carry_both_axes_and_ungraded_cap():
    places = {"s": {"place": "광장", "key": "벽돌", "quantity": 2}, "c": {"place": "광장", "key": "솜", "quantity": 2},
              "g": {"place": "광장", "key": "궤짝", "quantity": 1}, "u": {"place": "광장", "key": "모래", "quantity": 500}}

    def try_take(label, q, start=None):
        s = S({"P": dict(start or {})}, places=places)
        m = man(s, place="광장", defs=DEFS)
        return lambda: plan(s, m, [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, label), "quantity": q}],
                            defs=DEFS)
    try_take("벽돌 ×2(바닥", 1)()                                     # 무게 30 ≤ 40
    with pytest.raises(IT.ItemContractError):
        try_take("벽돌 ×2(바닥", 2)()                                 # 무게 60 > 40
    try_take("솜 ×2(바닥", 1)()                                       # 부피 30 ≤ 40
    with pytest.raises(IT.ItemContractError):
        try_take("솜 ×2(바닥", 2)()                                   # 무게 2지만 부피 60 > 40 — 축 별도
    with pytest.raises(IT.ItemContractError):
        try_take("궤짝", 1)()                                         # 운반 불가 등급
    with pytest.raises(IT.ItemContractError):
        try_take("모래", 500)()                                       # 미저작 대량 — 상한(무제한 0 처리 아님)
    try_take("모래", 3)()                                             # 미저작 소량은 막지 않는다(일괄 운반 불가 아님)
    assert IT.carry_load({"모래": 3}, [], DEFS)["ungraded"] == 3


# ══════════════════════════════════════════════════════════════
# E. 세계·사건·지식·NPC
# ══════════════════════════════════════════════════════════════

def test_E01_E04_facts_used_paid_distinct_for_next_turn():
    s = S({"P": {"손전등": 1, "빵": 2}, "N": {}})
    m = man(s, npcs=["N"], defs=DEFS)
    p = apply(s, m, [{"op": "USE", "owner": "O1", "item": key_of(m, "손전등"), "quantity": 1},
                     {"op": "TRANSFER", "owner": "O1", "to": own(m, "N"), "item": key_of(m, "빵"), "quantity": 1}],
              defs=DEFS)
    assert p.facts == {"used": ["손전등"], "paid": ["빵"]}
    assert s.item_facts == {"tx": "tx1", "used": ["손전등"], "paid": ["빵"]} and s.resources["P"]["손전등"] == 1
    from core import world_refs as WR
    assert WR.OBSERVER_USED_KEY != WR.OBSERVER_PAID_KEY


def test_E02_E03_show_is_not_payment_payment_op_is_code_owned():
    s = S({"P": {"항생제": 1}})
    assert IT.payment_op(s, pc_name="P", capability="항생제", quantity=1)["op"] == "PAY"
    assert IT.payment_op(s, pc_name="P", capability="항생제", quantity=2) is None
    p = apply(s, man(s), [{"op": "USE", "owner": "O1", "item": "H1", "quantity": 1}])   # 보여 주기·사용 ≠ 지급
    assert p.facts["paid"] == [] and s.resources["P"] == {"항생제": 1}
    p = apply(s, man(s), [], payments=[IT.payment_op(s, pc_name="P", capability="항생제", quantity=1)], tx="t2")
    assert p.facts["paid"] == ["항생제"] and s.resources["P"] == {}


def test_E05_read_yields_info_ids_only_no_belief():
    defs = {"편지": {"kind": "편지", "info_refs": ["doc.letter"]}}
    s = S({"P": {"편지": 1}})
    p = apply(s, man(s, defs=defs), [{"op": "READ", "owner": "O1", "item": "H1", "quantity": 1}], defs=defs)
    assert p.read_info_ids == ["doc.letter"] and s.resources["P"] == {"편지": 1}


def test_E06_npc_runtime_stock_wins_over_cache_seed():
    src = (ROOT / "core/cache.py").read_text(encoding="utf-8")
    assert 'and not getattr(session, "definition_revision", None)' in src      # 바인딩 세션은 캐시 초기 소지 생략
    s = S({"P": {}, "N": {"빵": 0}})
    blk = IT.item_block(s, pc_name="P", scene_owner_names=["N"])
    assert "N: " not in blk                                                     # 런타임 0 → 초기값 복원 없음


def test_E07_member_capability_not_item_namespace():
    assert "member:namhang" not in IT.capabilities({"member:namhang 증표": 1})


# ══════════════════════════════════════════════════════════════
# F. 저장·회귀·호환·비용
# ══════════════════════════════════════════════════════════════

async def test_F01_contract_failure_never_partially_applies(drig):
    # v7.29(사용자 승인 B1+B2): 거부 사유를 덧붙여 같은 턴 1회 재추출 → 또 거부되면 물건 연산만 빼고 턴 확정.
    #   어느 경우에도 어긋난 연산은 부분 적용되지 않는다(물건 정본 불변).
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 1}}
    r.prov.routes["judgment"] = judgment()
    bad = extraction({"item_ops": [{"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 5}]})
    prompts = []

    def _ext(kwargs):
        prompts.append(kwargs["contents"][0].parts[0].text)
        return bad
    r.prov.routes["extraction"] = _ext
    before = copy.deepcopy(r.sess.resources)
    tx = await turn(r, "붕대를 모두 쓴다")
    assert len(prompts) == 2 and "정본 검증에서 거부됨" not in prompts[0]
    assert "정본 검증에서 거부됨" in prompts[1] and "보유 1 < 5" in prompts[1]
    assert tx.preparation.phase != core.turn_preparation.PREP_RETRY_PENDING      # 막다른 재시도 없음
    assert r.sess.resources == before and r.sess.turn_count >= 1


async def test_F01b_corrected_reextraction_applies_the_fixed_ops(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 2}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [
        extraction({"item_ops": [{"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 5}]}),
        extraction({"item_ops": [{"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}]})]
    await turn(r, "붕대를 하나 쓴다")
    assert r.sess.resources["테스터"] == {"붕대": 1}


async def test_F02_F03_commit_then_rewind_restores_item_fields(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 2}}
    r.sess.initial_knowledge_progress = {"version": 1, "players": {"x": {"PROFILE": {"state": "COMMITTED"}}}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "숨을 고른다")
    before = {f: copy.deepcopy(getattr(r.sess, f)) for f in ("resources", *IT.ITEM_FIELDS)}
    ik = copy.deepcopy(r.sess.initial_knowledge_progress)
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "DROP", "owner": "O1", "item": "H1", "quantity": 1}]})]
    tx = await turn(r, "붕대 하나를 내려놓는다")
    assert tx.status == tt.TurnStatus.COMMITTED
    assert r.sess.resources["테스터"] == {"붕대": 1} and len(r.sess.place_items) == 1
    assert r.sess.item_facts["tx"] == tx.transaction_id
    for f in IT.ITEM_FIELDS:
        assert f in core.turn_history.REVERSIBLE_FIELDS and f in core.turn_preparation.CANONICAL_DOMAINS
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert {f: getattr(r.sess, f) for f in ("resources", *IT.ITEM_FIELDS)} == before   # 재고·현장·사실 함께
    assert r.sess.initial_knowledge_progress == ik             # 초기 전달 기록은 턴 되감기 대상 아님(X10)


async def test_F04_session_fields_roundtrip_defaults(rig):
    for f, d in IT.ITEM_FIELDS.items():
        assert core.SESSION_FIELDS[f] == d and getattr(rig.sess, f) == d
    assert core.SESSION_FIELDS[core.initial_knowledge.FIELD] == {}
    s = rig.sess
    s.resources = {"테스터": {"붕대": 2, "항생제 두 알": 1}}
    s.item_instances = {"k": {"kind": "열쇠", "owner": "테스터"}}
    s.place_items = {"d": {"place": "광장", "key": "돌", "quantity": 1}}
    s.initial_knowledge_progress = {"version": 1, "players": {"1": {"PROFILE": {"state": "SKIPPED"}}}}
    await core.io.save_session_data_strict(rig.bot, s)
    data = json.loads(pathlib.Path(f"sessions/{s.session_id}/data.json").read_text(encoding="utf-8"))
    for f in ("resources", "item_instances", "place_items", "initial_knowledge_progress"):
        assert data[f] == getattr(s, f)
    # 옛 저장 파일(필드 없음)은 기본값으로 — 서로 지우지 않는다
    old = {k: v for k, v in data.items() if k not in IT.ITEM_FIELDS and k != core.initial_knowledge.FIELD}
    mig = core.migrate_session_data(copy.deepcopy(old))
    assert mig.get("resources") == data["resources"]


def test_F05_unauthored_scenarios_revision_unchanged():
    expect = {"무협": None}
    for f in sorted((ROOT / "scenarios").glob("*.json")):
        if f.name.endswith(".quests.json") or f.stem == "영도":
            continue
        raw = json.loads(f.read_text(encoding="utf-8"))
        res = core.definition_compiler.compile_scenario(raw, f.stem)
        if res.definition is not None:
            assert "item_definitions" not in res.definition and "carry_limits" not in res.definition
    assert expect


async def test_F06_legacy_item_changes_path_unchanged(rig):
    s = rig.sess
    assert not core.player_world.session_world_context(s).dynamic
    src = (ROOT / "cogs/gm.py").read_text(encoding="utf-8")
    # 아이템 계획은 DYNAMIC 관찰 처리(_process_world_observation)에서만 만들어진다
    body = src[src.index("def _process_world_observation"):src.index("def _implied_discovery_take")]
    assert "_build_item_plan" in body and src.count("._build_item_plan(session") == 1


def test_F07_prompt_extraction_rule_and_director_block():
    assert "item_ops" in prompts.EXTRACTION_SYSTEM_INSTRUCTION
    s = S({"P": {"붕대": 1}})
    assert "정본 소지" in IT.item_block(s, pc_name="P")


def test_F08_no_new_provider_callsite_for_items():
    src = (ROOT / "core/items.py").read_text(encoding="utf-8")
    assert "generate_content" not in src and "genai" not in src


def test_F09_operator_or_profile_strings_normalize_safely():
    s = S({"P": {"항생제 두 알": 1, "항생제(은닉)": 1}})
    out = IT.genesis_item_state({}, s)
    assert out["resources"]["P"] == {"항생제[알]": 2, "항생제(은닉)": 1}


# ── E01·E03 계획 통합: 접근 확인의 실제 지급 → PAY 사실 → 다음 턴 사건 조건 ─────────────

def _paid_raw(templates=None):
    from tests.policy.test_h05a_intent_planning import raw_with
    raw = raw_with(templates)
    rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
    rule["actor_overrides"].append({"requires_capability": "통행료", "result": "ACTION_REQUIRED",
                                    "action_policy": "CONFIRM", "action_delay_minutes": 1,
                                    "payment": {"quantity": 1}})
    return raw


def test_E03_access_confirm_payment_is_planned_and_paid_by_code():
    from tests.policy.test_h05a_intent_planning import K, PLAYER_UID, go, make_session, manifest, plan as wplan
    seen = {"door_1": {"observed_result": "ACTION_REQUIRED", "observed_state": "LOCKED",
                       "action_policy": "CONFIRM", "confirmed_at_minute": K, "source": "OBSERVED"}}
    s, D = make_session(_paid_raw(), at="시장", edges=("e_mc#fwd",), access=seen, items={"통행료": 1})
    opts = core.player_world.access_confirm_options(D, s, core.player_world.session_world_context(s).lead_pc,
                                                    core.player_world.pc_capabilities(s, PLAYER_UID, D))
    assert len(opts) == 1 and "실제로 건넨다" in opts[0]["label"]
    m = manifest(s, access_options=opts)
    p, _, _ = wplan(s, dict(go(m, "성당"), access_confirm="K1"), access_options=opts)
    assert [(x["capability"], x["quantity"]) for x in p.item_payments] == [("통행료", 1)]
    op = IT.payment_op(s, pc_name="주인공", capability="통행료", quantity=1)
    pp = IT.build_plan(s, IT.build_manifest(s, pc_name="주인공"), [], pc_name="주인공", transaction_id="t",
                       payments=[op])
    IT.apply_plan(s, pp)
    assert s.resources["주인공"] == {} and s.item_facts["paid"] == ["통행료"]
    # 보여 주기만 하는 사건 조건(HAS)과 실제 지급(PAID)은 다른 ref — 아이템 코드는 문 상태를 바꾸지 않는다
    assert "access_control_state" not in json.dumps(pp.ops, ensure_ascii=False)


def test_E01_paid_fact_spawns_event_only_on_next_turn_plan():
    from tests.policy.test_h04b_events_progression import eq as _eq
    from tests.policy.test_h05a_intent_planning import (PLAYER_UID, SEED, make_session)
    from core import action_intent as AI
    tpl = {"version": 1, "spawn_when": _eq({"ref": "OBSERVER_PAID_ITEM", "item": "통행료"}, True),
           "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}
    s, D = make_session(_paid_raw({"paid_entry": tpl}), at="시장", edges=("e_mc#fwd",), items={"통행료": 1})
    c = core.player_world.session_world_context(s)
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 1,
                                   "activity": "WAIT"}, world=s)

    def spawned(facts):
        p = core.player_world.plan_player_turn(c, s, intent, transaction_id="t", narrative_seed=SEED,
                                               capabilities=core.player_world.pc_capabilities(s, PLAYER_UID, D),
                                               item_facts=facts)
        return any("paid_entry" in json.dumps(o.payload, ensure_ascii=False) for o in p.ops)
    assert not spawned({"used": [], "paid": []})               # 보유(HAS)만으로는 PAID 아님
    assert not spawned({"used": ["통행료"], "paid": []})        # 사용 ≠ 지급
    assert spawned({"tx": "prev", "used": [], "paid": ["통행료"]})


async def test_E03_turn_commits_payment_through_gm_wiring(drig):
    from tests.policy.test_h04b_events_progression import K
    r = drig

    def patch(raw):
        rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
        rule["actor_overrides"].append({"requires_capability": "통행료", "result": "ACTION_REQUIRED",
                                        "action_policy": "CONFIRM", "action_delay_minutes": 1,
                                        "payment": {"quantity": 1}})
    r.make(edges=("e_mc#fwd",), at="시장", raw_patch=patch)
    r.sess.spatial_knowledge[PC]["access"] = {"door_1": {
        "observed_result": "ACTION_REQUIRED", "observed_state": "LOCKED", "action_policy": "CONFIRM",
        "confirmed_at_minute": K, "source": "OBSERVED"}}
    r.sess.resources = {"테스터": {"통행료": 2}}
    r.prov.routes["judgment"] = judgment("성당", access_confirm="K1")
    r.prov.routes["extraction"] = [extraction({})]
    tx = await turn(r, "통행료를 내고 성당으로 들어간다")
    assert tx.status == tt.TurnStatus.COMMITTED, (tx.status, getattr(tx, "failure_code", None))
    assert core.spatial.placement_of(r.sess, PC) == core.spatial.at_place("성당")
    assert r.sess.resources["테스터"] == {"통행료": 1} and r.sess.item_facts["paid"] == ["통행료"]
    assert "코드가 이미 처리한 지급" in r.prov.prompts["extraction"][-1][0]


async def test_F02_strict_save_failure_rolls_back_items_with_world(drig, monkeypatch):
    """커밋 임계구역(적용 + strict 저장)이 실패하면 아이템 필드도 정본 세계·재고와 함께 롤백된다(부분 반영 없음)."""
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 2}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "DROP", "owner": "O1", "item": "H1", "quantity": 1}]})]
    before = {f: copy.deepcopy(getattr(r.sess, f)) for f in ("resources", "world_version", *IT.ITEM_FIELDS)}
    real = core.io.write_session_strict_locked
    state = {"n": 0}

    async def boom(session):
        state["n"] += 1
        raise OSError("disk full")
    monkeypatch.setattr(core.io, "write_session_strict_locked", boom)
    tx = await turn(r, "붕대 하나를 내려놓는다")
    monkeypatch.setattr(core.io, "write_session_strict_locked", real)
    assert state["n"] >= 1 and tx.status != tt.TurnStatus.COMMITTED
    assert {f: getattr(r.sess, f) for f in before} == before
    assert "item_instances" in core.commit_coordinator.COMMIT_OWNED_FIELDS
