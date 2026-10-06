# 정량 시간 — 달력 · canonical 분 시계 (H03-A)
#
# [권위 — DESIGN 03 · H03 결정 게이트 A5~A7]
#   정본 시간은 world_clock 하나다:
#     {"minute_index": int, "start_minute_index": int}     (초기화 전 레거시 = {})
#   minute_index = day_ordinal × 1440 + hour × 60 + minute. day_ordinal은 시나리오 달력의
#   1년 1월 1일을 1로 하는 일련번호다. 사람이 보는 날짜·시각·시간대는 전부 여기서 파생한다.
#
# [하지 않는 것]
#   · 현실 시각(wall-clock)을 읽지 않는다. 세션이 닫힌 동안 세계 시간은 흐르지 않는다.
#   · legacy core.timeline(12×30 기본 달력)을 새 정량 시간의 암묵 달력으로 쓰지 않는다 —
#     quantitative_time 시나리오는 달력을 명시해야 한다(컴파일러가 강제).
#   · 일반 진행에서 시간을 되돌리지 않는다(advance_to가 거부). 되감기는 턴 이력이 한다.
#   · core.timeline·world_timeline은 그대로 둔다(legacy production 경로, H07에서 은퇴).
from __future__ import annotations

import re
from dataclasses import dataclass

MINUTES_PER_DAY = 1440
# 정본 분의 하한 = 달력 일련번호 1(1년 1월 1일 00:00). Calendar.from_minute_index와 같은 계약.
MIN_MINUTE_INDEX = MINUTES_PER_DAY

CALENDAR_GREGORIAN = "GREGORIAN"
CALENDAR_FIXED_CUSTOM = "FIXED_CUSTOM"
CALENDAR_TYPES = (CALENDAR_GREGORIAN, CALENDAR_FIXED_CUSTOM)

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_GREG_MONTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


class ClockError(ValueError):
    """달력·시계 규칙 위반(존재하지 않는 날짜, 시간 후퇴, 미초기화 시계 등)."""


def parse_hhmm(text) -> int:
    """'HH:MM' → 하루 안의 분(0~1439)."""
    m = _HHMM.match(text) if isinstance(text, str) else None
    if not m:
        raise ClockError(f"'HH:MM' 형식이 아닙니다: {text!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def format_hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


@dataclass(frozen=True)
class DateTime:
    year: int
    month: int
    day: int
    hour: int
    minute: int

    def to_dict(self) -> dict:
        return {"year": self.year, "month": self.month, "day": self.day,
                "hour": self.hour, "minute": self.minute}

    def iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d} {self.hour:02d}:{self.minute:02d}"


class Calendar:
    """컴파일된 달력 정의(dict)의 순수 연산.

    compiled = {"calendar_id", "calendar_type", "month_lengths"(FIXED_CUSTOM),
                "dayparts": {id: {"start","end","representative"}}, "daypart_order": [...]}
    시간대 구간은 [start, end) 반열림이며 start > end이면 자정을 넘는다(예: 21:00~05:00).
    """

    def __init__(self, compiled: dict):
        if not isinstance(compiled, dict) or compiled.get("calendar_type") not in CALENDAR_TYPES:
            raise ClockError(f"알 수 없는 달력: {compiled!r}")
        self.calendar_id = compiled.get("calendar_id")
        self.calendar_type = compiled["calendar_type"]
        if self.calendar_type == CALENDAR_FIXED_CUSTOM:
            ml = compiled.get("month_lengths")
            if not ml or not all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in ml):
                raise ClockError("FIXED_CUSTOM 달력은 양의 정수 month_lengths가 필요합니다")
            self.month_lengths = tuple(ml)
            self.year_length = sum(ml)
        else:
            self.month_lengths = None
            self.year_length = None
        self.dayparts = {}
        for dp_id, dp in (compiled.get("dayparts") or {}).items():
            self.dayparts[dp_id] = (parse_hhmm(dp["start"]), parse_hhmm(dp["end"]),
                                    parse_hhmm(dp["representative"]))
        self.daypart_order = tuple(compiled.get("daypart_order") or sorted(
            self.dayparts, key=lambda k: (self.dayparts[k][0], k)))

    # ── 월 길이 ──
    def months_in_year(self, year: int) -> int:
        return 12 if self.calendar_type == CALENDAR_GREGORIAN else len(self.month_lengths)

    def days_in_month(self, year: int, month: int) -> int:
        if self.calendar_type == CALENDAR_GREGORIAN:
            if month == 2 and _is_leap(year):
                return 29
            return _GREG_MONTHS[month - 1]
        return self.month_lengths[month - 1]

    def _days_before_year(self, year: int) -> int:
        y = year - 1
        if self.calendar_type == CALENDAR_GREGORIAN:
            return y * 365 + y // 4 - y // 100 + y // 400
        return y * self.year_length

    def _check_date(self, year, month, day):
        for name, v in (("year", year), ("month", month), ("day", day)):
            if not isinstance(v, int) or isinstance(v, bool):
                raise ClockError(f"{name}는 정수여야 합니다: {v!r}")
        if year < 1:
            raise ClockError(f"연도는 1 이상이어야 합니다: {year}")
        if not 1 <= month <= self.months_in_year(year):
            raise ClockError(f"존재하지 않는 월: {year}-{month}")
        if not 1 <= day <= self.days_in_month(year, month):
            raise ClockError(f"존재하지 않는 날짜: {year}-{month}-{day}")

    # ── 일련번호 ──
    def to_ordinal(self, year: int, month: int, day: int) -> int:
        self._check_date(year, month, day)
        days = self._days_before_year(year)
        for m in range(1, month):
            days += self.days_in_month(year, m)
        return days + day

    def from_ordinal(self, ordinal: int) -> tuple:
        if not isinstance(ordinal, int) or ordinal < 1:
            raise ClockError(f"일련번호는 1 이상의 정수여야 합니다: {ordinal!r}")
        if self.calendar_type == CALENDAR_GREGORIAN:
            # 400년 주기(146097일)로 연도를 근사한 뒤 보정
            year = max(1, (ordinal * 400) // 146097)
            while self._days_before_year(year) >= ordinal:
                year -= 1
            while self._days_before_year(year + 1) < ordinal:
                year += 1
        else:
            year = (ordinal - 1) // self.year_length + 1
        rem = ordinal - self._days_before_year(year)
        month = 1
        while rem > self.days_in_month(year, month):
            rem -= self.days_in_month(year, month)
            month += 1
        return year, month, rem

    # ── 분 ──
    def to_minute_index(self, year, month, day, hour=0, minute=0) -> int:
        if not (isinstance(hour, int) and isinstance(minute, int) and 0 <= hour < 24 and 0 <= minute < 60):
            raise ClockError(f"시각이 잘못됐습니다: {hour}:{minute}")
        return self.to_ordinal(year, month, day) * MINUTES_PER_DAY + hour * 60 + minute

    def from_minute_index(self, index: int) -> DateTime:
        if not isinstance(index, int) or isinstance(index, bool):
            raise ClockError(f"minute_index는 정수여야 합니다: {index!r}")
        ordinal, mod = divmod(index, MINUTES_PER_DAY)
        y, m, d = self.from_ordinal(ordinal)
        return DateTime(y, m, d, mod // 60, mod % 60)

    # ── 시간대 ──
    def daypart_of(self, index: int) -> str | None:
        """minute_index가 속한 시간대 id. 어떤 구간에도 없으면 None."""
        mod = index % MINUTES_PER_DAY
        for dp_id in self.daypart_order:
            start, end, _ = self.dayparts[dp_id]
            if (start <= mod < end) if start < end else (mod >= start or mod < end):
                return dp_id
        return None

    def representative_minute_of_day(self, daypart_id: str) -> int:
        if daypart_id not in self.dayparts:
            raise ClockError(f"알 수 없는 시간대: {daypart_id!r}")
        return self.dayparts[daypart_id][2]

    def minute_index_at_daypart(self, year, month, day, daypart_id) -> int:
        """날짜 + 시간대 → 그 시간대 대표 시각의 minute_index(결정적)."""
        rep = self.representative_minute_of_day(daypart_id)
        return self.to_minute_index(year, month, day, rep // 60, rep % 60)


def daypart_contains(start: int, end: int, minute_of_day: int) -> bool:
    return (start <= minute_of_day < end) if start < end else (minute_of_day >= start or minute_of_day < end)


# ══════════════════════════════════════════════════════════════
#  world_clock 정본 dict 헬퍼
# ══════════════════════════════════════════════════════════════

def is_initialized(clock) -> bool:
    """초기화 '형태'인가(값 범위는 validate_clock이 본다)."""
    return (isinstance(clock, dict) and isinstance(clock.get("minute_index"), int)
            and isinstance(clock.get("start_minute_index"), int))


def validate_clock(clock) -> list:
    """정본 world_clock 형태 검사. {}(레거시 미초기화)는 정상."""
    if clock == {}:
        return []
    if not isinstance(clock, dict) or set(clock) != {"minute_index", "start_minute_index"}:
        return [f"world_clock 형태가 잘못됐습니다: {clock!r}"]
    errs = []
    for k in ("minute_index", "start_minute_index"):
        if not isinstance(clock[k], int) or isinstance(clock[k], bool):
            errs.append(f"{k}는 정수여야 합니다")
        elif clock[k] < MIN_MINUTE_INDEX:
            errs.append(f"{k}={clock[k]}가 달력 범위 밖입니다(≥ {MIN_MINUTE_INDEX}, 일련번호 ≥ 1)")
    if not errs and clock["minute_index"] < clock["start_minute_index"]:
        errs.append("minute_index가 start_minute_index보다 이릅니다")
    return errs


def initial_clock(minute_index: int) -> dict:
    if not isinstance(minute_index, int) or isinstance(minute_index, bool):
        raise ClockError("시작 minute_index는 정수여야 합니다")
    if minute_index < MIN_MINUTE_INDEX:
        raise ClockError(f"시작 minute_index {minute_index}가 달력 범위 밖입니다(≥ {MIN_MINUTE_INDEX})")
    return {"minute_index": minute_index, "start_minute_index": minute_index}


def current_minute(clock) -> int:
    if not is_initialized(clock):
        raise ClockError("world_clock이 초기화되지 않았습니다")
    return clock["minute_index"]


def advance_to(current: int, target: int) -> int:
    """일반 진행: target ≥ current만 허용(0분 진행 허용, 후퇴 거부)."""
    if not isinstance(target, int) or isinstance(target, bool):
        raise ClockError(f"target은 정수여야 합니다: {target!r}")
    if target < current:
        raise ClockError(f"시간은 후퇴하지 않습니다: {current} → {target}")
    return target


def add_minutes(current: int, minutes: int) -> int:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
        raise ClockError(f"경과 분은 0 이상의 정수여야 합니다: {minutes!r}")
    return current + minutes


def duration_between(a: int, b: int) -> int:
    return b - a


def elapsed_since_start(clock) -> int:
    return current_minute(clock) - clock["start_minute_index"]
