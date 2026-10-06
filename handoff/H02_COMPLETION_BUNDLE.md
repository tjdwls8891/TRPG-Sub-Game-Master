# H02 완료 번들 — canonical 상태·영속·이력 + WorldMutationBatch·ProjectedWorld·정본 변이 기반

- 작성일: 2026-10-06
- 프로그램: INDAIM 동적 세계 구현 — 통합 로드맵 v0.3(H01~H07). 패킷 `INDAIM_H02_CANONICAL_MUTATION_PACKET_v0.1_2026-10-06_28cad87`
- **BASE SHA:** `28cad8751076b2f02a8efc0048733f374d919832` (H01 승인본, clean)
- **H02-A checkpoint SHA:** `fd44d6c1f748756d9902deb714bcc5ea7a9287a5`
- **FINAL SHA:** 이 번들을 담은 커밋(브랜치 `claude/h02-canonical-mutation`). 정확한 값은 완료 보고에 기재한다.
- 제품 버전: **7.0.0** (6.0.0 → 7.0.0 — §10 D1, 사용자 결정)
- `SCHEMA_VERSION`: **4** · `HISTORY_SCHEMA_VERSION`: **3** · `COMMIT_PLAN_VERSION`: **2**(v1 읽기 지원) · H01 compiler version: 1(무변경)
- 상태: H02 구현 완료 · 독립 검증 대기. **H03은 시작하지 않았다. push하지 않았다**(패킷 지시).

---

## 1. 범위

H02-A(정본 필드·영속·이력)와 H02-B(변이 묶음·투영·공통 primitive·PLAYER_TURN 연결)를 순서대로 구현했다. H02-A 전체 회귀(682 passed) 통과 후 H02-B를 시작했다.

production 효과는 하나다: **모든 커밋된 PLAYER_TURN이 `world_version`을 전진시킨다**(묶음은 항상 비어 있다). 그 밖의 플레이 동작·provider 호출·판단층위 캐시 사용·청구/환급·캐시 의미는 바뀌지 않았다.

## 2. 변경 파일

| 파일 | 변경 |
|---|---|
| `core/io.py` | `SCHEMA_VERSION` 3→4, `SESSION_FIELDS.update(world_state.ALL_WORLD_FIELDS)`(82→95), v3→v4 이행 단계 |
| `core/models.py` | 생성자에 H02 13필드(레지스트리 기본값을 필드별 명시 대입, 시드 도출) |
| `core/turn_preparation.py` | `CANONICAL_DOMAINS += GAMEPLAY_WORLD_FIELDS`; 준비 객체 world lane(기준선·묶음·투영·digest·충돌); `capture_world_baseline`·`_world_ready_reasons`·`_freeze_and_project_world`; `ReadyProof` world 필드(기본값 있음) |
| `core/turn_history.py` | `HISTORY_SCHEMA_VERSION` 2→3, `REVERSIBLE_FIELDS += GAMEPLAY_WORLD_FIELDS`, v2 레코드 누락 필드 레거시 기본값 합성 복원 |
| `core/commit_coordinator.py` | `CommitPlan` v2(+source/target world_version·definition_revision·world_mutation_digest, v1 지문 재현), `build_commit_plan` 대상 version 결정적 생성, Phase A world 가드, Phase C 안 투영 반영 + world_version 전진, 복구 시 v2 대상 version 대조 |
| `core/turn_transaction.py` | `FailureCode.WORLD_PROJECTION_CONFLICT` |
| `core/constants.py` | `__version__` 7.0.0 |
| `core/__init__.py` | H02 모듈 3개 import(+헤더 주석) |
| `tests/characterize/test_h01_no_cutover.py` | H01 경계 갱신(§8) |
| `CLAUDE.md`·`DEVLOG.md`·`_PROJECTINFO.txt` | 통합 로드맵 H01~H07, 버전 정책(개발 패키지·내용 기준), v7.0.0, 모듈 표·수치 |

## 3. 신규 파일

| 파일 | 줄 | 내용 |
|---|---|---|
| `core/world_state.py` | 124 | H02 필드 **단일 출처**(가역 11 + 정체성 2), 시드 도출, v3→v4 이행, `world_version` 인과 정체성(`next_world_version`) |
| `core/world_mutation.py` | 546 | `WorldMutationOp`·`WorldMutationBatch`(LocalRef·검증·동결·digest·직렬화)·`OperationRegistry`(production 비어 있음)·`ProjectedWorld`·`project`·병합 4종·충돌 분류·`apply_projection` |
| `core/canonical_mutation.py` | 272 | INITIALIZATION·ADMIN_EDIT 공통 원자 적용 + `canonical_mutation_journal.jsonl` + 복구 분류. production 호출자 0 |
| `tests/policy/test_h02a_world_state.py` | — | H02-A 정책 10건 |
| `tests/policy/test_h02b_world_mutation.py` | — | H02-B 정책 20건 |
| `tests/characterize/test_h02_no_cutover.py` | — | H02 경계 특성화 5건 |
| `handoff/H02_COMPLETION_BUNDLE.md` | — | 이 문서 |

## 4. 새 세션 필드 · 필드 권위 매트릭스

| 필드 | 기본값 | SESSION_FIELDS | CANONICAL_DOMAINS(READY 지문) | COMMIT_OWNED(롤백) | REVERSIBLE(턴 이력) | writer |
|---|---|---|---|---|---|---|
| `world_version` | None | ✓ | ✓ | ✓ | ✓ | CommitCoordinator(PLAYER_TURN) · canonical_mutation(호출자 0) |
| `world_clock` · `actor_registry` · `spatial_state` · `spatial_knowledge` · `access_control_state` · `event_instances` · `world_schedule` · `information_state` · `cognition_state` · `relationship_state` | {} | ✓ | ✓ | ✓ | ✓ | `world_mutation.apply_projection`만(owner 원자 경계 안). H02 production 연산 0 |
| `definition_revision` | None | ✓ | ✗(별도 명시 가드) | ✗ | ✗ | **없음**(H06 바인딩). 생성자·이행 기본값만 |
| `narrative_seed` | session_id 도출 | ✓ | ✗ | ✗ | ✗ | 생성자·v3→v4 이행(빈 값일 때만). 이후 불변 |

- 목록 이중 관리 없음: 생성자·`SESSION_FIELDS`·`CANONICAL_DOMAINS`·`REVERSIBLE_FIELDS`가 `world_state`를 읽고, `COMMIT_OWNED_FIELDS`는 기존대로 `CANONICAL_DOMAINS`에서 파생.
- 재무·캐시 운영 필드는 새 집합 어디에도 없다(`test_partition_reversible_vs_identity_vs_operational`).

## 5. 호환 결정

### 5.1 세션 스키마 v3 → v4
- 새 게임 정본은 빈 기본값, `definition_revision` None, `narrative_seed = "seed_" + stable_digest("session.narrative_seed.v1", session_id, length=32)`.
- 멱등이며 이미 있는 시드는 바꾸지 않는다. v2 저장분(auto_gm_*)도 한 번에 v4까지 이행.
- 증거: `test_v3_payload_migrates_to_safe_defaults_idempotently`, `test_legacy_v3_data_json_restores_with_defaults_and_stable_seed`(실제 data.json을 v3으로 되돌려 재시작 → 재저장 → 재시작 시 같은 시드).

### 5.2 turn-history v3 (v2 호환)
- 새 레코드 = v3(새 11필드 포함). `_record_restorable`은 기존대로 v2 이상 허용.
- v2 레코드 복원 시 누락된 H02 필드를 **현재 값 유지가 아니라 레거시 기본값으로 합성**한다(v2 시점에는 그 도메인이 없었다). v1 비복원 정책은 그대로.
- 증거: `test_v2_history_record_rewinds_with_synthesized_legacy_defaults` — 실제 커밋 레코드를 v2 형식(새 필드 제거·schema 2·digest 재계산)으로 되돌린 뒤, 세션에 새 도메인 값을 남긴 채 되감기 → 전부 기본값, 기존 v2 내용(`world_timeline`)은 정상 복원, 시드·`definition_revision` 불변. `test_v1_history_record_policy_unchanged`.

### 5.3 CommitPlan / CommitJournal
- 저널 파일 형식(`JOURNAL_SCHEMA_VERSION`)은 바꾸지 않았다. PREPARED metadata인 CommitPlan만 v2로 올렸다.
- v2: `source_world_version`·`target_world_version`·`definition_revision`·`world_mutation_digest`를 `to_dict`(=지문)에 포함. 대상 version은 PREPARED 전에 `build_commit_plan`에서 한 번 결정적으로 만들고(stable_digest — 프로세스 RNG 무관) 복구는 재생성하지 않는다.
- v1: `from_journal_metadata`가 `plan_version` 1을 정상 구버전으로 읽고, `to_dict`는 v1 키만 내보내 **저장된 v1 지문을 그대로 재현**한다. v1 복구는 world_version을 건드리지 않는다(레거시 계보 — 이행된 세션이면 None).
- v2인데 world 키가 빠졌거나 대상 version 형식이 깨졌으면 손상(ValueError)이지 v1로 오인하지 않는다. 미지 버전(3)도 거부.
- 증거: `test_old_commit_plan_v1_prepared_recovers` — H02 코드로 만든 미완료 커밋의 PREPARED를 v1 형식(키 제거·지문·대상 표식 재계산)으로, data.json을 v3·H02 필드 없음·v1 표식으로 되돌린 뒤 재시작 → RESOLVED, COMMITTED 1회, world_version None, 시드 도출. `test_plan_v2_metadata_corruption_is_not_mistaken_for_v1`.

### 5.4 legacy `core.rewind`
- `TRACKED_PATHS`(14)는 **확장하지 않았다.** 전수 조사 결과 `core.rewind`의 production 소비자는 델타 기록(`record_delta`·`record_full_log`)·`archive_removed`·`_truncate_jsonl`·`available_range`·`capture_state`(델타 기준)뿐이며, 델타를 읽어 정본을 복원하는 경로가 없다. 되감기·재생성 권위는 turn_history(v3 스냅샷)다. 중복 거대 상태 기록을 피했다.

## 6. 롤백 · 이력 커버리지

| 상황 | world_version | 새 도메인 | 증거 |
|---|---|---|---|
| 정상 커밋(빈 묶음) | 대상으로 전진, data.json·이력 post에 기록 | 불변 | `test_empty_batch_player_turn_advances_world_version_and_freezes_plan` |
| 정상 커밋(typed 묶음, 테스트 handler) | 전진 | 투영 반영(같은 원자 경계) | `test_non_empty_typed_batch_applies_inside_commit_and_rewinds` |
| 영속 전 실패(strict 저장 실패) | 이전 값 복원 | 이전 값 복원 | `test_pre_persist_failure_rolls_back_world_version_and_domains` |
| 영속 후 크래시(SESSION_PERSISTED 미기록) → 재시작 | 동결 대상 유지 | 영속 값 유지 | `test_post_persist_recovery_keeps_frozen_target_version` |
| 영속 world_version이 대상과 다름(손상) | 복구 거부·차단 | — | `test_recovery_rejects_world_version_mismatch` |
| 되감기 | 대상 턴 post 값 | 대상 턴 post 값 | `test_rewind_restores_world_fields_but_not_identity`, 위 typed 묶음 테스트 |
| 정체성 필드 | 되감기·복원으로 불변 | — | `test_restore_reversible_never_touches_identity_or_operational` |

## 7. 테스트 결과

```
baseline(28cad87, 이 환경 재현)            672 passed
H02-A checkpoint(fd44d6c)                 682 passed (H02-A +10)
python -m compileall -q core cogs tests main.py prompts.py tools  → OK
pytest -q (FINAL)                          707 passed, 0 failed, 0 xfailed, 0 XPASS (1 warning: discord audioop — 기준과 동일)
  · 분류: 특성화 46 · 결함 27 · 정책 634
  · H02-A 대상 tests/policy/test_h02a_world_state.py       10 passed
  · H02-B 대상 tests/policy/test_h02b_world_mutation.py    20 passed
  · H02 경계  tests/characterize/test_h02_no_cutover.py    5 passed
python tools/scenario_check.py --all       → 종료 코드 0 (5종 ERROR 0, revision 무변경 — H01 compiler 무변경)
python tools/verify_docs.py                → 문서 수치 OK (21항목)
검증 루틴 ① OK · ② 재수출 OK / 필드 OK · ③ OK · ⑤ cogs 9 · 명령어 44 · views 5
```

## 8. direct-writer 스캔 · 신규 모듈 production 호출자 스캔

- **world_version / 새 도메인 직접 대입**(`test_world_version_direct_writers`, AST): 세션 대상 대입은 `commit_coordinator.py`·`canonical_mutation.py`의 `world_version`뿐(+`models.py` 생성자 기본값은 `self.` 대입이라 제외). 다른 10개 도메인의 production 직접 대입 0 — 오직 `world_mutation.apply_projection`(owner 원자 경계 안)만 쓴다.
- **definition_revision writer**: 세션 대상 대입 0(`test_definition_revision_field_exists_but_is_not_bound`).
- **canonical_mutation production 호출자 0**(`test_canonical_mutation_has_no_production_caller`, AST — 주석 제외).
- **world_mutation 사용자** = `turn_preparation`·`commit_coordinator`·`canonical_mutation`뿐.
- **production 연산 레지스트리**: 비어 있음, `PRODUCTION_REGISTRY.register` 호출 0.
- **H01 경계**: `definition_compiler`·`definition_snapshot` production 호출자 0 유지. `world_ids`는 H02 정본 모듈(`world_state`·`world_mutation`·`canonical_mutation`)만 재사용.
- **provider·재무·캐시**: 새 모듈 3개는 `google`·`discord`·`cost*`·`settlement`·`ink_transactions`·`accounts`·`cache*`·`prompt`·`resilience`를 import하지 않고 `call_with_retry`·`accrue`를 부르지 않는다. `git diff 28cad87`로 `prompts.py`·`scenarios/`·`cogs/`·`main.py`·`core/{cache,cache_lifecycle,prompt,cost,cost_ledger,settlement,ink_transactions,accounts,extraction,rewind,definition_compiler,definition_snapshot,world_ids}.py` 무변경 확인.
- strict 저장 호출 위치: 기존 owner + `canonical_mutation`(INIT/ADMIN 전용, 호출자 0). `test_tc26`(gm·game·turn_preparation 금지) 그대로 통과.

## 9. world_version 의미 · 복구 증거

- 형식 `{"version_id", "parent_version_id", "ordinal", "cause_kind", "cause_id"}`, `version_id = "wv_" + stable_digest("world_version.v1", session_id, parent_id, cause_kind, cause_id)`. 벽시계 없음.
- PLAYER_TURN cause_id = transaction_id(시도마다 고유) → 재생성은 같은 부모에서 다른 version(분기). 되감기 후 다음 턴은 되감긴 version을 부모로 한다.
- `test_batch_digest_and_version_ids_are_hashseed_independent` — PYTHONHASHSEED 0/777 별도 프로세스에서 묶음 digest·version_id·시드 동일.

## 10. 결정 기록 / 패킷과의 차이

| # | 내용 | 근거 |
|---|---|---|
| D1 | **제품 버전 7.0.0** — 패킷 A11(6.0.0 유지)과 다름 | 사용자가 같은 지시에서 "개발마다 버전을 내용·기능 기준으로 갱신"을 추가했고, 충돌을 질문해 **7.0.0으로 확정**받았다. CLAUDE.md 규칙상 `SCHEMA_VERSION` 증가 = MAJOR. H01(하위호환 도구, MINOR감)은 7.0.0에 흡수. CLAUDE.md 버전 체계를 "개발 패키지·내용 기준"으로 개정, DEVLOG 버전 기록에 반영 |
| D2 | 필드 레지스트리를 `world_state.py` 한 곳에 둠 | 패킷 §4.5 "이중 목록 금지"의 구현. 생성자는 검증 루틴 ②(정규식이 `self.X =`만 인식)를 위해 레지스트리 기본값을 필드별로 명시 대입 — 키 불일치 시 KeyError, 정합은 테스트로 고정 |
| D3 | 대상 world_version = stable digest(세션·부모·원인·tx) | 패킷 B2 선택지 중 결정적 생성. 동시에 PREPARED에도 동결(이중 안전) |
| D4 | CommitPlan만 v2, 저널 형식은 무변경 | 패킷 §8 "최소 backward-compatible". v1 지문 재현으로 구버전 레코드를 손상으로 오인하지 않음 |
| D5 | `TRACKED_PATHS` 미확장 | 패킷 §4.7·B5 — §5.4 조사 근거 |
| D6 | 투영 충돌은 READY 술어 실패로 표면화 → 기존 NOT_READY 종료 실패(FAILED_SYSTEM, 청구 0) 경로 | 새 실패 경로·cogs 변경 없이 "RETRY_PENDING 아님"을 만족. `prep.failure_code`와 `FailureCode.WORLD_PROJECTION_CONFLICT`, `prep.world_conflicts` 진단 기록. cogs/gm.py의 사용자 안내 문구는 기존 일반 실패 안내 그대로(별도 GM 개입 안내는 H05/H06에서 결정) |
| D7 | 경로 접두 겹침(상위 칸과 하위 칸 동시 쓰기)도 충돌로 분류 | 적용 순서 의존을 막기 위한 결정적 규칙. 같은 칸 단일값 동일 쓰기는 허용 |
| D8 | ADMIN_EDIT no-op = NOOP(새 version 없음), INITIALIZATION = 항상 genesis | DESIGN 11 §8·§9 |
| D9 | `canonical_mutation`은 활성 턴·커밋 복구 대기·미종결 변이가 있으면 거부 | 통합 설계 §53.4 · DESIGN 07 |
| D10 | H01 특성화 테스트 2건 의도적 갱신 | H02가 계획대로 세션 필드를 만들고 `world_ids`를 재사용하므로. 컴파일러·스냅샷 호출자 0 단언은 유지·강화 |

STOP 조건(패킷 §12·결정 게이트 C)은 하나도 발생하지 않았다.

## 11. 새로 확인한 사실

| # | 사실 | 처리 |
|---|---|---|
| F1 | `restore_reversible`은 레코드에 없는 필드를 건너뛰어 **현재 값을 남긴다** — 새 가역 필드를 추가하면서 v2 레코드를 그대로 쓰면 되감기 후 미래 상태가 새는 결함이 생긴다 | H02 필드에 한해 레거시 기본값 합성으로 막음(테스트 고정) |
| F2 | READY 술어 실패(NOT_READY)는 `cogs/gm.py`에서 종료 실패(FAILED_SYSTEM)로 처리되고, RETRY_PENDING은 추출 실패 전용이다 | 투영 충돌을 READY 술어로 표면화하는 근거(D6) |
| F3 | 검증 루틴 ②는 `models.py`의 `self.X =` 정규식만 본다 — `setattr` 초기화는 "누락"으로 오탐 | D2 |
| F4 | `turn_history` 재정합(`reconcile`)도 `CommitPlan.from_journal_metadata`를 쓴다 — v1/v2 공통 경로로 함께 호환됨 | 기존 테스트 통과로 확인 |
| F5 | 6.x 코드로 되돌리면 plan v2 PREPARED를 손상으로 본다(구 코드는 v1만 허용) | CLAUDE.md 미완 항목 'v7.0.0 반영'에 운영 주의로 기록 |

## 12. 알려진 한계

- production 묶음은 항상 비어 있다 — 실제 연산·quiescence 증명은 H03(시간·공간)부터. `ReadyProof`에 quiescent 플래그를 넣지 않았다(가짜 증명 금지).
- `canonical_mutation`은 저널 미종결 시 이후 적용을 막지만, 재시작 시 자동 `recover()` 호출 배선은 없다(호출자 0이므로 H06에서 세션 복구 순서와 함께 배선).
- 투영 충돌 시 플레이어 안내는 기존 일반 실패 문구다.

## 13. H03가 믿어도 되는 불변식

1. H02 세션 필드의 이름·기본값·분류는 `core.world_state`가 단일 출처다. 새 정본 도메인을 추가하려면 `GAMEPLAY_WORLD_FIELDS`에 넣는다 — 저장·READY 지문·롤백·턴 이력이 자동으로 따라온다(생성자에 명시 대입 한 줄 추가).
2. 정본 도메인 변경은 등록된 typed handler가 `world_mutation.write()`로 낸 쓰기만, 선언된 병합 의미로 합쳐져 owner 원자 경계 안의 `apply_projection`으로 반영된다. 임의 경로 setter는 없다. `world_version`은 연산이 쓰지 못한다.
3. PLAYER_TURN 정본 owner는 `CommitCoordinator`다. 플레이어 턴 연산은 `prep.world_mutation_batch`에 `capture_world_baseline` 이후·READY 이전에만 더한다. READY에서 동결되며 이후 추가는 `BatchFrozenError`.
4. 모든 커밋된 PLAYER_TURN은 world_version을 전진시킨다. 대상 version은 PREPARED 전에 동결되고 복구는 재생성하지 않는다.
5. 서로 다른 단일값 쓰기는 `WORLD_PROJECTION_CONFLICT`이고 재시도 대상이 아니다. 충돌 시 정본 불변·FAILED_SYSTEM(청구 0).
6. `definition_revision`은 아직 None이며 바인딩은 H06이다. 준비 객체는 기준선을 대조하므로 H06 바인딩 후 준비 중 정의가 바뀌면 READY가 막힌다.
7. `narrative_seed`는 불변·비가역이며 결정적 난수 입력으로 쓸 수 있다(`stable_digest(seed, ...)`).
8. 같은 입력의 묶음 digest·LocalRef 해석·version_id는 프로세스·해시시드와 무관하다.
9. turn-history는 v3, v2 레코드는 H02 필드를 레거시 기본값으로 복원한다. 새 가역 필드를 또 추가하면 같은 합성 규칙(기본값)을 따른다.
10. CommitPlan v1·v2 모두 복구 가능하다. 새 plan 필드를 추가하면 버전을 올리고 이전 버전 지문 재현을 유지한다.
11. `canonical_mutation`·`definition_compiler`·`definition_snapshot`의 production 호출자는 0이다(H06에서 배선).

## 14. 재현

```bash
git checkout <FINAL SHA>
pip install -r requirements.txt -r requirements-dev.txt
python -m compileall -q core cogs tests main.py prompts.py tools
python -m pytest -q                                                     # 707 passed
python -m pytest -q tests/policy/test_h02a_world_state.py tests/policy/test_h02b_world_mutation.py tests/characterize/test_h02_no_cutover.py   # 35 passed
python tools/scenario_check.py --all                                    # exit 0
python tools/verify_docs.py                                             # OK (21항목)
```
