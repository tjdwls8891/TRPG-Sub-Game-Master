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
        if core.world_lifecycle.joins_blocked(self.session) and uid not in (self.session.players or {}):
            # H06: 동적 세계 세션은 단일 PC — 초기화 뒤 참가자를 늘리지 않는다(다인 동적 의미 없음)
            await interaction.response.send_message(
                "⚠️ 이 세션은 참가자를 더 받을 수 없습니다(1인 동적 세계·세계 준비 재시도 대기 또는 종료 진행·종료).", ephemeral=True)
            return
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
        super().__init__(timeout=None)          # v7.28: 진행 화면 — 시간 제한 없음, 재시작 시 다시 붙는다
        self.bot = bot
        self.session = session
        self.options = options
        for i, opt in enumerate(options, 1):
            self.add_item(StartFrameButton(i, opt))

    async def interaction_check(self, interaction) -> bool:
        return await core.session_flow.owner_only(self.bot, self.session, interaction)   # D5: 개설자만


class StartFrameButton(discord.ui.Button):
    def __init__(self, index: int, option: dict):
        super().__init__(label=f"{index}. {option.get('title', '')}"[:80],
                         style=discord.ButtonStyle.primary)
        self.option = option

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        session = view.session
        # v7.17 T16: 연타·두 버튼 동시 선택에도 시작은 한 번만(초기 지식 호출·genesis 중복 방지)
        if getattr(view, "_chosen", False):
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        view._chosen = True
        await interaction.response.defer()
        # v7.28: 대기 기록을 지운다(재시작 뒤 다시 붙은 화면·옛 화면의 중복 선택도 여기서 막힌다)
        if not core.session_flow.forget_prompt(session, core.session_flow.PROMPT_START_FRAMES) \
                and getattr(session, "start_frame", None):
            return
        await core.save_session_data(view.bot, session)

        # 사전 확정 정보의 반영은 play_intro → open_dynamic_world가 정한다(H06 R2-03):
        #   LEGACY = 기존 apply_facts 그대로 · DYNAMIC = 저작 world_start → genesis가 유일한 owner.
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


class InitialKnowledgeView(discord.ui.View):
    """v7.17 — 초기 지식 전달이 미확정(UNCERTAIN)이거나 재시도 대기(FAILED_RETRYABLE)일 때의 개설자 버튼.

    자동 재전송·자동 인지는 없다. [다시 보내기]는 저장된 같은 패킷을 다시 보낸다(AI 재호출 없음),
    [보내지 않고 진행]은 인지를 만들지 않고 넘어간다. 해소되면 멈췄던 단계를 이어 간다."""

    def __init__(self, bot, session, uid, phase, state, on_resolved=None):
        super().__init__(timeout=None)          # v7.28: 진행 화면 — 시간 제한 없음, 재시작 시 다시 붙는다
        self.bot, self.session, self.uid, self.phase, self.on_resolved = bot, session, uid, phase, on_resolved
        self._busy = False
        if state == core.initial_knowledge.UNCERTAIN:
            self.add_item(self._button("다시 보내기", discord.ButtonStyle.primary, self._resend))
            self.add_item(self._button("보내지 않고 진행", discord.ButtonStyle.secondary, self._skip))
        else:
            self.add_item(self._button("다시 시도", discord.ButtonStyle.primary, self._retry))

    def _button(self, label, style, fn):
        b = discord.ui.Button(label=label, style=style)

        async def on_press(interaction):
            await fn(interaction)
        b.callback = on_press
        return b

    async def interaction_check(self, interaction) -> bool:
        return await core.session_flow.owner_only(self.bot, self.session, interaction)   # D5: 개설자만

    async def _finish(self, interaction):
        for c in self.children:
            c.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self.stop()
        core.session_flow.forget_prompt(self.session, core.session_flow.PROMPT_INITIAL_KNOWLEDGE)
        if self.on_resolved is not None:
            await self.on_resolved()
        else:                                   # v7.28: 재시작 뒤 다시 붙은 화면 — 저장된 상태로 이어 간다
            await core.session_flow.continue_after_initial_knowledge(
                self.bot, self.session, interaction.channel, self.phase)

    async def _guard(self, interaction) -> bool:
        if self._busy:
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return False
        self._busy = True
        await interaction.response.defer()
        return True

    async def _resend(self, interaction):
        if not await self._guard(interaction):
            return
        st = await core.initial_knowledge.resend(self.bot, self.session, self.uid, self.phase, interaction.channel)
        if st in core.initial_knowledge.DONE:
            await self._finish(interaction)
        else:
            self._busy = False

    async def _skip(self, interaction):
        if not await self._guard(interaction):
            return
        await core.initial_knowledge.skip_uncertain(self.bot, self.session, self.uid, self.phase)
        await self._finish(interaction)

    async def _retry(self, interaction):
        if not await self._guard(interaction):
            return
        await self._finish(interaction)


class DynamicOpenRetryView(discord.ui.View):
    """H06 — INITIALIZED(캐시 미개방) 동적 세계 세션의 열기 재시도. 같은 초기화 정체성으로 이어 간다."""

    def __init__(self, bot, session):
        super().__init__(timeout=None)          # v7.28: 진행 화면 — 시간 제한 없음, 재시작 시 다시 붙는다
        self.bot = bot
        self.session = session

    async def interaction_check(self, interaction) -> bool:
        return await core.session_flow.owner_only(self.bot, self.session, interaction)   # D5: 개설자만

    @discord.ui.button(label="세션 다시 열기", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        s = self.session
        core.session_flow.forget_prompt(s, core.session_flow.PROMPT_OPEN_RETRY)
        if not (getattr(s, "world_lifecycle", None) == core.world_lifecycle.LIFECYCLE_INITIALIZED
                or core.world_lifecycle.init_failed(s) or getattr(s, "_intro_pending", False)):
            # v7.13: 다시 열 상태가 아니면 무응답으로 끝내지 않는다(이미 열렸거나 종료된 세션).
            try:
                await interaction.followup.send(
                    "ℹ️ 이 세션은 지금 다시 열 상태가 아닙니다(이미 열렸거나 종료됐습니다).", ephemeral=True)
            except Exception:
                pass
            return
        cog = self.bot.get_cog("SessionCog")
        if cog:
            await cog.play_intro(self.session, getattr(self.session, "start_frame", None) or {},
                                 interaction.channel)


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

        # 사용자 결정 D6: 유지 시간 답변을 직접 입력해야 하므로 열람자에게 쓰기를 허용한다.
        #   실제 입력 허용은 chat_guard가 정한다(참가자·입력 대기 중일 때만, 그 밖은 삭제·안내).
        display_overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                send_messages=True, read_messages=not private),
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
        session.guild_id = guild.id
        session.category_id = category.id
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
        core.logs.session_meta(session, "create")
        return session

    async def upload_cache(self, session, *, notify=None, quoted_tokens=None,
                           report_failure: bool = True) -> bool:
        """
        장기 기억 캐시를 업로드한다. 프로필 생성 완료 후 호출된다.

        기획 규정 — 유지 시간은 이 시점 이전에 선택되어 있어야 하며,
        선불식으로 잔액을 확인한 뒤 진행한다.

        report_failure=False — 호출부가 실패 안내와 재시도 버튼을 직접 보낸다(같은 실패 안내 중복 방지, v7.13).
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
            await _say("⏳ " + core.master.say("cache.open_progress", "장기 기억 캐시를 업로드하는 중…"))
            minutes = int(getattr(session, "open_minutes", 0) or 0)
            if minutes <= 0:
                # 여기까지 왔는데 시간이 없다면 선택 단계가 건너뛰어진 것이다.
                # 기본값(6시간)으로 올리면 고르지도 않은 비용이 청구된다.
                print(f"⚠️ [캐시] open_minutes 미설정 — 기본 TTL로 진행합니다. "
                      f"세션 {session.session_id}")
                await _say("⚠️ 유지 시간이 정해지지 않아 기본값으로 엽니다.")

            # WP-F: 선불 창 + provider 캐시 생성은 캐시 생애주기 서비스가 소유한다.
            #   예상액은 선불액 산정에만 쓰이고, 생성 성공 뒤에만 생성 사실·선불이 기록된다.
            # WP-F(POLICY-CACHE-01): 시간 해석 청구는 캐시 창과 별개 owner 가 해석 직후 이미
            #   처리했다. 열기 직전에는 중단된 청구를 재개하고, 임계 미만 누적은 기존 규정대로
            #   면제한다(열기 시점 초기화).
            try:
                await core.interpretation_billing.settle(self.bot, session)
                core.interpretation_billing.waive_pending(session)
            except Exception as e:
                print(f"[시간해석] 열기 전 청구 정리 실패(재시도 대기): {e}")
            try:
                res = await core.cache_lifecycle.open_window(
                    self.bot, session, quoted_tokens=quoted_tokens)
            except core.cache_lifecycle.CacheOpenInsufficientFunds as e:
                await _say("⛔ " + core.master.say("cache.insufficient_ink",
                                                    "캐시 선불 잔액이 부족해 세션을 열지 않았습니다.")
                           + f" (필요 {e.need}잉크 / 보유 {e.balance}잉크)")
                return False
            if res.get("already"):
                return True
            ttl = int(res["ttl_seconds"])
            charge_ink = int(res["charge_ink"])

            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(embed=core.build_cache_cost_embed(
                    "새 세션 캐시 생성", 0.0, res["create_krw"], session.total_cost))
                if not res.get("prepaid"):
                    await master_ch.send(
                        "⚠️ 세션 오픈 선불 기록이 완료되지 않았습니다 — 다음 캐시 조작·재시작 시 재시도합니다.")

            # POLICY-CACHE-01: 캐시 선불(환급 대상)만 여기서 결제된다 — 시간 해석은 별개 거래.
            await _say(
                "✅ " + core.master.say("cache.open_success", "세션이 열렸습니다.") + f" (유지 {ttl // 60}분)\n"
                f"> 캐시 선결제 **{charge_ink}잉크** (미사용분은 종료 시 환급)")
            return True
        except Exception as e:
            # 캐시 없이 진행하지 않는다(사용자 결정 D2). 원인은 운영 로그로만 남긴다(플레이어 채널 노출 금지).
            print(f"[캐시] 업로드 실패 session={getattr(session, 'session_id', '?')}: {type(e).__name__}: {e}")
            if report_failure:
                await _say("⚠️ " + core.master.say("cache.open_failed", "세션을 열지 못했습니다 — 다시 시도해 주십시오."))
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
                # 프로필 항목(직업·나이 등)은 players[uid]["profile"]에 있다 — 구형 "fields" 키는 호환으로만 읽는다.
                profile = dict(p_data.get("fields") or p_data.get("profile") or {})
                profile.setdefault("이름", p_data.get("name", ""))
                prof_stat = p_data.get("profile")
                if isinstance(prof_stat, dict):
                    profile["능력치"] = prof_stat
                break

        options = core.start_frame.offer(session.scenario_data, profile)
        if options:
            # v7.27: 선택지는 카드 이미지(번호 = 버튼 번호) — 렌더 실패면 기존 글 목록
            card, _k = await core.gm_board.png_file(
                core.gm_board.build_frames_svg(core.gm_board.frames_data(options)), "start_frames.png")
            if card is not None:
                msg = await game_channel.send("**시작 상황을 선택해 주십시오.**", file=card,
                                              view=StartFrameView(self.bot, session, options))
            else:
                lines = ["**시작 상황을 선택해 주십시오.**\n"]
                for i, opt in enumerate(options, 1):
                    lines.append(core.start_frame.format_choice(i, opt))
                msg = await game_channel.send(
                    "\n\n".join(lines),
                    view=StartFrameView(self.bot, session, options),
                )
            await core.session_flow.remember_prompt(self.bot, session, core.session_flow.PROMPT_START_FRAMES,
                                                    game_channel, msg, options=list(options))
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

        H06: DYNAMIC 대상(단일 PC·필수 capability) 세션은 여기서 정의 바인딩 + genesis 초기화 → 캐시 열기를
        먼저 한다. 캐시 열기가 실패하면 INITIALIZED로 남고 인트로를 하지 않는다(재시도는 같은 초기화 정체성).
        """
        if not await self.open_dynamic_world(session, chosen, game_channel):
            return
        # 참가자 선택으로 채널 클리어 (기획 규정)
        try:
            await game_channel.purge(limit=100)
        except Exception as e:
            print(f"[인트로] 채널 정리 실패: {e}")

        profile = {}
        for p_data in (session.players or {}).values():
            if isinstance(p_data, dict):
                # 프로필 항목(직업·나이 등)은 players[uid]["profile"]에 있다 — 구형 "fields" 키는 호환으로만 읽는다.
                profile = dict(p_data.get("fields") or p_data.get("profile") or {})
                profile.setdefault("이름", p_data.get("name", ""))
                prof_stat = p_data.get("profile")
                if isinstance(prof_stat, dict):
                    profile["능력치"] = prof_stat
                break

        instruction = core.start_frame.build_intro_instruction(
            session.scenario_data, profile, chosen)

        game_cog = self.bot.get_cog("GameCog")
        intro_ok = True
        if game_cog:
            # 묘사층위로 인트로를 생성한다. 이후 턴과 같은 경로를 쓴다.
            # H06 R2-01: 생성·전달이 실제로 성공했을 때만 OPEN·첫 라운드로 넘어간다.
            res = await game_cog._execute_proceed(session, instruction)
            intro_ok = bool(isinstance(res, dict) and res.get("ok"))
        else:
            # 폴백 — 요약만 출력한다.
            briefing = core.start_frame.build_briefing(session.scenario_data, profile)
            await core.stream_text_to_channel(
                self.bot, game_channel,
                f"{briefing}\n\n{chosen.get('summary', '')}",
                words_per_tick=15, tick_interval=1.5)

        master_ch = self.bot.get_channel(session.master_ch_id)
        if not intro_ok:
            session._intro_pending = True
            await core.save_session_data(self.bot, session)
            for ch, text in ((master_ch, "⚠️ 인트로 생성·전달이 실패해 세션을 열지 않았습니다(첫 라운드 보류)."),
                             (game_channel, "⏸️ " + core.master.lead(
                                 "session.intro_failed", "아래 버튼으로 다시 시도해 주십시오.",
                                 default="인트로를 내보내지 못했습니다."))):
                try:
                    if ch is game_channel:
                        await self._send_open_retry(session, ch, text)
                    elif ch is not None:
                        await ch.send(text)
                except Exception:
                    pass
            return
        session._intro_pending = False
        # H06: 인트로 완료 → 동적 세계 OPEN(strict 영속 — 실패하면 INITIALIZED 유지·라운드 보류). 레거시는 전이 없음.
        if not await core.world_lifecycle.open_durable(self.bot, session):
            try:
                if master_ch:
                    await master_ch.send("⚠️ 세션 열림(OPEN) 상태를 저장하지 못했습니다 — 첫 라운드를 보류합니다.")
                if game_channel is not None:
                    await self._send_open_retry(session, game_channel, "⏸️ " + core.master.lead(
                        "session.intro_failed", "> 세션 상태 저장에 실패했습니다. 아래 버튼으로 다시 시도해 주십시오."))
            except Exception:
                pass
            return
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

    async def _send_open_retry(self, session, channel, text):
        """v7.28: 열기 재시도 버튼 — 시간 제한 없음, 재시작 뒤에도 같은 메시지에 다시 붙도록 기록한다."""
        if channel is None:
            return None
        try:
            msg = await channel.send(text, view=DynamicOpenRetryView(self.bot, session))
        except Exception:
            return None
        await core.session_flow.remember_prompt(self.bot, session, core.session_flow.PROMPT_OPEN_RETRY, channel, msg)
        return msg

    async def open_dynamic_world(self, session, chosen: dict, game_channel) -> bool:
        """H06 — 시작 선택 직후 동적 세계 초기화 + 캐시 열기. False = 인트로 보류(INITIALIZED 유지).

        대상이 아니면(LEGACY) 세계를 건드리지 않고, 연기했던 캐시 업로드만 기존 의미대로 진행한다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def _say(text, **kw):
            try:
                if game_channel is not None:
                    return await game_channel.send(text, **kw)
            except Exception:
                return None
            return None

        # v7.17: 시작 틀 확정 직후·genesis 전 — START 초기 지식(PROFILE 미확정분도 여기서 먼저 해소). 이미 묶인 세계는 생략.
        if not core.world_lifecycle.world_bound(session):
            for phase in ("PROFILE", "START"):
                if not await core.session_flow.initial_knowledge_gate(
                        self.bot, session, game_channel, phase, frame=chosen if phase == "START" else None,
                        on_resolved=lambda: self.play_intro(session, chosen, game_channel)):
                    return False
        res = await core.world_lifecycle.initialize_world(self.bot, session, chosen)
        if res.outcome == core.world_lifecycle.INIT_FAILED:
            # H06 R2-01: fail-closed — 레거시 캐시·인트로로 넘어가지 않는다(재시도 가능, 운영자 표시)
            if master_ch:
                try:
                    await master_ch.send(f"⛔ [동적 세계] 초기화 실패 — 세션을 열지 않았습니다. ({res.reason})")
                except Exception:
                    pass
            await self._send_open_retry(session, game_channel, "⏸️ " + core.master.lead(
                "session.opening_failed",
                "> 세계를 준비하지 못해 세션을 열지 않았습니다. 운영자 확인 후 아래 버튼으로 다시 시도해 주십시오."))
            return False
        if res.outcome == core.world_lifecycle.INIT_LEGACY:
            # 기존 LEGACY 시작 — 사전 확정 정보 반영(기획 규정)과 연기된 캐시를 기존 의미대로
            core.start_frame.apply_facts(session, chosen)
            if not getattr(session, "cache_name", None):
                # v7.33(②): 업로드 결과를 본다 — 캐시 없이 인트로로 넘어가지 않는다(DYNAMIC 분기와 같은 재시도 안내)
                ok = await self.upload_cache(session, notify=_say,
                                             quoted_tokens=getattr(session, "open_quoted_tokens", None),
                                             report_failure=False)
                if not ok:
                    session._intro_pending = True          # [세션 다시 열기]가 받아들이는 재시도 대기 표식
                    await core.save_session_data(self.bot, session)
                    await self._send_open_retry(session, game_channel, "⏸️ " + core.master.lead(
                        "session.opening_failed",
                        "> 세션 캐시를 열지 못해 인트로를 보류합니다. 잔액·상태를 확인한 뒤 아래 버튼으로 다시 여십시오."))
                    return False
            return True
        # DYNAMIC — 장소·시간은 정본 genesis가 유일한 owner. 표시용 레거시 world_timeline은 정본에서 파생한다
        #   (시작 틀 facts를 두 번째 권위로 쓰지 않는다 — 장소·시간과 무관한 세력 표시만 유지).
        core.world_lifecycle.apply_compat_view(session, chosen)
        ok = await self.upload_cache(session, notify=_say,
                                     quoted_tokens=getattr(session, "open_quoted_tokens", None),
                                     report_failure=False)
        if not ok:
            await core.save_session_data(self.bot, session)
            await self._send_open_retry(session, game_channel, "⏸️ " + core.master.lead(
                "session.opening_failed",
                "> 세계는 준비됐지만 세션 캐시를 열지 못해 인트로를 보류합니다. "
                "잔액·상태를 확인한 뒤 아래 버튼으로 다시 여십시오."))
            return False
        return True

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
