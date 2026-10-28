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
        # v7.43: choose가 지정되면 플레이어가 정확히 그 수만큼 고른다(다중 선택 → 확인 화면).
        n = min(int(args.get("choose") or 0), len(pool))
        if n:
            if isinstance(user_input, list):
                vals = [v for v in dict.fromkeys(str(x) for x in user_input) if v in pool]
                if len(vals) == n:
                    run["pending"] = {"field": field, "value": vals}
                    return {"type": CONFIRM, "field": field, "options": pool, "value": vals,
                            "guide": guide, "can_back": can_back,
                            "message": " · ".join(f"**{v}**" for v in vals)}
            return {"type": ASK, "field": field, "options": pool, "value": None, "multi": n,
                    "message": args.get("prompt") or f"{field}에서 {n}개를 골라 주십시오.",
                    "guide": guide, "can_back": can_back}
        if user_input is None:
            return {"type": ASK, "field": field, "options": pool, "value": None,
                    "message": f"{field}을(를) 선택해 주십시오.",
                    "guide": guide, "can_back": can_back}
        args = dict(args, options=pool)
        module = "select_one"
        handler = profile_gen.select_one

    # ── v7.46 묶음별 선택 — 묶음마다 차례로 고르고 마지막에 한 번 확인 ──
    if module in ("choose_skills", "choose_groups"):
        if module == "choose_skills":
            fixed, groups = profile_gen.skill_groups(scenario_data, args, run["chosen"])
        else:
            fixed, groups = [], profile_gen.item_groups(scenario_data, args)
        part = run.get("partial") or {}
        if part.get("field") != field:
            part = {"field": field, "done": {}}
        todo = [g for g in groups if g[0] not in part["done"]]
        if todo and isinstance(user_input, list):
            label, _v, opts, n = todo[0]
            vals = [v for v in dict.fromkeys(str(x) for x in user_input) if v in opts]
            if len(vals) == n:
                part["done"][label] = vals
                todo = todo[1:]
        run["partial"] = part
        table = (scenario_data or {}).get(args.get("source") or "skills")
        if todo:
            label, v, opts, n = todo[0]
            picked = [x for g in groups for x in part["done"].get(g[0], [])]
            if module == "choose_skills":
                msg = (f"**{label} {v}** — 이 스탯과 이어진 스킬을 **{n}개** 골라 주십시오."
                       + (f"\n> 직업 스킬: {', '.join(fixed)}" if fixed else ""))
            else:
                msg = (args.get("prompt") or "시작 물건을 역할마다 하나씩 골라 주십시오.") + f"\n> **{label}**"
            if picked:
                msg += f"\n> 고른 것: {', '.join(picked)}"
            return {"type": ASK, "field": field, "options": opts, "value": None, "multi": n,
                    "option_desc": {o: profile_gen.option_note(table, o) or "" for o in opts},
                    "message": msg, "guide": guide, "can_back": can_back}
        value = list(fixed) + [x for g in groups for x in part["done"].get(g[0], [])]
        run["partial"] = None
        run["pending"] = {"field": field, "value": value}
        return {"type": CONFIRM, "field": field, "options": [], "value": value, "guide": guide,
                "can_back": can_back, "message": " · ".join(f"**{v}**" for v in value) or "(없음)"}

    # ── 능력치 배분 ──
    # 산출은 언제나 랜덤이며, 총합·최대편차·최고항목은 선택 제약이다.
    # 제약은 run["stat_constraints"]에 보관되어 재굴림에도 유지된다.
    if module == "roll_stats":
        cons = run.get("stat_constraints") or {}
        # 등급 라벨을 실제 수치로 환산한다. 편차는 총합에 상대적이므로
        # 총합을 먼저 확정해야 한다.
        total_v = profile_gen.tier_to_total(
            cons.get("total_tier"), args, scenario_data) if cons.get("total_tier") else None
        spread_v = profile_gen.tier_to_spread(
            cons.get("spread_tier"), args, scenario_data,
            total_v) if cons.get("spread_tier") else None

        result = profile_gen.roll_stats(
            args,
            total=total_v,
            max_spread=spread_v,
            top_field=cons.get("top_field"),
        )
        # 직업에 명백한 치중이 있으면 최고값과 교환한다.
        # 재배분이 아니라 교환이므로 총합·편차 제약을 깨지 않는다.
        job = run["chosen"].get("직업")
        top = profile_gen.job_top_stat(scenario_data, job) if job else None
        if top:
            result["values"] = profile_gen.swap_to_top(result["values"], top)

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
                    + (f"\n> {job} 특성으로 **{top}**이 최고가 되도록 조정됨" if top else "")
                    + note)}

    # ── 단일 선택 ──
    if module == "select_one":
        result = profile_gen.select_one(scenario_data, args, run["chosen"],
                                        user_input)
        if result["status"] == "prompt":
            msg = f"{field}을(를) 선택해 주십시오."
            if args.get("list_desc"):      # v7.46: 고르기 전에 선택지마다 성격 한 줄을 함께 본다
                msg += "".join(f"\n> **{o}** — {profile_gen.describe_option(scenario_data, args, o)}"
                               for o in result["options"])
            return {"type": ASK, "field": field, "options": result["options"],
                    "value": None, "guide": guide, "can_back": can_back,
                    "message": msg}
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
        # 선택지별 덧붙임 한 줄(예: 영도 job_guides — 이 섬에서 그 직업의 의미)
        note = ((scenario_data or {}).get(args.get("note_from") or "") or {}).get(value)
        if isinstance(note, str) and note.strip():
            msg += f"\n> {note.strip()}"
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
    """설정된 제약을 표시 문자열로. 수치가 아니라 등급 라벨을 쓴다."""
    parts = []
    if cons.get("total_tier"):
        parts.append(cons["total_tier"])
    if cons.get("spread_tier"):
        parts.append(cons["spread_tier"])
    if cons.get("top_field"):
        parts.append(f"{cons['top_field']} 특화")
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
    run["partial"] = None


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
    run["partial"] = None
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


def dependents_of(scenario_data: dict, field: str) -> list:
    """field에 의존하는 뒤 단계 필드(args.depends 기준 — 예: 직업 → 스킬)."""
    out = []
    for st in profile_gen.get_steps(scenario_data):
        args = st.get("args") or {}
        deps = list(args.get("depends") or [])
        if st.get("module") == "choose_skills":    # v7.46(A2): 직업 전용 스킬·스탯 구간 개수가 이 둘에서 나온다
            deps += [args.get("job_field") or "직업", args.get("stats_field") or "능력치"]
        if field in deps and st.get("field"):
            out.append(st["field"])
    return out


def edit_field(scenario_data: dict, run: dict, field: str) -> tuple:
    """v7.15 최종 검토의 필드 수정 — 그 필드와 의존 필드만 비우고 나머지 값·자료형은 그대로 둔다.

    jump_to는 대상 필드만 지운다(뒤 단계 선택 유지). 의존 필드(직업 → 스킬)는 원래 값이 새 선택과
    어긋날 수 있으므로 함께 비워 다시 정하게 한다.
    """
    ok, f = jump_to(scenario_data, run, field)
    if not ok:
        return ok, f
    for dep in dependents_of(scenario_data, field):
        run["chosen"].pop(dep, None)
    return True, field


def expected_type_ok(step: dict, value, scenario_data: dict = None, chosen: dict = None) -> bool:
    """단계 모듈이 기대하는 값인가(불러온 저장 프로필 검증용 — 자동 형변환 없음).

    v7.46(A1): 시나리오를 함께 주면 내용도 본다 — 능력치는 지금 스탯 이름 그대로, 선택지 단계는 지금 선택지 안의 값,
    묶음 선택은 지금 직업·능력치로 다시 계산한 구성과 같아야 한다(옛 체계로 저장된 프로필은 다시 정하게 한다)."""
    module = (step or {}).get("module")
    args = (step or {}).get("args") or {}
    sd, ch = scenario_data, chosen or {}
    if module == "roll_stats":
        ok = isinstance(value, dict) and all(isinstance(v, int) and not isinstance(v, bool) for v in value.values())
        return ok and (sd is None or not args.get("stats") or set(value) == set(args["stats"]))
    if module in ("choose_skills", "choose_groups"):
        if not isinstance(value, list):
            return False
        if sd is None:
            return True
        if module == "choose_skills":
            fixed, groups = profile_gen.skill_groups(sd, args, ch)
        else:
            fixed, groups = [], profile_gen.item_groups(sd, args)
        vals = [str(x) for x in value]
        rest = [x for x in vals if x not in fixed]
        if set(fixed) - set(vals):
            return False
        for _label, _v, opts, n in groups:
            if sum(1 for x in rest if x in opts) != n:
                return False
        return len(rest) == sum(g[3] for g in groups)
    if module == "intersect_list":
        if (args.get("pick") or args.get("choose")):
            if not isinstance(value, list):
                return False
            pool = profile_gen.intersect_list(sd, args, ch) if sd is not None else []
            return not pool or set(map(str, value)) <= set(pool)
        return isinstance(value, (str, list))
    if not isinstance(value, str):
        return False
    if module == "select_one" and sd is not None:
        opts = profile_gen._resolve_options(sd, args, ch)
        return not opts or value in [str(o) for o in opts]
    return True


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
