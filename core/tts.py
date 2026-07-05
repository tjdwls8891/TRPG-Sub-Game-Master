# TTS(음성 더빙) — Gemini native TTS로 한국어 텍스트를 합성하고 믹서 표준 PCM으로 변환
#
# [실험 기능] 단일 나레이터 보이스. NPC 개별 보이스·자동 GM 연동은 추후 단계.
#
# 파이프라인: 텍스트 정제 → Gemini TTS 호출(오디오 출력) → 24kHz mono PCM 추출
#             → 48kHz stereo로 리샘플(audioop) → 믹서 voice 큐에 적재 가능한 bytes 반환
#
# NOTE: audioop은 Python 3.13에서 제거 예정(현재 3.12 동작). 이전 시 리샘플만 교체.

import re
import asyncio

import audioop
from google.genai import types

from .constants import (
    TTS_MODEL, TTS_NARRATOR_VOICE, TTS_LANGUAGE_CODE, TTS_STYLE_PROMPT,
    TRPG_SAFETY_SETTINGS,
)
from .cost import calculate_text_gen_cost_breakdown
from .io import write_log

# 믹서 표준 출력 포맷
_TARGET_RATE = 48000
_TARGET_CHANNELS = 2
_SAMPLE_WIDTH = 2


def clean_text_for_tts(text: str) -> str:
    """
    TTS에 넘기기 전 비음성 마크업을 제거한다.
    인용 접두(> ), 마크다운 기호(*_`#), 말풍선/헤더 장식(「」▍), 과도한 공백 정리.
    """
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^>\s*", "", t)               # 인용 접두
    t = re.sub(r"[*_`#>]", "", t)             # 마크다운 기호
    for ch in ("「", "」", "▍"):
        t = t.replace(ch, "")
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _extract_audio(response):
    """Gemini 응답에서 (pcm_bytes, src_rate)를 추출한다. 실패 시 (b'', 0)."""
    try:
        parts = response.candidates[0].content.parts
    except (AttributeError, IndexError, TypeError):
        return b"", 0
    for part in parts or []:
        inline = getattr(part, "inline_data", None)
        if inline is None or not getattr(inline, "data", None):
            continue
        data = inline.data
        mime = getattr(inline, "mime_type", "") or ""
        m = re.search(r"rate=(\d+)", mime)
        src_rate = int(m.group(1)) if m else 24000  # Gemini TTS 기본 24kHz
        return data, src_rate
    return b"", 0


def _to_mixer_pcm(pcm: bytes, src_rate: int) -> bytes:
    """24kHz mono s16le 등 → 48kHz stereo s16le 로 변환."""
    if not pcm:
        return b""
    # mono(1ch) 기준 리샘플 후 스테레오 복제
    if src_rate != _TARGET_RATE:
        pcm, _ = audioop.ratecv(pcm, _SAMPLE_WIDTH, 1, src_rate, _TARGET_RATE, None)
    pcm = audioop.tostereo(pcm, _SAMPLE_WIDTH, 1, 1)
    return pcm


async def synthesize_tts_pcm(bot, text: str, voice_name: str = None):
    """
    텍스트를 Gemini TTS로 합성해 (48kHz stereo PCM bytes, 비용KRW, in_tokens, out_tokens) 반환.

    실패(빈 텍스트·API 오류·오디오 없음) 시 (b'', 0.0, 0, 0). 게임 진행에는 영향 없음.
    비용은 호출 측에서 session.total_cost에 누적·로그한다(이 함수는 세션을 만지지 않음).
    """
    cleaned = clean_text_for_tts(text)
    if not cleaned:
        return b"", 0.0, 0, 0

    # 공식 "강력 프롬프트" 구조: 스타일 프로필(AUDIO PROFILE/SCENE/DIRECTOR'S NOTES) 뒤에
    # `#### TRANSCRIPT` 섹션으로 본문을 둔다. 모델은 TRANSCRIPT의 따옴표 안 본문만 낭독하고
    # 프로필은 톤 지시로만 사용한다. 내부 큰따옴표는 래퍼와 충돌하므로 치환한다.
    if TTS_STYLE_PROMPT:
        body = cleaned.replace('"', "'")
        prompt = f'{TTS_STYLE_PROMPT}\n\n#### TRANSCRIPT\n"{body}"'
    else:
        prompt = cleaned

    voice = voice_name or TTS_NARRATOR_VOICE
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            language_code=TTS_LANGUAGE_CODE,
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            ),
        ),
        safety_settings=TRPG_SAFETY_SETTINGS,
    )

    try:
        response = await asyncio.to_thread(
            bot.genai_client.models.generate_content,
            model=TTS_MODEL,
            contents=prompt,
            config=config,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[TTS] 합성 호출 실패(무시): {type(e).__name__} - {e}")
        return b"", 0.0, 0, 0

    raw_pcm, src_rate = _extract_audio(response)
    if not raw_pcm:
        print("[TTS] 응답에 오디오 데이터가 없습니다.")
        return b"", 0.0, 0, 0

    try:
        pcm48 = _to_mixer_pcm(raw_pcm, src_rate)
    except Exception as e:  # noqa: BLE001
        print(f"[TTS] 리샘플 실패(무시): {e}")
        return b"", 0.0, 0, 0

    # 비용 산출 (출력은 오디오 토큰 = candidates_token_count)
    cost = 0.0
    in_tokens = out_tokens = 0
    try:
        meta = response.usage_metadata
        in_tokens = getattr(meta, "prompt_token_count", 0) or 0
        out_tokens = getattr(meta, "candidates_token_count", 0) or 0
        breakdown = calculate_text_gen_cost_breakdown(
            TTS_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
        cost = breakdown["total_krw"]
    except Exception as e:  # noqa: BLE001
        print(f"[TTS] 비용 산출 실패(무시): {e}")

    return pcm48, cost, in_tokens, out_tokens
