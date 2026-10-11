# -*- coding: utf-8 -*-
"""구조 로그 — 호출 기록 프록시(동작 불변·델타 조각·맥락 상관), 이벤트·도메인·채팅, 상태 델타, 열람 도구."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

import core
from core import logs as L
from tools import log_view as LV

pytestmark = pytest.mark.policy

SID = "a1b2c3d4"


def _rows(name, sid=SID):
    p = os.path.join(L.session_dir(sid), name)
    return [json.loads(x) for x in open(p, encoding="utf-8")] if os.path.exists(p) else []


class _Inner:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    def generate_content(self, **kw):
        self.calls += 1
        if self.fail:
            raise ValueError("boom")
        return SimpleNamespace(
            text="응답 문단 하나\n\n" + "긴 응답 " * 40,
            usage_metadata=SimpleNamespace(prompt_token_count=100, cached_content_token_count=80,
                                           candidates_token_count=20, thoughts_token_count=5,
                                           total_token_count=125),
            candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name="STOP"))])


def _contents(*texts):
    return [SimpleNamespace(role=("user" if i % 2 == 0 else "model"),
                            parts=[SimpleNamespace(text=t)]) for i, t in enumerate(texts)]


HIST = "이전 턴 묘사 " * 60


def test_proxy_records_call_with_context_and_delta_and_is_transparent():
    inner = _Inner()
    models = L.LoggedModels(inner)
    cfg = SimpleNamespace(cached_content="caches/x", system_instruction=None, response_schema=None)

    async def run(prompt):
        return await core.resilience.call_with_retry(
            lambda: asyncio.to_thread(models.generate_content, model="m", contents=_contents(HIST, prompt),
                                      config=cfg),
            layer="narration", session_id=SID)
    ok, resp = asyncio.run(run("이번 입력 A " * 30))
    assert ok and resp.text.startswith("응답")
    asyncio.run(run("이번 입력 B " * 30))
    calls = _rows("calls.jsonl")
    assert len(calls) == 2 and inner.calls == 2
    c = calls[0]
    assert (c["layer"], c["attempt"], c["status"], c["cache"], c["finish"]) == ("narration", 1, "ok", "caches/x", "STOP")
    assert c["usage"] == {"in": 100, "cached": 80, "out": 20, "thought": 5, "total": 125}
    # 델타: 반복된 이력 조각은 blobs.jsonl에 한 번만
    blobs = _rows("blobs.jsonl")
    hist_h = c["req"][0]["parts"][0][0]
    assert sum(1 for b in blobs if "#" + b["h"] == hist_h) == 1
    D = LV.load(os.path.join("sessions", SID))
    assert LV.joined(D, c["req"][1]["parts"][0]) == "이번 입력 A " * 30
    assert LV.joined(D, c["resp"]) == resp.text


def test_proxy_error_is_recorded_and_reraised():
    models = L.LoggedModels(_Inner(fail=True))
    with L.call_context(SID, "judgment"):
        with pytest.raises(ValueError):
            models.generate_content(model="m", contents="질문")
    c = _rows("calls.jsonl")[-1]
    assert c["status"] == "error" and "boom" in c["exc"] and c["layer"] == "judgment"


def test_install_proxy_wraps_client_models_once():
    client = SimpleNamespace(_models=_Inner())
    L.install_client_proxy(client)
    L.install_client_proxy(client)
    assert isinstance(client._models, L.LoggedModels) and not isinstance(client._models._inner, L.LoggedModels)


def test_write_log_routes_to_chat_events_and_error_has_trace():
    core.write_log(SID, "game_chat", "[김이서]: 문을 연다")
    core.write_log(SID, "api", "[H05 계획] status=NONE\n상세 내용")
    try:
        raise RuntimeError("실패")
    except RuntimeError as e:
        L.error("commit", "정본 영속 전 실패", exc=e, sid=SID)
    chat = _rows("chat.jsonl")[-1]
    assert (chat["ch"], chat["who"], chat["text"]) == ("game", "김이서", "문을 연다")
    ev = _rows("events.jsonl")
    assert ev[0]["msg"] == "[H05 계획] status=NONE" and ev[0]["data"] == {"detail": "상세 내용"}
    assert ev[-1]["lv"] == "ERROR" and "Traceback" in ev[-1]["trace"]
    assert not os.path.exists(os.path.join("sessions", SID, "api_log.txt"))


def test_state_diff_paths_append_and_delete():
    a = {"players": {"1": {"res": {"물": 1}}}, "logs": [1, 2], "gone": 1, "same": {"x": 1}}
    b = {"players": {"1": {"res": {"물": 0, "칼": 1}}}, "logs": [1, 2, 3], "same": {"x": 1}}
    d = L.state_diff(a, b)
    assert d == {"gone": {"del": True}, "logs": {"append": [3]},
                 "players": {"1": {"res": {"물": {"set": 0}, "칼": {"set": 1}}}}}


def test_transaction_lifecycle_and_turn_correlation(session_auto_ready):
    s = session_auto_ready
    tx = core.turn_transaction.begin_turn_transaction(s, "선언")
    core.turn_transaction.mark_waiting_for_player(s, tx.transaction_id)
    L.event("turn", "테스트", session=s)
    dom = _rows("domain.jsonl", s.session_id)
    assert dom[0]["kind"] == "tx.begin" and dom[0]["data"]["declaration"] == "선언"
    assert dom[1]["kind"] == "tx.status" and dom[1]["data"]["to"] == "WAITING_FOR_PLAYER"
    assert _rows("events.jsonl", s.session_id)[-1]["tx"] == tx.transaction_id


def test_print_with_session_id_is_attributed(tmp_path):
    os.makedirs(os.path.join("sessions", SID), exist_ok=True)
    assert L._print_session(f"[GM/{SID}] 턴 처리 실패") == SID
    assert L._print_session("[GM/deadbeef] 없는 세션") is None


from tests.policy.test_h05bc_turn_wiring import drig, rig, turn  # noqa: E402,F401


async def test_committed_turn_leaves_delta_commit_cost_and_ink_facts(drig):
    r = drig
    r.make()
    await turn(r)                                              # 항구 → 시장 이동 턴(커밋)
    sid = r.sess.session_id
    kinds = [d["kind"] for d in _rows("domain.jsonl", sid)]
    for k in ("tx.begin", "cost.event", "turn.delta", "commit", "ink.charge"):
        assert k in kinds, (k, kinds)
    dom = {d["kind"]: d for d in _rows("domain.jsonl", sid)}
    delta = dom["turn.delta"]["data"]["diff"]
    assert "spatial_state" in delta                             # 이 턴의 정본 변화(위치)
    assert any(op.get("kind") or op.get("op_type") for op in dom["commit"]["data"]["world_ops"])
    assert dom["commit"]["data"]["tx"] == dom["turn.delta"]["data"]["tx"]
