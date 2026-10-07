# H05 완료 노트 — 구조화 행동 의도 · 플레이어 턴 계획 · 관찰 · 후속 묘사 · 투영 미디어

> 시작 `1906f1b507e3d3360d2fccde375645ab50a64f31`(H04 VERIFIED, 930 passed) · 브랜치 `claude/h05-intent-turn-plan`
> 최종 SHA는 제출 ZIP `commit_info.txt`(이 파일은 그 커밋에 포함). 버전 **7.3.0**(MINOR — 스키마·이력·CommitPlan·compiler 유지).
> H06 NOT STARTED.

## 1. 결과 요약

- 자동 턴이 H03/H04 엔진을 실제로 호출한다(아래 §5 배선). 정본 적용 경계는 여전히 `CommitCoordinator` 하나다.
- **DYNAMIC 세션만** 새 경로를 탄다: `definition_revision`이 바인딩돼 있고(스냅샷 또는 같은 revision 재컴파일), 정량 시간·구조화 장소가 검증됐으며, 시계가 초기화되고 PC 행위자가 배치된 세션이다. 바인딩·seed는 H06이라 **현 운영 세션은 전부 LEGACY**이고 기존 경로와 동일하다(회귀 0).
- `prompts.py`·시나리오 무변경. 판단·추출 스키마는 런타임 사본 확장이고, 지시는 사용자 프롬프트 블록이다.

## 2. H05-A — 구조화 의도 · 플레이어 세계 계획

- `core/action_intent.py`
  - `ActionReferenceManifest`: PC가 이름을 아는 장소·현재 장소(P), 제시된 경로(R), 관찰한 CONFIRM 접근(K), PC가 내용을 아는 정보(I), 시간대. world_version·revision에 결속된다.
  - 기존 판단 호출 스키마에 `action_intent`를 enum 키로 붙인다(**추가 provider 호출 0**).
  - `ResolvedActionIntent`: 후보 밖 키·형식 오류는 그 부분만 거부한다(unresolved). raw 문자열은 정본이 되지 않는다. stale 매니페스트도 거부한다. 이력에는 transient 키 대신 의미 기록(PlaceId·경로 서명·접근 토큰)만 남긴다.
- `core/player_world.py`
  - 세계 모드 판정.
  - 경로 정책: 플레이어가 아는 경로만 후보로 삼고, 의미 있는 차이(10분 이상, 비용비 5/4 초과, 알려진 제약 차이)가 있으면 지시층위 전에 ASK한다. 자동 선택은 허용된 후보 사이에서만 (지각 비용, 구간 서명) 사전순으로 고른다.
  - 이동: 구간마다 그 시점 투영 세계로 실제 접근을 확인하고, 구간 사이에서 H04 진행을 돌린다.
    - 관찰 가능한 신호에서 TRANSIT/중간 장소로 멈춘다.
    - 숨은 봉쇄는 그 구간 시작에 도달했을 때 처음 관찰한다(우회 없음, 제시 단계 누설 없음).
  - 대기·수면: ActivityContext → `OBSERVER_ACTIVITY`. 조용한 신호는 수면을 끊지 않는다.
  - 접근 확인 토큰: 관찰 상태·정책·capability variant로 만든 H04 `access_action_id`. 실제 상태가 바뀌었으면 맞지 않는다(성공을 보장하지 않음).
  - 명시 PC 태도 → PLAYER_EXPLICIT `COGNITION_BELIEF_SET`.
  - 같은 분 기반 연산은 인과 사슬(`chain`)로 묶고, 진행 호출을 이어 붙일 때는 id 이름공간 + 인과 장벽(`namespace_ops`)을 쓴다.

## 3. H05-B — 프롬프트 투영 · 관찰 · 같은 분 선후 · 결정적 충돌

- 묘사 입력:
  - 투영 장면 오버레이(경과·시각·도착/중단·관찰 신호, PC가 볼 수 있는 것만).
  - 장소 블록은 `PreparationView`로 만든 투영 `world_timeline`(정본 미변경).
  - 지시층위 이미지 후보는 투영 장면 + 상위 상속으로 한정.
- 추출(`world_observation`):
  - 장면에 함께 있는 행위자가 아는 정보의 노출 → PC 획득(믿음 불변).
  - 비코드 소유 추가 시간: 코드 소유 경과가 있으면 버리고, 상한은 240분.
  - 선후 선택, 이미 표현된 신호.
  - 국면·경로·접근·PC 믿음 판정 필드는 없다. DYNAMIC 세션에서 추출의 장소·날짜는 적용되지 않으며, 레거시 `world_timeline`은 정본 미러다.
- 같은 분 서사 선후: 허용 순서 하나(O 키)를 받아 그 순서로 재계획한다. 모호·무효·복수 tie면 READY 불가(`COMMIT_VALIDATION_FAILURE`)다. 장면 밖 충돌은 H04 `WORLD_PROJECTION_CONFLICT` 그대로이고 선택지로 보내지 않는다.
- 결정적 충돌:
  - 묘사 전이면 묘사 provider 호출 없이 FAILED_SYSTEM(`WORLD_PROJECTION_CONFLICT`)으로 끝낸다.
  - 묘사 후(보충 진행)면 재시도·RETRY_PENDING 없이 같은 코드로 끝내고, 마스터 내구 진단(`error_log`)을 남긴다.
  - 어느 쪽이든 발생한 provider 비용은 사실로 유지되고, 청구는 0이다.

## 4. H05-C — 후속 묘사 · 투영 미디어 · 출력 소유

- `ForegroundContinuationPlan`: 보충 진행이 현재 장면 IF_OBSERVABLE 신호를 새로 만들었고, 그 신호가 표현되지 않았을 때만 만든다(본 묘사 전 신호와 추출의 `represented_signals`로 중복 제거).
  - 같은 논리 턴이며 판단 재호출, 경로 재질문, 추가 시간, 재추출이 없다.
- 후속 묘사 provider 호출은 준비 객체의 필수 작업(`FOREGROUND_CONTINUATION_NARRATION`)으로 등록된다.
  - 비용 연산은 `TURN_FOREGROUND_CONTINUATION`이고, 같은 턴 비용 멤버십에 정확히 1회 들어간다.
  - 생성은 READY 전에 끝난다. 실패하면 READY 불가다.
  - 전달은 durable COMMITTED 뒤 파생 게임 출력(시도 소유·정리 대상)으로 하고, 그다음에 다음 행동 질문이 나온다.
  - 텍스트는 같은 시도의 대화 이력에 붙는다.
- 투영 미디어:
  - 성공 이동이면 목적지 장면 이미지를 쓰고, 태그가 없으면 자동 선택한다.
  - 중단(중간 장소·TRANSIT)이면 도착 전 목적지 장소 이미지를 송출 단계에서 차단한다.
  - 디스플레이·HUD는 커밋 후 정본만 읽는다.

## 5. 자동 턴 배선(production caller)

| 경로 | 호출 |
|---|---|
| `GMCog._call_judgment` | `action_intent.build_manifest` / `extend_judgment_schema` / `resolve_intent` |
| `GMCog._run_gm_logic_loop` | PROCEED → `_ensure_world_plan` → `player_world.plan_player_turn`, 경로 ASK |
| `GMCog._call_gm_logic` · `_dispatch_proceed` | `turn_world.narration_overlay`, 투영 이미지 후보 |
| `GMCog._run_extraction` · `_prepare_extraction` | `build_observation_manifest` · `process_observation` · 후속 묘사 작업 등록 |
| `GMCog._join_and_ready` | `turn_preparation.stage_world_ops`(PLAYER_TURN 묶음 — owner = 준비 객체) |
| `GMCog._apply_commit_effects` | 후속 묘사 파생 송출 |
| `GameCog._execute_proceed` | 투영 `world_timeline` 뷰 · `_projected_media_filter` |

- cogs는 H02~H04 엔진을 직접 부르지 않고 H05 모듈을 거친다(특성화 스캔으로 고정).
- DYNAMIC 경로의 레거시 장소/시간 권위는 막혀 있다: 판단의 `build_move_hint`를 매니페스트로 대체하고, 추출의 장소·날짜를 미러로 대체했다.

## 6. 변경 파일

- 운영 코드
  - 신규: `core/action_intent.py`, `core/player_world.py`, `core/turn_world.py`
  - 수정: `cogs/gm.py`, `cogs/game.py`, `core/__init__.py`, `core/constants.py`(7.3.0)
  - 기반 파일의 작은 인터페이스 확장:
    - `core/turn_transaction.py`(런타임 `world_turn`)
    - `core/turn_preparation.py`(`TASK_FOREGROUND_CONTINUATION`, `world_turn`·`world_observation_raw`, `stage_world_ops`)
    - `core/cost_ledger.py`(`OP_TURN_FOREGROUND_CONTINUATION`)
- 테스트
  - 신규: `tests/policy/test_h05a_intent_planning.py`(14), `tests/policy/test_h05b_observation.py`(5), `tests/policy/test_h05bc_turn_wiring.py`(24), `tests/characterize/h05_scope.py`
  - 의도적 특성화 갱신: `test_h01/h02/h03/h04_no_cutover.py`. H05 모듈만 엔진 소비자로 허용했고, 정의는 읽기 전용(바인딩 writer 0 유지) 단언을 추가했다(+1).
- 문서: `CLAUDE.md`, `DEVLOG.md`, 이 노트.

## 7. 검증

| 항목 | 결과 |
|---|---|
| H05 대상 | 48 passed(A 14 · B 5 · B/C 배선 24 · H01 특성화 5) |
| H01~H04 정책·특성화 | 114 passed |
| 전체 pytest | **974 passed**, 0 failed, xfail/XPASS 0(특성화 56 · 결함 27 · 정책 891) |
| compileall · 루틴 ①②④⑤ | OK · cogs 9 / 명령어 44 / views 5 · 문서 OK(core 서브모듈 77→80) |
| scenario_check --all | 5 × compiler v3, ERROR 0(영도 WARNING 3 = 기존) |
| provider | 운영 코드 `generate_content` 18 → **19**: `GMCog._generate_continuation` 1곳(DESIGN 12 §36 조건부 예외 — 현재 장면 추출 후 결과가 있을 때만, 기본 경로 추가 호출 0) |
| 범위 | `scenarios`·`prompts.py`·퀘스트·캐시·커밋/저널·스냅샷·컴파일러·H03/H04 엔진 diff 0. 세션 `definition_revision` writer 0 |

## 8. 남은 것 · 발견 사항(H06/H07 또는 calibration)

1. **운영 활성은 H06 이후.** 정의 스냅샷 바인딩과 초기 행위자/시계/지식 seed가 없으면 DYNAMIC이 되지 않는다(설계대로 dormant).
2. **동적 세계 재서술·되감기 이행은 H06.** 재서술 시도는 판단을 다시 돌리지 않으므로 `world_turn`이 없고, 세계 연산 없이 커밋된다. 의미 선택은 판단 이력(`semantic_record`)에 남겨 두었다.
3. **사건 조건이 PC ActorId를 참조할 수 없다**(컴파일러: 저작 행위자만, H04 계약). 그래서 "PC가 정보를 알게 됨 → 사건 반응" 같은 저작은 현재 불가능하다. 후속 묘사는 보충 시간·사건 연쇄로 검증했다. 관찰자/PC 슬롯 참조가 필요하면 H06/H07 저작 이행 때 결정해야 할 **교차 도메인 항목**이다(이번에 넓히지 않음).
4. `EXPLORE_DIRECTION`은 미구조화(unresolved)로 처리한다. 장소 안 이동(LOCAL)은 정본 장소를 바꾸지 않는다.
5. calibration: 경로 자동 선택 임계(10분·5/4), 추가 시간 상한(240분), PC capability = 보유 소지품 이름. 영도 정량 저작 후 조정한다.
6. `prompts.py` 시스템 지시문에 `action_intent`/`world_observation` 설명을 넣는 것은 승인 후 제안 사항이다(현재는 사용자 프롬프트 블록으로 충분히 동작).
