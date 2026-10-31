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
    assert v4.revert(conv) == v4.strip_ledger(old)
    marks = EV.actor_status_marks(s.event_instances)
    got = {aid: c["vitals"]["by_event"] for aid, c in conv.world.to_dict()["entities"].get("actor", {}).items()
           if "by_event" in c.get("vitals", {})}
    assert got == marks
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
