# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

Gemini API + discord.py 기반의 한국어 TRPG 보조 GM 디스코드 봇. GM이 마스터 채널에서 명령어를 입력하면 AI가 묘사를 생성하고, 비용 추적·캐시 관리·기억 압축·BGM/이미지 연출·자동 GM 진행을 자동화한다.

현재 버전: **v1.9**  
총 소스코드: ~7,400줄 (core/ 패키지 ~1,900 / auto_gm.py 1,878 / game.py 978 / character.py 936 / media.py 553 / system.py 334 / session.py ~200 / main.py 90 / prompts.py 144)

## 실행 및 환경 설정

```bash
pip install -r requirements.txt
python main.py
```

`.env` 파일이 필요하다 (`.env.example` 참고). 필수 환경 변수:
- `DISCORD_TOKEN` — 디스코드 봇 토큰
- `GEMINI_API_KEY` — Gemini API 키
- `TRPG_INTRO_TEXT` — !소개 명령어에 포함되는 공통 인트로 텍스트

> NOTE: `SYSTEM_INSTRUCTION`은 `prompts.py` 코드 영역으로 분리되었다. 변경 시 활성 세션은 `!캐시 재발급`으로 캐시를 갱신해야 반영된다.

코드 수정 후 봇을 재시작하지 않고 특정 모듈만 반영하려면 마스터 채널에서:
```
!리로드 [모듈명]   # 예: !리로드 game
```
`core/` 패키지와 `main.py`는 핫스왑 불가, `cogs/` 하위 파일만 가능.

## 파일 맵

| 파일 | 역할 |
|------|------|
| `main.py` | TRPGBot, active_sessions, setup_hook, restore_sessions_from_disk |
| `core/` | 전역 상수·모델·비용·IO·캐시·프롬프트·대화·미디어·UI·유틸 — 하위 서브모듈 참조 |
| `core_legacy.py` | 분리 이전 원본 core.py 백업 (롤백용, 운영에 사용하지 않음) |
| `prompts.py` | SYSTEM_INSTRUCTION (GM 페르소나·묘사 가이드·금지 사항) |
| `cogs/session.py` | !새세션, !시작, !소개 |
| `cogs/game.py` | !진행, !재생성, !출력물, !수정, !주사위, !기억압축, !노트, !캐시노트 |
| `cogs/character.py` | !참가, !설정, !증감(스탯/자원/상태), !외형, !프로필, !엔피씨, !능력치, !설정생성 |
| `cogs/media.py` | !이미지, !브금, !플리, !볼륨, !채팅 |
| `cogs/system.py` | !명령어, !채널정리, !세션종료, !캐시, !리로드 |
| `cogs/auto_gm.py` | !자동시작, !자동중단, !자동상태, !자동개입, !자동턴제한, !서사계획, !서사재계획 — AI 자동 GM 모드 + 서사 계획 시스템 |

### core/ 패키지 서브모듈 구조

`core.py`(단일 파일 1,896줄)를 10개 서브모듈로 분리. `core/__init__.py`가 모든 심볼을 re-export하므로 외부에서는 기존의 `import core` / `core.XYZ` 참조를 그대로 사용할 수 있다.

| 서브모듈 | 주요 내용 |
|---------|-----------|
| `core/constants.py` | `DEFAULT_MODEL`, `LOGIC_MODEL`, `IMAGE_MODEL`, `EXCHANGE_RATE`, `TRPG_SAFETY_SETTINGS`, `PRICING_1M`, `IMAGE_OUTPUT_TOKENS_BY_RES` |
| `core/models.py` | `TRPGSession` 데이터 모델 |
| `core/cost.py` | `format_cost`, `calculate_*_cost` 함수군 |
| `core/io.py` | `SCHEMA_VERSION`, `SESSION_FIELDS`, `SESSION_RESET_FIELDS`, `save_session_data`, `write_log`, `write_cost_log`, `load_scenario_from_file`, `get_available_scenarios`, `process_cache_deletion` |
| `core/cache.py` | `build_scenario_cache_text`, `update_session_cache_state`, `restore_sessions_from_disk` |
| `core/prompt.py` | `PromptBuilder`, `build_compression_prompt` |
| `core/dialogue.py` | `DIALOGUE_MARKER_PATTERN`, `parse_dialogue_paragraph`, `format_dialogue_block`, `merge_consecutive_dialogues`, `maybe_send_speaker_image`, `stream_text_to_channel` |
| `core/media.py` | `send_image_by_keyword`, `PlaylistManager` |
| `core/ui.py` | `_cleanup_session_memory`, `ChannelSelect`, `ChannelDeleteView`, `GeneralDiceView`, `DiceView` |
| `core/utils.py` | `get_uid_by_char_name`, `generate_character_details` |

의존성 방향(단방향): `constants` → `models` → `cost` → `io` → `cache`. 순환 임포트 없음.

## 주요 상수 (core/constants.py)

```python
DEFAULT_MODEL = "gemini-3-flash-preview"   # 턴 묘사, 캐시, GM-Logic, NARRATE
LOGIC_MODEL   = "gemini-3-flash-preview"   # 기억 압축, 설정생성, 서사 계획 (Pro 모델은 주석 처리됨)
IMAGE_MODEL   = "gemini-3.1-flash-image-preview"
EXCHANGE_RATE = 1500.0
```

## 아키텍처

### 상태 관리 흐름

`TRPGSession`(`core/models.py`)이 단일 세션의 모든 상태를 담는 중앙 컨테이너다. `bot.active_sessions` 딕셔너리에 **game_ch_id와 master_ch_id 양쪽 모두** 동일한 세션 객체를 키로 등록한다. 따라서 어느 채널에서든 `session = bot.active_sessions.get(ctx.channel.id)` 한 줄로 세션에 접근할 수 있다.

세션 상태는 `save_session_data(bot, session)` 호출마다 `sessions/{session_id}/data.json`에 직렬화된다. 봇 재시작 시 `restore_sessions_from_disk(bot)`이 이를 복구하며, Gemini 캐시가 만료된 경우 자동으로 재발급한다.

### 세션 직렬화 안정화 (core/io.py · core/cache.py)

직렬화 레이어에는 4가지 안정성 메커니즘이 적용되어 있다.

**① `SESSION_FIELDS` / `SESSION_RESET_FIELDS` 레지스트리**  
선택적 필드의 저장·복구 단일 진실 공급원. 새 `TRPGSession` 필드를 추가할 때 `SESSION_FIELDS`에만 등록하면 `save_session_data`와 `restore_sessions_from_disk` 양쪽에 자동 반영된다.
- `SESSION_FIELDS` — 저장·복구 대상 선택적 필드와 기본값 dict
- `SESSION_RESET_FIELDS` — 저장은 되지만 봇 재시작 시 항상 초기값으로 리셋되는 필드 (예: `auto_gm_pending_players`, `auto_gm_collected_actions`, `auto_gm_waiting_for`)
- **핵심 필드** (`session_id`, `game_ch_id`, `master_ch_id`, `prep_ch_id`, `players` 등)는 레지스트리가 아닌 직접 접근으로 저장·복구한다.

**② `SCHEMA_VERSION` 스키마 버전 관리**  
저장 JSON에 `schema_version` 정수를 기록. 복구 시 현재 버전보다 낮으면 경고를 출력한다. 현재 버전: `2`.

**③ `_MISSING` 센티널**  
`data.get(field, _MISSING)`으로 JSON에 키 자체가 없는 경우(`→ 기본값 사용`)와 키는 있지만 `null`로 저장된 경우(`→ None 유지`)를 구분한다. 복구 시 `copy.deepcopy(default)`를 사용해 세션 간 mutable 기본값 공유를 방지한다.

**④ 원자적 파일 쓰기**  
`.tmp` 임시 파일에 먼저 쓴 뒤 `os.replace(tmp, final)`로 교체. 중간 크래시 시 이전 저장 파일이 보존된다. 저장 실패는 예외를 흡수하고 경고만 출력해 게임 진행을 중단시키지 않는다.

**⑤ `_serialize_log_entry` / `_deserialize_log_entry`**  
`types.Content` ↔ `{"role", "text"}` 딕셔너리 간 안전 변환 계층. 이미지·함수 호출 등 텍스트 없는 파트는 조용히 건너뛰고, 변환 불가 엔트리는 `None`을 반환해 필터링된다.

### 채널 구성 (cogs/session.py)

`!새세션` 실행 시 카테고리 내에 **두 채널**이 생성된다:

| 채널 | 이름 형식 | 권한 | 용도 |
|------|----------|------|------|
| 게임 채널 | `game-{id}` | 봇만 전송, 플레이어 읽기 전용 | `!소개`·`!참가` 및 `!시작` 이후 AI 묘사 출력 |
| 마스터 채널 | `master-{id}` | GM·봇 전용 비공개 | GM 명령어 입력 |

**`!시작`** — 실행 시 게임 채널의 **기존 메시지를 전부 삭제**(`channel.purge`)한 뒤 시작 메시지를 스트리밍한다. `!소개`·`!참가` 등 준비 단계 내용을 정리해 실제 게임 공간을 깔끔하게 시작할 수 있다. Discord bulk-delete는 14일 이내 메시지만 지원하며, 삭제 실패 시 경고만 출력하고 게임을 계속 진행한다.

### 프롬프트 조립 순서 (PromptBuilder)

`PromptBuilder.build_prompt(session, gm_instruction)`은 아래 순서로 블록을 조립한다:
1. `compressed_memory` — **마지막 캐시 재발급 이후 새로 누적된** 압축 기억만. 캐시 재발급 시 이전 기억은 캐시 섹션 [9]으로 이동하므로 프롬프트에 중복 주입하지 않는다.
2. `session.note` (GM 하드코딩 노트, 매 턴 주입)
3. 플레이어 스탯·외형·resources·statuses
4. **NPC 델타만**: `add_npc_override_block` — 캐시 기준 데이터와 달라진 경우만 주입
5. **트리거 키워드 기억만**: `keyword_memory`의 keywords가 최근 로그 결합 문자열에 있을 때만 주입
6. `current_turn_logs` (현재 턴 행동)
7. GM 지시사항
8. 최종 룰 강제 + status_code_block 출력 지시

### NPC 주입 전략 (add_npc_override_block)

캐시 구성에 따라 세 그룹으로 분리된다:

| NPC 종류 | 캐시 섹션 | 프롬프트 주입 조건 |
|---|---|---|
| `default_npcs` (시나리오 정의) | `[3. NPC 사전]` — 항상 | 설정 필드 변경 또는 런타임 resources/statuses 변동 시에만 **변경된 필드(delta)만** |
| 세션 생성 NPC + 캐시된 상태 (`cached_session_npcs`에 있음) | `[8. 세션 진행 중 추가된 NPC]` — 재발급 시 | 마지막 캐시 스냅샷과 달라진 필드(delta)만 |
| 세션 생성 NPC + 미캐시 상태 (`cached_session_npcs`에 없음) | 없음 | 전체 프로파일 (`[전체 프로파일]` 레이블) |

**디폴트 NPC 수정**: `changed_info_fields`(달라진 필드만)를 추출해 `[필드 수정 — 이하 항목만 캐시 내용 대신 적용]` 레이블로 주입.  
**스탯 delta**: `ability_stats` 순서 보장. 캐시 기준값과 동일하면 주입 안 함.  
**런타임 base 비교**: 세션 NPC는 `cached_session_npcs[name].resources/statuses`를 기준으로 비교 (캐시 이후 변화만 delta로 주입).

### Gemini Context Caching

시나리오 룰북을 조립해 Gemini 서버에 캐싱한다. **캐시 재발급 시** 세션 진행 중 추가된 데이터도 포함된다:

**캐시 섹션 순서**:
1. `[1. 세계관 정보]`
2. `[2. 스토리 진행 가이드]`
3. `[3. NPC 사전 — 전체 등장인물 설정]` (default_npcs 전체)
4. `[4. 게임 스탯 및 판정 시스템]`
5. `[5. 시나리오 고유 묘사 가이드라인]`
6. `[6. GM 절대 금지 사항]` (prohibitions 정의 시에만)
7. `[7. 필수 출력: 상태창 코드블럭 양식]`
8. `[8. 세션 진행 중 추가된 NPC]` — `session` 인자가 있을 때, default_npcs에 없는 세션 생성 NPC 전체 (런타임 resources/statuses 포함). **캐시 재발급 시에만 갱신.**
9. `[9. 세션 진행 기억 — 과거 턴 압축 요약]` — `session` 인자가 있을 때, `cached_compressed_memory + compressed_memory` 합산. **캐시 재발급 시에만 갱신.**
10. `[추가 세계관 및 상태 (캐시 노트)]` — `cache_note`가 있을 때

**최소 32,768 토큰** 미만이면 `"."` 문자 패딩을 `[System Data Padding Area - DO NOT READ]` 헤더와 함께 추가해 요건을 충족한다(의도된 핵).

`build_scenario_cache_text(bot, model_id, scenario_data, cache_note="", session_id=None, session=None)` — 3-튜플 `(padded_text, total_tokens, base_rulebook_text)` 반환. `session`이 `None`이면 [8], [9] 섹션 생략 (하위 호환).

**`update_session_cache_state(session)`** — 캐시 생성 완료 직후 반드시 호출해야 한다:
- `cached_session_npcs` 스냅샷 갱신 (resources/statuses 포함)
- `cached_compressed_memory ← old + new`; `compressed_memory ← ""`
- **이 함수를 호출하지 않으면** 세션 NPC와 기억이 프롬프트에 계속 중복 주입된다.

**캐시 생성 3개 호출부**: `cogs/session.py` (!새세션), `cogs/system.py` (!캐시 재발급), `cogs/game.py` (generate_with_retry 자동 재발급). 모두 `session=session` 전달 + `update_session_cache_state(session)` 호출.

턴 진행(`!진행`) 중 캐시 만료 에러(400/404)가 발생하면 `generate_with_retry()`가 자동으로 캐시를 재발급하고 묘사를 이어서 출력한다.

### 기억 압축 시스템

턴이 완료될 때마다 `uncompressed_logs`에 해당 턴의 원본 로그를 누적한다. `turn_count % 5 == 0`이 되면 백그라운드에서 `LOGIC_MODEL`로 압축 요청을 보내고, 결과를 `compressed_memory`에 append한 뒤 `uncompressed_logs`에서 삭제한다. `raw_logs`는 최근 20개만 유지한다.

**캐시 재발급 시**: `compressed_memory`는 `cached_compressed_memory`로 이동되어 캐시 섹션 [9]에 수록된다. 이후 `compressed_memory`는 `""` 초기화되며, 프롬프트 `add_memory_block`은 재발급 이후 새로 누적된 기억만 주입한다.

### 인물 대사 자동 포매팅

`PromptBuilder.add_rule_enforcement_block`은 매 턴 AI에게 인물 대사를 `@대사:이름|본문` 단일 라인 마커로 출력하도록 지시한다. `_execute_proceed`는 문단별로 `core.parse_dialogue_paragraph`로 마커를 감지하고:

1. 시나리오 `media_keywords`에 `이름`이 등록되어 있거나 `media/{scenario_id}/{이름}.png`가 존재하면 인물 이미지를 대사 문단 바로 위에 자동 송출 (`core.maybe_send_speaker_image`)
2. 본문을 `## ▍이름\n## 「 본문 」` 형식으로 변환
3. `stream_text_to_channel`에 `quote_prefix=False`를 넘겨 `> ` 인용 접두를 생략

일반 묘사 문단은 기존과 동일하게 `> ` 접두로 스트리밍된다. `상/중/하:키워드` 이미지 태그는 인물 대사 자동 이미지와 독립적으로 작동.

### !진행 태그 시스템

GM의 instruction에서 정규식으로 태그를 추출한 뒤 AI에게 전달하는 clean_instruction에서는 제거한다:

| 태그 | 동작 |
|------|------|
| `상:키워드` `중:키워드` `하:키워드` | 첫 문단 후 / 텍스트 내 키워드 등장 시 / 묘사 끝 후 이미지 전송 |
| `자:이름;아이템;수치` | `session.resources[이름][아이템] += 수치` |
| `태:이름;상태` | `session.statuses[이름]`에 상태 추가 |
| `태:이름;-상태` | `session.statuses[이름]`에서 상태 제거 |

### !출력물 / !수정 시스템

직전 턴 AI 출력물을 GM이 편집할 수 있는 두 단계 워크플로:
1. `!출력물` → `session.raw_logs`에서 최근 `role="model"` 텍스트를 1950자 청크로 마스터 채널에 전송
2. `!수정 [텍스트]` → 게임 채널의 봇 텍스트 메시지를 Discord `edit()` API로 덮어쓰고, `raw_logs`, `uncompressed_logs`, `game_chat_log.txt` 동기화

`!수정`은 앵커(`last_turn_anchor_id`) 이후 봇 텍스트 메시지(첨부파일 없는 것)를 대상으로 하며 메시지 수 불일치 시 자동 추가/삭제한다.

### 설정생성 시스템

`!설정생성 [pc/npc] [이름] [지시사항]`으로 AI가 캐릭터 설정 초안을 생성한다:
- **PC**: 외모 전용 5개 고정 필드 (나이/성별/체형/얼굴/피부·헤어/복장/첫인상), 결과를 `!외형`으로 적용
- **NPC**: `npc_template.info_fields`가 시나리오에 정의된 경우 그 필드 목록을 그대로 출력 양식으로 사용. 미정의 시 기본 12항목 사용. 출력 포맷은 `**필드명**: 값` 형식이며, `!엔피씨 설정 [이름] [출력물 전체]`에 붙여넣으면 자동으로 구조화 파싱된다. `has_stats`/`has_resources`/`has_statuses` 플래그에 따라 스탯·자원·상태 필드와 `stat_system`도 프롬프트에 주입된다.
- 특수 태그 `엔:이름[,이름]`으로 참조 NPC 설정 주입 가능

### 자동 GM 모드 (Auto-GM)

`!자동시작`이 호출된 세션에서만 활성화되는 옵트인 모드.

**아키텍처: 2-티어 AI 루프**
- **Tier 1 (GM-Logic)**: `cogs/auto_gm.py`에서 `DEFAULT_MODEL`로 호출. `response_mime_type="application/json"` + `GM_LOGIC_RESPONSE_SCHEMA`로 강제된 결정 JSON 출력 (`action`: `ASK`/`NARRATE`/`ROLL`/`PROCEED` + `event_assessment`).
- **Tier 2 (묘사 생성)**: `GameCog._execute_proceed()` 헬퍼 직접 호출 (캐시 적중 그대로 활용).

**메시지 라우팅**: `on_message`가 게임 채널 발언만 큐잉. 봇 메시지·`!`로 시작하는 메시지 무시.

**처리 루프**: 한 플레이어 발언당 최대 5회 반복(`MAX_ITERATIONS_PER_MESSAGE`). `ASK`는 짧은 안내만 게임 채널에 송출 후 다음 발언 대기. `NARRATE`는 캐시 기반 경량 LLM 호출로 즉답 생성 후 대기. `ROLL`은 `random.randint`로 즉시 굴리고 결과를 컨텍스트에 주입한 채 재호출. `PROCEED`는 `_execute_proceed`를 호출하고 루프 종료.

**입력 중 표시**: `_call_gm_logic` 호출 및 NARRATE의 `_dispatch_narrate` 호출을 `async with game_ch.typing():` 으로 감싼다.

**안전장치**:
- `auto_gm_turn_cap` (기본 10) — 누적 자동 턴 도달 시 자동 정지
- `MAX_CLARIFY_PER_MESSAGE = 2` — 같은 발언에 ASK 2회 초과 시 강제 PROCEED
- `MAX_NARRATE_PER_MESSAGE = 7` — 같은 발언에 NARRATE 7회 초과 시 강제 PROCEED
- `auto_gm_cost_cap_krw` (기본 500) — 자동 모드 누적 비용 도달 시 정지
- 세션별 `asyncio.Lock`으로 동시 처리 방지

**비용 로그 분리**: 자동 모드 호출은 `cost_log.txt`에 `[AUTO]` 접두사로 기록.

**`_execute_proceed` 헬퍼**: `ctx`에 의존하지 않고 `(session, instruction, master_guild, cost_log_prefix)` 인자만 받는다. 자동 GM은 `cost_log_prefix="[AUTO] "` 인자로 호출.

**멀티플레이어 라운드 수집**: PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 순서대로 질문 (`_start_round` → `_ask_next_player`). `auto_gm_pending_players` 큐 기반 순차 수집, 전체 완료 시 GM-Logic 호출.

**스탯 적용 분야 주입**: `_build_logic_user_prompt`가 시나리오의 `stat_descriptions` 딕셔너리를 읽어 `[PC 프로필]` 줄 끝에 인라인으로 추가한다. GM-Logic이 `ROLL` 결정 시 어떤 스탯을 써야 할지 즉시 판단할 수 있다. `stat_descriptions`가 없는 시나리오에서는 기존과 동일하게 동작한다.

### Auto-GM PROCEED 이력 (반복 방지)

`session.auto_gm_proceed_history` — 최근 3회 PROCEED 이력 목록. 각 항목:
```python
{
    "turn_num": int,          # 해당 턴 번호
    "instruction": str,       # PROCEED에 사용된 지시사항
    "context": list[str],     # PROCEED 직전의 current_turn_logs 스냅샷 (NARRATE/ASK/ROLL 포함)
    "ai_summary": str,        # AI 출력 앞 500자 요약
}
```
`_dispatch_proceed` 완료 후 append, 3개 초과 시 가장 오래된 항목 삭제. GM-Logic 프롬프트에 `[최근 PROCEED 이력]` 블록으로 주입되어 동일 상황 반복·정체를 방지한다.

### Auto-GM 서사 계획 시스템 (Narrative Plan)

`session.narrative_plan` — 사건(event) 단위 서사 계획 딕셔너리:
```python
{
    "current_event": {
        "title": str,                  # 현재 사건 제목
        "summary": str,                # 개요
        "goal": str,                   # 서사 목표
        "resolution_direction": str,   # 마무리 방향성
        "progress": str,               # 현재 진행 상황 (PROCEED마다 AI 요약으로 자동 갱신)
    },
    "next_event": {
        "title": str,                  # 다음 사건 제목
        "summary": str,                # 개요
        "trigger": str,                # 시작 조건
    },
    "planner_notes": str,              # 설계 메모 (선택)
    "plan_version": int,               # 수립 횟수
    "last_planned_turn": int,          # 수립 시점 턴
}
```

**수립 시점**:
- `!자동시작` → 백그라운드 `asyncio.create_task(_init_narrative_and_start)` — 계획이 없으면 `_plan_narrative(session, "init")` 후 첫 라운드 시작
- PROCEED 완료 후 `_update_narrative_progress` 호출 → `event_assessment`가 `"completed"` 또는 `"deviated"`이면 `asyncio.create_task(_plan_narrative(...))` 트리거
- `!서사재계획` — 수동 강제 재수립

**`event_assessment`** — GM-Logic JSON 응답의 추가 필드:
- `"ongoing"` : 계획대로 진행 중 (기본값)
- `"resolving"` : 마무리 단계 진입
- `"completed"` : 서사 목표 달성·사건 종결 → 재계획 트리거
- `"deviated"` : 플레이어 선택으로 예상 범위 이탈 → 재계획 트리거
- PROCEED가 아닌 action(ASK/NARRATE/ROLL)에서는 항상 `"ongoing"`으로 고정

**GM-Logic 프롬프트 주입**: `[현재 서사 계획]` 블록 — 현재 사건 제목·개요·목표·마무리 방향·진행 상황 + 다음 사건 참고용.

**`_plan_narrative(session, trigger_reason, context_note="")`** — `LOGIC_MODEL` 호출, `NARRATIVE_PLAN_SCHEMA` + `NARRATIVE_PLANNER_SYSTEM_INSTRUCTION` 사용. 결과를 `session.narrative_plan`에 저장하고 마스터 채널에 보고.

**명령어**: `!서사계획` (임베드 출력) / `!서사재계획 [메모]` (강제 재수립)

### 비용 추적

`PRICING_1M` 딕셔너리로 모델별 INPUT/OUTPUT/CACHE_READ/CACHE_STORAGE_PER_HOUR 단가를 관리한다. 모든 API 호출 후 `calculate_upload_cost()`로 KRW 비용을 계산해 `session.total_cost`에 누적하고 `write_cost_log()`로 `sessions/{id}/cost_log.txt`에 기록한다. 캐시 보관 비용은 초를 분 단위로 반올림하며 최대 21,600초(6시간) 상한을 적용한다. 환율은 1500 KRW/USD 고정.

## 시나리오 JSON 작성 시 주의사항

- `default_npcs`에 정의된 NPC는 캐시 [3]에 구워진다. 게임 중 변경은 `session.npcs`(`!엔피씨 설정`)로 오버라이드해야 한다.
- `npc_template`를 정의하면 NPC 항목을 구조화 필드로 관리할 수 있다. `info_fields` 리스트에 필드명을 순서대로 지정하며, `has_resources`/`has_statuses`/`has_stats` 플래그로 런타임 상태 포함 여부를 선언한다. 미정의 시 레거시 `details` 문자열 방식으로 동작한다.
  - NPC 항목에 `"resources": {"아이템": 수량}` 또는 `"statuses": ["상태명"]`이 있으면 세션 초기화 시 `session.resources`/`session.statuses`에 자동 사전 적용된다.
  - `!엔피씨 설정 [이름] [필드명] [내용]`으로 단일 필드를 수정하거나, `!설정생성 npc` 출력물(`**필드명**: 값` 형식)을 그대로 붙여넣으면 자동 파싱 적용된다.
- `prohibitions` 항목(리스트 또는 문자열)을 정의하면 캐시 룰북에 `[6. GM 절대 금지 사항]` 섹션으로 삽입된다. 없으면 섹션 자체가 생략된다.
- `ability_stat_max` 항목(int 또는 `{"스탯명": 상한값}` dict)을 정의하면 `!능력치` 굴림 결과가 개별 상한을 초과하지 않도록 초과분을 나머지 스탯에 비율 재배분한다.
- `status_code_block`을 정의하면 매 턴 AI 응답의 마지막에 코드블럭 출력이 강제된다. 없으면 생략된다.
- `!이미지 생성` 명령어는 `scenarios/{시나리오명}.json`을 직접 덮어쓴다 (media_keywords 영구 추가).
- `keyword_memory`의 키워드는 최근 로그 전체를 단순 문자열로 `in` 검사하므로 짧고 구체적인 고유명사로 작성할 것.
- `image_prompts`에 형식키별 `prompt`와 `aspect_ratio`를 정의해야 `!이미지 생성`이 동작한다.
- `profile_secondary_stats`에 `pc_template` 항목명을 리스트로 지정하면 `!프로필` 임베드에서 구분선 아래 전체 폭 필드로 표시된다.
- `ability_stats`에 `pc_template` 항목명을 리스트로 지정하면 `!능력치` 명령어에서 주사위 굴림 대상이 된다. 순서대로 굴림이 진행되고 Hamilton 방식으로 target_total에 비례 배분된다.
- `stat_descriptions` 항목(`{"스탯명": "설명"}` dict)을 정의하면 자동 GM의 GM-Logic 프롬프트 `[PC 프로필]` 줄에 인라인으로 추가된다. ROLL 결정 시 스탯 용도를 AI가 즉시 파악할 수 있어 판정 스탯 선택 정확도가 높아진다. 미정의 시 생략된다.
  - `stat_system` 내에 스탯별 적용 분야 섹션도 함께 작성해 메인 AI(캐시)에도 반영할 것.

## 개발 주의사항

- `bot.active_sessions`에는 game_ch_id와 master_ch_id 양쪽이 등록된다. 채널 삭제 시 `_cleanup_session_memory()`가 두 키를 모두 pop해야 메모리 누수가 없다.
- `SESSION_FIELDS`에 새 `TRPGSession` 필드를 등록하면 저장·복구가 자동으로 처리된다. 핵심 필드(session_id, players 등)와 런타임 전용 필드(is_processing, auto_gm_lock 등)는 레지스트리에 넣지 않는다.
- `SCHEMA_VERSION`은 저장 JSON 구조가 변경될 때 증가시킨다. 현재 `2`.
- `!재생성`은 `turn_count % 5 == 0`(압축 직후)일 때 차단된다. 이 경우 롤백 대신 다음 턴으로 교정해야 한다.
- `!수정`은 `last_turn_anchor_id`가 없으면 동작하지 않는다. 세션 복구 직후 `!진행` 전에 사용 불가.
- `!시작`은 실행 전 게임 채널의 모든 메시지를 `channel.purge`로 삭제한 뒤 `start_message`를 스트리밍하고 `role="model"`로 `raw_logs`에 삽입한다. 중복 실행 시 AI 컨텍스트가 오염되므로 `is_started` 플래그로 차단된다.
- `build_scenario_cache_text()`는 3-튜플 `(padded_text, tokens, base_text)`를 반환한다. 모든 호출부에서 3개를 언팩해야 한다. 캐시 생성 후 반드시 `update_session_cache_state(session)` 호출 필요 (session.py / system.py / game.py / core/cache.py 복구 경로 모두).
- `!증감`은 `key` 인자에 따라 3가지 모드로 분기한다: 스탯 수치 증감(기본), `자원` 키워드(resources 딕셔너리), `상태` 키워드(statuses 리스트). NPC 이름도 char_name으로 사용 가능하며 PC 탐색 없이 resources/statuses에 직접 접근한다.
- `!프로필`은 기본적으로 마스터 채널에 출력하고, `게임` 인자를 붙이면 게임 채널에 출력한다.
- `!능력치`는 마스터 채널에서만 실행 가능. `ability_stats`가 시나리오 JSON에 없으면 명령어 실행이 차단된다.
- `!엔피씨 설정`은 3-모드로 분기한다: ① 첫 단어가 `npc_template.info_fields`의 항목명이면 단일 필드 수정, ② `**필드명**: 값` 형식이 감지되면 구조화 자동 파싱, ③ 그 외는 레거시 `details` 전체 덮어쓰기. 모드 ①·②는 `details` 필드를 제거해 구/신 혼재를 방지한다.
- `add_npc_override_block`은 `cached_session_npcs`를 세션 NPC의 delta 비교 기준으로 사용한다. 캐시 재발급 전에 만들어진 세션 NPC는 `cached_session_npcs`에 없으므로 전체 프로파일이 매 턴 주입된다. 재발급 후에는 변경된 필드만 delta로 주입된다.
- `_plan_narrative` 내에서 직접 마스터 채널에 계획 결과를 보고한다. `!서사재계획` 실행 시에도 동일.
- `paused_session/` 폴더는 자동 복구 대상이 아니다. 복구하려면 `sessions/`로 이동해야 한다.
- `sessions/` 폴더는 `.gitignore`에 포함되어 있다.
- NARRATE에서 `max_output_tokens`를 설정하지 않는다. `gemini-3-flash-preview`는 thinking 모델로, `max_output_tokens`를 지정하면 thinking 토큰이 한도를 소진하여 실제 텍스트 출력이 거의 없는 조기 종료가 발생한다. 출력 길이는 프롬프트 지시로 제어한다.
