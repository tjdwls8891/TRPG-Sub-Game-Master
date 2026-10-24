# 마스터(진행자) 대사 — 사전 저작 대사 데이터에서 한 줄을 고른다 (v7.13.0)
#
# 마스터는 전 시나리오 공통 진행자다. 플레이어 대상의 기존 고정 안내 문구를 이 대사로
# 바꿀 뿐이며, 새 이벤트·메시지·AI 호출을 만들지 않는다(표현 승격).
# 대사는 assets/master_dialogues_v1.json 데이터다 — 코드 수정 없이 문구를 교정한다.
#
# 실패(키 없음·슬롯 누락·파일 손상)는 None을 돌려주고, 호출부는 기존 문구를 그대로 쓴다.
import json
import os
import random
import re

from .koreantext import substitute

ASSET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "assets", "master_dialogues_v1.json")

_cache: dict | None = None
_SLOT_RE = re.compile(r"\{([^{}]+)\}")


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(ASSET_PATH, encoding="utf-8") as f:
                data = json.load(f)
            events = data.get("events") if isinstance(data, dict) else None
            acks = [tuple(a for a in pair if isinstance(a, str) and a.strip())
                    for pair in (data.get("acks") or []) if isinstance(pair, list)]
            _cache = {"events": events if isinstance(events, dict) else {},
                      "slots": set((data.get("reserved_placeholders") or {}).keys()),
                      "acks": [a for a in acks if a]}
        except Exception as e:
            print(f"[마스터] 대사 데이터 로드 실패(기존 문구 사용): {e}")
            _cache = {"events": {}, "slots": set(), "acks": []}
    return _cache


def _clean_slot(value) -> str | None:
    """표시용 슬롯 값 정제 — 멘션·마크다운 제어 문자를 무력화한다. 빈 값은 None."""
    if not isinstance(value, str):
        return None
    v = " ".join(value.split())
    if not v:
        return None
    v = v.replace("@", "@​")
    for ch in ("\\", "*", "_", "`", "~", "|", ">", "#"):
        v = v.replace(ch, "\\" + ch)
    return v                     # v7.25(CUT): 슬롯 값(플레이어 이름 등)은 대화 로그·프롬프트로 이어진다 — 자르지 않는다


def line(key: str, *, slots: dict | None = None) -> str | None:
    """사전 대사 1개. 실패하면 None(호출부는 기존 문구를 쓴다). API 호출 없음."""
    data = _load()
    ev = data["events"].get(key)
    variants = [v for v in (ev or {}).get("variants") or () if isinstance(v, str) and v.strip()]
    if not variants:
        return None
    text = random.choice(variants)
    used = set(_SLOT_RE.findall(text))
    if used:
        if not used <= data["slots"]:
            return None
        clean = {k: _clean_slot((slots or {}).get(k)) for k in used}
        if any(v is None for v in clean.values()):
            return None
        text = substitute(text, clean)   # 슬롯 뒤 조사 보정(외모를·성별을)
    return text


def acks() -> list:
    """v7.36 공용 응답 쌍(대화 박자 — 저작 응답이 없는 박자)."""
    return list(_load().get("acks") or [])


def has_key(key: str) -> bool:
    """대사 데이터에 그 키가 있는가(저작 검사용)."""
    return bool(_load()["events"].get(key))


def say(key: str, fallback: str, *, slots: dict | None = None) -> str:
    """마스터 대사 또는 기존 문구."""
    return line(key, slots=slots) or fallback


def lead(key: str, body: str, *, sep: str = "\n", default: str | None = None,
         slots: dict | None = None) -> str:
    """마스터 대사를 기존 정보 앞에 붙인다(수치·사유·버튼 안내는 body에 그대로).

    실패하면 default(대사가 대신한 기존 안내 문장)를, 그것도 없으면 body만 쓴다.
    """
    m = line(key, slots=slots) or default
    return f"{m}{sep}{body}" if m else body
