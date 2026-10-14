# -*- coding: utf-8 -*-
"""H06-E — DYNAMIC 재서술(같은 턴 재생성) · 되감기.

  · 재서술은 턴 이전 세계로 복원한 뒤 보존된 의미 선택으로 H05 세계 계획을 다시 세운다(world_turn=None 커밋 금지).
  · 경로 선택은 transient 키 없이 의미 기록(구간 서명) 그대로 — 재질문 없음.
  · 그 턴의 definition_revision이 현재 바인딩과 다르면 상태 변경 없이 거부.
  · 되감기는 가역 세계 전부(시계·배치·사건·예약·정보·인지·관계·world_version)를 정확히 복원하고,
    narrative_seed·정의 바인딩·생애주기·재무 사실은 바꾸지 않는다.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

import core
from core import player_world as PW
from core import spatial as SP
from core import turn_history as TH
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, drig, extraction, judgment, rig, turn, world_of)

pytestmark = pytest.mark.policy


def _intent_rec(s):
    head = TH.view(s.session_id).head_entry()
    return head, TH.load_record(s.session_id, head["transaction_id"])


async def test_dynamic_rerender_recomputes_world_plan_from_preserved_choice(drig):
    r = drig
    r.make(edges=("e_hm#fwd",))
    r.prov.routes["judgment"] = judgment("시장")
    tx1 = await turn(r, "시장으로 간다")
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")
    head, rec = _intent_rec(r.sess)
    ai = rec["judgment"]["judgment"][core.action_intent.INTENT_FIELD]
    assert ai["definition_revision"] == r.sess.definition_revision
    assert ai["semantic"]["movement"]["destination_place"] == "시장"
    v1 = copy.deepcopy(r.sess.world_version)
    n_judg = r.prov.calls.count("judgment")
    started, reason = await r.gm.rerender_latest(r.sess)
    assert started, reason
    assert r.prov.calls.count("judgment") == n_judg                   # 판단 재호출 없음
    assert "PC는 시장에 도착했다" in r.prov.prompts["logic"][-1][0]     # 지시층위가 재계산된 계획을 본다
    head2, rec2 = _intent_rec(r.sess)
    assert head2["attempt"] == head["attempt"] + 1 and head2["supersedes"] == tx1.transaction_id
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")          # 턴 이전(항구)에서 다시 이동해 커밋
    assert r.sess.world_version != v1 and r.sess.world_version["parent_version_id"] == v1["parent_version_id"]


async def test_rerender_preserves_semantic_route_choice_without_reasking(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd", "e_hh#fwd", "e_hc#fwd"))
    r.prov.routes["judgment"] = judgment("성당")
    await turn(r, "성당으로 간다")                                    # 의미 있는 경로 차이 → 질문

    def pick(kwargs):
        text = H05Provider._text(kwargs)
        m = re.search(r"(R\d+)=산복도로", text)
        return judgment("성당", route_choice=m.group(1) if m else "NONE")(kwargs)
    r.prov.routes["judgment"] = pick
    await turn(r, "산복도로 쪽으로")
    assert SP.placement_of(r.sess, PC) == SP.at_place("성당")
    _, rec = _intent_rec(r.sess)
    sig = rec["judgment"]["judgment"][core.action_intent.INTENT_FIELD]["semantic"]["route_signature"]
    assert sig and any("e_hh" in e for e in sig)
    asked = sum("1) " in (m.content or "") for m in r.gch.sent)     # v7.13: 경로 선택지로 센다
    started, reason = await r.gm.rerender_latest(r.sess)
    assert started, reason
    assert asked and sum("1) " in (m.content or "") for m in r.gch.sent) == asked     # 다시 묻지 않음
    assert SP.placement_of(r.sess, PC) == SP.at_place("성당")
    known = SP.actor_knowledge(r.sess, PC)["edges"]
    assert known["e_hh#fwd"]["last_confirmed_at"] is not None          # 같은 의미 경로(산복도로)로 재이동


async def test_rerender_rejected_on_definition_revision_mismatch(drig):
    r = drig
    r.make(edges=("e_hm#fwd",))
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")
    head, rec = _intent_rec(r.sess)
    p = TH.record_path(r.sess.session_id, head["transaction_id"])
    rec["judgment"]["judgment"][core.action_intent.INTENT_FIELD]["definition_revision"] = "sha256:" + "f" * 64
    json.dump(rec, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    before = world_of(r.sess)
    n = len(r.prov.calls)
    started, reason = await r.gm.rerender_latest(r.sess)
    assert not started and "revision" in reason
    assert world_of(r.sess) == before and len(r.prov.calls) == n       # 상태·provider 변화 없음
    assert TH.view(r.sess.session_id).head_entry()["transaction_id"] == head["transaction_id"]


async def test_dynamic_turn_without_semantic_record_is_not_reinterpreted(drig):
    r = drig
    r.make(edges=("e_hm#fwd",))
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")
    head, rec = _intent_rec(r.sess)
    p = TH.record_path(r.sess.session_id, head["transaction_id"])
    del rec["judgment"]["judgment"][core.action_intent.INTENT_FIELD]
    json.dump(rec, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    started, reason = await r.gm.rerender_latest(r.sess)
    assert not started and "의미 기록" in reason


async def test_dynamic_rewind_restores_world_exactly_and_keeps_identity_and_finance(drig):
    r = drig
    r.make(edges=("e_hm#fwd", "e_mc#fwd"))
    r.sess.relationship_state = {}
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")
    after1 = {f: copy.deepcopy(getattr(r.sess, f)) for f in WS.GAMEPLAY_WORLD_FIELDS}
    r.prov.routes["judgment"] = judgment(None, time_kind="DURATION", duration_minutes=30, activity="WAIT")
    await turn(r, "30분 기다린다")
    assert r.sess.world_clock != after1["world_clock"]
    ident = (r.sess.narrative_seed, r.sess.definition_revision, r.sess.world_lifecycle,
             copy.deepcopy(r.sess.world_initialization))
    costs = len(r.bot.cost_ledger.list_cost_events_strict(session_id=r.sess.session_id))
    res = await TH.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert {f: getattr(r.sess, f) for f in WS.GAMEPLAY_WORLD_FIELDS} == after1     # 가역 세계 전부 정확히
    assert (r.sess.narrative_seed, r.sess.definition_revision, r.sess.world_lifecycle,
            r.sess.world_initialization) == ident                                   # 비가역 정체성 불변
    assert len(r.bot.cost_ledger.list_cost_events_strict(session_id=r.sess.session_id)) == costs
    assert PW.session_world_context(r.sess).dynamic


def test_lifecycle_and_identity_fields_are_not_reversible():
    for f in ("narrative_seed", "definition_revision", "world_lifecycle", "world_initialization"):
        assert f not in TH.REVERSIBLE_FIELDS and f in TH.NON_REVERSIBLE_IDENTITY_FIELDS
