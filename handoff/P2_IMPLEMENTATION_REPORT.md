# P2 통합 구현 보고 — v7.19.0

- 대상 범위: 로드맵 #9 TTS 실효 · #10 목소리 프로필 · #3 저장 프로필 UX · #4 대화형 메시지 · #6 SVG 지연 로딩/오류 · #7 마스터 이미지 슬롯
- 브랜치: `claude/p2-voice-ui`
- 출발 HEAD = P1 기준 SHA: `63e257e5f5979d74326c3f0cdbd9f9b20b3b7d07` (v7.18.0)
- 완료 HEAD: 이 보고서를 담은 커밋(커밋 메시지 `v7.19.0 P2 …`)
- **최종 상태: 부분 완료(정당한 제약 포함)**
  - 기능 6종은 모두 구현했고 표적 테스트로 검증했습니다.
  - 공급자(Gemini TTS) 제약 때문에 아래 두 항목만 부분 충족입니다(§5).
    - 나이 기반 목소리 배정
    - 대사별 명시적 감정 신호
  - 마스터 원화 6종이 미제작인 것은 결함이 아닙니다. 정책대로 공용 로딩 이미지를 대신 쓰는 **정상 상태**입니다.

---

## 1. 변경 파일

| 구분 | 파일 |
|---|---|
| 신규 모듈 | `core/voice_profile.py`<br>`core/master_art.py`<br>`core/conversational.py` |
| 신규 자산 | `assets/indaim_loading_neutral.webp`(sha256 `a28a590e…`)<br>`assets/indaim_error_neutral.webp`(`75820e4e…`)<br>번들 원본 그대로이며 둘 다 332×312입니다.<br>`assets/master/manifest.json`(6슬롯 모두 null) |
| 음성 | `core/tts.py`(style 인자)<br>`cogs/game.py`(문단별 화자 목소리·두 게이트 일원화·카드 전달)<br>`cogs/gm.py`(`_voice_lines`: 진행자 직접 발화·NARRATE 더빙)<br>`cogs/media.py`(`!더빙` 플래그 동기) |
| 상태·UI | `core/media_control.py`(`tts_status`)<br>`core/display.py`(`disp:master_gender`·TTS 실효 표시)<br>`core/display_board.py`(진행자·TTS 실효 행)<br>`core/session_flow.py`(content화·진행자 성별 단계)<br>`core/profile_creation_ui.py`(content화·저장 규칙)<br>`core/initial_knowledge.py`(content화·영수증) |
| 카드 | `core/speaker_card.py`<br>추가: `deliver_card`·`_render_card`·`_replace_pending`·`loading_threshold_sec` |
| 영속 | `core/io.py`·`core/models.py`<br>세션 필드 `master_gender`(기본 `"male"`), `SESSION_FIELDS` 105 → 106 |
| 기타 | `core/__init__.py`(새 모듈 3개 재수출)<br>`.gitignore`(`data/voice_assignments.json`)<br>`core/constants.py`(7.19.0)<br>`CLAUDE.md`·`DEVLOG.md` |
| 테스트 | 신규 `tests/policy/test_p2_voice_ui.py`(30건)<br>기존 5개 파일을 새 계약에 맞게 갱신(§6) |

다음은 바꾸지 않았습니다.
- `prompts.py`
- `scenarios/*.json`
- `SCHEMA_VERSION`
- turn-history·CommitPlan

**저장·기동 마이그레이션**
- `master_gender`는 `SESSION_FIELDS` 기본값으로 로드됩니다. 구 세션은 `male`로 열리며, 기존 나레이터(Algenib)와 같은 목소리입니다.
- 별도 변환 코드는 없습니다.
- `data/voice_assignments.json`은 첫 NPC 발화 때 생성되는 운영 산출물이라 커밋하지 않습니다.

---

## 2. 사용자 결정 Q1~Q10 충족표

| Q | 결정 | 구현 | 상태 |
|---|---|---|---|
| Q1 | 남·여는 같은 진행자 | 대사 자산(`assets/master_dialogues_v1.json`)·슬롯 체계를 하나로 두고 성별은 표현(목소리·이미지 슬롯)만 바꿉니다. | 충족 |
| Q2 | 개설자가 세션별 선택 | 세션 생성 TTS 단계 뒤에 `MasterGenderView`(남성/여성 진행자)를 둡니다. 선택값은 `session.master_gender`로 영속됩니다. | 충족 |
| Q3 | NPC 목소리 자동 배정·영속 | 정본 저작 `default_npcs[*]['나이·성별']`에서 성별을 읽고 그 성별의 목소리 풀에서 sha256으로 고릅니다. 결과는 `data/voice_assignments.json`에 기록해 첫 배정 후 고정합니다. 나이는 쓰지 못합니다(§5). | 부분(나이) |
| Q4 | 감정은 절제, 마스터는 매우 절제, 새 LLM 호출 없음 | NPC: "그 대사 문장에 드러난 감정만, 아니면 중립, 과장 금지" 스타일 지시<br>마스터: 차분·정중 지시<br>내레이션: 기존 절제 프로필<br>새 호출 0 | 부분(명시 신호 없음, §5) |
| Q5 | 같은 시나리오+NPC는 세션이 달라도 같은 목소리 | 키 = `scenario_id|npc_key`<br>npc_key = 저작 사전 키(명시 `actor_id`가 있으면 그것)<br>표시 이름이 중복이면 추정하지 않습니다. | 충족 |
| Q6 | 개설자가 나중에 변경, 이후 출력만 | 디스플레이 `disp:master_gender`는 개설자(`flow_owner`)·봇 오너만 누를 수 있습니다. 저장·갱신만 하고 소급하지 않습니다. | 충족 |
| Q7 | 본문·NPC·마스터 직접 발화 모두 TTS, OFF는 합성 0 | 다음 경로를 더빙합니다(`_voice_lines`).<br>· 자동 턴 본문(동기·비동기 경로)<br>· ASK 안내<br>· 경로·목적지 질문<br>· 선언 게이트 보류 안내<br>· NARRATE 본문<br>OFF·미연결·트랜잭션 없음이면 합성 0입니다. | 충족 |
| Q8 | 본문 내레이션과 마스터 직접 발화는 같은 목소리 | 둘 다 `voice_profile.master_voice(session)`를 씁니다. 같은 목소리에 스타일만 다릅니다. | 충족 |
| Q9 | 임계 시간을 넘을 때만 로딩, 같은 메시지 교체 | `deliver_card`: 임계 안에 끝나면 카드만 보냅니다. 넘으면 로딩 이미지를 보낸 뒤 같은 메시지의 `attachments`를 교체합니다. | 충족 |
| Q10 | SVG 실패는 오류 이미지, 텍스트 자동 대체 없음 | 렌더 실패·제한 시간 초과·교체 실패 시 오류 이미지를 보내고 구조 로그를 남깁니다. | 충족 |

---

## 3. 실제 구현 및 근거(기술 계약)

### 3-1. TTS 실효(#9)

**OFF 불변식**
- 두 게이트(`tts_enabled`, `media_flags["tts"]`)가 서로 어긋나 있었습니다.
- 모든 런타임 합성 입구를 `media_control.is_enabled(session,"tts")`, 즉 두 게이트 AND로 통일했습니다.
  - `_synthesize_and_enqueue`
  - 비동기 더빙 판정
  - `_voice_lines`
- `!더빙`·디스플레이 토글·세션 생성 TTS 단계는 `sync_tts_flag`로 두 필드를 함께 씁니다.

**ON 유효성**
- `tts_status(session) → (설정 켜짐, 실제 재생 가능, OFF|NO_VOICE|OK)`를 추가했습니다.
- 재생 가능 = 믹서가 있음, 즉 봇이 음성 채널에 붙어 있음입니다.
- 상태판과 Embed 대체 표시에서 "ON / 미연결 / OFF"를 구분합니다. 켜짐이 곧 실제 출력으로 보이지 않습니다.
- 미연결이면 믹서 확인 단계에서 공급자 호출 전에 멈춥니다(기존 동작 유지).

**`!tts생성`(오너 전용)**
- 고정 문구 사전 합성 도구로, 세션 OFF와 목적이 다릅니다. 현행 의미·권한을 그대로 두었습니다.
- 조사 결과, 만들어진 프리셋을 런타임에 재생하는 경로가 없습니다(§4 의존성 ③). 따라서 성별 전환 뒤 옛 음원이 재사용될 위험(T8)도 없습니다.

**온오프 존치 조사 질문**
- 비용·음성 채널 미사용 세션을 위해 OFF 기능은 필요합니다. 사용자가 포기하지 않았으므로 유지했습니다.

### 3-2. 목소리 프로필(#10)

**공급자 계약**
- Gemini TTS는 요청 하나에 prebuilt 목소리 하나와 자연어 스타일 지시(DIRECTOR'S NOTES)를 받습니다.
- 그래서 화자별로 문단을 나눠 합성합니다. multi-speaker 모드는 쓰지 않습니다(T10).

**성별표**
- Google Cloud TTS 'Chirp 3: HD voices' 목소리 표(Gemini TTS와 같은 prebuilt 이름)를 `VOICE_GENDER`에 그대로 옮겼습니다.
- 공급자 목록(`TTS_VOICES`)과 일치하는지 테스트로 확인합니다.

**마스터 목소리**
- 남성: Algenib(기존 나레이터라서 구 세션도 같은 목소리)
- 여성: Achernar
- NPC 풀에서는 마스터 목소리 둘을 제외해 진행자와 혼동되지 않게 했습니다.

**NPC 식별 정책**
- 저작 voice가 있으면 그것을 씁니다(기존 데이터 호환).
- 그다음은 정규 NPC 고정 배정입니다.
  - 키: `default_npcs` 사전 키, 명시 `actor_id`가 있으면 그것
  - 표시 이름 `name`은 유일하게 일치할 때만 키로 거슬러 찾습니다.
- 비정규 NPC는 세션 등록부 목소리(v5의 `irregular_npc.voice_for`)를 씁니다. 세션 한정 인물입니다.
- 이름이 같고 키가 다른 NPC나 다른 시나리오는 키가 달라 서로 섞이지 않습니다.

**결정성·경합**
- `sha256("voice.npc.v1|시나리오|키") mod 풀`로 고르므로 Python hash seed와 무관합니다.
- 기록은 프로세스 락 안에서 원자적으로 교체합니다(tmp → replace).
- 기록이 실패해도 같은 결정적 값이 다시 나옵니다.
- 기록이 있으면 결정적 값보다 우선합니다. 목소리 목록이 바뀌어도 이미 배정된 인물은 바뀌지 않습니다.

**성별 정보 출처와 누출 방지**
- 성별은 '나이·성별' 저작 문구에서만 읽습니다. 비밀·성격 필드는 TTS에 쓰지 않습니다.
- 합성 본문은 화면에 나간 대사 문장(`clean_text_for_tts`)뿐이라 숨은 정보가 새지 않습니다.

**비용 귀속**
- 진행자 직접 발화·NARRATE 더빙은 그 턴 트랜잭션의 비용 멤버로 claim합니다(`claim_cost_operation`). 판단층위 ASK 비용과 같은 귀속입니다.
- 트랜잭션이 없는 안내(라운드 차례 안내 등)는 정산 귀속이 없으므로 더빙하지 않습니다. 그래서 운영자 부담과 섞이지 않습니다(X4).
- P1 TTS usage 관측(`_TTSObserver`)·늦은 응답 정책은 그대로입니다(T9).

### 3-3. 저장 프로필(#3)

- 불러오기 시점의 구조화 값을 `run["loaded_profile"]`에 깊은 사본으로 남깁니다. `run`과 함께 `profile_creation_progress`에 영속되므로 재시작 뒤에도 같은 판정이 나옵니다(P6).
- `profile_changed(run)`의 반환값:
  - `None`: 신규 생성
  - `False`: 최종값이 불러온 값과 같음(방문·같은 값 재입력·원복 포함)
  - `True`: 실제 변경 있음
- 판정은 최종값만 비교합니다.
- `False`이면 저장 버튼을 내지 않고 "계속"만 보여 줍니다(P1·P2).
- 저장은 확인 화면(`ConfirmSaveView`, 정확히 1회)을 거칩니다.
  - 확인하면 새 사전 프로필로 저장하고 원본은 덮어쓰지 않습니다.
  - 취소하면 저장 여부 화면으로 돌아갑니다.
  - 화면 세대(`gen`)로 연타·이전 화면 조작을 막습니다(P4·P5).

### 3-4. 대화형 메시지(#4)

- `conversational.split`이 송출 경계에서 기존 Embed 빌더 결과를 나눕니다.
  - content로: 제목·본문·이름 없는 이어 쓰기 필드·'알림'·진행도 푸터
  - 데이터 카드 Embed로: 이름 있는 정형 필드(예상 비용·능력치 등)
- 1,900자를 넘는 부분은 카드 설명으로 이어 붙이고 자르지 않습니다.
- 편집할 때는 `embeds=[]`(또는 카드)를 명시해 낡은 Embed가 남지 않게 합니다. view는 같은 메시지에서 교체합니다.
- 적용 경로(U1~U3):
  - 세션 생성 단일 메시지 화면(`session_flow.show`)
  - 소개 화면
  - 시작 틀 선택
  - 캐릭터 생성 화면(단계·초안·최종 검토·완성·불러오기 질문·절차 없음 안내)
  - 저장 확인
  - 초기 지식 전달
- 카드로 남긴 것(U4, 정형 데이터라서 Embed/SVG 유지):
  - 디스플레이 상태판
  - 프로필 DM 관리
  - 통계·명예의 전당·월드보드
  - 비용 임베드
  - 초기 지식 재열람(ephemeral)
- 마스터 이미지(§3-6)는 첫 송출에만 첨부합니다. 이후 편집에서는 첨부가 유지됩니다.
- 새 LLM 호출이나 새 메시지는 없습니다(U6).

**초기 지식 영수증**
- 전달 영수증 줄(`-# ik-… · i/n`)은 항상 content에 둡니다. 페이지가 길면 본문만 카드로 보냅니다.
- 영수증 조회는 content와 옛 Embed 푸터를 모두 읽습니다(v7.17 메시지 호환).
- 두 전달 시점(PROFILE·START)과 권위는 그대로입니다(U3).

### 3-5. SVG 지연 로딩·오류(#6)

**적용 경로**
- 실제 rasterize가 일어나는 화자 카드의 두 묘사 경로(비동기·TTS 동기)에만 적용했습니다(I7).

**임계값**
- 측정값(이 컨테이너):
  - 상태판 SVG: 0.4~1.1초
  - 원본 카드 템플릿(5.8MB): 13~17초
- 기본값을 **1.5초**로 정했습니다.
  - 짧은 렌더나 캐시 히트(`_PNG_CACHE`)는 로딩 없이 카드만 보냅니다(I1).
  - 실제 카드 렌더는 로딩 이미지를 먼저 보냅니다(I2).
- 환경변수 `SVG_LOADING_THRESHOLD_SEC`로 바꿀 수 있습니다.

**상태별 동작**

| 상태 | 동작 |
|---|---|
| NONE | 카드가 없으면 아무것도 보내지 않습니다. |
| CARD | 렌더가 임계 안에 끝나면 카드를 보냅니다. |
| ERROR | 렌더가 임계 안에 실패하면 오류 이미지와 로그를 남깁니다. |
| PENDING | 임계를 넘기면 로딩 이미지를 보내고 백그라운드에서 같은 메시지 첨부를 교체합니다. 교체 결과는 카드 또는 오류 이미지입니다(I3).<br>최종 대기 상한은 `RENDER_TIMEOUT_SEC + 30`초이며, 넘으면 오류 이미지로 바꿉니다. |
| SEND_FAILED | 처음 전송부터 실패하면 성공으로 포장하지 않고 로그만 남깁니다. |

**교체 실패(429·권한·삭제)**
- 카드 교체가 실패하면 오류 이미지로 **1회만** 더 시도합니다. 유한하며 로딩이 영구히 남지 않게 합니다.
- 그것마저 실패하면 `EDIT_FAILED` 로그를 남깁니다(I4).

**경합·누적**
- 카드 메시지는 대사 1건에 1개이고, 교체는 자기 메시지에만 합니다. 느린 렌더가 다른 메시지나 최신 상태를 덮지 않습니다(I5).
- 렌더 작업은 대사당 1회이고 PNG 캐시를 재사용합니다. 반복 rasterize가 없습니다(I6).
- 재시작으로 끊긴 PENDING은 로딩 이미지로 남을 수 있습니다. 프로세스 메모리 작업이라 재개하지 않습니다(§7 한계).

**상태 구분(I8)**
- 렌더 지연 중 로딩은 `deliver_card`의 PENDING입니다.
- 마스터 슬롯 미제작은 `master_art.UNPRODUCED`입니다. 이 경우 로그·재시도·타이머가 없습니다.

**디스플레이 상태판**
- 기존 v7.10 설계(렌더 실패 시 `build_embed` 대체)를 유지했습니다. 결정 항목은 §7에 있습니다.

### 3-6. 마스터 이미지 슬롯(#7)

**manifest**
- 파일: `assets/master/manifest.json`
- 키: `male|female` × `default|warning|positive` 6개
- 값: 파일명 또는 null

| 상태 | 조건 | 동작 |
|---|---|---|
| UNPRODUCED | null | 공용 로딩 이미지. 로그 없음 |
| ERROR | 파일명이 있는데 없거나 손상(PIL verify 실패) | 오류 이미지 + 슬롯당 1회 구조 로그 |
| PRODUCED | 파일이 정상 | 그 원화 |

- 원화 적용 트리거: `assets/master/`에 파일을 두고 manifest 해당 슬롯에 파일명을 적습니다. 재시작하거나 `master_art.reload()`를 부르면 그 슬롯만 바뀝니다(M3).
- 경로는 저장소 기준 절대 경로라 cwd와 무관합니다.

**톤**
- 기존 마스터 대사 키에 담긴 신호만 씁니다.
  - 경고: fail·blocked·insufficient 등
  - 긍정: completed·welcome·start 등
  - 나머지: 기본
- 새 대사나 성격은 만들지 않았습니다.

**성별 연동**
- 성별이 바뀌면 이후 화면의 슬롯이 바뀌고, 과거 메시지는 소급하지 않습니다(M4).
- 원화가 없어도 성별 설정·대사·TTS는 동작합니다.

---

## 4. 새로 발견한 간접 의존성(X6)

| # | 발견 | 처리 |
|---|---|---|
| ① | 세션 생성의 `tts_voice` 선택(VoiceSelect)이 어떤 합성에도 쓰이지 않았습니다(죽은 선택). | 선택 UI를 진행자 성별로 대체했습니다. 필드는 legacy 주석으로 남기고 읽지 않습니다. |
| ② | TTS 두 게이트가 경로마다 달랐습니다(일부는 `tts_enabled`만 확인). | `is_enabled` AND로 일원화했습니다(§3-1). |
| ③ | `tts_preset` 사전 합성 파일을 런타임에 재생하는 경로가 없습니다. | 범위 밖이라 보고만 합니다. 프리셋이 재생되지 않으므로 음원 재사용 위험(T8)도 없습니다. |
| ④ | TTS 동기 경로(`_stream_paragraphs_synced`)가 모든 문단을 나레이터 목소리로 읽었습니다. 화자 목소리가 비동기 경로에만 있었습니다. | 문단별 `paragraph_voice`로 통일했습니다. |
| ⑤ | 초기 지식 영수증 복구가 Embed 푸터 nonce에 의존했습니다. content화하면 복구가 깨집니다. | content 영수증 줄을 쓰고 옛 Embed 푸터도 함께 조회합니다(§3-4). |
| ⑥ | 진행자 직접 발화는 턴 정산 귀속이 있을 때만 청구할 수 있습니다. | 트랜잭션 없는 안내는 더빙하지 않습니다(§3-2). |

---

## 5. 공급자 SDK 제약(가짜 구현 없음)

1. **나이**: 공급자 목소리 메타데이터에는 성별 표기만 있고 나이 정보가 없습니다. 나이에 맞춘 배정은 구현하지 않았습니다. **Q3 부분 미충족**입니다.
2. **감정**:
   - 명시적인 대사별 감정 파라미터가 없습니다.
   - 자연어 스타일 지시로 "대사 문장에 드러난 정도만, 아니면 중립"을 요청하는 것이 가능한 범위입니다.
   - 영도 화자 카드 바리에이션은 전부 단일(`neutral`)이라 신뢰할 수 있는 코드 감정 신호도 없습니다.
   - 새 LLM 호출 금지에 따라 감정 분류를 추가하지 않았습니다. 감정 표현이 실제로 되는지는 실청취로만 확인할 수 있습니다. **Q4 부분 미충족**입니다.
   - 음색(prebuilt 목소리)은 감정과 무관하게 고정됩니다(T7).
3. **화자 전환**: multi-speaker 모드를 쓰지 않고 문단 단위 단일 화자로 합성합니다.
4. 실제 공급자·Discord E2E는 수행하지 않았습니다(보안 제약). 합성·렌더·Discord는 모의 fixture로 검증했습니다.

출처(목소리 성별표·TTS 계약):
- Google Cloud Text-to-Speech, "Chirp 3: HD voices" — https://cloud.google.com/text-to-speech/docs/chirp3-hd
- Gemini API, "Speech generation (text-to-speech)" — https://ai.google.dev/gemini-api/docs/speech-generation

---

## 6. 테스트

**신규 `tests/policy/test_p2_voice_ui.py`(30건)**

| 매트릭스 | 근거 |
|---|---|
| T1 | `test_tts_off_or_half_gate_means_zero_synthesis`<br>`test_master_direct_speech_voiced_only_when_live_and_in_transaction` |
| T2 | `test_tts_status_distinguishes_off_unconnected_live` |
| T3·T4·Q8 | `test_master_voice_by_gender_same_persona`<br>`test_paragraph_voice_dialogue_npc_and_narration_master`<br>`_voice_lines`의 성별 변경 이후 출력 |
| T5·T6 | `test_npc_voice_stable_across_sessions_and_persisted`<br>`test_npc_key_uses_authored_key_not_display_guess`<br>`test_npc_pool_excludes_master_voices_and_matches_gender` |
| T7 | NPC·마스터 스타일 고정, 목소리는 감정과 무관(코드 근거) |
| T8 | 프리셋 미재생(§4 ③), 합성 캐시 없음 |
| T9·X1·X4 | P1 테스트 전체 통과(`test_p1_cost`·`test_p1_world`)<br>`test_runtime_tts_cost_context_claimed_into_turn` |
| T10 | 단일 화자 요청(`test_tuple_items_carry_voice_and_style`) |
| P1·P2 | `test_unchanged_loaded_profile_hides_save`<br>`test_profile_changed_ignores_visits_and_reverts` |
| P3~P5 | `test_changed_loaded_profile_confirms_once_then_saves_new`(확인 전 저장 없음·취소 복귀·원본 불변) |
| P6 | `loaded_profile`이 `run`에 영속(코드 근거)<br>기존 T15 재시작 테스트 통과 |
| U1~U3 | 기존 v7.15·v7.17 테스트를 content 기준으로 갱신해 통과<br>`test_initial_knowledge_receipt_found_in_content`<br>`test_conversational_split_content_and_card` |
| U4~U6 | 카드 유지 목록(§3-4)<br>`edit_kwargs` `embeds=[]` |
| I1~I4 | `test_card_fast_render_sends_card_without_loading`<br>`…slow_render_loading_then_same_message_replaced`<br>`…failure_shows_error_image…`<br>`…slow_failure_replaces_loading_with_error`<br>`…replace_failure_falls_back_to_error_once` |
| I5~I8 | 메시지당 자기 교체(코드 근거)<br>`test_no_card_sends_nothing`<br>`test_slot_states_distinct` |
| M1~M5 | `test_bundled_ui_assets_unchanged_and_no_master_art_created`<br>`test_slot_states_distinct`<br>`test_tone_from_existing_master_keys` |
| X2·X3 | `test_master_gender_session_field_default_and_migration`<br>`test_display_master_gender_button_owner_only` |
| 경계 | `test_new_modules_make_no_provider_calls` |

**기존 테스트 갱신(약화 없음, 새 계약 반영)**

| 파일 | 변경 |
|---|---|
| `test_svg_speaker_card.py` | 렌더 실패 시 "이미지 생략" 대신 "오류 이미지 + 대사 유지"(Q10)<br>동기 경로 순서 단언을 `deliver_card` 기준으로 변경 |
| `test_v715_character_creation.py`<br>`test_v717_initial_knowledge.py` | 화면 글을 Embed 대신 content+카드에서 읽음(`_txt`) |
| `test_v711_defect_fixes.py` | 합성 대역에 `style` 인자 추가 |
| `test_v717_faction_capabilities.py` | `SESSION_FIELDS` 105 → 106 |

**검증 루틴**
- ① 구문·self 참조: OK
- ② 재수출·필드: OK
- ④ `verify_docs`: 21항목 OK. 버전·필드·서브모듈 93 → 96을 `--fix`로 반영했습니다.
- ⑤ 봇 로딩: cogs 9 · 명령어 46 · views 5(기준값과 같음)
- ③·⑥ 시나리오 검사: 시나리오를 바꾸지 않았으므로 생략했습니다.

---

## 7. 최종 회귀 · 미완료 · 운영 메모

**최종 회귀**
- `python3 -m pytest tests/ -q` → **1548 passed**
- 첫 게이트 실행(1546 passed) 뒤 두 가지를 보강했습니다.
  - 자산 경로를 패키지 지정 `assets/`로 맞춤
  - 카드 교체 실패 시 오류 이미지 1회 재시도와 테스트 2건 추가
- 그래서 전체를 1회 다시 실행했습니다.

**운영 메모**
- `prompts.py`·시나리오가 그대로라 캐시 재발급은 필요 없습니다.
- 새 필드 `master_gender`는 자동으로 기본값이 들어갑니다. 배포 전 `sessions/` 백업 관례는 유지해 주십시오.
- `data/voice_assignments.json`은 서버에서 생성되며 커밋하지 않습니다. 같은 NPC의 목소리를 유지하려면 백업 대상에 포함해야 합니다.

**결정 필요 / 한계**
- 디스플레이 상태판 SVG 실패는 오류 이미지가 아니라 기존 v7.10 Embed 대체를 유지합니다(문서화된 기존 설계). Q10을 상태판에도 적용할지 결정이 필요합니다.
- 재시작으로 끊긴 카드 PENDING은 로딩 이미지가 남을 수 있습니다. 메모리 작업이라 재개하지 않습니다.
- 나이 기반 배정과 명시 감정은 공급자 제약으로 부분 미충족입니다(§5).

**범위 밖 발견(수정하지 않음)**
- `tts_preset` 사전 합성 결과가 런타임에 쓰이지 않습니다(§4 ③).
- 운영자 비용으로 만든 파일이 미사용 상태입니다. 활용할지 은퇴시킬지 결정이 필요합니다.
