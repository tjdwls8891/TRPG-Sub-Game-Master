"""서사 방향 시뮬레이션·선언 검증 — 응답 스키마·시스템 지시(리팩터링 T-5: indaim/content/prompts.py에서 문구 그대로 나눔 — 바이트 동일)."""

# ========== [2단계 사고 서사 방향성 시뮬레이터 — 방안 6] ==========

NARRATIVE_DIRECTION_SCHEMA = {
    "type": "object",
    "properties": {
        # 개선 5 — 플레이어 행동의 세계 접촉점 (추론의 출발점)
        "action_world_contact": {
            "type": "string",
            "description": (
                "[STEP 1] 플레이어 행동이 세계관의 어떤 세력·규칙·시스템과 맞닿는지 분석. "
                "형식: '행동 → 접촉하는 세계관 요소 + 발동 가능한 메커니즘'. "
                "예: '큰 소음 발생 → 레인저 동맹 초소 감지 시스템과 접촉. "
                "구역 내 순찰 경보 체계 발동 가능성 진입.'"
            )
        },
        # 개선 2 — 세계 진실 명제 목록 (방향성 판단의 공리)
        "world_axioms": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "[STEP 2] 현재 상황에 적용되는 세계 진실 명제 목록. "
                "각 항목 형식: '[캐시 내 세계관 설정] → [현재 상황에서의 함의]'. "
                "이 공리들이 이후 반증 검토와 개연성 판단의 절대 기준이 된다. "
                "예: '[레인저 정착지 내부] → 외부 감염자 자연 침입 구조적 차단'"
            )
        },
        # 방향성 목록
        "directions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "방향성 제목 (10자 이내)"
                    },
                    "description": {
                        "type": "string",
                        "description": "이 방향성이 실현될 경우의 전개 요약 (60자 이내)"
                    },
                    # 개선 1 — 반증 우선 검토
                    "falsification_check": {
                        "type": "string",
                        "description": (
                            "[STEP 3] 반증 검토 결과. "
                            "이 방향을 불가능하게 만드는 world_axioms 항목이 존재하는가? "
                            "존재하면 해당 공리를 인용. 없으면 '반증 근거 없음'으로 기재."
                        )
                    },
                    "world_basis": {
                        "type": "string",
                        "description": (
                            "[STEP 4] 반증 통과 후, 이 방향이 가능한 세계관 논리 근거. "
                            "world_axioms 중 관련 항목을 반드시 인용하여 기술."
                        )
                    },
                    # 개선 4 — 명확한 개연성 5단계 + impossible 기준 강화
                    "plausibility": {
                        "type": "string",
                        "enum": ["high", "probable", "possible", "low", "impossible"],
                        "description": (
                            "[STEP 4] 세계관 논리 기반 개연성 등급. "
                            "high=가장 자연스러운 사건 / probable=흔한 사건 / "
                            "possible=추가 조건 필요 / low=현재 상황에서 드문 사건 / "
                            "impossible=반증 통과 실패 또는 전제 조건 물리적 불충족. "
                            "'드물다'는 이유만으론 impossible 불가."
                        )
                    },
                    "narrative_constraint": {
                        "type": "string",
                        "description": (
                            "이 방향이 제약·차단되는 세계관 논리. "
                            "impossible이면 반드시 명시. 제약 없으면 빈 문자열."
                        )
                    },
                    # 개선 3 — 방향 간 변별성 확인용 주 근거 요소
                    "primary_world_element": {
                        "type": "string",
                        "description": (
                            "[STEP 4] 이 방향의 주 세계관 근거 요소를 한 단어로 표기. "
                            "예: '레인저동맹', '감염자', '환경변화', '제3세력'. "
                            "방향들 간 primary_world_element 중복 불가."
                        )
                    }
                },
                "required": [
                    "title", "description", "falsification_check",
                    "world_basis", "plausibility", "narrative_constraint",
                    "primary_world_element"
                ]
            },
            "description": (
                "서사 방향성 2~3개. 개연성 높은 순 정렬. "
                "impossible 방향도 1개까지 포함 가능(지시층위에 차단 정보 제공). "
                "각 방향의 primary_world_element는 서로 달라야 한다."
            )
        },
        # v7.14 선언 게이트 — 역사칭·주변 발견 제안의 허용 범위(열거값 + 플레이어 원문 대상명만 코드가 쓴다)
        "declaration_gate": {
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
    },
    "required": ["action_world_contact", "world_axioms", "directions", "declaration_gate"]
}

NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION = (
    "당신은 TRPG 세계의 '서사 물리 법칙 시뮬레이터'입니다.\n"
    "플레이어 행동과 현재 세계 상태를 입력받아, 세계관 법칙과 세력 배치에 근거한\n"
    "서사 방향성들을 4단계 추론으로 평가하여 JSON으로 출력합니다.\n\n"

    "[핵심 임무 — 고차원 개연성 판단]\n"
    "당신의 판단은 단순한 인과 추론이 아닙니다. 세계관의 '구조적 법칙'에 근거한 서사 단위 개연성 판단입니다.\n"
    "✗ 단순 판단(금지): '소음이 발생했으니 감염자가 올 수 있다.'\n"
    "✓ 고차원 판단(요구): '현재 위치는 레인저 동맹 정착지 내부이므로, 외부 감염자의\n"
    "   자연 침입은 세계관 논리상 구조적으로 불가능하다. 소음 반응은 레인저 초소 병사가\n"
    "   확인 나오는 방향이 고개연성이다.'\n\n"

    "══════════════════════════════════\n"
    "[4단계 추론 절차 — 반드시 이 순서로 실행]\n"
    "══════════════════════════════════\n\n"

    "STEP 1 — 행동의 세계 접촉점 분석 → action_world_contact 출력\n"
    "  플레이어 행동이 세계관의 어떤 구조적 요소와 맞닿는지를 먼저 식별한다.\n"
    "  · 이 행동은 어떤 세력의 감지·반응 범위에 진입하는가?\n"
    "  · 어떤 지역 규칙 또는 세계관 시스템을 발동시키는가?\n"
    "  · 이 행동에 반응하도록 세계관상 설계된 메커니즘이 있는가?\n"
    "  → 이 접촉점이 이후 방향성 도출의 유일한 출발점이 된다.\n\n"

    "STEP 2 — 세계 진실 공리 추출 → world_axioms 출력\n"
    "  캐시 내 시나리오 설정에서 현재 상황에 적용되는 세계 진실 명제들을 추출한다.\n"
    "  · 형식: '[캐시 내 세계관 설정] → [현재 상황에서의 함의]'\n"
    "  · 예: '[레인저 정착지 내부] → 외부 감염자 자연 침입 구조적 차단'\n"
    "  · 예: '[야간 순찰 교대 시간] → 병사 집중도 일시 저하'\n"
    "  → 이 공리들이 STEP 3·4의 판단 기준이 된다.\n\n"

    "STEP 3 — 방향성 후보별 반증 우선 검토 → falsification_check 출력\n"
    "  각 방향성에 대해 '가능한가'가 아닌 '불가능한 공리가 있는가'를 먼저 물어라.\n"
    "  · world_axioms 중 이 방향을 차단하는 공리가 존재하는가?\n"
    "  · 이 방향의 전제 조건이 현재 세계 상태에서 물리적으로 불충족인가?\n"
    "  → 반증 발견: impossible 판정. narrative_constraint에 해당 공리 인용 필수. 이후 분석 중단.\n"
    "  → 반증 실패(반증 근거 없음): STEP 4로 진행.\n\n"

    "STEP 4 — 개연성 등급 부여 + 방향 간 변별성 확인 → 방향성 완성\n"
    "  [개연성 5단계]\n"
    "    high      — 이 상황에서 세계관상 가장 자연스럽게 발생하는 사건\n"
    "    probable  — 조건이 충족되며 흔히 발생하는 사건\n"
    "    possible  — 조건은 충족되나 특정 추가 조건이 더 필요한 사건\n"
    "    low       — 이론상 가능하나 현재 상황에서 드문 사건\n"
    "    impossible— 반증 통과 실패 또는 전제 조건 물리적 불충족에 한정.\n"
    "                '드물다'는 이유만으로는 impossible 판정 불가 — 그것은 low이다.\n\n"
    "  [방향 간 변별성 규칙]\n"
    "    각 방향의 primary_world_element(주 근거 요소)는 서로 달라야 한다.\n"
    "    같은 세력에서 파생된 유사한 결과 2개를 다른 방향으로 제시하는 것 금지.\n"
    "    다양성 기준: ① 개입 세력·주체가 다르거나 / ② 적용되는 규칙 체계가 다르거나\n"
    "                 ③ 사건 성격이 다른 경우 (NPC 반응 vs 환경 변화 vs 제3세력 개입)\n\n"

    "══════════════════════════════════\n"
    "[impossible 판정 명시적 트리거]\n"
    "══════════════════════════════════\n"
    "다음 중 하나라도 해당하면 impossible:\n"
    "① 캐시 설정이 해당 사건 발생을 구조적으로 차단하는 세력·규칙을 명시함\n"
    "② 해당 사건의 전제 조건이 현재 세계 상태에서 물리적으로 불충족\n"
    "③ 이 사건이 일어나려면 세계관의 확립된 규칙이 먼저 붕괴되어야 함\n\n"

    "[최종 출력 규칙]\n"
    "- 방향성 2~3개. impossible 방향도 1개까지 포함 가능 (GM에게 차단 정보 제공).\n"
    "- 개연성 높은 순 정렬 (impossible은 마지막).\n"
    "- 모든 world_basis와 falsification_check는 캐시 내 설정을 직접 인용할 것.\n\n"

    "══════════════════════════════════\n"
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
