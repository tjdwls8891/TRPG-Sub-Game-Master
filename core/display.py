# 디스플레이 채널 — 세션 상태 표기와 UI를 단일 메시지로 유지한다
#
# [단일 메시지 편집 방식]
#   채널에 메시지 하나를 두고 edit()으로 갱신한다. 매번 새로 보내면 채널이
#   지저분해지고 이전 UI 버튼이 살아남아 중복 조작이 가능해진다.
#
# [갱신 계층을 코드로 나누지 않는 이유]
#   기획서는 갱신 시점을 5계층으로 구분하지만, 임베드 조립은 API 호출이 아니라
#   문자열 작업이라 부분 갱신의 이득이 없다. refresh()는 항상 전체를 다시 그리고,
#   계층 구분은 '언제 호출하는가'로만 표현한다.
import discord

from . import media_control
from .cost import format_cost
from .ink import format_ink, cost_to_ink
from .timeline import format_timeline
from .constants import CACHE_TTL_SECONDS, TTS_NARRATOR_VOICE
from .estimate import estimate_session_open
import time


def _flag_style(on: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.success if on else discord.ButtonStyle.secondary


def build_embed(session) -> discord.Embed:
    """표기 22종을 임베드로 조립한다."""
    # NOTE: 현재 모든 세션에 마스터 채널이 생성되므로 master_ch_id 유무로는
    #       구분할 수 없다. 기획상 마스터 세션은 권한자가 명시적으로 선택해
    #       여는 것이므로 session_kind 필드로 판정한다.
    kind = {"master": "마스터", "multi": "멀티", "solo": "솔로"}.get(
        getattr(session, "session_kind", "solo") or "solo", "솔로")
    is_open = bool(getattr(session, "cache_name", None))
    private = "비공개" if getattr(session, "is_private", False) else "공개"

    embed = discord.Embed(
        title=f"🎲 {getattr(session, 'scenario_id', '(시나리오 미정)')}",
        description=(
            f"세션 {kind} · {private} · "
            f"{'🟢 오픈' if is_open else '⚫ 클로즈'}\n"
            f"`{getattr(session, 'session_id', '?')}`"
        ),
        color=0x2ECC71 if is_open else 0x95A5A6,
    )

    # ── 진행 상태 ──
    tl = getattr(session, "world_timeline", {}) or {}
    embed.add_field(
        name="진행",
        value=(
            f"턴 {getattr(session, 'turn_count', 0)}\n"
            f"{format_timeline(tl) or '(미확인)'}"
        ),
        inline=True,
    )

    # ── 비용 ──
    total = getattr(session, "total_cost", 0.0) or 0.0
    est = getattr(session, "last_estimate", {}) or {}
    last = getattr(session, "last_turn_cost", 0.0) or 0.0
    cost_lines = [f"총 {cost_to_ink(total)}잉크 ({format_cost(total)})"]
    if last:
        cost_lines.append(f"직전 턴 {cost_to_ink(last)}잉크 ({format_cost(last)})")
    if est:
        cost_lines.append(f"다음 턴 {est.get('min_ink', 0)}~{est.get('max_ink', 0)}잉크")
    prepaid = getattr(session, "compression_prepaid_krw", 0.0) or 0.0
    if prepaid:
        cost_lines.append(f"압축 선결제 {prepaid:.1f}원")
    embed.add_field(name="비용", value="\n".join(cost_lines), inline=True)

    # ── 세션 오픈 정보 ──
    # 기획 규정: 오픈 비용·클로즈 예정 시점·예정 비용을 명시한다.
    if is_open:
        created = getattr(session, "cache_created_at", 0.0) or 0.0
        tokens = getattr(session, "cache_read_tokens", 0) or getattr(session, "cache_tokens", 0) or 0
        open_lines = [f"캐시 {tokens:,} 토큰"]
        if created:
            expire = created + CACHE_TTL_SECONDS
            remain = max(0, int(expire - time.time()))
            open_lines.append(
                f"만료 예정 <t:{int(expire)}:t> (남은 {remain // 3600}시간 {remain % 3600 // 60}분)")
        try:
            plan = estimate_session_open(session, CACHE_TTL_SECONDS / 3600)
            open_lines.append(f"오픈·유지 예정 {plan['total_ink']}잉크")
        except Exception:
            pass
        embed.add_field(name="세션 오픈", value="\n".join(open_lines), inline=True)

    # ── 미디어 ──
    embed.add_field(
        name="미디어",
        value=(
            f"{media_control.format_flags(session)}\n"
            f"(TTS 실효: {'ON' if media_control.is_enabled(session, 'tts') else 'OFF'})\n"
            f"볼륨 {int((getattr(session, 'volume', 0.3) or 0) * 100)}% · "
            f"BGM {getattr(session, 'current_bgm', None) or '(없음)'}\n"
            f"목소리 {getattr(session, 'tts_voice', '') or TTS_NARRATOR_VOICE}"
        ),
        inline=False,
    )

    # ── 플레이어 ──
    lines = []
    for _uid, p in (getattr(session, "players", {}) or {}).items():
        if not isinstance(p, dict):
            continue
        name = p.get("name") or "?"
        sta = (getattr(session, "statuses", {}) or {}).get(name) or []
        res = (getattr(session, "resources", {}) or {}).get(name) or {}
        lines.append(
            f"**{name}** — {p.get('profile') or '(미배분)'}\n"
            f"상태: {', '.join(sta) if sta else '없음'}"
            + (f" · 소지: {', '.join(f'{k} {v}' for k, v in list(res.items())[:4])}" if res else "")
        )
    if lines:
        embed.add_field(name="플레이어", value="\n".join(lines)[:1000], inline=False)

    # ── 서사 ──
    ex = getattr(session, "last_extraction", {}) or {}
    sit = ex.get("situation") or {}
    qp = ex.get("quest_progress") or {}
    npcs = ex.get("npcs_met") or []
    narr = [
        f"장면 {sit.get('tag', '(미확인)')} · 긴장 {sit.get('tension', 0)}",
        f"진행 {qp.get('advance', 0)} · 이탈 {qp.get('deviation', 0)}",
    ]
    if npcs:
        narr.append(f"등장 NPC: {', '.join(npcs[:6])}")
    embed.add_field(name="서사", value="\n".join(narr), inline=False)

    # ── 기억 ──
    # NOTE: TTS 표기는 미디어 필드에 일원화한다. is_enabled('tts')가
    #       media_flags와 tts_enabled를 함께 보므로 두 곳에 적으면 어긋난다.
    plan_label = {"normal": "노멀", "high": "하이", "low": "로우", "ultra": "울트라"}
    mode_label = {"quest": "퀘스트", "free": "풀자유"}
    embed.add_field(
        name="기억·서사",
        value=(
            f"기억 방식 {plan_label.get(getattr(session, 'memory_plan', 'normal'), '노멀')} "
            f"(압축 {len(getattr(session, 'compressed_memory', '') or '')}자)\n"
            f"서사설계 {mode_label.get(getattr(session, 'narrative_mode', 'quest'), '퀘스트')}"
        ),
        inline=True,
    )

    if getattr(session, "extraction_pending", False):
        embed.add_field(
            name="⚠️ 주의",
            value="이전 턴 정보 정리가 완료되지 않았습니다. 다음 턴이 차단됩니다.",
            inline=False,
        )
    return embed


class DisplayView(discord.ui.View):
    """
    디스플레이 UI — persistent view.

    접촉 권한은 interaction_check로 일괄 적용한다. 버튼마다 검사를 넣으면
    누락이 생기고, 새 버튼 추가 시 잊기 쉽다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message(
                "세션을 찾을 수 없습니다.", ephemeral=True)
            return False
        if await self.bot.is_owner(interaction.user):
            return True
        if str(interaction.user.id) in (getattr(session, "players", {}) or {}):
            return True
        await interaction.response.send_message(
            "이 세션의 참가자만 조작할 수 있습니다.", ephemeral=True)
        return False

    def _busy(self, session) -> bool:
        return bool(getattr(session, "is_processing", False))

    async def _toggle(self, interaction, key: str):
        session = self.bot.active_sessions.get(interaction.channel.id)
        flags = media_control.get_media_flags(session)
        new = not flags.get(key, True)
        if key == "tts":
            media_control.sync_tts_flag(session, new)
        else:
            media_control.set_media_flag(session, key, new)
        note = ""
        if key == "bgm":
            if new:
                note = media_control.describe_bgm_pending(session)
            else:
                # 기획 규정 — 오프 시 즉시 페이드아웃한다.
                cog = self.bot.get_cog("MediaCog")
                if cog:
                    try:
                        await cog.stop_bgm(session)
                    except Exception as e:
                        print(f"[BGM] 정지 실패: {e}")
                else:
                    session.pending_bgm = None
        await interaction.response.edit_message(
            embed=build_embed(session), view=self)
        if note:
            await interaction.followup.send(note, ephemeral=True)

    @discord.ui.button(label="🔊 TTS", style=discord.ButtonStyle.secondary,
                       custom_id="disp:tts", row=0)
    async def tts(self, interaction, _b):
        await self._toggle(interaction, "tts")

    @discord.ui.button(label="🖼 이미지", style=discord.ButtonStyle.secondary,
                       custom_id="disp:image", row=0)
    async def image(self, interaction, _b):
        await self._toggle(interaction, "image")

    @discord.ui.button(label="🎵 BGM", style=discord.ButtonStyle.secondary,
                       custom_id="disp:bgm", row=0)
    async def bgm(self, interaction, _b):
        await self._toggle(interaction, "bgm")

    @discord.ui.button(label="🔔 효과음", style=discord.ButtonStyle.secondary,
                       custom_id="disp:sfx", row=0)
    async def sfx(self, interaction, _b):
        await self._toggle(interaction, "sfx")

    @discord.ui.button(label="🔉 −", style=discord.ButtonStyle.secondary,
                       custom_id="disp:vol_down", row=1)
    async def vol_down(self, interaction, _b):
        session = self.bot.active_sessions.get(interaction.channel.id)
        session.volume = max(0.0, round((getattr(session, "volume", 0.3) or 0) - 0.1, 2))
        await interaction.response.edit_message(embed=build_embed(session), view=self)

    @discord.ui.button(label="🔊 +", style=discord.ButtonStyle.secondary,
                       custom_id="disp:vol_up", row=1)
    async def vol_up(self, interaction, _b):
        session = self.bot.active_sessions.get(interaction.channel.id)
        session.volume = min(1.0, round((getattr(session, "volume", 0.3) or 0) + 0.1, 2))
        await interaction.response.edit_message(embed=build_embed(session), view=self)

    @discord.ui.button(label="⏪ 1턴 되감기", style=discord.ButtonStyle.danger,
                       custom_id="disp:rewind", row=2)
    async def rewind(self, interaction, _b):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if self._busy(session):
            await interaction.response.send_message(
                "턴 진행 중에는 되감을 수 없습니다.", ephemeral=True)
            return
        # 순환 임포트를 피하기 위해 지연 임포트한다.
        from .rewind import available_range
        oldest, newest = available_range(session)
        if newest == 0:
            await interaction.response.send_message(
                "되감기 기록이 아직 없습니다.", ephemeral=True)
            return
        target = newest - 1
        if target < oldest:
            await interaction.response.send_message(
                f"되감기 가능 범위는 {oldest}~{newest}턴입니다.", ephemeral=True)
            return
        cog = self.bot.get_cog("GMCog")
        confirm_cls = getattr(__import__("cogs.gm", fromlist=["RewindConfirmView"]),
                              "RewindConfirmView")
        await interaction.response.send_message(
            f"⚠️ **{newest}턴을 제거하고 {target}턴 종료 시점으로 되돌립니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.",
            view=confirm_cls(self.bot, session, target),
            ephemeral=False,
        )

    @discord.ui.button(label="🔄 턴 재시작", style=discord.ButtonStyle.danger,
                       custom_id="disp:restart", row=2)
    async def restart(self, interaction, _b):
        """직전 턴을 되감고 곧바로 선언 질문부터 다시 진행한다."""
        session = self.bot.active_sessions.get(interaction.channel.id)
        if self._busy(session):
            await interaction.response.send_message(
                "턴 진행 중에는 재시작할 수 없습니다.", ephemeral=True)
            return
        from .rewind import available_range
        _oldest, newest = available_range(session)
        if newest == 0:
            await interaction.response.send_message(
                "재시작할 턴이 없습니다.", ephemeral=True)
            return
        confirm_cls = getattr(__import__("cogs.gm", fromlist=["RewindConfirmView"]),
                              "RewindConfirmView")
        await interaction.response.send_message(
            f"⚠️ **{newest}턴을 취소하고 선언부터 다시 진행합니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.",
            view=confirm_cls(self.bot, session, newest - 1),
        )

    @discord.ui.button(label="⏻ 세션 오픈/클로즈", style=discord.ButtonStyle.secondary,
                       custom_id="disp:session", row=3)
    async def session_toggle(self, interaction, _b):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if self._busy(session):
            await interaction.response.send_message(
                "턴 진행 중에는 조작할 수 없습니다.", ephemeral=True)
            return
        is_open = bool(getattr(session, "cache_name", None))
        if is_open:
            await interaction.response.send_message(
                "세션 클로즈는 캐시 만료 처리와 환급 정산이 함께 필요합니다. "
                "결제 시스템 도입 후 활성화됩니다.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "세션 오픈은 캐시 업로드 시간 입력이 선행됩니다. "
                "세션 생성 플로우 도입 후 활성화됩니다.", ephemeral=True)

    @discord.ui.button(label="💰 결제", style=discord.ButtonStyle.primary,
                       custom_id="disp:pay", row=3)
    async def pay(self, interaction, _b):
        await interaction.response.send_message(
            "결제 기능은 계정·약관 시스템 도입 후 활성화됩니다.", ephemeral=True)


def build_view(bot, session) -> discord.ui.View:
    """UI 뷰를 조립한다. 턴 진행 중이면 민감한 버튼을 회색 비활성화한다.

    기획 규정 — 턴 진행 중이거나 충돌 가능성 있는 모든 시점에 UI를 비활성화.
    대상: 턴 회귀 · 턴 재시작 · TTS/이미지 온오프 · 세션 오픈/클로즈
    (볼륨·BGM·효과음은 진행 중에도 안전하므로 유지한다)
    """
    view = DisplayView(bot)
    if getattr(session, "is_processing", False):
        for child in view.children:
            cid = getattr(child, "custom_id", "")
            if cid in ("disp:rewind", "disp:restart", "disp:tts",
                       "disp:image", "disp:session"):
                child.disabled = True
    return view


async def refresh(bot, session, *, reason: str = "") -> bool:
    """디스플레이 메시지를 갱신한다. 실패해도 게임 진행을 막지 않는다.

    메시지가 없으면 새로 만들고 id를 기록한다.
    """
    ch_id = getattr(session, "display_ch_id", None)
    if not ch_id:
        return False
    channel = bot.get_channel(ch_id)
    if channel is None:
        return False

    embed = build_embed(session)
    view = build_view(bot, session)
    msg_id = getattr(session, "display_msg_id", None)

    try:
        if msg_id:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
            return True
    except Exception:
        # 메시지가 삭제됐거나 접근 불가 — 새로 만든다.
        pass

    try:
        msg = await channel.send(embed=embed, view=view)
        session.display_msg_id = msg.id
        return True
    except Exception as e:
        print(f"[디스플레이] 갱신 실패 ({reason}): {e}")
        return False
