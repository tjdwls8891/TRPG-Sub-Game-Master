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
class StartFrameView(discord.ui.View):
    """
    시작 상황 삼지선다.

    기획 규정 — 프로필로 필터링한 틀 중 랜덤 삼지선다를 제시하고,
    참가자 선택으로 채널을 정리한 뒤 인트로를 재생한다.
    """

    def __init__(self, bot, session, options: list):
        super().__init__(timeout=900)
        self.bot = bot
        self.session = session
        self.options = options
        for i, opt in enumerate(options, 1):
            self.add_item(StartFrameButton(i, opt))


class StartFrameButton(discord.ui.Button):
    def __init__(self, index: int, option: dict):
        super().__init__(label=f"{index}. {option.get('title', '')}"[:80],
                         style=discord.ButtonStyle.primary)
        self.option = option

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        session = view.session
        await interaction.response.defer()

        # 사전 확정 정보를 세계 상태에 반영한다(기획 규정).
        core.start_frame.apply_facts(session, self.option)
        session.start_frame = self.option

        for c in view.children:
            c.disabled = True
        try:
            await interaction.message.edit(view=view)
        except Exception:
            pass

        cog = view.bot.get_cog("SessionCog") or view.bot.get_cog("SessionsCog")
        if cog:
            await cog.play_intro(session, self.option, interaction.channel)
        view.stop()


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
        # 계정 등록·약관 동의 확인 (기획 규정 — 세션 생성 시점에 버전 비교)
        # 미동의·재동의 필요 시 DM으로 절차를 시작하고 생성을 중단한다.
        ok, notice = await core.ensure_agreed(self.bot, ctx.author)
        if not ok:
            await ctx.send(notice)
            return

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

        # 디스플레이 채널 — 상태 표기와 UI 전용. 채팅은 봇만 가능하게 막는다.
        # 기획 규정상 세션은 음성·게임·디스플레이 3채널로 구성된다.
        display_overwrites = {
            guild.default_role: discord.PermissionOverwrite(send_messages=False),
            guild.me: discord.PermissionOverwrite(send_messages=True),
        }
        display_ch = await guild.create_text_channel(
            f"display-{session_id}", category=category, overwrites=display_overwrites)

        session = core.TRPGSession(session_id, game_ch.id, master_ch.id, scenario_id, scenario_data)
        session.display_ch_id = display_ch.id

        try:
            await ctx.send("⏳ 시나리오 설정 및 장기 기억 캐싱 중...")
            caching_text, cache_tokens, base_text = await core.build_scenario_cache_text(
                self.bot, core.DEFAULT_MODEL, scenario_data, session=session
            )

            # NOTE: 유지 비용 선결제를 폐지하고, 캐시 생성 시점에는 순수 업로드(입력) 비용만 정산.
            upload_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
            session.total_cost += upload_cost
            session.cache_created_at = time.time()
            session.cache_tokens = cache_tokens
            session.cache_text = base_text
            core.write_cost_log(session.session_id, "초기 캐시 생성", cache_tokens, 0, 0, upload_cost, session.total_cost)

            print(f"[새 세션 캐시 업로드] upload={core.format_cost(upload_cost)} total={core.format_cost(session.total_cost)}")
            _cache_embed = core.build_cache_cost_embed(
                "새 세션 캐시 생성", 0.0, upload_cost, session.total_cost
            )
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(embed=_cache_embed)

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
            session.cache_model = core.DEFAULT_MODEL
            core.update_session_cache_state(session)
            await ctx.send(f"✅ 캐싱 완료! (캐시 ID: {cache.name})")
        except Exception as e:
            # WARNING: 캐싱에 실패하더라도 세션 객체 자체는 정상 구동되도록 예외 처리.
            await ctx.send(f"⚠️ 캐싱 실패 (일반 모드로 진행됩니다. 원인: {e})")

        self.bot.active_sessions[game_ch.id] = session
        self.bot.active_sessions[master_ch.id] = session
        # 디스플레이 채널에서도 세션을 찾을 수 있어야 UI 버튼이 동작한다.
        if getattr(session, "display_ch_id", None):
            self.bot.active_sessions[session.display_ch_id] = session
        await core.save_session_data(self.bot, session)

        await ctx.send(f"🎉 세션 준비 완료!\n플레이어 채널: {game_ch.mention}\n마스터 채널: {master_ch.mention}")


    @commands.command(name="시작")
    @commands.has_permissions(administrator=True)
    async def start_game(self, ctx):
        """
        게임 채널의 기존 메시지를 전부 삭제한 뒤 시작 메시지를 스트리밍하고 AI 컨텍스트에 주입. (1회 한정)

        NOTE: 시작 메시지를 시스템이 아닌 AI(role="model")의 발화로 조작하여
        raw_logs에 주입함으로써, AI 스스로 게임 마스터 스탠스를 유지하도록 유도.
        게임 채널의 기존 메시지를 전부 지워 소개·캐릭터 생성 내용을 깔끔하게 정리한다.

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
        session.started_at = time.time()

        # 통계 — 세션 수·플레이 이력. has_played는 사전 프로필 생성 가능
        # 여부 판정에 쓰이므로 시작 시점에 기록해야 한다.
        try:
            for uid in (session.players or {}):
                await core.stats.bump(uid, sessions=1)
                await core.stats.mark_played(uid, session.scenario_id)
        except Exception as e:
            print(f"[통계] 세션 시작 기록 실패: {e}")

        # 디스플레이 초기 렌더 (기획서 갱신 시점 ① 고정정보)
        try:
            await core.refresh_display(self.bot, session, reason="session_start")
        except Exception as e:
            print(f"[디스플레이] 초기 렌더 실패: {e}")
        await core.save_session_data(self.bot, session)

        # ── 게임 채널 초기화: 기존 메시지 전체 삭제 ──
        # NOTE: !소개·캐릭터 생성 등 준비 단계 메시지를 정리해 실제 게임 공간을 깔끔하게 시작.
        # Discord bulk-delete는 14일 이내 메시지만 지원. 오류 발생 시 경고만 출력하고 진행.
        await ctx.send("⏳ 게임 채널을 초기화합니다...")
        try:
            deleted = await game_channel.purge(limit=None)
            if deleted:
                await ctx.send(f"🗑️ 게임 채널 메시지 {len(deleted)}개 삭제 완료.")
        except discord.Forbidden:
            await ctx.send("⚠️ 게임 채널 메시지 삭제 권한이 없습니다. 메시지를 유지한 채 시작합니다.")
        except Exception as e:
            await ctx.send(f"⚠️ 게임 채널 초기화 중 오류 발생: {e}")

        # ── 시작 상황 선택 (기획 규정) ──
        # 프로필로 필터링한 틀 중 랜덤 삼지선다를 제시한다.
        # 틀이 없는 시나리오는 기존 고정 start_message로 폴백한다.
        profile = {}
        for p_data in (session.players or {}).values():
            if isinstance(p_data, dict):
                profile = dict(p_data.get("fields") or {})
                profile.setdefault("이름", p_data.get("name", ""))
                prof_stat = p_data.get("profile")
                if isinstance(prof_stat, dict):
                    profile["능력치"] = prof_stat
                break

        options = core.start_frame.offer(session.scenario_data, profile)
        if options:
            lines = ["**시작 상황을 선택해 주십시오.**\n"]
            for i, opt in enumerate(options, 1):
                lines.append(core.start_frame.format_choice(i, opt))
            await game_channel.send(
                "\n\n".join(lines),
                view=StartFrameView(self.bot, session, options),
            )
            session.is_started = True
            await core.save_session_data(self.bot, session)
            return

        start_message = session.scenario_data.get("start_message", "> 세션이 시작됩니다.")
        start_text = f"**[세션 시작]**\n\n{start_message}"

        await core.stream_text_to_channel(self.bot, game_channel, start_text, words_per_tick=5, tick_interval=1.5)

        # NOTE: Gemini API는 contents 배열이 반드시 role="user"로 시작해야 한다.
        # start_text를 model 단독으로 삽입하면 API가 해당 메시지를 무시하므로,
        # 반드시 user→model 쌍으로 삽입한다.
        session.raw_logs.append(types.Content(role="user", parts=[types.Part.from_text(text="[세션 시작]")]))
        session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=start_text)]))
        # 기억 압축 대상에 포함하여 초기 장면 정보가 compressed_memory에 보존되도록 한다.
        session.uncompressed_logs.append(f"[세션 시작 묘사]: {start_text}")
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


    async def play_intro(self, session, chosen: dict, game_channel):
        """
        선택된 시작 상황으로 인트로를 생성·재생한다.

        기획 규정 — 채널을 정리하고, 브리핑 이후 인트로를 연결해
        한 번에 스트리밍한다. 원경에서 근경으로 좁혀 몰입되게 시작한다.
        """
        # 참가자 선택으로 채널 클리어 (기획 규정)
        try:
            await game_channel.purge(limit=100)
        except Exception as e:
            print(f"[인트로] 채널 정리 실패: {e}")

        profile = {}
        for p_data in (session.players or {}).values():
            if isinstance(p_data, dict):
                profile = dict(p_data.get("fields") or {})
                profile.setdefault("이름", p_data.get("name", ""))
                prof_stat = p_data.get("profile")
                if isinstance(prof_stat, dict):
                    profile["능력치"] = prof_stat
                break

        instruction = core.start_frame.build_intro_instruction(
            session.scenario_data, profile, chosen)

        game_cog = self.bot.get_cog("GameCog")
        if game_cog:
            # 묘사층위로 인트로를 생성한다. 이후 턴과 같은 경로를 쓴다.
            await game_cog._execute_proceed(session, instruction)
        else:
            # 폴백 — 요약만 출력한다.
            briefing = core.start_frame.build_briefing(session.scenario_data, profile)
            await core.stream_text_to_channel(
                self.bot, game_channel,
                f"{briefing}\n\n{chosen.get('summary', '')}",
                words_per_tick=5, tick_interval=1.5)

        await core.save_session_data(self.bot, session)
        try:
            await core.refresh_display(self.bot, session, reason="intro")
        except Exception:
            pass

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
        ability_stats = session.scenario_data.get("ability_stats", [])
        secondary_stats = session.scenario_data.get("profile_secondary_stats", [])
        stat_desc = session.scenario_data.get("stat_descriptions", {})

        # [캐릭터 구성 항목] — 시나리오 데이터 기반으로 동적 구성.
        # 주사위 판정 능력치(ability_stats)를 먼저, 그 외 서술/기타 항목을 뒤에 배치하고
        # stat_descriptions가 있으면 한 줄 설명을 덧붙인다. (시나리오 무관 동작)
        def _stat_line(name, tag=""):
            desc = stat_desc.get(name)
            base = f"- **{name}**{tag}"
            return f"{base}: {desc}" if desc else base

        stat_lines = [_stat_line(k) for k in ability_stats if k in pc_template]
        for k in pc_template:
            if k in ability_stats:
                continue
            stat_lines.append(_stat_line(k, " (서술 항목)" if k in secondary_stats else ""))
        stat_block = "\n".join(stat_lines)
        dice_stats_str = " · ".join(ability_stats) if ability_stats else "(없음)"

        guide_text = (
            "이제 여러분의 분신이 될 캐릭터를 만들 차례입니다. 아래 순서를 따라 진행하십시오.\n\n"
            "**[1단계] 참가** — 게임 채널에 `!참가 [캐릭터이름]` 을 입력해 세션에 참가합니다.\n\n"
            f"**[2단계] 능력치 굴림** — 마스터(GM)가 제공한 주사위로 여러분의 기본 능력치를 굴려 배분합니다. 주사위 판정에 쓰이는 능력치: {dice_stats_str}.\n\n"
            "**[3단계] 외형 설정** — 마스터(GM)과의 대화로 페르소나 및 캐릭터의 모습을 정합니다.\n\n"
            f"**[캐릭터 구성 항목]**\n{stat_block}"
        )

        full_text = f"{self.bot.intro_text}\n\n{scenario_intro}\n\n{guide_text}"

        paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]

        await ctx.send("📢 게임 채널에 소개 문단 자동 스트리밍을 시작합니다...")

        # NOTE: !소개는 극적 묘사가 아닌 설명문 위주라 묘사용(5단어/1.5초)보다 빠르게 스트리밍한다.
        # 전체 분량(~2,000자, 다수 문단)을 고려해 체감 대기 시간을 줄인다.
        # (!시작·AI 턴 묘사의 연출 속도는 그대로 유지)
        for paragraph in paragraphs:
            await core.stream_text_to_channel(self.bot, game_channel, paragraph, words_per_tick=5, tick_interval=0.6)

        await ctx.send("✅ 소개 스트리밍이 완료되었습니다.")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(SessionCog(bot))
