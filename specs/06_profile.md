# profile — 기능 명세

- **모듈**: `core/profile_gen.py`(669) · `profile_runner.py`(347) · `profile_creation_ui.py`(613) · `profile_ai.py`(178) · `profiles.py`(232) · `profile_ui.py`(417)
- **연관 cogs**: `cogs/character.py`(1,248줄)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

---

## 개요

캐릭터를 만들고 관리한다. **10개 영역 중 가장 크다**(3,703줄).

| 모듈 | 담당 |
|---|---|
| `profile_gen.py` | 생성 모듈 10종 — 시나리오가 지시하는 알고리즘의 실행 단위 |
| `profile_runner.py` | 실행부 — 시나리오 JSON을 해독해 단계를 진행 |
| `profile_creation_ui.py` | 디스코드 UI — 버튼·셀렉트·모달 |
| `profile_ai.py` | AI 검증·병합 (무료 제공) |
| `profiles.py` | 사전 저장 프로필 저장소 |
| `profile_ui.py` | 사전 프로필 관리 DM |
| `cogs/character.py` | 수동 조작 명령 9종 |

**3계층 구조** — `gen`(모듈) → `runner`(해독·실행) → `creation_ui`(화면). 이 분리가 이 영역의 핵심 설계다.

---

## 설계 의도

### 코드는 기능만, 시나리오가 알고리즘을 지시한다

> *"기획 확정 사항 — 필요 기능은 함수 형태로 코드에 두고, 시나리오 JSON에는 항목별 순서·사용 모듈·모듈이 사용할 인자를 배치한다. 실행부 하나가 이를 해독해 종료 콜까지 실행한다."* `[코드]`
>
> *"따라서 이 모듈의 함수들은 '무엇을 물을지'를 모른다. 시나리오가 지시한 인자를 받아 처리만 한다. 시나리오가 바뀌면 코드는 그대로 두고 JSON만 바꾼다."* `[코드]`

```json
"profile_creation": [
  {"field": "성별", "module": "select_one", "args": {"options": ["남","여"]}},
  {"field": "무공", "module": "intersect_list",
   "args": {"depends": ["문파","경지"], "source": "martial_arts", "pick": "2-4"}}
]
```

### 능력치는 수치가 아니라 등급으로 고른다

> *"수치 대신 별명으로 고르게 한다. 비율 기준이라 스탯 개수가 다른 시나리오에서도 그대로 통한다."* `[코드]`

```
최대치 = 스탯 개수 × 상한(기본 20)
영도(3종) 60 · 무협(4종) 80

총합 등급  허접 25% · 약골 35% · 평범 50% · 튼튼 62% · 능력자 75% · 먼치킨 90%
편차 등급  만능 10% · 무난 30% · 뚜렷 50% · 극단 제한 없음
```

**산출은 언제나 랜덤**이고 총합·편차·특화가 제약이다. 5.6.0에서 프리셋 방식을 이렇게 바꿨다.

### 직업 치중은 재배분이 아니라 교환

`swap_to_top(values, target)` — **최고값과 목표 스탯의 자리를 바꾼다.**
총합·편차 제약을 깨지 않으면서 직업 특성을 반영한다.

### 프로필 AI는 무료

> *"기획 규정 — 프로필 생성 단순호출은 무료 제공 추진. 비용은 집계하되(운영 파악용) 잉크 차감 대상에서 제외한다."* `[코드]`
>
> *"짧은 입력에 대한 판정·정리라 세계관 룰북 전량이 필요하지 않다. 필요한 시나리오 정보만 사용자 프롬프트로 추려 넣는다."* `[코드]`

`_scenario_context`가 `worldview`·`desc_guide`를 1,200자로 잘라 넣는다.
> *"세계관 전문을 넣으면 무료 제공 취지에 어긋나는 비용이 든다."* `[코드]`

---

## 데이터 구조

### 시나리오 `profile_creation` 단계

```json
{
  "field": "직업",
  "module": "select_one",
  "args": {"options": [...], "describe": "job_desc", "warn": "job_warn"},
  "guide_from": ["성별", "나이"],
  "revise": {...}
}
```

**모듈 10종** (`MODULES`)

| 모듈 | 하는 일 |
|---|---|
| `select_one` | 목록 제시 → 텍스트 입력 → 오타 검사 → 확인 |
| `free_text` | 자유 입력 |
| `roll_stats` | 제약 랜덤 능력치 |
| `intersect_list` | 선행 선택의 교집합에서 N개 |
| `pick_random` | 무작위 N개 |
| `merge_inputs` | 여러 입력 합치기 |
| `ai_validate` | AI 검증 |
| `ai_merge` | AI 병합 |
| … | |

`AI_MODULES = {"ai_validate", "ai_merge"}` — 이 둘만 AI를 부른다.

### 실행 상태 (`run`)

```python
{
  "index": 3,              # 현재 단계
  "chosen": {"성별": "남", ...},
  "pending": {"field", "value"},   # 확인 대기
  "constraints": {"total": 30, "max_spread": 6, "top": "근력"},
  "history": [],
}
```

### 응답 타입 (`profile_runner`)

| 타입 | 의미 |
|---|---|
| `ASK` | 유저 입력을 기다린다 |
| `CONFIRM` | 확인 메시지 — 예/아니오 |
| `WARN` | 경고 후 확인 |
| `AUTO` | 자동 선택됨, 다음으로 |
| `DONE` | 전체 완료 |
| `ERROR` | 오류 |

### 사전 프로필 저장소 (`profiles/{uid}.json`)

```python
{
  "user_id": "...",
  "profiles": [
    {"id", "tag", "name", "scenario_id", "fields": {...}, "created_at"}
  ]
}
```

`tag`는 짧은 식별자다. 같은 이름이 여럿일 때 구분에 쓴다(`display_name`).

### 상수

| 상수 | 값 | 용도 |
|---|---|---|
| `TYPO_RATIO` | 0.6 | 오타 후보 최소 유사도 |
| `DEFAULT_STAT_CAP` | 20 | 스탯 상한. `ability_stat_max`로 덮인다 |
| `CONTEXT_LIMIT` | 1200 | 프로필 AI에 넣을 시나리오 요약 상한 |
| `SELECT_THRESHOLD` | (ui) | 이하면 옵션을 본문에 나열 |
| `MAX_OPTIONS` | (ui) | 셀렉트 최대 항목 |

---

## 기능 목록

### `profile_gen.py` — 생성 모듈

#### `select_one(scenario_data, args, chosen, user_input=None) -> dict`

**무엇을 하는가** — 목록을 제시하고 텍스트 입력으로 선택받는다.

> *"기획 규정 — 검색 실패 시 오타 검사를 실시하고, 확인 메시지는 필수다."* `[코드]`

**반환** — `{status: prompt|confirm|retry, options, value, suggestions}`

**호출부** — `profile_runner.py:175`

#### `describe_option` / `warn_on_choice`

선택지 설명 · 경고 문구. 시나리오가 `describe`·`warn` 키로 지정한다.
**호출부** — `profile_runner.py:194,192`

#### `branch_mode(args, chosen) -> str`

> ⚠️ **호출부 없음.** 조건 분기 모드를 정하는 함수로 보인다.

#### `goto_step(steps, target_field) -> int | None`

특정 필드의 단계 인덱스. `jump_to`가 쓴다.
**호출부** — `profile_runner.py:290`

#### 능력치 등급

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `get_total_tiers` · `get_spread_tiers` | 등급표. 시나리오가 덮어쓸 수 있다 | 각 2곳 |
| `tier_to_total(tier, args, sd)` | 등급 → 총합 수치 | 3곳 |
| `max_possible_spread(args, total)` | 가능한 최대 편차 | `profile_gen.py:259` |
| `tier_to_spread(tier, args, sd, total)` | 등급 → 편차 상한 | 2곳 |
| `describe_tier(tier, args, sd, total)` | 등급 설명 | `creation_ui` 2곳 |

#### `roll_stats(args, *, total=None, max_spread=None, ...) -> dict`

**무엇을 하는가** — 제약을 만족하는 능력치를 무작위로 만든다.

**동작** — `_random_partition`으로 시도하고, 실패하면 `_forced_partition`으로 강제 배분한다.

**호출부** — `profile_runner.py:143` · `profile_gen.py:474,476`(`reroll_stats` 내부)

#### `swap_to_top(values, target) -> dict`

최고값과 목표 스탯의 자리를 교환한다. **총합 유지.**
**호출부** — `profile_runner.py:154`

#### `job_top_stat(scenario_data, job) -> str | None`

직업의 치중 스탯. 영도는 16종 중 8종에 `top_stat`이 있다.
**호출부** — `profile_runner.py:152`

#### `starting_items(scenario_data, job) -> list`

공통 소지품 + 직업별. 영도는 공통 6 + 직업별 2.
**호출부** — `profile_creation_ui.py:156,190`

#### `reroll_stats(args, previous=None, **constraints) -> dict`

> ⚠️ **호출부 없음.** 이전 결과와 같으면 한 번 더 굴린다.
> UI가 `cancel_pending` + 재호출로 우회한다. `[추정]`

#### `revise_field(args, chosen) -> dict | None`

확정 후 앞선 선택을 수정하는 규칙.
**호출부** — `profile_runner.py:257`

#### `merge_inputs` / `guide_from_prior` / `intersect_list` / `resolve_pick_count` / `pick_random`

각각 입력 합치기 · 선행 선택 기반 안내 · 교집합 추출 · 개수 해석 · 무작위 선택.

**`resolve_pick_count`는 `quest.py:180`에서도 쓴다.** 영역을 넘는 재사용.

#### `get_steps(scenario_data) -> list`

`profile_creation` 배열.
**호출부** — 6곳

#### `validate_steps(scenario_data) -> list`

> ⚠️ **호출부 없음.** 시나리오 알고리즘의 정합성을 검사한다.
> **저작 지원 도구로 유용하나 어디서도 부르지 않는다.**

---

### `profile_runner.py` — 실행부

#### `new_run(scenario_data, *, prefill=None) -> dict`

실행 상태를 만든다. `prefill`을 주면 해당 단계를 건너뛴다(사전 프로필).
**호출부** — `profile_creation_ui.py:34,551`

#### `step(scenario_data, run, user_input=None) -> dict`

**한 단계를 진행한다.** 이 영역의 심장이다.

```
user_input=None  → 현재 단계를 렌더할 정보만
user_input 있음  → 모듈 실행 → ASK/CONFIRM/WARN/AUTO/DONE/ERROR
```

**호출부** — `profile_creation_ui.py:50` **한 곳**

#### `confirm(scenario_data, run) -> dict`

확인 대기 값을 확정한다. **확정 후 `revise_field` 규칙이 걸려 있으면 앞선 선택을 수정한다.** `[코드]`
**호출부** — `profile_creation_ui.py:278,322`

#### `cancel_pending(run)` / `set_stat_constraint(run, key, value)`

확인 취소 · 능력치 제약 설정.
**호출부** — 각 5곳 · 3곳

#### `go_back(scenario_data, run) -> tuple`

이전 단계로.
**호출부** — `profile_creation_ui.py:453`

#### `jump_to(scenario_data, run, field) -> tuple`

> ⚠️ **호출부 없음.** 기획 필요기능 5번(*"특정 단계만 호출"*)이다.
> UI에 "이 항목만 다시 정하기" 버튼이 없다.

#### `result(run) -> dict` / `progress(scenario_data, run) -> str` / `current_field` / `advance_index`

완성 프로필 · 진행 표기 · 현재 필드 · 인덱스 전진.

---

### `profile_ai.py` — 검증·병합

#### `_scenario_context(scenario_data) -> str`

`worldview`·`desc_guide`를 1,200자로 추린다.

#### `_accrue(session, response) -> float`

**무엇을 하는가** — 비용을 `session.profile_ai_cost_krw`에 집계한다.

> ⚠️ **`total_cost`·`total_usd`에 누적하지 않고 `write_cost_log`도 하지 않는다.**
> 무료 제공이라 차감은 안 하지만, **운영자가 `!사용량`으로 봐도 이 비용이 보이지 않는다.**
> 실제 API 비용은 발생하는데 어느 집계에도 잡히지 않는다. `[확인 필요]`

#### `_call(bot, system_instruction, schema, user_prompt, session=None, temperature=0.2)`

`call_with_retry`로 보호된다(`resilience.py:74`).

#### `validate(bot, scenario_data, field, value, session=None) -> dict`

입력이 세계관에 맞는지 검증한다.
**호출부** — `profile_runner.py:312`

#### `merge(bot, scenario_data, field, values, session=None) -> str`

여러 입력을 하나로 정리한다.
**호출부** — `profile_runner.py:324`

#### `format_validation(field, result) -> str`

검증 결과 문구.
**호출부** — `profile_runner.py:316`

---

### `profiles.py` — 사전 저장소

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `load_all(user_id)` | 전체 저장소 | 4곳 |
| `list_profiles(uid, sid=None)` | 목록 | 8곳 |
| `count_profiles` | 개수 | `creation.py:174` |
| `create(...)` | 저장 | `profile_ui.py:397` · `creation_ui.py:496` |
| `delete(...)` | 삭제 | `profile_ui.py` |
| `duplicate_names(uid, sid)` | 같은 이름 집합 | 2곳 |
| `display_name(profile, dup)` | 중복 시 태그 병기 | 4곳 |
| `search` · `search_by_tag` | 검색 | `profile_ui.py:152,166` |
| `preview` · `detail` | 요약 · 상세 | `profile_ui.py` |

**락** — `_lock_for(user_id)`로 사용자별 직렬화. 원자적 쓰기(`_write`).

---

### `profile_creation_ui.py` — 화면

#### `start(bot, session, uid, char_name, channel) -> State | None`

**진입점.** 사전 프로필이 있으면 `PrefillView`, 없으면 바로 생성.

**알고리즘이 없으면** 안내 후 `_advance_flow`로 넘긴다(5.21.1).

#### `render(state, channel)` / `_send(state, channel, res, *, view)` / `_embed(state, res)`

메시지 하나를 임베드로 갱신한다(5.21.1).
`state.notes`에 AUTO 확정·AI 반려 사유를 모아 다음 화면에 함께 싣는다.

#### `finish(state, channel)`

완성 화면 + `SaveProfileView`. **저장 여부를 고른 뒤** `_advance_flow`로 세션 플로우에 복귀한다.

#### 뷰 클래스

`PrefillView` · `PrefillSelect` · `OptionSelect` · `ConfirmView` · `StatTierView` · `BackOnlyView` · `SaveProfileView`

---

### `cogs/character.py` — 수동 조작

| 명령 | 하는 일 |
|---|---|
| `!참가` | 세션 참가 |
| `!설정` | 프로필 항목 수정 |
| `!증감` | 자원 증감 |
| `!외형` | 외형 설정 |
| `!프로필` | 조회 |
| `!엔피씨` | NPC 관리 (**267줄 — 거대 함수**) |
| `!능력치` | 스탯 조작 |
| `!설정생성` | AI로 초안 생성 |
| `!캐릭터가져오기` | 사전 프로필 불러오기 |

---

## 흐름

### 프로필 생성 전 과정

```
session_flow render(profile)
  ↓
profile_creation_ui.start(bot, session, uid, name, channel)
  get_steps 없으면 → 안내 + _advance_flow
  사전 프로필 있으면 → PrefillView
  ↓
runner.new_run(scenario_data, prefill=...)
  ↓ 반복
render(state, channel)
  runner.step(scenario_data, run, user_input)
    시나리오의 module을 profile_gen에서 찾아 실행
    ASK    → 입력 대기 (OptionSelect 또는 모달)
    CONFIRM→ ConfirmView
    WARN   → 경고 후 확인
    AUTO   → state.notes에 쌓고 계속
    ERROR  → BackOnlyView
    DONE   → finish
  ↓
finish(state, channel)
  완성 임베드 + SaveProfileView
  ↓ 저장 / 저장 안 함
_advance_flow → session_flow.on_profile_done
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **session** | session ↔ profile | `render(profile)` → `start` · `on_profile_done` 콜백 |
| **base** | profile → base | `resolve_pc` · `get_uid_by_char_name` · `save_session_data` |
| **ai** | profile → ai | `call_with_retry` |
| **cost** | profile → cost | `calculate_text_gen_cost_breakdown` (집계만) |
| **quest** | quest → profile | `quest.py:180`이 `resolve_pick_count` |
| **world** | profile → world | 없음 |

---

## 발견 사항

> ⚠️ **프로필 AI 비용이 어느 집계에도 잡히지 않는다**
> `_accrue`가 `session.profile_ai_cost_krw`에만 누적한다.
> `total_cost`·`total_usd`에 넣지 않고 `write_cost_log`도 하지 않는다.
> 무료 제공이라 차감은 안 하지만, **실제 API 비용은 발생하는데 `!사용량`에도
> `cost_log.txt`에도 나타나지 않는다.**
**확인 결과** — `profile_ai_cost_krw`를 읽는 곳이 **하나도 없다.**
> `profile_ui.py:365`가 `_StandaloneSession`의 초기값으로 두는 것뿐이다.
> 디스플레이·`!사용량`·`cost_log` 어디에도 나타나지 않는다.
> **집계는 되는데 볼 방법이 없다.**

> ⚠️ **미사용 함수 4종**
> `branch_mode` · `reroll_stats` · `validate_steps` · `jump_to`
>
> 이 중 둘은 특히 아깝다.
> - `validate_steps` — 시나리오 알고리즘 정합성 검사. **저작 지원에 유용하나 부르는 곳이 없다.**
> - `jump_to` — 기획 필요기능 5번. UI 버튼이 없어 닿지 못한다.

> ⚠️ **영도 외 시나리오에 `profile_creation`이 없다**
> ```
> 영도       10단계
> 무협       없음
> 다크판타지  없음
> 빈시나리오  없음
> ```
> 세 시나리오는 `start`가 안내만 하고 넘어간다. 캐릭터를 수동으로 만들어야 한다.

> ⚠️ **`cogs/character.py::manage_npc` 267줄**
> 거대 함수. NPC 조회·설정·삭제·목록을 한 함수가 처리한다.

> 🔁 **`step`이 단일 호출부**
> `runner.step`을 `creation_ui`만 부른다. 3계층 분리의 이점이 이 지점에서는 살지 않는다.

> ⚠️ **`profiles.search`가 이름 충돌**
> `tts.py:57`·`media.py:66`의 `search`와 이름이 같다. 전수 추적 시 혼동을 준다.
> (실제 동작에는 문제없다. 모듈이 다르다.)

---

## 확인 필요 목록

### 비용

- [ ] **프로필 AI 비용을 `total_cost`에 누적해야 합니까?** 무료 제공이라 차감은
      안 하더라도, 현재는 `!사용량`·`cost_log.txt` 어디에도 나타나지 않습니다.
      실제 API 비용은 발생합니다.

### 미사용 함수

- [ ] `validate_steps` — 시나리오 알고리즘 정합성 검사입니다.
      저작 시 쓸 수 있게 명령(`!검증` 같은)으로 노출해야 합니까?
- [ ] `jump_to` — 기획 필요기능 5번인데 UI 버튼이 없습니다. 붙여야 합니까?
- [ ] `branch_mode` — 조건 분기 모드로 보입니다. 무엇을 위한 것이었습니까?
- [ ] `reroll_stats` — UI가 `cancel_pending`으로 우회합니다. 제거해도 됩니까?

### 데이터

- [ ] 무협·다크판타지에 `profile_creation`을 만들 계획이 있습니까?
      현재는 세 시나리오가 수동 설정에 의존합니다.

### 구조

- [ ] `manage_npc`(267줄)를 분할해야 합니까?
- [ ] 3계층(gen → runner → ui) 분리가 의도대로 작동하고 있습니까?
      `runner.step`의 호출부가 하나뿐이라 중간 계층의 이점이 보이지 않습니다.
