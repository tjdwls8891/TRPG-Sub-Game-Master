# H07 영도 저작 워크북 V2 (H07 최종 통합 구현 지시 반영 · 저작 근거, 런타임 아님)

V1(`handoff/H07_YEONGDO_WORKBOOK/`, H07-G 체크포인트 `99f490e`)은 과거 기록으로 보존한다. 이 V2가 현재
`scenarios/영도.json`을 재현한다(멱등 — 현 파일에 다시 돌려도 결과가 같다).

| 파일 | V1 대비 변경 |
|---|---|
| `ev_lib.py` | 결단(`chose`·`undecided`·`P(options=…)`)·override 해제(`clear_npc`)·행적(`conduct`·`conduct_of`)·결말 사실(`ending`·`outcome`)·관찰자 조건(`has_item`·`stat_gte`) |
| `events_author.py` | NPC 정본 배치 47명 → `ROUTINES`(35, 근거 규칙 R1~R6)·`UNTRACKED`(12)·`GENESIS_CANONICAL`(0). R-6 이동용 seed 제거. 수습대 순찰 정본 이동 제거(일과가 위치 소유). 사건이 만든 override는 종료 시 `CLEAR_ACTOR_PLACEMENT`. 메인 결말 7아크 = 플레이어 명시 결단(`resolution_options`) → `ending` 사실 + 접근·배치 결과, 미결단 시 세계 결과(`outcome`, 결말 아님). 원문 `deviated` 결말 8건 = 명시 결단 → 행적 `conduct`, `main_upheaval` 개방 = 행적 둘 이상(원문 min_outcome 의미 그대로). 소지품·능력치 조건 = `OBSERVER_HAS_ITEM`·`OBSERVER_STAT` |
| `infos.py` | 거점 이름 발견 정보 4건(`reveals` — 거점 경계에서 사람과 마주쳐 이름을 듣는다) |
| `apply.py` | `yd_namhang_runner` seed 폐기, `PLACE_DISPLAY`(이름이 세력·인물을 누설하는 경계 장소 8곳의 `unknown_name`) |
| `geo.py`·`build_geo.py` | 변경 없음(V1과 같은 지도·구간 판정) |

재생성: `python3 handoff/H07_YEONGDO_WORKBOOK_V2/apply.py <repo>`(후보만 — `$TMPDIR/yd_candidate.json`) · `--write`(시나리오 덮어쓰기 — 명시 저작 단계).
분류·근거·남은 판단 항목은 `handoff/H07_FINAL_IMPLEMENTATION_REPORT.md`.
