# -*- coding: utf-8 -*-
"""v7.17.2 — 정본 소속(RESOLVED 사건 grant_faction) → 접근 capability(시나리오 faction_capabilities 저작 시에만).

합성 정의는 임의 표시명·임의 capability 이름을 쓴다(장르·영도 특례 없음). provider·Discord 대역.
"""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

import core
from core import player_world as PW
from core import spatial as SP
from tests.policy.test_h04b_events_progression import K, eq
from tests.policy.test_h05a_intent_planning import PLAYER_UID, SEED, go, make_session, raw_with
from tests.policy.test_h05bc_turn_wiring import PC, drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
STAR, MOON = "별빛 길드", "달그림자 회"


def _grant_tpl(value, spawn_when=None):
    return {"version": 1, "spawn_when": spawn_when or eq({"ref": "ACCESS_STATE", "control": "door_2"}, "OPEN"),
            "phases": {"joined": {"eligible_when": {"const": True}, "priority": 1, "terminal": True}},
            "transition_cases": [{"case_id": "spawn", "from": None, "to": "joined", "steps": [
                {"step_id": "grant", "type": "SET_EVENT_LOCAL", "key": "grant_faction", "value": value}]}],
            "repeat_policy": "ONCE_PER_SESSION",
            "resolve_when": eq({"ref": "EVENT_PHASE", "instance": "$self"}, "joined")}


def _raw(mapping=None, templates=None, extra_override=None):
    raw = raw_with(templates if templates is not None else {"join_star": _grant_tpl(STAR), "join_moon": _grant_tpl(MOON)})
    rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
    rule["actor_overrides"].insert(0, {"requires_capability": "guild:star", "result": "PASSABLE",
                                       "override_id": "star_pass"})
    rule["actor_overrides"].insert(1, {"requires_capability": "guild:moon", "result": "PASSABLE",
                                       "override_id": "moon_pass"})
    if extra_override:
        rule["actor_overrides"].append(extra_override)
    if mapping is not False:
        raw["faction_capabilities"] = mapping or {STAR: ["guild:star"], MOON: ["guild:moon"]}
    return raw


def _inst(value=None, lifecycle="RESOLVED", phase="joined"):
    local = {"grant_faction": value} if value else {}
    return {"lifecycle": lifecycle, "current_phase": phase, "local_state": local, "phase_entered_at": K}


SEEN = {"door_1": {"observed_result": "ACTION_REQUIRED", "observed_state": "LOCKED",
                   "action_policy": "SEPARATE_ACTION", "confirmed_at_minute": K, "source": "OBSERVED"}}


def _session(raw=None, instances=None, items=None):
    s, D = make_session(raw or _raw(), at="시장", edges=("e_mc#fwd",), access=SEEN, items=items)
    s.event_instances = copy.deepcopy(instances or {})
    return s, D


def _try_enter(s, D):
    caps = PW.pc_capabilities(s, PLAYER_UID, D)
    acc = PW.pc_access_capabilities(s, PLAYER_UID, D)
    c = PW.session_world_context(s)
    from core import action_intent as AI
    m = AI.build_manifest(c.definition, s, c.lead_pc, definition_revision=c.definition_revision)
    intent = AI.resolve_intent(m, go(m, "성당"), world=s)
    p = PW.plan_player_turn(c, s, intent, transaction_id="tx-f", narrative_seed=SEED, capabilities=caps,
                            access_capabilities=acc)
    return p, acc


# 1·4 가입 성공 — 봉쇄(LOCKED) 상태에서 소속 override로 통과, 지급·전역 개방 없음
def test_joined_faction_passes_locked_gate_without_payment_or_global_open():
    s, D = _session(instances={"ev1": _inst(STAR)}, items={"통행료": 1})
    p, acc = _try_enter(s, D)
    assert acc == ("guild:star", "통행료")
    assert p.outcome["arrived"] and p.item_payments == []
    assert (s.access_control_state or {}).get("door_1", {}).get("state", "LOCKED") == "LOCKED"
    assert s.resources["주인공"] == {"통행료": 1}


# 2 미가입·가입 실패·추론 차단
@pytest.mark.parametrize("instances", [
    {},                                                         # 미가입
    {"ev1": _inst(None, phase="thief")},                        # RESOLVED지만 grant 없음(가입 실패 종결)
    {"ev1": _inst(STAR, lifecycle="ACTIVE")},                   # 진행 중
    {"ev1": _inst(STAR, lifecycle="CANCELLED")},
    {"ev1": _inst("미매핑 세력")},                               # 매핑 없는 값
])
def test_no_capability_without_resolved_mapped_grant(instances):
    s, D = _session(instances=instances, items={"guild:star 증표": 1, "별빛 길드 문장": 1})
    s.players[PLAYER_UID]["profile"] = {"소속": STAR, "배경": "guild:star 출신"}
    s.player_faction = STAR                                      # 미러·표시값은 근거가 아니다
    s.relationship_state = {"r": {"from_holder": "x", "to_holder": "y", "deltas": []}}
    assert "guild:star" not in PW.faction_access_capabilities(D, s)
    p, _ = _try_enter(s, D)
    assert not p.outcome.get("arrived")


# 3 다중 가입 — 최근 하나가 아니라 전부
def test_multiple_memberships_all_apply():
    s, D = _session(instances={"a": _inst(STAR), "b": dict(_inst(MOON), phase_entered_at=K + 10)})
    assert PW.faction_access_capabilities(D, s) == ("guild:moon", "guild:star")
    assert core.turn_world.derived_player_faction(s) == MOON          # 표시 뷰는 최근 하나(근거로 쓰지 않음)
    s2, D2 = _session(_raw(templates={"join_star": _grant_tpl(STAR), "join_moon": _grant_tpl(MOON)}),
                      instances={"a": _inst(STAR), "b": dict(_inst(MOON), phase_entered_at=K + 10)})
    s2.access_control_state = {}
    p, _ = _try_enter(s2, D2)
    assert p.outcome["arrived"]


# 7 아이템 오인 방지 — 소속 capability는 사건 보유 조건(OBSERVER_HAS_ITEM)에 들어가지 않는다
def test_membership_is_not_item_possession():
    tpl = {"version": 1, "spawn_when": eq({"ref": "OBSERVER_HAS_ITEM", "item": "guild:star"}, True),
           "phases": {"x": {"eligible_when": {"const": True}, "priority": 1}}}
    raw = _raw(templates={"join_star": _grant_tpl(STAR), "join_moon": _grant_tpl(MOON), "has_probe": tpl})
    s, D = _session(raw, instances={"ev1": _inst(STAR)})
    assert "guild:star" not in PW.pc_capabilities(s, PLAYER_UID, D)
    p, _ = _try_enter(s, D)
    assert not any("has_probe" in json.dumps(o.payload, ensure_ascii=False) for o in p.ops)
    s.resources = {"주인공": {"guild:star": 1}}                    # 같은 이름 물건을 실제로 들면 보유는 참(기존 의미)
    p2, _ = _try_enter(s, D)
    assert any("has_probe" in json.dumps(o.payload, ensure_ascii=False) for o in p2.ops)


# 8 저작 선택성 · 컴파일 오류
def test_compile_optional_and_strict():
    d0 = core.definition_compiler.compile_scenario(_raw(mapping=False), "t").definition
    assert "faction_capabilities" not in d0 and PW.faction_access_capabilities(d0, object()) == ()
    for bad, code in (({"없는 세력": ["guild:star"]}, "FACTION_UNKNOWN"),
                      ({STAR: ["guild:none"]}, "FACTION_CAPABILITY_UNUSED"),
                      ({STAR: []}, "FACTION_CAPABILITIES_INVALID"),
                      ({STAR: ["guild:star", "guild:star"]}, "FACTION_CAPABILITIES_INVALID")):
        errs = [x.code for x in core.definition_compiler.compile_scenario(_raw(mapping=bad), "t").errors]
        assert code in errs, (bad, errs)
    expected = {"scenario.example": "sha256:eb812596eb87f", "다크판타지": "sha256:c03dbb4221aa6",
                "무협": "sha256:07f7f4cfda43f", "빈시나리오": "sha256:2422a64278cbc"}
    for name, rev in expected.items():
        raw = json.loads((ROOT / f"scenarios/{name}.json").read_text(encoding="utf-8"))
        res = core.definition_compiler.compile_scenario(raw, name)
        assert res.revision.startswith(rev) and "faction_capabilities" not in (res.definition or {})


# 6 영도 — 조건부·감시 수용도 기존 grant_faction 정본대로 포함, 가입 실패(thief)는 제외
def test_yeongdo_mapping_and_conditional_admissions():
    raw = json.loads((ROOT / "scenarios/영도.json").read_text(encoding="utf-8"))
    D = core.definition_compiler.compile_scenario(raw, "영도").definition
    assert D["faction_capabilities"] == {"남항 중공업 연대": ["member:namhang"], "조도 방위 사령부": ["member:jodo"],
                                         "해련 자치 마을": ["member:haeryeon"]}
    T = D["event_templates"]

    def grant_of(tid, phase):
        return {st["value"] for c in T[tid]["transition_cases"] if c.get("to") == phase
                for st in c["steps"] if st.get("key") == "grant_faction"}
    assert grant_of("yd_ev_namhang_infiltration", "judged") == {"남항 중공업 연대"}     # 조건부 수용
    assert grant_of("yd_ev_jodo_defector", "judged") == {"조도 방위 사령부"}            # 감시하 수용
    assert grant_of("yd_ev_namhang_infiltration", "thief") == set()
    w = type("W", (), {})()
    w.event_instances = {"i1": _inst("남항 중공업 연대", phase="judged"), "i2": _inst("조도 방위 사령부", phase="judged"),
                         "i3": _inst(None, phase="thief")}
    assert PW.faction_access_capabilities(D, w) == ("member:jodo", "member:namhang")
    w.event_instances = {"i3": _inst(None, phase="thief")}
    assert PW.faction_access_capabilities(D, w) == ()
    w.event_instances = {"i4": _inst("중리 교역소", phase="registered")}
    assert PW.faction_access_capabilities(D, w) == ()                                   # 매핑 대상 아님


# 5 되감기 — 가입 사건이 정본에서 사라지면 능력도 함께 사라진다(새 권위·저장 필드 없음)
async def test_join_commit_then_rewind_removes_capability(drig):
    r = drig

    def patch(raw):
        raw["event_templates"]["join_star"] = _grant_tpl(STAR, spawn_when=eq({"ref": "SCENE_PLACE"}, "시장"))
        rule = raw["access_control_definitions"]["rules"]["door_rule"]["states"]["LOCKED"]
        rule["actor_overrides"].insert(0, {"requires_capability": "guild:star", "result": "PASSABLE",
                                           "override_id": "star_pass"})
        raw["faction_capabilities"] = {STAR: ["guild:star"]}
    D = r.make(raw_patch=patch)
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction({})]
    await turn(r, "숨을 고른다")
    assert PW.faction_access_capabilities(D, r.sess) == ()
    r.prov.routes["judgment"] = judgment("시장")
    tx = await turn(r, "시장으로 간다")
    assert tx.status == core.turn_transaction.TurnStatus.COMMITTED
    assert SP.placement_of(r.sess, PC) == SP.at_place("시장")
    assert PW.faction_access_capabilities(D, r.sess) == ("guild:star",)
    assert "guild:star" in PW.pc_access_capabilities(r.sess, PLAYER_UID, D)
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert PW.faction_access_capabilities(D, r.sess) == ()


# 10 데이터·호출 영향 없음 · 범용(코드 하드코딩 없음)
def test_no_new_fields_calls_or_hardcoding():
    assert len(core.SESSION_FIELDS) == 105          # P1: world_pending_signals(전달 대기) 1필드 추가
    src = (ROOT / "core/player_world.py").read_text(encoding="utf-8")
    for s in ("member:", "남항", "해련", "조도", "중리"):
        assert s not in src
    assert "generate_content" not in src
    gm = (ROOT / "cogs/gm.py").read_text(encoding="utf-8")
    assert gm.count("pc_access_capabilities(") == 2


# ── v7.17.3 접근 능력 출처 분리 — 매핑에 선언된 토큰은 정본 소속에서만 ─────────────────

def _confirm_and_rule_pass(s, D, acc):
    """출입 확인 후보(미리보기)와 실제 규칙 판정이 같은 집합을 쓰는지 — LOCKED 규칙을 그 집합으로 평가."""
    rule = D["access_control_definitions"]["rules"]["door_rule"]
    dec = core.access.evaluate_state_table(rule, "LOCKED", acc)
    opts = PW.access_confirm_options(D, s, PW.session_world_context(s).lead_pc, acc)
    return dec, opts


def test_same_named_item_does_not_grant_membership_in_preview_or_plan():
    s, D = _session(instances={}, items={"guild:star": 1})
    acc = PW.pc_access_capabilities(s, PLAYER_UID, D)
    assert "guild:star" in PW.pc_capabilities(s, PLAYER_UID, D)          # 실물 보유는 그대로(아이템 의미)
    assert "guild:star" not in acc                                           # 소속 접근 권한은 아님
    dec, opts = _confirm_and_rule_pass(s, D, acc)
    assert dec["result"] != core.access.PASSABLE and not any("star_pass" in o["action_id"] for o in opts)
    p, _ = _try_enter(s, D)
    assert not p.outcome.get("arrived")
    # 같은 물건 + 유효한 가입 정본 → 회원 경로 통과(물건 유무와 무관)
    s.event_instances = {"ev1": _inst(STAR)}
    acc2 = PW.pc_access_capabilities(s, PLAYER_UID, D)
    assert "guild:star" in acc2 and _confirm_and_rule_pass(s, D, acc2)[0]["result"] == core.access.PASSABLE
    assert _try_enter(s, D)[0].outcome["arrived"]


def test_ordinary_item_access_and_payment_preserved():
    key = {"requires_capability": "청동 열쇠", "result": "PASSABLE", "override_id": "key_pass"}
    s, D = _session(_raw(extra_override=key), items={"청동 열쇠": 1})
    assert "청동 열쇠" in PW.pc_access_capabilities(s, PLAYER_UID, D)          # 매핑 밖 토큰은 기존대로
    assert _try_enter(s, D)[0].outcome["arrived"]
    pay = {"requires_capability": "통행료", "result": "ACTION_REQUIRED", "action_policy": "CONFIRM",
           "action_delay_minutes": 1, "payment": {"quantity": 1}}
    s2, D2 = _session(_raw(extra_override=pay), items={"통행료": 1})
    pc = PW.session_world_context(s2).lead_pc
    s2.spatial_knowledge[pc]["access"]["door_1"] = dict(SEEN["door_1"], action_policy="CONFIRM")
    acc = PW.pc_access_capabilities(s2, PLAYER_UID, D2)
    opts = PW.access_confirm_options(D2, s2, PW.session_world_context(s2).lead_pc, acc)
    assert len(opts) == 1 and "실제로 건넨다" in opts[0]["label"]                 # PAY 경로 유지


def test_without_mapping_item_named_token_keeps_legacy_meaning():
    s, D = _session(_raw(mapping=False), items={"guild:star": 1})
    assert "faction_capabilities" not in D
    assert PW.pc_access_capabilities(s, PLAYER_UID, D) == PW.pc_capabilities(s, PLAYER_UID, D)
    assert _try_enter(s, D)[0].outcome["arrived"]                               # 기존 시나리오 의미 불변


def test_yeongdo_member_named_item_is_not_membership():
    raw = json.loads((ROOT / "scenarios/영도.json").read_text(encoding="utf-8"))
    D = core.definition_compiler.compile_scenario(raw, "영도").definition
    sess = type("S", (), {})()
    sess.players = {PLAYER_UID: {"name": "주인공"}}
    sess.resources = {"주인공": {"member:namhang": 1, "항생제": 1}}
    sess.item_instances = {}
    sess.event_instances = {}
    acc = PW.pc_access_capabilities(sess, PLAYER_UID, D)
    assert "member:namhang" not in acc and "항생제" in acc                       # 교환 지급 토큰은 그대로
    sess.event_instances = {"i": _inst("남항 중공업 연대", phase="assigned")}
    assert "member:namhang" in PW.pc_access_capabilities(sess, PLAYER_UID, D)
