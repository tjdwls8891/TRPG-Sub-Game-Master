# core 패키지 — 하위 모듈의 모든 심볼을 re-export하여 기존 `import core` 참조를 유지
#
# 분리된 서브모듈:
#   constants  — 전역 상수 (모델 ID, 환율, 안전 설정, 과금 단가표, 잉크 환산 상수)
#   ink        — 게임머니 '잉크' 환산 (원화 비용 -> 차감 잉크, 충전 플랜 가격)
#   extraction — 추출층위 (묘사 출력물에서 세계 상태·수치 추출, 임계값 대조)
#   rewind     — 되감기 델타 로그 (턴별 변화 기록, 전 턴 로그 보존)
#   estimate   — 비용 예측 (입력 실측 + 출력 이동평균, action별 범위 산출)
#   timeline   — 시간선 정량화 (일/24시간 단위, 나이 코드 계산)
#   media_control — BGM 상황 선택, 미디어 온오프 토글
#   resilience — API 재시도·타임아웃 차단·오류 로그
#   stats      — 누적 플레이 통계 (계정과 분리된 저장소)
#   display    — 디스플레이 채널 (단일 메시지 편집, persistent UI)
#   creation   — 세션 제작 과정 상태 기계 (단계·회귀·복원)
#   profiles   — 사전 저장 프로필 (생성·검색·수정·삭제, 태그)
#   profile_gen — 프로필 생성 모듈 11종 (시나리오 JSON이 지시하는 알고리즘)
#   profile_runner — 생성 알고리즘 실행부 (해독→종료까지 단일 진입)
#   profile_ui — 사전 프로필 관리 DM 인터페이스 (생성·출력·수정·삭제)
#   profile_creation_ui — 프로필 풀오토 생성 UI (실행부 구동)
#   profile_ai — 프로필 검증·병합 AI 모듈 (무료 제공, 비용 집계만)
#   quest      — 퀘스트 시스템 (필터·슬롯·케이스 트리·온디맨드 주입)
#   memory_plan — 기억 압축 플랜 4종 (주기·모델·서술 형태)
#   start_frame — 시작 상황 틀·프로필 브리핑 (세션 시작 다양화)
#   session_open — 세션 유지 시간 해석 결과의 환산·확인
#   terms      — 약관 동의·계정 등록 DM 인터페이스
#   gm_space   — 서버 GM 스페이스 (홈·명전·보드)
#   growth     — 능력치 성장·행운 스탯 판정
#   chat_guard — 채팅 차단 (채널별 권한 검증 단일 훅)
#   tts_preset — 시스템 고정 문구 TTS 사전 생성 (런타임 합성 없이 파일 재생)
#   irregular_npc — 비정규 NPC 이미지·목소리 결정 및 유지
#   accounts   — 유저 계정 저장 계층 (등록 여부, 약관 버전, 잉크 잔액 원장)
#   models     — TRPGSession 데이터 모델
#   cost       — 비용 산출 함수
#   io         — 세션 직렬화/역직렬화, 로그 기록, 시나리오 로드
#   cache      — 시나리오 룰북 캐시 빌드, 캐시 상태 동기화, 디스크 복구
#   prompt     — PromptBuilder, 기억 압축 프롬프트 생성
#   dialogue   — 인물 대사 마커 파싱, 이미지 자동 송출, 채널 스트리밍
#   media      — 이미지 키워드 전송, PlaylistManager
#   ui         — 디스코드 UI 컴포넌트 (DiceView, ChannelDeleteView 등)
#   utils      — 캐릭터 이름 검색, AI 설정 생성

from .constants import (
    __version__,
    MIN_CACHE_TOKENS,
    CACHE_TTL_SECONDS,
    INK_UNIT_KRW,
    INK_NET_KRW,
    INK_PLANS,
    DEFAULT_MODEL,
    LOGIC_MODEL,
    PROFILE_AI_MODEL,
    IMAGE_MODEL,
    EXCHANGE_RATE,
    TRPG_SAFETY_SETTINGS,
    PRICING_1M,
    IMAGE_OUTPUT_TOKENS_BY_RES,
    TTS_MODEL,
    TTS_NARRATOR_VOICE,
    TTS_LANGUAGE_CODE,
    TTS_NARRATION_VOLUME,
    TTS_PCM_BYTES_PER_SEC,
    TTS_STYLE_PROMPT,
    TTS_VOICES,
)
from .ink import (
    cost_to_ink,
    ink_to_krw,
    ink_to_net_krw,
    plan_catalog,
    can_afford,
    format_ink,
    refund_ink,
)
from . import accounts
from .extraction import (
    COMMON_EXTRACTION_TARGETS,
    EXTRACTION_RESPONSE_SCHEMA,
    THRESHOLDS,
    get_thresholds,
    build_extraction_targets,
    parse_extraction,
    apply_extraction,
    resource_changes_to_tags,
    to_world_timeline,
    summarize_for_report,
)
from .rewind import (
    REWIND_MAX_TURNS,
    TRACKED_PATHS,
    capture_state,
    diff_state,
    record_delta,
    record_full_log,
    archive_removed,
    rewind_to,
    available_range,
    serialize_log_entries,
    read_jsonl,
)
from .estimate import (
    CHARS_TO_TOKENS,
    CONSERVATIVE_FACTOR,
    update_stats,
    estimate_input_tokens,
    estimate_turn,
    estimate_session_open,
    record_actual_input,
    estimate_compression,
    compression_prepay,
    settle_compression,
    settle_on_session_close,
    COMPRESSION_INTERVAL,
    get_calibration,
    format_estimate,
)
from .timeline import (
    quantify,
    current_year,
    compute_age,
    enrich_npc_ages,
    age_gap,
    format_timeline,
    to_day_number,
    hour_of,
)
from .media_control import (
    DEFAULT_MEDIA_FLAGS,
    get_media_flags,
    set_media_flag,
    is_enabled,
    sync_tts_flag,
    tension_band,
    select_bgm,
    describe_bgm_pending,
    format_flags,
)
from .resilience import (
    call_with_retry,
    get_timeout,
    write_error_log,
    build_failed_turn_notice,
    USER_FACING_NOTICE,
)
from . import stats
from . import creation
from . import profiles
from . import profile_gen
from . import profile_runner
from . import profile_ai
from . import quest
from . import memory_plan
from . import start_frame
from .profile_ui import open_manager, ProfileHomeView
from . import profile_creation_ui
from .session_open import (
    resolve_minutes,
    should_charge_interpretation,
    format_confirmation,
    VAGUE_MINUTES,
    MINUTES_PER_TURN,
    MIN_MINUTES,
    MAX_MINUTES,
)
from .terms import (
    TERMS_TEXT,
    SIGNUP_GIFT_INK,
    build_terms_embed,
    TermsView,
    start_registration,
    ensure_agreed,
)
from .gm_space import (
    ensure_space,
    refresh_boards,
    refresh_home,
    GMHomeView,
    build_hall_embed,
    build_board_embed,
)
from .growth import (
    process_roll_outcome,
    check_growth,
    check_luck,
    get_growth_config,
    get_luck_config,
    format_growth,
    format_luck,
)
from .chat_guard import chat_guard, NOTICE_SECONDS
from . import tts_preset
from . import irregular_npc
from .display import build_embed, build_view, DisplayView, refresh as refresh_display
from .models import TRPGSession
from .cost import (
    extract_token_usage,
    format_cost,
    calculate_text_gen_cost_breakdown,
    calculate_image_gen_cost,
    calculate_upload_cost,
    calculate_storage_cost,
    calculate_cost,
    build_cache_cost_embed,
    build_text_gen_cost_embed,
    build_image_gen_cost_embed,
    build_compression_cost_embed,
    build_turn_cost_embed,
)
from .io import (
    SCHEMA_VERSION,
    migrate_session_data,
    SESSION_FIELDS,
    SESSION_RESET_FIELDS,
    write_log,
    write_cost_log,
    load_scenario_from_file,
    get_available_scenarios,
    save_session_data,
    process_cache_deletion,
    load_authorized_users,
    save_authorized_users,
    AUTHORIZED_USERS_PATH,
)
from .cache import (
    build_scenario_cache_text,
    update_session_cache_state,
    restore_sessions_from_disk,
)
from .prompt import PromptBuilder, build_compression_prompt
from .dialogue import (
    pick_status_message,
    send_layer_status,
    LAYER_STATUS_MESSAGES,
    WAITING_TIPS,
    DIALOGUE_MARKER_PATTERN,
    parse_dialogue_paragraph,
    format_dialogue_block,
    merge_consecutive_dialogues,
    maybe_send_speaker_image,
    stream_text_to_channel,
    strip_unauthorized_pc_dialogue,
    send_status_message,
    clear_status_message,
)
from .media import send_image_by_keyword, PlaylistManager
from .audio_mixer import (
    MixerAudioSource,
    PCMBytesAudioSource,
    get_mixer,
    ensure_mixer,
    active_volume_source,
    preload_sfx,
    play_sfx_on_vc,
    play_dice_sfx,
)
from .tts import synthesize_tts_pcm, clean_text_for_tts
from .ui import (
    _cleanup_session_memory,
    ChannelSelect,
    ChannelDeleteView,
    GeneralDiceView,
    DiceView,
)
from .utils import (
    get_uid_by_char_name,
    generate_character_details,
    get_merged_status_effects,
    resolve_char_name,
    resolve_pc,
    decompose_hangul,
    suggest_commands,
)

__all__ = [
    # constants
    "__version__",
    "DEFAULT_MODEL", "LOGIC_MODEL", "PROFILE_AI_MODEL", "IMAGE_MODEL", "EXCHANGE_RATE",
    "MIN_CACHE_TOKENS", "CACHE_TTL_SECONDS", "INK_UNIT_KRW", "INK_NET_KRW", "INK_PLANS",
    # ink / accounts
    "creation", "profiles", "profile_gen", "profile_runner", "profile_ai", "quest", "memory_plan", "start_frame",
    "open_manager", "ProfileHomeView", "profile_creation_ui",
    "resolve_minutes", "should_charge_interpretation", "format_confirmation",
    "VAGUE_MINUTES", "MINUTES_PER_TURN", "MIN_MINUTES", "MAX_MINUTES",
    "TERMS_TEXT", "SIGNUP_GIFT_INK", "build_terms_embed", "TermsView",
    "start_registration", "ensure_agreed",
    "ensure_space", "refresh_boards", "refresh_home", "GMHomeView",
    "build_hall_embed", "build_board_embed",
    "process_roll_outcome", "check_growth", "check_luck",
    "get_growth_config", "get_luck_config", "format_growth", "format_luck",
    "chat_guard", "NOTICE_SECONDS", "tts_preset", "irregular_npc",
    "build_embed", "build_view", "DisplayView", "refresh_display",
    "DEFAULT_MEDIA_FLAGS", "get_media_flags", "set_media_flag", "is_enabled",
    "tension_band", "select_bgm", "describe_bgm_pending", "format_flags", "sync_tts_flag",
    "call_with_retry", "get_timeout", "write_error_log",
    "build_failed_turn_notice", "USER_FACING_NOTICE", "stats",
    "quantify", "current_year", "compute_age", "enrich_npc_ages",
    "age_gap", "format_timeline", "to_day_number", "hour_of",
    "CHARS_TO_TOKENS", "CONSERVATIVE_FACTOR", "update_stats",
    "estimate_input_tokens", "estimate_turn", "estimate_session_open", "format_estimate",
    "record_actual_input", "get_calibration",
    "estimate_compression", "compression_prepay", "settle_compression",
    "settle_on_session_close", "COMPRESSION_INTERVAL",
    "REWIND_MAX_TURNS", "TRACKED_PATHS", "capture_state", "diff_state",
    "record_delta", "record_full_log", "archive_removed", "available_range", "rewind_to",
    "serialize_log_entries", "read_jsonl",
    "COMMON_EXTRACTION_TARGETS", "EXTRACTION_RESPONSE_SCHEMA", "THRESHOLDS",
    "get_thresholds", "build_extraction_targets", "parse_extraction",
    "apply_extraction", "resource_changes_to_tags",
    "to_world_timeline", "summarize_for_report",
    "cost_to_ink", "ink_to_krw", "ink_to_net_krw", "plan_catalog",
    "can_afford", "format_ink", "refund_ink", "accounts",
    "TRPG_SAFETY_SETTINGS", "PRICING_1M", "IMAGE_OUTPUT_TOKENS_BY_RES",
    "TTS_MODEL", "TTS_NARRATOR_VOICE", "TTS_LANGUAGE_CODE",
    "TTS_NARRATION_VOLUME", "TTS_PCM_BYTES_PER_SEC", "TTS_STYLE_PROMPT", "TTS_VOICES",
    # models
    "TRPGSession",
    # cost
    "extract_token_usage", "format_cost", "calculate_text_gen_cost_breakdown", "calculate_image_gen_cost",
    "calculate_upload_cost", "calculate_storage_cost", "calculate_cost",
    # io
    "SCHEMA_VERSION", "migrate_session_data", "SESSION_FIELDS", "SESSION_RESET_FIELDS",
    "write_log", "write_cost_log", "load_scenario_from_file", "get_available_scenarios",
    "save_session_data", "process_cache_deletion",
    "load_authorized_users", "save_authorized_users", "AUTHORIZED_USERS_PATH",
    # cache
    "build_scenario_cache_text", "update_session_cache_state", "restore_sessions_from_disk",
    # prompt
    "PromptBuilder", "build_compression_prompt",
    # dialogue
    "DIALOGUE_MARKER_PATTERN", "parse_dialogue_paragraph", "format_dialogue_block",
    "merge_consecutive_dialogues", "maybe_send_speaker_image", "stream_text_to_channel",
    "strip_unauthorized_pc_dialogue", "pick_status_message", "send_layer_status",
    "LAYER_STATUS_MESSAGES", "WAITING_TIPS", "send_status_message", "clear_status_message",
    # media
    "send_image_by_keyword", "PlaylistManager",
    # audio_mixer
    "MixerAudioSource", "PCMBytesAudioSource", "get_mixer", "ensure_mixer",
    "active_volume_source", "preload_sfx", "play_sfx_on_vc", "play_dice_sfx",
    # tts
    "synthesize_tts_pcm", "clean_text_for_tts",
    # ui
    "_cleanup_session_memory", "ChannelSelect", "ChannelDeleteView",
    "GeneralDiceView", "DiceView",
    # utils
    "get_uid_by_char_name", "generate_character_details", "get_merged_status_effects",
    "resolve_char_name", "resolve_pc", "decompose_hangul", "suggest_commands",
]
