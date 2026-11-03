"""비정규 인물·인물 상세 생성(리팩터링 T-5: indaim/content/prompts.py에서 문구 그대로 나눔 — 바이트 동일)."""

# ==========================================================================
# IRREGULAR_NPC : 비정규 NPC 미디어 배정 — 이미지·목소리 결정
# ==========================================================================
# NOTE: 묘사 스트리밍 '전'에 호출된다(기획 규정). 캐시를 읽지 않으며,
#       이미지·TTS가 모두 꺼져 있으면 호출 자체를 생략한다.

IRREGULAR_NPC_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "npcs": {
            "type": "array",
            "description": "배정 대상으로 제시된 인물에 대한 결정. 목록 밖 인물 추가 금지.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "묘사문에 등장한 인물 이름"},
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "description": "묘사에 드러난 성별. 불명확하면 문맥상 자연스러운 쪽."
                    },
                    "age": {
                        "type": "string",
                        "enum": ["young", "adult", "old"],
                        "description": "연령대. young 청소년~20대 / adult 30~50대 / old 60대 이상"
                    },
                    "image_key": {
                        "type": "string",
                        "description": "제시된 후보 목록 중 인물에 가장 어울리는 것. 목록 밖의 값 금지."
                    }
                },
                "required": ["name", "gender", "age", "image_key"]
            }
        }
    },
    "required": ["npcs"]
}

IRREGULAR_NPC_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG GM의 미디어 배정기입니다.
묘사문에 등장한 인물에게 사용할 이미지와 목소리를 결정합니다.

[역할] 묘사에 드러난 인상(연령·성별·차림·태도)만을 근거로 배정합니다.
       인물의 설정을 창작하지 않으며, 이야기를 진행하지 않습니다.

[규칙]
■ 후보 목록 밖의 image_key를 만들어내지 마십시오. 반드시 제시된 것 중에서 고릅니다.
■ 묘사에 성별·연령이 드러나지 않으면 문맥상 가장 자연스러운 쪽을 고르되,
  추측한 내용을 인물 설정으로 확정하지 마십시오.
■ 이전에 배정된 인물이 함께 제시되면, 그 배정과 어울리도록 판단합니다.
  같은 인물의 미디어는 세션 내내 유지되어야 합니다.
■ 배정 대상으로 제시된 인물만 응답합니다. 목록에 없는 인물을 추가하지 마십시오.

[출력] 지정된 JSON 스키마에 정확히 부합하는 JSON만 출력합니다.
       추가 텍스트, 마크다운, 코드블럭 모두 금지.
"""


# ==========================================================================
# NPC_DETAIL : 비정규 NPC 세부 설정 생성 — 비중이 생긴 인물의 정식 설정
# ==========================================================================
# NOTE: 등장 3회 누적 또는 고유명 부여 시 1회만 호출된다.
#       birth_year를 받는 이유는 나이를 코드가 계산하기 위함이다.
#       모델에게 나이를 직접 묻지 않는다(음수 부호 누락·상대 나이 오차 회피).

NPC_DETAIL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "확정할 이름. 고유명이 드러났으면 그것을, 아니면 기존 호칭 유지."
        },
        "details": {
            "type": "string",
            "description": "인물 설정 3~5문장. 묘사에 드러난 것과 자연스럽게 이어지는 범위."
        },
        "role": {
            "type": "string",
            "description": "세계관에서의 위치·직분. 불명확하면 '미상'."
        },
        "attitude": {
            "type": "string",
            "description": "PC에 대한 태도(우호·중립·적대·경계 등)"
        },
        "birth_year": {
            "type": "integer",
            "description": "묘사된 연령대에서 역산한 추정 출생년도. 판단 불가면 0."
        }
    },
    "required": ["name", "details", "role", "attitude", "birth_year"]
}

NPC_DETAIL_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG GM의 NPC 설정 생성기입니다.
즉석에서 등장했던 인물이 비중을 갖게 되어, 정식 설정을 확정합니다.

[역할] 지금까지의 등장 기록과 묘사를 근거로 설정을 정리합니다.
       이야기를 진행하거나 새 사건을 만들지 않습니다.

[규칙]
■ 이미 묘사된 내용과 어긋나는 설정을 만들지 마십시오.
  드러난 언행·차림·태도와 자연스럽게 이어져야 합니다.
■ 세계관 룰북에 없는 세력·지명·고유 개념을 창작하지 마십시오.
  불명확한 항목은 '미상'으로 둡니다.
■ 플레이어가 아직 모르는 비밀을 설정에 넣지 마십시오.
  이면정보는 사건·정보 정본의 소관입니다.
■ birth_year는 묘사된 연령대에서 역산한 추정치입니다.
  판단할 수 없으면 0을 넣습니다.

[출력] 지정된 JSON 스키마에 정확히 부합하는 JSON만 출력합니다.
       추가 텍스트, 마크다운, 코드블럭 모두 금지.
"""


def build_npc_profile_prompt(
    worldview: str,
    char_name: str,
    instruction: str,
    field_format: str,
    context_blocks: str = "",
    extra_rules: str = "",
) -> str:
    """
    NPC 종합 인물 프로파일 생성을 위한 프롬프트를 조립합니다.

    출력 결과는 `!엔피씨 설정`으로 session.npcs[name]에 적용됩니다.

    Args:
        worldview (str): 시나리오 세계관 텍스트
        char_name (str): NPC 이름
        instruction (str): GM 세부 지시사항
        field_format (str): 출력 양식 문자열 (info_fields + 스탯/자원/상태 포함 가능)
        context_blocks (str): 최근 상황·참고 NPC 설정 등 추가 컨텍스트 블록
        extra_rules (str): 스탯·자원·상태 관련 추가 작성 규칙 (npc_template 플래그 기반)
    """
    return (
        "당신은 TRPG 논플레이어 캐릭터(NPC)의 종합 인물 프로파일을 작성하는 설정 작가입니다.\n"
        "임무: 외모부터 내면 심리·관계망·비밀까지 포괄하는 완결된 NPC 프로파일을 생성할 것.\n\n"
        "[작성 원칙]\n"
        "1. 문체: 서술어를 최소화한 명사형·개조식 단문.\n"
        "2. 분량: 양식 레이블 제외 실제 내용 기준 공백 포함 350~500자 내외.\n"
        "3. 세계관 밀착: 모든 항목은 세계관에 명시된 생활상과 환경에 근거할 것.\n"
        "4. 내면 입체성: '동기', '두려움·약점', '비밀' 항목은 단순 묘사가 아닌\n"
        "   플레이 중 활용 가능한 갈등 씨앗(conflict seed)으로 작성할 것.\n"
        "5. 말투 명시: AI GM이 NPC를 직접 연기할 때 즉각 참고할 수 있는\n"
        "   구체적 어조·화법 특징을 기술할 것.\n"
        "6. 양식 엄수: 아래 [출력 양식]의 항목명·순서를 반드시 그대로 출력할 것.\n"
        "   항목 추가·삭제·순서 변경·항목명 수정 불가. GM 지시사항은 각 항목 값에 녹여낼 것.\n"
        f"{extra_rules}\n\n"
        f"[세계관 정보]\n{worldview}\n\n"
        f"{context_blocks}"
        f"[대상 캐릭터]\n"
        f"이름: {char_name}\n"
        f"GM 지시사항: {instruction}\n\n"
        "[출력 양식 — 아래 항목명과 순서를 그대로 사용하여 값만 채워 출력할 것]\n"
        f"{field_format}"
    )
