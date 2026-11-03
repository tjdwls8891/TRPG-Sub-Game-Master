# -*- coding: utf-8 -*-
"""지시층위 품질 게이트 도구(tools/instruction_gate.py) — 가짜 client로 요청 구성·블라인드 판정 환원·비용 상한(네트워크 없음)."""
from __future__ import annotations

import json
import types

import pytest

from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.policy


def _tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("instruction_gate", f"{REPO_ROOT}/tools/instruction_gate.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeClient:
    def __init__(self, tokens=10_000):
        self.reqs, self.tokens = [], tokens
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.reqs.append({"model": model, "contents": contents, "config": config})
        text = json.dumps(contents, ensure_ascii=False)
        if "response_schema" in config and "proceed_instruction" in json.dumps(config["response_schema"]):
            out = json.dumps({"proceed_instruction": "진짜 지시"}, ensure_ascii=False)
        elif config.get("system_instruction", "").startswith("당신은 한국어 TRPG 묘사의 품질 검수자"):
            sc = {"canon": 9, "sovereignty": 9, "progress": 8, "prose": 8, "issues": ""}
            lo = {**sc, "canon": 3}
            first_is_a = text.index("A묘사") < text.index("B묘사")
            out = json.dumps({"가": sc if first_is_a else lo, "나": lo if first_is_a else sc,
                              "better": "가" if first_is_a else "나", "reason": "r"}, ensure_ascii=False)
        else:
            out = "A묘사" if "▶ 진짜 지시" in text else "B묘사"
        usage = types.SimpleNamespace(prompt_token_count=self.tokens, candidates_token_count=500, thoughts_token_count=500)
        return types.SimpleNamespace(text=out, usage_metadata=usage)


def _bundle(tmp_path, n=3):
    narr = [{"role": "user", "parts": [{"text": "[지시사항]\n▶ 선언대로 진행한다 [연속성 지시]\n[현재 작중 시각 — 정본]"}]}]
    logic = [{"role": "user", "parts": [{"text": "[신규 발언] 문을 연다"}]}]
    cases = [{"turn": i, "say": "문을 연다", "fake_instruction": "선언대로 진행한다",
              "logic": {"model": "gemini-3-flash-preview", "contents": logic,
                        "config": {"response_schema": {"properties": {"proceed_instruction": {"type": "string"}}}}},
              "narration": {"model": "gemini-3-flash-preview", "contents": narr, "config": {"temperature": 0.7}}}
             for i in range(n)]
    d = tmp_path / "cases"
    d.mkdir()
    (d / "영도.json").write_text(json.dumps({"scenario": "영도", "rulebook": "룰북본문", "cache_system": "묘사 페르소나",
                                           "cases": cases}, ensure_ascii=False), encoding="utf-8")
    return d


def test_gate_builds_variants_and_unblinds_verdict(tmp_path):
    T = _tool()
    c = FakeClient()
    rows, meter = T.main(["--cases", str(_bundle(tmp_path)), "--out", str(tmp_path / "out"), "--cap-krw", "3000"], client=c)
    assert len(rows) == 3 and all(r["verdict"]["better"] == "A" for r in rows)       # 가/나 뒤집혀도 A로 환원
    assert all(r["verdict"]["A"]["canon"] == 9 and r["verdict"]["B"]["canon"] == 3 for r in rows)
    narr = [q for q in c.reqs if q["config"].get("temperature") == 0.7]
    assert all(q["contents"][0]["parts"][0]["text"] == "룰북본문" for q in narr)        # 캐시 본문 앞붙임
    assert all(q["config"]["system_instruction"] == "묘사 페르소나" for q in narr)
    assert any("[플레이어 선언] 문을 연다" in json.dumps(q["contents"], ensure_ascii=False) for q in narr)
    assert "A 3 · B 0" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


def test_gate_stops_before_exceeding_cap(tmp_path):
    T = _tool()
    c = FakeClient(tokens=400_000)                     # 턴당 약 1,218원 — 다음 턴 예상(×1.2)까지 넣어 상한 2,500원이면 1턴만
    rows, meter = T.main(["--cases", str(_bundle(tmp_path, n=6)), "--out", str(tmp_path / "out"),
                          "--cap-krw", "2500", "--per-scenario", "6"], client=c)
    assert len(rows) == 1 and meter.krw <= 2500
    assert "중단" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
