import os
import re
import json
import asyncio
import random
from datetime import datetime

import discord
from google.genai import types
from google.genai.errors import APIError

# ========== 전역 상수(Constants) ==========
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

# ========== 프롬프트 빌더(Prompt Builder) ==========
class PromptBuilder:
    """
    TRPG 세션의 턴 진행을 위한 LLM 프롬프트를 단계별로 조립하는 빌더 클래스입니다.
    기존 format_turn_prompt 함수의 문자열 출력을 1글자의 오차도 없이 완벽히 동일하게 재현합니다.
    """
    def __init__(self, session: TRPGSession, gm_instruction: str):
        self.session = session
        self.gm_instruction = gm_instruction
        self.blocks = ["[현재 게임 상태]\n"]

        # 공통으로 사용되는 최근 로그 트리거 스캔용 문자열 사전 연산
        recent_texts = [c.parts[0].text for c in session.raw_logs[-10:]] + session.current_turn_logs
        self.recent_logs_combined = " ".join(recent_texts) + f" {gm_instruction}"

    def add_memory_block(self):
        if self.session.compressed_memory:
            self.blocks.append(f"▶ 이전 상황 요약 (절대 참조용 누적 기억):\n{self.session.compressed_memory}\n")
        return self

    def add_player_block(self):
        if self.session.players:
            block = "\n▶ 참가 플레이어 정보:\n"
            for uid, p_data in self.session.players.items():
                c_name = p_data['name']
                block += f"  - {c_name}: [스탯] {p_data['profile']}\n"
                if p_data.get("appearance"):
                    block += f"    * [외형]: {p_data['appearance']}\n"

                c_res = self.session.resources.get(c_name, {})
                c_stat = self.session.statuses.get(c_name, [])
                if c_res:
                    res_str = ", ".join([f"{k}: {v}" for k, v in c_res.items()])
                    block += f"    * [확정 소지 자원]: {res_str}\n"
                if c_stat:
                    stat_str = ", ".join(c_stat)
                    block += f"    * [현재 상태이상]: {stat_str}\n"
            self.blocks.append(block)
        return self

    def add_triggered_npc_block(self):
        if self.session.npcs:
            triggered_npcs = {}
            default_npcs = self.session.scenario_data.get("default_npcs", {})

            for npc_name, npc_data in self.session.npcs.items():
                if npc_name in self.recent_logs_combined:
                    base_npc_details = default_npcs.get(npc_name, {}).get("details", "")

                    if npc_data["details"] != base_npc_details:
                        triggered_npcs[npc_name] = npc_data

            if triggered_npcs:
                block = "\n▶ 현재 개입 중인 [수정/추가된] NPC 설정 (캐시 룰북보다 우선 적용):\n"
                for npc_name, npc_data in triggered_npcs.items():
                    block += f"  - {npc_name}: {npc_data['details']}\n"

                    # NPC에 대한 자원/상태도 존재할 경우 주입
                    n_res = self.session.resources.get(npc_name, {})
                    n_stat = self.session.statuses.get(npc_name, [])
                    if n_res:
                        n_res_str = ", ".join([f"{k}: {v}" for k, v in n_res.items()])
                        block += f"    * [확정 소지 자원]: {n_res_str}\n"
                    if n_stat:
                        n_stat_str = ", ".join(n_stat)
                        block += f"    * [현재 상태이상]: {n_stat_str}\n"
                self.blocks.append(block)
        return self

    def add_keyword_memory_block(self):
        keyword_memories = self.session.scenario_data.get("keyword_memory", [])
        if keyword_memories:
            triggered_memories = set()
            for memory in keyword_memories:
                for kw in memory.get("keywords", []):
                    if kw in self.recent_logs_combined:
                        triggered_memories.add(memory.get("description", ""))
                        break

            if triggered_memories:
                block = "\n[키워드 연관 기억/설정 (최근 대화 기반)]\n"
                for desc in triggered_memories:
                    block += f"▶ {desc}\n"
                self.blocks.append(block)
        return self

    def add_recent_action_block(self):
        block = "\n[최근 플레이어 행동 및 대화 (판정 완료됨)]\n"
        if self.session.current_turn_logs:
            block += "\n".join(self.session.current_turn_logs) + "\n"
        else:
            block += "(특별한 대화 없음)\n"
        self.blocks.append(block)
        return self

    def add_gm_instruction_block(self):
        block = f"\n[진행자(GM)의 판정 결과 및 지시사항]\n▶ {self.gm_instruction}\n\n"
        self.blocks.append(block)
        return self

    def add_rule_enforcement_block(self):
        block = f"[최종 지시] 캐시된 [시나리오 핵심 룰북]의 묘사 가이드와 위 GM의 지시사항을 최우선으로 반영하여 상황을 묘사하세요.\n"
        if self.session.scenario_data.get("status_code_block", ""):
            block += f"▶ 명령: 턴의 마지막에 반드시 룰북에 정의된 양식을 바탕으로 중괄호 내부 값을 기입하여 상태창 코드블럭을 출력하십시오. (현재 턴 수: {self.session.turn_count + 1}턴 기입)\n"
        self.blocks.append(block)
        return self

    def build(self) -> str:
        return "".join(self.blocks)

    @classmethod
    def build_prompt(cls, session, gm_instruction: str) -> str:
        """
        내부 블록 조립을 순차적으로 실행하여 완성된 문자열을 즉시 반환하는 파사드(Facade) 메서드
        """
        return (cls(session, gm_instruction)
                .add_memory_block()
                .add_player_block()
                .add_triggered_npc_block()
                .add_keyword_memory_block()
                .add_recent_action_block()
                .add_gm_instruction_block()
                .add_rule_enforcement_block()
                .build())


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


async def save_session_data(bot, session: TRPGSession):
    """
    진행 중인 세션 객체의 상태를 JSON 파일로 디스크에 저장합니다.

    Args:
        bot: 메인 봇 인스턴스
        session (TRPGSession): 저장할 세션 객체
    """
    if session.session_id not in bot.session_io_locks:
        bot.session_io_locks[session.session_id] = asyncio.Lock()

    async with bot.session_io_locks[session.session_id]:
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
async def build_scenario_cache_text(bot, model_id, scenario_data: dict) -> tuple[str, int]:
    """
    시나리오 데이터를 바탕으로 Context Caching을 위한 '시나리오 핵심 룰북' 텍스트를 조립합니다.
    최소 요구 토큰 수 미달 시 패딩을 추가합니다.

    Args:
        bot: 메인 봇 인스턴스
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
            bot.genai_client.models.count_tokens,
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
            bot.genai_client.models.count_tokens,
            model=model_id,
            contents=padded_text
        )
        total_tokens = final_response.total_tokens

        print(f"💡 [시스템] 룰북 조립 완료: 베이스({base_tokens}) + 패딩 -> 총 {total_tokens} 토큰 생성")
        return padded_text, total_tokens

    except Exception as e:
        print(f"⚠️ 토큰 계산 오류: {e}. 안전을 위해 임의의 대형 패딩을 적용합니다.")
        return rulebook_text + ("." * 150000), 38000


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
        "4. 아이템 및 수치 명시: 획득/소비한 아이템, 주사위 판정 수치는 정확한 이름과 숫자로 기록하십시오.\n"
        "5. 시간 및 턴 기록: 제공된 로그(코드블럭 등)를 바탕으로 해당 사건이 벌어진 '턴 수'와 '명시된 시점(날짜, 시간 등)'을 파악하십시오. 매 압축 요소마다 전부 기입할 필요는 없으며, 턴이 바뀌는 지점마다 해당 턴 기록의 첫 번째 항목에 이를 명시하여 사건의 발생 시점을 기록하십시오.\n\n"
        f"[이전 압축 기억 (맥락 파악용으로만 참고할 것)]\n{session.compressed_memory if session.compressed_memory else '없음'}\n\n"
        f"[최근 플레이 기록 (압축 대상)]\n{log_text}\n\n"
        "위 원칙을 엄격히 준수하여 [최근 플레이 기록]만을 건조하고 기계적인 개조식(Bullet points)으로 압축하여 출력하십시오."
    )


async def restore_sessions_from_disk(bot):
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
                        session.cache_obj = await asyncio.to_thread(bot.genai_client.caches.get, name=session.cache_name)
                        print(f"✅ {session_id}: 기존 캐시 연동 성공.")
                    except APIError:
                        print(f"🔄 {session_id}: 기존 캐시 만료됨. 새로 발급합니다...")
                        caching_text, cache_tokens = await build_scenario_cache_text(bot, DEFAULT_MODEL,
                                                                                     scenario_data)

                        creation_cost = calculate_cost(DEFAULT_MODEL, input_tokens=cache_tokens)
                        storage_cost = calculate_cost(DEFAULT_MODEL, cache_storage_tokens=cache_tokens, storage_hours=1)
                        session.total_cost += (creation_cost + storage_cost)
                        print(
                            f"💰 [비용 보고] 세션({session_id}) 복구용 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

                        cache = await asyncio.to_thread(
                            bot.genai_client.caches.create,
                            model=DEFAULT_MODEL,
                            config=types.CreateCachedContentConfig(
                                system_instruction=bot.system_instruction,
                                contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                                ttl="3600s"
                            )
                        )

                        session.cache_obj = cache
                        session.cache_name = cache.name
                        await save_session_data(bot, session)

                bot.active_sessions[session.game_ch_id] = session
                bot.active_sessions[session.master_ch_id] = session
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
async def generate_character_details(bot, scenario_data, char_type, char_name, instruction, session_id):
    """
    AI 모델을 사용하여 PC 또는 NPC의 세부 설정 초안을 텍스트로 생성합니다.

    Args:
        bot: 메인 봇 인스턴스
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
        bot.genai_client.models.generate_content,
        model=LOGIC_MODEL,
        contents=prompt
    )

    return response


async def stream_text_to_channel(bot, channel, text: str, words_per_tick: int = 10, tick_interval: float = 1.5):
    """
    디스코드 채널에 텍스트를 문단과 단어 단위로 쪼개어 타이핑 치듯 스트리밍 연출합니다.

    Args:
        bot: 메인 봇 인스턴스
        channel (discord.TextChannel): 텍스트를 출력할 디스코드 채널 객체
        text (str): 출력할 원본 전체 텍스트
        words_per_tick (int): 한 번의 갱신에 출력할 단어 수
        tick_interval (float): 갱신 간격 (초 단위)
    """
    session = bot.active_sessions.get(channel.id)
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
        bot: 메인 봇 인스턴스
        vc (discord.VoiceClient): 연결된 음성 채널 클라이언트
        queue (list): 재생할 로컬 mp3 파일 경로들의 리스트
        text_channel (discord.TextChannel): 알림을 보낼 디스코드 텍스트 채널
    """
    def __init__(self, bot, vc, queue, text_channel):
        self.bot = bot
        self.vc = vc
        self.queue = queue
        self.text_channel = text_channel
        self.current_index = 0
        self.play_next_event = asyncio.Event()
        self.skip_direction = 1
        self.task = self.bot.loop.create_task(self.player_loop())

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
                    self.bot.loop.call_soon_threadsafe(self.play_next_event.set)

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


# ========== 채널 관리 UI 및 유틸리티 ==========

def _cleanup_session_memory(bot, channel_id: int):
    """
    삭제되는 채널이 현재 활성화된 세션에 포함되어 있을 경우
    메모리 참조 에러를 방지하기 위해 딕셔너리에서 데이터를 안전하게 해제합니다.
    """
    if channel_id in bot.active_sessions:
        session = bot.active_sessions.pop(channel_id)
        other_id = session.game_ch_id if channel_id == session.master_ch_id else session.master_ch_id
        if other_id in bot.active_sessions and bot.active_sessions[other_id] == session:
            bot.active_sessions.pop(other_id)


class ChannelSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="삭제할 카테고리/채널을 선택하세요 (다중 선택 가능)",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_values = self.values
        await interaction.response.defer()


class ChannelDeleteView(discord.ui.View):
    def __init__(self, bot, ctx, target_items):
        super().__init__(timeout=120.0)
        self.bot = bot
        self.ctx = ctx
        self.selected_values = []
        self.target_items = target_items

        options = []
        for item_id, item in list(target_items.items())[:25]:  # API 한계상 최대 25개까지만 노출
            label = f"📁 {item.name}" if isinstance(item, discord.CategoryChannel) else f"💬 {item.name}"
            options.append(discord.SelectOption(label=label, value=str(item_id)))

        self.select = ChannelSelect(options)
        self.add_item(self.select)

    @discord.ui.button(label="선택 항목 영구 삭제", style=discord.ButtonStyle.danger, row=1)
    async def delete_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("명령어 실행자만 조작할 수 있습니다.", ephemeral=True)

        if not self.selected_values:
            return await interaction.response.send_message("삭제할 항목을 먼저 선택해 주십시오.", ephemeral=True)

        await interaction.response.send_message("⏳ 채널 연쇄 삭제 및 메모리 정리를 시작합니다...", ephemeral=True)

        deleted_count = 0
        for item_id_str in self.selected_values:
            item_id = int(item_id_str)
            item = self.target_items.get(item_id)

            if not item:
                continue

            try:
                if isinstance(item, discord.CategoryChannel):
                    for channel in item.channels:
                        _cleanup_session_memory(self.bot, channel.id)
                        await channel.delete()
                        deleted_count += 1
                    await item.delete()
                    deleted_count += 1
                elif isinstance(item, discord.TextChannel):
                    _cleanup_session_memory(self.bot, item.id)
                    await item.delete()
                    deleted_count += 1
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"⚠️ 채널 삭제 오류: {e}")

        for child in self.children:
            child.disabled = True

        await interaction.message.edit(content=f"✅ 연쇄 삭제 완료: 총 {deleted_count}개의 카테고리 및 채널이 정리되었습니다.", view=self)
        self.stop()


# ========== 디스코드 UI 클래스(Views) ==========
class GeneralDiceView(discord.ui.View):
    """
    능력치에 구애받지 않는 일반 주사위(N면체) 및 임의 목표값 판정을 위한 UI 뷰어입니다.

    Args:
        target_uid (str): 주사위를 굴릴 자격을 가진 플레이어의 디스코드 ID
        max_val (int): 주사위의 최대 눈금 수
        weight (int): 판정 결과 또는 기준치에 합산될 추가 가중치
        target_val (int, optional): 성공/실패를 판정할 기준 목표값
    """

    def __init__(self, bot, target_uid: str, max_val: int, weight: int = 0, target_val: int = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_uid = target_uid
        self.max_val = max_val
        self.weight = weight
        self.target_val = target_val

    @discord.ui.button(label="🎲 일반 주사위 굴리기", style=discord.ButtonStyle.secondary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message("> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True)

        result = random.randint(1, self.max_val)

        session = self.bot.active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)

        if self.target_val is None:
            # 기존 일반 주사위 로직
            final_result = result + self.weight
            weight_str = f" (가중치 {self.weight:+d})" if self.weight != 0 else ""
            calc_str = f" ({result}{self.weight:+d})" if self.weight != 0 else ""

            await interaction.response.edit_message(
                content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 일반 다이스 결과{weight_str}: **{final_result}**{calc_str}",
                view=None
            )

            if session:
                session.current_turn_logs.append(
                    f"[{char_name}]: {self.max_val}눈 일반 주사위 굴림{weight_str} -> 최종 결과 {final_result}"
                )
                await save_session_data(self.bot, session)

            await interaction.channel.send(
                f"> 📣 **일반 주사위 결과:** {char_name}의 {self.max_val}면체 주사위 최종 눈은 **{final_result}**입니다.{weight_str}"
            )
        else:
            # 임의 목표값이 부여된 성공/실패 판정 로직
            target_value = self.target_val + self.weight
            is_success = result <= target_value
            result_text = "성공 🟢" if is_success else "실패 🔴"

            weight_str = f" (가중치 {self.weight:+d} 적용)" if self.weight != 0 else ""
            target_str = f"{self.target_val}{self.weight:+d}={target_value}" if self.weight != 0 else f"{self.target_val}"

            await interaction.response.edit_message(
                content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 다이스 결과: **{result}** [목표값: {self.target_val}] 굴림  (기준치: {target_str})",
                view=None
            )

            if session:
                session.current_turn_logs.append(
                    f"[{char_name}]: 목표값 {self.target_val}{weight_str} 판정 (1~{self.max_val}) -> 주사위 {result} ({result_text})"
                )
                await save_session_data(self.bot, session)

            await interaction.channel.send(
                f"> 📣 **판정 결과:** {char_name}의 목표값 {self.target_val} 판정{weight_str} - **{result_text}**"
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
    def __init__(self, bot, target_uid: str, max_val: int, stat_name: str, stat_value: int, weight: int):
        super().__init__(timeout=None)
        self.bot = bot
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

        session = self.bot.active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)
            session.current_turn_logs.append(
                f"[{char_name}]: [{self.stat_name}]{weight_str} 판정 (1~{self.max_val}) -> 주사위 {result} ({result_text})")
            await save_session_data(self.bot, session)

        await interaction.channel.send(
            f"> 📣 **판정 결과:** {char_name}의 [{self.stat_name}] 판정{weight_str} - **{result_text}**"
        )
        return None