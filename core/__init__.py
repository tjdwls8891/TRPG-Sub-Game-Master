# core 패키지 — 하위 모듈의 모든 심볼을 re-export하여 기존 `import core` 참조를 유지
#
# 분리된 서브모듈:
#   constants  — 전역 상수 (모델 ID, 환율, 안전 설정, 과금 단가표, 잉크 환산 상수)
#   ink        — 게임머니 '잉크' 환산 (원화 비용 -> 차감 잉크, 충전 플랜 가격)
#   extraction — 추출층위 (묘사 출력물에서 세계 상태·수치 추출, 임계값 대조)
#   rewind     — 되감기 델타 로그 (턴별 변화 기록, 전 턴 로그 보존)
#   estimate   — 비용 예측 (입력 실측 + 출력 이동평균, action별 범위 산출)
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
    INK_UNIT_KRW,
    INK_NET_KRW,
    INK_PLANS,
    DEFAULT_MODEL,
    LOGIC_MODEL,
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
    format_estimate,
)
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
    "DEFAULT_MODEL", "LOGIC_MODEL", "IMAGE_MODEL", "EXCHANGE_RATE",
    "MIN_CACHE_TOKENS", "INK_UNIT_KRW", "INK_NET_KRW", "INK_PLANS",
    # ink / accounts
    "CHARS_TO_TOKENS", "CONSERVATIVE_FACTOR", "update_stats",
    "estimate_input_tokens", "estimate_turn", "estimate_session_open", "format_estimate",
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
    "SCHEMA_VERSION", "SESSION_FIELDS", "SESSION_RESET_FIELDS",
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
    "strip_unauthorized_pc_dialogue", "send_status_message", "clear_status_message",
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
