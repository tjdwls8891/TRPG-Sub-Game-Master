# 프로필 생성 실행부 — 시나리오 알고리즘을 해독해 종료까지 실행한다
#
# [기획 확정 사항]
#   "시나리오 생성과정을 해독하고 종료 콜 입력까지 실행할 실행부 하나로 실행"
#
#   즉 단계별로 코드를 따로 짜지 않는다. 이 실행부가 profile_creation 배열을
#   위에서부터 읽어 모듈을 호출하고, 유저 입력을 받아 다음 단계로 넘긴다.
#
# [자유도와 통제]
#   기획 규정 — 재선택·취소·단계회귀로 자유도를 보장하되 허가된 행동만
#   할 수 있게 통제한다. 따라서 실행부는 매 시점 '지금 가능한 행동'을
#   명시적으로 반환하고, 그 밖의 입력은 받지 않는다.
from . import profile_gen

# 실행 결과 유형
ASK = "ask"            # 유저 입력을 기다린다
CONFIRM = "confirm"    # 확인 메시지 — 예/아니오
WARN = "warn"          # 경고 후 확인
AUTO = "auto"          # 자동 선택됨, 다음으로
DONE = "done"          # 전체 완료
ERROR = "error"


def new_run(scenario_data: dict, *, prefill: dict = None) -> dict:
    """새 생성 실행 상태를 만든다.

    Args:
        prefill: 사전 프로필에서 가져온 값. 해당 항목은 건너뛴다.
    """
    return {
        "index": 0,
        "chosen": dict(prefill or {}),
        "pending": None,      # 확인 대기 중인 값
        "history": [],        # 회귀용 인덱스 기록
        "notes": [],
    }


def _step_at(scenario_data: dict, index: int) -> dict | None:
    steps = profile_gen.get_steps(scenario_data)
    if 0 <= index < len(steps):
        return steps[index]
    return None


def current_field(scenario_data: dict, run: dict) -> str | None:
    step = _step_at(scenario_data, run["index"])
    return step.get("field") if step else None


def advance_index(scenario_data: dict, run: dict) -> bool:
    """다음 미완료 단계로 이동한다. 이미 값이 있으면 건너뛴다.

    Returns:
        진행할 단계가 남았으면 True
    """
    steps = profile_gen.get_steps(scenario_data)
    i = run["index"]
    while i < len(steps):
        field = steps[i].get("field")
        if field not in run["chosen"]:
            run["index"] = i
            return True
        i += 1
    run["index"] = len(steps)
    return False


def step(scenario_data: dict, run: dict, user_input: str = None) -> dict:
    """한 단계를 진행한다.

    Args:
        user_input: 유저의 입력. None이면 현재 단계를 렌더할 정보만 반환한다.

    Returns:
        {"type": ASK|CONFIRM|WARN|AUTO|DONE|ERROR,
         "field": str, "options": [...], "message": str,
         "value": Any, "guide": str|None, "can_back": bool}
    """
    if not advance_index(scenario_data, run):
        return {"type": DONE, "field": None, "options": [], "value": None,
                "message": "프로필 생성이 완료되었습니다.",
                "guide": None, "can_back": bool(run["history"])}

    cur = _step_at(scenario_data, run["index"])
    field = cur.get("field")
    module = cur.get("module")
    args = cur.get("args") or {}
    can_back = bool(run["history"])

    guide = profile_gen.guide_from_prior(scenario_data, args, run["chosen"])

    # ── AI 모듈은 실행부가 직접 처리하지 않는다 ──
    if module in profile_gen.AI_MODULES:
        return {"type": ASK, "field": field, "options": [], "value": None,
                "message": args.get("prompt") or f"{field}을(를) 입력해 주십시오.",
                "guide": guide, "can_back": can_back, "ai_module": module,
                "args": args}

    handler = profile_gen.MODULES.get(module)
    if handler is None:
        return {"type": ERROR, "field": field, "options": [], "value": None,
                "message": f"알 수 없는 모듈입니다: {module}",
                "guide": guide, "can_back": can_back}

    # ── 교집합 추출 — 앞선 선택으로 목록이 좁혀진다 ──
    if module == "intersect_list":
        pool = profile_gen.intersect_list(scenario_data, args, run["chosen"])
        if not pool:
            return {"type": ERROR, "field": field, "options": [], "value": None,
                    "message": f"'{field}'에 해당하는 항목이 없습니다. 앞선 선택을 바꿔 주십시오.",
                    "guide": guide, "can_back": can_back}
        # pick이 지정되면 랜덤 선택으로 자동 확정한다.
        if args.get("pick"):
            picked = profile_gen.pick_random(pool, args["pick"])
            run["chosen"][field] = picked
            run["history"].append(run["index"])
            run["index"] += 1
            return {"type": AUTO, "field": field, "options": pool,
                    "value": picked, "guide": guide, "can_back": True,
                    "message": f"{field}: {', '.join(picked)}"}
        if user_input is None:
            return {"type": ASK, "field": field, "options": pool, "value": None,
                    "message": f"{field}을(를) 선택해 주십시오.",
                    "guide": guide, "can_back": can_back}
        args = dict(args, options=pool)
        module = "select_one"
        handler = profile_gen.select_one

    # ── 능력치 배분 ──
    # 산출은 언제나 랜덤이며, 총합·최대편차·최고항목은 선택 제약이다.
    # 제약은 run["stat_constraints"]에 보관되어 재굴림에도 유지된다.
    if module == "roll_stats":
        cons = run.get("stat_constraints") or {}
        result = profile_gen.roll_stats(
            args,
            total=cons.get("total"),
            max_spread=cons.get("max_spread"),
            top_field=cons.get("top_field"),
        )
        run["pending"] = {"field": field, "value": result["values"]}
        run["last_stats"] = result["values"]

        note = ""
        if not result["ok"]:
            note = "\n> ⚠️ 지정한 조건을 모두 만족하는 조합을 찾지 못해 근사값입니다."
        cons_txt = _describe_constraints(cons)
        return {"type": CONFIRM, "field": field, "options": [],
                "value": result["values"], "guide": guide, "can_back": can_back,
                "stat_module": True, "args": args,
                "message": (f"{field}: " + ", ".join(
                    f"**{k} {v}**" for k, v in result["values"].items())
                    + f"\n> 합 {result['total']} · 편차 {result['spread']}"
                    + (f"\n> 조건: {cons_txt}" if cons_txt else "")
                    + note)}

    # ── 단일 선택 ──
    if module == "select_one":
        result = profile_gen.select_one(scenario_data, args, run["chosen"],
                                        user_input)
        if result["status"] == "prompt":
            return {"type": ASK, "field": field, "options": result["options"],
                    "value": None, "guide": guide, "can_back": can_back,
                    "message": f"{field}을(를) 선택해 주십시오."}
        if result["status"] == "retry":
            sug = result["suggestions"]
            msg = f"'{user_input}'을(를) 찾지 못했습니다."
            if sug:
                msg += f" 혹시 이것입니까? {', '.join(sug)}"
            return {"type": ASK, "field": field, "options": result["options"],
                    "value": None, "guide": guide, "can_back": can_back,
                    "message": msg}

        value = result["value"]
        # 경고가 걸린 선택이면 확인 전에 알린다(기획 규정)
        warn = profile_gen.warn_on_choice(scenario_data, args, value)
        run["pending"] = {"field": field, "value": value}
        desc = profile_gen.describe_option(scenario_data, args, value)
        msg = f"**{value}**"
        if desc and desc != "(설명 없음)":
            msg += f"\n> {desc}"
        if warn:
            msg += f"\n⚠️ {warn}"
            return {"type": WARN, "field": field, "options": result["options"],
                    "value": value, "guide": guide, "can_back": can_back,
                    "message": msg}
        return {"type": CONFIRM, "field": field, "options": result["options"],
                "value": value, "guide": guide, "can_back": can_back,
                "message": msg}

    # ── 그 외 모듈은 값 없이 통과 ──
    run["history"].append(run["index"])
    run["index"] += 1
    return {"type": AUTO, "field": field, "options": [], "value": None,
            "message": f"{field} 처리 완료", "guide": guide, "can_back": True}


def _describe_constraints(cons: dict) -> str:
    """설정된 제약을 표시 문자열로."""
    parts = []
    if cons.get("total") is not None:
        parts.append(f"총합 {cons['total']}")
    if cons.get("max_spread") is not None:
        parts.append(f"편차 {cons['max_spread']} 이하")
    if cons.get("top_field"):
        parts.append(f"{cons['top_field']} 최고")
    return " · ".join(parts)


def set_stat_constraint(run: dict, key: str, value) -> dict:
    """능력치 제약을 설정하거나 해제한다(value가 None이면 해제)."""
    cons = dict(run.get("stat_constraints") or {})
    if value is None:
        cons.pop(key, None)
    else:
        cons[key] = value
    run["stat_constraints"] = cons
    return cons


def confirm(scenario_data: dict, run: dict) -> dict:
    """확인 대기 중인 값을 확정한다.

    확정 후 revise_field 규칙이 걸려 있으면 앞선 선택을 수정한다.
    """
    pending = run.get("pending")
    if not pending:
        return {"ok": False, "revised": None}

    field = pending["field"]
    run["chosen"][field] = pending["value"]
    run["pending"] = None
    run["history"].append(run["index"])
    run["index"] += 1

    # 이후 선택이 앞선 선택을 바꿔야 하는 경우 (세가 → 성씨)
    revised = None
    cur = _step_at(scenario_data, run["index"] - 1) or {}
    rule = (cur.get("args") or {}).get("revise")
    if isinstance(rule, dict):
        revised = profile_gen.revise_field(rule, run["chosen"])
        if revised:
            run["chosen"][revised["field"]] = revised["value"]
            run["notes"].append(revised["reason"])
    return {"ok": True, "revised": revised}


def cancel_pending(run: dict):
    """확인을 취소한다. 재선택으로 돌아가며 이전 선택 흔적을 지운다."""
    run["pending"] = None


def go_back(scenario_data: dict, run: dict) -> tuple:
    """이전 단계로 회귀한다.

    기획 규정 — 재선택 시 이전 선택 흔적을 제거한다.
    """
    if not run["history"]:
        return False, "되돌아갈 단계가 없습니다."
    prev = run["history"].pop()
    run["index"] = prev
    step_def = _step_at(scenario_data, prev)
    if step_def:
        run["chosen"].pop(step_def.get("field"), None)
    run["pending"] = None
    return True, step_def.get("field") if step_def else None


def jump_to(scenario_data: dict, run: dict, field: str) -> tuple:
    """특정 항목으로 이동한다(기획 규정 5번 — 특정 단계만 호출).

    이후 항목의 선택은 유지된다. 해당 항목만 다시 정하는 용도다.
    """
    idx = profile_gen.goto_step(profile_gen.get_steps(scenario_data), field)
    if idx is None:
        return False, f"'{field}' 단계를 찾을 수 없습니다."
    run["history"].append(run["index"])
    run["index"] = idx
    run["chosen"].pop(field, None)
    run["pending"] = None
    return True, field


async def run_ai_module(bot, session, scenario_data: dict, run: dict,
                        module: str, args: dict, user_input) -> dict:
    """AI 모듈을 실행한다. 실행부가 별도 경로로 처리하는 둘이다.

    Returns:
        {"ok": bool, "value": Any, "message": str}
    """
    from . import profile_ai

    field = current_field(scenario_data, run) or args.get("field") or ""

    if module == "ai_validate":
        res = await profile_ai.validate(
            bot, session, field=field, value=str(user_input or ""),
            scenario_data=scenario_data, rules=args.get("rules") or "",
        )
        msg = profile_ai.format_validation(field, res)
        if res["verdict"] == "reject":
            return {"ok": False, "value": None, "message": msg}
        run["pending"] = {"field": field, "value": user_input}
        return {"ok": True, "value": user_input, "message": msg}

    if module == "ai_merge":
        values = user_input if isinstance(user_input, list) else [user_input]
        res = await profile_ai.merge(
            bot, session, field=field, values=values,
            max_length=int(args.get("max_length") or 300),
        )
        note = ""
        if res["dropped"]:
            note = f"\n> 모순되어 제외됨: {', '.join(res['dropped'])}"
        run["pending"] = {"field": field, "value": res["merged"]}
        return {"ok": True, "value": res["merged"],
                "message": f"{res['merged']}{note}"}

    return {"ok": False, "value": None, "message": f"알 수 없는 AI 모듈: {module}"}


def result(run: dict) -> dict:
    """완성된 프로필 값."""
    return dict(run.get("chosen") or {})


def progress(scenario_data: dict, run: dict) -> str:
    """진행 표시."""
    steps = profile_gen.get_steps(scenario_data)
    done = sum(1 for s in steps if s.get("field") in run["chosen"])
    return f"{done}/{len(steps)}"
