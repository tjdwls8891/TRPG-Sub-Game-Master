# 추출층위 — 묘사 출력물에서 세계 상태·수치를 추출하고 임계값과 대조해 적용
#
# [설계 원칙 — 판단을 수치로 하기]
#   추출층위에는 '적용 기준'을 알려주지 않는다. 범위별 가이드만 제시하고,
#   임계값 비교와 실제 상태 적용은 이 모듈(코드)이 담당한다.
#   이분적 판단(부여할까 말까)을 모델에 위임하지 않고 기준을 확실히 하기 위함이다.
#
# [캐시 미사용]
#   추출은 출력물에서 값을 읽어내는 작업이므로 세계관 룰북 참조가 불필요하다.
#   따라서 세션 캐시를 읽지 않는다(비용 절감).

# ========== [공통 추출 타겟] ==========
# 시나리오별 타겟은 scenario_data["extraction_targets"]로 추가 병합된다.
COMMON_EXTRACTION_TARGETS = [
    "날짜 및 시간",
    "위치",
    "아이템 변화",
    "상태이상 평가 수치",
    "퀘스트 진행도 평가",
    "만난 NPC 목록",
    "턴의 상황",
]

# ========== [응답 스키마] ==========
# 모든 수치 필드는 0~100 정수. 모델은 이 값이 어떻게 쓰이는지 알지 못한다.
EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "datetime": {
            "type": "object",
            "description": "이번 묘사 시점의 작중 날짜·시간대.",
            "properties": {
                "date": {"type": "string", "description": "작중 날짜. 확인 불가면 '미확인'."},
                "time_of_day": {"type": "string", "description": "시간대(새벽·아침·낮·저녁·밤 등). 확인 불가면 '미확인'."},
            },
            "required": ["date", "time_of_day"],
        },
        "location": {
            "type": "object",
            "description": "이번 묘사 종료 시점의 PC 소재지.",
            "properties": {
                "name": {"type": "string", "description": "위치명. 확인 불가면 '미확인'."},
                "faction_context": {
                    "type": "string",
                    "description": "이 위치를 관할·점유하는 세력이나 지역 규칙. 확인 불가면 '미확인'.",
                },
            },
            "required": ["name", "faction_context"],
        },
        "item_changes": {
            "type": "array",
            "description": "이번 묘사에서 확인된 소지품 증감만. 변화가 없으면 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "소지 주체(캐릭터명)"},
                    "item": {"type": "string", "description": "품목명"},
                    "delta": {"type": "integer", "description": "증감 수량. 획득은 양수, 소모·상실은 음수."},
                },
                "required": ["target", "item", "delta"],
            },
        },
        "status_scores": {
            "type": "array",
            "description": (
                "이번 묘사에서 징후가 관찰된 상태를 0~100으로 평가한다. "
                "0-30 징후 없음 / 31-70 조짐 있음 / 71-100 명백함. "
                "징후가 전혀 없으면 빈 배열."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "대상 캐릭터명"},
                    "status": {"type": "string", "description": "상태 이름(부상·중독·탈진 등)"},
                    "score": {"type": "integer", "description": "0~100. 묘사에 드러난 정도만으로 평가."},
                },
                "required": ["target", "status", "score"],
            },
        },
        "quest_progress": {
            "type": "object",
            "description": "현재 서사 목표 대비 이번 묘사의 진행·이탈 정도.",
            "properties": {
                "advance": {
                    "type": "integer",
                    "description": "목표 방향으로 나아간 정도 0~100. 0-30 정체 / 31-70 진전 / 71-100 결정적 진전.",
                },
                "deviation": {
                    "type": "integer",
                    "description": "목표에서 벗어난 정도 0~100. 0-30 궤도 내 / 31-70 방향 차이 / 71-100 이탈.",
                },
            },
            "required": ["advance", "deviation"],
        },
        "npcs_met": {
            "type": "array",
            "description": "이번 묘사에 등장하거나 PC와 접촉한 NPC 이름. 없으면 빈 배열.",
            "items": {"type": "string"},
        },
        "situation": {
            "type": "object",
            "description": "이번 묘사가 만들어낸 장면의 성격.",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "장면 성격을 한 단어로(전투·추적·잠입·대화·이동·휴식·의식 등).",
                },
                "tension": {
                    "type": "integer",
                    "description": "긴장도 0~100. 0-30 평온 / 31-70 경계 / 71-100 위기.",
                },
            },
            "required": ["tag", "tension"],
        },
    },
    "required": [
        "datetime", "location", "item_changes", "status_scores",
        "quest_progress", "npcs_met", "situation",
    ],
}

# ========== [임계값] ==========
# 시나리오 JSON의 extraction_thresholds로 항목별 오버라이드 가능.
# NOTE: 실제 적용(apply_extraction)은 4.5.0에서 연결된다. 4.4.0은 추출·보관까지만 수행.
THRESHOLDS = {
    "status_apply": 71,      # 이상이면 상태 부여
    "status_clear": 30,      # 이하이면 기존 상태 해제
    "quest_advance": 71,     # 이상이면 케이스 진전
    "quest_deviated": 71,    # 이상이면 재계획 트리거
    "tension_high": 71,      # 이상이면 긴장 국면 (BGM 전환 판단에 사용)
}


def get_thresholds(session) -> dict:
    """시나리오 오버라이드를 반영한 임계값을 반환한다."""
    merged = dict(THRESHOLDS)
    try:
        override = (session.scenario_data or {}).get("extraction_thresholds") or {}
        for k, v in override.items():
            if k in merged and isinstance(v, int):
                merged[k] = v
    except Exception:
        pass
    return merged


def build_extraction_targets(session) -> list:
    """공통 타겟과 시나리오별 타겟을 병합해 반환한다.

    시나리오 JSON의 extraction_targets는 문자열 배열로, 공통 7종 뒤에 덧붙는다.
    중복은 제거하며 공통 타겟의 순서는 보존한다.
    """
    targets = list(COMMON_EXTRACTION_TARGETS)
    try:
        extra = (session.scenario_data or {}).get("extraction_targets") or []
        for t in extra:
            if isinstance(t, str) and t not in targets:
                targets.append(t)
    except Exception:
        pass
    return targets


def parse_extraction(raw: str) -> dict | None:
    """추출 응답 텍스트를 파싱·검증한다. 실패 시 None.

    response_schema로 구조가 강제되지만, 코드블럭 래핑이나 부분 누락에
    대비해 방어적으로 처리한다.
    """
    import json
    import re

    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        try:
            data = json.loads(cleaned)
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    # 누락 필드를 기본값으로 채워 하류 코드가 KeyError를 겪지 않게 한다.
    data.setdefault("datetime", {"date": "미확인", "time_of_day": "미확인"})
    data.setdefault("location", {"name": "미확인", "faction_context": "미확인"})
    data.setdefault("item_changes", [])
    data.setdefault("status_scores", [])
    data.setdefault("quest_progress", {"advance": 0, "deviation": 0})
    data.setdefault("npcs_met", [])
    data.setdefault("situation", {"tag": "미확인", "tension": 0})
    return data


def to_world_timeline(result: dict, existing: dict | None = None) -> dict:
    """추출 결과에서 세계 타임라인 필드를 갱신해 반환한다.

    NOTE: 기존 _update_world_timeline(별도 API 호출)을 흡수한 경로다.
          호출을 하나 줄이면서 동일한 필드를 유지한다.
    """
    tl = dict(existing or {})
    dt = result.get("datetime") or {}
    loc = result.get("location") or {}

    def _keep(new, old):
        """'미확인'은 기존 값을 덮어쓰지 않는다."""
        return old if (not new or new == "미확인") else new

    tl["current_date"] = _keep(dt.get("date"), tl.get("current_date"))
    tl["time_of_day"] = _keep(dt.get("time_of_day"), tl.get("time_of_day"))
    tl["current_location"] = _keep(loc.get("name"), tl.get("current_location"))
    tl["faction_context"] = _keep(loc.get("faction_context"), tl.get("faction_context"))
    return tl


def summarize_for_report(result: dict) -> str:
    """마스터 채널 보고용 한 줄 요약을 만든다."""
    loc = (result.get("location") or {}).get("name", "미확인")
    dt = (result.get("datetime") or {}).get("time_of_day", "미확인")
    sit = result.get("situation") or {}
    qp = result.get("quest_progress") or {}
    npcs = result.get("npcs_met") or []
    parts = [
        f"위치 {loc}",
        f"시간대 {dt}",
        f"장면 {sit.get('tag', '미확인')}(긴장 {sit.get('tension', 0)})",
        f"진행 {qp.get('advance', 0)} / 이탈 {qp.get('deviation', 0)}",
    ]
    if npcs:
        parts.append(f"NPC {', '.join(npcs[:5])}")
    return " · ".join(parts)
