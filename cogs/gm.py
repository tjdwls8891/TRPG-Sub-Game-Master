import os
import re
import json
import copy
import random
import asyncio
import time
import dataclasses
from types import SimpleNamespace
import discord
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core
from indaim.turn.declaration import (  # noqa: F401 — 리팩터링 T-5: 옮긴 함수(같은 이름 유지)
    GATE_CLASSES, GATE_RESOLUTIONS, GATE_REASONS, GATE_TARGET_MAX, _GATE_NOTICE_CLARIFY, _GATE_NOTICE_DEFAULT, _GATE_NOTICE_BLOCK, _parse_gate, _gate_blocks, _gate_rolls_discovery, _gate_notice, _gate_judgment_block, _discovery_outcome, _declaration_block)
from indaim.turn.roll_sides import (  # noqa: F401 — 리팩터링 T-5: 옮긴 함수(같은 이름 유지)
    _npc_key, _npc_stats, _judge_side, _side_total, _roll_domains, _judge_plan, _pc_skills, _party_scene, _attack_domains, _party_assist, _party_round)
from indaim.turn.brief import (  # noqa: F401 — 리팩터링 T-5: 옮긴 함수(같은 이름 유지)
    JUDGMENT_RECENT_LOGS, MAX_CLARIFY_PER_MESSAGE, MAX_NARRATE_PER_MESSAGE, _build_judgment_user_prompt, _build_logic_user_prompt, _sys_text, _clean_proceed_instruction, _cap_display, _interp_note)

# 프롬프트 중앙 등록소에서 AI 프롬프트 상수 및 빌더 임포트
from prompts import (
    CACHE_TIME_RESPONSE_SCHEMA,
    CACHE_TIME_SYSTEM_INSTRUCTION,
    EXTRACTION_SYSTEM_INSTRUCTION,
    NPC_DETAIL_RESPONSE_SCHEMA,
    NPC_DETAIL_SYSTEM_INSTRUCTION,
    IRREGULAR_NPC_RESPONSE_SCHEMA,
    IRREGULAR_NPC_SYSTEM_INSTRUCTION,
    JUDGMENT_RESPONSE_SCHEMA,
    JUDGMENT_SYSTEM_INSTRUCTION,
    GM_LOGIC_RESPONSE_SCHEMA,
    GM_LOGIC_SYSTEM_INSTRUCTION,
    NARRATIVE_PLAN_SCHEMA,
    NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
    build_narrate_prompt,
    # 방안 B — 세계 물리 타임라인
    # 방안 E — PROCEED 자기 검증
    # 방안 6 — 2단계 사고 서사 방향성 시뮬레이터
    NARRATIVE_DIRECTION_SCHEMA,
    NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION,
)
from indaim.adapters.discord.gm_views import (  # noqa: F401 — 리팩터링 T-5: 옮긴 화면(같은 이름 유지)
    ExtractionProviderFailure, ExtractionRetryView, GMRollView, InfinityPlanButton, InfinityPlanView, OpenConfirmView, RerenderConfirmView, RewindConfirmView, RewindView, RollContinueView, _rerender_and_report, _set_roll_open)
from indaim.adapters.discord.gm_config import (  # noqa: F401
    COST_LOG_PREFIX, EXTRACTION_MAX_RETRIES, EXTRACTION_MODEL, EXTRACTION_THINKING_BUDGET, GATE_UNVERIFIABLE_NOTICE, GM_LOGIC_MAX_RETRIES, JUDGMENT_MAX_RETRIES, JUDGMENT_MODEL, MAX_ITERATIONS_PER_MESSAGE, WORLD_REACTION_AUTHORITY_BLOCK, _OUTCOME_DELIVERY_FAILED, _OUTCOME_EXTRACTION_FAILED, _OUTCOME_NOT_READY, _OUTCOME_NO_NARRATION, _OUTCOME_READY, _OUTCOME_SUPERSEDED, _RETRY_MODE_PREPARATION)
from indaim.adapters.discord.turn_flow import TurnFlow
from indaim.adapters.discord.turn_steps import TurnSteps
from indaim.turn.world_steps import WorldSteps


# ========== [GM Cog] ==========
class GMCog(TurnFlow, TurnSteps, WorldSteps, commands.Cog):
    """
    게임 채널의 플레이어 발언을 받아 AI가 GM 역할을 자동 수행하는 옵트인 모드.

    PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 물어보는 라운드 수집 시스템을 포함.
    멀티플레이어 지원: 등록된 모든 PC에게 순서대로 행동을 물어본 뒤 종합하여 지시층위 호출.
    """


    def __init__(self, bot):
        self.bot = bot
        self._session_locks = {}

    def _lock_for(self, session):
        if session.session_id not in self._session_locks:
            self._session_locks[session.session_id] = asyncio.Lock()
        return self._session_locks[session.session_id]

    @commands.group(name="자동", invoke_without_command=True)
    async def auto(self, ctx, *args):
        """
        GM 명령어 그룹. 모든 하위 기능을 인자로 분기한다.
        인자가 없거나 알 수 없는 하위명령이면 사용법을 출력한다.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        unknown = f"⚠️ 알 수 없는 하위명령: `{args[0]}`\n" if args else ""
        await ctx.send(
            f"{unknown}🤖 **[GM 명령어]**\n"
            "`!자동 시작 (대상PC…)` : GM 활성화 (단일 PC면 자동 선택)\n"
            "`!자동 중단` : GM 정지 후 인간 GM 명령 모드 복귀\n"
            "`!자동 상태` : 활성 여부·자동 처리 턴·누적 비용 확인\n"
            "`!자동 개입 [텍스트]` : 다음 PROCEED 완료 시까지 GM 사이드 노트 유지\n"
            "`!자동 턴제한 [N|해제]` : 자동 진행 최대 턴 수 (해제=무제한)\n"
            "`!자동 비용제한 [원|해제]` : 자동 누적 비용 상한 (해제=무제한)\n"
            "`!자동 원장` : 정보 인지 원장(비공개 정보별 인지 주체) 출력"
        )

    @auto.command(name="시작")
    async def auto_start(self, ctx, *target_char_args: str):
        """
        GM 활성화. 인자 없으면 등록된 모든 PC를 대상으로 함.
        멀티플레이어 시 !자동 시작, 특정 PC만 지정 시 !자동 시작 이름1 이름2 형태로 사용.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "is_started", False):
            return await ctx.send("⚠️ 세션이 시작되지 않았습니다. `!시작`을 먼저 실행하세요.")

        # 대상 PC 결정
        if target_char_args:
            target_chars = list(target_char_args)
        elif session.players:
            target_chars = [p.get("name") for p in session.players.values() if p.get("name")]
        else:
            return await ctx.send(
                "⚠️ 등록된 PC가 없습니다. `!참가`로 PC를 먼저 등록하세요."
            )

        # 유효성 검증
        invalid = [n for n in target_chars if not core.get_uid_by_char_name(session, n)]
        if invalid:
            return await ctx.send(f"⚠️ 다음 PC를 찾을 수 없습니다: {', '.join(invalid)}")

        session.gm_active = True
        session.gm_target_chars = target_chars
        session.gm_target_char = target_chars[0]   # 하위 호환성 (지시층위 단일 PC 참조용)
        session.gm_turns_done = 0
        session.gm_clarify_count = 0
        core.mark_auto_mode_start(self.bot, session)   # WP-G: CostLedger 기준(AUD-033)
        session.gm_side_note = ""
        session.gm_pending_players = []
        session.gm_collected_actions = {}
        session.gm_waiting_for = None
        # NOTE: 최근 5회 PROCEED 이력 (지시사항 + 중간 컨텍스트 + AI 출력 요약).
        # 지시층위 프롬프트에 주입되어 서사 반복·정체를 방지한다. 봇 재시작 시 초기화 허용.
        session.gm_proceed_history = []
        await core.save_session_data(self.bot, session)

        has_existing_plan = bool(session.narrative_plan)
        plan_note = (
            "- 서사: 동적 세계 사건·정본이 주도 (레거시 서사 계획 없음)"
            if core.player_world.legacy_narrative_retired(session) else
            f"- 서사 계획: 기존 계획 유지 (v{session.narrative_plan.get('plan_version', '?')})"
            if has_existing_plan else
            "- 서사 계획: 수립 중... (백그라운드에서 진행)"
        )
        await ctx.send(
            f"🤖 **[GM 활성화]**\n"
            f"- 대상 PC: **{', '.join(target_chars)}**\n"
            f"- 자동 턴 한도: {_cap_display(session.gm_turn_cap)}\n"
            f"- 자동 누적 비용 한도: {_cap_display(session.gm_cost_cap_krw, is_cost=True)}\n"
            f"{plan_note}\n"
            f"- PROCEED 완료 후 GM이 선제적으로 행동을 물어봅니다.\n"
            f"- 중단: `!자동 중단`  /  GM에게 메모: `!자동 개입 [텍스트]`"
        )

        # 활성화 직후: 서사 계획 수립 후 첫 라운드 시작 (백그라운드 태스크)
        asyncio.create_task(self._init_narrative_and_start(session))

    @auto.command(name="중단")
    async def auto_stop(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "gm_active", False):
            return await ctx.send("⚠️ GM가 활성 상태가 아닙니다.")

        session.gm_active = False
        session.gm_waiting_for = None
        session.gm_pending_players = []
        await core.save_session_data(self.bot, session)

        try:
            used = core.auto_mode_used_krw(self.bot, session)
        except Exception as e:  # noqa: BLE001
            print(f"[자동 비용] 원장 읽기 실패: {e}")
            used = 0.0
        await ctx.send(
            f"🛑 **[GM 정지]**\n"
            f"- 자동 처리 턴: {session.gm_turns_done}턴\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)}\n"
            f"- 인간 GM 명령어 입력 모드로 복귀합니다."
        )

    @auto.command(name="상태")
    async def auto_status(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        active = getattr(session, "gm_active", False)
        try:
            used = core.auto_mode_used_krw(self.bot, session)   # WP-G: CostLedger provider 비용
        except Exception as e:  # noqa: BLE001
            print(f"[자동 비용] 원장 읽기 실패: {e}")
            used = 0.0
        target_chars = getattr(session, "gm_target_chars", [])
        waiting = getattr(session, "gm_waiting_for", None)
        pending = getattr(session, "gm_pending_players", [])
        collected = getattr(session, "gm_collected_actions", {})

        collected_str = "\n".join([f"    · {k}: {v[:40]}" for k, v in collected.items()]) or "    (없음)"
        await ctx.send(
            f"🤖 **[GM 상태]**\n"
            f"- 활성: {'✅ 켜짐' if active else '⛔ 꺼짐'}\n"
            f"- 대상 PC: {', '.join(target_chars) if target_chars else '(없음)'}\n"
            f"- 자동 처리 턴: {session.gm_turns_done} / {_cap_display(session.gm_turn_cap)}\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)} / {_cap_display(session.gm_cost_cap_krw, is_cost=True)}\n"
            f"- 현재 발언 대기 PC: {waiting or '(없음)'}\n"
            f"- 응답 대기 중인 PC: {', '.join(pending) if pending else '(없음)'}\n"
            f"- 수집된 행동:\n{collected_str}\n"
            f"- 직전 ASK 횟수: {session.gm_clarify_count}\n"
            f"- 대기 중 사이드 노트: {session.gm_side_note or '(없음)'}"
        )

    @auto.command(name="개입")
    async def auto_inject(self, ctx, *, text: str = ""):
        """다음 PROCEED 완료 시까지 GM 사이드 노트를 유지."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not text.strip():
            return await ctx.send("⚠️ 사용법: `!자동 개입 [GM에게 전달할 메모]`")

        session.gm_side_note = text.strip()
        await core.save_session_data(self.bot, session)
        await ctx.send(
            f"📝 사이드 노트 등록 (다음 PROCEED(턴 진행) 완료 시까지 유지):\n> {text.strip()}"
        )

    @auto.command(name="턴제한")
    async def auto_set_cap(self, ctx, n: str = None):
        """자동 진행 최대 턴 수 설정. '해제'/'0' 또는 인자 없음 → 무제한."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if n is None or n in ("해제", "무제한", "0"):
            session.gm_turn_cap = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 턴 한도를 **무제한**으로 설정했습니다.")

        try:
            val = int(n)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 턴제한 [1~1000 | 해제]`")
        if val < 1 or val > 1000:
            return await ctx.send("⚠️ 턴 한도는 1~1000 사이여야 합니다. (무제한: `해제`)")

        session.gm_turn_cap = val
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 턴 한도를 {val}턴으로 변경했습니다.")

    @auto.command(name="비용제한")
    async def auto_set_cost_cap(self, ctx, amount: str = None):
        """자동 모드 누적 비용 상한(원) 설정. '해제'/'0' 또는 인자 없음 → 무제한."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if amount is None or amount in ("해제", "무제한", "0"):
            session.gm_cost_cap_krw = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 누적 비용 한도를 **무제한**으로 설정했습니다.")

        try:
            val = float(amount)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 비용제한 [금액(원) | 해제]`")
        if val <= 0:
            return await ctx.send("⚠️ 비용 한도는 0보다 커야 합니다. (무제한: `해제`)")

        session.gm_cost_cap_krw = val
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 누적 비용 한도를 {core.format_cost(val)}으로 변경했습니다.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.content.startswith("!"):
            return

        # v7.15: 캐릭터 생성 중인 플레이어의 채팅은 생성 답변 경로가 소유한다(지금 텍스트를 기다리면 1건 소비,
        #   아니면 무시). GM 활성 여부·턴 처리보다 먼저 판정한다 — DM 사전 생성도 같은 경로.
        if core.profile_creation_ui.claims(self.bot, message):
            asyncio.create_task(core.profile_creation_ui.on_chat(self.bot, message))
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return

        # 디스플레이 채널의 시간 입력 — 입력 대기 중인 참가자(또는 오너)의 답변 1건만 소비한다.
        #   chat_guard와 별개로 모든 메시지가 여기에 오므로, 대기가 아니면 해석(청구)하지 않는다.
        if message.channel.id == getattr(session, "display_ch_id", None):
            if not getattr(session, "awaiting_display_input", False):
                return
            if getattr(session, "_open_input_ch_id", None) not in (None, message.channel.id):
                return                                          # v7.21.4: 첫 생성 답변은 게임 채널에서 받는다
            if (str(message.author.id) not in (session.players or {})
                    and not await self.bot.is_owner(message.author)):
                return
            if not getattr(session, "awaiting_display_input", False):   # 대기 중 다른 답변이 먼저 소비
                return
            session.awaiting_display_input = False
            session._display_input_msg_id = message.id
            await self._handle_open_time_input(session, message)
            return

        if message.channel.id != session.game_ch_id:
            return
        # v7.21.4: 세션 첫 생성의 유지 시간 답변(게임 채널) — 디스플레이 답변과 같은 1회 소비·해석 경로
        if core.session_flow.awaiting_open_here(session, message.channel.id):
            if (str(message.author.id) not in (session.players or {})
                    and not await self.bot.is_owner(message.author)):
                return
            session.awaiting_display_input = False
            session._display_input_msg_id = message.id
            await self._handle_open_time_input(session, message)
            return
        # WP-D(D-D4): 커밋 복구 admission은 새 자동 턴 eligibility(gm_active·턴/비용 한도)보다
        #   먼저 평가한다 — 마지막 턴이 영속 후 청구/확정 단계에서 멈췄어도 재시작 없이 복구된다.
        if getattr(session, "commit_recovery", None):
            asyncio.create_task(self._recovery_admission(session))
            return
        if not getattr(session, "gm_active", False):
            return

        user_id_str = str(message.author.id)
        char_name = session.players.get(user_id_str, {}).get("name")
        waiting_for = getattr(session, "gm_waiting_for", None)

        if waiting_for:
            # GM이 특정 PC의 발언을 기다리는 중
            if char_name != waiting_for:
                # 다른 플레이어 발언 무시
                return
            asyncio.create_task(self._handle_waiting_response(session, message, char_name))
        else:
            # 대기 상태 없음 → 기존 자발 발언 즉각 처리 (하위 호환)
            asyncio.create_task(self._handle_player_message(session, message))

    @commands.command(name="되감기")
    async def rewind_cmd(self, ctx, turn: str = None):
        """
        되감기 실행 (마스터 채널).

        NOTE: 디스플레이 채널 UI 도입 전까지의 임시 진입점.
              디스플레이 완성 후에는 RewindView 버튼이 주 경로가 된다.

        사용법:
            !되감기        — 1턴 되감기 (직전 턴 제거)
            !되감기 8      — 8턴 종료 시점으로 되감기
            !되감기 범위    — 되감기 가능 범위 확인
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session:
            await ctx.send("이 채널에 연결된 세션이 없습니다.")
            return

        oldest, newest = core.available_range(session)
        if newest == 0:
            await ctx.send("되감기 기록이 아직 없습니다. (4.6.0 이후 진행한 턴부터 기록됩니다)")
            return

        if turn == "범위":
            await ctx.send(
                f"⏪ 되감기 가능 범위: **{oldest}~{newest - 1}턴** "
                f"(현재 {newest}턴 / 상한 {core.REWIND_MAX_TURNS}턴)"
            )
            return

        if turn is None:
            target = newest - 1
        else:
            try:
                target = int(turn)
            except ValueError:
                await ctx.send("사용법: `!되감기` / `!되감기 [턴번호]` / `!되감기 범위`")
                return

        view = RewindConfirmView(self.bot, session, target)
        view.bind(await ctx.send(
            f"⚠️ **{target}턴 종료 시점으로 되돌립니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
            f"제거되는 정보는 되감기 로그로 이관됩니다.",
            view=view,
        ))

    @auto.command(name="원장")
    async def show_info_ledger(self, ctx):
        """현재 정보 인지 원장(비공개 정보별 인지 주체)을 마스터 채널에 출력."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if core.player_world.legacy_narrative_retired(session):
            # v7.35(S1): 동적 세계는 레거시 원장 대신 지금 정본이 추적하는 요소를 요약한다
            for e in core.master_report.pack("📂 정본 추적 요소", core.master_report.COLOR_DELTA,
                                             core.master_report.tracked_sections(session),
                                             description="비밀 원문은 턴 보고의 [🔒 비밀 원문] 버튼"):
                await ctx.send(embed=e)
            return
        ledger = getattr(session, "info_ledger", []) or []
        if not ledger:
            return await ctx.send(
                "📂 정보 인지 원장이 비어 있습니다.\n"
                "GM이 비공개·플롯 관련 정보를 감지하면 이 원장에 '누가 아는가'가 누적됩니다."
            )

        embed = discord.Embed(
            title="📂 정보 인지 원장",
            description="비공개·플롯 정보별로 '누가 아는가'가 기록됩니다. GM은 이 기록에 근거해 NPC·적의 앎/모름을 판정합니다.",
            color=0x9B59B6,
        )
        for item in ledger[-10:]:
            known = ", ".join(item.get("known_by", [])) or "(없음)"
            susp = ", ".join(item.get("suspected_by", [])) or "(없음)"
            leaks = item.get("leaks", [])
            val = f"**확지**: {known}\n**추정**: {susp}"
            if item.get("origin"):
                val += f"\n**출처**: {item['origin']}"
            if leaks:
                val += "\n**유출**: " + " / ".join(leaks[-3:])
            embed.add_field(name=f"🔒 {item.get('info', '?')[:200]}", value=val[:1020], inline=False)
        embed.set_footer(text=f"총 {len(ledger)}건 기록 (최근 10건 표시)")
        await ctx.send(embed=embed)


async def setup(bot):
    """디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수."""
    await bot.add_cog(GMCog(bot))

    # persistent view 등록 — 봇 재시작 후에도 추출 재시도 버튼이 동작하도록 한다.
    # 추출 실패는 다음 턴을 차단하므로, 버튼이 죽으면 세션이 영구 정지한다.
    # 중복 등록은 무해하지만 방어적으로 플래그를 둔다.
    if not getattr(bot, "_extraction_view_registered", False):
        bot.add_view(ExtractionRetryView(bot))
        bot.add_dynamic_items(core.master_report.SecretButton)     # v7.30: 전수보고 비밀 원문(재시작 뒤에도)
        bot.add_view(RewindView(bot))
        bot.add_view(core.DisplayView(bot))
        bot.add_view(core.GMHomeView(bot))
        bot._extraction_view_registered = True
