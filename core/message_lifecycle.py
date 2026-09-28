# core.message_lifecycle — WP-F 플레이어 채널 메시지 수명주기 단일 owner
#
# [분류 — MESSAGE_LIFECYCLE_SPEC §2, 새 분류를 만들지 않는다]
#   CANONICAL_DISPLAY      디스플레이 채널의 단일 상태판(core.display.refresh)
#   INTERACTION_PROMPT     확인/입력 UI — CONFIRM|CANCEL|TIMEOUT|SUPERSEDE 중 정확히 한 번 종결
#   TRANSIENT_GAME_STATUS  대기·운영·재시도 안내 — 종결 owner 가 반드시 정리, 로그 미기록
#   CANONICAL_GAME_EVENT   묘사·판정·커밋된 변화 알림 — WP-E 이력/정리 부채가 소유
#   OPERATOR_LOG           마스터 채널 — 플레이어 채널 기본 헬퍼로 흘리지 않는다
#
# [transient 등록부] sessions/{id}/transient_messages.json — 키 → 메시지 핸들 목록.
#   메시지 삭제 성공(또는 이미 없음)만이 항목을 지운다. 재시작 뒤에도 sweep 이 남은
#   항목을 정리한다(내구). 등록부는 봇이 보낸 메시지 ID만 담는다 — 플레이어 메시지는
#   구조적으로 대상이 될 수 없다. 게임 이력 선택(WP-E)과는 별개 도메인이다: 여기서의
#   삭제는 정본 상태를 바꾸지 않고, 정본 게임 출력은 여기 등록되지 않는다.

from __future__ import annotations

import asyncio
import json
import os
import time

import discord

CANONICAL_DISPLAY = "CANONICAL_DISPLAY"
INTERACTION_PROMPT = "INTERACTION_PROMPT"
TRANSIENT_GAME_STATUS = "TRANSIENT_GAME_STATUS"
CANONICAL_GAME_EVENT = "CANONICAL_GAME_EVENT"
OPERATOR_LOG = "OPERATOR_LOG"
MESSAGE_CLASSES = (CANONICAL_DISPLAY, INTERACTION_PROMPT, TRANSIENT_GAME_STATUS,
                   CANONICAL_GAME_EVENT, OPERATOR_LOG)

# 등록부 키 — 같은 키는 한 번에 하나의 안내만(교체 = supersede)
KEY_TURN_NOTICE = "turn_notice"          # 턴 실패/차단 안내 — 다음 턴 시작이 supersede
KEY_SESSION_NOTICE = "session_notice"    # 세션 열림/만료/업로드 진행 — 다음 열기가 supersede
KEY_DISPLAY_OPEN_PROMPT = "display_open_prompt"   # 디스플레이 유지 시간 질문
WAITING_PREFIX = "waiting:"              # 층위 대기 안내(WaitingStatus)

REGISTRY_FILE = "transient_messages.json"


# ══════════════════════════════════════════════════════════════
#  Discord 오류 판정
# ══════════════════════════════════════════════════════════════

def is_gone(exc) -> bool:
    """이미 없는 메시지(NotFound) — 정리 성공으로 취급."""
    try:
        if isinstance(exc, discord.NotFound):
            return True
    except Exception:
        pass
    return "NotFound" in type(exc).__name__


def is_inaccessible(exc) -> bool:
    """메시지가 없거나 접근 불가(NotFound/Forbidden) — 표면 재생성 사유."""
    if is_gone(exc):
        return True
    try:
        if isinstance(exc, discord.Forbidden):
            return True
    except Exception:
        pass
    return "Forbidden" in type(exc).__name__


# ══════════════════════════════════════════════════════════════
#  내구 transient 등록부
# ══════════════════════════════════════════════════════════════

def registry_path(session_id) -> str:
    return os.path.join("sessions", str(session_id), REGISTRY_FILE)


def load_registry(session_id) -> dict:
    path = registry_path(session_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        print(f"[메시지 수명주기] 등록부 손상 — 비움: {e}")
        return {}


def _save_registry(session_id, data: dict) -> bool:
    path = registry_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[메시지 수명주기] 등록부 저장 실패(런타임 정리는 계속): {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def register(session, key: str, message, *, cls: str = TRANSIENT_GAME_STATUS,
             owner=None, ttl: float | None = None) -> None:
    if message is None or getattr(message, "id", None) is None:
        return
    sid = session.session_id
    data = load_registry(sid)
    ch = getattr(message, "channel", None)
    data.setdefault(key, []).append({
        "message_id": message.id, "channel_id": getattr(ch, "id", None), "class": cls,
        "owner": owner, "created_at": time.time(),
        "expires_at": (time.time() + ttl) if ttl else None})
    _save_registry(sid, data)
    runtime = getattr(session, "_transient_handles", None)
    if runtime is None:
        runtime = session._transient_handles = {}
    runtime[message.id] = message


def _unregister(session, key: str, message_ids) -> None:
    sid = session.session_id
    ids = set(message_ids)
    data = load_registry(sid)
    rest = [e for e in data.get(key, []) if e.get("message_id") not in ids]
    if rest:
        data[key] = rest
    else:
        data.pop(key, None)
    _save_registry(sid, data)
    runtime = getattr(session, "_transient_handles", None) or {}
    for mid in ids:
        runtime.pop(mid, None)


async def _delete_entry(bot, session, entry) -> bool:
    mid = entry.get("message_id")
    msg = (getattr(session, "_transient_handles", None) or {}).get(mid)
    try:
        if msg is None:
            ch = bot.get_channel(entry.get("channel_id")) if entry.get("channel_id") else None
            if ch is None:
                return False           # 채널 미준비 — 항목 유지(다음 sweep 재시도)
            msg = await ch.fetch_message(mid)
        await msg.delete()
        return True
    except Exception as e:  # noqa: BLE001
        return is_gone(e)


async def clear(bot, session, key: str) -> int:
    """키의 모든 transient 를 멱등 삭제한다. 실패 항목은 등록부에 남는다."""
    entries = list(load_registry(session.session_id).get(key, []))
    done = []
    for e in entries:
        if await _delete_entry(bot, session, e):
            done.append(e.get("message_id"))
    if done:
        _unregister(session, key, done)
    return len(done)


async def sweep(bot, session, *, only_expired: bool = False, prefix: str | None = None) -> int:
    """등록부 전체(또는 만료분/접두 키)를 정리한다 — 재시작 복구·턴 종료 finally 용."""
    n = 0
    now = time.time()
    for key, entries in list(load_registry(session.session_id).items()):
        if prefix is not None and not key.startswith(prefix):
            continue
        targets = [e for e in entries
                   if not only_expired or (e.get("expires_at") and e["expires_at"] <= now)]
        done = []
        for e in targets:
            if await _delete_entry(bot, session, e):
                done.append(e.get("message_id"))
        if done:
            _unregister(session, key, done)
            n += len(done)
    return n


def pending(session_id) -> dict:
    return load_registry(session_id)


# ══════════════════════════════════════════════════════════════
#  TRANSIENT_GAME_STATUS 송출
# ══════════════════════════════════════════════════════════════

async def send_transient(bot, session, channel, content=None, *, key: str,
                         owner=None, ttl: float | None = None, replace: bool = True,
                         **kwargs):
    """분류·등록된 일시 안내를 보낸다. game_chat/raw_logs 에 기록하지 않는다.

    replace=True 면 같은 키의 이전 안내를 먼저 정리한다(하나만 유지 — supersede).
    ttl 이 있으면 그 시간 뒤 스스로 정리한다(재시작 뒤에도 sweep(only_expired)가 정리).
    """
    if channel is None:
        return None
    if replace:
        await clear(bot, session, key)
    try:
        msg = await channel.send(content, **kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"[메시지 수명주기] transient 전송 실패({key}): {e}")
        return None
    register(session, key, msg, owner=owner, ttl=ttl)
    if ttl:
        async def _expire():
            await asyncio.sleep(ttl)
            try:
                await msg.delete()
                _unregister(session, key, [msg.id])
            except Exception as e:  # noqa: BLE001
                if is_gone(e):
                    _unregister(session, key, [msg.id])
        asyncio.create_task(_expire())
    return msg


def transient_notifier(bot, session, channel, *, key: str = KEY_SESSION_NOTICE,
                       ttl: float | None = None):
    """notify(text) 콜백 형태의 transient 송출기(캐시 업로드 진행·결과 안내 등)."""
    async def _notify(text, **kw):
        return await send_transient(bot, session, channel, text, key=key, ttl=ttl, **kw)
    return _notify


async def send_operator(bot, session, content=None, **kwargs):
    """OPERATOR_LOG — 마스터 채널로만. 채널이 없으면 조용히 생략(플레이어 채널 폴백 금지)."""
    ch = bot.get_channel(getattr(session, "master_ch_id", 0) or 0)
    if ch is None:
        return None
    return await ch.send(content, **kwargs)


# ══════════════════════════════════════════════════════════════
#  INTERACTION_PROMPT — 종결 정확히 한 번
# ══════════════════════════════════════════════════════════════

PROMPT_CONFIRM = "CONFIRM"
PROMPT_CANCEL = "CANCEL"
PROMPT_TIMEOUT = "TIMEOUT"
PROMPT_SUPERSEDE = "SUPERSEDE"


class LifecyclePromptView(discord.ui.View):
    """확인/입력 프롬프트의 공통 수명주기.

    CREATE → CONFIRM | CANCEL | TIMEOUT | SUPERSEDE → 컨트롤 비활성화 → 결과로 접고 삭제.
    콜백은 첫 줄에서 claim()을 호출한다 — 이미 종결됐으면 False(경합 시 한쪽만 효과).
    """

    TIMEOUT_TEXT = "⌛ 응답 시간이 지나 요청을 취소했습니다."
    COLLAPSE_SECONDS = 6

    def __init__(self, *, timeout: float | None):
        super().__init__(timeout=timeout)
        self.message = None
        self.terminal = None

    def bind(self, message):
        self.message = message
        return message

    def claim(self, outcome: str) -> bool:
        if self.terminal is not None:
            return False
        self.terminal = outcome
        for child in self.children:
            child.disabled = True
        return True

    async def on_timeout(self):
        if not self.claim(PROMPT_TIMEOUT):
            return
        await self.on_expired()
        await collapse_message(self.message, self.TIMEOUT_TEXT, seconds=self.COLLAPSE_SECONDS)
        self.stop()

    async def on_expired(self):
        """하위 클래스 훅 — 시간 만료 시 되돌릴 런타임 상태(있다면)."""
        return None

    async def supersede(self, text: str = "다른 요청으로 대체되었습니다."):
        if not self.claim(PROMPT_SUPERSEDE):
            return False
        await collapse_message(self.message, text, seconds=self.COLLAPSE_SECONDS)
        self.stop()
        return True


async def collapse_message(message, text: str, *, seconds: float = 6):
    """프롬프트 메시지를 결과 문구로 접고(컨트롤 제거) 잠시 뒤 지운다. 멱등."""
    if message is None:
        return
    try:
        await message.edit(content=text, view=None, embed=None)
    except Exception as e:  # noqa: BLE001
        if is_gone(e):
            return

    async def _expire():
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except Exception:
            pass
    asyncio.create_task(_expire())


async def bind_interaction_prompt(interaction, view: LifecyclePromptView):
    """interaction.response.send_message 로 보낸 프롬프트의 메시지 핸들을 뷰에 묶는다."""
    try:
        view.bind(await interaction.original_response())
    except Exception as e:  # noqa: BLE001
        print(f"[메시지 수명주기] 프롬프트 핸들 확보 실패(타임아웃 정리 불가): {e}")
