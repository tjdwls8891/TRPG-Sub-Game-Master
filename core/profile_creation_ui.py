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
        # 화면은 메시지 하나를 고쳐 쓴다. 단계마다 새로 보내면 지난
        # 인터페이스가 남아 눌리고, 위아래로 스크롤해야 한다.
        self.message = None
        # AUTO 단계에서 코드가 정한 결과를 모아 화면에 함께 보여준다.
        self.notes = []


async def render(state: ProfileCreationSession, channel, *, user_input=None):
    """실행부를 한 단계 진행하고 화면을 갱신한다.

    AUTO는 화면 없이 다음 단계로 이어지므로 반복 처리한다.
    """
    sd = state.session.scenario_data

    for _ in range(20):   # 자동 단계가 연속될 수 있다. 무한 루프 방지.
        res = runner.step(sd, state.run, user_input)
        user_input = None

        if res["type"] == runner.AUTO:
            # 자동 확정은 따로 보내지 않고 다음 화면에 함께 싣는다.
            if res.get("message"):
                state.notes.append(res["message"])
            continue

        if res["type"] == runner.DONE:
            await finish(state, channel)
            return

        if res["type"] == runner.ERROR:
            await _send(state, channel,
                        {"message": f"⚠️ {res['message']}",
                         "guide": "이전 단계로 돌아가 다시 골라 주세요."},
                        view=BackOnlyView(state, channel))
            return

        if res.get("ai_module"):
            await _send(state, channel, res,
                        view=AIInputView(state, channel, res))
            return

        if res.get("stat_module"):
            await _send(state, channel, res,
                        view=StatRollView(state, channel, res))
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


def _embed(state, res: dict) -> discord.Embed:
    """단계 임베드. 진행도는 푸터 한 곳에만 둔다."""
    e = discord.Embed(
        title=f"{state.char_name} 만들기",
        description=res.get("message") or "",
        color=0x5865F2,
    )
    if res.get("guide"):
        e.add_field(name="\u200b", value=f"💡 {res['guide']}", inline=False)

    # 직전에 코드가 자동으로 정한 것들을 함께 보여준다.
    if state.notes:
        e.add_field(name="자동 확정", value="\n".join(f"· {n}" for n in state.notes),
                    inline=False)
        state.notes = []

    if res.get("options") and len(res["options"]) <= SELECT_THRESHOLD:
        e.add_field(name="\u200b",
                    value="  ·  ".join(str(o) for o in res["options"]),
                    inline=False)

    e.set_footer(text=runner.progress(state.session.scenario_data, state.run))
    return e


async def _send(state, channel, res: dict, *, view):
    """단계 화면을 그린다. 메시지 하나를 계속 고쳐 쓴다."""
    embed = _embed(state, res)
    if state.message:
        try:
            await state.message.edit(embed=embed, view=view, content=None)
            return
        except Exception:
            state.message = None
    state.message = await channel.send(embed=embed, view=view)


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

    # 초기 소지품 — 공통 + 직업별 (석 달을 버틴 사람이 배로 건너온 규모)
    try:
        items = profile_gen.starting_items(
            state.session.scenario_data, result.get("직업"))
        if items:
            res = dict(getattr(state.session, "resources", {}) or {})
            bag = dict(res.get(state.char_name) or {})
            for it in items:
                bag[it] = bag.get(it, 0) + 1
            res[state.char_name] = bag
            state.session.resources = res
    except Exception as e:
        print(f"[프로필] 소지품 지급 실패: {e}")

    from .io import save_session_data
    await save_session_data(state.bot, state.session)

    # 완성 화면도 같은 메시지를 고쳐 쓴다.
    e = discord.Embed(
        title=f"{state.char_name}",
        description="캐릭터가 완성되었습니다.",
        color=0x57F287,
    )
    stats_line = []
    for k, v in profile.items():
        if not v:
            continue
        if len(str(v)) > 60:
            e.add_field(name=k, value=str(v)[:1000], inline=False)
        else:
            stats_line.append(f"**{k}** {v}")
    if stats_line:
        e.insert_field_at(0, name="\u200b", value=" · ".join(stats_line),
                          inline=False)

    try:
        items = profile_gen.starting_items(
            state.session.scenario_data, result.get("직업"))
        if items:
            e.add_field(name="소지품", value=", ".join(items), inline=False)
    except Exception:
        pass

    view = SaveProfileView(state, result)
    if state.message:
        try:
            await state.message.edit(embed=e, view=view, content=None)
            view.message = state.message
        except Exception:
            state.message = None
    if not state.message:
        state.message = await channel.send(embed=e, view=view)
        view.message = state.message

    # NOTE: 다음 단계로 넘기는 것은 SaveProfileView가 한다.
    #       여기서 넘기면 저장 여부를 묻기도 전에 화면이 바뀐다.


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
            # 별도 메시지로 보내면 화면 밖에 쌓인다. 다음 화면에 함께 싣는다.
            self.state.notes.append(outcome["revised"]["reason"])
        await render(self.state, self.channel)

    @discord.ui.button(label="다시 선택", style=discord.ButtonStyle.secondary)
    async def again(self, interaction, _b):
        await interaction.response.defer()
        # 취소로 닫으면 재선택하며 이전 선택 흔적을 지운다(기획 규정).
        runner.cancel_pending(self.state.run)
        await render(self.state, self.channel)


class StatRollView(_Base):
    """
    능력치 배분 — 산출은 언제나 랜덤이고 세 조건을 등급으로 고른다.

    기획 규정 — 수치 대신 별명으로 직관적으로 선택하게 한다.
      총합   허접 · 약골 · 평범 · 튼튼 · 능력자 · 먼치킨
      편차   만능 · 무난 · 뚜렷 · 극단
      특화   골고루 또는 특정 능력치
    """

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        self.res = res
        args = res.get("args") or {}
        sd = state.session.scenario_data
        self.add_item(TotalTierSelect(args, sd))
        self.add_item(SpreadTierSelect(args, sd, state.run))
        stats = args.get("stats") or []
        if stats:
            self.add_item(TopFieldSelect(stats))

    @discord.ui.button(label="🎲 다시 굴리기", style=discord.ButtonStyle.primary, row=3)
    async def reroll(self, interaction, _b):
        await interaction.response.defer()
        runner.cancel_pending(self.state.run)
        await render(self.state, self.channel)

    @discord.ui.button(label="✅ 확정", style=discord.ButtonStyle.success, row=3)
    async def ok(self, interaction, _b):
        await interaction.response.defer()
        runner.confirm(self.state.session.scenario_data, self.state.run)
        await render(self.state, self.channel)


class TotalTierSelect(discord.ui.Select):
    """총합 등급 — 상한 대비 비율로 환산된다."""

    def __init__(self, args: dict, scenario_data: dict):
        tiers = profile_gen.get_total_tiers(scenario_data)
        options = [discord.SelectOption(label="총합: 무작위", value="__none__")]
        for name in tiers:
            options.append(discord.SelectOption(
                label=profile_gen.describe_tier(name, args, scenario_data)[:100],
                value=name))
        super().__init__(placeholder="전체적인 강함", options=options[:25], row=0)

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        val = None if self.values[0] == "__none__" else self.values[0]
        runner.set_stat_constraint(v.state.run, "total_tier", val)
        runner.cancel_pending(v.state.run)
        await render(v.state, v.channel)


class SpreadTierSelect(discord.ui.Select):
    """편차 등급 — 총합에서 가능한 최대 편차에 비례한다."""

    def __init__(self, args: dict, scenario_data: dict, run: dict):
        tiers = profile_gen.get_spread_tiers(scenario_data)
        cons = run.get("stat_constraints") or {}
        total_v = (profile_gen.tier_to_total(cons["total_tier"], args, scenario_data)
                   if cons.get("total_tier") else None)
        options = [discord.SelectOption(label="편차: 무작위", value="__none__")]
        for name in tiers:
            options.append(discord.SelectOption(
                label=profile_gen.describe_tier(
                    name, args, scenario_data, kind="spread", total=total_v)[:100],
                value=name))
        super().__init__(placeholder="능력의 치우침", options=options[:25], row=1)

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        val = None if self.values[0] == "__none__" else self.values[0]
        runner.set_stat_constraint(v.state.run, "spread_tier", val)
        runner.cancel_pending(v.state.run)
        await render(v.state, v.channel)


class TopFieldSelect(discord.ui.Select):
    """특정 능력치를 최고로 지정하거나 해제한다."""

    def __init__(self, stats: list):
        options = [discord.SelectOption(label="특화: 골고루", value="__none__")]
        options += [discord.SelectOption(label=f"{s} 특화", value=s)
                    for s in stats[:24]]
        super().__init__(placeholder="특화 분야", options=options, row=2)

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        val = None if self.values[0] == "__none__" else self.values[0]
        runner.set_stat_constraint(v.state.run, "top_field", val)
        runner.cancel_pending(v.state.run)
        await render(v.state, v.channel)


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
                self.state.notes.append(outcome["message"])
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
    """완성 후 사전 프로필 저장 여부를 묻는다(기획 규정).

    어느 쪽을 고르든 화면을 정리하고 세션 플로우로 넘긴다.
    버튼만 비활성화하고 두면 다음 단계가 시작되지 않는다.
    """

    def __init__(self, state, result: dict):
        super().__init__(state, None, timeout=600)
        self.result = result
        self.message = None

    async def _close(self, interaction, note: str):
        self.clear_items()
        e = discord.Embed(
            title=f"{self.state.char_name}",
            description=note,
            color=0x57F287,
        )
        try:
            await interaction.message.edit(embed=e, view=None)
        except Exception:
            pass
        self.state.message = None
        self.stop()
        await _advance_flow(self.state, interaction.channel)

    @discord.ui.button(label="💾 사전 프로필로 저장", style=discord.ButtonStyle.success)
    async def save(self, interaction, _b):
        await interaction.response.defer()
        saved = await profile_store.create(
            self.state.uid,
            name=self.state.char_name,
            scenario_id=getattr(self.state.session, "scenario_id", None),
            fields=dict(self.result),
        )
        note = (f"저장했습니다. 다음에 이 시나리오를 하실 때 불러오실 수 있어요.\n"
                f"태그: **{saved['tag']}**") if saved else "저장에 실패했습니다."
        await self._close(interaction, note)

    @discord.ui.button(label="저장 안 함", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, _b):
        await interaction.response.defer()
        await self._close(interaction, "준비가 끝났습니다.")


class PrefillView(_Base):
    """사전 프로필 사용 여부(기획 규정 — 보유자에게만 묻는다).

    고르든 새로 만들든 이 화면을 그대로 생성 화면으로 바꿔 이어간다.
    새 메시지를 보내면 이 버튼들이 남아 다시 눌린다.
    """

    def __init__(self, state, channel, candidates: list):
        super().__init__(state, channel)
        dup = profile_store.duplicate_names(
            state.uid, getattr(state.session, "scenario_id", None))
        self.add_item(PrefillSelect(candidates, dup))

    @discord.ui.button(label="새로 만들기", style=discord.ButtonStyle.primary, row=1)
    async def fresh(self, interaction, _b):
        await interaction.response.defer()
        self.state.message = interaction.message
        self.clear_items()
        self.stop()
        await render(self.state, self.channel)


class PrefillSelect(discord.ui.Select):
    def __init__(self, candidates, dup):
        super().__init__(
            placeholder="저장해둔 프로필 사용",
            options=[discord.SelectOption(
                label=profile_store.display_name(p, dup)[:100], value=p["id"])
                for p in candidates[:MAX_OPTIONS]],
        )
        self.candidates = candidates

    async def callback(self, interaction):
        await interaction.response.defer()
        v = self.view
        picked = next((p for p in self.candidates
                       if p["id"] == self.values[0]), None)
        if picked:
            # 사전 프로필 값을 채워 두면 실행부가 해당 단계를 건너뛴다.
            v.state.run = runner.new_run(
                v.state.session.scenario_data,
                prefill=dict(picked.get("fields") or {}))
            v.state.notes.append(
                f"{picked.get('name', '저장한 프로필')}을(를) 불러왔습니다")

        # 이 메시지를 생성 화면으로 이어 쓴다.
        v.state.message = interaction.message
        v.clear_items()
        v.stop()
        await render(v.state, v.channel)


async def _advance_flow(state, channel):
    """세션 제작 플로우 진행 중이면 다음 단계로 넘긴다."""
    if not getattr(state.session, "creation_state", None):
        return
    try:
        from . import session_flow
        await session_flow.on_profile_done(state.bot, state.session, channel)
    except Exception as e:
        print(f"[세션플로우] 프로필 완료 처리 실패: {e}")


async def start(bot, session, uid, char_name: str, channel):
    """프로필 생성을 시작한다.

    기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면
    사용 여부 질문을 생략한다.
    """
    state = ProfileCreationSession(bot, session, uid, char_name)

    steps = profile_gen.get_steps(session.scenario_data)
    if not steps:
        # 알고리즘이 없으면 흐름이 끊기지 않도록 안내 후 다음 단계로 넘긴다.
        await channel.send(embed=discord.Embed(
            title="캐릭터 설정",
            description=("이 시나리오에는 자동 생성 절차가 준비되어 있지 않습니다.\n"
                         "`!능력치` `!외형` `!설정` 명령으로 직접 정해 주세요."),
            color=0xFEE75C))
        await _advance_flow(state, channel)
        return None

    # 기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면 질문 생략.
    from . import creation
    sid = getattr(session, "scenario_id", None)
    if creation.can_skip_profile_question(session, uid, sid):
        await render(state, channel)
        return state

    candidates = profile_store.list_profiles(uid, sid)
    if candidates:
        e = discord.Embed(
            title=f"{char_name} 만들기",
            description=("전에 저장해두신 프로필이 있어요.\n"
                         "불러다 쓰시겠어요, 아니면 새로 만드시겠어요?"),
            color=0x5865F2)
        state.message = await channel.send(
            embed=e, view=PrefillView(state, channel, candidates))
        return state

    await render(state, channel)
    return state
