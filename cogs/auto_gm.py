import re
import json
import random
import asyncio
import discord
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core

# 프롬프트 중앙 등록소에서 AI 프롬프트 상수 및 빌더 임포트
from prompts import (
    GM_LOGIC_RESPONSE_SCHEMA,
    GM_LOGIC_SYSTEM_INSTRUCTION,
    NARRATIVE_PLAN_SCHEMA,
    NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
    build_narrate_prompt,
    # 방안 B — 세계 물리 타임라인
    WORLD_TIMELINE_EXTRACTION_SCHEMA,
    WORLD_TIMELINE_EXTRACTOR_SYSTEM_INSTRUCTION,
    # 방안 E — PROCEED 자기 검증
    PROCEED_VERIFY_SCHEMA,
    PROCEED_VERIFIER_SYSTEM_INSTRUCTION,
    # 방안 6 — 2단계 사고 서사 방향성 시뮬레이터
    NARRATIVE_DIRECTION_SCHEMA,
    NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION,
)


# ========== [자동 GM 모드 상수] ==========
# NOTE: GM-Logic 호출 시 한 플레이어 발언당 내부 루프 반복 상한.
MAX_ITERATIONS_PER_MESSAGE = 5

# NOTE: 같은 플레이어 발언에 대한 ASK 누적 상한. 초과 시 강제 PROCEED.
MAX_CLARIFY_PER_MESSAGE = 2

# NOTE: 같은 플레이어 발언에 대한 NARRATE 누적 상한. 초과 시 강제 PROCEED.
MAX_NARRATE_PER_MESSAGE = 7

# NOTE: 자동 GM 비용 로그 라벨에 부착하는 접두사.
COST_LOG_PREFIX = "[AUTO] "


# NOTE: GM_LOGIC_RESPONSE_SCHEMA, GM_LOGIC_SYSTEM_INSTRUCTION,
#       NARRATIVE_PLAN_SCHEMA, NARRATIVE_PLANNER_SYSTEM_INSTRUCTION 은
#       prompts.py로 이동. 위 import 블록에서 불러옵니다.


# ========== [유틸리티 함수] ==========
def _clean_proceed_instruction(instruction: str) -> str:
    """
    GM-Logic이 생성한 proceed_instruction에서 마크다운 서식을 제거하고 단일 자연어 서술문으로 정제.
    """
    if not instruction:
        return ""
    lines = instruction.strip().splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r'^[#\s]+', '', line)
        line = re.sub(r'^[-*+>\s]+(?=[^\s])', '', line)
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
        line = re.sub(r'\*([^*]+)\*', r'\1', line)
        line = line.strip()
        if line:
            cleaned.append(line)
    result = ' '.join(cleaned)
    return re.sub(r'\s+', ' ', result).strip()


def _build_logic_user_prompt(session, player_message: str, roll_results: list,
                              sim_result: dict | None = None) -> str:
    """
    GM-Logic 호출용 사용자 프롬프트 조립.

    Args:
        session: TRPGSession
        player_message (str): 플레이어 신규 발언 (멀티플레이어 시 종합 텍스트)
        roll_results (list[str]): 직전 ROLL 결과 문자열 목록 (재호출 시 누적)
        sim_result (dict | None): 방안 6 서사 시뮬레이터 결과. None이면 블록 생략.
    """
    target_char = session.auto_gm_target_char or "(미지정)"
    side_note = session.auto_gm_side_note or ""
    clarify_count = session.auto_gm_clarify_count
    narrate_count = getattr(session, "auto_gm_narrate_count", 0)

    # 최근 6턴 요약 (raw_logs 마지막 6개)
    recent_logs_lines = []
    for content in session.raw_logs[-6:]:
        try:
            text = content.parts[0].text
            role = content.role.upper()
            preview = text[:280] + ("..." if len(text) > 280 else "")
            recent_logs_lines.append(f"[{role}]\n{preview}")
        except Exception:
            continue
    recent_logs_str = "\n\n".join(recent_logs_lines) if recent_logs_lines else "(최근 로그 없음)"

    # PC 프로필 요약 (스탯명만)
    pc_profile_summary = ""
    for uid, p in session.players.items():
        if p.get("name") == target_char:
            stats = ", ".join([f"{k}:{v}" for k, v in p.get("profile", {}).items() if isinstance(v, (int, str))])
            pc_profile_summary = stats
            break

    # 스탯 적용 분야 설명 (시나리오에 stat_descriptions 가 있으면 주입 — ROLL 판정 스탯 선택 보조)
    stat_descs: dict = session.scenario_data.get("stat_descriptions") or {}
    stat_desc_line = ""
    if stat_descs:
        stat_desc_line = "  (" + " / ".join([f"{k}: {v}" for k, v in stat_descs.items()]) + ")"

    # 자원·상태
    res = session.resources.get(target_char, {}) if target_char else {}
    sta = session.statuses.get(target_char, []) if target_char else []
    res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "(없음)"
    sta_str = ", ".join(sta) or "(없음)"

    # 서사 계획 블록 (2단계: mid_plan + 순간 계획)
    narrative_plan = getattr(session, "narrative_plan", {})
    if narrative_plan:
        current = narrative_plan.get("current_event", {})
        next_ev = narrative_plan.get("next_event", {})
        mid     = narrative_plan.get("mid_plan", {})

        if mid:
            milestones = mid.get("milestones", [])
            ms_str = " → ".join(milestones) if milestones else "(없음)"
            mid_block = (
                f"■ 중규모 진행 방향: {mid.get('title', '?')}\n"
                f"  · 전체 흐름: {mid.get('overview', '')}\n"
                f"  · 이정표 순서: {ms_str}\n"
                f"  · 완료 조건: {mid.get('end_condition', '')}\n"
            )
        else:
            mid_block = ""

        narrative_block = (
            "\n[현재 서사 계획 — proceed_instruction 및 event_assessment 결정 시 반드시 참고]\n"
            + mid_block +
            f"■ 현재 순간 사건: {current.get('title', '?')}\n"
            f"  · 상황: {current.get('summary', '')}\n"
            f"  · 마무리 방향: {current.get('resolution_direction', '')}\n"
            f"■ 다음 순간 사건 (참고용): {next_ev.get('title', '?')}\n"
            f"  · 개요: {next_ev.get('summary', '')}\n"
            f"  · 시작 조건: {next_ev.get('trigger', '')}\n"
        )
    else:
        narrative_block = ""

    roll_block = ""
    if roll_results:
        roll_block = "\n[직전 굴림 결과 (반드시 반영하여 PROCEED를 작성)]\n" + "\n".join(roll_results)

    note_block = f"\n[GM 사이드 노트 (이번 턴 적용)]\n{side_note}\n" if side_note else ""

    # 최근 5회 PROCEED 이력 블록 조립
    # NOTE: 각 PROCEED의 지시사항 + 중간 컨텍스트(NARRATE/ASK/ROLL) + AI 묘사 출력 요약 포함.
    # GM-Logic이 직전 묘사 흐름을 인지하여 동일 상황 반복·정체를 방지하기 위함.
    proceed_history = getattr(session, "auto_gm_proceed_history", [])
    if proceed_history:
        ph_lines = []
        for i, entry in enumerate(proceed_history):
            turn_num = entry.get("turn_num", "?")
            instr = entry.get("instruction", "(없음)")
            ctx = entry.get("context", [])
            ai_out = entry.get("ai_summary", "")
            tag = "← 직전 PROCEED" if i == len(proceed_history) - 1 else ""
            ph_lines.append(f"  ─ PROCEED #{i + 1} (턴 {turn_num}) {tag}")
            ph_lines.append(f"    [지시사항] {instr[:200]}")
            if ctx:
                ph_lines.append("    [중간 컨텍스트 (NARRATE/ASK/ROLL)]")
                for c in ctx[:8]:  # 최대 8줄
                    ph_lines.append(f"      {c[:130]}")
            if ai_out:
                ph_lines.append("    [AI 묘사 출력 요약]")
                ph_lines.append(f"      {ai_out[:400]}")
        proceed_history_block = (
            "\n[최근 PROCEED 이력 — 반복·정체 방지 참고]\n"
            + "\n".join(ph_lines)
            + "\n"
        )
    else:
        proceed_history_block = ""

    # NOTE: 이번 턴에 누적된 플레이어 발언·ASK 브리지·주사위 결과를 컨텍스트에 포함.
    # ASK→플레이어 응답→ASK→... 연쇄 대화를 GM-Logic이 인지해야 중복 질문을 방지할 수 있음.
    current_turn_block = ""
    if session.current_turn_logs:
        current_turn_block = (
            "\n[이번 턴 누적 대화 (현재 PROCEED 이전까지 발생한 발언·GM 질문·판정)]\n"
            + "\n".join(session.current_turn_logs)
            + "\n"
        )

    # 장소 이미지 목록 (상: 태그 사용 시 이 목록에서만 선택 가능)
    location_images: dict = session.scenario_data.get("location_images", {})
    if location_images:
        loc_lines = [f"  - {kw}: {desc}" for kw, desc in location_images.items()]
        location_images_block = (
            "\n[사용 가능한 장소 이미지 목록 — 상:키워드 태그 선택 시 이 목록에서만 고를 것]\n"
            "(새로운 장면·장소로 전환될 때 PROCEED의 proceed_instruction 맨 앞에 '상:키워드'를 삽입하라.)\n"
            + "\n".join(loc_lines) + "\n"
        )
    else:
        location_images_block = ""

    # 유효 상태이상 목록 (태: 태그 사용 시 이 목록에서만 선택 가능)
    merged_statuses = core.get_merged_status_effects(session.scenario_data)
    if merged_statuses:
        status_list_lines = []
        for sname, seff in merged_statuses.items():
            w = seff.get("weight", 0)
            w_str = f"가중치 {w:+d}" if w != 0 else "가중치 없음"
            status_list_lines.append(f"  - {sname}: 적용조건=[{seff.get('apply_condition', '')}] / {w_str} / 제거조건=[{seff.get('remove_condition', '')}]")
        valid_status_block = (
            "\n[유효 상태이상 목록 — 태: 태그는 이 목록에 있는 이름만 사용 가능]\n"
            + "\n".join(status_list_lines) + "\n"
        )
    else:
        valid_status_block = ""

    # 압축 기억 (이전 턴 맥락 — 초기 장면·지난 사건 요약 포함)
    _mem = (
        session.compressed_memory
        or getattr(session, "cached_compressed_memory", "")
        or ""
    )
    memory_block = f"\n[압축 기억 — 이전 턴 요약]\n{_mem[:800]}\n" if _mem else ""

    # 세계 물리 타임라인 블록 (방안 B)
    world_tl = getattr(session, "world_timeline", {})
    if world_tl:
        world_tl_block = (
            "\n[현재 세계 상태 — 세력 배치·지역 규칙 기반 개연성 판단의 기준]\n"
            f"위치: {world_tl.get('current_location', '(미확인)')}\n"
            f"시간대: {world_tl.get('time_of_day', '(미확인)')} "
            f"| 날씨: {world_tl.get('weather', '(미확인)')}\n"
            f"세력/지역 컨텍스트: {world_tl.get('faction_context', '(미확인)')}\n"
            f"알려진 위협: {world_tl.get('known_threats', '없음')}\n"
            f"환경: {world_tl.get('environmental_note', '없음')}\n"
            f"(마지막 갱신: 턴 {world_tl.get('last_updated_turn', '?')})\n"
        )
    else:
        world_tl_block = ""

    # 서사 방향성 시뮬레이션 블록 (방안 6)
    if sim_result:
        world_analysis = sim_result.get("world_state_analysis", "")
        dirs = sim_result.get("directions", [])
        if dirs:
            dir_lines = []
            for d in dirs:
                p = d.get("plausibility", "?")
                plaus_label = {"high": "🟢높음", "medium": "🟡중간",
                               "low": "🔴낮음", "impossible": "⛔불가"}.get(p, p)
                dir_lines.append(
                    f"  [{plaus_label}] {d.get('title', '?')}: {d.get('description', '')}\n"
                    f"    근거: {d.get('world_basis', '')}\n"
                    + (f"    제약: {d.get('narrative_constraint', '')}\n"
                       if d.get("narrative_constraint") else "")
                )
            sim_block = (
                "\n[서사 방향성 시뮬레이션 — 세계관 논리 기반 개연성 판단 참고]\n"
                + (f"세계 상태: {world_analysis}\n" if world_analysis else "")
                + "\n".join(dir_lines)
                + "\n※ impossible 방향은 세계관상 발생 불가. "
                  "GM-Logic은 이 분석을 proceed_instruction 및 event_assessment에 반드시 반영할 것.\n"
            )
        else:
            sim_block = ""
    else:
        sim_block = ""

    # 멀티플레이어 정보 (여러 PC가 있을 때 모두 표시)
    target_chars = getattr(session, "auto_gm_target_chars", [])
    multi_info = ""
    if len(target_chars) > 1:
        pc_lines = []
        for cn in target_chars:
            r = session.resources.get(cn, {})
            s = session.statuses.get(cn, [])
            r_str = ", ".join([f"{k}:{v}" for k, v in r.items()]) or "없음"
            s_str = ", ".join(s) or "없음"
            pc_lines.append(f"  - {cn}: 자원={r_str} / 상태={s_str}")
        multi_info = "\n[참가 PC 전체 상태]\n" + "\n".join(pc_lines) + "\n"

    return f"""[현재 턴 #]: {session.turn_count + 1}
[대상 PC]: {target_char}
[PC 프로필]: {pc_profile_summary or "(미설정)"}{stat_desc_line}
[PC 자원]: {res_str}
[PC 상태]: {sta_str}
[직전 ASK 횟수 / 한도]: {clarify_count} / {MAX_CLARIFY_PER_MESSAGE}
[직전 NARRATE 횟수 / 한도]: {narrate_count} / {MAX_NARRATE_PER_MESSAGE}
{multi_info}{note_block}{world_tl_block}{memory_block}
[최근 6턴 컨텍스트]
{recent_logs_str}
{current_turn_block}{proceed_history_block}{narrative_block}{sim_block}{location_images_block}{valid_status_block}
{roll_block}

[플레이어 신규 발언]
{player_message}

위 컨텍스트를 분석하여 다음 단일 action(ASK / NARRATE / ROLL / PROCEED)을 결정하고 JSON 스키마에 맞춰 응답하십시오."""


# ========== [자동 GM 주사위 버튼 View] ==========
class AutoGMRollView(discord.ui.View):
    """
    자동 GM 모드에서 ROLL 판정 시 플레이어에게 주사위 버튼을 제공하는 View.
    """

    def __init__(self, cog, session, roll_specs: list, player_message: str,
                 prior_roll_results: list, target_uid: str | None):
        super().__init__(timeout=300)
        self.cog = cog
        self.session = session
        self.roll_specs = roll_specs
        self.player_message = player_message
        self.prior_roll_results = list(prior_roll_results)
        self.target_uid = target_uid
        self._resolved = False

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
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

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="> ⏳ 판정 결과를 처리 중입니다...", view=self
        )
        self.stop()

        asyncio.create_task(self._process_roll(interaction.channel))

    async def _process_roll(self, game_ch):
        new_results = await self.cog._execute_rolls(self.session, self.roll_specs, game_ch)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(self.session, self.player_message, combined)
        )

    async def on_timeout(self):
        if self._resolved:
            return
        self._resolved = True

        master_ch = self.cog.bot.get_channel(self.session.master_ch_id)
        if master_ch:
            await master_ch.send(
                "⚠️ **[자동 GM]** 판정 버튼 시간 초과(5분). 주사위를 자동으로 굴립니다."
            )
        game_ch = self.cog.bot.get_channel(self.session.game_ch_id)
        new_results = await self.cog._execute_rolls(self.session, self.roll_specs, game_ch)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(self.session, self.player_message, combined)
        )


# ========== [자동 GM Cog] ==========
class AutoGMCog(commands.Cog):
    """
    게임 채널의 플레이어 발언을 받아 AI가 GM 역할을 자동 수행하는 옵트인 모드.

    PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 물어보는 라운드 수집 시스템을 포함.
    멀티플레이어 지원: 등록된 모든 PC에게 순서대로 행동을 물어본 뒤 종합하여 GM-Logic 호출.
    """

    def __init__(self, bot):
        self.bot = bot
        self._session_locks = {}

    def _lock_for(self, session):
        if session.session_id not in self._session_locks:
            self._session_locks[session.session_id] = asyncio.Lock()
        return self._session_locks[session.session_id]

    # ─────────────────────────────────────────────────────────────
    # 명령어
    # ─────────────────────────────────────────────────────────────

    @commands.command(name="자동시작")
    async def auto_start(self, ctx, *target_char_args: str):
        """
        자동 GM 모드 활성화. 인자 없으면 등록된 모든 PC를 대상으로 함.
        멀티플레이어 시 !자동시작, 특정 PC만 지정 시 !자동시작 이름1 이름2 형태로 사용.
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

        session.auto_gm_active = True
        session.auto_gm_target_chars = target_chars
        session.auto_gm_target_char = target_chars[0]   # 하위 호환성 (GM-Logic 단일 PC 참조용)
        session.auto_gm_turns_done = 0
        session.auto_gm_clarify_count = 0
        session.auto_gm_cost_baseline = session.total_cost
        session.auto_gm_side_note = ""
        session.auto_gm_pending_players = []
        session.auto_gm_collected_actions = {}
        session.auto_gm_waiting_for = None
        # NOTE: 최근 5회 PROCEED 이력 (지시사항 + 중간 컨텍스트 + AI 출력 요약).
        # GM-Logic 프롬프트에 주입되어 서사 반복·정체를 방지한다. 봇 재시작 시 초기화 허용.
        session.auto_gm_proceed_history = []
        await core.save_session_data(self.bot, session)

        has_existing_plan = bool(session.narrative_plan)
        plan_note = (
            f"- 서사 계획: 기존 계획 유지 (v{session.narrative_plan.get('plan_version', '?')})"
            if has_existing_plan else
            "- 서사 계획: 수립 중... (백그라운드에서 진행)"
        )
        await ctx.send(
            f"🤖 **[자동 GM 모드 활성화]**\n"
            f"- 대상 PC: **{', '.join(target_chars)}**\n"
            f"- 자동 턴 한도: {session.auto_gm_turn_cap}턴\n"
            f"- 자동 누적 비용 한도: {core.format_cost(session.auto_gm_cost_cap_krw)}\n"
            f"{plan_note}\n"
            f"- PROCEED 완료 후 GM이 선제적으로 행동을 물어봅니다.\n"
            f"- 중단: `!자동중단`  /  GM에게 메모: `!자동개입 [텍스트]`\n"
            f"- 서사 확인: `!서사계획`  /  강제 재계획: `!서사재계획`"
        )

        # 활성화 직후: 서사 계획 수립 후 첫 라운드 시작 (백그라운드 태스크)
        asyncio.create_task(self._init_narrative_and_start(session))

    @commands.command(name="자동중단")
    async def auto_stop(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "auto_gm_active", False):
            return await ctx.send("⚠️ 자동 GM 모드가 활성 상태가 아닙니다.")

        session.auto_gm_active = False
        session.auto_gm_waiting_for = None
        session.auto_gm_pending_players = []
        await core.save_session_data(self.bot, session)

        used = session.total_cost - session.auto_gm_cost_baseline
        await ctx.send(
            f"🛑 **[자동 GM 모드 정지]**\n"
            f"- 자동 처리 턴: {session.auto_gm_turns_done}턴\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)}\n"
            f"- 인간 GM 명령어 입력 모드로 복귀합니다."
        )

    @commands.command(name="자동상태")
    async def auto_status(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        active = getattr(session, "auto_gm_active", False)
        used = session.total_cost - getattr(session, "auto_gm_cost_baseline", 0.0)
        target_chars = getattr(session, "auto_gm_target_chars", [])
        waiting = getattr(session, "auto_gm_waiting_for", None)
        pending = getattr(session, "auto_gm_pending_players", [])
        collected = getattr(session, "auto_gm_collected_actions", {})

        collected_str = "\n".join([f"    · {k}: {v[:40]}" for k, v in collected.items()]) or "    (없음)"
        await ctx.send(
            f"🤖 **[자동 GM 상태]**\n"
            f"- 활성: {'✅ 켜짐' if active else '⛔ 꺼짐'}\n"
            f"- 대상 PC: {', '.join(target_chars) if target_chars else '(없음)'}\n"
            f"- 자동 처리 턴: {session.auto_gm_turns_done} / {session.auto_gm_turn_cap}\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)} / {core.format_cost(session.auto_gm_cost_cap_krw)}\n"
            f"- 현재 발언 대기 PC: {waiting or '(없음)'}\n"
            f"- 응답 대기 중인 PC: {', '.join(pending) if pending else '(없음)'}\n"
            f"- 수집된 행동:\n{collected_str}\n"
            f"- 직전 ASK 횟수: {session.auto_gm_clarify_count}\n"
            f"- 대기 중 사이드 노트: {session.auto_gm_side_note or '(없음)'}"
        )

    @commands.command(name="자동개입")
    async def auto_inject(self, ctx, *, text: str = ""):
        """다음 PROCEED 완료 시까지 GM 사이드 노트를 유지."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not text.strip():
            return await ctx.send("⚠️ 사용법: `!자동개입 [GM에게 전달할 메모]`")

        session.auto_gm_side_note = text.strip()
        await core.save_session_data(self.bot, session)
        await ctx.send(
            f"📝 사이드 노트 등록 (다음 PROCEED(턴 진행) 완료 시까지 유지):\n> {text.strip()}"
        )

    @commands.command(name="자동턴제한")
    async def auto_set_cap(self, ctx, n: int = None):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if n is None or n < 1 or n > 100:
            return await ctx.send("⚠️ 사용법: `!자동턴제한 [1~100]`")

        session.auto_gm_turn_cap = n
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 턴 한도를 {n}턴으로 변경했습니다.")

    # ─────────────────────────────────────────────────────────────
    # 메시지 리스너
    # ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.content.startswith("!"):
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return
        if message.channel.id != session.game_ch_id:
            return
        if not getattr(session, "auto_gm_active", False):
            return

        user_id_str = str(message.author.id)
        char_name = session.players.get(user_id_str, {}).get("name")
        waiting_for = getattr(session, "auto_gm_waiting_for", None)

        if waiting_for:
            # GM이 특정 PC의 발언을 기다리는 중
            if char_name != waiting_for:
                # 다른 플레이어 발언 무시
                return
            asyncio.create_task(self._handle_waiting_response(session, message, char_name))
        else:
            # 대기 상태 없음 → 기존 자발 발언 즉각 처리 (하위 호환)
            asyncio.create_task(self._handle_player_message(session, message))

    # ─────────────────────────────────────────────────────────────
    # 라운드 수집 시스템 (선제 행동 질문 — #22)
    # ─────────────────────────────────────────────────────────────

    async def _start_round(self, session):
        """
        PROCEED 완료 후(또는 자동시작 직후) 호출. 모든 대상 PC에게 순서대로 행동을 묻는 라운드 시작.
        auto_gm_target_chars가 비어 있으면 아무 동작도 하지 않음.
        """
        if not getattr(session, "auto_gm_active", False):
            return
        target_chars = getattr(session, "auto_gm_target_chars", [])
        if not target_chars:
            return

        session.auto_gm_pending_players = list(target_chars)
        session.auto_gm_collected_actions = {}
        session.auto_gm_waiting_for = None
        session.auto_gm_narrate_count = 0
        await core.save_session_data(self.bot, session)
        await self._ask_next_player(session)

    async def _ask_next_player(self, session):
        """
        auto_gm_pending_players에서 다음 PC를 꺼내 행동 요청 메시지를 게임 채널에 전송.
        목록이 비어 있으면 수집 완료로 처리.
        """
        game_ch = self.bot.get_channel(session.game_ch_id)

        if not session.auto_gm_pending_players:
            # 모든 PC의 행동이 수집됨 → GM-Logic 호출
            await self._finalize_round_and_process(session)
            return

        next_char = session.auto_gm_pending_players.pop(0)
        session.auto_gm_waiting_for = next_char

        uid = core.get_uid_by_char_name(session, next_char)
        mention = f"<@{uid}>" if uid else f"**{next_char}**"

        collected = session.auto_gm_collected_actions
        if collected:
            # 이미 다른 PC의 행동이 수집된 상태 → 간단히 참고 표시
            others = "、".join([
                f"{k}: '{v[:20]}...'" if len(v) > 20 else f"{k}: '{v}'"
                for k, v in collected.items()
            ])
            prompt = f"{mention}, 현재까지 행동 선언: {others}\n{mention}은(는) 어떻게 하시겠습니까?"
        else:
            prompt = f"{mention}님 턴입니다. 어떤 행동을 하시겠습니까?"

        if game_ch:
            await core.stream_text_to_channel(
                self.bot, game_ch, f"> {prompt}",
                words_per_tick=8, tick_interval=0.8
            )
        # 선제 질문도 current_turn_logs에 기록
        session.current_turn_logs.append(f"[진행자 (자동 GM)]: {prompt}")
        await core.save_session_data(self.bot, session)

    async def _handle_waiting_response(self, session, message: discord.Message, char_name: str):
        """
        GM의 선제 행동 질문에 대한 플레이어 응답 수집. 모든 PC 수집 완료 시 GM-Logic 호출.
        """
        async with self._lock_for(session):
            # 이미 다른 처리가 완료된 경우 스킵
            if session.auto_gm_waiting_for != char_name:
                return
            if not session.auto_gm_active:
                return

            session.auto_gm_waiting_for = None
            content = message.content.strip()

            # 행동 수집
            session.auto_gm_collected_actions[char_name] = content
            session.current_turn_logs.append(f"[{char_name}]: {content}")

            if session.auto_gm_pending_players:
                # 아직 응답 안 한 PC가 있음 → 다음 PC에게 질문
                await core.save_session_data(self.bot, session)
                await self._ask_next_player(session)
            else:
                # 모든 PC 수집 완료
                await self._finalize_round_and_process(session)

    async def _finalize_round_and_process(self, session):
        """
        모든 PC의 행동이 수집된 후 종합하여 GM-Logic을 호출.
        단일 PC면 그대로, 멀티 PC면 종합 메시지 생성 + 게임 채널에 요약 표시.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        actions = session.auto_gm_collected_actions.copy()
        session.auto_gm_collected_actions = {}
        session.auto_gm_clarify_count = 0

        if not actions:
            return

        if len(actions) == 1:
            player_message = list(actions.values())[0]
        else:
            # 멀티플레이어 — 게임 채널에 행동 종합 표시
            summary_lines = "\n".join([f"> **{k}**: {v}" for k, v in actions.items()])
            if game_ch:
                await game_ch.send(f"> 📋 **행동 선언 종합:**\n{summary_lines}")
                core.write_log(session.session_id, "game_chat",
                               f"[행동 종합]: {'; '.join([f'{k}: {v}' for k, v in actions.items()])}")
            player_message = "\n".join([f"[{k}]: {v}" for k, v in actions.items()])
            # 대표 PC를 첫 번째 PC로 업데이트
            first_char = list(actions.keys())[0]
            session.auto_gm_target_char = first_char

        await self._process_actions(session, player_message, master_ch)

    # ─────────────────────────────────────────────────────────────
    # 안전장치 + GM-Logic 루프 진입점
    # ─────────────────────────────────────────────────────────────

    async def _handle_player_message(self, session, message: discord.Message):
        """
        기존 자발적 플레이어 발언 처리 경로 (auto_gm_waiting_for 없을 때).
        락 획득 후 _process_actions 호출.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.auto_gm_active:
                return

            if session.auto_gm_turns_done >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            session.auto_gm_clarify_count = 0
            char_name = session.auto_gm_target_char or message.author.display_name
            session.current_turn_logs.append(f"[{char_name}]: {message.content.strip()}")

            await self._process_actions(session, message.content.strip(), master_ch)

    async def _process_actions(self, session, player_message: str, master_ch):
        """
        안전장치 확인 후 GM-Logic 루프(_run_gm_logic_loop) 호출.
        _handle_player_message와 _finalize_round_and_process의 공통 진입 경로.
        이미 락 안에서 호출된다고 가정하므로 이 함수 내부에는 락 없음.
        """
        if not session.auto_gm_active:
            return

        if session.auto_gm_turns_done >= session.auto_gm_turn_cap:
            session.auto_gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        used_cost = session.total_cost - session.auto_gm_cost_baseline
        if used_cost >= session.auto_gm_cost_cap_krw:
            session.auto_gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        await self._run_gm_logic_loop(session, player_message, master_ch)

    # ─────────────────────────────────────────────────────────────
    # GM-Logic 루프 본체
    # ─────────────────────────────────────────────────────────────

    async def _run_gm_logic_loop(self, session, player_message: str, master_ch):
        """
        GM-Logic ASK / ROLL / PROCEED 루프.
        PROCEED 완료 후 자동으로 _start_round()를 호출하여 다음 라운드(선제 행동 질문)를 시작.

        NOTE: 이 함수는 락 없이 실행됨. 호출 측에서 이미 락을 잡고 있거나,
              비동기 태스크로 독립 실행되는 경우(버튼 콜백 등)에 사용.
        """
        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        action_labels = {
            "ASK":     "🟡 ASK (명확화 요청)",
            "NARRATE": "💬 NARRATE (경량 GM 응답)",
            "ROLL":    "🎲 ROLL (주사위 판정)",
            "PROCEED": "🟢 PROCEED (턴 진행)",
        }

        roll_results: list[str] = []

        game_ch = self.bot.get_channel(session.game_ch_id)

        # ── 방안 6: 서사 방향성 시뮬레이션 (첫 번째 GM-Logic 호출 전) ──
        # 캐시가 유효할 때만 실행 (세계관 문맥이 없으면 고차원 판단 불가)
        sim_result: dict | None = None
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        if cache_name and cache_model == core.DEFAULT_MODEL:
            try:
                sim_result = await self._simulate_narrative_directions(session, player_message, master_ch)
            except Exception as e:
                print(f"[AutoGM] 서사 시뮬레이션 실패(무시): {e}")
                sim_result = None

        for iteration in range(MAX_ITERATIONS_PER_MESSAGE):
            # 첫 번째 호출에만 sim_result 주입, 이후는 None (중복 비용 방지)
            current_sim = sim_result if iteration == 0 else None

            # GM-Logic 호출 동안 게임 채널에 입력 중 표시 (PROCEED와 일관성)
            if game_ch:
                async with game_ch.typing():
                    decision = await self._call_gm_logic(session, player_message, roll_results,
                                                          master_ch, sim_result=current_sim)
            else:
                decision = await self._call_gm_logic(session, player_message, roll_results,
                                                      master_ch, sim_result=current_sim)

            if not decision:
                await m_send("⚠️ 자동 GM 결정 호출 실패. 이번 발언을 스킵합니다.")
                return

            action = decision.get("action", "ASK").upper()
            reasoning = decision.get("reasoning", "")

            label = action_labels.get(action, action)
            print(f"[AutoGM/{session.session_id}] iter={iteration} action={action} :: {reasoning[:120]}")
            await m_send(
                f"🤖 **[자동 GM 판단 #{iteration + 1}]** {label}\n"
                f"> {reasoning[:200]}"
            )

            # ── ASK ──
            if action == "ASK":
                session.auto_gm_clarify_count += 1
                if session.auto_gm_clarify_count > MAX_CLARIFY_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[자동 GM]** ASK 한도({MAX_CLARIFY_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = _clean_proceed_instruction(
                        decision.get("proceed_instruction") or
                        "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                    )
                    if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                        session.auto_gm_active = False
                        await m_send(
                            f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                        )
                    await self._dispatch_proceed(session, forced_instr)
                    session.auto_gm_clarify_count = 0
                    session.auto_gm_narrate_count = 0
                    session.auto_gm_turns_done += 1
                    session.auto_gm_side_note = ""
                    await core.save_session_data(self.bot, session)
                    if session.auto_gm_active:
                        await self._start_round(session)
                    return

                bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                if game_ch:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, bridge,
                        words_per_tick=5, tick_interval=1.5
                    )
                # ASK 브리지를 current_turn_logs에 기록 → 다음 GM-Logic 호출 시 맥락 유지
                session.current_turn_logs.append(f"[진행자 (자동 GM)]: {bridge}")
                print(f"[AutoGM/{session.session_id}] ASK -> '{bridge[:80]}'")
                await core.save_session_data(self.bot, session)
                break

            # ── NARRATE ──
            elif action == "NARRATE":
                session.auto_gm_narrate_count = getattr(session, "auto_gm_narrate_count", 0) + 1
                if session.auto_gm_narrate_count > MAX_NARRATE_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[자동 GM]** NARRATE 한도({MAX_NARRATE_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = _clean_proceed_instruction(
                        decision.get("proceed_instruction") or
                        "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                    )
                    if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                        session.auto_gm_active = False
                        await m_send(
                            f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                        )
                    await self._dispatch_proceed(session, forced_instr)
                    session.auto_gm_narrate_count = 0
                    session.auto_gm_turns_done += 1
                    session.auto_gm_side_note = ""
                    await core.save_session_data(self.bot, session)
                    if session.auto_gm_active:
                        await self._start_round(session)
                    return

                narrate_instr = decision.get("narrate_instruction") or "현재 상황을 간략히 설명하십시오."
                # NOTE: typing 컨텍스트는 _dispatch_narrate 내부 API 호출 블록에서만 활성화됨.
                # 외부에서 typing()으로 감싸면 stream_text_to_channel 실행 시 typing이 살아있어
                # Discord 상충으로 스트리밍이 멈추는 버그 발생 — 외부 typing 제거.
                narrate_text = await self._dispatch_narrate(session, narrate_instr)
                if narrate_text:
                    print(f"[AutoGM/{session.session_id}] NARRATE #{session.auto_gm_narrate_count} -> '{narrate_text[:60]}'")
                await core.save_session_data(self.bot, session)
                break  # 플레이어 응답 대기

            # ── ROLL ──
            elif action == "ROLL":
                rolls = decision.get("rolls") or []
                if not rolls:
                    await m_send(
                        "⚠️ 자동 GM이 ROLL을 선언했으나 굴림 항목이 비어 있어 PROCEED로 폴백합니다."
                    )
                    fallback_instr = _clean_proceed_instruction(
                        decision.get("proceed_instruction") or
                        "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                    )
                    if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                        session.auto_gm_active = False
                        await m_send(
                            f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                        )
                    await self._dispatch_proceed(session, fallback_instr)
                    session.auto_gm_narrate_count = 0
                    session.auto_gm_turns_done += 1
                    session.auto_gm_side_note = ""
                    await core.save_session_data(self.bot, session)
                    if session.auto_gm_active:
                        await self._start_round(session)
                    return

                # 버튼 UI 전송 후 루프 종료 (계속 처리는 버튼 콜백 담당)
                await self._dispatch_rolls(session, rolls, player_message, list(roll_results))
                await core.save_session_data(self.bot, session)
                return

            # ── PROCEED ──
            elif action == "PROCEED":
                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or ""
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                # ── 방안 E: PROCEED 지시사항 자기 검증 ──
                instruction = await self._verify_proceed_instruction(
                    session, instruction, player_message, master_ch
                )

                if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                    session.auto_gm_active = False
                    await m_send(
                        f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                        f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                    )

                await self._dispatch_proceed(session, instruction)

                # 서사 사건 평가 → 진행도 갱신 + 필요 시 재계획
                event_assessment = decision.get("event_assessment", "ongoing")
                await self._update_narrative_progress(session, event_assessment, master_ch)

                session.auto_gm_narrate_count = 0
                session.auto_gm_turns_done += 1
                session.auto_gm_side_note = ""
                await core.save_session_data(self.bot, session)
                # PROCEED 완료 → 다음 라운드 시작 (선제 행동 질문)
                if session.auto_gm_active:
                    await self._start_round(session)
                return

            else:
                await m_send(f"⚠️ 자동 GM이 알 수 없는 action을 반환했습니다: {action}")
                break

        else:
            # 루프 한도 도달 → 강제 PROCEED
            await m_send(f"⚙️ 자동 GM 내부 루프 한도({MAX_ITERATIONS_PER_MESSAGE}) 도달 → 강제 PROCEED.")
            if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )
            await self._dispatch_proceed(session, "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오.")
            session.auto_gm_narrate_count = 0
            session.auto_gm_turns_done += 1
            session.auto_gm_side_note = ""
            await core.save_session_data(self.bot, session)
            if session.auto_gm_active:
                await self._start_round(session)

        await core.save_session_data(self.bot, session)

    # ─────────────────────────────────────────────────────────────
    # GM-Logic 호출
    # ─────────────────────────────────────────────────────────────

    async def _call_gm_logic(self, session, player_message: str, roll_results: list,
                              master_ch, sim_result: dict | None = None) -> dict | None:
        """
        GM-Logic 모델 호출. DEFAULT_MODEL 사용.

        [캐시 활용 전략]
        세션 캐시(scenario_data + NPC 사전 + 세계관)가 유효한 경우, cached_content로 호출하여
        GM-Logic이 시나리오 전체 컨텍스트를 읽도록 한다.

        cached_content 사용 시 GenerateContentConfig에 system_instruction을 지정할 수 없으므로,
        GM_LOGIC_SYSTEM_INSTRUCTION을 contents 배열의 첫 user 메시지로 주입한다:
          contents[0] user  : GM_LOGIC_SYSTEM_INSTRUCTION  (의사결정 엔진 역할 지시)
          contents[1] model : 수락 응답 (대화 구조 유지용 단문)
          contents[2] user  : _build_logic_user_prompt()   (현재 상황 + 플레이어 발언)

        캐시가 없거나 모델 불일치 시 system_instruction 방식으로 폴백.

        Args:
            sim_result: 방안 6 서사 시뮬레이터 결과 (첫 번째 호출에만 주입, 이후 None)
        """
        user_prompt = _build_logic_user_prompt(session, player_message, roll_results,
                                                sim_result=sim_result)

        core.write_log(session.session_id, "api", f"[자동 GM Logic 요청 - Payload]\n{user_prompt}")

        # 캐시 활용 가능 여부 판단
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        use_cache   = bool(cache_name and cache_model == core.DEFAULT_MODEL)

        try:
            if use_cache:
                # ── 캐시 활용 경로 ──
                # cached_content 사용 시 system_instruction 설정 불가.
                # GM_LOGIC_SYSTEM_INSTRUCTION을 user 턴으로 삽입하여 동일 효과를 얻는다.
                logic_contents = [
                    types.Content(role="user",
                                  parts=[types.Part.from_text(text=GM_LOGIC_SYSTEM_INSTRUCTION)]),
                    types.Content(role="model",
                                  parts=[types.Part.from_text(
                                      text="이해했습니다. 지시사항에 따라 JSON 형식으로만 응답하겠습니다.")]),
                    types.Content(role="user",
                                  parts=[types.Part.from_text(text=user_prompt)]),
                ]
                config = types.GenerateContentConfig(
                    cached_content=cache_name,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=GM_LOGIC_RESPONSE_SCHEMA,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                # ── 폴백: 캐시 없음 / 모델 불일치 ──
                logic_contents = [
                    types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])
                ]
                config = types.GenerateContentConfig(
                    system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=GM_LOGIC_RESPONSE_SCHEMA,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL,
                contents=logic_contents,
                config=config,
            )
        except Exception as e:
            print(f"[AutoGM] Logic 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 자동 GM Logic 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(meta, "candidates_token_count", 0) or 0
            cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}GM-Logic 호출",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            cache_tag = "캐시O" if use_cache else "캐시X"
            print(
                f"[AutoGM/{session.session_id}] Logic 비용({cache_tag}): "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)} (누적 {core.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": f"GM-Logic 판단({cache_tag})", "cost": cost})
        except Exception as e:
            print(f"[AutoGM] Logic 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            decision = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                decision = json.loads(cleaned)
            except Exception as e:
                print(f"[AutoGM] JSON 파싱 실패: {e}\n응답 원문: {raw_text[:500]}")
                if master_ch:
                    await master_ch.send("⚠️ 자동 GM Logic 응답이 JSON 형식이 아닙니다. 이번 발언 스킵.")
                return None

        core.write_log(
            session.session_id, "api",
            f"[자동 GM Logic 결정]\n{json.dumps(decision, ensure_ascii=False, indent=2)}"
        )
        return decision

    # ─────────────────────────────────────────────────────────────
    # ROLL 실행 및 버튼 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _execute_rolls(self, session, rolls: list, game_ch) -> list[str]:
        """rolls 목록을 굴리고 결과를 게임·마스터 채널에 선언."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        results: list[str] = []

        for r in rolls:
            char_name = r.get("char_name") or session.auto_gm_target_char or "?"
            stat_name = r.get("stat") or ""
            sides = int(r.get("sides") or 20)
            weight = int(r.get("weight") or 0)

            stat_value = None
            uid = core.get_uid_by_char_name(session, char_name)
            if uid:
                profile = session.players.get(uid, {}).get("profile", {})
                if stat_name in profile:
                    try:
                        stat_value = int(profile[stat_name])
                    except (TypeError, ValueError):
                        stat_value = None

            roll = random.randint(1, sides)

            if stat_value is None:
                line = f"> 🎲 [{char_name}] {stat_name or 'd' + str(sides)} 굴림: **{roll}** / {sides}"
                logic_line = (
                    f"- {char_name} {stat_name}({sides}면, 가중치 {weight:+d}) "
                    f"→ {roll} (스탯 미확인, 결과 해석 보류)"
                )
            else:
                target = stat_value + weight
                is_success = roll <= target
                crit = ""
                if 5 <= target <= 16:
                    if roll in (1, 2):
                        crit = " 🌟대성공"
                    elif roll in (sides - 1, sides):
                        crit = " 💥대실패"
                result_text = ("성공 🟢" if is_success else "실패 🔴") + crit
                weight_str = f"{stat_value}{weight:+d}={target}" if weight else f"{stat_value}"
                line = (
                    f"> 🎲 [{char_name}] **{stat_name}** 판정 (1~{sides}, 기준치 {weight_str}) "
                    f"→ **{roll}**  /  **{result_text}**"
                )
                logic_line = (
                    f"- {char_name} {stat_name} 판정: 1d{sides}={roll}, 기준치 {weight_str}, "
                    f"{result_text.replace('🟢', '').replace('🔴', '').replace('🌟', '').replace('💥', '').strip()}"
                )

            if game_ch:
                await game_ch.send(line)
                # 판정 결과 안내 메시지 (stat_value가 확인된 경우에만 출력)
                if stat_value is not None:
                    if "대성공" in crit:
                        announce = f"> 🌟 **{stat_name}** 판정이 **대성공**했습니다!"
                    elif "대실패" in crit:
                        announce = f"> 💥 **{stat_name}** 판정이 **대실패**했습니다!"
                    elif is_success:
                        announce = f"> ✅ **{stat_name}** 판정이 성공했습니다."
                    else:
                        announce = f"> ❌ **{stat_name}** 판정이 실패했습니다."
                    await game_ch.send(announce)
                core.write_log(session.session_id, "game_chat", f"[판정]: {line}")
            if master_ch:
                await master_ch.send(f"🤖 [자동 GM 굴림]\n{line}")
            session.current_turn_logs.append(logic_line.lstrip("- "))
            results.append(logic_line)

        return results

    async def _dispatch_rolls(self, session, rolls: list, player_message: str, prior_roll_results: list):
        """ROLL 결정 시 플레이어에게 버튼 UI 전송."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        target_uid = core.get_uid_by_char_name(session, session.auto_gm_target_char)

        roll_descs = []
        for r in rolls:
            char_name = r.get("char_name") or session.auto_gm_target_char or "?"
            stat_name = r.get("stat") or ""
            sides = int(r.get("sides") or 20)
            weight = int(r.get("weight") or 0)
            stat_value = None
            uid = core.get_uid_by_char_name(session, char_name)
            if uid:
                profile = session.players.get(uid, {}).get("profile", {})
                if stat_name in profile:
                    try:
                        stat_value = int(profile[stat_name])
                    except (TypeError, ValueError):
                        pass
            if stat_value is not None:
                target = stat_value + weight
                w_str = f"({stat_value}{weight:+d}={target})" if weight else f"(기준치 {stat_value})"
                roll_descs.append(f"**{stat_name}** {w_str} 판정 ({sides}면체)")
            else:
                roll_descs.append(f"**{stat_name}** 판정 ({sides}면체)")

        desc_text = " / ".join(roll_descs)
        mention = f"<@{target_uid}>" if target_uid else "플레이어"

        view = AutoGMRollView(
            cog=self,
            session=session,
            roll_specs=rolls,
            player_message=player_message,
            prior_roll_results=prior_roll_results,
            target_uid=target_uid,
        )

        roll_prompt_text = (
            f"> 🎲 {mention}, 판정이 필요합니다!\n"
            f"> {desc_text}\n"
            f"> 아래 버튼을 눌러 주사위를 굴리세요. (5분 내 미클릭 시 자동 굴림)"
        )
        if game_ch:
            await game_ch.send(roll_prompt_text, view=view)
            core.write_log(session.session_id, "game_chat", f"[판정 요청]: {desc_text}")
        if master_ch:
            await master_ch.send(
                f"🤖 **[자동 GM ROLL]** 플레이어 버튼 대기 중...\n> {desc_text}"
            )

    # ─────────────────────────────────────────────────────────────
    # ROLL 결과 반영 계속 처리
    # ─────────────────────────────────────────────────────────────

    async def _continue_with_roll_results(self, session, player_message: str, roll_results: list):
        """AutoGMRollView 버튼 클릭 후 굴림 결과를 반영하여 GM-Logic 재호출."""
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.auto_gm_active:
                return

            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            decision = await self._call_gm_logic(session, player_message, roll_results, master_ch)
            if not decision:
                await m_send("⚠️ 자동 GM 결정 호출 실패. 이번 발언 스킵.")
                return

            action = decision.get("action", "PROCEED").upper()
            reasoning = decision.get("reasoning", "")
            action_labels = {
                "ASK":     "🟡 ASK (명확화 요청)",
                "ROLL":    "🎲 ROLL (주사위 판정)",
                "PROCEED": "🟢 PROCEED (턴 진행)",
            }
            print(f"[AutoGM/{session.session_id}] post-roll action={action} :: {reasoning[:120]}")
            await m_send(
                f"🤖 **[자동 GM 판단 (굴림 후)]** {action_labels.get(action, action)}\n"
                f"> {reasoning[:200]}"
            )

            instruction = _clean_proceed_instruction(
                decision.get("proceed_instruction") or
                "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
            )
            if not instruction:
                instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

            if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )

            await self._dispatch_proceed(session, instruction)

            # 서사 사건 평가 → 진행도 갱신 + 필요 시 재계획
            event_assessment = decision.get("event_assessment", "ongoing")
            await self._update_narrative_progress(session, event_assessment, master_ch)

            session.auto_gm_turns_done += 1
            session.auto_gm_side_note = ""
            await core.save_session_data(self.bot, session)
            # PROCEED 완료 → 다음 라운드 시작
            if session.auto_gm_active:
                await self._start_round(session)

    # ─────────────────────────────────────────────────────────────
    # NARRATE 디스패치 (경량 캐시 기반 GM 응답)
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_narrate(self, session, narrate_instruction: str) -> str | None:
        """
        캐시 기반 경량 LLM 호출로 짧은 GM 응답(NARRATE)을 생성하고 게임 채널에 스트리밍.

        NOTE: PROCEED의 풀 프롬프트 대신 최근 로그 + narrate_instruction만 전달하여
        약 300자 이내의 빠른 응답을 생성. 캐시 히트 시 비용은 PROCEED의 절반 이하.
        대사 마커(@대사:이름|본문)를 감지하여 인물 이미지·말풍선 포맷으로 자동 변환.

        Args:
            session: TRPGSession
            narrate_instruction (str): GM-Logic이 생성한 경량 응답 지시문 (100자 이내)

        Returns:
            str | None: 생성된 NARRATE 응답 텍스트 (스트리밍 완료 후). 실패 시 None.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        # 최근 raw_logs 4개 (각 400자 제한)
        recent_parts = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                preview = text[:400] + ("..." if len(text) > 400 else "")
                recent_parts.append(f"[{role}]\n{preview}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_parts) if recent_parts else "(최근 로그 없음)"

        # 이번 턴 현재까지 누적된 대화
        current_turn_str = "\n".join(session.current_turn_logs) if session.current_turn_logs else "(없음)"

        narrate_prompt = build_narrate_prompt(recent_str, current_turn_str, narrate_instruction)

        core.write_log(session.session_id, "api", f"[자동 GM NARRATE 요청]\n{narrate_prompt}")

        # NOTE: max_output_tokens를 설정하지 않음 — PROCEED(_execute_proceed)와 동일한 방침.
        # DEFAULT_MODEL(gemini-3-flash-preview)은 thinking 모델이므로, max_output_tokens를
        # 지정하면 내부 thinking 토큰까지 한도에 포함되어 실제 텍스트 출력이 거의 없는
        # MAX_TOKENS 조기 종료가 발생한다. 출력 길이는 프롬프트의 "300자 이내" 지시로 제어한다.
        try:
            if session.cache_name:
                config = types.GenerateContentConfig(
                    cached_content=session.cache_name,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                config = types.GenerateContentConfig(
                    system_instruction=self.bot.system_instruction,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            # PROCEED와 동일한 구조: typing()은 API 호출만 감싸고,
            # 출력(stream_text_to_channel)은 typing 컨텍스트 밖에서 실행한다.
            if game_ch:
                async with game_ch.typing():
                    response = await asyncio.to_thread(
                        self.bot.genai_client.models.generate_content,
                        model=core.DEFAULT_MODEL,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                        config=config,
                    )
            else:
                response = await asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                    config=config,
                )
        except Exception as e:
            print(f"[AutoGM] NARRATE 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 자동 GM NARRATE 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(meta, "candidates_token_count", 0) or 0
            cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}NARRATE 경량 응답",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            print(
                f"[AutoGM/{session.session_id}] NARRATE 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)} (누적 {core.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "NARRATE 경량 응답", "cost": cost})
        except Exception as e:
            print(f"[AutoGM] NARRATE 비용 정산 실패: {e}")

        narrate_text = (response.text or "").strip()
        if not narrate_text:
            return None

        core.write_log(session.session_id, "api", f"[자동 GM NARRATE 응답]\n{narrate_text}")

        # 게임 채널에 스트리밍 출력 (대사 마커 처리 포함)
        # NOTE: PROCEED(_execute_proceed)와 동일한 구조 — typing 컨텍스트 밖에서 stream_text_to_channel 호출.
        # typing은 위 API 호출 블록에서만 활성화되며, 이 시점에서는 이미 종료된 상태.
        if game_ch:
            paragraphs = [p.strip() for p in narrate_text.split("\n\n") if p.strip()]
            paragraphs = core.merge_consecutive_dialogues(paragraphs)

            for paragraph in paragraphs:
                dialogue = core.parse_dialogue_paragraph(paragraph)
                if dialogue:
                    speaker, content = dialogue
                    await core.maybe_send_speaker_image(game_ch, session, speaker)
                    formatted = core.format_dialogue_block(speaker, content)
                    await core.stream_text_to_channel(
                        self.bot, game_ch, formatted,
                        words_per_tick=5, tick_interval=1.5, quote_prefix=False
                    )
                else:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, paragraph,
                        words_per_tick=5, tick_interval=1.5
                    )

        # current_turn_logs에 추가 — PROCEED 시 AI가 맥락을 볼 수 있도록
        session.current_turn_logs.append(f"[진행자 (자동 GM)]: {narrate_text}")
        return narrate_text

    # ─────────────────────────────────────────────────────────────
    # PROCEED 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_proceed(self, session, instruction: str):
        """기존 GameCog._execute_proceed를 호출하여 묘사 생성·연출."""
        game_cog = self.bot.get_cog("GameCog")
        if not game_cog:
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send("⚠️ GameCog를 찾을 수 없어 자동 진행을 중단합니다.")
            return None

        master_ch = self.bot.get_channel(session.master_ch_id)
        if master_ch:
            await master_ch.send(f"🤖 **[자동 GM PROCEED]**\n> {instruction[:300]}")

        # NOTE: PROCEED 직전에 이번 턴 컨텍스트(NARRATE/ASK/ROLL 중간 기록)와 지시사항을 스냅샷.
        # _execute_proceed 내부에서 current_turn_logs가 초기화되므로 반드시 먼저 캡처해야 함.
        context_snapshot = list(session.current_turn_logs)
        prev_raw_count = len(session.raw_logs)

        result = await game_cog._execute_proceed(
            session, instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX
        )

        # PROCEED 완료 후 AI 출력 요약 (최대 500자)
        ai_summary = ""
        new_entries = session.raw_logs[prev_raw_count:]
        for content in reversed(new_entries):
            if getattr(content, "role", None) == "model":
                try:
                    text = content.parts[0].text
                    ai_summary = text[:500] + ("..." if len(text) > 500 else "")
                except Exception:
                    pass
                break

        # 이력 누적 (최근 5개 유지)
        if not hasattr(session, "auto_gm_proceed_history"):
            session.auto_gm_proceed_history = []
        session.auto_gm_proceed_history.append({
            "turn_num": session.turn_count,
            "instruction": instruction,
            "context": context_snapshot,
            "ai_summary": ai_summary,
        })
        if len(session.auto_gm_proceed_history) > 5:
            session.auto_gm_proceed_history = session.auto_gm_proceed_history[-5:]

        # [방안 2] narrative_plan.current_event.progress 자동 갱신
        # ai_summary 앞 150자를 현재 진행 상황 한줄 메모로 덮어씀.
        if ai_summary and getattr(session, "narrative_plan", {}).get("current_event"):
            session.narrative_plan["current_event"]["progress"] = ai_summary[:150]

        # ── 방안 B: 세계 물리 타임라인 갱신 (백그라운드 태스크) ──
        # ai_summary가 있을 때만 추출 실행. 실패해도 게임 진행에 영향 없음.
        if ai_summary:
            asyncio.create_task(self._update_world_timeline(session, ai_summary))

        return result


    # ─────────────────────────────────────────────────────────────
    # 방안 B — 세계 물리 타임라인 갱신
    # ─────────────────────────────────────────────────────────────

    async def _update_world_timeline(self, session, ai_output_text: str):
        """
        PROCEED 완료 후 AI 묘사 텍스트를 분석하여 session.world_timeline을 갱신한다.
        백그라운드 태스크로 실행됨. 실패해도 게임 진행에 영향 없음.

        [추출 목표]
        단순 감각 묘사가 아닌, 세계관 세력 배치·지역 규칙에 근거한 세계 상태를 기록한다.
        GM-Logic의 고차원 개연성 판단(방안 6)의 기준 데이터로 활용된다.
        """
        existing_tl = getattr(session, "world_timeline", {})
        existing_summary = (
            f"기존: 위치={existing_tl.get('current_location', '미확인')}, "
            f"시간대={existing_tl.get('time_of_day', '미확인')}, "
            f"세력={existing_tl.get('faction_context', '미확인')}"
        ) if existing_tl else "(없음)"

        user_prompt = (
            f"[기존 세계 상태]\n{existing_summary}\n\n"
            f"[이번 묘사 텍스트]\n{ai_output_text[:600]}\n\n"
            f"[시나리오 세계관 핵심 요소]\n"
            f"{str(session.scenario_data.get('worldview', ''))[:400]}\n\n"
            "위 묘사에서 세계 물리 상태를 추출하여 갱신하십시오.\n"
            "세력·지역 규칙 등 세계관 논리가 확인 가능한 경우 반드시 faction_context에 명시하십시오."
        )

        try:
            config = types.GenerateContentConfig(
                system_instruction=WORLD_TIMELINE_EXTRACTOR_SYSTEM_INSTRUCTION,
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=WORLD_TIMELINE_EXTRACTION_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=[types.Content(role="user",
                                        parts=[types.Part.from_text(text=user_prompt)])],
                config=config,
            )
        except Exception as e:
            print(f"[AutoGM] 세계 타임라인 추출 실패: {e}")
            return

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens  = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(meta, "candidates_token_count", 0) or 0
            breakdown  = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}세계 타임라인 갱신",
                                 in_tokens, 0, out_tokens, cost, session.total_cost)
        except Exception:
            pass

        raw_text = response.text or ""
        try:
            extracted = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                extracted = json.loads(cleaned)
            except Exception as e:
                print(f"[AutoGM] 세계 타임라인 JSON 파싱 실패: {e}")
                return

        # 기존 타임라인과 병합 (빈 문자열 필드는 기존 값 유지)
        updated = dict(existing_tl)
        for key, value in extracted.items():
            if value != "" and value is not None:
                updated[key] = value

        # 누적 경과 시간 합산
        elapsed = int(extracted.get("elapsed_minutes", 0) or 0)
        updated["elapsed_minutes"] = int(existing_tl.get("elapsed_minutes", 0) or 0) + elapsed
        updated["last_updated_turn"] = session.turn_count

        session.world_timeline = updated
        print(f"[AutoGM/{session.session_id}] 세계 타임라인 갱신: {updated.get('current_location', '?')} / "
              f"{updated.get('faction_context', '?')[:60]}")
        await core.save_session_data(self.bot, session)

    # ─────────────────────────────────────────────────────────────
    # 방안 E — PROCEED 지시사항 자기 검증
    # ─────────────────────────────────────────────────────────────

    async def _verify_proceed_instruction(self, session, instruction: str,
                                           player_message: str, master_ch) -> str:
        """
        GM-Logic이 생성한 proceed_instruction에서 플레이어 자율성 침해 여부를 검증한다.
        위반 감지 시 corrected_instruction으로 교체하고 마스터 채널에 알림.

        Args:
            instruction (str): GM-Logic이 생성한 proceed_instruction
            player_message (str): 플레이어의 원본 발언 (선언된 행동 확인용)

        Returns:
            str: 검증 통과 또는 수정된 proceed_instruction
        """
        user_prompt = (
            f"[플레이어 선언 행동]\n{player_message}\n\n"
            f"[GM proceed_instruction]\n{instruction}\n\n"
            "위 proceed_instruction이 플레이어가 선언하지 않은 PC 행동·발언·내면을 포함하는지 검증하십시오."
        )

        try:
            config = types.GenerateContentConfig(
                system_instruction=PROCEED_VERIFIER_SYSTEM_INSTRUCTION,
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=PROCEED_VERIFY_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=[types.Content(role="user",
                                        parts=[types.Part.from_text(text=user_prompt)])],
                config=config,
            )
        except Exception as e:
            print(f"[AutoGM] PROCEED 검증 실패 (원본 사용): {e}")
            return instruction

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens  = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens = getattr(meta, "candidates_token_count", 0) or 0
            breakdown  = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}PROCEED 자기 검증",
                                 in_tokens, 0, out_tokens, cost, session.total_cost)
        except Exception:
            pass

        raw_text = response.text or ""
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                result = json.loads(cleaned)
            except Exception:
                return instruction

        if result.get("has_violation"):
            detail   = result.get("violation_detail", "")
            corrected = result.get("corrected_instruction", instruction) or instruction
            print(f"[AutoGM/{session.session_id}] PROCEED 위반 감지: {detail[:120]}")
            if master_ch:
                await master_ch.send(
                    f"⚠️ **[자동 GM 검증]** proceed_instruction에 PC 자율성 침해 감지 — 자동 수정\n"
                    f"> 위반: {detail[:200]}\n"
                    f"> 수정 후: {corrected[:200]}"
                )
            return _clean_proceed_instruction(corrected)

        return instruction

    # ─────────────────────────────────────────────────────────────
    # 방안 6 — 2단계 사고 서사 방향성 시뮬레이터
    # ─────────────────────────────────────────────────────────────

    async def _simulate_narrative_directions(self, session, player_message: str,
                                              master_ch) -> dict | None:
        """
        GM-Logic 호출 전 세계관 캐시를 활용하여 고차원 서사 방향성을 사전 시뮬레이션.

        [목적]
        단순 사건 추론(소리가 났으니 몬스터가 온다)이 아닌, 시나리오 세계관의 세력 배치·
        지역 규칙·물리 법칙에 근거한 구조적 개연성 판단 결과를 GM-Logic에 제공한다.

        [활성화 조건]
        - 세션 캐시 유효 (없으면 세계관 문맥 없어 고차원 판단 불가)
        - 캐시 모델이 DEFAULT_MODEL과 일치

        Returns:
            dict | None: {"world_state_analysis": str, "directions": [...]} 또는 None
        """
        cache_name = getattr(session, "cache_name", None)

        # 최근 로그 요약 (각 250자)
        recent_lines = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                recent_lines.append(f"[{role}]\n{text[:250]}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_lines) if recent_lines else "(없음)"

        # 세계 물리 타임라인 요약 (있으면)
        tl = getattr(session, "world_timeline", {})
        tl_note = ""
        if tl:
            tl_note = (
                f"\n[현재 세계 상태]\n"
                f"위치: {tl.get('current_location', '미확인')}\n"
                f"세력: {tl.get('faction_context', '미확인')}\n"
                f"위협: {tl.get('known_threats', '없음')}\n"
            )

        user_prompt = (
            f"[현재 턴]: {session.turn_count + 1}\n"
            f"{tl_note}"
            f"\n[최근 게임 로그]\n{recent_str}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n\n"
            "[지시사항]\n"
            "위 상황에서 다음에 일어날 수 있는 서사 방향성 2~3개를 세계관 논리에 근거하여 평가하십시오.\n"
            "반드시 시나리오 캐시의 세력 정보·지역 규칙·세계관 설정을 인용하여 판단하십시오."
        )

        try:
            sim_contents = [
                types.Content(role="user",
                              parts=[types.Part.from_text(text=NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION)]),
                types.Content(role="model",
                              parts=[types.Part.from_text(
                                  text="이해했습니다. 세계관 논리에 근거하여 서사 방향성을 분석하겠습니다.")]),
                types.Content(role="user",
                              parts=[types.Part.from_text(text=user_prompt)]),
            ]
            config = types.GenerateContentConfig(
                cached_content=cache_name,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=NARRATIVE_DIRECTION_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL,
                contents=sim_contents,
                config=config,
            )
        except Exception as e:
            print(f"[AutoGM] 서사 시뮬레이션 호출 실패: {e}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens     = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens    = getattr(meta, "candidates_token_count", 0) or 0
            cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0
            breakdown     = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                cached_read_tokens=cached_tokens)
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}서사 방향성 시뮬레이션",
                                 in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "서사 방향성 시뮬레이션", "cost": cost})
            print(
                f"[AutoGM/{session.session_id}] 시뮬레이션 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)}"
            )
        except Exception as e:
            print(f"[AutoGM] 시뮬레이션 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            sim_data = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                sim_data = json.loads(cleaned)
            except Exception as e:
                print(f"[AutoGM] 서사 시뮬레이션 JSON 파싱 실패: {e}")
                return None

        core.write_log(session.session_id, "api",
                       f"[서사 방향성 시뮬레이션]\n{json.dumps(sim_data, ensure_ascii=False, indent=2)}")
        return sim_data

    # ─────────────────────────────────────────────────────────────
    # 서사 계획 명령어
    # ─────────────────────────────────────────────────────────────

    @commands.command(name="서사계획")
    async def show_narrative_plan(self, ctx):
        """현재 서사 계획을 마스터 채널에 임베드로 출력."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        plan = getattr(session, "narrative_plan", {})
        if not plan:
            return await ctx.send(
                "⚠️ 수립된 서사 계획이 없습니다.\n"
                "`!자동시작`으로 자동 GM 모드를 활성화하면 초기 계획이 수립됩니다.\n"
                "또는 `!서사재계획`으로 직접 수립할 수 있습니다."
            )

        current = plan.get("current_event", {})
        next_ev = plan.get("next_event", {})
        version = plan.get("plan_version", "?")
        last_turn = plan.get("last_planned_turn", "?")

        embed = discord.Embed(title="📖 현재 서사 계획", color=0x5865F2)
        embed.set_footer(text=f"v{version}  |  수립 시점: 턴 {last_turn}")

        # 중규모 진행 방향 (mid_plan)
        mid = plan.get("mid_plan", {})
        if mid:
            milestones = mid.get("milestones", [])
            ms_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(milestones)]) if milestones else "(없음)"
            m_val = (
                f"**전체 흐름**: {mid.get('overview', '-')}\n"
                f"**이정표**:\n{ms_str}\n"
                f"**완료 조건**: {mid.get('end_condition', '-')}"
            )
            embed.add_field(
                name=f"🗺️ 중규모 진행 방향: {mid.get('title', '?')}",
                value=m_val[:1020],
                inline=False
            )

        # 현재 순간 사건
        c_val = (
            f"**상황**: {current.get('summary', '-')}\n"
            f"**마무리 방향**: {current.get('resolution_direction', '-')}\n"
            f"**진행**: {current.get('progress') or '(초기 상태)'}"
        )
        embed.add_field(
            name=f"📌 현재 순간 사건: {current.get('title', '?')}",
            value=c_val[:1020],
            inline=False
        )

        # 다음 순간 사건
        n_val = (
            f"**개요**: {next_ev.get('summary', '-')}\n"
            f"**시작 조건**: {next_ev.get('trigger', '-')}"
        )
        embed.add_field(
            name=f"⏭️ 다음 순간 사건: {next_ev.get('title', '?')}",
            value=n_val[:1020],
            inline=False
        )

        notes = plan.get("planner_notes", "")
        if notes:
            embed.add_field(name="📝 설계 메모", value=notes[:1020], inline=False)

        await ctx.send(embed=embed)

    @commands.command(name="서사재계획")
    async def replan_narrative(self, ctx, *, memo: str = ""):
        """
        서사 계획을 강제로 재수립한다.
        선택적으로 메모를 추가하면 계획 수립 시 반영된다.
        예: !서사재계획 플레이어가 예상과 달리 적에게 합류했다
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        await ctx.send("⏳ 서사 계획 재수립을 시작합니다...")
        success = await self._plan_narrative(session, "manual", context_note=memo.strip())
        if not success:
            await ctx.send("⚠️ 서사 계획 재수립에 실패했습니다.")

    # ─────────────────────────────────────────────────────────────
    # 서사 계획 내부 함수
    # ─────────────────────────────────────────────────────────────

    async def _init_narrative_and_start(self, session):
        """
        !자동시작 직후 호출. 서사 계획이 없으면 새로 수립한 뒤 첫 라운드를 시작한다.
        이미 계획이 있으면 수립 없이 바로 라운드를 시작한다.
        백그라운드 태스크로 실행됨.
        """
        if not session.narrative_plan:
            await self._plan_narrative(session, "init")
        await self._start_round(session)

    async def _update_narrative_progress(self, session, event_assessment: str, master_ch):
        """
        PROCEED 완료 후 호출. completed/deviated 평가 시 백그라운드에서 서사 재계획을 트리거한다.
        progress 자동 갱신은 _dispatch_proceed 내부에서 수행 (ai_summary 직후).

        [3턴 주기 강제 재계획]
        completed/deviated 외에도, 마지막 계획 수립 이후 3턴 이상 경과하면 mid_plan 포함 전체 재수립.
        last_planned_turn을 즉시 갱신하여 백그라운드 완료 전 중복 트리거를 방지한다.
        """
        plan = getattr(session, "narrative_plan", {})
        if not plan:
            return

        # completed/deviated → 즉시 재계획
        if event_assessment == "completed":
            # 순간 계획만 재수립 — mid_plan 유지
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 순간 사건 완료 감지\n"
                    f"> 중규모 계획을 유지하며 다음 순간 계획을 수립합니다..."
                )
            asyncio.create_task(self._plan_narrative(session, "completed", full_replan=False))
        elif event_assessment == "deviated":
            # mid_plan 포함 전부 재수립
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 경로 이탈 감지\n"
                    f"> 중규모 계획 포함 전체 재수립합니다..."
                )
            asyncio.create_task(self._plan_narrative(session, "deviated", full_replan=True))
        elif event_assessment == "resolving" and master_ch:
            await master_ch.send(
                f"📖 **[서사 계획]** 현재 순간 사건이 마무리 단계에 진입했습니다 (resolving)."
            )

        # [3턴 주기 강제 재계획]
        # completed/deviated가 이미 트리거된 경우는 건너뜀 (중복 방지)
        if event_assessment not in ("completed", "deviated"):
            last_planned = plan.get("last_planned_turn", 0)
            turns_elapsed = session.turn_count - last_planned
            if turns_elapsed >= 3:
                # last_planned_turn을 즉시 현재 턴으로 갱신 → 다음 턴 중복 트리거 방지
                plan["last_planned_turn"] = session.turn_count
                session.narrative_plan = plan
                if master_ch:
                    await master_ch.send(
                        f"📖 **[서사 계획]** 마지막 계획 수립 이후 {turns_elapsed}턴 경과 → 강제 재계획...\n"
                        f"> 현재 상황을 반영하여 전체 계획을 갱신합니다."
                    )
                asyncio.create_task(
                    self._plan_narrative(session, "manual", full_replan=True,
                                         context_note=f"{turns_elapsed}턴 주기 자동 강제 재계획")
                )

    async def _plan_narrative(self, session, trigger_reason: str = "init",
                               context_note: str = "", full_replan: bool = True) -> bool:
        """
        LOGIC_MODEL을 호출하여 서사 계획을 수립하거나 갱신한다.

        Args:
            session: TRPGSession
            trigger_reason: "init" | "completed" | "deviated" | "manual"
            context_note: GM이 추가한 메모 (재계획 시 계획 수립 프롬프트에 포함)
            full_replan: True이면 mid_plan 포함 전부 재수립.
                         False이면(completed) mid_plan을 유지하고 순간 계획만 갱신.

        Returns:
            bool: 성공 여부
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        # ── 시나리오 정보 ──
        story_guide = session.scenario_data.get("story_guide", "")
        worldview   = session.scenario_data.get("worldview", "")

        # ── 최근 게임 로그 (최대 6개, 각 350자) ──
        recent_log_lines = []
        for content in session.raw_logs[-6:]:
            try:
                text    = content.parts[0].text
                role    = content.role.upper()
                preview = text[:350] + ("..." if len(text) > 350 else "")
                recent_log_lines.append(f"[{role}]\n{preview}")
            except Exception:
                continue
        recent_logs_str = "\n\n".join(recent_log_lines) if recent_log_lines else "(로그 없음)"

        # ── PC 상태 요약 ──
        pc_lines = []
        for uid, p in session.players.items():
            name    = p.get("name", "?")
            res     = session.resources.get(name, {})
            sta     = session.statuses.get(name, [])
            res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "없음"
            sta_str = ", ".join(sta) or "없음"
            pc_lines.append(f"  - {name}: 자원={res_str}, 상태={sta_str}")
        pc_info = "\n".join(pc_lines) or "(PC 없음)"

        # ── 압축 기억 ──
        memory_str = (
            session.compressed_memory
            or getattr(session, "cached_compressed_memory", "")
            or "(없음)"
        )

        # ── 기존 계획 처리 ──
        existing_plan = session.narrative_plan or {}
        existing_plan_block = ""

        if not full_replan and existing_plan:
            # completed 재계획: mid_plan 유지, 순간 계획만 갱신
            mid = existing_plan.get("mid_plan", {})
            nxt = existing_plan.get("next_event", {})
            if mid:
                ms_str = " → ".join(mid.get("milestones", []))
                existing_plan_block = (
                    "\n[유지할 중규모 진행 계획 — 이 내용을 mid_plan으로 그대로 출력하십시오]\n"
                    f"title: {mid.get('title', '')}\n"
                    f"overview: {mid.get('overview', '')}\n"
                    f"milestones: {ms_str}\n"
                    f"end_condition: {mid.get('end_condition', '')}\n"
                    "\n위 중규모 계획에서 다음으로 도달해야 할 milestone을 목표로 삼아 "
                    "새 current_event와 next_event를 수립하십시오.\n"
                )
            if nxt:
                existing_plan_block += (
                    f"\n[이전 next_event — current_event 승격 참고용]\n"
                    f"제목: {nxt.get('title', '')} / 개요: {nxt.get('summary', '')}\n"
                    f"(이 사건이 새 current_event의 출발점이 됩니다)\n"
                )
            trigger_context = "직전 순간 사건이 완료되었습니다. 중규모 계획을 유지하며 다음 순간 계획으로 전환하세요."
        else:
            # 전체 재계획 (init/deviated/manual)
            if existing_plan and trigger_reason != "init":
                cur = existing_plan.get("current_event", {})
                existing_plan_block = (
                    "\n[이전 계획 (참고용 — 폐기 후 재수립)]\n"
                    f"이전 현재 사건: {cur.get('title', '?')} — {cur.get('summary', '')}\n"
                    f"마무리 방향: {cur.get('resolution_direction', '')}\n"
                )
            trigger_context_map = {
                "init":     "자동 GM 모드가 활성화되었습니다. 현재 상황을 분석하여 2단계 서사 계획(mid_plan + 순간 계획)을 수립하세요.",
                "deviated": "플레이어의 선택으로 서사 방향이 예상 범위를 벗어났습니다. mid_plan 포함 계획 전체를 재수립하세요.",
                "manual":   "GM이 수동으로 재계획을 요청했습니다. 현재 상황을 재평가하여 계획 전체를 갱신하세요.",
            }
            trigger_context = trigger_context_map.get(trigger_reason, f"재계획 요청 ({trigger_reason})")

        context_note_block = f"\n[GM 추가 메모 (계획에 반영하세요)]\n{context_note}\n" if context_note else ""

        user_prompt = (
            f"[서사 계획 수립 요청]\n"
            f"트리거: {trigger_context}\n\n"
            f"[시나리오 기반 정보]\n"
            f"세계관: {worldview[:600] if worldview else '(없음)'}\n"
            f"스토리 가이드: {story_guide[:1200] if story_guide else '(없음)'}\n\n"
            f"[현재 게임 상황]\n"
            f"진행 턴: {session.turn_count}\n"
            f"PC 상태:\n{pc_info}\n"
            f"압축 기억:\n{memory_str[:1000]}\n\n"
            f"[최근 게임 로그]\n{recent_logs_str}\n"
            f"{existing_plan_block}"
            f"{context_note_block}\n"
            "[출력 지시]\n"
            "위 정보를 바탕으로 자동 GM이 활용할 서사 계획을 JSON 스키마에 맞게 수립하십시오."
        )

        core.write_log(session.session_id, "api",
                       f"[서사 계획 요청 - trigger={trigger_reason}]\n{user_prompt}")

        try:
            config = types.GenerateContentConfig(
                system_instruction=NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=NARRATIVE_PLAN_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
                config=config,
            )
        except Exception as e:
            print(f"[AutoGM] 서사 계획 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 서사 계획 수립 실패: {type(e).__name__}")
            return False

        # ── 비용 정산 ──
        try:
            meta         = response.usage_metadata
            in_tokens    = getattr(meta, "prompt_token_count", 0) or 0
            out_tokens   = getattr(meta, "candidates_token_count", 0) or 0
            cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0
            breakdown    = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}서사 계획 수립",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )
            print(
                f"[AutoGM/{session.session_id}] 서사 계획 비용: "
                f"In={in_tokens:,} Out={out_tokens:,} → {core.format_cost(cost)}"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "서사 계획 수립", "cost": cost})
        except Exception as e:
            print(f"[AutoGM] 서사 계획 비용 정산 실패: {e}")

        # ── JSON 파싱 ──
        raw_text = response.text or ""
        try:
            plan = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                plan = json.loads(cleaned)
            except Exception as e:
                print(f"[AutoGM] 서사 계획 JSON 파싱 실패: {e}\n원문: {raw_text[:400]}")
                if master_ch:
                    await master_ch.send("⚠️ 서사 계획 JSON 파싱 실패. 기존 계획을 유지합니다.")
                return False

        # ── 버전·타임스탬프 기록 ──
        plan["plan_version"]      = session.narrative_plan.get("plan_version", 0) + 1
        plan["last_planned_turn"] = session.turn_count
        session.narrative_plan    = plan
        await core.save_session_data(self.bot, session)

        # ── 마스터 채널 보고 (embed) ──
        current = plan.get("current_event", {})
        next_ev = plan.get("next_event", {})
        trigger_label_map = {
            "init":      "초기 계획 수립",
            "completed": "사건 완료 → 순간 계획 갱신",
            "deviated":  "이탈 감지 → 전체 재수립",
            "manual":    "수동 재계획",
        }
        trigger_label = trigger_label_map.get(trigger_reason, "계획 갱신")

        if master_ch:
            embed = discord.Embed(
                title=f"📖 서사 계획 갱신 — {trigger_label}",
                color=0x5865F2,
            )
            embed.set_footer(text=f"v{plan['plan_version']}  |  턴 {session.turn_count}")

            mid = plan.get("mid_plan", {})
            if mid:
                milestones = mid.get("milestones", [])
                ms_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(milestones)]) if milestones else "(없음)"
                m_val = (
                    f"**전체 흐름**: {mid.get('overview', '-')}\n"
                    f"**이정표**:\n{ms_str}\n"
                    f"**완료 조건**: {mid.get('end_condition', '-')}"
                )
                embed.add_field(
                    name=f"🗺️ 중규모 진행 방향: {mid.get('title', '?')}",
                    value=m_val[:1020],
                    inline=False,
                )

            c_val = (
                f"**상황**: {current.get('summary', '-')}\n"
                f"**마무리 방향**: {current.get('resolution_direction', '-')}"
            )
            embed.add_field(
                name=f"📌 현재 순간 사건: {current.get('title', '?')}",
                value=c_val[:1020],
                inline=False,
            )

            n_val = (
                f"**개요**: {next_ev.get('summary', '-')}\n"
                f"**시작 조건**: {next_ev.get('trigger', '-')}"
            )
            embed.add_field(
                name=f"⏭️ 다음 순간 사건: {next_ev.get('title', '?')}",
                value=n_val[:1020],
                inline=False,
            )

            planner_notes = plan.get("planner_notes", "")
            if planner_notes:
                embed.add_field(name="📝 설계 메모", value=planner_notes[:1020], inline=False)

            await master_ch.send(embed=embed)

        core.write_log(session.session_id, "api",
                       f"[서사 계획 결과 ({trigger_label})]\n{json.dumps(plan, ensure_ascii=False, indent=2)}")
        return True


async def setup(bot):
    """디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수."""
    await bot.add_cog(AutoGMCog(bot))
