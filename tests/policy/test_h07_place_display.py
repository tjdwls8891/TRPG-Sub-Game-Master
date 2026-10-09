# -*- coding: utf-8 -*-
"""H07 §5 — 내부 PlaceId ≠ 플레이어 표시명(T-9). 이름이 스스로 드러나지 않는 장소(unknown_name)는 출구 학습·
도착만으로 이름을 알지 못하고, 명시 발견(정보 reveals)으로만 known display로 승격된다.
"""
from __future__ import annotations

import pytest

from core import action_intent as AI
from core import definition_compiler as dc
from core import place_display as PD
from core import player_world as PW
from core import spatial as SP
from core import turn_world as TW
from core import world_mutation as WM
from tests.policy.test_h04b_events_progression import ev_raw
from tests.policy.test_h05a_intent_planning import PC, ctx_of, go, make_session, manifest, plan

pytestmark = pytest.mark.policy

HIDDEN = "종탑이 보이는 언덕 위 건물"


def _raw():
    raw = ev_raw()
    raw["places"]["성당"]["unknown_name"] = HIDDEN
    raw["places"]["시장"]["display_name"] = "항구 시장"
    raw["information_definitions"]["S40"]["reveals"] = {"places": ["성당"], "edges": ["e_secret"]}
    return raw


def _defn(raw=None):
    res = dc.compile_scenario(raw or _raw(), "t")
    assert res.ok, [(d.code, d.message) for d in res.errors]
    return res.definition


def _sess(**kw):
    kw.setdefault("places", ("항구",))
    kw.setdefault("edges", ())
    return make_session(_raw(), **kw)


def _apply(s_or_w, ops):
    src = s_or_w if isinstance(s_or_w, WM.ProjectedWorld) else WM.ProjectedWorld.capture(s_or_w)
    b = WM.WorldMutationBatch(batch_id="b", outer_origin="ADMIN_EDIT", source_world_version_id=None,
                              definition_revision=None)
    for o in ops:
        b.add(o)
    b.freeze()
    r = WM.project(src, b, WM.PRODUCTION_REGISTRY)
    assert r.ok, [c.message for c in r.conflicts]
    return r.world


# ── 컴파일 ─────────────────────────────────────────────────────

def test_display_fields_compile_only_when_authored_and_reject_leaks():
    D = _defn()
    assert D["place_definitions"]["성당"]["unknown_name"] == HIDDEN
    assert D["place_definitions"]["시장"]["display_name"] == "항구 시장"
    assert "unknown_name" not in D["place_definitions"]["항구"]
    assert D["information_definitions"]["S40"]["reveals"] == {"places": ["성당"],
                                                              "edges": ["e_secret#fwd"]}
    plain = dc.compile_scenario(ev_raw(), "t")
    assert "display_name" not in plain.definition["place_definitions"]["시장"]
    raw = _raw()
    raw["places"]["성당"]["unknown_name"] = "성당"
    assert "PLACE_UNKNOWN_NAME_LEAKS" in [d.code for d in dc.compile_scenario(raw, "t").errors]
    raw = _raw()
    raw["information_definitions"]["S40"]["reveals"] = {"places": ["없는곳"]}
    assert "INFO_REVEALS_UNKNOWN" in [d.code for d in dc.compile_scenario(raw, "t").errors]


# ── 해석 규칙 ─────────────────────────────────────────────────

def test_display_name_rules():
    D = _defn()
    k0 = {"places": {}}
    assert PD.display_name(D, k0, "성당") is None                         # 존재조차 모름 — 이름 없음
    seen = {"places": {"성당": {"knows_name": False}, "시장": {"knows_name": False}}}
    assert PD.display_name(D, seen, "성당") == HIDDEN                    # 보았지만 이름 모름
    assert PD.display_name(D, seen, "시장") == "항구 시장"                # 스스로 드러나는 이름(저작 표시명)
    named = {"places": {"성당": {"knows_name": True}}}
    assert PD.display_name(D, named, "성당") == "성당"                   # 명시 발견 뒤 실제 이름


def test_local_exit_view_does_not_reveal_hidden_name():
    s, D = _sess(places=("시장",), at="시장")
    w = _apply(s, PW.local_view_ops("lv", D, s, [PC], "시장"))
    k = SP.actor_knowledge(w, PC)
    assert k["places"]["성당"]["knows_name"] is False                    # 출구는 보이지만 이름은 모른다
    assert PD.display_for(D, w, PC, "성당") == HIDDEN
    w2 = _apply(s, PW.local_view_ops("lv", D, s, [PC], "항구"))
    k2 = SP.actor_knowledge(w2, PC)
    assert k2["places"]["시장"]["knows_name"] is True                    # 스스로 드러나는 이름
    assert "e_secret#fwd" not in k2["edges"] and "성당" not in k2["places"]   # SECRET 출구는 보이지 않는다


# ── 플레이어 노출 표면 ─────────────────────────────────────────

def test_manifest_route_and_overlay_use_display_names():
    s, D = _sess(places=("시장",), at="시장")
    w = _apply(s, PW.local_view_ops("lv", D, s, [PC], "시장"))
    s.spatial_knowledge = w.domain("spatial_knowledge")
    m = manifest(s)
    labels = {pid: lbl for _, pid, lbl in m.places}
    assert labels["성당"] == HIDDEN and labels["시장"] == "항구 시장"
    assert "성당" not in m.render() and HIDDEN in m.render() and "항구 시장" in m.render()
    s3, _ = _sess(places=("항구", "성당"))
    s3.spatial_knowledge[PC]["places"]["성당"]["knows_name"] = False
    p, _, _ = plan(s3, go(manifest(s3), "성당"))                         # 길은 모름 — 일반 그래프 후보
    assert p.status == PW.PLAN_NEEDS_CHOICE
    for o in p.route_options:
        assert "성당" not in o["label"] and "e_" not in o["label"]
    s2, _ = _sess(places=("항구", "산복도로"), edges=("e_hh#fwd",))
    p2, _, _ = plan(s2, go(manifest(s2), "산복도로"))
    over = TW.narration_overlay(p2, D, ctx_of(s2))
    assert "산복도로에 도착했다" in over


def test_hidden_place_arrival_keeps_unknown_display_until_discovery():
    s, D = _sess(places=("항구", "산복도로", "성당"), edges=("e_hh#fwd", "e_hc#fwd"))
    s.spatial_knowledge[PC]["places"]["성당"]["knows_name"] = False
    p, _, _ = plan(s, go(manifest(s), "성당"))
    assert p.status == PW.PLAN_PLANNED and p.outcome["final_place"] == "성당"
    over = TW.narration_overlay(p, D, ctx_of(s))
    assert f"{HIDDEN}에 도착했다" in over and "성당에 도착" not in over
    tl = TW.mirror_timeline({}, D, p.projected_world, PC)
    assert tl["current_location"] == "성당" and tl["current_location_display"] == HIDDEN
    from core.timeline import format_timeline
    assert HIDDEN in format_timeline(tl) and "성당" not in format_timeline(tl)


def test_information_reveals_promote_known_display_and_secret_edge():
    s, D = _sess(places=("항구", "성당"))
    s.spatial_knowledge[PC]["places"]["성당"]["knows_name"] = False
    assert PD.display_for(D, s, PC, "성당") == HIDDEN
    ops = TW.reveal_ops(D, PC, ["S40", "S40"], prefix="rv", at_minute=5)
    assert [o.op_type for o in ops] == [SP.OP_PLACE_KNOWLEDGE_UPSERT, SP.OP_EDGE_KNOWLEDGE_UPSERT]
    w = _apply(s, ops)
    assert PD.display_for(D, w, PC, "성당") == "성당"
    k = SP.actor_knowledge(w, PC)
    assert SP.knows_edge(k, "e_secret#fwd") and k["edges"]["e_secret#fwd"]["acquisition_sources"] == ["INFO:S40"]
    assert TW.reveal_ops(D, PC, ["nothing"], prefix="x") == []


def test_dynamic_place_block_hides_internal_name_from_player_wording():
    s, D = _sess(places=("항구", "성당"), at="성당")
    s.spatial_knowledge[PC]["places"]["성당"]["knows_name"] = False
    s.spatial_state["scene_place"] = "성당"
    c = ctx_of(s)
    blk = TW.dynamic_place_block(c, None, s)
    first = blk.strip().splitlines()[1]
    assert first == HIDDEN and "내부 이름: 성당" in blk                  # GM(지시층위)만 내부 이름을 본다
    s.spatial_knowledge[PC]["places"]["성당"]["knows_name"] = True
    assert TW.dynamic_place_block(c, None, s).strip().splitlines()[1] == "성당"
