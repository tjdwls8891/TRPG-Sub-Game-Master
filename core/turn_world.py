# 턴 세계 — 묘사 투영·관찰·같은 분 선후·보충 진행·전달 대기·투영 미디어 (H05-B·C, P1)
#
# [권위 — DESIGN 12 · 11 §28~33 · 09 §18~22 · H05 handoff §4~5]
#   전부 순수 함수다(세션 불변). 결과 ops는 PLAYER_TURN 묶음에 담겨 READY → CommitCoordinator로만 커밋된다.
#   · 묘사 입력: 준비된 계획의 **투영 장면**(목적지 도착이면 목적지, 중단이면 중간 장소/TRANSIT)과
#     PC가 관찰할 수 있는 결과만 준다. 실제 숨은 경로·접근·미래 진실은 넣지 않는다.
#   · 추출(NarrationObservation): 확정 묘사를 관찰할 뿐 두 번째 세계 시뮬레이터가 아니다.
#       허용: 실제로 묘사된 정보 노출(후보 키) · 실제로 표현된 신호 표시(필수 서사 근거).
#       금지: 사건 국면/케이스 선택 · 숨은 경로/접근 발견 · 코드 소유 이동/시간 덮어쓰기 · PC 믿음 창작.
#   · P1(D1): 시간은 묘사 전에만 흐른다(이동·대기·행동 소요 시간 — 코드 소유). 추출의 추가 시간은 쓰지 않는다.
#   · P1(D2): 같은 분 서사 선후는 묘사 전에 정한다(지시층위 선택, 무효면 코드 결정 — resolve_plan_tie).
#     장면 밖 충돌은 H04가 이미 WORLD_PROJECTION_CONFLICT로 끝낸다(선택지로 보내지 않는다).
#   · 보충 진행(묘사 후 인지·아이템 사실의 같은 분 평가)은 묘사 전 계획과 같은 관찰자 문맥을 쓴다.
#     결정적 충돌은 재시도 사유가 아니다(provider 재호출·RETRY_PENDING 없음).
#   · P1(#1): 후속 묘사 없음. v7.21.1: 묘사 후 보충 진행이 처음 만든 관찰 신호만 world_pending_signals(되감기 대상
#     정본)에 남겨 다음 커밋되는 소설형 본문 1회가 '그사이' 일로 전한다. 묘사 전 계획 신호는 이월하지 않는다.
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from . import action_intent as _ai
from . import cognition as _cog
from . import information as _info
from . import player_world as _pw
from . import place_display as _pd
from . import relationships as _rel
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_progression as _wp
from .world_ids import stable_digest

OBS_FIELD = "world_observation"
NOISE_FIELD = "noise"            # v7.48(E4) 관찰 — 이번 묘사에서 PC 행동이 낸 소리(QUIET·NORMAL·LOUD)
MAX_EXTRA_MINUTES = 240                # 추출이 주는 비코드 소유 추가 시간 상한(안전)

TW_READY = "READY"
TW_TIE_UNRESOLVED = "TIE_UNRESOLVED"   # 묘사 선후가 모호·무효 — READY 불가
TW_CONFLICT = "CONFLICT"               # WORLD_PROJECTION_CONFLICT — 결정적, 재시도 없음

# P1 — 본문 전달 대기 필드 world_pending_signals(단일 출처 core.world_state.DELIVERY_FIELDS)
#   v7.21.1: 묘사 후 보충 진행 신호만, 다음 커밋된 PROCEED 본문 1회(재이월·횟수·은퇴 없음)


# ══════════════════════════════════════════════════════════════
#  런타임 턴 세계 기록(준비 객체에 붙는다 — 영속하지 않는다)
# ══════════════════════════════════════════════════════════════

@dataclass
class WorldTurn:
    ctx: object                                   # player_world.WorldContext
    manifest: object = None                       # ActionReferenceManifest(판단 시점)
    intent: object = None                         # ResolvedActionIntent
    plan: object = None                           # PlayerWorldPlan(묘사 전)
    obs_manifest: object = None                   # ObservationManifest(추출 시점)
    result: object = None                         # TurnWorldResult(추출 후)
    replan: object = None                         # 선후 확정 재계획 호출(callable(orders) -> plan)
    tie_choice: str | None = None                 # P1(D2): 지시층위가 고른 선후 키(O키, 검증 전)
    identified_faces: tuple = ()                  # SVG 카드: 지시층위가 지정한 얼굴 식별 ActorId(검증됨)
    identified_names: tuple = ()                  # SVG 카드: 지시층위가 지정한 이름 식별 ActorId(검증됨)
    discovery: dict | None = None                 # v7.14 이번 턴 주변 발견 결과(NEW만 정본화 — 추출 시점 고정)
    item_manifest: object = None                  # v7.17 core.items.ItemManifest(추출 시점 — 소유자·소지·현장 키)
    pending_move: dict | None = None              # v7.22 목적지 질문 문맥 {place, world_version, exits} — 이 시도 안에서만
    pending_route: dict | None = None             # v7.23 경로 질문 번호 스냅샷 {place, world_version, signatures}


# ══════════════════════════════════════════════════════════════
#  투영 장면 · 묘사 오버레이
# ══════════════════════════════════════════════════════════════

def _calendar(definition):
    cal = definition.get("calendar")
    return _clock.Calendar(cal) if cal else None


def clock_view(definition, minute) -> dict:
    cal = _calendar(definition)
    if cal is None or minute is None:
        return {}
    dt = cal.from_minute_index(minute)
    wd = cal.weekday_of(minute)
    return {"current_date": f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}",
            "time_of_day": cal.daypart_of(minute) or "미확인", "hhmm": f"{dt.hour:02d}:{dt.minute:02d}",
            "weekday": WEEKDAY_KO[wd] if wd is not None else None,
            "season": season_text(dt.month, dt.day) if cal.calendar_type == _clock.CALENDAR_GREGORIAN else None}


WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")
# v7.35(U4): 계절은 정본 날짜에서 파생한다(그레고리력·한국 기준). 날씨는 여전히 정본이 아니다.
SEASON_BY_MONTH = {12: "겨울", 1: "겨울", 2: "겨울", 3: "봄", 4: "봄", 5: "봄",
                   6: "여름", 7: "여름", 8: "여름", 9: "가을", 10: "가을", 11: "가을"}


def season_text(month: int, day: int) -> str:
    part = "초순" if day <= 10 else ("중순" if day <= 20 else "하순")
    return f"{SEASON_BY_MONTH[month]}({month}월 {part})"


def clock_text(cv: dict) -> str:
    return (f"{cv['current_date']}" + (f" {cv['weekday']}" if cv.get("weekday") else "")
            + f" {cv['hhmm']} ({cv['time_of_day']})")


def narration_clock_block(wt, session) -> str:
    """묘사층위용 정본 시각 — 매 턴 준다(이동이 없는 턴도). 상태 블록의 날짜·요일·시각은 이 값을 따른다."""
    plan = wt.plan
    if plan is not None and plan.status == _pw.PLAN_PLANNED and plan.end_minute is not None:
        minute = plan.end_minute
    else:
        world = getattr(plan, "projected_world", None) or session
        minute = ((world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
                   else getattr(world, "world_clock", {})) or {}).get("minute_index")
    cv = clock_view(wt.ctx.definition, minute)
    if not cv:
        return ""
    season = (f"· 계절: {cv['season']} — 기온·햇볕·더위·추위 묘사는 이 계절에 맞출 것(계절과 어긋나는 폭염·혹한을 "
              "지어내지 말 것).\n") if cv.get("season") else ""
    return ("\n[현재 작중 시각 — 정본]\n· " + clock_text(cv) + "\n" + season +
            "· 본문과 상태 블록의 날짜·요일·시각 표기는 이 값을 따를 것. 다른 날짜·요일·시각을 지어내지 말 것"
            "(경과일 등 다른 표기를 쓰더라도 이 날짜와 어긋나지 않게).\n")


# ══════════════════════════════════════════════════════════════
#  v7.25(F09) 상태창 — 날짜·요일·시간대·장소·턴 수는 코드가 정본에서 렌더링한다
# ══════════════════════════════════════════════════════════════

STATUS_UNKNOWN = "(미확인)"

INTRO_CANON_NOTE = ("\n[인트로 — 시작 틀과 정본]\n"
                    "· 위 정본 시각·장소·장면 인물이 세계의 사실이다. [시작 상황]·[묘사 지시]는 분위기·연출의 저작 틀이다.\n"
                    "· 시작 틀의 익명 인물·형체·소리는 이름 없는 연출로만 다룬다 — 장면 인물 목록의 정의 인물과 같은 사람으로 "
                    "확정하지 말 것. 둘이 어긋나면 정본(장면 인물 목록)을 따른다: 목록의 인물을 지우지도, 목록 밖 정의 인물을 "
                    "등장시키지도 말 것.\n"
                    "· 장소는 위 [현재 장소 — 정본]의 이름으로 부른다.\n")


def status_values(wt, session, turn_no) -> dict:
    """묘사가 표현하는 시점의 정본 상태창 값(묘사층위 세계 블록과 같은 기준).

    시각: 계획이 PLANNED면 묘사 끝 시각(plan.end_minute), 아니면 정본 시계. 장소: 투영 장면(PLANNED) 또는
    정본 PC 위치 — 길 위 중단(TRANSIT)이면 '출발 → 도착 쪽 이동 중'. 이름은 PC 지식 수준 표시명(내부 PlaceId 비노출).
    결정할 수 없는 값은 STATUS_UNKNOWN(추측하지 않는다)."""
    D, pc, plan = wt.ctx.definition, wt.ctx.lead_pc, wt.plan
    planned = plan is not None and plan.status == _pw.PLAN_PLANNED and plan.projected_world is not None
    world = plan.projected_world if planned else session
    if planned and plan.end_minute is not None:
        minute = plan.end_minute
    else:
        minute = ((world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
                   else getattr(world, "world_clock", {})) or {}).get("minute_index")
    cv = clock_view(D, minute)
    pl = _sp.placement_of(world, pc) or {}
    if pl.get("kind") == _sp.AT_PLACE:
        place = _pd.display_for(D, world, pc, pl["place_id"], require_known=False)
    elif pl.get("kind") == _sp.TRANSIT:
        place = (f"{_pd.display_for(D, world, pc, pl['from_place'], require_known=False)} → "
                 f"{_pd.display_for(D, world, pc, pl['to_place'], require_known=False)} 쪽 이동 중")
    else:
        place = STATUS_UNKNOWN
    return {"turn": str(turn_no), "date": cv.get("current_date") or STATUS_UNKNOWN,
            "weekday": cv.get("weekday") or STATUS_UNKNOWN, "daypart": cv.get("time_of_day") or STATUS_UNKNOWN,
            "hhmm": cv.get("hhmm") or STATUS_UNKNOWN, "place": place or STATUS_UNKNOWN}


_SLOT_RE = None


def _slot_kind(name: str) -> str | None:
    n = name.strip()
    if "턴" in n:
        return "turn"
    if "요일" in n:
        return "weekday"
    if "날짜" in n:
        return "date"
    if n.startswith("시간대") or "시간대" in n:
        return "daypart"
    if n.startswith("시각") or n.startswith("시간"):
        return "hhmm"
    if "장소" in n or "위치" in n:
        return "place"
    return None


def _slots(line: str) -> list:
    import re
    global _SLOT_RE
    if _SLOT_RE is None:
        _SLOT_RE = re.compile(r"\{([^{}]+)\}")
    return _SLOT_RE.findall(line)


def canon_record_line(values: dict) -> str:
    """v7.25(F05): 압축 기록용 정본 메타 줄 — 코드가 정본 세계에서 기록한 이 턴의 시각·장소(표시명)."""
    return (f"[정본 기록 #{values.get('turn')}턴 | {values.get('date')} {values.get('weekday')} "
            f"{values.get('hhmm')} ({values.get('daypart')}) | {values.get('place')}]")


def status_template_for_prompt(template: str, values: dict) -> str:
    """묘사층위에 보여 줄 상태창 양식 — 코드 소유 칸은 값으로 미리 채운다(모델은 나머지 칸만 채운다)."""
    out = template
    for name in _slots(template):
        k = _slot_kind(name)
        if k:
            out = out.replace("{" + name + "}", values.get(k, STATUS_UNKNOWN), 1)
    return out


def render_status_block(template: str, values: dict, model_block: str) -> str:
    """최종 상태창 — 시나리오 양식 그대로, 코드 소유 칸은 정본 값, 모델 소유 칸은 모델 상태창의 같은 줄에서 가져온다.

    모델 줄은 양식 줄의 고정 문구로 찾는다(코드 소유 칸 자리는 모델이 무엇을 썼든 정본 값으로 덮는다).
    모델이 그 줄을 쓰지 않았으면 그 줄은 내지 않는다(모델 소유 값을 지어내지 않는다)."""
    import re
    model_lines = [l for l in (model_block or "").splitlines() if l.strip() and not l.strip().startswith("```")]
    used = set()
    out = []
    for tl in template.splitlines():
        names = _slots(tl)
        if not names:
            out.append(tl)
            continue
        if all(_slot_kind(n) for n in names):
            out.append(status_template_for_prompt(tl, values))
            continue
        parts = re.split(r"(\{[^{}]+\})", tl)
        pat = "".join(("(.*?)" if not _slot_kind(p[1:-1]) else ".*?") if (p.startswith("{") and p.endswith("}"))
                      else re.escape(p) for p in parts)
        got = None
        for i, ml in enumerate(model_lines):
            if i in used:
                continue
            m = re.fullmatch(r"\s*" + pat.strip() + r"\s*", ml.strip())
            if m:
                got, _i = m, i
                break
        if got is None:
            continue
        used.add(_i)
        caps, rebuilt = iter(got.groups()), []
        for p in parts:
            if p.startswith("{") and p.endswith("}"):
                k = _slot_kind(p[1:-1])
                rebuilt.append(values.get(k, STATUS_UNKNOWN) if k else next(caps).strip())
            else:
                rebuilt.append(p)
        out.append("".join(rebuilt))
    return "\n".join(out)


NARRATE_HOLD_TEXT = ("· 이번 응답(NARRATE)은 세계 시계·사건을 진행시키지 않는다. 위 정본 시각·장소에서 질문에 답하거나 "
                     "이미 있는 것을 짧게 설명·반응하는 데 그친다. 시간이 흐르거나 새 사건이 일어난 것처럼 지시·서술하지 말 것.")


def narrate_hold_block(wt, session) -> str:
    """NARRATE 경계(지시층위·경량 묘사 공용) — 정본 시각·장소 + 비진행 문구. 계획·투영·전달 대기는 싣지 않는다."""
    D = wt.ctx.definition
    minute = (getattr(session, "world_clock", {}) or {}).get("minute_index")
    cv = clock_view(D, minute)
    lines = ["\n[이번 응답 — 세계 진행 없음(정본 기준)]"]
    if cv:
        lines.append(f"· 현재 시각: {clock_text(cv)}")
    pl = _sp.placement_of(session, wt.ctx.lead_pc) or {}
    if pl.get("kind") == _sp.AT_PLACE:
        lines.append(f"· PC의 현재 장소: {_pd.display_for(D, session, wt.ctx.lead_pc, pl['place_id'], require_known=False)}")
    lines.append(NARRATE_HOLD_TEXT)
    return "\n".join(lines) + "\n"


def _state_lines(D, world, pc, prev_tl) -> list:
    # v7.25(F11): 레거시 faction_context(시작 틀·추출 문자열)는 정본이 아니다 — 정본 블록에 싣지 않는다
    tl = mirror_timeline(prev_tl, D, world, pc)
    minute = ((world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
               else getattr(world, "world_clock", {})) or {}).get("minute_index")
    cv = clock_view(D, minute)
    return [f"날짜·시각: {clock_text(cv)}" if cv else f"날짜: {tl.get('current_date', '(미확인)')}",
            f"위치: {tl.get('current_location_display') or tl.get('current_location', '(미확인)')}",
            f"시간대: {tl.get('time_of_day', '(미확인)')}"]


def instruction_world_state_block(wt, session, prev_tl, *, hold: bool) -> str:
    """지시층위용 세계 상태 — 정본(행동 시작 전)과 이번 턴 진행 후(미커밋 투영)를 구별해 준다(P3 G2).

    진행 후 상태는 묘사층위가 받는 투영(prep.world_prompt_timeline)과 같은 계획에서 만든다. NARRATE(hold)나 계획이
    없으면 정본 하나만 '현재'로 준다 — 실행하지 않을 투영을 '현재'로 소개하지 않는다.
    """
    D, pc = wt.ctx.definition, wt.ctx.lead_pc
    plan = None if hold else wt.plan
    planned = plan is not None and plan.status == _pw.PLAN_PLANNED and plan.projected_world is not None
    before = _state_lines(D, session, pc, prev_tl)
    if not planned:
        title = "현재 세계 상태 — 정본" + (" · 이번 응답은 진행 없음" if hold else "")
        return f"\n[{title}]\n" + "\n".join(before) + "\n"
    after = _state_lines(D, plan.projected_world, pc, prev_tl)
    return ("\n[세계 상태 — 행동 시작 전(정본)]\n" + "\n".join(before)
            + "\n[세계 상태 — 이번 턴 진행 후(미커밋 투영 · 묘사층위와 같은 기준) — 이번 장면의 '현재'는 이 상태]\n"
            + "\n".join(after)
            + "\n※ 이번 장면은 시작 전 상태에서 출발해 아래 [세계 진행] 결과를 거쳐 진행 후 상태로 끝난다.\n")


def derived_player_faction(world):
    """H07-C 호환 뷰: 사건 결과에서 파생한 PC 소속(읽기 전용). RESOLVED 사건의 지역 상태 grant_faction 중
    가장 최근(국면 진입 분·인스턴스 id 순). 없으면 None(기존 표시 유지). 새 병렬 권위가 아니라 사건의 파생이다."""
    ev = (world.domain("event_instances") if isinstance(world, _wm.ProjectedWorld)
          else getattr(world, "event_instances", None)) or {}
    best = None
    for iid, inst in ev.items():
        if not isinstance(inst, dict) or inst.get("lifecycle") != "RESOLVED":
            continue
        f = (inst.get("local_state") or {}).get("grant_faction")
        if not isinstance(f, str) or not f:
            continue
        key = (inst.get("phase_entered_at") or 0, iid)
        if best is None or key > best[0]:
            best = (key, f)
    return best[1] if best else None


def pc_knowledge_block(ctx, plan, session) -> str:
    """H07-C: 지시층위용 PC 인지 블록 — 레거시 info_ledger를 대신한다(정보·인지가 권위).
    PC가 CONTENT로 아는 정보의 공개 안전 요약만 싣는다(명제 원문·NPC 비밀 없음)."""
    world, scene = _scene_world_and_place(ctx, plan, session)
    recs = ((world.domain("cognition_state") if isinstance(world, _wm.ProjectedWorld)
             else getattr(world, "cognition_state", None)) or {}).get(_cog.actor_holder(ctx.lead_pc)) or {}
    st = (world.domain("information_state") if isinstance(world, _wm.ProjectedWorld)
          else getattr(world, "information_state", None)) or {}
    scene_refs = set()
    if scene is not None:
        scene_refs.add(f"place:{scene}")
        try:
            from . import npc_position as _npos
            scene_refs |= {f"actor:{aid}" for aid, _src in _npos.actors_at(ctx.definition, world, scene,
                                                                          exclude=ctx.pcs)}
        except Exception:
            pass
    ranked = []
    for iid in sorted(recs):
        r = recs[iid]
        if not isinstance(r, dict) or r.get("awareness") != _cog.CONTENT:
            continue
        if _info.effective_validity(ctx.definition, st, iid) != _info.CURRENT:
            continue                                   # v7.14: 대체·반증된 정보는 지금 사실로 싣지 않는다
        d = _info.resolve_definition(ctx.definition, st, iid) or {}
        if AMBIENT_TAG in (d.get("tags") or ()):
            continue                                   # 주변 발견은 현재 장소 블록(discovery_block)이 싣는다
        if not d.get("summary"):
            continue
        # v7.17 관련성 선택(T19): 장면 장소·인물 관련 → 필수 초기 지식 → 최근 획득. InfoId 사전순 절단 없음.
        acqs = [a for a in (r.get("acquisitions") or ()) if isinstance(a, dict)]
        initial_req = (any(str(a.get("acquisition_id") or "").startswith("initial:") for a in acqs)
                       and ((d.get("initial_knowledge") or {}).get("tier") == "required"))
        recent = max([a.get("at_minute") for a in acqs if isinstance(a.get("at_minute"), int)], default=-1)
        rank = (0 if scene_refs & set(d.get("subject_refs") or ()) else 1, 0 if initial_req else 1, -recent, iid)
        ranked.append((rank, f"· {d['summary']}" + (f" (PC 태도: {r['belief']})" if r.get("belief") else "")))
    if not ranked:
        return ""
    lines = [line for _rk, line in sorted(ranked)[:PC_KNOWLEDGE_BUDGET]]
    return ("\n[PC가 알게 된 것 — 정본 인지(노출 ≠ 믿음). 여기 없는 이면을 PC가 아는 것처럼 다루지 말 것]\n"
            + "\n".join(lines) + "\n")


PC_KNOWLEDGE_BUDGET = 12


def mirror_timeline(prev_tl: dict, definition: dict, world, pc: str) -> dict:
    """정본 세계 → 레거시 world_timeline 표시 미러(파생값 — 권위 아님).

    DYNAMIC 세션에서 장소·시간 권위는 정본 spatial_state·world_clock이다. 레거시 소비자(장소 블록·
    퀘스트 필터·디스플레이)는 이 미러를 읽는다. 추출의 장소/날짜 값은 미러에 들어가지 않는다.
    """
    tl = dict(prev_tl or {})
    pl = _sp.placement_of(world, pc) or {}
    if pl.get("kind") == _sp.AT_PLACE:
        tl["current_location"] = pl["place_id"]
    elif pl.get("kind") == _sp.TRANSIT:
        tl["current_location"] = pl["from_place"]
    if tl.get("current_location") and pl:
        # H07 §5: 플레이어에게 보이는 표시명(PC 지식 수준). current_location(PlaceId)은 내부 소비자 전용.
        tl["current_location_display"] = _pd.display_for(definition, world, pc, tl["current_location"],
                                                         require_known=False)
    minute = ((world.domain("world_clock") if isinstance(world, _wm.ProjectedWorld)
               else getattr(world, "world_clock", {})) or {}).get("minute_index")
    cv = clock_view(definition, minute)
    if cv:
        tl["current_date"], tl["time_of_day"] = cv["current_date"], cv["time_of_day"]
    return tl


def _shown(plan, definition, ctx, pid, world=None):
    """H07 §5: 묘사층위·플레이어에게 쓰는 장소 이름 = PC 지식 수준 표시명(내부 PlaceId 비노출)."""
    if pid is None:
        return None
    world = getattr(plan, "projected_world", None) or world
    pc = getattr(ctx, "lead_pc", None)
    if world is None or pc is None:
        return _pd.display_name(definition or {}, {}, pid, require_known=False)
    return _pd.display_for(definition or {}, world, pc, pid, require_known=False)


PLACE_LOCK_TEXT = ("· 이번 턴에는 정본 이동이 없다. PC는 여전히 {place}에 있다. 이 장소를 벗어나거나 "
                   "다른 장소·건물·구역에 도착·진입한 것으로 지시·묘사하지 말 것 — 이 장소 안의 작은 움직임만 다룬다. "
                   "다른 곳으로 가려면 플레이어가 보이는 출구를 선언해야 한다.")


def narration_overlay(plan, definition: dict, ctx=None, *, world=None) -> str:
    """묘사층위에 주는 코드 확정 세계 결과(PC가 관찰 가능한 것만). 장소는 PC 지식 수준 표시명.

    정본 이동이 없는 턴(NONE — 장소 안 이동 포함)도 장소 고정 문구를 준다(묘사가 정본을 따른다).
    world: 계획에 투영 세계가 없을 때 표시명 판단에 쓸 정본 세계(세션).
    """
    if plan is None:
        return ""
    nm = lambda pid: _shown(plan, definition, ctx, pid, world)
    lines = ["\n[세계 진행 — 코드 확정 결과. 아래와 다른 시간·장소·결과를 묘사하지 말 것]"]
    if plan.status == _pw.PLAN_NONE:
        if not plan.scene_place:
            return ""
        lines.append(PLACE_LOCK_TEXT.format(place=nm(plan.scene_place)))
        return "\n".join(lines) + "\n"
    o = plan.outcome or {}
    kind = o.get("kind")
    if plan.status == _pw.PLAN_REJECTED:
        if plan.reason == "NO_KNOWN_ROUTE":
            lines.append("· PC는 그곳으로 가는 길을 알지 못한다. 이동은 일어나지 않는다(길을 지어내지 말 것).")
        elif plan.reason == "NO_EXIT":
            lines.append("· 지금 PC가 나설 만한 길이 보이지 않는다. 이동은 일어나지 않는다(길을 지어내지 말 것).")
        else:
            lines.append("· 선언한 이동·시간 행동은 성립하지 않았다. 장소와 시간은 그대로다.")
        return "\n".join(lines) + "\n"
    if plan.status == _pw.PLAN_TIE:
        lines.append("· 같은 순간에 아래 일들이 겹친다. 어느 쪽이 먼저인지 **하나의 순서**로 정해 묘사하되 "
                     "결과 자체·시각은 바꾸지 말 것:")
        for k, label in tie_option_labels(plan, definition, ctx):
            lines.append(f"  - {k}: {label}")
        return "\n".join(lines) + "\n"
    if plan.status != _pw.PLAN_PLANNED:
        return ""
    if plan.code_owned_minutes:
        lines.append(f"· 경과 시간: {plan.code_owned_minutes}분")
    cv = clock_view(definition, plan.end_minute)
    if cv:
        lines.append(f"· 현재 시각: {clock_text(cv)}")
    if kind == _pw.OUT_ARRIVED:
        lines.append(f"· PC는 {nm(o.get('final_place'))}에 도착했다.")
    elif kind == _pw.OUT_INTERRUPTED_SIGNAL:
        if plan.transit:
            lines.append(f"· PC는 {nm(plan.transit['from_place'])}에서 {nm(plan.transit['to_place'])} 쪽으로 가던 길 "
                         f"위에서 멈췄다(아직 도착하지 않았다 — 목적지 모습을 묘사하지 말 것).")
        else:
            lines.append(f"· PC는 {nm(plan.scene_place)}에서 멈췄다.")
    elif kind in (_pw.OUT_INTERRUPTED_ACCESS, _pw.OUT_INTERRUPTED_BLOCKED):
        lines.append(f"· PC는 {nm(plan.scene_place)}에서 더 나아가지 못했다 — 앞길이 막혀 있음을 이제 처음 확인했다. "
                     f"우회로를 지어내지 말 것.")
    elif kind in (_pw.OUT_TIME_DONE, _pw.OUT_TIME_INTERRUPTED):
        act = o.get("activity")
        lines.append(f"· PC의 활동({act})이 " + ("중간에 끊겼다." if kind == _pw.OUT_TIME_INTERRUPTED else "끝났다."))
    elif kind in (_pw.OUT_ACTION_DONE, _pw.OUT_ACTION_INTERRUPTED):
        lines.append("· PC가 이번 행동을 하는 동안 위 경과 시간이 흘렀다"
                     + (" — 아래 일이 끼어들어 행동이 중간에 끊겼다." if kind == _pw.OUT_ACTION_INTERRUPTED else "."))
    sigs = observable_signals(plan.signals)
    if sigs:
        lines.append("[필수 서사 — 이번 본문에 반드시 담을 것(PC가 직접 보고 듣는 일). 표현은 자유, 사실·순서는 바꾸지 말 것]")
    for sig in sigs:
        lines.append(f"· 지금 관찰되는 일: {sig['cue']}")
    return "\n".join(lines) + "\n"


def pending_signals(session) -> list:
    return [r for r in (getattr(session, "world_pending_signals", None) or []) if isinstance(r, dict) and r.get("cue")]


def pending_delivery_block(ctx, session) -> str:
    """P1: 지난 본문이 전하지 못한 관찰된 일 — 이번 본문 앞부분에 '그동안' 일어난 일로 담는다(새로 일어난 일처럼 쓰지 않음)."""
    rows = pending_signals(session)
    if not rows:
        return ""
    lines = ["\n[필수 서사 — 직전 장면에서 이미 일어났으나 아직 전하지 못한 일. 이번 본문 앞부분에 '그사이' 있었던 일로 담을 것. "
             "지금 막 일어난 일처럼 쓰지 말 것]"]
    cal = (ctx.definition if ctx is not None else {}) or {}
    for r in rows:
        cv = clock_view(cal, r.get("at_minute")) if r.get("at_minute") is not None else {}
        where = []
        if cv:
            where.append(f"작중 {clock_text(cv)}")
        if r.get("scene_place") and ctx is not None:               # 발생 장소 — 지금 장소의 일처럼 쓰지 않게(v7.21.1)
            where.append(f"장소: {_pd.display_for(cal, session, ctx.lead_pc, r['scene_place'], require_known=False)}")
        lines.append(f"· {r['cue']}" + (f" ({' · '.join(where)})" if where else ""))
    return "\n".join(lines) + "\n"


def observable_signals(signals) -> list:
    out, seen = [], set()
    for s in signals or ():
        if s.get("observable") and s.get("interrupt_policy") == "IF_OBSERVABLE":
            k = (s.get("signal_id"), s.get("instance_id"))
            if k not in seen:
                seen.add(k)
                out.append(s)
    return out


def projected_scene(plan) -> dict:
    """ProjectedMediaContext의 장면 부분 — 묘사가 실제로 표현하는 장소."""
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return {"scene_place_id": None, "transit": None}
    return {"scene_place_id": plan.scene_place, "transit": plan.transit}


# ══════════════════════════════════════════════════════════════
#  현재 장면 NPC — 정본 배치 + H04 CurrentNpcProjection (H05 R3)
#   · 현재 위치 권위 = 유효 위치(H07 core.npc_position: 정본 override > 평상시 일과 > untracked). place.npcs(연고)·session.npcs는 존재 권위가 아니다.
#   · AT_PLACE == 장면 장소인 NPC만. 기록 없음(추적 안 함)·TRANSIT은 장면에 있다고 추론하지 않는다.
#   · 투영은 project_npc(읽기 전용) — relevant_info_ids=()로 인지·명제는 싣지 않는다(공개 규칙 유지).
# ══════════════════════════════════════════════════════════════

def _scene_world_and_place(ctx, plan, session):
    """(world, scene_place). 묘사 전 계획이 PLANNED면 투영 장면, 아니면 정본 PC 위치."""
    if plan is not None and plan.status == _pw.PLAN_PLANNED and plan.projected_world is not None:
        return plan.projected_world, plan.scene_place                  # TRANSIT 중단이면 None(장소 없음)
    pl = _sp.placement_of(session, ctx.lead_pc) or {}
    return session, (pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None)


def _raw_place(definition, pid) -> dict:
    return ((((definition.get("legacy_source") or {}).get("scenario") or {}).get("places") or {}).get(pid)) or {}


def dynamic_place_block(ctx, plan, session, *, for_narration: bool = False) -> str:
    """H07: DYNAMIC 지시층위용 장소 블록 — 정본 장소·구간·PC 지식 기준(레거시 connected·reachable 홉 은퇴).

    · 현재 장소: 플레이어 표시명 + (다르면) 내부 이름과 '이름 사용 규칙'. 지시층위(GM 권한)만 내부 이름을 본다.
    · 보이는 출구: 현재 장소에서 나가는 구간 중 PC가 아는 것(도착·시작 시 PUBLIC/LOCAL 출구 학습 포함).
      RESTRICTED/SECRET은 발견했을 때만. 표시명은 PC 지식 수준, 미방문·이름 모름이면 unknown_hint만.
    · 연고 인물(places[*].npcs)은 싣지 않는다 — 현재 장면 인물은 scene_npc_block(유효 위치)이 준다.
    """
    world, scene = _scene_world_and_place(ctx, plan, session)
    if scene is None:
        return ""
    D = ctx.definition
    know = _sp.actor_knowledge(world, ctx.lead_pc)
    shown = _pd.display_name(D, know, scene, require_known=False)
    lines = ["\n[현재 장소 — 정본]", shown]
    if shown != scene and not for_narration:          # 내부 이름은 지시층위(GM 권한)만 본다
        lines.append(f"(내부 이름: {scene} — PC는 아직 이 이름을 모른다. 묘사·질문에서 내부 이름·소속을 쓰지 말고 "
                     f"'{shown}'처럼 PC가 아는 만큼만 부를 것)")
    node = _raw_place(D, scene)
    for key, label in (("location_desc", "위치"), ("exterior", "외부"), ("interior", "내부")):
        if node.get(key):
            mood = node.get(f"mood_{key}") if key != "location_desc" else None
            lines.append(f"{label}: {node[key]}" + (f" ({mood})" if mood else ""))
    for up in (node.get("inherit") or []):
        un = _raw_place(D, up)
        desc = " / ".join(str(un[k]) for k in ("location_desc", "exterior") if un.get(k))
        if desc:
            lines.append(f"\n[상위 맥락 — {_pd.display_name(D, know, up, require_known=False)}]\n{desc}")
    exits = []
    for eid, e in sorted((D.get("movement_edges") or {}).items()):
        if e.get("from") != scene or not _sp.knows_edge(know, eid):
            continue
        to = e["to"]
        nm = _pd.display_name(D, know, to, require_known=False)
        rec = (know.get("places") or {}).get(to) or {}
        tn = _raw_place(D, to)
        if rec.get("visited") and _pd.knows_true_name(D, know, to):
            brief, mark = tn.get("known_brief") or tn.get("location_desc") or "", "방문함"
        else:
            brief, mark = tn.get("unknown_hint") or "아직 가 보지 않은 곳이다.", "미방문"
        via = f" — {e['route_hint']}" if e.get("route_hint") else ""
        exits.append(f"· {nm}{via} ({mark}) — {brief}")
    if exits:
        lines.append("\n[보이는 출구 — PC 지식 기준]")
        lines += exits                                 # v7.22(F4): 앞 10개로 자르지 않는다 — 모든 출구가 지목 가능해야 한다
        lines.append("미방문 장소는 확정적으로 묘사하지 말 것. 이름을 모르는 곳은 표시된 이름으로만 부를 것.")
    return "\n".join(lines) + "\n"


def visible_exit_names(ctx, session) -> list:
    """현재 장소에서 PC가 아는 출구의 표시명(PC 지식 수준, 중복 제거·전부)."""
    out = []
    for _eid, to, _label, _perc in _ai.exit_candidates(ctx.definition, session, ctx.lead_pc):
        nm = _pd.display_name(ctx.definition, _sp.actor_knowledge(session, ctx.lead_pc), to, require_known=False)
        if nm and nm not in out:
            out.append(nm)
    return out


def session_is_dynamic(session) -> bool:
    """v7.23: DYNAMIC 세션인가(gm WorldTurn 생성과 같은 판정) — 예상 비용 등 코드 밖 소비자용. 실패는 False."""
    try:
        return bool(_pw.session_world_context(session).dynamic)
    except Exception:
        return False


def move_question_anchor(ctx, session) -> dict:
    """v7.22 목적지 질문 문맥의 기준선 — PC 정본 장소(AT_PLACE만)·world_version. 다르면 문맥은 낡았다."""
    pl = _sp.placement_of(session, ctx.lead_pc) or {}
    return {"place": pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None,
            "world_version": _ai.world_version_id(session)}


COMPASS = ("북쪽", "북동쪽", "동쪽", "남동쪽", "남쪽", "남서쪽", "서쪽", "북서쪽")
QUESTION_LIST_MAX = 6                              # 이보다 많은 출구는 방향별 갈래 수로 줄여 말한다(지목은 전부 가능)


def _exit_phrase(definition, know, edge, to) -> str:
    """route_hint(방향 · 경사 · 길 형태) → '북쪽 도로(오르막) — 저 너머 이름 쪽'. 저작이 없으면 이름만."""
    name = _pd.display_name(definition, know, to, require_known=False)
    parts = [p.strip() for p in str(edge.get("route_hint") or "").split("·") if p.strip()]
    if len(parts) == 3:
        d, slope, surface = parts
        return f"{d} {surface}" + ("" if slope == "평탄" else f"({slope})") + f" — {name} 쪽"
    return f"{name} 쪽 길"


def destination_question(ctx, session, edge_ids=None) -> tuple:
    """v7.22: 목적지를 정하지 않은 이동에 묻는 말과 제시한 출구 edge_id들.

    edge_ids가 주어지면(모호한 지목) 그 후보만 묻는다. 출구가 많으면 방향별 갈래 수로 말한다 — 생략한 길도
    방향·길 형태로 지목할 수 있다(판단 후보에는 전부 있다). '아무 데나' 같은 선택지는 따로 만들지 않는다.
    """
    from .master import say
    D = ctx.definition
    know = _sp.actor_knowledge(session, ctx.lead_pc)
    rows = [r for r in _ai.exit_candidates(D, session, ctx.lead_pc) if not edge_ids or r[0] in set(edge_ids)]
    q = say("turn.destination_question", "어디로 가시겠습니까?")
    if not rows:
        return q + " 가려는 곳을 구체적으로 말씀해 주십시오.", ()
    edges = D.get("movement_edges") or {}
    if len(rows) <= QUESTION_LIST_MAX:
        text = q + " 지금 보이는 길은 " + ", ".join(_exit_phrase(D, know, edges[e], to) for e, to, _l, _p in rows) + "입니다."
    else:
        dirs = {}
        for e, _to, _l, _p in rows:
            d = (str(edges[e].get("route_hint") or "").split("·")[0].strip()) or "그 밖"
            dirs[d] = dirs.get(d, 0) + 1
        rank = {d: i for i, d in enumerate(COMPASS)}
        text = (q + " 길이 여러 갈래로 나 있습니다 — " + ", ".join(
                    f"{d}으로 {n}갈래" for d, n in sorted(dirs.items(), key=lambda x: (rank.get(x[0], 99), x[0])))
                + ". 방향이나 길의 모양으로 말씀해 주십시오.")
    return text, tuple(r[0] for r in rows)


def narration_world_block(wt, session) -> str:
    """DYNAMIC 묘사층위용 세계 블록 — 정본 장소·출구 + 코드 확정 결과(장소 고정 포함) + 현재 장면 인물.

    GM 지시문은 공백이 접혀 한 문단이 되므로, 세계 제약은 지시문 뒤가 아니라 독립 블록으로 준다.
    """
    if wt is None:
        return ""
    parts = [narration_clock_block(wt, session), dynamic_place_block(wt.ctx, wt.plan, session, for_narration=True)]
    if wt.plan is not None:
        parts.append(narration_overlay(wt.plan, wt.ctx.definition, wt.ctx, world=session))
    parts.append(pending_delivery_block(wt.ctx, session))
    parts.append(scene_npc_block(wt.ctx, wt.plan, session))
    parts.append(scene_item_block(wt, session))
    return "".join(p for p in parts if p)


def scene_item_block(wt, session) -> str:
    """v7.34(A5): 묘사층위도 지시층위와 같은 정본 물건 블록을 받는다(PC·장면 인물 소지·현장 물건·닫힌 용기)
    — 없는 물건을 쓰거나 손상 물건을 멀쩡히 쓰는 묘사를 막는다(묘사 뒤 물건 연산 거부의 원인)."""
    from . import items as _items
    try:
        ctx = wt.ctx
        scene, projs = scene_npc_projections(ctx, wt.plan, session)
        pc = ((session.players or {}).get(ctx.uid_of.get(ctx.lead_pc)) or {}).get("name")
        if not pc:
            return ""
        return _items.item_block(session, pc_name=pc,
                                 scene_owner_names=[p.get("legacy_name") for p in projs if p.get("legacy_name")],
                                 scene_place=scene, defs=_items.definitions_of(ctx.definition))
    except Exception as e:  # noqa: BLE001 — 보조 블록(실패해도 묘사는 진행)
        print(f"[묘사] 물건 블록 실패: {e}")
        return ""


MAX_GUIDE_LINES = 8


# v7.37(A6): 국면 guide는 국면이 이어지는 동안 매 턴 같은 문장으로 들어간다 — 이미 보인 단서의 반복 서술을 막는다
GUIDE_REPEAT_RULE = ("· 이 지침의 단서·징후가 앞선 묘사(최근 기록)에 이미 나왔다면 상황이 달라졌을 때만 다시 다룬다 — "
                     "같은 단서를 같은 말로 되풀이하라고 지시하지 말고, 이번 턴 플레이어 행동이 닿는 것에 집중시킨다.")


def event_guide_block(ctx, plan, session) -> str:
    """H07 R-2: 진행 중 사건의 현재 국면 guide — **지시층위 전용**(묘사·추출·판단층위에는 주지 않는다).

    · 장면과 닿는 사건만(relevance_places — 없으면 장소에 묶이지 않은 세계 압력).
    · guide는 정본 결과·세계 사실을 정하지 않는다 — 현재 국면의 연출·맥락·운영 지침이다.
    · 결단 선택지가 열린 국면이면 선택지를 보여 주되, 고르는 것은 플레이어의 명시 선언뿐이다(GM이 대신 고르지 않음).
    """
    from . import events as _ev
    world, scene = _scene_world_and_place(ctx, plan, session)
    D = ctx.definition
    ev = (world.domain("event_instances") if isinstance(world, _wm.ProjectedWorld)
          else getattr(world, "event_instances", None)) or {}
    lines = []
    found = unknown_finds(D, world, ctx.lead_pc, scene)
    istate = (world.domain("information_state") if isinstance(world, _wm.ProjectedWorld)
              else getattr(world, "information_state", None)) or {}
    for iid in sorted(ev):
        inst = ev[iid]
        if not isinstance(inst, dict) or inst.get("lifecycle") != _ev.ACTIVE:
            continue
        tpl = _ev.template(D, inst.get("template_id")) or {}
        ph = (tpl.get("phases") or {}).get(inst.get("current_phase")) or {}
        clues = [f for i, f in found if i == iid]
        if not (ph.get("guide") or clues) or not (_ev.instance_relevant(D, inst, scene) or clues):
            continue
        lines.append(f"· {tpl.get('name') or inst.get('template_id')}: {ph.get('guide') or '(조사 단서만)'}")
        names = [x for x in ph.get("finds") or () if isinstance(x, str)]
        if names:                                    # v7.38(A3): 저작 후보 중 이 사건이 품은 하나(인스턴스 결정적)
            pick = names[int(hashlib.sha256(str(iid).encode("utf-8")).hexdigest()[:8], 16) % len(names)]
            lines.append(f"  └ 여기서 찾을 수 있는 쓸 만한 것(코드 확정): {pick} — 플레이어가 실제로 찾아 나설 때만 "
                         "드러낸다. 다른 쓸 만한 물건을 지어내지 말 것")
        for f in clues:                              # v7.44(E4): 조사하면 드러나는 저작 단서 — 정본(PC가 아직 모르는 것만)
            summ = (_info.resolve_definition(D, istate, f["info"]) or {}).get("summary")
            lines.append(f"  └ 이 장면에서 조사하면 드러날 수 있는 단서(코드 확정): {summ}"
                         + (f" — 실물: {f['object']}" if f.get("object") else "")
                         + (f" — {f['actor']}에게 묻거나 그를 살필 때" if f.get("actor") else "")
                         + " · 플레이어가 그 대상을 직접 살필 때만 드러내고, 판정이 필요하면 판단층위가 정한다. "
                           "다른 결정적 단서를 지어내지 말 것")
        res = (inst.get("local_state") or {}).get(_ev.RESOLUTION_KEY)
        opts = {o["option_id"]: o["label"] for o in ph.get("resolution_options") or ()}
        if opts and res is None and _ev.resolution_relevant(D, world, inst, scene):   # player-facing 관련성 gate
            lines.append("  └ 플레이어가 명시적으로 내릴 수 있는 결단: " + " / ".join(opts.values())
                         + " — GM·묘사가 대신 고르거나 유도하지 말 것")
        if len(lines) >= MAX_GUIDE_LINES:
            break
    if not lines:
        return ""
    return ("\n[진행 중인 사건 — 지시층위 전용 지침(정본 결과·세계 사실을 정하지 않는다. 플레이어의 자유 행동을 "
            "이 사건의 분기로 억지 해석하지 말 것)]\n" + "\n".join(lines) + "\n" + GUIDE_REPEAT_RULE + "\n")


def unknown_finds(definition: dict, world, pc: str, scene) -> list:
    """v7.44(E4): 지금 장면에서 찾을 수 있고 PC가 아직 내용을 모르는 저작 단서 [(instance_id, find)]."""
    from . import events as _ev
    cog = ((world.domain("cognition_state") if isinstance(world, _wm.ProjectedWorld)
            else getattr(world, "cognition_state", None)) or {}).get(_cog.actor_holder(pc)) or {}
    return [(i, f) for i, f in _ev.active_finds(definition, world, scene)
            if (cog.get(f["info"]) or {}).get("awareness") != _cog.CONTENT]


def find_object_names(definition: dict, world, pc: str, scene) -> list:
    """v7.45.4: 지금 장면에서 찾을 수 있고 PC가 아직 모르는 저작 단서의 실물 이름(선언 검증·판단 입력 — 요약 없음).

    플레이어가 이 물건을 지목해 찾으면 주변 발견 제안이 아니라 저작 단서 조사다(관찰 매니페스트 X키로 드러난다)."""
    return list(dict.fromkeys(f["object"] for _i, f in unknown_finds(definition, world, pc, scene)
                              if isinstance(f.get("object"), str) and f["object"].strip()))


def find_object_ops(definition: dict, world, pc: str, scene, exposed, *, prefix: str, transaction_id: str,
                    at_minute, place_name: str) -> list:
    """v7.44(E4): 이번 턴 드러난 저작 단서 중 실물(object)이 있는 것 → 그 자리의 주변 발견 물건(D키 수거 대상)으로.

    발견과 같은 정보·인지 경로(RUNTIME 정보 + PC 인지)라 이후 줍기·소진은 기존 D키 경로가 처리한다."""
    from . import events as _ev
    ops = []
    st = (world.domain("information_state") if isinstance(world, _wm.ProjectedWorld) else
          getattr(world, "information_state", None)) or {}
    hits = {f["info"]: f for _i, f in _ev.active_finds(definition, world, scene) if f.get("object")}
    for n, iid in enumerate(sorted(set(exposed) & set(hits))):
        f = hits[iid]
        rid = "rt.find." + stable_digest("find.object.v1", iid, scene, length=12)
        if _info.exists(definition, st, rid):
            continue
        create = _wm.make_op(f"{prefix}.fo{n}", _info.OP_INFORMATION_CREATE, "OBSERVATION", {
            "definition": ambient_record(rid, f["object"], "OBJECT", scene, place_name, at_minute)})
        ops.append(create)
        ops.append(_wm.make_op(f"{prefix}.fa{n}", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
            "holder": _cog.actor_holder(pc), "info_id": rid, "awareness": _cog.CONTENT,
            "acquisition": _cog.acquisition_record(f"turn:{transaction_id}:find:{rid}", "WITNESS",
                                                   at_minute=at_minute)}, causal_parent_op_ids=(create.op_id,)))
    return ops


def notebook_text(session, uid) -> str | None:
    """v7.44(E5) 수첩 — 시나리오가 `knowledge_notebook`({title?, groups:[{tag, label}]})을 저작했을 때만.

    누른 플레이어 PC가 내용까지 아는(CONTENT) 정보를 저작 태그별로 묶어 공개 안전 요약(summary)으로 보인다.
    정본 읽기만(호출·인지·비용 0). 태그가 없는 정보·다른 PC는 보이지 않는다."""
    cfg = (getattr(session, "scenario_data", None) or {}).get("knowledge_notebook")
    groups = (cfg or {}).get("groups") if isinstance(cfg, dict) else None
    if not groups:
        return None
    wctx = _pw.session_world_context(session)
    if wctx.mode != _pw.MODE_DYNAMIC or wctx.uid_of.get(wctx.lead_pc) != str(uid):
        return None
    st = getattr(session, "information_state", None) or {}
    recs = (getattr(session, "cognition_state", None) or {}).get(_cog.actor_holder(wctx.lead_pc)) or {}
    rows = {}
    for iid in sorted(recs):
        if not isinstance(recs[iid], dict) or recs[iid].get("awareness") != _cog.CONTENT:
            continue
        d = _info.resolve_definition(wctx.definition, st, iid) or {}
        summ = d.get("summary")
        if isinstance(summ, str) and summ.strip():
            for tag in d.get("tags") or ():
                rows.setdefault(tag, []).append(summ.strip())
    parts = []
    for g in groups:
        if isinstance(g, dict) and rows.get(g.get("tag")):
            parts.append(f"**{g.get('label') or g['tag']}**\n" + "\n".join(f"· {x}" for x in rows[g["tag"]]))
    title = (cfg.get("title") if isinstance(cfg.get("title"), str) else None) or "수첩"
    return f"**{title}**\n\n" + ("\n\n".join(parts) if parts else "아직 적어 둔 것이 없습니다.")


def ending_block(ctx, plan, session) -> str:
    """v7.44(E3): 이번 턴에 이야기가 결말 국면에 닿으면 — 지시층위 전용 맺음 지침(묘사 뒤 세션이 닫힌다)."""
    from . import events as _ev
    if plan is None or plan.status != _pw.PLAN_PLANNED or plan.projected_world is None:
        return ""
    ends = _ev.new_endings(ctx.definition, session, plan.projected_world)
    if not ends:
        return ""
    lines = [f"· {e['title']}" + (f"({e['tone']})" if e.get("tone") else "") + f": {e['epilogue']}" for e in ends]
    return ("\n[결말 — 지시층위 전용. 이번 묘사가 이 이야기의 마지막이다. 아래 지침대로 맺고, 다음 행동을 묻지 않는다]\n"
            + "\n".join(lines) + "\n")


def session_ending(session) -> dict | None:
    """v7.44(E3): 커밋된 정본이 저작 결말 국면에 닿았으면 그 결말(첫 하나), 아니면 None(동적 세계 세션만).

    v7.48(E2): PC에게 결말을 가진 상태(사망 등)가 붙었으면 그 결말이 먼저다(시나리오 status_endings로 문안 덮어쓰기)."""
    from . import events as _ev
    wctx = _pw.session_world_context(session)
    if wctx.mode != _pw.MODE_DYNAMIC:
        return None
    end = pc_status_ending(session)
    if end is not None:
        return end
    ends = list(_ev.endings_in(wctx.definition, session).values())
    return ends[0] if ends else None


def pc_status_ending(session) -> dict | None:
    """v7.48(E2): PC 상태 결말 — 공용·시나리오 상태의 ending(시나리오 status_endings[상태]가 문안을 덮어쓴다)."""
    from .status_rules import status_ending
    from .utils import get_merged_status_effects
    sd = getattr(session, "scenario_data", None) or {}
    pc = _session_pc_name(session)
    end = status_ending(get_merged_status_effects(sd), (getattr(session, "statuses", None) or {}).get(pc) or ())
    if end is None:
        return None
    over = (sd.get("status_endings") or {}).get(end["status"])
    return {**end, **over} if isinstance(over, dict) else end


def threat_block(ctx, plan, session) -> str:
    """v7.48(E3): 지시층위 — 추적자 위협(코드 확정). 같은 장소 = 등장 강제, 한 구간 거리에서 추적·경계 = 다가오는 징후.
    그 밖은 아무것도 쓰지 않는다(위치 유출 금지)."""
    from . import pursuit as _pu
    from . import status_rules as _sr
    if not _pu.configs(ctx.definition):
        return ""
    world = plan.projected_world if plan is not None and getattr(plan, "projected_world", None) is not None else session
    pl = _sp.placement_of(world, ctx.lead_pc) or {}
    here = pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None
    hidden = pc_has_role(session, _sr.ROLE_CONCEALED)
    lines = []
    for t in _pu.threats(ctx.definition, world, pc_place=here):
        if t["relation"] == "SAME" and hidden:
            lines.append(f"· {t['name']}이(가) 이 장소에 있다 — PC는 숨어 있다. 그가 주변을 뒤진다. 들켰는지는 판정 전까지 "
                         "확정하지 말 것(긴장만 묘사).")
        elif t["relation"] == "SAME":
            lines.append(f"· {t['name']}이(가) 이 장소에 나타난다(반드시 등장 — 어떻게 나타나는지는 묘사 재량). PC는 그와 "
                         "마주쳤다. PC의 반응·결과는 확정하지 말 것.")
        else:
            lines.append(f"· {t['name']}이(가) 바로 옆까지 다가오고 있다 — 발소리·문소리 같은 징후만(모습은 아직 보이지 않음).")
    if not lines:
        return ""
    return "\n[위협 — 코드 확정]\n" + "\n".join(lines) + "\n"


def dying_block(session, plan) -> str:
    """v7.48(E2): 지시층위 — PC가 죽어 가는 상태(역할 dying)면 결말 상태까지 남은 시간(코드 확정)."""
    from . import status_rules as _sr
    from .utils import get_merged_status_effects
    merged = get_merged_status_effects(getattr(session, "scenario_data", None) or {})
    pc = _session_pc_name(session)
    have = (getattr(session, "statuses", None) or {}).get(pc) or ()
    dying = next((n for n in _sr.with_role(merged, _sr.ROLE_DYING) if n in have), None)
    if dying is None:
        return ""
    _end, limit = _sr.death_timer(merged, dying)
    at = (((getattr(session, "status_clock", None) or {}).get(pc) or {}).get("applied") or {}).get(dying)
    now = getattr(plan, "end_minute", None) if plan is not None else None
    if now is None:
        now = (getattr(session, "world_clock", None) or {}).get("minute_index")
    if at is None or now is None or not limit:
        return ""
    care = "·".join(sorted((merged[dying].get("care") or {}))) or "처치"
    left = int(at) + limit - int(now)
    if left <= 0:
        return (f"\n[{dying} — 코드 확정] PC는 이번 턴 안에 {care}를 받지 못하면 숨진다. 처치가 묘사되지 않으면 이 묘사가 "
                "PC의 마지막 장면이다(죽음을 서둘러 확정하지는 말 것 — 확정은 코드가 한다).\n")
    return f"\n[{dying} — 코드 확정] PC는 쓰러져 있다. {care} 없이 약 {left}분이 지나면 숨진다. 몸을 거의 움직일 수 없다.\n"


def _session_pc_name(session):
    return next((p.get("name") for p in (getattr(session, "players", None) or {}).values()
                 if isinstance(p, dict) and p.get("name")), None)


def pc_has_role(session, role: str) -> bool:
    """PC에게 그 역할(status_rules.ROLES)의 상태가 붙어 있는가 — 상태를 이름이 아니라 역할로 읽는 공용 경계."""
    from . import status_rules as _sr
    from .utils import get_merged_status_effects
    merged = get_merged_status_effects(getattr(session, "scenario_data", None) or {})
    return _sr.has_role(merged, (getattr(session, "statuses", None) or {}).get(_session_pc_name(session)) or (), role)


def scene_npc_projections(ctx, plan, session) -> tuple:
    """(scene_place, [CurrentNpcProjection]) — 유효 위치가 장면 장소인 NPC만(정렬).

    H07: 유효 위치 = 정본 override > 평상시 일과(파생) > untracked(core.npc_position — 사건 조건과 같은 경계).
    일과로 나타난 인물을 장면에 올려도 정본 배치를 만들지 않는다(materialize 없음).
    """
    from . import npc_position as _npos
    from . import npc_projection as _np
    world, scene = _scene_world_and_place(ctx, plan, session)
    if scene is None:
        return None, []
    legacy = _np.LegacyRuntime.from_session(session)
    out = []
    for aid, _src in _npos.actors_at(ctx.definition, world, scene, exclude=ctx.pcs):
        proj = _np.project_npc(aid, ctx.definition, world, relevant_info_ids=(), legacy_runtime=legacy)
        if proj is not None and proj.get("kind") != "PC":
            out.append(proj)
    return scene, out


def scene_npc_names(ctx, plan, session) -> tuple:
    scene, projs = scene_npc_projections(ctx, plan, session)
    return scene, [p["display_name"] for p in projs]


def _npc_line(p) -> str:
    bits = []
    if p.get("relevant_statuses"):
        bits.append("상태 " + ", ".join(map(str, p["relevant_statuses"])))
    if p.get("relevant_resources"):
        bits.append("자원 " + ", ".join(f"{k} {v}" for k, v in p["relevant_resources"].items()))
    if (p.get("companionship") or {}).get("is_companion"):
        bits.append("PC 동행")
    for r in p.get("event_roles") or ():
        bits.append(f"사건 역할 {r.get('slot')}(국면 {r.get('phase')})")
    deltas = [d.get("summary") for d in (p.get("relationship_deltas") or ()) if d.get("summary")]
    if deltas:
        bits.append("관계 변화 " + " / ".join(deltas))
    role = (p.get("authored_profile") or {}).get("role")
    head = f"- {p['display_name']}" + (f"({role})" if role else "")
    return head + (": " + " · ".join(bits) if bits else "")


def scene_npc_block(ctx, plan, session) -> str:
    """지시·묘사층위용 현재 장면 인물 블록(정본 배치 기준 · 현재 상태 투영)."""
    scene, projs = scene_npc_projections(ctx, plan, session)
    world, _ = _scene_world_and_place(ctx, plan, session)
    # v7.25: 장면 이름도 PC 지식 수준 표시명 — 내부 PlaceId는 묘사층위에 주지 않는다
    where = (_pd.display_for(ctx.definition, world, ctx.lead_pc, scene, require_known=False)
             if scene else "이동 중(장소 밖)")
    lines = [f"\n[현재 장면 인물 — 정본 위치 기준 · 장면: {where}]",
             "※ 정의된 NPC 중 이 목록의 인물만 지금 이 장면에 있다. 장소의 연고 인물·NPC 사전의 거처는 "
             "현재 위치가 아니다 — 목록에 없는 정의 NPC를 이 장면에 있다고 묘사하지 말 것.",
             "※ 목록 밖 정의 인물(룰북 인물 사전의 이름)을 이름·정체로 등장시키지 말 것. 새로 필요한 인물은 "
             "이름 없는 인물로 다룬다."]
    from . import speaker_card as _card
    if _card.card_world(session) is not None:
        lines.append(MARKER_NAME_RULE)
    lines += [_npc_line(p) for p in projs] or ["(정본상 이 장면에 있는 정의 NPC 없음)"]
    return "\n".join(lines) + "\n" + scene_npc_detail_block(projs, session)


# v7.35(U2-a): 대사 마커 이름 칸은 식별용이다 — 화면 표시(이름·???·카드)는 코드가 PC 인지에 맞춰 정한다.
MARKER_NAME_RULE = ("※ 이 목록 인물의 대사 마커(@대사:이름|)는 이름 칸에 목록의 이름을 그대로 쓴다 — PC가 아직 모르는 "
                    "이름이면 화면에서 코드가 가린다(???). 본문 서술의 호칭과 대사 내용에는 PC가 아는 이름·호칭만 쓴다. "
                    "이름 없는 인물의 마커는 짧은 호칭(예: 경비)으로 쓴다.")


RELATION_STANCE_RULE = ("PC를 대하는 지금 태도(관계 정본에서 코드가 고름 — 단계 이름·수치를 본문에 쓰지 말고 행동으로만 드러낸다)")


def _persona_now(session, definition, persona) -> dict | None:
    """조건이 참인 첫 '지금' 항목(없으면 when 없는 기본). 조건은 사건과 같은 3값 조건식 — TRUE일 때만."""
    from . import predicates as _pred
    from . import world_refs as _refs
    rows = [r for r in (persona or {}).get("now") or () if isinstance(r, dict)]
    for r in rows:
        if "when" in r and definition is not None:
            try:
                if _pred.evaluate(r["when"], _refs.EvalContext(definition, session)) == _pred.TRUE:
                    return r
            except Exception:  # noqa: BLE001 — 평가 불가 = 해당 없음
                continue
    return next((r for r in rows if "when" not in r), None)


def persona_runtime_lines(session, actor_id, persona) -> list:
    """v7.39 동적 세계 장면 인물 — 관계 단계별 태도 한 줄 · 지금 · 이 인물이 기억하는 PC 일 최근 3건."""
    from . import world_refs as _refs
    from .cache import now_text
    if not isinstance(persona, dict) or not actor_id:
        return []
    pc = _refs.session_pc_actor(session)
    if not pc:
        return []
    st = _rel.trust_state(session, actor_id, pc, persona.get("initial_trust") or 0)
    stances = persona.get("stances") or {}
    out = []
    stance = stances.get(st["stage"]) or stances.get("known" if st["stage"] != "stranger" else "stranger")
    if stance:
        out.append(f"  {RELATION_STANCE_RULE}: {stance}")
    rev = getattr(session, "definition_revision", None)
    row = _persona_now(session, _pw.load_definition(session, rev) if rev else None, persona)
    if row:
        out.append("  지금: " + now_text(row))
    if st["memories"]:
        out.append("  이 인물이 기억하는 PC와의 일(최근순): " + " / ".join(reversed(st["memories"][-3:])))
    return out


STAGE_LABELS = _rel.STAGE_LABELS


def relation_changes(definition, before, after) -> list:
    """관계 정본 두 시점(relationship_state) → [(NPC ActorId, 이전 상태, 이후 상태)] — 인물 → PC 방향만(순수)."""
    from types import SimpleNamespace
    a, b = before or {}, after or {}
    out = []
    for key in sorted(b):
        if a.get(key) == b.get(key):
            continue
        rel = b[key] or {}
        frm, to = str(rel.get("from_holder") or ""), str(rel.get("to_holder") or "")
        if not (frm.startswith("actor:") and to.startswith("actor:")):
            continue
        npc, pc = frm[6:], to[6:]
        persona = ((((definition or {}).get("actor_definitions") or {}).get(npc) or {}).get("profile_base")
                   or {}).get("persona") or {}
        init = persona.get("initial_trust") or 0
        out.append((npc, _rel.trust_state(SimpleNamespace(relationship_state=a), npc, pc, init),
                    _rel.trust_state(SimpleNamespace(relationship_state=b), npc, pc, init)))
    return out


def relation_judgment_block(ctx, plan, session) -> str:
    """v7.39(결정 N3): 판단층위 — 장면 인물이 PC를 대하는 관계 단계(설득·거래·부탁 판정의 상황 보정 근거)."""
    _scene, projs = scene_npc_projections(ctx, plan, session)
    rows = []
    for p in projs:
        persona = (p.get("authored_profile") or {}).get("persona") or {}
        st = _rel.trust_state(session, p["actor_id"], ctx.lead_pc, persona.get("initial_trust") or 0)
        rows.append(f"- {p['display_name']}: {_rel.STAGE_LABELS[st['stage']]}")
    if not rows:
        return ""
    return ("[장면 인물이 PC를 대하는 관계 — 설득·거래·부탁 같은 사회적 판정의 상황 보정(H) 근거. "
            "단계 이름을 플레이어에게 말하지 말 것]\n" + "\n".join(rows))


def scene_npc_detail_block(projs, session) -> str:
    """v7.34(사용자 결정 4): 정본상 현재 장면에 있는 인물(동행 포함 — 동행은 PC와 같은 자리로 움직인다)의 추가 설정.

    캐시 NPC 사전에는 정체·인상만 있다. 행동 방식·동기·두려움·비밀·관계·특기·능력치는 여기서만 준다
    (장면 밖 인물의 비밀은 그 턴에 들어가지 않는다)."""
    from .cache import NPC_PART_EXTRA, npc_field_lines
    sd = getattr(session, "scenario_data", None) or {}
    npcs, tmpl = sd.get("default_npcs") or {}, sd.get("npc_template") or {}
    out = []
    for p in projs or ():
        data = npcs.get(p.get("legacy_name")) if p.get("legacy_name") else None
        if not isinstance(data, dict):
            continue
        rows = npc_field_lines(tmpl, data, part=NPC_PART_EXTRA, bound=True)
        rows += persona_runtime_lines(session, p.get("actor_id"), data.get("persona"))
        if rows:
            out.append(f"- {p.get('legacy_name')}:\n" + "\n".join(rows))
    if not out:
        return ""
    return ("\n[장면 인물 상세 — 룰북 NPC 사전(정체·인상)에 더하는 추가 설정 · 인물을 일관되게 움직이기 위한 내부 기준. "
            "PC가 모르는 비밀·숨은 관계·실명을 본문 서술·대사 내용에 드러내지 말 것(대사 마커의 이름 칸은 예외)]\n" + "\n".join(out) + "\n")


# ══════════════════════════════════════════════════════════════
#  v7.25(F03) 동행 — 관계(레거시 companions)와 위치(정본 배치)를 분리해 정합시킨다
# ══════════════════════════════════════════════════════════════
#   · 합류(추출 joined)는 '제안'이다 — 그 인물이 정본 등록 행위자이고 이번 턴 끝(투영)에 PC와 같은 장소에
#     있을 때만 관계로 확정한다. 다른 곳에 있는 인물을 PC 옆으로 옮기지 않는다(순간이동 없음).
#   · 함께 이동: 턴 시작에 PC와 같은 자리에 있던 동행은 PC가 움직이면 같은 계획(묘사 전)에 배치 연산으로 따라간다
#     — 묘사·장면 인물·사건 조건이 같은 투영을 본다. 이번 턴에 다른 연산이 그 인물의 위치를 바꿨으면 따라가지 않는다.
#   · 이탈(추출 left): 사건 역할이 없는 동행의 정본 배치 override를 해제한다 → 그 자리에서 평상시 일과로 복귀
#     (같은 분 보류 — 순간이동 없음). 사건 역할이 있으면 사건이 위치의 주인이므로 손대지 않는다.

_FOLLOW_KEYS = ("edge_id", "from_place", "to_place", "edge_started_at_minute", "edge_expected_end_minute")
_PLACEMENT_OPS = (_sp.OP_ACTOR_PLACEMENT_SET, _sp.OP_ACTOR_TRANSIT_SET, _sp.OP_ACTOR_PLACEMENT_CLEAR)


def same_spot(a, b) -> bool:
    if not a or not b or a.get("kind") != b.get("kind"):
        return False
    if a.get("kind") == _sp.AT_PLACE:
        return a.get("place_id") == b.get("place_id")
    return all(a.get(k) == b.get(k) for k in _FOLLOW_KEYS)


def canonical_npc_of(session, name):
    """v7.25(F07): 동적 세계 바인딩 세션의 화자 이름 → 정본 ActorId('__AMBIGUOUS__' = 여러 정본 인물). 아니면 None."""
    from . import actors as _act
    rev = getattr(session, "definition_revision", None)
    if not rev or not name:
        return None
    definition = _pw.load_definition(session, rev)
    if definition is None:
        return None
    r = _act.resolve_actor_id(definition, session, name)
    if r.actor_id:
        return r.actor_id
    return "__AMBIGUOUS__" if r.candidates else None


def turn_op_prefix(kind: str, transaction_id) -> str:
    """v7.25: 동행·세션 NPC 연산 id 접두어(트랜잭션별 결정적)."""
    return f"{kind}." + stable_digest(f"{kind}.v1", str(transaction_id or ""), length=10)


def companion_actor_id(definition: dict, world, name):
    """동행 이름 → 정본 NPC ActorId(모호·미등록·PC면 None)."""
    from . import actors as _act
    r = _act.resolve_actor_id(definition, world, name)
    return r.actor_id if r.actor_id and not _act.is_pc_actor_id(r.actor_id) else None


def _eff(definition, world, aid):
    from . import npc_position as _npos
    return _npos.effective_position(definition, world, aid)["placement"]


def companion_follow(ctx, base_world, plan, companion_names, *, prefix: str = "cmp"):
    """PC가 움직이는 PLANNED 계획에 동행의 같은 이동을 더한다(새 계획 반환, 원 계획 불변). 순수."""
    import dataclasses
    if plan is None or plan.status != _pw.PLAN_PLANNED or plan.projected_world is None or not companion_names:
        return plan
    D, pc, end = ctx.definition, ctx.lead_pc, plan.projected_world
    pc0, pc1 = _sp.placement_of(base_world, pc), _sp.placement_of(end, pc)
    if not pc1 or same_spot(pc0, pc1):
        return plan
    touched = {(op.payload or {}).get("actor_id") for op in plan.ops if op.op_type in _PLACEMENT_OPS}
    parents = tuple(sorted(op.op_id for op in plan.ops))
    new_ops, diags = [], []
    for name in companion_names:
        aid = companion_actor_id(D, base_world, name)
        if aid is None:
            continue
        p0 = _eff(D, base_world, aid)
        if not same_spot(p0, pc0):
            continue                                   # 턴 시작에 함께 있지 않았다 — 데려가지 않는다
        if aid in touched or not same_spot(_eff(D, end, aid), p0):
            diags.append({"code": "COMPANION_NOT_FOLLOWING", "actor_id": aid, "reason": "MOVED_BY_WORLD"})
            continue
        oid = f"{prefix}.follow.{aid}"
        if pc1.get("kind") == _sp.AT_PLACE:
            new_ops.append(_wm.make_op(oid, _sp.OP_ACTOR_PLACEMENT_SET, "PLAYER_INTENT",
                                       {"actor_id": aid, "place_id": pc1["place_id"]}, causal_parent_op_ids=parents))
        else:
            new_ops.append(_wm.make_op(oid, _sp.OP_ACTOR_TRANSIT_SET, "PLAYER_INTENT",
                                       dict({k: pc1[k] for k in _sp.TRANSIT_KEYS[1:]}, actor_id=aid),
                                       causal_parent_op_ids=parents))
    if not new_ops:
        return dataclasses.replace(plan, diagnostics=list(plan.diagnostics) + diags) if diags else plan
    b = _wm.WorldMutationBatch(batch_id=f"{prefix}.follow", outer_origin="PLAYER_TURN",
                               source_world_version_id=None, definition_revision=None)
    for op in new_ops:
        b.add(_wm.make_op(op.op_id, op.op_type, op.source_kind, op.payload))
    b.freeze()
    res = _wm.project(end, b, _wm.PRODUCTION_REGISTRY)
    if not res.ok:
        return dataclasses.replace(plan, diagnostics=list(plan.diagnostics) + diags + [
            {"code": "COMPANION_FOLLOW_REJECTED", "conflicts": [str(c) for c in res.conflicts]}])
    diags.append({"code": "COMPANION_FOLLOW", "actors": [op.payload["actor_id"] for op in new_ops]})
    return dataclasses.replace(plan, ops=tuple(plan.ops) + tuple(new_ops), projected_world=res.world,
                               diagnostics=list(plan.diagnostics) + diags)


def session_npc_register_ops(ctx, end_world, names, *, narrative_seed: str, prefix: str, barrier=(),
                             present=()) -> list:
    """v7.25(F06): 지속 등장한 비저작 인물(승격된 세션 NPC)을 정본 ActorRegistry에 SESSION_NPC로 등록한다.

    · 이미 정본 행위자로 해석되는 이름(저작·등록·alias)은 등록하지 않는다(중복·개명 금지).
    · 결정적 ActorId(세션 시드 + 최초 표시 키). 표시 이름 = 플레이어가 본 이름(모델이 지은 본명으로 바꾸지 않는다).
    · 이번 턴 묘사에 나온 인물(present)이고 PC가 장소에 있으면 그 자리에 배치한다 — 묘사가 그 인물을 그 장면에
      보여 준 사실(이후 이동은 정본 규칙). 이번 턴에 보이지 않은 인물은 위치를 지어내지 않는다(untracked)."""
    from . import actors as _act
    D, out = ctx.definition, []
    pc1 = _sp.placement_of(end_world, ctx.lead_pc) or {}
    reg = _act.registry_of(end_world)
    for name in sorted(set(n for n in names or () if isinstance(n, str) and n.strip())):
        r = _act.resolve_actor_id(D, end_world, name)
        if r.actor_id or r.candidates:
            continue
        aid = _act.session_npc_actor_id(narrative_seed, name)
        if aid in reg:
            continue
        rop = _wm.make_op(f"{prefix}.reg.{aid}", _act.OP_ACTOR_REGISTER, "OBSERVATION",
                          {"actor_id": aid, "kind": "NPC", "display_name": name, "source_kind": "SESSION_NPC",
                           "source_key": name, "aliases": []}, causal_parent_op_ids=tuple(barrier))
        out.append(rop)
        if pc1.get("kind") == _sp.AT_PLACE and name in set(present or ()):
            out.append(_wm.make_op(f"{prefix}.at.{aid}", _sp.OP_ACTOR_PLACEMENT_SET, "OBSERVATION",
                                   {"actor_id": aid, "place_id": pc1["place_id"]},
                                   causal_parent_op_ids=tuple(barrier) + (rop.op_id,)))
    return out


def companion_outcome(ctx, end_world, ops, *, joined, left, prefix: str = "cmp"):
    """묘사 후 — 추출 합류 제안 검증 + 이탈 배치 해제 연산. (kept_joined, clear_ops, diagnostics). 순수."""
    from . import npc_projection as _np
    D, pc = ctx.definition, ctx.lead_pc
    pc1 = _sp.placement_of(end_world, pc) or {}
    kept, diags, clears = [], [], []
    for name in joined or ():
        aid = companion_actor_id(D, end_world, name)
        if aid is None:
            diags.append({"code": "COMPANION_JOIN_REJECTED", "name": name, "reason": "NOT_A_WORLD_ACTOR"})
        elif pc1.get("kind") != _sp.AT_PLACE or not same_spot(_eff(D, end_world, aid), pc1):
            diags.append({"code": "COMPANION_JOIN_REJECTED", "name": name, "actor_id": aid, "reason": "NOT_COLOCATED"})
        else:
            kept.append(name)
    barrier = tuple(sorted(op.op_id for op in ops))
    touched = {(op.payload or {}).get("actor_id") for op in ops if op.op_type in _PLACEMENT_OPS}
    for name in left or ():
        aid = companion_actor_id(D, end_world, name)
        if aid is None or aid in touched or _sp.placement_of(end_world, aid) is None:
            continue
        proj = _np.project_npc(aid, D, end_world) or {}
        if proj.get("event_roles"):
            continue                                   # 사건이 위치의 주인 — 해제하지 않는다
        clears.append(_wm.make_op(f"{prefix}.leave.{aid}", _sp.OP_ACTOR_PLACEMENT_CLEAR, "OBSERVATION",
                                  {"actor_id": aid}, causal_parent_op_ids=barrier))
    return kept, clears, diags


# ══════════════════════════════════════════════════════════════
#  같은 분 서사 선후
# ══════════════════════════════════════════════════════════════

_ACCESS_STATE_WORDS = {"OPEN": "열린다", "UNLOCKED": "열린다", "CLOSED": "닫힌다", "LOCKED": "잠긴다",
                       "BARRED": "막힌다", "BLOCKED": "막힌다", "SEALED": "봉쇄된다", "GUARDED": "경비가 선다"}


def _control_label(definition, cid, scene, namer=None) -> str | None:
    """접근 제어 → 장면에서 보이는 출입로 표현(내부 control id 미노출 · H07 장소는 표시명)."""
    edges = [e for e in (definition.get("movement_edges") or {}).values() if e.get("access_ref") == cid]
    if not edges:
        return None
    edges.sort(key=lambda e: (scene not in (e.get("from"), e.get("to")), e.get("from"), e.get("to")))
    a, b = sorted((edges[0].get("from"), edges[0].get("to")))
    if namer is not None:
        a, b = namer(a), namer(b)
    return f"{a}–{b} 사이 출입로"


def _actor_label(definition, world, actor_id, pcs) -> str:
    from . import actors as _actors
    entry = _actors.registry_of(world).get(actor_id) if world is not None else None
    if actor_id in pcs or (isinstance(entry, dict) and entry.get("kind") == "PC"):
        return "PC"                                            # 묘사·추출이 같은 라벨을 받는다
    name = _actors.display_name(definition, world, actor_id) if world is not None else \
        ((definition.get("actor_definitions") or {}).get(actor_id) or {}).get("display_name")
    return name or "누군가"


def _place_shown(definition, world, pcs, pid):
    pc = sorted(pcs)[0] if pcs else None
    if pid is None or world is None or pc is None:
        return pid
    return _pd.display_for(definition, world, pc, pid, require_known=False)


def _fact_label(fact, definition, world, scene, pcs) -> str | None:
    """관찰 가능한 효과 사실 하나의 서술. 표현할 수 없는 효과는 None(내부 id로 대체하지 않는다).
    H07 §5: 장소는 PC 지식 수준 표시명."""
    t, p = fact.get("op_type"), fact.get("payload") or {}
    pn = lambda pid: _place_shown(definition, world, pcs, pid)
    if t == _sp.OP_ACCESS_STATE_SET:
        where = _control_label(definition, p.get("control_id"), scene, pn)
        word = _ACCESS_STATE_WORDS.get(p.get("state"))
        return f"{where}가 {word}" if where and word else None
    if t == _sp.OP_ACTOR_PLACEMENT_SET:
        who = _actor_label(definition, world, p.get("actor_id"), pcs)
        return f"{who}이(가) {pn(p.get('place_id'))}에 도착한다" if p.get("place_id") else None
    if t == _sp.OP_ACTOR_PLACEMENT_CLEAR:
        who = _actor_label(definition, world, p.get("actor_id"), pcs)
        return f"{who}이(가) 평소 자리로 돌아간다"
    if t == _sp.OP_ACTOR_TRANSIT_SET:
        who = _actor_label(definition, world, p.get("actor_id"), pcs)
        return f"{who}이(가) {pn(p.get('from_place'))}에서 {pn(p.get('to_place'))} 쪽으로 떠난다"
    if t == _sp.OP_SCENE_PLACE_SET:
        return f"장면이 {pn(p.get('place_id'))}(으)로 옮겨진다" if p.get("place_id") else None
    return None


def _unit_fact_labels(tie, definition, world, scene, pcs) -> dict:
    out = {}
    for f in getattr(tie, "effect_facts", ()) or ():
        lbl = _fact_label(f, definition or {}, world, scene, pcs)
        if lbl and lbl not in out.setdefault(f["effect_id"], []):
            out[f["effect_id"]].append(lbl)
    return out


def tie_option_labels(plan, definition=None, ctx=None) -> list:
    """[(O키, 설명)] — 허용 순서마다 하나. 키는 transient(영속하지 않는다).

    설명은 경쟁하는 **관찰 가능한 사실의 순서**다(예: "시장–성당 사이 출입로가 열린다 → … 막힌다").
    사건·예약 내부 id와 숨은 효과는 싣지 않는다. 표현 가능한 사실이 없는 효과는 '장면 밖의 다른 변화'.
    """
    out = []
    n = 0
    pcs = set(getattr(ctx, "pcs", ()) or ())
    world = getattr(plan, "projected_world", None)
    scene = getattr(plan, "scene_place", None)
    if scene is None and world is not None and ctx is not None:
        scene = (_sp.placement_of(world, ctx.lead_pc) or {}).get("place_id")
    for tie in plan.ties or ():
        facts = _unit_fact_labels(tie, definition, world, scene, pcs)
        for order in tie.allowed_orderings:
            n += 1
            out.append((f"O{n}", " → ".join(" · ".join(facts.get(u) or ["장면 밖의 다른 변화"]) for u in order)))
    return out


def tie_orders_for(plan, key) -> dict | None:
    """O키 → {tie_id: [order]}. 무효·모호(복수 tie)면 None."""
    if not plan.ties or len(plan.ties) != 1:
        return None
    n = 0
    for tie in plan.ties:
        for order in tie.allowed_orderings:
            n += 1
            if f"O{n}" == key:
                return {tie.tie_id: list(order)}
    return None


# ══════════════════════════════════════════════════════════════
#  v7.14 주변 발견 — 선언 게이트가 승인·판정 성공한 주변 물건·환경 요소
#   · 정본 = 기존 정보·인지 typed 연산뿐(RUNTIME InfoId + PC CONTENT 인지). 새 세계 필드·도메인 없음.
#   · 실패는 '이번에 찾지 못함' — 부재 정보·부재 인지·소지품을 만들지 않는다.
#   · 발견만으로 소지품에 넣지 않는다. 추출이 키(D…)로 지목한 실제 수거만 소지품 +1과 SUPERSEDED.
# ══════════════════════════════════════════════════════════════

AMBIENT_TAG = "ambient_discovery"
DISCOVERY_KINDS = ("OBJECT", "ENVIRONMENT")
DISCOVERY_NEW_KEY = "DNEW"
_KIND_WORD = {"OBJECT": "물건", "ENVIRONMENT": "환경"}


def ambient_info_id(transaction_id: str) -> str:
    """이번 시도의 발견 InfoId — tx에서 결정적(같은 시도의 재추출·재처리에서 중복 생성 없음)."""
    return "rt.ambient." + stable_digest("ambient.discovery.v1", transaction_id, length=12)


def ambient_record(info_id: str, target: str, kind: str, place_id: str, place_name: str, at_minute=None) -> dict:
    """발견 정보 정의 — 플레이어 대상명 + 장소 표시명의 고정 문형(캐시·숨은 설정 문구 없음)."""
    return _info.runtime_definition_record(
        info_id, f"[주변 발견] {place_name}에서 찾아낸 {_KIND_WORD.get(kind, '물건')}: {target}",
        summary=f"{place_name} — {target}", subject_refs=[f"place:{place_id}"],
        tags=[AMBIENT_TAG, kind], fragments=[target], created_at_minute=at_minute)


def same_target(a, b) -> bool:
    return isinstance(a, str) and isinstance(b, str) and "".join(a.split()) == "".join(b.split()) != ""


def discoveries_at(definition: dict, world, pc: str, place_id) -> list:
    """현재 장소에서 PC가 CONTENT로 아는 CURRENT 주변 발견 — [(D키, info_id, 대상명, 종류)](생성 순)."""
    if not place_id or not pc:
        return []
    dom = (lambda k: world.domain(k)) if isinstance(world, _wm.ProjectedWorld) else \
        (lambda k: getattr(world, k, None))
    recs = (dom("cognition_state") or {}).get(_cog.actor_holder(pc)) or {}
    st = dom("information_state") or {}
    rows = []
    for iid, r in recs.items():
        if not isinstance(r, dict) or r.get("awareness") != _cog.CONTENT:
            continue
        d = _info.runtime_definition(st, iid) or {}
        if AMBIENT_TAG not in (d.get("tags") or ()) or f"place:{place_id}" not in (d.get("subject_refs") or ()):
            continue
        if _info.effective_validity(definition, st, iid) != _info.CURRENT:
            continue
        frag = (d.get("fragments") or [""])[0]
        kind = next((t for t in d.get("tags") or () if t in DISCOVERY_KINDS), "OBJECT")
        rows.append((d.get("created_at_minute") or 0, iid, frag, kind))
    rows.sort(key=lambda x: (x[0], x[1]))
    return [(f"D{n}", iid, frag, kind) for n, (_m, iid, frag, kind) in enumerate(rows, 1)]


def pc_place(ctx, session):
    pl = _sp.placement_of(session, ctx.lead_pc) or {}
    return pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None


def pc_place_name(ctx, session, place_id) -> str:
    """PC 지식 수준의 장소 표시명(발견 명제·요약용 — 내부 PlaceId를 문장에 쓰지 않는다)."""
    return _pd.display_for(ctx.definition, session, ctx.lead_pc, place_id, require_known=False)


def discovery_rows_text(rows) -> str:
    return ", ".join(f"{k}={t}({_KIND_WORD.get(kd, kd)})" for k, _i, t, kd in rows)


def discovery_block(ctx, plan, session) -> str:
    """지시층위: 현재 장소에서 PC가 발견해 둔 것(최근 인지 12건 제한과 무관)."""
    world, scene = _scene_world_and_place(ctx, plan, session)
    rows = discoveries_at(ctx.definition, world, ctx.lead_pc, scene)
    if not rows:
        return ""
    return ("\n[현재 장소에서 PC가 발견해 둔 것 — 정본(아직 그 자리에 있다). 플레이어가 줍거나 쓴다고 선언할 때만 "
            "획득·사용으로 서술]\n" + "\n".join(f"· {t} ({_KIND_WORD.get(kd, kd)})" for _k, _i, t, kd in rows) + "\n")


def implied_discovery_take(obs_manifest, raw, ref_key, item_deltas, pc_name, pc_bag_before) -> str | None:
    """A1 보정: 추출이 D키를 빠뜨린 발견물 수거의 엄격한 조건부 복구 — 복구할 D키 또는 None. 순수.

    모두 충족할 때만: ① 선언이 가리킨 후보(ref_key — 게이트가 검증한 기존 발견 키 또는 이번 턴 DNEW)가 이번 턴
    매니페스트의 OBJECT 행이다 ② 그 키(다른 키 포함 어떤 D키)도 추출이 명시하지 않았다 ③ 매니페스트에 같은 이름
    행이 정확히 하나다 ④ 같은 이름 소지 변경이 추출 전체에서 PC +1 한 건뿐이다(다른 인물 증감 = 다른 출처 정황)
    ⑤ PC가 턴 전부터 같은 이름을 갖고 있지 않다(기존 소지품 정리와 구분). 이름 일치는 후보 좁히기일 뿐이다.
    """
    if not ref_key or not pc_name or not isinstance(raw, dict):
        return None
    if any(k not in (None, "", "NONE") for k in list(raw.get("discovery_taken") or ())
           + list(raw.get("discovery_gone") or ())):
        return None
    row = obs_manifest.discovery_of(ref_key)
    if row is None or row[3] != "OBJECT":
        return None
    target = row[2]
    if sum(1 for r in obs_manifest.discoveries if same_target(r[2], target)) != 1:
        return None
    hits = [e for e in item_deltas or () if same_target(e.get("item"), target)]
    if len(hits) != 1 or hits[0].get("target") != pc_name or int(hits[0].get("delta") or 0) != 1:
        return None
    if any(same_target(k, target) and (v or 0) > 0 for k, v in (pc_bag_before or {}).items()):
        return None
    return ref_key


def discovery_ops(ctx, world, obs_manifest, raw: dict, outcome, *, prefix: str, transaction_id: str, at_minute):
    """발견 생성(NEW) + 추출이 키로 지목한 수거·소진 → (ops, 수거한 대상명 목록, 진단). 순수.

    outcome: None 또는 {"status": "NEW"|"EXISTING", "target", "kind", "place", "place_name", "info_id"}.
    """
    ops, gains, diags = [], [], []
    lead = ctx.lead_pc
    create_id = None
    new_iid = None
    if outcome and outcome.get("status") == "NEW":
        new_iid = outcome["info_id"]
        st = (world.domain("information_state") if isinstance(world, _wm.ProjectedWorld) else
              getattr(world, "information_state", None)) or {}
        if _info.exists(ctx.definition, st, new_iid):
            diags.append({"code": "DISCOVERY_ALREADY_EXISTS", "info_id": new_iid})
        else:
            create = _wm.make_op(f"{prefix}.dc", _info.OP_INFORMATION_CREATE, "OBSERVATION", {
                "definition": ambient_record(new_iid, outcome["target"], outcome["kind"], outcome["place"],
                                             outcome.get("place_name") or outcome["place"], at_minute)})
            ops.append(create)
            create_id = create.op_id
            ops.append(_wm.make_op(f"{prefix}.da", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
                "holder": _cog.actor_holder(lead), "info_id": new_iid, "awareness": _cog.CONTENT,
                "acquisition": _cog.acquisition_record(f"turn:{transaction_id}:discovery:{new_iid}", "WITNESS",
                                                       at_minute=at_minute)}, causal_parent_op_ids=(create_id,)))
    taken = {k for k in (raw.get("discovery_taken") or ()) if isinstance(k, str)}
    gone = {k for k in (raw.get("discovery_gone") or ()) if isinstance(k, str)} - taken
    for n, key in enumerate(sorted(taken | gone)):
        if key == "NONE":
            continue
        row = obs_manifest.discovery_of(key)
        if row is None:
            diags.append({"code": "DISCOVERY_KEY_INVALID", "key": key})
            continue
        _k, iid, target, kind = row
        parents = ()
        if key == DISCOVERY_NEW_KEY:
            if create_id is None:
                diags.append({"code": "DISCOVERY_NEW_NOT_CREATED", "key": key})
                continue
            parents = (create_id,)
        if key in taken and kind != "OBJECT":
            diags.append({"code": "DISCOVERY_ENVIRONMENT_NOT_TAKEN", "key": key})
            continue
        ops.append(_wm.make_op(f"{prefix}.dv{n}", _info.OP_INFORMATION_VALIDITY_SET, "OBSERVATION",
                               {"info_id": iid, "validity": _info.SUPERSEDED}, causal_parent_op_ids=parents))
        if key in taken:
            gains.append(target)
    return ops, gains, diags


# ══════════════════════════════════════════════════════════════
#  관찰 매니페스트(추출) · 처리
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ObservationManifest:
    infos: tuple = ()             # (X키, info_id, 설명) — 이번 장면에서 묘사로 노출될 수 있는 정보
    signals: tuple = ()           # (S키, (signal_id, instance_id), cue)
    ties: tuple = ()              # (O키, 설명)
    extra_time_allowed: bool = True
    discoveries: tuple = ()       # v7.14 (D키, info_id, 대상명, 종류) — 현재 장소 유효 발견 + DNEW(이번 턴)
    npcs: tuple = ()              # v7.39 (N키, ActorId, 이름) — 장면 인물(PC와의 반응 보고 대상)

    def schema(self) -> dict:
        def enum(rows):
            return {"type": "string", "enum": ["NONE"] + [r[0] for r in rows]}
        return {"type": "object",
                "description": "확정 묘사에서 실제로 관찰된 세계 사실(후보 키만). 없으면 빈 값.",
                "properties": {
                    "info_exposed": {"type": "array", "items": enum(self.infos)},
                    "extra_minutes": {"type": "integer"},
                    "tie_order": enum(self.ties),
                    "represented_signals": {"type": "array", "items": enum(self.signals)},
                    **({"discovery_taken": {"type": "array", "items": enum(self.discoveries)},
                        "discovery_gone": {"type": "array", "items": enum(self.discoveries)}}
                       if self.discoveries else {}),
                    **({"npc_reactions": {"type": "array", "items": {"type": "object", "properties": {
                        "actor": enum(self.npcs), "trust": {"type": "integer"}, "memory": {"type": "string"}},
                        "required": ["actor", "trust"]}}} if self.npcs else {})}}

    def render(self) -> str:
        lines = ["\n[세계 관찰 — world_observation]"]
        if self.infos:
            lines.append("묘사에서 PC가 실제로 듣거나 본 정보만 info_exposed에(추측 금지): "
                         + ", ".join(f"{k}={lbl}" for k, _, lbl in self.infos))
        if self.signals:
            lines.append("묘사에 이미 나온 장면 사건만 represented_signals에: "
                         + ", ".join(f"{k}={cue}" for k, _, cue in self.signals))
        if self.ties:
            lines.append("묘사가 택한 선후 순서 하나를 tie_order에: " + ", ".join(f"{k}={lbl}" for k, lbl in self.ties))
        if self.discoveries:
            lines.append("PC가 이번 묘사에서 실제로 집어 소지한 발견물만 discovery_taken에, 사용·소모·파손·이동으로 "
                         "그 자리에서 사라진 것만 discovery_gone에(보기만 했거나 그대로 둔 것은 넣지 말 것 · "
                         "discovery_taken에 넣은 물건은 item_changes에 따로 넣지 말 것): "
                         + discovery_rows_text(self.discoveries))
        if self.npcs:
            lines.append("이번 묘사에서 PC와 실제로 말·행동을 주고받은 장면 인물만 npc_reactions에(지나가거나 보기만 한 "
                         "인물은 넣지 말 것): " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.npcs))
            lines.append("· trust: 그 인물이 이번 일로 PC를 더 믿게 됐는가(0~100, 50 = 변화 없음). 도움·약속 이행·"
                         "예의는 60~85, 무례·거짓·위협은 15~40. 목숨을 구하거나 배신·공격처럼 관계를 단번에 바꿀 "
                         "결정적 사건만 96 이상·4 이하. 묘사에 드러난 일만 근거로, 과장하지 말 것.")
            lines.append(f"· memory: 그 인물이 오래 기억할 만한 일이 있었을 때만 그 인물 시점의 한 줄({_rel.MEMORY_MAX}자 "
                         "이내, 예: '물통을 나눠 준 외지인'). 없으면 비워 둔다.")
        if self.extra_time_allowed:
            lines.append("이동·대기가 아닌 행동으로 묘사상 분명히 흐른 추가 시간이 있으면 extra_minutes(분, 없으면 0).")
        else:
            lines.append("이번 턴 경과 시간은 코드가 정했다 — extra_minutes는 0.")
        lines.append("PC의 믿음·국면·경로·접근은 판정하지 말 것.")
        return "\n".join(lines) + "\n"

    def info_of(self, key):
        return next((r[1] for r in self.infos if r[0] == key), None)

    def signal_of(self, key):
        return next((r[1] for r in self.signals if r[0] == key), None)

    def discovery_of(self, key):
        return next((r for r in self.discoveries if r[0] == key), None)

    def npc_of(self, key):
        return next((r[1] for r in self.npcs if r[0] == key), None)


def _scene_relevant(g, scene, rec) -> bool:
    """정보 정의의 장소 주제가 현재 장면(또는 그 상·하위)에 닿는가. 장소 주제가 없으면 장면 무관하게 허용."""
    places = [r[len("place:"):] for r in (rec.get("subject_refs") or ()) if str(r).startswith("place:")]
    if not places or not scene:
        return not places
    path = set(g.path_of(scene))
    return any(p in path or scene in g.path_of(p) for p in places if p in g.places)


def build_observation_manifest(definition: dict, ctx, plan, base_world, *, discovery=None,
                               pending=()) -> ObservationManifest:
    """추출 관찰 후보 — **이번 턴 노출 근거가 있는 것만**(R2).

    정보: 이번 턴 코드 계획(stage-1)의 사건이 결정적으로 만든 정보 중 장면과 관련되고 공개 안전 요약
          (summary)이 있는 것. 명제 전문·숨은 정보로 대체하지 않는다. 같은 장면 NPC가 내용을 안다는
          사실만으로는 후보가 아니다(노출 계획 없음 = 후보 없음).
          H07: 본 묘사에 준 관찰 신호의 exposes(저작 정보 — summary 필수)도 후보다(신호 = 노출 계획).
          v7.21.1: 함께 준 전달 대기 신호의 exposes도 같은 조건으로 후보다(옛 행은 exposes 없음 → 후보 없음).
    신호: 본 묘사에 실제로 준 관찰 신호만(P1: 이번 계획 신호 + 지난 본문이 전하지 못한 전달 대기 신호).
          ACTIVE 사건의 미래 EMIT_SIGNAL은 나열하지 않는다. 추출이 표시한 신호가 필수 서사의 표현 근거다.
    시간: P1(D1) 경과는 묘사 전에 코드가 정한다 — 추출 추가 시간은 받지 않는다.
    """
    world = plan.projected_world if plan is not None and plan.projected_world is not None else (
        base_world if isinstance(base_world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(base_world))
    lead = ctx.lead_pc
    planned = plan is not None and plan.status == _pw.PLAN_PLANNED
    scene = (plan.scene_place if planned else None) or (_sp.placement_of(world, lead) or {}).get("place_id")
    pc_recs = (world.domain("cognition_state") or {}).get(_cog.actor_holder(lead)) or {}
    g = _sp.SpatialGraph(definition)
    cand = {}
    for op in (plan.ops if planned else ()):
        if op.op_type != _info.OP_INFORMATION_CREATE:
            continue
        rec = (op.payload or {}).get("definition") or {}
        iid, summary = rec.get("info_id"), rec.get("summary")
        if not iid or not isinstance(summary, str) or not summary.strip():
            continue                                               # 공개 안전 설명 없음 → 후보 아님
        if (pc_recs.get(iid) or {}).get("awareness") == _cog.CONTENT or not _scene_relevant(g, scene, rec):
            continue
        cand[iid] = summary
    # H07: 이번 본 묘사에 준 관찰 신호가 노출할 수 있는 정보(저작 정의의 공개 안전 summary만)
    # v7.21.1: 이번 본문에 함께 준 전달 대기 신호의 exposes도 같은 조건으로 후보(인지는 추출 노출 확인 때만)
    exposing = list(observable_signals(plan.signals)) if planned else []
    exposing += [r for r in pending or () if isinstance(r, dict) and r.get("cue")]
    if exposing:
        istate = world.domain("information_state") or {}
        for s in exposing:
            for iid in s.get("exposes") or ():
                rec = _info.resolve_definition(definition, istate, iid) or {}
                summary = rec.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    continue
                if (pc_recs.get(iid) or {}).get("awareness") == _cog.CONTENT:
                    continue
                cand.setdefault(iid, summary)
    # v7.44(E4): 지금 장면에서 조사하면 드러날 수 있는 저작 단서(PC가 아직 모르는 것) — 묘사가 드러냈을 때만 노출
    istate_f = world.domain("information_state") or {}
    for _i, f in unknown_finds(definition, world, lead, scene):
        summ = (_info.resolve_definition(definition, istate_f, f["info"]) or {}).get("summary")
        if isinstance(summ, str) and summ.strip():
            cand.setdefault(f["info"], summ)
    infos = tuple((f"X{n}", iid, cand[iid]) for n, iid in enumerate(sorted(cand), 1))
    sigs = [((s["signal_id"], s["instance_id"]), s["cue"])
            for s in observable_signals(plan.signals if plan is not None else ())]
    seen = {k for k, _ in sigs}
    for r in pending or ():
        k = (r.get("signal_id"), r.get("instance_id"))
        if k not in seen and r.get("cue"):
            seen.add(k)
            sigs.append((k, r["cue"]))
    signals = tuple((f"S{n}", k, cue) for n, (k, cue) in enumerate(sigs, 1))
    ties = tuple(tie_option_labels(plan, definition, ctx)) if plan is not None and plan.status == _pw.PLAN_TIE else ()
    extra = False
    # v7.14: 현재 장면의 유효 발견(SUPERSEDED는 나오지 않는다 — 같은 물건 두 번 수거 불가) + 이번 턴 새 발견
    drows = list(discoveries_at(definition, world, lead, scene))
    if discovery and discovery.get("status") == "NEW":
        drows.append((DISCOVERY_NEW_KEY, discovery["info_id"], discovery["target"], discovery["kind"]))
    # v7.39: 장면 인물(유효 위치 기준 — 장면 인물 블록과 같은 해석) — PC와의 반응 보고 대상
    from . import npc_position as _npos
    adefs = definition.get("actor_definitions") or {}
    npc_ids = [a for a, _src in _npos.actors_at(definition, world, scene, exclude=ctx.pcs)
               if (adefs.get(a) or {}).get("kind") == "NPC"] if scene else []
    npcs = tuple((f"N{n}", a, (adefs.get(a) or {}).get("display_name") or a) for n, a in enumerate(npc_ids, 1))
    return ObservationManifest(infos, signals, ties, extra, tuple(drows), npcs)


def unrepresented_signals(signals, represented) -> list:
    """관찰 가능 신호 중 아직 표현되지 않은 것(본 묘사에 준 신호·추출이 표시한 신호 제외)."""
    rep = {tuple(k) for k in represented or ()}
    return [s for s in observable_signals(signals) if (s["signal_id"], s["instance_id"]) not in rep]


@dataclass
class TurnWorldResult:
    status: str
    ops: tuple = ()
    projected_world: object = None
    plan: object = None                     # 확정된(선후 반영) 묘사 전 계획
    signals: list = field(default_factory=list)            # 보충 진행 신호
    pending: list = field(default_factory=list)            # P1: 커밋 후 world_pending_signals(다음 본문 전달 대기)
    represented: tuple = ()
    diagnostics: list = field(default_factory=list)
    exposures: tuple = ()
    extra_minutes: int = 0
    conflict: dict | None = None
    item_gains: tuple = ()                  # v7.14 수거한 발견물 대상명(PC 소지품 +1 — 커밋 경로에서 적용)


def reveal_ops(definition: dict, pc: str, info_ids, *, prefix: str, at_minute=None) -> list:
    """H07 §5.2: PC가 CONTENT로 알게 된 저작 정보의 reveals → 공간 지식 upsert(장소 이름 · 구간 존재).

    같은 장소·구간을 여러 정보가 드러내도 연산은 하나(출처 목록 병합). RESTRICTED/SECRET 길도 이 경로로만
    자동이 아닌 '발견'으로 알게 된다. 노출 ≠ 믿음: 공간 지식은 '그런 이름·길이 있다고 안다'는 인지다.
    """
    infos = definition.get("information_definitions") or {}
    places, edges = {}, {}
    for iid in sorted(set(info_ids or ())):
        rv = (infos.get(iid) or {}).get("reveals") or {}
        for pid in rv.get("places") or ():
            places.setdefault(pid, []).append(f"INFO:{iid}")
        for eid in rv.get("edges") or ():
            edges.setdefault(eid, []).append(f"INFO:{iid}")
    out = []
    for pid in sorted(places):
        out.append(_wm.make_op(f"{prefix}.p.{pid}", _sp.OP_PLACE_KNOWLEDGE_UPSERT, "OBSERVATION", {
            "actor_id": pc, "place_id": pid, "fields": {"knows_name": True, "acquisitions": places[pid]}}))
    for eid in sorted(edges):
        out.append(_wm.make_op(f"{prefix}.e.{eid}", _sp.OP_EDGE_KNOWLEDGE_UPSERT, "OBSERVATION", {
            "actor_id": pc, "edge_id": eid, "fields": {"connection_known": True, "acquisition_sources": edges[eid],
                                                       "last_confirmed_at": at_minute}}))
    return out


def relation_ops(definition, world, obs_manifest, raw, lead, *, prefix, transaction_id, at_minute) -> tuple:
    """v7.39: 추출 npc_reactions → 인물 → PC 관계 delta(MET·TRUST·MEMORY). (ops, diags) — 순수.

    한 인물당 한 번만(중복 보고는 첫 것). 신뢰 변화량은 relationships.trust_change(임계·감쇠)가 정한다.
    기억은 60자 이내만 받고(자르지 않음 — 넘으면 버리고 진단), 같은 문장은 다시 쌓지 않는다."""
    ops, diags, seen = [], [], set()
    for r in raw.get("npc_reactions") or ():
        if not isinstance(r, dict):
            continue
        actor = obs_manifest.npc_of(r.get("actor"))
        if actor is None:
            if r.get("actor") not in (None, "", "NONE"):
                diags.append({"code": "NPC_REACTION_INVALID", "key": r.get("actor")})
            continue
        if actor in seen:
            continue
        seen.add(actor)
        frm, to = _cog.actor_holder(actor), _cog.actor_holder(lead)
        st = _rel.trust_state(world, actor, lead)
        base = f"turn:{transaction_id}:rel:{actor}"
        mk = lambda kind, summary: _wm.make_op(  # noqa: E731
            f"{prefix}.rel.{actor}.{kind.lower()}", _rel.OP_RELATIONSHIP_DELTA_APPEND, "OBSERVATION",
            {"delta": _rel.delta_record(f"{base}:{kind.lower()}", frm, to, kind, summary=summary,
                                        at_minute=at_minute, source_ref=f"turn:{transaction_id}")})
        if not st["met"]:
            ops.append(mk(_rel.KIND_MET, None))
        change = _rel.trust_change(world, actor, lead, r.get("trust"), at_minute)
        if change:
            ops.append(mk(_rel.KIND_TRUST, str(change)))
        mem = r.get("memory")
        if isinstance(mem, str) and mem.strip():
            mem = mem.strip()
            if len(mem) > _rel.MEMORY_MAX:
                diags.append({"code": "NPC_MEMORY_TOO_LONG", "actor": actor, "length": len(mem)})
            elif mem not in st["memories"]:
                ops.append(mk(_rel.KIND_MEMORY, mem))
    return ops, diags


def process_observation(ctx, base_world, plan, obs_manifest: ObservationManifest, raw_obs, *,
                        transaction_id: str, narrative_seed: str, replan=None,
                        face_info_ids=(), name_info_ids=(), discovery=None,
                        read_info_ids=(), pending=(), item_facts=None) -> TurnWorldResult:
    """확정 묘사 관찰 → 보충 typed 연산 + 같은 분 보충 진행 → 최종 묶음 연산(순수).

    v7.17 read_info_ids: 코드가 검증한 아이템 READ(실제 소지·현장 물건의 저작 info_refs)로 PC가 내용을 알게 된 정보 —
    같은 정보·인지 경로(DOCUMENT, 믿음 불변)로 같은 묶음에 합류한다. 아이템 소유만으로는 생기지 않는다.

    P1: pending = 이번 본문에 함께 준 전달 대기 신호. item_facts = 이번 턴 검증된 아이템 사용·지급 사실(묘사 후 확정)
    — 같은 턴 보충 진행의 관찰자 문맥에 더한다(D4 · 다음 턴으로 미루지 않음). 보충 진행은 묘사 전 계획과 같은
    관찰자 문맥(plan.observer)을 쓴다. 본문이 표현하지 못한 관찰 신호는 result.pending으로 남는다(유실 금지)."""
    D, lead = ctx.definition, ctx.lead_pc
    raw = raw_obs if isinstance(raw_obs, dict) else {}
    diags = []
    # ① 같은 분 선후(묘사 전 계획이 TIE였다면 필수)
    if plan is not None and plan.status == _pw.PLAN_TIE:
        orders = tie_orders_for(plan, raw.get("tie_order"))
        if orders is None or replan is None:
            return TurnWorldResult(TW_TIE_UNRESOLVED, plan=plan,
                                   diagnostics=[{"code": "TIE_ORDER_INVALID", "value": raw.get("tie_order")}])
        plan = replan(orders)
        if plan.status != _pw.PLAN_PLANNED:
            return TurnWorldResult(TW_TIE_UNRESOLVED if plan.status == _pw.PLAN_TIE else TW_CONFLICT, plan=plan,
                                   diagnostics=[{"code": "TIE_ORDER_NOT_RESOLVING", "status": plan.status}])
    elif raw.get("tie_order") not in (None, "", "NONE"):
        diags.append({"code": "TIE_ORDER_IGNORED"})
    if plan is not None and plan.status == _pw.PLAN_CONFLICT:
        return TurnWorldResult(TW_CONFLICT, plan=plan, conflict={"stage": "pre_narration"})
    start_world = plan.projected_world if plan is not None and plan.projected_world is not None else (
        base_world if isinstance(base_world, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(base_world))
    stage1 = tuple(plan.ops) if plan is not None and plan.status == _pw.PLAN_PLANNED else ()
    # ② 정보 노출 → PC 획득(믿음 불변)
    prefix = "h05o." + stable_digest("player.observe.v1", transaction_id, length=10)
    exposures, acq = [], []
    for n, key in enumerate(sorted(set(k for k in (raw.get("info_exposed") or ()) if isinstance(k, str)))):
        iid = obs_manifest.info_of(key)
        if iid is None:
            if key != "NONE":
                diags.append({"code": "INFO_EXPOSURE_INVALID", "key": key})
            continue
        exposures.append(iid)
        acq.append(_wm.make_op(f"{prefix}.acq{n}", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
            "holder": _cog.actor_holder(lead), "info_id": iid, "awareness": _cog.CONTENT,
            "acquisition": _cog.acquisition_record(f"turn:{transaction_id}:obs:{iid}", "TESTIMONY",
                                                   at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))}))
    # ②-a SVG 카드: 지시층위가 지정한 얼굴·이름 식별 → 저작 InfoId 획득(얼굴 WITNESS · 이름 TESTIMONY).
    #       같은 보충 진행에 합류한다(인지 변화에 의존하는 사건 연쇄가 같은 턴에 평가된다).
    for tag, route, ids in (("face", "WITNESS", face_info_ids), ("name", "TESTIMONY", name_info_ids)):
        for n, iid in enumerate(sorted(set(ids or ()))):
            acq.append(_wm.make_op(f"{prefix}.{tag}{n}", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
                "holder": _cog.actor_holder(lead), "info_id": iid, "awareness": _cog.CONTENT,
                "acquisition": _cog.acquisition_record(
                    f"turn:{transaction_id}:{tag}:{iid}", route,
                    at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))}))
    # ②-a' v7.17 아이템 읽기 — 검증된 READ 대상 물건의 저작 정보(DOCUMENT 경로, 믿음 불변)
    read_ids = []
    for n, iid in enumerate(sorted(set(read_info_ids or ()))):
        if not _info.exists(D, start_world.domain("information_state") or {}, iid):
            diags.append({"code": "ITEM_READ_INFO_UNKNOWN", "info_id": iid})
            continue
        read_ids.append(iid)
        acq.append(_wm.make_op(f"{prefix}.read{n}", _cog.OP_COGNITION_ACQUIRE, "OBSERVATION", {
            "holder": _cog.actor_holder(lead), "info_id": iid, "awareness": _cog.CONTENT,
            "acquisition": _cog.acquisition_record(f"turn:{transaction_id}:read:{iid}", "DOCUMENT",
                                                   at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))}))
    # ②-b H07 §5.2: 정보가 드러낸 공간 지식(장소 이름·숨은 길 발견) — 실제로 알게 된 정보의 저작 reveals만
    acq += reveal_ops(D, lead, list(exposures) + read_ids, prefix=f"{prefix}.rv",
                      at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))
    # ②-b' v7.44(E4): 드러난 저작 단서의 실물 → 그 자리의 주변 발견 물건(다음부터 D키로 줍는다)
    _scene0 = (plan.scene_place if plan is not None and plan.status == _pw.PLAN_PLANNED else None) or \
        (_sp.placement_of(start_world, lead) or {}).get("place_id")
    if exposures and _scene0:
        acq += find_object_ops(D, start_world, lead, _scene0, exposures, prefix=prefix, transaction_id=transaction_id,
                               at_minute=(start_world.domain("world_clock") or {}).get("minute_index"),
                               place_name=_pd.display_for(D, start_world, lead, _scene0, require_known=False))
    # ②-c v7.14 주변 발견: 승인·성공 발견 생성 + PC 인지, 추출이 키로 지목한 수거·소진(같은 묶음 — 원자적)
    d_ops, gains, d_diags = discovery_ops(ctx, start_world, obs_manifest, raw, discovery, prefix=prefix,
                                          transaction_id=transaction_id,
                                          at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))
    acq += d_ops
    diags += d_diags
    # ②-d v7.39 장면 인물 → PC 관계(첫 상호작용·신뢰 변화·기억) — 코드 임계·감쇠, 같은 묶음
    r_ops, r_diags = relation_ops(D, start_world, obs_manifest, raw, lead, prefix=prefix,
                                  transaction_id=transaction_id,
                                  at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))
    acq += r_ops
    diags += r_diags
    # ③ 추가 시간 — P1(D1) 시간은 묘사 전에 코드가 정한다. 매니페스트가 막으면 버린다(이중 계상 금지)
    extra = raw.get("extra_minutes")
    extra = extra if isinstance(extra, int) and not isinstance(extra, bool) and extra > 0 else 0
    if extra and not obs_manifest.extra_time_allowed:
        diags.append({"code": "EXTRA_TIME_CODE_OWNED", "ignored": extra})
        extra = 0
    if extra > MAX_EXTRA_MINUTES:
        diags.append({"code": "EXTRA_TIME_CLAMPED", "value": extra})
        extra = MAX_EXTRA_MINUTES
    represented = set()
    for k in raw.get("represented_signals") or ():
        sk = obs_manifest.signal_of(k) if isinstance(k, str) else None
        if sk:
            represented.add(tuple(sk))
    # ④ 보충 진행(노출·식별·READ·발견·아이템 사용/지급 사실로 인한 같은 분 연쇄)
    #    관찰자 문맥 = 묘사 전 계획과 같은 기준선(plan.observer) + 이번 턴 묘사 후 확정 아이템 사실(P1·D4)
    scene = (plan.scene_place if plan is not None and plan.status == _pw.PLAN_PLANNED else None) or \
        (_sp.placement_of(start_world, lead) or {}).get("place_id")
    now = (start_world.domain("world_clock") or {}).get("minute_index")
    observer = dict(getattr(plan, "observer", None) or {})
    new_facts = False
    for key, src in (("observer_used_items", "used"), ("observer_paid_items", "paid")):
        add = set((item_facts or {}).get(src) or ())
        if add - set(observer.get(key) or ()):
            new_facts = True
        if add or key in observer:
            observer[key] = tuple(sorted(set(observer.get(key) or ()) | add))
    ops, world, sigs = list(stage1), start_world, []
    if acq or extra or new_facts:
        r = _wp.project_world_progression(D, start_world, now + extra, base_ops=acq,
                                          progress={"observer_actor": lead, "scene_place": scene, **observer},
                                          narrative_seed=narrative_seed, outer_origin="PLAYER_TURN")
        diags += r.diagnostics
        if r.status == _wp.TIE_PENDING:
            return TurnWorldResult(TW_TIE_UNRESOLVED, plan=plan, diagnostics=diags + [
                {"code": "SUPPLEMENTAL_TIE", "ties": [t.to_dict() for t in r.same_minute_ties]}])
        if not r.committable or _pw.base_rejected(r.diagnostics):
            # H06 R2-07: 관찰 연산(PC 획득)이 거부되면 버린 채 커밋하지 않는다 — 결정적 충돌
            return TurnWorldResult(TW_CONFLICT, plan=plan, diagnostics=diags,
                                   conflict={"stage": "post_narration",
                                             "status": r.status,
                                             "base_rejected": _pw.base_rejected(r.diagnostics)})
        ops += _pw.namespace_ops(r.ops, f"{prefix}.sup", [o.op_id for o in stage1])
        world, sigs = r.projected_world, list(r.signals)
    # ⑤ 전달 대기(v7.21.1): 묘사 후 보충 진행이 **처음 만든** 관찰 신호만 다음 소설형 본문에 한 번 전한다.
    #    묘사 전 계획 신호는 이번 지시·묘사에 이미 주었으므로 표현 판정과 무관하게 남기지 않는다. 지난 대기는
    #    이번 본문에 한 번 주었으므로(이 결과가 커밋되면) 표현 여부와 무관하게 끝난다 — 재이월 없음.
    #    exposes(정보 노출 후보)·발생 시각·장소를 함께 보존한다(인지는 다음 턴 추출 노출 확인 때만).
    end_minute = (world.domain("world_clock") or {}).get("minute_index")
    already = {(r.get("signal_id"), r.get("instance_id")) for r in pending or () if isinstance(r, dict)}
    already |= {(s["signal_id"], s["instance_id"])
                for s in observable_signals(plan.signals if plan is not None and plan.status == _pw.PLAN_PLANNED else ())}
    out_pending, seen = [], set()
    for s in observable_signals(sigs):
        k = (s["signal_id"], s["instance_id"])
        if k in seen or k in already:
            continue
        seen.add(k)
        out_pending.append({"signal_id": k[0], "instance_id": k[1], "cue": s["cue"],
                            "at_minute": s.get("minute", end_minute), "scene_place": scene,
                            "source_tx": transaction_id, "exposes": list(s.get("exposes") or ())})
    if out_pending:
        diags.append({"code": "SIGNALS_PENDING_DELIVERY", "count": len(out_pending)})
    return TurnWorldResult(TW_READY, tuple(ops), world, plan, sigs, out_pending, tuple(sorted(represented)),
                           diags, tuple(exposures), extra, item_gains=tuple(gains))


# ══════════════════════════════════════════════════════════════
#  같은 분 선후(묘사 전 확정 — P1 D2) · 투영 미디어
# ══════════════════════════════════════════════════════════════

MAX_TIE_ROUNDS = 8


def resolve_plan_tie(plan, replan, choice_key=None, definition=None, ctx=None):
    """P1(D2): 묘사 전 계획이 같은 분 서사 선후(TIE)에서 멈췄으면 순서를 정해 다시 계획한다(순수).

    정본 인과·우선순위는 엔진이 이미 적용했다 — TIE는 남은 자유 경합뿐이다. 첫 경합은 지시층위가 고른
    허용 키(choice_key)를, 그 밖(무효·미선택·이어지는 경합)은 정렬 첫 허용 순서를 쓴다(결정적 — 재시도 동일).
    Returns: (plan, record) — record = [{"by": INSTRUCTION|CODE_DEFAULT, "key"}] (진단·재현 근거).
    """
    record, acc = [], {}
    for n in range(MAX_TIE_ROUNDS):
        if plan is None or plan.status != _pw.PLAN_TIE or replan is None:
            break
        keys = [k for k, _ in tie_option_labels(plan, definition, ctx)]
        if not keys:
            break
        key, by = (choice_key, "INSTRUCTION") if (n == 0 and choice_key in keys) else (keys[0], "CODE_DEFAULT")
        orders = tie_orders_for(plan, key)
        if orders is None:
            break
        acc.update(orders)
        record.append({"by": by, "key": key})
        plan = replan(dict(acc))
    return plan, record


def tie_choice_schema(plan, definition=None, ctx=None) -> tuple:
    """(지시층위 프롬프트 블록, 스키마 속성) — 계획이 TIE일 때만. 키는 O키 enum."""
    if plan is None or plan.status != _pw.PLAN_TIE:
        return "", None
    opts = tie_option_labels(plan, definition, ctx)
    if not opts:
        return "", None
    block = ("\n[같은 순간의 서사 선후 — tie_order]\n같은 분에 아래 일들이 겹친다. 결과·시각은 코드가 정했다. "
             "어느 쪽을 먼저 보여 줄지 서사에 맞는 순서 하나를 tie_order에 고른다(모르면 첫 번째):\n"
             + "\n".join(f"- {k}: {lbl}" for k, lbl in opts) + "\n")
    return block, {"type": "string", "enum": [k for k, _ in opts]}


def media_allowed_places(definition: dict, plan) -> set | None:
    """투영 장면이 허용하는 장소 이미지 키(장면 장소 + 상위 상속). None = 제한 없음(LEGACY).

    TRANSIT(중단) — 구간 저작 미디어 → 출발 장소 문맥. 도착 전 목적지 이미지는 금지.
    """
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return None
    g = _sp.SpatialGraph(definition)
    scene = plan.scene_place
    if scene is None and plan.transit:
        scene = plan.transit.get("from_place")
    if scene is None:
        return set()
    return set(g.path_of(scene))


def media_allowed_places_for(ctx, plan, session) -> set:
    """v7.25(F11): DYNAMIC 이미지 후보 장소 — 계획 투영 장면(PLANNED), 아니면 정본 PC 장소(+상위). 제한 없음은 없다."""
    allowed = media_allowed_places(ctx.definition, plan)
    if allowed is not None:
        return allowed
    pl = _sp.placement_of(session, ctx.lead_pc) or {}
    scene = pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else pl.get("from_place")
    return set(_sp.SpatialGraph(ctx.definition).path_of(scene)) if scene else set()


def image_for_scene(definition: dict, plan) -> str | None:
    """투영 장면의 장소 이미지 키(정의 image, 상위 상속). TRANSIT은 구간 미디어 우선."""
    if plan is None or plan.status != _pw.PLAN_PLANNED:
        return None
    places = definition.get("place_definitions") or {}
    if plan.scene_place is None and plan.transit:
        e = (definition.get("movement_edges") or {}).get(plan.transit.get("edge_id")) or {}
        if e.get("media"):
            return e["media"]
    allowed = media_allowed_places(definition, plan) or set()
    g = _sp.SpatialGraph(definition)
    scene = plan.scene_place or (plan.transit or {}).get("from_place")
    for pid in reversed(g.path_of(scene)) if scene else ():
        if pid in allowed and (places.get(pid) or {}).get("image"):
            return places[pid]["image"]
    return None


# ══════════════════════════════════════════════════════════════
#  재서술(같은 턴 재생성) — 보존된 의미 선택으로 세계 계획 재계산 (H06-E)
# ══════════════════════════════════════════════════════════════

def rerender_world_intent(session, judgment_record, post_world_version=None):
    """(intent | None, reason | None) — 재서술 대상 턴의 세계 의도.

    · DYNAMIC 세션: 이력의 의미 기록(action_intent.semantic)과 그 턴의 definition_revision이 있어야 하고,
      revision이 현재 바인딩과 같아야 한다(다른 정의로 옛 선언을 재해석하지 않는다 — 불일치는 거부).
    · LEGACY 세션의 레거시 턴: (None, None) — 기존 재서술 그대로.
    · 판단층위는 다시 부르지 않는다. 경로·접근 선택은 의미 기록 그대로(재질문 없음).
    """
    from . import action_intent as _ai
    ctx = _pw.session_world_context(session)
    j = (judgment_record or {}).get("judgment") if isinstance(judgment_record, dict) else None
    ai = (j or {}).get(_ai.INTENT_FIELD) if isinstance(j, dict) else None
    if ctx.mode == _pw.MODE_BLOCKED:
        return None, f"동적 세계 정의를 읽을 수 없습니다({ctx.reason})"
    if not ctx.dynamic:
        if isinstance(ai, dict):
            return None, f"동적 세계 턴이지만 세션이 DYNAMIC이 아닙니다({ctx.reason})"
        return None, None
    if not isinstance(ai, dict) or not isinstance(ai.get("semantic"), dict):
        return None, "이 턴에는 보존된 세계 의미 기록이 없어 재서술할 수 없습니다(재해석하지 않음)"
    if ai.get("definition_revision") != ctx.definition_revision:
        return None, "이 턴의 정의 revision이 현재 세션 바인딩과 다릅니다 — 재서술 거부"
    if post_world_version is not None and post_world_version != getattr(session, "world_version", None):
        # 턴 커밋 뒤 관리자 세계 편집(ADMIN_EDIT)이 있었다 — 재서술이 편집을 조용히 지우지 않게 한다
        return None, "이 턴 이후 세계 편집이 있어 재서술할 수 없습니다(되감기를 사용하십시오)"
    return _ai.ResolvedActionIntent.from_semantic(ai["semantic"], definition_revision=ctx.definition_revision), None


# ══════════════════════════════════════════════════════════════
#  v7.38(D5) 상태 판정 재료 — 커밋 시점 투영 세계(READY) 기준 읽기 전용
# ══════════════════════════════════════════════════════════════

def status_turn_facts(session, prep) -> dict | None:
    """{now, before, start, place, daypart, activity} — DYNAMIC 턴의 커밋 대상 세계에서. 아니면 None.

    판정 자체는 core.status_rules(순수)가 한다. 이 함수는 시계·PC 배치·시간대·이번 턴 활동만 읽는다."""
    wt = getattr(prep, "world_turn", None)
    proj = getattr(getattr(prep, "world_projection", None), "world", None)
    if wt is None or not isinstance(proj, _wm.ProjectedWorld):
        return None
    clock = proj.domain("world_clock") or {}
    if "minute_index" not in clock:
        return None
    now = int(clock["minute_index"])
    pl = _sp.placement_of(proj, wt.ctx.lead_pc) or {}
    daypart = None
    cal = _calendar(wt.ctx.definition or {})
    if cal is not None:
        try:
            daypart = cal.daypart_of(now)
        except Exception:  # noqa: BLE001 — 시간대 모름
            daypart = None
    outcome = getattr(getattr(wt, "plan", None), "outcome", None) or {}
    # v7.46: 이번 턴 물건 연산(입기·벗기 포함)을 반영한 PC 착용 효과 태그
    from . import items as _items
    defs = _items.definitions_of(wt.ctx.definition)
    equip = traits = None
    care = []
    ip = getattr(getattr(prep, "extraction_plan", None), "item_plan", None)
    if ip is not None:
        try:
            _sim = _items.run_ops(session, ip.ops, defs=ip.defs, pc_name=ip.pc_name, scene_place=ip.scene_place,
                                  transaction_id=ip.transaction_id, recipes=ip.recipes)
            equip, traits, care = _sim.equip, _sim.traits, _sim.care
        except Exception:  # noqa: BLE001 — 커밋이 같은 연산을 다시 검증한다(여기서는 직전 착용으로)
            equip = traits = None
    # v7.47(S4): PC에게 쓴(실패하지 않은) 물건의 사용 효과 → 처치, 섭취 묶음 연결이 있으면 그 묶음도 섭취로 친다
    #   착용·처치는 PC 이름이 소유자 키다(lead_pc는 행위자 id — v7.46은 여기서 행위자 id로 찾아 착용이 늘 비었다)
    uid = (getattr(wt.ctx, "uid_of", None) or {}).get(wt.ctx.lead_pc)
    pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name") or getattr(ip, "pc_name", None)
    effects = {e for c in care if c.get("on") == pc_name and not c.get("failed") for e in c.get("effects") or ()}
    care_by = {}                                   # v7.48(D-3): NPC에게 쓴 처치도 그 인물의 처치로
    for c in care:
        if c.get("on") and c.get("on") != pc_name and not c.get("failed"):
            care_by.setdefault(c["on"], set()).update(c.get("effects") or ())
    uv = _items.effect_vocab().get("use_effects") or {}
    intake = frozenset(uv[e]["intake"] for e in effects if (uv.get(e) or {}).get("intake"))
    return {"now": now, "before": int((getattr(session, "world_clock", None) or {}).get("minute_index", now)),
            "start": int(clock.get("start_minute_index", now)),
            "place": pl.get("place_id") if pl.get("kind") == _sp.AT_PLACE else None,
            "daypart": daypart, "activity": outcome.get("activity"),
            "worn": (worn := _items.worn_tags(session, pc_name, defs, equipment=equip, traits=traits)),
            "care": frozenset(effects), "care_consumed": intake, "care_used": intake,
            "care_by": {k: frozenset(v) for k, v in care_by.items()},
            "moved": _pc_moved(session, wt.ctx.lead_pc, pl),
            "noise": _turn_noise(prep, worn),
            "event_statuses": _event_statuses(wt.ctx.definition, proj, session)}


def _event_statuses(definition, proj, session) -> dict:
    """v7.49: 사건이 인물에게 붙인 상태(SET_ACTOR_STATUS) 중 아직 세션 상태에 없는 것 — {인물 이름: {상태: 분}}.
    세션 상태 키는 인물 이름이다(저작 NPC = legacy_key)."""
    from . import events as _ev
    acts = (definition or {}).get("actor_definitions") or {}
    have = getattr(session, "statuses", None) or {}
    out = {}
    for aid, marks in _ev.actor_status_marks(proj.domain("event_instances") or {}).items():
        name = (acts.get(aid) or {}).get("legacy_key") or aid
        new = {s: m for s, m in marks.items() if s not in (have.get(name) or ())}
        if new:
            out[name] = new
    return out


def _pc_moved(session, lead, after_pl) -> bool:
    """v7.48: 이번 턴 PC가 장소를 옮겼는가(턴 시작 정본 배치 대 투영 배치)."""
    before = _sp.placement_of(session, lead) or {}
    return (before.get("kind"), before.get("place_id")) != (after_pl.get("kind"), after_pl.get("place_id"))


def _turn_noise(prep, worn=()):
    """v7.48(E4): 이번 턴 PC 행동의 소리 — 추출 관찰 noise(+ 입은 '소음' 효과면 한 단계 위), 모르면 None."""
    raw = (getattr(prep, "world_observation_raw", None) or {}).get(NOISE_FIELD)
    from .status_rules import NOISE_LEVELS
    if raw not in NOISE_LEVELS:
        return None
    lvl = NOISE_LEVELS.index(raw)
    if "소음" in (worn or ()) and lvl > 0:          # 조용히 움직일 때(QUIET)는 그대로 — 행동이 소리를 낼 때만 커진다
        lvl = min(lvl + 1, len(NOISE_LEVELS) - 1)
    return NOISE_LEVELS[lvl]



def pursuit_hearing_ops(session, prep, ctx, res, pc) -> tuple:
    """v7.48(E4): 이번 턴 PC 소리(추출 noise + 입은 '소음')가 닿은 추적자에 들은 장소·분을 기록(다음 턴 반응).

    커밋 직전 투영 세계 기준 — 이번 턴 연산 전부를 인과 부모로 둔다.
    """
    from . import items as _items
    from . import pursuit as _pu
    D = ctx.definition
    if not _pu.configs(D):
        return ()
    pl = _sp.placement_of(res.projected_world, ctx.lead_pc) or {}
    if pl.get("kind") != _sp.AT_PLACE:
        return ()
    worn = _items.worn_tags(session, pc, _items.definitions_of(D))
    noise = _turn_noise(prep, worn)
    now = (res.projected_world.domain("world_clock") or {}).get("minute_index")
    ops = _pu.hearing_ops(D, res.projected_world, now=now, pc_place=pl.get("place_id"), noise=noise,
                          prefix=turn_op_prefix("puh", prep.transaction_id))
    if not ops:
        return ()
    barrier = [o.op_id for o in res.ops]
    return tuple(_wm.make_op(o.op_id, o.op_type, o.source_kind, o.payload, causal_parent_op_ids=barrier)
                 for o in ops)
