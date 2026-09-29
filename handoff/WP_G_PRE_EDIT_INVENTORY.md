# WP-G 편집 전 인벤토리 (G0)

- 시작 SHA: `c508ce76afef3c66182d879a18e91fb501a1ae0e` (fetch 후 확인, `git status --short` 공백)
- 브랜치: `claude/wp-g-legacy-retirement-final-stabilization` (로컬 생성, 미push)
- 베이스라인: `593 passed, 0 failed, 0 xfailed, 0 XPASS`
- 이 문서 작성 시점까지 저장소 편집 0건

범례: 줄 번호는 시작 SHA 기준.

---

## A. LEGACY GAMEPLAY / COMMAND OWNERSHIP

### A-1. 명령어 전수 (46개 · cogs 9)

| cog | 명령어 (정의 줄) |
|---|---|
| character.py | 참가 314 · 캐릭터가져오기 362 · 설정 453 · 증감 489 · 외형 653 · 프로필 684 · 엔피씨 784 · 능력치 1055 · 설정생성 1123 |
| game.py | 주사위 133 · **진행 232** · 더빙테스트 1231 · 재생성 1296 · 출력물 1317 · **수정 1344** · 기억압축 1473 · 노트 1569 · 캐시노트 1607 |
| gm.py | 자동(그룹) 1021: 시작 1045 · 중단 1108 · 상태 1130 · 개입 1157 · 턴제한 1173 · 비용제한 1196 · 퀘스트 2765 · 서사 4986 · 원장 5054 · 재계획 5086 · 되감기 2843 |
| media.py | 이미지 22 · 브금 266 · 플리 552 · 볼륨 634 · 채팅 672 · 더빙 707 |
| permissions.py | 권한부여 55 · 권한회수 68 · 권한목록 79 |
| session.py | 새세션 378 · 시작 391 · 소개 559 |
| system.py | 명령어 22 · 채널정리 147 · 세션종료 187 · 캐시 243 · 리로드 341 · 지급 361 · 사용량 418 · 잉크 533 · 재시작 598 · 스페이스초기화 616 · 스페이스 666 · tts생성 684 · 배포 732 |

### A-2. 게임플레이 진입점·공유 엔진·정본 변이

| Entry/site | User-facing? | Current callers | Canonical state effect | Replacement authority | Action |
|---|---|---|---|---|---|
| `!진행` `game.py:232 proceed_turn` | 마스터 명령 | 사용자 입력만 | `_execute_proceed(preparation=None)` 경유: raw/uncompressed 로그 append, `turn_count+=1`, 추출 없음(AUD-016), Settlement·청구 없음 | 자동 GM 턴(TurnTransaction→READY→CommitCoordinator) | **RETIRE_COMMAND_ONLY** |
| `_execute_proceed` `game.py:246` | 아님 | gm.py:3742 `_dispatch_proceed`(자동, preparation 있음) · session.py:534 `play_intro`(preparation=None) · game.py:244(`!진행`) · 테스트 다수 | 자동: 로그 스테이징만. 인트로: 즉시 적용 | — | **KEEP_SHARED_ENGINE** |
| `_execute_proceed` 내 "지시 없음 → `_call_gm_logic` 자동 생성" 분기 `game.py:380-392` | 아님 | `!진행`(인자 없음)만 도달. 인트로 지시문은 항상 비어 있지 않음(`start_frame.py:202`), 자동은 `cost_log_prefix` 있음 | 지시층위 호출(비용) | — | **RETIRE_DEAD_PATH** (`!진행` 은퇴 후) |
| `_execute_proceed` `preparation is None` 즉시 적용 분기 `game.py:436-443`, 수동 비용 임베드 `game.py:917-927`, 예외 시 잠금 해제 `game.py:500-506, 518-524` | 아님 | 인트로가 계속 사용 | 인트로 1회 로그/카운터 | — | **KEEP_SHARED_ENGINE** (인트로 전용으로 주석 정정) |
| `last_turn_anchor_id` 기록 `game.py:317-321` | 아님 | 유일한 리더가 `!수정`(game.py:1374, 1388) | 세션 필드 쓰기 + `history(limit=1)` API 1회 | 없음 | **RETIRE_DEAD_PATH** (쓰기·조회 제거, 필드는 구세션 호환으로 SESSION_FIELDS에 유지) |
| `!수정` `game.py:1344 edit_last_output` | 마스터 명령 | 사용자 입력만 | Discord 메시지 편집/삭제/추가 + `raw_logs`·`uncompressed_logs`·game_chat 직접 교체. 커밋 이력·추출·되감기 밖 (AUD-018) | `!재생성`(WP-E rerender) · `!되감기` | **RETIRE_COMMAND_ONLY** (명령 전용 경로 전체 삭제) |
| `!출력물` `game.py:1317` | 마스터 명령 | 사용자 입력만 | 읽기 전용(raw_logs 최근 model 텍스트) | — | **KEEP_OPERATOR_TOOL** (`!수정` 안내 문구만 제거) |
| `!재생성` `game.py:1296` | 마스터 명령 | 사용자 입력 | `gm_cog.rerender_latest(session, addendum=)` 위임만. 다른 재생성 구현 없음(`regenerate`/`rerender` 정의는 gm.py:2232 하나, 디스플레이 뷰 gm.py:645도 같은 함수) | WP-E | **KEEP_AUTHORITATIVE** |
| `!되감기` `gm.py:2843`, `disp:rewind*` | 마스터/버튼 | — | WP-E 커밋 이력 SELECT | WP-E | **KEEP_AUTHORITATIVE** |
| 자동 턴 `_process_actions`→`_run_gm_logic_loop`→`_finish_proceed_and_continue`→`_dispatch_proceed` | 플레이어 입력 | on_message | 준비→READY→CommitCoordinator | — | **KEEP_AUTHORITATIVE** |
| 퀘스트 선택 적용 | 아님 | `turn_preparation.stage_instruction_effects` 호출 4곳(gm.py 2261, 2629, 2912, 3492). `_apply_quest_choice` 정의 0건(gm.py:5106 주석만) | 스테이징 → 커밋 | WP-B | **KEEP_AUTHORITATIVE** (AUD-005 이미 해소, 주석만 존재) |
| 추출 적용 `_run_extraction`/`_apply_extraction_plan` | 아님 | 준비 모드: `_prepare_extraction`(gm.py:1667). 비준비 모드: 추출 재시도 버튼의 **구 표식 컨텍스트**(`{"text":..}`, gm.py:910)만. 커밋 경계: gm.py:2397(derived) | 계획만 적용(LegacyCompatibilityApplier) | WP-C/D | **KEEP_SHARED_ENGINE** — 비준비 경로는 WP-C 이전에 저장된 세션의 복구용으로 유지(근거 기록) |
| `자:/태:` 태그 | — | `_execute_proceed` 파싱(game.py:336-337)은 **strip 전용**(WP-B에서 변이 권위 제거, game.py:359-366). 응답 방어적 strip(game.py:562-563, 741-742) | 없음 | 추출층위 | **KEEP_SHARED_ENGINE** (방어적 strip 유지 — 모델 재출력 대비) · 의미 잔재는 §C/§E |
| `상:/중:/하:` 이미지 태그 | — | game.py:335, 343-357 | 이미지 송출 | — | **KEEP** (정규식 과잉 제거 금지) |
| 마스터 채널 발화 중계 `game.py:109-117` | 마스터 채팅 | on_message | 게임 채널 스트리밍 + `current_turn_logs`에 `[진행자]` 추가 | — | **KEEP_OPERATOR_TOOL** (D1에서 권위 위치 결정 대상) |
| `!증감` `!설정` `!외형` `!엔피씨 설정/삭제` `!능력치` `!캐릭터가져오기` | 마스터 명령 | 사용자 입력 | 커밋 경로 밖 직접 변이 + 저장 | 운영자 보정 수단(대체 없음) | **KEEP_OPERATOR_TOOL** |
| `!자동 퀘스트 열기/닫기/진전` gm.py:2765 | 마스터 명령 | 사용자 입력 | `quest_state` 직접 변이 | 운영자 보정 수단 | **KEEP_OPERATOR_TOOL** |
| `!주사위` `!기억압축` `!노트` `!캐시노트` `!자동 개입/재계획/서사/원장` | 마스터 명령 | 사용자 입력 | 각각 판정 UI / 수동 압축(WP-F 출처 가드) / note / cache_note / GM 보조 | — | **KEEP_OPERATOR_TOOL** |
| `!더빙` `!더빙테스트` · `disp:tts` | 마스터/버튼 | — | `tts_enabled` 토글. 더빙은 `not cost_log_prefix`일 때만 실행(game.py:815-819) → 현재 **인트로와 `!진행`에서만** 작동, 자동 턴에는 원래 미적용 | — | **KEEP_OPERATOR_TOOL** — 도움말을 "인트로 한정(자동 턴 미적용)"으로 정정. 자동 턴 더빙 확장은 신규 기능이라 범위 밖(§L 참고) |
| 관리 도구 `!지급` `!잉크` `!배포` `!재시작` `!리로드` `!캐시` `!세션종료` `!채널정리` `!스페이스*` `!권한*` `!tts생성` | 오너/마스터 | — | 계정·배포·채널·권한 | — | **KEEP_OPERATOR_TOOL** (삭제 금지) |

**`!진행` 은퇴 영향 증명 대상:** 자동 경로는 `_dispatch_proceed`만 `_execute_proceed`를 부른다(characterize `test_c004d`). 인트로는 `play_intro`. `test_c004` "세 호출자" 단언은 의도적으로 "두 호출자(자동·인트로)"로 바뀐다.

---

## B. LEGACY ACCOUNTING CONSUMERS

### B-1. 필드별 읽기/쓰기

| 필드/호출 | 사이트 | 분류 | 조치 |
|---|---|---|---|
| `accrue()` 18곳 | game.py 684·910·1039·1290·1528 · gm.py 2981·3169·3636·4007·4173·4618·4785·4926·5539 · media.py 196 · character.py 1203 · cache_lifecycle.py 455·519 | COMPATIBILITY_MIRROR — 전 사이트 CostLedger 관측 짝 확인(character.py 1203은 `utils.generate_character_details` 내부 `_cl_op.record`, game.py 910·1290 TTS는 `tts.py:167 record_context_event`) | 유지하되 비권위 표기. 사실 관측은 절대 제거하지 않음 |
| `total_cost` 쓰기 | `cost.accrue` 단일 | COMPATIBILITY_MIRROR | 유지 |
| `total_cost` 읽기 — **자동 GM 비용 상한** | gm.py 1122·1137·1425·1463·2300·3468, 기준 gm.py:1078 · session_flow.py:684 | **OPERATIONAL_BUDGET_OR_CAP — 현재 레거시 필드에 의존하는 유일한 규칙** | **MIGRATE**: CostLedger 세션 범위 합계로 전환(§I U-3) |
| `total_cost`/`total_usd` 표시 | cost.py `build_turn_cost_embed` Σ 누적(`USD\n= KRW` — **서로 다른 우주를 등식으로 표시, AUD-033 위반**) · system.py `!사용량`(`usd_to_krw(usd)` 현재 환율 재해석 + "= 결제 N잉크" 등식 + "환율 변동" 경고가 우주 차이를 환율로 오해) · `_usage_all`(total_usd 없으면 total_cost/환율 역산) · 캐시/압축 임베드 누적 표시 | DISPLAY_ONLY (오표시) | **MIGRATE**: CostLedger(제공자 비용, 기록 당시 KRW/USD 그대로) · 플레이어 잉크(Settlement 미러 + 캐시 창 선불/환급 + 해석 청구 저널) · 무료/운영자/시스템 분리 표시. 등식 표기 제거 |
| `turn_cost_log` | 쓰기: game.py 713·913, gm.py 2999·3189·3656·4019·4185·4631·4938·5557 · 비우기: gm.py 2035·2052·2107, game.py 926 · 읽기: `build_turn_cost_embed`(호출 내역), `!사용량` | DISPLAY_ONLY — 금액 권위 아님(Settlement 임베드는 `settlement.player_billable_cost_krw`·`charge_ink_per_user`만 청구액으로 표시, 호출 합계는 "참고") | 유지·명시적 **compatibility-only 표시 버퍼**로 표기. `!사용량`의 "직전 턴 호출" 표기는 "진행 중 턴 호출 내역(참고)"로 정정 |
| `total_ink_spent` | 쓰기: commit_coordinator.py:606 (Settlement 파생 미러) · 읽기: display.py:72, 턴 임베드, `!사용량`, `_usage_all` | COMPATIBILITY_MIRROR (턴 청구분만 — 캐시 선불/해석 청구 미포함) | 유지. 표시 시 "턴 청구 잉크"로 한정 명시 |
| `last_turn_cost` / `last_turn_ink` | commit_coordinator.py:607-608 쓰기 · display.py:75 읽기 | COMPATIBILITY_MIRROR / DISPLAY_ONLY | 유지 |
| `gm_cost_baseline` | gm.py:1078, session_flow.py:684 쓰기 | OPERATIONAL_BUDGET_OR_CAP | 상한 전환과 함께 의미 변경(§I U-3) |
| `compression_prepaid_krw` / `compression_prepay` / `settle_compression` / `settle_on_session_close` | gm.py:2539-2558 누적(예상액에 20% 가산) · display.py:89 "**압축 선결제 N잉크**" 플레이어 채널 표시 · game.py:959-980 "환급/추가 N잉크(표시용 — 계정 반영 없음)" · ui.py:57-66 세션 종료 "환급 N잉크" 로그 | **LEGACY_DEAD (재무 효과 없는 허구 결제 표시)** — 실제 압축 CostEvent는 session 범위, 턴 Settlement 미포함 → 사실상 운영자 부담 (WP-F bundle §18-5 "정책 미정") | **NEEDS_USER_DECISION D3** (§I) |
| `interpret_cost_krw` | interpretation_billing `_mirror` | COMPATIBILITY_MIRROR (저널이 권위) | 유지 |
| `profile_ai_cost_krw` | profile_ai `_accrue`(usd만 total_usd에, krw는 total_cost 제외) | DISPLAY_ONLY (무료분) | 유지 — total_cost/total_usd가 다른 우주인 원인 중 하나로 문서화 |
| `add_ink` | system.py:400 `!지급` · terms.py:114 가입선물 | ADMIN_ACCOUNT_TOOL — **레거시 tolerant 쓰기**(`load_account` 읽기 실패 시 빈 계정 → 잔액 덮어쓰기 위험, `_write_account` 실패를 무시하고 성공 보고: AUD-061 잔재) | 도구 유지 + **strict 영속화로 교체** (실패는 실패로 보고) |
| `deduct_ink` | system.py:403 `!지급 음수`(회수) | ADMIN_ACCOUNT_TOOL (동일 결함) | 동일 |
| `set_balance` | system.py:579 `!잉크` | ADMIN_ACCOUNT_TOOL (동일 결함, `ok` 반환은 있으나 읽기 실패 흡수) | 동일 |
| 세션 종료 통계 | ui.py:35 주석(WP-D에서 재계상 제거, AUD-028) | AUTHORITATIVE(stats) | 유지 |

### B-2. 결론
- 레거시 필드를 **독립 재무 진실로 소비하는 비즈니스 규칙은 자동 GM 비용 상한 1건**뿐이다 → 전환 대상.
- 표시 오류: Σ 누적 등식, `!사용량` 등식·환율 해석, 압축 선결제 허구 결제 표시.
- 관리 도구의 tolerant 쓰기(AUD-061 레거시 잔재) → 도구 유지, 쓰기만 strict로.

---

## C. PROMPT AUTHORITY MATRIX

| Layer | System prompt | Actual user/context blocks | Fixed scenario facts | session.note | PC sovereignty | operator/GM direction | memory/player claim precedence |
|---|---|---|---|---|---|---|---|
| judgment | `JUDGMENT_SYSTEM_INSTRUCTION` prompts.py:581 | gm.py:100-170: 최근 로그·능력치·note만(캐시 미사용). note 블록 제목 "**[실시간 노트 — 이번 판단에 우선 적용]**"(gm.py:166) | **받지 않음**(설계상). "세계관 설정을 창작하지 않습니다"(prompts.py:585) | "이번 판단에 우선 적용" — 무엇보다 우선인지 미정 | "[최우선 절대 원칙 — 플레이어 주권 보호 / 이하 규칙보다 항상 우선]"(prompts.py:604) | 마스터 중계 발화는 `[진행자]`로 로그에 섞여 들어옴 | 규정 없음(player_intent를 선언 기준으로 확정) |
| plausibility/simulation | `NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION` prompts.py:1621 | 캐시 + 행동/세계 상태 | "캐시 내 시나리오 설정에서 … 세계 진실 명제" 추출, 반증 우선(prompts.py:1641-1655) | **언급 없음** | 언급 없음(판정 재료 층위) | 없음 | 규정 없음 |
| instruction | `GM_LOGIC_SYSTEM_INSTRUCTION` prompts.py:795 | gm.py:275-560: 사이드노트 "[GM 사이드 노트 (이번 턴 적용)]", note "▶ 실시간 노트 (GM 직접 관리)", 장소 이미지 목록, **"[유효 상태이상 목록 — 태: 태그는 이 목록에 있는 이름만 사용 가능]"(gm.py:358)**, 말미 "[시나리오 금지사항 — … 반드시 준수]"(gm.py:490) | 캐시 룰북 | "**■ 실시간 노트(session.note)는 모든 원칙보다 절대 우선한다** … 서사 계획·긴장 생성 원칙·능동 서사 원칙·**시나리오 금지사항보다 위에 있는 최상위 제약**"(prompts.py:870-871) + constraint_check ①노트 ②금지사항(prompts.py:673-674, 812-813) | "[최우선 절대 원칙 — 플레이어 주권 보호 / 이하 규칙보다 항상 우선]"(prompts.py:825) | proceed_instruction = "**인간 GM의 !진행 인자처럼 동작하는 지시문**"(prompts.py:966), 스키마 "**!진행 인자 형태 지시문. 자/태/상중하 태그 포함 가능**"(prompts.py:738) | "서사 계획과 플레이어 선언이 다르면 플레이어 선언"(prompts.py:998) |
| narration | `SYSTEM_INSTRUCTION` prompts.py:26 | PromptBuilder(core/prompt.py): note "▶ 실시간 노트 (GM 직접 관리)"(prompt.py:208, 우선순위 표기 없음), NPC 런타임 "(캐시 룰북 [3. NPC 사전]보다 우선 적용)"(prompt.py:190), 최종 지시 "캐시된 룰북의 묘사 가이드와 위 GM의 지시사항을 최우선으로 반영"(prompt.py:251) | "세계관 설정 불변성 … **① 시나리오 룰북(캐시) > ② GM 지시사항 > ③ 압축 기억 > ④ 플레이어 발언**"(prompts.py:126) | **위 서열에 note 자리가 없음** | "★ [최우선 원칙] … 아래 모든 금지 사항보다 우선"(prompts.py:121), 재강조(prompts.py:169) | ② GM 지시사항 | ① > ② > ③ > ④ (명시) |
| extraction | `EXTRACTION_SYSTEM_INSTRUCTION` prompts.py:435 | 묘사문 + `build_extraction_limits`(유효 상태·NPC 목록) | 받지 않음 | 받지 않음 | 해당 없음 | 해당 없음 | "묘사문에 실제로 드러난 것만"(prompts.py:439-445) — 스토리 권위 아님 |
| cache/world context | core/cache.py 룰북 조립 | [4.5] "**(태: 태그로 캐릭터에 부여하거나 제거할 수 있는 공식 상태이상 목록이다. 이 목록에 존재하는 이름만 태: 태그에 사용해야 한다 …)**"(cache.py:236-238) · [6] "**(아래 요소들은 플레이어나 GM의 요청이 있더라도 예외 없이 준수한다 …)**"(cache.py:255-256) | 룰북 전체 | — | — | "GM의 요청이 있더라도 예외 없이" | — |

### C-1. 모순 (요약하지 않고 그대로)

| # | 모순 | 위치 |
|---|---|---|
| X1 | 지시층위: note가 **시나리오 금지사항보다 위** ↔ 캐시 [6]: 금지사항은 **GM의 요청이 있더라도 예외 없이** ↔ 묘사층위: **룰북 > GM 지시** | prompts.py:870-871 ↔ cache.py:256 ↔ prompts.py:126 |
| X2 | 지시층위: note가 "**모든 원칙보다 절대 우선**" ↔ 같은 프롬프트의 "플레이어 주권 … **이하 규칙보다 항상 우선**"(note 규칙은 그 '이하'에 위치) | prompts.py:870 ↔ 825 |
| X3 | gm.py:281-282 주석: note에는 "**PC 신분·세계관·기정사실 등 GM이 고정한 내용**"이 담긴다 ↔ 묘사층위 서열에 note가 없고 GM 지시(②)는 룰북(①) 아래 → note가 세계 사실을 추가/변경할 수 있는지 층위마다 다름 | gm.py:281 ↔ prompts.py:126 |
| X4 | 판단층위는 note를 "이번 판단에 우선 적용"하지만 금지사항·세계 사실은 받지 않음 → note와 세계 사실이 충돌해도 판단층위는 알 수 없음(설계상 한계, 모순이라기보다 공백) | gm.py:166 |
| X5 | 지시층위 PROCEED 규칙이 `자:/태:` 태그를 **가르친 직후 금지**: 예시 "자:정원모;물;-1 … 태:정원모;출혈", 언더바 규약 예, **좋은 예 few-shot에 `자:정원모;물;-1`** ↔ "자원 태그·상태 태그를 직접 작성하지 말 것" | prompts.py:976-981, 1068 ↔ 983-987 |
| X6 | 스키마 설명 "자/태/상중하 태그 포함 가능" + 상태 목록 "태: 태그는 이 목록에 있는 이름만" + 캐시 [4.5] "태: 태그로 부여하거나 제거" ↔ 런타임은 WP-B 이후 태그로 상태를 바꾸지 않음(strip만) | prompts.py:738 · gm.py:349·358 · cache.py:237-238 ↔ game.py:359-366 |
| X7 | 지시문 정의가 은퇴 대상 `!진행`에 기대어 서술됨 | prompts.py:738, 966 |
| X8 | 런타임 NPC 상태가 캐시 NPC 사전보다 우선(의도된 예외) ↔ "룰북 불변" 서열에 이 예외가 명시돼 있지 않음 | prompt.py:190 ↔ prompts.py:126 |
| X9 | 마스터 채널 중계 발화가 `[진행자]`로 `current_turn_logs`에 들어가 판단·지시 입력이 됨 — 층위별 서열에서 "플레이어 발언"인지 "GM 지시"인지 정의 없음 | game.py:109-117 |

### C-2. 사용자 작성 철학이 이미 해결하는 것 (PRODUCT_INTENT_PROMPTING_PHILOSOPHY.md)
- 고정 세계 사실은 세션마다 변하지 않아야 하고, 전달받지 않은 사실을 임의로 정하지 않는다(§2 l.34-40, 결론 l.201).
- 명시적 규칙을 암시보다 우선(§3).
- 플레이어 주권은 핵심 원칙(현 프롬프트가 모든 층위에서 최상위로 둠 — 층위 간 일치).
- 추출은 스토리 권위가 아님(현 프롬프트와 일치).
- → X5·X6·X7은 **런타임과의 불일치**라 철학상 결론이 분명하다(태그 권위 제거, 추출이 상태 권위). 다만 문구 변경 자체가 prompts.py 수정이므로 D1 승인 후 적용한다.

### C-3. 남은 모호점 (사용자 결정 필요)
1. note가 시나리오 금지사항(X1)을 **이길 수 있는가**.
2. note가 고정 세계 사실을 **추가·변경**할 수 있는가(X3) — "PC 신분 등 기정사실 추가"는 허용, "룰북 사실 변경"은?
3. note/운영자 지시와 플레이어 주권의 서열(X2).
4. 마스터 중계 발화의 지위(X9).
5. 런타임 상태 > 캐시 NPC 사전 예외를 서열에 명시할지(X8).

### C-4. 격자 선택지 (D1)

| 옵션 | 서열 | 판단 | 지시 | 묘사 | 추출 |
|---|---|---|---|---|---|
| **L1 (권장)** "주권 > 룰북 > note > GM 지시 > 기억 > 플레이어 주장" | 주권 최상위. 금지사항·고정 세계 사실은 note로도 무효화 불가. note는 룰북에 **없는** 사실(PC 신분 등)을 추가하고 톤·범위를 **좁히는** 제약으로만 최상위. 런타임 상태는 해당 항목(NPC 상태·위치)에 한해 캐시 사전보다 우선 | note "우선 적용" → "룰북 범위 안에서 우선" | prompts.py:870-871에서 "시나리오 금지사항보다 위" 삭제, "주권·룰북 범위 안에서 최상위 제약"으로 | 서열에 note 명시(①룰북 ②note ③GM 지시 ④기억 ⑤플레이어 발언) + 런타임 예외 명시 | 무변경 |
| L2 "주권 > note > 룰북 > …" | 운영자가 note로 금지사항까지 해제 가능(현 지시층위 문구 유지) | 무변경 | 무변경 | 서열을 note > 룰북으로 바꾸고 캐시 [6]의 "GM의 요청이 있더라도" 문구 완화 | 무변경 |
| L3 최소 변경 | X5·X6·X7(태그·`!진행`)만 정리, 서열 문구는 전부 현행 유지 → AUD-004/025는 "INTENTIONALLY_RETAINED(층위별 차이 = 설계)"로 종결하려면 사용자가 X1~X3을 현행대로 확정해야 함 | 무변경 | 태그·`!진행`만 | 무변경 | 무변경 |

마스터 중계 발화(X9)는 어느 옵션이든 "GM 지시(운영자 방향)"로 규정하는 것을 권장한다(현재 `[진행자]` 라벨).

**D1 결정 전에는 prompts.py·cache.py 룰북 문구·gm.py 지시층위 컨텍스트 블록 문구를 수정하지 않는다.**

---

## D. JSON/SCHEMA FIELD-USE MATRIX

| Field | Scenario generations | Runtime readers | Runtime writers | Persisted? | Prompted? | Safe action |
|---|---|---|---|---|---|---|
| `keyword_memory` | 5종 전부(example·다크판타지·무협·빈·영도) | cache.py:52(`get_home_section_ids` 연고지 매칭) · cache.py:330(캐시 편입) | 없음 | 시나리오 | 캐시(연고지 섹션) | **유지**(live). 의미 드리프트(AUD-010)는 문서로 정정 — "키워드 온디맨드 주입(gm.py:479에서 폐지됨)"이 아니라 "연고지 섹션 캐시 편입 원천". 이름 변경 없음 |
| `cached_worldview_sections` | — | turn_history.py:71-74(출처 스탬프), io.py 기본값 [] | cache.py:151 | SESSION_FIELDS | 아님 | **AUD-053 수정**: `models.py` 생성자에 초기화 추가(현재 새 세션 객체에 속성 없음 — `test_harness_selfcheck.KNOWN_MISSING_AT_CONSTRUCTION`). CLAUDE.md 검증 ②의 예외 줄 제거 |
| `media_dir` (JSON 키) | 5종 모두 `"./media"` | **없음** — 코드는 `media/{scenario_id}`를 계산(media.py:29, dialogue.py:359, cogs/media.py). 지역변수 이름만 같음 | 없음 | 시나리오 | 아님 | **죽은 데이터 필드**. JSON 수정은 D2 → 기본안: 유지 + 문서에 "런타임 미사용(폴더=시나리오 id)" 명시 |
| `job_guides` | 영도만 | **없음** | 없음 | 시나리오 | 아님 | 저작 콘텐츠·리더 0. 연결은 신규 기능 → D2: 유지(비활성 저작 데이터) 권장 |
| `image_prompts.인물/배경.prompt` | 5종(무협은 플레이스홀더) | cogs/media.py:58-63 `!이미지 생성` 프롬프트 접두 | 없음 | 시나리오 | 이미지 모델 | **D2** (AUD-006) |
| `location_images` | 영도·무협·example·빈 (다크판타지 없음) | core/media.py:54-56(구버전 폴백) · gm.py:327-347 지시층위 "사용 가능한 장소 이미지 목록" 주입 · game.py:345 | 없음 | 시나리오 | **지시층위** | 무협 `장소키워드1/2` 플레이스홀더가 실제 목록으로 지시층위에 주입됨(런타임 의미 영향: `상:장소키워드1` 유도 가능, 이미지 파일 없음) → **D2** |
| `intro_images` | 없음 | intro.py:275 | 없음 | — | 아님 | 연결부만(CLAUDE.md 미완 항목) — 유지 |
| `quest_select` | 없음(영도 퀘스트에도 미사용) | turn_preparation.py:162 | — | — | — | 유지(선택 기능) |
| `start_frames` `profile_creation` `places` `jobs` 등 | 영도만 | session_flow/start_frame/profile_*/places | — | — | 일부 | 유지 — 세대 차이(AUD-007)는 정규화하지 않음 |
| 추출 대상·임계 (`status_effects`, `build_extraction_limits`) | 전 시나리오(상태 5종 영도) | extraction.py | — | — | 추출층위 | 유지. 상태 목록 라벨의 `태:` 문구는 §C X6(D1) |
| NPC 저작 필드 | 영도 47 / 무협 저밀도(AUD-008) | cache [3], irregular_npc 등 | — | — | 캐시 | 유지(창작 금지). AUD-008은 INTENTIONALLY_RETAINED 권장 |

---

## E. HELP / DOCUMENTATION DRIFT

| 대상 | 현재 | 조치 (분류) |
|---|---|---|
| `cogs/system.py:39` 도움말 머리말 | "태그·`!증감` 값에 띄어쓰기 … `태:유이설;내력_고갈`" | `!증감` 예시로 교체 (CURRENT) |
| `cogs/system.py:76-80` | `!진행` + `자:/태:` 태그 설명, `!수정` | `!진행`·`!수정` 줄 삭제, `!출력물`은 읽기 전용으로 유지 |
| `cogs/system.py:96` | `!더빙` "`!진행` 한정" | "인트로 한정(자동 턴 미적용)" |
| `cogs/errors.py:38, 41` USAGE | `"진행"`, `"수정"` 사용법 | 삭제 (근접 매칭 후보에서도 사라짐) |
| `cogs/game.py:1234, 1261, 1337, 1375` · `cogs/media.py:717, 745` · `core/models.py:60` | `!진행`/`!수정` 언급 | 문구 정정 |
| `cogs/game.py:250` docstring "!진행 본체" | — | "공유 묘사 엔진(자동·인트로)"로 정정 |
| `prompts.py:738, 966` | `!진행` 기반 정의 | **D1 이후** |
| `CLAUDE.md` | game.py 명령 목록에 `!진행` `!수정`, 봇 로딩 기준값 "명령어 46", 검증 ② `cached_worldview_sections` 예외, "명세 작업 중에는 코드·시나리오·프롬프트를 수정하지 않는다"(WP 프로그램과 상충), 비용 절 "청구 근거는 달러다"(플레이어 청구 권위는 Settlement) | CURRENT로 정정(명령어 44 등). "진행 중인 작업 — 기능 명세" 절은 HISTORICAL 표기 제안 — **문서 권위 변경이라 G7에서 제안안으로 제출** |
| `specs/02_ai.md` | `!진행`·태그 서술 | 명세는 역추출 기록 → 본문 재작성 없이 머리에 SUPERSEDED 주석 + 현재 문서 지시 |
| `handoff/*` | 과거 번들 | HISTORICAL — 수정 없음 |

---

## F. AUD-001..AUD-063 FINALIZATION PLAN

| ID | 계획 최종 상태 | 근거/행동 |
|---|---|---|
| 001 | RESOLVED_IN_CODE | 런타임 권위 제거(WP-B, game.py:359) + 프롬프트 잔재는 D1 후 정리·정적 테스트 |
| 002 | RESOLVED_IN_CODE (D1 후) | prompts.py:976-981, 1068 |
| 003 | RESOLVED_IN_CODE (D1 후) | cache.py:236-238 |
| 004 | D1 결정에 따라 RESOLVED_IN_CODE 또는 INTENTIONALLY_RETAINED | §C |
| 005 | RESOLVED_IN_CODE | `_apply_quest_choice` 정의 0, `stage_instruction_effects` 단일 |
| 006 | D2 결정 | 무협 플레이스홀더 |
| 007 | INTENTIONALLY_RETAINED | 세대 차이 보존 |
| 008 | INTENTIONALLY_RETAINED (D2 확인) | 창작 금지 |
| 009 | 필드별(D2): media_dir·job_guides | §D |
| 010 | INTENTIONALLY_RETAINED + 문서 정정 | live 소비 |
| 011–013, 019–024 | RESOLVED_IN_CODE | WP-B/C/D/E 테스트 인용 |
| 014, 015 | INTENTIONALLY_RETAINED (보존 사실) | 검증 ③ |
| 016 | RESOLVED_IN_CODE | `!진행` 은퇴 |
| 017 | RESOLVED_IN_CODE | WP-E rerender 단일 구현 증명 테스트 |
| 018 | RESOLVED_IN_CODE | `!수정` 은퇴 |
| 025 | = 004 | D1 |
| 026, 027 | **D3 결정** 후 RESOLVED_IN_CODE | 압축 선결제 허구 표시 |
| 028–032, 034–036, 040 | RESOLVED_IN_CODE | WP-D/E/F |
| 033 | RESOLVED_IN_CODE | 표시 등식 제거 + 상한 CostLedger 전환 |
| 037–039, 042, 043, 046–048, 050, 055, 057–060, 063 | RESOLVED_IN_CODE (보존) | 선행 WP |
| 041, 044, 052, 056 | INTENTIONALLY_RETAINED (통제 요구) + 제어 동기화 제안 | 프로세스 사실 |
| 045 | RESOLVED_IN_CODE (보존) | `_process_actions` 경계 |
| 049 | RESOLVED_IN_CODE | ESTIMATE 구분 + total_cost가 더 이상 규칙 입력 아님 |
| 051 | RESOLVED_IN_CODE(런타임) + 문서 드리프트 정리 | |
| 053 | RESOLVED_IN_CODE | models.py 초기화 |
| 054 | RESOLVED_IN_CODE (WP-F 확인 필요) | 단위 API |
| 061, 062 | RESOLVED_IN_CODE | 관리 도구 strict 쓰기 전환 후. Settlement/Ink 경로는 이미 strict |

재검증은 G8에서 테스트/코드 인용으로 확정하며, 계획과 다르면 그대로 보고한다.

---

## CHANGE LIST (D1/D2/D3 비의존분 — 즉시 진행 가능)

1. `!진행` 명령 삭제, `_execute_proceed`의 명령 전용 "지시 없음 자동 생성" 분기 삭제, 주석/docstring 정정.
2. `!수정` 명령과 전용 경로 삭제, `last_turn_anchor_id` 쓰기·조회 제거(필드는 호환 유지), `!출력물` 안내 정정.
3. 도움말(system.py)·USAGE(errors.py)·`!더빙`/`!더빙테스트`/models 주석 정정.
4. 자동 GM 비용 상한을 CostLedger 세션 합계 기준으로 전환(§I U-3 기본안).
5. 턴 비용 임베드 Σ 누적과 `!사용량`(세션/전체)을 우주 분리 표시로 전환(제공자 비용: CostLedger 기록값, 플레이어 잉크: Settlement 미러 + 캐시 창 선불−환급 + 해석 청구, 무료/운영자/시스템 구분). 현재 환율 재해석·등식 제거.
6. `turn_cost_log`를 compatibility-only 표시 버퍼로 명시(주석·라벨).
7. `!지급`·`!잉크`·가입선물의 계정 쓰기를 strict 영속화로 전환(도구 유지).
8. AUD-053: `models.py` 초기화 + harness 테스트 의도적 갱신.
9. 은퇴 회귀 방지 정적 테스트(명령 부재, `!재생성` 위임, 은퇴 문구 재유입 탐지 — 프롬프트 부분은 D1 후).
10. CLAUDE.md·문서 CURRENT 정정(`verify_docs --fix`), 5종 완료 산출물.

D1 후: prompts.py(X5·X6·X7 + 선택 격자), cache.py [4.5]/[6] 문구, gm.py 상태 목록·note 블록 라벨, 정적 테스트.
D2 후: 승인된 JSON 변경만.
D3 후: 압축 선결제 처리.

## PRESERVATION LIST

TurnTransaction 생성 경계(`_process_actions`) · READY_TO_COMMIT 배리어 · 확정 묘사 전문 추출 · CommitCoordinator 단일 커밋 · CommitJournal · FAILED_SYSTEM 청구 0 · CostLedger append-only(모든 provider 관측 유지) · Settlement · InkTransaction/LifecycleInkTransaction exactly-once · WP-E 이력 SELECT/정리 부채/`!재생성`/`!되감기` · message_lifecycle 5분류 · 압축 출처 가드 · cache_lifecycle 단일 finalizer·선불/환급·운영자 부담 · interpretation strict CostEvent→청구 · `상:/중:/하:` 이미지 태그 · `자:/태:` 방어적 strip · 인트로 경로 · 운영자/관리 도구 전부 · 시나리오 JSON(승인 전 무변경) · prompts.py(D1 전 무변경).

## DECLARED FILE SCOPE (비의존분)

`cogs/game.py` · `cogs/system.py` · `cogs/errors.py` · `cogs/media.py`(문구) · `cogs/gm.py`(비용 상한·주석) · `core/session_flow.py`(상한 기준) · `core/cost.py`(임베드 표시, 사용량 집계 헬퍼) · `core/accounts.py`(관리 strict 헬퍼) · `core/terms.py`(가입선물 strict) · `core/models.py` · `core/io.py`(필요 시 필드) · `core/__init__.py`(재수출) · `CLAUDE.md` · `specs/02_ai.md`(SUPERSEDED 머리 주석) · `tests/**`(의도적 갱신·신규) · `handoff/WP_G_*`.
D1 후 추가: `prompts.py` · `core/cache.py` · `cogs/gm.py`(컨텍스트 문구) · `core/prompt.py`.
D2 후 추가: 승인된 `scenarios/*.json`.

## SHARED-FUNCTION CALLER MAP

| 함수 | 호출자 | 변경 후 |
|---|---|---|
| `GameCog._execute_proceed` | gm.py:3742 `_dispatch_proceed` · session.py:534 `play_intro` · game.py:244 `proceed_turn` · 테스트 | `proceed_turn` 제거 → 2곳 |
| `GMCog._call_gm_logic` | 자동 루프 다수 · game.py:385(`!진행` 무지시 분기) | game.py 호출 제거 |
| `GMCog.rerender_latest` | game.py:1312 `!재생성` · gm.py:645 뷰 | 무변경 |
| `GMCog._run_extraction` | `_prepare_extraction` · `ExtractionRetryView.retry` | 무변경 |
| `core.accrue` | 18곳(§B) | 무변경(미러) |
| `session.total_cost` 규칙 읽기 | gm.py 6곳(상한) | CostLedger 헬퍼로 교체 |
| `build_turn_cost_embed` | gm.py:2039 `_send_turn_cost_report` · game.py:919(인트로) | 표시만 변경 |
| `accounts.add_ink/deduct_ink/set_balance` | system.py 400·403·579 · terms.py 114 | strict 헬퍼로 교체(레거시 함수는 테스트 호환 위해 유지 여부 검토) |
| `cost_ledger.sum_cost_events` | (현재 프로덕션 호출 없음 확인 필요) | 상한/사용량이 소비 |

## USER-DECISION GATES

- **D1 — 프롬프트 권위 격자** (§C-4: L1 권장 / L2 / L3). 결정 전 프롬프트 의미 변경 없음.
- **D2 — 저작 콘텐츠**
  - D2-a 무협 `image_prompts.인물/배경.prompt` 플레이스홀더: (a) 사용자 제공 문구로 교체 (b) 현행 유지+INTENTIONALLY_RETAINED (c) 키 제거(해당 형식의 `!이미지 생성` 불가). 런타임 의미는 정상, 품질 문제.
  - D2-b 무협 `location_images.장소키워드1/2`: **런타임 영향 있음**(지시층위 목록에 주입) → (a) 키 제거(무협은 장소 이미지 목록 없음 = 다크판타지와 동일) **권장** (b) 사용자 제공 장소로 교체 (c) 유지.
  - D2-c `media_dir` JSON 키(리더 0): (a) 유지+문서화 **권장** (b) 5종에서 제거.
  - D2-d `job_guides`(영도, 리더 0): (a) 유지(비활성 저작 데이터) **권장** (b) 제거. 연결은 신규 기능이라 범위 밖.
  - D2-e AUD-008 무협 NPC 밀도: 유지(창작 금지) 확인.
- **D3 — 압축 선결제(AUD-026/027)**: 현재 계정 효과 없는 "선결제/환급 N잉크" 표시가 플레이어 디스플레이·로그에 남음.
  - (a) **권장**: 압축 비용은 운영자 부담(현 사실)으로 확정하고 허구 선결제 누적·정산·표시를 은퇴(필드는 호환 유지). 예상액에 압축 20% 가산도 제거.
  - (b) 압축 비용을 실제 청구 — Settlement/InkTransaction 정책 변경이라 WP-G 중단 조건(§17) → 비권장.
- **U-2 — F-NEW-2 압축 주기**: `mark_compressed`가 최초 생성 분기에서만 호출되어 두 번째부터 주기 판정이 매 턴 참이 될 수 있음(비용 증가). (a) **권장**: 적용된 모든 압축에서 `mark_compressed`(로우 플랜 모델 전환 횟수도 정상화) (b) 현행 유지(의도라면 근거 기록).
- **U-3 — 자동 GM 비용 상한 기준 우주**: (a) **권장(기본안으로 진행)**: CostLedger 세션 범위 전체 provider 비용(무료·운영자·캐시 포함) − 활성화 시점 스냅샷. 구세션은 첫 점검 때 재기준. (b) 플레이어 청구 후보(`billing_hint=PLAYER_CANDIDATE`)만. 반대 지시가 없으면 (a)로 진행합니다.

## BLOCKERS / MATERIAL DISCREPANCIES

1. **CLAUDE.md "명세 작업 중에는 코드·시나리오·프롬프트를 수정하지 않는다"** — WP-A~G 실행과 상충하는 낡은 통제 문구. 사용자 WP 지시가 우선한다고 보고 진행하되, G7에서 HISTORICAL 전환을 제안합니다(차단 아님).
2. 인계서 §11의 "`media_dir` is a disconnected candidate" — 확인 결과 JSON 키는 리더 0이 맞으나, 같은 이름의 지역변수가 30곳 가까이 있어 grep만으로는 live로 오판됨. 결론: JSON 키 = 죽은 데이터(D2-c).
3. 인계서 §11 "AUD-053 is likely resolved" — **아님**. io.py 기본값은 있으나 `models.py` 생성자에 없음(harness가 결함을 고정 중). 수정 대상.
4. 더빙은 자동 턴에 원래 미적용(`not cost_log_prefix`) — 세션 생성 플로우가 TTS를 켜도 자동 턴에서는 읽히지 않음. `!진행` 은퇴로 적용 범위가 인트로뿐이 됨. 신규 기능 확장은 범위 밖이므로 문구만 정정(필요하면 별도 지시 부탁드립니다).
5. WP-F에서 넘어온 판단 요청 F-NEW-2(U-2), 압축 선결제 정책(D3)은 AUD-026/027 최종 분류에 필수라 게이트로 올립니다.
6. 관리 도구의 tolerant 계정 쓰기 — 읽기 실패 시 빈 계정으로 대체한 뒤 저장하므로 **잔액 소실 가능**. AUD-061의 레거시 잔재로 보고 strict 전환(도구 유지)합니다.
