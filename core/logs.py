# core.logs — 구조 로그 단일 owner(허점 발견용)
#
# [세션] sessions/{id}/logs/
#   calls.jsonl   provider 호출 1건 = 1줄(메타데이터 + 요청·응답 조각 참조)
#   blobs.jsonl   내용 조각 저장소 {h, t} — 같은 조각은 세션당 한 번만(델타)
#   events.jsonl  내부 처리·경고·오류(ERROR는 스택 추적)
#   domain.jsonl  정본·재무·표시 사실 — 턴 상태 델타·커밋·비용·잉크·디스플레이·이력 조작·트랜잭션 상태
#   chat.jsonl    게임/마스터 채널 기록
# [전역] logs/bot.jsonl — 세션 밖 이벤트와 모든 print(회전 10MB × 5)
#
# 원칙: 로그 실패는 게임을 막지 않는다(전부 흡수). 정본·복구 권위가 아니다(읽기 전용 관찰).
# 조각 = 줄 단위. 이어 붙이면 원문과 같다. 짧은 조각(INLINE_MAX 미만)은 원문 그대로 둔다.
# 독자 = 오너 DM(!세션이력 zip)을 받는 AI 개발 에이전트(P3). 필드·kind 사전은 docs/LOG_SCHEMA.md(단일 출처).
# 순서: 모든 세션 줄은 b(프로세스 부팅 id) + seq(부팅 안 단조 증가) — 파일이 달라도 (b, seq)로 전순서.
# 조인: tx·turn·attempt(턴), call(호출 1건), op·op_id(비용 오퍼레이션 — cost.event의 operation_id와 같다).

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import io
import itertools
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback

INLINE_MAX = 16           # 이보다 짧은 조각은 해시 대신 원문(참조 비용이 더 큼)
VALUE_BLOB_MIN = 500      # 델타 값 문자열이 이보다 길면 조각 저장소로
LOG_SCHEMA_VERSION = 2    # P3: b·seq·op_id·session.meta·turn.summary·history.selection
# domain.jsonl kind 등록부(P3 G9) — 필드 사전은 docs/LOG_SCHEMA.md. 새 kind는 여기와 문서에 함께 등록한다(테스트가 소스 스캔으로 고정).
DOMAIN_KINDS = {
    "session.meta": "세션 생성·복구·genesis·캐시 발급 시 코드·정의·모드 헤더",
    "tx.begin": "자동 논리 턴 시도 시작",
    "tx.status": "트랜잭션 상태 전이",
    "turn.summary": "시도 1건의 대기·종결 요약(판단·세계·준비·호출 참조)",
    "commit": "정본 커밋(청구·세계 typed op)",
    "turn.delta": "커밋 이력 레코드의 pre→post 가역 상태 차이",
    "history.selection": "선택 인덱스 변화(SELECT·REWIND)",
    "history.rewind": "되감기 결과",
    "history.rerender_begin": "재서술 시작",
    "history.rerender_abort": "재서술 중단",
    "history.rerender_settle": "재서술 종결",
    "cost.event": "CostLedger 사실 1건",
    "cost.usage": "호환 미러 사용량·비용",
    "cost.estimate": "턴 예상 비용",
    "cost.unknown": "사용량 미상 provider 결과",
    "ink.charge": "턴 Settlement 잉크 청구",
    "ink.lifecycle": "캐시 생애주기·해석 잉크",
    "display": "바뀐 디스플레이 상태판",
    "card.speaker": "화자 표시 컨텍스트",
    "card.render": "SVG 화자 카드 렌더",
}
LOG_DIR = "logs"
GLOBAL_FILE = os.path.join("logs", "bot.jsonl")

_lock = threading.RLock()
_seen: dict = {}          # sid → 저장된 조각 해시 집합(첫 사용 때 blobs.jsonl에서 적재)
_turns: dict = {}         # sid → (transaction_id, logical_turn)
_display_last: dict = {}  # sid → 마지막 기록한 디스플레이 지문
_CTX = contextvars.ContextVar("indaim_log_ctx", default=None)
BOOT_ID = hashlib.sha1(f"{time.time()}{os.getpid()}".encode()).hexdigest()[:6]   # 이 프로세스(부팅) 식별
_SEQ = itertools.count(1)
_tx_calls: dict = {}      # tx → [(layer, call id)] — turn.summary의 호출 참조(프로세스 메모리)
_global = logging.getLogger("indaim")
_global.propagate = False


# ══════════════════════════════════════════════════════════════
#  경로 · 쓰기
# ══════════════════════════════════════════════════════════════

def session_dir(sid) -> str:
    return os.path.join("sessions", str(sid), LOG_DIR)


def _append(sid, name: str, obj: dict) -> None:
    if not sid:
        return
    from .io import is_session_removed
    if is_session_removed(sid):
        return                       # 세션 관리: 삭제된 세션 폴더를 로그로 되살리지 않는다
    try:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with _lock:
            d = session_dir(sid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, name), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:  # noqa: BLE001
        _stderr(f"[로그] 기록 실패 {sid}/{name}: {e}")


def _stderr(text: str) -> None:
    try:
        sys.__stderr__.write(text + "\n")
    except Exception:  # noqa: BLE001
        pass


def _base(sid, **extra) -> dict:
    out = {"ts": round(time.time(), 3), "b": BOOT_ID, "seq": next(_SEQ)}
    tx = _turns.get(str(sid)) if sid else None
    if tx:
        out["tx"], out["turn"] = tx
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# ══════════════════════════════════════════════════════════════
#  조각 저장소(델타)
# ══════════════════════════════════════════════════════════════

def _seen_for(sid) -> set:
    key = str(sid)
    s = _seen.get(key)
    if s is None:
        s = set()
        path = os.path.join(session_dir(sid), "blobs.jsonl")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            s.add(json.loads(line)["h"])
                        except Exception:  # noqa: BLE001
                            continue
            except Exception:  # noqa: BLE001
                pass
        _seen[key] = s
    return s


def blob(sid, text) -> str:
    """조각 하나 — 짧으면 원문, 길면 '#해시'(처음 보면 blobs.jsonl에 저장)."""
    text = "" if text is None else str(text)
    if len(text) < INLINE_MAX or not sid:
        return text
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    with _lock:
        seen = _seen_for(sid)
        if h not in seen:
            _append(sid, "blobs.jsonl", {"h": h, "t": text})
            seen.add(h)
    return "#" + h


SEGMENT_SEP = "\n"


def segments(sid, text) -> list:
    """긴 텍스트 → 줄 조각 참조 목록(SEGMENT_SEP.join(복원) == 원문). 매 턴 반복되는 줄은 참조만 남는다."""
    return [blob(sid, p) for p in str(text or "").split(SEGMENT_SEP)]


def _value(sid, v):
    if isinstance(v, str):
        return {"$b": blob(sid, v)} if len(v) >= VALUE_BLOB_MIN else v
    if isinstance(v, dict):
        return {str(k): _value(sid, x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_value(sid, x) for x in v]
    return v


# ══════════════════════════════════════════════════════════════
#  맥락 · 턴 상관
# ══════════════════════════════════════════════════════════════

@contextlib.contextmanager
def call_context(session_id=None, layer=None, **extra):
    """provider 호출 맥락(층위·세션) — 이 안에서 만든 태스크·스레드로 전파된다."""
    cur = dict(_CTX.get() or {})
    cur.update({k: v for k, v in {"sid": session_id, "layer": layer, **extra}.items() if v is not None})
    token = _CTX.set(cur)
    try:
        yield
    finally:
        _CTX.reset(token)


def set_turn(sid, transaction_id, logical_turn=None) -> None:
    if sid:
        if transaction_id:
            _turns[str(sid)] = (transaction_id, logical_turn)
        else:
            _turns.pop(str(sid), None)


# ══════════════════════════════════════════════════════════════
#  이벤트 · 도메인 사실 · 채팅
# ══════════════════════════════════════════════════════════════

LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def _sid_of(session=None, sid=None):
    return sid if sid else (getattr(session, "session_id", None) if session is not None else None)


def event(area: str, msg: str, *, level: str = "INFO", session=None, sid=None, data=None, exc=None) -> None:
    """내부 처리 이벤트. 세션이 있으면 세션 events.jsonl, 항상 전역 bot.jsonl에도."""
    try:
        s = _sid_of(session, sid)
        rec = _base(s, lv=level, area=area, msg=str(msg))
        if data is not None:
            rec["data"] = _value(s, data) if s else data
        if exc is not None:
            rec["exc"] = f"{type(exc).__name__}: {exc}"
            if level == "ERROR":
                rec["trace"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
        if s:
            _append(s, "events.jsonl", rec)
            # D1: 전역에는 세션 이벤트의 위치만(내용·데이터·추적은 세션 파일에 — 세션 삭제와 함께 사라진다)
            _global_line({k: v for k, v in rec.items() if k not in ("data", "trace")} | {"sid": s})
        else:
            _global_line(rec)
    except Exception as e:  # noqa: BLE001
        _stderr(f"[로그] 이벤트 실패: {e}")


def error(area: str, msg: str, exc=None, **kw) -> None:
    event(area, msg, level="ERROR", exc=exc, **kw)


def warn(area: str, msg: str, **kw) -> None:
    event(area, msg, level="WARN", **kw)


def domain(kind: str, data: dict, *, session=None, sid=None) -> None:
    """정본·재무·표시 사실 1건(domain.jsonl)."""
    try:
        s = _sid_of(session, sid)
        if not s:
            _global_line({"ts": round(time.time(), 3), "lv": "INFO", "area": "domain", "kind": kind,
                          "data": data})
            return
        rec = _base(s, kind=kind)
        rec["data"] = _value(s, data)
        _append(s, "domain.jsonl", rec)
    except Exception as e:  # noqa: BLE001
        _stderr(f"[로그] 도메인 기록 실패: {e}")


def chat(sid, channel: str, speaker: str, text: str) -> None:
    rec = _base(sid, ch=channel, who=speaker)
    rec["text"] = segments(sid, text) if len(str(text or "")) >= VALUE_BLOB_MIN else str(text or "")
    _append(sid, "chat.jsonl", rec)


# ══════════════════════════════════════════════════════════════
#  코드·세션 메타(P3 G2) — 에이전트가 '어떤 코드·시나리오·모드로 돌았나'를 한 줄로 안다
# ══════════════════════════════════════════════════════════════

_code_rev = None


def _git_sha(root: str):
    try:
        head = open(os.path.join(root, ".git", "HEAD"), encoding="utf-8").read().strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        p = os.path.join(root, ".git", *ref.split("/"))
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
        packed = os.path.join(root, ".git", "packed-refs")
        for line in open(packed, encoding="utf-8"):
            if line.strip().endswith(" " + ref):
                return line.split()[0]
    except Exception:  # noqa: BLE001
        return None
    return None


def code_revision() -> dict:
    """코드 버전·git SHA·프롬프트 지문(prompts.py 문자열 상수 전체) — 프로세스당 1회 계산."""
    global _code_rev
    if _code_rev is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = {"boot": BOOT_ID, "git_sha": _git_sha(root), "log_schema": LOG_SCHEMA_VERSION}
        try:
            from .constants import __version__
            out["version"] = __version__
        except Exception:  # noqa: BLE001
            pass
        try:
            import prompts as _p
            h = hashlib.sha1()
            for name in sorted(n for n in dir(_p) if n.isupper()):
                v = getattr(_p, name)
                if isinstance(v, (str, dict)):
                    h.update(name.encode() + json.dumps(v, ensure_ascii=False, sort_keys=True, default=str).encode())
            out["prompts_digest"] = h.hexdigest()[:12]
        except Exception:  # noqa: BLE001
            pass
        _code_rev = out
    return dict(_code_rev)


def session_meta(session, reason: str) -> None:
    """session.meta 1줄 — 세션 생성·복구·genesis·캐시 발급 때. 실패는 흡수."""
    try:
        sid = getattr(session, "session_id", None)
        data = {"reason": reason, **code_revision(),
                "scenario_id": getattr(session, "scenario_id", None),
                "definition_revision": getattr(session, "definition_revision", None),
                "world_lifecycle": getattr(session, "world_lifecycle", None),
                "players": len(getattr(session, "players", None) or {}),
                "turn_count": getattr(session, "turn_count", None),
                "gm_turns_done": getattr(session, "gm_turns_done", None),
                "cache_name": getattr(session, "cache_name", None),
                "cache_model": getattr(session, "cache_model", None)}
        # 모드(DYNAMIC/LEGACY)는 판정 단일 gate(player_world) 정책상 여기서 판정하지 않는다 — 턴별 모드는
        # turn.summary.world 유무(DYNAMIC 턴만 있음), 판정 재료는 위 definition_revision·world_lifecycle·players.
        try:
            from .constants import DEFAULT_MODEL
            data["default_model"] = DEFAULT_MODEL
        except Exception:  # noqa: BLE001
            pass
        domain("session.meta", data, sid=sid)
    except Exception as e:  # noqa: BLE001
        _stderr(f"[로그] 세션 메타 실패: {e}")


# ══════════════════════════════════════════════════════════════
#  상태 델타(되감기 레코드 pre → post)
# ══════════════════════════════════════════════════════════════

MAX_DIFF_DEPTH = 6


def state_diff(a, b, depth: int = 0):
    """JSON 값 차이. dict는 키별 재귀, 리스트는 앞부분이 같으면 추가분만, 그 밖은 교체.

    반환: None(같음) | {"set": 값} | {"del": True} | {"append": [...]} | {키: 하위 차이}
    """
    if a == b:
        return None
    if isinstance(a, dict) and isinstance(b, dict) and depth < MAX_DIFF_DEPTH:
        out = {}
        for k in sorted(set(a) | set(b), key=str):
            if k not in b:
                out[k] = {"del": True}
            elif k not in a:
                out[k] = {"set": b[k]}
            else:
                d = state_diff(a[k], b[k], depth + 1)
                if d is not None:
                    out[k] = d
        return out or None
    if isinstance(a, list) and isinstance(b, list) and len(b) > len(a) and b[:len(a)] == a:
        return {"append": b[len(a):]}
    return {"set": b}


# ══════════════════════════════════════════════════════════════
#  provider 호출 기록 프록시
# ══════════════════════════════════════════════════════════════

def _usage(resp) -> dict:
    """provider 원본 usage(용어: core.cost_ledger — out = 가시 출력, thought = 사고, 과금 출력 = out + thought)."""
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return {}
    out = {}
    for k, short in (("prompt_token_count", "in"), ("cached_content_token_count", "cached"),
                     ("candidates_token_count", "out"), ("thoughts_token_count", "thought"),
                     ("total_token_count", "total")):
        v = getattr(um, k, None)
        if v:
            out[short] = int(v)
    return out


def _resp_text(resp) -> str:
    try:
        t = getattr(resp, "text", None)
        if isinstance(t, str):
            return t
    except Exception:  # noqa: BLE001
        pass
    try:
        return "".join(getattr(p, "text", "") or "" for p in resp.candidates[0].content.parts)
    except Exception:  # noqa: BLE001
        return ""


def _finish(resp):
    try:
        fr = resp.candidates[0].finish_reason
        return getattr(fr, "name", None) or str(fr)
    except Exception:  # noqa: BLE001
        return None


def _contents(sid, contents) -> list:
    if isinstance(contents, str):
        return [{"role": "user", "parts": segments(sid, contents)}]
    out = []
    for c in contents or ():
        role = getattr(c, "role", None) or (c.get("role") if isinstance(c, dict) else None)
        parts = getattr(c, "parts", None) or (c.get("parts") if isinstance(c, dict) else None) or ()
        texts = []
        for p in parts:
            t = getattr(p, "text", None) if not isinstance(p, dict) else p.get("text")
            texts.append(segments(sid, t) if t is not None else ["<non-text>"])
        out.append({"role": role, "parts": texts})
    return out


def record_call(kwargs: dict, resp=None, exc=None, latency_ms: int = 0) -> None:
    try:
        ctx = dict(_CTX.get() or {})
        sid = ctx.get("sid") or None
        cfg = kwargs.get("config")
        rec = _base(sid, call=hashlib.sha1(f"{time.time()}{id(kwargs)}".encode()).hexdigest()[:12],
                    layer=ctx.get("layer") or "unlabeled", op=ctx.get("op"), op_id=ctx.get("op_id"),
                    model=kwargs.get("model"), attempt=ctx.get("attempt"), ms=latency_ms)
        if rec.get("tx"):
            with _lock:
                _tx_calls.setdefault(rec["tx"], []).append([rec["layer"], rec["call"]])
        cached = getattr(cfg, "cached_content", None) if cfg is not None else None
        if cached:
            rec["cache"] = cached
        if sid:
            si = getattr(cfg, "system_instruction", None) if cfg is not None else None
            if si:
                rec["system"] = blob(sid, si if isinstance(si, str) else str(si))
            schema = getattr(cfg, "response_schema", None) if cfg is not None else None
            if schema is not None:
                rec["schema"] = blob(sid, json.dumps(schema, ensure_ascii=False, sort_keys=True, default=str))
            think = getattr(getattr(cfg, "thinking_config", None), "thinking_budget", None) if cfg is not None else None
            if think is not None:
                rec["think_budget"] = think
            rec["req"] = _contents(sid, kwargs.get("contents"))
        if exc is not None:
            rec["status"] = "error"
            rec["exc"] = f"{type(exc).__name__}: {exc}"[:2000]
        else:
            rec["status"] = "ok"
            rec["usage"] = _usage(resp)
            fin = _finish(resp)
            if fin:
                rec["finish"] = fin
            if sid:
                rec["resp"] = segments(sid, _resp_text(resp))
        if sid:
            _append(sid, "calls.jsonl", rec)
        else:
            _global_line({k: v for k, v in rec.items() if k not in ("req", "resp")} | {"area": "call"})
    except Exception as e:  # noqa: BLE001
        _stderr(f"[로그] 호출 기록 실패: {e}")


def calls_of(transaction_id) -> list:
    """이 트랜잭션에서 기록한 호출 [(layer, call id)] — turn.summary 참조용(같은 프로세스 안)."""
    with _lock:
        return [list(x) for x in _tx_calls.get(transaction_id, ())]


def forget_calls(transaction_id) -> None:
    with _lock:
        _tx_calls.pop(transaction_id, None)


class LoggedModels:
    """client.models 프록시 — 동작 불변(반환·예외 그대로), 호출마다 기록."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def generate_content(self, **kwargs):
        t0 = time.monotonic()
        try:
            resp = self._inner.generate_content(**kwargs)
        except Exception as e:
            record_call(kwargs, exc=e, latency_ms=int((time.monotonic() - t0) * 1000))
            raise
        record_call(kwargs, resp=resp, latency_ms=int((time.monotonic() - t0) * 1000))
        return resp


def install_client_proxy(client):
    """genai.Client의 models를 기록 프록시로 감싼다(멱등)."""
    inner = getattr(client, "_models", None)
    if inner is not None and not isinstance(inner, LoggedModels):
        client._models = LoggedModels(inner)
    return client


# ══════════════════════════════════════════════════════════════
#  전역 로그 · print 포착
# ══════════════════════════════════════════════════════════════

def _global_line(rec: dict) -> None:
    try:
        if _global.handlers:
            _global.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        pass


_SID_RE = None


def _print_session(line: str):
    """print 줄에 실재 세션 ID(8자리 16진)가 있으면 그 ID."""
    global _SID_RE
    if _SID_RE is None:
        import re
        _SID_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])")
    for m in _SID_RE.finditer(line):
        if os.path.isdir(os.path.join("sessions", m.group(1))):
            return m.group(1)
    return None


_AREA_RE = None


def print_area(line: str) -> str:
    """print 줄 머리 '[영역/세션…]'·'[영역]'에서 영역 이름(구조 필드). 없으면 'print'."""
    global _AREA_RE
    if _AREA_RE is None:
        import re
        _AREA_RE = re.compile(r"^\s*(?:[^\w\[]{0,3}\s*)?\[([^\]/:\s][^\]/:]{0,40}?)(?:[/:][^\]]*)?\]")
    m = _AREA_RE.match(line)
    return m.group(1).strip() if m else "print"


class _Tee(io.TextIOBase):
    """stdout/stderr 줄을 원래 스트림에 쓰고 전역 로그에도 남긴다(print 포착)."""

    def __init__(self, stream, level: str):
        self._stream, self._level, self._buf = stream, level, ""

    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:  # noqa: BLE001
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                rec = {"ts": round(time.time(), 3), "lv": self._level, "area": print_area(line), "msg": line}
                sid = _print_session(line)
                if sid:                                 # 세션 ID가 찍힌 출력은 그 세션 이벤트에 원문 그대로
                    _append(sid, "events.jsonl", {**_base(sid), "src": "print",
                                                  **{k: v for k, v in rec.items() if k != "ts"}})
                    # D1: 전역에는 위치·길이만 — 서사 조각이 세션 삭제 뒤 공용 로그에 남지 않는다
                    _global_line({**rec, "msg": f"<세션 {sid} 출력 {len(line)}자 — 세션 events 참조>", "sid": sid})
                else:
                    _global_line(rec)
        return len(s)

    def flush(self):
        try:
            self._stream.flush()
        except Exception:  # noqa: BLE001
            pass


def setup_global(path: str = GLOBAL_FILE, *, capture_print: bool = True) -> None:
    """봇 시작 시 1회 — 전역 회전 로그 + print 포착."""
    if _global.handlers:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    _global.addHandler(h)
    _global.setLevel(logging.INFO)
    _global_line({"ts": round(time.time(), 3), "lv": "INFO", "area": "boot", "msg": "process start",
                  "data": code_revision()})
    if capture_print and not isinstance(sys.stdout, _Tee):
        sys.stdout = _Tee(sys.stdout, "INFO")
        sys.stderr = _Tee(sys.stderr, "WARN")
