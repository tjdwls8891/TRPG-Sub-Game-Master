import os
import re
import json
import random
import asyncio
import discord
from discord.ext import commands
from google.genai import types

# 코어 유틸리티 모듈 임포트
import core

# 프롬프트 중앙 등록소에서 AI 프롬프트 상수 및 빌더 임포트
from prompts import (
    EXTRACTION_SYSTEM_INSTRUCTION,
    JUDGMENT_RESPONSE_SCHEMA,
    JUDGMENT_SYSTEM_INSTRUCTION,
    GM_LOGIC_RESPONSE_SCHEMA,
    GM_LOGIC_SYSTEM_INSTRUCTION,
    NARRATIVE_PLAN_SCHEMA,
    NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
    build_narrate_prompt,
    # 방안 B — 세계 물리 타임라인
    # 방안 E — PROCEED 자기 검증
    PROCEED_VERIFY_SCHEMA,
    PROCEED_VERIFIER_SYSTEM_INSTRUCTION,
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


def _build_judgment_user_prompt(session, player_message: str, roll_results: list) -> str:
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
    lines.append("[플레이어 프로필]\n" + ("\n".join(prof_lines) if prof_lines else "(없음)"))

    # ②-2 등장 NPC — 무대에 누가 있는지 알아야 ASK 대상과 판정 대상을 정할 수 있다
    npc_names = list((session.npcs or {}).keys())
    if npc_names:
        lines.append("[세션 등장 NPC]\n" + ", ".join(npc_names[:30]))

    # ②-3 현재 세계 상태 — 추출층위가 갱신한 위치·시간대
    tl = getattr(session, "world_timeline", {}) or {}
    if tl:
        lines.append(
            "[현재 상황]\n"
            f"위치: {tl.get('current_location', '미확인')} / "
            f"시간대: {tl.get('time_of_day', '미확인')} / "
            f"날짜: {tl.get('current_date', '미확인')}"
        )

    # ③ 실시간 노트
    note = getattr(session, "note", "") or ""
    if note:
        lines.append(f"[실시간 노트 — 이번 판단에 우선 적용]\n{note}")

    # 진행 상태 카운터 — ASK/NARRATE 반복 억제 판단 근거
    lines.append(
        f"[진행 상태] ASK 누적 {session.auto_gm_clarify_count}회 / "
        f"NARRATE 누적 {getattr(session, 'auto_gm_narrate_count', 0)}회"
    )

    if roll_results:
        lines.append(
            "[직전 굴림 결과 — 이미 컨텍스트에 반영됨]\n" + "\n".join(roll_results)
            + "\n※ 굴림 결과가 존재하므로 원칙적으로 PROCEED로 진행한다."
        )

    lines.append(f"[플레이어 신규 발언]\n{player_message}")
    return "\n\n".join(lines)


def _build_logic_user_prompt(session, player_message: str, roll_results: list,
                              sim_result: dict | None = None) -> str:
    """
    지시층위 호출용 사용자 프롬프트 조립.

    Args:
        session: TRPGSession
        player_message (str): 플레이어 신규 발언 (멀티플레이어 시 종합 텍스트)
        roll_results (list[str]): 직전 ROLL 결과 문자열 목록 (재호출 시 누적)
        sim_result (dict | None): 방안 6 서사 설계자 결과. None이면 블록 생략.
    """
    target_char = session.auto_gm_target_char or "(미지정)"
    side_note = session.auto_gm_side_note or ""
    clarify_count = session.auto_gm_clarify_count
    narrate_count = getattr(session, "auto_gm_narrate_count", 0)

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
    for uid, p in session.players.items():
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
    sta_str = ", ".join(sta) or "(없음)"

    # 서사 계획 블록 (2단계: mid_plan + 순간 계획)
    narrative_plan = getattr(session, "narrative_plan", {})
    if narrative_plan:
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
        roll_block = "\n[직전 굴림 결과 (반드시 반영하여 PROCEED를 작성)]\n" + "\n".join(roll_results)

    note_block = f"\n[GM 사이드 노트 (이번 턴 적용)]\n{side_note}\n" if side_note else ""

    # 지속 GM 노트(!노트 → session.note): 메인 묘사 프롬프트(PromptBuilder.add_note_block)와
    # 동일하게 지시층위 결정에도 주입한다. PC 신분·세계관·기정사실 등 GM이 고정한 내용이 담긴다.
    gm_note = getattr(session, "note", "") or ""
    gm_note_block = f"\n▶ 실시간 노트 (GM 직접 관리):\n{gm_note}\n" if gm_note else ""

    # 최근 5회 PROCEED 이력 블록 조립
    # NOTE: 각 PROCEED의 지시사항 + 중간 컨텍스트(NARRATE/ASK/ROLL) + AI 묘사 출력 요약 포함.
    # 지시층위가 직전 묘사 흐름을 인지하여 동일 상황 반복·정체를 방지하기 위함.
    proceed_history = getattr(session, "auto_gm_proceed_history", [])
    if proceed_history:
        ph_lines = []
        for i, entry in enumerate(proceed_history):
            turn_num = entry.get("turn_num", "?")
            instr = entry.get("instruction", "(없음)")
            ctx = entry.get("context", [])
            ai_out = entry.get("ai_summary", "")
            tag = "← 직전 PROCEED" if i == len(proceed_history) - 1 else ""
            ph_lines.append(f"  ─ PROCEED #{i + 1} (턴 {turn_num}) {tag}")
            ph_lines.append(f"    [지시사항] {instr[:200]}")
            if ctx:
                ph_lines.append("    [중간 컨텍스트 (NARRATE/ASK/ROLL)]")
                for c in ctx[:8]:  # 최대 8줄
                    ph_lines.append(f"      {c[:130]}")
            if ai_out:
                ph_lines.append("    [AI 묘사 출력 요약]")
                ph_lines.append(f"      {ai_out[:400]}")
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
    location_images: dict = session.scenario_data.get("location_images", {})
    if location_images:
        loc_lines = [f"  - {kw}: {desc}" for kw, desc in location_images.items()]
        location_images_block = (
            "\n[사용 가능한 장소 이미지 목록 — 상:키워드 태그 선택 시 이 목록에서만 고를 것]\n"
            "(새로운 장면·장소로 전환될 때 PROCEED의 proceed_instruction 맨 앞에 '상:키워드'를 삽입하라.)\n"
            + "\n".join(loc_lines) + "\n"
        )
    else:
        location_images_block = ""

    # 유효 상태이상 목록 (태: 태그 사용 시 이 목록에서만 선택 가능)
    merged_statuses = core.get_merged_status_effects(session.scenario_data)
    if merged_statuses:
        status_list_lines = []
        for sname, seff in merged_statuses.items():
            w = seff.get("weight", 0)
            w_str = f"가중치 {w:+d}" if w != 0 else "가중치 없음"
            status_list_lines.append(f"  - {sname}: 적용조건=[{seff.get('apply_condition', '')}] / {w_str} / 제거조건=[{seff.get('remove_condition', '')}]")
        valid_status_block = (
            "\n[유효 상태이상 목록 — 태: 태그는 이 목록에 있는 이름만 사용 가능]\n"
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
    memory_block = f"\n[압축 기억 — 이전 턴 요약]\n{_mem[:800]}\n" if _mem else ""

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
            f"위치: {world_tl.get('current_location', '(미확인)')}\n"
            f"시간대: {world_tl.get('time_of_day', '(미확인)')}\n"
            f"세력/지역 컨텍스트: {world_tl.get('faction_context', '(미확인)')}\n"
        )
    else:
        world_tl_block = ""

    # 정보 인지 원장 블록 (지속형): 비공개·플롯 정보별 '누가 아는가'의 확립된 기록.
    # 지시층위가 info_access 필드로 갱신하며, 적·NPC가 도달 경로 없는 정보로 행동하지 않도록 하는 진실 기준.
    info_ledger = getattr(session, "info_ledger", []) or []
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
    target_chars = getattr(session, "auto_gm_target_chars", [])
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
    session.auto_gm_last_logic_manifest = manifest

    return f"""[현재 턴 #]: {session.turn_count + 1}
[대상 PC]: {target_char}
[PC 프로필]: {pc_profile_summary or "(미설정)"}{stat_desc_line}
[PC 자원]: {res_str}
[PC 상태]: {sta_str}{gm_note_block}
[직전 ASK 횟수 / 한도]: {clarify_count} / {MAX_CLARIFY_PER_MESSAGE}
[직전 NARRATE 횟수 / 한도]: {narrate_count} / {MAX_NARRATE_PER_MESSAGE}
{multi_info}{note_block}{world_tl_block}{info_ledger_block}{memory_block}{km_block}
[최근 5턴 컨텍스트 (온전 원문)]
{recent_logs_str}
{current_turn_block}{proceed_history_block}{narrative_block}{sim_block}{location_images_block}{valid_status_block}
{roll_block}
{prohibitions_block}
[플레이어 신규 발언]
{player_message}

위 컨텍스트를 분석하여 다음 단일 action(ASK / NARRATE / ROLL / PROCEED)을 결정하고 JSON 스키마에 맞춰 응답하십시오."""


# ========== [GM 주사위 버튼 View] ==========
class RewindConfirmView(discord.ui.View):
    """되감기 실행 확인 — 되돌리기 불가·환불 불가를 고지한 뒤 실행한다."""

    def __init__(self, bot, session, target_turn: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.session = session
        self.target_turn = target_turn

    @discord.ui.button(label="되감기 실행", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        result = core.rewind_to(self.session, self.target_turn)
        if not result["ok"]:
            await interaction.followup.send(f"⚠️ {result['reason']}")
            return
        await core.save_session_data(self.bot, self.session)
        msg = (
            f"⏪ **{self.target_turn}턴 종료 시점으로 되돌렸습니다.**\n"
            f"> 제거된 턴: {', '.join(str(t) for t in result['removed_turns'])}\n"
            f"> 복원된 항목: {result['changes']}건"
        )
        if result["compression_rolled_back"]:
            msg += "\n> 압축 기억도 함께 롤백되었습니다."
        await interaction.followup.send(msg)
        try:
            await interaction.message.delete()
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.edit_message(content="되감기를 취소했습니다.", view=None)
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
                "되감기 기록이 아직 없습니다.", ephemeral=True)
            return

        target = newest - 1
        if target < oldest:
            await interaction.response.send_message(
                f"되감기 가능 범위는 {oldest}~{newest}턴입니다.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"⚠️ **{newest}턴을 제거하고 {target}턴 종료 시점으로 되돌립니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
            f"제거되는 정보는 되감기 로그로 이관됩니다.",
            view=RewindConfirmView(self.bot, session, target),
        )


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
        ctx_text = (getattr(session, "extraction_retry_ctx", {}) or {}).get("text", "")
        if not ctx_text:
            session.extraction_pending = False
            await core.save_session_data(self.bot, session)
            await interaction.followup.send("재시도할 원본이 없어 차단만 해제했습니다.", ephemeral=True)
            return

        cog = self.bot.get_cog("GMCog")
        master_ch = self.bot.get_channel(session.master_ch_id) if getattr(session, "master_ch_id", None) else None
        result = await cog._run_extraction(session, ctx_text, master_ch)
        if result:
            await core.save_session_data(self.bot, session)
            await interaction.followup.send("✅ 턴 정보 정리가 완료되었습니다. 계속 진행하십시오.")
            try:
                await interaction.message.delete()
            except Exception:
                pass


class GMRollView(discord.ui.View):
    """
    GM에서 ROLL 판정 시 플레이어에게 주사위 버튼을 제공하는 View.
    """

    def __init__(self, cog, session, roll_specs: list, player_message: str,
                 prior_roll_results: list, target_uid: str | None):
        super().__init__(timeout=300)
        self.cog = cog
        self.session = session
        self.roll_specs = roll_specs
        self.player_message = player_message
        self.prior_roll_results = list(prior_roll_results)
        self.target_uid = target_uid
        self._resolved = False

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
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
        new_results = await self.cog._execute_rolls(self.session, self.roll_specs, game_ch)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(self.session, self.player_message, combined)
        )

    async def on_timeout(self):
        if self._resolved:
            return
        self._resolved = True

        master_ch = self.cog.bot.get_channel(self.session.master_ch_id)
        if master_ch:
            await master_ch.send(
                "⚠️ **[GM]** 판정 버튼 시간 초과(5분). 주사위를 자동으로 굴립니다."
            )
        game_ch = self.cog.bot.get_channel(self.session.game_ch_id)
        new_results = await self.cog._execute_rolls(self.session, self.roll_specs, game_ch)
        combined = self.prior_roll_results + new_results
        asyncio.create_task(
            self.cog._continue_with_roll_results(self.session, self.player_message, combined)
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

        session.auto_gm_active = True
        session.auto_gm_target_chars = target_chars
        session.auto_gm_target_char = target_chars[0]   # 하위 호환성 (지시층위 단일 PC 참조용)
        session.auto_gm_turns_done = 0
        session.auto_gm_clarify_count = 0
        session.auto_gm_cost_baseline = session.total_cost
        session.auto_gm_side_note = ""
        session.auto_gm_pending_players = []
        session.auto_gm_collected_actions = {}
        session.auto_gm_waiting_for = None
        # NOTE: 최근 5회 PROCEED 이력 (지시사항 + 중간 컨텍스트 + AI 출력 요약).
        # 지시층위 프롬프트에 주입되어 서사 반복·정체를 방지한다. 봇 재시작 시 초기화 허용.
        session.auto_gm_proceed_history = []
        await core.save_session_data(self.bot, session)

        has_existing_plan = bool(session.narrative_plan)
        plan_note = (
            f"- 서사 계획: 기존 계획 유지 (v{session.narrative_plan.get('plan_version', '?')})"
            if has_existing_plan else
            "- 서사 계획: 수립 중... (백그라운드에서 진행)"
        )
        await ctx.send(
            f"🤖 **[GM 활성화]**\n"
            f"- 대상 PC: **{', '.join(target_chars)}**\n"
            f"- 자동 턴 한도: {_cap_display(session.auto_gm_turn_cap)}\n"
            f"- 자동 누적 비용 한도: {_cap_display(session.auto_gm_cost_cap_krw, is_cost=True)}\n"
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

        if not getattr(session, "auto_gm_active", False):
            return await ctx.send("⚠️ GM가 활성 상태가 아닙니다.")

        session.auto_gm_active = False
        session.auto_gm_waiting_for = None
        session.auto_gm_pending_players = []
        await core.save_session_data(self.bot, session)

        used = session.total_cost - session.auto_gm_cost_baseline
        await ctx.send(
            f"🛑 **[GM 정지]**\n"
            f"- 자동 처리 턴: {session.auto_gm_turns_done}턴\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)}\n"
            f"- 인간 GM 명령어 입력 모드로 복귀합니다."
        )

    @auto.command(name="상태")
    async def auto_status(self, ctx):
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        active = getattr(session, "auto_gm_active", False)
        used = session.total_cost - getattr(session, "auto_gm_cost_baseline", 0.0)
        target_chars = getattr(session, "auto_gm_target_chars", [])
        waiting = getattr(session, "auto_gm_waiting_for", None)
        pending = getattr(session, "auto_gm_pending_players", [])
        collected = getattr(session, "auto_gm_collected_actions", {})

        collected_str = "\n".join([f"    · {k}: {v[:40]}" for k, v in collected.items()]) or "    (없음)"
        await ctx.send(
            f"🤖 **[GM 상태]**\n"
            f"- 활성: {'✅ 켜짐' if active else '⛔ 꺼짐'}\n"
            f"- 대상 PC: {', '.join(target_chars) if target_chars else '(없음)'}\n"
            f"- 자동 처리 턴: {session.auto_gm_turns_done} / {_cap_display(session.auto_gm_turn_cap)}\n"
            f"- 자동 모드 누적 비용: {core.format_cost(used)} / {_cap_display(session.auto_gm_cost_cap_krw, is_cost=True)}\n"
            f"- 현재 발언 대기 PC: {waiting or '(없음)'}\n"
            f"- 응답 대기 중인 PC: {', '.join(pending) if pending else '(없음)'}\n"
            f"- 수집된 행동:\n{collected_str}\n"
            f"- 직전 ASK 횟수: {session.auto_gm_clarify_count}\n"
            f"- 대기 중 사이드 노트: {session.auto_gm_side_note or '(없음)'}"
        )

    @auto.command(name="개입")
    async def auto_inject(self, ctx, *, text: str = ""):
        """다음 PROCEED 완료 시까지 GM 사이드 노트를 유지."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not text.strip():
            return await ctx.send("⚠️ 사용법: `!자동 개입 [GM에게 전달할 메모]`")

        session.auto_gm_side_note = text.strip()
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
            session.auto_gm_turn_cap = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 턴 한도를 **무제한**으로 설정했습니다.")

        try:
            val = int(n)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 턴제한 [1~1000 | 해제]`")
        if val < 1 or val > 1000:
            return await ctx.send("⚠️ 턴 한도는 1~1000 사이여야 합니다. (무제한: `해제`)")

        session.auto_gm_turn_cap = val
        await core.save_session_data(self.bot, session)
        await ctx.send(f"✅ 자동 턴 한도를 {val}턴으로 변경했습니다.")

    @auto.command(name="비용제한")
    async def auto_set_cost_cap(self, ctx, amount: str = None):
        """자동 모드 누적 비용 상한(원) 설정. '해제'/'0' 또는 인자 없음 → 무제한."""
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if amount is None or amount in ("해제", "무제한", "0"):
            session.auto_gm_cost_cap_krw = None
            await core.save_session_data(self.bot, session)
            return await ctx.send("✅ 자동 누적 비용 한도를 **무제한**으로 설정했습니다.")

        try:
            val = float(amount)
        except ValueError:
            return await ctx.send("⚠️ 사용법: `!자동 비용제한 [금액(원) | 해제]`")
        if val <= 0:
            return await ctx.send("⚠️ 비용 한도는 0보다 커야 합니다. (무제한: `해제`)")

        session.auto_gm_cost_cap_krw = val
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

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return
        if message.channel.id != session.game_ch_id:
            return
        if not getattr(session, "auto_gm_active", False):
            return

        user_id_str = str(message.author.id)
        char_name = session.players.get(user_id_str, {}).get("name")
        waiting_for = getattr(session, "auto_gm_waiting_for", None)

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
        auto_gm_target_chars가 비어 있으면 아무 동작도 하지 않음.
        """
        if not getattr(session, "auto_gm_active", False):
            return
        target_chars = getattr(session, "auto_gm_target_chars", [])
        if not target_chars:
            return

        session.auto_gm_pending_players = list(target_chars)
        session.auto_gm_collected_actions = {}
        session.auto_gm_waiting_for = None
        session.auto_gm_narrate_count = 0
        # 저장은 바로 이어지는 _ask_next_player가 처리 (중복 제거).
        # 여기서 초기화한 세 필드는 SESSION_RESET_FIELDS라 크래시 시에도 재시작 후 어차피 초기화됨.
        await self._ask_next_player(session)

    async def _ask_next_player(self, session):
        """
        auto_gm_pending_players에서 다음 PC를 꺼내 행동 요청 메시지를 게임 채널에 전송.
        목록이 비어 있으면 수집 완료로 처리.
        """
        game_ch = self.bot.get_channel(session.game_ch_id)

        if not session.auto_gm_pending_players:
            # 모든 PC의 행동이 수집됨 → 지시층위 호출
            await self._finalize_round_and_process(session)
            return

        next_char = session.auto_gm_pending_players.pop(0)
        session.auto_gm_waiting_for = next_char

        uid = core.get_uid_by_char_name(session, next_char)
        mention = f"<@{uid}>" if uid else f"**{next_char}**"

        collected = session.auto_gm_collected_actions
        if collected:
            # 이미 다른 PC의 행동이 수집된 상태 → 간단히 참고 표시
            others = "、".join([
                f"{k}: '{v[:20]}...'" if len(v) > 20 else f"{k}: '{v}'"
                for k, v in collected.items()
            ])
            prompt = f"{mention}, 현재까지 행동 선언: {others}\n{mention}은(는) 어떻게 하시겠습니까?"
        else:
            prompt = f"{mention}님 턴입니다. 어떤 행동을 하시겠습니까?"

        if game_ch:
            await core.stream_text_to_channel(
                self.bot, game_ch, f"> {prompt}",
                words_per_tick=8, tick_interval=0.8
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
            if session.auto_gm_waiting_for != char_name:
                return
            if not session.auto_gm_active:
                return

            session.auto_gm_waiting_for = None
            content = message.content.strip()

            # 행동 수집
            session.auto_gm_collected_actions[char_name] = content
            session.current_turn_logs.append(f"[{char_name}]: {content}")

            if session.auto_gm_pending_players:
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

        actions = session.auto_gm_collected_actions.copy()
        session.auto_gm_collected_actions = {}
        session.auto_gm_clarify_count = 0

        if not actions:
            return

        if len(actions) == 1:
            player_message = list(actions.values())[0]
        else:
            # 멀티플레이어 — 게임 채널에 행동 종합 표시
            summary_lines = "\n".join([f"> **{k}**: {v}" for k, v in actions.items()])
            if game_ch:
                await game_ch.send(f"> 📋 **행동 선언 종합:**\n{summary_lines}")
                core.write_log(session.session_id, "game_chat",
                               f"[행동 종합]: {'; '.join([f'{k}: {v}' for k, v in actions.items()])}")
            player_message = "\n".join([f"[{k}]: {v}" for k, v in actions.items()])
            # 대표 PC를 첫 번째 PC로 업데이트
            first_char = list(actions.keys())[0]
            session.auto_gm_target_char = first_char

        await self._process_actions(session, player_message, master_ch)

    # ─────────────────────────────────────────────────────────────
    # 안전장치 + 지시층위 루프 진입점
    # ─────────────────────────────────────────────────────────────

    async def _handle_player_message(self, session, message: discord.Message):
        """
        기존 자발적 플레이어 발언 처리 경로 (auto_gm_waiting_for 없을 때).
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
            if not session.auto_gm_active:
                return

            if session.auto_gm_turn_cap is not None and session.auto_gm_turns_done >= session.auto_gm_turn_cap:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if session.auto_gm_cost_cap_krw is not None and used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            session.auto_gm_clarify_count = 0
            char_name = session.auto_gm_target_char or message.author.display_name
            session.current_turn_logs.append(f"[{char_name}]: {message.content.strip()}")

            await self._process_actions(session, message.content.strip(), master_ch)

    async def _process_actions(self, session, player_message: str, master_ch):
        """
        안전장치 확인 후 지시층위 루프(_run_gm_logic_loop) 호출.
        _handle_player_message와 _finalize_round_and_process의 공통 진입 경로.
        이미 락 안에서 호출된다고 가정하므로 이 함수 내부에는 락 없음.
        """
        if not session.auto_gm_active:
            return

        if session.auto_gm_turn_cap is not None and session.auto_gm_turns_done >= session.auto_gm_turn_cap:
            session.auto_gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        used_cost = session.total_cost - session.auto_gm_cost_baseline
        if session.auto_gm_cost_cap_krw is not None and used_cost >= session.auto_gm_cost_cap_krw:
            session.auto_gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
            await core.save_session_data(self.bot, session)
            return

        await self._run_gm_logic_loop(session, player_message, master_ch)

    # ─────────────────────────────────────────────────────────────
    # 지시층위 루프 본체
    # ─────────────────────────────────────────────────────────────

    async def _finish_proceed_and_continue(self, session, instruction, master_ch,
                                           *, event_assessment=None):
        """
        강제/정상 PROCEED 공통 후처리. 여러 호출부에 중복되던 동일 블록을 단일화한다.

          1) 턴 한도 도달 시 자동 모드 정지 + 마스터 채널 알림
          2) _dispatch_proceed로 묘사 생성
          3) event_assessment가 주어지면(정상 PROCEED 계열) 서사 진행도 갱신
          4) 카운터·사이드 노트 초기화 + turns_done 증가
          5) 세션 저장 후, 여전히 활성이면 다음 라운드(선제 행동 질문) 시작

        Args:
            event_assessment: PROCEED 계열에서 지시층위가 평가한 사건 상태.
                None이면 서사 진행도 갱신을 건너뛴다(강제 PROCEED 폴백 경로).
        """
        if session.auto_gm_turn_cap is not None and (session.auto_gm_turns_done + 1) >= session.auto_gm_turn_cap:
            session.auto_gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 마지막 턴]** 자동 턴 한도({session.auto_gm_turn_cap}턴) 도달. "
                    f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                )

        # 되감기 델타 기준점.
        # NOTE: 턴마다 새로 스냅샷을 뜨면, 델타 계산과 다음 스냅샷 사이에 일어난
        #       변화(백그라운드 추출층위의 world_timeline·statuses 갱신 등)가
        #       어느 델타에도 잡히지 않고 영구 유실된다.
        #       따라서 '마지막으로 기록한 시점의 스냅샷'을 세션에 들고 다니며
        #       그것과 비교한다. 늦게 도착한 변화는 다음 턴 델타에 귀속되지만
        #       유실되지는 않는다.
        state_before = getattr(session, "_rewind_snapshot", None)
        if state_before is None:
            state_before = core.capture_state(session)
        cost_before = getattr(session, "total_cost", 0.0)

        await self._dispatch_proceed(session, instruction)

        if event_assessment is not None:
            await self._update_narrative_progress(session, event_assessment, master_ch)

        session.auto_gm_clarify_count = 0
        session.auto_gm_narrate_count = 0
        session.auto_gm_turns_done += 1
        session.auto_gm_side_note = ""

        # ── 되감기 기록 (4.6.0) ──
        # NOTE: 추출층위가 백그라운드로 도는 중이라 그 결과는 이 델타에 반영되지
        #       않을 수 있다. 추출 결과는 다음 턴 델타에서 잡힌다.
        turn_no = session.auto_gm_turns_done
        if turn_no > getattr(session, "last_recorded_turn", 0):
            try:
                state_after = core.capture_state(session)
                changes = core.diff_state(state_before, state_after)
                core.record_delta(
                    session, turn_no, changes,
                    cost_krw=getattr(session, "total_cost", 0.0) - cost_before,
                )
                session._rewind_snapshot = state_after
                core.record_full_log(
                    session, turn_no,
                    core.serialize_log_entries(session.raw_logs[-2:]),
                )
                session.last_recorded_turn = turn_no
            except Exception as e:
                print(f"[되감기] 델타 기록 실패(진행에는 영향 없음): {e}")

        await core.save_session_data(self.bot, session)

        if session.auto_gm_active:
            await self._start_round(session)

    async def _run_gm_logic_loop(self, session, player_message: str, master_ch):
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

        # ── 방안 6 → 지시층위 순차 주입 ──
        # 세계관 캐시가 유효하면 먼저 서사 방향성을 시뮬레이션하고, 그 결과(sim_result)를
        # 지시층위 첫 결정에 실제로 주입한다.
        # (과거 gather 병렬 실행은 첫 결정이 sim_result를 보지 못해 시뮬레이션 비용만
        #  낭비되는 구조였다 — 순차 주입으로 교정.)
        sim_result: dict | None = None
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        do_simulation = bool(cache_name and cache_model == core.DEFAULT_MODEL)

        # 이번 턴 예상 비용 — 디스플레이 채널 도입 전까지 마스터 채널에 보고한다.
        try:
            est = core.estimate_turn(session, "PROCEED")
            # 압축 선결제 몫 — 5턴 압축 비용의 20%를 매 턴 예상액에 포함한다(기획 규정).
            prepay = core.compression_prepay(session)
            est["compression_prepay_krw"] = prepay["krw"]
            est["min_krw"] = round(est["min_krw"] + prepay["krw"], 2)
            est["max_krw"] = round(est["max_krw"] + prepay["krw"], 2)
            est["min_ink"] = core.cost_to_ink(est["min_krw"])
            est["max_ink"] = core.cost_to_ink(est["max_krw"])
            session.last_estimate = est
            # 선결제분 누적 — 실제 압축 시 또는 세션 종료 시 정산된다.
            session.compression_prepaid_krw = (
                float(getattr(session, "compression_prepaid_krw", 0.0) or 0.0) + prepay["krw"]
            )
            await m_send(
                f"💰 **[예상]** {core.format_estimate(est)}\n"
                f"> 입력 {est['input_tokens']['instruction']:,} + 캐시 "
                f"{est['input_tokens']['cached']:,} 토큰 | "
                f"압축 선결제 {prepay['krw']:.2f}원 누적 "
                f"{session.compression_prepaid_krw:.2f}원"
            )
        except Exception as e:
            print(f"[EST] 예상 산출 실패(진행에는 영향 없음): {e}")

        # 플레이어가 보는 게임 채널에 판단 대기 안내 (판단 완료 후 삭제)
        status_msg = await core.send_status_message(
            game_ch, "🤔 *GM이 상황을 판단하는 중…*"
        )

        if do_simulation:
            if game_ch:
                async with game_ch.typing():
                    sim_result = await self._simulate_narrative_directions(
                        session, player_message, master_ch)
            else:
                sim_result = await self._simulate_narrative_directions(
                    session, player_message, master_ch)

        # ── [판단층위] 캐시 미사용 — 진행 유형과 ASK 질문·ROLL 명세를 결정 ──
        if game_ch:
            async with game_ch.typing():
                judgment = await self._call_judgment(
                    session, player_message, roll_results, master_ch)
        else:
            judgment = await self._call_judgment(
                session, player_message, roll_results, master_ch)

        await core.clear_status_message(status_msg)

        if not judgment:
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
                            sim_result=current_sim, action=action)
                else:
                    decision = await self._call_gm_logic(
                        session, player_message, roll_results, master_ch,
                        sim_result=current_sim, action=action)
                if not decision:
                    await m_send("⚠️ 지시층위 호출 실패. 이번 발언을 스킵합니다.")
                    return
                # 판단층위 산출물과 병합 — 이후 분기는 기존 구조를 그대로 사용한다.
                decision = {**judgment, **decision, "action": action}
            else:
                decision = dict(judgment)

            label = action_labels.get(action, action)
            print(f"[GM/{session.session_id}] iter={iteration} action={action} :: {reasoning[:120]}")
            await m_send(
                f"🤖 **[GM 판단 #{iteration + 1}]** {label}\n"
                f"> {reasoning[:200]}"
            )

            # ── ASK ──
            if action == "ASK":
                session.auto_gm_clarify_count += 1
                if session.auto_gm_clarify_count > MAX_CLARIFY_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** ASK 한도({MAX_CLARIFY_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result)
                    await self._finish_proceed_and_continue(session, forced_instr, master_ch)
                    return

                bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                if game_ch:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, bridge,
                        words_per_tick=5, tick_interval=1.5
                    )
                # ASK 브리지를 current_turn_logs에 기록 → 다음 지시층위 호출 시 맥락 유지
                session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
                print(f"[GM/{session.session_id}] ASK -> '{bridge[:80]}'")
                # 저장은 루프 종료 후 트레일링 save가 일괄 처리 (중복 제거)
                break

            # ── NARRATE ──
            elif action == "NARRATE":
                session.auto_gm_narrate_count = getattr(session, "auto_gm_narrate_count", 0) + 1
                if session.auto_gm_narrate_count > MAX_NARRATE_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** NARRATE 한도({MAX_NARRATE_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result)
                    await self._finish_proceed_and_continue(session, forced_instr, master_ch)
                    return

                narrate_instr = decision.get("narrate_instruction") or "현재 상황을 간략히 설명하십시오."
                # NOTE: typing 컨텍스트는 _dispatch_narrate 내부 API 호출 블록에서만 활성화됨.
                # 외부에서 typing()으로 감싸면 stream_text_to_channel 실행 시 typing이 살아있어
                # Discord 상충으로 스트리밍이 멈추는 버그 발생 — 외부 typing 제거.
                narrate_text = await self._dispatch_narrate(session, narrate_instr)
                if narrate_text:
                    print(f"[GM/{session.session_id}] NARRATE #{session.auto_gm_narrate_count} -> '{narrate_text[:60]}'")
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
                        session, player_message, roll_results, master_ch, sim_result)
                    await self._finish_proceed_and_continue(session, fallback_instr, master_ch)
                    return

                # 버튼 UI 전송 후 루프 종료 (계속 처리는 버튼 콜백 담당)
                await self._dispatch_rolls(session, rolls, player_message, list(roll_results))
                await core.save_session_data(self.bot, session)
                return

            # ── PROCEED ──
            elif action == "PROCEED":
                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or ""
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                # 자원 변동 — 지시층위가 독립 필드로 보고한 항목을 태그로 변환해 덧붙인다.
                # game.py의 기존 태그 파서·검증(등록 캐릭터명 확인)을 그대로 재사용하기 위함.
                res_tags = core.resource_changes_to_tags(decision.get("resource_changes"))
                if res_tags:
                    instruction = f"{instruction} {res_tags}"

                # ── 방안 E 제거 (방안 D) ──
                # _verify_proceed_instruction 호출 삭제.
                # 미선언 PC 행동 방지는 지시층위 [최우선 절대 원칙]으로 커버.

                # 서사 사건 평가는 event_assessment로 헬퍼에 전달되어 진행도 갱신·재계획에 사용된다.
                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"))
                return

            else:
                await m_send(f"⚠️ GM이 알 수 없는 action을 반환했습니다: {action}")
                break

        else:
            # 루프 한도 도달 → 강제 PROCEED
            await m_send(f"⚙️ GM 내부 루프 한도({MAX_ITERATIONS_PER_MESSAGE}) 도달 → 강제 PROCEED.")
            await self._finish_proceed_and_continue(
                session, "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오.", master_ch)
            return

        # ASK/NARRATE는 break로 여기 도달 — 대기 상태를 한 번 저장한다.
        await core.save_session_data(self.bot, session)

    # ─────────────────────────────────────────────────────────────
    # 지시층위 호출
    # ─────────────────────────────────────────────────────────────

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

        await ctx.send(
            f"⚠️ **{target}턴 종료 시점으로 되돌립니다.**\n"
            f"되돌리기는 취소할 수 없으며, 이미 소모된 비용은 환불되지 않습니다.\n"
            f"제거되는 정보는 되감기 로그로 이관됩니다.",
            view=RewindConfirmView(self.bot, session, target),
        )

    async def _forced_proceed_instruction(self, session, player_message: str,
                                          roll_results: list, master_ch,
                                          sim_result: dict | None = None) -> str:
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
            sim_result=sim_result, action="PROCEED")
        if not decision:
            return fallback
        return _clean_proceed_instruction(decision.get("proceed_instruction") or fallback)

    async def _call_judgment(self, session, player_message: str, roll_results: list,
                             master_ch) -> dict | None:
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
        user_prompt = _build_judgment_user_prompt(session, player_message, roll_results)
        core.write_log(session.session_id, "api", f"[판단층위 요청 - Payload]\n{user_prompt}")

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            system_instruction=JUDGMENT_SYSTEM_INSTRUCTION,
            temperature=0.4,
            response_mime_type="application/json",
            response_schema=JUDGMENT_RESPONSE_SCHEMA,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )

        # 층위 자체 재시도 — 판단 실패가 지시층위 재시도로 번지지 않게 한다.
        decision = None
        for attempt in range(JUDGMENT_MAX_RETRIES):
            try:
                response = await asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=JUDGMENT_MODEL,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                print(f"[GM] 판단층위 호출 실패(시도 {attempt + 1}): {type(e).__name__} - {e}")
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
                session.total_cost += cost
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
                    "manifest": [],
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
                    print(f"[GM] 판단층위 JSON 파싱 실패(시도 {attempt + 1}): {e}")
                    continue
            break

        if not decision:
            if master_ch:
                await master_ch.send("⚠️ 판단층위 호출에 실패했습니다. 이번 발언을 스킵합니다.")
            return None

        core.write_log(
            session.session_id, "api",
            f"[판단층위 결정]\n{json.dumps(decision, ensure_ascii=False, indent=2)}"
        )
        return decision

    async def _call_gm_logic(self, session, player_message: str, roll_results: list,
                              master_ch, sim_result: dict | None = None,
                              action: str = "PROCEED") -> dict | None:
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
        user_prompt = _build_logic_user_prompt(session, player_message, roll_results,
                                                sim_result=sim_result)
        # 판단층위가 확정한 진행 유형을 지시층위에 전달한다. 지시층위는 이 유형을 바꾸지 않는다.
        user_prompt = (
            f"[확정된 진행 유형] {action}\n"
            f"※ 이 유형은 판단층위에서 이미 결정되었습니다. 변경하지 말고, "
            f"이 유형에 필요한 지시문만 작성하십시오.\n\n"
        ) + user_prompt

        core.write_log(session.session_id, "api", f"[자동 지시층위 요청 - Payload]\n{user_prompt}")

        # 캐시 활용 가능 여부 판단
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        use_cache   = bool(cache_name and cache_model == core.DEFAULT_MODEL)

        try:
            if use_cache:
                # ── 캐시 활용 경로 (방안 ①) ──
                # GM_LOGIC_SYSTEM_INSTRUCTION은 이제 세션 캐시 본문에 함께 구워져 있으므로
                # contents에 신선 입력으로 다시 넣지 않는다(캐시 읽기 단가로 처리 → 비용 절감).
                logic_contents = [
                    types.Content(role="user",
                                  parts=[types.Part.from_text(text=user_prompt)]),
                ]
                config = types.GenerateContentConfig(
                    cached_content=cache_name,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=GM_LOGIC_RESPONSE_SCHEMA,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                # ── 폴백: 캐시 없음 / 모델 불일치 ──
                logic_contents = [
                    types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])
                ]
                config = types.GenerateContentConfig(
                    system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=GM_LOGIC_RESPONSE_SCHEMA,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL,
                contents=logic_contents,
                config=config,
            )
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
            session.total_cost += cost
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
                "manifest": list(getattr(session, "auto_gm_last_logic_manifest", [])),
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
                print(f"[GM] JSON 파싱 실패: {e}\n응답 원문: {raw_text[:500]}")
                if master_ch:
                    await master_ch.send("⚠️ 자동 지시층위 응답이 JSON 형식이 아닙니다. 이번 발언 스킵.")
                return None

        core.write_log(
            session.session_id, "api",
            f"[자동 지시층위 결정]\n{json.dumps(decision, ensure_ascii=False, indent=2)}"
        )

        # 정보 인지 원장 갱신 (지속형): info_access 델타를 session.info_ledger에 누적 병합.
        self._update_info_ledger(session, decision)
        return decision

    def _update_info_ledger(self, session, decision: dict):
        """
        지시층위의 info_access(new_secrets/new_leaks) 델타를 session.info_ledger에 누적 병합한다.
        - new_secrets: 원장에 없는 신규 비밀만 추가 (중복·드리프트 방지)
        - new_leaks: 기존 항목 leaks에 근거(how)와 함께 기록하고 known_by 확장(suspected_by에서 이동)
        - 스코핑: 최대 MAX_LEDGER_ITEMS 항목만 유지 (오래된 것부터 제거)
        갱신은 in-memory. 저장은 호출부 루프의 save_session_data에 위임. 실패해도 진행에 영향 없음.
        """
        MAX_LEDGER_ITEMS = 12
        try:
            ia = decision.get("info_access") or {}
            if not isinstance(ia, dict):
                return
            ledger = getattr(session, "info_ledger", None)
            if not isinstance(ledger, list):
                ledger = []
                session.info_ledger = ledger
            turn = session.turn_count + 1

            def _norm(s):
                return re.sub(r"\s+", "", (s or "")).lower()

            def _find(info):
                ni = _norm(info)
                if not ni:
                    return None
                for it in ledger:
                    ei = _norm(it.get("info", ""))
                    if ei and (ni == ei or ni in ei or ei in ni):
                        return it
                return None

            # ── 신규 비밀 ──
            for sec in (ia.get("new_secrets") or []):
                if not isinstance(sec, dict):
                    continue
                info = (sec.get("info") or "").strip()
                if not info or _find(info):
                    continue  # 이미 원장에 있으면 스킵(중복 주입·드리프트 방지)
                ledger.append({
                    "info": info,
                    "known_by": list(dict.fromkeys(sec.get("known_by") or [])),
                    "suspected_by": list(dict.fromkeys(sec.get("suspected_by") or [])),
                    "origin": (sec.get("origin") or "").strip(),
                    "leaks": [],
                    "turn_added": turn,
                })

            # ── 신규 유출 ──
            for lk in (ia.get("new_leaks") or []):
                if not isinstance(lk, dict):
                    continue
                info = (lk.get("info") or "").strip()
                to = (lk.get("to") or "").strip()
                how = (lk.get("how") or "").strip()
                if not info or not to:
                    continue
                item = _find(info)
                if item is None:
                    if not how:  # 근거 없는 유출은 무시(날조 방어)
                        continue
                    item = {"info": info, "known_by": [], "suspected_by": [],
                            "origin": "", "leaks": [], "turn_added": turn}
                    ledger.append(item)
                if to not in item["known_by"]:
                    item["known_by"].append(to)
                if to in item.get("suspected_by", []):
                    item["suspected_by"] = [x for x in item["suspected_by"] if x != to]
                item.setdefault("leaks", []).append(f"턴{turn}: {to} — {how}" if how else f"턴{turn}: {to}")

            # ── 스코핑: 최대 항목 수 유지 ──
            if len(ledger) > MAX_LEDGER_ITEMS:
                del ledger[:len(ledger) - MAX_LEDGER_ITEMS]
        except Exception as e:
            print(f"[GM] info_ledger 갱신 실패: {e}")

    # ─────────────────────────────────────────────────────────────
    # ROLL 실행 및 버튼 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _execute_rolls(self, session, rolls: list, game_ch) -> list[str]:
        """rolls 목록을 굴리고 결과를 게임·마스터 채널에 선언."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        results: list[str] = []

        for r in rolls:
            char_name = r.get("char_name") or session.auto_gm_target_char or "?"
            stat_name = r.get("stat") or ""
            sides = int(r.get("sides") or 20)
            weight = int(r.get("weight") or 0)

            stat_value = None
            uid = core.get_uid_by_char_name(session, char_name)
            if uid:
                profile = session.players.get(uid, {}).get("profile", {})
                if stat_name in profile:
                    try:
                        stat_value = int(profile[stat_name])
                    except (TypeError, ValueError):
                        stat_value = None

            roll = random.randint(1, sides)

            if stat_value is None:
                line = f"> 🎲 [{char_name}] {stat_name or 'd' + str(sides)} 굴림: **{roll}** / {sides}"
                logic_line = (
                    f"- {char_name} {stat_name}({sides}면, 가중치 {weight:+d}) "
                    f"→ {roll} (스탯 미확인, 결과 해석 보류)"
                )
            else:
                target = stat_value + weight
                is_success = roll <= target
                crit = ""
                if 5 <= target <= 16:
                    if roll in (1, 2):
                        crit = " 🌟대성공"
                    elif roll in (sides - 1, sides):
                        crit = " 💥대실패"
                result_text = ("성공 🟢" if is_success else "실패 🔴") + crit
                weight_str = f"{stat_value}{weight:+d}={target}" if weight else f"{stat_value}"
                line = (
                    f"> 🎲 [{char_name}] **{stat_name}** 판정 (1~{sides}, 기준치 {weight_str}) "
                    f"→ **{roll}**  /  **{result_text}**"
                )
                logic_line = (
                    f"- {char_name} {stat_name} 판정: 1d{sides}={roll}, 기준치 {weight_str}, "
                    f"{result_text.replace('🟢', '').replace('🔴', '').replace('🌟', '').replace('💥', '').strip()}"
                )

            if game_ch:
                await game_ch.send(line)
                # 판정 결과 안내 메시지 (stat_value가 확인된 경우에만 출력)
                if stat_value is not None:
                    if "대성공" in crit:
                        announce = f"> 🌟 **{stat_name}** 판정이 **대성공**했습니다!"
                    elif "대실패" in crit:
                        announce = f"> 💥 **{stat_name}** 판정이 **대실패**했습니다!"
                    elif is_success:
                        announce = f"> ✅ **{stat_name}** 판정이 성공했습니다."
                    else:
                        announce = f"> ❌ **{stat_name}** 판정이 실패했습니다."
                    await game_ch.send(announce)
                core.write_log(session.session_id, "game_chat", f"[판정]: {line}")
            if master_ch:
                await master_ch.send(f"🤖 [GM 굴림]\n{line}")
            session.current_turn_logs.append(logic_line.lstrip("- "))
            results.append(logic_line)

        return results

    async def _dispatch_rolls(self, session, rolls: list, player_message: str, prior_roll_results: list):
        """ROLL 결정 시 플레이어에게 버튼 UI 전송."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        target_uid = core.get_uid_by_char_name(session, session.auto_gm_target_char)

        roll_descs = []
        for r in rolls:
            char_name = r.get("char_name") or session.auto_gm_target_char or "?"
            stat_name = r.get("stat") or ""
            sides = int(r.get("sides") or 20)
            weight = int(r.get("weight") or 0)
            stat_value = None
            uid = core.get_uid_by_char_name(session, char_name)
            if uid:
                profile = session.players.get(uid, {}).get("profile", {})
                if stat_name in profile:
                    try:
                        stat_value = int(profile[stat_name])
                    except (TypeError, ValueError):
                        pass
            if stat_value is not None:
                target = stat_value + weight
                w_str = f"({stat_value}{weight:+d}={target})" if weight else f"(기준치 {stat_value})"
                roll_descs.append(f"**{stat_name}** {w_str} 판정 ({sides}면체)")
            else:
                roll_descs.append(f"**{stat_name}** 판정 ({sides}면체)")

        desc_text = " / ".join(roll_descs)
        mention = f"<@{target_uid}>" if target_uid else "플레이어"

        view = GMRollView(
            cog=self,
            session=session,
            roll_specs=rolls,
            player_message=player_message,
            prior_roll_results=prior_roll_results,
            target_uid=target_uid,
        )

        roll_prompt_text = (
            f"> 🎲 {mention}, 판정이 필요합니다!\n"
            f"> {desc_text}\n"
            f"> 아래 버튼을 눌러 주사위를 굴리세요. (5분 내 미클릭 시 자동 굴림)"
        )
        if game_ch:
            await game_ch.send(roll_prompt_text, view=view)
            core.write_log(session.session_id, "game_chat", f"[판정 요청]: {desc_text}")
        if master_ch:
            await master_ch.send(
                f"🤖 **[GM ROLL]** 플레이어 버튼 대기 중...\n> {desc_text}"
            )

    # ─────────────────────────────────────────────────────────────
    # ROLL 결과 반영 계속 처리
    # ─────────────────────────────────────────────────────────────

    async def _continue_with_roll_results(self, session, player_message: str, roll_results: list):
        """GMRollView 버튼 클릭 후 굴림 결과를 반영하여 지시층위 재호출."""
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.auto_gm_active:
                return

            used_cost = session.total_cost - session.auto_gm_cost_baseline
            if session.auto_gm_cost_cap_krw is not None and used_cost >= session.auto_gm_cost_cap_krw:
                session.auto_gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await core.save_session_data(self.bot, session)
                return

            decision = await self._call_gm_logic(session, player_message, roll_results, master_ch)
            if not decision:
                await m_send("⚠️ GM 결정 호출 실패. 이번 발언 스킵.")
                return

            action = decision.get("action", "PROCEED").upper()
            reasoning = decision.get("reasoning", "")
            action_labels = {
                "ASK":     "🟡 ASK (명확화 요청)",
                "ROLL":    "🎲 ROLL (주사위 판정)",
                "PROCEED": "🟢 PROCEED (턴 진행)",
            }
            print(f"[GM/{session.session_id}] post-roll action={action} :: {reasoning[:120]}")
            await m_send(
                f"🤖 **[GM 판단 (굴림 후)]** {action_labels.get(action, action)}\n"
                f"> {reasoning[:200]}"
            )

            instruction = _clean_proceed_instruction(
                decision.get("proceed_instruction") or
                "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
            )
            if not instruction:
                instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

            await self._finish_proceed_and_continue(
                session, instruction, master_ch,
                event_assessment=decision.get("event_assessment", "ongoing"))

    # ─────────────────────────────────────────────────────────────
    # NARRATE 디스패치 (경량 캐시 기반 GM 응답)
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_narrate(self, session, narrate_instruction: str) -> str | None:
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

        core.write_log(session.session_id, "api", f"[GM NARRATE 요청]\n{narrate_prompt}")

        # NOTE: max_output_tokens를 설정하지 않음 — PROCEED(_execute_proceed)와 동일한 방침.
        # DEFAULT_MODEL(gemini-3-flash-preview)은 thinking 모델이므로, max_output_tokens를
        # 지정하면 내부 thinking 토큰까지 한도에 포함되어 실제 텍스트 출력이 거의 없는
        # MAX_TOKENS 조기 종료가 발생한다. 출력 길이는 프롬프트의 "300자 이내" 지시로 제어한다.
        try:
            if session.cache_name:
                config = types.GenerateContentConfig(
                    cached_content=session.cache_name,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )
            else:
                config = types.GenerateContentConfig(
                    system_instruction=self.bot.system_instruction,
                    temperature=0.65,
                    safety_settings=core.TRPG_SAFETY_SETTINGS,
                )

            # PROCEED와 동일한 구조: typing()은 API 호출만 감싸고,
            # 출력(stream_text_to_channel)은 typing 컨텍스트 밖에서 실행한다.
            if game_ch:
                async with game_ch.typing():
                    response = await asyncio.to_thread(
                        self.bot.genai_client.models.generate_content,
                        model=core.DEFAULT_MODEL,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                        config=config,
                    )
            else:
                response = await asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                    config=config,
                )
        except Exception as e:
            print(f"[GM] NARRATE 호출 실패: {type(e).__name__} - {e}")
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
            session.total_cost += cost
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

        core.write_log(session.session_id, "api", f"[GM NARRATE 응답]\n{narrate_text}")

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
                    await core.maybe_send_speaker_image(game_ch, session, speaker)
                    formatted = core.format_dialogue_block(speaker, content)
                    await core.stream_text_to_channel(
                        self.bot, game_ch, formatted,
                        words_per_tick=5, tick_interval=1.5, quote_prefix=False
                    )
                else:
                    await core.stream_text_to_channel(
                        self.bot, game_ch, paragraph,
                        words_per_tick=5, tick_interval=1.5
                    )

        # current_turn_logs에 추가 — PROCEED 시 AI가 맥락을 볼 수 있도록
        session.current_turn_logs.append(f"[진행자 (GM)]: {narrate_text}")
        return narrate_text

    # ─────────────────────────────────────────────────────────────
    # PROCEED 디스패치
    # ─────────────────────────────────────────────────────────────

    async def _dispatch_proceed(self, session, instruction: str):
        """기존 GameCog._execute_proceed를 호출하여 묘사 생성·연출."""
        game_cog = self.bot.get_cog("GameCog")
        if not game_cog:
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send("⚠️ GameCog를 찾을 수 없어 자동 진행을 중단합니다.")
            return None

        master_ch = self.bot.get_channel(session.master_ch_id)
        if master_ch:
            # 지시사항 전문을 표시(잘림 방지). 1900자 초과 시 분할 전송.
            await master_ch.send("🤖 **[GM PROCEED]**")
            CHUNK = 1900
            for i in range(0, len(instruction), CHUNK):
                await master_ch.send(f"> {instruction[i:i + CHUNK]}")

        # NOTE: PROCEED 직전에 이번 턴 컨텍스트(NARRATE/ASK/ROLL 중간 기록)와 지시사항을 스냅샷.
        # _execute_proceed 내부에서 current_turn_logs가 초기화되므로 반드시 먼저 캡처해야 함.
        context_snapshot = list(session.current_turn_logs)
        prev_raw_count = len(session.raw_logs)

        # ③ 턴 연속성 강화(엔진 병행): 묘사 AI가 직전 턴을 되감지 않도록 지시에 연속성 directive 부착.
        # (원본 instruction은 위에서 이미 표시·아래 이력 저장에 사용하므로, 실행용으로만 증강.)
        exec_instruction = instruction + (
            "\n\n[연속성 지시] 직전 턴이 끝난 시점의 시간·공간·상태에서 곧바로 이어서 묘사할 것. "
            "이미 완료되었거나 서술된 행동·장면 전환·이동을 되풀이하거나 시간을 되감지 말고, "
            "지나간 장면에 인물의 행동·대사를 소급 삽입하지 말 것."
        )
        result = await game_cog._execute_proceed(
            session, exec_instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX
        )

        # PROCEED 완료 후 AI 출력 요약 (최대 500자)
        ai_summary = ""
        new_entries = session.raw_logs[prev_raw_count:]
        for content in reversed(new_entries):
            if getattr(content, "role", None) == "model":
                try:
                    text = content.parts[0].text
                    ai_summary = text[:500] + ("..." if len(text) > 500 else "")
                except Exception:
                    pass
                break

        # 이력 누적 (최근 5개 유지)
        if not hasattr(session, "auto_gm_proceed_history"):
            session.auto_gm_proceed_history = []
        session.auto_gm_proceed_history.append({
            "turn_num": session.turn_count,
            "instruction": instruction,
            "context": context_snapshot,
            "ai_summary": ai_summary,
        })
        if len(session.auto_gm_proceed_history) > 5:
            session.auto_gm_proceed_history = session.auto_gm_proceed_history[-5:]

        # [방안 2] narrative_plan.current_event.progress 자동 갱신
        # ai_summary 앞 150자를 현재 진행 상황 한줄 메모로 덮어씀.
        if ai_summary and getattr(session, "narrative_plan", {}).get("current_event"):
            session.narrative_plan["current_event"]["progress"] = ai_summary[:150]

        # ── 추출층위 (묘사 스트리밍과 동시 실행) ──
        # 기존 _update_world_timeline을 흡수했다. 세계 타임라인 갱신은
        # _run_extraction 내부에서 core.to_world_timeline으로 처리된다.
        if ai_summary:
            asyncio.create_task(self._run_extraction(session, ai_summary))

        return result


    # ─────────────────────────────────────────────────────────────
    # 추출층위 — 묘사 출력물에서 세계 상태·수치 추출
    # ─────────────────────────────────────────────────────────────

    async def _run_extraction(self, session, ai_output_text: str, master_ch=None) -> dict | None:
        """
        추출층위 — 묘사 출력물에서 공통·시나리오별 타겟 값을 추출한다.

        [동시 실행]
        묘사 스트리밍과 병행되므로, 실패해도 이미 출력된 묘사를 되돌릴 수 없다.
        따라서 일반적인 턴 취소 규정을 적용하지 않고 추출만 재시도한다.
        재시도까지 실패하면 session.extraction_pending을 세워 다음 턴을 차단하고,
        게임 채널에 재시도 버튼을 배치한다.

        [캐시 미사용] 출력물 판독에 룰북이 불필요하므로 캐시를 읽지 않는다.
        """
        targets = core.build_extraction_targets(session)
        prev_tl = getattr(session, "world_timeline", {}) or {}
        prev_summary = (
            f"위치={prev_tl.get('current_location', '미확인')}, "
            f"시간대={prev_tl.get('time_of_day', '미확인')}, "
            f"날짜={prev_tl.get('current_date', '미확인')}"
        ) if prev_tl else "(없음)"

        user_prompt = (
            "[추출 항목]\n" + "\n".join(f"- {t}" for t in targets) + "\n\n"
            f"[직전까지의 세계 상태]\n{prev_summary}\n\n"
            f"[이번 묘사문]\n{ai_output_text[:3000]}"
        )
        core.write_log(session.session_id, "api", f"[추출층위 요청 - Payload]\n{user_prompt}")

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=core.EXTRACTION_RESPONSE_SCHEMA,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
            # 추출은 묘사문에서 값을 읽어 옮기는 기계적 작업이다.
            # 실측 결과 출력의 90%가 사고 토큰이었고(1039/1148) 실제 JSON은
            # 109토큰에 불과했다. 사고 예산을 제한해 낭비를 줄인다.
            thinking_config=types.ThinkingConfig(thinking_budget=EXTRACTION_THINKING_BUDGET),
        )

        result = None
        for attempt in range(EXTRACTION_MAX_RETRIES):
            try:
                response = await asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=EXTRACTION_MODEL,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                print(f"[GM] 추출층위 호출 실패(시도 {attempt + 1}): {type(e).__name__} - {e}")
                continue

            try:
                meta = response.usage_metadata
                in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
                breakdown = core.calculate_text_gen_cost_breakdown(
                    EXTRACTION_MODEL, input_tokens=in_tokens,
                    output_tokens=out_tokens, cached_read_tokens=cached_tokens,
                )
                cost = breakdown["total_krw"]
                session.total_cost += cost
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
                    "manifest": [],
                })
            except Exception as e:
                print(f"[GM] 추출층위 비용 정산 실패: {e}")

            result = core.parse_extraction(response.text or "")
            if result:
                break
            print(f"[GM] 추출층위 응답 파싱 실패(시도 {attempt + 1})")

        if not result:
            # 재시도 실패 → 다음 턴 차단 + 재시도 버튼
            session.extraction_pending = True
            session.extraction_retry_ctx = {"text": ai_output_text[:3000]}
            await core.save_session_data(self.bot, session)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await game_ch.send(
                    "⚠️ 턴 정보 정리 중 문제가 발생했습니다.\n"
                    "아래 버튼으로 다시 시도해 주십시오. 완료 전까지 다음 턴은 진행되지 않습니다.",
                    view=ExtractionRetryView(self.bot),
                )
            if master_ch:
                await master_ch.send("⚠️ 추출층위 실패 — 다음 턴 차단됨. 재시도 버튼 배치.")
            return None

        # 성공 — 세계 타임라인 흡수 갱신 (기존 _update_world_timeline 대체)
        # 시간선 정량화 — 일/24시간 단위 정수 필드를 함께 보관한다.
        session.world_timeline = core.quantify(
            session, core.to_world_timeline(result, prev_tl))
        session.last_extraction = result
        session.extraction_pending = False
        session.extraction_retry_ctx = {}

        # 수치 판단 적용 — 임계값 비교는 코드가 전담한다(모델은 기준을 모른다).
        applied = core.apply_extraction(session, result)
        if applied["applied"] or applied["cleared"]:
            print(
                f"[GM/{session.session_id}] 상태 적용: "
                f"부여={applied['applied']} 해제={applied['cleared']}"
            )

        core.write_log(
            session.session_id, "api",
            f"[추출층위 결과]\n{json.dumps(result, ensure_ascii=False, indent=2)}"
        )
        if master_ch:
            report = f"🔎 **[추출층위]** {core.summarize_for_report(result)}"
            if applied["applied"]:
                report += f"\n> 상태 부여: {', '.join(applied['applied'])}"
            if applied["cleared"]:
                report += f"\n> 상태 해제: {', '.join(applied['cleared'])}"
            await master_ch.send(report)
        return result

    async def _verify_proceed_instruction(self, session, instruction: str,
                                           player_message: str, master_ch) -> str:
        """
        지시층위가 생성한 proceed_instruction에서 플레이어 자율성 침해 여부를 검증한다.
        위반 감지 시 corrected_instruction으로 교체하고 마스터 채널에 알림.

        Args:
            instruction (str): 지시층위가 생성한 proceed_instruction
            player_message (str): 플레이어의 원본 발언 (선언된 행동 확인용)

        Returns:
            str: 검증 통과 또는 수정된 proceed_instruction
        """
        user_prompt = (
            f"[플레이어 선언 행동]\n{player_message}\n\n"
            f"[GM proceed_instruction]\n{instruction}\n\n"
            "위 proceed_instruction이 플레이어가 선언하지 않은 PC 행동·발언·내면을 포함하는지 검증하십시오."
        )

        try:
            config = types.GenerateContentConfig(
                system_instruction=PROCEED_VERIFIER_SYSTEM_INSTRUCTION,
                temperature=0.1,
                response_mime_type="application/json",
                response_schema=PROCEED_VERIFY_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=[types.Content(role="user",
                                        parts=[types.Part.from_text(text=user_prompt)])],
                config=config,
            )
        except Exception as e:
            print(f"[GM] PROCEED 검증 실패 (원본 사용): {e}")
            return instruction

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, _cached_tokens, thought_tokens = core.extract_token_usage(meta)
            breakdown  = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
            cost = breakdown["total_krw"]
            session.total_cost += cost
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}PROCEED 자기 검증",
                                 in_tokens, 0, out_tokens, cost, session.total_cost)
        except Exception:
            pass

        raw_text = response.text or ""
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                result = json.loads(cleaned)
            except Exception:
                return instruction

        if result.get("has_violation"):
            detail   = result.get("violation_detail", "")
            corrected = result.get("corrected_instruction", instruction) or instruction
            print(f"[GM/{session.session_id}] PROCEED 위반 감지: {detail[:120]}")
            if master_ch:
                await master_ch.send(
                    f"⚠️ **[GM 검증]** proceed_instruction에 PC 자율성 침해 감지 — 자동 수정\n"
                    f"> 위반: {detail[:200]}\n"
                    f"> 수정 후: {corrected[:200]}"
                )
            return _clean_proceed_instruction(corrected)

        return instruction

    # ─────────────────────────────────────────────────────────────
    # 방안 6 — 2단계 사고 서사 방향성 시뮬레이터
    # ─────────────────────────────────────────────────────────────

    async def _simulate_narrative_directions(self, session, player_message: str,
                                              master_ch) -> dict | None:
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

        user_prompt = (
            f"[현재 턴]: {session.turn_count + 1}\n"
            f"{tl_note}"
            f"\n[최근 게임 로그]\n{recent_str}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n\n"
            "[지시사항]\n"
            "위 상황에서 다음에 일어날 수 있는 서사 방향성 2~3개를 세계관 논리에 근거하여 평가하십시오.\n"
            "반드시 시나리오 캐시의 세력 정보·지역 규칙·세계관 설정을 인용하여 판단하십시오."
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
                cached_content=cache_name,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=NARRATIVE_DIRECTION_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.DEFAULT_MODEL,
                contents=sim_contents,
                config=config,
            )
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
            session.total_cost += cost
            core.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}서사 방향성 시뮬레이션",
                                 in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "서사 설계자(방향성)", "cost": cost,
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
                print(f"[GM] 서사 설계 JSON 파싱 실패: {e}")
                return None

        core.write_log(session.session_id, "api",
                       f"[서사 방향성 시뮬레이션]\n{json.dumps(sim_data, ensure_ascii=False, indent=2)}")
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

    async def _init_narrative_and_start(self, session):
        """
        !자동 시작 직후 호출. 서사 계획이 없으면 새로 수립한 뒤 첫 라운드를 시작한다.
        이미 계획이 있으면 수립 없이 바로 라운드를 시작한다.
        백그라운드 태스크로 실행됨.
        """
        if not session.narrative_plan:
            await self._plan_narrative(session, "init")
        await self._start_round(session)

    async def _update_narrative_progress(self, session, event_assessment: str, master_ch):
        """
        PROCEED 완료 후 호출. 서사 재계획 트리거를 판정한다.

        [재계획 판정 — 수치 기반]
        기존의 '3턴 주기 강제 재계획'은 삭제되었다. 서사 상태와 무관하게
        주기만으로 재계획을 돌리면 불필요한 호출이 누적되기 때문이다.
        대신 추출층위가 산출한 quest_progress 수치를 임계값과 대조해 판정한다.
            deviation >= quest_deviated  → 전체 재수립 (mid_plan 포함)
            advance   >= quest_advance   → 순간 계획만 재수립
        지시층위의 event_assessment는 보조 신호로 유지한다(수치가 없을 때의 폴백).
        """
        plan = getattr(session, "narrative_plan", {})
        if not plan:
            return

        # completed/deviated → 즉시 재계획
        if event_assessment == "completed":
            # 순간 계획만 재수립 — mid_plan 유지
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 순간 사건 완료 감지\n"
                    f"> 중규모 계획을 유지하며 다음 순간 계획을 수립합니다..."
                )
            asyncio.create_task(self._plan_narrative(session, "completed", full_replan=False))
        elif event_assessment == "deviated":
            # mid_plan 포함 전부 재수립
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 경로 이탈 감지\n"
                    f"> 중규모 계획 포함 전체 재수립합니다..."
                )
            asyncio.create_task(self._plan_narrative(session, "deviated", full_replan=True))
        elif event_assessment == "resolving" and master_ch:
            await master_ch.send(
                f"📖 **[서사 계획]** 현재 순간 사건이 마무리 단계에 진입했습니다 (resolving)."
            )

        # ── 수치 기반 재계획 판정 (설계문서 3) ──
        # 추출층위가 산출한 quest_progress를 임계값과 대조한다.
        # event_assessment로 이미 트리거된 경우는 중복을 피한다.
        if event_assessment in ("completed", "deviated"):
            return

        ex = getattr(session, "last_extraction", {}) or {}
        qp = ex.get("quest_progress") or {}
        try:
            advance = int(qp.get("advance", 0))
            deviation = int(qp.get("deviation", 0))
        except (TypeError, ValueError):
            return

        th = core.get_thresholds(session)
        if deviation >= th["quest_deviated"]:
            plan["last_planned_turn"] = session.turn_count
            session.narrative_plan = plan
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 이탈 수치 {deviation} (임계 {th['quest_deviated']}) "
                    f"→ 중규모 계획 포함 전체 재수립합니다."
                )
            asyncio.create_task(
                self._plan_narrative(session, "deviated", full_replan=True,
                                     context_note=f"추출 이탈 수치 {deviation}")
            )
        elif advance >= th["quest_advance"]:
            plan["last_planned_turn"] = session.turn_count
            session.narrative_plan = plan
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 진행 수치 {advance} (임계 {th['quest_advance']}) "
                    f"→ 순간 계획을 재수립합니다."
                )
            asyncio.create_task(
                self._plan_narrative(session, "completed", full_replan=False,
                                     context_note=f"추출 진행 수치 {advance}")
            )

    async def _plan_narrative(self, session, trigger_reason: str = "init",
                               context_note: str = "", full_replan: bool = True) -> bool:
        """
        LOGIC_MODEL을 호출하여 서사 계획을 수립하거나 갱신한다.

        Args:
            session: TRPGSession
            trigger_reason: "init" | "completed" | "deviated" | "manual"
            context_note: GM이 추가한 메모 (재계획 시 계획 수립 프롬프트에 포함)
            full_replan: True이면 mid_plan 포함 전부 재수립.
                         False이면(completed) mid_plan을 유지하고 순간 계획만 갱신.

        Returns:
            bool: 성공 여부
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        # ── 시나리오 정보 ──
        story_guide = session.scenario_data.get("story_guide", "")
        worldview   = session.scenario_data.get("worldview", "")

        # ── 최근 게임 로그 (턴 개수 6개 유지, 각 턴은 온전 원문) ──
        recent_log_lines = []
        for content in session.raw_logs[-6:]:
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
            f"세계관: {worldview[:600] if worldview else '(없음)'}\n"
            f"스토리 가이드: {story_guide[:1200] if story_guide else '(없음)'}\n\n"
            f"[현재 게임 상황]\n"
            f"진행 턴: {session.turn_count}\n"
            f"PC 상태:\n{pc_info}\n"
            f"압축 기억:\n{memory_str[:1000]}\n\n"
            f"[최근 게임 로그]\n{recent_logs_str}\n"
            f"{existing_plan_block}"
            f"{context_note_block}\n"
            "[출력 지시]\n"
            "위 정보를 바탕으로 GM이 활용할 서사 계획을 JSON 스키마에 맞게 수립하십시오."
        )

        core.write_log(session.session_id, "api",
                       f"[서사 계획 요청 - trigger={trigger_reason}]\n{user_prompt}")

        try:
            config = types.GenerateContentConfig(
                system_instruction=NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=NARRATIVE_PLAN_SCHEMA,
                safety_settings=core.TRPG_SAFETY_SETTINGS,
            )
            response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
                config=config,
            )
        except Exception as e:
            print(f"[GM] 서사 계획 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 서사 계획 수립 실패: {type(e).__name__}")
            return False

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
            session.total_cost += cost
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
                print(f"[GM] 서사 계획 JSON 파싱 실패: {e}\n원문: {raw_text[:400]}")
                if master_ch:
                    await master_ch.send("⚠️ 서사 계획 JSON 파싱 실패. 기존 계획을 유지합니다.")
                return False

        # ── 버전·타임스탬프 기록 ──
        plan["plan_version"]      = session.narrative_plan.get("plan_version", 0) + 1
        plan["last_planned_turn"] = session.turn_count
        session.narrative_plan    = plan
        await core.save_session_data(self.bot, session)

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

        if master_ch:
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

            await master_ch.send(embed=embed)

        core.write_log(session.session_id, "api",
                       f"[서사 계획 결과 ({trigger_label})]\n{json.dumps(plan, ensure_ascii=False, indent=2)}")
        return True


async def setup(bot):
    """디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수."""
    await bot.add_cog(GMCog(bot))

    # persistent view 등록 — 봇 재시작 후에도 추출 재시도 버튼이 동작하도록 한다.
    # 추출 실패는 다음 턴을 차단하므로, 버튼이 죽으면 세션이 영구 정지한다.
    # 중복 등록은 무해하지만 방어적으로 플래그를 둔다.
    if not getattr(bot, "_extraction_view_registered", False):
        bot.add_view(ExtractionRetryView(bot))
        bot.add_view(RewindView(bot))
        bot._extraction_view_registered = True
