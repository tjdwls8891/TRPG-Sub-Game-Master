# 입력 구성 계측 — 실제 보낸 프롬프트를 블록 단위로 나눠 분류·키워드·토큰을 남긴다 (v7.30, 사용자 승인 계획 A·B)
#
# [분류]  다음 턴 비용 예측의 세 층과 같은 말이다.
#   확정(FIXED)    다음 턴에 반드시 들어가는 것 — 시스템 지시·스키마 안내·정본에서 정해지는 블록(장소·장면 인물·
#                  소지·사건 지침·PC 인지·추출 후보 등). 내용은 정본에 따라 바뀌어도 '들어간다'는 사실은 같다.
#   유력(LIKELY)   들어갈 확률이 높고 길이가 쌓이는 것 — 대화 이력·최근 로그·PROCEED 이력·압축 기억·노트.
#   가변(VARIABLE) 그 턴에서 생기는 것 — 플레이어 선언·지시층위 지시문·이번 묘사문·직전 추출 교정·전달 대기 신호.
# 프롬프트는 '[머리글]' 줄로 블록을 나눈다(각 층위 조립부의 기존 형식). 머리글 없는 앞부분은 '(머리)'다.
# 토큰은 그 호출의 실측 비캐시 입력을 글자 수 비율로 나눈다(근사 — 합은 실측과 같다). 새 provider 호출 없음.
from __future__ import annotations

import re

FIXED, LIKELY, VARIABLE = "확정", "유력", "가변"
CLASSES = (FIXED, LIKELY, VARIABLE)
HISTORY_CAP = 30                       # 층위별 보관 표본 수(세션 cost_stats["_census"])

_HEADER = re.compile(r"^\[([^\[\]\n]{1,200})\]", re.M)
# v7.34(C3): 머리글 첫 마디(short)만 보고, 낱말은 그 턴에 생기는 블록을 가리키는 구체적 표현만 쓴다
#   (설명 문구 속 '플레이어'·'지시문' 때문에 금지사항·사건 지침 같은 확정 블록이 가변으로 잡히던 오분류 수정).
_VARIABLE_KEYS = ("플레이어 신규", "플레이어 행동", "이번 턴 플레이어", "최근 플레이어", "선언", "이번 묘사문",
                  "묘사 지시", "GM 지시", "진행자", "proceed", "그사이", "전달 대기", "신호", "직전 추출", "교정", "사이드")
_LIKELY_KEYS = ("이력", "최근", "로그", "기억", "압축", "노트", "PROCEED", "히스토리", "대화")


def short(header: str) -> str:
    """표시용 블록 이름 — 머리글의 첫 마디(': ' · ' — ' 앞)만."""
    h = re.split(r"[:：]| — | - ", header or "", maxsplit=1)[0].strip()
    return h[:24] or "(머리)"


def classify(header: str) -> str:
    h = short(header or "")
    if any(k in h for k in _VARIABLE_KEYS):
        return VARIABLE
    if any(k in h for k in _LIKELY_KEYS):
        return LIKELY
    return FIXED


def _text_of(content) -> str:
    try:
        return "".join(getattr(p, "text", "") or "" for p in (content.parts or ()))
    except Exception:  # noqa: BLE001
        return str(content or "")


def known_names(session) -> list:
    """키워드로 찾을 인물 이름 — 저작 NPC·PC·비정규/세션 NPC(긴 이름 먼저: 부분 일치 오탐 방지)."""
    names = set()
    sd = getattr(session, "scenario_data", None) or {}
    names.update(k for k in (sd.get("default_npcs") or {}) if isinstance(k, str))
    for p in (getattr(session, "players", None) or {}).values():
        if isinstance(p, dict) and p.get("name"):
            names.add(p["name"])
    names.update(k for k in (getattr(session, "npcs", None) or {}) if isinstance(k, str))
    names.update(k for k in (getattr(session, "irregular_npcs", None) or {}) if isinstance(k, str))
    return sorted((n for n in names if len(n) >= 2), key=len, reverse=True)


def _keys(text: str, names) -> list:
    found = []
    for n in names:
        if n in text and not any(n in f for f in found):
            found.append(n)
    return found


def split_blocks(text: str) -> list:
    """[(머리글, 본문)] — 머리글 없는 앞부분은 '(머리)'."""
    text = text or ""
    ms = list(_HEADER.finditer(text))
    out = [("(머리)", text[:ms[0].start()] if ms else text)]
    for i, m in enumerate(ms):
        out.append((m.group(1).strip(), text[m.end():ms[i + 1].start() if i + 1 < len(ms) else len(text)]))
    return [(h, b) for h, b in out if h != "(머리)" or b.strip()]


def analyze(session, contents, *, system_text: str | None = None, fresh_tokens: int = 0) -> list:
    """한 호출의 비캐시 입력 구성 → [{"block", "cls", "chars", "tok", "keys"}].

    contents: 그 호출에 보낸 Content 목록(마지막 user 항목이 이번 프롬프트, 앞은 대화 이력).
    system_text: 캐시를 쓰지 않는 호출의 시스템 지시(캐시 호출은 캐시 안 — 넣지 않는다)."""
    names = known_names(session)
    rows = []
    if system_text:
        rows.append({"block": "시스템 지시", "cls": FIXED, "chars": len(system_text), "keys": []})
    items = list(contents or ())
    if isinstance(contents, str):
        items = []
        rows.append({"block": "(본문)", "cls": FIXED, "chars": len(contents), "keys": _keys(contents, names)})
    if len(items) > 1:
        hist = "".join(_text_of(c) for c in items[:-1])
        rows.append({"block": f"대화 이력 {len(items) - 1}건", "cls": LIKELY, "chars": len(hist),
                     "keys": _keys(hist, names)})
    if items:
        for head, body in split_blocks(_text_of(items[-1])):
            rows.append({"block": short(head), "cls": classify(head), "chars": len(body) + len(head) + 2,
                         "keys": _keys(body, names)})
    total = sum(r["chars"] for r in rows) or 1
    for r in rows:
        r["tok"] = int(round(fresh_tokens * r["chars"] / total)) if fresh_tokens else int(r["chars"] * 0.65)
    return rows


def totals(rows) -> dict:
    out = {c: 0 for c in CLASSES}
    for r in rows or ():
        out[r["cls"]] = out.get(r["cls"], 0) + int(r.get("tok") or 0)
    return out


def record(session, layer: str, contents, *, system_text=None, in_tokens=0, cached_tokens=0) -> list:
    """계측해 세션 통계(층위별 최근 HISTORY_CAP건)에 남기고 행을 돌려준다. 실패는 흡수(보고·예측 보조)."""
    try:
        rows = analyze(session, contents, system_text=system_text,
                       fresh_tokens=max(0, int(in_tokens or 0) - int(cached_tokens or 0)))
    except Exception as e:  # noqa: BLE001
        print(f"[계측] 입력 구성 분석 실패({layer}): {type(e).__name__}: {e}")
        return []
    st = getattr(session, "cost_stats", None)
    if not isinstance(st, dict):
        st = session.cost_stats = {}
    hist = st.setdefault("_census", {}).setdefault(layer, [])
    hist.append(totals(rows))
    del hist[:-HISTORY_CAP]
    return rows


def render(rows, *, limit_keys: int = 4) -> list:
    """사람이 읽는 줄 — 분류별로 '블록(키워드) ~토큰'."""
    lines = []
    for c in CLASSES:
        parts = []
        for r in rows or ():
            if r["cls"] != c:
                continue
            ks = r.get("keys") or []
            kw = f"({'·'.join(ks[:limit_keys])}{' 외 ' + str(len(ks) - limit_keys) if len(ks) > limit_keys else ''})" \
                if ks else ""
            parts.append(f"{r['block']}{kw} ~{_k(r.get('tok', 0))}")
        if parts:
            lines.append(f"[{c}] " + " · ".join(parts))
    return lines


def _k(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
