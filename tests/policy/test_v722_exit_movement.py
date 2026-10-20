# -*- coding: utf-8 -*-
"""v7.22 DYNAMIC 이동·탐험 — 보이는 출구(E키 → 실제 구간) · 목적지 질문 문맥 · 목적지 없는 한 구간 이동.

인수 기준(이동·탐험 v1 패키지 04): M01~M26 · E01~E12 중 코드로 검증 가능한 항목.
  · 순수: 출구 후보(나가는 아는 구간만·비밀/내부 이름 비노출·전부) · 의도 해석 · 구간 강제 · 결정적 선택 · 막힘 처리
  · 배선(drig): 최초 '이동한다' 질문 1회 → '그냥' 한 구간 / 최초 '아무 데나' / 질문 뒤 다른 행동 · 지목 · 재서술 고정
"""
from __future__ import annotations

import copy
import re

import pytest

import core
from core import action_intent as AI
from core import player_world as PW
from core import spatial as SP
from core import turn_history as TH
from core import turn_transaction as tt
from core import turn_world as TW
from tests.policy.test_h05a_intent_planning import ctx_of, make_session, raw_with
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401
    PC, H05Provider, drig, extraction, judgment, rig, turn)
from tests.policy import test_h07_yeongdo_draft as YD

pytestmark = pytest.mark.policy

ALL3 = ("e_hm#fwd", "e_hh#fwd", "e_hs#fwd")          # 항구에서 나가는 보이는 길(시장·산복도로·해안)


def _stairs(raw):
    """항구→시장 평행 구간(계단) — 같은 도착지의 다른 길."""
    raw["movement_edges"].append({"id": "e_st", "from": "항구", "to": "시장", "distance_m": 800,
                                  "route_hint": "북쪽 · 가파른 오르막 · 계단"})


def _ekey(text, part):
    m = re.search(r"(E\d+)=[^,\n]*" + re.escape(part), text)
    return m.group(1) if m else None


def _sess(edges=ALL3, at="항구", raw=None, access=None):
    s, D = make_session(raw or raw_with(), at=at, edges=edges, access=access)
    return s, D, ctx_of(s)


def _plan(c, s, intent, tx="tx-a"):
    return PW.plan_player_turn(c, s, intent, transaction_id=tx, narrative_seed="seed")


def _exit(c, eid):
    e = c.definition["movement_edges"][eid]
    return AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_EXIT, e["to"], eid),
                                   definition_revision=c.definition_revision)


# ══════════════════════════════════════════════════════════════
#  출구 후보 — 단일 출처 · 지식 경계 (M02·M16·M17·E02·E10·M12)
# ══════════════════════════════════════════════════════════════

def test_exit_candidates_are_known_outgoing_edges_only_without_secrets():
    s, D, c = _sess(edges=ALL3 + ("e_hm#rev",))
    rows = AI.exit_candidates(D, s, PC)
    assert [r[0] for r in rows] == sorted(ALL3)                            # E02: 역방향(시장→항구) 없음
    assert "e_secret#fwd" not in [r[0] for r in rows]                        # M17: 미발견 SECRET 없음
    m = AI.build_manifest(D, s, PC, definition_revision=c.definition_revision)
    text = m.render()
    assert "현재 장소에서 나가는 길" in text and "#fwd" not in text and "e_hm" not in text   # E10: 내부 id 없음
    assert m.schema()["properties"]["exit"]["enum"] == ["NONE", "E1", "E2", "E3"]


def test_unknown_name_destination_is_never_shown_by_true_name():
    raw = raw_with()
    raw["places"]["해안"]["unknown_name"] = "바람 부는 쪽"
    s, D, c = _sess(raw=raw)
    s.spatial_knowledge[PC]["places"]["해안"] = {"knows_name": False, "visited": False,
                                                "rough_location": None, "acquisitions": []}
    m = AI.build_manifest(D, s, PC, definition_revision=c.definition_revision)
    labels = " ".join(r[3] for r in m.exits)
    assert "바람 부는 쪽" in labels and "해안" not in labels.replace("해안도로", "")   # M16: 참이름 비노출
    q, _asked = TW.destination_question(c, s)
    assert "바람 부는 쪽" in q and "해안 쪽" not in q


def test_yeongdo_27_exits_all_reachable_and_summarized():
    w, _ = YD.genesis()
    w = YD.setup(w, "저지대 상업지구")
    D, rev = YD._defn()
    m = AI.build_manifest(D, w, YD.PC, definition_revision=rev)
    assert len(m.exits) == 27                                                # M12: 앞 10개로 자르지 않음
    last = m.exits[-1]
    i = AI.resolve_intent(m, {"movement_kind": "EXIT", "exit": last[0], "time_kind": "NONE", "action_minutes": 0})
    assert i.movement.edge_id == last[1] and not i.unresolved
    q, asked = TW.destination_question(YD.ctx(), w)
    assert len(asked) == 27 and sum(int(n) for n in re.findall(r"(\d+)갈래", q)) == 27
    block = TW.dynamic_place_block(YD.ctx(), None, w)
    assert block.count("\n· ") >= 27                                         # 지시층위 출구 목록도 전부


def test_yeongdo_exit_moves_through_that_exact_edge():
    w, _ = YD.genesis()
    w = YD.setup(w, "저지대 상업지구")
    D, rev = YD._defn()
    eid, to = "yd.e186#fwd", D["movement_edges"]["yd.e186#fwd"]["to"]
    p = YD.turn(w, AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_EXIT, to, eid), definition_revision=rev))
    assert p.status == PW.PLAN_PLANNED and p.resolved_choice["route_signature"] == [eid]
    pl = SP.placement_of(p.projected_world, YD.PC)                         # 도착 또는 그 구간 위 중단(기존 규칙)
    assert pl == SP.at_place(to) or (pl["kind"] == SP.TRANSIT and pl["edge_id"] == eid)


# ══════════════════════════════════════════════════════════════
#  의도 해석 (M03·M04·M05·E01·E05·F2)
# ══════════════════════════════════════════════════════════════

def _man(edges=ALL3, raw=None):
    s, D, c = _sess(edges=edges, raw=raw)
    return s, c, AI.build_manifest(D, s, PC, definition_revision=c.definition_revision)


def _ri(m, **kw):
    raw = {"time_kind": "NONE", "action_minutes": 0}
    raw.update(kw)
    return AI.resolve_intent(m, raw)


def test_exit_key_resolves_to_edge_and_ambiguity_keeps_only_matches():
    raw = raw_with()
    _stairs(raw)
    s, c, m = _man(edges=ALL3 + ("e_st#fwd",), raw=raw)
    k_st = _ekey(m.render(), "계단")
    i = _ri(m, movement_kind="EXIT", exit=k_st)                             # M03: 지목한 실제 구간
    assert i.movement == AI.MovementIntent(AI.MOVE_EXIT, "시장", "e_st#fwd")
    k_hm = next(r[0] for r in m.exits if r[1] == "e_hm#fwd")
    amb = _ri(m, movement_kind="EXIT", exit="NONE", exit_options=[k_st, k_hm])     # M04·E01: 둘 다 시장 쪽
    assert amb.movement is None and ("exit", "AMBIGUOUS") in amb.unresolved
    assert set(amb.exit_choices) == {"e_st#fwd", "e_hm#fwd"}
    one = _ri(m, movement_kind="EXIT", exit="NONE", exit_options=[k_st])
    assert one.movement.edge_id == "e_st#fwd"
    left = _ri(m, movement_kind="EXIT", exit="NONE", exit_options=[])      # M05: 기준 없는 '왼쪽'
    assert left.movement is None and ("exit", "MISSING") in left.unresolved and left.exit_choices == ()
    bad = _ri(m, movement_kind="EXIT", exit="E99")
    assert ("exit", "INVALID_CANDIDATE") in bad.unresolved
    assert "왼쪽·오른쪽" in m.render()                                       # 판단층위에 기준 없는 방향 규칙


def test_wander_and_legacy_explore_value_are_movement_not_stay():
    s, c, m = _man()
    w = _ri(m, movement_kind="WANDER", exit_options=[m.exits[0][0]])        # E05: 확인되는 조건만
    assert w.movement.kind == AI.MOVE_WANDER and w.movement.pool == (m.exits[0][1],) and not w.empty
    old = _ri(m, movement_kind="EXPLORE_DIRECTION")                          # F2: 옛 열거값 = 머무름 아님
    assert old.movement.kind == AI.MOVE_WANDER
    assert not any(code == "EXPLORE_UNSTRUCTURED" for _f, code in old.unresolved)
    assert "EXPLORE_DIRECTION" not in m.schema()["properties"]["movement_kind"]["enum"]


def test_semantic_record_round_trip_keeps_edge():
    i = AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_WANDER, "시장", "e_st#fwd", pool=("x",)),
                                definition_revision="r")
    back = AI.ResolvedActionIntent.from_semantic(i.semantic_record(), definition_revision="r")
    assert back.movement == AI.MovementIntent(AI.MOVE_WANDER, "시장", "e_st#fwd")   # pool은 기록하지 않는다
    old = AI.ResolvedActionIntent.from_semantic({"movement": {"kind": "DESTINATION", "destination_place": "시장"}},
                                                definition_revision="r")
    assert old.movement.edge_id is None                                      # 옛 기록 호환


# ══════════════════════════════════════════════════════════════
#  계획 — 구간 강제 · 결정적 선택 · 막힘 (M13·M14·M15·M18·M19·M20·M22·E03·E08)
# ══════════════════════════════════════════════════════════════

def test_forced_edge_is_not_replaced_by_parallel_shorter_edge():
    raw = raw_with()
    _stairs(raw)
    s, D, c = _sess(edges=ALL3 + ("e_st#fwd",), raw=raw)
    p = _plan(c, s, _exit(c, "e_st#fwd"))
    assert p.status == PW.PLAN_PLANNED and p.resolved_choice["route_signature"] == ["e_st#fwd"]   # M18
    k = SP.actor_knowledge(p.projected_world, PC)["edges"]
    assert "TRAVERSED" in k["e_st#fwd"]["acquisition_sources"]
    assert "TRAVERSED" not in (k["e_hm#fwd"].get("acquisition_sources") or [])
    d = _plan(c, s, AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_DESTINATION, "시장"),
                                            definition_revision=c.definition_revision))
    assert d.status in (PW.PLAN_PLANNED, PW.PLAN_NEEDS_CHOICE)               # M01: 목적지 이동은 기존 경로 정책
    hm = _plan(c, s, _exit(c, "e_hm#fwd"))
    assert hm.resolved_choice["route_signature"] == ["e_hm#fwd"] and p.end_minute > hm.end_minute


def test_stale_or_foreign_edge_is_rejected_without_ops():
    s, D, c = _sess()
    p = _plan(c, s, _exit(c, "e_mc#fwd"))                                    # 다른 출발점(시장)의 구간
    assert p.status == PW.PLAN_REJECTED and p.reason == "EXIT_STALE" and not p.ops
    none = _plan(c, s, AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_WANDER),
                                               definition_revision=c.definition_revision))
    assert none.status == PW.PLAN_REJECTED and none.reason == "NO_EXIT" and not none.ops
    assert "나설 만한 길이 보이지 않는다" in TW.narration_overlay(none, D, c, world=s)   # M13·M26


def test_wander_choice_is_deterministic_per_attempt_and_varies_across_attempts():
    s, D, c = _sess()
    a = [PW.wander_exit(D, s, PC, transaction_id="tx-1", narrative_seed="seed") for _ in range(3)]
    assert a[0] is not None and a.count(a[0]) == 3                           # M20: 같은 시도 = 같은 구간
    picks = {PW.wander_exit(D, s, PC, transaction_id=f"tx-{n}", narrative_seed="seed")[0] for n in range(40)}
    assert len(picks) > 1 and picks <= set(ALL3)                             # M22: 새 시도는 달라질 수 있다


def test_known_blocked_excluded_unknown_kept():
    blocked = {"e_hs#fwd": {"observed_result": "IMPASSABLE", "observed_state": "X", "action_policy": None,
                            "confirmed_at_minute": 0, "source": "OBSERVED"}}
    s, D, c = _sess(access=blocked)
    picks = {PW.wander_exit(D, s, PC, transaction_id=f"tx-{n}", narrative_seed="seed")[0] for n in range(60)}
    assert picks == {"e_hm#fwd", "e_hh#fwd"}                                 # M14
    assert PW.wander_exit(D, s, PC, pool=("e_hs#fwd",), transaction_id="t", narrative_seed="s") is None
    m = AI.build_manifest(D, s, PC, definition_revision=c.definition_revision)
    assert "[막힌 것으로 앎]" in m.render()
    p = _plan(c, s, _exit(c, "e_hs#fwd"))                                    # E03: 명시 지목은 기존 접근 정책
    assert p.status in (PW.PLAN_PLANNED, PW.PLAN_REJECTED)


def test_hidden_obstacle_stops_at_actual_access_without_reroll():
    s, D, c = _sess(edges=("e_mc#fwd",), at="시장")                         # door_1 실제 LOCKED, PC는 모름
    pick = PW.wander_exit(D, s, PC, transaction_id="tx", narrative_seed="seed")
    assert pick == ("e_mc#fwd", "성당")                                       # 실제 접근으로 거르지 않는다
    p = _plan(c, s, AI.ResolvedActionIntent(movement=AI.MovementIntent(AI.MOVE_WANDER, pick[1], pick[0]),
                                            definition_revision=c.definition_revision))
    assert p.outcome.get("kind") == PW.OUT_INTERRUPTED_BLOCKED                # M15·E08: 그 자리에서 멈춤
    assert SP.placement_of(p.projected_world, PC) == SP.at_place("시장")
    assert p.resolved_choice["route_signature"] == ["e_mc#fwd"]               # 다른 길로 재추첨 없음


def test_transit_has_no_new_exits():
    s, D, c = _sess()
    s.spatial_state["placements"][PC] = SP.transit(
        movement_id="mv", edge_id="e_hm#fwd", from_place="항구", to_place="시장",
        edge_started_at_minute=s.world_clock["minute_index"], edge_expected_end_minute=s.world_clock["minute_index"] + 5)
    assert AI.exit_candidates(D, s, PC) == ()                               # M19: 구간 위에서는 출구 없음
    p = _plan(c, s, _exit(c, "e_hh#fwd"))
    assert p.status == PW.PLAN_REJECTED and p.reason == "IN_TRANSIT_EXIT"


# ══════════════════════════════════════════════════════════════
#  자동 턴 배선 (M07~M11·M20·M25·M26·E04·E06·E07·E11·M24)
# ══════════════════════════════════════════════════════════════

def _move(**kw):
    return judgment(None, **dict({"movement_kind": "DESTINATION", "destination": "NONE", "action_minutes": 0}, **kw))


def _picker(kind, part, **kw):
    def route(kwargs):
        k = _ekey(H05Provider._text(kwargs), part)
        return judgment(None, movement_kind=kind, exit=k or "NONE", action_minutes=0, **kw)(kwargs)
    return route


def _here(r):
    return (SP.placement_of(r.sess, PC) or {}).get("place_id")


async def test_first_bare_move_asks_once_then_just_go_moves_one_edge(drig):
    r = drig
    r.make(edges=ALL3)
    w0, c0 = copy.deepcopy(r.sess.world_version), copy.deepcopy(r.sess.world_clock)
    r.prov.routes["judgment"] = _move()
    t1 = await turn(r, "이동한다")
    assert t1.status == tt.TurnStatus.WAITING_FOR_PLAYER                     # M07: 질문만
    assert "narration" not in r.prov.calls and _here(r) == "항구"
    assert r.sess.world_version == w0 and r.sess.world_clock == c0
    q = r.gch.sent[-1].content or ""
    assert "시장 쪽" in q and "산복도로 쪽" in q                            # 보이는 길을 말로
    assert t1.world_turn.pending_move["exits"] == ALL3 or set(t1.world_turn.pending_move["exits"]) == set(ALL3)
    asked = len(r.gch.sent)
    t2 = await turn(r, "그냥 간다")                                          # M09: 같은 답 — 되묻지 않는다
    assert t2.transaction_id == t1.transaction_id
    assert "직전에 진행자가 어디로 갈지 물었다" in r.prov.prompts["judgment"][-1][0]
    assert _here(r) in {"시장", "산복도로", "해안"} and r.prov.calls.count("narration") == 1
    assert not any("가실 곳" in (m.content or "") or "어디로 가시" in (m.content or "") for m in r.gch.sent[asked:])
    np_ = r.prov.prompts["narration"][-1][0]
    assert "도착했다" in np_ and "정본 이동이 없다" not in np_               # M26: 머무름 문구 아님
    head = TH.view(r.sess.session_id).head_entry()
    sem = TH.load_record(r.sess.session_id, head["transaction_id"])["judgment"]["judgment"][AI.INTENT_FIELD]["semantic"]
    assert sem["movement"]["kind"] == AI.MOVE_WANDER and sem["movement"]["edge_id"] in ALL3   # E11


async def test_first_wander_moves_without_question_or_extra_calls(drig):
    r = drig
    r.make(edges=ALL3)
    r.prov.routes["judgment"] = judgment(None, movement_kind="WANDER", exit_options=[], action_minutes=0)
    await turn(r, "아무 데나 간다")
    assert _here(r) in {"시장", "산복도로", "해안"}                         # M08
    assert r.prov.calls.count("judgment") == 1 and "continuation" not in r.prov.calls   # M25
    assert set(r.prov.calls) <= {"judgment", "logic", "narration", "extraction", "simulation", "irregular"}


async def test_question_then_other_action_does_not_move_and_clears_context(drig):
    r = drig
    r.make(edges=ALL3)
    r.prov.routes["judgment"] = _move()
    t1 = await turn(r, "이동한다")
    r.prov.routes["judgment"] = judgment(None, action_minutes=0)
    await turn(r, "그 전에 주변을 둘러본다")                                 # M11·E09
    assert _here(r) == "항구" and t1.world_turn.pending_move is None
    assert "정본 이동이 없다" in r.prov.prompts["narration"][-1][0]


async def test_question_then_specific_exit_moves_by_that_edge(drig):
    r = drig
    r.make(edges=ALL3)
    r.prov.routes["judgment"] = _move()
    await turn(r, "이동한다")
    r.prov.routes["judgment"] = _picker("EXIT", "해안")
    await turn(r, "해안 쪽으로")                                             # M10
    assert _here(r) == "해안"
    assert "TRAVERSED" in SP.actor_knowledge(r.sess, PC)["edges"]["e_hs#fwd"]["acquisition_sources"]


async def test_ambiguous_pointing_asks_only_matches_then_wander_stays_inside(drig):
    r = drig
    r.make(edges=ALL3)

    def amb(kwargs):
        t = H05Provider._text(kwargs)
        opts = [k for k in (_ekey(t, "시장"), _ekey(t, "산복도로")) if k]
        return judgment(None, movement_kind="EXIT", exit="NONE", exit_options=opts, action_minutes=0)(kwargs)
    r.prov.routes["judgment"] = amb
    t1 = await turn(r, "저 위쪽 길로")
    q = r.gch.sent[-1].content or ""
    assert "시장 쪽" in q and "산복도로 쪽" in q and "해안" not in q          # E01: 그 후보만
    assert set(t1.world_turn.pending_move["exits"]) == {"e_hm#fwd", "e_hh#fwd"}
    r.prov.routes["judgment"] = judgment(None, movement_kind="WANDER", exit_options=[], action_minutes=0)
    await turn(r, "아무 데나")
    assert _here(r) in {"시장", "산복도로"}                                  # E06: 축소 집합 밖으로 넓히지 않음


async def test_stale_context_after_world_change_is_dropped(drig):
    r = drig
    r.make(edges=ALL3)
    r.prov.routes["judgment"] = _move()
    t1 = await turn(r, "이동한다")
    wt = t1.world_turn
    assert r.gm._pending_move(r.sess, wt) is not None
    real = core.action_intent.world_version_id
    core.action_intent.world_version_id = lambda w: "other"                 # E04·M21: 기준선이 달라짐
    try:
        assert r.gm._pending_move(r.sess, wt) is None and wt.pending_move is None
    finally:
        core.action_intent.world_version_id = real
    await turn(r, "이동한다")                                                # 다시 묻는다(질문 문맥 새로)
    assert wt.pending_move is not None
    r.sess.spatial_state["placements"][PC] = SP.at_place("시장")              # 위치가 바뀌면 문맥 무효
    assert r.gm._pending_move(r.sess, wt) is None


async def test_question_send_failure_leaves_no_pending_context(drig, monkeypatch):
    r = drig
    r.make(edges=ALL3)
    r.prov.routes["judgment"] = _move()
    t1 = await turn(r, "이동한다")
    wt = t1.world_turn
    wt.pending_move = None

    async def boom(*a, **k):
        raise RuntimeError("전송 실패(테스트)")
    monkeypatch.setattr(core, "stream_text_to_channel", boom)
    with pytest.raises(RuntimeError):
        await r.gm._ask_destination(r.sess, wt, t1.transaction_id, r.master)
    assert wt.pending_move is None                                           # E07: 유령 대기 없음


async def test_rerender_reuses_selected_edge(drig):
    r = drig

    def patch(raw):
        _stairs(raw)
    r.make(edges=ALL3 + ("e_st#fwd",), raw_patch=patch)
    r.prov.routes["judgment"] = _picker("EXIT", "계단")
    await turn(r, "북쪽 계단으로")
    assert _here(r) == "시장"
    known = SP.actor_knowledge(r.sess, PC)["edges"]
    assert "TRAVERSED" in known["e_st#fwd"]["acquisition_sources"]
    n = r.prov.calls.count("judgment")
    started, reason = await r.gm.rerender_latest(r.sess)                     # M20·E11: 같은 구간 재현
    assert started, reason
    assert r.prov.calls.count("judgment") == n and _here(r) == "시장"
    head = TH.view(r.sess.session_id).head_entry()
    sem = TH.load_record(r.sess.session_id, head["transaction_id"])["judgment"]["judgment"][AI.INTENT_FIELD]["semantic"]
    assert sem["movement"]["edge_id"] == "e_st#fwd"
    assert "TRAVERSED" not in (SP.actor_knowledge(r.sess, PC)["edges"]["e_hm#fwd"].get("acquisition_sources") or [])


async def test_legacy_judgment_has_no_exit_candidates(drig):
    r = drig                                                                 # M24·E12: make() 안 함 = LEGACY
    r.prov.routes["judgment"] = judgment(None)
    await turn(r, "이동한다")
    jp, cfg = r.prov.prompts["judgment"][-1]
    assert "현재 장소에서 나가는 길" not in jp
    assert "action_intent" not in ((getattr(cfg, "response_schema", None) or {}).get("properties") or {})
