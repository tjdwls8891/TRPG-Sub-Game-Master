# 전역 상수 — 모델 ID, 환율, 안전 설정, 과금 단가표
from google.genai import types

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
