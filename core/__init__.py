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
#   session_flow — 세션 제작 17단계 통합 플로우 (상태 기계 구동)
#   profile_ai — 프로필 검증·병합 AI 모듈 (무료 제공, 비용 집계만)
#   quest      — 퀘스트 시스템 (필터·슬롯·케이스 트리·온디맨드 주입)
#   quest_filter — 필터 매칭 (통과한 값이 곧 슬롯)
#   memory_plan — 기억 압축 플랜 4종 (주기·모델·서술 형태)
#   start_frame — 시작 상황 틀·프로필 브리핑 (세션 시작 다양화)
#   koreantext — 슬롯 치환·조사 보정 (틀·퀘스트 공용)
#   places     — 장소 계층 그래프·이동 개연성·이미지 상속
#   session_open — 세션 유지 시간 해석 결과의 환산·확인
#   terms      — 약관 동의·계정 등록 DM 인터페이스
#   gm_space   — 서버 GM 스페이스 (홈·명전·보드)
#   growth     — 능력치 성장·행운 스탯 판정
#   judgment   — 2d50 판정 수식(T 조립·굴림·대결) 순수 함수
#   master     — 마스터(진행자) 사전 저작 대사 선택(assets/master_dialogues_v1.json, 폴백 = 기존 문구)
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
#   world_ids  — (H01) 정규 직렬화·안정 digest·저작 ID 파생
#   definition_compiler — (H01) 시나리오 → 컴파일된 세계 정의·revision·이행 보고 (production 미배선)
#   definition_snapshot — (H01) 세션 정의 스냅샷 쓰기·검증 프리미티브 (production 미배선)
#   world_state — (H02) 동적 세계 정본 필드 레지스트리·world_version 정체성
#   world_mutation — (H02) 세계 변이 묶음·투영 세계·병합/충돌
#   canonical_mutation — (H02) INITIALIZATION/ADMIN_EDIT 공통 원자 적용 (production 호출자 0)
#   world_clock — (H03) 달력·canonical 분 시계
#   world_schedule — (H03) pending 예약·base quiescence·시간/예약 typed 연산
#   spatial — (H03) 방향 이동 그래프·정적 가시성·배치/TRANSIT·공간 지식·공간 typed 연산
#   access — (H03) 접근 규칙 평가기·실제 접근/인지 접근 분리
#   movement — (H03) 고정 정밀도 소요시간·KnownRoutePlanner·ActualTraversalPlanner
#   world_refs — (H04) 타입 값 참조·의존 키
#   actors · information · cognition · relationships — (H04) 행위자·정보·인지·관계 정본 + typed 연산
#   npc_projection — (H04) 읽기 전용 CurrentNpcProjection
#   npc_position — (H07) NPC 유효 위치(정본 override > 평상시 일과 > untracked)
#   place_display — (H07) 내부 PlaceId ≠ 플레이어 표시명(PC 지식 수준)
#   predicates · events · world_progression — (H04) 3값 조건·사건 엔진·순수 세계 진행 투영
#   action_intent · player_world · turn_world — (H05) 구조화 의도·플레이어 세계 계획·턴 관찰/후속 묘사/투영 미디어
#   world_lifecycle — (H06) 정의 바인딩·초기 세계 계획·INITIALIZATION·동적 세계 생애주기
#   cache_source · admin_world_edit — (H06) 캐시 고정 원천 · typed 관리자 세계 편집(ADMIN_EDIT)

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
    apply_companions,
    normalize_status_item_effects,
    apply_normalized_status_item,
    normalize_companions,
    apply_normalized_companions,
    normalize_location,
    release_resident_companions,
    to_world_timeline,
    summarize_for_report,
    build_extraction_limits,
)
from .rewind import (
    REWIND_MAX_TURNS,
    TRACKED_PATHS,
    capture_state,
    diff_state,
    record_delta,
    record_full_log,
    archive_removed,
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
    estimate_tts,
    record_actual_input,
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
from . import quest_filter
from . import memory_plan
from . import start_frame
from . import koreantext
from . import places
from .profile_ui import open_manager, ProfileHomeView
from . import profile_creation_ui
from . import intro
from . import session_flow
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
from . import judgment
from . import master
from .chat_guard import chat_guard, NOTICE_SECONDS
from . import tts_preset
from . import irregular_npc
from .display import build_embed, build_view, DisplayView, refresh as refresh_display
from .models import TRPGSession
from . import turn_transaction
from . import turn_preparation
from . import cost_ledger  # WP-02: append-only shadow CostLedger
from . import commit_journal  # WP-JOURNAL-01: durable commit journal foundation
from . import settlement  # WP-SETTLEMENT-01: immutable TurnSettlement + durable store
from . import ink_transactions  # WP-SETTLEMENT-01: exactly-once InkTransaction executor
from . import commit_coordinator  # WP-D: authoritative commit / recovery / billing cutover
from . import turn_history  # WP-E: selected committed-attempt history / rewind / rerender
from . import cache_lifecycle  # WP-F: cache lifecycle finance single owner
from . import message_lifecycle  # WP-F: player-channel message lifecycle owner
from . import interpretation_billing  # WP-F: cache-time interpretation billing (separate from cache window)
from . import world_ids  # H01: canonical serialization / stable digest / authored IDs (production 미배선)
from . import definition_compiler  # H01: scenario → compiled world definition + revision (production 미배선)
from . import definition_snapshot  # H01: session-bound definition snapshot primitives (production 미배선)
from . import world_state  # H02: dynamic-world canonical field registry + world_version identity
from . import world_mutation  # H02: WorldMutationBatch / ProjectedWorld / merge-conflict foundation
from . import canonical_mutation  # H02: INITIALIZATION/ADMIN_EDIT common primitive (production 호출자 0)
from . import world_clock  # H03: calendar + canonical minute clock
from . import world_schedule  # H03: pending schedule + base quiescence + time typed ops
from . import access  # H03: access rule registry + actual vs perceived access
from . import spatial  # H03: directed movement graph + static visibility + spatial knowledge + typed ops
from . import movement  # H03: fixed-precision duration + known/actual route planners
from . import world_refs  # H04: typed ValueRef registry + dependency keys
from . import actors  # H04: ActorRegistry + legacy name resolver + typed ops
from . import information  # H04: InformationState validity/lineage/series + typed ops
from . import cognition  # H04: sparse cognition + acquisition provenance + belief authority
from . import relationships  # H04: sparse relationship deltas
from . import npc_projection  # H04: read-only CurrentNpcProjection
from . import npc_position  # H07: effective NPC position (canonical override > routine > untracked)
from . import place_display  # H07: PlaceId vs knowledge-based player display name
from . import predicates  # H04: tri-state typed predicates + time wakeups
from . import events  # H04: event templates/instances, phase/case selection, typed steps
from . import world_progression  # H04: DependencyIndex + pure WorldProgressionProjection
from . import action_intent  # H05: ActionReferenceManifest + ResolvedActionIntent
from . import player_world  # H05: session world mode + route policy + player movement/time planning
from . import turn_world  # H05: narration overlay, observation, same-minute tie, continuation, projected media
from . import world_lifecycle  # H06: definition bind + genesis initialization + lifecycle
from . import cache_source  # H06: FrozenCacheBuildSource
from . import admin_world_edit  # H06: typed ADMIN_EDIT service
from . import speaker_card  # SVG 화자 카드(DYNAMIC 전용, 시나리오 speaker_card)
from . import display_board  # 디스플레이 상태판 SVG(엔진 공통, v7.10)
from . import logs  # 구조 로그(세션 logs/·전역 logs/bot.jsonl)
from . import session_removal  # 세션 종료·제거 통합 관리(!세션관리)
from .cost import (
    extract_token_usage,
    format_cost,
    calculate_text_gen_cost_breakdown,
    accrue,
    usd_to_krw,
    format_usd,
    calculate_image_gen_cost,
    calculate_upload_cost,
    calculate_upload_cost_usd,
    calculate_storage_cost_usd,
    calculate_storage_cost,
    calculate_cost,
    build_cache_cost_embed,
    build_text_gen_cost_embed,
    build_image_gen_cost_embed,
    build_compression_cost_embed,
    build_turn_cost_embed,
    provider_cost_summary,
    provider_cost_by_session,
    player_ink_summary,
    mark_auto_mode_start,
    auto_mode_used_krw,
    auto_cost_cap_reached,
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
    save_session_data_strict,
    session_io_lock,
    write_session_strict_locked,
    SessionPersistenceError,
    process_cache_deletion,
    load_authorized_users,
    save_authorized_users,
    AUTHORIZED_USERS_PATH,
)
from .cache import (
    build_scenario_cache_text,
    update_session_cache_state,
    remaining_ttl,
    is_cache_expired,
    is_session_open,
    restore_sessions_from_disk,
)
from .prompt import PromptBuilder, build_compression_prompt
from .dialogue import (
    pick_status_message,
    send_layer_status,
    WaitingStatus,
    LAYER_STATUS_MESSAGES,
    WAITING_TIPS,
    DIALOGUE_MARKER_PATTERN,
    parse_dialogue_paragraph,
    format_dialogue_block,
    merge_consecutive_dialogues,
    maybe_send_speaker_image,
    stream_text_to_channel,
    send_streamed,
    strip_unauthorized_pc_dialogue,
    send_status_message,
    clear_status_message,
    clear_messages,
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
    status_roll_modifier,
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
    "creation", "profiles", "profile_gen", "profile_runner", "profile_ai", "quest", "quest_filter", "memory_plan", "start_frame", "koreantext", "places",
    "open_manager", "ProfileHomeView", "profile_creation_ui", "session_flow", "intro",
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
    "estimate_input_tokens", "estimate_turn", "estimate_session_open", "estimate_tts", "format_estimate",
    "record_actual_input", "get_calibration",
    "COMPRESSION_INTERVAL",
    "REWIND_MAX_TURNS", "TRACKED_PATHS", "capture_state", "diff_state",
    "record_delta", "record_full_log", "archive_removed", "available_range",
    "serialize_log_entries", "read_jsonl",
    "COMMON_EXTRACTION_TARGETS", "EXTRACTION_RESPONSE_SCHEMA", "THRESHOLDS",
    "get_thresholds", "build_extraction_targets", "parse_extraction",
    "apply_extraction", "apply_companions", "release_resident_companions",
    "normalize_status_item_effects", "apply_normalized_status_item",
    "normalize_companions", "apply_normalized_companions", "normalize_location",
    "to_world_timeline", "summarize_for_report", "build_extraction_limits",
    "cost_to_ink", "ink_to_krw", "ink_to_net_krw", "plan_catalog",
    "can_afford", "format_ink", "refund_ink", "accounts",
    "TRPG_SAFETY_SETTINGS", "PRICING_1M", "IMAGE_OUTPUT_TOKENS_BY_RES",
    "TTS_MODEL", "TTS_NARRATOR_VOICE", "TTS_LANGUAGE_CODE",
    "TTS_NARRATION_VOLUME", "TTS_PCM_BYTES_PER_SEC", "TTS_STYLE_PROMPT", "TTS_VOICES",
    # models
    "TRPGSession",
    # cost
    "extract_token_usage", "format_cost", "calculate_text_gen_cost_breakdown", "accrue",
    "provider_cost_summary", "provider_cost_by_session", "player_ink_summary", "mark_auto_mode_start", "auto_mode_used_krw", "auto_cost_cap_reached", "usd_to_krw", "format_usd", "calculate_image_gen_cost",
    "calculate_upload_cost", "calculate_upload_cost_usd", "calculate_storage_cost_usd", "calculate_storage_cost", "calculate_cost",
    # io
    "SCHEMA_VERSION", "migrate_session_data", "SESSION_FIELDS", "SESSION_RESET_FIELDS",
    "write_log", "write_cost_log", "load_scenario_from_file", "get_available_scenarios",
    "save_session_data", "save_session_data_strict", "session_io_lock", "write_session_strict_locked", "SessionPersistenceError", "process_cache_deletion",
    "load_authorized_users", "save_authorized_users", "AUTHORIZED_USERS_PATH",
    # cache
    "build_scenario_cache_text", "update_session_cache_state", "remaining_ttl", "is_cache_expired", "is_session_open", "restore_sessions_from_disk",
    # prompt
    "PromptBuilder", "build_compression_prompt",
    # dialogue
    "DIALOGUE_MARKER_PATTERN", "parse_dialogue_paragraph", "format_dialogue_block",
    "merge_consecutive_dialogues", "maybe_send_speaker_image", "stream_text_to_channel", "send_streamed",
    "strip_unauthorized_pc_dialogue", "pick_status_message", "send_layer_status", "WaitingStatus",
    "LAYER_STATUS_MESSAGES", "WAITING_TIPS", "send_status_message", "clear_status_message", "clear_messages",
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
    "status_roll_modifier", "resolve_char_name", "resolve_pc", "decompose_hangul", "suggest_commands",
]
