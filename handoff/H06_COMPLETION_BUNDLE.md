# H06 완료 노트 — 정의 바인딩 · genesis 초기화 · 캐시 고정 원천 · 재시작 · 재서술/되감기 · 관리자 편집 · 안전 종료

> 시작 `197a9ace9417c222eb4c7eb3a495619acc2bbe3e`(H05 R3, 994 passed) · 브랜치 `claude/h06-lifecycle`
> 최종 SHA는 제출 ZIP `commit_info.txt`(이 파일은 그 커밋에 포함). 버전 **7.4.0**(MINOR — 스키마·이력·CommitPlan·compiler 유지).
> H07 NOT STARTED.

## 0. 고정된 사용자 결정(2026-10-08)

1. DYNAMIC은 **단일 PC 전용**이다. 다인 세션은 LEGACY이며, 다인 동적 의미는 만들지 않았다.
2. `prompts.py`는 무변경이다. 동적 세계 문맥은 H05 런타임 오버레이가 그대로 싣는다.
3. 세션 PC 인지(EXISTENCE/CONTENT)만 사건 조건이 될 수 있다. PC 믿음 조건은 지원하지 않는다(컴파일 오류).

## 1. 핸드오프와 현재 소스의 불일치(가장 좁은 해법을 택함)

| # | 핸드오프 가정 | 현재 소스 | 선택 |
|---|---|---|---|
| D1 | 시작 선택 → 초기화 → 캐시 → 인트로 | 버튼 플로우는 **캐시 업로드가 시작 선택 전**(`on_open_time_done`)이다 | DYNAMIC 대상 세션만 캐시 업로드를 시작 선택·genesis 뒤로 미룬다. LEGACY 순서는 그대로다 |
| D2 | 정의의 초기화 정보로 초기 세계를 만든다 | 정의에 시작 날짜·NPC 초기 위치·초기 인지 저작이 없다. 시작 틀 facts는 슬롯 템플릿이며, 컴파일러 주석(DESIGN 08 §20)은 "기계 시각·위치로 해석하지 않는다 — 정량 시작점은 사람이 저작"이라고 한다 | 시작 틀에 선택적 저작 필드 **`world_start`**({place, date, time, knowledge_seed?})를 두고 컴파일러가 검증한다. 저작된 틀에만 출력 키가 생기므로 **기존 5개 시나리오 revision은 그대로**(대조 확인). 미저작이면 LEGACY |
| D3 | 재시작은 바인딩 스냅샷을 읽는다 | 재시작은 `scenario_data`를 **현재 파일**에서 읽고 캐시·프롬프트가 그것을 쓴다. H05 `load_definition`에는 재컴파일 대체 경로가 있었다 | 바인딩 세션은 재시작 때 `scenario_data`를 스냅샷 `legacy_source`로 되돌린다. 정의는 스냅샷만 읽는다(재컴파일 대체 제거). 손상·부재 시 BLOCKED |
| D4 | 재서술은 의미 선택을 보존한다 | 턴 이력에 그 턴의 `definition_revision`이 없다 | H05 판단 기록의 의미 기록에 revision을 함께 남긴다(스키마 변화 없음) |
| D5 | 관리자 편집은 기존 운영자 진입점 경유 | 동적 세계를 편집하는 운영자 명령이 없다 | 좁은 서비스(`admin_world_edit`)만 추가했다. 명령은 추가하지 않았다(명령어 44 유지) |
| D6 | 진행 엔진의 기반 연산은 모두 적용된다 | 엔진은 거부된 기반 단위를 진단으로만 남기고 버린다 | 초기화에서는 `UNIT_REJECTED(base)`를 충돌로 판정한다(PC 등록 충돌 시 등록 없는 genesis가 만들어지던 반례 수정) |

## 2. H06-A — 정의 바인딩 · genesis · 자격

- 새 모듈 `core/world_lifecycle.py`가 생애주기의 유일한 owner다.
  - 흐름: compile(세션 `scenario_data`) → `definition_snapshot.write_snapshot` → `definition_revision` 바인딩(**유일한 writer**) → `resolve_start_choices`(저작 world_start만) → `build_initial_world_plan`(typed) → `CanonicalMutationCoordinator(INITIALIZATION)`.
  - 초기 세계: PC ActorId 등록, 시계, PC 배치·장면, PC 공간 지식(시작 장소 방문 + 선택 seed), 시간 조건 spawn wakeup, 시작 분 전수 검사 1회(H04 `initial_full_scan` — INITIALIZATION 전용 최소 확장).
  - 접근 상태는 정의 initial_state가 기본값이므로 따로 심지 않는다.
- 멱등성: `initialization_id` = digest(세션, revision, 시작 선택)이다. 이미 바인딩된 세션은 ALREADY를 돌려준다(genesis 재생성 없음).
- 실패 처리: 충돌·정지 상태 실패가 나면 바인딩·생애주기·정본을 모두 되돌린다(부분 초기화 없음).
- 생애주기 `world_lifecycle`: None(LEGACY) / INITIALIZED / OPEN / CLOSED.
  - 첫 캐시 실패 시 INITIALIZED에 머물고 인트로를 하지 않는다. 재시도 버튼(`DynamicOpenRetryView`)이 같은 정체성으로 이어 간다.
  - 인트로가 끝나면 OPEN이 된다.
- 세계 모드(`session_world_context`): DYNAMIC = 단일 PC + 스냅샷 바인딩 + 필수 capability + **OPEN** + 시계 + PC 배치.
  - 초기화된 세션에서 정의를 읽을 수 없거나 PC가 늘면 **BLOCKED**다. `_process_actions`가 턴을 거부한다(조용한 LEGACY 전환 없음).
  - 초기화 뒤 참가(`JoinModal`·`!참가`)는 거부한다.

## 3. H06-B — 세션 PC 인지 사건 조건

- holder `"$session_pc"`(`world_refs.SESSION_PC`)는 COGNITION_AWARENESS에서만 허용한다. 정본 `actor_registry`의 PC가 정확히 하나일 때만 해석하고, 아니면 UNKNOWN이다(이름 검색 없음).
- 의존 키 `cognition:actor:$session_pc:<info>`는 `build_dependency_index`가 같은 규칙으로 구체화한다(spawn·instance·series). 그래서 PC 인지 변화는 그 키에 의존하는 사건만 깨운다.
- `COGNITION_BELIEF` + `$session_pc`는 컴파일 오류다. NPC 인지·믿음 조건과 PC 믿음 변경 권위(PLAYER_EXPLICIT·RULE_FORCED)는 바뀌지 않았다.

## 4. H06-C — FrozenCacheBuildSource

- `core/cache_source.py`: 짧은 io 락 안에서 깊은 복사로 캡처한다.
  - 캡처 대상: 바인딩 세션의 스냅샷 원문, 세션 사본, 기준 world_version, 이력 표식, 압축 기억, 세션 NPC 기준.
  - 조립(토큰 계산)과 provider 생성은 락 밖에서 한다.
- `cache_lifecycle._create_locked`:
  - 생성 사실에 `generation` = {definition_revision, base_world_version_id, history_marker}을 남긴다.
  - 생성 중 revision이 바뀌면 활성화하지 않고 기존 단일 finalizer로 정리한다(`CacheGenerationStale`, 생성·보관 비용 사실은 각 1회).
- `update_session_cache_state(frozen=)`: 기준선(NPC·압축 기억·이력 표식)을 원천 기준으로 잡는다. 생성 중 새로 쌓인 압축 기억은 런타임 쪽에 남긴다.
- `!캐시 재발급`: 바인딩 세션에서는 원본 재로드·`expand_default_npcs(replace=True)`를 생략한다.
- 재무 모델은 바꾸지 않았다(선불·환급·보관 정산은 WP-F 그대로).

## 5. H06-D — 재시작

- `restore_sessions_from_disk`에 두 단계를 추가했다.
  1. 바인딩 세션은 `scenario_data`를 스냅샷 원문으로 되돌린다(실패하면 BLOCKED).
  2. 커밋 복구 뒤 `world_lifecycle.restore`를 실행한다 — `canonical_mutation.recover`로 INIT/ADMIN 저널을 종결하고 스냅샷을 검증한다.
- 오프라인 시간 진행·예약 따라잡기는 없다. 캐시 복구 재생성은 세계를 바꾸지 않는다.

## 6. H06-E — 재서술 · 되감기

- `turn_world.rerender_world_intent`가 재서술 가능 여부를 판정한다. 상태를 바꾸기 전에 아래 조건을 검사한다.
  - DYNAMIC 세션일 것.
  - 의미 기록(`action_intent.semantic`)과 그 턴의 revision이 있고, revision이 현재 바인딩과 같을 것.
  - 턴 이후 ADMIN_EDIT가 없을 것(편집이 있으면 거부 — 편집을 조용히 지우지 않는다).
- `rerender_latest`는 턴 이전 상태로 복원한 뒤 `ResolvedActionIntent.from_semantic`으로 새 `WorldTurn`을 만들고 세계 계획을 다시 계산한다.
  - 경로·접근은 의미 기록 그대로이며 재질문·판단 재호출이 없다.
  - 턴 이전 세계가 DYNAMIC이 아니면 세계 연산 없이 커밋하지 않고 종결한다.
- 되감기는 H02부터 가역 집합이던 세계 필드를 그대로 복원한다.
  - `world_lifecycle`·`world_initialization`을 비가역 집합에 추가했다(`definition_revision`·`narrative_seed`와 함께).
  - 재무·운영 이력은 되감지 않는다.

## 7. H06-F — 관리자 세계 편집

- `core/admin_world_edit.apply_admin_edit` → typed 묶음 → `CanonicalMutationCoordinator(ADMIN_EDIT)`.
- 거부 조건: LEGACY·CLOSED 세션, 활성 턴, 커밋 복구 중, 이력 조작 중, stale 기준, typed가 아닌 연산.
- 완전 no-op이면 새 version을 만들지 않는다. 턴 수·Settlement·Ink는 바뀌지 않는다.
- 되감기는 시간 순서대로 동작한다. 대상 스냅샷 이후의 편집은 사라지고, 감사 저널은 남는다.

## 8. H06-G — 안전 종료

- `close_session`의 순서:
  1. 정본 직렬화 락(배타)을 잡는다.
  2. 안전 지점을 검사한다(`close_blocked_reason`). 진행 중 턴(재시도 대기 포함)·커밋 복구·재서술/되감기·미종결 INIT/ADMIN·처리 중이면 거부한다(강제 취소 없음).
  3. 기존 `close_window(…SETTLE_REFUND)`로 실제 보관비를 정산하고, 미사용 선불을 정확히 한 번 환급한다. 초과분은 운영자 부담이다.
  4. CLOSED를 영속한다.
- 환급이 durable하게 끝나지 않았으면 CLOSED로 표시하지 않는다. 재시도해도 중복 환급은 없다.
- 세계(시계·예약·사건·공간·인지·정보)는 그대로다.
- `!세션종료`는 안전 지점 → 종료 서비스 → 채널 잠금 순서로 바꿨다. `disp:close`도 같은 안전 지점을 거친다.

## 9. 변경 파일

- 운영 코드
  - 신규: `core/world_lifecycle.py`, `core/cache_source.py`, `core/admin_world_edit.py`
  - 수정
    - `core/player_world.py`(BLOCKED·단일 PC·OPEN·스냅샷 전용 읽기)
    - `core/world_refs.py`·`core/world_progression.py`(`$session_pc`·`initial_full_scan`)
    - `core/definition_compiler.py`(`world_start` 검증 — 저작 시에만 키)
    - `core/cache_lifecycle.py`·`core/cache.py`(고정 원천·세대 메타·스냅샷 원문 재시작)
    - `core/canonical_mutation.py`(`has_unfinished`)
    - `core/turn_world.py`(`rerender_world_intent`)
    - `core/action_intent.py`(`from_semantic`)
    - `core/turn_history.py`(비가역 집합)
    - `core/world_state.py`·`core/models.py`(생애주기 필드)
    - `core/session_flow.py`(캐시 연기)
    - `core/__init__.py`, `core/constants.py`(7.4.0)
    - `cogs/session.py`(초기화·재시도·OPEN·참가 거부)
    - `cogs/gm.py`(BLOCKED 거부·의미 기록 revision·재서술 세계 계획)
    - `cogs/character.py`(참가 거부), `cogs/system.py`(`!캐시 재발급`·`!세션종료`), `core/display.py`(`disp:close` 안전 지점)
- 테스트
  - 신규(52건)
    - `tests/policy/test_h06a_lifecycle.py`(15), `test_h06b_pc_awareness.py`(6), `test_h06c_cache_source.py`(5), `test_h06d_restart.py`(2), `test_h06e_rerender_rewind.py`(6), `test_h06f_admin_edit.py`(7), `test_h06g_close.py`(7)
    - `tests/characterize/test_h06_lifecycle_scope.py`(4)
    - 헬퍼 `tests/policy/world_bind.py`, `tests/characterize/h06_scope.py`
  - 의도적 갱신
    - `test_h01~h04_no_cutover.py`: H06 모듈만 새 허용 소비자로 추가. `definition_revision` writer = world_lifecycle 하나, canonical_mutation 호출자 = INIT·ADMIN뿐.
    - `test_cache_finance_policy.py::test_p8`: 운영자 종료가 `close_session`을 거쳐 같은 환급 처분을 쓰는 것을 단언.
    - `test_h05a/bc` 픽스처: H06 생애주기(스냅샷 바인딩 + OPEN)를 따르도록 변경.
    - `test_h05_r2_corrections.py`: 선택지 파싱 정규식 1곳(H05 R3 때와 같은 이유).
- 문서: `CLAUDE.md`, `DEVLOG.md`, 이 노트.

## 10. 영속 필드 · 버전

| 항목 | 값 |
|---|---|
| 새 영속 필드 | `world_lifecycle`(기본 None) · `world_initialization`(기본 {}) — 기본값이 곧 레거시 의미. 기존 v4 세션은 그대로 로드된다 |
| 런타임 전용 | `open_quoted_tokens`(연기된 캐시 견적 — 비영속) |
| 저널 추가분 | 캐시 CREATED 이벤트의 `generation` 키(가산) |
| 버전 | `__version__` 7.3.0 → **7.4.0**. `SCHEMA_VERSION` 4·turn-history 3·CommitPlan 2·compiler 3·정의 형식은 그대로 |

## 11. 검증

| 항목 | 결과 |
|---|---|
| H06 대상 | 52 passed. 수정 전(197a9ac)에는 H06 모듈이 없어 전부 수집 실패. 게이트별 부분 되돌림: C 4 실패/1 통과(보존), E 5 실패/1 통과(보존 — H02 되감기) |
| H01~H05 정책·특성화 | 전부 통과(H05 전체 68 + H01~H04 294 포함) |
| 전체 pytest | **1,046 passed**, 0 failed, xfail/XPASS 0(특성화 60 · 결함 27 · 정책 959) |
| compileall · 루틴 ①②④⑤ | OK · cogs 9 / 명령어 44 / views 5 · 문서 OK(SESSION_FIELDS 95→97, core 서브모듈 80→83) |
| scenario_check --all | 5 × compiler v3, ERROR 0(영도 WARNING 3 = 기존). **definition revision 5개 모두 H06 이전과 동일** |
| provider | 운영 코드 `generate_content` 19 → **19**(변화 없음) |
| 범위 | `prompts.py`·`scenarios/` diff 0. 영도 저작 이행 없음 |

## 12. 집중 스캔

- **CanonicalMutationCoordinator production 호출자:** `core/world_lifecycle.py`(INITIALIZATION), `core/admin_world_edit.py`(ADMIN_EDIT)뿐(특성화로 고정).
- **`definition_revision` writer · 스냅샷 쓰기:** `core/world_lifecycle.py`(`bind_definition`) 하나.
- **DYNAMIC 판정 호출처:** `player_world.session_world_context` — 호출자는 `cogs/gm.py`(턴 세계·재서술)·`core/turn_world.py`·`core/world_lifecycle.py`.
- **다인 세션:** LEGACY다. 초기화 뒤 참가는 거부하고, 우회되면 BLOCKED다.

## 13. 남은 것(H07 또는 사용자 결정)

1. **운영 활성은 저작 이후.** 현 5개 시나리오에는 `world_start`가 없어 운영 세션은 전부 LEGACY다(설계대로 dormant). 영도 world_start·사건·정보·초기 배치 저작은 H07이다.
2. 정의 migration(기존 세션을 새 revision으로)은 경계만 있고 서비스는 없다. 스냅샷 덮어쓰기 거부와 생성 중 revision 변경 세대 비활성은 들어가 있다.
3. 관리자 편집은 서비스만 있다. 운영자 명령·UI가 필요하면 별도 결정이 필요하다.
4. 진행 엔진의 "거부된 기반 단위 = 진단" 동작은 PLAYER_TURN(H05)에서도 같다. 초기화만 충돌로 승격했으며, PLAYER_TURN 쪽 검토는 H07 후보다.
5. `prompts.py` 시스템 지시문 정합은 H07이다.
