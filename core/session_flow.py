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


# 세션 제작 화면은 메시지 하나를 고쳐 쓴다. 단계마다 새로 보내면
# 지난 UI가 남아 눌리고, 위아래로 스크롤해야 한다.
STEP_TITLES = {
    "private": "공개 설정",
    "intro": "소개",
    "scenario": "시나리오",
    "tts": "음성",
    "memory": "기억 방식",
    "profile": "캐릭터",
    "open": "세션 열기",
    "start": "시작",
}


def step_embed(session, title: str, desc: str, *, color: int = 0x5865F2):
    """단계 임베드. 진행도는 푸터 한 곳에만 표시한다."""
    e = discord.Embed(title=title, description=desc, color=color)

    # 화면이 있는 단계만 진행도에 센다. kind(세션 종류)는 GM 홈에서
    # 처리되고 done은 화면이 없으므로 제외한다.
    shown = [k for k in creation.STEP_ORDER if k in STEP_TITLES]
    step = creation.current_step(session)
    idx = shown.index(step) if step in shown else 0

    dots = "".join("●" if i <= idx else "○" for i in range(len(shown)))
    e.set_footer(text=f"{dots}   세션 준비 {idx + 1} / {len(shown)}")
    return e


async def show(bot, session, channel, embed, view=None, *, replace: bool = True):
    """제작 화면을 갱신한다. 기존 메시지가 있으면 고치고, 없으면 보낸다.

    replace=False면 새 메시지를 보낸다. 프로필 생성처럼 별도 흐름이
    이어지는 경우에 쓴다.
    """
    st = creation.get_state(session)
    mid = st.get("flow_msg_id") if replace else None

    if mid:
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(embed=embed, view=view, attachments=[])
            return msg
        except Exception:
            pass

    msg = await channel.send(embed=embed, view=view)
    st["flow_msg_id"] = msg.id
    return msg


async def close_flow_message(bot, session, channel, text: str = None):
    """제작 화면을 정리한다. 다음 흐름이 채널을 쓰기 전에 호출한다."""
    st = creation.get_state(session)
    mid = st.pop("flow_msg_id", None)
    if not mid:
        return
    try:
        msg = await channel.fetch_message(mid)
        if text:
            await msg.edit(embed=discord.Embed(
                description=text, color=0x5865F2), view=None, attachments=[])
        else:
            await msg.delete()
    except Exception:
        pass


async def render(bot, session, channel):
    """현재 단계 화면을 그린다.

    화면은 메시지 하나를 고쳐 쓴다. 진행은 버튼 콜백이 이어받는다.
    """
    step = creation.current_step(session)

    if step == "private":
        await show(bot, session, channel, step_embed(
            session, "공개 설정",
            "비공개로 여시겠습니까?\n"
            "비공개 세션은 참가자 외에는 채널을 볼 수 없습니다."),
            PrivateView(bot, session, channel))

    elif step == "intro":
        uid = _owner(session)
        level = intro_mod.judge_level(
            stats.load_stats(str(uid)), getattr(session, "scenario_id", ""))
        st = creation.get_state(session)
        st["intro_level"] = level

        # 첫 플레이는 건너뛰기 없이 전부, 경험자는 언제든 넘길 수 있다.
        view = IntroView(bot, session, channel, level)
        await show(bot, session, channel, view._embed(), view)

    elif step == "scenario":
        scenarios = _visible_scenarios(session)
        await show(bot, session, channel, step_embed(
            session, "시나리오",
            "어느 세계에서 시작하시겠어요?\n"
            "고르시면 배경과 예상 비용을 먼저 보여드릴게요."),
            ScenarioView(bot, session, channel, scenarios))

    elif step == "tts":
        await show(bot, session, channel, step_embed(
            session, "음성",
            "턴 묘사를 음성으로 들으시겠어요?\n\n"
            "켜시면 묘사 분량만큼 비용이 더 듭니다. "
            "언제든 끄실 수 있으니 편하게 정하셔도 돼요."),
            TTSView(bot, session, channel))

    elif step == "memory":
        e = step_embed(
            session, "기억 방식",
            "턴이 쌓이면 지난 일을 간추려 보관합니다.\n"
            "자주 간추릴수록 세부가 오래 남지만 그만큼 비용이 듭니다.\n"
            "**한 번 정하면 도중에 바꿀 수 없어요.**")
        for key, plan in memory_plan.PLANS.items():
            iv = plan.get("interval", 0)
            cycle = f"{iv}턴마다" if iv else "가변"
            e.add_field(
                name=f"{plan['label']} — {cycle}",
                value=f"{plan.get('desc', '')}\n비용: {plan.get('cost', '')}",
                inline=False)
        # 대략적 비용 증가 양상(기획 규정)
        curves = memory_plan.compare_curves(30)
        lines = [f"{memory_plan.PLANS[k]['label']} {v[9]}·{v[19]}·{v[29]}회"
                 for k, v in curves.items()]
        e.add_field(name="10 / 20 / 30턴 누적 압축", value=" · ".join(lines),
                    inline=False)
        await show(bot, session, channel, e,
                   MemoryPlanView(bot, session, channel))

    elif step == "profile":
        await close_flow_message(bot, session, channel)
        await _start_profile(bot, session, channel)

    elif step == "open":
        await show(bot, session, channel, step_embed(
            session, "세션 열기",
            "이제 세계를 불러올 차례입니다.\n"
            "얼마나 플레이하실 예정인가요?\n\n"
            "그 시간만큼 미리 받고, 일찍 닫으시면 남은 만큼 돌려드립니다.\n"
            "아래 버튼을 누르시면 디스플레이 채널에서 답하실 수 있어요."),
            OpenTimeView(bot, session, channel))

    elif step == "start":
        await close_flow_message(bot, session, channel)
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
        """선택을 기록하고 다음 단계로. 화면은 render가 같은 메시지를 고친다."""
        creation.record(self.session, field, value)
        creation.advance(self.session, to=to)
        from .io import save_session_data
        await save_session_data(self.bot, self.session)

        # 이 메시지를 제작 화면으로 등록해 두면 render가 이어서 고쳐 쓴다.
        st = creation.get_state(self.session)
        if interaction.message:
            st["flow_msg_id"] = interaction.message.id
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


class IntroView(_Step):
    """
    소개 — 강의처럼 설명하고 중간중간 확인한다(기획 규정).

    설명이 먼저 나오고 그 끝에서 다음을 물어야 한다. 질문이 앞서면
    무엇을 고르는지 모른 채 답하게 된다.

      [설명]
      … 여기까지 괜찮으세요?
      다음: 무엇을 적으면 되는지
      [계속 들을게요] [이건 알아요] [◀ 이전]

    첫 플레이는 건너뛰기를 주지 않는다(기획 규정).
    """

    def __init__(self, bot, session, channel, level, order=None):
        super().__init__(bot, session, channel)
        self.level = level
        self.order = list(order or intro_mod.QUESTION_ORDER)
        self.index = -1        # -1은 시작 전
        self.seen = []         # 실제로 본 항목
        self._bodies = {}
        self._pending_image = None
        self._build()

    @property
    def _key(self):
        if 0 <= self.index < len(self.order):
            return self.order[self.index]
        return None

    def _next_key(self):
        nxt = self.index + 1
        return self.order[nxt] if nxt < len(self.order) else None

    # ── 화면 ──
    def _embed(self) -> discord.Embed:
        if self.index < 0:
            e = discord.Embed(
                title="소개",
                description=intro_mod.greeting(self.level),
                color=0x5865F2)
            nxt = self.order[0]
            e.add_field(
                name="먼저 드릴 말씀",
                value=f"**{intro_mod.get_label(nxt)}** — {intro_mod.summary_of(nxt)}",
                inline=False)
            e.set_footer(text=f"모두 {len(self.order)}가지입니다.")
            return e

        key = self._key
        body = self._bodies.setdefault(key, intro_mod.get_body(key, self.level))

        sections = intro_mod.split_sections(body)
        e = discord.Embed(title=intro_mod.get_label(key), color=0x5865F2)
        if sections:
            e.description = sections[0][1]
            for _t, chunk in sections[1:]:
                e.add_field(name="\u200b", value=chunk, inline=False)
        else:
            e.description = body

        # 설명을 마무리하며 확인하고, 다음이 무엇인지 미리 알린다.
        tail = intro_mod.check_of(key)
        nxt = self._next_key()
        if nxt:
            lead = intro_mod.lead_of(key)
            tail = f"{tail}\n{lead}" if lead else tail
            tail += (f"\n\n▸ 다음 — **{intro_mod.get_label(nxt)}**"
                     f" ({intro_mod.summary_of(nxt)})")
        e.add_field(name="\u200b", value=tail, inline=False)

        # 미디어(기획 규정) — 지정된 이미지가 실제로 있을 때만 붙인다.
        sd = getattr(self.session, "scenario_data", {}) or {}
        fname = intro_mod.media_for(key, sd)
        self._pending_image = None
        if fname:
            import os
            path = os.path.join(
                f"media/{getattr(self.session, 'scenario_id', '') or ''}", fname)
            if os.path.exists(path):
                e.set_image(url=f"attachment://{os.path.basename(path)}")
                self._pending_image = path

        dots = "".join("●" if i <= self.index else "○"
                       for i in range(len(self.order)))
        e.set_footer(text=f"{dots}   {self.index + 1} / {len(self.order)}")
        return e

    def _build(self):
        self.clear_items()
        skippable = self.level != intro_mod.LEVEL_NEW

        if self.index < 0:
            self.add_item(IntroBtn("next", "▶ 들어볼게요",
                                   discord.ButtonStyle.primary))
            if skippable:
                self.add_item(IntroBtn("end", "모두 건너뛰기",
                                       discord.ButtonStyle.secondary))
            return

        last = self.index >= len(self.order) - 1
        self.add_item(IntroBtn(
            "prev", "◀ 이전", discord.ButtonStyle.secondary,
            disabled=self.index <= 0))
        self.add_item(IntroBtn(
            "next", "마치기" if last else "계속 들을게요",
            discord.ButtonStyle.primary))
        if skippable and not last:
            # 아는 내용이면 다음 것도 굳이 들을 필요가 없다.
            self.add_item(IntroBtn("end", "여기까지 알겠습니다",
                                   discord.ButtonStyle.secondary))

    # ── 조작 ──
    async def open(self, channel):
        self.message = await channel.send(embed=self._embed(), view=self)

    async def go(self, interaction, action: str):
        if action == "end":
            await self._close(interaction, "건너뜀")
            return
        if action == "next":
            if self.index >= len(self.order) - 1:
                await self._close(interaction, "완료")
                return
            self.index += 1
            if self._key not in self.seen:
                self.seen.append(self._key)
        elif action == "prev":
            self.index = max(0, self.index - 1)

        self._build()
        await self._edit(interaction)

    async def _edit(self, interaction):
        """화면 갱신. 이미지가 붙는 항목만 메시지를 새로 보낸다."""
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
            await interaction.message.edit(
                embed=embed, view=self, attachments=[])
        except Exception:
            try:
                await interaction.message.edit(embed=embed, view=self)
            except Exception:
                pass

    async def _close(self, interaction, note: str):
        self.clear_items()
        try:
            await interaction.message.edit(embed=discord.Embed(
                title="소개를 마쳤습니다",
                description="이제 세계를 고를 차례입니다.",
                color=0x5865F2), view=None, attachments=[])
        except Exception:
            pass
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await self._next(interaction, "intro", note)


class IntroBtn(discord.ui.Button):
    def __init__(self, action: str, label: str,
                 style=discord.ButtonStyle.secondary, disabled: bool = False):
        super().__init__(label=label, style=style, disabled=disabled)
        self.action = action

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.view.go(interaction, self.action)


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
        # 이 메시지를 이어서 고쳐 쓴다. 새로 보내면 지난 선택지가 남는다.
        creation.get_state(v.session)["flow_msg_id"] = interaction.message.id
        # 소개문은 자르지 않는다. 임베드 description은 4096자까지 담긴다.
        # 이전에는 600자에서 잘려 영도만 294자가 사라졌다.
        intro = (data.get("scenario_intro") or "").strip()

        e = discord.Embed(title=sid, color=0x5865F2)
        if len(intro) <= 4000:
            e.description = intro
        else:
            # 그래도 넘치면 문단 경계에서 나눠 필드로 잇는다.
            head, rest = intro[:2000], intro[2000:]
            cut = head.rfind("\n\n")
            if cut > 500:
                head, rest = intro[:cut], intro[cut:]
            e.description = head
            for i in range(0, len(rest), 1000):
                e.add_field(name="\u200b", value=rest[i:i + 1000], inline=False)

        # 가격 안내 — 오픈·유지비용과 턴 진행비용을 함께 제시한다(기획 규정).
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
            e.add_field(
                name="예상 비용",
                value=(f"세션 열기·3시간 유지  **{open3['total_ink']}잉크**\n"
                       f"턴 진행  **{cost_to_ink(turn_krw)}잉크** 안팎 "
                       f"(턴이 쌓일수록 완만히 증가)"),
                inline=False)
        except Exception as ex:
            print(f"[비용안내] 산출 실패: {ex}")

        e.set_footer(text="이 세계로 시작하시겠어요?")
        await show(v.bot, v.session, v.channel, e,
                   ScenarioConfirmView(v.bot, v.session, v.channel, sid, data))


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

    @discord.ui.button(label="◀ 다시 고르기", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _b):
        await interaction.response.defer()
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await render(self.bot, self.session, self.channel)


class TTSView(_Step):
    @discord.ui.button(label="🔊 사용", style=discord.ButtonStyle.primary)
    async def on(self, interaction, _b):
        await interaction.response.defer()
        self.session.tts_enabled = True
        # 같은 메시지를 목소리 선택 화면으로 바꾼다.
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await show(self.bot, self.session, self.channel, step_embed(
            self.session, "음성",
            "어떤 목소리로 읽어드릴까요?\n"
            "나중에 디스플레이 채널에서 바꾸실 수 있어요."),
            VoiceView(self.bot, self.session, self.channel))

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
        await channel.send(embed=discord.Embed(
            description="시작 상황 틀이 없어 기본 인트로로 진행합니다.",
            color=0x5865F2))
        return

    e = discord.Embed(
        title="어디에서 시작하시겠어요?",
        description="셋 중 하나를 고르시면 그 자리에서 이야기가 열립니다.",
        color=0x5865F2)
    for i, opt in enumerate(options, 1):
        e.add_field(name=f"{i}. {opt.get('title', '')}",
                    value=opt.get("summary", ""), inline=False)

    view_cls = getattr(__import__("cogs.session", fromlist=["StartFrameView"]),
                       "StartFrameView")
    await channel.send(embed=e, view=view_cls(bot, session, options))


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
