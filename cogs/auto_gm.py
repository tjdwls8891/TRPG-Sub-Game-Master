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
#       (예: ASK→ROLL→PROCEED 같은 다단계 흐름 + 폭주 방지)
MAX_ITERATIONS_PER_MESSAGE = 5

# NOTE: 같은 플레이어 발언에 대한 ASK 누적 상한. 초과 시 강제 PROCEED.
MAX_CLARIFY_PER_MESSAGE = 2

# NOTE: 자동 GM 비용 로그 라벨에 부착하는 접두사 (결정사항 #3 — `[AUTO]` 표기).
COST_LOG_PREFIX = "[AUTO] "


# ========== [GM-Logic 응답 스키마 (JSON Schema)] ==========
# NOTE: Gemini의 response_mime_type="application/json" + response_schema로 강제하여
#       자유 형식 텍스트가 섞이지 않도록 한다.
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
   - 가능하면 핵심 사건만 명시하고, 묘사 본문은 메인 GM 모델에 일임하세요.

[엄격한 규칙]
- 응답은 정확히 하나의 action만 가집니다.
- 같은 플레이어 발언에 대해 ASK를 반복하지 마세요. 한 번 명확화한 뒤에는 PROCEED 또는 ROLL로 진행합니다.
- 시나리오 종결(엔딩) 판단은 절대 하지 마세요. 그것은 인간 GM의 권한입니다.
- 시나리오 룰북에 정의되지 않은 새 세력·이벤트·구조물을 임의 생성하지 마세요.
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

    AI가 가끔 ##·**·- 등 마크다운을 섞어 출력할 때를 대비한 후처리 필터.
    """
    if not instruction:
        return ""
    lines = instruction.strip().splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r'^[#\s]+', '', line)          # 줄 앞 # 헤더 제거
        line = re.sub(r'^[-*+>\s]+(?=[^\s])', '', line)  # 불릿/인용(앞에 내용 있을 때만) 제거
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)   # **굵은글씨** 제거
        line = re.sub(r'\*([^*]+)\*', r'\1', line)       # *기울임* 제거
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
        player_message (str): 플레이어 신규 발언
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

    return f"""[현재 턴 #]: {session.turn_count + 1}
[대상 PC]: {target_char}
[PC 프로필]: {pc_profile_summary or "(미설정)"}
[PC 자원]: {res_str}
[PC 상태]: {sta_str}
[직전 ASK 횟수 / 한도]: {clarify_count} / {MAX_CLARIFY_PER_MESSAGE}
{note_block}
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

    버튼 클릭 시 주사위를 굴리고, 결과를 컨텍스트에 주입하여 GM-Logic을 재호출합니다.
    5분 내 미클릭 시 자동으로 주사위를 굴립니다(타임아웃 폴백).

    Args:
        cog (AutoGMCog): 부모 Cog 참조 (계속 처리를 위해 필요)
        session: TRPGSession
        roll_specs (list): 굴려야 할 판정 목록 (GM-Logic이 결정한 rolls 배열)
        player_message (str): 이 판정을 유발한 플레이어의 원본 발언
        prior_roll_results (list[str]): 이번 턴에 이미 수집된 굴림 결과 목록
        target_uid (str | None): 버튼을 누를 수 있는 플레이어의 디스코드 UID
    """

    def __init__(self, cog, session, roll_specs: list, player_message: str,
                 prior_roll_results: list, target_uid: str | None):
        super().__init__(timeout=300)  # 5분 대기
        self.cog = cog
        self.session = session
        self.roll_specs = roll_specs
        self.player_message = player_message
        self.prior_roll_results = list(prior_roll_results)
        self.target_uid = target_uid
        self._resolved = False  # 중복 처리 방지 플래그

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # 대상 플레이어 검증
        if self.target_uid and str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message(
                "> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True
            )

        if self._resolved:
            return await interaction.response.send_message(
                "> 이미 처리된 판정입니다.", ephemeral=True
            )
        self._resolved = True

        # 버튼 비활성화 후 응답
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="> ⏳ 판정 결과를 처리 중입니다...", view=self
        )
        self.stop()

        # 실제 굴림 + GM-Logic 재호출 (비동기 태스크)
        asyncio.create_task(self._process_roll(interaction.channel))

    async def _process_roll(self, game_ch):
        """주사위 굴림 실행 후 _continue_with_roll_results 호출."""
        new_results = await self.cog._execute_rolls(self.session, self.roll_specs, game_ch)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(self.session, self.player_message, combined)
        )

    async def on_timeout(self):
        """5분 내 미클릭 시 자동 굴림 (폴백)."""
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

    NOTE: !자동시작이 호출된 세션에서만 동작. 비활성 세션에는 영향 없음(기존 흐름 보존).
    """

    def __init__(self, bot):
        self.bot = bot
        # 세션별 직렬화 락 (한 세션의 GM-Logic 호출이 동시에 두 번 돌지 않도록)
        self._session_locks = {}

    def _lock_for(self, session):
        if session.session_id not in self._session_locks:
            self._session_locks[session.session_id] = asyncio.Lock()
        return self._session_locks[session.session_id]

    # ---------- 명령어 ----------

    @commands.command(name="자동시작")
    async def auto_start(self, ctx, target_char: str = None):
        """
        자동 GM 모드 활성화. 게임 채널의 플레이어 발언이 모두 자동 GM에게 라우팅됨.

        Args:
            target_char (str): GM이 대화할 대상 PC 이름. 미지정 시 등록된 단일 PC 자동 선택.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "is_started", False):
            return await ctx.send("⚠️ 세션이 시작되지 않았습니다. `!시작`을 먼저 실행하세요.")

        if not target_char:
            if len(session.players) == 1:
                target_char = next(iter(session.players.values())).get("name")
            else:
                names = [p.get("name") for p in session.players.values()]
                return await ctx.send(
                    f"⚠️ 대상 PC를 지정해주세요. 예: `!자동시작 {names[0] if names else '캐릭터명'}`\n"
                    f"(현재 등록된 PC: {', '.join(names) if names else '없음'})"
                )

        # PC 존재 검증
        if not core.get_uid_by_char_name(session, target_char):
            return await ctx.send(f"⚠️ '{target_char}' PC를 찾을 수 없습니다.")

        session.auto_gm_active = True
        session.auto_gm_target_char = target_char
        session.auto_gm_turns_done = 0
        session.auto_gm_clarify_count = 0
        session.auto_gm_cost_baseline = session.total_cost
        session.auto_gm_side_note = ""
        await core.save_session_data(self.bot, session)

        await ctx.send(
            f"🤖 **[자동 GM 모드 활성화]**\n"
            f"- 대상 PC: **{target_char}**\n"
            f"- 자동 턴 한도: {session.auto_gm_turn_cap}턴\n"
            f"- 자동 누적 비용 한도: {core.format_cost(session.auto_gm_cost_cap_krw)}\n"
            f"- 게임 채널의 플레이어 발언을 모두 GM 모델이 처리합니다.\n"
            f"- 중단: `!자동중단`  /  GM에게 메모: `!자동개입 [텍스트]`"
        )

    @commands.command(name="자동중단")
    async def auto_stop(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "auto_gm_active", False):
            return await ctx.send("⚠️ 자동 GM 모드가 활성 상태가 아닙니다.")

        session.auto_gm_active = False
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
        await ctx.send(
            f"🤖 **[자동 GM 상태]**\n"
            f"- 활성: {'✅ 켜짐' if active else '⛔ 꺼짐'}\n"
            f"- 대상 PC: {session.auto_gm_target_char or '(없음)'}\n"
            f"- 자동 처리 턴: {session.auto_gm_turns_done} / {session.auto_gm_turn_cap}\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)} / {core.format_cost(session.auto_gm_cost_cap_krw)}\n"
            f"- 직전 ASK 횟수: {session.auto_gm_clarify_count}\n"
            f"- 대기 중 사이드 노트: {session.auto_gm_side_note or '(없음)'}"
        )

    @commands.command(name="자동개입")
    async def auto_inject(self, ctx, *, text: str = ""):
        """
        다음 PROCEED 완료 시까지 GM 사이드 노트를 유지. (#14: 턴 단위 적용)
        PROCEED가 실행될 때 자동으로 해제됨.
        """
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
            return await ctx.send("⚠️ 사용법: `!자동턴제한 [1~100]` (자동 모드에서 자동 진행할 최대 턴 수)")

        session.auto_gm_turn_cap = n
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 턴 한도를 {n}턴으로 변경했습니다.")

    # ---------- 메시지 리스너 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 봇 자신 / 다른 봇 메시지 무시
        if message.author.bot:
            return
        # 명령어 프리픽스는 무시 (인간 GM의 명령은 따로 처리됨)
        if message.content.startswith("!"):
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return
        # 게임 채널만 수집. 마스터 채널 발언은 명령어 외엔 무시.
        if message.channel.id != session.game_ch_id:
            return
        if not getattr(session, "auto_gm_active", False):
            return

        # 비동기 백그라운드 처리 (on_message 핸들러 블록 방지)
        asyncio.create_task(self._handle_player_message(session, message))

    # ---------- 처리 루프 ----------

    async def _handle_player_message(self, session, message: discord.Message):
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            # 활성 상태 재확인 (대기 중 비활성화될 수 있음)
            if not session.auto_gm_active:
                return

            # ----- 안전장치 -----
            # #15: turns_done >= cap이면 처리 거부 (마지막 턴은 PROCEED 분기에서 처리)
            if session.auto_gm_turns_done >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                    f"`!자동시작`으로 재개하거나 `!자동턴제한`으로 한도를 늘리세요."
                )
                await core.save_session_data(self.bot, session)
                return

            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 모드 누적 비용 한도({core.format_cost(session.auto_gm_cost_cap_krw)}) 도달. "
                    f"필요 시 한도 조정 후 `!자동시작`으로 재개하세요."
                )
                await core.save_session_data(self.bot, session)
                return

            # 새 플레이어 발언 → ASK 카운터 리셋
            session.auto_gm_clarify_count = 0

            # 플레이어 발언을 current_turn_logs에 기록 (인간 GM이 보던 흐름과 동일)
            char_name = session.auto_gm_target_char or message.author.display_name
            session.current_turn_logs.append(f"[{char_name}]: {message.content.strip()}")

            roll_results: list[str] = []

            # 액션 레이블 (마스터 채널 출력용)
            action_labels = {
                "ASK":     "🟡 ASK (명확화 요청)",
                "ROLL":    "🎲 ROLL (주사위 판정)",
                "PROCEED": "🟢 PROCEED (턴 진행)",
            }

            for iteration in range(MAX_ITERATIONS_PER_MESSAGE):
                decision = await self._call_gm_logic(session, message.content.strip(), roll_results, master_ch)
                if not decision:
                    await m_send("⚠️ 자동 GM 결정 호출 실패. 이번 발언을 스킵합니다.")
                    return

                action = decision.get("action", "ASK").upper()
                reasoning = decision.get("reasoning", "")

                # #7: 판단 결과를 콘솔 + 마스터 채널 동시 출력
                label = action_labels.get(action, action)
                print(f"[AutoGM/{session.session_id}] iter={iteration} action={action} :: {reasoning[:120]}")
                await m_send(
                    f"🤖 **[자동 GM 판단 #{iteration + 1}]** {label}\n"
                    f"> {reasoning[:200]}"
                )

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
                        # #15: 마지막 턴 체크
                        if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                            session.auto_gm_active = False
                            await m_send(
                                f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                                f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                            )
                        await self._dispatch_proceed(session, forced_instr)
                        session.auto_gm_clarify_count = 0
                        session.auto_gm_turns_done += 1
                        session.auto_gm_side_note = ""   # #14: PROCEED 후 사이드 노트 해제
                        break

                    bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                    game_ch = self.bot.get_channel(session.game_ch_id)
                    if game_ch:
                        await core.stream_text_to_channel(
                            self.bot, game_ch, bridge,
                            words_per_tick=5, tick_interval=1.5
                        )
                    # NOTE: ASK 브리지를 current_turn_logs에 기록 → 세션 JSON에 저장되고,
                    # 다음 GM-Logic 호출 시 [이번 턴 누적 대화] 블록에 포함되어
                    # "이미 이 질문을 했다"는 맥락을 인지할 수 있게 됨.
                    session.current_turn_logs.append(f"[진행자 (자동 GM)]: {bridge}")
                    print(f"[AutoGM/{session.session_id}] ASK -> '{bridge[:80]}'")
                    break

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
                        # #15: 마지막 턴 체크
                        if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                            session.auto_gm_active = False
                            await m_send(
                                f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                                f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                            )
                        await self._dispatch_proceed(session, fallback_instr)
                        session.auto_gm_turns_done += 1
                        session.auto_gm_side_note = ""   # #14
                        break

                    # #10: 버튼 UI 전송 후 핸들러 종료 (계속 처리는 버튼 콜백 담당)
                    await self._dispatch_rolls(session, rolls, message.content.strip(), list(roll_results))
                    await core.save_session_data(self.bot, session)
                    return  # 핸들러 종료; 버튼 콜백이 _continue_with_roll_results를 호출

                elif action == "PROCEED":
                    instruction = _clean_proceed_instruction(
                        decision.get("proceed_instruction") or ""
                    )
                    if not instruction:
                        instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                    # #15: 마지막 턴이면 묘사 시작 직전에 종료 알림
                    if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                        session.auto_gm_active = False
                        await m_send(
                            f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                            f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                        )

                    await self._dispatch_proceed(session, instruction)
                    session.auto_gm_turns_done += 1
                    session.auto_gm_side_note = ""   # #14: PROCEED 완료 후 사이드 노트 해제
                    break

                else:
                    await m_send(f"⚠️ 자동 GM이 알 수 없는 action을 반환했습니다: {action}")
                    break

            else:
                # iteration 한도 도달 → 강제 PROCEED
                await m_send(f"⚙️ 자동 GM 내부 루프 한도({MAX_ITERATIONS_PER_MESSAGE}) 도달 → 강제 PROCEED.")
                # #15: 마지막 턴 체크
                if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                    session.auto_gm_active = False
                    await m_send(
                        f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                        f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                    )
                await self._dispatch_proceed(session, "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오.")
                session.auto_gm_turns_done += 1
                session.auto_gm_side_note = ""   # #14

            # #14: 사이드 노트는 각 PROCEED 경로에서 개별 해제 (이곳에서는 하지 않음)
            await core.save_session_data(self.bot, session)

    # ---------- GM-Logic 호출 ----------

    async def _call_gm_logic(self, session, player_message: str, roll_results: list, master_ch) -> dict | None:
        """
        GM-Logic 모델 호출. DEFAULT_MODEL 사용.

        Returns:
            dict: 파싱된 결정 JSON. 실패 시 None.
        """
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

        # 비용 정산 + #13: 마스터 채널 비용 보고
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

            # #13: 마스터 채널에도 비용 보고
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
            # 모델이 코드 펜스로 감싼 경우 등 폴백 파싱
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

    # ---------- ROLL 실행 및 버튼 디스패치 ----------

    async def _execute_rolls(self, session, rolls: list, game_ch) -> list[str]:
        """
        rolls 목록을 random.randint로 즉시 굴리고, 결과를 게임/마스터 채널 양쪽에 선언.

        Returns:
            list[str]: 다음 GM-Logic 호출 시 컨텍스트로 주입할 결과 문자열 목록.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        results: list[str] = []

        for r in rolls:
            char_name = r.get("char_name") or session.auto_gm_target_char or "?"
            stat_name = r.get("stat") or ""
            sides = int(r.get("sides") or 20)
            weight = int(r.get("weight") or 0)

            # 스탯 값 조회
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
                # NOTE: 직접 send()는 stream_text_to_channel을 거치지 않으므로 명시적으로 로그 기록.
                core.write_log(session.session_id, "game_chat", f"[판정]: {line}")
            if master_ch:
                await master_ch.send(f"🤖 [자동 GM 굴림]\n{line}")
            session.current_turn_logs.append(logic_line.lstrip("- "))
            results.append(logic_line)

        return results

    async def _dispatch_rolls(self, session, rolls: list, player_message: str, prior_roll_results: list):
        """
        #10: ROLL 결정 시 자동 굴림 대신 플레이어에게 버튼 UI를 전송.

        버튼 클릭(또는 타임아웃) 후 AutoGMRollView가 _continue_with_roll_results를 호출한다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        target_uid = core.get_uid_by_char_name(session, session.auto_gm_target_char)

        # 판정 내용 요약 (버튼 메시지용)
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

    # ---------- ROLL 결과 반영 계속 처리 ----------

    async def _continue_with_roll_results(self, session, player_message: str, roll_results: list):
        """
        AutoGMRollView 버튼 클릭 후 굴림 결과를 반영하여 GM-Logic을 재호출하고 PROCEED.

        ROLL 결과가 나온 뒤 항상 PROCEED를 기대하지만, 안전을 위해 다른 action도 처리.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.auto_gm_active:
                return

            # 비용 한도 재확인
            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 자동 정지]** 자동 모드 누적 비용 한도 도달. "
                    f"필요 시 한도 조정 후 `!자동시작`으로 재개하세요."
                )
                await core.save_session_data(self.bot, session)
                return

            # GM-Logic 재호출 (굴림 결과 반영)
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

            # #15: 마지막 턴 체크
            if (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[자동 GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                    f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                )

            await self._dispatch_proceed(session, instruction)
            session.auto_gm_turns_done += 1
            session.auto_gm_side_note = ""   # #14: PROCEED 완료 후 사이드 노트 해제
            await core.save_session_data(self.bot, session)

    # ---------- PROCEED 디스패치 ----------

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
