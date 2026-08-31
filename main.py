import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

from google import genai

# 코어 유틸리티 모듈 임포트
import core
# GM 시스템 지시문 (코드 영역으로 분리되어 코드 변경과 함께 수정 가능)
import prompts

# ========== [환경 변수 로드 및 초기화] ==========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# [디렉토리 프로비저닝]
# 프로젝트 구동 시 누락된 폴더로 인한 파일 I/O 에러를 방지하기 위해 필수 디렉토리 강제 생성.
for directory in ["sessions", "scenarios", "media", "cogs"]:
    if not os.path.exists(directory):
        os.makedirs(directory)

# ========== [메인 봇 클래스 정의] ==========
class TRPGBot(commands.Bot):
    """
    모든 전역 상태 변수와 API 클라이언트를 캡슐화하여 관리하는 메인 봇 객체.
    """

    def __init__(self):
        """
        봇 인스턴스 초기화 및 전역 상태 딕셔너리 할당.
        """
        # NOTE: 게임 플레이어의 채팅 로그를 수집하고 명령어를 읽기 위해 message_content 인텐트 활성화 필수.
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        # 음성 채널 참가자 인식(상시 참가)에 필요. Intents.default()에 이미
        # 포함되지만 의존성을 명시해 둔다.
        intents.voice_states = True
        super().__init__(command_prefix='!', intents=intents)

        # 1. 기존 전역 변수들의 봇 객체 종속화 (상태 중앙 관리)
        # 런타임 중 데이터 오염을 방지하고 단일 진실 공급원(SSOT)을 유지하기 위한 전역 상태 딕셔너리.
        self.active_sessions = {}
        self.session_io_locks = {}
        self.playlist_sessions = {}

        # [명령어 권한] 허가 계정 allowlist (오너 + 부여받은 계정). 전역 체크(cogs/permissions.py)가 참조.
        self.authorized_users = core.load_authorized_users()

        # 2. API 클라이언트 및 환경 텍스트 세팅
        self.genai_client = genai.Client(api_key=GEMINI_API_KEY)
        # SYSTEM_INSTRUCTION은 prompts.py로 이전됨 (코드와 함께 수정·리뷰가 용이하도록).
        # WARNING: 변경 시 활성 세션은 `!캐시 재발급` 필요 — 시스템 지시문은 Gemini 캐시에 함께 업로드됨.
        self.system_instruction = prompts.SYSTEM_INSTRUCTION
        self.intro_text = os.getenv("TRPG_INTRO_TEXT", "인트로 텍스트를 불러오지 못했습니다.")

    async def setup_hook(self):
        """
        봇 시작 시 cogs 폴더 내부의 모든 확장 모듈(.py) 자동 로드.

        런타임 중 무중단 리로드(!리로드:모듈 수정사항 무중단 반영)를 지원하기 위해 기능 모듈을 동적으로 연결.
        """
        for filename in os.listdir('./cogs'):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"🔄 모듈 로드 완료: cogs.{filename[:-3]}")
                except Exception as e:
                    # WARNING: 모듈 로드 실패 시 봇은 구동되나 특정 기능이 누락되므로 로그 확인 요망.
                    print(f"⚠️ 모듈 로드 실패 ({filename}): {e}")

    async def on_message(self, message):
        """
        채팅 이벤트의 최우선 진입점.

        NOTE: 권한 검증을 봇 레벨 단일 훅으로 올린다(설계문서 6).
              cog마다 흩어져 있으면 새 채널 유형이 추가될 때 누락이 생긴다.
              차단된 메시지는 process_commands로 넘기지 않으므로 명령어도
              실행되지 않는다. cog의 on_message 리스너는 이 훅과 무관하게
              별도로 발화하므로, 각 리스너도 자체 조건을 유지한다.
        """
        if not await core.chat_guard(self, message):
            return
        await self.process_commands(message)

    async def on_ready(self):
        """
        봇 로그인 및 모든 Cogs 로드 후 1회 실행되는 초기화 이벤트.

        가능한 시나리오 목록을 표기하고, 세션 영속성을 위해 디스크 백업본 복구 실행.
        """
        print("=================================")
        print(f'로그인 성공: {self.user.name}')
        scenarios = core.get_available_scenarios()
        print(f'로드 가능한 시나리오 파일: {", ".join(scenarios) if scenarios else "없음"}')

        # [명령어 권한] 앱 오너를 자동 식별해 허가 목록에 기록(ID 하드코딩 불요).
        try:
            app_info = await self.application_info()
            owner = getattr(app_info, "owner", None)
            owner_id = owner.id if owner else None
            if owner_id:
                self.owner_id = owner_id  # discord.py is_owner()가 참조
                if self.authorized_users.get("owner_id") != owner_id:
                    self.authorized_users["owner_id"] = owner_id
                    core.save_authorized_users(self.authorized_users)
                print(f'명령어 오너: {owner} ({owner_id}) | 허가 계정 {len(self.authorized_users.get("granted", []))}명')
        except Exception as e:
            print(f"⚠️ 오너 식별 실패(권한 체크는 허가 목록으로만 동작): {e}")

        # [초대 링크] GM 홈 임베드가 bot.invite_url을 읽는다. 설정하지 않으면
        # '(미설정)'으로 남으므로 로그인 후 client_id로 생성해 둔다.
        try:
            self.invite_url = discord.utils.oauth_url(
                self.user.id,
                permissions=discord.Permissions(
                    manage_channels=True, manage_roles=True,
                    read_messages=True, send_messages=True,
                    manage_messages=True, embed_links=True,
                    attach_files=True, read_message_history=True,
                    connect=True, speak=True, use_application_commands=True,
                ),
                scopes=("bot", "applications.commands"),
            )
            print(f"초대 링크 준비 완료")
        except Exception as e:
            print(f"⚠️ 초대 링크 생성 실패(무시): {e}")

        # [디스크에 저장된 세션 복구 및 캐시 재연동 실행]
        # 봇 재시작으로 인한 데이터 증발을 막기 위해 sessions 폴더의 data.json을 메모리에 재적재.
        await core.restore_sessions_from_disk(self)

        # [GM 홈 갱신·복구] GM 스페이스는 오너가 아닌 유저의 유일한 세션
        # 진입점이다. 명령어는 마스터 채널 전용이므로, 스페이스가 없으면
        # 일반 유저는 세션을 열 방법이 없다.
        # 카테고리가 사라졌으면 재생성하고, 봇 버전·초대 링크를 갱신한다.
        for guild in self.guilds:
            try:
                if await core.refresh_home(self, guild):
                    # 명전·보드도 함께 채운다. 카테고리가 새로 만들어졌다면
                    # 홈만 갱신해서는 나머지 채널이 비어 있게 된다.
                    await core.refresh_boards(self, guild)
                    print(f"GM 홈 준비: {guild.name}")
            except Exception as e:
                print(f"⚠️ GM 홈 준비 실패({guild.name}, 무시): {e}")

        # [효과음 사전 디코드] 주사위 효과음을 미리 PCM으로 캐시해 첫 재생 지연 제거.
        try:
            await core.preload_sfx("dice")
        except Exception as e:
            print(f"⚠️ 효과음 사전 로드 실패(무시): {e}")
        print("=================================")


# ========== [실행부] ==========
bot = TRPGBot()

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)