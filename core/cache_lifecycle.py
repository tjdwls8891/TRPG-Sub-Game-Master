# core.cache_lifecycle — WP-F 캐시 생애주기 · 재무 단일 owner
#
# [분리하는 개념 — AUD-034/035, BILLING_POLICY §6, PF-11]
#   preflight estimate      예상(표시·선불액 산정용). CostEvent 아님, provider 사실 아님.
#   player prepayment       플레이어 재무 사실 — PREPAYMENT LifecycleInkTransaction.
#   provider create fact    provider 캐시가 실제로 생긴 뒤에만 — CostEvent(CACHE_CREATE/RECOVERY_CREATE).
#   provider storage fact   실제 생존 시간 기준, 생애주기당 한 번 — CostEvent(CACHE_STORAGE).
#   window settlement       오픈 창 종료 시 선불 대비 사용분 — REFUND(필요 시) 정확히 한 번.
#
# [두 층위]
#   오픈 창(window)  : 플레이어가 선불한 유지 시간 단위. 세션 열기 → 닫기/만료/운영자 종료.
#                      session.cache_created_at = 창 시작(표시·만료·remaining_ttl의 기준).
#   생애주기(lifecycle): provider 캐시 객체 하나. 창 안에서 재발급·복구로 교체될 수 있다.
#                      식별자 = provider 캐시 이름(유일) — 결정적.
#
# [불변식]
#   · session.cache_name 이 있으면 그 이름의 생애주기는 CREATED이고 FINALIZED가 아니다.
#   · 모든 close/delete/reissue/expiry/recovery-replace 경로는 _finalize_locked 하나를 지난다.
#   · provider 삭제(_provider_delete)·생성(_provider_create)은 이 모듈 안에서만 호출된다.
#   · 재무/CostEvent 는 결정적 키로 기록되어 재시도·재시작 replay 가 중복 효과를 만들지 않는다.
#   · 이 모듈은 게임 이력(turn_history)·턴 Settlement 를 소유하지 않는다. 캐시 CostEvent 는
#     턴에 claim 되지 않는다(session 범위) — 턴 청구에 섞이지 않는다.
#
# [저널] sessions/{id}/cache_lifecycle.jsonl — append-only, fsync. 이벤트:
#   WINDOW_OPENED · WINDOW_ABORTED · CREATED · WINDOW_PREPAID · FINALIZE_INTENT ·
#   REMOTE_RESULT · FINALIZED · WINDOW_SETTLE_INTENT · WINDOW_SETTLED

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

from google.genai import types

from . import cost_ledger as CL
from .constants import (CACHE_TTL_SECONDS, DEFAULT_MODEL, EXCHANGE_RATE,
                        MIN_CACHE_TOKENS)

JOURNAL_FILE = "cache_lifecycle.jsonl"

# 생성 목적
PURPOSE_OPEN = "OPEN"                       # 플레이어 세션 열기(선불 창 시작)
PURPOSE_REISSUE_AUTO = "REISSUE_AUTO"       # 묘사 중 만료/부재/출처 무효 자동 재발급
PURPOSE_REISSUE_MANUAL = "REISSUE_MANUAL"   # 운영자 !캐시 재발급
PURPOSE_RECOVERY = "RECOVERY"               # 재시작 복구 재생성

# 종료 사유
REASON_REPLACED = "REPLACED"                # 같은 창 안에서 교체(재발급/복구)
REASON_PLAYER_CLOSE = "PLAYER_CLOSE"        # 디스플레이 세션 닫기
REASON_EXPIRED = "EXPIRED"                  # 유지 시간 만료
REASON_OPERATOR_DELETE = "OPERATOR_DELETE"  # 운영자 !캐시 삭제
REASON_OPERATOR_END = "OPERATOR_END"        # 운영자 !세션종료
REASON_LEGACY = "LEGACY_DIRECT"             # process_cache_deletion 호환 래퍼
REASON_REISSUE_FAILED = "REISSUE_FAILED"    # 교체 생성 실패 → 창 종료

# 창 처분
WINDOW_CONTINUE = "CONTINUE"                # 창 유지(교체)
WINDOW_SETTLE_REFUND = "SETTLE_REFUND"      # 사용분 정산 후 남는 선불 환급
WINDOW_NO_PLAYER_EFFECT = "NO_PLAYER_EFFECT"  # 운영자 조작 — 플레이어 재무 효과 없음(현행 보존)

# provider 삭제 결과
REMOTE_DELETED = "DELETED"
REMOTE_GONE = "GONE"
REMOTE_FAILED = "FAILED"
REMOTE_SKIPPED = "SKIPPED"

REFERENCE_KIND = "CACHE_WINDOW"

_PURPOSE_ACTOR = {
    PURPOSE_OPEN: (CL.ACTOR_PLAYER, CL.HINT_PLAYER_CANDIDATE, CL.OP_CACHE_CREATE),
    PURPOSE_REISSUE_AUTO: (CL.ACTOR_SYSTEM, CL.HINT_SYSTEM, CL.OP_CACHE_CREATE),
    PURPOSE_REISSUE_MANUAL: (CL.ACTOR_OWNER, CL.HINT_OPERATOR, CL.OP_CACHE_CREATE),
    PURPOSE_RECOVERY: (CL.ACTOR_SYSTEM, CL.HINT_SYSTEM, CL.OP_CACHE_RECOVERY_CREATE),
    "LEGACY": (CL.ACTOR_UNKNOWN, CL.HINT_UNKNOWN, CL.OP_CACHE_CREATE),
}


class CacheLifecycleError(RuntimeError):
    """저널 손상·영속 실패·재무 기록 실패 — 추정하지 않고 표면화한다."""


# ══════════════════════════════════════════════════════════════
#  저널
# ══════════════════════════════════════════════════════════════

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
        raise CacheLifecycleError(f"캐시 생애주기 저널 기록 실패: {path}") from e
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
                raise CacheLifecycleError(f"{path}:{lineno} 저널 손상") from e
            if not isinstance(obj, dict) or not obj.get("type"):
                raise CacheLifecycleError(f"{path}:{lineno} 저널 레코드 무효")
            out.append(obj)
    return out


class JournalView:
    """저널을 접은 결과."""

    def __init__(self, events: list):
        self.windows: dict = {}
        self.lifecycles: dict = {}
        self.window_order: list = []
        for ev in events:
            t = ev["type"]
            if t == "WINDOW_OPENED":
                self.windows[ev["window_id"]] = {
                    "opened": ev, "aborted": None, "prepaid": False,
                    "settle_intent": None, "settled": False, "lifecycles": []}
                self.window_order.append(ev["window_id"])
            elif t == "WINDOW_ABORTED":
                self.windows.setdefault(ev["window_id"], {}).update(aborted=ev)
            elif t == "WINDOW_PREPAID":
                self.windows[ev["window_id"]]["prepaid"] = True
            elif t == "WINDOW_SETTLE_INTENT":
                self.windows[ev["window_id"]]["settle_intent"] = ev
            elif t == "WINDOW_SETTLED":
                self.windows[ev["window_id"]]["settled"] = True
            elif t == "CREATED":
                self.lifecycles[ev["cache_name"]] = {
                    "created": ev, "intent": None, "remote": None, "finalized": None}
                w = self.windows.get(ev.get("window_id"))
                if w is not None:
                    w["lifecycles"].append(ev["cache_name"])
            elif t == "FINALIZE_INTENT":
                self.lifecycles[ev["cache_name"]]["intent"] = ev
            elif t == "REMOTE_RESULT":
                self.lifecycles[ev["cache_name"]]["remote"] = ev
            elif t == "FINALIZED":
                self.lifecycles[ev["cache_name"]]["finalized"] = ev

    def open_window_id(self):
        """정산되지도 중단되지도 않은 가장 최근 창."""
        for wid in reversed(self.window_order):
            w = self.windows[wid]
            if not w.get("aborted") and not w.get("settled"):
                return wid
        return None

    def live(self, cache_name):
        lc = self.lifecycles.get(cache_name) if cache_name else None
        if lc is None or lc.get("finalized"):
            return None
        return lc


def view(session_id) -> JournalView:
    return JournalView(load_events(session_id))


def lifecycle_id(cache_name) -> str:
    return f"cachelc:{cache_name}"


# ══════════════════════════════════════════════════════════════
#  직렬화 락
# ══════════════════════════════════════════════════════════════

_LOCKS: dict = {}


def lifecycle_lock(session_id) -> asyncio.Lock:
    # 이벤트 루프별로 분리한다(asyncio.Lock 은 생성 루프에 묶인다).
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_key = 0
    key = (loop_key, str(session_id))
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


# ══════════════════════════════════════════════════════════════
#  provider 저수준 프리미티브 — 이 모듈 밖에서 caches.create/delete 호출 금지
# ══════════════════════════════════════════════════════════════

def _is_gone(exc) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 404:
        return True
    name = type(exc).__name__
    return "NotFound" in name or "not found" in str(exc).lower()


async def _provider_create(bot, *, caching_text: str, ttl_seconds: int):
    return await asyncio.to_thread(
        bot.genai_client.caches.create,
        model=DEFAULT_MODEL,
        config=types.CreateCachedContentConfig(
            system_instruction=bot.system_instruction,
            contents=[types.Content(role="user",
                                    parts=[types.Part.from_text(text=caching_text)])],
            ttl=f"{int(ttl_seconds)}s",
        ),
    )


async def _provider_delete(bot, name: str) -> str:
    try:
        await asyncio.to_thread(bot.genai_client.caches.delete, name=name)
        return REMOTE_DELETED
    except Exception as e:  # noqa: BLE001
        if _is_gone(e):
            return REMOTE_GONE
        print(f"[캐시 생애주기] provider 삭제 실패({name}): {type(e).__name__}: {e}")
        return REMOTE_FAILED


# ══════════════════════════════════════════════════════════════
#  CostEvent — 결정적 키, strict
# ══════════════════════════════════════════════════════════════

def _record_cost(bot, session, *, key, operation, model, actor_kind, billing_hint,
                 cost_usd, usage_source, input_tokens=0, metadata=None):
    """provider 사실 하나를 strict 로 기록. ledger 없으면 None(관측 불가 환경).

    Returns: created(bool) | None. 실패는 CacheLifecycleError.
    """
    ledger = CL.get_ledger(bot)
    if ledger is None:
        return None
    md = dict(metadata or {})
    md["pricing_basis"] = CL.pricing_basis_for(model)
    ev = CL.CostEvent(
        event_id=uuid.uuid4().hex, idempotency_key=key, created_at=time.time(),
        provider=CL.PROVIDER_GOOGLE_GENAI, operation=operation, model=model,
        session_id=getattr(session, "session_id", None), transaction_id=None,
        logical_turn=None, turn_attempt=None, provider_attempt=1,
        actor_user_id=None, actor_kind=actor_kind, billing_hint=billing_hint,
        input_tokens=int(input_tokens or 0),
        cost_usd=float(cost_usd), cost_krw=float(cost_usd) * EXCHANGE_RATE,
        usage_source=usage_source, success=True, metadata=md)
    try:
        return ledger.record_cost_event_strict(ev).created
    except Exception as e:  # noqa: BLE001
        raise CacheLifecycleError(f"캐시 CostEvent 기록 실패({key}): {type(e).__name__}: {e}") from e


def create_cost_key(session_id, cache_name) -> str:
    return f"cache:{session_id}:{cache_name}:create"


def storage_cost_key(session_id, cache_name) -> str:
    return f"cache:{session_id}:{cache_name}:storage"


def _ensure_create_cost(bot, session, created: dict):
    if created.get("legacy"):
        return None             # 레거시 채택분 — 생성 비용은 이미 레거시 회계로 발생·기록됨
    from .cost import cache_create_cost_usd
    actor, hint, op = _PURPOSE_ACTOR.get(created.get("purpose"), _PURPOSE_ACTOR["LEGACY"])
    usd = cache_create_cost_usd(created["model"], tokens=created["tokens"])
    return _record_cost(
        bot, session, key=create_cost_key(session.session_id, created["cache_name"]),
        operation=op, model=created["model"], actor_kind=actor, billing_hint=hint,
        cost_usd=usd, usage_source=CL.SOURCE_PROVIDER_METADATA,
        input_tokens=created["tokens"],
        metadata={"lifecycle_id": lifecycle_id(created["cache_name"]),
                  "cache_name": created["cache_name"], "window_id": created.get("window_id"),
                  "purpose": created.get("purpose"), "token_source": created.get("token_source")})


# ══════════════════════════════════════════════════════════════
#  예상(estimate) — provider 사실 아님
# ══════════════════════════════════════════════════════════════

def window_used_krw(model, tokens, elapsed_seconds, planned_seconds) -> float:
    """창 사용분의 정산 기준액(기존 세션 닫기 규약 보존: 업로드 + 경과 유지, 계획 시간 상한)."""
    from .cost import calculate_upload_cost
    secs = max(0.0, min(float(elapsed_seconds), float(planned_seconds)))
    return calculate_upload_cost(model or DEFAULT_MODEL, input_tokens=int(tokens or 0),
                                 store_hours=secs / 3600.0)


def _window_amounts(opened: dict, closed_at: float) -> dict:
    from .ink import cost_to_ink
    planned = float(opened.get("ttl_seconds") or CACHE_TTL_SECONDS)
    used_krw = window_used_krw(opened.get("model"), opened.get("tokens"),
                               closed_at - float(opened.get("opened_at") or closed_at), planned)
    used_ink = cost_to_ink(used_krw)
    cache_ink = int(opened.get("cache_ink") or 0)
    return {"used_krw": round(used_krw, 4), "used_ink": used_ink,
            "refund_per_user": max(0, cache_ink - used_ink),
            "shortfall_per_user": max(0, used_ink - cache_ink)}


def preview_close(session) -> dict:
    """세션 닫기 확인 화면용 예상 — 실제 정산은 종료 시점에 한 번 확정된다."""
    try:
        jv = view(session.session_id)
    except CacheLifecycleError:
        jv = JournalView([])
    wid = jv.open_window_id()
    opened = jv.windows[wid]["opened"] if wid else _legacy_window_payload(session)
    now = time.time()
    amounts = _window_amounts(opened, now)
    return {"used_hours": max(0.0, now - float(opened.get("opened_at") or now)) / 3600.0,
            "prepaid_ink": int(getattr(session, "open_prepaid_ink", 0) or 0),
            "refund_ink": amounts["refund_per_user"]}


# ══════════════════════════════════════════════════════════════
#  레거시 채택 — WP-F 이전에 열린 캐시(저널 없음)
# ══════════════════════════════════════════════════════════════

def _legacy_window_payload(session) -> dict:
    minutes = int(getattr(session, "open_minutes", 0) or 0)
    prepaid = int(getattr(session, "open_prepaid_ink", 0) or 0)
    uids = [str(u) for u in (getattr(session, "players", None) or {})]
    created = float(getattr(session, "cache_created_at", 0.0) or 0.0)
    return {"window_id": (f"legacy:{session.session_id}:{int(created)}" if created
                          else f"legacy:{session.session_id}:{uuid.uuid4().hex}"),
            "opened_at": created or time.time(),
            "planned_minutes": minutes,
            "ttl_seconds": minutes * 60 if minutes else CACHE_TTL_SECONDS,
            "model": getattr(session, "cache_model", None) or DEFAULT_MODEL,
            "tokens": int(getattr(session, "cache_tokens", 0) or 0) or MIN_CACHE_TOKENS,
            "cache_ink": prepaid, "interpret_ink": 0,
            # 레거시 선불은 마커 없는 tolerant 차감 — 기존 닫기 규약대로 현재 참가자 기준.
            "prepay": {u: prepaid for u in uids} if prepaid else {},
            "legacy": True}


def _adopt_legacy_locked(session) -> JournalView:
    sid = session.session_id
    name = session.cache_name
    jv = view(sid)
    if name in jv.lifecycles:
        return jv
    wid = jv.open_window_id()
    if wid is None:
        payload = _legacy_window_payload(session)
        wid = payload["window_id"]
        _append(sid, dict(payload, type="WINDOW_OPENED"))
        _append(sid, {"type": "WINDOW_PREPAID", "window_id": wid, "legacy": True})
    minutes = int(getattr(session, "open_minutes", 0) or 0)
    ttl = minutes * 60 if minutes else CACHE_TTL_SECONDS
    created_at = float(getattr(session, "cache_created_at", 0.0) or time.time())
    _append(sid, {"type": "CREATED", "cache_name": name, "window_id": wid,
                  "purpose": "LEGACY", "legacy": True,
                  "model": getattr(session, "cache_model", None) or DEFAULT_MODEL,
                  "tokens": int(getattr(session, "cache_tokens", 0) or 0) or MIN_CACHE_TOKENS,
                  "token_source": "legacy_session", "created_at": created_at,
                  "ttl_seconds": ttl, "expire_at": created_at + ttl})
    return view(sid)


# ══════════════════════════════════════════════════════════════
#  생성
# ══════════════════════════════════════════════════════════════

async def _build_text(bot, session, cache_note: str, log_session_id: bool):
    from .cache import build_scenario_cache_text
    return await build_scenario_cache_text(
        bot, DEFAULT_MODEL, session.scenario_data, cache_note or "",
        session.session_id if log_session_id else None, session=session)


async def _create_locked(bot, session, *, window_id, purpose, ttl_seconds,
                         caching_text, cache_tokens, base_text) -> dict:
    """provider 캐시를 만들고, **성공한 뒤에만** 생성 사실을 기록한다(AUD-035)."""
    from .cache import update_session_cache_state
    from .cost import accrue, cache_create_cost_usd
    from .io import write_cost_log
    sid = session.session_id
    cache = await _provider_create(bot, caching_text=caching_text, ttl_seconds=ttl_seconds)
    created_at = time.time()
    tokens = int(cache_tokens or 0)
    token_source = "count_tokens"
    um = getattr(cache, "usage_metadata", None)
    if um is not None and isinstance(getattr(um, "total_token_count", None), int):
        tokens = int(um.total_token_count)
        token_source = "cache_usage_metadata"
    ev = {"type": "CREATED", "cache_name": cache.name, "window_id": window_id,
          "purpose": purpose, "model": DEFAULT_MODEL, "tokens": tokens,
          "token_source": token_source, "created_at": created_at,
          "ttl_seconds": int(ttl_seconds), "expire_at": created_at + int(ttl_seconds)}
    try:
        _append(sid, ev)
    except CacheLifecycleError:
        # 생성 사실을 기록할 수 없으면 추적 불가 캐시를 남기지 않는다(보상 삭제).
        remote = await _provider_delete(bot, cache.name)
        if remote == REMOTE_FAILED:
            from .io import write_log
            write_log(sid, "error", f"[캐시 생애주기] 기록 실패 + 보상 삭제 실패 — 고아 캐시 {cache.name}")
        raise
    session.cache_obj = cache
    session.cache_name = cache.name
    session.cache_model = DEFAULT_MODEL
    session.cache_tokens = tokens
    session.cache_text = base_text
    session.cache_expired_notified = False
    update_session_cache_state(session)        # WP-E(E-E3) 출처 스탬프 포함
    usd = cache_create_cost_usd(DEFAULT_MODEL, tokens=tokens)
    krw = usd * EXCHANGE_RATE
    accrue(session, krw, usd)                  # 레거시 호환 누적 — 실제 생성분만
    write_cost_log(sid, f"캐시 생성 ({purpose})", tokens, 0, 0, krw, session.total_cost)
    try:
        _ensure_create_cost(bot, session, ev)
    except CacheLifecycleError as e:
        # 캐시는 존재한다 — 사실 기록은 종료(finalize) 시 같은 키로 재시도된다.
        print(f"[캐시 생애주기] 생성 CostEvent 보류(종료 시 재시도): {e}")
    return {"cache_name": cache.name, "tokens": tokens, "create_krw": krw, "create_usd": usd}


# ══════════════════════════════════════════════════════════════
#  종료(finalize) — 단일 finalizer
# ══════════════════════════════════════════════════════════════

async def _finalize_lifecycle_locked(bot, session, *, reason, delete_remote=True) -> dict:
    """현재 생애주기를 정확히 한 번 종료한다. 재호출·재시작 replay 안전.

    FINALIZE_INTENT(closed_at 고정) → provider 삭제(REMOTE_RESULT 고정) → 보관 CostEvent
    (결정적 키) → FINALIZED → 세션 메타데이터 정리.
    """
    from .cost import accrue, cache_storage_cost_usd
    from .io import write_cost_log
    sid = session.session_id
    name = getattr(session, "cache_name", None)
    out = {"cache_name": name, "storage_krw": 0.0, "remote": None, "finalized": False}
    if not name:
        return out
    jv = view(sid)
    if name not in jv.lifecycles:
        jv = _adopt_legacy_locked(session)
    lc = jv.lifecycles[name]
    created = lc["created"]
    if lc["finalized"] is None:
        intent = lc["intent"] or _append(sid, {"type": "FINALIZE_INTENT", "cache_name": name,
                                                "reason": reason, "closed_at": time.time()})
        remote_ev = lc["remote"]
        if remote_ev is None:
            remote = await _provider_delete(bot, name) if delete_remote else REMOTE_SKIPPED
            remote_ev = _append(sid, {"type": "REMOTE_RESULT", "cache_name": name,
                                      "remote": remote})
        remote = remote_ev["remote"]
        start = float(created["created_at"])
        expire_at = float(created.get("expire_at") or start + CACHE_TTL_SECONDS)
        closed_at = float(intent["closed_at"])
        # 삭제가 확인되지 않았으면 provider 는 TTL 만료까지 보관한다(예정분 — 추정 표기).
        end = expire_at if remote == REMOTE_FAILED else min(closed_at, expire_at)
        seconds = max(0.0, end - start)
        usd = cache_storage_cost_usd(created["model"], tokens=created["tokens"], seconds=seconds)
        _ensure_create_cost(bot, session, created)
        actor, hint, _op = _PURPOSE_ACTOR.get(created.get("purpose"), _PURPOSE_ACTOR["LEGACY"])
        _record_cost(
            bot, session, key=storage_cost_key(sid, name), operation=CL.OP_CACHE_STORAGE,
            model=created["model"], actor_kind=actor, billing_hint=hint, cost_usd=usd,
            usage_source=(CL.SOURCE_ESTIMATE if remote == REMOTE_FAILED
                          else CL.SOURCE_FIXED_PROVIDER_PRICING),
            input_tokens=0,
            metadata={"lifecycle_id": lifecycle_id(name), "cache_name": name,
                      "window_id": created.get("window_id"), "storage_tokens": created["tokens"],
                      "storage_seconds": round(seconds, 3), "created_at": start,
                      "end_at": end, "remote": remote, "reason": intent.get("reason")})
        krw = usd * EXCHANGE_RATE
        _append(sid, {"type": "FINALIZED", "cache_name": name, "reason": intent.get("reason"),
                      "remote": remote, "storage_seconds": round(seconds, 3),
                      "storage_usd": usd, "storage_krw": krw})
        accrue(session, krw, usd)              # 레거시 호환 누적 — FINALIZED 기록 시 한 번
        if krw > 0:
            write_cost_log(sid, f"캐시 보관 정산 ({intent.get('reason')})", 0, 0, 0, krw,
                           session.total_cost)
        out.update(storage_krw=krw, remote=remote, finalized=True)
    else:
        out.update(storage_krw=0.0, remote=lc["finalized"].get("remote"))
    if getattr(session, "cache_name", None) == name:
        session.cache_name = None
        session.cache_obj = None
        session.cache_model = None
        session.cache_tokens = 0
    return out


def _window_closed_at(jv: JournalView, wid):
    """창의 마지막 생애주기가 종료 의도를 가졌다면 그 고정 시각(재시도에도 불변)."""
    w = jv.windows.get(wid) or {}
    names = w.get("lifecycles") or []
    if not names:
        return None
    lc = jv.lifecycles.get(names[-1]) or {}
    return (lc.get("intent") or {}).get("closed_at")


async def _execute_prepayments_locked(session, wid, opened) -> bool:
    from . import ink_transactions as IT
    ok = True
    for uid, ink in (opened.get("prepay") or {}).items():
        try:
            await IT.execute_lifecycle_ink(
                reference_kind=REFERENCE_KIND, reference_id=wid, user_id=uid,
                kind=IT.KIND_PREPAYMENT, nominal_ink=int(ink), reason="cache_window_prepayment")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[캐시 생애주기] 선불 적용 실패(재시도 대기) uid={uid}: {type(e).__name__}: {e}")
    if ok:
        _append(session.session_id, {"type": "WINDOW_PREPAID", "window_id": wid})
    return ok


async def _paid_uids(wid, opened) -> list:
    from . import accounts
    from . import ink_transactions as IT
    if opened.get("legacy"):
        return list((opened.get("prepay") or {}).keys())
    out = []
    for uid in (opened.get("prepay") or {}):
        try:
            m = await accounts.get_applied_ink_marker(
                uid, IT.lifecycle_ink_tx_id(IT.KIND_PREPAYMENT, wid, uid))
        except Exception:  # noqa: BLE001
            m = None
        if m is not None:
            out.append(uid)
    return out


async def _settle_window_locked(session, wid, *, disposition, reason, closed_at=None) -> dict:
    """창을 정확히 한 번 정산한다. 환급은 선불이 실제 적용된 유저에게만."""
    from . import ink_transactions as IT
    sid = session.session_id
    jv = view(sid)
    w = jv.windows.get(wid)
    out = {"window_id": wid, "refund": {}, "settled": False}
    if w is None or w.get("settled") or w.get("aborted"):
        return out
    opened = w["opened"]
    intent = w["settle_intent"]
    if intent is None:
        if closed_at is None:
            closed_at = _window_closed_at(jv, wid)
        at = float(closed_at if closed_at is not None else time.time())
        refund, shortfall = {}, {}
        amounts = _window_amounts(opened, at)
        if disposition == WINDOW_SETTLE_REFUND:
            for uid in await _paid_uids(wid, opened):
                paid_cache = min(int(opened.get("cache_ink") or 0),
                                 int((opened.get("prepay") or {}).get(uid) or 0))
                r = min(paid_cache, amounts["refund_per_user"])
                if r > 0:
                    refund[uid] = r
                if amounts["shortfall_per_user"] > 0:
                    # 추가 청구 정책은 승인되지 않았다 — 초과분은 운영자 부담으로 기록만 한다.
                    shortfall[uid] = amounts["shortfall_per_user"]
        intent = _append(sid, {"type": "WINDOW_SETTLE_INTENT", "window_id": wid,
                               "disposition": disposition, "reason": reason, "closed_at": at,
                               "used_krw": amounts["used_krw"], "used_ink": amounts["used_ink"],
                               "refund": refund, "operator_borne_shortfall": shortfall})
    ok = True
    for uid, ink in (intent.get("refund") or {}).items():
        try:
            await IT.execute_lifecycle_ink(
                reference_kind=REFERENCE_KIND, reference_id=wid, user_id=uid,
                kind=IT.KIND_REFUND, nominal_ink=int(ink), reason=f"cache_window_{reason}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[캐시 생애주기] 환급 적용 실패(재시도 대기) uid={uid}: {type(e).__name__}: {e}")
    if ok:
        _append(sid, {"type": "WINDOW_SETTLED", "window_id": wid})
        if getattr(session, "cache_name", None) is None:
            session.cache_created_at = 0.0
            session.open_prepaid_ink = 0
    out.update(refund=dict(intent.get("refund") or {}), settled=ok,
               disposition=intent.get("disposition"))
    return out


async def _resume_locked(bot, session) -> None:
    """중단된 저널 작업을 결정적으로 이어간다(재시작·다음 조작 전)."""
    sid = session.session_id
    jv = view(sid)
    for name, lc in jv.lifecycles.items():
        if lc["intent"] is not None and lc["finalized"] is None:
            saved = session.cache_name
            session.cache_name = name
            await _finalize_lifecycle_locked(bot, session,
                                             reason=lc["intent"].get("reason") or REASON_LEGACY)
            if saved != name:
                session.cache_name = saved
    jv = view(sid)
    for wid in jv.window_order:
        w = jv.windows[wid]
        if w.get("aborted") or w.get("settled"):
            continue
        if not w["lifecycles"] and w["settle_intent"] is None:
            # 생성 기록 전에 끊긴 창(락 밖에서는 존재할 수 없다) — provider 캐시·선불 없음.
            _append(sid, {"type": "WINDOW_ABORTED", "window_id": wid,
                          "reason": "create_not_recorded"})
            continue
        if w["lifecycles"] and not w["prepaid"] and w["settle_intent"] is None:
            await _execute_prepayments_locked(session, wid, w["opened"])
        if w["settle_intent"] is not None:
            await _settle_window_locked(session, wid, disposition=w["settle_intent"]["disposition"],
                                        reason=w["settle_intent"].get("reason"))


async def _save(bot, session):
    from .io import save_session_data
    await save_session_data(bot, session)


# ══════════════════════════════════════════════════════════════
#  공개 진입점
# ══════════════════════════════════════════════════════════════

async def open_window(bot, session, *, say=None) -> dict:
    """세션 열기 — 선불 창 시작 + provider 캐시 생성.

    예상액은 선불액 산정에만 쓰인다(provider 사실 아님). 생성이 성공해야 생성 사실과
    선불(PREPAYMENT)이 기록된다. 실패하면 창은 ABORTED이고 재무 효과가 없다.
    """
    from .cost import calculate_upload_cost
    from .session_open import should_charge_interpretation
    from .ink import cost_to_ink
    sid = session.session_id
    async with lifecycle_lock(sid):
        if getattr(session, "cache_name", None):
            return {"ok": True, "already": True}
        await _resume_locked(bot, session)
        dangling = view(sid).open_window_id()
        if dangling:
            await _settle_window_locked(session, dangling, disposition=WINDOW_SETTLE_REFUND,
                                        reason="superseded_by_open")
        minutes = int(getattr(session, "open_minutes", 0) or 0)
        ttl = minutes * 60 if minutes else CACHE_TTL_SECONDS
        caching_text, tokens, base_text = await _build_text(bot, session, "", False)
        est_krw = calculate_upload_cost(DEFAULT_MODEL, input_tokens=tokens,
                                        store_hours=ttl / 3600)
        cache_ink = cost_to_ink(est_krw)
        interpret_charge, interpret_ink = should_charge_interpretation(session)
        per_user = cache_ink + (interpret_ink if interpret_charge else 0)
        payers = [str(u) for u in (session.players or {})] or \
            [str(u) for u in [getattr(session, "creator_uid", "")] if u]
        wid = f"win-{uuid.uuid4().hex}"
        now = time.time()
        opened = {"type": "WINDOW_OPENED", "window_id": wid, "opened_at": now,
                  "planned_minutes": minutes, "ttl_seconds": ttl, "model": DEFAULT_MODEL,
                  "tokens": int(tokens or 0), "estimate_krw": round(est_krw, 4),
                  "cache_ink": cache_ink,
                  "interpret_ink": interpret_ink if interpret_charge else 0,
                  "prepay": {u: per_user for u in payers}}
        _append(sid, opened)
        try:
            res = await _create_locked(bot, session, window_id=wid, purpose=PURPOSE_OPEN,
                                       ttl_seconds=ttl, caching_text=caching_text,
                                       cache_tokens=tokens, base_text=base_text)
        except Exception as e:
            _append(sid, {"type": "WINDOW_ABORTED", "window_id": wid,
                          "reason": f"create_failed:{type(e).__name__}"})
            raise
        session.cache_created_at = now
        session.interpret_cost_krw = 0.0
        session.open_prepaid_ink = per_user
        prepaid = await _execute_prepayments_locked(session, wid, opened)
        await _save(bot, session)
        return {"ok": True, "window_id": wid, "ttl_seconds": ttl, "charge_ink": per_user,
                "interpret_ink": opened["interpret_ink"], "prepaid": prepaid,
                "create_krw": res["create_krw"]}


async def reissue(bot, session, *, purpose, cache_note: str = "",
                  log_session_id: bool = True) -> dict:
    """같은 창 안에서 provider 캐시를 교체한다(기존 생애주기 종료 → 새 생애주기 생성).

    창 시작 시각은 유지된다 — 재발급이 결제한 유지 시간을 연장하지 않는다.
    새 생성이 실패하면 세션은 사실상 닫힌 것이므로 창을 정산(미사용 선불 환급)한다.
    """
    from .cache import remaining_ttl
    sid = session.session_id
    async with lifecycle_lock(sid):
        await _resume_locked(bot, session)
        old = await _finalize_lifecycle_locked(bot, session, reason=REASON_REPLACED)
        jv = view(sid)
        wid = jv.open_window_id()
        if wid is None:
            _legacy = _legacy_window_payload(session)
            wid = _legacy["window_id"]
            _append(sid, dict(_legacy, type="WINDOW_OPENED", prepay={}))
            _append(sid, {"type": "WINDOW_PREPAID", "window_id": wid, "legacy": True})
            if not getattr(session, "cache_created_at", 0.0):
                session.cache_created_at = time.time()
        try:
            caching_text, tokens, base_text = await _build_text(bot, session, cache_note,
                                                                log_session_id)
            res = await _create_locked(bot, session, window_id=wid, purpose=purpose,
                                       ttl_seconds=remaining_ttl(session),
                                       caching_text=caching_text, cache_tokens=tokens,
                                       base_text=base_text)
        except Exception:
            await _settle_window_locked(session, wid, disposition=WINDOW_SETTLE_REFUND,
                                        reason=REASON_REISSUE_FAILED)
            await _save(bot, session)
            raise
        await _save(bot, session)
        return {"ok": True, "old": old, "storage_krw": old["storage_krw"],
                "create_krw": res["create_krw"], "cache_name": res["cache_name"],
                "tokens": res["tokens"]}


async def close_window(bot, session, *, reason, disposition, delete_remote=True) -> dict:
    """창 종료 — 현재 생애주기 종료 + 창 정산(처분에 따라)."""
    sid = session.session_id
    async with lifecycle_lock(sid):
        await _resume_locked(bot, session)
        had_cache = bool(getattr(session, "cache_name", None))
        fin = await _finalize_lifecycle_locked(bot, session, reason=reason,
                                               delete_remote=delete_remote)
        wid = view(sid).open_window_id()
        settle = {"refund": {}, "settled": False}
        if wid is not None:
            settle = await _settle_window_locked(session, wid, disposition=disposition,
                                                 reason=reason,
                                                 closed_at=_window_closed_at(view(sid), wid))
        elif had_cache or fin.get("finalized"):
            session.cache_created_at = 0.0
            session.open_prepaid_ink = 0
        await _save(bot, session)
        return {"ok": True, "storage_krw": fin["storage_krw"], "remote": fin["remote"],
                "refund": settle.get("refund") or {}, "settled": settle.get("settled", False)}


async def finalize_legacy(bot, session) -> float:
    """io.process_cache_deletion 호환 — 생애주기 종료 + 창 종료(플레이어 재무 효과 없음)."""
    res = await close_window(bot, session, reason=REASON_LEGACY,
                             disposition=WINDOW_NO_PLAYER_EFFECT, delete_remote=False)
    return float(res["storage_krw"])


async def restore(bot, session) -> dict:
    """재시작 복구 — 저널 작업 재개 후 현재 캐시를 연동·교체·만료 처리한다."""
    from google.genai.errors import APIError
    from .cache import is_cache_expired, remaining_ttl
    sid = session.session_id
    if not getattr(session, "cache_name", None) and not os.path.exists(journal_path(sid)):
        return {"action": "NONE"}
    async with lifecycle_lock(sid):
        await _resume_locked(bot, session)
        jv = view(sid)
        name = getattr(session, "cache_name", None)
        if name and name in jv.lifecycles and jv.lifecycles[name]["finalized"]:
            session.cache_name = None          # 크래시로 남은 종료된 캐시 참조
            name = None
        wid = jv.open_window_id()
        if name and is_cache_expired(session):
            await _finalize_lifecycle_locked(bot, session, reason=REASON_EXPIRED)
            if view(sid).open_window_id():
                await _settle_window_locked(session, view(sid).open_window_id(),
                                            disposition=WINDOW_SETTLE_REFUND,
                                            reason=REASON_EXPIRED)
            session.cache_expired_notified = True
            await _save(bot, session)
            return {"action": "EXPIRED"}
        if name:
            try:
                session.cache_obj = await asyncio.to_thread(bot.genai_client.caches.get,
                                                            name=name)
                return {"action": "LINKED"}
            except APIError:
                pass
            await _finalize_lifecycle_locked(bot, session, reason=REASON_REPLACED,
                                             delete_remote=False)
            wid = view(sid).open_window_id()
        if wid is None:
            await _save(bot, session)
            return {"action": "NONE"}
        opened = view(sid).windows[wid]["opened"]
        if time.time() - float(opened.get("opened_at") or 0) >= float(
                opened.get("ttl_seconds") or CACHE_TTL_SECONDS):
            await _settle_window_locked(session, wid, disposition=WINDOW_SETTLE_REFUND,
                                        reason=REASON_EXPIRED)
            session.cache_expired_notified = True
            await _save(bot, session)
            return {"action": "EXPIRED"}
        if not getattr(session, "cache_created_at", 0.0):
            session.cache_created_at = float(opened.get("opened_at") or time.time())
        try:
            caching_text, tokens, base_text = await _build_text(bot, session, "", False)
            await _create_locked(bot, session, window_id=wid, purpose=PURPOSE_RECOVERY,
                                 ttl_seconds=remaining_ttl(session), caching_text=caching_text,
                                 cache_tokens=tokens, base_text=base_text)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ {sid}: 복구 캐시 생성 실패 — 창 정산: {e}")
            await _settle_window_locked(session, wid, disposition=WINDOW_SETTLE_REFUND,
                                        reason=REASON_REISSUE_FAILED)
            await _save(bot, session)
            return {"action": "RECOVERY_FAILED"}
        await _save(bot, session)
        return {"action": "RECREATED"}
