# 사전 저장 프로필 — 유저가 미리 만들어 두는 캐릭터
#
# [저장소 분리]
#   계정(accounts/)·통계(stats/)와 별개로 profiles/{user_id}.json에 둔다.
#   프로필은 개수가 늘어나고 항목도 많아 계정 파일에 넣으면 비대해진다.
#
# [태그]
#   기획 규정 — 생성 시점에 서로 다른 짧은 한국어 단어를 부여하되,
#   동명 프로필이 생기는 경우 해당 프로필들만 태그를 드러낸다.
#   즉 태그는 항상 부여하고 표시만 조건부다.
#
# [생성 제한]
#   사전 프로필 생성은 플레이해 본 시나리오만 가능하다(stats.has_played).
import asyncio
import json
import os
import random
import uuid

PROFILES_DIR = "profiles"
PROFILE_SCHEMA_VERSION = 1

# 태그 후보 — 짧은 한국어 단어. 중복되지 않게 배정한다.
TAG_WORDS = [
    "설산", "적목", "청류", "묵향", "백랑", "홍련", "야천", "은사",
    "고월", "창천", "흑철", "비류", "낙엽", "서리", "재림", "무형",
    "잔월", "화룡", "청명", "북풍", "석양", "미명", "한천", "유성",
]

# 공통 프로필에서 채우는 항목 (기획 규정).
COMMON_FIELDS = ["이름", "성별", "나이", "외형"]

# 시나리오별 프로필 고정 항목 (기획 규정).
SCENARIO_FIELDS = ["이름", "성별", "나이", "외형", "배경", "복장", "행운"]

_locks = {}


def _lock_for(user_id):
    k = str(user_id)
    if k not in _locks:
        _locks[k] = asyncio.Lock()
    return _locks[k]


def _path(user_id) -> str:
    return os.path.join(PROFILES_DIR, f"{user_id}.json")


def _blank(user_id) -> dict:
    return {"schema_version": PROFILE_SCHEMA_VERSION,
            "user_id": str(user_id), "profiles": []}


def load_all(user_id) -> dict:
    """유저의 프로필 저장소 전체."""
    path = _path(user_id)
    if not os.path.exists(path):
        return _blank(user_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[프로필] 로드 실패 ({user_id}): {e}")
        return _blank(user_id)
    base = _blank(user_id)
    base.update(data)
    return base


def _write(store: dict) -> bool:
    os.makedirs(PROFILES_DIR, exist_ok=True)
    path = _path(store["user_id"])
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[프로필] 저장 실패: {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


def list_profiles(user_id, scenario_id: str = None) -> list:
    """프로필 목록. scenario_id를 주면 그 시나리오 것만.

    공통 프로필(scenario_id 없음)은 항상 포함된다 —
    어느 시나리오에서도 기본 항목을 재사용할 수 있기 때문이다.
    """
    items = load_all(user_id).get("profiles") or []
    if scenario_id is None:
        return items
    return [p for p in items
            if p.get("scenario_id") in (scenario_id, None, "")]


def count_profiles(user_id, scenario_id: str = None) -> int:
    return len(list_profiles(user_id, scenario_id))


def _pick_tag(store: dict) -> str:
    """사용 중이 아닌 태그를 고른다. 모두 쓰였으면 번호를 붙인다."""
    used = {p.get("tag") for p in (store.get("profiles") or [])}
    pool = [w for w in TAG_WORDS if w not in used]
    if pool:
        return random.choice(pool)
    return f"{random.choice(TAG_WORDS)}{len(used) + 1}"


def duplicate_names(user_id, scenario_id: str = None) -> set:
    """같은 이름이 둘 이상인 이름 집합.

    기획 규정 — 태그는 동명 프로필이 생기는 경우에만 드러낸다.
    한 시나리오 내에서의 중복만 따진다.
    """
    counts = {}
    for p in list_profiles(user_id, scenario_id):
        n = p.get("name") or ""
        counts[n] = counts.get(n, 0) + 1
    return {n for n, c in counts.items() if c > 1}


def display_name(profile: dict, dup_names: set) -> str:
    """표시용 이름. 동명이 있을 때만 태그를 붙인다."""
    name = profile.get("name") or "(이름 없음)"
    if name in dup_names and profile.get("tag"):
        return f"{name} [{profile['tag']}]"
    return name


async def create(user_id, *, name: str, scenario_id: str = None,
                 fields: dict = None) -> dict | None:
    """프로필을 저장한다.

    Args:
        scenario_id: None이면 공통 프로필 (이름·성별·나이·외형만)
        fields: 항목별 값

    Returns:
        생성된 프로필 dict 또는 실패 시 None
    """
    async with _lock_for(user_id):
        store = load_all(user_id)
        profile = {
            "id": uuid.uuid4().hex[:8],
            "name": (name or "").strip() or "이름 없음",
            "tag": _pick_tag(store),
            "scenario_id": scenario_id,
            "fields": dict(fields or {}),
        }
        store.setdefault("profiles", []).append(profile)
        if not _write(store):
            return None
        return profile


async def update(user_id, profile_id: str, field: str, value) -> bool:
    """프로필 항목 하나를 수정한다."""
    async with _lock_for(user_id):
        store = load_all(user_id)
        for p in store.get("profiles") or []:
            if p.get("id") == profile_id:
                if field == "name":
                    p["name"] = str(value)
                else:
                    p.setdefault("fields", {})[field] = value
                return _write(store)
        return False


async def delete(user_id, profile_id: str) -> bool:
    """프로필을 삭제한다."""
    async with _lock_for(user_id):
        store = load_all(user_id)
        items = store.get("profiles") or []
        remain = [p for p in items if p.get("id") != profile_id]
        if len(remain) == len(items):
            return False
        store["profiles"] = remain
        return _write(store)


def search(user_id, query: str, scenario_id: str = None) -> list:
    """이름으로 1차 검색. 동명이면 태그로 2차 검색이 필요하다.

    Returns:
        일치하는 프로필 목록. 여럿이면 호출부가 태그로 재질문한다.
    """
    q = (query or "").strip()
    if not q:
        return []
    items = list_profiles(user_id, scenario_id)
    exact = [p for p in items if (p.get("name") or "") == q]
    if exact:
        return exact
    # 부분 일치 — 오타 대비
    return [p for p in items if q in (p.get("name") or "")]


def search_by_tag(profiles: list, tag: str) -> dict | None:
    """1차 검색 결과에서 태그로 좁힌다."""
    t = (tag or "").strip().strip("[]")
    for p in profiles:
        if (p.get("tag") or "") == t:
            return p
    return None


def preview(profile: dict, dup_names: set = None) -> str:
    """목록용 한 줄 미리보기."""
    dup = dup_names or set()
    fields = profile.get("fields") or {}
    bits = [fields.get(k) for k in ("성별", "나이") if fields.get(k)]
    tail = f" · {' / '.join(str(b) for b in bits)}" if bits else ""
    scope = profile.get("scenario_id") or "공통"
    return f"**{display_name(profile, dup)}** ({scope}){tail}"


def detail(profile: dict, dup_names: set = None) -> str:
    """상세 출력."""
    dup = dup_names or set()
    lines = [f"**{display_name(profile, dup)}**",
             f"> 시나리오: {profile.get('scenario_id') or '공통'}"]
    for k, v in (profile.get("fields") or {}).items():
        lines.append(f"> {k}: {v}")
    return "\n".join(lines)
