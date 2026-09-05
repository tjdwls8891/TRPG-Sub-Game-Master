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
class JoinView(discord.ui.View):
    """
    게임 채널 참가 버튼 — persistent view.

    NOTE: 기획 규정상 명령어 사용은 일반적인 모든 상황에서 불가·불필요해야
          한다. !참가를 치는 대신 버튼으로 참가하고, 이름은 모달로 받는다.
          참가 즉시 프로필 생성 UI가 이어진다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🙋 세션 참가", style=discord.ButtonStyle.success,
                       custom_id="session:join")
    async def join(self, interaction: discord.Interaction, _b: discord.ui.Button):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message(
                "세션을 찾을 수 없습니다.", ephemeral=True)
            return
        if str(interaction.user.id) in (session.players or {}):
            await interaction.response.send_message(
                "이미 참가하셨습니다.", ephemeral=True)
            return
        await interaction.response.send_modal(JoinModal(self.bot, session))


class JoinModal(discord.ui.Modal, title="세션 참가"):
    char_name = discord.ui.TextInput(
        label="캐릭터 이름", required=True, max_length=20,
        placeholder="예: 임성진")

    def __init__(self, bot, session):
        super().__init__()
        self.bot = bot
        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        name = str(self.char_name).strip()
        uid = str(interaction.user.id)
        base_profile = (self.session.scenario_data.get("pc_template") or {}).copy()

        self.session.players[uid] = {
            "name": name, "profile": base_profile, "appearance": ""
        }
        await core.save_session_data(self.bot, self.session)

        try:
            await interaction.user.edit(nick=name)
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ {interaction.user.mention}님이 **{name}**(으)로 참가했습니다.")

        # 프로필 생성 UI를 이어서 시작한다.
        # 제작 플로우 진행 중이면 flow의 profile 단계가 담당하므로 중복 시작하지 않는다.
        try:
            st = getattr(self.session, "creation_state", None) or {}
            if st.get("step") == "profile":
                await core.session_flow.render(
                    self.bot, self.session, interaction.channel)
                return
            await core.profile_creation_ui.start(
                self.bot, self.session, uid, name, interaction.channel)
        except Exception as e:
            print(f"[참가] 프로필 생성 시작 실패: {e}")
            await interaction.followup.send(
                "⚠️ 프로필 생성 UI를 열지 못했습니다. "
                "`!능력치` `!외형` `!설정` 명령으로 설정해 주십시오.")


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

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """
        세션 음성 채널 참가자를 인식해 상시 참가 처리한다(기획 규정).

        NOTE: 세션 채널에 들어온 것만으로 참가로 보되, 이미 등록된 참가자는
              건드리지 않는다. 봇이 아직 음성에 연결돼 있지 않으면 함께
              연결해 BGM·TTS가 즉시 들리게 한다.

              나갈 때 참가를 취소하지는 않는다. 잠시 끊긴 것과 이탈을
              구분할 수 없고, 프로필과 진행 상태가 이미 세션에 묶여 있다.
        """
        if member.bot:
            return
        if after.channel is None or before.channel == after.channel:
            return

        # 이 음성 채널을 쓰는 세션 찾기
        session = None
        for s in set(self.bot.active_sessions.values()):
            if getattr(s, "voice_ch_id", None) == after.channel.id:
                session = s
                break
        if session is None:
            return

        uid = str(member.id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        # 봇 음성 연결 — BGM·TTS 출력 경로를 미리 확보한다.
        vc = getattr(session, "voice_client", None)
        if not (vc and vc.is_connected()):
            try:
                session.voice_client = await after.channel.connect()
            except Exception as e:
                print(f"[음성] 연결 실패: {e}")

        if uid in (session.players or {}):
            return   # 이미 참가자

        # 비공개 세션이면 읽기 권한을 부여한다.
        if getattr(session, "is_private", False):
            for ch_id in (session.game_ch_id, getattr(session, "display_ch_id", None)):
                ch = self.bot.get_channel(ch_id) if ch_id else None
                if ch:
                    try:
                        await ch.set_permissions(member, read_messages=True)
                    except Exception:
                        pass

        if game_ch:
            await game_ch.send(
                f"🎧 {member.mention} 님이 음성 채널에 참가했습니다.\n"
                f"아래 버튼으로 캐릭터를 만들고 세션에 합류하십시오.",
                view=JoinView(self.bot))

    async def provision_session(self, guild, author, scenario_id: str,
                                *, kind: str = "solo", private: bool = False,
                                notify=None):
        """
        채널·세션 객체만 만든다. 캐시는 올리지 않는다.

        NOTE: 기획 규정상 캐시 업로드는 프로필 생성 이후다. 채널 생성과
              동시에 올리면 플레이어가 캐릭터를 만들기도 전에 비용이
              청구되고, 중도 이탈 시 그대로 손실이 된다.
              유지 시간 선택제도 이 시점에는 아직 답을 받지 못했다.
        """

        async def _say(text, **kw):
            if notify:
                try:
                    return await notify(text, **kw)
                except Exception:
                    return None
            return None

        scenario_data = core.load_scenario_from_file(scenario_id)
        if not scenario_data:
            await _say(f"⚠️ '{scenario_id}' 시나리오를 읽지 못했습니다.")
            return None

        session_id = str(uuid.uuid4())[:8]
        os.makedirs(f"sessions/{session_id}", exist_ok=True)

        category = await guild.create_category(f"TRPG Session {session_id}")

        # 비공개 세션은 즉시 권한으로 비참가자 읽기를 차단한다(기획 규정).
        base_deny = discord.PermissionOverwrite(read_messages=False) if private else None

        game_overwrites = {guild.default_role: discord.PermissionOverwrite(
            send_messages=False, read_messages=not private)}
        game_ch = await guild.create_text_channel(
            f"game-{session_id}", category=category, overwrites=game_overwrites)

        master_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True),
        }
        master_ch = await guild.create_text_channel(
            f"master-{session_id}", category=category, overwrites=master_overwrites)

        display_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                send_messages=False, read_messages=not private),
            guild.me: discord.PermissionOverwrite(send_messages=True),
        }
        display_ch = await guild.create_text_channel(
            f"display-{session_id}", category=category, overwrites=display_overwrites)

        # 음성 채널 — 3채널 구성(기획 규정). 참가자 인식에 쓰인다.
        voice_ch = None
        try:
            voice_ch = await guild.create_voice_channel(
                f"voice-{session_id}", category=category)
        except Exception as e:
            print(f"[세션] 음성 채널 생성 실패: {e}")

        session = core.TRPGSession(session_id, game_ch.id, master_ch.id,
                                   scenario_id, scenario_data)
        session.display_ch_id = display_ch.id
        session.session_kind = kind
        session.is_private = private
        if voice_ch:
            session.voice_ch_id = voice_ch.id

        # 개설자에게 읽기 권한 부여 (비공개 세션 대비)
        if private:
            for ch in (game_ch, display_ch):
                try:
                    await ch.set_permissions(author, read_messages=True)
                except Exception:
                    pass

        self.bot.active_sessions[game_ch.id] = session
        self.bot.active_sessions[master_ch.id] = session
        self.bot.active_sessions[display_ch.id] = session
        await core.save_session_data(self.bot, session)
        return session

    async def upload_cache(self, session, *, notify=None) -> bool:
        """
        장기 기억 캐시를 업로드한다. 프로필 생성 완료 후 호출된다.

        기획 규정 — 유지 시간은 이 시점 이전에 선택되어 있어야 하며,
        선불식으로 잔액을 확인한 뒤 진행한다.
        """

        async def _say(text, **kw):
            if notify:
                try:
                    return await notify(text, **kw)
                except Exception:
                    return None
            return None

        if getattr(session, "cache_name", None):
            return True

        try:
            await _say("⏳ 장기 기억 캐시를 업로드하는 중…")
            caching_text, cache_tokens, base_text = await core.build_scenario_cache_text(
                self.bot, core.DEFAULT_MODEL, session.scenario_data, session=session
            )

            # 선택된 유지 시간을 먼저 확정한다. 비용 계산과 TTL이 같은 값을 써야 한다.
            minutes = int(getattr(session, "open_minutes", 0) or 0)
            if minutes <= 0:
                # 여기까지 왔는데 시간이 없다면 선택 단계가 건너뛰어진 것이다.
                # 기본값(6시간)으로 올리면 고르지도 않은 비용이 청구된다.
                print(f"⚠️ [캐시] open_minutes 미설정 — 기본 TTL로 진행합니다. "
                      f"세션 {session.session_id}")
                await _say("⚠️ 유지 시간이 정해지지 않아 기본값으로 엽니다.")
            ttl = minutes * 60 if minutes else core.CACHE_TTL_SECONDS
            store_hours = ttl / 3600

            # 업로드(입력) + 유지(저장) 비용을 함께 계산한다.
            upload_cost = core.calculate_upload_cost(
                core.DEFAULT_MODEL, input_tokens=cache_tokens,
                store_hours=store_hours)
            if cache_tokens <= 0:
                print(f"⚠️ [캐시] 토큰 수가 0입니다. 비용이 0원으로 계산됩니다.")
            session.total_cost += upload_cost
            session.cache_created_at = time.time()
            # 새로 열렸으므로 만료 알림 플래그를 푼다.
            session.cache_expired_notified = False
            session.cache_tokens = cache_tokens
            session.cache_text = base_text
            core.write_cost_log(session.session_id, "초기 캐시 생성",
                                cache_tokens, 0, 0, upload_cost, session.total_cost)

            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(embed=core.build_cache_cost_embed(
                    "새 세션 캐시 생성", 0.0, upload_cost, session.total_cost))

            cache = await asyncio.to_thread(
                self.bot.genai_client.caches.create,
                model=core.DEFAULT_MODEL,
                config=types.CreateCachedContentConfig(
                    system_instruction=self.bot.system_instruction,
                    contents=[types.Content(role="user",
                                            parts=[types.Part.from_text(text=caching_text)])],
                    ttl=f"{ttl}s",
                ),
            )
            session.cache_obj = cache
            session.cache_name = cache.name
            session.cache_model = core.DEFAULT_MODEL
            core.update_session_cache_state(session)

            # 선불 차감 (기획 규정). 해석 비용이 2잉크 이상이면 함께 청구한다.
            charge_ink = core.cost_to_ink(upload_cost)
            interpret_charge, interpret_ink = core.should_charge_interpretation(session)
            if interpret_charge:
                charge_ink += interpret_ink
            session.interpret_cost_krw = 0.0
            session.open_prepaid_ink = charge_ink

            for uid in (session.players or {}) or [getattr(session, "creator_uid", "")]:
                if not uid:
                    continue
                await core.accounts.deduct_ink(uid, charge_ink, allow_overdraft=True)
            await core.save_session_data(self.bot, session)

            await _say(
                f"✅ 세션이 열렸습니다. (유지 {ttl // 60}분)\n"
                f"> 선결제 **{charge_ink}잉크**"
                + (f" (시간 해석 {interpret_ink}잉크 포함)" if interpret_charge else ""))
            return True
        except Exception as e:
            await _say(f"⚠️ 캐시 업로드 실패 (일반 모드로 진행됩니다. 원인: {e})")
            return False

    async def build_session(self, guild, author, scenario_id: str, notify=None):
        """세션 생성 — 채널 프로비저닝 후 즉시 캐시까지 올린다.

        NOTE: !새세션 명령의 기존 동작을 유지하는 경로다.
              버튼 플로우는 provision_session과 upload_cache를 단계에 맞춰
              따로 호출한다.
        """
        ok, notice = await core.ensure_agreed(self.bot, author)
        if not ok:
            if notify:
                await notify(notice)
            return None

        session = await self.provision_session(
            guild, author, scenario_id, notify=notify)
        if not session:
            return None
        await self.upload_cache(session, notify=notify)

        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            try:
                await game_ch.send(
                    "**세션이 준비되었습니다.**\n"
                    "아래 버튼을 누르면 이름을 묻고 캐릭터 만들기가 이어집니다.\n"
                    "> 만들고 나면 이 채널에서 하고 싶은 것을 적으시면 됩니다.",
                    view=JoinView(self.bot),
                )
            except Exception as e:
                print(f"[세션] 참가 버튼 배치 실패: {e}")
        return session

    @commands.command(name="새세션")
    async def create_session(self, ctx, scenario_id: str = None):
        """세션 생성 명령. 실제 작업은 build_session이 수행한다."""
        ok, notice = await core.ensure_agreed(self.bot, ctx.author)
        if not ok:
            await ctx.send(notice)
            return
        if not scenario_id:
            scenarios = core.get_available_scenarios()
            await ctx.send(f"⚠️ 시나리오 파일명을 입력해주세요. 예: `!새세션 dark_fantasy`\n(현재 파일: {', '.join(scenarios)})")
            return
        await self.build_session(ctx.guild, ctx.author, scenario_id, notify=ctx.send)

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

        await core.stream_text_to_channel(self.bot, game_channel, start_text, words_per_tick=15, tick_interval=1.5)

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
                words_per_tick=15, tick_interval=1.5)

        await core.save_session_data(self.bot, session)
        try:
            await core.refresh_display(self.bot, session, reason="intro")
        except Exception:
            pass

        # 인트로가 끝나면 자동 GM이 서사 계획을 세우고 첫 라운드를 연다.
        # 이것이 없으면 세션은 열렸는데 GM이 아무것도 묻지 않는다.
        if getattr(session, "gm_active", False):
            gm_cog = self.bot.get_cog("GMCog")
            if gm_cog:
                import asyncio
                asyncio.create_task(gm_cog._init_narrative_and_start(session))
            else:
                print("[인트로] GMCog를 찾지 못해 라운드를 시작하지 못했습니다")

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
            await core.stream_text_to_channel(self.bot, game_channel, paragraph, words_per_tick=15, tick_interval=0.6)

        await ctx.send("✅ 소개 스트리밍이 완료되었습니다.")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(SessionCog(bot))

    # persistent view 등록 — 봇 재시작 후에도 참가 버튼이 동작해야 한다.
    if not getattr(bot, "_join_view_registered", False):
        bot.add_view(JoinView(bot))
        bot._join_view_registered = True
