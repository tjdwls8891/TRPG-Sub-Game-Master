# H07 최종 통합 구현 보고 — 영도 DYNAMIC 이행 · NPC 위치/이동 모델 · 레거시 권위 은퇴

- **상태:** H07 IMPLEMENTATION COMPLETE / READY FOR INDEPENDENT REVIEW — **VERIFIED 아님**(사용자/GPT 독립 검수 전)
- **조사·검증일:** 2026-10-09
- **브랜치:** `claude/h07-yeongdo-cutover` · 시작 SHA `99f490eb8f61397b3b31ba5a50d8ef5d90600557` · push 없음
- **근거 지시:** H07_FINAL_INTEGRATED_IMPLEMENTATION_DIRECTIVE_2026-10-09_99f490e
- **과거 기록 보존:** `handoff/H07_YEONGDO_WORKBOOK/`·`handoff/H07_YEONGDO_AUTHORING_REVIEW.md`(H07-G 체크포인트)는 수정하지 않았다. 현재 저작 근거는 `handoff/H07_YEONGDO_WORKBOOK_V2/`(멱등 재현).

표기: **확정**(사용자 확정사항 구현) · **잠정**(근거가 약해 검토 권장) · **미정**(OPEN DECISION).

## 커밋

| 커밋 | 의미 |
|---|---|
| `364d759` | NPC 유효 위치(정본 override > 일과 > untracked)·선택 일과 스키마·일과 wakeup·명시 해제 op/step·이동 지식 정책 supersede(PC·NPC) |
| `11518f0` | PlaceId ≠ 표시명·정보 reveals(명시 발견)·플레이어 노출 표면 표시명·DYNAMIC 장소 블록 |
| `e014279` | 플레이어 명시 결단(resolution)·국면 guide 지시층위 주입(R-2)·EVENT_LOCAL template·관찰자 소지품/능력치 조건 |
| `bc14855` | 영도 V2 재저작(일과 35·untracked 12·genesis 정본 0·R-6 seed 폐기·결말/행적 명시 결단·조건 보존·표시명 8) |
| `ddccf19` | H07-C 레거시 서사 권위 은퇴(정의 바인딩 세션) + 소속 호환 뷰·PC 인지 블록 |
| `779924f` | H07-D `prompts.py` 동적 세계 조건부 권위 정합 |
| (이 문서를 담은 커밋) | H07-E/F/H 호환·통합 행동 게이트 T-1~T-10·성능·문서·버전 7.5.0 |

## 1. 최종 NPC 위치 분류 (확정 모델 · 개별 판정은 잠정 표기 참조)

유효 위치 = **정본 override**(사건·플레이어 영향 typed 배치) > **평상시 일과**(정의 + 정본 시계의 순수 함수) > **untracked**.
A(genesis 정본) **0명** — 시작 장면·즉시 사건이 정본 위치를 요구하는 NPC가 없다. B(일과) **35명** · C(untracked) **12명**.
근거 규칙: R1 `상주` → 기본 장소 · R2 `자주`(상주 없음) → 기본 장소(여럿이면 원문 첫 항목 — 잠정) · R3 프로필 '행동 방식'의 장소·시각 습관 · R4 직책이 장소를 직접 가리킴(잠정) · R5 `가끔`만/근거 없음 → untracked · R6 기존 저작 일과(수습대 낮 탐색).
공간 지식(seed)이 남은 NPC: **없음**(V1의 `yd_namhang_runner`는 이동 허용용이라 폐기 — 숨은 길·지름길을 안다는 인물 설정 근거가 없어 새로 저작하지 않았다).

| NPC | ActorId | 분류 | 평상시 일과(routine) | 근거 | 원문 연고(places[*].npcs) | V1 초안 genesis 배치 |
|---|---|---|---|---|---|---|
| 강철수 | `yd.npc.kang_cheolsu` | B routine | 남항 중공업 연대 [05:00~06:00]; 그 외 상층부 거주구 | R3 '매일 06시 전 전 구역 순시' + R2 상층부 거주구(자주) | 남항 중공업 연대(가끔), 상층부 거주구(자주) | 상층부 거주구 |
| 차봉순 | `yd.npc.cha_bongsun` | B routine | 그 외 지하 병참 창고 | R1 지하 병참 창고(상주) | 지하 병참 창고(상주), 취사반(자주) | 지하 병참 창고 |
| 노상진 | `yd.npc.no_sangjin` | B routine | 그 외 수습대 집결지 | R1 수습대 집결지(상주) | 남항 중공업 연대(자주), 수습대 집결지(상주) | 수습대 집결지 |
| 이준혁 | `yd.npc.lee_junhyeok` | B routine | 저지대 상업지구 [이른 아침/오전/한낮/오후]; 그 외 수습대 집결지 | R6 수습대 낮 탐색(이른 아침~오후 저지대, 그 외 요새 집결지) | — | 수습대 집결지 |
| 류가은 | `yd.npc.ryu_gaeun` | B routine | 저지대 상업지구 [이른 아침/오전/한낮/오후]; 그 외 수습대 집결지 | R6 수습대 낮 탐색(이른 아침~오후 저지대, 그 외 요새 집결지) | — | 수습대 집결지 |
| 박창수 | `yd.npc.park_changsu` | B routine | 그 외 제1방어선 | R1 제1방어선(상주) | 제1방어선(상주) | 제1방어선 |
| 오기태 | `yd.npc.oh_gitae` | B routine | 그 외 제1방어선 | R1 제1방어선(상주) | 제1방어선(상주) | 제1방어선 |
| 서태양 | `yd.npc.seo_taeyang` | B routine | 그 외 흑색 작업장 | R1 흑색 작업장(상주) | 흑색 작업장(상주) | 흑색 작업장 |
| 임재환 | `yd.npc.lim_jaehwan` | B routine | 그 외 지하 의료소 | R1 지하 의료소(상주) | 지하 의료소(상주) | 지하 의료소 |
| 황순애 | `yd.npc.hwang_sunae` | B routine | 그 외 거주 시설 하층부 | R1 거주 시설 하층부(상주) | 거주 시설 하층부(상주) | 거주 시설 하층부 |
| 최민준 | `yd.npc.choi_minjun` | C untracked | — | R5 연고 없음(3등급 부랑 용병 — 숙소 근거 없음) | — | 채무자 숙소 |
| 법운 | `yd.npc.beopun` | B routine | 그 외 해련사 | R1 해련사(상주) | 해련사(상주) | 해련사 |
| 조원달 | `yd.npc.jo_wondal` | B routine | 스카이웨이 [이른 아침]; 그 외 해련 자치 마을 | R3 '매일 아침 스카이웨이 연결 상태 직접 점검' + R2 해련 자치 마을(자주, 원문 첫 항목 — 잠정) | 해련 자치 마을(자주), 해련사 배급소(자주) | 해련 자치 마을 |
| 신대호 | `yd.npc.shin_daeho` | B routine | 그 외 집수 설비 | R2 집수 설비(자주, 원문 첫 항목 — 스카이웨이도 자주, 잠정) | 집수 설비(자주), 스카이웨이(자주) | 집수 설비 |
| 오명숙 | `yd.npc.oh_myeongsuk` | B routine | 그 외 중앙 주차장 텃밭 | R1 중앙 주차장 텃밭(상주) | 중앙 주차장 텃밭(상주) | 중앙 주차장 텃밭 |
| 장우진 | `yd.npc.jang_ujin` | C untracked | — | R5 연고 없음(청년 수송대장 — 고정 장소 근거 없음) | — | 스카이웨이 |
| 강병철 | `yd.npc.kang_byeongcheol` | B routine | 그 외 해련 자치 마을 | R2 해련 자치 마을(자주) — 초안의 하단 방어선은 원문 근거 없음 | 해련 자치 마을(자주) | 하단 방어선 |
| 박하은 | `yd.npc.park_haeun` | C untracked | — | R5 봉래산 능선 '가끔'만 | 봉래산 능선(가끔) | 봉래산 능선 |
| 윤점순 | `yd.npc.yoon_jeomsun` | B routine | 그 외 방음실 | R1 방음실(상주) | 방음실(상주) | 방음실 |
| 이동철 | `yd.npc.lee_dongcheol` | B routine | 중앙 주차장 텃밭 [오전/한낮/오후] | R4 경작대원 + 행동 방식 '텃밭 작업 중' — 낮 작업 시간만(잠정, 밤은 untracked) | — | 중앙 주차장 텃밭 |
| 박인철 | `yd.npc.park_incheol` | B routine | 중앙 주차장 텃밭 [오전/한낮/오후] | R4 경작대원 + 행동 방식 '지시받은 텃밭 작업' — 낮 작업 시간만(잠정) | — | 중앙 주차장 텃밭 |
| 이현석 | `yd.npc.lee_hyeonseok` | B routine | 그 외 조도 방위 사령부 | R3 '집무실 밖을 거의 나가지 않는다' — 원문 연고(강의동 병영·가끔)보다 행동 근거 우선(OPEN 확인) | 강의동 병영(가끔) | 조도 방위 사령부 |
| 민도현 | `yd.npc.min_dohyeon` | B routine | 그 외 조도 방위 사령부 | R2 조도 방위 사령부(자주) | 조도 방위 사령부(자주) | 강의동 병영 |
| 강지수 | `yd.npc.kang_jisu` | B routine | 그 외 검역 연구소 | R1 검역 연구소(상주) | 검역 연구소(상주) | 검역 연구소 |
| 황기영 | `yd.npc.hwang_giyeong` | B routine | 그 외 군수 창고 | R1 군수 창고(상주) | 군수 창고(상주) | 군수 창고 |
| 임철민 | `yd.npc.lim_cheolmin` | B routine | 그 외 방파제 초소 | R1 방파제 초소(상주) | 방파제 초소(상주) | 방파제 초소 |
| 손재현 | `yd.npc.son_jaehyeon` | B routine | 그 외 방파제 초소 | R4 '방파제 수비대원' — 초소(잠정) | — | 방파제 초소 |
| 윤상준 | `yd.npc.yoon_sangjun` | B routine | 그 외 조도 방위 사령부 | R1 조도 방위 사령부(상주) — 초안의 다중 검문소는 원문 근거 없음 | 조도 방위 사령부(상주) | 다중 검문소 |
| 배형식 | `yd.npc.bae_hyeongsik` | C untracked | — | R5 연고 없음(육상 작전대 요원) | — | 혁신지구 캠퍼스 |
| 최수연 | `yd.npc.choi_suyeon` | B routine | 그 외 혁신지구 캠퍼스 | R2 혁신지구 캠퍼스(자주) | 혁신지구 캠퍼스(자주) | 혁신지구 캠퍼스 |
| 김태호 | `yd.npc.kim_taeho` | C untracked | — | R5 연고 없음(기술지원팀 — 초안의 고정 함정은 근거 없음) | — | 고정 함정 |
| 문상철 | `yd.npc.mun_sangcheol` | B routine | 그 외 격리 구역 | R1 격리 구역(상주) — 격리 구역 감시병(수감자 아님) | 격리 구역(상주) | 격리 구역 |
| 나용순 | `yd.npc.na_yongsun` | C untracked | — | R5 중리 교역소·장부방 '가끔'만 | 중리 교역소(가끔), 장부방(가끔) | 장부방 |
| 엄주섭 | `yd.npc.eom_juseop` | B routine | 그 외 장부방 | R1 장부방(상주) | 장부방(상주) | 장부방 |
| 장철웅 | `yd.npc.jang_cheolung` | B routine | 그 외 바리케이드 미로 | R3 '교역소 바리케이드 미로의 사각지대 상시 …' + R2(자주 3곳 중 행동 근거) | 중리 교역소(자주), 바리케이드 미로(자주), 경비조 대기소(자주) | 경비조 대기소 |
| 조한 | `yd.npc.jo_han` | C untracked | — | R5 연고 없음(수거꾼) | — | 중리 해변 |
| 진복례 | `yd.npc.jin_bokrye` | B routine | 그 외 해녀 작업장 | R1 해녀 작업장(상주) | 해녀 작업장(상주) | 해녀 작업장 |
| 황민석 | `yd.npc.hwang_minseok` | B routine | 그 외 중리 해변 | R2 중리 해변(자주) | 중리 해변(자주) | 중리 해변 |
| 이선희 | `yd.npc.lee_seonhui` | B routine | 그 외 중리 해변 | R2 중리 해변(자주) | 중리 해변(자주) | 중리 해변 |
| 장현수 | `yd.npc.jang_hyeonsu` | C untracked | — | R5 연고 없음(채무자 숙소는 직책 추정이며 '야간에도 몰래 해안가' 행동과 상충) | — | 채무자 숙소 |
| 박미래 | `yd.npc.park_mirae` | C untracked | — | R5 연고 없음(채무자 숙소는 직책 추정) | — | 채무자 숙소 |
| 권상태 | `yd.npc.kwon_sangtae` | C untracked | — | R5 연고 없음(채무자 숙소는 직책 추정) | — | 채무자 숙소 |
| 신창호 | `yd.npc.shin_changho` | C untracked | — | R5 연고 없음(블랙리스트 추방자 — '중리 교역소 외곽 배회'는 특정 장소 아님) | — | 태종대 방면 숲 |
| 백인하 | `yd.npc.baek_inha` | C untracked | — | R5 연고 없음(단독 생존자) | — | 은신처 |
| 한도윤 | `yd.npc.han_doyun` | B routine | 그 외 바자르 | R2 바자르(자주) | 바자르(자주) | 바자르 |
| 전성남 | `yd.npc.jeon_seongnam` | B routine | 그 외 전성남의 은신처 | R1 전성남의 은신처(상주) — 하수도 분기점은 가끔 | 하수도 분기점(가끔), 전성남의 은신처(상주) | 하수도 분기점 |
| 염기준 | `yd.npc.yeom_gijun` | B routine | 그 외 자취방 | R1 자취방(상주) | 자취방(상주) | 자취방 |

## 2. 일과 스키마와 권위

```json
"routine": {
  "entries": [{"place": "<PlaceId>", "days": ["MON".."SUN"]?, "dayparts": ["<DaypartId>"]? | "from": "HH:MM", "to": "HH:MM"}],
  "default_place": "<PlaceId>"?
}
```
- 선택 키(미저작 인물·시나리오의 정의·revision 불변). 저작 순서 첫 일치 → 없으면 `default_place` → 없으면 untracked.
- 창은 `[start, end)` 반열림, 자정 넘김 허용, 요일은 창이 시작한 날 기준. 요일은 주가 정의된 달력(GREGORIAN)만 — FIXED_CUSTOM에 요일을 쓰면 컴파일 ERROR(`ROUTINE_WEEKDAY_UNSUPPORTED`). 시간대만 쓰는 일과는 모든 달력에서 유효.
- 오류 코드: `ROUTINE_REQUIRES_CALENDAR`·`ROUTINE_INVALID`·`ROUTINE_ENTRY_INVALID`·`ROUTINE_PLACE_UNKNOWN`·`ROUTINE_DAYS_INVALID`·`ROUTINE_WINDOW_CONFLICT`·`ROUTINE_DAYPARTS_INVALID`·`ROUTINE_DAYPART_UNKNOWN`·`ROUTINE_TIME_INVALID`·`ROUTINE_ENTRY_UNCONDITIONAL`·`ROUTINE_EMPTY`.
- **권위:** `core/npc_position.py` 하나가 해석한다. 장면 인물(`turn_world.scene_npc_projections`)·`CurrentNpcProjection`(`effective_placement`·`position_source`)·`world_refs`의 `ACTOR_PLACE`/`ACTOR_PLACEMENT_KIND`/`ACTOR_POSITION_SOURCE`(CANONICAL·ROUTINE·UNTRACKED)·사건 START_MOVEMENT 출발점.
- **정본 쓰기 없음:** 일과 전환을 SET하지 않고 장면 등장으로 materialize하지 않는다. 일과 경계는 조건이 실제로 읽는 행위자만 다음 \"장소가 바뀌는 분\"을 wakeup 후보로 내고(`predicates.next_wakeup`), 시계 전진 시 파생 위치 변화를 의존 키 `actor:X:placement`로 공급해 사건·정보 계열이 재평가된다(polling 없음).
- **override 해제:** typed `ACTOR_PLACEMENT_CLEAR`(사건 step `CLEAR_ACTOR_PLACEMENT`). 명시 해제만 · 미추적이면 no-op · TRANSIT 해제 시 그 행위자의 pending `MOVEMENT_WAKEUP` 삭제(남아 있어도 진행 엔진은 STALE로 무시). 자동 해제 없음.
- `places[*].npcs`의 `relation`·`frequency`는 `npc_associations` 메타데이터로 보존 — 현재 위치·확률 위치로 해석하지 않는다(영도 revision만 바뀜, LEGACY 4개 불변).

## 3. 이동 지식 정책 변경 (사용자 결정 — H04 의도적 supersede)

| 경우 | 동작 |
|---|---|
| PC·NPC 자율 이동 | 아는 구간만으로 경로가 있으면 그 후보(익숙한 길 우선) → 없으면 PUBLIC/LOCAL 일반 그래프(모르는 구간은 중립 비용·길 종류는 공개 분류로 거름) + 아는 RESTRICTED/SECRET |
| RESTRICTED/SECRET | 지식이 없으면 자율 경로에 넣지 않는다. 알아도 통과·권한은 `access.actual_access` |
| 저작 명시 사건 route | 연속성·출발/도착·실제 접근만 검증, 행위자 지식으로 거부하지 않음. wakeup은 남은 명시 구간을 그대로 이어 감 |
| 순수 지식 질의(`ROUTE_KNOWN_EXISTS`) | 아는 구간만(의미 불변) |

**supersede된 H04/H05 테스트(삭제 없이 새 정책 검증으로 교체):** `test_h04b_events_progression::test_r55_*`(→ `test_r55_h07_unknown_public_route_moves_but_never_uses_unknown_secret`) · `test_h04_r2_corrections::test_r2_04_valid_route_and_unknown_route_policy_unchanged`(명시 route 무지식 이동·출발점 불일치 거부) · `test_h05a_intent_planning::test_known_place_without_known_route_*`(→ 일반 그래프 출발·SECRET 비노출). 유지된 H04 불변식: 전지적 SECRET 금지·접근 검증·우회 없음·결정적 경로·정본 변이 경계.

## 4. 영도 레거시 퀘스트 44건 최종 매핑 (V2)

| 레거시 퀘스트 | 처분 | 사건 템플릿 | 보존·비고 |
|---|---|---|---|
| early_first_water | MIGRATED | yd_ev_thirst | 갈증 압박·고인 물=감염자 집결 규칙 보존. 끓이기/위험 음용 분기는 서사·추출(상태이상)로. |
| early_shelter | MIGRATED | yd_ev_first_night | 첫 밤 은신 압박·실내 고립 개체 규칙 보존. 반복 2회(원문 max 2) 유지. |
| early_first_sight | MIGRATED | yd_ev_first_sighting | 첫 조우·개체별 감각 손상 규칙 보존. 접근·도주 분기는 서사. |
| early_salvage_shore | MIGRATED | yd_ev_flotsam | 표류물·해산물 접촉 위험 보존. 원문 총 4회 → 장소별 최대 2회. |
| early_smoke | MIGRATED | yd_ev_smoke_column | 연기=조직의 통제된 불, 거점 첫 접촉 보존. 명시 결단 '달아난다' = hostile(행적 deviated — 원문 marked→run 보존). |
| early_road_choice | MIGRATED | yd_ev_crossroads | 고지대-물/저지대-물자 구조적 딜레마 보존(고지대 도달 시 노출). |
| join_namhang_labor | MIGRATED | yd_ev_namhang_gate_inspection | 노동력 심사·48시간 검역·하층부 배정 보존. 사정/허위 분기는 서사, 상처 은폐 발각은 검토 항목. |
| join_namhang_rescue | MIGRATED | yd_ev_namhang_lost_team | 실종 조·수습대장 출동(오프스크린 NPC 이동)·포기된 구조의 이면 보존. 허위 제보·흩어짐 분기는 서사. 생존자 동반 여부는 위치·시간으로만 판정 — 검토 항목. |
| join_namhang_trade | MIGRATED | yd_ev_namhang_barter_entry | 물품=입장료, 명부 '외부 조달' 표기 보존. 흥정·허풍 분기는 서사. 물품 조건(원문 item any)은 안쪽 문 접근 변형 + 사건 조건 OBSERVER_HAS_ITEM(V2)으로 보존. |
| join_namhang_smuggle | MIGRATED | yd_ev_namhang_infiltration, yd_ev_namhang_vent_cut, yd_ev_namhang_sewer_grate | 하수도·배기팬 침입 경로, 위원회 판정·조건부 수용, 상부가 경로를 알고 방치한 이면 보존. 떠나면 fled 취소, 명시 결단 '필요한 것만 챙겨 나간다' = thief(행적 deviated — 원문 결말 이력 보존). 민첩 10 = OBSERVER_STAT(V2). |
| join_haeryeon_silence | MIGRATED | yd_ev_haeryeon_ladder | 무소음 관찰·사다리 시험·사전 관찰 이면 보존. 소리침→거부 분기는 서사(대기 활동만 시험 진행). |
| join_haeryeon_water | MIGRATED | yd_ev_haeryeon_gutter | 물=목숨·거래 불가·자재 조달·절단된 홈통 이면 보존. 자재 소지 여부는 기계 판정 불가 — 검토 항목. |
| join_haeryeon_child | MIGRATED | yd_ev_crying_child | 우는 아이·소음 위협·아이를 받는 마을·방음실 쑥 향 이면 보존. 명시 결단 '아이를 두고 간다' = left_behind(행적 deviated). 아이는 정의 NPC가 아님. |
| join_haeryeon_watch | MIGRATED | yd_ev_haeryeon_night_watch | 야간 단독 파수·함정 선·사라진 사람 이면 보존. 파수꾼 = 강병철(원문 정적 파수꾼 선임) 배치 변경은 NEWLY_AUTHORED. |
| join_jodo_screening | MIGRATED | yd_ev_jodo_screening | 검문·신체 기록·사상 검증·24시간 격리(세계관 6.3.6.2)·폭력 이력 선별 이면 보존. 기술 인력 분류 분기는 서사. |
| join_jodo_signal | MIGRATED | yd_ev_jodo_radio | 무전 고장 소문·실력 시험·안테나 복구·상부의 의도적 차단 이면 보존. 기술 11 = 시험·복구 전이 조건 OBSERVER_STAT(V2). 첩자 판정 분기는 서사. |
| join_jodo_causeway | MIGRATED | yd_ev_causeway_stranded | 시간 압박 구조·400m 개활지·사격 없는 초소(탄약 고갈) 이면 보존. 미끼/업기는 서사, 명시 결단 '혼자 달린다' = survived_alone(행적 deviated). |
| join_jodo_defector | MIGRATED | yd_ev_jodo_defector | 이탈자·격리 구역 처분 이면·협조자/감시 대상 판정 보존. 신고/변호 분기는 서사. |
| join_jungri_debt | MIGRATED | yd_ev_jungri_grading | 값을 매기는 시장·3등급 채무·빠져나올 수 없는 이자 이면 보존. 흥정·허풍 분기는 서사. |
| join_jungri_goods | MIGRATED | yd_ev_jungri_valuables | 물건=신분·2등급 '유동' 표기 이면 보존. 물품 소지(원문 item any 8종) = OBSERVER_HAS_ITEM(V2). 표적화(targeted) 분기는 서사. |
| join_jungri_dive | MIGRATED | yd_ev_haenyeo_vacancy | 빈 잠수복·잠수 규칙·물 밖 보조 노동·수석 해녀 감염 은폐 이면 보존. 직접 잠수·이음매 파열 분기는 서사. 원문 deviated(파열 은폐)는 PC 잠수가 정본 모델에 없어 미표현 — 남은 한계(행적 사건 목록에서 제외). |
| join_jungri_courier | MIGRATED | yd_ev_jungri_courier | 무소속 운반인·봉인 꾸러미·세력 정보 양매 이면 보존. 민첩 11 = OBSERVER_STAT(V2), 명시 결단 '열어 챙긴다' = double_dealt(행적 deviated). |
| place_shop_scavenge | MIGRATED | yd_ev_shop_marks | 약탈된 상점 수색·표식 체계 이면 보존. 원문 총 5회 → 장소별 1회. |
| place_home_traces | MIGRATED | yd_ev_locked_room | 빈집 수색·나가지 못한 거주자 이면 보존. 원문 총 4회 → 장소별 1회. |
| place_coast_tide | MIGRATED | yd_ev_tide_window, yd_tide | 물때(정본 조석 상태)·조간대 변이 생물 이면 보존. 물때는 세계 사건 yd_tide가 소유. |
| place_high_vantage | MIGRATED | yd_ev_vantage | 고지대 조망·노출 위험·신호 가시성 이면 보존. 조망으로 지명 습득(공간 지식)은 미구현 — 검토 항목. |
| place_under_dark | MIGRATED | yd_ev_under_marks | 지하 암흑·방향 상실·지하 거주자 영역 표식 이면 보존. 원문 총 3회 → 장소별 1회. |
| place_store_haul | MIGRATED | yd_ev_claimed_store | 무게 딜레마·세력이 점찍은 창고 이면 보존. 원문 총 3회 → 장소별 1회. |
| place_public_record | MIGRATED | yd_ev_records | 초기 대피 기록·감염 판정 표시 이면 보존. 명시 결단 '태워 불을 얻는다' = destroyed(행적 deviated — 원문 root→burn 보존). |
| place_route_block | MIGRATED | yd_ev_barricade | 막힌 길·감염자 흐름 관리 이면 보존. 해체로 흐름 변경(접근 상태)은 미표현 — 검토 항목. |
| place_night_move | MIGRATED | yd_ev_night_road | 야간 암흑·불빛 노출·불 없이 다니는 야간 이동자 이면 보존. |
| place_utility_check | MIGRATED | yd_ev_failing_utility | 설비 노후·거점 간 부품 의존 이면 보존. 수리 성패의 정본 효과(설비 정지)는 미표현 — 검토 항목. |
| place_home_quarter | RETIRED | — | 이면정보·세계 결과가 없는 휴식 소품. DYNAMIC에서는 SLEEP 활동(코드 소유 시간·사건 중단)과 추출 상태·자원으로 대체. LEGACY 퀘스트로는 유지. |
| place_market_dig | RETIRED | — | 이면정보·세계 결과가 없는 획득 소품. DYNAMIC에서는 SEARCH 활동 + 묘사·추출 소지품, 시장 소문은 yd_ev_shop_marks·정보 거래소 서사로 대체. LEGACY 퀘스트로는 유지. |
| npc_keeper_ledger | MIGRATED | yd_ev_ledger_shortfall | 장부 불일치·야간 잠복·규정 vs 사정 이면 보존. 관리자-장소 쌍은 유효 위치(일과 포함)로 성립. 명시 결단 '혼자 쥔다' = leverage(행적 deviated). 원문 총 3회 → 쌍별 1회. |
| npc_worker_shift | MIGRATED | yd_ev_missing_shift | 빠진 교대·함께 일하기·검역 격리 은폐 이면 보존. 원문 총 4회 → 쌍별 1회. |
| npc_hidden_ill | MIGRATED | yd_ev_concealed_symptom | 숨기는 사람·감염 오인 가능성 이면 보존. 고발은 서사, 명시 결단 '함께 감춘다' = complicit(행적 deviated). 원문 1회 유지. |
| main_namhang_order | SPLIT | yd_ev_namhang_ration_crisis, yd_ev_namhang_clinic_records | 배급 격차→집결→대치 압력 단계를 세계 진행으로, 의료소 생체 연구 이면을 독립 노출 사건으로 분리. 결말 3종(규율·개혁·전복) = 대치 국면 플레이어 명시 결단(resolution) → ending 사실 + 문·배치 결과(V2). 미결단 시 위원회 질서 회복(outcome, 결말 아님). |
| main_haeryeon_water | SPLIT | yd_ev_haeryeon_drought, yd_ev_haeryeon_soundroom | 가뭄→재절단→회의 압력 단계와 방음실 이면을 분리. 결말 3종(나눔·폐쇄·폭로) = 회의 국면 명시 결단 → ending + 사다리·후방선 접근 결과(V2). 미결단 시 위원회 폐쇄(outcome). |
| main_jodo_command | SPLIT | yd_ev_jodo_closure_order, yd_ev_jodo_armory_check, yd_ev_jodo_radio_truth | 폐쇄 명령→방파제 봉쇄(정본 접근 SHUT)→동요 압력 단계, 탄약 고갈·무전 차단 이면을 각각 노출. 결말 3종 = 동요 국면 명시 결단 → ending + 방파제 접근 결과(V2). 미결단 시 폐쇄 유지(outcome). |
| main_jungri_ledger | SPLIT | yd_ev_jungri_debt_strike, yd_ev_jungri_ledger_audit | 채무 거부→바자르 정지 압력 단계와 등급제 설계 이면을 분리. 결말 3종 = 봉기 국면 명시 결단 → ending(V2). 미결단 시 장부 유지(outcome). |
| main_alone_survive | SPLIT | yd_ev_lone_winter, yd_ev_lone_traces | 무소속 겨울 압력(정본 날짜 기반 첫서리·겨울)과 앞선 무소속자 흔적 이면을 분리. 결말 4종 = 한겨울 국면 명시 결단 → ending(V2). 동료 NPC 조건은 미표현. |
| main_alone_escape | MIGRATED | yd_ev_boat_rumor | 해상 탈출 충동·바다 사람의 경고·돌아오지 못하는 바다 이면 보존. 출항 결말 3종 = 경고 국면 명시 결단 → ending(V2, 정본 이동 없음 — 바다는 이동 구간이 아니다). |
| main_upheaval | MIGRATED | yd_ev_island_balance | 원문 min_outcome deviated 2 = 행적 사실(conduct, 8개 사건의 명시 결단) 둘 이상으로 개방(V2 — V1의 '비밀 2개 인지' 대체는 폐기). 분업 균형 이면 보존. 결말 3종 = 통찰 국면 명시 결단 → ending. |

**조건 의미 보존:** 소지품 `item any` → `OBSERVER_HAS_ITEM`(+남항 안쪽 문 접근 변형) · `min_stat` → `OBSERVER_STAT` · `min_outcome deviated 2` → 행적 사실(`EVENT_LOCAL conduct`, 명시 결단 8건) 둘 이상 · V1의 '비밀 2개 인지' 대체는 폐기. `join_jungri_dive`의 deviated(파열 은폐)는 PC 잠수가 정본 모델에 없어 미표현(남은 한계).

## 5. 레거시 권위 은퇴 목록 (H07-C — 정의 바인딩 세션)

판정 `player_world.legacy_narrative_retired(session)` = 정의 revision 바인딩 또는 세계 생애주기 초기화. 바인딩 없는 LEGACY 세션은 그대로.
지시층위 퀘스트 블록·info_ledger 블록 / 묘사층위 `PromptBuilder.add_quest_block` / 런타임 지시 스키마의 `quest_choice`·`quest_case`·`info_access`(원본 스키마 불변) / 지시효과 스테이징(퀘스트 선택·무작위 선정·intended_case·info_ledger) / 추출 hidden 인지 조건 / 커밋의 퀘스트 진전·이면 인지·메인 해금·인피니티 엔딩·정적 상주 동행 해제 / `!자동 퀘스트`.
호환 뷰(새 권위 아님): `player_faction` = RESOLVED 사건의 `grant_faction` 중 최신 · 지시층위 `[PC가 알게 된 것]` = PC CONTENT 정보 요약(info_ledger 대체).

## 6. 프롬프트 권위 정합 요약 (H07-D)

4개 층위 시스템 지시문에 **동적 세계 블록이 있을 때만** 적용되는 절을 추가했다(LEGACY 의미 불변): 정본 세계 권위·레거시 퀘스트/원장 비권위·장면 인물은 장면 블록만·국면 지침은 맥락(결과 아님)·자유 행동 억지 분기 매핑 금지·결단 대신 선택/유도 금지·세계의 독립 압력(정본 밖 사실 확정 금지)·표시명·노출 ≠ 믿음. 판단층위: 매니페스트 키만·resolution은 명시 선언만·선택지 권유 금지. 추출층위: `quest_progress` 레거시 전용·노출은 묘사된 것만.
크기: 묘사 +319 · 추출 +464 · 판단 +286 · 지시 +734자(계 +1,803자, 토크나이저 없이 문자 기준 측정). provider 호출 지점 **19 유지**(새 호출 단계 없음).

## 7. 최종 게이트 결과

| 항목 | 결과 |
|---|---|
| full pytest | **1,183 passed**(특성화 61 · 결함 27 · 정책 1,095) · strict xfail 0 · 실패 0 |
| H07 테스트 | npc_position 30 · movement_policy 11 · place_display 7 · resolution 8 · legacy_retirement 6 · prompt_authority 3 · behavior_gates 11 · compat 5 · yeongdo_draft 19 · runtime_support 11 |
| T-1~T-10 | 전부 PASS(`tests/policy/test_h07_behavior_gates.py`; T-7은 `test_h07_movement_policy.py` 11건 포함) |
| compileall · 검증 루틴 ①②③④⑤⑥ | OK · cogs 9 · 명령어 44 · views 5 |
| `scenario_check --all` | 5개 ERROR 0 · LEGACY revision 불변(ee967f1c… / c03dbb42… / 56f6f2ef… / 8a8c11f2…) · 영도 `d3a01114…` · 반복 컴파일 결정적 |
| 영도 8개 시작 틀 genesis | READY(`test_h07_yeongdo_draft` 파라미터·`test_every_start_frame_*`) |
| restart·rewind·rerender·cache reissue | H06 테스트 유지 통과 + 스냅샷 drift 무시·결단 재서술·선택 키 미저작 정의 유효(`test_h07_compat`) |
| 성능 | genesis 5회 1.04/1.09/0.99/1.01/1.14초(중앙 1.04) · 24h 배회 10.9초 · 12h 대기 1회 약 1.5초 · 세계 90 KB · genesis 정본 배치 1(PC) · 일과 정의 3.2 KB(35명) · 정의 1.56 MB |

## 남은 한계

**DEFERRED EXISTING DEFECT(범위 밖 — 최소 수정 조건 미해당):** `core/places.py` `build_move_hint`의 미정의 `static_npcs`(LEGACY 이동 힌트 누락) · `quest.apply_grants`의 info_ledger dict 가정(실제 list) · `quest.load_quest_data`의 현재 파일 읽기 — 셋 다 DYNAMIC에서는 레거시 퀘스트 은퇴로 닿지 않고, LEGACY 동작은 기존과 같다.

**OPEN DECISION(사용자 확정 필요):**
1. 일과 판정 잠정 항목 — R2 다중 `자주`(조원달·신대호·장철웅은 행동 근거 R3 사용), R4(손재현·이동철·박인철), 이현석(행동 근거가 원문 연고 '가끔'과 다름).
2. untracked 12명(최민준·장우진·박하은·배형식·김태호·나용순·조한·장현수·박미래·권상태·신창호·백인하) 중 일과를 저작할 인물.
3. override 해제 뒤 복귀 규칙은 "명시 CLEAR → 즉시 현재 일과"로 구현했다(이동 시뮬레이션 없음). 귀환 이동을 보여 줄지(START_MOVEMENT 후 CLEAR)는 사건 저작 선택.
4. NPC 공간 지식의 역할은 경로 선택 품질(아는 길 우선·숨은 길)로 구현했다. 위험 회피·우회 성향의 세부 가중은 미구현.
5. 결단 확인 단계 — 판단층위가 명시 선언에서만 키를 고르도록 규칙·스키마로 제한했으나 별도 재확인(ASK) 단계는 두지 않았다.
6. 지도 재보정 — V1 지리(실제 영도 지리 기반 230쌍)를 그대로 유지했다. 새 근거가 없어 V2에서 구간을 바꾸지 않았다.

**알려진 성능·표현 한계:** 추출 스키마의 레거시 `quest_progress`·`secret_awareness`가 DYNAMIC에서도 요청된다(값은 무시 — 토큰 낭비) · 모르는 공개 구간은 중립 비용이라 지식이 없을 때 최단이 아닌 길을 고를 수 있다(지식 = 품질 정책) · 행적 사건 일부(은폐·운반)는 relevance 장소가 없어 결단 후보가 장면과 무관하게 열릴 수 있다 · 소지품·능력치 조건은 플레이어 턴 진행에서만 평가된다(오프스크린 진행에서는 UNKNOWN).
