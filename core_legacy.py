import os
import re
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
    },
    # NOTE: Nano Banana 2 (gemini-3.1-flash-image-preview) — 이미지 출력 토큰 단가가 텍스트 출력 단가와 별개로 책정됨.
    # 입력(텍스트/이미지) $0.50 / 1M, 출력 텍스트(thinking) $3.00 / 1M, 출력 이미지 $60.00 / 1M.
    "gemini-3.1-flash-image-preview": {
        "INPUT": 0.50,
        "OUTPUT": 3.00,
        "OUTPUT_IMAGE": 60.00,
        "CACHE_READ": 0.05,
        "CACHE_STORAGE_PER_HOUR": 1.00
    }
}

IMAGE_MODEL = "gemini-3.1-flash-image-preview"

# NOTE: Gemini 이미지 출력 모델의 해상도별 출력 토큰 표 (공식 가격 페이지 기준).
#       응답에서 usage_metadata가 비었을 때의 폴백 추산값으로 사용한다.
IMAGE_OUTPUT_TOKENS_BY_RES = {
    "0.5K": 747,    # 512px ≈ $0.045
    "1K":   1120,   # 1024x1024 ≈ $0.067
    "2K":   1680,   # 2048x2048 ≈ $0.101
    "4K":   2520,   # 4096x4096 ≈ $0.151
}


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

        # ========== [자동 GM 서사 계획] ==========
        # NOTE: 자동 GM 전용. 사건(event) 단위의 서사 계획을 저장한다.
        # 구조: {"current_event": {...}, "next_event": {...}, "plan_version": int, "last_planned_turn": int}
        # !자동시작 시 수립, PROCEED 완료 후 completed/deviated 평가 시 재수립.
        self.narrative_plan = {}

        # 가장 최근 캐시 재발급 시점의 세션 생성 NPC 스냅샷.
        # 이후 변경분만 delta로 주입하기 위한 기준 데이터. (재발급 전 없으면 빈 딕셔너리)
        self.cached_session_npcs = {}
        # 가장 최근 캐시 재발급 시점까지 누적된 압축 기억 (캐시 섹션 [9]에 포함됨).
        # 프롬프트에서는 이미 캐시에 있으므로 중복 주입하지 않는다.
        self.cached_compressed_memory = ""

        # ========== [자동 GM 모드 상태] ==========
        # NOTE: 자동 GM 모드는 게임 채널의 플레이어 발언을 받아 AI가 GM 역할을 수행하는 옵트인 모드.
        #       기본은 비활성(False) — 활성화되어야만 on_message 리스너가 동작한다.
        self.auto_gm_active = False
        self.auto_gm_target_char = None        # 자동 GM이 대화할 PC 이름 (단일, 하위 호환)
        self.auto_gm_turn_cap = 10             # 자동 모드에서 자동 진행할 최대 턴 수 (안전장치)
        self.auto_gm_turns_done = 0            # 활성화 이후 자동으로 처리한 턴 수
        self.auto_gm_clarify_count = 0         # 같은 플레이어 발언에 대한 명확화 누적 횟수
        self.auto_gm_narrate_count = 0         # 같은 플레이어 발언에 대한 NARRATE 누적 횟수
        self.auto_gm_cost_cap_krw = 500.0      # 자동 모드 누적 비용 상한 (도달 시 정지)
        self.auto_gm_cost_baseline = 0.0       # 활성화 시점의 session.total_cost (사용량 추적용)
        self.auto_gm_side_note = ""            # !자동개입으로 주입된 GM 사이드 노트 (다음 호출에 1회 합류 후 비움)
        self.auto_gm_lock = False              # 동시 처리 방지용 락 (직렬화 시 무시)

        # ========== [멀티플레이어 자동진행 상태 (#22)] ==========
        # NOTE: PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 순서대로 물어보는 라운드 수집 시스템.
        self.auto_gm_target_chars = []         # 자동진행 대상 PC 이름 전체 목록 (멀티 지원)
        self.auto_gm_pending_players = []      # 현재 라운드에서 아직 행동 선언 안 한 PC 목록
        self.auto_gm_collected_actions = {}    # 이번 라운드에 수집된 행동 {char_name: text}
        self.auto_gm_waiting_for = None        # 현재 발언을 기다리는 PC 이름 (None이면 대기 없음)

        self.npcs = {}
        default_npcs = scenario_data.get("default_npcs", {})
        npc_template = scenario_data.get("npc_template", {})
        _npc_info_fields = npc_template.get("info_fields", []) if isinstance(npc_template, dict) else []

        for npc_name, npc_data in default_npcs.items():
            if isinstance(npc_data, dict):
                # 전체 NPC 항목을 복사 (구조화 필드 + 하위 호환 details 모두 보존)
                npc_entry = {k: v for k, v in npc_data.items() if k != "resources" and k != "statuses"}
                npc_entry["name"] = npc_data.get("name", npc_name)
                self.npcs[npc_name] = npc_entry

                # NPC 기본값 resources/statuses → 런타임 딕셔너리에 사전 적용
                # (태그·!증감이 이 값을 기준으로 증감하도록)
                default_res = npc_data.get("resources", {})
                if default_res:
                    self.resources.setdefault(npc_name, {})
                    self.resources[npc_name].update(default_res)
                default_stat = npc_data.get("statuses", [])
                if default_stat:
                    self.statuses.setdefault(npc_name, [])
                    for s in default_stat:
                        if s not in self.statuses[npc_name]:
                            self.statuses[npc_name].append(s)
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
        # NOTE: cached_compressed_memory는 캐시 섹션 [9]에 포함되어 있으므로 프롬프트에 중복 주입하지 않는다.
        # compressed_memory는 마지막 캐시 재발급 이후 새로 누적된 기억만 포함한다.
        if self.session.compressed_memory:
            self.blocks.append(f"▶ 이전 상황 요약 (최근 압축 기억 — 절대 참조용):\n{self.session.compressed_memory}\n")
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
                # NOTE: 상태이상이 없을 때도 명시적으로 "없음"을 출력하여,
                # 해제 후에도 AI가 이전 상태를 언급하는 오류를 방지한다.
                stat_str = ", ".join(c_stat) if c_stat else "없음"
                block += f"    * [현재 상태이상]: {stat_str}\n"
            self.blocks.append(block)
        return self

    def add_npc_override_block(self):
        # NOTE: default_npcs 전체가 캐시에 수록되므로, 프롬프트에는 캐시와 차이가 있는
        # NPC만 델타(delta)로 주입한다.
        #
        # 주입 대상:
        #   A. 설정 필드(details 또는 구조화 info_fields)가 default_npcs와 다른 NPC
        #      → 캐시 내용을 현재 값으로 덮어씀
        #   B. 설정 변경 없이 resources / statuses(runtime)만 존재하는 NPC
        #      → 캐시에 없는 런타임 상태 동기화
        #   → 두 조건 모두 해당 없는 NPC는 캐시로 충분하므로 스킵
        if not self.session.npcs:
            return self

        default_npcs = self.session.scenario_data.get("default_npcs", {})
        npc_template = self.session.scenario_data.get("npc_template", {})
        info_fields = npc_template.get("info_fields", []) if isinstance(npc_template, dict) else []
        # NOTE: NPC 스탯은 PC와 동일한 ability_stats 스키마를 사용한다.
        # has_stats=true이고 ability_stats가 있을 때만 스탯 오버라이드를 처리한다.
        ability_stats_list = self.session.scenario_data.get("ability_stats", [])
        npc_has_stats = (
            isinstance(npc_template, dict)
            and npc_template.get("has_stats")
            and bool(ability_stats_list)
        )
        # 가장 최근 캐시 재발급 시점의 세션 생성 NPC 스냅샷
        cached_session_npcs = getattr(self.session, "cached_session_npcs", {})
        lines = []

        for npc_name, npc_data in self.session.npcs.items():
            is_session_npc = npc_name not in default_npcs  # 세션에서 새로 생성된 NPC
            if is_session_npc:
                # 캐시에 등록된 세션 NPC라면 해당 스냅샷을 기준 데이터로 사용 → delta만 주입
                # 캐시에 없는 경우(재발급 전 생성) base_data = {} → 전체 프로파일 주입
                base_data = cached_session_npcs.get(npc_name, {})
                is_npc_in_cache = npc_name in cached_session_npcs
            else:
                base_data = default_npcs.get(npc_name, {})
                is_npc_in_cache = True  # default NPC는 항상 캐시에 있음

            # ── info_fields 변경 필드 추출 ──
            # 디폴트 NPC: 실제로 달라진 필드(delta)만 추출 → 토큰 절약
            # 세션 생성 NPC: base_data = {} 이므로 값 있는 필드 전체가 delta
            if info_fields:
                changed_info_fields = [
                    f for f in info_fields
                    if npc_data.get(f) != base_data.get(f)
                ]
                info_changed = bool(changed_info_fields)
            else:
                # 레거시 NPC: details 문자열 비교 (전체 교체)
                info_changed = npc_data.get("details", "") != base_data.get("details", "")
                changed_info_fields = []

            # ── 스탯 변경 감지 (ability_stats 스키마) ──
            n_stats = npc_data.get("stats", {})
            base_stats = base_data.get("stats", {})
            # 변경된 스탯 항목만 추출 (delta)
            if npc_has_stats:
                changed_stats = {
                    k: n_stats[k] for k in n_stats
                    if n_stats.get(k) != base_stats.get(k)
                }
                # base에는 있었지만 현재 없는 스탯은 빈 값으로 표시
                removed_stats = {k: "" for k in base_stats if k not in n_stats}
                delta_stats = {**changed_stats, **removed_stats}
            else:
                delta_stats = {}
            stats_changed = bool(delta_stats)

            # ── 런타임 resources/statuses 감지 ──
            n_res = self.session.resources.get(npc_name, {})
            n_stat = self.session.statuses.get(npc_name, [])
            base_res = base_data.get("resources", {})
            base_statuses = base_data.get("statuses", [])
            res_changed = n_res != base_res
            stat_changed = sorted(n_stat) != sorted(base_statuses)

            if not info_changed and not stats_changed and not res_changed and not stat_changed:
                continue

            # ── 오버라이드 블록 구성 ──
            entry = f"  - {npc_name}"

            if info_changed:
                if info_fields:
                    if is_session_npc and not is_npc_in_cache:
                        # 아직 캐시에 없는 세션 생성 NPC: 전체 프로파일 출력
                        field_lines = "\n".join(
                            f"    {f}: {npc_data.get(f, '')}"
                            for f in info_fields if npc_data.get(f)
                        )
                        entry += f" [전체 프로파일]:\n{field_lines}"
                    else:
                        # 캐시된 NPC(디폴트 또는 이전 재발급에서 캐시된 세션 NPC): delta만 출력
                        field_lines = "\n".join(
                            f"    {f}: {npc_data.get(f, '')}"
                            for f in changed_info_fields
                        )
                        entry += f" [필드 수정 — 이하 항목만 캐시 내용 대신 적용]:\n{field_lines}"
                else:
                    # 레거시 details: 전체 교체
                    entry += f" [설정 변경]: {npc_data.get('details', '')}"

            # 스탯 delta 출력: ability_stats 순서 보장
            if stats_changed:
                ordered = [(s, delta_stats[s]) for s in ability_stats_list if s in delta_stats]
                extra = [(k, v) for k, v in delta_stats.items() if k not in ability_stats_list]
                all_stats = ordered + extra
                label = "[스탯 (전체)]" if (is_session_npc and not is_npc_in_cache) else "[스탯 수정]"
                entry += f"\n    * {label}: {', '.join(f'{k}={v}' for k, v in all_stats)}"

            if res_changed and n_res:
                entry += f"\n    * [확정 소지 자원]: {', '.join(f'{k}: {v}' for k, v in n_res.items())}"
            if stat_changed and n_stat:
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

        # 인물 대사 출력 형식 지시 (디스코드 후처리에서 자동으로 인물 헤더+이미지+말풍선으로 변환됨)
        block += (
            "▶ 명령 (인물 대사 마크업): 등장 인물이 직접 발화하는 대사는 반드시 별도의 문단으로 분리하고, "
            "다음 형식의 단일 라인으로만 출력하십시오:\n"
            "@대사:인물이름|대사 본문\n"
            "예) @대사:레비|…어때? 감각이 느껴져? 체온은 정상인데, 불편한 곳은?\n"
            "    @대사:김철수|결론만 말해. 수치로.\n"
            "이 마커가 포함된 문단은 시스템이 자동으로 인물 헤더와 말풍선 박스, 그리고 미디어 목록에 등록된 인물 이미지(존재 시)로 변환합니다. "
            "마커 외 추가 묘사(행동, 시선 등)는 같은 문단에 섞지 말고, 직전 또는 직후의 일반 문단으로 분리하여 작성하십시오. "
            "여러 인물이 연이어 발화하는 경우 각 발화마다 마커 한 줄씩 별도 문단으로 작성합니다.\n"
        )

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


def calculate_text_gen_cost_breakdown(model_id: str, input_tokens: int = 0, output_tokens: int = 0,
                                       cached_read_tokens: int = 0) -> dict:
    """
    텍스트 생성 모델 호출 비용을 항목별로 분해하여 KRW로 반환.

    캐시 적중분과 신규 입력분의 단가가 다르고(예: $0.50 vs $0.05/1M), 출력 단가($3/1M)와도
    분리 보고해야 GM이 어디서 비용이 새는지 즉시 진단할 수 있다.

    Args:
        model_id (str): 사용된 모델 식별자
        input_tokens (int): 응답 메타의 prompt_token_count (캐시 적중분 포함)
        output_tokens (int): candidates_token_count
        cached_read_tokens (int): cached_content_token_count (캐시에서 읽혀 할인된 분)

    Returns:
        dict: {
            input_billable_tokens, input_krw,         # 신규 입력분 (단가 $INPUT)
            cache_read_tokens, cache_read_krw,        # 캐시 적중분 (단가 $CACHE_READ)
            output_tokens, output_krw,                # 출력분 (단가 $OUTPUT)
            total_krw, total_usd,
            input_rate, cache_rate, output_rate       # 단가 (USD/1M, 보고용)
        }
    """
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0

    billable_input = max(0, input_tokens - cached_read_tokens)

    input_usd = (billable_input / 1_000_000) * rates["INPUT"]
    cache_usd = (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]
    output_usd = (output_tokens / 1_000_000) * rates["OUTPUT"]
    total_usd = input_usd + cache_usd + output_usd

    return {
        "input_billable_tokens": billable_input,
        "input_krw": input_usd * EXCHANGE_RATE,
        "cache_read_tokens": cached_read_tokens,
        "cache_read_krw": cache_usd * EXCHANGE_RATE,
        "output_tokens": output_tokens,
        "output_krw": output_usd * EXCHANGE_RATE,
        "total_krw": total_usd * EXCHANGE_RATE,
        "total_usd": total_usd,
        "input_rate": rates["INPUT"],
        "cache_rate": rates["CACHE_READ"],
        "output_rate": rates["OUTPUT"],
    }


def calculate_image_gen_cost(model_id: str, prompt_tokens: int = 0, image_output_tokens: int = 0,
                              text_output_tokens: int = 0) -> dict:
    """
    이미지 생성 모델(예: gemini-3.1-flash-image-preview)의 호출 비용을 항목별로 산출하여 KRW로 반환.

    이미지 출력 토큰과 텍스트 출력 토큰의 단가가 다르므로(이미지 $60/1M, 텍스트 $3/1M),
    별도 항목으로 분리 정산하여 모니터링 정확도를 확보한다.

    Args:
        model_id (str): 사용된 이미지 모델 식별자
        prompt_tokens (int): 입력 프롬프트(텍스트+레퍼런스 이미지) 토큰 수
        image_output_tokens (int): 출력된 이미지의 토큰 수 (해상도에 따라 결정됨)
        text_output_tokens (int): 응답에 포함된 텍스트(thinking 포함) 토큰 수

    Returns:
        dict: {input_krw, image_krw, text_krw, total_krw, total_usd} 형태의 분해 비용
    """
    rates = PRICING_1M.get(model_id, PRICING_1M.get(IMAGE_MODEL))

    input_usd = (max(0, prompt_tokens) / 1_000_000) * rates["INPUT"]
    image_usd = (max(0, image_output_tokens) / 1_000_000) * rates.get("OUTPUT_IMAGE", rates["OUTPUT"])
    text_usd = (max(0, text_output_tokens) / 1_000_000) * rates["OUTPUT"]
    total_usd = input_usd + image_usd + text_usd

    return {
        "input_krw": input_usd * EXCHANGE_RATE,
        "image_krw": image_usd * EXCHANGE_RATE,
        "text_krw": text_usd * EXCHANGE_RATE,
        "total_krw": total_usd * EXCHANGE_RATE,
        "total_usd": total_usd,
    }


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
            "cache_model": getattr(session, "cache_model", DEFAULT_MODEL),
            "last_turn_anchor_id": getattr(session, "last_turn_anchor_id", None),
            "cache_text": getattr(session, "cache_text", ""),
            "cached_session_npcs": getattr(session, "cached_session_npcs", {}),
            "cached_compressed_memory": getattr(session, "cached_compressed_memory", ""),
            "narrative_plan": getattr(session, "narrative_plan", {}),

            # 자동 GM 모드 상태 (auto_gm_lock은 런타임 전용이라 직렬화하지 않음)
            "auto_gm_active": getattr(session, "auto_gm_active", False),
            "auto_gm_target_char": getattr(session, "auto_gm_target_char", None),
            "auto_gm_turn_cap": getattr(session, "auto_gm_turn_cap", 10),
            "auto_gm_turns_done": getattr(session, "auto_gm_turns_done", 0),
            "auto_gm_clarify_count": getattr(session, "auto_gm_clarify_count", 0),
            "auto_gm_narrate_count": getattr(session, "auto_gm_narrate_count", 0),
            "auto_gm_cost_cap_krw": getattr(session, "auto_gm_cost_cap_krw", 500.0),
            "auto_gm_cost_baseline": getattr(session, "auto_gm_cost_baseline", 0.0),
            "auto_gm_side_note": getattr(session, "auto_gm_side_note", ""),
            # 멀티플레이어 수집 상태 (#22) — 재시작 시 라운드는 초기화
            "auto_gm_target_chars": getattr(session, "auto_gm_target_chars", []),
            "auto_gm_pending_players": getattr(session, "auto_gm_pending_players", []),
            "auto_gm_collected_actions": getattr(session, "auto_gm_collected_actions", {}),
            "auto_gm_waiting_for": getattr(session, "auto_gm_waiting_for", None)
        }

        def write_file():
            with open(f"sessions/{session.session_id}/data.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

        await asyncio.to_thread(write_file)


def update_session_cache_state(session: TRPGSession):
    """
    캐시 재발급(또는 신규 발급) 완료 직후 호출하여 세션 캐시 연동 상태를 동기화한다.

    1. 세션 생성 NPC 스냅샷(`cached_session_npcs`)을 현재 시점으로 업데이트.
       → 이후 변경분만 delta로 오버라이드 주입되도록 기준 데이터 고정.
       → resources/statuses 런타임 값도 함께 저장해 delta 비교가 정확하게 이루어지도록 함.
    2. 압축 기억(`compressed_memory`)을 캐시로 이동.
       → `cached_compressed_memory`에 누적 후 `compressed_memory` 초기화.
       → 이후 프롬프트 `add_memory_block`에는 캐시 재발급 이후 새로 생긴 기억만 주입된다.
    """
    default_npcs = session.scenario_data.get("default_npcs", {})
    session.cached_session_npcs = {
        name: {
            **dict(data),
            "resources": dict(session.resources.get(name, {})),
            "statuses": list(session.statuses.get(name, []))
        }
        for name, data in session.npcs.items()
        if name not in default_npcs
    }

    old_cached = getattr(session, "cached_compressed_memory", "")
    new_chunk = session.compressed_memory
    if old_cached and new_chunk:
        session.cached_compressed_memory = old_cached + "\n" + new_chunk
    else:
        session.cached_compressed_memory = old_cached or new_chunk
    session.compressed_memory = ""  # 캐시로 이동됨 — 프롬프트에서 중복 주입 방지


# noinspection PyShadowingNames
async def build_scenario_cache_text(bot, model_id, scenario_data: dict, cache_note: str = "", session_id: str = None, session: "TRPGSession" = None) -> tuple[str, int, str]:
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

    # ── NPC 사전 텍스트 조립 (구조화 필드 지원) ──
    npc_text = ""
    default_npcs = scenario_data.get("default_npcs", {})
    npc_template = scenario_data.get("npc_template", {})
    npc_info_fields = npc_template.get("info_fields", []) if isinstance(npc_template, dict) else []

    for npc_name, npc_data in default_npcs.items():
        if not isinstance(npc_data, dict):
            npc_text += f"\n- {npc_name}: {npc_data}\n"
            continue
        if npc_info_fields:
            # 구조화 양식: info_fields 순서대로 필드 렌더링
            field_lines = []
            for f in npc_info_fields:
                val = npc_data.get(f, "")
                if val:
                    field_lines.append(f"  {f}: {val}")
            # NOTE: has_stats / has_resources / has_statuses 플래그에 따라
            # NPC 기본값(stats/resources/statuses)을 캐시 룰북에 포함한다.
            # 이 값들은 세션 초기화 시 session.resources/statuses에 자동 반영되며,
            # 런타임 변경분은 프롬프트의 [NPC 변경사항 / 런타임 상태] 오버라이드 블록이 담당한다.
            if isinstance(npc_template, dict) and npc_template.get("has_stats"):
                stats = npc_data.get("stats", {})
                if stats:
                    field_lines.append(f"  스탯: {', '.join(f'{k}={v}' for k, v in stats.items())}")
            if isinstance(npc_template, dict) and npc_template.get("has_resources"):
                resources = npc_data.get("resources", {})
                if resources:
                    field_lines.append(f"  기본 자원: {', '.join(f'{k}: {v}' for k, v in resources.items())}")
            if isinstance(npc_template, dict) and npc_template.get("has_statuses"):
                statuses = npc_data.get("statuses", [])
                if statuses:
                    field_lines.append(f"  기본 상태: {', '.join(statuses)}")
            npc_text += f"\n- {npc_name}:\n" + "\n".join(field_lines) + "\n"
        else:
            # 레거시 양식: details 문자열
            details = npc_data.get("details", "")
            npc_text += f"\n- {npc_name}:\n{details}\n"

    if not npc_text:
        npc_text = "등록된 NPC 없음."

    # ── 금지사항 섹션 ──
    prohibitions_raw = scenario_data.get("prohibitions", [])
    if isinstance(prohibitions_raw, list) and prohibitions_raw:
        prohibitions_text = "\n".join(f"- {item}" for item in prohibitions_raw)
    elif isinstance(prohibitions_raw, str) and prohibitions_raw.strip():
        prohibitions_text = prohibitions_raw.strip()
    else:
        prohibitions_text = None

    prohibitions_section = ""
    if prohibitions_text:
        prohibitions_section = f"""
[6. GM 절대 금지 사항]
(아래 요소들은 플레이어나 GM의 요청이 있더라도 예외 없이 준수한다. 이를 위반하는 묘사는 즉시 수정한다.)
{prohibitions_text}
"""

    # ── 세션 생성 NPC 섹션 [8] (session 인자 있을 때만 조립) ──
    session_npc_section = ""
    if session is not None:
        default_npcs_set = set(default_npcs.keys())
        session_npc_lines = []
        for npc_name, npc_data in session.npcs.items():
            if npc_name in default_npcs_set:
                continue
            if not isinstance(npc_data, dict):
                session_npc_lines.append(f"\n- {npc_name}: {npc_data}\n")
                continue
            if npc_info_fields:
                field_lines = []
                for f in npc_info_fields:
                    val = npc_data.get(f, "")
                    if val:
                        field_lines.append(f"  {f}: {val}")
                # has_stats / has_resources / has_statuses 처리 (default NPC와 동일한 방식)
                if isinstance(npc_template, dict) and npc_template.get("has_stats"):
                    stats = npc_data.get("stats", {})
                    if stats:
                        field_lines.append(f"  스탯: {', '.join(f'{k}={v}' for k, v in stats.items())}")
                if isinstance(npc_template, dict) and npc_template.get("has_resources"):
                    # 런타임 자원 값을 반영 (session.resources 우선)
                    res_val = session.resources.get(npc_name, npc_data.get("resources", {}))
                    if res_val:
                        field_lines.append(f"  기본 자원: {', '.join(f'{k}: {v}' for k, v in res_val.items())}")
                if isinstance(npc_template, dict) and npc_template.get("has_statuses"):
                    stat_val = session.statuses.get(npc_name, npc_data.get("statuses", []))
                    if stat_val:
                        field_lines.append(f"  기본 상태: {', '.join(stat_val)}")
                session_npc_lines.append(f"\n- {npc_name}:\n" + "\n".join(field_lines) + "\n")
            else:
                details = npc_data.get("details", "")
                session_npc_lines.append(f"\n- {npc_name}:\n{details}\n")

        if session_npc_lines:
            session_npc_text = "".join(session_npc_lines)
            session_npc_section = f"""
[8. 세션 진행 중 추가된 NPC]
이 목록은 시나리오 외 세션 중 새로 생성된 NPC의 설정 원본이다.
[3. NPC 사전]과 동일하게, 프롬프트에 [NPC 변경사항 / 런타임 상태] 블록이 제공된 경우
해당 NPC에 한해 아래 내용 대신 프롬프트 내 정보를 우선 적용할 것.
{session_npc_text}
"""

    # ── 세션 기억 압축 섹션 [9] (session 인자 있을 때만 조립) ──
    memory_section = ""
    if session is not None:
        # cached_compressed_memory: 이전 재발급 시점까지의 기억
        # compressed_memory: 마지막 재발급 이후 새로 누적된 기억
        old_cached_mem = getattr(session, "cached_compressed_memory", "")
        new_mem = session.compressed_memory
        if old_cached_mem and new_mem:
            full_memory = old_cached_mem + "\n" + new_mem
        else:
            full_memory = old_cached_mem or new_mem
        if full_memory:
            memory_section = f"""
[9. 세션 진행 기억 — 과거 턴 압축 요약]
(아래는 현재 세션에서 발생한 사건의 압축 요약이다. 과거 맥락 파악 및 일관성 유지에 참조할 것.)
{full_memory}
"""

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
{prohibitions_section}
[7. 필수 출력: 상태창 코드블럭 양식]
(모든 묘사 후 턴의 마지막에 반드시 아래 양식을 바탕으로 상태창을 출력할 것)
{status_code_block}
{session_npc_section}{memory_section}{note_injection}============================
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

    NOTE: 압축의 목표는 동일한 정보를 더 적은 토큰으로 표현하는 것이지,
    '무엇을 기억할 것인가'를 선별하는 것이 아니다.
    세션에서 발생한 모든 의미 있는 정보는 형태를 바꾸더라도 전부 보존한다.
    턴 단위 단락 구조를 강제하여 AI의 장기 참조 품질을 높인다.

    Args:
        session (TRPGSession): 현재 진행 중인 세션 객체
        log_text (str): 요약 대상이 되는 미압축 기록 문자열 집합

    Returns:
        str: AI 모델에 전송할 압축 지시 프롬프트 문자열
    """
    return (
        "당신은 TRPG 세션의 전담 '기록 서기'입니다.\n"
        "임무: 제공된 [최근 플레이 기록]을 정보 손실 없이 텍스트 분량만 압축합니다.\n"
        "[이전 압축 기억]은 현재 상황의 맥락(인물·장소·진행 상황) 파악 용도로만 참고하고, "
        "절대 요약 결과에 다시 포함하여 출력하지 마십시오.\n\n"

        "══════════════════════════════════════════\n"
        "[압축의 목표]\n"
        "══════════════════════════════════════════\n"
        "이 압축은 '무엇을 기억할 것인가'를 고르는 작업이 아닙니다.\n"
        "세션에서 발생한 모든 의미 있는 정보는 형태를 바꾸더라도 전부 보존해야 합니다.\n"
        "비용 효율은 '문학적 잉여 표현 제거'와 '구조화를 통한 밀도 향상'으로만 달성합니다.\n\n"

        "══════════════════════════════════════════\n"
        "[반드시 보존해야 할 정보 — 누락 절대 불가]\n"
        "══════════════════════════════════════════\n"
        "아래 항목에 해당하는 정보는 어떠한 경우에도 생략하지 마십시오:\n\n"

        "▶ 행동 및 결과\n"
        "  - PC·NPC의 모든 행동과 그 직접적 결과·파급 영향\n"
        "  - 단순해 보이는 행동(물건 집기, 문 잠그기, 방향 전환 등)도 이후 전개에 영향을 줄 수 있으므로 포함\n"
        "  - 행동의 물리적 과정(어떻게 했는가)과 결과(무슨 일이 벌어졌는가)를 함께 기록\n\n"

        "▶ 판정 및 수치\n"
        "  - 주사위 굴림: 적용 스탯명, 굴림 수치, 기준치, 성공·실패 결과 및 대성공·대실패 여부\n"
        "  - 아이템 획득·소비·파괴·위치 변화: 아이템명과 수량 명시\n"
        "  - 스탯·자원 수치 변동: 항목명과 변동값 명시\n\n"

        "▶ 대화 전반\n"
        "  - 모든 대화의 주제, 핵심 내용, 결론\n"
        "  - 화자와 청자의 관계, 호칭, 존대·반말 여부\n"
        "  - 대화 중 드러난 감정·태도·의도 (적대, 경계, 호의, 회피 등)\n"
        "  - NPC가 제공하거나 암시한 정보, 숨긴 것이 드러난 경우 그 사실\n\n"

        "▶ 획득 정보\n"
        "  - 새로 알게 된 사실, 단서, 세계관 확인 사항\n"
        "  - 탐색·관찰로 발견한 물체·구조·상황\n\n"

        "▶ 감각 정보\n"
        "  - 이후 판단·행동·묘사에 영향을 줄 수 있는 시각·청각·후각·촉각 정보\n"
        "  - (예: 저 멀리 연기가 보임, 이상한 냄새가 남, 발소리가 들림 등)\n\n"

        "▶ 상태 변동\n"
        "  - PC·NPC의 상태이상 부여·해제 (항목명 정확히 기재)\n"
        "  - 부상·출혈 등 신체 상태 변화\n\n"

        "▶ 장소 및 공간\n"
        "  - 이동 경로와 현재 위치\n"
        "  - 진입한 공간의 구조·주요 물체·특이사항\n\n"

        "▶ NPC 변화\n"
        "  - 태도·감정·관계의 변화 (호감도 상승/하락, 의심 증가 등)\n"
        "  - 행동 의도가 드러난 발언이나 행동\n\n"

        "══════════════════════════════════════════\n"
        "[생략 허용 — 정보 가치 없는 것만]\n"
        "══════════════════════════════════════════\n"
        "이후 전개에 어떠한 영향도 미치지 않는다고 확신할 수 있는 경우만 제거합니다:\n"
        "  - 순수 분위기·미관 묘사 (예: 해가 지는 풍경, 바람 소리 — 이후 전개와 무관한 것만)\n"
        "  - 동일 내용의 반복 서술\n"
        "  - 정보를 담지 않는 잉여 구조 표현 ('그가 말했다', '그녀는 생각했다' 등)\n\n"

        "══════════════════════════════════════════\n"
        "[출력 양식 — 턴 단위 단락 구조]\n"
        "══════════════════════════════════════════\n"
        "각 턴을 하나의 독립된 단락으로 기록합니다.\n"
        "해당 사항이 없는 항목은 행을 생략하여 밀도를 유지합니다.\n\n"
        "─────────────────────────\n"
        "[#N턴 | 날짜·시간대 | 장소]\n"
        "· 행동·결과:      [누가] [무엇을] → [결과 / 파급 영향]\n"
        "· 대화:           [화자→청자] [호칭·존대] — [주제 / 핵심 내용 / 태도·감정 / 결론]\n"
        "· 획득 정보:      [새로 알게 된 사실·단서·발견물]\n"
        "· 감각 정보:      [이후 전개에 영향 가능한 시각·청각·후각·촉각 정보]\n"
        "· 수치·아이템:    [아이템명] +N획득 / -N소비 / 파괴 | [스탯명] 변동값\n"
        "· 상태 변동:      [이름] +상태이상명 / -상태이상명\n"
        "· 장소 이동:      [이전 장소] → [이후 장소] (경로 요약)\n"
        "─────────────────────────\n\n"
        "복수 턴이 포함된 경우 각 턴 단락 사이에 빈 줄을 삽입하여 구분합니다.\n"
        "코드블럭(상태창) 등 시스템 출력에서 턴 번호·날짜·장소 정보를 반드시 추출하여 단락 헤더에 기입하십시오.\n\n"

        f"[이전 압축 기억 (맥락 파악용으로만 참고할 것)]\n"
        f"{session.compressed_memory if session.compressed_memory else '없음'}\n\n"
        f"[최근 플레이 기록 (압축 대상)]\n{log_text}\n\n"
        "위 원칙과 양식에 따라 [최근 플레이 기록]만을 압축하여 출력하십시오."
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
                # NOTE: 캐시 보관 비용 산출에 필수 — 미복구 시 storage_cost가 항상 0으로 출력되는 버그
                session.cache_created_at = data.get("cache_created_at", 0.0)
                session.cache_tokens = data.get("cache_tokens", 0)
                session.cache_model = data.get("cache_model", DEFAULT_MODEL)
                session.cached_session_npcs = data.get("cached_session_npcs", {})
                session.cached_compressed_memory = data.get("cached_compressed_memory", "")
                session.narrative_plan = data.get("narrative_plan", {})

                # 자동 GM 모드 상태 복구 (auto_gm_lock은 런타임 락이므로 매 부팅마다 False로 초기화)
                session.auto_gm_active = data.get("auto_gm_active", False)
                session.auto_gm_target_char = data.get("auto_gm_target_char", None)
                session.auto_gm_turn_cap = data.get("auto_gm_turn_cap", 10)
                session.auto_gm_turns_done = data.get("auto_gm_turns_done", 0)
                session.auto_gm_clarify_count = data.get("auto_gm_clarify_count", 0)
                session.auto_gm_narrate_count = data.get("auto_gm_narrate_count", 0)
                session.auto_gm_cost_cap_krw = data.get("auto_gm_cost_cap_krw", 500.0)
                session.auto_gm_cost_baseline = data.get("auto_gm_cost_baseline", 0.0)
                session.auto_gm_side_note = data.get("auto_gm_side_note", "")
                session.auto_gm_lock = False
                # 멀티플레이어 수집 상태 복구 (#22)
                session.auto_gm_target_chars = data.get("auto_gm_target_chars", [])
                # 재시작 시 수집 진행 중이던 라운드는 초기화 (플레이어가 다시 응답해야 함)
                session.auto_gm_pending_players = []
                session.auto_gm_collected_actions = {}
                session.auto_gm_waiting_for = None

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
                                                                                                scenario_data,
                                                                                                session=session)

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
                        session.cache_created_at = time.time()
                        session.cache_tokens = cache_tokens
                        session.cache_model = DEFAULT_MODEL
                        update_session_cache_state(session)
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
    stat_system = scenario_data.get("stat_system", "")

    context_blocks = ""
    if recent_logs:
        context_blocks += f"[최근 상황 요약 (최근 3턴)]\n{recent_logs}\n\n"
    if npc_context:
        context_blocks += f"[참고용 기존 NPC 설정]\n{npc_context}\n\n"
    # NOTE: NPC 스탯 수치 부여 시 stat_system을 참조해야 적절한 기준값을 생성할 수 있다.
    # has_stats=true인 경우에만 주입하며, PC와 동일한 ability_stats 스키마를 사용한다.
    npc_tmpl_check = scenario_data.get("npc_template", {})
    if isinstance(npc_tmpl_check, dict) and npc_tmpl_check.get("has_stats") and stat_system:
        context_blocks += f"[스탯 시스템 (NPC 수치 부여 기준)]\n{stat_system}\n\n"

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
        # npc_template.info_fields가 있으면 시나리오 정의 항목을 그대로 양식으로 사용.
        # 출력 결과는 !엔피씨 설정으로 session.npcs[name]에 적용된다.
        npc_tmpl = scenario_data.get("npc_template", {})
        npc_fields = npc_tmpl.get("info_fields", []) if isinstance(npc_tmpl, dict) else []
        if not npc_fields:
            # 기본 12항목 양식
            npc_fields = [
                "나이·성별", "소속·직책", "외모", "핵심 기질",
                "행동 방식", "말투·어조", "동기·욕구", "두려움·약점",
                "비밀", "신뢰·의존", "경계·반목", "특기·능력"
            ]
        field_format = "\n".join(f"**{f}**: " for f in npc_fields)

        # NOTE: npc_template의 has_stats/has_resources/has_statuses 플래그에 따라
        # 출력 양식에 스탯·기본 자원·기본 상태 섹션을 추가한다.
        # 출력된 내용은 !엔피씨 설정에서 **레이블**: 형식으로 파싱되어 session.npcs에 저장되고,
        # resources/statuses는 session.resources/statuses에도 즉시 동기화된다.
        extra_format_lines = []
        extra_instructions = []
        if isinstance(npc_tmpl, dict):
            if npc_tmpl.get("has_stats"):
                # ability_stats가 있으면 그 이름을 힌트로 사용, 없으면 범용 힌트
                ability_stats = scenario_data.get("ability_stats", [])
                if ability_stats:
                    stat_hint = ", ".join(f"{s}=숫자" for s in ability_stats)
                else:
                    stat_hint = "스탯명=숫자, ..."
                extra_format_lines.append(f"**스탯**: {stat_hint}")
                extra_instructions.append(
                    "7. 스탯: '스탯명=숫자' 형식으로 콤마 구분 작성. NPC 역할에 맞는 수치를 부여할 것."
                )
            if npc_tmpl.get("has_resources"):
                extra_format_lines.append("**기본 자원**: 아이템명=수량, ... (초기 보유 없으면 '없음')")
                extra_instructions.append(
                    "8. 기본 자원: '아이템명=수량' 형식으로 콤마 구분 작성. NPC가 기본 보유하는 물자·장비만 포함."
                )
            if npc_tmpl.get("has_statuses"):
                extra_format_lines.append("**기본 상태**: 상태명, ... (초기 이상 없으면 '없음')")
                extra_instructions.append(
                    "9. 기본 상태: 쉼표로 구분된 상태명 목록. 처음부터 부여된 만성 상태·장애·특수 조건만 기재."
                )

        if extra_format_lines:
            field_format += "\n" + "\n".join(extra_format_lines)
        extra_rules = ("\n" + "\n".join(extra_instructions)) if extra_instructions else ""

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
            "6. 양식 엄수: 아래 [출력 양식]의 항목명·순서를 반드시 그대로 출력할 것.\n"
            "   항목 추가·삭제·순서 변경·항목명 수정 불가. GM 지시사항은 각 항목 값에 녹여낼 것.\n"
            f"{extra_rules}\n\n"
            f"[세계관 정보]\n{worldview}\n\n"
            f"{context_blocks}"
            f"[대상 캐릭터]\n"
            f"이름: {char_name}\n"
            f"GM 지시사항: {instruction}\n\n"
            "[출력 양식 — 아래 항목명과 순서를 그대로 사용하여 값만 채워 출력할 것]\n"
            f"{field_format}"
        )

    write_log(session_id, "api", f"[{char_type.upper()} 설정 생성 요청 - {char_name}]\n{prompt}")

    response = await asyncio.to_thread(
        bot.genai_client.models.generate_content,
        model=LOGIC_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(safety_settings=TRPG_SAFETY_SETTINGS)
    )
    return response


# ========== [인물 대사 마커 처리] ==========
# NOTE: AI가 출력한 `@대사:이름|본문` 마커를 감지하여 인물 헤더+말풍선 형식으로 변환.
#       마커 외 다른 텍스트가 섞이면 일반 묘사로 처리되도록 엄격 매칭.
DIALOGUE_MARKER_PATTERN = re.compile(r'^@대사:([^|\n]+)\|(.+)$', re.DOTALL)


def parse_dialogue_paragraph(paragraph: str):
    """
    문단이 인물 대사 마커(@대사:이름|본문)이면 (이름, 본문) 튜플을 반환, 아니면 None.
    """
    text = paragraph.strip()
    m = DIALOGUE_MARKER_PATTERN.match(text)
    if m:
        speaker = m.group(1).strip()
        content = m.group(2).strip()
        if speaker and content:
            return speaker, content
    return None


def format_dialogue_block(speaker: str, content: str) -> str:
    """인물 대사 문단을 디스코드 출력용 헤더 + 말풍선 마크다운으로 포매팅."""
    return f"## ▍{speaker}\n## 「 {content} 」"


def merge_consecutive_dialogues(paragraphs: list[str]) -> list[str]:
    """
    같은 화자의 연속된 대사 문단을 하나의 문단으로 통합.

    예)
      @대사:레비|어때? 감각이 느껴져?
      @대사:레비|체온은 정상인데, 불편한 곳은?
    →
      @대사:레비|어때? 감각이 느껴져? 체온은 정상인데, 불편한 곳은?

    연속 여부 기준: 두 대사 문단 사이에 다른 문단(일반 묘사 또는 다른 화자 대사)이 없어야 함.
    이 처리를 통해 동일 인물 이미지가 연속으로 중복 출력되는 것을 방지한다.

    Args:
        paragraphs (list[str]): split('\n\n')으로 분리된 문단 리스트

    Returns:
        list[str]: 연속 동일 화자 대사가 통합된 문단 리스트
    """
    merged: list[str] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        dialogue = parse_dialogue_paragraph(p)
        if not dialogue:
            merged.append(p)
            i += 1
            continue

        speaker, content = dialogue
        parts = [content]

        # 바로 다음 문단부터 같은 화자의 대사가 이어지는지 확인
        j = i + 1
        while j < len(paragraphs):
            next_d = parse_dialogue_paragraph(paragraphs[j])
            if next_d and next_d[0] == speaker:
                parts.append(next_d[1])
                j += 1
            else:
                break

        if len(parts) > 1:
            merged_content = " ".join(parts)
            merged.append(f"@대사:{speaker}|{merged_content}")
        else:
            merged.append(p)

        i = j

    return merged


async def maybe_send_speaker_image(channel, session, speaker: str) -> bool:
    """
    미디어 키워드 목록에 인물 이름과 일치하는 항목이 있으면 이미지를 전송.

    매칭 우선순위:
        1) media_keywords[speaker]        (정확한 키워드 매칭)
        2) media/{scenario_id}/{speaker}.png 파일 직접 존재 검사

    실패 시 조용히 False 반환 (대사 출력은 이어서 진행).
    """
    if not speaker:
        return False
    media_keywords = session.scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    candidate_filename = None
    if speaker in media_keywords:
        candidate_filename = media_keywords[speaker]
    else:
        # 폴백: 파일 직접 검사
        direct_path = os.path.join(media_dir, f"{speaker}.png")
        if os.path.exists(direct_path):
            candidate_filename = f"{speaker}.png"

    if not candidate_filename:
        return False

    filepath = os.path.join(media_dir, candidate_filename)
    if not os.path.exists(filepath):
        return False

    try:
        await channel.send(file=discord.File(filepath))
        return True
    except Exception as e:
        print(f"[Dialogue Image] {speaker} 이미지 전송 실패: {e}")
        return False


async def stream_text_to_channel(bot, channel, text: str, words_per_tick: int = 10, tick_interval: float = 1.5,
                                  quote_prefix: bool = True):
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
        quote_prefix (bool): True면 문단 앞에 '> '를 자동 부착 (기본). 인물 대사 등 헤더 마크다운이 들어간 문단은 False.
    """
    session = bot.active_sessions.get(channel.id)
    paragraphs = text.split('\n\n')

    for paragraph in paragraphs:
        if not paragraph.strip():
            continue

        if quote_prefix:
            current_text = "> " if not paragraph.startswith(">") else ""
        else:
            current_text = ""
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