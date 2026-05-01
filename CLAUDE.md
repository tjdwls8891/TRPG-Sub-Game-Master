# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

Gemini API + discord.py 기반의 한국어 TRPG 보조 GM 디스코드 봇. GM이 마스터 채널에서 명령어를 입력하면 AI가 묘사를 생성하고, 비용 추적·캐시 관리·기억 압축·BGM/이미지 연출을 자동화한다.

## 실행 및 환경 설정

```bash
pip install -r requirements.txt
python main.py
```

`.env` 파일이 필요하다 (`.env.example` 참고). 필수 환경 변수:
- `DISCORD_TOKEN` — 디스코드 봇 토큰
- `GEMINI_API_KEY` — Gemini API 키
- `TRPG_INTRO_TEXT` — !소개 명령어에 포함되는 공통 인트로 텍스트

> NOTE: 이전에 `.env`에 있던 `SYSTEM_INSTRUCTION`은 코드 영역(`prompts.py`)으로 분리되었다. 코드 변경과 함께 프롬프트도 같이 리뷰·수정하기 위함이다. 변경 시 활성 세션은 `!캐시 재발급`으로 캐시를 갱신해야 반영된다.

코드 수정 후 봇을 재시작하지 않고 특정 모듈만 반영하려면 마스터 채널에서:
```
!리로드 [모듈명]   # 예: !리로드 game
```
`core.py`와 `main.py`는 핫스왑 불가, `cogs/` 하위 파일만 가능.

## 아키텍처

### 상태 관리 흐름

`TRPGSession`(core.py)이 단일 세션의 모든 상태를 담는 중앙 컨테이너다. `bot.active_sessions` 딕셔너리에 **game_ch_id와 master_ch_id 양쪽 모두** 동일한 세션 객체를 키로 등록한다. 따라서 어느 채널에서든 `session = bot.active_sessions.get(ctx.channel.id)` 한 줄로 세션에 접근할 수 있다.

세션 상태는 `save_session_data(bot, session)` 호출마다 `sessions/{session_id}/data.json`에 직렬화된다. 봇 재시작 시 `restore_sessions_from_disk(bot)`이 이를 복구하며, Gemini 캐시가 만료된 경우 자동으로 재발급한다.

### 프롬프트 조립 순서 (PromptBuilder)

`PromptBuilder.build_prompt(session, gm_instruction)`은 아래 순서로 블록을 조립한다:
1. `compressed_memory` (압축된 장기 기억)
2. `session.note` (GM 하드코딩 노트, 매 턴 주입)
3. 플레이어 스탯·외형·resources·statuses
4. **NPC 델타만**: `session.npcs`에서 `default_npcs` 원본 details와 달라진 NPC 또는 런타임 resources/statuses가 있는 NPC만 주입 (`add_npc_override_block`)
5. **트리거 키워드 기억만**: `keyword_memory`의 keywords가 최근 로그 결합 문자열에 있을 때만 주입
6. `current_turn_logs` (현재 턴 행동)
7. GM 지시사항
8. 최종 룰 강제 + status_code_block 출력 지시

### NPC 주입 전략

모든 `default_npcs`는 Gemini Context Cache의 `[3. NPC 사전]` 섹션에 전체 수록되므로 프롬프트에 중복 주입하지 않는다. 프롬프트(`add_npc_override_block`)에는 아래 두 경우만 델타로 주입한다:
1. `!엔피씨 설정`으로 details가 변경된 NPC → 캐시 내용을 덮어씀
2. 세션 중 resources/statuses가 부여된 NPC → 캐시에 없는 런타임 상태 동기화

### Gemini Context Caching

시나리오 룰북(worldview, story_guide, **NPC 사전 전체**, stat_system, desc_guide, status_code_block)을 하나의 텍스트로 조립해 Gemini 서버에 캐싱한다. **최소 32,768 토큰** 미만이면 `"."` 문자 패딩을 `[System Data Padding Area - DO NOT READ]` 헤더와 함께 추가해 요건을 충족한다(의도된 핵). `session.cache_note`가 있으면 캐시 재발급 시 룰북 하단에 지연 병합된다.

`build_scenario_cache_text()`는 `(padded_text, total_tokens, base_rulebook_text)` 3-튜플을 반환한다. `base_rulebook_text`(패딩 제외 원본)는 `session.cache_text`에 저장되어 `!캐시 출력`으로 디버그 확인이 가능하다.

턴 진행(`!진행`) 중 캐시 만료 에러(400/404)가 발생하면 `generate_with_retry()`가 자동으로 캐시를 재발급하고 묘사를 이어서 출력한다.

### 기억 압축 시스템

턴이 완료될 때마다 `uncompressed_logs`에 해당 턴의 원본 로그를 누적한다. `turn_count % 5 == 0`이 되면 백그라운드에서 `LOGIC_MODEL`로 압축 요청을 보내고, 결과를 `compressed_memory`에 append한 뒤 `uncompressed_logs`에서 삭제한다. `raw_logs`는 최근 20개만 유지한다.

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
- **PC**: 외모 전용 5개 고정 필드 (체형/얼굴/피부·헤어/복장/첫인상), 결과를 `!외형`으로 적용
- **NPC**: 종합 프로파일 12개 고정 필드 (나이·성별/소속·직책/외모/핵심 기질/행동 방식/말투·어조/동기·욕구/두려움·약점/비밀/신뢰·의존/경계·반목/특기·능력), 결과를 `!엔피씨 설정`으로 적용
- 특수 태그 `엔:이름[,이름]`으로 참조 NPC 설정 주입 가능

### 자동 GM 모드 (Auto-GM)

`!자동시작`이 호출된 세션에서만 활성화되는 옵트인 모드. 인간 GM의 ①행동 촉구, ②의도 명확화, ③판정 선언·실행, ④결과 선언, ⑤상황 전이 선언, ⑥`!진행` 트리거, ⑦태그 판단을 자동화한다.

**아키텍처: 2-티어 AI 루프**
- **Tier 1 (GM-Logic)**: `cogs/auto_gm.py`에서 `DEFAULT_MODEL`로 호출. `response_mime_type="application/json"` + `response_schema`로 강제된 결정 JSON 출력 (`action`: `ASK`/`ROLL`/`PROCEED`).
- **Tier 2 (묘사 생성)**: `GameCog._execute_proceed()` 헬퍼 직접 호출 (캐시 적중 그대로 활용).

**메시지 라우팅**: `on_message`가 게임 채널 발언만 큐잉. 마스터 채널 발언은 명령어 외에는 무시(설계 결정). 봇 메시지·`!`로 시작하는 메시지도 무시.

**처리 루프**: 한 플레이어 발언당 최대 5회 반복(`MAX_ITERATIONS_PER_MESSAGE`). `ASK`는 짧은 안내만 게임 채널에 송출 후 다음 발언 대기. `ROLL`은 `random.randint`로 즉시 굴리고 결과를 컨텍스트에 주입한 채 재호출. `PROCEED`는 `_execute_proceed`를 호출하고 루프 종료.

**안전장치**:
- `auto_gm_turn_cap` (기본 10) — 누적 자동 턴 도달 시 자동 정지
- `MAX_CLARIFY_PER_MESSAGE = 2` — 같은 발언에 ASK 2회 초과 시 강제 PROCEED
- `auto_gm_cost_cap_krw` (기본 500) — 자동 모드 누적 비용 도달 시 정지
- 세션별 `asyncio.Lock`으로 동시 처리 방지

**비용 로그 분리**: 자동 모드 호출은 `cost_log.txt`에 `[AUTO]` 접두사로 기록 (예: `[AUTO] 턴 진행 생성`, `[AUTO] GM-Logic 호출`). 비용 보고 메시지에도 `(자동 GM)` 라벨이 붙는다.

**`_execute_proceed` 헬퍼**: `proceed_turn` 명령 본체를 추출한 메서드. `ctx`에 의존하지 않고 `(session, instruction, master_guild, cost_log_prefix)` 인자만 받는다. 명령 진입점(`!진행`)은 thin wrapper. 자동 GM은 `cost_log_prefix="[AUTO] "` 인자로 호출.

### 비용 추적

`PRICING_1M` 딕셔너리로 모델별 INPUT/OUTPUT/CACHE_READ/CACHE_STORAGE_PER_HOUR 단가를 관리한다. 모든 API 호출 후 `calculate_upload_cost()`로 KRW 비용을 계산해 `session.total_cost`에 누적하고 `write_cost_log()`로 `sessions/{id}/cost_log.txt`에 기록한다. 캐시 보관 비용은 초를 분 단위로 반올림하며 최대 21,600초(6시간) 상한을 적용한다. 환율은 1500 KRW/USD 고정.

## 파일 맵

| 파일 | 역할 |
|------|------|
| `main.py` | TRPGBot, active_sessions, setup_hook, restore_sessions_from_disk |
| `core.py` | 전역 상수, TRPGSession, PromptBuilder, build_scenario_cache_text, 유틸리티, UI 클래스 |
| `cogs/session.py` | !새세션, !시작, !소개 |
| `cogs/game.py` | !진행, !재생성, !출력물, !수정, !주사위, !기억압축, !노트, !캐시노트 |
| `cogs/character.py` | !참가, !설정, !증감(스탯/자원/상태), !외형, !프로필, !엔피씨, !능력치, !설정생성 |
| `cogs/media.py` | !이미지, !브금, !플리, !볼륨, !채팅 |
| `cogs/system.py` | !명령어, !채널정리, !세션종료, !캐시, !리로드 |
| `cogs/auto_gm.py` | !자동시작, !자동중단, !자동상태, !자동개입, !자동턴제한 — AI 자동 GM 모드 |

## 주요 상수 (core.py)

```python
DEFAULT_MODEL = "gemini-3-flash-preview"   # 턴 묘사, 캐시
LOGIC_MODEL   = "gemini-3-flash-preview"   # 기억 압축, 설정생성 (Pro 모델은 주석 처리됨)
IMAGE_MODEL   = "gemini-3.1-flash-image-preview"
EXCHANGE_RATE = 1500.0
```

## 시나리오 JSON 작성 시 주의사항

- `default_npcs`에 정의된 NPC는 캐시에 구워진다. 게임 중 변경은 `session.npcs`(`!엔피씨 설정`)로 오버라이드해야 한다. 이름 트리거 없이 모든 NPC가 캐시에 전체 수록된다.
- `status_code_block`을 정의하면 매 턴 AI 응답의 마지막에 코드블럭 출력이 강제된다. 없으면 생략된다.
- `!이미지 생성` 명령어는 `scenarios/{시나리오명}.json`을 직접 덮어쓴다 (media_keywords 영구 추가).
- `keyword_memory`의 키워드는 최근 로그 전체를 단순 문자열로 `in` 검사하므로 짧고 구체적인 고유명사로 작성할 것.
- `image_prompts`에 형식키별 `prompt`와 `aspect_ratio`를 정의해야 `!이미지 생성`이 동작한다.
- `profile_secondary_stats`에 `pc_template` 항목명을 리스트로 지정하면 `!프로필` 임베드에서 구분선 아래 전체 폭 필드로 표시된다. 미지정 항목은 구분선 위 인라인 3열 격자에 배치된다. 예: `"profile_secondary_stats": ["서사", "배경"]`
- `ability_stats`에 `pc_template` 항목명을 리스트로 지정하면 `!능력치` 명령어에서 주사위 굴림 대상이 된다. 순서대로 굴림이 진행되고 Hamilton 방식으로 target_total에 비례 배분된다. 예: `"ability_stats": ["근력", "민첩", "지능", "매력"]`

## 개발 주의사항

- `bot.active_sessions`에는 game_ch_id와 master_ch_id 양쪽이 등록된다. 채널 삭제 시 `_cleanup_session_memory()`가 두 키를 모두 pop해야 메모리 누수가 없다.
- `!재생성`은 `turn_count % 5 == 0`(압축 직후)일 때 차단된다. 이 경우 롤백 대신 다음 턴으로 교정해야 한다.
- `!수정`은 `last_turn_anchor_id`가 없으면 동작하지 않는다. 세션 복구 직후 `!진행` 전에 사용 불가.
- `!시작`은 `start_message`를 `role="model"`로 `raw_logs`에 삽입한다. 중복 실행 시 AI 컨텍스트가 오염되므로 `is_started` 플래그로 차단된다.
- `build_scenario_cache_text()`는 3-튜플 `(padded_text, tokens, base_text)`를 반환한다. 모든 호출부에서 3개를 언팩해야 한다 (session.py, game.py, system.py, core.py 내 restore_sessions_from_disk).
- `!증감`은 `key` 인자에 따라 3가지 모드로 분기한다: 스탯 수치 증감(기본), `자원` 키워드(resources 딕셔너리), `상태` 키워드(statuses 리스트). NPC 이름도 char_name으로 사용 가능하며 PC 탐색 없이 resources/statuses에 직접 접근한다.
- `!프로필`은 기본적으로 마스터 채널에 출력하고, `게임` 인자를 붙이면 게임 채널에 출력한다. 임베드는 구분선 위(인라인 3열 격자 스탯) → 구분선 → 2차 스탯(`profile_secondary_stats`) → 외형 → 소지 자원 → 상태이상 순으로 구성된다.
- `!능력치`는 마스터 채널에서만 실행 가능하며, 게임 채널에 버튼 UI를 전송한다. `StatRollView`(character.py)가 굴림 대상 유저 검증(`target_uid`), 순차 굴림 애니메이션(0.8s 딜레이), Hamilton 배분, `session.players` 자동 저장을 담당한다. `ability_stats`가 시나리오 JSON에 없으면 명령어 실행이 차단된다.
- `paused_session/` 폴더는 자동 복구 대상이 아니다. 복구하려면 `sessions/`로 이동해야 한다.
- `sessions/` 폴더는 `.gitignore`에 포함되어 있다.
