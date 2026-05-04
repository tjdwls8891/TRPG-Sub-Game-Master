# 유틸리티 — 캐릭터 이름 검색, AI 설정 생성
import os
import json
import asyncio

from google.genai import types

from .constants import DEFAULT_MODEL, LOGIC_MODEL, TRPG_SAFETY_SETTINGS
from .models import TRPGSession
from .io import write_log
from prompts import build_pc_appearance_prompt, build_npc_profile_prompt


def get_merged_status_effects(scenario_data: dict) -> dict:
    """
    공통 상태이상 목록(data/common_status_effects.json)과 시나리오별 status_effects를 병합.
    같은 이름이 겹치면 시나리오 정의가 공통 정의를 덮어쓴다.

    Returns:
        dict: {이름: {"name", "apply_condition", "weight", "remove_condition"}, ...}
    """
    common_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'common_status_effects.json')
    common_effects = []
    try:
        with open(common_path, 'r', encoding='utf-8') as f:
            common_effects = json.load(f)
    except Exception as e:
        print(f"[상태이상] 공통 목록 로드 실패: {e}")

    scenario_effects = scenario_data.get("status_effects", [])

    merged: dict = {}
    for effect in common_effects:
        name = effect.get("name")
        if name:
            merged[name] = effect
    for effect in scenario_effects:
        name = effect.get("name")
        if name:
            merged[name] = effect

    return merged


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
        prompt = build_pc_appearance_prompt(
            worldview=worldview,
            char_name=char_name,
            instruction=instruction,
            context_blocks=context_blocks,
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

        prompt = build_npc_profile_prompt(
            worldview=worldview,
            char_name=char_name,
            instruction=instruction,
            field_format=field_format,
            context_blocks=context_blocks,
            extra_rules=extra_rules,
        )

    write_log(session_id, "api", f"[{char_type.upper()} 설정 생성 요청 - {char_name}]\n{prompt}")

    response = await asyncio.to_thread(
        bot.genai_client.models.generate_content,
        model=LOGIC_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(safety_settings=TRPG_SAFETY_SETTINGS)
    )
    return response
