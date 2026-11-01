"""선언 검증 게이트 — 응답 정규화·차단/질문 판정·발견 결과·판단/지시층위 블록(cogs/gm.py에서 옮김)."""
from __future__ import annotations

from indaim.turn import turn_world as _turn_world


# ========== [v7.14 선언 게이트 — 역사칭·주변 발견] ==========
# 개연성 시뮬레이션(기존 캐시 호출)의 구조화 판정. 판단·지시층위에는 열거값과 플레이어 원문 대상명만 넘긴다
# (시뮬레이션 자유 서술·공리·방향은 판단층위로 가지 않는다 — 캐시의 숨은 설정 누설 방지).
GATE_CLASSES = ("ORDINARY", "AMBIENT_PROPOSAL", "IMPROPER_ASSERTION")


GATE_RESOLUTIONS = ("CONTINUE", "ACCEPT_NO_ROLL", "ROLL", "BLOCK", "CLARIFY")


GATE_REASONS = ("NONE", "CANNOT_CONFIRM", "WORLD_CONFLICT", "UNCLEAR_INTENT", "NOT_AMBIENT", "UNCHANGED_RETRY")


GATE_TARGET_MAX = 30

# 중립 안내 — 이유는 공개 범주만(비밀의 존재·위치를 암시하지 않도록 WORLD_CONFLICT·NOT_AMBIENT는 같은 문구)
_GATE_NOTICE_CLARIFY = "무엇을, 어디에서, 어떻게 하시려는지 조금 더 구체적으로 선언해 주십시오."


_GATE_NOTICE_DEFAULT = "그 결과는 선언으로 정할 수 없습니다. 인물이 직접 시도할 행동으로 다시 선언해 주십시오."


_GATE_NOTICE_BLOCK = {
    "CANNOT_CONFIRM": "선언하신 결과나 상대의 반응은 미리 정할 수 없습니다. 인물이 무엇을 시도하는지로 다시 선언해 주십시오.",
    "UNCHANGED_RETRY": "같은 방법으로는 다시 찾아볼 수 없습니다. 범위·방법·도구 등 무엇이 달라졌는지 함께 선언해 주십시오.",
}


def _parse_gate(raw, player_message: str, *, dynamic: bool, place=None, place_name=None, rows=()) -> dict | None:
    """시뮬레이션 declaration_gate → 코드 검증된 게이트. 열거값이 깨졌으면 None(검증 불가).

    · 대상명은 플레이어 원문에 (공백 무시) 실제로 있는 낱말만 — 아니면 CLARIFY.
    · LEGACY(P6): 주변 발견 승인·판정은 일반 행동으로 되돌린다(BLOCK·CLARIFY·역사칭 제지만 적용).
    · 이미 이 장소에서 발견한 같은 대상(existing_key 또는 같은 이름)은 새로 만들지 않고 재사용한다.
    """
    if not isinstance(raw, dict):
        return None
    c, res, rc = raw.get("classification"), raw.get("resolution"), raw.get("reason_code")
    if c not in GATE_CLASSES or res not in GATE_RESOLUTIONS or rc not in GATE_REASONS:
        return None
    gate = {"classification": c, "resolution": res, "reason_code": rc, "target": "", "kind": "NONE",
            "existing_info_id": None, "place": None, "place_name": None, "referenced_info_id": None}
    # A1 보정: 선언이 이 장소의 기존 발견물(D키)을 가리킨다는 시뮬레이션 구조화 근거(분류 무관, 검증된 키만)
    _ref = next((r for r in rows if r[0] == raw.get("existing_key")), None)
    if _ref is not None and res not in ("BLOCK", "CLARIFY"):
        gate["referenced_info_id"] = _ref[1]
    if c != "AMBIENT_PROPOSAL":
        if res not in ("CONTINUE", "BLOCK", "CLARIFY"):
            gate["resolution"] = "CONTINUE"
    elif res in ("ACCEPT_NO_ROLL", "ROLL"):
        target = raw.get("discovery_target")
        target = " ".join(target.split()) if isinstance(target, str) else ""
        kind = raw.get("discovery_kind")
        if not dynamic or not place:
            gate.update(classification="ORDINARY", resolution="CONTINUE", reason_code="NONE")
        elif (not target or len(target) > GATE_TARGET_MAX or kind not in _turn_world.DISCOVERY_KINDS
              or "".join(target.split()) not in "".join(str(player_message or "").split())):
            gate.update(resolution="CLARIFY", reason_code="UNCLEAR_INTENT")
        else:
            gate.update(target=target, kind=kind, place=place, place_name=place_name or place)
            hit = (next((r for r in rows if r[0] == raw.get("existing_key")), None)
                   or next((r for r in rows if _turn_world.same_target(r[2], target)), None))
            if hit:
                gate.update(resolution="ACCEPT_NO_ROLL", existing_info_id=hit[1], target=hit[2], kind=hit[3],
                            referenced_info_id=hit[1])
    elif res not in ("BLOCK", "CLARIFY"):
        gate["resolution"] = "CONTINUE"
    if gate["resolution"] in ("BLOCK", "CLARIFY") and gate["reason_code"] == "NONE":
        gate["reason_code"] = "UNCLEAR_INTENT" if gate["resolution"] == "CLARIFY" else "CANNOT_CONFIRM"
    return gate


def _gate_blocks(gate) -> bool:
    return bool(gate) and gate.get("resolution") in ("BLOCK", "CLARIFY")


def _gate_rolls_discovery(gate) -> bool:
    return bool(gate) and gate.get("classification") == "AMBIENT_PROPOSAL" and gate.get("resolution") == "ROLL"


def _gate_notice(gate) -> str:
    if gate.get("resolution") == "CLARIFY":
        return _GATE_NOTICE_CLARIFY
    return _GATE_NOTICE_BLOCK.get(gate.get("reason_code"), _GATE_NOTICE_DEFAULT)


def _gate_judgment_block(gate) -> str:
    """판단층위 입력 — 열거값 + 플레이어 원문 대상명만(시뮬레이션 서술 없음)."""
    if not gate:
        return ""
    line = f"분류 {gate['classification']} / 처리 {gate['resolution']} / 사유 {gate['reason_code']}"
    if gate.get("target"):
        line += f" / 대상 {gate['target']}({gate['kind']})"
    return "[선언 게이트 — 코드 확정(공개 범주)]\n" + line


def _discovery_outcome(jr, transaction_id) -> dict | None:
    """판단 기록(게이트 + 굴림 기록) → 이번 턴 발견 결과. 다른 판정의 성공은 쓰지 않는다(그 굴림의 성패만).

    status: NEW(승인·성공 — 새 발견) / EXISTING(이미 발견한 것) / FAILED(이번에 찾지 못함) / None(발견 아님)
    """
    gate = (jr or {}).get("gate")
    if not gate or gate.get("classification") != "AMBIENT_PROPOSAL" \
            or gate.get("resolution") not in ("ACCEPT_NO_ROLL", "ROLL") or not gate.get("place"):
        return None
    out = {"target": gate["target"], "kind": gate["kind"], "place": gate["place"],
           "place_name": gate.get("place_name") or gate["place"], "info_id": gate.get("existing_info_id")}
    if gate.get("existing_info_id"):
        return {**out, "status": "EXISTING"}
    if gate["resolution"] == "ROLL":
        rec = next((r for r in (jr.get("roll_records") or ()) if isinstance(r, dict)
                    and isinstance(r.get("discovery"), dict)), None)
        if rec is None or not rec["discovery"].get("success"):
            return {**out, "status": "FAILED"}
    return {**out, "status": "NEW", "info_id": _turn_world.ambient_info_id(transaction_id)}


def _declaration_block(jr, transaction_id) -> str:
    """지시층위 — 권위 있는 시도(판단 player_intent) · 게이트 분류 · 이번 턴 발견 결과."""
    jr = jr or {}
    intent = ((jr.get("judgment") or {}).get("player_intent") or "").strip()
    gate = jr.get("gate")
    lines = []
    if intent:
        lines.append(f"· 권위 있는 시도(판단층위 확정): {intent}")
    lines.append("· 플레이어 문장 속 결과 선취(성공·상대의 동의/반응·발견 결과)는 확정 사실이 아니다 — 시도로만 다루고 "
                 "결과는 판정·세계 상태·위 지시로 정한다.")
    if gate and gate.get("classification") == "IMPROPER_ASSERTION":
        lines.append("· 이번 선언은 결과를 미리 정했다. 그 시도만 진행한다.")
    d = _discovery_outcome(jr, transaction_id)
    if d is not None:
        what = f"'{d['target']}'({'물건' if d['kind'] == 'OBJECT' else '환경 요소'})"
        if d["status"] == "FAILED":
            lines.append(f"· 주변 발견 {what}: 이번 탐색에서는 찾지 못했다. 그것이 없다고 단정하지 말 것.")
        elif d["status"] == "EXISTING":
            lines.append(f"· 주변 발견 {what}: PC가 이 장소에서 이미 발견해 둔 것이다(그 자리에 있다).")
        else:
            how = "판정 성공" if gate.get("resolution") == "ROLL" else "자명함(무판정 승인)"
            lines.append(f"· 주변 발견 {what}: {how} — 이 장소에서 찾아냈다. 평범한 것으로만 서술하고, 핵심 단서·고유 물건·"
                         f"숨은 길로 키우지 말 것.")
        lines.append("· 발견은 소지가 아니다 — 플레이어가 줍거나 쓴다고 선언했을 때만 획득·사용으로 서술한다.")
    return "\n[선언 처리 — 코드 확정]\n" + "\n".join(lines) + "\n"
