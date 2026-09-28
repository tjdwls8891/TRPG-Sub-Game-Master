# core.interpretation_billing — WP-F 캐시 시간 해석 비용의 durable 청구 owner (POLICY-CACHE-01)
#
# [정책] 자연어 유지 시간 해석은 캐시와 별개인 '이미 수행된 AI 서비스'다.
#   · provider 해석 사실이 생기면 곧바로 durable 청구 대상이 된다 — 캐시 열기·취소·
#     시간 만료·크래시 재시작 어느 경우든 같은 결과(청구 대상이면 정확히 한 번).
#   · 캐시 선불/환급(core.cache_lifecycle)과 분리된다 — 창 ID와 무관한 자체 식별자,
#     캐시 REFUND 가 절대 건드리지 않는다.
#   · 기존 면제 규칙 보존: 미청구 누적이 2잉크(INTERPRET_CHARGE_THRESHOLD) 미만이면
#     청구하지 않고 이어서 누적하며, 세션을 열 때 남은 미만분은 면제(WAIVED)된다.
#
# [식별·감사]
#   interp_id  = 해석 provider 오퍼레이션 ID(CostLedger operation_id) — CostEvent 메타데이터
#                (interp_id, interpretation_billing=2)와 1:1 로 대조된다.
#   charge_id  = 청구에 묶인 interp_id 집합의 결정적 지문 — 재시작해도 같은 ID(이중 debit 방지).
#   계정 효과  = LifecycleInkTransaction(kind=INTERPRETATION_CHARGE, reference_id=charge_id)
#                — 기존 exactly-once 계정 마커·원장 프리미티브 재사용.
#
# [저널] sessions/{id}/interpretation_billing.jsonl — append-only, fsync
#   INTERPRETED   {interp_id, cost_krw, cost_usd, adopted?}
#   CHARGE_INTENT {charge_id, interp_ids[], krw, ink, payers{uid: ink}}
#   CHARGED       {charge_id}
#   WAIVED        {interp_ids[], krw, reason}
#
# [크래시 경계]
#   CostEvent 후 INTERPRETED 전 → reconcile 이 CostLedger 의 표식된 해석 사실을 채택
#   INTERPRETED 후 CHARGE_INTENT 전 → settle 이 누적 재계산 후 청구
#   CHARGE_INTENT 후 계정 효과 전/후 → 같은 charge_id 로 재실행(마커가 이중 debit 차단)

from __future__ import annotations

import hashlib
import json
import os
import time

JOURNAL_FILE = "interpretation_billing.jsonl"
REFERENCE_KIND = "CACHE_TIME_INTERPRETATION"
BILLING_MARK = 2                      # CostEvent metadata["interpretation_billing"] — 이 구조로 청구되는 사실


class InterpretationBillingError(RuntimeError):
    pass


def journal_path(session_id) -> str:
    return os.path.join("sessions", str(session_id), JOURNAL_FILE)


def _append(session_id, event: dict) -> dict:
    path = journal_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ev = dict(event, ts=time.time())
    line = json.dumps(ev, ensure_ascii=False) + "\n"
    pre = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            with open(path, "r+b") as f:
                f.truncate(pre)
        except Exception:
            pass
        raise InterpretationBillingError(f"해석 청구 저널 기록 실패: {path}") from e
    return ev


def load_events(session_id) -> list:
    path = journal_path(session_id)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            s = raw.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception as e:
                raise InterpretationBillingError(f"{path}:{lineno} 저널 손상") from e
            out.append(obj)
    return out


class BillingView:
    def __init__(self, events: list):
        self.interpreted: dict = {}       # interp_id -> ev
        self.intents: dict = {}           # charge_id -> ev
        self.charged: set = set()
        self.covered: set = set()         # 청구 의도 또는 면제로 처분된 interp_id
        for ev in events:
            t = ev.get("type")
            if t == "INTERPRETED":
                self.interpreted.setdefault(ev["interp_id"], ev)
            elif t == "CHARGE_INTENT":
                self.intents[ev["charge_id"]] = ev
                self.covered.update(ev.get("interp_ids") or [])
            elif t == "CHARGED":
                self.charged.add(ev["charge_id"])
            elif t == "WAIVED":
                self.covered.update(ev.get("interp_ids") or [])

    def pending_ids(self) -> list:
        return [i for i in self.interpreted if i not in self.covered]

    def pending_krw(self) -> float:
        return sum(float(self.interpreted[i].get("cost_krw") or 0.0) for i in self.pending_ids())


def view(session_id) -> BillingView:
    return BillingView(load_events(session_id))


def charge_id_for(session_id, interp_ids) -> str:
    digest = hashlib.sha256("|".join(sorted(interp_ids)).encode("utf-8")).hexdigest()[:24]
    return f"interp:{session_id}:{digest}"


def payers_of(session) -> list:
    """청구 대상 — 기존 규약(세션 참가자, 없으면 개설자)."""
    return [str(u) for u in (getattr(session, "players", None) or {})] or \
        [str(u) for u in [getattr(session, "creator_uid", "")] if u]


def _mirror(session, bv: BillingView) -> None:
    """레거시 표시·판정 호환 — interpret_cost_krw = 미청구(면제 전) 누적. 권위는 저널."""
    session.interpret_cost_krw = round(bv.pending_krw(), 6)


# ══════════════════════════════════════════════════════════════
#  기록 / 재구성
# ══════════════════════════════════════════════════════════════

def record_interpretation(session, *, interp_id: str, cost_krw: float, cost_usd: float,
                          cost_event_id: str | None = None) -> bool:
    """strict provider 해석 사실(CostEvent) 이 durable 해진 **뒤에만** 청구 후보로 기록한다(멱등).

    호출자는 ProviderOperation.record_fact_strict 성공(canonical event_id)을 먼저 확보해야 한다.
    """
    sid = session.session_id
    bv = view(sid)
    if interp_id in bv.interpreted:
        return False
    _append(sid, {"type": "INTERPRETED", "interp_id": interp_id,
                  "cost_krw": float(cost_krw or 0.0), "cost_usd": float(cost_usd or 0.0),
                  "cost_event_id": cost_event_id})
    return True


def reconcile_from_ledger(bot, session) -> int:
    """CostEvent 는 기록됐지만 INTERPRETED 전에 끊긴 해석을 채택한다(표식된 사실만).

    청구 복구 근거이므로 strict 로 읽는다 — 손상·중복 identity 행을 조용히 건너뛰어
    잘못된 결론을 내리지 않고 예외로 멈춘다(fail-closed: 채택 없음, 이미 저널된 청구 후보는
    각자 strict 사실을 근거로 계속 유효).
    """
    from . import cost_ledger as CL
    ledger = CL.get_ledger(bot)
    if ledger is None:
        return 0
    sid = session.session_id
    known = set(view(sid).interpreted)
    n = 0
    for ev in ledger.list_cost_events_strict(session_id=sid):
        md = ev.get("metadata") or {}
        if ev.get("operation") != CL.OP_CACHE_TIME_INTERPRET:
            continue
        if md.get("interpretation_billing") != BILLING_MARK or not md.get("interp_id"):
            continue                 # WP-F 이전(레거시 선불 합산) 사실은 재청구하지 않는다
        iid = md["interp_id"]
        if iid in known:
            continue
        _append(sid, {"type": "INTERPRETED", "interp_id": iid,
                      "cost_krw": float(ev.get("cost_krw") or 0.0),
                      "cost_usd": float(ev.get("cost_usd") or 0.0), "adopted": True,
                      "cost_event_id": ev.get("event_id")})
        known.add(iid)
        n += 1
    return n


# ══════════════════════════════════════════════════════════════
#  청구
# ══════════════════════════════════════════════════════════════

async def _execute_intent(intent: dict) -> bool:
    from . import ink_transactions as IT
    ok = True
    for uid, ink in (intent.get("payers") or {}).items():
        try:
            await IT.execute_lifecycle_ink(
                reference_kind=REFERENCE_KIND, reference_id=intent["charge_id"], user_id=uid,
                kind=IT.KIND_INTERPRETATION_CHARGE, nominal_ink=int(ink),
                reason="cache_time_interpretation")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[해석 청구] 계정 적용 실패(재시도 대기) uid={uid}: {type(e).__name__}: {e}")
    return ok


async def settle(bot, session) -> dict:
    """재구성 → 중단된 청구 재개 → 미청구 누적이 임계 이상이면 청구. 멱등.

    Returns: {"charged_ink": int(이번 호출에서 새로 확정한 1인당 잉크), "pending_krw": float,
              "complete": bool}
    """
    from .ink import cost_to_ink
    from .session_open import INTERPRET_CHARGE_THRESHOLD
    sid = session.session_id
    try:
        reconcile_from_ledger(bot, session)
    except Exception as e:  # noqa: BLE001
        print(f"[해석 청구] CostLedger 재구성 실패(다음 기회 재시도): {e}")
    bv = view(sid)
    complete = True
    for cid, intent in bv.intents.items():
        if cid not in bv.charged:
            if await _execute_intent(intent):
                _append(sid, {"type": "CHARGED", "charge_id": cid})
            else:
                complete = False
    bv = view(sid)
    charged_ink = 0
    pending = bv.pending_ids()
    krw = bv.pending_krw()
    ink = cost_to_ink(krw)
    if pending and ink >= INTERPRET_CHARGE_THRESHOLD:
        payers = payers_of(session)
        cid = charge_id_for(sid, pending)
        intent = _append(sid, {"type": "CHARGE_INTENT", "charge_id": cid, "interp_ids": pending,
                               "krw": round(krw, 6), "ink": ink,
                               "payers": {u: ink for u in payers}})
        if await _execute_intent(intent):
            _append(sid, {"type": "CHARGED", "charge_id": cid})
        else:
            complete = False
        charged_ink = ink
    bv = view(sid)
    _mirror(session, bv)
    return {"charged_ink": charged_ink, "pending_krw": bv.pending_krw(), "complete": complete}


def waive_pending(session, *, reason: str = "below_threshold_at_open") -> float:
    """세션을 열 때 남은 임계 미만 누적을 면제한다(기존 규정 — 열기 시점에 누적 초기화)."""
    from .ink import cost_to_ink
    from .session_open import INTERPRET_CHARGE_THRESHOLD
    sid = session.session_id
    bv = view(sid)
    pending = bv.pending_ids()
    krw = bv.pending_krw()
    if pending and cost_to_ink(krw) < INTERPRET_CHARGE_THRESHOLD:
        _append(sid, {"type": "WAIVED", "interp_ids": pending, "krw": round(krw, 6),
                      "reason": reason})
    _mirror(session, view(sid))
    return krw
