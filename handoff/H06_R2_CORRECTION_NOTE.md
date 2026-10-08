# H06 R2 보정 노트

> 시작 `1dacdda9be2b934c13a39bf768d6cc8d560c210b`(H06, 1,046 passed) · 브랜치 `claude/h06-lifecycle`. 버전·스키마·이력·CommitPlan·compiler 유지(7.4.0). H07 NOT STARTED.
> 기존 `H06_COMPLETION_BUNDLE.md`는 수정하지 않았다(이 노트가 보정분).

| 항목 | 결과 |
|---|---|
| R2-01 | `INIT_FAILED` fail-closed — 실패 표식(`world_initialization.status=FAILED`) strict 저장, 레거시 캐시·인트로·턴 차단, 재시도 버튼. 기존 durable 스냅샷이 있으면 현재 원본으로 재컴파일하지 않고 그 스냅샷을 쓴다. `play_intro`는 `_execute_proceed(...)["ok"]`가 참일 때만 OPEN·첫 라운드 |
| R2-02 | `open_durable`·`close_session`이 strict 저장(실패 시 상태 되돌림·성공 보고 없음). `!세션종료` = LEGACY·DYNAMIC 모두 CLOSED + 활성 등록 제거. 재시작은 CLOSED 미등록. 재시도는 기존 finalizer 멱등성으로 추가 환급·finalize 없음. 실패 반환 dict 순서 결함 수정 |
| R2-03 | StartFrameButton의 직접 `apply_facts` 제거 → LEGACY 경로에서만 적용. DYNAMIC 표시 `world_timeline`은 정본(`apply_compat_view`)에서 파생 |
| R2-04 | genesis world_version을 원인·기준에서 미리 계산해 `genesis_version_id`가 genesis strict 저장에 함께 실린다(복구 경로 포함) |
| R2-05 | 저작 NPC(`actor_definitions`, kind NPC)를 같은 INITIALIZATION 묶음에서 `ACTOR_REGISTER`(AUTHORED_NPC·legacy key·aliases). 위치 추론 없음 |
| R2-06 | TRANSIT = DYNAMIC. 중간 장소 도착 = 독립 진행(도착 사건 → AT_PLACE 중단). 구간 안 중단 → TRANSIT 유지, 다음 턴 매니페스트에 이동 중 문맥, 이동 선언은 남은 시간만 진행 후 일반 계획, 현재 구간 실제 접근이 막혔으면 거부(`TRANSIT_ROUTE_STALE`), 구간 끝을 넘는 대기는 거부(`IN_TRANSIT_TIME`). 구간 중간에서 출발지로 되돌아가는 이동은 모델링하지 않는다(다른 목적지는 구간 끝 도착 후 계획). 시나리오 저작·자동 장소화 없음 |
| R2-07 | 확인된 결함 — 진행 엔진의 `UNIT_REJECTED(base)`가 플레이어 턴에서 무시돼 필수 연산 탈락 상태로 묘사·커밋 가능(고장 주입 반례). `_Run.step`·`process_observation`에서 결정적 충돌로 처리(묘사 전 FAILED_SYSTEM, 청구 0). 현 데이터의 자연 발생 경로는 찾지 못함(플래너 사전 검사·컴파일러가 PC 대상 사건 효과를 막음) |

검증: 신규 R2 테스트 21건(수정 전 1dacdda: 19 실패 · 2 통과[보존]) + 특성화 1건, H06 전체 74 passed, H05 전체 68 passed, H01~H04 294 passed, 전체 **1,068 passed**, compileall OK, scenario_check ERROR 0, provider 19, `prompts.py`·`scenarios/` diff 0, 5개 시나리오 revision 불변.
