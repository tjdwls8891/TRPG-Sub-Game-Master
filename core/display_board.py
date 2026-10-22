# 디스플레이 상태판 — SVG 이미지(엔진 공통). 디스플레이 채널의 Embed를 대체한다.
#
# [권위 — 계획서 v1 · 사용자 결정 2026-10-12]
#   · 표시 정보는 build_embed(core/display.py)와 같은 출처·같은 규칙이다. 새 정보 항목을 만들지 않는다.
#   · 시각 기준은 사용자 제공 디스플레이 시안 v2. 시나리오 고유 흔적(제목·라틴 부제)은 시나리오 이름에서 뽑아 주입한다
#     — 이 모듈에는 특정 시나리오 이름·키가 없다.
#   · 가변 영역(플레이어·프로필·상태·소지·세션 상태·경고)은 코드가 배치하고 전체 높이를 계산한다.
#   · 래스터화는 speaker_card.rasterize(resvg)를 그대로 쓴다. 실패하면 호출자가 기존 Embed로 대체한다.
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from .constants import CACHE_TTL_SECONDS

W = 1200
KST = timezone(timedelta(hours=9), "KST")
PROFILE_SHORT_MAX = 20          # D1: 프로필 '짧은 항목' — 값이 이 길이 이하인 문자열·숫자
CHIP_ROWS_MAX = 3               # D7: 상태·소지 칩 최대 줄 수(넘치면 +N)
LATIN_SUFFIX = " / DISPLAY"
LATIN_KEY = "display_latin"     # 선택 시나리오 키(있으면 자동 로마자 표기보다 우선)

PLAN_LABEL = {"normal": "노멀", "high": "하이", "low": "로우", "ultra": "울트라"}
MODE_LABEL = {"quest": "퀘스트", "free": "풀자유"}
KIND_LABEL = {"master": "마스터", "multi": "멀티", "solo": "솔로"}
STATE_OPEN, STATE_EXPIRED, STATE_CLOSED = "open", "expired", "closed"
STATE_TEXT = {STATE_OPEN: "오픈", STATE_EXPIRED: "만료", STATE_CLOSED: "클로즈"}
STATE_DOT = {STATE_OPEN: "#41d78a", STATE_EXPIRED: "#ff4d55", STATE_CLOSED: "#8b9298"}
NONE_TEXT = "—"


# ══════════════════════════════════════════════════════════════
#  데이터 — build_embed와 같은 출처
# ══════════════════════════════════════════════════════════════

def session_state(session) -> str:
    from .cache import is_cache_expired
    expired = is_cache_expired(session)
    if not getattr(session, "cache_name", None) and getattr(session, "cache_expired_notified", False):
        expired = True
    if bool(getattr(session, "cache_name", None)) and not expired:
        return STATE_OPEN
    return STATE_EXPIRED if expired else STATE_CLOSED


def latin_name(session) -> str:
    """D2: 시나리오 선택 키 > 시나리오 이름의 로마자 표기."""
    from .koreantext import romanize
    v = (getattr(session, "scenario_data", None) or {}).get(LATIN_KEY)
    if isinstance(v, str) and v.strip():
        return v.strip().upper()
    return romanize(str(getattr(session, "scenario_id", "") or ""))


def short_profile_fields(profile, name: str) -> list:
    """D1: 값이 문자열·숫자이고 PROFILE_SHORT_MAX자 이하이며 플레이어 이름과 다른 항목(원래 순서)."""
    out = []
    if not isinstance(profile, dict):
        return out
    for k, v in profile.items():
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            continue
        s = str(v).strip()
        if not s or len(s) > PROFILE_SHORT_MAX or s == name:
            continue
        out.append((str(k), s))
    return out


def _open_lines(session) -> list:
    """세션 오픈 정보(build_embed '세션 오픈' 필드와 같은 값). 만료 시각은 KST 절대 시각(D4)."""
    from .estimate import estimate_session_open
    tokens = getattr(session, "cache_read_tokens", 0) or getattr(session, "cache_tokens", 0) or 0
    minutes = int(getattr(session, "open_minutes", 0) or 0)
    ttl = minutes * 60 if minutes else CACHE_TTL_SECONDS
    lines = [f"캐시 {tokens:,} 토큰"]
    created = getattr(session, "cache_created_at", 0.0) or 0.0
    if created:
        expire = created + ttl
        remain = max(0, int(expire - time.time()))
        hhmm = datetime.fromtimestamp(expire, KST).strftime("%H:%M")
        lines.append(f"만료 예정 {hhmm} (KST) · 남은 {remain // 3600}시간 {remain % 3600 // 60}분")
    try:
        prepaid = int(getattr(session, "open_prepaid_ink", 0) or 0)
        if prepaid:
            lines.append(f"선결제 {prepaid}잉크 ({ttl / 3600:.1f}시간)")
        else:
            lines.append(f"오픈·유지 예정 {estimate_session_open(session, ttl / 3600)['total_ink']}잉크")
    except Exception:
        pass
    return lines


def board_data(session) -> dict:
    """상태판에 그릴 값(문자열) — build_embed와 같은 출처를 같은 규칙으로 읽는다."""
    from . import media_control
    from .estimate import estimate_tts
    state = session_state(session)
    tl = getattr(session, "world_timeline", {}) or {}
    date_tod = f"{tl.get('current_date', '미확인')} · {tl.get('time_of_day', '') or '미확인'}"
    est = getattr(session, "last_estimate", {}) or {}
    last_ink = int(getattr(session, "last_turn_ink", 0) or 0)
    tts_line = None
    if est:
        try:
            t = estimate_tts(session)
            if t["enabled"] and t["max_ink"]:
                tts_line = f"+TTS {t['min_ink']}~{t['max_ink']}잉크"     # D5
        except Exception:
            pass
    flags = media_control.get_media_flags(session)
    onoff = lambda v: ("ON", "on") if v else ("OFF", "off")
    # P2(#9): 실효 = 설정 ∧ 실제 재생 가능(음성 채널 연결). 켜졌지만 미연결이면 '미연결'
    _tts_lbl = media_control.tts_status_label(session)
    from .voice_profile import GENDER_LABEL, master_gender
    media = [("이미지", *onoff(flags["image"])), ("TTS", *onoff(flags["tts"])), ("BGM", *onoff(flags["bgm"])),
             ("효과음", *onoff(flags["sfx"])), ("TTS 실효", _tts_lbl, "on" if _tts_lbl == "ON" else "off"),
             ("볼륨", f"{int((getattr(session, 'volume', 0.3) or 0) * 100)}%", ""),
             ("현재 BGM", getattr(session, "current_bgm", None) or "(없음)", ""),
             ("진행자", GENDER_LABEL[master_gender(session)], "")]
    players = []
    for _uid, p in (getattr(session, "players", {}) or {}).items():
        if not isinstance(p, dict):
            continue
        name = p.get("name") or "?"
        prof = p.get("profile")
        res = (getattr(session, "resources", {}) or {}).get(name) or {}
        players.append({
            "name": name,
            "fields": short_profile_fields(prof, name),
            "unassigned": not prof,
            "statuses": [str(s) for s in ((getattr(session, "statuses", {}) or {}).get(name) or [])],
            "resources": [f"{k} {v}" for k, v in res.items()],
        })
    ex = getattr(session, "last_extraction", {}) or {}
    sit = ex.get("situation") or {}
    qp = ex.get("quest_progress") or {}
    npcs = ex.get("npcs_met") or []
    narrative = [("장면", str(sit.get("tag", "(미확인)"))), ("긴장", str(sit.get("tension", 0))),
                 ("진행", str(qp.get("advance", 0))), ("이탈", str(qp.get("deviation", 0)))]
    if npcs:
        narrative.append(("등장 NPC", ", ".join(str(n) for n in npcs[:6])))
    try:
        from .quest import summary as quest_summary
        narrative.append(("퀘스트", quest_summary(session).replace("**", "")))
    except Exception:
        pass
    return {
        "title": str(getattr(session, "scenario_id", None) or "(시나리오 미정)"),
        "latin": latin_name(session),
        "kind": KIND_LABEL.get(getattr(session, "session_kind", "solo") or "solo", "솔로"),
        "privacy": "비공개" if getattr(session, "is_private", False) else "공개",
        "state": state,
        "session_id": str(getattr(session, "session_id", "?") or "?"),
        "turn": str(getattr(session, "turn_count", 0)),
        "date_tod": date_tod,
        "elapsed": f"{tl['elapsed_days']}일" if tl.get("elapsed_days") is not None else NONE_TEXT,
        "place": tl.get("current_location_display") or tl.get("current_location") or NONE_TEXT,
        "cost": {"total": f"{int(getattr(session, 'total_ink_spent', 0) or 0):,}잉크",
                 "last": f"{last_ink}잉크" if last_ink else NONE_TEXT,
                 "next": f"{est.get('min_ink', 0)}~{est.get('max_ink', 0)}잉크" if est else NONE_TEXT,
                 "tts": tts_line},
        "open_lines": _open_lines(session) if state == STATE_OPEN else [],
        "media": media,
        "players": players,
        "narrative": narrative,
        "memory": [("기억 방식", PLAN_LABEL.get(getattr(session, "memory_plan", "normal"), "노멀")),
                   ("압축", f"{len(getattr(session, 'compressed_memory', '') or '')}자"),
                   ("서사설계", MODE_LABEL.get(getattr(session, "narrative_mode", "quest"), "퀘스트"))],
        "warning": bool(getattr(session, "extraction_pending", False)),
    }


# ══════════════════════════════════════════════════════════════
#  글자 폭 근사 · 맞춤
# ══════════════════════════════════════════════════════════════

def text_width(text: str, size: float) -> float:
    """폰트 메트릭 없이 쓰는 근사 폭(px). 한글·전각 1.0em, 대문자 0.66em, 그 밖 ASCII 0.56em, 공백 0.3em."""
    w = 0.0
    for c in text or "":
        if c == " ":
            w += 0.3
        elif ord(c) >= 0x2E80:
            w += 1.0
        elif c.isupper():
            w += 0.66
        else:
            w += 0.56
    return w * size


def ellipsize(text: str, size: float, max_w: float) -> str:
    text = text or ""
    if text_width(text, size) <= max_w:
        return text
    while text and text_width(text + "…", size) > max_w:
        text = text[:-1]
    return text + "…"


def fit(text: str, size: float, max_w: float, min_size: float) -> tuple:
    """(글자, 크기) — 먼저 min_size까지 줄이고, 그래도 넘치면 말줄임."""
    s = size
    while s > min_size and text_width(text, s) > max_w:
        s -= 1
    return ellipsize(text, s, max_w), s


# ══════════════════════════════════════════════════════════════
#  SVG 조립 — 시각 기준: 디스플레이 시안 v2
# ══════════════════════════════════════════════════════════════

DEFS = """<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#111418"/><stop offset="0.45" stop-color="#0b0e11"/><stop offset="1" stop-color="#080a0c"/></linearGradient>
  <linearGradient id="metal" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f2f2ee"/><stop offset="0.18" stop-color="#8d9499"/><stop offset="0.34" stop-color="#e4e5e1"/><stop offset="0.58" stop-color="#666c70"/><stop offset="0.8" stop-color="#d9d9d4"/><stop offset="1" stop-color="#8b9195"/></linearGradient>
  <linearGradient id="amber" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#d69b2c"/><stop offset="0.45" stop-color="#9b6516"/><stop offset="1" stop-color="#4a2e0b"/></linearGradient>
  <linearGradient id="panel" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#151a1e"/><stop offset="1" stop-color="#0d1114"/></linearGradient>
  <linearGradient id="red" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#6e1519"/><stop offset="0.55" stop-color="#351012"/><stop offset="1" stop-color="#1a0b0c"/></linearGradient>
  <linearGradient id="green" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#0f3a26"/><stop offset="0.55" stop-color="#0d1f17"/><stop offset="1" stop-color="#0b100d"/></linearGradient>
  <pattern id="grain" width="14" height="14" patternUnits="userSpaceOnUse"><circle cx="2" cy="4" r="0.8" fill="#fff" opacity="0.025"/><circle cx="10" cy="9" r="0.6" fill="#fff" opacity="0.015"/></pattern>
  <pattern id="diag" width="24" height="24" patternUnits="userSpaceOnUse" patternTransform="rotate(32)"><rect width="9" height="24" fill="#c58a22" opacity="0.14"/></pattern>
  <pattern id="warnstripes" width="26" height="26" patternUnits="userSpaceOnUse" patternTransform="rotate(35)"><rect width="10" height="26" fill="#b92227" opacity="0.38"/></pattern>
  <style>
    .t{font-family:"Noto Sans CJK KR","Noto Sans KR",sans-serif;fill:#f1f2ef}
    .h{font-weight:800}
    .label{font-size:18px;fill:#9da4aa;font-weight:600}
    .value{fill:#f5f5f2;font-weight:700}
    .mono{font-family:monospace}
    .section{fill:#f3f3ef;font-size:25px;font-weight:800}
    .amber{fill:#d49a2c}
    .green{fill:#41d78a}
    .redtxt{fill:#ff555d}
    .dim{fill:#8b9298}
    .line{stroke:#596066;stroke-width:1.6}
    .line2{stroke:#b9bec2;stroke-width:1.3}
    .box{fill:url(#panel);stroke:#555d63;stroke-width:1.7}
    .slot{fill:#12171b;stroke:#444c52;stroke-width:1.4}
    .chip{fill:#101519;stroke:#51595f;stroke-width:1.4}
  </style>
</defs>"""

X0, X1 = 34, 1166          # 섹션 박스 좌우
GAP = 16                   # 섹션 사이 간격
TONE_CLASS = {"on": "t h green", "off": "t h redtxt", "": "t value"}


def _t(x, y, text, cls="t value", size=None, anchor=None, extra="") -> str:
    a = f' font-size="{size:g}"' if size else ""
    a += f' text-anchor="{anchor}"' if anchor else ""
    return f'<text x="{x:g}" y="{y:g}" class="{cls}"{a}{extra}>{escape(text)}</text>'


def _section(o, y, h, title, bars) -> None:
    """섹션 상자 + 제목 + 제목 옆 막대 + 구분선(시안 모티프). bars = [(폭, 색), ...]."""
    o.append(f'<path d="M{X0} {y} H{X1} V{y + h} H{X0} Z" fill="#0c1013" stroke="#666d73" stroke-width="1.7"/>')
    o.append(_t(52, y + 36, title, "section t"))
    bx = 52 + text_width(title, 25) + 24
    for bw, color in bars:
        o.append(f'<rect x="{bx:g}" y="{y + 23}" width="{bw}" height="6" fill="{color}"/>')
        bx += bw
    o.append(f'<line x1="52" y1="{y + 50}" x2="1148" y2="{y + 50}" class="line"/>')


def _flow(items, x0, x1, gap_x=12):
    """[(폭, 항목)] → [[(x, 폭, 항목), ...] 줄들]."""
    rows, row, x = [], [], x0
    for w, it in items:
        w = min(w, x1 - x0)
        if row and x + w > x1:
            rows.append(row)
            row, x = [], x0
        row.append((x, w, it))
        x += w + gap_x
    if row:
        rows.append(row)
    return rows


def _flow_capped(texts, width_of, x0, x1, max_rows, gap_x=12):
    """줄 수 상한 — 넘치면 뒤쪽 항목을 '+N' 칩으로 바꾼다(D7)."""
    n = len(texts)
    for keep in range(n, -1, -1):
        items = [(width_of(t), t) for t in texts[:keep]]
        if keep < n:
            more = f"+{n - keep}"
            items.append((width_of(more), more))
        rows = _flow(items, x0, x1, gap_x)
        if len(rows) <= max_rows:
            return rows
    return []


def _header(o, d) -> None:
    o.append('<path d="M34 36 H1166 V142 H34 Z" fill="#0b0f12" stroke="#72797f" stroke-width="1.8"/>')
    o.append('<path d="M34 36 H360 L328 142 H34 Z" fill="url(#diag)"/>')
    o.append('<path d="M34 36 H225 L195 142 H34 Z" fill="url(#amber)" opacity="0.22"/>')
    o.append('<path d="M48 48 H62 V130 H48 Z" fill="#d49a2c"/>')
    title, size = fit(d["title"], 39, 270, 26)
    o.append(_t(78, 82, title, "t h", size))
    latin = d["latin"] + LATIN_SUFFIX
    if len(latin) > 20:
        latin = d["latin"][:20 - len(LATIN_SUFFIX) - 1] + "…" + LATIN_SUFFIX
    o.append(_t(80, 112, latin, "t mono", 15, extra=' letter-spacing="5"'))
    o.append('<line x1="372" y1="54" x2="372" y2="124" class="line2"/>')
    o.append(_t(398, 76, "세션", "t label"))
    o.append(_t(398, 112, f"{d['kind']} · {d['privacy']}", "t value", 25))
    o.append('<line x1="690" y1="54" x2="690" y2="124" class="line2"/>')
    o.append(_t(716, 76, "상태", "t label"))
    o.append(f'<circle cx="724" cy="107" r="7" fill="{STATE_DOT[d["state"]]}"/>')
    o.append(_t(744, 114, STATE_TEXT[d["state"]], "t value", 25))
    o.append('<line x1="900" y1="54" x2="900" y2="124" class="line2"/>')
    o.append(_t(928, 76, "SESSION ID", "t label"))
    o.append(_t(928, 112, ellipsize(d["session_id"], 20, 220), "t mono", 20))


def _progress(o, y, d) -> int:
    h = 164
    _section(o, y, h, "진행", [(106, "#e9e9e4"), (20, "#777d82"), (20, "#d49a2c")])
    by = y + 72
    boxes = [(50, 236, "턴", d["turn"], 34), (298, 386, "날짜 · 시간대", d["date_tod"], 25),
             (696, 168, "경과", d["elapsed"], 25), (876, 284, "현재 장소", d["place"], 25)]
    for bx, bw, label, value, size in boxes:
        o.append(f'<path d="M{bx} {by} H{bx + bw - 12} L{bx + bw} {by + 12} V{by + 74} L{bx + bw - 12} {by + 86} '
                 f'H{bx} Z" class="box"/>')
        o.append(_t(bx + 18, by + 28, label, "t label"))
        txt, s = fit(value, size, bw - 40, 17)
        o.append(_t(bx + 18, by + 68, txt, "t value", s))
    return y + h


def _finance(o, y, d) -> int:
    h = 204
    _section_left = f'<path d="M{X0} {y} H586 V{y + h} H{X0} Z" fill="#0c1013" stroke="#666d73" stroke-width="1.7"/>'
    o.append(_section_left)
    o.append(_t(52, y + 38, "비용 (잉크)", "section t"))
    o.append(f'<rect x="190" y="{y + 25}" width="92" height="6" fill="#ecece8"/>')
    o.append(f'<rect x="282" y="{y + 25}" width="20" height="6" fill="#d49a2c"/>')
    o.append(f'<line x1="52" y1="{y + 53}" x2="564" y2="{y + 53}" class="line"/>')
    c = d["cost"]
    sy = y + 73
    for sx, sw, label, value in ((52, 150, "총 사용", c["total"]), (216, 150, "직전 턴", c["last"]),
                                 (380, 184, "다음 턴 예상", c["next"])):
        o.append(f'<rect x="{sx}" y="{sy}" width="{sw}" height="102" class="slot"/>')
        o.append(_t(sx + 18, sy + 30, label, "t label"))
        txt, s = fit(value, 25, sw - 30, 16)
        o.append(_t(sx + 18, sy + 72, txt, "t value", s))
    if c.get("tts"):
        o.append(_t(398, sy + 94, ellipsize(c["tts"], 15, 160), "t dim", 15))
    _session_panel(o, y, h, d)
    return y + h


def _session_panel(o, y, h, d) -> None:
    st = d["state"]
    x0 = 602
    if st == STATE_EXPIRED:
        o.append(f'<path d="M{x0} {y} H{X1} V{y + h} H{x0} Z" fill="#100b0c" stroke="#a3353a" stroke-width="1.9"/>')
        o.append(f'<rect x="610" y="{y + 8}" width="548" height="{h - 16}" fill="url(#red)" opacity="0.55"/>')
        o.append(f'<rect x="934" y="{y + 10}" width="214" height="28" fill="url(#warnstripes)" opacity="0.7"/>')
        o.append(_t(626, y + 38, "세션 상태", "section t"))
        o.append(f'<line x1="626" y1="{y + 53}" x2="1144" y2="{y + 53}" stroke="#8c3438" stroke-width="1.5"/>')
        o.append(f'<polygon points="646,{y + 84} 674,{y + 138} 618,{y + 138}" fill="#ef444b"/>')
        o.append(_t(646, y + 126, "!", "t h", 35, "middle"))
        o.append(_t(698, y + 100, "캐시 만료", "t h redtxt", 28))
        o.append(_t(698, y + 139, "유지 시간이 지나 캐시가 파기되었습니다.", "t value", 20))
        o.append(_t(698, y + 173, "세션 열기로 재시작 가능 · 업로드 비용 재발생", "t label"))
    elif st == STATE_OPEN:
        o.append(f'<path d="M{x0} {y} H{X1} V{y + h} H{x0} Z" fill="#0b100d" stroke="#2f8f5b" stroke-width="1.9"/>')
        o.append(f'<rect x="610" y="{y + 8}" width="548" height="{h - 16}" fill="url(#green)" opacity="0.55"/>')
        o.append(_t(626, y + 38, "세션 상태", "section t"))
        o.append(f'<line x1="626" y1="{y + 53}" x2="1144" y2="{y + 53}" stroke="#2c6b49" stroke-width="1.5"/>')
        o.append(f'<circle cx="646" cy="{y + 108}" r="26" fill="#2fbf74"/>')
        o.append(f'<polyline points="634,{y + 108} 643,{y + 118} 660,{y + 98}" fill="none" stroke="#0b100d" '
                 f'stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>')
        o.append(_t(698, y + 92, "세션 오픈", "t h green", 26))
        for i, line in enumerate(d["open_lines"][:3]):
            o.append(_t(698, y + 126 + i * 30, ellipsize(line, 19, 450), "t value" if i < 2 else "t label", 19))
    else:
        o.append(f'<path d="M{x0} {y} H{X1} V{y + h} H{x0} Z" class="box"/>')
        o.append(_t(626, y + 38, "세션 상태", "section t"))
        o.append(f'<line x1="626" y1="{y + 53}" x2="1144" y2="{y + 53}" class="line"/>')
        o.append(f'<circle cx="646" cy="{y + 120}" r="22" fill="none" stroke="#8b9298" stroke-width="4"/>')
        o.append(_t(698, y + 131, "클로즈", "t h dim", 28))


def _chip_rows(o, rows, top, h, gap_y, cls, tcls, size, pad):
    for r, row in enumerate(rows):
        ry = top + r * (h + gap_y)
        for x, w, txt in row:
            o.append(f'<rect x="{x:g}" y="{ry}" width="{w:g}" height="{h}" {cls}/>')
            o.append(_t(x + pad, ry + h / 2 + size * 0.36, ellipsize(txt, size, w - 2 * pad), tcls, size))
    return top + len(rows) * h + max(0, len(rows) - 1) * gap_y


def _label_value_chips(o, items, y, x0, x1, mins, h, label_y, value_y, value_size=20):
    """[(라벨, 값, 톤)] 칩 줄 — 최소 폭(시안 값) 이상, 넘치면 줄바꿈. 반환: 끝 y."""
    flow = []
    for i, (label, value, tone) in enumerate(items):
        w = max(mins[i] if i < len(mins) else 120, text_width(label, 18) + 36, text_width(value, value_size) + 36)
        flow.append((w, (label, value, tone)))
    rows = _flow(flow, x0, x1)
    for r, row in enumerate(rows):
        ry = y + r * (h + 12)
        for x, w, (label, value, tone) in row:
            o.append(f'<rect x="{x:g}" y="{ry}" width="{w:g}" height="{h}" class="chip"/>')
            o.append(_t(x + 14, ry + label_y, ellipsize(label, 18, w - 28), "t label"))
            o.append(_t(x + 14, ry + value_y, ellipsize(value, value_size, w - 28), TONE_CLASS[tone], value_size))
    return y + len(rows) * h + max(0, len(rows) - 1) * 12


def _media(o, y, d) -> int:
    body = []
    end = _label_value_chips(body, d["media"], y + 76, 50, 1158, [126, 112, 114, 132, 142, 112, 132, 154],
                             64, 26, 51)
    h = end - y + 26
    _section(o, y, h, "미디어", [(122, "#ecece8")])
    o.extend(body)
    return y + h


def _player_module(o, top, idx, p) -> int:
    body = []
    body.append(f'<path d="M52 {top} H1146 V{top + 50} H52 Z" fill="#171d21"/>')
    body.append(f'<rect x="52" y="{top}" width="250" height="50" fill="url(#diag)" opacity="0.45"/>')
    name, s = fit(p["name"], 30, 900, 22)
    body.append(_t(70, top + 34, name, "t h", s))
    body.append(_t(1050, top + 32, f"#{idx}", "t mono", 18, extra=' fill="#a6abb0"'))
    body.append(_t(70, top + 86, "프로필", "t h", 20))
    fields = p["fields"] or ([("프로필", "(미배분)")] if p["unassigned"] else [])
    gy = top + 108
    if fields:
        items = [(max(160.0, text_width(k, 18) + 36, text_width(v, 20) + 36), (k, v)) for k, v in fields]
        rows = _flow(items, 70, 1128)
        for r, row in enumerate(rows):
            ry = gy + r * 72
            for x, w, (k, v) in row:
                body.append(f'<rect x="{x:g}" y="{ry}" width="{w:g}" height="60" class="slot"/>')
                body.append(_t(x + 14, ry + 23, ellipsize(k, 18, w - 28), "t label"))
                body.append(_t(x + 14, ry + 52, ellipsize(v, 20, w - 28), "t value", 20))
        g_end = gy + len(rows) * 60 + (len(rows) - 1) * 12
    else:
        g_end = gy
    # 상태 칩(호박색)
    st_top = g_end + 12
    sts = p["statuses"] or ["없음"]
    rows = _flow_capped(sts, lambda t: max(114.0, text_width(t, 20) + 40), 126, 1128, CHIP_ROWS_MAX)
    body.append(_t(70, st_top + 24, "상태", "t h", 20))
    tone = ('rx="8" fill="#37270a" stroke="#d49a2c" stroke-width="1.6"' if p["statuses"]
            else 'rx="8" class="chip"')
    cur = st_top
    for r, row in enumerate(rows):
        ry = st_top + r * 50
        for x, w, txt in row:
            body.append(f'<rect x="{x:g}" y="{ry}" width="{w:g}" height="40" {tone}/>')
            body.append(_t(x + w / 2, ry + 27, ellipsize(txt, 20, w - 20),
                           "t h amber" if p["statuses"] else "t h dim", 20, "middle"))
        cur = ry + 40
    # 소지 칩
    if p["resources"]:
        r_top = cur + 12
        rows = _flow_capped(p["resources"], lambda t: max(120.0, text_width(t, 20) + 40), 126, 1128, CHIP_ROWS_MAX)
        body.append(_t(70, r_top + 26, "소지", "t h", 20))
        cur = _chip_rows(body, rows, r_top, 44, 10, 'class="chip"', "t value", 20, 16)
    bottom = cur + 22
    o.append(f'<path d="M52 {top} H1146 L1134 {bottom} H64 L52 {bottom - 12} Z" fill="#101519" stroke="#515960" '
             f'stroke-width="1.5"/>')
    o.extend(body)
    return bottom


def _players(o, y, d) -> int:
    body = []
    cur = y + 76
    for i, p in enumerate(d["players"], 1):
        cur = _player_module(body, cur, i, p) + GAP
    h = cur - GAP - y + 20
    _section(o, y, h, "플레이어", [(110, "#ecece8")])
    o.extend(body)
    return y + h


def _narrative(o, y, d) -> int:
    body = []
    items = [(label, value, "") for label, value in d["narrative"]]
    end = _label_value_chips(body, items, y + 76, 52, 1162, [154, 134, 134, 124, 188, 316], 58, 24, 49)
    h = end - y + 30
    _section(o, y, h, "서사", [(100, "#ecece8")])
    o.extend(body)
    return y + h


def _memory(o, y, d) -> int:
    h = 104
    o.append(f'<path d="M{X0} {y} H{X1} V{y + h} H{X0} Z" fill="#0c1013" stroke="#666d73" stroke-width="1.7"/>')
    o.append(_t(52, y + 40, "기억·서사", "section t"))
    o.append(f'<rect x="176" y="{y + 27}" width="104" height="6" fill="#ecece8"/>')
    for (label, value), (x, w) in zip(d["memory"], ((316, 230), (560, 230), (804, 314))):
        o.append(f'<rect x="{x}" y="{y + 18}" width="{w}" height="62" class="chip"/>')
        o.append(_t(x + 16, y + 43, label, "t label"))
        o.append(_t(x + 16, y + 71, ellipsize(value, 20, w - 32), "t value", 20))
    return y + h


def _warning(o, y) -> int:
    h = 44
    o.append(f'<path d="M{X0} {y} H{X1} V{y + h} H{X0} Z" fill="#360d10" stroke="#ae3036" stroke-width="1.7"/>')
    o.append(f'<rect x="{X0}" y="{y}" width="{X1 - X0}" height="{h}" fill="url(#warnstripes)"/>')
    o.append(f'<polygon points="58,{y + 8} 74,{y + 36} 42,{y + 36}" fill="#ff4c54"/>')
    o.append(_t(58, y + 33, "!", "t h", 22, "middle"))
    o.append(_t(92, y + 31, "이전 턴 정보 정리가 완료되지 않았습니다. 다음 턴이 차단됩니다.", "t h redtxt", 21))
    return y + h


def build_svg(d: dict) -> bytes:
    """board_data → SVG 바이트. 높이는 내용에 따라 계산한다(폭 1200 고정)."""
    body: list = []
    _header(body, d)
    y = _progress(body, 142 + GAP, d) + GAP
    y = _finance(body, y, d) + GAP
    y = _media(body, y, d) + GAP
    if d["players"]:
        y = _players(body, y, d) + GAP
    y = _narrative(body, y, d) + GAP
    y = _memory(body, y, d)
    if d["warning"]:
        y = _warning(body, y + GAP)
    return framed(body, y + 30)


def framed(body: list, H: int, defs: str = DEFS) -> bytes:
    """본문 조각 + 높이 → 금속 테두리 틀을 두른 SVG 바이트(상태판·GM 스페이스 카드 공용, 폭 1200 고정)."""
    frame = [
        f'<clipPath id="outerClip"><path d="M28 14 H1172 L1188 30 V{H - 30} L1172 {H - 14} H28 L12 {H - 30} V30 Z"/>'
        f'</clipPath>',
        '<g clip-path="url(#outerClip)">',
        f'<rect width="{W}" height="{H}" fill="#050708"/>',
        f'<rect x="12" y="14" width="1176" height="{H - 28}" fill="url(#bg)"/>',
        f'<rect x="12" y="14" width="1176" height="{H - 28}" fill="url(#grain)"/>',
        '</g>',
        f'<path d="M28 14 H1172 L1188 30 V{H - 30} L1172 {H - 14} H28 L12 {H - 30} V30 Z" fill="none" '
        f'stroke="url(#metal)" stroke-width="10"/>',
        f'<path d="M34 24 H1166 L1178 36 V{H - 36} L1166 {H - 24} H34 L22 {H - 36} V36 Z" fill="none" '
        f'stroke="#33393e" stroke-width="3"/>',
        '<path d="M46 35 H1154 L1166 47" fill="none" stroke="#d8d9d5" stroke-width="2"/>',
        f'<path d="M46 {H - 35} H1154 L1166 {H - 47}" fill="none" stroke="#d8d9d5" stroke-width="2"/>',
    ]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
           + defs + "".join(frame) + "".join(body) + "</svg>")
    return svg.encode("utf-8")


def render_png(svg: bytes) -> bytes:
    """동기 래스터화(스레드에서 호출). 렌더러 없음·실패는 speaker_card의 예외로 올린다."""
    from .speaker_card import rasterize
    return rasterize(svg)
