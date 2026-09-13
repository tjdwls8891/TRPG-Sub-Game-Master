"""D-002 — 커밋 배리어 부재 (AUD-012 / AUD-019).

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

추출층위는 의도적으로 묘사 스트리밍과 겹쳐 돈다(설계 의도). 결함은
동시성 자체가 아니라 **커밋 배리어가 없다**는 점이다. 추출이 끝나기 전에
다음 라운드가 열리고, 턴 델타·잉크 차감이 확정된다.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import source_of

pytestmark = pytest.mark.defect


def _make_gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


def test_d002_extraction_is_scheduled_not_awaited():
    """현재 구현 — 추출층위가 create_task로 떠나고 대기하지 않는다."""
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_dispatch_proceed")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])

    assert "create_task" in body and "_run_extraction" in body, (
        "추출층위 비동기 스케줄 지점이 사라졌습니다")
    assert "await self._run_extraction" not in body, (
        "추출층위를 await하면 이 결함의 성격이 달라집니다")


async def test_d002b_start_round_precedes_extraction_completion(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """현재 동작 — 추출이 끝나기 전에 다음 라운드가 열린다.

    특성화. 바람직한 동작은 아래 xfail이 기술한다.
    """
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    release = asyncio.Event()

    async def _blocked_extraction():
        recorder.order.append("extraction_start")
        await release.wait()
        recorder.order.append("extraction_done")

    async def _dispatch(session, instruction, *, transaction_id=None):
        recorder.order.append("dispatch_proceed")
        asyncio.create_task(_blocked_extraction())
        await asyncio.sleep(0)      # 추출 태스크가 시작할 틈
        return {"ok": True}

    monkeypatch.setattr(cog, "_dispatch_proceed", _dispatch, raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh_display"), raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core, "record_full_log",
                        recorder.make_sync("record_full_log"), raising=False)
    monkeypatch.setattr(core, "serialize_log_entries",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink", result={"ok": True}),
                        raising=False)
    monkeypatch.setattr(core.stats, "bump", recorder.make("stats_bump"),
                        raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")

    assert "extraction_start" in recorder.order
    assert "extraction_done" not in recorder.order, (
        "추출이 이미 끝났다면 이 시나리오가 성립하지 않습니다")
    assert "start_round" in recorder.order, (
        "현재 구현은 추출 완료를 기다리지 않고 다음 라운드를 연다")

    release.set()
    await asyncio.sleep(0)


@pytest.mark.xfail(strict=True,
                   reason="AUD-012/AUD-019 커밋 배리어가 없다")
async def test_d002c_no_next_round_before_commit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """바람직한 동작 — 추출/커밋이 끝나기 전에는 다음 라운드가 열리지 않는다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    release = asyncio.Event()

    async def _blocked_extraction():
        recorder.order.append("extraction_start")
        await release.wait()
        recorder.order.append("extraction_done")

    async def _dispatch(session, instruction, *, transaction_id=None):
        asyncio.create_task(_blocked_extraction())
        await asyncio.sleep(0)
        return {"ok": True}

    monkeypatch.setattr(cog, "_dispatch_proceed", _dispatch, raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh_display"), raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core, "record_full_log",
                        recorder.make_sync("record_full_log"), raising=False)
    monkeypatch.setattr(core, "serialize_log_entries",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink", result={"ok": True}),
                        raising=False)
    monkeypatch.setattr(core.stats, "bump", recorder.make("stats_bump"),
                        raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")

    if "start_round" in recorder.order:
        assert recorder.index("extraction_done") < recorder.index("start_round"), (
            "커밋 배리어 없이 다음 라운드가 열렸습니다")
    release.set()
    await asyncio.sleep(0)


@pytest.mark.xfail(strict=True,
                   reason="AUD-012 추출 결과 확정 전에 턴 델타가 기록된다")
async def test_d002d_delta_recorded_after_extraction_commit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """바람직한 동작 — 추출 결과가 반영된 뒤 델타를 기록해야 한다.

    현재는 추출이 백그라운드로 도는 중에 델타를 기록하므로, 그 턴의
    추출 결과는 **다음 턴 델타**에 귀속된다(D-004와 같은 뿌리).
    """
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    release = asyncio.Event()

    async def _blocked_extraction():
        await release.wait()
        recorder.order.append("extraction_done")

    async def _dispatch(session, instruction, *, transaction_id=None):
        asyncio.create_task(_blocked_extraction())
        await asyncio.sleep(0)
        return {"ok": True}

    monkeypatch.setattr(cog, "_dispatch_proceed", _dispatch, raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh_display"), raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core, "record_full_log",
                        recorder.make_sync("record_full_log"), raising=False)
    monkeypatch.setattr(core, "serialize_log_entries",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink", result={"ok": True}),
                        raising=False)
    monkeypatch.setattr(core.stats, "bump", recorder.make("stats_bump"),
                        raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")

    assert "extraction_done" in recorder.order, (
        "추출이 델타 기록 전에 끝나지 않았습니다")
    assert recorder.index("extraction_done") < recorder.index("record_delta")
    release.set()
    await asyncio.sleep(0)


@pytest.mark.xfail(strict=True,
                   reason="AUD-019 실패한 시스템 시도가 플레이어에게 청구된다")
async def test_d002e_failed_attempt_does_not_charge_player(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """바람직한 동작 — 시스템 실패 턴은 과금하지 않는다.

    현재는 묘사 실패 시 차감을 건너뛰지만, 묘사 성공 후 추출이 실패해도
    이미 차감이 끝난 상태다. 그 경우를 재현한다.
    """
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    async def _dispatch(session, instruction, *, transaction_id=None):
        # 묘사는 성공하고 비용이 발생한다.
        session.total_cost = 30.0
        # 추출은 즉시 실패한다.
        async def _fail():
            recorder.order.append("extraction_failed")
            raise RuntimeError("추출 실패")
        task = asyncio.create_task(_fail())
        task.add_done_callback(lambda t: t.exception())
        await asyncio.sleep(0)
        return {"ok": True}

    monkeypatch.setattr(cog, "_dispatch_proceed", _dispatch, raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh_display"), raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core, "record_full_log",
                        recorder.make_sync("record_full_log"), raising=False)
    monkeypatch.setattr(core, "serialize_log_entries",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink", result={"ok": True}),
                        raising=False)
    monkeypatch.setattr(core.stats, "bump", recorder.make("stats_bump"),
                        raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")
    await asyncio.sleep(0)

    assert "extraction_failed" in recorder.order
    assert "deduct_ink" not in recorder.order, (
        "시스템 실패 턴인데 플레이어에게 차감했습니다")
