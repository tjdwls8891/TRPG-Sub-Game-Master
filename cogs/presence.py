# 봇 프레즌스(상태 메시지) 자동 로테이션.
#
# 유저 활동이 없어도 봇이 살아있는 상태를 시각적으로 유지하고, 진행 중인 세션 수 등
# 유용한 정보를 주기적으로 노출한다. discord.ext.tasks 루프가 일정 간격마다 change_presence를 호출한다.
#
# ⚠️ 디스코드는 프레즌스 갱신에 레이트리밋(대략 5회/분)을 적용한다. 간격을 너무 짧게(≈12초 미만)
#    잡으면 봇이 레이트리밋되거나 게이트웨이 경고가 발생할 수 있으므로 STATUS_ROTATE_SECONDS는
#    12초 이상을 권장한다.
import discord
from discord.ext import commands, tasks

import core

STATUS_ROTATE_SECONDS = 15  # 상태 메시지 교체 주기(초). 디스코드 레이트리밋상 12초 이상 권장.


class PresenceCog(commands.Cog):
    """봇 상태 메시지를 주기적으로 로테이션한다 (유저 무활동 시에도 활성 상태 유지)."""

    def __init__(self, bot):
        self.bot = bot
        self._index = 0
        self.rotate_status.start()

    def cog_unload(self):
        # !리로드 시 기존 루프를 정리해 중복 실행을 방지한다.
        self.rotate_status.cancel()

    def _active_session_count(self) -> int:
        # active_sessions는 game_ch_id·master_ch_id 두 키로 동일 세션을 등록하므로 고유 세션만 집계.
        seen = set()
        for s in self.bot.active_sessions.values():
            seen.add(getattr(s, "session_id", id(s)))
        return len(seen)

    def _statuses(self) -> list:
        """(ActivityType, 표시문구) 튜플 목록. 매 주기 재계산하여 세션 수 등 동적 정보를 반영한다."""
        n = self._active_session_count()
        session_line = f"진행 중인 세션 {n}개 📜" if n else "새로운 세션을 기다리는 중 📜"
        return [
            (discord.ActivityType.playing, "TRPG · 자유 시나리오 🎲"),
            (discord.ActivityType.watching, session_line),
            (discord.ActivityType.listening, "플레이어는 선언해주세요. 😎"),
            (discord.ActivityType.playing, "GM의 붓끝에서 펼쳐지는 서사 ⚔️"),
            (discord.ActivityType.watching, "플레이어가 발견 못 한 비밀 🎭"),
        ]

    async def _check_expired(self):
        """열린 세션의 캐시 만료를 점검한다.

        만료는 API 호출이 실패해야 알아차리므로, 그 전까지 디스플레이가
        열린 것으로 보인다. 상태 메시지 주기에 얹어 확인한다.

        열린 세션만 보므로 부담이 적다. 대개 0~2개다.
        """
        seen = set()
        for session in list(self.bot.active_sessions.values()):
            sid = getattr(session, "session_id", None)
            if not sid or sid in seen:
                continue
            seen.add(sid)

            # 이미 닫힌 세션은 볼 것이 없다.
            if not getattr(session, "cache_name", None):
                continue
            if not core.is_cache_expired(session):
                continue
            # 같은 만료를 반복 처리하지 않는다.
            if getattr(session, "cache_expired_notified", False):
                continue

            session.cache_expired_notified = True
            print(f"[Presence] 캐시 만료 감지: {sid}")
            try:
                await core.refresh_display(self.bot, session, reason="expired")
            except Exception as e:
                print(f"[Presence] 디스플레이 갱신 실패(무시): {e}")

            game_ch = self.bot.get_channel(getattr(session, "game_ch_id", 0))
            if game_ch:
                try:
                    await game_ch.send(
                        "⏸️ **세션 유지 시간이 끝났습니다.**\n"
                        "> 디스플레이 채널의 **세션 열기**를 누르시면 이어서 진행하실 수 있습니다.")
                except Exception:
                    pass

    @tasks.loop(seconds=STATUS_ROTATE_SECONDS)
    async def rotate_status(self):
        try:
            await self._check_expired()
        except Exception as e:
            print(f"[Presence] 만료 점검 실패(무시): {e}")

        statuses = self._statuses()
        atype, text = statuses[self._index % len(statuses)]
        self._index += 1
        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=atype, name=text),
            )
        except Exception as e:
            # 레이트리밋·일시적 게이트웨이 문제 등은 무시하고 다음 주기에 재시도한다.
            print(f"[Presence] 상태 갱신 실패(무시): {e}")

    @rotate_status.before_loop
    async def _before(self):
        # 게이트웨이 연결이 준비된 뒤에 첫 갱신을 시작한다.
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(PresenceCog(bot))
