# 명령어 권한 (F1) — 오너(앱 소유자)와 허가받은 계정만 명령을 사용.
#
# 정책:
#   - 비허가 유저가 사용할 수 있는 명령은 EXEMPT_COMMANDS(=!참가) 뿐이다.
#   - 그 외 모든 명령은 오너 또는 오너가 부여한 허가 계정만 사용할 수 있다.
#   - 권한 관리 명령(!권한부여/!권한회수/!권한목록)은 오너 전용(@commands.is_owner).
#   - 오너는 앱 소유자로 자동 식별되며(main.py on_ready), 허가 목록에도 owner_id로 기록된다.
import discord
from discord.ext import commands

import core

# 비허가 유저도 사용할 수 있는 명령 (게임 채널의 플레이어 참가 전용)
EXEMPT_COMMANDS = {"참가"}


class PermissionCog(commands.Cog):
    """명령어 사용 권한을 오너 계정에 귀속시키고, 오너가 허가한 계정에 마스터 명령을 개방한다."""

    def __init__(self, bot):
        self.bot = bot
        # 전역 체크 등록 — 모든 명령 실행 전에 호출된다. (핫스왑 시 cog_unload에서 해제)
        bot.add_check(self.global_permission_check)

    def cog_unload(self):
        self.bot.remove_check(self.global_permission_check)

    async def _is_authorized(self, user) -> bool:
        au = self.bot.authorized_users
        if user.id == au.get("owner_id") or user.id in au.get("granted", []):
            return True
        try:
            if await self.bot.is_owner(user):
                return True
        except Exception:
            pass
        return False

    async def global_permission_check(self, ctx) -> bool:
        """비허가 계정은 !참가 외 모든 명령을 차단한다."""
        if ctx.command is None:
            return True
        if ctx.command.qualified_name in EXEMPT_COMMANDS:
            return True
        if await self._is_authorized(ctx.author):
            return True
        # 차단: 자체 안내 후 플래그를 세워 errors.py의 중복 메시지를 억제
        try:
            await ctx.send("⛔ 허가된 계정만 사용할 수 있는 명령어입니다. (진행자에게 `!권한부여`를 요청하세요.)")
        except Exception:
            pass
        ctx._perm_denied = True
        return False

    @commands.command(name="권한부여")
    @commands.is_owner()
    async def grant(self, ctx, member: discord.Member):
        """오너 전용 — 멘션한 계정에 마스터 명령 권한을 부여한다. 사용법: !권한부여 @유저"""
        au = self.bot.authorized_users
        if member.id == au.get("owner_id"):
            return await ctx.send(f"ℹ️ {member.mention}은(는) 오너로, 이미 모든 권한을 가집니다.")
        if member.id in au.get("granted", []):
            return await ctx.send(f"ℹ️ {member.mention}은(는) 이미 허가된 계정입니다.")
        au.setdefault("granted", []).append(member.id)
        core.save_authorized_users(au)
        await ctx.send(f"✅ {member.mention}에게 마스터 명령 권한을 부여했습니다.")

    @commands.command(name="권한회수")
    @commands.is_owner()
    async def revoke(self, ctx, member: discord.Member):
        """오너 전용 — 멘션한 계정의 권한을 회수한다. 사용법: !권한회수 @유저"""
        au = self.bot.authorized_users
        if member.id not in au.get("granted", []):
            return await ctx.send(f"ℹ️ {member.mention}은(는) 허가 목록에 없습니다.")
        au["granted"] = [x for x in au.get("granted", []) if x != member.id]
        core.save_authorized_users(au)
        await ctx.send(f"🗑️ {member.mention}의 마스터 명령 권한을 회수했습니다.")

    @commands.command(name="권한목록")
    @commands.is_owner()
    async def list_perms(self, ctx):
        """오너 전용 — 오너와 허가 계정 목록을 출력한다."""
        au = self.bot.authorized_users
        owner_id = au.get("owner_id")
        granted = au.get("granted", [])
        owner_line = f"<@{owner_id}>" if owner_id else "(미식별)"
        if granted:
            granted_line = "\n".join(f"- <@{uid}>" for uid in granted)
        else:
            granted_line = "(없음)"
        await ctx.send(
            f"👑 **오너**: {owner_line}\n"
            f"🔑 **허가 계정** ({len(granted)}명):\n{granted_line}"
        )


async def setup(bot):
    await bot.add_cog(PermissionCog(bot))
