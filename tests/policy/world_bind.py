# -*- coding: utf-8 -*-
"""테스트 픽스처 — H06 생애주기를 거친 것과 같은 DYNAMIC 세션 상태(스냅샷 바인딩 + OPEN)를 만든다.

H06부터 세계 모드는 세션 정의 스냅샷만 읽고(재컴파일 대체 없음), 생애주기 OPEN을 요구한다.
같은 테스트에서 정의를 바꿔 다시 만들 때는 기존 스냅샷을 지운다(테스트 전용 — 운영은 migration 필요).
"""
from __future__ import annotations

import os

from core import definition_snapshot as SNAP
from core import world_lifecycle as WL


def bind_open(s, res):
    path = SNAP.snapshot_path(s.session_id)
    if os.path.exists(path):
        os.remove(path)
    SNAP.write_snapshot(s.session_id, res)
    s.definition_revision = res.revision
    s.world_lifecycle = WL.LIFECYCLE_OPEN
    return res.revision
