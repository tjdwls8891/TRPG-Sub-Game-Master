# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

**INDAIM** — Gemini API + discord.py 기반의 한국어 TRPG 보조 GM 디스코드 봇.

AI가 4개 층위로 나뉘어 판단·지시·묘사·추출을 분담한다. 플레이어는 서버 GM 스페이스에서 버튼으로 세션을 열고, 캐릭터를 만들고, 턴을 진행한다. **명령어는 GM의 수동 개입·복구 수단이며 일반 플레이에는 쓰지 않는다.**

현재 버전: **v5.25.0** · `SCHEMA_VERSION` 3 · 총 ~23,800줄

---

## 실행 및 환경

```bash
pip install -r requirements.txt
python main.py
```

`.env` 필수 변수:
- `DISCORD_TOKEN` — 디스코드 봇 토큰
- `GEMINI_API_KEY` — Gemini API 키
- `TRPG_INTRO_TEXT` — `!소개`에 포함되는 공통 인트로

### 배포

운영 서버(Oracle Cloud, Ubuntu ARM, systemd `trpg-bot.service`)에서:

```
!배포 재시작      # git pull + 재시작
!재시작           # 재시작만 (오너 전용)
!캐시 재발급      # prompts.py·시나리오 변경 시 필수
!리로드 [모듈]    # cogs/ 하위만 핫스왑 가능
```

`core/`와 `main.py`는 핫스왑이 불가하다.

---

## 아키텍처 — 4층위 파이프라인

층위를 나눈 이유는 **비용과 정확도의 분리**다. 판단은 싸게, 묘사는 좋게, 수치는 코드가.

```
플레이어 선언
   ↓
[판단층위]  유형 결정 (ASK / NARRATE / ROLL / PROCEED)
   │        캐시를 읽지 않는다. 맥락을 사용자 프롬프트로 직접 공급.
   │        ASK·ROLL은 여기서 끝난다 (캐시 0회 → 가장 저렴)
   ↓
[지시층위]  묘사 지시문 작성 (NARRATE·PROCEED만)
   │        캐시를 읽는다. 세계관·금지사항·정보 접근을 반영.
   ↓
[묘사층위]  실제 출력. 스트리밍으로 게임 채널에 송출.
   ↓
[추출층위]  수치·상태 추출 → 코드가 임계값과 대조해 적용
            사고 예산 제한 (기계적 판독이므로)
```

### 설계 원칙

**① 판단은 수치로, 임계는 코드로**
모델에게 "위험한가?"를 묻지 않는다. 0~100 점수를 받아 코드가 임계값과 비교한다. 같은 상황에 같은 결과가 나오게 하기 위함이다.

**② 온디맨드 주입**
장소·퀘스트·NPC 정보는 캐시에 굽지 않고 턴마다 필요한 것만 주입한다. 캐시는 시나리오 룰북(고정분)만 담는다.

**③ 시나리오 JSON이 정의, 코드는 기능만**
스탯 체계·프로필 알고리즘·장소 그래프·퀘스트 트리가 모두 데이터다. 영도와 무협이 전혀 다른 체계를 가질 수 있다.

---

## 파일 맵

### 최상위

| 파일 | 역할 |
|---|---|
| `main.py` | `TRPGBot`, `active_sessions`, cog 자동 로드, 세션 디스크 복구 |
| `prompts.py` (1,660줄) | 시스템 지시문 + 응답 스키마 12종 |
| `scenarios/*.json` | 시나리오 데이터 (영도·무협·다크판타지) |

`prompts.py`와 `scenarios/*.json`은 **임의 수정 금지**. 수정안을 제시하고 승인받은 뒤 적용한다.

### cogs/ — 명령어 계층

| 파일 | 줄 | 명령어 |
|---|---|---|
| `gm.py` | 3,845 | `!자동` 그룹, `!되감기` — 4층위 파이프라인 본체 |
| `game.py` | 1,443 | `!진행` `!재생성` `!출력물` `!수정` `!주사위` `!기억압축` `!노트` `!캐시노트` `!더빙테스트` |
| `character.py` | 1,249 | `!참가` `!설정` `!증감` `!외형` `!프로필` `!엔피씨` `!능력치` `!설정생성` `!캐릭터가져오기` |
| `media.py` | 733 | `!이미지` `!브금` `!플리` `!볼륨` `!채팅` `!더빙` |
| `system.py` | 686 | `!명령어` `!배포` `!재시작` `!지급` `!스페이스` `!스페이스초기화` `!캐시` `!리로드` `!세션종료` `!채널정리` `!tts생성` |
| `session.py` | 639 | `!새세션` `!시작` `!소개` + `JoinView`(참가 버튼) + `on_voice_state_update` |
| `errors.py` | 154 | 명령어 오류 핸들러 |
| `permissions.py` | 98 | `!권한부여` `!권한회수` `!권한목록` |
| `presence.py` | 67 | 상태 메시지 순환 |

### core/ — 45개 서브모듈

`core/__init__.py`가 전 심볼을 re-export하므로 외부에서는 `core.XYZ`로 접근한다.

#### 기반

| 모듈 | 줄 | 내용 |
|---|---|---|
| `constants.py` | 143 | `DEFAULT_MODEL`, `LOGIC_MODEL`, `IMAGE_MODEL`, `EXCHANGE_RATE`, `PRICING_1M`, `TTS_VOICES`, `__version__` |
| `models.py` | 237 | `TRPGSession` — 단일 세션의 모든 상태를 담는 중앙 컨테이너 |
| `io.py` | 396 | `SCHEMA_VERSION`, `SESSION_FIELDS`(74), `migrate_session_data`, 직렬화·로그·시나리오 로드 |
| `cache.py` | 486 | 룰북 캐시 빌드, 캐시 상태 동기화, 세션 디스크 복구 |
| `resilience.py` | 107 | `call_with_retry` — 재시도·타임아웃·오류 로그 분리 |

#### 프롬프트·AI

| 모듈 | 줄 | 내용 |
|---|---|---|
| `prompt.py` | 332 | `PromptBuilder` — 체이닝으로 블록을 조립 |
| `extraction.py` | 438 | 추출층위 스키마·파싱·적용, 동행 갱신 |
| `dialogue.py` | 341 | `@대사:이름\|본문` 파싱, 인물 이미지 자동 송출, 스트리밍 |

#### 비용·결제

| 모듈 | 줄 | 내용 |
|---|---|---|
| `cost.py` | 390 | 토큰 단가 계산, 캐시 저장비 합산 |
| `estimate.py` | 418 | 턴·세션오픈·TTS 예상 비용 |
| `ink.py` | 85 | 게임머니 '잉크' 환산 (1잉크 = 10원) |
| `accounts.py` | 186 | 계정 등록, 약관 동의 버전, 잉크 잔액 |
| `terms.py` | 191 | 약관 동의 DM 인터페이스 |
| `stats.py` | 196 | 누적 플레이 기록 (계정과 분리) |

#### 세션 생성

| 모듈 | 줄 | 내용 |
|---|---|---|
| `session_flow.py` | 444 | **기획서 17단계 통합 플로우** — 상태 기계 구동 |
| `creation.py` | 176 | 단계 상태 기계 (kind→private→intro→scenario→tts→memory→profile→open→start) |
| `session_open.py` | 134 | 유지 시간 입력 해석 → 분 환산 |
| `gm_space.py` | 415 | 서버 GM 홈·명예의 전당·월드보드 |

#### 프로필

| 모듈 | 줄 | 내용 |
|---|---|---|
| `profile_gen.py` | 668 | 생성 모듈 10종 + 능력치 등급·랜덤 배분 |
| `profile_runner.py` | 347 | 시나리오 알고리즘 해독·실행 |
| `profile_creation_ui.py` | 526 | 풀오토 생성 UI (버튼·셀렉트·모달) |
| `profile_ai.py` | 178 | AI 검증·병합 (무료 제공) |
| `profiles.py` | 232 | 사전 저장 프로필 (`profiles/{uid}.json`) |
| `profile_ui.py` | 417 | 사전 프로필 관리 DM |

#### 세계

| 모듈 | 줄 | 내용 |
|---|---|---|
| `places.py` | 401 | **장소 계층 그래프** — 이동 개연성, 가시성, 이미지 상속 |
| `quest.py` | 749 | 퀘스트 시스템 — 트리 진행, 이면정보, 메인 해금 |
| `quest_filter.py` | 306 | **필터 매칭 — 통과한 값이 곧 슬롯** |
| `start_frame.py` | 239 | 시작 상황 틀 |
| `timeline.py` | 184 | 작중 시간 정량화, 나이 계산 |
| `growth.py` | 215 | 능력치 성장·행운 스탯 |
| `irregular_npc.py` | 230 | 비정규 NPC 이미지·목소리 배정 |
| `koreantext.py` | 144 | **슬롯 치환·조사 보정** (틀·퀘스트 공용) |

#### 기억

| 모듈 | 줄 | 내용 |
|---|---|---|
| `memory_plan.py` | 202 | 압축 플랜 4종 (노멀·하이·로우·울트라) |
| `rewind.py` | 340 | 되감기 델타 로그 — append-only 기록·역순 복원 |

#### 미디어·UI

| 모듈 | 줄 | 내용 |
|---|---|---|
| `display.py` | 519 | **디스플레이 채널** — 상태 표기와 UI를 단일 메시지로 |
| `audio_mixer.py` | 393 | BGM/플리(base) + 효과음(effects) + 음성(voice) 합산 |
| `tts.py` | 141 | Gemini native TTS → 48kHz stereo PCM |
| `tts_preset.py` | 256 | 시스템 문구 사전 합성 (런타임 API 0회) |
| `media.py` | 149 | 이미지 키워드 전송, `PlaylistManager` |
| `media_control.py` | 153 | 상황 기반 BGM 선택, 미디어 토글 |
| `ui.py` | 293 | 주사위 뷰, 채널 삭제 뷰 |
| `chat_guard.py` | 82 | 채널별 권한 검증 (봇 레벨 단일 훅) |
| `utils.py` | 283 | 캐릭터 검색, AI 설정 생성 |

---

## 데이터 흐름

### 세션 저장

```
sessions/{session_id}/
├── data.json              세션 스냅샷 (원자적 쓰기: tmp에 PID+나노초)
├── api_log.txt            층위별 요청·응답
├── game_chat_log.txt      게임 채널 대화
├── master_chat_log.txt    마스터 채널 대화
├── cost_log.txt           비용 내역
├── error_log.txt          오류
├── full_logs.jsonl        전 턴 대화 원본
├── rewind_log.jsonl       턴별 델타
└── rewind_archive.jsonl   되감기로 제거된 정보
```

세션 외부: `profiles/{uid}.json` · `accounts/{uid}.json` · `stats/{uid}.json`

### `data.json` 2계층

**① 명시 직렬화 15키** — 구조가 특수하거나 변환이 필요한 것
`schema_version`·채널 id·`players`·`npcs`·`resources`·`statuses`·`raw_logs`(파트 단위 변환) 등

**② `SESSION_FIELDS` 일괄 73키** — `getattr` 반복

### 스키마 마이그레이션

`SCHEMA_VERSION` 3. 필드명이 바뀐 버전은 `migrate_session_data`가 로드 시 자동 변환한다.

```python
FIELD_MIGRATIONS = {2: {"auto_gm_active": "gm_active", ...}}
```

---

## 시나리오 데이터 구조

### 영도 (`scenarios/영도.json`, 38키)

| 항목 | 수 | 비고 |
|---|---|---|
| `places` | 122 | 계층 그래프. `tier` 없이 `parent`만 |
| `default_npcs` | 47 | 32명이 장소에 배치됨 |
| `jobs` | 16 | 8종에 `top_stat` (스탯 치중) |
| `start_frames` | 8 | 도착 서사. 5축 슬롯 |
| `profile_creation` | 10단계 | 성별→이름→나이→직업→스킬→능력치→외모→외형→배경→복장 |
| `starting_items` | 공통 6 + 직업별 2 | |
| `briefing_formats` | 5 | |
| 퀘스트 (`영도.quests.json`) | 44 | 커버리지 122/122 |

### 무협 (`scenarios/무협.json`, 22키)

`places`·`profile_creation`·퀘스트가 **없다.** 스탯 4종(무공·내공·신법·기예), `keyword_memory` 38항목, 프로필 9항목(경지·소속·근거지·별호·익힌무공)으로 영도와 체계가 다르다.

---

## 핵심 시스템 상세

### 장소 계층 (`places.py`)

**계층은 틀이 아니라 그래프다.** `tier`를 두지 않고 `parent`만 가리키며 깊이는 결과일 뿐이다.

```
섬서 ─ 화음 ─ 화산 ─ 화산파 ─ 접객당   (깊이 5)
  ├─ 종남산 ─ 종남파                  (깊이 3)
  └─ 숲                              (깊이 2, 최소단위)
```

| 필드 | 의미 |
|---|---|
| `connected` | 직결. 문 하나 사이 |
| `reachable` | 한 턴에 이동해도 어색하지 않은 범위. 미지정 시 `connected` 3칸 확장 |
| `visible_within` | 상위 안에 들어가야 인지 가능 (해련에서 남항 내부는 안 보인다) |
| `inherit` | 온디맨드로 풀 정보를 주입할 상위 |
| `known_brief` / `unknown_hint` | 방문 여부에 따라 다르게 주입 |
| `image` | 없으면 상위 중 가장 하위의 것을 상속 |

**이동 개연성** — `reachable` 밖 목적지는 ASK로 안내한다. 차단이 아니라 거리 인식이다. 판단층위가 캐시를 읽지 않으므로 코드가 경로를 계산해 사용자 프롬프트에 넣는다.

### 퀘스트 필터 (`quest_filter.py`)

**필터가 곧 슬롯이다.** 후보를 나열해 두면 일치한 값이 빈칸을 채운다.

```json
"npc": {"any": ["엄주섭","차봉순","황기영","법운"], "as": "기록자"}
```

장부방 → 엄주섭, 지하 병참 창고 → 차봉순. **하나의 틀이 여러 곳에서 각기 다른 사건이 된다.**

개연성은 구조로 막는다 — NPC 후보는 **현재 장소에 실재하는 인물과 교집합**을 먼저 낸다. 경로 추정이 아니라 직접 대조라 시나리오 구조와 무관하다.

| 필터 | 기능 |
|---|---|
| `any`/`all`/`none` | 하나 일치 / 전부 / 배제 |
| `as` | 슬롯 이름 지정 |
| `scope` | `here`(장소 상주) / `companion`(동행) / `any` |
| `pair` | 인물·장소가 함께 성립 (전속 퀘스트) |
| `place`/`within` | 정확 일치 / 상위 경로 포함 |
| `faction`/`faction_scope` | 소속 / 영역 |
| `item`·`info`·`time_of_day`·`min_stat` | |
| `grants` | 클리어 시 세션 반영 (소속 획득) |

### 동행 (`extraction.py`)

`met_npcs`(누적 만남)와 `companions`(현재 동행)를 분리 관리한다. 추출층위는 `joined`/`left` **변화만** 보고한다 — 목록 전체를 다시 쓰게 하면 기존 동행자가 누락된다.

장소 이동 시 그곳 **상주** NPC는 자동 해제된다.

### 되감기 (`rewind.py`)

턴별 델타를 append-only로 기록하고 역순 복원한다. `TRACKED_PATHS` 14개 필드를 추적한다.

**압축 기억도 롤백**된다. 압축 발생 시점을 델타에 기록하므로 플랜별 주기를 몰라도 정확하다.

```
5턴 압축 시스템 · 12턴 진행 → 8턴으로 되감기
→ 제거턴 [12,11,10,9], 압축기억 '10턴까지' → '5턴까지'
```

### 결제

디스코드 결제가 한국 미지원이라 **`!지급`으로 오너가 직접 지급**한다. 나머지 로직은 기획대로 작동한다.

```
턴 진행 전   세션 오픈 확인 → 닫혀 있으면 차단
            소지금 < 예상 최대 → 플레이 차단
턴 종료      실제 차감. 음수면 1잉크 보정 (초과분 운영자 부담)
세션 오픈    선불 차감 (업로드 + 유지비)
세션 클로즈   사용분만 계산해 차액 환급, 10초간 알림
```

TTS 예상은 **합산하지 않고 구분 표기**한다 (`턴 3~4잉크 + TTS 7~11잉크`).

---

## UI 진입점

### GM 스페이스 (서버 홈)

```
gmspace:register   계정 등록 (DM)
gmspace:session    세션 열기 → 종류 선택 → 17단계 플로우
gmspace:profiles   사전 프로필 관리 (DM)
gmspace:charge     잉크 충전
gmspace:hall       명예의 전당
gmspace:world      월드보드 공개
gmspace:stats      내 통계 (DM)
gmspace:refresh    보드 갱신
```

### 디스플레이 채널

```
disp:tts disp:image disp:bgm disp:sfx    미디어 토글
disp:vol_down disp:vol_up                볼륨
disp:rewind                              1턴 되감기
disp:rewind_multi                        여러 턴 되감기 (모달)
disp:restart                             턴 재시작
disp:session                             세션 오픈/클로즈
disp:pay                                 결제 호출
```

턴 진행 중에는 민감 버튼이 회색으로 비활성화된다.

### 게임 채널

```
session:join       세션 참가 → 이름 모달 → 프로필 생성 UI
extraction:retry   추출층위 재시도
rewind:one         1턴 되감기 확인
```

---

## 작업 원칙

**① `prompts.py`·`scenarios/*.json` 불가침**
수정안을 제시하고 원본 대조 검사를 받은 후에만 적용한다.

**② 계획서 선행**
코드보다 md 계획서를 먼저 작성해 검증받는다.

**③ 기억에 근거한 임의 판단 금지**
저장된 기억으로 방향을 결정하지 않는다. 명시된 내용에만 의존하고, 불명확하면 질문한다.

**④ 유기적 정합성**
서로 얽힌 기능이 충돌 없이 굴러가도록 한 번에 개발한다.

**⑤ "완료"의 기준은 사용 가능 여부**
모듈이 임포트되고 단위 테스트를 통과하는 것은 완료가 아니다. **플레이어가 그 기능에 도달할 경로가 존재하는지**까지 확인해야 완료다. 커밋 전 진입점을 `grep`으로 확인한다.

**⑥ 오류 보고 전 실물 확인**
검출 결과를 그대로 보고하지 않는다. 해당 코드를 열어 읽고 실제 문제인지 확인한 뒤 보고한다.

**⑦ 표기값은 단일 출처에서 읽는다**
버전·초대 링크처럼 여러 곳에 나타나는 값은 상수나 런타임 속성 하나만 두고
그것을 읽는다. 임베드에 값을 박아두면 배포 후에도 낡은 채로 남으므로,
재시작 시 자동 갱신하거나 호출 시점에 읽어야 한다.

### 버전 체계

`MAJOR.MINOR.PATCH` — MAJOR는 `SCHEMA_VERSION` 증가 등 비호환 변경, MINOR는 하위호환 기능 추가·프롬프트 변경, PATCH는 버그 수정.

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
attrs.add('cached_worldview_sections')   # cache.py가 동적 생성하는 정상 예외
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

22항목을 대조한다 — 버전·`SCHEMA_VERSION`·`SESSION_FIELDS`·`TRACKED_PATHS`·
서브모듈 수·장소·NPC·직업·시작 틀·프로필 단계·브리핑·퀘스트.

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

기준값 — cogs 9 · 명령어 44 · views 5

---

## 현재 미완 항목

| 항목 | 상태 |
|---|---|
| 메인라인 퀘스트 | 구 15종 폐기 시 함께 사라짐. 서브 클리어 후 갈 곳이 없다 |
| 무협 시나리오 | `places`·`profile_creation`·퀘스트 전무 |
| 공통 소개 TTS·테마곡 | 오디오 자산 대기 |
| NPC `birth_year` | 0/47명. `timeline.enrich_npc_ages`가 대기 중 |
| 난이도·대결 판정 | 설계 보류 (계층 항목 처리 미결) |
| 아이템 명문화 | 소지품 지급은 되나 사용 판정 없음 |
| 디스코드 결제 | SKU 한국 미지원 |
