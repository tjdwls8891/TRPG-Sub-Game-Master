# -*- coding: utf-8 -*-
"""H03-B 합성 공간 시나리오(시나리오 원본 아님 — 테스트 전용 저작 데이터).

경로: 항구→성당 세 갈래(시장·산복도로·해안) + 비밀 통로(SECRET) + 수로(water).
시장→성당 구간에는 잠긴 문(door_1, 초기 LOCKED)이 있다.
"""
import copy

SPATIAL_RAW = {
    "scenario_schema": {"version": 1, "capabilities": {"structured_places": 1}},
    "default_npcs": {},
    "places": {
        "섬": {},
        "항구": {"parent": "섬"},
        "시장": {"parent": "섬"},
        "산복도로": {"parent": "섬"},
        "해안": {"parent": "섬"},
        "성당": {"parent": "섬", "visible_within": ["시장"]},
        "외딴곳": {},
    },
    "movement_modes": {
        "walk": {"base_speed_m_per_min": 80, "forbidden_edge_tags": ["water"]},
        "boat": {"base_speed_m_per_min": 200, "allowed_edge_tags": ["water"]},
    },
    "access_control_definitions": {
        "rules": {"door_rule": {"rule_type": "STATE_TABLE", "states": {
            "OPEN": {"result": "PASSABLE"},
            "CLOSED": {"result": "ACTION_REQUIRED", "action_policy": "IMPLICIT"},
            "LOCKED": {"result": "IMPASSABLE", "actor_overrides": [
                {"requires_capability": "key_door", "result": "ACTION_REQUIRED",
                 "action_policy": "CONFIRM", "action_delay_minutes": 1}]},
            "BARRED": {"result": "ACTION_REQUIRED", "action_policy": "SEPARATE_ACTION"}}}},
        "controls": {"door_1": {"rule_ref": "door_rule", "initial_state": "LOCKED"}},
    },
    "movement_edges": [
        {"id": "e_hm", "from": "항구", "to": "시장", "bidirectional": True, "distance_m": 400},
        {"id": "e_mc", "from": "시장", "to": "성당", "distance_m": 300, "access_ref": "door_1"},
        {"id": "e_hh", "from": "항구", "to": "산복도로", "distance_m": 600, "route_hint": "산복도로 쪽"},
        {"id": "e_hc", "from": "산복도로", "to": "성당", "distance_m": 300},
        {"id": "e_hs", "from": "항구", "to": "해안", "distance_m": 500, "route_hint": "해안도로 쪽",
         "movement_tags": ["road"], "base_time_factor": 1.2},
        {"id": "e_sc", "from": "해안", "to": "성당", "distance_m": 500, "fixed_delay_minutes": 2},
        {"id": "e_secret", "from": "항구", "to": "성당", "distance_m": 100, "knowledge_visibility": "SECRET"},
        {"id": "e_ferry", "from": "항구", "to": "해안", "distance_m": 1000, "movement_tags": ["water"]},
    ],
    "spatial_knowledge_seeds": {
        "local": {"familiarity_scopes": ["섬"], "known_access_facts": []},
        "outsider": {"known_places": ["항구"], "known_edges": ["e_hm"]},
    },
}


def spatial_raw(**patch):
    raw = copy.deepcopy(SPATIAL_RAW)
    raw.update(copy.deepcopy(patch))
    return raw
