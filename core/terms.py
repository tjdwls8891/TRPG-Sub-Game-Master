# 약관 동의 · 계정 등록 — DM 인터페이스
#
# [DM으로 진행하는 이유]
#   기획 규정 — 계정 등록은 DM으로 이동 안내. 약관 전문과 동의 절차가
#   공개 채널에 남으면 다른 참가자에게 방해가 되고, 개인정보 안내를
#   공개 채널에서 처리하는 것도 적절하지 않다.
#
# [재동의]
#   동의한 약관 버전을 저장하고, 세션 생성·오픈 시점에 현 버전과 비교해
#   낮으면 DM에서 재동의를 진행한다(accounts.needs_terms_reagreement).
import discord

from . import accounts

# 약관 본문. 개정 시 accounts.CURRENT_TERMS_VERSION을 함께 올려야 한다.
TERMS_TEXT = """**INDAIM 이용약관 (v1)**

**1. 서비스 개요**
INDAIM은 AI가 진행을 보조하는 텍스트 롤플레잉 서비스입니다.
플레이 진행에는 생성형 AI 호출 비용이 발생하며, 이용자는 게임머니
'잉크'로 이를 지불합니다.

**2. 잉크와 결제**
· 잉크는 서비스 내에서만 사용되는 게임머니입니다.
· 잉크는 선불로 충전하며, 플레이 진행 시 소모됩니다.
· 잔액이 부족하면 진행이 제한됩니다.
· 충전은 디스코드 결제 시스템을 통해 이루어집니다.

**3. 환불**
· 미사용 잉크의 환불은 디스코드 환불 정책을 따릅니다.
· 이미 소모된 잉크는 환불되지 않습니다.
· 턴 되감기를 사용해도 이미 소모된 잉크는 반환되지 않습니다.
· 세션 오픈 시 선결제한 유지비용 중 미사용분은 세션 종료 시 정산됩니다.

**4. 저장되는 정보**
· 디스코드 사용자 ID
· 잉크 잔액 및 충전·소모 내역
· 플레이 기록 통계(턴 수, 세션 수, 플레이 시간 등)
· 세션 진행 내용(대화 로그, 캐릭터 프로필)
저장된 정보는 서비스 제공 목적으로만 사용됩니다.

**5. 생성물에 관하여**
· AI 생성 결과물의 내용은 사전에 보장되지 않습니다.
· 생성 실패·지연이 발생할 수 있으며, 이 경우 재시도하거나
  해당 턴을 취소합니다.

**6. 이용 제한**
· 서비스를 방해하거나 타 이용자에게 피해를 주는 행위는 제한됩니다.

**7. 약관 변경**
· 약관이 개정되면 다음 세션 생성 시 재동의를 요청합니다.
"""

# 동의 시 지급하는 가입선물(잉크). accounts.SIGNUP_BONUS_INK와 동기화한다.
SIGNUP_GIFT_INK = 30


def build_terms_embed(*, reagree: bool = False) -> discord.Embed:
    """약관 임베드. 재동의인 경우 안내 문구가 달라진다."""
    title = "📜 약관 재동의" if reagree else "📜 INDAIM 계정 등록"
    desc = (
        "약관이 개정되었습니다. 계속 이용하시려면 재동의가 필요합니다."
        if reagree else
        "서비스를 이용하시려면 아래 약관에 동의해 주십시오."
    )
    embed = discord.Embed(title=title, description=desc, color=0x5865F2)
    # 임베드 필드는 1024자 제한이 있으므로 본문을 나눠 담는다.
    chunks = TERMS_TEXT.split("\n\n")
    buf = ""
    idx = 1
    for chunk in chunks:
        if len(buf) + len(chunk) + 2 > 1000:
            embed.add_field(name=f"약관 ({idx})", value=buf, inline=False)
            buf = chunk
            idx += 1
        else:
            buf = f"{buf}\n\n{chunk}" if buf else chunk
    if buf:
        embed.add_field(name=f"약관 ({idx})", value=buf, inline=False)

    if not reagree and SIGNUP_GIFT_INK:
        embed.set_footer(text=f"동의하시면 가입선물 {SIGNUP_GIFT_INK}잉크가 지급됩니다.")
    return embed


class TermsView(discord.ui.View):
    """
    약관 동의 UI — DM 전용.

    NOTE: persistent view로 두지 않는다. DM은 세션 컨텍스트가 없고,
          동의는 한 번 끝내면 되는 절차라 timeout이 적절하다.
          만료되면 GM 홈에서 다시 시작하면 된다.
    """

    def __init__(self, bot, user_id, *, reagree: bool = False):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = str(user_id)
        self.reagree = reagree

    @discord.ui.button(label="✅ 동의합니다", style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "본인만 동의할 수 있습니다.", ephemeral=True)
            return

        first_time = not accounts.is_registered(self.user_id)
        await accounts.register_account(self.user_id)

        lines = []
        if first_time:
            if SIGNUP_GIFT_INK:
                await accounts.add_ink(self.user_id, SIGNUP_GIFT_INK, reason="가입선물")
                lines.append(f"🎁 가입선물 **{SIGNUP_GIFT_INK}잉크**가 지급되었습니다.")
            lines.append(
                "이제 서버의 **GM 스페이스**에서 세션을 열 수 있습니다.\n"
                "잉크가 부족하면 홈의 **잉크 충전** 버튼을 이용하십시오."
            )
        else:
            lines.append("재동의가 완료되었습니다. 계속 이용하실 수 있습니다.")

        bal = accounts.get_balance(self.user_id)
        lines.append(f"현재 잔액: **{bal:,}잉크**")

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="✅ **약관에 동의하셨습니다.**\n\n" + "\n\n".join(lines),
            embed=None, view=self,
        )
        self.stop()

    @discord.ui.button(label="✖ 취소", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "본인만 조작할 수 있습니다.", ephemeral=True)
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="동의를 취소했습니다. 서비스를 이용하시려면 다시 시도해 주십시오.",
            embed=None, view=self,
        )
        self.stop()


async def start_registration(bot, user, *, reagree: bool = False) -> bool:
    """DM으로 약관 동의 절차를 시작한다.

    Returns:
        DM 전송 성공 여부. 실패(DM 차단)면 호출부가 안내해야 한다.
    """
    try:
        await user.send(
            embed=build_terms_embed(reagree=reagree),
            view=TermsView(bot, user.id, reagree=reagree),
        )
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        print(f"[약관] DM 전송 실패: {e}")
        return False


async def ensure_agreed(bot, user) -> tuple:
    """세션 생성·오픈 전 동의 상태를 확인한다.

    Returns:
        (통과 여부, 안내 문구)
    """
    uid = user.id
    if not accounts.is_registered(uid):
        sent = await start_registration(bot, user)
        return False, (
            "계정 등록이 필요합니다. DM을 확인해 주십시오."
            if sent else
            "계정 등록이 필요하지만 DM을 보낼 수 없습니다. "
            "서버 개인정보 설정에서 DM 수신을 허용해 주십시오."
        )
    if accounts.needs_terms_reagreement(uid):
        sent = await start_registration(bot, user, reagree=True)
        return False, (
            "약관이 개정되었습니다. DM에서 재동의를 진행해 주십시오."
            if sent else
            "약관 재동의가 필요하지만 DM을 보낼 수 없습니다. "
            "DM 수신을 허용해 주십시오."
        )
    return True, ""
