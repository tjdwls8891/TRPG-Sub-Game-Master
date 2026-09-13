"""결정적 테스트 페이크. 실제 디스코드·Gemini SDK를 쓰지 않는다."""

from .bot_fakes import FakeBot, RecordingCog, UnexpectedChannelRequest, UnexpectedCogRequest
from .discord_fakes import (
    FakeChannel, FakeInteraction, FakeMessage, FakeNotFound, FakeTyping, FakeUser,
)
from .genai_fakes import (
    FakeGenAIClient, FakeGenAIResponse, FakeUsageMetadata, ScriptedProvider,
)

__all__ = [
    "FakeBot", "RecordingCog", "UnexpectedChannelRequest", "UnexpectedCogRequest",
    "FakeChannel", "FakeInteraction", "FakeMessage", "FakeNotFound", "FakeTyping",
    "FakeUser", "FakeGenAIClient", "FakeGenAIResponse", "FakeUsageMetadata",
    "ScriptedProvider",
]
