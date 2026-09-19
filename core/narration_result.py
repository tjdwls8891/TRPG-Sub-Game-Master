# 묘사 생성/전달 경계 결과 타입 (WP-A — Narration Boundary & Output Ownership)
#
# 설계 원칙(감독 승인 조건):
#   · Discord 객체에 의존하지 않는 순수 결과 데이터 구조.
#   · 현재 전달(delivery) 경로가 실제로 소비하는 정보만 담는다.
#   · CostEvent의 금융 사실(비용/토큰)을 복제하는 별도 narration-cost 모델을
#     만들지 않는다. 비용은 CostLedger/turn_cost_log의 소관이며 이 결과에는 담지 않는다.
#   · 불변 값 객체(frozen dataclass)로 둔다. 불필요한 추상화는 추가하지 않는다.
#
# WP-A 범위 한정:
#   · 여기서 만들어지는 message ID들은 "현재 runtime attempt가 만든 bot-authored
#     출력"을 식별/정리하기 위한 것이지, durable canonical committed history가
#     생겼다는 뜻이 아니다. COMMITTED/SUPERSEDED/REWOUND 역사는 WP-D/E 소관이다.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NarrationResult:
    """묘사 생성(provider 호출 + 검증 + 후처리)의 최종 산출물.

    Discord 전달과 무관하게 단독으로 만들어질 수 있으며, 정상 턴 확정
    (canonical 로그/카운터/상태/청구/저장)의 부작용을 포함하지 않는다.
    전달 단계가 필요로 하는 최종 텍스트와 구조화 정보만 담는다.

    필드
        text           — PC 자율성 필터·태그 스트립까지 마친 최종 플레이어 노출 묘사 전문.
        narrative_text — text에서 말미 코드블럭을 제외한 서사 본문(비정규 NPC 배정·스트리밍 입력).
        code_block_text— 말미 코드블럭(없으면 "").
        paragraphs     — 문단 분리·동일화자 병합·PC 화자 문단 제거를 마친 스트리밍 단위.
        top/mid/bottom_images — instruction에서 파싱된 이미지 지시(상/중/하). 전달 단계가 인터리브한다.
    """

    text: str
    narrative_text: str
    code_block_text: str
    paragraphs: tuple[str, ...] = ()
    top_images: tuple[str, ...] = ()
    mid_images: tuple[str, ...] = ()
    bottom_images: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeliveryResult:
    """Discord 전달(스트리밍·이미지·TTS·코드블럭 송출)이 만든 산출을 caller에게 보고한다.

    핵심 요구: 부분 전달 실패에서도 이미 생성된 메시지 ID를 잃지 않는다. 따라서
    ok=False(부분/전체 실패)여도 그 시점까지 생성된 canonical_message_ids는 채워진다.

    필드
        ok                   — 전달이 예외 없이 끝났는가.
        canonical_message_ids— 이번 전달이 만든 bot-authored 묘사 메시지 ID들(생성 순서).
        transient_message_ids— 대기/진행 안내 등 일시 메시지 ID들(WaitingStatus 등).
        media_message_ids    — 이미지/미디어 메시지 ID들. WP-A에서는 수집하지 않으며(미디어
                               헬퍼 미개조, 전체 lifecycle은 WP-F), 인터페이스 완결성 위해 유지.
        delivery_error       — 실패 시 사유(정상 시 None).
    """

    ok: bool
    canonical_message_ids: tuple[int, ...] = ()
    transient_message_ids: tuple[int, ...] = ()
    media_message_ids: tuple[int, ...] = ()
    delivery_error: str | None = None


class NarrationDeliveryError(Exception):
    """전달 도중 실패. 이미 생성된 bot-authored 메시지 ID를 함께 실어 caller가 잃지 않게 한다.

    부분 전달(예: 메시지 1·2 성공, 3 전송 중 예외)에서도 caller가 1·2의 ID를 회수해
    좁은 정리(clear_messages)나 현재 attempt 귀속을 수행할 수 있게 한다. message에는 원
    예외 문자열을 그대로 담아 shell의 기존 오류 표시가 동일하게 동작하도록 한다.
    """

    def __init__(self, message: str = "", *,
                 canonical_message_ids=(), transient_message_ids=()):
        super().__init__(message)
        self.canonical_message_ids = tuple(canonical_message_ids)
        self.transient_message_ids = tuple(transient_message_ids)
