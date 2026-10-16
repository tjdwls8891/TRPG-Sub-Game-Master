# 프로필 생성 UI — 실행부를 구동하는 인터페이스
#
# [기획 목표]
#   "프로필 생성 시작부터 완성까지 질문에 대한 답과 제시된 다지선다 선택만 하면
#    시나리오별로 예정된 생성과정이 진행되도록 하는 것"
#
#   따라서 이 모듈은 무엇을 물을지 모른다. profile_runner가 반환하는
#   행동 유형(ASK/CONFIRM/WARN/AUTO/DONE)에 따라 화면만 그린다.
#
# [자유도와 통제]
#   재선택·취소·단계회귀를 제공하되, 실행부가 허용한 행동만 노출한다.
#   버튼으로 가능한 것만 보여주므로 허가되지 않은 조작이 불가능하다.
#
# [v7.15 캐릭터 생성 개선 — 기획 C1~C7·Q1~Q9]
#   · 텍스트 답변은 모달이 아니라 **일반 채팅**으로 받는다. 지금 화면이 텍스트를 기다릴 때(run["await"])
#     그 채널·그 플레이어의 메시지 1건만 소비한다(on_chat). 조작(이전·수정·취소)은 버튼으로만 한다.
#   · 장문 필드(profile_gen.is_long_form)는 [직접 작성]/[질문형 가이드] → 초안 검토(승인·부분 수정·질문
#     다시 고르기). 가이드 답이 여럿이면 기존 profile_ai.merge 한 번으로 조합한다(답변마다 호출하지 않는다).
#   · 시나리오 단계가 끝나면 바로 반영하지 않고 **전체 최종 검토**를 거친다. finish(정본 반영)는 최종 승인
#     뒤 한 번뿐이다. 저장 프로필을 불러와도 최종 검토로 들어가 고칠 필드만 고친다.
#   · 게임 세션 안의 진행(run 전체 — 답변·초안·커서)은 세션 필드 profile_creation_progress에 영속하고
#     봇 재시작 뒤 resume_all이 이어 그린다. 화면마다 세대(run["gen"])를 올려 낡은 버튼·답변을 무시한다.
import asyncio
import copy
import time

import discord

from . import master
from . import profile_gen
from . import profile_runner as runner
from . import profiles as profile_store

# 선택지가 이보다 많으면 셀렉트 메뉴를 쓴다.
SELECT_THRESHOLD = 5

# 디스코드 셀렉트 옵션 상한.
MAX_OPTIONS = 25

# 채팅 답변 한 건의 상한(기존 모달 계약과 같다).
MAX_TEXT = 1000

# 세션 필드 — {uid: {"version", "uid", "char_name", "channel_id", "scenario_id", "run"}}
PROGRESS_FIELD = "profile_creation_progress"
PROGRESS_VERSION = 1

# 진행 단계(run["phase"]) — 저장·복구용 표식
P_STEP, P_TEXT, P_MODE, P_DIRECT, P_GUIDE, P_DRAFT, P_EDIT = (
    "STEP", "TEXT", "MODE", "DIRECT", "GUIDE", "DRAFT", "EDIT")
P_PROCESSING, P_FINAL, P_SAVE = "PROCESSING", "FINAL", "SAVE"

CHAT_HINT = "💬 이 채널에 **채팅으로** 답해 주십시오. (조작은 아래 버튼으로)"

# 진행 중인 생성 — (channel_id, uid) → ProfileCreationSession. 플레이어별로 분리된다.
_ACTIVE: dict = {}


class ProfileCreationSession:
    """생성 진행 상태. 세션당 플레이어 한 명의 캐릭터 하나를 만든다."""

    def __init__(self, bot, session, uid: str, char_name: str, channel_id=None, run=None):
        self.bot = bot
        self.session = session
        self.uid = str(uid)
        self.char_name = char_name
        self.channel_id = channel_id
        self.run = run if run is not None else runner.new_run(session.scenario_data)
        self.run.setdefault("gen", 0)
        # 화면은 메시지 하나를 고쳐 쓴다. 단계마다 새로 보내면 지난
        # 인터페이스가 남아 눌리고, 위아래로 스크롤해야 한다.
        self.message = None
        # AUTO 단계에서 코드가 정한 결과를 모아 화면에 함께 보여준다.
        self.notes = []
        # 버튼·채팅·AI 완료의 경합을 막는다(LLM 호출 동안에는 잡지 않는다 — 세대로 막는다).
        self.lock = asyncio.Lock()


# ══════════════════════════════════════════════════════════════
#  영속 · 등록
# ══════════════════════════════════════════════════════════════

def _persistent(session) -> bool:
    """게임 세션(영속 필드가 있는 TRPGSession)인가. DM 사전 생성 대역은 영속하지 않는다."""
    return isinstance(getattr(session, PROGRESS_FIELD, None), dict)


async def _persist(state) -> bool:
    """진행 전체를 세션에 적고 strict 저장한다. 실패를 성공으로 가장하지 않는다(False + 안내 노트)."""
    s = state.session
    if not _persistent(s):
        return True
    prog = dict(getattr(s, PROGRESS_FIELD) or {})
    prog[state.uid] = {"version": PROGRESS_VERSION, "uid": state.uid, "char_name": state.char_name,
                       "channel_id": state.channel_id, "scenario_id": getattr(s, "scenario_id", None),
                       "run": copy.deepcopy(state.run)}
    setattr(s, PROGRESS_FIELD, prog)
    try:
        from .io import save_session_data_strict
        await save_session_data_strict(state.bot, s)
        return True
    except Exception as e:
        print(f"[프로필 생성] 진행 저장 실패: {e}")
        state.notes.append("⚠️ 진행 상황을 저장하지 못했습니다. 봇이 재시작되면 이 단계부터 다시 하셔야 할 수 있습니다.")
        return False


def _register(state):
    if state.channel_id is not None:
        _ACTIVE[(state.channel_id, state.uid)] = state


async def _complete(state):
    """생성 종료(저장 여부 선택 뒤) — 진행 기록·등록 해제. 그 뒤로는 일반 채팅이 다시 턴 맥락이 된다."""
    _ACTIVE.pop((state.channel_id, state.uid), None)
    s = state.session
    if _persistent(s) and state.uid in (getattr(s, PROGRESS_FIELD) or {}):
        prog = dict(getattr(s, PROGRESS_FIELD) or {})
        prog.pop(state.uid, None)
        setattr(s, PROGRESS_FIELD, prog)
        # v7.17 안정화 P3: 세션 플로우의 프로필 단계라면 '승인·저장 선택 완료'를 같은 strict 저장에 남긴다 —
        #   다음 단계(on_profile_done) 전에 재시작돼도 resume_all이 확정 프로필로 이어 간다(질문 반복 없음).
        st = getattr(s, "creation_state", None)
        if isinstance(st, dict) and st.get("step") == "profile":
            st.setdefault("data", {})["profile"] = "완료"
        try:
            from .io import save_session_data_strict
            await save_session_data_strict(state.bot, s)
        except Exception as e:
            print(f"[프로필 생성] 완료 기록 저장 실패: {e}")


def release_session(session) -> int:
    """세션 관리(종료·제거): 그 세션의 생성 화면 등록을 내린다 — 이후 채팅·버튼이 생성 경로로 오지 않는다.

    진행 중인 AI 작업은 강제로 끊지 않는다. 끝난 뒤의 저장은 종료·삭제 상태가 막는다. 내린 수를 돌려준다.
    """
    gone = [k for k, st in list(_ACTIVE.items()) if getattr(st, "session", None) is session]
    for k in gone:
        _ACTIVE.pop(k, None)
    return len(gone)


def claims(bot, message) -> bool:
    """이 메시지가 진행 중인 캐릭터 생성(그 채널·그 플레이어)에 속하는가 — 턴 맥락 기록·턴 처리 제외 판정.

    소비 여부(지금 텍스트를 기다리는지)와 별개로 넓게 판정한다. GMCog·GameCog가 같은 공유 상태로
    판정하므로 두 리스너의 호출 순서와 무관하다.
    """
    uid = str(getattr(getattr(message, "author", None), "id", ""))
    cid = getattr(getattr(message, "channel", None), "id", None)
    if (cid, uid) in _ACTIVE:
        return True
    session = (getattr(bot, "active_sessions", None) or {}).get(cid)
    p = (getattr(session, PROGRESS_FIELD, None) or {}).get(uid) if session is not None else None
    return isinstance(p, dict) and p.get("channel_id") == cid


# ══════════════════════════════════════════════════════════════
#  화면
# ══════════════════════════════════════════════════════════════

def _step_def(state) -> dict:
    return runner._step_at(state.session.scenario_data, state.run["index"]) or {}


def _mline(step, slot: str, default_key: str, *, slots=None) -> str | None:
    """마스터 대사 — 단계 저작 오버라이드(args.master_keys) → 공용 키 → None(호출부 기존 문구)."""
    mk = ((step or {}).get("args") or {}).get(profile_gen.MASTER_KEYS_ARG)
    key = mk.get(slot) if isinstance(mk, dict) else None
    return (master.line(key, slots=slots) if isinstance(key, str) else None) or master.line(default_key, slots=slots)


async def render(state: ProfileCreationSession, channel, *, user_input=None):
    """실행부를 한 단계 진행하고 화면을 갱신한다(호출자가 state.lock을 쥔다).

    AUTO는 화면 없이 다음 단계로 이어지므로 반복 처리한다.
    """
    sd = state.session.scenario_data
    run = state.run
    if run.get("finalized"):
        await _show_save(state, channel)
        return

    for _ in range(20):   # 자동 단계가 연속될 수 있다. 무한 루프 방지.
        res = runner.step(sd, run, user_input)
        user_input = None

        if res["type"] == runner.AUTO:
            # 자동 확정은 따로 보내지 않고 다음 화면에 함께 싣는다.
            if res.get("message"):
                state.notes.append(res["message"])
            continue

        if res["type"] == runner.DONE:
            await _show_final(state, channel)
            return

        if res["type"] == runner.ERROR:
            run["phase"] = P_STEP
            await _send(state, channel,
                        {"message": f"⚠️ {res['message']}",
                         "guide": "이전 단계로 돌아가 다시 골라 주세요."},
                        view=BackOnlyView(state, channel))
            return

        step = _step_def(state)
        if res.get("ai_module") or (res["type"] == runner.ASK and not res.get("options")):
            if profile_gen.is_long_form(step):
                await _show_long(state, channel, res, step)
                return
            pend = run.get("pending")
            if pend and pend.get("field") == res.get("field"):
                # 검증을 통과한 값의 확인 화면(재시작 뒤 복원 포함)
                run["phase"] = P_STEP
                await _send(state, channel, {"field": res.get("field"), "message": f"**{pend['value']}**",
                                             "can_back": True}, view=ConfirmView(state, channel, res))
                return
            run["phase"] = P_TEXT
            await _send(state, channel, res, view=TextWaitView(state, channel, res), await_kind="text")
            return

        run["phase"] = P_STEP
        if res.get("stat_module"):
            await _send(state, channel, res,
                        view=StatRollView(state, channel, res))
            return

        if res["type"] in (runner.CONFIRM, runner.WARN):
            await _send(state, channel, res,
                        view=ConfirmView(state, channel, res))
            return

        # ASK — 선택지
        await _send(state, channel, res, view=ChoiceView(state, channel, res))
        return


_NAME_PENDING = "(이름 미정)"


def _title(state) -> str:
    """단계 제목 — 이름이 정해졌으면 그 이름, 아직이면 '캐릭터 만들기'(v7.13: '(이름 미정) 만들기' 고정 표시 정정)."""
    name = str((runner.result(state.run) or {}).get("이름") or "").strip() if state.run else ""
    if not name and state.char_name and state.char_name != _NAME_PENDING:
        name = state.char_name
    return f"{name} 만들기" if name else "캐릭터 만들기"


def _master_head(state, res: dict) -> str | None:
    """v7.13 마스터 대사 — 단계 종류별 고정 안내 한 줄(단계 정보·선택지·AI 사유는 그대로)."""
    if "head" in res:
        head = res["head"]
    elif res.get("stat_module"):
        head = master.line("profile.stat_roll")
    elif res.get("await_text"):
        head = _mline(_step_def(state), "text_prompt", "profile.ai_input")
    elif res.get("type") == runner.WARN:
        head = master.line("profile.choice_warning")
    elif res.get("type") == runner.CONFIRM:
        head = master.line("profile.choice_confirm")
    else:
        head = None
    if not state.run.get("begun"):
        state.run["begun"] = True
        begin = master.line("profile.begin")
        head = f"{begin}\n{head}" if (begin and head) else (begin or head)
    return head


def _message(res: dict) -> str:
    """선택형 질문은 마스터 대사(항목명 슬롯·조사 보정)로, 그 밖은 원래 문구."""
    msg = res.get("message") or ""
    field = res.get("field")
    if res.get("type") == runner.ASK and res.get("options") and field and msg == f"{field}을(를) 선택해 주십시오.":
        return master.say("profile.field_prompt", msg, slots={"field_label": str(field)})
    return msg


def _embed(state, res: dict) -> discord.Embed:
    """단계 임베드. 진행도는 푸터 한 곳에만 둔다."""
    msg = _message(res)
    if res.get("type") == runner.ERROR:
        msg = master.lead("profile.error", msg)
    else:
        head = _master_head(state, res)
        if head:
            msg = f"*{head}*\n\n{msg}" if msg else f"*{head}*"
    if res.get("await_text"):
        msg = f"{msg}\n\n{CHAT_HINT}" if msg else CHAT_HINT
    e = discord.Embed(
        title=_title(state),
        description=msg[:4000],
        color=0x5865F2,
    )
    if res.get("guide"):
        e.add_field(name="​", value=f"💡 {res['guide']}"[:1024], inline=False)

    # 직전에 코드가 자동으로 정한 것들을 함께 보여준다.
    if state.notes:
        e.add_field(name="알림", value="\n".join(f"· {n}" for n in state.notes)[:1024],
                    inline=False)
        state.notes = []

    for name, value in res.get("extra_fields") or ():
        e.add_field(name=name, value=str(value)[:1024] or "​", inline=False)

    if res.get("options") and len(res["options"]) <= SELECT_THRESHOLD:
        e.add_field(name="​",
                    value="  ·  ".join(str(o) for o in res["options"]),
                    inline=False)

    e.set_footer(text=runner.progress(state.session.scenario_data, state.run))
    return e


async def _send(state, channel, res: dict, *, view, await_kind: str | None = None):
    """단계 화면을 그린다. 메시지 하나를 계속 고쳐 쓴다.

    화면마다 세대를 올린다 — 이전 화면의 버튼·대기 답변은 이후 무효다. 진행은 그리기 전에 영속한다.
    """
    run = state.run
    run["gen"] = int(run.get("gen") or 0) + 1
    if view is not None:
        view.gen = run["gen"]
        if run.get("review_old") is not None:
            # 최종 검토에서 들어온 필드 수정 — 이전 단계로 가지 않고 원래 값으로 돌아갈 수 있다
            for item in [c for c in view.children if isinstance(c, BackButton)]:
                view.remove_item(item)
            view.add_item(ReviewCancelButton())
    run["await"] = ({"kind": await_kind, "gen": run["gen"], "at": time.time()} if await_kind else None)
    if await_kind:
        res = dict(res, await_text=True)
    embed = _embed(state, res)
    await _persist(state)
    if state.message:
        try:
            await state.message.edit(embed=embed, view=view, content=None)
            return
        except Exception:
            state.message = None
    state.message = await channel.send(embed=embed, view=view)


# ══════════════════════════════════════════════════════════════
#  장문 필드 — 직접 작성 / 질문형 가이드 / 초안 검토
# ══════════════════════════════════════════════════════════════

def _lf(state, field: str) -> dict:
    lf = state.run.get("lf")
    if not isinstance(lf, dict) or lf.get("field") != field:
        lf = {"field": field, "mode": None, "answers": {}, "q": 0, "redo": False,
              "draft": None, "too_long": False, "blocked": None}
        state.run["lf"] = lf
    return lf


def _questions(step) -> list:
    g = profile_gen.creation_guide(step)
    return list((g or {}).get("questions") or [])


def _max_len(step) -> int | None:
    v = ((step or {}).get("args") or {}).get("max_length")
    return int(v) if isinstance(v, int) and v > 0 else None


async def _show_long(state, channel, res, step):
    field = res.get("field")
    g = profile_gen.creation_guide(step)
    lf = _lf(state, field)
    if lf.get("draft") is not None:
        await _show_draft(state, channel)
        return
    if g is None:
        lf["mode"] = "direct"
    elif lf.get("mode") is None:
        if g.get("direct", True):
            state.run["phase"] = P_MODE
            await _send(state, channel, {
                "field": field, "head": _mline(step, "mode_choice", "profile.mode_choice"),
                "message": (res.get("message") or f"{field}을(를) 정해 주십시오.")
                + "\n\n직접 적으시겠어요, 아니면 몇 가지 질문에 답하며 함께 정리하시겠어요?",
                "guide": res.get("guide")}, view=ModeView(state, channel, res))
            return
        lf["mode"] = "guide"
    if lf["mode"] == "direct":
        state.run["phase"] = P_DIRECT
        ml = _max_len(step)
        await _send(state, channel, {
            "field": field, "head": _mline(step, "text_prompt", "profile.ai_input"),
            "message": (res.get("message") or f"{field}을(를) 입력해 주십시오.")
            + (f"\n> {ml}자 이내" if ml else ""), "guide": res.get("guide")},
            view=LongWaitView(state, channel, res, guide=g is not None), await_kind="direct")
        return
    qs = _questions(step)
    if lf["q"] >= len(qs):
        # 모든 질문에 답했고 초안이 아직 없다(재시작 복원 등) — 조합은 버튼으로 다시 시작한다
        state.run["phase"] = P_GUIDE
        await _send(state, channel, {"field": field, "message": "답변을 모두 받았습니다. 초안을 만들까요?"},
                    view=ComposeView(state, channel))
        return
    q = qs[lf["q"]]
    state.run["phase"] = P_GUIDE
    cur = lf["answers"].get(q["id"])
    body = (f"**{q['prompt']}**" + ("" if q.get("required") else "  *(선택 — 건너뛰셔도 됩니다)*")
            + (f"\n> 지금 답: {cur}" if cur else ""))
    await _send(state, channel, {
        "field": field, "head": _mline(step, "guide_question", "profile.guide_question",
                                       slots={"field_label": str(field)}),
        "message": body, "options": q.get("options") or [],
        "extra_fields": [("진행", f"{lf['q'] + 1}/{len(qs)}")]},
        view=GuideView(state, channel, step, q), await_kind=None if q.get("options") else "guide")


async def _show_draft(state, channel):
    step = _step_def(state)
    lf = state.run["lf"]
    state.run["phase"] = P_DRAFT
    ml = _max_len(step)
    warn = []
    if lf.get("too_long") and ml:
        warn.append(f"⚠️ {ml}자 제한을 넘었습니다({len(lf['draft'])}자). [부분 수정]으로 줄여 주십시오. "
                    "잘라서 저장하지 않습니다.")
    if lf.get("blocked"):
        warn.append(lf["blocked"])
    await _send(state, channel, {
        "field": lf["field"], "head": _mline(step, "draft_review", "profile.draft_review"),
        "message": f"**{lf['field']} 초안**\n{lf['draft']}" + ("\n\n" + "\n".join(warn) if warn else "")},
        view=DraftView(state, channel, step))


async def _show_final(state, channel):
    """전체 최종 검토 — 여기서 승인해야 finish(정본 반영)가 한 번 실행된다."""
    run = state.run
    run["phase"] = P_FINAL
    run["review_old"] = None
    run["lf"] = None
    steps = profile_gen.get_steps(state.session.scenario_data)
    lines, bad = [], []
    for st in steps:
        f = st.get("field")
        v = run["chosen"].get(f)
        if f not in run["chosen"]:
            lines.append(f"**{f}** — (미정)")
            bad.append(f)
            continue
        if not runner.expected_type_ok(st, v):
            bad.append(f)
            lines.append(f"**{f}** — ⚠️ 저장된 값의 형식이 맞지 않습니다. 다시 정해 주십시오.")
            continue
        shown = (", ".join(f"{k} {x}" for k, x in v.items()) if isinstance(v, dict)
                 else ", ".join(map(str, v)) if isinstance(v, list) else str(v))
        lines.append(f"**{f}** — {shown[:300]}")
    run["final_invalid"] = bad
    await _send(state, channel, {
        "field": None, "head": master.line("profile.final_review"),
        "message": "\n".join(lines)[:3500]
        + ("\n\n고칠 항목을 고르시거나, 이대로 확정해 주십시오." if not bad else
           "\n\n⚠️ 표시된 항목을 먼저 정해 주십시오.")},
        view=FinalView(state, channel, [st.get("field") for st in steps if st.get("field")], bool(bad)))


# ══════════════════════════════════════════════════════════════
#  채팅 답변 수신
# ══════════════════════════════════════════════════════════════

async def on_chat(bot, message):
    """생성 중인 플레이어의 채팅 1건 — 지금 화면이 텍스트를 기다릴 때만 소비한다(그 밖은 무시).

    자연어를 조작 명령으로 해석하지 않는다("이전으로"도 답변 텍스트일 뿐이다).
    """
    state = _ACTIVE.get((message.channel.id, str(message.author.id)))
    if state is None:
        return
    job = None
    async with state.lock:
        run = state.run
        aw = run.get("await")
        if not aw or aw.get("gen") != run.get("gen"):
            return
        created = getattr(message, "created_at", None)
        if created is not None and hasattr(created, "timestamp") and created.timestamp() < aw.get("at", 0) - 1:
            return                      # 이 질문이 열리기 전에 쓴 메시지
        if message.id == run.get("last_msg_id"):
            return
        text = (message.content or "").strip()
        if not text:
            return
        if len(text) > MAX_TEXT:
            try:
                await message.channel.send(f"⚠️ 한 번에 {MAX_TEXT}자까지 받을 수 있습니다. 줄여서 다시 보내 주십시오.",
                                           delete_after=10)
            except Exception:
                pass
            return
        run["await"] = None                     # 슬롯을 먼저 닫는다 — 같은 질문의 두 번째 답은 받지 않는다
        run["last_msg_id"] = message.id
        job = await _on_text(state, message.channel, aw["kind"], text)
    if job:
        await job()


async def _begin_ai(state, channel, kind: str, raw: str, work, apply):
    """AI가 필요한 처리 — 처리 중 화면(버튼 없음, 세대 증가·영속) 뒤 락 밖에서 호출하고 같은 세대일 때만 반영."""
    state.run["phase"] = P_PROCESSING
    state.run["proc"] = {"kind": kind, "raw": raw}
    await _send(state, channel, {"head": None, "message": "⏳ 확인하는 중입니다…"}, view=None)
    my_gen = state.run["gen"]

    async def job():
        try:
            result = await work()
        except Exception as e:
            print(f"[프로필 생성] AI 처리 실패: {e}")
            result = None
        async with state.lock:
            if state.run.get("gen") != my_gen:
                return                          # 그 사이 화면이 바뀌었다 — 낡은 결과는 버린다
            state.run["proc"] = None
            await apply(result)
    return job


async def _on_text(state, channel, kind: str, text: str):
    sd = state.session.scenario_data
    run = state.run
    step = _step_def(state)
    field = step.get("field")
    args = step.get("args") or {}
    module = step.get("module")

    if kind == "text":
        if module != "ai_validate":
            await render(state, channel, user_input=text)
            return None

        async def work():
            from . import profile_ai
            return await profile_ai.validate(state.bot, state.session, field=field, value=text,
                                             scenario_data=sd, rules=args.get("rules") or "")

        async def apply(res):
            from . import profile_ai
            res = res or {"verdict": "ok", "reason": ""}
            msg = profile_ai.format_validation(field, res)
            if res["verdict"] == "reject":
                state.notes.append(master.lead("profile.ai_rejected", msg))
                await render(state, channel)
                return
            if msg:
                state.notes.append(master.lead("profile.ai_warned", msg))
            run["pending"] = {"field": field, "value": text}
            await render(state, channel)
        return await _begin_ai(state, channel, kind, text, work, apply)

    lf = _lf(state, field)
    if kind == "direct":
        if module != "ai_validate":
            _set_draft(state, step, text, blocked=None)
            await _show_draft(state, channel)
            return None

        async def work():
            from . import profile_ai
            return await profile_ai.validate(state.bot, state.session, field=field, value=text,
                                             scenario_data=sd, rules=args.get("rules") or "")

        async def apply(res):
            from . import profile_ai
            res = res or {"verdict": "ok", "reason": ""}
            msg = profile_ai.format_validation(field, res)
            if res["verdict"] == "reject":
                # 세계관 충돌 — 사유 안내 후 같은 항목 재입력(대안 제시 없음)
                state.notes.append(master.lead("profile.ai_rejected", msg))
                await render(state, channel)
                return
            if msg:
                state.notes.append(master.lead("profile.ai_warned", msg))
            _set_draft(state, step, text, blocked=None)
            await _show_draft(state, channel)
        return await _begin_ai(state, channel, kind, text, work, apply)

    if kind == "guide":
        qs = _questions(step)
        q = qs[lf["q"]] if lf["q"] < len(qs) else None
        if q is None:
            return None
        return await _answer_guide(state, channel, step, q, text)

    if kind == "edit":
        old = lf.get("draft") or ""
        rules = ("두 번째 항목은 첫 번째 초안에 대한 플레이어의 수정 요청이다. 수정 요청을 반영해 초안을 고쳐 쓰고, "
                 "요청이 언급하지 않은 부분은 그대로 둔다.")
        g = profile_gen.creation_guide(step) or {}
        if g.get("compose_rules"):
            rules += "\n" + g["compose_rules"]

        async def work():
            from . import profile_ai
            m = await profile_ai.merge(state.bot, state.session, field=field, values=[old, text],
                                       max_length=_max_len(step) or 1000, rules=rules)
            v = await _validate_long(state, step, m["merged"]) if not m.get("fallback") else None
            return m, v

        async def apply(result):
            if not result or result[0].get("fallback"):
                state.notes.append("⚠️ 수정 내용을 반영하지 못했습니다. 초안은 그대로입니다 — 다시 시도해 주십시오.")
                await _show_draft(state, channel)
                return
            m, v = result
            _set_draft(state, step, m["merged"], blocked=v)
            state.notes.append(master.say("profile.merged", "수정한 초안입니다."))
            await _show_draft(state, channel)
        return await _begin_ai(state, channel, kind, text, work, apply)
    return None


def _set_draft(state, step, text: str, *, blocked):
    lf = state.run["lf"]
    lf["draft"] = text
    ml = _max_len(step)
    lf["too_long"] = bool(ml and len(text) > ml)
    lf["blocked"] = blocked


async def _validate_long(state, step, text: str) -> str | None:
    """ai_validate 장문 필드의 조합 결과 검증 — reject면 사유(승인 차단), 아니면 None."""
    if step.get("module") != "ai_validate":
        return None
    from . import profile_ai
    res = await profile_ai.validate(state.bot, state.session, field=step.get("field"), value=text,
                                    scenario_data=state.session.scenario_data,
                                    rules=(step.get("args") or {}).get("rules") or "")
    if res.get("verdict") == "reject":
        return master.lead("profile.ai_rejected", profile_ai.format_validation(step.get("field"), res))
    return None


async def _answer_guide(state, channel, step, q, value):
    """보조 질문 답(None = 건너뛰기). 마지막이거나 다시 고른 질문이면 조합 작업을 돌려준다."""
    lf = state.run["lf"]
    if value is None:
        lf["answers"].pop(q["id"], None)
    else:
        lf["answers"][q["id"]] = value
    qs = _questions(step)
    if lf.get("redo"):
        lf["redo"] = False
        lf["q"] = len(qs)
    else:
        lf["q"] += 1
    if lf["q"] < len(qs):
        await render(state, channel)
        return None
    return await _compose(state, channel, step)


async def _compose(state, channel, step):
    """가이드 답변 → 초안. 필수 누락이면 그 질문으로 돌아간다. 답이 둘 이상일 때만 기존 merge 호출."""
    lf = state.run["lf"]
    qs = _questions(step)
    for i, q in enumerate(qs):
        if q.get("required") and not lf["answers"].get(q["id"]):
            lf["q"] = i
            state.notes.append("필수 질문에 답해 주셔야 초안을 만들 수 있습니다.")
            await render(state, channel)
            return None
    answered = [(q, lf["answers"][q["id"]]) for q in qs if lf["answers"].get(q["id"])]
    if not answered:
        lf["q"] = 0
        state.notes.append("답하신 내용이 없어 초안을 만들 수 없습니다.")
        await render(state, channel)
        return None
    field = step.get("field")
    g = profile_gen.creation_guide(step) or {}

    if len(answered) == 1:
        values = [answered[0][1]]
    else:
        values = [f"{q['prompt']} — {a}" for q, a in answered]

    async def work():
        from . import profile_ai
        m = await profile_ai.merge(state.bot, state.session, field=field, values=values,
                                   max_length=_max_len(step) or 1000, rules=g.get("compose_rules") or "")
        v = await _validate_long(state, step, m["merged"])
        return m, v

    async def apply(result):
        if not result:
            m = {"merged": "\n".join(a for _q, a in answered), "dropped": [], "fallback": True}
            v = None
        else:
            m, v = result
        _set_draft(state, step, m["merged"], blocked=v)
        if m.get("dropped"):
            state.notes.append("모순되어 빠진 답: " + ", ".join(m["dropped"]))
        if m.get("fallback"):
            state.notes.append("자동 정리에 실패해 답을 그대로 이어 붙였습니다. [부분 수정]으로 다듬어 주십시오.")
        else:
            state.notes.append(master.say("profile.merged", "초안을 정리했습니다."))
        await _show_draft(state, channel)
    return await _begin_ai(state, channel, "guide", "", work, apply)


# ══════════════════════════════════════════════════════════════
#  완료 · 저장
# ══════════════════════════════════════════════════════════════

async def finish(state: ProfileCreationSession, channel):
    """최종 승인된 값을 세션 플레이어에 반영하고 저장 여부를 묻는다(최종 승인 뒤 한 번만)."""
    result = runner.result(state.run)

    player = state.session.players.get(state.uid)
    if not isinstance(player, dict):
        player = {"name": state.char_name, "profile": {}, "appearance": ""}
        state.session.players[state.uid] = player

    profile = dict(player.get("profile") or {})
    for key, val in result.items():
        # 능력치는 하위 항목으로 펼쳐 pc_template 구조에 맞춘다.
        if isinstance(val, dict):
            profile.update({k: v for k, v in val.items()})
        elif isinstance(val, list):
            profile[key] = ", ".join(str(x) for x in val)
        else:
            profile[key] = val
    player["profile"] = profile
    if result.get("이름"):
        player["name"] = str(result["이름"])
        state.char_name = player["name"]
    if result.get("외형"):
        player["appearance"] = str(result["외형"])

    # 초기 소지품 — 공통 + 직업별 (석 달을 버틴 사람이 배로 건너온 규모)
    try:
        items = profile_gen.starting_items(
            state.session.scenario_data, result.get("직업"))
        if items:
            res = dict(getattr(state.session, "resources", {}) or {})
            bag = dict(res.get(state.char_name) or {})
            for it in items:
                bag[it] = bag.get(it, 0) + 1
            res[state.char_name] = bag
            state.session.resources = res
    except Exception as e:
        print(f"[프로필] 소지품 지급 실패: {e}")

    # 정본 반영과 '최종 승인됨' 표식을 한 번의 저장에 담는다 — 재시작 뒤 두 번 반영되지 않는다.
    state.run["finalized"] = True
    state.run["phase"] = P_SAVE
    if not _persistent(state.session):
        from .io import save_session_data
        await save_session_data(state.bot, state.session)
    await _show_save(state, channel)


def _completion_embed(state, result: dict) -> discord.Embed:
    profile = dict(((state.session.players or {}).get(state.uid) or {}).get("profile") or {})
    e = discord.Embed(
        title=f"{state.char_name}",
        description=(master.say("profile.completed", "캐릭터가 완성되었습니다.")
                     + ((" " + _sp) if (_sp := master.line("profile.save_prompt")) else "")),
        color=0x57F287,
    )
    stats_line = []
    for k, v in profile.items():
        if not v:
            continue
        if len(str(v)) > 60:
            e.add_field(name=k, value=str(v)[:1000], inline=False)
        else:
            stats_line.append(f"**{k}** {v}")
    if stats_line:
        e.insert_field_at(0, name="​", value=" · ".join(stats_line)[:1024],
                          inline=False)
    if state.notes:
        e.add_field(name="알림", value="\n".join(f"· {n}" for n in state.notes)[:1024], inline=False)
        state.notes = []
    try:
        items = profile_gen.starting_items(
            state.session.scenario_data, result.get("직업"))
        if items:
            e.add_field(name="소지품", value=", ".join(items), inline=False)
    except Exception:
        pass
    return e


async def _show_save(state, channel):
    """완성 화면 + 저장 여부(최종 승인 뒤). 재시작 복원도 이 화면부터 — finish를 다시 하지 않는다."""
    result = runner.result(state.run)
    view = SaveProfileView(state, result)
    state.run["gen"] = int(state.run.get("gen") or 0) + 1
    view.gen = state.run["gen"]
    state.run["await"] = None
    await _persist(state)
    e = _completion_embed(state, result)
    if state.message:
        try:
            await state.message.edit(embed=e, view=view, content=None)
            view.message = state.message
        except Exception:
            state.message = None
    if not state.message:
        state.message = await channel.send(embed=e, view=view)
        view.message = state.message

    # NOTE: 다음 단계로 넘기는 것은 SaveProfileView가 한다.
    #       여기서 넘기면 저장 여부를 묻기도 전에 화면이 바뀐다.


# ══════════════════════════════════════════════════════════════
#  뷰
# ══════════════════════════════════════════════════════════════

class _Base(discord.ui.View):
    """조작 권한 검사 — 본인만 조작할 수 있다. 시간 만료 없음(재시작은 resume_all이 새 화면으로 복원)."""

    def __init__(self, state, channel, timeout=None):
        super().__init__(timeout=timeout)
        self.state = state
        self.channel = channel
        self.gen = None

    async def interaction_check(self, interaction) -> bool:
        if str(interaction.user.id) == self.state.uid:
            return True
        await interaction.response.send_message(
            "본인만 조작할 수 있습니다.", ephemeral=True)
        return False


async def _guarded(view, interaction, fn):
    """버튼 처리 공통 — defer → 락 → 세대 확인(낡은 화면 무시) → fn. fn이 AI 작업을 돌려주면 락 밖에서 실행."""
    try:
        await interaction.response.defer()
    except Exception:
        pass
    state = view.state
    job = None
    async with state.lock:
        if view.gen is not None and view.gen != state.run.get("gen"):
            try:
                await interaction.followup.send("이전 화면입니다. 가장 최근 화면을 이용해 주십시오.", ephemeral=True)
            except Exception:
                pass
            return
        job = await fn()
    if job:
        await job()


class ChoiceView(_Base):
    """목록 선택 — 항목 수에 따라 버튼 또는 셀렉트."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        options = res.get("options") or []
        if len(options) <= SELECT_THRESHOLD:
            for opt in options[:SELECT_THRESHOLD]:
                self.add_item(ChoiceButton(str(opt)))
        else:
            self.add_item(ChoiceSelect(options))
        if res.get("can_back"):
            self.add_item(BackButton())


class ChoiceButton(discord.ui.Button):
    def __init__(self, value: str):
        super().__init__(label=value[:80], style=discord.ButtonStyle.primary)
        self.value = value

    async def callback(self, interaction):
        v = self.view
        await _guarded(v, interaction, lambda: render(v.state, v.channel, user_input=self.value))


class ChoiceSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(
            placeholder="선택",
            options=[discord.SelectOption(label=str(o)[:100], value=str(o))
                     for o in options[:MAX_OPTIONS]],
        )

    async def callback(self, interaction):
        v = self.view
        val = self.values[0]
        await _guarded(v, interaction, lambda: render(v.state, v.channel, user_input=val))


class ConfirmView(_Base):
    """확인 메시지 — 확정 또는 재선택(기획 규정)."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        self.res = res

    @discord.ui.button(label="확정", style=discord.ButtonStyle.success)
    async def ok(self, interaction, _b):
        async def fn():
            outcome = runner.confirm(self.state.session.scenario_data, self.state.run)
            if outcome.get("revised"):
                # 별도 메시지로 보내면 화면 밖에 쌓인다. 다음 화면에 함께 싣는다.
                self.state.notes.append(outcome["revised"]["reason"])
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)

    @discord.ui.button(label="다시 선택", style=discord.ButtonStyle.secondary)
    async def again(self, interaction, _b):
        async def fn():
            # 취소로 닫으면 재선택하며 이전 선택 흔적을 지운다(기획 규정).
            runner.cancel_pending(self.state.run)
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)


class StatRollView(_Base):
    """
    능력치 배분 — 산출은 언제나 랜덤이고 세 조건을 등급으로 고른다.

    기획 규정 — 수치 대신 별명으로 직관적으로 선택하게 한다.
      총합   허접 · 약골 · 평범 · 튼튼 · 능력자 · 먼치킨
      편차   만능 · 무난 · 뚜렷 · 극단
      특화   골고루 또는 특정 능력치
    """

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        self.res = res
        args = res.get("args") or {}
        sd = state.session.scenario_data
        self.add_item(TotalTierSelect(args, sd))
        self.add_item(SpreadTierSelect(args, sd, state.run))
        stats = args.get("stats") or []
        if stats:
            self.add_item(TopFieldSelect(stats))

    @discord.ui.button(label="🎲 다시 굴리기", style=discord.ButtonStyle.primary, row=3)
    async def reroll(self, interaction, _b):
        async def fn():
            runner.cancel_pending(self.state.run)
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)

    @discord.ui.button(label="✅ 확정", style=discord.ButtonStyle.success, row=3)
    async def ok(self, interaction, _b):
        async def fn():
            runner.confirm(self.state.session.scenario_data, self.state.run)
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)


class _ConstraintSelect(discord.ui.Select):
    key = ""

    async def callback(self, interaction):
        v = self.view
        val = None if self.values[0] == "__none__" else self.values[0]

        async def fn():
            runner.set_stat_constraint(v.state.run, self.key, val)
            runner.cancel_pending(v.state.run)
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class TotalTierSelect(_ConstraintSelect):
    """총합 등급 — 상한 대비 비율로 환산된다."""
    key = "total_tier"

    def __init__(self, args: dict, scenario_data: dict):
        tiers = profile_gen.get_total_tiers(scenario_data)
        options = [discord.SelectOption(label="총합: 무작위", value="__none__")]
        for name in tiers:
            options.append(discord.SelectOption(
                label=profile_gen.describe_tier(name, args, scenario_data)[:100],
                value=name))
        super().__init__(placeholder="전체적인 강함", options=options[:25], row=0)


class SpreadTierSelect(_ConstraintSelect):
    """편차 등급 — 총합에서 가능한 최대 편차에 비례한다."""
    key = "spread_tier"

    def __init__(self, args: dict, scenario_data: dict, run: dict):
        tiers = profile_gen.get_spread_tiers(scenario_data)
        cons = run.get("stat_constraints") or {}
        total_v = (profile_gen.tier_to_total(cons["total_tier"], args, scenario_data)
                   if cons.get("total_tier") else None)
        options = [discord.SelectOption(label="편차: 무작위", value="__none__")]
        for name in tiers:
            options.append(discord.SelectOption(
                label=profile_gen.describe_tier(
                    name, args, scenario_data, kind="spread", total=total_v)[:100],
                value=name))
        super().__init__(placeholder="능력의 치우침", options=options[:25], row=1)


class TopFieldSelect(_ConstraintSelect):
    """특정 능력치를 최고로 지정하거나 해제한다."""
    key = "top_field"

    def __init__(self, stats: list):
        options = [discord.SelectOption(label="특화: 골고루", value="__none__")]
        options += [discord.SelectOption(label=f"{s} 특화", value=s)
                    for s in stats[:24]]
        super().__init__(placeholder="특화 분야", options=options, row=2)


class TextWaitView(_Base):
    """텍스트 답변 대기 — 답은 채팅으로 받는다. 버튼은 조작(이전 단계)뿐이다."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        if res.get("can_back"):
            self.add_item(BackButton())


class ModeView(_Base):
    """장문 필드 — 직접 작성 / 질문형 가이드 선택."""

    def __init__(self, state, channel, res: dict):
        super().__init__(state, channel)
        if res.get("can_back"):
            self.add_item(BackButton())

    @discord.ui.button(label="✍️ 직접 작성", style=discord.ButtonStyle.primary, row=0)
    async def direct(self, interaction, _b):
        await self._pick(interaction, "direct")

    @discord.ui.button(label="🧭 질문에 답하며 정리", style=discord.ButtonStyle.primary, row=0)
    async def guide(self, interaction, _b):
        await self._pick(interaction, "guide")

    async def _pick(self, interaction, mode):
        async def fn():
            lf = _lf(self.state, _step_def(self.state).get("field"))
            lf["mode"] = mode
            lf["q"] = 0
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)


class ModeResetButton(discord.ui.Button):
    def __init__(self, label="↺ 방식 다시 고르기"):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction):
        v = self.view

        async def fn():
            field = _step_def(v.state).get("field")
            v.state.run["lf"] = None
            _lf(v.state, field)
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class LongWaitView(_Base):
    """장문 직접 작성 대기(채팅)."""

    def __init__(self, state, channel, res: dict, *, guide: bool):
        super().__init__(state, channel)
        if guide:
            self.add_item(ModeResetButton())
        if res.get("can_back"):
            self.add_item(BackButton())


class GuideView(_Base):
    """보조 질문 — 선택지면 버튼/셀렉트, 아니면 채팅. 선택 질문만 [건너뛰기]."""

    def __init__(self, state, channel, step, q: dict):
        super().__init__(state, channel)
        self.step = step
        self.q = q
        opts = q.get("options") or []
        if opts and len(opts) <= SELECT_THRESHOLD:
            for o in opts:
                self.add_item(GuideOptionButton(o))
        elif opts:
            self.add_item(GuideOptionSelect(opts))
        if not q.get("required"):
            self.add_item(GuideSkipButton())
        lf = state.run.get("lf") or {}
        if lf.get("q", 0) > 0 and not lf.get("redo"):
            self.add_item(GuidePrevButton())
        if (profile_gen.creation_guide(step) or {}).get("direct", True):
            self.add_item(ModeResetButton())


async def _guide_answer(view, interaction, value):
    async def fn():
        return await _answer_guide(view.state, view.channel, view.step, view.q, value)
    await _guarded(view, interaction, fn)


class GuideOptionButton(discord.ui.Button):
    def __init__(self, value: str):
        super().__init__(label=value[:80], style=discord.ButtonStyle.primary, row=0)
        self.value = value

    async def callback(self, interaction):
        await _guide_answer(self.view, interaction, self.value)


class GuideOptionSelect(discord.ui.Select):
    def __init__(self, options):
        super().__init__(placeholder="선택", row=0,
                         options=[discord.SelectOption(label=str(o)[:100], value=str(o))
                                  for o in options[:MAX_OPTIONS]])

    async def callback(self, interaction):
        await _guide_answer(self.view, interaction, self.values[0])


class GuideSkipButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="건너뛰기", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction):
        await _guide_answer(self.view, interaction, None)


class GuidePrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ 이전 질문", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction):
        v = self.view

        async def fn():
            lf = v.state.run["lf"]
            lf["q"] = max(0, lf["q"] - 1)
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class ComposeView(_Base):
    """답변은 다 받았는데 초안이 없는 상태(재시작 복원) — 조합을 다시 시작한다."""

    @discord.ui.button(label="📝 초안 만들기", style=discord.ButtonStyle.primary)
    async def compose(self, interaction, _b):
        async def fn():
            return await _compose(self.state, self.channel, _step_def(self.state))
        await _guarded(self, interaction, fn)


class DraftView(_Base):
    """장문 초안 검토 — 승인 / 부분 수정(채팅) / 질문 다시 고르기 / 처음부터."""

    def __init__(self, state, channel, step):
        super().__init__(state, channel)
        lf = state.run["lf"]
        self.approve.disabled = bool(lf.get("too_long") or lf.get("blocked"))
        qs = _questions(step)
        if lf.get("mode") == "guide" and qs:
            self.add_item(GuideReselect(qs))
        self.add_item(ModeResetButton("↺ 처음부터 다시"))

    @discord.ui.button(label="✅ 이대로 확정", style=discord.ButtonStyle.success, row=0)
    async def approve(self, interaction, _b):
        async def fn():
            st = self.state
            lf = st.run.get("lf") or {}
            if lf.get("draft") is None or lf.get("too_long") or lf.get("blocked"):
                return
            field = lf["field"]
            st.run["pending"] = {"field": field, "value": lf["draft"]}
            runner.confirm(st.session.scenario_data, st.run)
            st.run.setdefault("lf_store", {})[field] = {"mode": lf.get("mode"),
                                                        "answers": dict(lf.get("answers") or {})}
            st.run["lf"] = None
            await render(st, self.channel)
        await _guarded(self, interaction, fn)

    @discord.ui.button(label="✏️ 부분 수정", style=discord.ButtonStyle.primary, row=0)
    async def edit(self, interaction, _b):
        async def fn():
            st = self.state
            step = _step_def(st)
            lf = st.run["lf"]
            st.run["phase"] = P_EDIT
            await _send(st, self.channel, {
                "field": lf["field"], "head": _mline(step, "edit_prompt", "profile.edit_prompt"),
                "message": f"**지금 초안**\n{lf['draft']}\n\n고칠 부분을 채팅으로 적어 주십시오. "
                           "(예: '머리는 짧게', '마지막 문장은 빼 줘')"},
                view=EditWaitView(st, self.channel), await_kind="edit")
        await _guarded(self, interaction, fn)


class GuideReselect(discord.ui.Select):
    def __init__(self, qs):
        super().__init__(placeholder="질문 다시 고르기", row=2,
                         options=[discord.SelectOption(label=f"{i + 1}. {q['prompt']}"[:100], value=str(i))
                                  for i, q in enumerate(qs[:MAX_OPTIONS])])

    async def callback(self, interaction):
        v = self.view
        idx = int(self.values[0])

        async def fn():
            lf = v.state.run["lf"]
            lf["q"] = idx
            lf["redo"] = True
            lf["draft"] = None
            lf["too_long"] = False
            lf["blocked"] = None
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class EditWaitView(_Base):
    """부분 수정 입력 대기(채팅) — 취소하면 초안 그대로."""

    @discord.ui.button(label="↩ 수정 취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _b):
        async def fn():
            await _show_draft(self.state, self.channel)
        await _guarded(self, interaction, fn)


class FinalView(_Base):
    """전체 최종 검토 — 고칠 항목 선택 / 최종 승인."""

    def __init__(self, state, channel, fields: list, blocked: bool):
        super().__init__(state, channel)
        if fields:
            self.add_item(FinalEditSelect(fields))
        self.final.disabled = blocked

    @discord.ui.button(label="✅ 이대로 시작", style=discord.ButtonStyle.success, row=1)
    async def final(self, interaction, _b):
        async def fn():
            st = self.state
            if st.run.get("finalized") or st.run.get("final_invalid"):
                return
            await finish(st, self.channel)
        await _guarded(self, interaction, fn)


class FinalEditSelect(discord.ui.Select):
    def __init__(self, fields):
        super().__init__(placeholder="고칠 항목", row=0,
                         options=[discord.SelectOption(label=str(f)[:100], value=str(f))
                                  for f in fields[:MAX_OPTIONS]])

    async def callback(self, interaction):
        v = self.view
        field = self.values[0]

        async def fn():
            await begin_field_edit(v.state, v.channel, field)
        await _guarded(v, interaction, fn)


async def begin_field_edit(state, channel, field: str):
    """최종 검토의 필드 수정 — 그 필드(+의존 필드)만 다시 정한다. 원래 값은 취소 시 복원용으로 보관."""
    sd = state.session.scenario_data
    run = state.run
    deps = runner.dependents_of(sd, field)
    old = {f: copy.deepcopy(run["chosen"][f]) for f in [field] + deps if f in run["chosen"]}
    ok, _f = runner.edit_field(sd, run, field)
    if not ok:
        await _show_final(state, channel)
        return
    run["review_old"] = old
    step = _step_def(state)
    prev = old.get(field)
    if profile_gen.is_long_form(step) and isinstance(prev, str) and prev:
        # 장문은 이전 확정본을 초안으로 다시 열어 부분 수정·질문 다시 고르기를 할 수 있게 한다
        stored = (run.get("lf_store") or {}).get(field) or {}
        lf = _lf(state, field)
        lf["mode"] = stored.get("mode") or "direct"
        lf["answers"] = dict(stored.get("answers") or {})
        lf["q"] = len(_questions(step))
        _set_draft(state, step, prev, blocked=None)
    await render(state, channel)


class ReviewCancelButton(discord.ui.Button):
    """최종 검토에서 들어온 수정 — 원래 값으로 되돌리고 최종 검토로."""

    def __init__(self):
        super().__init__(label="↩ 원래대로", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view

        async def fn():
            run = v.state.run
            for f, val in (run.get("review_old") or {}).items():
                run["chosen"][f] = val
            run["pending"] = None
            run["review_old"] = None
            run["lf"] = None
            runner.advance_index(v.state.session.scenario_data, run)
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class BackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ 이전 단계", style=discord.ButtonStyle.secondary, row=4)

    async def callback(self, interaction):
        v = self.view

        async def fn():
            v.state.run["lf"] = None
            ok, _f = runner.go_back(v.state.session.scenario_data, v.state.run)
            if not ok:
                await interaction.followup.send("되돌아갈 단계가 없습니다.", ephemeral=True)
                return
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


class BackOnlyView(_Base):
    def __init__(self, state, channel):
        super().__init__(state, channel)
        self.add_item(BackButton())


class SaveProfileView(_Base):
    """완성 후 사전 프로필 저장 여부를 묻는다(기획 규정).

    어느 쪽을 고르든 화면을 정리하고 세션 플로우로 넘긴다.
    버튼만 비활성화하고 두면 다음 단계가 시작되지 않는다.
    """

    def __init__(self, state, result: dict):
        super().__init__(state, None)
        self.result = result
        self.message = None

    async def _close(self, interaction, note: str):
        self.clear_items()
        e = discord.Embed(
            title=f"{self.state.char_name}",
            description=note,
            color=0x57F287,
        )
        try:
            await interaction.message.edit(embed=e, view=None)
        except Exception:
            pass
        self.state.message = None
        self.stop()
        await _complete(self.state)
        await _advance_flow(self.state, interaction.channel)

    @discord.ui.button(label="💾 사전 프로필로 저장", style=discord.ButtonStyle.success)
    async def save(self, interaction, _b):
        async def fn():
            self.state.run["gen"] = int(self.state.run.get("gen") or 0) + 1   # 이 화면은 한 번만 처리
            saved = await profile_store.create(
                self.state.uid,
                name=self.state.char_name,
                scenario_id=getattr(self.state.session, "scenario_id", None),
                fields=dict(self.result),
            )
            note = (master.say("profile.saved", "저장했습니다.") + " 다음에 이 시나리오를 하실 때 불러오실 수 있어요.\n"
                    f"태그: **{saved['tag']}**") if saved else "저장에 실패했습니다."
            await self._close(interaction, note)
        await _guarded(self, interaction, fn)

    @discord.ui.button(label="저장 안 함", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction, _b):
        async def fn():
            self.state.run["gen"] = int(self.state.run.get("gen") or 0) + 1
            await self._close(interaction, master.say("profile.save_skipped", "준비가 끝났습니다."))
        await _guarded(self, interaction, fn)


class PrefillView(_Base):
    """사전 프로필 사용 여부(기획 규정 — 보유자에게만 묻는다).

    고르든 새로 만들든 이 화면을 그대로 생성 화면으로 바꿔 이어간다.
    새 메시지를 보내면 이 버튼들이 남아 다시 눌린다.
    """

    def __init__(self, state, channel, candidates: list):
        super().__init__(state, channel)
        dup = profile_store.duplicate_names(
            state.uid, getattr(state.session, "scenario_id", None))
        self.add_item(PrefillSelect(candidates, dup))

    @discord.ui.button(label="새로 만들기", style=discord.ButtonStyle.primary, row=1)
    async def fresh(self, interaction, _b):
        async def fn():
            self.state.message = interaction.message
            self.clear_items()
            self.stop()
            await render(self.state, self.channel)
        await _guarded(self, interaction, fn)


class PrefillSelect(discord.ui.Select):
    def __init__(self, candidates, dup):
        super().__init__(
            placeholder="저장해둔 프로필 사용",
            options=[discord.SelectOption(
                label=profile_store.display_name(p, dup)[:100], value=p["id"])
                for p in candidates[:MAX_OPTIONS]],
        )
        self.candidates = candidates

    async def callback(self, interaction):
        v = self.view
        picked = next((p for p in self.candidates
                       if p["id"] == self.values[0]), None)

        async def fn():
            if picked:
                # 사전 프로필 값을 채워 두면 실행부가 해당 단계를 건너뛴다 — 끝나면 최종 검토에서
                # 고칠 필드만 고친다(Q8). 저장본의 값·자료형을 그대로 복사하고 원본 파일은 건드리지 않는다.
                gen = v.state.run.get("gen", 0)
                v.state.run = runner.new_run(
                    v.state.session.scenario_data,
                    prefill=copy.deepcopy(dict(picked.get("fields") or {})))
                v.state.run["gen"] = gen
                v.state.notes.append(
                    f"{picked.get('name', '저장한 프로필')}을(를) 불러왔습니다")

            # 이 메시지를 생성 화면으로 이어 쓴다.
            v.state.message = interaction.message
            v.clear_items()
            v.stop()
            await render(v.state, v.channel)
        await _guarded(v, interaction, fn)


async def _advance_flow(state, channel):
    """세션 제작 플로우 진행 중이면 다음 단계로 넘긴다."""
    if not getattr(state.session, "creation_state", None):
        return
    try:
        from . import session_flow
        await session_flow.on_profile_done(state.bot, state.session, channel)
    except Exception as e:
        print(f"[세션플로우] 프로필 완료 처리 실패: {e}")


async def start(bot, session, uid, char_name: str, channel):
    """프로필 생성을 시작한다.

    기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면
    사용 여부 질문을 생략한다. 이미 진행 중이면(같은 채널·플레이어) 새로 만들지 않고 그 화면을 다시 그린다.
    """
    uid = str(uid)
    existing = _ACTIVE.get((getattr(channel, "id", None), uid))
    if existing is not None and existing.session is session:
        async with existing.lock:
            existing.message = None
            await render(existing, channel)
        return existing
    prog = (getattr(session, PROGRESS_FIELD, None) or {}).get(uid)
    if isinstance(prog, dict):
        return await _resume_one(bot, session, prog, channel=channel)

    state = ProfileCreationSession(bot, session, uid, char_name, channel_id=getattr(channel, "id", None))

    steps = profile_gen.get_steps(session.scenario_data)
    if not steps:
        # 알고리즘이 없으면 흐름이 끊기지 않도록 안내 후 다음 단계로 넘긴다.
        await channel.send(embed=discord.Embed(
            title="캐릭터 설정",
            description=("이 시나리오에는 자동 생성 절차가 준비되어 있지 않습니다.\n"
                         "`!능력치` `!외형` `!설정` 명령으로 직접 정해 주세요."),
            color=0xFEE75C))
        await _advance_flow(state, channel)
        return None

    _register(state)
    # 기획 규정 — 해당 시나리오가 처음이거나 사전 프로필이 없으면 질문 생략.
    from . import creation
    sid = getattr(session, "scenario_id", None)
    async with state.lock:
        if creation.can_skip_profile_question(session, uid, sid):
            await render(state, channel)
            return state

        candidates = profile_store.list_profiles(uid, sid)
        if candidates:
            e = discord.Embed(
                title=_title(state),
                description=master.say("profile.prefill", "전에 저장해두신 프로필이 있어요.\n"
                                                          "불러다 쓰시겠어요, 아니면 새로 만드시겠어요?"),
                color=0x5865F2)
            view = PrefillView(state, channel, candidates)
            state.run["gen"] = int(state.run.get("gen") or 0) + 1
            view.gen = state.run["gen"]
            await _persist(state)
            state.message = await channel.send(embed=e, view=view)
            return state

        await render(state, channel)
    return state


# ══════════════════════════════════════════════════════════════
#  재시작 복원
# ══════════════════════════════════════════════════════════════

async def _resume_one(bot, session, prog: dict, *, channel=None):
    """영속된 진행 하나를 복원해 안전한 화면을 새로 그린다(낡은 버튼은 세대로 무효)."""
    if not isinstance(prog, dict) or prog.get("version") != PROGRESS_VERSION or not isinstance(prog.get("run"), dict):
        return None
    if prog.get("scenario_id") != getattr(session, "scenario_id", None):
        print(f"[프로필 생성] 복원 거부 — 시나리오 불일치 uid={prog.get('uid')}")
        return None
    channel = channel or bot.get_channel(prog.get("channel_id"))
    if channel is None:
        return None
    run = copy.deepcopy(prog["run"])
    state = ProfileCreationSession(bot, session, prog.get("uid"), prog.get("char_name") or _NAME_PENDING,
                                   channel_id=prog.get("channel_id"), run=run)
    _register(state)
    async with state.lock:
        proc = run.get("proc")
        if run.get("phase") == P_PROCESSING or proc:
            # 진행 중이던 AI 처리는 재시작으로 끊겼다 — 결과를 추정·자동 승인하지 않고 그 입력을 다시 받는다
            raw = (proc or {}).get("raw") or ""
            run["proc"] = None
            state.notes.append("재시작으로 처리 중이던 입력이 취소되었습니다. 다시 보내 주십시오."
                               + (f"\n> {raw[:300]}" if raw else ""))
        line = master.line("profile.resume")
        if line:
            state.notes.insert(0, line)
        await render(state, channel)
    return state


async def resume_all(bot) -> int:
    """봇 재시작 뒤 세션별 생성 진행을 복원한다(main.on_ready, 세션 복구 직후). 복원 수를 돌려준다."""
    n = 0
    for session in {id(s): s for s in (getattr(bot, "active_sessions", None) or {}).values()}.values():
        for _uid, prog in list((getattr(session, PROGRESS_FIELD, None) or {}).items()):
            try:
                if await _resume_one(bot, session, prog):
                    n += 1
            except Exception as e:
                print(f"[프로필 생성] 복원 실패: {e}")
        try:
            if await _resume_after_profile(bot, session):
                n += 1
        except Exception as e:
            print(f"[프로필 생성] 완료 뒤 단계 복원 실패: {e}")
    return n


async def _resume_after_profile(bot, session) -> bool:
    """v7.17 안정화 P3: 프로필 확정·저장 선택까지 끝났는데 세션 플로우가 프로필 단계에 남은 세션 → 기존
    on_profile_done으로 이어 간다(PROFILE 초기 지식은 멱등·영수증·UNCERTAIN 규칙 그대로, 새 AI 호출 정책 불변)."""
    st = getattr(session, "creation_state", None)
    if not (isinstance(st, dict) and st.get("step") == "profile" and (st.get("data") or {}).get("profile") == "완료"):
        return False
    from . import session_flow
    uid = session_flow._owner(session)
    if not uid or uid in (getattr(session, PROGRESS_FIELD, None) or {}) \
            or not ((getattr(session, "players", None) or {}).get(uid) or {}).get("profile"):
        return False
    channel = bot.get_channel(getattr(session, "game_ch_id", 0))
    if channel is None:
        return False
    await session_flow.on_profile_done(bot, session, channel)
    return True
