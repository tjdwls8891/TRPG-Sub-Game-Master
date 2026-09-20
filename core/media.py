# 미디어 — 이미지 키워드 전송, PlaylistManager (음성 채널 플레이리스트)
import os
import asyncio

import discord

from .audio_mixer import ensure_mixer, get_mixer


async def send_image_by_keyword(game_channel, master_ctx, session, keyword, collector=None):
    """
    시나리오 데이터에 지정된 키워드와 파일 매핑을 참조하여 이미지를 게임 채널에 전송.

    NOTE: 경로 해킹(Path Traversal) 방지를 위해 절대경로 하드코딩 대신
    JSON 매핑 인덱스를 이용한 유효성 검증 수행.

    WP-A(출력 소유권): collector가 주어지면 게임 채널에 실제 생성한 이미지 메시지를 전송
        직후 즉시 등록한다(부분 전달 안전). 생성 메시지(또는 없으면 None)를 반환하나, 기존
        caller는 반환값을 무시하므로 하위호환이다. 마스터 채널 경고는 소유 대상이 아니다.

    Args:
        game_channel (discord.TextChannel): 이미지를 전송할 디스코드 게임 채널 객체
        master_ctx (commands.Context): 오류 메시지를 전송할 디스코드 마스터 컨텍스트 객체
        session (TRPGSession): 대상 세션 객체
        keyword (str): 출력할 이미지의 트리거 키워드
        collector (list | None): 생성한 게임 채널 메시지를 즉시 등록할 수집기(선택)
    """
    media_keywords = session.scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    if keyword in media_keywords:
        # media_keywords에 명시적으로 등록된 파일명 사용
        filepath = os.path.join(media_dir, media_keywords[keyword])
        if os.path.exists(filepath):
            _m = await game_channel.send(file=discord.File(filepath))
            if collector is not None:
                collector.append(_m)
            return _m
        else:
            await master_ctx.send(f"⚠️ [이미지 경고] 설정된 파일이 경로에 없습니다: `{filepath}`")
            return None
    else:
        # 장소 이미지 — places 시스템이 흡수했다(지시 확정).
        # 명시된 이미지가 없으면 상위 항목 중 가장 하위의 것을 쓴다.
        from .places import load_places, resolve, image_for

        pl = load_places(session.scenario_data)
        fname = None
        if pl:
            name = resolve(pl, keyword)
            if name:
                fname = image_for(pl, name)

        # 구버전 location_images 폴백 — 데이터 이전 전까지 유지한다.
        if not fname:
            legacy = session.scenario_data.get("location_images", {})
            if keyword in legacy:
                fname = f"{keyword}.png"

        if fname:
            filepath = os.path.join(media_dir, fname)
            if os.path.exists(filepath):
                _m = await game_channel.send(file=discord.File(filepath))
                if collector is not None:
                    collector.append(_m)
                return _m
            else:
                await master_ctx.send(f"⚠️ [장소 이미지 경고] 파일이 없습니다: `{filepath}`")
                return None
        else:
            await master_ctx.send(f"⚠️ [이미지 경고] 등록되지 않은 키워드입니다: `{keyword}`")
            return None


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
        self.paused = False
        # 효과음 오버레이를 위해 플리도 믹서 base를 통해 재생한다.
        # (vc.play를 직접 호출하지 않고 mixer.set_base로 트랙을 공급)
        self.mixer = ensure_mixer(bot, vc)
        self.task = self.bot.loop.create_task(self.player_loop())

    def _advance_signal(self):
        """트랙 종료(또는 스킵) 시 player_loop를 깨우는 thread-safe 시그널."""
        self.bot.loop.call_soon_threadsafe(self.play_next_event.set)

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

                ffmpeg_options = {'options': '-vn -sn -ar 48000 -ac 2'}
                source = discord.FFmpegPCMAudio(filepath, **ffmpeg_options)
                volume_source = discord.PCMVolumeTransformer(source, volume=self.volume)
                # 트랙을 base로 공급. 자연 소진 시 on_exhausted가 다음 곡으로 진행시킨다.
                self.mixer.set_base(volume_source, on_exhausted=self._advance_signal)

                await self.play_next_event.wait()

                self.current_index += self.skip_direction
                self.skip_direction = 1

        except asyncio.CancelledError:
            pass
        finally:
            mixer = get_mixer(self.vc)
            if mixer is not None:
                mixer.clear_base()

    # ── 외부 제어 (cogs/media.py !플리 명령에서 호출) ──────────────
    def skip(self, direction: int):
        """다음(1)/이전(-1) 곡으로 즉시 전환."""
        self.skip_direction = direction
        mixer = get_mixer(self.vc)
        if mixer is not None:
            mixer.clear_base()  # 현재 base 제거(on_exhausted 미발화) 후 수동 진행
        self.paused = False
        self._advance_signal()

    def pause(self) -> bool:
        mixer = get_mixer(self.vc)
        if mixer is None or self.paused or not mixer.has_base():
            return False
        mixer.pause_base()
        self.paused = True
        return True

    def resume(self) -> bool:
        mixer = get_mixer(self.vc)
        if mixer is None or not self.paused:
            return False
        mixer.resume_base()
        self.paused = False
        return True
