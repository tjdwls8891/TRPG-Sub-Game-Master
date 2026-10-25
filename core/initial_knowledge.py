# 초기 지식 — 페르소나 기반 2단계(PROFILE·START) 전달 · 전송 증명 · genesis 인지 (v7.17)
#
# [권위 — 초기 지식 통합 설계안 v1.0 · 통합 기술계약]
#   · 후보 = 명시 정보(EXPLICIT) 중 initial_knowledge opt-in + summary + 시점 적격 + 코드가 평가한 접근 조건(승인 프로필
#     평면 항목 equals/one_of) + START 시작 틀 일치 + PROFILE 전달분 제외. 미표기 정보·사건 단서·인물 카드·NPC 비밀은
#     후보가 될 수 없다. 조건이 불명확하면 공개 쪽으로 추정하지 않는다(필드 없음 = 불허).
#   · required는 코드가 결정적으로 포함, optional만 AI가 K키로 고른다. 출력 키는 허용 집합과 대조(초과·중복·미지 거부).
#   · AI 호출은 시점별 최대 1회(비캐시, 운영자 부담 OP_INITIAL_KNOWLEDGE). 후보 0이면 0회. 전송 재시도는 PREPARED
#     패킷을 재사용한다(AI 재호출 없음).
#   · 정본 summary 원문은 코드가 렌더한다. AI 문장은 연결 설명일 뿐 정본·인지 근거가 아니다.
#   · 전달 기록은 세션 필드 initial_knowledge_progress(되감기 대상 아님 — 턴 정본이 아니다). 세계 인지는 genesis
#     INITIALIZATION에서만, DELIVERED 항목만(world_lifecycle이 인지 연산을 만든다 — SYSTEM_GRANTED, 전언은 RUMOR).
#     이 모듈은 전달 기록·후보만 다루고 세계 연산을 만들지 않는다. 믿음 불변.
#   · 상태: NOT_STARTED → PREPARED → SENDING → DELIVERED → (genesis) COMMITTED
#           └→ SKIPPED(후보 0·지원 불가)   PREPARED/SENDING → UNCERTAIN   PREPARED → FAILED_RETRYABLE
#     UNCERTAIN은 자동 재전송·자동 인지 커밋을 하지 않는다(nonce로 채널 기록을 찾아 영수증을 복구할 때만 DELIVERED).
from __future__ import annotations

import asyncio
import copy
import json
import secrets

import hashlib

FIELD = "initial_knowledge_progress"
VERSION = 1
PHASE_PROFILE, PHASE_START = "PROFILE", "START"
PHASES = (PHASE_PROFILE, PHASE_START)

NOT_STARTED = "NOT_STARTED"
PREPARED = "PREPARED"
SENDING = "SENDING"
PRESENTING = "PRESENTING"        # v7.37(D1): 대화 박자로 보여 주는 중 — 마지막 박자를 그리면 DELIVERED
DELIVERED = "DELIVERED"
UNCERTAIN = "UNCERTAIN"
SKIPPED = "SKIPPED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
COMMITTED = "COMMITTED"
DONE = (DELIVERED, SKIPPED, COMMITTED)            # 이 시점 처리가 끝나 다음 단계로 갈 수 있다
GENESIS_BLOCKING = (PREPARED, SENDING, PRESENTING, UNCERTAIN)  # 전달 여부를 모른다 — genesis 금지

OPTIONAL_BUDGET = 4          # 선택 항목 기본 예산(설계안 §3.2 — 작은 기본값)
BRIEFING_MAX = 800           # AI 연결 설명 상한(정본이 아니므로 표시 길이만 제한)
PAGE_MAX = 3800              # Discord 임베드 설명 한도(4096) 안의 페이지 크기 — 정본 문장은 자르지 않는다
HISTORY_SCAN = 100

_LOCKS: dict = {}


def _lock(session, uid, phase) -> asyncio.Lock:
    key = (getattr(session, "session_id", None), str(uid), phase)
    if key not in _LOCKS:
        _LOCKS[key] = asyncio.Lock()
    return _LOCKS[key]


# ══════════════════════════════════════════════════════════════
#  진행 기록
# ══════════════════════════════════════════════════════════════

def progress(session) -> dict:
    p = getattr(session, FIELD, None)
    if not isinstance(p, dict) or p.get("version") != VERSION:
        p = {"version": VERSION, "players": {}}
        setattr(session, FIELD, p)
    p.setdefault("players", {})
    return p


def record(session, uid, phase) -> dict:
    pl = progress(session)["players"].setdefault(str(uid), {})
    return pl.setdefault(phase, {"state": NOT_STARTED})


def state_of(session, uid, phase) -> str:
    p = getattr(session, FIELD, None) or {}
    return (((p.get("players") or {}).get(str(uid)) or {}).get(phase) or {}).get("state") or NOT_STARTED


def approved_profile(session, uid) -> dict:
    """승인된 평면 프로필(players[uid].profile) + 이름·외형. 초안(profile_creation_progress)은 읽지 않는다(T04)."""
    pl = (getattr(session, "players", None) or {}).get(str(uid)) or {}
    prof = {k: v for k, v in (pl.get("profile") or {}).items() if isinstance(v, (str, int, float))
            and not isinstance(v, bool)}
    if pl.get("name"):
        prof.setdefault("이름", pl["name"])
    if pl.get("appearance"):
        prof.setdefault("외형", pl["appearance"])
    return prof


def profile_digest(session, uid) -> str:
    raw = "ik.profile.v1\x00" + json.dumps(approved_profile(session, uid), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════
#  후보(코드 게이트 — 권위)
# ══════════════════════════════════════════════════════════════

def access_ok(node, profile: dict) -> bool:
    """승인 프로필의 명시 항목 비교만. 항목이 없거나 형식이 다르면 불허(공개 쪽으로 추정하지 않는다)."""
    if node is None:
        return True
    if not isinstance(node, dict):
        return False
    if "all" in node:
        return all(access_ok(x, profile) for x in node.get("all") or [None])
    if "any" in node:
        return any(access_ok(x, profile) for x in node.get("any") or ())
    f = node.get("field")
    if not isinstance(f, str) or f not in profile:
        return False
    val = str(profile[f]).strip()
    if node.get("op") == "equals":
        return val == str(node.get("value")).strip()
    if node.get("op") == "one_of":
        return val in {str(v).strip() for v in node.get("values") or ()}
    return False


def _ik(rec) -> dict | None:
    ik = (rec or {}).get("initial_knowledge")
    return ik if isinstance(ik, dict) else None


def eligible(definition: dict, iid: str, phase: str, profile: dict, *, frame_id=None) -> bool:
    rec = (definition.get("information_definitions") or {}).get(iid)
    ik = _ik(rec)
    if rec is None or ik is None or rec.get("kind") != "EXPLICIT" or not (rec.get("summary") or "").strip():
        return False
    if phase not in (ik.get("phases") or ()):
        return False
    if phase == PHASE_START and ik.get("start_frame_ids") and frame_id not in ik["start_frame_ids"]:
        return False
    return access_ok(ik.get("access"), profile)


def candidates(definition: dict, phase: str, profile: dict, *, frame_id=None, exclude=()) -> tuple:
    """(required rows, optional rows) — row = {id, summary, kind}. InfoId 순(결정적)."""
    req, opt = [], []
    excl = set(exclude or ())
    for iid in sorted(definition.get("information_definitions") or {}):
        if iid in excl or not eligible(definition, iid, phase, profile, frame_id=frame_id):
            continue
        rec = definition["information_definitions"][iid]
        ik = _ik(rec)
        row = {"id": iid, "summary": rec["summary"].strip(), "kind": ik.get("kind") or "fact"}
        (req if ik.get("tier") == "required" else opt).append(row)
    return req, opt


def delivered_rows(session, uid, phase) -> list:
    rec = ((getattr(session, FIELD, None) or {}).get("players") or {}).get(str(uid), {}).get(phase) or {}
    if rec.get("state") not in (DELIVERED, COMMITTED):
        return []
    return [dict(r) for r in rec.get("selected_info") or ()]


# ══════════════════════════════════════════════════════════════
#  AI 입력·출력(키 매니페스트 — 관찰 매니페스트와 같은 패턴)
# ══════════════════════════════════════════════════════════════

def build_prompt(phase, profile: dict, required, optional, recontext, frame_public: dict | None) -> str:
    """AI 입력 — 승인 프로필 · 공개 시작 상황(제목·요약) · 후보 summary뿐. 룰북·NPC 비밀·명제 원문·시작 틀 facts 없음."""
    lines = ["[승인된 캐릭터 프로필]"]
    lines += [f"· {k}: {v}" for k, v in sorted(profile.items())]
    if frame_public:
        lines += ["", "[공개 시작 상황]", f"· {frame_public.get('title') or ''}: {frame_public.get('summary') or ''}"]
    lines += ["", f"[시점] {'캐릭터 승인 직후' if phase == PHASE_PROFILE else '시작 상황 확정 직후'}"]

    def _row(key, r):
        return f"{key}: {'(전언) ' if r['kind'] == 'reported_claim' else ''}{r['summary']}"
    lines += ["", "[필수 — 코드가 이미 전달하기로 정함(고르지 말 것)]"]
    lines += [_row("-", r) for r in required] or ["(없음)"]
    lines += ["", f"[선택 후보 — 이 인물과 이어지는 것만, 최대 {OPTIONAL_BUDGET}개]"]
    lines += [_row(f"K{i}", r) for i, r in enumerate(optional, 1)] or ["(없음)"]
    if phase == PHASE_START:
        lines += ["", "[이미 알려 준 것 — 지금 상황과 이어 상기할 것만]"]
        lines += [_row(f"R{i}", r) for i, r in enumerate(recontext, 1)] or ["(없음)"]
    return "\n".join(lines)


def parse_response(raw, optional, recontext) -> tuple:
    """(선택 rows, 상기 rows, 연결 설명, 진단). 허용 밖·중복 키는 버리고 진단에 남긴다."""
    diags = []
    if not isinstance(raw, dict):
        return [], [], "", [{"code": "IK_RESPONSE_INVALID"}]

    def _pick(keys, rows, prefix, budget):
        out, seen = [], set()
        for k in keys if isinstance(keys, list) else []:
            if not isinstance(k, str) or not k.startswith(prefix) or not k[len(prefix):].isdigit():
                diags.append({"code": "IK_KEY_UNKNOWN", "key": k})
                continue
            i = int(k[len(prefix):])
            if not (1 <= i <= len(rows)):
                diags.append({"code": "IK_KEY_UNKNOWN", "key": k})
                continue
            if k in seen:
                diags.append({"code": "IK_KEY_DUPLICATE", "key": k})
                continue
            if budget is not None and len(out) >= budget:
                diags.append({"code": "IK_OPTIONAL_OVER_BUDGET", "key": k})
                continue
            seen.add(k)
            out.append(rows[i - 1])
        return out
    opt = _pick(raw.get("optional_keys"), optional, "K", OPTIONAL_BUDGET)
    rc = _pick(raw.get("recontextualized_keys"), recontext, "R", None)
    brief = raw.get("master_briefing")
    if not isinstance(brief, str):
        diags.append({"code": "IK_BRIEFING_INVALID"})
        brief = ""
    brief = brief.strip()
    if len(brief) > BRIEFING_MAX:
        brief = brief[:BRIEFING_MAX].rstrip() + "…"
    return opt, rc, brief, diags


def render_text(phase, briefing: str, rows, recontext) -> str:
    """사용자에게 보이는 원문 — 정본 summary는 코드 렌더 구역(AI 문장과 분리, 무절단). 전언은 '들은 말'로."""
    parts = []
    if briefing:
        parts.append(briefing)
    if rows:
        parts.append("**알고 있는 것**")
        for r in rows:
            parts.append(f"· 전해 들은 말 — {r['summary']}" if r["kind"] == "reported_claim" else f"· {r['summary']}")
    if recontext:
        parts.append("**다시 떠올린 것**")
        parts += [f"· {r['summary']}" for r in recontext]
    return "\n".join(parts)


def pages_of(text: str) -> list:
    """정본 문장을 자르지 않고 줄 단위로 페이지를 나눈다."""
    pages, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > PAGE_MAX:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pages.append(cur)
    return pages


TITLES = {PHASE_PROFILE: "📜 이 세계에 대해 알고 있는 것", PHASE_START: "📜 시작하며 떠올린 것"}


def _footer(nonce, i, n):
    return f"초기 지식 · {nonce}" + (f" · {i}/{n}" if n > 1 else "")


# ══════════════════════════════════════════════════════════════
#  준비 → 전송 → 증명(비동기 서비스)
# ══════════════════════════════════════════════════════════════

def _definition_revision(definition) -> str | None:
    from . import world_lifecycle as _wl            # 정의 정체성은 생애주기 owner가 계산한다
    return _wl.definition_revision_of(definition)


async def _strict_save(bot, session) -> bool:
    from . import io as _io
    try:
        await _io.save_session_data_strict(bot, session)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[초기지식] strict 저장 실패: {type(e).__name__}: {e}")
        return False


async def _call_ai(bot, session, prompt: str):
    from prompts import INITIAL_KNOWLEDGE_RESPONSE_SCHEMA, INITIAL_KNOWLEDGE_SYSTEM_INSTRUCTION
    from . import cost_ledger, profile_ai
    return await profile_ai._call(bot, INITIAL_KNOWLEDGE_SYSTEM_INSTRUCTION, INITIAL_KNOWLEDGE_RESPONSE_SCHEMA,
                                  prompt, session=session, temperature=0.3,
                                  operation=cost_ledger.OP_INITIAL_KNOWLEDGE,
                                  billing_hint=cost_ledger.HINT_OPERATOR, accrue=False)


async def prepare(bot, session, uid, phase, definition: dict, *, frame=None) -> str:
    """후보 계산·AI 선택(최대 1회)·패킷 strict 저장. 반환 = 새 상태(PREPARED·SKIPPED·FAILED_RETRYABLE)."""
    rec = record(session, uid, phase)
    profile = approved_profile(session, uid)
    frame_id = (frame or {}).get("id")
    exclude = [r["id"] for r in delivered_rows(session, uid, PHASE_PROFILE)] if phase == PHASE_START else []
    req, opt = candidates(definition, phase, profile, frame_id=frame_id, exclude=exclude)
    recontext = []
    if phase == PHASE_START:
        valid = {r["id"] for r in candidates(definition, PHASE_PROFILE, profile)[0]
                 + candidates(definition, PHASE_PROFILE, profile)[1]}
        # 상기 = 1차에 실제로 보여 준 원문(스냅샷) 중 지금도 유효한 것 — 재부여·신규 표시 아님
        recontext = [{"id": r["id"], "summary": r.get("summary_snapshot") or "", "kind": r.get("kind") or "fact"}
                     for r in delivered_rows(session, uid, PHASE_PROFILE) if r["id"] in valid]
    base = {"scenario_revision": _definition_revision(definition), "profile_digest": profile_digest(session, uid),
            "start_frame_id": frame_id}
    if not req and not opt:
        # 새로 전달할 적격 후보가 없으면 호출하지 않는다 — 상기(recontext)만을 위한 호출 없음(안정화 P1)
        rec.clear()
        rec.update(base, state=SKIPPED, reason="NO_CANDIDATES", selected_info=[], ai_calls=0)
        await _strict_save(bot, session)       # 생략은 안전 상태 — 저장 실패해도 정본 영향 없음
        return SKIPPED
    ok, raw = await _call_ai(bot, session, build_prompt(phase, profile, req, opt, recontext,
                                                         {"title": (frame or {}).get("title"),
                                                          "summary": (frame or {}).get("summary")}
                                                         if phase == PHASE_START else None))
    diags = []
    if ok:
        chosen, rc, brief, diags = parse_response(raw, opt, recontext)
    else:
        chosen, rc, brief = [], [], ""
        diags.append({"code": "IK_AI_FAILED"})
    rows = req + chosen
    if not rows and not rc:
        # 필수가 없고 선택도 못 골랐다 — 정보 없이 지어낸 설명을 내보내지 않는다(이유를 남긴 생략)
        rec.clear()
        rec.update(base, state=SKIPPED, reason="AI_FAILED" if not ok else "NONE_SELECTED", selected_info=[],
                   ai_calls=1, diagnostics=diags)
        await _strict_save(bot, session)
        return SKIPPED
    if not brief:
        from . import master
        brief = master.say(f"initial_knowledge.{phase.lower()}",
                           "당신이 이 세계에 대해 이미 알고 있는 것들입니다." if phase == PHASE_PROFILE
                           else "이야기를 시작하기 전에 떠올려 둘 것들입니다.")
    text = render_text(phase, brief, rows, rc)
    rec.clear()
    rec.update(base, state=PREPARED, selected_info=[{"id": r["id"], "summary_snapshot": r["summary"],
                                                     "kind": r["kind"]} for r in rows],
               recontextualized=[r["id"] for r in rc], rendered_text=text, title=TITLES[phase],
               pages=pages_of(text), delivery_nonce="ik-" + secrets.token_hex(6), message_ids=[],
               ai_calls=1, cost_recorded=bool(ok), diagnostics=diags)
    if not await _strict_save(bot, session):
        # 저장 못 한 패킷은 보내지 않는다. 메모리 패킷은 남겨 같은 프로세스의 재시도가 AI를 다시 부르지 않게 한다
        rec.update(state=FAILED_RETRYABLE, reason="PREPARE_SAVE_FAILED")
        return FAILED_RETRYABLE
    return PREPARED


async def find_receipt(bot, channel, nonce: str, n_pages: int):
    """채널 기록에서 nonce 영수증을 찾는다. (message_ids | None=조회 불가). 모든 페이지가 있어야 성공."""
    if channel is None or not nonce:
        return None
    found = {}
    try:
        me = getattr(bot, "user", None)
        async for m in channel.history(limit=HISTORY_SCAN):
            if me is not None and getattr(m, "author", None) is not None and m.author.id != me.id:
                continue
            texts = [ln[3:] for ln in str(getattr(m, "content", None) or "").splitlines() if ln.startswith("-# ")]
            texts += [getattr(getattr(e, "footer", None), "text", None) or ""     # v7.17 임베드 영수증 호환
                      for e in getattr(m, "embeds", None) or ()]
            for ft in texts:
                if nonce in ft:
                    idx = 1
                    tail = ft.rsplit("·", 1)[-1].strip()
                    if "/" in tail and tail.split("/")[0].strip().isdigit():
                        idx = int(tail.split("/")[0])
                    found[idx] = str(m.id)
    except Exception as e:  # noqa: BLE001
        print(f"[초기지식] 영수증 조회 실패: {type(e).__name__}: {e}")
        return None
    if len(found) == n_pages:
        return [found[i] for i in sorted(found)]
    return []


def talk_text(rec, phase) -> str:
    """v7.37(D1) 대화 박자 원문 — 머리말·항목 하나씩을 박자로, 마지막 박자에만 영수증 줄(nonce).

    정본 문장(summary)은 자르지 않는다. 항목 묶음 제목은 그 묶음 첫 항목 박자에 붙는다."""
    from . import master
    title = rec.get("title") or TITLES[phase]
    beats, head = [], None
    for ln in str(rec.get("rendered_text") or "").split("\n"):
        if not ln.strip():
            continue
        if ln.startswith("**") and ln.endswith("**"):
            head = ln
            continue
        beats.append(f"{head}\n{ln}" if head else ln)
        head = None
    if not beats:
        beats = [""]
    beats[0] = f"**{title}**\n{beats[0]}"
    done = master.say("knowledge.complete", "전해 드릴 것은 여기까지입니다.")
    beats[-1] = f"{beats[-1]}\n\n*{done}*\n-# {_footer(rec['delivery_nonce'], 1, 1)}"
    return "\n[[]]\n".join(beats)


async def present(bot, session, uid, phase, channel, on_delivered=None, later=None) -> str:
    """v7.37(D1, 사용자 결정): PREPARED 패킷을 메시지 하나의 대화 박자로 보여 준다(응답 버튼 — 무엇을 눌러도 다음).

    전달 증명 = 마지막 박자(영수증 줄 포함)를 실제로 그린 메시지 id. 그때 DELIVERED(strict)로 바꾸고 on_delivered를
    부른다. 재시작 등으로 도중에 끊기면 상태는 PRESENTING → 기존 영수증 조회(마지막 박자 nonce)로만 해소된다."""
    from . import conversational as _conv
    from . import session_flow as _sf
    from . import talk as _talk
    rec = record(session, uid, phase)
    rec["state"] = PRESENTING
    rec["pages"] = [rec.get("rendered_text") or ""]          # 영수증 = 마지막 박자 한 장
    if not await _strict_save(bot, session):
        rec["state"] = FAILED_RETRYABLE
        rec["reason"] = "SENDING_SAVE_FAILED"
        return FAILED_RETRYABLE
    key = f"ik:{phase}:{rec['delivery_nonce']}"
    holder = {"msg": None, "inside": True}

    async def draw(body, *, final, view, partial, first):
        import discord
        content = body
        try:
            if holder["msg"] is None:
                holder["msg"] = await channel.send(content=content, view=view)
            else:
                await holder["msg"].edit(content=content, view=view)
        except discord.HTTPException as e:
            print(f"[초기지식] 박자 전송 실패: {type(e).__name__}: {e}")
            if holder["msg"] is None:
                rec["state"], rec["reason"] = FAILED_RETRYABLE, "SEND_FAILED"
            else:
                rec["state"], rec["reason"] = UNCERTAIN, "SEND_PARTIAL_OR_UNKNOWN"
            await _strict_save(bot, session)
            return
        if final and not partial:
            rec["state"] = DELIVERED
            rec["message_ids"] = [str(getattr(holder["msg"], "id", ""))]
            rec.pop("reason", None)
            if not await _strict_save(bot, session):
                rec["state"], rec["reason"] = UNCERTAIN, "DELIVERED_SAVE_FAILED"
                return
            if on_delivered is None:
                return
            if holder["inside"] and later is not None:
                later.append(on_delivered)            # 잠금 안에서 바로 끝난 경우 — 잠금을 푼 뒤 이어 간다
            else:
                await on_delivered()

    _talk.reset(session, key)
    await _talk.run(session, key, talk_text(rec, phase), draw,
                    check=lambda i: _sf.owner_only(bot, session, i))
    holder["inside"] = False
    return rec.get("state") or PRESENTING


async def send(bot, session, uid, phase, channel) -> str:
    """PREPARED 패킷 전송 → DELIVERED(메시지 id strict 기록) · FAILED_RETRYABLE(미발송 확실) · UNCERTAIN."""
    import discord
    rec = record(session, uid, phase)
    rec["state"] = SENDING
    if not await _strict_save(bot, session):
        rec["state"] = FAILED_RETRYABLE               # SENDING을 기록하지 못했다 — 보내지 않는다
        rec["reason"] = "SENDING_SAVE_FAILED"
        return FAILED_RETRYABLE
    pages = rec.get("pages") or [rec.get("rendered_text") or ""]
    ids = []
    try:
        for i, page in enumerate(pages, 1):
            # 대화형 안내 = content(P2 #4). 영수증 줄(-# nonce)은 항상 content에 둔다 — 긴 페이지는 본문만 카드로.
            title, ft = rec.get("title") or TITLES[phase], _footer(rec["delivery_nonce"], i, len(pages))
            body = f"**{title}**\n{page}\n-# {ft}"
            if len(body) <= 1900:
                msg = await channel.send(content=body)
            else:
                msg = await channel.send(content=f"**{title}**\n-# {ft}",
                                         embed=discord.Embed(description=page, color=0x5865F2))
            ids.append(str(getattr(msg, "id", "")))
    except Exception as e:  # noqa: BLE001
        print(f"[초기지식] 전송 실패: {type(e).__name__}: {e}")
        if not ids and isinstance(e, discord.HTTPException):
            rec["state"] = FAILED_RETRYABLE           # 한 장도 나가지 않음이 확실 — 같은 패킷 재전송 가능
            rec["reason"] = "SEND_FAILED"
            await _strict_save(bot, session)
            return FAILED_RETRYABLE
        rec["state"] = UNCERTAIN
        rec["message_ids"] = ids
        rec["reason"] = "SEND_PARTIAL_OR_UNKNOWN"
        await _strict_save(bot, session)
        return UNCERTAIN
    rec["state"] = DELIVERED
    rec["message_ids"] = ids
    rec.pop("reason", None)
    if not await _strict_save(bot, session):
        rec["state"] = UNCERTAIN                      # 보냈으나 증명을 남기지 못함 — 영수증 복구 전 인지 금지
        rec["reason"] = "DELIVERED_SAVE_FAILED"
        return UNCERTAIN
    return DELIVERED


async def resolve_uncertain(bot, session, uid, phase, channel) -> str:
    """SENDING·UNCERTAIN을 영수증으로만 해소한다(자동 재전송 없음)."""
    rec = record(session, uid, phase)
    ids = await find_receipt(bot, channel, rec.get("delivery_nonce"), len(rec.get("pages") or [1]))
    if ids:
        rec["state"] = DELIVERED
        rec["message_ids"] = ids
        rec["reason"] = "RECEIPT_RECOVERED"
        if await _strict_save(bot, session):
            return DELIVERED
        rec["state"] = UNCERTAIN
        return UNCERTAIN
    rec["state"] = UNCERTAIN
    rec.setdefault("reason", "RECEIPT_NOT_FOUND" if ids == [] else "RECEIPT_UNAVAILABLE")
    await _strict_save(bot, session)
    return UNCERTAIN


def _stale(session, uid, phase, rec, definition, frame) -> bool:
    return (rec.get("profile_digest") != profile_digest(session, uid)
            or rec.get("scenario_revision") != _definition_revision(definition)
            or (phase == PHASE_START and rec.get("start_frame_id") != (frame or {}).get("id")))


async def run_phase(bot, session, uid, phase, channel, definition: dict, *, frame=None, on_delivered=None) -> str:
    """한 시점의 처리(재진입 멱등). 반환 상태가 DONE이면 다음 단계로 간다."""
    later = []
    st = await _run_phase_locked(bot, session, uid, phase, channel, definition, frame, on_delivered, later)
    for fn in later:
        await fn()
    return st


async def _run_phase_locked(bot, session, uid, phase, channel, definition, frame, on_delivered, later) -> str:
    async with _lock(session, uid, phase):
        st = state_of(session, uid, phase)
        if st in DONE:
            return st
        if st in (SENDING, PRESENTING, UNCERTAIN):
            return await resolve_uncertain(bot, session, uid, phase, channel)
        rec = record(session, uid, phase)
        if st in (PREPARED, FAILED_RETRYABLE) and rec.get("pages"):
            if _stale(session, uid, phase, rec, definition, frame):
                rec.clear()                            # 아직 보내지 않은 패킷이 낡았다 — 폐기하고 다시 준비
                rec["state"] = st = NOT_STARTED
            else:
                rec["state"] = st = PREPARED           # 저장된 패킷 재사용(AI 재호출 없음)
        if st != PREPARED:
            st = await prepare(bot, session, uid, phase, definition, frame=frame)
            if st != PREPARED:
                return st
        return await present(bot, session, uid, phase, channel, on_delivered, later)


async def resend(bot, session, uid, phase, channel, on_delivered=None) -> str:
    """UNCERTAIN의 명시적 재전송(소유자 선택 — 중복 안내가 생길 수 있음을 알린 뒤). 같은 패킷·AI 호출 없음."""
    later = []
    st = await _resend_locked(bot, session, uid, phase, channel, on_delivered, later)
    for fn in later:
        await fn()
    return st


async def _resend_locked(bot, session, uid, phase, channel, on_delivered, later) -> str:
    async with _lock(session, uid, phase):
        rec = record(session, uid, phase)
        if rec.get("state") != UNCERTAIN or not rec.get("pages"):
            return rec.get("state") or NOT_STARTED
        rec["state"] = PREPARED
        rec["resent"] = int(rec.get("resent") or 0) + 1
        rec["delivery_nonce"] = "ik-" + secrets.token_hex(6)        # 새 박자 메시지의 영수증(이전 메시지와 구별)
        return await present(bot, session, uid, phase, channel, on_delivered, later)


async def skip_uncertain(bot, session, uid, phase) -> str:
    """UNCERTAIN을 인지 없이 넘긴다(소유자 선택) — 정본 인지를 만들지 않는 안전 쪽."""
    async with _lock(session, uid, phase):
        rec = record(session, uid, phase)
        if rec.get("state") != UNCERTAIN:
            return rec.get("state") or NOT_STARTED
        rec["state"] = SKIPPED
        rec["reason"] = "OWNER_SKIPPED_UNCERTAIN"
        await _strict_save(bot, session)
        return SKIPPED


# ══════════════════════════════════════════════════════════════
#  genesis — DELIVERED 항목만 같은 INITIALIZATION 묶음에서 인지
# ══════════════════════════════════════════════════════════════

def acquisition_id(session_id, uid, phase, iid) -> str:
    return f"initial:{session_id}:{uid}:{phase}:{iid}"


def genesis_entries(definition: dict, session, uid, *, frame_id=None) -> tuple:
    """(entries, blocked_reason, excluded). entries = [(phase, iid, kind)].

    전달 여부를 모르는 시점(PREPARED·SENDING·UNCERTAIN)이 있으면 blocked. DELIVERED 항목은 현재 바인딩 정의로 다시
    검증한다(정보 존재·opt-in·시점·summary 스냅샷 일치·접근) — 불일치는 자동 부여하지 않고 excluded로 남긴다.
    """
    p = (getattr(session, FIELD, None) or {}).get("players") or {}
    mine = p.get(str(uid)) or {}
    for phase in PHASES:
        st = (mine.get(phase) or {}).get("state") or NOT_STARTED
        if st in GENESIS_BLOCKING:
            return [], f"initial_knowledge_{phase.lower()}_{st.lower()}", []
    profile = approved_profile(session, uid)
    entries, excluded, seen = [], [], set()
    for phase in PHASES:
        for r in delivered_rows(session, uid, phase):
            iid = r.get("id")
            rec = (definition.get("information_definitions") or {}).get(iid) or {}
            fid = frame_id if phase == PHASE_START else None
            if iid in seen:
                continue
            if not eligible(definition, iid, phase, profile, frame_id=fid) \
                    or (rec.get("summary") or "").strip() != (r.get("summary_snapshot") or "").strip():
                excluded.append({"phase": phase, "id": iid, "code": "IK_REVALIDATION_FAILED"})
                continue
            seen.add(iid)
            entries.append((phase, iid, r.get("kind") or "fact"))
    return entries, None, excluded


def mark_committed(session, uid, entries, excluded) -> None:
    """genesis와 같은 strict 저장에 들어간다(호출자가 실패 시 되돌린다)."""
    mine = progress(session)["players"].setdefault(str(uid), {})
    for phase in PHASES:
        rec = mine.get(phase)
        if isinstance(rec, dict) and rec.get("state") == DELIVERED:
            rec["state"] = COMMITTED
            rec["acquisition_keys"] = [acquisition_id(session.session_id, uid, ph, iid)
                                       for ph, iid, _k in entries if ph == phase]
            ex = [x for x in excluded if x["phase"] == phase]
            if ex:
                rec["excluded"] = ex


# ══════════════════════════════════════════════════════════════
#  재열람(저장된 원문 — 호출·인지·비용 0)
# ══════════════════════════════════════════════════════════════

def review_text(session, uid) -> str | None:
    mine = ((getattr(session, FIELD, None) or {}).get("players") or {}).get(str(uid)) or {}
    parts = []
    for phase in PHASES:
        rec = mine.get(phase) or {}
        if rec.get("state") in (DELIVERED, COMMITTED) and rec.get("rendered_text"):
            parts.append(f"**{rec.get('title') or TITLES[phase]}**\n{rec['rendered_text']}")
    return "\n\n".join(parts) if parts else None


def snapshot_copy(session) -> dict:
    return copy.deepcopy(getattr(session, FIELD, None) or {})
