"""옛 세션 필드(SCHEMA_VERSION 4) → 새 커널 세계(엔티티·컴포넌트) 변환과 그 역변환.

· 키는 모두 ID로 바꾼다. 이름 → ActorId는 행위자 등록부(PC = 플레이어 이름, NPC = source_key·표시명·별칭)로만
  해석하고 추정하지 않는다 — 풀리지 않거나 모호한 이름은 `world.unresolved`에 원문 그대로 보관하고 issue로 알린다.
· 사건 지역 값의 엔진 장부 키(인물 상태·해제·파티·성장 지급·PC 기록·물건 지급)는 이미 다른 필드에 반영된 사본이라
  버린다. 단 사건이 붙인 인물 상태(제거 판정의 근거)는 `actor.vitals.by_event`로 옮긴다.
· 그 밖의 값은 잃지 않는다 — `revert(convert(x))`가 장부 키를 뺀 x와 같아야 한다(골든 왕복 테스트).
· 정의 기본값 기반 압축(인지·공간 지식 시드)은 해당 기능 모듈 이식 때 기본값 함수와 함께 들어온다(K-d).
"""
import copy
from dataclasses import dataclass, field

from indaim.kernel import World

LEDGER_PREFIXES = ("actor_status:", "actor_status_clear:", "party:", "growth_grant:", "pc_record:", "item_grant:")
NAME_FIELDS = ("statuses", "status_clock", "resources", "equipment")      # {이름: 값}
NAME_LISTS = ("companions", "met_npcs")                                  # [이름]
FIELDS = ("world_version", "world_clock", "actor_registry", "spatial_state", "spatial_knowledge",
          "access_control_state", "event_instances", "world_schedule", "information_state",
          "cognition_state", "relationship_state", "world_pending_signals", "item_instances",
          "place_items", "item_traits", "stat_fail_counts", "players") + NAME_FIELDS + NAME_LISTS
EVENT_CORE_SKIP = ("local_state", "history")


@dataclass
class Converted:
    world: World
    profile: dict = field(default_factory=dict)       # {uid: {name, narrative, fields}} — 세계 밖(프로필 폼 소유)
    issues: list = field(default_factory=list)


def _names(old):
    """이름 → ActorId(유일할 때만), ActorId → 정규 이름(옛 필드가 쓰던 키)."""
    reg = old.get("actor_registry") or {}
    players = old.get("players") or {}
    canon, cands = {}, {}
    for aid, r in sorted(reg.items()):
        if r.get("kind") == "PC":
            name = (players.get(str(r.get("source_key"))) or {}).get("name") or r.get("display_name")
        else:
            name = r.get("source_key") or r.get("display_name")
        canon[aid] = name
        for n in {name, r.get("display_name"), *(r.get("aliases") or ())} - {None, ""}:
            cands.setdefault(n, set()).add(aid)
    return {n: next(iter(a)) for n, a in cands.items() if len(a) == 1}, canon, {n for n, a in cands.items() if len(a) > 1}


def convert(old: dict, *, game_keys) -> Converted:
    """old = 세션 필드 사전(FIELDS). game_keys = PC 프로필 중 게임 수치 키(스탯·스킬·성장 기록 — 정의에서)."""
    old = copy.deepcopy(old)
    to_aid, _canon, ambiguous = _names(old)
    issues, unresolved = [], {}
    ents = {k: {} for k in ("actor", "place", "item", "info", "event")}
    wl = {}

    def put(kind, eid, comp, value):
        ents[kind].setdefault(eid, {})[comp] = value

    def aid_of(name, where):
        if name in to_aid:
            return to_aid[name]
        issues.append(f"{where}: {'모호한' if name in ambiguous else '풀리지 않는'} 이름 {name!r}")
        return None

    # ── 세계 단위 ──
    for src, comp in (("world_clock", "clock"), ("world_schedule", "schedule"), ("access_control_state", "access"),
                      ("item_traits", "item_lore")):
        if old.get(src):
            wl[comp] = old[src]
    if old.get("world_pending_signals"):
        wl["pending_signals"] = old["world_pending_signals"]
    sp = old.get("spatial_state") or {}
    if set(sp) - {"placements", "scene_place", "placement_holds"}:
        raise ValueError(f"spatial_state 알 수 없는 키 {sorted(set(sp) - {'placements', 'scene_place', 'placement_holds'})}")
    if "scene_place" in sp:
        wl["scene"] = {"place": sp["scene_place"]}
    for aid, v in (sp.get("placements") or {}).items():
        put("actor", aid, "pos", v)
    for aid, v in (sp.get("placement_holds") or {}).items():
        put("actor", aid, "hold", v)
    # ── 행위자 ──
    for aid, r in (old.get("actor_registry") or {}).items():
        put("actor", aid, "reg", r)
    for aid, v in (old.get("spatial_knowledge") or {}).items():
        put("actor", aid, "map", v)
    group_mind, group_bond = {}, {}
    for holder, v in (old.get("cognition_state") or {}).items():
        if holder.startswith("actor:"):
            put("actor", holder[len("actor:"):], "mind", v)
        else:
            group_mind[holder] = v
    for key, rec in (old.get("relationship_state") or {}).items():
        h = rec.get("from_holder") or ""
        if h.startswith("actor:"):
            ents["actor"].setdefault(h[len("actor:"):], {}).setdefault("bond", {}).setdefault("relations", {})[key] = rec
        else:
            group_bond[key] = rec
    if group_mind:
        wl["group_mind"] = group_mind
    if group_bond:
        wl["group_bond"] = group_bond
    sub = {"statuses": "statuses", "status_clock": "clock", "resources": "items", "equipment": "equipped"}
    comp_of = {"statuses": "vitals", "status_clock": "vitals", "resources": "bag", "equipment": "bag"}
    for f in NAME_FIELDS:
        for name, v in (old.get(f) or {}).items():
            aid = aid_of(name, f)
            if aid is None:
                unresolved.setdefault(f, {})[name] = v
                continue
            ents["actor"].setdefault(aid, {}).setdefault(comp_of[f], {})[sub[f]] = v
    for f, comp, key in (("companions", "party", "companion"), ("met_npcs", "bond", "met")):
        for i, name in enumerate(old.get(f) or ()):
            aid = aid_of(name, f)
            if aid is None:
                unresolved.setdefault(f, []).append([i, name])
                continue
            ents["actor"].setdefault(aid, {}).setdefault(comp, {})[key] = i
    for k, n in (old.get("stat_fail_counts") or {}).items():
        name, _, stat = k.rpartition(":")
        aid = aid_of(name, "stat_fail_counts") if name else None
        if aid is None:
            unresolved.setdefault("stat_fail_counts", {})[k] = n
            continue
        ents["actor"].setdefault(aid, {}).setdefault("stats", {}).setdefault("fail", {})[stat] = n
    pc_of_uid = {str(r.get("source_key")): aid for aid, r in (old.get("actor_registry") or {}).items() if r.get("kind") == "PC"}
    profile = {}
    for uid, p in (old.get("players") or {}).items():
        prof = p.get("profile") or {}
        game = {k: v for k, v in prof.items() if k in game_keys}
        aid = pc_of_uid.get(str(uid))
        if aid is None:
            issues.append(f"players: 등록되지 않은 플레이어 {uid}")
            game = {}
        elif game:
            ents["actor"].setdefault(aid, {}).setdefault("stats", {})["profile"] = game
        rest = {k: copy.deepcopy(v) for k, v in p.items() if k != "profile"}
        profile[str(uid)] = {**rest, "narrative": {k: v for k, v in prof.items() if k not in game},
                             "order": list(prof)}
    # ── 사건이 붙인 인물 상태(제거 판정 근거) ──
    for aid, ms in _status_marks(old.get("event_instances") or {}).items():
        ents["actor"].setdefault(aid, {}).setdefault("vitals", {})["by_event"] = ms
    # ── 물건·장소 ──
    for iid, r in (old.get("item_instances") or {}).items():
        r = dict(r)
        if r.get("owner"):
            aid = aid_of(r["owner"], "item_instances.owner")
            if aid is None:
                unresolved.setdefault("item_instances", {})[iid] = r
                continue
            r["owner"] = aid
        put("item", iid, "state", r)
    for sid, r in (old.get("place_items") or {}).items():
        ents["place"].setdefault(r.get("place") or "", {}).setdefault("floor", {})[sid] = {k: v for k, v in r.items() if k != "place"}
    if "" in ents["place"]:
        issues.append("place_items: 장소 없는 현장 물건")
        unresolved["place_items"] = ents["place"].pop("")["floor"]
    # ── 정보 ──
    info = old.get("information_state") or {}
    if set(info) - {"runtime_definitions", "status", "series"}:
        raise ValueError("information_state 알 수 없는 키")
    for part in ("runtime_definitions", "status"):
        for iid, v in (info.get(part) or {}).items():
            put("info", iid, "runtime" if part == "runtime_definitions" else "status", v)
    if info.get("series"):
        wl["info_series"] = info["series"]
    # ── 사건 ──
    for iid, inst in (old.get("event_instances") or {}).items():
        put("event", iid, "core", {k: v for k, v in inst.items() if k not in EVENT_CORE_SKIP})
        loc = {k: v for k, v in (inst.get("local_state") or {}).items() if not k.startswith(LEDGER_PREFIXES)}
        if loc:
            put("event", iid, "vars", loc)
        if "history" in inst:
            put("event", iid, "history", inst["history"])
    if unresolved:
        wl["unresolved"] = unresolved
    world = World({"version": old.get("world_version"), "entities": ents, "world": wl})
    return Converted(world, profile, issues)


def _status_marks(instances) -> dict:
    """옛 events.actor_status_marks와 같은 규칙(사건이 붙이고 아직 풀지 않은 인물 상태 {ActorId: {상태: 분}})."""
    rows = []
    for iid in sorted(instances):
        loc = (instances[iid] or {}).get("local_state") or {}
        for k in sorted(loc):
            v = loc[k]
            if not isinstance(v, dict):
                continue
            m = v.get("minute") if isinstance(v.get("minute"), int) else 0
            if k.startswith("actor_status:") and isinstance(v.get("status"), str):
                rows.append((m, iid, 0, k[len("actor_status:"):], v["status"]))
            elif k.startswith("actor_status_clear:"):
                rows.append((m, iid, 1, k[len("actor_status_clear:"):], v.get("status")))
    out = {}
    for _m, _i, clear, aid, st in sorted(rows, key=lambda r: (r[0], r[1], r[2])):
        if clear:
            if st is None:
                out.pop(aid, None)
            else:
                (out.get(aid) or {}).pop(st, None)
        else:
            out.setdefault(aid, {}).setdefault(st, _m)
    return {a: ms for a, ms in out.items() if ms}


def strip_ledger(old: dict) -> dict:
    """비교 기준 — 옛 필드에서 버리기로 한 장부 키만 뺀 사본."""
    old = copy.deepcopy(old)
    for inst in (old.get("event_instances") or {}).values():
        inst["local_state"] = {k: v for k, v in (inst.get("local_state") or {}).items() if not k.startswith(LEDGER_PREFIXES)}
    return old


def revert(conv: Converted) -> dict:
    """새 세계 → 옛 필드(장부 키 제외). 왕복 검증용."""
    d = conv.world.to_dict()
    ents, wl = d.get("entities") or {}, d.get("world") or {}
    actors = ents.get("actor") or {}
    reg = {aid: c["reg"] for aid, c in actors.items() if "reg" in c}
    players_shell = {uid: {k: v for k, v in p.items() if k not in ("narrative", "order")} for uid, p in conv.profile.items()}
    _to, canon, _amb = _names({"actor_registry": reg, "players": players_shell})
    un = wl.get("unresolved") or {}
    out = {f: {} for f in FIELDS}
    out.update({"world_version": d.get("version"), "world_pending_signals": wl.get("pending_signals", []),
                "companions": [], "met_npcs": []})
    for src, comp in (("world_clock", "clock"), ("world_schedule", "schedule"), ("access_control_state", "access"),
                      ("item_traits", "item_lore")):
        out[src] = wl.get(comp, {})
    sp = {}
    if "scene" in wl:
        sp["scene_place"] = wl["scene"]["place"]
    out["cognition_state"] = dict(wl.get("group_mind") or {})
    out["relationship_state"] = dict(wl.get("group_bond") or {})
    sub = {"statuses": ("vitals", "statuses"), "status_clock": ("vitals", "clock"),
           "resources": ("bag", "items"), "equipment": ("bag", "equipped")}
    lists = {"companions": [], "met_npcs": []}
    for aid in sorted(actors):
        c = actors[aid]
        if "pos" in c:
            sp.setdefault("placements", {})[aid] = c["pos"]
        if "hold" in c:
            sp.setdefault("placement_holds", {})[aid] = c["hold"]
        if "reg" in c:
            out["actor_registry"][aid] = c["reg"]
        if "map" in c:
            out["spatial_knowledge"][aid] = c["map"]
        if "mind" in c:
            out["cognition_state"]["actor:" + aid] = c["mind"]
        for key, rec in ((c.get("bond") or {}).get("relations") or {}).items():
            out["relationship_state"][key] = rec
        name = canon.get(aid)
        for f, (comp, k) in sub.items():
            if k in (c.get(comp) or {}):
                out[f][name] = c[comp][k]
        if "companion" in (c.get("party") or {}):
            lists["companions"].append((c["party"]["companion"], name))
        if "met" in (c.get("bond") or {}):
            lists["met_npcs"].append((c["bond"]["met"], name))
        for stat, n in ((c.get("stats") or {}).get("fail") or {}).items():
            out["stat_fail_counts"][f"{name}:{stat}"] = n
    for f in NAME_FIELDS:
        out[f].update(un.get(f) or {})
    for f in NAME_LISTS:
        rows = lists[f] + [tuple(x) for x in un.get(f) or ()]
        out[f] = [n for _i, n in sorted(rows)]
    out["stat_fail_counts"].update(un.get("stat_fail_counts") or {})
    out["spatial_state"] = sp
    for uid, p in conv.profile.items():
        aid = next((a for a, r in reg.items() if r.get("kind") == "PC" and str(r.get("source_key")) == uid), None)
        game = ((actors.get(aid) or {}).get("stats") or {}).get("profile") or {}
        merged = {**p["narrative"], **game}
        prof = {k: merged[k] for k in p["order"] if k in merged}
        out["players"][uid] = {**{k: v for k, v in p.items() if k not in ("narrative", "order")}, "profile": prof}
    for iid, c in (ents.get("item") or {}).items():
        r = dict(c["state"])
        if r.get("owner"):
            r["owner"] = canon.get(r["owner"], r["owner"])
        out["item_instances"][iid] = r
    out["item_instances"].update(un.get("item_instances") or {})
    for pid, c in (ents.get("place") or {}).items():
        for sid, r in (c.get("floor") or {}).items():
            out["place_items"][sid] = {"place": pid, **r}
    out["place_items"].update(un.get("place_items") or {})
    info = {}
    for iid, c in (ents.get("info") or {}).items():
        if "runtime" in c:
            info.setdefault("runtime_definitions", {})[iid] = c["runtime"]
        if "status" in c:
            info.setdefault("status", {})[iid] = c["status"]
    if wl.get("info_series"):
        info["series"] = wl["info_series"]
    out["information_state"] = info
    for iid, c in (ents.get("event") or {}).items():
        inst = dict(c["core"])
        inst["local_state"] = c.get("vars", {})
        inst["history"] = c.get("history", [])
        out["event_instances"][iid] = inst
    return out
