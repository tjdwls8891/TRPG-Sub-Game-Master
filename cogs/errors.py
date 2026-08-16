# -*- coding: utf-8 -*-
"""
명령어 오입력(오타·인자 부족·타입 오류 등) 전역 처리 cog.

- CommandNotFound : 세션 채널에서만 반응. 자모 분해 근접 매칭으로 오타 제안, 없으면 안내.
- MissingRequiredArgument / TooManyArguments / BadArgument : 한글 사용법 + 예시 안내.
- CheckFailure : 채널/권한 안내.
- CommandInvokeError : 사용자에겐 요약, 상세 traceback은 로그 기록.
"""
import traceback
import discord
from discord.ext import commands

import core

# ── 명령어별 한글 사용법·예시 (전량 큐레이션). 키 = command.qualified_name ──
# (usage, example) — example이 빈 문자열이면 생략.
USAGE = {
    # 세션
    "새세션": ("!새세션 (시나리오명)", "!새세션 무협"),
    "시작": ("!시작", ""),
    "소개": ("!소개", ""),
    "세션종료": ("!세션종료", ""),
    "채널정리": ("!채널정리", ""),
    "명령어": ("!명령어", ""),
    # 캐릭터
    "참가": ("!참가 [캐릭터이름]", "!참가 유이설"),
    "캐릭터가져오기": ("!캐릭터가져오기 [원본세션ID] [원본캐릭터이름] (대상이름)", "!캐릭터가져오기 ee378d6b 유이설"),
    "설정": ("!설정 [캐릭터] [항목] [값]", "!설정 유이설 무공 15"),
    "증감": ("!증감 [캐릭터] [항목|자원|상태] [값]", "!증감 유이설 무공 2"),
    "외형": ("!외형 [캐릭터] [외형 묘사]", "!외형 유이설 21세 여성, 흰 피부에 큰 눈…"),
    "프로필": ("!프로필 [캐릭터] (게임)", "!프로필 유이설"),
    "엔피씨": ("!엔피씨 [목록|설정|삭제 등] (이름) (내용)", "!엔피씨 설정 화산제자 소속·신분 화산파"),
    "능력치": ("!능력치 [캐릭터] [주사위눈] [목표총합]", "!능력치 유이설 20 60"),
    "설정생성": ("!설정생성 [pc|npc] [이름] [지시사항]", "!설정생성 npc 객잔주인 인상 좋은 중년"),
    # 진행/판정
    "주사위": ("!주사위 [캐릭터] [항목] (최대눈) (수정치)", "!주사위 유이설 무공  /  !주사위 유이설 무공 20 3"),
    "진행": ("!진행 (지시사항)", "!진행 상:객잔 유이설이 문을 열고 들어선다"),
    "재생성": ("!재생성 (지시사항)", "!재생성 좀 더 긴장감 있게"),
    "출력물": ("!출력물", ""),
    "수정": ("!수정 [새 텍스트]", "!수정 (편집한 묘사 전문)"),
    "기억압축": ("!기억압축", ""),
    "노트": ("!노트 [누적|갱신|출력] (내용)", "!노트 누적 유이설은 화산의 제자"),
    "캐시노트": ("!캐시노트 [누적|갱신|출력] (내용)", "!캐시노트 누적 무림맹이 소집됨"),
    # 미디어
    "이미지": ("!이미지 생성 [형식키] [키워드(파일명)] [프롬프트] (레:레퍼런스키)", "!이미지 생성 인물 유이설 a young swordswoman…"),
    "브금": ("!브금 [파일명]", "!브금 긴장감"),
    "플리": ("!플리 [시작|중지|스킵|일시정지|재생 등] (시나리오명)", "!플리 시작"),
    "볼륨": ("!볼륨 [0.0~1.0]", "!볼륨 0.3"),
    "채팅": ("!채팅 [잠금|해제]", "!채팅 잠금"),
    "더빙": ("!더빙 (켜기|끄기)", "!더빙 켜기"),
    "더빙테스트": ("!더빙테스트 (보이스)", "!더빙테스트 Gacrux"),
    # 시스템
    "캐시": ("!캐시 (재발급|상태)", "!캐시 재발급"),
    "리로드": ("!리로드 [모듈명]", "!리로드 game"),
    "배포": ("!배포 [리로드/재시작]", "!배포 재시작"),
    # GM 그룹
    "자동": ("!자동 [시작|중단|상태|개입|턴제한|비용제한|서사|재계획] …", "!자동 시작"),
    "자동 시작": ("!자동 시작 (대상PC)", "!자동 시작 유이설"),
    "자동 중단": ("!자동 중단", ""),
    "자동 상태": ("!자동 상태", ""),
    "자동 개입": ("!자동 개입 [GM에게 전달할 메모]", "!자동 개입 마교 세작을 등장시켜라"),
    "자동 턴제한": ("!자동 턴제한 [N | 해제]", "!자동 턴제한 20"),
    "자동 비용제한": ("!자동 비용제한 [금액(원) | 해제]", "!자동 비용제한 5000"),
    "자동 서사": ("!자동 서사", ""),
    "자동 재계획": ("!자동 재계획 (메모)", "!자동 재계획 플레이어가 적과 손잡음"),
}


def _usage_text(ctx) -> str:
    """해당 명령의 한글 사용법+예시 문자열. 큐레이션 없으면 signature 폴백."""
    cmd = ctx.command
    key = cmd.qualified_name if cmd else ""
    entry = USAGE.get(key)
    if entry:
        usage, example = entry
        s = f"사용법: `{usage}`"
        if example:
            s += f"\n예: `{example}`"
        return s
    # 폴백: discord.py 자동 시그니처
    sig = (cmd.signature if cmd else "").strip()
    return f"사용법: `!{key} {sig}`".rstrip()


class ErrorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_command_error(self, ctx, error):
        # 명령별 자체 error_handler가 처리한 경우 건너뜀
        if ctx.command is not None and ctx.command.has_error_handler():
            return

        # ── 명령어 오타 (CommandNotFound) ── 세션 채널에서만 반응 (타 채널 stray '!' 노이즈 방지)
        if isinstance(error, commands.CommandNotFound):
            if self.bot.active_sessions.get(ctx.channel.id) is None:
                return
            typo = (ctx.invoked_with or "").strip()
            if not typo:
                return
            names = []
            for c in self.bot.commands:
                names.append(c.name)
                names.extend(getattr(c, "aliases", []) or [])
            suggestions = core.suggest_commands(typo, names)
            if suggestions:
                hint = " / ".join(f"`!{s}`" for s in suggestions)
                await ctx.send(f"❓ `!{typo}` 명령을 찾을 수 없습니다. 혹시 {hint} 인가요?")
            else:
                await ctx.send(f"❓ `!{typo}` 명령을 찾을 수 없습니다. `!명령어`로 전체 목록을 확인하세요.")
            return

        # ── 인자 부족/과다 ──
        if isinstance(error, (commands.MissingRequiredArgument, commands.TooManyArguments)):
            await ctx.send(f"⚠️ `!{ctx.command.qualified_name}` 인자가 올바르지 않습니다.\n{_usage_text(ctx)}")
            return

        # ── 인자 타입 오류 ──
        if isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            await ctx.send(f"⚠️ 인자 형식이 올바르지 않습니다. ({error})\n{_usage_text(ctx)}")
            return

        # ── 권한/채널 체크 실패 ──
        if isinstance(error, commands.CheckFailure):
            # 전역 권한 체크(permissions.py)가 이미 안내한 경우 중복 메시지 억제
            if getattr(ctx, "_perm_denied", False):
                return
            await ctx.send("⚠️ 이 명령은 지금 이 채널 또는 권한에서 사용할 수 없습니다.")
            return

        # ── 명령 본문 예외 ── 사용자에겐 요약, 상세는 로그
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            tb = "".join(traceback.format_exception(type(original), original, original.__traceback__))
            sess = self.bot.active_sessions.get(ctx.channel.id)
            sid = getattr(sess, "session_id", None) if sess else None
            try:
                if sid:
                    core.write_log(sid, "error", f"[명령 오류] !{ctx.command} :: {original!r}\n{tb}")
            except Exception:
                pass
            print(f"⚠️ [CommandInvokeError] !{ctx.command}: {original!r}\n{tb}")
            await ctx.send("⚠️ 명령 처리 중 오류가 발생했습니다. (상세 내용은 로그에 기록되었습니다.)")
            return

        # ── 그 외 미처리 ── 크래시 방지: 콘솔 로그만
        print(f"⚠️ [처리되지 않은 명령 오류] {type(error).__name__}: {error}")


async def setup(bot):
    await bot.add_cog(ErrorCog(bot))
