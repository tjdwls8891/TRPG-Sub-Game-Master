# -*- coding: utf-8 -*-
"""턴 시뮬레이터 — 실제 genesis + 실제 자동 턴(판단·지시·묘사·추출은 가짜 provider) + READY + 커밋.

대본(script)을 돌려 턴마다 '의미 스냅샷'(저장 형식과 무관한 플레이어·정본 관점의 사실)을 만든다. 리팩터링 동안
골든(tests/golden)의 기준이며, 새 엔진으로 옮긴 뒤에도 같은 대본·같은 스냅샷이 나와야 한다.

대본 형식(JSON):
  {"scenario": id, "frame": 시작 틀 id?, "pc": {"name", "profile": {...}, "bag": {...}},
   "turns": [{"say": 선언,
              "intent": {"place": 장소 PlaceId | "wait": 분 | "resolution": 결단 이름 일부 | "growth": 키},
              "roll": [판단 rolls 항목…]?,           # 있으면 ROLL 턴 — 주사위는 seed로 결정적
              "seed": 정수?,
              "extraction": 추출 응답 사전?,
              "set": {"statuses": {...}, "status_clock": {...}}?}]}   # 턴 전에 세션에 주입(시험 조건)
"""
from __future__ import annotations

import copy
import json
import pathlib
import random
import re

from core import events as EV
from core import npc_position as NP
from core import player_world as PW
from core import spatial as SP
from core import world_lifecycle as WL
from tests.conftest import REPO_ROOT
from tests.policy.test_h05bc_turn_wiring import H05Provider, _json, extraction, turn

ENGINE_LOCAL_PREFIXES = (EV.ACTOR_STATUS_PREFIX, EV.ACTOR_STATUS_CLEAR_PREFIX, EV.PARTY_PREFIX,
                         EV.GROWTH_GRANT_PREFIX, EV.PC_RECORD_PREFIX, EV.ITEM_GRANT_PREFIX)
SYNTH_TEMPLATES = (EV.COMBAT_TEMPLATE,)
KEEP_LINE = re.compile(r"^(> 🎲|> ⚔️|어느 길|> ✅|> ❌)")


def _judge(step):
    intent, rolls = step.get("intent") or {}, step.get("roll")

    def route(kwargs):
        text = H05Provider._text(kwargs)
        ai = {"movement_kind": "NONE", "time_kind": "NONE"}
        if intent.get("place"):
            m = re.search(r"(P\d+)=" + re.escape(intent["place"]) + r"\b", text)
            ai.update({"movement_kind": "DESTINATION", "destination": m.group(1) if m else "NONE"})
        if intent.get("wait"):
            ai.update({"time_kind": "DURATION", "duration_minutes": int(intent["wait"]), "activity": "WAIT"})
        if intent.get("resolution"):
            m = re.search(r"(D\d+)=[^,\n]*" + re.escape(intent["resolution"]), text)
            ai["resolution"] = m.group(1) if m else "NONE"
        body = {"player_intent": step.get("say", ""), "situation_read": "상황", "reasoning": "대본",
                "action": "ROLL" if rolls else "PROCEED", "bridge_message": "", "rolls": list(rolls or []),
                "action_intent": ai, "action_minutes": int(step.get("minutes", 1))}
        if intent.get("growth"):
            body["growth"] = intent["growth"]
        return _json(body)
    return route


class Simulator:
    """rig(drig 픽스처) 위에서 대본을 돌린다."""

    def __init__(self, rig):
        self.r = rig

    async def boot(self, script):
        raw = json.loads(pathlib.Path(REPO_ROOT, "scenarios", f"{script['scenario']}.json").read_text(encoding="utf-8"))
        s = self.r.sess
        s.scenario_id, s.scenario_data = script["scenario"], raw
        s.npcs, s.resources, s.statuses = {}, {}, {}
        s.expand_default_npcs(replace=True)
        uid = next(iter(s.players))
        pc = script.get("pc") or {}
        s.players[uid]["name"] = pc.get("name", "테스터")
        s.players[uid]["profile"] = copy.deepcopy(pc.get("profile") or {})
        s.resources[s.players[uid]["name"]] = copy.deepcopy(pc.get("bag") or {})
        frames = raw.get("start_frames") or [{}]
        frame = next((f for f in frames if f.get("id") == script.get("frame")), frames[0])
        res = await WL.initialize_world(self.r.bot, s, frame)
        assert res.outcome == WL.INIT_OK, res.reason
        WL.mark_open(s)
        PW._DEF_CACHE.clear()
        self.pc = s.players[uid]["name"]
        return s

    async def step(self, step):
        s, r = self.r.sess, self.r
        for k, v in (step.get("set") or {}).items():
            cur = copy.deepcopy(getattr(s, k, None) or {})
            cur.update(copy.deepcopy(v))
            setattr(s, k, cur)
        r.prov.routes["judgment"] = _judge(step)
        r.prov.routes["extraction"] = [extraction(step["extraction"]) if step.get("extraction") is not None
                                       else extraction({})]
        sent0, calls0 = len(r.gch.sent), len(r.prov.calls)
        random.seed(int(step.get("seed", 0)))
        await turn(r, step.get("say", "선언"))
        if step.get("roll"):
            import cogs.gm as GM
            view = next((m.view for m in reversed(r.gch.sent) if isinstance(m.view, GM.GMRollView)), None)
            assert view is not None, "판정 화면이 없습니다"
            random.seed(int(step.get("seed", 0)))
            results = await r.gm._execute_rolls(s, view.roll_specs, r.gch, transaction_id=view.transaction_id)
            await r.gm._continue_with_roll_results(s, view.player_message, view.prior_roll_results + results,
                                                   transaction_id=view.transaction_id)
        calls = r.prov.calls[calls0:]
        out = [] if "narration" in calls or step.get("roll") else ["[질문] " + str(getattr(m, "content", ""))[:120]
                                                                    for m in r.gch.sent[sent0:][-1:]]
        for m in r.gch.sent[sent0:]:
            out += [f"[카드] {e.title}" for e in m.embeds if getattr(e, "title", None)]
            for line in str(m.content or "").splitlines():
                if KEEP_LINE.match(line.strip()):
                    out.append(line.strip())
        return {"view": semantic_view(s), "out": out, "calls": calls}

    async def run(self, script):
        await self.boot(script)
        return [{"say": st.get("say", ""), **(await self.step(st))} for st in script["turns"]]


def semantic_view(s) -> dict:
    """저장 형식과 무관한 의미 스냅샷(정렬·정규화)."""
    D = PW.load_definition(s, s.definition_revision)          # 닫힌 세션(결말)에서도 같은 정의로 본다
    lead = next(a for a, r in (s.actor_registry or {}).items() if (r or {}).get("kind") == "PC")
    acts = D.get("actor_definitions") or {}
    name_of = {aid: (a.get("display_name") or a.get("legacy_key")) for aid, a in acts.items()}
    pc_name = next(iter(s.players.values())).get("name")
    pc_pl = SP.placement_of(s, lead) or {}
    actors = {}
    marks = EV.actor_status_marks(s.event_instances)
    for aid in sorted(acts):
        name, key = name_of[aid], acts[aid].get("legacy_key")
        row = {"place": NP.effective_place(D, s, aid)}
        if aid in marks:
            row["removed_by"] = sorted(marks[aid])
        sts = sorted((s.statuses or {}).get(key) or ())
        if sts:
            row["statuses"] = sts
            stg = ((s.status_clock or {}).get(key) or {}).get("stage") or {}
            if stg:
                row["stages"] = {k: stg[k] for k in sorted(stg)}
        bag = {k: v for k, v in sorted(((s.resources or {}).get(key) or {}).items()) if v}
        if bag:
            row["bag"] = bag
        if any(v is not None for k, v in row.items()) or len(row) > 1:
            actors[name] = row
    prof = next(iter(s.players.values())).get("profile") or {}
    pc_clock = (s.status_clock or {}).get(pc_name) or {}
    pc = {"place": pc_pl.get("place_id") if pc_pl.get("kind") == SP.AT_PLACE else pc_pl.get("kind"),
          "statuses": sorted((s.statuses or {}).get(pc_name) or ()),
          "stages": {k: v for k, v in sorted((pc_clock.get("stage") or {}).items())},
          "bag": {k: v for k, v in sorted(((s.resources or {}).get(pc_name) or {}).items()) if v},
          "equipment": copy.deepcopy((s.equipment or {}).get(pc_name) or {}),
          "profile": {k: prof[k] for k in sorted(prof) if isinstance(prof[k], (int, float))}}
    events = {}
    for inst in sorted(s.event_instances.values(), key=lambda i: (i["template_id"], i["occurrence_ordinal"])):
        if inst["template_id"] in SYNTH_TEMPLATES:
            continue
        loc = {k: v for k, v in sorted((inst.get("local_state") or {}).items())
               if not k.startswith(ENGINE_LOCAL_PREFIXES)}
        events.setdefault(inst["template_id"], []).append(
            {"phase": inst["current_phase"], "lifecycle": inst["lifecycle"], **({"vars": loc} if loc else {})})
    cog = (s.cognition_state or {}).get("actor:" + lead) or {}
    return {
        "minute": int(s.world_clock["minute_index"]) - int(s.world_clock["start_minute_index"]),
        "lifecycle": s.world_lifecycle,
        "pc": pc,
        "companions": sorted(s.companions or ()),
        "actors": actors,
        "events": events,
        "floor": sorted([v["place"], v["key"], v["quantity"]] for v in (s.place_items or {}).values()),
        "access": {k: v.get("state") for k, v in sorted((s.access_control_state or {}).items())},
        "pc_knows": sorted(i for i, rec in cog.items() if rec.get("awareness") == "CONTENT"),
        "relations": len(((s.relationship_state or {}).get("deltas") or {})),
    }
