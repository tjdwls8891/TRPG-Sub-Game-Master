"""v7.31 이름 지정 고정 카드(감염자) — 사용자 결정: 항상 전체 표시 · 원화 여·남 턴마다 무작위 배정 · 문구 계획서 제안대로.

감염자는 정본 개체(ActorId)가 없다 — 화자 이름으로 카드를 고르고, 인지 조건 없이 전체를 보이며,
그림은 (턴 정체성, 화자)의 결정적 추첨(같은 턴·재생성·재시작 = 같은 그림)으로 고른다.
"""
from __future__ import annotations

import copy
import json
import os

import pytest

from core import speaker_card as SC
from tests.conftest import REPO_ROOT
from tests.fakes.discord_fakes import FakeChannel
from tests.policy.test_svg_speaker_card import DEFINITION, SID, _config, _world, card_session  # noqa: F401

pytestmark = pytest.mark.policy


def _fixed_cfg():
    cfg = _config()
    cfg["fixed_speakers"] = {"감염자": {
        "assign": SC.ASSIGN_TURN_RANDOM, "default_variation": "neutral",
        "variations": {"neutral": "cards/neutral.png", "alert": "cards/alert.png"},
        "values": {"name": "감염자", "faction": "INFECTED", "logo": "cards/logo.png", "color": "#C8102E"},
        "rules": [{"when": {"always": True}, "set": {"portrait": {"variation_image": True}, "name": {"value": "name"},
                                                    "faction": {"value": "faction"}, "logo": {"value": "logo"},
                                                    "panel": {"value": "color"}}}]}}
    return cfg


def test_fixed_speaker_shows_full_card_without_any_knowledge(card_session, monkeypatch):
    cfg = _fixed_cfg()
    monkeypatch.setattr(SC, "card_world", lambda _s: _world(cfg))
    ctx = SC.display_context(card_session, "감염자")
    assert (ctx.status, ctx.header, ctx.actor_id) == (SC.SPEAKER_FIXED, "감염자", None)
    slots = SC.evaluate_slots(cfg, ctx.card, frozenset(), "alert")            # 인지 0건
    assert (slots["name"], slots["faction"], slots["logo"], slots["portrait"]) == \
        ("감염자", "INFECTED", "cards/logo.png", "cards/alert.png")
    assert SC.display_context(card_session, "누군가").header == "???"           # 다른 화자 규칙 불변


def test_turn_random_assignment_is_stable_per_turn_and_spreads_across_turns():
    card = _fixed_cfg()["fixed_speakers"]["감염자"]
    a = SC.assigned_variation(card, "감염자", "tx-1")
    assert a in card["variations"] and SC.assigned_variation(card, "감염자", "tx-1") == a   # 같은 턴 = 같은 그림
    seen = {SC.assigned_variation(card, "감염자", f"{i:032x}") for i in range(50)}
    assert seen == set(card["variations"])                                    # 턴이 바뀌면 둘 다 나온다
    assert SC.assigned_variation(dict(card, assign=None), "감염자", "tx-1") is None


async def test_render_uses_transaction_identity_for_assignment(card_session, monkeypatch):
    cfg = _fixed_cfg()
    monkeypatch.setattr(SC, "card_world", lambda _s: _world(cfg))
    used = []
    orig = SC.render_png
    monkeypatch.setattr(SC, "render_png", lambda w, c, k, v, **kw: (used.append(v), orig(w, c, k, v, **kw))[1])
    prep = type("P", (), {"transaction_id": "tx-abc", "task_future": lambda self, n: None})()
    ctx = SC.display_context(card_session, "감염자")
    assert await SC.deliver_card(FakeChannel(1), card_session, ctx, prep, threshold=5) == "CARD"
    assert used == [SC.assigned_variation(ctx.card, "감염자", "tx-abc")]


def test_validator_rules_for_fixed_speakers():
    def codes(mut):
        cfg = _fixed_cfg()
        mut(cfg)
        return {d[1] for d in SC.check_card_definition({"speaker_card": cfg}, definition=DEFINITION, scenario_id=SID)}
    assert codes(lambda c: None) == set()
    card = lambda c: c["fixed_speakers"]["감염자"]  # noqa: E731
    assert "CARD_FIXED_SPEAKERS" in codes(lambda c: c.__setitem__("plain_speakers", ["감염자"]))
    assert "CARD_FIXED_SPEAKERS" in codes(lambda c: card(c).__setitem__("knowledge", {"name": "i.name"}))
    assert "CARD_FIXED_SPEAKERS" in codes(lambda c: card(c).__setitem__("assign", "per_line"))
    assert "CARD_RULE_WHEN" in codes(lambda c: card(c)["rules"][0].__setitem__("when", {"all_info": ["i.name"]}))
    assert "CARD_VALUE" in codes(lambda c: card(c)["rules"][0]["set"].__setitem__("faction", {"value": "nope"}))
    assert "CARD_ASSET" in codes(lambda c: card(c)["variations"].__setitem__("alert", "cards/missing.png"))
    # 인물 카드는 여전히 always를 쓸 수 없다(인지 조건 우회 금지)
    assert "CARD_RULE_WHEN" in codes(lambda c: c["actors"][next(iter(c["actors"]))]["rules"][0]
                                     .__setitem__("when", {"always": True}))


def test_yeongdo_infected_card_authored_with_two_portraits():
    with open(os.path.join(REPO_ROOT, "scenarios", "영도.json"), encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw["speaker_card"]
    assert "감염자" not in (cfg.get("plain_speakers") or [])
    card = cfg["fixed_speakers"]["감염자"]
    assert card["assign"] == SC.ASSIGN_TURN_RANDOM and sorted(card["variations"]) == ["female", "male"]
    assert card["values"]["name"] == "감염자" and card["values"]["accent"] == "#C8102E"
    errs = SC.check_card_definition(copy.deepcopy(raw), base=os.path.join(REPO_ROOT, "media"), scenario_id="영도")
    assert errs == []                                                          # 자산 해시 포함
