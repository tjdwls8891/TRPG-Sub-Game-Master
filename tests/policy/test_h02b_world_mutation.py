# -*- coding: utf-8 -*-
"""H02-B — WorldMutationBatch · ProjectedWorld · CommitCoordinator 연결 · 공통 정본 변이 primitive.

H02-R12  투영은 세션을 변경하지 않는다
H02-R13  묶음 digest 결정성(프로세스·PYTHONHASHSEED 무관)
H02-R14  동결 후 연산 추가 거부
H02-R15  LocalRef 결정적 해석
H02-R16  단일값 충돌 — 마지막 쓰기 채택 금지
H02-R17  투영 충돌 ≠ RETRY_PENDING (결정적 종료 실패)
H02-R18  TurnPreparation world_version·definition_revision 기준선 가드
H02-R19  CommitPlan v2가 source/target version + digest 동결
H02-R20  빈 묶음 PLAYER_TURN도 world_version 전진
H02-R21  영속 전 실패 → world_version·새 도메인 롤백
H02-R22  영속 후 복구 → 동결된 대상 version 유지(재생성 없음)
H02-R23  구 CommitPlan v1 PREPARED 복구 호환
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import core
from core import commit_coordinator as CC
from core import commit_journal as CJ
from core import canonical_mutation as CM
from core import turn_history as TH
from core import turn_preparation as TP
from core import turn_transaction as tt
from core import world_mutation as WM
from core import world_state as WS
from tests.conftest import PLAYER_UID, REPO_ROOT
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_ready_barrier import rig, _run_turn  # noqa: F401
from tests.policy.test_authoritative_commit import _disk, _fund, _phases
from tests.policy.test_commit_recovery import _assert_committed_once, _fail_phase_once, _restart

pytestmark = pytest.mark.policy


@pytest.fixture
def inject():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


# ── 테스트 전용 typed handler (production 레지스트리는 비어 있다) ──

def _registry():
    reg = WM.OperationRegistry("test")

    def set_clock(view, op, payload):
        return [WM.write(op, "world_clock", ("minute_index",), WM.MERGE_SINGLE_VALUE,
                         payload["minute"])]

    def bump_rel(view, op, payload):
        return [WM.write(op, "relationship_state", (payload["a"], payload["b"]),
                         WM.MERGE_ADDITIVE, payload["delta"])]

    def tag_place(view, op, payload):
        return [WM.write(op, "spatial_state", ("tags",), WM.MERGE_SET_LIKE,
                         {"add": payload.get("add", []), "remove": payload.get("remove", [])})]

    def log_event(view, op, payload):
        return [WM.write(op, "event_instances", ("log",), WM.MERGE_APPEND_DEDUPE, payload["entry"])]

    def spawn(view, op, payload):
        return [WM.write(op, "event_instances", ("spawned", "id"), WM.MERGE_SINGLE_VALUE,
                         payload["id"])]

    def set_clock_root(view, op, payload):
        return [WM.write(op, "world_clock", ("minute_index", "sub"), WM.MERGE_SINGLE_VALUE, 1)]

    for name, fn in (("set_clock", set_clock), ("bump_rel", bump_rel), ("tag_place", tag_place),
                     ("log_event", log_event), ("spawn", spawn), ("set_clock_sub", set_clock_root)):
        reg.register(name, fn)
    return reg


def _batch(sess, bid="b1"):
    return WM.WorldMutationBatch(batch_id=bid, outer_origin=WS.CAUSE_ADMIN_EDIT,
                                 source_world_version_id=WS.version_id_of(sess.world_version),
                                 definition_revision=sess.definition_revision)


def _sess(sid="s-wm"):
    return core.TRPGSession(sid, 1, 2, "x", {})


# ══════════════════════════════════════════════════════════════
# 순수 묶음 · 투영
# ══════════════════════════════════════════════════════════════

def test_projection_never_mutates_session_and_merges_by_declared_semantics():
    s = _sess()
    s.relationship_state = {"pc": {"npc": 1}}
    s.spatial_state = {"tags": ["dark"]}
    before = {k: json.dumps(getattr(s, k), sort_keys=True) for k in WS.GAMEPLAY_WORLD_FIELDS}
    b = _batch(s)
    b.add(WM.make_op("o1", "set_clock", "TEST", {"minute": 30}))
    b.add(WM.make_op("o2", "set_clock", "TEST", {"minute": 30}))           # 같은 값 — 허용
    b.add(WM.make_op("o3", "bump_rel", "TEST", {"a": "pc", "b": "npc", "delta": 2}))
    b.add(WM.make_op("o4", "bump_rel", "TEST", {"a": "pc", "b": "npc", "delta": -1}))
    b.add(WM.make_op("o5", "tag_place", "TEST", {"add": ["lit"], "remove": ["dark"]}))
    b.add(WM.make_op("o6", "log_event", "TEST", {"entry": {"e": 1}}))
    b.add(WM.make_op("o7", "log_event", "TEST", {"entry": {"e": 1}}))     # 정규 동일값은 한 번
    b.freeze()
    res = WM.project(s, b, _registry())
    assert res.ok, res.conflicts
    w = res.world
    assert w.get("world_clock", ("minute_index",)) == 30
    assert w.get("relationship_state", ("pc", "npc")) == 2
    assert w.get("spatial_state", ("tags",)) == ["lit"]
    assert w.get("event_instances", ("log",)) == [{"e": 1}]
    assert {k: json.dumps(getattr(s, k), sort_keys=True) for k in WS.GAMEPLAY_WORLD_FIELDS} == before
    assert set(res.changed_domains(WM.ProjectedWorld.capture(s))) == {
        "world_clock", "relationship_state", "spatial_state", "event_instances"}


def test_single_value_conflict_rejects_without_last_write_wins():
    s = _sess()
    b = _batch(s)
    b.add(WM.make_op("o1", "set_clock", "TEST", {"minute": 10}))
    b.add(WM.make_op("o2", "set_clock", "TEST", {"minute": 20}))
    b.freeze()
    res = WM.project(s, b, _registry())
    assert not res.ok and res.world is None
    c = res.conflicts[0]
    assert c.code == "MERGE_CONFLICT" and c.to_dict()["failure_class"] == "WORLD_PROJECTION_CONFLICT"
    assert set(c.op_ids) == {"o1", "o2"}
    with pytest.raises(WM.WorldProjectionConflict):
        WM.apply_projection(s, res, WM.ProjectedWorld.capture(s))
    assert s.world_clock == {}


def test_other_conflict_classes():
    s = _sess()
    cases = {
        "PATH_OVERLAP": [("set_clock", {"minute": 1}), ("set_clock_sub", {})],
        "UNKNOWN_OPERATION": [("teleport", {})],
    }
    for code, ops in cases.items():
        b = _batch(s, code)
        for i, (t, p) in enumerate(ops):
            b.add(WM.make_op(f"o{i}", t, "TEST", p))
        b.freeze()
        res = WM.project(s, b, _registry())
        assert not res.ok and res.conflicts[0].code == code, (code, res.conflicts)
    b = _batch(s, "exp")
    b.add(WM.make_op("o1", "set_clock", "TEST", {"minute": 5},
                     expected={"domain": "world_clock", "path": ["minute_index"], "value": 99}))
    b.freeze()
    assert WM.project(s, b, _registry()).conflicts[0].code == "EXPECTATION_FAILED"
    b = _batch(s, "set")
    b.add(WM.make_op("o1", "tag_place", "TEST", {"add": ["x"]}))
    b.add(WM.make_op("o2", "tag_place", "TEST", {"remove": ["x"]}))
    b.freeze()
    assert WM.project(s, b, _registry()).conflicts[0].code == "MERGE_CONFLICT"


def test_world_version_and_unknown_domains_are_not_writable_and_registry_is_empty():
    op = WM.make_op("o", "t", "TEST", {})
    with pytest.raises(WM.WorldMutationError):
        WM.write(op, "world_version", ("version_id",), WM.MERGE_SINGLE_VALUE, "x")
    with pytest.raises(WM.WorldMutationError):
        WM.write(op, "npcs", ("a",), WM.MERGE_SINGLE_VALUE, "x")
    with pytest.raises(WM.WorldMutationError):
        WM.write(op, "world_clock", (), WM.MERGE_SINGLE_VALUE, 1)
    assert WM.PRODUCTION_REGISTRY.op_types() == ()
    for bad in ({"f": lambda: 1}, {"s": {1, 2}}, {1: "x"}):
        with pytest.raises(WM.WorldMutationError):
            WM.make_op("o", "t", "TEST", bad)


def test_freeze_lifecycle_and_local_refs_are_deterministic():
    s = _sess()

    def build():
        b = _batch(s, "lr")
        b.add(WM.make_op("o1", "spawn", "TEST", {"id": WM.local_ref("ev")}, defines_locals=("ev",)))
        b.add(WM.make_op("o2", "log_event", "TEST", {"entry": {"ref": WM.local_ref("ev")}},
                         causal_parent_op_ids=("o1",)))
        return b
    b1, b2 = build(), build()
    d1 = b1.freeze()
    assert b1.freeze() == d1 and b1.frozen                     # 멱등
    assert b2.freeze() == d1
    with pytest.raises(WM.BatchFrozenError):
        b1.add(WM.make_op("o3", "set_clock", "TEST", {"minute": 1}))
    res = WM.project(s, b1, _registry())
    rid = res.world.get("event_instances", ("spawned", "id"))
    assert rid.startswith("local_") and res.world.get("event_instances", ("log",)) == [{"ref": rid}]
    assert rid == "local_" + core.world_ids.stable_digest("world_mutation.local_ref.v1", "lr", "ev")
    # 구조 오류
    for ops, code in (
        ([WM.make_op("a", "spawn", "T", {"id": WM.local_ref("x")})], "LOCAL_REF_UNDEFINED"),
        ([WM.make_op("a", "spawn", "T", {}, defines_locals=("x",)),
          WM.make_op("b", "log_event", "T", {"entry": WM.local_ref("x")})], "LOCAL_REF_NOT_CAUSAL"),
        ([WM.make_op("a", "spawn", "T", {}), WM.make_op("a", "spawn", "T", {})], "OP_ID_DUPLICATE"),
        ([WM.make_op("a", "spawn", "T", {}, causal_parent_op_ids=("z",))], "CAUSAL_PARENT_NOT_EARLIER"),
    ):
        b = _batch(s, code)
        for op in ops:
            b.add(op)
        with pytest.raises(WM.WorldMutationError, match=code):
            b.freeze()
    # 직렬화 왕복
    again = WM.WorldMutationBatch.from_dict(json.loads(json.dumps(build().to_dict())))
    assert again.freeze() == d1


_DIGEST_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from core import world_mutation as WM, world_state as WS
b = WM.WorldMutationBatch(batch_id="hb", outer_origin="ADMIN_EDIT",
                          source_world_version_id=None, definition_revision=None)
b.add(WM.make_op("o1", "spawn", "T", {"z": 1, "a": {"y": [3, 2], "b": "가"}, "id": WM.local_ref("q")},
                 defines_locals=("q",)))
v = WS.next_world_version("sess", None, cause_kind="PLAYER_TURN", cause_id="tx1")
print(json.dumps([b.freeze(), v["version_id"], WS.derive_narrative_seed("sess")]))
"""


def test_batch_digest_and_version_ids_are_hashseed_independent():
    outs = []
    for seed in ("0", "777"):
        cp = subprocess.run([sys.executable, "-c", _DIGEST_SCRIPT, REPO_ROOT], capture_output=True,
                            text=True, env=dict(os.environ, PYTHONHASHSEED=seed), timeout=120)
        assert cp.returncode == 0, cp.stderr
        outs.append(json.loads(cp.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1]


def test_next_world_version_chain():
    v1 = WS.next_world_version("s", None, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="t1")
    v2 = WS.next_world_version("s", v1, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="t2")
    assert (v1["ordinal"], v2["ordinal"]) == (1, 2) and v2["parent_version_id"] == v1["version_id"]
    assert v1 == WS.next_world_version("s", None, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="t1")
    assert v1["version_id"] != WS.next_world_version("s", None, cause_kind=WS.CAUSE_PLAYER_TURN,
                                                     cause_id="t9")["version_id"]
    assert "ts" not in v1 and "timestamp" not in v1
    with pytest.raises(ValueError):
        WS.next_world_version("s", {"bogus": 1}, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="t")


# ══════════════════════════════════════════════════════════════
# PLAYER_TURN 연결 (실제 자동 경로)
# ══════════════════════════════════════════════════════════════

def _prepared_md(s, tx):
    j = CJ.CommitJournal(CJ.default_journal_path(s.session_id))
    es = j.history_for_transaction(tx.transaction_id, attempt=tx.attempt)
    return next(e for e in es if e.phase == CJ.CommitPhase.PREPARED).metadata


async def test_empty_batch_player_turn_advances_world_version_and_freezes_plan(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    assert s.world_version is None
    tx1 = await _run_turn(r)
    assert tx1.status == tt.TurnStatus.COMMITTED
    v1 = s.world_version
    assert WS.is_world_version(v1) and v1["ordinal"] == 1 and v1["cause_kind"] == "PLAYER_TURN"
    assert v1["cause_id"] == tx1.transaction_id and v1["parent_version_id"] is None
    assert v1 == WS.next_world_version(s.session_id, None, cause_kind="PLAYER_TURN",
                                       cause_id=tx1.transaction_id)
    md = _prepared_md(s, tx1)
    assert md["plan_version"] == 2
    assert md["source_world_version"] is None and md["target_world_version"] == v1
    assert md["definition_revision"] is None
    prep = tx1.preparation
    assert md["world_mutation_digest"] == prep.world_mutation_digest == prep.world_mutation_batch.digest
    assert prep.world_mutation_batch.is_empty()
    assert prep.ready_proof.world_mutation_digest == md["world_mutation_digest"]
    assert _disk(s)["world_version"] == v1
    rec = TH.load_record(s.session_id, tx1.transaction_id)
    assert rec["pre"]["world_version"] is None and rec["post"]["world_version"] == v1
    # 두 번째 턴 — 부모 연결
    tx2 = await _run_turn(r)
    assert s.world_version["ordinal"] == 2 and s.world_version["parent_version_id"] == v1["version_id"]
    assert _prepared_md(s, tx2)["source_world_version"] == v1


async def test_non_empty_typed_batch_applies_inside_commit_and_rewinds(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        prep.world_registry = _registry()
        prep.world_mutation_batch.add(WM.make_op("c1", "set_clock", "TEST", {"minute": 45}))
    inject.setattr(TP, "capture_world_baseline", _hook)
    tx = await _run_turn(r)
    inject.undo()
    assert tx.status == tt.TurnStatus.COMMITTED
    assert s.world_clock == {"minute_index": 45}
    assert tx.preparation.ready_proof.world_changed_domains == ("world_clock",)
    assert _disk(s)["world_clock"] == {"minute_index": 45}
    await _run_turn(r)                                     # 빈 묶음 턴
    assert s.world_clock == {"minute_index": 45}
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"] and s.world_clock == {"minute_index": 45} and s.world_version["ordinal"] == 1


async def test_projection_conflict_is_deterministic_failure_not_retry(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        prep.world_registry = _registry()
        prep.world_mutation_batch.add(WM.make_op("c1", "set_clock", "TEST", {"minute": 1}))
        prep.world_mutation_batch.add(WM.make_op("c2", "set_clock", "TEST", {"minute": 2}))
    inject.setattr(TP, "capture_world_baseline", _hook)
    turn_before, wv_before = s.turn_count, s.world_version
    tx = await _run_turn(r)
    prep = tx.preparation
    assert prep.failure_code == "WORLD_PROJECTION_CONFLICT"
    assert prep.world_conflicts and prep.world_conflicts[0]["code"] == "MERGE_CONFLICT"
    assert prep.phase != TP.PREP_RETRY_PENDING
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert not s.extraction_retry_ctx
    assert s.turn_count == turn_before and s.world_version == wv_before and s.world_clock == {}
    assert "PREPARED" not in _phases(s, tx)
    assert tt.FailureCode.WORLD_PROJECTION_CONFLICT.value == "WORLD_PROJECTION_CONFLICT"


@pytest.mark.parametrize("field, value, reason", [
    ("definition_revision", "sha256:" + "e" * 64, "world:definition_revision_changed"),
    ("world_version", {"version_id": "wv_x", "parent_version_id": None, "ordinal": 9,
                       "cause_kind": "ADMIN_EDIT", "cause_id": "a"}, "world:version_changed"),
])
async def test_baseline_guard_blocks_ready(rig, inject, field, value, reason):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    seen = {}
    orig = TP.transition_to_ready

    def _wrap(session, prep):
        setattr(session, field, value)                    # 준비 도중 외부 변화
        proof, reasons = orig(session, prep)
        seen["reasons"] = reasons
        return proof, reasons
    inject.setattr(TP, "transition_to_ready", _wrap)
    tx = await _run_turn(r)
    assert reason in seen["reasons"], seen
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.preparation.phase != TP.PREP_RETRY_PENDING


async def test_pre_persist_failure_rolls_back_world_version_and_domains(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _run_turn(r)
    v1 = s.world_version
    orig = TP.capture_world_baseline

    def _hook(session, prep):
        orig(session, prep)
        prep.world_registry = _registry()
        prep.world_mutation_batch.add(WM.make_op("c1", "set_clock", "TEST", {"minute": 7}))
    inject.setattr(TP, "capture_world_baseline", _hook)

    async def _boom(session):
        raise OSError("디스크 실패(테스트)")
    inject.setattr(core.io, "write_session_strict_locked", _boom)
    tx = await _run_turn(r)
    inject.undo()
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert s.world_version == v1 and s.world_clock == {}
    assert _disk(s)["world_version"] == v1


async def test_post_persist_recovery_keeps_frozen_target_version(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.SESSION_PERSISTED)
    tx = await _run_turn(r)
    assert _phases(r.sess, tx) == ["PREPARED"]
    target = _prepared_md(r.sess, tx)["target_world_version"]
    assert _disk(r.sess)["world_version"] == target
    inject.undo()
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None
    _assert_committed_once(s2, tx)
    assert s2.world_version == target                      # 재생성하지 않은 동결 대상 그대로


async def test_recovery_rejects_world_version_mismatch(rig, inject):
    r = rig
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.SESSION_PERSISTED)
    tx = await _run_turn(r)
    inject.undo()
    path = f"sessions/{r.sess.session_id}/data.json"
    data = json.load(open(path, encoding="utf-8"))
    data["world_version"] = None                           # 영속 world_version이 대상과 어긋남(손상)
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery and "world_version" in json.dumps(s2.commit_recovery, ensure_ascii=False)
    assert "COMMITTED" not in [e.phase.value for e in CJ.CommitJournal(
        CJ.default_journal_path(s2.session_id)).history_for_transaction(tx.transaction_id,
                                                                        attempt=tx.attempt)]


async def test_old_commit_plan_v1_prepared_recovers(rig, inject):
    """H02 이전(plan v1) PREPARED + SESSION_PERSISTED 크래시 창을 새 코드가 정상 구버전으로 복구한다."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    _fail_phase_once(inject, CJ.CommitPhase.SESSION_PERSISTED)
    tx = await _run_turn(r)
    inject.undo()
    sid = s.session_id
    jpath = CJ.default_journal_path(sid)
    lines = [json.loads(x) for x in open(jpath, encoding="utf-8") if x.strip()]
    md1 = None
    for e in lines:
        if e["transaction_id"] == tx.transaction_id and e["phase"] == "PREPARED":
            md = e["metadata"]
            md1 = {k: v for k, v in md.items() if k not in (
                "source_world_version", "target_world_version", "definition_revision",
                "world_mutation_digest", "plan_fingerprint", "target_marker")}
            md1["plan_version"] = 1
            fp = CC._digest(md1)
            md1["plan_fingerprint"] = fp
            md1["target_marker"] = {"transaction_id": md["transaction_id"], "attempt": md["attempt"],
                                    "logical_turn": md["logical_turn"],
                                    "settlement_id": md["settlement_id"],
                                    "plan_fingerprint": fp, "story_turn": md["story_turn"]}
            e["metadata"] = md1
    with open(jpath, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in lines))
    # 구 프로세스가 쓴 data.json: v3 · H02 필드 없음 · v1 대상 표식
    dpath = f"sessions/{sid}/data.json"
    data = json.load(open(dpath, encoding="utf-8"))
    for k in WS.ALL_WORLD_FIELDS:
        data.pop(k, None)
    data["schema_version"] = 3
    data["commit_marker"] = md1["target_marker"]
    json.dump(data, open(dpath, "w", encoding="utf-8"), ensure_ascii=False)

    plan = CC.CommitPlan.from_journal_metadata(md1)
    assert plan.plan_version == 1 and plan.target_world_version is None
    assert "source_world_version" not in plan.to_dict()
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery is None
    _assert_committed_once(s2, tx)
    assert s2.world_version is None                        # 레거시 계보 — 재생성하지 않음
    assert s2.narrative_seed == WS.derive_narrative_seed(sid)


def test_plan_v2_metadata_corruption_is_not_mistaken_for_v1():
    md = {"plan_version": 2, "session_id": "s", "transaction_id": "t", "logical_turn": 1,
          "attempt": 1, "settlement_id": "x", "frozen_cost_event_ids": [], "billing_user_ids": [],
          "story_turn": 1, "gm_turn": 1}
    with pytest.raises(ValueError, match="v2"):
        CC.CommitPlan.from_journal_metadata(md)
    with pytest.raises(ValueError):
        CC.CommitPlan.from_journal_metadata(dict(md, plan_version=3))


# ══════════════════════════════════════════════════════════════
# 공통 정본 변이 primitive (INITIALIZATION · ADMIN_EDIT)
# ══════════════════════════════════════════════════════════════

async def test_canonical_mutation_initialization_and_admin_edit():
    bot = FakeBot()
    s = _sess("cm-1")
    coord = CM.CanonicalMutationCoordinator(bot, registry=_registry())
    # PLAYER_TURN은 다루지 않는다
    with pytest.raises(ValueError):
        CM.new_batch(s, cause_kind=WS.CAUSE_PLAYER_TURN, cause_id="x")
    b = CM.new_batch(s, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    b.add(WM.make_op("i1", "set_clock", "INIT", {"minute": 480}))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    assert res.outcome == CM.OUTCOME_COMMITTED, res
    assert s.world_version["cause_kind"] == "INITIALIZATION" and s.world_version["ordinal"] == 1
    assert s.world_clock == {"minute_index": 480}
    assert json.load(open("sessions/cm-1/data.json", encoding="utf-8"))["world_version"] == s.world_version
    assert [e["phase"] for e in CM.read_journal("cm-1")] == ["PREPARED", "PERSISTED", "COMMITTED"]
    # ADMIN no-op: 같은 값 → mutation·version 없음
    v1 = s.world_version
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a0")
    b.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 480}))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a0")
    assert res.outcome == CM.OUTCOME_NOOP and s.world_version == v1
    # ADMIN 실제 변화 → version 전진
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a1")
    b.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 500}))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a1")
    assert res.outcome == CM.OUTCOME_COMMITTED and s.world_version["parent_version_id"] == v1["version_id"]
    # stale 묶음 거부
    stale = WM.WorldMutationBatch(batch_id="st", outer_origin=WS.CAUSE_ADMIN_EDIT,
                                  source_world_version_id=v1["version_id"], definition_revision=None)
    res = await coord.apply(s, stale, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="st")
    assert res.outcome == CM.OUTCOME_REJECTED and "stale" in res.reason
    # 충돌 → 정본 불변
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a2")
    b.add(WM.make_op("x", "set_clock", "ADMIN", {"minute": 1}))
    b.add(WM.make_op("y", "set_clock", "ADMIN", {"minute": 2}))
    cur = s.world_version
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a2")
    assert res.outcome == "WORLD_PROJECTION_CONFLICT" and s.world_version == cur


async def test_canonical_mutation_rejects_during_active_turn_and_rolls_back(monkeypatch):
    bot = FakeBot()
    s = _sess("cm-2")
    coord = CM.CanonicalMutationCoordinator(bot, registry=_registry())
    tx = tt.get_or_begin_turn_transaction(s, "행동")
    b = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a")
    b.add(WM.make_op("a", "set_clock", "ADMIN", {"minute": 5}))
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a")
    assert res.outcome == CM.OUTCOME_REJECTED and "턴" in res.reason
    tt.finalize(s, tx.transaction_id, tt.TurnStatus.ABORTED, failure_stage="TEST")

    async def _boom(session):
        raise OSError("디스크 실패(테스트)")
    monkeypatch.setattr(core.io, "write_session_strict_locked", _boom)
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="a")
    assert res.outcome == CM.OUTCOME_FAILED_PRE_PERSIST
    assert s.world_version is None and s.world_clock == {}
    assert [e["phase"] for e in CM.read_journal("cm-2")] == ["PREPARED", "ABORTED"]


async def test_canonical_mutation_journal_recovery_classification(monkeypatch):
    bot = FakeBot()
    s = _sess("cm-3")
    coord = CM.CanonicalMutationCoordinator(bot, registry=_registry())
    orig = CM._append

    def _fail_after_persist(sid, entry):
        if entry["phase"] == CM.PHASE_PERSISTED:
            raise CM.CanonicalMutationJournalError("저널 실패(테스트)")
        return orig(sid, entry)
    monkeypatch.setattr(CM, "_append", _fail_after_persist)
    b = CM.new_batch(s, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    res = await coord.apply(s, b, cause_kind=WS.CAUSE_INITIALIZATION, cause_id="init")
    assert res.outcome == CM.OUTCOME_RECOVERY_PENDING
    assert s.world_version == res.target_world_version          # 영속됨 = 정본
    monkeypatch.setattr(CM, "_append", orig)
    assert [a for _, a, _ in CM.classify_recovery(s)] == ["COMPLETE"]
    b2 = CM.new_batch(s, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="x")
    assert (await coord.apply(s, b2, cause_kind=WS.CAUSE_ADMIN_EDIT, cause_id="x")).outcome \
        == CM.OUTCOME_REJECTED                                  # 미종결 변이가 있으면 금지
    assert CM.recover(s) == [(res.mutation_id, "COMPLETE")]
    assert CM.classify_recovery(s) == []
    # 미영속 PREPARED → ABORT
    s4 = _sess("cm-4")
    CM._append("cm-4", {"mutation_id": "m", "phase": CM.PHASE_PREPARED,
                        "source_world_version": None, "target_world_version": {"version_id": "wv"}})
    assert [a for _, a, _ in CM.classify_recovery(s4)] == ["ABORT"]
