"""캐릭터 생성 — 검증·병합·복장 판독·외형(리팩터링 T-5: indaim/content/prompts.py에서 문구 그대로 나눔 — 바이트 동일)."""

# ==========================================================================
# PROFILE_AI : 프로필 생성 AI 모듈 — 검증(8번) · 병합(9번)
# ==========================================================================
# NOTE: 프로필 생성에서 API를 쓰는 유일한 둘이다.
#       기획 규정상 프로필 생성 단순호출은 무료 제공 대상이므로
#       비용은 집계하되 잉크 차감에서 제외한다.

PROFILE_VALIDATE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["ok", "warn", "reject"],
            "description": "ok 통과 / warn 통과하되 안내 / reject 재입력 필요"
        },
        "reason": {"type": "string", "description": "판정 사유 한두 문장. ok면 빈 문자열."}
    },
    "required": ["verdict", "reason"]
}

PROFILE_VALIDATE_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG의 캐릭터 생성 검증기입니다.
플레이어가 입력한 항목이 시나리오에 어울리는지 판정합니다.

[역할] 판정만 합니다. 대신 값을 만들어내거나 이야기를 진행하지 않습니다.

[판정 기준]
■ reject — 세계관과 명백히 충돌할 때만.
  다른 시대·장르의 고유명사, 실존 인물명, 시나리오가 금지한 요소.
■ warn — 어울리지 않으나 진행 가능할 때.
  통과시키되 이유를 알립니다.
■ ok — 그 외 전부.

[절대 원칙]
■ 취향을 판정하지 마십시오. 촌스럽다·평범하다는 reject 사유가 아닙니다.
■ 플레이어의 선택을 존중하십시오. 애매하면 ok입니다.
■ 제시된 시나리오 정보에 없는 규칙을 만들어내지 마십시오.
■ 대안 설정을 제시하거나 입력을 고쳐 쓰지 마십시오. reject면 무엇이 왜 충돌하는지만 설명합니다.

[출력] 지정된 JSON 스키마에 정확히 부합하는 JSON만 출력합니다.
       추가 텍스트, 마크다운, 코드블럭 모두 금지.
"""

PROFILE_MERGE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "merged": {"type": "string", "description": "합친 결과. 지정된 길이 제한을 지킨다."},
        "dropped": {
            "type": "array",
            "items": {"type": "string"},
            "description": "모순되어 반영하지 못한 입력. 없으면 빈 배열."
        }
    },
    "required": ["merged", "dropped"]
}

# v7.46 시작 복장 → 착용 물건(캐릭터 생성 최종 승인 뒤 1회, 무료 프로필 AI)
PROFILE_OUTFIT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "옷·착용물의 짧은 이름(복장 글의 표현 그대로, 괄호 기호 없이)"},
                "slot": {"type": "string", "description": "입는 자리 — 제시된 자리 목록 중 하나"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "그 물건이 실제로 가진 효과 — 제시된 태그 목록 안에서만. 효과가 없으면 빈 배열"}},
                "required": ["name", "slot", "tags"]},
            "description": "복장 글에 적힌 옷·착용물. 자리마다 하나"},
        "carried": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "[가진 물건] 목록의 이름 그대로"},
                "slots": {"type": "array", "items": {"type": "string"},
                          "description": "입거나 손에 들 수 있는 자리(제시된 자리 목록 안). 입거나 들지 않는 물건은 빈 배열"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "입거나 들 때의 효과 — 제시된 태그 목록 안에서만. 없으면 빈 배열"},
                "use": {"type": "array", "items": {"type": "string"},
                        "description": "써서 나는 효과 — 제시된 사용 효과 목록 안에서만. 없으면 빈 배열"}},
                "required": ["name", "slots", "tags", "use"]},
            "description": "[가진 물건] 목록이 있으면 그 물건마다 하나. 없으면 빈 배열"}
    },
    "required": ["items"]
}

PROFILE_OUTFIT_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG의 복장 판독기입니다.
캐릭터의 복장 글을 읽어, 지금 몸에 걸친 옷과 착용물을 자리별 물건으로 나눕니다.

[규칙]
■ 글에 적힌 것만 씁니다. 없는 옷·장비를 보태지 않습니다.
■ 자리마다 하나만 고릅니다(겹쳐 입은 경우 그 자리에서 가장 바깥 것). 손에 든 무기·도구는 넣지 않습니다(옷과 착용물만).
■ 이름은 글의 표현을 짧게 살립니다(예: '해진 검은 패딩' → '검은 패딩').
■ 효과 태그는 그 물건이 실제로 그 효과를 가질 때만 붙입니다. 얇은 티셔츠·속옷·평범한 바지처럼 특별한 효과가 없으면 빈 목록입니다.
  두꺼운 외투·방한복은 보온, 비옷·고무장화는 방수처럼 분명한 것만 붙입니다.
■ [가진 물건] 목록이 있으면 carried에 그 물건마다 자리·착용 효과·사용 효과를 정합니다. 붕대는 지혈·응급처치,
  생수는 수분 보충처럼 그 물건이 실제로 가진 효과만, 애매하면 비웁니다. 한 번 정하면 세션 내내 유지됩니다.
■ 응답은 지정된 JSON만 출력합니다."""

PROFILE_MERGE_SYSTEM_INSTRUCTION = """당신은 한국어 TRPG의 캐릭터 정보 정리기입니다.
여러 갈래로 받은 입력을 하나의 자연스러운 서술로 합칩니다.

[역할] 주어진 내용만 정리합니다. 없는 정보를 보태지 않습니다.

[규칙]
■ 입력에 없는 설정을 만들어내지 마십시오.
  '검은 머리'만 주어졌다면 눈 색이나 키를 지어내지 않습니다.
■ 플레이어가 쓴 표현을 최대한 살리십시오.
  문장을 매끄럽게 잇는 것이 목적이지 다시 쓰는 것이 아닙니다.
■ 서로 모순되는 입력이 있으면 나중 것을 채택하고 dropped에 적으십시오.
■ 사소한 연결어·문장 흐름·분위기 표현만 보탤 수 있습니다. 과거 사건·가족·인맥·직업 경력·장애·특별한 능력·
  핵심 동기처럼 플레이어가 정하지 않은 중요한 사실은 만들지 마십시오.
■ [조합 지침]이 있으면 따르되, 그것이 위 금지를 풀어 주지는 않습니다.
■ 지정된 길이를 넘기지 마십시오.

[출력] 지정된 JSON 스키마에 정확히 부합하는 JSON만 출력합니다.
       추가 텍스트, 마크다운, 코드블럭 모두 금지.
"""


# ========== [캐릭터 설정 생성] ==========

def build_pc_appearance_prompt(
    worldview: str,
    char_name: str,
    instruction: str,
    context_blocks: str = "",
) -> str:
    """
    PC 외형 설정 생성을 위한 프롬프트를 조립합니다.

    출력 결과는 session.players[uid]['appearance']에 저장되어
    프롬프트의 [외형]: {appearance} 필드에 직접 주입됩니다.

    Args:
        worldview (str): 시나리오 세계관 텍스트
        char_name (str): 캐릭터 이름
        instruction (str): GM 세부 지시사항
        context_blocks (str): 최근 상황·참고 NPC 설정 등 추가 컨텍스트 블록
    """
    return (
        "당신은 TRPG 플레이어 캐릭터(PC)의 외양(外樣)을 구체화하는 설정 작가입니다.\n"
        "임무: 오직 캐릭터의 외모만 묘사할 것. 성격·배경·능력·내면 정보는 절대 포함하지 않는다.\n\n"
        "[작성 원칙]\n"
        "1. 문체: 서술어를 최소화한 명사형·개조식 단문. 각 항목은 한 줄을 초과하지 않는다.\n"
        "2. 분량: 양식 레이블 제외 실제 내용 기준 공백 포함 130~230자 내외.\n"
        "3. 세계관 밀착: 복장과 외모 상태는 세계관의 복식과 상황을 반영할 것\n"
        "   (예: 피로와 오염이 배인 피부, 실용적이고 낡은 생존 복장 등).\n"
        "4. 양식 엄수: 아래 [출력 양식]의 항목명·순서를 반드시 그대로 출력할 것.\n"
        "   항목 추가·삭제·순서 변경·항목명 수정 불가. GM 지시사항은 각 항목 값에 녹여낼 것.\n\n"
        f"[세계관 정보]\n{worldview}\n\n"
        f"{context_blocks}"
        f"[대상 캐릭터]\n"
        f"이름: {char_name}\n"
        f"GM 지시사항: {instruction}\n\n"
        "[출력 양식 — 아래 항목명과 순서를 그대로 사용하여 값만 채워 출력할 것]\n"
        "**나이/성별**: \n"
        "**체형**: \n"
        "**얼굴**: \n"
        "**피부·헤어**: \n"
        "**복장**: \n"
        "**첫인상**: "
    )
