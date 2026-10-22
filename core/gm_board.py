# GM 스페이스 카드 · 시작 상황 선택 카드 — SVG 이미지(디스플레이 상태판과 같은 톤)
#
# [권위 — 사용자 결정 2026-10-22]
#   · 홈·명예의 전당·서버보드·월드보드·개인 통계·시작 상황 삼지선다의 Embed/글 목록을 이미지로 바꾼다.
#     표시 정보는 기존 Embed와 같은 출처·같은 규칙이다(새 정보 항목 없음, 보드별 공개 규칙 그대로).
#   · 시각 기준은 디스플레이 상태판(core.display_board) — 같은 DEFS·틀·글자 맞춤·래스터화를 쓴다.
#     명예의 전당만 금·은·동 포인트를 준다.
#   · 이미지에는 멘션을 넣을 수 없다 — 호출자가 서버 표시 이름을 넘긴다(없으면 '알 수 없는 사용자').
#     누를 수 있어야 하는 것(초대 링크·버튼)은 메시지 본문·View에 남긴다.
#   · 렌더 실패(렌더러·폰트 없음 등)는 호출자가 기존 Embed/글로 대체한다.
from __future__ import annotations

import asyncio
import hashlib
import io

from . import display_board as DB
from .display_board import _t, ellipsize, fit, text_width

UNKNOWN_NAME = "알 수 없는 사용자"

GOLD_DEFS = DB.DEFS.replace("</defs>", (
    '<linearGradient id="gold" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff1b0"/>'
    '<stop offset="0.35" stop-color="#e2b33c"/><stop offset="0.7" stop-color="#a9781a"/>'
    '<stop offset="1" stop-color="#f4d36b"/></linearGradient>'
    '<linearGradient id="silver" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/>'
    '<stop offset="0.4" stop-color="#c3c8cc"/><stop offset="0.75" stop-color="#7e868c"/>'
    '<stop offset="1" stop-color="#e3e6e8"/></linearGradient>'
    '<linearGradient id="bronze" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f3c39a"/>'
    '<stop offset="0.4" stop-color="#c47a3e"/><stop offset="0.75" stop-color="#7d4520"/>'
    '<stop offset="1" stop-color="#dc9c66"/></linearGradient>'
    '<pattern id="golddiag" width="24" height="24" patternUnits="userSpaceOnUse" patternTransform="rotate(32)">'
    '<rect width="9" height="24" fill="#e2b33c" opacity="0.16"/></pattern>'
    '</defs>'))
MEDAL = {1: ("url(#gold)", "#e2b33c"), 2: ("url(#silver)", "#c3c8cc"), 3: ("url(#bronze)", "#c47a3e")}


# ══════════════════════════════════════════════════════════════
#  데이터 — 기존 Embed와 같은 출처
# ══════════════════════════════════════════════════════════════

def stat_items(st: dict) -> list:
    """stats.format_summary와 같은 항목·같은 값 — [(라벨, 값)]."""
    hours = (st.get("session_seconds") or 0) / 3600
    return [("플레이 턴", f"{st.get('turns', 0):,}"), ("세션", f"{st.get('sessions', 0)} ({hours:.1f}시간)"),
            ("클리어 퀘스트", str(st.get("quests_cleared", 0))), ("만난 NPC", str(st.get("npcs_met", 0))),
            ("주사위", f"{st.get('dice_rolled', 0):,}"),
            ("상태이상 획득/해제", f"{st.get('status_applied', 0)} / {st.get('status_cleared', 0)}"),
            ("소모", f"{st.get('ink_spent', 0):,}잉크")]


def hall_data(member_ids: list, names: dict, guild_name: str = "") -> dict:
    """명예의 전당 — 등록자만, 플레이 턴 순위(build_hall_embed와 같은 규칙)."""
    from . import stats as stats_mod
    from .gm_space import BOARD_LIMIT
    rows = stats_mod.leaderboard(member_ids, key="turns", only_registered=True, limit=BOARD_LIMIT)
    return {"kind": "hall", "title": "명예의 전당", "latin": "HALL OF FAME",
            "basis": guild_name or "기준", "subtitle": "플레이 턴 순위",
            "empty": "등록된 인원이 없습니다",
            "rows": [{"rank": i, "name": names.get(r["user_id"]) or UNKNOWN_NAME, "items": stat_items(r["stats"])}
                     for i, r in enumerate(rows, 1)]}


def board_data(member_ids: list, names: dict, *, world: bool = False) -> dict:
    """서버보드/월드보드 — 기본은 턴만, 서버보드는 명전 등록·월드보드는 공개 선택 시 세션·NPC까지
    (build_board_embed와 같은 규칙)."""
    from . import stats as stats_mod
    from .gm_space import BOARD_LIMIT
    rows = stats_mod.leaderboard(member_ids, key="turns", limit=BOARD_LIMIT)
    out = []
    for i, r in enumerate(rows, 1):
        st = r["stats"]
        items = [("턴", f"{r['value']:,}")]
        if st.get("public") if world else st.get("hall_registered"):
            items += [("세션", str(st.get("sessions", 0))), ("NPC", str(st.get("npcs_met", 0)))]
        out.append({"rank": i, "name": names.get(r["user_id"]) or UNKNOWN_NAME, "items": items})
    return {"kind": "world" if world else "server", "title": "월드보드" if world else "서버보드",
            "latin": "WORLD BOARD" if world else "SERVER BOARD", "basis": "기준", "subtitle": "플레이 턴 순위",
            "empty": "기록이 없습니다", "rows": out}


def home_data(version: str) -> dict:
    return {"title": "INDAIM", "latin": "GM SPACE", "version": f"v{version}",
            "lines": ["세션 생성과 계정 관리를 이곳에서 진행합니다.", "아래 버튼으로 조작하십시오. 이 채널은 채팅이 불가합니다."],
            "footer": "계정 등록 후 세션을 열 수 있습니다."}


def personal_data(name: str, st: dict) -> dict:
    return {"title": "내 통계", "latin": "PERSONAL RECORD", "name": name or UNKNOWN_NAME, "items": stat_items(st)}


def frames_data(options: list) -> dict:
    return {"title": "시작 상황", "latin": "STARTING POINT",
            "options": [{"index": i, "title": str(o.get("title") or "(제목 없음)"), "summary": str(o.get("summary") or "")}
                        for i, o in enumerate(options, 1)]}


# ══════════════════════════════════════════════════════════════
#  SVG
# ══════════════════════════════════════════════════════════════

def wrap(text: str, size: float, max_w: float, max_lines: int) -> list:
    """글자 폭 근사로 줄바꿈(어절 우선) — 넘치면 마지막 줄 말줄임."""
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        cand = f"{cur} {w}" if cur else w
        if text_width(cand, size) <= max_w:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = w
        while text_width(cur, size) > max_w:          # 한 어절이 줄보다 길면 글자 단위로 자른다
            cut = len(cur)
            while cut > 1 and text_width(cur[:cut], size) > max_w:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = ellipsize(lines[-1] + "…", size, max_w) if not lines[-1].endswith("…") else lines[-1]
    return lines


def _header(o, title, latin, right_label=None, right_value=None, *, gold=False) -> None:
    """상태판 머리와 같은 모티프(사선 무늬·호박색 막대) — 명예의 전당은 금색."""
    accent, diag = ("url(#gold)", "url(#golddiag)") if gold else ("#d49a2c", "url(#diag)")
    o.append('<path d="M34 36 H1166 V142 H34 Z" fill="#0b0f12" stroke="#72797f" stroke-width="1.8"/>')
    o.append(f'<path d="M34 36 H520 L488 142 H34 Z" fill="{diag}"/>')
    o.append(f'<path d="M34 36 H300 L270 142 H34 Z" fill="{"url(#gold)" if gold else "url(#amber)"}" opacity="0.22"/>')
    o.append(f'<path d="M48 48 H62 V130 H48 Z" fill="{accent}"/>')
    t, size = fit(title, 39, 560 if right_label else 1000, 26)
    o.append(_t(78, 82, t, "t h", size))
    o.append(_t(80, 112, latin, "t mono", 15, extra=' letter-spacing="5"'))
    if right_label:
        o.append('<line x1="860" y1="54" x2="860" y2="124" class="line2"/>')
        o.append(_t(888, 76, right_label, "t label"))
        o.append(_t(888, 112, ellipsize(right_value or "", 24, 250), "t value", 24))


def _chips(o, items, x0, x1, y, h=58, value_size=20, min_w=118.0) -> int:
    """[(라벨, 값)] 칩 줄 — 끝 y."""
    flow = [(max(min_w, text_width(k, 18) + 32, text_width(v, value_size) + 32), (k, v)) for k, v in items]
    rows = DB._flow(flow, x0, x1, 10)
    for r, row in enumerate(rows):
        ry = y + r * (h + 10)
        for x, w, (k, v) in row:
            o.append(f'<rect x="{x:g}" y="{ry}" width="{w:g}" height="{h}" class="chip"/>')
            o.append(_t(x + 14, ry + 23, ellipsize(k, 18, w - 28), "t label"))
            o.append(_t(x + 14, ry + h - 11, ellipsize(v, value_size, w - 28), "t value", value_size))
    return y + len(rows) * h + max(0, len(rows) - 1) * 10


def _rank_badge(o, x, y, rank, gold) -> None:
    fill, _c = MEDAL.get(rank, (None, None)) if gold else (None, None)
    if fill:
        o.append(f'<circle cx="{x}" cy="{y}" r="27" fill="{fill}" stroke="#0b0f12" stroke-width="3"/>')
        o.append(_t(x, y + 10, str(rank), "t h", 28, "middle", extra=' fill="#1a1205"'))
    else:
        o.append(f'<path d="M{x - 26} {y - 24} H{x + 18} L{x + 26} {y - 16} V{y + 24} H{x - 26} Z" class="slot"/>')
        o.append(_t(x, y + 9, str(rank), "t h", 25, "middle"))


def build_rank_svg(d: dict) -> bytes:
    """명예의 전당·서버보드·월드보드."""
    gold = d["kind"] == "hall"
    o: list = []
    _header(o, d["title"], d["latin"], d["basis"], d["subtitle"], gold=gold)
    y = 158
    if not d["rows"]:
        o.append(f'<path d="M{DB.X0} {y} H{DB.X1} V{y + 90} H{DB.X0} Z" class="box"/>')
        o.append(_t(600, y + 55, d["empty"], "t h dim", 24, "middle"))
        return DB.framed(o, y + 90 + 44, GOLD_DEFS if gold else DB.DEFS)
    for row in d["rows"]:
        body: list = []
        end = (_chips(body, row["items"], 70, 1130, y + 62, 58, 19, 96) if gold
               else _chips(body, row["items"], 380, 1130, y + 13, 58, 20))
        bottom = max(end, y + 68) + 16
        medal = MEDAL.get(row["rank"]) if gold else None
        stroke = medal[1] if medal else "#515960"
        o.append(f'<path d="M52 {y} H1146 L1134 {bottom} H64 L52 {bottom - 12} Z" fill="#101519" '
                 f'stroke="{stroke}" stroke-width="{2.2 if medal else 1.5}"/>')
        if medal:
            o.append(f'<rect x="52" y="{y}" width="300" height="50" fill="url(#golddiag)" opacity="0.6"/>')
        _rank_badge(o, 96, y + 40 if not gold else y + 32, row["rank"], gold)
        nx = 140
        nm, s = fit(row["name"], 27, (220 if not gold else 900), 18)
        o.append(_t(nx, (y + 49) if not gold else (y + 42), nm, "t h", s))
        o.extend(body)
        y = bottom + 12
    return DB.framed(o, y - 12 + 44, GOLD_DEFS if gold else DB.DEFS)


def build_home_svg(d: dict) -> bytes:
    o: list = []
    _header(o, d["title"], d["latin"], "봇 버전", d["version"])
    y = 158
    h = 70 + 38 * len(d["lines"]) + 40
    DB._section(o, y, h, "안내", [(110, "#ecece8"), (20, "#d49a2c")])
    for i, line in enumerate(d["lines"]):
        o.append(_t(52, y + 92 + i * 38, ellipsize(line, 23, 1090), "t value", 23))
    o.append(_t(52, y + h - 20, d["footer"], "t label"))
    return DB.framed(o, y + h + 30)


def build_personal_svg(d: dict) -> bytes:
    o: list = []
    _header(o, d["title"], d["latin"], "플레이어", d["name"])
    y = 158
    body: list = []
    end = _chips(body, d["items"], 52, 1148, y + 72, 64, 24)
    h = end - y + 26
    DB._section(o, y, h, "누적 기록", [(140, "#ecece8"), (20, "#d49a2c")])
    o.extend(body)
    return DB.framed(o, y + h + 30)


def build_frames_svg(d: dict) -> bytes:
    """시작 상황 삼지선다 — 번호(버튼과 같은 번호)·제목·요약."""
    o: list = []
    _header(o, d["title"], d["latin"], "선택", f"{len(d['options'])}가지 중 하나")
    y = 158
    for opt in d["options"]:
        lines = wrap(opt["summary"], 22, 900, 3)
        bottom = y + 74 + 34 * max(1, len(lines)) + 8
        o.append(f'<path d="M52 {y} H1146 L1134 {bottom} H64 L52 {bottom - 12} Z" fill="#101519" '
                 f'stroke="#515960" stroke-width="1.5"/>')
        o.append(f'<path d="M52 {y} H170 L156 {bottom} H52 Z" fill="url(#amber)" opacity="0.32"/>')
        o.append(f'<rect x="52" y="{y}" width="8" height="{bottom - y}" fill="#d49a2c"/>')
        o.append(_t(106, y + (bottom - y) / 2 + 20, str(opt["index"]), "t h amber", 56, "middle"))
        t, s = fit(opt["title"], 30, 920, 20)
        o.append(_t(196, y + 48, t, "t h", s))
        o.append(f'<line x1="196" y1="{y + 64}" x2="1120" y2="{y + 64}" class="line"/>')
        for i, line in enumerate(lines):
            o.append(_t(196, y + 98 + i * 34, line, "t value", 22))
        y = bottom + 14
    return DB.framed(o, y - 14 + 44)


# ══════════════════════════════════════════════════════════════
#  송출 — PNG 첨부(실패 시 None → 호출자 대체)
# ══════════════════════════════════════════════════════════════

async def png_file(svg: bytes, filename: str):
    """(discord.File, SVG 해시) — 렌더러·폰트 문제 등 실패면 (None, None)."""
    import discord
    try:
        png = await asyncio.to_thread(DB.render_png, svg)
    except Exception as e:  # noqa: BLE001 — 호출자가 기존 Embed/글로 대체
        from . import logs as _logs
        _logs.error("gm_board", f"카드 렌더 실패({filename}) — 기존 표시로 대체", exc=e)
        return None, None
    return discord.File(io.BytesIO(png), filename=filename), hashlib.sha256(svg).hexdigest()


def display_names(guild, member_ids) -> dict:
    """서버 표시 이름 {uid: 이름} — 이미지에는 멘션을 넣을 수 없다."""
    out = {}
    for uid in member_ids:
        m = guild.get_member(int(uid)) if guild is not None and str(uid).isdigit() else None
        if m is not None:
            out[str(uid)] = m.display_name
    return out
