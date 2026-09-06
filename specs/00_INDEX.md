# 명세 작업 진행

- **기준 버전**: v5.29.1 · 25,660줄
- **규칙**: `SPEC_RULES.md` — 작업 전 반드시 읽을 것
- **산출물**: `specs/{번호}_{영역}.md`

---

## 진행 현황

| # | 영역 | 모듈 | 줄 | 상태 | 발견 | 확인필요 | 커밋 |
|---|---|---|---|---|---|---|---|
| 1 | base | constants · models · io · utils | 1,101 | ⬜ 대기 | - | - | - |
| 2 | ai | prompt · extraction · dialogue · resilience | 1,461 | ⬜ 대기 | - | - | - |
| 3 | world | places · timeline · start_frame · irregular_npc · growth · koreantext | 1,413 | ⬜ 대기 | - | - | - |
| 4 | quest | quest · quest_filter | 1,075 | ⬜ 대기 | - | - | - |
| 5 | session | session_flow · creation · session_open · gm_space · intro | 1,699 | ⬜ 대기 | - | - | - |
| 6 | profile | profile_gen · profile_runner · profile_creation_ui · profile_ai · profiles · profile_ui | 2,455 | ⬜ 대기 | - | - | - |
| 7 | cost | cost · estimate · ink · accounts · terms · stats | 1,620 | ⬜ 대기 | - | - | - |
| 8 | memory | memory_plan · rewind · cache | 1,073 | ⬜ 대기 | - | - | - |
| 9 | media | audio_mixer · tts · tts_preset · media · media_control | 1,092 | ⬜ 대기 | - | - | - |
| 10 | ui | display · ui · chat_guard | 998 | ⬜ 대기 | - | - | - |

**작업 순서는 위 표의 순서를 따른다.** 아래에서 위로 쌓아야 참조가 성립한다.

### cogs 배분

각 cog는 해당 영역 명세에 포함한다.

| cogs | 줄 | 소속 영역 |
|---|---|---|
| `gm.py` | 4,037 | ai (층위 호출) + quest (퀘스트 명령) |
| `game.py` | 1,466 | memory (압축·되감기) + ai (턴 진행) |
| `character.py` | 1,249 | profile |
| `system.py` | 858 | cost |
| `media.py` | 733 | media |
| `session.py` | 658 | session |
| `presence.py` | 110 | memory (캐시 만료 감지) |
| `errors.py` · `permissions.py` | 252 | base |

---

## 누적 확인 필요 목록

> 영역 완료 시 여기에 모은다. 사용자가 한 번에 답할 수 있도록.

(아직 없음)

---

## 누적 발견 사항

> 결함 의심 · 죽은 코드 · 중복 · 데이터 불일치.
> **명세 작업 중에는 수정하지 않는다.** 기록만 한다.

### 착수 전 이미 알려진 것

이전 작업에서 파악된 것들이다. 각 영역 명세에서 재확인한다.

**미사용 판정 (사용자 지시로 유지 중)**

| 대상 | 상태 |
|---|---|
| `ink.can_afford` | `max_ink` 직접 비교로 대체된 것으로 보임 `[확인 필요]` |
| `memory_plan.plan_key` | `get_plan`과 중복으로 보임 `[확인 필요]` |
| `creation.is_done` · `get_data` | 호출부 없음 `[확인 필요]` |
| `profile_gen.reroll_stats` | UI가 `cancel_pending`으로 우회 `[확인 필요]` |
| `profile_runner.jump_to` | 기획 요구사항이나 UI 버튼 없음 `[확인 필요]` |

**중복 실측**

| 패턴 | 횟수 | 파일 수 |
|---|---|---|
| 세션 저장 | 89 | 13 |
| game_ch 획득 | 30 | 8 |
| 비용 로그 | 25 | 8 |
| 비용 누적 | 24 | 10 |
| 토큰 추출 | 20 | 6 |
| AI 호출 | 18 | 6 |
| master_ch 획득 | 17 | 4 |

**거대 함수 (100줄 이상)**

| 줄 | 위치 |
|---|---|
| 517 | `game.py::_execute_proceed` |
| 339 | `gm.py::_build_logic_user_prompt` |
| 267 | `character.py::manage_npc` |
| 261 | `gm.py::_run_extraction` |
| 253 | `gm.py::_run_gm_logic_loop` |
| 253 | `cache.py::build_scenario_cache_text` |
| 252 | `gm.py::_plan_narrative` |
| 225 | `models.py::__init__` |
| 218 | `media.py::send_media` |
| 187 | `gm.py::_call_gm_logic` |

**이름과 실체 불일치**

| 이름 | 실체 |
|---|---|
| `MIN_CACHE_TTL` | '최소'가 아니라 6시간 전체값 |
| ~~`서사 설계자(방향성)`~~ | 5.28.0에서 `개연성 시뮬레이션`으로 정정됨 |

---

## 명세 완료 후

명세가 전부 끝나면 다음을 판단한다.

1. **리팩터링 vs 재개발** — 명세를 근거로 결정
2. **발견 사항 처리** — 결함·죽은 코드를 어떻게 할지
3. **미사용 기능** — 의도를 확인한 뒤 유지·연결·제거 결정
