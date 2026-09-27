"""WP-D 게이트 패치 D-D1~D-D4 회귀 테스트.

D-D1 권위 커밋 경로의 정본 변이 예외 전파(헬퍼 내부 catch 경계 직접 검증)
D-D2 FAILED_SYSTEM 정산 영속 실패의 fail-closed(PREPARED 유무 무관)
D-D3 RETRY_PENDING exact 멤버십 — 재시도 provider 호출 crash window · breadcrumb strict 실패
D-D4 복구 admission이 gm_active/턴 한도/비용 한도보다 먼저
"""

from __future__ import annotations

import asyncio
import types

import pytest

import core
from core import commit_coordinator as CC
from core import commit_journal as CJ
from core import turn_transaction as tt
from tests.conftest import PLAYER_UID, source_of
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_ready_barrier import (  # noqa: F401 — 픽스처 재사용
    _canon, _run_turn, _usage, rig,
)
from tests.policy.test_authoritative_commit import (
    _balance, _disk, _fund, _ink_rows, _phases, _save_baseline, _store, _story,
)
from tests.policy.test_commit_recovery import _restart, _sid

pytestmark = pytest.mark.policy


@pytest.fixture
def inject():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def _no_committed(s, tx):
    assert "COMMITTED" not in _phases(s, tx)
    assert all(x.outcome != "COMMITTED" for x in _store(s).list_for_transaction(tx.transaction_id))


# ══════════════════════════════════════════════════════════════
# D-D1 — 헬퍼 내부 정본 변이 예외는 커밋 owner까지 전파된다
# ══════════════════════════════════════════════════════════════

class _Boom(RuntimeError):
    pass


def _mutate_then_raise(mutate):
    def _f(session, *a, **k):
        mutate(session)
        raise _Boom("정본 변이 도중 실패(테스트)")
    return _f


_SITES = {
    # 추출 적용 내부 try/except 경계 — 필드 A를 바꾼 뒤 예외
    "extraction_companions": ("apply_normalized_companions",
                              lambda s: s.companions.append("유령동행")),
    "extraction_quest": ("quest.advance_quest",
                         lambda s: s.quest_state.update({"오염": True})),
    "extraction_bgm": ("select_bgm",
                       lambda s: setattr(s, "last_bgm_situation", ("오염", 9))),
    # 지시효과 적용(이전 _apply_commit_effects의 try/except 경계)
    "instruction_effects": ("turn_preparation.apply_instruction_effects",
                            lambda s: s.info_ledger.append({"info": "오염"})),
}


@pytest.mark.parametrize("site", sorted(_SITES))
async def test_dd1_helper_mutation_exception_rolls_back(rig, inject, site):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    bgm_before = s.last_bgm_situation
    disk_before = _save_baseline(r)
    path, mutate = _SITES[site]
    owner = core
    *mods, attr = path.split(".")
    for m in mods:
        owner = getattr(owner, m)
    inject.setattr(owner, attr, _mutate_then_raise(mutate))
    seen = {}
    orig = CC.CommitCoordinator._fail_pre_persist

    async def _spy(self, session, prep, **k):
        seen.update(k)
        return await orig(self, session, prep, **k)
    inject.setattr(CC.CommitCoordinator, "_fail_pre_persist", _spy)

    tx = await _run_turn(r)
    assert seen.get("stage") == "APPLY" and "_Boom" in (seen.get("error") or ""), \
        "예외가 커밋 owner까지 전파되지 않았습니다"
    assert _canon(s) == start and s.last_bgm_situation == bgm_before   # 스냅샷 복원
    assert _story(_disk(s)) == disk_before                             # 디스크 정본 불변
    _no_committed(s, tx)
    assert _balance(PLAYER_UID) == 100 and _ink_rows(PLAYER_UID) == []
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and tx.failure_stage == "COMMIT_APPLY"


async def test_dd1_irregular_helper_boundary_propagates_only_in_commit_mode(rig, inject):
    """비정규 NPC 적용 내부 catch 경계 — 권위 커밋(derived)에서는 전파, 수동 경로는 기존 best-effort."""
    r = rig
    s = r.sess
    plan = core.turn_preparation.IrregularNpcMutationPlan(
        registrations=({"name": "행인", "image_key": "", "gender": "남", "age": "30대",
                        "context": "", "turn": 1},),
    ) if "registrations" in core.turn_preparation.IrregularNpcMutationPlan.__dataclass_fields__ \
        else None
    if plan is None:
        pytest.skip("IrregularNpcMutationPlan 구조 상이")
    inject.setattr(core.irregular_npc, "note_appearance",
                   _mutate_then_raise(lambda ss: ss.met_npcs.append("오염")))
    with pytest.raises(_Boom):
        await r.gm._apply_irregular_npc_plan(s, plan, "행인이 지나간다", None,
                                             staged_promotions={}, derived=CC.DerivedEffects())
    plan.applied = False
    await r.gm._apply_irregular_npc_plan(s, plan, "행인이 지나간다", None,
                                         staged_promotions={})       # 수동: 삼킴(기존 동작)


async def test_dd1_replan_apply_exception_propagates(rig, inject):
    r = rig
    s = r.sess
    prep = types.SimpleNamespace(
        irregular_plan=None, proceed_history_entry=None, narrative_progress=None,
        narrative_marker=False, replan_candidate=object(), logical_turn=1, attempt=1,
        extraction_plan=None, transaction_id="x")

    async def _bad(session, cand, **k):
        session.narrative_plan = {"오염": True}
        raise _Boom("재계획 적용 실패(테스트)")
    inject.setattr(r.gm, "_commit_replan_candidate", _bad)
    with pytest.raises(_Boom):
        await r.gm._apply_commit_effects(s, prep, CC.DerivedEffects())


def test_dd1_commit_path_has_no_swallowing_boundary():
    """정적 스캔 — 권위 적용 경로의 except는 derived 모드에서 반드시 re-raise한다."""
    import ast
    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not [h for h in ast.walk(fns["_apply_commit_effects"])
                if isinstance(h, ast.ExceptHandler)], "_apply_commit_effects에 catch가 있습니다"
    for name in ("_apply_extraction_plan", "_apply_irregular_npc_plan"):
        for h in ast.walk(fns[name]):
            if isinstance(h, ast.ExceptHandler):
                body = ast.get_source_segment(src, h)
                assert "if derived is not None:" in body and "raise" in body, (name, body[:120])


# ══════════════════════════════════════════════════════════════
# D-D2 — FAILED_SYSTEM 정산 영속 실패는 fail-closed
# ══════════════════════════════════════════════════════════════

def _fail_failed_settlements(inject):
    orig = core.settlement.SettlementStore.record_strict
    state = {"on": True}

    def _bad(self, st):
        if state["on"] and st.outcome == "FAILED_SYSTEM":
            raise core.settlement.SettlementPersistenceError("FAILED 정산 쓰기 실패(테스트)")
        return orig(self, st)
    inject.setattr(core.settlement.SettlementStore, "record_strict", _bad)
    return state


async def test_dd2_pre_ready_failure_with_failed_settlement_failure_blocks(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    state = _fail_failed_settlements(inject)
    r.ev["stream_fail_at"] = 1                               # 전달 실패(사전 READY 종료)
    tx = await _run_turn(r)
    frozen = list(tx.preparation.frozen_cost_event_ids)
    assert frozen and r.rec["start_round"] == 0               # 다음 자동 턴 금지
    cr = s.commit_recovery
    assert cr["stage"] == "FAILED_SETTLEMENT_PENDING" and cr["transaction_id"] == tx.transaction_id
    assert cr["cost_event_ids"] == frozen                     # exact 멤버십 보존
    backlog = _disk(s)["failed_settlement_backlog"]
    assert backlog[0]["cost_event_ids"] == frozen and backlog[0]["attempt"] == tx.attempt
    assert _store(s).find_settlement(_sid(tx)) is None        # 정산된 척하지 않음
    assert _balance(PLAYER_UID) == 100
    # 입력이 와도 차단 유지 → 복구 가능해지면 같은 입력으로 영속 후에야 라운드
    await r.gm._process_actions(s, "다음", r.master)
    assert r.rec["start_round"] == 0 and s.commit_recovery
    state["on"] = False
    await r.gm._process_actions(s, "다음", r.master)
    st = _store(s).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and set(st.included_cost_event_ids) == set(frozen)
    assert s.commit_recovery is None and s.failed_settlement_backlog == []
    assert r.rec["start_round"] == 1 and _balance(PLAYER_UID) == 100


async def test_dd2_ledger_parse_failure_blocks_candidate_and_failed_settlement(rig, inject):
    """전역 원장 strict 파싱 실패 모사 — 후보 COMMITTED와 FAILED_SYSTEM이 모두 막힌다."""
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    start = _canon(s)
    orig = core.cost_ledger.CostLedger.get_cost_events_by_ids_strict
    state = {"on": True}

    def _bad(self, ids, **k):
        if state["on"]:
            raise core.cost_ledger.CostLedgerCorruptionError("원장 손상(테스트)")
        return orig(self, ids, **k)
    inject.setattr(core.cost_ledger.CostLedger, "get_cost_events_by_ids_strict", _bad)
    tx = await _run_turn(r)
    frozen = list(tx.preparation.frozen_cost_event_ids)
    assert "PREPARED" not in _phases(s, tx)                   # PREPARED 이전 실패
    assert _canon(s) == start and r.rec["start_round"] == 0
    assert s.commit_recovery["cost_event_ids"] == frozen
    assert _disk(s)["failed_settlement_backlog"][0]["cost_event_ids"] == frozen
    assert _store(s).find_settlement(_sid(tx)) is None and _balance(PLAYER_UID) == 100
    # 재시작해도 차단은 durable(백로그) — 복구 가능해지면 exact ID로 종결
    bot2, s2 = await _restart(r)
    assert s2.commit_recovery and s2.commit_recovery["status"] == "PENDING"
    state["on"] = False
    bot3, s3 = await _restart(r)
    assert s3.commit_recovery is None and s3.failed_settlement_backlog == []
    st = _store(s3).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and set(st.included_cost_event_ids) == set(frozen)


# ══════════════════════════════════════════════════════════════
# D-D3 — RETRY_PENDING exact 멤버십 crash window · breadcrumb strict 실패
# ══════════════════════════════════════════════════════════════

class _HardCrash(BaseException):
    """프로세스 중단 등가 — except Exception으로 잡히지 않는다."""


async def test_dd3_retry_provider_event_before_breadcrumb_refresh_crash(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]
    tx = await _run_turn(r)
    prep = tx.preparation
    A = list(_disk(s)["extraction_retry_ctx"]["claimed_cost_event_ids"])
    assert A
    # 같은 tx 귀속이지만 멤버가 아닌 수동 이벤트(혼입 금지 대상)
    stray = core.cost_ledger.begin_operation(
        r.bot, "MANUAL_TOOL", session=s, model=core.DEFAULT_MODEL,
        billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
    stray.record(cost_usd=1.0, cost_krw=1400.0)
    # 재시도: provider가 usage를 돌려주지만(=CostEvent B) 결과는 무효 → 다시 실패 →
    #   breadcrumb 갱신 직전 하드 크래시
    r.prov.routes["extraction"] = [FakeGenAIResponse("JSON 아님", usage=_usage(700, 90))]

    async def _crash(*a, **k):
        raise _HardCrash()
    inject.setattr(CC, "persist_retry_breadcrumb", _crash)
    with pytest.raises(_HardCrash):
        await r.gm._retry_prepared_extraction(s)
    B = [eid for op in prep.cost_operations if op.operation == "TURN_EXTRACTION"
         for eid in op.event_ids if eid not in A]
    assert B, "재시도 provider 호출이 CostEvent를 만들지 않았습니다"
    assert _disk(s)["extraction_retry_ctx"]["claimed_cost_event_ids"] == A   # 갱신 전 크래시
    inject.undo()
    bot2, s2 = await _restart(r)
    st = _store(s2).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and st.charge_ink_per_user == 0
    assert set(st.included_cost_event_ids) == set(A) | set(B)          # 정확히 A + B
    assert stray.event_ids[0] not in st.included_cost_event_ids        # 무관 이벤트 제외
    assert _balance(PLAYER_UID) == 100 and s2.extraction_pending is False


async def test_dd3_retry_claim_is_durable_before_provider_call(rig, inject):
    """claim 기록 실패 시 provider 호출 자체가 일어나지 않는다(내구 claim 선행)."""
    r = rig
    s = r.sess
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]
    tx = await _run_turn(r)
    calls_before = r.prov.calls.count("extraction")

    def _bad(*a, **k):
        raise OSError("claim fsync 실패(테스트)")
    inject.setattr(CC, "record_retry_claim", _bad)
    out = await r.gm._retry_prepared_extraction(s)
    assert out == "failed" and r.prov.calls.count("extraction") == calls_before
    assert tx.preparation.phase == core.turn_preparation.PREP_RETRY_PENDING


async def test_dd3_breadcrumb_strict_failure_is_fail_closed(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    r.prov.routes["extraction"] = [RuntimeError("추출 실패")]

    async def _bad(bot, session):
        raise core.io.SessionPersistenceError("strict 저장 실패(테스트)")
    inject.setattr(core.io, "save_session_data_strict", _bad)
    tolerant = []
    _orig_tol = core.io.save_session_data

    async def _tol(bot, session):
        tolerant.append(dict(getattr(session, "extraction_retry_ctx", {}) or {}))
        return await _orig_tol(bot, session)
    inject.setattr(core.io, "save_session_data", _tol)
    tx = await _run_turn(r)
    prep = tx.preparation
    assert prep.phase == core.turn_preparation.PREP_FAILED            # 재시도 상태 아님
    assert not any(getattr(m, "view", None) is not None for m in r.gch.sent), "재시도 UI 제공됨"
    assert s.extraction_pending is False
    assert not any(c.get("claimed_cost_event_ids") for c in tolerant), \
        "tolerant 저장이 breadcrumb 성공을 대신했습니다"
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    st = _store(s).get_settlement_strict(_sid(tx))
    assert st.outcome == "FAILED_SYSTEM" and set(st.included_cost_event_ids) == \
        set(prep.frozen_cost_event_ids)
    assert _balance(PLAYER_UID) == 100


# ══════════════════════════════════════════════════════════════
# D-D4 — 복구 admission 우선
# ══════════════════════════════════════════════════════════════

def _msg(r, content="다음"):
    return types.SimpleNamespace(
        author=types.SimpleNamespace(bot=False, id=int(PLAYER_UID), display_name="테스터"),
        content=content, channel=r.gch)


async def _drain(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    while not pred():
        assert loop.time() - t0 < timeout, "복구 대기 시간 초과"
        await asyncio.sleep(0.01)


async def test_dd4_last_turn_recovery_runs_despite_gm_inactive(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    s.gm_turn_cap = 1                                        # 이번이 마지막 자동 턴

    def _bad(account):
        raise core.accounts.AccountPersistenceError("계정 쓰기 실패(테스트)")
    inject.setattr(core.accounts, "_write_account_strict", _bad)
    tx = await _run_turn(r)
    assert s.gm_active is False and s.commit_recovery
    assert "SESSION_PERSISTED" in _phases(s, tx) and "COMMITTED" not in _phases(s, tx)
    inject.undo()
    await r.gm.on_message(_msg(r))                           # 같은 프로세스의 입력
    await _drain(lambda: s.commit_recovery is None)
    ph = _phases(s, tx)
    assert ph.count("COMMITTED") == 1 and ph.count("BILLING_APPLIED") == 1
    st = _store(s).get_settlement_strict(_sid(tx))
    assert len(_ink_rows(PLAYER_UID)) == 1
    assert _balance(PLAYER_UID) == 100 - st.charge_ink_per_user     # 정확히 한 번
    assert tx.status == tt.TurnStatus.COMMITTED
    assert r.rec["start_round"] == 0 and s.gm_active is False       # 새 라운드 없음


async def test_dd4_cost_cap_blocked_session_still_recovers_first(rig, inject):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    orig = core.settlement.SettlementStore.record_strict
    state = {"on": True}

    def _bad(self, st):
        if state["on"] and st.outcome == "COMMITTED":
            raise core.settlement.SettlementPersistenceError("쓰기 실패(테스트)")
        return orig(self, st)
    inject.setattr(core.settlement.SettlementStore, "record_strict", _bad)
    tx = await _run_turn(r)
    assert s.commit_recovery and r.rec["start_round"] == 0
    state["on"] = False
    s.gm_cost_cap_krw = 0.0                                  # 비용 한도 도달 상태
    s.gm_cost_baseline = 0.0
    await r.gm._process_actions(s, "다음", r.master)         # 선언 경로도 복구 우선
    assert s.commit_recovery is None
    assert _phases(s, tx).count("COMMITTED") == 1 and len(_ink_rows(PLAYER_UID)) == 1
    assert r.rec["start_round"] == 0                         # eligibility 불충족 → 라운드 없음


def test_dd4_admission_order_scan():
    src = source_of("cogs/gm.py")
    om = src[src.index("async def on_message"):src.index("async def _start_round")]
    assert om.index("commit_recovery") < om.index('getattr(session, "gm_active"')
    pa = src[src.index("async def _process_actions"):src.index("async def _finish_proceed_and_continue")]
    assert pa.index("commit_recovery") < pa.index("if not session.gm_active")
    assert pa.index("commit_recovery") < pa.index("gm_turn_cap")
    assert pa.index("commit_recovery") < pa.index("gm_cost_cap_krw")
