# -*- coding: utf-8 -*-
"""v7.17 소규모 안정화 P1~P4 — START 무후보 호출 생략 · item_facts 턴 경계 · 프로필 확정 뒤 재시작 · member:* 도달성.

provider·Discord는 대역이다(실서비스 미검증).
"""
from __future__ import annotations

import copy
import json
import pathlib
from types import SimpleNamespace

import pytest

import core
from core import initial_knowledge as IK
from core import items as IT
from core import turn_transaction as tt
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID
from tests.policy.test_h05bc_turn_wiring import PC, drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_v717_initial_knowledge import INFOS, Ch, ai, deliver, ik_raw, pc_cog, setup  # noqa: F401

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]


# ══════════════════════════════════════════════════════════════
# P1 — START에 새로 전달할 적격 후보가 없으면 호출 0
# ══════════════════════════════════════════════════════════════

PROFILE_ONLY = {k: v for k, v in INFOS.items() if k in ("ik.common", "ik.rumor", "ik.guild", "clue.hidden")}


async def test_P1_start_without_new_candidates_makes_no_call_and_genesis_proceeds(rig, ai):
    r = rig
    s = setup(r, ik_raw(PROFILE_ONLY))
    ch = Ch()
    assert await deliver(r, s, ch, "PROFILE") == IK.DELIVERED and len(ai.calls) == 1
    assert await deliver(r, s, ch, "START", {"id": "harbor"}) == IK.SKIPPED
    assert len(ai.calls) == 1 and len(ch.sent) == 1                       # 상기만을 위한 호출·발송 없음
    rec = s.initial_knowledge_progress["players"][PLAYER_UID]["START"]
    assert rec["reason"] == "NO_CANDIDATES" and rec["ai_calls"] == 0
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    acqs = [a["acquisition_id"] for i in ("ik.common",) for a in pc_cog(s)[i]["acquisitions"]]
    assert acqs == [f"initial:{s.session_id}:{PLAYER_UID}:PROFILE:ik.common"]   # 중복 인지 없음


async def test_P1_new_start_candidate_keeps_call_and_recontext(rig, ai):
    r = rig
    s = setup(r)                                   # ik.dock(harbor 필수) 있음
    ch = Ch()
    await deliver(r, s, ch, "PROFILE")
    ai.resp = {"optional_keys": [], "recontextualized_keys": ["R1"], "master_briefing": "x"}
    assert await deliver(r, s, ch, "START", {"id": "harbor"}) == IK.DELIVERED
    assert len(ai.calls) == 2 and "R1: 항구에는" in ai.calls[-1]
    st = s.initial_knowledge_progress["players"][PLAYER_UID]["START"]
    assert [x["id"] for x in st["selected_info"]] == ["ik.dock"] and st["recontextualized"] == ["ik.common"]


async def test_P1_new_candidate_judged_after_access_and_frame_and_delivery(rig, ai):
    """원시 START 태그 수가 아니라 프로필·틀·접근·이미 전달분을 반영한 최종 적격 집합으로 판정."""
    r = rig
    infos = dict(PROFILE_ONLY)
    infos["ik.start.guild"] = {"proposition": "p", "summary": "길드원만 아는 시작 소식",
                               "initial_knowledge": {"tier": "required", "phases": ["START"],
                                                     "access": {"field": "소속", "op": "equals", "value": "도적 길드"}}}
    infos["ik.start.dock"] = {"proposition": "p", "summary": "부두 틀 전용 소식",
                              "initial_knowledge": {"tier": "required", "phases": ["START"], "start_frame_ids": ["dock"]}}
    s = setup(r, ik_raw(infos))                     # 소속 = 상인 길드 → ik.start.guild 불허, harbor 틀 → dock 전용 제외
    await deliver(r, s, Ch(), "PROFILE")
    assert await deliver(r, s, Ch(), "START", {"id": "harbor"}) == IK.SKIPPED and len(ai.calls) == 1


async def test_P1_yeongdo_profile_one_call_start_zero_calls(rig, ai):
    raw = json.loads((ROOT / "scenarios/영도.json").read_text(encoding="utf-8"))
    r = rig
    from tests.policy.test_h06a_lifecycle import setup_session
    s = setup_session(r, raw)
    s.scenario_id = "영도"
    s.players[PLAYER_UID]["profile"] = {"직업": "어부", "성별": "남성"}
    d, why = WL.compile_candidate(s)
    assert d is not None, why
    frame = next(f for f in d["initialization_definitions"]["start_frames"] if f.get("world_start"))
    ch = Ch()
    assert await IK.run_phase(r.bot, s, PLAYER_UID, "PROFILE", ch, d) == IK.DELIVERED
    assert await IK.run_phase(r.bot, s, PLAYER_UID, "START", ch, d, frame={"id": frame["frame_id"]}) == IK.SKIPPED
    assert len(ai.calls) == 1 and len(ch.sent) == 1
    rows = s.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]["selected_info"]
    assert sorted(x["id"] for x in rows) == ["yd.info.ik.bridges_cut", "yd.info.ik.infected_nature"]


# ══════════════════════════════════════════════════════════════
# P2 — item_facts는 그 사실을 만든 커밋 바로 다음 계획에서만
# ══════════════════════════════════════════════════════════════

def test_P2_current_facts_bound_to_last_commit():
    s = SimpleNamespace(item_facts={"tx": "t1", "used": ["손전등"], "paid": ["빵"]},
                        world_version={"version_id": "wv_a", "cause_id": "t1"})
    assert IT.current_facts(s) == {"used": ["손전등"], "paid": ["빵"]}            # 다음 계획 1회 인식
    s.world_version = {"version_id": "wv_b", "cause_id": "t2"}                     # 아이템 계획 없는 다른 커밋
    assert IT.current_facts(s) == {"used": [], "paid": []}
    s.world_version = {"version_id": "wv_a", "cause_id": "t1"}                     # 되감기로 둘 다 복원
    assert IT.current_facts(s)["paid"] == ["빵"]
    assert IT.current_facts(SimpleNamespace(item_facts={}, world_version=None)) == {"used": [], "paid": []}
    src = (ROOT / "cogs/gm.py").read_text(encoding="utf-8")
    assert "item_facts=core.items.current_facts(session)" in src and src.count("item_facts=core.items.current_facts(") == 1


async def test_P2_use_evaluated_same_turn_then_not_refired_and_rewind_consistent(drig):
    # P1(D4, 사용자 승인으로 v7.17 Q11 다음 턴 재평가를 대체): 사용·지급 사실은 만든 턴의 보충 진행이 평가하고
    #   다음 계획은 다시 평가하지 않는다(이중 반응 금지). 사실 기록·되감기 일치는 그대로다.
    r = drig
    r.make()
    r.sess.resources = {"테스터": {"손전등": 1}}
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "USE", "owner": "O1", "item": "H1", "quantity": 1}]})]
    tx1 = await turn(r, "손전등을 비춘다")
    assert tx1.status == tt.TurnStatus.COMMITTED
    assert r.sess.item_facts["used"] == ["손전등"] and r.sess.item_facts["evaluated_same_turn"] is True
    assert IT.current_facts(r.sess)["used"] == []                                  # (a)/(c) 같은 턴 평가 — 다음 계획 재평가 없음
    after1 = (copy.deepcopy(r.sess.item_facts), copy.deepcopy(r.sess.world_version))
    # 아이템 계획 없이 커밋된 턴(정본 version만 전진)과 같은 효과 — 옛 사실은 새 사실이 아니다
    r.sess.item_facts = dict(r.sess.item_facts)
    r.prov.routes["extraction"] = [extraction({})]
    tx2 = await turn(r, "숨을 고른다")
    assert tx2.status == tt.TurnStatus.COMMITTED
    assert IT.current_facts(r.sess) == {"used": [], "paid": []}                    # (b) 재발화 없음
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert (r.sess.item_facts, r.sess.world_version) == after1                     # (d) 되감기 후 시간 일치
    assert IT.current_facts(r.sess)["used"] == []


def test_P2_non_item_commit_without_plan_does_not_refresh_facts_but_retires_them():
    """추출 없는 커밋 경로(item_plan 없음)는 item_facts를 덮지 않는다 — current_facts가 version으로 걸러야 한다."""
    wv = core.world_state.next_world_version("sid", None, cause_kind="PLAYER_TURN", cause_id="t_pay")
    s = SimpleNamespace(item_facts={"tx": "t_pay", "used": [], "paid": ["통행료"]}, world_version=wv)
    assert IT.current_facts(s)["paid"] == ["통행료"]
    s.world_version = core.world_state.next_world_version("sid", s.world_version, cause_kind="PLAYER_TURN",
                                                          cause_id="t_narrate")
    assert IT.current_facts(s)["paid"] == [] and s.item_facts["paid"] == ["통행료"]   # 저장값은 그대로(권위 불변)


async def test_P2_paid_event_spawns_once_on_next_plan_only():
    from tests.policy.test_h04b_events_progression import eq as _eq
    from tests.policy.test_h05a_intent_planning import PLAYER_UID as UID, SEED, make_session
    from tests.policy.test_v717_items import _paid_raw
    from core import action_intent as AI
    tpl = {"version": 1, "spawn_when": _eq({"ref": "OBSERVER_PAID_ITEM", "item": "통행료"}, True),
           "phases": {"open": {"eligible_when": {"const": True}, "priority": 1}}}
    s, D = make_session(_paid_raw({"paid_entry": tpl}), at="시장", edges=("e_mc#fwd",), items={"통행료": 1})
    c = core.player_world.session_world_context(s)

    def spawned():
        m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
        intent = AI.resolve_intent(m, {"movement_kind": "NONE", "time_kind": "DURATION", "duration_minutes": 1,
                                       "activity": "WAIT"}, world=s)
        p = core.player_world.plan_player_turn(c, s, intent, transaction_id="t", narrative_seed=SEED,
                                               capabilities=core.player_world.pc_capabilities(s, UID, D),
                                               item_facts=IT.current_facts(s))
        return any("paid_entry" in json.dumps(o.payload, ensure_ascii=False) for o in p.ops)
    s.item_facts = {"tx": "t_pay", "used": [], "paid": ["통행료"]}
    nv = core.world_state.next_world_version
    s.world_version = nv(s.session_id, s.world_version, cause_kind="PLAYER_TURN", cause_id="t_pay")
    assert spawned()                                                             # PAY 커밋 직후 다음 계획
    s.world_version = nv(s.session_id, s.world_version, cause_kind="PLAYER_TURN", cause_id="t_other")
    assert not spawned()                                                         # 다른 커밋 뒤에는 재발화 없음


# ══════════════════════════════════════════════════════════════
# P3 — 프로필 확정·저장 선택 뒤 재시작
# ══════════════════════════════════════════════════════════════

def _flow_session(r):
    s = setup(r)
    s.creation_state = {"step": "profile", "history": [], "data": {}}
    s.profile_creation_progress = {PLAYER_UID: {"version": 1, "uid": PLAYER_UID, "run": {"finalized": True}}}
    return s


async def _complete_like_save_button(r, s):
    st = SimpleNamespace(bot=r.bot, session=s, uid=PLAYER_UID, channel_id=r.gch.id)
    await core.profile_creation_ui._complete(st)


def _disk(s):
    return json.loads(pathlib.Path(f"sessions/{s.session_id}/data.json").read_text(encoding="utf-8"))


@pytest.fixture
def flow_spy(monkeypatch):
    order = []

    async def _render(bot, session, channel):
        order.append(("render", core.creation.current_step(session)))
    monkeypatch.setattr(core.session_flow, "render", _render)
    return order


async def test_P3_restart_after_save_choice_resumes_to_next_step_once(rig, ai, flow_spy, monkeypatch):
    r = rig
    s = _flow_session(r)
    await _complete_like_save_button(r, s)
    d = _disk(s)
    assert PLAYER_UID not in d["profile_creation_progress"] and d["creation_state"]["data"]["profile"] == "완료"
    # 재시작: 진행 기록 없음 · 플로우는 profile 단계 — resume_all이 기존 on_profile_done으로 이어 간다
    r.bot.active_sessions = {s.game_ch_id: s}
    ch = Ch(s.game_ch_id)
    monkeypatch.setattr(r.bot, "get_channel", lambda cid: ch if cid == s.game_ch_id else None)
    draft = {"run": {"values": {"직업": "초안직업"}}}
    s.players[PLAYER_UID]["draft_should_not_be_read"] = draft
    assert await core.profile_creation_ui.resume_all(r.bot) == 1
    assert flow_spy == [("render", "open")] and len(ai.calls) == 1 and len(ch.sent) == 1
    assert "상인" in ai.calls[0] and "초안직업" not in ai.calls[0]                  # (d) 확정 프로필만
    # 다음 단계 화면을 띄운 뒤 다시 재시작해도(단계 미저장 포함) 중복 발송·인지 없음
    s.creation_state["step"] = "profile"
    assert await core.profile_creation_ui.resume_all(r.bot) == 1
    assert len(ai.calls) == 1 and len(ch.sent) == 1 and flow_spy[-1] == ("render", "open")
    s.creation_state["step"] = "open"
    assert await core.profile_creation_ui.resume_all(r.bot) == 0


async def test_P3_restart_with_uncertain_profile_packet_no_auto_resend(rig, ai, flow_spy, monkeypatch):
    r = rig
    s = _flow_session(r)
    await _complete_like_save_button(r, s)
    rec = IK.record(s, PLAYER_UID, "PROFILE")
    rec.update(state=IK.SENDING, pages=["x"], delivery_nonce="ik-restart", selected_info=[
        {"id": "ik.common", "summary_snapshot": "항구에는 밤마다 짙은 안개가 낀다.", "kind": "fact"}])
    r.bot.active_sessions = {s.game_ch_id: s}
    ch = Ch(s.game_ch_id)
    ch.history_fail = True
    monkeypatch.setattr(r.bot, "get_channel", lambda cid: ch if cid == s.game_ch_id else None)
    await core.profile_creation_ui.resume_all(r.bot)
    assert IK.state_of(s, PLAYER_UID, "PROFILE") == IK.UNCERTAIN and ai.calls == []
    assert flow_spy == [] and core.creation.current_step(s) == "profile"           # 진행 보류(재전송·인지 없음)
    assert not any(m.embeds for m in ch.sent)                                       # 안내 버튼만, 패킷 재발송 없음


async def test_P3_skipped_profile_resumes_and_genesis_once(rig, ai, flow_spy, monkeypatch):
    r = rig
    s = setup(r, ik_raw({}))
    s.creation_state = {"step": "profile", "history": [], "data": {}}
    s.profile_creation_progress = {PLAYER_UID: {"version": 1}}
    await _complete_like_save_button(r, s)
    r.bot.active_sessions = {s.game_ch_id: s}
    monkeypatch.setattr(r.bot, "get_channel", lambda cid: Ch(s.game_ch_id))
    assert await core.profile_creation_ui.resume_all(r.bot) == 1
    assert IK.state_of(s, PLAYER_UID, "PROFILE") == IK.SKIPPED and ai.calls == [] and flow_spy == [("render", "open")]
    await deliver(r, s, Ch(), "START", {"id": "harbor"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_ALREADY   # (e) 1회


async def test_P3_dm_or_non_flow_completion_not_marked(rig):
    r = rig
    s = setup(r)
    s.creation_state = {}
    s.profile_creation_progress = {PLAYER_UID: {"version": 1}}
    await _complete_like_save_button(r, s)
    assert s.creation_state == {}                                                  # 플로우 밖은 표식 없음


def test_P3_initial_knowledge_record_not_reversible():
    assert IK.FIELD not in core.turn_history.REVERSIBLE_FIELDS                       # (f)


# ══════════════════════════════════════════════════════════════
# P4 — member:* 도달성(조사 결과: PC producer 없음 — 수정하지 않음, 경계만 고정)
# ══════════════════════════════════════════════════════════════

def _member_raw():
    from tests.policy.test_v717_items import _paid_raw
    raw = _paid_raw()
    rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
    rule["actor_overrides"].insert(0, {"requires_capability": "member:guild", "result": "PASSABLE",
                                       "override_id": "member_guild_pass"})
    return raw


def test_P4_member_pass_and_pay_are_separate_paths():
    from tests.policy.test_h05a_intent_planning import K, go, make_session, manifest, plan as wplan
    seen = {"door_1": {"observed_result": "ACTION_REQUIRED", "observed_state": "LOCKED",
                       "action_policy": "CONFIRM", "confirmed_at_minute": K, "source": "OBSERVED"}}
    s, D = make_session(_member_raw(), at="시장", edges=("e_mc#fwd",), access=seen, items={"통행료": 1})
    # 소속 capability가 실제로 공급되면 지급 없이 통과(물건 불변)
    p, _, _ = wplan(s, go(manifest(s), "성당"), caps=("member:guild", "통행료"))
    assert p.outcome["arrived"] and p.item_payments == []
    # 소속 없이 물건만 → 지급 확인 경로(PAY) — 회원이 되지 않는다
    caps = core.player_world.pc_capabilities(s, "555001", D)
    assert not any(c.startswith("member:") for c in caps)
    opts = core.player_world.access_confirm_options(D, s, core.player_world.session_world_context(s).lead_pc, caps)
    m = manifest(s, access_options=opts)
    p2, _, _ = wplan(s, dict(go(m, "성당"), access_confirm="K1"), access_options=opts, caps=caps)
    assert [x["capability"] for x in p2.item_payments] == ["통행료"]


def test_P4_no_member_inference_from_items_faction_or_persona():
    from tests.policy.test_h05a_intent_planning import make_session
    s, D = make_session(_member_raw(), items={"member:guild 증표": 1, "길드 문장": 1})
    s.players["555001"]["profile"] = {"소속": "guild", "배경": "member:guild 출신"}
    s.event_instances = {"e1": {"lifecycle": "RESOLVED", "local_state": {"grant_faction": "guild"},
                                "phase_entered_at": 1}}
    caps = core.player_world.pc_capabilities(s, "555001", D)
    assert "member:guild" not in caps                                              # 소지품 이름·소속·서술로 추론 없음
    for f in ("core/player_world.py", "core/items.py"):
        assert "member:" not in (ROOT / f).read_text(encoding="utf-8")              # 하드코딩 없음
