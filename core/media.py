# 미디어 — 이미지 키워드 전송, PlaylistManager (음성 채널 플레이리스트)
import os
import asyncio

import discord


async def send_image_by_keyword(game_channel, master_ctx, session, keyword):
    """
    시나리오 데이터에 지정된 키워드와 파일 매핑을 참조하여 이미지를 게임 채널에 전송.

    NOTE: 경로 해킹(Path Traversal) 방지를 위해 절대경로 하드코딩 대신
    JSON 매핑 인덱스를 이용한 유효성 검증 수행.

    Args:
        game_channel (discord.TextChannel): 이미지를 전송할 디스코드 게임 채널 객체
        master_ctx (commands.Context): 오류 메시지를 전송할 디스코드 마스터 컨텍스트 객체
        session (TRPGSession): 대상 세션 객체
        keyword (str): 출력할 이미지의 트리거 키워드
    """
    media_keywords = session.scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    if keyword in media_keywords:
        # media_keywords에 명시적으로 등록된 파일명 사용
        filepath = os.path.join(media_dir, media_keywords[keyword])
        if os.path.exists(filepath):
            await game_channel.send(file=discord.File(filepath))
        else:
            await master_ctx.send(f"⚠️ [이미지 경고] 설정된 파일이 경로에 없습니다: `{filepath}`")
    else:
        # media_keywords에 없으면 location_images에서 폴백 조회
        # location_images 값은 설명 문자열이므로 파일명은 '{keyword}.png' 규칙으로 결정
        location_images = session.scenario_data.get("location_images", {})
        if keyword in location_images:
            filepath = os.path.join(media_dir, f"{keyword}.png")
            if os.path.exists(filepath):
                await game_channel.send(file=discord.File(filepath))
            else:
                await master_ctx.send(f"⚠️ [장소 이미지 경고] 파일이 없습니다: `{filepath}`")
        else:
            await master_ctx.send(f"⚠️ [이미지 경고] 등록되지 않은 키워드입니다: `{keyword}`")


class PlaylistManager:
    """
    음성 채널에서의 플레이리스트 셔플 재생 상태 및 백그라운드 루프를 관리하는 클래스.

    NOTE: 메인 TRPG 봇 로직과 스레드를 철저히 분리하여, 플레이리스트 연산이
    주사위 판정이나 AI 텍스트 생성 속도에 영향을 미치지 않도록 설계.

    Args:
        bot: 메인 봇 인스턴스
        vc (discord.VoiceClient): 연결된 음성 채널 클라이언트
        queue (list): 재생할 로컬 mp3 파일 경로들의 리스트
        text_channel (discord.TextChannel): 알림을 보낼 디스코드 텍스트 채널
    """

    def __init__(self, bot, vc, queue, text_channel):
        self.bot = bot
        self.vc = vc
        self.queue = queue
        self.text_channel = text_channel
        self.current_index = 0
        self.volume = 0.3
        self.play_next_event = asyncio.Event()
        self.skip_direction = 1
        self.task = self.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        try:
            # noinspection PyTypeChecker
            while True:
                self.play_next_event.clear()

                if self.current_index >= len(self.queue):
                    self.current_index = 0
                elif self.current_index < 0:
                    self.current_index = len(self.queue) - 1

                filepath = self.queue[self.current_index]

                def after_play(err):
                    if err:
                        print(f"⚠️ 플레이리스트 재생 오류: {err}")
                    self.bot.loop.call_soon_threadsafe(self.play_next_event.set)

                ffmpeg_options = {'options': '-vn -sn -ar 48000 -ac 2'}
                source = discord.FFmpegPCMAudio(filepath, **ffmpeg_options)
                volume_source = discord.PCMVolumeTransformer(source, volume=self.volume)
                self.vc.play(volume_source, after=after_play)

                await self.play_next_event.wait()

                self.current_index += self.skip_direction
                self.skip_direction = 1

        except asyncio.CancelledError:
            pass
        finally:
            if self.vc and self.vc.is_connected():
                if self.vc.is_playing() or self.vc.is_paused():
                    self.vc.stop()
