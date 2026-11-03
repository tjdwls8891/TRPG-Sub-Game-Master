"""상태 규칙의 순수 판정 — 조건 평가(3값)·부여/해제/단계/처치/잠복 결정·코드 단계 상승.

옛 core.status_rules에서 옮겼다(리팩터링 T-1). 세션·provider·디스코드를 모르는 순수 함수만 둔다 —
입력은 상태 정의(공용+시나리오 병합)·판정 재료(Ctx)·추출 후보, 출력은 결정(Decision).
저작 규칙의 문법·의미는 core.status_rules 머리 주석이 정본 설명이다(이 모듈 이식 후에도 같은 문법).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

TRUE, FALSE, UNKNOWN = "TRUE", "FALSE", "UNKNOWN"


ROLE_CONCEALED, ROLE_INCAPACITATED, ROLE_DYING, ROLE_DEAD = "concealed", "incapacitated", "dying", "dead"


ROLES = (ROLE_CONCEALED, ROLE_INCAPACITATED, ROLE_DYING, ROLE_DEAD)


SCOPE_COMMON, SCOPE_SCENARIO = "공용", "시나리오"


RULE_KEYS = ("apply_gate", "apply_auto", "clear_auto", "clear_gate", "raise_auto", "lower_auto")


STATUS_TIMES = ("SINCE_INTAKE_GTE", "SINCE_APPLIED_GTE", "SINCE_STATUS_GTE", "SINCE_CLEARED_GTE", "SINCE_CARED_GTE")


CARE_ACTIONS = ("down", "treat", "ease")


NOISE_LEVELS = ("QUIET", "NORMAL", "LOUD")       # v7.48(E4) 이번 턴 PC 행동의 소리(작은 것부터)


EASE_MINUTES = 240


ALL_DOMAIN = "전체"


@dataclass
class Ctx:
    """한 턴 커밋 시점의 판정 재료(정본 투영 기준 — 순수 값)."""
    now: int
    start_minute: int
    turn_minutes: int = 0
    place: str | None = None
    place_chain: tuple = ()                   # 장면 장소와 그 상위(계층)
    place_groups: dict = field(default_factory=dict)   # 시나리오 status_places {묶음: {장소…}}
    daypart: str | None = None
    activity: str | None = None
    consumed: frozenset = frozenset()         # 이번 턴 CONSUME한 묶음
    used: frozenset = frozenset()             # 이번 턴 USE·CONSUME한 묶음
    active: frozenset = frozenset()           # 판정 전 PC 상태
    clock: dict = field(default_factory=dict)  # 이 PC의 status_clock 항목
    seed: str = ""                            # 잠복 판정의 결정적 근거(세션 narrative_seed + PC)
    worn: frozenset = frozenset()             # v7.46 PC 착용 효과 태그(없으면 빈 집합 — 입은 것이 없다는 사실)
    care: frozenset = frozenset()             # v7.47 이번 턴 PC에게 쓴 물건의 사용 효과(실패·물건 상태로 꺼진 것 제외)
    stage_in: dict = field(default_factory=dict)   # v7.47 추출이 본 PC 상태 단계 {상태: 단계}
    moved: object = None                      # v7.48 이번 턴 장소를 옮겼는가(None = 모름)
    noise: object = None                      # v7.48 이번 턴 소리(NOISE_LEVELS, None = 모름)
    npc: bool = False                         # v7.48(D-3) NPC 판정 — 추적하지 않는 사실은 모름
    actor_id: str = ""                        # T-7(G12) 잠복 판정 근거 — 행위자 id(없으면 옛 이름 근거)


def _t_all(vals):
    vals = list(vals)
    return FALSE if FALSE in vals else (UNKNOWN if UNKNOWN in vals else TRUE)


def _t_any(vals):
    vals = list(vals)
    return TRUE if TRUE in vals else (UNKNOWN if UNKNOWN in vals else FALSE)


def _b(x):
    return TRUE if x else FALSE


def evaluate(cond, ctx: Ctx, status: str) -> str:
    if not isinstance(cond, dict):
        return UNKNOWN
    if "all" in cond:
        return _t_all(evaluate(c, ctx, status) for c in cond["all"])
    if "any" in cond:
        return _t_any(evaluate(c, ctx, status) for c in cond["any"])
    if "not" in cond:
        return {TRUE: FALSE, FALSE: TRUE}.get(evaluate(cond["not"], ctx, status), UNKNOWN)
    if "const" in cond:
        return _b(cond["const"] is True)
    if "status_time" in cond:
        k, n = cond["status_time"], int(cond.get("minutes") or 0)
        if k == "SINCE_INTAKE_GTE" and ctx.npc:
            return UNKNOWN
        if k == "SINCE_INTAKE_GTE":
            last = (ctx.clock.get("intake") or {}).get(cond.get("group"), ctx.start_minute)
            return _b(ctx.now - int(last) >= n)
        if k == "SINCE_CARED_GTE":
            at = (ctx.clock.get("cared") or {}).get(cond.get("status") or status)
            return FALSE if at is None else _b(ctx.now - int(at) >= n)
        if k == "SINCE_CLEARED_GTE":
            name = cond.get("status")
            if name in ctx.active:
                return FALSE
            last = (ctx.clock.get("cleared") or {}).get(name, ctx.start_minute)
            return _b(ctx.now - int(last) >= n)
        name = status if k == "SINCE_APPLIED_GTE" else cond.get("status")
        if name not in ctx.active:
            return FALSE
        at = (ctx.clock.get("applied") or {}).get(name)
        return UNKNOWN if at is None else _b(ctx.now - int(at) >= n)
    if "latent_due" in cond:
        rec = (ctx.clock.get("latent") or {}).get(cond["latent_due"]) or {}
        return _b(rec.get("due") is not None and ctx.now >= int(rec["due"]))
    if "consumed" in cond:
        return UNKNOWN if ctx.npc else _b(cond["consumed"] in ctx.consumed)
    if "used" in cond:
        return UNKNOWN if ctx.npc else _b(cond["used"] in ctx.used)
    if "moved" in cond:
        return UNKNOWN if ctx.moved is None else _b(bool(ctx.moved) == bool(cond["moved"]))
    if "noise_at_least" in cond:
        if ctx.noise not in NOISE_LEVELS:
            return UNKNOWN
        return _b(NOISE_LEVELS.index(ctx.noise) >= NOISE_LEVELS.index(cond["noise_at_least"]))
    if "place_within" in cond:
        return UNKNOWN if ctx.place is None else _b(cond["place_within"] in ctx.place_chain)
    if "place_group" in cond:
        members = ctx.place_groups.get(cond["place_group"])
        return UNKNOWN if ctx.place is None or members is None else _b(bool(members & set(ctx.place_chain)))
    if "daypart_in" in cond:
        return UNKNOWN if ctx.daypart is None else _b(ctx.daypart in (cond["daypart_in"] or ()))
    if "activity_in" in cond:
        if ctx.activity is None:                  # v7.53.1: 활동을 모르면 모름(NPC는 PC와 함께한 파티원만 활동이 있다)
            return UNKNOWN
        ok = ctx.activity in (cond["activity_in"] or ())
        return _b(ok and ctx.turn_minutes >= int(cond.get("min_minutes") or 0))
    if "status_active" in cond:
        return _b(cond["status_active"] in ctx.active)
    if "stage_min" in cond:
        name = cond.get("status") or status
        if name not in ctx.active:
            return FALSE
        return _b(int((ctx.clock.get("stage") or {}).get(name, 1)) >= int(cond["stage_min"]))
    if "wearing" in cond:
        return UNKNOWN if ctx.npc else _b(cond["wearing"] in ctx.worn)
    return UNKNOWN


def groups_of(scenario_data) -> dict:
    g = (scenario_data or {}).get("intake_groups")
    return {k: [str(w) for w in v] for k, v in g.items() if isinstance(v, list)} if isinstance(g, dict) else {}


def place_groups_of(scenario_data) -> dict:
    g = (scenario_data or {}).get("status_places")
    return {k: {str(x) for x in v} for k, v in g.items() if isinstance(v, list)} if isinstance(g, dict) else {}


def groups_for_kind(kind: str, groups: dict) -> set:
    """물건 종류 → 묶음(종류의 낱말 하나가 목록 낱말과 같을 때)."""
    words = set(str(kind or "").split())
    return {g for g, ws in groups.items() if words & set(ws)}


def with_role(merged: dict, role: str) -> list:
    """그 역할을 가진 상태 이름(저작 순서)."""
    return [n for n, e in (merged or {}).items() if role in ((e or {}).get("roles") or ())]


def has_role(merged: dict, statuses, role: str) -> bool:
    return bool(set(statuses or ()) & set(with_role(merged, role)))


def death_timer(merged: dict, dying: str):
    """(결말 상태 이름, 분) — dying 상태가 n분 지속되면 붙는 결말 상태(apply_auto SINCE_STATUS_GTE). 없으면 (None, 0)."""
    for n, e in (merged or {}).items():
        r = (((e or {}).get("rules") or {}).get("apply_auto") or {})
        if isinstance((e or {}).get("ending"), dict) and r.get("status") == dying and isinstance(r.get("minutes"), int):
            return n, r["minutes"]
    return None, 0


def status_ending(merged: dict, statuses) -> dict | None:
    """v7.48(E2): 그 인물에게 붙은 상태 중 결말을 가진 첫 상태의 결말(저작 순서), 없으면 None."""
    have = set(statuses or ())
    for name, eff in (merged or {}).items():
        end = (eff or {}).get("ending")
        if name in have and isinstance(end, dict):
            return {**end, "status": name}
    return None


def stages_of(eff) -> list:
    st = (eff or {}).get("stages")
    return list(st) if isinstance(st, list) else []


def stage_of(clock, name: str) -> int:
    return int(((clock or {}).get("stage") or {}).get(name, 1))


def mods_at(eff, stage: int) -> dict:
    """그 단계의 판정 불이익 {분야: 값}(단계 0 = 없음)."""
    if stage < 1:
        return {}
    sm, st = (eff or {}).get("stage_mods"), stages_of(eff)
    if isinstance(sm, list) and st:
        return dict(sm[min(stage, len(sm)) - 1] or {})
    return dict((eff or {}).get("mods") or {})


def effective_stage(eff, clock, name: str, now) -> int:
    """일시 완화(ease) 중이면 한 단계 낮은 값으로 계산한다."""
    st = stage_of(clock, name) if stages_of(eff) else 1
    until = ((clock or {}).get("eased") or {}).get(name)
    return st - 1 if until is not None and now is not None and int(until) > int(now) else st


def label(merged: dict, name: str, clock, now=None) -> str:
    """표시 — '부상(중상)' · 완화 중이면 '부상(중상·완화)'."""
    eff = (merged or {}).get(name) or {}
    st = stages_of(eff)
    bits = [st[min(stage_of(clock, name), len(st)) - 1]] if st else []
    until = ((clock or {}).get("eased") or {}).get(name)
    if until is not None and now is not None and int(until) > int(now):
        bits.append("완화")
    return name + (f"({'·'.join(bits)})" if bits else "")


def mods_text(m: dict) -> str:
    return ", ".join(f"{k} {int(v):+d}" for k, v in m.items() if v) or "판정 영향 없음"


@dataclass
class Decision:
    apply: list                 # 추출 DTO 형식 [{target,status,score}] (코드 판정은 score=None)
    clear: list
    clock: dict                 # 이 PC의 새 status_clock 항목
    notes: list                 # 마스터 보고용 한 줄들


def decide(merged: dict, ctx: Ctx, pc_name: str, apply_in, clear_in, groups_consumed=(), bumped_new=None) -> Decision:
    """PC 대상 추출 후보 + 처치 + 코드 규칙 → 최종 부여·해제·단계. NPC 대상 후보는 손대지 않는다(호출부가 그대로 둔다).

    순서(v7.47): ① 처치(사용 효과 → down·treat·ease, 잠복 취소) ② 추출 단계(올림 허용, 처치 전용 상태는 내림 거부)
    ③ 규칙(gate·auto·raise_auto·배타·잠복)."""
    notes = []
    clock = copy.deepcopy(ctx.clock or {})
    applied = dict(clock.get("applied") or {})
    for s in ctx.active:                      # 시계 없이 붙어 있던 상태(이전 버전) — 지금부터 잰다
        applied.setdefault(s, ctx.now)
    clock["applied"] = applied
    stage = {k: int(v) for k, v in (clock.get("stage") or {}).items() if k in ctx.active}
    cared = {k: v for k, v in (clock.get("cared") or {}).items() if k in ctx.active}
    eased = {k: v for k, v in (clock.get("eased") or {}).items() if k in ctx.active and int(v) > ctx.now}
    for s in ctx.active:
        if stages_of(merged.get(s)):
            stage.setdefault(s, 1)
    clock["stage"], clock["cared"], clock["eased"] = stage, cared, eased
    latent = dict(clock.get("latent") or {})
    ctx = Ctx(**{**ctx.__dict__, "clock": clock})

    def rules(s):
        r = (merged.get(s) or {}).get("rules")
        return r if isinstance(r, dict) else None

    def nstages(s):
        return len(stages_of(merged.get(s)))

    def roll_latent(s, at_stage):
        lat = (merged.get(s) or {}).get("latent")
        if not (isinstance(lat, dict) and lat.get("target")):
            return
        if at_stage < int(lat.get("min_stage") or 1):
            return
        cur = latent.get(lat["target"])
        if cur and cur.get("due") is not None and int(cur["due"]) > ctx.now:
            return                                # 이미 발현 예정 — 다시 굴리지 않는다
        rec = latent_roll(ctx.seed, pc_name, s, ctx.now, lat, actor_id=ctx.actor_id)
        latent[lat["target"]] = rec
        notes.append(f"{lat['target']} 잠복 판정(코드) — " + ("발현 예정" if rec["due"] is not None else "발현 없음"))

    want_apply, want_clear, cared_now = {}, set(), set()

    def apply_care(s):
        """① 처치 — 이번 턴 PC에게 쓴 물건의 사용 효과. True면 이 처치로 해제(down의 1단계)."""
        done = False
        for eff_name, act in ((merged.get(s) or {}).get("care") or {}).items():
            if eff_name not in ctx.care:
                continue
            cared_now.add(s)
            if act == "ease":
                eased[s] = ctx.now + EASE_MINUTES
                notes.append(f"{s} 증상 완화 {EASE_MINUTES // 60}시간({eff_name}, 코드)")
                continue
            cared[s] = ctx.now
            cur = stage.get(s, 1)
            if nstages(s) and cur > 1:
                stage[s] = cur - 1
                notes.append(f"{label(merged, s, clock)} ← 한 단계 내려감({eff_name} 처치, 코드)")
            elif act == "down":
                done = True
                notes.append(f"{s} 해제({eff_name} 처치, 코드)")
                break
            else:
                notes.append(f"{s} 처치 기록({eff_name}) — 시간이 마무리한다(코드)")
        return done

    def cancel_latents():
        for tgt, rec in list(latent.items()):        # 잠복 취소 — 발현 전 그 효과로 처치
            src = (merged.get(rec.get("from")) or {}).get("latent") or {}
            if src.get("cancel_by") in ctx.care and tgt not in ctx.active and rec.get("due") is not None:
                latent.pop(tgt)
                notes.append(f"{tgt} 잠복 취소({src['cancel_by']} 처치, 코드)")

    for s in sorted(ctx.active):
        if apply_care(s):
            want_clear.add(s)

    # ② 추출 단계 — 붙어 있는 상태(이번 턴 처치한 상태는 처치 결과가 우선 — 처치 전 단계를 다시 올리지 않는다)
    for s, n in (ctx.stage_in or {}).items():
        if s not in ctx.active or s in want_clear or s in cared_now or not nstages(s):
            continue
        n, cur = max(1, min(int(n), nstages(s))), stage.get(s, 1)
        if n > cur:
            stage[s] = n
            notes.append(f"{label(merged, s, clock)} ← 단계 올라감(추출)")
            roll_latent(s, n)
        elif n < cur:
            if (merged.get(s) or {}).get("lower_by") == "care":
                notes.append(f"{s} 단계 내림 보류 — 처치·시간으로만(코드)")
            else:
                stage[s] = n
                notes.append(f"{label(merged, s, clock)} ← 단계 내려감(추출)")

    # ③ 규칙
    def gate_ok(v):
        return v == TRUE or (ctx.npc and v == UNKNOWN)

    for e in apply_in:
        r = rules(e["status"])
        if r and "apply_gate" in r and not gate_ok(evaluate(r["apply_gate"], ctx, e["status"])):
            notes.append(f"{e['status']} 부여 보류 — 근거 조건 미충족(코드)")
            continue
        want_apply[e["status"]] = e
    for e in clear_in:
        s = e["status"]
        if s in want_clear:
            continue
        if (merged.get(s) or {}).get("lower_by") == "care":
            notes.append(f"{s} 해제 보류 — 처치·시간으로만(코드)")
            continue
        r = rules(s)
        if r is not None:
            gate = r.get("clear_gate")
            if gate is None or not gate_ok(evaluate(gate, ctx, s)):
                notes.append(f"{s} 해제 보류 — 해소 근거 미충족(코드)")
                continue
        want_clear.add(s)
    for s in merged:
        r = rules(s)
        if not r:
            continue
        if s in ctx.active or s in want_apply:
            if "clear_auto" in r and evaluate(r["clear_auto"], ctx, s) == TRUE:
                want_clear.add(s)
                want_apply.pop(s, None)
                notes.append(f"{s} 해제(코드 확정)")
                continue
        if s in ctx.active and s not in want_clear and "raise_auto" in r and nstages(s) \
                and stage.get(s, 1) < nstages(s) and evaluate(r["raise_auto"], ctx, s) == TRUE:
            stage[s] = stage.get(s, 1) + 1
            notes.append(f"{label(merged, s, clock)} ← 단계 올라감(코드 확정)")
            roll_latent(s, stage[s])
        elif s in ctx.active and s not in want_clear and "lower_auto" in r \
                and evaluate(r["lower_auto"], ctx, s) == TRUE:
            if nstages(s) and stage.get(s, 1) > 1:
                stage[s] = stage.get(s, 1) - 1
                notes.append(f"{label(merged, s, clock)} ← 단계 내려감(코드 확정)")
            else:
                want_clear.add(s)
                notes.append(f"{s} 해제(코드 확정)")
        if s not in ctx.active and s not in want_apply and "apply_auto" in r \
                and evaluate(r["apply_auto"], ctx, s) == TRUE:
            want_apply[s] = {"target": pc_name, "status": s, "score": None}
            notes.append(f"{s} 부여(코드 확정)")
    for s in list(want_apply):
        if s in want_clear:
            want_apply.pop(s)
    final = (set(ctx.active) - want_clear) | set(want_apply)
    for s in list(want_apply):                # 상호 배타 — 새로 붙은 쪽이 다른 쪽을 푼다
        for o in (merged.get(s) or {}).get("excludes") or ():
            if o in final and o not in want_apply:
                want_clear.add(o)
                final.discard(o)
                notes.append(f"{o} 해제 — {s}와(과) 배타(코드)")
    cleared = dict(clock.get("cleared") or {})
    for s in want_apply:
        if s not in ctx.active:
            applied[s] = ctx.now
            latent.pop(s, None)                   # 발현한 대상의 잠복 기록은 끝
            n = nstages(s)
            st = max(1, min(int((ctx.stage_in or {}).get(s) or 1), n)) if n else 1
            if n:
                stage[s] = st
            roll_latent(s, st)
    for s, n in sorted((bumped_new or {}).items()):   # T-7(G9): 코드가 새로 붙이는 상태(전투 부상·비용)도 같은 결정 안에서
        if s not in merged or s in ctx.active or s in want_apply:   # — 규칙·게이트 없이, 아래 같은 턴 처치가 본다
            continue
        want_apply[s] = {"target": pc_name, "status": s, "score": 100}
        applied[s] = ctx.now
        if nstages(s):
            stage[s] = max(1, min(nstages(s), int(n)))
        notes.append(f"{s} (코드)")
    for s in [x for x in want_apply if x not in ctx.active]:   # 같은 턴에 다치고 바로 처치한 경우도 처치한다
        if apply_care(s):
            want_apply.pop(s)
            for k in (applied, stage, cared, eased):
                k.pop(s, None)
    cancel_latents()                                           # 같은 턴에 걸린 잠복도 같은 턴 처치로 취소
    for s in want_clear:
        applied.pop(s, None)
        stage.pop(s, None)
        cared.pop(s, None)
        eased.pop(s, None)
        if s in ctx.active:
            cleared[s] = ctx.now
    clock["cleared"] = cleared
    for k, v in (("latent", latent), ("stage", stage), ("cared", cared), ("eased", eased)):
        if v:
            clock[k] = v
        else:
            clock.pop(k, None)
    intake = dict(clock.get("intake") or {})
    for g in groups_consumed:
        intake[g] = ctx.now
    clock["intake"] = intake
    apply_out = [e for s, e in want_apply.items() if s not in ctx.active]
    clear_out = [{"target": pc_name, "status": s, "score": None} for s in sorted(want_clear) if s in ctx.active]
    return Decision(apply_out, clear_out, clock, notes)


def latent_roll(seed: str, pc_name: str, status: str, at: int, lat: dict, *, actor_id: str = "") -> dict:
    """잠복 판정 — (세션 시드, 인물, 원인 상태, 시각)의 결정적 함수(재시도·재시작·재생성에도 같은 결과).
    T-7(G12): 인물 근거는 행위자 id(개명·표시 이름과 무관) — id를 모를 때만 옛 이름 근거(v1)."""
    import hashlib
    key = f"status.latent.v2|{seed}|{actor_id}" if actor_id else f"status.latent.v1|{seed}|{pc_name}"
    h = hashlib.sha256(f"{key}|{status}|{at}".encode("utf-8")).digest()
    roll, span = int.from_bytes(h[:4], "big") % 100, int.from_bytes(h[4:8], "big")
    lo, hi = int(lat["onset_minutes"][0]), int(lat["onset_minutes"][1])
    hit = roll < int(lat.get("chance") or 0)
    return {"from": status, "at": int(at), "due": int(at) + lo + span % (hi - lo + 1) if hit else None}


def deep_statuses(merged: dict) -> set:
    return {n for n, e in (merged or {}).items() if (e or {}).get("deep") is True}


def pre_bump(merged, ctx, bumps) -> None:
    """v7.50: 이미 붙은 상태의 코드 단계 상승을 판정 전에 반영(같은 턴 규칙 — 예: 중상 → 빈사 — 이 본다)."""
    for st, n in sorted((bumps or {}).items()):
        k = len(stages_of(merged.get(st)))
        if st in ctx.active and k:
            stage = ctx.clock.setdefault("stage", {})
            stage[st] = min(k, stage_of(ctx.clock, st) + int(n))


def post_bump(merged, ctx, d, target, bumps, plan) -> None:
    """v7.50: 붙어 있지 않던 상태의 코드 상승 = 그 단계로 새로 붙인다(규칙·게이트 없이 — 판정 결과가 근거)."""
    for st, n in sorted((bumps or {}).items()):
        if st not in merged or st in ctx.active or any(e.get("status") == st for e in d.apply):
            if st in ctx.active:
                d.notes.append(f"{st} {stage_of(d.clock, st)}단계(코드)")
            continue
        d.apply.append({"target": target, "status": st, "score": 100})   # v7.53.1: d.apply — 호출자가 plan에 합친다
        d.clock.setdefault("applied", {})[st] = ctx.now
        k = len(stages_of(merged.get(st)))
        if k:
            d.clock.setdefault("stage", {})[st] = max(1, min(k, int(n)))
        d.notes.append(f"{st} (코드)")


WOUND_STATUS = "부상"


def _new_bumps(ctx, bumps) -> dict:
    """코드 상승 중 붙어 있지 않던 상태 — 결정(decide) 안에서 새로 붙인다(T-7 G9)."""
    return {st: n for st, n in (bumps or {}).items() if st not in ctx.active}


def _old_bumps(ctx, bumps) -> dict:
    """코드 상승 중 이미 붙어 있던 상태 — pre_bump가 올렸고 post_bump는 보고 줄만 남긴다."""
    return {st: n for st, n in (bumps or {}).items() if st in ctx.active}


def stage_in(status_scores, name) -> dict:
    """추출 status_scores의 그 인물 단계(stage ≥ 1만 — 0·없음은 단계 판단 없음)."""
    out = {}
    for e in status_scores or ():
        if isinstance(e, dict) and e.get("target") == name and isinstance(e.get("stage"), int) \
                and not isinstance(e.get("stage"), bool) and e["stage"] >= 1:
            out[e.get("status")] = e["stage"]
    return out


def place_chain(places: dict, place) -> tuple:
    """장소와 그 상위(계층) — places = {PlaceId: {"parent"…}}."""
    if not place:
        return ()
    out, x = [], place
    while x and x not in out:
        out.append(x)
        x = ((places.get(x) or {}) if isinstance(places, dict) else {}).get("parent")
    return tuple(out)


@dataclass
class TurnDecision:
    apply: list                 # 최종 부여 후보 [{target, status, score}]
    clear: list                 # 최종 해제 후보
    clocks: dict                # 새 상태 시계 저장 {이름: 시계}
    notes: list                 # 마스터 보고 줄


def decide_turn(merged: dict, *, pc_name: str, statuses: dict, clocks: dict, facts: dict, apply_in: list,
                clear_in: list, consumed=frozenset(), used=frozenset(), status_scores=(), places=None,
                place_groups=None, seed: str = "") -> TurnDecision:
    """한 턴의 상태 결정(PC + 이번 턴 후보·상태가 있는 NPC) — 옛 status_rules.resolve_turn의 계산 부분.

    statuses·clocks = 턴 전 {이름: 상태 목록}·{이름: 시계}. facts = turn_world.status_turn_facts 형식.
    키는 이름이다. 잠복 판정의 결정적 근거는 facts['actor_ids']의 행위자 id다(T-7 G12 — 없으면 옛 이름 근거)."""
    apply_in, clear_in = list(apply_in), list(clear_in)
    now, before, place = facts["now"], facts["before"], facts["place"]
    consumed = frozenset(consumed) | facts.get("care_consumed", frozenset())
    used = frozenset(used) | facts.get("care_used", frozenset())
    store = clocks or {}
    bumps = {}
    for st, n in (facts.get("spend") or {}).items():
        bumps.setdefault(pc_name, {})[st] = bumps.get(pc_name, {}).get(st, 0) + int(n)
    for who, sp in (facts.get("npc_spend") or {}).items():
        for st, n in sp.items():
            bumps.setdefault(who, {})[st] = bumps.get(who, {}).get(st, 0) + int(n)
    for who, n in (facts.get("hits") or {}).items():
        bumps.setdefault(who, {})[WOUND_STATUS] = bumps.get(who, {}).get(WOUND_STATUS, 0) + int(n)
    if bumps:
        hit = lambda e: WOUND_STATUS in (bumps.get(e.get("target")) or {}) and e.get("status") == WOUND_STATUS  # noqa
        apply_in = [e for e in apply_in if not hit(e)]
        clear_in = [e for e in clear_in if not hit(e)]
    ctx = Ctx(now=now, start_minute=facts["start"], turn_minutes=max(0, now - before),
              place=place, place_chain=place_chain(places or {}, place), place_groups=place_groups or {},
              daypart=facts["daypart"], activity=facts["activity"],
              consumed=consumed, used=used, active=frozenset((statuses or {}).get(pc_name) or ()),
              clock=copy.deepcopy(store.get(pc_name) or {}), seed=seed, worn=facts.get("worn") or frozenset(),
              care=facts.get("care") or frozenset(), stage_in=stage_in(status_scores, pc_name),
              moved=facts.get("moved"), noise=facts.get("noise"),
              actor_id=(facts.get("actor_ids") or {}).get(pc_name) or "")
    pre_bump(merged, ctx, bumps.get(pc_name))
    mine = lambda e: e.get("target") == pc_name  # noqa: E731
    d = decide(merged, ctx, pc_name, [e for e in apply_in if mine(e)], [e for e in clear_in if mine(e)], consumed,
               bumped_new=_new_bumps(ctx, bumps.get(pc_name)))
    post_bump(merged, ctx, d, pc_name, _old_bumps(ctx, bumps.get(pc_name)), None)
    apply_out = [e for e in apply_in if not mine(e)] + d.apply
    clear_out = [e for e in clear_in if not mine(e)] + d.clear
    new_store = {**store, pc_name: d.clock}
    scored = {e.get("status") for e in status_scores or () if isinstance(e, dict) and e.get("target") == pc_name}
    for s in sorted(ctx.active - scored - deep_statuses(merged)):     # 붙어 있는데 판단이 빠진 표면 상태 — 유지
        d.notes.append(f"{s} 판단 누락(유지)")
    notes = list(d.notes)
    forced = {n: m for n, m in (facts.get("event_statuses") or {}).items() if n != pc_name}
    touched = {e.get("target") for e in apply_out + clear_out} \
        | {n for n, st in (statuses or {}).items() if st} | set(forced) | set(bumps) \
        | set(facts.get("event_clears") or {})
    for npc in sorted(t for t in touched if t and t != pc_name):
        own = lambda e, _n=npc: e.get("target") == _n  # noqa: E731
        nctx = Ctx(now=now, start_minute=facts["start"], turn_minutes=max(0, now - before), daypart=facts["daypart"],
                   activity=facts["activity"] if npc in (facts.get("party_present") or ()) else None,
                   active=frozenset((statuses or {}).get(npc) or ()), clock=copy.deepcopy(store.get(npc) or {}),
                   seed=seed, care=(facts.get("care_by") or {}).get(npc) or frozenset(),
                   stage_in=stage_in(status_scores, npc), npc=True,
                   actor_id=(facts.get("actor_ids") or {}).get(npc) or "")
        pre_bump(merged, nctx, bumps.get(npc))
        dn = decide(merged, nctx, npc, [e for e in apply_out if own(e)], [e for e in clear_out if own(e)], (),
                    bumped_new=_new_bumps(nctx, bumps.get(npc)))
        post_bump(merged, nctx, dn, npc, _old_bumps(nctx, bumps.get(npc)), None)
        apply_out = [e for e in apply_out if not own(e)] + dn.apply
        clear_out = [e for e in clear_out if not own(e)] + dn.clear
        for s in (facts.get("event_clears") or {}).get(npc) or ():      # 사건이 푼 상태 — 그대로 해제
            if not any(e.get("status") == s for e in dn.clear):
                clear_out.append({"target": npc, "status": s})
                for k in ("applied", "stage", "cared", "eased"):
                    (dn.clock.get(k) or {}).pop(s, None)
                dn.notes.append(f"{s} 해제(사건)")
        for s, at in sorted((forced.get(npc) or {}).items()):           # 사건이 붙인 상태 — 규칙·게이트 없이 그대로
            if s in merged and not any(e.get("status") == s for e in dn.apply):
                apply_out.append({"target": npc, "status": s, "score": 100})
                dn.clock.setdefault("applied", {})[s] = int(at if at is not None else now)
                dn.notes.append(f"{s} (사건)")
        new_store[npc] = dn.clock
        notes += [f"{npc}: {n}" for n in dn.notes]
    return TurnDecision(apply_out, clear_out, new_store, notes)


def apply_lists(statuses: dict, apply: list, clear: list) -> dict:
    """부여 → 해제 순으로 상태 목록에 반영한 새 사전(옛 extraction.apply_normalized_status_item과 같은 의미 —
    대상 키는 목록이 비어도 남는다)."""
    out = {k: list(v) for k, v in (statuses or {}).items()}
    for e in apply:
        cur = out.setdefault(e["target"], [])
        if e["status"] not in cur:
            cur.append(e["status"])
    for e in clear:
        cur = out.setdefault(e["target"], [])
        if e["status"] in cur:
            cur.remove(e["status"])
    return out
