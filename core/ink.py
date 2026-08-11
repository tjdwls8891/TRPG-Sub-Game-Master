# 게임머니 '잉크' 환산 — 원화 비용 ↔ 잉크 차감액, 충전 플랜 가격 산출
#
# [환산 체계]
#   판매가:   1 잉크 = 10원 (INK_UNIT_KRW)
#   실수령:   디스코드 결제 수수료 30% 차감 후 잉크당 7원 (INK_NET_KRW)
#   차감식:   API 원화 비용 X를 충당하려면 ceil(X / 7) 잉크가 필요하다.
#             (잉크 1개가 실제로 벌어들이는 금액이 7원이므로)
#
# 이 모듈은 순수 계산만 담당한다. 잔액 보관·차감 실행은 core/accounts.py 소관.
import math

from .constants import INK_UNIT_KRW, INK_NET_KRW, INK_PLANS


def cost_to_ink(cost_krw: float) -> int:
    """API 원화 비용을 청구할 잉크 수로 환산한다.

    수수료 차감 후 실수령액(잉크당 7원) 기준으로 비용을 충당해야 하므로
    ceil(비용 / 7)을 적용한다. 비용이 0 이하면 0잉크.

    Args:
        cost_krw: 원화 기준 API 비용

    Returns:
        차감할 잉크 수 (0 이상 정수)
    """
    if cost_krw is None or cost_krw <= 0:
        return 0
    return math.ceil(cost_krw / INK_NET_KRW)


def ink_to_krw(ink: int) -> int:
    """잉크 수를 판매가(원)로 환산한다. 충전 플랜 가격 표기에 사용."""
    return int(ink) * INK_UNIT_KRW


def ink_to_net_krw(ink: int) -> int:
    """잉크 수를 수수료 차감 후 실수령액(원)으로 환산한다. 수익성 점검용."""
    return int(ink) * INK_NET_KRW


def plan_catalog() -> list:
    """충전 플랜 목록을 반환한다.

    Returns:
        [{"ink": 100, "price_krw": 1000, "net_krw": 700}, ...]
    """
    return [
        {"ink": ink, "price_krw": ink_to_krw(ink), "net_krw": ink_to_net_krw(ink)}
        for ink in INK_PLANS
    ]


def can_afford(balance_ink: int, cost_krw: float) -> bool:
    """예상 비용을 감당할 잔액이 있는지 판정한다.

    기획 규정: 턴 진행 시도 전 소지금과 '다음 턴 예상 최대금액'을 비교하고
    소지금이 적으면 플레이를 차단한다.
    """
    return int(balance_ink) >= cost_to_ink(cost_krw)


def format_ink(ink: int) -> str:
    """잉크 수를 표기용 문자열로 변환한다. 예: 1250 -> '1,250잉크'"""
    return f"{int(ink):,}잉크"


def refund_ink(prepaid_ink: int, used_cost_krw: float) -> int:
    """선결제분 중 미사용액을 환급할 잉크 수로 산출한다.

    기획 규정: 캐시 만료 전 UI로 세션을 오프하면 해당 시점까지의 금액으로
    재계산하여 선결제액과의 차액을 돌려준다.

    실제 사용분은 cost_to_ink와 동일한 올림 규칙으로 환산하므로,
    환급액이 선결제액을 초과하는 일은 없다. 음수는 0으로 절삭한다.

    Args:
        prepaid_ink:   세션 오픈 시 선결제한 잉크 수
        used_cost_krw: 실제 경과 시간까지 발생한 원화 비용

    Returns:
        환급할 잉크 수 (0 이상)
    """
    used_ink = cost_to_ink(used_cost_krw)
    return max(0, int(prepaid_ink) - used_ink)
