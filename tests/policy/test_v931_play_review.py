# -*- coding: utf-8 -*-
"""v9.3.1 — 운영 세션 검토 결함(사용자 결정): ① 진행 중 유지 시간 만료 → 묘사 전 중단(청구 0, 무상 창 없음)
② 키 없는 물건 열기·읽기·쓰기는 그 연산만 뺀다 ③ 추출 상태 블록은 PC·동행·장면 인물만."""
from __future__ import annotations

import types

import pytest

import core
from core import turn_transaction as tt
from indaim.rules.items import items as IT
from tests.policy.test_h05bc_turn_wiring import drig, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


async def test_expiry_during_turn_aborts_before_narration_without_reissue(drig, monkeypatch):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment("시장")
    orig = core.player_world.plan_player_turn
    reissued = []

    def expire_then_plan(*a, **k):
        r.sess.cache_name = None                       # 판단 뒤 만료 감지(프레즌스 finalize와 같은 상태)
        return orig(*a, **k)
    monkeypatch.setattr(core.player_world, "plan_player_turn", expire_then_plan)

    async def no_reissue(*a, **k):
        reissued.append(1)
    monkeypatch.setattr(core.cache_lifecycle, "reissue", no_reissue)
    tx = await turn(r, "시장으로 간다")
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_code == tt.FailureCode.SESSION_NOT_OPEN.value
    assert "narration" not in r.prov.calls and not reissued                # 묘사·재발급(무상 창) 없음
    game = [m.content or "" for m in r.bot.get_channel(r.sess.game_ch_id).sent]
    assert any("세션 열기" in t and "시장으로 간다" in t for t in game)       # 재오픈 + 선언 복사 안내 하나로
    assert any("청구 0" in (m.content or "") for m in r.master.sent)


def test_unkeyed_open_read_use_are_dropped_alone():
    m = IT.ItemManifest(owners=(("O1", "PC", "PC"),))
    text = "배낭을 열자 공구 세트와 통조림이 보였다."
    raw = [{"op": "OPEN", "owner": "O1", "item": "NEW", "name": "방수 배낭"},
           {"op": "READ", "owner": "O1", "item": "NONE"},
           {"op": "USE", "owner": "O1", "item": "NEW", "name": "약통"},
           {"op": "ACQUIRE", "owner": "O1", "item": "NEW", "name": "공구 세트", "quantity": 1},
           {"op": "ACQUIRE", "owner": "O1", "item": "NEW", "name": "통조림", "quantity": 3}]
    ops = IT.normalize_raw_ops(m, raw, pc_name="PC", narration_text=text)
    assert [(o["op"], o["source"].get("new")) for o in ops] == [("ACQUIRE", "공구 세트"), ("ACQUIRE", "통조림")]
    with pytest.raises(IT.ItemContractError):                               # 상태를 바꾸는 연산은 그대로 거부(재추출)
        IT.normalize_raw_ops(m, [{"op": "DROP", "owner": "O1", "item": "NEW"}], pc_name="PC", narration_text=text)


def test_status_block_lists_only_targets():
    s = types.SimpleNamespace(scenario_data={"status_effects": {"불안": {"apply_condition": "a", "remove_condition": "b"}}},
                              statuses={"PC": ["불안"], "섬 반대편 인물": ["불안"]}, world_clock={}, status_clock={},
                              irregular_npcs={})
    full = core.extraction.build_extraction_limits(s)
    assert "- 섬 반대편 인물: 불안" in full
    narrow = core.extraction.build_extraction_limits(s, status_targets={"PC"})
    assert "- PC: 불안" in narrow and "섬 반대편 인물: 불안" not in narrow


def test_talk_streaming_pace_is_words_per_edit():
    from indaim.adapters.discord import talk as T
    text = " ".join(f"낱말{i}" for i in range(45))
    assert [len(p.split()) for p in T.partials(text, 20)] == [20, 40]             # 편집마다 20낱말씩
    assert T.partials("짧은 안내입니다.", 20) == []                                 # 한 편집 분량이면 바로 전체
    assert T.WORDS / T._env_num("TALK_TICK_SEC", "0.5", float) >= 3 * (35 / 3.6)                                    # v7.36(35낱말 박자·4편집·0.9초)의 세 배 이상


# ── v9.3.3 길 위(TRANSIT) 상호작용 자리 = 구간(사용자 결정 가) ───────────────

async def test_transit_interactions_live_on_the_road_edge(drig):
    import json as _json
    import pathlib as _pl
    from tests.sim.harness import Simulator
    script = _json.loads((_pl.Path(__file__).resolve().parents[1] / "golden" / "scripts" / "균열.json")
                         .read_text(encoding="utf-8"))
    sim = Simulator(drig)
    s = await sim.boot(script)
    ctx = core.player_world.session_world_context(s, intro=True)
    TW, SP = core.turn_world, core.spatial
    here = SP.placement_of(s, ctx.lead_pc)["place_id"]
    eid, e = next((k, v) for k, v in sorted(ctx.definition["movement_edges"].items()) if v["from"] == here)
    now = s.world_clock["minute_index"]
    s.spatial_state["placements"][ctx.lead_pc] = {
        "kind": SP.TRANSIT, "movement_id": "m1", "edge_id": eid, "from_place": e["from"], "to_place": e["to"],
        "edge_started_at_minute": now - 1, "edge_expected_end_minute": now + 5}
    road = TW.pc_place(ctx, s)
    assert road == TW.road_key(eid) and TW.is_road(road)                                # 양방향 공통 구간 자리
    assert TW.road_key(eid.replace("#fwd", "#rev")) == road
    assert TW.pc_place_name(ctx, s, road).endswith("사이 길") and "road:" not in TW.pc_place_name(ctx, s, road)
    pc = TW._session_pc_name(s)
    s.resources.setdefault(pc, {})["밧줄"] = 1                                            # 길 위에 내려놓기 → 그 구간 현장 물건
    sim_ = core.items.run_ops(s, [{"op": "DROP", "from": pc, "stock": "밧줄", "qty": 1}], pc_name=pc,
                              scene_place=road, transaction_id="tx-road")
    assert any(r.get("place") == road for r in sim_.places.values())
    s.place_items = dict(sim_.places)
    m = core.items.build_manifest(s, pc_name=pc, scene_place=TW.scene_npc_projections(ctx, None, s)[0])
    assert [p[3] for p in m.places] == ["밧줄"]                                             # 다시 지나면 보인다(L키)
    rec = TW.ambient_record("rt.ambient.road1", "녹슨 상자", "OBJECT", road, TW.pc_place_name(ctx, s, road), now)
    s.information_state.setdefault("runtime_definitions", {})
    assert "녹슨 상자" in rec["summary"] and f"place:{road}" in rec["subject_refs"]
    from indaim.app import io as _io, world_codec as _codec
    _codec.encode(_io._serialize_session(s), getattr(s, "scenario_data", None))         # v5 저장 왕복(커널 세계) 통과
    npc = next(a for a, d in sorted(ctx.definition["actor_definitions"].items()) if (d or {}).get("kind") == "NPC")
    rev = next((k for k in ctx.definition["movement_edges"] if k.split("#")[0] == eid.split("#")[0] and k != eid), eid)
    s.spatial_state["placements"][npc] = dict(s.spatial_state["placements"][ctx.lead_pc], edge_id=rev)
    scene, projs = TW.scene_npc_projections(ctx, None, s)
    assert scene == road and [p["actor_id"] for p in projs] == [npc]                      # 같은 길(반대 방향 포함)의 인물만
