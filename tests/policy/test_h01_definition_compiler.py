# -*- coding: utf-8 -*-
"""H01 — 정의 컴파일러·정체성·스냅샷 기반.

REQ-024  ActorId는 이름과 분리(저작 NPC 결정적 ID·명시 ID 우선)
REQ-052  세션 정의 스냅샷(원자적 쓰기·해시 검증·덮어쓰기 거부) — 프리미티브만, 배선은 H10
REQ-066  legacy 시나리오(capability 메타데이터 없음)가 새 capability 없이 컴파일·로드
REQ-068  컴파일러·도구는 원본을 고치지 않고 산문에서 기계 진실을 추론하지 않는다
DESIGN 08 §2~6·§31~51 · DESIGN 06 §14~21 · DESIGN 11 §12~13
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys

import pytest

from core import definition_compiler as dc
from core import definition_snapshot as ds
from core import world_ids as wid
from tests.conftest import REPO_ROOT

pytestmark = pytest.mark.policy

SCEN_DIR = os.path.join(REPO_ROOT, "scenarios")
ALL_SCENARIOS = ("영도", "무협", "다크판타지", "빈시나리오", "scenario.example")


def _src(sid):
    return dc.load_scenario_sources(sid, SCEN_DIR)


def _raw(sid):
    with open(os.path.join(SCEN_DIR, f"{sid}.json"), encoding="utf-8") as f:
        return json.load(f)


def _codes(res, severity=None):
    return [d.code for d in res.diagnostics if severity is None or d.severity == severity]


def _mini(**extra):
    raw = {
        "worldview": "섬. 본토까지 약 185km. 강철수는 주로 항구에 있다.",
        "default_npcs": {
            "강철수": {"name": "강철수", "비밀": "통조림 오염을 숨긴다.", "stats": {}},
            "김영희": {"name": "김영희"},
        },
        "places": {
            "섬": {},
            "항구": {"parent": "섬", "connected": ["시장"], "npcs": [{"name": "강철수"}]},
            "시장": {"parent": "섬", "connected": []},
        },
    }
    raw.update(extra)
    return raw


# ── world_ids ────────────────────────────────────────────────

def test_canonical_json_is_key_order_independent_and_strict():
    a = {"b": 1, "a": [1, {"y": 2, "x": 1}]}
    b = {"a": [1, {"x": 1, "y": 2}], "b": 1}
    assert wid.canonical_json(a) == wid.canonical_json(b)
    assert wid.content_digest(a) == wid.content_digest(b)
    # 삽입 순서 지문은 순서를 구분한다
    assert wid.order_digest(a) != wid.order_digest(b)
    for bad in ({1: "x"}, {"x": float("nan")}, {"x": {1, 2}}, {"x": float("inf")}):
        with pytest.raises(wid.CanonicalSerializationError):
            wid.canonical_json(bad)


def test_stable_digest_has_no_concatenation_collision():
    assert wid.stable_digest("ns.v1", "a:b", "c") != wid.stable_digest("ns.v1", "a", "b:c")
    assert wid.stable_digest("ns.v1", "x") != wid.stable_digest("ns.v2", "x")
    assert wid.stable_digest("ns.v1", "x") == wid.stable_digest("ns.v1", "x")


def test_authored_ids_are_deterministic_and_explicit_id_wins():
    a1 = wid.authored_npc_actor_id("영도", "강철수")
    assert a1 == wid.authored_npc_actor_id("영도", "강철수")
    assert a1 != wid.authored_npc_actor_id("무협", "강철수")
    assert wid.authored_npc_actor_id("영도", "강철수", "npc.kang") == "npc.kang"
    assert wid.pc_actor_id(555001) == wid.pc_actor_id("555001") == "pc_555001"
    with pytest.raises(ValueError):
        wid.pc_actor_id("abc")
    assert wid.npc_inline_secret_info_id(a1) != wid.npc_inline_secret_info_id("npc.kang")


_DETERMINISM_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from core import definition_compiler as dc, world_ids as wid
out = {}
for sid in ("영도", "무협", "다크판타지"):
    r = dc.compile_sources(dc.load_scenario_sources(sid, sys.argv[2]))
    out[sid] = [r.revision, r.definition["actor_order"][:3],
                sorted(r.definition["information_definitions"])[:3]]
out["ids"] = [wid.authored_npc_actor_id("영도", "강철수"), wid.stable_digest("t.v1", 1, "가")]
print(json.dumps(out, ensure_ascii=False))
"""


def test_revision_and_ids_are_process_and_hashseed_independent():
    """같은 원본 → 다른 프로세스·PYTHONHASHSEED에서도 같은 revision·ID."""
    results = []
    for seed in ("0", "4242"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        cp = subprocess.run([sys.executable, "-c", _DETERMINISM_SCRIPT, REPO_ROOT, SCEN_DIR],
                            capture_output=True, text=True, env=env, timeout=120)
        assert cp.returncode == 0, cp.stderr
        results.append(json.loads(cp.stdout.strip().splitlines()[-1]))
    assert results[0] == results[1]
    here = dc.compile_sources(_src("영도"))
    assert results[0]["영도"][0] == here.revision


# ── legacy 호환 (REQ-066) ────────────────────────────────────

@pytest.mark.parametrize("sid", ALL_SCENARIOS)
def test_every_shipped_scenario_compiles_as_legacy_without_error(sid):
    res = dc.compile_sources(_src(sid))
    assert res.ok, [d.to_dict() for d in res.errors]
    meta = res.definition["scenario_meta"]
    assert meta["schema_mode"] == "legacy"
    assert meta["capabilities"] == {} and meta["validated_capabilities"] == []
    assert res.revision == dc.definition_revision(res.definition)
    # 현행 런타임이 쓰는 raw 모양이 순서까지 그대로 복원된다
    scen, _ = dc.legacy_scenario_view(res.definition)
    raw = _raw(sid)
    assert scen == raw and list(scen) == list(raw)
    if isinstance(raw.get("places"), dict):
        assert list(scen["places"]) == list(raw["places"])
    assert list(scen.get("default_npcs") or {}) == list(raw.get("default_npcs") or {})


def test_yeongdo_wuxia_darkfantasy_compile_report_sizes():
    y = dc.compile_sources(_src("영도")).definition
    raw_y = _raw("영도")
    quests = json.load(open(os.path.join(SCEN_DIR, "영도.quests.json"), encoding="utf-8"))
    assert len(y["actor_definitions"]) == len(raw_y["default_npcs"])
    assert y["place_order"] == list(raw_y["places"])
    assert y["movement_edges"] == {}               # connected에서 구간을 만들지 않는다
    kinds = [i["kind"] for i in y["information_definitions"].values()]
    assert kinds.count("NPC_INLINE_SECRET") == sum(1 for v in raw_y["default_npcs"].values() if v.get("비밀"))
    assert kinds.count("QUEST_HIDDEN_TRUTH") == sum(
        1 for q in quests["quests"] if (q.get("hidden") or {}).get("truth"))
    assert [f["frame_id"] for f in y["initialization_definitions"]["start_frames"]] == \
        [f["id"] for f in raw_y["start_frames"]]

    w = dc.compile_sources(_src("무협"))
    assert w.ok and len(w.definition["actor_definitions"]) == len(_raw("무협")["default_npcs"])
    assert w.definition["place_definitions"] == {} and w.definition["scenario_meta"]["has_quest_sidecar"] is False

    k = dc.compile_sources(_src("다크판타지"))
    assert k.ok and len(k.definition["actor_definitions"]) == len(_raw("다크판타지")["default_npcs"])
    for res in (w, k):
        s = dc.summarize(res)
        assert s["ok"] and s["counts"]["ERROR"] == 0 and s["schema_mode"] == "legacy"


def test_compile_does_not_mutate_input_or_touch_sources():
    def _digest_dir():
        return {f: hashlib.sha256(open(os.path.join(SCEN_DIR, f), "rb").read()).hexdigest()
                for f in sorted(os.listdir(SCEN_DIR))}
    before = _digest_dir()
    for sid in ALL_SCENARIOS:
        s = _src(sid)
        raw0, q0 = copy.deepcopy(s.raw), copy.deepcopy(s.quests)
        res = dc.compile_sources(s)
        dc.migration_report(s)
        assert s.raw == raw0 and s.quests == q0
        # 정의가 원본 객체를 공유하지 않는다(이후 변경이 정의를 오염시키지 않음)
        res.definition["legacy_source"]["scenario"]["__probe__"] = 1
        assert "__probe__" not in s.raw
    assert _digest_dir() == before
    assert not os.path.exists("sessions")          # 컴파일은 아무것도 쓰지 않는다


def test_same_input_same_revision_and_order_is_part_of_definition():
    raw = _mini()
    r1 = dc.compile_scenario(copy.deepcopy(raw), "t")
    r2 = dc.compile_scenario(copy.deepcopy(raw), "t")
    assert r1.revision == r2.revision and r1.definition == r2.definition
    reordered = copy.deepcopy(raw)
    reordered["places"] = dict(reversed(list(raw["places"].items())))
    r3 = dc.compile_scenario(reordered, "t")
    # 장소 순회 순서는 런타임 의미에 닿으므로 정의(revision)의 일부다
    assert r3.revision != r1.revision
    assert r3.definition["place_order"] == list(reordered["places"])


# ── 산문 추론 금지·자동 보정 금지 (REQ-068) ──────────────────

def test_no_machine_truth_from_prose_or_static_npc_lists():
    res = dc.compile_scenario(_mini(), "t")
    df = res.definition
    assert df["movement_edges"] == {}
    assert "185" not in json.dumps({k: v for k, v in df.items() if k != "legacy_source"}, ensure_ascii=False)
    port = df["place_definitions"]["항구"]
    # places[*].npcs → 연관(association)으로만, 위치 필드는 없다
    assert port["npc_associations"] == [{"legacy_name": "강철수",
                                         "actor_id": df["actor_index"]["강철수"]}]
    for a in df["actor_definitions"].values():
        assert not ({"location", "place", "current_place"} & set(a))
    # 비대칭 connected는 자동 보정하지 않는다
    assert df["place_definitions"]["시장"]["connected"] == []
    assert "PLACE_CONNECTED_ASYMMETRIC" in _codes(res, "WARNING")
    rep = dc.migration_report(dc.ScenarioSources("t", _mini(), None, {}))
    assert rep["places"]["asymmetric_connected"] == [["항구", "시장"]]


def test_start_frame_facts_stay_templates():
    raw = _mini(start_points=["항구"], intro_times=["새벽"], start_frames=[{
        "id": "f1", "title": "도착", "conditions": {},
        "slots": {"때": "list:intro_times", "장소": "place:start", "x": "list:없는목록"},
        "facts": {"time": "{때}", "location": "{장소}"}}])
    res = dc.compile_scenario(raw, "t")
    fr = res.definition["initialization_definitions"]["start_frames"][0]
    assert fr["fact_templates"] == {"time": "{때}", "location": "{장소}"}
    assert fr["slots"]["때"] == {"spec": "list:intro_times", "source": "list", "ref": "intro_times"}
    assert fr["slots"]["장소"]["source"] == "place"
    assert "START_SLOT_LIST_MISSING" in _codes(res, "WARNING")
    assert not ({"minute_index", "world_clock", "date"} & set(fr))


# ── capability ──────────────────────────────────────────────

def test_capability_declaration_validation():
    ok = dc.compile_scenario(_mini(scenario_schema={"version": 1, "capabilities": {"stable_actors": 1}}), "t")
    assert ok.ok
    assert ok.definition["scenario_meta"]["validated_capabilities"] == ["stable_actors"]
    assert dc.capability_matrix(ok)["stable_actors"] == "v1 validated"

    deferred = dc.compile_scenario(_mini(scenario_schema={"version": 1, "capabilities": {
        "structured_places": 1}}), "t")
    assert deferred.ok and "CAP_VALIDATION_DEFERRED" in _codes(deferred, "WARNING")
    assert deferred.definition["scenario_meta"]["deferred_capabilities"] == ["structured_places"]

    cases = {
        "CAP_UNKNOWN": {"version": 1, "capabilities": {"teleport": 1}},
        "CAP_VERSION_UNSUPPORTED": {"version": 1, "capabilities": {"stable_actors": 9}},
        "CAP_VERSION_INVALID": {"version": 1, "capabilities": {"stable_actors": True}},
        "SCHEMA_VERSION_UNSUPPORTED": {"version": 2, "capabilities": {}},
        "SCHEMA_VERSION_INVALID": {"capabilities": {}},
        "CAP_DEPENDENCY_MISSING": {"version": 1, "capabilities": {"scheduled_world_progression": 1}},
    }
    for code, meta in cases.items():
        res = dc.compile_scenario(_mini(scenario_schema=meta), "t")
        assert not res.ok and res.revision is None, code
        assert code in _codes(res, "ERROR"), (code, _codes(res))


def test_dynamic_actor_location_requires_structured_places_as_error():
    """H01 보정: dynamic_actor_location → structured_places 의존은 ERROR."""
    bad = dc.compile_scenario(_mini(scenario_schema={"version": 1, "capabilities": {
        "dynamic_actor_location": 1}}), "t")
    assert not bad.ok and bad.revision is None
    assert "CAP_DEPENDENCY_MISSING" in _codes(bad, "ERROR")
    assert "CAP_DEPENDENCY_MISSING" not in _codes(bad, "WARNING")
    good = dc.compile_scenario(_mini(scenario_schema={"version": 1, "capabilities": {
        "dynamic_actor_location": 1, "structured_places": 1}}), "t")
    assert good.ok and "CAP_DEPENDENCY_MISSING" not in _codes(good)


def test_reserved_dynamic_keys_warn_not_silently_ignored():
    res = dc.compile_scenario(_mini(calendar={"start": "2031-03-01"}), "t")
    assert res.ok and "RESERVED_KEY_UNCOMPILED" in _codes(res, "WARNING")


# ── 인물·정보 정체성 (REQ-024) ───────────────────────────────

def test_actor_identity_is_separate_from_display_name():
    base = dc.compile_scenario(_mini(), "t").definition
    renamed = _mini()
    renamed["default_npcs"]["강철수"]["name"] = "철수 영감"
    df = dc.compile_scenario(renamed, "t").definition
    aid = base["actor_index"]["강철수"]
    assert df["actor_index"]["강철수"] == aid
    assert df["actor_definitions"][aid]["display_name"] == "철수 영감"
    sec = [i for i in df["information_definitions"].values() if i["kind"] == "NPC_INLINE_SECRET"]
    assert len(sec) == 1 and sec[0]["owner_actor_id"] == aid
    assert sec[0]["info_id"] == wid.npc_inline_secret_info_id(aid)


def test_default_npcs_key_rename_without_explicit_id_is_id_migration():
    """명시 ID 없는 저작 NPC의 사전 키 변경은 ActorId 변경(= ID migration)이다.

    표시 이름(name) 변경은 ID를 유지하지만, 키는 정체성 입력이다. 명시 actor_id가
    있으면 키를 바꿔도 ID가 유지된다.
    """
    base = dc.compile_scenario(_mini(), "t").definition
    rekeyed = _mini()
    rekeyed["default_npcs"] = {("강철수_개명" if k == "강철수" else k): v
                               for k, v in rekeyed["default_npcs"].items()}
    df = dc.compile_scenario(rekeyed, "t").definition
    assert df["actor_index"]["강철수_개명"] != base["actor_index"]["강철수"]
    assert "강철수" not in df["actor_index"]

    pinned, pinned_rekeyed = _mini(), _mini()
    pinned["default_npcs"]["강철수"]["actor_id"] = "npc.kang"
    pinned_rekeyed["default_npcs"] = {("강철수_개명" if k == "강철수" else k): v
                                      for k, v in pinned["default_npcs"].items()}
    assert dc.compile_scenario(pinned_rekeyed, "t").definition["actor_index"]["강철수_개명"] == "npc.kang"


def test_explicit_actor_id_rules():
    raw = _mini()
    raw["default_npcs"]["강철수"]["actor_id"] = "npc.kang"
    df = dc.compile_scenario(raw, "t").definition
    assert df["actor_index"]["강철수"] == "npc.kang"
    assert df["actor_definitions"]["npc.kang"]["explicit_id"] is True

    bad = _mini()
    bad["default_npcs"]["강철수"]["actor_id"] = "공백 있는 id"
    assert "ACTOR_ID_INVALID" in _codes(dc.compile_scenario(bad, "t"), "ERROR")

    dup = _mini()
    dup["default_npcs"]["강철수"]["actor_id"] = "same"
    dup["default_npcs"]["김영희"]["actor_id"] = "same"
    assert "ACTOR_ID_DUPLICATE" in _codes(dc.compile_scenario(dup, "t"), "ERROR")


def test_quest_hidden_compile_and_identity():
    quests = {"quests": [
        {"id": "q1", "version": "a", "filters": {"place": {"any": ["항구"]}},
         "hidden": {"truth": "물가는 위험하다", "if_known": "규칙"}},
        {"id": "q2", "version": "a", "filters": {}, "hidden": {}},
    ]}
    res = dc.compile_scenario(_mini(), "t", quests)
    infos = [i for i in res.definition["information_definitions"].values()
             if i["kind"] == "QUEST_HIDDEN_TRUTH"]
    assert len(infos) == 1 and infos[0]["quest_id"] == "q1"
    assert infos[0]["info_id"] == wid.quest_hidden_info_id("t", "q1")
    assert infos[0]["quest_version"] == "a"                # version은 메타데이터로만 남는다
    assert "QUEST_HIDDEN_NO_TRUTH" not in _codes(res)      # 빈 hidden은 '없음'(quest.py와 동일)

    # 현행 런타임(quest._find_quest)은 id만으로 찾는다 → 같은 id의 다른 version도 ERROR
    for dup in ({"quests": [{"id": "q1", "version": "a"}, {"id": "q1", "version": "a"}]},
                {"quests": [{"id": "q1", "version": "a"}, {"id": "q1", "version": "b"}]}):
        res = dc.compile_scenario(_mini(), "t", dup)
        assert not res.ok and "QUEST_ID_DUPLICATE" in _codes(res, "ERROR")


def test_quest_hidden_info_id_survives_version_and_text_change():
    def _id(version, truth):
        q = {"quests": [{"id": "q1", "version": version, "filters": {}, "hidden": {"truth": truth}}]}
        df = dc.compile_scenario(_mini(), "t", q).definition
        (iid, rec), = [(k, v) for k, v in df["information_definitions"].items()
                       if v["kind"] == "QUEST_HIDDEN_TRUTH"]
        return iid, rec
    a, ra = _id("a", "물가는 위험하다")
    b, rb = _id("b", "물가는 감염자가 모이는 자리다")
    c, _ = _id(None, "물가는 위험하다")
    assert a == b == c == wid.quest_hidden_info_id("t", "q1")
    assert (ra["quest_version"], rb["quest_version"]) == ("a", "b")
    assert ra["proposition"] != rb["proposition"]
    assert a != wid.quest_hidden_info_id("t", "q2")


def test_compiler_version_is_bound_into_revision(monkeypatch):
    raw = _mini()
    r1 = dc.compile_scenario(copy.deepcopy(raw), "t")
    assert r1.definition["compiler_version"] == dc.COMPILER_VERSION == r1.compiler_version
    monkeypatch.setattr(dc, "COMPILER_VERSION", dc.COMPILER_VERSION + 1)
    r2 = dc.compile_scenario(copy.deepcopy(raw), "t")
    assert r2.definition["compiler_version"] == r1.definition["compiler_version"] + 1
    assert r2.revision != r1.revision                       # 같은 원본, 다른 컴파일 의미 → 다른 revision
    assert r2.revision == dc.definition_revision(r2.definition)
    # 바뀐 부분은 compiler_version 하나뿐이다
    d1, d2 = dict(r1.definition), dict(r2.definition)
    d1.pop("compiler_version"); d2.pop("compiler_version")
    assert d1 == d2


# ── 장소·구간 sugar ─────────────────────────────────────────

def test_place_structure_errors():
    raw = _mini()
    raw["places"]["항구"]["parent"] = "없는곳"
    assert "PLACE_PARENT_MISSING" in _codes(dc.compile_scenario(raw, "t"), "ERROR")
    cyc = _mini()
    cyc["places"]["섬"]["parent"] = "항구"
    assert "PLACE_PARENT_CYCLE" in _codes(dc.compile_scenario(cyc, "t"), "ERROR")
    al = _mini()
    al["places"]["항구"]["aliases"] = ["시장"]
    assert "PLACE_ALIAS_SHADOWED" in _codes(dc.compile_scenario(al, "t"), "WARNING")


def test_bidirectional_edge_sugar_compiles_to_directed_edges_without_inventing_numbers():
    raw = _mini(movement_edges=[
        {"from": "항구", "to": "시장", "bidirectional": True, "distance_m": 400,
         "reverse": {"route_hint": "오르막"}},
        {"id": "ferry", "from": "시장", "to": "항구"},
    ], scenario_schema={"version": 1, "capabilities": {"structured_places": 1}})
    res = dc.compile_scenario(raw, "t")
    assert res.ok, _codes(res)
    e = res.definition["movement_edges"]
    pair = wid.movement_edge_pair_id("t", "항구", "시장")
    fwd, rev = e[f"{pair}#fwd"], e[f"{pair}#rev"]
    assert (fwd["from"], fwd["to"], rev["from"], rev["to"]) == ("항구", "시장", "시장", "항구")
    assert fwd["properties"] == {"distance_m": 400}
    assert rev["properties"] == {"distance_m": 400, "route_hint": "오르막"}
    assert e["ferry#fwd"]["properties"] == {} and "ferry#rev" not in e
    assert len(e) == 3

    for code, edges in {
        "EDGE_ENDPOINT_MISSING": [{"from": "항구", "to": "없는곳"}],
        "EDGE_SELF_LOOP": [{"from": "항구", "to": "항구"}],
        "EDGE_IDENTITY_AMBIGUOUS": [{"from": "항구", "to": "시장"}, {"from": "항구", "to": "시장"}],
        "EDGE_REVERSE_INVALID": [{"from": "항구", "to": "시장", "reverse": {}}],
    }.items():
        assert code in _codes(dc.compile_scenario(_mini(movement_edges=edges), "t"), "ERROR"), code
    assert "EDGES_WITHOUT_CAPABILITY" in _codes(
        dc.compile_scenario(_mini(movement_edges=[{"from": "항구", "to": "시장"}]), "t"), "WARNING")


def test_non_canonical_source_is_rejected():
    res = dc.compile_scenario(_mini(bad={1: "x"}), "t")
    assert not res.ok and "SOURCE_NOT_CANONICAL" in _codes(res, "ERROR")
    with pytest.raises(dc.DefinitionCompileError):
        res.require_definition()


# ── 세션 정의 스냅샷 (REQ-052 프리미티브) ────────────────────

def test_snapshot_roundtrip_atomic_and_hash_verified():
    res = dc.compile_sources(_src("다크판타지"))
    snap = ds.write_snapshot("s1", res, source=_src("다크판타지").provenance)
    path = ds.snapshot_path("s1")
    assert path == os.path.join("sessions", "s1", "definition_snapshot.json") and os.path.isfile(path)
    assert not [f for f in os.listdir("sessions/s1") if f.endswith(".tmp")]
    assert snap.definition_revision == res.revision and snap.scenario_id == "다크판타지"
    assert snap.compiler_version == res.definition["compiler_version"] == dc.COMPILER_VERSION
    back = ds.read_snapshot("s1", expected_revision=res.revision)
    assert back.definition == res.definition
    scen, _ = dc.legacy_scenario_view(back.definition)
    assert list(scen) == list(_raw("다크판타지"))
    assert ds.verify_snapshot("s1")["ok"] is True


def test_snapshot_idempotent_and_never_overwritten_by_other_revision():
    a = dc.compile_scenario(_mini(), "t")
    b = dc.compile_scenario(_mini(note="변경"), "t")
    ds.write_snapshot("s2", a)
    before = open(ds.snapshot_path("s2"), "rb").read()
    assert ds.write_snapshot("s2", a).definition_revision == a.revision      # 멱등
    with pytest.raises(ds.DefinitionSnapshotConflict):
        ds.write_snapshot("s2", b)
    assert open(ds.snapshot_path("s2"), "rb").read() == before


def test_snapshot_revision_excludes_provenance():
    res = dc.compile_scenario(_mini(), "t")
    s1 = ds.write_snapshot("p1", res, source={"scenario": {"sha256": "aa"}})
    s2 = ds.write_snapshot("p2", res, source={"scenario": {"sha256": "bb"}})
    assert s1.definition_revision == s2.definition_revision == res.revision


def test_snapshot_corruption_mismatch_missing_are_loud():
    res = dc.compile_scenario(_mini(), "t")
    with pytest.raises(ds.DefinitionSnapshotMissing):
        ds.read_snapshot("nope")
    assert ds.verify_snapshot("nope")["ok"] is False

    ds.write_snapshot("c1", res)
    with pytest.raises(ds.DefinitionSnapshotMismatch):
        ds.read_snapshot("c1", expected_revision="sha256:" + "0" * 64)

    def _tamper(sid, fn):
        p = ds.snapshot_path(sid)
        env = json.load(open(p, encoding="utf-8"))
        fn(env)
        json.dump(env, open(p, "w", encoding="utf-8"), ensure_ascii=False)

    ds.write_snapshot("c2", res)
    _tamper("c2", lambda env: env["definition"]["actor_index"].update({"강철수": "forged"}))
    with pytest.raises(ds.DefinitionSnapshotCorruption):
        ds.read_snapshot("c2")

    ds.write_snapshot("c3", res)

    def _reorder(env):
        pl = env["definition"]["legacy_source"]["scenario"]["places"]
        env["definition"]["legacy_source"]["scenario"]["places"] = dict(reversed(list(pl.items())))
    _tamper("c3", _reorder)
    with pytest.raises(ds.DefinitionSnapshotCorruption):
        ds.read_snapshot("c3")
    # 손상 파일 위에 조용히 재생성하지 않는다
    with pytest.raises(ds.DefinitionSnapshotCorruption):
        ds.write_snapshot("c3", res)

    # 봉투 compiler_version ≠ 정의 compiler_version
    ds.write_snapshot("c5", res)
    _tamper("c5", lambda env: env.update({"compiler_version": env["compiler_version"] + 1}))
    with pytest.raises(ds.DefinitionSnapshotCompilerVersionMismatch):
        ds.read_snapshot("c5")
    # 정의 안의 compiler_version을 바꾸면 revision 재계산이 어긋난다
    ds.write_snapshot("c6", res)
    _tamper("c6", lambda env: env["definition"].update(
        {"compiler_version": env["definition"]["compiler_version"] + 1}))
    with pytest.raises(ds.DefinitionSnapshotCorruption):
        ds.read_snapshot("c6")
    with pytest.raises(ds.DefinitionSnapshotCompilerVersionMismatch):
        ds.build_envelope(res.definition, compiler_version=res.compiler_version + 1)

    os.makedirs("sessions/c4")
    open(ds.snapshot_path("c4"), "w").write("{broken")
    with pytest.raises(ds.DefinitionSnapshotCorruption):
        ds.read_snapshot("c4")


# ── 운영자 도구 ─────────────────────────────────────────────

def _load_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "scenario_check", os.path.join(REPO_ROOT, "tools", "scenario_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scenario_check_tool_outputs_and_never_writes_sources(tmp_path, capsys):
    tool = _load_tool()
    before = {f: open(os.path.join(SCEN_DIR, f), "rb").read() for f in os.listdir(SCEN_DIR)}
    assert tool.main(["--all"]) == 0
    out = capsys.readouterr().out
    for sid in ("영도", "무협", "다크판타지"):
        assert f"== {sid}" in out

    comp = tmp_path / "영도.compiled.json"
    rep = tmp_path / "영도.migration.json"
    assert tool.main(["영도", "--compile-out", str(comp)]) == 0
    assert tool.main(["영도", "--migration-report", str(rep)]) == 0
    env = json.loads(comp.read_text(encoding="utf-8"))
    assert env["definition_revision"] == dc.definition_revision(env["definition"]) \
        == dc.compile_sources(_src("영도")).revision
    first = comp.read_bytes()
    assert tool.main(["영도", "--compile-out", str(comp)]) == 0
    assert comp.read_bytes() == first                    # 같은 입력 → 같은 파일
    mig = json.loads(rep.read_text(encoding="utf-8"))
    assert len(mig["places"]["asymmetric_connected"]) > 0
    assert mig["actors"]["count"] == len(_raw("영도")["default_npcs"])

    with pytest.raises(SystemExit):
        tool.main(["영도", "--compile-out", os.path.join(SCEN_DIR, "영도.json")])
    assert {f: open(os.path.join(SCEN_DIR, f), "rb").read() for f in os.listdir(SCEN_DIR)} == before

    ds.write_snapshot("t1", dc.compile_scenario(_mini(), "t"))
    assert tool.main(["--verify-snapshot", "t1"]) == 0
    assert tool.main(["--verify-snapshot", "missing"]) == 1
