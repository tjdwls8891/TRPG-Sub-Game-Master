"""봇 대역.

WP00_TEST_HARNESS_SPEC.md §2 기준.

핵심 규약 — 등록하지 않은 채널·cog 요청에는 **큰 소리로 실패**한다.
MagicMock을 조용히 반환하면 누락 필드 회귀를 숨긴다.
"""

from __future__ import annotations

from .discord_fakes import FakeChannel, FakeUser
from .genai_fakes import FakeGenAIClient, ScriptedProvider


class UnexpectedChannelRequest(AssertionError):
    """테스트가 등록하지 않은 채널을 요구했다."""


class UnexpectedCogRequest(AssertionError):
    """테스트가 등록하지 않은 cog를 요구했다."""


class FakeBot:
    def __init__(self, *, provider: ScriptedProvider | None = None,
                 strict: bool = True):
        self._channels: dict[int, FakeChannel] = {}
        self._cogs: dict[str, object] = {}
        self.active_sessions: dict[int, object] = {}
        self.session_io_locks: dict[str, object] = {}
        self.genai_client = FakeGenAIClient(provider)
        self.strict = strict
        self.owner_ids: set[int] = set()
        self.presence_calls: list[dict] = []

    # ── 등록 ──
    def add_channel(self, channel_id: int, name: str = "") -> FakeChannel:
        ch = FakeChannel(channel_id, name)
        self._channels[channel_id] = ch
        return ch

    def add_cog_stub(self, name: str, obj) -> object:
        self._cogs[name] = obj
        return obj

    def register_session(self, session) -> None:
        """세션의 채널 3개를 모두 등록한다.

        실제 provision_session이 game/master/display를 모두 등록하므로
        같은 형태를 지킨다.
        """
        for attr in ("game_ch_id", "master_ch_id", "display_ch_id"):
            cid = getattr(session, attr, None)
            if cid:
                self.active_sessions[cid] = session

    # ── discord.Bot 인터페이스 ──
    def get_channel(self, channel_id):
        if channel_id in self._channels:
            return self._channels[channel_id]
        if channel_id in (0, None):
            # 실제 코드가 getattr(session, "x_ch_id", 0)으로 0을 넘기는
            # 경로가 있다. 그때는 None이 정상 반환이다.
            return None
        if self.strict:
            raise UnexpectedChannelRequest(
                f"등록되지 않은 채널 {channel_id} 요청 — "
                f"등록된 채널: {sorted(self._channels)}")
        return None

    def get_cog(self, name):
        if name in self._cogs:
            return self._cogs[name]
        if self.strict:
            raise UnexpectedCogRequest(
                f"등록되지 않은 cog '{name}' 요청 — "
                f"등록된 cog: {sorted(self._cogs)}")
        return None

    async def is_owner(self, user):
        return getattr(user, "id", None) in self.owner_ids

    async def change_presence(self, **kwargs):
        self.presence_calls.append(dict(kwargs))

    async def wait_until_ready(self):
        return None

    def get_user(self, user_id):
        return FakeUser(user_id)

    async def fetch_user(self, user_id):
        return FakeUser(user_id)


class RecordingCog:
    """cog 대역. 호출된 메서드와 인자를 순서대로 기록한다.

    특성화 테스트가 호출 순서를 검증하는 데 쓴다.
    """

    def __init__(self, name: str, recorder: list | None = None):
        self.__cog_name__ = name
        self.calls: list = recorder if recorder is not None else []
        self._handlers: dict[str, object] = {}

    def set_handler(self, method: str, fn) -> None:
        self._handlers[method] = fn

    def __getattr__(self, item):
        if item.startswith("_recording_"):
            raise AttributeError(item)

        async def _call(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            fn = self._handlers.get(item)
            if fn is None:
                return None
            res = fn(*args, **kwargs)
            if hasattr(res, "__await__"):
                return await res
            return res
        return _call
