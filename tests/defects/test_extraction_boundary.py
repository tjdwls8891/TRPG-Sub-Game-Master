"""D-001 — 추출층위 입력 절단 (AUD-011).

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

`_dispatch_proceed`가 묘사 전문이 아니라 앞 500자만 추출층위에 넘긴다.
500자 이후에 나타난 자원·상태·위치 사실은 추출되지 않는다.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import source_of

pytestmark = pytest.mark.defect


def _make_gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


class _FakePart:
    def __init__(self, text): self.text = text


class _FakeContent:
    """raw_logs에 쌓이는 Gemini Content 대역.

    `_dispatch_proceed`가 `content.role`과 `content.parts[0].text`를 읽으므로
    그 형태를 그대로 맞춘다.
    """

    def __init__(self, role, text):
        self.role = role
        self.parts = [_FakePart(text)]


class _StubGameCog:
    """묘사를 반환하는 GameCog 대역."""

    def __init__(self, narration: str):
        self.narration = narration
        self.calls = []

    async def _execute_proceed(self, session, instruction, **kwargs):
        self.calls.append((session, instruction, kwargs))
        session.raw_logs.append(_FakeContent("model", self.narration))
        return {"ok": True}


LATE_FACT = "임성진이 물통을 비우고 배낭에 넣었다."


def _long_narration() -> str:
    """500자 경계 뒤에 사실이 오는 묘사."""
    filler = "바람이 분다. " * 100          # 넉넉히 500자 초과
    assert len(filler) > 500
    return filler + LATE_FACT


async def test_d001_extraction_input_is_truncated_at_500(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """현재 동작 — 추출층위가 받는 텍스트가 500자에서 잘린다.

    이것은 특성화다. 바람직한 동작은 아래 xfail 테스트가 기술한다.
    """
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    narration = _long_narration()

    stub = _StubGameCog(narration)
    wired_bot.add_cog_stub("GameCog", stub)

    captured = {}

    async def _capture(session, text, master_ch=None, *, transaction_id=None):
        captured["text"] = text

    monkeypatch.setattr(cog, "_run_extraction", _capture, raising=False)

    await cog._dispatch_proceed(sess, "지시문")
    # 추출은 create_task로 돈다. 태스크가 실행될 틈을 준다.
    await asyncio.sleep(0)

    assert "text" in captured, "추출층위가 호출되지 않았습니다"
    assert len(captured["text"]) <= 504, (
        f"현재 구현은 500자 + 말줄임으로 잘라야 합니다 (실제 {len(captured['text'])})")
    assert captured["text"].endswith("...")


@pytest.mark.xfail(strict=True,
                   reason="AUD-011 추출층위가 묘사 전문을 받지 못한다")
async def test_d001b_late_fact_reaches_extraction(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """바람직한 동작 — 500자 이후의 사실도 추출 대상이어야 한다."""
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    narration = _long_narration()

    wired_bot.add_cog_stub("GameCog", _StubGameCog(narration))

    captured = {}

    async def _capture(session, text, master_ch=None, *, transaction_id=None):
        captured["text"] = text

    monkeypatch.setattr(cog, "_run_extraction", _capture, raising=False)

    await cog._dispatch_proceed(sess, "지시문")
    await asyncio.sleep(0)

    assert LATE_FACT in captured.get("text", ""), (
        "500자 이후의 자원 변화가 추출층위에 전달되지 않았습니다")


def test_d001c_truncation_constant_is_still_500():
    """절단 상수가 바뀌면 이 결함의 성격도 바뀐다 — 값을 고정한다."""
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dispatch_proceed")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "[:500]" in body, (
        "절단 지점이 사라졌거나 값이 바뀌었습니다 — AUD-011 상태를 재확인하십시오")
