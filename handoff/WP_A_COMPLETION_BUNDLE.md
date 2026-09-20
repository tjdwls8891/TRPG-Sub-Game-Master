# WP-A COMPLETION BUNDLE
# Narration Boundary & Output Ownership

**Package:** WP-A — Narration Boundary & Output Ownership
**Status:** IMPLEMENTED (patch 2 — full delivery output ownership) — awaiting independent GPT gate
**Exact start SHA (required):** `810906b8d9c8773a089286f3a267385888c127cf`
**Branch:** `claude/wp-a-narration-output-boundary`
**Final SHA:** HEAD of the WP-A patch-2 commit on this branch (recorded in the completion report).
**Prior (superseded, WIRED_NOT_VERIFIED) checkpoint:** `ecd2165998d6be78827200c714d345f621039877` — not a verified start point.
**Independent gate required:** YES · **Next WP begun:** NO (WP-B not started).

> No secrets/PATs appear in this bundle, code, or the remote URL.

---

## 1. Git identity

- Start SHA required and verified: `810906b8…` (== `git rev-parse HEAD` at start; clean tracked tree).
- Branch: `claude/wp-a-narration-output-boundary` (continued; no new baseline per gate instruction).

## 2. Baseline / final regression

| Stage | Result |
|---|---|
| Fresh baseline (start SHA) | `269 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS` |
| Final full suite (patch 2) | `291 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS` |
| Delta | +22 WP-A tests. **XPASS = 0.** 9-xfail defect frontier unchanged. |

Compile/import: all 95 tracked `.py` compile (0 failures); `core`, `cogs.game/gm/session`, result types import.

## 3. Files changed + one-line reason

| File | Reason |
|---|---|
| `cogs/game.py` (M) | Decompose `_execute_proceed` into `_generate_narration` + `_deliver_narration`; keep `_execute_proceed` shell; thread `transaction_id`; two collectors (text + media); attach canonical/transient/**media** IDs to current attempt. |
| `core/narration_result.py` (A) | Pure immutable `NarrationResult` / `DeliveryResult` (canonical + transient + **media** IDs) + `NarrationDeliveryError` carrying already-created IDs. No Discord deps; no CostEvent duplication. |
| `core/dialogue.py` (M) | `stream_text_to_channel` registers each created message into optional `collector` (partial-safe) + returns list; `maybe_send_speaker_image` gains optional `collector`; add idempotent `clear_messages`. |
| `core/media.py` (M) | **[minimal scope expansion — patch 2]** `send_image_by_keyword` gains optional `collector`, captures each created game-channel image message, returns it. Additive; existing callers unaffected. |
| `core/turn_transaction.py` (M) | **[minimal scope expansion — patch 2]** add additive `media_message_ids: list` field (runtime attempt ownership of narration-associated media). No serialization impact (runtime-only, defaulted). |
| `core/__init__.py` (M) | Export `clear_messages`. (Result types imported directly from `core.narration_result`; core surface not widened for them.) |
| `cogs/gm.py` (M) | `_dispatch_proceed` passes `transaction_id` into `_execute_proceed` (identity only; no new transaction creation). |
| `tests/policy/test_narration_boundary.py` (A) | 22 WP-A contract tests (T-A01..A17 + directive-B legacy-tag + delivery canonical-purity + 5 media-ownership tests). |
| `cogs/session.py` | NOT changed — intro keeps calling `_execute_proceed` unchanged. |

Scope-expansion note (gate-permitted): `core/media.py` and `core/turn_transaction.py` were added to the WP-A file set solely to satisfy the output-ownership contract for narration-associated media (class B). Changes are additive/minimal; no media architecture or MessageLifecycle redesign. `prompts.py`, `scenarios/`, `data/` untouched.

## 4. Delivery send-site inventory (post-change)

All message-creating sends reachable from `_deliver_narration` (incl. `_stream_paragraphs_synced`), classified:

| Send site | Message class | Channel | Return handle now | WP-A runtime ownership | Collected now |
|---|---|---|---|---|---|
| `stream_text_to_channel` (async non-dialogue) | A canonical narration | game | yes (list + collector) | yes | **yes** (`collector`) |
| `stream_text_to_channel` (async dialogue) | A canonical narration | game | yes | yes | **yes** (`collector`) |
| `stream_text_to_channel` (synced TTS path) | A canonical narration | game | yes | yes | **yes** (`collector`) |
| `game_channel.send(code_block_text)` | A canonical narration (code block) | game | yes (`_cb_msg`) | yes | **yes** (`collector`) |
| `send_image_by_keyword` (async top/mid/bottom, 5 sites) | B narration-associated media | game | yes (patch 2) | yes | **yes** (`media_collector`) |
| `send_image_by_keyword` (synced top/mid/bottom, 4 sites) | B media | game | yes (patch 2) | yes | **yes** (`media_collector`) |
| `maybe_send_speaker_image` (async + synced) | B media (speaker image) | game | registers via collector (patch 2) | yes | **yes** (`media_collector`) |
| `WaitingStatus` message (created in shell) | C transient/operational (player-facing) | game | yes (`.message`) | yes (cleanup basis) | **yes** (`transient_message_ids`) |
| `m_send(...)` TTS warnings (2) | C operational | master | n/a | **no** — operator diagnostic, not turn narration output | no (by design) |
| `m_send(embed=cost)` turn cost embed | C operational | master | n/a | **no** — cost reporting to operator | no (by design) |
| `m_send("✅ 묘사 연출 완료 …")` | C operational | master | n/a | **no** — operator status notice | no (by design) |
| `_resolve_irregular_npcs` → `master_ch.send("🎭 …")` | C operational | master | n/a | **no** — operator media-assignment notice | no (by design) |

Rationale for C exclusions: master-channel messages are operator diagnostics / cost reporting, not player-facing turn narration output; rewind/cleanup of a turn's narration does not target operator notices. The one player-facing transient (WaitingStatus) IS tracked (`transient_message_ids`) for its cleanup basis. No class-D (unrelated) sends exist in these paths.

Verification: every `send_image_by_keyword` / `maybe_send_speaker_image` call in both methods passes `collector=media_collector`; both `stream_text_to_channel` calls pass `collector=collector`; the code-block send is captured. No bare (un-collected) A/B game-channel send remains.

## 5. Before/after responsibility map

Before — `_execute_proceed` monolith. After:

```
_execute_proceed (shell / orchestrator — preserves all legacy timing)
  ├─ pre-processing (compression trigger, anchor, chat-lock, instruction parse incl. legacy 자:/태: mutation, clean_instruction)
  ├─ WaitingStatus.begin
  ├─ _generate_narration(...) -> NarrationResult   (provider + validation + PC-autonomy + parse; NO canonical/finalization/streaming; narration CostEvent exactly once)
  ├─ canonical raw/uncompressed log append + turn_count++   (SHELL — same position/order; automatic caller's raw_logs-growth success signal preserved)
  ├─ WaitingStatus.done  (before streaming — same order)
  ├─ _deliver_narration(NarrationResult, ...) -> DeliveryResult(canonical + transient + media IDs)
  ├─ attach canonical/transient/media IDs to current attempt (if transaction_id current)  — never creates a transaction
  └─ save_session_data
```

Intro (`play_intro`) and manual (`!진행`) call the same shell unchanged (no `cost_log_prefix`, no `transaction_id`).

## 6. New/changed signatures

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
async def _stream_paragraphs_synced(..., *, cost_scope=None, collector=None, media_collector=None) -> dict

# core/dialogue.py
async def stream_text_to_channel(bot, channel, text, ..., collector=None)   # returns created list
async def maybe_send_speaker_image(channel, session, speaker, collector=None) -> bool
async def clear_messages(messages)                                          # idempotent

# core/media.py
async def send_image_by_keyword(game_channel, master_ctx, session, keyword, collector=None)  # returns created Message|None

# core/turn_transaction.py
TurnTransaction.media_message_ids: list  # additive runtime field

# core/narration_result.py
NarrationResult(text, narrative_text, code_block_text, paragraphs, top/mid/bottom_images)   # frozen
DeliveryResult(ok, canonical_message_ids, transient_message_ids, media_message_ids, delivery_error)   # frozen
NarrationDeliveryError(*, canonical_message_ids, transient_message_ids, media_message_ids)

# cogs/gm.py
_dispatch_proceed → _execute_proceed(..., transaction_id=transaction_id)
```

## 7. Tests (22)

Generation purity T-A01/02/03; preservation T-A04/04b/05/06; CostEvent count T-A15; delivery ownership T-A10/11/12; shell integration T-A07/08/09; compression T-A17; directive-B legacy-tag preservation; delivery canonical-purity. **Media ownership (patch 2):** M1 media ID in DeliveryResult; M2 automatic media attached to attempt; M3 intro/manual media creates no transaction; M4 text+media partial success then failure preserves all owned IDs; M5 media cleanup idempotent.

## 8. Preservation & forbidden-scope (summary)

- No `begin_turn_transaction` / `get_or_begin_turn_transaction` / `begin_attempt` in `cogs/game.py`; new helpers use only read-only `is_current_transaction` / `get_active_transaction`. (directive F)
- Legacy 자:/태: mutation, canonical log append, `turn_count++`, billing/extraction/next-round timing unchanged. (directives A/B; P-A15/16)
- narration `_narr_op` CostEvent registered exactly once; no second caller instrumentation. (directive C; T-A15)
- Compression trigger unchanged in shell. (directive D; T-A17)
- 0 live Settlement / InkTransaction / CommitJournal callers in `cogs/`; no prompt/scenario edits; no extraction/barrier cutover. (§S7 scans)

---

# Level-2 GPT gate evidence — S1–S7 (actual source excerpts)

### S1 — generation boundary (`cogs/game.py`)
```python
async def _generate_narration(self, session, clean_instruction, *, cost_log_prefix: str = "",
                               master_ch=None, game_channel=None, m_send=None,
                               top_imgs=None, mid_imgs=None, bottom_imgs=None) -> NarrationResult:
    """묘사 provider 호출 + 응답 검증 + PC자율성/파싱 후처리 → NarrationResult.

    단독 호출 시 정상 턴 확정 부작용을 수행하지 않는다: canonical raw/uncompressed 로그
    append, turn_count 증가, 상태/자원 변이, Settlement/InkTransaction, 청구, 다음 라운드
    unlock, canonical save, 스트리밍(게임 채널 전달) 중 어느 것도 하지 않는다. ...
    narration CostEvent는 정확히 한 번(_narr_op) 등록된다. 응답이 비면 ValueError를 던져 ...
    """
    # ... provider 호출 / 캐시 재시도 ...
    full_ai_response = response.text
    if not full_ai_response:
        finish_reason = response.candidates[0].finish_reason if response.candidates else "Unknown"
        raise ValueError(f"AI가 텍스트를 반환하지 않았습니다. ...")
    # ... PC 자율성 strip / 태그 strip / 코드블럭·문단 파싱 ...
    return NarrationResult(
        text=full_ai_response, narrative_text=narrative_text, code_block_text=code_block_text,
        paragraphs=tuple(paragraphs),
        top_images=tuple(top_imgs), mid_images=tuple(mid_imgs), bottom_images=tuple(bottom_imgs))
```
Generation returns the finalized result only; no `raw_logs.append` / `turn_count` / `deduct_ink` / `save_session_data` / streaming appears anywhere in the method body. (T-A01/02/03 assert this dynamically.)

### S2 — delivery boundary (`cogs/game.py`)
```python
async def _deliver_narration(self, session, narr: NarrationResult, *, game_channel=None,
                              master_ch=None, m_send=None, cost_log_prefix="", transient_ids=None) -> DeliveryResult:
    paragraphs      = list(narr.paragraphs)         # NarrationResult 소비
    narrative_text  = narr.narrative_text
    code_block_text = narr.code_block_text
    top_imgs, mid_imgs, bottom_imgs = list(narr.top_images), list(narr.mid_images), list(narr.bottom_images)

    collector: list = []          # 텍스트/코드블럭 메시지 즉시 등록(부분 전달 안전)
    media_collector: list = []    # 이미지/화자 이미지 메시지 즉시 등록(부분 전달 안전)
    # ... 스트리밍: core.stream_text_to_channel(..., collector=collector)
    # ... 이미지:   core.send_image_by_keyword(..., collector=media_collector)
    # ... 화자이미지: core.maybe_send_speaker_image(..., collector=media_collector)
    # ... 코드블럭: _cb_msg = await game_channel.send(code_block_text); collector.append(_cb_msg)
    return DeliveryResult(
        ok=True,
        canonical_message_ids=tuple(getattr(m, "id", None) for m in collector if m is not None),
        transient_message_ids=tuple(transient_ids),
        media_message_ids=tuple(getattr(m, "id", None) for m in media_collector if m is not None))
```

### S3 — automatic caller: identity outside renderer, path goes through new boundary
`cogs/gm.py` (`_dispatch_proceed`; transaction created upstream by `_process_actions`, passed as identity):
```python
result = await game_cog._execute_proceed(
    session, exec_instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX,
    transaction_id=transaction_id)
```
`cogs/game.py` shell attaches output to the current attempt (never creates one):
```python
if transaction_id and core.turn_transaction.is_current_transaction(session, transaction_id):
    _tx = core.turn_transaction.get_active_transaction(session)
    if _tx is not None:
        _tx.canonical_message_ids.extend(delivery.canonical_message_ids)
        _tx.transient_message_ids.extend(delivery.transient_message_ids)
        _tx.media_message_ids.extend(delivery.media_message_ids)
```
The automatic path reaches `_generate_narration` (game.py:416) and `_deliver_narration` (game.py:448) through the shell; raw_logs growth (shell) remains the automatic caller's success signal. (T-A07/M2.)

### S4 — intro/manual isolation
Intro and manual call the shared shell with **no** `cost_log_prefix` / `transaction_id`:
```python
await game_cog._execute_proceed(session, instruction)                       # cogs/session.py — intro
await self._execute_proceed(session, instruction, master_guild=ctx.guild)   # cogs/game.py — manual !진행
```
Neither passes a transaction, and the shell/helpers never call any transaction-creation API → no automatic TurnTransaction, no normal-turn billing. (T-A08/A09/A13, M3 assert `get_active_transaction(session) is None`.)

### S5 — output ownership / partial failure
Register-on-success (`core/dialogue.py::stream_text_to_channel`):
```python
current_message = await channel.send(display_text + "✍️")
# WP-A: 전송 직후 즉시 등록 — 이후 문단 전송이 실패해도 이 ID는 회수 가능하다.
created.append(current_message)
if collector is not None:
    collector.append(current_message)
```
Preserve-on-failure (`cogs/game.py::_deliver_narration`):
```python
except Exception as _e:
    raise NarrationDeliveryError(
        str(_e),
        canonical_message_ids=tuple(getattr(m, "id", None) for m in collector if m is not None),
        transient_message_ids=tuple(transient_ids),
        media_message_ids=tuple(getattr(m, "id", None) for m in media_collector if m is not None)) from _e
```
Transaction-local attachment: see S3 (success) and the shell `except` (failure) — both `extend` the current attempt's `canonical/transient/media_message_ids`. Idempotent cleanup: `core.clear_messages` swallows already-deleted/missing. (T-A11/A12, M4/M5.)

### S6 — CostEvent preservation (`cogs/game.py::_generate_narration`)
```python
_narr_op = core.cost_ledger.begin_operation(
    self.bot, core.cost_ledger.OP_TURN_NARRATION, session=session,
    model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
    billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
# ... 실제 provider 호출마다 _narr_op.mark_attempt() ...
_narr_op.record(
    input_tokens=in_tokens, cached_input_tokens=cached_tokens, output_tokens=out_tokens,
    thought_tokens=thought_tokens, cost_usd=breakdown["total_usd"], cost_krw=turn_cost,
    usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
```
Exactly one narration operation (`begin_operation` → `mark_attempt` per real attempt → single `record`) at the provider boundary inside generation; the shell/delivery add no narration instrumentation. (T-A15 asserts exactly 1 `TURN_NARRATION` event.)

### S7 — hard-stop scans (commands + results)
```
$ grep -rInE "Settlement|InkTransaction|CommitJournal" cogs/ --include="*.py"
cogs/game.py:502:   ... Settlement/InkTransaction, 청구, 다음 라운드   # (docstring only — 0 live callers)
  → Settlement live callers: 0 · InkTransaction live callers: 0 · CommitJournal live turn/billing callers: 0

$ git status --short prompts.py scenarios/ data/
  (empty)   # no unauthorized prompt/scenario/data edit

extraction staging / READY_TO_COMMIT barrier / commit cutover: none added (no such symbols introduced).
```

## 9. Hard stop

WP-B (Transactional State Preparation) was **not** begun. Stopping for independent GPT gate. The superseded `ecd2165…` is not a verified start point; use the patch-2 Final SHA in the completion report.
