# H03 완료 번들 — 정량 시간·예약 + 공간·이동·접근·가시성·공간인지

- 작성일: 2026-10-06
- 프로그램: INDAIM 동적 세계 구현 — 통합 로드맵(H01~H07). 패킷 `INDAIM_H03_TIME_SPATIAL_PACKET_v0.1_2026-10-06_49c8dcc`
- **BASE SHA:** `49c8dcc9c8881786643d60fa2caa97134aa76f31` (H02 승인본, clean)
- **H03-A checkpoint SHA:** `1045e00c2ab2b340b732f0a12e0d2d8ac6739a6d`
- **FINAL SHA (코드·문서):** `cc48d1e7ea9b30207772b8a6585be3e0b9c16633` — 이 번들은 그 위의 문서 전용 커밋에 담긴다(브랜치 `claude/h03-time-spatial`)
- 제품 버전: **7.1.0** (7.0.0 → 7.1.0 — 하위호환 기능 추가 = MINOR, 결정 게이트 A2)
- `SCHEMA_VERSION`: **4** · turn-history: **3** · CommitPlan: **2** — 전부 유지
- compiler semantic version: **1 → 2** · `DEFINITION_FORMAT_VERSION`: 1 유지(추가형 형태 변경, B7)
- 상태: H03 구현 완료 · 독립 검증 대기. **H04는 시작하지 않았다. push하지 않았다.**

---

## 1. 범위와 production 효과

H03-A(달력·WorldClock·pending 예약·base quiescence·`quantitative_time` 검증) → 전체 회귀(746 passed) → H03-B(방향 공간 그래프·이동 방식·접근·정적 가시성·공간 지식·경로 계획·TRANSIT·`structured_places` 검증) 순서로 진행했다.

production 효과는 하나다: **READY와 `canonical_mutation`이 투영 결과의 base quiescence를 증명한다.** 레거시 세션(빈 시계·빈 예약)은 항상 quiescent이므로 현행 플레이는 바뀌지 않는다.

하지 않은 것(경계 준수):
- 플레이어 자연어 턴 → 새 이동·시간 연산 전환 없음(producer 0, §8).
- legacy `places.py` BFS 이동·`world_timeline` 시간 권위 유지(파일 diff 0, §8).
- 사건 엔진·WorldProgression cascade·예약 효과 처리 없음(`SCHEDULE_CONSUME`은 큐 제거만).
- 시나리오 원본·`prompts.py` 무변경, 구간 거리 추론 없음, 거리 기반 가시성 없음, provider 호출 추가 0.
- 정의 스냅샷 세션 바인딩 없음(H06). H02 변이·롤백·복구 불변식 유지(H02 테스트 전부 통과).

## 2. 변경 파일

| 파일 | 변경 |
|---|---|
| `core/world_mutation.py` | 병합 의미 `DELETE` 추가(값 None 강제, 같은 계층 멱등, 혼용·없는 키 = `MERGE_CONFLICT`, 인과 없는 교차 계층 = `UNORDERED_INDEPENDENT_WRITES`), `ProjectedWorld.has`, 파일 끝 `_register_production_handlers()`가 시간·공간 handler 등록 |
| `core/turn_preparation.py` | `_freeze_and_project_world`가 투영 성공 뒤 `base_quiescence_of(result.world)` 증명 → 실패 시 `world_conflicts=[{code: NOT_QUIESCENT,...}]`, `failure_code=WORLD_PROJECTION_CONFLICT`, READY 사유 `world:not_quiescent`. `prep.world_quiescence`, `ReadyProof.base_quiescence`(기본값 None — 구 증명 호환) |
| `core/canonical_mutation.py` | 투영 성공 뒤 quiescence 실패 → `OUTCOME_CONFLICT`(reason `NOT_QUIESCENT: ...`) |
| `core/definition_compiler.py` | `COMPILER_VERSION=2`; `VALIDATED_CAPABILITIES += quantitative_time, structured_places`; 예약 키에서 `calendar`·`movement_modes`·`access_control_definitions`·`spatial_knowledge_seeds` 제거; `_compile_calendar`·`_normalize_edge_props`·`_check_zero_cost_cycles`·`_compile_movement_modes`·`_compile_access`·`_compile_spatial_seeds`; 정의에 `calendar`·`movement_modes`·`access_control_definitions`·`spatial_knowledge_seeds` 추가 |
| `core/__init__.py` | `world_clock`·`world_schedule`·`access`·`spatial`·`movement` 재수출 |
| `core/constants.py` | `__version__ = "7.1.0"` |
| `CLAUDE.md`·`DEVLOG.md`·`_PROJECTINFO.txt` | H03 완료 / H04 READY, 모듈 표, 수치(68 서브모듈·804 테스트), v7.1.0 기록 |

## 3. 신규 파일

| 파일 | 줄 | 내용 |
|---|---|---|
| `core/world_clock.py` | 244 | 달력·분 시계 |
| `core/world_schedule.py` | 235 | pending 예약·quiescence·시간 typed 연산 5종 |
| `core/spatial.py` | 401 | 그래프·가시성·배치/TRANSIT·지식·공간 typed 연산 7종 |
| `core/access.py` | 166 | 접근 규칙 레지스트리·실제/인지 접근 |
| `core/movement.py` | 495 | 소요시간·KnownRoutePlanner·ActualTraversalPlanner·`traversal_ops` |
| `tests/policy/test_h03a_time_schedule.py` | 479 | H03-A 28건 |
| `tests/policy/test_h03b_spatial_movement.py` | 733 | H03-B 54건 |
| `tests/characterize/test_h03_no_cutover.py` | — | 무배선 경계 4건 |
| `tests/fakes/spatial_fixture.py` | — | 테스트 전용 합성 공간 시나리오(시나리오 원본 아님) |

의도적으로 갱신한 기존 테스트(§12 D11·D12): `test_h01_definition_compiler.py`(3곳), `test_h02a_world_state.py`(시계 형태), `test_h02b_world_mutation.py`·`test_h02b_corrections.py`(합성 handler 도메인 `world_clock` → `information_state`, 레지스트리 단언), `test_h02_no_cutover.py`(H03 도메인 모듈 허용·레지스트리 기대값).

---

## 4. 시간 — 스키마와 정본 형태

### 4.1 달력 저작 스키마(raw `calendar`)

```json
{"calendar_id": "cal_main", "type": "GREGORIAN" | "FIXED_CUSTOM",
 "month_lengths": [30, 31, ...],              // FIXED_CUSTOM 필수, GREGORIAN 금지
 "dayparts": {"dawn": {"start": "05:00", "end": "09:00", "representative": "06:00"}, ...}}
```

- 시간대 구간은 `[start, end)`, `start > end`는 자정을 넘는다. 빈 구간·대표 시각 구간 밖·구간 겹침은 ERROR.
- `quantitative_time` 선언 + 달력 없음 = `CALENDAR_REQUIRED`(legacy 12×30 암묵 달력 금지). 달력만 있고 capability 없음 = WARNING.
- 컴파일 결과: `{calendar_id, calendar_type, month_lengths|None, dayparts, daypart_order}`(시작 시각 순 정렬). 시작 날짜의 세션 적용은 H06.
- GREGORIAN 일련번호는 파이썬 proleptic Gregorian `date.toordinal()`과 같다(1년 1월 1일 = 1, 테스트로 대조).

### 4.2 `world_clock`

```json
{"minute_index": int, "start_minute_index": int}      // 미초기화(레거시) = {}
```

`minute_index = day_ordinal × 1440 + hour × 60 + minute`. 정본은 이 두 정수뿐이며 날짜·시각·시간대는 파생한다. `advance_to`는 후퇴를 거부하고 0분 진행을 허용한다. wall-clock을 읽지 않는다.

### 4.3 `world_schedule`

```json
{"<schedule_id>": {"schedule_id", "due_minute": int,
                   "kind": "EVENT_WAKEUP" | "DEFERRED_EFFECT" | "MOVEMENT_WAKEUP",
                   "owner_ref", "payload_ref", "created_by_version", "order_key"}}
```

- **pending만** 담는다. 취소·소비는 `DELETE`로 큐에서 지운다(tombstone 없음).
- 순서 = `(due_minute, order_key, schedule_id)` — dict 삽입 순서 무관. `order_key` 기본값 = id.
- `created_by_version` = 생성 묶음의 source `world_version.version_id`(초기 상태면 None).
- 과거 시각 예약 생성 거부, 미초기화 시계에서 생성 거부.

### 4.4 시간 typed 연산

| op | payload | 규칙 |
|---|---|---|
| `CLOCK_INITIALIZE` | `minute_index` | 시계가 `{}`일 때만 — 두 정수를 같은 값으로 |
| `CLOCK_ADVANCE_TO` | `target_minute` | 초기화 필요, target ≥ now |
| `SCHEDULE_CREATE` | `schedule_id, due_minute, kind` (+`owner_ref, payload_ref, order_key`) | 초기화 필요, due ≥ now, 중복 id 거부 |
| `SCHEDULE_CANCEL` | `schedule_id` | 존재 필요 → DELETE |
| `SCHEDULE_CONSUME` | `schedule_id` | 존재 + due ≤ now → DELETE(효과 처리는 H04) |

### 4.5 base quiescence 증명 형태

`ReadyProof.base_quiescence` / `BaseQuiescenceReport.to_dict()`:

```json
{"quiescent": bool, "clock_initialized": bool, "current_minute": int | null,
 "pending_count": int, "overdue_ids": [schedule_id, ...], "reasons": [str, ...]}
```

| 상태 | 결과 |
|---|---|
| 시계 `{}` + 예약 `{}` (레거시) | quiescent |
| 시계 `{}` + 예약 있음 | NOT — 무효 상태 |
| 초기화 + 모든 pending due > now | quiescent |
| 초기화 + due ≤ now pending 존재 | NOT — `overdue_ids` 정렬 순서로 보고 |
| 시계·예약 형태 오류 | NOT |

NOT이면 READY는 `WORLD_PROJECTION_CONFLICT`(conflict code `NOT_QUIESCENT`)로 결정적 종료 실패한다 — `RETRY_PENDING`이 아니고 PREPARED도 쓰지 않는다. 증명은 **투영된(커밋될) 세계**에 대해 한다.

---

## 5. 공간 — 스키마

### 5.1 이동 구간(raw `movement_edges` → 컴파일)

H01 sugar(`id`·`from`·`to`·`bidirectional`·`reverse`)를 그대로 두고, 방향 구간마다 다음을 검증·정규화해 레코드에 더한다. 원 저작 속성은 `properties`에 남는다.

| 필드 | 규칙 |
|---|---|
| `distance_m` | 정수 ≥ 0(0 허용). `structured_places`에서 누락 = `EDGE_DISTANCE_REQUIRED` ERROR. capability 없으면 None(추론하지 않음) |
| `movement_tags` | 문자열 목록(정렬·중복 제거) |
| `allowed_modes` | None 또는 정의된 mode_id 목록 |
| `base_time_factor` | > 0, 소수 6자리 이내 → 정규 십진 문자열(`"1.2"`). 기본 `"1"` |
| `fixed_delay_minutes` | 정수 ≥ 0. 배수를 적용하지 않는다 |
| `access_ref` | 정의된 control_id |
| `access_rule_ref` | `access_ref`와 함께만. 같은 제어 상태를 다른 규칙으로 평가(방향별 규칙) — 제어 규칙의 상태를 전부 다뤄야 함 |
| `route_hint` · `authored_notes` | 비어 있지 않은 문자열 |
| `knowledge_visibility` | `PUBLIC`·`LOCAL`·`RESTRICTED`·`SECRET`, 기본 `LOCAL` |

자기 루프·없는 끝점·id 충돌은 H01부터 ERROR. 거리·지연 0 구간만으로 닫히는 방향 순환 = `ZERO_COST_CYCLE` WARNING. 알 수 없는 속성 = WARNING. `structured_places` + 구간 + `movement_modes` 없음 = `MODES_REQUIRED` ERROR. **connected·parent에서 구간을 만들지 않는다**(영도 컴파일 결과 구간 0).

### 5.2 이동 방식(raw `movement_modes`)

```json
{"walk": {"base_speed_m_per_min": 80, "allowed_edge_tags": [...]?, "forbidden_edge_tags": [...]?}}
```

속도는 저작값만(core에 장르 속도 없음). 통과 조건: 구간 `allowed_modes`에 포함(지정 시) · 구간 태그와 `forbidden_edge_tags` 교집합 없음 · `allowed_edge_tags` 지정 시 교집합 하나 이상.

### 5.3 접근(raw `access_control_definitions`)

```json
{"rules": {"door_rule": {"rule_type": "STATE_TABLE", "states": {
     "OPEN":   {"result": "PASSABLE"},
     "CLOSED": {"result": "ACTION_REQUIRED", "action_policy": "IMPLICIT"},
     "LOCKED": {"result": "IMPASSABLE", "actor_overrides": [
                 {"requires_capability": "key_door", "result": "ACTION_REQUIRED",
                  "action_policy": "CONFIRM", "action_delay_minutes": 1}]}}}},
 "controls": {"door_1": {"rule_ref": "door_rule", "initial_state": "LOCKED"}}}
```

- 결과 3종 `PASSABLE`·`ACTION_REQUIRED`·`IMPASSABLE`, 정책 3종 `IMPLICIT`·`CONFIRM`·`SEPARATE_ACTION`(ACTION_REQUIRED에만, 필수).
- `actor_overrides`는 저작 순서 첫 일치. capability 이름은 시나리오 어댑터가 행위자에게 부여한다(core는 열쇠·통행증을 모른다).
- 평가기 없는 `rule_type`은 ERROR. 런타임 `AccessRuleRegistry`(rule_type → evaluator)는 확장 가능.
- canonical `access_control_state = {control_id: {"state": str}}`, 항목 없음 = 정의 `initial_state`.

### 5.4 공간 지식 seed(raw `spatial_knowledge_seeds`)

`{seed_id: {known_places[], known_edges[](edge_id 또는 pair_id → 양방향 확장), familiarity_scopes[], known_access_facts[{ref, observed_result, observed_state?}]}}`. 참조 검증만 하고 세션 적용은 H06. `route_sets`는 WARNING으로 유예. `spatial.seed_knowledge()`가 순수 변환을 제공한다: familiarity scope(장소 + 하위) 안의 **PUBLIC·LOCAL 구간만** 포함, RESTRICTED·SECRET은 명시 `known_edges`가 있어야 한다.

### 5.5 canonical 공간 형태

```text
spatial_state = {"scene_place": PlaceId?, "placements": {actor_id: Placement}}
  AT_PLACE = {"kind": "AT_PLACE", "place_id"}
  TRANSIT  = {"kind": "TRANSIT", "movement_id", "edge_id", "from_place", "to_place",
              "edge_started_at_minute", "edge_expected_end_minute"}      # 정확히 이 7키

spatial_knowledge = {actor_id: {
    "places": {place_id: {"knows_name": bool, "visited": bool, "rough_location", "acquisitions": []}},
    "edges":  {edge_id:  {"connection_known": true, "distance_estimate_m", "distance_precision",
                          "last_confirmed_at", "acquisition_sources": []}},
    "access": {control_id | edge_id: {"observed_result", "observed_state", "action_policy",
                                      "confirmed_at_minute", "source"}},
    "revision": int}}                                                     # 지식 upsert마다 +1(ADDITIVE)
```

- 희소: 기록 없음 = 모름. `connection_known=false` 기록은 만들 수 없다(upsert가 true로 고정).
- `visited ⇒ knows_name`을 강제하지 않는다.
- TRANSIT 진행률은 `transit_progress(pl, now)`로 파생(0분 구간은 None). `progress_ratio` 키는 형태 검증·op payload 양쪽에서 거부.

### 5.6 공간 typed 연산

| op | 쓰기 |
|---|---|
| `SCENE_PLACE_SET` | `spatial_state.scene_place` SINGLE_VALUE |
| `ACTOR_PLACEMENT_SET` | `spatial_state.placements.<actor>` = AT_PLACE |
| `ACTOR_TRANSIT_SET` | 같은 칸 = TRANSIT. 초기화된 시계 + `start ≤ now ≤ end` 필요(시계 진행을 인과 부모로) |
| `ACCESS_STATE_SET` | `access_control_state.<control>` = `{"state"}` — 지식은 건드리지 않는다 |
| `SPATIAL_PLACE_KNOWLEDGE_UPSERT` | `spatial_knowledge.<actor>.places.<pid>`(기존 레코드에 필드 병합한 전체 값) + `revision` +1 |
| `SPATIAL_EDGE_KNOWLEDGE_UPSERT` | `...edges.<eid>` + `revision` +1 |
| `SPATIAL_ACCESS_KNOWLEDGE_UPSERT` | `...access.<ref>` + `revision` +1 |

같은 계층에서 같은 칸에 다른 값이면 `MERGE_CONFLICT`(마지막 쓰기 채택 없음). handler는 형태·시계·인과만 검증하고 PlaceId·EdgeId·control 실재 검증은 정의를 가진 producer 몫이다(§12 D4).

---

## 6. 이동 — 알고리즘

### 6.1 소요시간 · 정밀도 · 반올림

```text
moving_s  = distance_m / base_speed_m_per_min × 60 × (edge × stance × actor × environment)
edge_us   = ROUND_HALF_UP(moving_s × 10^6) + fixed_delay_minutes × 60×10^6 + 자동 수행 접근 지연 × 60×10^6
route_min = ROUND_HALF_UP(Σ edge_us / (60×10^6))                    # 경로 끝에서 한 번만
Ti        = T0 + ROUND_HALF_UP(누적_us_i / (60×10^6))               # checkpoint
```

- 배수는 모두 **시간 배수**(1.2 = 20% 더 걸림). 입력은 소수 6자리 이내 십진수만 허용 — float은 `str()`로 십진 표기를 고정한 뒤 해석하고, `0.1+0.2` 같은 이진 오차 표기는 거부한다.
- 계산은 `Fraction`(정확한 유리수) → 구간마다 **정수 마이크로초** 양자화(B4 선택) → 분 정규화는 경로 누적에서 한 번. 20초 × 6구간 = 2분(구간별 올림이면 6분), checkpoint `[0,1,1,1,2,2]`분.
- HALF_UP: 29.999999초 → 0분, 30초 → 1분, 89.999999초 → 1분, 90초 → 2분.

### 6.2 KnownRoutePlanner

- 입력: 정의, **행위자 지식**, 출발·도착, 이동 방식·배수, 설정. 실제 `access_control_state`는 인자에 없다(서명 고정 테스트 + `actual_access`를 예외로 바꿔도 통과).
- 지식 그래프: `connection_known` 구간 ∩ 이동 방식 허용 − 지식상 막힘(`PERCEIVED_BLOCKED`). 모르는 봉쇄는 남긴다.
- 구간 비용: 지식의 `distance_estimate_m`이 있으면 같은 공식(정수 μs), 없으면 중립 상수 `unknown_edge_cost_us`(기본 10분).
- **Yen K-shortest simple paths**(방향, raw_k 기본 10). 내부 Dijkstra는 `(비용, edge_id 서명 튜플)` 최소를 꺼내므로 **동률은 경로 서명 사전순**. 인접 목록은 edge_id 정렬 — dict 삽입 순서·`PYTHONHASHSEED` 무관(별도 프로세스 2개 비교 테스트).
- 다양성: 비용 순으로 최단을 고르고, 이후 후보는 `비용 ≤ 최단 × max_cost_ratio(기본 2)`이고 기존 선택들과의 **가중 Jaccard 중첩**(공유 구간 비용 / 합집합 비용, Fraction)이 `max_overlap(기본 7/10)` 이하일 때만 채택, 최대 `max_displayed(기본 3)`.
- 후보: `option_id`(transient `route_n`), `edge_ids`, `place_ids`, `perceived_cost_us`, `perceived_duration_minutes`(모든 구간 추정치가 있을 때만), `perceived_distance_m`, `cost_known`, `first_divergence_edge`, `route_label`(① 첫 분기 구간 `route_hint` ② 최단 경로에 없는 첫 경유지 ③ 분기 구간 도착지 방향), `known_constraints`(지식상 접근 제약만), `knowledge_basis`(ESTIMATED·PARTIAL·UNKNOWN).
- 상태: `OK` · `NO_KNOWN_ROUTE`(실제 그래프로 보완하지 않음) · `SAME_PLACE`.
- ASK 임계값은 적용하지 않는다 — `MovementAskPolicy`(임계 None, `max_displayed_routes=3`) 구조만.

### 6.3 ActualTraversalPlanner

- 선택된 구간 순서를 그대로 따라간다. 구간마다: 이동 방식 불가 → `BLOCKED`; 실제 접근 `IMPASSABLE` → `BLOCKED`; `ACTION_REQUIRED`는 `IMPLICIT`(항상)·`CONFIRM`(호출자가 확인했을 때만)이면 `ACCESS_ACTION` checkpoint + 지연, 아니면 `ACCESS_ACTION_REQUIRED`로 중단; `SEPARATE_ACTION`은 언제나 중단. **막힌 지점에서 멈추고 다른 길로 우회하지 않는다.**
- 출력 `ActualTraversalPlan`: `actor_id, selected_option_id, intended_destination, edge_sequence, traversed_edges, mode, stance, start_minute, raw_total_us, canonical_total_minutes, end_minute, expected_arrival_minute(완주 시), completed, end_place, interruption, access_checks, checkpoint_times, source_world_version, definition_revision`.
- checkpoint 종류: `EDGE_START`·`EDGE_END`·`ACCESS_ACTION`·`DESTINATION`(H04 사건·예약 checkpoint는 미결합).
- `placement_at(plan, minute, movement_id)` — `[EDGE_START, EDGE_END)` 안이면 TRANSIT, 아니면 AT_PLACE(파생).
- `traversal_ops(plan, op_prefix)` — `CLOCK_ADVANCE_TO` + `ACTOR_PLACEMENT_SET` + 통과 구간 지식(`TRAVERSED`, `last_confirmed_at`) + 도착 장소 `visited` + 실제로 확인한 접근 사실(`OBSERVED`). **production 호출자 0**(테스트만).

---

## 7. 등록된 production 연산과 실제 호출자

`world_mutation.PRODUCTION_REGISTRY.op_types()` (12종, typed만):

```
ACCESS_STATE_SET  ACTOR_PLACEMENT_SET  ACTOR_TRANSIT_SET  CLOCK_ADVANCE_TO  CLOCK_INITIALIZE
SCENE_PLACE_SET  SCHEDULE_CANCEL  SCHEDULE_CONSUME  SCHEDULE_CREATE
SPATIAL_ACCESS_KNOWLEDGE_UPSERT  SPATIAL_EDGE_KNOWLEDGE_UPSERT  SPATIAL_PLACE_KNOWLEDGE_UPSERT
```

- 등록은 `world_mutation._register_production_handlers()` 한 곳(`PRODUCTION_REGISTRY.register` 다른 파일 0 — 테스트 고정). 임의 경로 setter(`PATH`·`SET_ANY`류) 없음.
- **연산을 만드는 production 경로: 0.** `main.py`·`prompts.py`·`cogs/*`·기존 `core/*`(도메인 모듈·world_mutation·재수출 제외)에서 op 타입 문자열·`OP_*` 상수·`traversal_ops`·`plan_known_routes`·`plan_actual_traversal`·`placement_at`·`seed_knowledge` 참조 0건(`test_h03_no_cutover.py::test_no_production_producer_of_h03_operations`, grep 재확인).
- H03 모듈의 production 사용처(허용 목록 고정): `world_schedule` ← `turn_preparation`·`canonical_mutation`(quiescence), `world_mutation`(등록); `world_clock` ← `definition_compiler`(달력 검증); `spatial` ← `world_mutation`(등록). cogs·main 사용 0.

## 8. legacy 동작 불변 증거

- `git diff 49c8dcc cc48d1e -- scenarios prompts.py core/places.py core/timeline.py cogs main.py` → **빈 출력**.
- `core/places.py`·`core/timeline.py`·`cogs/gm.py`·`cogs/game.py`·`core/extraction.py`는 H03 모듈을 import하지 않고 새 도메인 필드(`world_clock`·`world_schedule`·`spatial_state`·`spatial_knowledge`·`access_control_state`)에 접근하지 않는다(테스트 고정).
- 정적 가시성: 영도 122개 장소 전 쌍(14,884)에서 `SpatialGraph.is_visible_from` == `places.is_visible_from`.
- 레거시 세션 READY: `base_quiescence = {quiescent: true, clock_initialized: false, ...}` — 기존 턴 그대로 커밋(테스트).
- 기존 718건 전부 통과(H02 롤백·복구·이력 스위트 포함). 봇 로딩 cogs 9 · 명령어 44 · views 5 불변.

## 9. 테스트 결과

| 시점 | 결과 |
|---|---|
| BASE baseline(`49c8dcc`) | 718 passed · 0 failed · 0 xfailed · 0 XPASS |
| H03-A 전용(`test_h03a_time_schedule.py`) | 28 passed |
| H03-A 게이트 전체 회귀(`1045e00`) | **746 passed** |
| H03-B 전용(`test_h03b_spatial_movement.py`) | 54 passed |
| H03 경계 특성화(`test_h03_no_cutover.py`) | 4 passed |
| **FINAL 전체**(`cc48d1e`) | **804 passed** · 0 failed · 0 xfailed · 0 XPASS (특성화 50 · 결함 27 · 정책 727) |
| `python3 -m compileall` | OK |
| 검증 루틴 ① 구문·미정의 self | OK |
| ② 재수출·세션 필드 | OK · OK (`SESSION_FIELDS` 95 불변) |
| ③ 퀘스트 트리·장소 참조 | OK |
| ④ `tools/verify_docs.py` | 문서 수치 OK(21항목) — `--fix`로 서브모듈 63→68, DEVLOG 버전 반영 |
| ⑤ 봇 로딩 | cogs 9 · 명령어 44 · views 5 (presence 경고는 문서상 무시 대상) |
| ⑥ `tools/scenario_check.py --all` | 5개 시나리오 compiler v2 · ERROR 0(영도 WARNING 3 = 기존 별칭 2·비대칭 connected 1) |

### 추적성(H03-R##)

| ID | 검증 |
|---|---|
| R03·R04·R05 | `constants.__version__`, `test_h02a…::test_schema_version_is_4…`, `test_compiler_version_2…` |
| R06·R07 | `test_compiler_version_2_and_quantitative_time_is_validated`(CALENDAR_REQUIRED, 12×30 fallback 없음) |
| R08~R12 | `test_gregorian…`·`test_fixed_custom…`·`test_dayparts…`·`test_clock_never_moves_backward…` |
| R13·R14 | `test_schedule_order_is_independent…`·`test_time_ops_initialize_advance_and_schedule_lifecycle` |
| R15·R16 | `test_due_now_pending_blocks_ready…`·`test_invalid_base_state_blocks_ready`·`test_legacy_empty_time_state_is_quiescent_in_ready`·`test_canonical_mutation_requires_quiescence` |
| R17 | `test_typed_time_ops_commit_and_rewind_in_player_turn`·`test_delete_semantic_conflicts_and_insertion_order` |
| R18·R19·R20 | `test_structured_places_is_validated…`·`test_edge_property_validation`·`test_legacy_connected_is_never_promoted…`·`test_compiler_never_invents_distance…` |
| R21~R24 | `test_duration_formula…`·`test_full_route_rounding_half_up…`·`test_plans_are_hashseed_and_process_independent` |
| R25·R26·R32 | `test_hidden_actual_block_never_filters_known_candidates`·`test_no_known_route_is_not_filled_from_actual_graph` |
| R27·R28 | `test_known_routes_rank_diverse…`·`test_diversity_filter…`·`test_unknown_cost_routes_are_valid_and_tie_break_is_signature`·`test_planner_order_is_insertion_order_independent` |
| R29·R30·R31 | `test_access_three_results_three_policies…`·`test_perceived_access_is_separate_from_actual`·`test_traversal_access_policies`·`test_access_rule_override_must_cover_control_states` |
| R33·R34·R35 | `test_no_known_route…`(장소 지식 ≠ 경로)·`test_actual_access_change_does_not_update_knowledge`·`test_secret_edges_need_explicit_knowledge…`·`test_blocked_traversal_learns_block_only_by_observation` |
| R36·R37 | `test_traversal_follows_selected_route_and_stops_at_hidden_block`·`test_traversal_eta_and_checkpoints`·`test_transit_shape_has_no_progress_ratio…`·`test_transit_op_requires_clock_window…` |
| R38 | `test_static_visibility_is_authored_not_distance_derived`·영도 전 쌍 대조 |
| R39·R40 | `test_production_registry_is_typed_time_and_spatial_only`·`test_h02_no_cutover::test_production_registry_holds_only_h03_typed_handlers`·`test_h03_no_cutover` 4건 |
| R41~R44 | §8 diff·특성화, §10 provider 스캔, 전체 회귀 |

## 10. 스캔

**direct-writer**(production `.py`에서 새 도메인 필드 대입): `core/models.py:254-260`(생성자 기본값) 뿐. 동적 setattr 경로는 H02와 같다 — `commit_coordinator.py:342`(커밋 적용), `turn_history.py:278·283`(되감기 복원), `canonical_mutation.py:268`(INIT/ADMIN 롤백), `world_mutation.py:676`(`apply_projection`). 새 writer 0.

**provider**: H03 모듈 import는 표준 라이브러리(`re`·`dataclasses`·`heapq`·`decimal`·`fractions`)와 H03/H02 core 모듈뿐(google·discord·aiohttp·requests·prompts·재무·캐시·io 금지 — 테스트 고정). `generate_content` 출현 수(.py) BASE 18 = FINAL 18, provider 호출 파일 집합 동일.

**scenario**: `git diff 49c8dcc cc48d1e -- scenarios prompts.py` 빈 출력. 테스트용 공간 데이터는 `tests/fakes/spatial_fixture.py`(합성)에만 있다.

## 11. definition revision — compiler v1 → v2

| 시나리오 | v1 (`49c8dcc`, H02) | v2 H03-A (`1045e00`, 달력만) | **v2 FINAL** (`cc48d1e`) |
|---|---|---|---|
| 영도 | `0d8255bc118b7423da81b1f604fe7887f31c43c91198ca8724c69bc58b0999cb` | `6e17849c5a01deec7d7404e38b6b131c8e1378aaf1d1d4537f671fbd51ed11ef` | `72985bbdbd5758d64d1678de9c8b0a721100e455d8b056b958f4336d2942d995` |
| 무협 | `1fafeb86ca5cd45fd88de82cdbcce0a92459dea34e335c913e186dad7ed07e6f` | `871b154e49a04fc7bcc69aae1632c3e306555fae6e4a03a137d9a30c22539225` | `994e4105c8a2febb7b8c075c5ead1236ca3f57e90a8cab808a979c5cbf01b425` |
| 다크판타지 | `570f1d14156568f20ed45889d1e4960b574a559f203ea13ef1966ab21d7010e6` | `b76302822bbe0c1f6b72f9a40212c9559b321bc48921e03fcc37263010af4462` | `8037d42b6ad65be39f7cc5a14b148584e8ca6ae0617447a95c85f427594184b7` |
| 빈시나리오 | `07c2ef195719d64eb15259f4f3c32f0dd34a84ecad433e7072a0f2d1bd3cc15f` | `5678c62311ac2d104207674bca71c94f28aa7419884063157294fb070285ec1c` | `38a98cab603adda835579745d251144b9990e0758d69bd81595376114cdd98d7` |
| scenario.example | `29be07e0b058a23752b856448fbd798046020198c686a86e207a12470036ed64` | `7930bce81a0764d2eb144d63902bee01af253378302165d01a4df3d4696a50f7` | `3ba754baccd7148bb5e47f5ff1d0b5e52ff2ccd46646313f9a3e7077aa5a250f` |

모두 바뀐 것은 예상된 결과다 — `compiler_version`이 정의에 포함되고(H01 보정), v2 정의에 `calendar`·`movement_modes`·`access_control_definitions`·`spatial_knowledge_seeds` 키가 추가됐다(legacy 값 None/빈 값). 5개 모두 legacy(`scenario_schema` 없음)라 의미 검증 대상 capability는 absent. production 스냅샷 바인딩 0 → 운영 이행 불필요(A3).

---

## 12. 결정 기록 · 패킷과의 차이

| # | 결정 | 이유 |
|---|---|---|
| D1 | 구간 레코드는 H01 키 `from`/`to`를 유지하고 정규화 필드를 옆에 더했다(`from_place`/`to_place`로 개명하지 않음) | H01 정의 계약·테스트 호환. 의미는 핸드오프 5.2와 같다 |
| D2 | `knowledge_visibility` 기본값 `LOCAL` | 설계가 기본값을 열어 둠. SECRET/RESTRICTED는 명시 저작만, 기본 구간은 familiarity로 획득 가능 |
| D3 | 지식 레코드에 `revision`(upsert마다 +1, ADDITIVE) | 핸드오프 5.7 형태. DESIGN 02 §88상 world_version으로도 충분하지만 캐시 키 후보로 둠 |
| D4 | 공간 handler는 정의 참조(PlaceId·EdgeId·control 실재)를 검증하지 않는다 | 정의가 세션에 바인딩되지 않았다(H06 금지). producer(H05/H06)가 정의를 들고 검증해야 한다 → §14 불변식 |
| D5 | 접근 정의 = `{rules, controls}`, `access_rule_ref` = 같은 제어 상태의 방향별 규칙(상태 전부를 다뤄야 함) | 상태(세계)와 규칙(해석)을 분리. 예: 같은 LOCKED 문이 안쪽에서는 IMPLICIT로 열림 |
| D6 | 방식 `allowed_edge_tags` = 교집합 하나 이상, `forbidden_edge_tags` = 교집합 없음 | 수로 전용(boat)·수로 금지(walk)를 표현하는 최소 규칙 |
| D7 | seed `route_sets`는 WARNING으로 유예 | 핸드오프 "pure helper 수준 가능". 묶음 지식은 H04/H06 NPC seed와 함께 |
| D8 | 이동 태세(stance)는 정의로 컴파일하지 않고 배수만 받는다 | DESIGN 02 §10: 계산기는 `travel_time_factor`만 소비. 핸드오프 5.3은 mode만 요구 |
| D9 | 후보 설정 기본값(raw_k 10, max_displayed 3, max_cost_ratio 2, max_overlap 7/10, 모르는 구간 10분)은 calibration 자리값 | A15·DESIGN 02 §40·46·47 — 영도 경로 데이터로 확정 예정 |
| D10 | 구간마다 정수 μs 양자화 후 경로 끝에서 분 정규화 | B4 허용 범위. μs 이하 반올림은 결정적이며 분 경계 판정과 보고값(`raw_total_us`)이 어긋나지 않는다 |
| D11 | H01 테스트 3곳 의도적 갱신: 예약 키 경고 예시 `calendar` → `event_templates`; 유예 capability 예시 `structured_places` → `information_registry`; 양방향 sugar 테스트에 `movement_modes`·ferry `distance_m: 0` 추가 + v2 정규화 단언 | v2에서 해당 키·capability가 검증 대상이 됨(완화가 아니라 계약 이동) |
| D12 | H02 테스트 갱신: 합성 handler 도메인 `world_clock` → `information_state`, 레지스트리 단언 "비어 있음" → "typed만", 되감기 테스트 시계를 정식 형태로 | H02 합성 handler가 잘못된 시계 형태를 쓰면 H03 quiescence가 정당하게 거부한다. 검증 강도는 그대로 |
| D13 | `MODES_REQUIRED`는 `structured_places` + 구간이 있을 때만 | 구간 없는 선언(장소 무결성만)도 유효해야 함 |
| D14 | 지식상 막힌 구간(`PERCEIVED_BLOCKED`)만 후보 그래프에서 제외, 지식상 ACTION_REQUIRED 지연은 인지 비용에 넣지 않음 | 숨은 봉쇄 비노출(§30)과 플레이어가 아는 제약 표시(§62)를 동시에 만족 |
| D15 | `ACTOR_TRANSIT_SET`은 초기화된 시계와 `start ≤ now ≤ end`를 요구 | 시계와 모순되는 TRANSIT 정본을 막음. 시간 점프와 함께 쓸 때는 `CLOCK_ADVANCE_TO`를 인과 부모로 |
| D16 | 달력 dayparts 저작 순서는 revision에 영향을 준다(컴파일된 `calendar`는 순서 무관) | H01 `legacy_source_order_digest`가 원문 dict 순서를 정의에 묶는 기존 설계 그대로 |

STOP 조건(C절)에 해당하는 상황은 없었다: connected 자동 승격 불필요, 숨은 경로 노출 불필요, 사건 엔진 없이 pending 큐 표현 가능, `world_timeline`과 이중 writer 없음(새 시계 writer는 production에 없음), provider 불필요, 새 세션 필드 없음(SCHEMA 4 유지), H02 불변식 유지.

## 13. 새로 확인한 사실

1. H02 합성 테스트 handler가 `world_clock`에 `{"minute_index": n}`만 쓰고 있었다 — H03 정본 형태 검증으로 드러났다(D12).
2. 모듈명 `world_clock`·`world_schedule`이 세션 필드명과 같아 소스 스캔이 `session.world_clock`을 모듈 참조로 오인한다 — H03 특성화 스캔은 import와 `core.<모듈>`만 센다.
3. 영도는 `scenario_schema`가 없는 legacy라 v2의 `structured_places`·`quantitative_time` 검증은 적용되지 않는다. 컴파일 결과 구간 0·이동 방식 0·접근 0·seed 0.
4. 영도 정적 가시성(`visible_within`)은 주 parent 사슬만 본다 — `extra_parents`는 가시성 범위에 들어가지 않는다(기존 `places.path_of` 의미, 새 그래프도 동일하게 유지).
5. 기존 `CLAUDE.md`의 "총 운영 코드 ~36,680줄" 표기는 단순 `wc -l` 합(BASE 39,194)과 산정 방식이 다르다. 이번에는 같은 표기 방식을 유지해 증분(+2,110 / 테스트 +1,418)만 더했다(~38,790 / ~16,145).

## 14. H04가 믿어도 되는 불변식

1. 정본 시간은 `world_clock` 두 정수뿐이고 production에서 후퇴하지 않는다. 미초기화 `{}` 세션도 유효하다.
2. `world_schedule`은 pending만 담고 순서는 `(due_minute, order_key, schedule_id)`다. 소비·취소는 `DELETE`(tombstone 없음).
3. READY·`canonical_mutation`은 커밋될 세계의 base quiescence를 증명한다. H04는 사건 즉시 cascade·foreground 조건을 **같은 보고 구조에 더해** 확장한다(`BaseQuiescenceReport.reasons`·`overdue_ids`).
4. `SCHEDULE_CONSUME`은 큐 제거만 한다 — 효과 처리·WorldProgression은 H04 몫이며 consume과 같은 묶음 안 인과 자식으로 붙여야 한다.
5. production 레지스트리는 typed 연산만 받는다. 새 도메인 연산은 도메인 모듈의 `register()`를 `_register_production_handlers()`에서 호출해 추가한다.
6. 같은 계층에서 같은 칸을 서로 다른 값으로 쓰면 충돌이다. 순서가 필요한 쓰기는 `causal_parent_op_ids`로 표현한다(시계 진행 → TRANSIT/소비).
7. 실제 이동 그래프는 정의의 방향 구간뿐이다. 계층·connected·거리 기반 가시성은 이동 근거가 아니다.
8. KnownRoutePlanner는 행위자 지식만 읽는다(실제 접근 상태가 서명에 없음). NPC 오프스크린 이동(H04)도 같은 planner와 seed 지식을 써야 한다(DESIGN 09 §9).
9. ActualTraversalPlanner는 선택 경로를 우회하지 않는다. 사건·예약 checkpoint는 H04가 `checkpoint_times`에 끼워 넣는다(현재 종류 4개).
10. 실제 세계 변화는 지식을 자동 갱신하지 않는다. 지식은 관찰·통과·정보 획득 연산으로만 바뀐다.
11. TRANSIT은 7키 고정, 진행률은 파생값이다.
12. 공간 handler는 정의 참조를 검증하지 않는다 → H04/H05/H06 producer는 정의(스냅샷)를 들고 PlaceId·EdgeId·control·actor 실재를 검증한 뒤 연산을 만든다.
13. legacy `places.py`·`world_timeline`은 여전히 플레이 권위다. 두 권위를 동시에 쓰는 writer를 만들지 않는다(전환은 H05/H07).
14. H02 불변식 전부 유지: 묶음 불변·인과 계층·삽입 순서 무권위·커밋 직렬화 락·PLAYER_TURN owner = CommitCoordinator·모든 PLAYER_TURN world_version 전진·결정적 충돌 ≠ 재시도·정체성 필드 비가역·CommitPlan v1/v2·turn-history v2/v3 호환.

## 15. 재현

```bash
git checkout cc48d1e7ea9b30207772b8a6585be3e0b9c16633
pip install -r requirements-dev.txt
python3 -m compileall -q core cogs main.py prompts.py tools tests
python3 -m pytest tests/ -q                      # 804 passed
python3 tools/scenario_check.py --all            # 5 × compiler v2, ERROR 0
python3 tools/verify_docs.py                     # 문서 수치 OK
git diff 49c8dcc cc48d1e -- scenarios prompts.py core/places.py core/timeline.py cogs main.py   # 빈 출력
```
