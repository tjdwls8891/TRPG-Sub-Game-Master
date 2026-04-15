import asyncio
import discord
from discord.ext import commands
from google.genai import types

# 분리된 코어 유틸리티 모듈을 임포트합니다.
import core


class SystemCog(commands.Cog):
    """
    봇 명령어 가이드, 채널 정리, 캐시 관리, 무중단 리로드 등
    시스템 및 서버 유지보수와 관련된 기능을 전담하는 모듈입니다.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="명령어")
    async def show_commands(self, ctx):
        """
        마스터 채널에서 사용 가능한 전체 명령어와 인자, 특수 태그 목록을 Embed 형태로 출력합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        embed = discord.Embed(title="📜 TRPG 봇 명령어 및 인자 가이드", color=0x9b59b6)

        embed.add_field(name="[세션 관리]", value=(
            "`!새세션 [시나리오명]` : 새로운 게임 세션 준비\n"
            "`!시작` : 세션 시작 (1회 제한)\n"
            "`!소개` : 인트로 및 캐릭터 생성 안내 출력"
        ), inline=False)

        embed.add_field(name="[캐릭터 및 NPC 설정]", value=(
            "`!참가 [이름]` : 플레이어 캐릭터로 세션 참가 (게임 채널)\n"
            "`!설정 [이름] [항목] [내용]` : 캐릭터 스탯/프로필 설정\n"
            "`!외형 [이름] (내용)` : 캐릭터 외형 설정 및 확인\n"
            "`!프로필 [이름]` : 캐릭터 전체 프로필 확인\n"
            "`!엔피씨 [설정/확인/삭제/목록] (이름) (내용)` : NPC 정보 통합 관리\n"
            "`!설정생성 [pc/npc] [이름] [지시사항]` : AI 설정 초안 생성"
        ), inline=False)

        embed.add_field(name="[게임 진행 및 판정]", value=(
            "`!진행 [지시사항]` : AI 턴 묘사 진행\n"
            "  *(특수 태그: `상/중/하:키워드`, `자:이름;아이템;수치`, `태:이름;[-]상태`)*\n"
            "`!주사위 [이름] [눈] (가중치) (목표값)` : 일반 주사위 / 목표값 판정\n"
            "`!주사위 [이름] [스탯명] [눈] (가중치)` : 능력치 주사위 굴림\n"
            "`!기억압축` : 미압축 로그 수동 요약 및 압축"
        ), inline=False)

        embed.add_field(name="[미디어 및 채널 제어]", value=(
            "`!이미지 [키워드/목록]` : 로컬 이미지 출력 및 확인\n"
            "`!브금 [파일명/목록/정지]` : BGM 재생, 정지 및 확인\n"
            "`!플리 [행동] (시나리오명)` : 플리 제어 (시작/종료/다음/이전/일시정지/재생)\n"
            "`!채팅 [잠금/해제]` : 일반 플레이어 채팅 통제"
        ), inline=False)

        embed.add_field(name="[시스템 관리]", value=(
            "`!채널정리` : 불필요한 더미 세션 카테고리/채널 일괄 삭제\n"
            "`!캐시 [재발급/삭제]` : 장기 기억 캐시 재발급 및 파기\n"
            "`!리로드 [모듈명]` : 시스템 무중단 모듈 업데이트 (관리자용)"
        ), inline=False)

        await ctx.send(embed=embed)


    @commands.command(name="채널정리")
    @commands.has_permissions(manage_channels=True)
    async def cleanup_channels(self, ctx):
        """
        서버 내에 생성된 더미 TRPG 채널 및 카테고리를 UI를 통해 일괄 삭제합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        target_items = {}

        for category in ctx.guild.categories:
            if "TRPG" in category.name or "세션" in category.name:
                target_items[category.id] = category

        # 고아 채널 필터링: 카테고리 없이 생성된 봇 관련 텍스트 채널을 수집합니다.
        for channel in ctx.guild.text_channels:
            if channel.category is None and ("game-" in channel.name or "master-" in channel.name):
                target_items[channel.id] = channel

        if not target_items:
            return await ctx.send("⚠️ 삭제 후보로 필터링된 TRPG 관련 채널이나 카테고리가 없습니다.")

        view = core.ChannelDeleteView(self.bot, ctx, target_items)
        await ctx.send(
            "🗑️ **[채널 정리 모드]** 아래 드롭다운에서 삭제할 카테고리나 채널을 모두 선택한 뒤 [영구 삭제] 버튼을 누르십시오.\n*(주의: 카테고리 선택 시 하위 채널도 함께 삭제됩니다.)*",
            view=view)

    @cleanup_channels.error
    async def cleanup_channels_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⚠️ 이 명령어를 사용하려면 '채널 관리' 권한이 필요합니다.")


    @commands.command(name="캐시")
    async def manage_cache(self, ctx, action: str = None):
        """
        장기 기억 캐시를 강제로 재발급하거나 명시적으로 삭제하여 과금을 관리합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            action (str): 수행할 작업 ('재발급' 또는 '삭제')
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if action == "재발급":
            await ctx.send("⏳ 수동 캐시 재발급을 시작합니다. 기존 캐시를 삭제하고 새로 생성 중...")

            if session.cache_name:
                try:
                    await asyncio.to_thread(self.bot.genai_client.caches.delete, name=session.cache_name)
                    print(f"🗑️ [캐시 관리] {session.session_id}: 기존 캐시({session.cache_name}) 명시적 삭제 완료.")
                except Exception as e:
                    print(f"⚠️ [캐시 관리] {session.session_id}: 기존 캐시 삭제 실패 (이미 만료되었거나 존재하지 않음) - {e}")

            try:
                caching_text, cache_tokens = await core.build_scenario_cache_text(self.bot, core.DEFAULT_MODEL, session.scenario_data)

                creation_cost = core.calculate_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
                storage_cost = core.calculate_cost(core.DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
                session.total_cost += (creation_cost + storage_cost)

                print(
                    f"💰 [비용 보고] 세션({session.session_id}) 수동 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

                cache = await asyncio.to_thread(
                    self.bot.genai_client.caches.create,
                    model=core.DEFAULT_MODEL,
                    config=types.CreateCachedContentConfig(
                        system_instruction=self.bot.system_instruction,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                        ttl="3600s"
                    )
                )

                session.cache_obj = cache
                session.cache_name = cache.name
                session.cache_model = core.DEFAULT_MODEL
                await core.save_session_data(self.bot, session)

                await ctx.send(f"✅ 수동 캐시 재발급 완료! (새 캐시 ID: {cache.name})\n누적 비용에 캐시 생성 및 1시간 유지 비용이 합산되었습니다.")

            except Exception as e:
                await ctx.send(f"⚠️ 캐시 재발급 중 오류가 발생했습니다: {e}")

        elif action == "삭제":
            if not session.cache_name:
                return await ctx.send("⚠️ 현재 유지 중인 캐시가 없습니다.")

            await ctx.send("⏳ 구글 서버에서 기존 캐시를 명시적으로 삭제하는 중입니다...")

            try:
                await asyncio.to_thread(self.bot.genai_client.caches.delete, name=session.cache_name)
                print(f"🗑️ [캐시 관리] {session.session_id}: 수동 캐시({session.cache_name}) 삭제 완료.")

                session.cache_name = None
                session.cache_obj = None
                session.cache_model = None
                await core.save_session_data(self.bot, session)

                await ctx.send(
                    "✅ 캐시가 정상적으로 삭제되어 스토리지 과금이 중단되었습니다.\n(참고: 이후 캐시 없이 `!진행` 시 매번 전체 로그를 읽게 되어 요금이 치솟을 수 있습니다. 게임 재개 시 반드시 `!캐시 재발급`을 먼저 실행해 주십시오.)")

            except Exception as e:
                print(f"⚠️ [캐시 관리] {session.session_id}: 캐시 삭제 실패 - {e}")

                session.cache_name = None
                session.cache_obj = None
                session.cache_model = None
                await core.save_session_data(self.bot, session)

                await ctx.send(f"⚠️ 캐시 삭제 중 오류가 발생했습니다 (이미 만료되어 사라졌을 확률이 높습니다): {e}\n✅ 시스템 상의 캐시 연결은 안전하게 해제되었습니다.")

        else:
            await ctx.send("⚠️ 잘못된 인자입니다. 사용법: `!캐시 [재발급/삭제]`")

    @commands.command(name="리로드")
    @commands.has_permissions(administrator=True)
    async def reload_cog(self, ctx, cog_name: str):
        """
        수정된 Cog(모듈) 파일을 무중단으로 다시 불러옵니다 (관리자 전용).

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            cog_name (str): 다시 불러올 확장 모듈 이름 (예: game, system)
        """
        try:
            await self.bot.reload_extension(f"cogs.{cog_name}")
            await ctx.send(f"✅ `cogs.{cog_name}` 모듈을 성공적으로 리로드했습니다. 변경 사항이 즉시 적용됩니다.")
        except Exception as e:
            await ctx.send(f"⚠️ 모듈 리로드 중 오류 발생: {e}")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수입니다.
    """
    await bot.add_cog(SystemCog(bot))