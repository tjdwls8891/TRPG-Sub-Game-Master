# 판정 수식 — 2d50 롤 (v7.12.0, 사용자 확정 설계)
#
#   R = 1d50 + 1d50 (독립)
#   T = 52 + A + S + E + M + H + D
#   성공 = R < T (동점 실패), 성공 여유 m = T - R
#
# A(능력)는 코드가 정본 원스탯에서 환산한다. M(행동)·H(상태)·D(난이도 단계)는 판단층위가
# 계약값으로 내고 코드가 범위를 검증한 뒤 조립한다. S(숙련)는 판단층위가 지목한 스킬이
# 그 PC 프로필 스킬 목록에 정확히 있을 때만 SKILL_BONUS, 그 밖(목록 밖·NPC·빈 값)은 0(v7.43).
# E(장비)는 판단층위가 지목한 PC의 착용·손에 든 물건 키의 효과 태그 중 그 판정 스탯에 해당하는 mod 합(v7.46,
# core.items.equip_mod — 그 밖·NPC는 0).
# 대성공·대실패 구간은 미정이라 두지 않는다. 이 모듈은 순수 함수만 둔다.
import random

BASE = 52
DIE = 50

DIFFICULTY = {
    "쉬움": 20,
    "약간 쉬움": 10,
    "보통": 0,
    "어려움": -15,
    "매우 어려움": -30,
    "극도로 어려움": -40,
}

KINDS = ("GENERAL", "CONTEST", "CRAFT")

METHOD_RANGE = (-12, 12)
STATUS_RANGE = (-15, 15)

SKILL_BONUS = 6
SKILL_FIELD = "스킬"


def skill_mod(profile: dict, skill) -> int:
    """지목 스킬이 PC 프로필 스킬 목록(쉼표 구분)에 정확히 있으면 SKILL_BONUS, 아니면 0."""
    name = str(skill or "").strip()
    raw = (profile or {}).get(SKILL_FIELD)
    owned = raw if isinstance(raw, list) else str(raw or "").split(",")
    return SKILL_BONUS if name and name in {str(x).strip() for x in owned} else 0

# v7.50(E5 · D-B3): 기술 종류 — 시나리오 skills 표의 항목은 설명 문자열(옛 형식 = 발동, +6) 또는 객체
#   {desc, kind?: 상시|발동(기본 발동), bonus?(기본 SKILL_BONUS), domains?: [굴림 분야], cost?: {status, stages}}.
#   상시 = 그 분야 굴림이면 코드가 자동으로 더한다(지목 불필요 — 가진 상시 기술 중 가장 큰 하나).
#   발동 = 판단층위가 굴림마다 지목할 때만(분야가 정해져 있으면 그 분야일 때만). cost가 있으면 쓸 때 그 상태 단계가
#   오르고(커밋), 비용 상태가 마지막 단계(고갈)면 보정 0·비용 없음.
SKILL_ALWAYS, SKILL_INVOKED = "상시", "발동"
SKILL_KINDS = (SKILL_ALWAYS, SKILL_INVOKED)
SKILL_KEYS = ("desc", "kind", "bonus", "domains", "cost")


def _find_skill(table, name):
    if not isinstance(table, dict):
        return None
    if name in table and (isinstance(table[name], str) or (isinstance(table[name], dict) and "desc" in table[name])):
        return table[name]
    for v in table.values():
        if isinstance(v, dict) and "desc" not in v:
            hit = _find_skill(v, name)
            if hit is not None:
                return hit
    return None


def skill_entry(scenario_data, name) -> dict | None:
    """기술 정의(정규화) — 표에 없으면 None(옛 동작: 프로필에 있으면 발동 +6)."""
    raw = _find_skill((scenario_data or {}).get("skills"), str(name or "").strip())
    if raw is None:
        return None
    if isinstance(raw, str):
        return {"desc": raw, "kind": SKILL_INVOKED, "bonus": SKILL_BONUS, "domains": None, "cost": None}
    return {"desc": str(raw.get("desc") or ""), "kind": raw.get("kind") or SKILL_INVOKED,
            "bonus": int(raw.get("bonus", SKILL_BONUS)), "domains": raw.get("domains"), "cost": raw.get("cost")}


def owned_skills(profile) -> list:
    raw = (profile or {}).get(SKILL_FIELD)
    vals = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [x for x in dict.fromkeys(str(v).strip() for v in vals) if x]


def is_invoked(scenario_data, name) -> bool:
    e = skill_entry(scenario_data, name)
    return e is None or e["kind"] == SKILL_INVOKED


def skill_terms(profile, scenario_data, designated, domain, exhausted=lambda _status: False) -> tuple:
    """(S, 표시, 비용 {상태: 단계}) — 상시 기술(분야 자동) 중 가장 큰 하나 + 지목한 발동 기술.

    domain이 없으면(레거시 판정) 상시는 붙지 않고 발동만 옛 규칙(+bonus)대로. exhausted(상태) = 비용 상태가 고갈인가."""
    owned = owned_skills(profile)
    best, best_name = 0, ""
    for n in owned:
        e = skill_entry(scenario_data, n)
        if e and e["kind"] == SKILL_ALWAYS and domain and domain in (e["domains"] or ()) and e["bonus"] > best:
            best, best_name = e["bonus"], n
    s, labels, spend = best, ([best_name] if best_name else []), {}
    name = str(designated or "").strip()
    if name and name in owned and name != best_name:
        e = skill_entry(scenario_data, name) or {"kind": SKILL_INVOKED, "bonus": SKILL_BONUS, "domains": None,
                                                  "cost": None}
        if e["kind"] == SKILL_INVOKED and not (e["domains"] and domain not in e["domains"]):
            cost = e["cost"] if isinstance(e["cost"], dict) else None
            if cost and exhausted(cost.get("status")):
                labels.append(f"{name}(고갈)")
            else:
                s += e["bonus"]
                labels.append(name)
                if cost:
                    spend[cost["status"]] = spend.get(cost["status"], 0) + int(cost.get("stages") or 1)
    return s, ", ".join(labels), spend


def check_skills(scenario_data, statuses=None) -> list:
    """skills 표 저작 검사 — [문구]. statuses = 공용·시나리오 상태 이름(비용 상태 검사)."""
    out = []

    def walk(t, path):
        for k, v in (t or {}).items():
            if isinstance(v, str):
                continue
            if isinstance(v, dict) and "desc" in v:
                p = f"{path}.{k}"
                extra = set(v) - set(SKILL_KEYS)
                if extra:
                    out.append(f"{p}: 알 수 없는 키 {sorted(extra)}")
                if v.get("kind", SKILL_INVOKED) not in SKILL_KINDS:
                    out.append(f"{p}.kind는 {'|'.join(SKILL_KINDS)}")
                if "bonus" in v and not (isinstance(v["bonus"], int) and not isinstance(v["bonus"], bool)
                                         and 0 <= v["bonus"] <= 20):
                    out.append(f"{p}.bonus는 0~20 정수")
                if v.get("kind") == SKILL_ALWAYS and not v.get("domains"):
                    out.append(f"{p}: 상시 기술은 domains(어느 분야 굴림에 붙는지)가 필요합니다")
                if v.get("kind") == SKILL_ALWAYS and v.get("cost"):
                    out.append(f"{p}: 상시 기술에는 cost를 둘 수 없습니다(발동 기술만)")
                c = v.get("cost")
                if c is not None and not (isinstance(c, dict) and isinstance(c.get("status"), str)
                                          and isinstance(c.get("stages", 1), int) and c.get("stages", 1) >= 1):
                    out.append(f"{p}.cost는 {{status, stages(1↑)}}")
                elif c and statuses is not None and c["status"] not in statuses:
                    out.append(f"{p}.cost.status {c['status']!r}는 정의되지 않은 상태")
            elif isinstance(v, dict):
                walk(v, f"{path}.{k}")
    walk((scenario_data or {}).get("skills"), "skills")
    return out


STAT_MIN, STAT_MAX = 1, 20


def ability_mod(stat_value) -> int | None:
    """원스탯(1~20 정수) → A = 2×(원스탯−10). 척도 밖·비정수는 None(환산 불가)."""
    if isinstance(stat_value, bool):
        return None
    if isinstance(stat_value, int):
        v = stat_value
    elif isinstance(stat_value, str) and stat_value.strip().lstrip("-").isdigit():
        v = int(stat_value.strip())
    else:
        return None
    if not STAT_MIN <= v <= STAT_MAX:
        return None
    return 2 * (v - 10)


def target(a: int, s: int, m: int, h: int, d: int, e: int = 0) -> int:
    return BASE + a + s + e + m + h + d


def roll_2d50(rng=random) -> tuple[int, int]:
    return rng.randint(1, DIE), rng.randint(1, DIE)


def is_success(r: int, t: int) -> bool:
    return r < t


def contest_outcome(m_a: int, m_b: int) -> str:
    """양측 성공 여유로 대결 결과. 'A'/'B' = 우세, 'TIE' = 같은 여유 교착, 'NONE' = 쌍방 실패(승자 없음)."""
    ok_a, ok_b = m_a > 0, m_b > 0
    if ok_a and ok_b:
        return "A" if m_a > m_b else "B" if m_b > m_a else "TIE"
    if ok_a:
        return "A"
    if ok_b:
        return "B"
    return "NONE"


def _int_in(v, lo, hi) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _side_errors(side: dict, where: str) -> list[str]:
    errs = []
    if not isinstance(side, dict):
        return [f"{where}: 객체가 아님"]
    if not isinstance(side.get("stat"), str) or not side["stat"].strip():
        errs.append(f"{where}.stat 누락")
    if side.get("difficulty") not in DIFFICULTY:
        errs.append(f"{where}.difficulty 허용값 아님({side.get('difficulty')!r})")
    if not _int_in(side.get("method_mod"), *METHOD_RANGE):
        errs.append(f"{where}.method_mod 범위 {METHOD_RANGE} 밖({side.get('method_mod')!r})")
    if not _int_in(side.get("status_mod"), *STATUS_RANGE):
        errs.append(f"{where}.status_mod 범위 {STATUS_RANGE} 밖({side.get('status_mod')!r})")
    return errs


def validate_rolls(rolls) -> list[str]:
    """판단층위 rolls 계약 검증. 오류 목록(빈 목록 = 통과). 값을 보정하지 않는다."""
    if not isinstance(rolls, list) or not rolls:
        return ["rolls 비어 있음"]
    errs = []
    for i, r in enumerate(rolls):
        where = f"rolls[{i}]"
        if not isinstance(r, dict):
            errs.append(f"{where}: 객체가 아님")
            continue
        if r.get("kind") not in KINDS:
            errs.append(f"{where}.kind 허용값 아님({r.get('kind')!r})")
        if not isinstance(r.get("char_name"), str) or not r["char_name"].strip():
            errs.append(f"{where}.char_name 누락")
        errs += _side_errors(r, where)
        if r.get("kind") == "CONTEST":
            opp = r.get("opponent")
            if not isinstance(opp, dict) or not isinstance(opp.get("name"), str) or not opp["name"].strip():
                errs.append(f"{where}.opponent.name 누락")
            else:
                errs += _side_errors(opp, f"{where}.opponent")
        # v7.14: 주변 발견 탐색 굴림 — 일반 판정(GENERAL)만
        if "discovery" in r and not isinstance(r["discovery"], bool):
            errs.append(f"{where}.discovery 불리언 아님")
        elif r.get("discovery") is True and r.get("kind") != "GENERAL":
            errs.append(f"{where}.discovery는 GENERAL만")
    if sum(1 for r in rolls if isinstance(r, dict) and r.get("discovery") is True) > 1:
        errs.append("discovery 굴림은 한 턴에 최대 1개")
    return errs


def success_chance(t: int) -> float:
    """P(R < T) — 2,500조합 전수(표시·검수용)."""
    hits = sum(1 for a in range(1, DIE + 1) for b in range(1, DIE + 1) if a + b < t)
    return hits / (DIE * DIE)
