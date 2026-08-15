import asyncio
import os
import subprocess
import sys
import discord
import time
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core

# ========== [시스템 관리 모듈(System Cog)] ==========
class SystemCog(commands.Cog):
    """
    봇 명령어 가이드, 채널 정리, 캐시 관리, 무중단 리로드 등
    시스템 및 서버 유지보수와 관련된 기능을 전담하는 모듈.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="명령어")
    async def show_commands(self, ctx):
        """
        마스터 채널에서 사용 가능한 전체 명령어와 인자, 특수 태그 목록을 Embed 형태로 출력.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        embed = discord.Embed(
            title="📜 TRPG 봇 명령어 가이드",
            description=(
                "마스터 채널 전용입니다. 인자 표기 규약:\n"
                "> `[필수]` · `(선택)` · `A/B` 중 택1\n"
                "> 태그·`!증감` 값에 띄어쓰기가 필요하면 **언더바(_)**로: `태:유이설;내력_고갈`"
            ),
            color=0x9b59b6,
        )

        embed.add_field(name="🎬  세션 관리", value=(
            "`!새세션 [시나리오명]` — 게임·마스터 채널 생성 + 룰북 캐시 업로드\n"
            "`!시작` — 게임 채널 정리 후 시작 메시지 스트리밍 (1회 한정)\n"
            "`!소개` — 인트로·캐릭터 생성 안내를 게임 채널에 스트리밍\n"
            "`!세션종료` — 게임 채널 잠금 · 캐시 파기 · 과금 정산"
        ), inline=False)

        embed.add_field(name="🧙  캐릭터 설정", value=(
            "`!참가 [이름]` — PC로 참가 (마스터 채널에 캐릭터 생성 매니저 버튼 게시)\n"
            "`!캐릭터가져오기 [원본세션ID] [원본이름] (대상이름)` — 타 세션 PC 이식\n"
            "`!설정 [이름] [항목] [내용]` — 스탯·프로필 항목 갱신\n"
            "`!증감 [이름] [스탯] [±수치]` — 스탯 수치 증감 (예: `+5`, `-3`)\n"
            "`!증감 [이름] 자원 [아이템] [±수치]` — 소지 자원 증감\n"
            "`!증감 [이름] 상태 [상태명]` — 상태 부여 / `-상태명` = 제거\n"
            "`!외형 [이름] (내용)` — 외형 설정 또는 확인\n"
            "`!프로필 [이름] (게임)` — 프로필 카드 (기본 마스터 / `게임` = 게임 채널)\n"
            "`!능력치 [이름] [주사위눈] [총합]` — 능력치 굴림 → 비율 배분·상한 적용\n"
            "`!설정생성 [pc/npc] [이름] [지시]` — AI 설정 초안 (`엔:이름` = 참조 NPC)"
        ), inline=False)

        embed.add_field(name="👥  NPC 관리", value=(
            "`!엔피씨 설정 [이름] [내용]` — NPC 전체 설정 덮어쓰기\n"
            "`!엔피씨 설정 [이름] [필드명] [내용]` — 단일 필드 수정 (나이·성별 등)\n"
            "`!엔피씨 [확인/삭제/목록] (이름)` — NPC 조회·삭제·목록"
        ), inline=False)

        embed.add_field(name="🎲  게임 진행·판정", value=(
            "`!진행 [지시사항]` — AI 턴 묘사 생성·스트리밍 연출\n"
            "　└ 태그: `상/중/하:키워드`(이미지) · `자:이름;아이템;수치`(자원) · `태:이름;[-]상태`(상태)\n"
            "`!재생성 (지시사항)` — 직전 턴 롤백 후 재생성\n"
            "`!출력물` — 직전 턴 AI 텍스트를 마스터 채널에 전송\n"
            "`!수정 [텍스트 전체]` — 직전 턴 게임 채널 출력물 편집\n"
            "`!주사위 [이름] [눈] (가중치) (목표값)` — 일반 / 목표값 판정\n"
            "`!주사위 [이름] [스탯] [눈] (가중치)` — 능력치 기반 판정\n"
            "`!기억압축` — 미압축 로그 수동 요약\n"
            "`!노트 [누적/갱신/출력] (내용)` — 매 턴 주입되는 실시간 GM 노트\n"
            "`!캐시노트 [누적/갱신/출력] (내용)` — 차기 캐시에 병합될 내용"
        ), inline=False)

        embed.add_field(name="🎵  미디어·채널", value=(
            "`!이미지 [키워드]` · `!이미지 목록` — 이미지 출력 · 키워드 목록\n"
            "`!이미지 생성 [형식키] [키워드] [프롬프트] (레:레퍼런스키)` — AI 이미지 생성·등록\n"
            "`!브금 [파일명]` · `!브금 [목록/정지]` — BGM 무한 반복 · 목록·정지\n"
            "`!플리 시작 [시나리오명]` — mp3 셔플 플레이리스트 시작\n"
            "`!플리 [재생/일시정지/다음/이전/종료]` — 플레이리스트 제어\n"
            "`!볼륨 [0.0~2.0]` — BGM·플리 볼륨 (기본 `0.3`)\n"
            "`!채팅 [잠금/해제]` — 게임 채널 플레이어 채팅 통제\n"
            "`!더빙 [켜기/끄기]` — AI 묘사 TTS 더빙 토글 (실험 · `!진행` 한정)\n"
            "`!더빙테스트 (보이스)` — 직전 묘사 마지막 문단 TTS 재생"
        ), inline=False)

        embed.add_field(name="🤖  자동 GM  (`!자동` 그룹)", value=(
            "`!자동` — 하위명령 목록\n"
            "`!자동 시작 (대상PC)` — 게임 채널 발언을 AI GM이 자동 처리\n"
            "`!자동 중단` · `!자동 상태` — 정지 · 활성/처리 턴/누적 비용 확인\n"
            "`!자동 개입 [텍스트]` — 다음 PROCEED까지 마스터 사이드 노트 유지\n"
            "`!자동 턴제한 [N|해제]` · `!자동 비용제한 [원|해제]` — 안전장치 (기본 무제한)\n"
            "`!자동 서사` · `!자동 재계획 [메모]` — 서사 계획 조회 · 강제 재수립\n"
            "`!자동 원장` — 정보 인지 원장(비공개 정보별 인지 주체) 조회\n"
            "　└ 판단: `ASK` 질문 · `NARRATE` 즉답 · `ROLL` 판정 · `PROCEED` 턴 진행"
        ), inline=False)

        embed.add_field(name="⚙️  시스템 관리", value=(
            "`!캐시 재발급` — 장기 기억 캐시 강제 재발급\n"
            "`!캐시 삭제` — 캐시 파기 · 보관 비용 정산\n"
            "`!캐시 출력` — 캐시 룰북 원본 텍스트 디버그 출력\n"
            "`!채널정리` — 더미 채널·카테고리 일괄 삭제 (채널 관리 권한)\n"
            "`!리로드 [모듈명]` — cogs 무중단 핫스왑\n"
            "`!배포 [리로드/재시작]` — GitHub 최신 코드 반영 (오너 전용)\n"
            "　└ 대상: `game` `character` `media` `session` `system` `auto_gm`"
        ), inline=False)

        embed.add_field(name="📖  설정 조회·권한", value=(
            "`!권한부여 @유저` · `!권한회수 @유저` — (오너) 마스터 명령 권한 부여·회수\n"
            "`!권한목록` — (오너) 오너·허가 계정 목록\n"
            "　└ 비허가 유저는 `!참가`만 사용 가능"
        ), inline=False)

        embed.set_footer(text="자세한 사용법은 각 명령어를 인자 없이 실행하면 안내됩니다.")

        await ctx.send(embed=embed)


    @commands.command(name="채널정리")
    @commands.has_permissions(manage_channels=True)
    async def cleanup_channels(self, ctx):
        """
        서버 내에 생성된 더미 TRPG 채널 및 카테고리를 UI를 통해 일괄 삭제.

        NOTE: 디스코드 서버 채널 개수 한계 도달 및 파이썬 객체 메모리 누수(Memory Leak)를
        방지하기 위한 가비지 컬렉션(Garbage Collection)의 진입점.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        target_items = {}

        for category in ctx.guild.categories:
            if "TRPG" in category.name or "세션" in category.name:
                target_items[category.id] = category

        # NOTE: 고아 채널 필터링: 카테고리 없이 생성된 봇 관련 텍스트 채널을 수집하여 가비지 컬렉션 대상에 포함.
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
        """
        채널정리 명령어 실행 시 발생하는 권한 예외 처리.
        """
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("⚠️ 이 명령어를 사용하려면 '채널 관리' 권한이 필요합니다.")


    @commands.command(name="세션종료")
    async def end_session(self, ctx):
        """
        게임 채널 잠금 및 캐시 명시적 파기를 원자적으로 수행하고 최종 과금액 정산 및 보고.

        NOTE: 불필요한 스토리지 과금을 차단하기 위한 필수 안전장치 명령어.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        await ctx.send("⏳ 세션 종료 시퀀스를 시작합니다...")

        # 1. 게임 채널 잠금
        try:
            await game_channel.set_permissions(ctx.guild.default_role, send_messages=False)
            await game_channel.send("🔒 **세션이 종료되어 채널이 잠겼습니다.**")
        except Exception as e:
            await ctx.send(f"⚠️ 게임 채널 잠금 실패: {e}")

        # 2. 캐시 파기 및 보관 시간 정산
        storage_cost = 0.0
        if session.cache_name:
            try:
                await asyncio.to_thread(self.bot.genai_client.caches.delete, name=session.cache_name)
                storage_cost = await core.process_cache_deletion(self.bot, session)
            except Exception as e:
                # WARNING: API 상에서 이미 파기된 상태라도 시간 계산 및 정산 로직이 정상 구동되도록 Fallback 처리.
                await ctx.send(f"⚠️ API 서버 측 캐시 삭제 실패 (이미 만료되었을 수 있습니다): {e}")
                storage_cost = await core.process_cache_deletion(self.bot, session)

            if storage_cost > 0:
                core.write_cost_log(session.session_id, "세션 종료 (캐시 유지비 정산)", 0, 0, 0, storage_cost, session.total_cost)

        await core.save_session_data(self.bot, session)

        # 3. 마스터 채널에 결산 보고
        embed = discord.Embed(title="🛑 세션 완전 종료 및 정산 완료", color=0xe74c3c)
        embed.add_field(name="채널 상태", value="게임 채널 채팅 잠금 완료", inline=False)
        embed.add_field(name="캐시 상태", value="장기 기억 캐시 명시적 파기 완료", inline=False)
        embed.add_field(name="정산 내역",
                        value=f"- 최종 캐시 보관 비용: **{core.format_cost(storage_cost)}**\n- 이번 세션 총 누적 비용: **{core.format_cost(session.total_cost)}**",
                        inline=False)

        await ctx.send(embed=embed)


    @commands.command(name="캐시")
    async def manage_cache(self, ctx, action: str = None):
        """
        장기 기억 캐시를 강제로 재발급하거나 명시적으로 삭제하여 과금 관리.

        NOTE: 캐시 서버는 유지 시간(TTL) 동안 지속적으로 스토리지 비용이 발생하므로,
        세션이 장기 휴식에 들어갈 때 명시적으로 삭제하여 비용 누수를 차단하는 재무적 통제 장치.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            action (str): 수행할 작업 ('재발급' 또는 '삭제')
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if action == "재발급":
            await ctx.send("⏳ 수동 캐시 재발급을 시작합니다...")

            # 파기 및 정산
            storage_cost = 0.0
            if session.cache_name:
                try:
                    await asyncio.to_thread(self.bot.genai_client.caches.delete, name=session.cache_name)
                except Exception as e:
                    pass
                storage_cost = await core.process_cache_deletion(self.bot, session)
                if storage_cost > 0:
                    core.write_cost_log(session.session_id, "수동 캐시 파기 (유지비 정산)", 0, 0, 0, storage_cost,
                                        session.total_cost)

            try:
                caching_text, cache_tokens, base_text = await core.build_scenario_cache_text(
                    self.bot, core.DEFAULT_MODEL, session.scenario_data,
                    getattr(session, "cache_note", ""), session=session
                )

                upload_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
                session.total_cost += upload_cost
                core.write_cost_log(session.session_id, "수동 캐시 재발급 (업로드)", cache_tokens, 0, 0, upload_cost,
                                    session.total_cost)
                session.cache_created_at = time.time()
                session.cache_tokens = cache_tokens

                print(f"[수동 캐시 재발급] storage={core.format_cost(storage_cost)} upload={core.format_cost(upload_cost)} total={core.format_cost(session.total_cost)}")
                _cache_embed = core.build_cache_cost_embed(
                    "수동 캐시 재발급", storage_cost, upload_cost, session.total_cost
                )
                await ctx.send(embed=_cache_embed)

                cache = await asyncio.to_thread(
                    self.bot.genai_client.caches.create,
                    model=core.DEFAULT_MODEL,
                    config=types.CreateCachedContentConfig(
                        system_instruction=self.bot.system_instruction,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                        ttl="21600s"  # 6시간
                    )
                )

                session.cache_obj = cache
                session.cache_name = cache.name
                session.cache_model = core.DEFAULT_MODEL
                session.cache_text = base_text
                core.update_session_cache_state(session)
                await core.save_session_data(self.bot, session)

                await ctx.send(f"✅ 수동 캐시 재발급 완료! (새 캐시 ID: {cache.name})\n누적 비용에 캐시 생성 및 1시간 유지 비용이 합산되었습니다.")

            except Exception as e:
                await ctx.send(f"⚠️ 캐시 재발급 중 오류가 발생했습니다: {e}")

        elif action == "삭제":
            if not session.cache_name:
                return await ctx.send("⚠️ 현재 유지 중인 캐시가 없습니다.")

            await ctx.send("⏳ 기존 캐시를 명시적으로 삭제하고 보관 비용을 정산합니다...")
            try:
                await asyncio.to_thread(self.bot.genai_client.caches.delete, name=session.cache_name)
                storage_cost = await core.process_cache_deletion(self.bot, session)
                if storage_cost > 0:
                    core.write_cost_log(session.session_id, "명시적 캐시 삭제 (유지비 정산)", 0, 0, 0, storage_cost,
                                        session.total_cost)

                print(f"[수동 캐시 파기] storage={core.format_cost(storage_cost)} total={core.format_cost(session.total_cost)}")
                _cache_embed = core.build_cache_cost_embed(
                    "수동 캐시 파기", storage_cost, 0.0, session.total_cost
                )
                await ctx.send(embed=_cache_embed)

                await ctx.send("✅ 캐시가 정상적으로 삭제되어 스토리지 과금이 중단되었습니다.")
            except Exception as e:
                storage_cost = await core.process_cache_deletion(self.bot, session)
                await ctx.send(
                    f"⚠️ 캐시 삭제 중 오류 발생 (이미 만료됨): {e}\n내부 메타데이터가 초기화되었습니다. 보관 비용 정산: {core.format_cost(storage_cost)}")

        elif action == "출력":
            cache_text = getattr(session, "cache_text", "")
            if not cache_text:
                return await ctx.send(
                    "⚠️ 저장된 캐시 텍스트가 없습니다.\n"
                    "(세션 복구 후 캐시가 재발급되지 않았거나, 이전 버전에서 생성된 세션일 수 있습니다. "
                    "`!캐시 재발급`으로 갱신하면 이후 출력 가능합니다.)"
                )

            cache_name = session.cache_name or "(캐시 없음)"
            total_chars = len(cache_text)
            cache_tokens = getattr(session, "cache_tokens", 0)
            await ctx.send(
                f"📦 **[캐시 룰북 출력]** (캐시 ID: `{cache_name}`)\n"
                f"패딩 제외 원본 텍스트 — 총 {total_chars:,}자 / 약 {cache_tokens:,} 토큰\n"
                f"(1950자 단위로 분할 전송합니다)"
            )

            chunk_size = 1950
            for i in range(0, len(cache_text), chunk_size):
                await ctx.send(cache_text[i:i + chunk_size])

        else:
            await ctx.send("⚠️ 잘못된 인자입니다. 사용법: `!캐시 [재발급/삭제/출력]`")


    @commands.command(name="리로드")
    @commands.has_permissions(administrator=True)
    async def reload_cog(self, ctx, cog_name: str):
        """
        수정된 Cog(모듈) 파일을 무중단으로 다시 불러옴 (관리자 전용).

        NOTE: 봇 프로세스 전체의 재시작 없이 특정 모듈의 코드 변경 사항만을
        런타임에 핫스왑(Hot-swap)하여 게임 흐름의 단절 방지.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            cog_name (str): 다시 불러올 확장 모듈 이름 (예: game, system)
        """
        try:
            await self.bot.reload_extension(f"cogs.{cog_name}")
            await ctx.send(f"✅ `cogs.{cog_name}` 모듈을 성공적으로 리로드했습니다. 변경 사항이 즉시 적용됩니다.")
        except Exception as e:
            await ctx.send(f"⚠️ 모듈 리로드 중 오류 발생: {e}")


    @commands.command(name="배포")
    @commands.is_owner()
    async def deploy(self, ctx, mode: str = None):
        """
        GitHub 원격 저장소의 최신 코드를 받아 봇에 반영 (오너 전용).

        NOTE: SSH 접속 없이 디스코드만으로 배포를 완결하기 위한 명령.
              서버 접속 수단을 잃어도 코드 갱신이 막히지 않도록 하는 안전장치다.

        사용법:
            !배포          — git pull만 수행 (변경 내역 확인용)
            !배포 리로드    — pull 후 모든 cogs를 무중단 핫스왑
            !배포 재시작    — pull 후 봇 프로세스 재시작
                             (core/·main.py·prompts.py 변경 시 필수)

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            mode (str): 배포 후 반영 방식 (None / "리로드" / "재시작")
        """
        if mode is not None and mode not in ("리로드", "재시작"):
            await ctx.send("⚠️ 사용법: `!배포` / `!배포 리로드` / `!배포 재시작`")
            return

        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        msg = await ctx.send("⏳ 원격 저장소에서 코드를 받는 중…")

        def _run(args, timeout=120):
            """git 명령을 실행하고 (성공여부, 출력)을 반환. 대화형 프롬프트는 차단."""
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            try:
                proc = subprocess.run(
                    args, cwd=repo_dir, env=env, timeout=timeout,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                return proc.returncode == 0, (proc.stdout or "").strip()
            except subprocess.TimeoutExpired:
                return False, f"시간 초과({timeout}초) — 명령이 응답하지 않았습니다."
            except Exception as e:
                return False, f"실행 실패: {e}"

        # ── 로컬 변경 확인 ──
        # 추적 중인 파일이 수정된 경우에만 차단한다. pull은 그런 변경을 덮어쓸 수
        # 없어 충돌하기 때문. 미추적 파일(status 접두 "??" — venv/, __pycache__ 등)은
        # 병합에 관여하지 않으므로 통과시킨다.
        ok, dirty = _run(["git", "status", "--porcelain"])
        if ok and dirty:
            blocking = [ln for ln in dirty.splitlines() if not ln.startswith("??")]
            if blocking:
                await msg.edit(content=(
                    "⚠️ 서버에 커밋되지 않은 로컬 변경이 있어 중단했습니다.\n"
                    f"```{chr(10).join(blocking)[:1500]}```"
                    "수동으로 정리한 뒤 다시 시도하십시오."
                ))
                return

        before_ok, before = _run(["git", "rev-parse", "--short", "HEAD"])

        ok, out = _run(["git", "pull", "--no-rebase"])
        if not ok:
            await msg.edit(content=f"❌ `git pull` 실패\n```{out[:1800]}```")
            return

        after_ok, after = _run(["git", "rev-parse", "--short", "HEAD"])

        if before_ok and after_ok and before == after:
            await msg.edit(content=f"✅ 이미 최신 상태입니다. (`{after}`)")
            return

        # 변경된 파일 목록 — 재시작이 필요한지 판단 근거로 제공
        _, changed = _run(["git", "diff", "--name-only", f"{before}..{after}"])
        needs_restart = any(
            f.startswith(("core/", "prompts.py", "main.py"))
            for f in changed.splitlines()
        )

        report = [f"✅ `{before}` → `{after}` 갱신 완료"]
        if changed:
            report.append(f"```{changed[:1200]}```")
        if needs_restart and mode != "재시작":
            report.append("⚠️ `core/`·`main.py`·`prompts.py` 변경이 포함되어 **재시작이 필요**합니다. `!배포 재시작`")

        # ── 반영 ──
        if mode == "리로드":
            results = []
            for filename in sorted(os.listdir(os.path.join(repo_dir, "cogs"))):
                if not filename.endswith(".py") or filename.startswith("__"):
                    continue
                name = filename[:-3]
                try:
                    await self.bot.reload_extension(f"cogs.{name}")
                    results.append(f"✅ {name}")
                except Exception as e:
                    results.append(f"❌ {name}: {e}")
            report.append("**핫스왑 결과**\n" + "\n".join(results))

        elif mode == "재시작":
            report.append("♻️ 3초 후 프로세스를 재시작합니다…")

        await msg.edit(content="\n".join(report)[:1900])

        if mode == "재시작":
            await asyncio.sleep(3)
            # NOTE: systemd가 Restart=always로 관리하므로 프로세스 종료만으로 재기동된다.
            #       execv는 systemd가 없는 환경을 위한 대비책.
            try:
                await self.bot.close()
            finally:
                os.execv(sys.executable, [sys.executable] + sys.argv)



async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(SystemCog(bot))