"""P-001 ~ P-003 — 청구 정책.

WP00_TEST_HARNESS_SPEC.md §6 기준.

프로덕션 정산 서비스가 아직 없으므로 **순수 정책 모델**에 대해 먼저 쓴다.
WP-05 이후 실제 `TurnSettlement`/`InkTransaction`으로 대상을 바꾼다.

여기서 기술하는 것은 사용자 확정 정책이다.
  · 시스템 실패는 플레이어에게 청구하지 않는다.
  · 재렌더가 앞선 시도의 청구를 취소하지 않는다.
  · 되감기는 이미 발생한 청구를 되돌리지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pytest

pytestmark = pytest.mark.policy


class Outcome(str, Enum):
    COMMITTED = "committed"
    FAILED_SYSTEM = "failed_system"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class CostEvent:
    """제공자 사용 관측 1건. 불변·추가 전용."""
    operation_id: str
    provider_attempt: int
    krw: float


@dataclass
class Attempt:
    """한 논리 턴의 한 시도."""
    logical_turn: int
    attempt: int
    outcome: Outcome = Outcome.COMMITTED
    cost_events: list[CostEvent] = field(default_factory=list)

    @property
    def provider_krw(self) -> float:
        return sum(e.krw for e in self.cost_events)


def player_charge_krw(attempt: Attempt) -> float:
    """정책 — 시스템 실패 시도는 플레이어에게 청구하지 않는다."""
    if attempt.outcome is Outcome.FAILED_SYSTEM:
        return 0.0
    return attempt.provider_krw


@dataclass
class Ledger:
    """추가 전용 원장. 되감기가 건드리지 않는다."""
    entries: list = field(default_factory=list)

    def charge(self, attempt: Attempt) -> float:
        amount = player_charge_krw(attempt)
        if amount > 0:
            self.entries.append((attempt.logical_turn, attempt.attempt, amount))
        return amount

    @property
    def total(self) -> float:
        return sum(e[2] for e in self.entries)

    def rewind_game_state_to(self, logical_turn: int) -> None:
        """게임 상태 되감기 — 원장은 건드리지 않는다."""
        return None


# ── P-001 시스템 실패 청구 ──────────────────────────────────

def test_p001_system_failure_is_not_billed():
    """P-001 — 제공자 비용 > 0 이지만 FAILED_SYSTEM이면 청구 0."""
    a = Attempt(logical_turn=8, attempt=1, outcome=Outcome.FAILED_SYSTEM,
                cost_events=[CostEvent("op-1", 1, 30.0)])
    assert a.provider_krw == 30.0, "제공자 비용은 사라지지 않는다"
    assert player_charge_krw(a) == 0.0


def test_p001b_provider_cost_is_still_recorded_on_failure():
    """실패해도 제공자 비용 관측은 남는다 — 운영자가 부담한 사실이다."""
    ledger = Ledger()
    a = Attempt(8, 1, Outcome.FAILED_SYSTEM,
                [CostEvent("op-1", 1, 30.0)])
    charged = ledger.charge(a)
    assert charged == 0.0
    assert ledger.total == 0.0
    assert a.provider_krw == 30.0


def test_p001c_committed_attempt_is_billed():
    a = Attempt(8, 1, Outcome.COMMITTED, [CostEvent("op-1", 1, 30.0)])
    assert player_charge_krw(a) == 30.0


# ── P-002 재렌더 청구 ───────────────────────────────────────

def test_p002_rerender_keeps_previous_charge():
    """P-002 — 시도 1이 커밋된 뒤 재렌더로 시도 2가 시작돼도
    시도 1의 청구는 남는다."""
    ledger = Ledger()
    first = Attempt(8, 1, Outcome.COMMITTED, [CostEvent("op-1", 1, 20.0)])
    ledger.charge(first)

    second = Attempt(8, 2, Outcome.COMMITTED, [CostEvent("op-2", 1, 25.0)])
    ledger.charge(second)

    assert ledger.total == 45.0, "재렌더가 앞선 청구를 취소했습니다"
    assert len(ledger.entries) == 2
    assert ledger.entries[0][:2] == (8, 1)
    assert ledger.entries[1][:2] == (8, 2)


def test_p002b_failed_rerender_does_not_add_charge():
    ledger = Ledger()
    ledger.charge(Attempt(8, 1, Outcome.COMMITTED,
                          [CostEvent("op-1", 1, 20.0)]))
    ledger.charge(Attempt(8, 2, Outcome.FAILED_SYSTEM,
                          [CostEvent("op-2", 1, 25.0)]))
    assert ledger.total == 20.0


# ── P-003 되감기 청구 ───────────────────────────────────────

def test_p003_rewind_does_not_reverse_charges():
    """P-003 — 커밋된 턴을 나중에 되감아도 청구는 불가역이다."""
    ledger = Ledger()
    ledger.charge(Attempt(8, 1, Outcome.COMMITTED,
                          [CostEvent("op-1", 1, 20.0)]))
    ledger.charge(Attempt(9, 1, Outcome.COMMITTED,
                          [CostEvent("op-2", 1, 25.0)]))
    before = ledger.total

    ledger.rewind_game_state_to(8)

    assert ledger.total == before, "되감기가 청구 이력을 바꿨습니다"
    assert len(ledger.entries) == 2


def test_p003b_cost_events_are_frozen():
    """CostEvent는 불변이어야 한다 — 사후 조정이 불가능해야 한다."""
    e = CostEvent("op-1", 1, 20.0)
    with pytest.raises(Exception):
        e.krw = 0.0  # type: ignore[misc]


# ── 현행 구현과의 대조 ──────────────────────────────────────

def test_p001_current_implementation_charges_on_narration_failure():
    """대조 — 현행(WP-C) 구현은 사전 READY 실패 경로에서 차감을 호출하지 않는다.

    legacy 차감은 READY 이후 continuation 안에만 있고, 실패 처리부에는 없다.
    (Settlement 기반 청구 권위 전환·FAILED_SYSTEM 청구 0의 권위화는 WP-D.)
    """
    import ast

    from tests.conftest import source_of

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)

    def body_of(name):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
        return "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])

    owner = body_of("_finish_proceed_and_continue")
    assert "_OUTCOME_READY" in owner and "_handle_preparation_failure" in owner, (
        "READY/실패 분기가 사라졌습니다 — 청구 정책 상태를 재확인하십시오")
    assert "deduct_ink" not in owner
    assert "deduct_ink" not in body_of("_handle_preparation_failure")
    assert "deduct_ink" not in body_of("_join_and_ready")
    assert "deduct_ink" in body_of("_post_ready_legacy_continuation")
