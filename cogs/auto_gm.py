import re
import json
import random
import asyncio
import discord
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core


# ========== [자동 GM 모드 상수] ==========
# NOTE: GM-Logic 호출 시 한 플레이어 발언당 내부 루프 반복 상한.
MAX_ITERATIONS_PER_MESSAGE = 5

# NOTE: 같은 플레이어 발언에 대한 ASK 누적 상한. 초과 시 강제 PROCEED.
MAX_CLARIFY_PER_MESSAGE = 2

# NOTE: 자동 GM 비용 로그 라벨에 부착하는 접두사.
COST_LOG_PREFIX = "[AUTO] "


# ========== [GM-Logic 응답 스키마 (JSON Schema)] ==========
GM_LOGIC_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["ASK", "ROLL", "PROCEED"],
            "description": "다음에 수행할 단일 행동."
        },
        "bridge_message": {
            "type": "string",
            "description": "ASK일 때 게임 채널에 GM으로서 출력할 서사·질문 (150자 이내). 다른 action에서는 빈 문자열."
        },
        "rolls": {
            "type": "array",
            "description": "ROLL일 때 굴려야 할 판정 목록. 다른 action에서는 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "char_name": {"type": "string", "description": "굴림 대상 캐릭터 이름"},
                    "stat": {"type": "string", "description": "기준 능력치 이름 (PC profile의 키)"},
                    "sides": {"type": "integer", "description": "주사위 면 수 (보통 20)"},
                    "weight": {"type": "integer", "description": "가중치(-5~+5 권장)"}
                },
                "required": ["char_name", "stat", "sides", "weight"]
            }
        },
        "proceed_instruction": {
            "type": "string",
            "description": "PROCEED일 때 !진행 인자 형태 지시문. 자/태/상중하 태그 포함 가능. 다른 action에서는 빈 문자열."
        },
        "reasoning": {
            "type": "string",
            "description": "결정 근거 1~2문장 (디버그용)."
        }
    },
    "required": ["action", "bridge_message", "rolls", "proceed_instruction", "reasoning"]
}

# ========== [GM-Logic 시스템 지시문] ==========
GM_LOGIC_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG '자동 GM 모드'의 의사결정 엔진입니다.
인간 GM이 자리를 비운 동안, 플레이어의 신규 발언을 받아 다음 단계를 결정합니다.

[당신의 역할]
당신은 '묘사를 직접 작성하지 않습니다'. 묘사는 별도의 메인 GM 모델이 PROCEED 단계에서 수행합니다.
당신의 출력은 JSON 결정문(action 디스패치)으로 한정됩니다.

[가능한 action 3가지 — 정확히 하나만 선택]

1. ASK
   - 플레이어 의도가 모호하거나 정보가 부족할 때.
   - bridge_message: 게임 채널에 GM으로서 출력하는 서사·질문 (150자 이내).
     짧은 상황 묘사나 주변 묘사를 한 문장 덧붙인 뒤, 자연스러운 GM 어투로 의도를 물어볼 것.
     예 1) "창고 한쪽에 기름때 묻은 쇠파이프가 눈에 들어옵니다. 그걸로 공격하시겠습니까, 아니면 다른 방법을 쓰시겠습니까?"
     예 2) "문은 굳게 잠겨 있습니다. 정면 돌파를 시도하시겠습니까, 아니면 다른 경로를 찾아보시겠습니까?"
     예 3) "표적이 등을 보이고 있습니다. 지금 바로 공격하시겠습니까?"
   - bridge_message는 핵심 장면 묘사와 감정 표현이 중심이 되는 긴 내러티브가 되어서는 안 됩니다.
     그런 묘사는 오직 PROCEED에서만 출력됩니다.

2. ROLL
   - 행동 수행에 능력치 판정이 필요할 때.
   - rolls: 굴려야 할 판정 목록. 가중치는 -5~+5 범위에서 합리적으로 선택.
   - 굴림 결과는 플레이어가 버튼을 눌러 산출하며, 결과가 다음 호출 컨텍스트에 주입됩니다.
     당신은 결과 선언을 하지 않습니다.

3. PROCEED
   - 충분한 맥락이 모이고, 묘사를 진행할 준비가 끝났을 때.
   - proceed_instruction: 인간 GM의 !진행 인자처럼 동작하는 지시문.
   - 작성 규칙: 한국어 자연어 서술문으로만 작성하십시오.
     마크다운 서식(##, **, -, >, 코드블럭 등)을 절대 사용하지 마십시오.
     아래 태그 외에는 일반 서술문만 사용할 것:

     [자원 변동 태그]
     자:이름;아이템;수치
     ※ 세미콜론(;)으로 구분된 이름·아이템·수치 세 부분이 반드시 모두 있어야 합니다. 수치는 정수.
     예) 자:정원모;물;-1     자:임성진;탄약;-2     자:아서;체력포션;+1

     [상태 태그]
     태:이름;상태            (상태 부여)  예) 태:정원모;출혈
     태:이름;-상태           (상태 제거)  예) 태:정원모;-출혈
     ※ 상태 이름 뒤에 마침표(.)·쉼표·물음표 등 구두점을 절대 붙이지 마십시오.
     ※ 잘못된 예: 태:임성진;-지침.   올바른 예: 태:임성진;-지침

     [이미지 삽입 태그]
     상:키워드 / 중:키워드 / 하:키워드  (등록된 이미지 키워드를 묘사 위/중/아래에 삽입)
     ※ 이 태그는 이미지 삽입 전용입니다. 자원이나 상태 변동에는 반드시 자:/태: 태그를 사용하십시오.

   - 태그는 지시문 내 임의 위치에 삽입 가능합니다. 예) "태:정원모;출혈 정원모가 총에 맞아 쓰러진다."

   [#25 — 능동적 서사 진행 원칙]
   proceed_instruction 작성 시 아래 원칙을 반드시 따르십시오:
   A) 플레이어 행동의 자연스러운 결과를 반영한다.
   B) 세계가 멈춰 있지 않음을 드러내는 **신규 사건**을 능동적으로 발생시킨다.
      - 감염자 출현·이동·소리, NPC의 예상치 못한 개입·이탈·태도 변화,
        환경 변화(소음·화재·문 잠김·날씨·전력 차단 등),
        다른 세력의 움직임, 새로운 위협 또는 기회의 등장 등을 적극 제안한다.
   C) 플레이어가 수동적으로 대기하거나 단순 이동·대화를 하는 상황에서도
      반드시 어떤 사건이 발생하거나 세계에 변화가 생기도록 지시문을 구성한다.
   D) 시나리오 룰북에 없는 소규모 사건은 기존 설정과 모순되지 않는 범위에서
      맥락에서 자연스럽게 파생시킬 수 있다. 시나리오 룰북에 정의된 주요 세력·이벤트를
      임의로 종결·왜곡하는 것은 금지한다.

[엄격한 규칙]
- 응답은 정확히 하나의 action만 가집니다.
- 같은 플레이어 발언에 대해 ASK를 반복하지 마세요. 한 번 명확화한 뒤에는 PROCEED 또는 ROLL로 진행합니다.
- 시나리오 종결(엔딩) 판단은 절대 하지 마세요. 그것은 인간 GM의 권한입니다.
- 출력은 반드시 지정된 JSON 스키마를 따릅니다. 추가 텍스트, 마크다운, 코드블럭 모두 금지.

[결정 우선순위]
1. 플레이어 발언이 명확한 행동 선언 + 판정이 필요 → ROLL
2. 명확한 행동이지만 판정 불필요 (대화·이동·관찰 등) → PROCEED
3. 의도 모호 → ASK (단, 직전에 ASK가 있었으면 PROCEED로 진행)
4. 굴림 결과가 컨텍스트에 들어와 있음 → PROCEED (결과를 반영한 지시문 작성)
"""


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


def _build_logic_user_prompt(session, player_message: str, roll_results: list) -> str:
    """
    GM-Logic 호출용 사용자 프롬프트 조립.

    Args:
        session: TRPGSession
        player_message (str): 플레이어 신규 발언 (멀티플레이어 시 종합 텍스트)
        roll_results (list[str]): 직전 ROLL 결과 문자열 목록 (재호출 시 누적)
    """
    target_char = session.auto_gm_target_char or "(미지정)"
    side_note = session.auto_gm_side_note or ""
    clarify_count = session.auto_gm_clarify_count

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

    # 자원·상태
    res = session.resources.get(target_char, {}) if target_char else {}
    sta = session.statuses.get(target_char, []) if target_char else []
    res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "(없음)"
    sta_str = ", ".join(sta) or "(없음)"

    roll_block = ""
    if roll_results:
        roll_block = "\n[직전 굴림 결과 (반드시 반영하여 PROCEED를 작성)]\n" + "\n".join(roll_results)

    note_block = f"\n[GM 사이드 노트 (이번 턴 적용)]\n{side_note}\n" if side_note else ""

    # NOTE: 이번 턴에 누적된 플레이어 발언·ASK 브리지·주사위 결과를 컨텍스트에 포함.
    # ASK→플레이어 응답→ASK→... 연쇄 대화를 GM-Logic이 인지해야 중복 질문을 방지할 수 있음.
    current_turn_block = ""
    if session.current_turn_logs:
        current_turn_block = (
            "\n[이번 턴 누적 대화 (현재 PROCEED 이전까지 발생한 발언·GM 질문·판정)]\n"
            + "\n".join(session.current_turn_logs)
            + "\n"
        )

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
[PC 프로필]: {pc_profile_summary or "(미설정)"}
[PC 자원]: {res_str}
[PC 상태]: {sta_str}
[직전 ASK 횟수 / 한도]: {clarify_count} / {MAX_CLARIFY_PER_MESSAGE}
{multi_info}{note_block}
[최근 6턴 컨텍스트]
{recent_logs_str}
{current_turn_block}
{roll_block}

[플레이어 신규 발언]
{player_message}

위 컨텍스트를 분석하여 다음 단일 action(ASK / ROLL / PROCEED)을 결정하고 JSON 스키마에 맞춰 응답하십시오."""


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
        await core.save_session_data(self.bot, session)

        await ctx.send(
            f"🤖 **[자동 GM 모드 활성화]**\n"
            f"- 대상 PC: **{', '.join(target_chars)}**\n"
            f"- 자동 턴 한도: {session.auto_gm_turn_cap}턴\n"
            f"- 자동 누적 비용 한도: {core.format_cost(session.auto_gm_cost_cap_krw)}\n"
            f"- PROCEED 완료 후 GM이 선제적으로 행동을 물어봅니다.\n"
            f"- 중단: `!자동중단`  /  GM에게 메모: `!자동개입 [텍스트]`"
        )

        # 활성화 직후 첫 라운드 즉시 시작 (선제 행동 질문)
        await self._start_round(session)

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
            prompt = f"{mention}, 이번에는 어떤 행동을 하거나 말을 하시겠습니까?"

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
            "ROLL":    "🎲 ROLL (주사위 판정)",
            "PROCEED": "🟢 PROCEED (턴 진행)",
        }

        roll_results: list[str] = []

        for iteration in range(MAX_ITERATIONS_PER_MESSAGE):
            decision = await self._call_gm_logic(session, player_message, roll_results, master_ch)
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
                    session.auto_gm_turns_done += 1
                    session.auto_gm_side_note = ""
                    await core.save_session_data(self.bot, session)
                    if session.auto_gm_active:
                        await self._start_round(session)
                    return

                bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                game_ch = self.bot.get_channel(session.game_ch_id)
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

                if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                    session.auto_gm_active = False
                    await m_send(
                        f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                        f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                    )

                await self._dispatch_proceed(session, instruction)
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
            session.auto_gm_turns_done += 1
            session.auto_gm_side_note = ""
            await core.save_session_data(self.bot, session)
            if session.auto_gm_active:
                await self._start_round(session)

        await core.save_session_data(self.bot, session)

    # ─────────────────────────────────────────────────────────────
    # GM-Logic 호출
    # ─────────────────────────────────────────────────────────────

    async def _call_gm_logic(self, session, player_message: str, roll_results: list, master_ch) -> dict | None:
        """GM-Logic 모델 호출. DEFAULT_MODEL 사용."""
        user_prompt = _build_logic_user_prompt(session, player_message, roll_results)

        core.write_log(session.session_id, "api", f"[자동 GM Logic 요청 - Payload]\n{user_prompt}")

        try:
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
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
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

            print(
                f"[AutoGM/{session.session_id}] Logic 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)} (누적 {core.format_cost(session.total_cost)})"
            )

            if master_ch:
                await master_ch.send(
                    f"💰 **[자동 GM 비용]** GM-Logic 호출\n"
                    f"- 입력 {in_tokens:,} / 출력 {out_tokens:,} 토큰 (캐시 {cached_tokens:,})\n"
                    f"- 발생: {core.format_cost(cost)} | 누적: {core.format_cost(session.total_cost)}"
                )
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
            session.auto_gm_turns_done += 1
            session.auto_gm_side_note = ""
            await core.save_session_data(self.bot, session)
            # PROCEED 완료 → 다음 라운드 시작
            if session.auto_gm_active:
                await self._start_round(session)

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

        return await game_cog._execute_proceed(
            session, instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX
        )


async def setup(bot):
    """디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수."""
    await bot.add_cog(AutoGMCog(bot))
