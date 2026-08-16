# TTS 사전 생성 — 시스템이 말할 문구를 배포 전에 미리 합성해 파일로 둔다
#
# [런타임 캐시와의 차이]
#   런타임 캐시는 '합성한 뒤에 저장'하므로 첫 실행 비용이 그대로 발생하고,
#   세션 플레이·결제처럼 매번 텍스트가 달라지는 경로에서는 히트하지 않는다.
#
#   이 모듈은 반대다. 시스템이 언제 무엇을 말할지 이미 정해져 있으므로,
#   그 문구 목록을 미리 합성해 두고 런타임에는 파일만 재생한다.
#   따라서 운영 중 TTS API 호출이 0이 된다.
#
# [대상]
#   PRESET_MESSAGES에 등록된 시스템 문구와, 시나리오 JSON의 고정 텍스트
#   (scenario_intro / start_message). 세션마다 동일하므로 사전 생성에 적합하다.
#
# [생성 시점]
#   운영 중이 아니라 배포 준비 단계에서 !tts생성 명령으로 일괄 수행한다.
import hashlib
import json
import os

PRESET_DIR = os.path.join("media", "_tts_preset")
INDEX_FILE = os.path.join(PRESET_DIR, "index.json")

# 시스템 고정 문구 — 키는 코드에서 참조할 식별자.
# 문구를 수정하면 해시가 바뀌므로 재생성이 필요하다(build가 자동 감지).
PRESET_MESSAGES = {
    "intro.welcome": "환영합니다. 지금부터 세션 준비를 시작하겠습니다.",
    "intro.trpg": "티알피지는 진행자와 참가자가 함께 이야기를 만들어가는 놀이입니다.",
    "intro.turn": "여러분이 행동을 선언하면, 저는 그 결과를 묘사합니다.",
    "intro.cost": "진행에는 잉크가 소모됩니다. 각 턴의 예상 비용은 디스플레이에 표시됩니다.",
    "intro.display": "디스플레이 채널에서 세션 상태를 확인하고 버튼으로 조작할 수 있습니다.",
    "ask.declare": "어떻게 하시겠습니까?",
    "ask.target": "누구를 대상으로 하시겠습니까?",
    "roll.prompt": "판정이 필요합니다. 주사위를 굴려 주십시오.",
    "session.opened": "세션이 열렸습니다.",
    "session.closed": "세션을 종료합니다.",
    "error.retry": "처리 중 문제가 발생하여 다시 시도하고 있습니다.",
    "error.cancelled": "이번 턴 처리에 실패하여 진행을 취소했습니다.",
    "fund.low": "잉크가 부족합니다. 충전 후 진행해 주십시오.",
}


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _path(key: str, voice: str) -> str:
    safe = key.replace("/", "_").replace("\\", "_")
    return os.path.join(PRESET_DIR, voice or "_default", f"{safe}.pcm")


def load_index() -> dict:
    """생성 인덱스. 키별로 어떤 텍스트·목소리로 만들었는지 기록한다."""
    if not os.path.exists(INDEX_FILE):
        return {}
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_index(index: dict) -> bool:
    try:
        os.makedirs(PRESET_DIR, exist_ok=True)
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        os.replace(tmp, INDEX_FILE)
        return True
    except Exception as e:
        print(f"[TTS프리셋] 인덱스 저장 실패: {e}")
        return False


def get(key: str, voice: str = "") -> bytes | None:
    """사전 생성된 PCM을 읽는다. 없으면 None.

    런타임에서 호출하는 유일한 함수다. API를 부르지 않는다.
    """
    path = _path(key, voice)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[TTS프리셋] 읽기 실패 {key}: {e}")
        return None


def has(key: str, voice: str = "") -> bool:
    """해당 키의 사전 생성 파일이 있는지."""
    return os.path.exists(_path(key, voice))


def text_of(key: str) -> str | None:
    """키에 대응하는 문구. 코드가 자막을 함께 출력할 때 쓴다."""
    return PRESET_MESSAGES.get(key)


def collect_targets(scenario_ids: list = None) -> dict:
    """사전 생성 대상 전체를 모은다.

    시스템 고정 문구 + 시나리오별 고정 텍스트(scenario_intro / start_message).
    시나리오 텍스트는 길어 문단 단위로 쪼개 저장한다 — 스트리밍 재생 시
    문단마다 이어 붙이기 위함이며, 한 파일이 지나치게 커지는 것도 막는다.

    Returns:
        {key: text}
    """
    targets = dict(PRESET_MESSAGES)

    if scenario_ids is None:
        scenario_ids = []
        if os.path.isdir("scenarios"):
            scenario_ids = [f[:-5] for f in os.listdir("scenarios") if f.endswith(".json")]

    for sid in scenario_ids:
        path = os.path.join("scenarios", f"{sid}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for field in ("scenario_intro", "start_message"):
            text = data.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            paras = [p.strip() for p in text.split("\n\n") if p.strip()]
            for i, para in enumerate(paras):
                targets[f"scenario.{sid}.{field}.{i}"] = para
    return targets


def needs_build(targets: dict, voice: str = "") -> dict:
    """아직 만들지 않았거나 문구가 바뀐 항목만 추린다.

    인덱스에 기록된 해시와 현재 텍스트 해시를 비교하므로,
    문구를 수정하면 자동으로 재생성 대상이 된다.
    """
    index = load_index()
    out = {}
    for key, text in targets.items():
        rec = index.get(key)
        if (not rec or rec.get("hash") != _hash(text)
                or rec.get("voice") != (voice or "_default")
                or not has(key, voice)):
            out[key] = text
    return out


async def build(bot, *, voice: str = "", scenario_ids: list = None,
                progress=None) -> dict:
    """사전 생성을 수행한다. 운영 중이 아니라 배포 준비 단계에서 호출한다.

    Args:
        progress: 진행 상황을 받을 코루틴 (선택)

    Returns:
        {"built": int, "skipped": int, "failed": int, "cost_krw": float}
    """
    from .tts import synthesize_tts_pcm, TTS_NARRATOR_VOICE

    v = voice or TTS_NARRATOR_VOICE
    targets = collect_targets(scenario_ids)
    todo = needs_build(targets, v)
    index = load_index()

    built = failed = 0
    total_cost = 0.0

    for i, (key, text) in enumerate(sorted(todo.items()), start=1):
        pcm, cost, _in, _out = await synthesize_tts_pcm(bot, text, voice_name=v)
        total_cost += cost
        if not pcm:
            failed += 1
            continue
        path = _path(key, v)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(pcm)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[TTS프리셋] 저장 실패 {key}: {e}")
            failed += 1
            continue
        index[key] = {"hash": _hash(text), "voice": v, "bytes": len(pcm)}
        built += 1
        if progress and i % 5 == 0:
            try:
                await progress(f"{i}/{len(todo)} 생성 중… (누적 {total_cost:.1f}원)")
            except Exception:
                pass

    _save_index(index)
    return {
        "built": built,
        "skipped": len(targets) - len(todo),
        "failed": failed,
        "cost_krw": round(total_cost, 2),
    }


async def play(bot, session, key: str, *, voice: str = "") -> bool:
    """사전 생성된 음성을 재생한다. API 호출 없음.

    파일이 없으면 조용히 False를 반환한다 — 사전 생성이 안 된 상태에서
    런타임 합성으로 폴백하면 '사전 저장'의 목적(운영 중 호출 0)이 무너진다.
    빠진 항목은 build로 채워야 한다.
    """
    from .audio_mixer import get_mixer, PCMBytesAudioSource
    from .constants import TTS_NARRATION_VOLUME

    vc = getattr(session, "voice_client", None)
    if not (vc and vc.is_connected()):
        return False
    pcm = get(key, voice)
    if not pcm:
        print(f"[TTS프리셋] 미생성 항목: {key} — !tts생성 필요")
        return False
    mixer = get_mixer(vc)
    if mixer is None:
        return False
    try:
        mixer.enqueue_voice(PCMBytesAudioSource(pcm, volume=TTS_NARRATION_VOLUME))
        return True
    except Exception as e:
        print(f"[TTS프리셋] 재생 실패 {key}: {e}")
        return False


def stats() -> dict:
    """사전 생성 현황. 운영 점검용."""
    files = 0
    total = 0
    if os.path.isdir(PRESET_DIR):
        for root, _d, names in os.walk(PRESET_DIR):
            for n in names:
                if n.endswith(".pcm"):
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(root, n))
                    except Exception:
                        pass
    targets = collect_targets()
    return {
        "files": files,
        "mb": round(total / 1024 / 1024, 2),
        "targets": len(targets),
        "pending": len(needs_build(targets)),
    }
