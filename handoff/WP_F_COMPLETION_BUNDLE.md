# WP-F COMPLETION BUNDLE — Derived Systems Stabilization

**Status:** WP-F 구현 완료 후보 + FINAL GATE PATCH(Cache Finance Policy Alignment) + RE-GATE PATCH 2(Interpretation Billing Durability) + **RE-GATE PATCH 3(Strict Interpretation Cost Fact)** 적용 — 독립 GPT 재게이트 대기 (VERIFIED 아님)
**Date:** 2026-09-28
**WP-G:** NOT STARTED

---

## 0-C. RE-GATE PATCH 3 — Strict Interpretation Cost Fact Only (이 절이 §0-B·§0보다 우선한다)

| 항목 | 값 |
|---|---|
| patch start SHA | `ee047f87f1369455f7e1f653d3b32d47b281ad87` (재게이트: PATCH REQUIRED — tolerant CostEvent 뒤 청구 가능 1건) |
| patch code commit | `65bcf45` `wp-f re-gate patch 3: strict interpretation cost fact before billing` |
| final SHA | 이 번들·스캔을 담은 최종 커밋 — push 후 채팅에 exact 값·local==remote·literal `git status --short`·아카이브 SHA-256 보고 |
| final regression | **593 passed, 0 failed, 0 xfailed, 0 XPASS** (587 + 신규 6) |

### 0-C.1 결함과 수정

**결함:** `interpret_cache_time`이 tolerant/shadow API인 `ProviderOperation.record()`의 반환값(False = 원장 없음 또는 쓰기 실패)을 확인하지 않고 `record_interpretation`과 `settle`로 넘어갔다. 그래서 CostEvent가 없어도 플레이어 청구가 가능했다.

**수정 (범위: `OP_CACHE_TIME_INTERPRET` 청구 경로만):**
1. `core/cost_ledger.py`
   - `ProviderOperation.record_fact_strict(...)`를 추가했다.
   - `record()`와 **같은** operation_id·provider_attempt·idempotency_key(`{operation_id}:attempt:{n}`)·metadata·토큰·pricing basis로 CostEvent를 만든다.
   - 기록은 `CostLedger.record_cost_event_strict`(write → flush → fsync)로 한다. 같은 요청을 다시 보내면 기존 canonical event_id를 돌려준다(`created=False`).
   - 원장이 없거나 영속·상충에 실패하면 예외를 올린다.
   - `record()`와 그 밖의 호출자는 **바꾸지 않았다**. 이름은 SettlementStore의 `record_strict(` 호출자 스캔과 겹치지 않도록 정했다.
2. `cogs/gm.py::interpret_cache_time`
   - strict CostEvent가 성공해야만 `record_interpretation(..., cost_event_id=<canonical>)`과 `settle`로 진행한다.
   - strict가 실패하면:
     - 청구 후보(INTERPRETED)를 만들지 않는다.
     - 청구는 0이다.
     - UI 청구 표시가 없다(`interpret_charge.ink = 0`).
     - 결과를 확정하지 않고 재질문(retry) 경로로 끝낸다.
     - error 로그를 남긴다.
   - 실제로 발생한 provider 비용은 시스템 영속 실패로 인한 운영 손실이다.
3. `core/interpretation_billing.py`
   - INTERPRETED에 `cost_event_id`를 기록해 감사 연결을 남긴다.
   - `reconcile_from_ledger`는 `list_cost_events_strict`로 읽는다. 손상되거나 identity가 중복된 원장이면 조용히 건너뛰지 않고 예외를 낸다. 이때 채택은 없지만 `settle`은 막히지 않는다. 이미 저널된 후보는 각자 strict 사실을 근거로 유효하다.
4. 허용 목록: `tests/policy/test_accounts_strict.py::test_29`에 `core/interpretation_billing.py`를 WP-F 인가 strict 소비 owner로 추가했다(의도된 변경).

### 0-C.2 변경 파일 (patch start → `65bcf45`; 최종 커밋은 번들·스캔 갱신을 더함)
```
 cogs/gm.py                                |  19 ++++-
 core/cost_ledger.py                       |  57 +++++++++++++
 core/interpretation_billing.py            |  20 +++--
 tests/policy/test_accounts_strict.py      |   5 +-
 tests/policy/test_cache_finance_policy.py | 129 ++++++++++++++++++++++++++++++
 5 files changed, 221 insertions(+), 9 deletions(-)
```
보존: P1~P14 의미·캐시 창 분리·취소/시간 만료·청구 의도 복구·재청구 방지·잔액 검사·면제·운영자 환급·운영자 부담·가격·finalizer·Message/UI·압축·WP-E·턴 Settlement/CHARGE — 모두 변경 없음(테스트 PASS).

### 0-C.3 테스트 — `tests/policy/test_cache_finance_policy.py` 25건 전부 PASS
```
  tests/policy/test_cache_finance_policy.py::test_p1_interpretation_charged_at_interpretation_then_open
  tests/policy/test_cache_finance_policy.py::test_p1_ui_note_matches_committed_charge
  tests/policy/test_cache_finance_policy.py::test_p2_interpretation_non_refundable_on_early_close
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[end]
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[delete]
  tests/policy/test_cache_finance_policy.py::test_p5_repeated_concurrent_and_restart_operator_close_exactly_once
  tests/policy/test_cache_finance_policy.py::test_p6_actual_exceeds_prepayment_is_operator_borne
  tests/policy/test_cache_finance_policy.py::test_p7_estimate_and_actual_share_canonical_pricing
  tests/policy/test_cache_finance_policy.py::test_p8_policy_source_scan
  tests/policy/test_cache_finance_policy.py::test_p9_interpretation_then_cancel_keeps_single_charge
  tests/policy/test_cache_finance_policy.py::test_p10_interpretation_then_timeout_keeps_single_charge
  tests/policy/test_cache_finance_policy.py::test_p11a_crash_after_cost_event_before_billing_record
  tests/policy/test_cache_finance_policy.py::test_p11b_crash_around_account_effect_resumes_exactly_once[after_intent]
  tests/policy/test_cache_finance_policy.py::test_p11b_crash_around_account_effect_resumes_exactly_once[after_account_effect]
  tests/policy/test_cache_finance_policy.py::test_p12_already_charged_then_open_and_early_close
  tests/policy/test_cache_finance_policy.py::test_p13_affordability_after_interpretation
  tests/policy/test_cache_finance_policy.py::test_p13b_displayed_need_equals_actual_prepayment
  tests/policy/test_cache_finance_policy.py::test_p14_below_threshold_preserved
  tests/policy/test_cache_finance_policy.py::test_p1c_interpretation_charge_survives_cache_create_failure
  tests/policy/test_cache_finance_policy.py::test_p15_strict_costevent_failure_means_no_charge
  tests/policy/test_cache_finance_policy.py::test_p15b_no_cost_ledger_means_no_charge
  tests/policy/test_cache_finance_policy.py::test_p16_strict_fact_then_crash_before_billing_record
  tests/policy/test_cache_finance_policy.py::test_p17_strict_replay_reuses_canonical_identity
  tests/policy/test_cache_finance_policy.py::test_p17b_corrupted_ledger_reconcile_fails_closed
  tests/policy/test_cache_finance_policy.py::test_p8b_interpretation_path_uses_strict_fact_before_billing
```
| 요구 | 테스트 |
|---|---|
| P15 strict 쓰기 실패 | `test_p15_…` — provider 응답 성공, strict append에 실패를 주입 → INTERPRETATION_CHARGE 0, 잔액 변화 0, INTERPRETED 0, UI 청구 표시 없음, 재질문, PREPAYMENT 0, 부분 CostEvent 0, 재시작 후에도 phantom 청구 없음. 보강: `test_p15b_…` — CostLedger가 없으면 청구 0 |
| P16 strict 사실 후 크래시 | `test_p16_…` — strict CostEvent가 durable 1건인 상태로 INTERPRETED 전에 크래시 → 재시작 시 strict 원장에서 채택, INTERPRETATION_CHARGE 정확히 1, double debit 없음, `cost_event_id` = canonical |
| P17 strict replay | `test_p17_…` — 같은 idempotency replay → `created=False`로 같은 event_id, durable CostEvent 1, INTERPRETED 1, 청구 1. 보강: `test_p17b_…` — 손상된 원장이면 복구 채택이 fail-closed |
| 스캔 | `test_p8b_…` — 해석 경로는 `record_fact_strict`만 쓰고 순서는 strict 사실 → 청구 후보 → 정산이다. 복구 읽기는 strict이며 tolerant `record()` 의미는 유지된다 |

### 0-C.4 기타 검증
- WP-F targeted(`test_cache_finance_policy` · `test_cache_lifecycle` · `test_compression_safety` · `test_message_lifecycle_wpf` · `test_cache_accounting`): **95 passed**. WP-E 이력 테스트 포함 스위트도 PASS.
- compileall OK, CLAUDE.md ①② OK, ④ verify_docs OK, ⑤ cogs 9 · 명령어 46 · views 5
- 변경 후 스캔: `handoff/WP_F_POST_CHANGE_SCAN.txt` 말미 "RE-GATE PATCH 3 — STRICT INTERPRETATION COST FACT SCAN". strict 사실 writer 1(`cogs/gm.py`), 해석 경로의 tolerant `record` 0, 복구 읽기는 strict만.
- source archive: 최종 커밋에서 `git archive`로 만들었고 `media/`를 제외했다. 채팅에 첨부하고 SHA-256을 보고한다.


---

## 0-B. RE-GATE PATCH 2 — Interpretation Billing Durability Only (이 절이 §0보다 우선한다)

| 항목 | 값 |
|---|---|
| patch start SHA | `2da800601ce5cd7cfc9b0c9db48df6d5a239cb52` (재게이트: PATCH REQUIRED — 해석 청구 경계·크래시 내구성 1건) |
| patch code commit | `wp-f re-gate patch 2: interpretation billing durability` |
| final SHA | 이 번들·스캔을 담은 최종 커밋 — push 후 채팅에 exact 값·local==remote·literal `git status --short`·아카이브 SHA-256 보고 |
| final regression | **587 passed, 0 failed, 0 xfailed, 0 XPASS** (579 − 대체된 해석 테스트 4 + 신규/재작성 12) |

### 0-B.1 결함과 수정

**이전 흐름:**
1. 해석 provider 호출이 끝나면 비용은 volatile 값인 `session.interpret_cost_krw`에만 쌓였다.
2. 청구는 OpenConfirmView에서 CONFIRM을 누른 뒤 `open_window` 안에서만 일어났다.

그래서 취소·시간 만료·크래시가 나면 청구가 사라졌다.

**수정 — 새 단일 owner `core/interpretation_billing.py`(캐시 창과 분리):**

1. `interpret_cache_time`
   - 해석 CostEvent 메타데이터에 `interpretation_billing=2`와 `interp_id`(= provider operation_id)를 붙인다.
   - CostEvent를 기록한 직후 `record_interpretation`으로 **INTERPRETED**를 durable 기록한다(fsync 저널 `sessions/{id}/interpretation_billing.jsonl`).
   - 곧바로 `settle`을 호출한다.
2. `settle`
   - `reconcile_from_ledger`: 표식된 CostEvent 중 저널에 없는 것을 채택한다(크래시 경계 A 복구). WP-F 이전의 표식 없는 해석 사실은 채택하지 않는다.
   - 중단된 CHARGE_INTENT를 같은 charge_id로 재실행한다.
   - 미청구 누적이 2잉크 이상이면 **CHARGE_INTENT**(charge_id = interp_id 집합의 결정적 해시)를 기록한다.
   - 이어서 `LifecycleInkTransaction(kind=INTERPRETATION_CHARGE, reference_kind=CACHE_TIME_INTERPRETATION, reference_id=charge_id)`를 실행한다. 기존 exactly-once 계정 마커·원장 프리미티브를 재사용한다.
   - 마지막으로 **CHARGED**를 기록한다.
3. 청구는 **확인 화면 이전**에 확정된다. 그 결과 열기·취소·시간 만료·재시작이 모두 같은 결과를 낸다.
4. 확인 화면 문구(`_interp_note`)는 방금 확정된 거래 금액을 그대로 표시한다(UI = 거래).
5. 기존 면제 규칙은 그대로다. 2잉크 미만은 누적을 이어 가고(이제 durable), 세션을 열 때 남은 미만분을 **WAIVED**로 면제한다(`upload_cache` 직전, 기존 "열기 시점 초기화"와 같은 의미).
6. `session.interpret_cost_krw`는 저널에서 파생된 호환 미러일 뿐 청구 근거가 아니다.
7. 재시작 시 `restore_sessions_from_disk`가 `interpretation_billing.settle`로 재개한다.
8. `core/cache_lifecycle.py`에서 해석 로직을 모두 제거했다: WINDOW_OPENED의 `interpret` 맵, `WINDOW_INTERPRET_CHARGED`, `_execute_interpretation_locked`. 캐시 창 ID와 해석 청구 ID는 무관하다. 따라서 새 창이 과거 해석을 다시 청구하지 않고, 캐시 REFUND는 해석 거래를 건드리지 않는다.

**잔액 검사 정합(요구 4):**
- `OpenConfirmView`는 해석 청구가 이미 반영된 현재 잔액으로 **캐시 선불만** 검사한다.
- 표시한 필요 잉크의 견적 토큰(`quoted_tokens`)을 `upload_cache`/`on_open_time_done`을 거쳐 `open_window`로 넘긴다. 따라서 **표시 필요액 = 실제 PREPAYMENT**다. 공식은 기존 canonical 공식 그대로다.
- `open_window`는 창을 열기 전에 모든 payer의 잔액이 캐시 선불 이상인지 검사한다. 부족하면 `CacheOpenInsufficientFunds`를 내며, 이때 창·선불·provider 생성은 모두 없다. 부족분이 overdraft나 운영자 보조로 우회되지 않는다.
- 잔액 검사와 선불 적용 사이의 좁은 경쟁 구간(다른 경로의 동시 차감)에서는 기존 floor 규약이 적용된다. 이 구간은 durable 재개 가능성을 위해 유지했다.

**문서 정합(요구 7):** `open_window` docstring은 이제 이렇게 말한다. 생성이 실패하면 캐시 선불과 생성 사실이 없고, 시간 해석 청구는 별개 owner가 해석 직후 이미 확정하므로 생성 실패·취소와 무관하게 유지된다.

### 0-B.2 변경 파일 (patch start → patch code commit; 최종 커밋은 번들·스캔 갱신을 더함)
```
 CLAUDE.md                                 |   2 +-
 cogs/gm.py                                |  58 +++++--
 cogs/session.py                           |  27 ++-
 core/__init__.py                          |   1 +
 core/cache.py                             |   7 +
 core/cache_lifecycle.py                   |  72 ++++----
 core/interpretation_billing.py            | 248 +++++++++++++++++++++++++++
 core/session_flow.py                      |   5 +-
 tests/defects/test_cache_accounting.py    |   4 +
 tests/policy/test_cache_finance_policy.py | 269 ++++++++++++++++++++++--------
 10 files changed, 555 insertions(+), 138 deletions(-)
```
`CLAUDE.md`: verify_docs --fix(core 서브모듈 56 → 57). 보존: 캐시 가격 공식·운영자 환급·운영자 부담 부족분·ADDITIONAL_CHARGE 0·finalizer·PREPAYMENT/REFUND 의미·Message/UI·압축·WP-E·CommitCoordinator·턴 CHARGE·prompts/scenarios — 모두 무변경.

### 0-B.3 테스트 — `tests/policy/test_cache_finance_policy.py` 19건 전부 PASS
```
  tests/policy/test_cache_finance_policy.py::test_p1_interpretation_charged_at_interpretation_then_open
  tests/policy/test_cache_finance_policy.py::test_p1_ui_note_matches_committed_charge
  tests/policy/test_cache_finance_policy.py::test_p2_interpretation_non_refundable_on_early_close
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[end]
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[delete]
  tests/policy/test_cache_finance_policy.py::test_p5_repeated_concurrent_and_restart_operator_close_exactly_once
  tests/policy/test_cache_finance_policy.py::test_p6_actual_exceeds_prepayment_is_operator_borne
  tests/policy/test_cache_finance_policy.py::test_p7_estimate_and_actual_share_canonical_pricing
  tests/policy/test_cache_finance_policy.py::test_p8_policy_source_scan
  tests/policy/test_cache_finance_policy.py::test_p9_interpretation_then_cancel_keeps_single_charge
  tests/policy/test_cache_finance_policy.py::test_p10_interpretation_then_timeout_keeps_single_charge
  tests/policy/test_cache_finance_policy.py::test_p11a_crash_after_cost_event_before_billing_record
  tests/policy/test_cache_finance_policy.py::test_p11b_crash_around_account_effect_resumes_exactly_once[after_intent]
  tests/policy/test_cache_finance_policy.py::test_p11b_crash_around_account_effect_resumes_exactly_once[after_account_effect]
  tests/policy/test_cache_finance_policy.py::test_p12_already_charged_then_open_and_early_close
  tests/policy/test_cache_finance_policy.py::test_p13_affordability_after_interpretation
  tests/policy/test_cache_finance_policy.py::test_p13b_displayed_need_equals_actual_prepayment
  tests/policy/test_cache_finance_policy.py::test_p14_below_threshold_preserved
  tests/policy/test_cache_finance_policy.py::test_p1c_interpretation_charge_survives_cache_create_failure
```
| 요구 | 테스트 |
|---|---|
| P9 해석 후 취소 | `test_p9_…` — INTERPRETATION_CHARGE 1, debit 1, PREPAYMENT 0, provider 생성 0, REFUND 0 |
| P10 해석 후 시간 만료 | `test_p10_…` — P9와 같음, 재정산 후에도 불변 |
| P11 크래시 경계 | `test_p11a_…`(CostEvent 후, 청구 기록 전 → CostLedger 재구성), `test_p11b_…[after_intent]`, `[after_account_effect]` — 재시작 후 정확히 1건, double debit 없음, CHARGE_INTENT 1 |
| P12 청구 완료 후 열기 | `test_p12_…` — 재청구 없음, PREPAYMENT만, 조기 종료 시 캐시분만 REFUND, 다음 새 창에서도 재청구 없음 |
| P13 해석 후 잔액 부족 | `test_p13_…` — 해석 청구 유지, 확인 화면 거부(필요액 표시), PREPAYMENT 0·생성 0·잔액 불변, 서비스 계층도 `CacheOpenInsufficientFunds`로 거부, 창 저널 없음. `test_p13b_…` — 표시 필요액 = 실제 PREPAYMENT |
| P14 2잉크 미만 면제 | `test_p14_…` — 미만 누적 → 다음 해석에서 합산 청구 → 열기 시 남은 미만분 WAIVED |
| P1/P2(재작성) | 실제 `interpret_cache_time` → 즉시 청구 → `OpenConfirmView`(선불만). UI 문구 = 거래. 해석은 환불 안 됨 |
| P3–P8 | 유지·통과(P8 스캔: 해석 청구 writer = `core/interpretation_billing.py` 단 한 곳, 캐시 창 모듈은 해석을 모름) |

대체된 이전 테스트: 해석을 `open_window`에서 청구하던 전제의 `test_p1_interpretation_actually_charged_on_reopen_path`, `test_p1b_below_threshold…`, `test_p1c…create_fails`, `test_p1d…resumes`는 새 경계 기준의 P1·P1c·P11·P14로 대체했다.

`tests/defects/test_cache_accounting.py::test_d006e`에는 선불 가능한 잔액을 준비하도록 픽스처 한 줄을 추가했다. 새 잔액 검사가 잔액 0인 payer를 정상적으로 거부하기 때문이다. 단언은 바뀌지 않았다.

### 0-B.4 기타 검증
- WP-F targeted(`test_cache_finance_policy` + `test_cache_lifecycle` + `test_compression_safety` + `test_message_lifecycle_wpf` + `test_cache_accounting`): **89 passed**
- compileall OK, CLAUDE.md ①② OK, ④ verify_docs OK(--fix 반영), ⑤ cogs 9 · 명령어 46 · views 5
- 변경 후 스캔: `handoff/WP_F_POST_CHANGE_SCAN.txt` 말미 "RE-GATE PATCH 2 — INTERPRETATION BILLING DURABILITY SCAN"
- source archive: 최종 커밋에서 `git archive`로 만들었고 `media/`를 제외했다. 채팅에 첨부하고 SHA-256을 보고한다.


---

## 0. FINAL GATE PATCH — Cache Finance Policy Alignment (이 절이 이전 서술보다 우선한다)

| 항목 | 값 |
|---|---|
| patch start SHA | `dacfa4b0d227173496a7b8a8aa93e6d7ddf12f7e` (GPT gate: PATCH REQUIRED) |
| patch code commit | 이 번들 직전 커밋(`wp-f gate patch: cache finance policy alignment`) |
| final SHA | 번들·스캔을 담은 최종 커밋 — push 후 채팅에 exact 값·local==remote·literal `git status --short`·아카이브 SHA-256 보고 |
| final regression | **579 passed, 0 failed, 0 xfailed, 0 XPASS** (571 − 정책으로 대체된 3 + 신규 P1~P8 11) |

### 0.1 확정 정책 → 구현

| 정책 | 구현 |
|---|---|
| **POLICY-CACHE-01** 해석 비용 = 별도 실제 청구·환불 없음·선불/환급과 분리 | **(RE-GATE PATCH 2로 대체: 청구 owner는 `core/interpretation_billing`이고 해석 직후 확정된다 — §0-B)** 새 kind `INTERPRETATION_CHARGE`(DEBIT, `LifecycleInkTransaction`, reference_kind `CACHE_TIME_INTERPRETATION`, 결정적 ID `ink-interpretation_charge:{window_id}:user:{uid}`). `open_window`가 WINDOW_OPENED(의도: `interpret` 맵) 기록 직후 **캐시 생성 전에** 청구 → `WINDOW_INTERPRET_CHARGED` → 누적값 0. 생성 실패여도 유지(이미 소비된 서비스). 실패 시 `_resume_locked`가 모든 창(중단·정산된 창 포함)에 대해 정확히 한 번 완료. `PREPAYMENT`는 캐시분만(`prepay = {uid: cache_ink}`), 환급 기준도 PREPAYMENT 마커 nominal만 → 해석은 구조적으로 환급 공식 밖. **F-NEW-1 수정:** `OpenConfirmView`가 업로드 전에 `interpret_cost_krw`를 0으로 만들던 코드를 제거하고, 안내 문구를 "세션을 열 때 별도로 청구되며 환불되지 않습니다"로 바꾸었으며, 열림 메시지는 실제 거래가 적용된 경우에만 "시간 해석 N잉크 청구"를 표시한다(UI = ledger = account). |
| **POLICY-CACHE-02** 운영자 조기 종료도 payer 환급 | `!세션종료`·`!캐시 삭제`(및 호환 `process_cache_deletion`)가 `WINDOW_SETTLE_REFUND` 처분으로 같은 창 정산 권위를 소비. `WINDOW_NO_PLAYER_EFFECT` 처분 **삭제**(참조 0). 종료 actor는 `reason`(OPERATOR_END/OPERATOR_DELETE)과 정산 의도 레코드에만 남고, 환급 자격은 사라지지 않는다. 실제 PREPAYMENT 마커가 없는 사용자(늦은 참가자)에게는 REFUND가 없다. |
| **POLICY-CACHE-03** 실제 > 선불 → 추가 청구 없음·운영자 부담 기록 | `WINDOW_SETTLE_INTENT.operator_borne_shortfall`에 기록. `KIND_ADDITIONAL_CHARGE`는 정의만 있으며 **production callers: 0**(P8 스캔 테스트로 고정). |

### 0.2 canonical pricing (요구 C)

`core/cost.py`:
- `cache_window_responsibility_usd(model, create_tokens, storage=[(tokens, seconds)…])`
- `cache_window_estimate_usd(model, tokens, planned_seconds)` — 위 함수를 계획 TTL로 호출한다.
- `cache_usd_to_ink(usd)` — 유일한 잉크 반올림 경계이며, 합계 USD → ×EXCHANGE_RATE → `cost_to_ink`를 **한 번** 적용한다.

같은 공식을 쓰는 곳:
- **선불 예상:** `open_window`
- **UI 예상:** `estimate_session_open` — 이제 같은 헬퍼와 같은 반올림을 쓴다.
- **종료 책임:** `window_responsibility`
- **provider 사실:** `cache_create_cost_usd`, `cache_storage_cost_usd`(초 단위, 반올림 없음)

예상과 실제의 차이는 저장 시간(계획 TTL과 실제 경과 초)과 토큰 실측값뿐이다. `cache_lifecycle.py`에는 `calculate_upload_cost`(시간 단위 레거시)가 0회 등장한다.

**실제 책임의 정의:**
- 창을 연 생애주기(OPEN 또는 레거시)의 생성 비용 1회
- 창 안의 각 생애주기가 실제로 존재한 초(종료 의도 시각, provider TTL 상한)의 저장 비용

재발급·복구 생성 비용은 시스템 부담(현행)이다. 원격 삭제 실패로 provider가 더 보관한 시간은 운영 사실(CostEvent ESTIMATE)로만 남고 플레이어 책임에는 넣지 않는다.

**payer 배분:** 현행 선불 규약상 각 payer가 창 전체 캐시 책임만큼 선불하므로, 배분 책임은 창 책임액이다. 공식: `refund_uid = max(PREPAYMENT_uid − used_ink, 0)`, `shortfall_uid = max(used_ink − PREPAYMENT_uid, 0)`.

**반올림 경계(P7 명시):** 잉크 올림은 합계에 한 번만 적용한다. 구성요소별 올림의 합보다 작거나 같다. 계획 TTL과 같은 경과·같은 토큰이면 예상과 실제가 USD까지 같고 잉크도 같다.

estimate는 여전히 estimate다. CostEvent로 기록하지 않고, 실제가 예상을 넘어도 과거 예상(WINDOW_OPENED)을 수정하지 않는다.

### 0.3 변경 파일 (patch start → patch code commit; 최종 커밋은 여기에 `handoff/WP_F_COMPLETION_BUNDLE.md`·`handoff/WP_F_POST_CHANGE_SCAN.txt` 갱신을 더한다)
```
 cogs/gm.py                                |   6 +-
 cogs/session.py                           |   8 +-
 cogs/system.py                            |  18 +-
 core/cache_lifecycle.py                   | 156 +++++++++----
 core/cost.py                              |  30 +++
 core/estimate.py                          |  18 +-
 core/ink_transactions.py                  |   5 +-
 tests/defects/test_cache_accounting.py    |   2 +-
 tests/policy/test_cache_finance_policy.py | 362 ++++++++++++++++++++++++++++++
 tests/policy/test_cache_lifecycle.py      |  58 +----
 10 files changed, 543 insertions(+), 120 deletions(-)
```
보존 확인: Message/UI lifecycle, 압축 guard와 락 순서, WP-E SELECT/매핑/출처, CommitCoordinator, turn Settlement/CHARGE(`execute_settlement_charge`·`apply_ink_charge_strict` 무변경), CostLedger, prompts/scenarios(diff 0) — 모두 변경 없음.

### 0.4 정책 테스트 P1–P8 — 11건 전부 PASS (`tests/policy/test_cache_finance_policy.py`)
```
  tests/policy/test_cache_finance_policy.py::test_p1_interpretation_actually_charged_on_reopen_path
  tests/policy/test_cache_finance_policy.py::test_p1b_below_threshold_is_waived_and_not_charged
  tests/policy/test_cache_finance_policy.py::test_p1c_interpretation_charged_even_if_cache_create_fails
  tests/policy/test_cache_finance_policy.py::test_p1d_interpretation_charge_failure_resumes_exactly_once
  tests/policy/test_cache_finance_policy.py::test_p2_interpretation_non_refundable_on_early_close
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[end]
  tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only[delete]
  tests/policy/test_cache_finance_policy.py::test_p5_repeated_concurrent_and_restart_operator_close_exactly_once
  tests/policy/test_cache_finance_policy.py::test_p6_actual_exceeds_prepayment_is_operator_borne
  tests/policy/test_cache_finance_policy.py::test_p7_estimate_and_actual_share_canonical_pricing
  tests/policy/test_cache_finance_policy.py::test_p8_policy_source_scan
```
| 요구 | 테스트 |
|---|---|
| P1 해석이 실제로 청구됨 | `test_p1_…_on_reopen_path` — 실제 `GMCog.interpret_cache_time`(provider 사실) → 실제 `OpenConfirmView.confirm` → `SessionCog.upload_cache` → `open_window`. 거래 1건, 잔액 1회, UI 문구 = 거래. 보강: 임계 미만 면제(p1b), 생성 실패에도 청구(p1c), 청구 실패 → 재시작 재개 정확히 1회(p1d) |
| P2 해석은 환불 안 됨 | `test_p2_…` — 캐시 미사용분만 REFUND, 해석 거래 유지, 정산 의도의 `paid`는 캐시 선불만 |
| P3 / P4 운영자 종료·삭제 환급 | `test_p3_p4_…[end]` / `[delete]` — 실제 `SystemCog.end_session` / `manage_cache("삭제")` 경로. finalizer 1회, 보관 CostEvent 1회, payer REFUND 1회, 늦은 참가자 0, 해석 환급 0 |
| P5 반복·동시·재시작 | `test_p5_…` — 명령 2종과 직접 호출을 동시 실행한 뒤 재호출·restore까지. 보관 1, REFUND 1, 잔액 불변, 원격 삭제 1 |
| P6 실제 > 선불 | `test_p6_…` — provider 실측 토큰이 3배. REFUND·ADDITIONAL_CHARGE 없음, 잔액 추가 감소 없음, shortfall > 0, 보관 CostEvent는 실측 토큰으로 완전히 기록 |
| P7 가격 parity | `test_p7_…` — UI 예상 = 선불 = `cache_usd_to_ink(estimate)`. 경과 = 계획이면 used_usd = est_usd, used_ink = 선불. 반올림 경계 단언 포함. CostEvent 합계 = est_usd |
| P8 소스 스캔 | `test_p8_policy_source_scan` + `handoff/WP_F_POST_CHANGE_SCAN.txt` 말미 "CACHE FINANCE POLICY SCAN" 절 |

### 0.5 정책으로 대체된 기존 테스트
`test_cache_lifecycle.py`에서 다음 세 테스트를 삭제하고 P2·P3·P4·P6으로 대체·강화했다. 삭제 사유는 파일 안에 주석으로 남겼다.
- `test_kf12_shortfall…` — 저널 조작 방식이었다.
- `test_kf13_operator_actions_have_no_player_effect` — 이전 정책(운영자 종료 무환급)을 고정하던 테스트다.
- `test_refund_excludes_interpretation_and_non_payers` — 해석을 선불에 합산하던 방식이었다.

`WINDOW_NO_PLAYER_EFFECT`를 쓰던 기존 테스트 3곳(원격 삭제 실패 보관 ESTIMATE, d006d 수렴, 레거시 래퍼 경로)은 `WINDOW_SETTLE_REFUND`로 바꿨다. 이 테스트들의 단언은 보관 CostEvent와 finalizer 수렴에 관한 것이라 결과가 달라지지 않는다.

### 0.6 source archive
`WP_F_SOURCE_<final-sha>.tar.gz` — 최종 커밋에서 `git archive`로 만들었고 `media/`를 제외했다. 아카이브와 SHA-256은 최종 커밋 이후에 생성되므로(자기 참조 방지) 저장소에 커밋하지 않고 채팅에 첨부·보고한다.


---

## 1. Identity (§27 items 1–6)

| 항목 | 값 |
|---|---|
| exact start SHA | `50653a96f9a68a97a5edd8e1007aceb957b22609` (fetch 후 확인; 로컬 체크아웃은 WP-B `5991466`이었으므로 exact SHA로 재설정) |
| branch | `claude/wp-f-derived-systems-stabilization-w258jk` — 세션 시스템이 지정한 push 대상(권장명 `claude/wp-f-derived-systems-stabilization` + 접미사) |
| code candidate head | `cdb05a5` |
| final SHA | 이 번들을 추가한 커밋(부모 = `cdb05a5`). 정확한 값·local==remote·literal `git status --short`는 push 직후 채팅 보고에 기재 |
| baseline (start) | `503 passed, 1 xfailed, 0 failed, 0 XPASS` — 유일 xfail `test_d006e_single_settlement_point` ✔ |
| final regression | **`571 passed, 0 failed, 0 xfailed, 0 XPASS`** |

### Commit chain (start → final)
```
4a64859 wp-f(F7-F9): cache lifecycle finance — single finalizer, durable prepayment/refund, d006e resolved (internal milestone)
8b8d35a wp-f(F5-F6): compression source-identity guard, serialized apply, branch-independent settle (internal milestone)
57393cc wp-f: WP-E d006e hand-off test now asserts WP-F resolution
cf58ff5 wp-f(F1-F4): message/UI lifecycle — transient registry, prompt terminal-once, display single surface (internal milestone)
25deafb wp-f(F10): restart re-adoption of journaled cache, connection-proof tests, docs count sync
cdb05a5 wp-f(F3): ROLL growth notice emitted only from committed growth (derived after COMMITTED)
<final>  wp-f: completion bundle + post-change scan
```
중간 커밋은 모두 내부 milestone이며 VERIFIED 기준점이 아니다. 캐시 재무는 fresh scan 판정상 **NORMAL continuation**(세션 스키마 변경 없음, 새 저널 파일 1종, InkTransaction은 별도 레코드 타입 확장)이었으므로 별도 체크포인트 분할은 하지 않았다.

## 2. Exact changed files (§27 item 7)

신규 production: `core/cache_lifecycle.py`, `core/message_lifecycle.py`
수정 production: `core/accounts.py` (생애주기 조정 프리미티브 추가 — 기존 함수 무변경), `core/ink_transactions.py` (LifecycleInkTransaction 추가 — CHARGE 무변경), `core/cost.py` (단위 명시 헬퍼 2개 추가), `core/cache.py` (재시작 복구 → 서비스), `core/io.py` (`process_cache_deletion` → 호환 래퍼), `core/display.py`, `core/dialogue.py`, `core/memory_plan.py`, `core/session_flow.py`, `core/__init__.py`, `core/turn_history.py` (+3줄: `restore_reversible`이 기억 세대 증가), `core/commit_coordinator.py` (+6줄: `commit_serialization_lock` 공개 접근자), `core/turn_preparation.py` (+2줄: `growth_notices`), `cogs/game.py`, `cogs/gm.py`, `cogs/session.py`, `cogs/system.py`, `cogs/presence.py`, `CLAUDE.md` (verify_docs --fix: core 서브모듈 수 — 시작 시점부터 45≠54 드리프트 존재, 현재 56)
테스트: 신규 `tests/policy/test_cache_lifecycle.py`, `test_compression_safety.py`, `test_message_lifecycle_wpf.py`; 수정 `tests/defects/test_cache_accounting.py` (d006d·d006e), `tests/policy/test_accounts_strict.py` (허용 owner 목록 +cache_lifecycle), `test_ink_transactions.py` (deduct_ink 호출자 2→1), `test_wp_e_patch.py` (d006e 인계 테스트 → WP-F 해소 단언)
증거: `handoff/WP_F_COMPLETION_BUNDLE.md`, `handoff/WP_F_POST_CHANGE_SCAN.txt`

**불가침 확인:** `git diff 50653a9..HEAD -- prompts.py scenarios/` = 0 파일.

## 3. Pre/post ownership map (§27 item 8)

| 관심사 | WP-F 이전 | WP-F 이후 |
|---|---|---|
| provider 캐시 create/get/delete | session·game(_reissue_cache)·cache(restore)·display(close)·system(×3)에 산재 | `core.cache_lifecycle` 저수준 프리미티브만(`_provider_create`/`_provider_delete`/restore의 `caches.get`) |
| 캐시 종료·보관 정산 | `process_cache_deletion`(일부 경로) / display close는 정산 없음 / 만료는 finalizer 없음 | `_finalize_lifecycle_locked` 단일 finalizer — 모든 close/delete/reissue/expiry/recovery-replace |
| 캐시 생성 비용 | create **전** 예상액(계획 TTL 저장 포함) accrue | create **성공 후** CACHE_CREATE/RECOVERY_CREATE CostEvent(결정적 키) + 실제 생성분 accrue |
| 캐시 보관 비용 | 경과분 accrue(일부 경로) 또는 누락 | CACHE_STORAGE CostEvent 생애주기당 1회(실제 생존 초, TTL 상한) |
| 오픈 선불 | `accounts.deduct_ink` 직접(마커 없음) | PREPAYMENT LifecycleInkTransaction(결정적 ID·계정 마커·원장) |
| 닫기 환급 | `accounts.add_ink` 직접, 보관 사실 없음 | REFUND LifecycleInkTransaction 1회(창 정산 의도 durable) — 플레이어·만료·운영자 종료 모두(gate patch) |
| 시간 해석 청구 | 선불에 합산(재오픈 경로는 청구 누락) · 닫기 시 함께 환급 | INTERPRETATION_CHARGE 별도 거래, 환불 없음(gate patch) |
| 백그라운드 압축 적용 | 태스크 완료 즉시 무조건 적용(출처 검증 없음) | 출처 식별(세대·접두 지문) 재검증 후에만 — 커밋 락+io 락 안 |
| 대기 안내(WaitingStatus) | 호출자 수동 `done()` | 멱등 `done()` + finally + 내구 등록부(재시작 sweep) |
| 게임 채널 운영 안내 | `send_streamed`(game_chat 로그에 기록·영구 잔존) | `message_lifecycle.send_transient`(분류·등록·supersede/TTL 정리, 로그 미기록) |
| 확인 프롬프트 | View timeout 시 방치 | `LifecyclePromptView` — CONFIRM/CANCEL/TIMEOUT/SUPERSEDE 정확히 1회 |
| 상태판 | 모든 예외에서 재생성(중복 가능), 새 ID 미영속 | NotFound/Forbidden에서만 재생성 + ID 영속 |
| ROLL 성장 알림 | 판정 시점(커밋 전) 게임 채널 송출 | 커밋된 성장 사실에서만 derived → COMMITTED 이후 WP-E 송출 의도와 함께 |

변경되지 않은 권위: CommitCoordinator(커밋), CommitJournal(복구), Settlement/InkTransaction CHARGE(턴 청구), CostLedger(관측), `core.turn_history`(선택·정리 부채·캐시 출처 판정).

## 4. Final message taxonomy & send-site inventory (§27 items 9–10)

분류는 승인된 5종 그대로(신설 없음): `CANONICAL_DISPLAY · INTERACTION_PROMPT · TRANSIENT_GAME_STATUS · CANONICAL_GAME_EVENT · OPERATOR_LOG` (`core/message_lifecycle.MESSAGE_CLASSES`).

| 사이트 | 분류 | owner / 종결 |
|---|---|---|
| `display.refresh` (9 호출부) | CANONICAL_DISPLAY | edit 우선, 소실 시 재생성+ID 영속 |
| Rewind/Rerender/Open/CloseConfirmView | INTERACTION_PROMPT | `LifecyclePromptView.claim` + `on_timeout` 접기·삭제 |
| 디스플레이 유지 시간 질문·재질문 | INTERACTION_PROMPT | 등록부 `display_open_prompt` — 답변 처리 시 정리, 재질문은 supersede |
| 판단/묘사 `WaitingStatus` | TRANSIENT_GAME_STATUS | finally `done()`(멱등), 등록부 `waiting:*`, 재시작·admission sweep |
| 턴 실패 안내 ×4, 세션 닫힘·잉크 부족 | TRANSIENT_GAME_STATUS | 키 `turn_notice` — 다음 턴 시작(`_run_gm_logic_loop`)이 supersede |
| 만료 안내(presence), 업로드 진행/결과(재오픈·세션 플로우) | TRANSIENT_GAME_STATUS | 키 `session_notice` — 교체 + TTL 60s(업로드) |
| 묘사 스트림·이미지·코드블록 (`_deliver_narration`) | CANONICAL_GAME_EVENT | WP-A/E 출력 ID 귀속 (무변경) |
| `_emit_commit_derived` (소속 획득·엔딩·**성장 확정**) | CANONICAL_GAME_EVENT | durable COMMITTED 이후 + WP-E 송출 의도·매핑 (무변경 경로 재사용) |
| 판정 결과·행운·행동 종합 (`_execute_rolls`, gm:1360) | CANONICAL_GAME_EVENT(판정 입력 사실) | 현행 유지 — 상태 변이 알림 아님 |
| GMRollView·ExtractionRetryView | INTERACTION_PROMPT(턴 소유) | 기존 on_timeout 자동굴림 / 성공 시 삭제 — 현행 유지 |
| 마스터 채널 m_send·비용 임베드 | OPERATOR_LOG | 마스터 전용; `send_operator`는 플레이어 채널 폴백 없음 |
| `cogs/character.py`·`!주사위`·`!수정`·`!세션종료` 잠금 안내·`media.py` | 수동/운영 명령 | **DEFERRED_BY_SCOPE(WP-G)** — 레거시 수동 명령 은퇴 범위 |

전체 AST 목록: `handoff/WP_F_POST_CHANGE_SCAN.txt` §"direct game-channel sends", §"WaitingStatus / transient lifecycle entry points", §"display canonical refresh writers", §"interaction prompt lifecycle owners", §"committed game-notification writers".

## 5. Compression scheduler/apply/version scan (§27 item 11)

```
scheduler  cogs/game.py:311  capture_compression_source → is_compressing=True(동기) → create_task(_run_auto_compression)
manual     cogs/game.py:1497 capture_compression_source (is_compressing 이면 거부, 실행 중 표식 설정)
apply      cogs/game.py:955  commit_serialization_lock → session_io_lock → memory_plan.apply_compression_result
validate   core/memory_plan.py:283 compression_source_status(세대·접두 길이·접두 지문)
version    core/memory_plan.py:302 (적용 시) · core/turn_history.py:273 (restore_reversible — 되감기·재생성 시작/중단)
mutations  compressed_memory/uncompressed_logs 접두 삭제는 memory_plan.apply_compression_result 한 곳
cost       OP_MEMORY_AUTO_COMPRESSION(game.py:1012) · OP_MEMORY_MANUAL_COMPRESSION(game.py:1505) — 적용 전에 기록(폐기돼도 사실 유지)
```
설계 판단: 출처 판정은 "출발 시점 접두가 아직 정본 이력의 접두인가"다. 압축을 촉발한 턴이 동시에 커밋되며 **뒤에 append**하는 것은 정상 경로이므로 무효화하지 않는다(C-F02a). 접두 변경(`!수정`)·이력 복원(세대 증가)·다른 압축의 적용은 무효화한다(C-F02b/03/04, 동일 출처 이중 적용 방지).

## 6. Cache create/get/delete/finalize callers (§27 item 12)

```
provider create  core/cache_lifecycle.py:225  (_provider_create — _create_locked 전용)
provider delete  core/cache_lifecycle.py:238  (_provider_delete — finalizer·보상 삭제 전용)
provider get     core/cache_lifecycle.py:803  (restore 연동 확인)
open_window      cogs/session.py:309 (upload_cache ← 세션 플로우·재오픈·!새세션)
reissue          cogs/game.py:607 (묘사 자동: 만료 오류·부재·출처 무효) · cogs/system.py:272 (!캐시 재발급)
close_window     core/display.py:456 (PLAYER_CLOSE, 환급) · cogs/presence.py:77 (EXPIRED, 환급) ·
                 cogs/system.py:216 (!세션종료) · cogs/system.py:296 (!캐시 삭제) — gate patch: 운영자도 SETTLE_REFUND(payer 환급)
restore          core/cache.py (restore_sessions_from_disk)
finalize_legacy  core/io.py:528 (process_cache_deletion 호환 래퍼 — 프로덕션 호출자 0)
```
K-F14 AST 테스트가 `core/cache_lifecycle.py` 밖의 `caches.create/get/delete` 참조 0건과 cogs의 `process_cache_deletion(` 0건을 강제한다.

## 7. Cache / prepayment / refund / additional-charge financial writers (§27 item 13)

```
PREPAYMENT  core/cache_lifecycle.py:555  IT.execute_lifecycle_ink(kind=PREPAYMENT)  ← open_window / resume (캐시분만)
REFUND      core/cache_lifecycle.py:639  IT.execute_lifecycle_ink(kind=REFUND)      ← 창 정산(SETTLE_REFUND — 모든 종료 경로)
INTERPRETATION_CHARGE  core/cache_lifecycle.py:590  ← open_window(캐시 생성 전) / resume
ADDITIONAL_CHARGE  정의만 — POLICY-CACHE-03 → production callers: 0, 부족분은 WINDOW_SETTLE_INTENT.operator_borne_shortfall 로만 기록
레거시 잔존(범위 밖·WP-G): system.py !지급/!잉크(add_ink·deduct_ink·set_balance), terms.py 가입선물(add_ink)
```

## 8. InkTransaction / account schema changes (§27 item 14)

- `core/accounts.py`: `apply_ink_adjustment_strict` (DEBIT/CREDIT, 같은 per-user 락·strict 로드·`applied_ink_transactions` 마커·원자 교체). DEBIT = 레거시 floor(잔액 1 미만 → 1, 초과분 운영자 부담), `total_spent_ink += nominal`; CREDIT = 잔액 가산, 누적 필드 무변경(레거시 환급과 동일). 기존 `apply_ink_charge_strict`·레거시 함수 무변경.
- `core/ink_transactions.py`: `LifecycleInkTransaction`(frozen) — `ink_tx_id, reference_kind, reference_id, user_id, kind, direction, nominal_ink(≥0), balance_before/after, applied_balance_delta, overdraft, operator_subsidy_ink, reason, created_at`. 결정적 ID `ink-{kind}:{window_id}:user:{uid}`. 요청 지문 = reference/user/kind/direction/nominal. 라우팅(§20)은 CHARGE와 동일: 마커+원장→ALREADY, 마커만→원장 복구, 원장만→RecoveryRequired, 신규→계정 원자 기록 후 원장 append, 0잉크→NO_MUTATION. 같은 per-user append-only 원장, 과거 레코드 재작성 없음. **부호는 방향 필드로 명시(음수 nominal 금지).**
- 세션 스키마(`SESSION_FIELDS`, `SCHEMA_VERSION`) 변경 없음.

## 9. CostLedger operation changes (§27 item 15)

새 operation 없음. 이미 정의만 되어 있던 `OP_CACHE_CREATE`, `OP_CACHE_STORAGE`, `OP_CACHE_RECOVERY_CREATE`를 처음으로 연결했다. strict append(`record_cost_event_strict`), 결정적 idempotency key `cache:{sid}:{cache_name}:create|storage`, session 범위(`transaction_id=None` — 턴 Settlement에 claim되지 않음). actor/hint: OPEN=PLAYER/PLAYER_CANDIDATE, REISSUE_AUTO·RECOVERY=SYSTEM/SYSTEM, REISSUE_MANUAL=OWNER/OPERATOR. usage_source: 보관=FIXED_PROVIDER_PRICING(실측 초 × 단가), 원격 삭제 실패 시 ESTIMATE(만료까지 예정분). cached-input read 이벤트는 추가하지 않는다(이중 계상 금지 보존). 압축 operation은 무변경(메타데이터에 출처 세대·접두 길이 추가).

## 10. `d006e` resolution (§27 item 16)

- strict xfail 제거. `test_d006e_single_settlement_point`는 **정상 게이트 테스트로 통과**한다.
- 옛 단언의 전제("업로드 시 계획 TTL 예상액이 이미 누적됐다")는 WP-F가 제거한 결함 자체였으므로, 같은 결함 의미를 최종 구조로 재표현했다(§21): 실제 `open_window`→1시간→`close_window` 후 (a) 열기 직후 누적 < 계획 TTL 예상, (b) 종료 후 `total_cost ≈ 생성 + 1시간 보관`, (c) CostEvent가 정확히 `CACHE_CREATE` 1 + `CACHE_STORAGE` 1이며 합계가 같은 값.
- WP-E의 인계 테스트(`test_ee3_d006e_…`)는 "xfail 아님 + cache_lifecycle 사용"을 단언하도록 갱신.
- d006d는 "세 경로 발산" 특성에서 "종료 경로 수렴 + 예상≠사실"로 갱신. d006a/b/c/f는 그대로 통과(순수 헬퍼 사실·호환 래퍼 결과 보존).

## 11. Named targeted tests (§27 item 17) — WP-F 기존 70건 + gate patch 11건(§0.4), 전부 PASS

```
  tests/policy/test_cache_lifecycle.py::test_kf01_estimate_is_not_a_cost_event
  tests/policy/test_cache_lifecycle.py::test_kf02_kf04_open_records_prepayment_and_create_fact_separately
  tests/policy/test_cache_lifecycle.py::test_kf03_failed_create_fabricates_nothing
  tests/policy/test_cache_lifecycle.py::test_create_success_but_journal_failure_compensates
  tests/policy/test_cache_lifecycle.py::test_kf05_kf11_kf06_close_settles_storage_and_refund_exactly_once
  tests/policy/test_cache_lifecycle.py::test_kf07_remote_not_found_still_finalizes
  tests/policy/test_cache_lifecycle.py::test_remote_delete_failure_bills_storage_to_expiry_as_estimate
  tests/policy/test_cache_lifecycle.py::test_kf08_remote_deleted_then_local_failure_replays_once
  tests/policy/test_cache_lifecycle.py::test_refund_account_failure_then_restart_replay
  tests/policy/test_cache_lifecycle.py::test_kf09_reissue_finalizes_old_once_and_keeps_window
  tests/policy/test_cache_lifecycle.py::test_reissue_create_failure_closes_window_with_refund
  tests/policy/test_cache_lifecycle.py::test_kf10_recovery_recreate_records_only_after_success
  tests/policy/test_cache_lifecycle.py::test_kf10b_recovery_recreate_success
  tests/policy/test_cache_lifecycle.py::test_restore_expired_window_finalizes_without_recreate
  tests/policy/test_cache_lifecycle.py::test_legacy_open_session_is_adopted_and_settled
  tests/policy/test_cache_lifecycle.py::test_kf15_new_cache_receives_history_marker
  tests/policy/test_cache_lifecycle.py::test_kf15b_failed_reissue_keeps_stale_cache_unusable
  tests/policy/test_cache_lifecycle.py::test_reissue_racing_close_is_serialized
  tests/policy/test_cache_lifecycle.py::test_concurrent_close_calls_single_financial_result
  tests/policy/test_cache_lifecycle.py::test_expiry_path_reaches_finalizer_and_allows_reopen
  tests/policy/test_cache_lifecycle.py::test_kf14_no_lifecycle_bypassing_provider_cache_calls
  tests/policy/test_cache_lifecycle.py::test_storage_helper_units_are_explicit
  tests/policy/test_cache_lifecycle.py::test_create_journaled_but_session_save_lost_is_readopted_on_restart
  tests/policy/test_cache_lifecycle.py::test_restart_path_reaches_lifecycle_service_for_expired_window
  tests/policy/test_cache_lifecycle.py::test_narration_reissue_routes_through_lifecycle_and_stays_out_of_turn_settlement
  tests/policy/test_compression_safety.py::test_cf01_same_version_applies
  tests/policy/test_compression_safety.py::test_cf02a_cf05_append_during_compression_applies_and_keeps_newer_logs
  tests/policy/test_compression_safety.py::test_cf02b_later_change_to_source_discards_and_preserves_logs
  tests/policy/test_compression_safety.py::test_cf03_rewind_is_blocked_while_compressing
  tests/policy/test_compression_safety.py::test_cf03_cf04_restored_history_rejects_old_result[rewind]
  tests/policy/test_compression_safety.py::test_cf03_cf04_restored_history_rejects_old_result[rerender_abort]
  tests/policy/test_compression_safety.py::test_cf06_cf07_cost_survives_discard_and_no_settlement_touch
  tests/policy/test_compression_safety.py::test_cf08_provider_failure_mutates_nothing
  tests/policy/test_compression_safety.py::test_cf08b_cancellation_mutates_nothing_and_releases_flag
  tests/policy/test_compression_safety.py::test_cf09_runtime_generation_is_not_persisted
  tests/policy/test_compression_safety.py::test_cf09b_source_from_previous_process_object_cannot_touch_restored_session
  tests/policy/test_compression_safety.py::test_two_results_from_same_source_apply_once
  tests/policy/test_compression_safety.py::test_manual_compression_refused_while_auto_running
  tests/policy/test_compression_safety.py::test_apply_waits_for_commit_critical_section
  tests/policy/test_compression_safety.py::test_legacy_settle_is_branch_independent_source_scan
  tests/policy/test_message_lifecycle_wpf.py::test_mf01_refresh_edits_same_display_message
  tests/policy/test_message_lifecycle_wpf.py::test_mf02_missing_display_recreated_and_id_persisted
  tests/policy/test_message_lifecycle_wpf.py::test_mf02b_transient_failure_does_not_create_second_surface
  tests/policy/test_message_lifecycle_wpf.py::test_mf03_confirm_resolves_and_removes_prompt
  tests/policy/test_message_lifecycle_wpf.py::test_mf04_cancel_resolves_and_removes_prompt
  tests/policy/test_message_lifecycle_wpf.py::test_mf05_timeout_disables_and_collapses_prompt
  tests/policy/test_message_lifecycle_wpf.py::test_mf05b_timeout_then_confirm_race_has_single_outcome
  tests/policy/test_message_lifecycle_wpf.py::test_mf05c_confirm_then_timeout_race_has_single_outcome
  tests/policy/test_message_lifecycle_wpf.py::test_open_confirm_timeout_resets_selection
  tests/policy/test_message_lifecycle_wpf.py::test_all_display_confirmation_views_use_prompt_lifecycle
  tests/policy/test_message_lifecycle_wpf.py::test_mf06_waiting_status_success_cleanup
  tests/policy/test_message_lifecycle_wpf.py::test_mf07_judgment_exception_leaves_no_waiting_status
  tests/policy/test_message_lifecycle_wpf.py::test_mf07b_narration_cancel_leaves_no_waiting_status
  tests/policy/test_message_lifecycle_wpf.py::test_committed_turn_leaves_no_transient
  tests/policy/test_message_lifecycle_wpf.py::test_restart_sweep_removes_orphaned_waiting_status
  tests/policy/test_message_lifecycle_wpf.py::test_transient_not_written_to_game_log
  tests/policy/test_message_lifecycle_wpf.py::test_mf08_supersede_clears_bot_notice_but_not_player_declaration
  tests/policy/test_message_lifecycle_wpf.py::test_mf08b_clear_is_idempotent_on_already_deleted
  tests/policy/test_message_lifecycle_wpf.py::test_mf09_mf12_committed_notice_emitted_after_commit_and_mapped
  tests/policy/test_message_lifecycle_wpf.py::test_mf10_failed_commit_emits_no_durable_notice
  tests/policy/test_message_lifecycle_wpf.py::test_mf11_operator_log_never_falls_back_to_player_channel
  tests/policy/test_message_lifecycle_wpf.py::test_mf11b_player_notices_route_through_lifecycle_source_scan
  tests/policy/test_message_lifecycle_wpf.py::test_mf09_growth_notice_only_after_commit
  tests/policy/test_message_lifecycle_wpf.py::test_mf10_growth_notice_absent_when_turn_fails
  tests/defects/test_cache_accounting.py::test_d006a_upload_estimate_is_charged_for_planned_ttl
  tests/defects/test_cache_accounting.py::test_d006b_direct_close_path_settles_by_elapsed
  tests/defects/test_cache_accounting.py::test_d006c_settlement_cap_uses_planned_ttl
  tests/defects/test_cache_accounting.py::test_d006d_close_paths_converge_on_one_finalizer
  tests/defects/test_cache_accounting.py::test_d006e_single_settlement_point
  tests/defects/test_cache_accounting.py::test_d006f_storage_cost_helpers_disagree_on_units
```
기존 제품 테스트 중 의도적으로 갱신: `test_accounts_strict::test_29` (strict 소비 owner 허용 목록 + `core/cache_lifecycle.py`), `test_ink_transactions::test_70` (cogs의 deduct_ink 2→1, session.py 0), `test_wp_e_patch::test_ee3_d006e_resolved_by_wp_f_not_by_wp_e`.

## 12. Failure injection (§27 item 18)

| 주입 | 결과 |
|---|---|
| provider create 실패(오픈) | CostEvent 0, 메타데이터 없음, 잔액 무변, 창 ABORTED (K-F03) |
| create 성공 + CREATED 저널 기록 실패 | 보상 삭제, 메타·재무 무변 (추적 불가 캐시 없음) |
| 원격 삭제 성공 + 보관 CostEvent 기록 실패 → 재시도 | 재삭제 없음, closed_at 고정, 보관 이벤트 1·환급 1 (K-F08) |
| 원격 NotFound | GONE으로 종료, 보관=closed_at까지 (K-F07) |
| 원격 삭제 일반 실패 | FAILED — 만료까지 보관(ESTIMATE) |
| 환급 계정 쓰기 실패 → 재시작 | 정산 의도 durable, 재시작 resume이 1회 완료, 재호출 무변 |
| 재발급 create 실패 | 구 생애주기 1회 종료, 생성 사실 없음, 창 정산(환급) |
| 복구 재생성 실패 | RECOVERY_CREATE 없음, 창 정산 |
| 생성 저널 O·세션 저장 유실 → 재시작 | 저널 권위로 재채택(LINKED), 재생성·고아 없음 |
| 압축 provider 실패 / 태스크 취소 | 정본 무변, `is_compressing` 해제 (C-F08) |
| strict 세션 저장 실패(커밋) | 거짓 소속·성장 알림 없음 (M-F10) |
| 상태판 fetch 일시 오류(5xx) | 두 번째 상태판 생성 없음 (M-F02b) |

## 13. Concurrency (§27 item 19)

| 경합 | 선택한 소유 방식 · 증거 |
|---|---|
| 압축 결과 vs 새 턴 커밋 | 적용이 커밋 직렬화 락 + io 락 안 — 커밋 임계구역 동안 대기 (`test_apply_waits_for_commit_critical_section`), 뒤 append 보존 (C-F02a) |
| 압축 결과 vs 되감기/재생성 | WP-E `_busy_reason`이 압축 중 조작 거부(보존) + 복원 시 세대 증가로 늦은 결과 폐기 (C-F03/04) |
| 같은 출처 두 결과 / 수동 vs 자동 | 1회만 적용, 이중 접두 삭제 없음; 수동은 자동 실행 중 거부 |
| 캐시 재발급 vs 명시적 닫기 | 세션별 생애주기 락으로 직렬화 — 모든 생애주기 정확히 1회 종료, 창 정산 1회 |
| 동시 닫기 3회 | 보관 1·환급 1·원격 삭제 1 |
| finalizer 재시도(원격 삭제 후 로컬 실패) | REMOTE_RESULT·closed_at durable 재사용 |
| transient 송출 vs 턴 실패/supersede | 키 교체·다음 턴 supersede, 플레이어 선언 불삭제 (M-F08) |
| 프롬프트 timeout vs confirm/cancel | `claim` 정확히 1회 (M-F05b/c) |

## 14. Compile / import / routines (§27 item 20)

- `python3 -m compileall -q core cogs main.py prompts.py` → OK
- CLAUDE.md ① 구문·미정의 self 참조 → OK · ② 재수출·세션 필드 → OK/OK · ③ 퀘스트 트리 → OK(시나리오 무변경) · ④ `verify_docs` → OK(21항목) · ⑤ 봇 로딩 → `cogs 9 | 명령어 46 | views 5` (기준값 동일)

## 15. Final full regression (§27 item 21)

`python3 -m pytest tests/ -q -rxX` → **579 passed, 1 warning (discord audioop deprecation) — 0 failed, 0 xfailed, 0 XPASS**. (시작 504건 = 503 passed + 1 xfailed(d006e → 이제 통과) + WP-F 신규 67건 = 571 → gate patch: 정책 대체 −3 + P1~P8 +11 = 579)

## 16. Preservation List evidence (§27 item 22)

| ID | 증거 |
|---|---|
| P-F01 WP-E 이력 권위 | 선택 인덱스·SELECT 경로 무변경(스캔 §turn_history SELECT); 메시지 등록부는 정본 출력을 담지 않음 |
| P-F02 SELECT after COMMITTED | `select_committed` 호출부 무변경; `test_wp_e_patch` E-E1 전부 통과 |
| P-F03 재무 이력 불되감기 | turn_history의 재무 참조 0 (스캔 마지막 절); 되감기 테스트 전부 통과 |
| P-F04/05/06 정리 부채·송출 의도·수동 ack | `_emit_commit_derived`/`drain_cleanup` 무변경, 성장 알림도 이 경로 재사용 (M-F09 매핑 단언) |
| P-F07 플레이어 선언 보존 | 등록부는 봇 송신 ID만 (M-F08) |
| P-F08/09 캐시 출처·가역 필드 | 모든 생성이 `update_session_cache_state` 경유 (K-F15, 묘사 재발급 연결 증명); 실패 재발급은 stale 유지 (K-F15b); `REVERSIBLE_FIELDS` 무변경 |
| P-F10 커밋 owner | 코디네이터 무변경(+락 접근자만); 알림은 derived 소비 |
| P-F11/13/14 Settlement·FAILED_SYSTEM·되감기 청구 | 캐시·압축 CostEvent는 session 범위 — 턴 Settlement 미포함 (연결 증명 테스트), 청구 테스트 전부 통과 |
| P-F12 CostLedger exactly-once | 결정적 키 strict append; 재시도·동시 닫기에서 이벤트 1 |
| P-F15/16 생성·전달 경계, 배리어 | `test_narration_boundary`·`test_ready_barrier` 전부 통과 |
| P-F17 게임 가용성 | 재발급 실패 시 캐시 없이 턴 진행(기존) — 창만 정산 |
| P-F18 prompt/scenario | 무변경 (diff 0) |
| P-F19 운영자 분리 | 운영자 재발급 = OWNER/OPERATOR CostEvent·플레이어 턴 청구 없음; 운영자 종료는 POLICY-CACHE-02에 따라 기존 선불의 미사용분 환급만(새 청구 없음) (P3/P4) |
| P-F20 strict xfail 의미 | d006e는 결함 해소 후 xfail 제거(XPASS 은폐 아님) |
| P-F21 압축 부기 | `mark_compressed`는 기존대로 최초 생성 분기에서만; `last_compressed_turn`/`compression_count` 가역성 무변경 |
| P-F22 no WP-G | 레거시 수동 명령·`!지급`·`total_cost` 표시 은퇴 미착수 |

## 17. Untouched scope (§27 item 23)

`prompts.py`, `scenarios/*.json`, 퀘스트/NPC/프로필 의미, 레거시 수동 명령(`character.py`, `!주사위`, `!진행`, `!수정`), `total_cost`/`total_usd` 호환 표시 은퇴, dead field 정리, HUD — 모두 미변경. WP-G 미착수.

## 18. New findings / decisions requiring confirmation / deferred (§27 item 24)

**게이트 이전에 확인이 필요했던 결정들은 사용자 확정 정책(POLICY-CACHE-01~03)으로 해소되었다(§0):**
1. 시간 해석 비용은 별도 청구이며 환불하지 않는다(POLICY-CACHE-01). 재오픈 경로에서 청구가 누락되던 문제(F-NEW-1)도 수정했다.
2. 선불하지 않은 늦은 참가자에게는 환급하지 않는다. PREPAYMENT 마커를 근거로 판정한다.
3. 재발급 실패·만료·플레이어 닫기·운영자 종료·운영자 삭제 모두 같은 창 정산 권위로 미사용 선불을 환급한다(POLICY-CACHE-02).
4. 실제 비용이 선불을 넘으면 운영자 부담으로 기록하며, ADDITIONAL_CHARGE production 호출자는 0이다(POLICY-CACHE-03).
5. 압축 선결제는 계정 효과가 없는 표시용 누적값으로 유지했다. 정책이 정해지지 않았고 이번 패치 범위도 아니다.

**새로 발견한 사항(범위 밖이라 수정하지 않음):**
- F-NEW-1 (gate patch에서 **수정됨**) `OpenConfirmView`가 업로드 전에 `interpret_cost_krw`를 0으로 만들어 안내와 달리 청구되지 않던 문제 — P1이 수정을 증명한다.
- F-NEW-2 `mark_compressed`가 최초 생성 분기에서만 호출된다. 두 번째 이후의 압축은 `last_compressed_turn`을 올리지 않으므로 주기 판정이 매 턴 참이 될 수 있다(캐시 재발급으로 `compressed_memory`가 비워지면 초기화됨). P-F21에 따라 보존했으며 판단을 요청한다.
- F-NEW-3 `stream_text_to_channel`은 모든 스트리밍 문단을 game_chat 로그에 기록한다. 그래서 WP-F 이전에 `send_streamed`로 나가던 운영 안내가 로그에 섞여 있었다. 이관한 경로는 이제 로그에 기록되지 않지만, 레거시 수동 명령 경로는 WP-G 범위다.
- F-NEW-4 재시작 시 `caches.get`에서 APIError가 아닌 예외(네트워크 등)가 나면 기존에는 세션 복구 전체가 중단됐다. 이제는 경고만 남기고 세션을 등록하며, 다음 조작에서 재시도한다.
- 문서 드리프트: CLAUDE.md의 core 서브모듈 수가 시작 SHA에서 이미 45≠54였다. `--fix`로 현재 값 56을 반영했다.

**잔여 위험:**
- 레거시 `total_cost`/`total_usd` 호환 누적은 저널 이벤트 기록 직후 메모리에서 증가시키고 tolerant 저장한다. 따라서 크래시 창에서는 호환 표시값이 소폭 누락될 수 있다. 권위 사실은 CostEvent이다.
- provider 생성 직후, CREATED 저널 기록 전에 프로세스가 죽으면 원격 캐시가 TTL까지 남을 수 있다. provider 목록 조회가 없으면 구조적으로 막을 수 없는 한계이며, 창은 재시작 시 ABORTED로 처리되어 재무 효과가 없다.

## 19. Hard stop (§27 item 25)

**WP-G: NOT STARTED.** 독립 GPT 게이트를 위해 여기서 멈춘다.
