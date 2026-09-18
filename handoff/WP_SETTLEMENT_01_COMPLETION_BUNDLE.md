# WP-SETTLEMENT-01 COMPLETION BUNDLE
## TurnSettlement / InkTransaction Idempotency Foundation

Repository history artifact. The current GPT gate should rely primarily on Claude's
final chat evidence; this file is durable context for future sessions.

---

## A. Gate summary

Immutable, replay-safe financial foundation implemented and verified **without wiring
into the live turn pipeline**:

- immutable `TurnSettlement` built from explicit strict CostEvent IDs + explicit
  transaction identity + explicit billing-user snapshot;
- one ink-rounding boundary (`cost_to_ink` called exactly once, in the builder);
- `FAILED_SYSTEM` ⇒ zero player charge;
- durable append-only `SettlementStore` with idempotent replay, conflict rejection,
  and one-CostEvent-belongs-to-one-Settlement ownership;
- deterministic per-user `InkTransaction` identity and append-only per-user ledger;
- exactly-once account executor: per-user lock → strict load → durable
  balance+marker atomic write → ledger append, with crash-window recovery;
- overdraft 1-ink floor preserved with explicit operator subsidy;
- multi-user batch executor, partial-batch replay-safe;
- CommitJournal composition proven in tests only.

Legacy `_finish_proceed_and_continue` billing remains the production authority.

---

## B. Baseline / final Git identity

- Start SHA: `125bb665fa5507e2e2adc6bfdd5be6382a18ba85`
- Branch: `claude/wp-settlement-01-financial-foundation`
- Baseline regression: `185 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS`
- Final regression: `269 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS` (+84)
- Compile/import: PASS

---

## C. Changed files / symbols

New:
- `core/settlement.py` — `SettlementOutcome`, `TransactionIdentity`, `TurnSettlement`,
  `settlement_id_for`, `normalize_billing_user_ids`, `build_turn_settlement`,
  `SettlementStore` (`record_strict` / `get_settlement_strict` / `find_settlement` /
  `list_for_transaction`), `default_settlement_path`, exception taxonomy.
- `core/ink_transactions.py` — `InkTransaction`, `expected_ink_tx_id`,
  `charge_request_fingerprint`, `InkTransactionLedger`
  (`append_strict` / `get_by_id_strict` / `list_all`), `execute_settlement_charge`,
  `execute_settlement_charges`, exception taxonomy.

Modified (narrow):
- `core/accounts.py` — `_blank_account` gains backward-compatible
  `applied_ink_transactions: {}`; adds `get_applied_ink_marker` and
  `apply_ink_charge_strict` (reuse existing `_lock_for` + `_write_account_strict`;
  no new lock domain). Legacy mutators untouched.
- `core/__init__.py` — two module exports (`settlement`, `ink_transactions`).
- `tests/policy/test_accounts_strict.py::test_29` — added the two new foundation
  definition modules to the strict-symbol scan's definition-file exclusion set
  (the settlement builder is the authorized first consumer of the strict exact-ID
  reader; cogs/ callers remain 0, proven directly by the new S8 scan).

New tests:
- `tests/policy/test_settlement.py` (44)
- `tests/policy/test_ink_transactions.py` (33)
- `tests/policy/test_settlement_journal_composition.py` (7)

---

## D. Source reconciliation and legacy billing snapshot

Legacy billing at `cogs/gm.py::_finish_proceed_and_continue` (unchanged):
`turn_cost = session.total_cost − cost_before` → `ink = core.cost_to_ink(turn_cost)`
→ `session.total_ink_spent += ink` → for each `uid in session.players`:
`accounts.deduct_ink(uid, ink, allow_overdraft=True)` + `stats.bump`.
Same full turn-ink to each player (not split). Overdraft floors balance to 1.

The foundation preserves these semantics: `charge_ink_per_user` is one computed
amount; `aggregate_nominal_charge_ink = charge_ink_per_user × len(billing_user_ids)`;
the executor replicates the legacy floor conditional (`balance − nominal < 1 ⇒ 1`)
and `total_spent_ink += nominal` compatibility.

---

## E. Settlement schema / identity

`settlement_id = "turn-settlement:<transaction_id>:attempt:<attempt>"` — deterministic
from immutable attempt identity; no time/random/narration/mutable-total input. Frozen
dataclass carries session/transaction/logical_turn/attempt, outcome, canonicalized
`included_cost_event_ids` and `billing_user_ids` tuples, provider/player/non-player/
system-unbilled/estimated cost totals, `charge_ink_per_user`,
`aggregate_nominal_charge_ink`, `policy_version`, `created_at`.

---

## F. CostEvent classification and estimate policy

`build_turn_settlement` reads exact IDs via
`CostLedger.get_cost_events_by_ids_strict`, then validates
session/transaction identity (and logical_turn/turn_attempt when populated) and
classifies:
- COMMITTED: PLAYER_CANDIDATE + PROVIDER_METADATA/FIXED_PROVIDER_PRICING ⇒ billable;
  PLAYER_CANDIDATE + ESTIMATE (or invalid source with positive cost) ⇒
  `SettlementUnresolvedEstimateError`; FREE_FEATURE/OPERATOR/SYSTEM ⇒ non-player;
  UNKNOWN ⇒ `SettlementPolicyError`.
- FAILED_SYSTEM: zero player charge; PLAYER_CANDIDATE cost → `system_unbilled`;
  FREE_FEATURE/OPERATOR/SYSTEM keep non-player nature; ESTIMATE exposure retained in
  `estimated_cost_krw` without charge.

---

## G. Rounding and multi-player semantics

`charge_ink_per_user = cost_to_ink(player_billable_cost_krw)` — invoked exactly once at
Settlement construction (verified by counting monkeypatch). Executor and all future
consumers reuse the stored value. No cost split among users.

---

## H. Settlement persistence / idempotency

`sessions/{session_id}/turn_settlements.jsonl`, append-only, per-path lock,
write→flush→fsync, truncate-rollback on durability failure. Same id + equivalent
canonical payload ⇒ idempotent no-op returning the canonical persisted object; same id
+ conflicting payload ⇒ `SettlementConflictError`; duplicate durable id or malformed
line ⇒ `SettlementCorruptionError`; missing id ⇒ `SettlementNotFoundError`.

---

## I. CostEvent single-settlement ownership

`SettlementStore` builds a `cost_event_id → settlement_id` ownership index over durable
rows; a new/different Settlement claiming an already-owned CostEvent ⇒
`CostEventAlreadySettledError`. Within one Settlement, duplicate input IDs ⇒
`SettlementPolicyError` (validated before canonical ordering).

---

## J. InkTransaction schema / identity

`ink_tx_id = "ink-charge:<settlement_id>:user:<user_id>"`. Frozen record carries
settlement/user/kind/nominal + balance_before/after + applied_balance_delta +
overdraft + operator_subsidy_ink + reason + created_at. Ledger:
`accounts/ink_transactions/{user_id}.jsonl`, append-only, per-path lock,
write→flush→fsync; duplicate id/malformed/path-user mismatch ⇒ corruption/conflict;
equivalent replay returns canonical; conflicting replay rejected.

---

## K. Account applied-marker design

`apply_ink_charge_strict` under the existing per-user `_lock_for`: strict-load →
marker check (equal fingerprint ⇒ ALREADY, different ⇒ CONFLICT, never overwritten) →
compute immutable result → write balance + `total_spent_ink` (nominal, legacy-compat) +
marker in ONE `_write_account_strict` atomic replace. The account marker (not the
separate ledger) is the durable "already applied" authority.

---

## L. Crash-window recovery proof

- account strict-write fails ⇒ no mutation, no marker, no ledger append,
  `InkTransactionPersistenceError` (test 57/58).
- account written, ledger append fails ⇒ balance changed once, marker present,
  `InkTransactionRecoveryRequired` (test 59); replay repairs ledger with no second
  deduction (test 60).
- both durable ⇒ replay is a no-op (test 61).
- ledger present but account marker absent ⇒ `InkTransactionRecoveryRequired`, no blind
  deduction (test 62).

---

## M. Overdraft / subsidy compatibility

1-ink floor preserved; `operator_subsidy_ink = nominal − (balance_before −
balance_after)` truthful and explicit (test 66: 5-ink balance, 10 nominal ⇒ after 1,
delta −4, subsidy 6). Sufficient balance ⇒ subsidy 0 (test 65). Overdraft-disallowed +
insufficient ⇒ no mutation, `InsufficientInkError` (test 68). `total_spent_ink`
nominal-accumulation preserved (test 69).

---

## N. Concurrency proof

Concurrent duplicate execution for the same user/settlement charges exactly once
(test 63) — the atomic `apply_ink_charge_strict` under the single per-user lock is the
authority; the second call observes the marker. `asyncio.Lock()` count in
`core/accounts.py` remains 1; `core/ink_transactions.py` introduces no account-mutation
lock (test 64).

---

## O. CommitJournal composition proof

`JournalEntry.settlement_id` already exists (no schema change). Tests place a persisted
settlement_id in a JournalEntry, reload the Settlement by that id after simulated
restart, and re-run the executor (RESUME_BILLING-style) with exactly one account effect
(tests 76–78). No production CommitJournal billing caller introduced (test 79).

---

## P. Production caller scans (post-patch)

- cogs Settlement builder/store callers: 0
- cogs InkTransaction executor/ledger callers: 0
- cogs CommitJournal billing/append callers: 0
- legacy `deduct_ink` callers in cogs: 3 (unchanged)
- `_finish_proceed_and_continue` legacy billing: present

---

## Q. Exact targeted / full tests

- `tests/policy/test_settlement.py` — 44 (identity/model, exact-set/classification,
  FAILED_SYSTEM/estimate/rounding, store/idempotency/ownership).
- `tests/policy/test_ink_transactions.py` — 33 (identity/ledger, exactly-once + failure
  injection, overdraft/compat, multi-player replay).
- `tests/policy/test_settlement_journal_composition.py` — 7 (composition + S8).
- Full suite: `269 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS`.

---

## R. Compile / import

`python3 -m py_compile core/settlement.py core/ink_transactions.py core/accounts.py
core/__init__.py` ⇒ OK. `import core` ⇒ OK.

---

## S. Untouched known defects

The 9 strict xfails remain intentional and unchanged; no XPASS. Legacy tolerant
CostLedger/account paths (AUD-061/AUD-062 runtime findings) remain intentionally
tolerant and are out of scope for this foundation package.

---

## T. New findings / blockers

- Existing `test_29` (WP-ACCOUNTING-PREREQ-01) encoded "zero strict-primitive callers
  anywhere in core+cogs except definition files." WP-SETTLEMENT-01 authorizes the first
  core-level consumer (the settlement builder consuming
  `get_cost_events_by_ids_strict`, by design). `test_29` was updated surgically to add
  `core/settlement.py` and `core/ink_transactions.py` to its definition-file exclusion
  set; full cogs/ coverage and the legacy-intact assertions are retained, and a new
  dedicated S8 scan proves cogs callers = 0.
- No BLOCKED conditions. No forbidden-scope work performed.

---

## U. Hard stop

Foundation only. Live Settlement billing cutover remains a later package
(authoritative commit integration tranche). This package does not enable any
authoritative turn commit and does not wire the foundation into the live pipeline.
Next critical package after independent PASS: narration generation / streaming
separation.
