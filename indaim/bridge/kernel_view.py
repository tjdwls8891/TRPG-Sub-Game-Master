"""커널 세계 → 옛 엔진이 읽는 ProjectedWorld(게임 정본 11도메인).

도메인은 변환기 역변환(v4.revert)으로 만든다 — 사건이 붙이고 푼 인물 상태 기록(actor.vitals.event_marks)은 옛 사건
지역 값 장부 키로 되살아나므로 옛 엔진의 제거 판정이 그대로 돈다. 커밋이 반영하는 사본 장부(파티·성장·물건 지급)는
커널에 없다 — 그 턴 연산이 투영 위에 새로 쓰므로 '이번 턴 새 기록' 계산이 같다.
"""
from core import world_mutation as _wm

from indaim.migrate import v4


class KernelProjectedWorld(_wm.ProjectedWorld):
    """커널 세계를 담은 ProjectedWorld — 옛 엔진 함수에 그대로 넘길 수 있다."""

    def __init__(self, conv: "v4.Converted", *, definition_revision=None):
        fields = v4.revert(conv)
        super().__init__({k: fields.get(k) for k in _wm.PROJECTED_DOMAINS})
        self.definition_revision = definition_revision          # 세션 정체성(바인딩) — 세계 밖 값, 낡은 정의 판정이 읽는다

    def clone(self) -> "KernelProjectedWorld":
        w = super().clone()
        w.__class__ = KernelProjectedWorld
        w.definition_revision = self.definition_revision
        return w
