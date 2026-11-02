"""커널 커밋(리팩터링 T-6) — 동적 세계 턴 하나의 정본 결과를 커널이 정한다.

턴 전 세계(옛 필드 → 커널 변환) 위에 한 묶음을 투영한다:
  · 계획 — 플레이어 턴 세계 연산(typed op)의 효과 번역
  · 확정 — 기능 모듈 확정 훅(items → vitals → party → growth → events). 재료는 settle_capture가 받은 해석 결과
결과 세계를 역변환한 값이 그 커밋 뒤 세션의 세계 필드다(필드 = 커널 세계의 작업 사본, 저장은 v5 커널 형식).
투영 충돌·훅 거부는 KernelCommitError — 커밋 owner까지 전파되어 영속 전 실패(롤백)가 된다.
"""
from __future__ import annotations

import copy
import hashlib

from indaim.kernel import EffectBatch, KernelError, View, project
from indaim.migrate import ops_v4, v4
from indaim.rules import growth, items, party, vitals
from indaim.rules.events import components as events

FIELDS = v4.FIELDS
SYNTH_PREFIX = "x."          # 등록부에 없는 소유자 이름의 임시 행위자(투영 동안만 — 결과에서 뺀다)


class KernelCommitError(RuntimeError):
    """커널 커밋 실패(번역·투영 충돌·훅 거부) — 이 시도는 영속 전에 실패한다."""


def capture(session) -> dict:
    return {f: copy.deepcopy(getattr(session, f, None)) for f in FIELDS}


def _fields(d: dict) -> dict:
    out = {f: [] if f in v4.NAME_LISTS or f == "world_pending_signals" else {} for f in FIELDS}
    out["world_version"] = None
    out.update({k: v for k, v in d.items() if k in out and v is not None})
    return out


def _strings(obj, out: set, depth=0) -> None:
    """확정 재료가 가리킬 수 있는 이름 후보(짧은 문자열 키·값)."""
    if depth > 6:
        return
    if isinstance(obj, str):
        if 0 < len(obj) <= 40:
            out.add(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _strings(k, out, depth + 1)
            _strings(v, out, depth + 1)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            _strings(v, out, depth + 1)


def referenced_names(inputs: dict) -> set:
    """이번 커밋 확정 재료에 나오는 이름 후보 — 판정 사실·적용/해제 후보·동행·물건 계획·지급(정의·상태 정의 제외)."""
    out = set()
    vit = inputs.get("vitals") or {}
    for k in ("facts", "apply_in", "clear_in", "pc_name"):
        _strings(vit.get(k), out)
    _strings(inputs.get("party"), out)
    it = inputs.get("items") or {}
    _strings(it.get("grants"), out)
    _strings(it.get("pc_name"), out)
    plan = it.get("plan") or {}
    _strings(plan.get("ops"), out)
    _strings(plan.get("pc_name"), out)
    return out


def _with_synthetic_actors(fields: dict, extra_names=()) -> tuple:
    """등록부로 풀리지 않는 소유자 이름(세션 인물·시험 인물)에 결정적 임시 행위자 — 훅이 이름 키 정본을 다루게.
    턴 전 소유자 + 이번 확정 재료가 가리키는 이름(이번 턴에 처음 상태·물건이 붙는 미등록 인물) — 결과에서 뺀다."""
    to_aid, _c, _a = v4._names(fields)
    owners = set(extra_names)
    for f in ("resources", "equipment", "statuses", "status_clock"):
        owners |= set(fields.get(f) or {})
    owners |= {r.get("owner") for r in (fields.get("item_instances") or {}).values()
               if isinstance(r, dict) and r.get("owner")}
    for f in v4.NAME_LISTS:
        owners |= set(fields.get(f) or ())
    extra = {SYNTH_PREFIX + hashlib.sha256(n.encode("utf-8")).hexdigest()[:16]:
             {"kind": "NPC", "display_name": n, "source_kind": "SESSION_NPC", "source_key": n, "aliases": []}
             for n in sorted(owners) if isinstance(n, str) and n and n not in to_aid}
    out = dict(fields)
    out["actor_registry"] = {**(fields.get("actor_registry") or {}), **extra}
    return out, set(extra)


def inputs_of(rec: dict, prep, session) -> dict:
    """수집한 재료(한 커밋 = 훅마다 1건) → 훅 입력. 같은 훅 재료가 둘이면 결정할 수 없다(실패)."""
    many = sorted(k for k, v in rec.items() if len(v) > 1)
    if many:
        raise KernelCommitError(f"한 커밋에 같은 확정 재료가 여러 번: {many}")
    one = {k: v[0] for k, v in rec.items()}
    out = {"vitals": one.get("vitals"), "party": one.get("party"),
           "items": ({**(one.get("grants") or {}), "plan": one.get("plan")}
                     if ("grants" in one or "plan" in one) else None)}
    if one.get("growth_staged") or one.get("growth_turn"):
        out["growth"] = {"sd": session.scenario_data or {}, "staged": one.get("growth_staged"),
                         "turn": one.get("growth_turn")}
    from indaim.turn import turn_world as _tw
    res = getattr(getattr(prep, "world_turn", None), "result", None)
    out["events"] = {"pending": [dict(r) for r in res.pending]
                     if res is not None and res.status == _tw.TW_READY else None}
    return out


def project_turn(pre_fields: dict, inputs: dict, ops, *, tx: str, game_keys):
    """(결과 세계, 변환 프로필, 임시 행위자 id) — 한 묶음 투영."""
    reg = ops_v4.registry()
    pre_aug, synth = _with_synthetic_actors(_fields(pre_fields), referenced_names(inputs))
    pre = v4.convert(pre_aug, game_keys=game_keys)
    names = v4._names(pre_aug)[0]
    view = View(pre.world, reg, None)
    effs = ops_v4.translate(ops or ())
    from indaim.turn import settle_capture as _sc
    with _sc.paused():
        if inputs.get("vitals") is not None:
            effs += vitals.settle(view, {**inputs["vitals"], "names": names}, effect_prefix=f"{tx}.v")
        if inputs.get("items") is not None:
            effs += items.settle(view, {**inputs["items"], "names": names}, effect_prefix=f"{tx}.i")
        if inputs.get("party") is not None:
            effs += party.settle(view, {**inputs["party"], "names": names}, effect_prefix=f"{tx}.p")
        if inputs.get("events") is not None:
            effs += events.settle(view, inputs["events"], effect_prefix=f"{tx}.e")
        g = inputs.get("growth")
        if g:
            pc_uid = next(iter(pre_aug.get("players") or {}), None)
            pc_actor = next((a for a, r in (pre_aug.get("actor_registry") or {}).items()
                             if r.get("kind") == "PC" and str(r.get("source_key")) == str(pc_uid)), None)
            if pc_actor is not None:
                effs += growth.settle(view, {**g, "names": names, "pc_uid": pc_uid, "pc_actor": pc_actor},
                                      effect_prefix=f"{tx}.g")
    b = EffectBatch(batch_id=f"turn.{tx}", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    try:
        b.extend(effs)
        b.freeze()
        res = project(pre.world, b, reg, None)
    except KernelError as e:
        raise KernelCommitError(f"번역·투영 오류: {e}") from e
    if not res.ok:
        raise KernelCommitError(f"커널 투영 충돌: {[c.message for c in res.conflicts]}")
    return res.world, pre.profile, synth


def commit_fields(pre_fields: dict, inputs: dict, ops, *, tx: str, game_keys) -> dict:
    """커밋 뒤 세계 필드(역변환) — 임시 행위자는 등록부에서 뺀다(그들의 값은 원래 이름 키로 돌아간다)."""
    world, profile, synth = project_turn(pre_fields, inputs, ops, tx=tx, game_keys=game_keys)
    out = v4.apply_key_orders(v4.revert(v4.Converted(world=world, profile=profile)), v4.key_orders(pre_fields))
    reg = dict(out.get("actor_registry") or {})
    for aid in synth:
        reg.pop(aid, None)
    out["actor_registry"] = reg
    return out


def materialize(session, fields: dict) -> None:
    """세션의 세계 필드를 커널 결과로 교체(world_version은 커밋 계획의 대상 버전이 정한다)."""
    for f in FIELDS:
        if f == "world_version":
            continue
        setattr(session, f, copy.deepcopy(fields.get(f)))
