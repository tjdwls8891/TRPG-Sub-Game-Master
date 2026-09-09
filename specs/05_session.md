# session — 기능 명세

- **모듈**: `core/session_flow.py`(689줄) · `core/creation.py`(176) · `core/session_open.py`(134) · `core/gm_space.py`(425) · `core/intro.py`(276)
- **연관 cogs**: `cogs/session.py`(658줄)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

> ⚠️ **데이터 오염 1건을 발견했습니다.** '발견 사항' 첫 항목을 보십시오.

---

## 개요

세션이 만들어져 첫 턴이 열리기까지를 담당한다. **플레이어가 가장 먼저 만나는 영역**이다.

| 모듈 | 담당 |
|---|---|
| `gm_space.py` | 서버 GM 홈. 오너 아닌 유저의 **유일한 진입점** |
| `creation.py` | 단계 상태 기계 — 순서·회귀·기록 |
| `session_flow.py` | 각 단계의 화면과 진행 |
| `intro.py` | 소개 데이터 — 인지 수준 3단계, 항목별 바리에이션 |
| `session_open.py` | 유지 시간 입력 해석 |
| `cogs/session.py` | 채널 생성, 캐시 업로드, 시작 상황, 인트로 |

---

## 설계 의도

### GM 스페이스가 유일한 진입점

명령어는 마스터 채널 전용이고 권한자만 쓸 수 있다. **스페이스가 사라지면 일반 유저는 세션을 열 방법이 없다.** 그래서 재시작 시 카테고리가 없으면 재생성한다. `[코드]` (5.16.1)

### 상태 기계로 만든 이유

> *"단계가 많고 회귀·취소가 필요하다. 각 단계를 (렌더, 입력 처리, 다음 단계 결정) 3종으로 정의하면 회귀는 history를 되감는 것으로 끝나고, 중간에 끊겨도 creation_state로 복원된다."* `[코드]`

### 되돌릴 수 없는 단계

> *"open은 캐시 업로드(비용 발생)를 수반하므로 되돌리면 재과금이 된다."* `[코드]`

`IRREVERSIBLE = {"open", "start", "done"}`

### 캐시 업로드를 프로필 뒤로

채널 생성과 동시에 캐시를 올리면 **캐릭터를 만들기도 전에 비용이 청구**되고, 중도 이탈 시 손실이 된다. 유지 시간도 그 시점엔 답을 받지 못했다. `[코드]` (5.3.0)

그래서 `build_session`을 `provision_session`(채널)과 `upload_cache`(캐시)로 분리했다.

### 시간은 코드가 정한다

> *"모델은 '어떤 유형의 답인가'만 분류한다. 실제 시간은 이 모듈이 고정표에서 꺼내거나 계산한다. 모델이 시간을 직접 정하면 같은 표현("적당히")에 매번 다른 값이 나온다."* `[코드]`

### 소개의 인지 수준 분기

> *"버튼식 케이스 트리 구조로 플레이어 인지 수준 및 설명 따라, 고인지 수준엔 동일 메시지 바리에이션 랜덤선택 재생, 첫 플레이면 스킵 선택지 안 주고 풀소개."* (기획 규정)

초심자에게는 **고정 문안**을 쓴다.
> *"처음 배우는 사람에게 표현이 매번 달라지면 오히려 혼란스럽다."* `[코드]`

### 화면은 메시지 하나를 고쳐 쓴다

> *"단계마다 새로 보내면 지난 UI가 남아 눌리고, 위아래로 스크롤해야 한다."* `[코드]` (5.21.0)

`creation_state["flow_msg_id"]`로 메시지를 추적한다.

---

## 데이터 구조

### 단계 정의 (`STEP_ORDER`)

| 단계 | 화면 | 담당 |
|---|---|---|
| `kind` | GM 홈 | 솔로/마스터 선택 |
| `private` | ✓ | 비공개 여부 |
| `intro` | ✓ | 공통 소개 |
| `scenario` | ✓ | 시나리오 선택 |
| `tts` | ✓ | 음성·목소리 |
| `memory` | ✓ | 기억 압축 방식 |
| `profile` | (위임) | 프로필 생성 |
| `open` | ✓ | 캐시 업로드 시간 |
| `start` | (위임) | 시작 상황 삼지선다 |
| `done` | — | 완료 |

`STEP_TITLES`에 있는 8단계만 진행도에 센다.

### `creation_state`

```python
{
  "step": "intro",
  "history": ["kind", "private"],   # 회귀용
  "data": {"kind": "솔로", ...},     # 단계별 선택 결과
  "flow_msg_id": 123456,            # 화면 메시지 (런타임 추가)
  "intro_level": "new"              # 소개 수준 (런타임 추가)
}
```

> `[확인 필요]` `flow_msg_id`·`intro_level`은 스키마에 없이 런타임에 추가된다. `creation_state`가 저장 대상이므로 디스크에도 남는다. 의도입니까?

### 유지 시간 상수 (`session_open.py`)

| 상수 | 값 | 근거 |
|---|---|---|
| `VAGUE_MINUTES` | short 60 / medium 180 / long 360 | *"표현마다 매번 다른 값이 나오지 않도록 고정"* `[코드]` |
| `MINUTES_PER_TURN` | 4 | *"실측 기반이며 세션 통계로 보정 가능"* `[코드]` |
| `TURN_BUFFER_RATIO` | 1.3 | *"예상보다 길어지는 경우를 대비"* `[코드]` |
| `RECOMMEND_MINUTES` | 180 | 판단을 맡긴 경우 |
| `MIN_MINUTES` | 10 | |
| `MAX_MINUTES` | 360 | `CACHE_TTL_SECONDS // 60` |
| `INTERPRET_CHARGE_THRESHOLD` | 2 | *"미만이면 청구하지 않는다"* `[코드]` |

### 소개 데이터 (`intro.py`)

**인지 수준 3단계**
```
LEVEL_NEW      통계 없음 — 건너뛰기 없이 전부
LEVEL_KNOWN    플레이 이력 있음
LEVEL_VETERAN  3세션↑ 또는 60턴↑ 또는 이 시나리오 경험
```

> *"세션 수와 턴 수를 함께 보는 이유는, 세션만 여러 번 열고 실제로 플레이하지 않은 경우를 걸러내기 위해서다."* `[코드]`

**항목 7종** — `trpg` · `turn` · `example` · `fail` · `world` · `cost` · `display`

각 항목은 `body`(초심자용 고정)와 `alts`(경험자용 변형 2종)를 갖는다.
`CHECKS`가 항목 끝의 확인 한마디와 다음 항목 연결 문구를 담는다.

### 세션 채널 구성

```
TRPG Session {id}/
├ game-{id}      플레이어 발언·묘사. default_role은 send_messages=False
├ master-{id}    GM 전용. default_role은 read_messages=False
├ display-{id}   상태판. send_messages=False
└ voice-{id}     음성. 참가자 인식에 쓰인다
```

---

## 기능 목록

### `creation.py` — 상태 기계

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `get_state` | 상태 조회·초기화 | 다수 |
| `current_step` | 현재 단계 | 4곳 |
| `is_done` | `step == "done"` | `display.py:515` |
| `record(key, value)` | 선택 기록 | 4곳 |
| `get_data(key)` | 기록 조회 | ⚠️ **호출부 없음** |
| `advance(*, to=None)` | 다음 단계. `to`로 건너뛰기 | 4곳 |
| `go_back()` | 회귀. `IRREVERSIBLE`이면 거부 | `profile_creation_ui.py:453` |
| `reset()` | 초기화 | ⚠️ **호출부 없음** |
| `progress_text()` | `A › **B** › ~~C~~` 형태 | ⚠️ **호출부 없음** |
| `summary()` | 선택 요약 | ⚠️ **호출부 없음** |
| `can_skip_profile_question(session, uid, sid)` | 사전 프로필 질문 생략 판정 | `profile_creation_ui.py:597` |

**`go_back`의 부작용** — *"되돌아간 단계의 선택은 무효화한다. 남겨두면 재선택 흔적이 섞인다."* `[코드]`

> `[확인 필요]` `progress_text`·`summary`가 5.21.0의 임베드 전환으로 죽었다. `step_embed`의 점 표시(`●●○○`)가 대신한다.

---

### `session_open.py` — 시간 해석

#### `resolve_minutes(result) -> dict`

**case별 처리**
```
unclear    재질문 (retry=True)
explicit   숫자 그대로. 하한·상한 적용
turns      턴수 × 4분 × 1.3
vague      short/medium/long 고정표
recommend  180분
```

**반환** — `{ok, minutes, case, notes, retry}`

**호출부** — `gm.py:2820`

#### `should_charge_interpretation(session) -> tuple`

누적 해석 비용이 2잉크 이상일 때만 청구한다.
**호출부** — `session.py:351` · `gm.py:671`

#### `format_confirmation(resolved, open_estimate) -> str`

확인 문구.
**호출부** — `gm.py:2763`

---

### `intro.py` — 소개 데이터

| 함수 | 하는 일 |
|---|---|
| `judge_level(user_stats, scenario_id)` | 인지 수준 3단계 판정 |
| `get_body(key, level)` | 본문. 초심자는 고정, 그 외 랜덤 |
| `get_label(key)` · `summary_of(key)` | 제목 · 한 줄 요약 |
| `greeting(level)` | 수준별 인사말 |
| `check_of(key)` · `lead_of(key)` | 확인 한마디 · 다음 연결 |
| `split_sections(body)` | 임베드 필드 분할 |
| `media_for(key, scenario_data)` | `intro_images` 조회 |

**전부 `session_flow.py`에서만 호출된다.**

> ⚠️ `media_for`가 `intro_images`를 읽는데 **어느 시나리오에도 이 키가 없다.** 연결부만 있고 자산이 없다.

---

### `gm_space.py` — GM 홈

#### `ensure_space(guild) -> dict`

카테고리와 채널 4종(홈·명전·서버보드·월드보드)을 보장한다. 없는 것만 만든다.

#### `refresh_home(bot, guild) -> bool`

홈 임베드를 갱신한다. **없으면 만든다.**
**호출부** — `system.py:668,687` · `main.py:142`(재시작 시)

#### `refresh_boards(bot, guild) -> bool`

명전·보드 갱신.
**호출부** — `gm_space.py:311` · `system.py:669,688` · `main.py`

#### `_begin_flow(bot, interaction, kind)`

**무엇을 하는가** — 채널을 만들고 17단계 플로우를 시작한다.

**주의점**
> *"시나리오는 아직 정하지 않았다. 기획 순서상 소개 이후에 고르므로, 임시로 첫 시나리오를 로드해 세션 객체만 만든 뒤 flow가 교체한다."* `[코드]`

> 🔴 **이 임시 시나리오가 오염을 남긴다.** '발견 사항' 참조.

#### 임베드 빌더 3종

`build_home_embed` · `build_hall_embed` · `build_board_embed`

---

### `session_flow.py` — 단계 화면

#### `step_embed(session, title, desc)`

진행도를 **푸터 한 곳에만** 점으로 표시한다.
```
●●●○○○○○   세션 준비 3 / 8
```

#### `show(bot, session, channel, embed, view=None, *, replace=True)`

`flow_msg_id`로 메시지를 추적해 고쳐 쓴다. 실패하면 새로 보낸다.

#### `close_flow_message(bot, session, channel, text=None)`

화면을 정리한다. **프로필·시작 상황처럼 별도 흐름이 이어지기 전에** 호출한다.

#### `render(bot, session, channel)`

단계별 화면을 그린다. 8단계 분기.

#### `advance_to(bot, session, channel, step=None)`

> ⚠️ **호출부 없음.** `_next`가 `creation.advance` + `render`를 직접 하는 방식으로 대체된 듯하다.

#### `on_profile_done` / `on_open_time_done`

외부 흐름이 끝났을 때 되돌아오는 진입점.

**`on_open_time_done`의 역할** (5.23.0에서 보강)
```
open_minutes 기록
upload_cache 호출
is_started = True
자동 GM 12개 필드 초기화
advance → start
```

#### 뷰 클래스

| 클래스 | 단계 |
|---|---|
| `PrivateView` | private |
| `IntroView` | intro (7항목 강의형) |
| `ScenarioView` · `ScenarioConfirmView` | scenario |
| `TTSView` · `VoiceView` | tts |
| `MemoryPlanView` | memory |
| `OpenTimeView` | open |

---

### `cogs/session.py`

#### `provision_session(guild, author, scenario_id, *, kind, private=False)`

채널 4종을 만들고 `TRPGSession`을 생성한다.

**부작용** — `TRPGSession.__init__`이 **시나리오의 `default_npcs`를 전개**한다.

#### `upload_cache(session, *, notify=None) -> bool`

룰북을 Gemini 캐시로 올린다. `open_minutes`로 TTL을 정한다.

#### `play_intro(session, chosen)`

인트로를 묘사층위로 생성하고 **자동 GM 첫 라운드를 띄운다**(5.23.0).

#### `StartFrameView`

시작 상황 삼지선다.

---

## 흐름

### 세션 생성 전 과정

```
GM 홈 [세션 열기]
  ↓
_begin_flow
  provision_session(scenarios[0])   ⚠️ 임시 시나리오
    채널 4종 생성
    TRPGSession(...)  → 임시 시나리오 NPC 전개
  creation.record("kind") + advance
  ↓
render(private) → PrivateView
  ↓ _next
render(intro) → IntroView (7항목)
  ↓
render(scenario) → ScenarioView → ScenarioConfirmView
  session.scenario_id / scenario_data 교체   ⚠️ npcs는 안 바뀜
  ↓
render(tts) → TTSView → VoiceView
  ↓
render(memory) → MemoryPlanView
  ↓
render(profile) → close_flow_message → profile_creation_ui.start
  ↓ on_profile_done
render(open) → OpenTimeView
  디스플레이 채널 1회 언락 → 유저 입력
  → resolve_minutes → OpenConfirmView
  ↓ on_open_time_done
  upload_cache · is_started=True · 자동 GM 초기화
  ↓
render(start) → StartFrameView
  ↓
play_intro → 묘사층위 → _init_narrative_and_start
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **profile** | session → profile | `profile_creation_ui.start` · `on_profile_done` 콜백 |
| **memory** | session → memory | `upload_cache` · `memory_plan.PLANS` |
| **cost** | session → cost | `estimate_session_open` · `accounts` · `stats` |
| **world** | session → world | `start_frame.offer` · `apply_facts` · `build_briefing` |
| **ai** | session → ai | `play_intro`가 `_execute_proceed` |
| **ui** | session → ui | `display.refresh` |
| **base** | session → base | `TRPGSession` · `save_session_data` · `get_available_scenarios` |

---

## 발견 사항

> 🔴 **임시 시나리오의 NPC가 세션에 남는다**
>
> `_begin_flow`가 `scenarios[0]`(정렬상 **다크판타지**)로 세션을 만든다.
> `TRPGSession.__init__`이 그 시나리오의 `default_npcs` 21명을
> `session.npcs`·`resources`로 전개한다.
>
> `ScenarioConfirmView`가 `scenario_id`·`scenario_data`만 교체하고
> **`npcs`·`resources`·`statuses`는 그대로 둔다.**
>
> **실측**
> ```
> 임시(다크판타지) 생성 → npcs 21명 (카르만·드라카·베른하르트…)
> 영도로 교체        → npcs 21명 그대로
> ```
>
> **영향 범위** — `session.npcs`를 읽는 곳
> ```
> extraction.py:280   valid |= session.npcs.keys()  ← 유효 캐릭터 검증
> prompt.py:96,109    NPC 델타 주입
> cache.py:138,259    캐시 조립
> irregular_npc.py:39 is_regular 판정
> utils.py:88         캐릭터 검색
> ```
>
> `build_extraction_limits`는 `scenario_data`를 읽어 영향이 없으나,
> `apply_extraction`의 `valid` 집합에는 다크판타지 인물이 들어간다.
> **추출층위가 '카르만'을 보고해도 통과한다.**
>
> 캐시 조립도 임시 시나리오 NPC를 델타 대상으로 본다. `[확인 필요]`

> ⚠️ **`creation` 미사용 함수 4종**
> `get_data` · `reset` · `progress_text` · `summary`.
> `progress_text`·`summary`는 5.21.0 임베드 전환으로 죽은 것으로 보인다.

> ⚠️ **`session_flow.advance_to` 호출부 없음**
> `_next`가 `creation.advance` + `render`를 직접 호출하는 방식으로 대체됐다.

> ⚠️ **`intro_images` 자산 없음**
> `media_for`가 이 키를 읽는데 어느 시나리오에도 없다. 연결부만 존재한다.

> ⚠️ **`flow_msg_id`·`intro_level`이 스키마 외 런타임 추가**
> `creation_state`가 저장 대상이므로 디스크에도 남는다.
> 봇 재시작 후 `flow_msg_id`가 가리키는 메시지가 없으면 `show`가 새로 보낸다(방어됨).

> 🔁 **`render`가 23곳에서 호출된다**
> 각 뷰의 `_next`가 개별 호출한다.

> ⚠️ **`빈시나리오`가 선택지에 노출된다**
> `get_available_scenarios`가 `scenario.example`만 제외한다.
> `빈시나리오.json`은 템플릿으로 보이는데 플레이어가 고를 수 있다. `[확인 필요]`

---

## 확인 필요 목록

### 🔴 최우선 — 데이터 오염

- [ ] **임시 시나리오 NPC가 세션에 남습니다.** `_begin_flow`가 `scenarios[0]`로
      세션을 만들고, 시나리오 교체 시 `npcs`·`resources`·`statuses`가 정리되지 않습니다.
      영도 세션에 다크판타지 인물 21명이 남아 추출층위 유효 검증과 캐시 조립에
      섞입니다.
      `ScenarioConfirmView`에서 NPC를 다시 전개해야 합니까?
- [ ] 아니면 `_begin_flow`가 세션 객체 생성을 시나리오 선택 이후로 미뤄야 합니까?
      다만 채널은 먼저 만들어야 플로우를 띄울 수 있습니다.

### 정리

- [ ] `creation.get_data`·`reset`·`progress_text`·`summary` 호출부가 없습니다.
      `progress_text`·`summary`는 5.21.0 임베드 전환으로 죽은 것 같습니다. 제거해도 됩니까?
- [ ] `session_flow.advance_to` 호출부가 없습니다.
- [ ] `빈시나리오`를 선택지에서 제외해야 합니까? 템플릿으로 보입니다.

### 설계 의도

- [ ] `flow_msg_id`·`intro_level`을 `creation_state`에 런타임 추가하는 것이 의도입니까?
      저장 대상이라 디스크에도 남습니다.
- [ ] `intro_images` 자산을 준비할 계획이 있습니까?
- [ ] `MINUTES_PER_TURN = 4`가 *"세션 통계로 보정 가능"*이라 되어 있는데
      실제 보정 경로가 없습니다. 만들어야 합니까?
