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


def test_d003_instruction_effects_are_staged_not_applied_inside_logic():
    """WP-B 구조 — 지시효과 적용이 _call_gm_logic 밖으로 나갔다(스테이징).

    _call_gm_logic은 더 이상 apply_choice/_update_info_ledger로 canonical을 직접
    바꾸지 않는다. 호출부 루프가 turn_preparation에 스테이징하고, 묘사가 성립한 뒤
    단일 호환 적용 경계에서만 반영한다(AUD-024 누출 차단 / AUD-005 단일 owner).
    """
    import ast

    from tests.conftest import source_of

    src = source_of("cogs/gm.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_call_gm_logic")
    body = "\n".join(src.splitlines()[fn.lineno - 1:fn.end_lineno])

    # 지시층위 안에서 canonical을 직접 여는 호출이 사라졌다.
    assert "apply_choice" not in body, (
        "지시층위가 여전히 canonical 퀘스트를 직접 연다 — 스테이징으로 이동 필요")
    assert "_update_info_ledger" not in body, (
        "지시층위가 여전히 info_ledger를 직접 병합한다")
    # 스테이징 진입점이 배선되어 있다.
    assert "stage_instruction_effects" in src


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


async def test_d003c_failed_narration_does_not_apply_staged_quest(
        monkeypatch, wired_bot, session_with_quest, recorder, master_channel):
    """WP-B 계약 — 지시효과를 스테이징한 뒤 묘사가 실패하면 canonical 무변화.

    (기존 롤백 모델을 스테이징 모델로 재작성 — §36. 애초에 canonical을 바꾸지
    않으므로 되돌릴 것도 없다.)
    """
    import core

    sess = session_with_quest
    cog = _make_gm_cog(wired_bot)

    core.quest.get_state(sess)["active"] = None
    sess._quest_offered = ["q_new"]
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    tx = core.turn_transaction.begin_turn_transaction(sess, "지시문")
    pre_turn = copy.deepcopy(core.capture_state(sess))

    # 지시층위 결정을 스테이징한다(canonical 미변경).
    core.turn_preparation.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "테스트"}},
        transaction_id=tx.transaction_id)
    assert core.capture_state(sess) == pre_turn, "스테이징만으로 상태가 바뀌었습니다"

    # 묘사층위가 실패한다.
    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result=None),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, transaction_id=tx.transaction_id)

    post = core.capture_state(sess)
    assert post == pre_turn, (
        "묘사 실패로 턴이 성립하지 않았는데 스테이징 퀘스트가 적용되었습니다")
    assert core.quest.get_state(sess)["active"] is None


async def test_d003c2_successful_narration_applies_staged_quest(
        monkeypatch, wired_bot, session_with_quest, recorder, master_channel):
    """WP-B 계약 — 묘사가 성립하면 스테이징된 퀘스트가 canonical에 반영된다."""
    import core

    sess = session_with_quest
    cog = _make_gm_cog(wired_bot)

    core.quest.get_state(sess)["active"] = None
    sess._quest_offered = ["q_new"]
    sess.last_recorded_turn = 999   # 되감기/잉크 기록 블록 skip
    sess.gm_active = False          # _start_round 재호출 방지
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    tx = core.turn_transaction.begin_turn_transaction(sess, "지시문")
    core.turn_preparation.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "테스트"}},
        transaction_id=tx.transaction_id)
    assert core.quest.get_state(sess)["active"] is None  # 적용 전

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result="ok"),
                        raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)
    monkeypatch.setattr(core, "refresh_display",
                        recorder.make("refresh"), raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, transaction_id=tx.transaction_id)

    assert core.quest.get_state(sess)["active"] is not None, (
        "묘사가 성립했는데 스테이징 퀘스트가 적용되지 않았습니다")
    assert core.quest.get_state(sess)["active"]["id"] == "q_new"


async def test_d003d_failed_narration_does_not_apply_staged_info_ledger(
        monkeypatch, wired_bot, session_auto_ready, recorder, master_channel):
    """WP-B 계약 — info_ledger 변이도 묘사 성립 전에는 확정되지 않는다."""
    import core

    sess = session_auto_ready
    sess.info_ledger = []
    cog = _make_gm_cog(wired_bot)

    tx = core.turn_transaction.begin_turn_transaction(sess, "지시문")
    pre_turn = copy.deepcopy(core.capture_state(sess))

    # 지시층위 info_access를 스테이징한다(canonical 미변경).
    core.turn_preparation.stage_instruction_effects(
        sess, {"info_access": {"new_secrets": [
            {"info": "비밀1", "known_by": ["테스터"]}]}},
        transaction_id=tx.transaction_id)
    assert sess.info_ledger == [], "스테이징만으로 info_ledger가 바뀌었습니다"

    monkeypatch.setattr(cog, "_dispatch_proceed",
                        recorder.make("dispatch_proceed", result=None),
                        raising=False)
    monkeypatch.setattr(cog, "_start_round",
                        recorder.make("start_round"), raising=False)
    monkeypatch.setattr(core, "save_session_data",
                        recorder.make("save_session"), raising=False)

    await cog._finish_proceed_and_continue(
        sess, "지시문", master_channel, transaction_id=tx.transaction_id)

    post = core.capture_state(sess)
    assert post == pre_turn, "실패한 턴의 info_ledger 스테이징이 적용되었습니다"
    assert sess.info_ledger == []
