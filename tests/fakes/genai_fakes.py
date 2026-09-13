"""Gemini 제공자 계층 페이크.

WP00_EXECUTABLE_TEST_PLAN.md §4 기준. 실제 SDK를 호출하지 않는다.

`FakeUsageMetadata`는 `core.extract_token_usage()`가 실제로 읽는 네 속성만
노출한다 — prompt_token_count, candidates_token_count,
thoughts_token_count, cached_content_token_count.
"""

from __future__ import annotations

import asyncio
import json


class FakeUsageMetadata:
    """core.extract_token_usage()가 받는 usage_metadata 대역."""

    def __init__(self, prompt=0, candidates=0, thoughts=0, cached=0):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.cached_content_token_count = cached


class FakeGenAIResponse:
    """models.generate_content() 반환 대역."""

    def __init__(self, text="", usage=None, **extra):
        self.text = text
        self.usage_metadata = usage
        # 일부 경로가 candidates/parts를 직접 읽는다. 필요한 테스트만 채운다.
        for k, v in extra.items():
            setattr(self, k, v)

    @classmethod
    def json_payload(cls, payload: dict, *, usage=None, **extra):
        """구조화 응답(response_schema) 경로용."""
        return cls(text=json.dumps(payload, ensure_ascii=False),
                   usage=usage, **extra)


class ProviderCallRecord:
    """SDK 호출 한 번의 기록."""

    def __init__(self, kwargs: dict):
        self.model = kwargs.get("model")
        self.contents = kwargs.get("contents")
        self.config = kwargs.get("config")
        self.kwargs = kwargs

    def __repr__(self):
        return f"<ProviderCall model={self.model}>"


class ScriptedProvider:
    """결정적 결과 큐.

    각 호출이 attempt_count를 올리고 큐의 다음 항목을 반환하거나 던진다.
    예외 인스턴스를 넣으면 raise, 그 밖이면 return.

        ScriptedProvider([
            FakeGenAIResponse("첫 시도"),
            asyncio.TimeoutError(),
            RuntimeError("boom"),
        ])

    큐가 비면 마지막 항목을 반복한다(기본) 또는 RuntimeError를 던진다
    (`exhaust="raise"`).
    """

    def __init__(self, outcomes=None, *, exhaust="repeat_last"):
        self.outcomes = list(outcomes or [])
        self.exhaust = exhaust
        self.attempt_count = 0
        self.calls: list[ProviderCallRecord] = []

    def _next(self):
        if self.outcomes:
            return self.outcomes.pop(0)
        if self.exhaust == "raise":
            raise RuntimeError("ScriptedProvider: 준비된 결과가 없습니다")
        if self.calls and getattr(self, "_last", None) is not None:
            return self._last
        raise RuntimeError("ScriptedProvider: 준비된 결과가 없습니다")

    def generate_content(self, **kwargs):
        """SDK와 같은 동기 시그니처. asyncio.to_thread로 감싸여 불린다."""
        self.attempt_count += 1
        self.calls.append(ProviderCallRecord(kwargs))
        item = self._next()
        self._last = item
        if isinstance(item, BaseException):
            raise item
        return item

    async def agenerate_content(self, **kwargs):
        return self.generate_content(**kwargs)


class FakeModels:
    def __init__(self, provider: ScriptedProvider):
        self._provider = provider

    def generate_content(self, **kwargs):
        return self._provider.generate_content(**kwargs)

    def count_tokens(self, **kwargs):
        class _R:
            total_tokens = 0
        return _R()


class FakeCaches:
    """캐시 SDK 대역. 생성·삭제 호출을 기록한다."""

    def __init__(self):
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.fail_create = False

    def create(self, **kwargs):
        if self.fail_create:
            raise RuntimeError("cache create failed")
        self.created.append(dict(kwargs))

        class _C:
            name = f"caches/fake-{len(self.created)}"
        return _C()

    def delete(self, name=None, **kwargs):
        self.deleted.append(name)

    def get(self, name=None, **kwargs):
        class _C:
            pass
        return _C()


class FakeGenAIClient:
    """bot.genai_client 대역."""

    def __init__(self, provider: ScriptedProvider | None = None):
        self.provider = provider or ScriptedProvider()
        self.models = FakeModels(self.provider)
        self.caches = FakeCaches()

    @property
    def attempt_count(self) -> int:
        return self.provider.attempt_count
