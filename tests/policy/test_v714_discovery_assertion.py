# -*- coding: utf-8 -*-
"""v7.14.0 — 역사칭·발견 선언(설계안 §7 (a)~(r)).

선언 게이트 = 기존 개연성 시뮬레이션 호출의 구조화 필드(새 provider 호출 0). 판단·지시층위에는 열거값과
플레이어 원문 대상명만 간다. DYNAMIC에서 승인·판정 성공한 주변 발견만 기존 정보·인지 typed 연산으로
관찰 → stage_world_ops → READY → CommitCoordinator를 거쳐 정본이 된다. 실패는 '이번에 찾지 못함'.
"""
from __future__ import annotations

import copy
import json

import pytest

import cogs.gm as gm_mod
import core
import prompts
from core import cognition as COG
from core import information as INFO
from core import spatial as SP
from core import turn_transaction as tt
from core import turn_world as TW
from tests.conftest import PLAYER_UID
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h05bc_turn_wiring import (  # noqa: F401 — 픽스처 재사용
    PC, H05Provider, drig, extraction, judgment, make_dynamic, turn)
from tests.policy.test_ready_barrier import _json, _usage, rig  # noqa: F401

pytestmark = pytest.mark.policy

SECRET = "비밀창고열쇠Z"            # 시뮬레이션 자유 서술에만 있는 숨은 설정 문구(누설 탐지용)


def sim(classification="ORDINARY", resolution="CONTINUE", reason="NONE", target="", kind="NONE", existing=""):
    return _json({"action_world_contact": f"접점 {SECRET}", "world_axioms": [f"[룰북] {SECRET}"],
                  "directions": [{"title": "t", "description": SECRET, "falsification_check": "x",
                                  "world_basis": SECRET, "plausibility": "high", "narrative_constraint": "",
                                  "primary_world_element": "e"}],
                  "declaration_gate": {"classification": classification, "resolution": resolution,
                                       "reason_code": reason, "discovery_target": target,
                                       "discovery_kind": kind, "existing_key": existing}})


def jdg(action="PROCEED", *, rolls=(), ambient=False, intent="선언한 시도"):
    return _json({"player_intent": intent, "situation_read": "상황", "action": action, "reasoning": "테스트",
                  "bridge_message": "", "rolls": list(rolls), "ambient_proposal": ambient, "self_check": "x",
                  "action_intent": {"movement_kind": "NONE", "time_kind": "NONE"}})


def droll(**kw):
    spec = {"kind": "GENERAL", "char_name": "테스터", "stat": "민첩", "difficulty": "보통", "method_mod": 0,
            "method_basis": "", "status_mod": 0, "status_basis": "", "discovery": True}
    spec.update(kw)
    return spec


@pytest.fixture
def grig(drig, monkeypatch):
    """DYNAMIC rig + 실제 시뮬레이션 경로(게이트 포함)."""
    r = drig
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions",
                        gm_mod.GMCog._simulate_narrative_directions.__get__(r.gm))
    r.make()
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["judgment"] = [jdg()]                     # 이동 없는 일반 행동
    return r


def ambient_ids(s):
    return sorted(i for i, d in ((s.information_state or {}).get("runtime_definitions") or {}).items()
                  if TW.AMBIENT_TAG in (d.get("tags") or ()))


def pc_cog(s):
    return (s.cognition_state or {}).get(COG.actor_holder(PC)) or {}


def validity(s, iid):
    return INFO.normalized_status(((s.information_state or {}).get("status") or {}).get(iid))["validity"]


def bag(s):
    return (s.resources or {}).get("테스터") or {}


async def roll_turn(r, monkeypatch, text, dice):
    """ROLL 대기까지 진행 → 버튼 대신 같은 실행·재개 경로를 직접 호출."""
    tx = await turn(r, text)
    assert tx.status == tt.TurnStatus.WAITING_FOR_ROLL
    monkeypatch.setattr(core.judgment, "roll_2d50", lambda _rng: dice)
    rolls = tx.judgment_result["judgment"]["rolls"]
    res = await r.gm._execute_rolls(r.sess, rolls, r.gch, transaction_id=tx.transaction_id)
    await r.gm._continue_with_roll_results(r.sess, text, res, transaction_id=tx.transaction_id)
    return tx


# ══════════════════════════════════════════════════════════════
# 역사칭 (a)(b)(c)
# ══════════════════════════════════════════════════════════════

def test_c_narration_rule_no_longer_accepts_preclaimed_result():
    assert "문이 열린 물리적 결과" not in prompts.SYSTEM_INSTRUCTION
    assert "선언은 시도입니다" in prompts.SYSTEM_INSTRUCTION
    assert "[선언 처리 — 코드 확정]" in prompts.GM_LOGIC_SYSTEM_INSTRUCTION
    assert "declaration_gate" in prompts.NARRATIVE_DIRECTION_SCHEMA["required"]


async def test_a_safe_attempt_continues_but_result_not_confirmed(grig):
    r = grig
    r.prov.routes["simulation"] = [sim("IMPROPER_ASSERTION", "CONTINUE")]
    r.prov.routes["judgment"] = [jdg(intent="경비의 칼을 빼앗으려 시도한다")]
    tx = await turn(r, "경비의 칼을 빼앗았다")
    assert tx.status == tt.TurnStatus.COMMITTED
    lp = r.prov.prompts["logic"][0][0]
    assert "권위 있는 시도(판단층위 확정): 경비의 칼을 빼앗으려 시도한다" in lp
    assert "이번 선언은 결과를 미리 정했다" in lp and "확정 사실이 아니다" in lp


async def test_b_npc_consent_preclaim_blocked_before_any_roll(grig):
    r = grig
    r.prov.routes["simulation"] = [sim("IMPROPER_ASSERTION", "BLOCK", "CANNOT_CONFIRM")]
    tx = await turn(r, "경비가 내게 문을 열어 주었다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    assert not {"judgment", "logic", "narration"} & set(r.prov.calls)      # 판단·굴림·묘사 없음
    sent = "\n".join(m.content or "" for m in r.gch.sent)
    assert "미리 정할 수 없습니다" in sent and SECRET not in sent
    assert tx.judgment_result["gate"]["resolution"] == "BLOCK"


# ══════════════════════════════════════════════════════════════
# 발견 (d)(e)(f)(g) · 비밀 보호 (h)(i)(j)
# ══════════════════════════════════════════════════════════════

async def test_d_g_trivial_discovery_no_roll_creates_one_info_and_pc_cognition(grig):
    r = grig
    s = r.sess
    know0, npc0 = copy.deepcopy(s.spatial_knowledge), {k: v for k, v in (s.cognition_state or {}).items()
                                                        if k != COG.actor_holder(PC)}
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target="나뭇조각", kind="OBJECT")]
    tx = await turn(r, "바닥에서 나뭇조각을 찾아본다")
    assert tx.status == tt.TurnStatus.COMMITTED
    assert not any("discovery" in (rr or {}) for rr in tx.judgment_result.get("roll_records") or ())
    ids = ambient_ids(s)
    assert ids == [TW.ambient_info_id(tx.transaction_id)]
    d = s.information_state["runtime_definitions"][ids[0]]
    assert d["subject_refs"] == ["place:항구"] and d["fragments"] == ["나뭇조각"] and "OBJECT" in d["tags"]
    assert pc_cog(s)[ids[0]]["awareness"] == COG.CONTENT
    # (g) NPC 인지 없음 · (j) 공간 지식·저작 InfoId 획득 없음
    assert {k: v for k, v in (s.cognition_state or {}).items() if k != COG.actor_holder(PC)} == npc0
    assert set(pc_cog(s)) == set(ids) and s.spatial_knowledge == know0
    assert bag(s).get("나뭇조각") is None                                 # 발견 ≠ 소지
    assert "판정 성공" not in r.prov.prompts["logic"][0][0] and "무판정 승인" in r.prov.prompts["logic"][0][0]


async def test_e_uncertain_discovery_rolls_and_success_persists(grig, monkeypatch):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]
    tx = await roll_turn(r, monkeypatch, "폐건물 구석에서 노끈을 찾아본다", (1, 1))
    assert tx.status == tt.TurnStatus.COMMITTED
    rec = tx.judgment_result["roll_records"][0]
    assert rec["discovery"] == {"target": "노끈", "kind": "OBJECT", "place": "항구", "success": True}
    assert ambient_ids(r.sess) == [TW.ambient_info_id(tx.transaction_id)]
    assert "판정 성공" in r.prov.prompts["logic"][-1][0]


async def test_f_failed_discovery_creates_nothing(grig, monkeypatch):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]
    tx = await roll_turn(r, monkeypatch, "폐건물 구석에서 노끈을 찾아본다", (50, 50))
    assert tx.status == tt.TurnStatus.COMMITTED
    assert tx.judgment_result["roll_records"][0]["discovery"]["success"] is False
    s = r.sess
    assert ambient_ids(s) == [] and pc_cog(s) == {} and bag(s).get("노끈") is None   # 부재 정보·인지·소지품 0
    lp = r.prov.prompts["logic"][-1][0]
    assert "찾지 못했다" in lp and "단정하지 말 것" in lp


async def test_other_roll_success_is_not_discovery_success(grig, monkeypatch):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll(stat="근력", discovery=False), droll()])]
    tx = await turn(r, "문을 밀면서 노끈을 찾아본다")
    seq = iter([(1, 1), (50, 50)])                                        # 일반 판정 성공 · 발견 판정 실패
    monkeypatch.setattr(core.judgment, "roll_2d50", lambda _rng: next(seq))
    res = await r.gm._execute_rolls(r.sess, tx.judgment_result["judgment"]["rolls"], r.gch,
                                    transaction_id=tx.transaction_id)
    await r.gm._continue_with_roll_results(r.sess, "x", res, transaction_id=tx.transaction_id)
    assert ambient_ids(r.sess) == []


async def test_gate_roll_without_discovery_roll_is_contract_violation(grig):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("PROCEED")]
    await turn(r, "노끈을 찾아본다")
    assert r.prov.calls.count("judgment") == gm_mod.JUDGMENT_MAX_RETRIES
    assert "logic" not in r.prov.calls and ambient_ids(r.sess) == []


async def test_h_secret_or_unique_proposal_blocked_with_neutral_notice(grig):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "BLOCK", "NOT_AMBIENT")]
    tx = await turn(r, "벽 뒤에서 비밀 통로를 찾는다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER and "judgment" not in r.prov.calls
    sent = "\n".join(m.content or "" for m in r.gch.sent)
    assert gm_mod._GATE_NOTICE_DEFAULT in sent and "비밀" not in gm_mod._GATE_NOTICE_DEFAULT
    assert ambient_ids(r.sess) == []


async def test_i_judgment_input_has_no_simulation_free_text(grig):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target="나뭇조각", kind="OBJECT")]
    await turn(r, "나뭇조각을 찾는다")
    jp = r.prov.prompts["judgment"][0][0]
    assert SECRET not in jp and "rt.ambient" not in jp
    assert "분류 AMBIENT_PROPOSAL / 처리 ACCEPT_NO_ROLL / 사유 NONE / 대상 나뭇조각(OBJECT)" in jp


def test_target_must_come_from_player_words():
    g = gm_mod._parse_gate({"classification": "AMBIENT_PROPOSAL", "resolution": "ROLL", "reason_code": "NONE",
                            "discovery_target": SECRET, "discovery_kind": "OBJECT", "existing_key": ""},
                           "노끈을 찾는다", dynamic=True, place="항구")
    assert g["resolution"] == "CLARIFY" and g["target"] == ""
    assert gm_mod._parse_gate({"classification": "X"}, "x", dynamic=True) is None


def test_discovery_roll_contract():
    assert core.judgment.validate_rolls([droll(kind="CONTEST", opponent={})])  # GENERAL만
    assert "discovery 굴림은 한 턴에 최대 1개" in core.judgment.validate_rolls([droll(), droll()])
    assert core.judgment.validate_rolls([droll()]) == []


# ══════════════════════════════════════════════════════════════
# 지속·획득 (k)(l)(m)
# ══════════════════════════════════════════════════════════════

async def _found(r, target="노끈", kind="OBJECT", text=None):
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target=target, kind=kind)]
    tx = await turn(r, text or f"주변에서 {target}을 찾아본다")
    assert tx.status == tt.TurnStatus.COMMITTED
    return ambient_ids(r.sess)[-1]


async def test_k_discovery_persists_next_turn_and_beyond_recent_12(grig):
    r = grig
    s = r.sess
    iid = await _found(r)
    for n in range(13):                                    # 최근 인지 12건을 넘기는 다른 정보
        rid = f"rt.other.{n}"
        s.information_state["runtime_definitions"][rid] = INFO.runtime_definition_record(
            rid, f"명제 {n}", summary=f"요약 {n}", created_at_minute=0)
        s.information_state["status"][rid] = {"validity": "CURRENT", "successor_ids": [], "predecessor_ids": []}
        s.cognition_state[COG.actor_holder(PC)][rid] = copy.deepcopy(pc_cog(s)[iid])
    r.prov.routes["simulation"] = [sim()]
    await turn(r, "주변을 둘러본다")
    lp = r.prov.prompts["logic"][-1][0]
    assert "[현재 장소에서 PC가 발견해 둔 것" in lp and "· 노끈 (물건)" in lp
    sp = r.prov.prompts["simulation"][-1][0]
    assert "D1=노끈(물건)" in sp                             # 게이트 입력(existing_key 재사용용)


async def test_l_take_supersedes_and_adds_one_item_once(grig):
    r = grig
    s = r.sess
    iid = await _found(r)
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]},
                                              item_changes=[{"target": "테스터", "item": "노끈", "delta": 1}])]
    await turn(r, "아까 찾은 노끈을 집어 든다")
    assert "D1=노끈(물건)" in r.prov.prompts["extraction"][-1][0]
    assert validity(s, iid) == INFO.SUPERSEDED and bag(s)["노끈"] == 1     # 추출의 같은 +1과 중복 없음
    # 같은 곳 중복 획득·표시 0
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]})]
    await turn(r, "노끈을 또 집는다")
    assert "D1=" not in r.prov.prompts["extraction"][-1][0] and bag(s)["노끈"] == 1
    assert "[현재 장소에서 PC가 발견해 둔 것" not in r.prov.prompts["logic"][-1][0]


async def test_m_find_and_take_same_turn_is_atomic(grig):
    r = grig
    s = r.sess
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target="나뭇조각", kind="OBJECT")]
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["DNEW"]})]
    tx = await turn(r, "나뭇조각을 찾아서 줍는다")
    assert tx.status == tt.TurnStatus.COMMITTED
    iid = TW.ambient_info_id(tx.transaction_id)
    assert ambient_ids(s) == [iid] and validity(s, iid) == INFO.SUPERSEDED and bag(s)["나뭇조각"] == 1


async def test_environment_cannot_be_taken(grig):
    r = grig
    iid = await _found(r, "벽 틈", "ENVIRONMENT", "벽 틈을 찾아본다")
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]})]
    await turn(r, "벽 틈을 집는다")
    assert validity(r.sess, iid) == INFO.CURRENT and "벽 틈" not in bag(r.sess)


async def test_existing_target_reused_not_duplicated(grig):
    r = grig
    await _found(r)
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT", existing="D1")]
    tx = await turn(r, "노끈을 다시 찾아본다")
    assert tx.judgment_result["gate"]["resolution"] == "ACCEPT_NO_ROLL"      # 재굴림 없음
    assert len(ambient_ids(r.sess)) == 1


# ══════════════════════════════════════════════════════════════
# 재시도·캐시 (n)(o)
# ══════════════════════════════════════════════════════════════

async def test_n_failed_search_reaches_gate_and_unchanged_retry_blocks(grig, monkeypatch):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]
    await roll_turn(r, monkeypatch, "폐건물 구석에서 노끈을 찾아본다", (50, 50))
    n_judg = r.prov.calls.count("judgment")
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "BLOCK", "UNCHANGED_RETRY")]
    tx = await turn(r, "폐건물 구석에서 노끈을 다시 찾아본다")
    sp = r.prov.prompts["simulation"][-1][0]
    assert "[이 장소에서 찾지 못한 이전 탐색" in sp and "· 노끈 — 당시 선언: 폐건물 구석에서 노끈을 찾아본다" in sp
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER and r.prov.calls.count("judgment") == n_judg
    assert ambient_ids(r.sess) == []                                     # 부재 확정 없음
    # 조건이 바뀌면(게이트 ROLL) 다시 굴릴 수 있다
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]
    tx2 = await turn(r, "손전등을 비추며 노끈을 찾아본다")
    assert tx2.status == tt.TurnStatus.WAITING_FOR_ROLL


@pytest.mark.parametrize("j", [jdg(ambient=True), jdg("ROLL", rolls=[droll()])])
async def test_o_cache_unusable_discovery_is_unverifiable_not_failed(drig, j):
    r = drig                                    # 시뮬레이션 없음(게이트 없음)
    r.make()
    r.prov.routes["judgment"] = [j]
    tx = await turn(r, "노끈을 찾아본다")
    assert tx.status == tt.TurnStatus.WAITING_FOR_PLAYER
    assert not {"logic", "narration"} & set(r.prov.calls) and not tx.judgment_result.get("roll_records")
    assert gm_mod.GATE_UNVERIFIABLE_NOTICE in "\n".join(m.content or "" for m in r.gch.sent)
    assert ambient_ids(r.sess) == [] and pc_cog(r.sess) == {}


# ══════════════════════════════════════════════════════════════
# 회귀 (p)(q)(r)
# ══════════════════════════════════════════════════════════════

async def test_p_legacy_never_persists_discovery_but_still_blocks(drig, monkeypatch):
    r = drig
    monkeypatch.setattr(r.gm, "_simulate_narrative_directions",
                        gm_mod.GMCog._simulate_narrative_directions.__get__(r.gm))
    r.sess.definition_revision = None                                     # LEGACY
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="노끈", kind="OBJECT")]
    r.prov.routes["judgment"] = [_json({"player_intent": "x", "situation_read": "x", "action": "PROCEED",
                                        "reasoning": "x", "bridge_message": "", "rolls": [],
                                        "ambient_proposal": True})]
    tx = await turn(r, "노끈을 찾아본다")
    assert tx.status == tt.TurnStatus.COMMITTED and tx.judgment_result["gate"]["classification"] == "ORDINARY"
    assert not (r.sess.information_state or {}).get("runtime_definitions")
    assert "[이 장소에서 PC가 이미 발견한 것]" not in r.prov.prompts["simulation"][0][0]
    r.prov.routes["simulation"] = [sim("IMPROPER_ASSERTION", "BLOCK", "CANNOT_CONFIRM")]
    tx2 = await turn(r, "경비가 문을 열어 주었다")
    assert tx2.status == tt.TurnStatus.WAITING_FOR_PLAYER


async def test_q_ordinary_turn_and_ordinary_roll_unchanged(grig, monkeypatch):
    r = grig
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll(discovery=False)])]
    tx = await roll_turn(r, monkeypatch, "문을 민다", (1, 1))
    assert tx.status == tt.TurnStatus.COMMITTED
    assert "discovery" not in tx.judgment_result["roll_records"][0] and ambient_ids(r.sess) == []


async def test_q_stray_discovery_flag_without_gate_roll_is_stripped(grig, monkeypatch):
    r = grig
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]           # 게이트 ORDINARY
    tx = await roll_turn(r, monkeypatch, "문을 민다", (1, 1))
    assert "discovery" not in tx.judgment_result["roll_records"][0] and ambient_ids(r.sess) == []


def test_r_observation_is_deterministic_and_never_duplicates(drig):
    """RETRY_PENDING 재추출·같은 버튼 재처리: 같은 tx → 같은 연산 id, 이미 있는 발견은 다시 만들지 않는다."""
    s = drig.sess
    D = make_dynamic(s)
    ctx = core.player_world.session_world_context(s)
    disc = {"status": "NEW", "target": "노끈", "kind": "OBJECT", "place": "항구", "place_name": "항구",
            "info_id": TW.ambient_info_id("tx-1")}
    man = TW.build_observation_manifest(D, ctx, None, s, discovery=disc)
    a = TW.process_observation(ctx, s, None, man, {"discovery_taken": ["DNEW"]}, transaction_id="tx-1",
                               narrative_seed="n", discovery=disc)
    b = TW.process_observation(ctx, s, None, man, {"discovery_taken": ["DNEW"]}, transaction_id="tx-1",
                               narrative_seed="n", discovery=disc)
    assert a.status == TW.TW_READY and [o.op_id for o in a.ops] == [o.op_id for o in b.ops]
    assert a.item_gains == ("노끈",)
    world = a.projected_world
    st = world.domain("information_state")
    ops, _g, diags = TW.discovery_ops(ctx, world, man, {}, disc, prefix="p", transaction_id="tx-1", at_minute=0)
    assert ops == [] and diags[0]["code"] == "DISCOVERY_ALREADY_EXISTS"
    assert INFO.normalized_status(st["status"][disc["info_id"]])["validity"] == INFO.SUPERSEDED


async def test_r_rerender_and_rewind_keep_world_and_items_consistent(grig):
    r = grig
    s = r.sess
    await _found(r, "나뭇조각")
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]})]
    await turn(r, "나뭇조각을 줍는다")
    assert bag(s)["나뭇조각"] == 1
    # 재서술: 턴 이전 상태로 복원 후 다시 — 이중 획득·유령 발견 없음
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]})]
    ok, reason = await r.gm.rerender_latest(s)
    assert ok, reason
    assert bag(s)["나뭇조각"] == 1 and len(ambient_ids(s)) == 1
    assert validity(s, ambient_ids(s)[0]) == INFO.SUPERSEDED
    # 되감기: 수거 이전 — 소지품·유효성이 함께 돌아간다
    res = await r.gm.history_rewind(s, 1)
    assert res
    assert bag(s).get("나뭇조각") is None and validity(s, ambient_ids(s)[0]) == INFO.CURRENT


# ══════════════════════════════════════════════════════════════
# A1 보정 — 추출 D키 누락의 엄격한 조건부 복구(이름만으로는 연결하지 않는다)
# ══════════════════════════════════════════════════════════════

PICK = {"target": "테스터", "item": "쇠파이프", "delta": 1}


async def _pipe(r):
    iid = await _found(r, "쇠파이프", text="주변에서 쇠파이프를 찾아본다")
    return iid


async def _take(r, *, gate=None, items=(PICK,), obs=None, text="아까 발견한 쇠파이프를 집어 든다"):
    r.prov.routes["simulation"] = [gate or sim(existing="D1")]
    r.prov.routes["extraction"] = [extraction(obs or {}, item_changes=list(items))]
    return await turn(r, text)


async def test_a1_missing_key_unique_referenced_find_is_taken_once(grig):
    r = grig
    s = r.sess
    iid = await _pipe(r)
    tx = await _take(r)                                   # 게이트 ORDINARY + existing_key D1, 추출은 item_changes만
    assert tx.judgment_result["gate"]["referenced_info_id"] == iid
    assert validity(s, iid) == INFO.SUPERSEDED and bag(s)["쇠파이프"] == 1
    # 후속 턴 재노출·재수거 없음
    r.prov.routes["simulation"] = [sim()]
    r.prov.routes["extraction"] = [extraction({"discovery_taken": ["D1"]})]
    await turn(r, "쇠파이프를 또 집는다")
    assert "D1=" not in r.prov.prompts["extraction"][-1][0] and bag(s)["쇠파이프"] == 1
    assert "현장 발견물(D키)을 PC가 실제로 획득했다면" in prompts.EXTRACTION_SYSTEM_INSTRUCTION


async def test_a1_missing_key_same_turn_new_find_is_taken_once(grig):
    r = grig
    s = r.sess
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ACCEPT_NO_ROLL", target="쇠파이프", kind="OBJECT")]
    r.prov.routes["extraction"] = [extraction({}, item_changes=[PICK])]
    tx = await turn(r, "쓸 만한 쇠파이프를 찾아 집어 든다")
    iid = TW.ambient_info_id(tx.transaction_id)
    assert validity(s, iid) == INFO.SUPERSEDED and bag(s)["쇠파이프"] == 1


async def test_a1_failed_find_with_item_change_does_not_link(grig, monkeypatch):
    r = grig
    r.prov.routes["simulation"] = [sim("AMBIENT_PROPOSAL", "ROLL", target="쇠파이프", kind="OBJECT")]
    r.prov.routes["judgment"] = [jdg("ROLL", rolls=[droll()])]
    r.prov.routes["extraction"] = [extraction({}, item_changes=[PICK])]
    await roll_turn(r, monkeypatch, "쇠파이프를 찾아 집어 든다", (50, 50))
    assert ambient_ids(r.sess) == []                      # 실패한 발견은 만들지도 연결하지도 않는다


@pytest.mark.parametrize("case", ["no_reference", "other_source_npc", "not_pc", "delta_2", "already_held",
                                  "explicit_gone"])
async def test_a1_ambiguous_cases_do_not_link_by_name(grig, case):
    r = grig
    s = r.sess
    iid = await _pipe(r)
    kw = {}
    if case == "no_reference":
        kw["gate"] = sim()                                # 선언이 발견물을 가리킨다는 구조화 근거 없음
    elif case == "other_source_npc":
        kw["items"] = (PICK, {"target": "강도", "item": "쇠파이프", "delta": -1})   # NPC에게서 받은 정황
        s.npcs = {**(s.npcs or {}), "강도": {}}
    elif case == "not_pc":
        s.npcs = {**(s.npcs or {}), "강도": {}}
        kw["items"] = ({"target": "강도", "item": "쇠파이프", "delta": 1},)
    elif case == "delta_2":
        kw["items"] = ({**PICK, "delta": 2},)
    elif case == "already_held":
        s.resources = {**(s.resources or {}), "테스터": {"쇠파이프": 1}}
    elif case == "explicit_gone":
        kw["obs"] = {"discovery_gone": ["D1"]}
    await _take(r, **kw)
    if case == "explicit_gone":
        assert validity(s, iid) == INFO.SUPERSEDED        # 명시 키가 우선(기존 처리)
    else:
        assert validity(s, iid) == INFO.CURRENT           # 자동 소멸·연결 없음


def test_a1_helper_rejects_duplicate_names_wrong_row_and_environment():
    man = TW.ObservationManifest(discoveries=(("D1", "i1", "쇠파이프", "OBJECT"), ("D2", "i2", "쇠파이프", "OBJECT"),
                                              ("D3", "i3", "벽 틈", "ENVIRONMENT")))
    deltas = [PICK]
    assert TW.implied_discovery_take(man, {}, "D1", deltas, "테스터", {}) is None          # 동명 후보 복수
    assert TW.implied_discovery_take(man, {}, "D9", deltas, "테스터", {}) is None          # 매니페스트 밖(다른 장소·무효)
    assert TW.implied_discovery_take(man, {}, "D3", [{**PICK, "item": "벽 틈"}], "테스터", {}) is None
    one = TW.ObservationManifest(discoveries=(("D1", "i1", "쇠파이프", "OBJECT"),))
    assert TW.implied_discovery_take(one, {}, "D1", deltas, "테스터", {}) == "D1"
    assert TW.implied_discovery_take(one, {}, None, deltas, "테스터", {}) is None          # 근거 없음
    assert TW.implied_discovery_take(one, {"discovery_taken": ["D1"]}, "D1", deltas, "테스터", {}) is None
