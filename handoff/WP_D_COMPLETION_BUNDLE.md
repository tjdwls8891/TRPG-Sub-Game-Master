# WP-D COMPLETION BUNDLE — Authoritative Commit, Recovery & Billing Cutover

**Status:** WIRED_NOT_VERIFIED (independent GPT gate 대기) · **WP-E: NOT STARTED**

| 항목 | 값 |
|---|---|
| start SHA | `420d61e9d040e59dcb909f8d156bc0ab02d58e72` |
| final SHA | 이 번들을 담은 커밋 = 브랜치 HEAD(채팅 보고에 literal SHA 기재) |
| branch | `claude/wp-d-authoritative-commit-recovery` |
| baseline | `402 passed, 3 xfailed` (시작 시 fresh 실행) |
| final suite | `439 passed, 3 xfailed, 0 failed, 0 XPASS` |
| compile/import | PASS (`py_compile` 전 모듈, `import core`, cog 로드 9·명령어 46·views 5) |
| push / local==remote / clean | 채팅 보고에 literal 출력 기재 |

> 종료 문장: **정상 자동 턴의 canonical game state와 player charge는 새 transaction authority(CommitCoordinator → CommitJournal/strict save/TurnSettlement/InkTransaction)에 의해 결정된다.**

---

## 1. 변경 파일

```
A core/commit_coordinator.py              ← 단일 커밋·복구 owner (신규)
A tests/policy/test_authoritative_commit.py
A tests/policy/test_commit_recovery.py
A handoff/WP_D_COMPLETION_BUNDLE.md
M cogs/gm.py        legacy continuation 제거 → _commit_ready_turn/_apply_commit_effects/_after_commit,
                    적용 헬퍼 derived 수집기, 사전 READY 실패 FAILED_SYSTEM 정산, 복구 게이트
M cogs/game.py      자동 경로 턴 비용 임베드를 READY 이전에 보내지 않음(수동·인트로 불변)
M core/io.py        session_io_lock / write_session_strict_locked seam, tolerant save 재진입 가드,
                    SESSION_FIELDS += commit_marker·last_turn_ink·rewind_degraded_turns
M core/models.py    위 3필드 + 런타임 commit_recovery
M core/turn_preparation.py  CANONICAL_DOMAINS += 3필드, PREP_COMMITTING
M core/cache.py     restore_sessions_from_disk: 등록·캐시 저장 전 recover_session / abandon_retry_pending
M core/cost.py      build_turn_cost_embed(settlement=) — Settlement 값만 표시
M core/display.py   직전 턴 잉크 = last_turn_ink(재환산 제거)
M core/ui.py        세션 종료 ink_spent 재계상 제거(AUD-028)
M core/__init__.py  commit_coordinator, io seam 재수출
M CLAUDE.md, DEVLOG.md  SESSION_FIELDS 76→79 (WP-D 필드 신설분만)
M tests/…(8)        owner가 바뀐 특성화/정책 단언 갱신(§6)
```

비변경(foundation 스키마): `core/settlement.py` `core/ink_transactions.py` `core/accounts.py`
`core/commit_journal.py` `core/cost_ledger.py` `core/rewind.py` · 프롬프트·시나리오 무변경.

## 2. 주요 심볼 / 시그니처

```python
class CommitCoordinator:
    async def commit_ready_turn(self, session, prep, *, apply_effects, state_before=None) -> CommitResult
CommitOutcome = COMMITTED | FAILED_PRE_PERSIST | RECOVERY_PENDING | REJECTED
@dataclass(frozen=True) class CommitPlan  # journal_metadata()/from_journal_metadata()/target_marker()
COMMIT_OWNED_FIELDS; capture_rollback_snapshot(); restore_rollback_snapshot()
build_committed_settlement(bot, plan); persist_failed_settlement(...); settle_failed_attempt(bot, session, prep)
async recover_session(bot, session, *, context) -> RecoveryReport   # CLEAN|RESOLVED|PENDING|RECOVERY_REQUIRED
retry_pending_breadcrumb(); async persist_retry_breadcrumb(); async abandon_retry_pending()
core.io.session_io_lock(bot, session); async core.io.write_session_strict_locked(session)
core.cost.build_turn_cost_embed(..., settlement=None)
GMCog: _commit_ready_turn, _apply_commit_effects, _after_commit, _emit_commit_derived,
       _send_turn_cost_report, _settle_failed_turn, _cleanup_uncommitted_output, _resume_commit_recovery
GMCog 헬퍼 kw 추가: _apply_extraction_plan/_apply_irregular_npc_plan/_apply_npc_promotion/
       _apply_prepared_extraction/_commit_replan_candidate/_apply_narrative_plan(*, derived=None)
삭제: GMCog._post_ready_legacy_continuation
```

## 3. 호출자 인벤토리 (pre → post)

| 대상 | pre (420d61e) | post |
|---|---|---|
| `_post_ready_legacy_continuation` | gm `_finish_proceed_and_continue`, `_retry_prepared_extraction` | **삭제** |
| `CommitCoordinator(` | 없음 | gm `_commit_ready_turn`, coordinator `recover_session` |
| `deduct_ink(` | gm continuation, session(캐시 오픈), system(!지급) | session, system (자동 턴 0) |
| `cost_to_ink` 청구/보고 | gm continuation, display 직전턴, ui 세션종료, cost 임베드(자동 턴) | Settlement 1회(설계상). 잔존: 예상치(estimate/session_flow/gm est), 캐시 오픈(WP-F), 압축 선결제(WP-F), 수동 경로 임베드 |
| `total_cost - cost_before` | gm continuation | 없음 |
| `CommitJournal`/`append_entry` | 0 | `core/commit_coordinator.py`만 |
| `build_turn_settlement(` / `SettlementStore` | 0 | coordinator만 |
| `execute_settlement_charges(` | 0 | coordinator만 |
| strict save | 0 | coordinator(`write_session_strict_locked`, breadcrumb/abandon `save_session_data_strict`) |
| 복원 진입 | `restore_sessions_from_disk`(저널 미조회) | 필드 복원 → **recover_session → abandon_retry_pending** → 캐시 → 등록 |
| 세션 종료 `ink_spent` | `cost_to_ink(total_cost)` 재계상 | 제거(세션 시간만) |
| tx 단위 원장 조회 | 프로덕션 0 | 프로덕션 0 (`list_cost_events(transaction_id=)`는 테스트만) |

## 4. 권위 커밋 순서 — 원문 발췌 (`core/commit_coordinator.py::_commit_locked`)

```python
TT.mark_transaction_status(session, tid, TT.TurnStatus.COMMITTING)
plan = build_commit_plan(session, tx, prep)
candidate = build_committed_settlement(self.bot, plan)   # 메모리 후보만
journal.append_entry(_entry(plan, cj.CommitPhase.PREPARED, settlement_id=plan.settlement_id,
                            metadata=plan.journal_metadata()))
snap = capture_rollback_snapshot(session, prep)
async with _io.session_io_lock(self.bot, session):
    session._commit_io_task = asyncio.current_task()
    try:
        rewind = await self._apply_in_memory(...)
        stage = "STRICT_SAVE"
        await _io.write_session_strict_locked(session)
        persisted = True
    except Exception as e:
        restore_rollback_snapshot(session, prep, snap)
...
journal.append_entry(_entry(plan, cj.CommitPhase.SESSION_PERSISTED, ...))
settlement, _created = await self._complete_from_persisted(...)   # Settlement → 되감기 → Ink → BILLING_APPLIED → COMMITTED
self._finalize_runtime(session, tid, prep)                          # 런타임 COMMITTED는 이 뒤에만
```

`_complete_from_persisted`:
```python
settlement = store.record_strict(cand)                      # E-1 최종 Settlement(영속 이후에만)
... _rw.record_delta / _rw.record_full_log → REWIND_RECORDED | _mark_rewind_degraded   # E-2
results = await _ink.execute_settlement_charges(settlement)  # E-3
journal.append_entry(... BILLING_APPLIED ...)
created = journal.append_entry(... COMMITTED ...)            # E-4
```

§6 순서 교정 준수: COMMITTED Settlement는 `SESSION_PERSISTED` 이후에만 영속되고, Ink 적용 전에 반드시 존재한다.

## 5. CommitPlan / exact ID → Settlement 발췌

```python
settlement_id=_st.settlement_id_for(prep.transaction_id, prep.attempt),
frozen_cost_event_ids=tuple(prep.frozen_cost_event_ids),    # WP-C 동결 튜플이 유일 입력
billing_user_ids=users,                                      # players 스냅샷 1회
baseline_marker=copy.deepcopy(getattr(session, "commit_marker", None)),
...
return _st.build_turn_settlement(cost_ledger=_ledger_of(bot), transaction_identity=plan.identity(),
    outcome=_st.SettlementOutcome.COMMITTED, cost_event_ids=plan.frozen_cost_event_ids,
    billing_user_ids=plan.billing_user_ids)
```
PREPARED metadata = CommitPlan 전체(+`plan_fingerprint`, `target_marker`) — 정체성·settlement_id·exact ID·청구 유저·
기준선/대상 신원·선언·판단 참조 digest·묘사 digest·메시지 ID·READY 지문 digest·작업 상태.

## 6. 롤백 스냅샷 + strict-save 실패

- `COMMIT_OWNED_FIELDS` = `CANONICAL_DOMAINS` ∪ {raw_logs, uncompressed_logs, current_turn_logs, extraction_pending,
  extraction_retry_ctx, gm_clarify/narrate_count, gm_side_note, _extraction_applied_tx, _narrative_replan_applied}.
  완전성 테스트: 실제 커밋 전후 직렬화 차이 ⊆ 이 집합 (`test_d_commit_owned_fields_cover_every_commit_write`).
- 적용+직렬화+원자 쓰기는 **같은 io 락 임계구역** — tolerant save가 적용 도중 상태를 끼워 쓸 수 없다.
  같은 태스크의 tolerant save는 교착 대신 no-op(가드: `session._commit_io_task`).
- 실패 → `restore_rollback_snapshot` → `_fail_pre_persist`: FAILED_SYSTEM Settlement(exact ID, 청구 0),
  런타임 FAILED_SYSTEM, 전달 출력 정리 + 재선언 안내, 다음 라운드. FAILED 정산 기록마저 실패하고 PREPARED가
  있으면 `commit_recovery`로 차단 후 재시도(미해결 시도를 남긴 채 진행하지 않음).

## 7. 재시작 복구 실행기 (`recover_session`)

| 저널 처분 | data.json `commit_marker` | 실행 |
|---|---|---|
| NO_JOURNAL / COMPLETE | — | 정상 복원 |
| PREPARED(±GAME_STATE) & FAILED_SYSTEM Settlement 존재 | — | 종결된 폐기 시도(skip) |
| PREPARED, SESSION_PERSISTED 없음 | = 대상 | SESSION_PERSISTED 수리 → 재개 |
| 〃 | = 기준선 | FAILED_SYSTEM Settlement(청구 0) → 해제 |
| 〃 | 둘 다 아님 | RECOVERY_REQUIRED |
| SESSION_PERSISTED, 청구 미완 | = 대상 필수 | Settlement 결정적 재구성·영속 → 같은 ink_tx_id 실행 → BILLING_APPLIED → COMMITTED |
| BILLING_APPLIED, COMMITTED 없음 | = 대상 필수 | Settlement strict 조회 + 전 유저 계정 마커 확인(없으면 RR, 재차감 금지) → 원장 복구 → COMMITTED |
| 손상/상충/다중 미완 | — | RECOVERY_REQUIRED 기록(가능 시) + 차단 |

일시 오류는 PENDING(차단 유지, 다음 선언 시 `_process_actions` 게이트가 같은 실행기로 재시도).
복원 순서: 필드·raw_logs 복원 → **recover_session → abandon_retry_pending** → 캐시 연동 → `active_sessions` 등록.

## 8. 연결 기록(WP-E 소비)

committed tx 정체성 → PREPARED metadata(CommitPlan) → `settlement_id` → Settlement / COMMITTED metadata(`plan_fingerprint`,
`story_turn`, `gm_turn`) / data.json `commit_marker` / rewind_log·full_logs 턴 = `gm_turn` / 메시지 ID·선언·판단 digest.
검증: `test_d_linkage_reconstructible_from_committed_identity`. 되감기 기록 실패는 `rewind_degraded_turns` +
REWIND_RECORDED 부재(저널 증거)로 표면화, 이야기 롤백 없음.

## 9. Settlement 파생 계정·보고·통계

- 계정: `execute_settlement_charges(settlement)`만(유저별 결정적 `ink_tx_id`, 1잉크 하한·운영자 부담 보존,
  하한 발생 시 기존 안내를 InkTransaction 사실에서 파생 방출).
- 미러: `total_ink_spent += charge`, `last_turn_cost = player_billable_cost_krw`, `last_turn_ink = charge` —
  이야기와 같은 strict 쓰기(재시도·복구 시 이중 증가 없음).
- 보고: `build_turn_cost_embed(..., settlement=)` — "1인당 N잉크 × M명"(재환산 없음), COMMITTED 이후 송출.
- 통계: `stats.bump(uid, ink_spent=charge)` COMMITTED 이후 파생. 세션 종료는 `session_seconds`만.

## 10. 실패 매트릭스 (§21) — 테스트

| 실패 지점 | 테스트 |
|---|---|
| PREPARED 이전(Settlement 검증) | `test_d_failure_before_mutation[settlement_validation]` |
| PREPARED append 실패 | `test_d_failure_before_mutation[prepared_append]` |
| 메모리 적용 예외 | `test_d_failure_during_apply_or_strict_save[apply_throws]` |
| strict 쓰기 실패 / 원자 교체 실패 | `…[write_fails]`, `…[replace_fails]` |
| data.json 교체 후 SESSION_PERSISTED 전 크래시 | `test_r_crash_after_replace_before_session_persisted_restart` |
| SESSION_PERSISTED append 실패(실행 중) | `test_r_session_persisted_append_fails_in_process_gate_recovers` |
| 영속 후 Settlement 영속 실패 | `test_r_settlement_persist_fails_after_persist` |
| 되감기/히스토리 기록 실패 | `test_r_rewind_append_failure_degrades_without_rollback` |
| 첫 유저 계정 strict 쓰기 실패 | `test_r_first_user_account_write_fails_then_restart` |
| A 청구·B 실패 | `test_r_user_a_charged_user_b_fails_replay` |
| 계정 기록 후 Ink 원장 실패 | `test_r_account_written_ledger_append_fails` |
| 청구 후 BILLING_APPLIED 전 크래시 | `test_r_crash_after_billing_restart_no_recharge[BILLING_APPLIED]` |
| BILLING_APPLIED 후 COMMITTED 전 크래시 | `…[COMMITTED]`, `test_r_committed_append_fails_in_process_finalizes_without_recharge` |
| COMMITTED 후 Discord / 통계 실패 | `test_r_derived_failures_after_committed_do_not_uncommit[discord|stats]` |
| 손상 저널 / 상충 Settlement / 마커 불일치 | `test_r_corrupted_journal_blocks_gameplay`, `test_r_conflicting_settlement_is_recovery_required`, `test_r_billing_applied_without_account_marker_is_recovery_required` |
| 사전 READY 종료 실패 FAILED_SYSTEM 정산 | `test_d_pre_ready_failure_persists_failed_system_settlement` |

## 11. 중복·동시·stale (§22)

`test_d_duplicate_commit_call_is_rejected`, `test_d_concurrent_duplicate_commit_calls`(gather 2회 → COMMITTED 1·REJECTED 1,
정본 1회·Settlement 1·Ink 1·COMMITTED 1), `test_d_stale_transaction_commit_rejected`(새 시도 불변),
`test_d_next_declaration_blocked_while_committing`.

## 12. 재시작 — 모든 durable phase

PREPARED+대상 → `test_r_crash_after_replace…` · PREPARED+기준선 → `test_r_restart_after_prepared_with_baseline_disk_discards` ·
SESSION_PERSISTED → `test_r_first_user…`/`…user_a…`/`…ledger_append…` · 청구 후 → `…[BILLING_APPLIED]` ·
BILLING_APPLIED → `…[COMMITTED]` · COMMITTED → `test_r_restart_after_complete_commit_is_noop` · 저널 없음 →
`test_r_restart_without_journal_is_clean`. 재시작은 새 FakeBot + `restore_sessions_from_disk`(구 tx/prep 미사용).
RETRY_PENDING: `test_r_retry_pending_restart_abandons_with_exact_claimed_ids`, 보존 `…in_process_retry_still_same_attempt`.

## 13. Preservation 증거

- WP-C 배리어 전 테스트(`test_ready_barrier.py` 전부, B-C1~B-C7 포함) 통과 — 단언 변경은 legacy `deduct_ink` 기록을
  Settlement 청구 기록으로 바꾼 rig 한 곳과 tc05/tc12/tc26의 owner 단언뿐.
- 늦은 usage 관측(B-C5 timeout→cancel 포함) 불변 — `core/resilience.py`/`cost_ledger.py` 무변경.
- Settlement/Ink/Journal/strict-save/accounts foundation 테스트 전부 통과(스키마 무변경).
- 수동·인트로 `_execute_proceed`(`test_tc28`, narration boundary), `_plan_narrative`(init/manual — derived=None 기존 의미),
  수동 추출 재시도 경로 불변.
- 다중 플레이어 동일 명목·분할 없음·하한/보조금 — `test_d_multiplayer_full_nominal_charge_each`.

## 14. 갱신한 기존 테스트(owner 이전의 의도적 반영)

`test_c001`(순서: READY→strict→되감기→Settlement 청구→저장), `test_d004`(스냅샷 이월 owner = `_apply_in_memory`),
`test_d002`/`_patch_common`(owner = `_commit_ready_turn`), `test_29`(coordinator를 foundation 조합 owner로 제외),
`test_p001`, `test_21`(저널 owner = coordinator 단 하나), `test_70`(gm deduct 0, cogs 2), `test_s8`, rig·tc05/tc12/tc26.

## 15. xfail (불변 3건)

`d004c` — AUD-020(WP-E), `d005d` — AUD-029(WP-E), `d006e` — AUD-034/035(WP-F). XPASS 없음.

## 16. 범위 밖 유지

WP-E 되감기/재생성 의미·AUD-029, WP-F 캐시/압축 회계(세션 오픈 `deduct_ink`, 압축 선결제 표시 포함),
메시지 수명주기 전면 재설계, WP-G 프롬프트/시나리오/레거시 명령, HUD. `verify_docs` 기존 불일치(core 서브모듈 수)는
체크포인트 규정에 따라 미수정.

## 17. 발견 사항 / 잔여

- **D-1** 자동 턴 비용 임베드가 READY 이전·독자 환산이었음 → COMMITTED 이후 Settlement 값으로 이전(해결).
- **D-2(운영 위험)** Settlement strict 리더가 전역 `data/cost_ledger.jsonl`(shadow tolerant 기록) 전체를 strict 파싱.
  과거 손상 라인이 있으면 모든 턴이 fail-safe FAILED_SYSTEM(청구 0)이 된다. 운영 원장 점검 또는 원장 분할은 후속 결정.
- **D-3** 저널에 폐기 종결 phase가 없어, 영속 전 폐기 시도의 종결 사실 = 같은 settlement_id의 FAILED_SYSTEM Settlement.
- **D-4** 기준선/대상 판정은 data.json `commit_marker`(strict 커밋과 원자 영속)로 한다.
- **D-5** SUPERSEDED 시도 비용은 Settlement를 만들지 않음(재생성 청구는 WP-E). RECOVERY_REQUIRED 세션의 운영자 해제
  도구 없음(세션 차단 유지, 증거 보존).
- **D-7** RETRY_PENDING 재시도 진행 중 크래시 시 그 재시도 호출의 CostEvent는 breadcrumb 이후라 FAILED Settlement 밖에
  남는다(청구 0 영향 없음, 운영 사실로 보존).
- **D-8** `_retry_prepared_extraction` 경로의 "released"는 이제 exact ID FAILED_SYSTEM 종결을 먼저 수행한다.

## 18. 종료 판정 AUD

AUD-019(durable unlock) · AUD-021(exact 멤버십 → Settlement 청구) · AUD-022(보고 = Settlement) · AUD-028(세션 종료 이중 계상
제거) · AUD-031(권위 결과 이전 차감 없음) · AUD-032(운영 이력 append-only·게임 롤백과 분리) — WP-D 범위 종결 후보.
AUD-030 정책 보존. WP-C F2(`m_send` NameError 경로) legacy 루프와 함께 소멸, F4 exact ID 권위 적용.
미종결 유지: AUD-020/029(WP-E), AUD-034/035(WP-F).

## 19. Hard stop

WP-D 구현 후 정지. **WP-E는 시작하지 않았다.** 독립 GPT 게이트 대기.
