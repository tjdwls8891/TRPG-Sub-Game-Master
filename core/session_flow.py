# 세션 제작 과정 — 기획서 17단계 통합 플로우
#
# [단계 순서] 기획 규정 그대로
#   1  계정 등록 확인
#   2  세션 종류 선택 + 생성 취소 (권한자에겐 마스터 세션 추가)
#   3  카테고리·채널 생성 (음성·게임·디스플레이)
#   4  게임 채널 참가자 호출 + 음성채널 참가 안내
#   5  비공개 세션 여부
#   6  공통 소개 (TTS + 테마곡 스트리밍)
#   7  TTS 사용 여부 → 스트리밍 속도 조절
#   8  인지 수준 분기 (첫 플레이 풀소개 / 경험자 케이스 트리)
#   9  시나리오 선택 (정보·가격 안내 확인 메시지)
#   10 턴 TTS 여부 + 목소리 선택
#   11 기억 압축 방식 선택
#   12 프로필 생성 (사전 프로필 여부)
#   13 프로필 저장 여부
#   14 캐시 업로드 시간 입력
#   15 캐시 업로드 → 세션 오픈
#   16 시작 상황 삼지선다
#   17 채널 클리어 + 인트로
#
# [캐시 업로드가 뒤에 있는 이유]
#   채널 생성과 동시에 올리면 플레이어가 캐릭터를 만들기도 전에 비용이
#   청구되고, 중도 이탈 시 그대로 손실이 된다. 유지 시간도 이 시점에는
#   아직 답을 받지 못했다.
import discord

from . import creation
from . import intro as intro_mod
from . import memory_plan
from . import profiles as profile_store
from . import stats
from .constants import TTS_VOICES
from .io import get_available_scenarios, load_scenario_from_file

# 소개 단계에서 보여줄 항목 (기획 규정 — TRPG 개념·특성·진행·과금·예시·디스플레이)
def _state(session):
    return creation.get_state(session)


async def advance_to(bot, session, channel, step: str = None):
    """다음 단계를 렌더한다. step을 주면 그 단계로 건너뛴다."""
    if step:
        creation.get_state(session)["step"] = step
    await render(bot, session, channel)


async def render(bot, session, channel):
    """현재 단계 화면을 그린다.

    각 단계는 자신의 뷰를 띄우고 끝난다. 진행은 버튼 콜백이 이어받는다.
    """
    step = creation.current_step(session)
    progress = creation.progress_text(session)

    if step == "private":
        await channel.send(
            f"**세션 공개 설정**\n> {progress}\n\n"
            "비공개 세션으로 여시겠습니까?\n"
            "> 비공개로 열면 참가자 외에는 채널을 볼 수 없습니다.",
            view=PrivateView(bot, session, channel))

    elif step == "intro":
        uid = _owner(session)
        level = intro_mod.judge_level(
            stats.load_stats(str(uid)), getattr(session, "scenario_id", ""))
        st = creation.get_state(session)
        st["intro_level"] = level

        if level == intro_mod.LEVEL_NEW:
            # 첫 플레이 — 건너뛰기 없이 순서대로(기획 규정)
            view = IntroStepView(bot, session, channel, level)
            await view.open(channel)
        else:
            # 경험자·숙련자 — 케이스 트리로 물어가며 진행(기획 규정)
            view = IntroCaseView(bot, session, channel, level)
            await channel.send(embed=view.embed(), view=view)

    elif step == "scenario":
        scenarios = _visible_scenarios(session)
        await channel.send(
            f"**시나리오 선택**\n> {progress}\n\n"
            "어느 세계에서 시작하시겠습니까?\n"
            "> 고르시면 배경과 예상 비용을 먼저 보여드립니다.",
            view=ScenarioView(bot, session, channel, scenarios))

    elif step == "tts":
        await channel.send(
            f"**음성 설정**\n> {progress}\n\n"
            "턴 묘사를 음성으로 들으시겠습니까?\n"
            "> 켜면 묘사 분량에 비례해 비용이 추가됩니다. 언제든 끌 수 있습니다.",
            view=TTSView(bot, session, channel))

    elif step == "memory":
        # 대략적 비용 증가 양상을 함께 제시한다(기획 규정).
        curves = memory_plan.compare_curves(30)
        curve_lines = ["\n**30턴 기준 누적 압축 호출**"]
        for key, vals in curves.items():
            label = memory_plan.PLANS[key]["label"]
            curve_lines.append(
                f"> {label}: 10턴 {vals[9]}회 · 20턴 {vals[19]}회 · 30턴 {vals[29]}회")
        await channel.send(
            f"**기억 방식**\n> {progress}\n\n"
            "턴이 쌓이면 지난 일을 간추려 보관합니다. 자주 간추릴수록 "
            "세부가 오래 남지만 그만큼 비용이 듭니다.\n\n"
            f"{memory_plan.format_plans()}\n"
            + "\n".join(curve_lines),
            view=MemoryPlanView(bot, session, channel))

    elif step == "profile":
        await _start_profile(bot, session, channel)

    elif step == "open":
        await channel.send(
            f"**세션 오픈**\n> {progress}\n\n"
            "이제 세계를 불러올 차례입니다. 얼마나 플레이하실 예정인가요?\n"
            "> 그 시간만큼 미리 결제하고, 일찍 닫으면 남은 만큼 돌려드립니다.\n"
            "> 아래 버튼을 누르면 디스플레이 채널에서 답하실 수 있습니다.",
            view=OpenTimeView(bot, session, channel))

    elif step == "start":
        await _offer_start_frames(bot, session, channel)


def _owner(session):
    """세션 개설자 uid. players가 비어 있으면 빈 문자열."""
    for uid in (session.players or {}):
        return uid
    return getattr(session, "creator_uid", "") or ""


def _visible_scenarios(session) -> list:
    """선택 가능한 시나리오. 블락 시나리오는 마스터 세션에만 노출한다."""
    all_ids = get_available_scenarios()
    if getattr(session, "session_kind", "solo") == "master":
        return all_ids
    out = []
    for sid in all_ids:
        data = load_scenario_from_file(sid) or {}
        if data.get("blocked"):
            continue
        out.append(sid)
    return out


# ── 단계별 뷰 ────────────────────────────────────────────
class _Step(discord.ui.View):
    def __init__(self, bot, session, channel, timeout=900):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.session = session
        self.channel = channel

    async def _next(self, interaction, field: str, value, *, to: str = None):
        creation.record(self.session, field, value)
        creation.advance(self.session, to=to)
        from .io import save_session_data
        await save_session_data(self.bot, self.session)
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass
        await render(self.bot, self.session, self.channel)


class PrivateView(_Step):
    @discord.ui.button(label="공개 세션", style=discord.ButtonStyle.primary)
    async def public(self, interaction, _b):
        await interaction.response.defer()
        self.session.is_private = False
        await self._next(interaction, "private", "공개")

    @discord.ui.button(label="🔒 비공개 세션", style=discord.ButtonStyle.secondary)
    async def private(self, interaction, _b):
        await interaction.response.defer()
        self.session.is_private = True
        # 즉시 권한으로 비참가자 읽기 차단(기획 규정)
        for ch_id in (self.session.game_ch_id, self.session.display_ch_id):
            ch = self.bot.get_channel(ch_id) if ch_id else None
            if ch:
                try:
                    await ch.set_permissions(interaction.guild.default_role,
                                             read_messages=False)
                    await ch.set_permissions(interaction.user, read_messages=True)
                except Exception as e:
                    print(f"[세션] 비공개 권한 적용 실패: {e}")
        await self._next(interaction, "private", "비공개")


class IntroStepView(_Step):
    """
    소개를 한 화면에서 진행한다.

    기획 규정 — 항목별로 설명문을 준비해놓고 순서를 정해둔 뒤,
    다음 설명을 출력할지 건너뛸지 결정하게 한다.

    내용과 버튼을 같은 메시지에 두고 갱신한다. 별도 메시지로 쏟아내면
    위아래로 스크롤해야 하고 지난 항목이 계속 쌓인다.
    """

    def __init__(self, bot, session, channel, level, order=None):
        super().__init__(bot, session, channel)
        self.level = level
        self.order = list(order or intro_mod.FULL_ORDER)
        self.index = 0
        self.message = None
        self.started = False
        # 항목별 문안을 한 번만 뽑아 고정한다. 이전/다음을 오갈 때마다
        # 바리에이션이 바뀌면 같은 항목이 다른 글로 보인다.
        self._bodies = {}
        self._pending_image = None
        self._build()

    # ── 화면 ──
    def _embed(self) -> discord.Embed:
        total = len(self.order)

        if not self.started:
            return discord.Embed(
                title="소개",
                description=intro_mod.greeting(self.level),
                color=0x5865F2,
            ).set_footer(text=f"모두 {total}가지입니다.")

        key = self.order[self.index]
        # 문안은 고정한다. 버튼을 누를 때마다 표현이 바뀌면 혼란스럽다.
        body = self._bodies.setdefault(key, intro_mod.get_body(key, self.level))

        sections = intro_mod.split_sections(body)
        e = discord.Embed(title=intro_mod.get_label(key), color=0x5865F2)
        if sections:
            # 첫 구획은 설명문으로, 나머지는 필드로 나눠 읽기 쉽게 한다.
            e.description = sections[0][1]
            for _t, chunk in sections[1:]:
                e.add_field(name="\u200b", value=chunk, inline=False)
        else:
            e.description = body

        # 기획 규정의 '미디어 활용한 설명'. 시나리오가 지정한 이미지가
        # 실제로 있으면 임베드에 붙인다. 없으면 텍스트만 나간다.
        sd = getattr(self.session, "scenario_data", {}) or {}
        fname = intro_mod.media_for(key, sd)
        if fname:
            import os
            path = os.path.join(
                f"media/{getattr(self.session, 'scenario_id', '') or ''}", fname)
            if os.path.exists(path):
                e.set_image(url=f"attachment://{os.path.basename(path)}")
                self._pending_image = path
            else:
                self._pending_image = None
        else:
            self._pending_image = None

        # 진행도는 한 곳에만 — 점으로 표시해 한눈에 들어오게 한다.
        dots = "".join("●" if i <= self.index else "○" for i in range(total))
        e.set_footer(text=f"{dots}   {self.index + 1} / {total}")
        return e

    def _build(self):
        self.clear_items()
        total = len(self.order)

        if not self.started:
            self.add_item(IntroNavButton("start", "▶ 시작하기",
                                         discord.ButtonStyle.primary))
            if self.level != intro_mod.LEVEL_NEW:
                self.add_item(IntroNavButton("skip_all", "모두 건너뛰기",
                                             discord.ButtonStyle.secondary))
            return

        # 이전
        self.add_item(IntroNavButton(
            "prev", "◀ 이전", discord.ButtonStyle.secondary,
            disabled=self.index <= 0))

        # 다음 — 마지막이면 마침
        last = self.index >= total - 1
        self.add_item(IntroNavButton(
            "next", "마치기" if last else "다음 ▶",
            discord.ButtonStyle.primary))

        if self.level != intro_mod.LEVEL_NEW:
            self.add_item(IntroNavButton("skip_all", "모두 건너뛰기",
                                         discord.ButtonStyle.secondary))

    # ── 조작 ──
    async def open(self, channel):
        """첫 화면을 띄운다."""
        embed = self._embed()
        self.message = await channel.send(embed=embed, view=self)

    async def _edit(self, interaction):
        """화면을 갱신한다. 이미지가 붙는 항목은 메시지를 새로 보낸다.

        NOTE: 첨부 파일은 edit으로 교체할 수 없다. 이미지가 있는 항목만
              예외적으로 다시 보내고, 나머지는 같은 메시지를 고친다.
        """
        embed = self._embed()
        if self._pending_image:
            try:
                await interaction.message.delete()
            except Exception:
                pass
            self.message = await self.channel.send(
                embed=embed, view=self,
                file=discord.File(self._pending_image))
            return
        try:
            await interaction.message.edit(embed=embed, view=self, attachments=[])
        except Exception:
            try:
                await interaction.message.edit(embed=embed, view=self)
            except Exception:
                pass

    async def go(self, interaction, action: str):
        if action == "start":
            self.started = True
        elif action == "next":
            if self.index >= len(self.order) - 1:
                await self.finish(interaction, "완료")
                return
            self.index += 1
        elif action == "prev":
            self.index = max(0, self.index - 1)
        elif action == "skip_all":
            await self.finish(interaction, "건너뜀")
            return

        self._build()
        await self._edit(interaction)

    async def finish(self, interaction, note: str):
        """소개를 닫고 다음 단계로. 화면은 한 줄로 접는다."""
        self.clear_items()
        done = discord.Embed(
            title="소개를 마쳤습니다",
            description="이제 세계를 고를 차례입니다.",
            color=0x5865F2,
        )
        try:
            await interaction.message.edit(embed=done, view=None)
        except Exception:
            pass
        await self._next(interaction, "intro", note)


class IntroNavButton(discord.ui.Button):
    def __init__(self, action: str, label: str,
                 style=discord.ButtonStyle.secondary, disabled: bool = False):
        super().__init__(label=label, style=style, disabled=disabled)
        self.action = action

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.view.go(interaction, self.action)


class IntroCaseView(_Step):
    """경험자·숙련자 — 무엇이 궁금한지 물어 갈래를 나눈다."""

    def __init__(self, bot, session, channel, level):
        super().__init__(bot, session, channel)
        self.level = level
        for i, (key, case) in enumerate(intro_mod.CASES.items()):
            self.add_item(CaseButton(key, case["label"], row=i // 2))

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="소개",
            description=intro_mod.greeting(self.level),
            color=0x5865F2,
        ).set_footer(text="필요하신 것만 골라 보셔도 됩니다.")


class CaseButton(discord.ui.Button):
    def __init__(self, key: str, label: str, row: int = 0):
        style = (discord.ButtonStyle.primary if key == "skip"
                 else discord.ButtonStyle.secondary)
        super().__init__(label=label, style=style, row=row)
        self.key = key

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        case = intro_mod.CASES.get(self.key) or {}
        topics = case.get("topics") or []

        if not topics:
            v.clear_items()
            try:
                await interaction.message.edit(
                    embed=discord.Embed(
                        title="바로 시작합니다",
                        description="필요하시면 언제든 다시 물어보셔도 됩니다.",
                        color=0x5865F2),
                    view=None)
            except Exception:
                pass
            await v._next(interaction, "intro", case.get("label", self.key))
            return

        # 같은 메시지를 소개 화면으로 바꿔 이어간다. 새 메시지를 쌓지 않는다.
        step = IntroStepView(v.bot, v.session, v.channel, v.level, order=topics)
        step.started = True
        step._build()
        step.message = interaction.message
        try:
            await interaction.message.edit(embed=step._embed(), view=step)
        except Exception:
            pass


class ScenarioView(_Step):
    def __init__(self, bot, session, channel, scenarios: list):
        super().__init__(bot, session, channel)
        self.add_item(ScenarioSelect(scenarios))


class ScenarioSelect(discord.ui.Select):
    def __init__(self, scenarios):
        super().__init__(
            placeholder="시나리오 선택",
            options=[discord.SelectOption(label=s[:100], value=s)
                     for s in scenarios[:25]])

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        sid = self.values[0]
        data = load_scenario_from_file(sid) or {}
        intro = (data.get("scenario_intro") or "")[:600]

        # 가격 안내 — 오픈·유지비용과 턴 진행비용을 함께 제시한다(기획 규정).
        cost_note = ""
        try:
            from .estimate import estimate_session_open, DEFAULT_BASELINE
            from .ink import cost_to_ink
            from .cost import calculate_text_gen_cost_breakdown
            from .constants import DEFAULT_MODEL

            probe = type("P", (), {"cache_tokens": 0, "cache_read_tokens": 0})()
            # 룰북 분량으로 캐시 토큰을 근사한다(실제 값은 업로드 시 확정).
            rough = int(len(str(data.get("worldview", ""))) * 0.65)
            probe.cache_tokens = rough
            open3 = estimate_session_open(probe, 3.0)
            turn_krw = calculate_text_gen_cost_breakdown(
                DEFAULT_MODEL, input_tokens=rough // 3,
                output_tokens=DEFAULT_BASELINE["narration_out"],
                cached_read_tokens=rough)["total_krw"]
            cost_note = (
                f"\n\n**예상 비용**\n"
                f"> 오픈·3시간 유지 약 {open3['total_ink']}잉크\n"
                f"> 턴 진행 약 {cost_to_ink(turn_krw)}잉크 (턴이 쌓일수록 완만히 증가)")
        except Exception as e:
            print(f"[비용안내] 산출 실패: {e}")

        await interaction.followup.send(
            f"**{sid}**\n{intro}{cost_note}\n\n"
            f"이 시나리오로 진행하시겠습니까?",
            view=ScenarioConfirmView(v.bot, v.session, v.channel, sid, data))


class ScenarioConfirmView(_Step):
    def __init__(self, bot, session, channel, sid: str, data: dict):
        super().__init__(bot, session, channel)
        self.sid = sid
        self.data = data

    @discord.ui.button(label="이 시나리오로", style=discord.ButtonStyle.success)
    async def ok(self, interaction, _b):
        await interaction.response.defer()
        self.session.scenario_id = self.sid
        self.session.scenario_data = self.data
        await self._next(interaction, "scenario", self.sid)

    @discord.ui.button(label="다시 고르기", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _b):
        await interaction.response.defer()
        try:
            await interaction.message.edit(view=None)
        except Exception:
            pass
        await render(self.bot, self.session, self.channel)


class TTSView(_Step):
    @discord.ui.button(label="🔊 사용", style=discord.ButtonStyle.primary)
    async def on(self, interaction, _b):
        await interaction.response.defer()
        self.session.tts_enabled = True
        await interaction.message.edit(view=None)
        await interaction.followup.send(
            "목소리를 고르십시오.",
            view=VoiceView(self.bot, self.session, self.channel))

    @discord.ui.button(label="사용 안 함", style=discord.ButtonStyle.secondary)
    async def off(self, interaction, _b):
        await interaction.response.defer()
        self.session.tts_enabled = False
        await self._next(interaction, "tts", "끔")


class VoiceView(_Step):
    def __init__(self, bot, session, channel):
        super().__init__(bot, session, channel)
        voices = list(TTS_VOICES)[:25] if TTS_VOICES else []
        if voices:
            self.add_item(VoiceSelect(voices))


class VoiceSelect(discord.ui.Select):
    def __init__(self, voices):
        super().__init__(
            placeholder="목소리 선택",
            options=[discord.SelectOption(label=str(v)[:100], value=str(v))
                     for v in voices])

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        v.session.tts_voice = self.values[0]
        await v._next(interaction, "tts", f"켬 ({self.values[0]})")


class MemoryPlanView(_Step):
    def __init__(self, bot, session, channel):
        super().__init__(bot, session, channel)
        for key, plan in memory_plan.PLANS.items():
            self.add_item(PlanButton(key, plan["label"]))


class PlanButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.key = key

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        v.session.memory_plan = self.key
        await v._next(interaction, "memory",
                      memory_plan.PLANS[self.key]["label"])


class OpenTimeView(_Step):
    @discord.ui.button(label="⏱️ 시간 입력", style=discord.ButtonStyle.primary)
    async def ask(self, interaction, _b):
        # 디스플레이 채널의 채팅을 1회만 언락한다(기획 규정).
        self.session.awaiting_display_input = True
        display = self.bot.get_channel(getattr(self.session, "display_ch_id", 0))
        target = display or self.channel
        await interaction.response.send_message(
            f"{target.mention} 에 답해 주십시오." if display else "이 채널에 답해 주십시오.",
            ephemeral=True)
        if display:
            await display.send(
                "⏱️ **세션을 얼마나 유지하시겠습니까?**\n"
                "이 채널에 답해 주십시오. (예: `3시간`, `20턴`, `적당히`, `알아서`)")


async def _start_profile(bot, session, channel):
    """프로필 생성 단계로 넘긴다."""
    from . import profile_creation_ui

    uid = _owner(session)
    player = (session.players or {}).get(uid) or {}
    name = player.get("name") or "(이름 미정)"
    await profile_creation_ui.start(bot, session, uid, name, channel)


async def _offer_start_frames(bot, session, channel):
    """시작 상황 삼지선다."""
    from . import start_frame

    uid = _owner(session)
    player = (session.players or {}).get(uid) or {}
    profile = dict(player.get("profile") or {})

    options = start_frame.offer(session.scenario_data, profile)
    if not options:
        await channel.send("시작 상황 틀이 없어 기본 인트로로 진행합니다.")
        return

    lines = ["**시작 상황을 선택해 주십시오.**\n"]
    for i, opt in enumerate(options, 1):
        lines.append(start_frame.format_choice(i, opt))

    cog = bot.get_cog("SessionCog")
    view_cls = getattr(__import__("cogs.session", fromlist=["StartFrameView"]),
                       "StartFrameView")
    await channel.send("\n\n".join(lines),
                       view=view_cls(bot, session, options))


async def on_profile_done(bot, session, channel):
    """프로필 생성 완료 시 호출된다. 다음 단계로 진행한다."""
    creation.record(session, "profile", "완료")
    creation.advance(session)
    await render(bot, session, channel)


async def on_open_time_done(bot, session, channel):
    """유지 시간 확정 시 호출된다. 캐시를 올리고 시작 상황으로 넘어간다."""
    creation.record(session, "open", f"{getattr(session, 'open_minutes', 0)}분")

    cog = bot.get_cog("SessionCog")
    if cog:
        await cog.upload_cache(session, notify=channel.send)

    # 세션이 실제로 열렸다. 이 플래그가 없으면 턴 진행이 차단된다.
    # !시작 명령 경로에만 있어 버튼 플로우에서는 영원히 False였다.
    session.is_started = True

    creation.advance(session)
    from .io import save_session_data
    await save_session_data(bot, session)
    await render(bot, session, channel)
