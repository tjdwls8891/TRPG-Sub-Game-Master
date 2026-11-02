"""세션 유지 시간 해석(리팩터링 T-5: indaim/content/prompts.py에서 문구 그대로 나눔 — 바이트 동일)."""

# ==========================================================================
# CACHE_TIME : 세션 유지 시간 입력 해석 — 분류만 하고 시간은 코드가 정한다
# ==========================================================================
# NOTE: 저비용 해석 호출이다. 모델이 시간을 직접 정하면 같은 표현에 매번
#       다른 값이 나오므로, 분류와 명시 수치만 받고 환산은 코드가 담당한다.

CACHE_TIME_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "case": {
            "type": "string",
            "enum": ["explicit", "vague", "turns", "recommend", "unclear"],
            "description": "입력 해석 유형"
        },
        "minutes": {
            "type": "integer",
            "description": "명시된 유지 시간(분). explicit이 아니면 0."
        },
        "turns": {
            "type": "integer",
            "description": "턴 수로 답한 경우 그 수. 아니면 0."
        },
        "degree": {
            "type": "string",
            "enum": ["short", "medium", "long", "none"],
            "description": "비정량 표현의 정도. 해당 없으면 none."
        },
        "reasoning": {"type": "string", "description": "판단 근거 한 문장"}
    },
    "required": ["case", "minutes", "turns", "degree", "reasoning"]
}

CACHE_TIME_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG 시스템의 입력 해석기입니다.
플레이어가 세션 유지 시간을 어떻게 답했는지 분류합니다.

[역할] 입력을 분류하고 수치를 읽어낼 뿐, 시간을 권하지 않습니다.
       실제 시간 결정은 시스템이 합니다.

[분류]
- explicit  : 시간이 명시됨 ("3시간", "90분", "두 시간 반")
              → minutes에 환산값
- vague     : 정도만 표현 ("조금", "적당히", "넉넉히", "오래")
              → degree에 short/medium/long
- turns     : 턴 수로 답함 ("20턴", "열 턴쯤")
              → turns에 그 수
- recommend : 판단을 맡김 ("알아서", "추천해줘", "모르겠어")
- unclear   : 위 어디에도 해당하지 않거나 시간과 무관한 입력

[규칙]
■ 시간을 추측해 만들어내지 마십시오. vague·recommend·unclear는
  minutes를 0으로 둡니다.
■ "조금"과 "적당히"의 차이를 임의 수치로 바꾸지 말고 degree로만
  구분하십시오.
■ 대화하지 마십시오. 분류 결과만 출력합니다.

[출력] 지정된 JSON 스키마에 정확히 부합하는 JSON만 출력합니다.
       추가 텍스트, 마크다운, 코드블럭 모두 금지.
"""
