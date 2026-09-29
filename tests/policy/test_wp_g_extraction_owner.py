# -*- coding: utf-8 -*-
"""WP-G FINAL RE-GATE — 비준비 추출의 정본 직접 적용 경로 은퇴.

최종 불변식:
    provider extraction → preparation → READY → CommitCoordinator
    → _apply_commit_effects → _apply_prepared_extraction → _apply_extraction_plan
만 정본 변이를 수행한다. preparation=None 추출은 provider 호출 전에 거부된다.

G-FINAL-1  _run_extraction 프로덕션 호출자 = 준비 소유 경로뿐(레거시 재시도 호출 없음)
G-FINAL-2  WP-C 이전 재시도 컨텍스트는 변이 없이 durable 은퇴
G-FINAL-3  은퇴 저장 실패 → 성공 보고 없음, 영속 pending/컨텍스트 유지, 변이 없음
G-FINAL-4  _apply_extraction_plan 프로덕션 정본 호출자 = 권위 커밋 경로 하나
G-FINAL-5  mode=wp_c_preparation 재시도는 기존 준비/커밋/복구 경로 그대로
"""
from __future__ import annotations

import ast
import copy
import json
import os

import pytest

import core
from core import cost_ledger as CL
from core import turn_preparation as tp
from tests.conftest import source_of

pytestmark = pytest.mark.policy


def _owners(rel: str, func_name: str) -> set:
    """rel 파일에서 func_name 호출을 감싸는 (가장 안쪽) 함수 이름 집합."""
    tree = ast.parse(source_of(rel))
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and (getattr(n.func, "attr", None)
                                        or getattr(n.func, "id", None)) == func_name:
            best = max((f for f in funcs if f.lineno <= n.lineno <= (f.end_lineno or 0)),
                       key=lambda f: f.lineno, default=None)
            out.add(best.name if best else "<module>")
    return out


def _production_owners(func_name: str) -> dict:
    from tests.conftest import REPO_ROOT
    found = {}
    for base in ("cogs", "core"):
        for dp, _d, fs in os.walk(os.path.join(REPO_ROOT, base)):
            if "__pycache__" in dp:
                continue
            for f in fs:
                if f.endswith(".py"):
                    rel = os.path.relpath(os.path.join(dp, f), REPO_ROOT).replace("\\", "/")
                    owners = _owners(rel, func_name)
                    if owners:
                        found[rel] = owners
    return found


# ── G-FINAL-1 ──────────────────────────────────────────────────

def test_gfinal1_run_extraction_production_callers_are_preparation_owned():
    assert _production_owners("_run_extraction") == {"cogs/gm.py": {"_prepare_extraction"}}
    gm = source_of("cogs/gm.py")
    view_src = gm[gm.index("class ExtractionRetryView"):gm.index("class GMRollView")]
    assert "_run_extraction" not in view_src          # 레거시 재시도 호출 없음


async def test_gfinal1b_non_prepared_extraction_is_refused_before_provider(
        wired_bot, session_auto_ready, master_channel):
    import cogs.gm as gm_mod
    s = session_auto_ready
    wired_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = wired_bot
    calls = {"n": 0}

    async def _cwr(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("provider must not be called")
    mp = pytest.MonkeyPatch()
    mp.setattr(core, "call_with_retry", _cwr)
    before = tp.canonical_fingerprint(s)
    try:
        with pytest.raises(RuntimeError):
            await cog._run_extraction(s, "묘사", master_channel)
    finally:
        mp.undo()
    assert calls["n"] == 0
    assert wired_bot.cost_ledger.list_cost_events_strict() == []
    assert tp.canonical_fingerprint(s) == before


# ── G-FINAL-2 / 3 공통 ────────────────────────────────────────

def _rig(wired_bot, session):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = wired_bot
    cog._session_locks = {}
    wired_bot.add_cog_stub("GMCog", cog)
    wired_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    session.resources = {"테스터": {"물": 1}}
    session.statuses = {"테스터": ["지침"]}
    session.companions = ["임성진"]
    session.world_timeline = {"current_location": "마을", "time_of_day": "낮"}
    session.extraction_pending = True
    session.extraction_retry_ctx = {"text": "구버전 묘사: 테스터가 물을 마시고 숲으로 간다."}
    return gm_mod, cog


def _disk(session):
    with open(os.path.join("sessions", session.session_id, "data.json"), encoding="utf-8") as f:
        return json.load(f)


def _spies(cog, mp):
    seen = {"run": 0, "apply": 0, "provider": 0}

    async def _run(*_a, **_k):
        seen["run"] += 1
    async def _apply(*_a, **_k):
        seen["apply"] += 1
    async def _cwr(*_a, **_k):
        seen["provider"] += 1
        raise AssertionError("provider must not be called")
    mp.setattr(cog, "_run_extraction", _run, raising=False)
    mp.setattr(cog, "_apply_extraction_plan", _apply, raising=False)
    mp.setattr(core, "call_with_retry", _cwr)
    return seen


# ── G-FINAL-2 ──────────────────────────────────────────────────

async def test_gfinal2_legacy_retry_context_is_retired_without_mutation(
        wired_bot, session_auto_ready, game_channel, master_channel):
    from tests.fakes.discord_fakes import FakeInteraction, FakeMessage
    s = session_auto_ready
    gm_mod, cog = _rig(wired_bot, s)
    await core.save_session_data_strict(wired_bot, s)            # 구세션이 디스크에 pending 상태
    assert _disk(s)["extraction_pending"] is True

    mp = pytest.MonkeyPatch()
    seen = _spies(cog, mp)
    notices = []

    async def _close(_inter, text, **_k):
        notices.append(text)
    mp.setattr(core.display, "close_notice", _close)
    before_fp = tp.canonical_fingerprint(s)
    before_ink = int(s.total_ink_spent or 0)
    try:
        view = gm_mod.ExtractionRetryView(wired_bot)
        inter = FakeInteraction(channel=game_channel, message=FakeMessage(channel=game_channel))
        await view.retry.callback(inter)
    finally:
        mp.undo()

    assert seen == {"run": 0, "apply": 0, "provider": 0}        # 재추출·적용·provider 0
    assert tp.canonical_fingerprint(s) == before_fp              # 정본 변화 0
    assert s.resources == {"테스터": {"물": 1}} and s.statuses == {"테스터": ["지침"]}
    assert s.companions == ["임성진"] and s.world_timeline["current_location"] == "마을"
    assert int(s.total_ink_spent or 0) == before_ink             # 재무 효과 0
    assert not os.path.isdir("accounts") or os.listdir("accounts") == []
    assert wired_bot.cost_ledger.list_cost_events_strict() == []  # 새 CostEvent 0
    # durable 은퇴
    assert s.extraction_pending is False and s.extraction_retry_ctx == {}
    d = _disk(s)
    assert d["extraction_pending"] is False and d["extraction_retry_ctx"] == {}
    assert notices and "소급 변경하지 않았습니다" in notices[-1]
    assert any("WP-C 이전 형식" in (t or "") for t in master_channel.texts)


# ── G-FINAL-3 ──────────────────────────────────────────────────

async def test_gfinal3_retirement_persistence_failure_keeps_block(
        wired_bot, session_auto_ready, game_channel):
    from tests.fakes.discord_fakes import FakeInteraction, FakeMessage
    s = session_auto_ready
    gm_mod, cog = _rig(wired_bot, s)
    await core.save_session_data_strict(wired_bot, s)
    ctx_before = copy.deepcopy(s.extraction_retry_ctx)

    mp = pytest.MonkeyPatch()
    seen = _spies(cog, mp)
    notices = []

    async def _close(_inter, text, **_k):
        notices.append(text)

    async def _boom(*_a, **_k):
        raise core.SessionPersistenceError("쓰기 실패(테스트)")
    mp.setattr(core.display, "close_notice", _close)
    mp.setattr(core.io, "save_session_data_strict", _boom)   # 커밋 owner 모듈이 쓰는 strict 저장
    before_fp = tp.canonical_fingerprint(s)
    try:
        view = gm_mod.ExtractionRetryView(wired_bot)
        inter = FakeInteraction(channel=game_channel, message=FakeMessage(channel=game_channel))
        await view.retry.callback(inter)
    finally:
        mp.undo()

    assert notices == []                                         # 성공 알림 없음
    followups = [x.get("content") or "" for x in inter.followup.sent]
    assert followups and "실패" in followups[-1] and "차단은 유지" in followups[-1]
    assert s.extraction_pending is True and s.extraction_retry_ctx == ctx_before  # 메모리 해제 없음
    d = _disk(s)
    assert d["extraction_pending"] is True and d["extraction_retry_ctx"] == ctx_before
    assert seen == {"run": 0, "apply": 0, "provider": 0}
    assert tp.canonical_fingerprint(s) == before_fp


# ── G-FINAL-4 ──────────────────────────────────────────────────

def test_gfinal4_canonical_extraction_apply_owner_is_commit_path_only():
    assert _production_owners("_apply_extraction_plan") == {
        "cogs/gm.py": {"_apply_prepared_extraction"}}
    assert _production_owners("_apply_prepared_extraction") == {
        "cogs/gm.py": {"_apply_commit_effects"}}
    gm = source_of("cogs/gm.py")
    # _apply_commit_effects는 CommitCoordinator에 apply_effects 콜백으로만 넘겨진다.
    refs = [ln for ln in gm.splitlines() if "_apply_commit_effects" in ln and "def " not in ln]
    assert refs and all("apply_effects=self._apply_commit_effects" in ln for ln in refs), refs
    # _run_extraction 본문은 어떤 정본 적용도 부르지 않는다(스테이징만).
    body = gm[gm.index("    async def _run_extraction("):gm.index("    async def _verify_proceed_instruction(")]
    assert "_apply_extraction_plan(" not in body and "_apply_prepared_extraction(" not in body
    assert "preparation.extraction_plan = plan" in body


# ── G-FINAL-5 ──────────────────────────────────────────────────

def test_gfinal5_current_prepared_retry_route_unchanged():
    gm = source_of("cogs/gm.py")
    view_src = gm[gm.index("class ExtractionRetryView"):gm.index("class GMRollView")]
    i = view_src.index("_ctx.get(\"mode\") == _RETRY_MODE_PREPARATION")
    j = view_src.index("_retire_legacy_extraction_retry")
    assert i < j                                                   # 현재 형식은 먼저 준비 경로로
    assert "await cog._retry_prepared_extraction(session)" in view_src
    # 행동 증명은 tests/policy/test_ready_barrier.py::test_retry_button_resumes_same_transaction,
    # test_tc12_*, test_tc12b_*, tests/policy/test_commit_recovery.py::test_r_retry_pending_* 가 담당.
