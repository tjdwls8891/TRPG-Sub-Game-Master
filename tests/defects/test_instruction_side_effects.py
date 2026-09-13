"""D-003 — 지시층위 부작용 누출 (AUD-024).

WP00_EXECUTABLE_TEST_PLAN.md §6 기준.

지시층위가 퀘스트를 열고 정보를 기록한 뒤 묘사가 실패하면, 턴은
성립하지 않는데 그 변이는 남는다. 커밋 배리어 도입 전까지 유효하다.
"""

from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.defect


def _install_quest(monkeypatch, scenario_id: str, quest: dict) -> None:
    """퀘스트 데이터를 결정적으로 주입한다.

    `_find_quest`가 `load_quest_data(scenario_id)`로 별도 파일에서 읽으므로
    모듈 캐시에 직접 심는다. 파일 IO도 실제 시나리오도 쓰지 않는다.
    """
    import core
    monkeypatch.setitem(core.quest._cache, scenario_id, {"quests": [quest]})


QUEST_NEW = {
    "id": "q_new", "name": "새 사건", "version": "a", "line": "sub",
    "filters": {}, "tree": {"root": {"guide": "시작", "cases": {}}},
}


def _make_gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


def test_d003_quest_apply_happens_inside_instruction_layer():
    """현재 구조 — 퀘스트 적용이 지시층위 호출 안에서 일어난다.

    묘사 성공 여부와 무관하게 이 시점에 상태가 바뀐다.
    """
    import ast

    from tests.conftest import source_of

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_call_gm_logic")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])

    assert "apply_choice" in body, (
        "지시층위가 퀘스트를 여는 지점이 사라졌습니다 — AUD-024 상태 재확인 필요")


async def test_d003b_quest_state_mutates_before_narration(
        monkeypatch, wired_bot, session_with_quest):
    """현재 동작 — apply_choice가 즉시 세션 상태를 바꾼다.

    특성화. 이 변이는 나중에 묘사가 실패해도 되돌아가지 않는다.
    """
    import core

    sess = session_with_quest
    # 진행 중인 퀘스트를 비워 선택 가능 상황을 만든다.
    core.quest.get_state(sess)["active"] = None
    sess._quest_offered = ["q_new"]
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    before = copy.deepcopy(core.capture_state(sess))
    res = core.quest.apply_choice(sess, {"id": "q_new", "reason": "테스트"})
    after = core.capture_state(sess)

    assert res["applied"] is True
    assert before != after, "퀘스트 적용이 상태를 바꾸지 않았습니다"
    assert core.quest.get_state(sess)["active"]["id"] == "q_new"


@pytest.mark.xfail(strict=True,
                   reason="AUD-024 묘사 실패 시 지시층위 변이가 롤백되지 않는다")
async def test_d003c_failed_narration_rolls_back_instruction_effects(
        monkeypatch, wired_bot, session_with_quest, recorder, master_channel):
    """바람직한 동작 — 묘사가 실패하면 턴 이전 상태로 되돌아가야 한다."""
    import core

    sess = session_with_quest
    cog = _make_gm_cog(wired_bot)

    core.quest.get_state(sess)["active"] = None
    sess._quest_offered = ["q_new"]
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    pre_turn = copy.deepcopy(core.capture_state(sess))

    # 지시층위가 퀘스트를 연다.
    core.quest.apply_choice(sess, {"id": "q_new", "reason": "테스트"})

    # 묘사층위가 실패한다.
    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result=None),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)

    await cog._finish_proceed_and_continue(sess, "지시문", master_channel)

    post = core.capture_state(sess)
    assert post == pre_turn, (
        "묘사 실패로 턴이 성립하지 않았는데 퀘스트 상태가 남았습니다")


@pytest.mark.xfail(strict=True,
                   reason="AUD-024 정보 원장도 같은 경로로 누출된다")
async def test_d003d_info_ledger_rolls_back_on_failure(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """바람직한 동작 — info_ledger 변이도 커밋 전에는 확정되지 않아야 한다."""
    import core

    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)

    pre_turn = copy.deepcopy(core.capture_state(sess))

    # 지시층위 info_access가 원장에 기록하는 상황을 모사한다.
    ledger = sess.info_ledger
    if isinstance(ledger, dict):
        ledger["비밀1"] = {"known_by": ["테스터"]}
    else:
        ledger.append({"info": "비밀1", "known_by": ["테스터"]})

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result=None),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)

    await cog._finish_proceed_and_continue(sess, "지시문", master_channel)

    post = core.capture_state(sess)
    assert post == pre_turn, "실패한 턴의 정보 원장 변이가 남았습니다"
