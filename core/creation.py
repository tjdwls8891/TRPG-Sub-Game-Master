# 세션 제작 과정 — 단계 상태 기계
#
# [상태 기계로 만드는 이유]
#   단계가 많고 회귀·취소가 필요하다. 각 단계를 (렌더, 입력 처리, 다음 단계 결정)
#   3종으로 정의하면 회귀는 history를 되감는 것으로 끝나고, 중간에 끊겨도
#   creation_state로 복원된다.
#
# [기획 규정 단계 순서]
#   계정 확인 → 세션 종류 → 채널 생성 → 비공개 여부 → 공통 소개
#   → 시나리오 선택 → TTS 여부·목소리 → 기억 방식 → 프로필 생성
#   → 캐시 업로드 → 시작 상황 삼지선다 → 인트로
#
#   이 모듈은 '진행 순서와 상태'를 관리한다. 각 단계의 실제 작업(채널 생성,
#   프로필 생성 등)은 해당 모듈이 담당하며 여기서는 호출만 한다.

# 단계 정의 — 순서는 STEP_ORDER가 정한다.
STEP_ORDER = [
    "kind",          # 세션 종류 선택 (솔로 / 마스터)
    "private",       # 비공개 여부
    "intro",         # 공통 소개 (인지 수준 분기)
    "scenario",      # 시나리오 선택
    "tts",           # TTS 사용 여부 및 목소리
    "memory",        # 기억 압축 방식
    "profile",       # 프로필 생성
    "open",          # 캐시 업로드 시간 입력
    "start",         # 시작 상황 삼지선다
    "done",
]

# 되돌아갈 수 없는 단계. 이후 단계에서 회귀 대상에서 제외한다.
# open은 캐시 업로드(비용 발생)를 수반하므로 되돌리면 재과금이 된다.
IRREVERSIBLE = {"open", "start", "done"}

STEP_LABELS = {
    "kind": "세션 종류",
    "private": "공개 설정",
    "intro": "소개",
    "scenario": "시나리오 선택",
    "tts": "음성 설정",
    "memory": "기억 방식",
    "profile": "프로필 생성",
    "open": "세션 오픈",
    "start": "시작 상황",
    "done": "완료",
}


def get_state(session) -> dict:
    """생성 상태. 없으면 초기 상태를 만들어 반환한다."""
    st = getattr(session, "creation_state", None)
    if not isinstance(st, dict) or not st:
        st = {"step": STEP_ORDER[0], "history": [], "data": {}}
        session.creation_state = st
    st.setdefault("step", STEP_ORDER[0])
    st.setdefault("history", [])
    st.setdefault("data", {})
    return st


def current_step(session) -> str:
    return get_state(session)["step"]


def is_done(session) -> bool:
    return current_step(session) == "done"


def record(session, key: str, value) -> dict:
    """단계 결과를 저장한다."""
    st = get_state(session)
    st["data"][key] = value
    return st


def get_data(session, key: str, default=None):
    return get_state(session)["data"].get(key, default)


def advance(session, *, to: str = None) -> str:
    """다음 단계로 진행한다.

    Args:
        to: 지정하면 그 단계로 건너뛴다(조건부 생략에 사용).
            예 — 사전 프로필이 없으면 관련 질문을 건너뛴다.

    Returns:
        새 단계 이름
    """
    st = get_state(session)
    cur = st["step"]
    st["history"].append(cur)

    if to and to in STEP_ORDER:
        st["step"] = to
        return to

    try:
        idx = STEP_ORDER.index(cur)
    except ValueError:
        idx = 0
    st["step"] = STEP_ORDER[min(idx + 1, len(STEP_ORDER) - 1)]
    return st["step"]


def go_back(session) -> tuple:
    """이전 단계로 회귀한다.

    되돌릴 수 없는 단계를 이미 지났으면 거부한다. 캐시 업로드처럼
    비용이 발생한 단계를 되돌리면 재과금이 되기 때문이다.

    Returns:
        (성공 여부, 단계 또는 사유)
    """
    st = get_state(session)
    if st["step"] in IRREVERSIBLE:
        return False, f"'{STEP_LABELS.get(st['step'], st['step'])}' 단계는 되돌릴 수 없습니다."
    if not st["history"]:
        return False, "되돌아갈 단계가 없습니다."

    prev = st["history"].pop()
    st["step"] = prev
    # 되돌아간 단계의 선택은 무효화한다. 남겨두면 재선택 흔적이 섞인다.
    st["data"].pop(prev, None)
    return True, prev


def reset(session):
    """생성 과정을 초기화한다(취소)."""
    session.creation_state = {"step": STEP_ORDER[0], "history": [], "data": {}}


def progress_text(session) -> str:
    """진행 표시. 어디까지 왔는지 보여준다."""
    st = get_state(session)
    cur = st["step"]
    parts = []
    for name in STEP_ORDER[:-1]:
        label = STEP_LABELS.get(name, name)
        if name == cur:
            parts.append(f"**{label}**")
        elif name in st["data"]:
            parts.append(f"~~{label}~~")
        else:
            parts.append(label)
    return " › ".join(parts)


def summary(session) -> str:
    """지금까지의 선택 요약. 확인 단계에서 보여준다."""
    data = get_state(session)["data"]
    lines = []
    for name in STEP_ORDER[:-1]:
        if name not in data:
            continue
        val = data[name]
        if isinstance(val, bool):
            val = "예" if val else "아니오"
        lines.append(f"· {STEP_LABELS.get(name, name)}: {val}")
    return "\n".join(lines) if lines else "(선택된 항목 없음)"


def can_skip_profile_question(session, user_id, scenario_id: str) -> bool:
    """사전 프로필 사용 여부 질문을 생략할지 판정한다.

    기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면 질문을 생략한다.
    """
    from . import stats

    if not stats.has_played(user_id, scenario_id):
        return True
    # 사전 프로필 저장소는 별도 모듈 소관. 없으면 생략한다.
    try:
        from .profiles import count_profiles
        return count_profiles(user_id, scenario_id) == 0
    except Exception:
        return True
