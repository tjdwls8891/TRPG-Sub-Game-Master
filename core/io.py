# 파일 I/O — 세션 직렬화/역직렬화, 로그 기록, 시나리오 로드, 캐시 파기 정산
import os
import json
import copy
import asyncio
import time
from datetime import datetime

from .constants import DEFAULT_MODEL
from .cost import calculate_storage_cost
from .models import TRPGSession


# ========== [명령어 권한: 허가 계정 목록] ==========
# 봇 전역 allowlist. 오너(앱 소유자)와 오너가 권한을 부여한 계정만 마스터 명령을 사용할 수 있다.
AUTHORIZED_USERS_PATH = "authorized_users.json"


def load_authorized_users() -> dict:
    """허가 계정 목록 로드. 반환: {"owner_id": int|None, "granted": [int, ...]}"""
    try:
        with open(AUTHORIZED_USERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "owner_id": data.get("owner_id"),
            "granted": [int(x) for x in data.get("granted", [])],
        }
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return {"owner_id": None, "granted": []}


def save_authorized_users(data: dict):
    """허가 계정 목록을 원자적으로 저장(.tmp → os.replace)."""
    payload = {
        "owner_id": data.get("owner_id"),
        "granted": sorted({int(x) for x in data.get("granted", [])}),
    }
    tmp = AUTHORIZED_USERS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, AUTHORIZED_USERS_PATH)
    except Exception as e:
        print(f"⚠️ [권한] 허가 계정 저장 실패: {e}")


# ========== [직렬화 스키마 버전] ==========
# 세션 데이터 JSON 구조가 변경될 때 증가. restore 시 구버전 경고 출력에 사용.
# SCHEMA_VERSION 3 — auto_gm_* 13필드를 gm_*로 이관 (5.0.0)
# 필드명이 바뀌었으므로 구버전 저장 파일은 migrate_session_data로 변환해야 한다.
SCHEMA_VERSION = 3

# 구버전 → 현행 필드명 매핑. 로드 시 자동 변환된다.
FIELD_MIGRATIONS = {
    2: {
        "auto_gm_active": "gm_active",
        "auto_gm_target_char": "gm_target_char",
        "auto_gm_target_chars": "gm_target_chars",
        "auto_gm_turn_cap": "gm_turn_cap",
        "auto_gm_turns_done": "gm_turns_done",
        "auto_gm_clarify_count": "gm_clarify_count",
        "auto_gm_narrate_count": "gm_narrate_count",
        "auto_gm_cost_cap_krw": "gm_cost_cap_krw",
        "auto_gm_cost_baseline": "gm_cost_baseline",
        "auto_gm_side_note": "gm_side_note",
        "auto_gm_proceed_history": "gm_proceed_history",
        "auto_gm_pending_players": "gm_pending_players",
        "auto_gm_collected_actions": "gm_collected_actions",
        "auto_gm_waiting_for": "gm_waiting_for",
    },
}


def migrate_session_data(data: dict) -> dict:
    """저장 데이터를 현행 스키마로 변환한다.

    NOTE: 구버전 필드명을 새 이름으로 옮긴다. 값이 이미 새 이름으로
          존재하면 덮어쓰지 않는다(부분 마이그레이션 상태 대비).
          변환 후 schema_version을 갱신하므로 다음 로드부터는 건너뛴다.
    """
    ver = int(data.get("schema_version", 1) or 1)
    if ver >= SCHEMA_VERSION:
        return data

    moved = 0
    for from_ver in sorted(FIELD_MIGRATIONS):
        if ver > from_ver:
            continue
        for old, new in FIELD_MIGRATIONS[from_ver].items():
            if old in data:
                if new not in data:
                    data[new] = data[old]
                    moved += 1
                data.pop(old, None)

    data["schema_version"] = SCHEMA_VERSION
    if moved:
        print(f"[마이그레이션] v{ver} → v{SCHEMA_VERSION}: {moved}개 필드 이관")
    return data


# ========== [선택적 필드 레지스트리] ==========
# save_session_data / restore_sessions_from_disk 양쪽의 단일 진실 공급원.
# 새 TRPGSession 필드를 추가할 때 이곳에만 등록하면 저장·복구가 자동 반영된다.
#
# 규칙:
#   - 값이 반드시 존재하는 핵심 필드(session_id, players 등)는 여기에 넣지 않는다.
#   - 런타임 전용 필드(gm_lock, is_processing 등)는 여기에 넣지 않는다.
#   - 재시작 시 항상 초기화되어야 하는 필드는 SESSION_RESET_FIELDS에 넣는다.
SESSION_FIELDS: dict = {
    "note": "",
    "is_started": False,
    "total_cost": 0.0,
    "volume": 0.3,
    "cache_note": "",
    "cache_created_at": 0.0,
    "cache_tokens": 0,
    "cache_model": DEFAULT_MODEL,
    "last_turn_anchor_id": None,
    "cache_text": "",
    "cached_session_npcs": {},
    "cached_compressed_memory": "",
    "cached_worldview_sections": [],  # 캐시에 편입된 연고지(사문·근거지) keyword_memory 섹션 id — 온디맨드 중복 주입 억제용
    "narrative_plan": {},
    "world_timeline": {},
    "start_frame": {},
    "last_compressed_turn": 0,
    "compression_count": 0,
    "pending_ending": "",
    "main_unlocked_notified": False,
    "infinity_plan": "",
    "quest_state": {},
    "player_faction": "",
    "profile_ai_cost_krw": 0.0,
    "creation_state": {},
    "interpret_cost_krw": 0.0,
    "open_prepaid_ink": 0,
    "open_minutes": 0,
    "started_at": 0.0,
    "stat_fail_counts": {},
    "last_turn_cost": 0.0,
    "tts_voice": "",
    "memory_plan": "normal",
    "narrative_mode": "quest",
    "session_kind": "solo",
    "irregular_npcs": {},
    "voice_ch_id": None,
    "creator_uid": "",
    "display_ch_id": None,
    "display_msg_id": None,
    "awaiting_display_input": False,
    "is_private": False,
    "pending_bgm": None,
    "media_flags": {},
    "last_bgm_situation": None,
    "start_day_number": None,
    "compression_prepaid_krw": 0.0,
    "last_estimate": {},
    "cache_read_tokens": 0,
    "cost_stats": {},
    "last_recorded_turn": 0,
    "extraction_pending": False,
    "extraction_retry_ctx": {},
    "last_extraction": {},
    "info_ledger": [],  # 정보 인지 원장 (비공개 정보별 인지 주체 — 지시층위가 누적 갱신)
    # TTS 음성 더빙 (실험)
    "tts_enabled": False,
    # GM 설정
    "gm_active": False,
    "gm_target_char": None,
    "gm_turn_cap": None,
    "gm_turns_done": 0,
    "gm_clarify_count": 0,
    "gm_narrate_count": 0,
    "gm_cost_cap_krw": None,
    "gm_cost_baseline": 0.0,
    "gm_side_note": "",
    "gm_target_chars": [],
    "gm_proceed_history": [],
    # 이하 세 필드는 저장은 하지만 복구 시 항상 초기화 (SESSION_RESET_FIELDS 참고)
    "gm_pending_players": [],
    "gm_collected_actions": {},
    "gm_waiting_for": None,
}

# 저장은 되지만 봇 재시작 시 항상 초기값으로 리셋되는 필드
SESSION_RESET_FIELDS: dict = {
    "gm_pending_players": [],
    "gm_collected_actions": {},
    "gm_waiting_for": None,
}

# ──────────────────────────────────────────
# 내부 헬퍼 — 외부에서 직접 호출하지 말 것
# ──────────────────────────────────────────

_MISSING = object()  # data.get 누락 여부를 None과 구분하기 위한 센티널


def _serialize_log_entry(content) -> dict | None:
    """
    types.Content 객체 → {"role": str, "text": str} 딕셔너리 변환.

    - 여러 text 파트는 줄바꿈으로 연결.
    - 이미지·함수 호출 등 text 속성 없는 파트는 조용히 건너뜀.
    - 변환 불가 엔트리는 None 반환 (save 시 필터링됨).
    """
    try:
        texts = []
        for part in (content.parts or []):
            try:
                t = part.text
                if t:
                    texts.append(t)
            except AttributeError:
                # 이미지, 함수 호출, 실행 결과 등 text가 없는 파트
                pass
        if not texts:
            return None
        return {"role": content.role, "text": "\n".join(texts)}
    except Exception as e:
        print(f"⚠️ [직렬화] raw_logs 엔트리 변환 실패 (건너뜀): {e}")
        return None


# ========== [코어 유틸리티 함수(Utilities)] ==========

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

    안정성 보장:
    - raw_logs: _serialize_log_entry로 파트 단위 안전 변환 (비-텍스트 파트 무시)
    - 필드 누락 방지: SESSION_FIELDS 레지스트리로 getattr 일괄 처리
    - 원자적 쓰기: .tmp 임시 파일에 쓴 뒤 os.replace로 교체 (중간 크래시 시 이전 파일 보존)
    - 예외 격리: 저장 실패가 게임 로직을 중단시키지 않도록 외부 try/except로 감쌈
    """
    if session.session_id not in bot.session_io_locks:
        bot.session_io_locks[session.session_id] = asyncio.Lock()

    async with bot.session_io_locks[session.session_id]:
        try:
            # ── raw_logs 직렬화 (파트 단위 안전 변환) ──
            serialized_raw_logs = []
            for content in session.raw_logs:
                entry = _serialize_log_entry(content)
                if entry is not None:
                    serialized_raw_logs.append(entry)

            # ── 핵심 필드 (항상 존재, 직접 접근) ──
            data: dict = {
                "schema_version": SCHEMA_VERSION,
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
            }

            # ── 선택적 필드 — SESSION_FIELDS 레지스트리로 일괄 직렬화 ──
            for field, default in SESSION_FIELDS.items():
                data[field] = getattr(session, field, default)

            # ── 원자적 쓰기: tmp → os.replace ──
            # NOTE: tmp 파일명을 호출별로 고유화한다. 동시 저장(예: 백그라운드 기억 압축과
            # 프로씨드가 각각 save_session_data 호출)이 같은 tmp를 덮어써 data.json이 손상되는
            # 경합을 방지한다. os.replace는 원자적이므로 최종 파일은 항상 완전한 스냅샷이다.
            session_dir = f"sessions/{session.session_id}"
            final_path = f"{session_dir}/data.json"
            tmp_path = f"{session_dir}/data.json.{os.getpid()}.{time.time_ns()}.tmp"

            def write_file():
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                os.replace(tmp_path, final_path)

            await asyncio.to_thread(write_file)

        except Exception as e:
            # NOTE: 저장 실패가 게임 진행을 중단시키면 안 되므로 예외를 흡수하고 경고만 출력.
            # 실패로 남은 고유 tmp 파일(data.json.*.tmp)들을 정리 시도.
            print(f"⚠️ [세션 저장 실패] {session.session_id}: {e}")
            try:
                session_dir = f"sessions/{session.session_id}"
                for fn in os.listdir(session_dir):
                    if fn.startswith("data.json.") and fn.endswith(".tmp"):
                        try:
                            os.remove(os.path.join(session_dir, fn))
                        except OSError:
                            pass
            except OSError:
                pass


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
        model_id = getattr(session, "cache_model", DEFAULT_MODEL) or DEFAULT_MODEL
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
