# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

Gemini API + discord.py 기반의 한국어 TRPG 보조 GM 디스코드 봇. GM이 마스터 채널에서 명령어를 입력하면 AI가 묘사를 생성하고, 비용 추적·캐시 관리·기억 압축·BGM/이미지 연출·GM 진행을 자동화한다.

현재 버전: **v5.15.0**  
총 소스코드: ~10,020줄 (core/ 패키지 ~3,080 [audio_mixer·tts 포함] / auto_gm.py 2,305 / game.py 1,310 / character.py 1,031 / prompts.py 1,019 / media.py 593 / system.py 342 / session.py 251 / main.py 95)

> NOTE: `prompts.py`는 SYSTEM_INSTRUCTION 외에도 GM용 시스템 지시문·응답 스키마(지시층위, 서사 계획, 서사 방향성 시뮬레이터, 세계 타임라인 추출기)를 모두 보관하므로 ~1,019줄로 커졌다.

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
| `prompts.py` | SYSTEM_INSTRUCTION (GM 페르소나·묘사 가이드·금지 사항) + GM 시스템 지시문·응답 스키마 모음 |
| `cogs/session.py` | !새세션, !시작, !소개 |
| `cogs/game.py` | !진행, !재생성, !출력물, !수정, !주사위, !기억압축, !노트, !캐시노트 |
| `cogs/character.py` | !참가, !설정, !증감(스탯/자원/상태), !외형, !프로필, !엔피씨, !능력치, !설정생성 |
| `cogs/media.py` | !이미지, !브금, !플리, !볼륨, !채팅, !더빙(TTS 토글) |
| `cogs/system.py` | !명령어, !채널정리, !세션종료, !캐시, !리로드 |
| `cogs/gm.py` | `!자동` 명령어 그룹 (시작/중단/상태/개입/턴제한/비용제한/서사/재계획) — AI GM + 서사 계획 시스템 |

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
| `core/media.py` | `send_image_by_keyword`, `PlaylistManager` (믹서 base로 트랙 공급) |
| `core/audio_mixer.py` | `MixerAudioSource`(BGM/플리 base + 효과음 effects + 음성 voice 큐 합산), `PCMBytesAudioSource`, `ensure_mixer`/`get_mixer`/`active_volume_source`, `preload_sfx`/`play_dice_sfx` |
| `core/tts.py` | `synthesize_tts_pcm`(Gemini native TTS → 48kHz stereo PCM), `clean_text_for_tts` |
| `core/ui.py` | `_cleanup_session_memory`, `ChannelSelect`, `ChannelDeleteView`, `GeneralDiceView`, `DiceView` |
| `core/utils.py` | `get_uid_by_char_name`, `generate_character_details`, `resolve_pc`, `resolve_char_name` |

의존성 방향(단방향): `constants` → `models` → `cost` → `io` → `cache`. 순환 임포트 없음.

## 주요 상수 (core/constants.py)

```python
DEFAULT_MODEL = "gemini-3-flash-preview"   # 턴 묘사, 캐시, 지시층위, NARRATE
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
- **핵심 필드** (`session_id`, `game_ch_id`, `master_ch_id`, `players` 등)는 레지스트리가 아닌 직접 접근으로 저장·복구한다.

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

턴이 완료될 때마다 `uncompressed_logs`에 해당 턴의 원본 로그를 누적한다. 압축 개시 시점은 **5N 턴 종료 직후가 아니라 다음 5N+1 프로씨드의 시작 시점**이다: `_execute_proceed` 도입부에서 `turn_count % 5 == 0`(=직전 완료 턴이 5의 배수)이고 `uncompressed_logs`가 있으면, 그 로그를 스냅샷해 `_run_auto_compression`을 **백그라운드 태스크(`asyncio.create_task`)로 개시**하고 프로씨드는 대기 없이 진행한다. 압축은 `LOGIC_MODEL`로 요청→`compressed_memory`에 append→`uncompressed_logs` **앞에서 `len(스냅샷)`개 삭제**한다(이번 턴 로그는 뒤로 append되어 경합 없음). `raw_logs`는 최근 20개만 유지한다.

> **이 타이밍 이동의 목적**: 5의 배수 턴에서 압축이 즉시 걸려 `!재생성`이 막히던 문제를 해소한다. 이제 5N 턴에는 압축이 없어 롤백이 가능하고, 압축은 5N+1로 진행을 선택한 순간에야 개시된다. 압축 실행 중(`session.is_compressing=True`)에는 롤백 대상 로그와 경합할 수 있어 `!재생성`이 잠시 차단된다(런타임 전용 플래그, `is_processing`과 동일 계열).
>
> 동시 저장 안전장치: 백그라운드 압축과 프로씨드가 각각 `save_session_data`를 호출할 수 있으므로, tmp 파일명을 호출별 고유(`data.json.{pid}.{ns}.tmp`)로 만들어 data.json 손상을 방지한다(`core/io.py`).

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

**띄어쓰기 = 언더바(_) 규약**: 태그 종결자가 공백이라 다단어 이름·항목·상태는 잘려 무시되던 문제를 해소하기 위해, 태그 값에 띄어쓰기가 필요하면 **언더바(_)로 표기**한다(예: `태:수적_세작;내상(중상)`, `태:유이설;내력_고갈`). `_execute_proceed`가 태그 추출 직후 `_`→공백으로 복원한다. 지시층위·SYSTEM_INSTRUCTION 프롬프트가 이 규약을 지시하며, `!증감`도 입력의 `_`를 공백으로 정규화한다(`char_name`·`args` 일괄 변환). 또한 AI 묘사 응답에 남은 태그(에코 등)는 **코드블럭 인식 전에 방어적으로 스트립**한다(태그가 코드블럭 뒤에 붙어 상태창 코드블럭 인식이 깨지던 문제 해소).

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

### GM (Auto-GM)

`!자동 시작`이 호출된 세션에서만 활성화되는 옵트인 모드.

**명령어 그룹**: 모든 GM 명령어는 `commands.group(name="자동", invoke_without_command=True)` 하위로 통합되어 있다. 하위명령: `!자동 시작 (대상PC)` / `!자동 중단` / `!자동 상태` / `!자동 개입 [텍스트]` / `!자동 턴제한 [N|해제]` / `!자동 비용제한 [원|해제]` / `!자동 서사` / `!자동 재계획 [메모]`. 인자 없거나 미상 하위명령이면 그룹 콜백이 사용법을 출력한다. (서사 관련 명령어도 이 그룹에 포함됨.) 하위명령 메서드는 클래스 본문 뒤쪽에 정의돼도 `@auto.command`로 묶이며, 그룹 전환 후 첫 적용은 핫스왑보다 재시작 검증을 권장한다.

**아키텍처: 2-티어 AI 루프**
- **Tier 1 (지시층위)**: `cogs/gm.py`에서 `DEFAULT_MODEL`로 호출. `response_mime_type="application/json"` + `GM_LOGIC_RESPONSE_SCHEMA`로 강제된 결정 JSON 출력 (`action`: `ASK`/`NARRATE`/`ROLL`/`PROCEED` + `event_assessment`).
- **Tier 2 (묘사 생성)**: `GameCog._execute_proceed()` 헬퍼 직접 호출 (캐시 적중 그대로 활용).

**메시지 라우팅**: `on_message`가 게임 채널 발언만 큐잉. 봇 메시지·`!`로 시작하는 메시지 무시.

**처리 루프**: `_run_gm_logic_loop`. `ASK`는 짧은 안내만 게임 채널에 송출 후 다음 발언 대기. `NARRATE`는 캐시 기반 경량 LLM 호출로 즉답 생성 후 대기. `ROLL`은 버튼 UI로 굴림을 받고 콜백(`_continue_with_roll_results`)에서 이어 처리. `PROCEED`는 `_dispatch_proceed`를 호출하고 종료.
- `for iteration in range(MAX_ITERATIONS_PER_MESSAGE)` 구조가 남아 있으나, 모든 action 분기가 iteration 0에서 `break`/`return` 하므로 **실질적으로 단일 패스**다. `iteration>=1` 재시도 분기와 `for...else` 강제 PROCEED는 현재 도달하지 않는 안전 스캐폴딩이다.

**사전 서사 설계 (방안 6)**: 지시층위 첫 결정 직전, 세션 캐시가 유효하면(`cache_model == DEFAULT_MODEL`) `_simulate_narrative_directions`로 세계관 세력·지역 규칙에 근거한 서사 방향성 2~3개를 먼저 산출하고, 그 `sim_result`를 `_call_gm_logic(..., sim_result=...)`에 **순차 주입**한다. (과거에는 `asyncio.gather`로 병렬 실행했으나 첫 결정이 sim을 보지 못해 비용만 낭비되어, 순차 주입으로 교정됨.) 캐시 미스 시 시뮬레이션을 건너뛴다.

**세계 물리 타임라인 (방안 B)**: PROCEED 완료 후 `_update_world_timeline`이 백그라운드 태스크로 AI 묘사를 분석해 `session.world_timeline`(위치·시간대·세력 배치·위협 등)을 갱신한다. 시뮬레이터 프롬프트에 주입되어 고차원 개연성 판단의 기준이 된다. 실패해도 게임 진행에 영향 없음.

**강제/정상 PROCEED 후처리 단일화**: `_finish_proceed_and_continue(session, instruction, master_ch, *, event_assessment=None)` — 턴 한도 체크 → `_dispatch_proceed` → (event_assessment 있으면) 서사 진행도 갱신 → 카운터·사이드노트 초기화 + `turns_done++` → 저장 → 다음 라운드 시작을 한 곳에 모은 헬퍼. `_run_gm_logic_loop`의 5개 강제 PROCEED 사이트 + `_continue_with_roll_results`가 모두 이 헬퍼를 호출한다. (이전엔 동일 블록이 6곳에 복붙되어 있었음.)

**입력 중 표시**: 시뮬레이션·`_call_gm_logic` 호출 및 NARRATE의 `_dispatch_narrate` 호출을 `async with game_ch.typing():` 으로 감싼다.

**안전장치**:
- `auto_gm_turn_cap` (기본 `None` = 무제한) — 값이 있을 때만 누적 자동 턴 도달 시 자동 정지. `!자동 턴제한 [N]`으로 설정, `해제`/`0`으로 무제한 복귀.
- `MAX_CLARIFY_PER_MESSAGE = 2` — 같은 발언에 ASK 2회 초과 시 강제 PROCEED
- `MAX_NARRATE_PER_MESSAGE = 7` — 같은 발언에 NARRATE 7회 초과 시 강제 PROCEED
- `auto_gm_cost_cap_krw` (기본 `None` = 무제한) — 값이 있을 때만 자동 모드 누적 비용 도달 시 정지. `!자동 비용제한 [원]`으로 설정, `해제`/`0`으로 무제한 복귀.
- **None 가드 필수**: 두 캡은 `None`일 수 있으므로 모든 검사 지점이 `cap is not None and used >= cap` 형태다(`_handle_player_message`·`_process_actions`·`_finish_proceed_and_continue`·`_continue_with_roll_results`). 새 검사 추가 시 None 가드를 빠뜨리지 말 것.
- 세션별 `asyncio.Lock`으로 동시 처리 방지

**대기 중 안내 (게임 채널)**: 수동 `_execute_proceed`는 묘사 생성 동안 게임 채널에 "🎬 GM이 다음 장면을 구성하는 중…", 자동 `_run_gm_logic_loop`는 시뮬레이션·판단 동안 "🤔 GM이 상황을 판단하는 중…"을 띄우고 출력 시작 직전 `core.clear_status_message`로 삭제한다(`core.send_status_message`/`clear_status_message`, game_chat 로그 미기록).

**비용 로그 분리**: 자동 모드 호출은 `cost_log.txt`에 `[AUTO]` 접두사로 기록.

**`_execute_proceed` 헬퍼**: `ctx`에 의존하지 않고 `(session, instruction, master_guild, cost_log_prefix)` 인자만 받는다. GM은 `cost_log_prefix="[AUTO] "` 인자로 호출.

**멀티플레이어 라운드 수집**: PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 순서대로 질문 (`_start_round` → `_ask_next_player`). `auto_gm_pending_players` 큐 기반 순차 수집, 전체 완료 시 지시층위 호출.

**스탯 적용 분야 주입**: `_build_logic_user_prompt`가 시나리오의 `stat_descriptions` 딕셔너리를 읽어 `[PC 프로필]` 줄 끝에 인라인으로 추가한다. 지시층위가 `ROLL` 결정 시 어떤 스탯을 써야 할지 즉시 판단할 수 있다. `stat_descriptions`가 없는 시나리오에서는 기존과 동일하게 동작한다.

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
`_dispatch_proceed` 완료 후 append, 3개 초과 시 가장 오래된 항목 삭제. 지시층위 프롬프트에 `[최근 PROCEED 이력]` 블록으로 주입되어 동일 상황 반복·정체를 방지한다.

### Auto-GM 정보 인지 원장 (Info Ledger — 정보·시간 개연성 구조화)

NPC·적이 물리적으로 알 수 없는 정보로 매복·간파·행동하는 개연성 붕괴를 **구조적으로** 막는 지속형 상태. 산문 규칙([최우선 절대 원칙]의 정보·시간 개연성)의 **외재화·명문화** 버전이다.

**상태**: `session.info_ledger` — 비공개·플롯 관련 정보별 인지 주체 기록 (`SESSION_FIELDS` 등록, 지속). 각 항목:
```python
{
    "info": str,              # 비공개 정보 내용
    "known_by": list[str],    # 확실히 아는 인물·세력 (확지)
    "suspected_by": list[str],# 막연히 추정·의심만 가능한 자 (구체적 앎 아님)
    "origin": str,            # 발생·출처 맥락 (근거)
    "leaks": list[str],       # 유출 이력 "턴N: 대상 — 근거(how)"
    "turn_added": int,
}
```

**메커니즘 (지시층위 인라인 — 추가 API 호출 없음)**:
- **스키마 필드 `info_access`** (`GM_LOGIC_RESPONSE_SCHEMA`): `constraint_check`와 `action` **사이**에 선언 → 선언 순서 CoT로 "누가 아는가"를 결정 전에 확정, action·proceed_instruction을 제약. 하위 필드: `assessment`(핵심 비밀·인지 주체 점검) / `new_secrets`(이번 턴 신규 비밀 델타) / `new_leaks`(기존 비밀의 신규 유출 델타, 근거 `how` 필수).
- **주입**: `_build_logic_user_prompt`가 `session.info_ledger`를 `[정보 인지 원장]` 블록으로 렌더(최근 8건, world_timeline 옆). "확지로 적히지 않은 자가 아는 것처럼 행동하게 하지 말 것" 지시 동반.
- **갱신**: `_call_gm_logic`이 응답 파싱 직후 `_update_info_ledger(session, decision)` 호출 — `info_access` 델타를 원장에 **append-only 병합**. 신규 비밀은 중복(정규화 매칭) 스킵, 유출은 `known_by` 확장+`suspected_by`에서 이동+근거 기록, 근거 없는 미등록 유출은 무시(날조 방어), 최대 12건 스코핑. 실패해도 진행 무영향(예외 흡수).

**설계 원칙 반영**: 지속화(드리프트 방지)·타이트 스코핑(비공개 정보만·12건 상한)·등급화(확지/추정)·근거 강제(origin·how)·Tier2 전파(proceed_instruction 제약)·플레이어 반영(PC 발설→new_leaks). 조회: `!자동 원장`.

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
- `!자동 시작` → 백그라운드 `asyncio.create_task(_init_narrative_and_start)` — 계획이 없으면 `_plan_narrative(session, "init")` 후 첫 라운드 시작
- PROCEED 완료 후 `_update_narrative_progress` 호출 → `event_assessment`가 `"completed"` 또는 `"deviated"`이면 `asyncio.create_task(_plan_narrative(...))` 트리거
- `!자동 재계획` — 수동 강제 재수립

**`event_assessment`** — 지시층위 JSON 응답의 추가 필드:
- `"ongoing"` : 계획대로 진행 중 (기본값)
- `"resolving"` : 마무리 단계 진입
- `"completed"` : 서사 목표 달성·사건 종결 → 재계획 트리거
- `"deviated"` : 플레이어 선택으로 예상 범위 이탈 → 재계획 트리거
- PROCEED가 아닌 action(ASK/NARRATE/ROLL)에서는 항상 `"ongoing"`으로 고정

**지시층위 프롬프트 주입**: `[현재 서사 계획]` 블록 — 현재 사건 제목·개요·목표·마무리 방향·진행 상황 + 다음 사건 참고용.

**`_plan_narrative(session, trigger_reason, context_note="")`** — `LOGIC_MODEL` 호출, `NARRATIVE_PLAN_SCHEMA` + `NARRATIVE_PLANNER_SYSTEM_INSTRUCTION` 사용. 결과를 `session.narrative_plan`에 저장하고 마스터 채널에 보고.

**명령어**: `!자동 서사` (임베드 출력) / `!자동 재계획 [메모]` (강제 재수립)

### 오디오 믹싱 / 효과음 (core/audio_mixer.py)

discord.py의 `VoiceClient`는 **동시에 한 AudioSource만** 재생하므로, BGM/플리 위에 효과음을 겹치려면 봇이 PCM을 직접 합산해야 한다. `MixerAudioSource`가 그 단일 소스 역할을 한다.

- **구조**: `VoiceClient.play(MixerAudioSource)` 단 1회. `base`(현재 BGM 또는 플리 트랙) + `effects`(효과음들)를 매 20ms 프레임(3840B)마다 `audioop.add`로 합산. 효과음 재생 중에는 base를 `duck_factor`(0.55)로 감쇠해 클리핑 방지.
- **영속성**: base·effects가 모두 없으면 무음 프레임을 반환해 연결을 유지(트랙이 끝나도 소스가 종료되지 않음). 완전 종료는 `vc.stop()`/`disconnect()`.
- **BGM**(`!브금`): `mixer.set_base(src, on_exhausted=loop_cb)`. `on_exhausted`가 같은 파일을 다시 base로 올려 무한 반복(콜백 재무장). 정지/교체는 `active_volume_source(vc)`로 base 볼륨을 페이드 후 `clear_base`/`set_base`.
- **플리**(`PlaylistManager`): `player_loop`가 `vc.play` 대신 `mixer.set_base`로 트랙을 공급. 트랙 종료 시 `on_exhausted`→다음 곡. `skip()`/`pause()`/`resume()`는 `clear_base`+이벤트 / `pause_base`/`resume_base`로 동작(VC를 멈추지 않으므로 효과음 계속 가능).
- **볼륨**: `!볼륨`은 `core.active_volume_source(vc)`(=믹서 base의 PCMVolumeTransformer)에 적용. 레거시 직접 재생도 호환.
- **효과음 적재**: `play_dice_sfx(bot, guild)` → `guild.voice_client`의 믹서에 `PCMBytesAudioSource`(사전 디코드 PCM 캐시) 추가. 보이스 미연결/믹서 부재 시 조용히 no-op. 효과음 파일은 `media/_sfx/{이름}.mp3`, 시작 시 `preload_sfx("dice")`로 사전 디코드(`on_ready`).

**주사위 효과음 연출**: 4종 주사위 버튼 콜백에서 버튼 누름 즉시 `play_dice_sfx`(논블로킹) 발사.
- `GeneralDiceView`/`DiceView`([core/ui.py]): "굴리는 중…" 표시(`response.edit_message`) → `asyncio.sleep(1.5)` → 결과(`edit_original_response`). 효과음(`dice.mp3` ≈1.24초)이 1.5초 창 안에서 선행.
- `GMRollView`([auto_gm.py]): 효과음 발사 후 `_process_roll`에서 1.5초 하한 후 결과.
- `StatRollView`([character.py], `!능력치`): 효과음 1회만. 기존 스탯별 0.8초 애니메이션 유지(1.5초 미적용).

> 핫스왑 주의: `core/audio_mixer.py`·`core/media.py`·`core/ui.py`는 재시작 필요(핫스왑 불가). `cogs/media.py`·`cogs/character.py`·`cogs/gm.py`만 `!리로드` 가능.

### TTS 음성 더빙 (실험 기능 · core/tts.py)

AI 묘사를 음성 채널에서 **단일 나레이터 보이스**로 읽어주는 옵트인 기능. 현재 범위: **수동 `!진행`에만** 적용(GM·NPC 개별 보이스 제외).

- **믹서 voice 레이어**: `MixerAudioSource`에 `effects`(동시 중첩)와 별개로 **순차 재생 큐**(`enqueue_voice`/`clear_voice`/`is_voice_active`)를 둔다. 한 번에 한 문단만 재생하고, 재생 중에는 base를 `voice_duck_factor`(0.30)로 SFX보다 더 깊게 덕킹.
- **합성**(`core.synthesize_tts_pcm`): Gemini native TTS(`TTS_MODEL`, `response_modalities=["AUDIO"]` + `SpeechConfig`/`PrebuiltVoiceConfig`) 호출 → 24kHz mono PCM 추출 → `audioop`으로 **48kHz stereo 리샘플** → bytes 반환. 비용은 출력 오디오 토큰 기준(`PRICING_1M["gemini-2.5-flash-preview-tts"]` 입력 $0.50 / 출력 $10 per 1M).
- **음성-텍스트 동기 출력**(`GameCog._stream_paragraphs_synced`): `_execute_proceed`에서 토글 ON + 보이스 연결(`dub_active`) + 비GM일 때 사용하는 **기본 출력 경로**. 문단별로 ① TTS PCM을 합성(첫 문단 선합성, 다음 문단은 현재 문단 출력 중 `asyncio.create_task`로 prefetch) ② voice 큐에 적재(재생 시작) ③ 그 문단 텍스트를 **음성 길이**(`len(pcm) / TTS_PCM_BYTES_PER_SEC` 초)에 맞춘 속도로 스트리밍한다. `stream_text_to_channel(total_duration=...)`이 0.8초 간격 기준으로 틱당 단어 수를 환산해 총 출력 시간을 음성 길이에 수렴시킨다. 순차 voice 큐와 텍스트가 문단 단위 lock-step을 이루고, 합성 지연 시 음성·텍스트가 함께 대기해 재동기된다. 이미지(상/중/하) 인터리브는 비동기 경로와 동일 규칙.
- **비동기 폴백**(`GameCog._synthesize_and_enqueue`): 토글 OFF거나 보이스 미연결이면 기존 경로 사용. 토글 ON·미연결이면 `no_voice` 경고용으로만 백그라운드 합성을 시도한다. 합성 함수는 순수(회계 미수행)하며 `{enqueued,total,cost,in,out,no_voice}`를 반환한다.
- **비용 보고 일원화**: 턴 비용 임베드(`build_turn_cost_embed`)를 **스트리밍·더빙 합성 완료 후**에 송출하도록 미뤘다. 동기/비동기 경로 모두 합성 결과 dict(`dub`)를 받아 TTS 비용을 `turn_cost_log`에 `"TTS 더빙(n/m문단)"`으로 합산한 뒤 임베드를 보낸다(=PROCEED·지시층위·TTS가 한 임베드에). `!더빙테스트`는 턴 밖이라 자체적으로 `cost_log`에 "TTS 더빙(테스트)"로 기록.
- **토글**: `!더빙 [켜기/끄기]`(`session.tts_enabled`, `SESSION_FIELDS` 등록). 보이스 미연결 시 합성은 no-op + 마스터 채널 경고.
- **상수**(`core/constants.py`): `TTS_MODEL`/`TTS_NARRATOR_VOICE`/`TTS_LANGUAGE_CODE`. **WARNING**: `TTS_MODEL`은 사용 중인 API 키에서 실제 제공되는 TTS 모델 ID와 일치해야 한다(미일치 시 합성 0건 → 경고).

### 비용 추적

`PRICING_1M` 딕셔너리로 모델별 INPUT/OUTPUT/CACHE_READ/CACHE_STORAGE_PER_HOUR 단가를 관리한다. 모든 API 호출 후 `calculate_upload_cost()`로 KRW 비용을 계산해 `session.total_cost`에 누적하고 `write_cost_log()`로 `sessions/{id}/cost_log.txt`에 기록한다. 캐시 보관 비용은 초를 분 단위로 반올림하며 최대 21,600초(6시간) 상한을 적용한다. 환율은 1500 KRW/USD 고정.

**턴 비용 임베드 (`build_turn_cost_embed`)**: `session.turn_cost_log` 항목은 `{"label", "cost", "in"?, "cached"?, "out"?, "manifest"?}` 형태다(토큰·manifest는 선택 — 없으면 비용만 렌더, 하위호환). 임베드는 호출별로 **토큰 내역(입력/캐시/신규/출력) + 비용**을 개별 필드로 펼치고, 전 호출의 `manifest`를 **중복 제거 합산**해 `[📥 입력에 주입된 정보]` 필드로 제시한 뒤 토큰 합계·턴 소계·누적을 붙인다.
- **manifest(입력 주입 목록)**: 프롬프트 빌더가 실제 주입한 온디맨드 블록을 기록한다. PROCEED는 `PromptBuilder.build_prompt`가 `session.last_proceed_manifest`에(압축 기억·실시간 노트·NPC 오버라이드 n명·키워드북 목록), 지시층위는 `_build_logic_user_prompt`가 `session.auto_gm_last_logic_manifest`에(키워드북·세계 타임라인·정보 원장 n건·서사 계획·서사 시뮬 n방향·PROCEED 이력 n건·압축 기억) 기록. 둘 다 비영속 임시값. **키워드북 항목명이 그대로 표시되므로** 어느 문파·지역 항목이 입력을 부풀렸는지 즉시 진단 가능(비용 폭증 원인 추적에 직결).
- **`!정보` 비용 보고**: 답변 송출 후 `build_text_gen_cost_embed`로 토큰 내역 + `[📥 입력에 주입된 정보]`(캐시 룰북 + 매칭된 키워드북 항목명)를 임베드로 보고한다.

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
- `stat_descriptions` 항목(`{"스탯명": "설명"}` dict)을 정의하면 GM의 지시층위 프롬프트 `[PC 프로필]` 줄에 인라인으로 추가된다. ROLL 결정 시 스탯 용도를 AI가 즉시 파악할 수 있어 판정 스탯 선택 정확도가 높아진다. 미정의 시 생략된다.
  - `stat_system` 내에 스탯별 적용 분야 섹션도 함께 작성해 메인 AI(캐시)에도 반영할 것.

## 개발 주의사항

- `bot.active_sessions`에는 game_ch_id와 master_ch_id 양쪽이 등록된다. 채널 삭제 시 `_cleanup_session_memory()`가 두 키를 모두 pop해야 메모리 누수가 없다.
- `SESSION_FIELDS`에 새 `TRPGSession` 필드를 등록하면 저장·복구가 자동으로 처리된다. 핵심 필드(session_id, players 등)와 런타임 전용 필드(is_processing, auto_gm_lock 등)는 레지스트리에 넣지 않는다.
- `SCHEMA_VERSION`은 저장 JSON 구조가 변경될 때 증가시킨다. 현재 `2`.
- `!재생성`은 자동 기억 압축이 **백그라운드 실행 중(`session.is_compressing`)** 일 때만 잠시 차단된다(수 초). 압축 타이밍이 5N+1 프로씨드로 이동되어 **5의 배수 턴 자체는 롤백 가능**하다(과거의 `turn_count % 5 == 0` 상시 차단은 제거됨).
- `!수정`은 `last_turn_anchor_id`가 없으면 동작하지 않는다. 세션 복구 직후 `!진행` 전에 사용 불가.
- `!시작`은 실행 전 게임 채널의 모든 메시지를 `channel.purge`로 삭제한 뒤 `start_message`를 스트리밍하고 `role="model"`로 `raw_logs`에 삽입한다. 중복 실행 시 AI 컨텍스트가 오염되므로 `is_started` 플래그로 차단된다.
- `build_scenario_cache_text()`는 3-튜플 `(padded_text, tokens, base_text)`를 반환한다. 모든 호출부에서 3개를 언팩해야 한다. 캐시 생성 후 반드시 `update_session_cache_state(session)` 호출 필요 (session.py / system.py / game.py / core/cache.py 복구 경로 모두).
- `!증감`은 `key` 인자에 따라 3가지 모드로 분기한다: 스탯 수치 증감(기본), `자원` 키워드(resources 딕셔너리), `상태` 키워드(statuses 리스트). NPC 이름도 char_name으로 사용 가능하며 PC 탐색 없이 resources/statuses에 직접 접근한다.
- `!프로필`은 기본적으로 마스터 채널에 출력하고, `게임` 인자를 붙이면 게임 채널에 출력한다.
- `!능력치`는 마스터 채널에서만 실행 가능. `ability_stats`가 시나리오 JSON에 없으면 명령어 실행이 차단된다.
- `!엔피씨 설정`은 3-모드로 분기한다: ① 첫 단어가 `npc_template.info_fields`의 항목명이면 단일 필드 수정, ② `**필드명**: 값` 형식이 감지되면 구조화 자동 파싱, ③ 그 외는 레거시 `details` 전체 덮어쓰기. 모드 ①·②는 `details` 필드를 제거해 구/신 혼재를 방지한다.
- `add_npc_override_block`은 `cached_session_npcs`를 세션 NPC의 delta 비교 기준으로 사용한다. 캐시 재발급 전에 만들어진 세션 NPC는 `cached_session_npcs`에 없으므로 전체 프로파일이 매 턴 주입된다. 재발급 후에는 변경된 필드만 delta로 주입된다.
- `_plan_narrative` 내에서 직접 마스터 채널에 계획 결과를 보고한다. `!자동 재계획` 실행 시에도 동일.
- `paused_session/` 폴더는 자동 복구 대상이 아니다. 복구하려면 `sessions/`로 이동해야 한다.
- `sessions/` 폴더는 `.gitignore`에 포함되어 있다.
- NARRATE에서 `max_output_tokens`를 설정하지 않는다. `gemini-3-flash-preview`는 thinking 모델로, `max_output_tokens`를 지정하면 thinking 토큰이 한도를 소진하여 실제 텍스트 출력이 거의 없는 조기 종료가 발생한다. 출력 길이는 프롬프트 지시로 제어한다.
- 강제/정상 PROCEED 후처리는 반드시 `_finish_proceed_and_continue`를 거친다. 새 PROCEED 분기를 추가할 때 후처리 블록을 손으로 복붙하지 말 것 — `event_assessment`만 인자로 넘기면 된다.
- `_run_gm_logic_loop` 내부 `save_session_data`는 의도적으로 일원화되어 있다. ASK/NARRATE 대기 분기는 개별 저장하지 않고 루프 종료 후 트레일링 save 한 번으로 처리하며, `_start_round`는 직후 `_ask_next_player`의 저장에 위임한다. 분기에 저장을 새로 추가하기 전에 중복 여부를 확인할 것.
- `_verify_proceed_instruction`(방안 E) + `PROCEED_VERIFIER_SYSTEM_INSTRUCTION` + `PROCEED_VERIFY_SCHEMA`는 방안 D 도입 이후 **호출되지 않는 미사용 코드**다(미선언 PC 행동 방지는 지시층위 [최우선 절대 원칙]이 담당). 제거 대기 상태이며, 살릴 경우 `_finish_proceed_and_continue` 진입 전에 호출해야 한다.
- `_execute_proceed`는 AI 응답을 raw_logs 저장·문단 파싱에 넘기기 **전에** `core.strip_unauthorized_pc_dialogue`로 'NPC가 아닌 PC 이름'으로 된 `@대사:` 문단을 문자열 단계에서 제거한다(AI의 PC 대사 임의 창작 차단). PC이면서 NPC이기도 한 이름·미상 인물 대사는 보존. 제거 시 마스터 채널에 알림.
- `!설정/증감/주사위/외형/프로필`은 이름 인자를 `core.resolve_pc`(PC 전용) 또는 `core.resolve_char_name`로 **부분 입력→고유 매칭** 해석한다. 우선순위 정확→고유 접두→고유 부분. 모호하면 후보를 안내하고 중단, 미발견이면 기존 "찾을 수 없습니다". `!증감`만 `resolve_char_name(include_npc=True)`로 NPC도 후보에 넣고, 미매칭 시 입력값을 그대로 사용해(임의·신규 자원/상태 대상 허용) 기존 동작을 보존한다.
