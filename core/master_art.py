# 마스터 이미지 슬롯 · 공용 로딩/오류 이미지 (P2 #7·#6)
#
# [슬롯] assets/master/manifest.json — 남·여 × 기본·경고·긍정 6개. 같은 진행자의 성별 표현(P2 Q1)이며 대사 체계는 하나다.
# [상태 — 서로 다른 세 가지(P2 이미지 계약 §3)]
#   PRODUCED         manifest에 파일명이 있고 열리는 원화 → 그 이미지
#   UNPRODUCED       manifest 값 null(의도적 미제작) → 공용 로딩 이미지 상시 대체. 오류·재시도·타이머 없음
#   ERROR            파일명이 있는데 없거나 손상 → 공용 오류 이미지 + 구조 로그(경로당 1회)
#   (렌더 지연 중 임시 로딩 = core.speaker_card의 render_pending — 이 모듈의 UNPRODUCED와 다른 상태다)
# [톤] 마스터 대사 키의 기존 신호(실패·차단·부족 → 경고, 완료·환영·시작 → 긍정)만 쓴다. 새 대사·성격을 만들지 않는다.
# [경로] 저장소 기준 절대 경로(cwd 무관). 외부 URL·CDN 없음.
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(ROOT, "assets")                 # 패키지 지정 경로(assets/indaim_*_neutral.webp)
MASTER_DIR = os.path.join(ROOT, "assets", "master")
MANIFEST = os.path.join(MASTER_DIR, "manifest.json")
LOADING_IMAGE = os.path.join(UI_DIR, "indaim_loading_neutral.webp")
ERROR_IMAGE = os.path.join(UI_DIR, "indaim_error_neutral.webp")

TONES = ("default", "warning", "positive")
SLOTS = tuple(f"{g}.{t}" for g in ("male", "female") for t in TONES)
PRODUCED, UNPRODUCED, ERROR = "PRODUCED", "UNPRODUCED", "ERROR"

_WARNING_WORDS = ("fail", "error", "blocked", "insufficient", "denied", "warning", "expired", "cancel",
                  "invalid", "conflict", "low", "retry", "closed")
_POSITIVE_WORDS = ("completed", "complete", "greeting", "welcome", "saved", "opened", "done", "success",
                   "start", "begin", "registered")

_manifest = None
_logged: set = set()


def reload() -> None:
    """manifest를 다시 읽는다(원화 배치 후 재시작 없이 반영할 때)."""
    global _manifest
    _manifest = None
    _logged.clear()


def _slots() -> dict:
    global _manifest
    if _manifest is None:
        try:
            with open(MANIFEST, encoding="utf-8") as f:
                data = json.load(f)
            _manifest = dict((data or {}).get("slots") or {})
        except Exception as e:  # noqa: BLE001 — manifest 자체가 깨졌으면 모두 미제작으로(오판 금지·1회 기록)
            _log_once("manifest", f"마스터 이미지 manifest 읽기 실패: {type(e).__name__}")
            _manifest = {}
    return _manifest


def _log_once(key, msg) -> None:
    if key in _logged:
        return
    _logged.add(key)
    try:
        from . import logs as _logs
        _logs.warn("master_art", msg, data={"slot": key})
    except Exception:  # noqa: BLE001
        print(f"[마스터 이미지] {msg}")


def tone_of(master_key: str | None) -> str:
    k = (master_key or "").lower()
    if any(w in k for w in _WARNING_WORDS):
        return "warning"
    if any(w in k for w in _POSITIVE_WORDS):
        return "positive"
    return "default"


def resolve(gender: str, tone: str = "default") -> tuple:
    """(이미지 경로, 상태) — 경로는 항상 존재하는 파일(공용 로딩/오류 포함)."""
    g = gender if gender in ("male", "female") else "male"
    t = tone if tone in TONES else "default"
    slot = f"{g}.{t}"
    name = _slots().get(slot)
    if not name:
        return LOADING_IMAGE, UNPRODUCED
    path = os.path.join(MASTER_DIR, os.path.basename(str(name)))
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
        return path, PRODUCED
    except Exception as e:  # noqa: BLE001
        _log_once(slot, f"마스터 이미지 슬롯 {slot} 파일 이상({type(e).__name__}) — 오류 이미지로 대체")
        return ERROR_IMAGE, ERROR


def for_session(session, master_key: str | None = None) -> tuple:
    from .voice_profile import master_gender
    return resolve(master_gender(session), tone_of(master_key))


def file_for(session, master_key: str | None = None, *, filename: str | None = None):
    """discord.File(마스터 이미지) — 상태와 함께. 파일명은 슬롯 상태만 드러낸다(실명·세력 없음)."""
    import discord
    path, state = for_session(session, master_key)
    ext = os.path.splitext(path)[1] or ".webp"
    return discord.File(path, filename=filename or f"master{ext}"), path, state
