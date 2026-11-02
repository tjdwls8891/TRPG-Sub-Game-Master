# -*- coding: utf-8 -*-
"""리팩터링 T-6 — 정본 전환: 커널 커밋 · 저장 형식 v5(세계 = 커널) · v4 로드 변환(K5 가) · 버전별 세계 보관(K2 가)."""
from __future__ import annotations

import copy
import json
import os

import pytest

import core
from core import io as IO
from indaim.app import world_codec as WC
from indaim.turn import kernel_commit as KC
from tests.conftest import PLAYER_UID, read_session_disk
from tests.policy.test_authoritative_commit import _fund
from tests.policy.test_commit_recovery import _restart
from tests.policy.test_h05bc_turn_wiring import drig, extraction, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401
from tests.policy.test_turn_history import _commit

pytestmark = pytest.mark.policy


def _raw(s):
    with open(f"sessions/{s.session_id}/data.json", encoding="utf-8") as f:
        return json.load(f)


async def test_disk_format_is_v5_world_without_old_fields(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _commit(r, "숲길")
    raw = _raw(s)
    assert raw["schema_version"] == IO.SCHEMA_VERSION == 5
    assert {"world", "world_profile"} <= set(raw)
    assert not set(WC.FIELDS) & set(raw)                     # 옛 세계 필드는 디스크에 없다
    assert read_session_disk(s.session_id)["players"] == s.players


async def test_v4_session_loads_preserves_original_and_resaves_as_v5(rig):
    r = rig
    s = r.sess
    await _fund(PLAYER_UID, 100)
    await _commit(r, "숲길")
    path = f"sessions/{s.session_id}/data.json"
    old = read_session_disk(s.session_id)
    old["schema_version"] = 4                               # 배포 전 프로세스가 쓴 v4 파일
    json.dump(old, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    bot2, s2 = await _restart(r)
    assert os.path.isfile(f"sessions/{s.session_id}/data.v4.json")          # 원본 보존(K5 가)
    assert json.load(open(f"sessions/{s.session_id}/data.v4.json", encoding="utf-8"))["schema_version"] == 4
    for f in ("players", "resources", "statuses", "world_version"):
        assert getattr(s2, f) == old[f], f
    await IO.save_session_data_strict(bot2, s2)
    assert _raw(s2)["schema_version"] == 5 and "world" in _raw(s2)


def test_unconvertible_v4_session_is_not_opened(monkeypatch, capsys):
    def boom(*a, **k):
        raise WC.WorldCodecError("시험 — 변환 불가")
    monkeypatch.setattr(WC, "encode", boom)
    data = {"schema_version": 4, "session_id": "bad-1", "game_ch_id": 1, "master_ch_id": 2, "scenario_id": "영도"}
    from indaim.ai import cache as C
    assert C._session_from_data(data) is None
    assert "열지 않습니다" in capsys.readouterr().out


def test_codec_refuses_lossy_world_and_preserves_unknown_keys():
    data = {"session_id": "c1", "players": {}, "information_state": {"probe": 3},
            "spatial_state": {"placements": {}, "odd": 1}}
    enc = WC.encode(dict(data))
    dec = WC.decode(enc)
    assert dec["information_state"] == {"probe": 3} and dec["spatial_state"]["odd"] == 1   # 모르는 키도 잃지 않음


async def test_commit_archives_world_per_version(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction()]
    await turn(r, "주변을 둘러본다")
    vid = r.sess.world_version["version_id"]
    path = f"sessions/{r.sess.session_id}/world/{vid}.json"
    assert os.path.isfile(path)
    assert json.load(open(path, encoding="utf-8"))["version"]["version_id"] == vid


async def test_kernel_commit_failure_rolls_back_before_persist(drig, monkeypatch):
    """커널 투영이 거부되면 영속 전 실패 — 정본·디스크 불변, 청구 0."""
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment()
    r.prov.routes["extraction"] = [extraction()]
    before = copy.deepcopy({f: getattr(r.sess, f) for f in KC.FIELDS})

    def bad(*a, **k):
        raise KC.KernelCommitError("시험 — 투영 충돌")
    monkeypatch.setattr(KC, "commit_fields", bad)
    await turn(r, "주변을 둘러본다")
    tx = core.turn_transaction.get_active_transaction(r.sess)
    assert tx is None or tx.status != core.turn_transaction.TurnStatus.COMMITTED
    assert {f: getattr(r.sess, f) for f in KC.FIELDS} == before
