"""WP-C 배리어 테스트 대역.

`_dispatch_proceed`를 대체하는 가짜 디스패치를 만든다. 실제 자동 경로와 같은
계약을 따른다:
    · 준비 객체에 확정 묘사와 스테이징 로그를 싣는다(정본 미변경).
    · 추출을 **등록 준비 작업**으로 발사한다(배리어가 합류할 수 있도록).
    · 전달 결과(DeliveryResult)를 기록한다.
모델·디스코드 호출은 없다. 정본 적용은 오직 READY 이후 legacy continuation이 한다.
"""

from __future__ import annotations

import asyncio


def begin_tx(session, declaration: str = "선언"):
    import core
    return core.turn_transaction.get_or_begin_turn_transaction(session, declaration)


def make_prepared_dispatch(*, text: str = "숲길로 걸어간다.",
                           extraction_result: dict | None = None,
                           extraction_coro=None,
                           extraction_fails: bool = False,
                           delivery_ok: bool = True,
                           delivery_gate: asyncio.Event | None = None,
                           order: list | None = None,
                           hold_processing: bool = True):
    """가짜 `_dispatch_proceed(session, instruction, *, transaction_id, preparation)`.

    extraction_coro(prep) 가 주어지면 그 코루틴이 추출 작업 본체가 된다.
    delivery_gate 가 주어지면 전달은 그 이벤트를 기다린 뒤 끝난다(겹침 검증용).
    """
    import core
    from core.narration_result import DeliveryResult, NarrationResult
    from google.genai import types

    TP = core.turn_preparation

    async def _default_extraction(prep):
        if order is not None:
            order.append("extraction_start")
        await asyncio.sleep(0)
        if extraction_fails:
            if order is not None:
                order.append("extraction_failed")
            raise RuntimeError("추출 실패(테스트)")
        plan = TP.build_extraction_plan(
            _session_ref["s"], extraction_result or {},
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt)
        prep.extraction_plan = plan
        if order is not None:
            order.append("extraction_done")
        return plan

    _session_ref = {}

    async def _dispatch(session, instruction, *, transaction_id=None, preparation=None):
        _session_ref["s"] = session
        prep = preparation or TP.ensure_preparation(session, transaction_id)
        if order is not None:
            order.append("dispatch_proceed")
        if hold_processing:
            session.is_processing = True
            prep.processing_held = True
        narr = NarrationResult(text=text, narrative_text=text, code_block_text="",
                               paragraphs=(text,))
        prep.narration = narr
        prep.staged_log = {
            "raw_entries": [
                types.Content(role="user", parts=[types.Part.from_text(text="선언")]),
                types.Content(role="model", parts=[types.Part.from_text(text=text)]),
            ],
            "uncompressed_entries": ["[플레이어 및 GM]: 선언", f"[GM 묘사]: {text}"],
            "consumed_turn_logs": len(session.current_turn_logs),
            "turn_no": int(session.turn_count) + 1,
            "applied": False,
        }
        prep.extraction_text = text
        body = extraction_coro(prep) if extraction_coro is not None else _default_extraction(prep)
        prep.register_task("extraction", TP.TASK_EXTRACTION, coro=body,
                           required_success=True)
        prep.register_task("delivery", TP.TASK_DELIVERY, required_success=True,
                           required_player_output=True, blocks_cost_closure=False,
                           turn_cost_membership=False)
        if order is not None:
            order.append("delivery_start")
        if delivery_gate is not None:
            await delivery_gate.wait()
        else:
            await asyncio.sleep(0)
        if delivery_ok:
            prep.delivery = DeliveryResult(ok=True, canonical_message_ids=(9001,))
            prep.mark_task("delivery", TP.TASK_SUCCEEDED)
        else:
            prep.delivery_error = RuntimeError("전달 실패(테스트)")
            prep.mark_task("delivery", TP.TASK_FAILED, error="전달 실패")
        if order is not None:
            order.append("delivery_done")
        prep.proceed_history_entry = {"instruction": instruction, "context": [],
                                      "ai_summary": text[:500]}
        prep.narrative_progress = text[:500]
        return {"ok": delivery_ok, "ai_text": text, "error": None, "finalized": True}

    return _dispatch


def install_game_cog_stub(bot):
    """release_turn_processing만 가진 GameCog 대역(처리 잠금 해제 확인용)."""
    class _GameCogStub:
        released = 0

        async def release_turn_processing(self, session, *, master_guild=None):
            type(self).released += 1
            session.is_processing = False

    stub = _GameCogStub()
    bot.add_cog_stub("GameCog", stub)
    return stub
