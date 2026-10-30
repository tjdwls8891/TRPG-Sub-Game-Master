# 추적자 행동 — 생존물 살인마처럼 PC를 감지해 쫓는 인물 (v7.48 E3·E4, 계획서 v1 · 사용자 결정 A2 나 · A3 가)
#
# [저작] 인물 정의의 행동 유형 — default_npcs[이름].behavior {type: "pursuer", mode, pace?, patrol?, hearing?,
#   lose_after_minutes?, search_minutes?, capabilities?, active_when?}. 일과(routine)와 같은 자리다 — 인물의 움직임은
#   인물 정의 하나(routine = 평상시 · behavior = 행동 유형)에서 나온다. 시나리오는 기능 `pursuit`을 선언해야 한다
#   (scenario_schema.capabilities — 의존: stealth·dynamic_events·dynamic_actor_location).
#   컴파일러가 검증해 정의 actor_definitions[ActorId].behavior로 옮기고, 추적자마다 상태 저장용 사건 템플릿
#   `pursuer_<ActorId>`(시작 즉시 생성, 국면 하나)를 만든다. 추적자 상태는 그 인스턴스의 사건 지역 상태다 —
#   새 세션 필드·새 typed 연산 없이 기존 EVENT_LOCAL_SET으로 되감기·재현된다.
#     지역 상태 키: mode(WANDER·ALERT·CHASE·SEARCH) · target(목표 장소) · since(그 모드가 시작된/마지막 감지 분) ·
#                   patrol_i(순회 다음 번호) · heard({place, minute} — 직전 커밋에서 들은 소리)
#
# [매 턴 판단 — 묘사 전 계획(plan_player_turn), 결정적·순수]
#   추적자에게 역할 incapacitated 상태(기절·빈사 등)가 있거나 active_when이 거짓이면 멈춤. 구간 위(TRANSIT)면 도착할
#   때까지 판단하지 않는다. 보임(같은 장소 또는 정적 가시성, PC에게 역할 concealed 상태 없음) → CHASE(PC 장소, 추적 속도) ·
#   들음 → ALERT(소리 난 장소) · CHASE·ALERT 목표에 닿음 → SEARCH(그 자리) · SEARCH가 search_minutes 지남 → WANDER ·
#   CHASE가 lose_after_minutes 동안 감지 없음 → SEARCH · WANDER → 순회지 다음 장소.
#   이동은 사건 이동과 같은 경로·시간 계산(world_progression._movement_ops — 일반 그래프 + 행위자 지식)을 쓰고, 이번 턴
#   경과 시간 동안의 이동·도착은 기존 세계 진행이 처리한다.
# [소리 — 커밋 단계] 추출 noise(조용함·보통·시끄러움)의 도달 거리(구간 수, 방향 무시) 안의 추적자는 그 장소·분을 기록
#   (heard)하고, 다음 턴 판단이 반응한다(소리를 낸 그 턴에 순간이동하지 않는다).
from __future__ import annotations

import copy

from . import spatial as _sp
from . import world_mutation as _wm
from .world_ids import stable_digest

WANDER, ALERT, CHASE, SEARCH = "WANDER", "ALERT", "CHASE", "SEARCH"
MODES = (WANDER, ALERT, CHASE, SEARCH)
BEHAVIOR_TYPE = "pursuer"
CAPABILITY = "pursuit"
TEMPLATE_PREFIX = "pursuer_"
SOURCE_KIND = "PURSUIT"
DEFAULTS = {"pace": {"wander": "1", "chase": "0.6"}, "hearing": {"LOUD": 2, "NORMAL": 0},
            "lose_after_minutes": 20, "search_minutes": 15}
KEYS = ("type", "mode", "pace", "patrol", "hearing", "lose_after_minutes", "search_minutes", "capabilities",
        "active_when")


def configs(definition) -> dict:
    """{ActorId: 행동 정의 + actor} — 인물 정의 behavior.type == pursuer인 인물(정렬)."""
    out = {}
    for aid, a in sorted(((definition or {}).get("actor_definitions") or {}).items()):
        b = (a or {}).get("behavior") if isinstance(a, dict) else None
        if isinstance(b, dict) and b.get("type") == BEHAVIOR_TYPE:
            out[aid] = {**b, "actor": aid}
    return out


def template_id(pid: str) -> str:
    return TEMPLATE_PREFIX + pid


def synth_templates(pids) -> dict:
    """컴파일러용 — 추적자마다 상태를 담을 사건 템플릿(시작 즉시 생성, 국면 하나)."""
    out = {}
    for pid in sorted(pids or ()):
        out[template_id(pid)] = {"version": 1, "name": f"추적자 {pid}", "spawn_when": {"const": True},
                                 "phases": {"hunt": {"eligible_when": {"const": True}, "priority": 1}}}
    return out


def check(behavior, *, path: str, places, modes) -> list:
    """저작 검사(행동 유형 pursuer 하나) — [(경로, 문구)]."""
    from .status_rules import NOISE_LEVELS
    c, p, errs = behavior, path, []
    if not isinstance(c, dict):
        return [(p, "behavior는 객체")]
    extra = set(c) - set(KEYS)
    if extra:
        errs.append((p, f"알 수 없는 키 {sorted(extra)}"))
    if c.get("mode") not in modes:
        errs.append((f"{p}.mode", f"정의되지 않은 이동 방식 {c.get('mode')!r}"))
    for x in c.get("patrol") or ():
        if x not in places:
            errs.append((f"{p}.patrol", f"정의되지 않은 장소 {x!r}"))
    pace = c.get("pace")
    if pace is not None and not (isinstance(pace, dict) and set(pace) <= {"wander", "chase"}
                                 and all(_factor_ok(v) for v in pace.values())):
        errs.append((f"{p}.pace", "pace는 {wander?, chase?: 양의 소수 문자열}"))
    hear = c.get("hearing")
    if hear is not None and not (isinstance(hear, dict) and set(hear) <= set(NOISE_LEVELS)
                                 and all(isinstance(v, int) and v >= 0 for v in hear.values())):
        errs.append((f"{p}.hearing", f"hearing은 {{{'·'.join(NOISE_LEVELS)}: 0 이상 정수(구간 수)}}"))
    for k in ("lose_after_minutes", "search_minutes"):
        if k in c and not (isinstance(c[k], int) and c[k] >= 0):
            errs.append((f"{p}.{k}", "0 이상 정수(분)"))
    caps = c.get("capabilities")
    if caps is not None and not (isinstance(caps, list) and all(isinstance(x, str) for x in caps)):
        errs.append((f"{p}.capabilities", "문자열 목록"))
    return errs


def context(definition, merged_statuses, statuses, pc) -> dict | None:
    """판단 입력 — PC가 숨었는가(역할 concealed) · 움직일 수 없는 추적자(역할 incapacitated). 추적자가 없으면 None.
    statuses = {인물 이름: [상태]}(세션 상태 — 인물은 표시 이름 키)."""
    from . import status_rules as _sr
    cfgs = configs(definition)
    if not cfgs:
        return None
    sts = statuses or {}
    acts = (definition or {}).get("actor_definitions") or {}
    disabled = [aid for aid in cfgs
                if _sr.has_role(merged_statuses, sts.get((acts.get(aid) or {}).get("legacy_key")) or (),
                                _sr.ROLE_INCAPACITATED)]
    return {"hidden": _sr.has_role(merged_statuses, sts.get(pc) or (), _sr.ROLE_CONCEALED), "disabled": disabled}


def _factor_ok(v) -> bool:
    from .movement import as_factor
    try:
        as_factor(v)
        return True
    except Exception:  # noqa: BLE001
        return False


def cfg_of(definition, pid) -> dict:
    c = dict(configs(definition).get(pid) or {})
    for k, v in DEFAULTS.items():
        c.setdefault(k, copy.deepcopy(v))
    c["pace"] = {**DEFAULTS["pace"], **(c.get("pace") or {})}
    return c


def instance_of(world, pid) -> str | None:
    """그 추적자의 상태 사건 인스턴스(시작 즉시 생성된 것)."""
    insts = _dom(world, "event_instances") or {}
    tid = template_id(pid)
    hits = sorted(iid for iid, r in insts.items() if isinstance(r, dict) and r.get("template_id") == tid)
    return hits[-1] if hits else None


def locals_of(world, iid) -> dict:
    return dict(((_dom(world, "event_instances") or {}).get(iid) or {}).get("local_state") or {})


def _dom(world, name):
    if isinstance(world, _wm.ProjectedWorld):
        return world._peek(name)
    return getattr(world, name, None)


def _adjacency(definition) -> dict:
    adj = {}
    for e in ((definition or {}).get("movement_edges") or {}).values():
        a, b = e.get("from"), e.get("to")
        if a and b:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
    return adj


def distance(definition, a, b, limit: int) -> int | None:
    """두 장소 사이 구간 수(방향 무시 — 소리는 양쪽으로 퍼진다), limit 밖이면 None."""
    if a is None or b is None:
        return None
    if a == b:
        return 0
    adj, seen, frontier = _adjacency(definition), {a}, [a]
    for d in range(1, limit + 1):
        nxt = []
        for x in frontier:
            for y in sorted(adj.get(x) or ()):
                if y == b:
                    return d
                if y not in seen:
                    seen.add(y)
                    nxt.append(y)
        frontier = nxt
    return None


def sees(definition, from_place, to_place) -> bool:
    if from_place is None or to_place is None:
        return False
    if from_place == to_place:
        return True
    try:
        return bool(_sp.SpatialGraph(definition).is_visible_from(to_place, from_place))
    except Exception:  # noqa: BLE001
        return False


def position(definition, world, actor) -> tuple:
    """(장소 | None, 구간 위 여부) — 유효 위치(정본 override > 일과)."""
    from . import npc_position as _npos
    pl = _npos.effective_position(definition, world, actor)["placement"] or {}
    if pl.get("kind") == _sp.AT_PLACE:
        return pl.get("place_id"), False
    return None, pl.get("kind") == _sp.TRANSIT


def _active(definition, world, cfg, progress) -> bool:
    cond = cfg.get("active_when")
    if cond is None:
        return True
    from . import predicates as _pr
    from . import world_refs as _refs
    try:
        return _pr.holds(cond, _refs.EvalContext(definition, world, progress=dict(progress or {})))
    except Exception:  # noqa: BLE001 — 조건을 평가할 수 없으면 멈춘다(보수적)
        return False


def decide(cfg, loc, *, now, here, pc_place, pc_hidden, sees_pc) -> tuple:
    """(새 지역 상태, 목적지 | None, 속도 키) — 순수. here = 추적자 장소(AT_PLACE)."""
    loc = dict(loc)
    mode = loc.get("mode") or WANDER
    since = int(loc.get("since") if loc.get("since") is not None else now)
    heard = loc.pop("heard", None)
    if sees_pc and not pc_hidden and pc_place is not None:
        loc.update(mode=CHASE, target=pc_place, since=now)
        return loc, (pc_place if pc_place != here else None), "chase"
    if isinstance(heard, dict) and heard.get("place") is not None:
        loc.update(mode=ALERT, target=heard["place"], since=int(heard.get("minute", now)))
        return loc, (heard["place"] if heard["place"] != here else None), "chase"
    if mode in (CHASE, ALERT):
        if loc.get("target") == here or loc.get("target") is None:
            loc.update(mode=SEARCH, target=here, since=now)
            return loc, None, "wander"
        if mode == CHASE and now - since >= int(cfg["lose_after_minutes"]):
            loc.update(mode=SEARCH, target=here, since=now)
            return loc, None, "wander"
        return loc, loc["target"], "chase"
    if mode == SEARCH:
        if now - since >= int(cfg["search_minutes"]):
            loc.update(mode=WANDER, since=now)
        else:
            return loc, None, "wander"
    patrol = list(cfg.get("patrol") or ())
    if not patrol:
        loc.update(mode=WANDER)
        return loc, None, "wander"
    i = int(loc.get("patrol_i") or 0) % len(patrol)
    if patrol[i] == here:
        i = (i + 1) % len(patrol)
    loc.update(mode=WANDER, patrol_i=(i + 1) % len(patrol), target=patrol[i])
    return loc, (patrol[i] if patrol[i] != here else None), "wander"


def plan_ops(definition, world, *, now, pc_place, pc_hidden, disabled=(), progress=None, prefix: str,
             diagnostics=None) -> list:
    """묘사 전 계획 — 추적자마다 상태 갱신(EVENT_LOCAL_SET)과 이동 연산. 추적자가 없으면 빈 목록.
    diagnostics(목록)를 주면 경로를 못 찾은 이동 같은 진단을 담는다(추적자는 그 자리에 남는다)."""
    from . import events as _ev
    from . import world_progression as _wpg
    ops = []
    for pid in sorted(configs(definition)):
        cfg = cfg_of(definition, pid)
        iid = instance_of(world, pid)
        actor = cfg.get("actor")
        if not iid or not actor or actor in set(disabled or ()):
            continue
        if not _active(definition, world, cfg, progress):
            continue
        here, moving = position(definition, world, actor)
        if moving or here is None:
            continue
        old = locals_of(world, iid)
        new, dest, pace = decide(cfg, old, now=now, here=here, pc_place=pc_place, pc_hidden=pc_hidden,
                                 sees_pc=sees(definition, here, pc_place))
        pre = f"{prefix}.{pid}"
        for k in sorted(set(old) | set(new)):
            if old.get(k) != new.get(k) and k in new:
                ops.append(_wm.make_op(f"{pre}.loc.{k}", _ev.OP_EVENT_LOCAL_SET, SOURCE_KIND,
                                       {"instance_id": iid, "key": k, "value": new[k]}))
        if "heard" in old and "heard" not in new:      # 소리는 한 번 반응하면 지운다(값 None — 키는 남는다)
            ops.append(_wm.make_op(f"{pre}.loc.heard", _ev.OP_EVENT_LOCAL_SET, SOURCE_KIND,
                                   {"instance_id": iid, "key": "heard", "value": None}))
        if dest is not None:
            mv, mdiags = _wpg._movement_ops(
                definition, world, actor=actor, here=here, dest=dest, mode=cfg["mode"], stance=None,
                stance_factor=cfg["pace"].get(pace, "1"), capabilities=cfg.get("capabilities"), route=None, now=now,
                movement_id="mv_" + stable_digest("pursuit.move.v1", pid, now, here, dest, prefix),
                owner_iid=iid, op_prefix=f"{pre}.mv", parents=[o.op_id for o in ops])
            ops += mv
            if diagnostics is not None:
                diagnostics += [dict(dg, pursuer=pid) for dg in mdiags]
    return ops


def hearing_ops(definition, world, *, now, pc_place, noise, prefix: str) -> list:
    """커밋 단계 — 이번 턴 PC 소리가 닿은 추적자에 heard 기록(다음 턴 판단 입력)."""
    from . import events as _ev
    from .status_rules import NOISE_LEVELS
    if noise not in NOISE_LEVELS or pc_place is None:
        return []
    ops = []
    for pid in sorted(configs(definition)):
        cfg = cfg_of(definition, pid)
        reach = (cfg.get("hearing") or {}).get(noise)
        iid = instance_of(world, pid)
        if reach is None or not iid:
            continue
        here, moving = position(definition, world, cfg.get("actor"))
        if here is None:
            continue
        if distance(definition, here, pc_place, int(reach)) is None:
            continue
        ops.append(_wm.make_op(f"{prefix}.{pid}.heard", _ev.OP_EVENT_LOCAL_SET, SOURCE_KIND,
                               {"instance_id": iid, "key": "heard", "value": {"place": pc_place, "minute": now}}))
    return ops


def threats(definition, world, *, pc_place) -> list:
    """장면 위협 — [{pid, actor, name, relation(SAME·NEAR), mode}] (NEAR = 한 구간 거리에서 추적·경계 중)."""
    from . import actors as _act
    out = []
    for pid in sorted(configs(definition)):
        cfg = cfg_of(definition, pid)
        actor = cfg.get("actor")
        here, _moving = position(definition, world, actor)
        iid = instance_of(world, pid)
        mode = (locals_of(world, iid) if iid else {}).get("mode") or WANDER
        rel = None
        if here is not None and here == pc_place:
            rel = "SAME"
        elif here is not None and mode in (CHASE, ALERT) and distance(definition, here, pc_place, 1) == 1:
            rel = "NEAR"
        if rel:
            try:
                name = _act.display_name(definition, world, actor) or actor
            except Exception:  # noqa: BLE001
                name = actor
            out.append({"pid": pid, "actor": actor, "name": name, "relation": rel, "mode": mode})
    return out
