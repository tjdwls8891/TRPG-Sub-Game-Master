"""C-001 ~ C-003 — 자동 턴 오케스트레이션 특성화.

WP00_EXECUTABLE_TEST_PLAN.md §5 기준.

이 파일은 **현재 동작을 고정**한다. 바람직한 동작이 아니다.
WP-09가 커밋 파이프라인으로 이행하면 C-001은 의도적으로 바뀐다.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import source_of

pytestmark = pytest.mark.characterize


def _make_gm_cog(fake_bot):
    """GMCog 인스턴스를 생성자 부작용 없이 만든다."""
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


# ──────────────────────────────────────────────────────────
# C-001 현재 _finish_proceed_and_continue 순서
# ──────────────────────────────────────────────────────────

async def test_c001_finish_proceed_current_order(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """C-001 — 정상 PROCEED 후처리의 현재 호출 순서를 고정한다.

    WP-09 커밋 파이프라인 이행 시 이 테스트는 의도적으로 바뀐다.
    """
    import core
    import cogs.gm as gm_mod

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result="묘사 결과"),
                        raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)

    monkeypatch.setattr(core, "capture_state",
                        recorder.make_sync("capture_state", result=lambda: {}),
                        raising=False)
    monkeypatch.setattr(core, "diff_state",
                        recorder.make_sync("diff_state", result=lambda: []),
                        raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core, "record_full_log",
                        recorder.make_sync("record_full_log"), raising=False)
    monkeypatch.setattr(core, "serialize_log_entries",
                        lambda *a, **k: [], raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh_display"), raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink", result={"ok": True}),
                        raising=False)
    monkeypatch.setattr(core.stats, "bump", recorder.make("stats_bump"),
                        raising=False)
    monkeypatch.setattr(gm_mod.GMCog, "_dispatch_proceed",
                        cog._dispatch_proceed, raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")

    order = recorder.order
    # 현재 관측되는 순서 — 이 목록 자체가 특성화 대상이다.
    assert "dispatch_proceed" in order
    assert order.index("dispatch_proceed") < order.index("narrative_progress")
    assert order.index("narrative_progress") < order.index("record_delta")
    assert order.index("deduct_ink") < order.index("record_delta")
    assert order.index("record_delta") < order.index("save_session")
    assert order.index("save_session") < order.index("refresh_display")
    assert order.index("refresh_display") < order.index("start_round")

    # 카운터는 dispatch 성공 후에만 오른다.
    assert sess.gm_turns_done == 1
    assert sess.gm_clarify_count == 0
    assert sess.gm_narrate_count == 0


async def test_c001b_failed_dispatch_does_not_count_turn(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """특성화 — 묘사 실패(None)면 턴을 성립시키지 않는다.

    v5.33.0은 이 경우 카운터·델타를 올리지 않고 저장 후 다음 라운드로 간다.
    다만 지시층위가 이미 남긴 부작용은 되돌리지 않는다(D-003 참조).
    """
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result=None),
                        raising=False)
    monkeypatch.setattr(cog, "_update_narrative_progress",
                        recorder.make("narrative_progress"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "record_delta",
                        recorder.make_sync("record_delta"), raising=False)
    monkeypatch.setattr(core.accounts, "deduct_ink",
                        recorder.make("deduct_ink"), raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, event_assessment="진행됨")

    assert sess.gm_turns_done == 0
    assert "record_delta" not in recorder.order
    assert "deduct_ink" not in recorder.order
    assert "narrative_progress" not in recorder.order
    assert "save_session" in recorder.order
    assert "start_round" in recorder.order


# ──────────────────────────────────────────────────────────
# C-002 ASK/NARRATE는 정규 턴 종료가 아니다
# ──────────────────────────────────────────────────────────

def _judgment_source(action: str):
    """판단층위 응답을 고정한 소스."""
    return {
        "action": action,
        "bridge_message": "어떻게 하시겠습니까?",
        "narrate_instruction": "주변을 묘사하십시오.",
        "reasoning": "테스트",
    }


async def test_c002_ask_is_not_a_canonical_turn_exit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """C-002 — ASK는 _finish_proceed_and_continue를 거치지 않고 break한다.

    WP-01이 요구하는 생명주기 사실 — 하나의 논리적 트랜잭션이 여러 번의
    플레이어/GM 교환에 걸칠 수 있다.
    """
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    monkeypatch.setattr(cog, "_call_judgment",
                        recorder.make("judgment",
                                      result=lambda: _judgment_source("ASK")),
                        raising=False)
    monkeypatch.setattr(cog, "_simulate_narrative_directions",
                        recorder.make("simulate", result=None), raising=False)
    monkeypatch.setattr(cog, "_call_gm_logic",
                        recorder.make("gm_logic", result=None), raising=False)
    monkeypatch.setattr(cog, "_finish_proceed_and_continue",
                        recorder.make("finish_proceed"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "stream_text_to_channel",
                        recorder.make("stream"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "estimate_turn",
                        lambda *a, **k: {"max_ink": 0, "min_ink": 0},
                        raising=False)
    monkeypatch.setattr(core.accounts, "get_balance", lambda *a, **k: 10_000,
                        raising=False)

    turns_before = sess.gm_turns_done
    await cog._run_gm_logic_loop(sess, "주변을 둘러본다", master_channel)

    assert "finish_proceed" not in recorder.order, (
        "ASK가 정규 턴 종료 경로를 탔습니다 — WP-01 전제가 깨집니다")
    assert sess.gm_turns_done == turns_before
    # ASK 브리지는 다음 지시층위 호출의 맥락으로 남는다.
    assert any("진행자 (GM)" in l for l in sess.current_turn_logs)
    assert sess.gm_clarify_count == 1


async def test_c002b_narrate_is_not_a_canonical_turn_exit(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """C-002 — NARRATE도 같은 성질을 갖는다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    monkeypatch.setattr(cog, "_call_judgment",
                        recorder.make("judgment",
                                      result=lambda: _judgment_source("NARRATE")),
                        raising=False)
    monkeypatch.setattr(cog, "_simulate_narrative_directions",
                        recorder.make("simulate", result=None), raising=False)
    monkeypatch.setattr(cog, "_call_gm_logic",
                        recorder.make("gm_logic",
                                      result=lambda: {"narrate_instruction": "묘사"}),
                        raising=False)
    monkeypatch.setattr(cog, "_dispatch_narrate",
                        recorder.make("dispatch_narrate", result="짧은 묘사"),
                        raising=False)
    monkeypatch.setattr(cog, "_finish_proceed_and_continue",
                        recorder.make("finish_proceed"), raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "estimate_turn",
                        lambda *a, **k: {"max_ink": 0, "min_ink": 0},
                        raising=False)
    monkeypatch.setattr(core.accounts, "get_balance", lambda *a, **k: 10_000,
                        raising=False)

    turns_before = sess.gm_turns_done
    await cog._run_gm_logic_loop(sess, "주변을 둘러본다", master_channel)

    assert "finish_proceed" not in recorder.order
    assert "dispatch_narrate" in recorder.order
    assert sess.gm_turns_done == turns_before
    assert sess.gm_narrate_count == 1


async def test_c002c_ask_limit_forces_canonical_proceed(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """특성화 — ASK 한도를 넘기면 강제 PROCEED로 전환된다.

    C-002의 예외 경로다. 여기서만 _finish_proceed_and_continue가 불린다.
    """
    import core
    import cogs.gm as gm_mod

    sess = session_auto_ready
    sess.gm_clarify_count = gm_mod.MAX_CLARIFY_PER_MESSAGE
    cog = _make_gm_cog(wired_bot)

    monkeypatch.setattr(cog, "_call_judgment",
                        recorder.make("judgment",
                                      result=lambda: _judgment_source("ASK")),
                        raising=False)
    monkeypatch.setattr(cog, "_simulate_narrative_directions",
                        recorder.make("simulate", result=None), raising=False)
    monkeypatch.setattr(cog, "_forced_proceed_instruction",
                        recorder.make("forced_instr", result="강제 지시"),
                        raising=False)
    monkeypatch.setattr(cog, "_finish_proceed_and_continue",
                        recorder.make("finish_proceed"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "estimate_turn",
                        lambda *a, **k: {"max_ink": 0, "min_ink": 0},
                        raising=False)
    monkeypatch.setattr(core.accounts, "get_balance", lambda *a, **k: 10_000,
                        raising=False)

    await cog._run_gm_logic_loop(sess, "또 물어본다", master_channel)

    assert "finish_proceed" in recorder.order
    assert "forced_instr" in recorder.order


# ──────────────────────────────────────────────────────────
# C-003 ROLL 재개는 View 경계를 넘는 비동기다
# ──────────────────────────────────────────────────────────

def test_c003_roll_view_holds_continuation_reference():
    """C-003 — GMRollView가 재개 지점을 들고 있다.

    ROLL은 _run_gm_logic_loop가 반환된 뒤 버튼 콜백에서 재개된다.
    WP-01은 이 경계를 넘어 트랜잭션 ID를 운반해야 한다.
    """
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)

    view = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "GMRollView"), None)
    assert view is not None, "GMRollView가 없습니다"

    body = "\n".join(src.splitlines()[view.lineno - 1:view.end_lineno])

    # 재개는 View 내부에서 태스크로 스케줄된다.
    assert "_continue_with_roll_results" in body, (
        "GMRollView가 재개 지점을 참조하지 않습니다")
    assert "create_task" in body, (
        "ROLL 재개가 동기 호출이면 식별자 전파 경계가 달라집니다")

    # WP-01: View는 이제 transaction_id를 들고 async UI 경계를 넘는다.
    # (WP-00 시점에는 부재를 단언했고, WP-01이 이 경계 운반을 배선하면서 뒤집힌다.)
    assert "transaction_id" in body, (
        "GMRollView가 transaction_id를 운반하지 않습니다 — WP-01 식별자 전파가 끊겼습니다")


def test_c003b_continuation_is_reached_after_loop_returns():
    """C-003 — 재개 함수가 루프 바깥에서 finish 경로에 도달한다."""
    import ast

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_continue_with_roll_results"), None)
    assert fn is not None
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])
    assert "_finish_proceed_and_continue" in body, (
        "ROLL 재개가 정규 턴 종료에 닿지 않습니다")
