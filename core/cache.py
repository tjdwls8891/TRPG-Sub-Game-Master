# 캐시 관리 — 시나리오 룰북 캐시 빌드, 캐시 상태 동기화, 세션 디스크 복구
import os
import json
import copy
import asyncio
import time

from google.genai import types
from google.genai.errors import APIError

from .constants import DEFAULT_MODEL
from .models import TRPGSession
from .io import write_log, save_session_data, load_scenario_from_file, SESSION_FIELDS, SESSION_RESET_FIELDS, SCHEMA_VERSION, _MISSING
from .cost import calculate_upload_cost, calculate_cost
from .utils import get_merged_status_effects


def _deserialize_log_entry(item: dict):
    """
    {"role": str, "text": str} 딕셔너리 → types.Content 객체로 복원.

    - role 또는 text가 누락된 항목은 None 반환 (restore 시 필터링됨).
    - SDK 변경이나 잘못된 저장 데이터에도 조용히 실패하도록 예외 처리.
    """
    try:
        role = item.get("role", "user")
        text = item.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return None
        return types.Content(role=role, parts=[types.Part.from_text(text=text)])
    except Exception as e:
        print(f"⚠️ [역직렬화] raw_logs 엔트리 복원 실패 (건너뜀): {e}")
        return None


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

    # ── 상태이상 목록 섹션 ──
    merged_status_effects = get_merged_status_effects(scenario_data)
    if merged_status_effects:
        se_lines = []
        for name, eff in merged_status_effects.items():
            w = eff.get("weight", 0)
            w_str = f"판정 가중치 {w:+d}" if w != 0 else "판정 가중치 없음"
            se_lines.append(
                f"- {name}: 적용조건=[{eff.get('apply_condition', '')}] / {w_str} / 제거조건=[{eff.get('remove_condition', '')}]"
            )
        status_effects_section = f"""
[4.5. 사용 가능한 상태이상 목록]
(태: 태그로 캐릭터에 부여하거나 제거할 수 있는 공식 상태이상 목록이다.
이 목록에 존재하는 이름만 태: 태그에 사용해야 한다. 적용 조건·제거 조건을 반드시 준수할 것.)
""" + "\n".join(se_lines) + "\n"
    else:
        status_effects_section = ""

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
{status_effects_section}
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

                # ── 스키마 버전 경고 ──
                saved_version = data.get("schema_version", 1)
                if saved_version < SCHEMA_VERSION:
                    print(f"⚠️ {session_id}: 저장 스키마 v{saved_version} → 현재 v{SCHEMA_VERSION} (구버전 — 일부 필드 기본값 사용)")

                scenario_data = load_scenario_from_file(data["scenario_id"])
                if not scenario_data:
                    continue

                session = TRPGSession(
                    data["session_id"], data["game_ch_id"], data["master_ch_id"],
                    data["scenario_id"], scenario_data
                )

                # ── 핵심 필드 복구 ──
                session.players = data.get("players", {})
                session.npcs = data.get("npcs", {})
                session.resources = data.get("resources", {})
                session.statuses = data.get("statuses", {})
                session.compressed_memory = data.get("compressed_memory", "")
                session.cache_name = data.get("cache_name")
                session.current_turn_logs = data.get("current_turn_logs", [])
                session.turn_count = data.get("turn_count", 0)
                session.uncompressed_logs = data.get("uncompressed_logs", [])

                # ── 선택적 필드 — SESSION_FIELDS 레지스트리로 일괄 복구 ──
                # _MISSING 센티널로 실제 누락(기본값 사용)과 None 저장값을 구분한다.
                for field, default in SESSION_FIELDS.items():
                    value = data.get(field, _MISSING)
                    if value is _MISSING:
                        setattr(session, field, copy.deepcopy(default))
                    else:
                        setattr(session, field, value)

                # ── 재시작 시 항상 초기화되는 필드 (SESSION_RESET_FIELDS) ──
                for field, reset_val in SESSION_RESET_FIELDS.items():
                    setattr(session, field, copy.deepcopy(reset_val))

                # ── 런타임 전용 필드 초기화 ──
                session.is_processing = False
                session.auto_gm_lock = False

                # ── 후처리: None으로 저장된 cache_model을 DEFAULT_MODEL로 복원 ──
                # (process_cache_deletion이 cache_model = None으로 저장하는 경우 대비)
                if not session.cache_model:
                    session.cache_model = DEFAULT_MODEL

                # ── raw_logs 복원 (_deserialize_log_entry로 안전 변환) ──
                restored_raw_logs = []
                for item in data.get("raw_logs", []):
                    entry = _deserialize_log_entry(item)
                    if entry is not None:
                        restored_raw_logs.append(entry)
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
