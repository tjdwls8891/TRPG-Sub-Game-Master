"""리팩터링 T-5: 시스템 지시문·응답 스키마의 실제 위치는 indaim/content/prompts/ 패키지(층위별 모듈 — 문구 바이트 동일,
불가침 원칙 그대로 · tests/content/test_prompts_identity.py).

이 모듈은 그 모듈과 같은 객체를 돌려주는 별칭이다(`import prompts`·`from prompts import X`·monkeypatch가 모두 같은 객체).
"""
import sys

from indaim.content import prompts as _p

sys.modules[__name__] = _p
