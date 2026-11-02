"""자동 턴 한 시도의 흐름 결과 표식·추출 provider 실패 예외·판정 화면 열림 표식(adapters/discord에서 옮김 — 값·동작 동일)."""
from indaim.turn import turn_transaction as _turn_transaction

_OUTCOME_READY = "READY"
_OUTCOME_NO_NARRATION = "NO_NARRATION"
_OUTCOME_DELIVERY_FAILED = "DELIVERY_FAILED"
_OUTCOME_EXTRACTION_FAILED = "EXTRACTION_FAILED"
_OUTCOME_NOT_READY = "NOT_READY"
_OUTCOME_SUPERSEDED = "SUPERSEDED"      # 더 새로운 시도가 현재 — 이 시도는 흐름을 소유하지 않음


class ExtractionProviderFailure(RuntimeError):
    """v7.29: 추출 provider 호출·파싱이 결과를 내지 못함(재시도 소진·stale) — 응답 처리 실패와 구별한다."""


def _set_roll_open(session, transaction_id, value: bool) -> None:
    """v7.33(F4): 판정 화면 열림 표식 — 같은 시도일 때만(낡은 화면이 새 시도를 건드리지 않음)."""
    tx = _turn_transaction.get_active_transaction(session)
    if tx is not None and transaction_id is not None and tx.transaction_id == transaction_id:
        tx.roll_open = value
