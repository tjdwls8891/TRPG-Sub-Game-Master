# H04 2차 독립 검수 보정 번들 (H04-R2)

> 상태: **H04 CANDIDATE — R2 보정 적용, 재검수 대기**. H05 **NOT STARTED**.
> 기준(보정 시작) HEAD: `bc559c41beda5bc4a40e10a35990f1ebac3f871a` (H04 코드 `481143d`, 907 passed)
> 브랜치: `claude/h04-actor-info-event` · 최종 커밋 SHA는 제출 ZIP의 `commit_info.txt`(이 파일은 그 커밋에 포함되므로 자기 SHA를 담을 수 없다)
> 이 번들은 `handoff/H04_COMPLETION_BUNDLE.md`를 대체하지 않고 보충한다(기존 번들 미수정). H04 설계·결정 게이트·추적성 매트릭스는 그대로 권위다.

## 0. 요약

| blocker | 결과 | 변경 파일 |
|---|---|---|
| H04-R2-01 PC 믿음 권위 우회 | 닫힘 | `core/cognition.py` |
| H04-R2-02 case_required 미강제 | 닫힘 | `core/definition_compiler.py`, `core/world_progression.py` |
| H04-R2-03 step 순차 가시성 결여 | 닫힘 | `core/events.py`, `core/world_progression.py` |
| H04-R2-04 명시 route 연속성·MovementError 누출 | 닫힘 | `core/definition_compiler.py`, `core/world_progression.py` |
| H04-R2-05 같은 분 3+ 효과 중재·연결 성분 | 닫힘 | `core/world_progression.py` |

- 신규 테스트 `tests/policy/test_h04_r2_corrections.py` **20건**: 보정 전 코드에서 **15 failed / 5 passed**(통과 5건은 기존 동작 보존 단언), 보정 후 **20 passed**.
- 전체 회귀 **927 passed**, 0 failed, xfail/XPASS 0 (특성화 55 · 결함 27 · 정책 845).
- 버전 불변: `__version__` 7.2.0 · `SCHEMA_VERSION` 4 · `SESSION_FIELDS` 95 · turn-history 3 · CommitPlan 2 · compiler 3. 정본 필드 11+2, production typed 연산 27 불변.

## 1. 블로커별 수정

### H04-R2-01 — PC 믿음 권위 우회

- **원인**: `h_cognition_upsert`가 인지 하향(CONTENT→EXISTENCE) 때 기존 `belief`를 권위 검사 없이 `None`으로 바꿨다. PC 믿음 권위 검사(A9)는 `COGNITION_BELIEF_SET`에만 있었다.
- **수정**: `core/cognition.py` — 권위 관문 `_check_belief_authority(op, view, holder, auth)`를 새로 두고 `h_cognition_belief_set`·`h_cognition_upsert` 둘 다 이것만 쓴다(단일 의미).
  - UPSERT payload에 선택 키 `authority`를 허용한다.
  - 하향으로 믿음이 사라지는 경우 PC면 `authority ∈ {PLAYER_EXPLICIT, RULE_FORCED}`가 필수다. 없거나 다른 권위면 거부되고 기존 믿음이 보존된다.
  - 허용된 경우 `belief_provenance`에 `{belief: None, authority, cause_ref}`를 남긴다.
  - NPC는 기존 동작 그대로다(cause_ref만으로 하향·믿음 소멸, 기록 형태 불변). `authority`를 주면 같은 관문으로 검증한다.
  - 믿음이 없는 PC의 하향은 믿음 변경이 아니므로 기존대로 cause_ref만 요구한다.
- **테스트**:
  - `test_r2_01_pc_awareness_downgrade_cannot_clear_belief_without_pc_authority` — 권위 없음·RULE·AUTHORED·알 수 없는 권위 모두 거부, CERTAIN·CONTENT 보존.
  - `test_r2_01_pc_belief_cleared_only_through_pc_authority_with_provenance[PLAYER_EXPLICIT|RULE_FORCED]` — 허용 권위로만 소멸, provenance 기록. 기존 경로(BELIEF_SET None → 하향)도 유지.
  - `test_r2_01_pc_without_belief_and_npc_downgrade_unchanged` — 보존 단언.
- **계약**: PC 믿음의 모든 변경(값→None 포함)이 PLAYER_EXPLICIT·RULE_FORCED 관문 하나를 지난다. 두 연산 사이에 우회 경로가 없다.

### H04-R2-02 — case_required 미강제

- **원인**: 컴파일러가 `case_required`를 기록만 했다. 런타임은 유효 케이스가 0이면 `CASE_REQUIRED_NONE_VALID` 진단만 남기고 전이를 성립시켰다. 후보 자체가 없으면 진단도 없었다.
- **수정**:
  - **컴파일**(`definition_compiler._uncovered_transitions` + 템플릿 컴파일): `case_required=true`이면 구조상 일어날 수 있는 모든 전이에 케이스 후보가 있어야 한다. 후보가 하나도 없는 전이마다 **ERROR `EVENT_CASE_REQUIRED_UNSATISFIABLE`**를 낸다. 케이스가 아예 없는 경우도 여기에 포함된다.
    - 대상 전이는 spawn 진입(from None)과 비종결 국면 → 다른 국면이다. MONOTONIC은 rank가 줄지 않는 전이만 본다.
    - `from:"*"`는 spawn도 덮는다(런타임 `case_candidates`와 같은 의미).
  - **런타임**(`world_progression._Engine._transition`): `case_required`인데 케이스를 고를 수 없으면 기존 안전 중단 경로(`_Stop`)로 투영 전체를 끝낸다.
    - 결과: `status=WORLD_PROJECTION_CONFLICT`, `ops=()`, `projected_world=None`, 진단 `reason=CASE_REQUIRED_NONE_VALID`.
    - 세션을 직접 바꾸지 않고, 예외도 API 밖으로 나가지 않는다.
  - `case_required=false` 경로(전이 유지 + `NO_VALID_CASE` 진단)는 바꾸지 않았다.
- **테스트**:
  - `test_r2_02_case_required_without_cases_is_compile_error` — 케이스 없음 → 거부. 구조상 덮이지 않은 국면 → 거부. 덮으면 통과.
  - `test_r2_02_case_required_runtime_no_valid_case_is_safe_abort` — 런타임 유효 케이스 0 → 안전 중단, 부분 변이·세션 변화 없음.
  - `test_r2_02_optional_no_case_and_required_valid_case_unchanged` — optional 동작, 결정적 가중 선택 유지.
- **계약**: 정적으로 판정 가능한 위반은 validator ERROR, 런타임 위반은 명시적 실패다. required 템플릿이 케이스 없이 조용히 전이하는 경로가 없다.

### H04-R2-03 — 같은 케이스 step의 순차 가시성

- **원인**: `run_steps`가 인과 부모만 이어 붙였다. 모든 step이 같은 `StepContext.world`(전이 전 투영)와 같은 가상 인스턴스로 계획돼, 뒤 step의 계획 시점 읽기가 앞 step 결과를 보지 못했다.
- **수정**(`core/events.py`):
  - `run_steps(..., prelude_ops=())` — 케이스보다 먼저 확정된 전이·생성 연산(head)을 먼저 **순수 임시 투영**한다.
  - 그 뒤 각 step의 연산을 만들 때마다 `_interim_world`로 `sctx.world`에 임시 투영한 다음 다음 step을 계획한다.
  - `_observe`는 `sctx.world`를 바꾸고, 인스턴스의 `local_state`를 투영 결과에서 가져온다. step이 바꿀 수 있는 인스턴스 필드는 local_state뿐이고, 나머지는 기존 가상 인스턴스 의미를 유지한다.
  - 임시 묶음은 연산 **사본**을 쓰며 부모는 같은 임시 묶음 안의 것만 남긴다. **반환 연산의 op_id·인과 부모는 그대로**이고 최종 커밋 경계도 바깥 묶음 그대로다.
  - 임시 투영이 실패하면 `STEP_PROJECTION_FAILED` 진단을 남기고 이후 step을 중단한다. 이미 만든 연산은 최종 라운드 투영이 기존 정책(`UNIT_REJECTED`)으로 처리한다. 임시 투영은 `ProjectedWorld` 사본뿐이라 정본 부분 효과가 없다.
  - `world_progression._transition`은 `prelude_ops=head`를 넘긴다. 예약 효과 단일 step 경로(`run_step`)는 바꾸지 않았다.
- **테스트**:
  - `test_r2_03_signal_step_observes_event_local_set_by_previous_step` — SET_EVENT_LOCAL → 조건부 EMIT_SIGNAL이 TRUE를 관찰하고 foreground로 멈춘다.
  - `test_r2_03_movement_step_plans_from_placement_set_by_previous_step` — SET_ACTOR_PLACEMENT(항구) → START_MOVEMENT가 항구에서 출발한다(TRANSIT `e_hh#fwd`). 정본 세션 불변.
  - `test_r2_03_interim_projection_keeps_final_op_identity_and_parents` — 인과 사슬(create→flag→put→go) 유지, 임시 사본 비혼입, 합본 재투영 = 결과 세계.
  - `test_r2_03_information_chain_still_sequential` + 기존 `test_event_steps_information_cognition_relationship_chain` 통과.
- **계약**: "같은 케이스의 step은 저작 순서대로 실행되며 뒤 step은 앞 step이 만든 세계 상태를 관찰한다"가 계획 시점에도 성립한다.

### H04-R2-04 — 명시 route 연속성·MovementError 누출

- **원인**: 컴파일러는 구간 존재만 확인했다. 런타임은 지식·첫/끝 구간만 보고 중간 `edge[i].to == edge[i+1].from`을 보지 않아, `plan_actual_traversal`의 `MovementError`가 `project_world_progression` 밖으로 탈출했다. 같은 계열로 `plan_known_routes`도 동적 데이터(정의되지 않은 장소·잘못된 배수)가 들어오면 `MovementError`를 던질 수 있었다.
- **수정**:
  - **컴파일**(START_MOVEMENT step): route 중간 연속성을 검사하고, 목적지가 고정 PlaceId면 끝 구간 도착지도 검사한다. 위반 시 **ERROR `STEP_ROUTE_DISCONTINUOUS`**. 출발지는 런타임 배치라 정적 검사하지 않는다.
  - **런타임**(`world_progression._movement_ops`):
    - `route_is_continuous`로 명시 route 구조를 먼저 검증한다. 끊겼으면 `MOVEMENT_ROUTE_INVALID` 진단을 내고 이동하지 않는다(대체 경로 없음).
    - `plan_known_routes`·`plan_actual_traversal` 호출을 `MovementError`에 대해 방어한다(`MOVEMENT_PLAN_INVALID`/`MOVEMENT_ROUTE_INVALID`). 이 경우 연산을 만들지 않거나, 도착 wakeup이면 기존과 같이 현재 장소 배치만 만든다.
    - 기존 지식·첫/끝 검사(`MOVEMENT_ROUTE_CONSTRAINT_UNKNOWN`)와 `NO_KNOWN_ROUTE` 정책은 그대로다.
- **테스트**:
  - `test_r2_04_discontinuous_explicit_route_is_compile_error` — 반례 `e_hm#fwd(항구→시장) · e_hh#fwd(항구→산복도로) · e_hc#fwd(산복도로→성당)` 거부, 목적지 불일치 거부, 정상 route 통과.
  - `test_r2_04_runtime_broken_route_contained_without_partial_mutation` — 컴파일 뒤 정의에 끊긴 route를 주입하고, 행위자가 모든 구간을 아는 상태에서 예외 없음·이동/전이/지식 연산 없음·세션 불변.
  - `test_r2_04_runtime_invalid_destination_or_factor_contained` — 없는 목적지·배수 0 주입 시에도 예외 없음·불변.
  - `test_r2_04_valid_route_and_unknown_route_policy_unchanged` — 정상 route 동작, 모르는 route는 전지적 대체 금지.
- **계약**: 정적 위반은 컴파일 거부, 동적 위반은 진단과 함께 미이동한다. 진행 API 밖으로 `MovementError`가 나가지 않고 부분 변이도 없다.

### H04-R2-05 — 같은 분 3+ 효과 중재·연결 성분

- **원인**: 충돌 성분마다 낮은 단위를 drop 표시만 한 상태에서, 같은 패스 안에 최고 우선순위 단위가 둘 이상이라는 이유로 tie를 만들었다. 동일 효과인 A·C(P2 OPEN)도 tie가 됐다. 또 `_conflict_groups`는 다리 충돌이 기존 두 그룹을 이을 때 첫 그룹에만 합쳐, 한 단위가 두 그룹에 남았다.
- **수정**(`world_progression._Engine`):
  1. 구조 충돌 탐지(`project`).
  2. `_conflict_groups` — 다리 충돌이 닿는 모든 기존 그룹을 하나의 연결 성분으로 병합한다.
  3–4. `_priority_drops` — 직접 충돌 상대 중 더 높은 우선순위가 있으면 '지배됨'으로 본다. 지배되지 않은 상대에게 지배되는 단위만 이번 패스에 제거한다(그 상대는 확실히 남는다). 제거된 단위는 소비 연산만 남는다(`SUPERSEDED_BY_PRIORITY`).
  5. 제거가 있으면 **tie 판단 없이 재투영해 충돌을 다시 계산한다**. 제거된 단위하고만 충돌하던 효과는 살아남는다.
  6. 제거할 것이 없을 때만(성분 전체가 같은 우선순위) 남은 실제 비호환 성분을 처리한다 — 장면 관련이면 `SameMinuteNarrativeTie`, 장면 밖이면 안전 중단. 해소 순서가 주어지면 기존대로 순서 적용.
  - 동일 우선순위 + 동일 효과는 `project`가 충돌로 보지 않으므로 처음부터 호환이다.
- **테스트**:
  - Case A `test_r2_05_case_a_lower_dropped_then_equal_top_effects_are_compatible` — P2 OPEN · P1 BARRED · P2 OPEN → OPEN, tie 없음, B만 SUPERSEDED, 전부 소비.
  - Case B `test_r2_05_case_b_real_top_conflict_after_drop_is_foreground_tie` — P2 OPEN · P1 BARRED · P2 LOCKED → tie `(due:a, due:c)`, 해소 순서 적용.
  - Case C `test_r2_05_case_c_conflict_groups_are_connected_components` — A–B, C–D, B–C(다리 마지막) → `{A,B,C,D}` 하나, E–F 별개, 각 단위 한 성분.
  - Case C `test_r2_05_case_c_bridge_component_through_real_projection_single_tie` — 실제 `project`+`apply_units`로 같은 계층 다리 충돌 → tie 4효과(24 순서). 성분 안 낮은 우선순위만 제거(B) 후 장면 밖 C–D 실제 충돌 → 안전 중단. 지배 사슬 A3>B2>C1에서 B만 제거, C 생존.
  - Case D `test_r2_05_case_d_offscreen_real_top_conflict_still_safe_abort` — 장면 밖 실제 충돌 안전 중단 유지, 장면 밖 동일 효과는 호환.
- **계약**: 탐지 → 연결 성분 → 우선순위 비교 → 낮은 효과 제거 → 재투영 → 실제 비호환만 tie/중단. 동일 우선순위만으로 tie가 생기지 않는다.

## 2. 테스트

| 구분 | 결과 |
|---|---|
| 신규 보정 테스트(`test_h04_r2_corrections.py`) | 20건 — 보정 전(`bc559c4` + 새 테스트) **15 failed / 5 passed**, 보정 후 **20 passed** |
| H04 대상 전체(h04a 24 + h04b 48 + no_cutover 5 + R2 20) | **97 passed** |
| 전체 pytest | **927 passed**, 0 failed, xfail/XPASS 0 (특성화 55 · 결함 27 · 정책 845) |

보정 전 통과 5건은 의도적인 보존 단언이다: NPC·믿음 없는 PC 하향, optional·결정적 케이스, 정보 사슬, 정상/모르는 route, Case B(실제 충돌은 여전히 tie).

## 3. 검증·스캔

| 항목 | 결과 |
|---|---|
| compileall | OK |
| 검증 루틴 ①②③⑤ | OK · OK · OK · cogs 9 / 명령어 44 / views 5 |
| ④ verify_docs | 문서 수치 OK(21항목) |
| ⑥ scenario_check --all | 5 × compiler v3 · ERROR 0 · WARNING 3(영도, 기존) · definition revision 5개 **불변** |
| provider | 운영 코드(main·prompts·cogs·core·tools) `generate_content` BASE 18 = FINAL 18, 파일 집합 동일 |
| H04 production caller | 0건(main·prompts·cogs·비도메인 core) |
| eval / 자유 실행 | 변경 4모듈 AST: eval·exec·compile·`__import__`·setattr·hash·random 0건 |
| authority-scope diff | `scenarios`·`prompts.py`·퀘스트·cogs·extraction·prompt·cache·places·timeline·main·io·models·world_state·turn_history·commit_coordinator·commit_journal·turn_preparation·constants·world_mutation·canonical_mutation·movement·spatial·access — diff **0줄** |

## 4. 범위 확인

- H05 NOT STARTED · provider 추가 없음 · PLAYER_TURN production wiring 없음.
- 시나리오·퀘스트·프롬프트 의미 변경 없음(파일 diff 0, definition revision 불변).
- 신규 canonical session field 없음 · schema/history/CommitPlan/compiler 버전 변경 없음. 앱 버전 7.2.0 유지(H03 보정 선례 — 미승인 WP 내부 보정).
- 문서: `CLAUDE.md` 테스트 수(907→927)·H04 상태 표기만 갱신. 과거 번들 미수정.

## 5. 작업 중 발견 — 별도 분류(이번에 고치지 않음)

| # | 내용 | 분류 |
|---|---|---|
| N-1 | `tests/policy/test_h04b_events_progression.py:535`가 공유 픽스처 `SIMPLE`의 중첩 `phases`를 얕은 복사로 고쳐 써서, 실행 순서에 따라 다른 모듈 테스트가 오염될 수 있다(R2 테스트는 독립 픽스처 `BASE`로 회피). 운영 코드 영향 없음 | 테스트 위생 — H04 계약 blocker 아님. 기존 테스트 수정은 범위 밖이라 보류 |
| N-2 | `world_mutation.project`(H02)는 충돌이 처음 나온 인과 계층에서 멈춘다. 따라서 같은 분 중재가 보는 성분은 그 계층의 충돌 기준이다. 더 깊은 계층에서만 드러나는 다리 충돌은 tie 해소·재투영 뒤 다음 단계로 나타난다(안전성 유지 — 추측 없음, 장면 밖은 중단) | H04 계약 위반 아님(단계적 제시). H02 동작이므로 변경하지 않음. H05에서 tie 제시 UX 설계 시 참고 |

## 6. 제출물 구성

`H04_R2_CORRECTION_BUNDLE.md`(이 문서) · `diff_bc559c4_to_final.patch` · `changed_files/`(운영 4 + 테스트 1 + CLAUDE.md) · `targeted_before_fix.txt` · `targeted_after_fix.txt` · `h04_all_tests.txt` · `full_regression.txt` · `marker_counts.txt` · `validation_scans.txt` · `scenario_check_all.txt` · `commit_info.txt`
