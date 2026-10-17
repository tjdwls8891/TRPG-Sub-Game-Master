# INDAIM P1 구현 보고 — 묘사 전 세계 진행 · 후속 묘사 은퇴 · 비용 정합 (v7.18.0)

> 지시 근거: `INDAIM_P1_Claude_개발패키지_v1_2026-10-17`(01 구현지시 · 02 정책대장 · 03 상세설계 v0.2 · 04 검증매트릭스)
> 선행 조사: `handoff/P1_INVESTIGATION_REPORT.md`(덮어쓰지 않음)
> 최종 상태: **완료** — 단, 외부 청구 대조(X4)는 환경에 GCP 청구 내보내기·권한이 없어 **미실행**(정책 Q3/D6의 조건부 B에 따른 처리). 필수 서사 보장 수준은 §3-B에 그대로 적었다(의미 보장 아님).

---

## 1. 기준 · 결과 SHA

| 항목 | 값 |
|---|---|
| 시작 브랜치 / HEAD | `docs/roadmap-post-v7.17.3` / `fb7c2146a5da1abdae7ec673891fd6d66c629823`(= main `c6b374b` + 조사 문서 2커밋, 코드 차이 0) |
| 작업 트리(시작) | 깨끗함 |
| 구현 브랜치 | `claude/p1-world-cost` — 최종 SHA는 이 보고서를 담은 커밋(커밋 메시지·최종 보고에 기재) |
| 버전 | 7.17.3 → **7.18.0**(MINOR — 하위호환 동작 변경·세션 필드 +1 기본값, `SCHEMA_VERSION` 4 유지) |

## 2. C1~C10 정합성 게이트(HEAD `fb7c214`, 조사 보고와 대조)

| ID | 근거(파일:함수) | 판정 | 비고 |
|---|---|---|---|
| C1 | `cogs/gm.py` `_finish_proceed_and_continue` → `_dispatch_proceed` → `_join_and_ready` → `_commit_ready_turn` → `_after_commit`(저장 → 파생 송출 → 이력 정리 → 비용 보고 → `refresh_display(turn_end)` → True) → `_start_round` | 일치 | 다음 선언은 표시 갱신을 await한 뒤. 본문 전달(의사 스트리밍)은 커밋 전 |
| C2 | `core/turn_world.py` `process_observation`·`ForegroundContinuationPlan`·`continuation_plan`, `gm._process_world_observation`·`_generate_continuation` | 일치 | `PLAN_NONE` 턴의 시간은 추출 `extra_minutes`만 — 조사 보고와 같음 |
| C3 | `turn_world.tie_option_labels/tie_orders_for`, `player_world.PLAN_TIE`, `gm._call_gm_logic`(런타임 스키마 사본 확장 선례: 화자 식별 `identity_schema`) | 세부 조정 | 지시층위 선택을 같은 런타임 확장 방식으로 추가(프롬프트 원본 불변) |
| C4 | `player_world._Run.observer`·`_progress`, `turn_world.process_observation` 진행 호출 | **확인 — 비대칭 재현** | 보충 진행에 `observer_items/stats/paid/used` 누락 → UNKNOWN. 활동(`OBSERVER_ACTIVITY`)은 묘사 전 이동 단계도 주지 않음(대칭) |
| C5 | `speaker_card` 항목별 공개(`all_info` AND)·`identity_info_ids`, 관찰 매니페스트 X키, `display.refresh_display` | 일치 | 이름·얼굴 식별은 지시층위(묘사 전) 결정 → 보충 진행 인지 → 같은 커밋. 카드는 커밋 인지 기준 |
| C6 | `CommitCoordinator`, `turn_preparation.CANONICAL_DOMAINS`, `turn_history.REVERSIBLE_FIELDS`, 재서술 `rerender_world_intent` | 일치 | 새 필드는 가역 정본·커밋 경로 단일 writer로 등록 |
| C7 | provider 호출 19곳 + 캐시, `cost.py`·`cost_ledger.py`·`settlement.py`·`estimate.py`·`logs.py`·`tts.py` | **확인 — 누락 재현** | TTS 오디오 가공 실패·늦은 응답 usage 누락, 늦은 텍스트 응답 무응답 시 침묵 |
| C8 | `cost.build_turn_cost_embed`·`format_breakdown`, `game.py` TTS 로그 항목 | **확인 — 불일치 재현** | TTS 항목에 모델 없음 → 텍스트 단가로 분해, 토큰 합계 혼합 |
| C9 | `session_removal`(게임 폴더 삭제 · 재무 증빙 보관 `data/session_finance`), 전역 원장 `data/cost_ledger.jsonl` | 세부 조정 | 원장은 세션 폴더와 독립. 시나리오 차원이 이벤트에 없었음 → 추가 |
| C10 | `world_turn` None(LEGACY), 합성 `$session_pc` 인지 사건(`test_h06b_pc_awareness` 선례) | 일치 | LEGACY는 새 경로를 타지 않는다. 합성 인지 조건으로 별도 검증(§5) |

본질 충돌: **없음**. 사용자 재결정이 필요한 사항: 없음.

## 3. 사용자 요구 대응표

### 3-A. 정책

| 결정 | 구현 | 위치 |
|---|---|---|
| Q1/D1 묘사 전 진행 · 판단 제안 시간 | 판단 런타임 스키마 `action_intent.action_minutes`(필수 정수 0~240; 초과는 240·`CLAMPED`; 음수·불리언·누락은 시간 없음). 이동·명시 대기·TRANSIT 규칙 우선(행동 시간은 그 경우 무시, TRANSIT에선 진단만). `_plan_action`이 묘사 전 코드 소유 경과로 진행 → 오버레이 "경과 시간". 추출 `extra_minutes`는 DYNAMIC에서 항상 거부(이중 가산 없음). 의미 기록에 남아 재서술이 같은 시간을 재현 | `core/action_intent.py`, `core/player_world.py`, `core/turn_world.py` |
| D2 선후 | 정본 인과·우선순위는 엔진(H04) 그대로. 남은 자유 경합(PLAN_TIE)만 지시층위 `tie_order`(O키 enum) → 무효·미선택·이어지는 경합은 정렬 첫 허용 순서(결정적). 묘사 전 `resolve_plan_tie`로 재계획 — 묘사·추출은 확정 결과만 받음 | `turn_world.resolve_plan_tie/tie_choice_schema`, `gm._call_gm_logic`·`_attach_world_turn` |
| D3 같은 턴 인지·표시 | 묘사 후 노출·이름/얼굴 식별·READ·발견은 같은 커밋 인지(기존 경로 유지). 이 인지로 같은 분에 성립한 사건은 보충 진행이 평가 — 가시 결과는 **발생 = 이번 커밋, 전달 = `world_pending_signals` 대기**(구분 영속, 다음 본문 '그사이'). 표시 갱신은 커밋 뒤·다음 선언 전(실패해도 재커밋·재청구·무한 대기 없음) | `turn_world.process_observation`, `gm._apply_commit_effects`, 테스트 W11 |
| D4 직접 반응 같은 턴 | 접근 확인 지급은 그 계획의 이후 진행 단계가 즉시 `OBSERVER_PAID_ITEM`으로 봄(`_note_payments`). 묘사 후 확정 사용·지급은 같은 턴 보충 진행 평가 + `evaluated_same_turn` 표식으로 다음 턴 재평가 없음(이중 반응 금지). P1 이전 기록은 기존 의미 | `player_world._note_payments`, `items.current_facts`, `gm._process_world_observation` |
| 추가 — 필수 서사 | 묘사 오버레이에 `[필수 서사 — …반드시 담을 것]`(관찰 가능 신호 cue만 — 숨은 사건·미인지 정체 비노출, 기존 `observable_signals` 경계). 전달 대기분은 `[…아직 전하지 못한 일 — '그사이']`(작중 시각 포함) | `turn_world.narration_overlay`·`pending_delivery_block` |
| #1 후속 묘사 제거 | `_generate_continuation`·`ForegroundContinuationPlan`·`TASK_FOREGROUND_CONTINUATION` 삭제, provider 호출 지점 19 → 18. 제2 소설형 호출·문단 없음 | `cogs/gm.py`, `core/turn_world.py` |
| Q2/D5 | 플레이어 예상 비용 UI 명칭·위치·형식·권한 불변(`format_estimate` 형식 테스트). 숫자만: 원장 통계·부가 호출·TTS 가시 본문·세션 생성 안내 캐시 근사 통일 | `core/estimate.py`, `core/session_flow.py` |
| Q3/D6 | 내부 정합 완료(§6). 외부 대조 미실행(§7) | — |

### 3-B. 필수 서사 검증 수준(거짓 보장 금지)

| 수준 | 적용 |
|---|---|
| 프롬프트 지시 | **적용** — 필수 항목·대기 항목을 묘사 입력의 독립 블록으로 준다 |
| 결정적 구문·근거 검증 | **미적용** — 문자열 일치 검사는 하지 않는다(표현 자유를 막고 오탐이 크다) |
| 추출층위 표현 판독 | **적용** — 기존 추출 호출의 `represented_signals`(S키)가 근거. LLM 판독이므로 의미 보장 아님 |
| 실제 의미 보장 | **아님** |
| 송출 전 버퍼링 | **미적용** — 본문은 확정 직후 추출과 겹쳐 의사 스트리밍된다. 추출 결과를 기다리면 모든 턴 송출이 추출 시간만큼 늦어진다(묘사 전 경과 시간 이동으로 미표현 위험의 주원인이 사라졌으므로 지연 비용 대비 효익이 낮다고 판단) |

근거 미확인 항목은 세계를 커밋하되 **신호를 버리지 않고** 전달 대기로 남긴다(이미 송출된 본문은 회수할 수 없으므로 실패·재시도 대신 영속 전달 대기를 택함). 같은 신호를 3회 연속 놓치면 `PENDING_SIGNAL_RETIRED` 진단과 함께 은퇴(무한 대기 방지).

## 4. 변경 파일 · 주요 함수 · UX

| 파일 | 변경 |
|---|---|
| `core/action_intent.py` | `action_minutes` 스키마·안내·검증·의미 기록 |
| `core/player_world.py` | `_plan_action`·`OUT_ACTION_*`, `PlayerWorldPlan.observer`, `_note_payments`(같은 턴 지급) |
| `core/turn_world.py` | 필수 서사 오버레이, `pending_signals`·`pending_delivery_block`, 관찰 매니페스트(대기 신호 포함·추가 시간 거부), `process_observation`(관찰자 문맥·아이템 사실·대기 산출), `resolve_plan_tie`·`tie_choice_schema`, 후속 묘사 삭제 |
| `core/world_state.py` · `models.py` · `io.py` · `turn_history.py` · `turn_preparation.py` | `DELIVERY_FIELDS = {"world_pending_signals": []}` 단일 출처 등록(가역 정본 · 커밋 도메인 · 세션 필드) |
| `core/items.py` | `current_facts` — `evaluated_same_turn`이면 다음 턴 재평가 없음 |
| `cogs/gm.py` | 지시층위 `tie_order`, 계획 부착 시 선후 확정, 관찰 처리에 대기·아이템 사실 전달, 커밋 적용에서 대기 교체, 후속 묘사 경로 삭제, `cost.estimate` 로그, 턴 비용 보고에 예상 전달 |
| `core/tts.py` | `_TTSObserver`(시도 계수·usage 선기록·늦은 응답 1회·UNKNOWN), `tts_usage_cost` |
| `core/cost_ledger.py` | `session_dims`(시나리오·revision·모드), 파생 차원 지문 제외, `log_unknown_cost`, 늦은 텍스트 무응답 UNKNOWN, `cost.event` 로그 용어 |
| `core/cache_lifecycle.py` | 캐시 CostEvent에 같은 차원 |
| `core/cost.py` | `_entry_parts`(항목 모델 단가), `format_breakdown`, `build_turn_cost_embed`(모델별 토큰 합계·원장 원가 대비·운영자 부담·예상 대비) |
| `core/estimate.py` | `ledger_profile`(원장 파생 통계·캐시), 층위 출력·입력 하한·부가 호출·TTS 원장 분포, `basis` |
| `core/session_flow.py` | 가격 안내 캐시 근사 통일, 입력 총량 산식 정정 |
| `core/io.py` · `core/logs.py` | `cost.usage`의 `out_billed`, 용어 주석 |
| 문서 | `CLAUDE.md`(버전·턴 순서·TTS·반영·테스트 수·v7.14~15 병합 정정), `DEVLOG.md`(버전 기록), 이 보고서 |

**플레이어 UX**: 대화·조사도 그 시간만큼 세계가 흐르고(일과 이동·사건), 그 결과가 **한 본문** 안에 나온다. 뒤늦은 보충 문단은 없다. 본문이 놓친 관찰된 일은 다음 본문에 '그사이' 일로 나온다. 비용 표시 형식은 그대로다.
**운영자 UX**: 턴 비용 보고에서 TTS 줄이 오디오 단가로 맞게 분해되고 합계가 항목 합과 같다. 원장 원가·호출 내역 차이, 운영자 부담, 예상 대비(근거·신뢰도)가 보인다. 구조 로그에 `cost.estimate`·`cost.unknown`이 생긴다.

## 5. 검증 매트릭스(04) 결과

명령: 표적 `python3 -m pytest -q <파일>` 반복 + 최종 전체 `python3 -m pytest tests/ -q` **1회** → **1518 passed, 0 failed**(이전 1491 + 신규 27). 검증 루틴 ①②④⑤⑥ OK(cogs 9 · 명령어 46 · views 5, 시나리오 ERROR 0 · revision 불변).

| ID | 결과 | 근거 테스트 |
|---|---|---|
| W1 | 통과 | `test_action_minutes_advance_before_narration_and_extraction_time_is_ignored`, `test_explicit_wait_wins_over_action_minutes`, `test_missing_or_invalid_action_minutes_never_invent_time`, `test_code_owned_duration_not_double_counted`, `test_h06_r2_transit`(TRANSIT 재개 12분) |
| W2 | 통과 | `test_action_time_reaction_is_in_the_single_narration`(묘사 1회·후속 0·필수 서사) |
| W3 | 통과 | `test_foreground_same_minute_tie_order_chosen_by_instruction_before_narration`, `test_invalid_tie_choice_falls_back_to_deterministic_code_order`(3변형), `test_r2_3_*` |
| W4 | 통과 | `test_hidden_truth_not_in_prompts`, `test_offscreen_transition_or_future_schedule_creates_no_obligation`, `test_r2_2_*`(미래 신호·같은 장면 NPC 비밀 비노출) |
| W5 | 통과 | `test_post_narration_progression_uses_plan_observer_context`(소지 시 성립·미소지 시 불성립 — 임의 통과 없음) |
| W6 | 통과(기존 + D4) | `test_v717_items` E01·E03, `test_access_payment_is_visible_to_later_steps_of_the_same_plan`, `test_P2_use_evaluated_same_turn_*`(되감기 일치) |
| W7 | 통과(기존) | `test_svg_speaker_card`: `test_header_follows_committed_name_knowledge_and_identification`, `test_name_identification_is_independent_testimony_acquisition`, `test_face_reveal_uses_turn_variation_only_when_face_known` |
| W8 | 통과 | `test_pc_awareness_event_after_narration_is_kept_pending_not_lost`(합성 `$session_pc` 인지 조건 — 영도 0건과 무관), `test_same_turn_paid_fact_is_evaluated_in_supplemental_progression` |
| W9 | 통과(수준 명시) | `test_unrepresented_signal_is_kept_pending_then_delivered_next_body`, `test_pending_signal_retires_after_repeated_misses` — §3-B 수준 |
| W10 | 통과 | `test_commit_failure_keeps_pending_and_world_unchanged`, `test_pending_delivery_field_is_reversible_canonical`, `test_main_narration_failure_discards_plan`, `test_extraction_failure_keeps_pre_state_then_retry_commits`, 기존 되감기·재서술·복구 스위트 |
| W11 | 통과 | `test_committed_display_refresh_precedes_next_prompt_and_failure_is_bounded`(2변형) |
| F1 | 통과(표) | §6 호출 경로 표 · `test_text_late_attempt_without_response_is_unknown_not_zero` |
| F2 | 통과 | `test_tts_usage_recorded_even_when_audio_extraction_fails` |
| F3 | 통과 | `test_tts_late_response_after_timeout_recorded_once`, `test_tts_duplicate_late_callback_and_unknown` |
| F4 | 통과 | `test_turn_cost_report_uses_each_entry_model_rate_and_sums_match` |
| F5 | 통과 | `test_log_usage_terms_and_dimensions` |
| F6 | 통과 | `test_log_usage_terms_and_dimensions`, `test_derived_dimensions_do_not_break_idempotent_replay`, `test_ledger_statistics_survive_session_folder_removal` |
| F7 | 통과 | `test_estimate_uses_ledger_statistics_then_falls_back`, `test_tts_estimate_excludes_thinking_and_uses_ledger_when_available`, 기존 `test_roll_clock_estimate` |
| F8 | 통과 | 같은 테스트의 `format_estimate` 형식 단언 |
| X1 | 통과 | LEGACY 전 스위트 회귀 0, 합성 인지 조건 W8 |
| X2 | 통과 | `prompts.py` 불변. 런타임 스키마 사본·사용자 프롬프트 블록만(판단 +1 필드·안내 3줄, 지시 TIE 때만 +1 필드·블록, 묘사 필수 서사 머리줄·대기 블록). provider 호출 수 −1(후속 묘사), 신규 호출 0 |
| X3 | 통과 | 비용 멤버십·재시도·정산 기존 스위트, 늦은 TTS는 동결 Settlement 밖 운영 사실 |
| X4 | **미실행**(환경) | §7 |

의도적으로 바꾼 기존 테스트(계약 변경, 단언 약화 아님): `test_h05bc_turn_wiring`(추출 시간 → 판단 시간, 묘사 선후 → 지시 선후, 후속 묘사 → 전달 대기), `test_h05b_observation`·`test_h05_r2_corrections`(같은 이유), `test_h06_r2_transit`(첫 턴 추출이 표현을 보고하도록 — 재발생 아님 단언 유지), `test_v717_stabilization`(D4 같은 턴 평가), `test_h07_prompt_authority`(19 → 18), `test_v717_faction_capabilities`(세션 필드 104 → 105).

## 6. 비용 — 개념 분리 · 누락 · UNKNOWN

| 개념 | 권위 | P1 이후 |
|---|---|---|
| provider usage | 응답 `usage_metadata` | 구조 로그 `calls`(원본) |
| 내부 공급자 원가 | CostEvent(기록 당시 단가·환율, `pricing_basis`) | TTS 가공 실패·늦은 응답도 기록, 통계 차원 추가 |
| 플레이어 청구 | Settlement → InkTransaction | **불변**(정책·반올림 경계·소급 없음) |
| 운영자 부담 | Settlement `non_player`·`system_unbilled`, 운영 OP | 턴 보고에 표시 |
| 미확정 | `cost.unknown` 구조 로그 | 신규 — CostEvent·0원 미생성 |
| 예측 | `estimate_turn`·`estimate_tts` | `cost.estimate` 로그 · 보고 '예상 대비' |

호출 경로별 계측: 조사 보고 Ⅶ-1 표의 19곳 중 후속 묘사 1곳 은퇴(18곳). 누락이던 TTS 2경로 해소. 남은 계측 한계: 응답 없는 일반 실패 시도(예외로 끝난 시도)는 청구 여부를 알 수 없어 기존대로 오류 로그만(공급자 4xx/5xx는 통상 비청구) — 타임아웃 뒤 응답 없음만 `cost.unknown`으로 분리.

정합: 턴 보고의 항목 줄 합 = 항목 합(소수 둘째 자리 표기 반올림 내), 토큰 합계 = 항목 합(모델별 단가), Settlement `provider_cost_krw`와 호출 내역 합의 차이를 표시(늦은 응답 등 원장에만 있는 사실이 있으면 '차이'로 드러난다). 1인당 전액 청구(N명 = 명목 N배)는 기존 재무 정책 그대로이며 보고에 '1인당 × 인원'으로 분리 표기.

## 7. 외부 청구 대조(X4)

- 현행: `genai.Client(api_key=…)` = Gemini Developer API. 이 환경에 GCP 결제 계정·BigQuery 내보내기·읽기 권한이 **없다**(확인 수단 없음 — 접근 시도·비밀 요청 안 함).
- 가능 수준: 내보내기가 켜지면 **일·SKU(모델)·통화 단위** 합계를 원장 `cost_usd`(같은 기간·모델, `metadata.scenario_id`로 내부 분해)와 대조. 요청별 매칭은 Developer API에 요청 라벨이 없어 불가(Vertex AI 이전 시 라벨 가능 — 라벨 키당 고유값 1,000 제한으로 세션 단위 부적합, 시나리오 단위 적합).
- 차이 요인: 반영 지연(수 시간 단위), 무료 등급·크레딧·세금, 환율(내부 고정 1500), 캐시 생성비를 토큰×단가로 계산·기록하는 점, 같은 프로젝트의 다른 키 사용.
- 필요 조치(사용자): 결제 계정 BigQuery 표준 사용량 내보내기 활성화 + 읽기 전용 서비스 계정 키(환경 비밀). 마련되면 별도 작업으로 집계 대조 스크립트만 추가한다.

## 8. 남은 위험 · 미검증 · 승인 대기

1. **운영 검증 미실시**: 실제 Gemini 응답에서 판단층위가 `action_minutes`를 얼마나 합리적으로 내는지, 추출의 `represented_signals` 판독 정확도는 실측하지 못했다(외부 호출 금지). 상한 240분은 기존 추가 시간 상한과 같다.
2. **재서술의 선후 선택**: 재서술은 지시층위를 다시 부르므로 자유 경합 선택이 원 턴과 다를 수 있다(무효 시 코드 결정은 결정적).
3. **보충 진행 같은 분 TIE**(인지·아이템 사실로 같은 분 경합이 생기는 드문 경우)는 기존대로 READY 불가(청구 0) — 지시층위가 이미 끝난 뒤라 선택 주체가 없다.
4. **예측 통계 캐시**: 원장 전체를 크기·수정 시각이 바뀔 때마다 다시 읽는다(측정 전 — 최적화하지 않음).
5. 승인 대기 없음(`prompts.py`·시나리오 무변경).

## 9. 범위 밖 발견(수정하지 않음)

- 본 묘사 호출이 `call_with_retry`·HTTP 타임아웃 없이 직접 호출(`cogs/game.py` `generate_with_retry`) — 조사 보고 Ⅹ-5 그대로.
- `_verify_proceed_instruction` 호출자 0(사문 코드).
- 동기 더빙 경로(`_stream_paragraphs_synced`)는 문단 화자별 목소리를 쓰지 않는다(P2 TTS 범위).
- 개연성 시뮬레이션이 세계 계획 전 레거시 미러로 실행(기존 결함 ⑥).
