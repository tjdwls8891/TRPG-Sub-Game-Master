# WP-G 완료 번들 — Legacy Retirement, Semantic Reconciliation & Final Stabilization

> **상태: IMPLEMENTED / AWAITING FINAL GATE.** 구현자는 WP-G나 7-WP 프로그램을 VERIFIED로 선언하지 않습니다. 최종 후보를 push한 뒤 독립 GPT 최종 게이트를 위해 멈춥니다. 다음 WP는 없습니다(There is no WP-H).

## 1. 식별 (인계서 §19 항목 1–3, 5–6)

| 항목 | 값 |
|---|---|
| exact start SHA | `c508ce76afef3c66182d879a18e91fb501a1ae0e` (fetch 후 확인, 시작 시 `git status --short` 공백, 베이스라인 `593 passed, 0 failed, 0 xfailed, 0 XPASS`) |
| 브랜치 | `claude/wp-g-legacy-retirement-final-stabilization` |
| 내부 커밋 | `99dd5fc` G1–G3 · `f410076` 결정 게이트 보고 · `bea5196` D1/D2-b/D3/U-2 · `003fff1` 매트릭스·문서 (코드 후보) |
| final candidate SHA | 이 번들·스캔·제어 동기화 제안을 담은 커밋(부모 = `003fff1d82bbee30a6abe9356e2af3393f7eb132`). 자기 참조를 피하려고 정확한 값은 push 직후 채팅 보고에 적습니다 |
| local == remote · literal `git status --short` | push 직후 채팅 보고에 원문으로 기재합니다 |

## 2. 변경 파일 (항목 4) — `git diff --name-status c508ce7`

```
M CLAUDE.md                      M core/estimate.py        M prompts.py
M cogs/errors.py                 M core/io.py              M scenarios/무협.json
M cogs/game.py                   M core/memory_plan.py     M specs/00_INDEX.md
M cogs/gm.py                     M core/models.py          M tests/characterize/test_execute_proceed_shared_paths.py
M cogs/media.py                  M core/prompt.py          M tests/characterize/test_harness_selfcheck.py
M cogs/system.py                 M core/session_flow.py    M tests/policy/test_accounts_strict.py
M core/__init__.py               M core/terms.py           M tests/policy/test_compression_safety.py
M core/accounts.py               M core/turn_preparation.py M tests/policy/test_ink_transactions.py
M core/cache.py                  M core/ui.py              M tests/policy/test_wp_d_patch.py
M core/cost.py                   M core/display.py         A tests/policy/test_wp_g_prompt_authority.py
                                                           A tests/policy/test_wp_g_retirement.py
A handoff/WP_G_PRE_EDIT_INVENTORY.md   A handoff/WP_G_DECISION_GATES.md
A handoff/WP_G_FINAL_AUDIT_MATRIX.md   A handoff/WP_G_FINAL_SCENARIO_MATRIX.md
A handoff/WP_G_POST_CHANGE_SCAN.txt    A handoff/WP_G_CONTROL_SYNC_PROPOSAL.md
A handoff/WP_G_COMPLETION_BUNDLE.md
```

`DEVLOG.md`는 `verify_docs --fix`가 82→83→82로 바꿨다가 되돌려 순변경이 없습니다.

## 3. 권위 지도 — 전/후 (항목 7)

| 영역 | 시작 SHA | 최종 후보 |
|---|---|---|
| `_execute_proceed` 호출자 | 자동 `_dispatch_proceed` · 인트로 `play_intro` · 수동 `!진행` | 자동 · 인트로 (스캔 A2) |
| 서술 직접 편집 | `!수정`이 Discord 메시지·`raw_logs`·`uncompressed_logs`를 커밋 이력 밖에서 교체 | 없음. 다시 쓰려면 `!재생성`(WP-E) |
| `!재생성` | WP-E `rerender_latest` 위임 | 동일(단일 구현 테스트로 고정) |
| 자원·상태 변이 | 추출 + 코드 검증(WP-B). 프롬프트·캐시는 여전히 `자:/태:` 태그를 가르침 | 추출 + 코드 검증. 프롬프트·캐시·컨텍스트가 같은 권위를 서술(스캔 A6 = 0) |
| 자동 GM 비용 상한 | `session.total_cost − gm_cost_baseline` (레거시 미러) | CostLedger 세션 provider 비용 − 활성화 스냅샷, fail-closed (스캔 B1 = 0) |
| 사용량·턴 비용 표시 | USD `=` KRW `=` 잉크 등식, 현재 환율 재해석 | 우주 분리: CostLedger 기록값 / Settlement·캐시 창·해석 저널 잉크 / 미러는 "참고" 표기 |
| 운영자 계정 쓰기 | tolerant(읽기 실패→빈 계정 덮어쓰기, 쓰기 실패 삼킴) | strict(`*_strict`, 실패는 실패로 보고). 레거시 writer 프로덕션 호출자 0 (스캔 B4) |
| 압축 비용 | 계정 효과 없는 "선결제/환급/추가" 표시 | 운영자 부담 유지비. CostEvent만 남음 (스캔 B8 = 0) |
| 압축 주기 부기 | 최초 생성에서만 `mark_compressed` | 적용된 모든 압축에서 갱신 |
| 프롬프트 권위 | 층위마다 다른 선형 서열(X1–X9 모순) | D1 종류별 권위(§6) |

WP-A~F 권위(TurnTransaction, READY 배리어, CommitCoordinator, CommitJournal, CostLedger, Settlement, InkTransaction, 커밋 이력, message lifecycle, 압축 출처, cache lifecycle, interpretation billing)는 새로 만들거나 교체하지 않았습니다.

## 4. 은퇴한 명령·경로와 보존한 운영자 도구 (항목 8)

**은퇴**
- `!진행` 명령과 명령 전용 "지시 없음 → 지시층위 자동 생성" 분기(AUD-016).
- `!수정` 명령과 직접 편집 경로, 그 경로만 소비하던 턴 앵커 쓰기. 필드 `last_turn_anchor_id`는 구세션 로드 호환용으로 남겼고 쓰지 않습니다(AUD-018).
- 허구 압축 선결제: 20% 가산·누적(`gm.py`), 정산(`estimate.py`의 `estimate_compression`/`compression_prepay`/`settle_compression`/`settle_on_session_close`), 디스플레이·세션 종료 표시, 필드 `compression_prepaid_krw`(AUD-026/027).
- 도움말·사용법표·TTS 문구의 `!진행`/`!수정`/`자:/태:` 안내.
- 명령어 수: 46 → 44.

**보존**: `!재생성` · `!출력물`(읽기 전용, 안내만 `!재생성`으로 변경) · `!되감기` · `!주사위` · `!기억압축` · `!노트` · `!캐시노트` · `!더빙`·`!더빙테스트` · 캐릭터·NPC 보정(`!증감` 등) · `!자동` 그룹 · 관리 도구 전부(`!지급`, `!잉크`, `!사용량`, `!배포`, `!재시작`, `!리로드`, `!캐시`, `!세션종료`, `!채널정리`, `!스페이스*`, `!권한*`, `!tts생성`). 마스터 채널 `[진행자]` 중계도 유지하며, D1에서 GM/운영자 지시로 분류했습니다. `상:/중:/하:` 이미지 태그와 `자:/태:` 방어적 strip도 유지합니다.

**운영자 역량 확인**: 은퇴한 두 명령의 기능은 자동 GM(턴 진행)과 `!재생성`(재서술)이 대신합니다. 자동 턴에는 더빙이 원래 적용되지 않았으므로(`not cost_log_prefix`), `!진행` 은퇴 뒤 더빙 적용 범위는 인트로입니다. 새 기능 확장은 하지 않고 문구만 정정했습니다.

## 5. 회계 소비자 전환 증거 (항목 9)

| 소비자 | 분류 | 결과 |
|---|---|---|
| 자동 GM 비용 상한 6곳 | OPERATIONAL_BUDGET_OR_CAP | `core.auto_cost_cap_reached` — CostLedger strict 세션 합계(모든 billing_hint), 원장 손상 시 도달 처리(fail-closed), 구세션 1회 재기준(`gm_cost_basis`) |
| `accrue()` 18곳 | COMPATIBILITY_MIRROR | 전부 CostLedger 관측과 짝(스캔 B2, 인벤토리 §B). 사실 관측은 제거하지 않음 |
| `total_cost`/`total_usd` 규칙 읽기 | — | 0 (스캔 B1, `test_g2_no_business_rule_reads_legacy_total_cost`) |
| 턴 비용 임베드 Σ | DISPLAY_ONLY | 등식 제거, 턴 청구 잉크 + 미러(참고) 분리 |
| `!사용량` / `!사용량 전체` | DISPLAY_ONLY | CostLedger 기록값(hint별) + 잉크(턴 Settlement 미러, 캐시 선불/환급, 해석 청구) |
| `turn_cost_log` | DISPLAY_ONLY | 호환 전용 표시 버퍼로 명시(금액 권위 아님) |
| `total_ink_spent`/`last_turn_*` | COMPATIBILITY_MIRROR | CommitCoordinator만 씀(무변경) |
| `add_ink`/`deduct_ink`/`set_balance`/`register_account` | ADMIN_ACCOUNT_TOOL | 프로덕션 호출자 0. 운영자 도구·가입선물은 strict 함수 사용 |
| 압축 선결제 | LEGACY_DEAD | 은퇴(D3) |

Settlement/InkTransaction 정책은 바꾸지 않았습니다(인계서 §17 중단 조건에 해당하지 않음).

## 6. 최종 프롬프트 권위 격자 (항목 10) — 사용자 결정 D1 (2026-09-29)

단일 선형 서열이 아니라 정보 종류별 권위로, 층위 역할에 맞게 표현했습니다(같은 한 줄을 모든 층위에 복사하지 않음).

| 층위 | 반영 |
|---|---|
| 묘사 (`SYSTEM_INSTRUCTION`) | PC 주권 최상위 원칙 유지. 선형 ①~④를 (가) 룰북 고정 사실·금지사항 불변 (나) 런타임 상태는 변하는 항목에 한해 우선 (다) note는 추가·축소만, 충돌 시 자동 적용 금지 (라) GM 지시는 사실·제약 안에서 진행 (마) 압축 기억 = 파생 (바) 플레이어 선언 = PC 행위 권위, 외부 세계 주장 = 미확인으로 교체 |
| 지시 (`GM_LOGIC_SYSTEM_INSTRUCTION`) | note는 서사 운영 원칙보다 우선하되 PC 주권 아래이고, 고정 사실·금지사항을 무효화하지 못하며, 런타임 상태와 충돌 시 constraint_check에 기록. `[진행자]`·`[진행자 (GM)]`·사이드 노트 = GM/운영자 진행 지시. 플레이어 외부 주장 = 미확인. `!진행` 정의·`자:/태:` 교습·few-shot 제거. `상:` 규칙과 언더바 규약(이미지 키워드 한정) 유지 |
| 판단 (`JUDGMENT_SYSTEM_INSTRUCTION`, 컨텍스트) | 세계 설정을 받지 않는 구조를 유지(확대 없음). note 라벨 = "진행 제약으로 반영(플레이어 주권이 우선)". 중계 줄은 플레이어 선언이 아니고, 외부 주장의 진위는 판정하지 않음 |
| 캐시 룰북 (`core/cache.py`) | "진행자 지시가 없는 한 유지" 삭제 → 고정 사실·[6]은 GM 지시·note로도 불변. NPC 런타임 우선은 변하는 항목에 한정(2곳). [4.5] 상태 목록은 추출+검증 권위. 캐시 노트 라벨에 한계 표기 |
| 묘사 컨텍스트 (`core/prompt.py`) | note 라벨에 한계, 최종 지시는 "룰북 사실·금지사항·묘사 가이드를 지키는 범위 안에서 GM 지시 최우선", NPC 런타임 블록은 변하는 항목 한정 |
| 지시 컨텍스트 (`cogs/gm.py`) | 상태 목록 라벨을 추출+검증 권위로 교체 |
| 추출 | 무변경 — 스토리·세계 권위로 승격하지 않음(테스트로 고정) |

증거: `tests/policy/test_wp_g_prompt_authority.py` 11건(원문 문구 + 런타임 조립 결과) · 스캔 C1·A6.

## 7. JSON/스키마 필드 결과 (항목 11)

| 필드 | 결과 |
|---|---|
| `cached_worldview_sections` | AUD-053 해소 — 생성자 초기화. harness의 "알려진 예외" 목록을 비우고 CLAUDE.md 검증 ②의 예외 줄을 삭제 |
| `gm_cost_basis` (신규) | 비용 상한 기준 우주 표식(SESSION_FIELDS 등록) |
| `compression_prepaid_krw` | 삭제(D3). 구세션 키는 레지스트리 기반 복구가 무시 |
| `last_turn_anchor_id` | 비활성 호환 필드(쓰기·읽기 0) |
| `keyword_memory` | live 유지, 의미를 문서로 정정(AUD-010) |
| `media_dir` | 런타임 미사용 사실을 문서화하고 유지(D2-c) |
| `job_guides` | 비활성 저작 데이터로 유지(D2-d) |
| SESSION_FIELDS | 82 (시작 82 → +`gm_cost_basis` −`compression_prepaid_krw`) |

## 8. 저작 콘텐츠 변경 (항목 12)

- `scenarios/무협.json`: `location_images`의 `장소키워드1`·`장소키워드2` 플레이스홀더 제거 → `{}`(D2-b 승인). 대체 키워드 창작 없음. 컨테이너를 유지하는 쪽이 더 작은 변경이고, 파서는 빈 dict를 목록 없음으로 처리합니다(`test_d2b_empty_location_images_injects_no_image_list`).
- 그 밖의 시나리오·NPC·세계관 문구는 무변경입니다(D2-a/c/d/e).

## 9. AUD-001..063 (항목 13)

`handoff/WP_G_FINAL_AUDIT_MATRIX.md` — RESOLVED_IN_CODE 52 · INTENTIONALLY_RETAINED_WITH_RATIONALE 11 · RETRACTED 0 · **NEEDS_INTENT 0**. 인용한 모든 테스트 함수가 실재하는지 자동 대조했습니다. strict xfail 변경은 없습니다(현재 xfail 0).

## 10. 최종 시나리오 매트릭스 (항목 14)

`handoff/WP_G_FINAL_SCENARIO_MATRIX.md` — 인계서 §14 전 사례, 69행 **전부 PASS**.

§16 보존 테스트 묶음(20 노드: 자동 트랜잭션 경계, READY, 전문 추출, CommitCoordinator 단일 커밋, FAILED_SYSTEM 0, CostEvent 보존, Settlement/Ink 정확히 한 번, WP-E 되감기·재서술, 현재 `!재생성`, 정리 부채, 디스플레이 lifecycle, stale 압축 거부, cache lifecycle, 운영자 환급, 해석 strict 사실)을 별도 실행한 결과: `22 passed`(파라미터 전개 포함).

## 11. 컴파일·임포트·봇 로딩 (항목 15)

```
① OK
② 재수출: OK | 필드: OK
③ OK
④ 문서 수치 OK (21항목)
⑤ cogs 9 | 명령어 44 | views 5
compile: OK   (python3 -m compileall main.py prompts.py core cogs tools)
import: OK    (core, prompts, main)
```

## 12. 최종 전체 회귀 (항목 16)

`631 passed, 0 failed, 0 xfailed, 0 XPASS` (`python3 -m pytest tests/ -q -p no:cacheprovider`). 시작 대비 +38건입니다: `test_wp_g_retirement.py` 23건 + `test_wp_g_prompt_authority.py` 11건 + U-2 4건(`test_compression_safety.py`). harness의 AUD-053 테스트는 기존 1건을 교체해 순증 0입니다.

의도적으로 갱신한 기존 테스트:
- `test_c004_*`: `_execute_proceed` 호출자 3 → 2(`!진행` 은퇴).
- `test_harness_selfcheck`: AUD-053 예외 목록을 비움.
- `test_accounts_strict::test_29`: strict 읽기 허용 목록에 `core/cost.py`·`cogs/system.py` 추가(사유 주석).
- `test_ink_transactions::test_70`: 레거시 `deduct_ink` 호출자 1 → 0.
- `test_wp_d_patch::test_dd4_admission_order_scan`: 상한 판정 이름이 `auto_cost_cap_reached`로 바뀜(순서 불변식은 그대로).
- `test_compression_safety::test_legacy_settle_*`: 선결제 정산 은퇴 반영.

## 13. prompts·scenarios diff 요약 (항목 17)

- `prompts.py`: +21 −14. 묘사층위 권위 문단 1곳, 지시층위(note 한계·진행자 지위·PROCEED 정의·태그 교습 제거·이미지 키워드 언더바 1줄·few-shot 태그 제거), 판단층위 3줄, 스키마 설명 1곳. 다른 규칙·예시·톤은 무변경입니다.
- `scenarios/무협.json`: +1 −4 (D2-b).
- 배포 시 `!캐시 재발급`이 필요합니다(캐시 룰북·프롬프트 변경).

## 14. 의도적으로 유지한 항목과 근거 (항목 18)

| 항목 | 근거 |
|---|---|
| 무협 `image_prompts` 플레이스홀더 | D2-a 사용자 결정 — 창작 금지. 의미 오류가 아닌 품질 문제 |
| `media_dir`·`job_guides` JSON 키 | D2-c/d 사용자 결정 |
| 무협 NPC 밀도, 시나리오 세대 차이 | D2-e / AUD-007 — 정규화·창작 금지 |
| 레거시 `add_ink`/`deduct_ink`/`set_balance`/`register_account` 정의 | 테스트 시드·하위 호환용. 프로덕션 호출자 0을 정적 테스트로 고정 |
| `total_cost`/`total_usd`/`turn_cost_log` 미러 | 표시 참고·cost_log 기록용. 규칙 입력 0 |
| 추출 재시도 버튼의 비준비(구 표식) 경로 | WP-C 이전에 저장된 세션의 복구용. 호출자 = 버튼 1곳(`test_c004e`) |
| `자:/태:` 방어적 strip | 모델이 태그를 다시 출력할 때 출력 오염 방지 — 권위 없음 |
| 더빙 인트로 한정 | 자동 턴 미적용은 기존 동작. 확장은 새 기능이라 범위 밖 |
| `__version__` v5.33.0 | 버전 상향은 배포 결정(제어 동기화 제안 §6) |
| 프로세스 통제 AUD(041/044/052/055/056) | 코드가 아닌 통제 요구 — 제어 동기화 제안으로 처리 |

## 15. 다음 WP (항목 19)

시작하지 않았습니다. 다음 WP는 없습니다.

## 16. 소스 아카이브 (항목 20)

`WP_G_SOURCE_<final-sha>.tar.gz` — 최종 커밋에서 `git archive`로 만들고 `media/`를 제외합니다. 아카이브와 SHA-256은 최종 커밋 뒤에 생성되므로(자기 참조 방지) 저장소에 커밋하지 않고 채팅에 첨부·보고합니다.

## 17. 산출물

- `handoff/WP_G_COMPLETION_BUNDLE.md` (이 문서)
- `handoff/WP_G_POST_CHANGE_SCAN.txt` (코드 후보 `003fff1` 기준 자동 스캔)
- `handoff/WP_G_FINAL_AUDIT_MATRIX.md`
- `handoff/WP_G_FINAL_SCENARIO_MATRIX.md`
- `handoff/WP_G_CONTROL_SYNC_PROPOSAL.md`
- 보조: `handoff/WP_G_PRE_EDIT_INVENTORY.md`, `handoff/WP_G_DECISION_GATES.md`
