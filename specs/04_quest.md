# quest — 기능 명세

- **모듈**: `core/quest.py`(750줄) · `core/quest_filter.py`(326줄)
- **연관 cogs**: `cogs/gm.py` (`!자동 퀘스트`, 층위 연동)
- **기준 버전**: v5.30.0
- **작성 상태**: 완료 (미검증 — 사용자 확인 대기)

> ⚠️ **중복 구현 1건을 발견했습니다.** 5.24.0에서 제가 만든 `_apply_quest_choice`가 이미 완비돼 있던 `apply_choice` 경로와 겹칩니다. '발견 사항' 첫 항목을 보십시오.

---

## 개요

기본 서사 설계 구조다. 기획서가 *"기본 서사 설계 구조로 퀘스트 시스템 채택"*이라 규정했다.

| 모듈 | 담당 |
|---|---|
| `quest.py` | 데이터 로드, 상태 관리, 트리 진행, 이면정보, 소속 부여, 메인 해금, 인피니티 플랜 |
| `quest_filter.py` | 필터 매칭 — **통과한 값이 곧 슬롯이 된다** |

**경계** — 이 영역은 *어떤 사건이 언제 어떻게 진행되는가*를 정한다. 실제 묘사는 ai 영역이 한다.

---

## 설계 의도

### 온디맨드 주입 — 캐시에 굽지 않는다

> *"퀘스트 틀 전량을 캐시에 구우면 시나리오 10만 토큰 목표와 충돌한다. 폐지된 키워드북이 쓰던 온디맨드 슬롯을 퀘스트가 대체한다."* `[코드]`

```
퀘스트 없음    → 필터링된 '가능 퀘스트 목록'을 주입, 지시층위가 선택
퀘스트 진행 중 → 선택된 퀘스트 하나만 주입 (토큰 절감)
```

### 중복 차단은 이름으로만

> *"같은 사건에 다른 이면정보를 가진 버전을 여럿 두되, 중복 발생 차단은 name 기준이다(기획 규정). 버전이 달라도 같은 이름이면 반복되지 않는다."* `[코드]`

`filter_available`이 이름별로 묶어 **버전 하나만 후보에 올린다.**
> *"이름 기준 중복 차단이므로 버전끼리 경쟁시킬 이유가 없다."* `[코드]`

### 선택할 수 없는 상황에서는 선택지를 주지 않는다

> *"기획 확정 사항 — 진행 중인 퀘스트가 없거나 전환할 수 있게 된 경우에만 선택하게 한다. 선택할 수 없는 상황에서는 아예 선택지를 주지 않아 잘못된 판단이 구조적으로 불가능하게 만든다."* `[코드]`

3-상황 모델(`CTX_ACTIVE`/`CTX_NONE`/`CTX_SWITCH`)이 이를 구현한다.

### 필터가 곧 슬롯

후보를 나열해 두면 일치한 값이 빈칸을 채운다.

```json
"npc": {"any": ["엄주섭","차봉순","황기영"], "as": "기록자"}
```

장부방 → 엄주섭, 지하 병참 창고 → 차봉순. **하나의 틀이 여러 곳에서 각기 다른 사건이 된다.**

### 맥락 무시 방지

> *"'context:location' 처럼 지정하면 추출층위가 갱신한 현재 위치를 쓴다. 맥락을 무시하고 무작위로 뽑으면 '저지대에 있는데 다른 동네로 가라'는 퀘스트가 나온다."* `[코드]`

### 판정 권한을 모델에 넘기지 않는다

> *"지시층위는 '어느 방향으로 이끌지'만 답한다. 실제 진전 여부는 추출층위 수치가 임계를 넘을 때만 일어나므로 판정 권한은 넘기지 않는다."* `[코드]` (`case_schema`)

### 모델 응답을 그대로 믿지 않는다

> *"선택 불가 상황(A)인데 값이 오면 무시 / 제시하지 않은 id면 무시 / 전환 시 진행 중이던 퀘스트는 abandoned로 기록"* `[코드]` (`apply_choice`)

---

## 데이터 구조

### 퀘스트 파일 (`scenarios/{id}.quests.json`)

```json
{
  "quests": [
    {
      "id": "early_first_water",
      "name": "마실 것",
      "version": "a",
      "line": "sub" | "main",
      "groups": ["초반군"],
      "scale": "소품" | "조우" | "사건" | "전환" | "엔드",
      "filters": {...},
      "repeatable": false,
      "max_occurrences": 1,
      "tree": {"root": {...}, ...},
      "hidden": {...},
      "grants": {...}
    }
  ]
}
```

### 필터 (`filters`)

| 키 | 형태 | 의미 |
|---|---|---|
| `npc` | dict 또는 list | 인물. `{any/all/none, as, scope}` |
| `place` | dict/list | 정확 일치 장소 |
| `within` | dict/list | 상위 경로 포함 |
| `pair` | — | 인물+장소 동시 성립 (전속 퀘스트) |
| `faction` | dict | 소속 |
| `faction_scope` | — | 세력 영역 |
| `item` · `info` | dict | 소지품 · 인지 정보 |
| `time_of_day` | dict | 시간대 |
| `min_stat` | dict | 능력치 하한 |
| `min_turn` | int | 턴 하한 |
| `max_turn` | int | 턴 상한 |
| `min_cleared_sub` | int | 서브 클리어 수 |
| `min_outcome` | dict | `{"deviated": 2}` — 결말 이력 |
| `min_visited` | int | 방문 장소 수 |
| `blocks_if_cleared` | list | 이미 클리어했으면 배제 |
| `requires` | list | 선행 퀘스트 |

**연산자** `[코드]`
```
any   하나만 일치하면 통과 (기본)
all   전부 있어야 통과
none  하나라도 있으면 탈락
```

**범위(scope)** `[코드]`
```
here       현재 장소 상주자 (기본)
companion  동행 중인 인물
any        둘 다
```

**`as`** — 통과한 값을 담을 슬롯 이름.

### 트리 노드

```json
"root": {
  "guide": "{장소}에서 {관리자}가 장부를 들여다보고 있다...",
  "cases": {
    "offer": {"next": "listen", "condition": "도울 일이 있는지 묻는다"}
  },
  "outcome": "clear" | "partial" | "deviated" | "failed" | "missed"
}
```

`outcome`이 있는 노드가 **종결 노드**다. 도달하면 `cleared`에 기록되고 `active`가 비워진다.

### 이면정보 (`hidden`)

| 키 | 용도 |
|---|---|
| `truth` | 플레이어가 모르는 사실. **누설 금지**로 주입된다 |
| `reveal_conditions` | 알아챌 수 있는 조건 |
| `if_unknown` | 모른 채 진행되면 |
| `if_known` | 알아챘을 때 |

### 세션 상태 (`quest_state`)

```python
{
  "active": {          # 진행 중인 퀘스트
    "id", "name", "version", "line",
    "node",            # 현재 노드 키
    "path": [],        # 지나온 케이스 키
    "slots": {},       # 채워진 가변정보
    "started_turn",    # 정체 판정 기준
    "intended_case",   # 지시층위가 지정한 다음 방향
    "secret_known"     # 이면정보 인지 여부
  },
  "cleared": [{name, version, line, outcome, turn}],
  "known_secrets": [],
  "occurrences": {name: 횟수}
}
```

`quest_state`는 **되감기 추적 대상**(`TRACKED_PATHS`)이다.

### 상수

| 상수 | 값 | 의미 |
|---|---|---|
| `CANDIDATE_LIMIT` | 4 | *"너무 많으면 토큰이 늘고 선택이 흐려진다"* `[코드]` |
| `STALL_TURNS` | 3 | root에서 이만큼 정체하면 전환 가능 |
| `QUEST_FILE_SUFFIX` | `.quests.json` | 부속 파일 규칙 |
| `_cache` | dict | 파일 단위 캐시 |

### 인피니티 플랜 (`INFINITY_PLANS`)

| 키 | narrative_mode | allow_main | 의미 |
|---|---|---|---|
| `sub_only` | quest | ✗ | 서브만 |
| `with_main` | quest | ✓ | 메인도 등장 |
| `designer` | free | ✗ | 서사설계자 주도 |
| `designer_calm` | free | ✗ | 서사설계자, 중형 억제 |

---

## 기능 목록

### `quest.py`

#### `load_quest_data(scenario_id) -> dict`

파일 단위 캐시(`_cache`). 없으면 빈 dict를 캐시해 재시도를 막는다.

> `[확인 필요]` `_cache`를 비우는 공개 경로가 없다. `!캐시 재발급`으로 퀘스트 JSON을 고쳐도 봇을 재시작해야 반영된다.

#### `get_state(session) -> dict`

없으면 4키를 초기화한다. **37곳에서 호출**된다.

#### `_repeat_ok(quest, state) -> bool`

**이름 기준** 중복 차단. `repeatable`이 아니면 1회, 맞으면 `max_occurrences`까지.

#### `filter_available(session, *, limit=4) -> list`

**동작**
```
전 퀘스트 순회
  _repeat_ok 통과
  _passes_filter 통과 (match_filters ≠ None)
이름별로 묶어 랜덤 하나씩
메인라인을 앞으로 정렬
상위 4개
```

**호출부** — `quest.py:388`(블록) · `quest.py:662`(메인 해금) · `gm.py:1760`(`!자동 퀘스트`)

#### `fill_slots(session, quest, matched=None) -> dict`

가변정보를 채운다.
```
'from': 'context:location'  현재 맥락에서
'exclude': ['출발']          앞서 정해진 슬롯과 다른 값
matched                     필터 통과 값이 우선
```

#### `start_quest(session, quest, matched=None) -> dict`

슬롯을 채우고 `root`에서 시작한다. `occurrences`를 올린다.

**호출부** — `quest.py:329`(`apply_choice` 내부) · `gm.py:1774`(`!자동 퀘스트 열기`) · `gm.py:3701`(⚠️ 중복 경로)

#### `choice_context(session) -> str`

**3-상황 판정**
```
active 없음                        → CTX_NONE
deviation ≥ quest_deviated(71)     → CTX_SWITCH
root에서 3턴 정체                  → CTX_SWITCH
그 외                              → CTX_ACTIVE
```

#### `offered_ids(session) -> list`

이번 턴에 제시한 id. **코드 측 검증용.**

#### `apply_choice(session, choice) -> dict`

**무엇을 하는가** — 지시층위의 선택을 검증하고 반영한다.

**검증 4단계**
```
① CTX_ACTIVE인데 값이 왔으면 무시 (오작동 로그)
② id가 비면 keep
③ offered_ids에 없으면 무시
④ 현재 것과 같으면 keep
전환 시 → 기존 퀘스트를 outcome="abandoned"로 cleared에 기록
```

**반환** — `{applied, action: start|switch|keep|ignored, reason}`

**호출부** — `gm.py:2138` (`_call_gm_logic` 내부)

#### `build_quest_block(session) -> str`

**상황별 조립**
```
A(ACTIVE)  활성 가이드만. 선택지 없음
B(NONE)    후보 목록 + "억지로 고르지 말 것"
C(SWITCH)  활성 가이드 + 후보 + 유지/전환 판단
```

**활성 가이드 구성**
```
이름 / 현재 노드의 guide (슬롯 치환됨)
지나온 경로
다음 전개 후보 (케이스별 condition)
이면정보 — 알아챘으면 if_known, 모르면 truth + "누설 금지"
"이 가이드를 참고하되 플레이어의 선택을 강요하지 말 것."
```

**부작용** — `session._quest_offered`를 채운다. **이것이 없으면 `apply_choice`가 전부 무시한다.**

**호출부** — `prompt.py:239`(묘사층위) · `gm.py:516`(지시층위)
> **5.30.0에서 수정됨.** 이전에는 두 경로 모두 작동하지 않았다.

#### `case_schema(session) -> dict | None` / `choice_schema(session) -> dict | None`

**무엇을 하는가** — 상황에 따라 지시층위 응답 스키마를 동적으로 만든다.

**설계** — 활성 퀘스트가 있을 때만 `quest_case`를, 선택 가능할 때만 `quest_choice`를 요구한다. **필드를 주지 않으면 모델이 답할 수 없다.**

**호출부** — `gm.py:2008,2009`

#### `set_intended_case(session, case) -> str | None`

지시층위가 지정한 다음 방향을 `active["intended_case"]`에 기록한다. **진전은 하지 않는다.**

**호출부** — `gm.py:2127`

#### `advance_quest(session, extraction) -> dict | None`

**무엇을 하는가** — 추출 수치로 케이스를 진전시킨다.

**판정 순서**
```
deviation ≥ quest_deviated(71)  → 진전 없음, replan=True
advance < quest_advance(71)     → 진전 없음
케이스 없음                     → 진전 없음
진전 — intended_case 우선, 없으면 첫 케이스로 폴백
outcome이 있으면 → cleared 기록 + apply_grants + active=None
```

**호출부** — `gm.py:3248`

> `[확인 필요]` 폴백이 **첫 케이스**다. 지시층위가 방향을 지정하지 않으면 항상 같은 갈래로 간다.

#### `move_to_case(session, case_key) -> bool`

수동 진전. `!자동 퀘스트 진전`에서 쓴다.
**호출부** — `gm.py:1793`

#### `check_secret_awareness(session, extraction) -> bool`

`secret_awareness ≥ secret_reveal(71)`이면 `mark_secret_known`.
**호출부** — `gm.py:3231`

#### `apply_grants(session, quest, outcome) -> dict | None`

**성공 계열(clear·partial)에만** 적용한다.
```
grants.faction → session.player_faction
grants.info    → session.info_ledger
```

**호출부** — `quest.py:564`(`advance_quest`) · `quest.py:593`(`move_to_case`)

#### `check_main_unlock(session) -> list`

메인라인이 조건을 만족하는지. `filter_available`로 확인.
**호출부** — `gm.py:3238`

#### `is_ending(outcome) -> bool` / `apply_infinity_plan(session, plan_key)` / `plan_allows_main(session)` / `format_plans()`

엔딩 판정과 인피니티 플랜.
**호출부** — `gm.py:3265` · `gm.py:755` · `quest_filter.py:304` · `gm.py:3272`

#### `summary(session) -> str`

> ⚠️ **호출부 없음.** 퀘스트 상태 요약 문자열. `!자동 퀘스트`가 자체 조립하는 방식으로 대체된 듯하다. `[추정]`

---

### `quest_filter.py`

#### `_normalize(spec) -> dict`

문자열·리스트·dict를 `{op, values, as, scope}`로 통일한다.

#### `_match(candidates, spec) -> list | None`

연산자별 매칭. `any`는 교집합, `all`은 전부 포함, `none`은 하나라도 있으면 탈락.

#### `npcs_available(session, scope="here") -> list`

**scope별 후보**
```
here       현재 장소 노드의 npcs
companion  session.companions
any        둘 다
```

**개연성 보장** — 현재 장소 실재 인물과 교집합을 먼저 낸다.
> 경로 추정이 아니라 직접 대조라 시나리오 구조와 무관하다.

#### `items_available(session)` / `info_available(session)` / `places_in_scope(session)`

소지품·인지 정보·장소 범위(현재 + 상위 경로).

#### `match_filters(session, quest, state) -> dict | None`

**무엇을 하는가** — 전 필터를 검사하고 통과하면 슬롯을 반환한다.

**반환** — `{"slots": {...}}` 또는 `None`

**핵심** — 통과한 값이 그대로 슬롯이 된다. `as`로 이름을 지정한다.

**호출부** — `quest.py:63`(`_passes_filter`) · `quest.py:207`(`start_quest`) · `quest.py:407`(블록 조립)

> 🔁 **같은 턴에 3회 호출된다.** `filter_available`이 후보마다 `_passes_filter`로 한 번, `build_quest_block`이 슬롯 채우려 또 한 번, `start_quest`가 또 한 번.

---

## 흐름

### 퀘스트가 열리기까지

```
_build_logic_user_prompt
  └ build_quest_block(session)
       choice_context() → CTX_NONE
       filter_available() → 후보 4개
       session._quest_offered = [id...]
       "[가능 퀘스트 목록]" 텍스트 생성
  ↓
지시층위 AI 호출
  choice_schema()로 quest_choice 필드 요구
  ↓
_call_gm_logic
  └ set_intended_case(quest_case)     방향 지정만
  └ apply_choice(quest_choice)        검증 후 start_quest
```

### 진행

```
묘사 출력 → 추출층위
  ↓
advance_quest(extraction)
  deviation ≥ 71 → replan (진전 안 함)
  advance ≥ 71  → intended_case로 진전
                   outcome 도달 시 cleared + grants + active=None
  ↓
check_secret_awareness → secret_known
check_main_unlock      → 메인 해금 알림
is_ending              → 인피니티 플랜 제시
```

---

## 다른 영역과의 접점

| 상대 | 방향 | 내용 |
|---|---|---|
| **ai** | quest → ai | `get_thresholds`(임계값) |
| **ai** | ai → quest | `build_quest_block` · `advance_quest` · `apply_choice` |
| **world** | quest → world | `load_places` · `resolve` · `path_of` · `substitute` |
| **base** | quest → base | 세션 저장 |
| **session** | session → quest | `get_state`(37곳 중 다수) |

---

## 발견 사항

> 🔴 **중복 구현 — `_apply_quest_choice` vs `apply_choice`**
>
> ```
> _call_gm_logic     gm.py:2138  core.quest.apply_choice(picked)
> _run_gm_logic_loop gm.py:1611  self._apply_quest_choice(session, decision, m_send)
> ```
>
> **같은 턴에 순차 실행된다.**
>
> `apply_choice`가 원래 경로이며 더 완비돼 있다.
> - 3-상황 검증 (`CTX_ACTIVE`면 무시)
> - `offered_ids` 대조
> - 전환 시 기존 퀘스트를 `abandoned`로 기록
> - `start`/`switch`/`keep`/`ignored` 구분
>
> 제가 5.24.0에서 이것을 모르고 `_apply_quest_choice`를 새로 만들었다.
> 그쪽은 `narrative_mode` 확인과 `quest_select: "random"` 처리를 추가로 하지만,
> 전환 처리가 없고 `active`가 있으면 그냥 반환한다.
>
**실행 순서를 확인했다.**
> ```
> gm.py:1596  decision = await self._call_gm_logic(...)   ← 내부에서 apply_choice
> gm.py:1611  await self._apply_quest_choice(...)          ← 중복
> ```
>
> `apply_choice`가 먼저 돌아 퀘스트를 열면, 뒤에 오는 `_apply_quest_choice`는
> `state["active"]`가 이미 차 있어 즉시 반환한다. 실질 피해는 없다.
>
> 다만 `apply_choice`가 열지 **않은** 경우(예: `CTX_ACTIVE`라 무시)에는
> `_apply_quest_choice`가 이어서 시도한다. 이때는 `apply_choice`가 막은 것을
> 우회해 여는 셈이라 **검증이 무력화된다.** `[확인 필요]`

> ⚠️ **`quest.summary` 호출부 없음**
> `!자동 퀘스트`가 자체 조립하는 방식으로 대체된 듯하다.

> ⚠️ **`_cache`를 비우는 경로가 없다**
> 퀘스트 JSON을 고쳐도 봇 재시작 전까지 반영되지 않는다.
> `!캐시 재발급`은 Gemini 캐시만 다루고 이 캐시는 건드리지 않는다.

> ⚠️ **`advance_quest`의 폴백이 첫 케이스**
> 지시층위가 `intended_case`를 지정하지 않으면 `next(iter(cases))`로 간다.
> dict 순서상 항상 같은 갈래다. 무작위 선택이 나은지 검토가 필요하다. `[확인 필요]`

> 🔁 **`match_filters`가 같은 턴에 3회 호출**
> `filter_available`(후보마다) → `build_quest_block`(슬롯) → `start_quest`(슬롯).
> 후보 4개면 최소 6회다. NPC 목록·장소 경로를 매번 다시 계산한다.

> ⚠️ **`quest_select` 설정이 `_apply_quest_choice`에만 있다**
> 5.24.0에서 추가한 `"random"` 모드가 중복 경로에만 있어, `apply_choice`가 먼저 도는
> 현재 구조에서는 동작하지 않는다. `[확인 필요]`

> ⚠️ **`narrative_mode` 확인도 중복 경로에만 있다**
> `apply_choice`는 `free` 모드에서도 퀘스트를 연다.
> 다만 `free`면 `build_quest_block`이 후보를 주지 않을 수도 있어 실질 영향은 확인이 필요하다.

---

## 확인 필요 목록

### 최우선 — 중복 구현

- [ ] **`_apply_quest_choice`를 제거하고 `apply_choice`로 일원화해야 합니까?**
      제가 5.24.0에서 기존 경로를 모르고 만든 중복입니다.
      `apply_choice`가 전환 처리·`abandoned` 기록까지 갖춰 더 완비돼 있습니다.
- [ ] 일원화한다면 `_apply_quest_choice`에만 있는 두 기능을 `apply_choice`로 옮겨야 합니까?
      · `narrative_mode != "quest"`면 건너뛰기
      · `quest_select: "random"`이면 코드가 무작위 선택

### 설계 의도

- [ ] `advance_quest`의 폴백이 `next(iter(cases))`입니다. 지시층위가 방향을 지정하지
      않으면 항상 첫 케이스로 갑니다. 무작위가 나을까요?
- [ ] `CANDIDATE_LIMIT = 4`가 적절합니까? 영도는 퀘스트가 44종입니다.
- [ ] `STALL_TURNS = 3`(root 정체 시 전환 허용)이 적절합니까?

### 정리

- [ ] `quest.summary` 호출부가 없습니다. 제거해도 됩니까?
- [ ] `load_quest_data`의 `_cache`를 비우는 명령이 필요합니까?
      퀘스트 JSON을 고쳐도 재시작 전까지 반영되지 않습니다.

### 성능

- [ ] `match_filters`가 한 턴에 6회 이상 돕니다. 결과를 캐시해야 합니까?
