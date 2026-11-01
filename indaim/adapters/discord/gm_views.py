"""자동 GM 화면 — 되감기·재서술 확인·세션 열기 확인·추출 재시도·판정 화면(cogs/gm.py에서 옮김)."""
from __future__ import annotations

import asyncio
import discord
import core

from indaim.adapters.discord.gm_config import _RETRY_MODE_PREPARATION


class ExtractionProviderFailure(RuntimeError):
    """v7.29: 추출 provider 호출·파싱이 결과를 내지 못함(재시도 소진·stale) — 응답 처리 실패와 구별한다."""


# ========== [GM 주사위 버튼 View] ==========
class RewindConfirmView(core.message_lifecycle.LifecyclePromptView):
    """되감기 실행 확인 — 되돌리기 불가·환불 불가를 고지한 뒤 실행한다.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결하고 접어서 지운다.
    """

    def __init__(self, bot, session, target_turn: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        self.target_turn = target_turn

    @discord.ui.button(label="되감기 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            await interaction.response.defer()
            return
        await interaction.response.defer()
        try:
            # WP-E: 되감기의 유일한 권위 — 선택된 커밋 시도 스냅샷 복원(게임 이력만, 환불 없음).
            cog = self.bot.get_cog("GMCog")
            try:                                         # v7.33(F10): 리로드 중(cog 없음)·예외도 안내로 끝낸다
                result = (await cog.history_rewind(self.session, self.target_turn) if cog is not None
                          else {"ok": False, "reason": "게임 진행 모듈을 다시 불러오는 중입니다 — 잠시 후 다시 시도해 주십시오."})
            except Exception as e:  # noqa: BLE001
                core.logs.error("history", "되감기 실행 예외", exc=e, session=self.session)
                result = {"ok": False, "reason": f"되감기 중 오류가 발생했습니다({type(e).__name__}). 상태는 바뀌지 않았습니다."}
            if not result["ok"]:
                await core.display.close_notice(
                    interaction, f"⚠️ {result['reason']}", seconds=8)
                return
            msg = core.master.lead("display.rewind_done", (
                f"⏪ **{self.target_turn}턴 종료 시점으로 되돌렸습니다.**\n"
                f"> 제거된 턴: {', '.join(str(t) for t in result['removed_turns'])}\n"
                f"> 정리된 봇 출력: {result.get('removed_messages', 0)}건 · 결제 이력은 그대로 유지됩니다."
            ))
            # 확인 메시지를 결과로 바꾸고 잠시 뒤 지운다.
            # 확인·결과가 따로 남으면 상태판이 위로 밀려난다.
            await core.display.close_notice(interaction, msg)
        finally:
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        await core.display.close_notice(
            interaction, core.master.say("display.cancelled", "되감기를 취소했습니다."), seconds=6)
        self.stop()


async def _rerender_and_report(cog, session, addendum: str) -> None:
    """v7.33(F1·F10): 재서술이 시작되지 못하면(묘사 이전 실패·예외) 이유를 게임 채널에 알린다 — 기존 턴은 그대로."""
    try:
        started, reason = await cog.rerender_latest(session, addendum=addendum)
    except Exception as e:  # noqa: BLE001
        core.logs.error("history", "재서술 예외", exc=e, session=session)
        started, reason = False, f"예기치 않은 오류({type(e).__name__})"
    if started:
        return
    game_ch = cog.bot.get_channel(session.game_ch_id)
    if game_ch:
        try:
            await core.message_lifecycle.send_transient(
                cog.bot, session, game_ch, f"⚠️ 다시 서술하지 못했습니다 — 기존 턴을 그대로 유지합니다. ({reason})",
                key=core.message_lifecycle.KEY_TURN_NOTICE)
        except Exception:  # noqa: BLE001
            pass


class RerenderConfirmView(core.message_lifecycle.LifecyclePromptView):
    """같은 턴 재생성 확인(WP-E) — 선언·판단 보존, 지시층위부터 다시 서술. 환불 없음.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결.
    """

    def __init__(self, bot, session, addendum: str = ""):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        self.addendum = addendum

    @discord.ui.button(label="재생성 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            return
        try:
            entry, _rec, reason = core.turn_history.rerender_target(self.session)
            if entry is None:
                await core.display.close_notice(interaction, f"⚠️ {reason}", seconds=8)
                return
            cog = self.bot.get_cog("GMCog")
            if cog is None:                              # v7.33(F10)
                await core.display.close_notice(
                    interaction, "⚠️ 게임 진행 모듈을 다시 불러오는 중입니다 — 잠시 후 다시 시도해 주십시오.", seconds=8)
                return
            await core.display.close_notice(
                interaction, "🔁 같은 선언으로 턴을 다시 서술합니다. 이전 턴 청구는 유지됩니다.")
            asyncio.create_task(_rerender_and_report(cog, self.session, self.addendum))
        finally:
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        await core.display.close_notice(
            interaction, core.master.say("display.cancelled", "재생성을 취소했습니다."), seconds=6)
        self.stop()


class RewindView(discord.ui.View):
    """
    되감기 버튼 — persistent view.

    NOTE: 디스플레이 채널에 상주해야 하므로 timeout=None + 고정 custom_id.
          실제 실행은 RewindConfirmView 확인을 거친다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="⏪ 1턴 되감기",
                       style=discord.ButtonStyle.secondary,
                       custom_id="rewind:one")
    async def rewind_one(self, interaction: discord.Interaction, _b: discord.ui.Button):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message("세션을 찾을 수 없습니다.", ephemeral=True)
            return
        if getattr(session, "is_processing", False):
            await interaction.response.send_message(
                "턴 진행 중에는 되감을 수 없습니다.", ephemeral=True)
            return

        oldest, newest = core.available_range(session)
        if newest == 0:
            await interaction.response.send_message(
                core.master.say("display.no_history", "되감기 기록이 아직 없습니다."), ephemeral=True)
            return

        target = newest - 1
        if target < oldest:
            await interaction.response.send_message(
                f"되감기 가능 범위는 {oldest}~{newest}턴입니다.", ephemeral=True)
            return

        view = RewindConfirmView(self.bot, session, target)
        await interaction.response.send_message(
            core.master.lead("display.rewind_confirm",
                             f"⚠️ **{newest}턴을 제거하고 {target}턴 종료 시점으로 되돌립니다.**\n"
                             f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
                             f"제거되는 정보는 되감기 로그로 이관됩니다."),
            view=view,
        )
        await core.message_lifecycle.bind_interaction_prompt(interaction, view)


class OpenConfirmView(core.message_lifecycle.LifecyclePromptView):
    """세션 오픈 확인 — 유지 시간과 예상 비용을 보고 진행 여부를 정한다.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결. 만료는 취소와 같다.
    """

    def __init__(self, bot, session, minutes: int, *, on_reopen=None, flow: bool = False):
        super().__init__(timeout=None if flow else 300)   # v7.37: 첫 생성 대화 안의 확인은 진행 화면(시간 제한 없음)
        self.bot = bot
        self.session = session
        self.minutes = minutes
        self.on_reopen = on_reopen          # 다시 열기 성공 후 콜백(닫힌 동안 보관한 선언 이어서 진행)
        self.flow = flow                    # 첫 생성 대화 메시지 안 — 결과를 같은 메시지에 쓰고 지우지 않는다

    async def _notice(self, interaction, text, seconds=12):
        if self.flow:
            ch = self.bot.get_channel(self.session.game_ch_id)
            if ch is not None:
                await core.session_flow.open_feedback(self.bot, self.session, ch, text)
                return
        await core.display.close_notice(interaction, text, seconds=seconds)

    async def on_expired(self):
        self.session.open_minutes = 0

    @discord.ui.button(label="세션 열기", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, _b):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            return

        # 잔액 확인 — 선불식이므로 부족하면 진행하지 않는다(기획 규정).
        #   WP-F: 해석 청구가 이미 반영된 현재 잔액으로 '캐시 선불'만 검사한다. 이 필요액은
        #   open_window 가 같은 견적 토큰(quoted_tokens)으로 실제 선불할 금액과 같다.
        uid = str(interaction.user.id)
        est = core.estimate_session_open(self.session, self.minutes / 60)
        need = est.get("total_ink", 0)
        quoted = int(est.get("cache_tokens") or 0) or None
        bal = core.accounts.get_balance(uid)
        if bal < need:
            await self._notice(
                interaction,
                core.master.lead("cache.insufficient_ink",
                                 f"⚠️ 잔액이 부족합니다. 필요 **{need}잉크** / 보유 **{bal}잉크**\n"
                                 f"잉크 지급은 운영자에게 문의해 주십시오."
                                 + ("\n더 짧은 시간을 이 채널에 다시 말씀하셔도 됩니다." if self.flow else "")),
                seconds=15)
            if self.flow:
                self.session.awaiting_display_input = True
                self.session._open_input_ch_id = self.session.game_ch_id
            self.stop()
            return

        # WP-F(POLICY-CACHE-01): 시간 해석 비용은 해석 직후 interpretation_billing 이 이미
        #   청구했다(취소·시간 만료와 무관). 여기서의 필요 잉크는 앞으로 낼 캐시 선불뿐이다.
        note = ""

        for child in self.children:
            child.disabled = True
        if not self.flow:
            await interaction.message.edit(view=self)
        await self._notice(
            interaction,
            core.master.lead("cache.open_confirm", f"✅ 세션을 {self.minutes}분간 유지합니다. 예상 {need}잉크{note}"))
        self.session.open_minutes = self.minutes

        game_ch = self.bot.get_channel(self.session.game_ch_id)
        step = core.creation.current_step(self.session)

        if step == "open" and game_ch:
            # 세션 제작 중 — 캐시를 올리고 시작 상황으로 넘어간다.
            try:
                await core.session_flow.on_open_time_done(
                    self.bot, self.session, game_ch, quoted_tokens=quoted)
            except Exception as e:
                print(f"[세션플로우] 오픈 처리 실패: {e}")
        else:
            # 닫았던 세션을 다시 여는 경우. 캐시만 새로 올린다.
            # 이 분기가 없으면 시간만 정해지고 세션이 열리지 않는다.
            cog = self.bot.get_cog("SessionCog")
            if cog:
                # WP-F: 업로드 진행/결과 안내는 TRANSIENT_GAME_STATUS(교체·자동 정리, 로그 미기록).
                notify = core.message_lifecycle.transient_notifier(
                    self.bot, self.session, game_ch, ttl=60) if game_ch else None
                ok = await cog.upload_cache(self.session, notify=notify, quoted_tokens=quoted)
                if ok:
                    self.session.is_started = True
                    await core.save_session_data(self.bot, self.session)
                    from core.display import refresh as _refresh
                    await _refresh(self.bot, self.session, reason="reopen")
                    if self.on_reopen is not None:
                        await self.on_reopen(self.session)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _b):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        self.session.open_minutes = 0
        if self.flow:                                   # v7.37: 첫 생성 대화 — 시간을 다시 묻는다
            ch = self.bot.get_channel(self.session.game_ch_id)
            if ch is not None:
                await core.session_flow.ask_open_time(
                    self.bot, self.session, ch, "알겠습니다. 그럼 얼마나 머무르시겠습니까? 이 채널에 다시 말씀해 주십시오.")
            self.stop()
            return
        await core.display.close_notice(
            interaction, "세션 오픈을 취소했습니다.", seconds=6)
        self.stop()


class InfinityPlanView(discord.ui.View):
    """
    인피니티 세션 플랜 선택.

    기획 규정 — 메인 클리어 후 이어갈지 닫을지 묻고, 이어간다면
    플랜을 고르게 한다. 각 플랜의 비용 안내가 필수다.
    """

    def __init__(self, bot, session):
        super().__init__(timeout=1800)
        self.bot = bot
        self.session = session
        for key, plan in core.quest.INFINITY_PLANS.items():
            self.add_item(InfinityPlanButton(key, plan["label"]))

    @discord.ui.button(label="🚪 세션 닫기", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction, _b):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "세션을 여기서 마칩니다. 수고하셨습니다.")
        self.session.gm_active = False
        await core.save_session_data(self.bot, self.session)
        self.stop()


class InfinityPlanButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=0
                         if key in ("sub_only", "with_main") else 1)
        self.plan_key = key

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        plan = core.quest.apply_infinity_plan(view.session, self.plan_key)
        if not plan:
            await interaction.response.send_message("플랜 적용에 실패했습니다.", ephemeral=True)
            return
        view.session.pending_ending = ""
        for c in view.children:
            c.disabled = True
        await interaction.response.edit_message(view=view)
        await interaction.followup.send(
            f"▶️ **인피니티 세션 — {plan['label']}**\n"
            f"> {plan['desc']}\n"
            f"> 비용: {plan['cost']}\n\n"
            f"이야기가 이어집니다."
        )
        await core.save_session_data(view.bot, view.session)
        try:
            await core.refresh_display(view.bot, view.session, reason="infinity")
        except Exception:
            pass
        view.stop()


class ExtractionRetryView(discord.ui.View):
    """
    추출층위 재시도 버튼 — persistent view.

    NOTE: timeout=None + 고정 custom_id + bot.add_view() 등록 조합으로
          봇 재시작 이후에도 버튼이 살아남는다. 추출 실패는 다음 턴을
          차단하므로, 재시작으로 버튼이 죽으면 세션이 영구 정지한다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🔄 턴 정보 정리 재시도",
                       style=discord.ButtonStyle.primary,
                       custom_id="extraction:retry")
    async def retry(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message("세션을 찾을 수 없습니다.", ephemeral=True)
            return
        if not getattr(session, "extraction_pending", False):
            await interaction.response.send_message("이미 정리가 완료되었습니다.", ephemeral=True)
            return

        await interaction.response.defer()
        _ctx = getattr(session, "extraction_retry_ctx", {}) or {}
        cog = self.bot.get_cog("GMCog")
        if _ctx.get("mode") == _RETRY_MODE_PREPARATION:
            # WP-C: 같은 논리 시도·같은 준비 owner로 재개한다(새 자동 턴을 열지 않는다).
            outcome = await cog._retry_prepared_extraction(session)
            if outcome == "ready":
                await core.display.close_notice(
                    interaction, "✅ 턴 정보 정리가 완료되었습니다. 계속 진행하십시오.")
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
            elif outcome == "released":
                await interaction.followup.send(
                    "진행 중이던 턴 준비 상태가 없어(재시작 등) 차단만 해제했습니다.",
                    ephemeral=True)
            return
        # WP-G(RE-GATE): 준비 모드가 아닌 재시도 컨텍스트(WP-C 이전 저장 형식)는 현재 권위로
        #   안전하게 적용할 수 없다 — provider 재호출·추출 계획 적용·정본 변이 없이 은퇴한다.
        #   은퇴는 strict 저장이 성공한 뒤에만 차단을 해제한다(실패 시 차단·컨텍스트 유지).
        if await cog._retire_legacy_extraction_retry(session):
            await core.display.close_notice(
                interaction,
                "ℹ️ 이전 버전에서 남은 턴 정보 정리 요청이라 현재 방식으로 안전하게 적용할 수 없어 "
                "폐기했습니다. 이미 진행된 게임 상태는 소급 변경하지 않았습니다. "
                "다음 행동을 선언해 계속 진행하십시오.")
        else:
            await interaction.followup.send(
                "⚠️ 이전 버전의 정리 요청을 폐기하는 저장에 실패했습니다. 차단은 유지되며 "
                "게임 상태는 바뀌지 않았습니다. 잠시 후 다시 눌러 주십시오.", ephemeral=True)


def _set_roll_open(session, transaction_id, value: bool) -> None:
    """v7.33(F4): 판정 화면 열림 표식 — 같은 시도일 때만(낡은 화면이 새 시도를 건드리지 않음)."""
    tx = core.turn_transaction.get_active_transaction(session)
    if tx is not None and transaction_id is not None and tx.transaction_id == transaction_id:
        tx.roll_open = value


class RollContinueView(discord.ui.View):
    """v7.33(F5): 굴림 뒤 지시층위가 실패했을 때 — 보관한 판정 결과로 이어서 진행(재굴림 없음)."""

    def __init__(self, cog, session, transaction_id: str):
        super().__init__(timeout=None)
        self.cog, self.session, self.transaction_id = cog, session, transaction_id
        self._busy = False

    @discord.ui.button(label="🎲 판정 결과로 이어서 진행", style=discord.ButtonStyle.primary,
                       custom_id=core.ui_expiry.ROLL_PREFIX + "continue")
    async def resume(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if str(interaction.user.id) not in (self.session.players or {}):
            return await interaction.response.send_message("> 이 세션의 플레이어만 누를 수 있습니다.", ephemeral=True)
        tx = core.turn_transaction.get_active_transaction(self.session)
        pend = getattr(tx, "pending_roll", None) if tx is not None and tx.transaction_id == self.transaction_id else None
        if self._busy or not pend:
            return await interaction.response.send_message("> 이미 처리됐거나 지난 판정입니다.", ephemeral=True)
        if not core.is_session_open(self.session):
            return await interaction.response.send_message(
                "> 세션이 닫혀 있습니다. 디스플레이 채널에서 세션을 연 뒤 다시 눌러 주십시오.", ephemeral=True)
        self._busy = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        asyncio.create_task(self.cog._continue_with_roll_results(
            self.session, pend["player_message"], list(pend["roll_results"]), transaction_id=self.transaction_id))


class GMRollView(discord.ui.View):
    """
    GM에서 ROLL 판정 시 플레이어에게 주사위 버튼을 제공하는 View.
    """

    def __init__(self, cog, session, roll_specs: list, player_message: str,
                 prior_roll_results: list, target_uid: str | None,
                 transaction_id: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.session = session
        self.roll_specs = roll_specs
        self.player_message = player_message
        self.prior_roll_results = list(prior_roll_results)
        self.target_uid = target_uid
        # WP-01: 원 오케스트레이션 스택은 버튼/타임아웃 재개 전에 이미 반환되었으므로,
        # 논리 턴 시도의 불변 식별자를 View가 직접 들고 async UI 경계를 넘긴다.
        self.transaction_id = transaction_id
        self._resolved = False
        self.message = None   # 판정 요청 메시지 — 자동 굴림 시 버튼을 비활성화한다(v7.13)

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary,
                       custom_id=core.ui_expiry.ROLL_PREFIX + "dice")   # 재시작 뒤 누르면 재선언 안내(v7.33)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.target_uid and str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message(
                "> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True
            )

        if self._resolved:
            return await interaction.response.send_message(
                "> 이미 처리된 판정입니다.", ephemeral=True
            )
        self._resolved = True
        _set_roll_open(self.session, self.transaction_id, False)

        # 효과음 즉시 발사(논블로킹). 결과는 _process_roll에서 1.5초 하한 후 출력.
        asyncio.create_task(core.play_dice_sfx(self.cog.bot, interaction.guild))

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="> 🎲 주사위를 굴리는 중…", view=self
        )
        self.stop()

        asyncio.create_task(self._process_roll(interaction.channel))

    async def _process_roll(self, game_ch):
        # 효과음이 결과보다 선행하도록 1.5초 하한 부여 (인게임 주사위와 리듬 통일)
        await asyncio.sleep(1.5)
        new_results = await self.cog._execute_rolls(
            self.session, self.roll_specs, game_ch, transaction_id=self.transaction_id)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(
                self.session, self.player_message, combined,
                transaction_id=self.transaction_id)
        )

    async def on_timeout(self):
        if self._resolved:
            return
        self._resolved = True
        _set_roll_open(self.session, self.transaction_id, False)
        # v7.13: 자동 굴림 뒤 요청 메시지에 누를 수 있는 버튼이 남지 않게 한다.
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception as e:
                print(f"[판정] 자동 굴림 버튼 비활성화 실패: {e}")

        master_ch = self.cog.bot.get_channel(self.session.master_ch_id)
        if master_ch:
            await master_ch.send(
                "⚠️ **[GM]** 판정 버튼 시간 초과(5분). 주사위를 자동으로 굴립니다."
            )
        game_ch = self.cog.bot.get_channel(self.session.game_ch_id)
        new_results = await self.cog._execute_rolls(
            self.session, self.roll_specs, game_ch, transaction_id=self.transaction_id)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(
                self.session, self.player_message, combined,
                transaction_id=self.transaction_id)
        )
