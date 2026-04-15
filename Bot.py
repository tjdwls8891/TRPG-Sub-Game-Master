# ========== 임포트(Imports) ==========
import os
import re
import json
import uuid
import asyncio
import random
from datetime import datetime

import discord
from discord.ext import commands
from dotenv import load_dotenv

from google import genai
from google.genai import types
from google.genai.errors import APIError

"""
봇 명령어 사용법

[세션 관리]
!새세션 [시나리오명] : 새로운 게임 세션을 준비합니다.
!시작 : 준비된 세션을 본격적으로 시작하고 초기 메시지를 출력합니다. (1회 제한)
!소개 : 시나리오 인트로와 캐릭터 생성 안내를 출력합니다.

[캐릭터 및 NPC 설정]
!참가 [이름] : 플레이어가 캐릭터 이름으로 세션에 참가합니다.
!설정 [이름] [항목] [내용] : 캐릭터의 스탯이나 프로필을 설정합니다.
!외형 [이름] [내용] : 캐릭터의 외형 묘사를 설정합니다.
!외형확인 [이름] : 설정된 캐릭터의 외형을 확인합니다.
!프로필 [이름] : 캐릭터의 전체 프로필을 확인합니다.
!npc설정 [이름] [내용] : NPC 정보를 추가하거나 수정합니다.
!npc확인 [이름] : NPC 정보를 확인합니다.
!npc삭제 [이름] : NPC 정보를 삭제합니다.
!npc목록 : 등록된 모든 NPC 목록을 확인합니다.
!설정생성 [pc/npc] [이름] [지시사항] : AI를 통해 캐릭터 설정 초안을 자동 생성합니다.

[게임 진행 및 판정]
!진행 [지시사항] : GM의 지시사항을 바탕으로 AI가 다음 턴을 묘사합니다. (상/중/하 이미지 태그 사용 가능)
!주사위 [이름] [눈] [가중치] : 일반 주사위를 굴립니다.
!주사위 [이름] [스탯명] [눈] [가중치] : 특정 스탯 기반 주사위를 굴립니다.
!기억압축 : 현재까지의 미압축 로그를 수동으로 요약 압축합니다.

[미디어 및 채널 제어]
!이미지 [키워드] : 시나리오에 설정된 키워드의 이미지를 출력합니다.
!브금 [파일명] : 음성 채널에서 해당 BGM을 무한 반복 재생합니다.
!브금정지 : 현재 재생 중인 BGM을 부드럽게 페이드아웃하며 정지합니다.
!채팅 [잠금/해제] : 일반 플레이어의 게임 채널 채팅 입력을 통제합니다.

[시스템 관리]
!캐시재발급 : 현재 세션의 장기 기억 캐시를 강제로 삭제하고 새로 발급합니다.
!캐시삭제 : 장기 기억 캐시를 명시적으로 삭제하여 스토리지 과금을 즉시 중단합니다.
"""

# ========== 환경 변수 및 전역 상수(Constants) ==========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRPG_INTRO_TEXT = os.getenv("TRPG_INTRO_TEXT", "인트로 텍스트를 불러오지 못했습니다.")
SYSTEM_INSTRUCTION = os.getenv("SYSTEM_INSTRUCTION", "시스템 지시사항을 불러오지 못했습니다.")

DEFAULT_MODEL = "gemini-3-flash-preview"
LOGIC_MODEL = "gemini-3.1-pro-preview"

PRICING_1M = {
    "gemini-3-flash-preview": {
        "INPUT": 0.50,
        "OUTPUT": 3.00,
        "CACHE_READ": 0.05,
        "CACHE_STORAGE_PER_HOUR": 1.00
    },
    "gemini-3.1-pro-preview": {
        "INPUT": 2.00,
        "OUTPUT": 12.00,
        "CACHE_READ": 0.20,
        "CACHE_STORAGE_PER_HOUR": 4.50
    }
}

# ========== 전역 인스턴스 및 상태 변수(Global Setup) ==========
client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

active_sessions = {}
session_io_locks = {}
playlist_sessions = {}

for directory in ["sessions", "scenarios", "media"]:
    if not os.path.exists(directory):
        os.makedirs(directory)


# ========== 데이터 모델(Data Models) ==========
class TRPGSession:
    """
    단일 TRPG 세션의 모든 상태와 데이터를 관리하는 데이터 모델 클래스입니다.

    Args:
        session_id (str): 세션의 고유 식별자 (UUID 기반)
        game_ch_id (int): 플레이어들이 참여하는 게임 채널의 ID
        master_ch_id (int): GM 전용 마스터 채널의 ID
        scenario_id (str): 로드된 시나리오 파일의 이름
        scenario_data (dict): 시나리오 JSON에서 로드된 원본 데이터
    """

    def __init__(self, session_id, game_ch_id, master_ch_id, scenario_id, scenario_data):
        self.session_id = session_id
        self.game_ch_id = game_ch_id
        self.master_ch_id = master_ch_id
        self.scenario_id = scenario_id
        self.scenario_data = scenario_data

        self.cache_name = None
        self.cache_obj = None

        self.players = {}
        self.npcs = {}
        self.resources = {}
        self.statuses = {}

        self.compressed_memory = ""
        self.raw_logs = []
        self.current_turn_logs = []
        self.uncompressed_logs = []

        self.turn_count = 0
        self.is_started = False
        self.total_cost = 0.0

        self.voice_client = None
        self.current_bgm = None
        self.is_bgm_looping = False

        self.npcs = {}
        default_npcs = scenario_data.get("default_npcs", {})

        for npc_name, npc_data in default_npcs.items():
            if isinstance(npc_data, dict):
                self.npcs[npc_name] = {
                    "name": npc_data.get("name", npc_name),
                    "details": npc_data.get("details", "")
                }
            else:
                self.npcs[npc_name] = {
                    "name": npc_name,
                    "details": str(npc_data)
                }


# ========== 코어 유틸리티 함수(Utilities) ==========
def calculate_cost(model_id: str, input_tokens=0, output_tokens=0, cached_read_tokens=0, cache_storage_tokens=0, storage_hours=0) -> float:
    """
    API 사용량을 기반으로 과금액(USD)을 산출합니다.

    Args:
        model_id (str): 사용된 Gemini 모델 식별자
        input_tokens (int): 입력 토큰 수
        output_tokens (int): 출력 토큰 수
        cached_read_tokens (int): 캐시에서 읽어온 토큰 수
        cache_storage_tokens (int): 저장된 캐시 토큰 수
        storage_hours (int): 캐시 유지 시간(시간 단위)

    Returns:
        float: 산출된 총 비용 (USD)
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0
    cache_storage_tokens = cache_storage_tokens or 0
    storage_hours = storage_hours or 0

    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    actual_input_tokens = max(0, input_tokens - cached_read_tokens)

    cost = 0.0
    cost += (actual_input_tokens / 1_000_000) * rates["INPUT"]
    cost += (output_tokens / 1_000_000) * rates["OUTPUT"]
    cost += (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]
    cost += (cache_storage_tokens / 1_000_000) * rates["CACHE_STORAGE_PER_HOUR"] * storage_hours
    return cost


def load_scenario_from_file(scenario_id: str) -> dict | None:
    """
    지정된 시나리오 ID에 해당하는 JSON 파일을 읽어옵니다.

    Args:
        scenario_id (str): 불러올 시나리오 파일의 이름 (확장자 제외)

    Returns:
        dict | None: 파싱된 시나리오 데이터 딕셔너리. 파일이 없으면 None 반환.
    """
    filepath = f"scenarios/{scenario_id}.json"
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def write_log(session_id: str, log_type: str, content: str):
    """
    세션별 행동 및 시스템 로그를 타임스탬프와 함께 로컬 텍스트 파일로 저장합니다.

    Args:
        session_id (str): 로그를 저장할 세션 식별자
        log_type (str): 로그 유형 (예: 'api', 'game_chat', 'master_chat')
        content (str): 기록할 내용
    """
    if not session_id:
        return

    log_filename = f"sessions/{session_id}/{log_type}_log.txt"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(log_filename, "a", encoding="utf-8") as f:
        f.write(f"[{now_str}] {content}\n")
        if log_type == "api":
            f.write("-" * 60 + "\n")


def get_available_scenarios() -> list:
    """
    scenarios 폴더 내에 존재하는 사용 가능한 시나리오 파일 목록을 반환합니다.

    Returns:
        list: '.json' 확장자가 제거된 파일명 문자열 리스트
    """
    return [f.replace(".json", "") for f in os.listdir("scenarios") if f.endswith(".json")]


async def save_session_data(session: TRPGSession):
    """
    진행 중인 세션 객체의 상태를 JSON 파일로 디스크에 저장합니다.

    Args:
        session (TRPGSession): 저장할 세션 객체
    """
    if session.session_id not in session_io_locks:
        session_io_locks[session.session_id] = asyncio.Lock()

    async with session_io_locks[session.session_id]:
        serialized_raw_logs = []
        for content in session.raw_logs:
            serialized_raw_logs.append({
                "role": content.role,
                "text": content.parts[0].text
            })

        data = {
            "session_id": session.session_id,
            "game_ch_id": session.game_ch_id,
            "master_ch_id": session.master_ch_id,
            "scenario_id": session.scenario_id,
            "cache_name": session.cache_name,
            "players": session.players,
            "npcs": session.npcs,
            "resources": session.resources,
            "statuses": session.statuses,
            "compressed_memory": session.compressed_memory,
            "raw_logs": serialized_raw_logs,
            "current_turn_logs": session.current_turn_logs,
            "uncompressed_logs": session.uncompressed_logs,
            "turn_count": session.turn_count,
            "is_started": getattr(session, "is_started", False),
            "total_cost": getattr(session, "total_cost", 0.0)
        }

        def write_file():
            with open(f"sessions/{session.session_id}/data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

        await asyncio.to_thread(write_file)


# noinspection PyShadowingNames
async def build_scenario_cache_text(client, model_id, scenario_data: dict) -> tuple[str, int]:
    """
    시나리오 데이터를 바탕으로 Context Caching을 위한 '시나리오 핵심 룰북' 텍스트를 조립합니다.
    최소 요구 토큰 수 미달 시 패딩을 추가합니다.

    Args:
        client (genai.Client): Gemini API 클라이언트
        model_id (str): 토큰 계산에 사용할 모델 식별자
        scenario_data (dict): 시나리오 데이터 딕셔너리

    Returns:
        tuple[str, int]: 최종 완성된 룰북 텍스트와 해당 텍스트의 총 토큰 수
    """
    worldview = scenario_data.get('worldview', '특별한 세계관 정보 없음')
    story_guide = scenario_data.get('story_guide', '특별한 스토리 가이드 없음')
    stat_system = scenario_data.get('stat_system', '특별한 스탯 시스템 없음')
    desc_guide = scenario_data.get('desc_guide', '상황에 맞게 묘사하세요.')
    status_code_block = scenario_data.get('status_code_block', '상태창 코드블럭 양식 없음')

    npc_text = ""
    default_npcs = scenario_data.get("default_npcs", {})
    for npc_name, npc_data in default_npcs.items():
        if isinstance(npc_data, dict):
            details = npc_data.get("details", "")
            npc_text += f"\n- {npc_name}:\n{details}\n"

    if not npc_text:
        npc_text = "등록된 고정 NPC 없음."

    rulebook_text = f"""=== [시나리오 핵심 룰북] ===
이 내용은 세션의 근간이 되는 절대적인 세계관 및 시스템 설정입니다. 
진행자(GM)의 특별한 지시가 없는 한 아래의 설정을 완벽하게 유지하십시오.

[1. 세계관 정보]
{worldview}

[2. 스토리 진행 가이드]
{story_guide}

[3. 주요 등장인물 (NPC) 사전]
{npc_text}

[4. 게임 스탯 및 판정 시스템]
{stat_system}

[5. 시나리오 고유 묘사 가이드라인]
{desc_guide}

[6. 필수 출력: 상태창 코드블럭 양식]
(모든 묘사 후 턴의 마지막에 반드시 아래 양식을 바탕으로 상태창을 출력할 것)
{status_code_block}
============================
"""

    try:
        response = await asyncio.to_thread(
            client.models.count_tokens,
            model=model_id,
            contents=rulebook_text
        )
        base_tokens = response.total_tokens
        min_cache_tokens = 32768

        if base_tokens >= min_cache_tokens:
            return rulebook_text, base_tokens

        missing_tokens = min_cache_tokens - base_tokens + 500
        padding_chars = "." * (missing_tokens * 4)

        padded_text = rulebook_text + f"\n\n[System Data Padding Area - DO NOT READ]\n{padding_chars}"

        final_response = await asyncio.to_thread(
            client.models.count_tokens,
            model=model_id,
            contents=padded_text
        )
        total_tokens = final_response.total_tokens

        print(f"💡 [시스템] 룰북 조립 완료: 베이스({base_tokens}) + 패딩 -> 총 {total_tokens} 토큰 생성")
        return padded_text, total_tokens

    except Exception as e:
        print(f"⚠️ 토큰 계산 오류: {e}. 안전을 위해 임의의 대형 패딩을 적용합니다.")
        return rulebook_text + ("." * 150000), 38000


def format_turn_prompt(session: TRPGSession, gm_instruction: str) -> str:
    """
    모델에게 전달할 메인 턴 진행 프롬프트를 세션 상태를 종합하여 구성합니다.

    Args:
        session (TRPGSession): 현재 진행 중인 세션 객체
        gm_instruction (str): 진행자(GM)가 작성한 다음 턴 묘사 지시사항

    Returns:
        str: AI 모델에 전송할 최종 프롬프트 문자열
    """
    prompt = "[현재 게임 상태]\n"
    if session.compressed_memory:
        prompt += f"▶ 이전 상황 요약 (절대 참조용 누적 기억):\n{session.compressed_memory}\n"

    if session.players:
        prompt += "\n▶ 참가 플레이어 정보:\n"
        for uid, p_data in session.players.items():
            c_name = p_data['name']
            prompt += f"  - {c_name}: [스탯] {p_data['profile']}\n"
            if p_data.get("appearance"):
                prompt += f"    * [외형]: {p_data['appearance']}\n"

            c_res = session.resources.get(c_name, {})
            c_stat = session.statuses.get(c_name, [])
            if c_res:
                res_str = ", ".join([f"{k}: {v}" for k, v in c_res.items()])
                prompt += f"    * [확정 소지 자원]: {res_str}\n"
            if c_stat:
                stat_str = ", ".join(c_stat)
                prompt += f"    * [현재 상태이상]: {stat_str}\n"

    recent_texts = [c.parts[0].text for c in session.raw_logs[-10:]] + session.current_turn_logs
    recent_logs_combined = " ".join(recent_texts) + f" {gm_instruction}"

    if session.npcs:
        triggered_npcs = {}
        default_npcs = session.scenario_data.get("default_npcs", {})

        for npc_name, npc_data in session.npcs.items():
            if npc_name in recent_logs_combined:
                base_npc_details = default_npcs.get(npc_name, {}).get("details", "")

                if npc_data["details"] != base_npc_details:
                    triggered_npcs[npc_name] = npc_data

        if triggered_npcs:
            prompt += "\n▶ 현재 개입 중인 [수정/추가된] NPC 설정 (캐시 룰북보다 우선 적용):\n"
            for npc_name, npc_data in triggered_npcs.items():
                prompt += f"  - {npc_name}: {npc_data['details']}\n"

                # NPC에 대한 자원/상태도 존재할 경우 주입
                n_res = session.resources.get(npc_name, {})
                n_stat = session.statuses.get(npc_name, [])
                if n_res:
                    n_res_str = ", ".join([f"{k}: {v}" for k, v in n_res.items()])
                    prompt += f"    * [확정 소지 자원]: {n_res_str}\n"
                if n_stat:
                    n_stat_str = ", ".join(n_stat)
                    prompt += f"    * [현재 상태이상]: {n_stat_str}\n"

    keyword_memories = session.scenario_data.get("keyword_memory", [])
    if keyword_memories:
        triggered_memories = set()
        for memory in keyword_memories:
            for kw in memory.get("keywords", []):
                if kw in recent_logs_combined:
                    triggered_memories.add(memory.get("description", ""))
                    break

        if triggered_memories:
            prompt += "\n[키워드 연관 기억/설정 (최근 대화 기반)]\n"
            for desc in triggered_memories:
                prompt += f"▶ {desc}\n"

    prompt += "\n[최근 플레이어 행동 및 대화 (판정 완료됨)]\n"
    if session.current_turn_logs:
        prompt += "\n".join(session.current_turn_logs) + "\n"
    else:
        prompt += "(특별한 대화 없음)\n"

    prompt += f"\n[진행자(GM)의 판정 결과 및 지시사항]\n▶ {gm_instruction}\n\n"

    prompt += f"[최종 지시] 캐시된 [시나리오 핵심 룰북]의 묘사 가이드와 위 GM의 지시사항을 최우선으로 반영하여 상황을 묘사하세요.\n"

    if session.scenario_data.get("status_code_block", ""):
        prompt += f"▶ 명령: 턴의 마지막에 반드시 룰북에 정의된 양식을 바탕으로 중괄호 내부 값을 기입하여 상태창 코드블럭을 출력하십시오. (현재 턴 수: {session.turn_count + 1}턴 기입)\n"

    return prompt


def build_compression_prompt(session: TRPGSession, log_text: str) -> str:
    """
    대화 기록을 무손실 압축하기 위한 요약 전용 프롬프트를 생성합니다.

    Args:
        session (TRPGSession): 현재 진행 중인 세션 객체
        log_text (str): 요약 대상이 되는 미압축 기록 문자열 집합

    Returns:
        str: AI 모델에 전송할 압축 지시 프롬프트 문자열
    """
    return (
        "당신은 TRPG 세션의 전담 '기록 서기'입니다. \n"
        "당신의 유일한 목표는 제공된 [최근 플레이 기록]을 초정밀 무손실 압축(Lossless Compression)하여 새로운 기록을 생성하는 것입니다.\n"
        "[이전 압축 기억]은 현재 상황의 맥락(인물, 장소, 진행 상황)을 파악하는 용도로만 참고하고, 절대 요약 결과에 다시 포함하여 출력하지 마십시오.\n\n"
        "[초정밀 압축 원칙]\n"
        "1. 마이크로 디테일 보존: 행동의 '결과'만 적지 말고, '구체적인 물리적 과정'과 '타격 부위' 등을 반드시 명시하십시오.\n"
        "2. 불필요 데이터 배제: 압축할 정보 중, 단순 추임새나 추후 잊더라도 개연성에 영향을 주지 않는 행동(예: 코를 긁는다 등)은 데이터에서 제외하십시오.\n"
        "2. 장식적 요소 배제: 비유, 감정 표현, 분위기 묘사 등 문학적 수사는 철저히 걷어내십시오.\n"
        "3. 인과성 및 상태 추적: A의 행동이 B에게 어떤 상태 변화를 일으켰는지 명확한 단문으로 기록하십시오.\n"
        "4. 아이템 및 수치 명시: 획득/소비한 아이템, 주사위 판정 수치는 정확한 이름과 숫자로 기록하십시오.\n\n"
        f"[이전 압축 기억 (맥락 파악용으로만 참고할 것)]\n{session.compressed_memory if session.compressed_memory else '없음'}\n\n"
        f"[최근 플레이 기록 (압축 대상)]\n{log_text}\n\n"
        "위 원칙을 엄격히 준수하여 [최근 플레이 기록]만을 건조하고 기계적인 개조식(Bullet points)으로 압축하여 출력하십시오."
    )


async def restore_sessions_from_disk():
    """
    봇 재시작 시 로컬 디스크에 저장된 모든 세션의 JSON 데이터를 읽어와 복구합니다.
    만료된 캐시가 존재할 경우 재발급 과정을 포함합니다.
    """
    if not os.path.exists("sessions"):
        return

    print("저장된 세션 데이터 복구를 시작합니다...")
    for session_id in os.listdir("sessions"):
        data_path = f"sessions/{session_id}/data.json"
        if os.path.isfile(data_path):
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                scenario_data = load_scenario_from_file(data["scenario_id"])
                if not scenario_data:
                    continue

                session = TRPGSession(
                    data["session_id"], data["game_ch_id"], data["master_ch_id"],
                    data["scenario_id"], scenario_data
                )
                session.players = data.get("players", {})
                session.npcs = data.get("npcs", {})
                session.resources = data.get("resources", {})
                session.statuses = data.get("statuses", {})
                session.compressed_memory = data.get("compressed_memory", "")
                session.cache_name = data.get("cache_name")
                session.current_turn_logs = data.get("current_turn_logs", [])
                session.turn_count = data.get("turn_count", 0)
                session.uncompressed_logs = data.get("uncompressed_logs", [])
                session.is_started = data.get("is_started", False)
                session.total_cost = data.get("total_cost", 0.0)

                restored_raw_logs = []
                for item in data.get("raw_logs", []):
                    restored_raw_logs.append(
                        types.Content(role=item["role"], parts=[types.Part.from_text(text=item["text"])])
                    )
                session.raw_logs = restored_raw_logs

                if session.cache_name:
                    try:
                        session.cache_obj = await asyncio.to_thread(client.caches.get, name=session.cache_name)
                        print(f"✅ {session_id}: 기존 캐시 연동 성공.")
                    except APIError:
                        print(f"🔄 {session_id}: 기존 캐시 만료됨. 새로 발급합니다...")
                        caching_text, cache_tokens = await build_scenario_cache_text(client, DEFAULT_MODEL,
                                                                                     scenario_data)

                        creation_cost = calculate_cost(DEFAULT_MODEL, input_tokens=cache_tokens)
                        storage_cost = calculate_cost(DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
                        session.total_cost += (creation_cost + storage_cost)
                        print(
                            f"💰 [비용 보고] 세션({session_id}) 복구용 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

                        cache = await asyncio.to_thread(
                            client.caches.create,
                            model=DEFAULT_MODEL,
                            config=types.CreateCachedContentConfig(
                                system_instruction=SYSTEM_INSTRUCTION,
                                contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                                ttl="3600s"
                            )
                        )

                        session.cache_obj = cache
                        session.cache_name = cache.name
                        await save_session_data(session)

                active_sessions[session.game_ch_id] = session
                active_sessions[session.master_ch_id] = session
                print(f"✅ 세션 {session_id} 복구 완료.")

            except Exception as e:
                print(f"⚠️ 세션 {session_id} 복구 중 오류: {e}")


def get_uid_by_char_name(session: TRPGSession, char_name: str) -> str | None:
    """
    캐릭터 이름 문자열을 통해 매핑된 디스코드 사용자 ID를 찾아 반환합니다.

    Args:
        session (TRPGSession): 검사할 대상 세션 객체
        char_name (str): 찾고자 하는 캐릭터의 이름

    Returns:
        str | None: 일치하는 디스코드 사용자 ID 문자열. 없으면 None.
    """
    for uid, p_data in session.players.items():
        if p_data["name"] == char_name:
            return uid
    return None


async def send_image_by_keyword(game_channel, master_ctx, session, keyword):
    """
    시나리오 데이터에 지정된 키워드와 파일 매핑을 참조하여 이미지를 게임 채널에 전송합니다.

    Args:
        game_channel (discord.TextChannel): 이미지를 전송할 디스코드 게임 채널 객체
        master_ctx (commands.Context): 오류 메시지를 전송할 디스코드 마스터 컨텍스트 객체
        session (TRPGSession): 대상 세션 객체
        keyword (str): 출력할 이미지의 트리거 키워드
    """
    media_keywords = session.scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    if keyword in media_keywords:
        filepath = os.path.join(media_dir, media_keywords[keyword])
        if os.path.exists(filepath):
            await game_channel.send(file=discord.File(filepath))
        else:
            await master_ctx.send(f"⚠️ [이미지 경고] 설정된 파일이 경로에 없습니다: `{filepath}`")
    else:
        await master_ctx.send(f"⚠️ [이미지 경고] 등록되지 않은 키워드입니다: `{keyword}`")


# noinspection PyShadowingNames
async def generate_character_details(client, scenario_data, char_type, char_name, instruction, session_id):
    """
    AI 모델을 사용하여 PC 또는 NPC의 세부 설정 초안을 텍스트로 생성합니다.

    Args:
        client (genai.Client): Gemini API 클라이언트
        scenario_data (dict): 기준이 될 시나리오 세계관 데이터
        char_type (str): 캐릭터의 종류 ('pc' 또는 'npc')
        char_name (str): 생성할 캐릭터의 이름
        instruction (str): GM이 추가로 부여한 세부 지시사항
        session_id (str): API 로그를 기록할 세션 식별자

    Returns:
        types.GenerateContentResponse: API에서 반환한 응답 객체
    """
    worldview = scenario_data.get("worldview", "특별한 세계관 정보 없음")

    style_guide = (
        "[작성 지침]\n"
        "1. 분량: 공백 포함 1000자 내외로 제한합니다.\n"
        "2. 문체: 불필요한 서술어를 철저히 생략하고, 핵심 정보만 전달하는 간결한 단문(개조식) 및 명사형/음슴체 종결을 사용하십시오.\n"
        "   (예시: '~가 특징이다.' -> '~가 특징.', '~를 좋아한다.' -> '~를 좋아함.')\n"
        "3. 창작 원칙: 제공된 지시사항은 왜곡 없이 반영하고, 누락된 [필수 항목]은 세계관에 맞춰 논리적으로 창작하십시오.\n\n"
    )

    if char_type == "pc":
        prompt = (
            f"당신은 TRPG에서 플레이어가 조종할 '플레이어 캐릭터(PC)'의 설정을 정리하는 보조 작가입니다.\n"
            f"{style_guide}"
            f"[세계관 정보]\n{worldview}\n\n"
            f"- 대상 이름: {char_name}\n"
            f"- GM 지시사항: {instruction}\n\n"
            f"[필수 항목 (PC)]\n"
            f"- 나이:\n"
            f"- 키와 체형:\n"
            f"- 외모:\n"
            f"(※ 그 외 GM 지시사항에 포함된 추가 설정이나 배경이 있다면 필수 항목 아래에 자연스럽게 이어서 정리할 것. 디테일 강화는 허용하나 새로운 내용 창작 금지.)"
        )
    else:
        prompt = (
            f"당신은 TRPG 세계관 속에서 GM이 조종할 '논플레이어 캐릭터(NPC)'의 설정을 정리하는 보조 작가입니다.\n"
            f"{style_guide}"
            f"[세계관 정보]\n{worldview}\n\n"
            f"- 대상 이름: {char_name}\n"
            f"- GM 지시사항: {instruction}\n\n"
            f"[필수 항목 (NPC)]\n"
            f"- 나이:\n"
            f"- 키와 체형:\n"
            f"- 외모:\n"
            f"- 성격:\n"
            f"- 소속:\n"
            f"- 특기:\n"
            f"- 좋아하는 것과 싫어하는 것:\n"
            f"(※ 그 외 GM 지시사항에 포함된 추가 설정이나 배경이 있다면 필수 항목 아래에 자연스럽게 이어서 정리할 것)"
        )

    write_log(session_id, "api", f"[{char_type.upper()} 설정 생성 요청 - {char_name}]\n{prompt}")

    response = await asyncio.to_thread(
        client.models.generate_content,
        model=LOGIC_MODEL,
        contents=prompt
    )

    return response


async def stream_text_to_channel(channel, text: str, words_per_tick: int = 10, tick_interval: float = 1.5):
    """
    디스코드 채널에 텍스트를 문단과 단어 단위로 쪼개어 타이핑 치듯 스트리밍 연출합니다.

    Args:
        channel (discord.TextChannel): 텍스트를 출력할 디스코드 채널 객체
        text (str): 출력할 원본 전체 텍스트
        words_per_tick (int): 한 번의 갱신에 출력할 단어 수
        tick_interval (float): 갱신 간격 (초 단위)
    """
    session = active_sessions.get(channel.id)
    paragraphs = text.split('\n\n')

    for paragraph in paragraphs:
        if not paragraph.strip():
            continue

        current_text = "> " if not paragraph.startswith(">") else ""
        current_message = await channel.send(current_text + "✍️")

        words = paragraph.split(' ')
        display_text = current_text

        for i in range(0, len(words), words_per_tick):
            chunk = words[i:i + words_per_tick]
            display_text += " ".join(chunk) + " "

            await asyncio.sleep(tick_interval)

            try:
                if len(display_text) > 1950:
                    break
                await current_message.edit(content=display_text + "✍️")
            except discord.errors.HTTPException:
                pass

        final_text = display_text[:2000].strip()
        await current_message.edit(content=final_text)

        if session:
            write_log(session.session_id, "game_chat", f"[연출 완료]: {final_text}")


class PlaylistManager:
    """
    음성 채널에서의 플레이리스트 셔플 재생 상태 및 백그라운드 루프를 관리하는 클래스입니다.

    Args:
        vc (discord.VoiceClient): 연결된 음성 채널 클라이언트
        queue (list): 재생할 로컬 mp3 파일 경로들의 리스트
        text_channel (discord.TextChannel): 알림을 보낼 디스코드 텍스트 채널
    """
    def __init__(self, vc, queue, text_channel):
        self.vc = vc
        self.queue = queue
        self.text_channel = text_channel
        self.current_index = 0
        self.play_next_event = asyncio.Event()
        self.skip_direction = 1
        self.task = bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        try:
            # noinspection PyTypeChecker
            while True:
                self.play_next_event.clear()

                if self.current_index >= len(self.queue):
                    self.current_index = 0
                elif self.current_index < 0:
                    self.current_index = len(self.queue) - 1

                filepath = self.queue[self.current_index]

                def after_play(err):
                    if err:
                        print(f"⚠️ 플레이리스트 재생 오류: {err}")
                    bot.loop.call_soon_threadsafe(self.play_next_event.set)

                source = discord.FFmpegPCMAudio(filepath)
                volume_source = discord.PCMVolumeTransformer(source, volume=1.0)
                self.vc.play(volume_source, after=after_play)

                await self.play_next_event.wait()

                self.current_index += self.skip_direction
                self.skip_direction = 1

        except asyncio.CancelledError:
            pass
        finally:
            if self.vc and self.vc.is_connected():
                if self.vc.is_playing() or self.vc.is_paused():
                    self.vc.stop()


# ========== 디스코드 UI 클래스(Views) ==========
class GeneralDiceView(discord.ui.View):
    """
    능력치에 구애받지 않는 일반 주사위(N면체)를 굴리기 위한 UI 뷰어입니다.

    Args:
        target_uid (str): 주사위를 굴릴 자격을 가진 플레이어의 디스코드 ID
        max_val (int): 주사위의 최대 눈금 수
        weight (int): 판정 결과에 합산될 추가 가중치
    """
    def __init__(self, target_uid: str, max_val: int, weight: int = 0):
        super().__init__(timeout=None)
        self.target_uid = target_uid
        self.max_val = max_val
        self.weight = weight

    @discord.ui.button(label="🎲 일반 주사위 굴리기", style=discord.ButtonStyle.secondary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message("이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True)

        base_result = random.randint(1, self.max_val)
        final_result = base_result + self.weight

        weight_str = f" (가중치 {self.weight:+d})" if self.weight != 0 else ""
        calc_str = f" ({base_result}{self.weight:+d})" if self.weight != 0 else ""

        await interaction.response.edit_message(
            content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 일반 다이스 결과{weight_str}: **{final_result}**{calc_str}",
            view=None
        )

        session = active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)
            session.current_turn_logs.append(
                f"[{char_name}]: {self.max_val}눈 일반 주사위 굴림{weight_str} -> 최종 결과 {final_result}"
            )
            await save_session_data(session)

        await interaction.channel.send(
            f"📣 **일반 주사위 결과:** {char_name}의 {self.max_val}면체 주사위 최종 눈은 **{final_result}**입니다.{weight_str}"
        )
        return None


class DiceView(discord.ui.View):
    """
    특정 스탯의 기준치를 기반으로 성공/실패를 판정하는 능력치 주사위 UI 뷰어입니다.

    Args:
        target_uid (str): 주사위를 굴릴 자격을 가진 플레이어의 디스코드 ID
        max_val (int): 주사위의 최대 눈금 수
        stat_name (str): 굴림의 기준이 되는 캐릭터 스탯의 이름
        stat_value (int): 스탯의 현재 수치
        weight (int): 기준 목표값에 합산될 보정 가중치
    """
    def __init__(self, target_uid: str, max_val: int, stat_name: str, stat_value: int, weight: int):
        super().__init__(timeout=None)
        self.target_uid = target_uid
        self.max_val = max_val
        self.stat_name = stat_name
        self.stat_value = stat_value
        self.weight = weight

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message("이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True)

        result = random.randint(1, self.max_val)

        target_value = self.stat_value + self.weight
        is_success = result <= target_value
        result_text = "성공 🟢" if is_success else "실패 🔴"

        weight_str = f" (가중치 {self.weight:+d} 적용)" if self.weight != 0 else ""
        target_str = f"{self.stat_value}{self.weight:+d}={target_value}" if self.weight != 0 else f"{self.stat_value}"

        await interaction.response.edit_message(
            content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 다이스 결과: **{result}** [{self.stat_name}] 굴림  (기준치: {target_str})",
            view=None
        )

        session = active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)
            session.current_turn_logs.append(
                f"[{char_name}]: [{self.stat_name}]{weight_str} 판정 (1~{self.max_val}) -> 주사위 {result} ({result_text})")
            await save_session_data(session)

        await interaction.channel.send(
            f"> 📣 **판정 결과:** {char_name}의 [{self.stat_name}] 판정{weight_str} - **{result_text}**"
        )
        return None


class IntroView(discord.ui.View):
    """
    세션 인트로 텍스트를 문단별로 끊어서 수동으로 스트리밍하기 위한 UI 뷰어입니다.

    Args:
        session (TRPGSession): 연출이 진행될 대상 세션 객체
        game_channel (discord.TextChannel): 텍스트가 출력될 디스코드 채널
        paragraphs (list): 출력할 인트로 텍스트의 문단 리스트
    """
    def __init__(self, session, game_channel, paragraphs):
        super().__init__(timeout=None)
        self.session = session
        self.game_channel = game_channel
        self.paragraphs = paragraphs
        self.current_idx = 0

    @discord.ui.button(label="다음 내용 출력", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        button.disabled = True
        button.label = "출력 중..."
        await interaction.response.edit_message(view=self)

        text_to_send = self.paragraphs[self.current_idx]
        await stream_text_to_channel(self.game_channel, text_to_send, words_per_tick=5, tick_interval=1.5)

        self.current_idx += 1

        if self.current_idx >= len(self.paragraphs):
            button.label = "소개 완료 (채팅 권한 해제됨)"
            button.style = discord.ButtonStyle.success
            button.disabled = True
            await interaction.message.edit(view=self)

            await self.game_channel.set_permissions(interaction.guild.default_role, send_messages=True)
            await self.game_channel.send("🔓 **[시스템] 플레이어 채팅 입력이 허용되었습니다.**")
        else:
            button.label = "다음 내용 출력"
            button.disabled = False
            await interaction.message.edit(view=self)


# ========== 봇 이벤트 핸들러(Events) ==========
@bot.event
async def on_ready():
    """
    디스코드 서버에 봇이 정상적으로 로그인하여 준비되었을 때 호출되는 이벤트입니다.
    """
    print("=================================")
    print(f'로그인 성공: {bot.user.name}')
    scenarios = get_available_scenarios()
    print(f'로드 가능한 시나리오 파일: {", ".join(scenarios) if scenarios else "없음"}')
    await restore_sessions_from_disk()
    print("=================================")


@bot.event
async def on_message(message):
    """
    채널에 메시지가 전송될 때마다 호출되어 명령어를 검사하고 행동/대화 로그를 처리하는 이벤트입니다.

    Args:
        message (discord.Message): 수신된 메시지 객체
    """
    await bot.process_commands(message)

    session = active_sessions.get(message.channel.id)

    if message.content.startswith('!'):
        if session and message.channel.id == session.master_ch_id:
            write_log(session.session_id, "master_chat", f"[GM 명령어]: {message.content}")
        return

    if not session:
        return

    if message.author == bot.user:
        if "✍️" in message.content:
            return

        if message.channel.id == session.master_ch_id:
            write_log(session.session_id, "master_chat", f"[시스템]: {message.content}")
        elif message.channel.id == session.game_ch_id:
            write_log(session.session_id, "game_chat", f"[시스템]: {message.content}")
        return

    if message.channel.id == session.master_ch_id:
        game_channel = bot.get_channel(session.game_ch_id)
        if game_channel:
            await stream_text_to_channel(game_channel, f"> {message.content}", words_per_tick=5, tick_interval=1.5)
            session.current_turn_logs.append(f"[진행자]: {message.content}")
            await save_session_data(session)

        write_log(session.session_id, "master_chat", f"[GM 전달]: {message.content}")

    elif message.channel.id == session.game_ch_id:
        user_id_str = str(message.author.id)

        if user_id_str in session.players:
            char_name = session.players[user_id_str]["name"]
        else:
            char_name = message.author.display_name

        session.current_turn_logs.append(f"[{char_name}]: {message.content}")
        await save_session_data(session)

        write_log(session.session_id, "game_chat", f"[{char_name}]: {message.content}")


# ========== 디스코드 명령어(Commands) ==========
# ========== 세션 관리 그룹 ==========
@bot.command(name="새세션")
async def create_session(ctx, scenario_id: str = None):
    """
    서버에 새로운 카테고리와 채널을 생성하고 시나리오 데이터를 캐싱하여 세션을 준비합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        scenario_id (str): 로드할 시나리오 파일 이름
    """
    if not scenario_id:
        scenarios = get_available_scenarios()
        await ctx.send(f"⚠️ 시나리오 파일명을 입력해주세요. 예: `!새세션 dark_fantasy`\n(현재 파일: {', '.join(scenarios)})")
        return

    scenario_data = load_scenario_from_file(scenario_id)
    if not scenario_data:
        await ctx.send(f"⚠️ 'scenarios/{scenario_id}.json' 파일을 찾을 수 없거나 형식이 잘못되었습니다.")
        return

    guild = ctx.guild
    session_id = str(uuid.uuid4())[:8]
    await ctx.send(f"🔄 '{scenario_id}.json' 데이터를 로드하여 세션({session_id})을 준비합니다...")

    session_dir = f"sessions/{session_id}"
    os.makedirs(session_dir, exist_ok=True)

    category = await guild.create_category(f"TRPG Session {session_id}")
    game_overwrites = {
        guild.default_role: discord.PermissionOverwrite(send_messages=False)
    }
    game_ch = await guild.create_text_channel(f"game-{session_id}", category=category, overwrites=game_overwrites)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True)
    }
    master_ch = await guild.create_text_channel(f"master-{session_id}", category=category, overwrites=overwrites)

    session = TRPGSession(session_id, game_ch.id, master_ch.id, scenario_id, scenario_data)

    try:
        await ctx.send("⏳ 시나리오 설정 및 장기 기억 캐싱 중...")
        caching_text, cache_tokens = await build_scenario_cache_text(client, DEFAULT_MODEL, scenario_data)

        creation_cost = calculate_cost(DEFAULT_MODEL, input_tokens=cache_tokens)
        storage_cost = calculate_cost(DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
        session.total_cost += (creation_cost + storage_cost)
        print(
            f"💰 [비용 보고] 세션({session_id}) 캐시 생성 및 1시간 저장비 선결제: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

        cache = await asyncio.to_thread(
            client.caches.create,
            model=DEFAULT_MODEL,
            config=types.CreateCachedContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                ttl="3600s"
            )
        )
        session.cache_obj = cache
        session.cache_name = cache.name
        await ctx.send(f"✅ 캐싱 완료! (캐시 ID: {cache.name})")
    except Exception as e:
        await ctx.send(f"⚠️ 캐싱 실패 (일반 모드로 진행됩니다. 원인: {e})")

    active_sessions[game_ch.id] = session
    active_sessions[master_ch.id] = session
    await save_session_data(session)

    await ctx.send(f"🎉 세션 준비 완료!\n플레이어 채널: {game_ch.mention}\n마스터 채널: {master_ch.mention}")


@bot.command(name="시작")
@commands.has_permissions(administrator=True)
async def start_game(ctx):
    """
    세션의 시작 메시지를 게임 채널에 출력하고 AI 모델에 컨텍스트를 주입합니다. (1회 한정)

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session:
        return None

    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    if getattr(session, "is_started", False):
        return await ctx.send("⚠️ 이미 시작된 세션입니다. 한 세션에서 `!시작` 명령어는 한 번만 사용할 수 있습니다.")

    session.is_started = True
    await save_session_data(session)

    start_message = session.scenario_data.get("start_message", "> 세션이 시작됩니다.")
    start_text = f"**[세션 시작]**\n{start_message}"

    await stream_text_to_channel(game_channel, start_text, words_per_tick=5, tick_interval=1.5)

    session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=start_text)]))
    await save_session_data(session)

    if ctx.channel.id != session.game_ch_id:
        await ctx.send("✅ 게임 채널에 초기 시작 메시지를 출력하고, 기억 로그에 추가했습니다.")
    return None


@start_game.error
async def start_game_error(ctx, error):
    """
    start_game 명령어 실행 중 권한 등의 에러가 발생했을 때 처리합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        error (Exception): 발생한 예외 객체
    """
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("⚠️ 이 명령어는 서버 관리자 권한을 가진 사용자(GM)만 사용할 수 있습니다.")


@bot.command(name="소개")
async def send_intro(ctx):
    """
    시나리오 인트로와 캐릭터 생성 안내 메시지를 UI 뷰어 형태로 전송하여 순차 출력을 돕습니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    scenario_intro = session.scenario_data.get("scenario_intro", "")
    pc_template = session.scenario_data.get("pc_template", {})

    template_keys_str = "\n".join([f"- {k}" for k in pc_template.keys()])
    guide_text = f"이제 플레이어 여러분의 캐릭터를 만들 차례입니다. `!참가 [이름]` 명령어를 통해 참가하세요.\n\n[플레이어 스탯 구성]\n{template_keys_str}"

    full_text = f"{TRPG_INTRO_TEXT}\n\n{scenario_intro}\n\n{guide_text}"

    paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]

    view = IntroView(session, game_channel, paragraphs)
    await ctx.send("📢 **[소개 모드 활성화]** 아래 버튼을 눌러 게임 채널에 소개 문단을 순차적으로 스트리밍하십시오.", view=view)
    return None


# ========== 캐릭터 및 NPC 설정 그룹 ==========
@bot.command(name="참가")
async def join_session(ctx, char_name: str):
    """
    플레이어를 세션 데이터베이스에 지정한 캐릭터명으로 등록합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 등록할 캐릭터의 이름
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.game_ch_id:
        await ctx.send("이 명령어는 게임 채널에서만 사용할 수 있습니다.")
        return

    user_id_str = str(ctx.author.id)
    base_profile = session.scenario_data.get("pc_template", {}).copy()

    session.players[user_id_str] = {
        "name": char_name,
        "profile": base_profile,
        "appearance": ""
    }
    await save_session_data(session)

    try:
        await ctx.author.edit(nick=char_name)
    except Exception:
        pass

    await ctx.send(
        f"✅ {ctx.author.mention}님이 **'{char_name}'**(으)로 세션에 참가했습니다!\n"
        f"(진행자(GM)가 설정을 통해 스탯을 배분해 줄 것입니다.)"
    )


@bot.command(name="설정")
async def set_profile(ctx, char_name: str, key: str, *, value: str):
    """
    특정 캐릭터의 프로필/스탯 속성을 지정한 값으로 갱신합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 대상 캐릭터 이름
        key (str): 갱신할 속성 키
        value (str): 갱신될 데이터 값
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        return

    user_id_str = get_uid_by_char_name(session, char_name)
    if not user_id_str:
        await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")
        return

    player_data = session.players[user_id_str]

    if key not in player_data["profile"]:
        allowed_keys = ", ".join(player_data["profile"].keys())
        await ctx.send(f"⚠️ 해당 시나리오에 없는 항목입니다. (가능한 항목: {allowed_keys})")
        return

    player_data["profile"][key] = value
    await save_session_data(session)

    game_channel = bot.get_channel(session.game_ch_id)
    if game_channel:
        await game_channel.send(f"✅ <@{user_id_str}>의 [{key}] 항목이 '{value}'(으)로 갱신되었습니다.")


@bot.command(name="외형")
async def set_appearance(ctx, char_name: str, *, appearance: str):
    """
    특정 캐릭터의 외형 묘사를 설정합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 대상 캐릭터 이름
        appearance (str): 적용할 외형 묘사 텍스트
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        return None

    user_id_str = get_uid_by_char_name(session, char_name)
    if not user_id_str:
        await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")
        return None

    session.players[user_id_str]["appearance"] = appearance
    await save_session_data(session)

    await ctx.send(f"✅ 캐릭터 [{char_name}] 외형 설정 완료 (덮어쓰기):\n{appearance}")
    return None


@bot.command(name="외형확인")
async def check_appearance(ctx, char_name: str):
    """
    특정 캐릭터에 설정된 외형 묘사 데이터를 마스터 채널에 출력합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 대상 캐릭터 이름
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    user_id_str = get_uid_by_char_name(session, char_name)
    if not user_id_str:
        return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

    appearance = session.players[user_id_str].get("appearance", "설정된 외형이 없습니다.")
    await ctx.send(f"🎭 **{char_name}의 현재 외형**:\n{appearance}")
    return None


@bot.command(name="프로필")
async def show_profile(ctx, char_name: str):
    """
    특정 캐릭터의 모든 스탯과 외형이 포함된 프로필 카드를 게임 채널에 출력합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 대상 캐릭터 이름
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    user_id_str = get_uid_by_char_name(session, char_name)
    if not user_id_str:
        return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

    player_data = session.players[user_id_str]
    member = ctx.guild.get_member(int(user_id_str))

    embed = discord.Embed(title=f"🎭 {char_name}의 프로필", color=0x3498db)
    if member:
        embed.set_author(name=member.display_name,
                         icon_url=member.display_avatar.url if member.display_avatar else None)
    else:
        embed.set_author(name=char_name)

    for key, val in player_data["profile"].items():
        embed.add_field(name=key, value=val, inline=True)

    appearance = player_data.get("appearance")
    if appearance:
        embed.add_field(name="외형", value=appearance, inline=False)

    game_channel = bot.get_channel(session.game_ch_id)
    if game_channel:
        await game_channel.send(embed=embed)
    return None


@bot.command(name="npc설정")
async def set_npc(ctx, name: str, *, details: str):
    """
    세션에 참여할 NPC의 상세 설정을 추가하거나 갱신합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        name (str): NPC 이름
        details (str): NPC 세부 설정 텍스트
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    session.npcs[name] = {"name": name, "details": details}
    await save_session_data(session)

    await ctx.send(f"✅ NPC [{name}] 설정 완료 (덮어쓰기):\n{details}")
    return None


@bot.command(name="npc확인")
async def check_npc(ctx, name: str):
    """
    저장된 특정 NPC의 세부 설정을 출력합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        name (str): 조회할 NPC 이름
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    if name in session.npcs:
        details = session.npcs[name]["details"]
        await ctx.send(f"📜 **NPC [{name}] 정보**:\n{details}")
        return None
    else:
        await ctx.send(f"⚠️ NPC [{name}]을(를) 찾을 수 없습니다.")
        return None


@bot.command(name="npc삭제")
async def remove_npc(ctx, name: str):
    """
    저장된 특정 NPC 데이터를 세션에서 삭제합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        name (str): 삭제할 NPC 이름
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    if name in session.npcs:
        del session.npcs[name]
        await save_session_data(session)
        await ctx.send(f"✅ NPC [{name}] 삭제 완료.")
        return None
    else:
        await ctx.send(f"⚠️ NPC [{name}]을(를) 찾을 수 없습니다.")
        return None


@bot.command(name="npc목록")
async def list_npc(ctx):
    """
    현재 세션에 등록된 모든 NPC의 정보 목록을 출력합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    if not session.npcs:
        return await ctx.send("등록된 NPC가 없습니다.")

    embed = discord.Embed(title="📜 등록된 NPC 목록", color=0x2ecc71)
    for npc_name, npc_data in session.npcs.items():
        embed.add_field(name=npc_name, value=npc_data["details"], inline=False)

    await ctx.send(embed=embed)
    return None


@bot.command(name="설정생성")
async def generate_character_cmd(ctx, char_type: str, char_name: str, *, instruction: str):
    """
    입력된 지시사항을 바탕으로 AI를 호출하여 캐릭터(PC/NPC)의 상세 설정 초안을 생성합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_type (str): 생성할 타입 ('pc' 혹은 'npc')
        char_name (str): 생성할 캐릭터 이름
        instruction (str): 창작 시 반영할 구체적 지시사항
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    char_type = char_type.lower()
    if char_type not in ["pc", "npc"]:
        return await ctx.send("⚠️ 캐릭터 유형은 `pc` 또는 `npc` 중 하나로 입력해주세요.\n(예시: `!설정생성 npc 레온타르트 용병단장`)")

    type_kr = "플레이어 캐릭터(PC)" if char_type == "pc" else "NPC"
    await ctx.send(f"⏳ AI가 세계관을 바탕으로 {type_kr} '{char_name}'의 설정 초안을 생성 중입니다. 잠시만 기다려주세요...")

    try:
        response = await generate_character_details(client, session.scenario_data, char_type, char_name,
                                                          instruction, session.session_id)
        generated_text = response.text

        meta = response.usage_metadata
        in_tokens = meta.prompt_token_count
        out_tokens = meta.candidates_token_count
        turn_cost = calculate_cost(LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
        session.total_cost += turn_cost
        print(
            f"💰 [비용 보고] 설정 생성({char_name}) - In:{in_tokens}, Out:{out_tokens} | 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")
        await save_session_data(session)

        if char_type == "pc":
            guide_cmd = f"`!외형 {char_name} [내용]`"
        else:
            guide_cmd = f"`!npc설정 {char_name} [내용]`"

        header = f"💡 **[{char_name}] {type_kr} 설정 초안 생성 완료**\n*아래 내용을 복사하여 자유롭게 수정한 뒤, {guide_cmd} 명령어로 게임에 적용하세요.*\n\n"
        full_message = header + generated_text

        if len(full_message) > 2000:
            for i in range(0, len(full_message), 2000):
                await ctx.send(full_message[i:i + 2000])
        else:
            await ctx.send(full_message)

    except Exception as e:
        await ctx.send(f"⚠️ 설정 초안 생성 중 오류가 발생했습니다: {e}")


# ========== 게임 진행 및 판정 그룹 ==========
# noinspection PyInconsistentReturns
@bot.command(name="주사위")
async def request_dice(ctx, char_name: str, param1: str, param2: str = None, param3: str = None):
    """
    일반적인 N면체 또는 캐릭터의 특정 스탯 기준에 대한 주사위 굴림 요청 UI를 전송합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        char_name (str): 굴림을 수행할 캐릭터 이름
        param1 (str): 주사위의 면 수(일반) 또는 기준이 되는 스탯 이름(능력치)
        param2 (str, optional): 가중치(일반) 또는 스탯 주사위의 면 수(능력치)
        param3 (str, optional): 스탯 판정에서의 보정 가중치
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    user_id_str = get_uid_by_char_name(session, char_name)
    if not user_id_str:
        return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

    player_data = session.players[user_id_str]
    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    if param1.isdigit():
        max_val = int(param1)
        weight = 0

        if param2 and param2.lstrip('-').isdigit():
            weight = int(param2)

        req_weight_str = f" (가중치 {weight:+d})" if weight != 0 else ""

        view = GeneralDiceView(target_uid=user_id_str, max_val=max_val, weight=weight)
        await game_channel.send(
            f"> 🎲 <@{user_id_str}>, 일반 {max_val}면체 다이스 판정을 시작합니다. 아래 버튼을 눌러주세요.{req_weight_str}",
            view=view
        )
        return None

    stat_name = param1
    if not param2 or not param2.lstrip('-').isdigit():
        return await ctx.send("⚠️ 능력치 판정 시 최대 눈(max_val)을 입력해야 합니다. 예: `!주사위 아서 근력 100`")

    max_val = int(param2)
    weight = int(param3) if param3 and param3.lstrip('-').isdigit() else 0

    if stat_name not in player_data["profile"]:
        allowed_keys = ", ".join(player_data["profile"].keys())
        return await ctx.send(f"⚠️ 프로필에 [{stat_name}] 항목이 없습니다. (가능한 항목: {allowed_keys})")

    try:
        stat_value = int(player_data["profile"][stat_name])
    except ValueError:
        return await ctx.send(f"⚠️ [{stat_name}]의 값이 숫자가 아닙니다. 판정을 진행할 수 없습니다.")

    req_weight_str = f" (가중치 {weight:+d})" if weight != 0 else ""
    view = DiceView(target_uid=user_id_str, max_val=max_val, stat_name=stat_name, stat_value=stat_value, weight=weight)

    await game_channel.send(
        f"> 🎲 <@{user_id_str}>, {max_val}눈 다이스로 [{stat_name}:{stat_value}] 판정을 시작합니다. 아래 버튼을 눌러주세요. {req_weight_str}",
        view=view
    )
    return None


@bot.command(name="진행")
async def proceed_turn(ctx, *, instruction: str = ""):
    """
    입력된 지시사항과 현재 누적된 로그를 기반으로 다음 게임 턴의 상황을 생성 및 연출합니다.
    인라인 특수 태그(상/중/하 이미지, 자원, 상태이상)를 파싱하여 백그라운드 상태를 즉각 갱신합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        instruction (str, optional): 진행할 방향성에 대한 GM의 프롬프트
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    if not getattr(session, "is_started", False):
        return await ctx.send("⚠️ 세션이 아직 시작되지 않았습니다. API 역할 동기화를 위해 반드시 `!시작` 명령어를 먼저 실행하십시오.")

    # 1. 태그 정규식 파싱
    img_pattern = r'(상|중|하):([^\s]+)'
    img_tags = re.findall(img_pattern, instruction)

    top_imgs, mid_imgs, bottom_imgs = [], [], []
    for pos, kw in img_tags:
        if pos == '상':
            top_imgs.append(kw)
        elif pos == '중':
            mid_imgs.append(kw)
        elif pos == '하':
            bottom_imgs.append(kw)

    res_pattern = r'자:([^\s;]+);([^\s;]+);([-+]?\d+)'
    res_tags = re.findall(res_pattern, instruction)

    for char_name, item_name, amount_str in res_tags:
        amount = int(amount_str)
        if char_name not in session.resources:
            session.resources[char_name] = {}
        session.resources[char_name][item_name] = session.resources[char_name].get(item_name, 0) + amount

    status_pattern = r'태:([^\s;]+);([^\s]+)'
    status_tags = re.findall(status_pattern, instruction)

    for char_name, status_text in status_tags:
        if char_name not in session.statuses:
            session.statuses[char_name] = []

        # 값 앞에 '-'가 붙어 있으면 해당 상태이상을 리스트에서 제거
        if status_text.startswith("-"):
            target_status = status_text[1:]
            if target_status in session.statuses[char_name]:
                session.statuses[char_name].remove(target_status)
        else:
            if status_text not in session.statuses[char_name]:
                session.statuses[char_name].append(status_text)

    # 2. 파싱된 태그 텍스트들을 AI 프롬프트용 지시문에서 제거
    clean_instruction = re.sub(img_pattern, '', instruction)
    clean_instruction = re.sub(res_pattern, '', clean_instruction)
    clean_instruction = re.sub(status_pattern, '', clean_instruction)
    clean_instruction = re.sub(r'\s+', ' ', clean_instruction).strip()

    if not clean_instruction:
        clean_instruction = "현재까지의 상황, 세계관, 누적된 기억, 그리고 플레이어의 직전 행동을 바탕으로 물리적 인과율에 맞춰 개연성 있게 다음 상황을 진행하고 묘사하십시오."

    await ctx.send("⏳ AI가 묘사를 생성 중입니다. 완료 후 게임 채널에 타이핑 연출을 시작합니다...")

    prompt = format_turn_prompt(session, clean_instruction)
    write_log(session.session_id, "api", f"[메인 턴 묘사 요청]\n{prompt}")

    current_contents = session.raw_logs + [
        types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
    ]

    async def generate_with_retry(retry_count=0):
        try:
            if session.cache_obj and session.cache_name:
                config = types.GenerateContentConfig(cached_content=session.cache_name, temperature=0.7)
            else:
                config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION, temperature=0.7)

            async with game_channel.typing():
                return await asyncio.to_thread(
                    client.models.generate_content,
                    model=DEFAULT_MODEL,
                    contents=current_contents,
                    config=config
                )
        except APIError as e:
            if retry_count == 0 and ("cache" in str(e).lower() or e.code in [400, 404]):
                await ctx.send("🔄 **[시스템 알림]** 장기 기억 캐시가 만료되어 자동으로 재발급을 진행합니다. 턴 묘사는 이어서 출력됩니다...")

                caching_text, cache_tokens = await build_scenario_cache_text(client, DEFAULT_MODEL,
                                                                             session.scenario_data)
                creation_cost = calculate_cost(DEFAULT_MODEL, input_tokens=cache_tokens)
                storage_cost = calculate_cost(DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
                session.total_cost += (creation_cost + storage_cost)

                print(
                    f"💰 [비용 보고] 세션({session.session_id}) 진행 중 자동 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

                new_cache = await asyncio.to_thread(
                    client.caches.create,
                    model=DEFAULT_MODEL,
                    config=types.CreateCachedContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                        ttl="3600s"
                    )
                )
                session.cache_obj = new_cache
                session.cache_name = new_cache.name
                session.cache_model = DEFAULT_MODEL
                await save_session_data(session)

                return await generate_with_retry(retry_count=1)
            else:
                raise e

    try:
        response = await generate_with_retry()

        meta = response.usage_metadata
        in_tokens = meta.prompt_token_count
        out_tokens = meta.candidates_token_count
        cached_tokens = getattr(meta, "cached_content_token_count", 0)

        turn_cost = calculate_cost(DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                   cached_read_tokens=cached_tokens)
        session.total_cost += turn_cost
        print(
            f"💰 [비용 보고] 턴 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

        full_ai_response = response.text

        turn_history_text = "\n".join(session.current_turn_logs) + f"\n[GM 지시]: {clean_instruction}"
        session.raw_logs.append(types.Content(role="user", parts=[types.Part.from_text(text=turn_history_text)]))
        session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=full_ai_response)]))

        session.uncompressed_logs.append(f"[플레이어 및 GM]: {turn_history_text}")
        session.uncompressed_logs.append(f"[GM 묘사]: {full_ai_response}")

        session.current_turn_logs.clear()
        session.turn_count += 1

        if len(session.raw_logs) > 20:
            session.raw_logs = session.raw_logs[-20:]

        code_block_match = re.search(r'(.*)(```.*?```)\s*$', full_ai_response, re.DOTALL)
        if code_block_match:
            narrative_text = code_block_match.group(1).strip()
            code_block_text = code_block_match.group(2).strip()
        else:
            narrative_text = full_ai_response.strip()
            code_block_text = ""

        paragraphs = [p.strip() for p in narrative_text.split('\n\n') if p.strip()]

        if not paragraphs:
            for kw in top_imgs + mid_imgs + bottom_imgs:
                await send_image_by_keyword(game_channel, ctx, session, kw)
        else:
            for i, paragraph in enumerate(paragraphs):
                await stream_text_to_channel(game_channel, paragraph, words_per_tick=5, tick_interval=1.5)

                if i == 0:
                    for kw in top_imgs:
                        await send_image_by_keyword(game_channel, ctx, session, kw)

                for kw in list(mid_imgs):
                    if kw in paragraph:
                        await send_image_by_keyword(game_channel, ctx, session, kw)
                        mid_imgs.remove(kw)

            for kw in mid_imgs:
                await send_image_by_keyword(game_channel, ctx, session, kw)
            for kw in bottom_imgs:
                await send_image_by_keyword(game_channel, ctx, session, kw)

        if code_block_text:
            await game_channel.send(code_block_text)

        await ctx.send(f"✅ 묘사 연출 완료 (현재 {session.turn_count}턴 경과). 다음 턴 대기 중...")

        if session.turn_count > 0 and session.turn_count % 5 == 0:
            if not session.uncompressed_logs:
                pass
            else:
                await ctx.send(f"⏳ (시스템: 백그라운드에서 자동 초정밀 기억 압축을 진행합니다...)")

                logs_to_compress = list(session.uncompressed_logs)
                log_text = "\n\n".join(logs_to_compress)
                summary_prompt = build_compression_prompt(session, log_text)

                write_log(session.session_id, "api", f"[기억 압축 요청]\n{summary_prompt}")

                try:
                    summary_response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=LOGIC_MODEL,
                        contents=summary_prompt
                    )

                    meta = summary_response.usage_metadata
                    in_tokens = meta.prompt_token_count
                    out_tokens = meta.candidates_token_count
                    cached_tokens = getattr(meta, "cached_content_token_count", 0)

                    turn_cost = calculate_cost(LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                               cached_read_tokens=cached_tokens)
                    session.total_cost += turn_cost
                    print(
                        f"💰 [비용 보고] 기억 압축 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

                    new_compressed_segment = summary_response.text.strip()
                    if session.compressed_memory:
                        session.compressed_memory += f"\n{new_compressed_segment}"
                    else:
                        session.compressed_memory = new_compressed_segment

                    del session.uncompressed_logs[:len(logs_to_compress)]

                    success_msg = f"✅ 자동 누적 압축 완료.\n**[최근 추가된 기억]**\n{new_compressed_segment}"
                    if len(success_msg) > 2000:
                        for i in range(0, len(success_msg), 2000):
                            await ctx.send(success_msg[i:i + 2000])
                            await asyncio.sleep(1)
                    else:
                        await ctx.send(success_msg)
                except Exception as e:
                    await ctx.send(f"⚠️ 자동 기억 압축 중 오류 발생: {e}")

        await save_session_data(session)

    except Exception as e:
        await ctx.send(f"⚠️ 시스템 오류가 발생했습니다: {str(e)}")


@bot.command(name="기억압축")
async def compress_memory(ctx):
    """
    현재까지 대기열에 쌓인 턴 로그들을 초정밀 요약하여 장기 기억 공간에 병합합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        return

    if not session.uncompressed_logs:
        await ctx.send("압축할 새로운 대화 로그가 없습니다.")
        return

    await ctx.send("⏳ 수 초정밀 기억 압축을 진행 중입니다...")

    logs_to_compress = list(session.uncompressed_logs)
    log_text = "\n\n".join(logs_to_compress)
    summary_prompt = build_compression_prompt(session, log_text)

    write_log(session.session_id, "api", f"[기억 압축 요청]\n{summary_prompt}")

    try:
        summary_response = await asyncio.to_thread(
            client.models.generate_content,
            model=LOGIC_MODEL,
            contents=summary_prompt
        )

        meta = summary_response.usage_metadata
        in_tokens = meta.prompt_token_count
        out_tokens = meta.candidates_token_count
        cached_tokens = getattr(meta, "cached_content_token_count", 0)

        turn_cost = calculate_cost(LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens, cached_read_tokens=cached_tokens)
        session.total_cost += turn_cost
        print(f"💰 [비용 보고] 수동 기억 압축 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

        new_compressed_segment = summary_response.text.strip()
        if session.compressed_memory:
            session.compressed_memory += f"\n{new_compressed_segment}"
        else:
            session.compressed_memory = new_compressed_segment

        del session.uncompressed_logs[:len(logs_to_compress)]
        await save_session_data(session)

        success_msg = f"✅ 수동 누적 압축 완료.\n**[최근 추가된 기억]**\n{new_compressed_segment}"
        if len(success_msg) > 2000:
            for i in range(0, len(success_msg), 2000):
                await ctx.send(success_msg[i:i + 2000])
                await asyncio.sleep(1)
        else:
            await ctx.send(success_msg)

    except Exception as e:
        await ctx.send(f"⚠️ 요약 중 오류 발생: {e}")


# ========== 미디어 및 채널 제어 그룹 ==========
@bot.command(name="이미지")
async def send_media(ctx, keyword: str):
    """
    시나리오 파일에 설정된 키워드를 기반으로 게임 채널에 로컬 미디어 이미지를 전송합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        keyword (str): 출력할 이미지의 지정된 식별 키워드
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    scenario_data = session.scenario_data
    media_keywords = scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    if keyword not in media_keywords:
        available_keys = ", ".join(media_keywords.keys()) if media_keywords else "등록된 키워드 없음"
        return await ctx.send(f"⚠️ '{keyword}'에 매핑된 파일이 없습니다. (사용 가능한 키워드: {available_keys})")

    filename = media_keywords[keyword]
    filepath = os.path.join(media_dir, filename)

    if not os.path.exists(filepath):
        return await ctx.send(f"⚠️ 설정된 경로에 파일이 존재하지 않습니다: `{filepath}`")

    try:
        await game_channel.send(file=discord.File(filepath))
        await ctx.send(f"✅ 게임 채널에 '{keyword}' 이미지를 출력했습니다.")
    except Exception as e:
        await ctx.send(f"⚠️ 이미지 전송 중 오류가 발생했습니다: {e}")


@bot.command(name="브금")
async def play_bgm(ctx, filename: str):
    """
    음성 채널에 봇을 입장시키고 지정된 오디오 파일의 반복 재생 루프를 시작합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        filename (str): 재생할 미디어 파일의 이름 (확장자 제외)
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    if not ctx.author.voice:
        return await ctx.send("⚠️ 마스터님, 먼저 디스코드 음성 채널에 접속해 주십시오.")

    media_dir = f"media/{session.scenario_id}"
    filepath = os.path.join(media_dir, f"{filename}.mp3")

    if not os.path.exists(filepath):
        return await ctx.send(f"⚠️ 설정된 파일이 경로에 없습니다: `{filepath}`")

    voice_channel = ctx.author.voice.channel

    vc = ctx.voice_client
    if not vc:
        vc = await voice_channel.connect()
    elif isinstance(vc, discord.VoiceClient) and vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    session.voice_client = vc

    # noinspection PyShadowingNames
    def after_playing(error):
        if error:
            print(f"⚠️ BGM 재생 오류: {error}")

        if getattr(session, "is_bgm_looping", False) and session.voice_client and session.voice_client.is_connected():
            try:
                next_filepath = os.path.join(media_dir, f"{session.current_bgm}.mp3")
                if os.path.exists(next_filepath):
                    source = discord.FFmpegPCMAudio(next_filepath)
                    volume_source = discord.PCMVolumeTransformer(source, volume=1.0)
                    session.voice_client.play(volume_source, after=after_playing)
            except Exception as e:
                print(f"⚠️ BGM 루프 생성 중 오류: {e}")

    fade_task = getattr(session, "fade_task", None)
    if fade_task and not fade_task.done():
        fade_task.cancel()
        session.is_fading = False

    if vc.is_playing():
        session.is_fading = True
        session.current_bgm = filename
        await ctx.send(f"🔉 볼륨을 서서히 줄인 후 BGM을 **'{filename}'**(으)로 교체합니다...")

        async def fade_out():
            try:
                if isinstance(vc.source, discord.PCMVolumeTransformer):
                    for _ in range(20):
                        if not vc.is_playing():
                            break
                        vc.source.volume = vc.source.volume * 0.8
                        await asyncio.sleep(0.1)
                    vc.source.volume = 0.0
                vc.stop()
            except asyncio.CancelledError:
                pass
            finally:
                session.is_fading = False

        session.fade_task = bot.loop.create_task(fade_out())

    else:
        session.current_bgm = filename
        session.is_bgm_looping = True

        source = discord.FFmpegPCMAudio(filepath)
        volume_source = discord.PCMVolumeTransformer(source, volume=1.0)
        vc.play(volume_source, after=after_playing)
        await ctx.send(f"▶️ BGM **'{filename}'**의 무한 반복 재생을 시작합니다.")


@bot.command(name="브금정지")
async def stop_bgm(ctx):
    """
    현재 재생 중인 음성 채널의 미디어를 부드럽게 감쇠시키며 정지합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    vc = session.voice_client
    if vc and vc.is_connected() and vc.is_playing():

        fade_task = getattr(session, "fade_task", None)
        if fade_task and not fade_task.done():
            fade_task.cancel()

        session.is_bgm_looping = False
        session.current_bgm = None
        session.is_fading = True

        await ctx.send("🔉 볼륨을 서서히 줄이며 BGM을 정지합니다...")

        async def fade_out_and_stop():
            try:
                if isinstance(vc.source, discord.PCMVolumeTransformer):
                    for _ in range(20):
                        if not vc.is_playing():
                            break
                        vc.source.volume = vc.source.volume * 0.8
                        await asyncio.sleep(0.1)
                    vc.source.volume = 0.0
                vc.stop()
            except asyncio.CancelledError:
                pass
            finally:
                session.is_fading = False

        session.fade_task = bot.loop.create_task(fade_out_and_stop())

    else:
        await ctx.send("⚠️ 현재 재생 중인 BGM이 없거나 음성 채널에 연결되어 있지 않습니다.")


@bot.command(name="플리")
async def playlist_control(ctx, action: str, scenario_id: str = None):
    """
    지정된 시나리오 미디어 폴더의 mp3 파일들을 셔플하여 무한 루프 플레이리스트 형태로 제어합니다.
    세션 진행과 무관하게 독립적으로 사용할 수 있습니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        action (str): 수행할 행동 (시작/재생/다음/이전/일시정지/종료)
        scenario_id (str, optional): 재생을 시작할 때 지정할 시나리오 폴더명
    """
    guild_id = ctx.guild.id

    if action in ["시작", "재생"] and scenario_id:
        if guild_id in playlist_sessions:
            return await ctx.send("⚠️ 이미 플레이리스트가 실행 중입니다. `!플리 종료` 후 다시 시작하거나 `!플리 재생`을 입력해 일시정지를 해제하세요.")

        if not ctx.author.voice:
            return await ctx.send("⚠️ 먼저 디스코드 음성 채널에 접속해 주십시오.")

        media_dir = f"media/{scenario_id}"
        if not os.path.exists(media_dir):
            return await ctx.send(f"⚠️ 해당 시나리오 미디어 폴더를 찾을 수 없습니다: `{media_dir}`")

        queue = [os.path.join(media_dir, f) for f in os.listdir(media_dir) if f.endswith(".mp3")]
        if not queue:
            return await ctx.send(f"⚠️ `{media_dir}` 폴더 내에 재생 가능한 mp3 파일이 없습니다.")

        random.shuffle(queue)

        voice_channel = ctx.author.voice.channel
        vc = ctx.voice_client
        if not vc:
            vc = await voice_channel.connect()
        elif isinstance(vc, discord.VoiceClient) and vc.channel != voice_channel:
            await vc.move_to(voice_channel)

        manager = PlaylistManager(vc, queue, ctx.channel)
        playlist_sessions[guild_id] = manager

        await ctx.send(f"🎵 **{scenario_id}** 미디어 폴더의 mp3 파일 {len(queue)}개를 셔플하여 플레이리스트 재생을 시작합니다.")
        return

    manager = playlist_sessions.get(guild_id)

    if not manager:
        return await ctx.send("⚠️ 현재 실행 중인 플레이리스트가 없습니다. `!플리 시작 [시나리오명]`으로 먼저 시작하십시오.")

    if action == "종료":
        manager.task.cancel()
        if manager.vc and manager.vc.is_connected():
            await manager.vc.disconnect()
        del playlist_sessions[guild_id]
        await ctx.send("⏹️ 플레이리스트 재생을 완전히 종료하고 음성 채널에서 퇴장합니다.")

    elif action == "다음":
        manager.skip_direction = 1
        if manager.vc.is_playing() or manager.vc.is_paused():
            manager.vc.stop()
        await ctx.send("⏭️ 현재 곡을 건너뛰고 다음 곡을 재생합니다.")

    elif action == "이전":
        manager.skip_direction = -1
        if manager.vc.is_playing() or manager.vc.is_paused():
            manager.vc.stop()
        await ctx.send("⏮️ 현재 곡을 취소하고 이전 곡을 재생합니다.")

    elif action == "일시정지":
        if manager.vc.is_playing():
            manager.vc.pause()
            await ctx.send("⏸️ 플레이리스트 재생을 일시정지했습니다.")
        else:
            await ctx.send("⚠️ 이미 일시정지 상태이거나 현재 재생 중인 곡이 없습니다.")

    elif action == "재생":
        if manager.vc.is_paused():
            manager.vc.resume()
            await ctx.send("▶️ 플레이리스트 재생을 재개합니다.")
        else:
            await ctx.send("⚠️ 일시정지 상태가 아닙니다.")

    else:
        await ctx.send("⚠️ 잘못된 명령어입니다. (사용 가능 인자: 시작/다음/이전/일시정지/재생/종료)")


@bot.command(name="채팅")
async def control_chat(ctx, state: str):
    """
    게임 채널에서 @everyone 권한 유저의 채팅 발언 가능 여부를 토글합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
        state (str): 변경할 상태 키워드 ('잠금', '금지', '오프' 등 혹은 '해제', '허용', '온' 등)
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

    game_channel = bot.get_channel(session.game_ch_id)
    if not game_channel:
        return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

    if state in ["잠금", "금지", "오프", "off"]:
        await game_channel.set_permissions(ctx.guild.default_role, send_messages=False)
        await ctx.send("🔒 게임 채널의 일반 유저 채팅 입력을 **잠금** 처리했습니다.")
        return None

    elif state in ["해제", "허용", "온", "on"]:
        await game_channel.set_permissions(ctx.guild.default_role, send_messages=True)
        await ctx.send("🔓 게임 채널의 일반 유저 채팅 입력을 **해제**했습니다.")
        return None

    else:
        await ctx.send("⚠️ 올바른 상태 인자를 입력해주세요. (사용 예시: `!채팅 잠금` 또는 `!채팅 해제`)")
        return None


# ========== 시스템 관리 그룹 ==========
@bot.command(name="명령어")
async def show_commands(ctx):
    """
    마스터 채널에서 사용 가능한 전체 명령어와 인자, 특수 태그 목록을 Embed 형태로 출력합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
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
        "`!외형 [이름] [내용]` : 캐릭터 외형 설정\n"
        "`!외형확인 [이름]` : 캐릭터 외형 확인\n"
        "`!프로필 [이름]` : 캐릭터 전체 프로필 확인\n"
        "`!npc설정 [이름] [내용]` : NPC 정보 추가/수정\n"
        "`!npc확인 [이름]` : NPC 정보 확인\n"
        "`!npc삭제 [이름]` : NPC 데이터 삭제\n"
        "`!npc목록` : 등록된 모든 NPC 목록 출력\n"
        "`!설정생성 [pc/npc] [이름] [지시사항]` : AI 설정 초안 생성"
    ), inline=False)

    embed.add_field(name="[게임 진행 및 판정]", value=(
        "`!진행 [지시사항]` : AI 턴 묘사 진행\n"
        "  *(특수 태그: `상/중/하:이미지키워드`, `자:이름;아이템;수치`, `태:이름;[-]상태`)*\n"
        "`!주사위 [이름] [눈] [가중치]` : 일반 주사위 굴림\n"
        "`!주사위 [이름] [스탯명] [눈] [가중치]` : 능력치 주사위 굴림\n"
        "`!기억압축` : 미압축 로그 수동 요약 및 압축"
    ), inline=False)

    embed.add_field(name="[미디어 및 채널 제어]", value=(
        "`!이미지 [키워드]` : 지정된 로컬 이미지 출력\n"
        "`!브금 [파일명]` : 해당 BGM 무한 반복 재생\n"
        "`!브금정지` : BGM 페이드아웃 및 정지\n"
        "`!플리 [행동] [시나리오명]` : 플레이리스트 제어 (행동: 시작/종료/다음/이전/일시정지/재생)\n"
        "`!채팅 [잠금/해제]` : 일반 플레이어 채팅 통제"
    ), inline=False)

    embed.add_field(name="[시스템 관리]", value=(
        "`!캐시재발급` : 장기 기억 캐시 강제 파기 및 재발급\n"
        "`!캐시삭제` : 장기 기억 캐시 명시적 삭제 (과금 중단)"
    ), inline=False)

    await ctx.send(embed=embed)


@bot.command(name="캐시재발급")
async def reissue_cache(ctx):
    """
    의도치 않은 캐시 만료나 데이터 오염을 해결하기 위해 강제로 이전 캐시를 삭제하고 즉시 재발급합니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        return

    await ctx.send("⏳ 수동 캐시 재발급을 시작합니다. 기존 캐시를 삭제하고 새로 생성 중...")

    if session.cache_name:
        try:
            await asyncio.to_thread(client.caches.delete, name=session.cache_name)
            print(f"🗑️ [캐시 관리] {session.session_id}: 기존 캐시({session.cache_name}) 명시적 삭제 완료.")
        except Exception as e:
            print(f"⚠️ [캐시 관리] {session.session_id}: 기존 캐시 삭제 실패 (이미 만료되었거나 존재하지 않음) - {e}")

    try:
        caching_text, cache_tokens = await build_scenario_cache_text(client, DEFAULT_MODEL, session.scenario_data)

        creation_cost = calculate_cost(DEFAULT_MODEL, input_tokens=cache_tokens)
        storage_cost = calculate_cost(DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
        session.total_cost += (creation_cost + storage_cost)

        print(
            f"💰 [비용 보고] 세션({session.session_id}) 수동 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

        cache = await asyncio.to_thread(
            client.caches.create,
            model=DEFAULT_MODEL,
            config=types.CreateCachedContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                ttl="3600s"
            )
        )

        session.cache_obj = cache
        session.cache_name = cache.name
        session.cache_model = DEFAULT_MODEL
        await save_session_data(session)

        await ctx.send(f"✅ 수동 캐시 재발급 완료! (새 캐시 ID: {cache.name})\n누적 비용에 캐시 생성 및 1시간 유지 비용이 합산되었습니다.")

    except Exception as e:
        await ctx.send(f"⚠️ 캐시 재발급 중 오류가 발생했습니다: {e}")


@bot.command(name="캐시삭제")
async def delete_cache(ctx):
    """
    현재 유지 중인 세션 캐시를 완전히 파기하여 스토리지 과금을 중단시킵니다.

    Args:
        ctx (commands.Context): 디스코드 컨텍스트 객체
    """
    session = active_sessions.get(ctx.channel.id)
    if not session or ctx.channel.id != session.master_ch_id:
        await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        return

    if not session.cache_name:
        await ctx.send("⚠️ 현재 유지 중인 캐시가 없습니다.")
        return

    await ctx.send("⏳ 구글 서버에서 기존 캐시를 명시적으로 삭제하는 중입니다...")

    try:
        await asyncio.to_thread(client.caches.delete, name=session.cache_name)
        print(f"🗑️ [캐시 관리] {session.session_id}: 수동 캐시({session.cache_name}) 삭제 완료.")

        session.cache_name = None
        session.cache_obj = None
        session.cache_model = None
        await save_session_data(session)

        await ctx.send(
            "✅ 캐시가 정상적으로 삭제되어 스토리지 과금이 중단되었습니다.\n(참고: 이후 캐시 없이 `!진행` 시 매번 전체 로그를 읽게 되어 요금이 치솟을 수 있습니다. 게임 재개 시 반드시 `!캐시재발급`을 먼저 실행해 주십시오.)")

    except Exception as e:
        print(f"⚠️ [캐시 관리] {session.session_id}: 캐시 삭제 실패 - {e}")

        session.cache_name = None
        session.cache_obj = None
        session.cache_model = None
        await save_session_data(session)

        await ctx.send(f"⚠️ 캐시 삭제 중 오류가 발생했습니다 (이미 만료되어 사라졌을 확률이 높습니다): {e}\n✅ 시스템 상의 캐시 연결은 안전하게 해제되었습니다.")


# ========== 실행부(Execution) ==========
bot.run(DISCORD_TOKEN)