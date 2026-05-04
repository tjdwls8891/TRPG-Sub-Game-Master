# core 패키지 — 하위 모듈의 모든 심볼을 re-export하여 기존 `import core` 참조를 유지
#
# 분리된 서브모듈:
#   constants  — 전역 상수 (모델 ID, 환율, 안전 설정, 과금 단가표)
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
    DEFAULT_MODEL,
    LOGIC_MODEL,
    IMAGE_MODEL,
    EXCHANGE_RATE,
    TRPG_SAFETY_SETTINGS,
    PRICING_1M,
    IMAGE_OUTPUT_TOKENS_BY_RES,
)
from .models import TRPGSession
from .cost import (
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
)
from .media import send_image_by_keyword, PlaylistManager
from .ui import (
    _cleanup_session_memory,
    ChannelSelect,
    ChannelDeleteView,
    GeneralDiceView,
    DiceView,
)
from .utils import get_uid_by_char_name, generate_character_details, get_merged_status_effects

__all__ = [
    # constants
    "DEFAULT_MODEL", "LOGIC_MODEL", "IMAGE_MODEL", "EXCHANGE_RATE",
    "TRPG_SAFETY_SETTINGS", "PRICING_1M", "IMAGE_OUTPUT_TOKENS_BY_RES",
    # models
    "TRPGSession",
    # cost
    "format_cost", "calculate_text_gen_cost_breakdown", "calculate_image_gen_cost",
    "calculate_upload_cost", "calculate_storage_cost", "calculate_cost",
    # io
    "SCHEMA_VERSION", "SESSION_FIELDS", "SESSION_RESET_FIELDS",
    "write_log", "write_cost_log", "load_scenario_from_file", "get_available_scenarios",
    "save_session_data", "process_cache_deletion",
    # cache
    "build_scenario_cache_text", "update_session_cache_state", "restore_sessions_from_disk",
    # prompt
    "PromptBuilder", "build_compression_prompt",
    # dialogue
    "DIALOGUE_MARKER_PATTERN", "parse_dialogue_paragraph", "format_dialogue_block",
    "merge_consecutive_dialogues", "maybe_send_speaker_image", "stream_text_to_channel",
    # media
    "send_image_by_keyword", "PlaylistManager",
    # ui
    "_cleanup_session_memory", "ChannelSelect", "ChannelDeleteView",
    "GeneralDiceView", "DiceView",
    # utils
    "get_uid_by_char_name", "generate_character_details",
]
