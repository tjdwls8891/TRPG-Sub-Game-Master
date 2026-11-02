# -*- coding: utf-8 -*-
"""옛 세션(v4) → 새 커널 세계 변환 — 네 시나리오 골든의 매 턴에서 왕복 무손실(장부 키 제외)."""
from __future__ import annotations

import copy
import json
import pathlib

import pytest

from core import events as EV
from core import growth as GR
from core import judgment as JG
from indaim.migrate import v4
from tests.policy.test_h05bc_turn_wiring import drig  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.sim.harness import Simulator

pytestmark = pytest.mark.policy
GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "scripts"


def game_keys(sd: dict) -> set:
    g = sd.get("growth") or {}
    return set(sd.get("ability_stats") or ()) | {JG.SKILL_FIELD} | \
        ({GR.POINTS_FIELD} | set(g.get("records") or ()) if g else set())


def old_fields(s) -> dict:
    return copy.deepcopy({f: getattr(s, f) for f in v4.FIELDS})


def check(s):
    old = old_fields(s)
    conv = v4.convert(old, game_keys=game_keys(s.scenario_data))
    assert conv.issues == []
    assert v4.revert(conv) == v4.strip_ledger(old)          # 사건 인물 상태 표식은 인물로 옮겨졌다가 그대로 되살아난다
    assert EV.actor_status_marks(v4.revert(conv)["event_instances"]) == EV.actor_status_marks(s.event_instances)
    return conv


@pytest.mark.parametrize("name", sorted(p.stem for p in GOLDEN.glob("*.json")))
async def test_roundtrip_every_turn(drig, name):
    script = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    sim = Simulator(drig)
    await sim.boot(script)
    check(drig.sess)
    for st in script["turns"]:
        await sim.step(st)
        check(drig.sess)


def test_unresolved_names_kept_verbatim():
    old = {f: {} for f in v4.FIELDS}
    old.update({"companions": ["유령"], "met_npcs": [], "world_pending_signals": [], "world_version": None,
                "statuses": {"유령": ["부상"]}, "stat_fail_counts": {"유령:근력": 2},
                "actor_registry": {"a1": {"kind": "NPC", "display_name": "갑", "source_key": "갑", "aliases": []},
                                   "a2": {"kind": "NPC", "display_name": "갑", "source_key": "을", "aliases": []}},
                "resources": {"갑": {"칼": 1}}})
    conv = v4.convert(old, game_keys=set())
    assert any("모호한" in i for i in conv.issues) and any("풀리지 않는" in i for i in conv.issues)
    assert v4.revert(conv) == v4.strip_ledger(old)


def test_revert_keeps_game_keys_added_after_conversion():
    """커널 커밋이 프로필에 새 게임 수치 키를 더하면(첫 성장 포인트 등) 역변환이 그 키를 잃지 않는다(T-6 발견)."""
    from indaim.kernel import EffectBatch, View, make_effect, project
    from indaim.migrate import ops_v4, v4
    old = {"players": {"u1": {"name": "가", "profile": {"근력": 12, "외형": "키 큼"}}},
           "actor_registry": {"pc.1": {"kind": "PC", "display_name": "가", "source_kind": "PLAYER", "source_key": "u1",
                                       "aliases": []}}}
    conv = v4.convert(old, game_keys={"근력", "성장 포인트"})
    reg = ops_v4.registry()
    b = EffectBatch(batch_id="b", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    b.add(make_effect("e", "actor", "pc.1", "stats", "set_profile", {"value": {"근력": 12, "성장 포인트": 5}},
                      cause="SETTLE", by="t"))
    b.freeze()
    res = project(conv.world, b, reg, None)
    out = v4.revert(v4.Converted(world=res.world, profile=conv.profile))
    assert out["players"]["u1"]["profile"] == {"근력": 12, "외형": "키 큼", "성장 포인트": 5}
    assert View(res.world, reg, None).read("actor", "pc.1", "stats")["profile"]["성장 포인트"] == 5
