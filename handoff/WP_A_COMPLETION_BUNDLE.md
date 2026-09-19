# WP-A COMPLETION BUNDLE
# Narration Boundary & Output Ownership

**Package:** WP-A — Narration Boundary & Output Ownership
**Status:** IMPLEMENTED — awaiting independent GPT gate
**Control date:** 2026-09-19
**Exact start SHA (required):** `810906b8d9c8773a089286f3a267385888c127cf`
**Branch:** `claude/wp-a-narration-output-boundary`
**Final SHA:** HEAD of the WP-A implementation commit on this branch (recorded in the completion report).
**Independent gate required:** YES · **Next WP begun:** NO (WP-B not started).

> No secrets/PATs appear in this bundle, in code, or in the remote URL (§36).

---

## 1. Package identity & git

- Start SHA required and verified: `810906b8d9c8773a089286f3a267385888c127cf` (== `git rev-parse HEAD` at start).
- Clean tracked tree at start: yes.
- Implementation branch: `claude/wp-a-narration-output-boundary`.
- Push: local == remote after final commit (see completion report).

## 2. Baseline / final regression

| Stage | Result |
|---|---|
| Fresh baseline (start SHA) | `269 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS` |
| Final full suite (with WP-A tests) | `286 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS` |
| Delta | +17 passed (new WP-A boundary tests). xfail frontier unchanged at 9. **XPASS count = 0.** |

Compile/import: PASS (all 93 tracked `.py` files compile; `core`, `cogs.game`, `cogs.gm`, `cogs.session` import).

The 9 strict-xfails (AUD-011/012/019/020/024/029/034·035) remain xfail — WP-A intentionally resolves none of them.

## 3. Files changed (exact) + one-line reason

| File | Reason |
|---|---|
| `cogs/game.py` (M) | Decompose `_execute_proceed` into `_generate_narration` + `_deliver_narration`; keep `_execute_proceed` as shell entrypoint; thread `transaction_id`; attach delivery message IDs to current attempt; thread output collector into `_stream_paragraphs_synced`. |
| `core/narration_result.py` (A) | Pure, immutable `NarrationResult` / `DeliveryResult` value objects + `NarrationDeliveryError` carrying already-created IDs. No Discord deps, no CostEvent duplication. |
| `core/dialogue.py` (M) | `stream_text_to_channel` now registers each created message into an optional `collector` (partial-delivery safe) and returns the created list; add idempotent `clear_messages` cleanup helper. |
| `core/__init__.py` (M) | Export `clear_messages`. (Result types are imported directly from `core.narration_result`; core public surface not widened for them.) |
| `cogs/gm.py` (M) | `_dispatch_proceed` passes `transaction_id` into `_execute_proceed` (identity only; no new transaction creation). |
| `tests/policy/test_narration_boundary.py` (A) | 17 WP-A contract tests (T-A01..A17 + directive-B legacy-tag preservation + delivery canonical-purity). |
| `cogs/session.py` | NOT changed — intro keeps calling `_execute_proceed` unchanged; no adaptation required. |

Out of scope and untouched: `prompts.py`, `scenarios/`, `data/` (verified clean).

## 4. Before/after responsibility map

Before — `cogs/game.py::_execute_proceed` (monolith): pre-processing (compression trigger, anchor, chat-lock, instruction tag parse incl. legacy 자:/태: mutation, clean_instruction/auto-instr) → WaitingStatus → provider request + cache-reissue retry + CostEvent instrumentation + response validation + PC-autonomy filter + tag strip → **canonical raw/uncompressed log append + `turn_count++`** → code-block/paragraph parse + PC-speaker drop → streaming/dialogue-format/images/TTS → code-block send → turn cost embed → save.

After:

```
_execute_proceed (shell / orchestrator — preserves all legacy timing)
  ├─ pre-processing (compression trigger, anchor, chat-lock, instruction parse incl. legacy 자:/태: mutation, clean_instruction)
  ├─ WaitingStatus.begin
  ├─ _generate_narration(...)  -> NarrationResult   (provider + validation + PC-autonomy + parse; NO canonical/finalization/streaming side effect; narration CostEvent registered exactly once)
  ├─ canonical raw/uncompressed log append + turn_count++   (SHELL owns — same position/order; automatic caller's raw_logs-growth success signal preserved)
  ├─ WaitingStatus.done  (same order: before streaming)
  ├─ _deliver_narration(NarrationResult, ...) -> DeliveryResult(message IDs)   (streaming/dialogue/images/TTS/code-block/cost-embed)
  ├─ attach delivery IDs to current attempt (if transaction_id current)  — never creates a transaction
  └─ save_session_data
```

Intro (`play_intro`) and manual (`!진행`) still call the same `_execute_proceed` shell unchanged (no `cost_log_prefix`, no `transaction_id`) → reuse the middle two helpers without inheriting automatic-turn authority.

## 5. New/changed signatures

```python
# cogs/game.py
async def _execute_proceed(self, session, instruction="", *, master_guild=None,
                           cost_log_prefix="", transaction_id: str | None = None) -> dict
async def _generate_narration(self, session, clean_instruction, *, cost_log_prefix="",
                              master_ch=None, game_channel=None, m_send=None,
                              top_imgs=None, mid_imgs=None, bottom_imgs=None) -> NarrationResult
async def _deliver_narration(self, session, narr: NarrationResult, *, game_channel=None,
                             master_ch=None, m_send=None, cost_log_prefix="",
                             transient_ids=None) -> DeliveryResult
async def _stream_paragraphs_synced(..., *, cost_scope=None, collector: list = None) -> dict  # +collector

# core/dialogue.py
async def stream_text_to_channel(bot, channel, text, ..., collector: list | None = None)  # +collector, returns created list
async def clear_messages(messages)  # idempotent cleanup

# core/narration_result.py
@dataclass(frozen=True) class NarrationResult(text, narrative_text, code_block_text, paragraphs, top/mid/bottom_images)
@dataclass(frozen=True) class DeliveryResult(ok, canonical_message_ids, transient_message_ids, media_message_ids, delivery_error)
class NarrationDeliveryError(Exception)  # carries canonical_message_ids / transient_message_ids

# cogs/gm.py
_dispatch_proceed → _execute_proceed(..., transaction_id=transaction_id)  # identity passthrough only
```

## 6. Caller evidence

- `_execute_proceed` callers (topology unchanged, C-004 characterization green): automatic `_dispatch_proceed` (`cogs/gm.py`), manual `proceed_turn` (`cogs/game.py`), intro `play_intro` (`cogs/session.py`).
- `_execute_proceed` delegates to `_generate_narration` (game.py:416) and `_deliver_narration` (game.py:448) → the automatic path actually uses the new boundary (not bypassed).
- New helpers referenced only by `_execute_proceed`; no other production caller introduced.

## 7. Preservation evidence (P-A01 … P-A18)

- **P-A01/A02/A03/A13/A14 (transaction boundary/identity):** No `begin_turn_transaction` / `get_or_begin_turn_transaction` / `begin_attempt` in `cogs/game.py`. New helpers use only read-only `is_current_transaction` / `get_active_transaction`. Tests T-A07 (automatic keeps same tx object), T-A08/A09/A13 (intro/manual create no tx). ROLL stale-continuation policy test remains green in full regression.
- **P-A04/A06 (response validation, final parsing):** `_generate_narration` keeps empty-response `ValueError` and code-block/paragraph parse verbatim. T-A04 finalized text preserved; T-A04b tag-strip preserved.
- **P-A05 (PC autonomy):** `strip_unauthorized_pc_dialogue` + PC-speaker paragraph drop preserved verbatim. T-A05.
- **P-A07/A08 (media directives, formatting):** image directives ride `NarrationResult`; paragraph/dialogue formatting unchanged in `_deliver_narration`. T-A06.
- **P-A09/A16 (TTS/media semantics):** TTS invocation (`_synthesize_and_enqueue`, `_stream_paragraphs_synced`), ordering, and cost classification carried verbatim; only change is threading `collector` into the synced path's `stream_text_to_channel` (no cost effect).
- **P-A10 (CostEvent instrumentation):** narration `_narr_op` (begin/mark_attempt/record) stays with the provider boundary in `_generate_narration`, registered exactly once; no second instrumentation added at the caller. T-A15 (exactly 1 `TURN_NARRATION` event).
- **P-A11 (compression trigger):** trigger stays in the shell, unchanged. T-A17.
- **P-A12 (processing/serialization):** `session.is_processing` guard set/reset in the shell unchanged; lock scope not broadened/narrowed.
- **P-A15 (legacy billing/extraction/log timing):** legacy 자:/태: direct mutation stays in the shell (directive B — not blocked, not partially removed); canonical log append + `turn_count++` stay in the shell at the same position; billing/extraction/next-round remain entirely in `_finish_proceed_and_continue` (untouched). Tests: legacy-tag-mutation-preserved; delivery-does-not-advance-canonical-state.
- **P-A16 (state mutation timing):** no extraction/instruction staging introduced; delivery owns no state.
- **P-A17 (runtime identity):** active transaction remains runtime identity; message IDs attach to the runtime attempt only, explicitly NOT durable committed history.
- **P-A18 (admin/ops):** shared send/render helper change (`stream_text_to_channel`) is additive/backward-compatible (13 existing callers ignore the return); admin paths untouched.

## 8. Output ownership evidence (§H)

- Where IDs are captured: `_deliver_narration` owns a local `collector`; `stream_text_to_channel` registers each created message **immediately after send** (partial-delivery safe); the code-block send and synced-TTS path also register.
- Association: `_execute_proceed` extends the current attempt's existing `TurnTransaction.canonical_message_ids` / `transient_message_ids` — only when `transaction_id` is the current active transaction (automatic path). intro/manual (`transaction_id=None`) skip attachment but still receive a normal `DeliveryResult`.
- Partial-delivery retention: on any delivery exception, `_deliver_narration` raises `NarrationDeliveryError` carrying the already-created IDs; the shell attaches those IDs to the current attempt before the existing error path. Test T-A11.
- Idempotent cleanup: `core.clear_messages` tolerates already-deleted/missing/None. Test T-A12.
- **Scope note:** these IDs identify the current runtime attempt's bot-authored output for runtime identification/cleanup only. This is NOT durable canonical committed-message history — COMMITTED/SUPERSEDED/REWOUND semantics remain WP-D/E.

## 9. Forbidden-scope scans (§I)

- Production Settlement callers in `cogs/`: **0** (only a docstring mention of the forbidden list).
- Production InkTransaction callers in `cogs/`: **0**.
- Production CommitJournal turn/billing callers in `cogs/`: **0**.
- No extraction staging / READY_TO_COMMIT barrier / commit cutover added.
- No prompt/scenario edits.

## 10. Failure evidence (§J)

- Generation failure: empty provider response → `ValueError` propagates to the shell's existing except → status cleared, operator error notice, `is_processing` reset, chat unlocked, `{"ok": False}` returned. No canonical/billing side effect from the new helper.
- Delivery failure (partial): `NarrationDeliveryError` carries already-created IDs; caller distinguishes generation success from delivery failure; narrow idempotent cleanup available. Tests T-A11/T-A12.

## 11. Known defects intentionally unchanged

AUD-011/012/019/020/024/029/034·035 remain strict-xfail. Legacy 자:/태: mutation authority, legacy `session.total_cost - cost_before` billing, extraction timing, rewind/next-round unlock all remain exactly where they were (WP-B/C/D/E scope).

## 12. New findings

- `TurnTransaction.canonical_message_ids` / `transient_message_ids` already existed on the dataclass with **zero production writers**; WP-A becomes their first (runtime-only) writer — consistent with the plan's intended attachment point. No new field invented.
- `stream_text_to_channel` previously discarded all created message handles (root cause of the "no transaction-owned narration IDs" gap in MESSAGE_LIFECYCLE_SPEC §6.5); now returns/collects them additively.

## 13. Hard stop

WP-B (Transactional State Preparation) was **not** begun. Stopping for independent GPT gate.
