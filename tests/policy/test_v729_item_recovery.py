"""v7.29 세션 cf916fc5 사고 — 닫힌 용기·물건 연산 거부 복구·추출 실패 분류.

재현: 영도 '돌아오도록 묶은 것'에서 상자를 연 묘사 → 추출이 바닥 물건 키로 OPEN → ItemContractError가 추출 작업 밖으로
새어 EXTRACTION_PROVIDER_FAILURE로 표기되고, 같은 응답을 같은 검증에 다시 넣는 재시도만 남았다.
"""
from __future__ import annotations

import copy
import types

import pytest

import core
from core import definition_compiler as dc
from core import items as IT
from core import turn_transaction as tt
from tests.policy.test_h05bc_turn_wiring import drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_h06a_lifecycle import START, dyn_raw
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v717_items import key_of

pytestmark = pytest.mark.policy

BOX = {"key": "파란 상자", "quantity": 1, "place": "항구",
       "contains": [{"key": "밧줄[타래]", "quantity": 1}, {"key": "낚싯바늘〈손상〉", "quantity": 5}]}


def _sess(items):
    s = types.SimpleNamespace(resources={"P": {}}, item_instances={}, place_items={})
    out = IT.genesis_item_state({}, s, items, source_prefix="start.f")
    s.resources, s.place_items = out["resources"], out["place_items"]
    return s


def _plan(s, ops):
    m = IT.build_manifest(s, pc_name="P", scene_place="항구")
    return m, (lambda raw: IT.build_plan(s, m, raw(m), pc_name="P", transaction_id="t"))


# ── 컴파일 ───────────────────────────────────────────────────

def test_contains_compiles_only_on_single_place_item():
    ok = dc.compile_scenario(dyn_raw(world_start=dict(START, items=[BOX])), "t")
    assert ok.ok and ok.definition["initialization_definitions"]["start_frames"][0]["world_start"]["items"] == [BOX]
    for bad in (dict(BOX, quantity=2), {"key": "x", "quantity": 1, "owner": "강지수", "contains": BOX["contains"]},
                dict(BOX, contains=[]), dict(BOX, contains=[{"key": "x"}])):
        codes = [d.code for d in dc.compile_scenario(dyn_raw(world_start=dict(START, items=[bad])), "t").errors]
        assert "WORLD_START_ITEMS_INVALID" in codes, bad


# ── 닫힌 용기 ─────────────────────────────────────────────────

def test_closed_box_hides_contents_until_opened_and_opening_is_not_taking():
    s = _sess([BOX])
    m, plan = _plan(s, None)
    labels = {r[3]: r[5] for r in m.places}
    assert "닫힌 용기" in labels["파란 상자"] and "열기 전에는 꺼낼 수 없음" in labels["밧줄[타래]"]
    block = IT.item_block(s, pc_name="P", scene_place="항구")
    assert "닫힌 용기" in block and "서술하지 말 것" in block                  # GM은 알되 열기 전에는 서술 금지
    with pytest.raises(IT.ItemContractError, match="열기"):
        plan(lambda m: [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, "밧줄"), "quantity": 1}])
    IT.apply_plan(s, plan(lambda m: [{"op": "OPEN", "owner": "O1", "item": key_of(m, "파란 상자"), "quantity": 1}]))
    assert s.resources["P"] == {}                                     # 열기 ≠ 획득
    assert s.place_items["start.f.0"]["closed"] is False
    m2, plan2 = _plan(s, None)
    assert "열기 전에는" not in {r[3]: r[5] for r in m2.places}["밧줄[타래]"]
    with pytest.raises(IT.ItemContractError, match="닫힌 용기가 아님"):     # 두 번 열 수 없다
        plan2(lambda m: [{"op": "OPEN", "owner": "O1", "item": key_of(m, "파란 상자"), "quantity": 1}])
    IT.apply_plan(s, plan2(lambda m: [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, "밧줄"), "quantity": 1}]))
    assert s.resources["P"] == {"밧줄[타래]": 1}


def test_open_on_non_container_place_item_is_rejected_not_misapplied():
    s = _sess([{"key": "작은 배", "quantity": 1, "place": "항구"}, BOX])
    _m, plan = _plan(s, None)
    with pytest.raises(IT.ItemContractError, match="닫힌 용기가 아님"):     # cf916fc5: OPEN L1(작은 배)
        plan(lambda m: [{"op": "OPEN", "owner": "O1", "item": key_of(m, "작은 배"), "quantity": 1}])


def test_taking_the_closed_box_carries_its_contents():
    s = _sess([BOX])
    _m, plan = _plan(s, None)
    IT.apply_plan(s, plan(lambda m: [{"op": "ACQUIRE", "owner": "O1", "item": key_of(m, "파란 상자"), "quantity": 1}]))
    assert s.resources["P"] == {"파란 상자": 1, "밧줄[타래]": 1, "낚싯바늘〈손상〉": 5} and s.place_items == {}


# ── 추출 경로: 거부 복구 · 실패 분류 · 운영자 문구 ─────────────────

async def test_rejected_item_ops_trigger_one_corrected_reextraction_then_drop(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 1}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [{"op": "OPEN", "owner": "O1", "item": "H1", "quantity": 1}]})]
    before = copy.deepcopy(r.sess.resources)
    tx = await turn(r, "붕대 포장을 연다")
    assert r.prov.calls.count("extraction") == 2
    assert tx.preparation.phase != core.turn_preparation.PREP_RETRY_PENDING
    assert r.sess.resources == before and not getattr(r.sess, "extraction_pending", False)
    assert any("물건 변화 없이 확정" in (m.content or "") for m in r.master.sent)


async def test_provider_failure_keeps_provider_code(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [RuntimeError("provider down")]
    tx = await turn(r, "숨을 고른다")
    assert tx.failure_code == tt.FailureCode.EXTRACTION_PROVIDER_FAILURE.value


async def test_processing_failure_gets_invalid_result_code_and_logged_error(drig, monkeypatch):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment()

    def _boom(*a, **k):
        raise ValueError("관찰 처리 버그")
    monkeypatch.setattr(r.gm, "_process_world_observation", _boom)
    events = []
    orig = core.logs.event
    monkeypatch.setattr(core.logs, "event", lambda *a, **k: (events.append((a, k)), orig(*a, **k))[1])
    tx = await turn(r, "숨을 고른다")
    assert tx.failure_code == tt.FailureCode.EXTRACTION_INVALID_RESULT.value
    assert any((k.get("data") or {}).get("error", "").startswith("ValueError: 관찰 처리 버그") for _a, k in events)


async def test_master_message_says_delivered_not_completed_for_auto_turn(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment()
    await turn(r, "숨을 고른다")
    texts = [m.content or "" for m in r.master.sent]
    assert any("묘사 전달 완료" in t for t in texts) and not any("묘사 연출 완료" in t for t in texts)
