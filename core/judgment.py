# 판정 수식 — 2d50 롤 (v7.12.0, 사용자 확정 설계)
#
#   R = 1d50 + 1d50 (독립)
#   T = 52 + A + S + M + H + D
#   성공 = R < T (동점 실패), 성공 여유 m = T - R
#
# A(능력)는 코드가 정본 원스탯에서 환산한다. M(행동)·H(상태)·D(난이도 단계)는 판단층위가
# 계약값으로 내고 코드가 범위를 검증한 뒤 조립한다. S(숙련)는 저작된 수치 스킬만 인정하며
# 현재 판정에는 쓰지 않는다(사용자 결정 2026-10-13 — 스킬 보너스·고유 규칙은 추후 통합) → 0.
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


def target(a: int, s: int, m: int, h: int, d: int) -> int:
    return BASE + a + s + m + h + d


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
    return errs


def success_chance(t: int) -> float:
    """P(R < T) — 2,500조합 전수(표시·검수용)."""
    hits = sum(1 for a in range(1, DIE + 1) for b in range(1, DIE + 1) if a + b < t)
    return hits / (DIE * DIE)
