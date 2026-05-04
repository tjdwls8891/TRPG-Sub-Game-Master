import os
import re
import json
import random
import asyncio
import discord
from google.genai import types
from discord.ext import commands

# 코어 유틸리티 모듈 임포트
import core

# ========== [미디어 및 환경 제어 모듈(Media Cog)] ==========
class MediaCog(commands.Cog):
    """
    이미지 출력, BGM 및 플레이리스트 오디오 재생, 채널 채팅 잠금 등
    시각/청각적 미디어와 게임 환경 제어를 전담하는 모듈.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="이미지")
    async def send_media(self, ctx, action_or_keyword: str, format_key: str = None, filename_key: str = None, *,
                         prompt: str = None):
        """
        [출력 모드]: !이미지 [키워드]
        - 시나리오 파일에 설정된 키워드를 기반으로 게임 채널에 이미지를 전송합니다.

        [생성 모드]: !이미지 생성 [형식키] [파일명(키워드)] [프롬프트] (레:레퍼런스키)
        - AI를 통해 이미지를 생성하고 로컬 폴더 저장 및 룰북 매핑을 자동화합니다.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        scenario_data = session.scenario_data

        if "media_keywords" not in scenario_data:
            scenario_data["media_keywords"] = {}
        media_keywords = scenario_data["media_keywords"]
        media_dir = f"media/{session.scenario_id}"

        # 1. 목록 출력 모드
        if action_or_keyword == "목록":
            if not media_keywords:
                return await ctx.send("⚠️ 현재 시나리오에 등록된 이미지 키워드가 없습니다.")

            embed = discord.Embed(title="🖼️ 등록된 이미지 키워드 목록", color=0x3498db)
            keys_str = "\n".join([f"- **{k}** ({v})" for k, v in media_keywords.items()])
            embed.description = keys_str
            return await ctx.send(embed=embed)

        # 2. 이미지 생성 및 매핑 모드
        if action_or_keyword == "생성":
            if not format_key or not filename_key or not prompt:
                return await ctx.send("⚠️ 사용법: `!이미지 생성 [형식키] [키워드(파일명)] [프롬프트] (레:레퍼런스키)`")

            image_prompts = scenario_data.get("image_prompts", {})
            if format_key not in image_prompts:
                return await ctx.send(f"⚠️ '{format_key}' 형식 프롬프트를 시나리오 파일에서 찾을 수 없습니다.")

            base_prompt = image_prompts[format_key].get("prompt", "")
            target_ratio = image_prompts[format_key].get("aspect_ratio", "1:1")

            # 레퍼런스 이미지 파싱
            ref_match = re.search(r'레:(\S+)', prompt)
            ref_image = None
            if ref_match:
                ref_keyword = ref_match.group(1)
                if ref_keyword in media_keywords:
                    ref_path = os.path.join(media_dir, media_keywords[ref_keyword])
                    if os.path.exists(ref_path):
                        try:
                            import PIL.Image
                            img = PIL.Image.open(ref_path)
                            # 추가: 레퍼런스용 이미지는 연산 부하 방지를 위해 사이즈를 절반(또는 512x512)으로 축소
                            img.thumbnail((512, 512))
                            ref_image = img
                        except Exception as e:
                            print(f"⚠️ 레퍼런스 로드 실패: {e}")
                    else:
                        return await ctx.send(f"⚠️ 레퍼런스 이미지 파일을 찾을 수 없습니다: {ref_keyword}")
                prompt = re.sub(r'레:\S+', '', prompt).strip()

            # 비율 지시를 프롬프트 최상단에 명시적으로 추가
            ratio_instruction = f"[System: The target aspect ratio for this image is {target_ratio}.] "
            final_prompt = f"{ratio_instruction}{base_prompt}\n\n[세부 지시사항]: {prompt}"

            # API 전송용 컨텐츠 리스트 구성
            contents_payload = [final_prompt]
            if ref_image:
                contents_payload.append(ref_image)

            await ctx.send(
                f"⏳ '{filename_key}' 이미지 생성을 시작합니다...\n- 적용된 형식: {format_key} (비율: {target_ratio})\n- 레퍼런스: {'사용됨' if ref_image else '없음'}")

            try:
                print(f"[DEBUG] API 호출 시작: {filename_key}")

                async with ctx.typing():
                    response = await asyncio.to_thread(
                        self.bot.genai_client.models.generate_content,
                        model=core.IMAGE_MODEL,
                        contents=contents_payload
                    )

                print(f"[DEBUG] API 응답 완료: {filename_key}")

                # 파일 저장 로직 (PNG 강제)
                os.makedirs(media_dir, exist_ok=True)
                filename = f"{filename_key}.png"
                filepath = os.path.join(media_dir, filename)

                image_saved = False

                print(f"[DEBUG] 이미지 파싱 및 저장 시작: {filename_key}")
                # 공식 예시에 따른 응답 파트 순회 및 이미지 추출
                for part in response.parts:
                    if part.inline_data is not None:
                        generated_image = part.as_image()
                        generated_image.save(filepath)
                        image_saved = True
                        break

                if not image_saved:
                    print(f"[DEBUG] 이미지 데이터 누락. 응답 텍스트: {response.text if hasattr(response, 'text') else '없음'}")
                    return await ctx.send("⚠️ API 호출은 성공했으나, 응답에서 이미지 데이터를 찾을 수 없습니다. (정책 위반으로 인한 필터링 가능성)")

                print(f"[DEBUG] 로컬 저장 완료: {filepath}")

                # 메모리 매핑 및 JSON 파일 덮어쓰기 (영구 저장)
                media_keywords[filename_key] = filename

                def save_scenario():
                    scenario_path = f"scenarios/{session.scenario_id}.json"
                    with open(scenario_path, "w", encoding="utf-8") as file:
                        json.dump(scenario_data, file, ensure_ascii=False, indent=4)

                await asyncio.to_thread(save_scenario)

                # 비용 계산: 응답의 usage_metadata에서 실제 토큰 수를 추출하여 항목별 정산.
                # NOTE: 이미지 출력 토큰 단가($60/1M)는 텍스트 출력($3/1M)과 별개이므로 modality 분리 시도.
                prompt_tokens = 0
                image_tokens = 0
                text_tokens = 0
                usage_source = "usage_metadata"
                try:
                    usage = getattr(response, "usage_metadata", None)
                    if usage:
                        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
                        candidates_total = getattr(usage, "candidates_token_count", 0) or 0

                        # NOTE: SDK 버전에 따라 candidates_tokens_details 또는 output_tokens_details에 modality별 분해 제공.
                        details = (getattr(usage, "candidates_tokens_details", None)
                                   or getattr(usage, "output_tokens_details", None))
                        if details:
                            for d in details:
                                modality = str(getattr(d, "modality", "")).upper()
                                count = getattr(d, "token_count", 0) or 0
                                if "IMAGE" in modality:
                                    image_tokens += count
                                else:
                                    text_tokens += count
                            # 분해 합산이 0이면 폴백
                            if image_tokens + text_tokens == 0:
                                image_tokens = candidates_total
                        else:
                            # NOTE: 분리 정보가 없으면 전체 출력을 이미지 토큰으로 간주(이미지 모델 응답의 일반 케이스).
                            image_tokens = candidates_total
                    else:
                        usage_source = "fallback(1K)"
                        image_tokens = core.IMAGE_OUTPUT_TOKENS_BY_RES.get("1K", 1120)
                except Exception as parse_err:
                    usage_source = f"fallback(parse_err: {type(parse_err).__name__})"
                    print(f"[WARN] usage_metadata 파싱 실패: {parse_err}")
                    image_tokens = core.IMAGE_OUTPUT_TOKENS_BY_RES.get("1K", 1120)

                cost_breakdown = core.calculate_image_gen_cost(
                    core.IMAGE_MODEL,
                    prompt_tokens=prompt_tokens,
                    image_output_tokens=image_tokens,
                    text_output_tokens=text_tokens,
                )
                turn_cost = cost_breakdown["total_krw"]
                session.total_cost += turn_cost
                core.write_cost_log(
                    session.session_id, f"이미지 생성 ({filename_key})",
                    prompt_tokens, 0, image_tokens + text_tokens, turn_cost, session.total_cost
                )

                await core.save_session_data(self.bot, session)

                print(f"\n[이미지 생성 비용] {filename_key} {core.format_cost(turn_cost)}")
                _ref_label = "사용" if ref_image else "미사용"
                _gen_embed = core.build_image_gen_cost_embed(
                    label=f"이미지 생성 — {format_key}",
                    model_id=core.IMAGE_MODEL,
                    cost_breakdown=cost_breakdown,
                    turn_cost=turn_cost,
                    total_cost=session.total_cost,
                    extra_fields=[
                        ("형식", f"{format_key}  (비율 {target_ratio})", True),
                        ("레퍼런스 이미지", _ref_label, True),
                        ("출처", usage_source, False),
                    ],
                )

                # 마스터 채널에 결과물과 비용 임베드 전송
                await ctx.send(
                    content=f"✅ 이미지 생성 및 로컬 에셋 매핑 완료: `{filename_key}`",
                    file=discord.File(filepath),
                )
                await ctx.send(embed=_gen_embed)

            except Exception as e:
                # 에러의 정확한 타입(Class명)까지 출력
                await ctx.send(f"⚠️ 이미지 생성 중 API 또는 I/O 오류가 발생했습니다: {type(e).__name__} - {e}")

            return

        # 3. 기존 이미지 출력 모드 (!이미지 [키워드])
        keyword = action_or_keyword
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
        '목록' 또는 '정지' 인자를 통해 BGM 제어.

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

                # [오디오 페이드아웃(Fade-out) 처리 로직]
                # 몰입을 깨는 급격한 오디오 단절을 막기 위해 비동기 sleep을 활용하여 볼륨을 0으로 서서히 줄인 후 재생 종료.
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

            # NOTE: 무한 루프 구현 시 while문을 쓰면 메인 스레드가 블로킹되므로,
            # 재생이 끝난 직후 트리거되는 after 콜백 내에서 동일한 파일을 재귀적으로 호출.
            if getattr(session, "is_bgm_looping", False) and session.voice_client and session.voice_client.is_connected():
                try:
                    next_filepath = os.path.join(media_dir, f"{session.current_bgm}.mp3")
                    if os.path.exists(next_filepath):
                        ffmpeg_options = {'options': '-vn -sn -ar 48000 -ac 2'}
                        source = discord.FFmpegPCMAudio(next_filepath, **ffmpeg_options)
                        volume_source = discord.PCMVolumeTransformer(source, volume=getattr(session, "volume", 0.3))
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

            ffmpeg_options = {'options': '-vn -sn -ar 48000 -ac 2'}
            source = discord.FFmpegPCMAudio(filepath, **ffmpeg_options)
            volume_source = discord.PCMVolumeTransformer(source, volume=getattr(session, "volume", 0.3))
            vc.play(volume_source, after=after_playing)
            await ctx.send(f"▶️ BGM **'{filename}'**의 무한 반복 재생을 시작합니다.")


    @commands.command(name="플리")
    async def playlist_control(self, ctx, action: str, scenario_id: str = None):
        """
        지정된 시나리오 미디어 폴더의 mp3 파일들을 셔플하여 무한 루프 플레이리스트 형태로 제어.
        세션 진행과 무관하게 독립적으로 사용 가능.

        NOTE: 단일 트랙 무한 반복인 BGM 기능과 달리 여러 트랙의 백그라운드 재생을 지원.
        게임 메인 루프와의 간섭을 막기 위해 core.PlaylistManager라는 독립된 백그라운드 태스크(Task) 할당.

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


    @commands.command(name="볼륨")
    async def set_volume(self, ctx, volume: float):
        """
        현재 재생 중인 BGM 또는 플레이리스트의 볼륨을 실시간으로 조절.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            volume (float): 설정할 볼륨 값 (0.0 ~ 2.0 사이, 0.3이 30%)
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not (0.0 <= volume <= 2.0):
            return await ctx.send("⚠️ 볼륨은 0.0(음소거)에서 2.0(200%) 사이의 숫자로 입력해 주십시오.")

        # 1. 세션 BGM 볼륨 갱신 및 파일 저장
        session.volume = volume
        await core.save_session_data(self.bot, session)

        # 2. 현재 재생 중인 세션 BGM에 즉시 반영 (페이드아웃 중첩 방지)
        if session.voice_client and session.voice_client.is_playing() and isinstance(session.voice_client.source,
                                                                                     discord.PCMVolumeTransformer):
            # WARNING: 페이드아웃(is_fading) 진행 중에 볼륨을 강제 수정하면 소리가 다시 커지며 연출이 깨질 수 있으므로 상태 플래그 검사 필수.
            if not getattr(session, "is_fading", False):
                session.voice_client.source.volume = volume

        # 3. 독립 구동 중인 플레이리스트가 있다면 동시 적용
        manager = self.bot.playlist_sessions.get(ctx.guild.id)
        if manager:
            manager.volume = volume
            if manager.vc and manager.vc.is_playing() and isinstance(manager.vc.source, discord.PCMVolumeTransformer):
                manager.vc.source.volume = volume

        await ctx.send(f"🔊 사운드 볼륨이 **{volume * 100:.0f}%** 로 조정되었습니다.")


    @commands.command(name="채팅")
    async def control_chat(self, ctx, state: str):
        """
        게임 채널에서 @everyone 권한 유저의 채팅 발언 가능 여부를 토글.

        NOTE: AI의 장문 텍스트 스트리밍 중 플레이어가 채팅을 입력하여 출력 화면이
        섞이는 것을 막기 위한 하드웨어적 채널 권한(Permissions) 통제 장치.

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
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(MediaCog(bot))