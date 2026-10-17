# TTS(음성 더빙) — Gemini native TTS로 한국어 텍스트를 합성하고 믹서 표준 PCM으로 변환
#
# [실험 기능] 단일 나레이터 보이스. NPC 개별 보이스 연동은 추후 단계.
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


def tts_usage_cost(response) -> tuple:
    """(krw, usd, in_tokens, audio_out_tokens) — TTS 모델 단가(오디오 출력)로만 계산. usage 없으면 None."""
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    in_tokens = getattr(meta, "prompt_token_count", 0) or 0
    out_tokens = getattr(meta, "candidates_token_count", 0) or 0
    bd = calculate_text_gen_cost_breakdown(TTS_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
    return bd["total_krw"], bd["total_usd"], in_tokens, out_tokens


class _TTSObserver:
    """TTS provider attempt 관측자 — 시도 계수 · 정상 응답 usage 기록 · 타임아웃 뒤 늦은 응답 1회 관측(P1).

    call_with_retry는 on_attempt의 __self__.track_inflight로 늦은 호출을 넘긴다. 늦은 응답의 usage는 같은
    operation_id:attempt 멱등키로 1회만 CostEvent가 된다(이미 동결된 턴 Settlement는 바꾸지 않는다 — 운영 사실).
    끝내 usage를 알 수 없으면 0원을 만들지 않고 구조 로그 cost.unknown으로 남긴다.
    """

    def __init__(self, bot, ctx):
        self.bot, self.ctx = bot, ctx
        self.operation = getattr(ctx, "operation", None) or "TTS"
        self.n = 0
        self._late = set()

    def on_attempt(self, **_kw):
        self.n += 1

    def record(self, response, attempt, *, extra=None) -> tuple:
        try:
            got = tts_usage_cost(response)
        except Exception as e:  # noqa: BLE001
            print(f"[TTS] 비용 산출 실패(무시): {e}")
            got = None
        if got is None:
            self.unknown(attempt, "NO_USAGE_METADATA")
            return 0.0, 0, 0
        krw, usd, in_t, out_t = got
        if self.ctx is not None:
            try:
                from . import cost_ledger
                cost_ledger.record_context_event(
                    self.bot, self.ctx, cost_usd=usd, cost_krw=krw,
                    usage_source=cost_ledger.SOURCE_PROVIDER_METADATA, provider_attempt=attempt,
                    input_tokens=in_t, audio_output_tokens=out_t, extra_metadata=extra)
            except Exception as _e:  # noqa: BLE001
                print(f"[TTS] shadow 관측 실패(무시): {type(_e).__name__} - {_e}")
        return krw, in_t, out_t

    def unknown(self, attempt, reason) -> None:
        from .cost_ledger import log_unknown_cost
        c = self.ctx
        log_unknown_cost(self.operation, operation_id=getattr(c, "operation_id", None), attempt=attempt,
                         session_id=getattr(c, "session_id", None), transaction_id=getattr(c, "transaction_id", None),
                         model=TTS_MODEL, reason=reason)

    def track_inflight(self, future, *, attempt=None, operation_id=None):
        pa = attempt or self.n or 1

        def _done(fut, _pa=pa):
            if _pa in self._late:
                return
            self._late.add(_pa)
            try:
                if fut.cancelled() or fut.exception() is not None:
                    self.unknown(_pa, "LATE_ATTEMPT_NO_RESPONSE")
                    return
                self.record(fut.result(), _pa, extra={"late_after_wrapper_timeout": True,
                                                      "gameplay_result_discarded": True})
            except Exception as e:  # noqa: BLE001
                print(f"[TTS] 늦은 attempt 관측 실패(무시): {type(e).__name__} - {e}")
        if future.done():
            _done(future)
        else:
            future.add_done_callback(_done)


async def synthesize_tts_pcm(bot, text: str, voice_name: str = None, *,
                             cost_context=None, style: str | None = None):
    """
    텍스트를 Gemini TTS로 합성해 (48kHz stereo PCM bytes, 비용KRW, in_tokens, out_tokens) 반환.

    실패(빈 텍스트·API 오류·오디오 없음) 시 (b'', 0.0, 0, 0). 게임 진행에는 영향 없음.
    비용은 호출 측에서 session.total_cost에 누적·로그한다(이 함수는 세션을 만지지 않음).

    WP-02: cost_context(ProviderCostContext)를 받으면 SDK를 소유한 이 헬퍼가
    provider attempt를 관측해 shadow CostEvent를 기록한다. 호출자가 operation
    (TTS_RUNTIME/TTS_TEST/TTS_PRESET_BUILD)·session·transaction·actor/billing을 정한다.

    P2: style = 공급자 자연어 스타일 지시(core.voice_profile — 마스터 안내·NPC 대사). None이면 기존 나레이터 프로필.
    """
    cleaned = clean_text_for_tts(text)
    if not cleaned:
        return b"", 0.0, 0, 0

    # 공식 "강력 프롬프트" 구조: 스타일 프로필(AUDIO PROFILE/SCENE/DIRECTOR'S NOTES) 뒤에
    # `#### TRANSCRIPT` 섹션으로 본문을 둔다. 모델은 TRANSCRIPT의 따옴표 안 본문만 낭독하고
    # 프로필은 톤 지시로만 사용한다. 내부 큰따옴표는 래퍼와 충돌하므로 치환한다.
    profile = style or TTS_STYLE_PROMPT
    if profile:
        body = cleaned.replace('"', "'")
        prompt = f'{profile}\n\n#### TRANSCRIPT\n"{body}"'
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

    # WP-02: SDK를 소유한 이 헬퍼가 provider attempt 를 관측한다.
    #        재시도가 있어도 성공한 attempt 번호를 정확히 남긴다.
    # P1: 래퍼 타임아웃 뒤 늦게 끝난 실제 호출도 같은 attempt id로 1회만 관측한다(_TTSObserver.track_inflight).
    obs = _TTSObserver(bot, cost_context)
    _op_id = (cost_context.ensure_operation_id()
              if cost_context is not None else None)
    try:
        from .resilience import call_with_retry
        _ok, response = await call_with_retry(
            lambda: asyncio.to_thread(
                bot.genai_client.models.generate_content,
                model=TTS_MODEL,
                contents=prompt,
                config=config,
            ),
            layer="media", session_id=getattr(cost_context, "session_id", None) or "",
            on_attempt_result=obs.on_attempt, operation_id=_op_id,
        )
        if not _ok:
            return (b"", 0.0, 0, 0)
    except Exception as e:  # noqa: BLE001
        print(f"[TTS] 합성 호출 실패(무시): {type(e).__name__} - {e}")
        return b"", 0.0, 0, 0

    # P1: provider usage는 오디오 추출·리샘플 **전에** 기록한다 — 가공 실패가 실제 비용 유실이 되지 않게.
    cost, in_tokens, out_tokens = obs.record(response, max(1, obs.n))

    raw_pcm, src_rate = _extract_audio(response)
    if not raw_pcm:
        print("[TTS] 응답에 오디오 데이터가 없습니다.")
        return b"", cost, in_tokens, out_tokens        # 비용은 발생했다(호출자가 턴 비용에 합산)

    try:
        pcm48 = _to_mixer_pcm(raw_pcm, src_rate)
    except Exception as e:  # noqa: BLE001
        print(f"[TTS] 리샘플 실패(무시): {e}")
        return b"", cost, in_tokens, out_tokens

    return pcm48, cost, in_tokens, out_tokens
