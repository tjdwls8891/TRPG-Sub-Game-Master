# WP-G 최종 시나리오 매트릭스 (인계서 §14)

- 기준: `claude/wp-g-legacy-retirement-final-stabilization` 최종 후보(해당 SHA는 `WP_G_COMPLETION_BUNDLE.md` §1)
- 실행: `scratchpad/scen.py`가 아래 노드 ID만 골라 `pytest -rA`로 실행하고, PASSED/FAILED 행을 그대로 옮겼습니다.
- 결과: **69행 모두 PASS** (파라미터 전개 포함 pytest 73 passed, 실패 0).
- 방침: 기존 패키지(WP-A~F) 테스트를 우선 인용했습니다. WP-G에서 새로 추가한 것은 G 범위 행동(은퇴·비용 상한·D3·U-2)뿐입니다. 행 수를 늘리려고 중복 테스트를 만들지 않았습니다.
- 전체 회귀는 완료 번들 §15에 기록합니다.

| 그룹 | 사례 | 증거 테스트 (노드 ID) | 결과 |
|---|---|---|---|
| Normal | successful turn | `tests/policy/test_authoritative_commit.py::test_d_happy_path_full_durable_order` | PASS |
| Normal | ASK → reply → PROCEED | `tests/policy/test_transaction_identity_policy.py::test_tid002_ask_continuation_reuses_identity` | PASS |
| Normal | ASK → reply → PROCEED | `tests/characterize/test_auto_turn_orchestration.py::test_c002c_ask_limit_forces_canonical_proceed` | PASS |
| Normal | NARRATE → reply | `tests/policy/test_transaction_identity_policy.py::test_tid003_narrate_continuation_reuses_identity` | PASS |
| Normal | ROLL → callback | `tests/policy/test_transaction_wiring.py::test_w01_roll_view_round_trip_preserves_single_id` | PASS |
| Normal | ROLL → callback | `tests/policy/test_ready_barrier.py::test_b1_roll_growth_staged_projected_and_applied_after_ready` | PASS |
| AI/provider | judgment failure | `tests/policy/test_message_lifecycle_wpf.py::test_mf07_judgment_exception_leaves_no_waiting_status` | PASS |
| AI/provider | instruction failure | `tests/policy/test_turn_history.py::test_e_rerender_instruction_failure_is_failed_system_zero_charge` | PASS |
| AI/provider | narration failure | `tests/policy/test_billing_policy.py::test_p001_system_failure_is_not_billed` | PASS |
| AI/provider | narration failure | `tests/policy/test_ready_barrier.py::test_fc01_generation_failure_launches_nothing` | PASS |
| AI/provider | extraction failure | `tests/policy/test_ready_barrier.py::test_tc12_exhausted_extraction_no_ready_no_charge_then_retry` | PASS |
| AI/provider | retry success | `tests/policy/test_costledger_shadow.py::test_retry_then_success_numbers_attempt` | PASS |
| AI/provider | retry success | `tests/policy/test_ready_barrier.py::test_tc11_extraction_provider_retry_same_transaction` | PASS |
| AI/provider | all retries fail | `tests/policy/test_ready_barrier.py::test_tc12_exhausted_extraction_no_ready_no_charge_then_retry` | PASS |
| AI/provider | all retries fail | `tests/policy/test_billing_policy.py::test_p001b_provider_cost_is_still_recorded_on_failure` | PASS |
| Commit | strict save failure | `tests/policy/test_authoritative_commit.py::test_d_failure_during_apply_or_strict_save` | PASS |
| Commit | journal failure | `tests/policy/test_commit_recovery.py::test_r_corrupted_journal_blocks_gameplay` | PASS |
| Commit | journal failure | `tests/policy/test_commit_journal.py::test_9_fsync_failure_is_observable_and_not_success` | PASS |
| Commit | account failure | `tests/policy/test_commit_recovery.py::test_r_first_user_account_write_fails_then_restart` | PASS |
| Commit | post-account ledger failure | `tests/policy/test_commit_recovery.py::test_r_account_written_ledger_append_fails` | PASS |
| Commit | post-account ledger failure | `tests/policy/test_ink_transactions.py::test_60_replay_after_59_repairs_ledger_without_rededuct` | PASS |
| Commit | restart mid-commit | `tests/policy/test_commit_recovery.py::test_r_crash_after_replace_before_session_persisted_restart` | PASS |
| Commit | restart mid-commit | `tests/policy/test_commit_recovery.py::test_r_crash_after_billing_restart_no_recharge` | PASS |
| Concurrency | slow extraction | `tests/policy/test_ready_barrier.py::test_tc03_fast_stream_slow_extraction_blocks_next_turn` | PASS |
| Concurrency | fast extraction | `tests/policy/test_ready_barrier.py::test_tc04_slow_stream_fast_extraction_waits_delivery` | PASS |
| Concurrency | stale extraction | `tests/policy/test_ready_barrier.py::test_tc13_stale_extraction_cannot_satisfy_barrier` | PASS |
| Concurrency | duplicate callback | `tests/policy/test_transaction_wiring.py::test_w04_stale_roll_callback_does_not_resume` | PASS |
| Concurrency | duplicate commit | `tests/policy/test_authoritative_commit.py::test_d_duplicate_commit_call_is_rejected` | PASS |
| Concurrency | duplicate commit | `tests/policy/test_authoritative_commit.py::test_d_concurrent_duplicate_commit_calls` | PASS |
| Concurrency | old-attempt callback after rerender | `tests/policy/test_turn_history.py::test_e_rerender_stale_old_attempt_callbacks_rejected` | PASS |
| History | one-turn rewind | `tests/policy/test_turn_history.py::test_e_rewind_one_turn_restores_exact_post_state_finance_untouched` | PASS |
| History | multi-turn rewind | `tests/policy/test_turn_history.py::test_e_rewind_multiple_turns_and_raw_logs_follow_selected_branch` | PASS |
| History | rerender | `tests/policy/test_turn_history.py::test_e_rerender_same_logical_turn_attempt_plus_one` | PASS |
| History | rerender (current !재생성) | `tests/policy/test_turn_history.py::test_e_rerender_command_and_display_entrypoints_use_history_authority` | PASS |
| History | rerender (current !재생성) | `tests/policy/test_wp_g_retirement.py::test_g1_regenerate_is_single_wp_e_rerender_delegate` | PASS |
| History | restart then rerender | `tests/policy/test_turn_history.py::test_e_rerender_after_restart_without_runtime_tx` | PASS |
| History | financial history preserved | `tests/policy/test_billing_policy.py::test_p003_rewind_does_not_reverse_charges` | PASS |
| History | financial history preserved | `tests/defects/test_rewind_accounting.py::test_d005d_provider_history_is_irreversible` | PASS |
| Discord | stream failure | `tests/policy/test_ready_barrier.py::test_tc22_partial_delivery_failure_not_ready` | PASS |
| Discord | stream failure | `tests/policy/test_narration_boundary.py::test_ta11_partial_delivery_preserves_created_ids` | PASS |
| Discord | deleted message during cleanup | `tests/policy/test_message_lifecycle_wpf.py::test_mf08b_clear_is_idempotent_on_already_deleted` | PASS |
| Discord | deleted message during cleanup | `tests/policy/test_turn_history.py::test_e_cleanup_missing_messages_is_idempotent` | PASS |
| Discord | timeout | `tests/policy/test_message_lifecycle_wpf.py::test_mf05_timeout_disables_and_collapses_prompt` | PASS |
| Discord | timeout | `tests/policy/test_ready_barrier.py::test_bc5_timeout_boundary_success_is_observed_and_frozen` | PASS |
| Discord | failed notification | `tests/policy/test_message_lifecycle_wpf.py::test_mf10_failed_commit_emits_no_durable_notice` | PASS |
| Discord | failed notification | `tests/policy/test_wp_e_patch.py::test_ee2b_manual_cleanup_debt_survives_warning_failure_until_surfaced` | PASS |
| Discord | display missing/recreate | `tests/policy/test_message_lifecycle_wpf.py::test_mf02_missing_display_recreated_and_id_persisted` | PASS |
| Discord | clean canonical channel | `tests/policy/test_message_lifecycle_wpf.py::test_committed_turn_leaves_no_transient` | PASS |
| Background | late compression | `tests/policy/test_compression_safety.py::test_cf02a_cf05_append_during_compression_applies_and_keeps_newer_logs` | PASS |
| Background | rewind during compression | `tests/policy/test_compression_safety.py::test_cf03_rewind_is_blocked_while_compressing` | PASS |
| Background | rewind after compression start (restored history) | `tests/policy/test_compression_safety.py::test_cf03_cf04_restored_history_rejects_old_result` | PASS |
| Background | compression cadence (U-2) | `tests/policy/test_compression_safety.py::test_wpg_u2_second_compression_updates_cadence_and_count` | PASS |
| Background | compression is operator-borne (D3) | `tests/policy/test_wp_g_retirement.py::test_d3_fictional_compression_prepayment_retired` | PASS |
| Background | cache create failure | `tests/policy/test_cache_lifecycle.py::test_kf03_failed_create_fabricates_nothing` | PASS |
| Background | cache create failure | `tests/policy/test_cache_finance_policy.py::test_p1c_interpretation_charge_survives_cache_create_failure` | PASS |
| Background | early cache close | `tests/policy/test_cache_finance_policy.py::test_p2_interpretation_non_refundable_on_early_close` | PASS |
| Background | cache expiry | `tests/policy/test_cache_lifecycle.py::test_expiry_path_reaches_finalizer_and_allows_reopen` | PASS |
| Background | refund | `tests/policy/test_cache_lifecycle.py::test_kf05_kf11_kf06_close_settles_storage_and_refund_exactly_once` | PASS |
| Background | refund | `tests/policy/test_cache_finance_policy.py::test_p3_p4_operator_close_refunds_payer_only` | PASS |
| Background | operator-borne shortfall / no player extra charge | `tests/policy/test_cache_finance_policy.py::test_p6_actual_exceeds_prepayment_is_operator_borne` | PASS |
| Background | interpretation strict-fact failure | `tests/policy/test_cache_finance_policy.py::test_p15_strict_costevent_failure_means_no_charge` | PASS |
| Background | auto-GM cost cap (U-3) | `tests/policy/test_wp_g_retirement.py::test_g2_auto_cost_cap_follows_costledger_not_mirror` | PASS |
| Background | auto-GM cost cap fail-closed (U-3) | `tests/policy/test_wp_g_retirement.py::test_g2_cap_fails_closed_on_corrupt_ledger` | PASS |
| Multiplayer | multiple billing users | `tests/policy/test_authoritative_commit.py::test_d_multiplayer_full_nominal_charge_each` | PASS |
| Multiplayer | multiple billing users | `tests/policy/test_ink_transactions.py::test_71_two_users_each_charged_once` | PASS |
| Multiplayer | one account write failure | `tests/policy/test_commit_recovery.py::test_r_user_a_charged_user_b_fails_replay` | PASS |
| Multiplayer | partial recovery | `tests/policy/test_ink_transactions.py::test_72_73_partial_batch_failure_then_retry` | PASS |
| Multiplayer | partial recovery | `tests/policy/test_ink_transactions.py::test_74_partial_ledger_repair_isolated_per_user` | PASS |
| Multiplayer | no duplicate charge | `tests/policy/test_ink_transactions.py::test_63_concurrent_duplicate_charges_once` | PASS |

## 판정 메모

- **인트로 경로**: `preparation=None`으로 공유 엔진을 쓰며 트랜잭션·배리어를 갖지 않습니다(`test_tc28_intro_manual_execute_proceed_has_no_barrier`, `test_ta08_intro_reuse_creates_no_automatic_transaction`). 수동 `!진행`은 은퇴해 이 경로의 호출자는 인트로뿐입니다(`test_c004_execute_proceed_has_two_distinct_callers`).
- **판단·지시 실패**: 판단층위 예외 행은 대기 안내가 남지 않는 것(`mf07`)을 증명합니다. 지시층위 실패 행은 재생성 시도에서 FAILED_SYSTEM·청구 0을 증명합니다. 정상 턴 준비 전 실패의 FAILED_SYSTEM 정산은 `test_d_pre_ready_failure_persists_failed_system_settlement`(전체 회귀 포함)가 증명합니다.
- **누락 사례 없음**: 인계서 §14의 모든 사례에 현재 소스 기준 통과 테스트가 하나 이상 있습니다.
