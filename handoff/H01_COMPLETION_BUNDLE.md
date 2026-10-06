# H01 완료 번들 — 정의 컴파일러·정체성·스냅샷 기반

- 작성일: 2026-10-06
- 프로그램: INDAIM 동적 세계 구현(H01~H11) · 패킷 `INDAIM_IMPLEMENTATION_PREP_PACKET_v0.2_2026-10-06_de367c7`
- **BASE SHA:** `de367c7afb79700436cb2c79f8645e43f8976db2` (main, clean, v6.0.0 · `SCHEMA_VERSION` 3)
- **FINAL SHA:** 이 번들을 담은 보정 커밋(브랜치 `claude/h01-definition-compiler`). 정확한 값은 완료 보고에 기재한다 — 커밋은 자기 SHA를 담을 수 없다.
- 이력: 1차 제출 `cb5905f8bdcc53282874f83949f4e11194d0170d` → 사용자 보정 요구 1~4 반영(§15).
- 제품 버전: **변경 없음**(`__version__` = 6.0.0). H01은 개발 패키지명이며 출시가 아니다.
- 상태: H01 구현 완료 · 독립 검증 대기. **H02는 시작하지 않았다.**

---

## 1. 범위

capability 감지, 시나리오 정의 컴파일러, 안정 저작 ID, 정규 직렬화, 정의 revision, 세션 정의 스냅샷 유틸리티, `tools/scenario_check.py` 뼈대.

**production cutover 없음.** 어떤 플레이 경로·캐시·프롬프트·판단층위·추출·청구 경로도 새 모듈을 부르지 않는다. 런타임은 여전히 `core.io.load_scenario_from_file`이 읽은 raw `scenario_data`를 쓴다.

## 2. 변경 파일

| 파일 | 변경 |
|---|---|
| `core/__init__.py` | H01 서브모듈 3개 import(+헤더 주석). `__all__` 미변경(WP 모듈 관례와 동일) |
| `CLAUDE.md` | H 프로그램 1문단, 파일 맵(`tools/scenario_check.py`), core 표 '세계 정의' 절, 서브모듈 57→60(verify_docs `--fix`), 테스트 수 637→672, 인도 번들, 검증 루틴 ⑥ |
| `DEVLOG.md` | '미출시 — 동적 세계 H01' 절(버전 번호 없음), 데이터 규모 줄(core 60 · 테스트 672) |
| `_PROJECTINFO.txt` | core 60 · 테스트 672 · `tools/scenario_check.py` · '세계 정의' 묶음 |

## 3. 신규 파일

| 파일 | 줄 | 내용 |
|---|---|---|
| `core/world_ids.py` | 157 | 정규 직렬화(키 정렬·문자열 키만·NaN/Infinity/set 거부)·순서 지문·`stable_digest(namespace, *parts)`·저작 ID 파생(NPC ActorId·PC ActorId·인라인 비밀/퀘스트 hidden InfoId·이동 구간 id) |
| `core/definition_compiler.py` | 872 | 원본 로드(읽기 전용)·capability 감지·저작 sugar 컴파일·검증 진단(ERROR/WARNING/INFO)·revision·요약·capability 표·이행 보고 |
| `core/definition_snapshot.py` | 235 | `sessions/{id}/definition_snapshot.json` 원자적 쓰기(write→flush→fsync→replace)·읽기 재검증·봉투/정의 compiler_version 일치 검증·멱등·다른 revision 덮어쓰기 거부·도구용 verify |
| `tools/scenario_check.py` | 191 | 요약·진단·capability 표·`--all`·`--json`·`--verbose`·`--compile-out`·`--migration-report`·`--verify-snapshot`. 출력 경로가 시나리오 디렉터리면 거부 |
| `tests/policy/test_h01_definition_compiler.py` | 555 | 정책 31건 |
| `tests/characterize/test_h01_no_cutover.py` | 83 | 특성화 4건(H01 경계 — H10/H04~H09가 갱신) |
| `handoff/H01_scenario_reports/` | — | 전 시나리오 컴파일 보고(txt·json), 영도·무협·다크판타지 이행 보고(md) |
| `handoff/H01_COMPLETION_BUNDLE.md` | — | 이 문서 |

## 4. 구현한 요구

| ID | 내용 | 검증 |
|---|---|---|
| REQ-024 (H01분) | 저작 NPC ActorId = 명시 `actor_id` 우선, 없으면 `stable_digest("actor.authored_npc.v1", scenario_id, 사전 키)`. 표시 이름 변경은 ID 유지, 명시 ID 없는 NPC의 사전 키 변경은 ID migration. 인라인 비밀 InfoId는 ActorId에 묶임 | `test_authored_ids_*`, `test_actor_identity_is_separate_from_display_name`, `test_default_npcs_key_rename_without_explicit_id_is_id_migration`, `test_explicit_actor_id_rules` |
| REQ-052 (프리미티브) | 스냅샷 원자적 쓰기·해시 검증·순서 지문 검증·덮어쓰기 거부·손상 위 재생성 거부. **세션 바인딩·restart 권위는 H10** | `test_snapshot_*` 4건 |
| REQ-066 | 메타데이터 없는 시나리오 = legacy(빈 capability). 출시 시나리오 5종 전부 ERROR 0, raw 모양이 키 순서까지 복원 | `test_every_shipped_scenario_compiles_as_legacy_without_error` ×5 |
| REQ-068 | 원본 미수정(바이트 대조), 산문·`places[*].npcs`·connected에서 기계 진실 미생성, 비대칭 자동 보정 없음, 도구 출력의 시나리오 디렉터리 거부 | `test_compile_does_not_mutate_input_or_touch_sources`, `test_no_machine_truth_*`, `test_scenario_check_tool_*`, `test_compiler_does_no_file_writes` |
| REQ-076 | 시작 HEAD = `de367c7…` clean 확인 후 편집 | 본 번들 §7 |
| REQ-077 | 전체 회귀 무실패(기준 637 → 현재 672) | §6 |
| REQ-080 | 제품 버전 미변경 | `core/constants.py` diff 없음 |
| DESIGN 11 §12~13 | 저작 정체성 = scenario + structural source key, 호출 순서·난수 무관 | `test_revision_and_ids_are_process_and_hashseed_independent`(PYTHONHASHSEED 0/4242 별도 프로세스) |
| DESIGN 08 §4·§31~35 | capability 의존성·심각도·예약 키 경고 | `test_capability_declaration_validation`, `test_dynamic_actor_location_requires_structured_places_as_error`, `test_reserved_dynamic_keys_warn_not_silently_ignored` |
| DESIGN 08 §94 | 컴파일러 의미 버전이 revision에 결속 | `test_compiler_version_is_bound_into_revision`, `test_snapshot_corruption_mismatch_missing_are_loud` |
| 퀘스트 정보 정체성 | hidden InfoId = 시나리오 id + 퀘스트 id(version·본문 무관), 같은 id 중복 ERROR | `test_quest_hidden_compile_and_identity`, `test_quest_hidden_info_id_survives_version_and_text_change` |
| DESIGN 08 §9 | 저작 sugar: NPC 비밀·퀘스트 hidden·시작 틀·양방향 구간 | `test_quest_hidden_*`, `test_start_frame_facts_stay_templates`, `test_bidirectional_edge_sugar_*` |

## 5. production 동작

- **변경된 동작: 없음.** `core/__init__.py`의 import 3줄 외 production 소스 diff 없음.
- 보호 경로 무변경 확인: `prompts.py`·`scenarios/`·`cogs/`·`main.py`·`core/constants.py`·`core/io.py`·`core/models.py`·`core/cache.py`·`core/prompt.py`·`core/cache_lifecycle.py` (`git diff --quiet` 통과).
- 새 provider 호출 0 · 청구/환급 의미 변경 0 · 캐시–정의 결합 0(캐시 빌드·재발급 경로가 새 모듈을 읽지 않음) · 판단층위(ASK/ROLL) 캐시 0회 구조 그대로.
- 세션 필드 추가 0(`SESSION_FIELDS` 82 유지).

## 6. 검증 결과

```
python -m compileall -q core cogs tests main.py prompts.py tools      → OK
pytest -q (전체)                                                    → 672 passed, 0 failed, 0 xfailed, 0 XPASS (1 warning: discord audioop DeprecationWarning — 기준과 동일)
  · 기준(de367c7, 이 환경에서 재현): 637 passed
  · H01 신규: 35 (정책 31 · 특성화 4)
  · 분류: 특성화 41 · 결함 27 · 정책 604
H01 대상 테스트(2 파일)                                              → 35 passed
검증 루틴 ① 구문·self 참조 OK · ② 재수출 OK / 필드 OK · ③ 퀘스트 트리 OK
          ④ verify_docs 문서 수치 OK (21항목) · ⑤ cogs 9 · 명령어 44 · views 5
          ⑥ tools/scenario_check.py --all → 종료 코드 0
```

### 시나리오 컴파일 보고 (compiler v1)

| 시나리오 | 모드 | ERROR | WARNING | 인물 | 장소 | 인라인 비밀 | 퀘스트 hidden | 시작 틀 | revision |
|---|---|---|---|---|---|---|---|---|---|
| 영도 | legacy | 0 | 3 | 47 | 122 | 47 | 42 | 8 | `sha256:0d8255bc118b7423da81b1f604fe7887f31c43c91198ca8724c69bc58b0999cb` |
| 무협 | legacy | 0 | 0 | 233 | 0 | 24 | 0 | 0 | `sha256:1fafeb86ca5cd45fd88de82cdbcce0a92459dea34e335c913e186dad7ed07e6f` |
| 다크판타지 | legacy | 0 | 0 | 21 | 0 | 0 | 0 | 0 | `sha256:570f1d14156568f20ed45889d1e4960b574a559f203ea13ef1966ab21d7010e6` |
| 빈시나리오 | legacy | 0 | 0 | 0 | 0 | 0 | 0 | 0 | `sha256:07c2ef195719d64eb15259f4f3c32f0dd34a84ecad433e7072a0f2d1bd3cc15f` |
| scenario.example | legacy | 0 | 0 | 2 | 0 | 1 | 0 | 0 | `sha256:29be07e0b058a23752b856448fbd798046020198c686a86e207a12470036ed64` |

원문: `handoff/H01_scenario_reports/compile_all.txt`·`compile_all.json`(원본 파일 SHA-256 provenance 포함).

## 7. 시작 기준 확인

```
git fetch origin main ; git rev-parse HEAD origin/main
→ de367c7afb79700436cb2c79f8645e43f8976db2 (둘 다) · git status --short 빈 출력
```

85333ee를 checkout하지 않았다. `__version__`을 되돌리거나 올리지 않았다.

## 8. direct-writer / cutover 스캔

- `test_no_production_caller_of_h01_modules` — `main.py`·`cogs/*`·`core/*`(H01 3모듈·`__init__` 제외)에 `world_ids`·`definition_compiler`·`definition_snapshot` 참조 0.
- `test_h01_modules_are_pure_no_provider_discord_or_session_save` — H01 모듈은 `google`·`discord` 등을 import하지 않고, `io`·`cache`·`cost*`·`settlement`·`commit_coordinator`·`cache_lifecycle`을 import하지 않으며, 세션 저장·`accrue`·`call_with_retry`를 부르지 않는다.
- `test_compiler_does_no_file_writes` — 컴파일러에 쓰기 모드 `open` 없음. 파일 쓰기는 `definition_snapshot`(세션 디렉터리)과 도구(사용자 지정 출력 경로)뿐.
- 정본 세션 상태를 바꾸는 새 경로 0. `CommitCoordinator` 우회 writer 0.

## 9. 영속·이력 커버리지

- 새 세션 필드 없음 → `SESSION_FIELDS`·`REVERSIBLE_FIELDS`·`COMMIT_OWNED_FIELDS`·`CANONICAL_DOMAINS`·`TRACKED_PATHS` 무변경.
- 정의 스냅샷 파일은 아직 어떤 세션도 만들지 않는다. 설계상 정의는 되감기 대상이 아니며(DESIGN 01 §10), turn_history 스냅샷에 넣지 않는다 — H10이 바인딩할 때도 이 분류를 유지해야 한다.

## 10. 결정 기록 / 패킷과의 차이

| # | 내용 | 근거·영향 |
|---|---|---|
| D1 | **세션 필드(`definition_revision`·`narrative_seed`) 미도입 → H02로 이관** | 패킷 §4는 "H02와 충돌하지 않게 최소화"를 요구. 새 필드는 turn_history 분류(REVERSIBLE/OPERATIONAL/CACHE_DERIVED)·READY 지문·롤백 커버리지 결정이 함께 필요하고, 그것이 H02 산출물이다. H01에 넣으면 써 주는 경로가 없는 죽은 필드가 된다(DEVLOG 실패 패턴 ①). 마스터 플랜 표도 `world_version/narrative_seed`를 H02에 둔다. |
| D2 | 시나리오 메타데이터 키 = **`scenario_schema`** `{version, capabilities}` | DESIGN 08 §1이 "키 이름은 구현 단계에서 고정"으로 위임. 세션 `SCHEMA_VERSION`과 혼동을 막기 위해(§93) `schema` 대신 분리된 이름. 아직 어떤 시나리오도 쓰지 않으므로 검토 후 개명 비용 0. |
| D3 | compiler v1이 도메인 검증을 끝내는 capability = **`stable_actors`만**. 나머지 7개는 선언을 기록하고 `CAP_VALIDATION_DEFERRED` WARNING + `deferred_capabilities`로 표시 | 각 도메인 검증기는 H04~H07 몫. 후속 WP는 검증기를 넣을 때 `VALIDATED_CAPABILITIES`로 옮기고 `COMPILER_VERSION`을 올린다. **H10은 deferred capability가 남은 정의를 런타임 활성화 근거로 쓰지 말 것.** |
| D4 | `dynamic_actor_location → structured_places` 의존 = **ERROR** | 1차 제출은 WARNING이었으나 **사용자 결정(보정 요구 3)으로 ERROR 확정**. |
| D5 | 컴파일된 정의에 **`legacy_source`(raw 시나리오+퀘스트 원문)** 를 함께 보존 | 현행 런타임은 raw 모양을 직접 소비하므로, H10에서 스냅샷 하나로 세션을 복원하려면 원문이 필요하다. 새 엔진의 계약은 구조화 절(actor/place/edge/info/initialization)이며 raw는 legacy 호환용임을 키 이름으로 구분했다. DESIGN 06 §17("원문 전체 복사보다 compiled 우선")과의 관계를 **검토 요청**. |
| D6 | revision이 **dict 순서와 `compiler_version`까지** 덮음(`legacy_source_order_digest`, 정의 최상위 `compiler_version`) | 장소 순회·NPC 전개 순서가 런타임(경로 탐색·캐시 텍스트)에 닿는다. 순서만 바꾼 원본도 다른 revision. 정규 직렬화 자체는 키 정렬. |
| D7 | 이동 구간 id = 명시 id 또는 `edge_<digest(scenario, from, to)>`, 방향 구간 = `<pair>#fwd` / `<pair>#rev`. 속성은 저작값 그대로(거리 생성 없음), 의미 검증은 H05 | 목록 위치를 id에 넣지 않음(재정렬 내성). id 없는 같은 (from,to) 중복은 ERROR. |
| D8 | 퀘스트는 hidden.truth만 InfoDefinition으로 컴파일. InfoId 정체성 입력 = 시나리오 id + 퀘스트 id(`info.quest_hidden.v2`). `quest_version`은 메타데이터. 같은 퀘스트 id 중복은 version이 달라도 ERROR(`QUEST_ID_DUPLICATE`). 사건 원형 변환은 하지 않음 | 사용자 결정(보정 요구 1). 현행 런타임 `quest._find_quest`가 id만으로 첫 항목을 찾으므로 같은 id의 다른 version은 도달 불가. 사건 원형은 H07/H11 |
| D9 | 인라인 비밀 InfoDefinition은 `owner_actor_id`만 가짐(인지 seed 없음) | cognition seed 의미는 H06. |
| D10 | CLAUDE.md '계획서 선행' 원칙 | 사용자가 제공한 H01 WP 패킷을 승인된 계획으로 보고 실행했다. |

설계 의미 변경 0. 위 항목은 모두 패킷이 구현자에게 위임한 범위(이름·내부 구조) 또는 보류(이관) 결정이다.

## 11. 새로 발견한 사실 (실물 확인 완료)

| # | 사실 | 처리 |
|---|---|---|
| F1 | 영도 별칭 가림 2건: `해련 자치 마을`의 별칭 `해련사`와 같은 이름의 장소 키 `해련사`가 있고, `저지대 상업지구`의 별칭 `봉래시장`과 같은 이름의 하위 장소 `봉래시장`이 있다. `places.resolve`는 정확 일치를 먼저 보므로 두 별칭은 상위 장소로 해석되지 않는다 | **수정하지 않음**(시나리오 불가침·데이터 불일치는 질문 — 원칙 ⑧). 의도된 것인지 사용자 확인 필요 |
| F2 | 영도 connected 비대칭 71쌍 · 거리 미저작 connected 쌍 234 · 암묵 상하위 이동 후보 2 · 장소 연관 인물(현재 위치 아님) 34곳 | 이행 보고에 전수 나열. H05/H11 저작 입력 |
| F3 | 영도 퀘스트 44 중 `place_home_quarter`·`place_market_dig`는 `hidden: {}` — 런타임(`quest.py`)도 이면정보 없음으로 본다 | 정상. hidden InfoDefinition은 42개 |
| F4 | 무협 NPC 233명 중 24명에 `비밀` 필드가 있다(CLAUDE.md 무협 설명에는 언급 없음) | 인라인 비밀 24개로 컴파일(런타임 효과 없음) |
| F5 | `!캐시 재발급`(`cogs/system.py`)은 여전히 원본 재로드 + `expand_default_npcs(replace=True)` (BLK-01) | 미변경. H10 |
| F6 | 재시작 복구(`core/cache.py restore_sessions_from_disk`)는 현재 디스크 원본을 다시 읽는다 — DESIGN 06이 막으려는 정의 drift 경로 | 미변경. H10이 스냅샷 로드·검증으로 교체 |
| F7 | 시나리오 파일에 `id` 필드가 없어 시나리오 id = 파일명. 명시 ID 없는 저작 NPC의 ActorId 입력은 (파일명, `default_npcs` 사전 키)이므로 **시나리오 파일명 또는 사전 키를 바꾸면 ActorId가 바뀐다 = ID migration**. 표시 이름(`name`)만 바꾸면 유지. 명시 `actor_id`가 있으면 둘 다 무관 | 설계(DESIGN 01 §7.3) 그대로. 테스트로 고정(`test_default_npcs_key_rename_without_explicit_id_is_id_migration`) |

## 12. 알려진 한계

- 스냅샷 동시 쓰기 직렬화 락 없음 — 두 writer가 경합하면 읽기 재검증에서 `DefinitionSnapshotMismatch`로 크게 실패한다(조용한 오염 없음). H10이 lifecycle 락 안에서 호출해야 한다.
- capability 7종은 도메인 검증 미구현(D3).
- 이행 보고의 암묵 상하위 후보 판정은 `places.is_container` 규칙을 근사한다(상위 존재 = 하위 있음).

## 13. 다음 WP(H02~)가 믿어도 되는 불변식

1. `core.world_ids.canonical_json`/`content_digest`/`stable_digest`는 프로세스·PYTHONHASHSEED·dict 삽입 순서와 무관하다. 새 결정적 ID는 반드시 `stable_digest(네임스페이스.vN, ...)`로 만들고 문자열 이어붙이기를 쓰지 않는다.
2. 저작 NPC ActorId는 `definition["actor_index"][레거시 키]`로 얻는다. **표시 이름(`display_name`/`name`) 변경은 ActorId를 유지한다. 명시 `actor_id`가 없는 저작 NPC의 `default_npcs` 사전 키 변경(또는 시나리오 파일명 변경)은 새 ActorId를 만드는 ID migration이다** — 키를 바꾸려면 명시 `actor_id`를 먼저 고정하거나 명시적 DefinitionMigration으로 매핑해야 한다. PC ActorId = `world_ids.pc_actor_id(uid)`.
2-1. 퀘스트 hidden InfoId는 (시나리오 id, 퀘스트 id)로만 정해진다. version·hidden 본문을 바꿔도 유지되고, 퀘스트 id는 정의 안에서 유일하다.
3. 정의 revision = `definition_compiler.definition_revision(definition)` — 정의 최상위 `compiler_version`과 dict 순서를 포함한 정의 전체. 원본 provenance·기록 시각은 제외. 컴파일 출력 의미가 바뀌는 변경은 `COMPILER_VERSION`을 올려 revision을 바꿔야 한다. 스냅샷 봉투의 `compiler_version`은 정의의 값과 같아야 한다(다르면 `DefinitionSnapshotCompilerVersionMismatch`).
4. `definition_snapshot.write_snapshot`은 다른 revision을 덮어쓰지 않고, `read_snapshot`은 형식·revision·순서 지문을 재계산해 어긋나면 예외를 던진다. 정의 교체는 별도 명시적 migration이어야 한다.
5. legacy 시나리오(`scenario_schema` 없음)는 컴파일 ERROR 없이 통과하며 capability는 빈 집합이다. 새 엔진은 capability가 없는 시나리오에서 활성화되면 안 된다.
6. `deferred_capabilities`가 비어 있지 않은 정의는 해당 도메인 런타임 활성화 근거가 아니다(D3).
7. 컴파일러는 connected를 구간으로, `places[*].npcs`를 현재 위치로, 산문을 수치로 바꾸지 않는다. 이 성질을 깨는 변경은 테스트(`test_no_machine_truth_*`)가 막는다.
8. H01 모듈의 production 호출자는 0이다(`tests/characterize/test_h01_no_cutover.py`). 배선하는 WP가 그 테스트를 의도적으로 갱신한다.
9. 세션 정본 필드·`SCHEMA_VERSION`은 H01에서 변하지 않았다. `definition_revision`·`narrative_seed` 도입과 이력 분류는 H02의 첫 결정이다.

## 14. 재현

```bash
git checkout <FINAL SHA>
pip install -r requirements.txt -r requirements-dev.txt
python -m compileall -q core cogs tests main.py prompts.py tools
python -m pytest -q                                   # 672 passed
python -m pytest -q tests/policy/test_h01_definition_compiler.py tests/characterize/test_h01_no_cutover.py   # 35 passed
python tools/scenario_check.py --all                  # exit 0, ERROR 0 ×5
python tools/scenario_check.py 영도 --migration-report -
python tools/verify_docs.py                           # OK (21항목)
```

## 15. 보정 이력 (1차 제출 `cb5905f` 이후)

| 요구 | 반영 | 검증 |
|---|---|---|
| 1. 퀘스트 hidden InfoId에서 `quest_version` 제거 | `world_ids.quest_hidden_info_id(scenario_id, quest_id)` — 네임스페이스 `info.quest_hidden.v2`. `quest_version`은 InfoDefinition 메타데이터로만 유지. 같은 퀘스트 id 중복은 version이 달라도 `QUEST_ID_DUPLICATE` ERROR(현행 `quest._find_quest`가 id만으로 첫 항목을 찾음을 소스로 확인) | `test_quest_hidden_compile_and_identity`(같은 version·다른 version 중복 둘 다 ERROR), `test_quest_hidden_info_id_survives_version_and_text_change`(version a/b/None·본문 변경에도 같은 InfoId) |
| 2. 컴파일러 의미 버전을 revision에 결속 | 정의 최상위에 `compiler_version` 저장 → `definition_revision`이 이를 포함한 정의 전체를 해시. `CompileResult.compiler_version`은 정의 값에서 읽음. 스냅샷: 정의의 `compiler_version` 형식 검사, 봉투 값이 정의 값과 다르면 쓰기(`build_envelope`)·읽기 모두 `DefinitionSnapshotCompilerVersionMismatch` | `test_compiler_version_is_bound_into_revision`(버전만 바꿔도 revision이 달라지고 다른 차이는 없음), `test_snapshot_corruption_mismatch_missing_are_loud`(봉투 변조·정의 내 버전 변조·잘못된 봉투 생성) |
| 3. `dynamic_actor_location → structured_places` ERROR 승격 | `CAPABILITY_DEPENDENCIES` 심각도 ERROR | `test_dynamic_actor_location_requires_structured_places_as_error`(단독 선언 ERROR·revision 없음, 함께 선언하면 통과) |
| 4. ActorId rename 불변식 정정 | §13-2·F7·REQ-024 정정. `world_ids.authored_npc_actor_id` docstring에도 명시 | `test_default_npcs_key_rename_without_explicit_id_is_id_migration`(키 변경 → ID 변경, 명시 ID면 유지) |
| 5. H02 시작 금지 | 시작하지 않음 | — |

- 출시 시나리오 5종의 revision이 모두 바뀌었다(정의에 `compiler_version`이 들어가고 퀘스트 hidden InfoId 규칙이 바뀌었기 때문). 1차 revision으로 만든 스냅샷은 production에 없다(H01은 미배선) — 영향 없음.
- `COMPILER_VERSION`은 1로 유지했다. 미출시·미영속 상태에서의 보정이므로 버전 증가 대상이 아니라고 판단했다. 이후 컴파일 출력 의미가 바뀌면 올린다(§13-3).
- 테스트: H01 31 → 35(정책 +4), 전체 668 → 672.

