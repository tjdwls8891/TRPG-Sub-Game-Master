# base — 기능 명세

- **모듈**: `core/constants.py` · `core/models.py` · `core/io.py` · `core/utils.py`
- **연관 cogs**: `cogs/errors.py` · `cogs/permissions.py`
- **기준 버전**: v5.29.1
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

---

## 개요

프로젝트의 토대다. 다른 모든 영역이 이 넷에 의존한다.

| 모듈 | 담당 |
|---|---|
| `constants.py` | 모델 ID·단가·환율·안전설정·TTS 상수. **값만 있고 로직이 없다** |
| `models.py` | `TRPGSession` — 세션 하나의 모든 상태를 담는 단일 컨테이너 |
| `io.py` | 세션 직렬화·복원, 로그 기록, 시나리오 로드, 권한 목록, 캐시 파기 정산 |
| `utils.py` | 캐릭터 이름 해석, AI 설정 생성, 한글 자모 분해 |

**경계** — 이 영역은 다른 영역을 거의 호출하지 않는다. 예외는 `io.py`가 `cost.py`를 부르는 것(캐시 보관비 정산)과 `utils.py`가 `prompts.py`를 부르는 것뿐이다.

`utils.py`의 `generate_character_details`는 AI를 직접 호출하므로 **ai 영역에 가깝지만**, 다른 곳에서 재사용되지 않고 `character.py` 한 곳만 쓰므로 여기 둔 것으로 보인다. `[추정]`

---

## 설계 의도

### 세션을 하나의 객체로 묶는다

> *"비동기 환경에서 데이터 파편화를 막기 위해 채널 메타데이터, 플레이어/NPC 상태, 자원, 로그 배열 등을 하나의 캡슐화된 객체로 중앙 통제."* `[코드]`

디스코드 봇은 여러 세션이 동시에 돌아간다. 상태를 모듈별로 흩어 두면 어느 세션의 것인지 추적이 어렵다.

### 필드 레지스트리로 저장·복구를 일원화

> *"save_session_data / restore_sessions_from_disk 양쪽의 단일 진실 공급원. 새 TRPGSession 필드를 추가할 때 이곳에만 등록하면 저장·복구가 자동 반영된다."* `[코드]`

`SESSION_FIELDS`에 등록하면 `getattr` 루프가 알아서 처리한다. 저장 코드와 복구 코드에 필드명을 두 번 적지 않아도 된다.

**규칙이 명시돼 있다.** `[코드]`
- 값이 반드시 존재하는 핵심 필드(`session_id`, `players`)는 넣지 않는다 → 명시 직렬화
- 런타임 전용 필드(`gm_lock`, `is_processing`)는 넣지 않는다
- 재시작 시 초기화할 필드는 `SESSION_RESET_FIELDS`에도 넣는다

### 원자적 쓰기 — 동시 저장 경합 방지

> *"tmp 파일명을 호출별로 고유화한다. 동시 저장(예: 백그라운드 기억 압축과 프로씨드가 각각 save_session_data 호출)이 같은 tmp를 덮어써 data.json이 손상되는 경합을 방지한다."* `[코드]`

tmp 파일명에 **PID와 나노초**를 넣는다. `os.replace`는 원자적이므로 최종 파일은 항상 완전한 스냅샷이다.

도입 커밋: `dc738b6` — 기억 압축 타이밍 변경 때 함께 들어갔다.

### 스키마 마이그레이션

5.0.0에서 `auto_gm_*` 14필드를 `gm_*`로 이관하며 `SCHEMA_VERSION`을 2→3으로 올렸다. `[코드]` (`0d7fc40`)

`migrate_session_data`가 로드 시 자동 변환한다. 값이 이미 새 이름으로 존재하면 덮어쓰지 않는다(부분 마이그레이션 대비).

### 단가표를 하드코딩하는 이유

> *"세션별 누적 과금액을 정밀하게 추적하기 위한 기준 데이터."* `[코드]`

Gemini API는 응답에 비용을 돌려주지 않는다. 토큰 수만 준다. 따라서 단가를 코드가 알아야 한다.

---

## 데이터 구조

### `TRPGSession` — 생성자 인자

| 인자 | 타입 | 용도 |
|---|---|---|
| `session_id` | str | UUID 기반 고유 식별자. 로그 디렉터리명이 된다 |
| `game_ch_id` | int | 게임 채널 |
| `master_ch_id` | int | 마스터 채널 |
| `scenario_id` | str | 시나리오 파일명(확장자 제외). **미디어 폴더명과 같아야 한다** |
| `scenario_data` | dict | 시나리오 JSON 원본 |

### 세션 필드 — 저장 대상 (`SESSION_FIELDS` 76개)

명시 직렬화 15키와 별개로, 이 레지스트리가 나머지를 일괄 처리한다.

**핵심 상태**

| 필드 | 기본 | 용도 |
|---|---|---|
| `is_started` | False | 세션이 열렸는가. **False면 턴 진행이 차단된다** |
| `turn_count` | (명시) | 진행 턴 수 |
| `note` | "" | GM 메모 |

**캐시**

| 필드 | 기본 | 용도 |
|---|---|---|
| `cache_name` | (명시) | Gemini 캐시 리소스명. None이면 닫힌 세션 |
| `cache_created_at` | 0.0 | 생성 시각(epoch). 만료·보관비 산출 기준 |
| `cache_tokens` | 0 | 조립 텍스트 기준 토큰 수 |
| `cache_read_tokens` | 0 | **실제 관측된 읽기 토큰.** 캐시에 구워진 지시문까지 포함하므로 예측은 이 값을 우선 사용 `[코드]` |
| `cache_model` | DEFAULT_MODEL | 캐시 생성에 쓴 모델. 모델이 바뀌면 캐시를 못 쓴다 |
| `cache_text` | "" | 룰북 원본(패딩 제외). `!캐시 출력` 디버그용 |
| `cache_note` | "" | 캐시 메모 |
| `cached_session_npcs` | {} | 재발급 시점 NPC 스냅샷. 이후 변경분만 delta 주입 |
| `cached_compressed_memory` | "" | 재발급 시점 압축 기억. 중복 주입 방지 |
| `cached_worldview_sections` | [] | 캐시에 편입된 keyword_memory 섹션 id |
| `cache_expired_notified` | False | 만료 알림 중복 방지 |

**비용**

| 필드 | 기본 | 용도 |
|---|---|---|
| `total_cost` | 0.0 | 누적 원화 |
| `total_usd` | 0.0 | 누적 달러. **청구 근거** |
| `total_ink_spent` | 0 | 실제 차감한 잉크 누적 |
| `last_turn_cost` | 0.0 | 직전 턴 원화 |
| `open_prepaid_ink` | 0 | 세션 오픈 선결제. 클로즈 환급 기준 |
| `open_minutes` | 0 | 선택한 유지 시간 |
| `interpret_cost_krw` | 0.0 | 시간 해석 호출 누적. 2잉크 이상일 때만 청구 |
| `profile_ai_cost_krw` | 0.0 | 프로필 AI 누적. **무료 제공이라 차감하지 않고 파악용** |
| `compression_prepaid_krw` | 0.0 | 압축 선결제 누적 |
| `cost_stats` | {} | 층위별 출력 토큰 이동평균 |
| `last_estimate` | {} | 직전 예상치 |

**GM 상태 (11개)**

`gm_active`·`gm_target_char`·`gm_target_chars`·`gm_turn_cap`·`gm_turns_done`·`gm_clarify_count`·`gm_narrate_count`·`gm_cost_cap_krw`·`gm_cost_baseline`·`gm_side_note`·`gm_proceed_history`

이 중 셋은 **저장하되 재시작 시 초기화**된다(`SESSION_RESET_FIELDS`) — `gm_pending_players`·`gm_collected_actions`·`gm_waiting_for`. 라운드 수집 중이던 상태를 재시작 후 이어받으면 어긋나기 때문으로 보인다. `[추정]`

**세계·퀘스트**

`world_timeline`·`quest_state`·`player_faction`·`companions`·`met_npcs`·`visited_places`·`start_frame`·`start_day_number`·`narrative_plan`·`narrative_mode`·`infinity_plan`·`pending_ending`·`main_unlocked_notified`·`irregular_npcs`·`info_ledger`·`stat_fail_counts`

**미디어·UI**

`tts_enabled`·`tts_voice`·`volume`·`media_flags`·`pending_bgm`·`last_bgm_situation`·`display_ch_id`·`display_msg_id`·`awaiting_display_input`·`voice_ch_id`·`is_private`·`session_kind`

**기억·되감기**

`compressed_memory`(명시)·`last_compressed_turn`·`compression_count`·`memory_plan`·`last_recorded_turn`·`extraction_pending`·`extraction_retry_ctx`·`last_extraction`·`last_turn_anchor_id`

**세션 생성**

`creation_state`·`creator_uid`·`started_at`

### 런타임 전용 (저장 안 함)

| 필드 | 이유 |
|---|---|
| `cache_obj` | Gemini 객체. 직렬화 불가 |
| `voice_client` | 디스코드 객체 |
| `gm_typing_task` | asyncio 태스크 |
| `gm_lock` | 동시 처리 방지 락 |
| `is_processing` · `is_compressing` | 진행 중 플래그. 재시작하면 진행 중이 아니다 |
| `current_bgm` · `is_bgm_looping` | 재생 상태 |
| `_rewind_snapshot` | 되감기 기준 스냅샷 |
| `_last_input_estimate` | 예측 대조용 |

### 되감기 추적 대상 (`TRACKED_PATHS` 14개)

`resources`·`statuses`·`world_timeline`·`info_ledger`·`quest_state`·`visited_places`·`companions`·`met_npcs`·`players`·`total_cost`·`gm_turns_done`·`compressed_memory`·`last_extraction`·`narrative_plan`

> `[확인 필요]` `total_ink_spent`·`total_usd`가 추적 대상이 아니다. 되감아도 실제 결제는 되돌아가지 않으므로 의도적일 수 있으나, `total_cost`는 추적하면서 이 둘은 하지 않아 표기가 어긋날 수 있다.

### 파일

| 경로 | 형식 | 내용 |
|---|---|---|
| `authorized_users.json` | `{owner_id, granted[]}` | 명령어 권한 allowlist |
| `sessions/{id}/data.json` | 세션 스냅샷 | 원자적 쓰기 |
| `sessions/{id}/{type}_log.txt` | 텍스트 | `api`·`game_chat`·`master_chat`·`cost`·`error` |
| `scenarios/{name}.json` | 시나리오 | `.quests.json`은 부속 파일 |
| `data/common_status_effects.json` | 배열 | 공통 상태이상 |

---

## 기능 목록

### `constants.py`

값만 정의한다. 함수가 없다.

**`__version__`** — 표기의 단일 출처. `[코드]`
문서(CLAUDE.md·DEVLOG.md)의 버전을 이 값과 일치시킨다. `tools/verify_docs.py`가 검증한다.

**모델 ID 4종**

| 상수 | 값 | 용도 |
|---|---|---|
| `DEFAULT_MODEL` | gemini-3-flash-preview | 묘사층위 |
| `LOGIC_MODEL` | 같음 | 지시층위. 주석에 `gemini-3-pro-preview` 대안이 남아 있다 |
| `PROFILE_AI_MODEL` | 같음 | 프로필 검증·병합. **"저비용 모델이 확정되면 이 상수만 교체"** `[코드]` |
| `IMAGE_MODEL` | gemini-3.1-flash-image-preview | 이미지 |
| `TTS_MODEL` | gemini-2.5-flash-preview-tts | 음성 |

**`MIN_CACHE_TOKENS`** = 1024 (env로 재정의 가능)

> *"과거 코드는 32,768로 잡혀 있었으나 이는 현행 기준의 32배로, 미달분을 마침표 패딩으로 채우는 만큼 캐시 생성비·시간당 저장비·매 턴 읽기비가 모두 부풀었다."* `[코드]` (`c03819c`)

Flash는 1,024, Pro는 4,096이 최소 요건이다. 운영 중 실패하면 `.env`로 되돌릴 수 있다.

**`CACHE_TTL_SECONDS`** = 21600 (6시간)

> ⚠️ **이름과 실체 불일치** — `cache.py`가 이 값을 `MIN_CACHE_TTL`이라는 별칭으로 임포트한다. '최소'가 아니라 기본 전체값이다.

**`EXCHANGE_RATE`** = 1500.0 — 고정 환율.

**잉크 상수**

| 상수 | 값 | 주석의 설명 |
|---|---|---|
| `INK_UNIT_KRW` | 10 | 판매가 1잉크 = 10원 |
| `INK_NET_KRW` | 7 | *"디스코드 결제 수수료 30% 차감 후 실수령"* |
| `INK_PLANS` | [100,300,500,1000,3000,5000] | 충전 플랜 |

`cost_to_ink`가 실제로 `ceil(비용 / 7)`을 적용함을 확인했다. `[코드]`
판매가(10원)와 실수령(7원)이 다르므로, 비용 충당 기준은 실수령이어야 한다.
플레이어는 10원에 사고 운영자는 7원을 받으므로, 7로 나눠야 API 비용을 메운다.

**`TRPG_SAFETY_SETTINGS`** — 4개 카테고리 전부 `BLOCK_NONE`.
TRPG 특성상 폭력·공포 묘사가 필요하기 때문으로 보인다. `[추정]`

**`PRICING_1M`** — 5개 모델의 100만 토큰당 달러 단가.
2026년 검색 기준 실가와 일치함을 확인했다(Gemini 3 Flash $0.50/$3.00).

**TTS 상수** — `TTS_NARRATOR_VOICE`(Algenib), `TTS_VOICES`(30종), `TTS_LANGUAGE_CODE`(ko-KR), `TTS_NARRATION_VOLUME`(0.5), `TTS_PCM_BYTES_PER_SEC`(48000×2×2), `TTS_STYLE_PROMPT`

`TTS_STYLE_PROMPT`는 공식 "강력 프롬프트" 구조(AUDIO PROFILE / DIRECTOR'S NOTES)를 차용했다. `[코드]`
코드가 이 뒤에 `#### TRANSCRIPT`로 본문을 붙인다.

**`IMAGE_OUTPUT_TOKENS_BY_RES`** — 해상도별 출력 토큰 폴백값.
`usage_metadata`가 비었을 때 쓴다. `[코드]`

---

### `models.py`

#### `TRPGSession.__init__(session_id, game_ch_id, master_ch_id, scenario_id, scenario_data)`

**무엇을 하는가** — 필드 90여 개를 초기화하고, 시나리오의 `default_npcs`를 런타임 `npcs`·`resources`·`statuses`로 전개한다.

**왜 있는가** — 세션 상태를 한 객체에 모으기 위함. `[코드]`

**NPC 전개 로직** (218~245행)

```
default_npcs의 각 항목에 대해
  dict면 → resources·statuses를 제외한 전 항목을 복사
           name이 없으면 키를 name으로
           resources 기본값 → session.resources[이름]에 병합
           statuses 기본값 → session.statuses[이름]에 추가 (중복 제외)
  문자열이면 → {"name": 키, "details": 값}
```

`resources`·`statuses`를 `npc_entry`에서 빼는 이유는 **런타임 딕셔너리로 옮기기 때문**이다. 태그·`!증감`이 그 값을 기준으로 증감한다. `[코드]`

`_npc_info_fields`를 220행에서 읽지만 **이 함수 안에서 쓰이지 않는다.**

> ⚠️ **미사용 지역변수** — `_npc_info_fields`가 계산만 되고 쓰이지 않는다. `[확인 필요]`

**호출부**
- `cogs/session.py` — `provision_session`
- `core/cache.py` — 세션 복구
- `core/profile_ui.py` — `_StandaloneSession`은 이 클래스를 쓰지 않고 별도 대역 객체를 만든다

**주의점**

> ⚠️ **`self.npcs = {}` 중복** — 30행과 217행에서 두 번 초기화된다. 30행 것은 217행에 덮어써지므로 무해하나, 30행과 217행 사이에서 `npcs`를 참조하는 코드가 생기면 혼란스럽다.

> ⚠️ **거대 생성자** — 225줄. 필드 정의가 `io.SESSION_FIELDS`와 이중으로 관리된다. 한쪽에만 추가하면 저장이 안 되거나 `getattr` 기본값으로 조용히 넘어간다.

---

### `io.py`

#### `load_authorized_users() -> dict`

**무엇을 하는가** — `authorized_users.json`에서 `{owner_id, granted[]}`를 읽는다. 실패 시 빈 값.

**왜 있는가** — 봇 전역 allowlist. *"오너와 오너가 권한을 부여한 계정만 마스터 명령을 사용할 수 있다."* `[코드]`

**호출부** — `main.py:51` (기동 시 1회)

**실패** — 파일 없음·JSON 오류·타입 오류를 모두 흡수하고 빈 값을 반환한다. 권한 파일이 깨져도 봇은 뜬다(오너만 쓸 수 있게 됨).

---

#### `save_authorized_users(data)`

**무엇을 하는가** — 원자적으로 저장(`.tmp` → `os.replace`). `granted`는 정렬·중복 제거한다.

**호출부** — `cogs/permissions.py:65,76` (부여·회수), `main.py:109` (오너 자동 등록)

**실패** — 예외를 흡수하고 경고만 출력. 저장 실패 시 메모리 상태와 파일이 어긋난다. `[추정]`

---

#### `migrate_session_data(data) -> dict`

**무엇을 하는가** — 저장 데이터를 현행 스키마로 변환한다. `FIELD_MIGRATIONS`에 정의된 구→신 필드명 매핑을 적용한다.

**왜 있는가** — 5.0.0에서 `auto_gm_*` 14필드를 `gm_*`로 바꿨다. 구세션 파일을 그대로 읽으면 GM 설정이 전부 초기화된다. `[코드]`

**동작**
```
schema_version >= 3 이면 그대로 반환
2 이하면 매핑 적용
  새 이름이 이미 있으면 덮어쓰지 않음 (부분 마이그레이션 대비)
  구 필드는 pop
변환 후 schema_version을 3으로 갱신 → 다음 로드부터 건너뜀
```

**호출부** — `core/cache.py:432` (세션 복구 시)

**주의점** — 치환 순서에 함정이 있다. `auto_gm_target_char`가 `auto_gm_target_chars`의 접두다. dict 순회이므로 이 구현은 안전하나, 문자열 치환으로 바꾸면 깨진다. `[추정]`

---

#### `_serialize_log_entry(content) -> dict | None`

**무엇을 하는가** — Gemini `types.Content`를 `{"role", "text"}`로 변환한다.

**왜 있는가** — `raw_logs`에 Gemini 객체가 그대로 들어 있어 JSON 직렬화가 안 된다. `[추정]`

**동작** — 여러 text 파트는 줄바꿈으로 연결. 이미지·함수 호출 등 text 없는 파트는 조용히 건너뛴다. 변환 불가면 `None`(저장 시 필터링).

**호출부** — `core/io.py:341` (내부 전용)

---

#### `load_scenario_from_file(scenario_id) -> dict | None`

**무엇을 하는가** — `scenarios/{id}.json`을 파싱한다.

**왜 있는가** — 시나리오 데이터 진입점.

**방어** — `.quests`로 끝나는 id를 거부한다.
> *"목록에서 걸러도 id가 직접 지정되면 통과하므로 여기서도 막는다."* `[코드]`

이는 5.15.1에서 `영도.quests`가 시나리오 선택지에 노출된 사고의 수정이다.

**호출부** — `session_flow.py:211,459` · `profile_ui.py:342` 등 5곳

---

#### `write_log(session_id, log_type, content)`

**무엇을 하는가** — `sessions/{id}/{type}_log.txt`에 타임스탬프와 함께 append.

**동작** — `log_type == "api"`면 60자 구분선을 덧붙인다.

**호출부** — **28곳** (`dialogue`·`cache`·`utils`·`gm`·`game` 등)

**실패** — `session_id`가 비면 조용히 반환. 파일 쓰기 실패는 **예외가 전파된다**(try 없음).

> ⚠️ **예외 미처리** — 디렉터리가 없거나 권한이 없으면 호출부로 전파된다. 28곳이 각자 감싸야 한다. `[확인 필요]`

---

#### `write_cost_log(session_id, usage_context, in_tokens, cached_tokens, out_tokens, cost, total_cost)`

**무엇을 하는가** — 비용 전용 로거. 토큰 내역과 누적을 한 줄로 기록한다.

**호출부** — **22곳**

**주의점** — 원화만 기록한다. 5.29.0에서 청구 근거를 달러로 바꿨으나 이 로그는 그대로다.
> `[확인 필요]` `cost_log.txt`에도 달러를 남길 필요가 있습니까?

---

#### `get_available_scenarios() -> list`

**무엇을 하는가** — 플레이 가능한 시나리오 목록.

**제외 규칙** `[코드]`
- `SCENARIO_SIDECAR_SUFFIXES` = `(".quests.json",)` — 부속 파일
- `SCENARIO_EXCLUDED_STEMS` = `{"scenario.example"}` — 예제

> *"확장자 제거도 replace가 아니라 접미사 절삭으로 처리한다 — replace는 파일명 중간의 '.json'까지 지운다."* `[코드]`

**호출부** — `session_flow.py:206` · `gm_space.py:359` · `cogs/session.py:412` 등 4곳

---

#### `save_session_data(bot, session)` (async)

**무엇을 하는가** — 세션을 `data.json`에 직렬화한다.

**왜 있는가** — 봇 재시작·크래시에도 세션이 살아남아야 한다.

**동작 순서**
```
① 세션별 락 획득 (bot.session_io_locks)
② raw_logs를 _serialize_log_entry로 변환 (None은 제외)
③ 명시 필드 15개를 dict에 담음
④ SESSION_FIELDS 76개를 getattr로 일괄 추가
⑤ tmp 파일(PID+나노초)에 쓰고 os.replace
```

**호출부** — **77곳.** 이 프로젝트에서 가장 많이 불리는 함수다.

**실패** — 전부 흡수한다. *"저장 실패가 게임 진행을 중단시키면 안 되므로."* `[코드]`
실패 시 남은 tmp 파일을 정리 시도한다.

**의존**
- 읽기: 세션 전 필드
- 쓰기: `sessions/{id}/data.json`
- 필요: `bot.session_io_locks` (main.py가 만든다)

> 🔁 **호출 중복** — 77곳이 각자 부른다. 상당수가 "무언가를 바꿨으니 저장"이라는 같은 패턴이다.

---

#### `process_cache_deletion(bot, session) -> float` (async)

**무엇을 하는가** — 캐시 파기 시 보관비를 정산하고 캐시 메타데이터를 초기화한다.

**동작**
```
cache_name이 있고 cache_created_at > 0 이면
  경과 시간 계산 → 21600초로 상한
  calculate_storage_cost로 보관비 산출
  accrue로 누적
cache_name·cache_obj·cache_model·cache_created_at·cache_tokens 초기화
저장 후 보관비 반환
```

**호출부** — `cogs/system.py:216,220,264` 등 6곳

**주의점**

> ⚠️ **하드코딩 21600** — `CACHE_TTL_SECONDS`를 쓰지 않고 숫자를 직접 적었다. 주석에 *"설정된 최대 캐시 유지 시간(6시간 = 21600초)"*이라 되어 있으나, `open_minutes`로 3시간을 골랐다면 상한은 10800이어야 한다. **3시간 세션인데 6시간 가까이 켜뒀다면 실제보다 많이 청구될 수 있다.** `[확인 필요]`

> ⚠️ **폴백 32768** — `getattr(session, "cache_tokens", 32768)`. `MIN_CACHE_TOKENS`가 1024로 바뀌기 전의 값이 남아 있다. `cache_tokens`가 없으면 32배 과다 청구된다. `[확인 필요]`

---

### `utils.py`

#### `get_merged_status_effects(scenario_data) -> dict`

**무엇을 하는가** — `data/common_status_effects.json`과 시나리오의 `status_effects`를 병합한다. 같은 이름이면 시나리오가 이긴다.

**왜 있는가** — 부상·출혈 같은 보편 상태를 시나리오마다 다시 쓰지 않게 한다. `[추정]`

**호출부** — `cache.py:220` (캐시 조립) · `game.py:362` · `gm.py:348`

**주의점**

> ⚠️ **추출층위 목록과 불일치 가능** — `extraction.build_extraction_limits`는 `scenario_data["status_effects"]`만 읽고 **공통 목록을 병합하지 않는다.** 공통 상태이상이 추출층위에 전달되지 않아, 모델이 그것을 쓰면 코드가 걸러낸다. `[확인 필요]`

**실패** — 공통 파일 로드 실패 시 경고 후 시나리오 것만 쓴다.

---

#### `get_uid_by_char_name(session, char_name) -> str | None`

**무엇을 하는가** — 캐릭터 이름 → 디스코드 uid.

**호출부** — `growth.py:71,87` · `utils.py:125` 등 11곳

**주의점** — `p_data["name"]`을 직접 인덱싱한다. `name` 키가 없으면 `KeyError`가 난다.

---

#### `resolve_char_name(session, partial, include_npc=False) -> (str|None, list)`

**무엇을 하는가** — 부분 입력으로 이름을 해석한다.

**우선순위** `[코드]`
```
① 정확 일치 (중복 이름이 있어도 정확 입력이면 그대로)
② 고유 접두 일치 ('제' → '제이크')
③ 고유 부분 일치
```

**반환** — `(이름, [이름])` 성공 / `(None, 후보들)` 모호 / `(None, [])` 미발견

**호출부** — `utils.py:117` (`resolve_pc`) · `character.py:517`

---

#### `resolve_pc(session, partial) -> (uid, name, error_msg)`

**무엇을 하는가** — PC 이름을 해석하고 **오류 메시지까지** 만들어 돌려준다.

**왜 있는가** — *"명령어에서 한 줄로 처리할 수 있도록 메시지까지 함께 돌려준다."* `[코드]`

**호출부** — `game.py:140` · `character.py:469,667` 등 4곳

---

#### `generate_character_details(bot, scenario_data, char_type, char_name, instruction, session_id, recent_logs="", npc_context="")` (async)

**무엇을 하는가** — AI로 PC 외모 또는 NPC 종합 프로파일 초안을 만든다.

**왜 있는가**
- PC는 **외모 묘사만** 만든다. 성격·배경은 생성하지 않는다. `[코드]`
  결과가 `session.players[uid]['appearance']`에 저장되어 프롬프트에 주입된다.
- NPC는 외모부터 내면·관계망·비밀까지 포괄한다. AI GM이 인물을 연기하는 데 필요하기 때문. `[코드]`

**양식 결정**
```
npc_template.info_fields가 있으면 그것을 양식으로
없으면 기본 12항목
has_stats / has_resources / has_statuses 플래그에 따라 섹션 추가
```

**호출부** — `cogs/character.py:1187` (`!설정생성`) **한 곳뿐**

**주의점**

> ⚠️ **비용 미기록** — 이 함수는 응답만 반환하고 토큰·비용을 처리하지 않는다. 호출부(`character.py:1187` 이후)가 처리한다. 다른 곳에서 부르면 비용이 누락된다.

> 🔁 **AI 호출 절차 반복** — `generate_content` 호출·설정 조립을 직접 한다. 다른 17곳도 같은 일을 각자 한다.

---

#### `decompose_hangul(s) -> str`

**무엇을 하는가** — 한글 음절을 초성·중성·종성 자모열로 분해한다. 비한글은 그대로.

**왜 있는가** — 명령어 오타 근접 매칭. '진행'과 '지행'은 음절로는 다르지만 자모로는 가깝다. `[추정]`

**호출부** — `utils.py:274,279` (`suggest_commands` 내부)

---

#### `suggest_commands(typo, names, n=3, cutoff=0.6) -> list`

**무엇을 하는가** — 오타와 명령어 목록을 자모 분해 후 `difflib` 유사도로 비교해 근접 명령을 반환한다.

**호출부** — `cogs/errors.py:108` (`CommandNotFound` 처리)

---

## 흐름

### 세션 생명주기에서 base의 역할

```
① 봇 기동
   main.py → load_authorized_users()
           → cache.restore_sessions_from_disk()
                → migrate_session_data()   구스키마 변환
                → TRPGSession(...)          객체 복원
                → SESSION_FIELDS로 필드 복구

② 세션 생성
   session.py → TRPGSession(...)  →  NPC 전개

③ 턴 진행 (반복)
   각 층위 → write_log("api", ...)
          → write_cost_log(...)
          → save_session_data()

④ 세션 종료
   system.py → process_cache_deletion()  보관비 정산
             → save_session_data()
```

### 저장·복구 대칭

```
save_session_data                 restore_sessions_from_disk (cache.py)
  명시 15키                    ↔    명시 15키
  SESSION_FIELDS 76개 getattr  ↔    SESSION_FIELDS 76개 setattr
                                    SESSION_RESET_FIELDS 3개는 초기값으로
```

**이 대칭이 깨지면 조용히 데이터가 사라진다.** `SESSION_FIELDS`에 등록하지 않으면 저장도 복구도 안 되는데 오류가 나지 않는다.

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **cost** | base → cost | `io.process_cache_deletion`이 `calculate_storage_cost`·`accrue` 호출 |
| **memory** | memory → base | `cache.py`가 `migrate_session_data`·`TRPGSession` 사용 |
| **전 영역** | → base | `save_session_data` 77곳, `write_log` 28곳 |
| **ai** | base → prompts | `utils.generate_character_details`가 `build_*_prompt` 사용 |
| **profile** | profile → base | `resolve_pc`·`get_uid_by_char_name` |
| **world** | world → base | `growth.py`가 `get_uid_by_char_name` |

---

## 발견 사항

> ⚠️ **`process_cache_deletion`의 21600 하드코딩**
> `CACHE_TTL_SECONDS`를 쓰지 않는다. `open_minutes`가 3시간이어도 6시간까지 청구될 수 있다.

> ⚠️ **`process_cache_deletion`의 32768 폴백**
> `MIN_CACHE_TOKENS`가 1024로 바뀌기 전 값. `cache_tokens`가 없으면 32배 과다 청구.

> ⚠️ **`self.npcs = {}` 중복 초기화**
> `models.py` 30행과 217행. 무해하나 혼란스럽다.

> ⚠️ **`_npc_info_fields` 미사용**
> `models.py:220`에서 계산만 하고 쓰지 않는다.

> ⚠️ **`write_log` 예외 미처리**
> 파일 쓰기 실패가 28개 호출부로 전파된다.

> ⚠️ **공통 상태이상이 추출층위에 전달되지 않음**
> `get_merged_status_effects`는 병합하는데 `build_extraction_limits`는 시나리오 것만 읽는다.

> ⚠️ **`total_ink_spent`·`total_usd`가 되감기 비추적**
> `total_cost`는 추적하면서 이 둘은 하지 않아 표기가 어긋날 수 있다.

> 🔁 **`save_session_data` 77회 호출**
> 대부분 "바꿨으니 저장" 패턴.

> 🔁 **필드 정의 이중 관리**
> `models.__init__`(225줄)과 `io.SESSION_FIELDS`(76개)에 같은 필드를 두 번 적는다.

> 🔁 **AI 호출 절차 반복**
> `generate_character_details`가 호출·설정 조립을 직접 한다.

> ⚠️ **`write_cost_log`가 원화만 기록**
> 5.29.0에서 청구 근거를 달러로 바꿨으나 로그는 그대로.

---

## 확인 필요 목록

- [ ] **`process_cache_deletion`의 21600 상한** — `open_minutes`를 반영해야 합니까, 6시간 고정이 의도입니까?
- [ ] **`cache_tokens` 폴백 32768** — 1024로 바꿔야 합니까? 아니면 이 폴백이 도달하지 않는 경로입니까?
- [ ] **`_npc_info_fields`** — `models.py:220`에서 쓰려던 것이 있었습니까? 지워도 됩니까?
- [ ] **`self.npcs` 중복 초기화** — 30행 것을 지워도 됩니까?
- [ ] **`SESSION_RESET_FIELDS` 3개** — 재시작 시 초기화하는 이유가 라운드 수집 상태의 정합성 때문이 맞습니까?
- [ ] **`total_ink_spent`·`total_usd` 되감기** — 추적 대상에서 뺀 것이 의도입니까?
- [ ] **공통 상태이상 주입** — `build_extraction_limits`가 `get_merged_status_effects`를 써야 합니까?
- [ ] **`write_cost_log`에 달러** — 추가할 필요가 있습니까?
- [ ] **`PROFILE_AI_MODEL`** — *"저비용 모델이 확정되면"*이라 되어 있는데, 후보가 있습니까?
- [ ] **`LOGIC_MODEL` 주석의 pro 대안** — `gemini-3-pro-preview`로 올릴 계획이 있습니까?
- [ ] **`generate_character_details`의 위치** — AI 호출이므로 ai 영역이 맞습니까, base가 맞습니까?
