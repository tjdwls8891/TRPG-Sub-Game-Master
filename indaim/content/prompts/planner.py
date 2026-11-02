"""서사설계자 — 응답 스키마·시스템 지시(리팩터링 T-5: indaim/content/prompts.py에서 문구 그대로 나눔 — 바이트 동일)."""

# ========== [GM — 서사 계획] ==========

# 서사 계획 API 응답을 강제하는 JSON 스키마.
# 2단계 구조: mid_plan(중규모) + current_event/next_event(순간 계획)
NARRATIVE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "mid_plan": {
            "type": "object",
            "description": (
                "중규모 진행 계획 — 현재 장면이 속한 서사 단위 (전체 세션의 약 1~4할 분량). "
                "순간 계획(current/next_event)에 방향성을 부여한다. "
                "deviated 판정 시 순간 계획과 함께 폐기·재수립된다."
            ),
            "properties": {
                "title":    {"type": "string", "description": "중규모 단위 제목 (15자 이내)"},
                "overview": {"type": "string", "description": "이 단위에서 달성해야 할 전체 흐름 개요 (2~3문장)"},
                "milestones": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "이 중규모 단위 안에서 순서대로 거쳐야 할 이정표 (2~4개). 각 이정표 하나가 순간 사건 1~2개에 해당한다."
                },
                "end_condition": {
                    "type": "string",
                    "description": "이 중규모 계획이 완료되거나 다음 중규모 단위로 전환하는 조건"
                }
            },
            "required": ["title", "overview", "milestones", "end_condition"]
        },
        "current_event": {
            "type": "object",
            "description": "순간 계획 — 지금 당장 진행 중인 단일 사건. 1턴으로도 해결 가능한 소규모 단위.",
            "properties": {
                "title":                {"type": "string", "description": "사건 제목 (10자 이내)"},
                "summary":              {"type": "string", "description": "지금 일어나고 있는 상황 (1~2문장)"},
                "resolution_direction": {"type": "string", "description": "이 사건을 마무리할 자연스러운 방향 (플레이어 선택과 무관하게 수렴 가능한 결말)"},
                "progress":             {"type": "string", "description": "현재 진행 상황 한줄 메모 (초기값: '사건 시작 전', 재계획 시: 현재 상황 1문장)"}
            },
            "required": ["title", "summary", "resolution_direction", "progress"]
        },
        "next_event": {
            "type": "object",
            "description": "다음 순간 계획 — current_event 해결 직후 이어질 사건.",
            "properties": {
                "title":   {"type": "string", "description": "다음 사건 제목 (10자 이내)"},
                "summary": {"type": "string", "description": "다음 사건 개요 (1~2문장)"},
                "trigger": {"type": "string", "description": "이 사건이 시작되는 조건"}
            },
            "required": ["title", "summary", "trigger"]
        },
        "planner_notes": {
            "type": "string",
            "description": "계획 수립 시 고려한 특이사항·주의점 (선택적, 1문장)"
        }
    },
    "required": ["mid_plan", "current_event", "next_event"]
}

# 서사 계획 설계자 시스템 지시문.
NARRATIVE_PLANNER_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG 세션의 서사 계획 설계자입니다.
현재 시나리오 상황을 분석하여 GM이 활용할 수 있는 2단계 서사 계획을 수립합니다.

[계획 구조]
1. mid_plan (중규모 진행 계획): 현재 장면이 속한 서사 단위. 전체 세션 분량의 약 1~4할.
   순간 계획(current/next_event)에 방향성을 부여하는 상위 계획.
   deviated 판정 시 순간 계획과 함께 폐기·재수립된다.
2. current_event (현재 순간 계획): 지금 당장 진행 중인 단일 사건.
   단일 감염자 처치처럼 1턴으로도 해결 가능한 소규모 단위.
   mid_plan의 milestones 중 현재 해당하는 이정표를 향해 진행되어야 한다.
3. next_event (다음 순간 계획): current_event 해결 직후 이어질 사건.

[계획 수립 원칙]
0. 순간 사건 크기 (최우선): current_event는 최소 1턴, 최대 수 턴 내에 해결 가능한 단위여야 한다.
   예시) "방 안 감염자 1명 처치" = 1턴 사건 / "건물 탈출 경로 확보" = 2~3턴 사건
   거대한 목표('탈출', '시나리오 완료')를 current_event로 설정하지 않는다.
   current_event는 반드시 지금 플레이어가 당면한 즉각적 상황이어야 한다.
1. mid_plan은 여러 순간 사건을 아우르는 서사 단위다.
   milestones는 현재 사건부터 mid_plan 완료까지의 이정표를 순서대로 나열한다 (2~4개).
   각 이정표 하나가 순간 사건 1~2개에 해당한다.
2. resolution_direction은 플레이어가 어떤 선택을 하든 사건이 자연스럽게 수렴할 수 있는 방향.
   특정 선택 강요(~해야 한다)가 아닌, 상황이 흘러가는 방향(~로 향하고 있다)으로 서술한다.
3. next_event는 current_event의 결말 이후를 상정한다. 진행 중인 상황을 next_event로 넣지 않는다.
4. 완료 재계획(trigger=completed): 프롬프트에 제공된 mid_plan을 그대로 출력하되,
   next_event를 새 current_event로 승격하고 mid_plan의 다음 milestone을 목표로
   새 next_event를 수립한다.
5. 이탈 재계획(trigger=deviated/manual/init): mid_plan을 새로 수립하고,
   현재 플레이어의 실제 상황을 출발점으로 삼아 current_event와 next_event를 모두 재수립한다.
6. progress 필드: 초기 계획 시 '사건 시작 전'. 재계획 시 현재 상황 1문장 이내.

[출력 지시]
지정된 JSON 스키마를 정확히 따른다. 마크다운·코드블럭 없이 순수 JSON만 출력한다."""
