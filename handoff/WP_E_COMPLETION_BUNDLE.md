# WP-E Completion Bundle — Rewind / Same-Turn Rerender / Canonical History

## 1–5. Identity

| Item | Value |
|---|---|
| Start SHA | `7b1e9ff9280b6cc6206d2effa2dc513426483de7` (WP-D gate patch HEAD) |
| Final SHA | the commit that adds this file — exact value, local==remote proof and literal `git status --short` are in the closure text (a commit cannot contain its own SHA) |
| Branch | `claude/wp-e-rewind-rerender-history` |

## 6. Changed files (vs start SHA)

| File | +/− | Role |
|---|---|---|
| `core/turn_history.py` | NEW (~760) | **Single canonical-history authority**: selection index, attempt records, rewind, rerender begin/abort/settle, restart reconcile |
| `core/commit_coordinator.py` | +34 −8 | Captures `pre`/`post` reversible state + judgment in the existing apply critical section; E-2 step calls `record_commit`; `DerivedEffects.transaction_id/superseded`. **D sequencing unchanged** (PREPARED→SESSION_PERSISTED→REWIND_RECORDED→BILLING_APPLIED→COMMITTED) |
| `core/rewind.py` | +9 −121 | `total_cost` removed from `TRACKED_PATHS` (AUD-029); `rewind_to`/`_set_path`/`_restore_raw_logs` deleted; `available_range` delegates to turn_history. Delta/full-log writers kept as legacy, non-authoritative files |
| `core/__init__.py` | +2 −2 | `rewind_to` export removed; `turn_history` exported |
| `core/cache.py` | +5 | Restart: `turn_history.reconcile` after D `recover_session` + RETRY_PENDING abandonment |
| `core/display.py` | +13 −11 | `disp:restart` = same-turn rerender (was rewind-then-new-declaration) |
| `cogs/gm.py` | +211 −11 | Adapters: `history_rewind`, `rerender_latest`, `_settle_history_op`, `_abort_rerender`, `_fail_rerender_before_narration`, `_cleanup_attempt_output`, `RerenderConfirmView`; `note_judgment` calls; derived game-message ID mapping; superseded output cleanup after COMMITTED; recovery reconcile |
| `cogs/game.py` | +13 −64 | `!재생성` → `rerender_latest` (legacy pop/purge/turn-decrement removed); stale help sentence |
| `cogs/system.py` | +1 −1 | `!명령어` help line for `!재생성` |
| `CLAUDE.md`, `DEVLOG.md` | +1 −1 each | `TRACKED_PATHS` 15 → 14 (verify_docs) |
| `tests/policy/test_turn_history.py` | NEW | 25 WP-E tests (§16 below) |
| `tests/defects/test_rewind_accounting.py` | +43 −39 | d004c → production turn_history test; d005/d005c/d005d flipped to AUD-029 semantics |
| `tests/characterize/test_execute_proceed_shared_paths.py` | +2 −1 | c004c owner set gains `rerender_latest` |

Not touched (verified by `git diff 7b1e9ff --stat`): `core/settlement.py`, `core/ink_transactions.py`,
`core/accounts.py`, `core/cost_ledger.py`, `core/turn_transaction.py`, `core/io.py`, `core/models.py`,
`prompts.py`, `scenarios/*.json`, provider/resilience internals.

## 7. Source map pre → post

| Concern | Pre (7b1e9ff) | Post |
|---|---|---|
| Rewind authority | `core.rewind.rewind_to` — reverse-apply `rewind_log.jsonl` deltas by turn number | `core.turn_history.rewind` — restore the **selected committed attempt's `post` snapshot**; selection index is authority |
| Rewind range | numeric rows of rewind_log (compression rows counted) | selected attempts with records, bounded by degraded gaps and `REWIND_MAX_TURNS=20` |
| Reversible domain | `TRACKED_PATHS` incl. `total_cost` | `REVERSIBLE_FIELDS` (32 game fields); `OPERATIONAL_FIELDS` disjoint (asserted at import) |
| `!재생성` | pop last raw_logs, purge channel, decrement counters, ask again | same logical turn attempt+1 from instruction layer, old selection kept until replacement COMMITTED |
| `disp:restart` | rewind 1 turn + request new declaration | `RerenderConfirmView` → `rerender_latest` |
| Judgment durability | `tx.judgment_result` never populated | `note_judgment` after judgment layer and after roll resolution; persisted in record |
| Output mapping | partial (canonical/media IDs in plan) | + derived game-message IDs (`MESSAGES` events); cleanup scoped per attempt |
| Restart | D recovery only | D recovery → RETRY_PENDING abandon → `turn_history.reconcile` |

## 8. Canonical history schema / API

Files under `sessions/{id}/`:

- `turn_history/{transaction_id}.json` — immutable attempt record, strict atomic write (tmp+fsync+replace):
  `schema_version, session_id, transaction_id, logical_turn, attempt, settlement_id, story_turn, gm_turn,
  plan_fingerprint, player_declaration, judgment{judgment, player_message, roll_results}, pre, post,
  canonical_message_ids, media_message_ids, committed_at, post_digest`.
- `turn_history.jsonl` — append-only (fsync, truncate-on-fail) selection index:
  `SELECT{transaction_id, logical_turn, attempt, gm_turn, story_turn, settlement_id, record, rerenderable, supersedes}`,
  `REWIND{op_id, target_gm_turn, removed[], disposition=REWOUND_BY_PLAYER}`, `MESSAGES{transaction_id, message_ids[]}`.
- `history_op.json` — in-flight history operation intent (`REWIND` or `RERENDER`), for crash reconciliation.
  Kept separate from CommitJournal so commit-recovery phase semantics are not mixed.

`HistoryView` folds the index: `selected[gm_turn]`, `all_attempts[tx]` (superseded/removed attempts remain
historical facts), `messages`, `rewind_ops`, `head`.

API: `capture_reversible / restore_reversible / note_judgment / record_commit / append_messages /
load_record / attempt_message_ids / available_range / rewind / rerender_target / begin_rerender /
abort_rerender / settle_rerender / rerender_op_for / reconcile`.

Pre-E turns: no records → `available_range` stops; rerender rejects with explicit reason (no guessing, no AI
reconstruction). Restart-recovered commits without in-memory payload are selected **degraded**
(`record:false`, gm_turn added to `rewind_degraded_turns`).

## 9. Rewind ordering and crash semantics

```
_lock_for(session) (GM adapter)
→ preconditions (_busy_reason: is_processing, commit_recovery, extraction_pending,
  is_compressing, pending history op, preparation beyond COLLECTING)
→ range check + record present + post_digest verified
→ abort waiting (pre-narration) tx
→ write history_op.json {REWIND, op_id, target_marker, from_marker, removed}
→ io lock: restore target post, clear transient input, trim degraded list, write_session_strict_locked
   (failure → in-memory backup restored, op cleared, original head remains)
→ append REWIND event → clear op
   (failure → commit_recovery HISTORY_REWIND PENDING; restart reconcile finalizes: marker==target_marker ⇒ append REWIND)
→ legacy rewind_log/full_logs truncate (non-authoritative, archive)
→ cleanup bot output of removed attempts (ID-scoped)
```

Restart reconcile for REWIND op: op_id already in index → clear; data.json marker == target → append REWIND;
marker == from → intent discarded; otherwise **block** (`HISTORY_REWIND_CONFLICT`).

## 10. Rerender ordering and atomic supersede

```
_lock_for → rerender_target (head only, record+complete judgment, marker == head tx, not busy/degraded)
→ begin_rerender: attempt counter = max(history attempts); TT.begin_attempt(logical_turn, declaration);
  tx.judgment_result = recorded judgment; write op {RERENDER, old_tx, new_tx}; io lock: restore record.pre
  (+ optional "[재생성 지시] …" addendum in gm_side_note) + strict save
  (so D's baseline marker check matches the pre-turn state)
→ _call_gm_logic(recorded player_message, recorded roll_results, action=PROCEED)   ← instruction layer; judgment NOT called
→ stage_instruction_effects → _finish_proceed_and_continue (normal WP-C/WP-D pipeline, one CommitCoordinator)
→ COMMITTED: record_commit appends SELECT{supersedes=old_tx} and clears op; after COMMITTED, old attempt's
  bot output is deleted (ID-scoped)
→ not committed (FAILED_SYSTEM/ABORTED/instruction failure): settle_rerender → abort_rerender restores old post,
  strict save, op cleared; zero charge (Settlement FAILED path); old output untouched
```

Old attempt stays selected in the index until the replacement is durably COMMITTED. The provisional pre-turn
data.json is covered by `history_op.json`; restart restores the old `post`.

**Defect found and fixed during testing**: if the replacement reached durable COMMITTED but `record_commit`
failed, the runtime settle path would have called `abort_rerender` and reverted a charged story. Now
`abort_rerender` refuses when the new tx is journal-COMMITTED; `settle_rerender` reconciles instead (degraded
SELECT with `supersedes`, old output cleanup); persistent failure blocks (`HISTORY_IO`) without reverting.
Reconcile blocks `HISTORY_RERENDER_CONFLICT` if a COMMITTED replacement is neither selected nor the marker.

## 11. Declaration / judgment preservation evidence

- `test_e_commit_records_selected_attempt_with_pre_post_and_judgment` — record carries `judgment.player_message`.
- `test_e_rerender_same_logical_turn_attempt_plus_one` — `_call_judgment` patched to raise (not called);
  instruction layer receives recorded `player_message`/`roll_results`; `rec_new.player_declaration == rec_old`,
  `rec_new.judgment == rec_old.judgment`, same `logical_turn`, `attempt+1`, identical `pre` (addendum only in `gm_side_note`).
- `test_e_unjudged_turn_is_selected_but_not_rerenderable` — no judgment ⇒ explicit rejection.

## 12. Message mapping / cleanup evidence

Mapping: plan canonical/media IDs (record) + derived game-message IDs (`MESSAGES`). Player messages are never
recorded, so never deleted. Cleanup uses `fetch_message` per ID; missing IDs skipped (idempotent).
Tests: `…rewind_one_turn…` (removed turn output deleted, kept turn output intact), `…attempt_plus_one`
(old deleted only after replacement commit, new kept), `…failure_keeps_old_selection…` (old output kept),
`…cleanup_missing_messages_is_idempotent`, `…crash_after_replacement_commit…` (old output cleaned in settle path).

## 13. Financial irreversibility scan

`core/turn_history.py` imports no settlement/ink/accounts/cost_ledger module; its only finance references are the
`OPERATIONAL_FIELDS` exclusion list and `settlement_id` stored as a link. Rewind/rerender never call
`execute_settlement_charges`, `deduct_ink`, `accrue`, or CostLedger writers. Replacement attempts are billed only by
the normal CommitCoordinator path. Tests assert unchanged balance, InkTransaction rows, CostEvents, old Settlement
equality, `total_cost/total_usd/total_ink_spent` in memory and on disk after rewind; after rerender the old
Settlement is unchanged and exactly one new InkTransaction is appended.

## 14. Stale-task rejection

After `begin_attempt`, the old tx is no longer current: `note_judgment` with the old id is a no-op
(`test_e_rerender_stale_old_attempt_callbacks_rejected`). Existing WP-C guards (`is_current_transaction`,
`superseded_by_newer_attempt`, preparation identity) are unchanged. History ops are rejected while any
preparation is past COLLECTING, extraction pending, or compression running. Cleanup is scoped to the specific
attempt's recorded IDs, so an old cleanup cannot delete new-attempt output.

## 15. Restart / degraded-gap evidence

`test_e_rewind_survives_restart`, `…crash_after_persist_before_index_is_reconciled`,
`…restart_with_rewind_intent_but_unchanged_state_discards_intent`, `…rerender_after_restart_without_runtime_tx`
(attempt = history max+1 with empty runtime counters), `…crash_during_rerender_restart_restores_old_post`,
`…crash_after_replacement_commit_before_history_select`, `…degraded_gap_blocks_rewind_across_it`,
`…corrupt_record_rejects_rewind`.

## 16. Named tests (`tests/policy/test_turn_history.py`, 25)

```
test_e_commit_records_selected_attempt_with_pre_post_and_judgment
test_e_unjudged_turn_is_selected_but_not_rerenderable
test_e_rewind_one_turn_restores_exact_post_state_finance_untouched
test_e_rewind_multiple_turns_and_raw_logs_follow_selected_branch
test_e_rewind_survives_restart
test_e_rewind_rejects_invalid_and_busy_states
test_e_duplicate_and_concurrent_rewind
test_e_rewind_crash_before_strict_save_keeps_original_head
test_e_rewind_crash_after_persist_before_index_is_reconciled
test_e_restart_with_rewind_intent_but_unchanged_state_discards_intent
test_e_degraded_gap_blocks_rewind_across_it
test_e_compression_only_rewind_log_rows_do_not_create_turns
test_e_corrupt_record_rejects_rewind
test_e_rerender_same_logical_turn_attempt_plus_one
test_e_rerender_failure_keeps_old_selection_zero_charge
test_e_rerender_instruction_failure_is_failed_system_zero_charge
test_e_rerender_rejected_for_non_head_or_busy
test_e_concurrent_rerender_creates_single_replacement
test_e_rerender_stale_old_attempt_callbacks_rejected
test_e_rerender_after_restart_without_runtime_tx
test_e_crash_during_rerender_restart_restores_old_post
test_e_crash_after_replacement_commit_before_history_select
test_e_rerender_command_and_display_entrypoints_use_history_authority
test_e_cleanup_missing_messages_is_idempotent
test_e_persistent_history_failure_after_replacement_commit_blocks_not_reverts
```

## 17. Full regression

```
python3 -m pytest tests/ -q
481 passed, 1 xfailed
```
(baseline at start: 454 passed, 3 xfailed)

## 18. Compile / import / routines

- `compileall core cogs main.py prompts.py tools tests` — OK; `import core, cogs.gm, cogs.game, core.display, core.turn_history` — OK
- Routine ① syntax/self-ref — OK · ② re-export / SESSION_FIELDS — OK
- ④ verify_docs — only the **pre-existing** `core 서브모듈 45 ≠ 실제` mismatch remains (deliberately not fixed per instruction); TRACKED_PATHS updated to 14
- ⑤ bot loading — cogs 9 · 명령어 46 · views 5 (baseline)

## 19. Strict xfail mapping

| id | start | end | mapping |
|---|---|---|---|
| d004c (AUD-020) | strict xfail | PASS | replaced by production `turn_history` test (records two commits, rewinds to 1, location from turn-1 extraction preserved); old version modelled pre-D delta timing |
| d005d (AUD-029) | strict xfail | PASS | `total_cost` not in `TRACKED_PATHS`; xfail removed after production change |
| d006e (AUD-034/035) | strict xfail | strict xfail | WP-F (cache accounting) — untouched |

## 20. Preservation evidence

WP-A narration boundary, WP-B preparation, WP-C READY barrier & stale guards, WP-D coordinator/recovery/
Settlement/InkTransaction: all existing suites pass unchanged except the three documented test updates
(c004c owner set, d004c/d005 family). D phase order asserted again in
`test_e_commit_records_selected_attempt_with_pre_post_and_judgment`.

## 21. Untouched WP-F / WP-G scope

No cache/compression accounting change; compression `record_delta` rows (cogs/game.py 1055/1571) remain as
legacy maintenance-overlay rows and are **not** read for selection or range (test). No general message
lifecycle migration, no `!수정` change, no manual-command retirement.

## 22. New findings / deferred risks

1. (fixed) committed-replacement revert defect — §10.
2. Compression runs in the background after a commit: head `post` snapshot predates a later compression, so a
   rewind/rerender restores pre-compression `compressed_memory/uncompressed_logs` while
   `last_compressed_turn`/`compression_count` (operational, not reversible) stay — the next compression cycle
   may be delayed. Same class as legacy behaviour; compression lifecycle is WP-F.
3. Degraded selections (restart-recovered commits) are not rewind targets nor rerenderable by design.
4. `!되감기`/rerender output cleanup deletes only mapped IDs; unmapped transient notices (WaitingStatus etc.)
   remain — WP-F message lifecycle.

## 23. Hard stop

WP-F **NOT STARTED**. Awaiting independent GPT gate.

---

# GATE PATCH — E-E1 ~ E-E3 (independent gate: PATCH REQUIRED)

Parent: `03654fd6ee51cf378d66429b95008251f8fd6614` · branch `claude/wp-e-rewind-rerender-history`.
Final SHA, local==remote and literal clean status: see closure text.
Not redesigned: snapshot rewind authority, financial irreversibility, structured-judgment preservation,
same logical_turn attempt+1, instruction-layer restart, WP-C/WP-D authority.

## Changed files (vs 03654fd)

| File | Change |
|---|---|
| `core/turn_history.py` | record/selection split (`write_attempt_record` / `select_committed` with COMMITTED gate); SELECT carries `message_ids` and supersede cleanup debt; REWIND carries cleanup debt; `CLEANUP_DONE`, `MESSAGES_BEGIN` events; `drain_cleanup`; mapping preservation (`begin_emit`, `record_emitted_messages`, pending file, `try_flush`, `needs_reconcile`); cache provenance (`cache_marker_now`, `_cache_compatible`, `cache_usable`); cache-derived fields reversible; record schema v2 required for restore; `abort_rerender` refuses once replacement story is persisted; `settle_rerender` WAIT; reconcile waits for D recovery, selects COMMITTED-unselected marker deterministically |
| `core/commit_coordinator.py` | E-2 writes attempt record only; new E-5 `select_committed` after durable COMMITTED; `_history_select_block` (HISTORY_SELECT) after runtime finalize / recovery finalize; `DerivedEffects.superseded` removed |
| `cogs/gm.py` | `_drain_history_cleanup` replaces direct deletes (after commit, after rewind, after recovery, at input admission); derived-message BEGIN/mapping preservation; `try_flush` before rewind/rerender; `cache_usable` at instruction, simulation, light-narrate sites |
| `cogs/game.py` | narration `cached_content` only when `cache_usable`; otherwise reissue from restored state (existing pre-emptive path) |
| `core/cache.py` | `update_session_cache_state` stamps `cache_history_marker`, clears stale; restart drains cleanup debt after reconcile |
| `core/models.py`, `core/io.py` | persisted `cache_history_marker`, `cache_history_stale` (SESSION_FIELDS 80 → 82) |
| `CLAUDE.md`, `DEVLOG.md` | SESSION_FIELDS 82 (verify_docs); core-module count left untouched as instructed |
| `tests/policy/test_wp_e_patch.py` | NEW — 17 gate-patch tests |
| `tests/policy/test_turn_history.py` | 4 tests adapted to the split/drain API (semantics strengthened: SELECT-failure now yields non-degraded selection) |
| `tests/defects/test_rewind_accounting.py` | d004c now commits through the real coordinator path (a fake uncommitted plan can no longer be selected) |
| `tests/fakes/bot_fakes.py` | `FakeBot.system_instruction` (real `TRPGBot` attribute) — needed by uncached/reissue paths |

Untouched: `core/settlement.py`, `core/ink_transactions.py`, `core/accounts.py`, `core/cost_ledger.py`,
`core/turn_transaction.py`, `prompts.py`, `scenarios/*.json`.

## E-E1 — selection only after durable COMMITTED

Order in `_complete_from_persisted`:

```
SESSION_PERSISTED → Settlement → E-2 write_attempt_record (pre/post/judgment, strict; NOT a selection)
→ legacy rewind linkage → REWIND_RECORDED → E-3 InkTransaction → BILLING_APPLIED → E-4 COMMITTED
→ E-5 select_committed: SELECT{supersedes, message_ids, cleanup} → clear RERENDER intent
```

- `select_committed` raises unless the CommitJournal already holds COMMITTED for that transaction.
- SELECT failure never undoes COMMITTED: `HISTORY_SELECT` block → reconcile selects deterministically
  (record exists → full, non-degraded selection).
- Billing failure after persist: old attempt stays selected; RERENDER intent kept; `settle_rerender` returns
  WAIT; `abort_rerender` refuses (story persisted); D recovery (in-process or restart) finishes
  BILLING_APPLIED/COMMITTED; SELECT then happens exactly once.
- Reconcile makes no changes while a non-HISTORY D recovery is blocked (`WAIT_COMMIT_RECOVERY`), and
  blocks `HISTORY_RERENDER_AWAIT_COMMIT` rather than reverting a persisted-but-uncommitted replacement.
  This also closes a latent restart case where the previous reconcile could have reverted a
  SESSION_PERSISTED replacement.

## E-E2 — durable output cleanup debt

- Debt shape: `{op_id, reason: RERENDER_SUPERSEDE|REWIND, transaction_ids, message_ids, unmapped}`,
  stored on the same atomic index line as the state transition (SELECT with supersede / REWIND; for
  REWIND also inside `history_op.json`, so a crash before the event is finalized with the same debt).
- `CLEANUP_DONE{op_id}` only when every ID is deleted or already gone (NotFound). Other failures leave the
  debt for retry. IDs belonging to any currently selected attempt are never deleted, even when a stale
  debt lists them. Player messages are never mapped, so they never appear in debt.
- Drain points: after COMMITTED (`_after_commit`), after rewind, after recovery, at restart (after
  reconcile), and at game-channel input admission.
- Derived game messages: `MESSAGES_BEGIN` before sending, `MESSAGES` after. If the index append fails,
  the mapping goes to a strict pending file; if that also fails, it goes to a runtime payload. Either way
  `needs_reconcile()` blocks rewind/rerender until `try_flush` (before each history op, at admission, in
  reconcile/restart) moves it into the index. A BEGIN with no MESSAGES (crash between send and mapping)
  marks the attempt's debt `unmapped`, and the master is told manual cleanup is needed. Gameplay is not
  blocked by a mapping failure.
- Cleanup failure never rolls back story or finance.

## E-E3 — cache history provenance

- `cache_history_marker = {transaction_id, attempt, gm_turn}` is stamped at every cache build
  (`update_session_cache_state` — session open, reissue, restore, `!캐시 재발급`).
- Rewind / rerender-begin evaluate compatibility against the restored state inside the same io-locked
  strict save. The cache counts as compatible only if the marker's gm_turn ≤ the restored gm_turns_done and
  the marker's transaction is the selected attempt at that gm_turn. Unknown provenance counts as
  incompatible. Incompatible ⇒ durable `cache_history_stale=True`.
- `cache_usable()` gates every `cached_content` use: instruction layer, simulation, light narration,
  main narration. The main narration path reissues from the restored canonical state, which clears the
  stale flag and re-stamps the marker. Until then the calls go uncached.
- Uncached prompts also read `cached_compressed_memory`, `cached_session_npcs` and
  `cached_worldview_sections`, so these are now reversible fields restored with the snapshot. Records
  therefore move to schema v2; v1 records (03654fd-era) are refused as restore targets rather than guessed.
- No cache billing/TTL/storage-settlement change (WP-F). d006e remains strict xfail.

## Named regression tests (tests/policy/test_wp_e_patch.py)

```
E-E1
test_ee1_billing_failure_keeps_old_selected_until_inprocess_recovery
test_ee1_billing_failure_then_restart_selects_exactly_once
test_ee1_crash_after_committed_before_select_restart_selects_once
test_ee1_replacement_failed_pre_persist_keeps_old_selection
test_ee1_select_committed_refuses_uncommitted_attempt
(select_timing fixture: every SELECT append observed with journal COMMITTED already durable)
E-E2
test_ee2_rerender_crash_before_old_output_cleanup_restart_resumes
test_ee2_rewind_crash_before_cleanup_restart_resumes
test_ee2_rewind_crash_before_event_restart_finalizes_debt_and_cleans
test_ee2_partial_failures_retry_idempotently_and_never_touch_current
test_ee2_derived_message_mapping_failure_is_not_silently_lost
test_ee2_mapping_total_failure_blocks_with_payload_then_recovers
test_ee2_emit_begin_without_mapping_surfaces_unmapped_debt
E-E3
test_ee3_rewind_blocks_future_cache_and_future_memory        (+ CostEvent/Settlement/Ink unchanged)
test_ee3_rerender_instruction_does_not_read_old_outcome
test_ee3_compatible_cache_kept_after_rewind
test_ee3_unknown_cache_provenance_is_not_trusted
test_ee3_d006e_remains_strict_xfail_for_wp_f
```

## Results

- Existing 25 WP-E tests: kept (4 adapted as listed; all pass)
- d004c / d005d: PASS
- d006e: strict xfail (only remaining xfail)
- Full regression: **498 passed, 1 xfailed, 0 failed, 0 XPASS**
- compileall + import: OK · routines ①② OK · bot load cogs 9 · 명령어 46 · views 5
- verify_docs: only the pre-existing core-module-count mismatch remains (deliberately untouched)
- Scans (§10–§14): `handoff/WP_E_GATE_PATCH_SCAN.txt`. Every SELECT append goes through
  `select_committed`, which has a COMMITTED gate; `turn_history` has no financial mutation or import.

## Hard stop

WP-F **NOT STARTED**.
