# 기억 압축 플랜 — 노멀 / 하이 / 로우 / 울트라
#
# [기획 규정]
#   세션 생성 시점에 선택하고 도중 변경 불가. 결정 시점은 시나리오별 비용
#   안내 이전. 모든 방식은 출력 구조를 JSON으로 양식화한다.
#   압축 시점은 대상 턴 종료 후 다음 턴 시작 시점이며, 턴 되감기 시
#   이전 턴을 재압축하지 않도록 주의한다.
#
# [플랜 구성]
#   노멀   현 방식과 유사. 고정 구조·고정 프롬프트로 5턴 단위 압축.
#   하이   상황별 최적 지시·구조 사용. 추출층위를 활용해 상황에 맞춰 압축.
#          설계에 따라 매 턴 압축도 고려.
#   로우   노멀을 채택하되 일정 턴 후부터 저렴한 모델로 크게 압축.
#          손익분기점을 넘겨야 한다.
#   울트라 매 턴 최적화 압축.
from .constants import DEFAULT_MODEL, LOGIC_MODEL

# 로우 플랜이 저비용 모델로 전환하는 시점(누적 압축 횟수).
# 전환 전까지는 노멀과 동일하게 동작한다.
LOW_SWITCH_AFTER = 3

PLANS = {
    "normal": {
        "label": "노멀",
        "interval": 5,
        "model": LOGIC_MODEL,
        "mode": "fixed",          # 고정 구조·고정 프롬프트
        "desc": "5턴마다 고정 방식으로 압축합니다. 균형 잡힌 기본 선택입니다.",
        "cost": "기본",
    },
    "high": {
        "label": "하이",
        "interval": 3,
        "model": LOGIC_MODEL,
        "mode": "adaptive",       # 상황별 최적 구조
        "desc": "3턴마다 상황에 맞춰 구조를 바꿔 압축합니다. 서사 보존이 가장 정확합니다.",
        "cost": "높음 (압축 빈도 증가 + 추출 정보 활용)",
    },
    "low": {
        "label": "로우",
        "interval": 5,
        "model": LOGIC_MODEL,
        "late_model": DEFAULT_MODEL,
        "mode": "fixed",
        "aggressive_after": LOW_SWITCH_AFTER,
        "desc": f"5턴마다 압축하되 {LOW_SWITCH_AFTER}회 이후부터 크게 줄입니다. 장기 세션에서 유리합니다.",
        "cost": "낮음 (후반 절감)",
    },
    "ultra": {
        "label": "울트라",
        "interval": 1,
        "model": LOGIC_MODEL,
        "mode": "adaptive",
        "desc": "매 턴 최적화 압축합니다. 입력이 가장 가벼워지지만 호출이 매 턴 발생합니다.",
        "cost": "매우 높음 (매 턴 호출)",
    },
}

DEFAULT_PLAN = "normal"


def get_plan(session) -> dict:
    """세션의 압축 플랜. 미지정이면 노멀."""
    key = getattr(session, "memory_plan", "") or DEFAULT_PLAN
    return PLANS.get(key, PLANS[DEFAULT_PLAN])


def plan_key(session) -> str:
    key = getattr(session, "memory_plan", "") or DEFAULT_PLAN
    return key if key in PLANS else DEFAULT_PLAN


def interval(session) -> int:
    """압축 주기(턴). 되감기 롤백은 주기를 몰라도 동작하지만,
    압축 발동 판정에는 필요하다."""
    return int(get_plan(session).get("interval") or 5)


def should_compress(session) -> bool:
    """이번 턴에 압축을 실행할지 판정한다.

    기획 규정 — 압축 시점은 대상 턴 종료 후 다음 턴 시작 시점이다.
    되감기로 턴이 되돌아간 경우 이미 압축한 구간을 다시 압축하지 않도록
    last_compressed_turn을 기준으로 판정한다.
    """
    turn = getattr(session, "turn_count", 0) or 0
    if turn <= 0:
        return False
    # 압축은 백그라운드로 돌아 완료까지 수 턴이 걸릴 수 있다.
    # 진행 중에 재발동하면 같은 구간이 중복 압축된다.
    if getattr(session, "is_compressing", False):
        return False

    last = getattr(session, "last_compressed_turn", 0) or 0
    # 되감기로 턴이 줄었으면 기준도 함께 내린다.
    if last > turn:
        session.last_compressed_turn = turn
        return False
    return (turn - last) >= interval(session)


def mark_compressed(session):
    """압축 완료 시점을 기록한다. 재압축 방지의 근거."""
    session.last_compressed_turn = getattr(session, "turn_count", 0) or 0
    session.compression_count = int(getattr(session, "compression_count", 0) or 0) + 1


def select_model(session) -> str:
    """이번 압축에 쓸 모델.

    로우 플랜은 일정 횟수 이후 저비용 모델로 전환한다.
    """
    plan = get_plan(session)
    after = plan.get("aggressive_after")
    if after is not None:
        count = int(getattr(session, "compression_count", 0) or 0)
        if count >= after:
            return plan.get("late_model") or plan["model"]
    return plan["model"]


def is_aggressive(session) -> bool:
    """크게 줄이는 압축 단계인지(로우 플랜 후반)."""
    plan = get_plan(session)
    after = plan.get("aggressive_after")
    if after is None:
        return False
    return int(getattr(session, "compression_count", 0) or 0) >= after


def is_adaptive(session) -> bool:
    """상황별 최적 구조를 쓰는 플랜인지(하이·울트라)."""
    return get_plan(session).get("mode") == "adaptive"


def build_context_hint(session) -> str:
    """하이·울트라 플랜이 쓸 상황 정보.

    추출층위가 산출한 값을 활용해 '지금 무엇이 중요한 국면인가'를 알린다.
    기획 규정 — 하이는 상황별 양식을 나누고 추출층위를 활용한다.
    """
    if not is_adaptive(session):
        return ""
    ex = getattr(session, "last_extraction", {}) or {}
    sit = ex.get("situation") or {}
    qp = ex.get("quest_progress") or {}
    tl = getattr(session, "world_timeline", {}) or {}

    parts = []
    tag = sit.get("tag")
    if tag and tag != "미확인":
        parts.append(f"장면 성격: {tag}")
    tension = sit.get("tension")
    if isinstance(tension, int):
        parts.append(f"긴장도: {tension}")
    if qp.get("advance") or qp.get("deviation"):
        parts.append(f"서사 진행 {qp.get('advance', 0)} / 이탈 {qp.get('deviation', 0)}")
    loc = tl.get("current_location")
    if loc and loc != "미확인":
        parts.append(f"위치: {loc}")

    try:
        from .quest import get_state
        active = get_state(session).get("active")
        if active:
            parts.append(f"진행 퀘스트: {active['name']} ({active['node']})")
    except Exception:
        pass

    return " · ".join(parts)


def format_plans() -> str:
    """플랜 선택 안내. 비용 증가 양상을 함께 제시한다(기획 규정)."""
    lines = ["**기억 압축 방식을 선택해 주십시오.** (세션 도중 변경 불가)\n"]
    for key, p in PLANS.items():
        lines.append(
            f"**{p['label']}** — {p['interval']}턴마다\n"
            f"> {p['desc']}\n"
            f"> 비용: {p['cost']}"
        )
    return "\n\n".join(lines)


def cost_curve(session, turns: int = 30) -> list:
    """플랜별 누적 압축 호출 횟수. 비용 증가 양상 그래프의 근거.

    기획 규정 — 대략적 비용 증가 양상을 그래프로 제시한다.

    Returns:
        [{"turn": int, "calls": int}, ...]
    """
    iv = interval(session)
    return [{"turn": t, "calls": t // iv} for t in range(1, turns + 1)]


def compare_curves(turns: int = 30) -> dict:
    """전 플랜의 누적 호출 횟수 비교."""
    return {
        key: [t // int(p["interval"]) for t in range(1, turns + 1)]
        for key, p in PLANS.items()
    }


# ══════════════════════════════════════════════════════════════
#  WP-F — 백그라운드 압축 출처 식별(source identity) · 적용 시 검증
# ══════════════════════════════════════════════════════════════
#  압축은 턴 진행과 독립적으로 늦게 도착할 수 있다. 결과는 '출발 시점의 정본 이력에서
#  파생된 것이 지금도 정본일 때만' 적용한다. 판정 근거:
#    · 기억 세대(memory generation) — 정본 이력을 되돌리는 모든 복원(되감기·재생성 시작/중단,
#      turn_history.restore_reversible)과 압축 적용 자체가 올린다. 정상 턴 커밋(뒤에 append)은
#      올리지 않는다 — 압축과 동시에 진행되는 턴은 정상 경로다.
#    · 접두 지문 — uncompressed_logs[:n] 이 출발 시점 스냅샷과 같아야 한다(수정·삭제 감지).
#  세대는 런타임 값이다. asyncio 태스크는 재시작을 넘지 못하므로 재시작 뒤 늦은 적용은
#  구조적으로 없다(재시작은 새 세대 0에서 시작하고 진행 중 압축 표식 is_compressing 도 초기화).

import hashlib as _hashlib
import json as _json
from dataclasses import dataclass as _dataclass

_GEN_ATTR = "_memory_generation"


def memory_generation(session) -> int:
    return int(getattr(session, _GEN_ATTR, 0) or 0)


def bump_memory_generation(session) -> int:
    g = memory_generation(session) + 1
    setattr(session, _GEN_ATTR, g)
    return g


def _prefix_digest(logs) -> str:
    return _hashlib.sha256(_json.dumps(list(logs), ensure_ascii=False).encode("utf-8")).hexdigest()


@_dataclass(frozen=True)
class CompressionSource:
    """압축 출발 시점의 불변 출처 식별."""
    session_id: str
    generation: int
    prefix_len: int
    prefix_digest: str
    started_turn: int
    commit_marker_tx: str | None
    logs: tuple

    @property
    def text(self) -> str:
        return "\n\n".join(self.logs)


def capture_compression_source(session, logs=None) -> CompressionSource:
    logs = tuple(list(session.uncompressed_logs) if logs is None else list(logs))
    marker = getattr(session, "commit_marker", None) or {}
    return CompressionSource(
        session_id=str(session.session_id), generation=memory_generation(session),
        prefix_len=len(logs), prefix_digest=_prefix_digest(logs),
        started_turn=int(getattr(session, "turn_count", 0) or 0),
        commit_marker_tx=marker.get("transaction_id"), logs=logs)


def compression_source_status(session, source: CompressionSource) -> tuple:
    """(still_canonical: bool, reason: str)."""
    if str(session.session_id) != source.session_id:
        return False, "session_mismatch"
    if memory_generation(session) != source.generation:
        return False, "history_generation_changed"
    cur = list(getattr(session, "uncompressed_logs", None) or [])
    if len(cur) < source.prefix_len:
        return False, "source_logs_truncated"
    if _prefix_digest(cur[:source.prefix_len]) != source.prefix_digest:
        return False, "source_logs_changed"
    return True, "ok"


def apply_compression_result(session, source: CompressionSource, segment: str) -> dict:
    """검증을 통과한 결과만 정본 압축 기억에 반영한다(동기 — 호출자가 직렬화 경계를 쥔다).

    Returns: {"applied": bool, "reason": str, "first": bool}
    """
    ok, reason = compression_source_status(session, source)
    if not ok:
        return {"applied": False, "reason": reason, "first": False}
    first = not bool(session.compressed_memory)
    if first:
        # 되감기용(레거시 비권위 로그) — 압축 발생 시점과 이전 원본. 수용된 결과에만 기록.
        try:
            from .rewind import record_delta
            record_delta(session, getattr(session, "gm_turns_done", 0), [],
                         compression={"occurred": True,
                                      "before": session.compressed_memory or ""})
        except Exception as e:  # noqa: BLE001
            print(f"[되감기] 압축 기록 실패: {e}")
        session.compressed_memory = segment
        # 압축 완료 시점 기록 — 재압축 방지와 로우 플랜 전환 판정의 근거(기존 규정: 최초 생성 시).
        mark_compressed(session)
    else:
        session.compressed_memory += f"\n{segment}"
    del session.uncompressed_logs[:source.prefix_len]
    bump_memory_generation(session)
    return {"applied": True, "reason": "ok", "first": first}
