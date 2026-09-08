# world — 기능 명세

- **모듈**: `core/places.py` · `core/timeline.py` · `core/start_frame.py` · `core/irregular_npc.py` · `core/growth.py` · `core/koreantext.py`
- **연관 cogs**: `cogs/gm.py`(비정규 NPC 해석·성장 판정) · `cogs/session.py`(시작 상황)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

---

## 개요

세계가 어떻게 생겼고 시간이 어떻게 흐르며 그 안의 인물이 어떻게 다뤄지는지를 담당한다.

| 모듈 | 담당 |
|---|---|
| `places.py` | 장소 그래프. 이동 개연성·가시성·이미지 상속 |
| `timeline.py` | 작중 시간 정량화, 나이 계산 |
| `start_frame.py` | 시작 상황 틀 — 세션 첫 장면 |
| `irregular_npc.py` | 즉석 등장 인물의 이미지·목소리 유지 |
| `growth.py` | 능력치 성장·행운 |
| `koreantext.py` | 슬롯 치환·조사 보정 |

**경계** — 이 영역은 데이터를 해석해 **텍스트나 판정 결과**를 낸다. AI를 직접 호출하지 않는다(예외: `gm.py`의 비정규 NPC 상세 생성).

`koreantext.py`는 언어 유틸이지만 **틀·퀘스트의 슬롯 치환 전용**이라 여기 둔 것으로 보인다. `[추정]`

---

## 설계 의도

### 계층은 틀이 아니라 그래프

> *"tier 같은 고정 계층을 두지 않는다. 각 장소는 parent 하나만 가리키고 깊이는 그 결과일 뿐이다. 섬서 아래 화음(깊이 3)과 종남산(깊이 2)이 대등하게 존재할 수 있으며, 낄 계층이 없으면 없는 대로 둔다. 최소단위는 하위 존재 여부로 자동 판정된다."* `[코드]`

시나리오가 계층 틀에 맞춰지지 않아도 된다.

### 이동은 차단이 아니라 거리 인식

> *"connected 직결. 문 하나 사이. / reachable 한 턴에 이동해도 어색하지 않은 범위. 미지정이면 connected를 3칸 확장해 자동 생성한다. / 그 밖의 목적지는 ASK로 안내한다. 차단이 아니라 거리 인식이다."* `[코드]`

멀리 가겠다고 선언해도 막지 않는다. **경로와 소요를 알려주고 출발할지 묻는다.**

### 판단층위가 세계관을 모르므로 코드가 계산한다

> *"판단층위는 캐시를 읽지 않아 세계관을 모른다. 코드가 경로를 계산해 사용자 프롬프트에 직접 넣어야 한다."* `[코드]` (`build_move_hint`)

4층위 설계의 부작용을 여기서 메운다.

### 나이는 코드가 계산한다

> *"LLM은 뺄셈에 비교적 강하지만 음수 부호를 누락하는 실패가 잦고, 다수 NPC의 상대 나이·항렬을 매 턴 파생시키면 오차가 예법 오류로 연쇄된다. 출생년도를 저장하고 나이는 코드가 계산해 주입한다."* `[코드]`

무협에서 항렬을 잘못 계산하면 예법 전체가 무너진다.

### 시간을 정수로 환산하는 이유

> *"추출층위는 날짜·시간대를 문자열로 산출한다("1204/05/21", "오후"). 문자열만으로는 경과 일수 비교·퀘스트 시간 조건 판정이 불가능하므로 정수로 환산해 함께 보관한다. 단위는 일(day) / 24시간 기준으로 확정되었다."* `[코드]`

### 비정규 NPC를 기록하는 이유

> *"묘사층위가 즉석에서 만들어낸 인물('낡은 외투의 사내')은 시나리오의 NPC 사전에도, 이미지 목록에도 없다. 매번 다른 이미지·목소리가 붙으면 같은 인물이 다른 사람처럼 보인다."* `[코드]`

**정규 NPC의 얼굴은 후보에서 제외**한다. 혼동을 막기 위함이다. `[코드]`

---

## 데이터 구조

### 장소 노드 (시나리오 JSON `places`)

| 필드 | 타입 | 용도 | 없으면 |
|---|---|---|---|
| `parent` | str | 상위 하나 | 최상위 |
| `extra_parents` | list | 복수 소속 | — |
| `connected` | list | 직결. 문 하나 사이 | 상하위만 |
| `reachable` | list | 한 턴 이동 범위 | `connected` 3홉 확장 |
| `visible_within` | str/list | 이 상위 안에 들어가야 인지 가능 | 항상 보임 |
| `inherit` | list | 풀 정보를 함께 주입할 상위 | 주입 안 함 |
| `traversable` | bool | 범위 노드라도 통로로 쓸 수 있음 | — |
| `aliases` | list | 별칭. `resolve`가 쓴다 | — |
| `image` | str | 장소 이미지 | 상위에서 상속 |
| `location_desc` | str | 위치 설명 | — |
| `exterior` / `interior` | str | 외부·내부 묘사 | — |
| `mood_exterior` / `mood_interior` | str | 분위기 | — |
| `known_brief` | str | 방문한 곳의 짧은 설명 | `location_desc` |
| `unknown_hint` | str | 미방문 힌트 | "아직 가보지 않은 곳이다." |
| `npcs` | list | `[{name, relation, frequency}]` | — |

**`frequency`가 `"상주"`인 NPC**는 장소를 옮기면 동행에서 자동 해제된다(ai 영역 `release_resident_companions`).

### 상수

| 상수 | 값 | 의미 |
|---|---|---|
| `REACHABLE_HOPS` | 3 | `reachable` 자동 확장 폭. *"지시 확정"* `[코드]` |
| `MAX_ROUTE_HOPS` | 40 | 경로 탐색 상한 |
| `TIME_OF_DAY_HOUR` | 10종 | 새벽4·아침7·오전10·정오12·낮13·오후15·저녁18·밤21·심야1·자정0 |
| `DEFAULT_HOUR` | 12 | 시간대 판독 실패 시 |
| `VOICE_POOL` | 6종 | (성별×연령) → prebuilt voice |
| `CHOICE_COUNT` | (start_frame) | 시작 상황 제시 개수 |

### 세션 필드

| 필드 | 이 영역의 용도 | 되감기 |
|---|---|---|
| `visited_places` | 방문 기록 | ✓ |
| `world_timeline.current_location` | 현재 위치 | ✓ |
| `irregular_npcs` | 비정규 NPC 등록부 | ✗ |
| `stat_fail_counts` | 스탯별 실패 누적 | ✗ |
| `start_frame` | 선택된 시작 틀 | ✗ |
| `start_day_number` | 시작 시점 통산 일수 | ✗ |

> `[확인 필요]` `irregular_npcs`·`stat_fail_counts`가 되감기 대상이 아니다. 되감아도 비정규 NPC 등록과 실패 누적은 남는다. 의도입니까?

### 시나리오 JSON 키

| 키 | 영역 | 용도 |
|---|---|---|
| `places` | places | 장소 그래프 |
| `calendar` | timeline | 달력 정의 (일/월, 월/년) |
| `start_frames` | start_frame | 시작 상황 틀 |
| `briefing_formats` | start_frame | 브리핑 양식 |
| `growth` | growth | 성장 규칙 |
| `luck` | growth | 행운 규칙 |
| `media_keywords` | irregular_npc | 정규 이미지 배제 기준 |

---

## 기능 목록

### `places.py` — 장소 그래프

#### 그래프 기본

| 함수 | 하는 일 | 호출부 |
|---|---|---|
| `load_places(scenario_data)` | 장소 사전 반환 | 9곳 |
| `get(places, name)` | 노드 조회 | 내부 다수 |
| `parents_of(places, name)` | `parent` + `extra_parents` | 내부 3 |
| `path_of(places, name)` | 최상위→자신 경로. **순환 방어** | 내부 3 + `quest_filter` |
| `children_of(places, name)` | 직속 하위 | 내부 3 |
| `is_leaf(places, name)` | 최소단위인지 | ⚠️ **호출부 없음** |

#### `resolve(places, text) -> str | None`

**무엇을 하는가** — 이름·별칭·부분 일치로 장소를 특정한다.

**왜 있는가** — *"추출층위나 플레이어가 정확한 이름을 쓰지 않을 수 있다."* `[코드]`

**우선순위** — 정확 일치 → `aliases` → **부분 일치(후보가 하나일 때만)**

**호출부** — 8곳 (`quest_filter`·`media`·`places` 내부)

#### `is_container(places, name) -> bool`

**무엇을 하는가** — 이동 대상이 아닌 상위 개념인지 판정한다.

**왜 있는가**
> *"'영도' 같은 최상위는 장소가 아니라 범위다. 이동 경로로 쓰면 중리에서 조도로 갈 때 해안로와 방파제를 건너뛰는 지름길이 생긴다."* `[코드]`

**판정** — `traversable`이면 아님. 아니면 `connected`가 없고 하위가 있으면 범위.

#### `_neighbors(places, name) -> list`

이동 가능한 인접 노드. **범위 노드는 통로가 되지 않는다.** 상위로 갈 때 `is_container`를 검사한다.

#### `is_visible_from(places, target, current) -> bool`

**왜 있는가**
> *"해련 마을에서 남항의 '제1방어선'까지 보이는 것은 이상하다. 거리로는 닿아도 남항 안에 들어가야 인지할 수 있는 것들이 있다."* `[코드]`

`visible_within`이 없으면 항상 `True`.

#### `reachable_from(places, name, hops=3) -> list`

명시 `reachable`이 있으면 그것을 쓰고, 없으면 BFS로 3홉 확장한다.

#### `route(places, start, goal) -> list`

BFS 최단 경로. `MAX_ROUTE_HOPS`(40) 상한.

#### `can_move(places, start, goal) -> bool`

`goal in reachable_from(start)`. 같은 곳이면 `True`.

#### 방문 기록

`visited` · `is_visited` · `mark_visited`.
`mark_visited`만 외부 호출(`gm.py:3193` — 추출 후 위치 갱신 시).

#### `image_for(places, name) -> str | None`

**상속 규칙** — *"각 장소마다 사용할 이미지를 명시하고, 없으면 상위 항목 중 이미지가 존재하는 것들에서 가장 하위 항목의 것을 사용한다."* `[코드]` (지시 확정)

`path_of`를 역순으로 훑어 첫 번째 `image`를 쓴다.

**호출부** — `media.py:43`

#### `format_name(places, name, current=None) -> str`

**규칙** — *"전체 경로를 나열하지 않는다. 같은 상위 안에서는 최소단위만, 상위가 다르면 직결 상위를 붙인다."* `[코드]`

#### `build_place_block(session) -> str`

**무엇을 하는가** — 지시·묘사층위에 주입할 장소 정보를 만든다.

**구성**
```
[현재 장소]      풀 정보 (위치·외부·내부·분위기·연관 인물 8명)
동행             companions
[상위 맥락 — X]  inherit에 명시된 것만 풀 정보
[갈 수 있는 곳]  reachable ∩ 가시 범위, 홉 거리 순, 최대 8곳
                 방문함 → known_brief / 미방문 → unknown_hint
                 "미방문 장소는 확정적으로 묘사하지 말 것"
```

**설계 판단**
> *"상위 맥락 — 저작자가 명시한 것만 (전부 주입하면 토큰이 폭증한다)"* `[코드]`
> *"갈 수 있는 곳 — 미리 얕게 주입해 이동 묘사의 재료를 준다. 추출층위가 장소를 바꾼 뒤에야 정보가 오면 이동을 묘사할 수 없다."* `[코드]`
> *"가까운 곳을 앞에 둔다. 목록이 잘릴 때 자기 하위나 직결이 먼 거점보다 뒤로 밀리면 이상하다."* `[코드]`

**호출부** — `prompt.py:223`(묘사층위) · `gm.py:504`(지시층위)

> **5.30.0에서 수정됨.** 이전에는 `self.parts` 오타로 묘사층위에 주입되지 않았고, 지시층위에는 호출 자체가 없었다.

**분량** — 영도 청학동 선착장 기준 430자 ≈ 141토큰.

#### `build_move_hint(session, goal_text) -> str`

**무엇을 하는가** — ASK용 이동 안내 재료를 만든다.

**세 가지 경우**
```
목적지 불명       갈 수 있는 곳 제시 + "특정 방향을 권하지 말 것"
한 턴에 가능       빈 문자열 (안내 불필요)
경로 없음         "길을 알지 못한다는 점을 알리고 되물을 것"
한 턴에 불가       경로 나열 + "출발할지 물을 것"
                  "거리와 소요는 경로의 길이에 맞춰 맥락에 어울리게"
```

**호출부** — `gm.py:182` (판단층위 프롬프트 조립)

#### `hops_between(session, start, goal) -> int`

경로 길이. **시간 경과 산출에 쓴다.** `[코드]`
**호출부** — `gm.py:3203`

---

### `timeline.py` — 작중 시간

#### `parse_date(date_str) -> tuple | None`

날짜 문자열을 `(년, 월, 일)`로 파싱한다.

#### `to_day_number(date_str, days_per_month=30, months_per_year=12) -> int`

통산 일수로 환산한다. 경과 일수 비교의 기준.

#### `hour_of(time_of_day) -> int`

시간대 문자열 → 시(hour). `TIME_OF_DAY_HOUR` 매핑, 실패 시 `DEFAULT_HOUR`(12).

#### `get_calendar(session) -> dict`

시나리오의 달력 정의. 없으면 기본(30일/월, 12월/년). `[추정]`

#### `quantify(session, timeline) -> dict`

**무엇을 하는가** — 타임라인의 문자열 날짜·시간대를 정수로 환산해 함께 보관한다.

**호출부** — `gm.py:3209` (추출 후)

#### `current_year(session) -> int | None`

작중 현재 연도.
**호출부** — `timeline.py:133`(내부) · `gm.py:2840`

#### `compute_age(session, birth_year) -> int | None`

**무엇을 하는가** — 출생년도에서 현재 나이를 계산한다.

**호출부** — `prompt.py:98`(NPC 나이 주입) · `timeline.py:157` · `gm.py:2924`

#### `enrich_npc_ages(session, npcs) -> dict`

> ⚠️ **호출부 없음.** NPC 사전에 나이를 일괄 채우는 함수로 보이나 쓰이지 않는다.
> `prompt.py`가 `compute_age`를 직접 반복 호출하는 방식으로 대체된 듯하다. `[추정]`

#### `age_gap(session, birth_year_a, birth_year_b) -> int | None`

> ⚠️ **호출부 없음.** 항렬·연배 판정에 쓰려던 것으로 보인다. `[추정]`
> **영도의 NPC 47명 모두 `birth_year`가 없어** 나이 기능 자체가 작동하지 않는다.

#### `format_timeline(timeline) -> str`

디스플레이 표기용.
**호출부** — `display.py:57`

---

### `start_frame.py` — 시작 상황

#### `get_frames(scenario_data) -> list`

시나리오의 `start_frames`.

#### `_matches(profile, cond) -> bool` / `_stat_of(profile, field)`

프로필이 틀의 조건에 맞는지. 스탯 값을 꺼낸다.

#### `filter_frames(scenario_data, profile) -> list`

프로필에 맞는 틀만 거른다.

#### `_fill_slot(scenario_data, profile, spec) -> str`

슬롯 하나를 채운다. **5축 슬롯**(시간대·위치·인물·위협·발견물).

#### `realize(scenario_data, profile, frame) -> dict`

틀의 슬롯을 실제 값으로 채워 확정한다. `koreantext.substitute`를 쓴다.

#### `offer(scenario_data, profile, *, count=CHOICE_COUNT) -> list`

**무엇을 하는가** — 후보를 걸러 랜덤으로 뽑고 실체화한다.

**호출부** — `session_flow.py:625` · `session.py:487`

#### `get_briefings(scenario_data) -> list` / `build_briefing(scenario_data, profile) -> str`

브리핑 양식을 골라 채운다. 영도는 5종.

#### `build_intro_instruction(scenario_data, profile, chosen) -> str`

**무엇을 하는가** — 인트로 묘사를 위한 지시문을 만든다. 묘사층위로 전달된다.

**호출부** — `session.py:554`

#### `apply_facts(session, chosen)`

**무엇을 하는가** — 선택된 틀의 사전 확정 정보를 세션에 반영한다(위치·시간 등).

**호출부** — `session.py:115`

#### `format_choice(index, chosen) -> str`

선택지 표기.
**호출부** — `session.py:491`

> `[확인 필요]` 5.21.0에서 시작 상황을 임베드 필드로 바꿨는데 `format_choice`가 아직 쓰인다. 두 표기가 공존합니까?

---

### `irregular_npc.py` — 비정규 NPC

#### `get_registry(session) -> dict`

등록부. **13곳에서 호출**된다.

#### `is_regular(session, name) -> bool`

정규 NPC인지. `session.npcs` 키 확인.

#### `has_image(session, name) -> bool`

정규 이미지가 있는지. `media_keywords` 또는 `{이름}.png`.

#### `regular_image_keys(session) -> set`

**왜 있는가** — *"정규 NPC가 쓰는 이미지 키 집합. 비정규 후보에서 배제하기 위함이다."* `[코드]`

#### `irregular_image_pool(session) -> list`

비정규 후보 이미지 목록. 정규 것을 뺀 나머지.
**호출부** — `gm.py:2957,3023`

#### `needs_resolution(session, names) -> list`

이미지·목소리 결정이 필요한 인물을 추린다. 이미 등록됐거나 정규면 제외.

#### `pick_voice(gender=None, age=None) -> str`

`VOICE_POOL`에서 (성별, 연령대)로 목소리를 고른다. 기본 `male/adult`.

#### `extract_candidate_names(text, session, limit=6) -> list`

묘사문에서 인물 후보 이름을 뽑는다.
**호출부** — `gm.py:2953`

#### `register(session, name, *, image_key="", gender=None, age=None)`

등록부에 기록한다. **한 번 결정한 것을 재사용**하기 위함.
**호출부** — `gm.py:3034`

#### `note_appearance(session, name, turn) -> int` / `should_promote(session, name, *, named=False) -> bool`

등장 횟수를 세고 정규 승격 여부를 판정한다.
**호출부** — `gm.py:3048,3049`

#### `mark_detailed(session, name)` / `promote(session, name, details) -> bool`

상세 생성 완료 표시 · 정규 NPC로 승격.
**호출부** — `gm.py:2913,2914`

#### `image_path_for(session, name) -> str | None`

배정된 이미지 경로.
**호출부** — `dialogue.py:333`

#### `voice_for(session, name) -> str | None`

> ⚠️ **호출부 없음.** 등록부에서 목소리를 꺼내는 함수인데 쓰이지 않는다.
>
> `game.py:1001`에서 `voice_name = None`으로 두고 기본 나레이터 보이스를 쓴다. **비정규 NPC에게 배정한 목소리가 실제 TTS에 반영되지 않는다.**
>
> `register`가 목소리를 저장하고 `pick_voice`가 고르는데, 꺼내 쓰는 쪽이 없어 **전체 사슬이 끊겨 있다.** `[확인 필요]`

---

### `growth.py` — 성장·행운

#### `get_growth_config(session) -> dict` / `get_luck_config(session) -> dict`

시나리오의 `growth`·`luck` 설정. 없으면 기본값.

#### `_fail_counts(session) -> dict`

`session.stat_fail_counts`.

#### `_stat_value(session, char_name, stat_name)` / `_bump_stat(session, char_name, stat_name, delta=1)`

스탯 읽기·올리기.

#### `check_growth(session, char_name, stat_name, sides=20) -> dict | None`

**무엇을 하는가** — 판정 실패 누적으로 성장 여부를 판정한다.

**설계** — 같은 능력치로 거듭 실패하면 그 능력치가 자란다. 소개 문안에도 나오는 규칙이다.

#### `check_luck(session, char_name, *, failed) -> dict | None`

**무엇을 하는가** — 실패한 순간 행운이 개입할지 판정한다.

#### `process_roll_outcome(session, char_name, stat_name, ...) -> dict`

성장·행운을 함께 처리하는 파사드.
**호출부** — `gm.py:2325`

#### `format_growth` / `format_luck`

결과 문구.
**호출부** — `gm.py:2331,2343`

---

### `koreantext.py` — 슬롯·조사

#### `has_batchim(ch) -> bool` / `is_rieul(ch) -> bool`

받침 유무 · ㄹ 받침 판정.

#### `pick_josa(word, with_batchim, without_batchim) -> str`

받침에 따라 조사를 고른다. `은/는`, `이/가`, `을/를`.

#### `attach(word, josa) -> str`

단어에 조사를 붙인다.

#### `fix_josa_at(text, pos) -> str`

치환 후 조사를 보정한다.

#### `substitute(text, slots, *, joiner=", ") -> str`

**무엇을 하는가** — `{슬롯}`을 값으로 치환하고 **조사를 자동 보정**한다.

**왜 있는가** — 퀘스트·시작 틀이 `{기록자}가`처럼 슬롯 뒤에 조사를 붙인다. `엄주섭가`가 되면 안 된다.

**호출부** — `quest.py:194` · `start_frame.py:131,185`

#### `strip_unfilled(text) -> str`

채워지지 않은 `{슬롯}`을 제거한다.
**호출부** — `quest.py:194`

---

## 흐름

### 턴 진행 중 장소의 역할

```
플레이어 "북쪽 조도로 간다"
  ↓
gm.py:182  build_move_hint(session, "조도")
             route로 경로 계산 → 5홉
             can_move False → 안내 텍스트 생성
  ↓
판단층위 프롬프트에 [이동 안내] 포함 → ASK 판정
  ↓
GM: "조도까지는 해안로와 방파제를 거쳐야 합니다. 출발하시겠습니까?"
```

### 세션 시작 시

```
프로필 완성
  ↓
start_frame.offer(scenario_data, profile)
    filter_frames  프로필 조건에 맞는 틀
    랜덤 3개 추출
    realize        슬롯 채움 (koreantext.substitute)
  ↓
플레이어 선택
  ↓
apply_facts        위치·시간을 세션에 반영
build_briefing     직업 기준 브리핑
build_intro_instruction → 묘사층위
```

### 비정규 NPC 사슬

```
묘사 출력
  ↓
extract_candidate_names  후보 이름 추출
needs_resolution         결정이 필요한 것만
irregular_image_pool     정규 제외 후보
pick_voice               목소리 결정
register                 등록부 기록
  ↓
다음 등장 시
  image_path_for → dialogue.py가 이미지 전송  ✓
  voice_for      → ⚠️ 아무도 부르지 않음
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **ai** | ai → world | `build_place_block`·`compute_age`·`image_path_for`·`release_resident_companions`가 `load_places` |
| **quest** | quest → world | `quest_filter`가 `load_places`·`resolve`·`path_of` · `quest`가 `substitute` |
| **session** | session → world | `start_frame.offer`·`apply_facts`·`build_briefing` |
| **media** | media → world | `image_for`·`resolve` |
| **ui** | ui → world | `display`가 `format_timeline` |
| **base** | world → base | `growth`가 `get_uid_by_char_name` |

---

## 발견 사항

> ⚠️ **`voice_for` 호출부 없음 — 인물별 목소리 전체가 단절**
>
> ```python
> def voice_for(session, name):
>     """해당 인물의 목소리. 정규 NPC는 npcs의 voice 항목을 본다."""
>     # 정규 NPC의 npcs[name]["voice"] → 없으면 등록부 조회
> ```
>
> **비정규 NPC뿐 아니라 정규 NPC의 `voice` 필드도 이 함수로만 읽힌다.**
> 호출부가 없으므로 시나리오가 NPC마다 목소리를 지정해도 반영되지 않는다.
>
> `game.py:1001`의 `voice_name`은 `!더빙테스트`의 인자를 받는 변수이며,
> 턴 진행 시에는 `_synthesize_and_enqueue(voice_name=None)`으로 호출되어
> 기본 나레이터 보이스만 쓴다.
>
> 이미지는 `image_path_for`가 `dialogue.py:333`에 연결돼 있어 정상이다.
> **같은 사슬인데 이미지는 이어지고 목소리는 끊겼다.**

> ⚠️ **`enrich_npc_ages` 호출부 없음**
> `prompt.py`가 `compute_age`를 직접 반복 호출하는 방식으로 대체된 듯하다.

> ⚠️ **`age_gap` 호출부 없음**
> 항렬·연배 판정용으로 보이나 쓰이지 않는다.

> ⚠️ **`is_leaf` 호출부 없음**
> 최소단위 판정. 주석은 *"최소단위는 하위 존재 여부로 자동 판정된다"*고 하는데 실제로 판정하는 곳이 없다.

> ⚠️ **영도 NPC 47명 전원 `birth_year` 없음**
> 나이 계산 기능(`compute_age`·`enrich_npc_ages`·`age_gap`)이 데이터 부재로 작동하지 않는다.
> `prompt.py`의 나이 주입 블록도 항상 비어 있다.

> ⚠️ **`irregular_npcs`·`stat_fail_counts`가 되감기 비추적**
> 되감아도 비정규 NPC 등록과 실패 누적이 남는다.

> 🔁 **`load_places`가 매번 다시 읽힌다**
> 9곳에서 각자 `scenario_data["places"]`를 꺼낸다. dict 반환이라 비용은 작으나, 파생 계산(`children_of`·`route`)은 캐시가 없어 반복된다.

> 🔁 **`_hop_distance`가 `route`를 전부 다시 계산**
> `build_place_block`의 정렬에서 `near` 항목마다 BFS를 돌린다. 8곳이면 8회.

> ⚠️ **`format_choice` 잔존 가능성**
> 5.21.0에서 시작 상황을 임베드로 바꿨는데 이 함수가 아직 `session.py:491`에서 쓰인다.

---

## 확인 필요 목록

### 기능 단절

- [ ] **`voice_for`를 TTS에 연결해야 합니까?** 비정규 NPC에게 목소리를 배정하는 사슬이 만들어져 있는데 마지막 고리가 없습니다. `game.py`가 화자별로 `voice_for`를 조회하도록 고치면 됩니다.
- [ ] **NPC `birth_year`를 채울 계획이 있습니까?** 영도 47명 전원이 비어 나이 기능 전체가 유휴 상태입니다.

### 미사용 함수

- [ ] `enrich_npc_ages` — `prompt.py`의 직접 호출로 대체된 것이 맞습니까? 제거해도 됩니까?
- [ ] `age_gap` — 항렬 판정에 쓰려던 것입니까? 무협 시나리오용입니까?
- [ ] `is_leaf` — 최소단위 판정을 어디서 쓰려 했습니까?

### 설계 의도

- [ ] `irregular_npcs`·`stat_fail_counts`를 되감기 추적에서 뺀 것이 의도입니까?
- [ ] `REACHABLE_HOPS = 3`이 *"지시 확정"*이라 되어 있는데, 시나리오별로 조정할 필요가 있습니까?
- [ ] `build_place_block`의 `[갈 수 있는 곳]` 8개 상한이 적절합니까? 영도는 장소가 122개라 잘리는 경우가 잦을 수 있습니다.

### 정리

- [ ] `format_choice`가 5.21.0 임베드 전환 후에도 쓰입니다. 두 표기가 공존해야 합니까?
- [ ] `_hop_distance`가 정렬마다 BFS를 반복합니다. 캐시를 두어야 합니까?
