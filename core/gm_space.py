# 서버 GM 스페이스 — 홈·명예의 전당·서버보드·월드보드
#
# [생성 시점]
#   세션이 열리거나 보드 갱신 시점이거나 서버 참가 시 카테고리·채널을 만든다.
#
# [갱신 시점]
#   세션 클로즈 시점에 명전과 보드를 일괄 갱신하고 서버를 나간 인원을 제거한다.
#   매 턴 갱신하면 API 호출이 낭비된다.
#
# [접근 통제]
#   계정 미등록자는 UI 접근이 차단된다. 등록 버튼 외의 UI를 누르면
#   회색으로 짧게 비활성화하며 안내한다(기획 규정).
import discord

from . import accounts
from . import stats as stats_mod

CATEGORY_NAME = "GM 스페이스"
CH_HOME = "gm-스페이스"
CH_HALL = "명예의-전당"
CH_SERVER_BOARD = "서버보드"
CH_WORLD_BOARD = "월드보드"

# 미등록자가 UI를 눌렀을 때 안내를 띄우는 시간(초).
NOTICE_SECONDS = 5

# 보드에 노출할 최대 인원.
BOARD_LIMIT = 20


def _channel_names() -> list:
    return [CH_HOME, CH_HALL, CH_SERVER_BOARD, CH_WORLD_BOARD]


async def ensure_space(guild) -> dict:
    """GM 스페이스 카테고리와 채널 4종을 보장한다.

    이미 있으면 그대로 쓴다. 채팅은 봇만 가능하도록 막는다 —
    기획 규정상 GM 스페이스는 채팅 불가이며, chat_guard가 2차로 삭제한다.

    Returns:
        {"category": CategoryChannel, "home": TextChannel, ...}
    """
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(CATEGORY_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(send_messages=False),
        guild.me: discord.PermissionOverwrite(send_messages=True),
    }

    out = {"category": category}
    key_map = {CH_HOME: "home", CH_HALL: "hall",
               CH_SERVER_BOARD: "server_board", CH_WORLD_BOARD: "world_board"}
    for name in _channel_names():
        ch = discord.utils.get(category.text_channels, name=name)
        if ch is None:
            ch = await guild.create_text_channel(
                name, category=category, overwrites=overwrites)
        out[key_map[name]] = ch
    return out


def build_home_embed(bot) -> discord.Embed:
    """홈 임베드 — 봇 버전과 안내."""
    from .constants import __version__

    embed = discord.Embed(
        title="🎲 INDAIM — GM 스페이스",
        description=(
            "세션 생성과 계정 관리를 이곳에서 진행합니다.\n"
            "아래 버튼으로 조작하십시오. 이 채널은 채팅이 불가합니다."
        ),
        color=0x5865F2,
    )
    embed.add_field(name="봇 버전", value=f"v{__version__}", inline=True)
    invite = getattr(bot, "invite_url", "") or "(미설정)"
    embed.add_field(name="초대 링크", value=invite, inline=True)
    embed.set_footer(text="계정 등록 후 세션을 열 수 있습니다.")
    return embed


def build_hall_embed(member_ids: list, guild_name: str = "") -> discord.Embed:
    """명예의 전당 — 등록자만, 플레이 턴 순위.

    기획 규정 — 홈에서 등록한 인원에 한해 통계 프로필을 등록한다.
    """
    rows = stats_mod.leaderboard(member_ids, key="turns",
                                 only_registered=True, limit=BOARD_LIMIT)
    embed = discord.Embed(
        title="🏆 명예의 전당",
        description=f"{guild_name} · 플레이 턴 순위" if guild_name else "플레이 턴 순위",
        color=0xF1C40F,
    )
    if not rows:
        embed.description += "\n\n(등록된 인원이 없습니다)"
        return embed

    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows):
        st = row["stats"]
        mark = medals[i] if i < 3 else f"{i + 1}."
        embed.add_field(
            name=f"{mark} <@{row['user_id']}>",
            value=stats_mod.format_summary(st),
            inline=False,
        )
    return embed


def build_board_embed(member_ids: list, *, world: bool = False) -> discord.Embed:
    """서버보드 / 월드보드.

    기획 규정 — 기본값은 아이디만 공개.
      서버보드: 명전 등록 시 프로필로 전환
      월드보드: 공개 선택제(public 플래그)
    """
    rows = stats_mod.leaderboard(member_ids, key="turns", limit=BOARD_LIMIT)
    embed = discord.Embed(
        title="🌐 월드보드" if world else "📋 서버보드",
        description="플레이 턴 순위",
        color=0x3498DB,
    )
    if not rows:
        embed.description += "\n\n(기록이 없습니다)"
        return embed

    lines = []
    for i, row in enumerate(rows, start=1):
        st = row["stats"]
        # 프로필 전환 조건이 보드마다 다르다.
        expanded = st.get("public") if world else st.get("hall_registered")
        if expanded:
            lines.append(f"**{i}.** <@{row['user_id']}> — 턴 {row['value']:,} · "
                         f"세션 {st.get('sessions', 0)} · NPC {st.get('npcs_met', 0)}")
        else:
            lines.append(f"**{i}.** <@{row['user_id']}> — 턴 {row['value']:,}")
    embed.description += "\n\n" + "\n".join(lines)
    return embed


async def refresh_boards(bot, guild) -> bool:
    """명전·보드를 일괄 갱신한다. 세션 클로즈 시점에 호출한다.

    서버를 나간 인원은 member_ids에 없으므로 자연히 제외된다(기획 규정).
    """
    try:
        chans = await ensure_space(guild)
    except Exception as e:
        print(f"[GM스페이스] 채널 보장 실패: {e}")
        return False

    member_ids = [str(m.id) for m in guild.members if not m.bot]

    targets = [
        (chans["hall"], build_hall_embed(member_ids, guild.name)),
        (chans["server_board"], build_board_embed(member_ids, world=False)),
        (chans["world_board"], build_board_embed(member_ids, world=True)),
    ]
    for channel, embed in targets:
        try:
            # 채널당 봇 메시지 하나만 유지한다. 매번 새로 보내면 누적된다.
            existing = None
            async for msg in channel.history(limit=10):
                if msg.author.id == bot.user.id:
                    existing = msg
                    break
            if existing:
                await existing.edit(embed=embed)
            else:
                await channel.send(embed=embed)
        except Exception as e:
            print(f"[GM스페이스] {channel.name} 갱신 실패: {e}")
    return True


class GMHomeView(discord.ui.View):
    """
    GM 홈 UI — persistent view.

    미등록자는 '계정 등록' 외의 버튼을 쓸 수 없다. 버튼을 실제로 비활성화하면
    유저마다 다른 뷰가 필요하므로, 누른 시점에 검사해 안내한다(기획 규정 —
    비등록자 접촉 시 짧게 안내).
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _require_account(self, interaction) -> bool:
        if accounts.is_registered(interaction.user.id):
            return True
        await interaction.response.send_message(
            "🔒 계정 등록 후 이용할 수 있습니다. **계정 등록** 버튼을 눌러 주십시오.",
            ephemeral=True, delete_after=NOTICE_SECONDS,
        )
        return False

    @discord.ui.button(label="📝 계정 등록", style=discord.ButtonStyle.success,
                       custom_id="gmspace:register", row=0)
    async def register(self, interaction, _b):
        if accounts.is_registered(interaction.user.id):
            if accounts.needs_terms_reagreement(interaction.user.id):
                from .terms import start_registration

                sent = await start_registration(self.bot, interaction.user, reagree=True)
                await interaction.response.send_message(
                    "📬 약관이 개정되었습니다. DM에서 재동의를 진행해 주십시오."
                    if sent else
                    "⚠️ 재동의가 필요하지만 DM을 보낼 수 없습니다. "
                    "DM 수신을 허용해 주십시오.",
                    ephemeral=True)
            else:
                await interaction.response.send_message(
                    "이미 등록된 계정입니다.", ephemeral=True)
            return
        from .terms import start_registration

        sent = await start_registration(self.bot, interaction.user)
        await interaction.response.send_message(
            "📬 DM으로 약관을 보냈습니다. 확인 후 동의해 주십시오."
            if sent else
            "⚠️ DM을 보낼 수 없습니다. 서버 개인정보 설정에서 "
            "DM 수신을 허용한 뒤 다시 시도해 주십시오.",
            ephemeral=True)

    @discord.ui.button(label="🎲 세션 열기", style=discord.ButtonStyle.primary,
                       custom_id="gmspace:session", row=0)
    async def open_session(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        await interaction.response.send_message(
            "세션 생성 플로우는 준비 중입니다. 현재는 `!새세션` 명령을 사용하십시오.",
            ephemeral=True)

    @discord.ui.button(label="👤 사전 프로필 관리", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:profiles", row=1)
    async def profiles(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        from .profile_ui import open_manager

        sent = await open_manager(self.bot, interaction.user)
        await interaction.response.send_message(
            "📬 DM으로 프로필 관리 인터페이스를 보냈습니다."
            if sent else
            "⚠️ DM을 보낼 수 없습니다. DM 수신을 허용해 주십시오.",
            ephemeral=True)

    @discord.ui.button(label="💰 잉크 충전", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:charge", row=1)
    async def charge(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        bal = accounts.get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"현재 잔액 **{bal:,}잉크**\n충전은 결제 시스템 도입 후 활성화됩니다.",
            ephemeral=True)

    @discord.ui.button(label="🏆 명전 등록/숨기기", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:hall", row=2)
    async def hall_toggle(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        st = stats_mod.load_stats(interaction.user.id)
        new = not st.get("hall_registered")
        await stats_mod.set_visibility(interaction.user.id, hall_registered=new)
        await interaction.response.send_message(
            f"명예의 전당 등록을 **{'켰습니다' if new else '껐습니다'}**.",
            ephemeral=True)

    @discord.ui.button(label="🌐 월드보드 공개", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:world", row=2)
    async def world_toggle(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        st = stats_mod.load_stats(interaction.user.id)
        new = not st.get("public")
        await stats_mod.set_visibility(interaction.user.id, public=new)
        await interaction.response.send_message(
            f"월드보드 공개를 **{'켰습니다' if new else '껐습니다'}**.",
            ephemeral=True)

    @discord.ui.button(label="📊 내 통계 (DM)", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:stats", row=3)
    async def my_stats(self, interaction, _b):
        if not await self._require_account(interaction):
            return
        st = stats_mod.load_stats(interaction.user.id)
        embed = discord.Embed(title="📊 내 통계",
                              description=stats_mod.format_summary(st),
                              color=0x2ECC71)
        try:
            await interaction.user.send(embed=embed)
            await interaction.response.send_message("DM으로 보냈습니다.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔄 보드 갱신", style=discord.ButtonStyle.secondary,
                       custom_id="gmspace:refresh", row=3)
    async def refresh(self, interaction, _b):
        await interaction.response.defer(ephemeral=True)
        ok = await refresh_boards(self.bot, interaction.guild)
        await interaction.followup.send(
            "보드를 갱신했습니다." if ok else "갱신에 실패했습니다.", ephemeral=True)


async def refresh_home(bot, guild) -> bool:
    """홈 채널의 안내와 UI를 갱신한다."""
    try:
        chans = await ensure_space(guild)
        channel = chans["home"]
        embed = build_home_embed(bot)
        view = GMHomeView(bot)
        existing = None
        async for msg in channel.history(limit=10):
            if msg.author.id == bot.user.id:
                existing = msg
                break
        if existing:
            await existing.edit(embed=embed, view=view)
        else:
            await channel.send(embed=embed, view=view)
        # chat_guard가 GM 스페이스 채팅을 막을 수 있도록 채널 id를 알린다.
        bot.gm_space_ch_id = channel.id
        return True
    except Exception as e:
        print(f"[GM스페이스] 홈 갱신 실패: {e}")
        return False
