# WP-00 NEW FINDINGS

Baseline: v5.33.0 (`482ffc0`)
Source: WP-00 실행 중 소스 검증 및 테스트 작성 과정

---

## F-1 · 베이스라인 드리프트 — 되돌림 완료

**성격**: 통제 (control), 결함 아님
**상태**: RESOLVED

WP-00 착수 시점의 HEAD가 **v5.34.0**이었다. `AUDIT_STATE.json`·
`CLAUDE_EXECUTION_CHAIN_WP00_WP02.md`가 기준으로 삼는 v5.33.0과 달랐다.

명세 작업 후속으로 즉시 수정 4차(5.31.0~5.34.0)가 적용돼 있었다.
그중 WP 범위와 충돌하는 것:

| 버전 | 내용 | 충돌 |
|---|---|---|
| 5.34.0 | 중복 퀘스트 적용 경로 제거 | `MASTER_ROADMAP` Track H — Phase 4 범위 |
| 5.31.0 | `call_with_retry` 전 호출부 적용 | TID-005 검증 표면 확대 |

**조치** — 사용자 지시(*"베이스라인은 되돌리고 진행하세요"*)에 따라
프로덕션 코드를 `a5cc828~1`로 복원했다. 커밋 `482ffc0`.
`git diff a5cc828~1 -- '*.py'` 결과 빈 출력으로 확인.

감사 기록은 `AI_IMPLEMENTATION_GUARDRAILS.md` §5에 따라 보존했다
(`specs/04_quest.md`의 RETRACTED 조치 이력, `SPEC_RULES.md`의 별칭 임포트 목록).

**주의** — 5.31.0~5.33.0은 되돌리지 않았다. 이들은 v5.33.0 스냅샷 **자체**를
구성한다. 문서가 참조하는 v5.33.0이 그 이전 상태를 뜻한다면 재확인이 필요하다.

---

## F-2 · `cached_worldview_sections`가 생성자에 없다

**성격**: 신규 결함 후보
**심각도 제안**: P2
**발견 경로**: `test_session_fields_registry_matches_model`

`core.SESSION_FIELDS`에 등록돼 있으나 `TRPGSession.__init__`이 설정하지 않는다.
`core/cache.py:151`(`update_session_cache_state`)이 나중에 설정한다.

```python
core.SESSION_FIELDS["cached_worldview_sections"]   # [] 로 등록됨
hasattr(fresh_session, "cached_worldview_sections")  # False
```

**현재 영향**: `save_session_data`가 `getattr` 기본값으로 넘어가므로 저장은
통과한다. 캐시를 한 번이라도 올리면 속성이 생긴다.

**잠재 영향**: 캐시 업로드 전에 `session.cached_worldview_sections`를 직접
읽는 코드가 추가되면 `AttributeError`가 난다.

`SESSION_FIELDS`가 *"저장·복구의 단일 진실 공급원"*이라는 주석의 계약과
어긋난다. 다른 75개 필드는 생성자에 있다.

특성화로 고정했다 — `KNOWN_MISSING_AT_CONSTRUCTION` 집합이 바뀌면 테스트가
실패해 목록 갱신을 강제한다.

---

## F-3 · 캐시 보관비 함수 두 개의 단위가 다르다

**성격**: 신규 결함 후보
**심각도 제안**: P1 (회계 오차 가능)
**발견 경로**: `test_d006f_storage_cost_helpers_disagree_on_units`
**관련**: AUD-034 / AUD-035, WP-04

```python
calculate_storage_cost(model_id, cache_storage_tokens, duration_seconds)  # 초
calculate_storage_cost_usd(model_id, tokens, hours)                       # 시간
```

이름이 거의 같은데 세 번째 인자의 단위가 **3600배** 다르다.

```
calculate_storage_cost(m, 26268, 3600.0)  → 118.21원   (1시간)
calculate_storage_cost(m, 26268, 1.0)     →   0.00원   (1초)
calculate_storage_cost_usd(m, 26268, 1.0) →   0.0788$  (1시간)
```

호출부가 헷갈리면 과소 계상된다. 현재 호출부(`core/io.py:441`)는 초를
올바르게 넘기고 있으나, WP-02가 새 호출부를 추가할 때 위험하다.

`calculate_storage_cost_usd`는 v5.29.1에 추가됐고 **현재 호출부가 없다**.

---

## F-4 · 필수 문서 2건이 저장소에 없다

**성격**: 통제
**상태**: 사용자 제공으로 해소

착수 시 다음이 저장소에 없었다.

```
MASTER_ROADMAP.md · AUDIT_STATE.json · AI_IMPLEMENTATION_GUARDRAILS.md
AI_WORK_PACKAGE_CHECKLIST.md · WP00_EXECUTABLE_TEST_PLAN.md
WP00_TEST_HARNESS_SPEC.md · CLAUDE_EXECUTION_CHAIN_WP00_WP02.md
```

전부 업로드로 제공받아 진행했다.

**권고** — 이 문서들을 저장소에 커밋하면 다음 세션이 `git pull`만으로
동일 기준을 확보한다. 현재는 세션마다 업로드가 필요하다.

---

## 기존 발견의 재확인

WP-00 작성 중 다음 기존 발견이 v5.33.0에서 **여전히 사실**임을 확인했다.

| AUD | 확인 방법 |
|---|---|
| AUD-011 | `_dispatch_proceed`의 `text[:500]` — `test_d001c` |
| AUD-012/019 | `create_task(_run_extraction)` 후 `_start_round` 무대기 — `test_d002` |
| AUD-020 | `session._rewind_snapshot = state_after` 이월 — `test_d004` |
| AUD-024 | `apply_choice`가 `_call_gm_logic` 내부 — `test_d003` |
| AUD-029 | `total_cost in TRACKED_PATHS` — `test_d005` |
| AUD-034/035 | 세 경로 값 불일치 — `test_d006d` |

`_execute_proceed` 3경로 공유(gm·game·session)도 소스로 재확인했다.

---

## WP-01에 대한 관찰

`GMRollView` 본문에 `transaction_id`가 **없음**을 확인했다
(`test_c003_roll_view_holds_continuation_reference`). WP-01이 추가하면
그 단언이 실패하므로, 해당 테스트는 WP-01이 갱신해야 한다.

`_process_actions`가 자동 수렴 경계라는 서술을 소스로 확인했다.
ASK/NARRATE는 `break`로 루프를 빠져나가고 플레이어 입력을 기다린다 —
`_finish_proceed_and_continue`는 **한도 초과 시 강제 PROCEED 경로에만**
나타난다. 이 비대칭이 WP-01 생명주기 규칙의 근거다.
