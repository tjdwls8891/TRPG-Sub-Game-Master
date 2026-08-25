# 장소 계층 — 그래프 구조와 이동 개연성
#
# [계층은 틀이 아니다]
#   tier 같은 고정 계층을 두지 않는다. 각 장소는 parent 하나만 가리키고
#   깊이는 그 결과일 뿐이다. 섬서 아래 화음(깊이 3)과 종남산(깊이 2)이
#   대등하게 존재할 수 있으며, 낄 계층이 없으면 없는 대로 둔다.
#   최소단위는 하위 존재 여부로 자동 판정된다.
#
# [이동 개연성]
#   connected  직결. 문 하나 사이.
#   reachable  한 턴에 이동해도 어색하지 않은 범위.
#              미지정이면 connected를 3칸 확장해 자동 생성한다.
#   그 밖의 목적지는 ASK로 안내한다. 차단이 아니라 거리 인식이다.
from collections import deque

# reachable 자동 확장 폭 (지시 확정).
REACHABLE_HOPS = 3

# 경로 탐색 상한. 너무 먼 곳은 계산하지 않는다.
MAX_ROUTE_HOPS = 40


def load_places(scenario_data: dict) -> dict:
    """시나리오의 장소 사전."""
    places = (scenario_data or {}).get("places")
    return places if isinstance(places, dict) else {}


def get(places: dict, name: str) -> dict | None:
    return places.get(name) if isinstance(places, dict) else None


def parents_of(places: dict, name: str) -> list:
    """직결 상위 목록. extra_parents까지 포함한다."""
    node = get(places, name) or {}
    out = []
    if node.get("parent"):
        out.append(node["parent"])
    for p in (node.get("extra_parents") or []):
        if p not in out:
            out.append(p)
    return out


def path_of(places: dict, name: str) -> list:
    """최상위부터 자신까지의 경로. 순환은 방어한다."""
    chain = []
    cur = name
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        ps = parents_of(places, cur)
        cur = ps[0] if ps else None
    return list(reversed(chain))


def children_of(places: dict, name: str) -> list:
    """직속 하위 목록."""
    return [k for k, v in (places or {}).items()
            if isinstance(v, dict) and name in
            ([v.get("parent")] + list(v.get("extra_parents") or []))]


def is_leaf(places: dict, name: str) -> bool:
    """최소단위인지. 하위가 없으면 최소단위다."""
    return not children_of(places, name)


def resolve(places: dict, text: str) -> str | None:
    """이름·별칭·부분 일치로 장소를 특정한다.

    추출층위나 플레이어가 정확한 이름을 쓰지 않을 수 있다.
    """
    if not text or not places:
        return None
    q = str(text).strip()
    if q in places:
        return q
    for name, node in places.items():
        if not isinstance(node, dict):
            continue
        if q in (node.get("aliases") or []):
            return name
    # 부분 일치 — 후보가 하나일 때만 채택한다.
    hits = [n for n in places if q and q in n]
    return hits[0] if len(hits) == 1 else None


def is_container(places: dict, name: str) -> bool:
    """이동 대상이 아닌 상위 개념인지.

    '영도' 같은 최상위는 장소가 아니라 범위다. 이동 경로로 쓰면
    중리에서 조도로 갈 때 해안로와 방파제를 건너뛰는 지름길이 생긴다.
    connected가 하나도 없는 상위 노드를 범위로 본다.
    """
    node = get(places, name) or {}
    if node.get("traversable"):
        return False
    return not (node.get("connected") or []) and bool(children_of(places, name))


def _neighbors(places: dict, name: str) -> list:
    """이동 가능한 인접 노드.

    직결(connected)과 상하위를 본다. 다만 범위 노드는 통로가 되지 않는다 —
    그 아래 실제 장소들끼리 connected로 이어져야 한다.
    """
    node = get(places, name) or {}
    out = list(node.get("connected") or [])
    for p in parents_of(places, name):
        if p not in out and not is_container(places, p):
            out.append(p)
    for c in children_of(places, name):
        if c not in out:
            out.append(c)
    return [n for n in out if n in places]


def is_visible_from(places: dict, target: str, current: str) -> bool:
    """target이 current에서 인지 가능한지.

    visible_within이 지정된 장소는 상위 항목 안에 가려져 있다.
    현재 위치의 경로에 그 상위가 포함돼야 보인다.

    해련 마을에서 남항의 '제1방어선'까지 보이는 것은 이상하다.
    거리로는 닿아도 남항 안에 들어가야 인지할 수 있는 것들이 있다.
    """
    node = get(places, target) or {}
    gate = node.get("visible_within")
    if not gate:
        return True
    if isinstance(gate, str):
        gate = [gate]
    scope = set(path_of(places, current)) | {current}
    return any(g in scope for g in gate)


def reachable_from(places: dict, name: str, hops: int = REACHABLE_HOPS) -> list:
    """한 턴에 이동 가능한 범위.

    명시된 reachable이 있으면 그것을 우선하고,
    없으면 connected를 hops칸 확장해 자동 생성한다(지시 확정: 3칸).
    """
    node = get(places, name) or {}
    explicit = node.get("reachable")
    if isinstance(explicit, list) and explicit:
        return [n for n in explicit if n in places]

    seen = {name}
    frontier = [name]
    for _ in range(max(1, hops)):
        nxt = []
        for cur in frontier:
            for nb in _neighbors(places, cur):
                if nb not in seen:
                    seen.add(nb)
                    nxt.append(nb)
        frontier = nxt
        if not frontier:
            break
    seen.discard(name)
    return sorted(seen)


def _hop_distance(places: dict, start: str, goal: str) -> int:
    """두 장소 사이의 홉 수. 경로가 없으면 큰 값."""
    path = route(places, start, goal)
    return len(path) - 1 if path else 99


def can_move(places: dict, start: str, goal: str) -> bool:
    """한 턴에 이동해도 어색하지 않은지."""
    if not start or not goal or start == goal:
        return True
    return goal in reachable_from(places, start)


def route(places: dict, start: str, goal: str) -> list:
    """최단 경로. 없으면 빈 리스트."""
    if not start or not goal or start not in places or goal not in places:
        return []
    if start == goal:
        return [start]

    prev = {start: None}
    q = deque([(start, 0)])
    while q:
        cur, depth = q.popleft()
        if depth >= MAX_ROUTE_HOPS:
            continue
        for nb in _neighbors(places, cur):
            if nb in prev:
                continue
            prev[nb] = cur
            if nb == goal:
                path = [goal]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return list(reversed(path))
            q.append((nb, depth + 1))
    return []


# ── 방문 기록 ─────────────────────────────────────────────
def visited(session) -> list:
    v = getattr(session, "visited_places", None)
    return v if isinstance(v, list) else []


def is_visited(session, name: str) -> bool:
    return name in visited(session)


def mark_visited(session, name: str) -> bool:
    """방문 기록. 되감기 대상이므로 TRACKED_PATHS에 포함된다."""
    if not name:
        return False
    v = list(visited(session))
    if name in v:
        return False
    v.append(name)
    session.visited_places = v
    return True


# ── 이미지 ────────────────────────────────────────────────
def image_for(places: dict, name: str) -> str | None:
    """장소 이미지. 명시된 것이 없으면 상위에서 가장 하위의 것을 쓴다.

    지시 확정 — 각 장소마다 사용할 이미지를 명시하고, 없으면 상위 항목 중
    이미지가 존재하는 것들에서 가장 하위 항목의 것을 사용한다.
    """
    chain = path_of(places, name)      # 최상위 → 자신
    for n in reversed(chain):          # 자신 → 최상위 순으로 훑는다
        node = get(places, n) or {}
        if node.get("image"):
            return node["image"]
    return None


# ── 표기 ──────────────────────────────────────────────────
def format_name(places: dict, name: str, current: str = None) -> str:
    """표기 규칙 — 전체 경로를 나열하지 않는다.

    같은 상위 안에서는 최소단위만, 상위가 다르면 직결 상위를 붙인다.
    """
    if not name or name not in places:
        return name or ""
    if not current or current == name:
        return name
    cur_parents = set(path_of(places, current))
    own = parents_of(places, name)
    if own and own[0] in cur_parents:
        return name        # 같은 갈래 — 최소단위만
    return f"{own[0]} {name}" if own else name


# ── 온디맨드 주입 ─────────────────────────────────────────
def _describe(places: dict, name: str, *, full: bool) -> str:
    """장소 설명. full이면 내외부·분위기·NPC까지."""
    node = get(places, name) or {}
    if not full:
        return node.get("known_brief") or node.get("location_desc") or ""

    lines = []
    if node.get("location_desc"):
        lines.append(f"위치: {node['location_desc']}")
    for key, label in (("exterior", "외부"), ("interior", "내부")):
        if node.get(key):
            mood = node.get(f"mood_{key}")
            lines.append(f"{label}: {node[key]}" + (f" ({mood})" if mood else ""))
    npcs = node.get("npcs") or []
    if npcs:
        parts = []
        for n in npcs[:8]:
            if isinstance(n, dict):
                parts.append(f"{n.get('name')}({n.get('relation','')}·{n.get('frequency','')})")
        if parts:
            lines.append("연관 인물: " + ", ".join(parts))
    return "\n".join(lines)


def build_place_block(session) -> str:
    """지시·묘사층위에 주입할 장소 정보.

    현재 장소는 풀 정보, inherit 상위는 풀 정보, 갈 수 있는 곳은 얕게.
    미방문 장소는 표기로 구분해 확정적 묘사를 막는다.
    """
    places = load_places(getattr(session, "scenario_data", {}) or {})
    if not places:
        return ""

    tl = getattr(session, "world_timeline", {}) or {}
    cur = resolve(places, tl.get("current_location") or "")
    if not cur:
        return ""

    lines = ["\n[현재 장소]", f"{cur}"]
    comp = list(getattr(session, "companions", []) or [])
    if comp:
        lines.append(f"동행: {', '.join(comp)}")
    desc = _describe(places, cur, full=True)
    if desc:
        lines.append(desc)

    # 상위 맥락 — 저작자가 명시한 것만 (전부 주입하면 토큰이 폭증한다)
    node = get(places, cur) or {}
    for up in (node.get("inherit") or []):
        if up in places:
            up_desc = _describe(places, up, full=True)
            if up_desc:
                lines.append(f"\n[상위 맥락 — {up}]")
                lines.append(up_desc)

    # 갈 수 있는 곳 — 미리 얕게 주입해 이동 묘사의 재료를 준다.
    # 추출층위가 장소를 바꾼 뒤에야 정보가 오면 이동을 묘사할 수 없다.
    # 가려진 장소는 제외한다. 거리로는 닿아도 상위 안에 들어가야
    # 인지할 수 있는 것들이 있다.
    near = [n for n in reachable_from(places, cur)
            if is_visible_from(places, n, cur)]
    # 가까운 곳을 앞에 둔다. 목록이 잘릴 때 자기 하위나 직결이
    # 먼 거점보다 뒤로 밀리면 이상하다.
    near.sort(key=lambda n: (_hop_distance(places, cur, n), n))
    if near:
        lines.append("\n[갈 수 있는 곳]")
        for n in near[:8]:
            nd = get(places, n) or {}
            if is_visited(session, n):
                brief = nd.get("known_brief") or nd.get("location_desc") or ""
                lines.append(f"· {format_name(places, n, cur)} (방문함) — {brief}")
            else:
                hint = nd.get("unknown_hint") or "아직 가보지 않은 곳이다."
                lines.append(f"· {format_name(places, n, cur)} (미방문) — {hint}")
        lines.append("미방문 장소는 확정적으로 묘사하지 말 것. 밖에서 보이는 만큼만 다룬다.")
    return "\n".join(lines) + "\n"


def build_move_hint(session, goal_text: str) -> str:
    """ASK용 이동 안내 재료.

    판단층위는 캐시를 읽지 않아 세계관을 모른다. 코드가 경로를 계산해
    사용자 프롬프트에 직접 넣어야 한다.

    Returns:
        안내가 필요 없으면 빈 문자열.
    """
    places = load_places(getattr(session, "scenario_data", {}) or {})
    if not places:
        return ""

    tl = getattr(session, "world_timeline", {}) or {}
    cur = resolve(places, tl.get("current_location") or "")
    if not cur:
        return ""

    goal = resolve(places, goal_text) if goal_text else None

    # 목적지 불명 — 갈 수 있는 곳을 제시하며 방향을 묻게 한다.
    if not goal:
        near = reachable_from(places, cur)
        if not near:
            return ""
        near = [n for n in near if is_visible_from(places, n, cur)]
        items = []
        for n in near[:8]:
            mark = "" if is_visited(session, n) else "(미방문)"
            items.append(f"{format_name(places, n, cur)}{mark}")
        return (
            "\n[이동 안내 — 목적지 불명]\n"
            f"현재 위치: {cur}\n"
            f"갈 수 있는 곳: {', '.join(items)}\n"
            "어느 쪽으로 갈지 묻되, 특정 방향을 권하지 말 것.\n"
        )

    if can_move(places, cur, goal):
        return ""   # 한 턴에 갈 수 있다 — 안내 불필요

    path = route(places, cur, goal)
    if not path:
        return (
            "\n[이동 안내 — 경로 없음]\n"
            f"현재 위치: {cur} / 선언된 목적지: {goal}\n"
            "그곳으로 가는 길을 알지 못한다는 점을 알리고 되물을 것.\n"
        )

    return (
        "\n[이동 안내 — 한 턴에 갈 수 없는 거리]\n"
        f"현재 위치: {cur}\n"
        f"선언된 목적지: {goal}\n"
        f"경로: {' → '.join(path)}\n"
        "위 경로를 자연스러운 설명으로 바꿔 전하고, 출발할지 물을 것.\n"
        "거리와 소요는 경로의 길이에 맞춰 맥락에 어울리게 표현한다.\n"
    )


def hops_between(session, start: str, goal: str) -> int:
    """경로 길이. 시간 경과 산출에 쓴다."""
    places = load_places(getattr(session, "scenario_data", {}) or {})
    path = route(places, start, goal)
    return max(0, len(path) - 1) if path else 0
