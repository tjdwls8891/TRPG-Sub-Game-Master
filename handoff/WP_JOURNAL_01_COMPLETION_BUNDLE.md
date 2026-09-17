# WP-JOURNAL-01 COMPLETION BUNDLE
## CommitJournal Foundation / Idempotent Recovery Scaffold

> 장기 핸드오프 기록(§15). 현재 GPT 게이트는 최종 채팅 응답이 증거면이다(§16).
> 이 문서는 이후 AI 세션이 소비할 저장소 산출물이다.

---

## A. Package / gate summary

WP-JOURNAL-01 은 authoritative commit 이전에 필요한 **내구성 커밋 저널 프리미티브
+ 복구 분류 스캐폴드**만 추가하는 파운데이션 패키지다. 저널을 라이브 턴
파이프라인에 배선하지 않으며(§11/§12/§14), 프로덕션 턴 동작은 행위상 불변이다.
authoritative commit / narration split / settlement / rewind 등 후속 스코프는
시작하지 않았다(hard stop).

## B. Baseline and final Git identity

- Baseline(시작 체크포인트, WP-PERSIST-01): `fea4e4f726271810e7d30782919518781444598f`
- Branch: `claude/wp-journal-01-foundation`
- Baseline frontier: `124 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS`
- Final frontier: `147 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS`
- Final SHA: (최종 커밋 SHA — 커밋 직후 최종 채팅 응답 §Git checkpoint 참조)

## C. Changed files / functions / signatures

신규:
- `core/commit_journal.py` — 저널 모듈(모델/페이즈/append/reader/분류기/예외).
- `tests/policy/test_commit_journal.py` — §13 의 24개 시나리오(테스트 23함수, 20/21/22 포함).
- `handoff/WP_JOURNAL_01_COMPLETION_BUNDLE.md` — 본 문서.

수정:
- `core/__init__.py` — `from . import commit_journal` 1줄(중립 모듈 노출, cost_ledger 선례와 동일).

공개 표면(요지):
```
class CommitPhase(str, Enum)            # PREPARED..RECOVERY_REQUIRED (7개)
class RecoveryDisposition(str, Enum)    # NO_JOURNAL / DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT
                                        # / RESUME_BILLING / VERIFY_AND_FINALIZE / COMPLETE / RECOVERY_REQUIRED
@dataclass(frozen=True) class JournalEntry   # to_dict()/from_dict() 포함
class CommitJournalError(RuntimeError)
class CommitJournalPersistenceError(CommitJournalError)   # append 내구성 실패
class CommitJournalCorruptionError(CommitJournalError)    # malformed 이력
make_idempotency_key(transaction_id, attempt, phase) -> str
default_journal_path(session_id) -> str          # sessions/{id}/turn_commit_journal.jsonl
new_entry(*, transaction_id, session_id, logical_turn, attempt, phase, ...) -> JournalEntry
entry_from_transaction(tx, phase, **kw) -> JournalEntry
classify_disposition(entries) -> RecoveryDisposition          # 순수 함수
class CommitJournal:
    __init__(self, path)
    append_entry(entry) -> bool
    list_entries(*, transaction_id=None, attempt=None) -> list[JournalEntry]
    latest_entry(*, transaction_id=None, attempt=None) -> JournalEntry | None
    history_for_transaction(transaction_id, attempt=None) -> list[JournalEntry]
    classify_recovery(transaction_id, attempt) -> RecoveryDisposition
    has_idempotency_key(key) -> bool
```
프로덕션 호출자: **0**. 위 심볼은 import/구성 유틸/테스트에서만 참조된다.

## D. Pre / post journal reference scan

- Pre: `CommitJournal`/`turn_commit_journal`/`JournalEntry`/`PREPARED`/`RECOVERY_REQUIRED`
  매치는 전부 핸드오프 완료 문서(§"not started")에만 존재. 구현 없음.
- Post: `core/`+`cogs/` 에서 구성/호출 패턴(`CommitJournal(`, `.append_entry(`,
  `.classify_recovery(`, `commit_journal.new_entry` 등) **0건**. 프로덕션 참조는
  `core/__init__.py` 의 중립 import 1줄과 `core/io.py:471` 의 기존 주석뿐.

## E. Model / API contract

`JournalEntry`(frozen, JSON 직렬화). 정체성 필드: `transaction_id, session_id,
logical_turn, attempt, phase`. 기타: `idempotency_key, completed_steps,
settlement_id(None 허용), error_code, entry_id, schema_version(=1), timestamp,
metadata`. mutable Session/Discord/task 참조 없음. `from_dict` 는 필수 필드/phase/
정수 검증에 실패하면 조용히 기본값으로 때우지 않고 `CommitJournalCorruptionError`.

## F. Append / idempotency contract

`idempotency_key = <transaction_id>:attempt:<attempt>:phase:<phase>` — 불변 커밋
정체성에서만 결정적 파생(벽시계/응답해시/무작위 UUID/가변 카운터 아님, §7.1).
check→append→flush→fsync→dedup갱신 전체가 경로 단위 공유 `threading.Lock` 하의
단일 임계구역(CostLedger 패턴 계승, §7.2). 같은 경로의 서로 다른 CommitJournal
인스턴스도 lock/keys 를 공유하므로 동일 키는 파일에 한 줄만 남는다. dedup 키는
내구 성공 후에만 등재된다.

## G. Durability / failure behavior

성공 append 는 `write → flush → os.fsync` 내구성 경계를 지난다(§7.3). 실패 시
삼키지 않고 `CommitJournalPersistenceError`(원인 `__cause__` 보존)로 올리며, 부분
기록은 실패 지점 이전 크기로 롤백한다(`_truncate_to`). 실패한 append 는 dedup 키에
등재되지 않는다 → '성공한 척' 하지 않는다.

## H. Corruption behavior

reader(`_read_all_locked` → `list_entries`/`latest_entry`/`history_for_transaction`/
`classify_recovery`)는 malformed 라인을 조용히 skip 하지 않고
`CommitJournalCorruptionError` 로 구분한다(§8.1). 구분: 부재/빈 파일 → `[]`(NO_JOURNAL),
유효 → 이력, malformed(JSON 파싱 실패 또는 구조 무효) → 손상 예외, append 내구성
실패 → 영속화 예외. 이 패키지는 자동 복구/절단 정책을 도입하지 않는다.

## I. Recovery classification table

| 이력 상태 | RecoveryDisposition | 스펙 |
|---|---|---|
| 이력 없음 | NO_JOURNAL | §9.1 |
| PREPARED / GAME_STATE_APPLIED_IN_MEMORY (SESSION_PERSISTED 미도달) | DISCARD_OR_RETRY_UNPERSISTED_ATTEMPT | §9.2 |
| SESSION_PERSISTED (BILLING 미도달) | RESUME_BILLING | §9.3 |
| BILLING_APPLIED (COMMITTED 없음) | VERIFY_AND_FINALIZE | §9.4 |
| COMMITTED | COMPLETE | §9.5 |
| RECOVERY_REQUIRED | RECOVERY_REQUIRED | §9.6 |

판정은 '도달한 내구성 마일스톤' 집합으로 하며 경직된 상태기계를 만들지 않는다(§10).
REWIND_RECORDED 등 비결정 마커는 처분 경계가 아니다. 마일스톤 없이 비결정 마커만 있는
비정상 이력은 명시적 RECOVERY_REQUIRED 로 표면화한다. 분류기는 순수(부수효과 없음):
청구/세션변이/Settlement 접근 없음.

## J. Strict-persistence composition proof

`test_20_composes_with_strict_save`: 격리 세션 + tmp 저널로
`append(PREPARED) → save_session_data_strict(bot, session) → append(SESSION_PERSISTED)`
를 실행하고, 이력 순서와 `RESUME_BILLING` 분류를 확인한다. strict 프리미티브는
재구현하지 않고 그대로 사용한다. **라이브 오케스트레이터에 배선하지 않음**(§11).

## K. Exact tests / results

- 신규: `tests/policy/test_commit_journal.py` — 23 tests(§13 시나리오 1–22 + 순수성 19b).
- 전체: `147 passed, 9 xfailed, 0 failed, 0 skipped, 0 XPASS`.
- 9 xfail(AUD-034/035, AUD-011, AUD-024×2, AUD-020, AUD-029, AUD-012/019×3) 불변.

## L. Compile / import

`python -m py_compile core/commit_journal.py core/__init__.py tests/...` OK.
`python -c "import core; import core.commit_journal"` OK.

## M. Known defects intentionally untouched

9개 strict xfail(캐시 회계 단일 정산점, 추출 경계, 지시 부수효과 롤백, 되감기 회계,
턴 커밋 경합/배리어)은 그대로 유지. 이들의 수리는 후속 Settlement/commit-integration/
cache-lifecycle 패키지 소관.

## N. New findings / blockers

- 없음(BLOCKED 아님). 소스가 핸드오프와 정합.
- 관찰: 세션 저장(`core/io.py::_atomic_write_session`)은 tmp+os.replace 원자성은 갖되
  fsync 는 하지 않는다. 저널은 자체 fsync 내구성을 구현(세션 저장과 독립). 세션 저장의
  fsync 도입은 본 패키지 스코프 밖.

## O. Proposed audit updates (GPT 가 게이트 후 반영)

- WP-JOURNAL-01 체크포인트를 accepted 로 등재(최종 SHA).
- 저널 프리미티브가 추가되었으나 authoritative commit 은 여전히 미구현임을 명시.
- `AUDIT_LEDGER.md`/`AUDIT_STATE.json`/`MASTER_ROADMAP.md` 는 이 세션에서 수정하지 않음(§15).

## P. Hard-stop confirmation

authoritative commit 통합 및 이후 패키지(narration split, staging, concurrency
barrier, Settlement/InkTransaction, rewind/rerender, message lifecycle, cache
migration, legacy removal, prompt cleanup, HUD)를 **시작하지 않았다**. 다음 행동은
독립 GPT 게이트 감사다.
