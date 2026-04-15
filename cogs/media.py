import os
import random
import asyncio
import discord
from discord.ext import commands

# 분리된 코어 유틸리티 모듈 임포트
import core


class MediaCog(commands.Cog):
    """
    이미지 출력, BGM 및 플레이리스트 오디오 재생, 채널 채팅 잠금 등
    시각/청각적 미디어와 게임 환경 제어를 전담하는 모듈입니다.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="이미지")
    async def send_media(self, ctx, keyword: str):
        """
        시나리오 파일에 설정된 키워드를 기반으로 게임 채널에 로컬 미디어 이미지를 전송하거나,
        '목록' 인자를 입력하여 사용 가능한 키워드를 확인합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            keyword (str): 출력할 이미지의 키워드 또는 '목록'
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        scenario_data = session.scenario_data
        media_keywords = scenario_data.get("media_keywords", {})
        media_dir = f"media/{session.scenario_id}"

        # '목록' 인자 처리
        if keyword == "목록":
            if not media_keywords:
                return await ctx.send("⚠️ 현재 시나리오에 등록된 이미지 키워드가 없습니다.")

            embed = discord.Embed(title="🖼️ 등록된 이미지 키워드 목록", color=0x3498db)
            keys_str = "\n".join([f"- **{k}** ({v})" for k, v in media_keywords.items()])
            embed.description = keys_str
            return await ctx.send(embed=embed)

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if keyword not in media_keywords:
            available_keys = ", ".join(media_keywords.keys()) if media_keywords else "등록된 키워드 없음"
            return await ctx.send(f"⚠️ '{keyword}'에 매핑된 파일이 없습니다. (사용 가능한 키워드: {available_keys})")

        filename = media_keywords[keyword]
        filepath = os.path.join(media_dir, filename)

        if not os.path.exists(filepath):
            return await ctx.send(f"⚠️ 설정된 경로에 파일이 존재하지 않습니다: `{filepath}`")

        try:
            await game_channel.send(file=discord.File(filepath))
            await ctx.send(f"✅ 게임 채널에 '{keyword}' 이미지를 출력했습니다.")
        except Exception as e:
            await ctx.send(f"⚠️ 이미지 전송 중 오류가 발생했습니다: {e}")


    @commands.command(name="브금")
    async def play_bgm(self, ctx, filename: str):
        """
        음성 채널에 봇을 입장시키고 지정된 오디오 파일의 반복 재생 루프를 시작하거나,
        '목록' 또는 '정지' 인자를 통해 BGM을 제어합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            filename (str): 재생할 파일 이름 (확장자 제외), '목록', 또는 '정지'
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        media_dir = f"media/{session.scenario_id}"

        # '목록' 인자 처리
        if filename == "목록":
            if not os.path.exists(media_dir):
                return await ctx.send(f"⚠️ 미디어 폴더가 존재하지 않습니다: `{media_dir}`")

            bgm_files = [f.replace(".mp3", "") for f in os.listdir(media_dir) if f.endswith(".mp3")]
            if not bgm_files:
                return await ctx.send(f"⚠️ `{media_dir}` 폴더 내에 재생 가능한 mp3 파일이 없습니다.")

            embed = discord.Embed(title="🎵 등록된 BGM 목록", color=0x9b59b6)
            bgm_str = "\n".join([f"- **{f}**" for f in bgm_files])
            embed.description = bgm_str
            return await ctx.send(embed=embed)

        # '정지' 인자 처리
        if filename == "정지":
            vc = session.voice_client
            if vc and vc.is_connected() and vc.is_playing():
                fade_task = getattr(session, "fade_task", None)
                if fade_task and not fade_task.done():
                    fade_task.cancel()

                session.is_bgm_looping = False
                session.current_bgm = None
                session.is_fading = True

                await ctx.send("🔉 볼륨을 서서히 줄이며 BGM을 정지합니다...")

                async def fade_out_and_stop():
                    try:
                        if isinstance(vc.source, discord.PCMVolumeTransformer):
                            for _ in range(20):
                                if not vc.is_playing():
                                    break
                                vc.source.volume = vc.source.volume * 0.8
                                await asyncio.sleep(0.1)
                            vc.source.volume = 0.0
                        vc.stop()
                    except asyncio.CancelledError:
                        pass
                    finally:
                        session.is_fading = False

                session.fade_task = self.bot.loop.create_task(fade_out_and_stop())
            else:
                await ctx.send("⚠️ 현재 재생 중인 BGM이 없거나 음성 채널에 연결되어 있지 않습니다.")
            return

        # 일반 재생 로직 (목록/정지가 아닌 경우)
        if not ctx.author.voice:
            return await ctx.send("⚠️ 마스터님, 먼저 디스코드 음성 채널에 접속해 주십시오.")

        filepath = os.path.join(media_dir, f"{filename}.mp3")

        if not os.path.exists(filepath):
            return await ctx.send(f"⚠️ 설정된 파일이 경로에 없습니다: `{filepath}`")

        voice_channel = ctx.author.voice.channel

        vc = ctx.voice_client
        if not vc:
            vc = await voice_channel.connect()
        elif isinstance(vc, discord.VoiceClient) and vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        session.voice_client = vc

        # noinspection PyShadowingNames
        def after_playing(error):
            if error:
                print(f"⚠️ BGM 재생 오류: {error}")

            if getattr(session, "is_bgm_looping", False) and session.voice_client and session.voice_client.is_connected():
                try:
                    next_filepath = os.path.join(media_dir, f"{session.current_bgm}.mp3")
                    if os.path.exists(next_filepath):
                        source = discord.FFmpegPCMAudio(next_filepath)
                        volume_source = discord.PCMVolumeTransformer(source, volume=1.0)
                        session.voice_client.play(volume_source, after=after_playing)
                except Exception as e:
                    print(f"⚠️ BGM 루프 생성 중 오류: {e}")

        fade_task = getattr(session, "fade_task", None)
        if fade_task and not fade_task.done():
            fade_task.cancel()
            session.is_fading = False

        if vc.is_playing():
            session.is_fading = True
            session.current_bgm = filename
            await ctx.send(f"🔉 볼륨을 서서히 줄인 후 BGM을 **'{filename}'**(으)로 교체합니다...")

            async def fade_out():
                try:
                    if isinstance(vc.source, discord.PCMVolumeTransformer):
                        for _ in range(20):
                            if not vc.is_playing():
                                break
                            vc.source.volume = vc.source.volume * 0.8
                            await asyncio.sleep(0.1)
                        vc.source.volume = 0.0
                    vc.stop()
                except asyncio.CancelledError:
                    pass
                finally:
                    session.is_fading = False

            session.fade_task = self.bot.loop.create_task(fade_out())

        else:
            session.current_bgm = filename
            session.is_bgm_looping = True

            source = discord.FFmpegPCMAudio(filepath)
            volume_source = discord.PCMVolumeTransformer(source, volume=1.0)
            vc.play(volume_source, after=after_playing)
            await ctx.send(f"▶️ BGM **'{filename}'**의 무한 반복 재생을 시작합니다.")


    @commands.command(name="플리")
    async def playlist_control(self, ctx, action: str, scenario_id: str = None):
        """
        지정된 시나리오 미디어 폴더의 mp3 파일들을 셔플하여 무한 루프 플레이리스트 형태로 제어합니다.
        세션 진행과 무관하게 독립적으로 사용할 수 있습니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            action (str): 수행할 행동 (시작/재생/다음/이전/일시정지/종료)
            scenario_id (str, optional): 재생을 시작할 때 지정할 시나리오 폴더명
        """
        guild_id = ctx.guild.id

        if action in ["시작", "재생"] and scenario_id:
            if guild_id in self.bot.playlist_sessions:
                return await ctx.send("⚠️ 이미 플레이리스트가 실행 중입니다. `!플리 종료` 후 다시 시작하거나 `!플리 재생`을 입력해 일시정지를 해제하세요.")

            if not ctx.author.voice:
                return await ctx.send("⚠️ 먼저 디스코드 음성 채널에 접속해 주십시오.")

            media_dir = f"media/{scenario_id}"
            if not os.path.exists(media_dir):
                return await ctx.send(f"⚠️ 해당 시나리오 미디어 폴더를 찾을 수 없습니다: `{media_dir}`")

            queue = [os.path.join(media_dir, f) for f in os.listdir(media_dir) if f.endswith(".mp3")]
            if not queue:
                return await ctx.send(f"⚠️ `{media_dir}` 폴더 내에 재생 가능한 mp3 파일이 없습니다.")

            random.shuffle(queue)

            voice_channel = ctx.author.voice.channel
            vc = ctx.voice_client
            if not vc:
                vc = await voice_channel.connect()
            elif isinstance(vc, discord.VoiceClient) and vc.channel != voice_channel:
                await vc.move_to(voice_channel)

            manager = core.PlaylistManager(self.bot, vc, queue, ctx.channel)
            self.bot.playlist_sessions[guild_id] = manager

            await ctx.send(f"🎵 **{scenario_id}** 미디어 폴더의 mp3 파일 {len(queue)}개를 셔플하여 플레이리스트 재생을 시작합니다.")
            return

        manager = self.bot.playlist_sessions.get(guild_id)

        if not manager:
            return await ctx.send("⚠️ 현재 실행 중인 플레이리스트가 없습니다. `!플리 시작 [시나리오명]`으로 먼저 시작하십시오.")

        if action == "종료":
            manager.task.cancel()
            if manager.vc and manager.vc.is_connected():
                await manager.vc.disconnect()
            del self.bot.playlist_sessions[guild_id]
            await ctx.send("⏹️ 플레이리스트 재생을 완전히 종료하고 음성 채널에서 퇴장합니다.")

        elif action == "다음":
            manager.skip_direction = 1
            if manager.vc.is_playing() or manager.vc.is_paused():
                manager.vc.stop()
            await ctx.send("⏭️ 현재 곡을 건너뛰고 다음 곡을 재생합니다.")

        elif action == "이전":
            manager.skip_direction = -1
            if manager.vc.is_playing() or manager.vc.is_paused():
                manager.vc.stop()
            await ctx.send("⏮️ 현재 곡을 취소하고 이전 곡을 재생합니다.")

        elif action == "일시정지":
            if manager.vc.is_playing():
                manager.vc.pause()
                await ctx.send("⏸️ 플레이리스트 재생을 일시정지했습니다.")
            else:
                await ctx.send("⚠️ 이미 일시정지 상태이거나 현재 재생 중인 곡이 없습니다.")

        elif action == "재생":
            if manager.vc.is_paused():
                manager.vc.resume()
                await ctx.send("▶️ 플레이리스트 재생을 재개합니다.")
            else:
                await ctx.send("⚠️ 일시정지 상태가 아닙니다.")

        else:
            await ctx.send("⚠️ 잘못된 명령어입니다. (사용 가능 인자: 시작/다음/이전/일시정지/재생/종료)")


    @commands.command(name="채팅")
    async def control_chat(self, ctx, state: str):
        """
        게임 채널에서 @everyone 권한 유저의 채팅 발언 가능 여부를 토글합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            state (str): 변경할 상태 키워드 ('잠금', '금지', '오프' 등 혹은 '해제', '허용', '온' 등)
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if state in ["잠금", "금지", "오프", "off"]:
            await game_channel.set_permissions(ctx.guild.default_role, send_messages=False)
            await ctx.send("🔒 게임 채널의 일반 유저 채팅 입력을 **잠금** 처리했습니다.")
            return None

        elif state in ["해제", "허용", "온", "on"]:
            await game_channel.set_permissions(ctx.guild.default_role, send_messages=True)
            await ctx.send("🔓 게임 채널의 일반 유저 채팅 입력을 **해제**했습니다.")
            return None

        else:
            await ctx.send("⚠️ 올바른 상태 인자를 입력해주세요. (사용 예시: `!채팅 잠금` 또는 `!채팅 해제`)")
            return None


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수입니다.
    """
    await bot.add_cog(MediaCog(bot))