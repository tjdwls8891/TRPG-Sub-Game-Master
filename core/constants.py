# 전역 상수 — 모델 ID, 환율, 안전 설정, 과금 단가표
import os

from google.genai import types

# ========== [전역 상수(Constants)] ==========
# NOTE: 프로젝트 버전 — 표기의 단일 출처(Single Source of Truth).
#       문서(CLAUDE.md·DEVLOG.md)의 버전 표기는 이 값과 일치시킨다.
#       체계: MAJOR.MINOR.PATCH
#         MAJOR — 저장 스키마/세션 구조 변경(SCHEMA_VERSION 증가), 명령어 체계 개편 등 비호환 변경
#         MINOR — 하위호환 기능 추가, 프롬프트·룰북 변경 (재시작 또는 캐시 재발급 필요)
#         PATCH — 버그 수정, 문구 조정, 리팩터링 (핫스왑 수준)
__version__ = "5.13.1"

DEFAULT_MODEL = "gemini-3-flash-preview"
LOGIC_MODEL = "gemini-3-flash-preview"
# 프로필 생성 AI 모듈(검증·병합) 전용 모델.
# 기획 규정상 프로필 생성 단순호출은 무료 제공 대상이므로,
# 저비용 모델이 확정되면 이 상수만 교체한다.
PROFILE_AI_MODEL = "gemini-3-flash-preview"
# LOGIC_MODEL = "gemini-3-pro-preview"
EXCHANGE_RATE = 1500.0

# ── 캐시 최소 토큰 ──
# 제미나이 컨텍스트 캐싱의 모델별 최소 입력 토큰 요건.
#   Gemini 3 Flash Preview / 2.5 Flash : 1,024
#   Gemini 3 Pro Preview  / 2.5 Pro    : 4,096
# 과거 코드는 32,768로 잡혀 있었으나 이는 현행 기준의 32배로, 미달분을 마침표
# 패딩으로 채우는 만큼 캐시 생성비·시간당 저장비·매 턴 읽기비가 모두 부풀었다.
# 값을 낮추면 세 비용이 동시에 비례 절감된다.
# 운영 중 캐시 생성이 최소 요건 오류로 실패하면 .env에 MIN_CACHE_TOKENS를 지정해
# 재배포 없이 되돌릴 수 있다.
MIN_CACHE_TOKENS = int(os.getenv("MIN_CACHE_TOKENS", "1024"))

# 캐시 TTL(초). 세션 오픈 유지 시간이자 만료 예정 시점 산출의 기준.
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "21600"))

# ── 게임머니 '잉크' ──
INK_UNIT_KRW = 10   # 판매가: 1잉크 = 10원
INK_NET_KRW = 7     # 디스코드 결제 수수료 30% 차감 후 실수령: 1잉크 = 7원
                    # → API 원화 비용 X의 청구액 = ceil(X / 7) 잉크
INK_PLANS = [100, 300, 500, 1000, 3000, 5000]  # 충전 플랜 (잉크)

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
    },
    # NOTE: TTS(음성 합성) 모델 — 출력은 '오디오 토큰' 과금. 공식가(2.5 Flash Preview TTS): 입력 텍스트 $0.50/1M, 출력 오디오 $10.00/1M.
    "gemini-2.5-flash-preview-tts": {
        "INPUT": 0.50,
        "OUTPUT": 10.00,
        "CACHE_READ": 0.05,
        "CACHE_STORAGE_PER_HOUR": 1.00
    }
}

IMAGE_MODEL = "gemini-3.1-flash-image-preview"

# ========== [TTS(음성 더빙) — 실험 기능] ==========
# WARNING: TTS_MODEL은 사용 중인 API 키에서 실제로 제공되는 TTS 모델 ID와 일치해야 한다.
#          (다른 모델 네이밍 체계를 쓰면 여기만 바꾸면 됨)
TTS_MODEL = "gemini-2.5-flash-preview-tts"
# 나레이터 기본 보이스 (Gemini prebuilt voice 이름). NPC 개별 보이스는 추후 단계에서 도입.
TTS_NARRATOR_VOICE = "Algenib"
TTS_LANGUAGE_CODE = "ko-KR"
# Gemini TTS prebuilt 보이스 30종 (미리듣기: https://aistudio.google.com/generate-speech).
# !더빙테스트의 보이스 인자 검증에 사용.
TTS_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede",
    "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
    "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
    "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]
# 내레이션 재생 볼륨 (믹서 voice 레이어에 적용, 0.0~1.0+)
TTS_NARRATION_VOLUME = 0.5
# 믹서 표준 PCM(48kHz·stereo·s16le) 1초당 바이트 수. 음성 길이(초) = len(pcm) / 이 값.
TTS_PCM_BYTES_PER_SEC = 48000 * 2 * 2
# 합성 스타일 프로필 — 공식 "강력 프롬프트" 구조(AUDIO PROFILE / SCENE / DIRECTOR'S NOTES)를 차용.
# 코드가 이 프로필 뒤에 `#### TRANSCRIPT` 섹션으로 본문을 붙여 전송한다(본문만 낭독, 프로필은 톤 지시로만 사용).
TTS_STYLE_PROMPT = """# AUDIO PROFILE: 게임 마스터 내레이터 (생존 호러)

### DIRECTOR'S NOTES
Style:
* 어둡고 진지하다. 중저음의 낮게 깔린 목소리로 감정을 절제하며 서늘하게 전달한다.
* 또렷한 육성으로 단정적으로 읽는다. 과장된 호러 연기, 비명, 떨리는 목소리, 속삭임은 금지. 정적과 여백으로 긴장을 만든다.

Pace: 차분하되 약간 빠른 편으로, 또렷하고 자연스럽게 진행한다. 늘어지지 않는다.

Delivery: 명료한 한국어 발음. 문장 끝을 무겁게 내려놓는다. 단정적이고 흔들림 없는 어조."""

# NOTE: Gemini 이미지 출력 모델의 해상도별 출력 토큰 표 (공식 가격 페이지 기준).
#       응답에서 usage_metadata가 비었을 때의 폴백 추산값으로 사용한다.
IMAGE_OUTPUT_TOKENS_BY_RES = {
    "0.5K": 747,    # 512px ≈ $0.045
    "1K":   1120,   # 1024x1024 ≈ $0.067
    "2K":   1680,   # 2048x2048 ≈ $0.101
    "4K":   2520,   # 4096x4096 ≈ $0.151
}
