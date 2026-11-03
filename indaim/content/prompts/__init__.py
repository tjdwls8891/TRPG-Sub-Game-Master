"""
TRPG 봇 프롬프트 중앙 등록소(Central Prompt Registry).

이 모듈은 봇 전역에서 사용되는 모든 AI 프롬프트 문자열을 단일 파일로 통합합니다.
프롬프트를 수정할 때는 이 파일만 편집하면 되며, 로직 파일에는 텍스트가 남지 않습니다.

포함 항목:
  - SYSTEM_INSTRUCTION          : 메인 GM 캐시 시스템 지시문
  - GM_LOGIC_RESPONSE_SCHEMA    : GM 의사결정 JSON 스키마
  - GM_LOGIC_SYSTEM_INSTRUCTION : GM 의사결정 시스템 지시문
  - build_narrate_prompt()      : GM 경량 묘사(NARRATE) 프롬프트 빌더
  - build_compression_prompt_text() : 기억 압축 프롬프트 빌더
  - build_pc_appearance_prompt()    : PC 외형 설정 생성 프롬프트 빌더
  - build_npc_profile_prompt()      : NPC 종합 프로파일 생성 프롬프트 빌더

WARNING: 이 모듈은 core/ 패키지를 임포트하지 않습니다(순환 임포트 방지).
         builder 함수는 순수 문자열 파라미터만 받습니다.
"""
# 리팩터링 T-5: 층위별 모듈로 나눴다(문구 바이트 동일 — tests/content/test_prompts_identity.py).
# 공개 이름은 예전 그대로 이 패키지에서 읽는다(`import prompts`·`from prompts import X`).
from .narration import (  # noqa: F401
    SYSTEM_INSTRUCTION, build_narrate_prompt)
from .judgment import (  # noqa: F401
    JUDGMENT_RESPONSE_SCHEMA, JUDGMENT_SYSTEM_INSTRUCTION)
from .instruction import (  # noqa: F401
    GM_LOGIC_RESPONSE_SCHEMA, GM_LOGIC_SYSTEM_INSTRUCTION)
from .extraction import (  # noqa: F401
    EXTRACTION_SYSTEM_INSTRUCTION)
from .declaration import (  # noqa: F401 — T-6: 레거시 서사 시뮬레이터·서사설계자 은퇴, 선언 검증 조각만
    DECLARATION_GATE_RULES, DECLARATION_GATE_SCHEMA)
from .npc import (  # noqa: F401
    IRREGULAR_NPC_RESPONSE_SCHEMA, IRREGULAR_NPC_SYSTEM_INSTRUCTION, NPC_DETAIL_RESPONSE_SCHEMA, NPC_DETAIL_SYSTEM_INSTRUCTION, build_npc_profile_prompt)
from .session import (  # noqa: F401
    CACHE_TIME_RESPONSE_SCHEMA, CACHE_TIME_SYSTEM_INSTRUCTION)
from .profile import (  # noqa: F401
    PROFILE_VALIDATE_RESPONSE_SCHEMA, PROFILE_VALIDATE_SYSTEM_INSTRUCTION, PROFILE_MERGE_RESPONSE_SCHEMA, PROFILE_OUTFIT_RESPONSE_SCHEMA, PROFILE_OUTFIT_SYSTEM_INSTRUCTION, PROFILE_MERGE_SYSTEM_INSTRUCTION, build_pc_appearance_prompt)
from .knowledge import (  # noqa: F401
    INITIAL_KNOWLEDGE_RESPONSE_SCHEMA, INITIAL_KNOWLEDGE_SYSTEM_INSTRUCTION)
from .memory import (  # noqa: F401
    COMPRESSION_RESPONSE_SCHEMA, ADAPTIVE_DENSITY_BLOCK, DENSE_FORM_BLOCK, build_compression_prompt_text)

__all__ = ['SYSTEM_INSTRUCTION', 'IRREGULAR_NPC_RESPONSE_SCHEMA', 'IRREGULAR_NPC_SYSTEM_INSTRUCTION', 'NPC_DETAIL_RESPONSE_SCHEMA', 'NPC_DETAIL_SYSTEM_INSTRUCTION', 'CACHE_TIME_RESPONSE_SCHEMA', 'CACHE_TIME_SYSTEM_INSTRUCTION', 'PROFILE_VALIDATE_RESPONSE_SCHEMA', 'PROFILE_VALIDATE_SYSTEM_INSTRUCTION', 'PROFILE_MERGE_RESPONSE_SCHEMA', 'PROFILE_OUTFIT_RESPONSE_SCHEMA', 'PROFILE_OUTFIT_SYSTEM_INSTRUCTION', 'PROFILE_MERGE_SYSTEM_INSTRUCTION', 'INITIAL_KNOWLEDGE_RESPONSE_SCHEMA', 'INITIAL_KNOWLEDGE_SYSTEM_INSTRUCTION', 'EXTRACTION_SYSTEM_INSTRUCTION', 'JUDGMENT_RESPONSE_SCHEMA', 'JUDGMENT_SYSTEM_INSTRUCTION', 'GM_LOGIC_RESPONSE_SCHEMA', 'GM_LOGIC_SYSTEM_INSTRUCTION', 'build_narrate_prompt', 'COMPRESSION_RESPONSE_SCHEMA', 'ADAPTIVE_DENSITY_BLOCK', 'DENSE_FORM_BLOCK', 'build_compression_prompt_text', 'build_pc_appearance_prompt', 'build_npc_profile_prompt', 'DECLARATION_GATE_RULES', 'DECLARATION_GATE_SCHEMA']
