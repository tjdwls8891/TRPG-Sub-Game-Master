"""디스코드 전송 계층 페이크.

WP00_EXECUTABLE_TEST_PLAN.md §3 기준. 일반 디스코드 에뮬레이터를 만들지
않는다 — 선택된 테스트가 실제로 쓰는 필드만 구현한다.

설계 원칙
    · 예상하지 못한 요청에는 큰 소리로 실패한다(MagicMock 반환 금지).
    · 모든 변이를 기록해 호출 순서·삭제 여부를 검증할 수 있게 한다.
"""

from __future__ import annotations

import itertools

_ID_SEQ = itertools.count(9_000_000_000_000_000_001)


class FakeNotFound(Exception):
    """discord.NotFound 대역. 두 번째 delete()가 던질 수 있다."""


class FakeMessage:
    """전송된 메시지 하나."""

    def __init__(self, channel=None, content=None, **kwargs):
        self.id = next(_ID_SEQ)
        self.channel = channel
        self.content = content
        self.embeds = list(kwargs.get("embeds") or
                           ([kwargs["embed"]] if kwargs.get("embed") else []))
        self.view = kwargs.get("view")
        self.files = kwargs.get("file") or kwargs.get("files")
        self.delete_after = kwargs.get("delete_after")
        self.deleted = False
        self.delete_count = 0
        self.edit_history: list[dict] = []
        # 두 번째 delete()에서 예외를 던질지. 테스트 모드별로 다르다.
        self.raise_on_second_delete = False

    async def edit(self, **kwargs):
        self.edit_history.append(dict(kwargs))
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] else []
        if "embeds" in kwargs:
            self.embeds = list(kwargs["embeds"] or [])
        if "view" in kwargs:
            self.view = kwargs["view"]
        return self

    async def delete(self):
        self.delete_count += 1
        if self.deleted and self.raise_on_second_delete:
            raise FakeNotFound("message already deleted")
        self.deleted = True


class FakeTyping:
    """async with channel.typing() 대역."""

    def __init__(self, channel):
        self.channel = channel
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        self.channel.typing_entries.append(self)
        return self

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


class FakeChannel:
    """게임·마스터·디스플레이 채널 대역."""

    def __init__(self, channel_id: int, name: str = ""):
        self.id = channel_id
        self.name = name or f"fake-{channel_id}"
        self.sent: list[FakeMessage] = []
        self.typing_entries: list[FakeTyping] = []
        self.permission_calls: list[tuple] = []

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(channel=self, content=content, **kwargs)
        self.sent.append(msg)
        return msg

    def typing(self):
        return FakeTyping(self)

    async def fetch_message(self, message_id: int):
        """id로 기존 메시지를 찾는다.

        display.refresh가 단일 표면을 유지하는 방식이다. 삭제된 메시지는
        실제 API처럼 예외를 던져야 새로 만드는 폴백이 동작한다.
        """
        for m in self.sent:
            if m.id == message_id and not m.deleted:
                return m
        raise FakeNotFound(f"message {message_id} not found")

    async def set_permissions(self, target, **kwargs):
        self.permission_calls.append((target, dict(kwargs)))

    # 텍스트만 뽑아 보기 위한 편의 속성 (테스트 가독성용)
    @property
    def texts(self) -> list[str]:
        return [m.content for m in self.sent if m.content is not None]

    def __repr__(self):
        return f"<FakeChannel {self.name} id={self.id} sent={len(self.sent)}>"


class FakeResponse:
    """interaction.response 대역."""

    def __init__(self):
        self.deferred = False
        self.messages: list[dict] = []
        self.edits: list[dict] = []

    def is_done(self) -> bool:
        return self.deferred or bool(self.messages)

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.edits.append(dict(kwargs))


class FakeFollowup:
    def __init__(self, channel=None):
        self.channel = channel
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        msg = FakeMessage(channel=self.channel, content=content, **kwargs)
        if self.channel is not None:
            self.channel.sent.append(msg)
        return msg


class FakeUser:
    def __init__(self, user_id: int, name: str = "tester"):
        self.id = user_id
        self.name = name
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.dms: list[str] = []

    async def send(self, content=None, **kwargs):
        self.dms.append(content)


class FakeInteraction:
    """뷰 콜백이 받는 interaction 대역.

    선택된 테스트가 쓰는 필드만 둔다.
    """

    def __init__(self, user=None, channel=None, message=None):
        self.user = user or FakeUser(1)
        self.channel = channel
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeFollowup(channel)

    async def original_response(self):
        return self.message
