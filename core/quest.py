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
    """조건 충족 여부. 슬롯 값이 필요하면 match_filters를 직접 쓴다."""
    from .quest_filter import match_filters

    return match_filters(session, quest, state) is not None


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


def _context_value(session, key: str):
    """현재 세션 맥락에서 슬롯 값을 꺼낸다.

    'context:location' 처럼 지정하면 추출층위가 갱신한 현재 위치를 쓴다.
    맥락을 무시하고 무작위로 뽑으면 '저지대에 있는데 다른 동네로 가라'는
    퀘스트가 나온다.
    """
    tl = getattr(session, "world_timeline", {}) or {}
    if key == "location":
        v = tl.get("current_location")
    elif key == "faction":
        v = getattr(session, "player_faction", "") or tl.get("faction_context")
    elif key == "time":
        v = tl.get("time_of_day")
    else:
        v = None
    return v if v and v != "미확인" else None


def fill_slots(session, quest: dict, matched: dict = None) -> dict:
    """가변정보를 채운다.

    - 'from': 'context:location' → 현재 맥락에서 가져온다
    - 'exclude': ['출발'] → 앞서 정해진 슬롯과 같은 값을 피한다
    - 'pick': '1-2' → 개수 랜덤(기획 규정)

    슬롯은 정의 순서대로 채워지므로, exclude는 앞선 슬롯만 참조할 수 있다.
    """
    from .profile_gen import resolve_pick_count

    data = load_quest_data(getattr(session, "scenario_id", ""))
    pools = data.get("quest_slots") or {}

    # 필터가 통과시킨 값이 곧 슬롯이다. 별도 정의 없이 빈칸이 채워진다.
    out = dict((matched or {}).get("slots") or {})

    for key, spec in (quest.get("slots") or {}).items():
        if key in out:
            continue   # 필터가 이미 채운 슬롯은 덮어쓰지 않는다
        if not isinstance(spec, dict):
            continue
        src = spec.get("from") or ""

        # 맥락 참조 — 실패하면 일반 풀로 폴백한다.
        if src.startswith("context:"):
            v = _context_value(session, src.split(":", 1)[1])
            if v:
                out[key] = v
                continue
            src = spec.get("fallback") or ""

        pool = list(pools.get(src) or [])
        if not pool:
            continue

        # 같은 퀘스트 안에서 중복을 피한다.
        # '중리 교역소에서 중리 교역소까지' 같은 문장을 막는다.
        taken = set()
        for ex_key in (spec.get("exclude") or []):
            ex_val = out.get(ex_key)
            if isinstance(ex_val, list):
                taken.update(ex_val)
            elif ex_val:
                taken.add(ex_val)
        remain = [x for x in pool if x not in taken]
        if remain:
            pool = remain

        n = min(resolve_pick_count(spec.get("pick", 1)), len(pool))
        chosen = random.sample(pool, max(1, n))
        out[key] = chosen[0] if n <= 1 else chosen
    return out


def _apply_slots(text: str, slots: dict) -> str:
    """가이드 문구의 {슬롯}을 채운다.

    조사 보정은 koreantext가 담당한다. 단순 replace를 쓰면
    '빗물 집수 장비이 남아' 같은 어색한 문장이 나온다.
    """
    from .koreantext import substitute, strip_unfilled

    return strip_unfilled(substitute(text, slots))


def start_quest(session, quest: dict, matched: dict = None) -> dict:
    """퀘스트를 활성화한다. 슬롯을 채우고 root 노드에서 시작한다.

    matched는 필터 매칭 결과다. 필터가 통과시킨 값(인물·장소 등)이
    그대로 슬롯이 되므로 함께 넘겨야 한다.
    """
    from .quest_filter import match_filters

    state = get_state(session)
    if matched is None:
        matched = match_filters(session, quest, state)
    slots = fill_slots(session, quest, matched)
    state["active"] = {
        "id": quest.get("id"),
        "name": quest.get("name"),
        "version": quest.get("version"),
        "line": quest.get("line"),
        "node": "root",
        "slots": slots,
        "path": [],
        "secret_known": False,
        "started_turn": getattr(session, "turn_count", 0),
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


# 전환 가능 판정 — root 노드에서 이만큼 정체하면 진입 실패로 본다.
STALL_TURNS = 3

# 상황 코드
CTX_ACTIVE = "active"      # A. 진행 중 — 선택 불가
CTX_NONE = "none"          # B. 없음 — 선택 요구
CTX_SWITCH = "switch"      # C. 전환 가능 — 유지/전환 판단


def choice_context(session) -> str:
    """지금이 퀘스트를 고를 수 있는 상황인지 판정한다.

    기획 확정 사항 — 진행 중인 퀘스트가 없거나 전환할 수 있게 된 경우에만
    선택하게 한다. 선택할 수 없는 상황에서는 아예 선택지를 주지 않아
    잘못된 판단이 구조적으로 불가능하게 만든다.
    """
    from .extraction import get_thresholds

    state = get_state(session)
    active = state.get("active")
    if not active:
        return CTX_NONE

    ex = getattr(session, "last_extraction", {}) or {}
    qp = ex.get("quest_progress") or {}
    try:
        deviation = int(qp.get("deviation", 0))
    except (TypeError, ValueError):
        deviation = 0

    # 이탈이 크면 전환을 검토할 수 있다.
    if deviation >= get_thresholds(session)["quest_deviated"]:
        return CTX_SWITCH

    # root에서 오래 정체하면 진입에 실패한 것으로 본다.
    if active.get("node") == "root":
        started = active.get("started_turn", 0)
        if getattr(session, "turn_count", 0) - started >= STALL_TURNS:
            return CTX_SWITCH

    return CTX_ACTIVE


def offered_ids(session) -> list:
    """이번 턴에 제시한 퀘스트 id 목록.

    코드 측 검증에 쓴다 — 제시하지 않은 id를 골랐다면 무시한다.
    """
    return list(getattr(session, "_quest_offered", []) or [])


def apply_choice(session, choice: dict) -> dict:
    """지시층위의 선택을 검증하고 반영한다.

    모델 응답을 그대로 믿지 않는다:
      - 선택 불가 상황(A)인데 값이 오면 무시
      - 제시하지 않은 id면 무시
      - 전환 시 진행 중이던 퀘스트는 abandoned로 기록

    Returns:
        {"applied": bool, "action": "start"|"switch"|"keep"|"ignored", "reason": str}
    """
    ctx = choice_context(session)
    if ctx == CTX_ACTIVE:
        # 필드를 주지 않았는데 값이 왔다면 오작동이다.
        if choice and (choice.get("id") or "").strip():
            print("[퀘스트] 선택 불가 상황의 quest_choice 무시")
        return {"applied": False, "action": "ignored", "reason": "선택 불가 상황"}

    qid = ((choice or {}).get("id") or "").strip()
    if not qid:
        return {"applied": False, "action": "keep", "reason": "선택 없음"}

    if qid not in offered_ids(session):
        print(f"[퀘스트] 제시하지 않은 id 무시: {qid}")
        return {"applied": False, "action": "ignored", "reason": "목록 밖 id"}

    state = get_state(session)
    active = state.get("active")

    # 유지 선택
    if active and active.get("id") == qid:
        return {"applied": False, "action": "keep", "reason": "현 퀘스트 유지"}

    quest = _find_quest(session, qid)
    if not quest:
        return {"applied": False, "action": "ignored", "reason": "정의 없음"}

    # 전환 — 진행 중이던 퀘스트를 이탈로 기록한다.
    if active:
        state["cleared"].append({
            "name": active["name"], "version": active.get("version"),
            "line": active.get("line"), "outcome": "abandoned",
            "turn": getattr(session, "turn_count", 0),
        })

    start_quest(session, quest)
    return {"applied": True,
            "action": "switch" if active else "start",
            "reason": (choice or {}).get("reason") or ""}


def build_quest_block(session) -> str:
    """지시층위에 주입할 퀘스트 블록 — 상황에 따라 다르게 조립한다.

    A(진행 중)  활성 퀘스트 가이드만. 선택지를 주지 않는다.
    B(없음)     후보 목록 + 선택 요구
    C(전환 가능) 활성 가이드 + 후보 목록 + 유지/전환 판단 요구

    제시한 id는 session._quest_offered에 남겨 코드 측 검증에 쓴다.
    """
    state = get_state(session)
    active = state.get("active")
    ctx = choice_context(session)
    session._quest_offered = []

    lines = []

    # ── 활성 퀘스트 가이드 (A·C) ──
    if active:
        quest = _find_quest(session, active["id"])
        if quest:
            tree = quest.get("tree") or {}
            node = tree.get(active["node"]) or {}
            slots = active.get("slots") or {}

            lines += [
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
            lines.append("이 가이드를 참고하되 플레이어의 선택을 강요하지 말 것.")
            session._quest_offered.append(active["id"])

    # ── A: 선택지 없이 종료 ──
    if ctx == CTX_ACTIVE:
        return "\n".join(lines) + "\n" if lines else ""

    # ── B·C: 후보 목록 ──
    candidates = filter_available(session)
    if not candidates and ctx == CTX_NONE:
        return ""
    state = get_state(session)

    if ctx == CTX_SWITCH:
        lines.append("\n[퀘스트 전환 가능]")
        lines.append(
            f"현재 '{active['name']}'이(가) 진행 중이나 서사가 궤도를 벗어났다.\n"
            f"유지하려면 quest_choice.id에 {active['id']}를, "
            f"전환하려면 아래 목록의 id를 적을 것.\n"
            f"전환은 서사가 명백히 다른 방향으로 갔을 때만 한다."
        )
    else:
        lines.append("\n[가능 퀘스트 목록 — 하나를 선택하거나 선택하지 않을 수 있다]")

    for q in candidates:
        # 필터 매칭 결과를 넘겨야 {인물}·{장소}가 채워진다.
        from .quest_filter import match_filters
        slots = fill_slots(session, q, match_filters(session, q, state))
        root = (q.get("tree") or {}).get("root") or {}
        lines.append(
            f"  - id={q.get('id')} | {q.get('name')} ({q.get('line')})\n"
            f"    {_apply_slots(root.get('guide'), slots)}"
        )
        if q.get("id") not in session._quest_offered:
            session._quest_offered.append(q.get("id"))

    if ctx == CTX_NONE:
        lines.append(
            "지금 상황에 어울리는 것이 있으면 quest_choice.id에 그 id를 적고,\n"
            "없으면 빈 문자열을 적을 것. 억지로 고르지 말 것."
        )
    return "\n".join(lines) + "\n"


def case_schema(session) -> dict | None:
    """활성 퀘스트가 있을 때만 quest_case 스키마를 반환한다.

    지시층위는 '어느 방향으로 이끌지'만 답한다. 실제 진전 여부는
    추출층위 수치가 임계를 넘을 때만 일어나므로 판정 권한은 넘기지 않는다.
    """
    state = get_state(session)
    active = state.get("active")
    if not active:
        return None
    quest = _find_quest(session, active["id"])
    if not quest:
        return None
    cases = ((quest.get("tree") or {}).get(active["node"]) or {}).get("cases") or {}
    if not cases:
        return None

    return {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": (
                    "이번 묘사가 향하는 케이스 키. 제시된 후보 중 하나("
                    + " / ".join(cases.keys())
                    + "). 어느 쪽도 아니면 빈 문자열."
                ),
            },
            "reason": {"type": "string", "description": "판단 근거 한 문장"},
        },
        "required": ["key", "reason"],
    }


def set_intended_case(session, case: dict) -> str | None:
    """지시층위가 지정한 케이스를 보관한다. 진전은 추출 수치가 결정한다."""
    state = get_state(session)
    active = state.get("active")
    if not active:
        return None
    key = ((case or {}).get("key") or "").strip()
    if not key:
        active["intended_case"] = None
        return None
    quest = _find_quest(session, active["id"])
    cases = ((quest or {}).get("tree") or {}).get(active["node"], {}).get("cases") or {}
    if key not in cases:
        print(f"[퀘스트] 존재하지 않는 케이스 키 무시: {key}")
        active["intended_case"] = None
        return None
    active["intended_case"] = key
    return key


def choice_schema(session) -> dict | None:
    """상황에 맞는 quest_choice 스키마를 반환한다.

    A(진행 중)에서는 None — 스키마에서 필드를 아예 뺀다.
    필드가 없으면 모델이 값을 낼 수 없으므로 '진행 중인데 다른 퀘스트를
    골라버리는' 사고가 구조적으로 불가능해진다.
    """
    ctx = choice_context(session)
    if ctx == CTX_ACTIVE:
        return None

    if ctx == CTX_SWITCH:
        desc = ("현 퀘스트를 유지하려면 그 id를, 전환하려면 새 id를 적는다. "
                "제시된 목록의 id만 허용.")
    else:
        desc = "선택한 퀘스트 id. 제시된 목록의 id만 허용. 고르지 않으면 빈 문자열."

    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": desc},
            "reason": {"type": "string", "description": "선택 또는 미선택 사유 한 문장"},
        },
        "required": ["id", "reason"],
    }


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

    # 진전 — 지시층위가 지정한 케이스를 우선한다.
    # 지정이 없거나 유효하지 않으면 첫 케이스로 폴백한다.
    intended = active.get("intended_case")
    next_key = intended if intended in cases else next(iter(cases))
    next_node = cases[next_key].get("next")
    if next_node not in tree:
        return {"moved": False, "node": active["node"], "outcome": None, "replan": False}

    active["path"].append(next_key)
    active["node"] = next_node
    active["intended_case"] = None
    active["started_turn"] = getattr(session, "turn_count", 0)
    outcome = (tree.get(next_node) or {}).get("outcome")

    granted = None
    if outcome:
        state["cleared"].append({
            "name": active["name"], "version": active["version"],
            "line": active["line"], "outcome": outcome,
            "turn": getattr(session, "turn_count", 0),
        })
        granted = apply_grants(session, quest, outcome)
        state["active"] = None

    return {"moved": True, "node": next_node, "outcome": outcome,
            "replan": False, "granted": granted}


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
        apply_grants(session, quest, outcome)
        state["active"] = None
    return True


def check_secret_awareness(session, extraction: dict) -> bool:
    """추출 수치로 이면정보 인지를 판정한다.

    임계 비교는 코드가 한다 — 추출층위에는 기준을 알려주지 않는다.
    """
    from .extraction import get_thresholds

    state = get_state(session)
    active = state.get("active")
    if not active or active.get("secret_known"):
        return False
    try:
        score = int((extraction or {}).get("secret_awareness", 0))
    except (TypeError, ValueError):
        return False
    if score < get_thresholds(session)["secret_reveal"]:
        return False
    return mark_secret_known(session)


def apply_grants(session, quest: dict, outcome: str) -> dict | None:
    """클리어 시 세션에 반영할 것을 적용한다.

    성공 계열(clear·partial)에만 적용한다. 실패나 이탈로 끝난 퀘스트가
    소속을 주면 안 된다.

    Returns:
        적용된 내용 또는 None
    """
    grants = quest.get("grants")
    if not isinstance(grants, dict) or not grants:
        return None
    if outcome not in ("clear", "partial"):
        return None

    applied = {}
    faction = grants.get("faction")
    if faction and getattr(session, "player_faction", "") != faction:
        session.player_faction = faction
        applied["faction"] = faction

    for name in (grants.get("info") or []):
        ledger = getattr(session, "info_ledger", None)
        if isinstance(ledger, dict):
            ledger[name] = True
            applied.setdefault("info", []).append(name)

    return applied or None


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


# ── 인피니티 세션 ─────────────────────────────────────────
# 메인라인 클리어 후 이어갈 때의 플랜. 기획 규정상 비용 안내가 필수다.
INFINITY_PLANS = {
    "sub_only": {
        "label": "서브라인만",
        "desc": "중·소형 사건만 이어진다. 세션 엔딩은 다시 오지 않는다.",
        "cost": "기본 (추가 호출 없음)",
        "narrative_mode": "quest",
        "allow_main": False,
        "suppress_medium": False,
    },
    "with_main": {
        "label": "메인 포함",
        "desc": "새 메인라인이 등장할 수 있다. 클리어하면 다시 엔딩이 열린다.",
        "cost": "기본 (추가 호출 없음)",
        "narrative_mode": "quest",
        "allow_main": True,
        "suppress_medium": False,
    },
    "designer": {
        "label": "서사 설계자",
        "desc": "퀘스트 틀을 벗어나 자유롭게 전개된다. 무엇이든 일어날 수 있다.",
        "cost": "턴당 추가 (서사 설계 호출이 매 턴 발생)",
        "narrative_mode": "free",
        "allow_main": False,
        "suppress_medium": False,
    },
    "designer_calm": {
        "label": "설계자 + 중형 억제",
        "desc": "자유 전개하되 큰 사건은 억제한다. 일상과 소소한 사건 중심.",
        "cost": "턴당 추가 (서사 설계 호출이 매 턴 발생)",
        "narrative_mode": "free",
        "allow_main": False,
        "suppress_medium": True,
    },
}


def is_ending(outcome: str) -> bool:
    """세션 엔딩을 부르는 결과인지."""
    return bool(outcome) and str(outcome).startswith("ending")


def apply_infinity_plan(session, plan_key: str) -> dict | None:
    """인피니티 세션 플랜을 적용한다.

    기획 규정 — 메인 클리어 후 서브만 / 메인 포함 / 서사 설계자 /
    설계자+중형 억제 중 선택. 선택에 따라 서사설계 유형이 바뀐다.
    """
    plan = INFINITY_PLANS.get(plan_key)
    if not plan:
        return None
    session.narrative_mode = plan["narrative_mode"]
    session.infinity_plan = plan_key
    state = get_state(session)
    state["active"] = None
    return plan


def plan_allows_main(session) -> bool:
    """현재 플랜이 메인라인 등장을 허용하는지."""
    key = getattr(session, "infinity_plan", "") or ""
    if not key:
        return True   # 인피니티 이전에는 제한 없음
    return bool(INFINITY_PLANS.get(key, {}).get("allow_main"))


def format_plans() -> str:
    """플랜 선택 안내. 비용 안내가 필수다(기획 규정)."""
    lines = ["**인피니티 세션 플랜을 선택해 주십시오.**\n"]
    for key, p in INFINITY_PLANS.items():
        lines.append(f"**{p['label']}**\n> {p['desc']}\n> 비용: {p['cost']}")
    return "\n\n".join(lines)


def summary(session) -> str:
    """디스플레이 표기용 퀘스트 정보."""
    state = get_state(session)
    active = state.get("active")
    cleared = len(state.get("cleared") or [])
    if not active:
        return f"진행 중 없음 · 완료 {cleared}건"
    return (f"**{active['name']}** ({active['node']}) · "
            f"완료 {cleared}건")
