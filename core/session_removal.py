# 세션 종료·제거 통합 관리 — 종료만 / 채널 삭제 / 게임 데이터 삭제 / 완전 제거 (`!세션관리`)
#
# [권위]
#   · 종료·정산·환급은 기존 단일 경로 world_lifecycle.close_session → cache_lifecycle.close_window만 쓴다.
#     종료가 영속적으로 끝난(CLOSED) 뒤에만 채널·데이터를 지운다. 게임 채널이 없어도 종료할 수 있다.
#   · 승인은 확인 메시지 1회. 승인되면 세션 폴더 밖 체크포인트(data/session_removal/{id}.json)에
#     범위·정리 대상 id·완료 단계를 남긴다. 재시도·재시작 재개는 같은 승인으로 이어 가며 완료 단계를 다시
#     하지 않는다. 모두 끝나면 체크포인트를 지운다(영구 삭제 이력을 따로 만들지 않는다).
#   · 채널은 세션에 영속된 id로만 지운다(이름 패턴 삭제 없음). 카테고리는 다른 채널이 남지 않을 때만.
#   · 게임 데이터 = sessions/{id}/ 하나. 지우기 전에 세션 안 재무 저널을 data/session_finance/{id}/로
#     복사하고 해시로 검증한다. 전역 CostLedger·계정·잉크 거래·공용 로그·사전 프로필은 건드리지 않는다.
#   · 삭제가 시작되면 io의 제거 표식이 늦은 저장·로그가 세션 폴더를 되살리는 것을 막는다.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid

import discord

from . import io as _io
from . import master

SCOPE_CLOSE, SCOPE_CHANNELS, SCOPE_DATA, SCOPE_FULL = "close", "channels", "data", "full"
SCOPES = {
    SCOPE_CLOSE: {"label": "종료만", "channels": False, "data": False,
                  "desc": "종료·정산만 합니다. 채널은 잠그고 남깁니다. 기록은 보존됩니다."},
    SCOPE_CHANNELS: {"label": "채널 삭제", "channels": True, "data": False,
                     "desc": "종료·정산 뒤 이 세션의 채널(게임·마스터·디스플레이·음성)과 카테고리를 지웁니다. 기록은 보존됩니다."},
    SCOPE_DATA: {"label": "게임 데이터 삭제", "channels": False, "data": True,
                 "desc": "종료·정산 뒤 세션 기록(대화·이력·세계 상태·로그)을 지웁니다. 채널은 잠그고 남깁니다."},
    SCOPE_FULL: {"label": "완전 제거", "channels": True, "data": True,
                 "desc": "종료·정산 뒤 채널과 세션 기록을 모두 지웁니다. 목록에서도 사라집니다."},
}

SESSIONS_ROOT = "sessions"
STATE_DIR = os.path.join("data", "session_removal")
ARCHIVE_DIR = os.path.join("data", "session_finance")
TRASH_DIR = os.path.join(SESSIONS_ROOT, ".removal_trash")   # 같은 파일시스템(원자적 이동). 복구 스캔은 data.json이 없어 무시
# 세션 폴더 안의 재무 증빙(턴 정산·커밋 단계·캐시 선불/환급·시간 해석 청구·추출 재시도 claim)
FINANCE_FILES = ("turn_settlements.jsonl", "turn_commit_journal.jsonl", "cache_lifecycle.jsonl",
                 "interpretation_billing.jsonl", "retry_claims.jsonl")
CHANNEL_KEYS = ("game_ch_id", "master_ch_id", "display_ch_id", "voice_ch_id")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RUN_LOCKS: dict = {}

STEP_CLOSE, STEP_STATS, STEP_LOCK, STEP_CHANNELS = "close", "stats", "lock", "channels"
STEP_ARCHIVE, STEP_MOVED, STEP_DATA = "archive", "data_moved", "data"


class RemovalError(RuntimeError):
    pass


def valid_session_id(sid) -> bool:
    return isinstance(sid, str) and bool(_SID_RE.match(sid))


# ══════════════════════════════════════════════════════════════
#  체크포인트(세션 폴더 밖, 승인 작업별)
# ══════════════════════════════════════════════════════════════

def checkpoint_path(sid) -> str:
    if not valid_session_id(sid):
        raise RemovalError(f"잘못된 세션 id {sid!r}")
    return os.path.join(STATE_DIR, f"{sid}.json")


def _write_json_atomic(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_checkpoint(sid):
    try:
        with open(checkpoint_path(sid), "r", encoding="utf-8") as f:
            cp = json.load(f)
        return cp if isinstance(cp, dict) and cp.get("session_id") == sid else None
    except (FileNotFoundError, RemovalError):
        return None


def list_checkpoints() -> list:
    out = []
    if not os.path.isdir(STATE_DIR):
        return out
    for fn in sorted(os.listdir(STATE_DIR)):
        if fn.endswith(".json") and valid_session_id(fn[:-5]):
            cp = read_checkpoint(fn[:-5])
            if cp:
                out.append(cp)
    return out


def _save_checkpoint(cp: dict) -> None:
    _write_json_atomic(checkpoint_path(cp["session_id"]), cp)


def _mark(cp: dict, step: str, **extra) -> None:
    cp.setdefault("steps", {})[step] = True
    cp.update(extra)
    _save_checkpoint(cp)


def _drop_checkpoint(sid) -> None:
    try:
        os.remove(checkpoint_path(sid))
    except FileNotFoundError:
        pass


# ══════════════════════════════════════════════════════════════
#  세션 조회
# ══════════════════════════════════════════════════════════════

def hold(bot, session) -> None:
    """재시작 복구: 제거가 진행 중인 세션 객체를 활성 등록 없이 붙잡아 둔다(재개용)."""
    held = getattr(bot, "removal_held", None)
    if not isinstance(held, dict):
        held = {}
        bot.removal_held = held
    held[str(session.session_id)] = session


def _release_hold(bot, sid) -> None:
    held = getattr(bot, "removal_held", None)
    if isinstance(held, dict):
        held.pop(str(sid), None)


def live_session(bot, sid):
    """메모리에 있는 같은 세션 객체(활성 등록 또는 제거 대기 보류). 디스크 사본을 따로 만들지 않기 위함."""
    for s in (getattr(bot, "active_sessions", None) or {}).values():
        if str(getattr(s, "session_id", "")) == sid:
            return s
    return (getattr(bot, "removal_held", None) or {}).get(sid)


def _meta_from_session(s) -> dict:
    m = {k: getattr(s, k, None) for k in CHANNEL_KEYS + ("guild_id", "category_id", "creator_uid",
                                                        "scenario_id", "world_lifecycle", "cache_name")}
    m["players"] = {str(u): (p or {}).get("name") for u, p in (getattr(s, "players", None) or {}).items()}
    return m


def _meta_from_disk(sid) -> dict | None:
    path = os.path.join(SESSIONS_ROOT, sid, "data.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001
        return None
    m = {k: d.get(k) for k in CHANNEL_KEYS + ("guild_id", "category_id", "creator_uid",
                                            "scenario_id", "world_lifecycle", "cache_name")}
    m["players"] = {str(u): (p or {}).get("name") for u, p in (d.get("players") or {}).items()
                    if isinstance(p, dict)}
    return m


def entries(bot) -> dict:
    """{sid: meta} — 디스크 세션(data.json) + 메모리 상태 + 진행 중 제거(체크포인트)."""
    out = {}
    if os.path.isdir(SESSIONS_ROOT):
        for sid in os.listdir(SESSIONS_ROOT):
            if not valid_session_id(sid):
                continue
            path = os.path.join(SESSIONS_ROOT, sid, "data.json")
            if not os.path.isfile(path) or os.path.islink(os.path.join(SESSIONS_ROOT, sid)):
                continue
            meta = _meta_from_disk(sid)
            if meta is None:
                continue
            live = live_session(bot, sid)
            if live is not None:
                meta.update(_meta_from_session(live))
            meta["session_id"], meta["mtime"] = sid, os.path.getmtime(path)
            out[sid] = meta
    for cp in list_checkpoints():
        sid = cp["session_id"]
        m = out.setdefault(sid, dict(cp.get("meta") or {}, session_id=sid, mtime=cp.get("approved_at", 0)))
        m["checkpoint"] = cp
    return out


def guild_id_of(bot, meta: dict):
    """세션 길드: 영속 guild_id > 살아 있는 세션 채널의 길드 > 진행 기록. 모르면 None(운영자 전용)."""
    g = meta.get("guild_id") or (meta.get("checkpoint") or {}).get("guild_id")
    if g:
        return int(g)
    for k in CHANNEL_KEYS:
        ch = bot.get_channel(meta.get(k) or 0)
        if ch is not None and getattr(ch, "guild", None) is not None:
            return ch.guild.id
    return None


def creator_of(meta: dict) -> str:
    return str(meta.get("creator_uid") or (meta.get("checkpoint") or {}).get("creator_uid") or "")


def can_manage(meta: dict, user_id, *, is_operator: bool) -> bool:
    """운영자 = 기존 허가 범위 전부. 생성자 = 자기 세션만(생성자 불명 구 세션은 운영자 전용 — 추측하지 않음)."""
    if is_operator:
        return True
    c = creator_of(meta)
    return bool(c) and c == str(user_id)


def visible(bot, meta: dict, guild_id, user_id, *, is_operator: bool) -> bool:
    g = guild_id_of(bot, meta)
    if g is None:
        return is_operator                     # 길드를 모르는 구 세션은 운영자만
    return g == guild_id and can_manage(meta, user_id, is_operator=is_operator)


def blocked_reason(session) -> str | None:
    """종료·제거를 지금 시작할 수 없는 활성 작업. 강제 취소하지 않는다."""
    if session is None:
        return None
    from . import world_lifecycle as _wl
    if getattr(session, "world_lifecycle", None) not in _wl.ENDING:
        why = _wl.close_blocked_reason(session)
        if why:
            return why
    if getattr(session, "is_compressing", False):
        return "기억 압축이 진행 중입니다 — 끝난 뒤 다시 시도하십시오"
    return None


def _category_of(bot, sid, meta: dict):
    """세션 카테고리 id — 영속 id, 없으면 세션 채널의 부모 중 이 세션 이름(`TRPG Session {id}`)인 것."""
    if meta.get("category_id"):
        return int(meta["category_id"])
    for k in CHANNEL_KEYS:
        ch = bot.get_channel(meta.get(k) or 0)
        cat = getattr(ch, "category", None)
        if cat is not None and cat.name == f"TRPG Session {sid}":
            return cat.id
    return None


def owned_resource_ids(bot) -> set:
    """세션이 소유한 채널·카테고리 id(디스크·메모리·진행 기록) — `!채널정리` 보호용."""
    ids = set()
    for sid, m in entries(bot).items():
        cp = m.get("checkpoint") or {}
        for k in CHANNEL_KEYS:
            for v in (m.get(k), (cp.get("channels") or {}).get(k)):
                if v:
                    ids.add(int(v))
        for v in (m.get("category_id"), cp.get("category_id"), _category_of(bot, sid, m)):
            if v:
                ids.add(int(v))
    return ids


# ══════════════════════════════════════════════════════════════
#  실행(승인 → 체크포인트 → 단계)
# ══════════════════════════════════════════════════════════════

def begin(bot, sid, scope: str, *, actor_id, guild_id, meta: dict) -> dict:
    """확인 [진행] 직후 — 승인 작업을 체크포인트로 영속한다(이미 있으면 그것을 그대로 쓴다)."""
    cp = read_checkpoint(sid)
    if cp is not None:
        return cp
    if scope not in SCOPES:
        raise RemovalError(f"알 수 없는 범위 {scope!r}")
    from . import world_lifecycle as _wl
    cp = {"session_id": sid, "scope": scope, "approved_at": time.time(), "requested_by": str(actor_id),
          "guild_id": guild_id_of(bot, meta) or guild_id, "creator_uid": creator_of(meta),
          "channels": {k: meta.get(k) for k in CHANNEL_KEYS}, "category_id": _category_of(bot, sid, meta),
          "was_open": meta.get("world_lifecycle") not in _wl.ENDING,
          "meta": {k: meta.get(k) for k in ("scenario_id", "players", "creator_uid", "guild_id") + CHANNEL_KEYS},
          "steps": {}}
    _save_checkpoint(cp)
    return cp


def _run_lock(sid) -> asyncio.Lock:
    lk = _RUN_LOCKS.get(sid)
    if lk is None:
        lk = _RUN_LOCKS[sid] = asyncio.Lock()
    return lk


async def run(bot, sid) -> dict:
    """승인된 작업을 이어서 수행한다. {ok, done, lines, reason}. 같은 세션 동시 실행은 거부."""
    lk = _run_lock(sid)
    if lk.locked():
        return {"ok": False, "done": False, "lines": [], "reason": "이미 처리 중입니다"}
    async with lk:
        cp = read_checkpoint(sid)
        if cp is None:
            return {"ok": False, "done": False, "lines": [], "reason": "승인된 작업이 없습니다"}
        rep = {"ok": True, "done": False, "lines": [], "reason": ""}
        sc = SCOPES[cp["scope"]]
        try:
            steps = cp.setdefault("steps", {})
            if not steps.get(STEP_CLOSE) and not await _step_close(bot, sid, cp, rep):
                return rep
            if cp.get("was_open") and not steps.get(STEP_STATS):
                await _step_stats(bot, cp)
                _mark(cp, STEP_STATS)
            if not sc["channels"] and cp.get("was_open") and not steps.get(STEP_LOCK):
                await _step_lock(bot, cp, rep)
                _mark(cp, STEP_LOCK)
            if sc["channels"] and not steps.get(STEP_CHANNELS):
                if not await _step_channels(bot, sid, cp, rep):
                    return rep
                _mark(cp, STEP_CHANNELS)
            if sc["data"]:
                if not steps.get(STEP_MOVED) and not steps.get(STEP_DATA):
                    _step_archive(sid)
                    _mark(cp, STEP_ARCHIVE)
                    await _step_move(bot, sid, cp)
                if not steps.get(STEP_DATA):
                    _step_purge(sid, cp)
                    _mark(cp, STEP_DATA)
                rep["lines"].append("🗑️ 세션 기록을 삭제했습니다(재무 증빙은 별도 보존).")
            _release_hold(bot, sid)
            _drop_checkpoint(sid)
            rep["done"] = True
            return rep
        except Exception as e:  # noqa: BLE001 — 승인 기록이 남아 재개할 수 있다
            rep.update(ok=False, reason=f"{type(e).__name__}: {e}")
            return rep


async def _step_close(bot, sid, cp, rep) -> bool:
    from . import cache_lifecycle as _cl
    from . import profile_creation_ui as _pcu
    from . import world_lifecycle as _wl
    s = live_session(bot, sid)
    src = "live" if s is not None else "disk"
    if s is None:
        from .cache import load_session_from_disk
        s = load_session_from_disk(sid)
    if s is None:
        if os.path.isdir(os.path.join(SESSIONS_ROOT, sid)):
            rep.update(ok=False, reason="세션 데이터를 읽지 못했습니다(시나리오 파일 등) — 운영자 확인이 필요합니다")
            return False
        _mark(cp, STEP_CLOSE)                     # 데이터가 이미 없다 = 앞 실행에서 종료·삭제까지 끝남
        return True
    lc = getattr(s, "world_lifecycle", None)
    if lc != _wl.LIFECYCLE_CLOSED:
        if src == "disk" and lc != _wl.LIFECYCLE_CLOSING:
            # 활성 복구를 거치지 않은 열린 세션 — 커밋·청구 복구 없이 정산하지 않는다
            rep.update(ok=False, reason="복구되지 않은 열린 세션입니다(재시작 복구 실패 등) — 운영자 확인이 필요합니다")
            return False
        why = blocked_reason(s)
        if why:
            rep.update(ok=False, reason=why)
            _abandon_if_untouched(bot, sid, cp, s)
            return False
        res = await _wl.close_session(bot, s, reason=_cl.REASON_OPERATOR_END)
        if not res.get("ok"):
            rep.update(ok=False, reason=res.get("reason") or "종료가 완료되지 않았습니다")
            _abandon_if_untouched(bot, sid, cp, s)
            return False
        if res.get("refund"):
            rep["lines"].append("💰 미사용 캐시 선불 환급: " + ", ".join(
                f"<@{u}> {v}잉크" for u, v in res["refund"].items()))
        if res.get("remote") == _cl.REMOTE_FAILED:
            rep["lines"].append("⚠️ API 서버 측 캐시 삭제 실패 — 만료 시각까지의 보관분으로 정산했습니다.")
        rep["lines"].append("🛑 세션을 종료하고 정산했습니다.")
    else:
        rep["lines"].append("🛑 이미 종료된 세션입니다(정산 없음).")
    _wl.deactivate(bot, s)
    _pcu.release_session(s)
    _release_hold(bot, sid)
    cp["started_at"] = getattr(s, "started_at", 0.0) or 0.0
    cp["players"] = [str(u) for u in (getattr(s, "players", None) or {})]
    _mark(cp, STEP_CLOSE)
    return True


def _abandon_if_untouched(bot, sid, cp, s) -> None:
    """종료 전 거부(진행 중 작업 등) — 아무것도 바뀌지 않았으면 승인을 없던 일로 한다(활성 세션만).

    재시작 보류 세션은 등록을 다시 하지 않았으므로 승인을 유지한다(재개로 잇는다).
    """
    from . import world_lifecycle as _wl
    if cp.get("steps") or getattr(s, "world_lifecycle", None) in _wl.ENDING:
        return
    if sid in (getattr(bot, "removal_held", None) or {}):
        return
    _drop_checkpoint(sid)


async def _step_stats(bot, cp) -> None:
    """기존 종료와 같은 통계·보드 반영 — 이 승인 작업에서 한 번만(체크포인트 단계)."""
    from . import stats as _stats
    from .gm_space import refresh_boards
    started = float(cp.get("started_at") or 0.0)
    seconds = max(0.0, time.time() - started) if started else 0.0
    try:
        for uid in cp.get("players") or ():
            await _stats.bump(uid, session_seconds=seconds)
        g = bot.get_guild(int(cp["guild_id"])) if cp.get("guild_id") else None
        if g is not None:
            await refresh_boards(bot, g)
    except Exception as e:  # noqa: BLE001 — 통계 실패가 종료를 되돌리지 않는다(기존 종료와 같음)
        print(f"[세션관리] 통계·보드 갱신 실패: {e}")


async def _step_lock(bot, cp, rep) -> None:
    ch = bot.get_channel((cp.get("channels") or {}).get("game_ch_id") or 0)
    if ch is None:
        rep["lines"].append("ℹ️ 게임 채널이 없어 잠금을 건너뛰었습니다.")
        return
    try:
        await ch.set_permissions(ch.guild.default_role, send_messages=False)
        await ch.send(master.lead("session.ended_by_operator", "🔒 **세션이 종료되어 채널이 잠겼습니다.**"))
        rep["lines"].append("🔒 게임 채널을 잠갔습니다.")
    except Exception as e:  # noqa: BLE001
        rep["lines"].append(f"⚠️ 게임 채널 잠금 실패: {e}")


async def _fetch(bot, cid):
    ch = bot.get_channel(cid)
    if ch is not None:
        return ch
    try:
        return await bot.fetch_channel(cid)
    except discord.NotFound:
        return None


async def _step_channels(bot, sid, cp, rep) -> bool:
    """영속 id로 식별한 이 세션 채널만 지운다. NotFound = 이미 완료. 다른 길드 채널·잔여 카테고리는 지우지 않는다."""
    gid = int(cp["guild_id"]) if cp.get("guild_id") else None
    chans = cp.get("channels") or {}
    failures = []
    vid = chans.get("voice_ch_id")
    g = bot.get_guild(gid) if gid else None
    vc = getattr(g, "voice_client", None)
    if vc is not None and vid and getattr(getattr(vc, "channel", None), "id", None) == vid:
        try:
            await vc.disconnect(force=True)
        except Exception as e:  # noqa: BLE001
            print(f"[세션관리] 음성 연결 해제 실패: {e}")
    for k in CHANNEL_KEYS:
        cid = chans.get(k)
        if not cid:
            continue
        try:
            ch = await _fetch(bot, int(cid))
            if ch is None:
                continue
            if gid is not None and getattr(getattr(ch, "guild", None), "id", None) != gid:
                failures.append(f"{k}: 다른 서버의 채널 — 지우지 않습니다")
                continue
            await ch.delete(reason=f"세션 관리: {sid}")
        except discord.NotFound:
            continue
        except Exception as e:  # noqa: BLE001
            failures.append(f"{k}: {type(e).__name__}")
    if failures:
        rep.update(ok=False, reason="채널 삭제 일부 실패 — " + "; ".join(failures) + " (다시 시도하면 이어서 처리합니다)")
        return False
    cat_id = cp.get("category_id")
    if cat_id:
        try:
            cat = await _fetch(bot, int(cat_id))
            if cat is not None and (gid is None or cat.guild.id == gid):
                if list(getattr(cat, "channels", None) or ()):
                    rep["lines"].append("ℹ️ 카테고리에 다른 채널이 남아 있어 카테고리는 유지했습니다.")
                else:
                    await cat.delete(reason=f"세션 관리: {sid}")
        except discord.NotFound:
            pass
        except Exception as e:  # noqa: BLE001
            rep.update(ok=False, reason=f"카테고리 삭제 실패({type(e).__name__}) — 다시 시도하면 이어서 처리합니다")
            return False
    rep["lines"].append("🧹 세션 채널을 삭제했습니다.")
    return True


# ── 게임 데이터 ─────────────────────────────────────────────

def session_dir(sid) -> str:
    """세션 폴더 — sessions/ 바로 아래 실제 디렉터리만(경로 탈출·심볼릭 링크 거부)."""
    if not valid_session_id(sid):
        raise RemovalError(f"잘못된 세션 id {sid!r}")
    path = os.path.join(SESSIONS_ROOT, sid)
    if os.path.islink(path):
        raise RemovalError("세션 폴더가 심볼릭 링크입니다 — 삭제하지 않습니다")
    root = os.path.realpath(SESSIONS_ROOT)
    if os.path.dirname(os.path.realpath(path)) != root:
        raise RemovalError("세션 폴더 경로가 sessions/ 밖을 가리킵니다 — 삭제하지 않습니다")
    return path


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def archive_path(sid) -> str:
    return os.path.join(ARCHIVE_DIR, sid)


def _step_archive(sid) -> dict:
    """세션 안 재무 저널을 세션 밖에 복사하고 해시로 검증한다. 실패하면 삭제로 진행하지 않는다(예외)."""
    src = session_dir(sid)
    dst = archive_path(sid)
    os.makedirs(dst, exist_ok=True)
    files = {}
    for fn in FINANCE_FILES:
        sp = os.path.join(src, fn)
        if os.path.islink(sp):
            raise RemovalError(f"재무 저널 {fn}이(가) 심볼릭 링크입니다 — 보존을 확인할 수 없어 삭제를 거부합니다")
        if not os.path.isfile(sp):
            continue
        h = _sha256(sp)
        tmp = os.path.join(dst, f".{fn}.{os.getpid()}.tmp")
        shutil.copyfile(sp, tmp)
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(dst, fn))
        if _sha256(os.path.join(dst, fn)) != h:
            raise RemovalError(f"재무 저널 {fn} 보존 검증 실패 — 삭제를 거부합니다")
        files[fn] = {"sha256": h, "bytes": os.path.getsize(sp)}
    summary = {}
    try:
        with open(os.path.join(src, "data.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        summary = {k: d.get(k) for k in ("scenario_id", "creator_uid", "total_cost", "total_usd",
                                         "total_ink_spent")}
        summary["player_uids"] = sorted(str(u) for u in (d.get("players") or {}))
    except Exception:  # noqa: BLE001
        pass
    manifest = {"session_id": sid, "files": files, "summary": summary}
    _write_json_atomic(os.path.join(dst, "manifest.json"), manifest)
    return manifest


def _verify_archive(sid) -> None:
    """이동 직전: 원본 재무 저널이 보존본과 같은지(그 사이 추가 기록이 없는지) 확인한다."""
    with open(os.path.join(archive_path(sid), "manifest.json"), "r", encoding="utf-8") as f:
        files = (json.load(f) or {}).get("files") or {}
    src = session_dir(sid)
    for fn in FINANCE_FILES:
        sp = os.path.join(src, fn)
        if os.path.isfile(sp) and (fn not in files or _sha256(sp) != files[fn]["sha256"]):
            raise RemovalError(f"재무 저널 {fn}이(가) 보존 뒤 바뀌었습니다 — 다시 시도하십시오")


async def _step_move(bot, sid, cp) -> None:
    """정본 직렬화 락 안에서: 보존 검증 → 제거 표식 → 세션 폴더를 휴지 위치로 원자적 이동."""
    from .commit_coordinator import commit_serialization_lock
    async with commit_serialization_lock(sid):
        src = session_dir(sid)
        if not os.path.isdir(src):
            _mark(cp, STEP_MOVED, trash=None)
            return
        _verify_archive(sid)
        _io.mark_session_removed(sid)
        os.makedirs(TRASH_DIR, exist_ok=True)
        dst = os.path.join(TRASH_DIR, f"{sid}.{uuid.uuid4().hex[:8]}")
        os.rename(src, dst)
        _mark(cp, STEP_MOVED, trash=dst)


def _step_purge(sid, cp) -> None:
    _io.mark_session_removed(sid)
    trash = cp.get("trash")
    if trash:
        real_trash = os.path.realpath(TRASH_DIR)
        if os.path.dirname(os.path.realpath(trash)) != real_trash or os.path.islink(trash):
            raise RemovalError("휴지 경로가 예상 위치가 아닙니다 — 삭제하지 않습니다")
        if os.path.isdir(trash):
            shutil.rmtree(trash)
    # 늦은 비동기 기록이 표식 이전에 다시 만든 빈 폴더(data.json 없음)만 정리한다
    left = os.path.join(SESSIONS_ROOT, sid)
    if os.path.isdir(left) and not os.path.islink(left) and not os.path.exists(os.path.join(left, "data.json")):
        shutil.rmtree(left)


# ══════════════════════════════════════════════════════════════
#  화면(`!세션관리`)
# ══════════════════════════════════════════════════════════════

async def is_operator(bot, user) -> bool:
    cog = bot.get_cog("PermissionCog")
    if cog is None:
        return False
    return await cog._is_authorized(user)


def _status_text(meta: dict) -> str:
    cp = meta.get("checkpoint")
    if cp:
        done = ", ".join(k for k, v in (cp.get("steps") or {}).items() if v) or "시작 전"
        return f"제거 진행 중 — {SCOPES[cp['scope']]['label']} (완료: {done})"
    lc = meta.get("world_lifecycle") or "진행 중"
    cache = "캐시 열림" if meta.get("cache_name") else "캐시 없음"
    return f"{lc} · {cache}"


def _option_label(meta: dict) -> str:
    return f"{meta['session_id']} · {meta.get('scenario_id') or '?'}"[:100]


PAGE_SIZE = 25          # 디스코드 선택 메뉴 항목 한도


def _is_closed(meta: dict) -> bool:
    return meta.get("world_lifecycle") == "CLOSED" and not meta.get("checkpoint")


def _no_cache(meta: dict) -> bool:
    return not meta.get("cache_name") and not meta.get("checkpoint")


FILTERS = (("종료된 세션 전부", _is_closed), ("캐시 없는 세션 전부", _no_cache))


class ManageView(discord.ui.View):
    """세션 선택(다중·페이지·상태별 일괄) → 범위 선택 → 확인(1회) → 일괄 실행.

    v7.32 일괄 관리(사용자 결정): 막힌 세션은 건너뛰고 나머지를 진행, 진행 중 제거는 승인된 원래 범위로 재개.
    진행 중 제거만 고른 경우는 기존처럼 확인 없이 [재개]. 권한·소유권은 매 조작·실행마다 서버 측으로 다시 본다.
    """

    def __init__(self, bot, author, guild):
        super().__init__(timeout=600)
        self.bot, self.author, self.guild = bot, author, guild
        self.selected: list = []            # 선택 순서 유지
        self.scope = None
        self.page = 0
        self.message = None
        self.metas = {}

    # 단일 세션 호환(기존 호출부·테스트): sid = 선택이 하나일 때의 그 세션
    @property
    def sid(self):
        return self.selected[0] if len(self.selected) == 1 else None

    @sid.setter
    def sid(self, value):
        self.selected = [value] if value else []

    async def prepare(self) -> bool:
        op = await is_operator(self.bot, self.author)
        shown = [m for m in entries(self.bot).values()
                 if visible(self.bot, m, self.guild.id, self.author.id, is_operator=op)]
        shown.sort(key=lambda m: m.get("mtime") or 0, reverse=True)
        self.metas = {m["session_id"]: m for m in shown}
        if not self.metas:
            return False
        self._build_home()
        return True

    # ── 선택 화면 ──
    def pages(self) -> int:
        return max(1, -(-len(self.metas) // PAGE_SIZE))

    def page_ids(self) -> list:
        ids = list(self.metas)
        return ids[self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE]

    def _build_home(self) -> None:
        self.clear_items()
        self.add_item(_SessionSelect(self))
        for label, pred in FILTERS:
            self.add_item(_Button(label, discord.ButtonStyle.secondary, self._filter_handler(pred), row=1))
        self.add_item(_Button("선택 해제", discord.ButtonStyle.secondary, self._clear, row=1))
        self.add_item(_Button("◀", discord.ButtonStyle.secondary, self._prev, row=2, disabled=self.page == 0))
        self.add_item(_Button("▶", discord.ButtonStyle.secondary, self._next, row=2,
                              disabled=self.page >= self.pages() - 1))
        self.add_item(_Button(f"다음 ({len(self.selected)}개)", discord.ButtonStyle.primary, self._to_scope, row=2,
                              disabled=not self.selected))
        self.add_item(_Button("닫기", discord.ButtonStyle.secondary, self._cancel, row=2))

    def home_embed(self) -> discord.Embed:
        e = discord.Embed(title="🗂️ 세션 관리", color=0x5865F2, description=(
            f"정리할 세션을 고르십시오(여러 개 가능). 페이지 {self.page + 1}/{self.pages()} · 전체 {len(self.metas)}개 · "
            f"선택 {len(self.selected)}개\n상태별 버튼은 모든 페이지에서 해당 세션을 선택에 더합니다."))
        for sid in self.page_ids()[:10]:
            m = self.metas[sid]
            e.add_field(name=("✅ " if sid in self.selected else "") + _option_label(m), value=_status_text(m),
                        inline=False)
        if len(self.page_ids()) > 10:
            e.set_footer(text=f"이 페이지의 나머지 {len(self.page_ids()) - 10}개는 선택 메뉴에서 보입니다.")
        return e

    async def _show_home(self, interaction):
        self._build_home()
        await interaction.response.edit_message(embed=self.home_embed(), view=self)

    def set_page_selection(self, values) -> None:
        page = set(self.page_ids())
        keep = [x for x in self.selected if x not in page]
        self.selected = keep + [x for x in self.page_ids() if x in set(values)]

    def _filter_handler(self, pred):
        async def handler(interaction):
            add = [sid for sid, m in self.metas.items() if pred(m) and sid not in self.selected]
            self.selected += add
            await self._show_home(interaction)
        return handler

    async def _clear(self, interaction):
        self.selected = []
        await self._show_home(interaction)

    async def _prev(self, interaction):
        self.page = max(0, self.page - 1)
        await self._show_home(interaction)

    async def _next(self, interaction):
        self.page = min(self.pages() - 1, self.page + 1)
        await self._show_home(interaction)

    async def _to_scope(self, interaction):
        self.scope = None
        await self.show_confirm(interaction)

    async def interaction_check(self, interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("이 화면은 명령을 입력한 분만 쓸 수 있습니다.", ephemeral=True)
            return False
        return True

    async def recheck_all(self, interaction) -> dict:
        """서버 측 재검사 — 실행자·길드·세션·소유권을 매번 다시 본다. 통과한 세션의 최신 meta만."""
        op = await is_operator(self.bot, interaction.user)
        all_e = entries(self.bot)
        out = {}
        for sid in self.selected:
            meta = all_e.get(sid)
            if meta is not None and visible(self.bot, meta, self.guild.id, interaction.user.id, is_operator=op):
                out[sid] = meta
        return out

    async def recheck(self, interaction):
        """단일 세션 호환 — 선택이 하나일 때 그 세션의 최신 meta."""
        return (await self.recheck_all(interaction)).get(self.sid or "")

    # ── 범위 · 확인 ──
    async def show_confirm(self, interaction):
        metas = await self.recheck_all(interaction)
        if not metas:
            return await self._finish(interaction, "⛔ 이 세션을 관리할 권한이 없거나 세션을 찾을 수 없습니다.")
        self.selected = [sid for sid in self.selected if sid in metas]
        self.clear_items()
        resume = [sid for sid, m in metas.items() if m.get("checkpoint")]
        fresh = [sid for sid in metas if sid not in resume]
        if not fresh:                       # 진행 중 제거만 — 승인된 작업이라 확인 없이 재개
            e = discord.Embed(title="⏯️ 진행 중인 제거", color=0xFEE75C, description="\n".join(
                [f"`{sid}` — 이미 승인된 **{SCOPES[metas[sid]['checkpoint']['scope']]['label']}** 작업: "
                 f"{_status_text(metas[sid])}" for sid in resume] + ["[재개]를 누르면 남은 단계를 이어서 처리합니다."])[:4000])
            self.add_item(_Button("재개", discord.ButtonStyle.primary, self._resume))
            self.add_item(_Button("닫기", discord.ButtonStyle.secondary, self._cancel))
            return await interaction.response.edit_message(embed=e, view=self)
        if self.scope is None:
            self.add_item(_ScopeSelect(self))
            self.add_item(_Button("뒤로", discord.ButtonStyle.secondary, self._show_home, row=1))
            head = (f"`{fresh[0]}` — {_status_text(metas[fresh[0]])}" if len(fresh) == 1
                    else f"새로 정리할 세션 {len(fresh)}개")
            extra = f"\n진행 중 제거 {len(resume)}개는 원래 범위로 재개합니다." if resume else ""
            e = discord.Embed(title=f"🗂️ {fresh[0] if len(fresh) == 1 else '일괄 정리'}", color=0x5865F2,
                              description=f"{head}{extra}\n정리 범위를 고르십시오(새로 정리할 세션 모두에 적용).")
            return await interaction.response.edit_message(embed=e, view=self)
        sc = SCOPES[self.scope]
        lines, n_settle, n_skip = [], 0, 0
        for sid in fresh:
            m = metas[sid]
            why = blocked_reason(live_session(self.bot, sid))
            closed = m.get("world_lifecycle") == "CLOSED"
            n_settle += int(not closed and bool(m.get("cache_name")))
            n_skip += int(bool(why))
            players = ", ".join(f"<@{u}>" for u in (m.get("players") or {})) or "참가자 없음"
            lines.append(f"`{sid}` · {m.get('scenario_id') or '?'} · {players} · {_status_text(m)}"
                         + (f" — ⏸️ 지금은 건너뜀: {why}" if why else ""))
        for sid in resume:
            lines.append(f"`{sid}` — ⏯️ 재개(원래 범위 {SCOPES[metas[sid]['checkpoint']['scope']]['label']})")
        lost = ["종료된 세션은 다시 열 수 없습니다."]
        if sc["channels"]:
            lost.append("채널·메시지는 복구할 수 없습니다.")
        if sc["data"]:
            lost.append("세션 기록(대화·이력·세계 상태·로그)은 복구할 수 없습니다. 재무 원장·잉크 거래·정산 증빙은 보존됩니다.")
        settle = (f"열린 캐시 {n_settle}개 — 종료하며 캐시를 파기하고 실제 보관비를 정산합니다. "
                  "미사용 선불은 결제한 플레이어에게 환급됩니다." if n_settle else
                  "열린 캐시가 없습니다. 생애주기만 종료합니다(이미 종료된 세션은 정산 없음).")
        e = discord.Embed(title="⚠️ 세션 정리 확인", color=0xED4245)
        targets = "\n".join(lines)
        e.add_field(name=f"대상 {len(metas)}개" + (f" (건너뜀 예정 {n_skip})" if n_skip else ""),
                    value=targets if len(targets) <= 1024 else targets[:1000] + f"\n… 외 {targets[1000:].count(chr(10)) + 1}줄",
                    inline=False)
        e.add_field(name="정산", value=settle, inline=False)
        e.add_field(name=f"범위 — {sc['label']}", value=sc["desc"], inline=False)
        e.add_field(name="복구 불가", value="\n".join(f"· {x}" for x in lost), inline=False)
        self.add_item(_Button("진행", discord.ButtonStyle.danger, self._proceed))
        self.add_item(_Button("취소", discord.ButtonStyle.secondary, self._cancel))
        await interaction.response.edit_message(embed=e, view=self)

    # ── 실행 ──
    async def _proceed(self, interaction):
        await self._execute(interaction, approve=True)

    async def _resume(self, interaction):
        await self._execute(interaction, approve=False)

    async def _execute(self, interaction, *, approve: bool):
        metas = await self.recheck_all(interaction)
        if not metas:
            return await self._finish(interaction, "⛔ 이 세션을 관리할 권한이 없거나 세션을 찾을 수 없습니다.")
        self.clear_items()
        self.stop()
        order = [sid for sid in self.selected if sid in metas]
        try:
            await interaction.response.edit_message(
                embed=discord.Embed(description=f"⏳ 세션 {len(order)}개 정리 중…", color=0x5865F2), view=None)
        except Exception:  # noqa: BLE001
            pass
        done, skipped, partial, details = [], [], [], []
        for i, sid in enumerate(order, 1):
            meta = metas[sid]
            if not meta.get("checkpoint"):
                if not approve or self.scope is None:
                    continue
                why = blocked_reason(live_session(self.bot, sid))
                if why:                                    # 막힌 세션은 건너뛴다(강제 취소 없음)
                    skipped.append(f"`{sid}` — {why}")
                    continue
                begin(self.bot, sid, self.scope, actor_id=interaction.user.id, guild_id=self.guild.id, meta=meta)
            rep = await run(self.bot, sid)
            if rep["done"]:
                done.append(sid)
            elif rep["ok"] is False and read_checkpoint(sid) is None:
                skipped.append(f"`{sid}` — {rep['reason']}")
            else:
                partial.append(f"`{sid}` — {rep['reason']}")
            details += [f"`{sid}` {ln}" for ln in rep["lines"]]
            if len(order) > 1:
                try:
                    await interaction.edit_original_response(embed=discord.Embed(
                        description=f"⏳ 세션 정리 중… {i}/{len(order)}", color=0x5865F2), view=None)
                except Exception:  # noqa: BLE001
                    pass
        await self._report(interaction, order, done, skipped, partial, details)

    async def _report(self, interaction, order, done, skipped, partial, details):
        from .master_report import pack
        if len(order) == 1:                       # 단일 세션 — 기존 문구
            sid = order[0]
            head = (f"✅ `{sid}` 정리를 마쳤습니다." if done else
                    f"⏸️ `{sid}` 정리를 시작하지 않았습니다: {skipped[0].split(' — ', 1)[1]}" if skipped else
                    f"⚠️ `{sid}` 정리가 끝나지 않았습니다: {partial[0].split(' — ', 1)[1]}\n"
                    "`!세션관리`에서 [재개]하면 이어서 처리합니다." if partial else f"`{sid}` — 처리한 것이 없습니다.")
            text = "\n".join([head] + [d.split(" ", 1)[1] for d in details])
            embeds = [discord.Embed(description=text[:4000], color=0x57F287 if done else 0xFEE75C)]
        else:
            text = (f"완료 {len(done)} · 건너뜀 {len(skipped)} · 미완료 {len(partial)} (요청 {len(order)})"
                    + ("\n미완료는 `!세션관리`에서 [재개]하면 이어서 처리합니다." if partial else ""))
            secs = [(f"✅ 완료 {len(done)}", [f"`{x}`" for x in done]),
                    (f"⏸️ 건너뜀 {len(skipped)}", skipped), (f"⚠️ 미완료 {len(partial)}", partial),
                    ("세부", details)]
            embeds = pack("🗂️ 일괄 정리 결과", 0x57F287 if not (skipped or partial) else 0xFEE75C,
                          [(n, ls) for n, ls in secs if ls], description=text)[:10]
            text = text + "\n" + "\n".join(l for _n, ls in secs for l in ls)
        try:
            await interaction.edit_original_response(embeds=embeds, view=None)
        except Exception:  # noqa: BLE001 — 명령을 입력한 채널이 삭제된 경우
            try:
                await self.author.send(text[:2000])
            except Exception:  # noqa: BLE001
                pass

    async def _cancel(self, interaction):
        await self._finish(interaction, "취소했습니다. 아무것도 바뀌지 않았습니다.")

    async def _finish(self, interaction, text):
        self.clear_items()
        self.stop()
        await interaction.response.edit_message(embed=discord.Embed(description=text, color=0x99AAB5), view=None)

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:  # noqa: BLE001
                pass


class _SessionSelect(discord.ui.Select):
    def __init__(self, view: ManageView):
        ids = view.page_ids()
        super().__init__(placeholder="세션 선택(여러 개)", min_values=0, max_values=len(ids), row=0, options=[
            discord.SelectOption(label=_option_label(view.metas[sid]), value=sid,
                                 description=_status_text(view.metas[sid])[:100], default=sid in view.selected)
            for sid in ids])

    async def callback(self, interaction):
        self.view.set_page_selection(self.values)
        await self.view._show_home(interaction)


class _ScopeSelect(discord.ui.Select):
    def __init__(self, view: ManageView):
        super().__init__(placeholder="정리 범위", options=[
            discord.SelectOption(label=v["label"], value=k, description=v["desc"][:100]) for k, v in SCOPES.items()])

    async def callback(self, interaction):
        self.view.scope = self.values[0]
        await self.view.show_confirm(interaction)


class _Button(discord.ui.Button):
    def __init__(self, label, style, handler, *, row=None, disabled=False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.on_press = handler

    async def callback(self, interaction):
        await self.on_press(interaction)
