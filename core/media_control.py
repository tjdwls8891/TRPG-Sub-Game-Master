# 미디어 제어 — 상황 기반 BGM 선택과 미디어 온오프 토글
#
# [설계 근거]
#   추출층위가 매 턴 situation.tag(장면 성격)와 situation.tension(긴장도)을
#   산출한다(4.4.0). 이 값을 소비해 BGM을 전환한다.
#   긴장도 구간은 extraction.THRESHOLDS.tension_high를 재사용하여
#   추출층위와 판정 기준을 일치시킨다.
import random

from .extraction import get_thresholds

# 미디어 토글 기본값. 전부 켜진 상태에서 시작한다.
DEFAULT_MEDIA_FLAGS = {
    "image": True,
    "tts": True,
    "bgm": True,
    "sfx": True,
}

# 긴장도 구간 라벨. bgm_map의 키로 쓰인다.
TENSION_LOW = "low"
TENSION_MID = "mid"
TENSION_HIGH = "high"


def get_media_flags(session) -> dict:
    """세션의 미디어 토글 상태. 누락 항목은 기본값으로 채운다."""
    flags = dict(DEFAULT_MEDIA_FLAGS)
    stored = getattr(session, "media_flags", None)
    if isinstance(stored, dict):
        for k in flags:
            if isinstance(stored.get(k), bool):
                flags[k] = stored[k]
    return flags


def set_media_flag(session, key: str, value: bool) -> dict:
    """토글 하나를 변경하고 전체 상태를 반환한다."""
    flags = get_media_flags(session)
    if key in flags:
        flags[key] = bool(value)
    session.media_flags = flags
    return flags


def is_enabled(session, key: str) -> bool:
    """해당 미디어가 켜져 있는지. 호출 생략 판단에 쓴다.

    NOTE: TTS는 기존 session.tts_enabled가 실제 게이트로 동작하므로
          두 값을 함께 본다. 어느 쪽이든 꺼져 있으면 꺼진 것으로 취급한다.
    """
    flags = get_media_flags(session)
    if key == "tts":
        return bool(flags.get("tts", True)) and bool(getattr(session, "tts_enabled", False))
    return flags.get(key, True)


def sync_tts_flag(session, value: bool):
    """TTS 토글을 두 필드에 동시 반영한다(디스플레이 UI에서 사용)."""
    set_media_flag(session, "tts", value)
    session.tts_enabled = bool(value)


def tension_band(session, tension: int) -> str:
    """긴장도를 low/mid/high 구간으로 나눈다.

    추출층위의 임계값(tension_high)을 재사용해 기준을 일치시킨다.
    하한은 그 절반 지점을 mid 시작으로 삼는다.
    """
    try:
        t = int(tension)
    except (TypeError, ValueError):
        return TENSION_LOW
    high = get_thresholds(session)["tension_high"]
    mid = max(1, high // 2)
    if t >= high:
        return TENSION_HIGH
    if t >= mid:
        return TENSION_MID
    return TENSION_LOW


def select_bgm(session, situation: dict) -> str | None:
    """상황에 맞는 BGM 트랙을 고른다.

    기획 규정 — 상황이 변하지 않으면 재생을 유지하고, 전환할 때만
    새 트랙을 반환한다. 유지해야 하면 None을 반환한다.

    시나리오 JSON 구조:
        "bgm_map": {
            "전투": {"high": ["battle_a", "battle_b"], "mid": ["tense_a"]},
            "대화": {"low": ["calm_a"]}
        }

    Args:
        situation: 추출층위의 situation ({"tag": str, "tension": int})

    Returns:
        재생할 트랙 이름, 또는 유지/불가 시 None
    """
    if not is_enabled(session, "bgm"):
        return None
    if not isinstance(situation, dict):
        return None

    tag = situation.get("tag") or ""
    band = tension_band(session, situation.get("tension", 0))

    # 상황이 그대로면 유지 — 매 턴 트랙이 바뀌면 몰입이 끊긴다.
    prev = getattr(session, "last_bgm_situation", None)
    if prev == (tag, band):
        return None

    try:
        bgm_map = (session.scenario_data or {}).get("bgm_map") or {}
    except Exception:
        return None

    entry = bgm_map.get(tag)
    if not isinstance(entry, dict):
        # 태그 미등록 — 기본 그룹으로 폴백
        entry = bgm_map.get("_default")
        if not isinstance(entry, dict):
            return None

    # 해당 구간에 트랙이 없으면 인접 구간으로 완화 탐색
    for b in (band, TENSION_MID, TENSION_LOW, TENSION_HIGH):
        tracks = entry.get(b)
        if tracks:
            session.last_bgm_situation = (tag, band)
            chosen = random.choice(list(tracks))
            # 같은 트랙이 다시 뽑히면 전환 의미가 없으므로 유지
            if chosen == getattr(session, "current_bgm", None):
                return None
            return chosen
    return None


def describe_bgm_pending(session) -> str:
    """BGM을 켠 직후 실제 재생까지의 안내 문구.

    기획 규정 — 온 시에는 즉시 재생하지 않고 다음 스트리밍 시작 또는
    추출층위 완료 시 재생하므로, 재생 시점을 대략 알린다.
    """
    return "BGM은 다음 묘사가 시작될 때 재생됩니다."


def format_flags(session) -> str:
    """디스플레이 표기용 토글 상태 문자열."""
    f = get_media_flags(session)
    mark = lambda v: "ON" if v else "OFF"
    return (f"이미지 {mark(f['image'])} · TTS {mark(f['tts'])} · "
            f"BGM {mark(f['bgm'])} · 효과음 {mark(f['sfx'])}")
