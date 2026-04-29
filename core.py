import os
import json
import asyncio
import random
import time
from datetime import datetime

import discord
from google.genai import types
from google.genai.errors import APIError

# ========== [전역 상수(Constants)] ==========
DEFAULT_MODEL = "gemini-3-flash-preview"
LOGIC_MODEL = "gemini-3-flash-preview"
# LOGIC_MODEL = "gemini-3-pro-preview"
EXCHANGE_RATE = 1500.0

TRPG_SAFETY_SETTINGS = [
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
    types.SafetySetting(
        category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
        threshold=types.HarmBlockThreshold.BLOCK_NONE,
    ),
]

# NOTE: API 사용 모델별 100만 토큰 당 단가표. 세션별 누적 과금액을 정밀하게 추적하기 위해 하드코딩된 기준 데이터.
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
    },
    "gemini-2.5-pro": {
        "INPUT": 1.25,
        "OUTPUT": 10.00,
        "CACHE_READ": 0.20,
        "CACHE_STORAGE_PER_HOUR": 4.50
    }
}

IMAGE_MODEL = "gemini-3.1-flash-image-preview"
IMAGE_GEN_COST = 200  # 1024x1024 해상도 1장 출력 고정 비용


# ========== [데이터 모델(Data Models)] ==========
class TRPGSession:
    """
    단일 TRPG 세션의 모든 상태와 데이터를 관리하는 데이터 모델 클래스.

    비동기 환경에서 데이터 파편화를 막기 위해 채널 메타데이터, 플레이어/NPC 상태, 자원, 로그 배열 등을
    하나의 캡슐화된 객체로 중앙 통제.

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
        self.note = ""

        self.cache_note = ""
        self.cache_created_at = 0.0
        self.cache_tokens = 0

        self.turn_count = 0
        self.is_started = False
        self.total_cost = 0.0

        self.volume = 0.3

        self.voice_client = None
        self.current_bgm = None
        self.is_bgm_looping = False

        self.is_processing = False
        self.last_turn_anchor_id = None

        self.gm_typing_task = None

        # 가장 최근 캐시 업로드 시점의 룰북 원본 텍스트 (패딩 제외). !캐시 출력 디버그용.
        self.cache_text = ""

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


# ========== [프롬프트 빌더(Prompt Builder)] ==========
class PromptBuilder:
    """
    TRPG 세션의 턴 진행을 위한 LLM 프롬프트를 단계별로 조립하는 빌더 클래스.

    할루시네이션을 통제하기 위해 '요약 -> 캐릭터 -> NPC 델타 -> 기억 -> 행동 -> 룰' 순서의
    엄격한 정보 주입 포맷을 시스템 레벨에서 강제.

    [NPC 주입 전략]
    모든 default_npcs는 캐시 룰북 [3. NPC 사전]에 전체 수록되므로 프롬프트에 중복 주입하지 않는다.
    프롬프트(add_npc_override_block)에는 아래 두 경우만 델타(차분)로 주입한다:
      1. !엔피씨 설정으로 details가 변경된 NPC  → 캐시 내용을 덮어씀
      2. 세션 중 resources / statuses가 부여된 NPC → 캐시에 없는 런타임 상태 동기화
    """

    def __init__(self, session: TRPGSession, gm_instruction: str):
        self.session = session
        self.gm_instruction = gm_instruction
        self.blocks = ["[현재 게임 상태]\n"]

        # keyword_memory 트리거 스캔에 사용되는 최근 로그 결합 문자열 (사전 연산)
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

    def add_npc_override_block(self):
        # NOTE: default_npcs 전체가 캐시에 수록되므로, 프롬프트에는 캐시와 차이가 있는
        # NPC만 델타(delta)로 주입한다. 이름 트리거 검사는 폐기.
        #
        # 주입 대상:
        #   A. details가 default_npcs와 다른 NPC (설정 변경 → 캐시 내용 덮어씀)
        #   B. details 변경 없이 resources / statuses만 존재하는 NPC (런타임 상태 동기화)
        #   → 두 조건 모두 해당 없는 NPC는 캐시로 충분하므로 스킵
        if not self.session.npcs:
            return self

        default_npcs = self.session.scenario_data.get("default_npcs", {})
        lines = []

        for npc_name, npc_data in self.session.npcs.items():
            base_details = default_npcs.get(npc_name, {}).get("details", "")
            details_changed = npc_data["details"] != base_details
            n_res = self.session.resources.get(npc_name, {})
            n_stat = self.session.statuses.get(npc_name, [])

            # 캐시 내용과 동일하고 런타임 상태도 없으면 주입 불필요
            if not details_changed and not n_res and not n_stat:
                continue

            entry = f"  - {npc_name}"
            if details_changed:
                entry += f" [설정 변경]: {npc_data['details']}"
            if n_res:
                entry += f"\n    * [확정 소지 자원]: {', '.join(f'{k}: {v}' for k, v in n_res.items())}"
            if n_stat:
                entry += f"\n    * [현재 상태이상]: {', '.join(n_stat)}"
            lines.append(entry)

        if lines:
            block = "\n▶ [NPC 변경사항 / 런타임 상태] (캐시 룰북 [3. NPC 사전]보다 우선 적용):\n"
            block += "\n".join(lines) + "\n"
            self.blocks.append(block)

        return self

    def add_keyword_memory_block(self):
        # NOTE: 특정 키워드가 최근 로그에 등장할 때만 연관 기억을 활성화하여 동적 컨텍스트 최적화 수행.
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

    def add_note_block(self):
        if getattr(self.session, "note", ""):
            block = f"\n▶ 실시간 노트 (GM 직접 관리):\n{self.session.note}\n"
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
        내부 블록 조립을 순차적으로 실행하여 완성된 문자열을 즉시 반환하는 파사드(Facade) 메서드.
        """
        return (cls(session, gm_instruction)
                .add_memory_block()
                .add_note_block()
                .add_player_block()
                .add_npc_override_block()
                .add_keyword_memory_block()
                .add_recent_action_block()
                .add_gm_instruction_block()
                .add_rule_enforcement_block()
                .build())


# ========== [비용 산출 및 포맷팅 유틸리티] ==========

def format_cost(cost_krw: float) -> str:
    """
    원화(KRW)로 환산된 비용을 소수점 셋째 자리에서 반올림하여 UI 출력용 포맷으로 변환.
    """
    return f"₩{cost_krw:.2f}"


def calculate_upload_cost(model_id: str, input_tokens=0, output_tokens=0, cached_read_tokens=0) -> float:
    """
    API 사용량을 기반으로 업로드 및 생성 과금액을 원화(KRW)로 산출.

    NOTE: 내부 데이터의 무결성을 위해 소수점 이하의 부동소수점 값을 반올림 없이 원형 그대로 반환.
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0

    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    actual_input_tokens = max(0, input_tokens - cached_read_tokens)

    cost_usd = 0.0
    cost_usd += (actual_input_tokens / 1_000_000) * rates["INPUT"]
    cost_usd += (output_tokens / 1_000_000) * rates["OUTPUT"]
    cost_usd += (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]

    return cost_usd * EXCHANGE_RATE


def calculate_storage_cost(model_id: str, cache_storage_tokens: int, duration_seconds: float) -> float:
    """
    캐시 보관 시간을 초 단위에서 분 단위로 반올림하여 스토리지 과금액을 원화(KRW)로 산출.
    """
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])

    # NOTE: 초 단위에서 분 단위로 반올림 (예: 15분 45초 -> 16분) 수행.
    storage_minutes = round(duration_seconds / 60.0)

    cost_usd = (cache_storage_tokens / 1_000_000) * (rates["CACHE_STORAGE_PER_HOUR"] / 60.0) * storage_minutes
    return cost_usd * EXCHANGE_RATE


async def process_cache_deletion(bot, session) -> float:
    """
    캐시 파기 시 보관 시간을 계산하여 정산하고 캐시 관련 메타데이터를 초기화.

    Returns:
        float: 정산된 보관 비용 (KRW)
    """
    storage_cost_krw = 0.0
    if session.cache_name and getattr(session, "cache_created_at", 0.0) > 0:
        duration_seconds = time.time() - session.cache_created_at
        # NOTE: 설정된 최대 캐시 유지 시간(6시간 = 21600초)을 초과한 과금 방지용 상한선(Cap) 적용.
        duration_seconds = min(duration_seconds, 21600.0)

        cache_tokens = getattr(session, "cache_tokens", 32768)

        # NOTE: AttributeError 방지를 위해 getattr를 사용하여 안전하게 접근하고 기본값(DEFAULT_MODEL) 할당.
        model_id = getattr(session, "cache_model", DEFAULT_MODEL)
        storage_cost_krw = calculate_storage_cost(model_id, cache_tokens, duration_seconds)
        session.total_cost += storage_cost_krw

    session.cache_name = None
    session.cache_obj = None

    # NOTE: 존재하지 않는 속성에 접근하여 발생하는 에러를 막기 위해 setattr 활용.
    setattr(session, "cache_model", None)
    session.cache_created_at = 0.0
    session.cache_tokens = 0

    await save_session_data(bot, session)
    return storage_cost_krw


# ========== [코어 유틸리티 함수(Utilities)] ==========
def calculate_cost(model_id: str, input_tokens=0, output_tokens=0, cached_read_tokens=0, cache_storage_tokens=0,
                   storage_hours=0) -> float:
    """
    API 사용량을 기반으로 과금액(USD) 산출.

    입력, 출력 토큰 외에도 캐시 유지 비용 및 할인율을 종합적으로 합산하여 재무적 모니터링 지원.

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
    지정된 시나리오 ID에 해당하는 JSON 파일을 파싱하여 딕셔너리로 반환.

    Args:
        scenario_id (str): 불러올 시나리오 파일의 이름 (확장자 제외)

    Returns:
        dict | None: 파싱된 시나리오 데이터 딕셔너리. 파일 부재 시 None 반환.
    """
    filepath = f"scenarios/{scenario_id}.json"
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def write_log(session_id: str, log_type: str, content: str):
    """
    세션별 행동 및 시스템 로그를 타임스탬프와 함께 로컬 텍스트 파일로 영구 저장.

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


def write_cost_log(session_id: str, usage_context: str, in_tokens: int, cached_tokens: int, out_tokens: int, cost: float, total_cost: float):
    """
    비용 발생 시점, 사용처, 토큰 수, 청구액을 기록하는 전용 비용 로거.
    """
    if not session_id:
        return
    log_filename = f"sessions/{session_id}/cost_log.txt"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_filename, "a", encoding="utf-8") as f:
        f.write(f"[{now_str}] [사용처: {usage_context}] 토큰: In({in_tokens}), Cached({cached_tokens}), Out({out_tokens}) | 발생 비용: ₩{cost:.2f} | 누적 비용: ₩{total_cost:.2f}\n")


def get_available_scenarios() -> list:
    """
    scenarios 폴더 내에 존재하는 사용 가능한 시나리오 파일 목록을 스캔하여 반환.

    Returns:
        list: '.json' 확장자가 제거된 파일명 문자열 리스트
    """
    return [f.replace(".json", "") for f in os.listdir("scenarios") if f.endswith(".json")]


async def save_session_data(bot, session: TRPGSession):
    """
    진행 중인 세션 객체의 상태를 JSON 파일로 디스크에 직렬화하여 저장.
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
            "note": getattr(session, "note", ""),
            "turn_count": session.turn_count,
            "is_started": getattr(session, "is_started", False),
            "total_cost": getattr(session, "total_cost", 0.0),
            "volume": getattr(session, "volume", 0.3),
            "cache_note": getattr(session, "cache_note", ""),
            "cache_created_at": getattr(session, "cache_created_at", 0.0),
            "cache_tokens": getattr(session, "cache_tokens", 0),
            "last_turn_anchor_id": getattr(session, "last_turn_anchor_id", None),
            "cache_text": getattr(session, "cache_text", "")
        }

        def write_file():
            with open(f"sessions/{session.session_id}/data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

        await asyncio.to_thread(write_file)


# noinspection PyShadowingNames
async def build_scenario_cache_text(bot, model_id, scenario_data: dict, cache_note: str = "", session_id: str = None) -> tuple[str, int]:
    """
    시나리오 데이터를 바탕으로 Context Caching을 위한 '시나리오 핵심 룰북' 텍스트 조립.
    최소 요구 토큰 수 미달 시 임의의 패딩 추가.

    Args:
        bot: 메인 봇 인스턴스
        model_id (str): 토큰 계산에 사용할 모델 식별자
        scenario_data (dict): 시나리오 데이터 딕셔너리
        cache_note (str): 캐시에 삽입할 GM 관리 기억사항
        session_id (str): 세션 아이디

    Returns:
        tuple[str, int]: 최종 완성된 룰북 텍스트와 해당 텍스트의 총 토큰 수
    """
    worldview = scenario_data.get('worldview', '특별한 세계관 정보 없음')
    story_guide = scenario_data.get('story_guide', '특별한 스토리 가이드 없음')
    stat_system = scenario_data.get('stat_system', '특별한 스탯 시스템 없음')
    desc_guide = scenario_data.get('desc_guide', '상황에 맞게 묘사하세요.')
    status_code_block = scenario_data.get('status_code_block', '상태창 코드블럭 양식 없음')
    note_injection = f"\n[추가 세계관 및 상태 (캐시 노트)]\n{cache_note}\n" if cache_note else ""

    npc_text = ""
    default_npcs = scenario_data.get("default_npcs", {})
    for npc_name, npc_data in default_npcs.items():
        if isinstance(npc_data, dict):
            details = npc_data.get("details", "")
            npc_text += f"\n- {npc_name}:\n{details}\n"

    if not npc_text:
        npc_text = "등록된 NPC 없음."

    rulebook_text = f"""=== [시나리오 핵심 룰북] ===
이 내용은 세션의 근간이 되는 절대적인 세계관 및 시스템 설정입니다.
진행자(GM)의 특별한 지시가 없는 한 아래의 설정을 완벽하게 유지하십시오.

[1. 세계관 정보]
{worldview}

[2. 스토리 진행 가이드]
{story_guide}

[3. NPC 사전 — 전체 등장인물 설정 (기준 데이터)]
이 목록이 모든 NPC의 기본 설정 원본이다.
게임 진행 중 프롬프트에 [NPC 변경사항 / 런타임 상태] 블록이 제공된 경우,
해당 NPC에 한해 아래 내용 대신 프롬프트 내 정보를 우선 적용할 것.
{npc_text}

[4. 게임 스탯 및 판정 시스템]
{stat_system}

[5. 시나리오 고유 묘사 가이드라인]
{desc_guide}

[6. 필수 출력: 상태창 코드블럭 양식]
(모든 묘사 후 턴의 마지막에 반드시 아래 양식을 바탕으로 상태창을 출력할 것)
{status_code_block}
{note_injection}============================
"""

    if session_id:
        write_log(session_id, "api", f"[캐시 발급용 원본 룰북 (패딩 제외)]\n{rulebook_text}")

    try:
        response = await asyncio.to_thread(
            bot.genai_client.models.count_tokens,
            model=model_id,
            contents=rulebook_text
        )
        base_tokens = response.total_tokens
        min_cache_tokens = 32768

        if base_tokens >= min_cache_tokens:
            # 패딩 불필요 — 원본과 업로드 텍스트가 동일
            return rulebook_text, base_tokens, rulebook_text

        # HACK: 제미나이 캐싱의 최소 요구 조건(32,768 토큰)을 강제 충족시키기 위해,
        # 시스템이 읽지 않도록 지시한 의미 없는 마침표(.) 배열을 덧붙이는 우회 기법 적용.
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
        # rulebook_text: 패딩 제외 원본 (디버그용 저장 대상)
        return padded_text, total_tokens, rulebook_text

    except Exception as e:
        print(f"⚠️ 토큰 계산 오류: {e}. 안전을 위해 임의의 대형 패딩을 적용합니다.")
        return rulebook_text + ("." * 150000), 38000, rulebook_text


def build_compression_prompt(session: TRPGSession, log_text: str) -> str:
    """
    대화 기록을 무손실 압축하기 위한 요약 전용 프롬프트 생성.

    NOTE: 장기 세션 진행 시 모델의 망각 및 개연성 붕괴를 막기 위해, 문학적 수사를 배제하고
    철저히 인과율과 수치(아이템, 턴 수) 중심의 개조식 데이터로 변환할 것을 AI에게 지시.

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
    봇 재시작 시 로컬 디스크에 저장된 모든 세션의 JSON 데이터를 읽어와 복구.
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
                session.note = data.get("note", "")
                session.cache_name = data.get("cache_name")
                session.current_turn_logs = data.get("current_turn_logs", [])
                session.turn_count = data.get("turn_count", 0)
                session.uncompressed_logs = data.get("uncompressed_logs", [])
                session.is_started = data.get("is_started", False)
                session.total_cost = data.get("total_cost", 0.0)
                session.volume = data.get("volume", 0.3)

                session.is_processing = False
                session.last_turn_anchor_id = data.get("last_turn_anchor_id", None)
                session.cache_text = data.get("cache_text", "")

                restored_raw_logs = []
                for item in data.get("raw_logs", []):
                    restored_raw_logs.append(
                        types.Content(role=item["role"], parts=[types.Part.from_text(text=item["text"])])
                    )
                session.raw_logs = restored_raw_logs

                if session.cache_name:
                    try:
                        session.cache_obj = await asyncio.to_thread(bot.genai_client.caches.get,
                                                                    name=session.cache_name)
                        print(f"✅ {session_id}: 기존 캐시 연동 성공.")
                    except APIError:
                        print(f"🔄 {session_id}: 기존 캐시 만료됨. 새로 발급합니다...")
                        caching_text, cache_tokens, base_text = await build_scenario_cache_text(bot, DEFAULT_MODEL,
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
                                ttl="21600s",
                            )
                        )

                        session.cache_obj = cache
                        session.cache_name = cache.name
                        session.cache_text = base_text
                        await save_session_data(bot, session)

                bot.active_sessions[session.game_ch_id] = session
                bot.active_sessions[session.master_ch_id] = session
                print(f"✅ 세션 {session_id} 복구 완료.")

            except Exception as e:
                print(f"⚠️ 세션 {session_id} 복구 중 오류: {e}")


def get_uid_by_char_name(session: TRPGSession, char_name: str) -> str | None:
    """
    캐릭터 이름 문자열을 통해 매핑된 디스코드 사용자 ID 탐색 및 반환.

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
    시나리오 데이터에 지정된 키워드와 파일 매핑을 참조하여 이미지를 게임 채널에 전송.

    NOTE: 경로 해킹(Path Traversal) 방지를 위해 절대경로 하드코딩 대신
    JSON 매핑 인덱스를 이용한 유효성 검증 수행.

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
async def generate_character_details(bot, scenario_data, char_type, char_name, instruction, session_id, recent_logs: str = "", npc_context: str = ""):
    """
    AI 모델을 사용하여 PC 또는 NPC의 세부 설정 초안 텍스트 생성.

    PC는 '외모 묘사' 전용 양식을 사용하며, 성격·배경·능력 등 내면 정보는 생성하지 않는다.
    NPC는 외모부터 내면 심리·관계망·비밀까지 포괄하는 종합 인물 프로파일을 생성한다.

    NOTE: 출력 일관성 및 재주입 토큰 효율 극대화를 위해 LOGIC_MODEL 호출.
    양식(template) 항목과 순서를 프롬프트 내에 직접 삽입하여 AI의 포맷 이탈을 원천 차단.

    Args:
        bot: 메인 봇 인스턴스
        scenario_data (dict): 기준이 될 시나리오 세계관 데이터
        char_type (str): 캐릭터의 종류 ('pc' 또는 'npc')
        char_name (str): 생성할 캐릭터의 이름
        instruction (str): GM이 추가로 부여한 세부 지시사항
        session_id (str): API 로그를 기록할 세션 식별자
        recent_logs (str): 최근 3턴 로그 (문맥 참고용)
        npc_context (str): 관련 NPC 설정 (관계 항목 연계용)

    Returns:
        types.GenerateContentResponse: API에서 반환한 응답 객체
    """
    worldview = scenario_data.get("worldview", "특별한 세계관 정보 없음")

    context_blocks = ""
    if recent_logs:
        context_blocks += f"[최근 상황 요약 (최근 3턴)]\n{recent_logs}\n\n"
    if npc_context:
        context_blocks += f"[참고용 기존 NPC 설정]\n{npc_context}\n\n"

    if char_type == "pc":
        # PC 설정 생성은 '외모 묘사'만을 목적으로 한다.
        # 출력 결과는 session.players[uid]['appearance']에 저장되어
        # 프롬프트의 [외형]: {appearance} 필드에 직접 주입된다.
        prompt = (
            "당신은 TRPG 플레이어 캐릭터(PC)의 외양(外樣)을 구체화하는 설정 작가입니다.\n"
            "임무: 오직 캐릭터의 외모만 묘사할 것. 성격·배경·능력·내면 정보는 절대 포함하지 않는다.\n\n"
            "[작성 원칙]\n"
            "1. 문체: 서술어를 최소화한 명사형·개조식 단문. 각 항목은 한 줄을 초과하지 않는다.\n"
            "2. 분량: 양식 레이블 제외 실제 내용 기준 공백 포함 130~230자 내외.\n"
            "3. 세계관 밀착: 복장과 외모 상태는 세계관의 복식과 상황을 반영할 것\n"
            "   (예: 피로와 오염이 배인 피부, 실용적이고 낡은 생존 복장 등).\n"
            "4. 양식 엄수: 아래 [출력 양식]의 항목명·순서를 반드시 그대로 출력할 것.\n"
            "   항목 추가·삭제·순서 변경·항목명 수정 불가. GM 지시사항은 각 항목 값에 녹여낼 것.\n\n"
            f"[세계관 정보]\n{worldview}\n\n"
            f"{context_blocks}"
            f"[대상 캐릭터]\n"
            f"이름: {char_name}\n"
            f"GM 지시사항: {instruction}\n\n"
            "[출력 양식 — 아래 항목명과 순서를 그대로 사용하여 값만 채워 출력할 것]\n"
            "**나이/성별**: \n"
            "**체형**: \n"
            "**얼굴**: \n"
            "**피부·헤어**: \n"
            "**복장**: \n"
            "**첫인상**: "
        )
    else:
        # NPC 설정 생성은 AI GM이 인물을 직접 연기하는 데 필요한 모든 요소를 포함한다.
        # 출력 결과는 scenario_data['default_npcs'][name]['details'] 또는
        # session.npcs[name]['details']에 저장되어 캐시 룰북 또는 프롬프트에 주입된다.
        prompt = (
            "당신은 TRPG 논플레이어 캐릭터(NPC)의 종합 인물 프로파일을 작성하는 설정 작가입니다.\n"
            "임무: 외모부터 내면 심리·관계망·비밀까지 포괄하는 완결된 NPC 프로파일을 생성할 것.\n\n"
            "[작성 원칙]\n"
            "1. 문체: 서술어를 최소화한 명사형·개조식 단문.\n"
            "2. 분량: 양식 레이블 제외 실제 내용 기준 공백 포함 350~500자 내외.\n"
            "3. 세계관 밀착: 모든 항목은 세계관에 명시된 생활상과 환경에 근거할 것.\n"
            "4. 내면 입체성: '동기', '두려움·약점', '비밀' 항목은 단순 묘사가 아닌\n"
            "   플레이 중 활용 가능한 갈등 씨앗(conflict seed)으로 작성할 것.\n"
            "5. 말투 명시: AI GM이 NPC를 직접 연기할 때 즉각 참고할 수 있는\n"
            "   구체적 어조·화법 특징을 기술할 것.\n"
            "6. 양식 엄수: 아래 [출력 양식]의 항목명·순서를 반드시 그대로, 총 500자 이내로 출력할 것.\n"
            "   항목 추가·삭제·순서 변경·항목명 수정 불가. GM 지시사항은 각 항목 값에 녹여낼 것.\n\n"
            f"[세계관 정보]\n{worldview}\n\n"
            f"{context_blocks}"
            f"[대상 캐릭터]\n"
            f"이름: {char_name}\n"
            f"GM 지시사항: {instruction}\n\n"
            "[출력 양식 — 아래 항목명과 순서를 그대로 사용하여 값만 채워 출력할 것]\n"
            "**나이·성별**: \n"
            "**소속·직책**: \n"
            "**외모**: \n"
            "**핵심 기질**: \n"
            "**행동 방식**: \n"
            "**말투·어조**: \n"
            "**동기·욕구**: \n"
            "**두려움·약점**: \n"
            "**비밀**: \n"
            "**신뢰·의존**: \n"
            "**경계·반목**: \n"
            "**특기·능력**: "
        )

    write_log(session_id, "api", f"[{char_type.upper()} 설정 생성 요청 - {char_name}]\n{prompt}")

    response = await asyncio.to_thread(
        bot.genai_client.models.generate_content,
        model=LOGIC_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(safety_settings=TRPG_SAFETY_SETTINGS)
    )
    return response


async def stream_text_to_channel(bot, channel, text: str, words_per_tick: int = 10, tick_interval: float = 1.5):
    """
    디스코드 채널에 텍스트를 문단과 단어 단위로 쪼개어 타이핑 치듯 스트리밍 연출.

    NOTE: 한 번에 방대한 텍스트가 출력되는 것을 막아 TRPG 특유의 시각적 긴장감을 조성하고,
    디스코드 API의 메시지 전송 제한(Rate Limit)을 우회하기 위한 비동기 sleep 로직 적용.

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
            write_log(session.session_id, "game_chat", f"[GM]: {final_text}")


class PlaylistManager:
    """
    음성 채널에서의 플레이리스트 셔플 재생 상태 및 백그라운드 루프를 관리하는 클래스.

    NOTE: 메인 TRPG 봇 로직과 스레드를 철저히 분리하여, 플레이리스트 연산이
    주사위 판정이나 AI 텍스트 생성 속도에 영향을 미치지 않도록 설계.

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
        self.volume = 0.3
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

                ffmpeg_options = {'options': '-vn -sn -ar 48000 -ac 2'}
                source = discord.FFmpegPCMAudio(filepath, **ffmpeg_options)
                volume_source = discord.PCMVolumeTransformer(source, volume=self.volume)
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


# ========== [채널 관리 UI 및 유틸리티] ==========

def _cleanup_session_memory(bot, channel_id: int):
    """
    삭제되는 채널이 현재 활성화된 세션에 포함되어 있을 경우
    메모리 참조 에러를 방지하기 위해 딕셔너리에서 데이터를 안전하게 해제.

    WARNING: 채널 삭제 시 파이썬 가비지 컬렉터가 객체를 온전히 수거할 수 있도록
    반드시 메모리 참조를 끊어주는 메모리 누수(Memory Leak) 방지 로직.
    """
    if channel_id in bot.active_sessions:
        session = bot.active_sessions.pop(channel_id)
        other_id = session.game_ch_id if channel_id == session.master_ch_id else session.master_ch_id
        if other_id in bot.active_sessions and bot.active_sessions[other_id] == session:
            bot.active_sessions.pop(other_id)


class ChannelSelect(discord.ui.Select):
    """
    채널 삭제 대상 선택을 위한 드롭다운 UI 컴포넌트 클래스.
    """

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
    """
    필터링된 더미 세션 채널 및 카테고리의 일괄 삭제를 돕는 UI 뷰어 클래스.

    NOTE: 디스코드 API의 SelectOption 최대 개수 제한으로 인해 노출되는 항목을
    최대 25개로 슬라이싱하여 안정성 확보.
    """

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


# ========== [디스코드 UI 클래스(Views)] ==========
class GeneralDiceView(discord.ui.View):
    """
    능력치에 구애받지 않는 일반 주사위(N면체) 및 임의 목표값 판정을 위한 UI 뷰어.

    NOTE: 버튼 클릭 시 판정 결과가 단순히 채팅으로 출력되는 것에 그치지 않고,
    session.current_turn_logs에 직렬화되어 AI의 다음 턴 프롬프트에 자동으로 연동됨.

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
    특정 스탯의 기준치를 기반으로 성공/실패를 판정하는 능력치 주사위 UI 뷰어.

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