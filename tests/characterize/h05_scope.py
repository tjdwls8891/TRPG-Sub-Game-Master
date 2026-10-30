# -*- coding: utf-8 -*-
"""H05 모듈 목록(특성화 스캔 공유). H05가 H02~H04 엔진의 유일한 플레이어 경로 소비자다.

자동 턴 진입(cogs/gm.py·cogs/game.py)은 H05 모듈만 부르고 H02~H04 도메인 모듈을 직접 부르지 않는다.
"""
H05_MODULES = ("action_intent", "player_world", "turn_world", "pursuit")   # v7.48: 추적자 계획(플레이어 턴 계획 소비)
H05_FILES = tuple(f"{m}.py" for m in H05_MODULES)
