"""v7.43.0 — 스킬 숙련 보정(S)·시작 아이템 선택.

S: 판단층위가 굴림마다 PC 프로필 스킬 하나를 지목하면 코드가 목록과 대조해 +6, 목록 밖·NPC·빈 값은 0.
"""
from __future__ import annotations

import pytest

import core
import prompts
from core import judgment as J
from core import turn_transaction as tt
from tests.policy.test_ready_barrier import rig  # noqa: F401 — 픽스처 재사용
from tests.policy.test_v712_judgment_2d50 import _dice, _spec

pytestmark = pytest.mark.policy


def _give_skills(r, text):
    uid = core.get_uid_by_char_name(r.sess, "테스터")
    r.sess.players[uid].setdefault("profile", {})["스킬"] = text


def test_skill_mod_exact_match_only():
    assert J.skill_mod({"스킬": "화물 다루기, 부두 지리"}, "부두 지리") == J.SKILL_BONUS == 6
    assert J.skill_mod({"스킬": "화물 다루기, 부두 지리"}, "부두") == 0
    assert J.skill_mod({"스킬": ["응급 처치"]}, "응급 처치") == 6
    assert J.skill_mod({}, "응급 처치") == 0 and J.skill_mod({"스킬": "응급 처치"}, "") == 0


@pytest.mark.parametrize("named,expect_t,shown", [
    ("근력 작업", 58, "+6(숙련 근력 작업)"),     # 근력 10 → A 0, S +6
    ("등반", 52, None),                         # 프로필 밖 → 0
    ("", 52, None),
])
async def test_named_profile_skill_raises_target(rig, monkeypatch, named, expect_t, shown):
    r = rig
    _give_skills(r, "근력 작업, 매듭·로프")
    uid = core.get_uid_by_char_name(r.sess, "테스터")
    r.sess.players[uid]["profile"]["근력"] = 10
    _dice(monkeypatch, 25, 30)
    tx = tt.get_or_begin_turn_transaction(r.sess, "상자를 든다")
    res = await r.gm._execute_rolls(r.sess, [_spec(skill=named)], r.gch,
                                    transaction_id=tx.transaction_id)
    rec = tx.judgment_result["roll_records"][0]["actor"]
    assert rec["T"] == expect_t and rec["S"] == (6 if shown else 0)
    assert (shown in res[0]) if shown else ("숙련" not in res[0])


def test_schema_offers_optional_skill_on_pc_side_only():
    items = prompts.JUDGMENT_RESPONSE_SCHEMA["properties"]["rolls"]["items"]
    assert "skill" in items["properties"] and "skill" not in items["required"]
    assert "skill" not in items["properties"]["opponent"]["properties"]


# ── 시작 물건 선택(D4 가) ─────────────────────────────────

from core import profile_gen as PG  # noqa: E402
from core import profile_runner as R  # noqa: E402

_SD = {
    "jobs": {"요리사": {"desc": "칼"}},
    "starting_item_choices": {"직업": {"요리사": ["식칼", "소금[봉지]", "라이터", "통조림"]}},
    "starting_items": {"공통": ["물통", "비상식량"], "수량": {"소금[봉지]": 2}},
    "profile_creation": [
        {"field": "직업", "module": "select_one", "args": {"from": "jobs"}},
        {"field": "시작 소지품", "module": "intersect_list",
         "args": {"source": "starting_item_choices", "depends": ["직업"], "choose": 2,
                  "grant_items": True, "prompt": "두 가지를 골라 주십시오."}},
    ],
}


def _at_items():
    run = R.new_run(_SD, prefill={"직업": "요리사"})
    res = R.step(_SD, run)
    return run, res


def test_choose_asks_for_exactly_n_from_job_pool():
    run, res = _at_items()
    assert res["type"] == R.ASK and res["multi"] == 2
    assert sorted(res["options"]) == sorted(["식칼", "소금[봉지]", "라이터", "통조림"])
    for bad in (["식칼"], ["식칼", "식칼"], ["식칼", "총"], ["식칼", "라이터", "통조림"]):
        again = R.step(_SD, run, bad)
        assert again["type"] == R.ASK and run["chosen"].get("시작 소지품") is None, bad


def test_choice_confirms_then_grants_with_quantity_not_profile():
    run, _ = _at_items()
    res = R.step(_SD, run, ["소금[봉지]", "라이터"])
    assert res["type"] == R.CONFIRM
    R.confirm(_SD, run)
    result = R.result(run)
    assert result["시작 소지품"] == ["소금[봉지]", "라이터"]
    items = PG.starting_items(_SD, "요리사", PG.granted_choices(_SD, result))
    assert items == {"물통": 1, "비상식량": 1, "소금[봉지]": 2, "라이터": 1}
    assert PG.items_text(items).endswith("소금[봉지] ×2, 라이터")
    assert R.expected_type_ok(_SD["profile_creation"][1], ["식칼", "라이터"])
    assert not R.expected_type_ok(_SD["profile_creation"][1], "식칼")


def test_job_change_clears_item_choice():
    assert R.dependents_of(_SD, "직업") == ["시작 소지품"]


# ── 생성 화면 통합: 다중 선택 → 확인 → 최종 승인 → 소지품(프로필 글에는 없음) ──

import copy  # noqa: E402

from core import profile_creation_ui as PCU  # noqa: E402
from tests.conftest import PLAYER_UID  # noqa: E402
from tests.fakes.discord_fakes import FakeInteraction, FakeUser  # noqa: E402
from tests.policy import test_v715_character_creation as C  # noqa: E402

_ITEM_SCN = copy.deepcopy(C.SCN)
_ITEM_SCN["starting_item_choices"] = {"직업": {"어부": ["갈고리", "장화[켤레]", "밧줄[타래]"]}}
_ITEM_SCN["starting_items"] = {"공통": ["물병"], "수량": {"밧줄[타래]": 2}}
_ITEM_SCN["profile_creation"].insert(4, {
    "field": "시작 소지품", "module": "intersect_list",
    "args": {"source": "starting_item_choices", "depends": ["직업"], "choose": 2, "grant_items": True}})


@pytest.fixture
def pci(fake_bot, session_factory, monkeypatch):
    monkeypatch.setattr(C, "SCN", _ITEM_SCN)
    yield from C.pc.__wrapped__(fake_bot, session_factory, monkeypatch)


async def test_player_picks_items_in_creation_and_gets_them_as_inventory(pci):
    r = pci
    await C.start(r)
    for lab in ("남", "확정"):
        await C.press(r, lab)
    await C.chat(r, "홍길동")
    for lab in ("확정", "어부", "확정"):
        await C.press(r, lab)
    state = C.st(r)
    sel = next(c for c in state.message.view.children if getattr(c, "placeholder", None) == "2개 선택")
    assert (sel.min_values, sel.max_values) == (2, 2)
    sel._values = ["밧줄[타래]", "갈고리"]
    await sel.callback(FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=r.ch, message=state.message))
    await C.press(r, "확정")
    assert state.run["chosen"]["시작 소지품"] == ["밧줄[타래]", "갈고리"]
    await C.press(r, "✅ 확정")                                        # 능력치
    await C.guided_appearance(r)
    await C.press(r, "✅ 이대로 확정")
    await C.chat(r, "낡은 작업복")
    await C.press(r, "✅ 이대로 확정")
    await C.press(r, "✅ 이대로 시작")
    assert r.sess.resources["홍길동"] == {"물병": 1, "밧줄[타래]": 2, "갈고리": 1}
    assert "시작 소지품" not in r.sess.players[PLAYER_UID]["profile"]
