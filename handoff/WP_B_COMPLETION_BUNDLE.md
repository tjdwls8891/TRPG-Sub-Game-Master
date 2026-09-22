# WP-B — Transactional State Preparation · Completion Bundle

> 독립 GPT 게이트 검토용 증거 묶음. 비밀(토큰)은 포함하지 않는다.
> 이 패키지는 **AI-파생 자동턴 gameplay 변이**에 스테이징/계획 경계를 세우는 것에 한정된다.
> 권위적 commit·barrier·정산 배선(WP-C/D 이상)은 착수하지 않았다.

---

## 1. 패키지 식별 / Git identity
- Package: **WP-B (Transactional State Preparation)**
- Branch: `claude/wp-b-transactional-state-preparation`
- Start SHA (WP-A tip): `a488f421a85028589556033be93d749bb0bfc081`
- Final SHA: `2c5d62c9c386ce99f37a6919dc5034d000107f6e`
- Push / local == remote / `git status --short`: **§20에서 push 후 기재** (아래 최종 절차에서 확정).

### 중간 커밋
| SHA | 내용 |
|---|---|
| `b583f31` | b3: core/turn_preparation.py 스테이징 모델(PendingInstructionEffects, info_ledger 순수병합, 통합 quest-choice, 단일 idempotent applier) + policy 테스트 |
| `3c43a42` | b4/b5: 지시 스테이징 배선 + quest projection; AUD-024 xfail 2건 전환 |
| `967263e` | b6: 레거시 자:/태: 직접 resource/status 변이 권위 제거(AUD-001) + T-B06/07 |
| `2c5d62c` | b7-b12: 추출 result-only + 검증 계획 + stale guard + idempotent 호환 경계; D-001 전환 + T-B17/B19/B21 |

---

## 2. 기준/최종 회귀 (Baseline / final)
- **Baseline (start SHA, WP-A tip):** `291 passed, 9 xfailed`
- **Final full suite:** `311 passed, 6 xfailed` (failed 0 / error 0 / skipped 0 / **XPASS 0**)
- xfail 순증: 없음. **xfail 3건 전환(9→6)** — 의도한 결함이 수정되어 통과로 전환:
  - `test_d003c` (AUD-024 지시효과 누출) → 스테이징 모델로 통과
  - `test_d003d` (AUD-024 info_ledger 누출) → 통과
  - `test_d001b` (AUD-011 전체 묘사 미도달) → 통과
- **남은 6 xfail은 전부 상위 WP 소관**(WP-B가 건드리지 않음):
  - `test_d002c/d/e` (AUD-012/019 커밋 배리어) — WP-C/D
  - `test_d004c` (AUD-020 되감기 추출 보존) · `test_d005d` (AUD-029 제공자 이력) — WP-E
  - `test_d006e` (AUD-034/035 캐시 단일 정산점) — WP-F

targeted 대표 실행: `test_turn_preparation`(정책), `test_extraction_staging`(정책, T-B17/19/21),
`test_instruction_side_effects`(결함), `test_extraction_boundary`(결함), `test_narration_boundary`(정책, T-B06/07) 모두 green.

---

## 3. 변경 파일 (사유 1줄)
| 파일 | 사유 |
|---|---|
| `core/turn_preparation.py` (신규) | 트랜잭션-로컬 스테이징/검증 모델: 지시효과 스테이징·단일 applier·quest projection·info_ledger 순수병합·추출 검증계획(ExtractionMutationPlan)·stale guard·narrative progress 단일 owner |
| `core/__init__.py` | `from . import turn_preparation` 등록 (그 외 무변경) |
| `cogs/gm.py` | 지시 적용을 _call_gm_logic 밖으로 이동→스테이징; 묘사 성립 후 단일 경계에서 apply; logic 프롬프트가 projected quest 사용; 추출에 전체 묘사·트랜잭션 정체성 전달; _run_extraction 적용부를 stale/idempotent 단일 호환 경계로 표시; narrative_plan progress를 단일 owner로 라우팅 |
| `cogs/game.py` | 레거시 자:/태: 직접 resource/status 변이 두 루프 제거(방어적 strip 보존); 묘사 프롬프트가 staged quest projection 사용 |
| `core/prompt.py` | build_prompt/add_quest_block에 read-only `quest_projection` threading |
| `core/quest.py` | `_ProjectionView`(quest_state 복제·canonical write 거부), `clone_state`, `projection_view`, `build_quest_block(..., quest_state=)` 추가 |
| `tests/policy/test_turn_preparation.py` (신규) | 스테이징 순수성·단일 applier·info_ledger 병합 정책 테스트 |
| `tests/policy/test_extraction_staging.py` (신규) | T-B17 dedup / T-B19 stale / T-B21 idempotent |
| `tests/policy/test_narration_boundary.py` | WP-A 태그보존 특성화 → T-B06/T-B07 중립화 증명으로 전환 |
| `tests/defects/test_instruction_side_effects.py` | D-003 롤백 모델 → 스테이징 모델(성공·실패)로 재작성 |
| `tests/defects/test_extraction_boundary.py` | D-001 500자 절단 특성화 → 전체묘사 수정동작으로 전환 |

---

## 4. 편집 전 변이 인벤토리 (§8)
> 자동턴 AI-파생 변이만 스테이징 대상. 수동/운영자/설정/표시/회계는 스테이징하지 않음.

| 변이/기록 지점 | 도메인/필드 | 스코프 | 당시 타이밍 | 당시 owner | AI-파생? | WP-B 조치 | 대상 owner |
|---|---|---|---|---|---|---|---|
| `_call_gm_logic`→`apply_choice` | quest active/node | 자동 gameplay | logic 도중(묘사 전) | 지시층위 | 예 | 스테이징 후 성립시 적용 | turn_preparation 스테이징 + 단일 applier |
| `_call_gm_logic`→`set_intended_case` | quest intended_case | 자동 gameplay | logic 도중 | 지시층위 | 예 | 스테이징 | turn_preparation |
| `_call_gm_logic`→`_update_info_ledger` | info_ledger | 자동 gameplay | logic 도중 | 지시층위 | 예 | 순수병합 스테이징 | turn_preparation `_merge_info_ledger` |
| `_apply_quest_choice` (중복 경로, AUD-005) | quest active | 자동 gameplay | loop | 지시층위(2번째) | 예 | 제거→단일 owner 통합 | turn_preparation |
| `_execute_proceed` 자:태그 루프 | resources | 자동+수동 공유 | 묘사 직전 | 셸 파서 | 예(자동턴) | 직접변이 제거(AUD-001) | 추출+코드검증 |
| `_execute_proceed` 태:태그 루프 | statuses | 자동+수동 공유 | 묘사 직전 | 셸 파서 | 예(자동턴) | 직접변이 제거 | 추출+코드검증 |
| `_dispatch_proceed` 추출입력 `ai_summary[:500]` | (추출 입력) | 자동 gameplay | 묘사 후 | 셸 | 예 | 전체 묘사 전달(AUD-011) | result['ai_text'] |
| `_dispatch_proceed` narrative_plan progress 쓰기 | narrative_plan.current_event.progress | 자동 gameplay | 묘사 후 | 셸 | 예 | 단일 owner 경유 | `apply_narrative_progress` |
| `_run_extraction` to_world_timeline/quantify | world_timeline | 자동 gameplay | 추출 후(async) | 추출 소비부 | 예 | 단일 호환 경계+stale/idempotent | (WP-D가 commit 뒤로 이동) |
| `_run_extraction` places.mark_visited/release_resident_companions | visited/companions | 자동 gameplay | 추출 후 | 추출 소비부 | 예 | 경계 내부로 감쌈 | 동일 |
| `_run_extraction` apply_companions | companions | 자동 gameplay | 추출 후 | 추출 소비부 | 예 | 경계 내부 | 동일 |
| `_run_extraction` apply_extraction | statuses/resources/npcs | 자동 gameplay | 추출 후 | 추출 소비부 | 예 | 경계 내부(merged-status 검증 보존) | 동일 |
| `_run_extraction` advance_quest/check_secret/check_main_unlock/pending_ending | quest/secret/ending | 자동 gameplay | 추출 후 | 추출 소비부 | 예 | 경계 내부 | 동일 |
| `_run_extraction` select_bgm→pending_bgm | pending_bgm | 자동 gameplay | 추출 후 | 추출 소비부 | 예 | 경계 내부 | 동일 |
| `!퀘스트 열기`(`start_quest`, gm.py:1853) | quest active | **운영자/admin** | 명령 | 운영자 명령 | 아니오 | **스테이징 안 함(스코프 분리)** | 유지 |
| `_plan_narrative` 재수립(3994/4006/4220) | narrative_plan 전체 | planning AI operation | 별도 오퍼레이션 | 서사설계 | 예(별도) | **WP-B 경계 밖(§41 유지)** | 유지 |

---

## 5. 편집 후 소유권 맵 (도메인별)
| 도메인 | producer | staged 표현 | validator | 호환 consumer | canonical mutator | stale guard |
|---|---|---|---|---|---|---|
| 지시 quest-choice | `_call_gm_logic`(decision 반환) | `PendingInstructionEffects.quest_choice` | `stage_instruction_effects`(narrative_mode/offered/random) | `apply_instruction_effects` (gm.py:1441, 묘사성립 후) | `core.quest.apply_choice`(단일) | 해당 트랜잭션 pending에 귀속(트랜잭션-로컬) |
| 지시 intended_case | 동상 | `PendingInstructionEffects.intended_case` | 동상 | 동상 | `set_intended_case` | 동상 |
| info_ledger | 동상 | `PendingInstructionEffects.info_ledger_merge` | `_merge_info_ledger`(순수) | 동상 | 병합 결과 대입 | 동상 |
| 자:/태: 태그 | (없음 — 권위 제거) | — | — | — | **없음(제거)** | — |
| narrative progress | `_dispatch_proceed` ai_summary | (직접) | `apply_narrative_progress`(current_event 가드+150자) | 동함수 | narrative_plan.current_event.progress | 묘사 성립 이후 지점 |
| 추출(위치/상태/자원/동행/NPC/퀘스트/이면정보/BGM) | provider+parse(result-only) | `ExtractionMutationPlan` | `build_extraction_plan`(등록캐릭터·merged-status·dedup·conflict) + mutator 내부 임계검증 | `_run_extraction` 내 **단일 호환 경계**(gm.py 3312–3498) | to_world_timeline/places/apply_companions/apply_extraction/advance_quest 등(재사용) | `extraction_is_stale`(§26/§38) + `_extraction_applied_tx` idempotency |

---

## 6. 스테이징/결과 인터페이스
- `PendingInstructionEffects` — quest_choice / intended_case / info_ledger_merge / projected_quest_state / narrative_progress. 트랜잭션-로컬(`TurnTransaction.instruction_result` 슬롯에 귀속).
- `stage_instruction_effects(session, decision, *, transaction_id)` → canonical 무변경, pending 반환.
- `apply_instruction_effects(session, pending)` → 묘사 성립 후 1회 idempotent 적용; `{applied, quest_action, quest_active_name, quest_reason}` 반환.
- `ExtractionMutationPlan` — transaction_id/logical_turn/attempt/result/entries/conflicts/diagnostics/applied/rejected_stale.
- `build_extraction_plan(session, result, *, transaction_id, logical_turn, attempt)` → result-only 검증·정규화 계획.
- `extraction_is_stale(session, *, logical_turn, attempt)` → 더 새로운 논리 시도 활성 시 True.

---

## 7. Projection 결정
- **quest_state 투영 = 방안(b)**: canonical 임시변경 없이 `projected_quest_state`(스테이징된 quest 선택 반영)를 read-view로 전달.
  - logic 프롬프트: `build_quest_block(session, quest_state=projected_quest_state(session))` (gm.py `_build_logic_user_prompt`).
  - 묘사 프롬프트: `PromptBuilder.build_prompt(..., quest_projection=pending.projected_quest_state)` (game.py `_generate_narration`).
  - `_ProjectionView`는 quest_state만 복제하고 `_quest_offered` 등 forward-write scratch는 실제 세션에 위임하며 그 외 canonical write는 거부 → canonical 무변이.
  - 스테이징 없으면 projection=None → canonical get_state와 **등가**(before/after 프롬프트 quest 맥락 동일).
- **자:/태: 자동턴 투영 안 함**: canonical 미변경 + 묘사 projected에도 미반영. 파싱/방어적 strip만 보존.
- **info_ledger 투영 불필요**(AUD 결정): 동일 프롬프트 경로에서 별도 read-view 소비처 없음.

---

## 8. 호환 적용 경계 (열거·최소)
자동턴 AI-파생 canonical 적용 지점은 **정확히 다음뿐**:
1. **지시효과**: `apply_instruction_effects` — gm.py:1441 (`_finish_proceed_and_continue`, 묘사 성립 직후; 실패 경로는 그 이전 return).
2. **narrative progress**: `apply_narrative_progress` — `_dispatch_proceed`(묘사 성립 후 ai_summary 존재 시).
3. **추출효과**: gm.py **3312–3498** `▼▼▼ WP-B 단일 호환 적용 경계 ▼▼▼ … ▲▲▲ 끝 ▲▲▲` — 내부에서만 추출 mutator 호출(경계 밖 호출 0건, §14 스캔).
경계는 주석으로 명시되어 WP-D가 이동/치환 가능. **authoritative commit 아님**.

---

## 9. 전체-묘사 추출 증거 (AUD-011)
- `_dispatch_proceed`: `_full_narration = (result or {}).get("ai_text") or ai_summary` → `_run_extraction(session, _full_narration, ...)`.
- 구조 특성화 `test_d001c`: `_dispatch_proceed` 본문에 `ai_text` 존재 + `_run_extraction(session, ai_summary` 부재.
- 행위 `test_d001`/`test_d001b`: 500자 경계 뒤 사실(LATE_FACT)이 추출 입력에 도달(전체 == narration).
- (내부 `ai_output_text[:3000]`은 추출 프롬프트 크기 제어로 **보존** — 500자 요약 절단(AUD-011)과 별개의 통제이며, 일반 묘사 길이를 포괄. 아래 findings에 명시.)

## 10. 변이-계획/검증 증거
- `build_extraction_plan`: 등록 캐릭터·merged-status 목록으로 1차 검증(무효 status/캐릭터 drop → diagnostics), 동치 중복 제거(seen set, T-B17), 상호 모순(동행 join&leave) conflict 진단.
- `test_b17_equivalent_effects_deduped`(npc_met/companion 중복 1건화), `test_b17b_join_and_leave_conflict_flagged`.
- 임계 비교(status_apply 등)·소지품 정산은 기존 mutator가 경계 내부에서 수행(권위 검증 보존, P-B08).

## 11. 파생 효과 증거
- plan entries 도메인 실제 존재: `location(move)`, `status(score)`, `item(delta)`, `npc_met(meet)`, `companion(join/leave)`, `quest(progress)`, `secret(awareness)`.

## 12. 호환 applier + stale/idempotent 증거
- `extraction_is_stale`: active=None(커밋 후 새 턴 없음)→False(정상 적용), active의 (logical_turn,attempt)가 추출보다 큼→True(거부). `test_b19_extraction_is_stale_decision`.
- 행위 `test_b19b_stale_extraction_applies_no_mutation`: 더 새로운 논리 턴 활성 시 world_timeline·resources **무변화**, 반환 None.
- idempotency `test_b21_compatibility_apply_at_most_once`: 동일 transaction_id 재적용 시 additive 자원이 **이중 반영 안 됨**(`_extraction_applied_tx` 가드).

---

## 13. 지시 스테이징 증거 (§H)
- 직접 변이 제거: `test_d003`(AST) — `_call_gm_logic` 본문에 `apply_choice`/`_update_info_ledger` 부재 + src에 `stage_instruction_effects` 존재.
- 묘사 실패 행위: `test_d003c`/`test_d003d` — 스테이징 후 `_dispatch_proceed`가 None(실패) 반환 시 canonical(quest active/info_ledger) **무변화**; `test_d003c2` — 성립 시 적용.

## 14. 호출/변이 스캔 (§39 8-증명, 전부 PASS)
1. instruction producer가 quest/info canonical mutator 직접호출 **안 함** — `_call_gm_logic`에 apply_choice/start_quest/_update_info_ledger/_apply_quest_choice/merge 부재.
2. 자:/태: 파서가 resources/statuses 직접변이 **안 함** — `_execute_proceed`에 session.resources[char]/session.statuses[char]/res_tags/status_tags 부재.
3. 추출 producer/parse 구역(3168–3311)에 gameplay 필드 쓰기 **없음**(통제 플래그·비용만).
4. 자동턴 AI 효과가 staged/plan 경유 — 지시=stage/apply, 추출=plan+guarded, progress=단일 owner, 자:태:=중립화.
5. 호환 적용 사이트 **열거·최소**(위 §8: 3개).
6. 두 번째 quest-choice 자동 owner **없음** — `_apply_quest_choice` def 0건. (gm.py:1853 `start_quest`는 `!퀘스트 열기` **운영자 명령**, 별개 제품 의미 — P-B10.)
7. raw 모델 추출 result가 경계 **밖** 불투명 mutator로 전달 **안 됨** — apply_extraction/apply_companions/advance_quest 호출 전부 3312–3498 내부.
8. prompt/scenario/data 파일 **무수정** — diff에 .json/scenario/data/prompt-text 없음.

## 15. 금지 범위 스캔 (§P / S10) — WP-C/D/E/F/G cutover 없음
- READY_TO_COMMIT/join_barrier: 변경분 유일 매치는 turn_preparation.py **범위 제외 설명 주석**(“이 모듈이 하지 않는 것”)뿐 — 기능 도입 아님.
- CommitJournal/Settlement/InkTransaction **live caller 추가 0건** (`^\+.*commit_journal\.` 매치 없음). `__init__.py` 변경은 `+from . import turn_preparation` 한 줄; commit_journal/settlement import는 기존 WP-A 것.
- `core/turn_transaction.py` 이 브랜치에서 **무변경**(TurnStatus에 새 상태 추가 없음).
- rewind/cache 마이그레이션 없음(P-B17/P-B18): 해당 xfail 6건 그대로 유지.

---

## 16. Findings 상태
| AUD | 상태 | 근거 |
|---|---|---|
| AUD-001 (자:/태: 이중 상태권위) | **닫힘** | 직접변이 제거, 권위 추출+코드검증 단일화. T-B06/07 |
| AUD-005 (중복 quest-choice owner) | **닫힘** | `_apply_quest_choice` 제거, 단일 스테이징 owner |
| AUD-011 (추출 입력 500자 절단) | **닫힘** | 전체 묘사 전달. test_d001/b/c |
| AUD-024 (묘사 실패 시 지시효과 누출) | **닫힘** | 스테이징+성립후 단일경계 적용. test_d003c/d |
| AUD-014 (merged-status 검증) | **보존(유지)** | extraction.py 285–312 검증 그대로. P-B08 |
| AUD-012 / AUD-019 (커밋 배리어·실패청구) | **열림 — WP-C/D** | test_d002c/d/e xfail 유지 |
| AUD-020 / AUD-029 (되감기·제공자 이력) | **열림 — WP-E** | test_d004c/d005d xfail 유지 |
| AUD-034 / AUD-035 (캐시 단일 정산점) | **열림 — WP-F** | test_d006e xfail 유지 |
| AUD-019(추출 stale 부분완화) | **부분** | stale 거부 추가(§26 명시대로 완전해결 아님) |

## 17. 새 findings / blocker
- **blocker 없음.** handoff BLOCK 조건 미발생.
- 관찰: `_run_extraction` 내부 `ai_output_text[:3000]`은 추출 **프롬프트 크기 통제**로 보존. AUD-011의 500자 요약 절단(추출이 묘사 대부분을 못 보던 문제)과 별개이며, WP-D에서 barrier/commit 재배치 시 함께 재검토 대상으로 남긴다.
- 관찰: 추출 적용부는 기존 도메인 mutator를 **재사용**(재작성 아님, 디렉터 지시). rule-7 취지는 “검증된 계획 존재 + 단일 표시 경계”로 충족하되, mutator 입력의 완전 정규화 위임은 WP-D 소관으로 남긴다.

---

## 18. 예약 목록 증거 (P-B01~P-B22)
- **P-B01/02** TurnTransaction 정체성·ROLL stale guard: turn_transaction.py 무변경, 연속성 테스트 green.
- **P-B03/04/05** 묘사 생성·전달·PC 자율성 경계: game.py 생성/전달 분리·검증 보존, test_narration_boundary green.
- **P-B06/07** provider CostEvents·retry: 비용/재시도 로직 무변경, 추출 재시도는 같은 attempt(§27). T-B20 의미 보존.
- **P-B08** merged status 검증: extraction.py 285–312 보존.
- **P-B09** 추출 증거 권위(전체 묘사=사실, 목록=유효성): 전체 묘사 전달 + 목록 검증 유지.
- **P-B10** 수동/admin 분리: `!퀘스트 열기` 미스테이징(§8/§14.6).
- **P-B11/12** prompt/scenario 무변경: diff 확인.
- **P-B13** 서사 로그/턴 commit 타이밍: raw_logs/turn_count 등 셸 소유 그대로(이동 없음).
- **P-B14** 청구 권위: 레거시 유지(WP-D까지).
- **P-B15/16** CommitJournal/Settlement/Ink live·async barrier: 없음.
- **P-B17/18** rewind/output 서비스: 미이동.
- **P-B19** 성공 자동턴 등가 효과: 스테이징/타이밍 교정만 반영, 적용 결과 동등(정책 테스트 + 회귀 green).
- **P-B20** 전역 scratch 권위 없음: preparation은 트랜잭션-로컬(instruction_result 슬롯).
- **P-B21** 엄격 xfail 감사: **XPASS 0**, 전환 3건은 의도적.
- **P-B22** 재무/영속 기반: live 호출 없이 green 유지.

---

## 19. 게이트용 소스 발췌 색인 (§45 S1–S10)
- S1 지시 producer 순수: `cogs/gm.py` `_call_gm_logic` → `return decision`(직접 canonical 없음).
- S2 스테이징 projection: `core/quest.py` `projection_view`/`_ProjectionView` + `build_quest_block(..., quest_state=)`.
- S3 태그 권위 제거: `cogs/game.py` `_execute_proceed` 중립화 주석 + 루프 부재.
- S4 전체 추출 입력: `cogs/gm.py` `_dispatch_proceed` `_full_narration`.
- S5 result-only 경계: `cogs/gm.py` `_run_extraction` 3312 경계 위 파싱 반환.
- S6 검증/계획: `core/turn_preparation.py` `build_extraction_plan`.
- S7 파생 효과: 동함수 `_add(domain,...)` entries.
- S8 stale guard: `extraction_is_stale` + 3324~ 경계 가드.
- S9 quest 단일 owner: `stage_instruction_effects`/`apply_instruction_effects`, `_apply_quest_choice` 제거.
- S10 하드스톱 스캔: §15 결과.

---

## 20. 하드 스톱
- WP-B 구현·증거 완료. **WP-C는 착수하지 않았다.**
- 다음 절차(별도 실행): `commit → push → local == remote → clean status`.
- 이후 **독립 GPT 게이트 PASS 전까지 WP-C(barrier/READY_TO_COMMIT) 및 상위 WP는 미승인 상태로 정지.**
