# WP-00 COMPLETION REPORT

Status: **VERIFIED**
Baseline commit/snapshot: `482ffc0` (production code == v5.33.0, verified by `git diff a5cc828~1 -- '*.py'` → empty)
Work package: WP-00 — executable characterization / defect / policy test harness

---

## Tracks served

- **Track I — Verification / handoff**: 실행 가능한 특성화·결함·정책 테스트 트리 신설.
- **Track A — Audit truth / anti-drift**: 베이스라인 드리프트 1건을 발견·되돌림·기록.

## Tracks explicitly out of scope

- Track B (TurnTransaction 구현) — TID 정책 테스트는 작성했으나 `core.turn_transaction` 부재로 skip.
- Track C (CostLedger 구현) — 청구 정책은 순수 모델로만 기술.
- Track D/E/F/G/H — 손대지 않음.

---

## Changed files

**신규 (테스트 전용)**

```
pytest.ini
requirements-dev.txt
tests/__init__.py
tests/conftest.py
tests/fakes/__init__.py
tests/fakes/discord_fakes.py
tests/fakes/genai_fakes.py
tests/fakes/bot_fakes.py
tests/characterize/__init__.py
tests/characterize/test_harness_selfcheck.py
tests/characterize/test_auto_turn_orchestration.py
tests/characterize/test_execute_proceed_shared_paths.py
tests/characterize/test_persistence_behavior.py
tests/defects/__init__.py
tests/defects/test_extraction_boundary.py
tests/defects/test_turn_commit_races.py
tests/defects/test_instruction_side_effects.py
tests/defects/test_rewind_accounting.py
tests/defects/test_cache_accounting.py
tests/policy/__init__.py
tests/policy/test_transaction_identity_policy.py
tests/policy/test_billing_policy.py
tests/policy/test_message_lifecycle_policy.py
handoff/WP00_COMPLETION_REPORT.md
handoff/WP00_CALLSITE_SCAN.txt
handoff/WP00_TEST_RESULTS.txt
handoff/WP00_CHANGED_FILES.txt
handoff/WP00_NEW_FINDINGS.md
```

**되돌림 (별도 커밋 `482ffc0`)**

```
cogs/gm.py        _apply_quest_choice 복원
core/quest.py     apply_choice의 narrative_mode·quest_select 제거, 폴백 복원
core/constants.py 5.34.0 → 5.33.0
```

**프로덕션 의미 변경: 없음.**
`git status --short -- core cogs main.py prompts.py scenarios` → 빈 출력.
prompts.py·scenarios/*.json 무수정.

---

## Exact functions/classes/signatures changed

프로덕션 함수·클래스·시그니처 변경 **0건**.
테스트 가능성을 위한 프로덕션 seam도 추가하지 않았다.

신규 테스트 전용 클래스: `FakeBot` · `FakeChannel` · `FakeMessage` · `FakeTyping` ·
`FakeInteraction` · `FakeUser` · `FakeUsageMetadata` · `FakeGenAIResponse` ·
`ScriptedProvider` · `FakeGenAIClient` · `FakeCaches` · `RecordingCog` · `CallRecorder`.

---

## Pre-edit callsite scan

`handoff/WP00_CALLSITE_SCAN.txt` (258줄, 26개 대상 함수).

핵심 결과:

| 함수 | 정의 | 호출 | 비고 |
|---|---|---|---|
| `_execute_proceed` | game.py:235 | 3 | gm(`_dispatch_proceed`) · game(`proceed_turn`) · session(`play_intro`) |
| `_dispatch_proceed` | gm.py:2632 | 1 | `_finish_proceed_and_continue`만 |
| `_finish_proceed_and_continue` | gm.py:1357 | 6 | `_run_gm_logic_loop` 5 + `_continue_with_roll_results` 1 |
| `_start_round` | gm.py:1154 | 4 | |
| `_run_extraction` | gm.py:3079 | 2 | `_dispatch_proceed` + 재시도 버튼 |
| `save_session_data` | io.py | 77 | |

## Post-edit callsite scan

프로덕션 무변경이므로 pre-edit과 동일. 재확인 완료.

---

## Connection proof

WP-00은 새 아키텍처 컴포넌트를 도입하지 않는다. 연결 증명 대상은 **테스트
하네스 자체**다.

```
실제 TRPGSession → 픽스처 → 테스트 단언
```

`tests/characterize/test_harness_selfcheck.py`가 이를 증명한다.

- `test_session_is_real_trpgsession` — `type(...) is core.TRPGSession`
- `test_session_fields_registry_matches_model` — 레지스트리↔모델 대조
- `test_rewind_snapshot_uses_real_capture` — 실제 `core.capture_state` 사용
- `test_fake_bot_fails_loudly_on_unknown_channel/cog` — 조용한 MagicMock 반환 금지

**경계 양쪽을 동시에 모의하지 않았다.** 예:
`test_d001_extraction_input_is_truncated_at_500`은 `_run_extraction`만 가로채고
`_dispatch_proceed`는 실제 코드를 실행한다.

---

## Tests run

Command: `python3 -m pytest tests/ -q`

```
68 passed, 1 skipped, 9 xfailed in 1.25s
```

| 분류 | 개수 |
|---|---|
| characterize | 37 |
| defects | 25 |
| policy | 15 |
| **합계** | **77** |

Passed: 68 · Xfailed: 9 · Skipped: 1 · Failed: 0 · **XPASS: 0**

Skipped 1건: `tests/policy/test_transaction_identity_policy.py` 전체 —
`core.turn_transaction` 부재(WP-01 소유).

### strict xfail 목록 (AUD ID)

| 테스트 | AUD |
|---|---|
| `test_d001b_late_fact_reaches_extraction` | AUD-011 |
| `test_d002c_no_next_round_before_commit` | AUD-012 / AUD-019 |
| `test_d002d_delta_recorded_after_extraction_commit` | AUD-012 |
| `test_d002e_failed_attempt_does_not_charge_player` | AUD-019 |
| `test_d003c_failed_narration_rolls_back_instruction_effects` | AUD-024 |
| `test_d003d_info_ledger_rolls_back_on_failure` | AUD-024 |
| `test_d004c_rewind_to_n_preserves_n_extraction` | AUD-020 |
| `test_d005d_provider_history_is_irreversible` | AUD-029 |
| `test_d006e_single_settlement_point` | AUD-034 / AUD-035 |

전부 `strict=True`. XPASS 시 실패하므로 무성 통과가 불가능하다.

---

## Compile/import checks

```
python3 -m compileall -q core cogs tests main.py prompts.py   →  exit 0
```

전 cog 임포트: `cogs 9 · 명령어 46 · views 5` (기존 검증 루틴과 일치).

---

## 라이브 외부 호출 부재 증명

1. **자격증명 제거** — `conftest._no_live_calls`가 autouse로
   `GEMINI_API_KEY`·`DISCORD_TOKEN`·`GOOGLE_API_KEY`를 `monkeypatch.delenv`.
2. **SDK 생성 차단** — 같은 픽스처가 `google.genai.Client`를 예외 함수로 치환.
   `test_genai_client_construction_is_blocked`가 이를 검증한다.
3. **파일시스템 격리** — `_isolated_cwd`가 `monkeypatch.chdir(tmp_path)`.
   세션/계정/통계 쓰기가 저장소를 오염시키지 않는다.
   `test_cwd_is_isolated`·`test_session_writes_go_to_tmp`가 검증.
4. **정적 스캔** — `tests/` 전체에 `requests`·`urllib`·`socket`·`httpx`·`aiohttp`·
   `discord.Client`·`commands.Bot(` 참조 0건.
   `genai.Client` 참조 1건은 **차단을 검증하는 테스트 자체**다.
5. **제공자 대역** — `ScriptedProvider`는 결정적 큐이며 네트워크를 타지 않는다.

---

## Invariants verified

- [x] no duplicate authoritative owner introduced — 프로덕션 무변경
- [x] every real caller reviewed — `WP00_CALLSITE_SCAN.txt` 26개 대상
- [x] stale async task cannot overwrite newer transaction metadata where this WP owns that guarantee — **WP-00 비소유**. P-004가 계약만 기술
- [x] failed-system billing semantics not worsened — 무변경
- [x] provider history not made rewindable — 무변경. D-005가 현상 고정
- [x] no unauthorized prompt/scenario change — `git status` 빈 출력
- [x] admin/intro/manual paths reviewed where shared functions changed — 변경 없음. C-004가 세 경로를 고정

---

## Known defects intentionally untouched

AUD-011 · AUD-012 · AUD-019 · AUD-020 · AUD-024 · AUD-029 · AUD-034 · AUD-035.
전부 재현 테스트로 고정했고 수정하지 않았다.

추가로 손대지 않은 것:
- 중복 퀘스트 적용 경로 (`_apply_quest_choice` vs `apply_choice`) — Track H 소유
- `_execute_proceed` 3경로 공유 — WP-01 전제
- `save_session_data` 실패 흡수 — strict persistence 소유

---

## New findings/blockers

`handoff/WP00_NEW_FINDINGS.md` 참조. 요약:

| # | 내용 | 성격 |
|---|---|---|
| F-1 | 베이스라인 드리프트 (v5.34.0) — 되돌림 | 통제 |
| F-2 | `cached_worldware_sections`가 생성자에 없음 | 신규 결함 후보 |
| F-3 | `calculate_storage_cost`(초) vs `_usd`(시간) 단위 불일치 | 신규 결함 후보 |
| F-4 | 필수 문서 2건이 저장소에 부재 | 통제 |

**BLOCKED 없음.** 모든 게이트 요건을 충족했다.

---

## Artifacts produced

```
handoff/WP00_COMPLETION_REPORT.md   이 문서
handoff/WP00_CALLSITE_SCAN.txt      26개 함수 호출부 (258줄)
handoff/WP00_TEST_RESULTS.txt       pytest 원문 + 인벤토리 + xfail 목록
handoff/WP00_CHANGED_FILES.txt      변경 파일 목록
handoff/WP00_NEW_FINDINGS.md        신규 발견 4건
```

---

## Gate assessment

Gate name: **WP00-G1**
**PASS**

| 요건 | 증거 |
|---|---|
| 1. characterization tests pass | 37/37 통과 |
| 2. known defects reproducible | 9개 strict xfail + 대응 특성화 |
| 3. intended assertions are `xfail(strict=True)` | XPASS 0 |
| 4. no production semantics changed for tests | `git status` 빈 출력, seam 미추가 |
| 5. C-002/C-003/C-004 증명 | 아래 |

**C-002** — `test_c002_ask_is_not_a_canonical_turn_exit` ·
`test_c002b_narrate_...`가 `_finish_proceed_and_continue` 미호출과
`gm_turns_done` 불변을 단언. `test_c002c`가 한도 초과 시의 강제 PROCEED
예외 경로를 별도로 고정.

**C-003** — `test_c003_roll_view_holds_continuation_reference`가
`GMRollView` 본문에 `_continue_with_roll_results` + `create_task`가 있고
`transaction_id`는 **없음**을 단언(WP-01 전제 확인).
`test_c003b`가 재개 함수의 `_finish_proceed_and_continue` 도달을 확인.

**C-004** — `test_c004_execute_proceed_has_three_distinct_callers`가
`_dispatch_proceed`·`proceed_turn`·`play_intro` 세 소유 함수를 AST로 단언.
`test_c004c`가 수동·인트로 경로는 정규 턴 종료에 닿지 않음을 확인.

---

## WP-01 착수 전 필수 입력

이 보고서 + `WP00_TEST_RESULTS.txt` + `WP00_CALLSITE_SCAN.txt`를
다음 세션에 그대로 넘길 것. 명세가 아니라 **실제로 일어난 일**이 기준이다.
