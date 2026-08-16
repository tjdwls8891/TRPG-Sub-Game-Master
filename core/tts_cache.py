# TTS 사전 저장 — 고정 메시지의 합성 결과를 재사용해 비용을 줄인다
#
# [적용 대상]
#   공통 소개글, 확인 메시지, 대기 문구처럼 매 세션 동일한 텍스트.
#   세션 생성 플로우의 소개 스트리밍이 최대 수혜처다 — 모든 세션이 같은
#   문구를 쓰므로 첫 생성 이후 전부 무료가 된다.
#
# [캐시 키]
#   (텍스트, 목소리) 해시. 목소리가 다르면 다른 음성이므로 함께 넣는다.
#   PCM 원본을 그대로 저장한다. 믹서가 요구하는 형식(48kHz stereo)으로
#   이미 변환된 상태라 재생 시 추가 처리가 없다.
import hashlib
import os

CACHE_DIR = os.path.join("media", "_tts_cache")

# 캐시 파일 하나의 상한(바이트). 지나치게 긴 텍스트는 캐시하지 않는다.
MAX_CACHE_BYTES = 8 * 1024 * 1024

# 캐시 대상으로 삼을 최대 텍스트 길이. 턴 묘사처럼 매번 다른 텍스트는
# 캐시해도 히트하지 않으므로 짧은 고정 문구만 받는다.
MAX_CACHEABLE_CHARS = 600


def _key(text: str, voice: str) -> str:
    raw = f"{voice}\x00{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _path(text: str, voice: str) -> str:
    return os.path.join(CACHE_DIR, voice or "_default", f"{_key(text, voice)}.pcm")


def is_cacheable(text: str) -> bool:
    """캐시 대상인지. 길거나 비어 있으면 제외한다."""
    t = (text or "").strip()
    return 0 < len(t) <= MAX_CACHEABLE_CHARS


def load(text: str, voice: str = "") -> bytes | None:
    """캐시된 PCM을 읽는다. 없으면 None."""
    if not is_cacheable(text):
        return None
    path = _path(text, voice)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[TTS캐시] 읽기 실패: {e}")
        return None


def store(text: str, voice: str, pcm: bytes) -> bool:
    """합성 결과를 캐시에 저장한다."""
    if not is_cacheable(text) or not pcm:
        return False
    if len(pcm) > MAX_CACHE_BYTES:
        return False
    path = _path(text, voice)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(pcm)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[TTS캐시] 저장 실패: {e}")
        return False


async def synthesize_cached(bot, text: str, voice_name: str = None):
    """캐시를 먼저 보고, 없으면 합성 후 저장한다.

    NOTE: core.tts.synthesize_tts_pcm과 동일한 4-튜플을 반환한다.
          캐시 히트 시 비용·토큰은 전부 0이다 — 그것이 이 모듈의 목적이다.

    Returns:
        (pcm bytes, 비용KRW, in_tokens, out_tokens)
    """
    from .tts import synthesize_tts_pcm, TTS_NARRATOR_VOICE

    voice = voice_name or TTS_NARRATOR_VOICE
    hit = load(text, voice)
    if hit is not None:
        return hit, 0.0, 0, 0

    pcm, cost, in_t, out_t = await synthesize_tts_pcm(bot, text, voice_name=voice)
    if pcm:
        store(text, voice, pcm)
    return pcm, cost, in_t, out_t


def cache_stats() -> dict:
    """캐시 현황. 운영 점검용."""
    files = 0
    total = 0
    if os.path.isdir(CACHE_DIR):
        for root, _dirs, names in os.walk(CACHE_DIR):
            for n in names:
                if n.endswith(".pcm"):
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(root, n))
                    except Exception:
                        pass
    return {"files": files, "bytes": total, "mb": round(total / 1024 / 1024, 2)}
