"""선언 검증(v7.23 조건부 축소 선언 검증) — 선언 게이트 규칙·응답 조각.

T-6: 레거시 서사 방향 시뮬레이터(방향성·공리·개연성 등급)를 은퇴하고, 선언 검증이 쓰던 두 조각만 문구 그대로 남긴다
(옛 NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION의 [선언 게이트 규칙] 이후 · NARRATIVE_DIRECTION_SCHEMA의 declaration_gate)."""

DECLARATION_GATE_RULES = (
    "[선언 게이트 규칙 — declaration_gate]\n"
    "══════════════════════════════════\n"
    "플레이어는 자기 PC가 무엇을 '시도'하는지의 권위자이지, 결과·NPC 반응·세계 사실의 권위자가 아니다.\n"
    "classification\n"
    "  · ORDINARY — 일반 행동 선언. resolution=CONTINUE.\n"
    "  · IMPROPER_ASSERTION — 행동의 성공·NPC의 동의/반응·사건 결론·세계 사실을 플레이어가 스스로 확정한 선언.\n"
    "    시도가 분명하고 안전하면 resolution=CONTINUE(그 시도만 진행 — 선확정 결과는 무시된다).\n"
    "    시도가 모호하거나 중요한 세계 사실·NPC 의사를 확정하려 하면 BLOCK(CANNOT_CONFIRM) 또는 CLARIFY(UNCLEAR_INTENT).\n"
    "  · AMBIENT_PROPOSAL — 지금 장소에서 주변의 작은 물건(노끈·막대기 등)이나 무해한 환경 요소(틈·마모 흔적 등)를\n"
    "    찾겠다는 제안.\n"
    "    - 핵심 사건 증거·비밀 통로/숨은 길·유일하거나 고유한 물건·NPC의 숨은 동기·과거 사건을 증명하는 흔적·희소 자원은\n"
    "      주변 요소가 아니다 → BLOCK(NOT_AMBIENT). 룰북·현재 상태와 충돌하면 BLOCK(WORLD_CONFLICT).\n"
    "    - 이 장소에 있을 물리적 이유가 충분하고 거의 확실하면 ACCEPT_NO_ROLL, 있을 법하나 불확실하면 ROLL.\n"
    "    - 무엇을 어디서 찾는지 알 수 없으면 CLARIFY(UNCLEAR_INTENT).\n"
    "    - [이 장소에서 찾지 못한 이전 탐색]과 대상·범위·방법·도구·상황이 실질적으로 같으면 BLOCK(UNCHANGED_RETRY).\n"
    "      시간만 흐른 것은 변화가 아니다. 무엇이 달라졌다면 다시 ROLL할 수 있다.\n"
    "    - [이 장소에서 PC가 이미 발견한 것]과 같은 대상이면 existing_key에 그 키를 쓰고 ACCEPT_NO_ROLL.\n"
    "    - discovery_target은 플레이어 문장에 실제로 있는 낱말만(새 이름을 만들지 말 것), discovery_kind는 물건이면 OBJECT.\n"
    "reason_code는 공개 가능한 범주일 뿐이다 — 비밀의 존재·위치·정체를 어떤 필드에도 쓰지 말 것."
)


DECLARATION_GATE_SCHEMA = {
    "type": "object",
    "description": "이번 플레이어 선언의 권한·발견 판정. [선언 게이트 규칙]을 따른다.",
    "properties": {
        "classification": {"type": "string",
                           "enum": ["ORDINARY", "AMBIENT_PROPOSAL", "IMPROPER_ASSERTION"]},
        "resolution": {"type": "string",
                       "enum": ["CONTINUE", "ACCEPT_NO_ROLL", "ROLL", "BLOCK", "CLARIFY"]},
        "reason_code": {"type": "string",
                        "enum": ["NONE", "CANNOT_CONFIRM", "WORLD_CONFLICT", "UNCLEAR_INTENT",
                                 "NOT_AMBIENT", "UNCHANGED_RETRY"]},
        "discovery_target": {"type": "string",
                             "description": "AMBIENT_PROPOSAL일 때 찾으려는 대상 — 플레이어 문장의 낱말 그대로(2~15자). 그 밖에는 빈 문자열"},
        "discovery_kind": {"type": "string", "enum": ["OBJECT", "ENVIRONMENT", "NONE"]},
        "existing_key": {"type": "string",
                         "description": "[이 장소에서 PC가 이미 발견한 것]에 같은 대상이 있으면 그 키(D1…), 없으면 빈 문자열"}
    },
    "required": ["classification", "resolution", "reason_code", "discovery_target",
                 "discovery_kind", "existing_key"]
}
