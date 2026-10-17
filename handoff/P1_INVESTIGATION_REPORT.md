# INDAIM P1 조사 보고서 — 세계 상태 갱신 시점 · 후속 묘사 · 비용 시스템

> 성격: 설계 전 조사. 코드·테스트·문서·시나리오·프롬프트는 바꾸지 않았다(이 보고서 파일만 추가).
> 범위: 로드맵 #2(세계 상태 갱신 시점) · #1(후속 묘사 제거) · #8(비용 계측·예측·추적 정합) — 통합 패키지 P1.

---

## Ⅰ. 조사 기준 및 범위

| 항목 | 값 |
|---|---|
| 조사 일자 | 2026-10-17 |
| 코드 기준 | `main` = `c6b374bda618e52e54c5a93d2c5124ee8fa5b770`(v7.17.3) |
| 작업 브랜치 | `docs/roadmap-post-v7.17.3`(HEAD `6d20a03` = main + 로드맵 문서 1커밋, 코드 차이 0) |
| 프로젝트 버전 | `core/constants.py` `__version__ = "7.17.3"` |
| 작업 트리 | 조사 시작 시 깨끗함 |

**검토 문서**: `CLAUDE.md`, `DEVLOG.md`, `handoff/ROADMAP_POST_v7.17.3.md`, `handoff/H05_COMPLETION_BUNDLE.md`(후속 묘사 도입 근거), H02~H07 번들(필요 절만).

**조사 방법**
- 정방향 추적: 자동 턴 진입점 `GMCog._process_actions`부터 커밋·송출·다음 라운드까지 읽었다.
- 역방향 탐색: `continuation`·`extra_minutes`·`tie_order`·`world_observation`·`turn_cost_log`·`estimate_*`·`generate_content`·`OP_*`의 전 저장소 참조를 훑었다.
- 공급자 호출 전수: `generate_content` 호출 지점 19곳, `caches.create/delete/get`, `count_tokens`를 조사하고, 각 지점의 CostEvent 기록·claim·보고 경로를 대조했다.
- 저작 데이터: 영도 사건 템플릿 64개의 조건·단계 종류를 기계적으로 세었다(인지 조건 사용 여부 판정용).
- 테스트: 기준선 확인용 표적 테스트만 실행했다. `test_h05bc_turn_wiring`·`test_h05b_observation`·`test_h05_r2_corrections`·`test_roll_clock_estimate`·`test_h07_prompt_authority` → **51 passed**. 전체 회귀는 돌리지 않았다(조사 단계).

**조사하지 못한 영역과 이유**
1. **설계 원문 DESIGN 11·12**: 코드 주석이 근거로 인용하지만(§30·§32·§36 등) 저장소에 없다. 근거는 H05 번들과 코드 주석으로만 재구성했다.
2. **운영 데이터**: 운영 서버의 `data/cost_ledger.jsonl`·`sessions/*/logs/`가 저장소에 없다. 그래서 후속 묘사 실제 발생 빈도, 사고 토큰 분포, 예측 오차 실측은 확인하지 못했다.
3. **외부 청구 데이터**: GCP 결제 계정 접근 권한이 없다. 공개 문서 수준만 확인했다(Ⅸ).

---

## Ⅱ. 현재 턴 처리 구조

### Ⅱ-1. 자동 턴 실행 순서(DYNAMIC 세션, PROCEED 기준)

| # | 단계 | 코드 | 계산 | 임시 반영 | 정본 확정 | 플레이어 노출 |
|---|---|---|---|---|---|---|
| 1 | 선언 수신·안전장치(복구·턴/비용 한도·준비 중 차단·BLOCKED) | `cogs/gm.py:1803` `_process_actions` | — | — | — | — |
| 2 | 잔액 검사·예상 비용 | `gm.py:3358`·`3376` `estimate_turn` | 예측 | `session.last_estimate`(비권위) | — | 마스터 보고. 디스플레이는 다음 갱신 때 |
| 3 | 개연성 시뮬레이션(+선언 게이트) | `gm.py:5805` | 캐시 호출 | 시뮬 결과(메모리) | — | — |
| 4 | 판단층위(+`action_intent` 매니페스트) | `gm.py:3794` | 의도 해석 | tx `judgment_result`·`world_turn.intent` | — | ASK/게이트 안내 |
| 5 | **코드 세계 계획(stage-1)** — 이동·대기·사건 진행·NPC 일과·TRANSIT·같은 분 선후 검출 | `gm.py:3466` → `player_world.plan_player_turn`(`core/player_world.py:500`) | **묘사 전 순수 투영** | `wt.plan.projected_world`·`ops`·`signals` | — | 경로 ASK만 |
| 6 | 지시층위(투영 장소·장면 인물·사건 가이드·PC 지식·발견 블록 + `narration_overlay`) | `gm.py:3936`·`3963-3967` | 캐시 호출 | 스테이징(지시효과) | — | — |
| 7 | READY 배리어 소유 시작·계획 부착 | `gm.py:1876` `_finish_proceed_and_continue` → `_attach_world_turn`(`2115`) | — | `prep.world_turn`·투영 `world_timeline` | — | — |
| 8 | 묘사층위(세계 블록 `narration_world_block`) | `cogs/game.py:647` | provider 1회(**비스트리밍** `generate_content`) | `prep.narration` | — | — |
| 9 | 확정 묘사 직후 동시 준비 발사 | `gm.py:2398` | 비정규 NPC·추출·재계획 작업 등록 | 준비 레지스트리 | — | — |
| 10 | 전달(의사 스트리밍·TTS·화자 카드) | `game.py` `_execute_proceed` | TTS provider N회 | `prep.delivery` | — | **묘사 본문이 이 시점에 노출됨(커밋 전)** |
| 11 | 추출(+`world_observation`·`item_ops`) | `gm.py:2427`·`5502` | provider 1회 | `prep.extraction_plan`·`world_observation_raw` | — | — |
| 12 | **관찰 처리 + 보충 진행(stage-2)** | `gm.py:2151` → `turn_world.process_observation`(`core/turn_world.py:908`, 진행 호출 `:1001`) | 노출→PC 인지, 추가 시간, 선후 재계획, 발견·READ | `wt.result`(최종 ops·투영) | — | — |
| 13 | **후속 묘사 생성(조건부)** | `gm.py:2207-2212` 등록, `:2283` `_generate_continuation` | provider 0~1회 | `wt.continuation_text`, 대화 이력 덧붙임 | — | — |
| 14 | 합류·세계 연산 스테이징·READY | `gm.py:2514` `_join_and_ready`, `:2344` `_stage_world_turn` | 비용 멤버십 동결 | `WorldMutationBatch` 동결·투영·quiescence 증명 | — | — |
| 15 | 커밋 | `core/commit_coordinator.py` `CommitCoordinator`(`gm.py:2686`) | Settlement·InkTransaction | — | **world_version 전진·정본 세계·추출 효과·청구**(strict 저장·저널) | — |
| 16 | 파생 송출 | `gm.py:2703`(후속 묘사를 `derived.game`에 적재) → `:2766` `_emit_commit_derived` | — | — | — | **후속 묘사·성장 알림** |
| 17 | 마스터 턴 비용 보고·디스플레이 갱신 | `gm.py:2826`·`2862` | — | — | — | 마스터 임베드·상태판(정본 기준) |
| 18 | 다음 라운드(다음 행동 질문) | `gm.py:1584` `_start_round` | — | — | — | 질문 |

### Ⅱ-2. 핵심 정리 — "세계 상태 갱신"은 네 겹이다

1. **코드 소유 진행 계산**(이동·명시 대기/수면·그 사이 사건·NPC 일과)은 이미 **묘사 전**(5단계)에 끝난다. 결과는 `narration_overlay`로 묘사에 들어간다.
2. **묘사 의존 보충 계산**(12단계)은 묘사 **후**에 한다. 노출 정보 인지, 비코드 소유 추가 시간(`extra_minutes`), 같은 분 선후, 화자 식별 인지, 아이템 READ, 발견물 수거가 여기에 속한다.
3. **정본 확정**은 턴 끝의 커밋 한 번뿐이다(15단계). 다음 행동 질문(18단계)보다 앞선다.
4. **플레이어 노출**은 본문이 커밋 **전**(10단계), 후속 묘사·HUD가 커밋 **후**(16~17단계)다.

**로드맵 #2 "턴 종료 시점 갱신"과 Q1=A의 관계**: 정본 확정은 이미 턴 종료 시점이다. 그런데도 "늦게 갱신된다"고 체감되는 원인은 아래 다섯 가지다(모두 코드로 확인).
- (a) 대화·조사처럼 이동·대기가 아닌 턴(`PLAN_NONE`)은 묘사 전에 세계 진행이 **전혀 없다**(`player_world.py:572-575`). 시간은 묘사 뒤 추출이 보고한 `extra_minutes`로만 흐르고, 그 결과는 후속 묘사로만 보인다.
- (b) 후속 묘사는 커밋 뒤에 나온다.
- (c) 접근 확인 지급·아이템 사용 사실(`OBSERVER_PAID_ITEM`·`OBSERVER_USED_ITEM`)은 **다음 커밋 턴의 계획**에서만 평가된다(`core/items.py:744` `current_facts`).
- (d) TRANSIT 중단은 다음 턴에 남은 시간을 이어 간다.
- (e) ASK·NARRATE 응답은 세계를 진행시키지 않는다.

따라서 Q1=A의 실제 의미는 **"2번(묘사 의존 보충 계산)을 묘사 전으로 옮기거나 다음 턴으로 넘기고, 정본 확정(3번)은 지금 경계를 유지하는 것"**이다. 선행 계산은 조기 커밋이 아니다. 지금 stage-1이 이미 그런 구조로 되어 있다.

---

## Ⅲ. 세계 갱신 시점의 개발 배경

| 결정 | 시점 | 이유(근거) | 현재도 유효한 안전장치 |
|---|---|---|---|
| 정본 변경은 READY 이후 `CommitCoordinator` 하나 | WP-C·D | 묘사·추출 실패 시 정본 누출 방지(AUD-024), 원자적 청구·복구 | **유효** — 저널 복구, 실패 턴 청구 0, strict 저장 |
| 코드 소유 시간·이동은 묘사 전 계획 | H05-A | 판단층위가 캐시를 안 읽으므로 코드가 경로·시간을 확정하고 묘사는 따른다 | 유효 — `narration_overlay`의 "다른 시간·장소·결과를 묘사하지 말 것" |
| 정보 노출·추가 시간은 **묘사 후 관찰**로 결정 | H05-B | "PC가 실제로 듣거나 본 것만" 인지시킨다(노출 ≠ 믿음, 추측 금지). 묘사 전에는 무엇이 실제로 묘사될지 모른다 | 유효 — 정보 공개의 정직성 |
| 같은 분 서사 선후는 묘사가 고른다 | H04 계약 → H05-B | H04 엔진은 선후를 스스로 고르지 않는다(`SameMinuteNarrativeTie`). 서사 선택권을 묘사에 둔다 | Q1=A와 **충돌**한다. 선후를 묘사 전에 정해야 한다(Ⅺ-D2) |
| 후속 묘사(조건부 1회) | H05-C(DESIGN 12 §36 "조건부 예외") | 보충 진행(2번)이 현재 장면에 관찰 가능한 새 신호를 만들었는데 본 묘사가 그것을 표현하지 못했다. 그 공백을 같은 턴 안에서 메운다 | 제거 대상. 아래 Ⅳ의 기능은 보존해야 한다 |
| 실패 처리 | H05-B | 묘사 전 충돌은 묘사 호출 없이 종료, 묘사 후 충돌은 재시도 없이 FAILED_SYSTEM(청구 0) | 유효 — 선행 계산 시 '묘사 후 충돌' 유형이 줄어든다 |
| 아이템 사용·지급 사실은 다음 계획이 본다 | v7.17 R2·R4(Q11) | 사용은 추출(묘사 후)에서만 확정된다. 지급은 같은 턴 계획 결과다. 보유와 구별되는 일회성 사실 | 사용은 본질적으로 묘사 후. 지급은 같은 턴으로 당길 여지가 있다 |

**후속 묘사가 도입된 근본 이유**: 세계 진행 일부가 **묘사가 무엇을 표현했는지**(노출·추가 시간·선후)에 의존하도록 설계했기 때문이다. 그 의존 진행이 장면에 새 결과를 내면 본 묘사는 이미 나간 뒤다. 그래서 두 번째 묘사가 필요했다.

**이 결정에 기대어 이후 개발된 기능**(Ⅵ 상세): v7.14 발견 수거(D키), v7.17 아이템 READ·사용 사실·지급 반응, SVG 화자 얼굴·이름 식별 인지 합류, H06 재서술(같은 의미 선택으로 재계획하지만 추출은 다시 함), H07 신호 `exposes`, 디스플레이 시각 표시, 비용 멤버십(`TURN_FOREGROUND_CONTINUATION`), provider 호출 지점 수 고정 테스트(19).

---

## Ⅳ. 후속 묘사의 기능 및 제거 영향

### Ⅳ-1. 발생 조건(코드)

`continuation_plan`(`turn_world.py:1041`)은 다음을 모두 만족할 때만 만든다.
- 보충 진행 결과가 `TW_READY`다.
- 보충 진행이 만든 신호 중 `IF_OBSERVABLE`·observable이다.
- 본 묘사에 준 신호도, 추출의 `represented_signals`도 아니다(`unrepresented_signals`, `:861`).

보충 진행 자체는 `acq`(인지 획득 연산)나 `extra`(추가 시간)가 있을 때만 돈다(`:1000`).

### Ⅳ-2. 실제로 신호를 만들 수 있는 원천

| 원천 | 묘사 전에 알 수 있나 | 영도 현재 저작에서 같은 턴 신호를 만드나 |
|---|---|---|
| `extra_minutes`(비코드 소유 경과) — 시간 경과로 국면 타이머·일과 경계·예약 도래 | **현재는 불가**(묘사가 정한다) | **예 — 사실상 유일한 실제 원천**. 테스트 대표 시나리오도 "한동안 이야기한다" + 추출 10분 → 경비 신호 → 후속 묘사(`tests/policy/test_h05bc_turn_wiring.py:480`) |
| 정보 노출 → PC 인지 → 인지 조건 사건 | 후보(X키)는 묘사 전에 안다(stage-1 생성 정보·신호 `exposes`). 실제 노출 여부만 묘사 후 | **아니오** — 영도 64개 사건 중 `COGNITION_*`·`$session_pc` 조건 0건(기계 집계) |
| 화자 얼굴·이름 식별 인지 | **안다** — 지시층위(묘사 전)가 정한다(`gm.py:4150-4155`) | 아니오(인지 조건 0) |
| 아이템 READ 인지 | 아니오(추출 `item_ops`) | 아니오(인지 조건 0) |
| 발견물 수거·소진 | 아니오(추출 D키) | 아니오(사건 조건 없음) |
| 같은 분 선후 → 재계획 | 묘사 전 계획이 TIE로 멈춘다. 선택은 묘사 | 선후 확정 뒤 진행이 이어질 수 있다 |

### Ⅳ-3. 후속 묘사가 담당하는 기능

| 기능 | 담당 여부 | 제거 시 영향 |
|---|---|---|
| 같은 턴에 새로 생긴 사건 전달 | 예(시간 경과로 생긴 장면 신호) | 정본에는 반영되는데 **플레이어가 보지 못한다**. 다음 턴 묘사도 이 신호를 받지 못한다(아래 Ⅳ-4) |
| NPC 도착·행동 반영 | 예(일과 경계·사건 이동 신호) | 동일 |
| 플레이어 행동 결과 보충 | 아니오(코드 소유 결과는 본 묘사가 표현) | 없음 |
| 시간 경과에 따른 장면 변화 | 부분(신호가 있을 때만) | 상태판·다음 턴 시각은 바뀌는데 묘사는 침묵 |
| 중요 정보 공개 | 간접(신호 cue). 정보 인지 자체는 관찰 처리가 한다 | 인지 기록은 유지된다. 단, 신호가 노출할 정보(`exposes`)는 다음 매니페스트에 오르지 않을 수 있다 |
| 관찰·선후 정합 | 아니오(선후는 본 묘사·추출) | 없음 |
| 대화 이력 연속성 | 예 — 후속 묘사 본문을 같은 시도의 대화 이력 마지막 model 항목에 덧붙인다(`gm.py:2327-2338`) | 다음 턴 묘사가 그 반응을 맥락으로 갖지 못한다 |

### Ⅳ-4. 단순 삭제의 공백(핵심)

- 보충 진행의 신호는 `TurnWorldResult.signals`에만 있다. **정본에 "미표현 신호"로 남는 필드가 없다.** 후속 묘사를 지우면 그 신호는 어디에도 전달되지 않고 사라진다. 사건 상태 변화는 정본에 남지만 그 '관찰 계기'가 사라진다.
- 다음 턴 stage-1은 새 진행만 신호로 낸다. 이미 일어난 지난 턴 신호는 다시 내지 않는다.
- **결론**: 후속 묘사를 지우려면 (i) 보충 진행을 묘사 전으로 옮겨 신호가 본 묘사에 들어가게 하거나, (ii) 미표현 신호를 다음 턴 묘사로 넘기는 저장 경로를 만들어야 한다. 둘 중 하나는 반드시 필요하다.

### Ⅳ-5. 후속 묘사에 직접 묶인 코드·테스트

- 코드: `TASK_FOREGROUND_CONTINUATION`(`turn_preparation.py:778`), `OP_TURN_FOREGROUND_CONTINUATION`(`cost_ledger.py:60`), `WorldTurn.continuation_text/delivered`(`turn_world.py:51-52`), `ForegroundContinuationPlan`, `_generate_continuation`, `_apply_commit_effects` 송출부, 턴 비용 로그 라벨 "후속 묘사".
- 테스트: `test_h05bc_turn_wiring.py:480-560`(5건 — 발생·순서·비용 멤버·실패 시 커밋 차단·커밋 실패 시 미송출), `test_h05b_observation.py:71`, `test_h05_r2_corrections.py:195`. 호출 지점 수 고정 `test_h07_prompt_authority.py:33`(19)와 `test_v717_initial_knowledge.py:484`도 갱신이 필요하다.
- **용어 주의**: 코드의 "continuation"은 세 가지다 — ① H05 후속 묘사(제거 대상), ② WP-C "legacy continuation"(READY 이후 적용을 가리키던 옛 이름, `test_billing_policy`·`test_turn_commit_races`가 부재를 단언), ③ ROLL 버튼 뒤 이어 가기(`test_auto_turn_orchestration` c003). ②·③은 P1과 무관하다.

---

## Ⅴ. 묘사 전 세계 진행 선계산 가능성(Q1=A)

| # | 질문 | 답 | 근거 |
|---|---|---|---|
| 1 | 최초 묘사 전에 계산할 수 있나 | **코드 소유분은 이미 그렇다**(stage-1). 남은 것은 묘사 의존분 | Ⅱ-2 |
| 2 | 묘사·추출 결과가 있어야만 계산되는 진행이 있나 | 있다: 추가 시간, 실제 노출 여부, 같은 분 선후, 아이템 사용·READ, 발견 수거 | `process_observation` 입력 |
| 3 | 본질적 의존인가, 구현 순서 탓인가 | **추가 시간·선후**: 구현 순서 탓. 묘사 전에 결정 주체를 정하면 된다(Ⅺ-D1·D2). **노출 여부**: 반쯤 본질적. 후보는 묘사 전에 알지만 "실제로 묘사됐는가"를 인지 근거로 삼는 정책 때문이다. **아이템 사용·READ·수거**: 본질적. 플레이어 행동의 세부를 묘사가 확정한다 | — |
| 4 | 선행 결과를 묘사에 안전하게 전달할 수 있나 | 예. 이미 `narration_overlay`·`narration_world_block`이 "PC가 관찰 가능한 것만" 전달하는 경로다(`observable_signals`) | `turn_world.py:209`·`264` |
| 5 | 미인지 정보를 차단할 수 있나 | 예. 신호 cue·정보 `summary`(공개 안전 요약)만 쓰고, 명제 전문·숨은 정보는 싣지 않는 규칙이 이미 있다(R2) | `build_observation_manifest` 독스트링, 테스트 `test_h05_r2_corrections` |
| 6 | 묘사 실패·재시도 시 폐기·재사용 | 가능. stage-1은 순수 투영이고 `source_world_version`·`definition_revision`에 결속돼 있다(`_ensure_world_plan`이 버전이 바뀌면 재계획). 정본 무변경이라 폐기 비용이 없다 | `gm.py:2077` |
| 7 | 단일 묘사로 바꾸면 중요한 진행이 누락되나 | 묘사 후에만 생기는 사실(아이템 사용·READ·수거·실제 노출)이 같은 턴에 사건을 일으키는 경우만 누락된다. **영도 현재 저작에서는 해당 0건**이지만 컴파일러는 `$session_pc` 인지 조건을 허용한다. 미래 저작에는 생길 수 있다 | Ⅳ-2 |
| 8 | 스트리밍·병렬 구조 보존 | 보존된다. 묘사 provider 호출은 비스트리밍이고 "스트리밍"은 디스코드 의사 스트리밍이다(`game.py:663`). 선행 계산은 순수 코드라 provider 호출이 늘지 않는다. 단, **추가 시간을 판단층위가 내면 판단 스키마 확장**(호출 수 불변)이 필요하다 | — |

**선행 계산 시 함께 해결되는 현존 불일치**
- 묘사 시각과 HUD 시각 불일치: 묘사층위 시각 블록은 `plan.end_minute`(stage-1)을 쓴다(`turn_world.py:88-102`). 정본·상태판은 `extra_minutes`까지 더한 값이다. 지금은 묘사가 "14:00", 상태판이 "14:30"이 될 수 있다.
- 재서술 비결정성: 재서술은 추출을 다시 하므로 `extra_minutes`가 달라져 세계 결과가 바뀔 수 있다(`rerender_latest`, `gm.py:3027`). 경과 시간을 묘사 전 의미 기록에 넣으면 같은 결과가 재현된다.
- 보충 진행의 관찰자 맥락 누락: Ⅹ-1.

**선행 계산 시 새로 생기는 위험**
- 묘사가 선행 결과를 무시하거나 다르게 그리면 정본과 묘사가 어긋난다. 기존 overlay 규칙과 같은 위험이며 새 위험은 아니다.
- 추가 시간을 묘사 전에 정하면 "묘사상 분명히 흐른 시간" 보정 능력을 잃는다(240분 상한 보정 포함).
- 선후를 묘사 전에 정하면 H04/H05 계약("엔진은 고르지 않는다")을 바꾼다.

---

## Ⅵ. 관련 시스템 의존성 전수조사

위험도: 상(설계 필수 결정) / 중(코드 변경 필요) / 하(확인만)

| 시스템 | 관련 코드 | 현행 의존 관계 | 변경 영향 | 위험도 |
|---|---|---|---|---|
| CommitCoordinator·READY | `commit_coordinator.py`, `gm.py:2514` | 최종 ops는 stage-1+stage-2 합을 READY에서 동결 | 동결 대상 구성만 바뀐다. 경계는 유지 | 하 |
| SameMinuteNarrativeTie | `player_world` PLAN_TIE, `turn_world.py:552-590`, `process_observation` ① | 묘사가 고르고 추출이 키로 보고 → 재계획. 보충 단계 TIE는 READY 불가 | 결정 주체 이동(Ⅺ-D2). TIE 실패 경로 재설계 | **상** |
| ForegroundContinuationPlan | Ⅳ-5 | — | 제거 | 중 |
| world_observation·관찰 매니페스트 | `turn_world.py:747-858`, `gm.py:5586-5593` | 추출 스키마에 X/S/O/D키·추가 시간 | 필드 축소·의미 변경(S·O·extra는 묘사 전 확정 시 불필요해짐) | 중 |
| process_observation | `turn_world.py:908` | 노출·추가 시간·선후·발견·READ·식별을 한 번에 보충 진행 | 시간·선후는 stage-1로 이동. 인지·발견·READ는 묘사 후 유지(정본 확정 전) | **상** |
| 시간·이동·TRANSIT | `player_world.py:596-801` | 코드 소유 경과는 묘사 전, 비코드 경과는 묘사 후 | `PLAN_NONE` 턴에 경과 계산 추가 | 중 |
| NPC 일과·위치 | `npc_position.py` | 시계의 순수 함수 — 시계가 언제 전진하느냐에 따름 | 시계 전진이 묘사 전으로 오면 일과 이동도 묘사 전 신호가 됨(이점) | 하 |
| 사건 상태기계·wakeup | `world_progression.py` | 진행 호출마다 `progress` 맥락 필요 | 보충 진행에 관찰자 맥락이 빠져 있음(Ⅹ-1) | 중 |
| 플레이어 인지·정보 공개 | `cognition.py`, X키 | 노출 = 묘사 후 확인 | 정책 유지 권고(Ⅺ-D3) | 중 |
| **아이템 사용·지급 반응**(신규 발견) | `items.py:744` `current_facts`, `player_world.py:521-526` | 직전 커밋 턴 사실만 다음 계획에 반영 | 지급은 같은 턴 계획에서 알 수 있어 당길 여지가 있다. 사용은 묘사 후라 다음 턴 유지. 계획 없는 커밋 턴이 끼면 사실이 소멸 | 중 |
| **화자 식별 인지**(신규 발견) | `gm.py:4150-4155`, `process_observation` ②-a | 지시층위가 묘사 전에 정한다. 반영은 보충 진행에서 | 묘사 전 진행에 합류 가능(이점) | 하 |
| **발견물 수거·A1 보정**(신규 발견) | `gm.py:2151-2167`, `turn_world.py:664-745` | 추출 D키 → 수거·소진·소지 +1 | 묘사 후 유지(본질적). 시간 진행과 분리 필요 | 중 |
| 디스플레이·HUD | `gm.py:2875` `refresh_display(turn_end)` | 커밋 후 정본 | 시각 불일치 해소(이점) | 하 |
| 되감기·`turn.delta` | `commit_coordinator.py:699-729`, `turn_history` | 커밋 전후 캡처 | 커밋 경계 불변 → 영향 없음 | 하 |
| 재서술 | `gm.py:3027`, `turn_world.py:1088` `rerender_world_intent` | 의미 선택 보존 + 재계획. 추출은 다시 함 | 묘사 전 결정(경과·선후)을 의미 기록에 넣어야 재현성 유지 | 중 |
| 캐시 재발급·world_version | `cache_source.py` | 기준 world_version 캡처 | 영향 없음 | 하 |
| 재시작·복구 | `CommitJournal`, plan v2 | PREPARED에 target version·digest 동결 | 커밋 형식 불변이면 영향 없음 | 하 |
| LEGACY/DYNAMIC 분기 | `_world_turn`(`gm.py:1989`) | LEGACY는 `world_turn` None → 후속 묘사 없음 | LEGACY 무영향 | 하 |
| TTS | `game.py:838-951` | 본 묘사만 더빙. 후속 묘사는 더빙 안 함 | 단일 묘사가 되면 반응까지 더빙됨(UX 이점, TTS 비용 소폭 증가) | 하 |
| 메시지 수명주기 | `_emit_commit_derived` | 후속 묘사는 파생 게임 출력(송출 의도·되감기 정리 대상) | 제거 시 파생 출력 1종 감소 | 하 |
| **화자 카드 바리에이션**(신규 발견) | `speaker_card.py:616-623` | 전달 중 카드가 **추출 결과를 기다린다**(바리에이션 > 1) | 추출 스키마를 바꿀 때 전달 지연 특성 유지 확인 | 하 |
| 개연성 시뮬레이션 | `gm.py:3398-3407` | 세계 계획 **전**에 레거시 미러로 실행(기존 결함 ⑥) | 선행 계산과 순서가 겹친다. 시뮬은 투영을 모른다 | 중 |
| 비용 멤버십 | `turn_preparation.py:1133-1172` | 후속 묘사 op가 멤버 | 제거 | 하 |
| provider 호출 수 고정 테스트 | `test_h07_prompt_authority.py:33` | 19 고정 | 18로 의도적 갱신 | 하 |
| 예상 비용 | `estimate.py:166` | 후속 묘사를 원래 포함하지 않음 | 영향 없음(Ⅶ 참조) | 하 |

---

## Ⅶ. 비용 계측·예측·정산 현황

### Ⅶ-1. 공급자 비용 발생 경로 전수

`generate_content` 19곳 + 캐시 생성/보관 + `count_tokens`(Gemini 무료):

| 경로 | OP | 모델 | 청구 힌트 | 턴 claim | CostEvent | 사고 토큰 | 마스터 턴 보고 | 비고 |
|---|---|---|---|---|---|---|---|---|
| 시뮬레이션 `gm.py:5903` | TURN_SIMULATION | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| 판단 `:3844` | TURN_JUDGMENT | LOGIC | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| 지시 `:4073` | TURN_INSTRUCTION | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| 묘사 `game.py:663` | TURN_NARRATION | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | `call_with_retry` 미사용·HTTP 타임아웃 없음(Ⅹ-5) |
| 경량 NARRATE `gm.py:4574/4587` | TURN_LIGHT_NARRATE | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| 후속 묘사 `:2306` | TURN_FOREGROUND_CONTINUATION | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | 제거 대상 |
| 추출 `:5644` | TURN_EXTRACTION | DEFAULT | PLAYER_CANDIDATE | 예 | 예 | 예(예산 제한) | 예 | 재시도는 `retry_claims` 내구 claim |
| 비정규 NPC `:5139` | TURN_IRREGULAR_NPC | — | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| NPC 프로필 `:4977` | TURN_NPC_PROFILE | — | PLAYER_CANDIDATE | 예 | 예 | 예 | 예 | |
| 서사 재계획 `:6508` | TURN_NARRATIVE_PLANNING | — | — | 예 | 예 | 예 | 예 | free 모드 |
| 지시 검증 `:5745` | TURN_INSTRUCTION_VALIDATION | — | PLAYER_CANDIDATE | 예 | 예 | — | **아니오** | **호출자 0 — 사문 코드**(Ⅹ-6) |
| TTS 런타임 `tts.py:124` | TTS_RUNTIME | TTS | PLAYER_CANDIDATE | 예(`_claim_tts`) | 예(오디오 토큰) | 해당 없음 | 예(단가 오류, Ⅹ-3) | 오디오 추출 실패 시 미기록·늦은 응답 미관측(Ⅹ-4) |
| TTS 테스트·프리셋 | TTS_TEST·TTS_PRESET_BUILD | TTS | SYSTEM·OPERATOR | — | 예 | — | — | |
| 자동/수동 압축 `game.py:1051/1420` | MEMORY_* | DEFAULT | 운영자 부담 | — | 예 | 예 | 별도 임베드 | |
| 유지 시간 해석 `gm.py:4858` | CACHE_TIME_INTERPRET | — | 해석 청구 저널 | — | 예 | — | — | |
| 프로필 AI·초기 지식 `profile_ai.py:103` | PROFILE_AI·INITIAL_KNOWLEDGE | PROFILE_AI | FREE/OPERATOR | — | 예 | 예 | — | |
| 캐릭터 상세 `utils.py:261` | CHARACTER_DETAIL_GENERATION | — | — | — | 예 | — | — | |
| 이미지 생성 `media.py:107` | IMAGE_GENERATION | IMAGE | UNKNOWN | — | 예 | — | 별도 임베드 | |
| 캐시 생성 `cache_lifecycle.py:224` | CACHE_CREATE/RECOVERY | DEFAULT | 목적별(오픈=PLAYER, 자동 재발급=SYSTEM, 수동=OPERATOR) | — | 예(**토큰×단가 계산값**, `usage_source=PROVIDER_METADATA`로 표기) | — | 별도 | 턴 중 자동 재발급(`game.py:671`)은 SYSTEM |
| 캐시 보관 | CACHE_STORAGE | DEFAULT | 목적별 | — | 예(FIXED_PRICING, 삭제 실패면 ESTIMATE) | — | — | |
| `count_tokens` `cache.py:407/426` | — | — | — | — | 아니오 | — | — | 공급자 무료 |

**계측되지 않는 비용**
- TTS: 응답은 성공했는데 오디오 추출·리샘플이 실패한 경우.
- TTS: 래퍼 타임아웃 뒤 늦게 도착한 응답(늦은 관측 추적이 없다).
- 구조 로그 `calls.jsonl`은 TTS 호출을 세션 ID 없이 전역 로그에만 남긴다(`tts.py`의 `call_with_retry`에 `session_id` 미전달).

### Ⅶ-2. 권위와 흐름

```
usage_metadata ─ extract_token_usage(out=가시+사고) ─ calculate_text_gen_cost_breakdown
   ├─ ProviderOperation.record → CostEvent(append-only, data/cost_ledger.jsonl) ─ 권위
   │     └─ 턴 claim → close_cost_membership(frozen ids) → build_turn_settlement
   │            → charge_ink_per_user = ceil(player_billable_krw / 7) ← 유일한 반올림 경계
   │            → InkTransaction(정확히 1회) → total_ink_spent·last_turn_ink 미러
   ├─ accrue → total_cost/total_usd(호환 미러, 권위 아님)
   ├─ turn_cost_log(표시 버퍼) → build_turn_cost_embed(마스터)
   ├─ write_cost_log → logs domain "cost.usage"
   └─ update_stats/record_actual_input → session.cost_stats(예측 학습)
CostEvent 기록 시 → logs domain "cost.event"
provider 응답 전부 → logs calls.jsonl usage(가시 out·thought 분리)
```

| 질문 | 답 |
|---|---|
| 최종 권위 | 공급자 비용 = CostEvent. 플레이어 부담 = Settlement(턴)·캐시 저널·해석 저널 |
| 재계산 지점 | 마스터 임베드의 항목별·합계 계산(`format_breakdown`, `build_turn_cost_embed` 토큰 합계)이 `PRICING_1M[DEFAULT_MODEL]`로 다시 계산한다. CostEvent와 **같은 단가 함수지만 모델을 잃으면 어긋난다**(Ⅹ-3) |
| 반올림 | 잉크는 Settlement 경계에서 1회(턴), 캐시 창은 합계에 1회. 원화 표시는 `format_cost`의 소수 2자리 |
| 중복·누락 | 멱등키 `{operation_id}:attempt:{n}`. 누락은 Ⅶ-1의 TTS 2건. 늦은 응답(텍스트)은 기록·멤버 포함 |
| 실비용과 청구 혼동 | 정책상 **1인당 전액 청구**(`charge_ink_per_user × 인원`, 청구 대상 = 세션 전 플레이어, `commit_coordinator.py:274`). 다인 세션은 명목 청구 합계가 공급자 비용의 N배다. 재무 정책이므로 바꾸지 않는다. 운영 보고에서는 둘을 구분해야 한다 |
| 실패 호출 | 응답(usage)이 없는 시도는 기록하지 않는다(날조 금지). 응답이 있으면 게임에 버려도 기록한다(늦은 관측). 실패 턴은 FAILED_SYSTEM으로 청구 0, 비용은 `system_unbilled` |

### Ⅶ-3. 비용 예측

경로: `estimate_turn`(`estimate.py:166`) · `estimate_tts`(`:379`) · `estimate_session_open`(`:284`) · 세션 생성 화면 가격 안내(`session_flow.py:509-528`).

| 항목 | 현황 |
|---|---|
| 입력 | 실제 `PromptBuilder.build_prompt` 조립 × 0.65 토큰/자 × 세션 보정계수. 보정은 지시·묘사 실측만 학습. **DYNAMIC 블록**(장소·장면 인물·사건 가이드·PC 지식·발견·묘사 세계 블록·행동 매니페스트·관찰/아이템 매니페스트)은 조립에 없다 |
| 출력·사고 | 층위별 **세션 내** 누적 평균·최소·최대(`session.cost_stats`). 상한 = 관측 최대 × 1.15. 표본이 없으면 `DEFAULT_BASELINE`. 영도 포함 **모든 시나리오에 `cost_baseline` 미저작** |
| 캐시 | 관측 최대 캐시 읽기량(`cache_read_tokens`) |
| 포함 호출 | 시뮬·판단·지시·묘사·추출. **미포함**: 비정규 NPC, NPC 프로필, 재계획, 후속 묘사, 추출 재시도, 턴 중 캐시 재발급(SYSTEM) |
| 사고 예산 | 추출만 `thinking_budget` 지정. 나머지 층위는 모델 기본(동적) → 분산이 크다 |
| TTS | 묘사 출력 토큰(**사고 포함**) ÷ 0.65 × 0.02원/자 상수. 보정 코드 없음 |
| 시나리오별 통계 | **없음**. 통계가 세션 객체에 갇혀 있고 세션 간 공유가 없다. CostEvent에 시나리오·모드·행동 유형 필드가 없다. 단 `session_id`로 `sessions/{id}/data.json` 또는 제거 후 `data/session_finance/{id}` 요약의 `scenario_id`를 잇는 사후 결합은 가능하다 |
| 모델·가격 변경 | CostEvent `metadata.pricing_basis`에 가격표 버전·환율 보존. 예측은 현재 상수만 쓴다 |

**현재 데이터만으로 통계 예측이 가능한가**: 원장 사후 결합으로 시나리오 × OP × 모델 단위 분포는 만들 수 있다. 하지만 다음은 구분할 수 없다.
- (a) LEGACY/DYNAMIC
- (b) 행동 유형(ASK/NARRATE/PROCEED/ROLL)
- (c) 입력 구성(캐시분·고정분·가변분)
- (d) 턴 번호에 따른 로그량 성장

이를 위해 필요한 필드(권고, 확정 아님): `scenario_id`, `world_mode`, `action`, 층위별 비캐시 입력의 구성 근사(프롬프트 블록별 문자 수), `thinking_budget`. 기존 `metadata` dict에 넣으면 CostEvent 스키마를 바꾸지 않는다.

---

## Ⅷ. 비용 보고·로그 정합성

| 비교 | 결과 | 재현 조건·원인 |
|---|---|---|
| 턴 보고 "호출 내역 합계" vs Settlement 공급자 합 | 보통 일치 | 불일치: (1) 늦은 응답 이벤트는 `turn_cost_log`에 없다. (2) TTS 오디오 추출 실패분은 CostEvent에도 없다 |
| TTS 항목 계산식 | **불일치** | TTS 켜짐 + 음성 연결. 로그 항목에 `model`이 없어(`game.py:950`) `format_breakdown`이 텍스트 단가($3/1M)로 펼친다. 실제 합은 TTS 단가($10/1M)라 **항목 줄의 합 ≠ "합"**. 하단 "토큰 합계"도 오디오 토큰을 텍스트 단가로 합산한다 |
| 구조 로그 토큰 의미 | **세 가지가 다르다** | `calls.jsonl usage.out` = 가시 출력만(사고 별도). `cost.event out` = 가시+사고. `cost.usage out` = 가시+사고. `cost.event`는 오디오·이미지 토큰과 metadata를 빼고 기록한다 → TTS가 0토큰·비용 있음으로 보인다 |
| 마스터 예상 vs 실제 | 비교 수단 없음 | 예상(`last_estimate`)은 턴 실제와 짝지어 기록되지 않는다(로그 없음) |
| 디스플레이 "캐시 N 토큰" vs 오픈 예정 비용 | **다른 양** | 표시는 `cache_read_tokens`(관측 최대 캐시 읽기, 시스템 지시문 포함)를 우선 쓰고, 비용 계산은 `cache_tokens`를 쓴다(`display_board.py:74`, `display.py:110`) |
| 세션 생성 가격 안내 vs 오픈 예상 | **산식 불일치** | `session_flow.py:514-518`은 worldview × 0.65로 캐시 토큰을 잡는다. `approx_cache_tokens`는 여러 필드 × 0.33(실측 근거 주석 포함)이다. 같은 양을 두 산식이 따로 계산한다 |
| `!사용량` | 원장 기반(권위) | 정상. 다만 OP·모델·시나리오별 분해가 없다(힌트별만) |
| 자동 비용 상한 | 원장 세션 합계 | 매 턴 원장 전체를 strict로 읽는다 — 원장이 커지면 느려질 수 있음(측정 전, 최적화 대상 아님) |

---

## Ⅸ. 외부 청구 데이터 대조 가능성

**현행 연결 방식**: `genai.Client(api_key=GEMINI_API_KEY)`(`main.py:56`) — Gemini Developer API(AI Studio 키). Vertex AI가 아니다.

| 조건 | 확인 결과 |
|---|---|
| 조회 수단 | Google Cloud Billing → BigQuery 내보내기(표준·상세) |
| 집계 단위 | 프로젝트·서비스·SKU·시간 구간. **API 키별·요청별 분해 불가** |
| 요청 라벨 | Vertex AI `generateContent`만 사용자 라벨을 지원해 청구 보고서에서 라벨별 분해가 된다. **Developer API(현행)는 라벨 필드를 지원하지 않는다**는 커뮤니티 보고가 있다. 라벨 키당 고유 값 1,000개 제한이 있어 세션 ID 라벨은 부적합, 시나리오 라벨은 적합 |
| 지연 | 청구 내보내기는 수 시간 단위로 늦게 반영된다(정확한 지연은 Google 문서로 재확인 필요) |
| 차이 요인 | 캐시 토큰 분류, 사고 토큰, 무료 등급·크레딧, 가격 변경, 환율(내부는 고정 1500), 캐시 보관 시간 산정 방식, 우리가 계산값으로 기록하는 캐시 생성비 |
| 필요 조치 | 결제 계정의 BigQuery 내보내기 활성화 + 읽기 권한 서비스 계정 키(CLAUDE.md 미완 항목 'Cloud Billing 연동'과 같음) |

**분류**: **3. 일부 집계 수준에서만 가능**(일·SKU 단위로 원장 합계와 대조) + **2. 추가 설정·권한이 있으면 가능**. 요청 단위 대조는 **4. 현재 환경에서 불가**(Vertex AI 이전이 전제). 이번 조사에서는 외부 접근을 시도하지 않았고 인증 정보를 요청하지 않았다.

출처(공개 자료, Google 공식 문서로 재확인 권장):
- [Vertex AI — Custom metadata labels](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/add-labels-to-api-calls)
- [dev.to — GCP Billing & Vertex AI: Solving Gemini Cost Allocation](https://dev.to/gde/gcp-billing-vertex-ai-solving-gemini-cost-allocation-in-a-single-project-vertex-ai-dynamic-5aok)
- [Google Developers 토론 — Billing export to BigQuery quick guide](https://discuss.google.dev/t/gcp-billing-export-to-bigquery-quick-guide-to-tracking-ai-costs/318584)

---

## Ⅹ. 발견된 추가 문제(기존 목록 밖, 수정하지 않음)

| # | 분류 | 내용 | 근거 | 영향 |
|---|---|---|---|---|
| 1 | 간접 상태(A) | **보충 진행이 관찰자 맥락 없이 돈다.** stage-1은 `observer_items/stats/paid/used/activity`를 넘기지만(`player_world.py:521-528`), `process_observation`의 진행 호출은 `observer_actor`·`scene_place`만 넘긴다(`turn_world.py:1001-1003`). 그래서 추가 시간 구간에서 `OBSERVER_HAS_ITEM`(영도 8)·`OBSERVER_STAT`(6)·`OBSERVER_PAID_ITEM`(5)·`OBSERVER_ACTIVITY`(127)이 UNKNOWN으로 평가된다(UNKNOWN ≠ 충족, STICKY 유지 조건도 불충족) | 코드 확인 | **잠재적** — 같은 턴 안에서 stage-1과 다른 판정. 실제 발현 여부는 사건별 확인 필요. P1에서 시간 진행을 stage-1로 옮기면 자연히 해소 |
| 2 | 사용자 경험(D) | 묘사 시각 ≠ 상태판 시각(`extra_minutes` 반영 차이) | Ⅴ | 직접 — P1이 해소 |
| 3 | 재무 보고(E) | TTS 항목 단가 오표기·토큰 합계 혼합 | Ⅷ | 직접(운영 보고). 청구에는 영향 없음 |
| 4 | 외부 서비스(F) | TTS 비용 누락 2경로(오디오 추출 실패, 타임아웃 뒤 늦은 응답) | `tts.py:135-160`, `_count_attempt`에 `track_inflight` 없음 | 직접(원장 과소) |
| 5 | 외부 서비스(F) | 본 묘사 호출이 `call_with_retry`·HTTP 타임아웃 없이 직접 호출된다 → 공급자 지연 시 무기한 대기 가능. 캐시 오류 외에는 재시도가 없다 | `game.py:650-667` | 잠재적(실패 경로) — P1 범위 밖 |
| 6 | 테스트되지 않은 경로(G) | `_verify_proceed_instruction`(OP_TURN_INSTRUCTION_VALIDATION) 호출자 0 | grep | 영향 없음(사문) |
| 7 | 예측(E) | 예측 누락 호출·DYNAMIC 블록 미포함·TTS 사고 토큰 포함·상수 미보정 | Ⅶ-3 | 직접(예측 정확도) |
| 8 | 예측(E) | 캐시 토큰 산식 이중화(0.65 vs 0.33) | Ⅷ | 직접(세션 생성 안내 — 플레이어 노출) |
| 9 | 로그(G) | 토큰 의미 3종 불일치, `cost.event`의 오디오·이미지 토큰 누락 | Ⅷ | 직접(운영 분석) |
| 10 | 시간(B) | 지급·사용 사실이 다음 커밋 계획에서만 평가되고, 그 사이 계획 없는 커밋이 끼면 소멸 | `items.py:744-755` | 간접 — Q1=A 설계 시 같은 턴 처리 여부 결정 필요 |
| 11 | 재서술(C) | 재서술이 추출을 다시 해 경과 시간·세계 결과가 바뀔 수 있다 | `gm.py:3027` | 간접 — P1이 개선 가능 |
| 12 | 순서(B) | 개연성 시뮬레이션이 세계 계획 전에 레거시 미러로 실행(기존 결함 ⑥) | `gm.py:3398` | 간접 |
| 13 | 문서 | 설계 원문 DESIGN 11·12가 저장소에 없다 | — | 설계 검증 근거 부재 |

---

## Ⅺ. 사용자 결정이 필요한 사항

### D1. 이동·대기가 아닌 행동의 경과 시간을 묘사 전에 누가 정하나

- **필요 이유**: Q1=A를 그대로 적용하면 대화·조사 턴(`PLAN_NONE`)에도 묘사 전 경과가 있어야 세계가 진행된다. 지금은 묘사 뒤 추출이 정한다(`extra_minutes`).
- **근거**: `player_world.py:572-575`, `turn_world.py:980-988`, 테스트 `test_h05bc_turn_wiring.py:480`.

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. 판단층위 스키마에 소요 시간 필드 추가(호출 수 불변) | 단일 묘사·재서술 재현성·HUD 일치 | 판단층위는 세계 설정을 모른다(시간 추정은 세계 사실이 아니라 허용 범위로 봄). 프롬프트 수정 승인 필요 |
| B. 코드 기본값(행동 분류별 고정 분) | 결정적·호출·프롬프트 무변경 | 서사와 어긋날 수 있다 |
| C. 묘사 전에는 0분, 묘사 후 추가 시간은 정본에만 반영하고 결과 신호는 다음 턴 묘사로 넘김 | 현 구조 최소 변경 | 반응이 한 턴 늦다(로드맵 #2 취지와 어긋남) |

- **권고**: **A**(필드가 없거나 무효면 B로 대체). 묘사 후 `extra_minutes`는 폐기한다.

### D2. 같은 분 서사 선후를 묘사 전에 누가 정하나

- **필요 이유**: 지금은 묘사가 고른다. 단일 묘사에서는 묘사 전에 정해야 결과를 오버레이로 줄 수 있다.
- **근거**: `turn_world.py:552-590`, `process_observation` ①, H04 계약.

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. 지시층위가 O키로 선택(이미 묘사 전·캐시 읽기) | 서사적 판단 유지, 추가 호출 0 | 지시 스키마 확장(프롬프트 승인) |
| B. 코드 결정적 선택(시드) | 단순 | 서사 선택권 상실 |

- **권고**: **A**, 무효면 B로 대체. 보충 단계 TIE 실패 경로는 사라진다.

### D3. 묘사 후에만 확정되는 사실(실제 노출·아이템 사용·READ·발견 수거)이 만든 같은 분 반응을 어떻게 다루나

- **필요 이유**: 이 사실들은 본질적으로 묘사 후에 생긴다. 후속 묘사가 없으면 같은 턴에 보여 줄 수 없다.
- **근거**: Ⅳ-2. 영도 현재 저작에서는 같은 턴 반응 0건이지만 컴파일러는 인지 조건을 허용한다.

| 선택지 | 장점 | 단점 |
|---|---|---|
| A. 정본에는 이번 턴에 반영하고, 미표현 관찰 신호를 정본(이월 필드)에 남겨 다음 턴 묘사의 "직전 이후 일어난 일" 블록으로 전달 | 정보 보존·후속 호출 0 | 세션 필드 추가(스키마·되감기 포함), 반응이 다음 턴에 보임 |
| B. 이 사실들은 같은 턴 사건 진행에 넣지 않음(인지·소지만 반영, 사건 평가는 다음 턴 stage-1) | 가장 단순, 현 저작 영향 0 | 미래 인지 조건 저작에서 반응이 한 턴 늦음 |

- **권고**: **B**를 기본으로 하고, 미표현 신호가 실제로 생기는 경우에만 A를 쓴다. 영도 현 저작 기준 B로 충분하다. 다만 Ⅹ-1을 해소하려면 시간 진행은 반드시 stage-1로 옮겨야 한다.

### D4. 접근 확인 지급 반응(`OBSERVER_PAID_ITEM`)을 같은 턴으로 당길지

- **근거**: 지급은 stage-1 계획 결과(`plan.item_payments`)라 묘사 전에 안다. 그런데도 반응은 다음 턴이다(`items.py:744`). 영도 남항 교환 5건이 해당한다.
- **선택지**: A. 같은 턴 진행에 포함 / B. 현행(다음 턴) 유지.
- **권고**: **A**(Q1=A 취지와 일치). 단 v7.17 R4 결정의 재검토이므로 승인이 필요하다.

### D5. 플레이어용 예상 비용 '표시 변경 없음'(Q2)의 범위

- **필요 이유**: 형식은 유지하더라도 산식을 고치면 표시 숫자와 잔액 차단 기준(`max_ink`)이 바뀐다.
- **근거**: `gm.py:3358-3369`, `session_flow.py:514`, `estimate.py`.
- **선택지**: A. 형식·문구 유지, 숫자 정확도 개선 허용 / B. 플레이어 쪽 산식도 동결하고 운영자 보고만 개선.
- **권고**: **A**. 숫자가 지금보다 실제에 가까워지는 것은 정책 변경이 아니라 정정으로 본다. 잔액 차단은 더 정확해진다.

### D6. 외부 청구 대조 수준

- **근거**: Ⅸ.
- **선택지**: A. 일·SKU 집계 대조(결제 내보내기 + 서비스 계정 설정 필요) / B. Vertex AI 이전 후 시나리오 라벨 대조(클라이언트 전환, 별도 패키지 규모) / C. 이번 P1에서는 내부 정합만.
- **권고**: **C → A 순차**. 내부 원장·보고·로그 정합을 먼저 끝내고, 사용자가 GCP 설정을 마련하면 A를 붙인다. B는 P1 범위를 넘는다.

---

## Ⅻ. 설계 착수 가능성 평가

| 구분 | 영역 |
|---|---|
| **충분히 조사되어 설계 가능** | 턴 실행 순서·권위 경계, 후속 묘사의 발생 조건·기능·직접 의존(코드·테스트), 선행 계산의 전달·차단 경로, 커밋·되감기·복구 무영향 판단, 비용 발생 경로 전수와 claim·Settlement 흐름, 마스터 보고 불일치(TTS 단가·토큰 의미) |
| **추가 코드 조사가 필요** | (1) Ⅹ-1의 실제 발현 여부(영도 사건 중 관찰자 ref가 STICKY 유지·eligible 조건에 들어간 것 목록) (2) 재서술 의미 기록에 경과·선후를 넣을 때의 이력 v3 호환 (3) 판단·지시 스키마 확장 위치(`action_intent.extend_judgment_schema` 재사용 가능 여부) (4) `cost_stats` 대체 저장소(세션 간 공유)의 영속 형식 |
| **사용자 결정 필요** | D1~D6 |
| **외부 환경 때문에 확인 못 함** | 운영 원장·로그 기반 실측(후속 묘사 빈도, 사고 토큰 분포, 예측 오차), GCP 청구 접근·지연 수치, DESIGN 11·12 원문 |

**권고 착수 순서**: D1·D2·D3 결정 → Ⅹ-1 표적 확인 → 세계 계획 통합 설계(시간·선후의 stage-1 이동, 보충 진행 축소, 후속 묘사 제거, 재서술 의미 기록) → 비용 계측 정합(TTS 누락·보고 단가·로그 의미 통일·metadata 필드) → 통계 기반 예측(원장 사후 결합) → 외부 대조(D6).
