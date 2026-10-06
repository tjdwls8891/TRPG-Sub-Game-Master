# -*- coding: utf-8 -*-
"""H04-A — ActorRegistry · Information/Series · Cognition · Relationship · CurrentNpcProjection · compiler v3.

H04-R06  actor profile_base — legacy_source 불필요
H04-R07  ActorId 개명 정체성 유지
H04-R08  명시 정보 registry + 충돌 검증
H04-R09  인라인 비밀 info_ref
H04-R10  status 없는 저작 정보 = CURRENT
H04-R11  런타임 정보 명제 불변
H04-R12  SUPERSEDED/REFUTED 계보
H04-R13  계열 A→B→A 새 revision InfoId
H04-R14  옛 인지는 옛 InfoId에 남는다
H04-R15  기록 없음 = 미인지
H04-R16  노출이 믿음을 자동 설정하지 않음
H04-R17  PC 믿음은 PLAYER_EXPLICIT·RULE_FORCED만
H04-R18  획득 provenance 안정 id·멱등
H04-R19  group 인지 구성원 자동 전파 없음
H04-R20  관계 희소 append·멱등
H04-R21  CurrentNpcProjection 정본 저장 없음
H04-R22  장소 연관 ≠ 현재 위치
"""
from __future__ import annotations

import copy

import pytest

import core
from core import actors as AR
from core import cognition as CG
from core import definition_compiler as dc
from core import information as IN
from core import npc_projection as NP
from core import relationships as RL
from core import turn_preparation as TP
from core import turn_transaction as tt
from core import world_clock as WC
from core import world_mutation as WM
from core import world_state as WS
from core.world_ids import authored_npc_actor_id, npc_inline_secret_info_id
from tests.conftest import PLAYER_UID
from tests.fakes.spatial_fixture import spatial_raw
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _disk, _fund

pytestmark = pytest.mark.policy

K = 1440 * 1000
KANG = authored_npc_actor_id("t", "강지수")
PARK = authored_npc_actor_id("t", "박노인")
PC = "pc_" + str(PLAYER_UID)


def info_raw(**patch):
    raw = spatial_raw()
    raw["scenario_schema"]["capabilities"].update(
        {"stable_actors": 1, "information_registry": 1, "actor_cognition": 1, "dynamic_actor_location": 1})
    raw["default_npcs"] = {
        "강지수": {"name": "강지수", "비밀": {"info_ref": "S40"}, "role": "관리인",
                   "resources": {"hp": 3}, "statuses": ["피로"], "actor_id": None},
        "박노인": {"비밀": "창고 열쇠를 성당 제단 밑에 숨겼다", "role": "어부"},
    }
    del raw["default_npcs"]["강지수"]["actor_id"]
    raw["places"]["시장"]["npcs"] = [{"name": "강지수"}]
    raw["information_definitions"] = {
        "S40": {"proposition": "원본 기록은 성당 제3보관실에 있다", "summary": "원본 기록 위치",
                "subject_refs": [f"actor:{KANG}", "place:성당"], "tags": ["기록", "비밀"],
                "fragments": [{"fragment_id": "f_room", "proposition": "제3보관실"},
                              {"fragment_id": "f_church", "proposition": "성당"}]},
    }
    raw["information_series"] = {
        "kang_loc": {"value_ref": {"ref": "ACTOR_PLACE", "actor": KANG},
                     "proposition_template": "강지수는 {value}에 있다", "subject_refs": [f"actor:{KANG}"]}}
    raw.update(copy.deepcopy(patch))
    return raw


def _defn(raw=None):
    res = dc.compile_scenario(raw or info_raw(), "t")
    assert res.ok, [(d.code, d.path, d.message) for d in res.errors]
    return res.definition


def _codes(res, sev=None):
    return [d.code for d in res.diagnostics if sev is None or d.severity == sev]


def _sess(sid="s-h04a"):
    return core.TRPGSession(sid, 1, 2, "x", {})


def _project(world, *ops):
    src = world if isinstance(world, WM.ProjectedWorld) else WM.ProjectedWorld.capture(world)
    b = WM.WorldMutationBatch(batch_id="b", outer_origin=WS.CAUSE_ADMIN_EDIT, source_world_version_id=None,
                              definition_revision=None)
    for op in ops:
        b.add(op)
    b.freeze()
    return WM.project(src, b, WM.PRODUCTION_REGISTRY)


def _ok(world, *ops):
    r = _project(world, *ops)
    assert r.ok, r.conflicts
    return r.world


def _op(i, t, p, **kw):
    return WM.make_op(i, t, "TEST", p, **kw)


def _acq(aid, route="TESTIMONY", **kw):
    return CG.acquisition_record(aid, route, **kw)


# ══════════════════════════════════════════════════════════════
# compiler v3
# ══════════════════════════════════════════════════════════════

def test_compiler_v3_actor_profile_base_and_info_registry():
    res = dc.compile_scenario(info_raw(), "t")
    assert res.ok and res.compiler_version == dc.COMPILER_VERSION == 3
    for cap in ("information_registry", "actor_cognition", "dynamic_actor_location"):
        assert cap in dc.VALIDATED_CAPABILITIES
    assert "information_definitions" not in dc.RESERVED_UNCOMPILED_KEYS
    D = res.definition
    kang = D["actor_definitions"][KANG]
    assert kang["profile_base"] == {"role": "관리인"}                # 정체성·비밀·런타임 초기값 제외
    assert kang["secret_info_id"] == "S40"                          # info_ref — 생성 정의 없음
    park = D["actor_definitions"][PARK]
    assert park["secret_info_id"] == npc_inline_secret_info_id(PARK)
    infos = D["information_definitions"]
    assert set(infos) == {"S40", npc_inline_secret_info_id(PARK)}
    s40 = infos["S40"]
    assert s40["kind"] == "EXPLICIT" and s40["tags"] == ["기록", "비밀"] and len(s40["fragments"]) == 2
    assert infos[npc_inline_secret_info_id(PARK)]["subject_refs"] == [f"actor:{PARK}"]
    for rec in infos.values():
        assert {"info_id", "kind", "proposition", "summary", "subject_refs", "tags", "fragments",
                "sensitivity", "source_ref"} <= set(rec)
    s = D["information_series"]["kang_loc"]
    assert s["dependency_keys"] == [f"actor:{KANG}:placement"] and s["tracking_policy"] == "ON_CHANGE"


@pytest.mark.parametrize("mutate, code", [
    (lambda r: r["default_npcs"]["강지수"].update({"비밀": {"info_ref": "NOPE"}}), "NPC_SECRET_REF_UNKNOWN"),
    (lambda r: r["default_npcs"]["강지수"].update({"비밀": {"ref": "S40"}}), "NPC_SECRET_REF_INVALID"),
    (lambda r: r["information_definitions"].update({npc_inline_secret_info_id(PARK): {"proposition": "x"}}),
     "INFO_ID_COLLISION"),
    (lambda r: r["information_definitions"]["S40"].update({"subject_refs": ["actor:ghost"]}), "INFO_SUBJECT_REF_UNKNOWN"),
    (lambda r: r["information_definitions"]["S40"].update({"proposition": ""}), "INFO_PROPOSITION_REQUIRED"),
    (lambda r: r["information_definitions"]["S40"]["fragments"].append(
        {"fragment_id": "f_room", "proposition": "중복"}), "INFO_FRAGMENT_DUPLICATE"),
    (lambda r: r["information_definitions"].update({"한글id": {"proposition": "x"}}), "INFO_ID_INVALID"),
    (lambda r: r["information_series"]["kang_loc"].update({"proposition_template": "값 없음"}),
     "INFO_SERIES_TEMPLATE_INVALID"),
    (lambda r: r["information_series"]["kang_loc"].update({"value_ref": {"ref": "ACTOR_PLACE", "actor": "ghost"}}),
     "INFO_SERIES_VALUE_REF_INVALID"),
    (lambda r: r["information_series"]["kang_loc"].update({"value_ref": "강지수.location"}),
     "INFO_SERIES_VALUE_REF_INVALID"),
    (lambda r: r["information_series"]["kang_loc"].update({"value_ref": {"ref": "CLOCK_MINUTE"}}),
     "INFO_SERIES_VALUE_REF_INVALID"),
])
def test_info_compiler_errors(mutate, code):
    raw = info_raw()
    mutate(raw)
    res = dc.compile_scenario(raw, "t")
    assert not res.ok and code in _codes(res, "ERROR"), _codes(res)


def test_duplicate_proposition_is_warned_not_merged():
    raw = info_raw()
    raw["information_definitions"]["S99"] = {"proposition": "창고 열쇠를 성당 제단 밑에 숨겼다"}
    res = dc.compile_scenario(raw, "t")
    assert res.ok and "INFO_DUPLICATE_PROPOSITION" in _codes(res, "WARNING")
    assert {"S99", npc_inline_secret_info_id(PARK)} <= set(res.definition["information_definitions"])


# ══════════════════════════════════════════════════════════════
# ActorRegistry
# ══════════════════════════════════════════════════════════════

def test_actor_register_rename_preserves_identity_and_resolver():
    D = _defn()
    s = _sess()
    sid = AR.session_npc_actor_id(s.narrative_seed, "떠돌이")
    assert sid == AR.session_npc_actor_id(s.narrative_seed, "떠돌이") and sid.startswith("npc_s")
    w = _ok(s, _op("pc", AR.OP_ACTOR_REGISTER, {"actor_id": PC, "kind": "PC", "display_name": "주인공",
                                                "source_kind": "PLAYER", "source_key": str(PLAYER_UID)}),
            _op("n", AR.OP_ACTOR_REGISTER, {"actor_id": sid, "kind": "NPC", "display_name": "떠돌이",
                                             "source_kind": "SESSION_NPC", "source_key": "떠돌이"}))
    assert AR.validate_registry(w.domain("actor_registry")) == []
    w = _ok(w, _op("r", AR.OP_ACTOR_RENAME, {"actor_id": sid, "display_name": "한영수"}))
    w = _ok(w, _op("r", AR.OP_ACTOR_RENAME, {"actor_id": sid, "display_name": "영수"}))
    e = w.domain("actor_registry")[sid]
    assert e["display_name"] == "영수" and e["aliases"] == ["떠돌이", "한영수"]
    assert AR.resolve_actor_id(D, w, "한영수") == AR.ActorResolution(sid, "registry_alias", (sid,))
    assert AR.resolve_actor_id(D, w, "영수").actor_id == sid
    assert AR.resolve_actor_id(D, w, "강지수").actor_id == KANG            # 저작 사전 키
    assert AR.resolve_actor_id(D, w, KANG).matched_by == "actor_id"
    assert AR.resolve_actor_id(D, w, "없는사람").actor_id is None
    assert AR.legacy_name_of(D, w, KANG) == "강지수" and AR.legacy_name_of(D, w, sid) == "떠돌이"
    # 중복 등록·잘못된 PC id·미등록 개명 거부
    for bad in (_op("x", AR.OP_ACTOR_REGISTER, {"actor_id": sid, "kind": "NPC", "display_name": "a",
                                                "source_kind": "SESSION_NPC", "source_key": None}),
                _op("x", AR.OP_ACTOR_REGISTER, {"actor_id": "npc_x", "kind": "PC", "display_name": "a",
                                                "source_kind": "PLAYER", "source_key": None}),
                _op("x", AR.OP_ACTOR_RENAME, {"actor_id": "npc_none", "display_name": "a"})):
        assert _project(w, bad).conflicts[0].code == "HANDLER_REJECTED"


def test_resolver_reports_ambiguity_instead_of_guessing():
    D = _defn()
    s = _sess()
    w = _ok(s, *[_op(f"n{i}", AR.OP_ACTOR_REGISTER, {"actor_id": f"npc_dup{i}", "kind": "NPC",
                                                     "display_name": "김씨", "source_kind": "SESSION_NPC",
                                                     "source_key": None}) for i in (1, 2)])
    r = AR.resolve_actor_id(D, w, "김씨")
    assert r.actor_id is None and r.candidates == ("npc_dup1", "npc_dup2")


# ══════════════════════════════════════════════════════════════
# Information
# ══════════════════════════════════════════════════════════════

def test_authored_info_defaults_current_and_runtime_is_immutable():
    D = _defn()
    s = _sess()
    s.world_clock = WC.initial_clock(K)
    assert IN.effective_validity(D, {}, "S40") == IN.CURRENT
    assert IN.effective_validity(D, {}, "NOPE") is None
    rec = IN.runtime_definition_record("info_rt1", "문이 부서졌다", created_at_minute=K)
    w = _ok(s, _op("c", IN.OP_INFORMATION_CREATE, {"definition": rec}))
    st = w.domain("information_state")
    assert IN.effective_validity(D, st, "info_rt1") == IN.CURRENT
    assert IN.validate_state(D, st) == []
    again = IN.runtime_definition_record("info_rt1", "문이 멀쩡하다")
    assert _project(w, _op("c2", IN.OP_INFORMATION_CREATE, {"definition": again})).conflicts
    assert not any("UPDATE" in t for t in IN.INFORMATION_OP_TYPES)   # 명제 갱신 연산이 없다


def test_supersede_refute_lineage_and_no_revival():
    D = _defn()
    s = _sess()
    s.world_clock = WC.initial_clock(K)
    new = IN.runtime_definition_record("S41", "원본 기록은 제1보관실로 옮겨졌다", derived_from=["S40"])
    w = _ok(s, *IN.build_supersede_ops("S40", new, op_prefix="sup", source_kind="TEST"))
    st = w.domain("information_state")
    assert IN.effective_validity(D, st, "S40") == IN.SUPERSEDED
    assert IN.effective_validity(D, st, "S41") == IN.CURRENT
    assert IN.successors(st, "S40") == ["S41"] and IN.predecessors(st, "S41") == ["S40"]
    assert IN.lineage_forward(st, "S40") == ["S40", "S41"]
    assert IN.validate_state(D, st) == []
    assert D["information_definitions"]["S40"]["proposition"] == "원본 기록은 성당 제3보관실에 있다"
    w = _ok(w, _op("rf", IN.OP_INFORMATION_VALIDITY_SET, {"info_id": "S40", "validity": "REFUTED"}))
    for bad in (_op("v", IN.OP_INFORMATION_VALIDITY_SET, {"info_id": "S40", "validity": "CURRENT"}),
                _op("l", IN.OP_INFORMATION_LINK, {"predecessor": "S41", "successor": "S40"}),
                _op("l", IN.OP_INFORMATION_LINK, {"predecessor": "S41", "successor": "S41"})):
        assert _project(w, bad).conflicts[0].code == "HANDLER_REJECTED"


def test_series_a_b_a_mints_new_revision_ids_and_keeps_old_cognition():
    D = _defn()
    s = _sess()
    s.world_clock = WC.initial_clock(K)
    s.spatial_state = {"placements": {KANG: {"kind": "AT_PLACE", "place_id": "시장"}}}
    ops, diag = IN.build_series_init_ops(D, s, "kang_loc", op_prefix="si")
    assert diag is None
    w = _ok(s, *ops)
    st = w.domain("information_state")["series"]["kang_loc"]
    i0 = st["current_info_id"]
    assert st["revision_ordinal"] == 0
    assert w.domain("information_state")["runtime_definitions"][i0]["proposition"] == "강지수는 시장에 있다"
    # NPC가 rev0 정보를 획득
    w = _ok(w, _op("a", CG.OP_COGNITION_ACQUIRE, {"holder": f"actor:{PARK}", "info_id": i0,
                                                  "awareness": "CONTENT", "acquisition": _acq("acq-1")}))
    ids = [i0]
    for n, place in enumerate(("성당", "시장"), 1):                     # A → B → A
        w = _ok(w, _op(f"mv{n}", "ACTOR_PLACEMENT_SET", {"actor_id": KANG, "place_id": place}))
        ops, diag = IN.reevaluate_series(D, w, "kang_loc", op_prefix=f"re{n}")
        assert diag is None and ops
        w = _ok(w, *ops)
        ids.append(w.domain("information_state")["series"]["kang_loc"]["current_info_id"])
    assert len(set(ids)) == 3                                            # 같은 값으로 돌아와도 새 InfoId
    st = w.domain("information_state")
    assert [IN.effective_validity(D, st, i) for i in ids] == ["SUPERSEDED", "SUPERSEDED", "CURRENT"]
    assert IN.lineage_forward(st, ids[0]) == ids
    assert st["series"]["kang_loc"]["revision_ordinal"] == 2
    assert set(w.domain("cognition_state")[f"actor:{PARK}"]) == {i0}   # 옛 인지는 옛 InfoId에
    # 같은 값이면 no-op, 미초기화·UNKNOWN 값은 바꾸지 않는다
    assert IN.reevaluate_series(D, w, "kang_loc", op_prefix="z") == ([], None)
    assert IN.reevaluate_series(D, s, "kang_loc", op_prefix="z") == ([], "SERIES_UNINITIALIZED")
    w2 = WM.ProjectedWorld(dict(w.to_dict(), spatial_state={}))
    assert IN.reevaluate_series(D, w2, "kang_loc", op_prefix="z") == ([], "SERIES_VALUE_UNKNOWN")
    assert IN.validate_state(D, st) == []


# ══════════════════════════════════════════════════════════════
# Cognition
# ══════════════════════════════════════════════════════════════

def test_cognition_sparse_acquisition_idempotent_and_no_auto_belief():
    s = _sess()
    h = f"actor:{KANG}"
    assert CG.record_of(s, h, "S40") is None and CG.awareness_of(s, h, "S40") == "NONE"
    w = _ok(s, _op("a", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "EXISTENCE",
                                                  "acquisition": _acq("ev:1", "RUMOR")}))
    rec = CG.record_of(w, h, "S40")
    assert rec["awareness"] == "EXISTENCE" and rec["belief"] is None
    w = _ok(w, _op("b", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "CONTENT",
                                                  "acquisition": _acq("ev:2", "DOCUMENT", fragments=["f_room"])}))
    rec = CG.record_of(w, h, "S40")
    assert rec["awareness"] == "CONTENT" and rec["belief"] is None      # 노출 ≠ 믿음
    assert [a["acquisition_id"] for a in rec["acquisitions"]] == ["ev:1", "ev:2"]
    assert rec["fragments"] == ["f_room"]
    # 같은 원인 재적용 = 멱등, 같은 id 다른 내용 = 거부, 낮은 인지 획득은 하향하지 않음
    r = _project(w, _op("b2", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "CONTENT",
                                                        "acquisition": _acq("ev:2", "DOCUMENT", fragments=["f_room"])}))
    assert r.ok and r.world.digest() == w.digest()
    assert _project(w, _op("b3", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "CONTENT",
                                                           "acquisition": _acq("ev:2", "RUMOR")})).conflicts
    w = _ok(w, _op("c", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "EXISTENCE",
                                                  "acquisition": _acq("ev:3", "RUMOR")}))
    assert CG.record_of(w, h, "S40")["awareness"] == "CONTENT"
    assert len(CG.record_of(w, h, "S40")["acquisitions"]) == 3          # provenance 보존(cap 없음)
    assert CG.validate_state(w.domain("cognition_state")) == []
    # 하향은 cause_ref 필요
    assert _project(w, _op("d", CG.OP_COGNITION_UPSERT, {"holder": h, "info_id": "S40",
                                                         "fields": {"awareness": "EXISTENCE"}})).conflicts
    assert _project(w, _op("d", CG.OP_COGNITION_UPSERT, {"holder": h, "info_id": "S40",
                                                         "fields": {"awareness": "EXISTENCE"},
                                                         "cause_ref": "rule:amnesia"})).ok


def test_pc_belief_authority_and_no_truth_correction():
    D = _defn()
    s = _sess()
    pc, npc = f"actor:{PC}", f"actor:{KANG}"
    w = _ok(s, *[_op(f"a{n}", CG.OP_COGNITION_ACQUIRE, {"holder": h, "info_id": "S40", "awareness": "CONTENT",
                                                        "acquisition": _acq(f"x{n}")}) for n, h in enumerate((pc, npc))])
    for auth in ("RULE", "AUTHORED"):
        r = _project(w, _op("pb", CG.OP_COGNITION_BELIEF_SET, {"holder": pc, "info_id": "S40", "belief": "CERTAIN",
                                                               "authority": auth, "cause_ref": "event:e1"}))
        assert r.conflicts and "PC 믿음" in r.conflicts[0].message
    w = _ok(w, _op("pb", CG.OP_COGNITION_BELIEF_SET, {"holder": pc, "info_id": "S40", "belief": "SUSPECTED",
                                                      "authority": "PLAYER_EXPLICIT", "cause_ref": "turn:1"}))
    w = _ok(w, _op("pr", CG.OP_COGNITION_BELIEF_SET, {"holder": pc, "info_id": "S40", "belief": "CERTAIN",
                                                      "authority": "RULE_FORCED", "cause_ref": "rule:oath"}))
    w = _ok(w, _op("nb", CG.OP_COGNITION_BELIEF_SET, {"holder": npc, "info_id": "S40", "belief": "CERTAIN",
                                                      "authority": "RULE", "cause_ref": "event:e1"}))
    assert [p["authority"] for p in CG.record_of(w, pc, "S40")["belief_provenance"]] == ["PLAYER_EXPLICIT", "RULE_FORCED"]
    # 정보가 반박돼도 믿음은 자동 교정되지 않는다(거짓 믿음)
    w = _ok(w, _op("rf", IN.OP_INFORMATION_VALIDITY_SET, {"info_id": "S40", "validity": "REFUTED"}))
    assert IN.effective_validity(D, w.domain("information_state"), "S40") == "REFUTED"
    assert CG.record_of(w, npc, "S40")["belief"] == "CERTAIN"
    # EXISTENCE만 아는 경우 믿음 설정 불가
    w = _ok(w, _op("e", CG.OP_COGNITION_ACQUIRE, {"holder": npc, "info_id": "S41x", "awareness": "EXISTENCE",
                                                  "acquisition": _acq("y")}))
    assert _project(w, _op("nb2", CG.OP_COGNITION_BELIEF_SET, {"holder": npc, "info_id": "S41x", "belief": "SUSPECTED",
                                                               "authority": "RULE", "cause_ref": "c"})).conflicts
    # 등록부에 PC로 올라 있으면 id 형식과 무관하게 PC 권위 규칙
    assert CG.is_pc_holder(pc) and not CG.is_pc_holder(npc) and not CG.is_pc_holder("group:경비대")


def test_group_cognition_does_not_propagate_to_members():
    s = _sess()
    w = _ok(s, _op("g", CG.OP_COGNITION_ACQUIRE, {"holder": "group:경비대", "info_id": "S40",
                                                  "awareness": "CONTENT", "acquisition": _acq("g1", "OFFICIAL")}))
    assert CG.holders_aware_of(w, "S40") == ["group:경비대"]
    assert CG.awareness_of(w, f"actor:{KANG}", "S40") == "NONE"
    assert _project(s, _op("bad", CG.OP_COGNITION_ACQUIRE, {"holder": "faction:x", "info_id": "S40",
                                                            "awareness": "CONTENT", "acquisition": _acq("q")})).conflicts


# ══════════════════════════════════════════════════════════════
# Relationship · NPC projection
# ══════════════════════════════════════════════════════════════

def test_relationship_sparse_append_idempotent():
    s = _sess()
    d1 = RL.delta_record("rel:1", f"actor:{KANG}", f"actor:{PC}", "TRUST_GAINED", summary="문서를 지켜 줌", at_minute=K)
    w = _ok(s, _op("r1", RL.OP_RELATIONSHIP_DELTA_APPEND, {"delta": d1}))
    r = _project(w, _op("r1b", RL.OP_RELATIONSHIP_DELTA_APPEND, {"delta": d1}))
    assert r.ok and r.world.digest() == w.digest()
    assert _project(w, _op("r1c", RL.OP_RELATIONSHIP_DELTA_APPEND,
                           {"delta": dict(d1, kind="BETRAYAL")})).conflicts
    st = w.domain("relationship_state")
    assert list(st) == [f"actor:{KANG}->actor:{PC}"] and st[list(st)[0]]["deltas"] == [d1]   # 행렬 아님
    assert RL.deltas_involving(w, f"actor:{PC}") == [d1]


def test_npc_projection_is_read_only_and_association_is_not_presence(monkeypatch):
    D = _defn()
    D_no_legacy = dict(D, legacy_source=None)                          # 프로필은 profile_base에서만
    s = _sess()
    s.npcs = {"강지수": {"name": "강지수"}}
    s.resources = {"강지수": {"hp": 2}}
    s.statuses = {"강지수": ["피로"]}
    s.companions = ["강지수"]
    s.event_instances = {"evt_x": {"template_id": "tpl", "lifecycle": "ACTIVE", "current_phase": "p1",
                                   "bound_slots": {"manager": KANG}},
                         "evt_y": {"template_id": "tpl", "lifecycle": "RESOLVED", "current_phase": "end",
                                   "bound_slots": {"manager": KANG}}}
    s.cognition_state = {f"actor:{KANG}": {"S40": dict(CG.empty_record(), awareness="CONTENT")}}
    before = copy.deepcopy({k: getattr(s, k) for k in WS.GAMEPLAY_WORLD_FIELDS})
    p = NP.project_npc(KANG, D_no_legacy, s, legacy_runtime=NP.LegacyRuntime.from_session(s))
    assert p["authored_profile"] == {"role": "관리인"} and p["display_name"] == "강지수"
    assert p["placement"] is None and p["location_tracked"] is False   # 연관 ≠ 현재 위치
    assert p["place_associations"] == ["시장"]
    assert p["relevant_resources"] == {"hp": 2} and p["companionship"] == {"is_companion": True, "met": False}
    assert p["event_roles"] == [{"instance_id": "evt_x", "template_id": "tpl", "slot": "manager", "phase": "p1"}]
    assert p["relevant_cognition"] == []                               # 지정하지 않은 인지는 넣지 않는다
    p2 = NP.project_npc(KANG, D, s, relevant_info_ids=["S40", "S99"])
    assert p2["relevant_cognition"] == [{"info_id": "S40", "awareness": "CONTENT", "belief": None},
                                        {"info_id": "S99", "awareness": "NONE", "belief": None}]
    s.spatial_state = {"placements": {KANG: {"kind": "AT_PLACE", "place_id": "성당"}}}
    assert NP.project_npc(KANG, D, s)["placement"]["place_id"] == "성당"
    assert NP.project_npc("npc_ghost", D, s) is None
    s.spatial_state = {}
    assert {k: getattr(s, k) for k in WS.GAMEPLAY_WORLD_FIELDS} == before
    assert not hasattr(s, "current_npc_context")


# ══════════════════════════════════════════════════════════════
# PLAYER_TURN 통합 — typed 연산 커밋·되감기
# ══════════════════════════════════════════════════════════════

async def test_h04a_ops_commit_and_rewind_in_player_turn(rig, inject_h04a):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.world_clock = WC.initial_clock(K)
    await _run_turn(r)
    ops = [_op("reg", AR.OP_ACTOR_REGISTER, {"actor_id": PC, "kind": "PC", "display_name": "주인공",
                                             "source_kind": "PLAYER", "source_key": str(PLAYER_UID)}),
           _op("acq", CG.OP_COGNITION_ACQUIRE, {"holder": f"actor:{PC}", "info_id": "S40", "awareness": "CONTENT",
                                                "acquisition": _acq("turn:2", "DOCUMENT")}),
           _op("rel", RL.OP_RELATIONSHIP_DELTA_APPEND, {"delta": RL.delta_record(
               "rel:t2", f"actor:{KANG}", f"actor:{PC}", "MET")})]
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        for op in ops:
            prep.world_mutation_batch.add(op)
    inject_h04a.setattr(TP, "capture_world_baseline", _hook)
    tx = await _run_turn(r)
    inject_h04a.undo()
    assert tx.status == tt.TurnStatus.COMMITTED
    assert s.actor_registry[PC]["kind"] == "PC"
    assert CG.record_of(s, f"actor:{PC}", "S40")["awareness"] == "CONTENT"
    assert _disk(s)["relationship_state"] == s.relationship_state
    assert set(tx.preparation.ready_proof.world_changed_domains) == {"actor_registry", "cognition_state",
                                                                     "relationship_state"}
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    assert s.actor_registry == {} and s.cognition_state == {} and s.relationship_state == {}


@pytest.fixture
def inject_h04a():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()
