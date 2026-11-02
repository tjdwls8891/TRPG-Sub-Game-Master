"""커밋 확정 훅 입력 수집기(리팩터링 T-6).

커밋 한 번(CommitCoordinator._apply_in_memory) 동안 옛 판정 경로가 계산한 '확정 재료'(상태 판정 인자·물건 계획·
사건 지급·동행 정규화·성장 재료)를 받아 둔다. 커널 커밋(kernel_commit)이 이 재료로 기능 모듈의 확정 훅을 돌려
정본 세계를 만든다 — 규칙의 적용 주인은 커널 훅이고, 여기 모이는 것은 관찰·판정의 해석 결과다.
수집 구간 밖의 호출은 아무것도 남기지 않는다.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy

_CUR: contextvars.ContextVar = contextvars.ContextVar("indaim_settle_capture", default=None)


def note(kind: str, payload) -> None:
    rec = _CUR.get()
    if rec is not None:
        rec.setdefault(kind, []).append(copy.deepcopy(payload))


def active() -> bool:
    return _CUR.get() is not None


@contextlib.contextmanager
def capturing():
    token = _CUR.set({})
    try:
        yield _CUR.get()
    finally:
        _CUR.reset(token)


@contextlib.contextmanager
def paused():
    """수집 중 재진입(커널 훅 자신의 판정 호출 등)을 세지 않는다."""
    token = _CUR.set(None)
    try:
        yield
    finally:
        _CUR.reset(token)
