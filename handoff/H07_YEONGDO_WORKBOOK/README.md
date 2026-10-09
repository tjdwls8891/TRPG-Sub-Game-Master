# H07 영도 저작 워크북 (저작 근거 · 런타임 아님)

`scenarios/영도.json`의 H07 추가분(달력·이동 방식·구간·접근·seed·world_start·정보·사건·NPC actor_id/knowledge_seed)을
사람이 정한 표에서 만든 저작 도구다. **정본은 시나리오 JSON**이며, 이 폴더는 각 값의 출처를 검토하기 위한 근거다.

| 파일 | 내용 |
|---|---|
| `geo.py` | 좌표 스케치(x·y·고도), 원본 connected 234쌍 전부의 판정(BI/RE/SKIP + 사유), 신규 장소 3곳, 지형 계수, 공개 지명, 접근 제어 배치 |
| `build_geo.py` | 좌표·지형 → 거리(m)·시간 계수(Tobler 보행 함수 × 지형)·방향 힌트. 보행 속도 48 m/min |
| `apply.py` | 달력·이동 방식·접근 규칙·seed·시작 틀 world_start. `--write`를 줄 때만 시나리오를 쓴다 |
| `ev_lib.py` | 사건 조건식·step 헬퍼 |
| `infos.py` | information_definitions(퀘스트 이면정보 원문 보존 + 공개 사실) |
| `events_author.py` | 사건 템플릿·genesis 배치/초기 인지·레거시 퀘스트 매핑(MAP) |

재생성: `python3 handoff/H07_YEONGDO_WORKBOOK/apply.py <repo>` (후보만 만든다) · `--write`(시나리오 덮어쓰기 — 명시 저작 단계).
도구는 역방향 구간을 자동으로 만들지 않는다 — 모든 양방향 구간은 `geo.py`의 쌍별 판정 표에 사유와 함께 적혀 있다.
