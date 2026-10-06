# -*- coding: utf-8 -*-
"""H02-A — 동적 세계 정본 필드 · 영속(SCHEMA_VERSION 4) · 되감기 분류 · turn-history v3.

H02-R03  SCHEMA_VERSION 3 → 4
H02-R04  레거시 v3 저장분 → 새 필드 안전 기본값
H02-R05  narrative_seed 재시작·마이그레이션 안정, 기존 값 불변
H02-R06  definition_revision 되감기 제외
H02-R07  narrative_seed 되감기 제외
H02-R08  새 게임 정본 필드 가역(캡처·복원)
H02-R09  v2 이력 레코드(새 필드 없음) → 레거시 기본값 합성 복원
H02-R10  CANONICAL_DOMAINS ⊇ 새 게임 정본, ∌ 정체성 필드
H02-R11  재무·캐시 운영 필드가 새 가역 집합에 유입되지 않음
"""
from __future__ import annotations

import copy
import json
import os

import pytest

import core
from core import commit_coordinator as CC
from core import io as IO
from core import turn_history as TH
from core import turn_preparation as TP
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용
from tests.policy.test_authoritative_commit import _fund
from tests.policy.test_commit_recovery import _restart
from tests.policy.test_turn_history import _commit

pytestmark = pytest.mark.policy

GAMEPLAY = tuple(WS.GAMEPLAY_WORLD_FIELDS)
IDENTITY = ("definition_revision", "narrative_seed")


# ── 레지스트리 · 생성자 ─────────────────────────────────────

def test_schema_version_is_4_and_registry_is_single_source():
    assert IO.SCHEMA_VERSION == 4
    assert len(GAMEPLAY) == 11 and set(GAMEPLAY) == {
        "world_version", "world_clock", "actor_registry", "spatial_state", "spatial_knowledge",
        "access_control_state", "event_instances", "world_schedule", "information_state",
        "cognition_state", "relationship_state"}
    for f, default in WS.ALL_WORLD_FIELDS.items():
        assert f in core.SESSION_FIELDS and core.SESSION_FIELDS[f] == default
    s = core.TRPGSession("sess-1", 1, 2, "x", {})
    for f in GAMEPLAY:
        assert getattr(s, f) == WS.GAMEPLAY_WORLD_FIELDS[f]
    assert s.definition_revision is None
    assert s.narrative_seed == WS.derive_narrative_seed("sess-1")
    # 세션마다 독립 객체(공유 가변 기본값 금지)
    s2 = core.TRPGSession("sess-2", 1, 2, "x", {})
    s.world_clock["probe"] = 1
    assert s2.world_clock == {} and WS.GAMEPLAY_WORLD_FIELDS["world_clock"] == {}
    assert s.narrative_seed != s2.narrative_seed


def test_partition_reversible_vs_identity_vs_operational():
    assert set(GAMEPLAY) <= set(TP.CANONICAL_DOMAINS)
    assert set(GAMEPLAY) <= set(TH.REVERSIBLE_FIELDS)
    assert set(GAMEPLAY) <= set(CC.COMMIT_OWNED_FIELDS)
    for f in IDENTITY:
        assert f not in TP.CANONICAL_DOMAINS
        assert f not in TH.REVERSIBLE_FIELDS
        assert f not in CC.COMMIT_OWNED_FIELDS
    operational = set(TH.OPERATIONAL_FIELDS) | {
        "cache_name", "cache_text", "cache_created_at", "cache_tokens", "cache_model",
        "cache_history_marker", "cache_history_stale", "open_prepaid_ink", "open_minutes",
        "interpret_cost_krw", "profile_ai_cost_krw", "cost_stats", "failed_settlement_backlog"}
    assert not operational & set(WS.ALL_WORLD_FIELDS)
    assert not operational & set(TH.WORLD_REVERSIBLE_FIELDS)


# ── 마이그레이션 · 시드 안정성 ──────────────────────────────

def test_v3_payload_migrates_to_safe_defaults_idempotently():
    data = {"schema_version": 3, "session_id": "abc", "players": {}}
    out = IO.migrate_session_data(copy.deepcopy(data))
    assert out["schema_version"] == 4
    for f in GAMEPLAY:
        assert out[f] == WS.GAMEPLAY_WORLD_FIELDS[f]
    assert out["definition_revision"] is None
    assert out["narrative_seed"] == WS.derive_narrative_seed("abc")
    again = dict(out, schema_version=3)
    assert IO.migrate_session_data(again)["narrative_seed"] == out["narrative_seed"]
    # 이미 있는 시드는 절대 바꾸지 않는다
    kept = {"schema_version": 3, "session_id": "abc", "narrative_seed": "seed_custom"}
    assert IO.migrate_session_data(kept)["narrative_seed"] == "seed_custom"
    # v2(auto_gm_*) 저장분도 한 번에 v4까지
    v2 = {"schema_version": 2, "session_id": "old", "auto_gm_active": True}
    m = IO.migrate_session_data(v2)
    assert m["gm_active"] is True and m["world_version"] is None and m["schema_version"] == 4


def test_seed_is_hash_independent_derivation():
    a = WS.derive_narrative_seed("same")
    assert a == WS.derive_narrative_seed("same") and a.startswith("seed_")
    assert a != WS.derive_narrative_seed("other")
    assert a == "seed_" + core.world_ids.stable_digest("session.narrative_seed.v1", "same", length=32)


async def test_legacy_v3_data_json_restores_with_defaults_and_stable_seed(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _commit(r, "숲길")
    path = f"sessions/{s.session_id}/data.json"
    data = json.load(open(path, encoding="utf-8"))
    assert data["schema_version"] == 4 and data["narrative_seed"] == s.narrative_seed
    # v3 저장분 모사: H02 필드 제거
    for f in WS.ALL_WORLD_FIELDS:
        data.pop(f, None)
    data["schema_version"] = 3
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False)

    bot2, s2 = await _restart(r)
    for f in GAMEPLAY:
        assert getattr(s2, f) == WS.GAMEPLAY_WORLD_FIELDS[f], f
    assert s2.definition_revision is None
    assert s2.narrative_seed == WS.derive_narrative_seed(s.session_id) == s.narrative_seed
    # 재저장·재시작해도 같은 시드
    await IO.save_session_data_strict(bot2, s2)
    saved = json.load(open(path, encoding="utf-8"))
    assert saved["schema_version"] == 4 and saved["narrative_seed"] == s.narrative_seed
    r.sess = s2
    _, s3 = await _restart(r)
    assert s3.narrative_seed == s.narrative_seed


async def test_new_fields_persist_through_strict_save_and_restart(rig):
    r = rig
    s = r.sess
    s.world_clock = {"probe": 7}
    s.relationship_state = {"pc_1": {"npc_x": 2}}
    s.definition_revision = "sha256:" + "a" * 64
    await IO.save_session_data_strict(r.bot, s)
    _, s2 = await _restart(r)
    assert s2.world_clock == {"probe": 7}
    assert s2.relationship_state == {"pc_1": {"npc_x": 2}}
    assert s2.definition_revision == "sha256:" + "a" * 64
    assert s2.narrative_seed == s.narrative_seed


# ── 되감기 분류 · turn-history v3 ───────────────────────────

async def test_rewind_restores_world_fields_but_not_identity(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx1 = await _commit(r, "숲길")
    rec1 = TH.load_record(s.session_id, tx1.transaction_id)
    assert rec1["schema_version"] == TH.HISTORY_SCHEMA_VERSION == 3
    assert set(GAMEPLAY) <= set(rec1["post"]) and not set(IDENTITY) & set(rec1["post"])
    # 턴 사이 외부 변화(테스트 시드) — 턴 2 post에 실린다
    s.world_clock = {"minute_index": 42}
    s.event_instances = {"ev1": {"phase": "A"}}
    await _commit(r, "동굴", "동굴로 간다")
    seed = s.narrative_seed
    s.definition_revision = "sha256:" + "b" * 64

    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    assert s.world_clock == rec1["post"]["world_clock"] == {}
    assert s.event_instances == {}
    assert s.world_version == rec1["post"]["world_version"]
    # 정체성 필드는 되감기 대상이 아니다
    assert s.narrative_seed == seed
    assert s.definition_revision == "sha256:" + "b" * 64


async def test_v2_history_record_rewinds_with_synthesized_legacy_defaults(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    tx1 = await _commit(r, "숲길")
    await _commit(r, "동굴", "동굴로 간다")
    # 턴 1 레코드를 H02 이전 v2 형식으로 되돌린다(새 필드 없음, digest 재계산)
    path = TH.record_path(s.session_id, tx1.transaction_id)
    rec = json.load(open(path, encoding="utf-8"))
    for part in ("pre", "post"):
        for f in GAMEPLAY:
            rec[part].pop(f, None)
    rec["schema_version"] = 2
    rec["post_digest"] = TH._digest(rec["post"])
    json.dump(rec, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    assert TH._record_restorable(rec)

    # 현재 세션에 새 도메인 값이 남아 있어도 복원 결과로 새지 않아야 한다
    s.world_clock = {"minute_index": 99}
    s.spatial_state = {"scene": "x"}
    s.definition_revision = "sha256:" + "c" * 64
    seed = s.narrative_seed
    res = await r.gm.history_rewind(s, 1)
    assert res["ok"], res
    for f in GAMEPLAY:
        assert getattr(s, f) == WS.GAMEPLAY_WORLD_FIELDS[f], f
    assert s.world_timeline["current_location"] == "숲길"       # 기존 v2 내용은 그대로 복원
    assert s.narrative_seed == seed
    assert s.definition_revision == "sha256:" + "c" * 64


def test_v1_history_record_policy_unchanged():
    rec = {"schema_version": 1, "post": {"x": 1}}
    rec["post_digest"] = TH._digest(rec["post"])
    assert not TH._record_restorable(rec)


def test_restore_reversible_never_touches_identity_or_operational():
    s = core.TRPGSession("idn", 1, 2, "x", {})
    s.definition_revision = "sha256:" + "d" * 64
    s.total_cost = 123.0
    state = TH.capture_reversible(core.TRPGSession("other", 1, 2, "x", {}))
    state["definition_revision"] = None             # 레코드에 섞여 있어도 무시된다
    state["narrative_seed"] = "seed_forged"
    state["total_cost"] = 0.0
    seed = s.narrative_seed
    TH.restore_reversible(s, state)
    assert s.definition_revision == "sha256:" + "d" * 64
    assert s.narrative_seed == seed and s.total_cost == 123.0
