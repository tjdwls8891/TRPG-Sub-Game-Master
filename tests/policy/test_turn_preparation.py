"""WP-B — TurnPreparation 지시층위 스테이징 정책 테스트.

증명 대상:
    · 스테이징만으로는 canonical quest_state/info_ledger가 바뀌지 않는다(생산자 순수성).
    · 투영(projection)은 스테이징 효과를 반영하되 canonical은 변경하지 않는다.
    · 단일 호환 적용부는 묘사 성공 후에만 반영하며 이중 적용되지 않는다.
"""

from __future__ import annotations

import copy

import pytest

import core
from core import turn_preparation as tp

pytestmark = pytest.mark.policy


QUEST_NEW = {
    "id": "q_new", "name": "새 사건", "version": "a", "line": "sub",
    "filters": {}, "tree": {"root": {"guide": "시작", "cases": {}}},
}


def _install_quest(monkeypatch, scenario_id, quest):
    monkeypatch.setitem(core.quest._cache, scenario_id, {"quests": [quest]})


def _selectable(session):
    """선택 가능 상황(진행 중 퀘스트 없음 + 후보 제시)을 만든다."""
    core.quest.get_state(session)["active"] = None
    session._quest_offered = ["q_new"]


# ── 생산자 순수성 ────────────────────────────────────────────────
def test_staging_quest_choice_does_not_mutate_canonical(
        monkeypatch, session_with_quest):
    """T-B02 — 지시효과 스테이징만으로 canonical quest_state가 바뀌지 않는다."""
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    before = copy.deepcopy(core.capture_state(sess))
    pending = tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "테스트"}})
    after = core.capture_state(sess)

    assert before == after, "스테이징이 canonical 상태를 변경했습니다"
    assert pending.projected_quest_state is not None
    assert pending.projected_quest_state["active"]["id"] == "q_new"
    # canonical은 여전히 비활성.
    assert core.quest.get_state(sess)["active"] is None


def test_staging_info_ledger_does_not_mutate_canonical(session_auto_ready):
    """info_access 스테이징만으로 canonical info_ledger가 바뀌지 않는다."""
    sess = session_auto_ready
    sess.info_ledger = []
    before = copy.deepcopy(sess.info_ledger)

    decision = {"info_access": {"new_secrets": [
        {"info": "촌장의 비밀", "known_by": ["테스터"]}]}}
    pending = tp.stage_instruction_effects(sess, decision)

    assert sess.info_ledger == before, "스테이징이 info_ledger를 변경했습니다"
    assert pending.info_ledger is not None
    assert any(it["info"] == "촌장의 비밀" for it in pending.info_ledger)


# ── 투영 ─────────────────────────────────────────────────────────
def test_projection_reflects_staged_choice_without_canonical_change(
        monkeypatch, session_with_quest):
    """투영은 스테이징된 quest 선택을 보여주되 canonical은 그대로다."""
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    pending = tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "r"}})

    projected = tp.project_quest_state(sess, pending)
    assert projected["active"]["id"] == "q_new"          # 투영: 반영됨
    assert core.quest.get_state(sess)["active"] is None   # canonical: 미변경


def test_projection_without_pending_returns_canonical(session_with_quest):
    """스테이징 효과가 없으면 투영은 canonical get_state와 동치다."""
    sess = session_with_quest
    pending = tp.stage_instruction_effects(sess, {})
    projected = tp.project_quest_state(sess, pending)
    assert projected == core.quest.get_state(sess)


# ── 단일 적용 / idempotent ───────────────────────────────────────
def test_apply_once_updates_canonical(monkeypatch, session_with_quest):
    """T-B04 — 묘사 성공 후 적용이 정확히 한 번 canonical을 갱신한다."""
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    pending = tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "r"}})

    r1 = tp.apply_instruction_effects(sess, pending)
    assert r1["applied"] is True
    assert core.quest.get_state(sess)["active"]["id"] == "q_new"

    # 재적용은 무연산(중복 quest 적용 없음).
    snapshot = copy.deepcopy(core.capture_state(sess))
    r2 = tp.apply_instruction_effects(sess, pending)
    assert r2["applied"] is False
    assert core.capture_state(sess) == snapshot


def test_never_applied_leaves_canonical_unchanged(
        monkeypatch, session_with_quest):
    """T-B03 핵심 — 스테이징 후 적용을 하지 않으면(묘사 실패) canonical 무변화."""
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    pre = copy.deepcopy(core.capture_state(sess))
    tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "r"}})
    # 적용을 호출하지 않는다(묘사 실패 시나리오).
    assert core.capture_state(sess) == pre


def test_info_ledger_apply(session_auto_ready):
    """info_ledger 스테이징 → 적용 시 canonical 반영."""
    sess = session_auto_ready
    sess.info_ledger = []
    pending = tp.stage_instruction_effects(
        sess, {"info_access": {"new_secrets": [{"info": "비밀"}]}})
    assert sess.info_ledger == []           # 적용 전
    tp.apply_instruction_effects(sess, pending)
    assert any(it["info"] == "비밀" for it in sess.info_ledger)


# ── intended_case ───────────────────────────────────────────────
def test_intended_case_staged_not_canonical(session_with_quest):
    """intended_case 지정도 스테이징 단계에서는 canonical에 반영되지 않는다."""
    sess = session_with_quest
    # active 퀘스트에 root 노드 케이스를 준다.
    sess.quest_state["active"]["node"] = "root"

    import core as _c
    # tree에 케이스가 있어야 set_intended_case가 수용한다.
    monkey_quest = {
        "id": "q_test", "name": "시험 사건", "version": "a", "line": "sub",
        "tree": {"root": {"cases": {"c1": {"next": "n1"}}}, "n1": {}},
    }
    _c.quest._cache[sess.scenario_id] = {"quests": [monkey_quest]}

    before = copy.deepcopy(core.capture_state(sess))
    pending = tp.stage_instruction_effects(
        sess, {"quest_case": {"key": "c1"}})
    assert core.capture_state(sess) == before        # canonical 미변경
    assert pending.projected_quest_state["active"]["intended_case"] == "c1"

    tp.apply_instruction_effects(sess, pending)
    assert core.quest.get_state(sess)["active"]["intended_case"] == "c1"


# ── 투영 등가성 characterization (디렉터 조건) ────────────────────
def test_projected_quest_block_equivalent_to_applied_block(
        monkeypatch, session_with_quest):
    """build_quest_block(투영) == 실제 적용 후 build_quest_block.

    묘사 프롬프트가 pre-apply 시점에 보던 quest 의미를, canonical 변경 없이
    투영만으로 등가하게 볼 수 있어야 한다(§17 조건).
    """
    import core
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)

    pending = tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "r"}})

    # 투영 기준 블록(canonical 미변경).
    projected_block = core.quest.build_quest_block(
        sess, quest_state=pending.projected_quest_state)
    assert core.quest.get_state(sess)["active"] is None  # 여전히 미적용

    # 실제 적용 후 블록.
    tp.apply_instruction_effects(sess, pending)
    applied_block = core.quest.build_quest_block(sess)

    assert projected_block == applied_block, (
        "투영 quest 블록이 실제 적용 후 블록과 다릅니다")


def test_build_quest_block_projection_does_not_mutate_canonical(
        monkeypatch, session_with_quest):
    """build_quest_block에 투영을 넘겨도 canonical quest_state는 불변."""
    import core
    sess = session_with_quest
    _selectable(sess)
    _install_quest(monkeypatch, sess.scenario_id, QUEST_NEW)
    pending = tp.stage_instruction_effects(
        sess, {"quest_choice": {"id": "q_new", "reason": "r"}})

    before = copy.deepcopy(core.capture_state(sess))
    core.quest.build_quest_block(sess, quest_state=pending.projected_quest_state)
    assert core.capture_state(sess) == before
