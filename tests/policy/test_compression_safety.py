"""WP-F — 백그라운드 압축 안전성 (C-F01 ~ C-F09 + 동시성).

압축 결과는 출발 시점의 불변 출처(세대·접두 지문)가 여전히 정본일 때만 적용된다.
버려진 결과도 provider 비용(CostEvent)은 사실로 남으며 턴 Settlement 는 건드리지 않는다.
"""

from __future__ import annotations

import asyncio
import os

import pytest

import core
from core import cost_ledger as CL
from cogs.game import GameCog
from tests.fakes.genai_fakes import FakeGenAIResponse, FakeUsageMetadata

pytestmark = pytest.mark.policy

LOGS = [f"[GM 묘사]: 장면 {i}" for i in range(5)]


@pytest.fixture
def cbot(wired_bot):
    wired_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    return wired_bot


@pytest.fixture
def sess(session_auto_ready):
    s = session_auto_ready
    s.uncompressed_logs = list(LOGS)
    s.compressed_memory = ""
    s.turn_count = 10
    s.last_compressed_turn = 5
    s.compression_count = 1
    return s


class Gate:
    """call_with_retry 대역 — 게이트가 열릴 때까지 provider 결과를 붙잡는다."""

    def __init__(self, text="요약 결과", fail=False):
        self.event = asyncio.Event()
        self.text = text
        self.fail = fail
        self.calls = 0

    async def __call__(self, fn, *, on_attempt_result=None, **_kw):
        self.calls += 1
        await self.event.wait()
        if on_attempt_result:
            on_attempt_result(attempt=1, success=not self.fail)
        if self.fail:
            return False, None
        return True, FakeGenAIResponse(self.text, usage=FakeUsageMetadata(prompt=1000, candidates=200))


@pytest.fixture
def gate(monkeypatch):
    g = Gate()
    monkeypatch.setattr(core, "call_with_retry", g)
    return g


def _comp_events(bot):
    return [e for e in bot.cost_ledger.list_cost_events_strict()
            if e["operation"] in (CL.OP_MEMORY_AUTO_COMPRESSION, CL.OP_MEMORY_MANUAL_COMPRESSION)]


async def _start(cog, s):
    src = core.memory_plan.capture_compression_source(s)
    s.is_compressing = True
    task = asyncio.create_task(cog._run_auto_compression(s, src, ""))
    await asyncio.sleep(0)
    return src, task


# ── C-F01 · C-F02a · C-F05 ─────────────────────────────────

async def test_cf01_same_version_applies(cbot, sess, gate):
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    gate.event.set()
    await task
    assert sess.compressed_memory == "요약 결과"
    assert sess.uncompressed_logs == []
    assert sess.is_compressing is False
    assert sess.last_compressed_turn == 10 and sess.compression_count == 2
    assert len(_comp_events(cbot)) == 1


async def test_cf02a_cf05_append_during_compression_applies_and_keeps_newer_logs(cbot, sess, gate):
    """정상 동시 진행: 출발 이후 뒤에 append 된 턴 로그(커밋)는 출처를 무효화하지 않고 보존된다."""
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    sess.uncompressed_logs.extend(["[플레이어 및 GM]: 새 턴", "[GM 묘사]: 새 묘사"])
    gate.event.set()
    await task
    assert sess.compressed_memory == "요약 결과"
    assert sess.uncompressed_logs == ["[플레이어 및 GM]: 새 턴", "[GM 묘사]: 새 묘사"]


# ── C-F02b · C-F05 — 출처 접두가 바뀐 늦은 결과 ────────────

async def test_cf02b_later_change_to_source_discards_and_preserves_logs(cbot, sess, gate):
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    sess.uncompressed_logs[-1] = "[GM 묘사]: 수정된 장면"      # !수정 등으로 출처 변경
    sess.uncompressed_logs.append("[GM 묘사]: 이후 장면")
    before = list(sess.uncompressed_logs)
    gate.event.set()
    await task
    assert sess.compressed_memory == ""
    assert sess.uncompressed_logs == before
    assert sess.last_compressed_turn == 5 and sess.compression_count == 1
    assert "⏭️" in (cbot.get_channel(sess.master_ch_id).texts[-1] or "")


# ── C-F03 · C-F04 — 되감기/재생성 복원 ─────────────────────

async def test_cf03_rewind_is_blocked_while_compressing(cbot, sess, gate):
    """WP-E 보존: 압축 진행 중에는 되감기·재생성 자체가 거부된다."""
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    assert core.turn_history._busy_reason(sess) is not None
    res = await core.turn_history.rewind(cbot, sess, 1)
    assert res["ok"] is False
    gate.event.set()
    await task


@pytest.mark.parametrize("label", ["rewind", "rerender_abort"])
async def test_cf03_cf04_restored_history_rejects_old_result(cbot, sess, gate, label):
    """복원(turn_history.restore_reversible)이 끼면 출발 이력 기반 결과는 적용되지 않는다."""
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    restored = core.turn_history.capture_reversible(sess)   # 동일 내용으로 복원해도
    core.turn_history.restore_reversible(sess, restored)    # 이력 세대가 바뀌면 폐기
    gate.event.set()
    await task
    assert sess.compressed_memory == ""
    assert sess.uncompressed_logs == LOGS
    assert sess.last_compressed_turn == 5
    from core import rewind as RW
    assert RW.read_jsonl(sess.session_id, RW.REWIND_LOG) == []   # 거짓 되감기 델타 없음


# ── C-F06 · C-F07 ──────────────────────────────────────────

async def test_cf06_cf07_cost_survives_discard_and_no_settlement_touch(cbot, sess, gate):
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    core.memory_plan.bump_memory_generation(sess)
    gate.event.set()
    await task
    ev = _comp_events(cbot)
    assert len(ev) == 1 and ev[0]["cost_usd"] > 0
    assert ev[0]["transaction_id"] is None and ev[0]["logical_turn"] is None
    # 턴 Settlement 저장소에 아무것도 쓰지 않는다
    store_dir = os.path.join("sessions", sess.session_id)
    assert not any("settlement" in f for f in os.listdir(store_dir))
    assert sess.compressed_memory == ""


# ── C-F08 — 실패·취소 ──────────────────────────────────────

async def test_cf08_provider_failure_mutates_nothing(cbot, sess, gate):
    gate.fail = True
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    gate.event.set()
    await task
    assert sess.compressed_memory == "" and sess.uncompressed_logs == LOGS
    assert sess.is_compressing is False and _comp_events(cbot) == []


async def test_cf08b_cancellation_mutates_nothing_and_releases_flag(cbot, sess, gate):
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sess.compressed_memory == "" and sess.uncompressed_logs == LOGS
    assert sess.is_compressing is False


# ── C-F09 — 재시작 ────────────────────────────────────────

def test_cf09_runtime_generation_is_not_persisted():
    """세대·진행 표식은 런타임 전용 — 재시작 뒤 늦은 적용 경로(태스크)는 존재하지 않는다."""
    assert "_memory_generation" not in core.SESSION_FIELDS
    assert "is_compressing" not in core.SESSION_FIELDS      # 재시작 시 models 기본값 False
    assert core.TRPGSession("x", 1, 2, "t", {}).is_compressing is False


async def test_cf09b_source_from_previous_process_object_cannot_touch_restored_session(cbot, sess, session_factory):
    src = core.memory_plan.capture_compression_source(sess)
    restored = session_factory(session_id=sess.session_id)
    restored.uncompressed_logs = list(LOGS) + ["[GM 묘사]: 재시작 후"]
    core.memory_plan.bump_memory_generation(restored)       # 복원 경로는 새 객체·새 세대
    res = core.memory_plan.apply_compression_result(restored, src, "늦은 요약")
    assert res["applied"] is False and restored.compressed_memory == ""


# ── 동시성 ────────────────────────────────────────────────

async def test_two_results_from_same_source_apply_once(cbot, sess):
    s1 = core.memory_plan.capture_compression_source(sess)
    s2 = core.memory_plan.capture_compression_source(sess)
    cog = GameCog(cbot)
    r1, r2 = await asyncio.gather(cog._apply_compression(sess, s1, "A"),
                                  cog._apply_compression(sess, s2, "B"))
    assert [r1["applied"], r2["applied"]].count(True) == 1
    assert sess.uncompressed_logs == []                      # 이중 접두 삭제 없음
    assert sess.compressed_memory in ("A", "B")


async def test_manual_compression_refused_while_auto_running(cbot, sess, gate, master_channel):
    from tests.fakes.discord_fakes import FakeInteraction
    cog = GameCog(cbot)
    _src, task = await _start(cog, sess)

    class Ctx:
        channel = master_channel
        async def send(self, content=None, **kw):
            return await master_channel.send(content, **kw)
    await GameCog.compress_memory.callback(cog, Ctx())
    assert "진행 중" in master_channel.texts[-1]
    assert gate.calls == 1
    gate.event.set()
    await task
    assert sess.uncompressed_logs == []


async def test_apply_waits_for_commit_critical_section(cbot, sess):
    src = core.memory_plan.capture_compression_source(sess)
    cog = GameCog(cbot)
    lock = core.commit_coordinator.commit_serialization_lock(sess.session_id)
    await lock.acquire()
    t = asyncio.create_task(cog._apply_compression(sess, src, "요약"))
    await asyncio.sleep(0.01)
    assert sess.compressed_memory == ""                      # 커밋 임계구역 동안 대기
    lock.release()
    res = await t
    assert res["applied"] and sess.compressed_memory == "요약"


def test_legacy_settle_is_branch_independent_source_scan():
    """PF-10: 압축 선결제 비교는 적용 분기와 무관하게 한 경로(_record_compression_settle)."""
    from tests.conftest import source_of
    src = source_of("cogs/game.py")
    assert src.count("core.settle_compression(") == 1
    assert src.count("self." + "_record_compression_settle(") == 2
    assert "del session.uncompressed_logs" not in src        # 적용은 memory_plan 한 곳
