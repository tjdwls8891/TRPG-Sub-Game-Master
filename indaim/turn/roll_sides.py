"""판정 당사자 조립(목표치 A·S·E·M·H·D)·NPC 판정 능력치·파티 굴림(cogs/gm.py에서 옮김)."""
from __future__ import annotations

from indaim.app import utils as _utils
from indaim.rules.checks import judgment as _judgment
from indaim.rules.items import items as _items
from indaim.rules.party import party as _party
from indaim.rules.vitals import status_rules as _status_rules
from indaim.turn import turn_world as _turn_world


def _npc_key(session, name: str, world_turn=None):
    """판정 대상 NPC의 정본 이름(DYNAMIC은 현재 장면 인물의 표시명 → 레거시 이름, 겹치거나 장면 밖이면 None)."""
    if world_turn is None:
        return name
    try:
        _scene, projs = _turn_world.scene_npc_projections(world_turn.ctx, None, session)
    except Exception:
        projs = []
    hits = [p for p in projs if p.get("display_name") == name]
    return hits[0].get("legacy_name") if len(hits) == 1 else None


def _npc_stats(session, name: str, world_turn=None) -> dict | None:
    """판정 대상 NPC의 정본 능력치(session.npcs[*].stats). 확인할 수 없으면 None — 허구 수치 없음.

    DYNAMIC은 현재 장면 인물의 표시명으로만 찾는다(표시명이 겹치거나 장면 밖이면 확인 불가).
    """
    key = name
    if world_turn is not None:
        try:
            _scene, projs = _turn_world.scene_npc_projections(world_turn.ctx, None, session)
        except Exception:
            projs = []
        hits = [p for p in projs if p.get("display_name") == name]
        if len(hits) != 1:
            return None
        key = hits[0].get("legacy_name")
    data = (getattr(session, "npcs", None) or {}).get(key) if key else None
    stats = data.get("stats") if isinstance(data, dict) else None
    return stats if isinstance(stats, dict) and stats else None


def _judge_side(view, side: dict, name: str, world_turn=None, contest: bool = False) -> dict:
    """한 판정 당사자의 목표치 조립. A는 코드가 정본 원스탯에서 환산, S는 지목 스킬이 PC 프로필에 있을 때만(v7.43),
    M·H·D는 판단층위 계약값(검증 통과분). 능력치를 확인할 수 없으면 target=None(굴리지 않음).

    v7.47(동적 세계, 굴림 분야 domain이 있을 때): E = 지목 물건의 그 분야 효과 + 입은 것의 그 분야 불리 효과(자동, W5·W6),
    PC의 H = 상태·단계 저작 불이익의 코드 합산(S2 — 판단층위 status_mod는 쓰지 않는다)."""
    stat = str(side.get("stat") or "")
    uid = _utils.get_uid_by_char_name(view, name)
    sd = getattr(view, "scenario_data", None)
    domain = side.get("domain") if world_turn is not None and side.get("domain") in _roll_domains() else None
    skill, s, item, e, spend = "", 0, "", 0, {}
    h = int(side.get("status_mod") or 0)
    status_basis = str(side.get("status_basis") or "")
    if uid:
        _prof = (view.players.get(uid) or {}).get("profile") or {}
        raw = _prof.get(stat)
        # v7.50(E5 · D-B3): 상시 기술(분야 자동) + 지목한 발동 기술(비용 상태가 고갈이면 0)
        _clk = (getattr(view, "status_clock", None) or {}).get(name) or {}
        _merged = _utils.get_merged_status_effects(sd or {})

        def _exhausted(st, _c=_clk, _m=_merged, _have=(getattr(view, "statuses", None) or {}).get(name) or ()):
            n = len(_status_rules.stages_of(_m.get(st)))
            return bool(n) and st in _have and _status_rules.stage_of(_c, st) >= n
        s, skill, spend = _judgment.skill_terms(_prof, sd, side.get("skill"), domain, _exhausted)
        _defs = _items.definitions_of(world_turn.ctx.definition) if world_turn is not None else {}
        if world_turn is not None and side.get("item") not in (None, "", "NONE"):   # v7.46 장비 보정 E
            row = next((r for r in _items.equip_rows(view, name, _defs) if r[0] == side.get("item")), None)
            if row is not None and domain is not None and _items.worn_on_body(view, name, row[2]):
                row = None                     # D2 나: 몸에 입은 것은 아래 자동 효과에 이미 들어간다(이중 반영 없음)
            if row is not None:
                e = _items.equip_mod([t for t in row[3] if domain is None
                                          or int((_items.effect_vocab()["tags"].get(t) or {}).get("mod") or 0) > 0],
                                         stat, sd, domain)
                item = row[2] if e else ""
        if domain is not None:
            auto, auto_names = _items.worn_auto(view, name, domain, _defs)
            if auto_names:
                e += auto
                item = ", ".join(([item] if item else []) + auto_names)
            _clock = (getattr(view, "status_clock", None) or {}).get(name) or {}
            h, status_basis = _status_rules.status_mod(
                _utils.get_merged_status_effects(sd or {}), (view.statuses or {}).get(name) or (),
                _clock, (getattr(view, "world_clock", None) or {}).get("minute_index"), domain)
            status_basis = (status_basis + " (코드 확정)") if status_basis else ""
    else:
        raw = (_npc_stats(view, name, world_turn) or {}).get(stat)
        if world_turn is not None and contest:  # v7.46(B3): 대결 상대 NPC — 손에 드는 무기·보호구 중 가장 유리한 것 하나
            item, e = _items.npc_equip_mod(view, name, stat, _items.definitions_of(world_turn.ctx.definition),
                                               domain)
        _nk = _npc_key(view, name, world_turn) if domain is not None else None
        if _nk:                                 # v7.48(D-3 나): NPC 상태·단계 불이익도 코드 합산(분야가 있을 때)
            h, status_basis = _status_rules.status_mod(
                _utils.get_merged_status_effects(sd or {}), (view.statuses or {}).get(_nk) or (),
                (getattr(view, "status_clock", None) or {}).get(_nk) or {},
                (getattr(view, "world_clock", None) or {}).get("minute_index"), domain)
            status_basis = (status_basis + " (코드 확정)") if status_basis else ""
    a = _judgment.ability_mod(raw)
    diff = side.get("difficulty") if side.get("difficulty") in _judgment.DIFFICULTY else "보통"
    m = int(side.get("method_mod") or 0)
    d = _judgment.DIFFICULTY[diff]
    out = {"name": name, "stat": stat, "is_pc": bool(uid), "uid": uid, "difficulty": diff,
           "spend": spend if uid else {},
           "A": a, "S": s, "skill": skill, "E": e, "item": item, "M": m, "H": h, "D": d, "domain": domain or "",
           "method_basis": str(side.get("method_basis") or ""),
           "status_basis": status_basis, "T": None}
    if a is None:
        return out
    return _side_total(out)


def _side_total(out: dict) -> dict:
    """목표치 T와 내역 — 능력·숙련·장비·행동·상태·난이도 + v7.51 파티 보조(P, 행동 보정과 같은 자리)."""
    a, s, e, m, h, d, p = (out.get(k) or 0 for k in ("A", "S", "E", "M", "H", "D", "P"))
    out["T"] = _judgment.target(a, s, m + p, h, d, e)
    terms = [(a, "능력"), (s, f"숙련 {out.get('skill')}"), (e, f"장비 {out.get('item')}"), (m, "행동"),
             (p, f"{_party.ASSIST_LABEL} {out.get('assist_by_display') or ''}".strip()), (h, "상태"),
             (d, f"난이도 {out.get('difficulty')}")]
    _terms = "".join(f"{v:+d}({n})" for v, n in terms if v)
    out["breakdown"] = f"{_judgment.BASE}{_terms}={out['T']}" if _terms else f"{out['T']}"
    return out


def _roll_domains() -> list:
    """v7.47(W6) 굴림 분야 — 공용 효과 어휘 domains + 기타."""
    return list(_items.effect_vocab().get("domains") or {}) + ["기타"]


def _judge_plan(view, spec: dict, world_turn=None) -> dict:
    """rolls 항목 → {kind, actor, opponent}. 계약 검증은 판단층위 수신 시 끝났다(여기서 값을 보정하지 않는다)."""
    kind = spec.get("kind") if spec.get("kind") in _judgment.KINDS else "GENERAL"
    actor = _judge_side(view, spec, str(spec.get("char_name") or view.gm_target_char or "?"), world_turn)
    opp = None
    if kind == "CONTEST" and isinstance(spec.get("opponent"), dict):
        o = spec["opponent"]
        opp = _judge_side(view, o, str(o.get("name") or "?"), world_turn, contest=True)
        _party_assist(view, world_turn, actor, opp)
    return {"kind": kind, "actor": actor, "opponent": opp}


def _pc_skills(session, p_data) -> list:
    """v7.46: PC 프로필 스킬 이름 목록(쉼표 구분 문자열 또는 목록)."""
    raw = ((p_data or {}).get("profile") or {}).get(_judgment.SKILL_FIELD)
    vals = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [x for x in dict.fromkeys(str(v).strip() for v in vals) if x]


def _party_scene(view, world_turn):
    """(파티원 목록, 장면의 적 표시명 집합) — 기능 party가 없거나 동적 세계 턴이 아니면 ([], set())."""
    if world_turn is None or not _party.config(world_turn.ctx.definition):
        return [], set()
    try:
        members = _turn_world.party_members(world_turn.ctx, view)
        _scene, projs = _turn_world.scene_npc_projections(world_turn.ctx, None, view)
    except Exception:  # noqa: BLE001 — 파티 재료를 못 읽으면 파티 행동 없음
        return [], set()
    return members, {p["display_name"] for p in projs if _party.is_hostile(world_turn.ctx.definition, p["actor_id"])}


def _attack_domains(view) -> tuple:
    return tuple((getattr(view, "scenario_data", None) or {}).get("attack_domains") or ("근접", "원거리"))


def _party_assist(view, world_turn, actor, opp) -> None:
    """v7.51 보조 역할: PC가 적과 공격 분야로 맞서는 대결 굴림에 bonus를 더한다(행동 가능한 보조 파티원 첫째)."""
    if actor.get("T") is None or not actor.get("is_pc") or opp is None or actor.get("domain") not in _attack_domains(view):
        return
    members, foes = _party_scene(view, world_turn)
    if opp.get("name") not in foes:
        return
    m = next((m for m in members if m["act"] == _party.ASSIST and _turn_world.party_can_act(view, m)), None)
    if m is None:
        return
    actor["P"] = int(m["def"].get("bonus") or _party.DEFAULT_ASSIST)
    actor["assist_by"], actor["assist_by_display"] = m["name"], m["display"]
    _side_total(actor)


def _party_round(view, world_turn, records, roll_side, game_line) -> tuple:
    """v7.51(D-P1 가) 전투 턴의 파티 행동 — (기록, 게임 줄, 로직 줄).

    전투 턴 = 이번 턴 대결에 장면의 적(stance hostile)이 있다. 막기 = 파티 기록 guard(부상 대신 받기 — combat_hits),
    공격 = 적 하나와 코드 대결(역할 능력치·분야, 상시 자동 + 마력이 남은 가장 큰 발동 기술), 보조 = PC 굴림에 이미 더한
    보정의 비용 기록. 플레이어가 명령한 파티원(이번 턴 굴림의 행위자)은 자동 행동이 없다. 치유는 커밋(매 턴)."""
    members, foes = _party_scene(view, world_turn)
    if not members or not foes:
        return [], [], []
    names = lambda r: {(r.get("actor") or {}).get("name"), (r.get("opponent") or {}).get("name")}  # noqa: E731
    fights = [r for r in records if r.get("kind") == "CONTEST" and names(r) & foes]
    if not fights:
        return [], [], []
    D = world_turn.ctx.definition
    sd = getattr(view, "scenario_data", None) or {}
    commanded = {(r.get("actor") or {}).get("name") for r in records}
    engaged = next((n for r in fights for n in names(r) if n in foes), None)
    party = {"kind": "PARTY", "guard": None, "assist": None, "spend": {}}
    out, game, logic = [], [], []

    def spend(m, sp):
        row = party["spend"].setdefault(m["name"], {})
        for st, n in sp.items():
            row[st] = row.get(st, 0) + int(n)
    for r in records:                                  # 보조 — PC 굴림에 이미 더한 보정의 비용(턴 1회)
        a = r.get("actor") or {}
        if a.get("assist_by") and not party["assist"]:
            m = next((m for m in members if m["name"] == a["assist_by"]), None)
            if m is not None:
                party["assist"] = m["name"]
                spend(m, _party.cost_of(m["def"]))
                logic.append(f"- [파티] {m['display']}({m['role']}) 보조 — PC 공격 +{a.get('P')}")
    for m in members:
        if m["display"] in commanded or not _turn_world.party_can_act(view, m):
            continue
        if m["act"] == _party.GUARD and party["guard"] is None:
            party["guard"] = m["name"]
            spend(m, _party.cost_of(m["def"]))
            logic.append(f"- [파티] {m['display']}({m['role']}) 막기 — 적이 PC를 이긴 공격 하나를 대신 받는다(코드)")
        elif m["act"] == _party.STRIKE:
            dom = (m["def"].get("domains") or [_attack_domains(view)[0]])[0]
            stats = _npc_stats(view, m["display"], world_turn) or {}
            stat = m["def"].get("stat") if m["def"].get("stat") in stats else (max(stats, key=stats.get) if stats else "")
            side = _judge_side(view, {"stat": stat, "domain": dom, "difficulty": "보통"}, m["display"], world_turn)
            foe = engaged or sorted(foes)[0]
            fstats = _npc_stats(view, foe, world_turn) or {}
            fstat = stat if stat in fstats else (max(fstats, key=fstats.get) if fstats else "")
            opp = _judge_side(view, {"stat": fstat, "domain": _attack_domains(view)[0], "difficulty": "보통"}, foe,
                              world_turn, contest=True)
            if side["T"] is None or opp["T"] is None:
                continue
            prof = (((D.get("actor_definitions") or {}).get(m["aid"]) or {}).get("profile_base") or {})
            exh = lambda st, _n=m["name"]: _turn_world.party_exhausted(view, _n, st)  # noqa: E731
            side["S"], side["skill"], side["spend"] = _judgment.skill_terms(
                prof, sd, _party.best_invoked(prof, sd, dom, exh), dom, exh)
            _side_total(side)
            side["spend"] = {**side["spend"], **{k: side["spend"].get(k, 0) + v
                                                 for k, v in _party.cost_of(m["def"]).items()}}
            roll_side(side)
            roll_side(opp)
            side["contest"] = opp["contest"] = _judgment.contest_outcome(side["margin"], opp["margin"])
            win = {"A": side["name"], "B": opp["name"]}.get(side["contest"])
            out.append({"kind": "CONTEST", "actor": side, "opponent": opp, "party": True})
            game += [game_line(side), game_line(opp),
                     f"> ⚔️ [파티] {side['name']} 대 {opp['name']}: **{(win + ' 우세') if win else '우열 없음'}**"]
            logic.append(f"- [파티] {side['name']}({m['role']}) {stat} 공격 대 {opp['name']}: R={side['R']}/T={side['breakdown']} "
                         f"대 R={opp['R']}/T={opp['T']} → {(win + ' 우세') if win else '우열 없음'}(부상은 코드)")
    recs = out + ([party] if party["guard"] or party["assist"] or party["spend"] else [])
    return recs, game, logic
