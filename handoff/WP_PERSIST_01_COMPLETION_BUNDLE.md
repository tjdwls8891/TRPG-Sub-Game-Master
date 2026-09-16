# WP-PERSIST-01 COMPLETION BUNDLE — Strict persistence prerequisite

Single completion artifact. Package: **WP-PERSIST-01 only** (strict save primitive).
CommitJournal and all later tranches **not started**.

---

## A. Gate summary

- **Goal (AUD-042):** add a strict persistence primitive that gives callers an unambiguous durable-failure signal, while the legacy tolerant `save_session_data` keeps its swallow contract for all existing callers.
- **Status:** implemented, shared-serialization refactor, strict + tolerant wrappers, 11 executable strict tests + 5 preserved tolerant characterization tests. **WP-PERSIST-01-G recommendation: PASS** (pending independent GPT audit).
- No non-goal was touched (no CommitJournal, no TurnTransaction/lifecycle change, no ink/account change, no rewind/CostLedger/cache-policy change, no caller rewiring).

---

## B. Baseline / final SHA

- **Baseline (start):** `960b8a0ed5e82777e74a7462d2306e52f662059e` (verified WP-02 checkpoint).
- **Branch:** `claude/wp-persist-01-strict-save` (created from baseline).
- **Final SHA:** recorded at end of session (see chat report + `git rev-parse HEAD`).

---

## C. Exact changed files / functions / signatures

`core/io.py` — `save_session_data` refactored into shared helpers + two wrappers (no independent duplicate serializer):
- **New** `class SessionPersistenceError(RuntimeError)` — narrow typed persistence failure; preserves cause via `__cause__`.
- **New** `_get_session_io_lock(bot, session)` — returns/creates the per-session `asyncio.Lock` (shared discipline).
- **New** `_serialize_session(session) -> dict` — the single canonical serialization (SCHEMA_VERSION + core fields + `SESSION_FIELDS` registry + `_serialize_log_entry`).
- **New** `_atomic_write_session(session, data) -> None` — unique-tmp write + `os.replace`; on failure removes its own tmp and **re-raises** (no swallow).
- **New** `_sweep_leftover_tmp(session_id) -> None` — best-effort `data.json.*.tmp` cleanup.
- **Changed** `async def save_session_data(bot, session)` — tolerant wrapper; same behavior as before (swallow + warn + sweep + return `None`), now delegating to the shared helpers.
- **New** `async def save_session_data_strict(bot, session) -> None` — strict wrapper; same serialization/lock/tmp/replace; on success returns `None` silently; on failure sweeps tmp then raises `SessionPersistenceError` preserving cause.

`core/__init__.py` — export `save_session_data_strict`, `SessionPersistenceError` (added to imports and `__all__`).

`tests/policy/test_strict_save.py` — **new**, 11 tests.

Diffstat: 3 files, +343 / −61.

**Signatures.** Legacy: `async def save_session_data(bot, session: TRPGSession)` (unchanged contract). Strict: `async def save_session_data_strict(bot, session: TRPGSession) -> None`.

---

## D. Pre-edit save caller scan

`save_session_data` is invoked from 76 production sites across:
`cogs/system.py, cogs/media.py, cogs/game.py, cogs/session.py, cogs/character.py, cogs/gm.py, core/session_flow.py, core/display.py, core/ui.py, core/cache.py, core/profile_creation_ui.py` — all via the tolerant `core.save_session_data(bot, session)`. Exported from `core/__init__.py`. No pre-existing strict variant.

---

## E. Post-edit save caller scan

- Tolerant `save_session_data` production callers: **76, unchanged** — no caller was rewired.
- `save_session_data_strict` production callers: **0**. The strict symbol appears only in `core/io.py` (definition), `core/__init__.py` (export), and `tests/policy/test_strict_save.py` (tests). Per handoff §3/§4, the strict primitive is created and verified but **not wired** into existing callers this package; the next CommitJournal package consumes it.
- `SessionPersistenceError` references: same three files only.

---

## F. Strict-vs-tolerant contract table

| Aspect | tolerant `save_session_data` | strict `save_session_data_strict` |
|---|---|---|
| Serialization rules | `_serialize_session` (shared) | `_serialize_session` (shared) — identical |
| Per-session lock | `_get_session_io_lock` (shared) | `_get_session_io_lock` (shared) — same lock object |
| tmp + atomic replace | `_atomic_write_session` (shared) | `_atomic_write_session` (shared) — identical |
| On success | returns `None` (no signal) | returns `None` (success = absence of exception; no log-and-return) |
| On failure | swallow + `⚠️ [세션 저장 실패]` log + sweep tmp + return `None` | sweep tmp + **raise `SessionPersistenceError` from cause** |
| Canonical file on failed replace | preserved (atomic replace) | preserved (atomic replace) |
| Intended use | all existing callers (unchanged) | future CommitJournal authoritative commit |

---

## G. Failure-injection evidence

All via monkeypatch + tmp paths (no real user data):
- **Serialization failure** (`io._serialize_session` raises `ValueError`): strict raises `SessionPersistenceError`, `__cause__ is` the injected error. `test_strict_serialization_failure_is_observable`.
- **tmp/write failure** (`io.json.dump` raises `OSError`): strict raises, `__cause__` is `OSError`, no `.tmp` leftovers. `test_strict_write_failure_is_observable`.
- **replace/finalization failure** (`os.replace` raises `OSError`): strict raises, cause preserved. `test_strict_replace_failure_is_observable`.
- **Canonical preserved on failure:** good strict save, then failing replace with different `turn_count` → `data.json` byte-identical to the earlier snapshot. `test_strict_failure_preserves_existing_canonical_file`.
- **tmp cleaned on failure:** `test_strict_failure_cleans_tmp_artifacts`.
- **tolerant still swallows the same injected failure while strict raises:** `test_tolerant_swallows_same_failure_that_strict_raises`.

---

## H. Concurrency / locking evidence

- Strict uses the same per-session `asyncio.Lock` object as tolerant (`io._get_session_io_lock(...) is bot.session_io_locks[sid]`, and tolerant reuses the same instance). `test_strict_uses_same_per_session_lock`.
- Two concurrent strict saves on the same session (`asyncio.gather`) serialize via the lock and complete without corruption; resulting `data.json` is valid JSON with correct `session_id` and no tmp leftovers. `test_strict_concurrent_saves_serialize_without_corruption`.
- Pre-existing tolerant characterization `test_c005e_session_lock_is_per_session` still passes (per-session lock discipline intact).

---

## I. Exact pytest output

`python3 -m pytest tests/ -q` → **124 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS.**
- Baseline preserved: 113 passed, 9 xfailed.
- New strict tests (`tests/policy/test_strict_save.py`): 11 passed.
- Tolerant characterization (`tests/characterize/test_persistence_behavior.py`, C-005a–e): 5 passed (contract unchanged).

---

## J. Compile / import output

- `python3 -m py_compile core/io.py core/__init__.py tests/policy/test_strict_save.py` → OK.
- `import core` + all 11 caller modules (`cogs.system/media/game/session/character/gm`, `core.session_flow/display/ui/cache/profile_creation_ui`, `core.io`) → IMPORT OK. `core.save_session_data_strict` / `core.SessionPersistenceError` resolve.

---

## K. Known defects intentionally untouched

The nine strict xfails remain intentional. AUD-053 and any unrelated IO/schema issues were **not** opportunistically fixed (they do not block the strict primitive). Restart-recovery semantics were **not** invented (handoff §3). Legacy tolerant callers keep their swallow behavior by design.

---

## L. New findings / blockers

- No blocker; source topology matched the handoff (single `save_session_data` in `core/io.py`, per-session `asyncio.Lock`, unique-tmp + `os.replace`, `SESSION_FIELDS` registry). No BLOCKED condition.
- Finding: `_serialize_session` is now the single serialization source shared by both wrappers, eliminating any risk of divergent strict/tolerant serializers (handoff §3 preference satisfied). Verified equivalent by `test_strict_and_tolerant_serialize_equivalent_fields`.

---

## M. `git status --short`

Clean after commit (only the WP-PERSIST-01 changeset committed): `core/__init__.py`, `core/io.py`, `tests/policy/test_strict_save.py`, `handoff/WP_PERSIST_01_COMPLETION_BUNDLE.md`. Full output recorded in the chat report.

---

## N. Hard-stop confirmation

**CommitJournal and all later packages were NOT started.** No authoritative commit, no journal, no barrier, no Settlement/InkTransaction, no caller rewiring to strict. The next action is independent GPT audit; only after gate PASS may `WP-JOURNAL-01` begin.
