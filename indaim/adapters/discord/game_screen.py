# 게임 채널 턴 화면(v9.3 — 계획서 v1 · 사용자 결정 D1~D4 가)
#
# · 상단 SVG 하나: 게임 채널에 세션마다 상태 메시지 하나를 두고 커밋된 턴마다 그 메시지를 고쳐 쓴다
#   (턴·날짜·요일·시각·시간대·장소는 정본, '상황 요약'은 모델 상태창 줄, 이동 경로 연결도 = turn_world.route_map).
#   양식은 디스플레이 상태판(display_board.framed·fit) 공용. 렌더 실패 시 같은 메시지를 글로 대체.
# · 한 턴 단위: 새 턴이 커밋되면 상단 SVG와 이번 턴 선언 사이의 메시지(지난 턴 출력·선언·되묻기·판정 화면)를 지운다.
#   디스코드는 순서를 바꿀 수 없으므로 "SVG 아래만 지운다"로 맨 위를 유지한다. 권한·기간 제한으로 못 지우면 남긴다.
# · 되감기: SVG를 그 턴 값으로 고치고 SVG 아래를 비운 뒤, 선택된 턴의 묘사 원문(텍스트만)을 다시 게시한다(provider 0).
# · 정본·청구·이력은 바꾸지 않는다. 원문은 full_logs·구조 로그·턴 이력에 남는다.
from __future__ import annotations

import asyncio
import hashlib
import io
import re

import discord

from indaim.adapters.discord import dialogue as _dlg
from indaim.adapters.discord import display_board as _db


def _tw():
    from indaim.turn import turn_world        # 지연 import — 모듈 적재 순환 회피
    return turn_world


def _io():
    from indaim.app import io
    return io

FIELD = "game_screen"            # 세션 필드 {"message_id": int, "summary": str}
SWEEP_LIMIT = 200


def _state(session) -> dict:
    st = getattr(session, FIELD, None)
    if not isinstance(st, dict):
        st = {}
        setattr(session, FIELD, st)
    return st


def screen_data(session, summary: str | None = None) -> dict | None:
    """상단 SVG 값 — 동적 세계가 아니면 None(그때는 기존 코드블럭 출력을 유지한다)."""
    vals = _tw().screen_values(session)
    if vals is None:
        return None
    st = _state(session)
    if summary is not None:
        st["summary"] = summary
    return {**vals, "summary": st.get("summary") or "",
            "scenario": str(getattr(session, "scenario_id", "") or "")}


def enabled(session) -> bool:
    return screen_data(session) is not None


# ══════════════════════════════════════════════════════════════
#  SVG — 디스플레이 상태판 양식
# ══════════════════════════════════════════════════════════════

def build_svg(d: dict) -> bytes:
    o: list = []
    t = _db._t
    # 머리 띠 — 시나리오 · 턴
    o.append('<path d="M34 36 H1166 V118 H34 Z" fill="#0b0f12" stroke="#72797f" stroke-width="1.8"/>')
    o.append('<path d="M34 36 H300 L272 118 H34 Z" fill="url(#diag)"/>')
    o.append('<path d="M48 48 H62 V106 H48 Z" fill="#d49a2c"/>')
    title, size = _db.fit(d.get("scenario") or "INDAIM", 34, 210, 22)
    o.append(t(78, 90, title, "t h", size))
    o.append('<line x1="312" y1="50" x2="312" y2="104" class="line2"/>')
    o.append(t(336, 70, "턴", "t label"))
    o.append(t(336, 102, f"#{d.get('turn', '')}", "t value", 28))
    o.append('<line x1="470" y1="50" x2="470" y2="104" class="line2"/>')
    o.append(t(494, 70, "날짜 · 시각", "t label"))
    when = f"{d.get('date', '')} {d.get('weekday', '')} · {d.get('hhmm', '')} ({d.get('daypart', '')})"
    txt, s = _db.fit(when, 26, 650, 17)
    o.append(t(494, 102, txt, "t value", s))
    y = 118 + _db.GAP
    # 현재 장소 · 상황
    rm = d.get("routes") or {}
    place = rm.get("transit") or d.get("place") or ""
    h = 140
    _db._section(o, y, h, "현재", [(80, "#e9e9e4"), (20, "#d49a2c")])
    txt, s = _db.fit(place, 26, 1080, 17)
    o.append(t(52, y + 88, txt, "t value", s))
    summary = d.get("summary") or ""
    if summary:
        txt, s = _db.fit(f"상황 · {summary}", 22, 1080, 16)
        o.append(t(52, y + 124, txt, "t dim", s))
    y += h + _db.GAP
    # 이동 경로 연결도(F1)
    lines = (_tw().route_map_text(rm, svg=True).splitlines()[1:] if not rm.get("transit")
             else ["길 위 — 도착하면 갈림길이 보입니다"])
    lines = lines or [" └─ (아는 길 없음)"]
    h = 70 + 38 * len(lines)
    _db._section(o, y, h, "이동 경로", [(80, "#e9e9e4"), (20, "#777d82")])
    for i, ln in enumerate(lines):
        txt, s = _db.fit(ln, 23, 1080, 15)
        o.append(t(52, y + 88 + 38 * i, txt, "t value", s))
    y += h
    return _db.framed(o, y + 30)


def fallback_text(d: dict) -> str:
    """렌더 실패 시 같은 메시지에 쓰는 글(옛 상태창 형식 + 연결도)."""
    rm = d.get("routes") or {}
    head = (f"📅 [#{d.get('turn')}] [{d.get('date')} {d.get('weekday')} {d.get('daypart')} {d.get('hhmm')}]\n"
            f"📍 {rm.get('transit') or d.get('place')}")
    if d.get("summary"):
        head += f"\n상황 요약 : {d['summary']}"
    graph = _tw().route_map_text(rm)
    return f"```css\n{head}\n```" + (f"\n```\n{graph}\n```" if graph and not rm.get("transit") else "")


# ══════════════════════════════════════════════════════════════
#  메시지 — 고쳐 쓰기 · 정리
# ══════════════════════════════════════════════════════════════

async def _fetch(channel, mid):
    if not mid:
        return None
    try:
        return await channel.fetch_message(int(mid))
    except Exception:  # noqa: BLE001
        return None


async def update(bot, session, *, summary: str | None = None, channel=None) -> bool:
    """상단 SVG를 지금 정본 값으로 고친다(없거나 접근 불가면 새로 만든다). 같은 SVG면 다시 올리지 않는다."""
    d = screen_data(session, summary)
    if d is None:
        return False
    channel = channel or bot.get_channel(getattr(session, "game_ch_id", None))
    if channel is None:
        return False
    st = _state(session)
    svg = build_svg(d)
    key = hashlib.sha256(svg).hexdigest()
    msg = await _fetch(channel, st.get("message_id"))
    if msg is not None and st.get("key") == key:
        return True
    try:
        png = await asyncio.to_thread(_db.render_png, svg)
        payload = {"content": None, "attachments": [discord.File(io.BytesIO(png), filename="turn.png")]}
    except Exception as e:  # noqa: BLE001 — 렌더러 없음·실패: 같은 자리를 글로
        print(f"[턴 화면] SVG 렌더 실패 → 글 대체: {e}")
        payload = {"content": fallback_text(d), "attachments": []}
    try:
        if msg is not None:
            await msg.edit(**payload)
        else:
            kw = {"content": payload["content"]}
            if payload["attachments"]:
                kw["file"] = payload["attachments"][0]
            msg = await channel.send(**kw)
            st["message_id"] = getattr(msg, "id", None)
        st["key"] = key
    except Exception as e:  # noqa: BLE001
        print(f"[턴 화면] 상단 메시지 갱신 실패(진행 유지): {e}")
        return False
    try:
        await _io().save_session_data(bot, session)
    except Exception:  # noqa: BLE001
        pass
    return True


async def ensure(bot, session, *, channel=None) -> bool:
    """상단 SVG가 없으면(첫 인트로·배포 전부터 진행 중인 세션) 묘사보다 먼저 만든다 — 있으면 손대지 않는다."""
    channel = channel or bot.get_channel(getattr(session, "game_ch_id", None))
    if channel is None:
        return False
    if await _fetch(channel, _state(session).get("message_id")) is not None:
        return True
    _state(session).pop("key", None)
    return await update(bot, session, channel=channel)


async def sweep(bot, session, *, keep_from=None, channel=None) -> int:
    """상단 SVG 아래, keep_from(이번 턴 선언) 이전의 메시지를 지운다. keep_from 없으면 SVG 아래 전부."""
    channel = channel or bot.get_channel(getattr(session, "game_ch_id", None))
    top = _state(session).get("message_id")
    if channel is None or not top or not hasattr(channel, "history"):
        return 0
    try:
        kw = {"limit": SWEEP_LIMIT, "after": discord.Object(id=int(top))}
        if keep_from:
            kw["before"] = discord.Object(id=int(keep_from))
        msgs = [m async for m in channel.history(**kw) if getattr(m, "id", None) != int(top)]
    except Exception as e:  # noqa: BLE001
        print(f"[턴 화면] 지난 턴 목록 실패(남김): {e}")
        return 0
    if not msgs:
        return 0
    try:
        for i in range(0, len(msgs), 100):
            chunk = msgs[i:i + 100]
            if len(chunk) == 1:
                await chunk[0].delete()
            else:
                await channel.delete_messages(chunk)
        return len(msgs)
    except Exception:  # noqa: BLE001 — 일괄 삭제 불가(권한·14일) → 하나씩, 실패는 남긴다
        n = 0
        for m in msgs:
            try:
                await m.delete()
                n += 1
            except Exception:  # noqa: BLE001
                pass
        return n


async def on_turn_committed(bot, session, prep=None) -> None:
    """커밋 직후 — 상단 SVG 갱신 + 지난 턴 정리(이번 턴 선언부터 남긴다)."""
    try:
        if not enabled(session):
            return
        await update(bot, session, summary=getattr(prep, "status_summary", None))
        keep = getattr(session, "_turn_start_message_id", None)
        if keep:
            await sweep(bot, session, keep_from=keep)
    except Exception as e:  # noqa: BLE001
        print(f"[턴 화면] 커밋 뒤 갱신 실패(진행 유지): {e}")


def _head_narration(session) -> str:
    for e in reversed(list(getattr(session, "raw_logs", None) or ())):
        role = getattr(e, "role", None) or (e.get("role") if isinstance(e, dict) else None)
        if role != "model":
            continue
        parts = getattr(e, "parts", None)
        if parts is not None:
            return "".join(getattr(p, "text", "") or "" for p in parts)
        return str((e.get("text") if isinstance(e, dict) else "") or "")
    return ""


async def after_rewind(bot, session, target_turn=None) -> None:
    """되감기 뒤(D4 가) — SVG를 그 턴 값으로, 아래를 비우고, 선택된 턴 묘사 원문을 텍스트로 다시 게시(provider 0)."""
    try:
        if not enabled(session):
            return
        text = _head_narration(session)
        m = re.search(r"(.*)(```.*?```)\s*$", text, re.DOTALL)
        body, block = (m.group(1).strip(), m.group(2)) if m else (text.strip(), "")
        await update(bot, session, summary=_tw().status_summary(block))
        await sweep(bot, session)
        channel = bot.get_channel(getattr(session, "game_ch_id", None))
        if channel is None:
            return
        out = [f"⏪ {target_turn if target_turn is not None else session.turn_count}턴으로 되돌렸습니다."]
        for p in [p.strip() for p in body.split("\n\n") if p.strip()]:
            dl = _dlg.parse_dialogue_paragraph(p)
            out.append(_dlg.format_dialogue_block(*dl) if dl else p)
        buf = ""
        for piece in out:
            if buf and len(buf) + len(piece) + 2 > 1900:
                await channel.send(buf)
                buf = ""
            buf = (buf + "\n\n" + piece) if buf else piece[:1900]
        if buf:
            await channel.send(buf)
    except Exception as e:  # noqa: BLE001
        print(f"[턴 화면] 되감기 뒤 갱신 실패(진행 유지): {e}")
