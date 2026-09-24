# WP-B — Transactional State Preparation · Completion Bundle

> 독립 GPT 게이트 검토용 증거 묶음. 비밀(토큰)은 포함하지 않는다.
> 이 패키지는 **AI-파생 자동턴 gameplay 변이**에 스테이징/계획 경계를 세우는 것에 한정된다.
> 권위적 commit·barrier·정산 배선(WP-C/D 이상)은 착수하지 않았다.
>
> **읽는 법:** §0이 **현재 상태의 정본**이다. §1 이후는 구현·게이트 라운드별 **HISTORICAL** 기록이며,
> 현재 상태와 다른 주장에는 `HISTORICAL / SUPERSEDED` 표시와 대체 근거를 달았다. 두 곳이 다르면 §0이 우선한다.

---

## 0. 현재 상태 (CANONICAL — 이 절이 정본)

### 0.1 식별 / Git
| 항목 | 값 |
|---|---|
| Package | WP-B (Transactional State Preparation) |
| Branch | `claude/wp-b-transactional-state-preparation` |
| Start SHA (WP-A tip) | `a488f421a85028589556033be93d749bb0bfc081` |
| **Final code snapshot** | **`4fb8577939878ccd1ca8d426058015c8768c2971`** |
| 코드 이후 커밋 | 문서 전용(completion/evidence 정합화). 최종 branch tip SHA는 closure 보고에서 별도 제시(이 문서는 자기 자신의 커밋 SHA를 담을 수 없음). `4fb8577..tip` 변경은 `handoff/` 문서뿐. |
| 게이트 | 독립 GPT 게이트 — 코드 구현 **PASS**(4fb8577 기준). 공식 VERIFIED checkpoint는 이 문서 정합화 후 디렉터 확정. |

### 0.2 회귀 (final code snapshot 4fb8577 기준)
- Baseline(start SHA): `291 passed, 9 xfailed`
- **Final: `339 passed, 6 xfailed, 0 XPASS`** (failed 0 / error 0). 컴파일·임포트 OK.
- xfail 9→6: 의도한 결함 수정으로 3건 통과 전환(`test_d003c`, `test_d003d` = AUD-024 / `test_d001b` = AUD-011).
- 남은 6 xfail은 상위 WP 소관: `test_d002c/d/e`(AUD-012/019, WP-C/D) · `test_d004c`(AUD-020)·`test_d005d`(AUD-029) WP-E · `test_d006e`(AUD-034/035, WP-F).

### 0.3 Findings (최종)
| AUD | 상태 | 근거 |
|---|---|---|
| AUD-001 자:/태: 이중 상태권위 | **RESOLVED_IN_CODE** | `_execute_proceed`의 resources/statuses 직접 변이 루프 제거(방어적 strip 보존). T-B06/07 |
| AUD-005 중복 quest-choice owner | **RESOLVED_IN_CODE** | `_apply_quest_choice` 제거, `stage_instruction_effects`→`apply_instruction_effects` 단일 owner |
| AUD-011 추출 입력 절단 | **RESOLVED_IN_CODE** | 완결 전체 묘사가 추출 provider 프롬프트에 도달(`[:500]`/`[:3000]` 제거, 요약 폴백 제거). `test_d001/b/c/d`(provider `contents` 캡처) |
| AUD-024 묘사 실패 시 지시효과 누출 | **RESOLVED_IN_CODE** | 지시효과 스테이징 + 묘사 성립 후 단일 적용. `test_d003c/d/c2` |
| **AUD-065** 자동 서사 재계획이 provider 결과를 canonical `session.narrative_plan`에 직접 적용 | **RESOLVED_IN_CODE** | result-only producer · 정규화 `NarrativePlanMutationPlan` · 스케줄 시점 원인 tx 정체성 · stale 거부 · operation-level 멱등 · 정규화 호환 적용 · provider CostEvent 보존 · 자동/setup/manual scope 분리. N-B01~09(§23) |
| AUD-014 merged-status 검증 | **preserved** | `get_merged_status_effects` 기반 유효 상태 검증 의미 유지. 위치만 이동: 시작 시 `apply_extraction` 본문(extraction.py 285–312) → 최종 `_valid_status_set`(extraction.py 263–) 을 `normalize_status_item_effects`(289)가 사용. P-B08 |
| AUD-019 추출 stale | 부분 완화(WP-B 범위) | stale 거부 추가. 커밋 배리어·실패청구 완전 해결은 WP-C/D |
| AUD-012 / AUD-019 커밋 배리어·실패청구 | 열림 — WP-C/D | xfail 유지 |
| AUD-020 / AUD-029 되감기·제공자 이력 | 열림 — WP-E | xfail 유지 |
| AUD-034 / AUD-035 캐시 단일 정산점 | 열림 — WP-F | xfail 유지 |

### 0.4 최종 소유권 맵 (자동 턴 AI-파생 canonical 변이, final code snapshot)
원칙: provider/parser는 canonical을 바꾸지 않는다 → 코드 검증·정규화 → 계획/후보 → (stale·멱등) → 명시적 단일 호환 소비부 → canonical mutator(정규화 입력만).

| 도메인 | producer (result-only) | validator / normalizer | plan / 후보 | stale · 멱등 | 호환 소비부 | canonical mutator |
|---|---|---|---|---|---|---|
| 지시 quest-choice · intended_case · info_ledger | `_call_gm_logic`(gm.py 2063–2247, decision 반환) | `stage_instruction_effects`(tp 203–227: narrative_mode·offered·random, `_merge_info_ledger` 순수) | `PendingInstructionEffects`(트랜잭션-로컬) + quest 투영 | 트랜잭션 귀속, 묘사 실패 시 미적용 | `apply_instruction_effects`(tp 254–288; 호출 gm.py 1442, 묘사 성립 후) | `quest.apply_choice`/`set_intended_case`/병합 결과 |
| 자:/태: 태그 | — | — | — | — | **없음(권위 제거)** | 없음 |
| narrative progress (`current_event.progress`) | 묘사 결과 | `apply_narrative_progress`(tp 291–301: current_event 가드·150자) | — | 묘사 성립 이후 지점 | 동일(호출 gm.py 2780) | `narrative_plan.current_event.progress` |
| 추출: 상태이상 · 소지품 · 만난 NPC · 동행 · 위치/세계 타임라인 · 퀘스트 진전 · 이면정보 · BGM (+코드 파생 메인 해금) | `_run_extraction`(gm.py 3368–3554) provider+`parse_extraction` | `build_extraction_plan`(tp 403–486) → `normalize_status_item_effects`·`normalize_companions`·`normalize_location`(검증·임계·dedup·모순 제외) | `ExtractionMutationPlan`(typed 필드, tp 311–347) | `extraction_is_stale`(→`superseded_by_newer_attempt`) · `_extraction_applied_tx` | `_apply_extraction_plan`(gm.py 3220–3366) | `apply_normalized_status_item`·`apply_normalized_companions`·places/`quantify`·`advance_quest({quest_progress})`·`check_secret_awareness({secret_awareness})`·`check_main_unlock`·`select_bgm(plan.situation)` |
| irregular NPC 등록(미디어 배정) | `_resolve_irregular_npcs`(gm.py 3049–3180) | `build_irregular_npc_plan`(tp 518–556: 코드 파생 후보 allowlist·유효 이미지 풀·dedup) | `IrregularNpcMutationPlan`(tp 499–515) | 호출 전 tx 고정 + stale · `register` 멱등 | `_apply_irregular_npc_plan`(gm.py 3182–3218) | `irregular_npc.register`(`reg[*]`만), `note_appearance` |
| irregular NPC 승격 | `_generate_npc_detail`(gm.py 2923–3017) | `normalize_npc_detail`(tp 559–581) | 정규화 후보 `{final_name, details}` | 호출 전 tx 고정 + stale · `promote` 멱등 | `_apply_npc_promotion`(gm.py 3019–3047) | `mark_detailed`/`promote`(`norm["details"]`만) + rename(`final_name`) |
| **자동 서사 재계획(AUD-065)** | `_generate_narrative_plan_candidate`(gm.py 4182–4397) | `build_narrative_plan`/`normalize_narrative_plan`(tp 625–680: 스키마 구조 검증·리더 필드만·provider metadata 폐기) | `NarrativePlanMutationPlan`(tp 605–622) | 스케줄 시점 원인 tx 복사(`_update_narrative_progress` gm.py 3941–4031) + `superseded_by_newer_attempt` · `narrative_replan_key`(tp 683–695) | `_auto_replan_narrative`(gm.py 4059–4098) → `_apply_narrative_plan`(gm.py 4100–4180) | `session.narrative_plan` = 정규화 사본 + 코드 소유 `plan_version`/`last_planned_turn` → `save_session_data` |

**automatic narrative replanning = WP-B staged/normalized path** (위 표 마지막 행). setup(`_init_narrative_and_start`)·manual(`!자동 재계획`)은 `_plan_narrative`(gm.py 4033–4057)로 기존 제품 의미를 유지하며 자동 TurnTransaction 의미를 요구하지 않는다(같은 producer·normalizer·소비부 공유).

### 0.5 호환 적용 지점 (열거·최소, final)
자동턴 AI-파생 canonical 적용은 **다음 여섯 소비부에서만** 일어난다. 모두 authoritative commit이 아니며 WP-C/D가 barrier·commit 뒤로 이동/치환한다.
1. `apply_instruction_effects` — 지시효과
2. `apply_narrative_progress` — `current_event.progress`
3. `_apply_extraction_plan` — 추출 효과
4. `_apply_irregular_npc_plan` — irregular NPC 등록
5. `_apply_npc_promotion` — irregular NPC 승격
6. `_apply_narrative_plan` — 서사 계획 교체(자동 경로는 `_auto_replan_narrative`의 stale·멱등 통과 후)

공통 stale 판정: `superseded_by_newer_attempt`(기존 `extraction_is_stale` 본체를 일반 이름으로 옮김; `extraction_is_stale`는 동일 동작 위임).

### 0.6 범위 밖으로 확정된 AI-결과 쓰기 (최종 AST 인벤토리 결론, §23)
계획/정규화 경계를 거치지 않는 자동 턴 AI-derived canonical write는 **없음**. 남은 AI 결과 기반 쓰기는 모두 패킷상 WP-B 범위 밖:
- 서사 이력 `raw_logs`/`current_turn_logs`(패킷 §29, WP-B 레거시 유지 — WP-A 출력 소유)
- 압축/캐시 `compressed_memory` 등(패킷 §28, WP-F)
- setup/admin: `!설정생성`(`generate_character_details`), 캐릭터 생성 UI(`profile_ai`), 세션 유지시간 해석(`interpret_cache_time`, 캐시·billing legacy)
- restore/load: 디스크 세션 복구·되감기
- 운영자 명령: `!퀘스트 열기`(`start_quest`, P-B10)

### 0.7 커밋 이력 (start → final code snapshot)
| SHA | 내용 |
|---|---|
| `b583f310a4f8488aaf6575dfd4f127ba77b4cf72` | b3: 스테이징 모델(PendingInstructionEffects·quest projection·단일 applier) |
| `3c43a4230ae63b76ebd2627636cb85bbc562a910` | b4/b5: 지시 스테이징 배선 + quest projection; AUD-024 xfail 전환 |
| `967263e280a69870db718d2e665562abd26f4cc9` | b6: 자:/태: 직접 변이 권위 제거(AUD-001) |
| `2c5d62c9c386ce99f37a6919dc5034d000107f6e` | b7–b12: 추출 result-only + 검증 계획 + stale + 멱등 경계 |
| `feec269d79fdca88d000b1400510f402fb32b636` | 완료 번들 초판 |
| `1699687d88149a16f03e27a10dd09689c25f0540` | 게이트 패치 1: 전체 묘사 추출 + plan-only 적용 권위 |
| `39f6b09166170c7a6a8ce0e6778b92cc24f3f22d` | 게이트 패치 2: irregular NPC result-only + 정규화 계획 |
| **`4fb8577939878ccd1ca8d426058015c8768c2971`** | 게이트 패치 3: 자동 서사 재계획 스테이징(AUD-065) — **final code snapshot** |

### 0.8 누적 변경 파일 (start → 4fb8577)
- production: `cogs/game.py`, `cogs/gm.py`, `core/__init__.py`, `core/extraction.py`, `core/prompt.py`, `core/quest.py`, `core/turn_preparation.py`(신규)
- tests: `tests/defects/test_extraction_boundary.py`, `tests/defects/test_instruction_side_effects.py`, `tests/policy/test_narration_boundary.py`, 신규 `test_turn_preparation.py`·`test_extraction_staging.py`·`test_plan_authority.py`·`test_irregular_npc_plan.py`·`test_narrative_replan_plan.py`
- docs: `handoff/WP_B_COMPLETION_BUNDLE.md`
- 무변경 확인: `core/turn_transaction.py`, settlement/ink/accounts/commit_journal, rewind/cache/io, prompts/scenarios/data.

### 0.9 하드 스톱
- WP-B 코드 구현 완료, 게이트 코드 PASS. **WP-C(barrier/READY_TO_COMMIT) 및 상위 WP는 착수하지 않았다.**
- 공식 VERIFIED checkpoint 확정 전까지 정지.

---

# HISTORICAL RECORD (§1–§23)

> 아래는 1차 구현(§1–§20)과 게이트 라운드별 패치(§21–§23)의 **당시 기록**이다. 시행착오를 보존하기 위해 삭제하지 않았다.
> 현재 상태와 다른 주장에는 `HISTORICAL / SUPERSEDED` 표시를 달았다. **현재 상태는 §0을 따른다.**
> 게이트 라운드 대응: §21 = 게이트 1회차 blocker 2건(코드 `1699687`) · §22 = irregular NPC omission(`39f6b09`) · §23 = AUD-065(`4fb8577`).

## 1. 패키지 식별 / Git identity
- Package: **WP-B (Transactional State Preparation)**
- Branch: `claude/wp-b-transactional-state-preparation`
- Start SHA (WP-A tip): `a488f421a85028589556033be93d749bb0bfc081`
- ~~Final SHA: `2c5d62c9c386ce99f37a6919dc5034d000107f6e`~~ — **HISTORICAL / SUPERSEDED**: 1차 구현 시점 tip. 최종 코드 스냅샷은 §0.1 `4fb8577939878ccd1ca8d426058015c8768c2971`.
- ~~Push / local == remote / `git status --short`: §20에서 push 후 기재~~ — **HISTORICAL / SUPERSEDED**: push·local==remote·clean은 매 라운드 완료됨. 최종 closure 증거는 §0.1 및 closure 보고.

### 중간 커밋
> **HISTORICAL / SUPERSEDED**: 1차 구현 커밋만 기재. 전체 이력(게이트 패치 포함)은 §0.7.

| SHA | 내용 |
|---|---|
| `b583f31` | b3: core/turn_preparation.py 스테이징 모델(PendingInstructionEffects, info_ledger 순수병합, 통합 quest-choice, 단일 idempotent applier) + policy 테스트 |
| `3c43a42` | b4/b5: 지시 스테이징 배선 + quest projection; AUD-024 xfail 2건 전환 |
| `967263e` | b6: 레거시 자:/태: 직접 resource/status 변이 권위 제거(AUD-001) + T-B06/07 |
| `2c5d62c` | b7-b12: 추출 result-only + 검증 계획 + stale guard + idempotent 호환 경계; D-001 전환 + T-B17/B19/B21 |

---

## 2. 기준/최종 회귀 (Baseline / final)
- **Baseline (start SHA, WP-A tip):** `291 passed, 9 xfailed`
- ~~Final full suite: `311 passed, 6 xfailed`~~ — **HISTORICAL / SUPERSEDED**: 1차 구현 시점 수치. 최종(4fb8577)은 §0.2 **`339 passed, 6 xfailed, 0 XPASS`**.
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
> **HISTORICAL / SUPERSEDED**: 1차 구현 시점 목록. 이후 `core/extraction.py`, `test_plan_authority.py`, `test_irregular_npc_plan.py`, `test_narrative_replan_plan.py` 등이 추가됨 — 누적 목록은 §0.8.

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
> **HISTORICAL / SUPERSEDED**: 이 인벤토리에는 두 가지 오류가 있었고 게이트에서 교정됐다. (1) irregular NPC 등록·승격이 **누락**됨 → §22에서 스테이징. (2) 자동 `_plan_narrative`를 'WP-B 경계 밖'으로 **잘못 분류** → §23(AUD-065)에서 스테이징. 또한 추출 행의 '경계 내부로 감쌈·mutator 재사용'은 §21의 plan-only 적용 권위로 대체됨. 최종 맵은 §0.4.

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
| `_plan_narrative` 재수립(3994/4006/4220) | narrative_plan 전체 | planning AI operation | 별도 오퍼레이션 | 서사설계 | 예(별도) | ~~WP-B 경계 밖(§41 유지)~~ **HISTORICAL / SUPERSEDED** → 자동 경로 = WP-B 스테이징(AUD-065, §23) · setup/manual = 범위 밖 유지 | `_auto_replan_narrative`→`_apply_narrative_plan` |

---

## 5. 편집 후 소유권 맵 (도메인별)
> **HISTORICAL / SUPERSEDED**: 1차 구현 시점 맵(plan-only 권위·irregular NPC·AUD-065 이전). 추출 행의 'mutator 재사용(raw 입력)'은 §21에서 정규화 DTO 소비로 대체됨. **최종 맵은 §0.4.**

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
> **HISTORICAL / SUPERSEDED**: `ExtractionMutationPlan`은 §21에서 typed 정규화 필드로 확장됨. 이후 `IrregularNpcMutationPlan`(§22), `NarrativePlanMutationPlan`·`superseded_by_newer_attempt`(§23) 추가. 최종은 §0.4.

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
> **HISTORICAL / SUPERSEDED**: 아래 '3개'와 줄번호(3312–3498)는 1차 구현 시점. 최종 호환 적용 지점은 **§0.5의 6개**.

자동턴 AI-파생 canonical 적용 지점은 **(당시) 정확히 다음뿐**:
1. **지시효과**: `apply_instruction_effects` — gm.py:1441 (`_finish_proceed_and_continue`, 묘사 성립 직후; 실패 경로는 그 이전 return).
2. **narrative progress**: `apply_narrative_progress` — `_dispatch_proceed`(묘사 성립 후 ai_summary 존재 시).
3. **추출효과**: gm.py **3312–3498** `▼▼▼ WP-B 단일 호환 적용 경계 ▼▼▼ … ▲▲▲ 끝 ▲▲▲` — 내부에서만 추출 mutator 호출(경계 밖 호출 0건, §14 스캔).
경계는 주석으로 명시되어 WP-D가 이동/치환 가능. **authoritative commit 아님**.

---

## 9. 전체-묘사 추출 증거 (AUD-011)
- ~~`_dispatch_proceed`: `_full_narration = (result or {}).get("ai_text") or ai_summary`~~ — **HISTORICAL / SUPERSEDED**: 요약 폴백 제거, 최종은 `... or _full_model_text or ai_summary`(아래 게이트 패치 항목).
- 구조 특성화 `test_d001c`: `_dispatch_proceed` 본문에 `ai_text` 존재 + `_run_extraction(session, ai_summary` 부재.
- 행위 `test_d001`/`test_d001b`: 500자 경계 뒤 사실(LATE_FACT)이 추출 입력에 도달(전체 == narration).
- **[게이트 패치] 내부 `ai_output_text[:3000]` 절단 제거** — 추출 provider input에 완결 전체 묘사 전문 전달. 별도 op `_resolve_irregular_npcs`의 `[:1500]`도 전문으로 상향(묘사 증거 uniform). `_full_narration` 폴백을 500자 요약이 아닌 원문(`_full_model_text`)으로 강화(요약-only 경로 제거).
- **실증 `test_d001d_full_narration_reaches_provider_prompt`**: 묘사>4000자, 마커를 char 3500 뒤에 배치 → provider 호출 직전 실제 `contents`(프롬프트)를 캡처해 마커·전문 포함 검증. `_run_extraction` 내부 truncation 0건(§39 스캔). ⇒ AUD-011 **RESOLVED_IN_CODE**.

## 10. 변이-계획/검증 증거
- `build_extraction_plan`: 등록 캐릭터·merged-status 목록으로 1차 검증(무효 status/캐릭터 drop → diagnostics), 동치 중복 제거(seen set, T-B17), 상호 모순(동행 join&leave) conflict 진단.
- `test_b17_equivalent_effects_deduped`(npc_met/companion 중복 1건화), `test_b17b_join_and_leave_conflict_flagged`.
- ~~임계 비교·소지품 정산은 기존 mutator가 경계 내부에서 수행~~ — **HISTORICAL / SUPERSEDED**: §21에서 순수 정규화기(`normalize_status_item_effects` 등)가 계획 빌드 단계에서 수행, 적용부는 정규화 DTO만 소비.

## 11. 파생 효과 증거
- plan entries 도메인 실제 존재: `location(move)`, `status(score)`, `item(delta)`, `npc_met(meet)`, `companion(join/leave)`, `quest(progress)`, `secret(awareness)`.
  - **HISTORICAL / SUPERSEDED**: §21 이후 entries는 typed 필드를 반영(`status apply/clear`, `item delta`, `npc_met`, `companion join/leave`, `location move`, `quest progress`, `secret awareness`). irregular NPC·서사 재계획 파생 효과는 §22·§23.

## 12. 호환 applier + stale/idempotent 증거
- `extraction_is_stale`: active=None(커밋 후 새 턴 없음)→False(정상 적용), active의 (logical_turn,attempt)가 추출보다 큼→True(거부). `test_b19_extraction_is_stale_decision`.
- 행위 `test_b19b_stale_extraction_applies_no_mutation`: 더 새로운 논리 턴 활성 시 world_timeline·resources **무변화**, 반환 None.
- idempotency `test_b21_compatibility_apply_at_most_once`: 동일 transaction_id 재적용 시 additive 자원이 **이중 반영 안 됨**(`_extraction_applied_tx` 가드).

---

## 13. 지시 스테이징 증거 (§H)
- 직접 변이 제거: `test_d003`(AST) — `_call_gm_logic` 본문에 `apply_choice`/`_update_info_ledger` 부재 + src에 `stage_instruction_effects` 존재.
- 묘사 실패 행위: `test_d003c`/`test_d003d` — 스테이징 후 `_dispatch_proceed`가 None(실패) 반환 시 canonical(quest active/info_ledger) **무변화**; `test_d003c2` — 성립 시 적용.

## 14. 호출/변이 스캔 (§39 8-증명, 전부 PASS)
> **HISTORICAL / SUPERSEDED**(부분): 3·5번의 줄번호와 '3개'는 1차 구현 시점. 4번 목록에는 이후 irregular NPC(§22)·자동 서사 재계획(§23)이 추가됨. 최종 스캔은 §23, 최종 적용 지점은 §0.5.

1. instruction producer가 quest/info canonical mutator 직접호출 **안 함** — `_call_gm_logic`에 apply_choice/start_quest/_update_info_ledger/_apply_quest_choice/merge 부재.
2. 자:/태: 파서가 resources/statuses 직접변이 **안 함** — `_execute_proceed`에 session.resources[char]/session.statuses[char]/res_tags/status_tags 부재.
3. 추출 producer/parse 구역(3168–3311)에 gameplay 필드 쓰기 **없음**(통제 플래그·비용만).
4. 자동턴 AI 효과가 staged/plan 경유 — 지시=stage/apply, 추출=plan+guarded, progress=단일 owner, 자:태:=중립화.
5. 호환 적용 사이트 **열거·최소**(위 §8: 3개).
6. 두 번째 quest-choice 자동 owner **없음** — `_apply_quest_choice` def 0건. (gm.py:1853 `start_quest`는 `!퀘스트 열기` **운영자 명령**, 별개 제품 의미 — P-B10.)
7. raw 모델 추출 result가 canonical mutator에 **전혀 전달 안 됨**(게이트 패치) — `_run_extraction` 내 raw `result` 소비처는 (1)`parse_extraction` 산출 (2)`build_extraction_plan` 입력 (3)stale 로그 (4)`return`뿐. 적용부 `_apply_extraction_plan`의 모든 mutator는 계획 파생 정규화 DTO(또는 좁은 `{quest_progress}`/`{secret_awareness}`)만 소비. 구 mutator(apply_extraction/apply_companions/to_world_timeline)는 호출부 0(하위호환 래퍼로만 존치). 상세 §21.
8. prompt/scenario/data 파일 **무수정** — diff에 .json/scenario/data/prompt-text 없음.

## 15. 금지 범위 스캔 (§P / S10) — WP-C/D/E/F/G cutover 없음
- READY_TO_COMMIT/join_barrier: 변경분 유일 매치는 turn_preparation.py **범위 제외 설명 주석**(“이 모듈이 하지 않는 것”)뿐 — 기능 도입 아님.
- CommitJournal/Settlement/InkTransaction **live caller 추가 0건** (`^\+.*commit_journal\.` 매치 없음). `__init__.py` 변경은 `+from . import turn_preparation` 한 줄; commit_journal/settlement import는 기존 WP-A 것.
- `core/turn_transaction.py` 이 브랜치에서 **무변경**(TurnStatus에 새 상태 추가 없음).
- rewind/cache 마이그레이션 없음(P-B17/P-B18): 해당 xfail 6건 그대로 유지.

---

## 16. Findings 상태
> **HISTORICAL / SUPERSEDED**: 라운드 진행 중 표. **최종 findings는 §0.3**(AUD-001/005/011/024/065 = RESOLVED_IN_CODE, AUD-014 = preserved).

| AUD | 상태 | 근거 |
|---|---|---|
| AUD-001 (자:/태: 이중 상태권위) | **닫힘** | 직접변이 제거, 권위 추출+코드검증 단일화. T-B06/07 |
| AUD-005 (중복 quest-choice owner) | **닫힘** | `_apply_quest_choice` 제거, 단일 스테이징 owner |
| AUD-011 (추출 입력 절단) | **RESOLVED_IN_CODE** | 전체 묘사가 provider 프롬프트에 도달(절단 제거). test_d001/b/c/**d**(실제 contents 캡처) |
| AUD-024 (묘사 실패 시 지시효과 누출) | **닫힘** | 스테이징+성립후 단일경계 적용. test_d003c/d |
| AUD-014 (merged-status 검증) | **보존(유지)** | extraction.py 285–312 검증 그대로. P-B08 — **HISTORICAL / SUPERSEDED**(위치): 최종은 `_valid_status_set`→`normalize_status_item_effects`로 이동, 의미 동일(§0.3) |
| AUD-012 / AUD-019 (커밋 배리어·실패청구) | **열림 — WP-C/D** | test_d002c/d/e xfail 유지 |
| AUD-020 / AUD-029 (되감기·제공자 이력) | **열림 — WP-E** | test_d004c/d005d xfail 유지 |
| AUD-034 / AUD-035 (캐시 단일 정산점) | **열림 — WP-F** | test_d006e xfail 유지 |
| AUD-019(추출 stale 부분완화) | **부분** | stale 거부 추가(§26 명시대로 완전해결 아님) |
| AUD-065 (자동 서사 재계획이 provider 결과를 canonical `session.narrative_plan`에 직접 적용) | ~~RESOLVED_IN_CODE 후보~~ → **RESOLVED_IN_CODE** (게이트 PASS, §0.3) | result-only 후보 + 정규화 + 스케줄 시점 tx 정체성 + stale/멱등 + 단일 호환 소비부. N-B01~09(§23) |

## 17. 새 findings / blocker
- ~~blocker 없음.~~ — **HISTORICAL / SUPERSEDED**: 이후 독립 게이트가 세 라운드에 걸쳐 blocker를 지정했다(추출 절단·plan-only 권위 / irregular NPC 누락 / AUD-065). 모두 해소, 코드 PASS(§0).
- **게이트 패치(WIRED_NOT_VERIFIED → 해소)**: 아래 §21 참조. Blocker 1(추출 절단 제거)·Blocker 2(계획을 적용의 유일 권위로) 모두 코드 반영·테스트 완료. 이전 판본의 `[:3000] 보존`·`정규화 위임은 WP-D` 관찰은 **철회**됨 — 정규화 위임을 WP-B 내에서 완결했다.

---

## 18. 예약 목록 증거 (P-B01~P-B22)
- **P-B01/02** TurnTransaction 정체성·ROLL stale guard: turn_transaction.py 무변경, 연속성 테스트 green.
- **P-B03/04/05** 묘사 생성·전달·PC 자율성 경계: game.py 생성/전달 분리·검증 보존, test_narration_boundary green.
- **P-B06/07** provider CostEvents·retry: 비용/재시도 로직 무변경, 추출 재시도는 같은 attempt(§27). T-B20 의미 보존.
- **P-B08** merged status 검증: extraction.py 285–312 보존. — **HISTORICAL / SUPERSEDED**(위치): 최종 `_valid_status_set`(263–) 경유, 의미 동일.
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
> **HISTORICAL / SUPERSEDED**(줄번호): 아래 줄번호(3312·3324 등)는 당시 기준. 최종 실제 코드 발췌는 별도 제출물 `WP_B_S1_S10_SOURCE_EXCERPTS.md`(4fb8577 기준, `git show`로 추출)를 따른다.

- S1 지시 producer 순수: `cogs/gm.py` `_call_gm_logic` → `return decision`(직접 canonical 없음).
- S2 스테이징 projection: `core/quest.py` `projection_view`/`_ProjectionView` + `build_quest_block(..., quest_state=)`.
- S3 태그 권위 제거: `cogs/game.py` `_execute_proceed` 중립화 주석 + 루프 부재.
- S4 전체 추출 입력: `cogs/gm.py` `_dispatch_proceed` `_full_narration`.
- S5 result-only 경계: `cogs/gm.py` `_run_extraction` 3312 경계 위 파싱 반환.
- S6 검증/계획: `core/turn_preparation.py` `build_extraction_plan`.
- S7 파생 효과: 동함수 `_add(domain,...)` entries. **[3차 패치] 비정규 NPC 경로**: producer `_resolve_irregular_npcs`/`_generate_npc_detail`(result-only) → normalizer `build_irregular_npc_plan`/`normalize_npc_detail` → 계획 `IrregularNpcMutationPlan`/정규화 후보 → 단일 소비부 `_apply_irregular_npc_plan`/`_apply_npc_promotion` → mutator `irregular_npc.register`/`promote`(정규화 입력만).
  **[4차 패치] 자동 서사 재계획(AUD-065)**: scheduling `_update_narrative_progress`(원인 tx 정체성 복사) → producer `_generate_narrative_plan_candidate`(result-only) → normalizer `build_narrative_plan`/`normalize_narrative_plan` → 후보 `NarrativePlanMutationPlan` → stale `superseded_by_newer_attempt` + 멱등 `narrative_replan_key` → 단일 소비부 `_apply_narrative_plan` → `session.narrative_plan`(정규화 후보 + 코드 소유 metadata).
- S8 stale guard: `extraction_is_stale` + 3324~ 경계 가드.
- S9 quest 단일 owner: `stage_instruction_effects`/`apply_instruction_effects`, `_apply_quest_choice` 제거.
- S10 하드스톱 스캔: §15 결과.

---

## 20. 하드 스톱
- WP-B 구현·증거 완료. **WP-C는 착수하지 않았다.**
- ~~다음 절차(별도 실행): commit → push → local == remote → clean status.~~ — **HISTORICAL / SUPERSEDED**: 완료됨(§0.1).
- 이후 **독립 GPT 게이트 PASS 전까지 WP-C(barrier/READY_TO_COMMIT) 및 상위 WP는 미승인 상태로 정지.** — **HISTORICAL / SUPERSEDED**: 게이트 코드 PASS 완료. WP-C는 여전히 미착수(§0.9).

---

## 21. 게이트 패치 (WIRED_NOT_VERIFIED → 해소)

독립 GPT 게이트가 **WIRED_NOT_VERIFIED / PATCH REQUIRED**로 두 blocker를 지정했다(동일 브랜치, WP-C 금지). 아래로 해소했다.

### Blocker 1 — 추출 입력 절단 제거 (완결 전체 묘사 전달)
- `_run_extraction` 추출 프롬프트 `ai_output_text[:3000]` → **전문**. 재시도 컨텍스트 `extraction_retry_ctx["text"]`도 전문(재추출도 전체 증거).
- `_dispatch_proceed`: `_full_model_text`(원문) 캡처 추가, `_full_narration = ai_text or _full_model_text or ai_summary` — 500자 요약-only 폴백 제거.
- 별도 AI op `_resolve_irregular_npcs`(NPC 이미지배정, game.py:764 호출)의 `[:1500]`도 전문으로 상향 — 묘사 증거 uniform, "동등한 silent truncation" 제거.
- provider(Gemini) 컨텍스트 한도 ≫ 묘사 길이(수천 자) → 청킹 불필요, 전문 전달(코드 주석 명시).
- **테스트**: `test_d001d_full_narration_reaches_provider_prompt` — 묘사>4000자·마커 char 3500 뒤 → provider 호출 직전 실제 `contents` 캡처 → 마커·전문 포함 검증. 기존 d001/d001b/d001c 유지. (`tests/defects/test_extraction_boundary.py` 4 passed)
- **스캔**: `_run_extraction`/`_resolve_irregular_npcs` truncation 0건.

### Blocker 2 — ExtractionMutationPlan을 적용의 유일한 권위로
요구 파이프라인: `raw result → parse → 코드 검증 → 정규화 accepted 효과 → ExtractionMutationPlan → LegacyCompatibilityApplier → 계획 항목만 적용`.

구현:
- **순수 정규화기/적용기 분리**(`core/extraction.py`): `normalize_status_item_effects`(검증+임계+dedup, 무변이) / `apply_normalized_status_item`; `normalize_companions`(dedup + join&leave 모순 양쪽 제외) / `apply_normalized_companions`; `normalize_location`(to_world_timeline+resolve+hops, 무변이). 구 `apply_extraction`/`apply_companions`는 **하위호환 래퍼**(normalize→apply)로 전환 — 기존 동작·테스트 보존.
- **계획 = 권위**(`core/turn_preparation.py`): `ExtractionMutationPlan`에 typed 필드(status_apply/clear, item_deltas, npcs_met, companions_joined/left, world_new_tl, location_before/after/moved, quest_progress, secret_awareness, situation, conflicts, dropped). `build_extraction_plan`이 정규화기로 typed 필드를 채우고 entries는 typed 필드를 반영(적용 권위와 일치).
- **LegacyCompatibilityApplier**(`cogs/gm.py` `_apply_extraction_plan`): 계획의 정규화 typed 필드(또는 좁은 `{quest_progress}`/`{secret_awareness}`)만 소비. `_run_extraction` 적용 블록(구 raw-result mutator 호출부)을 `await self._apply_extraction_plan(session, plan, master_ch)` 한 줄로 대체. raw result는 빌드 이후 어떤 mutator의 입력도 아님(스냅샷 `last_extraction`·로그로만 존치, mutator 입력 아님).

원칙 대응: (1)raw result plan 이후 mutator 입력 금지 ✓ (2)applier는 정규화 DTO만 ✓ (3)rejected 부활 불가 ✓ (4)dedup/conflict 버린 후보 재등장 불가 ✓ (5)code-derived(main unlock)도 경계 내 ✓ (6)old helper 재사용하되 입력 정규화 ✓ (7)parse+validate+mutate 결합을 분리(normalize↔apply) ✓ (8)WP-D로 미루지 않음 ✓.

#### 도메인 소유권 맵 (raw candidate → validator/normalizer → plan entry → compatibility consumer → final mutator)
> **HISTORICAL / SUPERSEDED**(범위): 추출 도메인 한정 맵. irregular NPC·자동 서사 재계획을 포함한 **최종 전체 맵은 §0.4.**

| 도메인 | raw 후보(schema) | validator/normalizer(순수) | plan 필드 | applier 소비 | 최종 변이 |
|---|---|---|---|---|---|
| 상태이상 | status_scores | normalize_status_item_effects(검증+임계+dedup) | status_apply/clear | apply_normalized_status_item | session.statuses |
| 소지품 | item_changes | 〃(dedup by (t,item,delta)) | item_deltas | 〃 | session.resources |
| 만난 NPC | npcs_met | normalize_status_item/companions(dedup) | npcs_met | apply_normalized_companions | session.met_npcs |
| 동행 | companions.joined/left | normalize_companions(dedup+모순 제외) | companions_joined/left | apply_normalized_companions | session.companions |
| 세계 타임라인/장소 | location, datetime | normalize_location(to_world_timeline+resolve+hops) | world_new_tl, location_* | mark_visited/release/quantify | world_timeline, visited, companions |
| 퀘스트 진전 | quest_progress | build_plan(좁은 정규화) | quest_progress | advance_quest({quest_progress}) | quest 상태·grants·pending_ending |
| 이면정보 | secret_awareness | build_plan | secret_awareness | check_secret_awareness({secret_awareness}) | secret_known |
| BGM | situation | build_plan | situation | select_bgm(plan.situation) | pending_bgm |
| 메인 해금 | (code-derived) | — | — | check_main_unlock(session) | main_unlocked_notified |

~~경계 밖(추출-result-plan 아님, 명시): irregular NPC 등록은 별도 AI op `_resolve_irregular_npcs` — `_plan_narrative`(§41)와 동류.~~ — **HISTORICAL / SUPERSEDED**: 'extraction schema에 없는 별도 AI op'라는 이유의 제외는 **잘못된 분류**였다(패킷 canonical 목록에 `irregular_npcs created by the turn` 명시). irregular NPC는 §22, 자동 `_plan_narrative`는 §23(AUD-065)에서 WP-B 스테이징됨. **`narrative_plan.progress`**는 단일 owner `apply_narrative_progress`(gm.py 라이브 + turn_preparation 스테이징 → 공개 함수 291) 경유, canonical(영속+미래 GM read) 분류.

#### 부정 테스트 (계획=권위 증명) — `tests/policy/test_plan_authority.py` 4 passed
- **A** rejected 부활 불가: 무효 캐릭터/상태/아이템 → 계획 status_apply/item_deltas 빈 값 + dropped 기록 → 적용 후 canonical 무변화.
- **B** 동치 중복 이중적용 불가: 물+5 두 번 → 계획 item_deltas 1건 → 적용 물=+5(≠+10).
- **C** 모순 비-last-write-wins: 동행 join&leave 동일 이름 → 계획 joined/left 양쪽 제외 + conflict → 적용 후 동행·**met_npcs 모두 흔적 없음**(합류 부수효과 없음).
- **D** 계획-only 적용: 계획 빌드 후 `plan.result={}`·raw 폐기 → `_apply_extraction_plan(plan)`만으로 물/상태/동행/만난NPC/장소이동 전부 반영(raw 없이 계획이 유일 권위).

### 재게이트 스캔
- **raw-result-to-mutator**: `_run_extraction` raw `result` 소비처 = parse 산출/plan 입력/stale 로그/return뿐. 적용부 모든 mutator는 계획 파생. (§14-7)
- **post-change 변이**: `_apply_extraction_plan` 모든 canonical write가 `plan.*` 파생 확인. `parse_extraction` 순수(canonical write 0).
- **금지 범위**: READY_TO_COMMIT 전이 0(상수 정의만), CommitJournal 인스턴스화 0, cogs 내 settlement/ink live 호출 0, settlement/ink/turn_transaction/accounts **무수정**.
- **회귀(당시 라운드)**: 316 passed / 6 xfailed / XPASS 0. — 최종은 §0.2.

### 변경/추가 파일(게이트 패치)
- `cogs/gm.py`(추출 절단 제거, `_full_model_text`, `_apply_extraction_plan` 신규, 적용 블록 대체)
- `core/extraction.py`(정규화기/적용기 신규, 구 mutator 위임 래퍼화)
- `core/turn_preparation.py`(ExtractionMutationPlan typed 필드, build_extraction_plan 재작성)
- `core/__init__.py`(정규화기/적용기 export)
- `tests/defects/test_extraction_boundary.py`(_long_narration>4000, test_d001d 추가)
- `tests/policy/test_plan_authority.py`(신규, A/B/C/D)

---

## 22. 게이트 패치 3차 (WIRED_NOT_VERIFIED — irregular NPC omission)

3차 게이트가 **_resolve_irregular_npcs 경로 누락**을 지정했다(동일 브랜치, WP-C 금지). 이전 두 blocker(전체 묘사, 추출 plan-authority)는 인정·불변 보존.

### Scope 판정 — A(gameplay canonical mutation) 확정
실제 source로 다음을 확인:
1. **자동 턴 orchestration 호출** — `cogs/game.py:764`에서 묘사 스트리밍 '전' `await gm_cog._resolve_irregular_npcs(session, narrative_text, master_ch)`.
2. **provider 결과가 등록/승격 대상·속성 결정** — `_resolve_irregular_npcs`의 model `data.npcs[].{image_key,gender,age}` → register 입력; `_generate_npc_detail`의 model `data.{details,role,attitude,birth_year}` → promote 입력.
3. **canonical 변경** — `register`/`note_appearance`/`mark_detailed` → `session.irregular_npcs`; `promote` → `session.npcs`(+등록부 제거). (core/irregular_npc.py:140/164/189/207/208)
4. **이후 read** — 등록부는 프롬프트(동일인 유지)·목소리/이미지 조회로, 승격된 `session.npcs`는 정규 델타 주입(프롬프트)으로 읽힘.
⇒ 판정 A. "extraction schema에 없음"만으로 제외 불가.

### 패치 — result-only + normalized plan/candidate 경계
- **미디어 배정**(`_resolve_irregular_npcs`): provider+parse는 result-only. `build_irregular_npc_plan(session, data, names, valid_pool, use_image, text, turn, tx…)`이 검증(이름은 코드 파생 후보 `names` 안, image_key는 유효 풀 안 else "", gender/age str만)·dedup해 `IrregularNpcMutationPlan.registrations` 생성. stale guard 후 `_apply_irregular_npc_plan`이 계획 항목만 `register`에 입력.
- **승격**(`_generate_npc_detail`): provider+parse는 result-only. `normalize_npc_detail(data, fallback_name)`이 정규화 후보(`{final_name, details}`) 생성. stale guard 후 `_apply_npc_promotion`이 정규화 `details`만 `mark_detailed`/`promote`에 입력.
- **단일 호환 소비부**: 등록=`_apply_irregular_npc_plan`, 승격=`_apply_npc_promotion`. 각 canonical effect가 명시적 단일 지점, 정규화 입력만.
- **register/promote helper 재사용**(전면 재작성 아님) — 입력만 정규화된 계획/후보.
- **stale**: 두 provider-boundary op 모두 호출 '전' `get_active_transaction`으로 (logical_turn, attempt) 고정, 적용 직전 `extraction_is_stale`로 더 새로운 논리 시도 활성 시 등록/승격 금지.
- **idempotency**: `register`(이미 있으면 기존 반환)·`promote`(등록부에서 pop, 재호출 시 False)·`mark_detailed`·`note_appearance`(같은 턴 dedup) 자연 멱등 — 동일 계획 2회 적용 시 canonical effect 1회.

### post-edit 소유권 맵 (irregular NPC)
| 단계 | 미디어 배정 | 승격 |
|---|---|---|
| producer(result-only) | `_resolve_irregular_npcs` provider+`json.loads` | `_generate_npc_detail` provider+`json.loads` |
| validator/normalizer | `build_irregular_npc_plan`(이름 allowlist·pool·dedup) | `normalize_npc_detail`(필드 정규화) |
| plan representation | `IrregularNpcMutationPlan.registrations` | 정규화 후보 `{final_name, details}` |
| compatibility consumer | `_apply_irregular_npc_plan`(단일) | `_apply_npc_promotion`(단일) |
| canonical mutator | `irregular_npc.register`(+note_appearance) | `irregular_npc.mark_detailed`/`promote`(+rename) |
| stale guard | `extraction_is_stale`(op 시작 시 tx 고정) | 동일 |
| idempotency | register 멱등 | promote/mark_detailed 멱등 |

### 테스트 — `tests/policy/test_irregular_npc_plan.py` 6 passed (당시)
> **HISTORICAL / SUPERSEDED**: 이 판본의 **I-B04는 잘못된 이유로 통과**했다(provider 대기 중 이중 begin 예외 → 호출 실패로 0 반환, stale 판정 미실행). 구현 무변경으로 테스트를 교정(2모드 + stale 판정 스파이)하고 승격 stale **I-B04c**를 추가 → 8 passed. 상세 §23 '테스트 교정 공개'.

I-B01 producer purity(plan/normalize build → irregular_npcs·npcs 무변경) / I-B02 valid applies once(register 1회) / I-B03 invalid rejected(후보 밖 이름 → registrations 빈값 → 무변경) / I-B04 stale rejected(provider 반환 직전 더 새로운 tx 활성 → 무변경) / I-B05 duplicate idempotent(동일 계획 2회 → 항목 1개·속성 불변) / I-B06 existing semantics(정상 배정 + 3회 등장 승격 → session.npcs 편입).

### 스캔
- **raw-payload-to-mutator(irregular NPC)**: raw `data`/`item` 소비처는 parse 산출 + `build_irregular_npc_plan`/`normalize_npc_detail` 입력뿐. `register`=`reg[*]`(plan), `promote`=`norm["details"]`, rename=`norm["final_name"]` — raw payload 0.
- **complete mutation scan**: `session.irregular_npcs`/`session.npcs` write는 irregular_npc helper(정규화 입력) + `_apply_npc_promotion` rename(정규화 final_name)뿐.
- **금지 범위**: READY_TO_COMMIT 전이 0, CommitJournal 0, settlement/ink/turn_transaction/accounts 무수정.
- **회귀(당시 라운드)**: 322 passed / 6 xfailed / XPASS 0. — 최종은 §0.2.
- **이전 blocker 불변**: 전체-묘사 추출·추출 plan-authority 코드/테스트 미변경(전체 회귀에 포함되어 통과).

### 변경/추가 파일(3차 패치)
- `cogs/gm.py`(`_resolve_irregular_npcs` result-only+plan+stale, `_apply_irregular_npc_plan` 신규; `_generate_npc_detail` result-only+normalize+stale, `_apply_npc_promotion` 신규; op 시작 tx 캡처 2곳)
- `core/turn_preparation.py`(`IrregularNpcMutationPlan`, `build_irregular_npc_plan`, `normalize_npc_detail`)
- `tests/policy/test_irregular_npc_plan.py`(신규, I-B01~I-B06)

---

## 23. 게이트 패치 4차 — AUD-065 자동 서사 재계획 (WIRED_NOT_VERIFIED — final omission)

게이트가 부록 B(자체 발견)의 판정 A를 승인했다. 이전 승인 패치(지시 스테이징·퀘스트 투영·중복 owner 제거·자:/태: 권위 제거·전체 묘사 추출·추출 plan-authority·추출 stale/멱등·irregular NPC 스테이징·WP-A 출력 소유·비동기 추출 비-join·billing/rewind/cache/prompt/scenario 비변경)는 **구현 무변경**.

**AUD-065** — automatic narrative replanning directly applied provider result to canonical `session.narrative_plan`. → ~~RESOLVED_IN_CODE 후보~~ **RESOLVED_IN_CODE** (게이트 코드 PASS; §0.3).

### 범위 — 세 scope 분리
| scope | 진입점 | 소비부 | TurnTransaction 의미 |
|---|---|---|---|
| A. 자동 턴 재계획 | `_finish_proceed_and_continue` → `_update_narrative_progress` → `create_task(_auto_replan_narrative)` (completed/deviated, 추출 수치 advance/deviation 4곳) | `_auto_replan_narrative` → `_apply_narrative_plan` | 원인 tx 정체성·stale·멱등 **적용** |
| B. 세션 초기화(setup) | `_init_narrative_and_start` → `_plan_narrative("init")` | `_plan_narrative` → `_apply_narrative_plan` | 요구하지 않음(기존 의미) |
| C. 운영자 수동 재계획 | `!자동 재계획`(`replan_narrative`) → `_plan_narrative("manual")` | 〃 | 요구하지 않음(기존 의미) |

### 구조
- **producer(result-only)** `_generate_narrative_plan_candidate`: 프롬프트 구성·provider 호출·재시도·비용 관측/정산(`begin_operation(OP_TURN_NARRATIVE_PLANNING)`/`accrue`/`_cl_op.record`/`turn_cost_log`)은 **기존 코드 그대로 이동**. `session.narrative_plan` 쓰기 0. 파싱 결과는 `build_narrative_plan`으로 후보화해 반환(실패·구조불량·퀘스트 모드 → None).
- **validator/normalizer** `normalize_narrative_plan`(core/turn_preparation.py, 순수): 최상위 dict + 필수 객체(`mid_plan`/`current_event`/`next_event`)가 dict가 아니면 거부. 객체별로 현행 스키마·리더 필드만 통과(`mid_plan{title,overview,milestones[str],end_condition}`, `current_event{title,summary,resolution_direction,progress}`, `next_event{title,summary,trigger}`, `planner_notes`). 형이 틀린 필드는 버림(리더 기본값 유지). **provider가 보낸 `plan_version`/`last_planned_turn` 등 코드 소유·미지 키는 폐기**. 모델 출력 의미는 재설계하지 않음.
- **plan representation** `NarrativePlanMutationPlan`(normalized, rejected, trigger_reason, full_replan, transaction_id/logical_turn/attempt).
- **compatibility consumer** `_apply_narrative_plan`: `copy.deepcopy(candidate.normalized)`에 코드 소유 metadata(`plan_version = 이전+1`, `last_planned_turn = turn_count`)를 부여해 `session.narrative_plan`에 적용 → 기존대로 `save_session_data` → embed 보고·로그. raw payload 입력 없음. authoritative commit(WP-D)으로 옮기지 않음.
- **stale guard**: `_update_narrative_progress` 진입 시 활성 tx의 `(transaction_id, logical_turn, attempt)`를 **스케줄 시점에 복사**해 `create_task` 인자로 전달(태스크 안에서 활성 tx 재조회 없음). 적용 직전 `superseded_by_newer_attempt`로 더 새로운 시도(같은 턴 재시도 또는 다음 턴) 활성 시 폐기 — session에 적용·저장되지 않음.
  - `superseded_by_newer_attempt`는 기존 `extraction_is_stale` 본체를 **일반 이름으로 옮긴 것**이며, `extraction_is_stale`는 동일 동작의 위임으로 남겼다(추출·irregular NPC 호출부·테스트 무변경). 추출 전용 이름을 서사 경로에 오용하지 않기 위함.
- **idempotency**: 안정 키 `narrative_replan_key` = `tx:<transaction_id>` (없으면 `lt:<logical_turn>:<attempt>`) + `|<trigger_reason>|<full|moment>`. `session._narrative_replan_applied`(scratch, 최근 16개, 추출 멱등과 같은 방식). 검사와 기록 사이에 await가 없어 **동시 중복 태스크도 한 번만 통과**. Python 객체 `applied` 플래그에 의존하지 않음.
- **persistence**: 적용 성공 시에만 기존 `save_session_data`. stale·중복·구조불량 후보는 적용·저장되지 않음. CommitJournal/strict commit 미도입.
- **narrative progress 분리**: `narrative_plan.current_event.progress` 갱신은 기존 단일 owner `apply_narrative_progress` 유지(무변경). 이번 패치는 전체 계획 교체 경로만.
- **비동기 유지**: 자동 재계획은 여전히 `create_task`(비-join). barrier/timing은 WP-C.

### post-edit 소유권 맵 (AUD-065)
| 단계 | 자동(A) | setup/manual(B/C) |
|---|---|---|
| scheduling / 원인 정체성 | `_update_narrative_progress`: 활성 tx 정체성 복사 → `create_task(_auto_replan_narrative(..., **_origin))` | 해당 없음(직접 await) |
| producer (result-only) | `_generate_narrative_plan_candidate` | 동일 |
| validator/normalizer | `build_narrative_plan` / `normalize_narrative_plan` | 동일 |
| plan representation | `NarrativePlanMutationPlan` | 동일 |
| stale guard | `superseded_by_newer_attempt(origin)` | 없음(기존 의미) |
| idempotency | `narrative_replan_key` + `_narrative_replan_applied` | 없음(기존 의미: 호출마다 버전 증가) |
| compatibility consumer | `_apply_narrative_plan` | 동일 |
| canonical mutator | `session.narrative_plan = normalized+code metadata` → `save_session_data` | 동일 |

### 테스트 — `tests/policy/test_narrative_replan_plan.py` 15 passed
N-B01 producer purity(후보 생성 후 narrative_plan 무변경, provider metadata 폐기) / N-B02 valid 자동 적용 + `plan_version=이전+1`·`last_planned_turn=turn_count`(provider 값 무시) / N-B03 구조 불량 3종 + 파싱 불가 → 무변경 / **N-B04** stale(같은 턴 재시도·다음 턴 2모드; stale 판정 스파이로 **실제 True 판정** + provider 첫 시도 성공 단언) / **N-B04b** 스케줄 시점 정체성(태스크 실행 전 다음 턴이 열려도 원인 tx 정체성 전달, 자동 경로가 `_plan_narrative`를 부르지 않음) / N-B05 동일 정체성 2회 → 1회 적용·버전 1회 증가 / N-B05b 동시 중복 태스크 → 1회 / N-B06 setup(`_init_narrative_and_start`, tx 없음, stale 판정 미호출) / N-B07 manual(`!자동 재계획`, 새 시도 활성 중에도 적용, stale 판정 미호출, 운영자 메모 프롬프트 반영) / N-B08 후속 GM 프롬프트(`_build_logic_user_prompt`)가 적용된 계획을 읽음 / N-B09 실제 `call_with_retry`+격리 CostLedger: 재시도 성공 시 `TURN_NARRATIVE_PLANNING` 이벤트 1건(provider_attempt=2), stale 폐기 재계획도 실제 호출이므로 관측 정확히 1건 추가.

### 테스트 교정 공개 (irregular NPC I-B04)
AUD-065 stale 테스트를 만들다 **3차 게이트에서 승인된 I-B04가 잘못된 이유로 통과**하고 있었음을 발견했다. provider 대기 중 `begin_turn_transaction`을 다시 호출해 '비종료 활성 트랜잭션' 예외가 났고, `_resolve_irregular_npcs`의 호출 실패 처리로 0이 반환되어 **stale 판정이 한 번도 실행되지 않았다**(스파이 계측: `stale_calls = []`).
- **구현은 무변경**. 테스트만 실제 흐름(`begin_attempt` 또는 `finalize`→다음 턴 `begin`)으로 교정하고 stale 판정이 실제로 True였음을 단언(2모드) → 통과. irregular NPC stale 가드는 이제 실증됨.
- 승격 경로 stale 전용 테스트가 없었으므로 **I-B04c** 추가(승격 provider 대기 중 새 시도 → promote 안 됨) → 통과.
- 전 스위트 계측(대조군으로 계측 유효성 확인): 조용히 삼켜진 이중 begin **0건** — 다른 stale 테스트(추출 b19b 등)는 올바른 이유로 통과.

### 사후 스캔 (AST 기반)
**`session.narrative_plan` 쓰기 분류**
| 위치 | 함수 | 분류 |
|---|---|---|
| cogs/gm.py `_apply_narrative_plan` | 정규화 후보 + 코드 metadata 적용 | **automatic validated compatibility apply** (+ setup/manual 공용 소비부) |
| cogs/gm.py `_update_narrative_progress` ×2 | 기존 계획 dict에 `last_planned_turn = turn_count` 스탬프 후 재대입 | 자동 경로의 **코드 파생 트리거 스탬프**(provider payload 아님, 무변경 보존; 리더는 표시용 운영자 명령 `show_narrative_plan`뿐) |
| core/turn_preparation.py `apply_narrative_progress` | `current_event.progress` 갱신 | 기존 단일 owner(승인, 무변경) |
| core/models.py `__init__` | `{}` 초기화 | setup(초기화) |
| core/cache.py 복구 루프(io.py 기본값 레지스트리) / core/rewind.py 되감기 | 저장본·스냅샷 복원 | restore/load |

**자동 provider-result direct assignment = 0.**

- **callers**: `_plan_narrative` ← `replan_narrative`(manual), `_init_narrative_and_start`(setup) / `_auto_replan_narrative` ← `_update_narrative_progress` ×4(자동).
- **readers**: `_build_logic_user_prompt`(GM 프롬프트), `auto_start`(버전 표시), `show_narrative_plan`(운영자 표시), `_update_narrative_progress`(재계획 트리거), `_init_narrative_and_start`(존재 확인), `_generate_narrative_plan_candidate`(completed 시 기존 mid_plan 참조), `_apply_narrative_plan`(버전), `apply_narrative_progress`, io.py/rewind.py 레지스트리.
- **narrative-planning provider calls**: `OP_TURN_NARRATIVE_PLANNING`은 `_generate_narrative_plan_candidate` 1곳뿐.

### 전체 AI-provider 변이 인벤토리 최종 재스캔 (AST: 직접 session 쓰기 + provider 결과 변수의 흐름)
| 함수 | 직접 session 쓰기 | provider 결과 흐름 | 분류 |
|---|---|---|---|
| game.py `generate_with_retry` | — | — | 묘사 헬퍼(WP-A 출력 소유) |
| game.py `_run_auto_compression` / `compress_memory` | compressed_memory 등 | 압축 세그먼트 | 압축/캐시 = **WP-F**(패킷 §28) |
| gm.py `_call_judgment` | turn_cost_log | 반환 → 결정 병합 → 스테이징 | 비용 로그만; 판정 결과는 S1 스테이징 경유 |
| gm.py `_call_gm_logic` | cache_read_tokens, turn_cost_log | 반환 → `stage_instruction_effects` | S1/S9 스테이징 |
| gm.py `_simulate_narrative_directions` | turn_cost_log | 반환 → 지시 프롬프트 입력 | canonical 쓰기 없음 |
| gm.py `_verify_proceed_instruction` | — | 반환 | canonical 쓰기 없음 |
| gm.py `_dispatch_narrate` | current_turn_logs, turn_cost_log | 묘사 텍스트 → current_turn_logs | 서사 이력 = **WP-B 레거시 유지**(패킷 §29) |
| gm.py `interpret_cache_time` | interpret_cost_krw | `resolve_minutes` → 반환(캐시 유지시간) | 운영/캐시(WP-F) + billing legacy |
| gm.py `_generate_npc_detail` | turn_cost_log | `normalize_npc_detail` | S7-c 스테이징 |
| gm.py `_resolve_irregular_npcs` | turn_cost_log | `build_irregular_npc_plan` | S7-b 스테이징 |
| gm.py `_run_extraction` | 통제 플래그·turn_cost_log | `build_extraction_plan` | S5~S8 plan-authority |
| gm.py `_generate_narrative_plan_candidate` | turn_cost_log | `build_narrative_plan` | **AUD-065 스테이징** |
| media.py `send_media` / tts.py `synthesize_tts_pcm` | — | — | 출력 전용 |
| profile_ai.py `_call` | — | — | 캐릭터 생성 UI(`profile_runner`) = profile setup(패킷 §8) |
| utils.py `generate_character_details` | — | — | `!설정생성` 운영자 명령 = admin/setup(패킷 §8) |

(이전 정규식 인벤토리는 `session.raw_logs[-6:]` 같은 **읽기**를 쓰기로 잘못 표시했다 — `_plan_narrative`·`_simulate_narrative_directions`의 raw_logs는 읽기다. 이번 표는 AST로 대입·증강대입·변이 메서드만 집계했다.)

**결론: 자동 턴의 AI-derived canonical write 중 계획/정규화 경계를 거치지 않는 경로는 더 이상 발견되지 않았다.** 남은 AI 결과 기반 쓰기는 패킷이 WP-B 범위 밖으로 명시한 서사 이력(§29)과 압축/캐시(§28), 그리고 setup/admin 경로뿐이다.

### 금지 범위 / 회귀
- READY_TO_COMMIT 전이 0(범위제외 주석만), CommitJournal 0, cogs settlement/ink live 0, 추출·재계획 join 0(`resilience.wait_for`는 기존 호출별 타임아웃), 기준 대비 turn_transaction/settlement/ink/accounts/commit_journal/rewind/cache/io/prompts/scenarios **무변경**.
- 회귀: **339 passed / 6 xfailed(WP-C/D/E/F) / XPASS 0**. 컴파일·임포트 OK.

### 변경/추가 파일(4차 패치)
- `cogs/gm.py` — `_plan_narrative`를 setup/manual consumer로, `_auto_replan_narrative`(자동)·`_apply_narrative_plan`(단일 소비부)·`_generate_narrative_plan_candidate`(producer) 분리; `_update_narrative_progress` 스케줄 시점 정체성 복사·자동 소비부 연결; `import copy`.
- `core/turn_preparation.py` — `superseded_by_newer_attempt`(일반 stale 판정, `extraction_is_stale` 위임), `NarrativePlanMutationPlan`, `normalize_narrative_plan`, `build_narrative_plan`, `narrative_replan_key`.
- `tests/policy/test_narrative_replan_plan.py`(신규, N-B01~09 = 15 케이스).
- `tests/policy/test_irregular_npc_plan.py`(I-B04 교정 2모드, I-B04c 추가 — 테스트만).
