# 장소 표시명 — 내부 PlaceId와 플레이어 표시명의 분리 (H07)
#
# [권위 — H07 최종 통합 구현 지시 §5]
#   내부 정본 해석(배치·구간·사건·예약)은 언제나 PlaceId다. 플레이어에게 보이는 이름만 PC 지식 수준으로 고른다.
#     · display_name(저작, 없으면 PlaceId) — 이름을 아는 PC에게 보이는 이름.
#     · unknown_name(저작) — 이름을 모르는 PC에게 보이는 서술형 이름(세력·비밀을 누설하지 않는 문구).
#   unknown_name이 저작된 장소는 '이름이 스스로 드러나지 않는 곳'이다 — 보거나(출구 학습) 가 보는 것만으로는
#   이름을 알지 못하고, 명시 발견(정보 reveals — NPC 제보·문서·지도·수색·사건 효과)이나 저작 seed로만 안다
#   (spatial_knowledge.places[pid].knows_name = True). unknown_name이 없는 장소는 이름이 곧 공개 지명이다.
#   PC가 존재조차 모르는 장소(지식 기록 없음)는 표시명이 없다(None) — 호출자가 이름 없이 서술한다.
from __future__ import annotations

from . import spatial as _sp

GENERIC_UNKNOWN = "이름 모를 곳"


def _rec(definition: dict, pid: str) -> dict:
    return (definition.get("place_definitions") or {}).get(pid) or {}


def known_display(definition: dict, pid: str) -> str:
    return _rec(definition, pid).get("display_name") or pid


def needs_discovery(definition: dict, pid: str) -> bool:
    """이름이 스스로 드러나지 않는 장소(unknown_name 저작)."""
    return bool(_rec(definition, pid).get("unknown_name"))


def knows_true_name(definition: dict, knowledge: dict, pid: str) -> bool:
    if not needs_discovery(definition, pid):
        return True
    rec = (knowledge.get("places") or {}).get(pid)
    return isinstance(rec, dict) and rec.get("knows_name") is True


def display_name(definition: dict, knowledge: dict, pid: str, *, require_known: bool = True):
    """PC 지식 수준의 표시명. require_known이면 PC가 존재를 모르는 장소는 None."""
    if pid is None:
        return None
    if require_known and not _sp.knows_place(knowledge, pid):
        return None
    if knows_true_name(definition, knowledge, pid):
        return known_display(definition, pid)
    return _rec(definition, pid).get("unknown_name") or GENERIC_UNKNOWN


def display_for(definition: dict, world, actor_id: str, pid: str, *, require_known: bool = True):
    return display_name(definition, _sp.actor_knowledge(world, actor_id), pid, require_known=require_known)


def seen_place_fields(definition: dict, pid: str) -> dict:
    """그 자리에서 보이는 장소(출구 너머·도착지)의 지식 필드 — 이름은 스스로 드러나는 곳만 안다."""
    return {"knows_name": not needs_discovery(definition, pid)}


def route_label(definition: dict, knowledge: dict, edge_ids, best_edge_ids, origin: str, n: int) -> str:
    """경로 후보 라벨(플레이어 지식 수준). 1) 첫 분기 구간 route_hint 2) 최단 후보에 없는 첫 경유지(PC가 아는
    장소면 표시명) 3) 첫 분기 구간 도착지 방향(아는 장소면) 4) 이름 없는 길 번호. 모르는 장소 이름은 쓰지 않는다."""
    edges = definition.get("movement_edges") or {}
    path, best = tuple(edge_ids), tuple(best_edge_ids)
    div = 0 if path == best else next((i for i, e in enumerate(path) if i >= len(best) or best[i] != e), 0)
    hint = (edges.get(path[div]) or {}).get("route_hint") if path else None
    if hint:
        return hint
    best_places = {origin} | {edges[e]["to"] for e in best if e in edges}
    for e in path[div:-1]:
        to = edges[e]["to"]
        if to not in best_places:
            nm = display_name(definition, knowledge, to)
            if nm:
                return f"{nm} 경유"
    if path:
        nm = display_name(definition, knowledge, edges[path[div]]["to"])
        if nm:
            return f"{nm} 방향"
    return f"잘 모르는 길 {n}"
