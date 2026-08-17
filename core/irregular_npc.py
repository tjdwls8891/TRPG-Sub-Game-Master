# 비정규 NPC — 시나리오에 등록되지 않은 인물의 이미지·목소리를 결정하고 유지한다
#
# [문제]
#   묘사층위가 즉석에서 만들어낸 인물('낡은 외투의 사내')은 시나리오의
#   NPC 사전에도, 이미지 목록에도 없다. 매번 다른 이미지·목소리가 붙으면
#   같은 인물이 다른 사람처럼 보인다.
#
# [해법]
#   한 번 결정한 이미지·목소리를 세션에 기록해 재사용한다. 결정 호출은
#   해당 인물이 처음 등장할 때 1회만 발생한다.
#
# [정규 NPC 이미지 배제]
#   비정규 인물이 정규 NPC의 얼굴을 쓰면 혼동이 생기므로 후보에서 제외한다.
import os

# 목소리 후보 — (성별, 연령대) 조합에 대응하는 prebuilt voice.
# 실제 사용 가능한 이름은 Gemini TTS 문서 기준이며, 미지정 시 나레이터 보이스를 쓴다.
VOICE_POOL = {
    ("male", "young"): "Puck",
    ("male", "adult"): "Charon",
    ("male", "old"): "Fenrir",
    ("female", "young"): "Leda",
    ("female", "adult"): "Kore",
    ("female", "old"): "Aoede",
}

DEFAULT_GENDER = "male"
DEFAULT_AGE = "adult"


def get_registry(session) -> dict:
    """비정규 NPC 등록부. 없으면 빈 dict."""
    reg = getattr(session, "irregular_npcs", None)
    return reg if isinstance(reg, dict) else {}


def is_regular(session, name: str) -> bool:
    """시나리오에 등록된 정규 NPC인지."""
    return name in (getattr(session, "npcs", {}) or {})


def has_image(session, name: str) -> bool:
    """정규 이미지가 존재하는지. 있으면 비정규 처리가 불필요하다."""
    try:
        kws = (session.scenario_data or {}).get("media_keywords", {}) or {}
    except Exception:
        kws = {}
    if name in kws:
        return True
    path = os.path.join(f"media/{getattr(session, 'scenario_id', '')}", f"{name}.png")
    return os.path.exists(path)


def regular_image_keys(session) -> set:
    """정규 NPC가 쓰는 이미지 키 집합. 비정규 후보에서 배제하기 위함이다."""
    try:
        kws = (session.scenario_data or {}).get("media_keywords", {}) or {}
    except Exception:
        return set()
    return {str(v) for v in kws.values()}


def irregular_image_pool(session) -> list:
    """비정규 NPC용 이미지 후보.

    시나리오의 irregular_images 목록에서 정규 NPC가 쓰는 키를 제외한다.
    """
    try:
        pool = (session.scenario_data or {}).get("irregular_images") or []
    except Exception:
        return []
    used = regular_image_keys(session)
    return [p for p in pool if isinstance(p, str) and p not in used]


def needs_resolution(session, names: list) -> list:
    """결정이 필요한 인물만 추린다.

    정규 NPC, 이미 이미지가 있는 인물, 이미 결정된 인물은 제외한다.
    """
    reg = get_registry(session)
    out = []
    for n in (names or []):
        if not isinstance(n, str) or not n.strip():
            continue
        n = n.strip()
        if n in reg or is_regular(session, n) or has_image(session, n):
            continue
        if n not in out:
            out.append(n)
    return out


def pick_voice(gender: str = None, age: str = None) -> str:
    """성별·연령대에 대응하는 목소리 이름."""
    key = (gender or DEFAULT_GENDER, age or DEFAULT_AGE)
    if key in VOICE_POOL:
        return VOICE_POOL[key]
    return VOICE_POOL[(DEFAULT_GENDER, DEFAULT_AGE)]


def extract_candidate_names(text: str, session, limit: int = 6) -> list:
    """묘사문에서 배정 대상 후보 이름을 추린다.

    NOTE: 추출층위의 npcs_met은 스트리밍 이후에 나오므로 여기서는 쓸 수 없다.
          기획 규정상 배정은 스트리밍 '전'에 이뤄져야 하므로, 대사 마커에서
          화자 이름을 뽑아 후보로 삼는다. 대사가 있는 인물이 곧 이미지·목소리가
          필요한 인물이기도 하다.
    """
    import re

    names = []
    # 대사 마커 형식: 「이름」 또는 [이름] 뒤에 대사가 오는 패턴
    for m in re.finditer(r'[「\[]\s*([^\]」\n]{1,20}?)\s*[\]」]', text or ""):
        n = m.group(1).strip()
        if n and n not in names:
            names.append(n)
    return needs_resolution(session, names)[:limit]


def register(session, name: str, *, image_key: str = "", gender: str = None,
             age: str = None, context: str = "", turn: int = 0) -> dict:
    """비정규 NPC를 등록한다. 이미 있으면 기존 항목을 반환한다.

    NOTE: 동일 인물의 미디어를 유지하는 것이 이 함수의 목적이다.
          이미 등록된 인물을 덮어쓰면 얼굴과 목소리가 바뀐다.
    """
    reg = dict(get_registry(session))
    if name in reg:
        return reg[name]
    entry = {
        "image_key": image_key or "",
        "voice": pick_voice(gender, age),
        "gender": gender or DEFAULT_GENDER,
        "age": age or DEFAULT_AGE,
        "context": context or "",
        "first_turn": turn,
    }
    reg[name] = entry
    session.irregular_npcs = reg
    return entry


def promote(session, name: str, details: dict) -> bool:
    """비중이 생긴 비정규 NPC를 정규 NPC로 승격한다.

    승격하면 session.npcs에 편입되어 델타 주입 대상이 되고,
    비정규 등록부에서는 제거된다. 목소리는 유지한다.
    """
    reg = dict(get_registry(session))
    entry = reg.pop(name, None)
    if entry is None:
        return False
    npcs = dict(getattr(session, "npcs", {}) or {})
    merged = dict(details or {})
    merged.setdefault("voice", entry.get("voice"))
    merged.setdefault("image_key", entry.get("image_key"))
    npcs[name] = merged
    session.npcs = npcs
    session.irregular_npcs = reg
    return True


def voice_for(session, name: str) -> str | None:
    """해당 인물의 목소리. 정규 NPC는 npcs의 voice 항목을 본다."""
    npcs = getattr(session, "npcs", {}) or {}
    if name in npcs and isinstance(npcs[name], dict):
        v = npcs[name].get("voice")
        if v:
            return v
    entry = get_registry(session).get(name)
    return entry.get("voice") if entry else None


def image_path_for(session, name: str) -> str | None:
    """해당 인물에게 배정된 이미지 파일 경로. 없으면 None."""
    entry = get_registry(session).get(name)
    if not entry or not entry.get("image_key"):
        return None
    path = os.path.join(f"media/{getattr(session, 'scenario_id', '')}",
                        f"{entry['image_key']}.png")
    return path if os.path.exists(path) else None
