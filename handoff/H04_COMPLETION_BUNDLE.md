# H04 완료 번들 — Actor·정보·인지·NPC 투영 + 사건·DependencyIndex·WorldProgression

- 작성일: 2026-10-06
- 프로그램: INDAIM 동적 세계 구현 — 통합 로드맵 v0.5(H01~H07). 패킷 `INDAIM_H04_ACTOR_INFO_EVENT_PACKET_v0.1_2026-10-06_567f262`
- **BASE SHA:** `567f262c8f7880f41e2f66e695beed8867ed8c1c` (H03 승인본, clean, 830 passed 재현)
- **H04-A checkpoint SHA:** `0b060610d166a80a7d08b25b88d3a2c92d17dc81` (전체 회귀 854 passed)
- **FINAL SHA (코드·문서):** `481143daedcdc4d80337113fe576233ab8362485` — 이 번들은 그 위의 문서 전용 커밋에 담긴다(브랜치 `claude/h04-actor-info-event`). 실제 최종 HEAD는 완료 보고에 기재
- 제품 버전: **7.2.0** (7.1.0 → 7.2.0 — 하위호환 기능 = MINOR, 결정 게이트 A2)
- compiler semantic version: **2 → 3** · `DEFINITION_FORMAT_VERSION` 1 유지(추가형)
- `SCHEMA_VERSION` **4** · turn-history **3** · CommitPlan **2** — 전부 유지(새 top-level 세션 필드 없음 — H02 정본 필드만 사용)
- 상태: H04 구현 완료 · 독립 검증 대기. **H05는 시작하지 않았다. push하지 않았다.**

---

## 1. 범위와 production 효과

H04-A(행위자·정보·인지·관계·NPC 투영·compiler v3 정보) → 전체 회귀 854 → H04-B(조건식·DependencyIndex·사건·예약 v2·세계 진행·compiler v3 사건) 순서로 진행했다.

**production 동작 변화는 없다.** 새 도메인 typed 연산이 production 레지스트리에 등록됐지만(27종), 그 연산을 만드는 플레이어·cogs 경로와 `project_world_progression` 호출자는 0이다. 레거시 세션 READY·커밋·되감기는 그대로다.

경계 준수:
- player/cogs orchestration 전환 없음 · prompts/extraction/provider 변경 없음(diff 0, provider 호출 18 = 18).
- 시나리오 저작 사건 이행 없음(퀘스트를 EventTemplate으로 자동 변환하지 않음) · 퀘스트·`narrative_plan`·`info_ledger` 권위 은퇴 없음(diff 0).
- 정의 스냅샷 세션 바인딩 없음 · 임의 dotted writer/eval/Python 스크립트 없음.
- H02/H03 변이·롤백·quiescence·이동 인식 경계 보존(전 회귀 통과, H03 계약 테스트 강화).

## 2. 변경 · 신규 파일

| 파일 | 줄 | 내용 |
|---|---|---|
| `core/world_refs.py` (신규) | 433 | 타입 ValueRef 13종 해석(known/UNKNOWN)·의존 키·컴파일 검증·결정적 표시 |
| `core/actors.py` (신규) | 189 | ActorRegistry 검증·레거시 이름 해석·`ACTOR_REGISTER`/`ACTOR_RENAME` |
| `core/information.py` (신규) | 373 | InformationState·계보·계열·typed 연산 4종·supersede/series 빌더 |
| `core/cognition.py` (신규) | 260 | 희소 인지·획득 provenance·믿음 권위·typed 연산 3종 |
| `core/relationships.py` (신규) | 79 | 희소 관계 delta·typed 연산 1종 |
| `core/npc_projection.py` (신규) | 92 | 읽기 전용 CurrentNpcProjection |
| `core/predicates.py` (신규) | 297 | 3값 조건 AST·검증·의존 키·시간 wakeup |
| `core/events.py` (신규) | 527 | 사건 정체성·binding·국면/케이스 선택·typed step 12종·사건 연산 5종 |
| `core/world_progression.py` (신규) | 940 | DependencyIndex·같은 분 일괄·연쇄·계열·NPC 이동·신호·순수 투영 |
| `core/definition_compiler.py` | 1,965 | v3: profile_base·명시 정보·info_ref·information_series·event_templates·override variant id·capability 8종 검증 |
| `core/world_schedule.py` | 304 | 예약 v2(7키 호환 + 11키 신규)·`normalized_entry` |
| `core/access.py` · `core/movement.py` | 168 · 544 | `matched_variant`·`access_action_id`에 variant 포함 |
| `core/world_mutation.py` | 701 | H04 도메인 handler 등록 |
| `core/__init__.py` · `core/constants.py` | — | 재수출 · `__version__ = "7.2.0"` |
| `CLAUDE.md` · `DEVLOG.md` · `_PROJECTINFO.txt` | — | H04 완료 / H05 READY, 모듈 표, 수치(77 서브모듈·907 테스트), v7.2.0 기록 |
| `tests/policy/test_h04a_actor_info.py` (신규) | — | H04-A 24건 |
| `tests/policy/test_h04b_events_progression.py` (신규) | — | H04-B 48건 |
| `tests/characterize/test_h04_no_cutover.py` · `h04_scope.py` (신규) | — | 무배선 경계 5건 · H04 모듈 목록/기대 레지스트리 공유 |

의도적으로 갱신한 기존 테스트(§12 D12): `test_h01_definition_compiler.py`(유예 capability·예약 키 예시), `test_h03a_time_schedule.py`(compiler ≥2, 신규 예약 11키), `test_h03b_spatial_movement.py`(compiler ≥2, 레지스트리 기대값, `access_action_id` variant), `test_h03_corrections.py`(variant), `test_h01/h02/h03_no_cutover.py`(H04 도메인 모듈 허용).

---

## 3. H04-A — 정본 형태

### 3.1 ActorRegistry

```text
actor_registry = {ActorId: {"kind": "PC"|"NPC", "display_name": str,
                            "source_kind": "PLAYER"|"AUTHORED_NPC"|"SESSION_NPC",
                            "source_key": str|null, "aliases": [str]}}      # 정확히 5키
```
- PC ⇔ source_kind PLAYER, PC id는 `pc_<uid>`. 세션 NPC id = `npc_s` + stable_digest(narrative_seed, 최초 키)(순수 함수, 적용은 H05/H06).
- `ACTOR_REGISTER`(중복 거부)·`ACTOR_RENAME`(id 유지, 옛 표시 이름 alias append+dedupe, 외래 키 rekey 없음).
- 레거시 해석 `resolve_actor_id`: 등록부 표시 이름 → alias → 저작 사전 키 → 저작 표시 이름. 같은 단계 후보 2개 이상이면 `actor_id=None` + 후보(고르지 않음).
- compiler v3 `actor_definitions[*]`에 `profile_base`(정제 저작 프로필 — `actor_id`·`name`·`비밀`·`resources`·`statuses` 제외)와 `secret_info_id` 추가. CurrentNpcProjection은 `legacy_source` 없이 동작한다(테스트: `legacy_source=None`).

### 3.2 Information

```text
information_state = {"runtime_definitions": {InfoId: def}, "status": {InfoId: status}, "series": {SeriesId: state}}
  (논리적 빈 값 {} 허용 — normalized()가 3키로 읽는다)
def    = {"info_id","kind":"RUNTIME","proposition","summary","subject_refs","tags","fragments","sensitivity",
          "source_ref","derived_from","series_ref","created_at_minute"}       # 정확히 12키, 갱신 연산 없음
status = {"validity": CURRENT|SUPERSEDED|REFUTED, "successor_ids", "predecessor_ids",
          "changed_at_minute", "changed_by_version"}
series = {"current_info_id", "value_fingerprint", "revision_ordinal"}
```
- status 기록 없는 저작·런타임 정보 = CURRENT. 전이 CURRENT→SUPERSEDED|REFUTED, SUPERSEDED→REFUTED만(CURRENT 복귀 거부). 계보 순환·자기 링크 거부.
- 연산: `INFORMATION_CREATE`(같은 id 재생성 거부 = 명제 불변) · `INFORMATION_VALIDITY_SET` · `INFORMATION_LINK`(양방향 SET_LIKE) · `INFORMATION_SERIES_SET`(revision = 이전+1, 런타임 정의 존재 필요).
- `build_supersede_ops`: create → (validity, link[create의 인과 자식]) 원자 조합.
- **정보 계열**: 값 지문 = content_digest(value), InfoId = `info_` + stable_digest(series_id, revision, 지문). 값이 바뀌면 옛 정보 SUPERSEDED + 새 런타임 정의(derived_from) + 링크 + revision+1. A→B→A도 새 InfoId. 같은 지문은 no-op, 미초기화·UNKNOWN 값은 변경 없음(진단). 명제 = 저작 템플릿 `{value}` + 결정적 표시 해석(provider 없음). 초기 상태 적용은 H06(`build_series_init_ops`는 순수 빌더).
- compiler v3 정보 registry: 명시 `information_definitions` + 인라인 `비밀`(문자열 → 생성 InfoId, `{info_ref}` → 명시 정보 참조) + 퀘스트 hidden을 하나로 합쳐 충돌 ERROR. 같은 명제 문장 이중 정의는 합치지 않고 WARNING.

### 3.3 Cognition · PC 믿음 권위

```text
cognition_state = {"actor:<id>"|"group:<id>": {InfoId: record}}      # 기록 없음 = 미인지
record = {"awareness": EXISTENCE|CONTENT, "fragments", "belief": null|SUSPECTED|PROBABLE|BELIEVED|CERTAIN,
          "acquisitions": [AcquisitionRecord], "belief_provenance": [{belief, authority, cause_ref}],
          "secrecy_awareness"}
AcquisitionRecord = {"acquisition_id","at_minute","route","source_actor","source_group","source_info",
                     "evidence_ref","event_instance","place_id","fragments","note"}
```
- `COGNITION_ACQUIRE`: acquisition_id append(같은 id·같은 내용 = 멱등, 다른 내용 = 거부), 인지 수준은 올리기만, fragment 합집합, **믿음 불변**. 임의 cap·삭제 없음.
- `COGNITION_UPSERT`: 인지 수준·fragment·비밀성 인지(믿음 제외), 하향은 `cause_ref` 필수.
- `COGNITION_BELIEF_SET`: CONTENT 기록 필요. **PC holder(등록부 kind PC 또는 `pc_<uid>`)는 `PLAYER_EXPLICIT`·`RULE_FORCED`만** — 다른 권위(RULE·AUTHORED)는 handler가 거부. 사건 step의 정적 PC 믿음 변경도 컴파일 ERROR(`STEP_PC_BELIEF_AUTHORITY`). 증거: `test_pc_belief_authority_and_no_truth_correction`, `test_r40_typed_steps_only[...STEP_PC_BELIEF_AUTHORITY]`.
- 정보가 REFUTED가 돼도 믿음을 교정하지 않는다(거짓 믿음). group 인지는 group으로만 저장·질의(구성원 자동 전파 없음).

### 3.4 Relationship

`relationship_state = {"<from>-><to>": {"from_holder","to_holder","deltas":[Delta]}}`, Delta = `{delta_id, from_holder, to_holder, kind, summary, at_minute, source_ref}`. `RELATIONSHIP_DELTA_APPEND`: 같은 delta_id 멱등·다른 내용 거부. NxN 행렬 없음.

### 3.5 CurrentNpcProjection 출처

`project_npc(actor_id, definition, world, relevant_info_ids=(), legacy_runtime=LegacyRuntime)` → dict:
정의 `profile_base` · `actor_registry`(표시 이름) · `spatial_state.placements`(없으면 `location_tracked=False` — '없음' 아님) · 장소 연관(`place_associations`, 현재 위치 아님) · 레거시 읽기 adapter(resources·statuses·companions·met_npcs·irregular media, 이름 키 읽기만) · ACTIVE 사건 bound slot 역할 · **지정한 InfoId만**의 인지 · 관계 delta. 세션에 저장하지 않는다(production 사용처 0 — 특성화 테스트).

## 4. H04-B — 조건식 · 의존 키

### 4.1 Predicate AST · ValueRef

```text
{"all":[P]} {"any":[P]} {"not":P} {"const":bool}
{"cmp": EQ|NE|LT|LTE|GT|GTE, "ref": ValueRef, "value": literal | "value_ref": ValueRef}
{"exists": ValueRef} {"is_unknown": ValueRef}
{"time": "ELAPSED_SINCE_PHASE_GTE", "minutes": n}
{"time": "DAYPART_IS", "daypart": id}
{"time": "AT_OR_AFTER_DATETIME", "date": "YYYY-MM-DD", "hhmm": "HH:MM"}
ValueRef = {"ref": T, ...}  T ∈ CLOCK_MINUTE · SCENE_PLACE · OBSERVER_ACTIVITY · ACTOR_PLACE · ACTOR_PLACEMENT_KIND ·
           ACCESS_STATE · INFO_VALIDITY · COGNITION_AWARENESS · COGNITION_BELIEF · EVENT_LIFECYCLE(instance|template) ·
           EVENT_PHASE · EVENT_LOCAL · ROUTE_KNOWN_EXISTS
슬롯 "$slot:<name>", 현재 인스턴스 "$self", 같은 케이스 생성 정보 "$step:<step_id>"(step 전용)
```
- 결과 TRUE/FALSE/UNKNOWN(Kleene). 충족 = TRUE만. UNKNOWN 예: 위치 미추적 actor의 ACTOR_PLACE, 기록 없는 EVENT_LOCAL, 묶이지 않은 슬롯, 순서 비교 불성립. '없음'이 정본 의미인 것은 known(인지 NONE, 사건 NONE, 이동 중 ACTOR_PLACE None).
- 자유 문자열 → `PREDICATE_FREEFORM`, 알 수 없는 노드 → `PREDICATE_INVALID`, `CLOCK_MINUTE` 비 GTE/GT → `PREDICATE_TIME_POLLING`, spawn의 국면 경과 → `PREDICATE_ELAPSED_OUTSIDE_PHASE`, 달력 없는 날짜·시간대 → `PREDICATE_TIME_NO_CALENDAR`.

### 4.2 시간 → wakeup

시간 원자는 의존 키가 아니다. `next_wakeup(preds, ctx)` = 참값이 바뀔 수 있는 다음 분(> now): 국면 경과 = entered+n, 시간대 = 다음 시작·끝 경계, 절대 시각 = 그 분, CLOCK_MINUTE GTE v = v(GT = v+1). 사건 평가 뒤 그 분에 `EVENT_WAKEUP`(owner_epoch, id `wk_`+digest(epoch, 분)) 1건 — 이미 있으면 만들지 않는다. 대상 조건: resolve/cancel + (STICKY 유지 중이면 현재 국면 유지 조건, 아니면 전 국면 조건). 시간 조건 spawn 템플릿의 첫 wakeup은 `initial_template_wakeups`(순수, 적용 H06). 테스트: 06:00 경계 spawn·30분 국면 타이머가 1분 polling 없이 해당 분만 처리(`dependency_trace` 분 목록).

### 4.3 DependencyKey · DependencyIndex

키(정규 문자열): `actor:<id>:placement` · `scene:place` · `access:<cid>:state` · `info:<id>:validity` · `series:<sid>` · `cognition:<holder>:<info>` · `event:<iid>:lifecycle|phase|local:<k>` · `knowledge:<actor>:spatial` · `relationship:<key>` · `actor:<id>:registry`. 템플릿 키는 `$slot`·`$self` 패턴으로 컴파일하고 binding·instance로 구체화한다. `*` 세그먼트 = 와일드카드(EVENT_LIFECYCLE{template}), 컴파일 WARNING `DEPENDENCY_WILDCARD`.
`build_dependency_index(definition, world)` → spawn(템플릿×binding)·instance(ACTIVE)·series. `affected(changed)`만 정규 경로, `full_scan`은 초기화·디버그 전용. 변경 키는 두 투영의 정본 차이(`changed_keys`)에서 결정적으로 나오며 시계·예약 변화는 키가 아니다.

## 5. 사건

### 5.1 EventTemplate 스키마(raw → compiled)

```text
event_templates: {template_id: {
  version, name?, slots?: {name: {type: ACTOR|PLACE|INFO, candidates: [..]}}, distinct?: [[a,b]]|"all",
  spawn_when, repeat_policy: ONCE_PER_SESSION|ONCE_PER_BINDING|REPEATABLE, max_occurrences?,
  access_when?, phase_selector: {mode: PRIORITY|SCORE, stability: STICKY(기본)|BEST_MATCH|MONOTONIC, fallback_phase?},
  phases: {phase_id: {eligible_when, retain_when?, priority(필수), base_score?, score_terms?: [{when, points}], rank?, terminal?}},
  transition_cases?: [{case_id, from: phase|null(spawn)|"*", to, valid_when?, weight(양의 정수, 기본 1), steps: [Step]}],
  case_selection: DETERMINISTIC_WEIGHTED(기본)|FIRST_VALID, case_required?, resolve_when?, cancel_when?}}
compiled += slots 후보 정렬(ACTOR는 레거시 키 → ActorId), dependency {spawn, instance, wildcards}, has_time_predicates
```
정적 검증: 알 수 없는 키(자유 스크립트 없음)·슬롯 후보 실재·국면 참조·fallback 실재·**priority 동점 ERROR**(JSON 순서로 결정하지 않음)·MONOTONIC rank 필수·케이스 국면 참조·weight ≤0/비정수 ERROR·typed step 참조(제어 상태·장소·mode·구간·info·holder·`$step` 앞선 생성)·조건 참조·시간 조건·같은 케이스 고정 쓰기 충돌(`STEP_FIXED_WRITE_CONFLICT`). fallback·항상 참 국면이 없으면 WARNING.

### 5.2 EventInstance

```text
{"instance_id","template_id","template_version","bound_slots","lifecycle": ACTIVE|RESOLVED|CANCELLED,
 "current_phase","phase_entered_at","transition_ordinal","occurrence_ordinal","local_state","history"}
history entry = {"kind": SPAWN|TRANSITION|RESOLVED|CANCELLED, "at_minute", "transition_ordinal", "from_phase",
                 "to_phase", "case_id", "case_candidates", "cause_world_version"}
instance_id = "evt_" + stable_digest("event.instance.v1", narrative_seed, template_id, version, canonical binding, occurrence)
epoch = "<instance_id>:<transition_ordinal>"
```
현재 위치·인지·관계를 복제하지 않는다. 연산: `EVENT_INSTANCE_CREATE`(ACTIVE만) · `EVENT_PHASE_TRANSITION`(from·ordinal 기대값 불일치 거부) · `EVENT_LIFECYCLE_SET`(ACTIVE→RESOLVED|CANCELLED) · `EVENT_LOCAL_SET` · `EVENT_HISTORY_APPEND`.

### 5.3 spawn · repeat

영향받은 템플릿의 binding(슬롯 이름 정렬 × 후보 정렬 − distinct)마다 repeat 판정 → `spawn_when` TRUE → 초기 국면 selector(없으면 `SPAWN_NO_ELIGIBLE_PHASE`, 임의 국면 없음) → create + spawn 진입 케이스(from null). ONCE_PER_SESSION = 템플릿 1회(같은 평가에서도 정규 순서 첫 binding만), ONCE_PER_BINDING = binding당 1회, REPEATABLE = 같은 binding ACTIVE 없을 때 + max_occurrences.

### 5.4 국면 · 케이스 알고리즘

평가 순서(ACTIVE만): ① resolve_when ② cancel_when(TRUE면 lifecycle 변경 + 옛 epoch 예약 취소) ③ terminal이면 정지 ④ STICKY: 유지 조건(retain_when, 없으면 eligible_when) TRUE면 유지 — FALSE·UNKNOWN이면 selector ⑤ selector: PRIORITY = eligible 중 최고 priority, SCORE = base_score + TRUE 항 points 최대(동점 priority), MONOTONIC은 현재 rank 이상만, 없으면 fallback 또는 `NO_ELIGIBLE_PHASE`(현재 유지) ⑥ 대상이 다르면 전이(ordinal+1, 옛 epoch 예약 취소) ⑦ 대상 국면이 적용된 투영으로 케이스 후보 평가·선택 ⑧ step(저작 순서 인과 사슬) ⑨ 새 국면 wakeup. 전이는 케이스보다 먼저 확정된다 — 유효 케이스가 없어도 전이는 성립(`NO_VALID_CASE`/`CASE_REQUIRED_NONE_VALID` 진단).

**결정적 시드:** `seed = int(stable_digest("event.case.v1", narrative_seed, instance_id, transition_ordinal, from, to, sorted(valid case_ids), length=16), 16)`, `pick = seed % Σweight`, case_id 정렬 순 누적. FIRST_VALID = 저작 순서 첫 유효. 선택 case_id·후보는 history에 저장(재추첨 없음). Python hash·random 미사용(특성화 AST 검사). 후보 풀이 바뀌면 선택이 바뀔 수 있다(세계 분기 — 의도).

### 5.5 typed step registry

`SET_ACCESS_STATE` · `SET_ACTOR_PLACEMENT` · `START_MOVEMENT` · `CREATE_INFORMATION`(id = digest(instance, ordinal, case, step)) · `SUPERSEDE_INFORMATION` · `ACQUIRE_INFORMATION`(acquisition_id = `event:<iid>:t<n>:case:<c>:step:<s>:<holder>`) · `SET_COGNITION_BELIEF`(기본 권위 RULE — PC 거부) · `APPEND_RELATIONSHIP_DELTA` · `SET_EVENT_LOCAL` · `SCHEDULE_WAKEUP` · `SCHEDULE_EFFECT`(nested effect step, guard, epoch_bound) · `EMIT_SIGNAL`. 모든 step은 안정 `step_id`. 일반 dotted mutation step 없음(`STEP_TYPE_UNKNOWN`).

## 6. 예약 v2

- H03 7키 항목은 그대로 유효·읽기·소비(`normalized_entry`가 payload/guard_ref/owner_epoch = null, conflict_priority = 0으로 본다). load 시 재작성 없음.
- 신규 `SCHEDULE_CREATE`는 11키를 정규 작성: H03 7키 + `payload`(kind별 typed JSON) · `guard_ref` · `owner_epoch` · `conflict_priority`(기본 0).
- **`created_by_version` = 그 예약을 만든 투영의 source/baseline world version**(handler가 투영 기준 view의 world_version을 읽는다 — 진행 중 world_version은 바뀌지 않으므로 source). 대상 커밋 version이 아니다. 테스트 `test_r41_r42_r43_…`.
- `owner_epoch`: 사건 국면 epoch. 전이·해결 시 미래 예약을 `SCHEDULE_CANCEL`하고, 누락돼도 due 시 자기 검사 → `STALE_OWNER_EPOCH` 소비·no-op.
- `guard_ref` = `template|case|step|guard`. due 시 FALSE/UNKNOWN → 소비·no-op(재예약 polling 없음).
- `payload_ref` = 컴파일된 step 정체성 `template|case|step`(이동은 `movement:<id>`).

## 7. WorldProgression

### 7.1 API

```text
project_world_progression(definition, source(세션|ProjectedWorld), target_minute, *, base_ops=(), progress=None,
                          narrative_seed, resolved_tie_orders=None, max_rounds=32, max_transitions=8,
                          outer_origin="PLAYER_TURN") -> ProgressionResult
ProgressionResult = {status: COMPLETED|FOREGROUND_INTERRUPT|TIE_PENDING|WORLD_PROJECTION_CONFLICT|CLOCK_UNINITIALIZED|INVALID_TARGET,
                     source_minute, reached_minute, ops, projected_world, due_processed, events_changed, signals,
                     foreground_interrupt, same_minute_ties, diagnostics, dependency_trace}
committable = COMPLETED|FOREGROUND_INTERRUPT;  build_batch(...) → WorldMutationBatch(바깥 원인)
progress = {observer_actor?, scene_place?, activity?, movement_context?, activity_context?}
```
세션을 바꾸지 않는다(테스트로 확인). 예약 진행은 독립 커밋이 아니라 바깥 트랜잭션(outer_origin)의 묶음이다.

### 7.2 알고리즘

```text
now = 기준 시계(정본 범위 밖·미초기화 → CLOCK_UNINITIALIZED), target < now → INVALID_TARGET
base_ops 투영(라운드 BASE)
분 T 반복:
  1 due 일괄: due ≤ T 항목(정렬)을 같은 기준 상태에서 평가 → 단위(consume + 효과)
  2 즉시 연쇄: changed → affected(spawn·instance·series) → 단위 → 투영 → changed … 고정점
  3 연쇄가 T 이하 due를 새로 만들었으면 같은 T에서 다시 1
  4 IF_OBSERVABLE 관찰 가능 신호가 T에 있으면 FOREGROUND_INTERRUPT(그 분에서 정지)
  5 다음 due 분 ≤ target이면 CLOCK_ADVANCE_TO 후 반복, 아니면 target으로 진행하고 끝
```
라운드마다 한 묶음으로 투영하고, 합본 연산은 이전 라운드 전 연산을 인과 부모(장벽)로 더해 이어 붙인다. 끝에서 합본을 원본에 한 번 투영한 결과 digest가 라운드별 결과와 같음을 확인한다(불일치 = 엔진 버그 assert).

### 7.3 같은 분 · 충돌 · tie

한 라운드의 단위들을 함께 투영. 구조 충돌(MERGE_CONFLICT·PATH_OVERLAP·UNORDERED_INDEPENDENT_WRITES)이면 충돌 연산 → 단위 그룹:
- 우선순위가 다르면 낮은 단위의 효과 제거(due는 소비만 남김) — `SUPERSEDED_BY_PRIORITY`.
- 같은 최고 우선순위가 둘 이상: `tie_id = "tie_" + stable_digest(분, 정렬 effect_ids)`.
  - `resolved_tie_orders[tie_id]`가 있으면 그 순서로 인과 사슬 적용(뒤 효과가 최종).
  - 없고 대상 칸이 현재 장면 관련(관찰자·장면 장소 행위자 배치, 장면 장소에 닿는 구간의 접근 제어, scene_place)이면 `TIE_PENDING` + `SameMinuteNarrativeTie{tie_id, minute, effect_ids, allowed_orderings(≤4개면 전 순열), affected_targets}` — **H04는 순서를 고르지 않는다**.
  - 장면 밖이면 `WORLD_PROJECTION_CONFLICT` 안전 중단.
- handler 거부(구조 충돌 아님) 단위는 효과를 버리고(due는 소비 유지) `UNIT_REJECTED` 진단.
비구조 충돌·tie·중단 결과는 ops를 내지 않는다(committable 아님).

### 7.4 연쇄 안전 상한(구현 상수)

| 상수 | 값 | 의미 |
|---|---|---|
| `MAX_CASCADE_ROUNDS` | 32 | 한 분의 즉시 연쇄 라운드 상한 |
| `MAX_TRANSITIONS_PER_EVENT_PER_MINUTE` | 8 | 사건당 같은 분 전이 상한 |
| 세계 digest 반복 | — | 같은 분 안 상태 반복 = `CASCADE_OSCILLATION` |
| `MAX_DUE_MINUTES` | 10,000 | 한 투영의 서로 다른 due 분 처리 상한 |
초과 시 `WORLD_PROJECTION_CONFLICT`(서사 규칙이 아닌 안전장치).

### 7.5 NarrativeSignal

`EMIT_SIGNAL` → `{signal_id, instance_id, minute, cue, interrupt_policy: NONE|IF_OBSERVABLE, observability: TRUE|FALSE|UNKNOWN, observable, source_ref}`. observable_when 없음·UNKNOWN = 관찰 불가. 신호 존재 ≠ 사건 foreground. IF_OBSERVABLE + 관찰 가능이면 그 분에서 정지. cue를 LLM에 보내지 않는다(H05).

### 7.6 NPC 이동 wakeup

- `START_MOVEMENT`: 행위자가 AT_PLACE여야(아니면 `MOVEMENT_ORIGIN_UNKNOWN`/`MOVEMENT_ACTOR_IN_TRANSIT`). 행위자 SpatialKnowledge → `plan_known_routes`(명시 route는 지식·연속성 검증) → `NO_KNOWN_ROUTE`면 step 불충족(`MOVEMENT_NO_KNOWN_ROUTE`, 위치 변경·예약 없음 — 전지적 대체 없음) → `plan_actual_traversal`.
- 0분 구간(EDGE_END = now)은 TRANSIT 없이 같은 분에 통과(구간 지식·방문 기록). 첫 양수 구간에서 `ACTOR_TRANSIT_SET[now, end)` + `MOVEMENT_WAKEUP`(due = 구간 끝, id `mw_`+digest(movement, edge, end)).
- payload: `{actor_id, destination_place, mode_id, stance, stance_factor, capabilities, owner_event_instance, movement_id, edge_id, route_edge_ids}`. owner_epoch 없음(이동 의도는 NPC 소유).
- wakeup: 현재 배치가 같은 movement·edge의 TRANSIT인지 확인(아니면 `MOVEMENT_WAKEUP_STALE` 소비·no-op) → 도착 구간 지식 → 그 장소에서 **현재 지식으로 다시 KnownRoutePlanner + 실제 통과** → 다음 checkpoint 또는 AT_PLACE. 숨은 봉쇄·확인 필요 접근에서는 그 장소에 멈추고(`MOVEMENT_STOPPED`) 관찰한 접근 사실을 지식에 기록 — 다른 길로 우회하지 않는다. 최종 ETA 순간이동 없음.
- 반올림: 구간 경계마다 그 시점부터 새 traversal을 시작하므로 분 이하 누적은 checkpoint마다 다시 계산된다(§12 D9).

## 8. 접근 variant (H03 보강, A25)

컴파일러가 상태마다 `variant_id = "<rule>:<state>:base"`, override마다 `"<rule>:<state>:override:<index>"` 또는 명시 `override_id`(중복 ERROR). `AccessDecision.matched_variant`. `access_action_id(edge, control, state, policy, matched_variant)` = `"edge|control|state|policy|variant"`. 같은 문·상태·정책의 열쇠/통행증 행동은 다른 확인 토큰(테스트 `test_r58_…`).

## 9. production 연산과 실제 호출자

`PRODUCTION_REGISTRY.op_types()` 27종:

```
H03(12): ACCESS_STATE_SET ACTOR_PLACEMENT_SET ACTOR_TRANSIT_SET CLOCK_ADVANCE_TO CLOCK_INITIALIZE SCENE_PLACE_SET
         SCHEDULE_CANCEL SCHEDULE_CONSUME SCHEDULE_CREATE SPATIAL_ACCESS_KNOWLEDGE_UPSERT
         SPATIAL_EDGE_KNOWLEDGE_UPSERT SPATIAL_PLACE_KNOWLEDGE_UPSERT
H04(15): ACTOR_REGISTER ACTOR_RENAME INFORMATION_CREATE INFORMATION_VALIDITY_SET INFORMATION_LINK
         INFORMATION_SERIES_SET COGNITION_ACQUIRE COGNITION_UPSERT COGNITION_BELIEF_SET RELATIONSHIP_DELTA_APPEND
         EVENT_INSTANCE_CREATE EVENT_PHASE_TRANSITION EVENT_LIFECYCLE_SET EVENT_LOCAL_SET EVENT_HISTORY_APPEND
```
- 등록은 `world_mutation._register_production_handlers()` 한 곳. 임의 경로 setter 없음(특성화).
- **실제 production 호출자 0**: `main.py`·`prompts.py`·`cogs/*`·기존 `core/*`에서 H04 op 문자열·`project_world_progression`·`build_dependency_index`·`project_npc`·`build_supersede_ops`·`reevaluate_series`·`initial_template_wakeups` 참조 0건(grep + `test_h04_no_cutover`).

## 10. 테스트 결과

| 시점 | 결과 |
|---|---|
| BASE(`567f262`) | 830 passed · 0 failed · 0 xfailed · 0 XPASS |
| H04-A 전용 | 24 passed |
| H04-A 게이트 전체(`0b06061`) | **854 passed** |
| H04-B 전용 | 48 passed |
| H04 경계 특성화 | 5 passed |
| H04 targeted 합계 | 77 passed |
| **FINAL 전체**(`481143d`) | **907 passed** · 0 failed · 0 xfailed · 0 XPASS (특성화 55 · 결함 27 · 정책 825) |
| compileall | OK |
| 검증 루틴 ①②③⑤ | OK · OK · OK · cogs 9 / 명령어 44 / views 5 |
| ④ `verify_docs` | 문서 수치 OK(21항목) — `--fix`로 서브모듈 68→74(A)→77, DEVLOG 버전 반영 |
| ⑥ `scenario_check --all` | 5 × compiler v3 · ERROR 0(영도 WARNING 3 = 기존) |

추적성(H04-R##)은 테스트 이름에 ID를 붙였다(`test_r23_…` ~ `test_r61_…`). R06~R22는 `test_h04a_actor_info.py` 모듈 docstring 매핑, R62~R66은 `test_h04_no_cutover.py`·§11 스캔·전체 회귀.

## 11. 스캔

| 스캔 | 결과 |
|---|---|
| caller(H04 op·엔진 함수, 도메인 밖) | 0건 |
| provider | `generate_content`(.py) BASE 18 = FINAL 18, provider 호출 파일 집합 동일 |
| generic writer / eval | 코드 사용 0건(AST 검사: eval·exec·compile·`__import__`·setattr·hash·random 금지). grep 2건은 "eval 없음" 주석 |
| scenario / prompt diff | `git diff 567f262 -- scenarios prompts.py` 빈 출력 |
| quest authority / legacy / cogs | `git diff 567f262 -- core/quest.py core/quest_filter.py cogs core/extraction.py core/prompt.py core/cache.py core/places.py core/timeline.py main.py` 빈 출력. H04 모듈은 info_ledger·known_secrets·secret_known·narrative_plan을 읽지 않음(AST) |
| direct writer(H04 도메인 필드 대입) | `core/models.py` 생성자뿐(동적 setattr 경로는 H02와 동일) |

## 12. definition revision — compiler v2 → v3

| 시나리오 | v2 (`567f262`, H03 승인) | v3 H04-A (`0b06061`) | **v3 FINAL** (`481143d`) |
|---|---|---|---|
| 영도 | `72985bbdbd5758d64d1678de9c8b0a721100e455d8b056b958f4336d2942d995` | `72ab223acab32a22b11e1dd9290113dcffa7c1a75ae2c045126abc3ee71efcfd` | `101b4cfcf2e1a9d0b74c35b68f1c1aaefffd4e503bff4f44962e8aa0ee4a3ab2` |
| 무협 | `994e4105c8a2febb7b8c075c5ead1236ca3f57e90a8cab808a979c5cbf01b425` | `c0f1ae2d70a6a6249d62842f0cfbd811bc5ccd4865682a728551b474d6e0ea88` | `56f6f2ef0322c8eb9a228edc3bbede513376893116bae0ea14a4205d3c463e04` |
| 다크판타지 | `8037d42b6ad65be39f7cc5a14b148584e8ca6ae0617447a95c85f427594184b7` | `291ea186c6753b28fada01ce6864ffcad5284979fbf1c7a873938c45bfe8e517` | `c03dbb4221aa6666b301965c840beeb180811396ab89dbac9c44091fa5b15372` |
| 빈시나리오 | `38a98cab603adda835579745d251144b9990e0758d69bd81595376114cdd98d7` | `b657548b850d65e29308e5ca01c751e0a8d0f8dbc5d79c30af2cd97c788d5528` | `8a8c11f28dd0b2e75e06ba1a3aa6e6919f0948aafc15c3e872275dc6c999ceeb` |
| scenario.example | `3ba754baccd7148bb5e47f5ff1d0b5e52ff2ccd46646313f9a3e7077aa5a250f` | `a3ddac62e16449b0aad2839a38ab9f57ecb9b07cfd469657bd3c4cabc4f05f07` | `ee967f1c4ec515cc1b8ec6c4e6635c37ea332dd78dbcf453233180bd821ee636` |

예상된 변화: `compiler_version` 3, 정의에 `information_series`·`event_templates` 키, actor `profile_base`·`secret_info_id`, 정보 레코드 정규 필드. 5개 모두 legacy(`scenario_schema` 없음). 스냅샷 바인딩 0 → 이행 불필요.

## 13. 결정 · 편차

| # | 결정 | 이유 |
|---|---|---|
| D1 | `profile_base`에서 `resources`·`statuses`도 제외 | 레거시 런타임 초기값(세션 전개용)이지 프로필이 아니다. 현재 값은 레거시 읽기 adapter가 공급 |
| D2 | `AT_OR_AFTER_DATETIME`의 시각 필드 이름 `hhmm` | 노드 종류 키 `time`과 JSON 키가 겹치기 때문(B2 범위) |
| D3 | 인지 기록 없음 = known `"NONE"`, 사건 없음 = known `"NONE"`, 이동 중 ACTOR_PLACE = known None | 정본이 '없음'을 의미 — UNKNOWN은 추적되지 않는 사실(위치 미추적·EVENT_LOCAL 미기록·미결합 슬롯)에만 |
| D4 | `CLOCK_MINUTE`는 GTE/GT 정수 리터럴만 | 다른 비교는 매분 polling 필요(A20) |
| D5 | 케이스 weight는 양의 정수 | B4 허용, 정수 누적이 프로세스 독립 |
| D6 | 국면 priority는 두 모드 모두 필수·전 국면 고유 | PRIORITY 동점 금지 + SCORE 동점 해소를 저작이 명시(A15·§38~39) |
| D7 | 케이스 step은 저작 순서 인과 사슬 | 같은 케이스에서 생성 정보 → 획득 → 믿음 같은 의존을 정본 연산 순서로 표현(같은 계층이면 앞 결과를 못 봄) |
| D8 | 진행 투영에 `outer_origin` 인자(기본 PLAYER_TURN) | 예약 진행은 독립 SCHEDULED 커밋이 아니라 바깥 트랜잭션 안(DESIGN 04 §2~3). world_version 원인 종류를 늘리지 않음 |
| D9 | NPC 이동은 checkpoint마다 traversal을 새로 시작 | 각 wakeup에서 실제 상태·지식을 다시 검증해야 해 분 이하 누적을 구간 경계에서 새로 센다(H03 경로 전체 반올림은 한 traversal segment 안에서 유지) |
| D10 | 이동 wakeup에 owner_epoch 없음 | 이동 의도는 NPC 소유 — 사건 국면이 바뀌어도 진행 중 이동은 계속(사건이 멈추려면 다른 step으로) |
| D11 | 같은 분 tie 판정의 '현재 장면 관련' = 관찰자·장면 장소 행위자 배치·장면 장소에 닿는 구간 접근·scene_place | A17의 "current-scene consequential"을 정본 칸 단위로 구체화. 그 외 동률 비호환은 안전 중단 |
| D12 | 기존 테스트 갱신(완화 아님) | compiler 3·capability 전부 검증·신규 예약 11키·access variant·H04 도메인 모듈 허용 — 각 줄에 주석 |
| D13 | 계열 값으로 시간·관찰 문맥 ref 금지 | 계열은 추적 가능한 정본 사실이어야 재평가가 의존 키로 성립 |
| D14 | 컴파일러는 템플릿 하나에 ERROR가 있으면 정의 전체를 내지 않는다(기존 규칙) | H01 이래 ERROR = definition None |
| D15 | wakeup 대상 조건을 현재 상태에 영향 있는 것으로 한정 | 불필요한 wakeup을 줄이되, 시간 밖 원인의 변화는 DependencyIndex가 재평가를 부른다 |

STOP 조건(C) 해당 없음: eval/스크립트 불필요, dotted writer 불필요, PC 믿음 자동 추론 불필요, NPC 이동에 전지적 그래프 불필요, 프롬프트 수정 불필요, 새 세션 필드 불필요(H02 정본 필드 사용), 7키 예약 호환 유지, H02/H03 불변식 유지.

## 14. 새로 확인한 사실

1. H02 `WorldMutationBatch`는 인과 부모가 **같은 묶음의 앞선 연산**이어야 한다 — 라운드 투영 묶음과 합본 묶음의 부모 집합을 분리해야 했다.
2. `outer_origin`은 world_version 원인 종류(3개)만 허용 — 진행 투영은 바깥 트랜잭션 원인을 받아야 한다.
3. 국면 경과 조건은 새 국면 진입 시점부터 다시 잰다 — "경과 후 진입"한 국면의 eligible이 진입 직후 FALSE가 되므로, 그런 국면에는 명시 retain_when이 필요하다(저작 지침).
4. 영도 등 5개 시나리오는 legacy라 v3 정보·사건 검증은 실제 저작에 적용되지 않는다(사건 0·명시 정보 0·계열 0).
5. ONCE_PER_SESSION은 같은 평가 안에서도 binding이 여러 개면 동시 spawn될 수 있었다 — 정규 순서 첫 binding만 spawn하도록 했다.

## 15. H05가 믿어도 되는 불변식

1. `project_world_progression`은 순수하다. ops는 바깥 트랜잭션 묶음으로만 적용하고, committable(COMPLETED·FOREGROUND_INTERRUPT)일 때만 묶음을 만든다.
2. `TIE_PENDING`이면 H05 묘사 관찰이 `resolved_tie_orders[tie_id]`로 순서를 정하고 투영을 다시 돌린다. H04는 순서를 고르지 않는다.
3. `FOREGROUND_INTERRUPT`는 `reached_minute`에서 멈춘 투영이며 `foreground_interrupt.cue`가 묘사 입력 후보다(H04는 LLM에 보내지 않음).
4. PC 믿음은 `PLAYER_EXPLICIT`·`RULE_FORCED`로만 — 플레이어 선언 해석(H05)이 PLAYER_EXPLICIT 출처다. 노출·묘사는 획득만 만든다.
5. 획득 id는 안정 원인에서 만든다(재적용 멱등). 정보 명제는 불변, 바뀌면 supersede.
6. CONFIRM 확인 토큰 = `access_action_id`(variant 포함) — 중단 결과의 `action_id`를 그대로 쓴다.
7. 예약 `created_by_version`은 source/baseline version, 낡은 타이머는 `owner_epoch`로 판정한다.
8. 새 typed 연산을 만드는 producer는 정의를 들고 참조 실재(Place·Edge·control·Actor·Info)를 검증한다(handler는 형태·상태 전이만 — 정의 미바인딩, H06).
9. H03 불변식 유지: 시계 ≥ 1440, pending-only, 7키 예약 호환, base quiescence, KnownRoute 인식 경계, 숨은 우회 없음, TRANSIT 반열림, provenance 누적, 저작 가시성.
10. 플레이어·cogs 경로는 아직 H03/H04 엔진을 부르지 않는다 — 배선은 H05가 처음 한다.

## 16. 재현

```bash
git checkout 481143daedcdc4d80337113fe576233ab8362485
pip install -r requirements-dev.txt
python3 -m compileall -q core cogs main.py prompts.py tools tests
python3 -m pytest tests/ -q                      # 907 passed
python3 tools/scenario_check.py --all            # 5 × compiler v3, ERROR 0
python3 tools/verify_docs.py                     # 문서 수치 OK
git diff 567f262 481143d -- scenarios prompts.py core/quest.py core/quest_filter.py cogs main.py   # 빈 출력
```
