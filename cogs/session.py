import os
import uuid
import asyncio
import discord
import time
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core

# ========== [세션 관리 모듈(Session Cog)] ==========
class SessionCog(commands.Cog):
    """
    새로운 게임 세션의 생성, 디스코드 채널 세팅, AI 컨텍스트 초기화,
    그리고 게임의 시작 및 소개를 전담하는 모듈.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="새세션")
    async def create_session(self, ctx, scenario_id: str = None):
        """
        서버에 새로운 카테고리와 채널을 생성하고 시나리오 데이터를 캐싱하여 세션 준비.

        NOTE: UUID를 이용해 샌드박스화된 채널 환경을 프로비저닝하고, AI 서버에
        장기 기억 캐시(Context Cache)를 선결제하여 게임 중 발생할 응답 지연(Delay)을 최소화.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            scenario_id (str): 로드할 시나리오 파일 이름
        """
        if not scenario_id:
            scenarios = core.get_available_scenarios()
            await ctx.send(f"⚠️ 시나리오 파일명을 입력해주세요. 예: `!새세션 dark_fantasy`\n(현재 파일: {', '.join(scenarios)})")
            return

        scenario_data = core.load_scenario_from_file(scenario_id)
        if not scenario_data:
            await ctx.send(f"⚠️ 'scenarios/{scenario_id}.json' 파일을 찾을 수 없거나 형식이 잘못되었습니다.")
            return

        guild = ctx.guild
        session_id = str(uuid.uuid4())[:8]
        await ctx.send(f"🔄 '{scenario_id}.json' 데이터를 로드하여 세션({session_id})을 준비합니다...")

        session_dir = f"sessions/{session_id}"
        os.makedirs(session_dir, exist_ok=True)

        category = await guild.create_category(f"TRPG Session {session_id}")
        game_overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False)
        }
        game_ch = await guild.create_text_channel(f"game-{session_id}", category=category, overwrites=game_overwrites)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True)
        }
        master_ch = await guild.create_text_channel(f"master-{session_id}", category=category, overwrites=overwrites)

        session = core.TRPGSession(session_id, game_ch.id, master_ch.id, scenario_id, scenario_data)

        try:
            await ctx.send("⏳ 시나리오 설정 및 장기 기억 캐싱 중...")
            caching_text, cache_tokens, base_text = await core.build_scenario_cache_text(
                self.bot, core.DEFAULT_MODEL, scenario_data
            )

            # NOTE: 유지 비용 선결제를 폐지하고, 캐시 생성 시점에는 순수 업로드(입력) 비용만 정산.
            upload_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
            session.total_cost += upload_cost
            session.cache_created_at = time.time()
            session.cache_tokens = cache_tokens
            session.cache_text = base_text
            core.write_cost_log(session.session_id, "초기 캐시 생성", cache_tokens, 0, 0, upload_cost, session.total_cost)

            report_msg = f"💰 **[캐시 업로드 완료]**\n- 초기 업로드 비용: {core.format_cost(upload_cost)}\n- 누적 비용: {core.format_cost(session.total_cost)}"
            print(report_msg)

            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(report_msg)

            cache = await asyncio.to_thread(
                self.bot.genai_client.caches.create,
                model=core.DEFAULT_MODEL,
                config=types.CreateCachedContentConfig(
                    system_instruction=self.bot.system_instruction,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                    ttl="21600s"
                )
            )
            session.cache_obj = cache
            session.cache_name = cache.name
            await ctx.send(f"✅ 캐싱 완료! (캐시 ID: {cache.name})")
        except Exception as e:
            # WARNING: 캐싱에 실패하더라도 세션 객체 자체는 정상 구동되도록 예외 처리.
            await ctx.send(f"⚠️ 캐싱 실패 (일반 모드로 진행됩니다. 원인: {e})")

        self.bot.active_sessions[game_ch.id] = session
        self.bot.active_sessions[master_ch.id] = session
        await core.save_session_data(self.bot, session)

        await ctx.send(f"🎉 세션 준비 완료!\n플레이어 채널: {game_ch.mention}\n마스터 채널: {master_ch.mention}")


    @commands.command(name="시작")
    @commands.has_permissions(administrator=True)
    async def start_game(self, ctx):
        """
        세션의 시작 메시지를 게임 채널에 출력하고 AI 모델에 컨텍스트 주입. (1회 한정)

        NOTE: 시작 메시지를 시스템이 아닌 AI(role="model")의 발화로 조작하여
        raw_logs에 주입함으로써, AI 스스로 게임 마스터 스탠스를 유지하도록 유도.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session:
            return None

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        # NOTE: 이중 실행 시 AI 프롬프트 오염을 막기 위한 상태 검증 장치.
        if getattr(session, "is_started", False):
            return await ctx.send("⚠️ 이미 시작된 세션입니다. 한 세션에서 `!시작` 명령어는 한 번만 사용할 수 있습니다.")

        session.is_started = True
        await core.save_session_data(self.bot, session)

        start_message = session.scenario_data.get("start_message", "> 세션이 시작됩니다.")
        start_text = f"**[세션 시작]**\n{start_message}"

        await core.stream_text_to_channel(self.bot, game_channel, start_text, words_per_tick=5, tick_interval=1.5)

        session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=start_text)]))
        await core.save_session_data(self.bot, session)

        if ctx.channel.id != session.game_ch_id:
            await ctx.send("✅ 게임 채널에 초기 시작 메시지를 출력하고, 기억 로그에 추가했습니다.")
        return None

    @start_game.error
    async def start_game_error(self, ctx, error):
        """
        start_game 명령어 실행 중 발생하는 권한 에러 처리.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            error (Exception): 발생한 예외 객체
        """
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⚠️ 이 명령어는 서버 관리자 권한을 가진 사용자(GM)만 사용할 수 있습니다.")


    @commands.command(name="소개")
    async def send_intro(self, ctx):
        """
        시나리오 인트로와 캐릭터 생성 안내 메시지를 게임 채널에 자동으로 스트리밍.

        NOTE: 시나리오별로 상이한 플레이어 스탯(pc_template)을 동적으로 추출하여
        안내문을 자동 완성함으로써 온보딩(Onboarding) 프로세스 일관성 유지.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        scenario_intro = session.scenario_data.get("scenario_intro", "")
        pc_template = session.scenario_data.get("pc_template", {})

        template_keys_str = "\n".join([f"- {k}" for k in pc_template.keys()])
        guide_text = f"이제 플레이어 여러분의 캐릭터를 만들 차례입니다. 마스터 채널에 `!참가 [이름]` 명령어를 입력하여 게임 채널에 캐릭터로 참가하십시오.\n\n[플레이어 스탯 구성]\n{template_keys_str}"

        full_text = f"{self.bot.intro_text}\n\n{scenario_intro}\n\n{guide_text}"

        paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]

        await ctx.send("📢 게임 채널에 소개 문단 자동 스트리밍을 시작합니다...")

        for paragraph in paragraphs:
            await core.stream_text_to_channel(self.bot, game_channel, paragraph, words_per_tick=5, tick_interval=1.5)

        await ctx.send("✅ 소개 스트리밍이 완료되었습니다.")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(SessionCog(bot))