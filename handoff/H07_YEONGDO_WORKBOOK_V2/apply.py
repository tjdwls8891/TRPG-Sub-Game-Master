# -*- coding: utf-8 -*-
"""워크북 전체 → 영도 raw(메모리). --write 시에만 scenarios/영도.json을 쓴다(사람이 실행하는 저작 단계)."""
import copy
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import geo  # noqa: E402
import build_geo  # noqa: E402

START_DATE = "2016-10-02"     # D+90 (세계관 9절 '현재') — CALIBRATED, 원문에 날짜 없음

CALENDAR = {
    "calendar_id": "yd_gregorian_2016",
    "type": "GREGORIAN",
    # 시간대 id = 원본 intro_times·퀘스트 time_of_day 어휘. 경계는 2016년 10월 초 부산 일출 ~06:20·일몰 ~18:05 기준.
    "dayparts": {
        "새벽": {"start": "04:30", "end": "06:20", "representative": "05:30"},
        "이른 아침": {"start": "06:20", "end": "08:00", "representative": "07:00"},
        "오전": {"start": "08:00", "end": "11:30", "representative": "10:00"},
        "한낮": {"start": "11:30", "end": "14:30", "representative": "13:00"},
        "오후": {"start": "14:30", "end": "17:30", "representative": "16:00"},
        "해질 무렵": {"start": "17:30", "end": "18:40", "representative": "18:00"},
        "밤": {"start": "18:40", "end": "04:30", "representative": "22:00"},
    },
}

MODES = {"walk": {"base_speed_m_per_min": build_geo.SPEED}}

# 접근 규칙 — 상태 이름·결과는 저작값(장르 상태를 core에 두지 않는다)
def _member(cap):
    return [{"requires_capability": cap, "result": "PASSABLE", "override_id": f"{cap.replace(':', '_')}_pass"}]


ACCESS = {
    "rules": {
        "yd_guarded_door": {"rule_type": "STATE_TABLE", "states": {
            # 외부인은 경비에게 신원을 밝히고(확인) 지난다. 그 세력 구성원은 그냥 지난다.
            "GUARDED": {"result": "ACTION_REQUIRED", "action_policy": "CONFIRM", "action_delay_minutes": 5,
                        "actor_overrides": _member("member:namhang") + _member("member:jodo")},
            "OPEN": {"result": "PASSABLE"},
            "SHUT": {"result": "IMPASSABLE"}}},
        "yd_namhang_gate": {"rule_type": "STATE_TABLE", "states": {
            # 외부인: 별도 행동(검역 통과·위원회 판정) 없이는 못 지난다. 연대가 목마른 물품을 가진 자는 그 자리에서
            # 넘기고 지난다(가입 퀘스트 '값을 치르고 들어가다' — PC capability = 소지품 이름).
            "SEALED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION",
                       "actor_overrides": _member("member:namhang") + [
                           {"requires_capability": item, "result": "ACTION_REQUIRED", "action_policy": "CONFIRM",
                            "action_delay_minutes": 15, "override_id": f"namhang_trade_{n}"}
                           for n, item in enumerate(["발전기 부품", "정수 필터", "항생제", "공구", "연료"])]},
            "ADMITTED": {"result": "PASSABLE"},
            "LOCKDOWN": {"result": "IMPASSABLE", "actor_overrides": _member("member:namhang")}}},
        "yd_haeryeon_gate": {"rule_type": "STATE_TABLE", "states": {
            "SEALED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION",
                       "actor_overrides": _member("member:haeryeon")},
            "ADMITTED": {"result": "PASSABLE"},
            "LOCKDOWN": {"result": "IMPASSABLE", "actor_overrides": _member("member:haeryeon")}}},
        "yd_jodo_gate": {"rule_type": "STATE_TABLE", "states": {
            "SEALED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION",
                       "actor_overrides": _member("member:jodo")},
            "ADMITTED": {"result": "PASSABLE"},
            "LOCKDOWN": {"result": "IMPASSABLE", "actor_overrides": _member("member:jodo")}}},
        "yd_pulley_ladder": {"rule_type": "STATE_TABLE", "states": {
            "RAISED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION",
                       "actor_overrides": _member("member:haeryeon")},
            "LOWERED": {"result": "PASSABLE"}}},
        "yd_mesh_grate": {"rule_type": "STATE_TABLE", "states": {
            "MESHED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION"},
            "CUT": {"result": "PASSABLE"}}},
        "yd_tidal_path": {"rule_type": "STATE_TABLE", "states": {
            "HIGH_TIDE": {"result": "PASSABLE"},
            "LOW_TIDE": {"result": "IMPASSABLE"}}},
    },
    "controls": {
        "namhang_outer_door": {"rule_ref": "yd_guarded_door", "initial_state": "GUARDED"},
        "namhang_inner_door": {"rule_ref": "yd_namhang_gate", "initial_state": "SEALED"},
        "namhang_vent_mesh": {"rule_ref": "yd_mesh_grate", "initial_state": "MESHED"},
        "namhang_sewer_grate": {"rule_ref": "yd_mesh_grate", "initial_state": "MESHED"},
        "haeryeon_ladder": {"rule_ref": "yd_pulley_ladder", "initial_state": "RAISED"},
        "haeryeon_rear_line": {"rule_ref": "yd_haeryeon_gate", "initial_state": "SEALED"},
        "jodo_causeway_barricade": {"rule_ref": "yd_guarded_door", "initial_state": "GUARDED"},
        "jodo_inner_gate": {"rule_ref": "yd_jodo_gate", "initial_state": "SEALED"},
        "jungri_tide_path": {"rule_ref": "yd_tidal_path", "initial_state": "LOW_TIDE"},
    },
}

# 시작 틀 — 무작위 장소·시각 슬롯을 틀마다 고정 문안으로(정본 world_start와 일치). 그 밖의 서사 슬롯은 유지.
FRAME_STARTS = {
    "arrive_plain": ("청학동 선착장", "05:40", "새벽"),
    "arrive_finding": ("동삼동 갯가", "07:10", "이른 아침"),
    "arrive_person": ("물양장", "10:20", "오전"),
    "arrive_threat": ("봉래동 물류창고", "13:00", "한낮"),
    "arrive_person_finding": ("감지해변", "16:10", "오후"),
    "arrive_threat_finding": ("흰여울 절벽길", "17:55", "해질 무렵"),
    "arrive_person_threat": ("영선동 방파제", "11:50", "한낮"),
    "arrive_night": ("감지해변", "21:40", "밤"),
}


def seeds(rows):
    pub = set(geo.PUBLIC_PLACES)
    pub_edges = sorted(e["id"] for e in rows if e.get("knowledge_visibility") == "PUBLIC")
    out = {
        # 본토 생존자: 사태 이전 부산 사람이 아는 영도의 공개 지명과 큰길만(세력 거점 이름·내부·비밀 통로 없음)
        "yd_newcomer": {"known_places": sorted(pub), "known_edges": pub_edges},
    }
    # V2: NPC 이동 허용용 seed(yd_namhang_runner, R-6)는 폐기 — 공간 지식은 이동 허가가 아니다(H07 최종 지시 §6).
    return out


# V2 §5: 내부 PlaceId ≠ 플레이어 표시명. 이름 자체가 세력·인물을 누설하는 경계 장소만 unknown_name을 둔다(저작).
#     실제 이름은 명시 발견(information reveals — 거점 경계에서 사람과 마주침)으로만 안다.
PLACE_DISPLAY = {
    "남항 중공업 연대": "크레인이 선 서쪽 조선소",
    "제1방어선": "조선소 방벽 쪽",
    "해련 자치 마을": "산 중턱 불빛이 걸린 빌라촌",
    "하단 방어선": "콘크리트로 메운 빌라 아래쪽",
    "조도 방위 사령부": "방파제 건너편 캠퍼스",
    "다중 검문소": "방파제 끝 철조망",
    "중리 교역소": "동남쪽 천막촌",
    "전성남의 은신처": "안쪽에서 잠긴 점검구",
}


def apply_geo(raw):
    raw = copy.deepcopy(raw)
    for k, v in geo.NEW_PLACES.items():
        raw["places"][k] = copy.deepcopy(v)
        for nb in v["connected"]:
            cs = raw["places"][nb].setdefault("connected", [])
            if isinstance(cs, str):
                cs = raw["places"][nb]["connected"] = [cs]
            if k not in cs:
                cs.append(k)
    rows, ledger, decisions = build_geo.build(raw)
    raw.setdefault("scenario_schema", {"version": 1, "capabilities": {}})
    raw["scenario_schema"] = {"version": 1, "capabilities": {
        "structured_places": 1, "quantitative_time": 1, "stable_actors": 1, "dynamic_actor_location": 1,
        "information_registry": 1, "actor_cognition": 1, "dynamic_events": 1, "scheduled_world_progression": 1}}
    raw["calendar"] = copy.deepcopy(CALENDAR)
    raw["movement_modes"] = copy.deepcopy(MODES)
    raw["access_control_definitions"] = copy.deepcopy(ACCESS)
    raw["movement_edges"] = rows
    raw["spatial_knowledge_seeds"] = seeds(rows)
    for pid, node in raw["places"].items():
        node.pop("unknown_name", None)
        if pid in PLACE_DISPLAY:
            node["unknown_name"] = PLACE_DISPLAY[pid]
    for fr in raw["start_frames"]:
        place, hhmm, daypart = FRAME_STARTS[fr["id"]]
        slots = fr.setdefault("slots", {})
        if "장소" in slots:
            slots["장소"] = place
        if "때" in slots:
            slots["때"] = daypart
        fr["world_start"] = {"place": place, "date": START_DATE, "time": hhmm, "knowledge_seed": "yd_newcomer"}
    return raw, ledger, decisions


def full(raw):
    raw, ledger, decisions = apply_geo(raw)
    import events_author
    raw = events_author.apply(raw)
    return raw, ledger, decisions


if __name__ == "__main__":
    root = sys.argv[1]
    sys.path.insert(0, root)
    raw0 = json.load(open(os.path.join(root, "scenarios/영도.json"), encoding="utf-8"))
    raw, ledger, decisions = full(raw0)
    if "--write" in sys.argv:
        s = json.dumps(raw, ensure_ascii=False, indent=2)
        open(os.path.join(root, "scenarios/영도.json"), "w", encoding="utf-8").write(s)
        print("written")
    json.dump(raw, open(os.path.join(os.environ.get("TMPDIR", "/tmp"), "yd_candidate.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("candidate ok")
