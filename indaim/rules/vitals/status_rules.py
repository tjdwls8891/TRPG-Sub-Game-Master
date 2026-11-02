# 상태이상 조건 환경 (v7.38.0, 사용자 결정 D5 — 계획서 v1 §2)
#
# 공용 상태(data/common_status_effects.json)와 시나리오 상태(status_effects)를 구분하고, 상태별 선택 키 `rules`를
# 코드가 평가한다. DYNAMIC 세션(정본 시계)의 PC만 대상이다 — LEGACY와 NPC는 지금처럼 추출 점수 임계만 쓴다.
#
#   rules = {"apply_gate": C, "apply_auto": C, "clear_auto": C, "clear_gate": C}
#     apply_gate  추출 점수 71↑ 부여는 C가 TRUE일 때만(모름 = 부여 안 함)
#     apply_auto  C가 TRUE면 코드가 부여
#     clear_auto  C가 TRUE면 코드가 해제(코드 해제가 같은 턴 부여보다 우선)
#     clear_gate  추출 점수 30↓ 해제는 C가 TRUE일 때만 — rules가 있는데 clear_gate가 없으면 추출 해제 금지(D5-a 가)
#
#   조건 C(JSON AST, eval 없음 — 3값):
#     {"all": [C…]} · {"any": [C…]} · {"not": C} · {"const": bool}
#     {"status_time": "SINCE_INTAKE_GTE", "group": g, "minutes": n}   마지막 섭취(없으면 세션 시작) 뒤 n분↑
#     {"status_time": "SINCE_APPLIED_GTE", "minutes": n}              이 상태가 붙은 뒤 n분↑
#     {"status_time": "SINCE_STATUS_GTE", "status": s, "minutes": n}  다른 상태가 n분↑ 이어짐
#     {"status_time": "SINCE_CLEARED_GTE", "status": s, "minutes": n} 그 상태가 없고, 풀린 지 n분↑(v7.41 — 붙은 적 없으면 세션 시작부터)
#     {"latent_due": s}          잠복 판정(`latent`)이 s를 정했고 발현 시각이 됨(v7.41)
#     {"consumed": g}            이번 턴 그 묶음 물건 CONSUME
#     {"used": g}                이번 턴 그 묶음 물건 USE 또는 CONSUME
#     {"place_within": PlaceId}  장면 장소가 그곳이거나 그 아래
#     {"place_group": g}         장면 장소나 그 상위가 시나리오 `status_places[g]` 목록에 있음(v7.40 — 미선언 묶음은 모름)
#     {"daypart_in": [시간대…]}  현재 시간대
#     {"activity_in": [활동…], "min_minutes": n?}   이번 턴 활동(SLEEP·WAIT·TREAT…)과 그 턴의 경과 분
#     {"status_active": s}       그 상태가 지금 붙어 있음
#     {"wearing": 태그}          PC가 그 효과 태그의 물건을 입거나 들고 있음(v7.46 — 이번 턴 입기·벗기 반영, 공용 어휘)
#
#   시나리오 최상위 `intake_groups: {묶음: [물건 낱말…]}` — 물건 종류의 낱말 하나가 목록과 같으면 그 묶음.
#   `status_common_exclude: [이름…]` — 공용 상태 빼기. 상태 `excludes: [이름…]` — 상호 배타(붙은 쪽이 다른 쪽을 푼다).
#   상태 시계 `status_clock`(세션 필드, 되감기 대상): {PC 이름: {"applied": {상태: 분}, "cleared": {상태: 분},
#     "intake": {묶음: 분}, "latent": {대상 상태: {"from", "at", "due"|None}}}}.
#   v7.41 상태 선택 키: `deep: true` — 심화 상태(코드가 시간으로만 정한다, 추출 점수 무시 — 규칙에 gate const false).
#     `latent: {"target": 상태, "chance": 0~100, "onset_minutes": [최소, 최대]}` — 이 상태가 붙는 순간 코드가 (세션 시드,
#     PC, 시각)으로 대상 상태의 발현 여부·시각을 결정적으로 정한다.
#
#   v7.47 단계·불이익·처치(사용자 결정 S1~S6 — 계획서 v1):
#     `stages: [이름…]` 단계(1부터). `stage_mods: [{분야: 값}…]`(단계별) 또는 `mods: {분야: 값}` — 판정 상태 보정 H
#       ('전체'는 모든 판정, 분야 이름은 그 분야 판정에 더한다 — 분야 = 공용 효과 어휘 domains).
#     `care: {사용 효과: down|treat|ease}` — 이번 턴 PC에게 쓴 물건의 사용 효과(core.items)로 코드가 처치한다.
#       down = 한 단계 낮춤(1단계면 해제) · treat = 한 단계 낮춤(1단계면 처치 기록만 — 시간 규칙이 마무리) ·
#       ease = EASE_MINUTES 동안 판정 불이익을 한 단계 낮은 값으로(원인은 그대로).
#     `lower_by: "care"` — 추출이 단계를 낮추거나 해제하지 못한다(처치·시간 규칙만).
#     `latent.cancel_by: 사용 효과` — 발현 전 그 효과로 처치하면 잠복 취소 · `latent.min_stage: n` — 그 단계 이상에서만 판정.
#     rules 키 `raise_auto: C` — C가 TRUE면 코드가 한 단계 올린다.
#     v7.50 rules 키 `lower_auto: C` — C가 TRUE면 코드가 한 단계 내린다(1단계면 해제). 예: 휴식으로 마력 소모 회복.
#     조건 원자: {"stage_min": n, "status"?: s}(그 상태 — 없으면 이 상태 — 가 n단계 이상) ·
#       {"status_time": "SINCE_CARED_GTE", "minutes": n, "status"?: s}(마지막 처치 뒤 n분↑, 처치 없으면 FALSE).
#     상태 시계 추가 키: "stage" {상태: 단계} · "cared" {상태: 분} · "eased" {상태: 끝나는 분}(비면 키 없음).
#
#   v7.48(생존물 A — 계획서 v1 · D-3 나):
#     조건 원자 {"moved": true}(이번 턴 PC가 장소를 옮김) · {"noise_at_least": QUIET|NORMAL|LOUD}(이번 턴 PC가 낸 소리).
#     상태 선택 키 `ending: {id, title, epilogue, tone?}` — PC에게 붙으면 그 결말로 세션이 끝난다(사망 등).
#     NPC도 같은 규칙으로 판정한다(D-3 나). NPC는 섭취·활동·착용·장소·이번 턴 사용을 추적하지 않으므로 그 조건은 '모름' —
#     자동 부여·해제(auto)는 모름이면 일어나지 않고, 추출 부여·해제의 근거 조건(gate)은 모름이면 막지 않는다.
#     상태 선택 키 `roles: [concealed|incapacitated|dying|dead]` — 엔진이 상태를 이름이 아니라 역할로 읽는다
#       (concealed = 숨어 보이지 않음 · incapacitated = 움직이거나 행동할 수 없음 · dying = 처치 없으면 죽어 감 ·
#        dead = 세계에서 빠짐 — v7.49: NPC는 장면·사건 조건·추적에서 사라진다).
#     공용 상태 선택 키 `requires: 기능` — 시나리오가 그 기능(scenario_schema.capabilities)을 선언해야 붙는다(core.utils).
from __future__ import annotations

import copy
from dataclasses import dataclass, field  # noqa: F401

from indaim.rules.vitals.logic import (  # noqa: E402,F401 — 리팩터링 T-1: 순수 판정은 vitals 모듈이 소유
    ALL_DOMAIN, CARE_ACTIONS, Ctx, Decision, EASE_MINUTES, FALSE, NOISE_LEVELS, ROLES, ROLE_CONCEALED, ROLE_DEAD, ROLE_DYING, ROLE_INCAPACITATED, RULE_KEYS, SCOPE_COMMON, SCOPE_SCENARIO, STATUS_TIMES, TRUE, UNKNOWN, _b, _t_all, _t_any, death_timer, decide, deep_statuses, effective_stage, evaluate, groups_for_kind, groups_of, has_role, label, latent_roll, mods_at, mods_text, place_groups_of, post_bump, pre_bump, stage_of, stages_of, status_ending, with_role)













def check_condition(cond, *, groups=(), statuses=(), path="", place_groups=None) -> list:
    """저작 검사 — 오류 문구 목록. groups=None이면 섭취 묶음 참조는, place_groups=None이면 장소 묶음 참조는 검사하지 않는다."""
    errs = []
    if not isinstance(cond, dict) or len(cond) == 0:
        return [f"{path}: 조건은 객체여야 합니다"]
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        if not isinstance(cond[key], list) or not cond[key]:
            return [f"{path}.{key}: 비어 있지 않은 목록이어야 합니다"]
        for i, c in enumerate(cond[key]):
            errs += check_condition(c, groups=groups, statuses=statuses, path=f"{path}.{key}[{i}]",
                                    place_groups=place_groups)
        return errs
    if "not" in cond:
        return check_condition(cond["not"], groups=groups, statuses=statuses, path=f"{path}.not",
                               place_groups=place_groups)
    if "const" in cond:
        return [] if isinstance(cond["const"], bool) else [f"{path}.const: true/false"]
    if "status_time" in cond:
        k = cond["status_time"]
        if k not in STATUS_TIMES:
            errs.append(f"{path}: 알 수 없는 status_time {k!r}")
        if not isinstance(cond.get("minutes"), int) or cond["minutes"] < 0:
            errs.append(f"{path}: minutes는 0 이상 정수")
        if k == "SINCE_INTAKE_GTE" and groups is not None and cond.get("group") not in groups:
            errs.append(f"{path}: 정의되지 않은 섭취 묶음 {cond.get('group')!r}(intake_groups)")
        if k in ("SINCE_STATUS_GTE", "SINCE_CLEARED_GTE") and cond.get("status") not in statuses:
            errs.append(f"{path}: 알 수 없는 상태 {cond.get('status')!r}")
        if k == "SINCE_CARED_GTE" and cond.get("status") is not None and cond["status"] not in statuses:
            errs.append(f"{path}: 알 수 없는 상태 {cond.get('status')!r}")
        return errs
    for key in ("consumed", "used"):
        if key in cond:
            ok = groups is None or cond[key] in groups
            return [] if ok else [f"{path}: 정의되지 않은 섭취 묶음 {cond[key]!r}(intake_groups)"]
    if "status_active" in cond:
        return [] if cond["status_active"] in statuses else [f"{path}: 알 수 없는 상태 {cond['status_active']!r}"]
    if "moved" in cond:
        return [] if isinstance(cond["moved"], bool) else [f"{path}: moved는 true/false"]
    if "noise_at_least" in cond:
        return [] if cond["noise_at_least"] in NOISE_LEVELS else [f"{path}: noise_at_least는 {NOISE_LEVELS}"]
    if "stage_min" in cond:
        ok = isinstance(cond["stage_min"], int) and cond["stage_min"] >= 1 \
            and (cond.get("status") is None or cond["status"] in statuses)
        return [] if ok else [f"{path}: stage_min은 1 이상 정수(status는 알려진 상태)"]
    if "wearing" in cond:
        from indaim.rules.items.items import effect_vocab
        return [] if cond["wearing"] in effect_vocab()["tags"] else [f"{path}: 알 수 없는 효과 태그 {cond['wearing']!r}"]
    if "latent_due" in cond:
        return [] if cond["latent_due"] in statuses else [f"{path}: 알 수 없는 상태 {cond['latent_due']!r}"]
    if "place_within" in cond:
        return [] if isinstance(cond["place_within"], str) else [f"{path}: place_within은 장소 이름"]
    if "place_group" in cond:
        g = cond["place_group"]
        if not isinstance(g, str):
            return [f"{path}: place_group은 묶음 이름"]
        ok = place_groups is None or g in place_groups
        return [] if ok else [f"{path}: 정의되지 않은 장소 묶음 {g!r}(status_places)"]
    for key in ("daypart_in", "activity_in"):
        if key in cond:
            v = cond[key]
            ok = isinstance(v, list) and v and all(isinstance(x, str) for x in v)
            return [] if ok else [f"{path}: {key}는 문자열 목록"]
    return [f"{path}: 알 수 없는 조건 {sorted(cond)}"]


# ══════════════════════════════════════════════════════════════
#  정의
# ══════════════════════════════════════════════════════════════







def check_definitions(scenario_data, merged: dict) -> list:
    """저작 검사 — (코드, 문구) 목록. rules·excludes·intake_groups."""
    out = []
    raw = (scenario_data or {}).get("intake_groups")
    groups = groups_of(scenario_data) if raw is not None else None   # 묶음 미저작 = 묶음 조건은 거짓(부여 없음)
    if raw is not None and (not isinstance(raw, dict) or any(
            not isinstance(v, list) or not all(isinstance(w, str) and w.strip() for w in v) for v in raw.values())):
        out.append(("STATUS_INTAKE_GROUPS", "intake_groups는 {묶음: [낱말…]}"))
    raw_p = (scenario_data or {}).get("status_places")
    pgroups = place_groups_of(scenario_data) if raw_p is not None else None   # 미저작 = 장소 묶음 조건은 모름
    places = (scenario_data or {}).get("places") or {}
    if raw_p is not None and (not isinstance(raw_p, dict) or any(
            not isinstance(v, list) or not all(isinstance(x, str) and x in places for x in v) for v in raw_p.values())):
        out.append(("STATUS_PLACES", "status_places는 {묶음: [실재 장소…]}"))
    names = set(merged)
    for name, eff in merged.items():
        rules = eff.get("rules")
        if rules is not None:
            if not isinstance(rules, dict) or set(rules) - set(RULE_KEYS):
                out.append(("STATUS_RULES", f"{name}: rules 키는 {RULE_KEYS}만"))
            else:
                for k, c in rules.items():
                    out += [("STATUS_RULES", f"{name}: {e}")
                            for e in check_condition(c, groups=groups, statuses=names, path=k, place_groups=pgroups)]
        if "deep" in eff and not isinstance(eff["deep"], bool):
            out.append(("STATUS_DEEP", f"{name}: deep은 true/false"))
        lat = eff.get("latent")
        if lat is not None:
            ok = isinstance(lat, dict) and lat.get("target") in names and isinstance(lat.get("chance"), int) \
                and 0 <= lat["chance"] <= 100 and isinstance(lat.get("onset_minutes"), list) \
                and len(lat["onset_minutes"]) == 2 and all(isinstance(x, int) and x >= 0 for x in lat["onset_minutes"]) \
                and lat["onset_minutes"][0] <= lat["onset_minutes"][1]
            if not ok:
                out.append(("STATUS_LATENT", f"{name}: latent는 {{target(상태), chance(0~100), onset_minutes[최소,최대]}}"))
            elif lat.get("cancel_by") is not None and lat["cancel_by"] not in _use_effects():
                out.append(("STATUS_LATENT", f"{name}: latent.cancel_by {lat['cancel_by']!r}는 공용 사용 효과가 아님"))
            elif lat.get("min_stage") is not None and not (isinstance(lat["min_stage"], int) and lat["min_stage"] >= 1):
                out.append(("STATUS_LATENT", f"{name}: latent.min_stage는 1 이상 정수"))
        out += [("STATUS_STAGES", f"{name}: {e}") for e in _check_stage_keys(eff)]
        for other in eff.get("excludes") or ():
            if other not in names:
                out.append(("STATUS_EXCLUDES", f"{name}: 알 수 없는 배타 상태 {other!r}"))
            elif name not in (merged[other].get("excludes") or ()):
                out.append(("STATUS_EXCLUDES", f"{name}↔{other}: excludes는 양쪽에 적어야 합니다"))
    for x in (scenario_data or {}).get("status_common_exclude") or ():
        if not isinstance(x, str):
            out.append(("STATUS_COMMON_EXCLUDE", "status_common_exclude는 이름 목록"))
    return out


# ══════════════════════════════════════════════════════════════
#  판정
# ══════════════════════════════════════════════════════════════

def _use_effects() -> dict:
    from indaim.rules.items.items import effect_vocab
    return effect_vocab().get("use_effects") or {}


def _domains() -> set:
    from indaim.rules.items.items import effect_vocab
    return set(effect_vocab().get("domains") or {}) | {ALL_DOMAIN}


def _check_stage_keys(eff: dict) -> list:
    """v7.47 stages·stage_mods·mods·care·lower_by 저작 검사."""
    errs = []
    st = eff.get("stages")
    if st is not None and (not isinstance(st, list) or len(st) < 2 or not all(isinstance(x, str) and x for x in st)):
        errs.append("stages는 이름 2개 이상의 목록")
    dom = _domains()

    def bad_mods(m):
        return not isinstance(m, dict) or any(k not in dom or not isinstance(v, int) for k, v in m.items())
    sm = eff.get("stage_mods")
    if sm is not None and (not isinstance(st, list) or not isinstance(sm, list) or len(sm) != len(st)
                           or any(bad_mods(m) for m in sm)):
        errs.append("stage_mods는 단계 수만큼의 {분야: 정수} 목록(분야 = 공용 domains 또는 '전체')")
    if eff.get("mods") is not None and bad_mods(eff["mods"]):
        errs.append("mods는 {분야: 정수}(분야 = 공용 domains 또는 '전체')")
    care = eff.get("care")
    if care is not None and (not isinstance(care, dict) or any(
            k not in _use_effects() or v not in CARE_ACTIONS for k, v in care.items())):
        errs.append(f"care는 {{공용 사용 효과: {'|'.join(CARE_ACTIONS)}}}")
    if eff.get("lower_by") not in (None, "care"):
        errs.append("lower_by는 'care'만")
    end = eff.get("ending")
    if end is not None and not (isinstance(end, dict) and all(isinstance(end.get(k), str) and end[k].strip()
                                                               for k in ("id", "title", "epilogue"))):
        errs.append("ending은 {id, title, epilogue, tone?} 문자열")
    roles = eff.get("roles")
    if roles is not None and not (isinstance(roles, list) and all(r in ROLES for r in roles)):
        errs.append(f"roles는 [{'|'.join(ROLES)}] 목록")
    if eff.get("requires") is not None and not (isinstance(eff["requires"], str) and eff["requires"]):
        errs.append("requires는 기능 이름 문자열")
    return errs


















def status_mod(merged: dict, statuses, clock, now, domain) -> tuple:
    """v7.47(S2) PC 판정 상태 보정 H — (값, 근거 문구). '전체' + 그 분야 값의 합, 판정 범위(-15~+15)로 자른다."""
    from indaim.rules.checks.judgment import STATUS_RANGE
    total, parts = 0, []
    for s in statuses or ():
        eff = (merged or {}).get(s)
        if not isinstance(eff, dict):
            continue
        m = mods_at(eff, effective_stage(eff, clock, s, now))
        v = int(m.get(ALL_DOMAIN, 0)) + (int(m.get(domain, 0)) if domain and domain != ALL_DOMAIN else 0)
        if v:
            total += v
            parts.append(f"{label(merged, s, clock, now)} {v:+d}")
    lo, hi = STATUS_RANGE
    return max(lo, min(hi, total)), " · ".join(parts)




def labels_for(session, name: str) -> list:
    """표시용 — 그 인물의 상태 이름(단계·완화 표기, 단계 기록이 없으면 이름 그대로)."""
    from indaim.app.utils import get_merged_status_effects
    sts = (getattr(session, "statuses", None) or {}).get(name) or []
    clock = (getattr(session, "status_clock", None) or {}).get(name)
    if not clock:
        return [str(x) for x in sts]
    merged = get_merged_status_effects(getattr(session, "scenario_data", None) or {})
    now = (getattr(session, "world_clock", None) or {}).get("minute_index")
    return [label(merged, str(x), clock, now) for x in sts]












# ══════════════════════════════════════════════════════════════
#  턴 커밋 배선(DYNAMIC) — 추출 계획 적용 직전, 같은 커밋 단위
# ══════════════════════════════════════════════════════════════


def _turn_groups(item_plan, groups) -> tuple:
    from indaim.rules.items.items import parse_key
    consumed, used = set(), set()
    for op in getattr(item_plan, "ops", None) or ():
        if op.get("op") not in ("CONSUME", "USE") or not op.get("stock"):
            continue
        gs = groups_for_kind(parse_key(op["stock"], getattr(item_plan, "defs", None))[0].kind, groups)
        used |= gs
        if op["op"] == "CONSUME":
            consumed |= gs
    return frozenset(consumed), frozenset(used)



def _place_chain(scenario_data, place) -> tuple:
    from indaim.rules.vitals.logic import place_chain
    return place_chain((scenario_data or {}).get("places") or {}, place)


def resolve_turn(session, prep) -> list:
    """DYNAMIC 턴의 상태 후보를 규칙으로 확정하고 status_clock을 갱신한다. 반환: 보고 줄(빈 목록 = 변화 없음).

    PC와 NPC(v7.48 D-3 나 — 추적하지 않는 사실은 모름)를 판정한다. LEGACY·정본 시계 없음이면 아무것도 안 한다.
    계산은 vitals 모듈의 순수 decide_turn이 한다(리팩터링 T-1) — 여기는 세션·준비 객체에서 재료를 모아 결과를 쓴다."""
    from indaim.rules.vitals import logic as _vl
    from indaim.turn import turn_world as _tw
    from indaim.app.utils import get_merged_status_effects
    plan = getattr(prep, "extraction_plan", None)
    facts = _tw.status_turn_facts(session, prep)      # H05 경계: 세계 읽기는 turn_world가 한다
    if plan is None or facts is None:
        return []
    sd = getattr(session, "scenario_data", None) or {}
    merged = get_merged_status_effects(sd)
    if not any(isinstance(e.get("rules"), dict) or e.get("care") or e.get("stages") for e in merged.values()):
        return []
    pc_name = next((p.get("name") for p in (session.players or {}).values() if p.get("name")), None)
    if not pc_name:
        return []
    consumed, used = _turn_groups(getattr(plan, "item_plan", None), groups_of(sd))
    from indaim.turn import settle_capture as _sc
    _sc.note("vitals", {"merged": merged, "pc_name": pc_name, "statuses": session.statuses or {},
                        "clocks": getattr(session, "status_clock", None) or {}, "facts": facts,
                        "apply_in": plan.status_apply, "clear_in": plan.status_clear, "consumed": consumed, "used": used,
                        "status_scores": (getattr(plan, "result", None) or {}).get("status_scores") or [],
                        "places": sd.get("places") or {}, "place_groups": place_groups_of(sd),
                        "seed": f"{getattr(session, 'narrative_seed', '') or ''}"})
    td = _vl.decide_turn(
        merged, pc_name=pc_name, statuses=session.statuses or {}, clocks=getattr(session, "status_clock", None) or {},
        facts=facts, apply_in=plan.status_apply, clear_in=plan.status_clear, consumed=consumed, used=used,
        status_scores=(getattr(plan, "result", None) or {}).get("status_scores") or [],
        places=sd.get("places") or {}, place_groups=place_groups_of(sd),
        seed=f"{getattr(session, 'narrative_seed', '') or ''}")
    plan.status_apply, plan.status_clear = td.apply, td.clear
    session.status_clock = td.clocks
    if td.notes:
        plan.diagnostics["status_rules"] = list(td.notes)
    return td.notes
