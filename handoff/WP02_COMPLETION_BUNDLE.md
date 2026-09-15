# WP-02 COMPLETION BUNDLE — Append-only shadow CostLedger

Single consolidated completion artifact (replaces the multi-file handoff set).
Package: **WP-02 — provider-attempt shadow CostLedger instrumentation only.**
WP-03+ not started.

---

## A. Gate summary

- **Status vocabulary:** `WIRED_NOT_VERIFIED` → recommend **`VERIFIED`** pending independent GPT audit. Implementation + wiring + connection proof + tests are complete; every provider boundary is classified.
- **WP02-G1 recommendation:** **PASS.**
- **Baseline SHA:** `9404e4b9da1b2472efea12362c3eedb489bda1d4`
- **Final SHA:** recorded at end of this session (see chat report; commit on branch `claude/wp-02-costledger-shadow`).
- **`git status --short`** after commit: clean (only the WP-02 changeset, committed).

Shadow-mode invariant held: no legacy accounting authority, ink, TurnTransaction
lifecycle, commit barrier, extraction/mutation timing, narration streaming, cache
prepayment policy, prompts, or scenario JSON was changed.

---

## B. Tracks served / out of scope

- **Served:** Track C (Cost/billing/monitoring) — provider-attempt shadow instrumentation + callsite coverage. Track I (Verification) — new executable tests.
- **Out of scope (untouched):** Tracks A, B, D, E, F, G, H. Specifically no Settlement/InkTransaction, no CommitJournal, no rewind/rerender redesign, no narration-streaming redesign, no cache-lifecycle cutover, no legacy-command retirement.

---

## C. Changed files and exact functions/signatures

New:
- `core/cost_ledger.py` — `CostEvent` (frozen dataclass, full schema), `CostTotals`, `CostLedger(path=DEFAULT_LEDGER_PATH)` with `record_cost_event/list_cost_events/sum_cost_events/has_idempotency_key`, taxonomy constants (`ACTOR_*`, `HINT_*`, `SOURCE_*`, `OP_*`), `new_operation_id`, `get_ledger`, `_attr_from_session`, `ProviderCostContext`, `ProviderOperation` (`mark_attempt/on_attempt/current_attempt/record`), `begin_operation`, `tts_context`, `record_context_event`.
- `tests/policy/test_costledger_shadow.py` — 28 tests.

Signature changes:
- `core/resilience.py::call_with_retry(fn, *, layer, session_id="", retries=None, timeout=None, on_retry=None, `**`on_attempt_result=None, operation_id=None`**`)` — additive keyword-only args + `_notify_attempt_observer` helper (observer exceptions swallowed).
- `core/tts.py::synthesize_tts_pcm(bot, text, voice_name=None, *, `**`cost_context=None`**`)`.
- `cogs/game.py::_synthesize_and_enqueue(self, session, texts, voice_name=None, *, force=False, `**`cost_scope=None`**`)`.
- `cogs/game.py::_stream_paragraphs_synced(..., voice_name=None, *, `**`cost_scope=None`**`)`.

Wiring (op create + observer + post-hoc `record`) — no signature change:
- `main.py::TRPGBot.__init__` — attaches `self.cost_ledger = core.cost_ledger.CostLedger()`.
- `core/__init__.py` — `from . import cost_ledger`.
- `cogs/gm.py` — `_call_judgment`, `_call_gm_logic`, `_dispatch_narrate` (both branches, shared op), `interpret_cache_time`, `_generate_npc_detail`, `_resolve_irregular_npcs`, `_run_extraction`, `_verify_proceed_instruction`, `_simulate_narrative_directions`, `_plan_narrative`.
- `cogs/game.py` — `_execute_proceed::generate_with_retry` (direct instrumentation), `_run_auto_compression`, `compress_memory`, `test_tts` (passes `TTS_TEST` scope).
- `cogs/media.py` — `send_media` (image, usage_source mapping).
- `core/profile_ai.py` — `_call` (FREE_FEATURE).
- `core/utils.py` — `generate_character_details` (owner-records at SDK boundary).
- `core/tts_preset.py::build` — passes `TTS_PRESET_BUILD` context.
- `.gitignore` — `data/cost_ledger.jsonl`.

Diffstat: 13 files, +1,227 / −11.

---

## D. Pre-edit provider callsite scan

Fresh scan at baseline `9404e4b` (patterns: `call_with_retry(`, `genai_client`, `models.generate_content`, `caches.create/get/delete`, `count_tokens`, `synthesize_tts_pcm`). Boundary inventory:

- `models.generate_content` × 18
- `caches.create` × 4, `caches.get` × 1, `caches.delete` × 4
- `count_tokens` × 3 (2 runtime + 1 standalone `토큰계산.py`)

Source/matrix discrepancies (source authoritative):
1. `_dispatch_narrate` has **two** physical `call_with_retry` callsites (`if game_ch` / `else`, mutually exclusive) — matrix lists one row. Same logical op; both branches instrumented with one shared `operation_id`.
2. `TTS_TEST` reaches the SDK through the same `_synthesize_and_enqueue` helper as `TTS_RUNTIME` (`test_tts` command). Distinguished by threading `cost_scope` through the intermediate helper (helper not named in matrix §4).
3. `caches.delete` appears at 4 sites across 3 functions (`end_session`, `manage_cache`×2, `CloseConfirmView.confirm`) — matrix mentions the category only. All classified `VERIFIED_NO_COST`.

---

## E. Post-edit provider callsite scan

Re-scan after patch — boundary set **identical** (nothing disappeared into an unreviewed path):

- `models.generate_content` × 18 → 17 via `call_with_retry`+observer, 1 via direct narration instrumentation. Observer parity per file: gm.py 11/11, game.py 2/2, media.py 1/1, tts.py 1/1, profile_ai.py 1/1, utils.py 1/1.
- `caches.create` × 4, `caches.get` × 1, `caches.delete` × 4, `count_tokens` × 3 — unchanged, no CostEvent emitted (see F).

Enforced by tests `test_every_call_with_retry_site_is_observed` (parametrized over all 6 wired files) and `test_narration_has_direct_attempt_instrumentation`.

---

## F. Final coverage matrix

Legend: SW=SHADOW_WIRED, VNC=VERIFIED_NO_COST, DEF=DEFERRED(reason).

| Source | Function | Op key | Scope | Retry boundary | Attribution | usage_source | Class |
|---|---|---|---|---|---|---|---|
| cogs/gm.py | `_call_judgment` | TURN_JUDGMENT | turn | outer + cwr(1); shared op | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_call_gm_logic` | TURN_INSTRUCTION | turn | cwr | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_dispatch_narrate` (×2) | TURN_LIGHT_NARRATE | turn | cwr (2 branches, shared op) | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `interpret_cache_time` | CACHE_TIME_INTERPRET | session | cwr(1) | session (no txn) | PROVIDER_METADATA | SW |
| cogs/gm.py | `_generate_npc_detail` | TURN_NPC_PROFILE | turn | cwr(1) | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_resolve_irregular_npcs` | TURN_IRREGULAR_NPC | turn | cwr | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_run_extraction` | TURN_EXTRACTION | turn | outer + cwr(1); shared op | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_verify_proceed_instruction` | TURN_INSTRUCTION_VALIDATION | turn | cwr | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_simulate_narrative_directions` | TURN_SIMULATION | turn | cwr | txn | PROVIDER_METADATA | SW |
| cogs/gm.py | `_plan_narrative` | TURN_NARRATIVE_PLANNING | turn/session | cwr | txn | PROVIDER_METADATA | SW |
| cogs/game.py | `_execute_proceed::generate_with_retry` | TURN_NARRATION | turn | **direct** (cache-expiry retry, shared op) | txn | PROVIDER_METADATA | SW |
| cogs/game.py | `_run_auto_compression` | MEMORY_AUTO_COMPRESSION | background | cwr | session only (txn omitted, note) | PROVIDER_METADATA | SW |
| cogs/game.py | `compress_memory` | MEMORY_MANUAL_COMPRESSION | manual | cwr | session | PROVIDER_METADATA | SW |
| cogs/media.py | `send_media` | IMAGE_GENERATION | media | cwr | txn if active else null | PROVIDER_METADATA \| ESTIMATE | SW |
| core/tts.py | `synthesize_tts_pcm` | TTS_RUNTIME/TEST/PRESET_BUILD | mixed | cwr + caller ctx | caller-supplied | PROVIDER_METADATA | SW |
| core/profile_ai.py | `_call` | PROFILE_AI | free feature | cwr(1) | session (FREE_FEATURE) | PROVIDER_METADATA | SW |
| core/utils.py | `generate_character_details` | CHARACTER_DETAIL_GENERATION | manual | cwr | session_id only | PROVIDER_METADATA | SW |
| cogs/game.py | `_execute_proceed::_reissue_cache` | (CACHE_CREATE) | — | direct | — | — | **DEF** |
| cogs/session.py | `upload_cache` | (CACHE_CREATE) | — | direct | — | — | **DEF** |
| cogs/system.py | `manage_cache` | (CACHE_CREATE) | — | direct | — | — | **DEF** |
| core/cache.py | `restore_sessions_from_disk` | (CACHE_RECOVERY_CREATE) | — | direct | — | — | **DEF** |
| core/cache.py | `build_scenario_cache_text` (×2) | count_tokens | — | direct | — | — | **VNC** |
| core/cache.py | `restore_sessions_from_disk` | caches.get | — | direct | — | — | **VNC** |
| cogs/system.py (×3) / core/display.py | cache delete paths | caches.delete | — | direct | — | — | **VNC** |
| 토큰계산.py | `main` | count_tokens | — | standalone | — | — | **DEF (excluded: not bot runtime)** |

**DEFERRED reasons.** Cache `create` paths compute cost from a **pre-call TTL/upload estimate** (`calculate_upload_cost`/`calculate_cost` on `cache_tokens`); `caches.create` returns a cache object, **not** grounded provider `usage_metadata`. Per WP-02 rules ("do not reuse prepaid estimate as observed usage"; "successful cache create → shadow event only if grounded") and the forbidden-scope list ("cache billing policy 수정 금지"), no CostEvent is emitted. Separating grounded upload cost from prospective storage is a cache-lifecycle cutover reserved for a later WP. Observed and classified here without emitting an event (AUD-034/035 remain open). Enforced by test `test_cache_create_paths_emit_no_cost_event`.

---

## G. Connection proof

`real provider attempt → exactly one CostEvent → durable append-only ledger → reconciliation consumer`, demonstrated end-to-end:

1. **Retry-wrapped model call → attempt observer → CostEvent → ledger.** `call_with_retry` invokes `ProviderOperation.on_attempt` per attempt (`_notify_attempt_observer`, exceptions swallowed); the site records the successful response's legacy usage/cost via `op.record` → `CostLedger.record_cost_event` appends one JSONL line keyed `<operation_id>:attempt:<n>`. Tests 1, 2, 14.
2. **Outer semantic retry shares operation_id.** Judgment/extraction generate `operation_id` before the outer loop; each iteration increments `provider_attempt`. Test `test_outer_retry_shares_operation_id` (both keys) → 2 events, 1 operation_id, attempts {1,2}.
3. **Direct narration provider call → direct instrumentation → CostEvent.** `generate_with_retry` calls `_narr_op.mark_attempt()` per SDK invocation; cache-expiry retry keeps one `operation_id`, `provider_attempt=2`. Test 6.
4. **TTS caller context → SDK helper → CostEvent.** Caller builds `tts_context(bot, session, scope)`; the SDK-owning helper observes attempts and records via `record_context_event`. Runtime/test/preset distinguished. Test 9.
5. **Image fallback → ESTIMATE-source event.** `send_media` maps `fallback*` → `SOURCE_ESTIMATE`, else `PROVIDER_METADATA`; legacy fallback string preserved in `metadata.legacy_usage_source`. Tests 10, mapping-rule test.
6. **Reconciliation consumer.** `CostLedger.list_cost_events/sum_cost_events` filter by session/transaction/operation/billing_hint → totals for reconciliation.

---

## H. Tests

- Command: `python3 -m pytest tests/ -q`
- **Result: 109 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS.**
  - Baseline (unchanged): 81 passed, 9 xfailed.
  - New WP-02 (`tests/policy/test_costledger_shadow.py`): 28 passed.
- Evidence mapping (spec §13 / directive §Required tests / Part I §6):
  1 success→1 event; 2 retry→attempt#; 3 dup key→no dup; 4/5 judgment+extraction outer-retry share op_id; 6 narration cache-expiry retry shares op_id; 7 transaction identity carried (+null when none); 8 PROFILE_AI FREE_FEATURE; 9 TTS runtime/test/preset distinct; 10 image fallback=ESTIMATE (+mapping rule); 11 cache create emits no event (+estimate/metadata distinguishable); 12 rewind leaves ledger file unchanged; 13 record does not touch session totals; 14 shadow==legacy formula; plus: observer-failure isolation, missing-ledger no-op, cross-instance dedup persistence, exit-criterion coverage (parity + narration direct + additive signature).
- The nine known-defect xfails remain intentional and strict (no test-double signature change was required for observation plumbing).

---

## I. Compile / import checks

- `python3 -m py_compile` on all modified modules → OK.
- `import core, cogs.gm, cogs.game, cogs.media, cogs.session, cogs.system, core.cache, core.tts, core.tts_preset, core.profile_ai, core.utils` → OK.

---

## J. Reconciliation (legacy vs shadow)

Method: each shadow event is recorded **post-hoc from the exact values the legacy path already computed** (same `extract_token_usage` + same cost function), so shadow == legacy by construction.

| Callsite class | Legacy KRW | Shadow KRW | Δ | Classification |
|---|---|---|---|---|
| Text turn ops (judgment/instruction/narrate/extraction/verify/simulate/plan/npc/irregular/narration) | `calculate_text_gen_cost_breakdown[...total_krw]` | same value passed to `record` | **0** | exact (same formula) |
| Compression (auto/manual) | `calculate_upload_cost(...)` (KRW) | same KRW; USD = KRW/`EXCHANGE_RATE` | **0 KRW**; USD identical to legacy `accrue(krw)` derivation | expected (legacy accrues KRW-only; USD derived identically) |
| CACHE_TIME_INTERPRET | `...[total_krw]` → `interpret_cost_krw` | same KRW; USD from same breakdown | **0** | expected policy difference: legacy omits `total_cost`/`total_usd` (AUD-037); shadow records the provider fact regardless |
| PROFILE_AI | breakdown → `total_usd`/`profile_ai_cost_krw` (not `total_cost`) | same breakdown values | **0** | expected policy difference (free feature; AUD-033) |
| Image (metadata) | `calculate_image_gen_cost` | same values | **0** | exact |
| Image (fallback) | fallback token estimate → cost | same numbers, `usage_source=ESTIMATE` | **0 numerically** | flagged: estimate vs observed distinguished (AUD-049) |
| TTS | `calculate_text_gen_cost_breakdown(TTS_MODEL,...)` | same KRW; USD from same breakdown | **0** | exact |

No discrepancy was silently normalized. USD-derivation for KRW-only legacy sites equals the legacy `accrue` behavior (`usd = krw/EXCHANGE_RATE`).

---

## K. Instrumentation failures and diagnostics

- All observation is best-effort: `_notify_attempt_observer` swallows observer exceptions; `CostLedger.record_cost_event` and every `record`/`record_context_event` call swallow and log on failure; missing ledger → silent no-op. Test `test_observer_failure_does_not_break_call` and `test_missing_ledger_is_noop` confirm legacy call behavior is unaffected.
- Failed/timeout attempts with no usage metadata: counted (provider_attempt increments) but **no CostEvent fabricated**.

---

## L. Known defects intentionally untouched

The nine strict xfails (cache accounting, extraction boundary, instruction side-effects, rewind accounting, turn-commit races) remain as-is. AUD-021/022/026/027/028/029/031/032/033/034/035/037/049/050/054 are observed/annotated but not repaired — their fixes belong to later Settlement/CommitJournal/cache-lifecycle WPs.

---

## M. New findings / blockers

- No new blocker required a forbidden cutover. No scope was widened.
- Finding (documentation): TTS observation uses the "caller-context + SDK-owner-records" pattern (matrix §4) rather than an outer observer that records; the internal `call_with_retry` still carries an attempt-counting observer so `provider_attempt` reflects TTS retries.
- Finding: `_dispatch_narrate` two-branch topology and the `TTS_TEST` shared-helper path (Section D) are worth reflecting into the coverage matrix source.

---

## N. WP-03 hard-stop confirmation

**WP-03 was not started.** No strict-persistence/CommitJournal, narration split, staging, concurrency barrier, authoritative commit, settlement, or any later-package work was begun. Next action is the independent GPT audit + program-wide checkpoint.

---

## Proposed audit updates
*(Advisory only — AUDIT_LEDGER/AUDIT_STATE/MASTER_ROADMAP were NOT modified this session, per director instruction. GPT audit to apply.)*

**New AUD candidates**
- **AUD-05x (proposed): `_dispatch_narrate` dual-branch provider topology.** One logical NARRATE op has two mutually-exclusive `call_with_retry` callsites (`game_ch`/`else`). Coverage matrix §2 lists one row; recommend annotating the two-callsite reality. Evidence: `cogs/gm.py::_dispatch_narrate`.
- **AUD-05x (proposed): TTS_TEST shares the runtime enqueue helper.** `test_tts` (`!더빙테스트`) reaches the SDK via `_synthesize_and_enqueue`, identical to runtime dubbing; scope disambiguation requires caller-threaded `cost_scope`. Recommend matrix §4 note. Evidence: `cogs/game.py::test_tts` → `_synthesize_and_enqueue` → `core/tts.py::synthesize_tts_pcm`.

**Status-change candidates**
- **AUD-046 / AUD-039 (retry-boundary attempt visibility):** shadow provider-attempt observation now implemented at every retry boundary + direct narration retry → propose OPEN → ADDRESSED-IN-SHADOW (billing policy still outside resilience).
- **AUD-050 (TTS attribution context):** caller-supplied `ProviderCostContext` now threaded → propose CONFIRMED → ADDRESSED-IN-SHADOW.
- **AUD-049 (image estimate vs observed):** `usage_source` now distinguishes ESTIMATE vs PROVIDER_METADATA; legacy fallback retained in metadata → propose CONFIRMED → ADDRESSED-IN-SHADOW (legacy accrual unchanged).
- **AUD-037 (interpret-time provider cost absent from operational total):** now captured as a `CACHE_TIME_INTERPRET` CostEvent → propose note that shadow ledger now records it (legacy `interpret_cost_krw`-only behavior unchanged).

**Blockers / deferrals**
- **AUD-034 / AUD-035 (cache prepayment vs provider settlement):** cache `create` paths intentionally emit **no** CostEvent in WP-02 (no grounded usage; estimate/prepayment must not be mislabeled). Grounded upload-vs-storage separation is a cache-lifecycle cutover for a later WP. Marked DEFERRED in the coverage matrix.

**Roadmap / critical-path**
- No critical-path change. After WP-02, stop for the program-wide checkpoint before strict persistence/CommitJournal, per MASTER_ROADMAP. Recommend the next tranche consume this bundle (Sections E/F/J) as the shadow baseline for the Settlement cutover.
