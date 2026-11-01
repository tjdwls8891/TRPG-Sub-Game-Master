"""자동 GM 설정 상수 — 모델·재시도·반복 한도·결과 표식·고정 블록(cogs/gm.py에서 옮김)."""
import core
import os



JUDGMENT_MODEL = core.DEFAULT_MODEL
JUDGMENT_MAX_RETRIES = 2
EXTRACTION_MODEL = core.DEFAULT_MODEL
EXTRACTION_MAX_RETRIES = 2
EXTRACTION_THINKING_BUDGET = int(os.getenv("EXTRACTION_THINKING_BUDGET", "512"))
GM_LOGIC_MAX_RETRIES = 2
MAX_ITERATIONS_PER_MESSAGE = 5
COST_LOG_PREFIX = "[GM] "
GATE_UNVERIFIABLE_NOTICE = "지금은 그 탐색 결과를 확인할 수 없습니다. 다른 행동을 선언해 주십시오."
WORLD_REACTION_AUTHORITY_BLOCK = (
    "\n[세계 반응 권위 — 동적 세계]\n"
    "· 이 세션에는 서사 방향 시뮬레이션이 없다. 가능성 추측을 근거로 새 위협·사건이 '일어났다'고 정하지 말 것.\n"
    "· 장면 인물·감염자·환경의 즉시 반응(몸짓·소리·시선·짧은 대응)은 위 정본 블록과 직전 장면에서 개연적으로 "
    "추론해 지시할 수 있다 — 세계는 플레이어를 기다리지 않는다.\n"
    "· 경보 발령·추적 개시·포착·세력 상태 변화처럼 다음 턴까지 이어지는 결과는 진행 중인 사건·코드 확정 세계 진행·"
    "현재 장면 인물 정본에 근거가 있을 때만 사실로 지시한다. 근거가 없으면 확정하지 말고 PC가 실제로 지각한 것만 쓴다.\n"
    "· 판정·관찰로 이미 확인된 결론(직전 장면)을 새 근거 없이 뒤집지 말 것.\n"
)
_OUTCOME_READY = "READY"
_OUTCOME_NO_NARRATION = "NO_NARRATION"
_OUTCOME_DELIVERY_FAILED = "DELIVERY_FAILED"
_OUTCOME_EXTRACTION_FAILED = "EXTRACTION_FAILED"
_OUTCOME_NOT_READY = "NOT_READY"
_OUTCOME_SUPERSEDED = "SUPERSEDED"      # 더 새로운 시도가 현재 — 이 시도는 흐름을 소유하지 않음
_RETRY_MODE_PREPARATION = core.commit_coordinator.RETRY_MODE_PREPARATION
