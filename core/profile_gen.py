# 프로필 생성 모듈 — 시나리오 JSON이 지시하는 생성 알고리즘의 실행 단위
#
# [설계 원칙]
#   기획 확정 사항 — 필요 기능은 함수 형태로 코드에 두고, 시나리오 JSON에는
#   항목별 순서·사용 모듈·모듈이 사용할 인자를 배치한다. 실행부 하나가
#   이를 해독해 종료 콜까지 실행한다.
#
#   따라서 이 모듈의 함수들은 '무엇을 물을지'를 모른다. 시나리오가 지시한
#   인자를 받아 처리만 한다. 시나리오가 바뀌면 코드는 그대로 두고
#   JSON만 바꾼다.
#
# [시나리오 JSON 형식]
#   "profile_creation": [
#     {"field": "성별", "module": "select_one", "args": {"options": ["남","여"]}},
#     {"field": "문파", "module": "select_one", "args": {"from": "sects"}},
#     {"field": "무공", "module": "intersect_list",
#      "args": {"depends": ["문파","경지"], "source": "martial_arts", "pick": "2-4"}}
#   ]
import random
import re

# 오타 검사에서 후보로 볼 최소 유사도.
TYPO_RATIO = 0.6

# 능력치 프리셋 — 총합·편차·최고항목 조합 (기획 규정 6번).
STAT_PRESETS = {
    "균형": {"label": "균형형", "spread": "low"},
    "특화": {"label": "특화형", "spread": "high"},
    "양극": {"label": "양극형", "spread": "extreme"},
}


def _resolve_options(scenario_data: dict, args: dict, chosen: dict) -> list:
    """옵션 목록을 확정한다.

    args에 options가 있으면 그대로, from이 있으면 시나리오에서 꺼낸다.
    """
    if isinstance(args.get("options"), list):
        return list(args["options"])
    key = args.get("from")
    if not key:
        return []
    src = (scenario_data or {}).get(key)
    if isinstance(src, list):
        return list(src)
    if isinstance(src, dict):
        return list(src.keys())
    return []


def _similar(a: str, b: str) -> float:
    """간이 유사도. difflib보다 가볍고 한국어 오타에 충분하다."""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


# ── 1. 목록에서 단일 항목 선택 ──────────────────────────────
def select_one(scenario_data: dict, args: dict, chosen: dict,
               user_input: str = None) -> dict:
    """목록을 제시하고 텍스트 입력으로 선택받는다.

    기획 규정 — 검색 실패 시 오타 검사를 실시하고, 확인 메시지는 필수다.

    Returns:
        {"status": "prompt"|"confirm"|"retry", "options": [...],
         "value": str|None, "suggestions": [...]}
    """
    options = _resolve_options(scenario_data, args, chosen)
    if user_input is None:
        return {"status": "prompt", "options": options, "value": None,
                "suggestions": []}

    q = (user_input or "").strip()
    for opt in options:
        if opt == q:
            return {"status": "confirm", "options": options, "value": opt,
                    "suggestions": []}

    # 부분 일치
    partial = [o for o in options if q and q in o]
    if len(partial) == 1:
        return {"status": "confirm", "options": options, "value": partial[0],
                "suggestions": []}

    # 오타 검사 — 검색 실패 시에만 수행한다(기획 규정)
    scored = sorted(
        ((o, _similar(q, o)) for o in options), key=lambda x: -x[1]
    )
    suggestions = [o for o, r in scored if r >= TYPO_RATIO][:3]
    return {"status": "retry", "options": options, "value": None,
            "suggestions": suggestions or [o for o, _ in scored[:3]]}


# ── 2. 항목 설명 출력 ───────────────────────────────────────
def describe_option(scenario_data: dict, args: dict, option: str) -> str:
    """선택 후보 하나의 설명을 반환한다.

    기획 규정 — 확인 메시지와 같은 방식으로 출력하고, 취소로 닫으면 재선택한다.
    설명 원본은 시나리오의 descriptions 맵에서 찾는다.
    """
    key = args.get("describe_from") or args.get("from")
    src = (scenario_data or {}).get(key)
    if isinstance(src, dict):
        val = src.get(option)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            return val.get("desc") or val.get("description") or "(설명 없음)"
    return "(설명 없음)"


# ── 3. 선택에 따른 경고 메시지 ──────────────────────────────
def warn_on_choice(scenario_data: dict, args: dict, option: str) -> str | None:
    """특정 선택에 경고가 걸려 있으면 반환한다.

    시나리오 형식: "warnings": {"마교": "적대 세력이 많아 난이도가 높습니다."}
    """
    warns = args.get("warnings")
    if not isinstance(warns, dict):
        warns = (scenario_data or {}).get(args.get("warn_from") or "warnings") or {}
    if isinstance(warns, dict):
        return warns.get(option)
    return None


# ── 4. 다음 항목 선택 방식 결정 ─────────────────────────────
def branch_mode(args: dict, chosen: dict) -> str:
    """선택 결과에 따라 다음 항목을 자동/수동 중 무엇으로 진행할지 정한다.

    시나리오 형식: {"auto_if": {"문파": ["무명"]}}
      → 문파가 '무명'이면 다음 항목은 자동 선택
    """
    rules = args.get("auto_if")
    if isinstance(rules, dict):
        for field, values in rules.items():
            if chosen.get(field) in (values or []):
                return "auto"
    return "manual"


# ── 5. 단계 회귀·호출 ───────────────────────────────────────
def goto_step(steps: list, target_field: str) -> int | None:
    """특정 항목의 단계 인덱스를 찾는다.

    기획 규정 — 사전 지정도, 유저 선택도 알고리즘에 따라 가능해야 한다.
    """
    for i, step in enumerate(steps or []):
        if step.get("field") == target_field:
            return i
    return None


# ── 6. 능력치 프리셋 ────────────────────────────────────────
def stat_preset(args: dict, preset: str = None, top_field: str = None) -> dict:
    """능력치 총합·편차·최고항목 프리셋으로 배분한다.

    Args:
        args: {"stats": ["무공","심계",...], "total": 20, "max_single": 6, "min_single": 1}
        preset: STAT_PRESETS 키
        top_field: 최고 항목으로 지정할 능력치

    Returns:
        {"values": {stat: int}, "preset": str, "total": int}
    """
    stats = list(args.get("stats") or [])
    if not stats:
        return {"values": {}, "preset": preset or "균형", "total": 0}

    total = int(args.get("total") or (len(stats) * 3))
    hi = int(args.get("max_single") or 6)
    lo = int(args.get("min_single") or 1)
    spread = STAT_PRESETS.get(preset or "균형", STAT_PRESETS["균형"])["spread"]

    n = len(stats)
    # 편차가 클수록 기준값을 낮게 잡아 재분배할 여지를 남긴다.
    # 기준값을 total//n으로만 두면 나누어떨어질 때 나머지가 0이 되어
    # 세 프리셋이 같은 결과를 낸다.
    floor_ratio = {"low": 1.0, "high": 0.7, "extreme": 0.45}[spread]
    base = max(lo, int((total // n) * floor_ratio))
    values = {s: base for s in stats}
    remain = total - base * n

    # 편차에 따라 나머지를 어떻게 뿌릴지 정한다.
    order = list(stats)
    if top_field and top_field in order:
        order.remove(top_field)
        order.insert(0, top_field)
    elif spread != "low":
        random.shuffle(order)

    if spread == "low":
        idx = 0
        while remain > 0:
            s = order[idx % n]
            if values[s] < hi:
                values[s] += 1
                remain -= 1
            idx += 1
            if idx > n * hi * 2:
                break
    else:
        weights = [3, 2] + [1] * (n - 2) if spread == "high" else [5, 1] + [1] * (n - 2)
        idx = 0
        while remain > 0:
            s = order[idx % n]
            step = min(weights[idx % n], hi - values[s], remain)
            if step > 0:
                values[s] += step
                remain -= step
            idx += 1
            if idx > n * hi * 2:
                break

    return {"values": values, "preset": preset or "균형",
            "total": sum(values.values())}


# ── 7. 선택에 따른 기존 선택 수정 ───────────────────────────
def revise_field(args: dict, chosen: dict) -> dict | None:
    """이후 선택이 앞선 선택을 바꿔야 하는 경우를 처리한다.

    기획 예시 — 세가 소속을 고르면 이름의 성씨를 수정한다.

    시나리오 형식:
      {"when": "세가", "target": "이름", "rule": "prefix",
       "map": {"남궁세가": "남궁", "제갈세가": "제갈"}}

    Returns:
        {"field": str, "value": str, "reason": str} 또는 None
    """
    when = args.get("when")
    target = args.get("target")
    if not when or not target:
        return None
    trigger = chosen.get(when)
    mapping = args.get("map") or {}
    prefix = mapping.get(trigger)
    if not prefix:
        return None

    cur = str(chosen.get(target) or "")
    rule = args.get("rule") or "prefix"
    if rule == "prefix":
        # 이미 그 성씨면 바꾸지 않는다.
        if cur.startswith(prefix):
            return None
        # 기존 성씨를 떼고 붙인다. 한국어 성은 1~2자.
        given = cur[1:] if len(cur) > 1 else cur
        for p in mapping.values():
            if cur.startswith(p):
                given = cur[len(p):]
                break
        return {"field": target, "value": f"{prefix}{given}",
                "reason": f"{trigger} 소속이므로 성씨를 '{prefix}'로 맞췄습니다."}
    return None


# ── 9. 여러 응답 병합 (AI 없이 가능한 경우) ─────────────────
def merge_inputs(values: list, *, sep: str = ", ") -> str:
    """여러 입력을 하나로 합친다. 중복과 공백을 정리한다.

    AI 병합이 필요한 경우는 별도 호출 소관이며, 여기서는 단순 결합만 한다.
    """
    seen = []
    for v in values or []:
        t = str(v or "").strip()
        if t and t not in seen:
            seen.append(t)
    return sep.join(seen)


# ── 10. 이전 선택 기반 가이드라인 ───────────────────────────
def guide_from_prior(scenario_data: dict, args: dict, chosen: dict) -> str | None:
    """앞선 선택에 따라 현재 항목의 안내 문구를 만든다.

    시나리오 형식:
      {"guides": {"문파": {"화산파": "화산파는 검術을 중시합니다."}}}
    """
    guides = args.get("guides")
    if not isinstance(guides, dict):
        guides = (scenario_data or {}).get(args.get("guide_from") or "guides") or {}
    if not isinstance(guides, dict):
        return None
    parts = []
    for field, mapping in guides.items():
        if not isinstance(mapping, dict):
            continue
        val = chosen.get(field)
        if val and mapping.get(val):
            parts.append(mapping[val])
    return "\n".join(parts) if parts else None


# ── 11. 교집합 추출 ─────────────────────────────────────────
def intersect_list(scenario_data: dict, args: dict, chosen: dict) -> list:
    """앞선 선택들에 모두 해당하는 항목만 추린다.

    기획 예시안 — 문파별·경지별 가능 무공 목록을 저장해 두고,
    프로필 각 항목에 해당하는 목록의 교집합을 뽑는다.

    시나리오 형식:
      "martial_arts": {
        "문파": {"화산파": ["매화검법", "이십사수매화검"]},
        "경지": {"삼류": ["매화검법"]}
      }
      args: {"source": "martial_arts", "depends": ["문파","경지"], "pick": "2-4"}
    """
    source = (scenario_data or {}).get(args.get("source") or "")
    if not isinstance(source, dict):
        return []

    pools = []
    for field in (args.get("depends") or []):
        val = chosen.get(field)
        table = source.get(field)
        if not isinstance(table, dict) or val is None:
            continue
        items = table.get(val)
        if isinstance(items, list):
            pools.append(set(items))

    if not pools:
        return []
    result = set.intersection(*pools) if len(pools) > 1 else pools[0]
    return sorted(result)


def resolve_pick_count(spec) -> int:
    """'2-4' 같은 개수 지정을 실제 수로 환산한다.

    기획 규정 — 아이템 등의 경우 개수 랜덤 표기를 기능으로 실현한다.
    """
    if isinstance(spec, int):
        return max(0, spec)
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(spec or ""))
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return random.randint(min(lo, hi), max(lo, hi))
    try:
        return max(0, int(spec))
    except (TypeError, ValueError):
        return 1


def pick_random(items: list, spec) -> list:
    """목록에서 지정 개수만큼 무작위로 고른다."""
    n = min(resolve_pick_count(spec), len(items or []))
    if n <= 0:
        return []
    return random.sample(list(items), n)


# ── 실행부 지원 ─────────────────────────────────────────────
MODULES = {
    "select_one": select_one,
    "describe_option": describe_option,
    "warn_on_choice": warn_on_choice,
    "branch_mode": branch_mode,
    "goto_step": goto_step,
    "stat_preset": stat_preset,
    "revise_field": revise_field,
    "merge_inputs": merge_inputs,
    "guide_from_prior": guide_from_prior,
    "intersect_list": intersect_list,
}

# AI 호출이 필요한 모듈. 실행부가 별도 경로로 처리한다.
AI_MODULES = {"ai_validate", "ai_merge"}


def get_steps(scenario_data: dict) -> list:
    """시나리오의 프로필 생성 알고리즘."""
    steps = (scenario_data or {}).get("profile_creation")
    return steps if isinstance(steps, list) else []


def validate_steps(scenario_data: dict) -> list:
    """알고리즘 정의의 오류를 미리 찾는다. 시나리오 저작 지원용.

    Returns:
        오류 메시지 목록. 비어 있으면 정상.
    """
    errors = []
    steps = get_steps(scenario_data)
    if not steps:
        return ["profile_creation이 정의되지 않았습니다."]

    fields = [s.get("field") for s in steps]
    for i, step in enumerate(steps):
        field = step.get("field")
        module = step.get("module")
        if not field:
            errors.append(f"{i}번 단계에 field가 없습니다.")
        if not module:
            errors.append(f"'{field}' 단계에 module이 없습니다.")
        elif module not in MODULES and module not in AI_MODULES:
            errors.append(f"'{field}' 단계의 module '{module}'을 알 수 없습니다.")

        args = step.get("args") or {}
        # depends가 앞선 단계를 가리키는지 확인한다.
        for dep in (args.get("depends") or []):
            if dep not in fields[:i]:
                errors.append(f"'{field}' 단계가 아직 정해지지 않은 '{dep}'에 의존합니다.")
        if args.get("from") and args["from"] not in (scenario_data or {}):
            errors.append(f"'{field}' 단계의 from '{args['from']}'이 시나리오에 없습니다.")
    return errors
