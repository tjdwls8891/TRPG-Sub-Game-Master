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
        """WP-C 계약 재현: 확정 묘사를 준비 객체에 싣고 on_finalized 훅을 부른다.

        (정본 raw_logs는 건드리지 않는다 — 로그 적용은 READY 이후.)
        """
        from core.narration_result import NarrationResult
        self.calls.append((session, instruction, kwargs))
        narr = NarrationResult(text=self.narration, narrative_text=self.narration,
                               code_block_text="", paragraphs=())
        prep = kwargs.get("preparation")
        if prep is not None:
            prep.narration = narr
            hook = kwargs.get("on_finalized")
            if hook is not None:
                await hook(narr)
        return {"ok": True, "ai_text": self.narration, "finalized": True}


LATE_FACT = "임성진이 물통을 비우고 배낭에 넣었다."


def _long_narration() -> str:
    """마커(LATE_FACT)를 char 3500 훨씬 뒤(4000자 초과 지점)에 두는 긴 묘사."""
    filler = "바람이 분다. " * 600          # 4000자 초과
    assert len(filler) > 4000
    return filler + LATE_FACT               # 마커 위치 > 3500


async def test_d001_extraction_receives_full_narration(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """WP-B(AUD-011) — 추출층위가 500자 요약이 아니라 완결된 전체 묘사를 받는다.

    (기존 500자 절단 특성화를 수정 동작으로 전환 — §36.)
    """
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    narration = _long_narration()

    stub = _StubGameCog(narration)
    wired_bot.add_cog_stub("GameCog", stub)

    captured = {}

    async def _capture(session, text, master_ch=None, *,
                       transaction_id=None, logical_turn=None, attempt=None,
                       preparation=None):
        captured["text"] = text

    monkeypatch.setattr(cog, "_run_extraction", _capture, raising=False)

    import core
    tx = core.turn_transaction.get_or_begin_turn_transaction(sess, "선언")
    await cog._dispatch_proceed(sess, "지시문", transaction_id=tx.transaction_id)
    # WP-C: 추출은 등록 준비 작업이다 — 배리어처럼 합류한다.
    await tx.preparation.join()

    assert "text" in captured, "추출층위가 호출되지 않았습니다"
    assert captured["text"] == narration, "추출층위가 전체 묘사를 받지 못했습니다"
    assert len(captured["text"]) > 504, "여전히 짧게 잘린 텍스트가 전달됩니다"
    assert not captured["text"].endswith("..."), "요약(말줄임)이 전달되었습니다"


async def test_d001b_late_fact_reaches_extraction(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """WP-B(AUD-011) — 500자 이후의 사실도 추출 대상이 된다(전문 전달)."""
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    narration = _long_narration()

    wired_bot.add_cog_stub("GameCog", _StubGameCog(narration))

    captured = {}

    async def _capture(session, text, master_ch=None, *,
                       transaction_id=None, logical_turn=None, attempt=None,
                       preparation=None):
        captured["text"] = text

    monkeypatch.setattr(cog, "_run_extraction", _capture, raising=False)

    import core
    tx = core.turn_transaction.get_or_begin_turn_transaction(sess, "선언")
    await cog._dispatch_proceed(sess, "지시문", transaction_id=tx.transaction_id)
    await tx.preparation.join()

    assert LATE_FACT in captured.get("text", ""), (
        "500자 이후의 자원 변화가 추출층위에 전달되지 않았습니다")


async def test_d001d_full_narration_reaches_provider_prompt(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """provider 호출 직전 실제 프롬프트에 3500자 이후 마커가 절단 없이 들어간다.

    _run_extraction 내부에 [:3000]/[:1500]/요약-only/꼬리 드롭이 없음을 실증한다.
    """
    from types import SimpleNamespace

    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    narration = _long_narration()
    assert len(narration) > 4000
    assert narration.index(LATE_FACT) > 3500

    captured = {}

    def _fake_generate(*args, **kwargs):
        captured["contents"] = kwargs.get("contents")
        return SimpleNamespace(text='{"situation": {}}', usage_metadata=None)

    monkeypatch.setattr(cog.bot.genai_client.models, "generate_content",
                        _fake_generate, raising=False)

    await cog._run_extraction(sess, narration, master_channel,
                              transaction_id=None, logical_turn=None, attempt=None)

    contents = captured.get("contents")
    assert contents, "추출 provider가 호출되지 않았습니다"
    prompt_text = contents[0].parts[0].text
    assert LATE_FACT in prompt_text, "3500자 이후 마커가 추출 프롬프트에서 잘렸습니다"
    assert narration in prompt_text, "전체 묘사가 추출 프롬프트에 온전히 들어가지 않았습니다(꼬리 드롭)"


def test_d001c_extraction_dispatch_passes_full_narration():
    """구조 특성화 — 추출 발사가 전달과 같은 확정 묘사 전문(narr.text)을 넘긴다.

    ai_summary[:500]은 표시·이력·진행도 요약용으로 남아 있을 수 있으나, 추출 입력은
    확정 묘사 전문이어야 한다(AUD-011 수정 보존, WP-C S1 — 재생성 없음).
    """
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_launch_concurrent_preparation")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    # 추출 입력이 확정 묘사 전문(narr.text)에서 온다.
    assert "_prepare_extraction(session, prep, narr.text" in body, (
        "추출 발사가 확정 묘사 전문을 넘기지 않습니다")
    # 추출 호출이 500자 요약을 넘기지 않는다.
    assert "ai_summary" not in body, (
        "추출 발사가 요약을 참조합니다 — AUD-011 상태 재확인")
