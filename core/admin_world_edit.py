# 관리자 세계 편집 — typed WorldMutationBatch → CanonicalMutationCoordinator(ADMIN_EDIT) (H06-F)
#
# [권위 — 통합 설계 §53.4 · DESIGN 11 §8 · H06 handoff §8]
#   운영자가 동적 세계 정본을 고치는 유일한 경로다. 세션 필드 직접 쓰기·자유 문자열 해석·provider 호출 없음.
#   · 대상: 동적 세계가 초기화된(INITIALIZED/OPEN) 세션만. 종료(CLOSED)·레거시 세션은 거부.
#   · 진행 중 플레이어 턴·커밋 복구·미종결 정본 변이 → 거부(coordinator 사전 검사).
#     재서술/되감기 진행 중·턴 처리 중 → 거부(이력 조작과 경합하지 않는다).
#   · typed 연산만(production 레지스트리에 있는 op_type) — 정본 id 검증은 각 handler가 한다.
#   · 실제 변화 → world_version 1회 전진 · 완전 no-op → 새 version 없음.
#   · 플레이어 턴 수·Settlement·Ink·CostEvent를 만들지 않는다.
#   · 정의 스냅샷(바인딩)을 바꾸지 않는다 — 정의 교체는 별도의 명시적 migration이다.
from __future__ import annotations

from . import canonical_mutation as _cm
from . import world_mutation as _wm
from . import world_state as _ws

CURRENT = object()            # 기준 생략 = 현재 world_version/definition_revision


async def apply_admin_edit(bot, session, operations, *, edit_id: str,
                           expected_world_version_id=CURRENT,
                           expected_definition_revision=CURRENT) -> _cm.CanonicalMutationResult:
    """operations: [{"op_type": str, "payload": dict}] (같은 분 순서대로 인과 사슬로 묶인다).

    expected_*: 운영자가 편집안을 만들 때 본 기준(생략하면 현재). 다르면 stale로 거부된다.
    """
    from . import player_world as _pw
    from . import turn_history as _th
    from . import world_lifecycle as _wl
    lc = getattr(session, "world_lifecycle", None)
    if lc not in (_wl.LIFECYCLE_INITIALIZED, _wl.LIFECYCLE_OPEN):
        return _cm.CanonicalMutationResult(_cm.OUTCOME_REJECTED,
                                           reason="동적 세계가 초기화된 열린 세션이 아닙니다")
    if getattr(session, "is_processing", False) or _th.read_op(session.session_id):
        return _cm.CanonicalMutationResult(_cm.OUTCOME_REJECTED,
                                           reason="턴 처리 또는 이력 조작(재서술·되감기)이 진행 중입니다")
    if not isinstance(edit_id, str) or not edit_id:
        return _cm.CanonicalMutationResult(_cm.OUTCOME_REJECTED, reason="edit_id가 필요합니다")
    ops = []
    for n, raw in enumerate(operations or ()):
        t = (raw or {}).get("op_type")
        if t not in _wm.PRODUCTION_REGISTRY.op_types():
            return _cm.CanonicalMutationResult(_cm.OUTCOME_REJECTED, reason=f"typed 연산이 아닙니다: {t!r}")
        ops.append(_wm.make_op(f"adm.{n}", t, "ADMIN_EDIT", dict(raw.get("payload") or {})))
    if not ops:
        return _cm.CanonicalMutationResult(_cm.OUTCOME_REJECTED, reason="연산이 없습니다")
    cause_id = f"admin:{edit_id}"
    batch = _cm.new_batch(session, cause_kind=_ws.CAUSE_ADMIN_EDIT, cause_id=cause_id)
    if expected_world_version_id is not CURRENT or expected_definition_revision is not CURRENT:
        batch = _wm.WorldMutationBatch(
            batch_id=batch.batch_id, outer_origin=_ws.CAUSE_ADMIN_EDIT,
            source_world_version_id=(batch.source_world_version_id if expected_world_version_id is CURRENT
                                     else expected_world_version_id),
            definition_revision=(batch.definition_revision if expected_definition_revision is CURRENT
                                 else expected_definition_revision))
    for op in _pw.chain(*[[o] for o in ops]):
        batch.add(op)
    return await _cm.CanonicalMutationCoordinator(bot).apply(
        session, batch, cause_kind=_ws.CAUSE_ADMIN_EDIT, cause_id=cause_id)
