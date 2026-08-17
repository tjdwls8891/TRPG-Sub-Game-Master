# 퀘스트 시스템 — 서사 설계의 기본 구조
#
# [온디맨드 주입]
#   퀘스트 틀 전량을 캐시에 구우면 시나리오 10만 토큰 목표와 충돌한다.
#   폐지된 키워드북이 쓰던 온디맨드 슬롯을 퀘스트가 대체한다.
#
#   퀘스트 없음   → 필터링된 '가능 퀘스트 목록'을 주입, 지시층위가 선택
#   퀘스트 진행 중 → 선택된 퀘스트 하나만 주입 (토큰 절감)
#
# [중복 차단은 이름으로만]
#   같은 사건에 다른 이면정보를 가진 버전을 여럿 두되, 중복 발생 차단은
#   name 기준이다(기획 규정). 버전이 달라도 같은 이름이면 반복되지 않는다.
import json
import os
import random

# 퀘스트 데이터 파일 접미사. scenarios/{scenario_id}.quests.json
QUEST_FILE_SUFFIX = ".quests.json"

# 목록 주입 시 제시할 후보 수. 너무 많으면 토큰이 늘고 선택이 흐려진다.
CANDIDATE_LIMIT = 4

_cache = {}


def load_quest_data(scenario_id: str) -> dict:
    """시나리오의 퀘스트 데이터를 읽는다. 파일 단위로 캐시한다."""
    if not scenario_id:
        return {}
    if scenario_id in _cache:
        return _cache[scenario_id]
    path = os.path.join("scenarios", f"{scenario_id}{QUEST_FILE_SUFFIX}")
    if not os.path.exists(path):
        _cache[scenario_id] = {}
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[퀘스트] 로드 실패 ({scenario_id}): {e}")
        data = {}
    _cache[scenario_id] = data
    return data


def get_state(session) -> dict:
    """퀘스트 진행 상태. 없으면 초기화한다."""
    st = getattr(session, "quest_state", None)
    if not isinstance(st, dict) or not st:
        st = {"active": None, "cleared": [], "known_secrets": [], "occurrences": {}}
        session.quest_state = st
    st.setdefault("active", None)
    st.setdefault("cleared", [])
    st.setdefault("known_secrets", [])
    st.setdefault("occurrences", {})
    return st


def _passes_filter(session, quest: dict, state: dict) -> bool:
    """퀘스트 조건을 만족하는지. 조건이 없으면 통과."""
    f = quest.get("filters") or {}

    turn = getattr(session, "turn_count", 0) or 0
    if f.get("min_turn") and turn < f["min_turn"]:
        return False
    if f.get("max_turn") and turn > f["max_turn"]:
        return False

    # 위치 조건 — 추출층위가 갱신한 현재 위치를 본다.
    locs = f.get("location")
    if locs:
        cur = (getattr(session, "world_timeline", {}) or {}).get("current_location") or ""
        if not any(l in cur for l in locs):
            return False

    # 세력 조건 — 세션에 소속이 기록돼 있으면 대조한다.
    factions = f.get("faction")
    if factions:
        cur = (getattr(session, "world_timeline", {}) or {}).get("faction_context") or ""
        player_faction = getattr(session, "player_faction", "") or ""
        if not any(x in cur or x == player_faction for x in factions):
            return False

    # 능력치 조건
    for stat, need in (f.get("min_stat") or {}).items():
        if _player_stat(session, stat) < need:
            return False

    # 선행 퀘스트
    cleared_names = {c.get("name") for c in state["cleared"]}
    for req in (f.get("requires_cleared") or []):
        if req not in cleared_names:
            return False

    # 메인라인 — 서브 클리어 수 조건
    if quest.get("line") == "main":
        need = f.get("min_cleared_sub") or 0
        subs = sum(1 for c in state["cleared"] if c.get("line") == "sub")
        if subs < need:
            return False

    return True


def _player_stat(session, stat: str) -> int:
    """플레이어 스탯 값. 여럿이면 최대값을 본다."""
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


def _repeat_ok(quest: dict, state: dict) -> bool:
    """반복 가능 여부. 중복 차단은 이름 기준이다(기획 규정)."""
    name = quest.get("name")
    seen = state["occurrences"].get(name, 0)
    if not quest.get("repeatable"):
        return seen == 0
    return seen < int(quest.get("max_occurrences") or 1)


def filter_available(session, *, limit: int = CANDIDATE_LIMIT) -> list:
    """조건에 맞는 퀘스트 후보를 추린다.

    같은 이름의 다른 버전이 여럿이면 그중 하나만 후보에 올린다.
    이름 기준 중복 차단이므로 버전끼리 경쟁시킬 이유가 없다.
    """
    data = load_quest_data(getattr(session, "scenario_id", ""))
    quests = data.get("quests") or []
    state = get_state(session)

    by_name = {}
    for q in quests:
        if not _repeat_ok(q, state) or not _passes_filter(session, q, state):
            continue
        by_name.setdefault(q.get("name"), []).append(q)

    picked = [random.choice(v) for v in by_name.values()]
    # 메인라인이 조건을 만족하면 우선 노출한다.
    picked.sort(key=lambda q: 0 if q.get("line") == "main" else 1)
    return picked[:limit]


def fill_slots(session, quest: dict) -> dict:
    """가변정보를 시나리오 목록에서 채운다.

    'pick': '1-2' 같은 개수 랜덤도 지원한다(기획 규정).
    """
    from .profile_gen import resolve_pick_count

    data = load_quest_data(getattr(session, "scenario_id", ""))
    pools = data.get("quest_slots") or {}
    out = {}
    for key, spec in (quest.get("slots") or {}).items():
        if not isinstance(spec, dict):
            continue
        pool = pools.get(spec.get("from")) or []
        if not pool:
            continue
        n = min(resolve_pick_count(spec.get("pick", 1)), len(pool))
        chosen = random.sample(list(pool), max(1, n))
        out[key] = chosen[0] if n <= 1 else chosen
    return out


def _apply_slots(text: str, slots: dict) -> str:
    """가이드 문구의 {슬롯}을 채운다."""
    if not text:
        return ""
    for k, v in (slots or {}).items():
        val = ", ".join(v) if isinstance(v, list) else str(v)
        text = text.replace("{" + k + "}", val)
    return text


def start_quest(session, quest: dict) -> dict:
    """퀘스트를 활성화한다. 슬롯을 채우고 root 노드에서 시작한다."""
    state = get_state(session)
    slots = fill_slots(session, quest)
    state["active"] = {
        "id": quest.get("id"),
        "name": quest.get("name"),
        "version": quest.get("version"),
        "line": quest.get("line"),
        "node": "root",
        "slots": slots,
        "path": [],
        "secret_known": False,
    }
    name = quest.get("name")
    state["occurrences"][name] = state["occurrences"].get(name, 0) + 1
    return state["active"]


def _find_quest(session, quest_id: str) -> dict | None:
    data = load_quest_data(getattr(session, "scenario_id", ""))
    for q in (data.get("quests") or []):
        if q.get("id") == quest_id:
            return q
    return None


def build_quest_block(session) -> str:
    """지시층위에 주입할 퀘스트 블록.

    활성 퀘스트가 있으면 그것만, 없으면 후보 목록을 주입한다.
    선택이 확정되면 다음 턴부터 해당 퀘스트만 실린다(토큰 절감).
    """
    state = get_state(session)
    active = state.get("active")

    if active:
        quest = _find_quest(session, active["id"])
        if not quest:
            return ""
        tree = quest.get("tree") or {}
        node = tree.get(active["node"]) or {}
        slots = active.get("slots") or {}

        lines = [
            "\n[진행 중인 퀘스트 — 서사 가이드]",
            f"이름: {active['name']}",
            f"가이드: {_apply_slots(node.get('guide'), slots)}",
        ]
        if active.get("path"):
            lines.append(f"지나온 경로: {' → '.join(active['path'])}")

        cases = node.get("cases") or {}
        if cases:
            lines.append("다음 전개 후보 (플레이어 행동에 따라 갈림):")
            for key, case in cases.items():
                lines.append(f"  - {key}: {case.get('condition', '')}")

        hidden = quest.get("hidden") or {}
        if hidden:
            if active.get("secret_known"):
                lines.append(f"※ 플레이어가 이면정보를 알아챘다: {hidden.get('if_known', '')}")
            else:
                lines.append(f"※ 플레이어가 모르는 사실이 있다(누설 금지): {hidden.get('truth', '')}")
                lines.append(f"   알아채지 못한 채 진행되면: {hidden.get('if_unknown', '')}")
        lines.append("이 가이드를 참고하되 플레이어의 선택을 강요하지 말 것.\n")
        return "\n".join(lines)

    candidates = filter_available(session)
    if not candidates:
        return ""

    lines = ["\n[가능 퀘스트 목록 — 하나를 선택하거나 선택하지 않을 수 있다]"]
    for q in candidates:
        slots = fill_slots(session, q)
        root = (q.get("tree") or {}).get("root") or {}
        lines.append(
            f"  - id={q.get('id')} | {q.get('name')} ({q.get('line')})\n"
            f"    {_apply_slots(root.get('guide'), slots)}"
        )
    lines.append(
        "지금 상황에 어울리는 것이 있으면 quest_choice.id에 그 id를 적고,\n"
        "없으면 빈 문자열을 적을 것. 억지로 고르지 말 것.\n"
    )
    return "\n".join(lines)


def advance_quest(session, extraction: dict) -> dict | None:
    """추출층위 수치로 케이스를 진전시킨다.

    기획 규정 — 추출층위 판단상 특정 케이스로 진전될 것으로 결론나면
    단계를 진전한 프롬프트를 지시층에 부여한다.

    Returns:
        {"moved": bool, "node": str, "outcome": str|None, "replan": bool}
    """
    from .extraction import get_thresholds

    state = get_state(session)
    active = state.get("active")
    if not active:
        return None

    qp = (extraction or {}).get("quest_progress") or {}
    try:
        advance = int(qp.get("advance", 0))
        deviation = int(qp.get("deviation", 0))
    except (TypeError, ValueError):
        return None

    th = get_thresholds(session)
    quest = _find_quest(session, active["id"])
    if not quest:
        return None
    tree = quest.get("tree") or {}
    node = tree.get(active["node"]) or {}
    cases = node.get("cases") or {}

    # 이탈이 크면 케이스를 진전시키지 않고 재계획을 요청한다.
    if deviation >= th["quest_deviated"]:
        return {"moved": False, "node": active["node"], "outcome": None, "replan": True}

    if advance < th["quest_advance"] or not cases:
        return {"moved": False, "node": active["node"], "outcome": None, "replan": False}

    # 진전 — 다음 노드로. 어느 케이스인지는 묘사 내용이 정하므로
    # 여기서는 첫 케이스를 기본으로 두고, 호출부가 지정할 수 있게 한다.
    next_key = next(iter(cases))
    next_node = cases[next_key].get("next")
    if next_node not in tree:
        return {"moved": False, "node": active["node"], "outcome": None, "replan": False}

    active["path"].append(next_key)
    active["node"] = next_node
    outcome = (tree.get(next_node) or {}).get("outcome")

    if outcome:
        state["cleared"].append({
            "name": active["name"], "version": active["version"],
            "line": active["line"], "outcome": outcome,
            "turn": getattr(session, "turn_count", 0),
        })
        state["active"] = None

    return {"moved": True, "node": next_node, "outcome": outcome, "replan": False}


def move_to_case(session, case_key: str) -> bool:
    """지시층위가 지정한 케이스로 진전시킨다."""
    state = get_state(session)
    active = state.get("active")
    if not active:
        return False
    quest = _find_quest(session, active["id"])
    tree = (quest or {}).get("tree") or {}
    cases = (tree.get(active["node"]) or {}).get("cases") or {}
    case = cases.get(case_key)
    if not case or case.get("next") not in tree:
        return False

    active["path"].append(case_key)
    active["node"] = case["next"]
    outcome = (tree.get(case["next"]) or {}).get("outcome")
    if outcome:
        state["cleared"].append({
            "name": active["name"], "version": active["version"],
            "line": active["line"], "outcome": outcome,
            "turn": getattr(session, "turn_count", 0),
        })
        state["active"] = None
    return True


def mark_secret_known(session) -> bool:
    """플레이어가 이면정보를 알아챘음을 기록한다."""
    state = get_state(session)
    active = state.get("active")
    if not active:
        return False
    active["secret_known"] = True
    if active["name"] not in state["known_secrets"]:
        state["known_secrets"].append(active["name"])
    return True


def check_main_unlock(session) -> list:
    """메인라인 후보를 반환한다."""
    return [q for q in filter_available(session, limit=99)
            if q.get("line") == "main"]


def summary(session) -> str:
    """디스플레이 표기용 퀘스트 정보."""
    state = get_state(session)
    active = state.get("active")
    cleared = len(state.get("cleared") or [])
    if not active:
        return f"진행 중 없음 · 완료 {cleared}건"
    return (f"**{active['name']}** ({active['node']}) · "
            f"완료 {cleared}건")
