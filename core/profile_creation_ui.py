# 프로필 생성 UI — 실행부를 구동하는 인터페이스
#
# [기획 목표]
#   "프로필 생성 시작부터 완성까지 질문에 대한 답과 제시된 다지선다 선택만 하면
#    시나리오별로 예정된 생성과정이 진행되도록 하는 것"
#
#   따라서 이 모듈은 무엇을 물을지 모른다. profile_runner가 반환하는
#   행동 유형(ASK/CONFIRM/WARN/AUTO/DONE)에 따라 화면만 그린다.
#
# [자유도와 통제]
#   재선택·취소·단계회귀를 제공하되, 실행부가 허용한 행동만 노출한다.
#   버튼으로 가능한 것만 보여주므로 허가되지 않은 조작이 불가능하다.
import discord

from . import profile_gen
from . import profile_runner as runner
from . import profiles as profile_store

# 선택지가 이보다 많으면 셀렉트 메뉴를 쓴다.
SELECT_THRESHOLD = 5

# 디스코드 셀렉트 옵션 상한.
MAX_OPTIONS = 25


class ProfileCreationSession:
    """생성 진행 상태. 세션당 캐릭터 하나를 만든다."""

    def __init__(self, bot, session, uid: str, char_name: str):
        self.bot = bot
        self.session = session
        self.uid = str(uid)
        self.char_name = char_name
        self.run = runner.new_run(session.scenario_data)
        self.message = None


async def render(state: ProfileCreationSession, channel, *, user_input=None):
    """실행부를 한 단계 진행하고 화면을 갱신한다.

    AUTO는 화면 없이 다음 단계로 이어지므로 반복 처리한다.
    """
    sd = state.session.scenario_data

    for _ in range(20):   # 자동 단계가 연속될 수 있다. 무한 루프 방지.
        res = runner.step(sd, state.run, user_input)
        user_input = None

        if res["type"] == runner.AUTO:
            await channel.send(f"▸ {res['message']}")
            continue

        if res["type"] == runner.DONE:
            await finish(state, channel)
            return

        if res["type"] == runner.ERROR:
            await channel.send(
                f"⚠️ {res['message']}\n"
                f"> 이전 단계로 돌아가 다시 선택해 주십시오.",
                view=BackOnlyView(state, channel))
            return

        if res.get("ai_module"):
            await _send(state, channel, res,
                        view=AIInputView(state, channel, res))
            return

        if res["type"] in (runner.CONFIRM, runner.WARN):
            await _send(state, channel, res,
                        view=ConfirmView(state, channel, res))
            return

        # ASK — 선택지 또는 자유 입력
        view = (ChoiceView(state, channel, res) if res.get("options")
                else AIInputView(state, channel, res))
        await _send(state, channel, res, view=view)
        return


async def _send(state, channel, res: dict, *, view):
    """단계 화면을 그린다. 기존 메시지는 정리해 채널을 깨끗이 유지한다."""
    progress = runner.progress(state.session.scenario_data, state.run)
    lines = [f"**[{state.char_name} 생성 · {progress}]**"]
    if res.get("guide"):
        lines.append(f"> {res['guide']}")
    lines.append("")
    lines.append(res.get("message") or "")

    if res.get("options") and len(res["options"]) <= SELECT_THRESHOLD:
        lines.append("")
        lines.append("· " + "  ·  ".join(str(o) for o in res["options"]))

    content = "\n".join(lines)
    if state.message:
        try:
            await state.message.edit(content=content, view=view)
            return
        except Exception:
            pass
    state.message = await channel.send(content, view=view)


async def finish(state: ProfileCreationSession, channel):
    """완성된 값을 세션 플레이어에 반영하고 저장 여부를 묻는다."""
    result = runner.result(state.run)

    player = state.session.players.get(state.uid)
    if not isinstance(player, dict):
        player = {"name": state.char_name, "profile": {}, "appearance": ""}
        state.session.players[state.uid] = player

    profile = dict(player.get("profile") or {})
    for key, val in result.items():
        # 능력치는 하위 항목으로 펼쳐 pc_template 구조에 맞춘다.
        if isinstance(val, dict):
            profile.update({k: v for k, v in val.items()})
        elif isinstance(val, list):
            profile[key] = ", ".join(str(x) for x in val)
        else:
            profile[key] = val
    player["profile"] = profile
    if result.get("이름"):
        player["name"] = str(result["이름"])
        state.char_name = player["name"]
    if result.get("외형"):
        player["appearance"] = str(result["외형"])

    from .io import save_session_data
    await save_session_data(state.bot, state.session)

    summary = "\n".join(f"> {k}: {v}" for k, v in profile.items() if v)
    if state.message:
        try:
            await state.message.delete()
        except Exception:
            pass
        state.message = None

    await channel.send(
        f"✅ **{state.char_name}** 생성이 완료되었습니다.\n{summary}",
        view=SaveProfileView(state, result),
    )

    # 세션 제작 플로우 진행 중이면 다음 단계로 넘긴다.
    # 사전 프로필 단독 생성(_StandaloneSession)은 해당하지 않는다.
    if getattr(state.session, "creation_state", None):
        try:
            from . import session_flow
            await session_flow.on_profile_done(state.bot, state.session, channel)
        except Exception as e:
            print(f"[세션플로우] 프로필 완료 처리 실패: {e}")


class _Base(discord.ui.View):
    """조작 권한 검사 — 본인만 조작할 수 있다."""

    def __init__(self, state, channel, timeout=900):
        super().__init__(timeout=timeout)
        self.state = state
        self.channel = channel

    async def interaction_check(self, interaction) -> bool:
        if str(interaction.user.id) == self.state.uid:
            return True
        await interaction.response.send_message(
            "본인만 조작할 수 있습니다.", ephemeral=True)
        return False


class ChoiceView(_Base):
    """목록 선택 — 항목 수에 따라 버튼 또는 셀렉트."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        options = res.get("options") or []
        if len(options) <= SELECT_THRESHOLD:
            for opt in options[:SELECT_THRESHOLD]:
                self.add_item(ChoiceButton(str(opt)))
        else:
            self.add_item(ChoiceSelect(options))
        if res.get("can_back"):
            self.add_item(BackButton())


class ChoiceButton(discord.ui.Button):
    def __init__(self, value: str):
        super().__init__(label=value[:80], style=discord.ButtonStyle.primary)
        self.value = value

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        await render(v.state, v.channel, user_input=self.value)


class ChoiceSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="선택",
            options=[discord.SelectOption(label=str(o)[:100], value=str(o))
                     for o in options[:MAX_OPTIONS]],
        )

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        await render(v.state, v.channel, user_input=self.values[0])


class ConfirmView(_Base):
    """확인 메시지 — 확정 또는 재선택(기획 규정)."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        self.res = res

    @discord.ui.button(label="확정", style=discord.ButtonStyle.success)
    async def ok(self, interaction, _b):
        await interaction.response.defer()
        outcome = runner.confirm(self.state.session.scenario_data, self.state.run)
        if outcome.get("revised"):
            await self.channel.send(f"↻ {outcome['revised']['reason']}")
        await render(self.state, self.channel)

    @discord.ui.button(label="다시 선택", style=discord.ButtonStyle.secondary)
    async def again(self, interaction, _b):
        await interaction.response.defer()
        # 취소로 닫으면 재선택하며 이전 선택 흔적을 지운다(기획 규정).
        runner.cancel_pending(self.state.run)
        await render(self.state, self.channel)


class AIInputView(_Base):
    """자유 입력 — 모달로 받는다."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        self.res = res
        if res.get("can_back"):
            self.add_item(BackButton())

    @discord.ui.button(label="입력하기", style=discord.ButtonStyle.primary)
    async def enter(self, interaction, _b):
        await interaction.response.send_modal(
            InputModal(self.state, self.channel, self.res))


class InputModal(discord.ui.Modal):
    def __init__(self, state, channel, res: dict):
        super().__init__(title=f"{res.get('field', '입력')}")
        self.state = state
        self.channel = channel
        self.res = res
        self.value = discord.ui.TextInput(
            label=str(res.get("field", "값"))[:45],
            style=discord.TextStyle.paragraph,
            required=True, max_length=1000,
        )
        self.add_item(self.value)

    async def on_submit(self, interaction):
        await interaction.response.defer()
        text = str(self.value)
        module = self.res.get("ai_module")

        if module:
            outcome = await runner.run_ai_module(
                self.state.bot, self.state.session,
                self.state.session.scenario_data, self.state.run,
                module, self.res.get("args") or {}, text,
            )
            if outcome.get("message"):
                await self.channel.send(outcome["message"])
            if not outcome.get("ok"):
                # 반려 — 같은 단계를 다시 묻는다.
                await render(self.state, self.channel)
                return
            # 검증 통과분은 확인 절차를 거친다.
            await _send(self.state, self.channel,
                        {"field": self.res.get("field"),
                         "message": f"**{outcome['value']}**",
                         "can_back": True},
                        view=ConfirmView(self.state, self.channel, self.res))
            return

        await render(self.state, self.channel, user_input=text)


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ 이전 단계", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        ok, _f = runner.go_back(v.state.session.scenario_data, v.state.run)
        if not ok:
            await interaction.followup.send("되돌아갈 단계가 없습니다.", ephemeral=True)
            return
        await render(v.state, v.channel)


class BackOnlyView(_Base):
    def __init__(self, state, channel):
        super().__init__(state, channel)
        self.add_item(BackButton())


class SaveProfileView(_Base):
    """완성 후 사전 프로필 저장 여부를 묻는다(기획 규정)."""

    def __init__(self, state, result: dict):
        super().__init__(state, None, timeout=600)
        self.result = result

    @discord.ui.button(label="💾 사전 프로필로 저장", style=discord.ButtonStyle.success)
    async def save(self, interaction, _b):
        await interaction.response.defer()
        saved = await profile_store.create(
            self.state.uid,
            name=self.state.char_name,
            scenario_id=getattr(self.state.session, "scenario_id", None),
            fields=dict(self.result),
        )
        for c in self.children:
            c.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await interaction.followup.send(
            f"💾 저장했습니다. (태그: {saved['tag']})" if saved else "⚠️ 저장에 실패했습니다.",
            ephemeral=True)

    @discord.ui.button(label="저장 안 함", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, _b):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)


class PrefillView(_Base):
    """사전 프로필 사용 여부(기획 규정 — 보유자에게만 묻는다)."""

    def __init__(self, state, channel, candidates: list):
        super().__init__(state, channel)
        dup = profile_store.duplicate_names(state.uid,
                                            getattr(state.session, "scenario_id", None))
        self.add_item(PrefillSelect(candidates, dup))

    @discord.ui.button(label="새로 만들기", style=discord.ButtonStyle.primary, row=1)
    async def fresh(self, interaction, _b):
        await interaction.response.defer()
        await render(self.state, self.channel)


class PrefillSelect(discord.ui.Select):
    def __init__(self, candidates, dup):
        super().__init__(
            placeholder="사전 프로필 사용",
            options=[discord.SelectOption(
                label=profile_store.display_name(p, dup)[:100], value=p["id"])
                for p in candidates[:MAX_OPTIONS]],
        )
        self.candidates = candidates

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        picked = next((p for p in self.candidates if p["id"] == self.values[0]), None)
        if picked:
            # 사전 프로필 값을 채워 두면 실행부가 해당 단계를 건너뛴다.
            v.state.run = runner.new_run(v.state.session.scenario_data,
                                         prefill=dict(picked.get("fields") or {}))
        await render(v.state, v.channel)


async def start(bot, session, uid, char_name: str, channel):
    """프로필 생성을 시작한다.

    기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면
    사용 여부 질문을 생략한다.
    """
    state = ProfileCreationSession(bot, session, uid, char_name)

    steps = profile_gen.get_steps(session.scenario_data)
    if not steps:
        await channel.send(
            "⚠️ 이 시나리오에는 프로필 생성 알고리즘이 정의되어 있지 않습니다.\n"
            "> `!능력치` `!외형` `!설정` 명령으로 직접 설정해 주십시오.")
        return None

    candidates = profile_store.list_profiles(
        uid, getattr(session, "scenario_id", None))
    if candidates:
        await channel.send(
            f"**{char_name}** — 저장된 프로필을 사용하시겠습니까?",
            view=PrefillView(state, channel, candidates))
        return state

    await render(state, channel)
    return state
