# cost — 기능 명세

- **모듈**: `core/cost.py`(482) · `estimate.py`(455) · `ink.py`(85) · `accounts.py`(211) · `terms.py`(191) · `stats.py`(196)
- **연관 cogs**: `cogs/system.py`(857줄)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

---

## 개요

돈을 다룬다. 계산·예측·환산·계정·통계가 여기 모여 있다.

| 모듈 | 담당 |
|---|---|
| `cost.py` | 토큰 단가 계산, 누적, 비용 임베드 |
| `estimate.py` | 턴·세션오픈·TTS·압축 예상 |
| `ink.py` | 게임머니 환산 |
| `accounts.py` | 계정·잔액·원장 |
| `terms.py` | 약관 동의 DM |
| `stats.py` | 누적 플레이 기록 |

**경계** — 계산은 여기서, 표시는 ui가, 차감 시점 판단은 ai(턴)와 session(오픈)이 한다.

---

## 설계 의도

### 청구 근거는 달러

원화만 쌓으면 환율이 바뀔 때 과거분이 왜곡된다. `accrue`가 KRW와 USD를 함께 쌓는다. `[코드]` (5.29.0)

```python
core.accrue(session, krw, usd)   # usd를 주지 않으면 현재 환율로 역산
```

### 잉크 환산은 실수령 기준

```
INK_UNIT_KRW = 10   판매가
INK_NET_KRW  = 7    수수료 30% 차감 후 실수령
cost_to_ink  = ceil(비용 / 7)
```

플레이어는 10원에 사고 운영자는 7원을 받으므로, **비용 충당 기준은 실수령**이어야 한다. `[코드]`

### 예측은 보수적으로

> *"미진행 턴의 데이터량을 평균보다 약간 많게 잡는 보수계수(기획 규정)"* — `CONSERVATIVE_FACTOR = 1.15` `[코드]`

> *"프롬프트 조립 실패 시 사용할 입력 토큰 보수 기본값. 0으로 두면 예측이 실제보다 크게 낮아져 잔액 차단이 무력화된다."* — `FALLBACK_BODY_TOKENS = 3000` `[코드]`

### 표본이 쌓이면 실측으로 대체

`update_stats`가 층위별 출력 토큰의 이동평균을 유지한다. 표본 수에 따라 신뢰도가 오른다(`CONFIDENCE_MID=3`, `CONFIDENCE_HIGH=10`).

### 압축 비용은 선결제

> *"기획 규정: 예상액의 20%"* — `COMPRESSION_PREPAY_RATIO = 0.20` `[코드]`

매 턴 조금씩 미리 받고, 실제 압축 시 정산한다. 5턴마다 한 번에 큰 금액이 나가는 것을 막는다. `[추정]`

### 프로필 AI는 무료

집계는 하되 차감하지 않는다(profile 영역 참조).

---

## 데이터 구조

### 단가 (`PRICING_1M`, constants)

100만 토큰당 달러. 5개 모델. 2026년 실가와 일치함을 확인했다.

```
gemini-3-flash-preview   INPUT 0.50 · OUTPUT 3.00 · CACHE_READ 0.05 · STORAGE 1.00/h
```

### 예측 상수 (`estimate.py`)

| 상수 | 값 | 용도 |
|---|---|---|
| `CHARS_TO_TOKENS` | 0.65 | 한국어 문자당 토큰. *"[TOKENS] 실측 로그로 보정한다"* |
| `CONSERVATIVE_FACTOR` | 1.15 | 보수계수 |
| `FALLBACK_BODY_TOKENS` | 3000 | 조립 실패 시 |
| `DEFAULT_BASELINE` | 묘사 2400 · 지시 620 · 판단 280 · 추출 340 | 표본 없을 때 |
| `CONFIDENCE_MID` / `HIGH` | 3 / 10 | 신뢰도 구간 |
| `COMPRESSION_INTERVAL` | 5 | 압축 주기 |
| `COMPRESSION_PREPAY_RATIO` | 0.20 | 선결제 비율 |

> ⚠️ **`approx_cache_tokens`는 0.33을 쓴다.** 같은 한국어 텍스트인데 계수가 2배 다르다. '발견 사항' 참조.

### 세션 필드

| 필드 | 용도 |
|---|---|
| `total_cost` · `total_usd` | 누적 |
| `total_ink_spent` | 실제 차감 잉크 |
| `last_turn_cost` | 직전 턴 |
| `cost_stats` | 층위별 이동평균 |
| `last_estimate` | 직전 예상 |
| `open_prepaid_ink` | 세션 오픈 선결제 |
| `compression_prepaid_krw` | 압축 선결제 누적 |
| `interpret_cost_krw` | 시간 해석 누적 |
| `profile_ai_cost_krw` | 프로필 AI (표시되지 않음) |
| `_last_input_estimate` | 예측 대조용 (런타임) |

### 계정 파일 (`accounts/{uid}.json`)

```python
{
  "user_id", "registered": bool, "terms_version",
  "ink_balance": int,
  "history": [{at, delta, balance, reason}]
}
```

### 통계 파일 (`stats/{uid}.json`)

`base` 영역 명세 참조. `has_played`가 사전 프로필 질문 생략 판정에 쓰인다.

---

## 기능 목록

### `cost.py`

#### `extract_token_usage(meta) -> tuple`

**무엇을 하는가** — `usage_metadata`에서 `(입력, 출력, 캐시, 사고)`를 뽑는다.

**핵심** — **`thoughts_token_count`를 출력에 합산한다.** 누락하면 47% 과소 계상된다.

**호출부** — **15곳**

#### `accrue(session, krw=0.0, usd=None)`

**무엇을 하는가** — KRW와 USD를 함께 누적한다.

**동작** — `usd`를 안 주면 현재 환율로 역산. `krw`가 0이면 `usd`로 계산.

**호출부** — **20곳**

#### `calculate_text_gen_cost_breakdown(model_id, input_tokens, output_tokens, cached_read_tokens)`

**반환** — `input_usd`·`cache_read_usd`·`output_usd`·`total_usd` + 원화 환산.

**중요** — 신규 입력은 `input_tokens - cached_read_tokens`다. 캐시 읽기는 별도 단가.

**호출부** — **18곳**

#### `calculate_upload_cost(...)` / `calculate_upload_cost_usd(...)` / `calculate_storage_cost_usd(...)`

업로드·보관비. 뒤 둘은 5.29.1에서 추가했다.

> ⚠️ **`calculate_upload_cost_usd`·`calculate_storage_cost_usd` 호출부 없음.**
> 5.29.1에서 만들었으나 실제로는 `upload_cost / EXCHANGE_RATE`로 역산하는 코드를
> 넣었다. **만들어 놓고 쓰지 않았다.**

#### 임베드 빌더 5종

| 함수 | 호출부 |
|---|---|
| `build_cache_cost_embed` | `system.py:283,324` · `game.py:450` |
| `build_text_gen_cost_embed` | `character.py:1217` |
| `build_image_gen_cost_embed` | `media.py:195` |
| `build_compression_cost_embed` | `game.py:796,1323` |
| `build_turn_cost_embed` | `game.py:719` |

#### `format_breakdown(entry) -> str`

**기획 규정** — 캐시입력×단가 + 순수입력×단가 + 출력×단가 + 기타.
**호출부** — `cost.py:396`(`build_turn_cost_embed` 내부)

#### `calculate_cost(...)`

> `[확인 필요]` `cache.py:495,496`만 쓴다. `calculate_text_gen_cost_breakdown`과
> 역할이 겹쳐 보인다.

---

### `estimate.py`

#### `_tok(text) -> int`

`len(text) * CHARS_TO_TOKENS`.

#### `get_baseline(session) -> dict`

시나리오 `cost_baseline` 또는 `DEFAULT_BASELINE`.

#### `update_stats(session, layer, out_tokens, thought_tokens=0)`

층위별 출력 토큰 이동평균 갱신.
**호출부** — `game.py:529,806,1335` 등 6곳

#### `estimate_input_tokens(session, action="PROCEED") -> dict`

**무엇을 하는가** — `PromptBuilder.build_prompt`를 실제로 조립해 길이를 잰다.

**주의** — 조립 실패 시 `FALLBACK_BODY_TOKENS`(3000).

> **5.30.0 이후 이 값이 커졌다.** 장소·퀘스트 블록이 들어가면서 프롬프트가 늘었다.

#### `estimate_turn(session, action="PROCEED") -> dict`

**반환** — `{min_ink, max_ink, min_krw, max_krw, confidence}`

**호출부** — `gm.py:1517,1533` (턴 진행 전 잔액 검사)

#### `approx_cache_tokens(scenario_data) -> int`

**무엇을 하는가** — 업로드 전 캐시 토큰 근사.

**구성** — `worldview`·`story_guide`·`stat_system`·`desc_guide`·`status_code_block` + NPC(`info_fields`만).

> ⚠️ **계수 0.33.** `CHARS_TO_TOKENS`(0.65)와 2배 다르다.

#### `estimate_session_open(session, hours) -> dict`

캐시 업로드 + 유지 비용. `cache_tokens`가 0이면 `approx_cache_tokens`로 근사.
**호출부** — `session_flow.py:490` · `display.py:111` · `gm.py:660`

#### `record_actual_input(session, layer, fresh_tokens) -> dict | None`

예측 대비 실제를 기록한다. `get_calibration`이 이를 쓴다.
**호출부** — `gm.py:2082` **한 곳**

> `[확인 필요]` 지시층위만 기록한다. 묘사층위는 하지 않아 보정이 편향될 수 있다.

#### `get_calibration(session) -> float`

예측 보정 계수.
**호출부** — `estimate.py:133`(내부)

#### `estimate_compression` / `compression_prepay` / `settle_compression`

압축 예상·선결제·정산.
**호출부** — `gm.py:1535` · `game.py:805,1334`

#### `estimate_tts(session) -> dict`

TTS 예상. **합산하지 않고 구분 표기**한다(기획 규정).
**호출부** — `display.py:77` · `gm.py:1546`

---

### `ink.py`

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `cost_to_ink(krw)` | `ceil(krw / 7)` | **20곳** |
| `ink_to_krw(ink)` | `ink * 10` (판매가) | `ink.py:49` |
| `ink_to_net_krw(ink)` | `ink * 7` (실수령) | `ink.py:49` |
| `refund_ink(prepaid, used_krw)` | 환급액 | `display.py:393` (**`as _refund` 별칭**) |
| `can_afford(balance, cost)` | 잔액 충분 여부 | ⚠️ **호출부 없음** |
| `format_ink(ink)` | 표기 | ⚠️ **호출부 없음** |
| `plan_catalog()` | 충전 플랜 목록 | ⚠️ **호출부 없음** |

> **주의** — `refund_ink`는 별칭 임포트라 단순 검색에 잡히지 않는다.
> 이전에 미사용으로 오판한 적이 있다(5.29.x 이전).

`can_afford`는 `estimate_turn`의 `max_ink`를 직접 비교하는 방식으로 대체됐다. `[추정]`
`format_ink`는 `display.py`가 임포트하나 쓰지 않는다.

---

### `accounts.py`

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `load_account(uid)` | 계정 조회 | 8곳 |
| `is_registered(uid)` | 등록 여부 | 8곳 |
| `register_account(uid)` | 등록 + 가입 선물 | `terms.py:109` · `system.py:416,587` |
| `get_balance(uid)` | 잔액 | 7곳 |
| `add_ink(uid, amount, reason)` | 적립 | `display.py:451` · `terms.py:114` · `system.py:420` |
| `deduct_ink(uid, amount, allow_overdraft)` | 차감 | `system.py:423` · `session.py:360` · `gm.py:1439` |
| `set_balance(uid, amount, reason)` | 잔액 지정 | `system.py:590` (`!잉크`) |

**락** — `_lock_for(uid)`로 사용자별 직렬화. 원장 이력을 남긴다.

**`deduct_ink`의 `allow_overdraft`** — 턴 차감은 `True`다. 음수가 되면 1잉크로 보정하고 초과분은 운영자 부담이다. `[코드]`

---

### `terms.py`

약관 동의 DM. `is_registered`로 확인하고 `register_account`를 호출한다.

---

### `stats.py`

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `load_stats(uid)` | 통계 조회 | 11곳 |
| `has_played(uid, sid)` | 해당 시나리오 경험 | `creation.py:169` |
| `record_session` · `record_turn` | 기록 | ⚠️ **호출부 없음** |

> ⚠️ **통계가 갱신되지 않는다.** `record_session`·`record_turn`이 정의만 되어 있다.
> `load_stats`는 11곳에서 읽는데 **쓰는 곳이 없다.**
>
> 영향
> - `intro.judge_level`이 `sessions`·`turns`로 인지 수준을 판정한다 → **항상 `LEVEL_NEW`**
> - `creation.can_skip_profile_question`이 `has_played`를 본다 → **항상 생략**
> - 명예의 전당·`!사용량 전체`의 통계도 비어 있을 것

---

## 흐름

### 턴 비용의 일생

```
턴 시작
  estimate_turn(session)          예상 min~max 잉크
    estimate_input_tokens          PromptBuilder로 실측 조립
    get_calibration                과거 예측 오차 보정
    update_stats의 이동평균         층위별 출력 예상
  ↓ 잔액 < max_ink 면 차단
4층위 실행 (각각)
  extract_token_usage             사고 토큰 합산
  calculate_text_gen_cost_breakdown
  accrue(session, krw, usd)
  write_cost_log
  turn_cost_log.append
  ↓
턴 종료
  cost_to_ink(turn_cost)          ceil(krw / 7)
  total_ink_spent += ink
  deduct_ink(uid, ink, allow_overdraft=True)
  build_turn_cost_embed → 마스터 채널
```

### 세션 오픈

```
유지 시간 입력 → resolve_minutes
  ↓
estimate_session_open(session, hours)
  cache_tokens 0이면 approx_cache_tokens로 근사
  ↓
OpenConfirmView — 잔액 확인
  ↓
upload_cache
  calculate_upload_cost(실측 토큰)
  accrue
  open_prepaid_ink 기록
  ↓
세션 클로즈
  refund_ink(prepaid, used_krw)   사용분만 계산
  add_ink(환급)
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **ai** | ai → cost | 층위마다 `breakdown`·`accrue`·`update_stats` |
| **memory** | memory → cost | `estimate_compression`·`settle_compression` |
| **session** | session → cost | `estimate_session_open`·`accounts`·`terms` |
| **ui** | ui → cost | `display`가 `estimate_turn`·`estimate_tts`·`cost_to_ink` |
| **base** | cost → base | `io.process_cache_deletion`이 `calculate_storage_cost`·`accrue` |
| **profile** | profile → cost | `calculate_text_gen_cost_breakdown` (집계만) |

---

## 발견 사항

> 🔴 **통계가 갱신되지 않는다 — `record_session`·`record_turn` 호출부 없음**
>
> `load_stats`를 11곳에서 읽는데 **쓰는 곳이 하나도 없다.**
>
> 연쇄 영향
> ```
> intro.judge_level        sessions·turns로 판정 → 항상 LEVEL_NEW
>                          모든 유저가 매번 초심자 풀소개를 본다
> can_skip_profile_question has_played → 항상 False → 질문 항상 생략
> 명예의 전당·월드보드      집계가 비어 있을 것
> ```
>
> 5.22.x에서 소개의 인지 수준 분기를 정교하게 만들었으나, **판정 근거가 되는
> 데이터가 채워지지 않아 경험자 경로가 실행되지 않는다.** `[확인 필요]`

> ⚠️ **토큰 계수가 두 개다**
> ```
> CHARS_TO_TOKENS      0.65  (estimate 전역)
> approx_cache_tokens  0.33  (5.28.3에서 실측 보정)
> ```
> 같은 한국어 텍스트인데 2배 다르다. 영도 캐시 실측 26,268토큰 기준으로는
> 0.33이 맞다(오차 0.5%). **`CHARS_TO_TOKENS`가 과대 추정하고 있을 수 있다.**

> ⚠️ **`calculate_upload_cost_usd`·`calculate_storage_cost_usd` 호출부 없음**
> 5.29.1에서 만들었으나 정작 `upload_cost / EXCHANGE_RATE` 역산 코드를 넣었다.
> **만들어 놓고 쓰지 않았다.**

> ⚠️ **`ink.can_afford`·`format_ink`·`plan_catalog` 호출부 없음**
> `format_ink`는 `display.py`가 임포트만 하고 쓰지 않는다.

> ⚠️ **`record_actual_input`이 지시층위만 기록**
> 묘사층위는 하지 않아 `get_calibration` 보정이 편향될 수 있다.

> 🔁 **`calculate_cost`와 `calculate_text_gen_cost_breakdown` 중복**
> `cache.py:495,496`만 전자를 쓴다.

> ⚠️ **`profile_ai_cost_krw`가 표시되지 않음** (profile 영역에서도 지적)

---

## 확인 필요 목록

### 🔴 최우선 — 통계 미갱신

- [ ] **`record_session`·`record_turn`을 연결해야 합니까?**
      통계를 읽는 곳은 11곳인데 쓰는 곳이 없습니다.
      그 결과 `intro.judge_level`이 항상 `LEVEL_NEW`를 반환해
      **모든 유저가 매번 초심자 풀소개를 봅니다.**
      명예의 전당·월드보드 집계도 비어 있을 것입니다.
- [ ] 연결한다면 어느 시점입니까? 턴 종료 시 `record_turn`, 세션 클로즈 시
      `record_session`이 자연스러워 보입니다.

### 계수

- [ ] **`CHARS_TO_TOKENS`(0.65)를 0.33으로 낮춰야 합니까?**
      영도 캐시 실측으로는 0.33이 맞습니다(오차 0.5%).
      0.65면 입력 토큰 예측이 2배로 잡혀 잔액 차단이 과하게 걸릴 수 있습니다.
- [ ] 두 계수가 다른 이유가 있습니까? (캐시 텍스트와 프롬프트의 성격 차이 등)

### 미사용

- [ ] `calculate_upload_cost_usd`·`calculate_storage_cost_usd` — 5.29.1에서
      만들었으나 역산 코드를 대신 넣었습니다. 이 함수들로 교체할까요?
- [ ] `ink.can_afford`·`format_ink`·`plan_catalog` — 제거해도 됩니까?
      `plan_catalog`는 결제 도입 시 필요해 보입니다.

### 정합

- [ ] `record_actual_input`이 지시층위만 기록합니다. 묘사층위도 해야 합니까?
- [ ] `calculate_cost`와 `calculate_text_gen_cost_breakdown`이 겹칩니다.
- [ ] `profile_ai_cost_krw`를 어딘가에 표시해야 합니까?
