# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**INDAIM** — Gemini API + discord.py 기반의 한국어 TRPG 보조 GM 디스코드 봇.

AI가 4개 층위로 나뉘어 판단·지시·묘사·추출을 분담한다. 플레이어는 서버 GM 스페이스에서 버튼으로 세션을 열고, 캐릭터를 만들고, 턴을 진행한다. **명령어는 GM의 수동 개입·복구 수단이며 일반 플레이에는 쓰지 않는다.**

현재 버전: **v7.33.0** · `SCHEMA_VERSION` 4 · 총 운영 코드 ~52,600줄(main·prompts·cogs·core) · 테스트 ~27,270줄

리팩터링: **7-WP 프로그램(WP-A~WP-G)** — 턴 트랜잭션·커밋·재무·이력·파생 시스템을 단일 권위로 재구성했다. WP-A~G 전부 독립 게이트 VERIFIED — **프로그램 완료**(WP-G 최종 후보 `f0fd69e`, 증거 `handoff/WP_G_COMPLETION_BUNDLE.md`). 다음 WP는 없다. 버전은 이 비호환 전환 전체를 **단일 MAJOR v6.0.0**으로 채택했다(2026-10-06, 이력 보정은 DEVLOG '버전 기록').

동적 세계 구현 프로그램(통합 로드맵 v0.3, **H01~H07**, `de367c7` 기준)이 진행 중이다. H 번호는 개발 패키지명이다.
H07 이후 패키지(v7.6.0~v7.9.1): SVG 화자 카드·영도 카드 저작 → 7.6.0, `!세션이력` → 7.7.0, 묘사↔정본 정합·만료 중 선언 보관·화자 표기 → 7.8.0, 구조 로그 → 7.9.0, 정본 시각·상태이상 판정 보정·예상 비용·판정 기록 중복 → 7.9.1, 디스플레이 상태판 SVG 이미지 → 7.10.0, `!배포` 의존성 자동 설치 → 7.10.1, 전수조사 결함 수정·자동 턴 더빙 → 7.11.0, 판정 2d50 통합 → 7.12.0, 마스터 인물화 UI 표현 승격 → 7.13.0, 역사칭·발견 선언 → 7.14.0, 캐릭터 생성 개선 → 7.15.0, 세션 종료·제거 통합 관리 → 7.16.0, 아이템 시스템·초기 지식 → 7.17.0, 소규모 안정화 → 7.17.1, 소속 접근 capability → 7.17.2, 접근 능력 출처 분리 → 7.17.3, P1 묘사 전 세계 진행·후속 묘사 은퇴·비용 정합 → 7.18.0, P2 음성·마스터 성별·대화형 UI·SVG 로딩 → 7.19.0, `!명령어` 갱신 → 7.19.1, 턴 서사·지시층위 정합·같은 턴 식별 → 7.20.0, P3 에이전트 판독용 로그 → 7.21.0, 미전달 서사 신호 수명주기 → 7.21.1, 마스터 원화 반영 → 7.21.2, 캐릭터 생성 답변 정리 → 7.21.3, 첫 생성 유지 시간 게임 채널 답변 → 7.21.4, 마스터 대사 개정 → 7.21.5, DYNAMIC 이동·탐험(출구 지목·목적지 없는 이동) → 7.22.0, DYNAMIC 개연성 시뮬레이션 해체·조건부 선언 검증 → 7.23.0, 판단 프롬프트 계약 충돌 해소 → 7.24.0, DYNAMIC 정본·프롬프트 권위 정합화·프롬프트 문자 절단 제거 → 7.25.0, 영도 시작 상황 재저작·시작별 정본(`world_start.items`·시작 장면 인물 사건) → 7.26.0, GM 스페이스·시작 상황 선택 SVG 카드 → 7.27.0, 버튼 유효시간·재시작 승계 → 7.28.0, 닫힌 용기·물건 연산 거부 복구·추출 실패 분류 → 7.29.0, 마스터 턴 전수보고·비용 예측 개편 → 7.30.0, 영도 감염자 카드(이름 지정 고정 카드) → 7.31.0, `!세션관리` 일괄 관리 → 7.32.0, 예외 케이스 전수조사 결함 수정 → 7.33.0(DEVLOG '버전 기록').

| WP | 범위 | 상태 |
|---|---|---|
| H01 | 정의 컴파일러·정체성·스냅샷 기반 | **완료** — 완료 시점 production 미배선(H06에서 세션 바인딩), `handoff/H01_COMPLETION_BUNDLE.md` |
| H02 | canonical 상태·영속·이력 + WorldMutationBatch·ProjectedWorld·정본 변이 기반 | **완료** — 빈 변이 lane 배선, `handoff/H02_COMPLETION_BUNDLE.md` |
| H03 | 정량 시간·예약 + 공간·이동·접근·공간인지 | **완료** — 서비스·typed 연산 등록, 완료 시점 플레이어 경로 미배선(H05에서 DYNAMIC 턴 배선), `handoff/H03_COMPLETION_BUNDLE.md` |
| H04 | Actor·정보·인지·NPC 투영 + 사건·DependencyIndex·WorldProgression | **완료**(VERIFIED `1906f1b`, R2·R3 보정 포함) — 순수 엔진·typed 연산, `handoff/H04_COMPLETION_BUNDLE.md` + `handoff/H04_R2_CORRECTION_BUNDLE.md` |
| H05 | 구조화 행동 의도·턴 계획 + 프롬프트·추출·후속묘사·미디어 | **완료 제출(검수 대기, R2·R3 보정 포함)** — 자동 턴 배선(DYNAMIC 세션만, 현 운영 세션은 LEGACY), `handoff/H05_COMPLETION_BUNDLE.md` |
| H06 | 초기화·캐시·관리자·복구·재서술·세션 종료 | **완료 제출(검수 대기, R2 보정 포함)** — 단일 PC DYNAMIC 생애주기 배선, `handoff/H06_COMPLETION_BUNDLE.md` + `handoff/H06_R2_CORRECTION_NOTE.md` |
| H07 | 영도 실제 이행·legacy 권위 은퇴·production cutover·최종 게이트 | **구현 완료 — 독립 검수 대기(VERIFIED 아님)** — NPC 유효 위치·이동 지식 정책·표시명·명시 결단·레거시 권위 은퇴·프롬프트 정합, `handoff/H07_FINAL_IMPLEMENTATION_REPORT.md`. 최종 게이트 보정 `b7a4d82`(CLEAR 순간이동 방지·결단 후보 관련성 gate) — main 반영 |

H01 완료 번들은 통합 전 번호(H01~H11)를 쓴다 — 구 H02+H03→H02, H04+H05→H03, H06+H07→H04, H08+H09→H05, H10→H06, H11→H07로 읽는다.

---

## 실행 및 환경

```bash
pip install -r requirements.txt
python main.py
```

`.env` 필수 변수:
- `DISCORD_TOKEN` — 디스코드 봇 토큰
- `GEMINI_API_KEY` — Gemini API 키
- `TRPG_INTRO_TEXT` — `!소개`가 쓰는 공통 인트로 (소개 화면은 `core/intro.py`가 따로 관리)

### 배포

운영 서버(Oracle Cloud, Ubuntu x86_64, systemd `trpg-bot.service`)에서:

```
!배포 재시작      # git pull + (requirements.txt 변경 시 실행 중인 venv의 pip install, 실패하면 중단) + 재시작
!재시작           # 재시작만 (오너 전용)
!캐시 재발급      # 세션 하나의 캐시 교체(마스터 채널) — 아래 '캐시 재발급이 필요한 경우' 참고
!리로드 [모듈]    # cogs/ 하위만 핫스왑 가능
```

`core/`와 `main.py`는 핫스왑이 불가하다.

서버 경로(`systemctl cat trpg-bot`): 저장소 `/home/ubuntu/trpgbot`, 파이썬 `/home/ubuntu/trpgbot/venv/bin/python`(서비스 이름은 `trpg-bot`).

**캐시 재발급이 필요한 경우** — `!캐시 재발급`은 세션 닫기→열기처럼 그 세션의 캐시를 새로 만든다(차이: 결제한 유지 시간 시작점은 그대로, 퀘스트 캐시 비움, LEGACY는 시나리오 파일 재로드).

| 변경 | 기존 세션 반영 |
|---|---|
| `prompts.py`(시스템 지시문은 캐시에 포함) | **열려 있는** 세션만 재발급 또는 닫기→열기 필요. 닫힘·만료 세션은 다음 열기에서 자동 반영 |
| LEGACY 시나리오 JSON | 재시작 시 파일 재로드. 열린 캐시의 룰북 텍스트만 옛것 → 열린 세션만 재발급 |
| DYNAMIC(영도 단일 PC) 시나리오 JSON | 바인딩 스냅샷만 쓴다 — 재발급·재오픈으로도 반영되지 않는다. **새 세션에만** 반영 |
| 코드(`core/`·`cogs/`) | 재시작(또는 cogs 리로드)만으로 반영. 재발급 불필요 |

---

## 아키텍처 — 4층위 파이프라인

층위를 나눈 이유는 **비용과 정확도의 분리**다. 판단은 싸게, 묘사는 좋게, 수치는 코드가.

```
플레이어 선언
   ↓
[개연성 시뮬레이션]  세계관 대비 이 행동이 무엇에 닿는지 미리 훑는다(캐시 읽기 1회)
   │                 LEGACY만: 캐시가 유효하면 판단 전에 모든 유형에서 돈다. 지시층위의 판단 재료.
   │                 v7.23 DYNAMIC은 돌지 않는다 — 판단이 발견 제안·결과 선취를 표시할 때만 축소 선언 검증 1회.
   │                 서사설계자와는 별개다.
   ↓
[판단층위]  유형 결정 (ASK / NARRATE / ROLL / PROCEED)
   │        캐시를 읽지 않는다. 맥락을 사용자 프롬프트로 직접 공급.
   │        ASK·ROLL은 여기서 끝난다 (지시·묘사 캐시 0회 → 가장 저렴. ROLL은 굴림 뒤 같은 턴에 지시층위부터 이어 감 — 시뮬레이션·판단 재진입 없음)
   │        ROLL 판정 = 2d50 < T(=52+능력+숙련+행동+상태+난이도) — 수치·성패는 코드(`core/judgment.py`), 판단층위는 계약값만
   ↓
[지시층위]  묘사 지시문 + 퀘스트 선택 (NARRATE·PROCEED만)
   │        캐시를 읽는다. 세계관·금지사항·정보 접근을 반영.
   ↓
[묘사층위]  실제 출력. 스트리밍으로 게임 채널에 송출.
   ↓
[추출층위]  수치·상태 추출 → 코드가 임계값과 대조해 적용
            사고 예산 제한 (기계적 판독이므로)
```

### 설계 원칙

**① 판단은 수치로, 임계는 코드로**
모델에게 "위험한가?"를 묻지 않는다. 0~100 점수를 받아 코드가 임계값과 비교한다.

**② 온디맨드 주입**
장소·퀘스트·NPC 정보는 캐시에 굽지 않고 턴마다 필요한 것만 주입한다. 캐시는 시나리오 룰북(고정분)만 담는다.

**③ 시나리오 JSON이 정의, 코드는 기능만**
스탯 체계·프로필 알고리즘·장소 그래프·퀘스트 트리가 모두 데이터다.

**④ 모델에게 유효 목록을 준다**
코드가 걸러낼 수 있어도 목록 없이는 매번 헛일을 한다. 상태이상·NPC 이름은 시나리오에서 뽑아 추출층위에 주입한다(`build_extraction_limits`).

---

## 파일 맵

### 최상위

| 파일 | 역할 |
|---|---|
| `main.py` | `TRPGBot`, `active_sessions`, cog 자동 로드, 세션 복구, 초대 링크 생성, GM 홈 갱신 |
| `prompts.py` (1,836줄) | 시스템 지시문 + 응답 스키마 12종. 권위 서술은 WP-G D1(아래 '프롬프트 권위') |
| `scenarios/*.json` | 시나리오 데이터. 퀘스트는 `{이름}.quests.json`으로 분리 |
| `media/{시나리오명}/` | 인물 이미지·BGM. **폴더명은 시나리오 id와 같아야 한다** |
| `assets/master_dialogues_v1.json` | 마스터(진행자) 사전 저작 대사 데이터(키 126·변주 252, v7.21.5 진중한 태도·플레이 순서 기준 개정) — 문구 교정은 이 파일만 고친다. 마스터 원화는 `assets/master/`(v7.21.2) |
| `tools/verify_docs.py` | 문서 수치 정합 검증 |
| `tools/scenario_check.py` | 시나리오 정의 검사·컴파일·이행 보고(H01, 원본 읽기 전용) |
| `tools/log_view.py` | 구조 로그 열람(세션 폴더·`!세션이력` zip) — 요약·시간순·호출 원문 복원·이벤트·도메인·채팅 |
| `handoff/` | WP별 완료 번들·스캔·매트릭스(과거 번들은 HISTORICAL 증거, 수정 금지) |
| `tests/` | 특성화·결함·정책 테스트 하네스(WP-00~G) |

`prompts.py`와 `scenarios/*.json`은 **임의 수정 금지**. 수정안을 제시하고 승인받은 뒤 적용한다.

### cogs/ — 명령어 계층 (9개)

| 파일 | 줄 | 명령어 |
|---|---|---|
| `gm.py` | 6,091 | `!자동` 그룹(시작·중단·상태·개입·재계획·서사·원장·**퀘스트**·턴제한·비용제한), `!되감기` |
| `game.py` | 1,566 | `!재생성` `!출력물` `!주사위` `!기억압축` `!노트` `!캐시노트` `!더빙테스트` (WP-G: 수동 `!진행`·`!수정` 은퇴) |
| `character.py` | 1,253 | `!참가` `!설정` `!증감` `!외형` `!프로필` `!엔피씨` `!능력치` `!설정생성` `!캐릭터가져오기` |
| `system.py` | 1,004 | `!명령어` `!배포` `!재시작` `!지급` **`!잉크`** **`!사용량`** `!스페이스` `!캐시` `!리로드` `!세션종료` `!채널정리` `!tts생성` `!세션이력` |
| `media.py` | 756 | `!이미지` `!브금` `!플리` `!볼륨` `!채팅` `!더빙` |
| `session.py` | 749 | `!새세션` `!시작` `!소개` + `JoinView` + `on_voice_state_update` |
| `errors.py` | 152 | 명령어 오류 핸들러 |
| `permissions.py` | 98 | `!권한부여` `!권한회수` `!권한목록` |
| `presence.py` | 122 | 상태 메시지 순환 + **캐시 만료 감지**(15초 주기) |

### core/ — 101개 서브모듈

`core/__init__.py`가 전 심볼을 re-export하므로 외부에서는 `core.XYZ`로 접근한다.

#### 턴·커밋·재무 권위 (WP-02~G)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `turn_transaction.py` | 348 | `TurnTransaction` — 자동 논리 턴 시도의 런타임 정체성 |
| `turn_preparation.py` | 1,535 | `TurnPreparation` — 결과 스테이징, 작업 레지스트리, `READY_TO_COMMIT` 배리어, 추출 계획 |
| `commit_coordinator.py` | 1,230 | `CommitCoordinator` — 유일한 정본 커밋·복구, 재시도 breadcrumb·구 컨텍스트 은퇴 |
| `commit_journal.py` | 548 | `CommitJournal` — 커밋 단계 append-only 내구 기록·복구 분류 |
| `settlement.py` | 562 | `TurnSettlement` — 정상 턴 플레이어 부담·보고의 단일 출처 |
| `ink_transactions.py` | 602 | `InkTransaction`/`LifecycleInkTransaction` — 정확히 한 번 계정 효과 |
| `cost_ledger.py` | 961 | `CostLedger` — provider 사실 append-only(`data/cost_ledger.jsonl`), strict 조회 |
| `turn_history.py` | 1,198 | 커밋된 턴 이력 — 되감기·재생성 선택, 출력 정리 부채 |
| `cache_lifecycle.py` | 906 | 캐시 생애주기 단일 finalizer — 창 선불·환급·보관비 |
| `interpretation_billing.py` | 258 | 유지 시간 해석 비용의 strict 사실 → 청구 |
| `message_lifecycle.py` | 323 | 플레이어 채널 메시지 5분류 소유·정리 |
| `narration_result.py` | 82 | 묘사 생성/전달 결과 DTO |

#### 기반

| 모듈 | 줄 | 내용 |
|---|---|---|
| `constants.py` | 143 | 모델 ID, `EXCHANGE_RATE`, `PRICING_1M`, `TTS_VOICES`, `__version__` |
| `models.py` | 319 | `TRPGSession` — 단일 세션의 모든 상태 |
| `io.py` | 535 | `SCHEMA_VERSION`, `SESSION_FIELDS`(107), `migrate_session_data`, 직렬화·로그 |
| `cache.py` | 604 | 룰북 캐시 빌드, `remaining_ttl`, `is_cache_expired`, `is_session_open`, 세션 복구 |
| `resilience.py` | 207 | `call_with_retry` — 재시도·타임아웃. 시도 실패는 구조 로그 ERROR, provider 호출 맥락(층위·작업·시도·세션)을 설정 |
| `logs.py` | 589 | **구조 로그 단일 owner** — 세션 `logs/`(`calls`·`blobs`·`events`·`domain`·`chat` JSONL)·전역 `logs/bot.jsonl`(회전 10MB×5, 모든 print 포착 — 세션 ID 출력·세션 이벤트는 전역에 위치·길이만(P3 D1), 원문은 세션 events). provider 호출은 `install_client_proxy`가 `genai_client.models`를 감싸 1건씩 기록(동작 불변). 요청·응답은 줄 조각 해시 델타(반복 줄은 참조만, 테스트 세션 요청 원문 대비 27%). 모든 세션 줄 `b`(부팅)·`seq`(단조), 호출 `op_id` = `cost.event.operation_id`(P3). 도메인 kind는 등록부 `DOMAIN_KINDS`(필드 사전 `docs/LOG_SCHEMA.md` — `!세션이력` zip에 `_index/LOG_README.md`로 동봉): `session.meta`·`tx.*`·`turn.summary`·`turn.delta`(되감기 레코드 pre→post 상태 차이)·`commit`(청구·세계 typed op)·`cost.*`·`ink.*`·`display`·`history.*`(+`history.selection`)·`card.*`. 열람은 `tools/log_view.py` |
| `turn_log.py` | 120 | **`turn.summary`**(P3) — 트랜잭션 대기·종결마다 1줄: 판단·세계 계획·관찰·준비·세계 연산 개수·비용 이벤트·단계 ms·호출 참조(메모리 값만 모음, 실패 흡수) |

#### 프롬프트·AI

| 모듈 | 줄 | 내용 |
|---|---|---|
| `prompt.py` | 346 | `PromptBuilder` — 체이닝으로 블록 조립 |
| `extraction.py` | 664 | 추출 스키마·파싱·적용, 동행 갱신, **소지품 반영**, `build_extraction_limits` |
| `master.py` | 86 | **마스터 대사 선택**(v7.13) — `line`/`say`/`lead`, 실패(키·슬롯·파일) 시 기존 문구 폴백, 슬롯 이스케이프·조사 보정, API 호출 없음. 플레이어 대상 고정 안내만 대체(GM 홈·운영자 메시지·본편·결과 블록 제외) |
| `voice_profile.py` | 184 | **음성 프로필**(v7.19 P2) — 세션 `master_gender`(남·여 = 같은 진행자) 목소리, 본문 내레이션·직접 발화 공용, NPC 목소리 결정적 배정 + 전역 기록 `data/voice_assignments.json`(시나리오+NPC 키), 문단별 화자·절제 스타일 지시. 새 LLM 호출 없음 |
| `master_art.py` | 103 | **마스터 이미지 슬롯**(v7.19) — `assets/master/manifest.json` 6슬롯(남·여 × 기본·경고·긍정 — v7.21.2 원화 반영: 기본=손 내밈·경고=손바닥 제지·긍정=엄지척, 16:9 WEBP, 필기 포즈 2장은 미배정 보관). null = 미제작(공용 로딩 이미지), 파일 이상 = 오류 이미지 + 로그. 공용 `assets/indaim_loading_neutral.webp`·`indaim_error_neutral.webp` |
| `conversational.py` | 67 | **대화형 안내 content화**(v7.19) — Embed 빌더 결과를 송출 경계에서 content(제목·본문·진행도)와 정형 데이터 카드로 나눈다 |
| `dialogue.py` | 516 | `@대사:이름\|본문` 파싱, 인물 이미지 송출, 스트리밍, `WaitingStatus` |

#### 비용·결제

| 모듈 | 줄 | 내용 |
|---|---|---|
| `cost.py` | 689 | 토큰 단가 계산, 호환 미러 `accrue`, 비용 임베드, CostLedger 조회(`provider_cost_summary`·`player_ink_summary`·`auto_cost_cap_reached`) |
| `estimate.py` | 768 | 턴·세션오픈·TTS 예상, `approx_cache_tokens`. v7.30: 층위 입력 = 확정(직전) + 유력·가변 p10~p90(`census_range`), 출력 p10~p90, 층위별 보정, 원장 시나리오·모드 그룹, 조건부 호출은 관측 발생률 ≥ 20%만 상한(`cond_rate`), 예측·실제 대조 `data/cost_estimate_accuracy.jsonl` → 되먹임(`feedback_factor`) |
| `prompt_census.py` | 147 | **입력 구성 계측**(v7.30) — 실제 보낸 프롬프트를 머리글 블록으로 나눠 확정/유력/가변·인물 키워드·토큰, 세션 `cost_stats["_census"]`(층위별 30건) |
| `master_report.py` | 468 | **마스터 턴 전수보고**(v7.30, 매 턴) — 비용(주황)·층위활동(파랑)·세계 델타(초록, 이력 pre→post·장면 밖 `[원격]`)·한도 분리·비밀 요약 + 원문 버튼(DynamicItem `mrep:sec:`) |
| `ink.py` | 85 | 잉크 환산 (1잉크 = 10원) |
| `accounts.py` | 576 | 계정 등록·약관·잔액. strict 금전 프리미티브(Settlement CHARGE·Lifecycle 조정 마커) + 운영자 `*_strict`(WP-G). 레거시 tolerant writer는 테스트 시드 전용 |
| `terms.py` | 202 | 약관 동의 DM |
| `stats.py` | 196 | 누적 플레이 기록 |

#### 세션 생성

| 모듈 | 줄 | 내용 |
|---|---|---|
| `session_flow.py` | 708 | **17단계 통합 플로우** — 단일 메시지 임베드로 갱신. v7.21.4: 첫 생성 유지 시간은 게임 채널에서 답한다(`awaiting_open_here`, 재오픈은 디스플레이) |
| `intro.py` | 276 | **소개 — 인지 수준 3단계, 항목별 바리에이션** |
| `creation.py` | 176 | 단계 상태 기계 |
| `session_open.py` | 134 | 유지 시간 입력 해석 |
| `gm_space.py` | 425 | GM 홈·명예의 전당·월드보드 |

#### 프로필

| 모듈 | 줄 | 내용 |
|---|---|---|
| `profile_gen.py` | 743 | 생성 모듈 10종, 능력치 등급·랜덤 배분, `swap_to_top`, `starting_items`, v7.15 단계 저작 검사(`creation_guide`·`master_keys`) |
| `profile_runner.py` | 382 | 시나리오 알고리즘 해독·실행, v7.15 `edit_field`(필드+의존 필드만 비움)·자료형 검사 |
| `profile_creation_ui.py` | 1,531 | 풀오토 생성 UI — 단일 메시지 임베드. v7.15: 채팅 답변·장문 직접/질문형 가이드·초안 검토·최종 검토·세션 영속·재시작 복원(아래 '캐릭터 생성') |
| `profile_ai.py` | 214 | AI 검증·병합 (무료). v7.15: 검증 대안 제시(`suggestion`) 제거, 병합 결과 무절단(`too_long`) |
| `profiles.py` · `profile_ui.py` | 649 | 사전 저장 프로필과 DM 관리 |

#### 세계

| 모듈 | 줄 | 내용 |
|---|---|---|
| `places.py` | 409 | **장소 계층 그래프** — 이동 개연성, 가시성, 이미지 상속 |
| `quest.py` | 815 | 퀘스트 트리, 이면정보, 메인 해금, `apply_grants`, 인피니티 플랜 |
| `quest_filter.py` | 326 | **필터 매칭 — 통과한 값이 곧 슬롯** |
| `start_frame.py` | 239 | 시작 상황 틀 |
| `timeline.py` | 185 | 작중 시간 정량화 |
| `growth.py` | 232 | 능력치 성장·행운 — 성장 재판정 d20 고정·상한 `growth.stat_max` > `ability_stat_max` > 20, 행운 무저작 기본값 10(프로필에 쓰지 않음) |
| `judgment.py` | 118 | **2d50 판정 수식**(v7.12) — `R=1d50+1d50 < T=52+A+S+M+H+D`(동점 실패), A=2×(원스탯−10), 난이도 6단계, 대결 결과, 판단 계약 검증(보정 없음). S는 현재 0(스킬 보너스 추후 통합) |
| `irregular_npc.py` | 244 | 비정규 NPC 이미지·목소리 |
| `koreantext.py` | 166 | 슬롯 치환·조사 보정, 국어 로마자 표기(`romanize`, 음절 전자) |
| `items.py` | 827 | **아이템**(v7.17, DYNAMIC 전용) — `resources` 정규 해석 `종류[단위]〈상태〉`(수량 분명한 옛 문구만)·특수 객체 `item_instances`·현장 물건 `place_items`·추출 `item_ops`(O·H·L·D·C 키) 순차 가상 적용(하나라도 어긋나면 재추출)·운반 등급·제작/분해·READ→정보 인지·접근 확인 실지급 PAY·직전 턴 사용/지급 사실 `item_facts`(사건 ref `OBSERVER_USED_ITEM`·`OBSERVER_PAID_ITEM`)·genesis 초기 재고. v7.29 닫힌 용기(`closed`·`inside` — 바닥 용기 OPEN은 여는 것만, 열기 전 내용물 꺼내기·사용 거부). 물건 연산 거부 시 같은 턴 사유 덧붙인 재추출 1회 → 또 거부되면 물건 연산만 빼고 확정(`cogs/gm.py` `_observe_with_item_recovery`) |
| `initial_knowledge.py` | 544 | **페르소나 초기 지식**(v7.17) — 정보 `initial_knowledge` opt-in·코드 접근 게이트(승인 프로필 equals/one_of)·PROFILE(캐릭터 승인 직후)·START(시작 틀 직후·genesis 전) 각 최대 1회 비캐시 운영자 부담 호출(`OP_INITIAL_KNOWLEDGE`)·summary 코드 렌더·nonce 영수증·UNCERTAIN은 자동 재전송·인지 없음·DELIVERED만 genesis INITIALIZATION 인지·재열람 `disp:knowledge`(0호출) |

#### 세계 정의 (H01 — H06부터 세션 바인딩)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `world_ids.py` | 157 | 정규 직렬화·순서 지문·안정 digest, 저작 ActorId/InfoId/구간 id 파생 |
| `definition_compiler.py` | 2,296 | capability 감지(`scenario_schema`)·legacy 통과·저작 sugar 컴파일·검증 진단·revision·이행 보고. **compiler v2(H03)**: 달력(`quantitative_time`)·구간 속성·이동 방식·접근 정의·공간 지식 seed(`structured_places`) 검증. **compiler v3(H04)**: 행위자 `profile_base`·명시 `information_definitions`·인라인 `info_ref`·`information_series`·`event_templates`(타입 조건·국면·케이스·typed step·의존 키)·접근 override 안정 variant id. 인식 capability 8종 전부 검증 대상 |
| `definition_snapshot.py` | 235 | `sessions/{id}/definition_snapshot.json` 원자적 쓰기·해시 검증·덮어쓰기 거부 |

production 호출자는 `world_lifecycle`(H06) — DYNAMIC 후보 판정·시작 선택 뒤 컴파일과 스냅샷 바인딩(`definition_revision` 단일 writer). 바인딩 세션은 `scenario_data`를 스냅샷 원문으로 쓴다. LEGACY 세션은 여전히 raw `scenario_data`.
H07에서 compiler v3에 선택 키(`routine`·`display_name`/`unknown_name`·`reveals`·`resolution_options`)가 추가됐다 — 미저작 시나리오 4종은 revision 불변, 영도만 `d3a01114…`로 바뀌었다.

#### 동적 세계 정본·변이 (H02)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `world_state.py` | 132 | H02 세션 필드의 **단일 출처** — 가역 게임 정본 11필드·비가역 정체성 2필드(`definition_revision`·`narrative_seed`), `world_version` 인과 정체성 |
| `world_mutation.py` | 724 | `WorldMutationBatch`(typed op·LocalRef·동결·digest — 내부는 정규 사본이라 외부·반환 객체 변경과 분리)·`ProjectedWorld`·**인과 계층 투영**(`causal_parent_op_ids`)·병합 의미 5종(H03: typed `DELETE` — 같은 계층 멱등, 혼용·없는 키·인과 없는 교차 계층은 충돌)·`WORLD_PROJECTION_CONFLICT`. production 레지스트리 = H03 시간·공간 12종 + H04 행위자·정보·인지·관계·사건 15종 + H07 `ACTOR_PLACEMENT_CLEAR` = typed 연산 28종(임의 경로 setter 없음) |
| `canonical_mutation.py` | 291 | INITIALIZATION·ADMIN_EDIT 공통 원자 적용(검증~저장 전체가 커밋 직렬화 락 안 — 직렬화 락 → io 락)·작은 내구 저널·base quiescence 증명(H03) — production 호출자 = H06 genesis(`world_lifecycle`, INITIALIZATION). ADMIN_EDIT(`admin_world_edit`)는 호출자 0 |

모든 커밋된 PLAYER_TURN은 빈 묶음이어도 `world_version`을 전진시킨다(CommitPlan v2가 PREPARED 전에 대상 version을 동결). PLAYER_TURN 정본 owner는 여전히 `CommitCoordinator` 하나다.

#### 동적 세계 시간·공간 (H03 — 서비스·typed 연산, H05부터 DYNAMIC 턴 배선)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `world_clock.py` | 265 | 달력(GREGORIAN·FIXED_CUSTOM)·시간대 `[start,end)`(자정 넘김)·canonical 분 시계 `{"minute_index","start_minute_index"}`(미초기화 `{}`, 값 ≥ 1440 = 달력 일련번호 ≥ 1). 후퇴 거부·0분 허용 |
| `world_schedule.py` | 304 | pending 전용 예약 큐(순서 = due·order_key·id, 빈 큐는 `{}`뿐)·H04 v2 확장(H03 7키 항목 그대로 읽음, 신규 11키: payload·guard_ref·owner_epoch·conflict_priority)·`BaseQuiescenceReport`·시간/예약 typed 연산 5종 |
| `spatial.py` | 480 | 방향 구간 그래프(계층·connected는 구간 아님)·정적 가시성(저작 `visible_within`)·배치/TRANSIT(`start < end`, `start ≤ now < end`, 진행률 미저장)·희소 공간 지식(인지 통과 정보 `known_*`, provenance append+dedupe)·familiarity seed·공간 typed 연산 8종(H07 `ACTOR_PLACEMENT_CLEAR` — 같은 분에는 해제 시점 위치를 `placement_holds`로 보류) |
| `access.py` | 168 | `AccessRuleRegistry`(STATE_TABLE)·실제 접근 3결과/3정책 · 인지 접근(KnownAccessFact, 없으면 UNKNOWN) 분리 |
| `movement.py` | 577 | 고정 정밀도 소요시간(Fraction → 구간 정수 μs → 경로 전체 HALF_UP 분)·`KnownRoutePlanner`(지식만 — 실제 접근·구간 성질 미사용, Yen K-shortest·서명 동률·가중 Jaccard 다양성)·`ActualTraversalPlanner`(실제 구간 진실, 우회 없음, CONFIRM은 접근 행동 id 단위 — H04부터 override variant 포함)·`traversal_ops` |

#### 동적 세계 행위자·정보·사건·진행 (H04 — 순수 엔진·typed 연산, H05부터 DYNAMIC 턴 배선)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `world_refs.py` | 507 | 타입 ValueRef 16종(해석 결과 known/UNKNOWN 구분)·의존 키·컴파일 검증·결정적 표시. H07: 행위자 위치 ref는 유효 위치(`npc_position`), `ACTOR_POSITION_SOURCE`·`OBSERVER_HAS_ITEM`·`OBSERVER_STAT`·`EVENT_LOCAL` template 변형 |
| `actors.py` | 189 | `actor_registry` 검증·레거시 이름 → ActorId 해석(모호하면 후보만)·`ACTOR_REGISTER`/`ACTOR_RENAME`(개명 = id 유지·alias 누적) |
| `information.py` | 373 | 정보 상태(저작 = 기본 CURRENT, 런타임 명제 불변)·SUPERSEDED/REFUTED 계보·정보 계열(값 변화 = 새 revision InfoId)·typed 연산 4종 |
| `cognition.py` | 276 | 희소 인지(기록 없음 = 미인지)·획득 provenance 멱등·노출 ≠ 믿음·PC 믿음은 PLAYER_EXPLICIT·RULE_FORCED만·group 자동 전파 없음 |
| `relationships.py` | 79 | 희소 관계 delta(행렬 없음, delta_id 멱등) |
| `npc_projection.py` | 98 | 읽기 전용 `CurrentNpcProjection`(정본 저장 없음, 장소 연관 ≠ 현재 위치, 지정한 정보만) |
| `predicates.py` | 339 | 3값 조건 AST(자유 문자열·eval 없음)·시간 조건 → wakeup(매분 polling 없음) |
| `events.py` | 695 | 사건 인스턴스 정체성·binding 열거·PRIORITY/SCORE·STICKY 기본·결정적 가중 케이스·typed step 13종(H07 `CLEAR_ACTOR_PLACEMENT`)·사건 연산 5종. H07: 예약 지역 상태 `resolution`(플레이어 명시 결단 전용)·`open_resolutions`·`instance_relevant`·`resolution_relevant`(player-facing 결단 후보 관련성 gate — 저작 `relevance_places` 또는 사건 자신의 장면 조건·장면 인물, 장면 근거가 없는 섬 전체 압력 사건만 어디서나) |
| `world_progression.py` | 1,060 | DependencyIndex·같은 분 due 일괄(우선순위·`SameMinuteNarrativeTie`·장면 밖 안전 중단)·즉시 연쇄(라운드 32·사건당 분당 전이 8·진동 검출)·정보 계열 재평가·NPC 이동 checkpoint wakeup·신호/foreground·순수 `project_world_progression` |

#### 동적 세계 H07 (영도 이행 — DYNAMIC 세션)

| 모듈 | 내용 |
|---|---|
| `npc_position.py` | NPC 유효 위치 단일 경계 — 정본 override > 평상시 일과(선택 저작 `default_npcs[*].routine`, 정의 + 정본 시계의 순수 함수) > untracked. 장면 인물·CurrentNpcProjection·사건 조건·사건 이동 출발점이 같은 해석을 쓴다. 일과 경계 wakeup(실제 장소 변화 분만), 정본 SET·materialize 없음. 명시 해제 직후 같은 분에는 해제 시점 위치에 머물고(순간이동 없음), 일과가 이미 그 자리이거나 시계가 흐른 뒤 일과로 복귀 |
| `place_display.py` | 내부 PlaceId ≠ 플레이어 표시명(`display_name`·`unknown_name`, PC 지식 수준). 이름이 드러나지 않는 장소는 정보 `reveals`(명시 발견)로만 이름을 안다 |

이동(PC·NPC 공통, H04 R55 의도적 supersede): 아는 길 우선, 없으면 PUBLIC/LOCAL 일반 그래프 + 아는 RESTRICTED/SECRET — 지식은 경로 품질, 통과·권한은 실제 접근. 저작 명시 사건 route는 연속성·실제 접근만 검증. 메인 결말·행적은 국면 `resolution_options` → 판단층위 키 → typed `EVENT_LOCAL_SET(resolution, PLAYER_RESOLUTION)` → 저작 국면·케이스. 국면 guide는 지시층위 전용(R-2). 정의 바인딩 세션은 레거시 퀘스트·info_ledger·hidden 인지·정적 상주 동행 해제를 은퇴한다(`player_world.legacy_narrative_retired`) — 소속은 사건 결과 파생 호환 뷰.

H04는 세계가 스스로 진행되는 데 필요한 정본과 **순수 투영**까지만 만든다. 결과 연산은 바깥 트랜잭션(PLAYER_TURN·INITIALIZATION·ADMIN_EDIT)의 묶음으로 기존 커밋 경로가 적용한다 — 플레이어 턴 orchestration은 H05, 초기 행위자·정보·사건 seed와 정의 스냅샷 바인딩은 H06, 레거시 퀘스트·`info_ledger` 권위 은퇴는 H07이다. H04는 같은 분 서사 선후(`SameMinuteNarrativeTie`)를 스스로 고르지 않는다(H05 묘사 관찰이 정한다).

#### 동적 세계 플레이어 턴 (H05 — 자동 턴 배선, DYNAMIC 세션만)

| 모듈 | 줄 | 내용 |
|---|---|---|
| `action_intent.py` | 409 | `ActionReferenceManifest`(PC 지식 기반 후보 → transient 키 P/R/K/I, world_version·revision 결속)·판단 스키마 런타임 확장(`action_intent`, 추가 provider 호출 0)·`ResolvedActionIntent`(목적지·시간·경로 선택·접근 확인 토큰·명시 PC 태도, 후보 밖 키는 거부)·시간 목표 계산 · v7.22 보이는 출구 단일 출처 `exit_candidates`(E키 → 실제 구간, EXIT/WANDER) |
| `player_world.py` | 745 | 세계 모드(`definition_revision` 바인딩 + 정량 시간·구조화 장소 + 시계 초기화 + PC 배치 = DYNAMIC, 그 외 LEGACY)·경로 선택 정책(의미 있는 차이는 ASK, 자동은 (비용, 서명) 결정적)·구간별 실제 접근 + 구간 사이 H04 세계 진행(관찰 신호 중단·숨은 봉쇄는 도달 시 관찰)·대기/수면 ActivityContext·PLAYER_EXPLICIT 태도·`namespace_ops`/`chain` · v7.22 출구 구간 강제(평행 구간 대체 없음)·`wander_exit` 결정적 한 구간(아는 막힘 제외, 재추첨 없음) |
| `turn_world.py` | 1,077 | 묘사 오버레이(투영 장면·관찰 가능한 결과만 · 정본 이동 없는 턴은 장소 고정 문구 · 묘사층위 세계 블록 `narration_world_block` — 정본 날짜·요일·시각·장소·출구·장면 인물 독립 블록 · 목적지 불명 이동은 보이는 출구로 되묻기)·레거시 `world_timeline` 표시 미러(정본에서 파생)·관찰 매니페스트(장면 행위자가 아는 정보·장면 신호·선후 선택지)·`process_observation`(노출 → PC 획득, 비코드 소유 추가 시간, 선후 검증, 보충 진행)·`ForegroundContinuationPlan`·투영 미디어 허용 장소 · v7.14 주변 발견(`ambient_discovery` RUNTIME 정보 + PC 인지, 관찰 매니페스트 D키 수거·소진 → SUPERSEDED·소지품 +1) |

자동 턴 순서(DYNAMIC, v7.18 P1): 판단(매니페스트·`action_intent` + 행동 소요 분 `action_minutes`) → 코드 계획(이동·대기·행동 시간 모두 묘사 전 진행, 경로 ASK는 지시층위 전) → 지시(같은 분 선후 `tie_order` 선택 — 무효면 코드 결정) → 단일 묘사(오버레이 필수 서사·전달 대기 '그사이' 블록·투영 장소 블록·투영 장면 이미지) → 추출(`world_observation` — 노출·표현 신호·발견·아이템, 추가 시간 없음) → 같은 분 보충 진행(계획과 같은 관찰자 문맥) → READY(`stage_world_ops`) → CommitCoordinator(+`world_pending_signals`) → 표시 갱신 → 다음 행동 질문. 후속 묘사는 없다 — v7.21.1: 묘사 후 보충 진행이 만든 관찰 신호만 다음 커밋된 PROCEED 본문에 1회(발생 시각·장소·`exposes` 보존, 재이월 없음), 묘사 전 계획 신호는 이월하지 않는다. 장소·시간 권위는 정본 세계이며 추출의 장소·날짜는 적용되지 않는다. 묘사 전 결정적 충돌은 묘사 호출 없이, 묘사 후 충돌·모호한 선후는 재시도 없이 FAILED_SYSTEM(청구 0). LEGACY 세션은 기존 경로와 동일하다. 운영 세션이 DYNAMIC이 되려면 H06 정의 바인딩·genesis가 필요하다(현재 영도 단일 PC만).

READY와 `canonical_mutation`은 투영 결과의 **base quiescence**를 증명한다 — 초기화된 시계에 `due ≤ now` pending 예약이 남거나, 미초기화 시계에 예약이 있으면 `WORLD_PROJECTION_CONFLICT`(코드 `NOT_QUIESCENT`, 재시도 아님). 레거시 빈 시계·빈 예약은 quiescent다. H03·H04 연산의 production producer는 genesis(INITIALIZATION)와 DYNAMIC 플레이어 턴이다. LEGACY 세션에서는 `places.py` 이동·`world_timeline` 시간이 그대로 권위다.

#### 동적 세계 생애주기 (H06 — 단일 PC DYNAMIC만)

| 모듈 | 내용 |
|---|---|
| `world_lifecycle.py` | 유일한 생애주기 owner — 정의 스냅샷 바인딩(`definition_revision` 단일 writer)·시작 틀 저작 `world_start`(v7.26 `items` — 그 시작에서만 생기는 장소 물건·NPC 소지, genesis와 같은 strict 저장) → InitialWorldPlan → `CanonicalMutationCoordinator(INITIALIZATION)` genesis(멱등)·생애주기 `world_lifecycle`(INITIALIZED/OPEN/CLOSING/CLOSED)·재시작 검증(`restore`)·종료 안전 지점(`close_blocked_reason`·`close_session`) |
| `cache_source.py` | `FrozenCacheBuildSource` — 짧은 io 락 캡처(바인딩 스냅샷 원문·기준 world_version·이력 표식), 락 밖 조립·생성. 생성 중 revision 변경 세대는 비활성 |
| `admin_world_edit.py` | typed ADMIN_EDIT 서비스(활성 턴·이력 조작·stale 거부, no-op은 version 없음, 턴 수·청구 불변) — 운영자 명령 없음 |

DYNAMIC = 단일 PC + 스냅샷 바인딩 + 필수 capability + 생애주기 OPEN + 시계 + PC 배치(AT_PLACE 또는 TRANSIT). 초기화된 세션에서 정의를 읽을 수 없거나 PC가 늘면 BLOCKED(턴 거부). 바인딩 세션은 재시작 때 `scenario_data`를 스냅샷 원문으로 되돌린다(원본 drift 무시). 사건 조건 holder `$session_pc`는 COGNITION_AWARENESS 전용(PC 믿음 조건은 컴파일 오류). 재서술은 그 턴의 revision이 현재 바인딩과 같을 때만, 보존된 의미 선택으로 세계 계획을 다시 세운다.
R2: 초기화 실패는 fail-closed(실패 표식 strict 저장 — LEGACY 진행·인트로 금지, 재시도 버튼). 인트로가 실제로 성공해야 OPEN(strict)·첫 라운드. `!세션종료`는 LEGACY·DYNAMIC 모두 OPEN → CLOSING(strict) → 캐시 단일 finalizer → CLOSED(strict)로 끝나고 활성 등록을 내린다. 환급 뒤 CLOSED 저장이 실패해도 durable CLOSING이 남아 턴·참가·재개(캐시 재오픈)를 막고, 재시도·재시작이 환급 반복 없이 CLOSED를 완성한다. 재시작은 CLOSED 세션을 다시 올리지 않는다(`disp:close`는 재오픈 가능한 캐시 창 종료로 별개). DYNAMIC 시작의 장소·시간 owner는 저작 `world_start` → genesis 하나(시작 틀 facts는 LEGACY에서만 적용, 표시용 `world_timeline`은 정본에서 파생). genesis에 저작 NPC를 ActorRegistry로 등록한다(위치 추론 없음). 플레이 가능한 이동 공간은 일반 PlaceId다 — 중간 장소 도착은 독립 진행이라 도착 사건이 그 장소에서 멈추고, 구간 안 중단은 TRANSIT을 유지하며 다음 턴에 남은 시간만 이어 간다(경로 무효화 시 강행 없음). 플레이어 턴의 필수 연산이 handler에서 거부되면 결정적 충돌(묘사 전 실패)이다.

#### 기억

| 모듈 | 줄 | 내용 |
|---|---|---|
| `memory_plan.py` | 306 | 압축 플랜 4종 |
| `rewind.py` | 231 | 레거시 델타 로그(비권위, `TRACKED_PATHS` 14개). 되감기 권위는 `turn_history` |

#### 미디어·UI

| 모듈 | 줄 | 내용 |
|---|---|---|
| `display.py` | 745 | **디스플레이 채널** — 상태·UI 단일 메시지, `notify`/`close_notice`. v7.10: 상태판은 이미지(`display_board`)로 Embed를 대체하고 버튼은 같은 메시지, 렌더 실패 시 `build_embed`로 대체. 세션별 갱신 직렬화·같은 SVG면 재업로드 생략, 버튼은 defer 후 갱신 |
| `display_board.py` | 551 | **디스플레이 상태판 SVG**(엔진 공통) — `board_data`(build_embed와 같은 출처)·코드 조립 SVG(가변 높이: 플레이어·프로필·상태·소지 칩 3줄+`+N`·세션 상태 오픈/만료/클로즈·경고)·글자 폭 근사 맞춤/말줄임. 시나리오 흔적은 이름(`scenario_id`)과 라틴 부제(선택 키 `display_latin` > `koreantext.romanize`)로만 주입. 프로필은 20자 이하·이름과 다른 값만. 만료 예정은 KST 절대 시각. 래스터화 = `speaker_card.rasterize` |
| `speaker_card.py` | 982 | **SVG 화자 카드**(DYNAMIC 전용) — 시나리오 최상위 `speaker_card`(기본 `legacy_png`)·커밋된 PC 인지(CONTENT) 기반 항목별 공개(`all_info` AND, 슬롯당 규칙 1개)·지시층위 얼굴·이름 식별(`identified_faces`·`identified_names`)·안전 슬롯 치환·자산 SHA-256·래스터화(resvg CLI/`resvg_py`, 운영 설치 필요)·독립 저작 검사(`scenario_check`). 렌더 지연 = 로딩 이미지 후 교체, 실패 = 오류 이미지(v7.19). v7.20: 같은 턴 실제 공개(추출 인용 확인) 뒤 문단부터 이름·얼굴 표시. 미식별 표기 `???`, 시나리오 `plain_speakers`는 원문 표기·이미지 없음. v7.31: `fixed_speakers`(개체 없는 이름 화자, 예: 감염자)는 원문 표기·항상 전체 공개 카드, `assign: turn_random`이면 턴마다 결정적 추첨 그림. 재색은 `baseline` 기준색 배율, `front_ids`로 지정 요소를 맨 앞에 그림. 영도 저작: NPC 47명 카드·얼굴·이름 InfoId 94개(`yd.info.card.*`, revision `7cfe9f16…`) |
| `gm_board.py` | 265 | **GM 스페이스·시작 상황 카드 SVG**(v7.27, 상태판 톤) — 홈·명예의 전당(금·은·동)·서버보드·월드보드·내 통계·시작 상황 삼지선다(번호 = 버튼). 기존 Embed와 같은 출처·공개 규칙, 멘션 대신 서버 표시 이름, 렌더 실패 시 기존 Embed/글로 대체. 틀은 `display_board.framed` 공용 |
| `audio_mixer.py` | 393 | BGM/플리 + 효과음 + 음성 합산 |
| `tts.py` · `tts_preset.py` | 436 | Gemini TTS, 시스템 문구 사전 합성 |
| `media.py` · `media_control.py` | 316 | 이미지 전송, 플레이리스트, BGM 선택 |
| `ui.py` | 282 | 주사위 뷰, 채널 삭제 뷰 |
| `ui_expiry.py` | 74 | **버튼 유효시간·재시작 승계**(v7.28) — 받을 화면이 없는 버튼 판별·안내(`answer_stale`, `ErrorCog.on_interaction`)·C 보조 화면 만료 잠금 `ExpiringView`. A 진행 화면 재부착은 `session_flow.resume_all`(`resumable_prompts`·`creation_state`) |
| `chat_guard.py` | 82 | 채널별 권한 검증 (단일 훅) |
| `utils.py` | 326 | 캐릭터 검색, AI 설정 생성, 상태이상 weight 합산(`status_roll_modifier` — v7.12부터 판정 경로 미사용, 상태 영향은 판단층위 status_mod) |

---

## 데이터 흐름

### 세션 저장

```
sessions/{session_id}/
├── data.json              세션 스냅샷 (원자적 쓰기)
├── logs/                  구조 로그(core.logs) — calls·blobs(델타 조각)·events·domain·chat JSONL
├── full_logs.jsonl        전 턴 대화 원본
├── turn_commit_journal.jsonl   커밋 단계 저널 (CommitJournal)
├── turn_settlements.jsonl      턴 Settlement
├── turn_history.jsonl          커밋 턴 이력 인덱스 (+ 턴별 레코드 디렉터리)
├── history_op.json · history_pending_messages.json   되감기/재생성 진행·정리 부채
├── transient_messages.json     임시 안내 메시지 레지스트리
├── cache_lifecycle.jsonl       캐시 창·생애주기 저널
├── interpretation_billing.jsonl 유지 시간 해석 청구 저널
├── retry_claims.jsonl          추출 재시도 provider claim
├── canonical_mutation_journal.jsonl  INITIALIZATION/ADMIN_EDIT 정본 변이 저널 (DYNAMIC genesis부터 생성)
├── definition_snapshot.json   바인딩된 정의 스냅샷 (H06 — DYNAMIC 세션만)
├── rewind_log.jsonl       레거시 턴별 델타 (비권위)
└── rewind_archive.jsonl   되감기로 제거된 정보
```

세션 외부: `logs/bot.jsonl`(전역 구조 로그) · `profiles/{uid}.json` · `accounts/{uid}.json` · `accounts/ink_transactions/{uid}.jsonl` · `stats/{uid}.json` · `data/cost_ledger.jsonl`(전 세션 provider 비용 원장)

### 스키마 마이그레이션

`SCHEMA_VERSION` 4. 필드명이 바뀐 버전은 `migrate_session_data`가 로드 시 자동 변환한다. v3 → v4(H02)는 새 정본 필드를 안전한 빈 기본값으로, `narrative_seed`를 session_id에서 결정적으로 채운다(멱등, 기존 시드 불변). turn-history는 v3이며 v2 레코드는 새 필드를 레거시 기본값으로 합성해 복원한다.

---

## 시나리오 데이터

### 영도 (`scenarios/영도.json`)

| 항목 | 수 | 비고 |
|---|---|---|
| `places` | 125 | 계층 그래프. `tier` 없이 `parent`만 |
| `default_npcs` | 47 | 32명이 장소에 배치 |
| `jobs` | 16 | 8종에 `top_stat` |
| `start_frames` | 6 | 도착 서사(v7.26 재저작). 고정 문안 · 시작 물건 · 시작 장면 인물 사건 |
| `profile_creation` | 10단계 | 성별→이름→나이→직업→스킬→능력치→외모→외형→배경→복장 |
| `status_effects` | 5 | 감염 의심·감염·저체온·탈수·허기 |
| 퀘스트 (`영도.quests.json`) | 44 | 초반 6 + 가입 16 + 장소·인물 15 + 메인 7 |

### 무협 (`scenarios/무협.json`)

`places`·`profile_creation`·퀘스트가 **없다.** 스탯 4종(무공·내공·신법·기예), 프로필 9항목으로 영도와 체계가 다르다.
`location_images`는 비어 있다(WP-G D2-b: 가짜 예시 키워드 2개 제거). `image_prompts`의 예시 문구는 사용자 결정으로 유지한다.

### 시나리오 필드 메모 (WP-G 정합)

| 필드 | 런타임 의미 |
|---|---|
| `keyword_memory` | 플레이어 연고지(사문·근거지) 섹션을 캐시 `[1-B]`에 편입하는 원천. 턴별 키워드 온디맨드 주입은 폐지됐다 |
| `media_dir` | **런타임 미사용.** 미디어 경로는 항상 `media/{시나리오 id}`로 계산한다(키는 호환용으로 유지) |
| `job_guides` (영도) | 읽는 코드가 없는 비활성 저작 데이터 |
| `location_images` | 지시층위에 "사용 가능한 장소 이미지 목록"으로 주입된다 — 실재 이미지 키워드만 넣을 것 |

시나리오 세대 간 구조 차이는 정규화 대상이 아니다.

---

## 핵심 시스템

### 턴 확정 권위 (WP-A~F)

자동 턴 하나 = `TurnTransaction` 하나. AI 결과는 준비(`turn_preparation`)에 스테이징되고, 필수 작업이 모두 합류한 `READY_TO_COMMIT` 뒤에 `CommitCoordinator`만 정본 상태를 커밋한다(`CommitJournal`로 복구). 게임 채널 출력은 `message_lifecycle` 5분류로 소유되며, 백그라운드 압축은 출처 식별이 맞을 때만 적용된다. 수동 `!진행`·`!수정`은 WP-G에서 은퇴했다 — 공유 묘사 엔진(`_execute_proceed`)의 호출자는 자동 턴과 인트로뿐이다.
H02: `TurnPreparation`은 world_version·definition_revision 기준선과 `WorldMutationBatch`(LEGACY 턴은 빈 묶음, DYNAMIC 턴은 `stage_world_ops`로 세계 연산)를 소유하고, READY에서 묶음을 동결·투영한다. H03: 투영 뒤 base quiescence를 증명해 `ReadyProof.base_quiescence`에 남긴다. 투영 충돌(`WORLD_PROJECTION_CONFLICT`)은 결정적 종료 실패이며 추출 재시도(RETRY_PENDING)가 아니다. `CommitPlan` v2는 source/target world_version·definition_revision·mutation digest를 PREPARED에 동결하고, v1 PREPARED는 정상 구버전으로 복구한다.
추출 결과의 정본 적용은 커밋 경로 하나뿐이다(`CommitCoordinator` → `_apply_commit_effects` → `_apply_prepared_extraction` → `_apply_extraction_plan`). `_run_extraction`은 준비 객체 없이 호출하면 provider 호출 전에 거부된다. WP-C 이전 형식의 추출 재시도 컨텍스트는 재추출·정본 변이 없이 은퇴된다(`commit_coordinator.retire_legacy_retry_context`).

### 프롬프트 권위 (WP-G D1)

단일 선형 서열이 아니라 정보 종류별 권위다.

- **플레이어 주권** — PC의 선택·생각·감정·행동을 AI가 확정하지 않는다(세계 사실의 출처 서열과 별개인 최상위 제약). 플레이어 선언은 PC가 무엇을 시도·말·선택하는지의 권위다.
- **룰북 고정 사실·금지사항** — `session.note`·GM 지시로 무효화되지 않는다.
- **런타임 상태** — 변하는 항목(NPC 현재 위치·상태 등)에 한해 룰북 초기값보다 우선한다.
- **`session.note`·캐시 노트** — 세션 한정 사실 추가·범위 축소. 룰북 사실·런타임 상태와 충돌하면 자동 override하지 않는다.
- **GM 지시·`[진행자]` 중계·사이드 노트** — 위 사실과 제약 안의 진행 지시.
- **압축 기억** — 파생 기억. **플레이어의 외부 세계 주장** — 확인되지 않은 주장.
- 판단층위는 세계 설정을 받지 않으므로 세계 사실을 판정하지 않는다. 추출층위는 스토리 권위가 아니다. 자원·상태 변화는 묘사 → 추출 → 코드 검증으로만 적용된다(`자:/태:` 태그 권위 없음, `상:/중:/하:` 이미지 태그는 유지).

### 선언 게이트 — 역사칭·주변 발견 (v7.14)

(v7.23: DYNAMIC은 판단 뒤 조건부 축소 선언 검증이 같은 규칙·스키마로 이 게이트를 낸다 — 판단의 `ambient_proposal`·`assertion_claim`·`discovery_ref` 표시가 있을 때만, 발견 굴림은 판단 초안을 검증 뒤 집행. v7.24: 이 초안 계약은 판단 시스템 지시 [선언 검증 — 주변 발견·결과 선취] 절이 소유한다. LEGACY는 아래 그대로) 개연성 시뮬레이션(기존 캐시 호출)의 `declaration_gate`가 선언을 ORDINARY·AMBIENT_PROPOSAL·IMPROPER_ASSERTION으로 나누고 CONTINUE·ACCEPT_NO_ROLL·ROLL·BLOCK·CLARIFY를 정한다(새 provider 호출 0). 코드 검증(`cogs/gm.py` `_parse_gate`): 열거값이 깨지면 게이트 없음, 발견 대상명은 플레이어 원문 낱말만(아니면 CLARIFY). 판단·지시층위에는 열거값과 대상명만 — 시뮬레이션 자유 서술은 판단층위로 가지 않는다.

- BLOCK·CLARIFY → 판단 호출 없이 중립 안내(굴림 0). 게이트 없음(캐시 불가) + 발견 제안 → "확인할 수 없다" 안내(굴림·정보 연산 0). IMPROPER_ASSERTION + CONTINUE → 시도만 진행(지시층위 `[선언 처리 — 코드 확정]`에 판단 `player_intent`를 권위 있는 시도로).
- 게이트 ROLL → 판단의 `discovery=true` GENERAL 굴림 1개 필수(없으면 계약 위반 재시도). 발견 성공 = 그 굴림의 성패만. 굴림 기록 `roll_records[*].discovery`. 실패는 '이번에 찾지 못함' — 정본 무변경.
- DYNAMIC만 영속: 승인·성공 → 관찰 단계 `INFORMATION_CREATE`(`rt.ambient.<tx digest>`, `ambient_discovery`, `place:<PlaceId>`) + PC `COGNITION_ACQUIRE(CONTENT)` → READY → CommitCoordinator. 관찰 매니페스트 D키(현재 장소 유효 발견 + `DNEW`)로 추출이 `discovery_taken`·`discovery_gone`을 지목 → `INFORMATION_VALIDITY_SET(SUPERSEDED)`, 수거(OBJECT만)는 같은 커밋 소지품 +1(같은 이름 + 중복 제거). 지시층위 '현재 장소에서 PC가 발견해 둔 것' 블록(최근 인지 12건과 무관), `pc_knowledge_block`은 CURRENT만. A1 보정: 추출이 D키를 빠뜨려도 게이트가 검증한 선언 대상(`referenced_info_id` 또는 이번 턴 DNEW)이 매니페스트의 유일한 동명 OBJECT이고, 같은 이름 소지 변경이 PC +1 한 건뿐이며, 명시 키가 없고, PC가 턴 전에 같은 이름을 갖고 있지 않을 때만 키 기반 수거로 정규화한다(`turn_world.implied_discovery_take`). 그 밖에는 이름으로 연결하지 않는다.
- 반복 탐색: 시뮬레이션 입력에 이 장소의 유효 발견(D키 — `existing_key` 재사용)과 찾지 못한 이전 탐색(`turn_history.recent_failed_discoveries` — 선택 이력 roll_records)을 넣고 조건 불변이면 BLOCK(UNCHANGED_RETRY). 횟수 제한·쿨다운·부재 기록 없음.
- LEGACY: 발견 승인·판정은 일반 행동으로(정보 연산 0), BLOCK·CLARIFY·역사칭 처리만 적용.

### 캐릭터 생성 (v7.15 — `profile_creation_ui`)

시나리오 `profile_creation` 순차 단계·버튼·능력치/스킬 규칙은 그대로다. 텍스트 답변은 모달이 아니라 **채팅**으로 받는다 — 화면이 텍스트를 기다릴 때(`run["await"]`, 화면 세대 `run["gen"]`) 그 채널·그 플레이어의 메시지 1건만 소비하고(`on_chat`, v7.21.3: 소비한 답변 메시지는 인식 뒤 삭제 — 실패·DM은 무시), 조작(이전·수정·취소)은 버튼으로만 한다. 생성 중인 플레이어의 채팅은 `claims`(공유 상태)로 판정해 GMCog가 생성 경로로 넘기고 GameCog는 첫 턴 맥락(`current_turn_logs`)에 쓰지 않는다.

- 장문 필드(`ai_merge` 또는 `args.creation_guide` 저작): [직접 작성] / [질문형 가이드](시나리오 저작 `questions[{id,prompt,required,options?}]`·`compose_rules`, 선택 질문만 건너뛰기). 가이드 답이 둘 이상이면 기존 `profile_ai.merge` 1회로 조합, `ai_validate` 필드는 조합본을 검증. 초안 검토 = 승인 / [부분 수정](채팅 → merge [초안, 수정]) / [질문 다시 고르기] / 처음부터. 길이 초과는 자르지 않고 승인을 막는다.
- 단계가 끝나면 **전체 최종 검토** — 필드 수정(`runner.edit_field`: 그 필드 + `depends` 의존 필드만)·[원래대로]·[이대로 시작]. `finish`(players·초기 소지품 반영)는 최종 승인 뒤 1회. 저장 프로필 불러오기도 최종 검토로 들어간다(값·자료형 그대로, 형식이 맞지 않으면 다시 정할 때까지 승인 불가, 원본 파일 불변).
- 세계관 충돌은 사유 안내 후 같은 항목 재입력 — 검증 응답 스키마에 `suggestion` 없음.
- 마스터 대사: 공용 `profile.*` 키(+ v7.15 6키), 단계별 `args.master_keys`로 키 오버라이드. 새 LLM 호출 없음.
- 영속: 세션 필드 `profile_creation_progress`(`{uid: {version, char_name, channel_id, scenario_id, run}}`)에 화면마다 strict 저장, `main.on_ready` → `resume_all`이 안전한 화면을 다시 그린다(처리 중이던 AI 입력은 다시 받음, 최종 승인 뒤면 저장 화면만). 저장 여부 선택 뒤 진행 기록 삭제. DM 사전 생성은 같은 UI(채팅 포함)지만 영속하지 않는다.
- 저작 검사: `profile_gen.validate_steps` — `tools/scenario_check.py`가 `profile_creation`이 있는 시나리오에 독립 실행.

### 장소 계층 (`places.py`)

`tier`를 두지 않고 `parent`만 가리키며 깊이는 결과일 뿐이다.

| 필드 | 의미 |
|---|---|
| `connected` | 직결 |
| `reachable` | 한 턴 이동 범위. 미지정 시 `connected` 3칸 확장 |
| `visible_within` | 상위 안에 들어가야 인지 가능 |
| `known_brief` / `unknown_hint` | 방문 여부에 따라 다르게 주입 |
| `image` | 없으면 상위 중 가장 하위의 것 상속 |

`reachable` 밖 목적지는 ASK로 안내한다. 판단층위가 캐시를 읽지 않으므로 코드가 경로를 계산해 프롬프트에 넣는다.

### 퀘스트 (`quest.py` · `quest_filter.py`)

**필터가 곧 슬롯이다.** 후보를 나열해 두면 일치한 값이 빈칸을 채운다.

```json
"npc": {"any": ["엄주섭","차봉순","황기영","법운"], "as": "기록자"}
```

장부방 → 엄주섭, 지하 병참 창고 → 차봉순. 하나의 틀이 여러 곳에서 각기 다른 사건이 된다.

개연성은 구조로 막는다 — NPC 후보는 **현재 장소에 실재하는 인물과 교집합**을 먼저 낸다.

| 필터 | 기능 |
|---|---|
| `any`/`all`/`none` | 하나 일치 / 전부 / 배제 |
| `as` | 슬롯 이름 |
| `scope` | `here`(상주) / `companion`(동행) / `any` |
| `pair` | 인물·장소가 함께 성립 |
| `place`/`within` | 정확 일치 / 상위 경로 포함 |
| `faction`/`faction_scope` | 소속 / 영역 |
| `item`·`info`·`time_of_day`·`min_stat` | |
| `min_outcome` | `{"deviated": 2}` — 성향을 결말 이력으로 판정 |
| `min_visited` | 방문 장소 수 |
| `grants` | 클리어 시 세션 반영 (소속 획득) |

**퀘스트 선택** — 지시층위가 `quest_choice`로 고르거나, 시나리오가 `quest_select: "random"`이면 코드가 무작위로 고른다.

### 서사 모드

```
narrative_mode == "free"   서사설계자가 주도 (풀자유·인피니티 designer 플랜)
그 외                      퀘스트 시스템이 주도. _plan_narrative는 즉시 반환
```

두 서사가 겹치면 지시층위가 어느 쪽을 따를지 모호해진다.

### 동행 (`extraction.py`)

`met_npcs`(누적)와 `companions`(현재)를 분리한다. 추출층위는 `joined`/`left` **변화만** 보고한다. 장소 이동 시 그곳 **상주** NPC는 자동 해제된다.

### 되감기·재생성 (`turn_history.py`)

WP-E 이후 되감기(`!되감기`, `disp:rewind*`)와 재생성(`!재생성`)은 커밋된 턴 이력(`core.turn_history`)을 권위로 삼는다. 선택 전환은 durable COMMITTED 이후에만 일어나고, 재무 사실(CostEvent·Settlement·잉크)은 되돌리지 않는다. `rewind.py`의 델타 로그는 비권위 레거시 기록이다.

### 비용

권위는 셋으로 나뉜다(WP-02·D·F·G).

| 사실 | 권위 |
|---|---|
| provider 사용량·비용 | `CostLedger` CostEvent (기록 당시 KRW/USD 그대로) |
| 정상 턴 플레이어 부담 | `Settlement` → `InkTransaction` (정확히 한 번) |
| 캐시 창 선불·환급 / 유지 시간 해석 청구 | `cache_lifecycle` · `interpretation_billing` 저널 + `LifecycleInkTransaction` |

```python
core.accrue(session, krw, usd)   # 호환 미러(total_cost/total_usd)만 쌓는다 — 규칙 입력 금지
```

`total_cost`와 `total_usd`는 집계 범위가 다르다(무료 프로필 AI는 usd에만). 둘을 환율 등식으로 잇지 않는다.
자동 GM 비용 상한은 CostLedger 세션 합계 기준이다(`core.auto_cost_cap_reached`, fail-closed).
압축 provider 비용은 운영자 부담 유지비다(WP-G D3 — '압축 선결제' 은퇴).

**사고 토큰 주의** — thinking 계열은 `thoughts_token_count`를 별도 반환한다. `candidates`와 합산해야 하며, 누락하면 40%를 과소 계상한다.

**플레이어에게는 잉크만 보인다.** 원 단위는 마스터 전용이다.

누적 잉크는 `total_ink_spent`를 쓴다. `cost_to_ink(total_cost)`는 매 턴 올림한 실제 차감액과 어긋난다.

### 캐시 수명

```python
remaining_ttl(session)      # 남은 시간만 이어준다 (재발급·복구 시)
is_cache_expired(session)   # 시간상 만료 판정
is_session_open(session)    # cache_name && not expired
```

`cache_name`만 보면 만료 뒤에도 열린 것으로 보인다. 프레즌스 루프가 15초마다 점검해 디스플레이를 갱신하고 게임 채널에 알린다.

---

## UI 진입점

### GM 스페이스

```
gmspace:register  gmspace:session  gmspace:profiles  gmspace:charge
gmspace:hall      gmspace:world    gmspace:stats     gmspace:refresh
```

### 디스플레이 채널

```
disp:tts  disp:image  disp:bgm  disp:sfx     미디어 토글
disp:vol_down  disp:vol_up                   볼륨
disp:rewind  disp:rewind_multi  disp:restart 되감기·재시작
disp:open  disp:close                        세션 열기/닫기 (분리)
disp:pay                                     결제
disp:master_gender                           진행자 남/여(세션 개설자, v7.19)
```

상태에 맞지 않는 버튼은 미리 잠근다 — 열림이면 `disp:open`, 닫힘·만료면 `disp:close`.

### 게임 채널

```
session:join  extraction:retry  rewind:one
```

---

## 테스트 하네스 (WP-00)

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

`tests/`는 **현재 동작과 권위 불변식을 고정**한다. 현재 1,738건(특성화 61 · 결함 27 · 정책 1,650)이 모두 통과하며 strict xfail은 0건이다.

| 분류 | 의미 |
|---|---|
| `characterize` | 관측된 동작 고정. 의도적 변경 시 함께 바뀐다(예: `_execute_proceed` 호출자 = 자동·인트로) |
| `defect` | 알려진 결함 재현. 바람직한 단언은 `xfail(strict=True)` — WP-B~F에서 전부 해소돼 현재 0건 |
| `policy` | 목표 정책·권위 불변식(WP별 `test_wp_*`, 커밋·청구·이력·캐시·메시지·프롬프트 권위) |

권위 불변식 테스트는 소스 스캔으로 "누가 부르는가"를 고정한다(예: strict 저장은 `commit_coordinator`만, 추출 정본 적용은 커밋 경로만, 레거시 tolerant 계정 writer 프로덕션 호출자 0). **테스트를 완화해서 통과시키지 말고 설계를 맞춘다.**

**strict xfail이 XPASS로 바뀌면 실패한다.** 결함이 고쳐졌다는 뜻이므로
리뷰 후 xfail을 제거해야 한다.

라이브 디스코드·Gemini·네트워크·자격증명을 쓰지 않는다.
`tests/conftest.py`의 autouse 픽스처가 자격증명을 지우고 SDK 생성을 막는다.

인도 번들: `handoff/WP00_*` ~ `handoff/WP_G_*` (WP-G: 완료 번들·사후 스캔·AUD-001~063 최종 매트릭스·시나리오 매트릭스·제어 동기화 제안) · `handoff/H01_*`~`handoff/H07_*`(동적 세계 H01~H07, H07 저작 근거 `handoff/H07_YEONGDO_WORKBOOK_V2/`)

---

## 기능 명세 작업 (HISTORICAL)

> **HISTORICAL** — v5.29.1 기준 역추출 명세 작업의 기록이다. 이후 코드는 사용자 WP 지시(WP-A~WP-G)로
> 변경됐으며 `specs/`는 당시 스냅샷이다. 현재 동작은 이 문서의 나머지 절과 `handoff/WP_G_COMPLETION_BUNDLE.md`를 따른다.

전 기능의 명세를 코드에서 역추출하는 작업이었다.
당시 규칙: 명세 작업 중에는 코드·시나리오·프롬프트를 수정하지 않는다.

- 규칙: `SPEC_RULES.md` (작업 전 전문을 읽을 것)
- 진행: `specs/00_INDEX.md`
- 산출: `specs/{번호}_{영역}.md`

이 작업이 필요한 이유는, 5,000줄 시절 사용자가 전부 인지하고 유기적으로
설계한 요소들의 의도를 25,000줄이 된 지금 Claude가 파악하지 못하기
때문이다. 퀘스트 미시작·자동 GM 미작동·NPC 이미지 미출력이 모두
"이것이 왜 있고 무엇과 연결되는지 몰라서" 생긴 사고였다.

---

## 작업 원칙

**① `prompts.py`·`scenarios/*.json` 불가침**
수정안을 제시하고 승인받은 후에만 적용한다.

**② 계획서 선행**
코드보다 md 계획서를 먼저 작성해 검증받는다.

**③ 기억에 근거한 임의 판단 금지**
저장된 기억으로 방향을 결정하지 않는다. 불명확하면 질문한다.

**④ 유기적 정합성**
서로 얽힌 기능이 충돌 없이 굴러가도록 한 번에 개발한다.

**⑤ "완료"의 기준은 사용 가능 여부**
모듈이 임포트되는 것은 완료가 아니다. **플레이어가 그 기능에 도달할 경로가 존재하는지**까지 확인한다.

**⑥ 오류 보고 전 실물 확인**
검출 결과를 그대로 보고하지 않는다. 코드를 열어 읽고 확인한 뒤 보고한다.

**⑦ 표기값은 단일 출처에서 읽는다**
버전·초대 링크처럼 여러 곳에 나타나는 값은 상수 하나만 두고 읽는다. 임베드에 박아두면 배포 후에도 낡은 채로 남는다.

**⑧ 데이터 불일치는 물어본다**
이름·경로가 어긋날 때 어느 쪽이 맞는지 임의로 정하지 않는다. NPC 이름과 이미지 파일명이 달랐을 때 데이터가 많은 쪽을 골랐다가 반대였다.

**⑨ 가장 단순한 해법을 먼저 본다**
미디어 폴더명이 시나리오 id와 달랐을 때 코드에 경로 해석 계층을 넣으려 했다. 폴더 이름만 바꾸면 될 일이었다.

**⑩ 보고 파일은 저장소에 넣지 않는다**
개발사항이 아닌 보고·조사·로드맵 문서는 커밋하지 않고 항상 채팅에 파일로 제출한다(2026-10-18 사용자 결정). 기존 `handoff/` 번들은 HISTORICAL로 그대로 둔다.

### 버전 체계

`MAJOR.MINOR.PATCH` — MAJOR는 `SCHEMA_VERSION` 증가 등 비호환 변경, MINOR는 하위호환 기능 추가·프롬프트 변경, PATCH는 버그 수정.

번호는 **개발 패키지(WP 완료)마다, 그 내용과 기능에 맞춰** 갱신한다(2026-10-06 사용자 결정 — 이전의 '출시 단위' 규칙을 대체). 갱신 횟수가 아니라 변경 성격이 기준이다: 세션 스키마·저장 형식 비호환은 MAJOR, 하위호환 기능은 MINOR, 버그 수정은 PATCH. 같은 패키지 안의 하위 등급 변경은 상위 등급에 흡수된다. 되돌린 번호(5.34.0)는 재사용하지 않는다. 버전 값은 `core/constants.py`의 `__version__` 하나에서만 읽는다(작업 원칙 ⑦).

---

## 검증 루틴

커밋 전 세 루틴을 실행한다. 전부 `OK`여야 커밋한다.

### ① 구문 · 미정의 self 참조

```bash
python3 -c "
import ast, pathlib, re
SKIP = {'_execute_proceed', '_call_gm_logic', '_resolve_irregular_npcs',
        '_next', '_begin_flow'}   # 다른 cog·뷰의 메서드를 정상 호출
bad = 0
for f in sorted(pathlib.Path('.').rglob('*.py')):
    src = f.read_text(encoding='utf-8')
    ast.parse(src)
    defined = set(re.findall(r'(?:async )?def (\w+)\(', src))
    called  = set(re.findall(r'self\.(_\w+)\(', src)) - SKIP
    if called - defined:
        print('미정의', f, sorted(called - defined)); bad += 1
print('OK' if bad == 0 else f'{bad}건')
"
```

### ② 재수출 · 세션 필드 정합

```bash
python3 -c "
import sys, re, pathlib; sys.path.insert(0, '.')
import core
print('재수출:', [n for n in core.__all__ if not hasattr(core, n)] or 'OK')
attrs = set(re.findall(r'self\.(\w+)\s*=',
            pathlib.Path('core/models.py').read_text(encoding='utf-8')))
print('필드:', [k for k in core.SESSION_FIELDS if k not in attrs] or 'OK')
"
```

`SESSION_FIELDS`에 등록했는데 `models.py`에 없으면 로드 시 `getattr` 기본값으로 조용히 넘어가 버그를 늦게 발견한다.

### ③ 퀘스트 트리 무결 · 장소 참조

```bash
python3 -c "
import sys, json; sys.path.insert(0, '.')
import core
d  = json.load(open('scenarios/영도.json', encoding='utf-8'))
q  = json.load(open('scenarios/영도.quests.json', encoding='utf-8'))
PL = core.places.load_places(d)
err = []
for x in q['quests']:
    t = x['tree']
    # next 참조 무결
    for node, body in t.items():
        for ck, cv in (body.get('cases') or {}).items():
            if cv['next'] not in t:
                err.append(f\"{x['id']}:{node}.{ck} -> {cv['next']}\")
    # 도달 불가 노드
    reach, changed = {'root'}, True
    while changed:
        changed = False
        for n in list(reach):
            for cv in (t.get(n, {}).get('cases') or {}).values():
                if cv['next'] not in reach:
                    reach.add(cv['next']); changed = True
    if set(t) - reach:
        err.append(f\"{x['id']}: 도달불가 {sorted(set(t) - reach)}\")
    # outcome 존재
    if not any(b.get('outcome') for b in t.values()):
        err.append(f\"{x['id']}: outcome 없음\")
    # 필터가 실재 장소를 가리키는가
    for key in ('place', 'within'):
        f = x['filters'].get(key)
        vals = f.get('any', []) if isinstance(f, dict) else (f or [])
        for v in vals:
            if v not in PL:
                err.append(f\"{x['id']}: 미존재 장소 {v}\")
print(err or 'OK')
"
```

### ④ 문서 수치 정합

```bash
python3 tools/verify_docs.py          # 검사
python3 tools/verify_docs.py --fix    # 불일치 자동 반영
python3 tools/verify_docs.py --show   # 실제 값 출력 (문서 작성 참고)
```

문서를 손으로 갱신하면 반드시 낡는다. 실제로 두 문서가 v1.x 시절 수치를
그대로 달고 있었다(core 서브모듈 10개 → 실제 44개). **원칙을 지키려는
의지에 기대지 않고 기계가 잡아낸다.**

21항목을 대조한다(CLAUDE.md 11 · DEVLOG.md 10) — 버전·`SCHEMA_VERSION`·`SESSION_FIELDS`·`TRACKED_PATHS`·
서브모듈 수·장소·NPC·직업·시작 틀·프로필 단계·퀘스트. 줄 수·테스트 수는 대조하지 않으므로 손으로 갱신한다.

수치가 바뀌는 작업(장소 추가, 퀘스트 저작, 필드 신설)을 했다면
`--fix`를 돌리고 함께 커밋한다.

### ⑤ 봇 로딩 (선택)

cog 로드와 persistent view 등록까지 확인한다. 실제 로그인 없이 실행하므로 `presence.py` 경고는 무시한다.

```bash
GEMINI_API_KEY=dummy python3 -c "
import sys, asyncio, pathlib; sys.path.insert(0, '.')
import discord
from discord.ext import commands
async def t():
    bot = commands.Bot(command_prefix='!', intents=discord.Intents.default())
    for f in sorted(pathlib.Path('cogs').glob('*.py')):
        if not f.name.startswith('__'):
            await bot.load_extension(f'cogs.{f.stem}')
    print('cogs', len(bot.cogs), '| 명령어', len(bot.commands),
          '| views', len(bot.persistent_views))
asyncio.run(t())
" 2>&1 | grep -E 'cogs|Error'
```

기준값 — cogs 9 · 명령어 46 · views 5 (WP-G: `!진행`·`!수정` 은퇴로 46 → 44, `!세션이력` 추가로 45, `!세션관리` 추가로 46)

### ⑥ 시나리오 정의 검사 (H01)

```bash
python3 tools/scenario_check.py --all     # 전 시나리오 컴파일·진단(ERROR 0이어야 함)
```

원본을 고치지 않는다. 시나리오를 수정했다면 함께 돌린다.

---

## 현재 미완 항목

| 항목 | 상태 |
|---|---|
| 무협 시나리오 | `places`·`profile_creation`·퀘스트 전무 |
| 아이템 사용 판정 | 소지품 지급·증감은 되나 사용 판정 없음 |
| 난이도·대결 판정 | 계층 항목 처리 미결로 보류 |
| Cloud Billing 연동 | 자체 계산과 실청구 대조. 서비스 계정 키 필요 |
| NPC `birth_year` | 0/47명 |
| 오디오 자산 | 공통 소개 TTS·테마곡 |
| 소개 미디어 | `intro_images` 연결부만 있고 자산 없음 |
| 디스코드 결제 | SKU 한국 미지원. `!지급`·`!잉크`로 대체 |
| v7.x 반영 | `SCHEMA_VERSION` 4·turn-history v3·CommitPlan v2(7.1.0·7.2.0도 유지). 배포 전 `sessions/`·`accounts/` 백업. 기존 세션은 로드 시 자동 마이그레이션되며, 배포 후 6.x로 되돌리면 plan v2 PREPARED 복구를 읽지 못한다 |
| 영도 DYNAMIC 저작 | H07 V2 저작 완료(사건 64·정보 146·일과 35·`unknown_name` 8·구간 230쌍) — 남은 판단 항목(OPEN DECISION)은 `handoff/H07_FINAL_IMPLEMENTATION_REPORT.md` |
| 운영 반영 | v7.12.0이 main에 반영됨(`8f4b3f6`), v7.13.0 main 반영(`3329ce8`), v7.14.0~v7.17.3 main 반영(`c6b374b`, #7 — v7.14 `prompts.py` 변경: **열린** 세션은 `!캐시 재발급` 또는 닫기→열기. v7.15의 프롬프트 변경은 프로필 검증·병합 지시문뿐이라 재발급 불필요, 영도 `profile_creation` 가이드는 새로 시작하는 캐릭터 생성부터). v7.12.0 배포 뒤 LEGACY 열린 세션은 `!캐시 재발급`(시나리오 `stat_system` 문구 정정), 영도 DYNAMIC은 새 세션부터 새 룰북 — 기존 세션은 지시층위 판정 블록의 '코드 판정 권위' 문구로 정합. `!배포`는 v7.10.1부터 requirements 변경 시 pip를 실행한다 — 그 이전 코드로 실행하는 첫 배포는 서버에서 `/home/ubuntu/trpgbot/venv/bin/python -m pip install -r requirements.txt`를 직접 실행해야 한다. 배포 전 운영 `sessions/`·`accounts/` 백업. 영도 `speaker_card`·`plain_speakers`(v7.6~7.8)는 DYNAMIC 바인딩 스냅샷이라 **새 영도 세션에만** 반영된다(재발급 불필요·무효). SVG 래스터화(화자 카드·디스플레이 상태판)는 `resvg_py`(requirements 고정) 또는 resvg CLI와 `Noto Sans CJK KR` 폰트가 필요하다(없으면 카드는 생략, 상태판은 Embed 대체). 구조 로그는 세션 `logs/`·전역 `logs/bot.jsonl`에 쌓이며 옛 텍스트 로그 파일은 더 이상 쓰지 않는다 |
| 자동 턴 TTS | v7.11부터 인트로와 자동 턴 묘사를 더빙한다(`tts_enabled`·음성 연결 시). 자동 턴의 TTS 비용은 그 턴의 비용 멤버로 claim되어 Settlement가 청구한다. NARRATE·ASK는 더빙하지 않는다(v7.18: 후속 묘사 은퇴). TTS usage는 오디오 가공 전 기록, 타임아웃 뒤 늦은 응답은 운영 사실(이미 동결된 Settlement 불변) |
| H07 운영 활성 | 영도 단일 PC 세션은 DYNAMIC(시작 틀 `world_start` 저작), 나머지 4개 시나리오·다인 세션은 LEGACY. 프롬프트 변경은 열린 세션만 재발급, 영도 정의 변경은 새 세션에만 반영(배포 절 '캐시 재발급이 필요한 경우') |
| H06 배포 | 새 영속 필드 `world_lifecycle`·`world_initialization`(기본값 = 레거시). 스키마·이력·CommitPlan·compiler 유지. DYNAMIC 대상 세션은 캐시 업로드가 시작 선택 뒤로 이동 |
| H07 프롬프트 | `prompts.py` 4개 층위에 동적 세계 조건부 권위 절 추가(H07-D). 추출 스키마의 레거시 `quest_progress`·`secret_awareness`는 DYNAMIC에서 쓰이지 않지만 스키마에 남아 있다(토큰 낭비 — 남은 한계) |
| H05~H07 신규 관찰 결함 | 전부 해소 — ③ v7.28(열기 재시도 승계)·④ 은퇴 안내 분기·⑥ v7.23(DYNAMIC 시뮬레이션 해체)·① ② ⑤ v7.33(초기화 실패 동안 참가 차단·LEGACY 업로드 실패 시 인트로 보류·`!이미지 생성`은 원본 `media_keywords`만 수정) |
| v7.17 반영 | `prompts.py`(추출 item_ops 규칙·초기 지식 지시문) — **열린** 세션은 `!캐시 재발급` 또는 닫기→열기. 영도 초기 지식 2·남항 교환 지급(`payment`·`OBSERVER_PAID_ITEM`)은 DYNAMIC 바인딩 스냅샷이라 **새 영도 세션에만** 반영 |
| v7.17.2 소속 접근 | 시나리오 선택 키 `faction_capabilities`(사건 `grant_faction` 값 → 접근 capability). PC 접근 판정만 RESOLVED 가입 사건 전체에서 파생 — v7.17.3: 매핑에 선언된 토큰은 같은 이름 소지품으로 만족되지 않음(표시용 `derived_player_faction`·`player_faction` 미러는 근거 아님, 해제 정본 없음 — 되감기로만 사라짐). 영도 남항·해련·조도 매핑은 새 영도 세션에만 반영 |
| v7.18 반영(P1) | `prompts.py`·시나리오 불변 — 판단·지시·묘사·추출의 P1 지시는 런타임 사용자 프롬프트 블록·스키마 사본이라 캐시 재발급 불필요. 새 되감기 필드 `world_pending_signals`(기본 [])는 로드 시 기본값. 외부 청구 대조(GCP Cloud Billing BigQuery 내보내기)는 미구성 — 내부 원장·정산·보고·로그 정합만(보고서 참조) |
| v7.19 반영(P2) | `prompts.py`·시나리오 불변 — 재발급 불필요. 새 세션 필드 `master_gender`(기본 male = 기존 나레이터 목소리). TTS 스타일은 런타임 지시. SVG 화자 카드 로딩 임계값은 환경변수 `SVG_LOADING_THRESHOLD_SEC`(기본 1.5초). 마스터 원화 6슬롯은 미제작이 정상 상태(공용 로딩 이미지) — 원화를 `assets/master/`에 두고 manifest에 파일명을 적으면 반영. NPC 목소리 기록 `data/voice_assignments.json`은 운영 산출물(커밋 제외) |
| v7.20 반영 | `prompts.py` 지시층위 시스템 지시 변경(캐시에 구워짐) — **열린** 세션은 `!캐시 재발급` 또는 닫기→열기(구 캐시도 새 사용자 프롬프트와 호환되나 새 지침은 재발급 뒤 적용). 시나리오·세션 필드 불변. 같은 턴 이름·얼굴 표시는 추출의 공개 인용이 본문에서 확인될 때만 — 확인 실패 시 `???` 유지, 그 턴 정본 인지도 생기지 않음 |
| v7.21 반영(P3 로그) | `prompts.py`·시나리오·세션 필드 불변 — 재발급 불필요. 코드 재시작만. `!세션이력` zip에 `_index/manifest.json`·`LOG_README.md` 동봉, 한도 초과 jsonl은 줄 경계 조각(`.partNNN.`). 기존 로그 줄(`b`·`seq` 없음)도 `log_view`로 읽힌다. 세션 통계 결함(영구 0 카운터·주사위 집계 시점)은 별도 과제(D2) |
| v7.21.1 반영 | 코드 재시작만(프롬프트·시나리오·세션 필드 불변, 재발급 불필요). 기존 세션의 대기 행(`carried` 있음·`exposes` 없음)은 다음 커밋된 PROCEED 본문에 1회 전달 후 소멸(노출 후보 없음) |
| v7.21.2 반영 | 마스터 원화 6슬롯(`assets/master/*.webp`) — 재시작(또는 `core.master_art.reload()`)만으로 반영. 진행자 남/여(`disp:master_gender`)에 따라 같은 톤의 원화가 나온다 |
| v7.21.3 반영 | 코드 재시작만. 봇에 게임 채널 `메시지 관리` 권한이 있어야 답변이 지워진다(없으면 남고 진행은 정상) |
| v7.21.4 반영 | 코드 재시작만. 재시작 전에 디스플레이로 시간 질문을 받아 둔 생성 중 세션은 재시작 뒤 [⏱️ 시간 입력]을 다시 누르면 게임 채널에서 받는다 |
| v7.21.5 반영 | 마스터 대사 데이터만 변경 — 재시작만(캐시 재발급 불필요) |
| v7.33 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변). 묘사 이전 실패(예외·판단/지시층위 실패)는 청구 0으로 끝나고 플레이어에게 재선언 안내가 나간다. 주사위 화면이 열려 있거나 판정 결과를 보관 중이면 새 선언을 받지 않는다(보관 결과는 [판정 결과로 이어서 진행] — 재시작하면 사라지고 재선언 안내). 손상 계정은 '계정 확인 필요'로 막힌다 |
| v7.32 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변). `!세션관리`에서 여러 세션을 골라(페이지·상태별 일괄 선택) 한 번의 확인으로 정리한다 — 막힌 세션은 건너뛰고, 진행 중 제거는 원래 범위로 재개 |
| v7.31 반영 | 영도 시나리오(감염자 고정 카드)·자산·코드 — 코드 재시작만. 영도 정의는 바인딩 스냅샷이라 **새 영도 세션에만** 감염자 카드가 나온다(기존 세션은 원문 표기·이미지 없음 그대로). `prompts.py`·세션 필드 불변 |
| v7.30 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변). 마스터 채널에 매 턴 비용·층위활동·세계 델타 임베드가 나간다(이전보다 메시지 수 증가). 예측·실제 대조는 운영 산출물 `data/cost_estimate_accuracy.jsonl`(커밋 제외) — 같은 시나리오·모드 10턴부터 되먹임. 기존 세션은 입력 분류 표본 3턴 뒤부터 확정/유력/가변 범위를 쓴다 |
| v7.29 반영 | 코드 재시작만(`prompts.py`·세션 필드 불변). 영도 2편 보관함(닫힌 상자)은 바인딩 스냅샷이라 **새 영도 세션에만** — 기존 세션(cf916fc5 등)은 옛 정의 그대로지만, 물건 연산이 거부돼도 교정 재추출·물건 연산 제외로 턴이 막히지 않는다(재시도 버튼으로 같은 턴 재개) |
| v7.28 반영 | 코드 재시작만(`prompts.py`·시나리오 불변). 새 세션 필드 `resumable_prompts`(기본 {}). 이번 배포 **이전**에 띄워져 있던 진행 화면은 기록이 없어 재시작 뒤 다시 붙지 않는다(누르면 만료 안내) — 세션 생성 단계 화면은 `creation_state`로 다시 그려진다. 이후 화면부터 승계된다 |
| v7.27 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변). GM 스페이스 카드는 다음 보드 갱신(세션 종료·[🔄 보드 갱신]·홈 갱신) 때 이미지로 바뀐다. 래스터화는 상태판과 같은 `resvg_py` + `Noto Sans CJK KR` — 없으면 기존 Embed로 대체 |
| v7.26 반영 | 영도 시나리오(시작 틀 6·시작 장면 인물 사건 `yd_start_*` 5·`item_definitions` 배 2종·`scenario_intro` 한 문단)와 코드(`world_start.items`) — 코드 재시작만. 영도 정의는 DYNAMIC 바인딩 스냅샷이라 **새 영도 세션에만** 반영(재발급 무효). `prompts.py`·세션 필드·스키마 불변. `scenario_intro` 사전 합성 TTS가 있으면 `!tts생성`으로 다시 만들어야 새 문장이 나온다. 시작 장면 인물은 기존 NPC(이선희·진복례·류가은·신대호·백인하)이며 시작 첫 분에만 사건이 배치하고 일을 마치면 걸어서 일과로 돌아간다 |
| v7.25 반영 | `prompts.py`(묘사 §8·추출 동행·지시층위 레거시 조항·압축 헤더)·캐시 본문(`core/cache.py` NPC 사전·상태창·기억 문구) 변경 — 묘사 시스템 지시와 지시층위 지시가 캐시에 구워짐 → **열린** 세션은 `!캐시 재발급` 또는 닫기→열기(구 캐시도 동작하나 새 문구는 재발급 뒤). 시나리오·세션 필드·스키마 불변. 문자 절단 제거로 지시층위 입력이 늘었다(실측 근거 추정 1회 +4천 자 내외). 동행 배치·세션 NPC 등록은 기존 typed 연산(ACTOR_PLACEMENT_SET/TRANSIT_SET/CLEAR·ACTOR_REGISTER)으로 같은 턴 커밋에 들어간다. v7.25 이전에 승격된 세션 NPC는 소급 등록하지 않는다 |
| v7.24 반영 | `prompts.py` 변경 — 판단 시스템 지시(캐시 미사용, 재시작만으로 반영)·지시층위 시스템 지시(캐시에 구워짐 → **열린** 세션은 `!캐시 재발급` 또는 닫기→열기). 시나리오·세션 필드 불변. LEGACY 호환은 요구하지 않는다(사용자 결정) — LEGACY 판단도 같은 지시를 받으며 게이트가 판단 앞이라 판정 집행 절이 적용된다 |
| v7.23 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변 — DYNAMIC 판단·지시 블록은 런타임 사용자 프롬프트·스키마 사본, 재발급 불필요). DYNAMIC만 — LEGACY 시뮬레이션·게이트·🔮 불변. 새 원장 op `TURN_DECLARATION_CHECK`(플레이어 부담 후보, 턴 Settlement 귀속) |
| v7.22 반영 | 코드 재시작만(`prompts.py`·시나리오·세션 필드 불변 — 출구 후보·이동 규칙은 런타임 판단 사용자 프롬프트·스키마 사본, 재발급 불필요). DYNAMIC(영도 단일 PC)만 — LEGACY 불변. 목적지 질문 문맥은 대기 시도 런타임 기록이라 재시작하면 사라지고 다음 '이동한다'에 다시 묻는다 |
| H05 calibration | 경로 자동 선택(10분 미만·비용비 5/4)·추가 시간 상한(240분)·PC capability = 보유 소지품 이름 — 영도 정량 저작 후 조정 |
