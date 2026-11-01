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
# 리팩터링 T-3: 구현은 indaim.rules.checks.logic(단일 구현) — 여기는 다시 내보낸다.
import random  # noqa: F401

from indaim.rules.checks.logic import (  # noqa: F401
    BASE, DIE, DIFFICULTY, KINDS, METHOD_RANGE, SKILL_ALWAYS, SKILL_BONUS, SKILL_FIELD, SKILL_INVOKED, SKILL_KEYS, SKILL_KINDS, STATUS_RANGE, STAT_MAX, STAT_MIN, _find_skill, _int_in, _side_errors, ability_mod, check_skills, contest_outcome, is_invoked, is_success, owned_skills, roll_2d50, skill_entry, skill_mod, skill_terms, success_chance, target, validate_rolls)
