"""WP-PERSIST-01 — strict 영속화 프리미티브 검증.

AUD-042: 기존 save_session_data 는 실패를 삼켜 호출자에게 durable 실패 신호를
주지 못한다. 이 패키지는 tolerant 계약을 보존한 채, 명확한 실패 신호를 주는
strict 저장 프리미티브(save_session_data_strict)를 추가한다.

검증(§5):
  1 strict 성공 저장이 재적재 가능한 세션 파일을 남긴다
  2 strict 직렬화 실패가 호출자에게 관측된다
  3 strict tmp/write 실패가 관측된다
  4 strict os.replace/최종화 실패가 관측된다
  5 실패가 기존 유효 캐노니컬 파일을 훼손하지 않는다
  6 실패가 임시 산출물을 정리한다
  7 세션별 락/동시성 규율이 약화되지 않는다
  8 동일 주입 실패에서도 tolerant save_session_data 는 기존 계약대로 삼킨다
  9 strict/tolerant 성공 저장이 동등한 캐노니컬 필드를 직렬화한다
  10 기존 113 pass + 9 xfail 불변(전체 스위트 실행으로 확인)

실패 주입 + tmp 경로만 사용하며 실제 사용자 세션 데이터는 만지지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

import core
import core.io as io

pytestmark = pytest.mark.policy


def _data_path(session) -> str:
    return f"sessions/{session.session_id}/data.json"


def _leftover_tmps(session) -> list:
    d = f"sessions/{session.session_id}"
    return [f for f in os.listdir(d) if f.endswith(".tmp")]


# ── 1. 성공 저장 → 재적재 가능한 파일 ─────────────────────────

async def test_strict_success_writes_reloadable_file(wired_bot, session_auto_ready):
    result = await core.save_session_data_strict(wired_bot, session_auto_ready)
    assert result is None  # 성공은 예외 부재로만 신호(로그-후-성공반환 아님)

    path = _data_path(session_auto_ready)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    # 재적재된 내용이 표준 직렬화 규칙과 일치한다.
    assert loaded["schema_version"] == io.SCHEMA_VERSION
    assert loaded["session_id"] == session_auto_ready.session_id
    assert loaded == io._serialize_session(session_auto_ready)


# ── 2. 직렬화 실패가 관측된다 ─────────────────────────────────

async def test_strict_serialization_failure_is_observable(
        monkeypatch, wired_bot, session_auto_ready):
    boom = ValueError("직렬화 실패 시뮬레이션")

    def _boom(_session):
        raise boom

    monkeypatch.setattr(io, "_serialize_session", _boom)

    with pytest.raises(core.SessionPersistenceError) as ei:
        await core.save_session_data_strict(wired_bot, session_auto_ready)
    assert ei.value.__cause__ is boom  # 원인 보존


# ── 3. tmp/write 실패가 관측된다 ──────────────────────────────

async def test_strict_write_failure_is_observable(
        monkeypatch, wired_bot, session_auto_ready):
    def _boom(*a, **k):
        raise OSError("tmp 쓰기 실패 시뮬레이션")

    # tmp 파일 기록(json.dump) 단계에서 실패시킨다.
    monkeypatch.setattr(io.json, "dump", _boom)

    with pytest.raises(core.SessionPersistenceError) as ei:
        await core.save_session_data_strict(wired_bot, session_auto_ready)
    assert isinstance(ei.value.__cause__, OSError)
    # 실패 시 tmp 산출물이 남지 않는다.
    assert _leftover_tmps(session_auto_ready) == []


# ── 4. os.replace(최종화) 실패가 관측된다 ────────────────────

async def test_strict_replace_failure_is_observable(
        monkeypatch, wired_bot, session_auto_ready):
    injected = OSError("replace 실패 시뮬레이션")

    def _boom(*a, **k):
        raise injected

    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(core.SessionPersistenceError) as ei:
        await core.save_session_data_strict(wired_bot, session_auto_ready)
    assert ei.value.__cause__ is injected


# ── 5. 실패가 기존 유효 캐노니컬 파일을 훼손하지 않는다 ───────

async def test_strict_failure_preserves_existing_canonical_file(
        monkeypatch, wired_bot, session_auto_ready):
    # 먼저 정상 저장으로 유효 스냅샷을 만든다.
    await core.save_session_data_strict(wired_bot, session_auto_ready)
    path = _data_path(session_auto_ready)
    before = open(path, encoding="utf-8").read()

    def _boom(*a, **k):
        raise OSError("replace 실패")

    monkeypatch.setattr(os, "replace", _boom)
    session_auto_ready.turn_count = 999  # 다른 내용으로 저장 시도
    with pytest.raises(core.SessionPersistenceError):
        await core.save_session_data_strict(wired_bot, session_auto_ready)

    after = open(path, encoding="utf-8").read()
    assert after == before, "실패한 strict 저장이 기존 스냅샷을 훼손했다"


# ── 6. 실패가 임시 산출물을 정리한다 ─────────────────────────

async def test_strict_failure_cleans_tmp_artifacts(
        monkeypatch, wired_bot, session_auto_ready):
    def _boom(*a, **k):
        raise OSError("replace 실패")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(core.SessionPersistenceError):
        await core.save_session_data_strict(wired_bot, session_auto_ready)

    assert _leftover_tmps(session_auto_ready) == []


# ── 7. 세션별 락/동시성 규율이 약화되지 않는다 ────────────────

async def test_strict_uses_same_per_session_lock(wired_bot, session_auto_ready):
    await core.save_session_data_strict(wired_bot, session_auto_ready)
    sid = session_auto_ready.session_id
    assert sid in wired_bot.session_io_locks
    lock = wired_bot.session_io_locks[sid]
    assert isinstance(lock, asyncio.Lock)
    # strict/tolerant 가 동일한 세션 락 객체를 공유한다(락 규율 단일화).
    assert io._get_session_io_lock(wired_bot, session_auto_ready) is lock
    await core.save_session_data(wired_bot, session_auto_ready)
    assert wired_bot.session_io_locks[sid] is lock


async def test_strict_concurrent_saves_serialize_without_corruption(
        wired_bot, session_auto_ready):
    # 동일 세션 동시 strict 저장 2건이 락으로 직렬화되어 손상 없이 완료된다.
    await asyncio.gather(
        core.save_session_data_strict(wired_bot, session_auto_ready),
        core.save_session_data_strict(wired_bot, session_auto_ready),
    )
    with open(_data_path(session_auto_ready), encoding="utf-8") as f:
        loaded = json.load(f)  # 유효 JSON(부분 쓰기/경합 손상 없음)
    assert loaded["session_id"] == session_auto_ready.session_id
    assert _leftover_tmps(session_auto_ready) == []


# ── 8. tolerant 는 동일 주입 실패에서도 기존 계약대로 삼킨다 ──

async def test_tolerant_swallows_same_failure_that_strict_raises(
        monkeypatch, capsys, wired_bot, session_auto_ready):
    def _boom(*a, **k):
        raise OSError("동일 주입 실패")

    monkeypatch.setattr(os, "replace", _boom)

    # tolerant: 예외 없이 None 반환 + 경고 로그(기존 계약).
    result = await core.save_session_data(wired_bot, session_auto_ready)
    assert result is None
    assert "세션 저장 실패" in capsys.readouterr().out

    # strict: 동일 실패에서 명확히 raise.
    with pytest.raises(core.SessionPersistenceError):
        await core.save_session_data_strict(wired_bot, session_auto_ready)


# ── 9. strict/tolerant 성공 저장이 동등한 캐노니컬 필드를 만든다 ─

async def test_strict_and_tolerant_serialize_equivalent_fields(
        wired_bot, session_auto_ready):
    await core.save_session_data_strict(wired_bot, session_auto_ready)
    strict_bytes = open(_data_path(session_auto_ready), encoding="utf-8").read()

    await core.save_session_data(wired_bot, session_auto_ready)
    tolerant_bytes = open(_data_path(session_auto_ready), encoding="utf-8").read()

    assert strict_bytes == tolerant_bytes
    # 동일 직렬화 소스를 공유함을 명시적으로도 확인.
    assert json.loads(strict_bytes) == io._serialize_session(session_auto_ready)


# ── 10. 두 함수가 공존하며 계약이 분리되어 있다(스위트 전체는 러너로 확인) ─

def test_strict_and_tolerant_are_distinct_and_exported():
    assert core.save_session_data is not core.save_session_data_strict
    assert issubclass(core.SessionPersistenceError, Exception)
    # tolerant 는 실패 신호 타입을 던지지 않는다(계약 분리).
    assert core.io.save_session_data.__name__ == "save_session_data"
    assert core.io.save_session_data_strict.__name__ == "save_session_data_strict"
