# -*- coding: utf-8 -*-
"""v7.32 `!세션관리` 일괄 관리 — 사용자 결정: 다중 선택 + 페이지 + 상태별 일괄 선택 · 막힌 세션은 건너뛰고 나머지 진행 ·
진행 중 제거는 승인된 원래 범위로 재개. 정산·환급·삭제 단계는 기존 단일 세션 경로(begin → run) 그대로다.
"""
from __future__ import annotations

import types

import pytest

from core import session_removal as SR
from tests.conftest import PLAYER_UID
from tests.policy.test_cache_lifecycle import cbot, clock, fresh  # noqa: F401
from tests.policy.test_session_removal import GUILD, _Inter, _labels, _open, _refunds, _approve, _wire

pytestmark = pytest.mark.policy


async def _true(*a, **k):
    return True


async def _false(*a, **k):
    return False


def _metas(n):
    out = {}
    for i in range(n):
        sid = f"s{i:02d}"
        out[sid] = {"session_id": sid, "guild_id": GUILD, "creator_uid": "555", "scenario_id": "영도",
                    "mtime": 1000 - i, "world_lifecycle": "CLOSED" if i % 3 == 0 else "OPEN",
                    "cache_name": None if i % 2 == 0 else "c", "players": {}}
    return out


async def test_multi_select_persists_across_pages_and_status_filters_add_all_pages(monkeypatch):
    metas = _metas(30)
    monkeypatch.setattr(SR, "entries", lambda bot: {k: dict(v) for k, v in metas.items()})
    monkeypatch.setattr(SR, "is_operator", _true)
    view = SR.ManageView(types.SimpleNamespace(), types.SimpleNamespace(id=555), types.SimpleNamespace(id=GUILD))
    assert await view.prepare() and view.pages() == 2 and len(view.page_ids()) == 25
    log = []
    view.set_page_selection(["s01", "s02"])
    await view._next(_Inter(555, log))
    assert view.page == 1 and view.page_ids() == [f"s{i}" for i in range(25, 30)]
    view.set_page_selection(["s26"])
    assert view.selected == ["s01", "s02", "s26"]                           # 페이지를 넘겨도 선택 유지
    view.set_page_selection([])                                             # 이 페이지만 해제
    assert view.selected == ["s01", "s02"]
    await view._filter_handler(SR._is_closed)(_Inter(555, log))             # 모든 페이지의 종료된 세션
    closed = [sid for sid, m in metas.items() if m["world_lifecycle"] == "CLOSED"]
    assert set(closed) <= set(view.selected) and "s27" in view.selected
    assert f"다음 ({len(view.selected)}개)" in _labels(view)
    await view._clear(_Inter(555, log))
    assert view.selected == [] and next(c for c in view.children if getattr(c, "label", "").startswith("다음")).disabled


async def test_batch_runs_each_session_skips_blocked_and_resumes_approved_scope(fresh, cbot, clock, session_factory,
                                                                             monkeypatch):
    a = fresh
    _wire(cbot, a)
    a.creator_uid = "555"
    await _open(cbot, a)
    b = session_factory(session_id="wpf-b", game_ch_id=5101, master_ch_id=5102, display_ch_id=5103,
                        players={PLAYER_UID: {"name": "테스터", "profile": {}}}, open_minutes=180, creator_uid="555")
    cbot.register_session(b)
    _wire(cbot, b)
    await _open(cbot, b)
    c = session_factory(session_id="wpf-c", game_ch_id=5201, master_ch_id=5202, display_ch_id=5203,
                        players={PLAYER_UID: {"name": "테스터", "profile": {}}}, open_minutes=180, creator_uid="555")
    cbot.register_session(c)
    _wire(cbot, c)
    await _open(cbot, c)
    d = session_factory(session_id="wpf-d", game_ch_id=5301, master_ch_id=5302, display_ch_id=5303,
                        players={PLAYER_UID: {"name": "테스터", "profile": {}}}, open_minutes=180, creator_uid="555")
    cbot.register_session(d)
    _, _, d_chans = _wire(cbot, d)
    await _open(cbot, d)
    d_chans[d.display_ch_id].fail_delete = RuntimeError("API 실패")          # d: 채널 삭제 범위로 승인 후 미완료
    _approve(cbot, d, SR.SCOPE_CHANNELS, actor="555")
    assert not (await SR.run(cbot, d.session_id))["done"]
    d_chans[d.display_ch_id].fail_delete = None

    orig = SR.blocked_reason
    monkeypatch.setattr(SR, "blocked_reason", lambda s: "턴 진행 중" if s is c else orig(s))   # c: 막힘
    monkeypatch.setattr(SR, "is_operator", _false)
    view = SR.ManageView(cbot, types.SimpleNamespace(id=555, send=None), types.SimpleNamespace(id=GUILD))
    assert await view.prepare()
    view.selected = [a.session_id, b.session_id, c.session_id, d.session_id]
    log = []
    await view.show_confirm(_Inter(555, log))                               # 범위 선택(새 세션에 적용)
    view.scope = SR.SCOPE_CLOSE
    await view.show_confirm(_Inter(555, log))
    assert _labels(view) == ["진행", "취소"]
    confirm = [e for e in log if e[0] == "edit" and getattr(e[1].get("embed"), "title", "") == "⚠️ 세션 정리 확인"]
    assert len(confirm) == 1
    target = confirm[0][1]["embed"].fields[0]
    assert "건너뜀 예정 1" in target.name and "재개(원래 범위 채널 삭제)" in target.value
    await view._proceed(_Inter(555, log))

    assert SR.read_checkpoint(a.session_id) is None and SR.read_checkpoint(b.session_id) is None
    assert SR.read_checkpoint(c.session_id) is None and c.world_lifecycle != "CLOSED"   # 건너뜀 — 아무것도 안 바뀜
    assert SR.read_checkpoint(d.session_id) is None and d_chans[d.display_ch_id].deleted   # 원래 범위로 재개 완료
    assert a.world_lifecycle == b.world_lifecycle == "CLOSED"
    assert len(_refunds()) == 3                                             # a·b·d 각 1회(중복 없음)
    final = [e for e in log if e[0] == "final"][-1][1]["embeds"][0]
    assert final.title.startswith("🗂️ 일괄 정리 결과") and "완료 3 · 건너뜀 1 · 미완료 0" in final.description
