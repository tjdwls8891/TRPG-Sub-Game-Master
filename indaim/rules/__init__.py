"""기능 모듈 — 각 모듈이 자기 컴포넌트·효과 연산(·이후 계획/확정 훅·화면 조각)을 가진다.

이식된 모듈(정본 효과 연산): time(시계·예약) · space(위치·보류·장면·접근·공간 지식) · actors(등록)
· knowledge(정보·계보·계열·인지) · relations(관계 delta) · events(인스턴스·변수·결단·기록).
"""
from indaim.kernel import Registry

from . import actors, events, knowledge, relations, space, time

FEATURES = (time, space, actors, knowledge, relations, events)


def register_all(registry: Registry) -> Registry:
    for f in FEATURES:
        for c in f.COMPONENTS:
            registry.register(c)
    return registry
