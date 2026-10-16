# 턴 세계 — 묘사 투영·관찰·같은 분 선후·보충 진행·후속 묘사·투영 미디어 (H05-B·C)
#
# [권위 — DESIGN 12 · 11 §28~33 · 09 §18~22 · H05 handoff §4~5]
#   전부 순수 함수다(세션 불변). 결과 ops는 PLAYER_TURN 묶음에 담겨 READY → CommitCoordinator로만 커밋된다.
#   · 묘사 입력: 준비된 계획의 **투영 장면**(목적지 도착이면 목적지, 중단이면 중간 장소/TRANSIT)과
#     PC가 관찰할 수 있는 결과만 준다. 실제 숨은 경로·접근·미래 진실은 넣지 않는다.
#   · 추출(NarrationObservation): 확정 묘사를 관찰할 뿐 두 번째 세계 시뮬레이터가 아니다.
#       허용: 실제로 묘사된 정보 노출(후보 키) · 코드가 소유하지 않은 추가 시간 · 같은 분 선후 선택 ·
#             이미 표현된 신호 표시.
#       금지: 사건 국면/케이스 선택 · 숨은 경로/접근 발견 · 코드 소유 이동/시간 덮어쓰기 · PC 믿음 창작.
#   · 코드 소유 경과(이동·대기)가 있으면 추출의 추가 시간은 버린다(이중 계상 금지).
#   · 같은 분 서사 선후: 묘사가 고른 **허용 순서 하나**만 받는다 — 모호·무효면 READY 불가(임의 last-write 없음).
#     장면 밖 충돌은 H04가 이미 WORLD_PROJECTION_CONFLICT로 끝낸다(선택지로 보내지 않는다).
#   · 보충 진행의 결정적 충돌은 재시도 사유가 아니다(provider 재호출·RETRY_PENDING 없음).
#   · ForegroundContinuationPlan: 추출이 만든 새 결과가 현재 장면에서 관찰 가능(IF_OBSERVABLE)하고
#     아직 표현되지 않았을 때만. 같은 논리 턴·추가 시간 0·재추출 없음.
from __future__ import annotations

from dataclasses import dataclass, field

from . import cognition as _cog
from . import information as _info
from . import player_world as _pw
from . import place_display as _pd
from . import spatial as _sp
from . import world_clock as _clock
from . import world_mutation as _wm
from . import world_progression as _wp
from .world_ids import stable_digest

OBS_FIELD = "world_observation"
MAX_EXTRA_MINUTES = 240                # 추출이 주는 비코드 소유 추가 시간 상한(안전)

TW_READY = "READY"
TW_TIE_UNRESOLVED = "TIE_UNRESOLVED"   # 묘사 선후가 모호·무효 — READY 불가
TW_CONFLICT = "CONFLICT"               # WORLD_PROJECTION_CONFLICT — 결정적, 재시도 없음


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
    continuation_text: str | None = None          # READY 전 생성, COMMITTED 후 전달
    continuation_delivered: bool = False
    replan: object = None                         # 선후 확정 재계획 호출(callable(orders) -> plan)
    identified_faces: tuple = ()                  # SVG 카드: 지시층위가 지정한 얼굴 식별 ActorId(검증됨)
    identified_names: tuple = ()                  # SVG 카드: 지시층위가 지정한 이름 식별 ActorId(검증됨)
    discovery: dict | None = None                 # v7.14 이번 턴 주변 발견 결과(NEW만 정본화 — 추출 시점 고정)
    item_manifest: object = None                  # v7.17 core.items.ItemManifest(추출 시점 — 소유자·소지·현장 키)


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
            "weekday": WEEKDAY_KO[wd] if wd is not None else None}


WEEKDAY_KO = ("월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일")


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
    return ("\n[현재 작중 시각 — 정본]\n· " + clock_text(cv) + "\n"
            "· 본문과 상태 블록의 날짜·요일·시각 표기는 이 값을 따를 것. 다른 날짜·요일·시각을 지어내지 말 것"
            "(경과일 등 다른 표기를 쓰더라도 이 날짜와 어긋나지 않게).\n")


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
    for sig in observable_signals(plan.signals):
        lines.append(f"· 지금 관찰되는 일: {sig['cue']}")
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
        lines += exits[:10]
        lines.append("미방문 장소는 확정적으로 묘사하지 말 것. 이름을 모르는 곳은 표시된 이름으로만 부를 것.")
    return "\n".join(lines) + "\n"


def visible_exit_names(ctx, session) -> list:
    """현재 장소에서 PC가 아는 출구의 표시명(PC 지식 수준) — 목적지 되묻기용."""
    world, scene = _scene_world_and_place(ctx, None, session)
    if scene is None:
        return []
    D = ctx.definition
    know = _sp.actor_knowledge(world, ctx.lead_pc)
    out = []
    for eid, e in sorted((D.get("movement_edges") or {}).items()):
        if e.get("from") == scene and _sp.knows_edge(know, eid):
            nm = _pd.display_name(D, know, e["to"], require_known=False)
            if nm and nm not in out:
                out.append(nm)
    return out


def destination_question(ctx, session) -> str:
    from .master import say
    names = visible_exit_names(ctx, session)
    q = say("turn.destination_question", "어디로 가시겠습니까?")
    if names:
        return q + " 지금 보이는 길: " + ", ".join(names[:10]) + "."
    return q + " 가려는 곳을 구체적으로 말씀해 주세요."


def narration_world_block(wt, session) -> str:
    """DYNAMIC 묘사층위용 세계 블록 — 정본 장소·출구 + 코드 확정 결과(장소 고정 포함) + 현재 장면 인물.

    GM 지시문은 공백이 접혀 한 문단이 되므로, 세계 제약은 지시문 뒤가 아니라 독립 블록으로 준다.
    """
    if wt is None:
        return ""
    parts = [narration_clock_block(wt, session), dynamic_place_block(wt.ctx, wt.plan, session, for_narration=True)]
    if wt.plan is not None:
        parts.append(narration_overlay(wt.plan, wt.ctx.definition, wt.ctx, world=session))
    parts.append(scene_npc_block(wt.ctx, wt.plan, session))
    return "".join(p for p in parts if p)


MAX_GUIDE_LINES = 8


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
    for iid in sorted(ev):
        inst = ev[iid]
        if not isinstance(inst, dict) or inst.get("lifecycle") != _ev.ACTIVE:
            continue
        tpl = _ev.template(D, inst.get("template_id")) or {}
        ph = (tpl.get("phases") or {}).get(inst.get("current_phase")) or {}
        if not ph.get("guide") or not _ev.instance_relevant(D, inst, scene):
            continue
        lines.append(f"· {tpl.get('name') or inst.get('template_id')}: {ph['guide']}")
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
            "이 사건의 분기로 억지 해석하지 말 것)]\n" + "\n".join(lines) + "\n")


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
        bits.append("관계 변화 " + " / ".join(deltas[-3:]))
    role = (p.get("authored_profile") or {}).get("role")
    head = f"- {p['display_name']}" + (f"({role})" if role else "")
    return head + (": " + " · ".join(bits) if bits else "")


def scene_npc_block(ctx, plan, session) -> str:
    """지시·묘사층위용 현재 장면 인물 블록(정본 배치 기준 · 현재 상태 투영)."""
    scene, projs = scene_npc_projections(ctx, plan, session)
    where = scene or "이동 중(장소 밖)"
    lines = [f"\n[현재 장면 인물 — 정본 위치 기준 · 장면: {where}]",
             "※ 정의된 NPC 중 이 목록의 인물만 지금 이 장면에 있다. 장소의 연고 인물·NPC 사전의 거처는 "
             "현재 위치가 아니다 — 목록에 없는 정의 NPC를 이 장면에 있다고 묘사하지 말 것.",
             "※ 목록 밖 정의 인물(룰북 인물 사전의 이름)을 이름·정체로 등장시키지 말 것. 새로 필요한 인물은 "
             "이름 없는 인물로 다룬다."]
    lines += [_npc_line(p) for p in projs] or ["(정본상 이 장면에 있는 정의 NPC 없음)"]
    return "\n".join(lines) + "\n"


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
                       if self.discoveries else {})}}

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


def _scene_relevant(g, scene, rec) -> bool:
    """정보 정의의 장소 주제가 현재 장면(또는 그 상·하위)에 닿는가. 장소 주제가 없으면 장면 무관하게 허용."""
    places = [r[len("place:"):] for r in (rec.get("subject_refs") or ()) if str(r).startswith("place:")]
    if not places or not scene:
        return not places
    path = set(g.path_of(scene))
    return any(p in path or scene in g.path_of(p) for p in places if p in g.places)


def build_observation_manifest(definition: dict, ctx, plan, base_world, *, discovery=None) -> ObservationManifest:
    """추출 관찰 후보 — **이번 턴 노출 근거가 있는 것만**(R2).

    정보: 이번 턴 코드 계획(stage-1)의 사건이 결정적으로 만든 정보 중 장면과 관련되고 공개 안전 요약
          (summary)이 있는 것. 명제 전문·숨은 정보로 대체하지 않는다. 같은 장면 NPC가 내용을 안다는
          사실만으로는 후보가 아니다(노출 계획 없음 = 후보 없음).
          H07: 본 묘사에 준 관찰 신호의 exposes(저작 정보 — summary 필수)도 후보다(신호 = 노출 계획).
    신호: 본 묘사에 실제로 준 관찰 신호만. ACTIVE 사건의 미래 EMIT_SIGNAL은 나열하지 않는다.
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
    if planned:
        istate = world.domain("information_state") or {}
        for s in observable_signals(plan.signals):
            for iid in s.get("exposes") or ():
                rec = _info.resolve_definition(definition, istate, iid) or {}
                summary = rec.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    continue
                if (pc_recs.get(iid) or {}).get("awareness") == _cog.CONTENT:
                    continue
                cand.setdefault(iid, summary)
    infos = tuple((f"X{n}", iid, cand[iid]) for n, iid in enumerate(sorted(cand), 1))
    sigs = [((s["signal_id"], s["instance_id"]), s["cue"])
            for s in observable_signals(plan.signals if plan is not None else ())]
    signals = tuple((f"S{n}", k, cue) for n, (k, cue) in enumerate(sigs, 1))
    ties = tuple(tie_option_labels(plan, definition, ctx)) if plan is not None and plan.status == _pw.PLAN_TIE else ()
    extra = not (plan is not None and plan.code_owned_minutes)
    # v7.14: 현재 장면의 유효 발견(SUPERSEDED는 나오지 않는다 — 같은 물건 두 번 수거 불가) + 이번 턴 새 발견
    drows = list(discoveries_at(definition, world, lead, scene))
    if discovery and discovery.get("status") == "NEW":
        drows.append((DISCOVERY_NEW_KEY, discovery["info_id"], discovery["target"], discovery["kind"]))
    return ObservationManifest(infos, signals, ties, extra, tuple(drows))


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
    continuation_signals: list = field(default_factory=list)
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


def process_observation(ctx, base_world, plan, obs_manifest: ObservationManifest, raw_obs, *,
                        transaction_id: str, narrative_seed: str, replan=None,
                        face_info_ids=(), name_info_ids=(), discovery=None,
                        read_info_ids=()) -> TurnWorldResult:
    """확정 묘사 관찰 → 보충 typed 연산 + 보충 세계 진행 → 최종 묶음 연산(순수).

    v7.17 read_info_ids: 코드가 검증한 아이템 READ(실제 소지·현장 물건의 저작 info_refs)로 PC가 내용을 알게 된 정보 —
    같은 정보·인지 경로(DOCUMENT, 믿음 불변)로 같은 묶음에 합류한다. 아이템 소유만으로는 생기지 않는다."""
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
    # ②-c v7.14 주변 발견: 승인·성공 발견 생성 + PC 인지, 추출이 키로 지목한 수거·소진(같은 묶음 — 원자적)
    d_ops, gains, d_diags = discovery_ops(ctx, start_world, obs_manifest, raw, discovery, prefix=prefix,
                                          transaction_id=transaction_id,
                                          at_minute=(start_world.domain("world_clock") or {}).get("minute_index"))
    acq += d_ops
    diags += d_diags
    # ③ 추가 시간(코드 소유 경과가 있으면 버린다)
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
    for s in observable_signals(plan.signals if plan is not None else ()):
        represented.add((s["signal_id"], s["instance_id"]))          # 묘사 전 신호는 본 묘사가 표현했다
    # ④ 보충 진행(노출로 인한 연쇄 + 추가 시간)
    scene = (plan.scene_place if plan is not None and plan.status == _pw.PLAN_PLANNED else None) or \
        (_sp.placement_of(start_world, lead) or {}).get("place_id")
    now = (start_world.domain("world_clock") or {}).get("minute_index")
    ops, world, sigs = list(stage1), start_world, []
    if acq or extra:
        r = _wp.project_world_progression(D, start_world, now + extra, base_ops=acq,
                                          progress={"observer_actor": lead, "scene_place": scene},
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
    cont = unrepresented_signals(sigs, represented)
    return TurnWorldResult(TW_READY, tuple(ops), world, plan, sigs, cont, tuple(sorted(represented)),
                           diags, tuple(exposures), extra, item_gains=tuple(gains))


# ══════════════════════════════════════════════════════════════
#  ForegroundContinuationPlan · 투영 미디어
# ══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ForegroundContinuationPlan:
    source_transaction_id: str
    at_minute: int | None
    projected_scene: str | None
    signals: tuple

    def prompt(self) -> str:
        cues = "\n".join(f"- {s['cue']}" for s in self.signals)
        return ("[후속 묘사 — 같은 턴의 마지막 세계 반응]\n"
                "[이미 확정된 즉시 세계 반응 — 아래만 짧게 묘사한다]\n" + cues + "\n"
                f"[현재 장면] {self.projected_scene or '현재 장소'}\n"
                "[금지] 새 사건·새 이동·시간 경과·새 인물 계획을 만들지 말 것. PC의 선택·생각·감정·행동을 "
                "정하지 말 것. 앞으로 일어날 일을 암시하거나 밝히지 말 것.")


def continuation_plan(transaction_id: str, result: TurnWorldResult) -> ForegroundContinuationPlan | None:
    if result is None or result.status != TW_READY or not result.continuation_signals:
        return None
    w = result.projected_world
    minute = (w.domain("world_clock") or {}).get("minute_index") if w is not None else None
    scene = result.plan.scene_place if result.plan is not None else None
    return ForegroundContinuationPlan(transaction_id, minute, scene, tuple(result.continuation_signals))


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
