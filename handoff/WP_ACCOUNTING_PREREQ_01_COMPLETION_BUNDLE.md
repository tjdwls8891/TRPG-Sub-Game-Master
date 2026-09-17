# WP-ACCOUNTING-PREREQ-01 COMPLETION BUNDLE
## Strict Financial Persistence / Read Prerequisites

Long-term repository record. Not for user retransmission.

---

## A. Gate summary

Adds strict future-authority financial I/O primitives only, leaving every live
billing/shadow caller untouched.

```text
strict CostLedger append (durable, payload-aware idempotent, typed failure)   DONE
strict CostLedger reader + exact-ID query (no silent omission)                 DONE
tolerant shadow CostLedger API preserved                                       DONE
strict account load (corruption ≠ blank, identity-checked)                     DONE
strict account write (temp+fsync+atomic replace, typed failure)                DONE
legacy account APIs + per-user lock preserved                                  DONE
strict CostLedger production authoritative callers                             0
strict account persistence production financial callers                        0
TurnSettlement code symbols/callers                                            0
InkTransaction code symbols/callers                                            0
full regression                                                                180 passed, 9 xfailed
old 9 strict xfails                                                            unchanged (0 XPASS)
```

## B. Baseline / final Git identity

```text
start checkpoint : 0eb088ee536beb34cc38032223253aac2551255c
branch           : claude/wp-accounting-prereq-01-strict-boundaries
baseline suite   : 150 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS
final suite      : 180 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS
```

Final SHA / push / local==remote recorded in the session chat gate report.

## C. Changed files / functions / signatures

```text
core/cost_ledger.py
  + class CostLedgerError(RuntimeError)
  + class CostLedgerPersistenceError(CostLedgerError)
  + class CostLedgerCorruptionError(CostLedgerError)
  + class CostLedgerConflictError(CostLedgerError)
  + class CostEventNotFoundError(CostLedgerError)
  + _canonical_cost_payload(obj) / _cost_fingerprint(obj) / _validate_cost_row_strict(obj, where)
  + CostLedger._truncate_to(size)
  + CostLedger._read_all_strict_locked() -> list[dict]
  + CostLedger.record_cost_event_strict(event: CostEvent) -> bool
  + CostLedger.list_cost_events_strict(*, session_id=None, transaction_id=None) -> list[dict]
  + CostLedger.get_cost_events_by_ids_strict(event_ids, *, session_id=None, transaction_id=None) -> list[dict]
  ~ shadow record_cost_event / list_cost_events / sum_cost_events : UNCHANGED
  ~ _registry_for structure {lock, keys(set), loaded} : UNCHANGED

core/accounts.py
  + class AccountError(RuntimeError)
  + class AccountPersistenceError(AccountError)
  + class AccountCorruptionError(AccountError)
  + load_account_strict(user_id) -> dict
  + _write_account_strict(account: dict) -> None
  ~ load_account / _write_account / register_account / add_ink / set_balance / deduct_ink / get_balance : UNCHANGED
  ~ _lock_for(user_id) : UNCHANGED (single asyncio.Lock() construction site)

tests/policy/test_cost_ledger_strict.py   (new, 16 tests §10.1-16)
tests/policy/test_accounts_strict.py       (new, 14 tests §11.17-30)
```

`core/__init__.py` NOT modified — `accounts` and `cost_ledger` are already
exported as modules, so new symbols resolve via `core.cost_ledger.X` /
`core.accounts.X`.

## D. Pre/post caller scans

```text
pre-edit production callers (core/ + cogs/, definitions excluded)
  record_cost_event : 2 (both internal shadow writers in cost_ledger.py)
  list_cost_events  : 1 (internal sum_cost_events)
  sum_cost_events   : 0
  load_account      : 8 (7 internal + cogs/system.py:587)
  _write_account    : 4 (internal: register/add_ink/set_balance/deduct_ink)
  add_ink           : 3 (core/display.py, core/terms.py, cogs/system.py)
  deduct_ink        : 3 (cogs/system.py, cogs/session.py, cogs/gm.py)
  set_balance       : 1 (cogs/system.py)

post-edit strict primitive production callers (core/ + cogs/, definitions excluded)
  record_cost_event_strict / list_cost_events_strict /
  get_cost_events_by_ids_strict / load_account_strict / _write_account_strict : 0
```

Legacy add_ink/deduct_ink/set_balance/register_account still call the tolerant
`_write_account(acc)`. No live billing callsite was rewired.

## E. Tolerant-vs-strict CostLedger contract

```text
record_cost_event (shadow)   : swallows all errors → False; key-only dedup; no fsync
record_cost_event_strict     : payload-aware dedup; write→flush→fsync; typed failure; rollback
list_cost_events (shadow)    : skips malformed lines; swallows read errors
list_cost_events_strict      : malformed/structural → CostLedgerCorruptionError; open fail → CostLedgerPersistenceError
```

Both share the same canonical serialization (`asdict(event)` → JSON) and the same
path-keyed lock. Strict append also registers the key in the shared shadow key
set so the tolerant path never double-writes it.

## F. Strict CostLedger idempotency / corruption contract

```text
same idempotency_key + equal canonical payload      → False (idempotent no-op)
same idempotency_key + conflicting canonical payload → CostLedgerConflictError, no append
on-disk same-key conflicting payload                 → CostLedgerConflictError
on-disk duplicate event_id                           → CostLedgerConflictError
canonical payload excludes envelope {event_id, created_at};
includes provider/operation/model/session_id/transaction_id/logical_turn/
turn_attempt/provider_attempt/actor_*/billing_hint/usage+token/cost_usd/cost_krw/
usage_source/success/metadata. Semantics survive reload (file-truth each strict call).
```

## G. Strict exact-ID query contract

```text
get_cost_events_by_ids_strict(ids)
  → each requested id present exactly once, returned in request order
  → missing id            → CostEventNotFoundError
  → duplicate stored id   → CostLedgerConflictError
  → optional session/transaction filter mismatch → CostEventNotFoundError (out of requested scope)
Settlement membership is explicit, never inferred from mutable session totals.
```

## H. Tolerant-vs-strict account contract

```text
load_account (legacy)   : read/JSON failure → blank account (ambiguous, AUD-061)
load_account_strict     : absent → blank (intended); malformed → AccountCorruptionError;
                          read fail → AccountPersistenceError; non-mapping → corruption;
                          stored user_id ≠ requested path → AccountCorruptionError
_write_account (legacy)  : tmp + os.replace, no fsync, returns False on failure (swallowed)
_write_account_strict    : serialize-first; temp write+flush+fsync; atomic os.replace;
                           serialize/write/fsync/replace failure → AccountPersistenceError;
                           returns None only on full durable success (no false success)
```

## I. Account failure-injection evidence

```text
serialization failure (set value)      → AccountPersistenceError            (test_22)
write failure (open 'w' raises)         → AccountPersistenceError            (test_23)
fsync failure (os.fsync raises)         → AccountPersistenceError            (test_24)
replace failure (os.replace raises)     → AccountPersistenceError            (test_25)
prior valid file survives failed replace → old balance intact               (test_26)
temp artifact cleaned after failure     → no .tmp left                       (test_27)
```

## J. Concurrency / locking evidence

```text
CostLedger: two objects same path, concurrent equivalent-replay strict append
  → exactly one durable line, results [True, False]                          (test_6)
accounts: _lock_for(uid) returns a stable single asyncio.Lock per user,
  distinct per user; only one asyncio.Lock() construction site (no second
  locking system introduced); strict primitives do not self-lock (future
  InkTransaction RMW will wrap load+mutate+write under _lock_for)            (test_30)
```

## K. Exact tests / full suite

```text
tests/policy/test_cost_ledger_strict.py : 16 (§10.1-16)
tests/policy/test_accounts_strict.py     : 14 (§11.17-30)
full suite                               : 180 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS
```

## L. Compile / import

```text
py_compile core/cost_ledger.py core/accounts.py + both new test files : OK
import core ; from core import cost_ledger, accounts                    : OK
```

## M. Untouched known defects

AUD-061 (tolerant account persistence ambiguity) and AUD-062 (shadow CostLedger
tolerance) remain intentionally in place for legacy callers. This package adds
strict alternatives beside them; it does not repair or rewire the legacy path.
Overdraft/charge policy, cost_to_ink, session totals, cache accounting,
CommitJournal, TurnTransaction, rewind/rerender, prompts and Discord output are
all untouched.

## N. New findings / blockers

None. Source at the start SHA matched the package. No scope expansion was
required to implement the primitives (§7 escape hatch not triggered).

## O. Hard-stop confirmation

TurnSettlement and InkTransaction are NOT implemented (0 code symbols/callers).
No authoritative CommitCoordinator, no live financial cutover. Strict primitives
exist and are verified only. Next action: independent GPT gate, then the
TurnSettlement / InkTransaction idempotency foundation package.
