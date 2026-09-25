"""D-002 — 커밋 배리어 부재 (AUD-012 / AUD-019) — WP-C에서 배리어 측 해소.

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

추출층위는 의도적으로 묘사 스트리밍과 겹쳐 돈다(설계 의도). 결함은
동시성 자체가 아니라 **커밋 배리어가 없다**는 점이었다(추출이 끝나기 전에
다음 라운드가 열리고, 턴 델타·잉크 차감이 확정됨).

WP-C 해소 증거(strict xfail → PASS 전환):
    · d002c — READY 배리어 이전에는 다음 라운드가 열리지 않는다(AUD-012 배리어 측,
      AUD-019 배리어 전제). 최종 durable unlock 권위는 WP-D.
    · d002d — 이번 턴 추출 결과는 READY 이후 legacy continuation에서 델타 기록 '전'에
      적용된다(배리어/순서 측 증거). committed-turn rewind 권위 전체(AUD-020)는 WP-E.
    · d002e — 사전 READY 시스템 실패(추출 소진)는 성공 턴 차감을 호출하지 않는다.
      과거 메타데이터가 AUD-019로 잘못 매핑돼 있었다(PF-22). 올바른 연결은
      AUD-030(시스템 실패 청구 0 정책) 및 AUD-031(권위 결과 이전 차감)이다.
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


def test_d002_extraction_is_registered_and_joined():
    """현재 구현(WP-C) — 추출은 등록 준비 작업으로 발사되고 배리어가 합류한다.

    fire-and-forget create_task(_run_extraction)는 사라졌고, 디스패치는 추출을
    await하지도 않는다(직렬화 금지 — 전달과 겹친다).
    """
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)

    def body_of(name):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == name)
        return "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])

    launch = body_of("_launch_concurrent_preparation")
    assert "register_task" in launch and "_prepare_extraction" in launch, (
        "추출이 등록 준비 작업으로 발사되지 않습니다")
    dispatch = body_of("_dispatch_proceed")
    assert "await self._run_extraction" not in dispatch, "추출을 직렬 await합니다"
    assert "create_task" not in dispatch, "디스패치에 미등록 fire-and-forget이 남았습니다"
    assert "create_task(\n                self._run_extraction" not in src
    owner = body_of("_finish_proceed_and_continue")
    assert owner.index("_join_and_ready") < owner.index("_post_ready_legacy_continuation")
    assert owner.index("_post_ready_legacy_continuation") < owner.index("_start_round")


def _patch_common(monkeypatch, recorder):
    import core
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


async def test_d002b_start_round_waits_for_extraction(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """현재 동작(WP-C) — 추출이 끝나기 전에는 다음 라운드가 열리지 않는다(특성화)."""
    from tests.fakes.barrier_fakes import begin_tx, make_prepared_dispatch

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    tx = begin_tx(sess)
    release = asyncio.Event()

    async def _blocked_extraction(prep):
        recorder.order.append("extraction_start")
        await release.wait()
        recorder.order.append("extraction_done")
        import core
        prep.extraction_plan = core.turn_preparation.build_extraction_plan(
            sess, {}, transaction_id=prep.transaction_id,
            logical_turn=prep.logical_turn, attempt=prep.attempt)
        return prep.extraction_plan

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        make_prepared_dispatch(extraction_coro=_blocked_extraction),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    _patch_common(monkeypatch, recorder)

    owner = asyncio.create_task(cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨",
        transaction_id=tx.transaction_id))
    for _ in range(5):
        await asyncio.sleep(0)

    assert "extraction_start" in recorder.order
    assert "extraction_done" not in recorder.order
    assert "start_round" not in recorder.order, "추출 완료 전 다음 라운드가 열렸습니다"
    assert not owner.done()

    release.set()
    await owner
    assert recorder.index("extraction_done") < recorder.index("start_round")


async def test_d002c_no_next_round_before_commit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """AUD-012/AUD-019(배리어 측) 해소 — READY 이전에는 다음 라운드가 열리지 않는다.

    (strict xfail → PASS 전환. 최종 durable unlock 권위는 WP-D.)
    """
    import core
    from tests.fakes.barrier_fakes import begin_tx, make_prepared_dispatch

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    tx = begin_tx(sess)
    release = asyncio.Event()

    async def _blocked_extraction(prep):
        recorder.order.append("extraction_start")
        await release.wait()
        recorder.order.append("extraction_done")
        prep.extraction_plan = core.turn_preparation.build_extraction_plan(
            sess, {}, transaction_id=prep.transaction_id,
            logical_turn=prep.logical_turn, attempt=prep.attempt)
        return prep.extraction_plan

    _orig_ready = core.turn_preparation.transition_to_ready

    def _ready(session, prep):
        recorder.order.append("ready_to_commit")
        return _orig_ready(session, prep)
    monkeypatch.setattr(core.turn_preparation, "transition_to_ready", _ready)
    monkeypatch.setattr(cog, "_dispatch_proceed",
                        make_prepared_dispatch(extraction_coro=_blocked_extraction),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    _patch_common(monkeypatch, recorder)

    owner = asyncio.create_task(cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨",
        transaction_id=tx.transaction_id))
    await asyncio.sleep(0.01)
    assert "start_round" not in recorder.order
    release.set()
    await owner

    assert recorder.index("extraction_done") < recorder.index("ready_to_commit")
    assert recorder.index("ready_to_commit") < recorder.index("start_round"), (
        "커밋 배리어 없이 다음 라운드가 열렸습니다")


async def test_d002d_delta_recorded_after_extraction_commit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """AUD-012(배리어/순서 측) 해소 — 이번 턴 추출 결과가 적용된 뒤 델타를 기록한다.

    (strict xfail → PASS 전환. rewind 구현 불변. AUD-020 전체는 WP-E에 남는다.)
    """
    from tests.fakes.barrier_fakes import begin_tx, make_prepared_dispatch

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    tx = begin_tx(sess)

    _orig_apply = cog._apply_extraction_plan

    async def _rec_apply(session, plan, master_ch=None):
        recorder.order.append("extraction_applied")
        return await _orig_apply(session, plan, master_ch)

    monkeypatch.setattr(cog, "_apply_extraction_plan", _rec_apply, raising=False)
    monkeypatch.setattr(cog, "_dispatch_proceed",
                        make_prepared_dispatch(
                            extraction_result={"location": {"name": "숲길"}}),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    _patch_common(monkeypatch, recorder)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨",
        transaction_id=tx.transaction_id)

    assert "extraction_applied" in recorder.order, "추출이 델타 기록 전에 적용되지 않았습니다"
    assert recorder.index("extraction_applied") < recorder.index("record_delta")


async def test_d002e_failed_attempt_does_not_charge_player(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """시스템 실패(사전 READY 추출 소진) 턴은 성공 턴 차감을 호출하지 않는다.

    매핑 정정(PF-22): 과거 xfail 사유 'AUD-019'는 잘못된 메타데이터다. 이 테스트는
    AUD-030(시스템 실패 시 플레이어 청구 0 정책)과 AUD-031(권위 결과 이전 차감)의
    WP-C 배리어 측 증거다. Settlement 기반 청구 권위 전환은 WP-D.
    """
    import core
    from tests.fakes.barrier_fakes import begin_tx, make_prepared_dispatch

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    tx = begin_tx(sess)
    sess.total_cost = 30.0

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        make_prepared_dispatch(extraction_fails=True,
                                               order=recorder.order),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    _patch_common(monkeypatch, recorder)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨",
        transaction_id=tx.transaction_id)
    await asyncio.sleep(0)

    assert "extraction_failed" in recorder.order
    assert "deduct_ink" not in recorder.order, (
        "시스템 실패 턴인데 플레이어에게 차감했습니다")
    assert "start_round" not in recorder.order, "재시도 대기 중 다음 라운드가 열렸습니다"
    assert sess.extraction_pending is True
    assert sess.turn_count == 7 and sess.gm_turns_done == 0
    assert tx.status != core.turn_transaction.TurnStatus.READY_TO_COMMIT
