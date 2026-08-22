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
# ── 능력치 등급 ────────────────────────────────────────────
# 수치 대신 별명으로 고르게 한다. 비율 기준이라 스탯 개수가 다른
# 시나리오에서도 그대로 통한다.
#
#   최대치 = 스탯 개수 × 상한(기본 20)
#   영도(3종) 60 · 무협(4종) 80

# 총합 등급 — 상한 대비 비율. 평범 50%는 스탯당 평균 10에 해당한다.
TOTAL_TIERS = {
    "허접": 0.25,
    "약골": 0.35,
    "평범": 0.50,
    "튼튼": 0.62,
    "능력자": 0.75,
    "먼치킨": 0.90,
}

# 편차 등급 — (상한 - 하한) 대비 비율. None이면 제한 없음.
SPREAD_TIERS = {
    "만능": 0.10,
    "무난": 0.30,
    "뚜렷": 0.50,
    "극단": None,
}

# 상한 기본값. 시나리오의 ability_stat_max로 덮인다.
DEFAULT_STAT_CAP = 20


def get_total_tiers(scenario_data: dict = None) -> dict:
    """총합 등급표. 시나리오가 stat_tiers로 덮어쓸 수 있다."""
    override = (scenario_data or {}).get("stat_tiers")
    if isinstance(override, dict) and override:
        return {k: float(v) for k, v in override.items()
                if isinstance(v, (int, float))}
    return dict(TOTAL_TIERS)


def get_spread_tiers(scenario_data: dict = None) -> dict:
    """편차 등급표. 시나리오가 spread_tiers로 덮어쓸 수 있다."""
    override = (scenario_data or {}).get("spread_tiers")
    if isinstance(override, dict) and override:
        return {k: (float(v) if isinstance(v, (int, float)) else None)
                for k, v in override.items()}
    return dict(SPREAD_TIERS)


def tier_to_total(tier: str, args: dict, scenario_data: dict = None) -> int | None:
    """총합 등급을 실제 수치로 환산한다.

    최대치는 '스탯 개수 × 상한'이며, 상한은 시나리오의
    ability_stat_max를 따른다(미지정 20).
    """
    tiers = get_total_tiers(scenario_data)
    ratio = tiers.get(tier)
    if ratio is None:
        return None
    stats = args.get("stats") or []
    n = len(stats)
    if n == 0:
        return None

    # NOTE: 명목 상한(ability_stat_max)이 아니라 실제 배분 상한(max_single)을
    #       기준으로 삼는다. 영도는 명목 20이지만 PC 배분 범위가 5~15라,
    #       명목 기준으로 계산하면 먼치킨(90% = 54)이 달성 불가능한 값이 된다.
    hi = int(args.get("max_single")
             or (scenario_data or {}).get("ability_stat_max")
             or DEFAULT_STAT_CAP)
    lo = int(args.get("min_single") or 1)

    # 최대치(개수 × 상한)에 비율을 곧바로 적용한다.
    # 하한 합을 바닥으로 깔면 등급별 평균이 밀려 올라간다 —
    # 1~20 범위에서 평범 50%가 평균 10.7이 되어 룰북 기준과 어긋난다.
    # 단순 비율이면 평균이 5·7·10·12·15·18로 떨어져 직관적이다.
    return max(lo * n, round(n * hi * ratio))


def max_possible_spread(args: dict, total: int = None) -> int:
    """주어진 총합에서 실제로 만들 수 있는 최대 편차.

    NOTE: 총합이 극단이면 조합이 하나뿐이라 편차 선택이 무의미해진다.
          허접(합15, 스탯 3종, 하한 5)은 5+5+5뿐이고
          먼치킨은 상한에 몰린다. 등급을 이 값에 비례시켜야
          어느 총합에서도 선택이 의미를 갖는다.
    """
    stats = args.get("stats") or []
    n = len(stats)
    if n < 2:
        return 0
    hi = int(args.get("max_single") or DEFAULT_STAT_CAP)
    lo = int(args.get("min_single") or 1)
    t = int(total if total is not None else (args.get("total") or n * 3))
    t = max(lo * n, min(hi * n, t))

    # 한 항목을 최대한 올렸을 때의 값 — 나머지는 하한으로 내린다.
    top = min(hi, t - lo * (n - 1))
    # 한 항목을 최대한 내렸을 때의 값 — 나머지는 상한으로 올린다.
    bottom = max(lo, t - hi * (n - 1))
    return max(0, top - bottom)


def tier_to_spread(tier: str, args: dict, scenario_data: dict = None,
                   total: int = None) -> int | None:
    """편차 등급을 실제 수치로 환산한다. 제한 없음이면 None.

    총합에서 실제로 가능한 최대 편차에 비율을 적용한다.
    """
    tiers = get_spread_tiers(scenario_data)
    if tier not in tiers:
        return None
    ratio = tiers[tier]
    if ratio is None:
        return None
    return max(0, round(max_possible_spread(args, total) * ratio))


def describe_tier(tier: str, args: dict, scenario_data: dict = None,
                  *, kind: str = "total", total: int = None) -> str:
    """등급 라벨에 실제 수치를 덧붙인 설명."""
    if kind == "total":
        v = tier_to_total(tier, args, scenario_data)
        return f"{tier} (합 {v})" if v is not None else tier
    v = tier_to_spread(tier, args, scenario_data, total)
    return f"{tier} (편차 {v} 이하)" if v is not None else f"{tier} (제한 없음)"


def roll_stats(args: dict, *, total: int = None, max_spread: int = None,
               top_field: str = None, tries: int = 200) -> dict:
    """능력치를 무작위로 배분한다 (기획 규정 6번).

    산출은 언제나 랜덤이며, 아래 셋은 선택 가능한 제약이다.
      total       총합 지정 (미지정이면 args의 기본 총합)
      max_spread  능력치 간 최대 편차 상한
      top_field   해당 능력치가 최고가 되도록

    제약을 만족하는 조합을 무작위로 뽑는다. 프리셋처럼 결과가 고정되지
    않으므로 같은 조건이라도 매번 다른 캐릭터가 나온다.

    Args:
        args: {"stats": [...], "total": int, "max_single": int, "min_single": int}

    Returns:
        {"values": {stat: int}, "total": int, "spread": int,
         "constraints": {...}, "ok": bool}
    """
    stats = list(args.get("stats") or [])
    if not stats:
        return {"values": {}, "total": 0, "spread": 0,
                "constraints": {}, "ok": False}

    n = len(stats)
    target = int(total if total is not None else (args.get("total") or n * 3))
    hi = int(args.get("max_single") or 6)
    lo = int(args.get("min_single") or 1)

    # 총합이 물리적으로 불가능하면 범위 안으로 당긴다.
    target = max(lo * n, min(hi * n, target))

    constraints = {"total": target, "max_spread": max_spread,
                   "top_field": top_field}

    best = None
    for _ in range(tries):
        values = _random_partition(stats, target, lo, hi)
        if values is None:
            continue
        spread = max(values.values()) - min(values.values())

        if max_spread is not None and spread > max_spread:
            continue
        if top_field and top_field in values:
            # 단독 최고여야 한다. 동률이면 다시 뽑는다.
            top_val = values[top_field]
            if any(v >= top_val for k, v in values.items() if k != top_field):
                continue

        return {"values": values, "total": sum(values.values()),
                "spread": spread, "constraints": constraints, "ok": True}

    # 제약을 모두 만족하는 조합을 못 찾았다 — 가능한 선까지 맞춘다.
    values = _forced_partition(stats, target, lo, hi,
                               max_spread=max_spread, top_field=top_field)
    return {"values": values, "total": sum(values.values()),
            "spread": max(values.values()) - min(values.values()),
            "constraints": constraints, "ok": False}


def _random_partition(stats: list, target: int, lo: int, hi: int) -> dict | None:
    """총합이 target인 무작위 배분. 각 값은 lo~hi.

    최소값을 깔고 남은 몫을 무작위로 흩뿌린다. 균등 분포는 아니지만
    치우침 없이 다양한 조합이 나온다.
    """
    n = len(stats)
    remain = target - lo * n
    if remain < 0 or remain > (hi - lo) * n:
        return None

    values = {s: lo for s in stats}
    order = list(stats)
    while remain > 0:
        random.shuffle(order)
        placed = False
        for s in order:
            if values[s] >= hi:
                continue
            # 한 번에 뿌리는 양을 무작위로 해 분포를 넓힌다.
            step = random.randint(1, min(remain, hi - values[s]))
            values[s] += step
            remain -= step
            placed = True
            if remain <= 0:
                break
        if not placed:
            return None
    return values


def _forced_partition(stats: list, target: int, lo: int, hi: int, *,
                      max_spread: int = None, top_field: str = None) -> dict:
    """제약을 만족하는 조합을 못 찾았을 때의 폴백.

    편차 상한을 우선 지키고, 최고 항목 지정은 마지막에 강제한다.
    """
    n = len(stats)
    base = max(lo, min(hi, target // n))
    values = {s: base for s in stats}
    remain = target - base * n

    order = list(stats)
    random.shuffle(order)
    if top_field and top_field in order:
        order.remove(top_field)
        order.insert(0, top_field)

    idx = 0
    guard = 0
    while remain != 0 and guard < n * (hi - lo) * 4:
        s = order[idx % n]
        if remain > 0 and values[s] < hi:
            values[s] += 1
            remain -= 1
        elif remain < 0 and values[s] > lo:
            values[s] -= 1
            remain += 1
        idx += 1
        guard += 1

    # 편차 상한 강제 — 최고를 깎아 최저에 더한다.
    if max_spread is not None:
        guard = 0
        while (max(values.values()) - min(values.values())) > max_spread and guard < 100:
            top = max(values, key=lambda k: values[k])
            bot = min(values, key=lambda k: values[k])
            if values[top] - 1 < lo or values[bot] + 1 > hi:
                break
            values[top] -= 1
            values[bot] += 1
            guard += 1

    # 최고 항목 강제 — 단독 1위가 되도록 한 칸 확보한다.
    if top_field and top_field in values:
        guard = 0
        while guard < 100:
            others = [k for k in values if k != top_field]
            if not others:
                break
            rival = max(others, key=lambda k: values[k])
            if values[top_field] > values[rival]:
                break
            if values[top_field] + 1 > hi or values[rival] - 1 < lo:
                break
            values[top_field] += 1
            values[rival] -= 1
            guard += 1
    return values


def reroll_stats(args: dict, previous: dict = None, **constraints) -> dict:
    """같은 제약으로 다시 굴린다.

    기획 규정상 산출은 언제나 랜덤이므로 재굴림이 자연스러운 조작이다.
    직전 결과와 완전히 같으면 한 번 더 시도한다.
    """
    result = roll_stats(args, **constraints)
    if previous and result["values"] == previous:
        result = roll_stats(args, **constraints)
    return result


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
    "roll_stats": roll_stats,
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
