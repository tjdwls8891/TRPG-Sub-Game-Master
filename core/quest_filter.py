# 퀘스트 필터 매칭 — 필터가 곧 슬롯 후보다
#
# [설계]
#   퀘스트는 인스턴스가 아니라 틀이다. 필터에 후보를 나열해 두면,
#   현재 상황과 일치한 값이 그대로 빈칸을 채운다.
#
#     "npc": {"any": ["엄주섭","차봉순","황기영"], "as": "기록자"}
#
#   장부방에서 엄주섭과 있으면 {기록자}=엄주섭,
#   지하 병참 창고에서 차봉순과 있으면 {기록자}=차봉순이 된다.
#   하나의 틀이 여러 곳에서 각기 다른 사건이 된다.
#
# [개연성]
#   필터를 독립적으로 통과시키면 '지하 병참 창고에 엄주섭이 있는' 조합이
#   생긴다. NPC 후보는 현재 장소에 실재하는 인물과 교집합을 먼저 낸다.
#   장소에 없는 인물은 구조적으로 후보가 될 수 없다.
import random

# 조건 연산자
OP_ANY = "any"      # 하나만 일치하면 통과
OP_ALL = "all"      # 전부 있어야 통과
OP_NONE = "none"    # 하나라도 있으면 탈락

# NPC 탐색 범위
SCOPE_HERE = "here"            # 현재 장소 상주자 (기본)
SCOPE_COMPANION = "companion"  # 동행 중인 인물
SCOPE_ANY = "any"              # 둘 다


def _as_list(v) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def _normalize(spec) -> dict:
    """필터 항목을 표준형으로. 문자열이나 배열도 받아들인다.

    "place": "장부방"              → {"any": ["장부방"]}
    "place": ["장부방", "바자르"]   → {"any": [...]}
    "place": {"any": [...], "as": "장소"}  → 그대로
    """
    if isinstance(spec, dict):
        out = dict(spec)
        for op in (OP_ANY, OP_ALL, OP_NONE):
            if op in out:
                out[op] = _as_list(out[op])
        return out
    return {OP_ANY: _as_list(spec)}


def _match(candidates: list, spec: dict) -> list:
    """조건을 만족하는 값 목록. 불통과면 None.

    통과한 값이 슬롯 후보가 되므로 bool이 아니라 목록을 반환한다.
    """
    present = set(candidates)

    banned = spec.get(OP_NONE)
    if banned and (present & set(banned)):
        return None

    need_all = spec.get(OP_ALL)
    if need_all:
        if not set(need_all).issubset(present):
            return None
        return list(need_all)

    want = spec.get(OP_ANY)
    if want:
        hit = [x for x in want if x in present]
        return hit or None

    # 조건이 없으면 통과시키되 슬롯 값은 없다.
    return []


# ── 후보 수집 ────────────────────────────────────────────
def npcs_available(session, scope: str = SCOPE_HERE) -> list:
    """현재 상호작용 가능한 NPC.

    here      현재 장소에 실재하는 인물
    companion 동행 중인 인물
    any       둘 다

    장소 상주자를 보는 이유는 조우 기록만으로는 처음 간 장소에서
    인물군이 발동하지 않기 때문이다.
    """
    from .places import load_places, resolve, get as get_place

    out = []
    if scope in (SCOPE_HERE, SCOPE_ANY):
        places = load_places(getattr(session, "scenario_data", {}) or {})
        tl = getattr(session, "world_timeline", {}) or {}
        cur = resolve(places, tl.get("current_location") or "") if places else None
        if cur:
            node = get_place(places, cur) or {}
            for entry in (node.get("npcs") or []):
                if isinstance(entry, dict) and entry.get("name"):
                    out.append(entry["name"])

    if scope in (SCOPE_COMPANION, SCOPE_ANY):
        for name in (getattr(session, "companions", []) or []):
            if name not in out:
                out.append(name)
    return out


def items_available(session) -> list:
    """플레이어가 소지한 물품 이름."""
    out = []
    for bag in (getattr(session, "resources", {}) or {}).values():
        if isinstance(bag, dict):
            for name, qty in bag.items():
                try:
                    if int(qty) > 0 and name not in out:
                        out.append(name)
                except (TypeError, ValueError):
                    if name not in out:
                        out.append(name)
    return out


def info_available(session) -> list:
    """플레이어가 획득한 정보 항목."""
    ledger = getattr(session, "info_ledger", None)
    if isinstance(ledger, dict):
        return [k for k, v in ledger.items() if v]
    if isinstance(ledger, list):
        return [str(x) for x in ledger]
    return []


def places_in_scope(session) -> tuple:
    """(현재 장소, 상위 경로 포함 범위)."""
    from .places import load_places, resolve, path_of

    places = load_places(getattr(session, "scenario_data", {}) or {})
    tl = getattr(session, "world_timeline", {}) or {}
    cur = resolve(places, tl.get("current_location") or "") if places else None
    if not cur:
        return None, []
    return cur, list(path_of(places, cur)) + [cur]


# ── 통합 매칭 ────────────────────────────────────────────
def match_filters(session, quest: dict, state: dict) -> dict | None:
    """퀘스트 필터를 대조하고 슬롯 값을 함께 반환한다.

    Returns:
        통과하면 {"slots": {이름: 값}}, 불통과면 None
    """
    from .extraction import get_thresholds  # noqa: F401  (임계 참조 여지)

    f = quest.get("filters") or {}
    slots = {}

    # ── 턴 ──
    turn = getattr(session, "turn_count", 0) or 0
    if f.get("min_turn") and turn < f["min_turn"]:
        return None
    if f.get("max_turn") and turn > f["max_turn"]:
        return None

    # ── 장소 ──
    cur_place, scope_places = places_in_scope(session)

    if f.get("place"):
        spec = _normalize(f["place"])
        hit = _match([cur_place] if cur_place else [], spec)
        if hit is None:
            return None
        if hit:
            slots[spec.get("as") or "장소"] = hit[0]

    if f.get("within"):
        spec = _normalize(f["within"])
        hit = _match(scope_places, spec)
        if hit is None:
            return None
        if hit and spec.get("as"):
            slots[spec["as"]] = hit[0]

    # ── 인물 ──
    # 여러 조건을 걸 수 있다. "권한자 한 명과 부랑자 한 명이 함께" 같은 경우다.
    npc_specs = f.get("npc")
    if npc_specs:
        specs = npc_specs if isinstance(npc_specs, list) and \
            npc_specs and isinstance(npc_specs[0], dict) else [npc_specs]
        used = set()
        for raw in specs:
            spec = _normalize(raw)
            pool = [n for n in npcs_available(session, spec.get("scope", SCOPE_HERE))
                    if n not in used]
            hit = _match(pool, spec)
            if hit is None:
                return None
            if hit:
                picked = random.choice(hit)
                used.add(picked)
                slots[spec.get("as") or "인물"] = picked

    # ── 인물·장소 쌍 ──
    # 그 인물이 그 장소에 있어야만 성립하는 틀에 쓴다.
    pairs = f.get("pair")
    if pairs:
        here = npcs_available(session, SCOPE_ANY)
        ok = None
        for pr in pairs:
            if not isinstance(pr, dict):
                continue
            if pr.get("place") in scope_places and pr.get("npc") in here:
                ok = pr
                break
        if not ok:
            return None
        slots.setdefault("인물", ok["npc"])
        slots.setdefault("장소", ok["place"])

    # ── 세력 ──
    player_faction = getattr(session, "player_faction", "") or ""
    tl = getattr(session, "world_timeline", {}) or {}
    if f.get("faction"):
        spec = _normalize(f["faction"])
        hit = _match([player_faction] if player_faction else [], spec)
        if hit is None:
            return None
        if hit:
            slots[spec.get("as") or "세력"] = hit[0]

    if f.get("faction_scope"):
        spec = _normalize(f["faction_scope"])
        ctx = [x for x in (player_faction, tl.get("faction_context")) if x]
        hit = _match(ctx, spec)
        if hit is None:
            return None
        if hit and spec.get("as"):
            slots[spec["as"]] = hit[0]

    # ── 소지품 ──
    if f.get("item"):
        spec = _normalize(f["item"])
        hit = _match(items_available(session), spec)
        if hit is None:
            return None
        if hit:
            slots[spec.get("as") or "물품"] = random.choice(hit)

    # ── 획득 정보 ──
    if f.get("info"):
        spec = _normalize(f["info"])
        hit = _match(info_available(session), spec)
        if hit is None:
            return None
        if hit and spec.get("as"):
            slots[spec["as"]] = hit[0]

    # ── 시간대 ──
    if f.get("time_of_day"):
        spec = _normalize(f["time_of_day"])
        hit = _match([tl.get("time_of_day") or ""], spec)
        if hit is None:
            return None
        if hit and spec.get("as"):
            slots[spec["as"]] = hit[0]

    # ── 능력치 ──
    for stat, need in (f.get("min_stat") or {}).items():
        if _player_stat(session, stat) < need:
            return None

    # ── 선행 퀘스트 ──
    cleared = {c.get("name") for c in (state.get("cleared") or [])}
    for req in _as_list(f.get("requires_cleared")):
        if req not in cleared:
            return None
    for banned in _as_list(f.get("blocks_if_cleared")):
        if banned in cleared:
            return None

    # ── 메인라인 ──
    if quest.get("line") == "main":
        from .quest import plan_allows_main
        if not plan_allows_main(session):
            return None
        need = f.get("min_cleared_sub") or 0
        subs = sum(1 for c in (state.get("cleared") or []) if c.get("line") == "sub")
        if subs < need:
            return None

    return {"slots": slots}


def _player_stat(session, stat: str) -> int:
    """플레이어 스탯. 여럿이면 최대값."""
    best = 0
    for p in (getattr(session, "players", {}) or {}).values():
        if not isinstance(p, dict):
            continue
        profile = p.get("profile")
        if isinstance(profile, dict):
            try:
                best = max(best, int(profile.get(stat, 0)))
            except (TypeError, ValueError):
                continue
    return best
