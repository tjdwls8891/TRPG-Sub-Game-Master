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

# 프롬프트 조립 실패 시 사용할 입력 토큰 보수 기본값.
# 0으로 두면 예측이 실제보다 크게 낮아져 잔액 차단이 무력화된다.
FALLBACK_BODY_TOKENS = 3000

# 표본이 없을 때 쓰는 전역 기본값. 시나리오 cost_baseline으로 오버라이드.
DEFAULT_BASELINE = {
    "narration_out": 2400,     # 묘사층위 출력(사고 포함)
    "instruction_out": 620,    # 지시층위 출력
    "judgment_out": 280,       # 판단층위 출력
    "extraction_out": 340,     # 추출층위 출력
    "simulation_out": 2100,    # 개연성 시뮬레이션 출력(사고 포함)
}

# 개연성 시뮬레이션 비캐시 입력의 고정분(시뮬레이터 지시문·지시사항) — 최근 로그 4개는 따로 센다.
SIMULATION_FIXED_TOKENS = 1700
# v7.23 DYNAMIC 조건부 선언 검증(발견·결과 선취 때만) — 비캐시 입력 고정분·출력(사고 포함) 상한
DECLARATION_CHECK_FIXED_TOKENS = 1200
DECLARATION_CHECK_OUT = 900

# 통계 표본 수에 따른 신뢰도 구간
CONFIDENCE_MID = 3
CONFIDENCE_HIGH = 10

# ── P1: 원장(CostLedger) 파생 통계 — 세션 표본이 부족할 때의 상위 그룹(시나리오 → 전역) ──
#   · 토큰 분포만 쓴다(금액은 **현재 가격표**로 다시 계산 — 옛 단가로 미래를 예측하지 않음).
#   · 같은 모델의 공급자 실측(PROVIDER_METADATA)만. 결측 차원(옛 이벤트의 scenario_id 없음)은 전역에만 든다.
#   · 표본 MIN_LEDGER_SAMPLES 미만 그룹은 쓰지 않는다(한두 건으로 급전환 금지).
MIN_LEDGER_SAMPLES = 5
# v7.30(사용자 결정): 범위 폭 = 가변분·출력 분포의 p10~p90. 조건부 호출(선언 검증·부가 호출)은 관측 발생률이
#   COND_RATE_MIN 이상일 때만 상한에 넣는다(발생률 미상이면 넣지 않고 표시만).
P_LO, P_HI = 0.1, 0.9
COND_RATE_MIN = 0.2
SAMPLE_CAP = 40
ACCURACY_PATH = "data/cost_estimate_accuracy.jsonl"   # 예측·실제 대조(시나리오·모드) — 예측 폭 되먹임 원천
FEEDBACK_MIN, FEEDBACK_WINDOW = 10, 20
LAYER_OPS = {"judgment": "TURN_JUDGMENT", "simulation": "TURN_SIMULATION", "instruction": "TURN_INSTRUCTION",
             "narration": "TURN_NARRATION", "extraction": "TURN_EXTRACTION"}
AUX_OPS = ("TURN_IRREGULAR_NPC", "TURN_NPC_PROFILE", "TURN_NARRATIVE_PLANNING")   # 조건부 부가 호출
_STATS_CACHE: dict = {}


def _pct(vals, q):
    v = sorted(vals)
    if not v:
        return 0
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))]


def _priced(model, in_t, cached_t, out_t) -> float:
    try:
        return calculate_text_gen_cost_breakdown(model, input_tokens=int(in_t), output_tokens=int(out_t),
                                                 cached_read_tokens=int(cached_t))["total_krw"]
    except Exception:
        return 0.0


def ledger_profile(path: str | None = None) -> dict | None:
    """원장 → {scope: {op: {"out": [..], "fresh": [..]}}, "turns": {scope: {"aux": [krw..], "tts": [krw..]}}}.

    scope = "*"(전역) 또는 scenario_id. 파일 크기·수정 시각이 같으면 캐시를 재사용한다. 원장이 없으면 None."""
    import os
    from . import cost_ledger as CL
    from .constants import TTS_MODEL
    path = path or CL.DEFAULT_LEDGER_PATH
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (os.path.abspath(path), st.st_size, st.st_mtime_ns)
    hit = _STATS_CACHE.get("key") == key and _STATS_CACHE.get("val")
    if hit:
        return hit
    try:
        rows = CL.CostLedger(path).list_cost_events()
    except Exception:
        return None
    ops, by_tx = {}, {}
    for r in rows:
        if r.get("usage_source") != CL.SOURCE_PROVIDER_METADATA or not r.get("success", True):
            continue
        op = r.get("operation")
        md = r.get("metadata") or {}
        sc = md.get("scenario_id") or None
        scm = f"{sc}|{md['world_mode']}" if sc and md.get("world_mode") else None   # v7.30: 시나리오 + 모드
        model = r.get("model") or DEFAULT_MODEL
        fresh = max(0, int(r.get("input_tokens") or 0) - int(r.get("cached_input_tokens") or 0))
        if op in LAYER_OPS.values() and model == DEFAULT_MODEL:
            for scope in tuple(x for x in ("*", sc, scm) if x):
                d = ops.setdefault(scope, {}).setdefault(op, {"out": [], "fresh": []})
                d["out"].append(int(r.get("output_tokens") or 0))
                d["fresh"].append(fresh)
        tx = r.get("transaction_id")
        if tx:
            t = by_tx.setdefault(tx, {"sc": sc, "scm": scm, "narr": False, "aux": 0.0, "tts": 0.0, "ops": set()})
            t["sc"], t["scm"] = t["sc"] or sc, t["scm"] or scm
            t["ops"].add(op)
            if op == "TURN_EXTRACTION":
                t["ext"] = t.get("ext", 0) + 1
        if tx and (op == "TURN_NARRATION" or op in AUX_OPS or op == "TTS_RUNTIME"):
            if op == "TURN_NARRATION":
                t["narr"] = True
            elif op == "TTS_RUNTIME":
                t["tts"] += _priced(TTS_MODEL, r.get("input_tokens") or 0, 0,
                                    r.get("audio_output_tokens") or r.get("output_tokens") or 0)
            else:
                t["aux"] += _priced(model, r.get("input_tokens") or 0, r.get("cached_input_tokens") or 0,
                                    r.get("output_tokens") or 0)
    turns, counts = {}, {}
    for t in by_tx.values():
        if not t["narr"]:
            continue                                  # 정상 묘사가 있었던 턴만(부가 호출 발생률의 분모)
        for scope in tuple(x for x in ("*", t["sc"], t["scm"]) if x):
            d = turns.setdefault(scope, {"aux": [], "tts": []})
            d["aux"].append(t["aux"])
            if t["tts"] > 0:
                d["tts"].append(t["tts"])
            c = counts.setdefault(scope, {"_turns": 0})
            c["_turns"] += 1
            for o in t["ops"] | ({"_AUX"} if t["ops"] & set(AUX_OPS) else set()) \
                    | ({"_EXTRACTION_RETRY"} if t.get("ext", 0) >= 2 else set()):
                c[o] = c.get(o, 0) + 1
    rates = {sc_: {o: n / c["_turns"] for o, n in c.items() if o != "_turns"} | {"_turns": c["_turns"]}
             for sc_, c in counts.items()}
    val = {"ops": ops, "turns": turns, "rates": rates}
    _STATS_CACHE.update(key=key, val=val)
    return val


def _ledger_group(session, getter):
    """(표본, 근거 라벨) — 시나리오 그룹 → 전역 그룹 순서로 표본이 충분한 첫 그룹."""
    prof = ledger_profile()
    if not prof:
        return None, None
    sc = getattr(session, "scenario_id", None)
    scm = f"{sc}|{_mode(session)}" if sc else None
    for scope, label in (((scm, "시나리오·모드 원장"), (sc, "시나리오 원장")) if sc else ()) + (("*", "전역 원장"),):
        vals = getter(prof, scope)
        if vals and len(vals) >= MIN_LEDGER_SAMPLES:
            return vals, label
    return None, None


def _mode(session) -> str:
    """원장 차원과 같은 모드 판정(정의 바인딩 = DYNAMIC) — cost_ledger.session_dims와 같은 규칙."""
    return "DYNAMIC" if getattr(session, "definition_revision", None) else "LEGACY"


def cond_rate(session, op: str):
    """조건부 호출의 관측 발생률(정상 묘사 턴 대비) — 시나리오·모드 → 시나리오 → 전역, 표본 부족이면 None."""
    prof = ledger_profile()
    if not prof:
        return None
    sc = getattr(session, "scenario_id", None)
    for scope in ((f"{sc}|{_mode(session)}", sc) if sc else ()) + ("*",):
        r = (prof.get("rates") or {}).get(scope)
        if r and r.get("_turns", 0) >= MIN_LEDGER_SAMPLES:
            return float(r.get(op, 0.0))
    return None


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
    entry["samples"] = (list(entry.get("samples") or []) + [int(out_tokens)])[-SAMPLE_CAP:]   # v7.30: p10~p90용
    st[layer] = entry
    return entry


def _out_range(session, layer: str, baseline_key: str) -> tuple:
    """해당 층위의 출력 토큰 예상 범위 (하한, 상한).

    P1: 세션 표본 < CONFIDENCE_MID면 원장 통계(시나리오 → 전역, 같은 모델) → 기본값 순서로 대체한다."""
    st = _stats(session).get(layer)
    base = get_baseline(session)[baseline_key]
    if not st or st["n"] < CONFIDENCE_MID:
        op = LAYER_OPS.get(layer)
        vals, _label = _ledger_group(session, lambda p, sc: ((p["ops"].get(sc) or {}).get(op) or {}).get("out")) \
            if op else (None, None)
        if vals:
            return (int(_pct(vals, P_LO)), int(_pct(vals, P_HI)))
    if not st or st["n"] == 0:
        # 콜드 스타트 — 기본값 기준으로 넓게 잡는다.
        return (int(base * 0.6), int(base * 1.6 * CONSERVATIVE_FACTOR))
    if len(st.get("samples") or ()) >= CONFIDENCE_MID:          # v7.30: 세션 실측 분포의 p10~p90
        return (int(_pct(st["samples"], P_LO)), int(_pct(st["samples"], P_HI)))
    lo = st["min"] if st["min"] is not None else int(st["mean"] * 0.6)
    hi = st["max"] if st["max"] is not None else int(st["mean"] * 1.6)
    return (int(lo), int(hi))


def estimate_input_tokens(session, action: str = "PROCEED") -> dict:
    """이번 턴에 실릴 입력을 실제 조립해 층위별로 산출한다.

    NOTE: 추정이 아니라 조립 결과의 실측이다. 실패 시에도 예측이 중단되지
          않도록 방어적으로 0을 반환하고 상위에서 기본값으로 처리한다.

    Returns:
        {"judgment": int, "instruction": int, "narration": int,
         "extraction": int, "simulation": int, "cached": int}
    """
    out = {"judgment": 0, "instruction": 0, "narration": 0, "extraction": 0, "simulation": 0, "cached": 0}
    # 실제 관측된 캐시 읽기량을 우선 사용한다. cache_tokens는 조립 텍스트 기준이라
    # 캐시 본문에 함께 구워진 시스템 지시문(약 8천 토큰)이 빠져 있다.
    out["cached"] = int(getattr(session, "cache_read_tokens", 0)
                        or getattr(session, "cache_tokens", 0) or 0)

    # 지시·묘사층위가 공유하는 프롬프트 본문 — 실제 빌더로 조립한다.
    try:
        from .prompt import PromptBuilder
        assembled = PromptBuilder.build_prompt(session, "(예상 산출용)")
        body = _tok(assembled)
    except Exception as e:
        # 조립 실패를 조용히 0으로 삼키면 예측이 실제보다 크게 낮아진다.
        # 원인을 드러내고, 최소한 캐시 외 입력이 0이 되지 않도록 보수적 기본값을 쓴다.
        print(f"⚠️ [예측] 프롬프트 조립 실패 — 기본값 사용: {type(e).__name__}: {e}")
        body = FALLBACK_BODY_TOKENS
    # v7.30(D2): 보정계수는 층위별 — 지시·묘사의 실제 입력 구성이 달라 하나를 공유하면 서로를 흔든다
    out["instruction"] = int(body * get_calibration(session, "instruction"))
    out["narration"] = int(body * get_calibration(session, "narration"))

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
        out["simulation"] = _tok("\n".join(recent[-4:])) + SIMULATION_FIXED_TOKENS
    except Exception:
        out["judgment"] = 400
        out["simulation"] = SIMULATION_FIXED_TOKENS

    # 추출층위 — 묘사문(최대 3000자) + 타겟 목록
    narr_hi = _out_range(session, "narration", "narration_out")[1]
    out["extraction"] = min(int(narr_hi), _tok("x" * 3000)) + 300

    # P1: 조립으로 재지 못하는 블록(동적 세계 장소·장면 인물·사건 가이드·행동/관찰/아이템 매니페스트 등)이
    #   있다 — 원장의 같은 층위 실측 비캐시 입력 중앙값보다 작게 잡지 않는다(과소 예측 방지).
    for layer, op in LAYER_OPS.items():
        vals, _label = _ledger_group(session, lambda p, sc, _op=op: ((p["ops"].get(sc) or {}).get(_op) or {}).get("fresh"))
        if vals:
            out[layer] = max(int(out[layer]), int(_pct(vals, 0.5)))

    # 다음 턴 실측과 대조하기 위해 예측값을 보관한다(런타임 전용).
    session._last_input_estimate = dict(out)
    return out


def census_range(session, layer: str):
    """v7.30: 실제 보낸 입력의 분류 이력(prompt_census) → 다음 호출 비캐시 입력 (lo, mid, hi).

    확정 = 직전 관측값(다음 턴에도 반드시 들어가는 블록), 유력·가변 = 최근 분포의 p10~p90(중앙은 p50).
    표본 CONFIDENCE_MID 미만이면 None(기존 조립·원장 방식으로 대체)."""
    from .prompt_census import FIXED, LIKELY, VARIABLE
    hist = (_stats(session).get("_census") or {}).get(layer) or []
    if len(hist) < CONFIDENCE_MID:
        return None
    fixed = int(hist[-1].get(FIXED, 0))
    lo = mid = hi = fixed
    for c in (LIKELY, VARIABLE):
        vals = [int(h.get(c, 0)) for h in hist]
        lo += int(_pct(vals, P_LO))
        mid += int(_pct(vals, 0.5))
        hi += int(_pct(vals, P_HI))
    return lo, mid, hi


def _scope_key(session) -> str:
    return f"{getattr(session, 'scenario_id', None) or '*'}|{_mode(session)}"


_ACC_CACHE: dict = {}


def feedback_factor(session, path: str | None = None):
    """예측 정확도 되먹임 — 같은 시나리오·모드의 최근 FEEDBACK_WINDOW건 실제/예측중앙 비율 중앙값(0.7~1.3).

    표본 FEEDBACK_MIN 미만이면 1.0. 파일이 없거나 읽지 못하면 1.0(예측은 계속된다)."""
    import os
    path = path or ACCURACY_PATH
    try:
        st = os.stat(path)
    except OSError:
        return 1.0
    key = (path, st.st_mtime_ns, st.st_size)
    rows = _ACC_CACHE.get(key)
    if rows is None:
        import json
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return 1.0
        _ACC_CACHE.clear()
        _ACC_CACHE[key] = rows
    scope = _scope_key(session)
    ratios = [r["actual_krw"] / r["mid_krw"] for r in rows
              if r.get("scope") == scope and (r.get("mid_krw") or 0) > 0 and r.get("actual_krw") is not None]
    ratios = ratios[-FEEDBACK_WINDOW:]
    if len(ratios) < FEEDBACK_MIN:
        return 1.0
    return max(0.7, min(1.3, float(_pct(ratios, 0.5))))


_LINE_OF_LABEL = {"선언 검증": "선언 검증(조건부)", "NPC 설정 생성": "부가 호출(조건부)",
                  "비정규 NPC 배정": "부가 호출(조건부)", "서사 설계자(계획)": "부가 호출(조건부)"}
_LINE_OF_LAYER = {"judgment": "판단층위", "instruction": "지시층위", "narration": "묘사층위", "extraction": "추출층위"}


def actual_by_line(cost_log) -> dict:
    """턴 호출 내역(turn_cost_log) → 예상 내역과 같은 항목명별 실제 원가(TTS는 예상과 별도라 뺀다).
    같은 턴 두 번째 추출 호출은 '추출 재시도(조건부)'다."""
    from .constants import TTS_MODEL
    out, ext = {}, 0
    for e in cost_log or ():
        if e.get("model") == TTS_MODEL or str(e.get("label", "")).startswith("TTS"):
            continue
        name = _LINE_OF_LAYER.get(e.get("layer")) or _LINE_OF_LABEL.get(e.get("label")) or e.get("label") or "?"
        if name == "추출층위":
            ext += 1
            if ext >= 2:
                name = "추출 재시도(조건부)"
        if str(name).startswith("묘사층위"):
            name = "묘사층위"
        out[name] = out.get(name, 0.0) + float(e.get("cost") or 0.0)
    return out


def record_accuracy(session, est: dict | None, actual_krw: float, per_layer: dict | None = None,
                    *, transaction_id: str | None = None, path: str | None = None) -> dict | None:
    """커밋된 턴의 예측·실제를 append-only로 남긴다(예측 데이터 자동 수집 — 다음 예측 폭 되먹임 원천).

    실패는 흡수한다(보고·예측 보조 — 청구와 무관)."""
    if not est:
        return None
    import json
    import os
    import time
    row = {"t": round(time.time(), 1), "scope": _scope_key(session),
           "session_id": getattr(session, "session_id", None), "tx": transaction_id,
           "turn": getattr(session, "turn_count", None),
           "min_krw": est.get("min_krw"), "mid_krw": est.get("mid_krw"), "max_krw": est.get("max_krw"),
           "actual_krw": round(float(actual_krw or 0.0), 4),
           "in_range": bool((est.get("min_krw") or 0) <= (actual_krw or 0) <= (est.get("max_krw") or 0)),
           "layers": {k: {"pred": list(v), "actual": round(float((per_layer or {}).get(k, 0.0)), 4)}
                      for k, v in (est.get("breakdown") or {}).items()},
           "unlisted_actual": {k: round(float(v), 4) for k, v in (per_layer or {}).items()
                               if k not in (est.get("breakdown") or {})}}
    try:
        path = path or ACCURACY_PATH
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[EST] 정확도 기록 실패(무시): {e}")
        return None
    return row


def estimate_turn(session, action: str = "PROCEED") -> dict:
    """이번 턴 예상 비용을 범위로 산출한다.

    action별 호출 구성이 다르다 (4층위 분리의 결과):
        ASK     판단만                      → 캐시 읽기 0회
        ROLL    판단만 (굴림 뒤 지시층위부터 이어 감) → 캐시 읽기 0회
        NARRATE 판단 + 지시 + 경량묘사        → 캐시 읽기 2회
        PROCEED 판단 + 지시 + 묘사 + 추출     → 캐시 읽기 2회
    캐시가 유효하면 매 판단 전에 개연성 시뮬레이션(캐시 읽기 1회)이 함께 돈다.
    ROLL을 거친 턴은 굴림 뒤 지시층위만 다시 부른다(시뮬레이션·판단 재진입 없음) —
    PROCEED 구성과 호출 수가 같으므로 따로 더하지 않는다.

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

    breakdown, mids, conditional, in_ranges = {}, {}, {}, {}
    c = inp["cached"]

    def _layer(name, layer, base_key, cached):
        """한 층위: 비캐시 입력 (lo, mid, hi) — 입력 분류 이력이 있으면 확정 + 유력·가변 p10~p90, 없으면 조립·원장 점값."""
        cr = census_range(session, layer)
        lo_in, mid_in, hi_in = cr if cr else (inp[layer],) * 3
        in_ranges[layer] = {"lo": lo_in, "mid": mid_in, "hi": hi_in, "basis": "입력 분류" if cr else "조립·원장"}
        o_lo, o_hi = _out_range(session, layer, base_key)
        breakdown[name] = (_cost(lo_in + cached, o_lo, cached), _cost(hi_in + cached, o_hi, cached))
        mids[name] = _cost(mid_in + cached, (o_lo + o_hi) / 2, cached)

    def _conditional(name, op, cost_hi):
        """조건부 호출(사용자 결정 3.b) — 관측 발생률 ≥ COND_RATE_MIN일 때만 상한에, 중앙엔 발생률 × 비용."""
        rate = cond_rate(session, op)
        inc = rate is not None and rate >= COND_RATE_MIN and cost_hi > 0
        conditional[name] = {"rate": None if rate is None else round(rate, 3), "included": inc,
                             "cost": round(float(cost_hi), 2)}
        if inc:
            breakdown[name] = (0.0, float(cost_hi))
            mids[name] = float(cost_hi) * rate

    # 판단층위 — 모든 action에 공통
    _layer("판단층위", "judgment", "judgment_out", 0)

    # 개연성 시뮬레이션 — 판단 전에 실행(gm._call_gm_logic의 do_simulation과 같은 조건).
    # v7.23: DYNAMIC은 매 발화 시뮬레이션이 없다 — 판단이 발견·결과 선취를 표시할 때만 선언 검증 1회(조건부).
    sim = _simulation_runs(session)
    if sim and _dynamic(session):
        g_in = c + DECLARATION_CHECK_FIXED_TOKENS + inp["simulation"] - SIMULATION_FIXED_TOKENS
        _conditional("선언 검증(조건부)", "TURN_DECLARATION_CHECK", _cost(g_in, DECLARATION_CHECK_OUT, c))
    elif sim:
        s_lo, s_hi = _out_range(session, "simulation", "simulation_out")
        s_in = inp["simulation"] + c
        breakdown["개연성 시뮬레이션"] = (_cost(s_in, s_lo, c), _cost(s_in, s_hi, c))
        mids["개연성 시뮬레이션"] = _cost(s_in, (s_lo + s_hi) / 2, c)

    if action in ("NARRATE", "PROCEED"):
        # 단가 함수의 input_tokens는 캐시 적중분을 포함한 총량이다(본문만 넘기면 비캐시 입력이 0으로 계산된다).
        _layer("지시층위", "instruction", "instruction_out", c)
        _layer("묘사층위", "narration", "narration_out", c)

    if action == "PROCEED":
        _layer("추출층위", "extraction", "extraction_out", 0)
        e_lo, e_hi = breakdown["추출층위"]
        _conditional("추출 재시도(조건부)", "_EXTRACTION_RETRY", e_hi)
        # P1: 조건부 부가 호출(비정규 NPC·NPC 프로필·서사 재계획) — 원장의 턴당 발생액(현재 단가로 재계산)
        aux, _label = _ledger_group(session, lambda p, sc: (p["turns"].get(sc) or {}).get("aux"))
        nz = [a for a in (aux or ()) if a > 0]
        if nz:
            _conditional("부가 호출(조건부)", "_AUX", float(_pct(nz, P_HI)))
        elif _aux_possible(session):
            _conditional("부가 호출(조건부)", "_AUX", _cost(int(narr_hi_tokens(session)) + 500, 400, 0))

    # v7.30: 예측 정확도 되먹임(같은 시나리오·모드의 실제/예측 중앙 비율) — 범위 전체에 곱한다
    fb = feedback_factor(session)
    lo_total = sum(v[0] for v in breakdown.values()) * fb
    hi_total = sum(v[1] for v in breakdown.values()) * fb
    mid_total = min(max(sum(mids.values()) * fb, lo_total), hi_total)

    # 표본 수가 가장 적은 층위를 기준으로 신뢰도를 매긴다.
    st = _stats(session)
    ns = [v.get("n", 0) for k, v in st.items() if not k.startswith("_")] or [0]
    n_min = min(ns)
    confidence = "high" if n_min >= CONFIDENCE_HIGH else ("mid" if n_min >= CONFIDENCE_MID else "low")
    basis = "세션 통계"
    if n_min < CONFIDENCE_MID:
        _v, basis = _ledger_group(session, lambda p, sc: ((p["ops"].get(sc) or {}).get("TURN_NARRATION") or {}).get("out"))
        if _v:
            confidence = "mid"                         # 원장 표본(MIN_LEDGER_SAMPLES 이상)이 받친다
        else:
            basis = "기본값"

    return {
        "min_krw": round(lo_total, 2),
        "mid_krw": round(mid_total, 2),
        "max_krw": round(hi_total, 2),
        "min_ink": cost_to_ink(lo_total),
        "max_ink": cost_to_ink(hi_total),
        "breakdown": {k: (round(v[0], 2), round(v[1], 2)) for k, v in breakdown.items()},
        "confidence": confidence,
        "basis": basis,
        "input_tokens": inp,
        "input_ranges": in_ranges,
        "conditional": conditional,
        "feedback": round(fb, 3),
    }


def narr_hi_tokens(session) -> int:
    return _out_range(session, "narration", "narration_out")[1]


def _aux_possible(session) -> bool:
    """비정규 NPC 분석이 돌 수 있는가(이미지·TTS 중 하나라도 켜짐 — gm._resolve_irregular_npcs와 같은 조건)."""
    try:
        from .media_control import is_enabled
        return bool(is_enabled(session, "image") or is_enabled(session, "tts"))
    except Exception:
        return True


def _dynamic(session) -> bool:
    """DYNAMIC 세션인가(gm의 WorldTurn 생성 조건과 같은 판정). 실패는 LEGACY로 본다(기존 예상 유지)."""
    try:
        from . import turn_world
        return turn_world.session_is_dynamic(session)
    except Exception:
        return False


def _simulation_runs(session) -> bool:
    """이번 턴에 개연성 시뮬레이션이 돌 것인가 — gm._call_gm_logic의 do_simulation과 같은 조건."""
    try:
        from .constants import DEFAULT_MODEL as _dm
        from . import turn_history
        return bool(getattr(session, "cache_name", None) and getattr(session, "cache_model", None) == _dm
                    and turn_history.cache_usable(session))
    except Exception:
        return False


def approx_cache_tokens(scenario_data: dict) -> int:
    """업로드 전 캐시 토큰 근사.

    build_scenario_cache_text가 넣는 항목을 같은 구성으로 세야 한다.
    worldview만 세면 영도 기준 21,413 대 실측 26,268로 18% 적게 나와
    예상 17잉크 / 실제 20잉크의 오차가 생긴다.
    """
    sd = scenario_data or {}
    raw = 0
    for key in ("worldview", "story_guide", "stat_system", "desc_guide",
                "status_code_block"):
        raw += len(str(sd.get(key, "")))

    # NPC 사전 — 캐시 텍스트의 가장 큰 비중이다. 다만 info_fields에
    # 지정된 필드만 실리므로 dict 전체를 세면 과대 추정된다.
    tpl = sd.get("npc_template") or {}
    fields = tpl.get("info_fields") or []
    for name, data in (sd.get("default_npcs") or {}).items():
        if not isinstance(data, dict):
            raw += len(str(name)) + len(str(data))
            continue
        raw += len(str(name)) + 4
        for f in fields:
            v = data.get(f)
            if v:
                raw += len(str(f)) + len(str(v)) + 4

    # 영도 실측 기준 79,988자 → 26,268토큰. 한국어 혼합 텍스트라
    # 글자당 0.33토큰이다. 이전에 0.65로 두어 두 배로 잡혔다.
    return int(raw * 0.33) if raw else 0


def estimate_session_open(session, hours: float) -> dict:
    """세션 오픈(캐시 업로드) 및 유지 비용.

    cache_tokens 실측값 기반이라 정확도가 높다.
    """
    from .cost import (cache_create_cost_usd, cache_usd_to_ink,
                       cache_window_estimate_usd)
    from .constants import EXCHANGE_RATE

    tokens = int(getattr(session, "cache_tokens", 0) or 0)
    if tokens <= 0:
        # 아직 업로드 전이면 실측값이 없다. 시간 선택은 업로드보다 먼저
        # 일어나므로 근사해야 하는데, worldview만 세면 실제와 크게 어긋난다.
        # 캐시에는 story_guide·stat_system·desc_guide·NPC 사전도 함께 실린다.
        tokens = approx_cache_tokens(getattr(session, "scenario_data", {}) or {})
    # NOTE: 실제 청구와 같은 함수를 써야 예상과 결과가 어긋나지 않는다.
    #       WP-F: 선불·종료 책임과 같은 canonical 공식(core.cost.cache_window_*)과
    #       같은 잉크 반올림 경계(cache_usd_to_ink)를 쓴다.
    try:
        total_usd = cache_window_estimate_usd(DEFAULT_MODEL, tokens=tokens,
                                              planned_seconds=float(hours) * 3600.0)
        create_krw = cache_create_cost_usd(DEFAULT_MODEL, tokens=tokens) * EXCHANGE_RATE
        total = total_usd * EXCHANGE_RATE
        store_krw = total - create_krw
        total_ink = cache_usd_to_ink(total_usd)
    except Exception:
        create_krw = store_krw = total = 0.0
        total_ink = 0
    return {
        "cache_tokens": tokens,
        "create_krw": round(create_krw, 2),
        "store_krw": round(store_krw, 2),
        "total_krw": round(total, 2),
        "total_ink": total_ink,
        "hours": hours,
    }


def record_actual_input(session, layer: str, fresh_tokens: int) -> dict | None:
    """예측 입력과 실측 입력을 대조해 문자→토큰 계수를 자동 보정한다.

    CHARS_TO_TOKENS는 한국어 근사값이므로 시나리오·프롬프트 구성에 따라
    실제와 어긋난다. 매 턴 오차를 누적해 세션별 보정 계수를 학습한다.

    Args:
        layer: 'instruction' | 'narration' 등
        fresh_tokens: 실제 신선 입력 토큰 (In - Cached)

    Returns:
        {"predicted": int, "actual": int, "error_pct": float, "calib": float}
        예측값이 없으면 None
    """
    pred_map = getattr(session, "_last_input_estimate", None)
    if not isinstance(pred_map, dict):
        return None
    predicted = pred_map.get(layer)
    if not predicted or fresh_tokens <= 0:
        return None

    st = _stats(session)
    key = f"_calib_{layer}"                       # v7.30(D2): 층위별 계수
    entry = st.get(key) or {"n": 0, "ratio": 1.0}
    # 실측/예측 비율의 누적 평균 = 보정 계수(예측값은 이미 이 층위 계수가 곱해진 값 — 원 조립값 기준으로 되돌린다)
    ratio = fresh_tokens / (predicted / get_calibration(session, layer))
    n = entry["n"] + 1
    entry["ratio"] = entry["ratio"] + (ratio - entry["ratio"]) / n
    entry["n"] = n
    st[key] = entry

    err = (predicted - fresh_tokens) / fresh_tokens * 100
    print(f"[EST] {layer} 예측={predicted:,} 실제={fresh_tokens:,} "
          f"오차 {err:+.1f}% | 보정계수 {entry['ratio']:.3f} (n={n})")
    return {"predicted": predicted, "actual": fresh_tokens,
            "error_pct": round(err, 1), "calib": round(entry["ratio"], 3)}


def get_calibration(session, layer: str | None = None) -> float:
    """학습된 보정 계수(층위별, 옛 세션의 공유 계수는 층위 표본이 없을 때만). 표본이 없으면 1.0."""
    st = _stats(session)
    entry = (st.get(f"_calib_{layer}") if layer else None) or st.get("_calib")
    if not entry or entry.get("n", 0) < 2:
        return 1.0
    # 극단값 방어 — 0.5~2.0 범위로 제한
    return max(0.5, min(2.0, float(entry.get("ratio", 1.0))))


# 압축 주기 (턴). 기억 방식 플랜 도입 시 세션별로 달라진다.
COMPRESSION_INTERVAL = 5
# WP-G(D3/AUD-026·027): '압축 선결제'(예상액 20% 가산·누적·정산)는 은퇴했다. 압축 provider
#   비용은 운영자 부담 유지비로 CostEvent 사실만 남으며 플레이어 청구·환급이 없다.
#   estimate_compression/compression_prepay/settle_compression/settle_on_session_close 삭제.


# TTS 문자당 비용(원). 실측으로 보정한다.
TTS_KRW_PER_CHAR = 0.02


def estimate_tts(session) -> dict:
    """TTS 예상 비용. 묘사 분량에 비례한다.

    기획 규정 — 예상액에 합산하지 않고 구분해 표기한다.
    TTS가 꺼져 있으면 0을 반환한다.
    """
    from .media_control import is_enabled

    if not is_enabled(session, "tts"):
        return {"min_ink": 0, "max_ink": 0, "enabled": False}

    # P1: 원장의 턴당 TTS 실측(현재 단가로 재계산)이 있으면 그 분포를 쓴다
    tts, _label = _ledger_group(session, lambda p, sc: (p["turns"].get(sc) or {}).get("tts"))
    if tts:
        return {"min_ink": cost_to_ink(_pct(tts, P_LO)), "max_ink": cost_to_ink(_pct(tts, P_HI)),
                "enabled": True}
    lo, hi = _out_range(session, "narration", "narration_out")
    # 더빙 대상은 **가시 본문**뿐이다 — 사고 토큰은 읽지 않는다(이전 산식은 사고분까지 문자로 환산했다)
    thought = float(((_stats(session).get("narration") or {}).get("thought_mean")) or 0.0)
    lo, hi = max(0, lo - thought), max(0, hi - thought)
    lo_chars = int(lo / CHARS_TO_TOKENS)
    hi_chars = int(hi / CHARS_TO_TOKENS)
    return {
        "min_ink": cost_to_ink(lo_chars * TTS_KRW_PER_CHAR),
        "max_ink": cost_to_ink(hi_chars * TTS_KRW_PER_CHAR),
        "enabled": True,
    }


def format_estimate_detail(est: dict) -> list:
    """v7.30 마스터 전용 — 층위별 입력 범위(확정·유력·가변 근거)·조건부 호출 발생률·되먹임 계수."""
    names = {"judgment": "판단", "instruction": "지시", "narration": "묘사", "extraction": "추출"}
    lines = []
    ir = est.get("input_ranges") or {}
    if ir:
        lines.append("입력(비캐시) " + " · ".join(
            f"{names.get(k, k)} {v['lo']:,}~{v['hi']:,}" + ("" if v.get("basis") == "입력 분류" else "*")
            for k, v in ir.items()) + (" (*조립·원장 점값)" if any(v.get("basis") != "입력 분류" for v in ir.values()) else ""))
    for k, v in (est.get("conditional") or {}).items():
        rate = "미상" if v.get("rate") is None else f"{v['rate'] * 100:.0f}%"
        lines.append(f"{k} 발생률 {rate} → {'상한 포함' if v.get('included') else '제외'} (1회 {v.get('cost', 0):.2f}원)")
    if est.get("feedback", 1.0) != 1.0:
        lines.append(f"정확도 되먹임 ×{est['feedback']}")
    return lines


def format_estimate(est: dict, tts: dict | None = None) -> str:
    """디스플레이 표기용 문자열. TTS는 합산하지 않고 구분 표기한다(기획 규정)."""
    mark = {"low": " (표본 부족)", "mid": "", "high": ""}.get(est.get("confidence", "low"), "")
    line = f"이번 턴 예상: {est['min_ink']}~{est['max_ink']}잉크{mark}"
    if tts:
        line += f"\n        TTS: +{tts.get('min_ink', 0)}~{tts.get('max_ink', 0)}잉크"
    return line
