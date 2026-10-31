# -*- coding: utf-8 -*-
"""새 커널(indaim.kernel) — 효과·투영·변경분 세계·스냅샷·턴 기록·계층 계약."""
from __future__ import annotations

import ast
import itertools
import pathlib
import sys

import pytest

from indaim import kernel as K
from indaim.kernel.components import ADDITIVE, APPEND_DEDUPE, DELETE, SET_LIKE, SINGLE_VALUE

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFN = {"hp": {"npc_a": 3}}


def _ops(**extra):
    def set_(view, e):
        return [K.write(e, e.args["path"], SINGLE_VALUE, e.args["value"])]

    def add(view, e):
        return [K.write(e, e.args["path"], ADDITIVE, e.args["n"])]

    def tag(view, e):
        return [K.write(e, ("tags",), SET_LIKE, {"add": e.args.get("add", []), "remove": e.args.get("remove", [])})]

    def log(view, e):
        return [K.write(e, ("log",), APPEND_DEDUPE, e.args["v"])]

    def drop(view, e):
        return [K.write(e, e.args["path"], DELETE, None)]

    def copy_hp(view, e):                       # 부모 결과를 읽는다
        return [K.write(e, ("seen",), SINGLE_VALUE, view.read("actor", e.address[1], "vitals", ("hp",)))]

    def reject(view, e):
        raise K.KernelError("거부")

    def foreign(view, e):
        other = K.make_effect("x", "actor", "zz", "vitals", "set", cause="PLAN", by="t")
        return [K.write(other, ("hp",), SINGLE_VALUE, 1)]
    return {"set": set_, "add": add, "tag": tag, "log": log, "drop": drop, "copy_hp": copy_hp,
            "reject": reject, "foreign": foreign, **extra}


def _reg():
    r = K.Registry("test")
    r.register(K.Component("vitals", "actor", "rules.test", _ops(),
                           default=lambda d, aid: {"hp": d["hp"].get(aid, 10)}))
    r.register(K.Component("clock", K.WORLD, "kernel", _ops(), default={"minute": 0}))
    return r.freeze()


def _eff(i, op, args=None, eid="npc_a", parents=(), kind="actor", comp="vitals"):
    return K.make_effect(f"e{i}", kind, eid if kind != K.WORLD else "", comp, op, args or {},
                         cause="PLAN", by="test", parents=parents)


def _proj(effects, base=None):
    b = K.EffectBatch(batch_id="b1", origin="PLAYER_TURN", base_version_id=None, definition_revision="r")
    b.extend(effects)
    b.freeze()
    return K.project(base or K.World(), b, _reg(), DEFN)


def _val(p, eid="npc_a"):
    return K.View(p.world, _reg(), DEFN).read("actor", eid, "vitals")


# ── 옛 core와의 연속성(id·digest 규칙) ──

def test_canon_and_version_match_core():
    from core import world_ids as WI
    from core import world_state as WS
    obj = {"b": [1, 2.5, None, True], "a": {"한글": "값"}}
    assert K.canonical_json(obj) == WI.canonical_json(obj)
    assert K.content_digest(obj) == WI.content_digest(obj)
    assert K.stable_digest("ns.v1", "x", 3) == WI.stable_digest("ns.v1", "x", 3)
    v1 = K.next_version("s1", None, cause_kind="INITIALIZATION", cause_id="init")
    assert v1 == WS.next_world_version("s1", None, cause_kind="INITIALIZATION", cause_id="init")
    assert K.next_version("s1", v1, cause_kind="PLAYER_TURN", cause_id="tx") == \
        WS.next_world_version("s1", v1, cause_kind="PLAYER_TURN", cause_id="tx")


# ── 변경분 세계 ──

def test_default_read_and_compaction():
    p = _proj([_eff(1, "add", {"path": ("hp",), "n": 2})])
    assert p.ok and _val(p)["hp"] == 5                           # 정의 기본값 3 + 2
    assert _val(p, "npc_b")["hp"] == 10                          # 저장 없음 = 기본값
    back = _proj([_eff(2, "add", {"path": ("hp",), "n": -2})], base=p.world)
    assert back.ok and back.world.to_dict()["entities"] == {}    # 기본값과 같아지면 저장 지움
    assert back.world.digest() == K.World().digest()


def test_world_component_and_base_unchanged():
    base = K.World()
    p = _proj([_eff(1, "set", {"path": ("minute",), "value": 5}, kind=K.WORLD, comp="clock")], base=base)
    assert p.ok and p.world.to_dict()["world"] == {"clock": {"minute": 5}}
    assert base.to_dict() == K.World().to_dict() and p.changed


# ── 병합 의미 ──

def test_single_value_same_ok_different_conflict():
    assert _proj([_eff(1, "set", {"path": ("hp",), "value": 7}), _eff(2, "set", {"path": ("hp",), "value": 7})]).ok
    p = _proj([_eff(1, "set", {"path": ("hp",), "value": 7}), _eff(2, "set", {"path": ("hp",), "value": 8})])
    assert not p.ok and p.conflicts[0].code == "MERGE_CONFLICT" and p.world is None


def test_set_like_append_delete():
    p = _proj([_eff(1, "tag", {"add": ["b", "a"]}), _eff(2, "log", {"v": "x"}), _eff(3, "log", {"v": "x"})])
    assert p.ok and _val(p)["tags"] == ["a", "b"] and _val(p)["log"] == ["x"]
    assert not _proj([_eff(1, "tag", {"add": ["a"]}), _eff(2, "tag", {"remove": ["a"]})]).ok
    gone = _proj([_eff(4, "drop", {"path": ("hp",)})], base=p.world)
    assert gone.ok and "hp" not in _val(gone)
    missing = _proj([_eff(5, "drop", {"path": ("nope",)})])
    assert not missing.ok and "없는 키" in missing.conflicts[0].message


def test_path_overlap_same_layer_conflict():
    p = _proj([_eff(1, "set", {"path": ("a",), "value": {"b": 1}}), _eff(2, "set", {"path": ("a", "b"), "value": 2})])
    assert not p.ok and p.conflicts[0].code == "PATH_OVERLAP"


def test_causal_child_reads_parent_and_unordered_cross_layer_conflict():
    p = _proj([_eff(1, "add", {"path": ("hp",), "n": 4}), _eff(2, "copy_hp", parents=("e1",))])
    assert p.ok and _val(p)["seen"] == 7
    # e3은 e1의 자식이라 다음 계층 — e2(부모 없음, 계층 0)와 같은 칸을 다른 값으로: 인과 없음 → 충돌
    q = _proj([_eff(1, "set", {"path": ("x",), "value": 1}), _eff(2, "set", {"path": ("y",), "value": 1}),
               _eff(3, "set", {"path": ("y",), "value": 2}, parents=("e1",))])
    assert not q.ok and q.conflicts[0].code == "UNORDERED_INDEPENDENT_WRITES"
    # 부모-자식 관계가 있으면 덮어쓰기 허용
    r = _proj([_eff(1, "set", {"path": ("y",), "value": 1}), _eff(2, "set", {"path": ("y",), "value": 2}, parents=("e1",))])
    assert r.ok and _val(r)["y"] == 2


def test_insertion_order_does_not_matter():
    effs = [_eff(1, "add", {"path": ("hp",), "n": 1}), _eff(2, "tag", {"add": ["z"]}), _eff(3, "add", {"path": ("hp",), "n": 2}),
            _eff(4, "set", {"path": ("k",), "value": 1}, eid="npc_b")]
    digests = {_proj(list(perm)).world.digest() for perm in itertools.permutations(effs)}
    assert len(digests) == 1


def test_unknown_rejected_foreign():
    assert _proj([_eff(1, "nope")]).conflicts[0].code == "UNKNOWN_OPERATION"
    assert _proj([_eff(1, "set", comp="ghost")]).conflicts[0].code == "UNKNOWN_OPERATION"
    assert _proj([_eff(1, "reject")]).conflicts[0].code == "HANDLER_REJECTED"
    assert _proj([_eff(1, "foreign")]).conflicts[0].code == "FOREIGN_WRITE"


# ── 묶음 불변성 ──

def test_batch_frozen_and_tamper():
    b = K.EffectBatch(batch_id="b", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    e = _eff(1, "set", {"path": ("hp",), "value": 1})
    b.add(e)
    e.args["value"] = 99                                     # 원래 객체 변경은 묶음에 닿지 않는다
    assert b.effects[0].args["value"] == 1
    b.freeze()
    with pytest.raises(K.KernelError):
        b.add(_eff(2, "set", {"path": ("hp",), "value": 2}))
    b._effects[0]["args"]["value"] = 5
    with pytest.raises(K.KernelError):
        K.project(K.World(), b, _reg(), DEFN)
    dup = K.EffectBatch(batch_id="d", origin="PLAYER_TURN", base_version_id=None, definition_revision=None)
    dup.extend([_eff(1, "set"), _eff(1, "set"), _eff(2, "set", parents=("e9",))])
    assert {x.split(":")[0] for x in dup.validate()} == {"EFFECT_ID_DUPLICATE", "PARENT_NOT_EARLIER"}
    assert K.EffectBatch.from_dict(b.to_dict()).to_dict()["effects"][0]["id"] == "e1"


def test_registry_rules():
    r = K.Registry()
    r.register(K.Component("vitals", "actor", "a"))
    with pytest.raises(K.KernelError):
        r.register(K.Component("vitals", "actor", "b"))       # 주인은 하나
    with pytest.raises(K.KernelError):
        r.register(K.Component("x", "monster", "a"))
    r.freeze()
    with pytest.raises(K.KernelError):
        r.register(K.Component("y", "actor", "a"))
    w = K.World({"entities": {"actor": {"a1": {"ghost": {}}}}})
    with pytest.raises(K.KernelError):
        r.check_world(w)


# ── 스냅샷 · 되감기 ──

def _versioned(world, parent=None, cause="t1"):
    return world.with_version(K.next_version("s", parent, cause_kind="PLAYER_TURN", cause_id=cause))


def test_snapshot_put_get_current_rewind(tmp_path):
    st = K.SnapshotStore(str(tmp_path))
    w1 = _versioned(K.World())
    w2 = _versioned(_proj([_eff(1, "add", {"path": ("hp",), "n": 1})], base=w1).world, w1.version, "t2")
    for w in (w1, w2):
        st.put(w)
    st.set_current(w2.version["version_id"])
    assert st.current().digest() == w2.digest()
    st.set_current(w1.version["version_id"])                 # 되감기 = 포인터 이동
    assert st.current().digest() == w1.digest()
    assert st.put(w1) == w1.version["version_id"]            # 같은 내용 재기록은 멱등
    forged = K.World(w2.to_dict()).with_version(w1.version)
    with pytest.raises(K.KernelStoreError):
        st.put(forged)                                       # 같은 버전 다른 내용 거부
    with pytest.raises(K.KernelStoreError):
        st.put(K.World())
    with pytest.raises(K.KernelStoreError):
        st.set_current("wv_none")


# ── 턴 기록 ──

def test_turn_log(tmp_path):
    log = K.TurnLog(str(tmp_path))
    rec = K.TurnRecord("tx1", "wv_a", "wv_b", {"declaration": "간다"},
                       effects_plan=(_eff(1, "set", {"path": ("hp",), "value": 1}).to_dict(),), settlement_ref="st1")
    log.append(rec)
    assert log.find("tx1").to_dict() == rec.to_dict()
    with pytest.raises(K.KernelStoreError):
        log.append(rec)
    with pytest.raises(K.KernelError):
        K.TurnRecord("tx2", "wv_b", None)                    # 커밋 턴에 새 버전 없음
    with pytest.raises(K.KernelError):
        K.TurnRecord("tx3", "wv_b", "wv_c", outcome={"kind": "FAILED_SYSTEM"})
    log.append(K.TurnRecord("tx4", "wv_b", None, outcome={"kind": "FAILED_SYSTEM", "reason": "x"}))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("{깨진 줄\n")
    with pytest.raises(K.KernelStoreError):
        log.read_all()


# ── 계층: 커널은 표준 라이브러리와 자기 자신만 import ──

def test_kernel_imports_stdlib_only():
    bad = []
    for f in sorted((ROOT / "indaim" / "kernel").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                continue
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) else []
            bad += [f"{f.name}:{n}" for n in names if n.split(".")[0] not in sys.stdlib_module_names]
    assert bad == []


def test_core_does_not_import_indaim():
    hits = [str(f) for d in ("core", "cogs") for f in (ROOT / d).glob("*.py")
            if "indaim" in f.read_text(encoding="utf-8").split("\n#")[0] and "import indaim" in f.read_text(encoding="utf-8")]
    assert hits == []
