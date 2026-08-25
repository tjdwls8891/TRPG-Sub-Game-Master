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
        "companions": {
            "type": "object",
            "description": "동행 상태 변화. 변화가 없으면 빈 배열 둘.",
            "properties": {
                "joined": {
                    "type": "array", "items": {"type": "string"},
                    "description": "이번 턴에 동행을 시작한 인물. 함께 가기로 하거나 따라나선 경우."
                },
                "left": {
                    "type": "array", "items": {"type": "string"},
                    "description": "이번 턴에 동행이 끝난 인물. 헤어지거나 남거나 죽은 경우."
                }
            },
            "required": ["joined", "left"]
        },
        "secret_awareness": {
            "type": "integer",
            "description": (
                "플레이어가 숨겨진 사실을 알아차린 정도 0~100. "
                "0-30 전혀 모름 / 31-70 의심하거나 단서를 접함 / 71-100 명확히 인지. "
                "숨겨진 사실이 제시되지 않았으면 0."
            )
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
        "quest_progress", "npcs_met", "situation", "secret_awareness",
        "companions",
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
    "secret_reveal": 71,     # 이상이면 이면정보를 인지한 것으로 처리
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
    data.setdefault("secret_awareness", 0)
    comp = data.get("companions")
    if not isinstance(comp, dict):
        comp = {}
    comp.setdefault("joined", [])
    comp.setdefault("left", [])
    data["companions"] = comp
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
        """'미확인'은 기존 값을 덮어쓰지 않는다. 기존 값도 없으면 '미확인'."""
        if not new or new == "미확인":
            return old if old else "미확인"
        return new

    tl["current_date"] = _keep(dt.get("date"), tl.get("current_date"))
    tl["time_of_day"] = _keep(dt.get("time_of_day"), tl.get("time_of_day"))
    tl["current_location"] = _keep(loc.get("name"), tl.get("current_location"))
    tl["faction_context"] = _keep(loc.get("faction_context"), tl.get("faction_context"))
    return tl


def apply_extraction(session, result: dict) -> dict:
    """추출 수치를 임계값과 대조해 세션 상태에 적용한다.

    [설계 원칙]
    추출층위는 0~100 수치만 보고하고 그 값이 어떤 결과를 낳는지 모른다.
    임계값 비교와 실제 적용은 이 함수(코드)가 전담한다.

    적용 대상:
      - 상태이상 부여/해제 (status_scores vs status_apply / status_clear)
      - 만난 NPC 기록 (npcs_met)
    적용 제외:
      - 자원(item_changes)은 지시층위 resource_changes 필드 소관.
        추출층위 값은 참고용으로만 남기고 여기서 적용하지 않는다.
      - 퀘스트 진행·이탈은 서사 계층에서 소비한다(설계문서 3).

    Returns:
        {"applied": [...], "cleared": [...], "npcs": [...]} 적용 내역
    """
    th = get_thresholds(session)
    applied, cleared = [], []

    # 유효 캐릭터명 — 등록된 PC·NPC만 허용 (일반 명사 차단)
    valid = set()
    try:
        valid |= {p.get("name") for p in (session.players or {}).values() if p.get("name")}
        valid |= set((session.npcs or {}).keys())
    except Exception:
        pass

    # 유효 상태이상 이름 — 시나리오에 목록이 있으면 그 안으로 제한
    valid_status = None
    try:
        eff = (session.scenario_data or {}).get("status_effects")
        if isinstance(eff, dict):
            valid_status = set(eff.keys())
        elif isinstance(eff, list):
            valid_status = {e.get("name") for e in eff if isinstance(e, dict) and e.get("name")}
    except Exception:
        pass

    for entry in (result.get("status_scores") or []):
        if not isinstance(entry, dict):
            continue
        target = entry.get("target")
        status = entry.get("status")
        try:
            score = int(entry.get("score", 0))
        except (TypeError, ValueError):
            continue
        if not target or not status:
            continue
        if valid and target not in valid:
            print(f"[추출 무시] {target};{status} — 등록되지 않은 캐릭터 이름")
            continue
        if valid_status is not None and status not in valid_status:
            print(f"[추출 무시] {target};{status} — 유효 상태이상 목록에 없음")
            continue

        current = session.statuses.setdefault(target, [])
        if score >= th["status_apply"]:
            if status not in current:
                current.append(status)
                applied.append(f"{target};{status}({score})")
        elif score <= th["status_clear"]:
            if status in current:
                current.remove(status)
                cleared.append(f"{target};{status}({score})")

    # 만난 NPC — 중복 없이 누적
    npcs = [n for n in (result.get("npcs_met") or []) if isinstance(n, str) and n]

    return {"applied": applied, "cleared": cleared, "npcs": npcs}


def resource_changes_to_tags(changes: list) -> str:
    """지시층위의 resource_changes를 기존 자원 태그 문자열로 변환한다.

    NOTE: 모델이 보는 형식은 독립 필드로 강제하되, 내부 파이프라인은
          game.py에 이미 구현된 검증(등록 캐릭터명 확인 등)을 재사용한다.
          태그 파서가 공백을 허용하지 않으므로 언더스코어로 치환한다.
    """
    tags = []
    for c in (changes or []):
        if not isinstance(c, dict):
            continue
        target = str(c.get("target", "")).strip().replace(" ", "_")
        item = str(c.get("item", "")).strip().replace(" ", "_")
        try:
            delta = int(c.get("delta", 0))
        except (TypeError, ValueError):
            continue
        if not target or not item or delta == 0:
            continue
        tags.append(f"자:{target};{item};{delta:+d}")
    return " ".join(tags)


def apply_companions(session, data: dict) -> dict:
    """동행 상태를 갱신한다.

    만난 인물(met_npcs)과 동행 중인 인물(companions)을 분리 관리한다.
    스쳐 지나간 사람과 함께 움직이는 사람은 다르며, 퀘스트 인물 필터가
    이를 구분하지 못하면 부정확해진다.

    동행은 명시적으로 끝나기 전까지 유지된다.

    Returns:
        {"joined": [...], "left": [...]}
    """
    comp = data.get("companions") or {}
    current = list(getattr(session, "companions", []) or [])
    met = list(getattr(session, "met_npcs", []) or [])

    # 만난 기록은 누적된다. 동행 여부와 무관하다.
    for name in (data.get("npcs_met") or []):
        if isinstance(name, str) and name and name not in met:
            met.append(name)

    joined = []
    for name in (comp.get("joined") or []):
        if isinstance(name, str) and name and name not in current:
            current.append(name)
            joined.append(name)
            if name not in met:
                met.append(name)

    left = []
    for name in (comp.get("left") or []):
        if name in current:
            current.remove(name)
            left.append(name)

    session.companions = current
    session.met_npcs = met
    return {"joined": joined, "left": left}


def release_resident_companions(session, new_location: str) -> list:
    """장소를 옮기면 그곳 상주 NPC는 동행에서 자동 해제한다.

    차봉순이 병참 창고를 떠나 저지대까지 따라올 리 없다.
    상주가 아닌 인물은 계속 동행할 수 있다.
    """
    from .places import load_places

    current = list(getattr(session, "companions", []) or [])
    if not current:
        return []
    places = load_places(getattr(session, "scenario_data", {}) or {})
    if not places:
        return []

    resident = {}
    for pname, node in places.items():
        if not isinstance(node, dict):
            continue
        for entry in (node.get("npcs") or []):
            if isinstance(entry, dict) and entry.get("frequency") == "상주":
                resident[entry.get("name")] = pname

    released = []
    for name in list(current):
        home = resident.get(name)
        if home and home != new_location:
            current.remove(name)
            released.append(name)
    session.companions = current
    return released


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
