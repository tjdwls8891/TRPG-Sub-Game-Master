# WP-C COMPLETION BUNDLE — Concurrent Preparation & READY_TO_COMMIT Barrier

## 0. Canonical current status
- **WP-C: WIRED_NOT_VERIFIED**. Independent GPT gate is pending. This is not a VERIFIED checkpoint.
- **WP-D: NOT_STARTED.** Nothing from WP-D is wired live:
  - no CommitJournal production wiring;
  - no TurnSettlement or InkTransaction live cutover;
  - no authoritative commit;
  - no rewind/rerender change.
- Gate statement supported:
  > The automatic turn now performs required preparation concurrently and joins it at a transaction-owned barrier. READY_TO_COMMIT is reached only after:
  > - required output and state preparation is complete;
  > - the exact turn CostEvent membership is closed;
  > - the attempt is still current;
  > - turn-owned canonical gameplay/narrative commit state remains unapplied.
  >
  > No next automatic turn can begin before that point.
- **Not claimed:** "the turn is authoritatively committed." That remains false until WP-D.

## 1. Package identity
| Item | Value |
|---|---|
| Start SHA | `5991466abdf8c6fe96bebef6517af6aac90f866b` |
| Branch | `claude/wp-c-concurrent-ready-barrier` |
| Internal milestone | `c029f99` (not a VERIFIED checkpoint) |
| Final SHA | the commit that adds this bundle. Reported in the final Claude message together with local==remote proof. |

**User decisions applied (2026-09-25)**

- **B1 — ROLL growth is staged, not an exception.**
  - The dice outcome itself is preserved as a player-visible adjudication input fact.
  - The growth mutation (`players[*].profile` +N, `stat_fail_counts`) is turn-owned canonical state. It is staged and applied only after READY.
  - Later ROLLs in the same transaction, and later instruction/narration prompts, read the staged projection.
  - On a pre-READY failure the growth is discarded.
  - This is recorded as a canonical writer discovered by the fresh WP-C mutation inventory, and it is closed within this package.
- **D2 — prepared effects are applied before the rewind delta.** In the post-READY continuation, this turn's extraction and automatic replan are applied before `record_delta`. The rewind implementation is unchanged. AUD-020 stays open (WP-E).
- **D3 — conversion plus mapping correction.**
  - d002c, d002d and d002e: strict xfail → PASS, with resolution evidence.
  - d002 and d002b: re-characterized to the barrier structure.
  - d002e mapping corrected: AUD-019 → AUD-030 / AUD-031 (PF-22).

## 2. Baseline / final regression
| Run | Result |
|---|---|
| Baseline at start SHA | `339 passed, 6 xfailed, 0 failed, 0 XPASS` |
| Final full suite | `368 passed, 3 xfailed, 0 failed, 0 XPASS, 0 skipped` (371 = 345 + 26 new) |
| Targeted (`test_ready_barrier.py` + `test_turn_commit_races.py`) | `31 passed` |
| Remaining strict xfails | d006e (AUD-034/035), d004c (AUD-020), d005d (AUD-029). All unrelated to WP-C and unchanged. |
| XPASS | 0. The three resolved WP-C xfails were converted with evidence (§16). |
| compileall / `import main` (dummy key) | PASS |

**CLAUDE.md verification routines**
- ① self-refs: OK
- ② re-export / fields: OK
- ⑤ bot load: cogs 9 · commands 46 · views 5, same as baseline.
- ④ `verify_docs`: reports "core 서브모듈 45 ≠ 52". This is **pre-existing** (52 modules at the start SHA; WP-C added no core module) and was left as is (out of scope).

## 3. Changed files
| File | Reason |
|---|---|
| `core/turn_preparation.py` | See below. |
| `core/turn_transaction.py` | `TurnTransaction.preparation` runtime field (not persisted). |
| `core/cost_ledger.py` | `ProviderOperation.event_ids` keeps the ids `record()` actually appended. This is the exact membership source. Observation authority is unchanged. |
| `core/irregular_npc.py` | Read-only staged overlay in `image_path_for` / `voice_for`. Writers never see it. |
| `cogs/game.py` | See below. |
| `cogs/gm.py` | See below. |
| `tests/policy/test_ready_barrier.py` | New WP-C test suite, 26 tests. |
| `tests/fakes/barrier_fakes.py` | Prepared-dispatch fake following the real contract. |
| `tests/defects/test_turn_commit_races.py` | D3 conversion and mapping fix. |
| `tests/characterize/test_auto_turn_orchestration.py` | C-001 re-characterized to the barrier order. |
| `tests/characterize/test_execute_proceed_shared_paths.py` | C-004e caller set. |
| `tests/defects/test_extraction_boundary.py` | D-001 through the prepared launch path. |
| `tests/defects/test_instruction_side_effects.py` | d003c2: applied after READY, not before. |
| `tests/defects/test_rewind_accounting.py` | d004 snapshot line moved into the continuation. |
| `tests/policy/test_billing_policy.py` | p001: no deduction in failure / join code. |
| `tests/policy/test_narrative_replan_plan.py` | nb04b: replan is a registered task with the origin identity. |
| `tests/policy/test_transaction_wiring.py` | w01: ROLL execution also receives the transaction id. |
| `handoff/WP_C_COMPLETION_BUNDLE.md` | This file. |

**`core/turn_preparation.py` adds:**
- `TurnPreparation` registry;
- `PreparationTaskRecord` with independent axes;
- `ReadyProof`;
- `PreparationView` projection;
- cost claim/freeze;
- the single READY predicate and transition;
- staged-apply helpers;
- the growth projection.

**`cogs/game.py` changes:**
- `_execute_proceed(preparation=, on_finalized=)`:
  - stages logs and counters instead of applying them;
  - calls the finalized-narration hook;
  - registers the delivery record;
  - hands the processing/chat unlock to the owner.
- `_generate_narration(transaction_id=)`: narration cost claim, plus a growth projection for the prompt.
- `_deliver_narration`: awaits the staged irregular-NPC task, and takes a display turn number.
- `release_turn_processing` helper.

**`cogs/gm.py` changes:**
- Barrier owner: `_finish_proceed_and_continue`.
- New functions:
  - `_launch_concurrent_preparation`
  - `_prepare_extraction`
  - `_join_and_ready`
  - `_join_after_exception`
  - `_handle_preparation_failure`
  - `_cleanup_failed_delivery`
  - `_post_ready_legacy_continuation`
  - `_apply_prepared_extraction`
  - `_retry_prepared_extraction`
  - `_stage_npc_promotions`
  - `_prepare_auto_replan`
  - `_commit_replan_candidate`
- Preparation modes added to:
  - `_run_extraction` (plus the F1 fix);
  - `_resolve_irregular_npcs`;
  - `_generate_npc_detail`;
  - `_generate_narrative_plan_candidate`;
  - `_update_narrative_progress`.
- Other:
  - cost claims in judgment, instruction, light narrate and simulation;
  - ROLL growth staging;
  - the `_process_actions` guard;
  - retry-view routing.

**Unchanged:**
- `prompts.py` and `scenarios/*`;
- `core/rewind.py`, `cache.py`, `accounts.py`, `settlement.py`, `ink_transactions.py`, `commit_journal.py`, `memory_plan.py`, `io.py`, `prompt.py`, `dialogue.py`, `tts.py` (verified by `git diff --name-only`, §17).

## 4. Pre-edit task/provider inventory (start SHA)
| Operation | Trigger | Launch | tx attribution | Provider/CostEvent | State result | Awaited | Next-turn dependency | WP-C class |
|---|---|---|---|---|---|---|---|---|
| Simulation `_simulate_narrative_directions` | Cache valid | Inline | Copies active tx | TURN_SIMULATION | none | yes | – | AUTOMATIC_TURN member |
| Judgment `_call_judgment` | Every declaration | Inline | copy | TURN_JUDGMENT | none | yes | – | member |
| Instruction `_call_gm_logic` | NARRATE / PROCEED / after ROLL / forced | Inline | copy | TURN_INSTRUCTION | staged (WP-B) | yes | – | member. Manual `!진행` call: MANUAL (not claimed). |
| Light NARRATE `_dispatch_narrate` | NARRATE | Inline | copy | TURN_LIGHT_NARRATE | none | yes | – | member |
| Narration `_generate_narration` | PROCEED | Inline | copy | TURN_NARRATION | none | yes | – | member |
| Cache reissue `caches.create` | Cache missing/expired | Inline | – | **no event** (legacy accrue) | cache infrastructure | yes | – | excluded (AUD-035, WP-F) |
| Irregular NPC `_resolve_irregular_npcs` | During delivery, before stream | Inline | copy | TURN_IRREGULAR_NPC | **registered canonically at once** | yes | images/voice | member, spawns children |
| NPC detail `_generate_npc_detail` | Promotion inside the above | Inline, nested | copy | TURN_NPC_PROFILE | **npcs promoted at once** | yes | – | member (child) |
| Extraction `_run_extraction` | **After delivery completes** | `create_task`, handle dropped | usually **None** (task ran after finalize) | TURN_EXTRACTION | applied inside the task | **no** | next prompt | member, required success |
| Auto replan `_auto_replan_narrative` | Assessment / numeric trigger | `create_task`, handle dropped | usually None | TURN_NARRATIVE_PLANNING | narrative_plan replaced | no | next prompt | member, required terminal |
| Auto compression | Start of `_execute_proceed` | `create_task` | copy_transaction=False | MEMORY_AUTO_COMPRESSION | compressed_memory / uncompressed prefix | no | – | SESSION_BACKGROUND, excluded |
| Runtime TTS | only without `cost_log_prefix` | – | – | TTS_RUNTIME | none | – | – | **not invoked on the automatic path** |
| Local image / speaker image | During delivery | Inline | – | none | none | yes | – | output only |
| `_verify_proceed_instruction` | – | – | – | – | – | – | – | **dead code (0 callers)** |
| Excluded scopes | – | – | – | – | – | – | – | see note below |

Excluded scopes (all excluded by explicit claim, never by timing):
- CACHE_TIME_INTERPRET (session open);
- PROFILE_AI (FREE_FEATURE);
- CHARACTER_DETAIL (manual);
- IMAGE_GENERATION (`!이미지`, may copy an active tx);
- MEMORY_MANUAL_COMPRESSION;
- TTS_TEST / TTS_PRESET_BUILD;
- cache create/delete (no events).

## 5. Pre-edit pre-READY write inventory
**A. Turn-owned canonical — must not apply before READY. All moved or staged.**
- `_execute_proceed`, after generation and before delivery:
  - `raw_logs` ×2 and the 20-entry cap;
  - `uncompressed_logs` ×2;
  - `current_turn_logs.clear()`;
  - `turn_count += 1`.
- Delivery:
  - `irregular_npcs` register / note_appearance;
  - `npcs` promote / rename.
- `_dispatch_proceed`:
  - `apply_narrative_progress`;
  - append to `gm_proceed_history`.
- `_finish…` (already after dispatch):
  - `apply_instruction_effects` (quest_state / info_ledger);
  - the `last_planned_turn` marker set by `_update_narrative_progress`;
  - counters and `gm_turns_done`;
  - rewind delta, full log, `last_recorded_turn`.
- Asynchronous, after the next round: extraction apply, touching:
  - visited, companions, met, timeline, last_extraction;
  - statuses, resources, quest;
  - pending_ending, pending_bgm, main_unlocked_notified, player_faction.
- Asynchronous: replan apply (`narrative_plan`).
- **Newly discovered by the WP-C fresh scan:** at ROLL time, `growth.process_roll_outcome` writes `players[*].profile` and `stat_fail_counts`. Staged per decision B1.

**B. Transaction-local scratch (allowed):**
- `tx.instruction_result`;
- preparation plans and candidates;
- message ids.

**C. Append-only provider facts (allowed):** CostLedger events.

**D. Operational / diagnostic (preserved):**
- `accrue` (total_cost / total_usd), `turn_cost_log`, cost_log files;
- `update_stats` calibration, `last_estimate`, `compression_prepaid_krw`, `last_turn_anchor_id`;
- cache state and reissue;
- the tolerant `save_session_data`;
- declaration-collection `current_turn_logs` appends (turn input);
- quest reader lazy shape normalization (`{}` → default shape);
- consumption of the prior-turn committed `pending_bgm` when BGM starts.

**E. Background:** automatic compression (compressed_memory, uncompressed prefix deletion, compression rewind record).

**F. Setup / admin / manual (separate scope):**
- `!진행`, `!재생성`, `!수정`, `!증감`, `!엔피씨`;
- `!자동 퀘스트`, `!자동 재계획`;
- intro, rewind, the infinity plan.

## 6. Next-round / unlock inventory
| Site | Behavior at start | After WP-C |
|---|---|---|
| `_process_actions` → `get_or_begin_turn_transaction` | the single tx creation site (PF-18) | unchanged, plus a guard: refused while the active tx preparation is PREPARING / READY / RETRY_PENDING |
| `_handle_waiting_response` | session lock; does **not** check `extraction_pending` | unchanged. `gm_waiting_for` is only set by `_start_round`, which now runs after READY. |
| `_handle_player_message` | session lock + extraction_pending check | unchanged. It waits on the lock that the owner holds through READY and the continuation. |
| `_continue_with_roll_results` | session lock + stale guard | unchanged |
| `_finish_proceed_and_continue` → `_start_round` | called while extraction was still running | only after READY + continuation, or after a terminal failure. **Not** called while RETRY_PENDING. |
| `_retry_prepared_extraction` → `_start_round` | – | only after READY + continuation |
| `_init_narrative_and_start` → `_start_round` | setup (no tx) | unchanged |
| `is_processing` / chat unlock | released at the end of `_execute_proceed` (during extraction) | the automatic owner releases it after the barrier and continuation (`processing_held`). Intro/manual unchanged. |
| `chat_guard` | is_processing / extraction_pending | unchanged; now covers the preparation window |

## 7. Task registry design (post-edit map)
The registry follows `OPEN → CLOSING → CLOSED`. `register_task` after the seal raises `BarrierViolationError`.

| Task | Causal identity | Readiness | Cost closure | Children | Terminal handling | CostEvents |
|---|---|---|---|---|---|---|
| `delivery` | prep identity | required success, required player output | no | no | inline record: SUCCEEDED or FAILED | none (local sends) |
| `irregular_npc` | prep identity | must be terminal; failure tolerated (current semantics) | yes | **yes** (`npc_promotion:*`) | asyncio task | TURN_IRREGULAR_NPC |
| `npc_promotion:<name>` | prep identity, parent=irregular_npc | terminal; failure tolerated (no promotion, retry next turn) | yes | no | task | TURN_NPC_PROFILE |
| `extraction` | prep identity | **required success**, plan present, not stale | yes | no | task. Waits for irregular NPC and its children first (input-semantics preservation). | TURN_EXTRACTION |
| `narrative_replan` | prep identity (at registration) | terminal; failure tolerated (existing plan kept) | yes | no | task | TURN_NARRATIVE_PLANNING |
| inline pre-narration ops | claimed ops | awaited inline | yes | – | – | judgment, simulation, instruction, light narrate, narration |

## 8. Concurrency topology
```text
player declaration → tx (_process_actions) → [judgment/sim/instruction/ROLL: inline, claimed; growth staged]
→ _finish_proceed_and_continue (owner, holds session lock)
→ _dispatch_proceed → _execute_proceed: generate → finalized NarrationResult
   → stage logs/counters → on_finalized: register irregular_npc, extraction(awaits irregular+children), narrative_replan
   → register delivery → _deliver_narration (awaits irregular task only, then streams)   ‖ extraction ‖ replan ‖ promotions
→ _join_and_ready: join all → seal → close cost → transition_to_ready (single owner)
→ READY_TO_COMMIT → legacy continuation → finalize(COMMITTED, WP-01 legacy) → save → release processing → display → _start_round
```

## 9. Output / TTS / media classification
- **Text delivery:** required success. A complete delivery is required for READY.
  - Partial or failed delivery → not READY.
  - The already-created canonical and media ids are retained on the tx.
  - `_cleanup_failed_delivery` runs fetch + `core.clear_messages` (idempotent).
  - The failed-turn notice is sent, then FAILED_SYSTEM (MESSAGE_DELIVERY_FAILURE), then the declaration question again.
  - No charge.
- **Automatic runtime TTS:** not invoked. `dub_active` / `dub_task` require `not cost_log_prefix`. Proven by T-C20 (no TTS provider call, no TTS event).
- **Manual/intro TTS:** MANUAL/SETUP scope, never claimed.
- **Local images:** best effort. A missing file is only a master-channel warning (T-C23). Ids go to `media_message_ids`. No CostEvent (T-C21).
- **Irregular NPC image/voice:** staged plan plus a read-only overlay, so the stream shows this turn's assignment (T-C08) while `irregular_npcs` stays unchanged.

## 10. State-preparation join
| Result | Staged in | Applied in post-READY continuation |
|---|---|---|
| Instruction effects | `tx.instruction_result` (WP-B) | `apply_instruction_effects` |
| Extraction | `prep.extraction_plan` (validated ExtractionMutationPlan, built from a projection view) | `_apply_prepared_extraction` (idempotent by tx) |
| Irregular NPCs | `prep.irregular_plan` and `prep.promotions` | `_apply_irregular_npc_plan(staged_promotions=…)` (no provider call) |
| Replan | `prep.replan_candidate` and `prep.narrative_marker` | `_commit_replan_candidate` (stale / idempotent rule shared with WP-B) |
| Logs / counters / history / progress | `prep.staged_log`, `proceed_history_entry`, `narrative_progress` | staged apply helpers |
| ROLL growth | `prep.growth_players`, `growth_fail_counts`, `growth_events` | `apply_staged_growth` |

**Input-semantic equivalence.** Pre-READY providers read `PreparationView` projections:
- The extraction prompt and plan see:
  - the projected quest state (staged instruction effects);
  - this turn's staged irregular registrations;
  - the staged promotions.
- The planner sees:
  - staged `raw_logs` including this turn's narration;
  - staged turn N (T-C09).
- The narration and instruction prompts see the projected players after staged growth.

## 11. Cost membership closure
- Automatic-path operations are claimed explicitly (`claim_cost_operation`). Manual calls (transaction_id=None) are no-ops.
  - A claim whose `op.transaction_id` differs is recorded as a violation and is **not** added.
  - A claim after the freeze raises before the provider call.
- The freeze (`close_cost_membership`) requires:
  - every `blocks_cost_closure` task is terminal;
  - the registry is CLOSED.
  - It then builds the tuple of `op.event_ids` in claim order, which are the exact ids appended to the ledger.
  - No time window and no cost-value inference.
- Excluded, and proven by T-C17/T-C19:
  - background compression (tx None; READY is not delayed even while compression is gated);
  - a manual `_call_gm_logic` made while the tx is active;
  - local media.
- Retry preservation (T-C11/T-C27):
  - the failed attempt has no usage and gets no event;
  - the succeeding attempt is recorded with `provider_attempt=2` and `turn_attempt=1`.
- On terminal failure (generation, delivery, not-ready, superseded) membership is frozen too (FAILED_SYSTEM → zero charge in WP-D).
- RETRY_PENDING keeps membership OPEN for the same-tx retry.
- No Settlement build, no Ink calculation, no pricing change.

## 12. READY proof
- There is a single transition, `core.turn_preparation.transition_to_ready`. It is the only `READY_TO_COMMIT` assignment in the repository (§17 scan).
- `evaluate_ready` checks:
  - **identity:** active tx == prep, status not terminal or already READY, and preparation ownership;
  - **narrative:** finalized narration present and log staged;
  - **output:** delivery ok;
  - **state:** extraction plan present and not stale;
  - **tasks:** every `blocks_preparation_ready` task terminal, every `required_success` task succeeded, and no foreign-identity task;
  - **effects:** no staged effect already applied;
  - **registry:** CLOSED;
  - **cost:** CLOSED with frozen ids;
  - **violations:** none.
- `ReadyProof` captures:
  - identity;
  - task states;
  - the frozen ids;
  - the baseline and READY canonical fingerprints;
  - `canonical_unchanged` / `changed_domains`;
  - the delivered ids;
  - the extraction entry count.
- The fingerprint covers these reviewed domains:
  - quest_state (shape-normalized), info_ledger;
  - resources, statuses, world_timeline, visited_places;
  - companions, met_npcs, irregular_npcs, npcs;
  - narrative_plan, pending_ending, last_extraction;
  - players, stat_fail_counts;
  - turn_count, gm_turns_done, last_recorded_turn, gm_proceed_history, raw_logs.
- A domain change caused by a legitimate admin write is logged as evidence and does not block.

## 13. Failure disposition
| Case | READY | Canonical | Charge | Next round | tx |
|---|---|---|---|---|---|
| Generation fails (F-C01) | no; no tasks launched | unchanged | no | yes (declaration question) | FAILED_SYSTEM |
| Delivery fails immediately (F-C02) | no; running extraction is joined and its event retained | unchanged | no | yes | FAILED_SYSTEM |
| Partial delivery (T-C22 / F-C03) | no; ids retained, cleanup idempotent | unchanged | no | yes | FAILED_SYSTEM (MESSAGE_DELIVERY_FAILURE) |
| Extraction exhausted (T-C12 / F-C04) | no | unchanged (growth, logs and counters discarded unless retried) | no | **no** (RETRY_PENDING, retry button) | active, non-terminal |
| Retry succeeds | yes, same tx | applied after READY | legacy, after READY | yes | COMMITTED (legacy) |
| Retry with no live preparation (restart) | – | untouched (block released only) | no | – | – |
| Stale / superseded (T-C13 / F-C11) | no | untouched; no session block | no | not by this flow | the newer tx is untouched |
| Unexpected pre-READY exception | no; joined, sealed, frozen | untouched | no | yes | FAILED_SYSTEM |
| Exception inside the post-READY continuation | already READY | partial (not crash-safe) | – | – | FAILED_SYSTEM, stage POST_READY_CONTINUATION. WP-D recovery follow-through. |

## 14. Post-READY legacy continuation
`_post_ready_legacy_continuation` asserts that a `ready_proof` exists before it mutates anything.

Order:
1. logs / counters;
2. ROLL growth;
3. irregular NPC register / promote;
4. proceed history;
5. progress;
6. instruction effects;
7. replan marker;
8. **extraction;**
9. **replan;**
10. gm counters;
11. rewind delta, legacy `deduct_ink`, full log;
12. `finalize(COMMITTED)` (existing WP-01 transitional metadata; the barrier itself never sets COMMITTED);
13. save.

This is labelled **LEGACY POST-READY CONTINUATION (pre-WP-D)**. It is not authoritative commit and it makes no CommitJournal, Settlement or Ink calls.

## 15. Preservation evidence (P-C01 … P-C22)
| ID | Evidence |
|---|---|
| P-C01 transaction identity | `get_or_begin` is unchanged and remains the only creation site.<br>Provider retry does not create an attempt (T-C11).<br>The ROLL View carries the id to `_execute_rolls` and to the continuation (w01–w04).<br>Stale ROLL continuation is still rejected (w04). |
| P-C02 finalized narration semantics | `_generate_narration` body is unchanged apart from the claim and the growth-projection prompt view.<br>ta04/ta04b/ta05/ta06 pass. |
| P-C03 delivery ownership | ids are attached to the tx; partial ids are retained (ta10/ta11/m1–m5, T-C22). |
| P-C04 WP-B preparation authority | Extraction, irregular and replan paths stay plan-only; pa_a–d, b17–b21, ib01–06 and nb01–09 pass. |
| P-C05 merged-status validation | normalizers are unchanged; the validation run inside the extraction plan build still passes. |
| P-C06 full extraction narration | d001, d001b, d001c, d001d. The same `narr.text` feeds extraction and delivery. |
| P-C07 irregular NPC authority | registration/promotion is plan-only (ib01–06, T-C08). |
| P-C08 replan authority | shared `_commit_replan_candidate` stale/idempotent rule (nb04/nb05/nb05b, T-C09). |
| P-C09 CostEvent observation | no new provider instrumentation; `record()` unchanged apart from keeping ids. Shadow tests pass. |
| P-C10 retry semantics | T-C11 and `test_retry_then_success_numbers_attempt`. |
| P-C11 no fabricated usage | a failed attempt produces no event (T-C11). |
| P-C12 pricing basis | untouched (`pricing_basis` tests pass). |
| P-C13 stale authority | reuses `superseded_by_newer_attempt` / `extraction_is_stale`; no second owner. |
| P-C14 intro/manual isolation | `preparation=None` path unchanged (T-C28, ta08, ta09, m3). |
| P-C15 compression scope | still SESSION_BACKGROUND; not joined; excluded (T-C19). |
| P-C16 legacy billing | same formula and authority, called only after READY (T-C05, p001). |
| P-C17 CommitJournal foundation-only | zero live callers (§17 scan, T-C26). |
| P-C18 rewind/rerender | `core/rewind.py` unchanged; the snapshot carry-forward is kept (d004, d004b). |
| P-C19 prompts/scenario/data | no diff (§17). |
| P-C20 strict xfail | 0 XPASS. The three converted tests are documented here. |
| P-C21 WP-B findings | AUD-001/005/011/024/065 tests pass. |
| P-C22 successful gameplay | T-C05, T-C08, T-C09, T-C10 and B1 show current success semantics after READY, including input equivalence of prompts. |

## 16. Findings status
| Finding | Status | Evidence / note |
|---|---|---|
| AUD-012 | **RESOLVED_IN_CODE (barrier/concurrency part)** | T-C02/03/04; d002b/d002c converted to PASS |
| AUD-023 | **RESOLVED_IN_CODE** | extraction launched at finalized narration and overlapping the stream (T-C02, T-C02b wall-clock) |
| AUD-019 | PARTIAL / FOLLOW-THROUGH | no next automatic turn before READY and the legacy continuation (T-C03/T-C24, d002c). Durable unlock authority → WP-D. |
| AUD-021 | PARTIAL / FOLLOW-THROUGH | exact same-turn membership is frozen and includes extraction, replan and NPC costs (T-C17). Settlement and charging → WP-D. |
| AUD-020 | OPEN (WP-E) | d002d shows that same-turn extraction now lands before the delta (barrier/order evidence only). d004c is still a strict xfail. |
| AUD-030 / AUD-031 | OPEN (WP-D) with WP-C barrier evidence | d002e now PASS: a pre-READY failure performs no successful-turn deduction. **Mapping correction:** the historical xfail reason "AUD-019" was wrong metadata (PF-22). |
| AUD-022 / 029 / 034 / 035 | OPEN by scope | unchanged |
| **New — ROLL growth canonical writer** | **RESOLVED_IN_CODE in WP-C (B1)** | Found by the WP-C fresh mutation inventory: `growth.process_roll_outcome` wrote `players.profile` and `stat_fail_counts` before READY. Now staged, projected and applied after READY; discarded on pre-READY failure (B1 tests). |
| **New F1 — extraction `attempt` shadowing** | **RESOLVED_IN_CODE** | `for attempt in range(...)` shadowed the tx attempt used by the stale check and plan identity. Renamed to `_try`. |
| New F2 — `m_send` undefined in the overdraft branch | OPEN, reported → WP-D | Legacy billing: a NameError is swallowed and the remaining players' deduction is skipped. Preserved verbatim; billing authority is WP-D. |
| New F3 — post-ROLL / forced-PROCEED instruction not staged | OPEN, reported | `_continue_with_roll_results` and `_forced_proceed_instruction` never call `stage_instruction_effects`, so quest_choice etc. from those decisions is dropped (possible WP-B-era behavior loss). Not changed. |
| New F4 — manual commands copy the active tx into CostEvents | OPEN, reported | Excluded from membership by explicit claim; the attribution fix is later. |
| New F5 — `call_with_retry` timeout leaves the provider thread running | OPEN residual | Possible billed usage with no event; not observable by the barrier. |
| New F6 — replan numeric trigger reads the prior turn's `last_extraction` | OPEN, needs intent | Current behavior preserved. |
| Residual — quest `min_stat` filters | note | Read canonical profile; a same-turn staged growth becomes visible to quest filters on the next turn. |
| Residual — rewind button during RETRY_PENDING | note | `is_processing` is released while waiting for a retry, so rewind is possible then (same as before). Rewind authority → WP-E. |
| Residual — restart during RETRY_PENDING | note | The runtime preparation is lost; the retry only releases the block. Durable recovery → WP-D. |
| Pre-existing — `verify_docs` core module count 45 ≠ 52 | note | Present at the start SHA; out of scope. |

## 17. Provider / caller / write scans (post-change, literal output)
```text
== 1/2 create_task sites in cogs/gm.py (no extraction/replan fire-and-forget)
cogs/gm.py:891:        asyncio.create_task(core.play_dice_sfx(self.cog.bot, interaction.guild))
cogs/gm.py:900:        asyncio.create_task(self._process_roll(interaction.channel))
cogs/gm.py:908:        asyncio.create_task(
cogs/gm.py:928:        asyncio.create_task(
cogs/gm.py:1042:        asyncio.create_task(self._init_narrative_and_start(session))
cogs/gm.py:1189:            asyncio.create_task(self._handle_waiting_response(session, message, char_name))
cogs/gm.py:1192:            asyncio.create_task(self._handle_player_message(session, message))
cogs/game.py:75:        session.gm_typing_task = self.bot.loop.create_task(typing_sync_task())
cogs/game.py:310:            asyncio.create_task(
cogs/game.py:878:                        dub_task = asyncio.create_task(self._synthesize_and_enqueue(session, tts_texts))
cogs/game.py:1189:                next_task = asyncio.create_task(_synth(items[i + 1][2]))
core/turn_preparation.py:932:            rec.task = _asyncio.ensure_future(self._run(rec, coro))
== 3/4 provider begin_operation sites + claim
cogs/game.py:638:        _narr_op = core.cost_ledger.begin_operation(
cogs/game.py:643:        core.turn_preparation.claim_cost_operation(session, transaction_id, _narr_op)
cogs/game.py:989:            _cl_op = core.cost_ledger.begin_operation(
cogs/game.py:1554:            _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:2385:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:2390:        core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
cogs/gm.py:2559:            _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:2564:            core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
cogs/gm.py:2984:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:2989:        core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
cogs/gm.py:3268:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:3361:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:3366:            preparation.claim_cost_operation(_cl_op)
cogs/gm.py:3518:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:3523:            preparation.claim_cost_operation(_cl_op)
cogs/gm.py:3925:        _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:3930:            preparation.claim_cost_operation(_cl_op)
cogs/gm.py:4093:            _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:4228:            _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:4233:            core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
cogs/gm.py:4851:            _cl_op = core.cost_ledger.begin_operation(
cogs/gm.py:4856:                preparation.claim_cost_operation(_cl_op)
cogs/media.py:100:                _cl_op = core.cost_ledger.begin_operation(
core/profile_ai.py:93:    _cl_op = cost_ledger.begin_operation(
core/turn_preparation.py:1087:    return prep.claim_cost_operation(op)
core/utils.py:238:    _cl_op = cost_ledger.begin_operation(
== 5 WP-B compat appliers — call sites
cogs/gm.py:1752:        TP.apply_staged_narration_log(session, prep.staged_log)
cogs/gm.py:1753:        TP.apply_staged_growth(session, prep)
cogs/gm.py:1757:                await self._apply_irregular_npc_plan(
cogs/gm.py:1774:            TP.apply_narrative_progress(session, prep.narrative_progress)
cogs/gm.py:1777:            _applied = TP.apply_instruction_effects(session, TP.pending_for(session))
cogs/gm.py:1794:        await self._apply_prepared_extraction(session, prep)
cogs/gm.py:1798:                await self._commit_replan_candidate(
cogs/gm.py:1869:        await self._apply_extraction_plan(session, plan, _mch)
cogs/gm.py:3425:        return await self._apply_npc_promotion(session, name, norm, master_ch)
cogs/gm.py:3604:        return await self._apply_irregular_npc_plan(session, plan, text, master_ch)
cogs/gm.py:3671:                        await self._apply_npc_promotion(
cogs/gm.py:4006:        #  · 적용은 _apply_extraction_plan(LegacyCompatibilityApplier)이 계획의
cogs/gm.py:4062:        await self._apply_extraction_plan(session, plan, master_ch)
cogs/gm.py:4575:        return await self._apply_narrative_plan(session, candidate)
cogs/gm.py:4593:        return await self._commit_replan_candidate(
cogs/gm.py:4621:        return await self._apply_narrative_plan(session, candidate)
== 6 log/counter writers
cogs/game.py:433:                session.raw_logs.extend(_raw_entries)
cogs/game.py:434:                session.uncompressed_logs.extend(_unc_entries)
cogs/game.py:436:                session.turn_count += 1
cogs/game.py:1049:                    core.record_delta(
cogs/game.py:1356:            session.turn_count -= 1
cogs/game.py:1616:                    core.record_delta(
cogs/gm.py:1806:        session.gm_turns_done += 1
cogs/gm.py:1833:                core.record_delta(session, turn_no, changes, cost_krw=turn_cost)
cogs/gm.py:1835:                core.record_full_log(
cogs/session.py:508:        session.raw_logs.append(types.Content(role="user", parts=[types.Part.from_text(text="[세션 시작]")]))
cogs/session.py:509:        session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=start_text)]))
cogs/session.py:511:        session.uncompressed_logs.append(f"[세션 시작 묘사]: {start_text}")
core/turn_preparation.py:1251:    session.raw_logs.extend(staged["raw_entries"])
core/turn_preparation.py:1252:    session.uncompressed_logs.extend(staged["uncompressed_entries"])
core/turn_preparation.py:1256:    session.turn_count += 1
== 7 next-turn / tx creation
cogs/gm.py:1412:        tx = core.turn_transaction.get_or_begin_turn_transaction(session, player_message)
cogs/gm.py:1522:            await self._start_round(session)
cogs/gm.py:1917:                await self._start_round(session)
cogs/gm.py:1924:        PROCEED 완료 후 자동으로 _start_round()를 호출하여 다음 라운드(선제 행동 질문)를 시작.
cogs/gm.py:4451:        await self._start_round(session)
== 12 WP-D live callers (expect none)
(none)
== 13 READY_TO_COMMIT assignment sites
cogs/gm.py:1432:             → 단일 READY_TO_COMMIT 전이(증명 캡처, 턴 소유 정본은 아직 미적용)
cogs/gm.py:1525:    # WP-C — 동시 준비 / READY_TO_COMMIT 배리어 / legacy continuation
cogs/gm.py:1639:                    print(f"[WP-C/{prep.transaction_id[:8]}] READY_TO_COMMIT "
cogs/gm.py:1740:        READY_TO_COMMIT 증명이 캡처된 '이후'에만 스테이징 효과를 정본에 적용하고 기존
core/turn_preparation.py:14:    · 권위적 commit / READY_TO_COMMIT / CommitJournal·Settlement·InkTransaction 배선(WP-C/D).
core/turn_preparation.py:699:#  WP-C — 트랜잭션 소유 동시 준비(Concurrent Preparation) + READY_TO_COMMIT 배리어
core/turn_preparation.py:703:#  READY_TO_COMMIT에 도달한다.
core/turn_preparation.py:705:#  READY_TO_COMMIT은 authoritative commit이 아니다:
core/turn_preparation.py:757:PREP_READY = "READY"                    # READY_TO_COMMIT 도달(증명 보유)
core/turn_preparation.py:791:    """READY_TO_COMMIT 전이 시점에 캡처되는 불변 증명(§26/§27)."""
core/turn_preparation.py:1164:    """READY_TO_COMMIT 조건을 검사하고 미충족 사유 목록을 반환한다(비면 충족)."""
core/turn_preparation.py:1170:    elif _tx.is_terminal(tx.status) or tx.status == _tx.TurnStatus.READY_TO_COMMIT:
core/turn_preparation.py:1210:    """유일한 READY_TO_COMMIT 전이 경로. 증명 객체 없이 상태만 바꾸지 않는다.
core/turn_preparation.py:1236:                                _tx.TurnStatus.READY_TO_COMMIT)
== 14 COMMITTED sites
cogs/gm.py:1847:            session, tid, core.turn_transaction.TurnStatus.COMMITTED)
core/turn_transaction.py:53:    TurnStatus.COMMITTED,
core/commit_journal.py:38:#  주의(§6.1/§12/§19): 저널 COMMITTED 는 WP-01 TurnStatus.COMMITTED 와 다르다.
== 15 prompts/scenarios/rewind/cache/compression/accounts diff vs start
(end)
== changed files vs start
M	cogs/game.py
M	cogs/gm.py
M	core/cost_ledger.py
M	core/irregular_npc.py
M	core/turn_preparation.py
M	core/turn_transaction.py
M	tests/characterize/test_auto_turn_orchestration.py
M	tests/characterize/test_execute_proceed_shared_paths.py
M	tests/defects/test_extraction_boundary.py
M	tests/defects/test_instruction_side_effects.py
M	tests/defects/test_rewind_accounting.py
M	tests/defects/test_turn_commit_races.py
A	tests/fakes/barrier_fakes.py
M	tests/policy/test_billing_policy.py
M	tests/policy/test_narrative_replan_plan.py
A	tests/policy/test_ready_barrier.py
M	tests/policy/test_transaction_wiring.py
```
Interpretation:
- Non-preparation branches (`_resolve_irregular_npcs` at game.py delivery, `_run_extraction` in the retry view, `_generate_npc_detail` in `_apply_irregular_npc_plan`) are reached only from intro/manual paths or the legacy retry context.
- `_auto_replan_narrative` has no production caller. It is kept as the WP-B compatibility rule, which is shared through `_commit_replan_candidate`.
- `_verify_proceed_instruction` is dead code at the start SHA and remains unclaimed.

## 18. Forbidden-scope scans
- CommitJournal, Settlement, InkTransaction and strict-save live callers in `cogs/` and `main.py`: **none** (scan 12, T-C26).
- `TurnStatus.COMMITTED` is set only by the existing WP-01 `finalize` inside the legacy continuation. `core/turn_preparation.py` contains no COMMITTED.
- No diff in:
  - prompts, scenarios;
  - rewind, cache, compression (memory_plan), accounts;
  - settlement, ink, journal;
  - io, prompt, dialogue, tts (scan 15).
- No message-lifecycle redesign. Only the existing WP-A `clear_messages` is used on the delivery-failure path.

## 19. Source excerpts (S1–S12, extracted from the committed tree)

### S1 — finalized narration → staged logs + concurrent launch (same `narr` feeds delivery and extraction)

`cogs/game.py:404-444`
```python
            # === canonical 턴 확정 로그/카운터 ===
            turn_history_text = "\n".join(session.current_turn_logs) + f"\n[GM 지시]: {clean_instruction}"
            _raw_entries = [
                types.Content(role="user", parts=[types.Part.from_text(text=turn_history_text)]),
                types.Content(role="model", parts=[types.Part.from_text(text=full_ai_response)]),
            ]
            _unc_entries = [f"[플레이어 및 GM]: {turn_history_text}",
                            f"[GM 묘사]: {full_ai_response}"]
            if preparation is not None:
                # WP-C: 자동 턴은 로그/카운터를 READY 이후 legacy continuation에서 적용한다.
                #   소비한 current_turn_logs 개수만 기록해, 대기 중 추가된 입력 로그는 보존한다.
                preparation.narration = narr
                preparation.staged_log = {
                    "raw_entries": _raw_entries,
                    "uncompressed_entries": _unc_entries,
                    "consumed_turn_logs": len(session.current_turn_logs),
                    "turn_no": int(session.turn_count) + 1,
                    "applied": False,
                }
                display_turn = int(session.turn_count) + 1
                # 확정 묘사가 고정된 직후 — 동시 준비 작업(비정규 NPC·추출·재계획) 발사.
                if on_finalized is not None:
                    await on_finalized(narr)
                preparation.register_task(
                    "delivery", core.turn_preparation.TASK_DELIVERY,
                    required_success=True, required_player_output=True,
                    blocks_cost_closure=False, turn_cost_membership=False)
            else:
                # 인트로·수동: 기존 timing/순서 그대로 즉시 적용.
                session.raw_logs.extend(_raw_entries)
                session.uncompressed_logs.extend(_unc_entries)
                session.current_turn_logs.clear()
                session.turn_count += 1
                if len(session.raw_logs) > 20:
                    session.raw_logs = session.raw_logs[-20:]
                display_turn = session.turn_count

            # 출력(타이핑 연출) 시작 직전 대기 안내 메시지 제거 (기존 순서 보존)
            _transient_ids = []
            if status_msg is not None and getattr(status_msg, "message", None) is not None:
                _transient_ids.append(status_msg.message.id)
```

`cogs/gm.py:1543-1571`
```python
    async def _launch_concurrent_preparation(self, session, prep, narr, master_ch) -> None:
        """확정 묘사가 고정된 직후 — 동시 준비 작업을 등록·발사한다(S1).

        같은 확정 묘사 페이로드가 전달·추출·비정규 NPC 분석에 들어간다(재생성 없음).
          · 비정규 NPC 배정: 스트리밍 전에 전달이 결과(스테이징)만 기다린다. 승격 세부
            생성은 자식 작업으로 등록된다(부모 종결 전 등록).
          · 추출: 비정규 NPC 준비(자식 포함)를 기다린 뒤 투영 입력으로 시작 — 스트리밍과 겹친다.
          · 자동 재계획: 트리거되면 등록 작업으로 스트리밍·추출과 겹친다.
        """
        core.turn_transaction.mark_transaction_status(
            session, prep.transaction_id,
            core.turn_transaction.TurnStatus.STREAMING_EXTRACTING)
        _mch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        TP = core.turn_preparation
        if narr.paragraphs:
            prep.register_task(
                "irregular_npc", TP.TASK_IRREGULAR_NPC,
                coro=self._resolve_irregular_npcs(
                    session, narr.narrative_text, _mch, preparation=prep),
                may_spawn_required_child_work=True)
        prep.extraction_text = narr.text
        prep.register_task(
            "extraction", TP.TASK_EXTRACTION,
            coro=self._prepare_extraction(session, prep, narr.text, _mch),
            required_success=True)
        if prep.event_assessment is not None:
            await self._update_narrative_progress(
                session, prep.event_assessment, master_ch, preparation=prep)

```

### S2 — task registry (registration, seal, join with child pickup)

`core/turn_preparation.py:915-988`
```python
    def register_task(self, name, kind, *, coro=None, **axes) -> PreparationTaskRecord:
        """필수 턴 작업을 등록한다. 봉인 이후 등록은 배리어 위반(예외)."""
        if self.registry_state != REGISTRY_OPEN:
            if coro is not None:
                coro.close()
            self.violations.append(f"late_task:{name}")
            raise BarrierViolationError(
                f"레지스트리 봉인 이후 필수 작업 등록 시도: {name} (tx={self.transaction_id})")
        if name in self.tasks and not self.tasks[name].terminal:
            if coro is not None:
                coro.close()
            raise BarrierViolationError(f"같은 이름의 작업이 이미 실행 중: {name}")
        rec = PreparationTaskRecord(
            name=name, kind=kind, transaction_id=self.transaction_id,
            logical_turn=self.logical_turn, attempt=self.attempt, **axes)
        self.tasks[name] = rec
        if coro is not None:
            rec.task = _asyncio.ensure_future(self._run(rec, coro))
        return rec

    async def _run(self, rec, coro):
        try:
            rec.result = await coro
            if rec.state == TASK_RUNNING:
                rec.state = TASK_SUCCEEDED
        except _asyncio.CancelledError:
            rec.state = TASK_CANCELLED
            rec.error = "cancelled"
        except Exception as e:  # 작업 실패는 기록만(분류에 따라 READY 판단)
            rec.state = TASK_FAILED
            rec.error = f"{type(e).__name__}: {e}"
            print(f"[WP-C/{self.transaction_id[:8]}] 준비 작업 실패 {rec.name}: {rec.error}")
        return rec.result

    def mark_task(self, name, state, *, result=None, error=None) -> None:
        rec = self.tasks.get(name)
        if rec is None:
            return
        rec.state = state
        if result is not None:
            rec.result = result
        if error is not None:
            rec.error = error

    def task_future(self, name):
        rec = self.tasks.get(name)
        return rec.task if rec is not None else None

    def running_tasks(self) -> list:
        return [r for r in self.tasks.values() if not r.terminal]

    async def join(self) -> None:
        """등록된 모든 작업이 terminal이 될 때까지 기다린다.

        부모 작업이 실행 중 자식 작업을 등록하면 다음 반복에서 함께 기다린다
        (부모가 terminal이 된 뒤에도 자식 등록이 선행되므로 누락이 없다).
        """
        while True:
            pending = [r.task for r in self.tasks.values()
                       if not r.terminal and r.task is not None]
            if not pending:
                break
            await _asyncio.wait(pending)
        for r in self.tasks.values():
            if not r.terminal:     # task 없이 인라인 관리되는 기록이 비종결로 남음
                self.violations.append(f"unterminated:{r.name}")

    def seal(self) -> None:
        """레지스트리 봉인: OPEN → CLOSING → CLOSED. 이후 등록은 위반."""
        self.registry_state = REGISTRY_CLOSING
        if any((not r.terminal) for r in self.tasks.values()):
            self.violations.append("seal_with_running_task")
        self.registry_state = REGISTRY_CLOSED

```

### S3 — extraction join; preparation mode stages plan only

`cogs/gm.py:1572-1595`
```python
    async def _prepare_extraction(self, session, prep, text, master_ch):
        """추출 준비 작업(result-only). 비정규 NPC 준비(승격 자식 포함) 완료 후 시작한다.

        기존 흐름에서 추출은 비정규 등록·승격 이후에 돌았으므로, 같은 입력 의미(등록/승격
        NPC 목록·지시효과 quest 투영)를 투영 뷰로 보존한다. 계획은 준비 객체에만 저장된다.
        """
        TP = core.turn_preparation
        irr = prep.task_future("irregular_npc")
        if irr is not None:
            await asyncio.wait([irr])
        while True:
            kids = [r.task for r in prep.tasks.values()
                    if r.kind == TP.TASK_NPC_PROMOTION and r.task is not None and not r.terminal]
            if not kids:
                break
            await asyncio.wait(kids)
        plan = await self._run_extraction(
            session, text, master_ch,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if plan is None:
            raise RuntimeError("추출층위 실패(재시도 소진) 또는 stale")
        return plan

```

`cogs/gm.py:4022-4036`
```python
            # WP-C 준비 모드: 계획만 스테이징(정본 미적용). stale이면 현재 배리어를 만족 못함.
            if (not preparation.matches(core.turn_transaction.get_active_transaction(session))
                    or core.turn_preparation.extraction_is_stale(
                        session, logical_turn=logical_turn, attempt=attempt)):
                plan.rejected_stale = True
                print(f"[추출/{session.session_id}] stale 준비 결과 — 배리어 미충족 "
                      f"(tx={transaction_id}, lt={logical_turn}, at={attempt})")
                return None
            core.write_log(
                session.session_id, "api",
                f"[추출층위 결과(준비·미적용)]\n{json.dumps(result, ensure_ascii=False, indent=2)}")
            preparation.extraction_plan = plan
            return plan

        if core.turn_preparation.extraction_is_stale(
```

### S4 — irregular NPC nested child + replan registration (origin identity)

`cogs/gm.py:3606-3644`
```python
    def _stage_npc_promotions(self, session, prep, plan, text, master_ch=None) -> list:
        """WP-C: 승격 판정을 투영으로 재현하고 세부 생성을 자식 준비 작업으로 등록한다.

        기존 _apply_irregular_npc_plan의 순서(등록 → 등장 누적 → should_promote → 세부
        생성)를 정본 변경 없이 투영 등록부에서 계산한다. 자식은 부모(배정) 작업이
        끝나기 전에 등록되므로 배리어 합류가 누락하지 않는다.
        """
        TP = core.turn_preparation
        IRR = core.irregular_npc
        turn = plan.registrations[0]["turn"] if plan.registrations else (
            prep.staged_log["turn_no"] if isinstance(prep.staged_log, dict)
            else getattr(session, "turn_count", 0))
        reg = copy.deepcopy(IRR.get_registry(session))
        for r in plan.registrations:
            if r["name"] not in reg:
                reg[r["name"]] = TP.staged_irregular_entry(session, r["name"]) or {}
        launched = []
        for name in list(reg.keys()):
            if name not in (text or ""):
                continue
            entry = reg[name]
            seen = list(entry.get("seen_turns") or [])
            if turn not in seen:
                seen.append(turn)
                entry["seen_turns"] = seen[-20:]
                entry["appearances"] = len(seen)
            if entry.get("detailed"):
                continue
            if entry.get("appearances", 0) < IRR.PROMOTE_APPEARANCES:
                continue
            prep.register_task(
                f"npc_promotion:{name}", TP.TASK_NPC_PROMOTION,
                coro=self._generate_npc_detail(
                    session, name, text, master_ch, preparation=prep, entry=dict(entry)),
                parent="irregular_npc")
            launched.append(name)
        return launched

    async def _apply_irregular_npc_plan(self, session, plan, text, master_ch=None,
```

`cogs/gm.py:4526-4551`
```python
        if trigger is not None:
            reason, full, note = trigger
            preparation.register_task(
                "narrative_replan", core.turn_preparation.TASK_NARRATIVE_REPLAN,
                coro=self._prepare_auto_replan(
                    session, preparation, reason, full_replan=full, context_note=note))

    async def _prepare_auto_replan(self, session, prep, trigger_reason, *,
                                   full_replan: bool, context_note: str = ""):
        """WP-C 자동 재계획 준비 작업 — 후보 생성(result-only) + stale 검사 → 스테이징."""
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if candidate is None:
            return None
        if (not prep.matches(core.turn_transaction.get_active_transaction(session))
                or core.turn_preparation.superseded_by_newer_attempt(
                    session, logical_turn=prep.logical_turn, attempt=prep.attempt)):
            candidate.rejected_stale = True
            print(f"[서사설계/{session.session_id}] stale 재계획 준비 폐기")
            return None
        prep.replan_candidate = candidate
        return candidate

    async def _plan_narrative(self, session, trigger_reason: str = "init",
```

### S5 — TTS/media classification (automatic path: no runtime TTS; staged irregular await before stream)

`cogs/game.py:833-837`
```python
            dub_active = (
                getattr(session, "tts_enabled", False)
                and not cost_log_prefix
                and core.get_mixer(getattr(session, "voice_client", None)) is not None
            )
```

`cogs/game.py:846-851`
```python
                    # WP-C: 자동 턴 — 배정은 확정 묘사 직후 발사된 등록 준비 작업이다.
                    #   스트리밍 전에 그 결과(스테이징 계획)만 기다린다. 정본 등록은 READY 이후.
                    _irr = preparation.task_future("irregular_npc")
                    if _irr is not None:
                        await _asyncio_shield_wait(_irr)
                else:
```

### S6 — cost claim / exact freeze (+ event id retention)

`core/turn_preparation.py:997-1031`
```python
    def claim_cost_operation(self, op) -> bool:
        """자동 턴 provider 오퍼레이션을 이 시도의 비용 멤버로 명시 등록한다."""
        if self.cost_membership == COST_CLOSED:
            self.violations.append(f"late_cost_op:{getattr(op, 'operation', '?')}")
            raise BarrierViolationError(
                f"비용 멤버십 동결 이후 필수 provider 오퍼레이션 시작 시도: "
                f"{getattr(op, 'operation', '?')} (tx={self.transaction_id})")
        if getattr(op, "transaction_id", None) != self.transaction_id:
            # 귀속 불일치 — 멤버로 들이지 않는다(추정 금지). 진단은 남긴다.
            self.violations.append(
                f"attribution_mismatch:{getattr(op, 'operation', '?')}")
            return False
        if op not in self.cost_operations:
            self.cost_operations.append(op)
        return True

    def close_cost_membership(self) -> tuple:
        """비용 관련 작업이 모두 terminal일 때만 정확한 event_id 튜플을 동결한다."""
        if self.cost_membership == COST_CLOSED:
            return self.frozen_cost_event_ids
        open_cost = [r.name for r in self.tasks.values()
                     if r.blocks_cost_closure and not r.terminal]
        if open_cost:
            raise BarrierViolationError(f"비용 관련 작업 미종결: {open_cost}")
        if self.registry_state != REGISTRY_CLOSED:
            raise BarrierViolationError("레지스트리 봉인 전 비용 동결 불가")
        ids = []
        for op in self.cost_operations:
            for eid in getattr(op, "event_ids", ()) or ():
                if eid not in ids:
                    ids.append(eid)
        self.frozen_cost_event_ids = tuple(ids)
        self.cost_membership = COST_CLOSED
        return self.frozen_cost_event_ids

```

`core/cost_ledger.py:724-732`
```python
        try:
            created = self.ledger.record_cost_event(event)
        except Exception as e:  # noqa: BLE001
            print(f"[CostLedger] record 실패(무시): {type(e).__name__} - {e}")
            return False
        if created:
            self.event_ids.append(event.event_id)
        return created

```

### S7 — READY predicate + single transition

`core/turn_preparation.py:1163-1241`
```python
def evaluate_ready(session, prep) -> list:
    """READY_TO_COMMIT 조건을 검사하고 미충족 사유 목록을 반환한다(비면 충족)."""
    reasons = []
    tx = _tx.get_active_transaction(session)
    # 정체성
    if tx is None or not prep.matches(tx):
        reasons.append("identity:not_current")
    elif _tx.is_terminal(tx.status) or tx.status == _tx.TurnStatus.READY_TO_COMMIT:
        reasons.append(f"identity:status_{tx.status.value}")
    if getattr(tx, "preparation", None) is not prep:
        reasons.append("identity:preparation_not_owned")
    # 서사
    if prep.narration is None or not getattr(prep.narration, "text", ""):
        reasons.append("narrative:no_finalized_narration")
    if not isinstance(prep.staged_log, dict):
        reasons.append("narrative:log_not_staged")
    # 출력
    if prep.delivery is None or not getattr(prep.delivery, "ok", False):
        reasons.append("output:narration_not_delivered")
    # 상태 준비
    if prep.extraction_plan is None:
        reasons.append("state:no_extraction_plan")
    elif getattr(prep.extraction_plan, "rejected_stale", False):
        reasons.append("state:extraction_stale")
    for r in prep.tasks.values():
        if r.blocks_preparation_ready and not r.terminal:
            reasons.append(f"task:running:{r.name}")
        if r.required_success and r.state != TASK_SUCCEEDED:
            reasons.append(f"task:failed:{r.name}")
        if (r.transaction_id, r.logical_turn, r.attempt) != prep.identity():
            reasons.append(f"task:foreign_identity:{r.name}")
    if tx is not None:
        applied = prep.turn_effects_unapplied(tx)
        if applied:
            reasons.append("state:effects_already_applied:" + ",".join(applied))
    # 작업 종결
    if prep.registry_state != REGISTRY_CLOSED:
        reasons.append("tasks:registry_not_closed")
    # 비용
    if prep.cost_membership != COST_CLOSED or prep.frozen_cost_event_ids is None:
        reasons.append("cost:membership_not_closed")
    if prep.violations:
        reasons.append("violations:" + ",".join(prep.violations))
    return reasons


def transition_to_ready(session, prep):
    """유일한 READY_TO_COMMIT 전이 경로. 증명 객체 없이 상태만 바꾸지 않는다.

    Returns: (ReadyProof | None, reasons)
    """
    reasons = evaluate_ready(session, prep)
    if reasons:
        return None, reasons
    ready_fp = canonical_fingerprint(session)
    base = prep.baseline_fingerprint or {}
    changed = tuple(sorted(k for k in ready_fp if base.get(k) != ready_fp.get(k)))
    delivered = tuple(getattr(prep.delivery, "canonical_message_ids", ()) or ())
    proof = ReadyProof(
        transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
        attempt=prep.attempt,
        task_states=tuple((r.name, r.kind, r.state) for r in prep.tasks.values()),
        frozen_cost_event_ids=tuple(prep.frozen_cost_event_ids),
        canonical_baseline_fingerprint=dict(base),
        canonical_ready_fingerprint=ready_fp,
        canonical_unchanged=not changed, changed_domains=changed,
        delivered_message_ids=delivered,
        extraction_entries=len(getattr(prep.extraction_plan, "entries", ()) or ()),
    )
    if changed:
        # 관리자/수동 scope 쓰기(예: !증감)는 정당할 수 있으므로 차단하지 않고 증거로 남긴다.
        print(f"[WP-C/{prep.transaction_id[:8]}] READY 시점 정본 도메인 변화 감지: {changed}")
    _tx.mark_transaction_status(session, prep.transaction_id,
                                _tx.TurnStatus.READY_TO_COMMIT)
    prep.ready_proof = proof
    prep.phase = PREP_READY
    return proof, []


```

### S8 — canonical fingerprint (reviewed domains) + test

`core/turn_preparation.py:807-834`
```python
CANONICAL_DOMAINS = (
    "quest_state", "info_ledger", "resources", "statuses", "world_timeline",
    "visited_places", "companions", "met_npcs", "irregular_npcs", "npcs",
    "narrative_plan", "pending_ending", "last_extraction", "players",
    "stat_fail_counts", "turn_count", "gm_turns_done", "last_recorded_turn",
    "gm_proceed_history",
)


def _domain_digest(value) -> str:
    try:
        blob = _json.dumps(value, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        blob = repr(value)
    return _hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def canonical_fingerprint(session) -> dict:
    """리뷰된 커밋 소유 도메인의 지문(전체 Session 깊은 비교가 아님)."""
    fp = {d: _domain_digest(getattr(session, d, None)) for d in CANONICAL_DOMAINS}
    # quest_state는 리더(get_state)가 빈 dict를 기본 형태로 지연 정규화한다 —
    # 의미 변화가 아니므로 정규화된 형태로 비교한다.
    fp["quest_state"] = _domain_digest(_quest.clone_state(session))
    raw = getattr(session, "raw_logs", None) or []
    fp["raw_logs"] = f"{len(raw)}:{id(raw[-1]) if raw else 0}"
    return fp


```

`tests/policy/test_ready_barrier.py:336-344`
```python
    start = _canon(s)

    await _run_turn(r, tx=tx)

    at_ready = r.rec["ready_snapshots"][0]
    assert at_ready["status"] == tt.TurnStatus.READY_TO_COMMIT
    assert at_ready["canon"] == start, "READY 시점에 커밋 소유 정본이 이미 바뀌었습니다"
    proof = tx.preparation.ready_proof
    assert proof.canonical_unchanged and proof.changed_domains == ()
```

### S9 — next-turn gate (owner holds lock; guard; start_round after READY)

`cogs/gm.py:1395-1408`
```python
        # WP-C: 현재 시도가 확정 묘사 이후 준비/READY/재시도 대기 중이면 새 선언을
        #   받지 않는다 — 다음 자동 논리 턴은 READY 배리어(와 legacy continuation) 이후에만.
        _active = core.turn_transaction.get_active_transaction(session)
        _aprep = getattr(_active, "preparation", None) if _active is not None else None
        if _aprep is not None and getattr(_aprep, "phase", None) in (
                core.turn_preparation.PREP_PREPARING,
                core.turn_preparation.PREP_READY,
                core.turn_preparation.PREP_RETRY_PENDING):
            print(f"[WP-C/{session.session_id}] 준비 중인 시도가 있어 새 선언 차단 "
                  f"(phase={_aprep.phase})")
            if master_ch:
                await master_ch.send("⏸️ 이전 턴 준비가 끝나지 않아 새 선언을 처리하지 않습니다.")
            return

```

`cogs/gm.py:1481-1526`
```python
        outcome = None
        try:
            try:
                await self._dispatch_proceed(
                    session, instruction, transaction_id=transaction_id, preparation=prep)
                outcome = await self._join_and_ready(session, prep)
            except Exception as e:
                # 사전 READY 예기치 못한 예외 — 준비 작업을 합류시키고 실패로 처리한다.
                print(f"[WP-C/{prep.transaction_id[:8]}] 준비 중 예외: {type(e).__name__}: {e}")
                prep.violations.append(f"exception:{type(e).__name__}")
                outcome = await self._join_after_exception(session, prep)
            if outcome == _OUTCOME_READY:
                try:
                    await self._post_ready_legacy_continuation(
                        session, prep, master_ch,
                        state_before=state_before, cost_before=cost_before)
                except Exception as e:
                    # READY 이후 legacy continuation 도중 예외 — crash-safe 아님(WP-D 복구 소관).
                    #   활성 포인터가 남아 세션이 잠기지 않도록 식별자만 정리한다.
                    print(f"[WP-C/{prep.transaction_id[:8]}] READY 이후 continuation 예외: "
                          f"{type(e).__name__}: {e}")
                    prep.failure_stage = "POST_READY_CONTINUATION"
                    core.turn_transaction.finalize(
                        session, prep.transaction_id,
                        core.turn_transaction.TurnStatus.FAILED_SYSTEM,
                        failure_stage="POST_READY_CONTINUATION",
                        failure_code=core.turn_transaction.FailureCode.COMMIT_VALIDATION_FAILURE,
                        failure_message=f"{type(e).__name__}: {e}")
            else:
                await self._handle_preparation_failure(session, prep, outcome, master_ch)
        finally:
            await self._release_turn_processing(session, prep)

        if outcome == _OUTCOME_READY:
            # 디스플레이 갱신 — 턴 종료 계층 (기획서 갱신 시점 ②)
            try:
                await core.refresh_display(self.bot, session, reason="turn_end")
            except Exception as e:
                print(f"[디스플레이] 턴 종료 갱신 실패: {e}")

        if outcome in _OUTCOMES_RESTART_ROUND and session.gm_active:
            await self._start_round(session)

    # ─────────────────────────────────────────────────────────────
    # WP-C — 동시 준비 / READY_TO_COMMIT 배리어 / legacy continuation
    # ─────────────────────────────────────────────────────────────
```

### S10 — failure path (no READY, no charge)

`cogs/gm.py:1610-1687`
```python
    async def _join_and_ready(self, session, prep) -> str:
        """모든 준비 작업을 합류시키고 배리어 결과를 판정한다(S6/S7)."""
        TP = core.turn_preparation
        await prep.join()
        prep.seal()
        if not prep.matches(core.turn_transaction.get_active_transaction(session)):
            # 더 새로운 시도가 현재 — 결과는 어떤 배리어도 만족하지 못하며 흐름을 소유하지 않는다.
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
            return _OUTCOME_SUPERSEDED
        if prep.narration is None:
            outcome = _OUTCOME_NO_NARRATION
        elif prep.delivery is None or not getattr(prep.delivery, "ok", False):
            outcome = _OUTCOME_DELIVERY_FAILED
        else:
            ext = prep.tasks.get("extraction")
            if (ext is None or ext.state != TP.TASK_SUCCEEDED
                    or prep.extraction_plan is None
                    or getattr(prep.extraction_plan, "rejected_stale", False)):
                outcome = _OUTCOME_EXTRACTION_FAILED
            else:
                try:
                    prep.close_cost_membership()
                except TP.BarrierViolationError as e:
                    prep.violations.append(f"cost_close:{e}")
                proof, reasons = TP.transition_to_ready(session, prep)
                if proof is not None:
                    print(f"[WP-C/{prep.transaction_id[:8]}] READY_TO_COMMIT "
                          f"logical={prep.logical_turn} attempt={prep.attempt} "
                          f"cost_events={len(proof.frozen_cost_event_ids)} "
                          f"canonical_unchanged={proof.canonical_unchanged}")
                    return _OUTCOME_READY
                print(f"[WP-C/{prep.transaction_id[:8]}] READY 불가: {reasons}")
                prep.failure_stage = "READY_PREDICATE"
                outcome = _OUTCOME_NOT_READY
        if outcome != _OUTCOME_EXTRACTION_FAILED:
            # 종료 실패 — 비용 멤버십을 동결해 둔다(FAILED_SYSTEM은 청구 0, 사실은 보존).
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
        return outcome

    async def _handle_preparation_failure(self, session, prep, outcome, master_ch) -> None:
        """사전 READY 실패 처리 — 정본 전진·성공 턴 청구 없음(S10)."""
        TP = core.turn_preparation
        TT = core.turn_transaction
        tid = prep.transaction_id
        if outcome == _OUTCOME_SUPERSEDED:
            # 정본·세션 차단 상태를 건드리지 않는다(현재 시도가 흐름을 소유).
            prep.phase = TP.PREP_FAILED
            prep.failure_stage = "SUPERSEDED"
            print(f"[WP-C/{tid[:8]}] 대체된 시도의 준비 결과 폐기")
            return
        if outcome == _OUTCOME_EXTRACTION_FAILED:
            # 재시도 가능 상태 — 같은 논리 시도를 유지한다(새 자동 턴을 열지 않음).
            prep.phase = TP.PREP_RETRY_PENDING
            prep.failure_stage = "EXTRACTION"
            TT.mark_transaction_status(
                session, tid, TT.TurnStatus.STREAMING_EXTRACTING,
                failure_stage="EXTRACTION",
                failure_code=TT.FailureCode.EXTRACTION_PROVIDER_FAILURE)
            session.extraction_pending = True
            session.extraction_retry_ctx = {
                "text": prep.extraction_text or "", "transaction_id": tid,
                "mode": _RETRY_MODE_PREPARATION}
            await core.save_session_data(self.bot, session)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await game_ch.send(
                    "⚠️ 턴 정보 정리 중 문제가 발생했습니다.\n"
                    "아래 버튼으로 다시 시도해 주십시오. 완료 전까지 다음 턴은 진행되지 않습니다.",
                    view=ExtractionRetryView(self.bot),
                )
            if master_ch:
                await master_ch.send(
```

### S11 — post-READY legacy continuation (READY proof asserted first)

`cogs/gm.py:1736-1806`
```python
    async def _post_ready_legacy_continuation(self, session, prep, master_ch, *,
                                              state_before, cost_before) -> None:
        """
        ▼▼▼ LEGACY POST-READY CONTINUATION (pre-WP-D) ▼▼▼
        READY_TO_COMMIT 증명이 캡처된 '이후'에만 스테이징 효과를 정본에 적용하고 기존
        카운터·되감기 델타·legacy 청구·WP-01 finalize를 수행한다(S11).
        이것은 authoritative commit이 아니다 — CommitJournal/Settlement/InkTransaction
        호출 없음, crash-safe 아님. WP-D가 이 경계를 CommitCoordinator로 치환한다.
        적용 순서(현행 의미 보존 + D2 결정):
          로그/카운터 → ROLL 성장 → 비정규 NPC 등록·승격 → 진행 이력 → progress →
          지시효과 → 재계획 마커 → 추출 → 자동 재계획 → 카운터 → 델타·청구 → finalize → 저장
        """
        TP = core.turn_preparation
        tid = prep.transaction_id
        assert prep.ready_proof is not None, "READY 증명 없이 continuation 불가"

        TP.apply_staged_narration_log(session, prep.staged_log)
        TP.apply_staged_growth(session, prep)

        if prep.irregular_plan is not None and not prep.irregular_plan.applied:
            try:
                await self._apply_irregular_npc_plan(
                    session, prep.irregular_plan, prep.irregular_text,
                    self.bot.get_channel(getattr(session, "master_ch_id", 0)),
                    staged_promotions=prep.promotions)
            except Exception as e:
                print(f"[비정규NPC] 적용 실패(진행에는 영향 없음): {e}")

        if prep.proceed_history_entry is not None:
            if not hasattr(session, "gm_proceed_history"):
                session.gm_proceed_history = []
            entry = dict(prep.proceed_history_entry)
            entry["turn_num"] = session.turn_count
            session.gm_proceed_history.append(entry)
            if len(session.gm_proceed_history) > 5:
                session.gm_proceed_history = session.gm_proceed_history[-5:]

        if prep.narrative_progress:
            TP.apply_narrative_progress(session, prep.narrative_progress)

        try:
            _applied = TP.apply_instruction_effects(session, TP.pending_for(session))
            if (master_ch and _applied["applied"]
                    and _applied["quest_action"] in ("start", "switch")
                    and _applied["quest_active_name"]):
                verb = "전환" if _applied["quest_action"] == "switch" else "선정"
                await master_ch.send(
                    f"📜 **[퀘스트 {verb}]** {_applied['quest_active_name']}\n"
                    f"> {_applied['quest_reason']}")
        except Exception as e:
            print(f"[WP-B] 지시효과 적용 실패(진행에는 영향 없음): {e}")

        if prep.narrative_marker:
            _plan = getattr(session, "narrative_plan", None)
            if isinstance(_plan, dict) and _plan:
                _plan["last_planned_turn"] = session.turn_count
                session.narrative_plan = _plan

        await self._apply_prepared_extraction(session, prep)

        if prep.replan_candidate is not None:
            try:
                await self._commit_replan_candidate(
                    session, prep.replan_candidate,
                    logical_turn=prep.logical_turn, attempt=prep.attempt)
            except Exception as e:
                print(f"[서사설계] 재계획 적용 실패(기존 계획 유지): {e}")

        session.gm_clarify_count = 0
        session.gm_narrate_count = 0
        session.gm_turns_done += 1
```

### S11b — ROLL growth staging (B1)

`cogs/gm.py:2674-2688`
```python
        _prep = None
        roll_session = session
        if transaction_id is not None:
            _prep = core.turn_preparation.ensure_preparation(session, transaction_id)
            if _prep is not None:
                roll_session = core.turn_preparation.growth_projection(session, _prep)
            else:
                # stale 재개 — 정본을 절대 바꾸지 않는 일회용 투영(성장 폐기).
                roll_session = core.turn_preparation.PreparationView(session, overrides={
                    "players": copy.deepcopy(getattr(session, "players", {}) or {}),
                    "stat_fail_counts": copy.deepcopy(
                        getattr(session, "stat_fail_counts", None) or {}),
                })

        for r in rolls:
```

`core/turn_preparation.py:1140-1154`
```python
def growth_projection(session, prep) -> PreparationView:
    """ROLL 성장 판정이 정본 대신 스테이징 복제본을 변경하도록 하는 뷰."""
    if prep.growth_players is None:
        prep.growth_players = copy.deepcopy(getattr(session, "players", {}) or {})
    if prep.growth_fail_counts is None:
        fc = getattr(session, "stat_fail_counts", None)
        prep.growth_fail_counts = copy.deepcopy(fc) if isinstance(fc, dict) else {}
    view = PreparationView(session, overrides={
        "players": prep.growth_players,
        "stat_fail_counts": prep.growth_fail_counts,
    })
    return view


def projected_players(session):
```

### S12 — hard-stop scans
See §17 scans 12–15 and §18. The T-C26 test asserts the absence of WP-D callers by call pattern.

## 20. Git closure
The final SHA, the push result, local==remote equality and the literal `git status --short` are reported in the final Claude message. They are produced after this file is committed (self-reference).

## 21. Hard stop
WP-D was **not** started. The work stops here for the independent GPT gate. No merge.
