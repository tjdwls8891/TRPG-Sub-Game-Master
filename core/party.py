# NPC 파티 — 역할군이 뚜렷한 동행 (v7.51, 파티 엔진 계획서 v1 · 사용자 결정 D-P1~P6 가)
#
# [축] 새 명단을 만들지 않는다 — 파티원 = 동행(session.companions) 중 인물 정의에 `party`가 있는 인물.
#   함께 이동은 v7.25 동행 이동, 부상·빈사·사망은 v7.48 NPC 상태 규칙·v7.50 전투 쓰러짐, 기술·마력은 v7.50 상시·발동.
# [저작] 기능 선언 `party`(의존 combat_wounds·dynamic_events) +
#   · 시나리오 `party_roles` {역할 이름: {act, stat?, domains?, bonus?, cost?, effect?}} · `party_limit`(기본 4)
#   · 인물 `party` {role, join_when?} — routine·behavior 옆 자리. join_when(조건 AST)이 없으면 사건으로만 합류.
#   · 인물 `stance` {type: "hostile", kill_points?} — 적(몬스터). 공격·막기·보조의 상대이고, PC가 쓰러뜨리면 처치 성장.
#   · 사건 단계 JOIN_PARTY·LEAVE_PARTY {actor} (core.events — 지역 값 party:<ActorId>)
# [역할 행동 — 엔진 어휘 4종] 시나리오는 역할 이름을 자유롭게 짓고 행동 하나에 잇는다.
#   막기: 전투 턴, 적이 PC를 이긴 공격 대결 하나의 부상을 대신 받는다(턴 1회).
#   공격: 전투 턴, 장면의 적 하나와 코드가 대결을 굴린다(역할 능력치·분야, 상시 기술 자동 + 마력이 남은 가장 큰 발동 기술).
#   치유: 매 턴 1회, 처치 효과(effect, 기본 응급처치)를 가장 위태로운 PC·파티원에게 쓴다(v7.47 처치 규칙 그대로).
#   보조: 전투 턴, PC의 공격 대결 굴림에 bonus(기본 +4)를 '파티 보조' 항으로 더한다.
#   cost {status, stages} — 그 행동을 할 때 오르는 비용 상태(고갈이면 그 행동을 못 한다).
#   행동 불능(역할 incapacitated 상태)인 파티원은 아무것도 하지 않는다. 플레이어가 명령한 파티원(그 턴 판단 굴림의
#   행위자)은 그 턴 자동 행동을 건너뛴다(D-P1 가).
from __future__ import annotations

CAPABILITY = "party"
ACTS = ("막기", "공격", "치유", "보조")
GUARD, STRIKE, HEAL, ASSIST = ACTS
ROLE_KEYS = ("act", "stat", "domains", "bonus", "cost", "effect")
MEMBER_KEYS = ("role", "join_when")
STANCE_KEYS = ("type", "kill_points")
HOSTILE = "hostile"
DEFAULT_LIMIT = 4
DEFAULT_ASSIST = 4
DEFAULT_EFFECT = "응급처치"
ASSIST_LABEL = "파티 보조"


# ── 저작 검사(컴파일러가 부른다) ─────────────────────────────

def check_roles(roles, *, statuses, domains, effects) -> list:
    """party_roles 검사 — [(경로, 문구)]."""
    if not isinstance(roles, dict) or not roles:
        return [("party_roles", "{역할 이름: {act, ...}} 객체여야 합니다")]
    errs = []
    for name, r in roles.items():
        p = f"party_roles.{name}"
        if not isinstance(r, dict):
            errs.append((p, "역할은 객체"))
            continue
        if set(r) - set(ROLE_KEYS):
            errs.append((p, f"알 수 없는 키 {sorted(set(r) - set(ROLE_KEYS))}"))
        if r.get("act") not in ACTS:
            errs.append((f"{p}.act", f"행동은 {list(ACTS)} 중 하나"))
        if r.get("stat") is not None and not isinstance(r["stat"], str):
            errs.append((f"{p}.stat", "능력치 이름(문자열)"))
        for dm in r.get("domains") or ():
            if dm not in domains:
                errs.append((f"{p}.domains", f"알 수 없는 굴림 분야 {dm!r}"))
        b = r.get("bonus")
        if b is not None and not (isinstance(b, int) and not isinstance(b, bool) and 0 < b <= 12):
            errs.append((f"{p}.bonus", "1~12 정수"))
        c = r.get("cost")
        if c is not None and not (isinstance(c, dict) and c.get("status") in statuses
                                  and isinstance(c.get("stages", 1), int) and c.get("stages", 1) >= 1):
            errs.append((f"{p}.cost", "{status: 정의된 상태, stages?: 양의 정수}"))
        if r.get("effect") is not None and r.get("effect") not in effects:
            errs.append((f"{p}.effect", f"알 수 없는 사용 효과 {r.get('effect')!r}"))
    return errs


def check_member(member, roles, path) -> list:
    if not isinstance(member, dict) or set(member) - set(MEMBER_KEYS):
        return [(path, f"party는 {{{', '.join(MEMBER_KEYS)}}} 객체")]
    if member.get("role") not in (roles or {}):
        return [(f"{path}.role", f"party_roles에 없는 역할 {member.get('role')!r}")]
    return []


def check_stance(stance, path) -> list:
    if not isinstance(stance, dict) or set(stance) - set(STANCE_KEYS) or stance.get("type") != HOSTILE:
        return [(path, f"stance는 {{type: {HOSTILE!r}, kill_points?}} 객체")]
    k = stance.get("kill_points")
    if k is not None and not (isinstance(k, int) and not isinstance(k, bool) and k >= 0):
        return [(f"{path}.kill_points", "0 이상 정수")]
    return []


# ── 정의 읽기 ────────────────────────────────────────────────

def config(definition) -> dict | None:
    """{"roles", "limit"} — 기능 party를 선언한 정의만."""
    c = (definition or {}).get("party")
    return c if isinstance(c, dict) else None


def _actor(definition, aid) -> dict:
    return ((definition or {}).get("actor_definitions") or {}).get(aid) or {}


def role_of(definition, aid):
    """(역할 이름, 역할 정의) 또는 None."""
    c = config(definition)
    m = _actor(definition, aid).get("party")
    if not c or not isinstance(m, dict):
        return None
    r = (c.get("roles") or {}).get(m.get("role"))
    return (m["role"], r) if isinstance(r, dict) else None


def is_hostile(definition, aid) -> bool:
    s = _actor(definition, aid).get("stance")
    return isinstance(s, dict) and s.get("type") == HOSTILE


def kill_points(definition, aid, default: int = 0) -> int:
    s = _actor(definition, aid).get("stance")
    if not isinstance(s, dict) or s.get("type") != HOSTILE:
        return 0
    k = s.get("kill_points")
    return int(k) if isinstance(k, int) and not isinstance(k, bool) else int(default or 0)


def limit(definition) -> int:
    return int((config(definition) or {}).get("limit") or DEFAULT_LIMIT)


def legacy_key(definition, aid):
    return _actor(definition, aid).get("legacy_key")


# ── 명단 ─────────────────────────────────────────────────────

def member_ids(definition, companions, resolve) -> list:
    """동행 이름 → 파티 역할이 있는 ActorId(동행 순서, 중복 없음). resolve(이름) → ActorId|None."""
    out = []
    for n in companions or ():
        aid = resolve(n)
        if aid and aid not in out and role_of(definition, aid):
            out.append(aid)
    return out


def join_allowed(definition, world, aid, members, *, progress=None):
    """합류 가능 (bool, 사유). 파티 역할이 없는 인물은 파티와 무관한 동행이라 늘 허용한다."""
    if not role_of(definition, aid):
        return True, None
    if aid in (members or ()):
        return True, None
    if len([m for m in members or () if m != aid]) >= limit(definition):
        return False, "PARTY_FULL"
    cond = (_actor(definition, aid).get("party") or {}).get("join_when")
    if cond is None:
        return False, "EVENT_ONLY"
    from . import predicates as _pr
    from . import world_refs as _refs
    try:
        ok = _pr.holds(cond, _refs.EvalContext(definition, world, progress=dict(progress or {})))
    except Exception:  # noqa: BLE001 — 평가할 수 없으면 합류하지 않는다
        ok = False
    return (True, None) if ok else (False, "JOIN_CONDITION")


# ── 행동 재료(순수) ──────────────────────────────────────────

def best_invoked(profile, scenario_data, domain, exhausted) -> str:
    """파티원이 그 분야에 쓸 발동 기술 — S가 가장 큰 것(고갈 비용이면 제외, 동률은 이름 순). 없으면 ''."""
    from . import judgment as _j
    best, name = 0, ""
    for n in sorted(_j.owned_skills(profile)):
        e = _j.skill_entry(scenario_data, n)
        if not e or e["kind"] != _j.SKILL_INVOKED or (e["domains"] and domain not in e["domains"]):
            continue
        cost = e["cost"] if isinstance(e["cost"], dict) else None
        if cost and exhausted(cost.get("status")):
            continue
        if e["bonus"] > best:
            best, name = e["bonus"], n
    return name


def cost_of(role_def) -> dict:
    c = (role_def or {}).get("cost")
    return {c["status"]: int(c.get("stages") or 1)} if isinstance(c, dict) and c.get("status") else {}


def heal_target(candidates, effect, merged, statuses, clocks) -> str | None:
    """치유 대상 — 그 처치 효과가 듣는 상태를 가진 이름 중 (역할 dying 먼저, 다음 단계 큰 순, 이름 순) 첫째."""
    from . import status_rules as _sr
    best = None
    for n in candidates:
        have = (statuses or {}).get(n) or ()
        clock = (clocks or {}).get(n) or {}
        score = 0
        for st in have:
            act = ((merged.get(st) or {}).get("care") or {}).get(effect)
            if act is None:
                continue
            # v7.53.1(F3): 효과 없는 처치는 고르지 않는다 — 이미 처치한 1단계(treat)·완화 중(ease)
            stg = _sr.stage_of(clock, st) or 1
            if act == "treat" and stg <= 1 and st in (clock.get("cared") or {}):
                continue
            if act == "ease" and st in (clock.get("eased") or {}):
                continue
            s = 1000 if _sr.ROLE_DYING in ((merged.get(st) or {}).get("roles") or ()) else stg
            score = max(score, s)
        if score and (best is None or (-score, n) < (-best[0], best[1])):
            best = (score, n)
    return best[1] if best else None
