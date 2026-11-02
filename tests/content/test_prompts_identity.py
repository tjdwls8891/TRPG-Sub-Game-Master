# -*- coding: utf-8 -*-
"""프롬프트 바이트 동일 — 층위별로 나눈 indaim/content/prompts/의 모든 공개 이름이 나누기 전 값과 같다(리팩터링 T-5).

prompts_digest.json = 나누기 전 indaim/content/prompts.py에서 뜬 지문(상수 = 값의 정규 JSON, 함수 = 정의 원문).
문구를 의도적으로 바꿀 때만(사용자 승인 — 불가침 원칙) 이 지문을 함께 갱신한다.
"""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib

import pytest

import prompts

pytestmark = pytest.mark.policy
HERE = pathlib.Path(__file__).parent
PKG = pathlib.Path(prompts.__file__).parent


def _fn_sources() -> dict:
    out = {}
    for f in sorted(PKG.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for n in ast.parse(src).body:
            if isinstance(n, ast.FunctionDef):
                out[n.name] = ast.get_source_segment(src, n)
    return out


def test_every_prompt_name_is_byte_identical_to_pre_split():
    want = json.loads((HERE / "prompts_digest.json").read_text(encoding="utf-8"))
    fns = _fn_sources()
    got = {}
    for name in prompts.__all__:
        if name in fns:
            got[name] = "src:" + hashlib.sha256(fns[name].encode("utf-8")).hexdigest()
        else:
            got[name] = hashlib.sha256(json.dumps(getattr(prompts, name), ensure_ascii=False,
                                                  sort_keys=True).encode("utf-8")).hexdigest()
    assert got == want


def test_root_alias_is_the_package():
    import indaim.content.prompts as P
    assert prompts is P and sorted(P.__all__) == sorted(json.loads((HERE / "prompts_digest.json").read_text()))
