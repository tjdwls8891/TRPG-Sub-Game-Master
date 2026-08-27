# TRPG Sub GM Bot — 개발 로그 (DEVLOG)

> 현재 버전: **v5.14.1**  
> 최종 갱신: 2026-06-27 (오디오 믹서·TTS 더빙, 명령어 가이드 갱신)  
> 스택: Gemini API + discord.py / 한국어 TRPG 보조 GM 봇

---
 
## 목차

1. [아키텍처 요약](#아키텍처-요약)
2. [개발 이력](#개발-이력)
3. [잔여 개발 계획](#잔여-개발-계획)

---

## 아키텍처 요약

| 파일 | 역할 |
|------|------|
| `main.py` | 봇 엔트리포인트, active_sessions, cog 자동 로드, 세션 복구 |
| `core/` | 10개 서브모듈 패키지 — `constants/models/cost/io/cache/prompt/dialogue/media/audio_mixer/tts/ui/utils`. `__init__.py`가 전 심볼 re-export (기존 `core.XYZ` 호출부 무수정) |
| `core_legacy.py` | 분리 이전 단일 core.py 백업 (롤백용, 운영 미사용) |
| `prompts.py` | SYSTEM_INSTRUCTION + GM 시스템 지시문·응답 스키마 모음 (지시층위·서사 계획·시뮬레이터·타임라인) |
| `cogs/session.py` | !새세션, !시작, !소개 |
| `cogs/game.py` | !진행, !재생성, !출력물, !수정, !주사위, !기억압축, !노트, !캐시노트, !더빙테스트 |
| `cogs/character.py` | !참가, !설정, !증감, !외형, !프로필, !엔피씨, !능력치, !설정생성 |
| `cogs/media.py` | !이미지, !브금, !플리, !볼륨, !채팅, !더빙 |
| `cogs/system.py` | !명령어, !채널정리, !세션종료, !캐시, !리로드 |
| `cogs/gm.py` | `!자동` 그룹 (시작/중단/상태/개입/턴제한/비용제한/서사/재계획) |

**핵심 데이터 흐름:**
- 상태는 `TRPGSession` 객체 하나에 집중 관리
- `bot.active_sessions[game_ch_id]` = `bot.active_sessions[master_ch_id]` = 동일 세션 객체
- 매 상태 변경마다 `save_session_data()` → `sessions/{id}/data.json` 직렬화
- 봇 재시작 시 `restore_sessions_from_disk()` 자동 복구

---

## 개발 이력

### ✅ 기반 시스템 (v1.0~v1.4)

#### 세션·채널 관리
- UUID 기반 샌드박스 채널 프로비저닝 (`!새세션`)
- 게임 채널 / 마스터 채널 권한 분리 생성
- 세션 JSON 직렬화·복구 (`data.json`, `restore_sessions_from_disk`)
- 세션 종료 시 캐시 파기 + 스토리지 비용 정산 (`!세션종료`)

#### Gemini Context Caching
- 시나리오 룰북(worldview · story_guide · NPC사전 · stat_system · desc_guide · status_code_block)을 단일 텍스트로 조립해 서버 캐싱
- 최소 32,768 토큰 요건 미달 시 `"."` 패딩 삽입 해킹 (`build_scenario_cache_text`)
- `(padded_text, total_tokens, base_text)` 3-튜플 반환 구조 표준화
- 캐시 만료(400/404) 자동 감지 + 재발급 후 재시도 (`generate_with_retry`)
- `session.cache_note` 지연 병합: 캐시 재발급 시 룰북 하단에 자동 추가

#### 비용 추적 시스템
- `PRICING_1M` 딕셔너리 기반 모델별 단가 (INPUT / OUTPUT / CACHE_READ / STORAGE_PER_HOUR)
- `calculate_text_gen_cost_breakdown()`: 항목별 분해 (신규 입력 / 캐시 적중 / 출력)
- `calculate_storage_cost()`: 초→분 반올림, 6시간 상한 캡 적용
- `write_cost_log()` → `sessions/{id}/cost_log.txt` 타임스탬프 기록
- 환율 고정: 1,500 KRW/USD

#### 기억 압축 시스템
- `turn_count % 5 == 0` 자동 트리거, `!기억압축` 수동 강제 실행
- `uncompressed_logs` → `LOGIC_MODEL` → `compressed_memory` 누적 append
- `raw_logs` 최근 20개 슬라이딩 윈도우 유지

#### 프롬프트 빌더 (`PromptBuilder`)
- 주입 순서 표준화: 압축 기억 → GM 노트 → 플레이어 스탯 → NPC 델타 → 키워드 기억 → 현재 턴 행동 → GM 지시 → 최종 룰 강제
- NPC 주입 전략: `default_npcs`는 캐시에만 보관, 프롬프트에는 **델타(변경·런타임 상태)만** 주입
- `keyword_memory`: 최근 로그+지시문에 키워드가 있을 때만 관련 기억 활성화

---

### ✅ 게임 진행 시스템 (v1.4~v1.5)

#### !진행 태그 파싱
- 태그 추출 후 `clean_instruction`에서 제거하여 AI에 전달 (태그는 Python이 처리)
- `상/중/하:키워드` — 이미지 삽입 타이밍 제어 (첫 문단 후 / 키워드 등장 시 / 묘사 끝 후)
- `자:이름;아이템;수치` — `session.resources[이름][아이템] += 수치`
- `태:이름;상태` / `태:이름;-상태` — `session.statuses` 추가/제거

#### !출력물 / !수정 시스템
- `!출력물`: 직전 턴 `role="model"` 텍스트를 1,950자 청크로 마스터 채널 전송
- `!수정`: `last_turn_anchor_id` 이후 봇 메시지를 Discord `edit()` API로 덮어쓰기
  - `raw_logs`, `uncompressed_logs`, `game_chat_log.txt` 동기화
  - 메시지 수 불일치 시 자동 추가/삭제

#### !재생성 롤백
- 앵커 메시지 이후 봇 출력 Discord `purge()`
- `raw_logs[-2:]`에서 `current_turn_logs` 복원
- `turn_count % 5 == 0` (압축 직후) 시 롤백 차단

#### !시작 role 조작 패턴
- `start_message`를 `types.Content(role="model")`로 `raw_logs`에 직접 삽입
  → AI가 "내가 이 묘사를 했다"고 인식 → GM 스탠스 자동 유지
- `is_started` 플래그로 이중 실행 차단

---

### ✅ 캐릭터 / NPC 시스템 (v1.5)

- `!참가`: `pc_template.copy()` → `session.players` 등록, 디스코드 닉네임 자동 변경
- `!설정` / `!증감`: 스탯 수치 whitelist 검증 후 수정
- `!증감`: `key` 인자에 따라 스탯 수치 / `자원` / `상태` 3-모드 분기, NPC도 대상 가능
- `!외형`: `session.players[uid]['appearance']` 조작
- `!프로필`: 인라인 3열 격자(기본 스탯) + 구분선 + 2차 스탯 + 외형 + 자원 + 상태이상 Embed
  - `profile_secondary_stats` 시나리오 키로 임베드 레이아웃 동적 구성
- `!엔피씨 설정` / `!엔피씨 확인` / `!엔피씨 삭제` / `!엔피씨 목록`
- `!능력치`: `ability_stats` 키 기반 순차 D20 굴림, Hamilton 방식 `target_total` 비례 배분
  - `StatRollView` UI: 대상 유저 검증 + 0.8s 딜레이 애니메이션 + 자동 저장
- `!설정생성`: LOGIC_MODEL 호출, PC(외모 5항목) / NPC(종합 12항목) 고정 양식
  - `엔:이름[,이름]` 태그로 참조 NPC 설정 교차 주입

---

### ✅ 미디어 시스템 (v1.5)

- `!이미지 [키워드]`: `media_keywords` 매핑 → 게임 채널 전송
- `!이미지 생성 [형식키] [키워드] [프롬프트]`: IMAGE_MODEL 호출, PNG 저장, `scenario.json` media_keywords 영구 갱신
  - `레:키워드` 태그로 레퍼런스 이미지 첨부 가능
- `!브금`: 단일 트랙 무한 루프 / 페이드아웃 정지
- `!플리`: `PlaylistManager` 백그라운드 셔플 재생 (다음/이전/일시정지/재생)
- `!볼륨`: 세션 BGM + 플리 볼륨 동시 적용
- `!채팅 [잠금/해제]`: `@everyone send_messages` 토글

---

### ✅ 인물 대사 자동 포매팅

- AI 출력 `@대사:이름|본문` 마커 감지 → `parse_dialogue_paragraph()`
- `maybe_send_speaker_image()`: `media_keywords` 또는 `media/{scenario_id}/{이름}.png` 직접 검사 후 이미지 자동 선송출
- `format_dialogue_block()`: `## ▍이름\n## 「 본문 」` 형식 변환
- `merge_consecutive_dialogues()`: 동일 화자 연속 대사 통합 (이미지 중복 방지)
- `stream_text_to_channel()`: 대사 문단은 `quote_prefix=False`로 `> ` 접두 생략

---

### ✅ GM — Auto-GM (v1.6~v1.7)

#### 기반 구조 (v1.6)

- `!자동시작`: 옵트인 모드 활성화, 대상 PC 지정 (단일 / 멀티플레이어)
- **2-티어 AI 루프**:
  - **Tier 1 (지시층위)**: `DEFAULT_MODEL` + `response_mime_type="application/json"` + `response_schema` → 결정 JSON 강제 출력
  - **Tier 2 (묘사 생성)**: `GameCog._execute_proceed()` 직접 호출 (캐시 적중 유지)
- `asyncio.Lock` 기반 세션별 동시 처리 방지
- **안전장치**:
  - `auto_gm_turn_cap` (기본 `None`=무제한): 누적 자동 턴 한도 (`!자동 턴제한 [N|해제]`)
  - `MAX_CLARIFY_PER_MESSAGE = 2`: ASK 2회 초과 시 강제 PROCEED
  - `MAX_NARRATE_PER_MESSAGE = 7`: NARRATE 7회 초과 시 강제 PROCEED
  - `auto_gm_cost_cap_krw` (기본 `None`=무제한): 자동 모드 누적 비용 한도 (`!자동 비용제한 [원|해제]`)
  - 두 캡 모두 `None` 가드 필수 (`cap is not None and used >= cap`)
- 비용 로그 `[AUTO]` 접두사 분리 기록

#### #22 — 멀티플레이어 라운드 수집
- PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 순서대로 질문
- `auto_gm_pending_players` 큐 기반 순차 수집 → 전체 완료 시 지시층위 호출
- 멀티플레이어 종합 시 게임 채널에 행동 선언 요약 표시
- `auto_gm_waiting_for`: 특정 PC 응답 대기 중 다른 PC 발언 무시

#### #25 — 능동적 서사 진행 원칙
- `proceed_instruction` 작성 규칙 지시층위 시스템 지시문에 명시:
  - 플레이어 행동의 자연스러운 결과 반영
  - 세계가 멈춰 있지 않음을 드러내는 **신규 사건** 능동 생성
  - 단순 이동·대기 상황에서도 반드시 환경 변화 발생
  - 시나리오 미정의 소규모 사건은 설정 범위 내에서 파생 허용

#### #8 — GM 대화 모드 NARRATE (v1.7 신규)
- **목적**: PROCEED 없이 해결 가능한 가벼운 질문 응답·상황 설명·NPC 짧은 반응
- **action 체계 4→4가지 확장**: ASK / **NARRATE** / ROLL / PROCEED
- **Method B (캐시 기반 경량 LLM 호출)**:
  - 최근 raw_logs 4개(400자 제한) + 현재 턴 누적 대화 + narrate_instruction
  - `max_output_tokens=220` (≈ 300자), `temperature=0.65`
  - 캐시 히트 시 비용 ≈ PROCEED의 절반 이하
- **대사 마커 지원**: `@대사:이름|본문` 파싱 → 이미지 선송출 + 말풍선 포맷
- **로그 통합**: `current_turn_logs.append("[진행자 (GM)]: ...")` → PROCEED 시 AI 맥락 유지
- **안전장치**: `MAX_NARRATE_PER_MESSAGE = 7`, 초과 시 강제 PROCEED + 카운트 초기화
- **카운트 초기화 지점**: PROCEED 완료, `_start_round()`, ASK/ROLL 강제 PROCEED, 루프 한도 초과

---

### ✅ core/ 패키지 분리 · 서사 시스템 · GM 루프 정리 (v1.8~v1.9)

#### core.py → core/ 10개 서브모듈 분리
- 단일 `core.py`(1,896줄)를 `constants/models/cost/io/cache/prompt/dialogue/media/ui/utils`로 분리
- `core/__init__.py` re-export로 기존 `import core` / `core.XYZ` 호출부 무수정
- 의존성 단방향 `constants→models→cost→io→cache`, 순환 임포트 없음. `core_legacy.py`는 롤백 백업

#### Auto-GM 서사 계획 / 시뮬레이션 / 세계 타임라인
- **서사 계획(Narrative Plan)**: 사건 단위 `current_event`/`next_event` 구조. `!자동 시작` 시 수립, `event_assessment`가 `completed`/`deviated`면 재계획. `!자동 서사`/`!자동 재계획`
- **서사 방향성 시뮬레이션(방안 6)**: 지시층위 결정 전 세계관 캐시 기반으로 방향성 2~3개 사전 산출 → 결정에 주입
- **세계 물리 타임라인(방안 B)**: PROCEED 후 묘사에서 위치·시간대·세력·위협 추출, 시뮬레이터 기준 데이터로 사용

#### GM 루프 정리 (2026-06-17)
- **시뮬레이션 순차 주입**: 과거 `asyncio.gather` 병렬 실행은 첫 결정이 `sim_result`를 못 보고 비용만 낭비 → 시뮬레이션을 먼저 실행하고 결과를 `_call_gm_logic`에 주입하도록 교정
- **`_finish_proceed_and_continue` 헬퍼 추출**: 6곳(루프 5개 강제/정상 PROCEED + `_continue_with_roll_results`)에 복붙되던 후처리 블록 단일화
- **중복 `save_session_data` 제거**: ASK/NARRATE 대기 분기 → 트레일링 save 위임, `_start_round` → `_ask_next_player` 위임 (auto_gm.py 저장 호출 24→16)
- `_verify_proceed_instruction`(방안 E)는 방안 D 이후 미사용 코드로 잔존 (제거 대기)

---

### ✅ 오디오 믹싱 · TTS 음성 더빙 (v1.9)

#### PCM 실시간 믹서 (`core/audio_mixer.py`)
- discord.py `VoiceClient`는 동시에 한 AudioSource만 재생 → BGM/플리 위에 효과음·음성을 겹치려면 봇이 PCM을 직접 합산해야 함
- `MixerAudioSource`: `VoiceClient.play()` 단 1회. `base`(BGM/플리 트랙) + `effects`(효과음 동시 중첩) + `voice`(순차 재생 큐)를 매 20ms 프레임마다 `audioop.add`로 합산
- 효과음 재생 중 base를 `duck_factor`(0.55) 감쇠, 음성 재생 중 `voice_duck_factor`(0.30) 더 깊게 덕킹
- base·effects·voice 모두 없으면 무음 프레임 반환해 연결 유지 (트랙 종료 ≠ 소스 종료)
- `!브금`/`!플리`/`!볼륨` 모두 `set_base`/`active_volume_source` 기반으로 전환 — 효과음·음성이 끊기지 않음
- **주사위 효과음**: `media/_sfx/dice.mp3` `on_ready` 사전 디코드(`preload_sfx`), 4종 주사위 버튼 콜백에서 `play_dice_sfx` 논블로킹 발사

#### TTS 음성 더빙 (실험 기능 · `core/tts.py`)
- AI 묘사를 음성 채널에서 단일 나레이터 보이스로 낭독하는 옵트인 기능 (현재 수동 `!진행` 한정)
- `synthesize_tts_pcm`: Gemini native TTS(`TTS_MODEL`) → 24kHz mono PCM → `audioop` 48kHz stereo 리샘플
- **음성-텍스트 동기 출력**(`_stream_paragraphs_synced`): 문단별 TTS PCM 합성(다음 문단 prefetch) → voice 큐 적재 → 텍스트를 음성 길이에 맞춰 스트리밍, 문단 단위 lock-step
- 비동기 폴백(`_synthesize_and_enqueue`): 토글 OFF·미연결 시 기존 경로
- **비용 보고 일원화**: 턴 비용 임베드를 합성 완료 후로 미뤄 PROCEED·지시층위·TTS를 한 임베드에 합산
- 토글 `!더빙 [켜기/끄기]`(`session.tts_enabled`), 즉시 재생 `!더빙테스트 (보이스)`
- 상수 `TTS_MODEL`/`TTS_NARRATOR_VOICE`/`TTS_LANGUAGE_CODE`(`core/constants.py`) — `TTS_MODEL`은 API 키 제공 모델 ID와 일치해야 함

---

### ✅ prompts.py 분리 (v1.6)

- 기존 `.env`의 `SYSTEM_INSTRUCTION` → `prompts.py` 코드 영역으로 분리
- 프롬프트와 코드를 함께 리뷰·수정 가능
- 변경 후 활성 세션은 `!캐시 재발급` 필요

---

## 잔여 개발 계획

### 🤖 자동진행 (Auto-GM) 개선

| # | 항목 | 내용 | 파일 |
|---|------|------|------|
| 23 | ~~`proceed_instruction` 정비~~ ✅ | PROCEED 섹션 재작성. 태그 의무 검토 원칙(① 자원 소비·획득 시 `자:` 필수, ② 상태 변동 시 `태:` 필수), 최소 2문장 요건, [행동 결과]+[세계 능동 반응] 구조 의무화, 나쁜/좋은 예시 삽입. | `cogs/gm.py` |
| 24 | ~~지시사항 없는 `proceed_turn` 정비~~ ✅ | `!진행` 지시사항이 비어있거나 태그만 있어 `clean_instruction`이 공백이 될 때, `GMCog._call_gm_logic()`을 호출해 `proceed_instruction`을 자동 생성. Auto-GM 모드(`cost_log_prefix` 있음)는 항상 채워진 상태로 진입하므로 건너뜀. 생성된 지시사항은 마스터 채널에 표시 후 `_execute_proceed`에 전달. | `cogs/game.py` |
| 29 | ~~ASK/NARRATE 입력 중 표시~~ ✅ | `_run_gm_logic_loop` 루프 본체에서 `_call_gm_logic` 호출을 `async with game_ch.typing():` 으로 감싸 지시층위 응답 대기 동안 입력 중 상태 표시. NARRATE의 경우 `_dispatch_narrate` 호출도 동일하게 감쌈. PROCEED는 기존 `generate_with_retry` 내부 타이핑 유지. | `cogs/gm.py` |

---

### 📝 프롬프트 / 시스템 인스트럭션

| # | 항목 | 내용 | 파일 |
|---|------|------|------|
| 1 | ~~자동압축 프롬프트 정비~~ ✅ | `build_compression_prompt()` 완전 재작성. 무손실 압축 원칙 명시 + 8개 필수 보존 카테고리(행동·판정·대화·획득정보·감각정보·상태변동·장소이동·NPC변화) + 턴 단락 출력 양식(`[#N턴 \| 날짜·시간 \| 장소]` + 7개 항목 포맷) + 생략 허용 범위를 순수 문학적 수식으로 엄격히 제한 | `core.py` (`build_compression_prompt`) |
| 9 | ~~PC 대사 묘사 금지 규칙~~ ✅ | 섹션 [4. 절대 행동 수칙]에 규칙 추가: 플레이어가 선언하지 않은 PC 대사(`@대사:PC이름\|...`)·내면 독백·감정 반응 창작 절대 금지. `"~라고 말했습니다"`, `"~을 느꼈습니다"` 등 PC 내면 단정 표현도 금지. | `prompts.py` |
| 11 | ~~상태이상 없을 때 명시~~ ✅ | `add_player_block()`에서 `c_stat`이 비어있어도 `"현재 상태이상: 없음"` 항상 출력. 상태 해제 후 AI가 해제된 상태를 언급하는 현상 방지. | `core.py` (`PromptBuilder`) |
| 12 | ~~나이별 호칭·존대 규칙~~ ✅ | `prompts.py` 섹션 [3]에 새 항목 추가: 연상→연하 반말 전환 시점, 연하→연상 존댓말 유지, 첫 만남 해요체 기본값, 조직 내 직책 우선 서열, 호칭 일관성, 극한 상황 말 놓기 등 7개 세부 원칙. `shadow_island.json` `desc_guide`에 시나리오 전용 항목 추가: 생존자 사회 서열 언어, PC 첫 만남 기본값, 세력 내부 위계, 극한 상황 말 놓기. | `prompts.py` 섹션 [3], `scenarios/shadow_island.json` (`desc_guide`) |
| 18 | ~~세계관 설정 일탈 방지~~ ✅ | `prompts.py` 섹션 [4]에 규칙 추가: 정보 신뢰도 우선순위(①룰북 > ②GM지시 > ③압축기억 > ④플레이어 발언) 명시, 룰북 미정의 세력·기술·사건 임의 창작 금지, 세계관 제약(고립·자원 희소성 등) 편의적 완화 금지 등 4개 절대 금지 패턴 명시. | `prompts.py` 섹션 [4] |

---

### ✅ 능력치·NPC·시나리오 구조 개선 (v1.7 추가)

| # | 항목 | 내용 | 파일 |
|---|------|------|------|
| 28 | ~~`ability_stat_max` 능력치 상한~~ ✅ | `_apply_stat_cap()` 헬퍼 추가. Hamilton 배분 후 상한 초과 스탯의 초과분을 잔여 여유 비율로 반복 재배분. 모든 스탯 만캡 시 초과분 소멸. `shadow_island.json`에 `"ability_stat_max": 20` 추가. | `cogs/character.py`, `shadow_island.json` |
| 30 | ~~`prohibitions` 금지사항 신설~~ ✅ | 시나리오 JSON `prohibitions` (list 또는 str) 추가 시 캐시 룰북 `[6. GM 절대 금지 사항]` 섹션으로 자동 렌더링. 없으면 섹션 생략 (기존 JSON 하위 호환). 상태창 섹션 `[7]`로 번호 변경. `shadow_island.json`에 6개 금지 항목 수록. | `core.py`, `shadow_island.json` |
| 31 | ~~NPC 구조 세분화~~ ✅ | `npc_template.info_fields` 기반 구조화 NPC 지원. `build_scenario_cache_text` 필드별 렌더링, `add_npc_override_block` 필드별 diff 비교, `TRPGSession.__init__` 전체 필드 복사 + 기본 resources/statuses 런타임 사전 적용. `!엔피씨 설정`에 단일 필드 수정 모드 + `**필드**: 값` 형식 자동 파싱 추가. `!엔피씨 확인/목록` 구조화 표시. `generate_character_details` npc_template 동적 양식 사용. | `core.py`, `cogs/character.py`, `shadow_island.json` |

---

### 🗺️ 시나리오 콘텐츠 (shadow_island.json)

| # | 항목 | 내용 | 위치 |
|---|------|------|------|
| 2 | 인물 묘사 규칙 명시 | 세력별 행동상·생활상(복장·언어·습관 등)을 `desc_guide`에 추가. 현재 세계관 묘사는 있으나 인물 생활상 가이드 부재 | `desc_guide` |
| 4 | ~~영도 지리 세계관 명시~~ ✅ | `worldview` 1.3 섹션 신설: 4개 거점 위치·고도, 거점 간 도보 이동 거리(40~120분), 산복도로·저지대 상업지구·동삼동 해안로·봉래산 능선·하수도 경로 특성 명세. | `worldview` |
| 16 | ~~거점 내부 지리·공간 구조~~ ✅ | `keyword_memory` 11개 항목 신설: 하역장·검역통제소, 흑색 작업장, 지하 병참, 스카이웨이, 중앙 주차장, 해련사, 방파제, 혁신지구, 바자르, 바리케이드 미로, 해안 비밀통로. 키워드 트리거 시 해당 공간 묘사 컨텍스트 자동 주입. | `keyword_memory` |
| 19 | ~~각종 절차 진행 방식 명시~~ ✅ | `desc_guide` 섹션 7 신설: 세력별 접촉 절차·협상 방식·수색검문·교환 규칙 4개 항목. 남항(노동 기반 현물 협상), 해련(공동체 합의·신뢰), 조도(계급 명령·정보 교환), 중리(신용 시스템·시장 논리). | `desc_guide` |
| 26 | ~~감염자 대사 표기 고정~~ ✅ | `desc_guide` 섹션 8 신설: `@대사:감염자|본문` 마커 의무화, 완전 문장 금지·단어 파편·신음·기억 파편 허용 원칙, 이미지 연동은 별도 `상:/중:/하:` 태그로 처리하도록 명시. | `desc_guide` |

---

### 🖼️ 미디어 / UI

| # | 항목 | 내용 | 파일 |
|---|------|------|------|
| 27 | 지역 이미지 자동삽입 규칙 | 특정 지역 진입 묘사 시 해당 지역 이미지 자동 송출 로직. 현재 GM이 `상:키워드` 태그를 직접 입력해야 함 — 지역명 키워드 감지 기반 자동 트리거로 개선 | `cogs/game.py` (`_execute_proceed`) |

---

### 👤 플레이어 시스템

| # | 항목 | 내용 | 파일 |
|---|------|------|------|
| 21 | 캐릭터 제작 과정 구체화 | `!능력치` 이후 스탯 배분 → `!외형` 설정 → 배경 서사 입력 등 전체 온보딩 플로우 정비. 현재 각 명령어는 존재하나 가이드 체계 미흡 | `cogs/session.py` (`send_intro`) 또는 신규 온보딩 Cog |

---

## 참고 — 현재 상수

```python
DEFAULT_MODEL = "gemini-3-flash-preview"   # 턴 묘사, 캐시, 지시층위, NARRATE
LOGIC_MODEL   = "gemini-3-flash-preview"   # 기억 압축, 설정생성, 서사 계획
IMAGE_MODEL   = "gemini-3.1-flash-image-preview"
TTS_MODEL     = "gemini-2.5-flash-preview-tts"  # 음성 더빙 (API 키 제공 모델과 일치 필요)
EXCHANGE_RATE = 1500.0                     # KRW/USD 고정
```

> `LOGIC_MODEL`에 Pro 모델 주석이 존재함. 비용 계산은 `PRICING_1M` 딕셔너리 기준이므로 모델 변경 시 pricing 키 확인 필수.
