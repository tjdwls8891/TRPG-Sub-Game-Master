# 명세 작업 진행

- **기준 버전**: v5.29.1 · 25,660줄
- **규칙**: `SPEC_RULES.md` — 작업 전 반드시 읽을 것
- **산출물**: `specs/{번호}_{영역}.md`

---

## 진행 현황

| # | 영역 | 모듈 | 줄 | 상태 | 발견 | 확인필요 | 커밋 |
|---|---|---|---|---|---|---|---|
| 1 | base | constants · models · io · utils | 1,101 | ✅ 완료 | 11 | 11 | (이 커밋) |
| 2 | ai | prompt · extraction · dialogue · resilience | 1,461 | ✅ 완료 | 10 | 12 | (이 커밋) |
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

### base (11건)

**비용 관련 — 우선 확인 권장**
- [ ] `process_cache_deletion`의 21600초 상한이 `open_minutes`를 반영해야 합니까?
      3시간 세션도 6시간까지 청구될 수 있습니다.
- [ ] `cache_tokens` 폴백 32768 — `MIN_CACHE_TOKENS`가 1024로 바뀌기 전 값입니다.
      도달하면 32배 과다 청구됩니다.
- [ ] `write_cost_log`에 달러를 추가할 필요가 있습니까?

**데이터 정합**
- [ ] `total_ink_spent`·`total_usd`를 되감기 추적에서 뺀 것이 의도입니까?
- [ ] `build_extraction_limits`가 공통 상태이상(`get_merged_status_effects`)도 주입해야 합니까?
- [ ] `SESSION_RESET_FIELDS` 3개를 재시작 시 초기화하는 이유가 라운드 수집 정합성 때문이 맞습니까?

**정리 대상**
- [ ] `models.py:220` `_npc_info_fields`가 미사용입니다. 쓰려던 것이 있었습니까?
- [ ] `models.py:30` `self.npcs = {}` 중복 초기화를 지워도 됩니까?
- [ ] `write_log`의 파일 쓰기 예외가 28개 호출부로 전파됩니다. 감싸야 합니까?

**설계 의도**
- [ ] `PROFILE_AI_MODEL` — *"저비용 모델이 확정되면"*이라 되어 있는데 후보가 있습니까?
- [ ] `LOGIC_MODEL` 주석의 `gemini-3-pro-preview` 대안을 쓸 계획이 있습니까?

### ai (12건)

**🔴 최우선 — 중대 결함**
- [ ] **장소·퀘스트 블록이 프롬프트에 주입되지 않습니다.**
      `PromptBuilder.add_place_block`·`add_quest_block`이 없는 `self.parts`에
      append해 조용히 실패합니다. 퀘스트는 4.33.0, 장소는 5.8.0부터입니다.
      `self.blocks`로 고치면 되는 단순 오타로 보이나, **두 블록이 들어가면
      프롬프트가 크게 늘어 비용이 오릅니다.** 고칠까요?
- [ ] 고친 뒤 두 블록을 `manifest`(비용 보고)에 등록할까요?

**설계 의도**
- [ ] 임계값 31~70 구간에서 상태 부여도 해제도 안 되는 것이 의도된 이력 현상입니까?
- [ ] `call_with_retry`가 AI 호출 18곳 중 5곳만 보호합니다. 전체 적용해야 합니까?
- [ ] 지시층위의 `resource_changes` 경로를 제거해야 합니까?
      5.27.0에서 자원 권한을 추출층위로 옮겼는데 양쪽이 살아 있어 이중 적용될 수 있습니다.
- [ ] `strip_unauthorized_pc_dialogue`(PC 대사 창작 제거)가 `game.py` 한 곳에만
      적용됩니다. 자동 GM 경로에도 넣어야 합니까?

**정리 대상**
- [ ] `send_layer_status` 호출부가 없습니다(WaitingStatus로 대체). 제거해도 됩니까?
- [ ] `dialogue.py:350~354`의 `_images_this_turn` 기록이 읽는 쪽 없이 남았습니다.
- [ ] `apply_extraction`의 `valid_status`가 공통 상태이상을 포함해야 합니까?

**구조**
- [ ] `_build_logic_user_prompt`(339줄)를 `PromptBuilder` 같은 빌더로 바꿔야 합니까?
- [ ] 판단·추출층위의 프롬프트 조립을 모듈로 분리해야 합니까?

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

**🔴 미작동 기능 (ai 명세에서 발견)**

| 기능 | 상태 |
|---|---|
| `PromptBuilder.add_place_block` | `self.parts` 오타로 5.8.0부터 미작동 |
| `PromptBuilder.add_quest_block` | 같은 오타로 4.33.0부터 미작동 |
| `dialogue.send_layer_status` | 호출부 없음 (WaitingStatus로 대체) |

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
