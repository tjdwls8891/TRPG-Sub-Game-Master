# 음성 프로필 — 마스터(진행자) 성별 목소리 · NPC 목소리 자동 배정 · 문단별 화자 결정 · 절제된 스타일 (P2 #10)
#
# [권위]
#   · 마스터: 세션 단위 master_gender(male|female, 개설자가 정한다 — P2 Q2·Q6). 같은 진행자의 두 표현(Q1)이며
#     본문 내레이션과 액자 밖 직접 발화가 같은 목소리를 쓴다(Q8). 변경은 이후 출력부터(소급 없음).
#   · NPC: 시나리오 id + 안정 NPC 키로 결정한다(Q5 — 다른 세션·재시작에서도 같은 목소리). 표시 이름만으로 동일성을
#     추정하지 않는다 — 저작 NPC 키는 default_npcs의 사전 키(= authored_npc_actor_id의 입력, 명시 actor_id가
#     있으면 그것)다. 비정규 NPC는 세션 등록부의 목소리(기존 v5 배정)를 그대로 쓴다(세션 한정 인물).
#   · 배정 = 결정적 함수(sha256) + 전역 기록(data/voice_assignments.json — 첫 배정 후 고정). 기록이 있으면 기록이
#     우선이다(목소리 목록이 바뀌어도 이미 정해진 인물은 바뀌지 않는다). 기록 실패는 결정적 함수가 같은 값을 다시 낸다.
#   · 성별은 정본 NPC 저작 '나이·성별' 문구에서만 읽는다(비밀·성격 등 다른 필드는 TTS에 쓰지 않는다).
#     공급자 목소리에는 공식 나이 정보가 없어 나이는 배정에 쓰지 않는다(부분 충족 — 보고서 참조).
#
# [공급자 범위] Gemini TTS: 요청당 단일 화자 prebuilt voice + 자연어 스타일 지시. 화자별로 문단을 나눠 합성한다
#   (multi-speaker 모드는 쓰지 않는다). 감정은 별도 LLM 호출 없이 그 대사 문장 자체에 드러난 정도만 절제해 싣도록
#   지시하고(그 밖에는 중립), 마스터·내레이션은 기존 절제 프로필을 유지한다.
import hashlib
import json
import os
import threading
import time

from .constants import TTS_NARRATOR_VOICE, TTS_VOICES

# 목소리 성별 — Google Cloud TTS 'Chirp 3: HD voices' 표(같은 prebuilt 이름을 Gemini TTS가 쓴다).
VOICE_GENDER = {
    "Zephyr": "female", "Kore": "female", "Leda": "female", "Aoede": "female", "Achernar": "female",
    "Callirrhoe": "female", "Autonoe": "female", "Despina": "female", "Erinome": "female",
    "Laomedeia": "female", "Gacrux": "female", "Pulcherrima": "female", "Vindemiatrix": "female",
    "Sulafat": "female",
    "Puck": "male", "Charon": "male", "Fenrir": "male", "Orus": "male", "Algenib": "male",
    "Enceladus": "male", "Iapetus": "male", "Umbriel": "male", "Algieba": "male", "Rasalgethi": "male",
    "Alnilam": "male", "Schedar": "male", "Achird": "male", "Zubenelgenubi": "male",
    "Sadachbia": "male", "Sadaltager": "male",
}

MASTER_GENDERS = ("male", "female")
DEFAULT_MASTER_GENDER = "male"            # 구 세션 기본 = 기존 나레이터(Algenib)와 같은 목소리
MASTER_VOICES = {"male": TTS_NARRATOR_VOICE, "female": "Achernar"}
GENDER_LABEL = {"male": "남성", "female": "여성"}

ASSIGN_VERSION = 1
ASSIGN_PATH = os.path.join("data", "voice_assignments.json")
_LOCK = threading.Lock()

KIND_NARRATION = "narration"              # 액자 안 본문 내레이션 — 마스터 목소리
KIND_MASTER = "master"                    # 액자 밖 마스터 직접 발화 — 마스터 목소리
KIND_NPC = "npc"                          # NPC 대사 — NPC 고정 목소리


def master_gender(session) -> str:
    g = getattr(session, "master_gender", None)
    return g if g in MASTER_GENDERS else DEFAULT_MASTER_GENDER


def master_voice(session) -> str:
    return MASTER_VOICES[master_gender(session)]


def npc_pool(gender: str | None) -> tuple:
    """NPC 후보 목소리 — 공급자 목록 ∩ 성별(모르면 전체), 마스터 목소리 제외(진행자와 혼동 방지). 정렬 고정."""
    masters = set(MASTER_VOICES.values())
    pool = [v for v in TTS_VOICES if v not in masters and (gender is None or VOICE_GENDER.get(v) == gender)]
    return tuple(sorted(pool)) or tuple(sorted(v for v in TTS_VOICES if v not in masters))


def gender_of_text(text) -> str | None:
    """저작 '나이·성별' 문구 → male|female|None(판단 불가는 None — 추측하지 않는다)."""
    if not isinstance(text, str):
        return None
    f = any(w in text for w in ("여성", "여자", "여)", "(여", "소녀", "할머니", "여,", " 여"))
    m = any(w in text for w in ("남성", "남자", "남)", "(남", "소년", "할아버지", "남,", " 남"))
    if f == m:
        return None
    return "female" if f else "male"


def deterministic_voice(scenario_id: str, npc_key: str, gender: str | None) -> str:
    pool = npc_pool(gender)
    h = int(hashlib.sha256(f"voice.npc.v1|{scenario_id}|{npc_key}".encode("utf-8")).hexdigest(), 16)
    return pool[h % len(pool)]


def _load(path=ASSIGN_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("assignments"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[음성] 배정 기록 읽기 실패(결정적 배정으로 대체): {type(e).__name__}: {e}")
    return {"version": ASSIGN_VERSION, "assignments": {}}


def assigned_voice(scenario_id: str, npc_key: str, gender: str | None, *, path=ASSIGN_PATH) -> str:
    """시나리오 + NPC 키의 고정 목소리. 기록이 있으면 그 값, 없으면 결정적 값을 기록(첫 배정 고정)."""
    key = f"{scenario_id}|{npc_key}"
    with _LOCK:
        data = _load(path)
        rec = data["assignments"].get(key)
        if isinstance(rec, dict) and rec.get("voice") in TTS_VOICES:
            return rec["voice"]
        voice = deterministic_voice(scenario_id, npc_key, gender)
        data["assignments"][key] = {"voice": voice, "gender": gender, "at": round(time.time(), 3),
                                    "rule": "sha256-v1"}
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001 — 기록 실패여도 결정적 값은 같다
            print(f"[음성] 배정 기록 저장 실패(같은 값이 다시 계산됨): {type(e).__name__}: {e}")
        return voice


def npc_key_of(session, name: str):
    """(npc_key, 성별 문구) — 정규 NPC의 안정 키. 비정규·모르는 이름이면 (None, None)."""
    sd = getattr(session, "scenario_data", None) or {}
    npcs = sd.get("default_npcs") or {}
    entry = npcs.get(name)
    key = name if isinstance(entry, dict) else None
    if key is None:
        for k, v in npcs.items():                      # 표시 이름(name 필드)으로 저작 키를 찾는다 — 유일할 때만
            if isinstance(v, dict) and v.get("name") == name:
                if key is not None:
                    return None, None                  # 같은 표시 이름이 둘 이상 — 추정하지 않는다
                key, entry = k, v
    if key is None:
        return None, None
    stable = entry.get("actor_id") or key              # 명시 ActorId가 있으면 그것(이름 변경과 무관)
    return stable, entry.get("나이·성별")


def npc_voice(session, name: str) -> str | None:
    """NPC 목소리 — 저작 voice > 정규 NPC 고정 배정 > 비정규 등록부 > None(나레이터)."""
    from . import irregular_npc as _irr
    npcs = getattr(session, "npcs", {}) or {}
    v = (npcs.get(name) or {}).get("voice") if isinstance(npcs.get(name), dict) else None
    if v:
        return v
    key, gtext = npc_key_of(session, name)
    if key is not None:
        return assigned_voice(getattr(session, "scenario_id", "") or "", key, gender_of_text(gtext))
    return _irr.voice_for(session, name)


# ── 스타일(공급자 자연어 지시) ──
NARRATION_STYLE = None     # None = core.tts 기본 절제 프로필(TTS_STYLE_PROMPT)
MASTER_STYLE = """# AUDIO PROFILE: 게임 진행자(액자 밖 안내)

### DIRECTOR'S NOTES
Style: 차분하고 정중한 안내. 감정은 아주 절제한다. 과장·연기 금지.
Pace: 보통 속도로 또렷하게."""
NPC_STYLE = """# AUDIO PROFILE: 등장인물의 대사

### DIRECTOR'S NOTES
Style: 이 대사 문장 자체에 드러난 감정과 상황만 자연스럽게 싣는다. 드러나지 않으면 중립으로 읽는다.
과장된 연기·비명·울먹임·속삭임 금지. 목소리 자체(음색)는 바꾸지 않는다.
Pace: 대사 길이에 맞게 자연스럽게."""


def style_for(kind: str):
    if kind == KIND_MASTER:
        return MASTER_STYLE
    if kind == KIND_NPC:
        return NPC_STYLE
    return NARRATION_STYLE


def paragraph_voice(session, paragraph: str) -> tuple:
    """(voice, style, spoken_text, kind) — 대사 문단은 화자 NPC 목소리, 그 밖은 마스터 내레이션 목소리."""
    from .dialogue import parse_dialogue_paragraph
    from .tts import clean_text_for_tts
    d = parse_dialogue_paragraph(paragraph)
    if d:
        speaker, content = d
        v = npc_voice(session, speaker)
        if v:
            return v, style_for(KIND_NPC), clean_text_for_tts(content), KIND_NPC
        return master_voice(session), style_for(KIND_NARRATION), clean_text_for_tts(content), KIND_NARRATION
    return master_voice(session), style_for(KIND_NARRATION), clean_text_for_tts(paragraph), KIND_NARRATION
