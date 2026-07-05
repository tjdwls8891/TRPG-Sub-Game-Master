# 오디오 믹서 — BGM/플리(base) 위에 효과음(effects)을 겹쳐 단일 PCM 스트림으로 송출
#
# [배경]
# discord.py의 VoiceClient는 동시에 단 하나의 AudioSource만 재생한다.
# 따라서 BGM이 흐르는 도중 효과음을 "겹쳐" 내보내려면, 봇이 직접 두 PCM 스트림을
# 20ms 프레임 단위로 합산(mix)하여 하나의 소스로 만들어 재생해야 한다.
#
# [구조]
#   VoiceClient.play(MixerAudioSource) 단 한 번.
#     - base    : 현재 BGM 또는 플리 트랙 (PCMVolumeTransformer). set_base()로 교체.
#     - effects : 활성 효과음들 (PCMBytesAudioSource). add_effect()로 추가, 소진 시 자동 제거.
#   매 read()마다 base + effects 프레임을 audioop.add로 합산해 반환한다.
#
# NOTE: audioop은 Python 3.13에서 제거 예정이다(현재 3.12에서 동작).
#       이전 시 numpy(int32 합산 후 클립) 등으로 _mix_frames만 교체하면 된다.

import os
import queue
import asyncio
import threading
import subprocess

import audioop
import discord

# 48kHz · 스테레오 · 16-bit · 20ms = 48000 * 0.02 * 2ch * 2bytes = 3840 bytes
FRAME_SIZE = 3840
SAMPLE_WIDTH = 2
SILENCE = b"\x00" * FRAME_SIZE

SFX_DIR = "media/_sfx"

# 효과음 raw PCM 캐시 — 매 재생마다 ffmpeg 프로세스를 새로 띄우는 비용(30~100ms)을 제거한다.
# key: (path, mtime) → value: bytes(s16le/48k/stereo)
_PCM_CACHE: dict = {}


def _pad_frame(frame: bytes) -> bytes:
    """프레임을 정확히 FRAME_SIZE로 맞춘다 (짧으면 0 패딩, 길면 잘라냄)."""
    if len(frame) == FRAME_SIZE:
        return frame
    if len(frame) < FRAME_SIZE:
        return frame + b"\x00" * (FRAME_SIZE - len(frame))
    return frame[:FRAME_SIZE]


def decode_to_pcm(path: str) -> bytes:
    """
    오디오 파일을 s16le/48k/stereo raw PCM으로 1회 디코드하여 캐시.

    블로킹 호출이므로 asyncio 루프에서는 asyncio.to_thread로 감싸 호출한다.
    """
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return b""
    cached = _PCM_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", path, "-f", "s16le", "-ar", "48000", "-ac", "2",
             "-loglevel", "quiet", "pipe:1"],
            capture_output=True,
            check=False,
        )
        pcm = proc.stdout or b""
    except Exception as e:  # noqa: BLE001
        print(f"[Mixer] 효과음 디코드 실패 {path}: {e}")
        pcm = b""

    _PCM_CACHE[key] = pcm
    return pcm


class PCMBytesAudioSource(discord.AudioSource):
    """메모리에 적재된 raw PCM(s16le/48k/stereo)을 20ms 프레임으로 흘려보내는 소스."""

    def __init__(self, pcm: bytes, volume: float = 1.0):
        self._pcm = pcm
        self._pos = 0
        self._volume = volume

    def read(self) -> bytes:
        chunk = self._pcm[self._pos:self._pos + FRAME_SIZE]
        if not chunk:
            return b""
        self._pos += FRAME_SIZE
        chunk = _pad_frame(chunk)
        if self._volume != 1.0:
            chunk = audioop.mul(chunk, SAMPLE_WIDTH, self._volume)
        return chunk

    def is_opus(self) -> bool:
        return False


class MixerAudioSource(discord.AudioSource):
    """
    base(BGM/플리) + effects(효과음)를 합산해 단일 PCM 스트림으로 송출하는 영속 소스.

    영속성: base도 effects도 없을 때는 무음 프레임을 반환해 보이스 연결을 유지한다.
    (트랙이 끝나도 소스가 종료되지 않으므로, 이후 효과음/다음 곡을 언제든 얹을 수 있다.)
    완전 종료는 VoiceClient.stop()/disconnect()로 수행한다.

    스레드 안전성: read()는 discord.py의 보이스 송신 스레드에서 호출된다.
    effect 추가는 thread-safe 큐로, base 교체는 lock으로 보호한다.
    """

    def __init__(self, bot):
        self.bot = bot
        self._base = None
        self._base_paused = False
        self._on_base_exhausted = None
        self._base_lock = threading.Lock()

        self._effects: list = []
        self._pending: "queue.SimpleQueue" = queue.SimpleQueue()

        # ── 음성(TTS) 레이어 — 순차 재생 큐 ──
        # 효과음(effects)이 동시 중첩인 것과 달리, 음성 문단은 한 번에 하나만 순차 재생한다.
        self._voice_current = None              # 현재 재생 중 음성 소스 (read 스레드 소유)
        self._voice_pending: "queue.SimpleQueue" = queue.SimpleQueue()
        self._voice_clear = False               # True면 현재+대기 음성 모두 중단

        # 효과음 재생 중 base를 일시 감쇠해 헤드룸 확보(클리핑 방지 + 효과음 명료도↑)
        self.duck_factor = 0.55
        # 음성 재생 중에는 base를 더 깊게 덕킹해 내레이션 명료도를 확보
        self.voice_duck_factor = 0.30

    # ── base(BGM/플리) 제어 ──────────────────────────────────────
    def set_base(self, source, on_exhausted=None):
        """base 소스를 교체한다. on_exhausted는 base가 자연 소진될 때 1회 호출된다(루프 스레드)."""
        with self._base_lock:
            old = self._base
            self._base = source
            self._base_paused = False
            self._on_base_exhausted = on_exhausted
        if old is not None and old is not source:
            self._safe_cleanup(old)

    def clear_base(self):
        self.set_base(None, None)

    def has_base(self) -> bool:
        with self._base_lock:
            return self._base is not None

    def pause_base(self):
        with self._base_lock:
            self._base_paused = True

    def resume_base(self):
        with self._base_lock:
            self._base_paused = False

    @property
    def base_volume_source(self):
        """볼륨/페이드 조절 대상이 되는 PCMVolumeTransformer base (없으면 None)."""
        with self._base_lock:
            if isinstance(self._base, discord.PCMVolumeTransformer):
                return self._base
        return None

    # ── effects(효과음) 제어 ─────────────────────────────────────
    def add_effect(self, source: discord.AudioSource):
        """효과음 소스를 다음 read() 시점부터 합산되도록 큐에 적재(thread-safe)."""
        self._pending.put(source)

    # ── voice(TTS 내레이션) 제어 ─────────────────────────────────
    def enqueue_voice(self, source: discord.AudioSource):
        """음성 소스를 순차 재생 큐에 적재(thread-safe). 큐에 쌓인 순서대로 하나씩 재생된다."""
        self._voice_pending.put(source)

    def clear_voice(self):
        """진행 중·대기 중 음성을 모두 중단(턴 취소·중단 시)."""
        self._voice_clear = True

    def is_voice_active(self) -> bool:
        return self._voice_current is not None or not self._voice_pending.empty()

    # ── 핵심: 프레임 합산 ────────────────────────────────────────
    def read(self) -> bytes:
        # 0) 음성 중단 요청 처리
        if self._voice_clear:
            self._voice_clear = False
            if self._voice_current is not None:
                self._safe_cleanup(self._voice_current)
                self._voice_current = None
            self._drain_voice_pending()

        # 1) 대기 중인 효과음 흡수
        while True:
            try:
                self._effects.append(self._pending.get_nowait())
            except queue.Empty:
                break

        # 2) base 프레임
        base_frame = None
        with self._base_lock:
            base = self._base
            paused = self._base_paused
        if base is not None and not paused:
            bf = base.read()
            if not bf:
                self._handle_base_exhausted(base)
            else:
                base_frame = _pad_frame(bf)

        # 3) effects 프레임 (소진된 것은 제거)
        effect_frames = []
        for eff in list(self._effects):
            ef = eff.read()
            if not ef:
                self._effects.remove(eff)
                self._safe_cleanup(eff)
                continue
            effect_frames.append(_pad_frame(ef))

        # 4) voice 프레임 (순차: 현재 소진 시 다음 큐로)
        voice_frame = self._read_voice_frame()

        # 5) 합산
        if base_frame is None and not effect_frames and voice_frame is None:
            return SILENCE  # 무음으로 연결 유지

        # base 덕킹: 음성 재생 중이면 더 깊게, 아니면 효과음 기준
        if base_frame is not None:
            if voice_frame is not None and self.voice_duck_factor < 1.0:
                base_frame = audioop.mul(base_frame, SAMPLE_WIDTH, self.voice_duck_factor)
            elif effect_frames and self.duck_factor < 1.0:
                base_frame = audioop.mul(base_frame, SAMPLE_WIDTH, self.duck_factor)

        mixed = base_frame if base_frame is not None else SILENCE
        for ef in effect_frames:
            mixed = audioop.add(mixed, ef, SAMPLE_WIDTH)
        if voice_frame is not None:
            mixed = audioop.add(mixed, voice_frame, SAMPLE_WIDTH)
        return mixed

    def _read_voice_frame(self):
        """현재 음성 소스에서 한 프레임 읽기. 소진 시 다음 큐 항목으로 넘어간다."""
        if self._voice_current is None:
            try:
                self._voice_current = self._voice_pending.get_nowait()
            except queue.Empty:
                return None
        vf = self._voice_current.read()
        if not vf:
            self._safe_cleanup(self._voice_current)
            self._voice_current = None
            try:
                self._voice_current = self._voice_pending.get_nowait()
            except queue.Empty:
                return None
            vf = self._voice_current.read()
            if not vf:
                self._safe_cleanup(self._voice_current)
                self._voice_current = None
                return None
        return _pad_frame(vf)

    def _drain_voice_pending(self):
        while True:
            try:
                self._safe_cleanup(self._voice_pending.get_nowait())
            except queue.Empty:
                break

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        # VoiceClient.stop()/교체 시 discord.py가 호출. 자식 소스 정리.
        with self._base_lock:
            base = self._base
            self._base = None
        if base is not None:
            self._safe_cleanup(base)
        for eff in list(self._effects):
            self._safe_cleanup(eff)
        self._effects.clear()
        if self._voice_current is not None:
            self._safe_cleanup(self._voice_current)
            self._voice_current = None
        self._drain_voice_pending()

    # ── 내부 ────────────────────────────────────────────────────
    def _handle_base_exhausted(self, base):
        with self._base_lock:
            if self._base is base:
                self._base = None
                cb = self._on_base_exhausted
                self._on_base_exhausted = None
            else:
                cb = None
        self._safe_cleanup(base)
        if cb is not None:
            try:
                self.bot.loop.call_soon_threadsafe(cb)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _safe_cleanup(source):
        try:
            source.cleanup()
        except Exception:  # noqa: BLE001
            pass


# ========== [헬퍼] ==========

def get_mixer(vc) -> "MixerAudioSource | None":
    """VoiceClient에서 활성 MixerAudioSource를 얻는다 (없으면 None)."""
    if vc and isinstance(getattr(vc, "source", None), MixerAudioSource):
        return vc.source
    return None


def ensure_mixer(bot, vc) -> "MixerAudioSource":
    """
    VoiceClient에 MixerAudioSource가 재생 중이면 그대로 반환,
    아니면 새로 생성해 play()한 뒤 반환한다.
    """
    mx = get_mixer(vc)
    if mx is not None:
        return mx
    # 방어적: 혹시 다른 소스가 재생 중이면 정지 후 믹서로 교체
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    mx = MixerAudioSource(bot)
    vc.play(mx)
    return mx


def active_volume_source(vc):
    """
    볼륨/페이드 조절 대상 PCMVolumeTransformer를 반환.
    믹서 사용 시 base, 레거시 직접 재생 시 vc.source. 없으면 None.
    """
    src = getattr(vc, "source", None) if vc else None
    if isinstance(src, MixerAudioSource):
        return src.base_volume_source
    if isinstance(src, discord.PCMVolumeTransformer):
        return src
    return None


async def preload_sfx(name: str = "dice"):
    """봇 시작 시 호출하면 효과음을 미리 디코드해 첫 재생 지연을 없앤다."""
    path = os.path.join(SFX_DIR, f"{name}.mp3")
    if os.path.exists(path):
        await asyncio.to_thread(decode_to_pcm, path)


async def play_sfx_on_vc(vc, name: str = "dice", volume: float = 0.9) -> bool:
    """
    주어진 VoiceClient의 믹서에 효과음을 얹는다.
    보이스 미연결/믹서 부재/파일 없음 시 조용히 False (게임 진행 무영향).
    """
    # fire-and-forget(create_task)로 호출되므로 어떤 예외도 밖으로 내보내지 않는다.
    try:
        mixer = get_mixer(vc)
        if mixer is None:
            return False
        path = os.path.join(SFX_DIR, f"{name}.mp3")
        if not os.path.exists(path):
            return False
        pcm = await asyncio.to_thread(decode_to_pcm, path)
        if not pcm:
            return False
        mixer.add_effect(PCMBytesAudioSource(pcm, volume=volume))
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[Mixer] 효과음 재생 실패(무시): {e}")
        return False


async def play_dice_sfx(bot, guild, delay: float = 0.5) -> bool:
    """
    주사위 버튼용 편의 함수 — 길드의 VoiceClient 믹서에 dice 효과음 송출.

    create_task로 fire-and-forget 호출되므로 delay 동안 sleep해도 인터랙션 응답을 막지 않는다.
    delay(기본 0.5초): 버튼 누름 시점부터 효과음 재생까지의 지연.
    """
    if delay and delay > 0:
        await asyncio.sleep(delay)
    vc = guild.voice_client if guild else None
    # 주사위 효과음 볼륨은 50%로 고정.
    return await play_sfx_on_vc(vc, "dice", volume=0.5)
