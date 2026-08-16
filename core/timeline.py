# 시나리오 시간선 — 작중 시간을 정량 관리하고 나이를 코드로 계산한다
#
# [설계 근거]
#   추출층위는 날짜·시간대를 문자열로 산출한다("1204/05/21", "오후").
#   문자열만으로는 경과 일수 비교·퀘스트 시간 조건 판정이 불가능하므로
#   정수로 환산해 함께 보관한다.
#
#   단위는 일(day) / 24시간 기준으로 확정되었다.
#
# [나이 계산을 코드가 맡는 이유]
#   LLM은 뺄셈에 비교적 강하지만 음수 부호를 누락하는 실패가 잦고,
#   다수 NPC의 상대 나이·항렬을 매 턴 파생시키면 오차가 예법 오류로
#   연쇄된다. 출생년도를 저장하고 나이는 코드가 계산해 주입한다.
import re

# 시간대 → 시(hour) 대표값. 추출층위가 산출하는 표현을 정규화한다.
TIME_OF_DAY_HOUR = {
    "새벽": 4,
    "아침": 7,
    "오전": 10,
    "정오": 12,
    "낮": 13,
    "오후": 15,
    "저녁": 18,
    "밤": 21,
    "심야": 1,
    "자정": 0,
}

DEFAULT_HOUR = 12


def parse_date(date_str: str) -> tuple | None:
    """작중 날짜 문자열을 (년, 월, 일)로 파싱한다.

    지원 형식: '1204/05/21', '1204-05-21', '1204년 5월 21일'
    파싱 불가 시 None.
    """
    if not date_str or date_str == "미확인":
        return None
    nums = re.findall(r"\d+", str(date_str))
    if len(nums) < 3:
        return None
    try:
        y, m, d = int(nums[0]), int(nums[1]), int(nums[2])
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return (y, m, d)


def to_day_number(date_str: str, days_per_month: int = 30,
                  months_per_year: int = 12) -> int | None:
    """작중 날짜를 통산 일수로 환산한다.

    NOTE: 가상 세계관은 실제 달력과 역법이 다를 수 있으므로 datetime을 쓰지 않고
          시나리오가 지정 가능한 단순 역법으로 계산한다.
          기본값은 1년 12개월 · 1개월 30일이다.
    """
    parsed = parse_date(date_str)
    if not parsed:
        return None
    y, m, d = parsed
    return (y * months_per_year + (m - 1)) * days_per_month + (d - 1)


def hour_of(time_of_day: str) -> int:
    """시간대 표현을 시(hour) 대표값으로 환산한다. 미확인은 정오."""
    if not time_of_day:
        return DEFAULT_HOUR
    for key, hour in TIME_OF_DAY_HOUR.items():
        if key in str(time_of_day):
            return hour
    return DEFAULT_HOUR


def get_calendar(session) -> dict:
    """시나리오가 지정한 역법. 없으면 기본값(12개월·30일)."""
    cal = {"days_per_month": 30, "months_per_year": 12}
    try:
        override = (session.scenario_data or {}).get("calendar") or {}
        for k in cal:
            if isinstance(override.get(k), int) and override[k] > 0:
                cal[k] = override[k]
    except Exception:
        pass
    return cal


def quantify(session, timeline: dict) -> dict:
    """world_timeline에 정량 필드를 추가해 반환한다.

    추가 필드:
        day_number   통산 일수 (경과 비교·퀘스트 시간 조건용)
        hour         시(0~23)
        elapsed_days 세션 시작일로부터 경과한 일수
    """
    tl = dict(timeline or {})
    cal = get_calendar(session)
    day = to_day_number(tl.get("current_date", ""), **cal)
    if day is not None:
        tl["day_number"] = day
        start = getattr(session, "start_day_number", None)
        if start is None:
            session.start_day_number = day
            start = day
        tl["elapsed_days"] = day - start
    tl["hour"] = hour_of(tl.get("time_of_day", ""))
    return tl


def current_year(session) -> int | None:
    """작중 현재 연도. 나이 계산의 기준."""
    tl = getattr(session, "world_timeline", {}) or {}
    parsed = parse_date(tl.get("current_date", ""))
    if parsed:
        return parsed[0]
    try:
        return (session.scenario_data or {}).get("start_year")
    except Exception:
        return None


def compute_age(session, birth_year) -> int | None:
    """출생년도로부터 작중 나이를 계산한다.

    NOTE: 모델에게 뺄셈을 시키지 않기 위해 코드가 계산한다.
          연 나이 방식(현재년도 - 출생년도)이며, 월·일은 고려하지 않는다.
    """
    if birth_year is None:
        return None
    year = current_year(session)
    if year is None:
        return None
    try:
        age = int(year) - int(birth_year)
    except (TypeError, ValueError):
        return None
    return age if age >= 0 else None


def enrich_npc_ages(session, npcs: dict) -> dict:
    """NPC 사전의 birth_year를 나이로 환산해 age 필드를 채운다.

    시나리오 JSON에 birth_year가 있는 NPC만 대상이며, 원본은 수정하지 않고
    사본을 반환한다. 캐시 조립·프롬프트 주입 시점에 호출한다.
    """
    out = {}
    for name, data in (npcs or {}).items():
        if not isinstance(data, dict):
            out[name] = data
            continue
        entry = dict(data)
        by = entry.get("birth_year")
        if by is not None:
            age = compute_age(session, by)
            if age is not None:
                entry["age"] = age
        out[name] = entry
    return out


def age_gap(session, birth_year_a, birth_year_b) -> int | None:
    """두 인물의 나이 차이. 절대값이 아니라 부호를 유지한다.

    a가 연상이면 양수. 모델이 음수 부호를 누락하는 실패를 피하기 위해
    이 계산도 코드가 맡는다.
    """
    try:
        return int(birth_year_b) - int(birth_year_a)
    except (TypeError, ValueError):
        return None


def format_timeline(timeline: dict) -> str:
    """표시용 문자열."""
    tl = timeline or {}
    parts = [f"{tl.get('current_date', '미확인')} {tl.get('time_of_day', '')}".strip()]
    if tl.get("elapsed_days") is not None:
        parts.append(f"(경과 {tl['elapsed_days']}일)")
    if tl.get("current_location"):
        parts.append(f"@ {tl['current_location']}")
    return " ".join(parts)
