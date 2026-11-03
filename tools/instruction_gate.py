# -*- coding: utf-8 -*-
"""지시층위 품질 게이트(리팩터링 T-7 · 명세 v2 V1 가) — 실제 provider 표본 비교. 운영 서버에서 실행한다.

같은 턴 요청(골든 대본 4종에서 수집)을 두 방식으로 실제 모델에 보낸다.
  A 현행    : 지시층위 호출 → 그 proceed_instruction으로 묘사
  B 얇은 지시: 지시층위 없음 → 플레이어 선언 + '정본을 따르라' 3줄로 묘사
그리고 판정 모델이 두 묘사를 블라인드로 비교한다(정본 위반·플레이어 주권 위반·진전/정체·문장 품질).

비용: 매 호출의 usage로 원 단위를 누적하고, 다음 턴이 상한을 넘을 것으로 보이면 멈춘다(--cap-krw, 기본 3000).
캐시는 만들지 않는다 — 캐시 본문(룰북 + 지시층위 지시문)을 요청 앞에 그대로 붙인다(운영과 같은 내용, 캐시 읽기 대신 일반 입력 단가).

사용(서버):
  venv/bin/pip install -r requirements-dev.txt                 # 표본 수집에 pytest가 필요
  venv/bin/python tools/instruction_gate.py --cap-krw 3000     # 수집 → 실행 → data/instruction_gate/<시각>/summary.md
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import random
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCENARIOS = ("영도", "설화재", "청람호", "균열")
JUDGE_MODEL = "gemini-3-flash-preview"
THIN = ("[플레이어 선언] {say}\n"
        "· 선언된 시도와 그 직접 결과만 묘사한다. PC의 선언되지 않은 행동·생각·감정을 정하지 않는다.\n"
        "· 장소·시각·장면 인물·사건 결과는 아래 정본 블록을 그대로 따른다.")
JUDGE_SYS = """당신은 한국어 TRPG 묘사의 품질 검수자입니다. 같은 턴에 대한 두 묘사(가·나)를 비교합니다.
기준(각 0~10, 높을수록 좋음):
- canon: 프롬프트의 정본 블록(시각·장소·장면 인물·사건·소지품·상태)과 어긋나는 사실을 지어내지 않았는가
- sovereignty: 플레이어가 선언하지 않은 PC의 행동·대사·생각·감정·결정을 확정하지 않았는가
- progress: 선언에 맞게 장면이 의미 있게 진전하는가(같은 묘사 반복·정체·과잉 사건 없음)
- prose: 문장·몰입·분량의 자연스러움
위반은 issues에 짧게 근거를 적습니다. 둘이 비슷하면 better=TIE. 지정된 JSON만 출력합니다."""
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "object", "properties": {
            "canon": {"type": "integer"}, "sovereignty": {"type": "integer"},
            "progress": {"type": "integer"}, "prose": {"type": "integer"}, "issues": {"type": "string"}},
            "required": ["canon", "sovereignty", "progress", "prose", "issues"]} for k in ("가", "나")},
        "better": {"type": "string", "enum": ["가", "나", "TIE"]},
        "reason": {"type": "string"}},
    "required": ["가", "나", "better", "reason"]}
CRITERIA = ("canon", "sovereignty", "progress", "prose")


class CapReached(Exception):
    pass


class Meter:
    """호출 usage → 원 단위 누적. 다음 턴 예상(지금까지 턴당 최대 × 1.2)이 상한을 넘으면 시작하지 않는다."""

    def __init__(self, cap_krw: float):
        from indaim.app import constants as K
        self.K, self.cap, self.krw, self.per_case = K, float(cap_krw), 0.0, []

    def add(self, model, usage) -> float:
        p = self.K.PRICING_1M.get(model) or self.K.PRICING_1M[self.K.DEFAULT_MODEL]
        n_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        n_out = int(getattr(usage, "candidates_token_count", 0) or 0) + int(getattr(usage, "thoughts_token_count", 0) or 0)
        krw = (n_in * p["INPUT"] + n_out * p["OUTPUT"]) / 1e6 * self.K.EXCHANGE_RATE
        self.krw += krw
        return krw

    def check_next(self, first_guess_krw: float):
        nxt = (max(self.per_case) * 1.2) if self.per_case else first_guess_krw
        if self.krw + nxt > self.cap:
            raise CapReached(f"누적 {self.krw:.0f}원 + 다음 턴 예상 {nxt:.0f}원 > 상한 {self.cap:.0f}원")


def _text_of(contents) -> str:
    if isinstance(contents, str):
        return contents
    return "\n".join(p.get("text", "") for c in contents for p in (c.get("parts") or []) if isinstance(p, dict))


def _swap(contents, old: str, new: str):
    """묘사 요청 안의 가짜 지시문 자리('▶ ' + 지시)를 바꾼다(정확히 한 곳)."""
    blob = json.dumps(contents, ensure_ascii=False)
    key, rep = json.dumps("▶ " + old, ensure_ascii=False)[1:-1], json.dumps("▶ " + new, ensure_ascii=False)[1:-1]
    assert blob.count(key) == 1, "지시문 자리를 찾지 못했습니다"
    return json.loads(blob.replace(key, rep))


def _with_cache(req: dict, rulebook: str, cache_system: str) -> dict:
    """캐시 대신 캐시 본문을 요청 앞에 붙인다 — 시스템 지시도 캐시의 것(요청에 따로 없을 때)."""
    cfg = dict(req["config"])
    cfg.setdefault("system_instruction", cache_system)
    contents = req["contents"] if isinstance(req["contents"], list) else [{"role": "user", "parts": [{"text": req["contents"]}]}]
    return {"model": req["model"], "config": cfg,
            "contents": [{"role": "user", "parts": [{"text": rulebook}]}] + contents}


def _call(client, meter, req, *, retries=1):
    last = None
    for _ in range(retries + 1):
        try:
            resp = client.models.generate_content(model=req["model"], contents=req["contents"], config=req["config"])
            krw = meter.add(req["model"], getattr(resp, "usage_metadata", None))
            return (getattr(resp, "text", None) or ""), krw
        except Exception as e:  # noqa: BLE001 — 표본 하나의 실패는 기록하고 넘어간다
            last = e
            time.sleep(2)
    raise last


def sample(cases: list, per: int) -> list:
    if len(cases) <= per:
        return list(cases)
    step = len(cases) / per
    return [cases[int(i * step)] for i in range(per)]


def run_case(client, meter, bundle, case, rng) -> dict:
    rb, sysi = bundle["rulebook"], bundle["cache_system"]
    logic_text, k1 = _call(client, meter, _with_cache(case["logic"], rb, sysi))
    try:
        instr = (json.loads(logic_text).get("proceed_instruction") or "").strip()
    except Exception:  # noqa: BLE001
        instr = ""
    instr = instr or case["fake_instruction"]
    narr_a, k2 = _call(client, meter, _with_cache({**case["narration"], "contents": _swap(
        case["narration"]["contents"], case["fake_instruction"], instr)}, rb, sysi))
    narr_b, k3 = _call(client, meter, _with_cache({**case["narration"], "contents": _swap(
        case["narration"]["contents"], case["fake_instruction"], THIN.format(say=case["say"]))}, rb, sysi))
    flip = rng.random() < 0.5                               # 블라인드 — 가/나에 A/B를 무작위로
    first, second = (narr_b, narr_a) if flip else (narr_a, narr_b)
    base = _text_of(_swap(case["narration"]["contents"], case["fake_instruction"], "(지시 생략)"))
    judge_req = {"model": JUDGE_MODEL, "config": {
        "system_instruction": JUDGE_SYS, "temperature": 0.0, "response_mime_type": "application/json",
        "response_schema": JUDGE_SCHEMA},
        "contents": [{"role": "user", "parts": [{"text":
                     f"[플레이어 선언]\n{case['say']}\n\n[묘사 요청(정본 블록 포함)]\n{base}\n\n"
                     f"[묘사 가]\n{first}\n\n[묘사 나]\n{second}"}]}]}
    judge_text, k4 = _call(client, meter, judge_req)
    try:
        j = json.loads(judge_text)
        a, b = (j["나"], j["가"]) if flip else (j["가"], j["나"])
        better = {"TIE": "TIE", "가": "B" if flip else "A", "나": "A" if flip else "B"}.get(j.get("better"), "TIE")
        verdict = {"A": a, "B": b, "better": better, "reason": j.get("reason", "")}
    except Exception as e:  # noqa: BLE001
        verdict = {"error": f"판정 파싱 실패: {e}", "raw": judge_text}
    return {"turn": case["turn"], "say": case["say"], "instruction_A": instr, "narration_A": narr_a,
            "narration_B": narr_b, "verdict": verdict, "krw": round(k1 + k2 + k3 + k4, 2)}


def capture(out_dir: pathlib.Path):
    env = {**os.environ, "GATE_CAPTURE_OUT": str(out_dir)}
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/golden/test_gate_capture.py"],
                   cwd=ROOT, env=env, check=True)


def summarize(rows: list, meter, cap) -> str:
    ok = [r for r in rows if "error" not in r["verdict"]]
    lines = [f"# 지시층위 품질 게이트 결과", "",
             f"- 표본 {len(rows)}턴(판정 성공 {len(ok)}) · 비용 {meter.krw:.0f}원 / 상한 {cap:.0f}원", ""]
    if ok:
        lines += ["| 기준 | A 현행 평균 | B 얇은 지시 평균 |", "|---|---|---|"]
        for c in CRITERIA:
            av = sum(r["verdict"]["A"][c] for r in ok) / len(ok)
            bv = sum(r["verdict"]["B"][c] for r in ok) / len(ok)
            lines.append(f"| {c} | {av:.2f} | {bv:.2f} |")
        wins = {k: sum(1 for r in ok if r["verdict"]["better"] == k) for k in ("A", "B", "TIE")}
        lines += ["", f"- 우세: A {wins['A']} · B {wins['B']} · 비슷 {wins['TIE']}", ""]
        by = {}
        for r in ok:
            by.setdefault(r["scenario"], []).append(r)
        lines += ["| 시나리오 | 턴 | A 우세 | B 우세 | 비슷 |", "|---|---|---|---|---|"]
        for s, rs in by.items():
            lines.append(f"| {s} | {len(rs)} | " + " | ".join(
                str(sum(1 for r in rs if r["verdict"]["better"] == k)) for k in ("A", "B", "TIE")) + " |")
    return "\n".join(lines) + "\n"


def main(argv=None, *, client=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cap-krw", type=float, default=3000)
    ap.add_argument("--per-scenario", type=int, default=6)
    ap.add_argument("--cases", default="", help="수집된 표본 폴더(없으면 지금 수집)")
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = pathlib.Path(a.out or ROOT / "data" / "instruction_gate" / stamp)
    out.mkdir(parents=True, exist_ok=True)
    cases_dir = pathlib.Path(a.cases) if a.cases else out / "cases"
    if not cases_dir.is_dir() or not any(cases_dir.glob("*.json")):
        capture(cases_dir)
    if client is None:
        from dotenv import load_dotenv
        from google import genai
        load_dotenv(ROOT / ".env")
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    meter, rng, rows = Meter(a.cap_krw), random.Random(a.seed), []
    stopped = ""
    with open(out / "results.jsonl", "w", encoding="utf-8") as f:
        try:
            for name in SCENARIOS:
                p = cases_dir / f"{name}.json"
                if not p.is_file():
                    continue
                bundle = json.loads(p.read_text(encoding="utf-8"))
                for case in sample(bundle["cases"], a.per_scenario):
                    meter.check_next(first_guess_krw=150.0)
                    row = {"scenario": name, **run_case(client, meter, bundle, case, rng)}
                    meter.per_case.append(row["krw"])
                    rows.append(row)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    print(f"[게이트] {name} 턴{case['turn']} · {row['krw']:.0f}원 · 누적 {meter.krw:.0f}원")
        except CapReached as e:
            stopped = str(e)
            print(f"[게이트] 상한으로 중단 — {e}")
    text = summarize(rows, meter, a.cap_krw) + (f"\n- 중단: {stopped}\n" if stopped else "")
    (out / "summary.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"결과: {out}")
    return rows, meter


if __name__ == "__main__":
    main()
