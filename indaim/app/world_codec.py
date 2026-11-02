"""세션 저장 형식 v5의 세계 부분(리팩터링 T-6, 커널 명세 v1 §7·§9).

data.json의 옛 세계 필드(v4 — 이름 키 23필드)는 저장하지 않고, 커널 세계(엔티티·컴포넌트)와 세계 밖 PC 서술
프로필로 저장한다. 메모리의 세션 필드는 그 세계의 작업 사본이다(로드 = 역변환, 저장 = 변환).
· 저장 전에 왕복(역변환(변환(필드)) = 장부 사본을 뺀 필드)을 확인한다 — 어긋나면 저장하지 않는다(값 손실 금지).
· 커밋마다 그 버전의 세계를 world/<VersionId>.json에 한 번 보관한다(K2 가 — 보존 무제한, 보관용).
· v4 세션은 로드할 때 data.v4.json으로 원본을 보존하고, 변환할 수 없으면 그 세션을 열지 않는다(K5 가).
"""
from __future__ import annotations

import copy
import json
import os

from indaim.kernel import World
from indaim.migrate import v4

FIELDS = v4.FIELDS
WORLD_KEY, PROFILE_KEY, ORDER_KEY = "world", "world_profile", "world_key_order"


class WorldCodecError(RuntimeError):
    """세계를 v5로 변환·역변환할 수 없다(추정 변환 없음 — 저장·열기를 막는다)."""


def _fields(d: dict) -> dict:
    out = {f: [] if f in v4.NAME_LISTS or f == "world_pending_signals" else {} for f in FIELDS}
    out["world_version"] = None
    out.update({k: copy.deepcopy(v) for k, v in d.items() if k in out and v is not None})
    return out


def _game_keys(scenario_data) -> set:
    from indaim.rules.growth import components as _g
    return _g.game_keys(scenario_data or {})


def encode(data: dict, scenario_data=None) -> dict:
    """직렬화 사전(옛 세계 필드 포함) → v5(세계 필드 대신 world·world_profile). 왕복이 어긋나면 WorldCodecError."""
    fields = _fields({f: data.get(f) for f in FIELDS})
    try:
        conv = v4.convert(fields, game_keys=_game_keys(scenario_data))
        orders = v4.key_orders(fields)
        back = v4.apply_key_orders(v4.revert(conv), orders)
    except Exception as e:  # noqa: BLE001
        raise WorldCodecError(f"세계 변환 실패: {type(e).__name__}: {e}") from e
    want = v4.strip_ledger(fields)
    bad = sorted(f for f in FIELDS if _norm(back.get(f)) != _norm(want.get(f))
                 or (isinstance(want.get(f), dict) and list(back.get(f) or {}) != list(want.get(f))))
    if bad:
        detail = ""
        for f in bad[:2]:
            a, b = back.get(f), want.get(f)
            if isinstance(a, dict) and isinstance(b, dict):
                ks = [k for k in sorted(set(a) | set(b), key=str) if _norm(a.get(k)) != _norm(b.get(k))][:2]
                detail += f" {f}: " + "; ".join(f"{k}: 옛 {str(_norm(b.get(k)))[:200]} ≠ 왕복 {str(_norm(a.get(k)))[:200]}" for k in ks)
        raise WorldCodecError(f"세계 왕복 불일치: {bad}{detail}")
    out = {k: v for k, v in data.items() if k not in FIELDS}
    out[WORLD_KEY] = conv.world.to_dict()
    out[PROFILE_KEY] = conv.profile
    out[ORDER_KEY] = orders
    return out


def _norm(v):
    """의미 비교(안쪽 사전 순서 무시 — 최상위 키 순서는 따로 확인)."""
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str) if v is not None else None


def decode(data: dict) -> dict:
    """v5 사전 → 옛 세계 필드를 채운 사전(세계 키는 뺀다). v4 사전은 그대로."""
    if WORLD_KEY not in data:
        return data
    try:
        conv = v4.Converted(world=World.from_dict(data[WORLD_KEY]), profile=data.get(PROFILE_KEY) or {})
        fields = v4.apply_key_orders(v4.revert(conv), data.get(ORDER_KEY) or {})
    except Exception as e:  # noqa: BLE001
        raise WorldCodecError(f"세계 역변환 실패: {type(e).__name__}: {e}") from e
    out = {k: v for k, v in data.items() if k not in (WORLD_KEY, PROFILE_KEY, ORDER_KEY)}
    out.update(fields)
    return out


def archive(session_id: str, world: dict) -> None:
    """그 버전의 세계를 한 번 보관(이미 있으면 그대로 — 버전 정체성은 커밋이 정한다). 실패는 저장을 막지 않는다."""
    vid = ((world or {}).get("version") or {}).get("version_id")
    if not vid:
        return
    path = f"sessions/{session_id}/world/{vid}.json"
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(world, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        print(f"⚠️ [세계 보관] {session_id}/{vid}: {e}")


def preserve_v4(session_id: str) -> None:
    """v4 원본 data.json을 data.v4.json으로 한 번 보존(덮어쓰지 않음)."""
    src, dst = f"sessions/{session_id}/data.json", f"sessions/{session_id}/data.v4.json"
    if os.path.isfile(src) and not os.path.exists(dst):
        import shutil
        shutil.copyfile(src, dst)
