import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

from google import genai

# 코어 유틸리티 모듈 임포트
import core

# ========== 환경 변수 로드 ==========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 시스템 구동에 필요한 기본 디렉토리 생성 (cogs 폴더 포함)
for directory in ["sessions", "scenarios", "media", "cogs"]:
    if not os.path.exists(directory):
        os.makedirs(directory)


# ========== 메인 봇 클래스 정의 ==========
class TRPGBot(commands.Bot):
    """
    모든 전역 상태 변수와 API 클라이언트를 캡슐화하여 관리하는 메인 봇 객체입니다.
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)

        # 1. 기존 전역 변수들의 봇 객체 종속화 (상태 중앙 관리)
        self.active_sessions = {}
        self.session_io_locks = {}
        self.playlist_sessions = {}

        # 2. API 클라이언트 및 환경 텍스트 세팅
        self.genai_client = genai.Client(api_key=GEMINI_API_KEY)
        self.system_instruction = os.getenv("SYSTEM_INSTRUCTION", "시스템 지시사항을 불러오지 못했습니다.")
        self.intro_text = os.getenv("TRPG_INTRO_TEXT", "인트로 텍스트를 불러오지 못했습니다.")

    async def setup_hook(self):
        """
        봇이 시작될 때 /cogs 폴더 내부의 모든 확장 모듈(.py)을 자동으로 로드합니다.
        """
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"🔄 모듈 로드 완료: cogs.{filename[:-3]}")
                except Exception as e:
                    print(f"⚠️ 모듈 로드 실패 ({filename}): {e}")

    async def on_ready(self):
        """
        봇이 디스코드에 로그인하고 모든 Cogs가 로드된 후 실행되는 초기화 이벤트입니다.
        """
        print("=================================")
        print(f'로그인 성공: {self.user.name}')
        scenarios = core.get_available_scenarios()
        print(f'로드 가능한 시나리오 파일: {", ".join(scenarios) if scenarios else "없음"}')

        # 디스크에 저장된 세션 복구 및 캐시 재연동 실행
        await core.restore_sessions_from_disk(self)
        print("=================================")


# ========== 실행부 ==========
bot = TRPGBot()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)