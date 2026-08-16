# 비용 예측 — 이번 턴 예상 비용을 상황에 맞춰 산출한다
#
# [설계 원칙]
#   입력은 추정하지 않는다. 프롬프트를 실제로 조립해 재면 되기 때문이다.
#   블록 구성이 현재 세션 상태(압축 기억 길이, 변경된 NPC 수, 최근 로그량,
#   퀘스트 주입 여부)로 결정되므로, 조립 결과를 재는 것이 유일하게 정확하다.
#   온디맨드 주입도 이 방식으로 자연히 반영된다.
#
#   진짜 불확실한 것은 출력, 특히 사고 토큰이다(가시 출력보다 클 때도 있다).
#   이것만 세션별 이동평균으로 관리하여, 시나리오 기본값에서 출발해
#   해당 세션의 실제 경향으로 수렴시킨다.
#
# [토큰 환산]
#   count_tokens API를 쓰지 않는다. 예측하는 데 비용과 지연이 발생하면
#   본말전도이기 때문이다. 문자 수 근사를 쓰고 계수는 실측으로 보정한다.
import math

from .constants import DEFAULT_MODEL, EXCHANGE_RATE
from .cost import calculate_text_gen_cost_breakdown
from .ink import cost_to_ink

# 한국어 문자당 토큰 근사 계수. [TOKENS] 실측 로그로 보정한다.
CHARS_TO_TOKENS = 0.65

# 미진행 턴의 데이터량을 평균보다 약간 많게 잡는 보수계수 (기획 규정).
CONSERVATIVE_FACTOR = 1.15

# 표본이 없을 때 쓰는 전역 기본값. 시나리오 cost_baseline으로 오버라이드.
DEFAULT_BASELINE = {
    "narration_out": 2400,     # 묘사층위 출력(사고 포함)
    "instruction_out": 620,    # 지시층위 출력
    "judgment_out": 280,       # 판단층위 출력
    "extraction_out": 340,     # 추출층위 출력
}

# 통계 표본 수에 따른 신뢰도 구간
CONFIDENCE_MID = 3
CONFIDENCE_HIGH = 10


def _tok(text: str) -> int:
    """문자열의 토큰 수를 근사한다."""
    return int(len(text or "") * CHARS_TO_TOKENS)


def get_baseline(session) -> dict:
    """시나리오 오버라이드를 반영한 출력 기본값."""
    merged = dict(DEFAULT_BASELINE)
    try:
        override = (session.scenario_data or {}).get("cost_baseline") or {}
        for k, v in override.items():
            if k in merged and isinstance(v, (int, float)):
                merged[k] = v
    except Exception:
        pass
    return merged


def _stats(session) -> dict:
    if not isinstance(getattr(session, "cost_stats", None), dict):
        session.cost_stats = {}
    return session.cost_stats


def update_stats(session, layer: str, out_tokens: int, thought_tokens: int = 0) -> dict:
    """실측 출력을 이동평균에 반영한다. 매 API 호출 후 호출한다.

    Args:
        layer: 'narration' | 'instruction' | 'judgment' | 'extraction'
        out_tokens: 사고 토큰이 합산된 출력 토큰
        thought_tokens: 그중 사고분

    Returns:
        갱신된 해당 층위 통계
    """
    st = _stats(session)
    entry = st.get(layer) or {"n": 0, "mean": 0.0, "min": None, "max": None,
                              "thought_mean": 0.0}
    n = entry["n"] + 1
    # 누적 평균 — 전체 표본을 들고 있지 않아도 갱신된다.
    entry["mean"] = entry["mean"] + (out_tokens - entry["mean"]) / n
    entry["thought_mean"] = entry["thought_mean"] + (thought_tokens - entry["thought_mean"]) / n
    entry["min"] = out_tokens if entry["min"] is None else min(entry["min"], out_tokens)
    entry["max"] = out_tokens if entry["max"] is None else max(entry["max"], out_tokens)
    entry["n"] = n
    st[layer] = entry
    return entry


def _out_range(session, layer: str, baseline_key: str) -> tuple:
    """해당 층위의 출력 토큰 예상 범위 (하한, 상한)."""
    st = _stats(session).get(layer)
    base = get_baseline(session)[baseline_key]
    if not st or st["n"] == 0:
        # 콜드 스타트 — 기본값 기준으로 넓게 잡는다.
        return (int(base * 0.6), int(base * 1.6 * CONSERVATIVE_FACTOR))
    lo = st["min"] if st["min"] is not None else int(st["mean"] * 0.6)
    hi = st["max"] if st["max"] is not None else int(st["mean"] * 1.6)
    # 관측 최대보다 보수적으로 잡아, 예상 초과로 잔액이 음수가 되는 일을 줄인다.
    return (int(lo), int(hi * CONSERVATIVE_FACTOR))


def estimate_input_tokens(session, action: str = "PROCEED") -> dict:
    """이번 턴에 실릴 입력을 실제 조립해 층위별로 산출한다.

    NOTE: 추정이 아니라 조립 결과의 실측이다. 실패 시에도 예측이 중단되지
          않도록 방어적으로 0을 반환하고 상위에서 기본값으로 처리한다.

    Returns:
        {"judgment": int, "instruction": int, "narration": int,
         "extraction": int, "cached": int}
    """
    out = {"judgment": 0, "instruction": 0, "narration": 0, "extraction": 0, "cached": 0}
    out["cached"] = int(getattr(session, "cache_tokens", 0) or 0)

    # 지시·묘사층위가 공유하는 프롬프트 본문 — 실제 빌더로 조립한다.
    try:
        from .prompt import PromptBuilder
        assembled = PromptBuilder.build_prompt(session, "(예상 산출용)")
        body = _tok(assembled)
    except Exception:
        body = 0
    out["instruction"] = body
    out["narration"] = body

    # 판단층위 — 최근 로그 5개 + 능력치 + 노트 (캐시 미사용)
    try:
        recent = []
        for c in (session.raw_logs or [])[-5:]:
            try:
                recent.append(c.parts[0].text or "")
            except (AttributeError, IndexError):
                continue
        judged = "\n".join(recent) + (getattr(session, "note", "") or "")
        out["judgment"] = _tok(judged) + 200   # 지시문·능력치 블록 고정분
    except Exception:
        out["judgment"] = 400

    # 추출층위 — 묘사문(최대 3000자) + 타겟 목록
    narr_hi = _out_range(session, "narration", "narration_out")[1]
    out["extraction"] = min(int(narr_hi), _tok("x" * 3000)) + 300
    return out


def estimate_turn(session, action: str = "PROCEED") -> dict:
    """이번 턴 예상 비용을 범위로 산출한다.

    action별 호출 구성이 다르다 (4층위 분리의 결과):
        ASK     판단만                      → 캐시 읽기 0회
        ROLL    판단만 (이후 재진입)         → 캐시 읽기 0회
        NARRATE 판단 + 지시 + 경량묘사        → 캐시 읽기 2회
        PROCEED 판단 + 지시 + 묘사 + 추출     → 캐시 읽기 2회

    Returns:
        {"min_krw", "max_krw", "min_ink", "max_ink",
         "breakdown": {...}, "confidence": "low|mid|high"}
    """
    inp = estimate_input_tokens(session, action)
    model = DEFAULT_MODEL

    def _cost(in_t, out_t, cached_t):
        try:
            return calculate_text_gen_cost_breakdown(
                model, input_tokens=int(in_t), output_tokens=int(out_t),
                cached_read_tokens=int(cached_t),
            )["total_krw"]
        except Exception:
            return 0.0

    lo_total = hi_total = 0.0
    breakdown = {}

    # 판단층위 — 모든 action에 공통
    j_lo, j_hi = _out_range(session, "judgment", "judgment_out")
    breakdown["판단층위"] = (_cost(inp["judgment"], j_lo, 0), _cost(inp["judgment"], j_hi, 0))

    if action in ("NARRATE", "PROCEED"):
        i_lo, i_hi = _out_range(session, "instruction", "instruction_out")
        breakdown["지시층위"] = (_cost(inp["instruction"], i_lo, inp["cached"]),
                              _cost(inp["instruction"], i_hi, inp["cached"]))
        n_lo, n_hi = _out_range(session, "narration", "narration_out")
        breakdown["묘사층위"] = (_cost(inp["narration"], n_lo, inp["cached"]),
                              _cost(inp["narration"], n_hi, inp["cached"]))

    if action == "PROCEED":
        e_lo, e_hi = _out_range(session, "extraction", "extraction_out")
        breakdown["추출층위"] = (_cost(inp["extraction"], e_lo, 0), _cost(inp["extraction"], e_hi, 0))

    for lo, hi in breakdown.values():
        lo_total += lo
        hi_total += hi

    # 표본 수가 가장 적은 층위를 기준으로 신뢰도를 매긴다.
    st = _stats(session)
    ns = [v.get("n", 0) for v in st.values()] or [0]
    n_min = min(ns)
    confidence = "high" if n_min >= CONFIDENCE_HIGH else ("mid" if n_min >= CONFIDENCE_MID else "low")

    return {
        "min_krw": round(lo_total, 2),
        "max_krw": round(hi_total, 2),
        "min_ink": cost_to_ink(lo_total),
        "max_ink": cost_to_ink(hi_total),
        "breakdown": {k: (round(v[0], 2), round(v[1], 2)) for k, v in breakdown.items()},
        "confidence": confidence,
        "input_tokens": inp,
    }


def estimate_session_open(session, hours: float) -> dict:
    """세션 오픈(캐시 업로드) 및 유지 비용.

    cache_tokens 실측값 기반이라 정확도가 높다.
    """
    from .cost import calculate_upload_cost

    tokens = int(getattr(session, "cache_tokens", 0) or 0)
    try:
        create_krw = calculate_upload_cost(DEFAULT_MODEL, tokens)
    except Exception:
        create_krw = 0.0
    # 저장 단가는 시간당 과금. constants의 단가표를 그대로 쓴다.
    try:
        from .constants import PRICING_1M
        store_rate = PRICING_1M[DEFAULT_MODEL].get("CACHE_STORAGE_PER_HOUR", 0.0)
    except Exception:
        store_rate = 0.0
    store_krw = tokens / 1_000_000 * store_rate * hours * EXCHANGE_RATE

    total = create_krw + store_krw
    return {
        "cache_tokens": tokens,
        "create_krw": round(create_krw, 2),
        "store_krw": round(store_krw, 2),
        "total_krw": round(total, 2),
        "total_ink": cost_to_ink(total),
        "hours": hours,
    }


def format_estimate(est: dict, tts: dict | None = None) -> str:
    """디스플레이 표기용 문자열. TTS는 합산하지 않고 구분 표기한다(기획 규정)."""
    mark = {"low": "?", "mid": "~", "high": ""}.get(est.get("confidence", "low"), "")
    line = f"이번 턴 예상: {est['min_ink']}~{est['max_ink']}잉크{mark}"
    if tts:
        line += f"\n        TTS: +{tts.get('min_ink', 0)}~{tts.get('max_ink', 0)}잉크"
    return line
