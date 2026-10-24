# -*- coding: utf-8 -*-
"""v7.35 테스트 세션 f4574fbe 결함 수정(사용자 승인: 인용 없는 공개 인정 · 엑스트라 공용 카드 · 계절 계산 ·
D1 '나'(Claude 결정 — 처음 나온 물건 NEW 획득) · 이동 중 행동 시간 · 예상 보고 흡수 · 나머지 표대로).
"""
from __future__ import annotations

import json
import os
import types

import pytest

import core
from core import items as IT
from core import master_report as MR
from core import speaker_card as SC
from core import spatial as SP
from core import turn_transaction as tt
from tests.conftest import REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import PC, drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_h06_r2_transit import ROUTE_B, _go, _spawn, midway_tpl
from tests.policy.test_h04b_events_progression import K
from tests.policy.test_svg_speaker_card import AID, _config, _known, _world, card_session  # noqa: F401
from tests.policy.test_v717_items import S, man
from tests.policy.test_v734_prompt_assembly import ydyn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy

NAME_MAN = {"R1": (AID, "name", "i.name")}
DEF = {"actor_definitions": {AID: {"display_name": "김노인"}}}


# ── U1: 이름 공개 — 문단 경계를 넘는 인용 · 대사 속 이름 ─────────────────────

def test_reveal_quote_spanning_paragraphs_is_found_at_its_start_paragraph():
    paras = ["앞 문단.", "@대사:김노인|...김노인.", "이름을 내뱉는 음성은 건조하다."]
    q = [{"key": "R1", "quote": "@대사:김노인|...김노인. 이름을 내뱉는 음성은"}]          # 세션 4턴 실측 형태
    assert SC.parse_reveals(NAME_MAN, q, paras, DEF, None) == {"i.name": 1}
    bad = [{"key": "R1", "quote": "@대사:김노인|...김노인. 본문에 없는 말"}]
    assert SC.parse_reveals(NAME_MAN, bad, paras, DEF, None) == {}                      # 보수성 유지


def test_name_in_dialogue_body_counts_without_extraction_quote():
    paras = ["김노인이 고개를 든다.", "@대사:경비|김노인, 복귀냐?"]
    assert SC.implied_name_reveals(NAME_MAN, paras, DEF, None) == {"i.name": 1}         # 타인의 호명(세션 11턴)
    assert SC.implied_name_reveals(NAME_MAN, paras[:1], DEF, None) == {}                # 서술의 이름은 근거가 아니다
    assert SC.implied_name_reveals({"F1": (AID, "face", "i.face")}, paras, DEF, None) == {}


def test_identity_candidates_skip_already_known(card_session, monkeypatch):
    world = _world(_config())
    monkeypatch.setattr(SC, "scene_card_actors", lambda *_a: [AID])
    assert SC.identity_manifest(world, None, card_session, "face") == {"F1": AID}
    _known(card_session, "i.face")
    assert SC.identity_manifest(world, None, card_session, "face") == {}                 # D4
    assert SC.identity_manifest(world, None, card_session, "name") == {"N1": AID}


# ── U2: 엑스트라 공용 카드 · 마커 이름 규칙 ───────────────────────────────

async def test_unresolved_speaker_gets_all_hidden_common_card(card_session, monkeypatch):
    cfg = dict(_config(), extra_speaker=True)
    monkeypatch.setattr(SC, "card_world", lambda _s: _world(cfg))
    ctx = SC.display_context(card_session, "경비")
    assert (ctx.status, ctx.header, ctx.card) == (SC.SPEAKER_UNKNOWN, "???", SC.EXTRA_CARD)
    slots = SC.evaluate_slots(cfg, ctx.card, frozenset({"i.face", "i.name"}), None)
    assert slots == {k: v["unknown"] for k, v in cfg["slots"].items()}                  # 모든 슬롯 미공개
    assert await SC.prepare_card(card_session, ctx) is not None
    card_session.actor_registry = {"pc_x": {"display_name": "테스터", "kind": "PC"}}
    assert SC.display_context(card_session, "테스터").card is None                      # PC 이름 화자는 카드 없음


def test_extra_speaker_validation_and_yeongdo_authoring():
    with open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8") as f:
        y = json.load(f)
    assert y["speaker_card"]["extra_speaker"] is True
    y["speaker_card"]["extra_speaker"] = "yes"
    codes = {row[1] for row in SC.check_card_definition(y, scenario_id="영도")}
    assert "CARD_EXTRA_SPEAKER" in codes


async def test_scene_block_tells_marker_name_rule_and_season(ydyn):
    r = ydyn
    await r.gm._process_actions(r.sess, "주변을 둘러본다", r.master)
    narr = r.prov.prompts["narration"][0][0]
    assert core.turn_world.MARKER_NAME_RULE in narr
    assert "· 계절: 가을(10월 초순)" in narr                                             # 영도 시작 2016-10-02
    assert "본문 서술·대사 내용에" in narr


def test_season_text_from_month():
    assert core.turn_world.season_text(10, 2) == "가을(10월 초순)"
    assert core.turn_world.season_text(1, 25) == "겨울(1월 하순)"
    assert core.turn_world.season_text(7, 15) == "여름(7월 중순)"


# ── D1: 처음 나온 물건 획득(NEW) ────────────────────────────────────

def test_new_item_acquired_only_when_named_in_narration():
    s = S({"P": {"칼": 1}})
    m = man(s)
    ops = [{"op": "ACQUIRE", "owner": "O1", "item": IT.NEW_KEY, "name": "은박 담요", "quantity": 1},
           {"op": "ACQUIRE", "owner": "O1", "item": IT.NEW_KEY, "name": "멀티툴", "quantity": 1}]
    p = IT.build_plan(s, m, ops, pc_name="P", transaction_id="t", narration_text="은박 담요와 녹슨 멀티 툴을 챙긴다")
    IT.apply_plan(s, p)
    assert s.resources["P"] == {"칼": 1, "은박 담요": 1, "멀티툴": 1}
    for bad, why in (({"name": "권총"}, "본문에 없음"), ({"name": "은박 담요", "quantity": 11}, "상한")):
        with pytest.raises(IT.ItemContractError, match=why):
            IT.build_plan(S({"P": {}}), man(S({"P": {}})), [{**ops[0], **bad}], pc_name="P", transaction_id="t",
                          narration_text="은박 담요")
    s2 = S({"P": {}, "N": {}})
    with pytest.raises(IT.ItemContractError, match="PC만"):
        IT.build_plan(s2, man(s2, npcs=("N",)), [{**ops[0], "owner": "O2"}], pc_name="P", transaction_id="t",
                      narration_text="은박 담요")


async def test_new_item_reaches_canon_through_turn_commit(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {}}
    r.prov.routes["judgment"] = judgment()
    from tests.fakes.genai_fakes import FakeGenAIResponse
    from tests.policy.test_ready_barrier import _usage
    r.prov.routes["narration"] = [FakeGenAIResponse("바위 틈의 가방에서 은박 담요를 꺼내 배낭에 넣는다.", usage=_usage())]
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "ACQUIRE", "owner": "O1", "item": "NEW", "name": "은박 담요", "quantity": 1}]})]
    tx = await turn(r, "가방을 뒤진다")
    assert tx.status == tt.TurnStatus.COMMITTED and r.sess.resources["테스터"] == {"은박 담요": 1}


# ── D2: 이동 중(TRANSIT) 행동 시간 ─────────────────────────────────

async def test_action_time_flows_inside_transit_up_to_edge_end(drig):
    r = drig
    D = r.make(templates={"dog": midway_tpl()}, edges=ROUTE_B)
    _spawn(r, D)
    await _go(r, "산복도로")                                     # 8분 구간의 3분째에 멈춘 TRANSIT
    assert r.sess.world_clock["minute_index"] == K + 3
    r.prov.routes["judgment"] = judgment(None, action_minutes=2)
    await turn(r, "칼을 고쳐 쥔다")
    assert r.sess.world_clock["minute_index"] == K + 5 and SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT
    r.prov.routes["judgment"] = judgment(None, action_minutes=30)
    await turn(r, "한참 살핀다")
    assert r.sess.world_clock["minute_index"] == K + 7                         # 구간 끝(K+8) 직전까지만
    assert SP.placement_of(r.sess, PC)["kind"] == SP.TRANSIT


# ── 보고 체계 ──────────────────────────────────────────────────────

async def test_master_channel_drops_absorbed_messages_and_reports_compactly(drig):
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"붕대": 2}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "CONSUME", "owner": "O1", "item": "H1", "quantity": 1}]})]
    await turn(r, "붕대를 감는다")
    texts = [m.content or "" for m in r.master.sent]
    for gone in ("💰 **[예상]**", "[GM PROCEED]", "[GM 판단 #", "🔎 **[추출층위]**", "[동행]"):
        assert not any(gone in t for t in texts), gone
    fields = {f.name: f.value for m in r.master.sent for e in (m.embeds or []) for f in e.fields}
    assert "🧭 판단" in fields and "**PROCEED**" in fields["🧭 판단"]
    assert fields["🎬 지시"].startswith("지시문 ") and "물건 연산 1" in fields["🔎 추출"]
    assert "🔎 추출층위(원문)" not in fields and "{" not in fields["🧭 판단"]          # 원문 JSON 없음
    assert "붕대 −1 (→1)" in fields["🎒 물건"]
    assert "🌍 세계 계획" in fields


def test_item_change_lines_cover_place_and_special_items():
    pre = {"place_items": {"a": {"place": "항구", "key": "상자", "quantity": 1, "closed": True}},
           "item_instances": {}}
    post = {"place_items": {"a": {"place": "항구", "key": "상자", "quantity": 1, "closed": False}},
            "item_instances": {"i1": {"name": "무전기", "owner": "테스터", "condition": "고장"}}}
    lines = MR.item_change_lines(pre, post)
    assert "현장(항구) 상자 ×1 · 열림" in lines and any(l.startswith("특수 물건 무전기") for l in lines)


def test_tracked_sections_summarize_canon():
    s = types.SimpleNamespace(
        definition_revision=None, world_clock={"minute_index": 30, "start_minute_index": 10}, world_schedule={"x": {}},
        spatial_state={"placements": {"pc_1": {"kind": "AT_PLACE", "place_id": "항구"}}},
        actor_registry={"pc_1": {"display_name": "테스터"}}, visited_places=["항구"], spatial_knowledge={},
        event_instances={"e": {"lifecycle": "ACTIVE", "template_id": "t", "current_phase": "p"}},
        cognition_state={"actor:pc_1": {"i": {}}}, information_state={}, resources={"테스터": {"칼": 1}},
        players={"1": {"name": "테스터"}}, place_items={}, item_instances={}, statuses={}, companions=[],
        met_npcs=[], relationship_state={}, access_control_state={}, world_pending_signals=[])
    text = "\n".join(l for _n, ls in MR.tracked_sections(s) for l in ls)
    assert "시작 +20분" in text and "테스터=항구" in text and "t(p)" in text and "테스터: 칼×1" in text


def test_turn_summary_counts_world_ops_by_type():
    batch = types.SimpleNamespace(_ops=[{"op_type": "CLOCK_ADVANCE"}, {"op_type": "CLOCK_ADVANCE"}])
    prep = types.SimpleNamespace(world_mutation_batch=batch)
    assert core.turn_log._prep(prep)["world_ops"] == {"CLOCK_ADVANCE": 2}
