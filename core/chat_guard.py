# 채팅 차단 — 채널별 권한 검증을 봇 레벨 단일 훅으로 처리한다
#
# [단일 훅으로 올린 이유]
#   권한 검증이 cog마다 흩어져 있으면 새 채널 유형이 추가될 때 누락이 생긴다.
#   기획 규정도 "채팅 이벤트 발생 시 최우선 단계로 채널과 아이디를 받아
#   필요한 모든 경우에 권한검증 실시"이므로 진입점을 하나로 둔다.
#
# [이중 차단]
#   1차는 서버 권한(채널 오버라이드)으로 막고, 2차로 새어 들어온 메시지를
#   삭제한다. 권한으로 막는 것이 원칙이고 삭제는 보완이다.
import asyncio

import discord

# 차단 안내를 띄우는 시간(초). 기획 규정 3초.
NOTICE_SECONDS = 3


async def _reject(message: discord.Message, reason: str) -> bool:
    """메시지를 삭제하고 잠깐 안내를 띄운다. 항상 False를 반환한다."""
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await message.channel.send(reason, delete_after=NOTICE_SECONDS)
    except Exception:
        pass
    return False


async def chat_guard(bot, message: discord.Message) -> bool:
    """이 메시지를 처리해도 되는지 판정한다.

    Returns:
        True면 통과(명령어 처리 등 진행), False면 차단됨.
    """
    if message.author.bot:
        return False
    if message.guild is None:
        return True  # DM은 별도 인터페이스 소관

    session = bot.active_sessions.get(message.channel.id)

    # ── 세션 채널이 아닌 경우 ──
    if session is None:
        # GM 스페이스는 채팅 불가 (설계문서 6). 아직 채널 생성 전이면 통과.
        gm_space_id = getattr(bot, "gm_space_ch_id", None)
        if gm_space_id and message.channel.id == gm_space_id:
            return await _reject(message, "ℹ️ 이 채널에서는 채팅할 수 없습니다. 버튼을 이용해 주십시오.")
        return True

    is_owner = await bot.is_owner(message.author)
    uid = str(message.author.id)
    is_player = uid in (getattr(session, "players", {}) or {})

    # ── 디스플레이 채널 ──
    # 답변이 필요한 순간에만 1회 허용한다(세션 생성 플로우의 시간 입력 등).
    if message.channel.id == getattr(session, "display_ch_id", None):
        if getattr(session, "awaiting_display_input", False):
            session.awaiting_display_input = False
            return True
        return await _reject(message, "ℹ️ 디스플레이 채널은 버튼으로만 조작합니다.")

    # ── 마스터 채널 ── 권한자인 참가자만
    if message.channel.id == getattr(session, "master_ch_id", None):
        if is_owner or is_player:
            return True
        return await _reject(message, "ℹ️ 이 채널은 진행 권한자만 사용할 수 있습니다.")

    # ── 게임 채널 ── 턴 진행 중이 아닐 때, 참가자만
    if message.channel.id == getattr(session, "game_ch_id", None):
        if not (is_owner or is_player):
            return await _reject(message, "ℹ️ 세션 참가자만 발언할 수 있습니다.")
        if getattr(session, "is_processing", False):
            return await _reject(message, "⏳ 턴 진행 중에는 입력할 수 없습니다.")
        if getattr(session, "extraction_pending", False):
            return await _reject(
                message, "⏸️ 이전 턴 정보 정리가 완료되지 않았습니다. 재시도 버튼을 눌러 주십시오.")
        return True

    return True
