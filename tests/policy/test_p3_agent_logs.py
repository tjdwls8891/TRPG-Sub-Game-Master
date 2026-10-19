# -*- coding: utf-8 -*-
"""P3 에이전트 판독용 로그 — kind 등록부·순서(b·seq)·조인 키(op_id)·D1 전역 축약·session.meta·
turn.summary·history.selection·턴 흐름 구조 이벤트·!세션이력 manifest/README·줄 경계 분할·열람 도구."""
from __future__ import annotations

import io
import json
import os
import pathlib
import re
import zipfile
from types import SimpleNamespace

import pytest

import core
from cogs import system as SYS
from core import logs as L
from tools import log_view as LV

pytestmark = pytest.mark.policy

ROOT = pathlib.Path(__file__).resolve().parents[2]
SID = "a1b2c3d4"


def _rows(name, sid=SID):
    p = os.path.join(L.session_dir(sid), name)
    return [json.loads(x) for x in open(p, encoding="utf-8")] if os.path.exists(p) else []


# ── G9 등록부 ────────────────────────────────────────────────

def test_every_domain_kind_in_source_is_registered_and_documented():
    pat = re.compile(r"(?:logs\.domain|_log_history|_log_ink)\(\s*\"([a-z_.]+)\"")
    found = set()
    for d in ("core", "cogs"):
        for f in (ROOT / d).glob("*.py"):
            found |= set(pat.findall(f.read_text(encoding="utf-8")))
    assert found and found <= set(L.DOMAIN_KINDS), sorted(found - set(L.DOMAIN_KINDS))
    doc = (ROOT / "docs" / "LOG_SCHEMA.md").read_text(encoding="utf-8")
    assert all(f"`{k}`" in doc for k in L.DOMAIN_KINDS), [k for k in L.DOMAIN_KINDS if f"`{k}`" not in doc]
    assert f"log_schema {L.LOG_SCHEMA_VERSION}" in doc


# ── G1 순서 · D1 전역 축약 ───────────────────────────────────

def test_session_lines_carry_boot_and_monotonic_seq_across_files():
    L.event("x", "a", sid=SID)
    L.domain("display", {"k": 1}, sid=SID)
    L.event("x", "b", sid=SID)
    rows = _rows("events.jsonl") + _rows("domain.jsonl")
    assert {r["b"] for r in rows} == {L.BOOT_ID}
    seqs = sorted(r["seq"] for r in rows)
    assert len(set(seqs)) == 3
    ev = _rows("events.jsonl")
    assert ev[0]["seq"] < _rows("domain.jsonl")[0]["seq"] < ev[1]["seq"]


def test_global_copy_keeps_location_only(monkeypatch):
    lines = []
    monkeypatch.setattr(L, "_global_line", lambda rec: lines.append(rec))
    L.event("turn", "판단", sid=SID, data={"reasoning": "서사 원문"})
    assert lines[-1]["sid"] == SID and "data" not in lines[-1]
    assert _rows("events.jsonl")[-1]["data"] == {"reasoning": "서사 원문"}
    os.makedirs(os.path.join("sessions", SID), exist_ok=True)
    tee = L._Tee(io.StringIO(), "INFO")
    tee.write(f"[GM/{SID}] ASK -> '비밀 서사'\n")
    assert "비밀 서사" not in lines[-1]["msg"] and lines[-1]["sid"] == SID
    last = _rows("events.jsonl")[-1]
    assert last["src"] == "print" and last["area"] == "GM" and "비밀 서사" in last["msg"]


def test_print_area():
    assert L.print_area("[GM/abc] x") == "GM"
    assert L.print_area("⛔ [WP-E] 차단") == "WP-E"
    assert L.print_area("[H05 계획] status") == "H05 계획"
    assert L.print_area("그냥 출력") == "print"


# ── G2 session.meta ───────────────────────────────────────────

def test_session_meta_has_code_and_world_identity(session_auto_ready):
    s = session_auto_ready
    L.session_meta(s, "create")
    m = [d for d in _rows("domain.jsonl", s.session_id) if d["kind"] == "session.meta"][-1]["data"]
    for k in ("reason", "boot", "git_sha", "version", "log_schema", "prompts_digest", "scenario_id",
              "definition_revision", "world_lifecycle", "players"):
        assert k in m, k
    assert m["boot"] == L.BOOT_ID and m["version"] == core.__version__ and "world_mode" not in m


def test_log_failure_never_blocks():
    L.session_meta(object(), "create")                         # 세션 아님 — 흡수
    core.turn_log.note_status(object(), "COMMITTED")           # 트랜잭션 아님 — 흡수


# ── G3·G1·G4 실제 커밋 턴 ─────────────────────────────────────

from tests.policy.test_h05bc_turn_wiring import drig, judgment, rig, turn  # noqa: E402,F401


async def test_committed_turn_summary_joins_calls_costs_and_selection(drig):
    r = drig
    r.make()
    r.bot.genai_client.models = L.LoggedModels(r.bot.genai_client.models)
    t = await turn(r)
    sid = r.sess.session_id
    dom = _rows("domain.jsonl", sid)
    summ = [d["data"] for d in dom if d["kind"] == "turn.summary" and d["data"]["tx"] == t.transaction_id]
    assert summ and summ[-1]["status"] == "COMMITTED" and not summ[-1].get("partial")
    s = summ[-1]
    assert s["declaration"] == "시장으로 간다" and s["judgment"]["action"] == "PROCEED"
    assert s["world"]["plan"]["status"] and s["prep"]["narration"]["chars"] > 0
    assert "COMMITTED" in s["stages_ms"]
    calls = {c["call"]: c for c in _rows("calls.jsonl", sid)}
    assert s["calls"] and all(cid in calls for _layer, cid in s["calls"])
    # 호출 ↔ 비용: calls.op_id == cost.event.operation_id
    ops = {c.get("op_id") for c in calls.values() if c.get("op_id")}
    cost = [d["data"] for d in dom if d["kind"] == "cost.event"]
    assert cost and {c["operation_id"] for c in cost} & ops
    assert all(c["idempotency_key"].startswith(c["operation_id"] + ":attempt:") for c in cost)
    sel = [d["data"] for d in dom if d["kind"] == "history.selection"]
    assert sel and sel[-1]["reason"] == "SELECT" and sel[-1]["tx"] == t.transaction_id
    # 커밋 뒤 호출 참조는 비워진다(메모리 누수 없음)
    assert L.calls_of(t.transaction_id) == []


async def test_waiting_turn_emits_partial_summary_and_turn_event(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment(action="ASK")
    t = await turn(r)
    sid = r.sess.session_id
    summ = [d["data"] for d in _rows("domain.jsonl", sid) if d["kind"] == "turn.summary"]
    assert summ[-1]["partial"] is True and summ[-1]["tx"] == t.transaction_id
    assert any(e.get("area") == "turn" and (e.get("data") or {}).get("stage") == "judgment"
               for e in _rows("events.jsonl", sid))


async def test_rewind_logs_deselected_selection(drig):
    r = drig
    r.make()
    await turn(r)
    r.prov.routes["judgment"] = judgment()
    t2 = await turn(r, "주변을 둘러본다")
    sid = r.sess.session_id
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res.get("ok"), res
    sel = [d["data"] for d in _rows("domain.jsonl", sid) if d["kind"] == "history.selection"]
    assert sel[-1]["reason"] == "REWIND" and sel[-1]["head_after"] == 1
    assert sel[-1]["deselected"] == {"2": t2.transaction_id}


# ── G6·G7 묶음 ───────────────────────────────────────────────

def _make(tmp_path, files):
    root = tmp_path / "sessions" / "s1"
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return str(root)


def _unzip(parts):
    out = {}
    for _n, data in parts:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                out[n] = z.read(n)
    return out


def test_pack_adds_manifest_and_readme_with_selection_and_time_range(tmp_path):
    th = "\n".join(json.dumps(e) for e in (
        {"type": "SELECT", "gm_turn": 1, "transaction_id": "txA", "logical_turn": 1, "attempt": 1},
        {"type": "SELECT", "gm_turn": 1, "transaction_id": "txB", "logical_turn": 1, "attempt": 2})) + "\n"
    dom = json.dumps({"ts": 1_760_000_000.0, "b": "x", "seq": 1, "kind": "session.meta",
                      "data": {"reason": "create", "git_sha": "abc"}}) + "\n"
    path = _make(tmp_path, {"data.json": json.dumps({"scenario_id": "영도", "players": {"1": {}}}).encode(),
                            "turn_history.jsonl": th.encode(), "logs/domain.jsonl": dom.encode()})
    parts, skipped = SYS.pack_session_dir(path, "s1")
    files = _unzip(parts)
    assert skipped == [] and files["s1/_index/LOG_README.md"].startswith(b"# INDAIM")
    m = json.loads(files["s1/_index/manifest.json"])
    assert m["log_schema"] == L.LOG_SCHEMA_VERSION and m["session"]["scenario_id"] == "영도"
    assert m["session"]["players"] == 1 and m["last_session_meta"]["git_sha"] == "abc"
    assert m["selection"]["selected"]["1"]["tx"] == "txB" and m["selection"]["unselected_tx"] == ["txA"]
    assert m["time_range"]["first"].endswith("+09:00")
    assert {f["path"] for f in m["files"]} == {"data.json", "turn_history.jsonl", "logs/domain.jsonl"}


def test_pack_splits_large_jsonl_on_line_boundaries_and_viewer_rejoins(tmp_path):
    rows = [{"ts": 1.0 + i, "b": "x", "seq": i, "lv": "INFO", "area": "a", "msg": os.urandom(40).hex()}
            for i in range(200)]
    raw = "".join(json.dumps(r) + "\n" for r in rows).encode()
    path = _make(tmp_path, {"logs/events.jsonl": raw, "huge.bin": os.urandom(9000)})
    parts, skipped = SYS.pack_session_dir(path, "s1", limit=8000)
    files = _unzip(parts)
    pieces = sorted(n for n in files if re.fullmatch(r"s1/logs/events\.part\d{3}\.jsonl", n))
    assert len(pieces) > 1 and b"".join(files[n] for n in pieces) == raw
    assert all(files[n].endswith(b"\n") for n in pieces)
    assert skipped == [("s1/huge.bin", 9000)]
    m = json.loads(files["s1/_index/manifest.json"])
    ev = next(f for f in m["files"] if f["path"] == "logs/events.jsonl")
    assert ev["lines"] == 200 and ev["parts"] == [n.split("/", 1)[1] for n in pieces]
    assert m["excluded"] == [{"path": "huge.bin", "size": 9000}]
    zips = []
    for i, (n, data) in enumerate(parts):
        p = tmp_path / n
        p.write_bytes(data)
        zips.append(str(p))
    D = LV.load(*zips)
    assert [e["seq"] for e in D["events"]] == list(range(200))


def test_viewer_shows_date_and_literal_hash_text():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", LV.ts(1_760_000_000))
    D = {"blob_map": {}}
    assert LV.piece(D, "# 제목") == "# 제목"
    assert LV.piece(D, "#0123456789ab").startswith("<조각 누락")
