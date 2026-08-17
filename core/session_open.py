# 세션 유지 시간 결정 — 입력 해석 결과를 실제 분(minute)으로 환산한다
#
# [모델과 코드의 분담]
#   모델은 '어떤 유형의 답인가'만 분류한다(prompts.CACHE_TIME_*).
#   실제 시간은 이 모듈이 고정표에서 꺼내거나 계산한다.
#   모델이 시간을 직접 정하면 같은 표현("적당히")에 매번 다른 값이 나온다.
#
# [해석 비용]
#   기획 규정 — 저비용 모델로 해석하고, 2잉크 이상 발생하는 경우에만
#   캐시 업로드 시점에 청구한다. 대개 1잉크 미만이라 사실상 무료다.
from .constants import CACHE_TTL_SECONDS
from .ink import cost_to_ink

# 비정량 표현의 정도별 고정 추천시간(분).
# 표현마다 매번 다른 값이 나오지 않도록 고정한다.
VAGUE_MINUTES = {"short": 60, "medium": 180, "long": 360}

# 턴당 예상 소요 시간(분). 실측 기반이며 세션 통계로 보정 가능.
MINUTES_PER_TURN = 4

# 턴 수 답변 시 곱하는 완충 계수. 예상보다 길어지는 경우를 대비한다.
TURN_BUFFER_RATIO = 1.3

# 판단을 맡긴 경우의 고정 추천시간(분).
RECOMMEND_MINUTES = 180

# 하한·상한. 상한은 캐시 TTL을 넘을 수 없다.
MIN_MINUTES = 10
MAX_MINUTES = CACHE_TTL_SECONDS // 60

# 해석 비용을 청구하는 하한(잉크). 미만이면 청구하지 않는다.
INTERPRET_CHARGE_THRESHOLD = 2


def resolve_minutes(result: dict) -> dict:
    """해석 결과를 실제 유지 시간으로 환산한다.

    Args:
        result: CACHE_TIME_RESPONSE_SCHEMA 응답

    Returns:
        {"ok": bool, "minutes": int, "case": str, "notes": [str], "retry": bool}
        retry=True면 재질문이 필요하다.
    """
    case = (result or {}).get("case") or "unclear"
    notes = []

    if case == "unclear":
        return {"ok": False, "minutes": 0, "case": case,
                "notes": ["입력을 이해하지 못했습니다."], "retry": True}

    if case == "explicit":
        try:
            minutes = int(result.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            return {"ok": False, "minutes": 0, "case": case,
                    "notes": ["시간을 읽지 못했습니다."], "retry": True}

    elif case == "vague":
        degree = result.get("degree") or "medium"
        minutes = VAGUE_MINUTES.get(degree, VAGUE_MINUTES["medium"])
        label = {"short": "조금", "medium": "적당히", "long": "넉넉히"}.get(degree, "적당히")
        notes.append(f"'{label}'라는 답변으로 이해하여 {minutes // 60}시간으로 잡았습니다.")

    elif case == "turns":
        try:
            turns = int(result.get("turns") or 0)
        except (TypeError, ValueError):
            turns = 0
        if turns <= 0:
            return {"ok": False, "minutes": 0, "case": case,
                    "notes": ["턴 수를 읽지 못했습니다."], "retry": True}
        base = turns * MINUTES_PER_TURN
        minutes = int(base * TURN_BUFFER_RATIO)
        notes.append(
            f"{turns}턴은 약 {base}분으로 예상되어 완충을 더해 {minutes}분으로 잡았습니다."
        )
        # 기획 규정 — 턴 수로 답한 경우 조기종료를 추천한다.
        notes.append("예정보다 일찍 끝나면 세션을 종료해 주십시오. 남은 유지비는 환급됩니다.")

    else:  # recommend
        minutes = RECOMMEND_MINUTES
        notes.append(f"기본 추천 시간인 {minutes // 60}시간으로 잡았습니다.")

    # 상·하한 적용 및 안내
    if minutes < MIN_MINUTES:
        notes.append(
            f"최소 유지 시간은 {MIN_MINUTES}분입니다. 너무 짧으면 캐시 생성 비용이 "
            f"유지 비용보다 커져 손해입니다."
        )
        minutes = MIN_MINUTES
    elif minutes > MAX_MINUTES:
        notes.append(
            f"최대 유지 시간은 {MAX_MINUTES // 60}시간입니다. "
            f"길게 잡으면 사용하지 않는 시간에도 유지비가 발생합니다."
        )
        minutes = MAX_MINUTES

    return {"ok": True, "minutes": minutes, "case": case, "notes": notes, "retry": False}


def should_charge_interpretation(session) -> tuple:
    """해석 비용을 청구할지 판정한다.

    기획 규정 — 2잉크 이상 발생하는 경우에만 캐시 업로드 시점에 청구한다.

    Returns:
        (청구 여부, 잉크)
    """
    krw = float(getattr(session, "interpret_cost_krw", 0.0) or 0.0)
    ink = cost_to_ink(krw)
    return (ink >= INTERPRET_CHARGE_THRESHOLD, ink)


def format_confirmation(resolved: dict, open_estimate: dict) -> str:
    """확인 메시지. 기획 규정 — 항상 출력하며 케이스별로 내용이 다르다."""
    minutes = resolved["minutes"]
    hours = minutes // 60
    mins = minutes % 60
    dur = f"{hours}시간 {mins}분" if hours and mins else (f"{hours}시간" if hours else f"{mins}분")

    lines = [f"⏱️ **세션 유지 시간: {dur}**"]
    for n in resolved.get("notes") or []:
        lines.append(f"> {n}")
    if open_estimate:
        lines.append(
            f"\n💰 오픈·유지 예상 비용 **{open_estimate.get('total_ink', 0)}잉크**\n"
            f"> 캐시 생성 {open_estimate.get('create_krw', 0):.1f}원 · "
            f"유지 {open_estimate.get('store_krw', 0):.1f}원"
        )
    lines.append("\n이대로 세션을 여시겠습니까?")
    return "\n".join(lines)
