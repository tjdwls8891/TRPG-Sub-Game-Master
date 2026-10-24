# 대화 박자 (v7.36.0, 사용자 결정: 메시지 하나를 고쳐 씀 · 표기 [[답1|답2]] · 소개문 자동 분할 · 소개 주제 분할)
#
# 마스터 안내를 박자로 나눈다. 결정이 아닌 박자 끝에는 응답 버튼(1~2개 — 무엇을 눌러도 같은 다음 박자)을 두고,
# 마지막 박자에만 그 화면의 기존 결정 UI가 붙는다. 응답은 결정이 아니다 — 정본·단계 상태를 바꾸지 않고 박자
# 번호만 넘긴다. 박자는 같은 메시지를 고쳐 쓰며 스트리밍한다(부분 편집 → 전체 + 버튼). provider 호출 없음.
#
# 저작 표기(줄 하나를 통째로 차지할 때만):
#   [[답1|답2]]   여기서 끊고 응답 버튼 두 개      [[답]]   버튼 하나      [[]]   공용 응답(대사 데이터 acks)
#   표기 뒤 마지막 글 = 결정 박자(그 화면의 결정 UI). 글이 표기로 끝나면 응답 뒤 빈 결정 박자(호출부가 다음
#   순서로 넘긴다). 표기가 없는 글은 한 박자 — 기존 동작 그대로.
from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass

import discord

MARK_RE = re.compile(r"^\s*\[\[(.*?)\]\]\s*$")
PREFIX = "talk:"
LABEL_MAX = 30
ANSWERS_MAX = 2
AUTO_SPLIT_CHARS = 300          # 표기 없는 긴 소개문의 자동 분할 기준(문단 경계)
DEFAULT_ANSWERS = ("네.",)


def _tick() -> float:
    try:
        return max(0.0, float(os.environ.get("TALK_TICK_SEC", "0.9")))
    except ValueError:
        return 0.9


TICK = _tick()                   # 스트리밍 부분 편집 간격(초) — 0이면 스트리밍 없이 한 번에
PARTIALS = 3                     # 박자당 부분 편집 수(디스코드 편집 한도 안)


@dataclass(frozen=True)
class Beat:
    text: str
    answers: tuple | None        # None = 마지막(결정) 박자 · () = 공용 응답


def has_marks(text) -> bool:
    return any(MARK_RE.match(ln) for ln in str(text or "").split("\n"))


def parse(text) -> list:
    """글 → 박자 목록. 표기만 있고 본문이 없는 박자는 버린다. 끝이 표기면 응답 뒤 빈 결정 박자를 둔다."""
    beats, buf = [], []
    for ln in str(text or "").split("\n"):
        m = MARK_RE.match(ln)
        if m is None:
            buf.append(ln)
            continue
        body = "\n".join(buf).strip()
        buf = []
        if body:
            beats.append(Beat(body, tuple(a.strip() for a in m.group(1).split("|") if a.strip())))
    beats.append(Beat("\n".join(buf).strip(), None))
    return beats


def plain(text) -> str:
    """표기를 걷은 본문(박자를 문단으로 잇는다) — 한 번에 보여 줄 곳·음성용."""
    return "\n\n".join(b.text for b in parse(text) if b.text)


def check(text) -> list:
    """저작 검사 — 오류 문구 목록(빈 목록 = 정상)."""
    errs = []
    for n, ln in enumerate(str(text or "").split("\n"), 1):
        s = ln.strip()
        m = MARK_RE.match(ln)
        if m is None:
            if s.startswith("[[") or (s.endswith("]]") and "[[" in s):
                errs.append(f"{n}줄: 박자 표기는 줄 하나를 통째로 차지해야 합니다 — {s[:40]}")
            continue
        answers = [a.strip() for a in m.group(1).split("|") if a.strip()]
        if len(answers) > ANSWERS_MAX:
            errs.append(f"{n}줄: 응답은 {ANSWERS_MAX}개까지 — {s[:40]}")
        for a in answers:
            if len(a) > LABEL_MAX:
                errs.append(f"{n}줄: 응답 문구는 {LABEL_MAX}자까지 — {a[:40]}")
    return errs


def auto_split(text, limit: int = AUTO_SPLIT_CHARS) -> str:
    """표기 없는 긴 글을 문단 경계에서 약 limit자 박자로 나눈다(공용 응답). 표기가 있으면 저작을 그대로 쓴다."""
    text = str(text or "").strip()
    if not text or has_marks(text):
        return text
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    groups, cur = [], ""
    for p in paras:
        cur = f"{cur}\n\n{p}" if cur else p
        if len(cur) >= limit:
            groups.append(cur)
            cur = ""
    if cur:
        groups.append(cur)
    return "\n[[]]\n".join(groups)


def answers_for(beat: Beat, key: str, salt: str = "") -> tuple:
    """박자 응답 — 저작 응답, 없으면 공용 목록에서 (박자 키, 세션)으로 결정적으로 고른다."""
    if beat.answers:
        return beat.answers[:ANSWERS_MAX]
    from . import master
    pool = master.acks() or [DEFAULT_ANSWERS]
    h = int(hashlib.sha256(f"{salt}|{key}|{beat.text[:40]}".encode("utf-8")).hexdigest()[:8], 16)
    return tuple(pool[h % len(pool)])[:ANSWERS_MAX]


def partials(text: str, n: int = PARTIALS) -> list:
    """스트리밍 부분 본문 — 낱말 경계에서 1/(n+1)씩 늘어난다(마지막 전체는 포함하지 않는다)."""
    out = []
    for k in range(1, n + 1):
        cut = len(text) * k // (n + 1)
        sp = text.find(" ", cut)
        nl = text.find("\n", cut)
        ends = [x for x in (sp, nl) if x != -1]
        cut = min(ends) if ends else len(text)
        if 0 < cut < len(text) and (not out or cut > len(out[-1])):
            out.append(text[:cut])
    return out


# ── 박자 위치(메모리) · 응답 기록 ──
# 위치는 저장하지 않는다. started(영속 dict)에 이 키를 시작했다는 표식만 남겨, 재시작 뒤에는 설명을 다시
# 들려주지 않고 결정 박자부터 그린다(v7.28 화면 승계와 같은 원칙).
_POS: dict = {}


def _sid(session) -> str:
    return str(getattr(session, "session_id", "") or id(session))


def position(session, key: str, n: int, started: dict | None = None) -> int:
    k = (_sid(session), key)
    if k not in _POS:
        _POS[k] = (n - 1) if (started is not None and started.get(key)) else 0
        if started is not None:
            started[key] = True
    return min(_POS[k], n - 1)


def reset(session, key: str, started: dict | None = None) -> None:
    _POS.pop((_sid(session), key), None)
    _ECHO.pop((_sid(session), key), None)
    _TEXT.pop((_sid(session), key), None)
    if started is not None:
        started.pop(key, None)


_ECHO: dict = {}
_TEXT: dict = {}                  # 박자 진행 중에는 처음 고른 대사 변주를 그대로 쓴다(변주가 박자마다 섞이지 않게)


def echo(session, key: str) -> str:
    """직전 응답 한 줄(같은 메시지 위쪽) — 대화가 이어진다는 표시."""
    e = _ECHO.get((_sid(session), key))
    return f"-# ↳ {e[0]} · {e[1]}\n" if e else ""


def answer(session, key: str, who: str, said: str) -> None:
    k = (_sid(session), key)
    _POS[k] = _POS.get(k, 0) + 1
    _ECHO[k] = (who, said)


class TalkView(discord.ui.View):
    """응답 버튼 — 무엇을 눌러도 같은 다음 박자. 결정·저장 없음."""

    def __init__(self, key: str, answers, on_answer, check=None):
        super().__init__(timeout=None)
        self.on_answer, self.check = on_answer, check
        for i, a in enumerate(answers):
            b = discord.ui.Button(label=a[:80], style=discord.ButtonStyle.secondary,
                                  custom_id=f"{PREFIX}{key}:{i}"[:100])
            b.callback = self._make(a)
            self.add_item(b)

    async def interaction_check(self, interaction) -> bool:
        return True if self.check is None else await self.check(interaction)

    def _make(self, said):
        async def cb(interaction):
            await self.on_answer(interaction, said)
        return cb


async def run(session, key: str, text: str, draw, *, check=None, started: dict | None = None, salt: str = ""):
    """현재 박자를 그린다. draw(body, *, final, view, partial, first) — final이면 호출부가 결정 UI를 붙인다.

    응답을 누르면 다음 박자로 같은 draw를 다시 부른다(같은 메시지 고쳐 쓰기는 draw 책임).
    """
    text = _TEXT.setdefault((_sid(session), key), text)
    beats = parse(text)
    i = position(session, key, len(beats), started)
    beat = beats[i]
    final = i >= len(beats) - 1
    head = echo(session, key)
    first = i == 0                    # 단계의 첫 박자 첫 그리기만 이미지(마스터 원화)를 바꾼다
    if TICK > 0:
        for part in partials(beat.text):
            await draw(head + part + " ▍", final=False, view=None, partial=True, first=first)
            first = False
            await asyncio.sleep(TICK)
    if final:
        await draw(head + beat.text, final=True, view=None, partial=False, first=first)
        return

    async def on_answer(interaction, said):
        try:
            await interaction.response.defer()
        except Exception:  # noqa: BLE001
            pass
        who = getattr(getattr(interaction, "user", None), "display_name", None) or "플레이어"
        answer(session, key, who, said)
        await run(session, key, text, draw, check=check, started=started, salt=salt)

    await draw(head + beat.text, final=False,
               view=TalkView(key, answers_for(beat, key, salt or _sid(session)), on_answer, check),
               partial=False, first=first)
