# 진상 변형별 풀이 가능성 정적 검사 (v7.44 E6) — tools/scenario_check.py 전용(런타임·정의 revision 무관)
#
# 시나리오 최상위 선택 키(검사 도구만 읽는다):
#   "variant_check": {
#     "template": 진상 사건 template_id, "key": 지역 상태 키(예: "culprit"), "min_support": 정수(기본 3),
#     "clues": {InfoId: {"points_to": 그 단서가 가리키는 값(진상 값 또는 저작 NPC 이름), "strength": "decisive"|"support"}}
#   }
# 진상 값 = 진상 사건의 spawn 케이스(from 없음)가 SET_EVENT_LOCAL(key)로 쓰는 리터럴. 값마다 도달 가능한 단서:
#   ① 그 케이스 단계가 직접 쓰는 정보(info 인자) ② 모든 사건 국면의 finds 정보 — 국면(과 사건 spawn_when)이
#   EVENT_LOCAL(template, key) EQ 리터럴을 요구하면 그 리터럴이 이 값일 때만(그 밖 조건은 보수적으로 도달 가능으로 본다).
# 오류: 진상 값마다 그 값을 가리키는 단서가 min_support 미만 · 다른 값을 가리키는 decisive 단서가 도달 가능.
# 경고: 어떤 진상에서도 도달하지 못하는 단서.
from __future__ import annotations

ERROR, WARNING = "ERROR", "WARNING"


def _eq_literals(pred, tid, key) -> set:
    """조건식 안의 EVENT_LOCAL(template=tid, key) EQ 리터럴(AND/OR 구분 없이 — 보수적 근사)."""
    out = set()
    if isinstance(pred, dict):
        ref = pred.get("ref")
        if pred.get("cmp") == "EQ" and isinstance(ref, dict) and ref.get("ref") == "EVENT_LOCAL" \
                and ref.get("template") == tid and ref.get("key") == key and "value" in pred:
            out.add(pred["value"])
        for v in pred.values():
            out |= _eq_literals(v, tid, key)
    elif isinstance(pred, list):
        for v in pred:
            out |= _eq_literals(v, tid, key)
    return out


def _step_infos(steps) -> set:
    out = set()
    for st in steps or ():
        for k in ("info", "exposes"):
            v = (st or {}).get(k)
            if isinstance(v, str):
                out.add(v)
            elif isinstance(v, list):
                out |= {x for x in v if isinstance(x, str)}
    return out


def _local_value(steps, key):
    for st in steps or ():
        if (st or {}).get("type") == "SET_EVENT_LOCAL" and st.get("key") == key:
            return st.get("value")
    return None


def check(raw: dict, definition: dict) -> list:
    """[(severity, code, message)] — variant_check가 없으면 []."""
    cfg = (raw or {}).get("variant_check")
    if cfg is None:
        return []
    if not isinstance(cfg, dict) or not isinstance(cfg.get("clues"), dict):
        return [(ERROR, "VARIANT_CHECK_INVALID", "variant_check는 {template, key, min_support?, clues{}}")]
    tid, key, need = cfg.get("template"), cfg.get("key"), cfg.get("min_support", 3)
    tpls = (definition or {}).get("event_templates") or {}
    tpl = tpls.get(tid)
    if not tpl or not isinstance(key, str) or not isinstance(need, int):
        return [(ERROR, "VARIANT_CHECK_INVALID", f"진상 사건 '{tid}'·키 '{key}'·min_support를 확인하십시오")]
    names = {a: (d or {}).get("legacy_key") for a, d in ((definition or {}).get("actor_definitions") or {}).items()}
    by_name = {v: a for a, v in names.items() if v}

    def same(v, target):
        return v == target or by_name.get(v) == target or by_name.get(target) == v

    variants = {}
    for c in tpl.get("transition_cases") or ():
        if c.get("from") is None:
            val = _local_value(c.get("steps"), key)
            if val is not None:
                variants.setdefault(val, set()).update(_step_infos(c.get("steps")))
    if not variants:
        return [(ERROR, "VARIANT_CHECK_NO_VARIANTS", f"'{tid}' spawn 케이스에 SET_EVENT_LOCAL({key})가 없습니다")]
    for other in tpls.values():
        spawn_req = _eq_literals(other.get("spawn_when"), tid, key)
        for ph in (other.get("phases") or {}).values():
            req = spawn_req | _eq_literals(ph.get("eligible_when"), tid, key)
            infos = {f["info"] for f in ph.get("finds") or () if isinstance(f, dict)}
            for val in variants:
                if not req or val in req:
                    variants[val] |= infos
    out, reached = [], set()
    for val in sorted(variants, key=str):
        reach = variants[val]
        reached |= reach
        support = [i for i, c in cfg["clues"].items() if i in reach and same(c.get("points_to"), val)]
        if len(support) < need:
            out.append((ERROR, "VARIANT_UNDER_SUPPORTED",
                        f"진상 {val}: 가리키는 단서 {len(support)}개(최소 {need}) — {sorted(support)}"))
        wrong = [i for i, c in cfg["clues"].items() if i in reach and c.get("strength") == "decisive"
                 and not same(c.get("points_to"), val)]
        if wrong:
            out.append((ERROR, "VARIANT_DECISIVE_MISLEAD", f"진상 {val}: 다른 사람을 확정하는 단서 {sorted(wrong)}"))
    for i in sorted(set(cfg["clues"]) - reached):
        out.append((WARNING, "VARIANT_CLUE_UNREACHABLE", f"어떤 진상에서도 도달하지 못하는 단서 {i}"))
    return out
