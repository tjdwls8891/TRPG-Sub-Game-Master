import os
import re
import json
import copy
import random
import asyncio
import time
import dataclasses
from types import SimpleNamespace
import discord
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core

# 프롬프트 중앙 등록소에서 AI 프롬프트 상수 및 빌더 임포트
from prompts import (
    CACHE_TIME_RESPONSE_SCHEMA,
    CACHE_TIME_SYSTEM_INSTRUCTION,
    EXTRACTION_SYSTEM_INSTRUCTION,
    NPC_DETAIL_RESPONSE_SCHEMA,
    NPC_DETAIL_SYSTEM_INSTRUCTION,
    IRREGULAR_NPC_RESPONSE_SCHEMA,
    IRREGULAR_NPC_SYSTEM_INSTRUCTION,
    JUDGMENT_RESPONSE_SCHEMA,
    JUDGMENT_SYSTEM_INSTRUCTION,
    GM_LOGIC_RESPONSE_SCHEMA,
    GM_LOGIC_SYSTEM_INSTRUCTION,
    NARRATIVE_PLAN_SCHEMA,
    NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
    build_narrate_prompt,
    # 방안 B — 세계 물리 타임라인
    # 방안 E — PROCEED 자기 검증
    # 방안 6 — 2단계 사고 서사 방향성 시뮬레이터
    NARRATIVE_DIRECTION_SCHEMA,
    NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION,
)


# ========== [GM 상수] ==========
# NOTE: 지시층위 호출 시 한 플레이어 발언당 내부 루프 반복 상한.
# 판단층위에 주입할 최근 로그 개수 (캐시 미사용이므로 맥락을 직접 공급)
JUDGMENT_RECENT_LOGS = 5
# 판단층위 모델 — 우선 DEFAULT_MODEL 유지. 실측 후 저비용 모델 교체 검토.
JUDGMENT_MODEL = core.DEFAULT_MODEL
# 층위별 자체 재시도 횟수
JUDGMENT_MAX_RETRIES = 2
EXTRACTION_MODEL = core.DEFAULT_MODEL
EXTRACTION_MAX_RETRIES = 2
# 추출층위 사고 예산 — 기계적 판독 작업이므로 제한한다. 0이면 사고 비활성.
EXTRACTION_THINKING_BUDGET = int(os.getenv("EXTRACTION_THINKING_BUDGET", "512"))
GM_LOGIC_MAX_RETRIES = 2

MAX_ITERATIONS_PER_MESSAGE = 5

# NOTE: 같은 플레이어 발언에 대한 ASK 누적 상한. 초과 시 강제 PROCEED.
MAX_CLARIFY_PER_MESSAGE = 2

# NOTE: 같은 플레이어 발언에 대한 NARRATE 누적 상한. 초과 시 강제 PROCEED.
MAX_NARRATE_PER_MESSAGE = 7

# NOTE: GM 비용 로그 라벨에 부착하는 접두사.
COST_LOG_PREFIX = "[GM] "


# NOTE: GM_LOGIC_RESPONSE_SCHEMA, GM_LOGIC_SYSTEM_INSTRUCTION,
#       NARRATIVE_PLAN_SCHEMA, NARRATIVE_PLANNER_SYSTEM_INSTRUCTION 은
#       prompts.py로 이동. 위 import 블록에서 불러옵니다.


# ========== [v7.14 선언 게이트 — 역사칭·주변 발견] ==========
# 개연성 시뮬레이션(기존 캐시 호출)의 구조화 판정. 판단·지시층위에는 열거값과 플레이어 원문 대상명만 넘긴다
# (시뮬레이션 자유 서술·공리·방향은 판단층위로 가지 않는다 — 캐시의 숨은 설정 누설 방지).
GATE_CLASSES = ("ORDINARY", "AMBIENT_PROPOSAL", "IMPROPER_ASSERTION")
GATE_RESOLUTIONS = ("CONTINUE", "ACCEPT_NO_ROLL", "ROLL", "BLOCK", "CLARIFY")
GATE_REASONS = ("NONE", "CANNOT_CONFIRM", "WORLD_CONFLICT", "UNCLEAR_INTENT", "NOT_AMBIENT", "UNCHANGED_RETRY")
GATE_TARGET_MAX = 30
# 중립 안내 — 이유는 공개 범주만(비밀의 존재·위치를 암시하지 않도록 WORLD_CONFLICT·NOT_AMBIENT는 같은 문구)
_GATE_NOTICE_CLARIFY = "무엇을, 어디에서, 어떻게 하시려는지 조금 더 구체적으로 선언해 주십시오."
_GATE_NOTICE_BLOCK = {
    "CANNOT_CONFIRM": "선언하신 결과나 상대의 반응은 미리 정할 수 없습니다. 인물이 무엇을 시도하는지로 다시 선언해 주십시오.",
    "UNCHANGED_RETRY": "같은 방법으로는 다시 찾아볼 수 없습니다. 범위·방법·도구 등 무엇이 달라졌는지 함께 선언해 주십시오.",
}
_GATE_NOTICE_DEFAULT = "그 결과는 선언으로 정할 수 없습니다. 인물이 직접 시도할 행동으로 다시 선언해 주십시오."
GATE_UNVERIFIABLE_NOTICE = "지금은 그 탐색 결과를 확인할 수 없습니다. 다른 행동을 선언해 주십시오."


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
        elif (not target or len(target) > GATE_TARGET_MAX or kind not in core.turn_world.DISCOVERY_KINDS
              or "".join(target.split()) not in "".join(str(player_message or "").split())):
            gate.update(resolution="CLARIFY", reason_code="UNCLEAR_INTENT")
        else:
            gate.update(target=target, kind=kind, place=place, place_name=place_name or place)
            hit = (next((r for r in rows if r[0] == raw.get("existing_key")), None)
                   or next((r for r in rows if core.turn_world.same_target(r[2], target)), None))
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
    return {**out, "status": "NEW", "info_id": core.turn_world.ambient_info_id(transaction_id)}


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


# ========== [유틸리티 함수] ==========
def _cap_display(cap, *, is_cost: bool = False) -> str:
    """턴/비용 상한을 표시용 문자열로. None이면 '무제한'."""
    if cap is None:
        return "무제한"
    return core.format_cost(cap) if is_cost else f"{cap}턴"


def _clean_proceed_instruction(instruction: str) -> str:
    """
    지시층위가 생성한 proceed_instruction에서 마크다운 서식을 제거하고 단일 자연어 서술문으로 정제.
    """
    if not instruction:
        return ""
    lines = instruction.strip().splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r'^[#\s]+', '', line)
        line = re.sub(r'^[-*+>\s]+(?=[^\s])', '', line)
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
        line = re.sub(r'\*([^*]+)\*', r'\1', line)
        line = line.strip()
        if line:
            cleaned.append(line)
    result = ' '.join(cleaned)
    return re.sub(r'\s+', ' ', result).strip()


def _npc_stats(session, name: str, world_turn=None) -> dict | None:
    """판정 대상 NPC의 정본 능력치(session.npcs[*].stats). 확인할 수 없으면 None — 허구 수치 없음.

    DYNAMIC은 현재 장면 인물의 표시명으로만 찾는다(표시명이 겹치거나 장면 밖이면 확인 불가).
    """
    key = name
    if world_turn is not None:
        try:
            _scene, projs = core.turn_world.scene_npc_projections(world_turn.ctx, None, session)
        except Exception:
            projs = []
        hits = [p for p in projs if p.get("display_name") == name]
        if len(hits) != 1:
            return None
        key = hits[0].get("legacy_name")
    data = (getattr(session, "npcs", None) or {}).get(key) if key else None
    stats = data.get("stats") if isinstance(data, dict) else None
    return stats if isinstance(stats, dict) and stats else None


def _judge_side(view, side: dict, name: str, world_turn=None) -> dict:
    """한 판정 당사자의 목표치 조립. A는 코드가 정본 원스탯에서 환산, S는 지목 스킬이 PC 프로필에 있을 때만(v7.43),
    M·H·D는 판단층위 계약값(검증 통과분). 능력치를 확인할 수 없으면 target=None(굴리지 않음)."""
    stat = str(side.get("stat") or "")
    uid = core.get_uid_by_char_name(view, name)
    skill, s = "", 0
    if uid:
        _prof = (view.players.get(uid) or {}).get("profile") or {}
        raw = _prof.get(stat)
        s = core.judgment.skill_mod(_prof, side.get("skill"))
        skill = str(side.get("skill") or "").strip() if s else ""
    else:
        raw = (_npc_stats(view, name, world_turn) or {}).get(stat)
    a = core.judgment.ability_mod(raw)
    diff = side.get("difficulty") if side.get("difficulty") in core.judgment.DIFFICULTY else "보통"
    m = int(side.get("method_mod") or 0)
    h = int(side.get("status_mod") or 0)
    d = core.judgment.DIFFICULTY[diff]
    out = {"name": name, "stat": stat, "is_pc": bool(uid), "uid": uid, "difficulty": diff,
           "A": a, "S": s, "skill": skill, "M": m, "H": h, "D": d,
           "method_basis": str(side.get("method_basis") or ""),
           "status_basis": str(side.get("status_basis") or ""), "T": None}
    if a is None:
        return out
    out["T"] = core.judgment.target(a, s, m, h, d)
    terms = [(a, "능력"), (s, f"숙련 {skill}"), (m, "행동"), (h, "상태"), (d, f"난이도 {diff}")]
    _terms = "".join(f"{v:+d}({n})" for v, n in terms if v)
    out["breakdown"] = f"{core.judgment.BASE}{_terms}={out['T']}" if _terms else f"{out['T']}"
    return out


def _judge_plan(view, spec: dict, world_turn=None) -> dict:
    """rolls 항목 → {kind, actor, opponent}. 계약 검증은 판단층위 수신 시 끝났다(여기서 값을 보정하지 않는다)."""
    kind = spec.get("kind") if spec.get("kind") in core.judgment.KINDS else "GENERAL"
    actor = _judge_side(view, spec, str(spec.get("char_name") or view.gm_target_char or "?"), world_turn)
    opp = None
    if kind == "CONTEST" and isinstance(spec.get("opponent"), dict):
        o = spec["opponent"]
        opp = _judge_side(view, o, str(o.get("name") or "?"), world_turn)
    return {"kind": kind, "actor": actor, "opponent": opp}


def _build_judgment_user_prompt(session, player_message: str, roll_results: list,
                                world_turn=None, gate=None, declaration_draft: bool = False,
                                discovery_rows=()) -> str:
    """
    판단층위 호출용 사용자 프롬프트 조립.

    NOTE: 판단층위는 세션 캐시를 읽지 않는다(비용 절감). 따라서 캐시에 들어 있는
          시나리오 룰북·세계관에 접근할 수 없으므로, 판단에 필요한 최소 정보를
          여기서 직접 주입한다. 주입 항목은 아래 셋으로 한정한다:
            ① 최근 로그 N개 (JUDGMENT_RECENT_LOGS)
            ② 플레이어 능력치 목록 (rolls 명세 작성용)
            ③ session.note (실시간 제약)

    Args:
        session: TRPGSession
        player_message (str): 플레이어 신규 발언
        roll_results (list[str]): 직전 ROLL 결과 (있으면 PROCEED 우선 판단 근거)
    """
    lines = []

    # ① 최근 로그
    recent = []
    for content in session.raw_logs[-JUDGMENT_RECENT_LOGS:]:
        try:
            text = content.parts[0].text
        except (AttributeError, IndexError):
            continue
        if text:
            role = "GM" if getattr(content, "role", "") == "model" else "플레이어"
            recent.append(f"[{role}]: {text}")
    lines.append("[최근 대화 맥락]\n" + ("\n\n".join(recent) if recent else "(없음)"))

    # ② 플레이어 프로필 — ROLL 판정 명세 및 상황 판단의 근거
    # NOTE: players는 {uid: {"name", "profile", "appearance"}} 구조다.
    #       uid가 아니라 name을 써야 하며, profile이 스탯 본체다.
    #       자원·상태이상은 session.resources / session.statuses에 별도 보관된다.
    prof_lines = []
    for _uid, p_data in (session.players or {}).items():
        if not isinstance(p_data, dict):
            continue
        c_name = p_data.get("name") or "(이름 미상)"
        prof_lines.append(f"- {c_name}: [스탯] {p_data.get('profile') or '(미배분)'}")
        if p_data.get("appearance"):
            prof_lines.append(f"    외형: {p_data['appearance']}")
        c_res = (session.resources or {}).get(c_name, {})
        if c_res:
            prof_lines.append("    소지 자원: " + ", ".join(f"{k} {v}" for k, v in c_res.items()))
        c_sta = (session.statuses or {}).get(c_name, [])
        prof_lines.append("    상태이상: " + (", ".join(c_sta) if c_sta else "없음"))
        # v7.12: 상태 영향은 코드가 고정 가중치로 더하지 않는다 — 판단층위가 아래 정의를 보고 status_mod 하나로 종합.
        _defs = core.get_merged_status_effects(session.scenario_data or {})
        for _sn in c_sta:
            _e = _defs.get(_sn)
            if not isinstance(_e, dict):
                continue
            _w = _e.get("weight")
            prof_lines.append(
                f"    · {_sn}: 적용조건=[{_e.get('apply_condition', '')}] / 제거조건=[{_e.get('remove_condition', '')}]"
                + (f" / 저작 가중치 {int(_w):+d}(옛 D20 척도 — T로는 ×2, 참고용)" if isinstance(_w, int) and _w else ""))
    lines.append("[플레이어 프로필]\n" + ("\n".join(prof_lines) if prof_lines else "(없음)"))

    # ②-2 등장 NPC — 무대에 누가 있는지 알아야 ASK 대상과 판정 대상을 정할 수 있다
    # H05 R3: DYNAMIC 세션은 정본 배치(spatial_state.placements) 기준 현재 장면 NPC만 — session.npcs 전부를
    #   현재 등장인물로 제시하지 않는다(판단층위는 세계 설정을 받지 않으므로 이름만).
    # v7.12: 대결 판정용으로 정본 능력치가 확인되는 NPC는 수치를 함께 준다(판단 입력 전용 — 비공개 값).
    def _npc_entry(display, stats, statuses):
        bits = []
        if stats:
            bits.append("능력치 " + "·".join(f"{k} {v}" for k, v in stats.items()))
        if statuses:
            bits.append("상태 " + ", ".join(map(str, statuses)))
        return display + (f"({' / '.join(bits)})" if bits else "")

    if world_turn is not None:
        _scene, _projs = core.turn_world.scene_npc_projections(world_turn.ctx, None, session)
        _ents = [_npc_entry(p["display_name"], _npc_stats(session, p["display_name"], world_turn),
                            p.get("relevant_statuses")) for p in _projs]
        lines.append("[현재 장면 NPC — 정본 위치 기준]\n" + (", ".join(_ents) if _ents else "(없음)"))
    else:
        npc_names = list((session.npcs or {}).keys())
        if npc_names:
            lines.append("[세션 등장 NPC]\n" + ", ".join(
                _npc_entry(n, _npc_stats(session, n), (session.statuses or {}).get(n)) for n in npc_names[:30]))
    lines.append("※ NPC 능력치는 판정 계산 전용 비공개 값이다 — bridge_message에 쓰지 말 것. 능력 차이는 코드가 "
                 "능력 보정(A)으로 반영하므로 difficulty·method_mod에 다시 넣지 말 것. 능력치가 없는 인물과는 "
                 "CONTEST를 만들지 말 것.")

    # ②-3 현재 세계 상태 — 추출층위가 갱신한 위치·시간대
    tl = getattr(session, "world_timeline", {}) or {}
    if tl:
        lines.append(
            "[현재 상황]\n"
            f"위치: {tl.get('current_location_display') or tl.get('current_location', '미확인')} / "
            f"시간대: {tl.get('time_of_day', '미확인')} / "
            f"날짜: {tl.get('current_date', '미확인')}"
        )

    # ③ 실시간 노트
    note = getattr(session, "note", "") or ""
    if note:
        # WP-G(D1): 판단층위는 룰북·세계 사실을 받지 않는다 — 노트를 진행 제약으로만 싣고
        #   세계 사실 판정 권위를 부여하지 않는다.
        lines.append(f"[실시간 노트 — 진행 제약으로 반영 (플레이어 주권이 우선)]\n{note}")

    # 진행 상태 카운터 — ASK/NARRATE 반복 억제 판단 근거
    lines.append(
        f"[진행 상태] ASK 누적 {session.gm_clarify_count}회 / "
        f"NARRATE 누적 {getattr(session, 'gm_narrate_count', 0)}회"
    )

    if roll_results:
        lines.append(
            "[직전 굴림 결과 — 이미 컨텍스트에 반영됨]\n" + "\n".join(roll_results)
            + "\n※ 굴림 결과가 존재하므로 원칙적으로 PROCEED로 진행한다."
        )

    # ④ 이동 안내 — 판단층위는 캐시를 읽지 않아 세계관을 모른다.
    # 코드가 경로를 계산해 재료로 넣어야 ASK로 거리를 안내할 수 있다.
    # H05: DYNAMIC 세션은 레거시 장소 그래프 안내 대신 코드 후보 매니페스트(PC 지식 기반)를 준다 —
    #   이동 가능성·경로는 판단층위가 아니라 KnownRoutePlanner가 정한다(평행 권위 없음).
    if world_turn is not None and getattr(world_turn, "manifest", None) is not None:
        lines.append(world_turn.manifest.render())
        _rel_block = core.turn_world.relation_judgment_block(world_turn.ctx, world_turn.plan, session)
        if _rel_block:
            lines.append(_rel_block)                 # v7.39(N3) 관계 단계 — 사회적 판정 보정 근거
    else:
        try:
            hint = core.places.build_move_hint(session, player_message)
            if hint:
                lines.append(hint)
        except Exception as e:
            print(f"[장소] 이동 안내 생성 실패: {e}")

    if world_turn is not None and session.current_turn_logs:
        # v7.23: 이번 턴에 진행자가 이미 물은 질문(경로·목적지·ASK)과 그 답 — 커밋 전이라 위 최근 맥락에 없다.
        #   짧은 답('1번'·'그냥 간다')은 이 질문에 대한 답으로 읽는다(새 행동이면 새 행동으로).
        lines.append("[이번 턴 대화 — 진행자 질문과 플레이어 답(커밋 전)]\n"
                     + "\n".join(session.current_turn_logs[-12:]))
    if gate:
        lines.append(_gate_judgment_block(gate))   # v7.14 — 열거값 + 원문 대상명만
    elif declaration_draft:
        # v7.23.1: 검증 전 초안 규칙은 판단 시스템 지시([선언 검증])가 소유한다 — 여기서는 지목 후보(D키)만 준다
        if discovery_rows:
            lines.append("[이 장소에서 PC가 이미 발견한 것 — 선언이 이것을 가리키면 discovery_ref에 그 키]\n"
                         + core.turn_world.discovery_rows_text(discovery_rows))
    lines.append(f"[플레이어 신규 발언]\n{player_message}")
    return "\n\n".join(lines)


# v7.23(DYNAMIC): 지시층위 세계 반응 권위 — 서사 방향 시뮬레이션이 없다. 즉시 반응은 자유, 지속 결과는 정본 근거.
WORLD_REACTION_AUTHORITY_BLOCK = (
    "\n[세계 반응 권위 — 동적 세계]\n"
    "· 이 세션에는 서사 방향 시뮬레이션이 없다. 가능성 추측을 근거로 새 위협·사건이 '일어났다'고 정하지 말 것.\n"
    "· 장면 인물·감염자·환경의 즉시 반응(몸짓·소리·시선·짧은 대응)은 위 정본 블록과 직전 장면에서 개연적으로 "
    "추론해 지시할 수 있다 — 세계는 플레이어를 기다리지 않는다.\n"
    "· 경보 발령·추적 개시·포착·세력 상태 변화처럼 다음 턴까지 이어지는 결과는 진행 중인 사건·코드 확정 세계 진행·"
    "현재 장면 인물 정본에 근거가 있을 때만 사실로 지시한다. 근거가 없으면 확정하지 말고 PC가 실제로 지각한 것만 쓴다.\n"
    "· 판정·관찰로 이미 확인된 결론(직전 장면)을 새 근거 없이 뒤집지 말 것.\n"
)



def _build_logic_user_prompt(session, player_message: str, roll_results: list,
                              sim_result: dict | None = None, world_turn=None, action: str | None = None) -> str:
    """
    지시층위 호출용 사용자 프롬프트 조립.

    Args:
        session: TRPGSession
        player_message (str): 플레이어 신규 발언 (멀티플레이어 시 종합 텍스트)
        roll_results (list[str]): 직전 ROLL 결과 문자열 목록 (재호출 시 누적)
        sim_result (dict | None): 방안 6 서사 설계자 결과. None이면 블록 생략.
    """
    target_char = session.gm_target_char or "(미지정)"
    side_note = session.gm_side_note or ""
    clarify_count = session.gm_clarify_count
    narrate_count = getattr(session, "gm_narrate_count", 0)

    # 최근 5턴 컨텍스트 (raw_logs 마지막 5개, 절단 없이 온전 제공).
    # NOTE: 이전에는 6개×280자 절단으로 각 턴 후반부(NPC 반응·상태 변화·미결 사항)가 소실되어
    # 판단 근거가 훼손되었다. 온전 주입으로 되감기·dropped thread를 구조적으로 방지한다.
    recent_logs_lines = []
    for content in session.raw_logs[-5:]:
        try:
            text = content.parts[0].text
            role = content.role.upper()
            recent_logs_lines.append(f"[{role}]\n{text}")
        except Exception:
            continue
    recent_logs_str = "\n\n".join(recent_logs_lines) if recent_logs_lines else "(최근 로그 없음)"

    # PC 프로필 요약 (스탯명만)
    pc_profile_summary = ""
    # WP-C: 같은 턴 ROLL 성장이 스테이징돼 있으면 투영(정본 미변경)을 읽는다.
    for uid, p in core.turn_preparation.projected_players(session).items():
        if p.get("name") == target_char:
            stats = ", ".join([f"{k}:{v}" for k, v in p.get("profile", {}).items() if isinstance(v, (int, str))])
            pc_profile_summary = stats
            break

    # 스탯 적용 분야 설명 (시나리오에 stat_descriptions 가 있으면 주입 — ROLL 판정 스탯 선택 보조)
    stat_descs: dict = session.scenario_data.get("stat_descriptions") or {}
    stat_desc_line = ""
    if stat_descs:
        stat_desc_line = "  (" + " / ".join([f"{k}: {v}" for k, v in stat_descs.items()]) + ")"

    # 자원·상태
    res = session.resources.get(target_char, {}) if target_char else {}
    sta = session.statuses.get(target_char, []) if target_char else []
    res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "(없음)"
    if getattr(session, "definition_revision", None):
        res_str = "(아래 [물건 — 정본 소지] 블록 참조)"      # v7.34(B3): 동적 세계는 같은 사실을 두 번 싣지 않는다
    sta_str = ", ".join(sta) or "(없음)"

    # 서사 계획 블록 (2단계: mid_plan + 순간 계획)
    narrative_plan = getattr(session, "narrative_plan", {})
    # v7.25(F12): 동적 세계는 레거시 서사 계획을 지시층위에 싣지 않는다(바인딩 전 잔존 계획 포함)
    if narrative_plan and not core.player_world.legacy_narrative_retired(session):
        current = narrative_plan.get("current_event", {})
        next_ev = narrative_plan.get("next_event", {})
        mid     = narrative_plan.get("mid_plan", {})

        if mid:
            milestones = mid.get("milestones", [])
            ms_str = " → ".join(milestones) if milestones else "(없음)"
            mid_block = (
                f"■ 중규모 진행 방향: {mid.get('title', '?')}\n"
                f"  · 전체 흐름: {mid.get('overview', '')}\n"
                f"  · 이정표 순서: {ms_str}\n"
                f"  · 완료 조건: {mid.get('end_condition', '')}\n"
            )
        else:
            mid_block = ""

        narrative_block = (
            "\n[현재 서사 계획 — proceed_instruction 및 event_assessment 결정 시 반드시 참고]\n"
            + mid_block +
            f"■ 현재 순간 사건: {current.get('title', '?')}\n"
            f"  · 상황: {current.get('summary', '')}\n"
            f"  · 마무리 방향: {current.get('resolution_direction', '')}\n"
            f"■ 다음 순간 사건 (참고용): {next_ev.get('title', '?')}\n"
            f"  · 개요: {next_ev.get('summary', '')}\n"
            f"  · 시작 조건: {next_ev.get('trigger', '')}\n"
        )
    else:
        narrative_block = ""

    roll_block = ""
    if roll_results:
        roll_block = ("\n[직전 굴림 결과 (반드시 반영하여 PROCEED를 작성)]\n" + "\n".join(roll_results)
                      + "\n※ 판정의 수치·성패는 코드 판정(2d50, 목표치 미만이면 성공)이 권위다. 룰북에 남은 옛 D20 판정·"
                        "대성공/대실패·상태 고정 감점 서술과 다르면 이 결과를 따르고, 여기에 없는 대성공·대실패를 만들지 말 것.\n")

    note_block = f"\n[GM 사이드 노트 (이번 턴 적용)]\n{side_note}\n" if side_note else ""

    # 지속 GM 노트(!노트 → session.note): 메인 묘사 프롬프트(PromptBuilder.add_note_block)와
    # 동일하게 지시층위 결정에도 주입한다. GM이 고정한 세션 한정 사실(PC 신분 등)과 제약이 담긴다.
    # WP-G(D1): 노트는 룰북에 없는 사실을 더하거나 범위를 좁힐 뿐, 룰북 고정 사실·금지사항·
    #   런타임 상태를 덮어쓰지 않는다(권위 한계는 GM_LOGIC_SYSTEM_INSTRUCTION에 명시).
    gm_note = getattr(session, "note", "") or ""
    gm_note_block = f"\n▶ 실시간 노트 (GM 직접 관리):\n{gm_note}\n" if gm_note else ""

    # 최근 5회 PROCEED 이력 블록 조립
    # NOTE: 각 PROCEED의 지시사항 + 중간 컨텍스트(NARRATE/ASK/ROLL) + AI 묘사 출력 요약 포함.
    # 지시층위가 직전 묘사 흐름을 인지하여 동일 상황 반복·정체를 방지하기 위함.
    proceed_history = getattr(session, "gm_proceed_history", [])
    if proceed_history:
        ph_lines = []
        for i, entry in enumerate(proceed_history):
            turn_num = entry.get("turn_num", "?")
            instr = entry.get("instruction", "(없음)")
            ctx = entry.get("context", [])
            ai_out = entry.get("ai_summary", "")
            tag = "← 직전 PROCEED" if i == len(proceed_history) - 1 else ""
            ph_lines.append(f"  ─ PROCEED #{i + 1} (턴 {turn_num}) {tag}")
            ph_lines.append(f"    [지시사항] {instr}")
            if ctx:
                ph_lines.append("    [중간 컨텍스트 (NARRATE/ASK/ROLL)]")
                for c in ctx:      # v7.25(CUT): 문자 수로 자르지 않는다 — 중간 컨텍스트 전부
                    ph_lines.append(f"      {c}")
            if ai_out:
                ph_lines.append("    [AI 묘사 출력]")
                ph_lines.append(f"      {ai_out}")
        proceed_history_block = (
            "\n[최근 PROCEED 이력 — 반복·정체 방지 참고]\n"
            + "\n".join(ph_lines)
            + "\n"
        )
    else:
        proceed_history_block = ""

    # NOTE: 이번 턴에 누적된 플레이어 발언·ASK 브리지·주사위 결과를 컨텍스트에 포함.
    # ASK→플레이어 응답→ASK→... 연쇄 대화를 지시층위가 인지해야 중복 질문을 방지할 수 있음.
    current_turn_block = ""
    if session.current_turn_logs:
        current_turn_block = (
            "\n[이번 턴 누적 대화 (현재 PROCEED 이전까지 발생한 발언·GM 질문·판정)]\n"
            + "\n".join(session.current_turn_logs)
            + "\n"
        )

    # 장소 이미지 목록 (상: 태그 사용 시 이 목록에서만 선택 가능)
    # 장소 이미지 후보 — places가 있으면 그쪽을 쓴다(지시 확정: 흡수).
    location_images: dict = session.scenario_data.get("location_images", {})
    try:
        from core.places import load_places
        _pl = load_places(session.scenario_data)
        if _pl:
            location_images = {
                n: (d.get("location_desc") or d.get("known_brief") or n)
                for n, d in _pl.items()
                if isinstance(d, dict) and d.get("image")
            }
    except Exception:
        pass
    # H05(ProjectedMediaContext): DYNAMIC 세션은 묘사가 표현할 투영 장면(+상위 상속)의 이미지만 후보 —
    #   도착 전 목적지 이미지는 후보가 아니다.
    _wplan = getattr(world_turn, "plan", None) if world_turn is not None else None
    # v7.25(F11): 계획이 없거나 이동 없는 턴(NARRATE 등)도 동적 세계는 정본 PC 장소(+상위)의 이미지만 후보
    _allowed = core.turn_world.media_allowed_places_for(world_turn.ctx, _wplan, session) \
        if world_turn is not None else None
    if _allowed is not None:
        location_images = {k: v for k, v in location_images.items() if k in _allowed}
    if location_images:
        loc_lines = [f"  - {kw}: {desc}" for kw, desc in location_images.items()]
        location_images_block = (
            "\n[사용 가능한 장소 이미지 목록 — 상:키워드 태그 선택 시 이 목록에서만 고를 것]\n"
            "(새로운 장면·장소로 전환될 때 PROCEED의 proceed_instruction 맨 앞에 '상:키워드'를 삽입하라.)\n"
            + "\n".join(loc_lines) + "\n"
        )
    else:
        location_images_block = ""

    # 유효 상태이상 목록 — 상태 변화 묘사의 어휘 기준. WP-G(D1/AUD-001): 레거시 상태 태그 권위 문구 제거
    #   (상태 적용 권위는 추출층위 + 코드 검증).
    merged_statuses = core.get_merged_status_effects(session.scenario_data)
    if merged_statuses and getattr(session, "cache_name", None):
        # v7.34(B2): 캐시 룰북 [4.5. 사용 가능한 상태이상 목록]과 같은 목록 — 캐시를 읽는 호출에는 다시 싣지 않는다
        valid_status_block = ("\n[유효 상태이상 목록] 캐시 룰북 [4.5. 사용 가능한 상태이상 목록]을 기준으로 할 것 "
                              "(실제 적용은 묘사를 읽은 추출층위와 시스템 검증이 한다)\n")
    elif merged_statuses:
        status_list_lines = []
        for sname, seff in merged_statuses.items():
            # v7.25(F11): weight는 v7.12부터 판정에 쓰이지 않는다 — 무효 수치를 싣지 않는다
            status_list_lines.append(f"  - {sname}: 적용조건=[{seff.get('apply_condition', '')}] / 제거조건=[{seff.get('remove_condition', '')}]")
        valid_status_block = (
            "\n[유효 상태이상 목록 — 상태 변화를 묘사하게 할 때 이 목록의 이름과 조건을 기준으로 할 것 "
            "(실제 적용은 묘사를 읽은 추출층위와 시스템 검증이 한다)]\n"
            + "\n".join(status_list_lines) + "\n"
        )
    else:
        valid_status_block = ""

    # 압축 기억 (이전 턴 맥락 — 초기 장면·지난 사건 요약 포함)
    _mem = (
        session.compressed_memory
        or getattr(session, "cached_compressed_memory", "")
        or ""
    )
    # v7.25(CUT): 압축 기억 전문 — 앞 800자(가장 오래된 부분)만 남기던 절단 제거
    memory_block = f"\n[압축 기억 — 이전 턴 요약(파생 기억 · 정본 블록과 어긋나면 정본을 따른다)]\n{_mem}\n" if _mem else ""

    # 세계 물리 타임라인 블록 (방안 B)
    world_tl = getattr(session, "world_timeline", {})
    if world_tl:
        # NOTE: 4.4.0에서 구버전 세계 타임라인 추출기를 추출층위가 흡수하면서
        #       생산되는 키가 4종(current_date·time_of_day·current_location·
        #       faction_context)으로 정리되었다. weather·known_threats·
        #       environmental_note·last_updated_turn은 더 이상 생산되지 않으므로
        #       소비도 하지 않는다(항상 기본값만 출력되어 토큰만 소모했다).
        world_tl_block = (
            "\n[현재 세계 상태 — 세력 배치·지역 규칙 기반 개연성 판단의 기준]\n"
            f"날짜: {world_tl.get('current_date', '(미확인)')}\n"
            f"위치: {world_tl.get('current_location_display') or world_tl.get('current_location', '(미확인)')}\n"
            f"시간대: {world_tl.get('time_of_day', '(미확인)')}\n"
            f"세력/지역 컨텍스트: {world_tl.get('faction_context', '(미확인)')}\n"
        )
    else:
        world_tl_block = ""
    if world_turn is not None:
        # P3(G2): DYNAMIC — 행동 시작 전(정본)과 이번 턴 진행 후(묘사층위와 같은 투영)를 구별한다.
        #   NARRATE는 실행하지 않을 투영 없이 정본 하나만(G1).
        world_tl_block = core.turn_world.instruction_world_state_block(
            world_turn, session, world_tl, hold=(action or "").upper() == "NARRATE")

    # 정보 인지 원장 블록 (지속형): 비공개·플롯 정보별 '누가 아는가'의 확립된 기록.
    # 지시층위가 info_access 필드로 갱신하며, 적·NPC가 도달 경로 없는 정보로 행동하지 않도록 하는 진실 기준.
    # H07-C: 동적 세계 세션은 레거시 info_ledger를 진실 기준으로 싣지 않는다(정보·인지가 권위).
    info_ledger = [] if core.player_world.legacy_narrative_retired(session) else (
        getattr(session, "info_ledger", []) or [])
    if info_ledger:
        led_lines = []
        for item in info_ledger[-8:]:  # 최근 8건만 주입(스코핑)
            info = item.get("info", "?")
            known = ", ".join(item.get("known_by", [])) or "(없음)"
            susp = ", ".join(item.get("suspected_by", [])) or "(없음)"
            origin = item.get("origin", "")
            leaks = item.get("leaks", [])
            led_lines.append(f"  ■ {info}")
            led_lines.append(f"     · 확지: {known}")
            led_lines.append(f"     · 추정: {susp}")
            if origin:
                led_lines.append(f"     · 출처: {origin}")
            if leaks:
                led_lines.append(f"     · 유출: " + " / ".join(leaks[-3:]))
        info_ledger_block = (
            "\n[정보 인지 원장 — 확립된 정보 접근 상태 (이 기록에 근거해 NPC·적의 앎/모름을 판정)]\n"
            "여기 '확지'로 적히지 않은 인물·세력이 그 비밀을 아는 것처럼 매복·간파·행동하게 만들지 말 것.\n"
            "새 유출은 근거(how)와 함께 info_access.new_leaks에 기록하면 원장에 누적된다.\n"
            + "\n".join(led_lines) + "\n"
        )
    else:
        info_ledger_block = ""

    # 서사 방향성 시뮬레이션 블록 (방안 6 — 개선 1~5 적용)
    if sim_result:
        contact   = sim_result.get("action_world_contact", "")
        axioms    = sim_result.get("world_axioms", [])
        dirs      = sim_result.get("directions", [])
        if dirs:
            # 공리 목록 포맷
            axiom_lines = "\n".join(f"    · {a}" for a in axioms) if axioms else "    (없음)"

            # 방향성 포맷 (개선 1 반증 결과 + 개선 4 5단계 등급 + 개선 3 변별성 레이블 포함)
            plaus_label = {
                "high":      "🟢 high",
                "probable":  "🔵 probable",
                "possible":  "🟡 possible",
                "low":       "🟠 low",
                "impossible":"⛔ impossible",
            }
            dir_lines = []
            for d in dirs:
                p     = d.get("plausibility", "?")
                label = plaus_label.get(p, p)
                fc    = d.get("falsification_check", "")
                basis = d.get("world_basis", "")
                const = d.get("narrative_constraint", "")
                elem  = d.get("primary_world_element", "")
                block = (
                    f"  [{label}] {d.get('title', '?')} (근거요소: {elem})\n"
                    f"    내용: {d.get('description', '')}\n"
                    f"    반증: {fc}\n"
                    f"    근거: {basis}\n"
                )
                if const:
                    block += f"    제약: {const}\n"
                dir_lines.append(block)

            sim_block = (
                "\n[서사 방향성 시뮬레이션 — 세계관 구조 기반 개연성 사전 분석]\n"
                f"■ 행동 접촉점: {contact}\n"
                f"■ 세계 진실 공리:\n{axiom_lines}\n"
                "■ 방향성 분석:\n"
                + "\n".join(dir_lines)
                + "※ impossible 방향은 세계관상 구조적 발생 불가.\n"
                  "지시층위는 이 분석을 proceed_instruction·event_assessment 결정에 반드시 반영할 것.\n"
            )
        else:
            sim_block = ""
    else:
        sim_block = ""

    # 멀티플레이어 정보 (여러 PC가 있을 때 모두 표시)
    target_chars = getattr(session, "gm_target_chars", [])
    multi_info = ""
    if len(target_chars) > 1:
        pc_lines = []
        for cn in target_chars:
            r = session.resources.get(cn, {})
            s = session.statuses.get(cn, [])
            r_str = ", ".join([f"{k}:{v}" for k, v in r.items()]) or "없음"
            s_str = ", ".join(s) or "없음"
            pc_lines.append(f"  - {cn}: 자원={r_str} / 상태={s_str}")
        multi_info = "\n[참가 PC 전체 상태]\n" + "\n".join(pc_lines) + "\n"

    # NOTE: 키워드북(keyword_memory) 온디맨드 주입 폐지 — 매 턴 토큰을 소모하는 데 비해
    #       기여가 낮아 제거했다. 세계관 상세는 캐시 룰북 참조로 일원화한다.
    #       비운 슬롯은 장소·퀘스트가 대체한다.
    km_block = ""


    # 시나리오 금지사항 살라이언스 강화: 캐시 [6]에도 있으나, 지시층위 결정 시점 상기를 위해
    # 사용자 프롬프트 말미(플레이어 발언 직전)에도 재주입한다. 판단의 중요도만 키우는 목적.
    prohibitions_block = ""
    _prohibits = session.scenario_data.get("prohibitions", [])
    if isinstance(_prohibits, list) and _prohibits:
        prohibitions_block = (
            "\n[시나리오 금지사항 — 이 결정과 지시문 작성 시 반드시 준수]\n"
            + "\n".join(f"- {item}" for item in _prohibits) + "\n"
        )
    elif isinstance(_prohibits, str) and _prohibits.strip():
        prohibitions_block = (
            "\n[시나리오 금지사항 — 이 결정과 지시문 작성 시 반드시 준수]\n"
            + _prohibits.strip() + "\n"
        )

    # 입력에 실제 주입된 온디맨드 정보 목록 (비용 보고용). 비어있는 블록은 제외.
    manifest = []
    # 장소 — 이동 개연성 판단의 근거. 지시층위가 갈 수 있는 곳을 알아야
    # "거기까지는 한 번에 못 간다"를 판정할 수 있다.
    place_block = ""
    try:
        # H07: DYNAMIC은 정본 장소·구간·PC 지식 기반 블록(레거시 connected 홉·연고 인물 은퇴, 표시명 규칙)
        _pb = (core.turn_world.dynamic_place_block(world_turn.ctx, world_turn.plan, session)
               if world_turn is not None else core.places.build_place_block(session))
        if _pb:
            place_block = "\n" + _pb
            manifest.append("장소 정보")
    except Exception as e:
        print(f"[지시층위] 장소 블록 실패: {e}")
    # H05 R3: DYNAMIC — 현재 장면 인물은 정본 배치 + CurrentNpcProjection(읽기 전용)
    if world_turn is not None:
        try:
            place_block += core.turn_world.scene_npc_block(world_turn.ctx, world_turn.plan, session)
            manifest.append("현재 장면 인물")
        except Exception as e:
            print(f"[지시층위] 현재 장면 인물 블록 실패: {e}")
        try:
            _gb = core.turn_world.event_guide_block(world_turn.ctx, world_turn.plan, session)   # H07 R-2
            if _gb:
                place_block += _gb
                manifest.append("사건 국면 지침")
            _eb = core.turn_world.ending_block(world_turn.ctx, world_turn.plan, session)        # v7.44 E3
            if _eb:
                place_block += _eb
                manifest.append("결말")
            _kb = core.turn_world.pc_knowledge_block(world_turn.ctx, world_turn.plan, session)  # H07-C
            if _kb:
                place_block += _kb
                manifest.append("PC 인지")
            _db = core.turn_world.discovery_block(world_turn.ctx, world_turn.plan, session)   # v7.14
            if _db:
                place_block += _db
                manifest.append("현장 발견")
            # v7.17: 정본 소지(PC·장면 인물)·현장 물건·사용 불가 상태 — 캐시 초기 소지품보다 우선
            _wc = world_turn.ctx
            _sc, _pj = core.turn_world.scene_npc_projections(_wc, world_turn.plan, session)
            _pcn = ((session.players or {}).get(_wc.uid_of.get(_wc.lead_pc)) or {}).get("name")
            if _pcn:
                place_block += core.items.item_block(
                    session, pc_name=_pcn, scene_owner_names=[p.get("legacy_name") for p in _pj if p.get("legacy_name")],
                    scene_place=_sc, defs=core.items.definitions_of(_wc.definition))
                manifest.append("정본 소지")
        except Exception as e:
            print(f"[지시층위] 사건 국면 지침 블록 실패: {e}")

    # 퀘스트 — 후보 목록을 여기서 제시해야 quest_choice로 고를 수 있다.
    # build_quest_block이 session._quest_offered를 채우고, 코드가 그것으로
    # 모델의 선택을 검증한다. 이 호출이 없으면 후보가 비어 아무것도 열리지 않는다.
    quest_block = ""
    try:
        _qb = "" if core.player_world.legacy_narrative_retired(session) else core.quest.build_quest_block(
            session,
            quest_state=core.turn_preparation.projected_quest_state(session))   # H07-C: 동적 세계는 사건 지침이 대신
        if _qb:
            quest_block = "\n" + _qb
            manifest.append("퀘스트")
    except Exception as e:
        print(f"[지시층위] 퀘스트 블록 실패: {e}")
    if world_tl_block:
        manifest.append("세계 타임라인")
    if info_ledger_block:
        manifest.append(f"정보 원장 {len(info_ledger)}건")
    if narrative_block:
        manifest.append("서사 계획")
    if sim_block:
        _ndirs = len(sim_result.get("directions", [])) if sim_result else 0
        manifest.append(f"서사 시뮬 {_ndirs}방향")
    if proceed_history_block:
        manifest.append(f"PROCEED 이력 {len(proceed_history)}건")
    if memory_block:
        manifest.append("압축 기억")
    if km_block == "" and not manifest:
        manifest.append("기본 컨텍스트만")
    session.gm_last_logic_manifest = manifest

    return f"""[현재 턴 #]: {session.turn_count + 1}
[대상 PC]: {target_char}
[PC 프로필]: {pc_profile_summary or "(미설정)"}{stat_desc_line}
[PC 자원]: {res_str}
[PC 상태]: {sta_str}{gm_note_block}
[직전 ASK 횟수 / 한도]: {clarify_count} / {MAX_CLARIFY_PER_MESSAGE}
[직전 NARRATE 횟수 / 한도]: {narrate_count} / {MAX_NARRATE_PER_MESSAGE}
{multi_info}{note_block}{world_tl_block}{info_ledger_block}{memory_block}{km_block}{place_block}{quest_block}
[최근 5턴 컨텍스트 (온전 원문)]
{recent_logs_str}
{current_turn_block}{proceed_history_block}{narrative_block}{sim_block}{location_images_block}{valid_status_block}
{roll_block}
{prohibitions_block}
[플레이어 신규 발언]
{player_message}

위 컨텍스트를 분석하여, 맨 앞 [확정된 진행 유형]에 필요한 지시문을 JSON 스키마에 맞춰 작성하십시오(진행 유형은 바꾸지 않습니다)."""


# ── WP-C 배리어 결과 ──
_OUTCOME_READY = "READY"
_OUTCOME_NO_NARRATION = "NO_NARRATION"
_OUTCOME_DELIVERY_FAILED = "DELIVERY_FAILED"
_OUTCOME_EXTRACTION_FAILED = "EXTRACTION_FAILED"
_OUTCOME_NOT_READY = "NOT_READY"
_OUTCOME_SUPERSEDED = "SUPERSEDED"      # 더 새로운 시도가 현재 — 이 시도는 흐름을 소유하지 않음


def _sys_text(config):
    """비캐시 호출의 시스템 지시 문자열(캐시 호출은 None — 지시가 캐시 안에 있다)."""
    t = getattr(config, "system_instruction", None)
    return t if isinstance(t, str) else None


class ExtractionProviderFailure(RuntimeError):
    """v7.29: 추출 provider 호출·파싱이 결과를 내지 못함(재시도 소진·stale) — 응답 처리 실패와 구별한다."""
# 추출 재시도 컨텍스트 표식 — 이 표식이 있으면 같은 tx 준비 owner로 재개한다.
_RETRY_MODE_PREPARATION = core.commit_coordinator.RETRY_MODE_PREPARATION


# ========== [GM 주사위 버튼 View] ==========
class RewindConfirmView(core.message_lifecycle.LifecyclePromptView):
    """되감기 실행 확인 — 되돌리기 불가·환불 불가를 고지한 뒤 실행한다.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결하고 접어서 지운다.
    """

    def __init__(self, bot, session, target_turn: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        self.target_turn = target_turn

    @discord.ui.button(label="되감기 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            await interaction.response.defer()
            return
        await interaction.response.defer()
        try:
            # WP-E: 되감기의 유일한 권위 — 선택된 커밋 시도 스냅샷 복원(게임 이력만, 환불 없음).
            cog = self.bot.get_cog("GMCog")
            try:                                         # v7.33(F10): 리로드 중(cog 없음)·예외도 안내로 끝낸다
                result = (await cog.history_rewind(self.session, self.target_turn) if cog is not None
                          else {"ok": False, "reason": "게임 진행 모듈을 다시 불러오는 중입니다 — 잠시 후 다시 시도해 주십시오."})
            except Exception as e:  # noqa: BLE001
                core.logs.error("history", "되감기 실행 예외", exc=e, session=self.session)
                result = {"ok": False, "reason": f"되감기 중 오류가 발생했습니다({type(e).__name__}). 상태는 바뀌지 않았습니다."}
            if not result["ok"]:
                await core.display.close_notice(
                    interaction, f"⚠️ {result['reason']}", seconds=8)
                return
            msg = core.master.lead("display.rewind_done", (
                f"⏪ **{self.target_turn}턴 종료 시점으로 되돌렸습니다.**\n"
                f"> 제거된 턴: {', '.join(str(t) for t in result['removed_turns'])}\n"
                f"> 정리된 봇 출력: {result.get('removed_messages', 0)}건 · 결제 이력은 그대로 유지됩니다."
            ))
            # 확인 메시지를 결과로 바꾸고 잠시 뒤 지운다.
            # 확인·결과가 따로 남으면 상태판이 위로 밀려난다.
            await core.display.close_notice(interaction, msg)
        finally:
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        await core.display.close_notice(
            interaction, core.master.say("display.cancelled", "되감기를 취소했습니다."), seconds=6)
        self.stop()


async def _rerender_and_report(cog, session, addendum: str) -> None:
    """v7.33(F1·F10): 재서술이 시작되지 못하면(묘사 이전 실패·예외) 이유를 게임 채널에 알린다 — 기존 턴은 그대로."""
    try:
        started, reason = await cog.rerender_latest(session, addendum=addendum)
    except Exception as e:  # noqa: BLE001
        core.logs.error("history", "재서술 예외", exc=e, session=session)
        started, reason = False, f"예기치 않은 오류({type(e).__name__})"
    if started:
        return
    game_ch = cog.bot.get_channel(session.game_ch_id)
    if game_ch:
        try:
            await core.message_lifecycle.send_transient(
                cog.bot, session, game_ch, f"⚠️ 다시 서술하지 못했습니다 — 기존 턴을 그대로 유지합니다. ({reason})",
                key=core.message_lifecycle.KEY_TURN_NOTICE)
        except Exception:  # noqa: BLE001
            pass


class RerenderConfirmView(core.message_lifecycle.LifecyclePromptView):
    """같은 턴 재생성 확인(WP-E) — 선언·판단 보존, 지시층위부터 다시 서술. 환불 없음.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결.
    """

    def __init__(self, bot, session, addendum: str = ""):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        self.addendum = addendum

    @discord.ui.button(label="재생성 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            return
        try:
            entry, _rec, reason = core.turn_history.rerender_target(self.session)
            if entry is None:
                await core.display.close_notice(interaction, f"⚠️ {reason}", seconds=8)
                return
            cog = self.bot.get_cog("GMCog")
            if cog is None:                              # v7.33(F10)
                await core.display.close_notice(
                    interaction, "⚠️ 게임 진행 모듈을 다시 불러오는 중입니다 — 잠시 후 다시 시도해 주십시오.", seconds=8)
                return
            await core.display.close_notice(
                interaction, "🔁 같은 선언으로 턴을 다시 서술합니다. 이전 턴 청구는 유지됩니다.")
            asyncio.create_task(_rerender_and_report(cog, self.session, self.addendum))
        finally:
            self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        await core.display.close_notice(
            interaction, core.master.say("display.cancelled", "재생성을 취소했습니다."), seconds=6)
        self.stop()


class RewindView(discord.ui.View):
    """
    되감기 버튼 — persistent view.

    NOTE: 디스플레이 채널에 상주해야 하므로 timeout=None + 고정 custom_id.
          실제 실행은 RewindConfirmView 확인을 거친다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="⏪ 1턴 되감기",
                       style=discord.ButtonStyle.secondary,
                       custom_id="rewind:one")
    async def rewind_one(self, interaction: discord.Interaction, _b: discord.ui.Button):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message("세션을 찾을 수 없습니다.", ephemeral=True)
            return
        if getattr(session, "is_processing", False):
            await interaction.response.send_message(
                "턴 진행 중에는 되감을 수 없습니다.", ephemeral=True)
            return

        oldest, newest = core.available_range(session)
        if newest == 0:
            await interaction.response.send_message(
                core.master.say("display.no_history", "되감기 기록이 아직 없습니다."), ephemeral=True)
            return

        target = newest - 1
        if target < oldest:
            await interaction.response.send_message(
                f"되감기 가능 범위는 {oldest}~{newest}턴입니다.", ephemeral=True)
            return

        view = RewindConfirmView(self.bot, session, target)
        await interaction.response.send_message(
            core.master.lead("display.rewind_confirm",
                             f"⚠️ **{newest}턴을 제거하고 {target}턴 종료 시점으로 되돌립니다.**\n"
                             f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
                             f"제거되는 정보는 되감기 로그로 이관됩니다."),
            view=view,
        )
        await core.message_lifecycle.bind_interaction_prompt(interaction, view)


def _interp_note(resolved: dict) -> str:
    """WP-F(POLICY-CACHE-01): 방금 확정된 시간 해석 청구를 확인 화면에 그대로 알린다(UI = 거래)."""
    ch = (resolved or {}).get("interpret_charge") or {}
    ink = int(ch.get("ink") or 0)
    if not ink:
        return ""
    if ch.get("complete"):
        return f"\n> 🧾 시간 해석 비용 **{ink}잉크**가 청구되었습니다(캐시와 별개 · 환불되지 않습니다)."
    return f"\n> 🧾 시간 해석 비용 {ink}잉크 청구가 기록되었습니다(계정 반영은 곧 재시도됩니다)."


class OpenConfirmView(core.message_lifecycle.LifecyclePromptView):
    """세션 오픈 확인 — 유지 시간과 예상 비용을 보고 진행 여부를 정한다.

    WP-F: INTERACTION_PROMPT — 확인/취소/시간 만료 중 정확히 한 번 종결. 만료는 취소와 같다.
    """

    def __init__(self, bot, session, minutes: int, *, on_reopen=None, flow: bool = False):
        super().__init__(timeout=None if flow else 300)   # v7.37: 첫 생성 대화 안의 확인은 진행 화면(시간 제한 없음)
        self.bot = bot
        self.session = session
        self.minutes = minutes
        self.on_reopen = on_reopen          # 다시 열기 성공 후 콜백(닫힌 동안 보관한 선언 이어서 진행)
        self.flow = flow                    # 첫 생성 대화 메시지 안 — 결과를 같은 메시지에 쓰고 지우지 않는다

    async def _notice(self, interaction, text, seconds=12):
        if self.flow:
            ch = self.bot.get_channel(self.session.game_ch_id)
            if ch is not None:
                await core.session_flow.open_feedback(self.bot, self.session, ch, text)
                return
        await core.display.close_notice(interaction, text, seconds=seconds)

    async def on_expired(self):
        self.session.open_minutes = 0

    @discord.ui.button(label="세션 열기", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, _b):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CONFIRM):
            return

        # 잔액 확인 — 선불식이므로 부족하면 진행하지 않는다(기획 규정).
        #   WP-F: 해석 청구가 이미 반영된 현재 잔액으로 '캐시 선불'만 검사한다. 이 필요액은
        #   open_window 가 같은 견적 토큰(quoted_tokens)으로 실제 선불할 금액과 같다.
        uid = str(interaction.user.id)
        est = core.estimate_session_open(self.session, self.minutes / 60)
        need = est.get("total_ink", 0)
        quoted = int(est.get("cache_tokens") or 0) or None
        bal = core.accounts.get_balance(uid)
        if bal < need:
            await self._notice(
                interaction,
                core.master.lead("cache.insufficient_ink",
                                 f"⚠️ 잔액이 부족합니다. 필요 **{need}잉크** / 보유 **{bal}잉크**\n"
                                 f"잉크 지급은 운영자에게 문의해 주십시오."
                                 + ("\n더 짧은 시간을 이 채널에 다시 말씀하셔도 됩니다." if self.flow else "")),
                seconds=15)
            if self.flow:
                self.session.awaiting_display_input = True
                self.session._open_input_ch_id = self.session.game_ch_id
            self.stop()
            return

        # WP-F(POLICY-CACHE-01): 시간 해석 비용은 해석 직후 interpretation_billing 이 이미
        #   청구했다(취소·시간 만료와 무관). 여기서의 필요 잉크는 앞으로 낼 캐시 선불뿐이다.
        note = ""

        for child in self.children:
            child.disabled = True
        if not self.flow:
            await interaction.message.edit(view=self)
        await self._notice(
            interaction,
            core.master.lead("cache.open_confirm", f"✅ 세션을 {self.minutes}분간 유지합니다. 예상 {need}잉크{note}"))
        self.session.open_minutes = self.minutes

        game_ch = self.bot.get_channel(self.session.game_ch_id)
        step = core.creation.current_step(self.session)

        if step == "open" and game_ch:
            # 세션 제작 중 — 캐시를 올리고 시작 상황으로 넘어간다.
            try:
                await core.session_flow.on_open_time_done(
                    self.bot, self.session, game_ch, quoted_tokens=quoted)
            except Exception as e:
                print(f"[세션플로우] 오픈 처리 실패: {e}")
        else:
            # 닫았던 세션을 다시 여는 경우. 캐시만 새로 올린다.
            # 이 분기가 없으면 시간만 정해지고 세션이 열리지 않는다.
            cog = self.bot.get_cog("SessionCog")
            if cog:
                # WP-F: 업로드 진행/결과 안내는 TRANSIENT_GAME_STATUS(교체·자동 정리, 로그 미기록).
                notify = core.message_lifecycle.transient_notifier(
                    self.bot, self.session, game_ch, ttl=60) if game_ch else None
                ok = await cog.upload_cache(self.session, notify=notify, quoted_tokens=quoted)
                if ok:
                    self.session.is_started = True
                    await core.save_session_data(self.bot, self.session)
                    from core.display import refresh as _refresh
                    await _refresh(self.bot, self.session, reason="reopen")
                    if self.on_reopen is not None:
                        await self.on_reopen(self.session)
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _b):
        await interaction.response.defer()
        if not self.claim(core.message_lifecycle.PROMPT_CANCEL):
            return
        self.session.open_minutes = 0
        if self.flow:                                   # v7.37: 첫 생성 대화 — 시간을 다시 묻는다
            ch = self.bot.get_channel(self.session.game_ch_id)
            if ch is not None:
                await core.session_flow.ask_open_time(
                    self.bot, self.session, ch, "알겠습니다. 그럼 얼마나 머무르시겠습니까? 이 채널에 다시 말씀해 주십시오.")
            self.stop()
            return
        await core.display.close_notice(
            interaction, "세션 오픈을 취소했습니다.", seconds=6)
        self.stop()


class InfinityPlanView(discord.ui.View):
    """
    인피니티 세션 플랜 선택.

    기획 규정 — 메인 클리어 후 이어갈지 닫을지 묻고, 이어간다면
    플랜을 고르게 한다. 각 플랜의 비용 안내가 필수다.
    """

    def __init__(self, bot, session):
        super().__init__(timeout=1800)
        self.bot = bot
        self.session = session
        for key, plan in core.quest.INFINITY_PLANS.items():
            self.add_item(InfinityPlanButton(key, plan["label"]))

    @discord.ui.button(label="🚪 세션 닫기", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction, _b):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "세션을 여기서 마칩니다. 수고하셨습니다.")
        self.session.gm_active = False
        await core.save_session_data(self.bot, self.session)
        self.stop()


class InfinityPlanButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, row=0
                         if key in ("sub_only", "with_main") else 1)
        self.plan_key = key

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        plan = core.quest.apply_infinity_plan(view.session, self.plan_key)
        if not plan:
            await interaction.response.send_message("플랜 적용에 실패했습니다.", ephemeral=True)
            return
        view.session.pending_ending = ""
        for c in view.children:
            c.disabled = True
        await interaction.response.edit_message(view=view)
        await interaction.followup.send(
            f"▶️ **인피니티 세션 — {plan['label']}**\n"
            f"> {plan['desc']}\n"
            f"> 비용: {plan['cost']}\n\n"
            f"이야기가 이어집니다."
        )
        await core.save_session_data(view.bot, view.session)
        try:
            await core.refresh_display(view.bot, view.session, reason="infinity")
        except Exception:
            pass
        view.stop()


class ExtractionRetryView(discord.ui.View):
    """
    추출층위 재시도 버튼 — persistent view.

    NOTE: timeout=None + 고정 custom_id + bot.add_view() 등록 조합으로
          봇 재시작 이후에도 버튼이 살아남는다. 추출 실패는 다음 턴을
          차단하므로, 재시작으로 버튼이 죽으면 세션이 영구 정지한다.
    """

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="🔄 턴 정보 정리 재시도",
                       style=discord.ButtonStyle.primary,
                       custom_id="extraction:retry")
    async def retry(self, interaction: discord.Interaction, _button: discord.ui.Button):
        session = self.bot.active_sessions.get(interaction.channel.id)
        if not session:
            await interaction.response.send_message("세션을 찾을 수 없습니다.", ephemeral=True)
            return
        if not getattr(session, "extraction_pending", False):
            await interaction.response.send_message("이미 정리가 완료되었습니다.", ephemeral=True)
            return

        await interaction.response.defer()
        _ctx = getattr(session, "extraction_retry_ctx", {}) or {}
        cog = self.bot.get_cog("GMCog")
        if _ctx.get("mode") == _RETRY_MODE_PREPARATION:
            # WP-C: 같은 논리 시도·같은 준비 owner로 재개한다(새 자동 턴을 열지 않는다).
            outcome = await cog._retry_prepared_extraction(session)
            if outcome == "ready":
                await core.display.close_notice(
                    interaction, "✅ 턴 정보 정리가 완료되었습니다. 계속 진행하십시오.")
                try:
                    await interaction.message.delete()
                except Exception:
                    pass
            elif outcome == "released":
                await interaction.followup.send(
                    "진행 중이던 턴 준비 상태가 없어(재시작 등) 차단만 해제했습니다.",
                    ephemeral=True)
            return
        # WP-G(RE-GATE): 준비 모드가 아닌 재시도 컨텍스트(WP-C 이전 저장 형식)는 현재 권위로
        #   안전하게 적용할 수 없다 — provider 재호출·추출 계획 적용·정본 변이 없이 은퇴한다.
        #   은퇴는 strict 저장이 성공한 뒤에만 차단을 해제한다(실패 시 차단·컨텍스트 유지).
        if await cog._retire_legacy_extraction_retry(session):
            await core.display.close_notice(
                interaction,
                "ℹ️ 이전 버전에서 남은 턴 정보 정리 요청이라 현재 방식으로 안전하게 적용할 수 없어 "
                "폐기했습니다. 이미 진행된 게임 상태는 소급 변경하지 않았습니다. "
                "다음 행동을 선언해 계속 진행하십시오.")
        else:
            await interaction.followup.send(
                "⚠️ 이전 버전의 정리 요청을 폐기하는 저장에 실패했습니다. 차단은 유지되며 "
                "게임 상태는 바뀌지 않았습니다. 잠시 후 다시 눌러 주십시오.", ephemeral=True)


def _set_roll_open(session, transaction_id, value: bool) -> None:
    """v7.33(F4): 판정 화면 열림 표식 — 같은 시도일 때만(낡은 화면이 새 시도를 건드리지 않음)."""
    tx = core.turn_transaction.get_active_transaction(session)
    if tx is not None and transaction_id is not None and tx.transaction_id == transaction_id:
        tx.roll_open = value


class RollContinueView(discord.ui.View):
    """v7.33(F5): 굴림 뒤 지시층위가 실패했을 때 — 보관한 판정 결과로 이어서 진행(재굴림 없음)."""

    def __init__(self, cog, session, transaction_id: str):
        super().__init__(timeout=None)
        self.cog, self.session, self.transaction_id = cog, session, transaction_id
        self._busy = False

    @discord.ui.button(label="🎲 판정 결과로 이어서 진행", style=discord.ButtonStyle.primary,
                       custom_id=core.ui_expiry.ROLL_PREFIX + "continue")
    async def resume(self, interaction: discord.Interaction, _b: discord.ui.Button):
        if str(interaction.user.id) not in (self.session.players or {}):
            return await interaction.response.send_message("> 이 세션의 플레이어만 누를 수 있습니다.", ephemeral=True)
        tx = core.turn_transaction.get_active_transaction(self.session)
        pend = getattr(tx, "pending_roll", None) if tx is not None and tx.transaction_id == self.transaction_id else None
        if self._busy or not pend:
            return await interaction.response.send_message("> 이미 처리됐거나 지난 판정입니다.", ephemeral=True)
        if not core.is_session_open(self.session):
            return await interaction.response.send_message(
                "> 세션이 닫혀 있습니다. 디스플레이 채널에서 세션을 연 뒤 다시 눌러 주십시오.", ephemeral=True)
        self._busy = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        asyncio.create_task(self.cog._continue_with_roll_results(
            self.session, pend["player_message"], list(pend["roll_results"]), transaction_id=self.transaction_id))


class GMRollView(discord.ui.View):
    """
    GM에서 ROLL 판정 시 플레이어에게 주사위 버튼을 제공하는 View.
    """

    def __init__(self, cog, session, roll_specs: list, player_message: str,
                 prior_roll_results: list, target_uid: str | None,
                 transaction_id: str | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.session = session
        self.roll_specs = roll_specs
        self.player_message = player_message
        self.prior_roll_results = list(prior_roll_results)
        self.target_uid = target_uid
        # WP-01: 원 오케스트레이션 스택은 버튼/타임아웃 재개 전에 이미 반환되었으므로,
        # 논리 턴 시도의 불변 식별자를 View가 직접 들고 async UI 경계를 넘긴다.
        self.transaction_id = transaction_id
        self._resolved = False
        self.message = None   # 판정 요청 메시지 — 자동 굴림 시 버튼을 비활성화한다(v7.13)

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary,
                       custom_id=core.ui_expiry.ROLL_PREFIX + "dice")   # 재시작 뒤 누르면 재선언 안내(v7.33)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.target_uid and str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message(
                "> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True
            )

        if self._resolved:
            return await interaction.response.send_message(
                "> 이미 처리된 판정입니다.", ephemeral=True
            )
        self._resolved = True
        _set_roll_open(self.session, self.transaction_id, False)

        # 효과음 즉시 발사(논블로킹). 결과는 _process_roll에서 1.5초 하한 후 출력.
        asyncio.create_task(core.play_dice_sfx(self.cog.bot, interaction.guild))

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="> 🎲 주사위를 굴리는 중…", view=self
        )
        self.stop()

        asyncio.create_task(self._process_roll(interaction.channel))

    async def _process_roll(self, game_ch):
        # 효과음이 결과보다 선행하도록 1.5초 하한 부여 (인게임 주사위와 리듬 통일)
        await asyncio.sleep(1.5)
        new_results = await self.cog._execute_rolls(
            self.session, self.roll_specs, game_ch, transaction_id=self.transaction_id)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(
                self.session, self.player_message, combined,
                transaction_id=self.transaction_id)
        )

    async def on_timeout(self):
        if self._resolved:
            return
        self._resolved = True
        _set_roll_open(self.session, self.transaction_id, False)
        # v7.13: 자동 굴림 뒤 요청 메시지에 누를 수 있는 버튼이 남지 않게 한다.
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception as e:
                print(f"[판정] 자동 굴림 버튼 비활성화 실패: {e}")

        master_ch = self.cog.bot.get_channel(self.session.master_ch_id)
        if master_ch:
            await master_ch.send(
                "⚠️ **[GM]** 판정 버튼 시간 초과(5분). 주사위를 자동으로 굴립니다."
            )
        game_ch = self.cog.bot.get_channel(self.session.game_ch_id)
        new_results = await self.cog._execute_rolls(
            self.session, self.roll_specs, game_ch, transaction_id=self.transaction_id)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(
                self.session, self.player_message, combined,
                transaction_id=self.transaction_id)
        )


# ========== [GM Cog] ==========
class GMCog(commands.Cog):
    """
    게임 채널의 플레이어 발언을 받아 AI가 GM 역할을 자동 수행하는 옵트인 모드.

    PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 물어보는 라운드 수집 시스템을 포함.
    멀티플레이어 지원: 등록된 모든 PC에게 순서대로 행동을 물어본 뒤 종합하여 지시층위 호출.
    """

    def __init__(self, bot):
        self.bot = bot
        self._session_locks = {}

    def _lock_for(self, session):
        if session.session_id not in self._session_locks:
            self._session_locks[session.session_id] = asyncio.Lock()
        return self._session_locks[session.session_id]

    # ─────────────────────────────────────────────────────────────
    # 명령어
    # ─────────────────────────────────────────────────────────────

    @commands.group(name="자동", invoke_without_command=True)
    async def auto(self, ctx, *args):
        """
        GM 명령어 그룹. 모든 하위 기능을 인자로 분기한다.
        인자가 없거나 알 수 없는 하위명령이면 사용법을 출력한다.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        unknown = f"⚠️ 알 수 없는 하위명령: `{args[0]}`\n" if args else ""
        await ctx.send(
            f"{unknown}🤖 **[GM 명령어]**\n"
            "`!자동 시작 (대상PC…)` : GM 활성화 (단일 PC면 자동 선택)\n"
            "`!자동 중단` : GM 정지 후 인간 GM 명령 모드 복귀\n"
            "`!자동 상태` : 활성 여부·자동 처리 턴·누적 비용 확인\n"
            "`!자동 개입 [텍스트]` : 다음 PROCEED 완료 시까지 GM 사이드 노트 유지\n"
            "`!자동 턴제한 [N|해제]` : 자동 진행 최대 턴 수 (해제=무제한)\n"
            "`!자동 비용제한 [원|해제]` : 자동 누적 비용 상한 (해제=무제한)\n"
            "`!자동 서사` : 현재 서사 계획 임베드 출력\n"
            "`!자동 재계획 [메모]` : 서사 계획 강제 재수립\n"
            "`!자동 원장` : 정보 인지 원장(비공개 정보별 인지 주체) 출력"
        )

    @auto.command(name="시작")
    async def auto_start(self, ctx, *target_char_args: str):
        """
        GM 활성화. 인자 없으면 등록된 모든 PC를 대상으로 함.
        멀티플레이어 시 !자동 시작, 특정 PC만 지정 시 !자동 시작 이름1 이름2 형태로 사용.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "is_started", False):
            return await ctx.send("⚠️ 세션이 시작되지 않았습니다. `!시작`을 먼저 실행하세요.")

        # 대상 PC 결정
        if target_char_args:
            target_chars = list(target_char_args)
        elif session.players:
            target_chars = [p.get("name") for p in session.players.values() if p.get("name")]
        else:
            return await ctx.send(
                "⚠️ 등록된 PC가 없습니다. `!참가`로 PC를 먼저 등록하세요."
            )

        # 유효성 검증
        invalid = [n for n in target_chars if not core.get_uid_by_char_name(session, n)]
        if invalid:
            return await ctx.send(f"⚠️ 다음 PC를 찾을 수 없습니다: {', '.join(invalid)}")

        session.gm_active = True
        session.gm_target_chars = target_chars
        session.gm_target_char = target_chars[0]   # 하위 호환성 (지시층위 단일 PC 참조용)
        session.gm_turns_done = 0
        session.gm_clarify_count = 0
        core.mark_auto_mode_start(self.bot, session)   # WP-G: CostLedger 기준(AUD-033)
        session.gm_side_note = ""
        session.gm_pending_players = []
        session.gm_collected_actions = {}
        session.gm_waiting_for = None
        # NOTE: 최근 5회 PROCEED 이력 (지시사항 + 중간 컨텍스트 + AI 출력 요약).
        # 지시층위 프롬프트에 주입되어 서사 반복·정체를 방지한다. 봇 재시작 시 초기화 허용.
        session.gm_proceed_history = []
        await core.save_session_data(self.bot, session)

        has_existing_plan = bool(session.narrative_plan)
        plan_note = (
            "- 서사: 동적 세계 사건·정본이 주도 (레거시 서사 계획 없음)"
            if core.player_world.legacy_narrative_retired(session) else
            f"- 서사 계획: 기존 계획 유지 (v{session.narrative_plan.get('plan_version', '?')})"
            if has_existing_plan else
            "- 서사 계획: 수립 중... (백그라운드에서 진행)"
        )
        await ctx.send(
            f"🤖 **[GM 활성화]**\n"
            f"- 대상 PC: **{', '.join(target_chars)}**\n"
            f"- 자동 턴 한도: {_cap_display(session.gm_turn_cap)}\n"
            f"- 자동 누적 비용 한도: {_cap_display(session.gm_cost_cap_krw, is_cost=True)}\n"
            f"{plan_note}\n"
            f"- PROCEED 완료 후 GM이 선제적으로 행동을 물어봅니다.\n"
            f"- 중단: `!자동 중단`  /  GM에게 메모: `!자동 개입 [텍스트]`\n"
            f"- 서사 확인: `!자동 서사`  /  강제 재계획: `!자동 재계획`"
        )

        # 활성화 직후: 서사 계획 수립 후 첫 라운드 시작 (백그라운드 태스크)
        asyncio.create_task(self._init_narrative_and_start(session))

    @auto.command(name="중단")
    async def auto_stop(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not getattr(session, "gm_active", False):
            return await ctx.send("⚠️ GM가 활성 상태가 아닙니다.")

        session.gm_active = False
        session.gm_waiting_for = None
        session.gm_pending_players = []
        await core.save_session_data(self.bot, session)

        try:
            used = core.auto_mode_used_krw(self.bot, session)
        except Exception as e:  # noqa: BLE001
            print(f"[자동 비용] 원장 읽기 실패: {e}")
            used = 0.0
        await ctx.send(
            f"🛑 **[GM 정지]**\n"
            f"- 자동 처리 턴: {session.gm_turns_done}턴\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)}\n"
            f"- 인간 GM 명령어 입력 모드로 복귀합니다."
        )

    @auto.command(name="상태")
    async def auto_status(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        active = getattr(session, "gm_active", False)
        try:
            used = core.auto_mode_used_krw(self.bot, session)   # WP-G: CostLedger provider 비용
        except Exception as e:  # noqa: BLE001
            print(f"[자동 비용] 원장 읽기 실패: {e}")
            used = 0.0
        target_chars = getattr(session, "gm_target_chars", [])
        waiting = getattr(session, "gm_waiting_for", None)
        pending = getattr(session, "gm_pending_players", [])
        collected = getattr(session, "gm_collected_actions", {})

        collected_str = "\n".join([f"    · {k}: {v[:40]}" for k, v in collected.items()]) or "    (없음)"
        await ctx.send(
            f"🤖 **[GM 상태]**\n"
            f"- 활성: {'✅ 켜짐' if active else '⛔ 꺼짐'}\n"
            f"- 대상 PC: {', '.join(target_chars) if target_chars else '(없음)'}\n"
            f"- 자동 처리 턴: {session.gm_turns_done} / {_cap_display(session.gm_turn_cap)}\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)} / {_cap_display(session.gm_cost_cap_krw, is_cost=True)}\n"
            f"- 현재 발언 대기 PC: {waiting or '(없음)'}\n"
            f"- 응답 대기 중인 PC: {', '.join(pending) if pending else '(없음)'}\n"
            f"- 수집된 행동:\n{collected_str}\n"
            f"- 직전 ASK 횟수: {session.gm_clarify_count}\n"
            f"- 대기 중 사이드 노트: {session.gm_side_note or '(없음)'}"
        )

    @auto.command(name="개입")
    async def auto_inject(self, ctx, *, text: str = ""):
        """다음 PROCEED 완료 시까지 GM 사이드 노트를 유지."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not text.strip():
            return await ctx.send("⚠️ 사용법: `!자동 개입 [GM에게 전달할 메모]`")

        session.gm_side_note = text.strip()
        await core.save_session_data(self.bot, session)
        await ctx.send(
            f"📝 사이드 노트 등록 (다음 PROCEED(턴 진행) 완료 시까지 유지):\n> {text.strip()}"
        )

    @auto.command(name="턴제한")
    async def auto_set_cap(self, ctx, n: str = None):
        """자동 진행 최대 턴 수 설정. '해제'/'0' 또는 인자 없음 → 무제한."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if n is None or n in ("해제", "무제한", "0"):
            session.gm_turn_cap = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 턴 한도를 **무제한**으로 설정했습니다.")

        try:
            val = int(n)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 턴제한 [1~1000 | 해제]`")
        if val < 1 or val > 1000:
            return await ctx.send("⚠️ 턴 한도는 1~1000 사이여야 합니다. (무제한: `해제`)")

        session.gm_turn_cap = val
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 턴 한도를 {val}턴으로 변경했습니다.")

    @auto.command(name="비용제한")
    async def auto_set_cost_cap(self, ctx, amount: str = None):
        """자동 모드 누적 비용 상한(원) 설정. '해제'/'0' 또는 인자 없음 → 무제한."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if amount is None or amount in ("해제", "무제한", "0"):
            session.gm_cost_cap_krw = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 누적 비용 한도를 **무제한**으로 설정했습니다.")

        try:
            val = float(amount)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 비용제한 [금액(원) | 해제]`")
        if val <= 0:
            return await ctx.send("⚠️ 비용 한도는 0보다 커야 합니다. (무제한: `해제`)")

        session.gm_cost_cap_krw = val
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 누적 비용 한도를 {core.format_cost(val)}으로 변경했습니다.")

    # ─────────────────────────────────────────────────────────────
    # 메시지 리스너
    # ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.content.startswith("!"):
            return

        # v7.15: 캐릭터 생성 중인 플레이어의 채팅은 생성 답변 경로가 소유한다(지금 텍스트를 기다리면 1건 소비,
        #   아니면 무시). GM 활성 여부·턴 처리보다 먼저 판정한다 — DM 사전 생성도 같은 경로.
        if core.profile_creation_ui.claims(self.bot, message):
            asyncio.create_task(core.profile_creation_ui.on_chat(self.bot, message))
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return

        # 디스플레이 채널의 시간 입력 — 입력 대기 중인 참가자(또는 오너)의 답변 1건만 소비한다.
        #   chat_guard와 별개로 모든 메시지가 여기에 오므로, 대기가 아니면 해석(청구)하지 않는다.
        if message.channel.id == getattr(session, "display_ch_id", None):
            if not getattr(session, "awaiting_display_input", False):
                return
            if getattr(session, "_open_input_ch_id", None) not in (None, message.channel.id):
                return                                          # v7.21.4: 첫 생성 답변은 게임 채널에서 받는다
            if (str(message.author.id) not in (session.players or {})
                    and not await self.bot.is_owner(message.author)):
                return
            if not getattr(session, "awaiting_display_input", False):   # 대기 중 다른 답변이 먼저 소비
                return
            session.awaiting_display_input = False
            session._display_input_msg_id = message.id
            await self._handle_open_time_input(session, message)
            return

        if message.channel.id != session.game_ch_id:
            return
        # v7.21.4: 세션 첫 생성의 유지 시간 답변(게임 채널) — 디스플레이 답변과 같은 1회 소비·해석 경로
        if core.session_flow.awaiting_open_here(session, message.channel.id):
            if (str(message.author.id) not in (session.players or {})
                    and not await self.bot.is_owner(message.author)):
                return
            session.awaiting_display_input = False
            session._display_input_msg_id = message.id
            await self._handle_open_time_input(session, message)
            return
        # WP-D(D-D4): 커밋 복구 admission은 새 자동 턴 eligibility(gm_active·턴/비용 한도)보다
        #   먼저 평가한다 — 마지막 턴이 영속 후 청구/확정 단계에서 멈췄어도 재시작 없이 복구된다.
        if getattr(session, "commit_recovery", None):
            asyncio.create_task(self._recovery_admission(session))
            return
        if not getattr(session, "gm_active", False):
            return

        user_id_str = str(message.author.id)
        char_name = session.players.get(user_id_str, {}).get("name")
        waiting_for = getattr(session, "gm_waiting_for", None)

        if waiting_for:
            # GM이 특정 PC의 발언을 기다리는 중
            if char_name != waiting_for:
                # 다른 플레이어 발언 무시
                return
            asyncio.create_task(self._handle_waiting_response(session, message, char_name))
        else:
            # 대기 상태 없음 → 기존 자발 발언 즉각 처리 (하위 호환)
            asyncio.create_task(self._handle_player_message(session, message))

    # ─────────────────────────────────────────────────────────────
    # 라운드 수집 시스템 (선제 행동 질문 — #22)
    # ─────────────────────────────────────────────────────────────

    async def _start_round(self, session):
        """
        PROCEED 완료 후(또는 자동시작 직후) 호출. 모든 대상 PC에게 순서대로 행동을 묻는 라운드 시작.
        gm_target_chars가 비어 있으면 아무 동작도 하지 않음.
        """
        if not getattr(session, "gm_active", False):
            return
        target_chars = getattr(session, "gm_target_chars", [])
        if not target_chars:
            return

        session.gm_pending_players = list(target_chars)
        session.gm_collected_actions = {}
        session.gm_waiting_for = None
        session.gm_narrate_count = 0
        # 저장은 바로 이어지는 _ask_next_player가 처리 (중복 제거).
        # 여기서 초기화한 세 필드는 SESSION_RESET_FIELDS라 크래시 시에도 재시작 후 어차피 초기화됨.
        await self._ask_next_player(session)

    async def _ask_next_player(self, session):
        """
        gm_pending_players에서 다음 PC를 꺼내 행동 요청 메시지를 게임 채널에 전송.
        목록이 비어 있으면 수집 완료로 처리.
        """
        game_ch = self.bot.get_channel(session.game_ch_id)

        if not session.gm_pending_players:
            # 모든 PC의 행동이 수집됨 → 지시층위 호출
            await self._finalize_round_and_process(session)
            return

        next_char = session.gm_pending_players.pop(0)
        session.gm_waiting_for = next_char

        uid = core.get_uid_by_char_name(session, next_char)
        mention = f"<@{uid}>" if uid else f"**{next_char}**"

        collected = session.gm_collected_actions
        if collected:
            # 이미 다른 PC의 행동이 수집된 상태 → 간단히 참고 표시
            others = "、".join([
                f"{k}: '{v}'"
                for k, v in collected.items()
            ])
            prompt = f"{mention}, 현재까지 행동 선언: {others}\n{mention}은(는) 어떻게 하시겠습니까?"
        else:
            # v7.13 마스터 대사 — 차례 안내는 캐릭터 이름으로 부른다(사용자 결정 Q3). 대사가 없으면 기존(멘션) 문구.
            prompt = core.master.say("turn.next_player", f"{mention}님 턴입니다. 어떤 행동을 하시겠습니까?",
                                     slots={"player_name": next_char})

        if game_ch:
            await core.stream_text_to_channel(
                self.bot, game_ch, f"> {prompt}",
                words_per_tick=24, tick_interval=0.8
            )
        # 선제 질문도 current_turn_logs에 기록
        session.current_turn_logs.append(f"[진행자 (GM)]: {prompt}")
        await core.save_session_data(self.bot, session)

    async def _handle_waiting_response(self, session, message: discord.Message, char_name: str):
        """
        GM의 선제 행동 질문에 대한 플레이어 응답 수집. 모든 PC 수집 완료 시 지시층위 호출.
        """
        async with self._lock_for(session):
            # 이미 다른 처리가 완료된 경우 스킵
            if session.gm_waiting_for != char_name:
                return
            if not session.gm_active:
                return
            if await self._roll_pending_blocks(session):
                return
            content = message.content.strip()
            if await self._hold_if_closed(session, char_name, content):
                return                                     # 질문 대기 상태 유지 — 다시 열면 보관 선언으로 진행

            session.gm_waiting_for = None

            # 행동 수집
            session.gm_collected_actions[char_name] = content
            session.current_turn_logs.append(f"[{char_name}]: {content}")

            if session.gm_pending_players:
                # 아직 응답 안 한 PC가 있음 → 다음 PC에게 질문
                await core.save_session_data(self.bot, session)
                await self._ask_next_player(session)
            else:
                # 모든 PC 수집 완료
                await self._finalize_round_and_process(session)

    async def _finalize_round_and_process(self, session):
        """
        모든 PC의 행동이 수집된 후 종합하여 지시층위를 호출.
        단일 PC면 그대로, 멀티 PC면 종합 메시지 생성 + 게임 채널에 요약 표시.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        actions = session.gm_collected_actions.copy()
        session.gm_collected_actions = {}
        session.gm_clarify_count = 0

        if not actions:
            return

        if len(actions) == 1:
            player_message = list(actions.values())[0]
        else:
            # 멀티플레이어 — 게임 채널에 행동 종합 표시
            summary_lines = "\n".join([f"> **{k}**: {v}" for k, v in actions.items()])
            if game_ch:
                await core.send_streamed(self.bot, game_ch, f"> 📋 **행동 선언 종합:**\n{summary_lines}")
                core.write_log(session.session_id, "game_chat",
                               f"[행동 종합]: {'; '.join([f'{k}: {v}' for k, v in actions.items()])}")
            player_message = "\n".join([f"[{k}]: {v}" for k, v in actions.items()])
            # 대표 PC를 첫 번째 PC로 업데이트
            first_char = list(actions.keys())[0]
            session.gm_target_char = first_char

        await self._process_actions(session, player_message, master_ch)

    # ─────────────────────────────────────────────────────────────
    # 안전장치 + 지시층위 루프 진입점
    # ─────────────────────────────────────────────────────────────

    async def _roll_pending_blocks(self, session) -> bool:
        """v7.33(F4 — 사용자 결정 '가'): 판정이 끝나지 않은 동안 새 선언을 받지 않는다(같은 시도로 이중 진행·재굴림 방지).

        판정 대기 = 주사위를 아직 굴리지 않음 또는 굴린 결과를 보관 중(지시층위 실패 뒤 [이어서 진행] 대기)."""
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is None or tx.status != core.turn_transaction.TurnStatus.WAITING_FOR_ROLL \
                or not (getattr(tx, "roll_open", False) or getattr(tx, "pending_roll", None)):
            return False
        text = ("🎲 보관된 판정 결과가 있습니다. 위의 **[판정 결과로 이어서 진행]**을 눌러 주십시오."
                if getattr(tx, "pending_roll", None) else
                "🎲 진행 중인 판정이 있습니다. 먼저 위의 **[🎲 주사위 굴리기]**를 눌러 주십시오.")
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.message_lifecycle.send_transient(self.bot, session, game_ch, text,
                                                        key=core.message_lifecycle.KEY_TURN_NOTICE)
        return True

    async def _abort_turn_before_narration(self, session, transaction_id, player_message, master_ch, *,
                                           code, stage: str, exc: BaseException | None = None,
                                           master_text: str | None = None) -> bool:
        """v7.33(F1·F3): 묘사 이전 단계 실패 — 시도를 FAILED_SYSTEM(청구 0)으로 종결하고 플레이어에게 재선언을 안내한다.

        이미 종결·대체된 시도이거나 준비가 묘사 이후 단계(커밋 경로 소유)면 아무것도 하지 않는다(False)."""
        TT, TP = core.turn_transaction, core.turn_preparation
        if exc is not None:
            core.logs.error("turn", f"예기치 않은 예외 — {stage}", exc=exc, session=session)
        tx = TT.get_active_transaction(session)
        if transaction_id is None or tx is None or tx.transaction_id != transaction_id or TT.is_terminal(tx.status):
            return False
        prep = TP.get_preparation(session, transaction_id)
        if prep is not None and prep.phase != TP.PREP_COLLECTING:
            return False
        prep = TP.ensure_preparation(session, transaction_id)
        try:
            await prep.join()
            prep.seal()
            prep.close_cost_membership()
        except Exception as e:  # noqa: BLE001
            prep.violations.append(f"cost_close:{e}")
        prep.phase = TP.PREP_FAILED
        prep.failure_stage = stage
        prep.failure_code = getattr(code, "value", code)
        TT.finalize(session, transaction_id, TT.TurnStatus.FAILED_SYSTEM, failure_stage=stage, failure_code=code,
                    failure_message=(f"{type(exc).__name__}: {exc}"[:300] if exc is not None else None))
        await self._release_turn_processing(session, prep)
        session.current_turn_logs = []
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            try:
                await core.message_lifecycle.send_transient(
                    self.bot, session, game_ch, core.build_failed_turn_notice(player_message),
                    key=core.message_lifecycle.KEY_TURN_NOTICE)
            except Exception as e:  # noqa: BLE001
                print(f"[턴] 실패 안내 송출 실패: {e}")
        if master_ch:
            try:
                await master_ch.send(master_text or (
                    f"⚠️ 턴 처리 중 예기치 않은 오류({stage}: {type(exc).__name__ if exc else code}) — "
                    f"이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다."))
            except Exception:  # noqa: BLE001
                pass
        await self._settle_failed_turn(session, prep, master_ch)
        await core.save_session_data(self.bot, session)
        return True

    async def _hold_if_closed(self, session, char_name: str, content: str) -> bool:
        """세션이 닫혀 있으면 선언을 보관하고 안내한다(턴 맥락에 기록하지 않음). 다시 열면 이어서 진행한다.

        보관은 런타임 한정(재시작 시 소실) — 새 선언이 오면 새 것으로 교체된다.
        """
        if core.is_session_open(session):
            return False
        session._held_declaration = {"char": char_name, "content": content}
        master_ch = self.bot.get_channel(session.master_ch_id)
        if master_ch:
            await master_ch.send("⚠️ 세션이 닫혀 있어 선언을 보관했습니다. 세션을 열면 이어서 진행합니다.")
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.message_lifecycle.send_transient(
                self.bot, session, game_ch,
                "⏸️ " + core.master.say("cache.closed_held", "세션이 닫혀 있습니다. 선언은 보관해 두었습니다.") + "\n"
                "> 디스플레이 채널의 **세션 열기**를 누르시면 그 선언으로 이어서 진행합니다.",
                key=core.message_lifecycle.KEY_TURN_NOTICE)
        return True

    async def resume_held_declaration(self, session) -> bool:
        """세션 다시 열기 직후 — 닫힌 동안 보관한 선언으로 턴을 진행한다(재입력 불필요)."""
        held = getattr(session, "_held_declaration", None)
        session._held_declaration = None
        if not held or not getattr(session, "gm_active", False) or not core.is_session_open(session):
            return False
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.message_lifecycle.send_transient(
                self.bot, session, game_ch,
                "▶️ " + core.master.say("cache.reopen_held", "보관된 선언으로 이어서 진행합니다.")
                + f"\n> {held['content']}",
                key=core.message_lifecycle.KEY_TURN_NOTICE)
        shim = SimpleNamespace(content=held["content"], channel=game_ch,
                               author=SimpleNamespace(display_name=held["char"]))
        if getattr(session, "gm_waiting_for", None) == held["char"]:
            await self._handle_waiting_response(session, shim, held["char"])
        else:
            await self._handle_player_message(session, shim)
        return True

    async def _handle_player_message(self, session, message: discord.Message):
        """
        기존 자발적 플레이어 발언 처리 경로 (gm_waiting_for 없을 때).
        락 획득 후 _process_actions 호출.
        """
        # 추출층위 미완료 시 다음 턴 차단 (설계문서 1 §5)
        if getattr(session, "extraction_pending", False):
            try:
                await message.channel.send(
                    "⏸️ 이전 턴 정보 정리가 완료되지 않아 진행할 수 없습니다. "
                    "위의 재시도 버튼을 눌러 주십시오.",
                    delete_after=8,
                )
            except Exception:
                pass
            return

        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            # WP-D(D-D4): 라우팅 이후 경합으로 복구 대기가 생겼어도 복구가 eligibility보다 먼저.
            if getattr(session, "commit_recovery", None):
                await self._recover_then_maybe_round(session, master_ch)
                return
            if not session.gm_active:
                return

            if session.gm_turn_cap is not None and session.gm_turns_done >= session.gm_turn_cap:
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.gm_turn_cap}턴) 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            if core.auto_cost_cap_reached(self.bot, session):
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            if await self._roll_pending_blocks(session):
                return
            char_name = session.gm_target_char or message.author.display_name
            if await self._hold_if_closed(session, char_name, message.content.strip()):
                return
            session.gm_clarify_count = 0
            session.current_turn_logs.append(f"[{char_name}]: {message.content.strip()}")

            await self._process_actions(session, message.content.strip(), master_ch)

    async def _process_actions(self, session, player_message: str, master_ch):
        """
        안전장치 확인 후 지시층위 루프(_run_gm_logic_loop) 호출.
        _handle_player_message와 _finalize_round_and_process의 공통 진입 경로.
        이미 락 안에서 호출된다고 가정하므로 이 함수 내부에는 락 없음.
        """
        # WP-D(D-D4): 복구 우선 — eligibility 검사보다 먼저.
        if getattr(session, "commit_recovery", None):
            await self._recover_then_maybe_round(session, master_ch)
            return

        if not session.gm_active:
            return

        if session.gm_turn_cap is not None and session.gm_turns_done >= session.gm_turn_cap:
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.gm_turn_cap}턴) 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        if core.auto_cost_cap_reached(self.bot, session):
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        # WP-C: 현재 시도가 확정 묘사 이후 준비/READY/재시도 대기 중이면 새 선언을
        #   받지 않는다 — 다음 자동 논리 턴은 READY 배리어와 durable 커밋 이후에만.
        _active = core.turn_transaction.get_active_transaction(session)
        _aprep = getattr(_active, "preparation", None) if _active is not None else None
        if _aprep is not None and getattr(_aprep, "phase", None) in (
                core.turn_preparation.PREP_PREPARING,
                core.turn_preparation.PREP_READY,
                core.turn_preparation.PREP_COMMITTING,
                core.turn_preparation.PREP_RETRY_PENDING):
            core.logs.event("turn", "새 선언 차단 — 준비 중 시도", level="WARN", session=session,
                            data={"code": "PREP_IN_FLIGHT", "tx": _aprep.transaction_id, "phase": _aprep.phase})
            if master_ch:
                await master_ch.send("⏸️ 이전 턴 준비가 끝나지 않아 새 선언을 처리하지 않습니다.")
            return

        # H06: 동적 세계 세션이 열리지 않았거나(INITIALIZED) 바인딩 정의를 읽을 수 없으면(BLOCKED) 턴을
        #   받지 않는다 — 조용히 LEGACY 경로로 진행하지 않는다(운영자 확인).
        _blocked = core.world_lifecycle.blocked_reason(session)
        if _blocked:
            _atx = core.turn_transaction.get_active_transaction(session)
            if _atx is not None and getattr(_atx, "world_turn", None) is not None and \
                    core.action_intent.is_definition_stale(_atx.world_turn.ctx.definition_revision, session):
                _atx.world_turn = None                     # 옛 정의 WorldTurn 재사용 금지(R2)
            core.logs.event("turn", "턴 차단 — 동적 세계", level="WARN", session=session,
                            data={"code": "WORLD_BLOCKED", "reason": _blocked})
            if master_ch:
                await master_ch.send(f"⏸️ [동적 세계] {_blocked}")
            return

        # WP-01: 자동 턴 수렴 경계. 안전장치(활성/턴 한도/비용 한도)를 통과한 직후,
        # 하나의 플레이어 선언을 하나의 논리 턴 시도에 대응시킨다. get_or_begin은
        # ASK/NARRATE 재입력 시 같은 트랜잭션을 재사용한다(새 시도를 만들지 않는다).
        if await self._roll_pending_blocks(session):
            return
        tx = core.turn_transaction.get_or_begin_turn_transaction(session, player_message)
        print(f"{tx.log_prefix('PROCESS_ACTIONS')}")
        try:
            await self._run_gm_logic_loop(
                session, player_message, master_ch,
                transaction_id=tx.transaction_id)
        except Exception as e:  # noqa: BLE001 — v7.33(F1): 입력 태스크 밖으로 새지 않게 — 묘사 이전이면 청구 0 종결
            await self._abort_turn_before_narration(
                session, tx.transaction_id, player_message, master_ch,
                code=core.turn_transaction.FailureCode.UNEXPECTED_EXCEPTION, stage="LOGIC_LOOP", exc=e)

    # ─────────────────────────────────────────────────────────────
    # 지시층위 루프 본체
    # ─────────────────────────────────────────────────────────────

    async def _finish_proceed_and_continue(self, session, instruction, master_ch,
                                           *, event_assessment=None,
                                           transaction_id: str | None = None):
        """
        강제/정상 PROCEED 공통 owner — WP-C 트랜잭션 배리어 소유자.

          1) 턴 한도 도달 시 자동 모드 정지 + 마스터 채널 알림
          2) _dispatch_proceed: 묘사 생성 → 확정 묘사 직후 동시 준비(비정규 NPC·추출·
             자동 재계획) 발사 → 전달/스트리밍(준비 작업과 겹침)
          3) 모든 등록 준비 작업 합류 → 레지스트리 봉인 → 비용 멤버십 동결
             → 단일 READY_TO_COMMIT 전이(증명 캡처, 턴 소유 정본은 아직 미적용)
          4) WP-D: READY 이후 권위적 커밋 — core.commit_coordinator.CommitCoordinator
             (PREPARED → 부수효과 없는 적용 → strict 저장 → SESSION_PERSISTED →
              Settlement → InkTransaction → BILLING_APPLIED → durable COMMITTED)
          5) 처리 잠금 해제 → 파생 알림·비용 보고·통계 → 디스플레이 → 다음 라운드
             (다음 라운드는 durable COMMITTED 또는 안전한 실패 처분 이후에만 열린다)
        사전 READY 실패(생성/전달/추출/검증)는 정본 전진 없이 FAILED_SYSTEM Settlement(청구 0).

        Args:
            event_assessment: PROCEED 계열에서 지시층위가 평가한 사건 상태.
                None이면 서사 진행도/재계획 판정을 건너뛴다(강제 PROCEED 폴백 경로).
        """
        if session.gm_turn_cap is not None and (session.gm_turns_done + 1) >= session.gm_turn_cap:
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 마지막 턴]** 자동 턴 한도({session.gm_turn_cap}턴) 도달. "
                    f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                )

        # 되감기 델타 기준점.
        # NOTE: 턴마다 새로 스냅샷을 뜨면, 델타 계산과 다음 스냅샷 사이에 일어난
        #       변화가 어느 델타에도 잡히지 않고 영구 유실된다.
        #       따라서 '마지막으로 기록한 시점의 스냅샷'을 세션에 들고 다니며
        #       그것과 비교한다. (WP-C: 이번 턴 추출/재계획은 READY 이후 델타 기록 '전'에
        #       적용되므로 이번 턴 델타에 귀속된다. rewind 구현 자체는 변경하지 않는다.)
        state_before = getattr(session, "_rewind_snapshot", None)
        if state_before is None:
            state_before = core.capture_state(session)

        # 대기 중인 BGM 재생 — 기획 규정상 온 직후가 아니라 다음 스트리밍 시작 시점이다.
        #   (이전 턴에 확정된 pending_bgm의 소비 — 이번 턴 소유 정본 변경이 아니다.)
        pending = getattr(session, "pending_bgm", None)
        _bgm_consumed = False
        if pending and core.is_enabled(session, "bgm"):
            try:
                media_cog = self.bot.get_cog("MediaCog")
                if media_cog and await media_cog.start_bgm(session, pending):
                    session.pending_bgm = None
                    _bgm_consumed = True
            except Exception as e:
                print(f"[BGM] 재생 실패(진행에는 영향 없음): {e}")

        # WP-C: 트랜잭션 소유 준비 객체 — 새 트랜잭션을 만들지 않는다(PF-18).
        prep = core.turn_preparation.ensure_preparation(session, transaction_id)
        if prep is None:
            print(f"[WP-C/{session.session_id}] 현재 자동 트랜잭션이 아님 — 배리어 owner 불가(tx={transaction_id})")
            if master_ch:
                await master_ch.send("⚠️ 현재 자동 턴 시도가 아니어서 이번 진행을 중단합니다.")
            return
        if _bgm_consumed:
            # B-C6: 이전 턴에 확정된 pending_bgm을 이번 스트리밍 시작에 소비한 정당한 운영
            #   쓰기(묘사·준비 이전). 이후 이 턴의 추출이 새 pending_bgm을 스테이징하므로
            #   기준선을 소비 직후로 재설정하고 사유를 남긴다.
            prep.rebaseline(session, "pending_bgm",
                            "prior-turn committed pending_bgm consumed at stream start "
                            "(before finalized narration)")
        prep.phase = core.turn_preparation.PREP_PREPARING
        prep.event_assessment = event_assessment
        _wt = self._attach_world_turn(session, prep, transaction_id)

        outcome = None
        result = None
        open_round = False
        try:
            try:
                if _wt is not None and _wt.plan is not None \
                        and _wt.plan.status == core.player_world.PLAN_CONFLICT:
                    # DESIGN 11 §30: 묘사 전 결정적 충돌 — provider 묘사 호출 없이 종결(재시도 없음).
                    outcome = await self._world_conflict_outcome(session, prep, stage="pre_narration")
                else:
                    await self._dispatch_proceed(
                        session, instruction, transaction_id=transaction_id, preparation=prep)
                    outcome = await self._join_and_ready(session, prep)
            except Exception as e:
                # 사전 READY 예기치 못한 예외 — 준비 작업을 합류시키고 실패로 처리한다.
                core.logs.event("turn", "준비 중 예외", level="ERROR", session=session, exc=e,
                                data={"code": "PREP_EXCEPTION", "tx": prep.transaction_id})
                prep.violations.append(f"exception:{type(e).__name__}")
                outcome = await self._join_after_exception(session, prep)
            if outcome == _OUTCOME_READY:
                result = await self._commit_ready_turn(
                    session, prep, state_before=state_before)
            else:
                open_round = await self._handle_preparation_failure(
                    session, prep, outcome, master_ch)
        finally:
            await self._release_turn_processing(session, prep)

        if outcome == _OUTCOME_READY:
            if not await self._after_commit(session, prep, result, master_ch):
                return
            open_round = True
        # WP-E: 재생성 교체 시도가 커밋되지 못했으면 이전 선택 시도의 정본을 되돌린다.
        await self._settle_history_op(session, prep, master_ch)
        # 다음 라운드: durable COMMITTED 또는 안전하게 종결된 실패만. 복구 대기(차단)면 금지.
        if open_round and session.gm_active and not getattr(session, "commit_recovery", None):
            await self._start_round(session)

    # ─────────────────────────────────────────────────────────────
    # H05 — 턴 세계(구조화 의도 · 플레이어 세계 계획 · 관찰 · 후속 묘사)
    #   DYNAMIC 세션(정의 바인딩·시계 초기화·PC 배치)만. LEGACY 세션은 모두 None → 기존 경로 그대로.
    # ─────────────────────────────────────────────────────────────

    def _world_turn(self, session, transaction_id, *, create=False):
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is None or transaction_id is None or tx.transaction_id != transaction_id:
            return None
        wt = getattr(tx, "world_turn", None)
        if wt is not None and core.action_intent.is_definition_stale(wt.ctx.definition_revision, session):
            # R2: 정의 revision이 바뀌었다 — 옛 매니페스트·계획·경로 선택을 같은 시도 재진입에서도 재사용하지 않는다
            core.write_log(session.session_id, "api",
                           f"[H05 정의 변경] 옛 WorldTurn 폐기 rev={wt.ctx.definition_revision} "
                           f"→ {getattr(session, 'definition_revision', None)}")
            tx.world_turn = wt = None
        if wt is None and create:
            ctx = core.player_world.session_world_context(session)
            if not ctx.dynamic:
                return None
            wt = core.turn_world.WorldTurn(ctx=ctx)
            tx.world_turn = wt
        return wt

    def _tx_judgment(self, session, transaction_id) -> dict | None:
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is None or transaction_id is None or tx.transaction_id != transaction_id:
            return None
        return tx.judgment_result or None

    def _gate_inputs(self, session, transaction_id) -> dict:
        """v7.14 선언 게이트 입력 — DYNAMIC만: PC 정본 장소·표시명·유효 발견(D키)·찾지 못한 이전 탐색."""
        out = {"dynamic": False, "place": None, "place_name": None, "rows": [], "failed": []}
        wt = self._world_turn(session, transaction_id, create=True)
        if wt is None:
            return out
        ctx = wt.ctx
        place = core.turn_world.pc_place(ctx, session)
        out["dynamic"] = True
        if not place:
            return out
        out["place"] = place
        try:
            out["place_name"] = core.turn_world.pc_place_name(ctx, session, place)
            out["rows"] = core.turn_world.discoveries_at(ctx.definition, session, ctx.lead_pc, place)
            out["failed"] = core.turn_history.recent_failed_discoveries(session.session_id, place)
        except Exception as e:
            print(f"[선언 게이트] 입력 조립 실패(발견 목록 생략): {e}")
        return out

    async def _voice_lines(self, session, lines, *, transaction_id, kind: str = "master") -> None:
        """P2 Q7: 액자 밖 마스터 직접 발화(ASK·경로/목적지 질문·게이트 안내)와 NARRATE 본문을 더빙한다.

        TTS 실효(설정 켜짐 ∧ 음성 채널 연결)일 때만 합성한다 — 꺼짐·미연결이면 provider 호출 0(P2 #9).
        이 턴 트랜잭션의 비용 멤버로 claim해 같은 Settlement로 정산한다(판단층위 ASK 비용과 같은 귀속).
        트랜잭션이 없는 안내(라운드 차례 안내 등)는 정산 귀속이 없어 더빙하지 않는다.
        """
        on, live, _r = core.media_control.tts_status(session)
        if not (on and live) or not lines or transaction_id is None:
            return
        game_cog = self.bot.get_cog("GameCog")
        if game_cog is None or not hasattr(game_cog, "_synthesize_and_enqueue"):
            return
        VP = core.voice_profile
        items = []
        for t in lines:
            if kind == "master":
                txt = core.clean_text_for_tts(t)
                if txt:
                    items.append((VP.master_voice(session), VP.style_for(VP.KIND_MASTER), txt))
            else:
                v, st, sp, _k = VP.paragraph_voice(session, t)
                if sp:
                    items.append((v, st, sp))
        if not items:
            return
        try:
            dub = await game_cog._synthesize_and_enqueue(session, items, claim_tx=transaction_id)
        except Exception as e:  # noqa: BLE001 — 음성 실패는 턴 진행을 막지 않는다
            print(f"[TTS] 진행자 안내 더빙 실패(진행 유지): {type(e).__name__}: {e}")
            return
        if dub.get("cost", 0) > 0:
            core.accrue(session, dub["cost"], dub.get("usd"))
            session.turn_cost_log = list(getattr(session, "turn_cost_log", None) or []) + [{
                "label": f"TTS {'진행자 안내' if kind == 'master' else 'NARRATE'}({dub['enqueued']}/{dub['total']})",
                "cost": dub["cost"], "in": dub["in"], "cached": 0, "out": dub["out"], "model": core.TTS_MODEL}]

    async def _hold_declaration(self, session, transaction_id, player_message, gate, text, master_ch) -> None:
        """v7.14: 게이트 BLOCK·CLARIFY·검증 불가 — 굴림·세계 연산 없이 중립 안내 후 플레이어 재선언 대기."""
        core.turn_history.note_judgment(
            session, transaction_id,
            judgment={"action": "ASK", "bridge_message": text, "player_intent": "",
                      "reasoning": "선언 게이트 " + (f"{gate['resolution']}/{gate['reason_code']}" if gate else "검증 불가")},
            player_message=player_message, roll_results=[], gate=gate)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.stream_text_to_channel(self.bot, game_ch, text, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [text], transaction_id=transaction_id)
        session.current_turn_logs.append(f"[진행자 (GM)]: {text}")
        core.turn_transaction.mark_waiting_for_player(session, transaction_id)
        if master_ch:
            await master_ch.send("🚧 **[선언 게이트]** " + (
                f"{gate['classification']} → {gate['resolution']} ({gate['reason_code']})" if gate
                else "검증 불가(시뮬레이션 게이트 없음) — 발견 제안 미처리"))
        await core.save_session_data(self.bot, session)

    def _build_world_manifest(self, session, wt) -> None:
        ctx = wt.ctx
        uid = ctx.uid_of.get(ctx.lead_pc)
        routes = wt.plan.route_options if (wt.plan is not None and wt.plan.status
                                           == core.player_world.PLAN_NEEDS_CHOICE) else ()
        access = core.player_world.access_confirm_options(
            ctx.definition, session, ctx.lead_pc,
            core.player_world.pc_access_capabilities(session, uid, ctx.definition))   # v7.17.2: 소속 포함(접근 전용)
        pend = self._pending_move(session, wt)
        wt.manifest = core.action_intent.build_manifest(
            ctx.definition, session, ctx.lead_pc, definition_revision=ctx.definition_revision,
            route_options=routes, access_options=access, asked_exits=pend["exits"] if pend else ())

    @staticmethod
    def _pending_move(session, wt):
        """v7.22 목적지 질문 문맥 — 같은 시도(WorldTurn)·같은 장소·같은 world_version일 때만 유효(아니면 버린다).
        재시작·다른 세션·다른 시도에는 남지 않는다(트랜잭션 런타임 기록)."""
        pend = wt.pending_move
        if pend is None:
            return None
        now = core.turn_world.move_question_anchor(wt.ctx, session)
        if pend.get("place") != now["place"] or pend.get("world_version") != now["world_version"]:
            wt.pending_move = None
            return None
        return pend

    def _settle_move_intent(self, session, wt, transaction_id) -> None:
        """v7.22: 판단 의도의 출구 이동을 확정한다(판단 직후, 이력 기록 전 — 재서술·재시도는 같은 구간).

        · 질문 문맥이 있는데 다시 길을 정하지 않은 이동(목적지 미정·WANDER)이면 되묻지 않고 한 구간 간다 —
          질문이 제시한 후보 안에서만. 질문 없이 WANDER면 현재 출구 전체(조건 키가 있으면 그 안).
        · 출구가 정해지면·다른 행동이면 질문 문맥을 내린다. 모호한 지목(AMBIGUOUS)은 그 후보로 다시 묻는다."""
        intent = wt.intent
        if intent is None or intent.stale:
            return
        pend = self._pending_move(session, wt)
        mv = intent.movement
        move_open = [(f, c) for f, c in intent.unresolved if f in ("destination", "exit")]
        unspecified = (mv is not None and mv.kind == core.action_intent.MOVE_WANDER and not mv.edge_id) or (
            pend is not None and move_open and all(u == ("destination", "MISSING") for u in move_open))
        if unspecified:
            pool = tuple(mv.pool) if mv is not None and mv.kind == core.action_intent.MOVE_WANDER else ()
            if pend is not None:
                asked = set(pend["exits"])
                pool = tuple(e for e in pool if e in asked) or tuple(sorted(asked))   # 질문 범위를 넓히지 않는다
            pick = core.player_world.wander_exit(
                wt.ctx.definition, session, wt.ctx.lead_pc, pool=pool, transaction_id=transaction_id,
                narrative_seed=getattr(session, "narrative_seed", None) or session.session_id)
            wt.intent = intent = dataclasses.replace(
                intent, movement=core.action_intent.MovementIntent(
                    core.action_intent.MOVE_WANDER, pick[1] if pick else None, pick[0] if pick else None),
                exit_choices=(), unresolved=tuple(u for u in intent.unresolved if u not in move_open))
            core.write_log(session.session_id, "api", f"[v7.22 출구 선택] pool={len(pool)} pick={pick}")
        if not any(f in ("destination", "exit") for f, _c in intent.unresolved):
            wt.pending_move = None

    def _plan_world(self, session, wt, transaction_id, *, resolved_tie_orders=None):
        ctx = wt.ctx
        intent = wt.intent or core.action_intent.ResolvedActionIntent()
        plan = self._plan_world_core(session, wt, transaction_id, intent, resolved_tie_orders)
        # v7.25(F03): 턴 시작에 PC와 함께 있던 동행은 같은 계획(묘사 전)에서 PC와 함께 움직인다
        return core.turn_world.companion_follow(
            ctx, session, plan, list(getattr(session, "companions", None) or ()),
            prefix=core.turn_world.turn_op_prefix("cmpf", transaction_id))

    def _plan_world_core(self, session, wt, transaction_id, intent, resolved_tie_orders):
        ctx = wt.ctx
        return core.player_world.plan_player_turn(
            ctx, session, intent, transaction_id=transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            capabilities=core.player_world.pc_capabilities(session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),
            access_capabilities=core.player_world.pc_access_capabilities(
                session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),                     # v7.17.2: 접근 판정 전용
            item_facts=core.items.current_facts(session),           # v7.17: 직전 커밋 턴의 사실만
            stats=core.player_world.pc_stats(session, ctx.uid_of.get(ctx.lead_pc)),       # H07: 스탯 조건
            resolved_tie_orders=resolved_tie_orders)

    def _ensure_world_plan(self, session, wt, transaction_id):
        cur = core.action_intent.world_version_id(session)
        if wt.plan is None or wt.plan.source_world_version_id != cur or \
                core.action_intent.is_definition_stale(wt.plan.definition_revision, session):
            wt.plan = self._plan_world(session, wt, transaction_id)
            wt.replan = lambda orders, _s=session, _w=wt, _t=transaction_id: self._plan_world(
                _s, _w, _t, resolved_tie_orders=orders)
            core.write_log(session.session_id, "api",
                           f"[H05 계획] status={wt.plan.status} reason={wt.plan.reason} "
                           f"outcome={json.dumps(wt.plan.outcome, ensure_ascii=False, default=str)[:600]}")
        return wt.plan

    async def _ask_route_choice(self, session, plan, transaction_id, master_ch) -> None:
        """의미 있는 경로 차이 — 플레이어 지식 범위의 선택지를 묻는다(실제 숨은 상태 미포함)."""
        lines = [core.master.say("turn.route_question", "어느 길로 가시겠습니까?")]
        for n, o in enumerate(plan.route_options, 1):
            mins = f" (약 {o['minutes']}분)" if o.get("minutes") is not None else ""
            lines.append(f"{n}) {o['label']}{mins}")
        bridge = "\n".join(lines)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.stream_text_to_channel(self.bot, game_ch, bridge, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [bridge], transaction_id=transaction_id)
        session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
        # v7.23: 질문을 낸 뒤에만 번호 응답 스냅샷을 건다(같은 시도·장소·world_version에서만 유효)
        wt = self._world_turn(session, transaction_id)
        if wt is not None:
            wt.pending_route = dict(core.turn_world.move_question_anchor(wt.ctx, session),
                                    signatures=[list(o["signature"]) for o in plan.route_options])
        if master_ch:
            await master_ch.send(f"🧭 **[경로 선택]** 후보 {len(plan.route_options)}개 — 플레이어 선택 대기")
        core.turn_transaction.mark_waiting_for_player(session, transaction_id)

    def _route_number_answer(self, session, transaction_id, player_message):
        """v7.23: 대기 중인 경로 질문에 번호만 답했다('1', '1번') → 그 질문의 선택지 스냅샷으로 고른 판단(재호출 없음).

        번호 밖·다른 말이 섞임·기준선(장소·world_version)이 바뀜·직전 판단 없음이면 None(일반 판단으로)."""
        wt = self._world_turn(session, transaction_id)
        pend = getattr(wt, "pending_route", None) if wt is not None else None
        if not pend or wt.intent is None:
            return None
        m = re.fullmatch(r"\s*(\d{1,2})\s*(?:번|번째)?\s*[.!]?\s*", str(player_message or ""))
        if not m:
            return None
        anchor = core.turn_world.move_question_anchor(wt.ctx, session)
        sigs = pend.get("signatures") or []
        n = int(m.group(1))
        prior = (self._tx_judgment(session, transaction_id) or {}).get("judgment")
        if (anchor["place"] != pend.get("place") or anchor["world_version"] != pend.get("world_version")
                or not 1 <= n <= len(sigs) or not isinstance(prior, dict)):
            wt.pending_route = None                        # 낡은 번호는 다시 쓰지 않는다
            return None
        wt.pending_route = None
        wt.intent = dataclasses.replace(wt.intent, route_signature=tuple(sigs[n - 1]))
        wt.plan = None
        core.turn_transaction.mark_transaction_status(
            session, transaction_id, core.turn_transaction.TurnStatus.JUDGING)
        core.logs.event("turn", "경로 질문 번호 응답", session=session,
                        data={"stage": "route_answer", "tx": transaction_id, "choice": n, "options": len(sigs)})
        return {k: v for k, v in prior.items() if k != core.action_intent.INTENT_FIELD}

    async def _dynamic_declaration_check(self, session, player_message, roll_results, judgment, gate_in,
                                         master_ch, *, transaction_id, cache_ok):
        """v7.23 DYNAMIC 조건부 선언 검증 — (judgment, gate).

        판단이 주변 발견 제안(ambient_proposal·discovery 초안)이나 결과 선취(assertion_claim)를 표시했을 때만 검증한다.
        · 이 장소에서 이미 발견한 같은 대상이면 코드가 재사용(검증 호출 없음).
        · 그 밖은 캐시 룰북으로 축소 검증 1회 → 기존 _parse_gate(원문 대상명·D키·열거값 검증).
        · 검증 결과와 판단 초안(발견 굴림)이 어긋나면 검증 결과를 보여 주고 판단을 한 번 다시 부른다(기존 게이트 계약).
        · 검증할 수 없으면 None — 호출자가 새 발견은 보류(fail-closed), 결과 선취만이면 시도로 진행한다."""
        rolls = [r for r in (judgment.get("rolls") or []) if isinstance(r, dict)]
        ambient = judgment.get("ambient_proposal") is True or any(r.get("discovery") is True for r in rolls)
        assertion = judgment.get("assertion_claim") is True
        pk = dict(dynamic=True, place=gate_in["place"], place_name=gate_in["place_name"], rows=gate_in["rows"])
        ref = judgment.get("discovery_ref")
        if not (ambient or assertion):
            # v7.14 A1 근거 — 선언이 이 장소의 기존 발견(D키)을 가리킨다(코드 확정 게이트, 검증 호출 없음)
            if isinstance(ref, str) and any(r[0] == ref for r in gate_in["rows"]):
                return judgment, _parse_gate({"classification": "ORDINARY", "resolution": "CONTINUE",
                                              "reason_code": "NONE", "existing_key": ref}, player_message, **pk)
            return judgment, None
        gate, source = None, None
        target = judgment.get("discovery_target")
        if ambient and not assertion:
            row = (next((r for r in gate_in["rows"] if r[0] == ref), None)
                   or next((r for r in gate_in["rows"] if isinstance(target, str)
                            and core.turn_world.same_target(r[2], target)), None))
            if row is not None:
                gate = _parse_gate({"classification": "AMBIENT_PROPOSAL", "resolution": "ACCEPT_NO_ROLL",
                                    "reason_code": "NONE", "discovery_target": row[2], "discovery_kind": row[3],
                                    "existing_key": row[0]}, player_message, **pk)
                if gate is not None and gate.get("existing_info_id"):
                    source = "CODE"
                else:
                    gate = None
        if gate is None and cache_ok:
            raw = await self._call_declaration_gate(session, player_message, judgment, gate_in, master_ch,
                                                    transaction_id=transaction_id)
            gate = _parse_gate((raw or {}).get("declaration_gate"), player_message, **pk)
            source = "LLM" if raw is not None else None
        core.logs.event("turn", "선언 검증", session=session, data={
            "stage": "declaration_check", "tx": transaction_id, "ambient": ambient, "assertion": assertion,
            "source": source, "result": None if gate is None else
            f"{gate['classification']}/{gate['resolution']}/{gate['reason_code']}"})
        if master_ch and (gate is None or _gate_blocks(gate)):     # v7.35: 진행하는 판정은 층위활동 보고(🧭)로
            await master_ch.send("🛡️ **[선언 검증]** " + (
                f"{gate['classification']} → {gate['resolution']} ({gate['reason_code']})"
                + (" · 기존 발견 재사용" if source == "CODE" else "") if gate else "검증 불가(캐시 없음·응답 오류)"))
        if gate is None or _gate_blocks(gate):
            return judgment, gate
        need = _gate_rolls_discovery(gate)
        has = str(judgment.get("action", "")).upper() == "ROLL" and any(r.get("discovery") is True for r in rolls)
        if need != has and (need or gate.get("resolution") == "ACCEPT_NO_ROLL"):
            # 초안과 검증 결과가 어긋난다 — 검증 결과를 준 판단 한 번(굴림 0·발견 0인 상태에서). 실패면 진행하지 않는다.
            judgment = await self._call_judgment(session, player_message, roll_results, master_ch,
                                                 transaction_id=transaction_id, gate=gate)
        return judgment, gate

    async def _call_declaration_gate(self, session, player_message, judgment, gate_in, master_ch,
                                     *, transaction_id) -> dict | None:
        """v7.23 축소 선언 검증(캐시 룰북) — 기존 시뮬레이터의 선언 게이트 규칙·스키마만 재사용한다.\n\n        서사 방향·공리·개연성 등급은 묻지도 받지도 않는다. 응답은 열거값·원문 대상명뿐(정본 사실 아님)."""
        cache_name = getattr(session, "cache_name", None)
        rules = NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION[
            NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION.index("[선언 게이트 규칙 — declaration_gate]"):]
        instruction = ("당신은 선언 검증기다. 시나리오 캐시의 룰북·세계관만 근거로, 아래 규칙에 따라 이번 플레이어 선언의 "
                       "declaration_gate 하나만 판정한다. 서사 방향·사건 전개를 제안하지 않는다.\n\n" + rules)
        recent = []
        for content in session.raw_logs[-2:]:
            try:
                recent.append(f"[{content.role.upper()}]\n{content.parts[0].text}")
            except Exception:
                continue
        gi = gate_in
        rows, failed = gi.get("rows") or [], gi.get("failed") or []
        user_prompt = (
            f"[현재 장소]\n{gi.get('place_name') or '(장소 밖)'}\n\n"
            f"[최근 게임 로그]\n{chr(10).join(recent) or '(없음)'}\n\n"
            f"[이번 턴 대화(커밋 전)]\n{chr(10).join(session.current_turn_logs[-8:]) or '(없음)'}\n\n"
            f"[판단층위가 읽은 시도]\n{judgment.get('player_intent') or ''}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n\n"
            "[이 장소에서 PC가 이미 발견한 것]\n"
            + (core.turn_world.discovery_rows_text(rows) if rows else "(없음)")
            + "\n[이 장소에서 찾지 못한 이전 탐색 — 실패는 부재가 아니다]\n"
            + ("\n".join(f"· {f['target']} — 당시 선언: {f['declaration']}" for f in failed) if failed else "(없음)")
            + "\n\n[지시사항]\n이번 플레이어 선언을 [선언 게이트 규칙]에 따라 declaration_gate로 판정하십시오.")
        schema = {"type": "object",
                  "properties": {"declaration_gate": NARRATIVE_DIRECTION_SCHEMA["properties"]["declaration_gate"]},
                  "required": ["declaration_gate"]}
        try:
            contents = [
                types.Content(role="user", parts=[types.Part.from_text(text=instruction)]),
                types.Content(role="model", parts=[types.Part.from_text(text="이해했습니다. 선언만 판정하겠습니다.")]),
                types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]),
            ]
            config = types.GenerateContentConfig(
                http_options=core.resilience.provider_http_options("instruction"),
                cached_content=cache_name, temperature=0.2, response_mime_type="application/json",
                response_schema=schema, safety_settings=core.TRPG_SAFETY_SETTINGS)
            _cl_op = core.cost_ledger.begin_operation(
                self.bot, core.cost_ledger.OP_TURN_DECLARATION_CHECK, session=session,
                model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
                billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
            core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(self.bot.genai_client.models.generate_content,
                                          model=core.DEFAULT_MODEL, contents=contents, config=config),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id)
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            core.logs.event("turn", "선언 검증 호출 실패", level="WARN", session=session, exc=e,
                            data={"code": "DECLARATION_CHECK_CALL"})
            return None
        try:
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(response.usage_metadata)
            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens, cached_read_tokens=cached_tokens)
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(input_tokens=in_tokens, cached_input_tokens=cached_tokens, output_tokens=out_tokens,
                          thought_tokens=thought_tokens, cost_usd=breakdown["total_usd"], cost_krw=cost,
                          usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}선언 검증",
                                in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            session.turn_cost_log = list(getattr(session, "turn_cost_log", None) or []) + [{
                "label": "선언 검증", "cost": cost, "in": in_tokens, "cached": cached_tokens, "out": out_tokens}]
        except Exception as e:
            print(f"[GM] 선언 검증 비용 정산 실패: {e}")
        raw_text = response.text or ""
        try:
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE))
        except Exception as e:
            core.logs.event("turn", "선언 검증 JSON 파싱 실패", level="WARN", session=session, exc=e,
                            data={"code": "DECLARATION_CHECK_JSON"})
            return None

    async def _ask_destination(self, session, wt, transaction_id, master_ch) -> None:
        """목적지 불명 이동 — PC가 아는 출구를 제시하며 묻는다(묘사·추출 호출 없음).

        v7.22: 모호한 출구 지목이면 그 후보만 묻는다. 질문이 나간 뒤에만 질문 문맥을 건다(전송 실패 = 문맥 없음) —
        다음 답이 다시 길을 정하지 않으면 되묻지 않고 그 후보 안에서 한 구간 간다(_settle_move_intent)."""
        prev = self._pending_move(session, wt)
        bridge, asked = core.turn_world.destination_question(
            wt.ctx, session, (wt.intent.exit_choices if wt.intent is not None else ()) or (prev or {}).get("exits"))
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await core.stream_text_to_channel(self.bot, game_ch, bridge, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [bridge], transaction_id=transaction_id)
        wt.pending_move = dict(core.turn_world.move_question_anchor(wt.ctx, session),
                               exits=tuple(asked)) if asked else None
        session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
        if master_ch:
            await master_ch.send("🧭 **[목적지 확인]** 이동 목적지가 정본 후보에 없어 되묻습니다")
        core.turn_transaction.mark_waiting_for_player(session, transaction_id)

    def _attach_world_turn(self, session, prep, transaction_id):
        """PROCEED 배리어 소유 시작 — 계획을 준비 객체에 붙이고 묘사용 투영 장면을 만든다."""
        wt = self._world_turn(session, transaction_id)
        if wt is None:
            return None
        plan = self._ensure_world_plan(session, wt, transaction_id)
        if plan.status == core.player_world.PLAN_NEEDS_CHOICE:
            # 선택을 물을 수 없는 경로(ROLL 후·강제 PROCEED) — 이동 없이 묘사(선택은 다음 선언에서).
            plan.status, plan.reason = core.player_world.PLAN_REJECTED, "ROUTE_CHOICE_PENDING"
        if plan.status == core.player_world.PLAN_TIE:
            # P1(D2): 묘사 전에 같은 분 선후를 정한다 — 지시층위 선택(허용 키) 우선, 무효·미선택은 코드 결정
            plan, rec = core.turn_world.resolve_plan_tie(plan, wt.replan, wt.tie_choice, wt.ctx.definition, wt.ctx)
            wt.plan = plan
            core.write_log(session.session_id, "api",
                           f"[P1 선후] {json.dumps(rec, ensure_ascii=False)} → status={plan.status}")
        prep.world_turn = wt
        if plan.projected_world is not None:
            prep.world_prompt_timeline = core.turn_world.mirror_timeline(
                getattr(session, "world_timeline", {}) or {}, wt.ctx.definition,
                plan.projected_world, wt.ctx.lead_pc)
        return wt

    def _world_diagnostic(self, session, prep, code, detail) -> None:
        """DESIGN 11 §32 — 마스터 진단(내구 로그). 숨은 정보는 플레이어 채널에 보내지 않는다."""
        rec = {"code": code, "transaction_id": prep.transaction_id,
               "world_version": core.action_intent.world_version_id(session),
               "definition_revision": getattr(session, "definition_revision", None), "detail": detail}
        core.logs.warn("world", f"세계 진단 {code}", session=session, data=rec)
        diags = getattr(prep, "world_diagnostics", None)   # v7.30: 그 턴 전수보고(층위활동)에 함께 싣는다(보조 — 실패 무관)
        if isinstance(diags, list):
            diags.append(rec)

    async def _world_conflict_outcome(self, session, prep, *, stage: str) -> str:
        prep.seal()
        try:
            prep.close_cost_membership()
        except core.turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"cost_close:{e}")
        prep.failure_code = core.turn_transaction.FailureCode.WORLD_PROJECTION_CONFLICT.value
        prep.failure_stage = f"WORLD_{stage.upper()}"
        wt = getattr(prep, "world_turn", None)
        self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                               {"stage": stage, "diagnostics": getattr(getattr(wt, "plan", None), "diagnostics", None)})
        return _OUTCOME_NOT_READY

    def _process_world_observation(self, session, prep, plan, master_ch) -> None:
        """추출 관찰 처리(순수) + 레거시 장소/시간 값을 정본 미러로 대체 + 후속 묘사 등록."""
        wt = prep.world_turn
        ctx = wt.ctx
        _man = wt.obs_manifest or core.turn_world.ObservationManifest()
        _raw = prep.world_observation_raw
        # A1 보정: 추출이 D키를 빠뜨린 명확·유일한 현장 발견물 수거만 키 기반 수거로 정규화(이름만으로는 연결 안 함)
        _implied = self._implied_discovery_take(session, prep.transaction_id, ctx, wt, _man, _raw, plan)
        if _implied:
            _raw = {**(_raw if isinstance(_raw, dict) else {}),
                    "discovery_taken": list((_raw or {}).get("discovery_taken") or []) + [_implied]}
            core.write_log(session.session_id, "api", f"[v7.14 발견 수거 키 복구] {_implied}")
            # 복구된 수거는 그 PC +1(item_changes) 한 건이 설명한다 — 같은 물리 수거를 두 번 세지 않는다
            _row = _man.discovery_of(_implied)
            _pc = self._pc_name(session, ctx)
            _hit = next((e for e in plan.item_deltas if _row and e.get("target") == _pc and int(e.get("delta") or 0) == 1
                         and core.turn_world.same_target(e.get("item"), _row[2])), None)
            if _hit is not None:
                plan.item_deltas = [e for e in plan.item_deltas if e is not _hit]
        # v7.17 아이템: 같은 기준 재고에 모든 연산을 순서대로 가상 적용 — 하나라도 어긋나면 전체를 재추출(부분 적용 없음).
        #   발견물 수거(D키)·코드 지급(접근 확인)·문서 읽기를 하나의 계획으로 묶고, 수거한 D키만 소진(SUPERSEDED)한다.
        item_plan = self._build_item_plan(session, prep, ctx, wt, _raw, plan)
        item_plan.facts["evaluated_same_turn"] = True     # P1(D4): 이 턴 보충 진행이 사용·지급 사실을 평가한다
        _raw = {**(_raw if isinstance(_raw, dict) else {}), "discovery_taken": list(item_plan.discovery_takes)}
        res = core.turn_world.process_observation(
            ctx, session, wt.plan, _man,
            _raw, transaction_id=prep.transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            replan=wt.replan,
            face_info_ids=self._revealed_identity_ids(session, prep, wt.identified_faces, "face"),
            name_info_ids=self._revealed_identity_ids(session, prep, wt.identified_names, "name"),
            discovery=wt.discovery, read_info_ids=item_plan.read_info_ids,
            pending=core.turn_world.pending_signals(session), item_facts=item_plan.facts)
        wt.result = res
        core.write_log(session.session_id, "api",
                       f"[H05 관찰] status={res.status} exposures={list(res.exposures)} "
                       f"extra={res.extra_minutes} pending={len(res.pending)} "
                       f"diag={json.dumps(res.diagnostics, ensure_ascii=False, default=str)[:600]}")
        if res.status != core.turn_world.TW_READY:
            return
        # v7.25(F03): 동행 합류는 제안 — 정본 행위자이고 턴 끝에 PC와 같은 장소일 때만 관계로 확정. 이탈은 배치 해제.
        _kept, _clears, _cdiags = core.turn_world.companion_outcome(
            ctx, res.projected_world, res.ops, joined=plan.companions_joined, left=plan.companions_left,
            prefix=core.turn_world.turn_op_prefix("cmpo", prep.transaction_id))
        if _cdiags:
            _rej = {d.get("name") for d in _cdiags if d.get("code") == "COMPANION_JOIN_REJECTED"}
            plan.companions_joined = list(_kept)
            plan.entries = [e for e in plan.entries
                            if not (e.get("domain") == "companion" and e.get("op") == "join" and e.get("target") in _rej)]
            plan.diagnostics["companion"] = list(_cdiags)
            self._world_diagnostic(session, prep, "COMPANION_JOIN_REJECTED", {"diagnostics": _cdiags})
        if _clears:
            res.ops = tuple(res.ops) + tuple(_clears)
        # 장소·시간 권위 = 정본 세계. 레거시 적용부는 추출값이 아니라 정본 미러를 받는다.
        prev_tl = getattr(session, "world_timeline", {}) or {}
        new_tl = core.turn_world.mirror_timeline(prev_tl, ctx.definition, res.projected_world, ctx.lead_pc)
        plan.world_new_tl = new_tl
        plan.world_player_faction = core.turn_world.derived_player_faction(res.projected_world)   # H07-C 호환 뷰
        plan.location_before = prev_tl.get("current_location")
        plan.location_after = new_tl.get("current_location")
        plan.location_moved = plan.location_before != plan.location_after
        # v7.17: 동적 세계의 소지품 권위 = 검증된 아이템 계획 하나(발견물 수거 +1도 그 안의 ACQUIRE). item_deltas는 쓰지 않는다.
        plan.item_plan = item_plan
        plan.item_deltas = []
        plan.entries = [e for e in plan.entries if e.get("domain") != "item"] + [
            {"domain": "item", "target": r["target"], "op": "delta", "payload": {"item": r["item"], "delta": r["delta"]}}
            for r in item_plan.report]
        if item_plan.diagnostics:
            plan.diagnostics["item_plan"] = list(item_plan.diagnostics)
        if res.pending:
            # P1(#1): 후속 묘사 없음 — 본문이 전하지 못한 관찰 신호는 커밋과 함께 전달 대기로 남는다(유실 금지)
            self._world_diagnostic(session, prep, "SIGNALS_PENDING_DELIVERY",
                                   {"signals": [[r["signal_id"], r["instance_id"]] for r in res.pending]})

    @staticmethod
    def _revealed_identity_ids(session, prep, actor_ids, kind) -> tuple:
        """P3 G3: 식별 후보 중 본문에서 실제 공개가 확인된 InfoId만 정본 인지로 — 이미 아는 것은 그대로(멱등)."""
        if not core.speaker_card.card_world(session):
            return ()
        ids = core.speaker_card.identity_info_ids(core.speaker_card.config_of(session.scenario_data), actor_ids, kind)
        shown = set(getattr(prep, "identity_reveals", None) or {})
        return tuple(i for i in ids if i in shown)

    def _implied_discovery_take(self, session, transaction_id, ctx, wt, manifest, raw, plan):
        """A1 보정 근거 조립 — 게이트가 검증한 선언 대상(기존 발견 D키 또는 이번 턴 새 발견)만 후보."""
        gate = (self._tx_judgment(session, transaction_id) or {}).get("gate")
        if wt.discovery and wt.discovery.get("status") == "NEW":
            ref_key = core.turn_world.DISCOVERY_NEW_KEY
        else:
            iid = (gate or {}).get("referenced_info_id")
            ref_key = next((r[0] for r in manifest.discoveries if iid and r[1] == iid), None)
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        return core.turn_world.implied_discovery_take(
            manifest, raw, ref_key, plan.item_deltas, pc_name,
            ((getattr(session, "resources", None) or {}).get(pc_name) or {}) if pc_name else {})

    @staticmethod
    def _pc_name(session, ctx):
        uid = ctx.uid_of.get(ctx.lead_pc)
        return ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")

    def _item_manifest(self, session, wt):
        """v7.17: 이번 추출의 아이템 후보(PC + 장면 정의 NPC의 정본 소지·현장 물건·발견물·저작 제작법·코드 지급)."""
        ctx = wt.ctx
        pc_name = self._pc_name(session, ctx)
        scene, projs = core.turn_world.scene_npc_projections(ctx, wt.plan, session)
        owners = [p.get("legacy_name") for p in projs if p.get("legacy_name")]
        pays = [f"{p.get('capability')} ×{p.get('quantity')}(접근 확인 지급)"
                for p in (getattr(wt.plan, "item_payments", None) or ())]
        return core.items.build_manifest(
            session, pc_name=pc_name, scene_owner_names=owners, scene_place=scene,
            defs=core.items.definitions_of(ctx.definition),
            discovery_rows=(wt.obs_manifest.discoveries if wt.obs_manifest is not None else ()),
            recipes=ctx.definition.get("item_recipes"), payments=pays)

    def _build_item_plan(self, session, prep, ctx, wt, raw, plan):
        """v7.17: 추출 item_ops(+ 발견 D키 수거·코드 지급) → 검증된 ItemPlan. 실패하면 ItemContractError(재추출)."""
        pc_name = self._pc_name(session, ctx)
        defs = core.items.definitions_of(ctx.definition)
        man = getattr(wt, "item_manifest", None) or self._item_manifest(session, wt)
        pays = []
        for p in (getattr(wt.plan, "item_payments", None) or ()):
            op = core.items.payment_op(session, pc_name=pc_name, capability=p.get("capability"),
                                       quantity=int(p.get("quantity") or 1), defs=defs)
            if op is None:
                raise core.items.ItemContractError(f"접근 확인 지급 불가: {p.get('capability')} ×{p.get('quantity')}")
            pays.append(op)
        obs = raw if isinstance(raw, dict) else {}
        return core.items.build_plan(
            session, man, obs.get(core.items.OPS_FIELD), defs=defs, recipes=ctx.definition.get("item_recipes"),
            pc_name=pc_name, transaction_id=prep.transaction_id,
            discovery_taken=[k for k in (obs.get("discovery_taken") or ()) if isinstance(k, str)],
            legacy_item_deltas=plan.item_deltas, payments=pays,
            narration_text=getattr(getattr(prep, "narration", None), "text", "") or "")

    @staticmethod
    def _apply_discovery_gains(session, ctx, plan, gains) -> None:
        """v7.14: 키로 지목된 발견물 수거 → 같은 커밋의 소지품 +1(추출이 같은 이름으로 이미 +를 냈으면 중복 없음)."""
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        if not pc_name:
            return
        for item in gains or ():
            if any(e.get("target") == pc_name and core.turn_world.same_target(e.get("item"), item)
                   and int(e.get("delta") or 0) > 0 for e in plan.item_deltas):
                continue
            plan.item_deltas.append({"target": pc_name, "item": item, "delta": 1})
            plan.entries.append({"domain": "item", "target": pc_name, "op": "delta",
                                 "payload": {"item": item, "delta": 1}})

    def _stage_world_turn(self, session, prep):
        """READY 직전 — 관찰 결과 검증 후 최종 연산을 PLAYER_TURN 묶음에 담는다. 실패 코드 또는 None."""
        wt = getattr(prep, "world_turn", None)
        if wt is None:
            return None
        FC = core.turn_transaction.FailureCode
        res = wt.result
        if res is None:
            return FC.COMMIT_VALIDATION_FAILURE.value
        if res.status == core.turn_world.TW_CONFLICT:
            self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                                   {"stage": "post_narration", "conflict": res.conflict,
                                    "diagnostics": res.diagnostics})
            return FC.WORLD_PROJECTION_CONFLICT.value
        if res.status != core.turn_world.TW_READY:
            self._world_diagnostic(session, prep, res.status, {"diagnostics": res.diagnostics})
            return FC.COMMIT_VALIDATION_FAILURE.value
        base = core.world_state.version_id_of(prep.baseline_world_version)
        if res.plan is not None and res.plan.source_world_version_id != base:
            return FC.COMMIT_VALIDATION_FAILURE.value
        base_rev = getattr(prep, "baseline_definition_revision", None)
        if wt.ctx.definition_revision != base_rev or (
                res.plan is not None and res.plan.definition_revision != base_rev):
            # R2: 옛 정의로 만든 계획은 새 revision 준비에 들어가지 않는다
            self._world_diagnostic(session, prep, "DEFINITION_STALE",
                                   {"plan_revision": getattr(res.plan, "definition_revision", None),
                                    "context_revision": wt.ctx.definition_revision, "baseline_revision": base_rev})
            return FC.COMMIT_VALIDATION_FAILURE.value
        # v7.25(F06): 승격된(지속 등장) 비저작 인물을 정본 ActorRegistry에 같은 커밋으로 등록 — 정본 인물과 겹치면 등록 없음
        #   이번 턴에 승격된 인물만(턴마다 옛 세션 NPC를 소급 등록하지 않는다 — 변이 없는 턴은 변이 없이).
        _promoted = list(getattr(prep, "promotions", {}) or {})
        _reg_ops = core.turn_world.session_npc_register_ops(
            wt.ctx, res.projected_world, _promoted,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            prefix=core.turn_world.turn_op_prefix("snpc", prep.transaction_id),
            barrier=[o.op_id for o in res.ops], present=_promoted)
        try:
            core.turn_preparation.stage_world_ops(prep, tuple(res.ops) + tuple(_reg_ops))
        except core.turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"world_stage:{e}")
            return FC.COMMIT_VALIDATION_FAILURE.value
        return None

    # ─────────────────────────────────────────────────────────────
    # WP-C — 동시 준비 / READY_TO_COMMIT 배리어 / legacy continuation
    # ─────────────────────────────────────────────────────────────

    async def _release_turn_processing(self, session, prep) -> None:
        """자동 턴이 보유한 처리 잠금·채팅 잠금을 배리어/continuation 이후 해제한다."""
        if prep is None or not getattr(prep, "processing_held", False):
            return
        prep.processing_held = False
        game_cog = None
        try:
            game_cog = self.bot.get_cog("GameCog")
        except Exception:
            game_cog = None
        if game_cog is not None and hasattr(game_cog, "release_turn_processing"):
            await game_cog.release_turn_processing(session)
        else:
            session.is_processing = False

    async def _launch_concurrent_preparation(self, session, prep, narr, master_ch) -> None:
        """확정 묘사가 고정된 직후 — 동시 준비 작업을 등록·발사한다(S1).

        같은 확정 묘사 페이로드가 전달·추출·비정규 NPC 분석에 들어간다(재생성 없음).
          · 비정규 NPC 배정: 스트리밍 전에 전달이 결과(스테이징)만 기다린다. 승격 세부
            생성은 자식 작업으로 등록된다(부모 종결 전 등록).
          · 추출: 비정규 NPC 준비(자식 포함)를 기다린 뒤 투영 입력으로 시작 — 스트리밍과 겹친다.
          · 자동 재계획: 트리거되면 등록 작업으로 스트리밍·추출과 겹친다.
        """
        core.turn_transaction.mark_transaction_status(
            session, prep.transaction_id,
            core.turn_transaction.TurnStatus.STREAMING_EXTRACTING)
        _mch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        TP = core.turn_preparation
        if narr.paragraphs:
            prep.register_task(
                "irregular_npc", TP.TASK_IRREGULAR_NPC,
                coro=self._resolve_irregular_npcs(
                    session, narr.narrative_text, _mch, preparation=prep),
                may_spawn_required_child_work=True)
        prep.extraction_text = narr.text
        prep.register_task(
            "extraction", TP.TASK_EXTRACTION,
            coro=self._prepare_extraction(session, prep, narr.text, _mch),
            required_success=True, may_spawn_required_child_work=True)
        if prep.event_assessment is not None:
            await self._update_narrative_progress(
                session, prep.event_assessment, master_ch, preparation=prep)

    async def _prepare_extraction(self, session, prep, text, master_ch):
        """추출 준비 작업(result-only). 비정규 NPC 준비(승격 자식 포함) 완료 후 시작한다.

        기존 흐름에서 추출은 비정규 등록·승격 이후에 돌았으므로, 같은 입력 의미(등록/승격
        NPC 목록·지시효과 quest 투영)를 투영 뷰로 보존한다. 계획은 준비 객체에만 저장된다.
        """
        TP = core.turn_preparation
        irr = prep.task_future("irregular_npc")
        if irr is not None:
            await asyncio.wait([irr])
        while True:
            kids = [r.task for r in prep.tasks.values()
                    if r.kind == TP.TASK_NPC_PROMOTION and r.task is not None and not r.terminal]
            if not kids:
                break
            await asyncio.wait(kids)
        plan = await self._run_extraction(
            session, text, master_ch,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if plan is None:
            raise ExtractionProviderFailure("추출층위 실패(재시도 소진) 또는 stale")
        # H05-B/C: 관찰 → 보충 typed 연산·세계 진행(순수) → 필요 시 후속 묘사 작업 등록(추출 종결 전).
        if getattr(prep, "world_turn", None) is not None:
            # v7.29: 물건 연산이 정본 검증에서 거부되면 ① 사유를 덧붙여 같은 턴 1회 재추출(B1)
            #   ② 또 거부되면 물건 연산만 빼고 확정(B2). 같은 응답을 같은 검증에 다시 넣는 막다른 재시도를 없앤다.
            first = self._observe_or_item_rejection(session, prep, plan, master_ch)
            if first is not None:
                self._world_diagnostic(session, prep, "ITEM_CONTRACT_REJECTED", {"stage": "first", "error": first})
                plan = await self._run_extraction(
                    session, text, master_ch, transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
                    attempt=prep.attempt, preparation=prep, correction=first)
                if plan is None:
                    raise ExtractionProviderFailure("물건 연산 교정 재추출 실패(재시도 소진) 또는 stale")
                second = self._observe_or_item_rejection(session, prep, plan, master_ch)
                if second is not None:
                    await self._drop_item_ops_and_observe(session, prep, plan, master_ch, first, second)
        # B-C3: 이번 턴 계획의 quest_progress로 수치 재계획을 판정 — 자식 작업 등록은
        #   이 추출 작업이 terminal이 되기 전에 끝나므로 배리어 합류가 누락하지 않는다.
        await self._evaluate_numeric_replan(session, prep, plan, master_ch)
        return plan

    def _observe_or_item_rejection(self, session, prep, plan, master_ch) -> str | None:
        """관찰 처리 — 물건 연산이 정본 검증에서 거부되면 그 사유(그 밖의 예외는 그대로 전파)."""
        try:
            self._process_world_observation(session, prep, plan, master_ch)
            return None
        except core.items.ItemContractError as e:
            return str(e)

    async def _drop_item_ops_and_observe(self, session, prep, plan, master_ch, first, second) -> None:
        """v7.29 B2 — 물건 연산만 제외(발견물 수거 키·옛 소지 변화 포함)하고 관찰을 마친다. 부분 적용은 없다."""
        raw = dict(prep.world_observation_raw or {})
        raw.pop(core.items.OPS_FIELD, None)
        raw.pop("discovery_taken", None)
        prep.world_observation_raw = raw
        plan.item_deltas = []
        self._world_diagnostic(session, prep, "ITEM_OPS_DROPPED", {"first": first, "second": second})
        if master_ch is not None:
            try:
                await master_ch.send(f"⚠️ [물건] 추출의 물건 연산이 두 번 거부되어 이번 턴은 물건 변화 없이 확정합니다.\n"
                                     f"> 1차: {first}\n> 2차: {second}")
            except Exception:
                pass
        self._process_world_observation(session, prep, plan, master_ch)

    async def _evaluate_numeric_replan(self, session, prep, plan, master_ch) -> str | None:
        """WP-C(B-C3) — 이번 트랜잭션 추출 계획의 수치로 자동 재계획을 트리거한다.

        · 권위: plan.quest_progress(advance/deviation). 정본 last_extraction(직전 턴)은 보지 않는다.
        · precedence/dedupe: event_assessment가 completed/deviated면 이미 사건 트리거가
          등록됐으므로 생략(턴당 1건). event_assessment=None(강제 PROCEED)이면 기존과 같이
          재계획 판정 자체를 하지 않는다. narrative_replan이 이미 등록돼 있어도 생략.
        · 결과 후보는 스테이징되고 READY 이후에만 적용된다.
        """
        ea = prep.event_assessment
        if ea is None or ea in ("completed", "deviated"):
            return None
        if "narrative_replan" in prep.tasks:
            return None
        if not getattr(session, "narrative_plan", None):
            return None
        qp = getattr(plan, "quest_progress", None) or {}
        try:
            advance = int(qp.get("advance", 0))
            deviation = int(qp.get("deviation", 0))
        except (TypeError, ValueError, AttributeError):
            return None
        th = core.get_thresholds(session)
        if deviation >= th["quest_deviated"]:
            reason, full, note = "deviated", True, f"추출 이탈 수치 {deviation}"
            msg = (f"📖 **[서사 계획]** 이탈 수치 {deviation} (임계 {th['quest_deviated']}) "
                   f"→ 중규모 계획 포함 전체 재수립합니다.")
        elif advance >= th["quest_advance"]:
            reason, full, note = "completed", False, f"추출 진행 수치 {advance}"
            msg = (f"📖 **[서사 계획]** 진행 수치 {advance} (임계 {th['quest_advance']}) "
                   f"→ 순간 계획을 재수립합니다.")
        else:
            return None
        prep.narrative_marker = True            # last_planned_turn 마커(스테이징)
        prep.register_task(
            "narrative_replan", core.turn_preparation.TASK_NARRATIVE_REPLAN,
            coro=self._prepare_auto_replan(
                session, prep, reason, full_replan=full, context_note=note),
            parent="extraction")
        if master_ch:
            await master_ch.send(msg)
        return reason

    async def _join_after_exception(self, session, prep) -> str:
        try:
            await prep.join()
        except Exception:
            pass
        prep.seal()
        try:
            prep.close_cost_membership()
        except Exception as e:
            prep.violations.append(f"cost_close:{e}")
        if not prep.matches(core.turn_transaction.get_active_transaction(session)):
            return _OUTCOME_SUPERSEDED
        return _OUTCOME_NOT_READY if prep.narration is not None else _OUTCOME_NO_NARRATION

    async def _join_and_ready(self, session, prep) -> str:
        """모든 준비 작업을 합류시키고 배리어 결과를 판정한다(S6/S7)."""
        TP = core.turn_preparation
        await prep.join()
        prep.seal()
        if not prep.matches(core.turn_transaction.get_active_transaction(session)):
            # 더 새로운 시도가 현재 — 결과는 어떤 배리어도 만족하지 못하며 흐름을 소유하지 않는다.
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
            return _OUTCOME_SUPERSEDED
        if prep.narration is None:
            outcome = _OUTCOME_NO_NARRATION
        elif prep.delivery is None or not getattr(prep.delivery, "ok", False):
            outcome = _OUTCOME_DELIVERY_FAILED
        else:
            ext = prep.tasks.get("extraction")
            if (ext is None or ext.state != TP.TASK_SUCCEEDED
                    or prep.extraction_plan is None
                    or getattr(prep.extraction_plan, "rejected_stale", False)):
                outcome = _OUTCOME_EXTRACTION_FAILED
            else:
                try:
                    prep.close_cost_membership()
                except TP.BarrierViolationError as e:
                    prep.violations.append(f"cost_close:{e}")
                _wf = self._stage_world_turn(session, prep)
                if _wf is not None:
                    prep.failure_code, prep.failure_stage = _wf, "WORLD"
                    return _OUTCOME_NOT_READY
                proof, reasons = TP.transition_to_ready(session, prep)
                if proof is not None:
                    print(f"[WP-C/{prep.transaction_id[:8]}] READY_TO_COMMIT "
                          f"logical={prep.logical_turn} attempt={prep.attempt} "
                          f"cost_events={len(proof.frozen_cost_event_ids)} "
                          f"canonical_unchanged={proof.canonical_unchanged}")
                    return _OUTCOME_READY
                core.logs.event("turn", "READY 불가", level="WARN", session=session,
                                data={"code": "READY_REJECTED", "tx": prep.transaction_id, "reasons": list(reasons or ())})
                prep.failure_stage = "READY_PREDICATE"
                outcome = _OUTCOME_NOT_READY
        if outcome != _OUTCOME_EXTRACTION_FAILED:
            # 종료 실패 — 비용 멤버십을 동결해 둔다(FAILED_SYSTEM은 청구 0, 사실은 보존).
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
        return outcome

    async def _handle_preparation_failure(self, session, prep, outcome, master_ch) -> bool:
        """사전 READY 실패 처리 — 정본 전진·성공 턴 청구 없음(S10).

        Returns: 다음 라운드를 열어도 되는 안전한 종결 처분이면 True(재선언 UX).
        재시도 대기·대체된 시도는 False. 호출자는 commit_recovery(차단)도 함께 본다.
        """
        TP = core.turn_preparation
        TT = core.turn_transaction
        tid = prep.transaction_id
        if outcome == _OUTCOME_SUPERSEDED:
            # 정본·세션 차단 상태를 건드리지 않는다(현재 시도가 흐름을 소유).
            prep.phase = TP.PREP_FAILED
            prep.failure_stage = "SUPERSEDED"
            print(f"[WP-C/{tid[:8]}] 대체된 시도의 준비 결과 폐기")
            return False
        if outcome == _OUTCOME_EXTRACTION_FAILED:
            # 재시도 가능 상태 — 같은 논리 시도를 유지한다(새 자동 턴을 열지 않음).
            prep.phase = TP.PREP_RETRY_PENDING
            prep.failure_stage = "EXTRACTION"
            # v7.29: 호출 실패와 응답 처리 실패를 나눈다 — 실제 예외를 구조 로그에 남긴다
            _ext = prep.tasks.get("extraction")
            _err = getattr(_ext, "error", None) or ""
            _code = (TT.FailureCode.EXTRACTION_PROVIDER_FAILURE
                     if not _err or _err.startswith(ExtractionProviderFailure.__name__)
                     else TT.FailureCode.EXTRACTION_INVALID_RESULT)
            core.logs.event("turn", "추출 작업 실패", level="ERROR", session=session,
                            data={"code": _code.value, "tx": tid, "error": _err})
            TT.mark_transaction_status(
                session, tid, TT.TurnStatus.STREAMING_EXTRACTING,
                failure_stage="EXTRACTION",
                failure_code=_code)
            # WP-D: 재시작 시 이 시도를 exact 멤버십으로 종결할 수 있도록 정확한 클레임
            #   CostEvent ID를 복구 입력으로 strict 영속한다(최종 Settlement 아님).
            if not await core.commit_coordinator.persist_retry_breadcrumb(
                    self.bot, session, prep):
                # D-D3 fail-closed: exact 멤버십 내구성을 보장할 수 없으므로 재시도 상태를
                #   열지 않는다. 멤버십을 동결해 이 시도를 FAILED_SYSTEM(청구 0)으로 종결한다.
                try:
                    prep.close_cost_membership()
                except TP.BarrierViolationError as e:
                    prep.violations.append(f"cost_close:{e}")
                prep.phase = TP.PREP_FAILED
                prep.failure_stage = "EXTRACTION_BREADCRUMB"
                TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                            failure_stage="EXTRACTION_BREADCRUMB",
                            failure_code=TT.FailureCode.PERSISTENCE_FAILURE)
                game_ch = self.bot.get_channel(session.game_ch_id)
                if game_ch:
                    await core.message_lifecycle.send_transient(
                        self.bot, session, game_ch, core.build_failed_turn_notice(""),
                        key=core.message_lifecycle.KEY_TURN_NOTICE)
                if master_ch:
                    await master_ch.send(
                        "⚠️ 추출 실패 + 재시도 복구 기록 저장 실패 — 재시도 없이 턴을 취소합니다(청구 0).")
                session.current_turn_logs = []
                session.gm_side_note = ""
                await self._settle_failed_turn(session, prep, master_ch)
                return True
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await game_ch.send(
                    "⚠️ " + core.master.say("turn.extraction_retry", "턴 정보 정리 중 문제가 발생했습니다.\n"
                                            "아래 버튼으로 다시 시도해 주십시오.")
                    + " 완료 전까지 다음 턴은 진행되지 않습니다.",
                    view=ExtractionRetryView(self.bot),
                )
            if master_ch:
                await master_ch.send(
                    "⚠️ 추출층위 실패 — 턴 미확정(READY 미도달), 다음 턴 차단. 재시도 버튼 배치.")
            return False

        prep.phase = TP.PREP_FAILED
        if outcome == _OUTCOME_DELIVERY_FAILED:
            prep.failure_stage = "DELIVERY"
            await self._cleanup_failed_delivery(session, prep)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await core.message_lifecycle.send_transient(
                    self.bot, session, game_ch, core.build_failed_turn_notice(""),
                    key=core.message_lifecycle.KEY_TURN_NOTICE)
            if master_ch:
                await master_ch.send("⚠️ 묘사 전달 실패 — 턴을 취소하고 선언 질문으로 되돌립니다.")
            session.current_turn_logs = []
            session.gm_side_note = ""
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="DELIVERY",
                        failure_code=TT.FailureCode.MESSAGE_DELIVERY_FAILURE)
        elif outcome == _OUTCOME_NO_NARRATION:
            prep.failure_stage = "NARRATION"
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="NARRATION",
                        failure_code=TT.FailureCode.NARRATION_PROVIDER_FAILURE)
        else:
            if master_ch:
                await master_ch.send("⚠️ 턴 준비 검증 실패 — 턴을 확정하지 않습니다.")
            # H02: READY 술어가 구체 원인을 남겼으면(예: WORLD_PROJECTION_CONFLICT — 결정적 세계
            #   충돌, 재시도 대상 아님) 그 코드를 트랜잭션에 보존한다. 없으면 기존 분류.
            try:
                code = TT.FailureCode(getattr(prep, "failure_code", None))
            except ValueError:
                code = TT.FailureCode.COMMIT_VALIDATION_FAILURE
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="READY_PREDICATE", failure_code=code)
        # WP-D(§10): 종료 실패 — 동결된 exact 멤버십으로 FAILED_SYSTEM Settlement(청구 0).
        #   영속 실패 시 fail-closed(D-D2): 백로그 + commit_recovery 차단 → 라운드 미개방.
        await self._settle_failed_turn(session, prep, master_ch)
        await core.save_session_data(self.bot, session)
        return True

    async def _cleanup_failed_delivery(self, session, prep) -> None:
        """부분 전달 실패 — 이미 생성된 bot 출력을 멱등 정리한다(ID는 tx에 보존)."""
        err = prep.delivery_error
        ids = list(getattr(err, "canonical_message_ids", ()) or ()) + \
            list(getattr(err, "media_message_ids", ()) or ())
        game_ch = self.bot.get_channel(session.game_ch_id)
        if not ids or game_ch is None:
            return
        msgs = []
        for mid in ids:
            if mid is None:
                continue
            try:
                msgs.append(await game_ch.fetch_message(mid))
            except Exception:
                continue
        await core.clear_messages(msgs)

    # ─────────────────────────────────────────────────────────────
    # WP-D — 권위적 커밋(CommitCoordinator) 연결 · 파생 출력 · 실패/복구 UX
    # ─────────────────────────────────────────────────────────────

    async def _commit_ready_turn(self, session, prep, *, state_before=None):
        """READY_TO_COMMIT 시도를 단일 커밋 owner(CommitCoordinator)로 넘긴다."""
        coord = core.commit_coordinator.CommitCoordinator(self.bot)
        try:
            return await coord.commit_ready_turn(
                session, prep, apply_effects=self._apply_commit_effects,
                state_before=state_before)
        except Exception as e:
            # 코디네이터 자체의 예기치 못한 예외 — 정본 영속 여부를 저널로만 판정한다.
            print(f"[WP-D/{prep.transaction_id[:8]}] 커밋 owner 예외: {type(e).__name__}: {e}")
            session.commit_recovery = {"transaction_id": prep.transaction_id,
                                       "status": "PENDING", "stage": "COORDINATOR_EXCEPTION",
                                       "error": f"{type(e).__name__}: {e}"}
            return core.commit_coordinator.CommitResult(
                core.commit_coordinator.CommitOutcome.RECOVERY_PENDING,
                reason="COORDINATOR_EXCEPTION", error=str(e))

    async def _apply_commit_effects(self, session, prep, derived) -> None:
        """커밋 영속 전 도메인 적용 — 정본 메모리 변이만(Discord/통계/세션 저장 없음).

        적용 순서(WP-C 순서 보존): 비정규 NPC 등록·승격 → 진행 이력 → progress →
        지시효과 → 재계획 마커 → 추출 → 자동 재계획. 묘사 로그·ROLL 성장·카운터·
        Settlement 미러·되감기 델타·커밋 표식은 CommitCoordinator가 적용한다.
        파생 알림은 derived에만 쌓여 durable COMMITTED 이후 방출된다.
        """
        # WP-D(D-D1): 이 경로의 정본 변이 예외는 삼키지 않는다 — CommitCoordinator까지
        #   전파되어 롤백 스냅샷이 전체 precommit 상태를 복원한다(영속 전 실패). 파생 출력
        #   (Discord/통계)은 derived 기술자일 뿐이며 COMMITTED 이후에만 방출·실패한다.
        TP = core.turn_preparation
        # WP-F: 커밋된 ROLL 성장의 확정 알림(CANONICAL_GAME_EVENT) — 성장은 코디네이터가
        #   이미 정본에 적용했다. derived 로만 쌓여 durable COMMITTED 이후 송출 의도와 함께 나간다.
        # P1(#1): 본문 전달 대기 — 관찰 처리 결과(이번 본문이 전하지 못한 신호 · 지난 대기 중 미전달분)로 교체.
        #   세계 연산과 같은 커밋 단위(롤백 스냅샷·strict 저장 안). 동적 세계 턴만 쓴다.
        _wt = getattr(prep, "world_turn", None)
        _res = getattr(_wt, "result", None) if _wt is not None else None
        if _res is not None and _res.status == core.turn_world.TW_READY:
            session.world_pending_signals = [dict(r) for r in _res.pending]
        if derived is not None:
            for _line in list(getattr(prep, "growth_notices", None) or []):
                derived.game(_line)
        if prep.irregular_plan is not None and not prep.irregular_plan.applied:
            await self._apply_irregular_npc_plan(
                session, prep.irregular_plan, prep.irregular_text, None,
                staged_promotions=prep.promotions, derived=derived)

        if prep.proceed_history_entry is not None:
            if not hasattr(session, "gm_proceed_history"):
                session.gm_proceed_history = []
            entry = dict(prep.proceed_history_entry)
            entry["turn_num"] = session.turn_count
            session.gm_proceed_history.append(entry)
            if len(session.gm_proceed_history) > 5:
                session.gm_proceed_history = session.gm_proceed_history[-5:]

        if prep.narrative_progress:
            TP.apply_narrative_progress(session, prep.narrative_progress)

        _applied = TP.apply_instruction_effects(session, TP.pending_for(session))
        if (_applied["applied"]
                and _applied["quest_action"] in ("start", "switch")
                and _applied["quest_active_name"]):
            verb = "전환" if _applied["quest_action"] == "switch" else "선정"
            derived.master(
                f"📜 **[퀘스트 {verb}]** {_applied['quest_active_name']}\n"
                f"> {_applied['quest_reason']}")

        if prep.narrative_marker:
            _plan = getattr(session, "narrative_plan", None)
            if isinstance(_plan, dict) and _plan:
                _plan["last_planned_turn"] = session.turn_count
                session.narrative_plan = _plan

        await self._apply_prepared_extraction(session, prep, derived=derived)

        if prep.replan_candidate is not None:
            await self._commit_replan_candidate(
                session, prep.replan_candidate,
                logical_turn=prep.logical_turn, attempt=prep.attempt,
                derived=derived)

    async def _emit_commit_derived(self, session, derived) -> None:
        """durable COMMITTED 이후 파생 출력 방출. 실패는 커밋을 되돌리지 않는다(§7 Phase F)."""
        if derived is None:
            return
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        game_ch = self.bot.get_channel(getattr(session, "game_ch_id", 0))
        game_ids = []
        # WP-E(E-E2a): 게임 채널 파생 출력은 durable 송출 의도(인덱스 BEGIN 또는 대체 파일)가
        #   확보된 뒤에만 보낸다. 확보 불가면 보내지 않는다(fail-closed) — 추적 불가 출력 금지.
        #   이미 COMMITTED된 이야기·재무는 되돌리지 않는다.
        emit_id = None
        _tid = getattr(derived, "transaction_id", None)
        n_game = sum(1 for it in derived.items if it[0] == "game")
        game_allowed = False
        if n_game and game_ch:
            if _tid:
                try:
                    emit_id, _ = core.turn_history.secure_emit_intent(session, _tid)
                    game_allowed = True
                except Exception as e:
                    print(f"⛔ [WP-E] 송출 의도 영속 불가 — 게임 채널 파생 출력 {n_game}건 보류: {e}")
            else:
                print(f"⛔ [WP-E] 시도 식별 없는 게임 채널 파생 출력 {n_game}건 보류(추적 불가)")
            if not game_allowed and master_ch:
                try:
                    await master_ch.send(
                        f"⚠️ 이력 기록을 확보하지 못해 게임 채널 파생 알림 {n_game}건을 보내지 "
                        f"않았습니다(턴 확정·청구는 유지).")
                except Exception:
                    pass
        for item in list(derived.items):
            try:
                kind = item[0]
                if kind == "master" and master_ch:
                    _, content, embed = item
                    if embed is not None:
                        await master_ch.send(content, embed=embed)
                    elif content:
                        await master_ch.send(content)
                elif kind == "game" and game_ch and game_allowed:
                    _, content, view_factory = item
                    if view_factory is not None:
                        _m = await game_ch.send(content, view=view_factory())
                    else:
                        _m = await game_ch.send(content)
                    if getattr(_m, "id", None) is not None:
                        game_ids.append(_m.id)
                elif kind == "stats":
                    await core.stats.bump(item[1], **item[2])
                elif kind == "npcs":
                    await core.stats.add_npcs(item[1], item[2])
            except Exception as e:
                print(f"[WP-D] 파생 출력 실패(커밋 유지): {type(e).__name__}: {e}")
        # WP-E(E-E2): 정본 게임 이벤트 메시지를 커밋 시도에 귀속(되감기·재생성 정리 대상).
        #   인덱스 실패 → 대체 파일 → 런타임 차단 순으로 보전하며 조용히 유실하지 않는다.
        if _tid and (game_ids or emit_id):
            where = core.turn_history.record_emitted_messages(session, _tid, emit_id, game_ids)
            if where != "INDEX":
                print(f"⛔ [WP-E] 파생 메시지 매핑 보류({where}) — 재정합 전 되감기·재생성 차단")

    async def _send_turn_cost_report(self, session, settlement, prep=None) -> None:
        """Settlement 파생 턴 비용 보고(마스터 전용). 재환산·재반올림 없음(AUD-022).
        v7.30: 매 턴 전수보고 — 비용(주황)·층위활동(파랑)·세계 델타(초록), 한도 초과는 분리(core.master_report)."""
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        log = list(getattr(session, "turn_cost_log", None) or [])
        session.turn_cost_log = []
        if settlement is not None:            # v7.30: 예측·실제 대조 자동 수집(예측 되먹임 원천, 청구와 무관)
            try:
                _by = core.estimate.actual_by_line(log)
                core.estimate.record_accuracy(session, getattr(session, "last_estimate", None), sum(_by.values()),
                                              _by, transaction_id=getattr(settlement, "transaction_id", None))
            except Exception as e:  # noqa: BLE001
                print(f"[EST] 정확도 수집 실패(무시): {e}")
        if not master_ch or settlement is None:
            return
        try:
            embed = core.build_turn_cost_embed(
                session.turn_count, log, session.total_cost,
                total_ink=int(getattr(session, "total_ink_spent", 0) or 0),
                total_usd=float(getattr(session, "total_usd", 0.0) or 0.0),
                free_krw=float(getattr(session, "profile_ai_cost_krw", 0.0) or 0.0),
                settlement=settlement,
                estimate=getattr(session, "last_estimate", None) or None)     # P1: 예상 대비(운영자 점검)
            await core.master_report.send(
                master_ch, session, transaction_id=getattr(settlement, "transaction_id", None), cost_embed=embed,
                cost_log=log, estimate=getattr(session, "last_estimate", None) or None, prep=prep)
        except Exception as e:
            print(f"[WP-D] 턴 비용 보고 실패(커밋 유지): {e}")

    async def _settle_failed_turn(self, session, prep, master_ch) -> None:
        """시스템 실패 시도 → FAILED_SYSTEM Settlement(청구 0). 기록 실패는 명시적으로 알린다."""
        s, err = await core.commit_coordinator.settle_failed_attempt(self.bot, session, prep)
        session.turn_cost_log = []
        if not master_ch:
            return
        try:
            if s is not None:
                await master_ch.send(
                    f"🧾 실패 턴 정산 — 플레이어 청구 **0잉크** "
                    f"(발생 비용 {core.format_cost(s.provider_cost_krw)}은 운영 기록으로 보존)")
            else:
                await master_ch.send(
                    f"⛔ 실패 턴 정산 기록 실패 — 청구 0, 복구 입력 보존·새 턴 차단. "
                    f"다음 입력 시 재시도합니다: {err}")
        except Exception:
            pass

    async def _after_commit(self, session, prep, result, master_ch) -> bool:
        """커밋 결과 처분. True면 호출자가 다음 라운드를 연다(성공 또는 안전한 실패)."""
        CC = core.commit_coordinator
        outcome = getattr(result, "outcome", None)
        if outcome == CC.CommitOutcome.COMMITTED:
            # 비권위 저장 — 정본은 이미 strict 저장됨. 커밋 이후 파생 필드(되감기 공백 표식 등)만.
            await core.save_session_data(self.bot, session)
            await self._emit_commit_derived(session, result.derived)
            # WP-E(E-E2): 재생성 교체의 durable 정리 부채(SELECT와 같은 라인)를 실행한다 —
            #   선택 전환이 COMMITTED 이후에만 일어나므로 이전 출력 삭제도 그 이후뿐이다.
            await self._drain_history_cleanup(session)
            await self._send_turn_cost_report(session, result.settlement, prep)
            try:
                await core.refresh_display(self.bot, session, reason="turn_end")
            except Exception as e:
                print(f"[디스플레이] 턴 종료 갱신 실패: {e}")
            await self._conclude_if_ending(session, master_ch)
            return True
        if outcome == CC.CommitOutcome.FAILED_PRE_PERSIST:
            # 정본 미영속 — 전달된 묘사는 정본이 아니므로 정리하고 재선언을 안내한다.
            await self._cleanup_uncommitted_output(session, prep)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                try:
                    await core.message_lifecycle.send_transient(
                        self.bot, session, game_ch, core.build_failed_turn_notice(""),
                        key=core.message_lifecycle.KEY_TURN_NOTICE)
                except Exception:
                    pass
            session.current_turn_logs = []
            session.gm_side_note = ""
            if master_ch:
                try:
                    await master_ch.send(
                        f"⚠️ 턴 확정 실패(영속 전: {result.reason}) — 정본 미반영·청구 0잉크. "
                        f"선언 질문으로 되돌립니다.")
                    if result.audit_error:
                        await master_ch.send(
                            f"⚠️ 실패 턴 정산 기록 실패 — 새 턴 차단 후 재시도합니다: {result.audit_error}")
                except Exception:
                    pass
            session.turn_cost_log = []
            await core.save_session_data(self.bot, session)
            return not getattr(session, "commit_recovery", None)
        if outcome == CC.CommitOutcome.RECOVERY_PENDING:
            if master_ch:
                try:
                    await master_ch.send(
                        f"⏸️ 턴 이야기는 확정·저장되었으나 정산/확정 단계가 끝나지 않았습니다"
                        f"({result.reason}). 복구 완료 전까지 다음 턴을 열지 않습니다.")
                except Exception:
                    pass
            return False
        return False     # REJECTED — 이 호출은 흐름을 소유하지 않는다

    async def _conclude_if_ending(self, session, master_ch) -> bool:
        """v7.44(E3): 커밋된 정본이 저작 결말 국면에 닿았으면 — 결말 카드 → 기존 종료 경로(정산·환급) → 채널 잠금·기록.

        자동 GM을 끈다(다음 라운드 없음). 종료가 지금 막히면(진행 중 이력 작업 등) 보고만 하고 다음 커밋에서 다시 본다."""
        end = core.turn_world.session_ending(session)
        if not end:
            return False
        session.gm_active = False
        res = await core.world_lifecycle.close_session(self.bot, session, reason=core.cache_lifecycle.REASON_ENDING)
        if not res.get("ok"):
            session.gm_active = True
            if master_ch:
                try:
                    await master_ch.send(f"⏸️ 결말({end['id']})에 닿았으나 세션을 아직 닫지 못했습니다: {res.get('reason')}")
                except Exception:
                    pass
            return False
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            try:
                await game_ch.send(embed=discord.Embed(title=f"🏁 {end['title']}",
                                                       description="이야기가 끝났습니다. 채널을 잠급니다.",
                                                       color=0x2b2d31))
                await game_ch.set_permissions(game_ch.guild.default_role, send_messages=False)
            except Exception as e:
                print(f"[결말] 결말 카드·채널 잠금 실패: {e}")
        try:
            started = getattr(session, "started_at", 0.0) or 0.0
            seconds = max(0.0, time.time() - started) if started else 0.0
            for uid in (getattr(session, "players", {}) or {}):
                await core.stats.bump(uid, session_seconds=seconds)
                await core.stats.add_ending(uid, getattr(session, "scenario_id", ""), end["id"])
            if game_ch is not None:
                await core.refresh_boards(self.bot, game_ch.guild)
        except Exception as e:
            print(f"[결말] 통계·보드 갱신 실패: {e}")
        await core.save_session_data(self.bot, session)
        if master_ch:
            try:
                refund = res.get("refund") or {}
                await master_ch.send(f"🏁 결말 `{end['id']}` — {end['title']} · 세션 종료·정산 완료"
                                     + (" · 환급 " + ", ".join(f"<@{u}> {v}잉크" for u, v in refund.items())
                                        if refund else ""))
            except Exception:
                pass
        return True

    async def _cleanup_uncommitted_output(self, session, prep) -> None:
        """커밋되지 않은 시도의 전달 출력 정리(ID는 tx에 보존) — 정본처럼 남지 않게."""
        ids = list(getattr(prep.delivery, "canonical_message_ids", ()) or ()) + \
            list(getattr(prep.delivery, "media_message_ids", ()) or ())
        game_ch = self.bot.get_channel(session.game_ch_id)
        if not ids or game_ch is None:
            return
        msgs = []
        for mid in ids:
            if mid is None:
                continue
            try:
                msgs.append(await game_ch.fetch_message(mid))
            except Exception:
                continue
        try:
            await core.clear_messages(msgs)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────
    # WP-E — 되감기 · 같은 턴 재생성 어댑터(권위는 core.turn_history)
    # ─────────────────────────────────────────────────────────────

    async def _drain_history_cleanup(self, session) -> int:
        """durable 정리 부채(E-E2) 실행 — 멱등, 실패는 부채로 남아 다음 복구·입력에서 재시도.

        부채에는 봇 정본 출력 ID만 있다(플레이어 메시지 없음). 현재 선택 시도의 출력은
        부채에 있어도 삭제하지 않는다. 이야기·재무에는 영향이 없다.
        """
        try:
            res = await core.turn_history.drain_cleanup(self.bot, session)
        except Exception as e:
            print(f"[WP-E] 출력 정리 부채 실행 실패(부채 유지): {e}")
            return 0
        # E-E2b: unmapped 부채는 수동 정리 안내가 실제로 전달된 뒤에만 닫는다(ack).
        #   채널 부재·전송 실패 시 부채가 남아 다음 admission/재시작에서 다시 안내된다.
        manual = res.get("manual") or []
        if manual:
            master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
            sent = False
            if master_ch:
                try:
                    await master_ch.send(
                        "⚠️ 일부 이전 턴 출력은 매핑 기록이 없어 자동 정리하지 못했습니다. "
                        "게임 채널을 확인해 수동으로 정리해 주십시오.")
                    sent = True
                except Exception as e:
                    print(f"[WP-E] 수동 정리 안내 실패(부채 유지·재안내): {e}")
            if sent:
                for m in manual:
                    core.turn_history.ack_manual_cleanup(session, m["op_id"])
                session._history_cleanup_clear = not core.turn_history.pending_cleanups(
                    session.session_id)
        return int(res.get("deleted") or 0)

    async def _settle_history_op(self, session, prep, master_ch) -> None:
        """재생성 교체 시도 종결 후 남은 의도 처분.

        비커밋 종결 → 이전 선택 시도의 커밋 직후 정본으로 복원. 교체가 COMMITTED인데 선택
        기록만 실패했다면 되돌리지 않고 선택을 정합(degraded)한다 — 청구된 이야기가 정본.
        """
        if prep is None:
            return
        op = core.turn_history.rerender_op_for(session.session_id, prep.transaction_id)
        if not op:
            return
        if prep.phase == core.turn_preparation.PREP_RETRY_PENDING:
            return          # 같은 시도로 재시도 가능 — 의도 유지(재시작 시 정합이 처분)
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is not None and tx.transaction_id == prep.transaction_id \
                and not core.turn_transaction.is_terminal(tx.status):
            return
        try:
            res = await core.turn_history.settle_rerender(self.bot, session)
        except Exception as e:
            session.commit_recovery = {"status": "PENDING", "stage": "HISTORY_RERENDER_ABORT",
                                       "error": f"{type(e).__name__}: {e}"}
            print(f"⛔ [WP-E] 재생성 종결 처분 실패 — 차단: {e}")
            return
        if res.get("action") == "ABORTED" and master_ch:
            await master_ch.send("↩️ 재생성 시도가 확정되지 않아 기존 턴을 그대로 유지합니다(청구 0).")
        elif res.get("action") == "SELECTED":
            # 교체가 COMMITTED로 확정(선택은 재정합으로 맞춤) — durable 부채로 이전 출력 정리
            await self._drain_history_cleanup(session)

    async def _abort_rerender(self, session, master_ch) -> None:
        try:
            await core.turn_history.abort_rerender(self.bot, session)
            if master_ch:
                await master_ch.send("↩️ 재생성 시도가 확정되지 않아 기존 턴을 그대로 유지합니다(청구 0).")
        except Exception as e:
            session.commit_recovery = {"status": "PENDING", "stage": "HISTORY_RERENDER_ABORT",
                                       "error": f"{type(e).__name__}: {e}"}
            print(f"⛔ [WP-E] 재생성 중단 복원 실패 — 차단: {e}")

    async def history_rewind(self, session, target: int) -> dict:
        """모든 되감기 입구(!되감기·디스플레이·rewind:one)의 단일 어댑터."""
        async with self._lock_for(session):
            core.turn_history.try_flush(session)        # E-E2 보전 매핑 먼저 인덱스로
            result = await core.turn_history.rewind(self.bot, session, target)
            if not result.get("ok"):
                return result
            # E-E2: REWIND 이벤트에 실린 durable 정리 부채를 실행(실패 시 부채 유지·재시도)
            result["removed_messages"] = await self._drain_history_cleanup(session)
        try:
            await core.refresh_display(self.bot, session, reason="rewind")
        except Exception as e:
            print(f"[디스플레이] 되감기 갱신 실패: {e}")
        return result

    async def rerender_latest(self, session, *, addendum: str = "") -> tuple:
        """같은 논리 턴 attempt+1 재생성 — 선언·판단 보존, 지시층위부터 일반 파이프라인.

        Returns: (started: bool, reason: str)
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        async with self._lock_for(session):
            core.turn_history.try_flush(session)        # E-E2 보전 매핑 먼저 인덱스로
            entry, rec, reason = core.turn_history.rerender_target(session)
            if entry is None:
                return False, reason
            # H06-E: DYNAMIC 턴은 보존된 의미 선택 + 같은 정의 revision에서만 재서술한다(상태 변경 전 검사)
            w_intent, w_reason = core.turn_world.rerender_world_intent(
                session, rec.get("judgment"), (rec.get("post") or {}).get("world_version"))
            if w_reason:
                return False, w_reason
            try:
                tx = await core.turn_history.begin_rerender(
                    self.bot, session, entry, rec, addendum=addendum)
            except core.turn_history.HistoryError as e:
                return False, str(e)
            tid = tx.transaction_id
            j = tx.judgment_result
            try:
                if w_intent is not None:
                    # 턴 이전 세계로 복원된 뒤 새 시도의 세계 계획을 다시 세운다(옛 시도의 WorldTurn 재사용 금지)
                    _ctx = core.player_world.session_world_context(session)
                    if _ctx.dynamic:
                        _wt = core.turn_world.WorldTurn(ctx=_ctx, intent=w_intent)
                        tx.world_turn = _wt
                        self._ensure_world_plan(session, _wt, tid)
                    else:
                        # 세계 연산 없이 커밋하지 않는다 — 교체 시도를 묘사 전에 종결하고 기존 턴 유지
                        await self._fail_rerender_before_narration(session, tid, master_ch)
                        return False, f"턴 이전 세계가 DYNAMIC이 아닙니다({_ctx.reason}) — 기존 턴 유지"
                # v7.12: 같은 판정 결과를 재사용하므로 원 턴 굴림의 성장·실패 누적도 그대로 다시 스테이징한다
                #   (재추첨·이중 적용 없음, 커밋 뒤 성장 알림은 다시 내지 않음).
                core.turn_preparation.restage_roll_effects(session, tid, (j or {}).get("roll_effects"))
                if master_ch:
                    await master_ch.send(
                        f"🔁 **[재생성]** {rec['gm_turn']}턴을 같은 선언·판단으로 다시 서술합니다 "
                        f"(시도 {tx.attempt}). 기존 턴 청구는 유지됩니다.")
                decision = await self._call_gm_logic(
                    session, j["player_message"], list(j.get("roll_results") or []), master_ch,
                    action="PROCEED", transaction_id=tid)
                if not decision:
                    await self._fail_rerender_before_narration(session, tid, master_ch)
                    return False, "지시층위 호출 실패 — 기존 턴 유지"
                decision = {**(j.get("judgment") or {}), **decision, "action": "PROCEED"}
                core.turn_preparation.stage_instruction_effects(
                    session, decision, transaction_id=tid)
                instruction = _clean_proceed_instruction(decision.get("proceed_instruction") or "") \
                    or "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=tid)
                return True, ""
            except Exception as e:  # noqa: BLE001 — v7.33(F1): 묘사 이전 예외는 교체 시도만 종결(기존 턴 유지)
                core.logs.error("turn", "재서술 중 예기치 않은 예외", exc=e, session=session)
                _p = core.turn_preparation.get_preparation(session, tid)
                if _p is None or _p.phase == core.turn_preparation.PREP_COLLECTING:
                    await self._fail_rerender_before_narration(session, tid, master_ch)
                return False, f"예기치 않은 오류({type(e).__name__}) — 기존 턴 유지"

    async def _fail_rerender_before_narration(self, session, tid, master_ch) -> None:
        """묘사 이전 실패 — 교체 시도를 FAILED_SYSTEM(청구 0)으로 종결하고 이전 선택 유지."""
        TP = core.turn_preparation
        prep = TP.get_preparation(session, tid)
        if prep is not None:
            try:
                await prep.join()
                prep.seal()
                prep.close_cost_membership()
            except Exception as e:
                prep.violations.append(f"cost_close:{e}")
            prep.phase = TP.PREP_FAILED
        core.turn_transaction.finalize(
            session, tid, core.turn_transaction.TurnStatus.FAILED_SYSTEM,
            failure_stage="RERENDER_INSTRUCTION",
            failure_code=core.turn_transaction.FailureCode.INSTRUCTION_PROVIDER_FAILURE)
        if prep is not None:
            await self._settle_failed_turn(session, prep, master_ch)
        if core.turn_history.rerender_op_for(session.session_id, tid):
            await self._abort_rerender(session, master_ch)

    def _round_eligible(self, session) -> bool:
        """새 자동 라운드를 열 자격(복구 이후에만 평가) — 활성·턴 한도·비용 한도."""
        if not getattr(session, "gm_active", False):
            return False
        cap = getattr(session, "gm_turn_cap", None)
        if cap is not None and session.gm_turns_done >= cap:
            return False
        if core.auto_cost_cap_reached(self.bot, session):   # WP-G: CostLedger 기준 운영 예산
            return False
        return True

    async def _recover_then_maybe_round(self, session, master_ch) -> bool:
        """복구 우선 admission(D-D4): 복구 성공 후에만 eligibility로 새 라운드 여부를 정한다."""
        if not await self._resume_commit_recovery(session, master_ch):
            return False
        if self._round_eligible(session):
            await self._start_round(session)
        return True

    async def _recovery_admission(self, session) -> None:
        """게임 채널 입력 경로의 복구 admission — gm_active와 무관하게 먼저 실행된다."""
        async with self._lock_for(session):
            if not getattr(session, "is_processing", False):
                # WP-F: 주인 없는 대기 안내(재시작 잔재)·만료된 transient 재시도 정리.
                try:
                    await core.message_lifecycle.sweep(
                        self.bot, session, prefix=core.message_lifecycle.WAITING_PREFIX)
                    await core.message_lifecycle.sweep(self.bot, session, only_expired=True)
                except Exception as e:
                    print(f"[메시지 수명주기] transient 정리 실패(등록부 유지): {e}")
            if not getattr(session, "commit_recovery", None):
                # WP-E(E-E2): 보전된 출력 매핑 재정합 · 남은 출력 정리 부채 재시도
                if core.turn_history.needs_reconcile(session):
                    core.turn_history.try_flush(session)
                if not getattr(session, "_history_cleanup_clear", False):
                    await self._drain_history_cleanup(session)
                return
            master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
            await self._recover_then_maybe_round(session, master_ch)

    async def _resume_commit_recovery(self, session, master_ch) -> bool:
        """차단 중인 커밋 복구를 같은 durable 입력으로 재시도한다. True면 차단 해제."""
        rep = await core.commit_coordinator.recover_session(
            self.bot, session, context="in_process")
        if not rep.blocked:
            # WP-E: D 커밋 복구 이후 선택 이력·이력 조작 의도 정합(모순이면 다시 차단).
            _h = await core.turn_history.reconcile(self.bot, session)
            if not _h["ok"]:
                rep = core.commit_coordinator.RecoveryReport(
                    "RECOVERY_REQUIRED", action=_h["action"], detail=_h["detail"])
            else:
                await self._drain_history_cleanup(session)      # E-E2 남은 정리 부채
        if rep.blocked:
            if master_ch:
                try:
                    await master_ch.send(
                        f"⏸️ 커밋 복구 대기({rep.status}) — 새 턴을 진행하지 않습니다. {rep.detail}")
                except Exception:
                    pass
            return False
        # durable 사실상 미결 시도가 없는데 런타임 tx가 COMMITTING으로 남았다면(영속 전
        #   owner 예외) — 정본은 영속되지 않았으므로 실패로 종결해 새 선언을 받는다.
        _tx = core.turn_transaction.get_active_transaction(session)
        if (_tx is not None
                and _tx.status == core.turn_transaction.TurnStatus.COMMITTING):
            core.turn_transaction.finalize(
                session, _tx.transaction_id, core.turn_transaction.TurnStatus.FAILED_SYSTEM,
                failure_stage="COMMIT_ORPHANED",
                failure_code=core.turn_transaction.FailureCode.COMMIT_VALIDATION_FAILURE)
            _p = getattr(_tx, "preparation", None)
            if _p is not None:
                _p.phase = core.turn_preparation.PREP_FAILED
        if rep.status == "RESOLVED":
            await self._emit_commit_derived(session, rep.derived)
            if rep.action == "FINALIZED":
                await self._send_turn_cost_report(session, rep.settlement)
            try:
                await core.refresh_display(self.bot, session, reason="turn_end")
            except Exception:
                pass
            if master_ch:
                try:
                    await master_ch.send(f"✅ 커밋 복구 완료({rep.action}).")
                except Exception:
                    pass
        return True

    async def _apply_prepared_extraction(self, session, prep, *, derived=None) -> None:
        """READY 이후 추출 계획 호환 적용(멱등). 계획(정규화 typed 필드)만이 권위다."""
        plan = prep.extraction_plan
        if plan is None or plan.applied:
            return
        tid = prep.transaction_id
        _applied_ids = getattr(session, "_extraction_applied_tx", None)
        if _applied_ids is None:
            _applied_ids = session._extraction_applied_tx = []
        if tid in _applied_ids:
            print(f"[추출/{session.session_id}] 이미 적용된 트랜잭션 — 이중 적용 방지 (tx={tid})")
            return
        _applied_ids.append(tid)
        if len(_applied_ids) > 16:
            del _applied_ids[:-16]
        _mch = None if derived is not None else \
            self.bot.get_channel(getattr(session, "master_ch_id", 0))
        # v7.38(D5): DYNAMIC PC 상태 — 규칙(시간·장소·섭취)으로 부여·해제를 확정하고 상태 시계를 같은 커밋에 기록
        core.status_rules.resolve_turn(session, prep)
        await self._apply_extraction_plan(session, plan, _mch, derived=derived)

    async def _retire_legacy_extraction_retry(self, session) -> bool:
        """WP-G(RE-GATE): 준비 모드가 아닌(WP-C 이전) 추출 재시도 컨텍스트의 fail-closed 호환 은퇴.

        provider 추출을 다시 부르지 않고, 추출 계획을 적용하지 않으며, 정본 게임 상태를 바꾸지
        않는다. 차단(extraction_pending)과 구 컨텍스트는 strict 세션 저장이 성공한 뒤에만
        해제된다 — 저장이 실패하면 메모리 상태도 되돌려 성공으로 보고하지 않는다.
        새 트랜잭션·별도 커밋 권위를 만들지 않는다.

        Returns: True = 은퇴 영속 완료, False = 저장 실패(차단·컨텍스트 유지)
        """
        async with self._lock_for(session):
            ok = await core.commit_coordinator.retire_legacy_retry_context(self.bot, session)
        if not ok:
            return False
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        if master_ch:
            try:
                await master_ch.send(
                    "ℹ️ **[추출 재시도]** WP-C 이전 형식의 재시도 요청을 폐기했습니다 — "
                    "현재 커밋 권위로 적용할 수 없어 정본 상태를 소급 변경하지 않았습니다.")
            except Exception:
                pass
        return True

    async def _retry_prepared_extraction(self, session) -> str:
        """추출 재시도 — 같은 논리 시도·같은 준비 owner로 재개한다(새 자동 턴 아님).

        Returns: "ready" | "failed" | "released"
        """
        TP = core.turn_preparation
        ctx = getattr(session, "extraction_retry_ctx", {}) or {}
        master_ch = self.bot.get_channel(session.master_ch_id)
        async with self._lock_for(session):
            prep = TP.get_preparation(session, ctx.get("transaction_id"))
            if prep is None or prep.phase != TP.PREP_RETRY_PENDING:
                # 런타임 준비 상태 소실(재시작 등) — 미확정 묘사에 추출을 적용하지 않는다.
                #   WP-D: 이 시도를 exact 클레임 ID로 FAILED_SYSTEM(청구 0) 종결한 뒤 해제.
                res = await core.commit_coordinator.abandon_retry_pending(self.bot, session)
                if res == "PENDING":
                    return "failed"
                if getattr(session, "extraction_pending", False):
                    session.extraction_pending = False
                    session.extraction_retry_ctx = {}
                    await core.save_session_data(self.bot, session)
                return "released"
            session.is_processing = True
            prep.processing_held = True
            outcome = None
            result = None
            state_before = getattr(session, "_rewind_snapshot", None)
            if state_before is None:
                state_before = core.capture_state(session)
            open_round = False
            try:
                prep.reopen_for_retry()
                # D-D3: 재시도 중 새 provider 오퍼레이션 claim은 호출 전에 내구 기록된다.
                prep.durable_claim = lambda op: core.commit_coordinator.record_retry_claim(
                    session.session_id, prep.transaction_id, prep.attempt,
                    getattr(op, "operation_id", None))
                _mch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
                prep.register_task(
                    "extraction", TP.TASK_EXTRACTION,
                    coro=self._prepare_extraction(session, prep, prep.extraction_text or ctx.get("text", ""), _mch),
                    required_success=True, may_spawn_required_child_work=True)
                outcome = await self._join_and_ready(session, prep)
                if outcome == _OUTCOME_READY:
                    result = await self._commit_ready_turn(
                        session, prep, state_before=state_before)
                else:
                    open_round = await self._handle_preparation_failure(
                        session, prep, outcome, master_ch)
            finally:
                prep.durable_claim = None
                await self._release_turn_processing(session, prep)
            committed = (outcome == _OUTCOME_READY and getattr(result, "outcome", None)
                         == core.commit_coordinator.CommitOutcome.COMMITTED)
            if outcome == _OUTCOME_READY:
                if not await self._after_commit(session, prep, result, master_ch):
                    return "failed"
                open_round = True
            await self._settle_history_op(session, prep, master_ch)
            if (open_round and session.gm_active
                    and not getattr(session, "commit_recovery", None)):
                await self._start_round(session)
            return "ready" if committed else "failed"

    async def _run_gm_logic_loop(self, session, player_message: str, master_ch,
                                 *, transaction_id: str | None = None):
        """
        지시층위 ASK / ROLL / PROCEED 루프.
        PROCEED 완료 후 자동으로 _start_round()를 호출하여 다음 라운드(선제 행동 질문)를 시작.

        NOTE: 이 함수는 락 없이 실행됨. 호출 측에서 이미 락을 잡고 있거나,
              비동기 태스크로 독립 실행되는 경우(버튼 콜백 등)에 사용.
        """
        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        action_labels = {
            "ASK":     "🟡 ASK (명확화 요청)",
            "NARRATE": "💬 NARRATE (경량 GM 응답)",
            "ROLL":    "🎲 ROLL (주사위 판정)",
            "PROCEED": "🟢 PROCEED (턴 진행)",
        }

        roll_results: list[str] = []

        game_ch = self.bot.get_channel(session.game_ch_id)
        # WP-F: 새 선언이 들어왔다 — 이전 턴 실패/차단 안내(transient)를 supersede 로 정리한다.
        try:
            await core.message_lifecycle.clear(self.bot, session,
                                               core.message_lifecycle.KEY_TURN_NOTICE)
        except Exception as e:
            print(f"[메시지 수명주기] 턴 안내 정리 실패(등록부 유지·재시도): {e}")

        # ── 방안 6 → 지시층위 순차 주입 ──
        # 세계관 캐시가 유효하면 먼저 서사 방향성을 시뮬레이션하고, 그 결과(sim_result)를
        # 지시층위 첫 결정에 실제로 주입한다.
        # (과거 gather 병렬 실행은 첫 결정이 sim_result를 보지 못해 시뮬레이션 비용만
        #  낭비되는 구조였다 — 순차 주입으로 교정.)
        sim_result: dict | None = None
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        # WP-E(E-E3): 이력 출처가 선택 이력보다 새로운 캐시는 읽지 않는다.
        do_simulation = bool(cache_name and cache_model == core.DEFAULT_MODEL
                             and core.turn_history.cache_usable(session))

        # ── 세션 오픈 여부 확인 (기획 규정 — 닫혀 있으면 차단) ──
        # 만료된 캐시로 진행하면 API 오류가 난다. 미리 막고 안내한다.
        if not core.is_session_open(session):
            expired = core.is_cache_expired(session)
            note = ("유지 시간이 지나 캐시가 만료되었습니다."
                    if expired else "세션이 닫혀 있습니다.")
            await m_send(f"⚠️ {note} 디스플레이에서 세션을 열어 주십시오.")
            if game_ch:
                # WP-F: TRANSIENT_GAME_STATUS — 다음 턴 시도·세션 열기가 supersede(로그 미기록).
                await core.message_lifecycle.send_transient(
                    self.bot, session, game_ch,
                    f"⏸️ {note}\n> 디스플레이 채널의 **세션 열기**를 눌러 주십시오.",
                    key=core.message_lifecycle.KEY_TURN_NOTICE)
            return

        # ── 잔액 검사 (기획 규정 — 소지금 < 예상 최대금액이면 차단) ──
        try:
            _pre_est = core.estimate_turn(session, "PROCEED")
            for _uid in (session.players or {}):
                try:
                    _bal = core.accounts.strict_balance(_uid)
                except core.accounts.AccountError as _ae:
                    # v7.33(F7): 손상·읽기 실패 계정 — '잉크 부족'으로 오안내하지 않고 턴을 막는다(청구가 어차피 불가)
                    if game_ch:
                        await core.message_lifecycle.send_transient(
                            self.bot, session, game_ch,
                            "⛔ 계정 정보를 읽지 못해 턴을 진행할 수 없습니다. 운영자에게 문의해 주십시오.",
                            key=core.message_lifecycle.KEY_TURN_NOTICE)
                    await m_send(f"⛔ 계정 읽기 실패로 턴 차단 (<@{_uid}>): {type(_ae).__name__}: {_ae}")
                    return
                if _bal < _pre_est["max_ink"]:
                    if game_ch:
                        await core.message_lifecycle.send_transient(
                            self.bot, session, game_ch,
                            f"⛔ **{core.master.say('cache.insufficient_ink', '잉크가 부족합니다.')}**\n"
                            f"> 필요 {_pre_est['max_ink']}잉크 / 보유 {_bal}잉크\n"
                            f"> 잉크 지급은 운영자에게 문의해 주십시오.",
                            key=core.message_lifecycle.KEY_TURN_NOTICE)
                    await m_send(f"⛔ 잔액 부족으로 턴 차단 (보유 {_bal} < 필요 {_pre_est['max_ink']})")
                    return
        except Exception as e:
            print(f"[결제] 잔액 검사 실패(진행 허용): {e}")

        # 이번 턴 예상 비용 — 디스플레이 채널 도입 전까지 마스터 채널에 보고한다.
        session.last_estimate = None     # v7.33(F9): 산출 실패 시 지난 턴 예상이 이번 턴 대조·정확도 기록에 섞이지 않게
        try:
            est = core.estimate_turn(session, "PROCEED")
            # WP-G(D3): 압축 비용은 운영자 부담 유지비다 — 계정 효과 없는 '압축 선결제' 20% 가산·
            #   누적·정산 표시를 은퇴했다. 압축 provider 비용은 CostEvent(운영 사실)로만 남는다.
            session.last_estimate = est
            _tts = core.estimate_tts(session)
            # P1: 예상은 정산이 아니다 — 운영 예측 정확도 점검용 구조 로그(커밋 보고의 '예상 대비'와 같은 값)
            core.logs.domain("cost.estimate", {"tx": transaction_id, "min_ink": est["min_ink"],
                                               "max_ink": est["max_ink"], "min_krw": est["min_krw"],
                                               "max_krw": est["max_krw"], "basis": est.get("basis"),
                                               "confidence": est.get("confidence"),
                                               "breakdown": est.get("breakdown"),
                                               "tts": _tts if _tts.get("enabled") else None}, session=session)
            # v7.35(R1): 예상은 별도 메시지로 보내지 않는다 — 커밋 뒤 턴 비용 보고의 '층위별 예상 대조'가 같은 값을 싣는다
        except Exception as e:
            print(f"[EST] 예상 산출 실패(진행에는 영향 없음): {e}")

        # 플레이어가 보는 게임 채널에 판단 대기 안내 (판단 완료 후 삭제)
        # 층위별 문구 — 4층위 도입으로 대기가 길어져 같은 문구 반복은 멈춘 듯 보인다.
        # WP-F: TRANSIENT_GAME_STATUS — 성공·실패·예외·취소 어느 경로든 finally 에서 멱등 정리.
        status_msg = await core.WaitingStatus.begin(game_ch, "judgment", session=session)
        gate = None
        judgment = None
        # v7.14: 선언 게이트 입력(DYNAMIC — 현재 장소의 유효 발견·찾지 못한 이전 탐색)
        gate_in = self._gate_inputs(session, transaction_id)
        dynamic = gate_in["dynamic"]
        try:
            # v7.23: DYNAMIC은 매 발화 개연성 시뮬레이션(서사 방향)을 부르지 않는다 — LEGACY만 기존 그대로.
            if do_simulation and not dynamic:
                if game_ch:
                    async with game_ch.typing():
                        sim_result = await self._simulate_narrative_directions(
                            session, player_message, master_ch,
                            transaction_id=transaction_id, gate_inputs=gate_in)
                else:
                    sim_result = await self._simulate_narrative_directions(
                        session, player_message, master_ch,
                        transaction_id=transaction_id, gate_inputs=gate_in)
            gate = _parse_gate((sim_result or {}).get("declaration_gate"), player_message,
                               dynamic=gate_in["dynamic"], place=gate_in["place"],
                               place_name=gate_in["place_name"], rows=gate_in["rows"])

            # v7.23: 대기 중인 코드 경로 질문에 번호만 답했으면 그 질문의 선택지 스냅샷으로 바로 고른다(판단 재호출 없음).
            if dynamic:
                judgment = self._route_number_answer(session, transaction_id, player_message)

            # ── [판단층위] 캐시 미사용 — 진행 유형과 ASK 질문·ROLL 명세를 결정 ──
            # v7.14: 게이트 BLOCK·CLARIFY는 판단 호출 없이 중립 안내로 끝난다(굴림 0).
            if judgment is None and not _gate_blocks(gate):
                if game_ch:
                    async with game_ch.typing():
                        judgment = await self._call_judgment(
                            session, player_message, roll_results, master_ch,
                            transaction_id=transaction_id, gate=gate, declaration_draft=dynamic,
                            discovery_rows=gate_in["rows"])
                else:
                    judgment = await self._call_judgment(
                        session, player_message, roll_results, master_ch,
                        transaction_id=transaction_id, gate=gate, declaration_draft=dynamic,
                            discovery_rows=gate_in["rows"])
                # v7.23 DYNAMIC: 판단이 발견 제안·결과 선취를 표시했을 때만 축소 선언 검증(캐시) → 게이트
                if dynamic and judgment:
                    judgment, gate = await self._dynamic_declaration_check(
                        session, player_message, roll_results, judgment, gate_in, master_ch,
                        transaction_id=transaction_id, cache_ok=do_simulation)
        finally:
            await status_msg.done()

        if _gate_blocks(gate):
            await self._hold_declaration(session, transaction_id, player_message, gate,
                                         _gate_notice(gate), master_ch)
            return
        if not judgment:
            # v7.33(F3): 판단층위 실패(재시도 소진) — 마스터 안내는 _call_judgment가 했다. 플레이어에게 재선언 안내
            await self._abort_turn_before_narration(
                session, transaction_id, player_message, master_ch,
                code=core.turn_transaction.FailureCode.JUDGMENT_PROVIDER_FAILURE, stage="JUDGMENT",
                master_text="ℹ️ 판단층위 실패 — 이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다.")
            return
        _rolls = [r for r in (judgment.get("rolls") or []) if isinstance(r, dict)]
        if gate is None and gate_in["dynamic"] and (
                judgment.get("ambient_proposal") is True or any(r.get("discovery") is True for r in _rolls)):
            # P7: 게이트를 확인할 수 없다 — 새 발견 제안은 미처리(실패 아님·부재 단정 없음). 굴림·정보 연산 0.
            await self._hold_declaration(session, transaction_id, player_message, None,
                                         GATE_UNVERIFIABLE_NOTICE, master_ch)
            return
        if not _gate_rolls_discovery(gate):
            for _r in _rolls:
                _r.pop("discovery", None)        # 발견 굴림 표식은 게이트 ROLL에서만 유효(그 밖은 일반 판정)
        # WP-E: 재생성이 판단을 다시 돌리지 않도록 구조화 판단과 지시층위 입력을 tx에 기록한다.
        # H05: 이력에는 transient 후보 키(P1·R2…)가 아니라 의미 기록(PlaceId·경로 서명·접근 토큰)을 남긴다.
        _wt = self._world_turn(session, transaction_id)
        _jnote = judgment
        if _wt is not None and _wt.intent is not None:
            _jnote = dict(judgment)
            # H06-E: 재서술이 같은 정의에서만 같은 의미 선택을 재현하도록 그 턴의 revision을 함께 남긴다
            _jnote[core.action_intent.INTENT_FIELD] = {"semantic": _wt.intent.semantic_record(),
                                                       "definition_revision": _wt.ctx.definition_revision}
        core.turn_history.note_judgment(
            session, transaction_id, judgment=_jnote,
            player_message=player_message, roll_results=list(roll_results), gate=gate)

        # 정합: 이동 선언인데 목적지가 없거나 후보 밖이면 진행(묘사가 임의로 이동시킴)하지 않고 되묻는다.
        if _wt is not None and _wt.intent is not None \
                and (judgment.get("action") or "").upper() in ("PROCEED", "ROLL") \
                and any(f in ("destination", "exit") for f, _c in (_wt.intent.unresolved or ())):
            await self._ask_destination(session, _wt, transaction_id, master_ch)
            await core.save_session_data(self.bot, session)
            return

        # H05-A: PROCEED면 지시층위 전에 코드 소유 세계 계획을 세운다(정본 불변). 의미 있는 경로 차이는
        #   여기서 플레이어에게 묻는다(경로 선택은 판단층위가 아니라 코드 정책).
        if _wt is not None and (judgment.get("action") or "").upper() == "PROCEED":
            _plan = self._ensure_world_plan(session, _wt, transaction_id)
            if _plan.status == core.player_world.PLAN_NEEDS_CHOICE:
                await self._ask_route_choice(session, _plan, transaction_id, master_ch)
                await core.save_session_data(self.bot, session)
                return

        for iteration in range(MAX_ITERATIONS_PER_MESSAGE):
            action = (judgment.get("action") or "ASK").upper()
            reasoning = judgment.get("reasoning", "")

            # ── [지시층위] 캐시 사용 — NARRATE·PROCEED에만 필요 ──
            # ASK와 ROLL은 판단층위가 내용물(bridge_message·rolls)까지 생성하므로
            # 지시층위를 호출하지 않는다 → 캐시 읽기 절감.
            if action in ("NARRATE", "PROCEED"):
                current_sim = sim_result if iteration == 0 else None
                if game_ch:
                    async with game_ch.typing():
                        decision = await self._call_gm_logic(
                            session, player_message, roll_results, master_ch,
                            sim_result=current_sim, action=action,
                            transaction_id=transaction_id)
                else:
                    decision = await self._call_gm_logic(
                        session, player_message, roll_results, master_ch,
                        sim_result=current_sim, action=action,
                        transaction_id=transaction_id)
                if not decision:
                    await m_send("⚠️ 지시층위 호출 실패. 이번 발언을 스킵합니다.")
                    # v7.33(F3): 플레이어에게도 재선언 안내 · 시도는 청구 0 종결
                    await self._abort_turn_before_narration(
                        session, transaction_id, player_message, master_ch,
                        code=core.turn_transaction.FailureCode.INSTRUCTION_PROVIDER_FAILURE, stage="INSTRUCTION",
                        master_text="ℹ️ 지시층위 실패 — 이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다.")
                    return
                # 판단층위 산출물과 병합 — 이후 분기는 기존 구조를 그대로 사용한다.
                decision = {**judgment, **decision, "action": action}

                # WP-B: 지시효과(quest choice/intended_case/info_access)를
                #   canonical 변경 없이 트랜잭션에 스테이징한다(단일 owner 통합).
                #   묘사가 성립한 뒤 _finish_proceed_and_continue가 단일 호환 적용
                #   경계에서만 canonical에 반영한다. 여기서는 아무 상태도 안 바뀐다.
                core.turn_preparation.stage_instruction_effects(
                    session, decision, transaction_id=transaction_id)
            else:
                decision = dict(judgment)

            label = action_labels.get(action, action)
            core.logs.event("turn", f"판단 {action}", session=session,
                            data={"stage": "judgment", "iter": iteration, "action": action, "reasoning": reasoning})
            # v7.35: PROCEED·ROLL 판단은 커밋 뒤 층위활동 보고(🧭)가 싣는다. 턴이 확정되지 않고 응답을 기다리는
            #   ASK·NARRATE만 지금 한 줄로 알린다(그 시점에 다른 보고가 없다).
            if action not in ("PROCEED", "ROLL"):
                await m_send(f"🤖 **[GM 판단 #{iteration + 1}]** {label} — {reasoning[:120]}")

            # ── ASK ──
            if action == "ASK":
                session.gm_clarify_count += 1
                if session.gm_clarify_count > MAX_CLARIFY_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** ASK 한도({MAX_CLARIFY_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, forced_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                if game_ch:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, bridge,
                        words_per_tick=15, tick_interval=1.5
                    )
                    await self._voice_lines(session, [bridge], transaction_id=transaction_id)
                # ASK 브리지를 current_turn_logs에 기록 → 다음 지시층위 호출 시 맥락 유지
                session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
                core.logs.event("turn", "ASK 대기", session=session,
                                data={"stage": "ask", "tx": transaction_id, "bridge": bridge})
                # WP-01: 플레이어 입력 대기(비종료). 재입력 시 같은 트랜잭션을 재사용한다.
                core.turn_transaction.mark_waiting_for_player(session, transaction_id)
                # 저장은 루프 종료 후 트레일링 save가 일괄 처리 (중복 제거)
                break

            # ── NARRATE ──
            elif action == "NARRATE":
                session.gm_narrate_count = getattr(session, "gm_narrate_count", 0) + 1
                if session.gm_narrate_count > MAX_NARRATE_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** NARRATE 한도({MAX_NARRATE_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, forced_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                narrate_instr = decision.get("narrate_instruction") or "현재 상황을 간략히 설명하십시오."
                # NOTE: typing 컨텍스트는 _dispatch_narrate 내부 API 호출 블록에서만 활성화됨.
                # 외부에서 typing()으로 감싸면 stream_text_to_channel 실행 시 typing이 살아있어
                # Discord 상충으로 스트리밍이 멈추는 버그 발생 — 외부 typing 제거.
                narrate_text = await self._dispatch_narrate(
                    session, narrate_instr, transaction_id=transaction_id)
                if narrate_text:
                    core.logs.event("turn", "NARRATE 대기", session=session,
                                    data={"stage": "narrate", "tx": transaction_id, "n": session.gm_narrate_count,
                                          "text": narrate_text})
                # WP-01: 플레이어 입력 대기(비종료). 재입력 시 같은 트랜잭션을 재사용한다.
                core.turn_transaction.mark_waiting_for_player(session, transaction_id)
                # 저장은 루프 종료 후 트레일링 save가 일괄 처리 (중복 제거)
                break  # 플레이어 응답 대기

            # ── ROLL ──
            elif action == "ROLL":
                rolls = decision.get("rolls") or []
                if not rolls:
                    await m_send(
                        "⚠️ GM이 ROLL을 선언했으나 굴림 항목이 비어 있어 PROCEED로 폴백합니다."
                    )
                    fallback_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, fallback_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                # WP-01: ROLL은 여기서 루프가 반환되고 버튼/타임아웃 콜백에서 재개된다.
                #        불변 식별자를 View에 실어 async UI 경계를 넘긴다(WAITING_FOR_ROLL).
                core.turn_transaction.mark_waiting_for_roll(session, transaction_id)
                # 버튼 UI 전송 후 루프 종료 (계속 처리는 버튼 콜백 담당)
                await self._dispatch_rolls(
                    session, rolls, player_message, list(roll_results),
                    transaction_id=transaction_id)
                await core.save_session_data(self.bot, session)
                return

            # ── PROCEED ──
            elif action == "PROCEED":
                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or ""
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                # NOTE: 자원 증감은 추출층위 소관이다(기획 규정 — 지시층위의 자원·상태
                #       수정권한 제거). 5.27.0에서 apply_extraction으로 옮겼는데
                #       이 경로가 남아 같은 턴에 이중 적용될 수 있었다.

                # ── 방안 E 제거 (방안 D) ──
                # _verify_proceed_instruction 호출 삭제.
                # 미선언 PC 행동 방지는 지시층위 [최우선 절대 원칙]으로 커버.

                # 서사 사건 평가는 event_assessment로 헬퍼에 전달되어 진행도 갱신·재계획에 사용된다.
                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=transaction_id)
                return

            else:
                await m_send(f"⚠️ GM이 알 수 없는 action을 반환했습니다: {action}")
                break

        else:
            # 루프 한도 도달 → 강제 PROCEED
            await m_send(f"⚙️ GM 내부 루프 한도({MAX_ITERATIONS_PER_MESSAGE}) 도달 → 강제 PROCEED.")
            await self._finish_proceed_and_continue(
                session, "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오.", master_ch,
                transaction_id=transaction_id)
            return

        # ASK/NARRATE는 break로 여기 도달 — 대기 상태를 한 번 저장한다.
        await core.save_session_data(self.bot, session)

    # ─────────────────────────────────────────────────────────────
    # 지시층위 호출
    # ─────────────────────────────────────────────────────────────

    @auto.command(name="퀘스트")
    async def auto_quest(self, ctx, action: str = "", *, arg: str = ""):
        """퀘스트 확인·간섭 (기획 규정 — 마스터 세션에서 명령어로 가능하게).

        !자동 퀘스트            현재 상태와 후보
        !자동 퀘스트 열기 <id>   특정 퀘스트를 연다
        !자동 퀘스트 닫기        진행 중인 퀘스트를 중단한다
        !자동 퀘스트 진전 <케이스> 특정 케이스로 진전시킨다
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session:
            await ctx.send("이 채널에는 세션이 없습니다.")
            return
        if core.player_world.legacy_narrative_retired(session):
            # H07-C: 동적 세계 세션은 사건 상태기계가 서사 권위다 — 레거시 퀘스트 수동 개입은 병렬 권위를 만든다
            await ctx.send("이 세션은 동적 세계 세션입니다. 레거시 퀘스트는 사용하지 않습니다(사건 상태기계가 권위).")
            return

        state = core.quest.get_state(session)
        active = state.get("active")

        if not action:
            lines = ["**[퀘스트 상태]**"]
            if active:
                lines.append(f"진행 중: **{active['name']}** (노드 `{active['node']}`)")
                if active.get("path"):
                    lines.append(f"> 경로: {' → '.join(active['path'])}")
                if active.get("slots"):
                    lines.append(f"> 슬롯: {active['slots']}")
            else:
                lines.append("진행 중인 퀘스트가 없습니다.")

            cleared = state.get("cleared") or []
            if cleared:
                subs = sum(1 for c in cleared if c.get("line") == "sub")
                lines.append(f"클리어: 서브 {subs} · 전체 {len(cleared)}")

            cands = core.quest.filter_available(session)
            if cands:
                lines.append("\n**[현재 후보]**")
                for q in cands[:10]:
                    lines.append(f"· `{q['id']}` {q['name']} ({q['line']})")
            await ctx.send("\n".join(lines)[:1900])
            return

        if action == "열기":
            qid = arg.strip()
            quest = core.quest._find_quest(session, qid)
            if not quest:
                await ctx.send(f"`{qid}` 퀘스트를 찾을 수 없습니다.")
                return
            opened = core.quest.start_quest(session, quest)
            await core.save_session_data(self.bot, session)
            await ctx.send(f"📜 **{opened['name']}** 을(를) 열었습니다.")
            return

        if action == "닫기":
            if not active:
                await ctx.send("진행 중인 퀘스트가 없습니다.")
                return
            name = active["name"]
            state["active"] = None
            await core.save_session_data(self.bot, session)
            await ctx.send(f"⏹ **{name}** 을(를) 중단했습니다.")
            return

        if action == "진전":
            if not active:
                await ctx.send("진행 중인 퀘스트가 없습니다.")
                return
            if core.quest.move_to_case(session, arg.strip()):
                await core.save_session_data(self.bot, session)
                st = core.quest.get_state(session)
                cur = st.get("active")
                node = cur["node"] if cur else "(종료)"
                await ctx.send(f"▶ `{arg.strip()}` 으로 진전했습니다. 현재 노드 `{node}`")
            else:
                await ctx.send(f"`{arg.strip()}` 케이스로 진전할 수 없습니다.")
            return

        await ctx.send("사용법: `!자동 퀘스트 [열기|닫기|진전] [값]`")

    @commands.command(name="되감기")
    async def rewind_cmd(self, ctx, turn: str = None):
        """
        되감기 실행 (마스터 채널).

        NOTE: 디스플레이 채널 UI 도입 전까지의 임시 진입점.
              디스플레이 완성 후에는 RewindView 버튼이 주 경로가 된다.

        사용법:
            !되감기        — 1턴 되감기 (직전 턴 제거)
            !되감기 8      — 8턴 종료 시점으로 되감기
            !되감기 범위    — 되감기 가능 범위 확인
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session:
            await ctx.send("이 채널에 연결된 세션이 없습니다.")
            return

        oldest, newest = core.available_range(session)
        if newest == 0:
            await ctx.send("되감기 기록이 아직 없습니다. (4.6.0 이후 진행한 턴부터 기록됩니다)")
            return

        if turn == "범위":
            await ctx.send(
                f"⏪ 되감기 가능 범위: **{oldest}~{newest - 1}턴** "
                f"(현재 {newest}턴 / 상한 {core.REWIND_MAX_TURNS}턴)"
            )
            return

        if turn is None:
            target = newest - 1
        else:
            try:
                target = int(turn)
            except ValueError:
                await ctx.send("사용법: `!되감기` / `!되감기 [턴번호]` / `!되감기 범위`")
                return

        view = RewindConfirmView(self.bot, session, target)
        view.bind(await ctx.send(
            f"⚠️ **{target}턴 종료 시점으로 되돌립니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
            f"제거되는 정보는 되감기 로그로 이관됩니다.",
            view=view,
        ))

    async def _forced_proceed_instruction(self, session, player_message: str,
                                          roll_results: list, master_ch,
                                          sim_result: dict | None = None,
                                          *, transaction_id: str | None = None) -> str:
        """
        ASK/NARRATE 한도 초과 또는 ROLL 폴백으로 강제 PROCEED 전환할 때,
        지시층위를 호출해 묘사 지시문을 확보한다.

        NOTE: 층위 분리 이후 판단층위 결정에는 proceed_instruction이 없다.
              지시문 없이 진행하면 묘사 품질이 급락하므로 여기서 별도 확보한다.
              호출 실패 시에만 범용 문구로 폴백한다.
        """
        fallback = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
        decision = await self._call_gm_logic(
            session, player_message, roll_results, master_ch,
            sim_result=sim_result, action="PROCEED",
            transaction_id=transaction_id)
        if not decision:
            return fallback
        # WP-C(B-C2): 강제 PROCEED 결정의 지시효과(quest_choice/intended_case/info_access)도
        #   기존 단일 owner로 스테이징한다(유실 금지). 정본 적용은 READY 이후에만.
        if transaction_id is not None:
            core.turn_preparation.stage_instruction_effects(
                session, {**decision, "action": "PROCEED"}, transaction_id=transaction_id)
        return _clean_proceed_instruction(decision.get("proceed_instruction") or fallback)

    async def _call_judgment(self, session, player_message: str, roll_results: list,
                             master_ch, *, transaction_id: str | None = None, gate=None,
                             declaration_draft: bool = False, discovery_rows=()) -> dict | None:
        """
        판단층위 호출 — 진행 유형(ASK/NARRATE/ROLL/PROCEED)을 결정한다.

        [캐시 미사용]
        판단은 초단기 맥락과 선언만으로 가능하므로 세션 캐시를 읽지 않는다.
        ROLL 판정 명세(rolls)도 이 층위가 생성하므로, ROLL이 발생해도
        캐시 읽기는 뒤이은 지시층위 호출 1회로 유지된다.
        (분리 이전에는 지시층위가 두 번 호출되어 캐시를 2회 읽었다.)

        Returns:
            판단 결과 dict 또는 실패 시 None. 재시도는 이 함수 내부에서 처리한다.
        """
        # WP-01: 현재 트랜잭션이면 JUDGING으로 표기(수동/인트로 경로는 no-op).
        core.turn_transaction.mark_transaction_status(
            session, transaction_id, core.turn_transaction.TurnStatus.JUDGING)
        # H05-A: DYNAMIC 세션이면 코드 후보 매니페스트를 같은 판단 호출에 싣는다(추가 provider 호출 0).
        _wt = self._world_turn(session, transaction_id, create=True)
        _schema = JUDGMENT_RESPONSE_SCHEMA
        if _wt is not None:
            _wt.pending_route = None                       # v7.23: 일반 판단이 돌면 이전 경로 번호 스냅샷은 낡는다
            self._build_world_manifest(session, _wt)
            _schema = core.action_intent.extend_judgment_schema(JUDGMENT_RESPONSE_SCHEMA, _wt.manifest)
            if declaration_draft and not gate:
                # v7.23: 조건부 선언 검증 촉발 표시(스키마 사본 확장 — prompts.py 원본 불변)
                _schema["properties"]["assertion_claim"] = {"type": "boolean"}
                _schema["properties"]["discovery_target"] = {"type": "string"}
                _schema["properties"]["discovery_ref"] = {"type": "string",
                                                          "enum": ["NONE"] + [r[0] for r in discovery_rows]}
        user_prompt = _build_judgment_user_prompt(session, player_message, roll_results, world_turn=_wt, gate=gate,
                                                  declaration_draft=declaration_draft and not gate,
                                                  discovery_rows=discovery_rows if declaration_draft and not gate else ())

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            http_options=core.resilience.provider_http_options("judgment"),  # WP-C B-C4
            system_instruction=JUDGMENT_SYSTEM_INSTRUCTION,
            temperature=0.4,
            response_mime_type="application/json",
            response_schema=_schema,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )

        # 층위 자체 재시도 — 판단 실패가 지시층위 재시도로 번지지 않게 한다.
        # WP-02: 외부 판정 재시도 전체가 하나의 논리 오퍼레이션. operation_id를
        #        루프 밖에서 1회 생성하고 provider_attempt는 실제 호출마다 증가시킨다.
        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_TURN_JUDGMENT, session=session,
            model=JUDGMENT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
        core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
        decision = None
        for attempt in range(JUDGMENT_MAX_RETRIES):
            # 타임아웃 보호 — 지연된 원 응답이 재시도 결과와 경합하지 않도록
            # wait_for가 태스크를 취소한다(기획 규정).
            ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=JUDGMENT_MODEL, contents=contents, config=config,
                ),
                layer="judgment", session_id=session.session_id, retries=1,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not ok:
                continue

            # 비용 정산 — 캐시 미사용이므로 cached_tokens는 0
            try:
                meta = response.usage_metadata
                in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
                breakdown = core.calculate_text_gen_cost_breakdown(
                    JUDGMENT_MODEL,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cached_read_tokens=cached_tokens,
                )
                cost = breakdown["total_krw"]
                core.accrue(session, cost, breakdown["total_usd"])
                # WP-02 shadow 관측 — 레거시 계산값 그대로 기록(shadow == legacy).
                _cl_op.record(
                    input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                    output_tokens=out_tokens, thought_tokens=thought_tokens,
                    cost_usd=breakdown["total_usd"], cost_krw=cost,
                    usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
                core.write_cost_log(
                    session.session_id, f"{COST_LOG_PREFIX}판단층위 호출",
                    in_tokens, cached_tokens, out_tokens, cost, session.total_cost
                )
                core.update_stats(session, "judgment", out_tokens, thought_tokens)
                print(
                    f"[GM/{session.session_id}] 판단 비용: "
                    f"In={in_tokens:,} Out={out_tokens:,} → {core.format_cost(cost)}"
                )
                if not hasattr(session, "turn_cost_log"):
                    session.turn_cost_log = []
                session.turn_cost_log.append({
                    "label": "판단층위", "cost": cost,
                    "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                    "manifest": [], "layer": "judgment",
                    # v7.30: 로우 입력 구성(확정·유력·가변·키워드) — 보고·예측 공용
                    "census": core.prompt_census.record(
                        session, "judgment", contents, system_text=_sys_text(config),
                        in_tokens=in_tokens, cached_tokens=cached_tokens),
                })
            except Exception as e:
                print(f"[GM] 판단층위 비용 정산 실패: {e}")

            raw_text = response.text or ""
            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError:
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
                try:
                    decision = json.loads(cleaned)
                except Exception as e:
                    core.logs.event("turn", "판단층위 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                    data={"code": "JUDGMENT_JSON", "attempt": attempt + 1})
                    continue
            # v7.12: ROLL 계약 검증 — 범위 밖·누락 값을 보정해 굴리지 않는다. 파싱 실패와 같은 재시도 경로.
            #   (rolls가 빈 ROLL은 기존 PROCEED 폴백 경로가 처리한다)
            if (isinstance(decision, dict) and str(decision.get("action", "")).upper() == "ROLL"
                    and decision.get("rolls")):
                _errs = core.judgment.validate_rolls(decision.get("rolls"))
                if _errs:
                    print(f"[GM] 판단층위 ROLL 계약 위반(시도 {attempt + 1}): {_errs}")
                    core.write_log(session.session_id, "api", "[판정 계약 위반] " + "; ".join(_errs))
                    decision = None
                    continue
            # v7.14: 게이트가 ROLL이면 그 발견 탐색 굴림(discovery=true)이 있어야 한다 — 없으면 같은 재시도 경로.
            if _gate_rolls_discovery(gate) and not (
                    isinstance(decision, dict) and str(decision.get("action", "")).upper() == "ROLL"
                    and any(isinstance(r, dict) and r.get("discovery") is True
                            for r in decision.get("rolls") or ())):
                print(f"[GM] 판단층위 발견 굴림 누락(시도 {attempt + 1}) — 게이트 ROLL")
                core.write_log(session.session_id, "api", "[판정 계약 위반] 게이트 ROLL인데 discovery 굴림 없음")
                decision = None
                continue
            break

        if not decision:
            if master_ch:
                await master_ch.send("⚠️ 판단층위 호출에 실패했습니다. 이번 발언을 스킵합니다.")
            return None

        if _wt is not None and _wt.manifest is not None:
            # 후보 키 → 정본 참조(코드 검증). raw 문자열은 정본이 되지 않는다. 새 의도 = 재계획.
            _wt.intent = core.action_intent.resolve_intent(
                _wt.manifest, decision.get(core.action_intent.INTENT_FIELD), world=session)
            self._settle_move_intent(session, _wt, transaction_id)
            _wt.plan = None
            core.write_log(session.session_id, "api",
                           "[H05 의도]\n" + json.dumps(_wt.intent.semantic_record(), ensure_ascii=False)
                           + f"\nunresolved={list(_wt.intent.unresolved)}")
        return decision

    async def _call_gm_logic(self, session, player_message: str, roll_results: list,
                              master_ch, sim_result: dict | None = None,
                              action: str = "PROCEED",
                              *, transaction_id: str | None = None) -> dict | None:
        """
        지시층위 모델 호출. DEFAULT_MODEL 사용.

        [캐시 활용 전략]
        세션 캐시(scenario_data + NPC 사전 + 세계관)가 유효한 경우, cached_content로 호출하여
        지시층위가 시나리오 전체 컨텍스트를 읽도록 한다.

        [방안 ①] GM_LOGIC_SYSTEM_INSTRUCTION은 세션 캐시 본문에 함께 구워져 있으므로,
        캐시 경로에서는 contents에 user_prompt만 넣는다(지시문이 캐시 읽기 단가로 처리됨):
          contents[0] user  : _build_logic_user_prompt()   (현재 상황 + 플레이어 발언)

        캐시가 없거나 모델 불일치 시(폴백): 캐시에 지시문이 없으므로
        GenerateContentConfig.system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION 방식으로 전달한다.

        Args:
            sim_result: 방안 6 서사 설계자 결과 (첫 번째 호출에만 주입, 이후 None)
        """
        # WP-01: 현재 트랜잭션이면 INSTRUCTING으로 표기(수동/인트로 경로는 no-op).
        core.turn_transaction.mark_transaction_status(
            session, transaction_id, core.turn_transaction.TurnStatus.INSTRUCTING)
        _wt = self._world_turn(session, transaction_id)
        _narrate = (action or "").upper() == "NARRATE"
        if _wt is not None and _wt.plan is None and not _narrate:
            # 지시층위도 이번 턴 정본 계획(장소 고정 포함)을 본다 — ROLL 뒤 PROCEED 경로(순수 계획)
            self._ensure_world_plan(session, _wt, transaction_id)
        # P3(G1): NARRATE는 세계를 커밋하지 않는다 — 계획·투영·필수 서사·전달 대기 없이 정본 기준 뷰로 지시한다.
        _wv = dataclasses.replace(_wt, plan=None) if (_wt is not None and _narrate) else _wt
        if _wt is not None:
            sim_result = None             # v7.23: DYNAMIC 지시는 서사 방향 시뮬레이션을 받지 않는다(강제 진행 경로 포함)
        user_prompt = _build_logic_user_prompt(session, player_message, roll_results,
                                                sim_result=sim_result, world_turn=_wv, action=action)
        _tie_schema = None
        if _wv is not None and _narrate:
            user_prompt += core.turn_world.narrate_hold_block(_wv, session)
        elif _wt is not None and _wt.plan is not None:
            user_prompt += core.turn_world.narration_overlay(_wt.plan, _wt.ctx.definition, _wt.ctx, world=session)
            user_prompt += core.turn_world.pending_delivery_block(_wt.ctx, session)
            # P1(D2): 남은 자유 경합(같은 분 서사 선후)은 묘사 전에 지시층위가 고른다 — 무효면 코드 결정
            _tie_block, _tie_schema = core.turn_world.tie_choice_schema(_wt.plan, _wt.ctx.definition, _wt.ctx)
            user_prompt += _tie_block
        _jr = self._tx_judgment(session, transaction_id)
        if _jr:
            user_prompt += _declaration_block(_jr, transaction_id)     # v7.14 권위 있는 시도·발견 결과
        if _wt is not None:
            user_prompt += WORLD_REACTION_AUTHORITY_BLOCK                 # v7.23 DYNAMIC 세계 반응 권위
        # 판단층위가 확정한 진행 유형을 지시층위에 전달한다. 지시층위는 이 유형을 바꾸지 않는다.
        user_prompt = (
            f"[확정된 진행 유형] {action}\n"
            f"※ 이 유형은 판단층위에서 이미 결정되었습니다. 변경하지 말고, "
            f"이 유형에 필요한 지시문만 작성하십시오.\n\n"
        ) + user_prompt

        # SVG 카드: 얼굴 식별 후보(장면 카드 인물의 transient 키) — 응답 키는 서버가 ActorId로 검증한다
        _card_world = core.speaker_card.card_world(session) if _wt is not None else None
        _id_manifests = {}
        for _kind in ("face", "name"):
            _m = core.speaker_card.identity_manifest(_card_world, _wv, session, _kind) if _card_world else {}
            if _m:
                _id_manifests[_kind] = _m
                user_prompt += core.speaker_card.identity_block(_m, _card_world.definition, session, _kind)


        # 캐시 활용 가능 여부 판단
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        # WP-E(E-E3): 되감기/재생성 뒤 미래 이력을 담은 캐시는 쓰지 않는다(캐시 없는 폴백).
        use_cache   = bool(cache_name and cache_model == core.DEFAULT_MODEL
                           and core.turn_history.cache_usable(session))

        try:
            # ── 상황별 스키마 조립 ──
            # 퀘스트를 고를 수 없는 상황(진행 중)에서는 quest_choice 필드를
            # 스키마에서 아예 뺀다. 필드가 없으면 모델이 값을 낼 수 없으므로
            # 잘못된 선택이 구조적으로 불가능해진다.
            logic_schema = GM_LOGIC_RESPONSE_SCHEMA
            _retired = core.player_world.legacy_narrative_retired(session)
            if _retired:
                # H07-C: 동적 세계 세션 — 레거시 퀘스트 선택·info_access(정보 원장) 필드를 스키마에서 뺀다
                #        (prompts.py 원본 스키마 불변, 런타임 사본만). 값을 낼 수 없으니 병렬 권위가 생길 수 없다.
                logic_schema = dict(GM_LOGIC_RESPONSE_SCHEMA)
                #   v7.25(F12): 레거시 서사 계획 신호 event_assessment도 뺀다(동적 세계에는 소비할 계획이 없다)
                _gone = ("quest_choice", "quest_case", "info_access", "event_assessment")
                logic_schema["properties"] = {k: v for k, v in logic_schema["properties"].items() if k not in _gone}
                logic_schema["required"] = [k for k in logic_schema["required"] if k not in _gone]
            try:
                q_schema = None if _retired else core.quest.choice_schema(session)
                c_schema = None if _retired else core.quest.case_schema(session)
                if q_schema or c_schema:
                    logic_schema = dict(GM_LOGIC_RESPONSE_SCHEMA)
                    logic_schema["properties"] = dict(logic_schema["properties"])
                    logic_schema["required"] = list(logic_schema["required"])
                    if q_schema:
                        logic_schema["properties"]["quest_choice"] = q_schema
                        logic_schema["required"].append("quest_choice")
                    if c_schema:
                        # 활성 퀘스트가 있을 때만. 지시층위는 방향만 제시하고
                        # 진전 여부는 추출 수치가 정한다.
                        logic_schema["properties"]["quest_case"] = c_schema
                        logic_schema["required"].append("quest_case")
            except Exception as e:
                print(f"[퀘스트] 스키마 조립 실패: {e}")
            if _id_manifests:
                logic_schema = dict(logic_schema)
                logic_schema["properties"] = dict(logic_schema["properties"])
                for _kind, _m in _id_manifests.items():
                    logic_schema["properties"][core.speaker_card.identity_field(_kind)] = \
                        core.speaker_card.identity_schema(_m, _kind)
            if _tie_schema is not None:
                logic_schema = dict(logic_schema)
                logic_schema["properties"] = dict(logic_schema["properties"])
                logic_schema["properties"]["tie_order"] = _tie_schema

            if use_cache:
                # ── 캐시 활용 경로 (방안 ①) ──
                # GM_LOGIC_SYSTEM_INSTRUCTION은 이제 세션 캐시 본문에 함께 구워져 있으므로
                # contents에 신선 입력으로 다시 넣지 않는다(캐시 읽기 단가로 처리 → 비용 절감).
                logic_contents = [
                    types.Content(role="user",
                                  parts=[types.Part.from_text(text=user_prompt)]),
                ]
                config = types.GenerateContentConfig(
                    http_options=core.resilience.provider_http_options("instruction"),  # WP-C B-C4
                    cached_content=cache_name,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=logic_schema,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                # ── 폴백: 캐시 없음 / 모델 불일치 ──
                logic_contents = [
                    types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])
                ]
                config = types.GenerateContentConfig(
                    http_options=core.resilience.provider_http_options("instruction"),  # WP-C B-C4
                    system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=logic_schema,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            # 재시도·타임아웃 보호. 층위 호출은 모두 이 관문을 통과한다.
            _cl_op = core.cost_ledger.begin_operation(
                self.bot, core.cost_ledger.OP_TURN_INSTRUCTION, session=session,
                model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
                billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
            # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
            core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL,
                    contents=logic_contents,
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] Logic 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 자동 지시층위 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            core.update_stats(session, "instruction", out_tokens, thought_tokens)
            # 캐시 읽기 실측 — 캐시에 구워진 지시문까지 포함된 실제 값.
            if cached_tokens > getattr(session, "cache_read_tokens", 0):
                session.cache_read_tokens = cached_tokens
            # 예측 대조 — 신선 입력(In - Cached)으로 문자→토큰 계수를 자동 보정한다.
            core.record_actual_input(session, "instruction", in_tokens - cached_tokens)
            core.accrue(session, cost)
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}지시층위 호출",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            print(
                f"[GM/{session.session_id}] Logic 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)} (누적 {core.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "지시층위", "cost": cost,
                "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                "manifest": list(getattr(session, "gm_last_logic_manifest", [])), "layer": "instruction",
                "census": core.prompt_census.record(
                    session, "instruction", logic_contents, system_text=_sys_text(config),
                    in_tokens=in_tokens, cached_tokens=cached_tokens),
            })
        except Exception as e:
            print(f"[GM] Logic 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            decision = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                decision = json.loads(cleaned)
            except Exception as e:
                core.logs.event("turn", "지시층위 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "DIRECTOR_JSON", "raw": raw_text})
                if master_ch:
                    await master_ch.send("⚠️ 자동 지시층위 응답이 JSON 형식이 아닙니다. 이번 발언 스킵.")
                return None

        if _tie_schema is not None and _wt is not None:
            _tc = decision.get("tie_order")
            _wt.tie_choice = _tc if isinstance(_tc, str) else None      # 검증은 resolve_plan_tie(허용 키만)

        if _id_manifests and _wt is not None:
            # 이 시도의 WorldTurn에 누적 — process_observation이 저작 얼굴·이름 InfoId 획득으로 합류시킨다(커밋 후 공개)
            for _kind, _m in _id_manifests.items():
                _hit = core.speaker_card.parse_identified(_m, decision.get(core.speaker_card.identity_field(_kind)))
                if _hit:
                    _attr = "identified_faces" if _kind == "face" else "identified_names"
                    setattr(_wt, _attr, tuple(sorted(set(getattr(_wt, _attr)) | set(_hit))))
        if _wt is not None:
            try:
                core.speaker_card.prewarm(session, _wt)      # v7.37: 묘사 전에 장면 카드를 뒤에서 그려 둔다
            except Exception as e:  # noqa: BLE001
                print(f"[카드] 미리 그리기 실패(무시): {e}")

        # WP-B: 지시층위 효과(quest_case/quest_choice/info_access)는 여기서 직접
        #       canonical에 적용하지 않는다. 호출부 루프가 이 decision을 스테이징
        #       (canonical 미변경)하고, 묘사가 성립한 뒤 단일 호환 적용 경계에서만
        #       반영한다. AUD-024(묘사 실패 시 지시효과 누출) 차단 +
        #       AUD-005(중복 quest 적용 경로) 단일 owner 통합.
        return decision

    # WP-B: 기존 _update_info_ledger(지시층위 info_access 직접 병합)는
    #   core.turn_preparation._merge_info_ledger(순수 병합) + 스테이징으로 대체됨.
    #   canonical info_ledger는 묘사 성립 후 단일 호환 적용 경계에서만 갱신된다.

    # ─────────────────────────────────────────────────────────────
    # ROLL 실행 및 버튼 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _execute_rolls(self, session, rolls: list, game_ch,
                             *, transaction_id: str | None = None) -> list[str]:
        """rolls 목록을 굴리고 결과를 게임·마스터 채널에 선언.

        WP-C: 주사위 판정 결과 자체는 플레이어에게 공개된 판정 입력 사실로 그대로
        보존한다. 그러나 그 결과가 유발하는 성장 mutation(players.profile +1,
        stat_fail_counts)은 턴 소유 정본 변경이므로 트랜잭션 준비 객체에 스테이징하고
        READY 이후 legacy continuation에서만 적용한다. 같은 tx의 후속 ROLL은 스테이징
        투영을 읽는다. 사전 READY 실패 시 성장은 폐기된다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        results: list[str] = []
        _prep = None
        roll_session = session
        if transaction_id is not None:
            _prep = core.turn_preparation.ensure_preparation(session, transaction_id)
            if _prep is not None:
                roll_session = core.turn_preparation.growth_projection(session, _prep)
            else:
                # stale 재개 — 정본을 절대 바꾸지 않는 일회용 투영(성장 폐기).
                roll_session = core.turn_preparation.PreparationView(session, overrides={
                    "players": copy.deepcopy(getattr(session, "players", {}) or {}),
                    "stat_fail_counts": copy.deepcopy(
                        getattr(session, "stat_fail_counts", None) or {}),
                })

        wt = self._world_turn(session, transaction_id)
        records: list[dict] = []
        effects = {"growth_events": [], "fail_counts": {}}

        def _roll_side(side):
            d1, d2 = core.judgment.roll_2d50(random)
            side["dice"] = [d1, d2]
            side["R"] = d1 + d2
            side["success"] = core.judgment.is_success(side["R"], side["T"])
            side["margin"] = side["T"] - side["R"]

        def _game_line(side):
            res = "성공 🟢" if side["success"] else "실패 🔴"
            if not side["is_pc"]:
                # NPC 능력치·목표치는 비공개 — 플레이어에게는 주사위와 성패만
                return f"> 🎲 [{side['name']}] 판정 — 2d50 **{side['R']}** → **{res}**"
            return (f"> 🎲 [{side['name']}] **{side['stat']}** 판정 — 2d50 **{side['R']}** "
                    f"({side['dice'][0]}+{side['dice'][1]}) / 목표 {side['T']} ({side['breakdown']})"
                    f"  →  **{res}** (여유 {side['margin']:+d})")

        def _logic_text(side):
            res = "성공" if side["success"] else "실패"
            basis = "; ".join(b for b in (
                f"행동 근거: {side['method_basis']}" if side["method_basis"] else "",
                f"상태 근거: {side['status_basis']}" if side["status_basis"] else "") if b)
            return (f"{side['name']} {side['stat']} 판정(2d50): R={side['R']}, 목표 T={side['breakdown']}, "
                    f"{res}(여유 {side['margin']:+d})" + (f" [{basis}]" if basis else ""))

        for r in rolls:
            plan = _judge_plan(roll_session, r, wt)
            kind, act, opp = plan["kind"], plan["actor"], plan["opponent"]
            char_name, stat_name = act["name"], act["stat"]
            game_lines: list[str] = []
            logic_lines: list[str] = []

            if act["T"] is None:
                # 능력치를 확인할 수 없다(등급제·결측·장면 밖 NPC) — 허구 수치로 굴리지 않는다.
                game_lines.append(f"> 🎲 [{char_name}] {stat_name} 판정 — 능력치를 확인할 수 없어 판정을 보류합니다.")
                logic_lines.append(f"- {char_name} {stat_name} 판정 불가(능력치 미확인) — 코드 성패 없음, "
                                   f"정본 상황에 따라 서술 재량")
            else:
                _roll_side(act)
                game_lines.append(_game_line(act))
                logic_lines.append("- " + _logic_text(act))
                if kind == "CRAFT":
                    logic_lines.append(
                        f"- 제작 판정: 성공 여유 {act['margin']:+d} — 성공품은 여유가 클수록 마감·완성도가 높게 서술한다"
                        f"(품질 단계·수치 속성은 정하지 않는다). 실패해도 재료 손실을 자동으로 가정하지 말 것.")
                if kind == "CONTEST":
                    if opp is None or opp["T"] is None:
                        oname = opp["name"] if opp else "상대"
                        logic_lines.append(f"- 대결 상대 {oname}의 판정 능력치를 확인할 수 없음 — 상대 판정 없음. "
                                           f"우열은 코드가 정하지 않았다(정본 상황에 따라 서술).")
                    else:
                        _roll_side(opp)
                        game_lines.append(_game_line(opp))
                        logic_lines.append("- " + _logic_text(opp))
                        outcome = core.judgment.contest_outcome(act["margin"], opp["margin"])
                        opp["contest"] = act["contest"] = outcome
                        if outcome in ("A", "B"):
                            win = act if outcome == "A" else opp
                            verdict = f"{win['name']} 우세"
                        elif outcome == "TIE":
                            verdict = "교착 — 같은 여유, 우열 미결"
                        else:
                            verdict = "쌍방 실패 — 승자 없음(상황 유지·쌍방 실패로 서술)"
                        game_lines.append(f"> ⚔️ 대결 결과: **{verdict}**")
                        logic_lines.append(
                            f"- 대결 결과: {verdict} (여유 {act['margin']:+d} 대 {opp['margin']:+d}). "
                            f"격차는 연출 근거일 뿐 피해·보상·제압 수치를 새로 만들지 말 것.")

            if game_ch:
                for gl in game_lines:
                    await core.send_streamed(self.bot, game_ch, gl)
                if act["T"] is not None and act["is_pc"]:
                    await core.send_streamed(
                        self.bot, game_ch,
                        "> ✅ " + core.master.say("turn.roll_success", f"**{stat_name}** 판정이 성공했습니다.")
                        if act["success"] else
                        "> ❌ " + core.master.say("turn.roll_failure", f"**{stat_name}** 판정이 실패했습니다."))
                # 판정 줄은 send_streamed가 게임 채널 기록([GM])에 남긴다 — 별도 기록하지 않는다(중복 방지).
            # 주사위 효과음 — 토글이 켜져 있을 때만.
            try:
                if core.is_enabled(session, "sfx"):
                    vc = getattr(session, "voice_client", None)
                    if vc and vc.is_connected():
                        await core.play_dice_sfx(self.bot, getattr(game_ch, "guild", None))
            except Exception as e:
                print(f"[효과음] 재생 실패: {e}")

            if master_ch:
                # 운영자는 NPC 목표치까지 전부 본다
                await master_ch.send("🤖 [GM 굴림]\n" + "\n".join(logic_lines))
            for ll in logic_lines:
                session.current_turn_logs.append(ll.lstrip("- "))
            _rec = {"kind": kind, "actor": copy.deepcopy(act), "opponent": copy.deepcopy(opp)}
            if r.get("discovery") is True:
                # v7.14: 발견 성공 = 이 굴림의 성패만(다른 판정의 성공을 전용하지 않는다). 실패는 '이번에 찾지 못함'.
                _g = (self._tx_judgment(session, transaction_id) or {}).get("gate")
                if _gate_rolls_discovery(_g):
                    _ok = act["T"] is not None and bool(act["success"])
                    _rec["discovery"] = {"target": _g["target"], "kind": _g["kind"], "place": _g["place"],
                                         "success": _ok}
                    _dl = (f"- 주변 발견 탐색 '{_g['target']}': " + (
                        "찾아냈다." if _ok else "이번 탐색에서는 찾지 못했다(없다고 단정하지 말 것)."))
                    logic_lines.append(_dl)
                    session.current_turn_logs.append(_dl.lstrip("- "))
            results.extend(logic_lines)
            records.append(_rec)

            # 통계 — 굴린 주사위 수
            try:
                for _uid in (session.players or {}):
                    await core.stats.bump(_uid, dice_rolled=1)
            except Exception:
                pass

            # ── 성장 · 행운 판정 (기획 규정) ──
            # PC 본인 판정의 성패만 넘긴다. 성장 재판정은 2d50과 독립인 d20 원척도 비교다(core.growth).
            if act["T"] is not None and act["is_pc"] and stat_name:
                uid = act["uid"]
                try:
                    outcome = core.process_roll_outcome(
                        roll_session, char_name, stat_name,
                        failed=not act["success"], sides=core.growth.GROWTH_DIE,
                    )
                    _fk = f"{char_name}:{stat_name}"
                    if _fk in (getattr(roll_session, "stat_fail_counts", None) or {}):
                        effects["fail_counts"][_fk] = roll_session.stat_fail_counts[_fk]
                    growth = outcome["growth"]
                    if growth:
                        g_line = core.format_growth(char_name, stat_name, growth)
                        # WP-F: '성장!'은 정본 변이(스테이징) 알림이다 — 자동 턴에서는 커밋된
                        #   성장 사실에서만(derived, durable COMMITTED 이후) 게임 채널에 나간다.
                        #   실패·폐기 시도에 거짓 영구 알림을 남기지 않는다. 판정 사실(아직/상한)은
                        #   정본 변이가 아니므로 즉시 보인다. 수동 경로(transaction_id 없음)는 기존대로.
                        if growth.get("grew") and transaction_id is not None:
                            if _prep is not None:
                                _prep.growth_notices.append(g_line)
                        elif game_ch:
                            await core.send_streamed(self.bot, game_ch, g_line)
                        if master_ch:
                            await master_ch.send(g_line)
                        if growth.get("grew"):
                            if _prep is not None and uid:
                                _prep.growth_events.append(
                                    (uid, stat_name, growth["new_value"]))
                                effects["growth_events"].append([uid, stat_name, growth["new_value"]])
                            results.append(
                                f"- {char_name} {stat_name} 성장: "
                                f"{growth['new_value']}로 상승"
                            )
                    luck = outcome["luck"]
                    if luck:
                        l_line = core.format_luck(char_name, luck)
                        if game_ch:
                            await core.send_streamed(self.bot, game_ch, l_line)
                        if master_ch:
                            await master_ch.send(l_line)
                        # 지시층위에 행운 발생을 전달한다. 서사 제약은
                        # 프롬프트가 담당한다(행운은 상황에서 발생 가능한
                        # 범위여야 하며 구도를 뒤틀어서는 안 된다).
                        results.append(
                            f"- {char_name} 행운 발생: 이번 실패를 "
                            f"현재 상황에서 자연스럽게 발생 가능한 행운으로 완충할 것"
                        )
                except Exception as e:
                    print(f"[성장·행운] 판정 실패(진행에는 영향 없음): {e}")

        if _prep is not None:
            # 재서술이 다시 굴리지 않고 같은 판정·성장 효과를 재사용하도록 판단 기록에 남긴다.
            core.turn_history.note_rolls(session, transaction_id, records, effects)
        return results

    async def _dispatch_rolls(self, session, rolls: list, player_message: str, prior_roll_results: list,
                              *, transaction_id: str | None = None):
        """ROLL 결정 시 플레이어에게 버튼 UI 전송."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        target_uid = core.get_uid_by_char_name(session, session.gm_target_char)

        roll_descs = []
        wt = self._world_turn(session, transaction_id)
        # 미리보기는 실행과 같은 조립(_judge_plan)을 쓴다 — 같은 tx의 스테이징 성장 투영 기준.
        view = core.turn_preparation.PreparationView(
            session, overrides={"players": core.turn_preparation.projected_players(session)})
        for r in rolls:
            plan = _judge_plan(view, r, wt)
            act, opp = plan["actor"], plan["opponent"]
            label = {"CRAFT": "제작 ", "CONTEST": "대결 "}.get(plan["kind"], "")
            if act["T"] is None:
                desc = f"{label}**{act['stat']}** 판정 (능력치 미확인)"
            elif act["is_pc"]:
                desc = (f"{label}**{act['stat']}** 판정 — 목표 {act['T']} ({act['breakdown']}, "
                        f"성공률 {core.judgment.success_chance(act['T']):.0%})")
            else:
                desc = f"{label}[{act['name']}] 판정"
            if opp is not None:
                desc += f" vs {opp['name']}"
            roll_descs.append(desc)

        desc_text = " / ".join(roll_descs)
        mention = f"<@{target_uid}>" if target_uid else "플레이어"

        view = GMRollView(
            cog=self,
            session=session,
            roll_specs=rolls,
            player_message=player_message,
            prior_roll_results=prior_roll_results,
            target_uid=target_uid,
            transaction_id=transaction_id,
        )

        roll_prompt_text = (
            f"> 🎲 {mention}, " + core.master.say("turn.roll_request", "판정이 필요합니다!") + "\n"
            f"> {desc_text}\n"
            f"> 2d50 합이 목표보다 작으면 성공합니다. 아래 버튼을 눌러 주사위를 굴리세요. (5분 내 미클릭 시 자동 굴림)"
        )
        if game_ch:
            view.message = await game_ch.send(roll_prompt_text, view=view)
            core.write_log(session.session_id, "game_chat", f"[판정 요청]: {desc_text}")
            _set_roll_open(session, transaction_id, True)
        if master_ch:
            await master_ch.send(
                f"🤖 **[GM ROLL]** 플레이어 버튼 대기 중...\n> {desc_text}"
            )

    # ─────────────────────────────────────────────────────────────
    # ROLL 결과 반영 계속 처리
    # ─────────────────────────────────────────────────────────────

    async def _continue_with_roll_results(self, session, player_message: str, roll_results: list,
                                          *, transaction_id: str | None = None):
        """GMRollView 버튼 클릭 후 굴림 결과를 반영하여 지시층위 재호출."""
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.gm_active:
                return

            # WP-01: stale 재개 가드. View는 async 경계 너머에서 재개되므로, 그 사이
            #        새 시도가 생겼다면(플레이어 재요청 등) 낡은 ROLL 콜백은 옛 트랜잭션을
            #        되살리지 않고 조용히 종료한다. ID가 없으면(레거시/수동) 통과시킨다.
            if transaction_id is not None and not core.turn_transaction.is_current_transaction(session, transaction_id):
                print(f"[TURN] stale roll continuation tx={str(transaction_id)[:8]} 무시(현재 트랜잭션 아님)")
                return

            if core.auto_cost_cap_reached(self.bot, session):
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            try:
                # WP-E: 굴림 결과까지 포함한 지시층위 입력을 판단 기록에 반영(재생성 보존 대상).
                core.turn_history.note_judgment(
                    session, transaction_id, player_message=player_message,
                    roll_results=list(roll_results))
                _tx = core.turn_transaction.get_active_transaction(session)
                if _tx is not None and _tx.transaction_id == transaction_id:
                    _tx.pending_roll = None                       # 이어서 진행 — 보관 결과는 이번 호출이 소유
                decision = await self._call_gm_logic(
                    session, player_message, roll_results, master_ch,
                    transaction_id=transaction_id)
                if not decision:
                    # v7.33(F5 — 사용자 결정 '재사용'): 굴린 결과를 시도에 보관하고 [이어서 진행]으로 같은 결과를 다시 쓴다
                    #   (재선언·재굴림 없음 — 판정이 끝날 때까지 새 선언은 _roll_pending_blocks가 막는다).
                    await m_send("⚠️ GM 결정 호출 실패 — 판정 결과를 보관했습니다. 플레이어의 [이어서 진행]을 기다립니다.")
                    await self._hold_roll_results(session, player_message, roll_results, transaction_id)
                    return

                action = decision.get("action", "PROCEED").upper()
                reasoning = decision.get("reasoning", "")
                # WP-C(B-C2): 굴림 후 지시층위 결정의 지시효과를 기존 단일 owner로 스테이징한다
                #   (유실 금지). 정본 적용은 READY 이후 legacy continuation에서만.
                core.turn_preparation.stage_instruction_effects(
                    session, {**decision, "action": action}, transaction_id=transaction_id)
                action_labels = {
                    "ASK":     "🟡 ASK (명확화 요청)",
                    "ROLL":    "🎲 ROLL (주사위 판정)",
                    "PROCEED": "🟢 PROCEED (턴 진행)",
                }
                core.logs.event("turn", f"굴림 후 판단 {action}", session=session,
                                data={"stage": "post_roll", "action": action, "reasoning": reasoning})
                if action not in ("PROCEED", "ROLL"):          # v7.35: PROCEED는 층위활동 보고(🧭)로
                    await m_send(f"🤖 **[GM 판단 (굴림 후)]** {action_labels.get(action, action)} — {reasoning[:120]}")

                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or
                    "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=transaction_id)
            except Exception as e:  # noqa: BLE001 — v7.33(F1): 굴림 재개 태스크 밖으로 새지 않게
                await self._abort_turn_before_narration(
                    session, transaction_id, player_message, master_ch,
                    code=core.turn_transaction.FailureCode.UNEXPECTED_EXCEPTION, stage="ROLL_CONTINUE", exc=e)

    async def _hold_roll_results(self, session, player_message, roll_results, transaction_id) -> None:
        """굴림 뒤 지시층위 실패 — 결과를 시도에 보관하고 게임 채널에 [이어서 진행] 버튼(재시작하면 사라짐 → 재선언 안내)."""
        tx = core.turn_transaction.get_active_transaction(session)
        if transaction_id is None or tx is None or tx.transaction_id != transaction_id:
            return
        tx.pending_roll = {"player_message": player_message, "roll_results": list(roll_results)}
        core.turn_transaction.mark_waiting_for_roll(session, transaction_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            closed = "" if core.is_session_open(session) else " 세션이 닫혀 있으니 세션을 연 뒤 눌러 주십시오."
            try:
                await game_ch.send("⚠️ 판정 뒤 진행을 준비하지 못했습니다. 판정 결과는 그대로 보관했습니다."
                                   + closed + "\n> 아래 버튼을 누르면 같은 결과로 이어서 진행합니다.",
                                   view=RollContinueView(self, session, transaction_id))
            except Exception as e:  # noqa: BLE001
                print(f"[판정] 이어서 진행 안내 실패: {e}")

    # ─────────────────────────────────────────────────────────────
    # NARRATE 디스패치 (경량 캐시 기반 GM 응답)
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_narrate(self, session, narrate_instruction: str,
                                *, transaction_id: str | None = None) -> str | None:
        """
        캐시 기반 경량 LLM 호출로 짧은 GM 응답(NARRATE)을 생성하고 게임 채널에 스트리밍.

        NOTE: PROCEED의 풀 프롬프트 대신 최근 로그 + narrate_instruction만 전달하여
        약 300자 이내의 빠른 응답을 생성. 캐시 히트 시 비용은 PROCEED의 절반 이하.
        대사 마커(@대사:이름|본문)를 감지하여 인물 이미지·말풍선 포맷으로 자동 변환.

        Args:
            session: TRPGSession
            narrate_instruction (str): 지시층위가 생성한 경량 응답 지시문 (100자 이내)

        Returns:
            str | None: 생성된 NARRATE 응답 텍스트 (스트리밍 완료 후). 실패 시 None.
        """
        # WP-01: 현재 트랜잭션이면 NARRATING으로 표기(수동/인트로 경로는 no-op).
        core.turn_transaction.mark_transaction_status(
            session, transaction_id, core.turn_transaction.TurnStatus.NARRATING)
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        # 최근 raw_logs 4개 (턴 개수 유지, 각 턴은 온전 원문)
        recent_parts = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                recent_parts.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_parts) if recent_parts else "(최근 로그 없음)"

        # 이번 턴 현재까지 누적된 대화
        current_turn_str = "\n".join(session.current_turn_logs) if session.current_turn_logs else "(없음)"

        narrate_prompt = build_narrate_prompt(recent_str, current_turn_str, narrate_instruction)
        _nwt = self._world_turn(session, transaction_id)
        if _nwt is not None:
            # P3(G1): 지시층위와 같은 비진행 경계 — 정본 시각·장소에서 짧게 답한다(세계 진행 없음)
            narrate_prompt += "\n" + core.turn_world.narrate_hold_block(_nwt, session)
            # v7.25(F02/A04): 계획 없는 응답도 정본 장면 인물을 받는다(없는 인물을 지어내지 않게)
            narrate_prompt += core.turn_world.scene_npc_block(_nwt.ctx, None, session)


        # NOTE: max_output_tokens를 설정하지 않음 — PROCEED(_execute_proceed)와 동일한 방침.
        # DEFAULT_MODEL(gemini-3-flash-preview)은 thinking 모델이므로, max_output_tokens를
        # 지정하면 내부 thinking 토큰까지 한도에 포함되어 실제 텍스트 출력이 거의 없는
        # MAX_TOKENS 조기 종료가 발생한다. 출력 길이는 프롬프트의 "300자 이내" 지시로 제어한다.
        # WP-02: game_ch/else 두 분기는 상호배타이며 동일 논리 오퍼레이션이다.
        #        하나의 operation_id를 공유하고 양쪽 호출을 관측한다.
        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_TURN_LIGHT_NARRATE, session=session,
            model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
        core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
        try:
            if core.turn_history.cache_usable(session):     # WP-E(E-E3)
                config = types.GenerateContentConfig(
                    http_options=core.resilience.provider_http_options("narration"),  # WP-C B-C4
                    cached_content=session.cache_name,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                config = types.GenerateContentConfig(
                    http_options=core.resilience.provider_http_options("narration"),  # WP-C B-C4
                    system_instruction=self.bot.system_instruction,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            # PROCEED와 동일한 구조: typing()은 API 호출만 감싸고,
            # 출력(stream_text_to_channel)은 typing 컨텍스트 밖에서 실행한다.
            if game_ch:
                async with game_ch.typing():
                    _ok, response = await core.call_with_retry(
                        lambda: asyncio.to_thread(
                            self.bot.genai_client.models.generate_content,
                            model=core.DEFAULT_MODEL,
                            contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                            config=config,
                        ),
                        layer="narration", session_id=session.session_id,
                        on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
                    )
                    if not _ok:
                        raise RuntimeError("AI 호출 실패")
            else:
                _ok, response = await core.call_with_retry(
                    lambda: asyncio.to_thread(
                        self.bot.genai_client.models.generate_content,
                        model=core.DEFAULT_MODEL,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                        config=config,
                    ),
                    layer="narration", session_id=session.session_id,
                    on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
                )
                if not _ok:
                    raise RuntimeError("AI 호출 실패")
        except Exception as e:
            core.logs.event("turn", "NARRATE 호출 실패", level="ERROR", session=session, exc=e,
                            data={"code": "NARRATE_CALL"})
            if master_ch:
                await master_ch.send(f"⚠️ GM NARRATE 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}NARRATE 경량 응답",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            print(
                f"[GM/{session.session_id}] NARRATE 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)} (누적 {core.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "묘사층위(NARRATE)", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
        except Exception as e:
            print(f"[GM] NARRATE 비용 정산 실패: {e}")

        narrate_text = (response.text or "").strip()
        if not narrate_text:
            return None


        # 게임 채널에 스트리밍 출력 (대사 마커 처리 포함)
        # NOTE: PROCEED(_execute_proceed)와 동일한 구조 — typing 컨텍스트 밖에서 stream_text_to_channel 호출.
        # typing은 위 API 호출 블록에서만 활성화되며, 이 시점에서는 이미 종료된 상태.
        if game_ch:
            paragraphs = [p.strip() for p in narrate_text.split("\n\n") if p.strip()]
            paragraphs = core.merge_consecutive_dialogues(paragraphs)

            for paragraph in paragraphs:
                dialogue = core.parse_dialogue_paragraph(paragraph)
                if dialogue:
                    speaker, content = dialogue
                    _dctx = core.speaker_card.display_context(session, speaker)
                    if _dctx is None:
                        await core.maybe_send_speaker_image(game_ch, session, speaker)
                        formatted = core.format_dialogue_block(speaker, content)
                    else:
                        # SVG 카드(DYNAMIC): NARRATE는 이미지를 내지 않고 헤더만 커밋된 이름 인지로 표시한다
                        formatted = core.format_dialogue_block(_dctx.header, content)
                    await core.stream_text_to_channel(
                        self.bot, game_ch, formatted,
                        words_per_tick=15, tick_interval=1.5, quote_prefix=False
                    )
                else:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, paragraph,
                        words_per_tick=15, tick_interval=1.5
                    )
            # P2 Q7: NARRATE 본문도 더빙 대상(대사는 NPC 목소리, 그 밖은 진행자 내레이션 목소리)
            await self._voice_lines(session, paragraphs, transaction_id=transaction_id, kind="paragraphs")

        # current_turn_logs에 추가 — PROCEED 시 AI가 맥락을 볼 수 있도록
        session.current_turn_logs.append(f"[진행자 (GM)]: {narrate_text}")
        return narrate_text

    # ─────────────────────────────────────────────────────────────
    # PROCEED 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_proceed(self, session, instruction: str,
                                *, transaction_id: str | None = None,
                                preparation=None):
        """GameCog._execute_proceed를 호출하여 묘사 생성·연출(자동 경로 전용).

        WP-C: 확정 묘사 직후 on_finalized 훅이 동시 준비 작업을 발사하고, 전달은 그와
        겹쳐 진행된다. 이 함수는 어떤 턴 소유 정본도 적용하지 않는다 — 진행 이력·
        progress·로그/카운터는 준비 객체에 스테이징되고 READY 이후에 적용된다.
        추출은 여기서 await하지 않는다(배리어 owner가 합류).

        Returns:
            성공 시 결과 dict, 묘사 생성 실패 시 None.
        """
        if preparation is None:
            preparation = core.turn_preparation.ensure_preparation(session, transaction_id)
        game_cog = self.bot.get_cog("GameCog")
        if not game_cog:
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send("⚠️ GameCog를 찾을 수 없어 자동 진행을 중단합니다.")
            return None

        master_ch = self.bot.get_channel(session.master_ch_id)
        # v7.35: 지시문 원문은 마스터 채널에 따로 보내지 않는다 — 층위활동 보고(🎬 분량)·!세션이력 호출 원문으로

        # NOTE: PROCEED 직전에 이번 턴 컨텍스트(NARRATE/ASK/ROLL 중간 기록)와 지시사항을 스냅샷.
        context_snapshot = list(session.current_turn_logs)

        # ③ 턴 연속성 강화(엔진 병행): 묘사 AI가 직전 턴을 되감지 않도록 지시에 연속성 directive 부착.
        exec_instruction = instruction + (
            "\n\n[연속성 지시] 직전 턴이 끝난 시점의 시간·공간·상태에서 곧바로 이어서 묘사할 것. "
            "이미 완료되었거나 서술된 행동·장면 전환·이동을 되풀이하거나 시간을 되감지 말고, "
            "지나간 장면에 인물의 행동·대사를 소급 삽입하지 말 것."
        )

        # H05 R3·정합: 묘사층위의 세계 제약(확정 결과·장소 고정·장면 인물)은 지시문에 붙이지 않고
        #   GameCog 묘사 프롬프트의 독립 블록(turn_world.narration_world_block)으로 준다.

        async def _on_finalized(narr):
            if preparation is not None:
                await self._launch_concurrent_preparation(session, preparation, narr, master_ch)

        result = await game_cog._execute_proceed(
            session, exec_instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX,
            transaction_id=transaction_id, preparation=preparation,
            on_finalized=_on_finalized,
        )

        # ── 묘사층위 실패 감지 (설계문서 6) ──
        # 확정 묘사가 없으면 묘사가 생성되지 않은 것이다. 묘사는 플레이어에게 직접
        # 노출되는 산출물이므로 실패 시 턴을 취소하고 선언 질문으로 재시작한다(기획 규정).
        narr = getattr(preparation, "narration", None) if preparation is not None else None
        full_text = getattr(narr, "text", "") or ""
        if not full_text:
            core.logs.event("turn", "묘사층위 출력 없음 — 턴 취소", level="ERROR", session=session,
                            data={"code": "NARRATION_EMPTY"})
            core.write_error_log(session.session_id, "narration",
                                 RuntimeError("묘사 출력 없음"), 1)
            game_ch = self.bot.get_channel(session.game_ch_id)
            last_decl = ""
            try:
                for c in reversed(session.raw_logs):
                    if getattr(c, "role", None) == "user":
                        last_decl = c.parts[0].text or ""
                        break
            except Exception:
                pass
            if game_ch:
                await core.message_lifecycle.send_transient(
                    self.bot, session, game_ch, core.build_failed_turn_notice(last_decl),
                    key=core.message_lifecycle.KEY_TURN_NOTICE)
            if master_ch:
                await master_ch.send("⚠️ 묘사층위 실패 — 턴을 취소하고 선언 질문으로 되돌립니다.")
            # 실패 턴의 잔재 제거. 플레이 로그에는 정상 선언 질문처럼 남는다.
            session.current_turn_logs = []
            session.gm_side_note = ""
            return None

        # PROCEED 완료 후 AI 출력(이력용). v7.25(CUT): 지시층위 이력에 다시 들어가므로 문자 수로 자르지 않는다.
        ai_summary = full_text

        # 이력 누적(최근 5개) · [방안 2] narrative_plan.current_event.progress 갱신
        #   — WP-C: 둘 다 턴 소유 서사 상태이므로 스테이징하고 READY 이후 적용한다.
        preparation.proceed_history_entry = {
            "instruction": instruction,
            "context": context_snapshot,
            "ai_summary": ai_summary,
        }
        preparation.narrative_progress = ai_summary
        return result


    # ─────────────────────────────────────────────────────────────
    # 추출층위 — 묘사 출력물에서 세계 상태·수치 추출
    # ─────────────────────────────────────────────────────────────

    async def _handle_open_time_input(self, session, message):
        """
        디스플레이 채널의 유지 시간 답변을 처리한다.

        기획 규정 — 모든 답변은 자체 재질문이나 유저의 재입력 선택이 아닌 경우에
        한 번만 하도록 즉시 채팅을 잠그고 UI로 진행한다.
        """
        try:
            await message.delete()
        except Exception:
            pass

        # v7.37(R1): 세션 첫 생성 대화 안이면 해석·재질문·확인을 같은 대화 메시지에 이어 쓴다
        flow = core.session_flow.open_in_flow(session, message.channel.id)
        if flow:
            echo = f"-# ↳ {getattr(message.author, 'display_name', None) or '플레이어'} · {message.content}\n"
            await core.session_flow.open_feedback(self.bot, session, message.channel,
                                                  echo + "말씀하신 시간을 헤아리고 있습니다…")
            resolved = await self.interpret_cache_time(session, message.content)
            await core.message_lifecycle.clear(self.bot, session, core.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT)
            if resolved.get("retry"):
                session.awaiting_display_input = True
                session._open_input_ch_id = message.channel.id
                await core.session_flow.open_feedback(
                    self.bot, session, message.channel,
                    echo + core.master.say("cache.time_unclear", "말씀하신 시간을 정확히 헤아리지 못했습니다. "
                                                                  "다시 한 번 말씀해 주십시오.")
                    + "\n" + core.session_flow.OPEN_EXAMPLES + _interp_note(resolved))
                return
            session.open_minutes = resolved["minutes"]
            try:
                est = core.estimate_session_open(session, resolved["minutes"] / 60)
            except Exception:
                est = {}
            view = OpenConfirmView(self.bot, session, resolved["minutes"],
                                   on_reopen=self.resume_held_declaration, flow=True)
            view.bind(await core.session_flow.open_feedback(
                self.bot, session, message.channel,
                echo + core.format_confirmation(resolved, est) + _interp_note(resolved), view))
            return

        thinking = await message.channel.send("⏱️ *입력을 확인하는 중…*")
        resolved = await self.interpret_cache_time(session, message.content)
        try:
            await thinking.delete()
        except Exception:
            pass

        # WP-F: 답변이 들어왔다 — 유지 시간 질문(INTERACTION_PROMPT)은 여기서 종결 정리한다.
        await core.message_lifecycle.clear(self.bot, session,
                                           core.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT)
        if resolved.get("retry"):
            # 재질문 — 이 경우에만 다시 언락한다(기획 규정). 재질문도 같은 키의 프롬프트다.
            session.awaiting_display_input = True
            core.message_lifecycle.register(
                session, core.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT,
                await message.channel.send(
                    "❓ " + core.master.say("cache.time_unclear", "입력을 이해하지 못했습니다. 다시 답해 주십시오.") + "\n"
                    "> 예: `3시간`, `90분`, `20턴`, `적당히`, `알아서`"
                    + _interp_note(resolved)
                ), cls=core.message_lifecycle.INTERACTION_PROMPT)
            return

        session.open_minutes = resolved["minutes"]
        try:
            est = core.estimate_session_open(session, resolved["minutes"] / 60)
        except Exception:
            est = {}
        view = OpenConfirmView(self.bot, session, resolved["minutes"],
                               on_reopen=self.resume_held_declaration)
        view.bind(await message.channel.send(
            core.format_confirmation(resolved, est) + _interp_note(resolved), view=view))

    async def interpret_cache_time(self, session, text: str) -> dict:
        """
        세션 유지 시간 입력을 해석한다 (저비용 호출).

        모델은 분류만 하고 실제 시간은 core.resolve_minutes가 정한다.
        해석 비용은 세션에 누적했다가 캐시 업로드 시점에 2잉크 이상일 때만
        청구한다(기획 규정).

        Returns:
            core.resolve_minutes의 결과 dict
        """
        quick = core.quick_parse(text)
        if quick is not None:                 # 숫자·단위가 분명한 답 — 코드가 읽는다(호출·해석 비용 0)
            resolved = core.resolve_minutes(quick)
            resolved["interpret_charge"] = {"ink": 0, "complete": True}
            return resolved
        config = types.GenerateContentConfig(
            system_instruction=CACHE_TIME_SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=CACHE_TIME_RESPONSE_SCHEMA,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(
            role="user", parts=[types.Part.from_text(text=f"[플레이어 입력]\n{text or ''}")]
        )]

        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_CACHE_TIME_INTERPRET, session=session,
            model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE,
            copy_transaction=False)
        # WP-F(POLICY-CACHE-01): 이 provider 사실은 interpretation_billing 이 청구한다 —
        #   표식·interp_id 로 CostEvent ↔ 청구 레코드를 감사·재구성할 수 있게 한다.
        _cl_op.metadata.update({"interpretation_billing": core.interpretation_billing.BILLING_MARK,
                                "interp_id": _cl_op.operation_id})
        ok, response = await core.call_with_retry(
            lambda: asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL, contents=contents, config=config,
            ),
            layer="media", session_id=session.session_id, retries=1,
            on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
        )
        if not ok:
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석에 실패했습니다."], "retry": True}

        # WP-F(POLICY-CACHE-01): provider 해석 사실 → durable 청구 후보 → (임계 이상이면) 즉시
        #   청구. 캐시 열기·취소·시간 만료·재시작과 무관하게 같은 결과다. 2잉크 미만 누적은
        #   기존대로 이어서 누적되고 세션을 열 때 면제된다.
        #   플레이어 청구는 durable provider 사실보다 앞설 수 없다 — 이 경로만 strict CostEvent
        #   (fsync, 멱등 replay 는 canonical identity)를 쓰고, 그 성공 뒤에만 청구 후보를 기록한다.
        interp_charge = {"ink": 0, "complete": True}
        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, _th = core.extract_token_usage(meta)
            _bd = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = _bd["total_krw"]
            _fact = _cl_op.record_fact_strict(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=_th,
                cost_usd=_bd["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
        except Exception as e:
            # strict provider 사실 없음 → 청구 0·청구 후보 없음·결과 미확정(재질문).
            #   실제 provider 비용은 시스템 영속 실패로 인한 운영 손실로 남는다.
            print(f"[시간해석] provider 비용 사실 strict 기록 실패 — 청구 없이 재질문: {e}")
            core.write_log(session.session_id, "error",
                           f"[시간해석] strict CostEvent 실패(op={_cl_op.operation_id}): "
                           f"{type(e).__name__}: {e}")
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석 결과를 확정하지 못했습니다(비용 기록 실패)."], "retry": True,
                    "interpret_charge": {"ink": 0, "complete": True}}
        try:
            core.interpretation_billing.record_interpretation(
                session, interp_id=_cl_op.operation_id, cost_krw=cost,
                cost_usd=_bd["total_usd"], cost_event_id=_fact.event_id)
        except Exception as e:
            print(f"[시간해석] 청구 후보 기록 실패(strict CostEvent 로 재구성·재시도): {e}")
        try:
            _st = await core.interpretation_billing.settle(self.bot, session)
            interp_charge = {"ink": _st["charged_ink"], "complete": _st["complete"]}
        except Exception as e:
            interp_charge = {"ink": 0, "complete": False}
            print(f"[시간해석] 청구 처리 실패(다음 해석·재시작 시 재시도): {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석 결과를 읽지 못했습니다."], "retry": True,
                    "interpret_charge": interp_charge}

        resolved = core.resolve_minutes(data)
        resolved["interpret_charge"] = interp_charge
        core.write_log(
            session.session_id, "api",
            f"[유지시간 해석] 입력={text[:60]} → {json.dumps(data, ensure_ascii=False)} "
            f"→ {resolved['minutes']}분"
        )
        return resolved

    async def _generate_npc_detail(self, session, name: str, recent_text: str,
                                   master_ch=None, *, preparation=None,
                                   entry: dict | None = None) -> bool:
        """
        비정규 NPC 세부 설정 생성 — 비중이 생기거나 이름이 부여되면 1회 호출.

        생성 후 session.npcs로 승격되어 정규 델타 주입 대상이 된다.
        목소리와 이미지는 배정 당시의 것을 유지한다.

        WP-C: preparation이 주어지면(자동 턴 자식 준비 작업) 정규화 결과를
        preparation.promotions에 스테이징만 하고 정본 승격은 READY 이후에 한다.
        entry는 이번 턴 등장 누적이 반영된 투영 항목이다.
        """
        if entry is None:
            entry = core.irregular_npc.get_registry(session).get(name)
        if not entry:
            return False

        # §38: provider-boundary 승격 오퍼레이션의 논리 시도 정체성을 호출 '전'에 고정.
        _tx_obj = core.turn_transaction.get_active_transaction(session)

        year = core.current_year(session)
        user_prompt = (
            f"[대상 인물]\n{name}\n\n"
            f"[등장 기록]\n"
            f"- 최초 등장: {entry.get('first_turn', 0)}턴\n"
            f"- 누적 등장: {entry.get('appearances', 1)}회\n"
            f"- 배정 인상: {entry.get('gender')}/{entry.get('age')}\n"
            f"- 등장 맥락: {entry.get('context') or '(없음)'}\n\n"
            f"[작중 현재 연도]\n{year if year is not None else '(미확인)'}\n\n"
            f"[최근 묘사문]\n{recent_text or ''}"
        )

        config = types.GenerateContentConfig(
            http_options=core.resilience.provider_http_options("media"),  # WP-C B-C4
            system_instruction=NPC_DETAIL_SYSTEM_INSTRUCTION,
            temperature=0.5,
            response_mime_type="application/json",
            response_schema=NPC_DETAIL_RESPONSE_SCHEMA,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_TURN_NPC_PROFILE, session=session,
            model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        ok, response = await core.call_with_retry(
            lambda: asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL, contents=contents, config=config,
            ),
            layer="media", session_id=session.session_id, retries=1,
            on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
        )
        if not ok:
            return False

        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, thought_t = core.extract_token_usage(meta)
            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=thought_t,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(
                session.session_id, f"{COST_LOG_PREFIX}NPC 설정 생성",
                in_t, cached_t, out_t, cost, session.total_cost
            )
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "NPC 설정 생성", "cost": cost,
                "in": in_t, "cached": cached_t, "out": out_t, "manifest": [],
            })
        except Exception as e:
            print(f"[NPC설정] 비용 정산 실패: {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            print("[NPC설정] 응답 파싱 실패")
            return False

        # result-only: raw payload를 promote에 직접 넣지 않고 정규화 후보로 만든다.
        norm = core.turn_preparation.normalize_npc_detail(data, fallback_name=name)

        # stale guard(§38): 이 승격 결과보다 더 새로운 논리 시도가 활성화됐으면 승격 금지.
        if core.turn_preparation.extraction_is_stale(
                session, logical_turn=getattr(_tx_obj, "logical_turn", None),
                attempt=getattr(_tx_obj, "attempt", None)):
            print(f"[NPC설정/{session.session_id}] stale 결과 거부 — 승격 보류")
            return False

        if preparation is not None:
            # WP-C: 스테이징만 — 정본 승격(mark_detailed/promote/개명)은 READY 이후.
            preparation.promotions[name] = norm
            return True
        return await self._apply_npc_promotion(session, name, norm, master_ch)

    async def _apply_npc_promotion(self, session, name, norm, master_ch=None,
                                   *, derived=None) -> bool:
        """LegacyCompatibilityApplier(NPC 승격) — 정규화된 세부설정만 소비한다.

        raw 모델 payload는 이 함수에 입력되지 않는다. mark_detailed/promote는 정규화된
        details만 받는다. promote는 멱등(이미 승격돼 등록부에 없으면 False)하다.
        """
        details = norm["details"]
        final_name = norm["final_name"]

        core.irregular_npc.mark_detailed(session, name)
        promoted = core.irregular_npc.promote(session, name, details)
        if promoted and final_name != name and not core.player_world.legacy_narrative_retired(session):
            # v7.25(F06): 동적 세계는 개명하지 않는다 — 키 = 플레이어가 본 이름(정본 ActorRegistry 표시 이름과 같다).
            #   모델이 지은 본명은 PC가 알게 된 사실이 아니다.
            # 고유명이 확정되면 키를 교체한다.
            npcs = dict(session.npcs)
            npcs[final_name] = npcs.pop(name)
            session.npcs = npcs

        if master_ch or derived is not None:
            age_txt = ""
            if details.get("birth_year"):
                age = core.compute_age(session, details["birth_year"])
                if age is not None:
                    age_txt = f" ({age}세)"
            _msg = (f"📇 **[NPC 승격]** {final_name}{age_txt} — "
                    f"{details['role']} · {details['attitude']}\n"
                    f"> {details['details'][:200]}")
            if derived is not None:
                derived.master(_msg)        # WP-D: COMMITTED 이후 방출
            else:
                await master_ch.send(_msg)
        return promoted

    async def _resolve_irregular_npcs(self, session, text: str, master_ch=None,
                                      *, preparation=None) -> int:
        """
        비정규 NPC 미디어 배정 — 묘사 스트리밍 '전'에 호출된다(기획 규정).

        [호출 생략 조건]
        이미지·TTS가 모두 꺼져 있으면 배정 결과를 쓸 곳이 없으므로 호출하지 않는다.
        이미지만 꺼져 있으면 목소리 결정을 위해 호출하되 image_key는 버린다.

        [동일인 유지]
        이전 배정 기록을 함께 전달해 같은 인물의 미디어가 세션 내내 유지되게 한다.

        WP-C: preparation이 주어지면(자동 턴 준비 작업) 검증된 등록 계획을
        preparation.irregular_plan에 스테이징하고 승격 세부 생성을 자식 작업으로
        등록한다. 정본(irregular_npcs/npcs)은 READY 이후에만 바뀐다. 스트리밍 중 화자
        이미지는 core.irregular_npc의 읽기 전용 투영으로 이번 배정을 반영한다.

        Returns:
            새로 배정된(또는 스테이징된) 인물 수
        """
        use_image = core.is_enabled(session, "image")
        use_tts = core.is_enabled(session, "tts")
        if not (use_image or use_tts):
            return 0

        names = core.irregular_npc.extract_candidate_names(text, session)
        if not names:
            return 0

        # §38: provider-boundary 오퍼레이션의 논리 시도 정체성을 호출 '전'에 고정해
        #      적용 직전 stale guard에 쓴다(더 새로운 턴이 시작되면 등록/승격 금지).
        _tx_obj = core.turn_transaction.get_active_transaction(session)

        pool = core.irregular_npc.irregular_image_pool(session)
        if not pool:
            # 이미지 후보가 없으면 목소리만 결정한다. image_key는 빈 값으로 응답하도록
            # 후보 목록에 자리표시자를 넣지 않고, 아래에서 결과를 무시한다.
            pool = ["(없음)"]

        prev = core.irregular_npc.get_registry(session)
        prev_lines = [
            f"- {n}: {e.get('gender')}/{e.get('age')}, 이미지 {e.get('image_key') or '(없음)'}"
            for n, e in prev.items()          # v7.25: 앞 10명(가장 오래된 등록)만 주던 선별 제거 — 일관성 기준 전부
        ]

        user_prompt = (
            f"[이번 묘사문]\n{text or ''}\n\n"
            f"[배정 대상 인물]\n" + "\n".join(f"- {n}" for n in names) + "\n\n"
            f"[이미지 후보 목록]\n" + ", ".join(pool) + "\n\n"
            f"[이전 배정 기록]\n" + ("\n".join(prev_lines) if prev_lines else "(없음)")
        )

        config = types.GenerateContentConfig(
            http_options=core.resilience.provider_http_options("media"),  # WP-C B-C4
            system_instruction=IRREGULAR_NPC_SYSTEM_INSTRUCTION,
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=IRREGULAR_NPC_RESPONSE_SCHEMA,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_TURN_IRREGULAR_NPC, session=session,
            model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        try:
            _ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL, contents=contents, config=config,
                ),
                layer="media", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                return 0
        except Exception as e:
            print(f"[비정규NPC] 호출 실패(진행에는 영향 없음): {type(e).__name__} - {e}")
            return 0

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, thought_t = core.extract_token_usage(meta)
            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=thought_t,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(
                session.session_id, f"{COST_LOG_PREFIX}비정규 NPC 배정",
                in_t, cached_t, out_t, cost, session.total_cost
            )
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "비정규 NPC 배정", "cost": cost,
                "in": in_t, "cached": cached_t, "out": out_t, "manifest": [],
            })
        except Exception as e:
            print(f"[비정규NPC] 비용 정산 실패: {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            print("[비정규NPC] 응답 파싱 실패")
            return 0

        # ══════════════════════════════════════════════════════════════
        #  비정규 NPC 미디어 배정 — 계획-권위 적용 경계
        #  위쪽(provider 호출 + 파싱)은 result-only: canonical 미변경.
        #  raw payload는 build_irregular_npc_plan에서 검증·정규화된 등록 계획으로만
        #  소비되고, 이후 어떤 canonical mutator(register)의 입력도 되지 않는다.
        # ══════════════════════════════════════════════════════════════
        valid_pool = set(core.irregular_npc.irregular_image_pool(session))
        plan = core.turn_preparation.build_irregular_npc_plan(
            session, data, names=names, valid_pool=valid_pool, use_image=use_image,
            text=text,
            turn=(preparation.staged_log["turn_no"]
                  if preparation is not None and isinstance(preparation.staged_log, dict)
                  else getattr(session, "turn_count", 0)),
            transaction_id=getattr(_tx_obj, "transaction_id", None),
            logical_turn=getattr(_tx_obj, "logical_turn", None),
            attempt=getattr(_tx_obj, "attempt", None))

        # stale guard(§26/§38): 이 결과의 논리 시도보다 더 새로운 시도가 활성화됐으면
        # canonical NPC 등록/승격을 하지 않는다.
        if core.turn_preparation.extraction_is_stale(
                session, logical_turn=getattr(_tx_obj, "logical_turn", None),
                attempt=getattr(_tx_obj, "attempt", None)):
            plan.rejected_stale = True
            print(f"[비정규NPC/{session.session_id}] stale 결과 거부 — 더 새로운 논리 시도 활성")
            return 0

        if preparation is not None:
            preparation.irregular_plan = plan
            preparation.irregular_text = text
            self._stage_npc_promotions(session, preparation, plan, text, master_ch)
            return len(plan.registrations)
        return await self._apply_irregular_npc_plan(session, plan, text, master_ch)

    def _stage_npc_promotions(self, session, prep, plan, text, master_ch=None) -> list:
        """WP-C: 승격 판정을 투영으로 재현하고 세부 생성을 자식 준비 작업으로 등록한다.

        기존 _apply_irregular_npc_plan의 순서(등록 → 등장 누적 → should_promote → 세부
        생성)를 정본 변경 없이 투영 등록부에서 계산한다. 자식은 부모(배정) 작업이
        끝나기 전에 등록되므로 배리어 합류가 누락하지 않는다.
        """
        TP = core.turn_preparation
        IRR = core.irregular_npc
        turn = plan.registrations[0]["turn"] if plan.registrations else (
            prep.staged_log["turn_no"] if isinstance(prep.staged_log, dict)
            else getattr(session, "turn_count", 0))
        reg = copy.deepcopy(IRR.get_registry(session))
        for r in plan.registrations:
            if r["name"] not in reg:
                reg[r["name"]] = TP.staged_irregular_entry(session, r["name"]) or {}
        launched = []
        for name in list(reg.keys()):
            if name not in (text or ""):
                continue
            entry = reg[name]
            seen = list(entry.get("seen_turns") or [])
            if turn not in seen:
                seen.append(turn)
                entry["seen_turns"] = seen[-20:]
                entry["appearances"] = len(seen)
            if entry.get("detailed"):
                continue
            if entry.get("appearances", 0) < IRR.PROMOTE_APPEARANCES:
                continue
            prep.register_task(
                f"npc_promotion:{name}", TP.TASK_NPC_PROMOTION,
                coro=self._generate_npc_detail(
                    session, name, text, master_ch, preparation=prep, entry=dict(entry)),
                parent="irregular_npc")
            launched.append(name)
        return launched

    async def _apply_irregular_npc_plan(self, session, plan, text, master_ch=None,
                                        *, staged_promotions=None, derived=None) -> int:
        """LegacyCompatibilityApplier(비정규 NPC 등록) — 계획의 정규화 항목만 적용한다.

        raw 모델 payload는 이 함수에 입력되지 않는다. register는 계획의 정규화된
        등록 항목만 소비하며 멱등(이미 있으면 기존 항목 반환)하다. 등장 누적·승격
        판정은 코드 파생(이름이 묘사에 등장)이며, 승격 세부설정 생성은 별도 provider
        오퍼레이션(_generate_npc_detail)이 자체 정규화 경계를 거친다.
        """
        added = 0
        for reg in plan.registrations:
            core.irregular_npc.register(
                session, reg["name"], image_key=reg["image_key"],
                gender=reg["gender"], age=reg["age"],
                context=reg["context"], turn=reg["turn"])
            added += 1

        # 등장 누적 및 승격 판정 — 비중이 생긴 인물의 설정을 생성한다(코드 파생).
        try:
            turn = getattr(session, "turn_count", 0)
            for name in list(core.irregular_npc.get_registry(session).keys()):
                if name not in (text or ""):
                    continue
                core.irregular_npc.note_appearance(session, name, turn)
                if staged_promotions is not None:
                    # WP-C READY 이후: 사전 READY에 준비된 승격만 적용(provider 호출 없음).
                    if name in staged_promotions:
                        await self._apply_npc_promotion(
                            session, name, staged_promotions[name], master_ch,
                            derived=derived)
                elif core.irregular_npc.should_promote(session, name):
                    await self._generate_npc_detail(session, name, text, master_ch)
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[NPC설정] 승격 판정 실패(진행에는 영향 없음): {e}")

        if added and (master_ch or derived is not None):
            reg = core.irregular_npc.get_registry(session)
            names = [r["name"] for r in plan.registrations]
            lines = [f"{n} — {reg[n]['gender']}/{reg[n]['age']}"
                     f"{', ' + reg[n]['image_key'] if reg[n].get('image_key') else ''}"
                     for n in names if n in reg]
            _msg = "🎭 **[비정규 NPC 배정]** " + " · ".join(lines)
            if derived is not None:
                derived.master(_msg)
            else:
                await master_ch.send(_msg)
        plan.applied = True
        return added

    async def _apply_extraction_plan(self, session, plan, master_ch=None,
                                     *, derived=None) -> dict:
        """LegacyCompatibilityApplier — 추출 변이 계획(정규화 typed 필드)만 적용한다.

        raw provider result는 이 함수에 **입력되지 않으며** 어떤 canonical mutator의
        입력도 되지 않는다. 검증 탈락·dedup·conflict로 계획에서 빠진 후보는 여기에
        도달할 수 없다(되살아남 불가). 기존 도메인 helper는 재사용하되 입력은 계획에서
        파생한 정규화 DTO(또는 좁은 구조)뿐이다. plan.result는 증거 스냅샷·로그용일 뿐
        mutator 입력이 아니다.

        WP-D: derived(core.commit_coordinator.DerivedEffects)가 주어지면(권위적 커밋의
        영속 전 적용) Discord 송신·통계 쓰기를 하지 않고 기술자만 쌓는다 — 정본 변이만
        메모리에서 일어나며 파생 출력은 durable COMMITTED 이후 방출된다.
        """
        async def _master(content):
            if derived is not None:
                derived.master(content)
            elif master_ch:
                await master_ch.send(content)

        async def _game(content, view_factory=None):
            if derived is not None:
                derived.game(content, view_factory=view_factory)
                return
            ch = self.bot.get_channel(session.game_ch_id)
            if ch:
                if view_factory is not None:
                    await ch.send(content, view=view_factory())
                else:
                    await ch.send(content)

        # ── 세계 타임라인 / 장소(정규화된 이름으로만) ──
        if plan.location_after:
            try:
                if core.places.mark_visited(session, plan.location_after):
                    print(f"[장소] 첫 방문: {plan.location_after}")
                if plan.location_moved and not core.player_world.legacy_narrative_retired(session):
                    # H07-C: 정적 상주(places[*].npcs) 기반 동행 해제는 LEGACY만 — 연고는 현재 위치가 아니다
                    gone = core.release_resident_companions(session, plan.location_after)
                    if gone:
                        await _master(
                            f"👋 **[동행]** {', '.join(gone)}이(가) 자리에 남았습니다.")
            except Exception as e:
                if derived is not None:
                    raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                print(f"[장소] 적용 실패: {e}")
        if plan.world_new_tl is not None:
            session.world_timeline = core.quantify(session, plan.world_new_tl)

        # 증거 스냅샷(참조·로그용) + 추출 차단 해제.
        session.last_extraction = plan.result
        session.extraction_pending = False
        session.extraction_retry_ctx = {}

        # ── 동행 (정규화 DTO만) ──
        try:
            comp = core.apply_normalized_companions(session, {
                "joined": plan.companions_joined,
                "left": plan.companions_left,
                "npcs_met": plan.npcs_met})
            # v7.35: 동행 변화는 세계 델타 보고(🧍 동행)가 싣는다
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[동행] 갱신 실패: {e}")

        # ── 상태이상 / 소지품 (정규화 DTO만; 임계·검증은 이미 계획 빌드에서 끝남) ──
        _ip = getattr(plan, "item_plan", None)
        applied = core.apply_normalized_status_item(session, {
            "status_apply": plan.status_apply,
            "status_clear": plan.status_clear,
            "item_deltas": [] if _ip is not None else plan.item_deltas,
            "npcs": plan.npcs_met})
        if _ip is not None:
            # v7.17 DYNAMIC: 검증된 아이템 계획을 같은 커밋에서 다시 검증·적용(불일치는 커밋 owner로 전파 — 부분 적용 없음)
            applied["items"] = core.items.apply_plan(session, _ip)

        # ── 퀘스트 진전 / 이면정보 / 메인 해금 / 엔딩 (좁은 정규화 입력만) ──
        moved = None
        if core.player_world.legacy_narrative_retired(session):
            # H07-C: 동적 세계 세션 — 레거시 퀘스트 진전·이면 인지·메인 해금·엔딩은 은퇴(사건 상태기계가 권위).
            #        소속은 사건 결과에서 파생한 읽기 전용 호환 뷰(world_player_faction)만 반영한다.
            if getattr(plan, "world_player_faction", None):
                if session.player_faction != plan.world_player_faction:
                    await _game(core.master.lead("turn.faction_notice",
                                                 f"🏛️ **{plan.world_player_faction}**의 일원이 되었습니다."))
                session.player_faction = plan.world_player_faction
        if not core.player_world.legacy_narrative_retired(session):
            try:
                if core.quest.check_secret_awareness(
                        session, {"secret_awareness": plan.secret_awareness}):
                    print(f"[GM/{session.session_id}] 이면정보 인지됨")
                    await _master("🔓 **[퀘스트]** 플레이어가 숨겨진 사실을 알아챘습니다.")

                try:
                    mains = core.quest.check_main_unlock(session)
                    if mains and not getattr(session, "main_unlocked_notified", False):
                        session.main_unlocked_notified = True
                        await _master(
                            f"🗝️ **[퀘스트]** 메인라인 조건 충족 — "
                            f"{', '.join(q['name'] for q in mains)}")
                except Exception as e:
                    if derived is not None:
                        raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                    print(f"[퀘스트] 메인 해금 확인 실패: {e}")

                moved = core.quest.advance_quest(
                    session, {"quest_progress": plan.quest_progress})
                if moved and moved.get("moved"):
                    outcome = moved.get("outcome")
                    print(f"[GM/{session.session_id}] 퀘스트 진전 → {moved['node']}"
                          + (f" (완료: {outcome})" if outcome else ""))
                    if outcome:
                        await _master(f"📜 **[퀘스트]** 완료 — {outcome}")
                    if moved.get("granted"):
                        g = moved["granted"]
                        if g.get("faction"):
                            print(f"[GM/{session.session_id}] 소속 획득: {g['faction']}")
                            await _game(core.master.lead("turn.faction_notice",
                                                         f"🏛️ **{g['faction']}**의 일원이 되었습니다."))
                    if core.quest.is_ending(outcome):
                        session.pending_ending = outcome
                        await _game(
                            f"🎬 **세션 엔딩에 도달했습니다.**\n"
                            f"> 결말: {outcome}\n\n"
                            + core.quest.format_plans(),
                            view_factory=lambda: InfinityPlanView(self.bot, session))
            except Exception as e:
                if derived is not None:
                    raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                print(f"[퀘스트] 진전 실패(진행에는 영향 없음): {e}")
        if applied["applied"] or applied["cleared"]:
            print(f"[GM/{session.session_id}] 상태 적용: "
                  f"부여={applied['applied']} 해제={applied['cleared']}")

        # ── BGM (정규화 situation) ──
        try:
            track = core.select_bgm(session, plan.situation or {})
            if track:
                session.pending_bgm = track
                print(f"[GM/{session.session_id}] BGM 전환 예정: {track}")
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[BGM] 선택 실패(진행에는 영향 없음): {e}")

        # ── 통계 ── (WP-D: 권위 커밋에서는 COMMITTED 이후 파생 방출)
        try:
            for uid in (session.players or {}):
                if derived is not None:
                    derived.stats(uid, turns=1,
                                  status_applied=len(applied["applied"]),
                                  status_cleared=len(applied["cleared"]))
                    derived.npcs(uid, applied["npcs"])
                    continue
                await core.stats.bump(
                    uid, turns=1,
                    status_applied=len(applied["applied"]),
                    status_cleared=len(applied["cleared"]))
                await core.stats.add_npcs(uid, applied["npcs"])
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[통계] 누적 실패(진행에는 영향 없음): {e}")

        # 로그(증거) + 계획 충돌 진단.
        core.write_log(
            session.session_id, "api",
            f"[추출층위 결과]\n{json.dumps(plan.result, ensure_ascii=False, indent=2)}")
        if plan.conflicts:
            print(f"[추출/{session.session_id}] 계획 충돌 진단(적용 제외): {plan.conflicts}")

        # 마스터 보고 — 계획·적용 결과에서 구성(raw result는 mutator에 안 들어감).
        # v7.35: 동적 세계는 추출의 위치가 권위가 아니다 — 추출 요약은 층위활동 보고(🔎)·세계 델타(🎒·🩹)로만
        if (master_ch or derived is not None) and not getattr(session, "definition_revision", None):
            loc = (plan.world_new_tl or {}).get("current_location") or "미확인"
            report = f"🔎 **[추출층위]** 위치={loc}"
            if applied["applied"]:
                report += f"\n> 상태 부여: {', '.join(applied['applied'])}"
            if applied["cleared"]:
                report += f"\n> 상태 해제: {', '.join(applied['cleared'])}"
            if applied.get("items"):
                shown = [f"{i['target']} {i['item']} "
                         f"{'+' if i['delta'] > 0 else ''}{i['delta']} (→{i['after']})"
                         for i in applied["items"][:6]]
                report += f"\n> 소지품 반영: {', '.join(shown)}"
            if plan.companions_joined:
                report += f"\n> 동행 합류: {', '.join(plan.companions_joined)}"
            await _master(report)

        plan.applied = True
        return {"applied": applied, "moved": moved}

    async def _run_extraction(self, session, ai_output_text: str, master_ch=None,
                              *, transaction_id: str | None = None,
                              logical_turn: int | None = None,
                              attempt: int | None = None,
                              preparation=None, correction: str | None = None):
        """
        추출층위 — 묘사 출력물에서 공통·시나리오별 타겟 값을 추출한다.

        v7.29 correction: 직전 추출의 물건 연산이 정본 검증에서 거부됐을 때 그 사유를 덧붙여 같은 턴에 한 번 더
        추출한다(같은 묘사·같은 후보 — 사유만 추가).

        [동시 실행]
        묘사 스트리밍과 병행되므로, 실패해도 이미 출력된 묘사를 되돌릴 수 없다.
        따라서 일반적인 턴 취소 규정을 적용하지 않고 추출만 재시도한다.
        재시도까지 실패하면 session.extraction_pending을 세워 다음 턴을 차단하고,
        게임 채널에 재시도 버튼을 배치한다.

        [캐시 미사용] 출력물 판독에 룰북이 불필요하므로 캐시를 읽지 않는다.

        [WP-C 준비 모드] preparation이 주어지면(자동 턴 등록 준비 작업) result-only로
        동작한다: 검증·정규화된 ExtractionMutationPlan을 preparation.extraction_plan에
        저장해 반환하고, 정본 적용·extraction_pending/UI는 하지 않는다(배리어 owner 소관).
        프롬프트·계획 입력은 기존 흐름과 같은 의미(지시효과 quest 투영, 이번 턴 비정규
        등록/승격 NPC)를 읽기 전용 투영 뷰로 본다.

        WP-G(RE-GATE): 준비 객체가 필수다. preparation=None 호출은 provider 호출 전에 거부된다 —
        추출 결과를 정본에 직접 적용하던 비준비 경로(WP-B 호환 경계)는 은퇴했고, 정본 적용은
        READY 이후 CommitCoordinator 경로(_apply_prepared_extraction)만 수행한다.
        """
        if preparation is None:
            raise RuntimeError(
                "추출층위는 자동 턴 준비 객체(preparation) 없이 실행할 수 없습니다 "
                "(비준비 추출 → 정본 직접 적용 경로는 WP-G에서 은퇴)")
        view = session
        if preparation is not None:
            _pend = core.turn_preparation.pending_for(session)
            _qproj = (_pend.projected_quest_state
                      if _pend is not None and _pend.projected_quest_state is not None
                      else copy.deepcopy(getattr(session, "quest_state", None)))
            view = core.turn_preparation.PreparationView(session, overrides={
                "quest_state": _qproj,
                "irregular_npcs": core.turn_preparation.projected_irregular_registry(
                    session, preparation),
                "npcs": core.turn_preparation.projected_npcs(session, preparation),
            })
        targets = core.build_extraction_targets(view)
        prev_tl = getattr(session, "world_timeline", {}) or {}
        prev_summary = (
            f"위치={prev_tl.get('current_location', '미확인')}, "
            f"시간대={prev_tl.get('time_of_day', '미확인')}, "
            f"날짜={prev_tl.get('current_date', '미확인')}"
        ) if prev_tl else "(없음)"

        # 이면정보 인지 판정을 위해 '인지 조건'만 주입한다.
        # 이면정보 본문(truth)은 넣지 않는다 — 추출층위가 알면 판정이 오염된다.
        secret_block = ""
        try:
            # H07-C: 동적 세계 세션의 이면정보 인지는 정보 노출(관찰 매니페스트)만 — 레거시 인지 조건 없음
            st = {} if core.player_world.legacy_narrative_retired(session) else core.quest.get_state(view)
            active = st.get("active")
            if active and not active.get("secret_known"):
                q = core.quest._find_quest(view, active["id"])
                conds = ((q or {}).get("hidden") or {}).get("reveal_conditions") or []
                if conds:
                    secret_block = (
                        "\n[숨겨진 사실의 인지 조건 — 아래에 해당하는 정황이 묘사에 있었는지 평가]\n"
                        + "\n".join(f"- {c}" for c in conds) + "\n"
                    )
        except Exception as e:
            print(f"[추출] 인지 조건 주입 실패: {e}")

        # 유효 목록을 주입한다. 코드가 걸러내기는 하지만, 목록을 주지 않으면
        # 매번 없는 상태이상을 만들고 인물 아닌 것을 npcs_met에 넣는다.
        limit_block = core.build_extraction_limits(view)

        # WP-B(AUD-011): 추출 증거에는 완결된 전체 묘사가 들어가야 한다.
        #   추출 모델(Gemini)의 컨텍스트 한도는 묘사 길이(수천 자)를 크게 상회하므로
        #   청킹 없이 전문을 전달한다. 요약·앞부분 절단·꼬리 드롭을 하지 않는다.
        _wt = getattr(preparation, "world_turn", None)
        _ext_schema = core.EXTRACTION_RESPONSE_SCHEMA
        _obs_block = ""
        _var_manifest = {}
        _rev_manifest = {}
        if _wt is not None:
            # H05-B: 관찰 후보(장면 행위자가 아는 정보·장면 신호·선후 선택지)만 — 숨은 진실 미포함.
            # v7.14: 이번 턴 발견(게이트 승인·발견 굴림 성공)만 DNEW로 — 판단 기록에서 고정
            _disc = _discovery_outcome(self._tx_judgment(session, transaction_id), transaction_id)
            _wt.discovery = _disc if _disc and _disc.get("status") == "NEW" else None
            _wt.obs_manifest = core.turn_world.build_observation_manifest(
                _wt.ctx.definition, _wt.ctx, _wt.plan, session, discovery=_wt.discovery,
                pending=core.turn_world.pending_signals(session))     # P1: 전달 대기 신호도 표현 근거 후보
            _ext_schema = copy.deepcopy(core.EXTRACTION_RESPONSE_SCHEMA)
            # v7.25(F10): 동적 세계에서 적용되지 않는 레거시 필드는 묻지 않는다 — 장소·날짜는 정본 세계, 퀘스트 진행·
            #   이면정보 인지는 사건 상태기계·정보 인지가 소유한다(파서가 기본값으로 채워 하류 호환은 유지)
            for _k in core.extraction.DYNAMIC_RETIRED_FIELDS:
                _ext_schema["properties"].pop(_k, None)
            _ext_schema["required"] = [k for k in _ext_schema.get("required", [])
                                       if k not in core.extraction.DYNAMIC_RETIRED_FIELDS]
            targets = [t for t in targets if t not in core.extraction.DYNAMIC_RETIRED_TARGETS]
            _ext_schema.setdefault("properties", {})[core.turn_world.OBS_FIELD] = _wt.obs_manifest.schema()
            _obs_block = _wt.obs_manifest.render()
            # v7.17 아이템: 같은 추출 호출의 런타임 확장(새 provider 호출 0) — 소유자·소지·현장 물건 키만
            _wt.item_manifest = self._item_manifest(session, _wt)
            _ext_schema["properties"][core.turn_world.OBS_FIELD]["properties"][core.items.OPS_FIELD] = \
                _wt.item_manifest.schema()
            _obs_block += _wt.item_manifest.render()
            # SVG 카드: 장면 카드 인물별 허용 바리에이션 — 인물당 턴 하나(턴 로컬, 정본 저장 없음)
            _cw = core.speaker_card.card_world(session)
            _var_manifest = core.speaker_card.variation_manifest(_cw, _wt, session) if _cw else {}
            if _var_manifest:
                _ext_schema["properties"]["speaker_variations"] = core.speaker_card.variation_schema(_var_manifest)
                _obs_block += core.speaker_card.variation_block(_var_manifest, _cw.definition, session)
            # P3 G3: 이번 턴 이름·얼굴 식별 후보의 실제 공개 문장(같은 추출 호출 — 새 provider 호출 0)
            _rev_manifest = core.speaker_card.reveal_manifest(_cw, _wt, session) if _cw else {}
            if _rev_manifest:
                _ext_schema["properties"][core.speaker_card.REVEAL_FIELD] = \
                    core.speaker_card.reveal_schema(_rev_manifest)
                _obs_block += core.speaker_card.reveal_block(_rev_manifest, _cw.definition, session)
            # 장소·시간 권위는 정본 세계다 — 레거시 요약 대신 정본 미러를 보여 준다.
            _tl = core.turn_world.mirror_timeline(prev_tl, _wt.ctx.definition,
                                                  _wt.plan.projected_world if _wt.plan is not None
                                                  and _wt.plan.projected_world is not None else session,
                                                  _wt.ctx.lead_pc)
            prev_summary = (f"위치={_tl.get('current_location_display') or _tl.get('current_location', '미확인')}, "
                            f"시간대={_tl.get('time_of_day', '미확인')}, 날짜={_tl.get('current_date', '미확인')} "
                            f"(코드 확정 정본)")
        user_prompt = (
            "[추출 항목]\n" + "\n".join(f"- {t}" for t in targets) + "\n"
            + limit_block + secret_block + _obs_block + "\n"
            f"[직전까지의 세계 상태]\n{prev_summary}\n\n"
            f"[이번 묘사문]\n{ai_output_text}"
        )
        if correction:
            user_prompt += ("\n\n[직전 추출의 물건 연산이 정본 검증에서 거부됨 — 바로잡을 것]\n"
                            f"{correction}\n"
                            "· 위 사유를 고쳐 다시 추출하십시오. 키는 [물건 — item_ops]의 후보만, 묘사에 실제로 일어난 "
                            "변화만 적고, 맞는 키가 없으면 그 연산은 적지 마십시오.")

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            http_options=core.resilience.provider_http_options("extraction"),  # WP-C B-C4
            system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_ext_schema,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
            # 추출은 묘사문에서 값을 읽어 옮기는 기계적 작업이다.
            # 실측 결과 출력의 90%가 사고 토큰이었고(1039/1148) 실제 JSON은
            # 109토큰에 불과했다. 사고 예산을 제한해 낭비를 줄인다.
            thinking_config=types.ThinkingConfig(thinking_budget=EXTRACTION_THINKING_BUDGET),
        )

        result = None
        # WP-02: 외부 추출 재시도 전체가 하나의 논리 오퍼레이션(operation_id 공유).
        _cl_op = core.cost_ledger.begin_operation(
            self.bot, core.cost_ledger.OP_TURN_EXTRACTION, session=session,
            model=EXTRACTION_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
            billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        # WP-C(F1): 재시도 루프 변수가 트랜잭션 attempt 매개변수를 가리던 결함 수정.
        #   (기존: for attempt in ... → stale 판정·계획 정체성이 루프 인덱스로 오염)
        for _try in range(EXTRACTION_MAX_RETRIES):
            ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=EXTRACTION_MODEL, contents=contents, config=config,
                ),
                layer="extraction", session_id=session.session_id, retries=1,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not ok:
                continue

            try:
                meta = response.usage_metadata
                in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
                breakdown = core.calculate_text_gen_cost_breakdown(
                    EXTRACTION_MODEL, input_tokens=in_tokens,
                    output_tokens=out_tokens, cached_read_tokens=cached_tokens,
                )
                cost = breakdown["total_krw"]
                core.accrue(session, cost, breakdown["total_usd"])
                _cl_op.record(
                    input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                    output_tokens=out_tokens, thought_tokens=thought_tokens,
                    cost_usd=breakdown["total_usd"], cost_krw=cost,
                    usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
                core.update_stats(session, "extraction", out_tokens, thought_tokens)
                core.write_cost_log(
                    session.session_id, f"{COST_LOG_PREFIX}추출층위 호출",
                    in_tokens, cached_tokens, out_tokens, cost, session.total_cost
                )
                if not hasattr(session, "turn_cost_log"):
                    session.turn_cost_log = []
                session.turn_cost_log.append({
                    "label": "추출층위", "cost": cost,
                    "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                    "manifest": [], "layer": "extraction",
                    "census": core.prompt_census.record(
                        session, "extraction", contents, system_text=_sys_text(config),
                        in_tokens=in_tokens, cached_tokens=cached_tokens),
                })
            except Exception as e:
                print(f"[GM] 추출층위 비용 정산 실패: {e}")

            result = core.parse_extraction(response.text or "")
            if result:
                break
            print(f"[GM] 추출층위 응답 파싱 실패(시도 {_try + 1})")

        if not result:
            # WP-C: 실패 처리(재시도 UI·차단)는 배리어 owner가 담당한다.
            return None

        # 검증·정규화 계획 — ExtractionMutationPlan이 적용의 유일 권위다. raw result는 어떤
        #   canonical mutator의 입력도 되지 않는다. 여기서는 스테이징만 하며 정본 적용은
        #   READY 이후 CommitCoordinator 경로(_apply_prepared_extraction)가 수행한다.
        plan = core.turn_preparation.build_extraction_plan(
            view, result, transaction_id=transaction_id,
            logical_turn=logical_turn, attempt=attempt)
        # stale이면 현재 배리어를 만족 못한다(정본 미변경).
        if (not preparation.matches(core.turn_transaction.get_active_transaction(session))
                or core.turn_preparation.extraction_is_stale(
                    session, logical_turn=logical_turn, attempt=attempt)):
            plan.rejected_stale = True
            print(f"[추출/{session.session_id}] stale 준비 결과 — 배리어 미충족 "
                  f"(tx={transaction_id}, lt={logical_turn}, at={attempt})")
            return None
        preparation.extraction_plan = plan
        preparation.world_observation_raw = result.get(core.turn_world.OBS_FIELD) if _wt is not None else None
        if _wt is not None and _var_manifest:
            preparation.card_variations = core.speaker_card.parse_variations(
                _var_manifest, result.get("speaker_variations"))
        if _wt is not None and _rev_manifest:
            _narr = getattr(preparation, "narration", None)
            _paras = getattr(_narr, "paragraphs", ()) or ()
            _rev = core.speaker_card.parse_reveals(
                _rev_manifest, result.get(core.speaker_card.REVEAL_FIELD), _paras, _cw.definition, session)
            for _iid, _ix in core.speaker_card.implied_name_reveals(
                    _rev_manifest, _paras, _cw.definition, session).items():   # v7.35(U1-b): 대사 속 이름
                _rev[_iid] = min(_rev.get(_iid, _ix), _ix)
            preparation.identity_reveals = _rev
        return plan

    async def _simulate_narrative_directions(self, session, player_message: str,
                                              master_ch,
                                              *, transaction_id: str | None = None,
                                              gate_inputs: dict | None = None) -> dict | None:
        """
        지시층위 호출 전 세계관 캐시를 활용하여 고차원 서사 방향성을 사전 시뮬레이션.

        [목적]
        단순 사건 추론(소리가 났으니 몬스터가 온다)이 아닌, 시나리오 세계관의 세력 배치·
        지역 규칙·물리 법칙에 근거한 구조적 개연성 판단 결과를 지시층위에 제공한다.

        [활성화 조건]
        - 세션 캐시 유효 (없으면 세계관 문맥 없어 고차원 판단 불가)
        - 캐시 모델이 DEFAULT_MODEL과 일치

        Returns:
            dict | None: {"world_state_analysis": str, "directions": [...]} 또는 None
        """
        cache_name = getattr(session, "cache_name", None)
        if not core.turn_history.cache_usable(session):     # WP-E(E-E3)
            return None

        # 최근 로그 (턴 개수 4개 유지, 각 턴은 온전 원문)
        recent_lines = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                recent_lines.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_lines) if recent_lines else "(없음)"

        # 세계 물리 타임라인 요약 (있으면)
        tl = getattr(session, "world_timeline", {})
        tl_note = ""
        if tl:
            tl_note = (
                f"\n[현재 세계 상태]\n"
                f"위치: {tl.get('current_location', '미확인')}\n"
                f"시간대: {tl.get('time_of_day', '미확인')}\n"
                f"세력: {tl.get('faction_context', '미확인')}\n"
            )

        # v7.14 선언 게이트 입력 — PC 지식 범위(이 장소의 유효 발견·찾지 못한 이전 탐색)만
        gate_note = ""
        gi = gate_inputs or {}
        if gi.get("dynamic") and gi.get("place"):
            rows = gi.get("rows") or []
            failed = gi.get("failed") or []
            gate_note = (
                "\n[이 장소에서 PC가 이미 발견한 것]\n"
                + (core.turn_world.discovery_rows_text(rows) if rows else "(없음)")
                + "\n[이 장소에서 찾지 못한 이전 탐색 — 실패는 부재가 아니다]\n"
                + ("\n".join(f"· {f['target']} — 당시 선언: {f['declaration']}" for f in failed) if failed else "(없음)")
                + "\n")
        elif not gi.get("dynamic"):
            gate_note = "\n[세션 형식] 이 세션은 주변 발견을 영속하지 않는다 — 발견 제안은 일반 행동(ORDINARY)으로 분류한다.\n"

        user_prompt = (
            f"[현재 턴]: {session.turn_count + 1}\n"
            f"{tl_note}"
            f"\n[최근 게임 로그]\n{recent_str}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n"
            f"{gate_note}\n"
            "[지시사항]\n"
            "위 상황에서 다음에 일어날 수 있는 서사 방향성 2~3개를 세계관 논리에 근거하여 평가하십시오.\n"
            "반드시 시나리오 캐시의 세력 정보·지역 규칙·세계관 설정을 인용하여 판단하십시오.\n"
            "이번 플레이어 선언을 [선언 게이트 규칙]에 따라 declaration_gate로 판정하십시오."
        )

        try:
            sim_contents = [
                types.Content(role="user",
                              parts=[types.Part.from_text(text=NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION)]),
                types.Content(role="model",
                              parts=[types.Part.from_text(
                                  text="이해했습니다. 세계관 논리에 근거하여 서사 방향성을 분석하겠습니다.")]),
                types.Content(role="user",
                              parts=[types.Part.from_text(text=user_prompt)]),
            ]
            config = types.GenerateContentConfig(
                http_options=core.resilience.provider_http_options("instruction"),  # WP-C B-C4
                cached_content=cache_name,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=NARRATIVE_DIRECTION_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            # 재시도·타임아웃 보호. 층위 호출은 모두 이 관문을 통과한다.
            _cl_op = core.cost_ledger.begin_operation(
                self.bot, core.cost_ledger.OP_TURN_SIMULATION, session=session,
                model=core.DEFAULT_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
                billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
            # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
            core.turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL,
                    contents=sim_contents,
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] 서사 설계 호출 실패: {e}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
            breakdown     = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                cached_read_tokens=cached_tokens)
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}서사 방향성 시뮬레이션",
                                 in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            core.update_stats(session, "simulation", out_tokens, thought_tokens)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            # 라벨을 '서사 설계자'라 두면 _plan_narrative와 혼동된다.
            # 이것은 지시층위 직전에 세계관 개연성을 미리 훑는 별개 호출이다.
            session.turn_cost_log.append({"label": "개연성 시뮬레이션", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
            print(
                f"[GM/{session.session_id}] 시뮬레이션 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {core.format_cost(cost)}"
            )
        except Exception as e:
            print(f"[GM] 시뮬레이션 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            sim_data = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                sim_data = json.loads(cleaned)
            except Exception as e:
                core.logs.event("turn", "개연성 시뮬레이션 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "SIMULATION_JSON"})
                return None

        # 결과를 마스터에 알린다. 비용이 드는 호출인데 무엇을 얻었는지
        # 보이지 않으면 켜둘 가치가 있는지 판단할 수 없다.
        if master_ch:
            try:
                dirs = sim_data.get("directions") or []
                lines = [f"🔮 **[개연성 시뮬레이션]** 방향 {len(dirs)}건"]
                contact = sim_data.get("action_world_contact")
                if contact:
                    lines.append(f"> 접점: {str(contact)[:150]}")
                for d in dirs[:3]:
                    if not isinstance(d, dict):
                        continue
                    lines.append(
                        f"> `{d.get('plausibility', '?')}` "
                        f"{str(d.get('label') or d.get('direction', ''))[:70]}")
                await master_ch.send("\n".join(lines)[:1900])
            except Exception as e:
                print(f"[GM] 시뮬레이션 보고 실패(무시): {e}")

        return sim_data

    # ─────────────────────────────────────────────────────────────
    # 서사 계획 명령어
    # ─────────────────────────────────────────────────────────────

    @auto.command(name="서사")
    async def show_narrative_plan(self, ctx):
        """현재 서사 계획을 마스터 채널에 임베드로 출력."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if core.player_world.legacy_narrative_retired(session):
            return await ctx.send("📖 동적 세계 세션은 레거시 서사 계획을 쓰지 않습니다 — 서사는 진행 중인 사건·정본 세계가 주도합니다.")
        plan = getattr(session, "narrative_plan", {})
        if not plan:
            return await ctx.send(
                "⚠️ 수립된 서사 계획이 없습니다.\n"
                "`!자동 시작`으로 GM를 활성화하면 초기 계획이 수립됩니다.\n"
                "또는 `!자동 재계획`으로 직접 수립할 수 있습니다."
            )

        current = plan.get("current_event", {})
        next_ev = plan.get("next_event", {})
        version = plan.get("plan_version", "?")
        last_turn = plan.get("last_planned_turn", "?")

        embed = discord.Embed(title="📖 현재 서사 계획", color=0x5865F2)
        embed.set_footer(text=f"v{version}  |  수립 시점: 턴 {last_turn}")

        # 중규모 진행 방향 (mid_plan)
        mid = plan.get("mid_plan", {})
        if mid:
            milestones = mid.get("milestones", [])
            ms_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(milestones)]) if milestones else "(없음)"
            m_val = (
                f"**전체 흐름**: {mid.get('overview', '-')}\n"
                f"**이정표**:\n{ms_str}\n"
                f"**완료 조건**: {mid.get('end_condition', '-')}"
            )
            embed.add_field(
                name=f"🗺️ 중규모 진행 방향: {mid.get('title', '?')}",
                value=m_val[:1020],
                inline=False
            )

        # 현재 순간 사건
        c_val = (
            f"**상황**: {current.get('summary', '-')}\n"
            f"**마무리 방향**: {current.get('resolution_direction', '-')}\n"
            f"**진행**: {current.get('progress') or '(초기 상태)'}"
        )
        embed.add_field(
            name=f"📌 현재 순간 사건: {current.get('title', '?')}",
            value=c_val[:1020],
            inline=False
        )

        # 다음 순간 사건
        n_val = (
            f"**개요**: {next_ev.get('summary', '-')}\n"
            f"**시작 조건**: {next_ev.get('trigger', '-')}"
        )
        embed.add_field(
            name=f"⏭️ 다음 순간 사건: {next_ev.get('title', '?')}",
            value=n_val[:1020],
            inline=False
        )

        notes = plan.get("planner_notes", "")
        if notes:
            embed.add_field(name="📝 설계 메모", value=notes[:1020], inline=False)

        await ctx.send(embed=embed)

    @auto.command(name="원장")
    async def show_info_ledger(self, ctx):
        """현재 정보 인지 원장(비공개 정보별 인지 주체)을 마스터 채널에 출력."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if core.player_world.legacy_narrative_retired(session):
            # v7.35(S1): 동적 세계는 레거시 원장 대신 지금 정본이 추적하는 요소를 요약한다
            for e in core.master_report.pack("📂 정본 추적 요소", core.master_report.COLOR_DELTA,
                                             core.master_report.tracked_sections(session),
                                             description="비밀 원문은 턴 보고의 [🔒 비밀 원문] 버튼"):
                await ctx.send(embed=e)
            return
        ledger = getattr(session, "info_ledger", []) or []
        if not ledger:
            return await ctx.send(
                "📂 정보 인지 원장이 비어 있습니다.\n"
                "GM이 비공개·플롯 관련 정보를 감지하면 이 원장에 '누가 아는가'가 누적됩니다."
            )

        embed = discord.Embed(
            title="📂 정보 인지 원장",
            description="비공개·플롯 정보별로 '누가 아는가'가 기록됩니다. GM은 이 기록에 근거해 NPC·적의 앎/모름을 판정합니다.",
            color=0x9B59B6,
        )
        for item in ledger[-10:]:
            known = ", ".join(item.get("known_by", [])) or "(없음)"
            susp = ", ".join(item.get("suspected_by", [])) or "(없음)"
            leaks = item.get("leaks", [])
            val = f"**확지**: {known}\n**추정**: {susp}"
            if item.get("origin"):
                val += f"\n**출처**: {item['origin']}"
            if leaks:
                val += "\n**유출**: " + " / ".join(leaks[-3:])
            embed.add_field(name=f"🔒 {item.get('info', '?')[:200]}", value=val[:1020], inline=False)
        embed.set_footer(text=f"총 {len(ledger)}건 기록 (최근 10건 표시)")
        await ctx.send(embed=embed)

    @auto.command(name="재계획")
    async def replan_narrative(self, ctx, *, memo: str = ""):
        """
        서사 계획을 강제로 재수립한다.
        선택적으로 메모를 추가하면 계획 수립 시 반영된다.
        예: !자동 재계획 플레이어가 예상과 달리 적에게 합류했다
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        await ctx.send("⏳ 서사 계획 재수립을 시작합니다...")
        success = await self._plan_narrative(session, "manual", context_note=memo.strip())
        if not success:
            await ctx.send("⚠️ 서사 계획 재수립에 실패했습니다.")

    # ─────────────────────────────────────────────────────────────
    # 서사 계획 내부 함수
    # ─────────────────────────────────────────────────────────────

    # WP-B: 기존 _apply_quest_choice(중복 quest 적용 경로, AUD-005)는
    #   core.turn_preparation.stage_instruction_effects의 통합 스테이징으로 대체됨.
    #   narrative_mode 가드·random 선정·offered 검증은 그 안에서 apply_choice와
    #   함께 단일 owner로 수행되고, canonical은 묘사 성립 후에만 갱신된다.

    async def _init_narrative_and_start(self, session):
        """
        !자동 시작 직후 호출. 서사 계획이 없으면 새로 수립한 뒤 첫 라운드를 시작한다.
        이미 계획이 있으면 수립 없이 바로 라운드를 시작한다.
        백그라운드 태스크로 실행됨.
        """
        if not session.narrative_plan:
            await self._plan_narrative(session, "init")
        await self._start_round(session)

    async def _update_narrative_progress(self, session, event_assessment: str, master_ch,
                                         *, preparation=None):
        """
        PROCEED 확정 묘사 직후(READY 이전) 호출. 서사 재계획 트리거를 판정한다.

        [재계획 판정 — 수치 기반]
        기존의 '3턴 주기 강제 재계획'은 삭제되었다. 서사 상태와 무관하게
        주기만으로 재계획을 돌리면 불필요한 호출이 누적되기 때문이다.
        대신 추출층위가 산출한 quest_progress 수치를 임계값과 대조해 판정한다.
            deviation >= quest_deviated  → 전체 재수립 (mid_plan 포함)
            advance   >= quest_advance   → 순간 계획만 재수립
        지시층위의 event_assessment는 보조 신호로 유지한다(수치가 없을 때의 폴백).

        WP-C: 트리거된 자동 재계획은 이 턴의 필수 준비 작업(다음 GM 프롬프트가 계획에
        의존)으로 등록되어 READY 전에 합류한다. 후보·last_planned_turn 마커는 준비 객체에
        스테이징되고 READY 이후에만 정본 narrative_plan에 적용된다.
        수치(advance/deviation) 판정은 이번 트랜잭션의 추출 계획(ExtractionMutationPlan.
        quest_progress)이 권위다 — _prepare_extraction이 계획 생성 직후
        _evaluate_numeric_replan으로 자식 준비 작업을 등록한다(B-C3).
        """
        plan = getattr(session, "narrative_plan", {})
        if not plan or preparation is None:
            return

        trigger = None      # (reason, full_replan, context_note)
        # completed/deviated → 즉시 재계획
        if event_assessment == "completed":
            # 순간 계획만 재수립 — mid_plan 유지
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 순간 사건 완료 감지\n"
                    f"> 중규모 계획을 유지하며 다음 순간 계획을 수립합니다..."
                )
            trigger = ("completed", False, "")
        elif event_assessment == "deviated":
            # mid_plan 포함 전부 재수립
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 경로 이탈 감지\n"
                    f"> 중규모 계획 포함 전체 재수립합니다..."
                )
            trigger = ("deviated", True, "")
        elif event_assessment == "resolving" and master_ch:
            await master_ch.send(
                f"📖 **[서사 계획]** 현재 순간 사건이 마무리 단계에 진입했습니다 (resolving)."
            )

        # ── 수치 기반 재계획 판정(설계문서 3)은 WP-C(B-C3)부터 이번 턴 추출 계획이
        #    나온 뒤 _evaluate_numeric_replan이 수행한다(직전 턴 last_extraction 사용 금지).
        #    precedence: event_assessment(completed/deviated) 트리거가 있으면 수치 판정은
        #    생략한다(턴당 재계획 1건 — 기존 중복 회피 규칙 유지).
        if trigger is not None:
            reason, full, note = trigger
            preparation.register_task(
                "narrative_replan", core.turn_preparation.TASK_NARRATIVE_REPLAN,
                coro=self._prepare_auto_replan(
                    session, preparation, reason, full_replan=full, context_note=note))

    async def _prepare_auto_replan(self, session, prep, trigger_reason, *,
                                   full_replan: bool, context_note: str = ""):
        """WP-C 자동 재계획 준비 작업 — 후보 생성(result-only) + stale 검사 → 스테이징."""
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if candidate is None:
            return None
        if (not prep.matches(core.turn_transaction.get_active_transaction(session))
                or core.turn_preparation.superseded_by_newer_attempt(
                    session, logical_turn=prep.logical_turn, attempt=prep.attempt)):
            candidate.rejected_stale = True
            print(f"[서사설계/{session.session_id}] stale 재계획 준비 폐기")
            return None
        prep.replan_candidate = candidate
        return candidate

    async def _plan_narrative(self, session, trigger_reason: str = "init",
                              context_note: str = "", full_replan: bool = True) -> bool:
        """
        서사 계획 수립 — **setup(init)·수동/운영자(manual) 경로 전용** consumer.

        세션 시작(_init_narrative_and_start)과 `!자동 재계획`(replan_narrative)이
        부른다. 자동 턴 TurnTransaction 의미(원인 시도·stale·멱등)를 요구하지 않으며
        기존 제품 의미(성공 시 버전 증가·저장·보고, 실패 시 기존 계획 유지)를 보존한다.
        자동 턴 재계획은 _auto_replan_narrative를 쓴다(AUD-065).

        Args:
            session: TRPGSession
            trigger_reason: "init" | "manual"
            context_note: GM이 추가한 메모 (재계획 시 계획 수립 프롬프트에 포함)
            full_replan: True이면 mid_plan 포함 전부 재수립.
                         False이면 mid_plan을 유지하고 순간 계획만 갱신.

        Returns:
            bool: 성공 여부
        """
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan)
        if candidate is None:
            return False
        return await self._apply_narrative_plan(session, candidate)

    async def _auto_replan_narrative(self, session, trigger_reason: str, *,
                                     full_replan: bool, context_note: str = "",
                                     transaction_id=None, logical_turn=None,
                                     attempt=None) -> bool:
        """자동 턴 서사 재계획(AUD-065) 비준비 경로 — 원인 정체성은 스케줄 시점 복사값.

          provider → 정규화 후보 → stale guard → 멱등 guard → 호환 적용.
        WP-C 자동 턴은 _update_narrative_progress가 준비 작업(_prepare_auto_replan)으로
        돌리고 READY 이후 _commit_replan_candidate로 적용한다. 이 함수는 그 적용 규칙
        (stale/멱등)을 공유하는 비준비 호환 경로로 남는다.
        """
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan,
            transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt)
        if candidate is None:
            return False
        return await self._commit_replan_candidate(
            session, candidate, logical_turn=logical_turn, attempt=attempt)

    async def _commit_replan_candidate(self, session, candidate, *,
                                       logical_turn=None, attempt=None,
                                       derived=None) -> bool:
        """정규화 재계획 후보를 stale/멱등 검사 후 호환 적용한다(단일 적용 규칙)."""
        # stale guard(§26/§38): 이 재계획을 일으킨 시도보다 더 새로운 시도가 활성이면 폐기.
        if core.turn_preparation.superseded_by_newer_attempt(
                session, logical_turn=logical_turn, attempt=attempt):
            candidate.rejected_stale = True
            print(f"[서사설계/{session.session_id}] stale 재계획 폐기 — 더 새로운 논리 시도 활성 "
                  f"(원인 turn={logical_turn}, attempt={attempt})")
            return False

        # 멱등 guard: 같은 (원인 시도, 사유, 모드) 재계획은 canonical에 한 번만 적용.
        #   검사와 기록 사이에 await가 없으므로 중복 태스크가 동시에 도착해도 한 번만 통과.
        key = core.turn_preparation.narrative_replan_key(candidate)
        applied_keys = getattr(session, "_narrative_replan_applied", None)
        if applied_keys is None:
            applied_keys = session._narrative_replan_applied = []
        if key and key in applied_keys:
            print(f"[서사설계/{session.session_id}] 이미 적용된 재계획 — 중복 적용 방지 ({key})")
            return False
        if key:
            applied_keys.append(key)
            if len(applied_keys) > 16:
                del applied_keys[:-16]

        return await self._apply_narrative_plan(session, candidate, derived=derived)

    async def _apply_narrative_plan(self, session, candidate, *, derived=None) -> bool:
        """LegacyCompatibilityApplier(서사 계획 교체) — 정규화 후보만 canonical에 적용한다.

        raw provider payload는 입력되지 않는다(candidate.normalized만 소비).
        코드 소유 metadata(plan_version, last_planned_turn)는 여기서 코드가 부여한다.
        기존 영속 의미(save_session_data)를 유지하며 authoritative commit(WP-D)으로
        옮기지 않는다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        trigger_reason = candidate.trigger_reason

        # ── 버전·타임스탬프 기록(코드 소유) ──
        plan = copy.deepcopy(candidate.normalized)
        plan["plan_version"]      = (session.narrative_plan or {}).get("plan_version", 0) + 1
        plan["last_planned_turn"] = session.turn_count
        session.narrative_plan    = plan
        if derived is None:
            # setup/수동 경로 — 기존 영속 의미 유지. (WP-D 권위 커밋은 strict 저장이 영속한다)
            await core.save_session_data(self.bot, session)
        candidate.applied = True

        # ── 마스터 채널 보고 (embed) ──
        current = plan.get("current_event", {})
        next_ev = plan.get("next_event", {})
        trigger_label_map = {
            "init":      "초기 계획 수립",
            "completed": "사건 완료 → 순간 계획 갱신",
            "deviated":  "이탈 감지 → 전체 재수립",
            "manual":    "수동 재계획",
        }
        trigger_label = trigger_label_map.get(trigger_reason, "계획 갱신")

        if master_ch or derived is not None:
            embed = discord.Embed(
                title=f"📖 서사 계획 갱신 — {trigger_label}",
                color=0x5865F2,
            )
            embed.set_footer(text=f"v{plan['plan_version']}  |  턴 {session.turn_count}")

            mid = plan.get("mid_plan", {})
            if mid:
                milestones = mid.get("milestones", [])
                ms_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(milestones)]) if milestones else "(없음)"
                m_val = (
                    f"**전체 흐름**: {mid.get('overview', '-')}\n"
                    f"**이정표**:\n{ms_str}\n"
                    f"**완료 조건**: {mid.get('end_condition', '-')}"
                )
                embed.add_field(
                    name=f"🗺️ 중규모 진행 방향: {mid.get('title', '?')}",
                    value=m_val[:1020],
                    inline=False,
                )

            c_val = (
                f"**상황**: {current.get('summary', '-')}\n"
                f"**마무리 방향**: {current.get('resolution_direction', '-')}"
            )
            embed.add_field(
                name=f"📌 현재 순간 사건: {current.get('title', '?')}",
                value=c_val[:1020],
                inline=False,
            )

            n_val = (
                f"**개요**: {next_ev.get('summary', '-')}\n"
                f"**시작 조건**: {next_ev.get('trigger', '-')}"
            )
            embed.add_field(
                name=f"⏭️ 다음 순간 사건: {next_ev.get('title', '?')}",
                value=n_val[:1020],
                inline=False,
            )

            planner_notes = plan.get("planner_notes", "")
            if planner_notes:
                embed.add_field(name="📝 설계 메모", value=planner_notes[:1020], inline=False)

            if derived is not None:
                derived.master(embed=embed)     # WP-D: COMMITTED 이후 방출
            else:
                await master_ch.send(embed=embed)

        core.write_log(session.session_id, "api",
                       f"[서사 계획 결과 ({trigger_label})]\n{json.dumps(plan, ensure_ascii=False, indent=2)}")
        return True

    async def _generate_narrative_plan_candidate(self, session, trigger_reason: str,
                                                 *, context_note: str = "",
                                                 full_replan: bool = True,
                                                 transaction_id=None, logical_turn=None,
                                                 attempt=None, preparation=None):
        """
        LOGIC_MODEL을 호출해 서사 계획 **후보**를 만든다 — result-only producer.

        WP-C: preparation이 주어지면(자동 턴 READY 이전) 프롬프트는 기존 흐름과 같은
        입력 의미를 투영으로 읽는다 — 이번 턴 스테이징 로그를 포함한 최근 raw_logs와
        스테이징된 진행 턴 번호. 정본은 읽기만 한다.

        session.narrative_plan을 변경하지 않는다. provider 호출·재시도·비용 관측/정산
        (CostLedger operation, accrue, turn_cost_log)은 기존 의미 그대로 수행하고,
        raw JSON은 core.turn_preparation.build_narrative_plan으로 구조 검증·정규화된
        NarrativePlanMutationPlan으로만 반환한다.

        Args:
            trigger_reason: "init" | "completed" | "deviated" | "manual"
            full_replan: True이면 mid_plan 포함 전부 재수립, False이면 순간 계획만 갱신.
            transaction_id/logical_turn/attempt: 자동 경로의 원인 시도 정체성(후보에 부착).

        Returns:
            NarrativePlanMutationPlan | None (퀘스트 모드·호출 실패·파싱 실패·구조 불량)
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        # 퀘스트 모드에서는 서사설계자가 주도하지 않는다(기획 규정).
        # 퀘스트 시스템이 서사 가이드를 대신하므로, 계획까지 세우면
        # 두 서사가 겹쳐 지시층위가 어느 쪽을 따를지 모호해진다.
        # 서사설계자는 풀자유 세션과 인피니티 플랜에서 쓴다.
        if core.player_world.legacy_narrative_retired(session):
            # v7.25(F12): 동적 세계 — 서사 권위는 사건 상태기계·정본이다. 레거시 서사설계자를 부르지 않는다(바인딩 전 free 모드 잔존 포함)
            print(f"[서사설계] 동적 세계 — 레거시 계획 수립 없음")
            return None
        if getattr(session, "narrative_mode", "quest") != "free":
            print(f"[서사설계] 퀘스트 모드 — 계획 수립 생략")
            return None

        # ── 시나리오 정보 ──
        story_guide = session.scenario_data.get("story_guide", "")
        worldview   = session.scenario_data.get("worldview", "")

        # ── 최근 게임 로그 (턴 개수 6개 유지, 각 턴은 온전 원문) ──
        recent_log_lines = []
        _raw_src = list(session.raw_logs)
        _turn_no = session.turn_count
        if preparation is not None and isinstance(preparation.staged_log, dict) \
                and not preparation.staged_log.get("applied"):
            _raw_src = (_raw_src + list(preparation.staged_log["raw_entries"]))[-20:]
            _turn_no = preparation.staged_log["turn_no"]
        for content in _raw_src[-6:]:
            try:
                text    = content.parts[0].text
                role    = content.role.upper()
                recent_log_lines.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_logs_str = "\n\n".join(recent_log_lines) if recent_log_lines else "(로그 없음)"

        # ── PC 상태 요약 ──
        pc_lines = []
        for uid, p in session.players.items():
            name    = p.get("name", "?")
            res     = session.resources.get(name, {})
            sta     = session.statuses.get(name, [])
            res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "없음"
            sta_str = ", ".join(sta) or "없음"
            pc_lines.append(f"  - {name}: 자원={res_str}, 상태={sta_str}")
        pc_info = "\n".join(pc_lines) or "(PC 없음)"

        # ── 압축 기억 ──
        memory_str = (
            session.compressed_memory
            or getattr(session, "cached_compressed_memory", "")
            or "(없음)"
        )

        # ── 기존 계획 처리 ──
        existing_plan = session.narrative_plan or {}
        existing_plan_block = ""

        if not full_replan and existing_plan:
            # completed 재계획: mid_plan 유지, 순간 계획만 갱신
            mid = existing_plan.get("mid_plan", {})
            nxt = existing_plan.get("next_event", {})
            if mid:
                ms_str = " → ".join(mid.get("milestones", []))
                existing_plan_block = (
                    "\n[유지할 중규모 진행 계획 — 이 내용을 mid_plan으로 그대로 출력하십시오]\n"
                    f"title: {mid.get('title', '')}\n"
                    f"overview: {mid.get('overview', '')}\n"
                    f"milestones: {ms_str}\n"
                    f"end_condition: {mid.get('end_condition', '')}\n"
                    "\n위 중규모 계획에서 다음으로 도달해야 할 milestone을 목표로 삼아 "
                    "새 current_event와 next_event를 수립하십시오.\n"
                )
            if nxt:
                existing_plan_block += (
                    f"\n[이전 next_event — current_event 승격 참고용]\n"
                    f"제목: {nxt.get('title', '')} / 개요: {nxt.get('summary', '')}\n"
                    f"(이 사건이 새 current_event의 출발점이 됩니다)\n"
                )
            trigger_context = "직전 순간 사건이 완료되었습니다. 중규모 계획을 유지하며 다음 순간 계획으로 전환하세요."
        else:
            # 전체 재계획 (init/deviated/manual)
            if existing_plan and trigger_reason != "init":
                cur = existing_plan.get("current_event", {})
                existing_plan_block = (
                    "\n[이전 계획 (참고용 — 폐기 후 재수립)]\n"
                    f"이전 현재 사건: {cur.get('title', '?')} — {cur.get('summary', '')}\n"
                    f"마무리 방향: {cur.get('resolution_direction', '')}\n"
                )
            trigger_context_map = {
                "init":     "GM가 활성화되었습니다. 현재 상황을 분석하여 2단계 서사 계획(mid_plan + 순간 계획)을 수립하세요.",
                "deviated": "플레이어의 선택으로 서사 방향이 예상 범위를 벗어났습니다. mid_plan 포함 계획 전체를 재수립하세요.",
                "manual":   "GM이 수동으로 재계획을 요청했습니다. 현재 상황을 재평가하여 계획 전체를 갱신하세요.",
            }
            trigger_context = trigger_context_map.get(trigger_reason, f"재계획 요청 ({trigger_reason})")

        context_note_block = f"\n[GM 추가 메모 (계획에 반영하세요)]\n{context_note}\n" if context_note else ""

        user_prompt = (
            f"[서사 계획 수립 요청]\n"
            f"트리거: {trigger_context}\n\n"
            f"[시나리오 기반 정보]\n"
            f"세계관: {worldview if worldview else '(없음)'}\n"
            f"스토리 가이드: {story_guide if story_guide else '(없음)'}\n\n"
            f"[현재 게임 상황]\n"
            f"진행 턴: {_turn_no}\n"
            f"PC 상태:\n{pc_info}\n"
            f"압축 기억:\n{memory_str}\n\n"
            f"[최근 게임 로그]\n{recent_logs_str}\n"
            f"{existing_plan_block}"
            f"{context_note_block}\n"
            "[출력 지시]\n"
            "위 정보를 바탕으로 GM이 활용할 서사 계획을 JSON 스키마에 맞게 수립하십시오."
        )


        try:
            config = types.GenerateContentConfig(
                http_options=core.resilience.provider_http_options("instruction"),  # WP-C B-C4
                system_instruction=NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=NARRATIVE_PLAN_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            _cl_op = core.cost_ledger.begin_operation(
                self.bot, core.cost_ledger.OP_TURN_NARRATIVE_PLANNING, session=session,
                model=core.LOGIC_MODEL, actor_kind=core.cost_ledger.ACTOR_PLAYER,
                billing_hint=core.cost_ledger.HINT_PLAYER_CANDIDATE)
            if preparation is not None:
                preparation.claim_cost_operation(_cl_op)
            _ok, response = await core.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.LOGIC_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] 서사 계획 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 서사 계획 수립 실패: {type(e).__name__}")
            return None

        # ── 비용 정산 ──
        try:
            meta         = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
            breakdown    = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            core.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=core.cost_ledger.SOURCE_PROVIDER_METADATA)
            core.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}서사 계획 수립",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )
            print(
                f"[GM/{session.session_id}] 서사 계획 비용: "
                f"In={in_tokens:,} Out={out_tokens:,} → {core.format_cost(cost)}"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "서사 설계자(계획)", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
        except Exception as e:
            print(f"[GM] 서사 계획 비용 정산 실패: {e}")

        # ── JSON 파싱 ──
        raw_text = response.text or ""
        try:
            plan = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                plan = json.loads(cleaned)
            except Exception as e:
                core.logs.event("turn", "서사 계획 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "PLAN_JSON", "raw": raw_text})
                if master_ch:
                    await master_ch.send("⚠️ 서사 계획 JSON 파싱 실패. 기존 계획을 유지합니다.")
                return None

        # ── 구조 검증·정규화 → 후보(result-only; canonical 미변경) ──
        candidate = core.turn_preparation.build_narrative_plan(
            plan, trigger_reason=trigger_reason, full_replan=full_replan,
            transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt)
        if candidate.normalized is None:
            print(f"[GM] 서사 계획 구조 불량: {candidate.rejected}")
            if master_ch:
                await master_ch.send("⚠️ 서사 계획 구조가 올바르지 않습니다. 기존 계획을 유지합니다.")
            return None
        return candidate


async def setup(bot):
    """디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수."""
    await bot.add_cog(GMCog(bot))

    # persistent view 등록 — 봇 재시작 후에도 추출 재시도 버튼이 동작하도록 한다.
    # 추출 실패는 다음 턴을 차단하므로, 버튼이 죽으면 세션이 영구 정지한다.
    # 중복 등록은 무해하지만 방어적으로 플래그를 둔다.
    if not getattr(bot, "_extraction_view_registered", False):
        bot.add_view(ExtractionRetryView(bot))
        bot.add_dynamic_items(core.master_report.SecretButton)     # v7.30: 전수보고 비밀 원문(재시작 뒤에도)
        bot.add_view(RewindView(bot))
        bot.add_view(core.DisplayView(bot))
        bot.add_view(core.GMHomeView(bot))
        bot._extraction_view_registered = True
