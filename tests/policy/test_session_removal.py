# -*- coding: utf-8 -*-
"""세션 종료·제거 통합 관리(`!세션관리`) — 재무 정합성·데이터 안전성 우선의 수용 기준.

이중 환급 없음 · 재무 원장·증빙 보존 · 타인 세션 리소스 보호 · 늦은 쓰기 경합 · 재시작·재개 멱등.
"""
from __future__ import annotations

import json
import os
import shutil
import types

import discord
import pytest

import core
from core import cache_lifecycle as CLC
from core import io as cio
from core import session_removal as SR
from core import turn_transaction as tt
from core import world_lifecycle as WL
from tests.conftest import PLAYER_UID
from tests.fakes.bot_fakes import FakeBot
from tests.policy.test_cache_lifecycle import _ink_rows, cbot, clock, fresh  # noqa: F401

pytestmark = pytest.mark.policy

GUILD = 7700


# ── 길드·채널 최소 대역(실제 discord.py 인터페이스 중 쓰는 것만) ──

class _Guild:
    def __init__(self, gid=GUILD):
        self.id = gid
        self.voice_client = None
        self.default_role = "everyone"


class _Ch:
    def __init__(self, cid, guild, category=None, name=""):
        self.id, self.guild, self.category, self.name = cid, guild, category, name or f"ch-{cid}"
        self.deleted, self.sent, self.perms = False, [], []
        self.fail_delete = None
        self.channels = []          # 카테고리일 때 하위 채널

    async def delete(self, reason=None):
        if self.fail_delete:
            raise self.fail_delete
        self.deleted = True
        if self.category is not None and self in self.category.channels:
            self.category.channels.remove(self)

    async def send(self, content=None, **kw):
        self.sent.append(content)

    async def set_permissions(self, target, **kw):
        self.perms.append(kw)


def _not_found():
    return discord.NotFound(types.SimpleNamespace(status=404, reason="Not Found"), "gone")


def _wire(bot, s, *, guild=None, sid_name=None):
    """세션 채널 4개 + 카테고리를 대역 길드에 붙이고 봇 조회를 연결한다."""
    g = guild or _Guild()
    cat = _Ch(9000 + len(getattr(bot, "_wired", {})), g, name=f"TRPG Session {sid_name or s.session_id}")
    chans = {}
    s.voice_ch_id = s.voice_ch_id or (s.display_ch_id + 7)
    for k in SR.CHANNEL_KEYS:
        ch = _Ch(getattr(s, k), g, category=cat)
        cat.channels.append(ch)
        chans[getattr(s, k)] = ch
    chans[cat.id] = cat
    s.guild_id, s.category_id = g.id, cat.id
    wired = getattr(bot, "_wired", None)
    if wired is None:
        wired = bot._wired = {}
    wired.update({k: v for k, v in chans.items()})
    bot._guilds = getattr(bot, "_guilds", {})
    bot._guilds[g.id] = g

    def get_channel(cid):
        ch = bot._wired.get(cid)
        return None if ch is None or ch.deleted else ch

    async def fetch_channel(cid):
        ch = bot._wired.get(cid)
        if ch is None or ch.deleted:
            raise _not_found()
        return ch
    bot.get_channel = get_channel
    bot.fetch_channel = fetch_channel
    bot.get_guild = lambda gid: bot._guilds.get(gid)
    return g, cat, chans


@pytest.fixture(autouse=True)
def _fresh_removal_marks():
    cio._REMOVED_SESSION_IDS.clear()           # 프로세스 수명 표식 — 테스트 간 같은 세션 id 재사용
    SR._RUN_LOCKS.clear()
    yield
    cio._REMOVED_SESSION_IDS.clear()


@pytest.fixture(autouse=True)
def _quiet_boards(monkeypatch):
    calls = []

    async def _rb(bot, guild):
        calls.append(guild.id)
        return True
    monkeypatch.setattr(core.gm_space, "refresh_boards", _rb)
    return calls


def _refunds():
    return [x for x in _ink_rows() if x["direction"] == "CREDIT"]


def _ledger_lines():
    p = os.path.join("data", "cost_ledger.jsonl")
    return open(p, encoding="utf-8").read().splitlines() if os.path.exists(p) else []


async def _open(cbot, s):
    await CLC.open_window(cbot, s)
    await cio.save_session_data_strict(cbot, s)


def _approve(cbot, s, scope, actor=PLAYER_UID):
    meta = SR._meta_from_session(s)
    return SR.begin(cbot, s.session_id, scope, actor_id=actor, guild_id=GUILD, meta=meta)


# ══════════════════════════════════════════════════════════════
#  완전 제거 — 정산 1회 · 원장·증빙 보존 · 정확한 리소스만 삭제
# ══════════════════════════════════════════════════════════════

async def test_full_removal_settles_once_preserves_finance_and_deletes_only_own(fresh, cbot, clock):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    await _open(cbot, s)
    clock.advance(600)
    other = _Ch(424242, g, category=cat, name="남의-채널")       # 같은 카테고리의 다른 소유 채널
    cat.channels.append(other)
    cbot._wired[other.id] = other
    os.makedirs("sessions/other-sess", exist_ok=True)
    json.dump({"session_id": "other-sess"}, open("sessions/other-sess/data.json", "w"))
    journal_before = open(f"sessions/{s.session_id}/cache_lifecycle.jsonl", "rb").read()

    _approve(cbot, s, SR.SCOPE_FULL)
    rep = await SR.run(cbot, s.session_id)
    assert rep["done"] and rep["ok"], rep
    assert len(_refunds()) == 1                                          # 미사용 선불 환급 정확히 한 번
    assert all(ch.deleted for cid, ch in chans.items() if cid != cat.id)  # 세션 채널 4개
    assert not other.deleted and not cat.deleted                          # 다른 채널이 남은 카테고리는 유지
    assert not os.path.exists(f"sessions/{s.session_id}")                 # 게임 데이터 삭제
    assert os.path.exists("sessions/other-sess/data.json")                # 다른 세션 보존
    assert SR.read_checkpoint(s.session_id) is None                       # 임시 체크포인트 정리
    arch = SR.archive_path(s.session_id)
    man = json.load(open(os.path.join(arch, "manifest.json"), encoding="utf-8"))
    assert "cache_lifecycle.jsonl" in man["files"]
    saved = open(os.path.join(arch, "cache_lifecycle.jsonl"), "rb").read()
    assert saved.startswith(journal_before) and len(saved) > len(journal_before)   # 종료 정산까지 보존
    led = _ledger_lines()
    assert led                                                            # 전역 비용 원장 보존
    assert not any(v is s for v in cbot.active_sessions.values())

    # 같은 승인의 재실행·재요청: 아무 일도 다시 일어나지 않는다
    rep2 = await SR.run(cbot, s.session_id)
    assert not rep2["done"] and len(_refunds()) == 1 and _ledger_lines() == led


async def test_category_deleted_when_empty_and_close_only_keeps_everything(fresh, cbot, clock, _quiet_boards):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    await _open(cbot, s)
    _approve(cbot, s, SR.SCOPE_CHANNELS)
    rep = await SR.run(cbot, s.session_id)
    assert rep["done"] and cat.deleted                                     # 비면 카테고리도 정리
    assert os.path.exists(f"sessions/{s.session_id}/data.json")           # 채널 삭제 범위 = 기록 보존
    assert _quiet_boards == [GUILD]                                       # 통계·보드 반영 1회


async def test_close_only_locks_game_channel_and_keeps_data(fresh, cbot, clock):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    await _open(cbot, s)
    _approve(cbot, s, SR.SCOPE_CLOSE)
    rep = await SR.run(cbot, s.session_id)
    assert rep["done"]
    game = chans[s.game_ch_id]
    assert game.perms == [{"send_messages": False}] and not game.deleted
    disk = json.load(open(f"sessions/{s.session_id}/data.json", encoding="utf-8"))
    assert disk["world_lifecycle"] == WL.LIFECYCLE_CLOSED and len(_refunds()) == 1


async def test_already_closed_session_is_not_settled_or_counted_again(fresh, cbot, clock, _quiet_boards):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    res = await WL.close_session(cbot, s, reason=CLC.REASON_OPERATOR_END)  # 기존 !세션종료 경로
    assert res["ok"] and len(_refunds()) == 1
    finals = [e for e in CLC.load_events(s.session_id) if e["type"] == "FINALIZED"]
    _approve(cbot, s, SR.SCOPE_DATA)                                       # 메모리에 없음 → 디스크에서 읽음
    os.makedirs("scenarios", exist_ok=True)
    json.dump(s.scenario_data, open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8"), ensure_ascii=False)
    rep = await SR.run(cbot, s.session_id)
    assert rep["done"], rep
    assert len(_refunds()) == 1 and _quiet_boards == []                    # 정산·통계 중복 없음
    archived = open(os.path.join(SR.archive_path(s.session_id), "cache_lifecycle.jsonl"), encoding="utf-8").read()
    assert sum('"FINALIZED"' in ln for ln in archived.splitlines()) == len(finals)


# ══════════════════════════════════════════════════════════════
#  안전 지점 · 쓰기 경합
# ══════════════════════════════════════════════════════════════

async def test_active_turn_or_compression_blocks_without_side_effects(fresh, cbot, clock):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    s.is_compressing = True
    _approve(cbot, s, SR.SCOPE_FULL)
    rep = await SR.run(cbot, s.session_id)
    assert not rep["ok"] and "압축" in rep["reason"]
    assert SR.read_checkpoint(s.session_id) is None                        # 아무것도 안 바뀜 → 승인 무효
    assert s.world_lifecycle not in WL.ENDING and _refunds() == []
    assert os.path.exists(f"sessions/{s.session_id}/data.json")
    s.is_compressing = False
    tt.begin_turn_transaction(s, "문을 연다")                              # 진행 중인 턴
    _approve(cbot, s, SR.SCOPE_FULL)
    rep = await SR.run(cbot, s.session_id)
    assert not rep["ok"] and "턴" in rep["reason"] and _refunds() == []
    assert SR.read_checkpoint(s.session_id) is None and s.world_lifecycle not in WL.ENDING


async def test_late_save_and_log_cannot_resurrect_deleted_session(fresh, cbot, clock):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    _approve(cbot, s, SR.SCOPE_DATA)
    assert (await SR.run(cbot, s.session_id))["done"]
    await core.save_session_data(cbot, s)                                  # 늦게 끝난 비동기 작업(tolerant)
    with pytest.raises(Exception):
        await cio.save_session_data_strict(cbot, s)                        # strict는 실패를 알린다
    core.logs._append(s.session_id, "events.jsonl", {"x": 1})
    assert not os.path.exists(f"sessions/{s.session_id}")
    bot2 = FakeBot(strict=False)
    await core.restore_sessions_from_disk(bot2)
    assert not any(getattr(v, "session_id", None) == s.session_id for v in bot2.active_sessions.values())


async def test_finance_symlink_refuses_data_deletion(fresh, cbot, clock, tmp_path):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("{}\n")
    os.symlink(str(target), f"sessions/{s.session_id}/retry_claims.jsonl")
    _approve(cbot, s, SR.SCOPE_DATA)
    rep = await SR.run(cbot, s.session_id)
    assert not rep["done"] and "심볼릭" in rep["reason"]
    assert os.path.exists(f"sessions/{s.session_id}/data.json")            # 삭제 거부
    assert SR.read_checkpoint(s.session_id)["steps"].get("close")          # 정산은 끝났고 재개 대기
    assert len(_refunds()) == 1


def test_session_dir_rejects_traversal_and_symlink(tmp_path):
    for bad in ("..", "../x", "a/b", "", ".removal_trash"):
        with pytest.raises(SR.RemovalError):
            SR.session_dir(bad)
    os.makedirs("sessions", exist_ok=True)
    os.makedirs(tmp_path / "outside", exist_ok=True)
    os.symlink(str(tmp_path / "outside"), "sessions/linked")
    with pytest.raises(SR.RemovalError):
        SR.session_dir("linked")


# ══════════════════════════════════════════════════════════════
#  부분 실패 · 재시작 · 재개 멱등
# ══════════════════════════════════════════════════════════════

async def test_channel_failure_stops_before_data_and_resume_finishes_once(fresh, cbot, clock):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    await _open(cbot, s)
    chans[s.master_ch_id].fail_delete = RuntimeError("API 실패")
    _approve(cbot, s, SR.SCOPE_FULL)
    rep = await SR.run(cbot, s.session_id)
    assert not rep["done"] and "채널 삭제 일부 실패" in rep["reason"]
    assert os.path.exists(f"sessions/{s.session_id}/data.json")            # 채널 미완료 → 데이터 삭제 안 함
    cp = SR.read_checkpoint(s.session_id)
    assert cp["steps"].get("close") and not cp["steps"].get("channels")
    chans[s.master_ch_id].fail_delete = None
    rep2 = await SR.run(cbot, s.session_id)                                # 재개(추가 승인 없음)
    assert rep2["done"] and len(_refunds()) == 1
    assert not os.path.exists(f"sessions/{s.session_id}")


async def test_crash_after_move_resumes_purge_without_resurrection(fresh, cbot, clock, monkeypatch):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    _approve(cbot, s, SR.SCOPE_DATA)
    real = shutil.rmtree
    monkeypatch.setattr(SR.shutil, "rmtree", lambda p: (_ for _ in ()).throw(OSError("중단")))
    rep = await SR.run(cbot, s.session_id)
    assert not rep["done"]
    cp = SR.read_checkpoint(s.session_id)
    assert cp["steps"].get("data_moved") and os.path.isdir(cp["trash"])
    bot2 = FakeBot(strict=False)                                          # 재시작 — 휴지 폴더를 세션으로 읽지 않음
    await core.restore_sessions_from_disk(bot2)
    assert not any(getattr(v, "session_id", None) == s.session_id for v in bot2.active_sessions.values())
    monkeypatch.setattr(SR.shutil, "rmtree", real)
    rep2 = await SR.run(bot2, s.session_id)
    assert rep2["done"] and not os.path.exists(cp["trash"]) and len(_refunds()) == 1


async def test_restart_with_approved_open_session_holds_it_and_resume_settles_once(fresh, cbot, clock):
    s = fresh
    _wire(cbot, s)
    await _open(cbot, s)
    _approve(cbot, s, SR.SCOPE_DATA)                                       # 승인 직후 크래시(종료 전)
    os.makedirs("scenarios", exist_ok=True)
    json.dump(s.scenario_data, open(f"scenarios/{s.scenario_id}.json", "w", encoding="utf-8"), ensure_ascii=False)
    bot2 = FakeBot(strict=False)
    bot2.cost_ledger = cbot.cost_ledger
    await core.restore_sessions_from_disk(bot2)
    assert not any(getattr(v, "session_id", None) == s.session_id for v in bot2.active_sessions.values())
    held = bot2.removal_held[s.session_id]
    assert held.world_lifecycle not in WL.ENDING
    rep = await SR.run(bot2, s.session_id)
    assert rep["done"], rep
    assert len(_refunds()) == 1 and not os.path.exists(f"sessions/{s.session_id}")
    assert s.session_id not in (bot2.removal_held or {})


# ══════════════════════════════════════════════════════════════
#  권한 · 목록 · !채널정리 보호
# ══════════════════════════════════════════════════════════════

def test_permission_scope_creator_owns_only_own_and_unknown_is_operator_only():
    bot = FakeBot(strict=False)
    mine = {"session_id": "a", "creator_uid": "111", "guild_id": GUILD}
    theirs = {"session_id": "b", "creator_uid": "222", "guild_id": GUILD}
    legacy = {"session_id": "c", "creator_uid": "", "guild_id": GUILD}
    unknown = {"session_id": "d", "creator_uid": "111", "guild_id": None}
    other_guild = {"session_id": "e", "creator_uid": "111", "guild_id": 1}
    v = lambda m, op: SR.visible(bot, m, GUILD, 111, is_operator=op)     # noqa: E731
    assert v(mine, False) and not v(theirs, False) and not v(legacy, False)
    assert not v(unknown, False) and v(unknown, True)                    # 길드 불명 = 운영자 전용
    assert not v(other_guild, False) and not v(other_guild, True)         # 다른 길드 세션은 보이지 않음
    assert v(theirs, True) and v(legacy, True)


async def test_entries_and_owned_ids_cover_disk_sessions_and_checkpoints(fresh, cbot, clock):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    await _open(cbot, s)
    owned = SR.owned_resource_ids(cbot)
    assert {s.game_ch_id, s.master_ch_id, s.display_ch_id, s.voice_ch_id, cat.id} <= owned
    e = SR.entries(cbot)[s.session_id]
    assert e["guild_id"] == GUILD and e["category_id"] == cat.id


def test_channel_cleanup_command_excludes_session_resources():
    src = open(os.path.join(os.path.dirname(core.__file__), "..", "cogs", "system.py"), encoding="utf-8").read()
    body = src[src.index('name="채널정리"'):src.index('name="세션관리"')]
    assert "owned_resource_ids" in body and "!세션관리" in body


def test_management_uses_single_close_path_and_new_sessions_persist_ids():
    src = open(SR.__file__, encoding="utf-8").read()
    assert "_wl.close_session(" in src and "close_window(" not in src     # 종료·환급 단일 경로 재사용
    sess = open(os.path.join(os.path.dirname(core.__file__), "..", "cogs", "session.py"), encoding="utf-8").read()
    assert "session.guild_id = guild.id" in sess and "session.category_id = category.id" in sess
    assert "세션관리" in __import__("cogs.permissions", fromlist=["x"]).EXEMPT_COMMANDS


# ══════════════════════════════════════════════════════════════
#  화면 — 확인은 정확히 1회, 재개는 확인 없음, 실행자·권한 재검사
# ══════════════════════════════════════════════════════════════

class _Resp:
    def __init__(self, log):
        self.log = log

    async def edit_message(self, **kw):
        self.log.append(("edit", kw))

    async def send_message(self, *a, **kw):
        self.log.append(("send", a, kw))


class _Inter:
    def __init__(self, uid, log):
        self.user = types.SimpleNamespace(id=uid)
        self.response = _Resp(log)
        self.log = log

    async def edit_original_response(self, **kw):
        self.log.append(("final", kw))


def _labels(view):
    return [getattr(i, "label", None) for i in view.children]


async def test_view_shows_single_confirmation_and_resume_needs_none(fresh, cbot, clock, monkeypatch):
    s = fresh
    _wire(cbot, s)
    s.creator_uid = "555"
    await _open(cbot, s)
    monkeypatch.setattr(SR, "is_operator", lambda bot, user: _false())
    author = types.SimpleNamespace(id=555, send=None)
    view = SR.ManageView(cbot, author, types.SimpleNamespace(id=GUILD))
    assert await view.prepare() and s.session_id in view.metas
    log = []
    assert not await view.interaction_check(_Inter(999, log))            # 다른 사람 조작 거부
    view.sid = s.session_id
    await view.show_confirm(_Inter(555, log))                             # 범위 선택 화면
    view.scope = SR.SCOPE_CHANNELS
    await view.show_confirm(_Inter(555, log))                             # 확인 화면(1회)
    assert _labels(view) == ["진행", "취소"]
    await view._proceed(_Inter(555, log))
    confirms = [e for e in log if e[0] == "edit" and getattr(e[1].get("embed"), "title", "") == "⚠️ 세션 정리 확인"]
    assert len(confirms) == 1
    assert SR.read_checkpoint(s.session_id) is None and len(_refunds()) == 1

    # 다른 생성자는 목록·조작 모두 불가(서버 측 재검사)
    stranger = SR.ManageView(cbot, types.SimpleNamespace(id=777), types.SimpleNamespace(id=GUILD))
    assert not await stranger.prepare()


async def test_view_resume_runs_without_new_confirmation(fresh, cbot, clock, monkeypatch):
    s = fresh
    g, cat, chans = _wire(cbot, s)
    s.creator_uid = "555"
    await _open(cbot, s)
    chans[s.display_ch_id].fail_delete = RuntimeError("API 실패")
    _approve(cbot, s, SR.SCOPE_CHANNELS, actor="555")
    assert not (await SR.run(cbot, s.session_id))["done"]
    chans[s.display_ch_id].fail_delete = None
    monkeypatch.setattr(SR, "is_operator", lambda bot, user: _false())
    view = SR.ManageView(cbot, types.SimpleNamespace(id=555, send=None), types.SimpleNamespace(id=GUILD))
    assert await view.prepare()
    log = []
    view.sid = s.session_id
    await view.show_confirm(_Inter(555, log))
    assert _labels(view) == ["재개", "닫기"]                               # 승인된 작업 — 확인 창 없음
    await view._resume(_Inter(555, log))
    assert SR.read_checkpoint(s.session_id) is None and len(_refunds()) == 1


async def _false():
    return False
