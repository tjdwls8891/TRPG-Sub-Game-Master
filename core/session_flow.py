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
import asyncio

import discord

from . import conversational as _conv
from . import creation
from . import intro as intro_mod
from . import master as _master
from . import master_art as _master_art
from . import voice_profile as _voice
from . import memory_plan
from . import profiles as profile_store
from . import stats
from . import talk as _talk
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


async def show(bot, session, channel, embed, view=None, *, replace: bool = True, master_key: str | None = None,
               art: bool = True):
    """제작 화면을 갱신한다. 기존 메시지가 있으면 고치고, 없으면 보낸다.

    replace=False면 새 메시지를 보낸다. 프로필 생성처럼 별도 흐름이
    이어지는 경우에 쓴다.
    P2(#4): 마스터가 말을 거는 화면이라 일반 메시지(content)로 보낸다 — 이름 있는 구조 필드만 데이터 카드로 남는다
    (core.conversational). P2(#7): 마스터 이미지 슬롯(세션 진행자 성별·대사 톤)을 같은 메시지에 붙인다 —
    원화 미제작이면 공용 로딩 이미지(의도된 임시 표시, core.master_art).
    """
    st = creation.get_state(session)
    mid = st.get("flow_msg_id") if replace else None
    # v7.37(D2): 마스터 이미지는 대화 메시지 위의 전용 메시지 — 대화 메시지는 글만 고쳐 쓴다
    art_store = st.setdefault("flow_art", {})

    if mid:
        try:
            msg = await channel.fetch_message(mid)
            await msg.edit(**_conv.edit_kwargs(embed, view=view))
            if art:
                await _master_art.art_above(channel, session, master_key, art_store, create=False)
            return msg
        except Exception:
            pass

    await _master_art.drop_art(channel, art_store)                   # 대화 메시지를 새로 만들 때는 이미지도 그 위에 새로
    await _master_art.art_above(channel, session, master_key, art_store)
    msg = await channel.send(**_conv.send_kwargs(embed, view=view))
    st["flow_msg_id"] = msg.id
    return msg


async def close_flow_message(bot, session, channel, text: str = None):
    """제작 화면을 정리한다. 다음 흐름이 채널을 쓰기 전에 호출한다."""
    st = creation.get_state(session)
    mid = st.pop("flow_msg_id", None)
    await _master_art.drop_art(channel, st.setdefault("flow_art", {}))
    if not mid:
        return
    try:
        msg = await channel.fetch_message(mid)
        if text:
            await msg.edit(content=text, embeds=[], view=None, attachments=[])
        else:
            await msg.delete()
    except Exception:
        pass


# ── v7.36 대화 박자(사용자 결정: 메시지 하나를 고쳐 씀) ──
# 설명 박자는 응답 버튼(무엇을 눌러도 다음 박자 — 결정 아님), 마지막 박자에 그 단계의 기존 결정 UI.
# 문구는 마스터 대사 데이터가 [[답]] 표기로 박자를 끊는다. 아래는 데이터를 읽지 못할 때의 같은 구조 대체 문구.
PRIVATE_TEXT = ("먼저 이 세션을 다른 분들께 보일지 정하겠습니다.\n[[네.]]\n"
                "비공개로 하시면 참가자 외에는 채널을 볼 수 없습니다. 어떻게 하시겠습니까?")
SCENARIO_TEXT = ("이제 들어가실 세계를 고를 차례입니다.\n[[네.|어떤 세계가 있죠?]]\n"
                 "고르시면 배경과 예상 비용을 먼저 보여 드리겠습니다.")
TTS_TEXT = ("장면을 제 목소리로 들으실 수 있습니다.\n[[그래요?|네.]]\n"
            "켜시면 묘사 분량만큼 비용이 조금 더 듭니다. 언제든 끄실 수 있으니 편하게 정하십시오.")
VOICE_TEXT = ("제 목소리와 모습을 정해 주십시오. 남성·여성 어느 쪽이든 같은 진행자입니다.\n"
              "나중에 디스플레이 채널에서 개설자가 바꾸실 수 있습니다.")
MEMORY_TEXT = ("턴이 쌓이면 지난 일을 간추려 보관해 둡니다.\n[[네.|그렇군요.]]\n"
               "자주 간추릴수록 세부가 오래 남습니다. 간추리는 비용은 청구하지 않습니다.\n[[좋네요.]]\n"
               "**한 번 정하시면 도중에 바꿀 수 없습니다.** 어떻게 하시겠습니까?")
OPEN_TEXT = ("이제 세계를 불러올 차례입니다.\n[[네.]]\n"
             "머무르실 시간만큼 미리 받고, 일찍 닫으시면 남은 만큼 돌려드립니다.\n[[알겠습니다.]]\n"
             "얼마나 머무르실 생각이십니까? 이 채널에 바로 답해 주십시오.")
OPEN_EXAMPLES = "-# 예: 3시간 · 90분 · 20턴 · 적당히 · 알아서"
START_TEXT = ("이야기가 시작될 자리를 정하겠습니다.\n[[네.|어디서 시작하죠?]]\n"
              "셋 중 하나를 고르시면 그 자리에서 이야기가 열립니다.")
GREETING_TEXT = ("어서 오십시오.\n[[네.|안녕하세요.]]\n"
                 "이곳에서 이야기를 준비하겠습니다. 몇 가지를 정하고 인물을 세운 뒤 막을 올리겠습니다.\n[[좋습니다.]]")
PROFILE_BEGIN_TEXT = "이제 이 세계에 들어설 인물을 세우겠습니다.\n[[네.|좋습니다.]]"


def _talk_store(session) -> dict:
    """박자를 시작한 단계 표식(영속 creation_state) — 재시작 뒤에는 결정 박자부터 그린다."""
    return creation.get_state(session).setdefault("talk_started", {})


def greeting_text(mention: str, voice_mention: str | None) -> str:
    """첫 인사 박자(공개 설정 단계 앞에 붙는다)."""
    body = _master.say("session.first_game_greeting", GREETING_TEXT)
    out = f"{mention} 님, {body}"
    if voice_mention:
        out += (f"\n음성 채널 {voice_mention} 에 들어오시면 배경음과 제 목소리가 함께 나옵니다. "
                "지금이든 나중이든 편하실 때 오십시오.\n[[알겠습니다.|나중에 갈게요.]]")
    return out


async def talk_step(bot, session, channel, key, text, make_embed, final_view, *, master_key=None,
                    final_fn=None):
    """단계 하나를 박자로 그린다 — 같은 제작 메시지를 고쳐 쓰며 스트리밍, 마지막 박자에 final_view.

    make_embed(body, final) → Embed. final_fn(body)가 있으면 마지막 박자는 호출부가 직접 처리한다."""
    async def draw(body, *, final, view, partial, first):
        if final and final_fn is not None:
            await final_fn(body)
            return
        await show(bot, session, channel, make_embed(body, final), final_view if final else view,
                   master_key=master_key, art=first)

    await _talk.run(session, key, text, draw, check=lambda i: owner_only(bot, session, i),
                    started=_talk_store(session))


async def render(bot, session, channel):
    """현재 단계 화면을 그린다.

    화면은 메시지 하나를 고쳐 쓴다. 진행은 버튼 콜백이 이어받는다.
    """
    step = creation.current_step(session)

    if step == "private":
        st = creation.get_state(session)
        text = _master.say("session.private_select", PRIVATE_TEXT)
        if st.get("greeting"):                         # 첫 인사 박자(게임 채널 첫 화면)
            text = st["greeting"] + "\n" + text
        await talk_step(bot, session, channel, "private", text,
                        lambda body, _f: step_embed(session, "공개 설정", body),
                        PrivateView(bot, session, channel), master_key="session.private_select")

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
        await talk_step(bot, session, channel, "scenario", _master.say("session.scenario_select", SCENARIO_TEXT),
                        lambda body, _f: step_embed(session, "시나리오", body),
                        ScenarioView(bot, session, channel, scenarios), master_key="session.scenario_select")

    elif step == "tts":
        await talk_step(bot, session, channel, "tts", _master.say("session.tts_select", TTS_TEXT),
                        lambda body, _f: step_embed(session, "음성", body),
                        TTSView(bot, session, channel), master_key="session.tts_select")

    elif step == "memory":
        def _memory_embed(body, final):
            e = step_embed(session, "기억 방식", body)
            if not final:
                return e
            for key, plan in memory_plan.PLANS.items():
                iv = plan.get("interval", 0)
                cycle = f"{iv}턴마다" if iv else "가변"
                e.add_field(
                    name=f"{plan['label']} — {cycle}",
                    value=f"{plan.get('desc', '')}",   # WP-G D3: 압축 비용은 운영자 부담 — 비용 표기 없음
                    inline=False)
            # 플랜별 누적 압축 횟수(기획 규정)
            curves = memory_plan.compare_curves(30)
            lines = [f"{memory_plan.PLANS[k]['label']} {v[9]}·{v[19]}·{v[29]}회"
                     for k, v in curves.items()]
            e.add_field(name="10 / 20 / 30턴 누적 압축", value=" · ".join(lines), inline=False)
            return e
        await talk_step(bot, session, channel, "memory", _master.say("session.memory_select", MEMORY_TEXT),
                        _memory_embed, MemoryPlanView(bot, session, channel), master_key="session.memory_select")

    elif step == "profile":
        async def _begin(_body):
            await close_flow_message(bot, session, channel)
            await _start_profile(bot, session, channel)
        await talk_step(bot, session, channel, "profile", _master.say("profile.begin", PROFILE_BEGIN_TEXT),
                        lambda body, _f: step_embed(session, "캐릭터", body), None,
                        master_key="profile.begin", final_fn=_begin)

    elif step == "open":
        async def _ask(body):
            await ask_open_time(bot, session, channel, body)
        await talk_step(bot, session, channel, "open", _master.say("session.open_time", OPEN_TEXT),
                        lambda body, _f: step_embed(session, "세션 열기", body), None,
                        master_key="cache.time_question", final_fn=_ask)

    elif step == "start":
        async def _frames(body):
            await _offer_start_frames(bot, session, channel, lead=body)
        await talk_step(bot, session, channel, "start", _master.say("session.start_frame_select", START_TEXT),
                        lambda body, _f: step_embed(session, "시작", body), None,
                        master_key="session.start_frame_select", final_fn=_frames)


def _owner(session):
    """세션 개설자 uid. players가 비어 있으면 빈 문자열."""
    for uid in (session.players or {}):
        return uid
    return getattr(session, "creator_uid", "") or ""


def _visible_scenarios(session) -> list:
    """선택 가능한 시나리오. 블락 시나리오는 마스터 세션에만 노출한다.

    시작 상황 틀(`start_frames`)이 없는 시나리오는 버튼 플로우로 인트로·첫 라운드에 닿지 못하므로
    목록에서 뺀다(2026-10-13 사용자 결정 D4). 운영자 `!새세션` 경로는 그대로다.
    """
    master = getattr(session, "session_kind", "solo") == "master"
    out = []
    for sid in get_available_scenarios():
        data = load_scenario_from_file(sid) or {}
        if data.get("blocked") and not master:
            continue
        if not data.get("start_frames"):
            continue
        out.append(sid)
    return out


def flow_owner(session) -> str:
    """세션 플로우를 조작할 수 있는 사람 = 세션 개설자(버튼 플로우가 기록한 creator_uid)."""
    return str(getattr(session, "creator_uid", "") or _owner(session) or "")


async def owner_only(bot, session, interaction) -> bool:
    """개설자(또는 봇 오너)만 통과. 다른 사람에게는 ephemeral 안내(사용자 결정 D5)."""
    uid = flow_owner(session)
    if not uid or str(interaction.user.id) == uid or await bot.is_owner(interaction.user):
        return True
    await interaction.response.send_message(f"이 선택은 <@{uid}>님만 할 수 있습니다.", ephemeral=True)
    return False


# ── v7.28 진행 대기 버튼의 승계 ─────────────────────────────
# 세션 생성 단계 화면·시작 상황·초기 지식·열기/업로드 재시도는 대답해야 다음으로 넘어가는 화면이라 시간 제한이 없다.
# 봇이 다시 시작되면 메모리의 버튼은 사라지므로, 저장된 기록으로 같은 메시지에 새 버튼을 붙인다(resume_all).
#   · 생성 단계 화면: creation_state(단계·flow_msg_id)로 render를 다시 부른다(소개 단계는 처음 항목부터).
#   · 그 밖의 대기 화면: resumable_prompts {종류: {channel_id, message_id, ...복원 근거}} — 처리되면 지운다.
PROMPT_START_FRAMES = "start_frames"
PROMPT_INITIAL_KNOWLEDGE = "initial_knowledge"
PROMPT_OPEN_RETRY = "open_retry"
PROMPT_UPLOAD_RETRY = "upload_retry"
RESUMABLE_STEPS = ("private", "intro", "scenario", "tts", "memory", "open")


async def remember_prompt(bot, session, kind: str, channel, message, **data) -> None:
    """대기 버튼 화면의 위치·복원 근거를 세션에 남긴다(같은 종류는 최신 하나)."""
    if message is None or getattr(message, "id", None) is None:
        return
    ch = getattr(message, "channel", None) or channel
    prompts = dict(getattr(session, "resumable_prompts", None) or {})
    prompts[kind] = dict(data, channel_id=getattr(ch, "id", None), message_id=message.id)
    session.resumable_prompts = prompts
    from .io import save_session_data
    try:
        await save_session_data(bot, session)
    except Exception as e:  # noqa: BLE001 — 기록 실패는 재시작 승계만 잃는다(진행은 막지 않음)
        print(f"[세션플로우] 대기 화면 기록 저장 실패: {e}")


def forget_prompt(session, kind: str) -> bool:
    """대기 화면이 처리됐다 — 기록을 지운다. 있었으면 True(같은 화면의 중복 처리 방지에 쓴다)."""
    prompts = dict(getattr(session, "resumable_prompts", None) or {})
    had = prompts.pop(kind, None) is not None
    session.resumable_prompts = prompts
    return had


def _prompt_view(bot, session, kind, rec, channel):
    import cogs.session as _sm
    if kind == PROMPT_START_FRAMES:
        return _sm.StartFrameView(bot, session, list(rec.get("options") or ()))
    if kind == PROMPT_INITIAL_KNOWLEDGE:
        return _sm.InitialKnowledgeView(bot, session, rec.get("uid"), rec.get("phase"), rec.get("state"))
    if kind == PROMPT_OPEN_RETRY:
        return _sm.DynamicOpenRetryView(bot, session)
    if kind == PROMPT_UPLOAD_RETRY:
        return UploadRetryView(bot, session, channel, rec.get("quoted_tokens"))
    return None


async def resume_session(bot, session) -> int:
    """한 세션의 대기 버튼 화면을 다시 붙인다. 붙인 화면 수."""
    n = 0
    step = creation.current_step(session) if getattr(session, "creation_state", None) else None
    if step in RESUMABLE_STEPS and creation.get_state(session).get("flow_msg_id"):
        ch = bot.get_channel(getattr(session, "game_ch_id", None) or 0)
        if ch is not None:
            _talk_store(session)[step] = True          # v7.36: 재시작 뒤에는 설명 박자 없이 결정 박자부터
            await render(bot, session, ch)
            n += 1
    prompts = dict(getattr(session, "resumable_prompts", None) or {})
    dropped = False
    for kind, rec in prompts.items():
        ch = bot.get_channel(rec.get("channel_id") or 0)
        view = None
        try:
            msg = await ch.fetch_message(rec["message_id"]) if ch is not None else None
            view = _prompt_view(bot, session, kind, rec, ch) if msg is not None else None
            if view is not None:
                await msg.edit(view=view)
                n += 1
        except Exception as e:  # noqa: BLE001 — 지워진 메시지 등: 기록만 정리
            print(f"[세션플로우] 대기 화면 복원 실패({kind}): {e}")
            view = None
        if view is None:
            forget_prompt(session, kind)
            dropped = True
    if dropped:
        from .io import save_session_data
        await save_session_data(bot, session)
    return n


async def resume_all(bot) -> int:
    """봇 재시작 뒤 — 활성 세션들의 대기 버튼 화면을 같은 메시지에 다시 붙인다."""
    n = 0
    for session in {id(s): s for s in (getattr(bot, "active_sessions", {}) or {}).values()}.values():
        try:
            n += await resume_session(bot, session)
        except Exception as e:  # noqa: BLE001
            print(f"[세션플로우] 세션 {getattr(session, 'session_id', '?')} 화면 복원 실패: {e}")
    return n


async def continue_after_initial_knowledge(bot, session, channel, phase) -> None:
    """초기 지식 대기 해소 뒤 멈췄던 단계를 저장된 상태로 이어 간다(재시작 뒤에도 같은 결과)."""
    if phase == "PROFILE" and creation.current_step(session) == "profile":
        await on_profile_done(bot, session, channel)      # 캐릭터 승인 직후 멈춘 경우
        return
    cog = bot.get_cog("SessionCog")                        # 시작 상황 선택 뒤(세계 초기화 전) 멈춘 경우
    if cog:
        await cog.play_intro(session, getattr(session, "start_frame", None) or {}, channel)


# ── 단계별 뷰 ────────────────────────────────────────────
class _Step(discord.ui.View):
    def __init__(self, bot, session, channel, timeout=None):     # v7.28: 진행 화면은 시간 제한 없음
        super().__init__(timeout=timeout)
        self.bot = bot
        self.session = session
        self.channel = channel

    async def interaction_check(self, interaction) -> bool:
        return await owner_only(self.bot, self.session, interaction)

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
        self.beat = 0          # v7.36: 주제 본문 안의 박자(대화 박자 — [[답]] 표기)
        self.seen = []         # 실제로 본 항목
        self._bodies = {}
        self._pending_image = None
        self.partial = None
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
        beats = _talk.parse(body)
        self.beat = min(self.beat, len(beats) - 1)
        beat = beats[self.beat]
        last_beat = self.beat >= len(beats) - 1
        echo = _talk.echo(self.session, f"intro:{key}")

        sections = intro_mod.split_sections(beat.text)
        e = discord.Embed(title=intro_mod.get_label(key), color=0x5865F2)
        # v7.13 마스터 대사 — 주제 도입 한 줄(본문은 그대로). 이전/다음 재렌더에도 같은 줄을 쓴다.
        lines = self.__dict__.setdefault("_master_lines", {})
        if key not in lines:
            lines[key] = _master.line(f"intro.topic_{key}") or ""
        head = f"*{lines[key]}*\n\n" if (lines[key] and self.beat == 0) else ""
        head = echo + head
        if self.partial is not None:                  # 스트리밍 중(부분 본문)
            e.description = head + self.partial + " ▍"
            return e
        if sections:
            e.description = head + sections[0][1]
            for _t, chunk in sections[1:]:
                e.add_field(name="\u200b", value=chunk, inline=False)
        else:
            e.description = head + body

        if not last_beat:                             # 주제 중간 박자 — 확인·다음 안내·이미지는 마지막 박자에만
            dots = "".join("●" if i <= self.index else "○" for i in range(len(self.order)))
            e.set_footer(text=f"{dots}   {self.index + 1} / {len(self.order)}")
            self._pending_image = None
            return e

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
                self.add_item(IntroBtn("end", "모든 설명 건너뛰기",
                                       discord.ButtonStyle.secondary))
            return

        beats = _talk.parse(self._bodies.setdefault(self._key, intro_mod.get_body(self._key, self.level)))
        if self.beat < len(beats) - 1:                # 중간 박자 — 응답(무엇을 눌러도 다음 박자) + 건너뛰기
            for a in _talk.answers_for(beats[self.beat], f"intro:{self._key}", str(self.session.session_id)):
                self.add_item(IntroBtn("ack", a, discord.ButtonStyle.secondary, said=a))
            if skippable:
                self.add_item(IntroBtn("end", "모든 설명 건너뛰기", discord.ButtonStyle.secondary))
            return

        last = self.index >= len(self.order) - 1
        self.add_item(IntroBtn(
            "prev", "◀ 이전", discord.ButtonStyle.secondary,
            disabled=self.index <= 0))
        self.add_item(IntroBtn(
            "next", "마치기" if last else "계속 들을게요",
            discord.ButtonStyle.primary))
        if skippable and not last:
            self.add_item(IntroBtn("end", "모든 설명 건너뛰기",
                                   discord.ButtonStyle.secondary))

    # ── 조작 ──
    async def open(self, channel):
        self.message = await channel.send(**_conv.send_kwargs(self._embed(), view=self))

    async def go(self, interaction, action: str, said: str | None = None):
        if action == "end":
            await self._close(interaction, "건너뜀")
            return
        if action == "ack":
            who = getattr(getattr(interaction, "user", None), "display_name", None) or "플레이어"
            _talk.answer(self.session, f"intro:{self._key}", who, said or "")
            self.beat += 1
        elif action == "next":
            if self.index >= len(self.order) - 1:
                await self._close(interaction, "완료")
                return
            self.index += 1
            self.beat = 0
            _talk.reset(self.session, f"intro:{self._key}")
            if self._key not in self.seen:
                self.seen.append(self._key)
        elif action == "prev":
            self.index = max(0, self.index - 1)
            self.beat = 0
            _talk.reset(self.session, f"intro:{self._key}")

        await self._stream(interaction)
        self._build()
        await self._edit(interaction)

    async def _stream(self, interaction):
        """v7.36 박자 스트리밍 — 같은 메시지에 부분 본문을 몇 번 고쳐 쓴 뒤 전체 + 버튼."""
        if _talk.TICK <= 0 or self._key is None:
            return
        beats = _talk.parse(self._bodies.setdefault(self._key, intro_mod.get_body(self._key, self.level)))
        text = beats[min(self.beat, len(beats) - 1)].text
        try:
            for part in _talk.partials(text):
                self.partial = part
                await interaction.message.edit(**_conv.edit_kwargs(self._embed(), view=None))
                await asyncio.sleep(_talk.TICK)
        except Exception:  # noqa: BLE001 — 스트리밍은 연출: 실패하면 전체로 바로
            pass
        finally:
            self.partial = None

    async def _edit(self, interaction):
        """화면 갱신. 이미지가 붙는 항목만 메시지를 새로 보낸다."""
        embed = self._embed()
        if self._pending_image:
            try:
                await interaction.message.delete()
            except Exception:
                pass
            self.message = await self.channel.send(
                **_conv.send_kwargs(embed, view=self, file=discord.File(self._pending_image)))
            return
        try:
            await interaction.message.edit(**_conv.edit_kwargs(embed, view=self, attachments=[]))
        except Exception:
            try:
                await interaction.message.edit(**_conv.edit_kwargs(embed, view=self))
            except Exception:
                pass

    async def _close(self, interaction, note: str):
        self.clear_items()
        try:
            await interaction.message.edit(content="**소개를 마쳤습니다**\n이제 세계를 고를 차례입니다.",
                                           embeds=[], view=None, attachments=[])
        except Exception:
            pass
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await self._next(interaction, "intro", note)


class IntroBtn(discord.ui.Button):
    def __init__(self, action: str, label: str,
                 style=discord.ButtonStyle.secondary, disabled: bool = False, said: str | None = None):
        super().__init__(label=label, style=style, disabled=disabled)
        self.action = action
        self.said = said

    async def callback(self, interaction):
        await interaction.response.defer()
        await self.view.go(interaction, self.action, self.said)


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
        _talk.reset(v.session, f"scenario_confirm:{sid}", _talk_store(v.session))   # 고를 때마다 소개를 처음부터
        await show_scenario_confirm(v.bot, v.session, v.channel, sid, data)


def _cost_field(data):
    """가격 안내 — 오픈·유지비용과 턴 진행비용을 함께 제시한다(기획 규정). 실패하면 None."""
    try:
        from .estimate import estimate_session_open, DEFAULT_BASELINE, approx_cache_tokens
        from .ink import cost_to_ink
        from .cost import calculate_text_gen_cost_breakdown
        from .constants import DEFAULT_MODEL

        probe = type("P", (), {"cache_tokens": 0, "cache_read_tokens": 0})()
        # 룰북 분량으로 캐시 토큰을 근사한다(실제 값은 업로드 시 확정).
        # P1: 세션 열기 예상(estimate_session_open)과 같은 근사(approx_cache_tokens — 캐시에 실리는 항목 전부·
        #     실측 계수 0.33)를 쓴다. 이전의 worldview × 0.65는 같은 양을 다른 산식으로 셌다.
        rough = approx_cache_tokens(data) or int(len(str(data.get("worldview", ""))) * 0.33)
        probe.cache_tokens = rough
        open3 = estimate_session_open(probe, 3.0)
        turn_krw = calculate_text_gen_cost_breakdown(
            DEFAULT_MODEL, input_tokens=rough + rough // 3,   # P1: 단가 함수의 입력은 캐시분 포함 총량
            output_tokens=DEFAULT_BASELINE["narration_out"],
            cached_read_tokens=rough)["total_krw"]
        return (f"세션 열기·3시간 유지  **{open3['total_ink']}잉크**\n"
                f"턴 진행  **{cost_to_ink(turn_krw)}잉크** 안팎 "
                f"(턴이 쌓일수록 완만히 증가)")
    except Exception as ex:
        print(f"[비용안내] 산출 실패: {ex}")
        return None


async def show_scenario_confirm(bot, session, channel, sid: str, data: dict):
    """시나리오 소개문(박자 — 저작 [[ ]] 또는 문단 자동 분할) → 확인 질문 + 예상 비용 카드 + 확인 버튼."""
    intro = _talk.auto_split((data.get("scenario_intro") or "").strip())
    question = _master.say("session.scenario_confirm", "이 세계로 들어가시겠습니까?")
    text = f"{intro}\n[[]]\n{question}" if intro else question
    cost = _cost_field(data)

    def _embed(body, final):
        e = discord.Embed(title=sid, description=body, color=0x5865F2)
        if final and cost:
            e.add_field(name="예상 비용", value=cost, inline=False)
        return e

    await talk_step(bot, session, channel, f"scenario_confirm:{sid}", text, _embed,
                    ScenarioConfirmView(bot, session, channel, sid, data), master_key="session.scenario_confirm")


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
        # 임시 시나리오로 전개된 NPC를 걷고 새로 전개한다.
        # 이것이 없으면 다크판타지 인물 21명이 영도 세션에 남아
        # 추출층위 유효 검증과 캐시 조립에 섞인다.
        try:
            self.session.expand_default_npcs(replace=True)
        except Exception as e:
            print(f"[세션플로우] NPC 재전개 실패: {e}")
        await self._next(interaction, "scenario", self.sid)

    @discord.ui.button(label="◀ 다시 고르기", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _b):
        await interaction.response.defer()
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await render(self.bot, self.session, self.channel)


class TTSView(_Step):
    async def _choose(self, interaction, on: bool):
        await interaction.response.defer()
        from .media_control import sync_tts_flag
        sync_tts_flag(self.session, on)            # P2(#9): 설정 게이트 두 곳을 함께(어긋남 방지)
        # 같은 메시지를 진행자(마스터) 성별 선택 화면으로 바꾼다 — 이미지·목소리 모두에 쓰이므로 TTS와 무관하게 묻는다.
        creation.get_state(self.session)["flow_msg_id"] = interaction.message.id
        await talk_step(self.bot, self.session, self.channel, "voice", _master.say("session.voice_select", VOICE_TEXT),
                        lambda body, _f: step_embed(self.session, "진행자", body),
                        MasterGenderView(self.bot, self.session, self.channel, on), master_key="session.voice_select")

    @discord.ui.button(label="🔊 사용", style=discord.ButtonStyle.primary)
    async def on(self, interaction, _b):
        await self._choose(interaction, True)

    @discord.ui.button(label="사용 안 함", style=discord.ButtonStyle.secondary)
    async def off(self, interaction, _b):
        await self._choose(interaction, False)


class MasterGenderView(_Step):
    """P2 Q2: 세션 개설자가 진행자 성별(이미지·목소리 표현)을 정한다 — 세션 단위·영속(master_gender)."""

    def __init__(self, bot, session, channel, tts_on: bool):
        super().__init__(bot, session, channel)
        self.tts_on = tts_on
        for g in _voice.MASTER_GENDERS:
            self.add_item(MasterGenderButton(g))


class MasterGenderButton(discord.ui.Button):
    def __init__(self, gender: str):
        super().__init__(label=f"{_voice.GENDER_LABEL[gender]} 진행자", style=discord.ButtonStyle.primary)
        self.gender = gender

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        v.session.master_gender = self.gender
        await v._next(interaction, "tts", f"{'켬' if v.tts_on else '끔'} · {_voice.GENDER_LABEL[self.gender]} 진행자")


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


def awaiting_open_here(session, channel_id) -> bool:
    """세션 첫 생성의 유지 시간 답변을 이 채널(게임 채널)에서 기다리는가(v7.21.4).

    대기 플래그는 디스플레이 재오픈과 같은 `awaiting_display_input`(1회 소비·재질문 시만 재언락)이고,
    답변 채널 표식 `_open_input_ch_id`는 런타임 값이다(재시작 뒤에는 [시간 입력]을 다시 누르면 된다).
    """
    return bool(getattr(session, "awaiting_display_input", False)) and \
        getattr(session, "_open_input_ch_id", None) == channel_id


async def ask_open_time(bot, session, channel, body: str):
    """v7.37(R1): 세션 첫 생성의 유지 시간 — 대화 메시지가 묻고 이 채널의 다음 답을 바로 받는다(버튼 단계 없음).
    답 처리(gm._handle_open_time_input)가 같은 대화 메시지에 해석·확인을 이어 쓴다."""
    await show(bot, session, channel, step_embed(session, "세션 열기", f"{body}\n{OPEN_EXAMPLES}"), None,
               master_key="cache.time_question", art=False)
    session.awaiting_display_input = True
    session._open_input_ch_id = channel.id


def open_in_flow(session, channel_id) -> bool:
    """이 답이 세션 첫 생성 대화(게임 채널 제작 메시지)의 유지 시간 답인가."""
    return (creation.current_step(session) == "open" and bool(creation.get_state(session).get("flow_msg_id"))
            and channel_id == getattr(session, "game_ch_id", None))


async def open_feedback(bot, session, channel, text: str, view=None):
    """유지 시간 해석·재질문·확인을 같은 대화 메시지에 쓴다(새 메시지 없음)."""
    return await show(bot, session, channel, step_embed(session, "세션 열기", text), view,
                      master_key="cache.time_question", art=False)


async def _start_profile(bot, session, channel):
    """프로필 생성 단계로 넘긴다."""
    from . import profile_creation_ui

    uid = _owner(session)
    player = (session.players or {}).get(uid) or {}
    name = player.get("name") or "(이름 미정)"
    await profile_creation_ui.start(bot, session, uid, name, channel, begun=True)


async def _offer_start_frames(bot, session, channel, lead: str | None = None):
    """시작 상황 삼지선다. v7.36: 박자 대화를 이어 온 제작 메시지가 있으면 그 메시지를 선택 화면으로 고쳐 쓴다."""
    from . import start_frame

    uid = _owner(session)
    player = (session.players or {}).get(uid) or {}
    profile = dict(player.get("profile") or {})

    options = start_frame.offer(session.scenario_data, profile)
    if not options:
        await close_flow_message(bot, session, channel)
        await channel.send("시작 상황 틀이 없어 기본 인트로로 진행합니다.")
        # H06: 캐시를 연기했던 세션도 시작 선택이 없으면 기존(LEGACY) 의미로 지금 연다.
        cog = bot.get_cog("SessionCog")
        if cog and not getattr(session, "cache_name", None):
            await cog.upload_cache(session, quoted_tokens=getattr(session, "open_quoted_tokens", None))
        return

    e = discord.Embed(
        title="어디에서 시작하시겠어요?",
        description=lead if lead is not None else _talk.plain(_master.say("session.start_frame_select", START_TEXT)),
        color=0x5865F2)
    for i, opt in enumerate(options, 1):
        e.add_field(name=f"{i}. {opt.get('title', '')}",
                    value=opt.get("summary", ""), inline=False)

    view_cls = getattr(__import__("cogs.session", fromlist=["StartFrameView"]),
                       "StartFrameView")
    # v7.27: 세 선택지는 카드 이미지 한 장(번호 = 버튼 번호), 선택은 기존 버튼. 렌더 실패면 기존 목록 + 마스터 원화.
    from . import gm_board as _gb
    card, _k = await _gb.png_file(_gb.build_frames_svg(_gb.frames_data(options)), "start_frames.png")
    view = view_cls(bot, session, options)
    flow = None
    mid = creation.get_state(session).pop("flow_msg_id", None)
    if mid:
        try:
            flow = await channel.fetch_message(mid)
        except Exception:  # noqa: BLE001
            flow = None
    if card is not None:
        kw = {"content": f"**{e.title}**\n\n{e.description}", "view": view}
        if flow is not None:
            await flow.edit(embeds=[], attachments=[card], **kw)
            msg = flow
        else:
            msg = await channel.send(file=card, **kw)
    else:
        mfile, _p, _st = _master_art.file_for(session, "session.start_frame_select")
        if flow is not None:
            await flow.edit(**_conv.edit_kwargs(e, view=view, attachments=[mfile]))
            msg = flow
        else:
            msg = await channel.send(**_conv.send_kwargs(e, view=view, file=mfile))
    await remember_prompt(bot, session, PROMPT_START_FRAMES, channel, msg, options=list(options))


async def initial_knowledge_gate(bot, session, channel, phase, *, frame=None, on_resolved=None) -> bool:
    """v7.17 초기 지식 한 시점 — True면 다음 단계로 간다. DYNAMIC 후보(단일 PC·필수 capability)만 대상이고
    LEGACY·후보 아님은 아무것도 하지 않는다(호출 0). 미확정·재시도 상태면 개설자 버튼을 띄우고 False."""
    from . import initial_knowledge as _ik
    from . import world_lifecycle as _wl
    definition, _why = _wl.compile_candidate(session)
    uids = _wl.player_uids(session)
    if definition is None or len(uids) != 1:
        return True
    frames = (definition.get("initialization_definitions") or {}).get("start_frames") or ()
    if phase == "START" and _wl.resolve_start_choices(definition, frame)[0] is None:
        return True                                   # 이 틀은 genesis가 없다(LEGACY 시작) — 인지할 곳이 없으므로 생략
    if phase == "PROFILE" and not any(f.get("world_start") for f in frames):
        return True
    shown = []

    async def _cont():                                # v7.37(D1): 마지막 박자를 그린 뒤 멈췄던 단계를 이어 간다
        shown.append(True)
        if on_resolved is not None:
            await on_resolved()
        else:
            await continue_after_initial_knowledge(bot, session, channel, phase)
    st = await _ik.run_phase(bot, session, uids[0], phase, channel, definition, frame=frame, on_delivered=_cont)
    if shown:
        return False                                  # 박자 마지막에서 이미 이어 갔다
    if st in _ik.DONE:
        return True
    if st == _ik.PRESENTING:
        return False                                  # 대화 박자 진행 중 — 마지막 박자가 이어 간다
    try:
        view_cls = getattr(__import__("cogs.session", fromlist=["InitialKnowledgeView"]), "InitialKnowledgeView")
        if st == _ik.UNCERTAIN:
            text = ("⏸️ 초기 안내가 전달됐는지 확인하지 못했습니다. 위에 안내가 보이지 않으면 [다시 보내기]를, "
                    "보였거나 건너뛰려면 [보내지 않고 진행]을 눌러 주십시오. (다시 보내면 같은 안내가 두 번 보일 수 있습니다)")
        else:
            text = "⏸️ 초기 안내를 준비하거나 보내지 못했습니다. 아래 버튼으로 다시 시도해 주십시오."
        msg = await channel.send(text, view=view_cls(bot, session, uids[0], phase, st, on_resolved))
        await remember_prompt(bot, session, PROMPT_INITIAL_KNOWLEDGE, channel, msg,
                              uid=uids[0], phase=phase, state=st)
    except Exception as e:  # noqa: BLE001
        print(f"[초기지식] 안내 버튼 표시 실패: {e}")
    return False


async def on_profile_done(bot, session, channel):
    """프로필 생성 완료 시 호출된다. 다음 단계로 진행한다."""
    # v7.17: 캐릭터 전체 승인 직후·시작 틀 전 — PROFILE 초기 지식(미확정이면 여기서 멈추고 버튼으로 재개)
    if not await initial_knowledge_gate(bot, session, channel, "PROFILE",
                                        on_resolved=lambda: on_profile_done(bot, session, channel)):
        return
    creation.record(session, "profile", "완료")
    creation.advance(session)
    await render(bot, session, channel)


async def on_open_time_done(bot, session, channel, *, quoted_tokens=None):
    """유지 시간 확정 시 호출된다. 캐시를 올리고 시작 상황으로 넘어간다."""
    creation.record(session, "open", f"{getattr(session, 'open_minutes', 0)}분")

    cog = bot.get_cog("SessionCog")
    # H06: DYNAMIC 대상(단일 PC·필수 capability·시작 틀 있음) 세션은 캐시를 시작 선택 → 정의 바인딩 →
    #   genesis 초기화 뒤로 미룬다(캐시 원천이 바인딩 정의 + genesis 세계여야 한다). 레거시는 그대로.
    from . import world_lifecycle as _wl
    from .io import save_session_data
    defer_cache = bool((session.scenario_data or {}).get("start_frames")) and _wl.is_dynamic_candidate(session)
    if defer_cache:
        session.open_quoted_tokens = quoted_tokens
    if cog and not defer_cache:
        from . import message_lifecycle as _ml
        ok = await cog.upload_cache(session, notify=_ml.transient_notifier(bot, session, channel, ttl=60),
                                    quoted_tokens=quoted_tokens, report_failure=False)
        if not ok:
            # 사용자 결정 D2: 캐시 없이 진행하지 않는다 — 이 단계에 머물고 재시도를 받는다.
            await save_session_data(bot, session)
            msg = await channel.send("⚠️ " + _master.say("cache.open_failed", "세션을 열지 못했습니다.")
                                     + " 아래 버튼으로 다시 시도해 주십시오.",
                                     view=UploadRetryView(bot, session, channel, quoted_tokens))
            await remember_prompt(bot, session, PROMPT_UPLOAD_RETRY, channel, msg, quoted_tokens=quoted_tokens)
            return

    # 통계 — 세션 수·플레이 이력(사전 프로필 생성 가능 판정). !시작 경로와 같은 기록을 버튼 플로우도 남긴다(1회).
    if not getattr(session, "is_started", False):
        import time as _time
        session.started_at = _time.time()        # 세션 시간 통계 기준(!시작 경로와 같음)
        try:
            from . import stats as _stats
            for uid in (session.players or {}):
                await _stats.bump(uid, sessions=1)
                await _stats.mark_played(uid, session.scenario_id)
        except Exception as e:
            print(f"[통계] 세션 시작 기록 실패: {e}")

    # 세션이 실제로 열렸다. 이 플래그가 없으면 턴 진행이 차단된다.
    # !시작 명령 경로에만 있어 버튼 플로우에서는 영원히 False였다.
    session.is_started = True

    # 자동 GM을 켠다. 버튼으로 만든 세션은 !자동 시작을 칠 일이 없으므로
    # 여기서 켜주지 않으면 첫 턴이 열려도 GM이 반응하지 않는다.
    # !자동 시작과 같은 필드를 초기화해야 이후 턴 처리가 어긋나지 않는다.
    names = [p.get("name") for p in (session.players or {}).values()
             if p.get("name")]
    if names:
        session.gm_active = True
        session.gm_target_chars = names
        session.gm_target_char = names[0]   # 지시층위 단일 PC 참조용
        session.gm_turns_done = 0
        session.gm_clarify_count = 0
        from .cost import mark_auto_mode_start
        mark_auto_mode_start(bot, session)   # WP-G: CostLedger 기준(AUD-033)
        session.gm_side_note = ""
        session.gm_pending_players = []
        session.gm_collected_actions = {}
        session.gm_waiting_for = None
        session.gm_proceed_history = []
        session.gm_narrate_count = 0
    else:
        print(f"[세션플로우] PC가 없어 자동 GM을 켜지 못했습니다")

    creation.advance(session)
    await save_session_data(bot, session)
    await render(bot, session, channel)


class UploadRetryView(_Step):
    """캐시 업로드 실패 후 재시도(사용자 결정 D2). 개설자만 누를 수 있다."""

    def __init__(self, bot, session, channel, quoted_tokens=None):
        super().__init__(bot, session, channel)
        self.quoted_tokens = quoted_tokens

    @discord.ui.button(label="🔄 다시 시도", style=discord.ButtonStyle.primary)
    async def retry(self, interaction, _b):
        await interaction.response.defer()
        forget_prompt(self.session, PROMPT_UPLOAD_RETRY)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.stop()
        await on_open_time_done(self.bot, self.session, self.channel, quoted_tokens=self.quoted_tokens)
