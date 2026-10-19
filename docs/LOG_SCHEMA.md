# INDAIM 로그 스키마 (log_schema 2)

이 문서는 `!세션이력` zip의 `{세션ID}/_index/LOG_README.md`로 그대로 동봉된다. 단일 출처는 저장소 `docs/LOG_SCHEMA.md`, 코드 owner는 `core/logs.py`(kind 등록부 `DOMAIN_KINDS`).

독자는 오너가 받은 zip을 읽는 **AI 개발 에이전트**다. 목적은 디버그·개발·작동 사실 파악이다. 로그는 **관찰 기록**이며 정본·복구·재무 권위가 아니다. 권위 파일은 아래 '권위 파일'에 있다.

## 1. 묶음 구조

```
{sid}/_index/manifest.json   묶음 자기 서술(아래 6절)
{sid}/_index/LOG_README.md   이 문서
{sid}/data.json              세션 스냅샷(정본, 마지막 저장 시점)
{sid}/logs/calls.jsonl       provider 호출 1건 = 1줄
{sid}/logs/blobs.jsonl       내용 조각 저장소 {h, t}
{sid}/logs/events.jsonl      내부 처리·경고·오류·print 포착
{sid}/logs/domain.jsonl      정본·재무·표시·이력 사실(kind별)
{sid}/logs/chat.jsonl        게임·마스터 채널 기록
{sid}/...                    권위 파일(아래)
```

- **분할 묶음**: 묶음은 9.5MB 단위 zip 여러 개(`session_{sid}_partIofN.zip`)일 수 있다. 전부 받아 함께 읽는다.
- **분할 파일**: 한 파일이 한도를 넘으면 줄 경계 조각으로 나뉜다. 형식은 `이름.partNNN.확장자`(예: `logs/calls.part001.jsonl`)다.
  - NNN 순서로 이어 붙이면 원본과 바이트 단위로 같다.
  - `manifest.files[].parts`에 조각 목록이 있다.
- 줄 기반이 아닌 파일이 한도를 넘으면 제외된다(`manifest.excluded`).

## 2. 공통 필드 (세션 로그 모든 줄)

| 필드 | 의미 |
|---|---|
| `ts` | epoch 초(소수 3자리). 시간대는 manifest `tz` |
| `b` | 프로세스 부팅 id(6 hex). 재시작마다 바뀐다 |
| `seq` | 부팅 안에서 단조 증가하는 순번. 파일이 달라도 같은 `b` 안에서는 `seq`가 전순서다 |
| `tx`, `turn` | 기록 시점에 진행 중이던 자동 턴 트랜잭션 id와 logical_turn(없으면 생략) |

정렬 규칙:
- 같은 `b` 안에서는 `seq` 순서다.
- 서로 다른 `b`는 `ts` 순서다.
- 같은 `b` 안에서는 `ts`가 같아도 `seq`로 판별한다.

## 3. 조각(blob) 복원

텍스트는 줄(`\n`) 단위 조각 목록으로 저장된다.

- 각 조각은 둘 중 하나다.
  - 원문 문자열: 16자 미만
  - `"#" + sha1 앞 12 hex`
- `#해시`는 `blobs.jsonl`의 `{h, t}`에서 `t`로 바꾼다.
- 복원한 조각을 `"\n"`으로 이어 붙이면 원문이다.
- 해시 형식이 아닌 `#…` 짧은 문자열은 원문이다.
- `domain`·`events`의 `data` 안에 있는 500자 이상 문자열은 `{"$b": "#해시"}`로 바뀌어 있다.
- 같은 조각은 세션·부팅당 한 번만 저장된다. 따라서 재시작 뒤 같은 해시가 다시 나올 수 있다(값은 같다).

## 4. 파일별 필드

### calls.jsonl
| 필드 | 의미 |
|---|---|
| `call` | 호출 1건 id(12 hex) — `turn.summary.calls`가 참조 |
| `layer` | 층위(judgment·instruction·narration·extraction·compression·media, 미지정 `unlabeled`). 같은 층위 안 작업 구분은 `op` |
| `op` | 비용 오퍼레이션 종류(CostLedger operation) |
| `op_id` | 비용 오퍼레이션 id — `cost.event.operation_id`와 같다 |
| `attempt` | 재시도 회차(`call_with_retry`) |
| `model`, `ms`, `status`(ok/error), `exc`, `finish` | |
| `usage` | provider 원본 `{in, cached, out, thought, total}` — 과금 출력 = out + thought |
| `cache` | 사용한 캐시 이름 |
| `system`, `schema` | 시스템 지시문·응답 스키마 조각 참조 |
| `think_budget` | 사고 예산 |
| `req` | `[{role, parts: [조각 목록, …]}]` |
| `resp` | 응답 텍스트 조각 목록 |

### events.jsonl
| 필드 | 의미 |
|---|---|
| `lv` | DEBUG·INFO·WARN·ERROR |
| `area` | 영역. `turn` = 턴 흐름 구조 이벤트(아래), 그 밖은 모듈 영역 |
| `msg` | 짧은 설명 |
| `data` | 구조 데이터 |
| `exc` | 예외 요약 |
| `trace` | ERROR일 때만, 스택 추적(최대 6000자) |
| `src: "print"` | 세션 id가 찍힌 print 출력의 원문(`msg`). `area`는 줄 머리 `[영역/…]`에서 뽑은 값 |

`area="turn"` 이벤트의 `data`:
- 진행 단계는 `stage`로 구분한다.
  - `judgment`: iter, action, reasoning
  - `ask`: tx, bridge
  - `narrate`: tx, n, text
  - `post_roll`: action, reasoning
- 실패·차단은 `code`로 구분한다.
  - `PREP_IN_FLIGHT`
  - `WORLD_BLOCKED`
  - `PREP_EXCEPTION`
  - `READY_REJECTED`(reasons)
  - `JUDGMENT_JSON`(attempt)
  - `DIRECTOR_JSON`(raw)
  - `NARRATE_CALL`
  - `NARRATION_EMPTY`
  - `PROCEED_VIOLATION`(detail)
  - `SIMULATION_JSON`
  - `PLAN_JSON`(raw)

### chat.jsonl
- `ch`: 채널 종류
- `who`: 화자
- `text`: 500자 이상이면 조각 목록, 아니면 원문

### domain.jsonl
- 형식: `{kind, data}`
- kind 목록은 `core.logs.DOMAIN_KINDS`와 같다.

| kind | 기록 시점 | data 주요 필드 |
|---|---|---|
| `session.meta` | 세션 생성·복구·genesis·캐시 발급 | reason(create/restore/genesis/cache), boot, git_sha, version, log_schema, prompts_digest, scenario_id, definition_revision, world_lifecycle, players, turn_count, gm_turns_done, cache_name, cache_model, default_model |
| `tx.begin` | 자동 턴 시도 시작 | tx, turn, attempt, declaration |
| `tx.status` | 상태 전이 | tx, from, to, stage, code, message |
| `turn.summary` | 시도가 대기(WAITING_FOR_PLAYER·WAITING_FOR_ROLL) 또는 종결(COMMITTED·FAILED_SYSTEM·ABORTED·SUPERSEDED)에 이를 때 | 아래 표 참조. 값이 빈 키는 생략 |
| `commit` | 정본 커밋 | tx, turn, attempt, story_turn, gm_turn, settlement_id, charge_ink_per_user, billing_users, cost_events, world_version, definition_revision, world_ops |
| `turn.delta` | 커밋 이력 레코드 저장 | tx, gm_turn, story_turn, attempt, settlement_id, diff(pre→post 가역 상태 차이) |
| `history.selection` | 선택 인덱스 변화 | 아래 표 참조 |
| `history.rewind` | 되감기 결과 | target_gm_turn, ok, reason, removed_turns |
| `history.rerender_begin` | 재서술 시작 | gm_turn, replaces_tx, new_tx, addendum |
| `history.rerender_abort` | 재서술 중단 | ok |
| `history.rerender_settle` | 재서술 종결 | action… |
| `cost.event` | CostLedger 사실 | event_id, op, operation_id, idempotency_key(`{operation_id}:attempt:{n}`), tx, turn, attempt, actor, actor_kind, billing_hint, in, cached, out, thought, out_billed, audio_out, krw, usd… |
| `cost.usage` | 호환 미러 누적 | context, in, cached, out_billed, krw, usd, session_total_krw |
| `cost.estimate` | 턴 예상 | tx, min/max ink·krw, basis, confidence, breakdown, tts |
| `cost.unknown` | 사용량 미상 | op, operation_id, attempt, tx, model, reason |
| `ink.charge`, `ink.lifecycle` | 잉크 계정 효과(턴 Settlement / 캐시 생애주기·해석) | reference·kind·user·nominal_ink + 결과 또는 error |
| `display` | 바뀐 디스플레이 상태판 | reason, 바뀐 항목, render |
| `card.speaker`, `card.render` | 화자 컨텍스트·SVG 카드 | speaker, actor, header, status / variation, knows |

`turn.summary` data:

| 필드 | 의미 |
|---|---|
| `status` | 이 줄이 기록된 시점의 상태 |
| `partial` | 대기 중 요약이면 true. 이 경우 같은 tx의 종결 요약이 뒤에 또 온다 |
| `tx`, `turn`, `attempt`, `declaration` | 정체·입력 |
| `failure` | {failure_stage, failure_code, failure_message} |
| `dur_ms` | 생성부터 지금까지 소요 시간 |
| `stages_ms` | 상태별로 처음 도달한 시각(생성 기준 ms). 같은 프로세스 안에서만 |
| `judgment` | {action, player_intent, rolls(개수), roll_results, gate{classification, resolution, target, reason}} |
| `world` | DYNAMIC 턴만 있다(없으면 LEGACY 턴 — 모드 판정은 단일 gate 정책상 로그가 따로 하지 않는다). {intent(의미 기록), plan{status, reason, outcome, minutes, start_minute, end_minute, scene_place, signals}, tie_choice, identified_names/faces, observation{status, exposures, represented, pending_after, diagnostics}} |
| `prep` | {phase, failure_code/stage, violations, narration{chars, paragraphs}, extraction, identity_reveals, world_ops{op_type: 개수}, cost_events, delivery_ok} |
| `calls` | 이 tx에서 기록된 `[layer, call id]` 목록(같은 프로세스 안) |

`history.selection` data:

| reason | 필드 |
|---|---|
| `SELECT` | gm_turn, tx, attempt, superseded_tx(같은 gm_turn에서 밀려난 시도), record |
| `REWIND` | head_after, deselected{gm_turn: tx} |

## 5. 조인 키

| 묻는 것 | 잇는 법 |
|---|---|
| 턴 N에서 무슨 일이 있었나 | `tx`로 잇는다: `tx.begin` → `tx.status`… → `turn.summary` → `commit` → `turn.delta`. 같은 tx의 events·calls·chat도 이어 붙인다 |
| 그 턴의 호출 원문 | `turn.summary.calls[].call` → `calls.jsonl.call` |
| 호출과 비용 | `calls.op_id` == `cost.event.operation_id`. 시도까지 맞추려면 `calls.attempt`를 `idempotency_key`의 `:attempt:{n}`과 대조한다 |
| 청구 | `commit.settlement_id`·`commit.cost_events` → 권위 파일 `turn_settlements.jsonl`, `data/cost_ledger.jsonl` |
| 현재 이야기 가지 | `manifest.selection.selected`(gm_turn → tx). 시간축 변화는 `history.selection`. `unselected_tx`는 재서술·되감기로 버려진 시도다 |
| 어떤 코드였나 | `session.meta`(git_sha, version, prompts_digest, boot). 줄의 `b`가 `session.meta.boot`와 같으면 그 코드로 기록됐다 |

## 6. manifest.json

| 필드 | 의미 |
|---|---|
| `manifest_version`, `log_schema`, `session_id` | |
| `generated_at` | ISO 시각(+09:00) |
| `tz` | 시간대 |
| `packer` | 묶음을 만든 프로세스의 code_revision |
| `session` | data.json 주요 필드와 players 수 |
| `last_session_meta` | 마지막 `session.meta` |
| `time_range` | 로그 첫·끝 ISO 시각 |
| `selection` | {head_gm_turn, rewinds, selected{gm_turn: {tx, logical_turn, attempt}}, unselected_tx} |
| `files` | [{path, size, lines, parts}] |
| `excluded` | [{path, size}] |

용량 상한은 없다. 크기는 `files[].size`로 관찰한다.

## 7. 권위 파일 (로그가 아님)

| 파일 | 내용 |
|---|---|
| `turn_history.jsonl` + `turn_history/` | 선택 인덱스와 턴별 pre/post 레코드 |
| `turn_commit_journal.jsonl` | 커밋 단계 |
| `turn_settlements.jsonl` | 턴 청구 |
| `cache_lifecycle.jsonl`, `interpretation_billing.jsonl` | 캐시 창·해석 청구 |
| `canonical_mutation_journal.jsonl`, `definition_snapshot.json` | DYNAMIC 정본 변이·정의 |
| `full_logs.jsonl` | 대화 원본 |
| `rewind_log.jsonl`, `rewind_archive.jsonl` | 레거시 비권위 델타·제거 정보 |

로그와 권위 파일이 어긋나면 권위 파일이 옳다. 그 어긋남 자체가 결함 신호다.

## 8. 전역 로그 `logs/bot.jsonl` (서버, zip에 없음)

- 세션 밖 이벤트와 print를 담는다.
- 세션 이벤트는 위치만 남는다(`sid`·area·msg, `data`·`trace` 없음).
- 세션 id가 찍힌 print는 `<세션 … 출력 N자>`로 대체되고, 원문은 세션 events에 있다.
- 부팅마다 `area="boot"` 줄에 code_revision을 남긴다.
