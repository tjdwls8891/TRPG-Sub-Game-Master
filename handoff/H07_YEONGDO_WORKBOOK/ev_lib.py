# -*- coding: utf-8 -*-
"""사건 저작 헬퍼 — 타입 조건식·step 생성(저작 편의, 결과는 일반 JSON)."""

PC = "$session_pc"

NPC_IDS = {
    "강철수": "yd.npc.kang_cheolsu", "차봉순": "yd.npc.cha_bongsun", "노상진": "yd.npc.no_sangjin",
    "이준혁": "yd.npc.lee_junhyeok", "류가은": "yd.npc.ryu_gaeun", "박창수": "yd.npc.park_changsu",
    "오기태": "yd.npc.oh_gitae", "서태양": "yd.npc.seo_taeyang", "임재환": "yd.npc.lim_jaehwan",
    "황순애": "yd.npc.hwang_sunae", "최민준": "yd.npc.choi_minjun", "법운": "yd.npc.beopun",
    "조원달": "yd.npc.jo_wondal", "신대호": "yd.npc.shin_daeho", "오명숙": "yd.npc.oh_myeongsuk",
    "장우진": "yd.npc.jang_ujin", "강병철": "yd.npc.kang_byeongcheol", "박하은": "yd.npc.park_haeun",
    "윤점순": "yd.npc.yoon_jeomsun", "이동철": "yd.npc.lee_dongcheol", "박인철": "yd.npc.park_incheol",
    "이현석": "yd.npc.lee_hyeonseok", "민도현": "yd.npc.min_dohyeon", "강지수": "yd.npc.kang_jisu",
    "황기영": "yd.npc.hwang_giyeong", "임철민": "yd.npc.lim_cheolmin", "손재현": "yd.npc.son_jaehyeon",
    "윤상준": "yd.npc.yoon_sangjun", "배형식": "yd.npc.bae_hyeongsik", "최수연": "yd.npc.choi_suyeon",
    "김태호": "yd.npc.kim_taeho", "문상철": "yd.npc.mun_sangcheol", "나용순": "yd.npc.na_yongsun",
    "엄주섭": "yd.npc.eom_juseop", "장철웅": "yd.npc.jang_cheolung", "조한": "yd.npc.jo_han",
    "진복례": "yd.npc.jin_bokrye", "황민석": "yd.npc.hwang_minseok", "이선희": "yd.npc.lee_seonhui",
    "장현수": "yd.npc.jang_hyeonsu", "박미래": "yd.npc.park_mirae", "권상태": "yd.npc.kwon_sangtae",
    "신창호": "yd.npc.shin_changho", "백인하": "yd.npc.baek_inha", "한도윤": "yd.npc.han_doyun",
    "전성남": "yd.npc.jeon_seongnam", "염기준": "yd.npc.yeom_gijun",
}


def N(name):
    return NPC_IDS[name]


def eq(ref, value):
    return {"cmp": "EQ", "ref": ref, "value": value}


def ne(ref, value):
    return {"cmp": "NE", "ref": ref, "value": value}


def all_(*ps):
    ps = [p for p in ps if p is not None]
    return ps[0] if len(ps) == 1 else {"all": list(ps)}


def any_(*ps):
    ps = [p for p in ps if p is not None]
    return ps[0] if len(ps) == 1 else {"any": list(ps)}


def not_(p):
    return {"not": p}


TRUE = {"const": True}
SCENE = {"ref": "SCENE_PLACE"}


def at(place):
    return eq(SCENE, place)


def at_any(places):
    return any_(*[at(p) for p in places])


def not_at_any(places):
    return not_(at_any(places))


def act(*states):
    return any_(*[eq({"ref": "OBSERVER_ACTIVITY"}, s) for s in states])


def knows(info):
    return eq({"ref": "COGNITION_AWARENESS", "holder": PC, "info": info}, "CONTENT")


def life(tpl, v):
    return eq({"ref": "EVENT_LIFECYCLE", "template": tpl}, v)


def phase(p):
    return eq({"ref": "EVENT_PHASE", "instance": "$self"}, p)


def phase_in(*ps):
    return any_(*[phase(p) for p in ps])


def elapsed(m):
    return {"time": "ELAPSED_SINCE_PHASE_GTE", "minutes": m}


def daypart(*ds):
    return any_(*[{"time": "DAYPART_IS", "daypart": d} for d in ds])


def after(date, hhmm):
    return {"time": "AT_OR_AFTER_DATETIME", "date": date, "hhmm": hhmm}


def npc_at(name_or_slot, place):
    actor = name_or_slot if name_or_slot.startswith("$") else N(name_or_slot)
    return eq({"ref": "ACTOR_PLACE", "actor": actor}, place)


def npc_with_pc(name_or_slot):
    actor = name_or_slot if name_or_slot.startswith("$") else N(name_or_slot)
    return {"cmp": "EQ", "ref": {"ref": "ACTOR_PLACE", "actor": actor}, "value_ref": SCENE}


def access(control, state):
    return eq({"ref": "ACCESS_STATE", "control": control}, state)


# ── step ──
def sig(sid, cue, *, where=None, exposes=None, observable=None):
    st = {"step_id": sid, "type": "EMIT_SIGNAL", "signal_id": sid, "cue": cue, "interrupt_policy": "IF_OBSERVABLE"}
    st["observable_when"] = observable if observable is not None else (at_any(where) if where else TRUE)
    if exposes:
        st["exposes"] = list(exposes)
    return st


def set_access(sid, control, state):
    return {"step_id": sid, "type": "SET_ACCESS_STATE", "control": control, "state": state}


def place_npc(sid, name, place):
    return {"step_id": sid, "type": "SET_ACTOR_PLACEMENT", "actor": N(name), "place": place}


def move_npc(sid, name, dest, caps=()):
    st = {"step_id": sid, "type": "START_MOVEMENT", "actor": N(name), "destination": dest, "mode": "walk"}
    if caps:
        st["capabilities"] = list(caps)
    return st


def acquire(sid, name, info, route="PARTICIPATION"):
    return {"step_id": sid, "type": "ACQUIRE_INFORMATION", "holder": f"actor:{N(name)}", "info": info,
            "route": route}


def local(sid, key, value):
    return {"step_id": sid, "type": "SET_EVENT_LOCAL", "key": key, "value": value}


def case(cid, frm, to, steps=(), valid=None):
    c = {"case_id": cid, "from": frm, "to": to, "steps": list(steps)}
    if valid is not None:
        c["valid_when"] = valid
    return c


def ph(eligible, priority, guide=None, *, retain=None, terminal=False, rank=None):
    p = {"eligible_when": eligible, "priority": priority}
    if retain is not None:
        p["retain_when"] = retain
    if terminal:
        p["terminal"] = True
    if rank is not None:
        p["rank"] = rank
    if guide:
        p["guide"] = guide
    return p


def tpl(name, spawn, phases, cases, *, slots=None, repeat="ONCE_PER_SESSION", max_occ=None, resolve=None,
        cancel=None, relevance=None, fallback=None, stability="STICKY", mode="PRIORITY", distinct=None):
    t = {"version": 1, "name": name, "spawn_when": spawn, "phases": phases, "transition_cases": cases,
         "repeat_policy": repeat}
    if slots:
        t["slots"] = slots
    if distinct:
        t["distinct"] = distinct
    if max_occ is not None:
        t["max_occurrences"] = max_occ
    if resolve is not None:
        t["resolve_when"] = resolve
    if cancel is not None:
        t["cancel_when"] = cancel
    if relevance:
        t["relevance_places"] = list(relevance)
    sel = {"mode": mode, "stability": stability}
    if fallback:
        sel["fallback_phase"] = fallback
    t["phase_selector"] = sel
    return t
