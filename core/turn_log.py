# turn.summary — 자동 논리 턴 시도 1건의 작동 요약(P3 G3, 에이전트 판독용)
#
# 트랜잭션이 대기(WAITING_*)·종료(COMMITTED·FAILED_SYSTEM·ABORTED·SUPERSEDED)에 이를 때 domain.jsonl에 1줄.
# 이미 메모리에 있는 값(트랜잭션·준비 객체·WorldTurn·호출 참조)을 모아 쓰기만 한다 — 정본·청구에 영향 없음.
# 청구·세계 연산 원본은 같은 tx의 commit·turn.delta·cost.event 줄이 권위다(여기는 개요·조인 참조).
# 실패는 전부 흡수(로그가 게임을 막지 않는다).
from __future__ import annotations

import time
from collections import Counter

from . import logs as _logs

_stages: dict = {}          # tx → {status: ms since created}
WAITING = ("WAITING_FOR_PLAYER", "WAITING_FOR_ROLL")
TERMINAL = ("COMMITTED", "FAILED_SYSTEM", "ABORTED", "SUPERSEDED")


def _val(x):
    return getattr(x, "value", x)


def note_status(tx, status) -> None:
    """상태 전이 시각(트랜잭션 생성 기준 ms) — 단계별 소요 계산용. 최초 도달만 남긴다."""
    try:
        st = _val(status)
        d = _stages.setdefault(tx.transaction_id, {})
        d.setdefault(st, int((time.time() - (tx.created_at or time.time())) * 1000))
        if st in WAITING or st in TERMINAL:
            emit(tx, st)
        if st in TERMINAL:
            _stages.pop(tx.transaction_id, None)
            _logs.forget_calls(tx.transaction_id)
    except Exception as e:  # noqa: BLE001
        _logs._stderr(f"[로그] turn.summary 실패: {e}")


def _judgment(tx) -> dict:
    jr = getattr(tx, "judgment_result", None) or {}
    j = jr.get("judgment") or {}
    gate = jr.get("gate") or {}
    out = {"action": j.get("action"), "player_intent": j.get("player_intent"),
           "rolls": len(j.get("rolls") or ()), "roll_results": list(jr.get("roll_results") or ()) or None}
    if gate:
        out["gate"] = {k: gate.get(k) for k in ("classification", "resolution", "target", "reason") if gate.get(k)}
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _world(wt) -> dict | None:
    if wt is None:
        return None
    out = {}
    intent = getattr(wt, "intent", None)
    if intent is not None:
        try:
            out["intent"] = intent.semantic_record()
        except Exception:  # noqa: BLE001
            pass
    plan = getattr(wt, "plan", None)
    if plan is not None:
        o = getattr(plan, "outcome", None) or {}
        out["plan"] = {"status": getattr(plan, "status", None), "reason": getattr(plan, "reason", None),
                       "outcome": o.get("kind"), "minutes": getattr(plan, "code_owned_minutes", None),
                       "start_minute": getattr(plan, "start_minute", None), "end_minute": getattr(plan, "end_minute", None),
                       "scene_place": getattr(plan, "scene_place", None),
                       "signals": len(getattr(plan, "signals", None) or ())}
    if getattr(wt, "tie_choice", None):
        out["tie_choice"] = wt.tie_choice
    for k in ("identified_names", "identified_faces"):
        if getattr(wt, k, None):
            out[k] = list(getattr(wt, k))
    res = getattr(wt, "result", None)
    if res is not None:
        out["observation"] = {"status": getattr(res, "status", None),
                              "exposures": list(getattr(res, "exposures", None) or ()),
                              "represented": [list(x) for x in (getattr(res, "represented", None) or ())],
                              "pending_after": len(getattr(res, "pending", None) or ()),
                              "diagnostics": [d.get("code") for d in (getattr(res, "diagnostics", None) or ())
                                              if isinstance(d, dict)]}
    return out or None


def _prep(prep) -> dict | None:
    if prep is None:
        return None
    out = {"phase": getattr(prep, "phase", None), "failure_code": getattr(prep, "failure_code", None),
           "failure_stage": getattr(prep, "failure_stage", None),
           "violations": list(getattr(prep, "violations", None) or ()) or None}
    narr = getattr(prep, "narration", None)
    if narr is not None:
        out["narration"] = {"chars": len(getattr(narr, "text", "") or ""),
                            "paragraphs": len(getattr(narr, "paragraphs", ()) or ())}
    out["extraction"] = getattr(prep, "extraction_plan", None) is not None
    if getattr(prep, "identity_reveals", None):
        out["identity_reveals"] = dict(prep.identity_reveals)
    batch = getattr(prep, "world_mutation_batch", None)
    ops = list(getattr(batch, "_ops", None) or ()) if batch is not None else []
    if ops:
        out["world_ops"] = dict(Counter((o.get("op_type") if isinstance(o, dict) else getattr(o, "op_type", None)) or "?"
                                        for o in ops))
    ids = getattr(prep, "frozen_cost_event_ids", None)
    if ids:
        out["cost_events"] = list(ids)
    delivery = getattr(prep, "delivery", None)
    if delivery is not None:
        out["delivery_ok"] = bool(getattr(delivery, "ok", False))
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def emit(tx, status: str) -> None:
    data = {"tx": tx.transaction_id, "turn": tx.logical_turn, "attempt": tx.attempt, "status": status,
            "partial": status in WAITING, "declaration": tx.player_declaration,
            "failure": {k: getattr(tx, k) for k in ("failure_stage", "failure_code", "failure_message")
                        if getattr(tx, k, None)} or None,
            "dur_ms": int((time.time() - (tx.created_at or time.time())) * 1000),
            "stages_ms": dict(_stages.get(tx.transaction_id) or {}),
            "judgment": _judgment(tx) or None,
            "world": _world(getattr(tx, "world_turn", None)),
            "prep": _prep(getattr(tx, "preparation", None)),
            "calls": _logs.calls_of(tx.transaction_id)}
    _logs.domain("turn.summary", {k: v for k, v in data.items() if v not in (None, [], {})}, sid=tx.session_id)
