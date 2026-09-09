# memory — 기능 명세

- **모듈**: `core/memory_plan.py`(202) · `rewind.py`(340) · `cache.py`(531)
- **연관 cogs**: `cogs/game.py`(압축 실행) · `cogs/presence.py`(만료 감지)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

---

## 개요

세션이 무엇을 기억하고 무엇을 잊는가를 다룬다.

| 모듈 | 담당 |
|---|---|
| `memory_plan.py` | 압축 플랜 4종 — 주기·모델·구조 |
| `rewind.py` | 되감기 델타 로그 — append-only 기록·역순 복원 |
| `cache.py` | Gemini 캐시 — 룰북 조립·수명·세션 복구 |

**세 가지 기억이 있다.**
```
캐시        룰북. 세션 내내 고정. Gemini 서버에 상주
압축 기억    지난 턴의 요약. 주기적으로 갱신
raw_logs    최근 대화 원본. 20개 캡
```

---

## 설계 의도

### 델타만 기록한다

> *"SESSION_FIELDS는 최신 상태만 보관하므로 과거 시점을 재구성할 수 없다. 턴별 전체 스냅샷은 용량이 턴 수에 비례해 커지므로 델타만 기록한다."* `[코드]`

> *"세션 JSON과 분리해 평상시 로드 비용에 영향을 주지 않는다."* `[코드]`

### 압축 주기와 되감기의 관계

> *"압축 시점은 대상 턴 종료 후 다음 턴 시작 시점이며, 턴 되감기 시 이전 턴을 재압축하지 않도록 주의한다."* `[코드]` (기획 규정)

`last_compressed_turn`이 기준이다. **되감기로 턴이 줄면 기준도 함께 내린다.**

### 압축 중 재발동 방지

> *"압축은 백그라운드로 돌아 완료까지 수 턴이 걸릴 수 있다. 진행 중에 재발동하면 같은 구간이 중복 압축된다."* `[코드]`

`is_compressing` 플래그로 막는다.

### 캐시 최소 토큰

> *"과거 코드는 32,768로 잡혀 있었으나 이는 현행 기준의 32배로, 미달분을 마침표 패딩으로 채우는 만큼 캐시 생성비·시간당 저장비·매 턴 읽기비가 모두 부풀었다."* `[코드]` (4.1.0)

### 만료를 미리 안다

`cache_name`만 보면 API 호출이 실패해야 알아차린다. `is_cache_expired`가 시간으로 판정한다. `[코드]` (5.24.5)

---

## 데이터 구조

### 압축 플랜 (`PLANS`)

| 키 | 주기 | 모델 | 모드 | 비용 |
|---|---|---|---|---|
| `normal` | 5턴 | LOGIC | fixed | 기본 |
| `high` | 3턴 | LOGIC | adaptive | 높음 |
| `low` | 5턴 | LOGIC → **late_model** | fixed | 낮음(후반 절감) |
| `ultra` | 1턴 | LOGIC | adaptive | 매우 높음 |

`LOW_SWITCH_AFTER = 3` — 로우는 3회 압축 후 저비용 모델로 전환.

> ⚠️ **`late_model`이 `DEFAULT_MODEL`인데 `LOGIC_MODEL`과 같은 값이다.** 전환해도 비용이 같다. '발견 사항' 참조.

### 되감기 파일

```
sessions/{id}/rewind_log.jsonl      턴별 델타 (append-only)
sessions/{id}/rewind_archive.jsonl  되감기로 제거된 정보 보존
sessions/{id}/full_logs.jsonl       전 턴 대화 (raw_logs 20개 캡 우회)
```

`REWIND_MAX_TURNS = 20` — *"저장 용량과 복원 시간의 상한"* `[코드]`

### `TRACKED_PATHS` (14개)

```
resources · statuses · world_timeline · info_ledger · quest_state
visited_places · companions · met_npcs · players
total_cost · gm_turns_done · compressed_memory · last_extraction · narrative_plan
```

**주석에 중요한 설명이 있다.** `[코드]`
> *"플레이어 스탯은 `players[uid]["profile"]`에 들어 있다. `session.ability_stats`라는 속성은 존재하지 않으며, `scenario_data["ability_stats"]`는 능력치 '이름 목록'일 뿐이다. 능력치 성장·프로필 수정을 되감으려면 `players` 자체를 추적해야 한다."*

> `[확인 필요]` `total_ink_spent`·`total_usd`가 빠져 있다. `total_cost`는 추적하면서 이 둘은 하지 않아 되감기 후 표기가 어긋난다. (base 영역에서도 지적)

### 캐시 세션 필드

`base` 영역 명세의 '캐시' 표 참조. 12개 필드가 있다.

### 상수

| 상수 | 값 | 용도 |
|---|---|---|
| `LOW_SWITCH_AFTER` | 3 | 로우 플랜 전환 시점 |
| `REWIND_MAX_TURNS` | 20 | 되감기 상한 |
| `MIN_CACHE_TOKENS` | 1024 | 캐시 최소. env로 재정의 가능 |
| `MIN_TTL_SECONDS` | 600 | 캐시 최소 수명 |
| `CACHE_TTL_SECONDS` | 21600 | 기본 6시간. `MIN_CACHE_TTL` 별칭으로도 쓰임 |

---

## 기능 목록

### `memory_plan.py`

#### `get_plan(session) -> dict`

세션의 플랜. 미지정이면 노멀.
**호출부** — 내부 4곳

#### `plan_key(session) -> str`

> ⚠️ **호출부 없음.** `get_plan`과 첫 두 줄이 같고 반환만 다르다.

#### `interval(session) -> int`

압축 주기.
> *"되감기 롤백은 주기를 몰라도 동작하지만, 압축 발동 판정에는 필요하다."* `[코드]`

#### `should_compress(session) -> bool`

**판정 순서**
```
turn <= 0                    → False
is_compressing               → False (중복 방지)
last_compressed_turn > turn  → 기준을 내리고 False (되감기 대응)
(turn - last) >= interval    → True
```

**호출부** — `game.py:287`

#### `mark_compressed(session)`

`last_compressed_turn` 갱신 + `compression_count` 증가.
**호출부** — `game.py:831,1360`

#### `select_model(session) -> str`

로우 플랜은 `aggressive_after` 이후 `late_model`을 쓴다.
**호출부** — `game.py:778`

#### `is_aggressive` / `is_adaptive`

크게 줄이는 단계인지 · 상황별 구조를 쓰는지.
**호출부** — `prompt.py:333` · `memory_plan.py:142`

#### `build_context_hint(session) -> str`

플랜별 압축 지시 힌트. `build_compression_prompt`가 쓴다.
**호출부** — `prompt.py:332`

#### `format_plans() -> str` / `compare_curves(turns=30) -> dict`

플랜 목록 표기 · 누적 압축 횟수 비교.
**호출부** — `gm.py:3272` · `session_flow.py:171`

#### `cost_curve(session, turns=30) -> list`

> ⚠️ **호출부 없음.** `compare_curves`가 내부에서 같은 계산을 다시 한다. `[추정]`

---

### `rewind.py`

#### `read_jsonl(session_id, filename) -> list`

JSONL 파일을 읽는다. 깨진 줄은 건너뛴다. `[추정]`
**호출부** — 내부 4곳

#### `capture_state(session) -> dict`

`TRACKED_PATHS` 필드의 현재 값을 깊은 복사로 담는다.
**호출부** — `gm.py:1389,1425`

#### `diff_state(before, after) -> list`

두 스냅샷의 차이를 경로 단위로 낸다.
**호출부** — `gm.py:1426`

#### `record_delta(session, turn, changes, ...)`

턴 델타를 append한다.
**호출부** — `game.py:820,1349` · `gm.py:1447`

#### `record_full_log(session, turn, entries) -> bool`

전 턴 대화를 별도 파일에 남긴다.
> *"raw_logs 20개 캡 우회"* `[코드]`

**호출부** — `gm.py:1449`

#### `archive_removed(session, target_turn, removed) -> bool`

되감기로 제거된 정보를 보존한다.
**호출부** — `rewind.py:268`(`rewind_to` 내부)

#### `available_range(session) -> tuple`

되감기 가능 범위 `(oldest, newest)`.
**호출부** — `display.py:293,323,341` 등 6곳

#### `rewind_to(session, target_turn) -> dict`

**무엇을 하는가** — 목표 턴 종료 시점으로 되돌린다.

**동작 4단계** `[코드]`
```
① 목표 턴 초과 델타를 역순으로 되돌린다
   "역순으로 되돌려야 중간 변경이 올바로 상쇄된다"
② rewind_log를 잘라낸다
③ full_logs로 raw_logs를 재구성
④ 제거된 정보를 아카이브로 이관
```

**반환** — `{ok, reason, removed_turns, changes, compression_rolled_back}`

**압축 롤백** — 압축 발생 시점을 델타에 기록하므로 **플랜별 주기를 몰라도 정확하다.**

**호출부** — `gm.py:571`

#### `serialize_log_entries(entries) -> list`

로그 직렬화. `io._serialize_log_entry`와 유사하다.

> 🔁 `io._serialize_log_entry`와 중복으로 보인다. `[확인 필요]`

#### `preview_rewind` / `restart_turn`

> ⚠️ **정의되지 않았다.** 제 초기 목록이 잘못이었다. `rewind.py`에 없다.

---

### `cache.py`

#### `build_scenario_cache_text(bot, model, session, ...) -> tuple`

**무엇을 하는가** — 룰북 텍스트를 조립하고 토큰을 센다.

**구성**
```
[1] 세계관 (worldview)
[2] 스토리 가이드 · 스탯 체계 · 묘사 가이드
[3] NPC 사전 (info_fields만)
[4] 상태이상 코드블럭
…
```

**패딩** — `MIN_CACHE_TOKENS` 미달이면 마침표로 채운다.

**호출부** — `cache.py:491` · `system.py:270` · `game.py:436`

#### `update_session_cache_state(session)`

캐시 관련 필드를 갱신한다.
**호출부** — `cache.py:520` · `system.py:303` · `game.py:472`

#### `remaining_ttl(session) -> int`

**남은 시간만 이어준다.** 재발급·복구 시 원래 고른 시간을 유지한다.
> *"매번 6시간을 새로 주면 결제한 것보다 오래 살아 비용이 어긋난다."* `[코드]` (5.24.4)

**호출부** — `cache.py:509` · `system.py:295` · `game.py:465`

#### `is_cache_expired(session) -> bool` / `is_session_open(session) -> bool`

시간상 만료 판정.
**호출부** — `display.py:37` · `cache.py:101` · `presence.py:66` / `display.py:582` · `gm.py:1504`

#### `restore_sessions_from_disk(bot)`

**봇 재시작 시 세션 복구.**
```
data.json 로드 → migrate_session_data → TRPGSession 재구성
SESSION_FIELDS 복원 · SESSION_RESET_FIELDS 초기화
캐시가 살아 있으면 재연결, 만료면 재발급
active_sessions에 채널 3개 등록
```

**호출부** — `main.py:134`

#### `get_home_section_ids(session) -> list`

연고지 관련 `keyword_memory` 섹션 id. **온디맨드 중복 주입 억제용.**
**호출부** — `cache.py:151,323`

---

### `cogs/presence.py`

#### `_check_expired(self)`

**15초마다** 열린 세션의 캐시 만료를 점검한다.
```
cache_name 없으면 건너뜀
is_cache_expired 아니면 건너뜀
cache_expired_notified면 건너뜀 (중복 방지)
→ 디스플레이 갱신 + 게임 채널 안내
```

`active_sessions`가 채널 3개로 같은 세션을 등록하므로 `session_id`로 걸러 한 번만 처리한다. `[코드]`

---

## 흐름

### 압축

```
턴 시작
  should_compress(session)
    is_compressing 확인
    (turn - last_compressed_turn) >= interval
  ↓ True
백그라운드 태스크
  is_compressing = True
  select_model(session)              로우면 후반에 late_model
  build_compression_prompt(session, log_text)
    build_context_hint               플랜별 지시
    is_aggressive                    크게 줄일지
  AI 호출
  compressed_memory 갱신
  mark_compressed(session)           last_compressed_turn·count
  settle_compression(actual_krw)     선결제 정산
  is_compressing = False
```

### 되감기

```
디스플레이 [되감기] 버튼
  available_range(session) → (oldest, newest)
  ↓
rewind_to(session, target_turn)
  ① 델타 역순 복원
  ② rewind_log 절단
  ③ full_logs로 raw_logs 재구성
  ④ archive_removed
  ↓
compression_rolled_back이면 압축 기억도 되돌아감
display.refresh
```

### 캐시 수명

```
세션 오픈 → upload_cache
  build_scenario_cache_text
  remaining_ttl(session)  ← open_minutes 반영
  Gemini 캐시 생성
  ↓
15초마다 presence._check_expired
  is_cache_expired → 디스플레이 🔴 만료 + 안내
  ↓
!캐시 재발급 또는 세션 다시 열기
  remaining_ttl로 남은 시간만
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **ai** | ai → memory | `build_compression_prompt`가 `memory_plan` 참조 |
| **base** | memory → base | `TRPGSession` · `migrate_session_data` · `save_session_data` |
| **cost** | memory → cost | `estimate_compression` · `settle_compression` · `accrue` |
| **ui** | ui → memory | `display`가 `available_range` · `is_cache_expired` |
| **session** | session → memory | `upload_cache` · `memory_plan.PLANS` |

---

## 발견 사항

> ⚠️ **로우 플랜의 저비용 전환이 무의미하다**
> ```
> DEFAULT_MODEL = gemini-3-flash-preview
> LOGIC_MODEL   = gemini-3-flash-preview   ← 같은 값
>
> low: model = LOGIC_MODEL, late_model = DEFAULT_MODEL
> ```
> **3회 압축 후 전환해도 같은 모델이라 비용이 동일하다.**
> `is_aggressive`가 압축 강도는 올리므로 출력 토큰은 줄지만,
> *"저렴한 모델로"*라는 플랜 설명은 현재 사실이 아니다.
>
> `constants.py`의 `PROFILE_AI_MODEL` 주석이 *"저비용 모델이 확정되면 이 상수만
> 교체"*라 한 것과 같은 상태다. **저비용 모델이 아직 정해지지 않았다.** `[추정]`

> ⚠️ **`memory_plan.plan_key` 호출부 없음**
> `get_plan`과 첫 두 줄이 같다.

> ⚠️ **`memory_plan.cost_curve` 호출부 없음**
> `compare_curves`가 같은 계산을 내부에서 다시 한다.

> ⚠️ **`total_ink_spent`·`total_usd`가 `TRACKED_PATHS`에 없다**
> `total_cost`만 추적한다. 되감기 후 디스플레이의 잉크 표기가 어긋난다.
> (base 영역에서도 지적)

> 🔁 **`rewind.serialize_log_entries`와 `io._serialize_log_entry` 중복**
> 둘 다 Gemini `Content`를 dict로 바꾼다.

> ⚠️ **`MIN_CACHE_TTL` 별칭**
> `cache.py`가 `CACHE_TTL_SECONDS`를 이 이름으로 임포트한다.
> '최소'가 아니라 기본 전체값이라 오해를 부른다. (base 영역에서도 지적)

> 🔁 **압축 실행이 `game.py`에 두 번 있다 — 유사도 90%**
> ```
> _run_auto_compression  game.py:754~848  (94줄)  자동 GM 경로
> compress_memory        game.py:1278~1374 (96줄) !기억압축 명령
> ```
> 본문 유사도 0.898. `memory` 영역의 핵심 동작인데 모듈이 아니라 cog에 있고,
> 두 경로가 각자 구현한다. 압축 로직을 고치면 두 곳을 함께 고쳐야 한다.

---

## 확인 필요 목록

### 플랜

- [ ] **로우 플랜의 `late_model`이 `model`과 같은 값입니다.**
      *"저렴한 모델로 크게 압축"*이라는 설계 의도가 실현되지 않았습니다.
      저비용 모델 후보가 정해지면 `DEFAULT_MODEL`을 바꿀 계획입니까?
      아니면 플랜 설명을 *"압축 강도를 높인다"*로 고쳐야 합니까?

### 되감기

- [ ] `total_ink_spent`·`total_usd`를 `TRACKED_PATHS`에 넣어야 합니까?
      현재는 되감아도 잉크 표기가 그대로입니다.
- [ ] `REWIND_MAX_TURNS = 20`이 적절합니까?

### 정리

- [ ] `memory_plan.plan_key`·`cost_curve` 호출부가 없습니다. 제거해도 됩니까?
- [ ] `rewind.serialize_log_entries`와 `io._serialize_log_entry`가 겹칩니다.
- [ ] `MIN_CACHE_TTL` 별칭을 `CACHE_TTL_SECONDS`로 되돌려야 합니까?

### 구조

- [ ] **압축 실행이 `game.py`에 두 번 있습니다.** 유사도 90%입니다.
      `_run_auto_compression`(754~848, 자동 GM)과 `compress_memory`(1278~1374, 명령).
      `memory` 영역 모듈로 옮겨 하나로 합쳐야 합니까?
