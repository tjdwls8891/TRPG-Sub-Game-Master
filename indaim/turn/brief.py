"""판단층위·지시층위 사용자 프롬프트 조립 — 문구는 옮기기 전과 바이트 동일(cogs/gm.py에서 옮김)."""
from __future__ import annotations

import re

from indaim.app import profile_gen as _profile_gen
from indaim.app import utils as _utils
from indaim.ledger import cost as _cost
from indaim.rules.checks import judgment as _judgment
from indaim.rules.items import items as _items
from indaim.rules.pursuit import pursuit as _pursuit
from indaim.rules.space import places as _places
from indaim.rules.vitals import status_rules as _status_rules
from indaim.scenario import quest as _quest
from indaim.turn import player_world as _player_world
from indaim.turn import turn_preparation as _turn_preparation
from indaim.turn import turn_world as _turn_world
from indaim.turn.declaration import _gate_judgment_block
from indaim.turn.roll_sides import _npc_stats
from indaim.turn.roll_sides import _pc_skills


# ========== [GM 상수] ==========
# NOTE: 지시층위 호출 시 한 플레이어 발언당 내부 루프 반복 상한.
# 판단층위에 주입할 최근 로그 개수 (캐시 미사용이므로 맥락을 직접 공급)
JUDGMENT_RECENT_LOGS = 5

# NOTE: 같은 플레이어 발언에 대한 ASK 누적 상한. 초과 시 강제 PROCEED.
MAX_CLARIFY_PER_MESSAGE = 2

# NOTE: 같은 플레이어 발언에 대한 NARRATE 누적 상한. 초과 시 강제 PROCEED.
MAX_NARRATE_PER_MESSAGE = 7


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
        prof_lines.append(f"- {c_name}: [스탯] {_items.prompt_profile(session, c_name, p_data.get('profile')) or '(미배분)'}")
        if p_data.get("appearance"):
            prof_lines.append(f"    외형: {p_data['appearance']}")
        c_res = (session.resources or {}).get(c_name, {})
        if c_res:
            prof_lines.append("    소지 자원: " + ", ".join(f"{k} {v}" for k, v in c_res.items()))
        c_sta = (session.statuses or {}).get(c_name, [])
        _defs = _utils.get_merged_status_effects(session.scenario_data or {})
        if world_turn is not None:
            # v7.47(S2): 동적 세계 PC — 상태·단계 불이익은 코드가 굴림 분야로 더한다(status_mod는 0)
            _clk = (getattr(session, "status_clock", None) or {}).get(c_name) or {}
            _now = (getattr(session, "world_clock", None) or {}).get("minute_index")
            prof_lines.append("    상태이상: " + (", ".join(_status_rules.label(_defs, x, _clk, _now) for x in c_sta)
                                                if c_sta else "없음")
                              + (" — 판정 불이익은 코드가 굴림 분야에 맞춰 더한다(status_mod 0)" if c_sta else ""))
            for _sn in c_sta:
                _e = _defs.get(_sn)
                if isinstance(_e, dict):
                    _m = _status_rules.mods_at(_e, _status_rules.effective_stage(_e, _clk, _sn, _now))
                    prof_lines.append(f"    · {_status_rules.label(_defs, _sn, _clk, _now)}: "
                                      f"{_status_rules.mods_text(_m)} / 제거조건=[{_e.get('remove_condition', '')}]")
        else:
            prof_lines.append("    상태이상: " + (", ".join(c_sta) if c_sta else "없음"))
        # v7.12: 상태 영향은 코드가 고정 가중치로 더하지 않는다 — 판단층위가 아래 정의를 보고 status_mod 하나로 종합.
        for _sn in (c_sta if world_turn is None else ()):
            _e = _defs.get(_sn)
            if not isinstance(_e, dict):
                continue
            _w = _e.get("weight")
            prof_lines.append(
                f"    · {_sn}: 적용조건=[{_e.get('apply_condition', '')}] / 제거조건=[{_e.get('remove_condition', '')}]"
                + (f" / 저작 가중치 {int(_w):+d}(옛 D20 척도 — T로는 ×2, 참고용)" if isinstance(_w, int) and _w else ""))
        # v7.46(A): 스킬 쓰임 — rolls의 skill은 이 목록에서만(스키마 열거)
        _sk = _pc_skills(session, p_data)
        if _sk:
            prof_lines.append("    스킬(쓰임): " + " / ".join(
                f"{n}({_profile_gen.skill_note(session.scenario_data, n) or '쓰임 미저작'}"
                + ("·상시 — 코드가 분야에 맞으면 자동" if not _judgment.is_invoked(session.scenario_data, n) else "")
                + ")" for n in _sk))
        if world_turn is not None:
            _eb = _items.equip_block(_items.equip_rows(
                session, c_name, _items.definitions_of(world_turn.ctx.definition)), session.scenario_data)
            if _eb:
                prof_lines.append("    " + _eb.replace("\n", "\n    "))
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
        _scene, _projs = _turn_world.scene_npc_projections(world_turn.ctx, None, session)
        _pursuers = set(_pursuit.configs(world_turn.ctx.definition))
        _hidden = _turn_world.pc_has_role(session, _status_rules.ROLE_CONCEALED)
        _ents = [_npc_entry(p["display_name"], _npc_stats(session, p["display_name"], world_turn),
                            p.get("relevant_statuses"))
                 + (("[위협 — 추적자: PC가 숨어 있다. 들킬지는 CONTEST(그의 감각 대 PC의 은밀)로]" if _hidden
                     else "[위협 — 추적자: PC와 마주침]") if p.get("actor_id") in _pursuers else "")
                 for p in _projs]
        lines.append("[현재 장면 NPC — 정본 위치 기준]\n" + (", ".join(_ents) if _ents else "(없음)"))
    else:
        npc_names = list((session.npcs or {}).keys())
        if npc_names:
            lines.append("[세션 등장 NPC]\n" + ", ".join(
                _npc_entry(n, _npc_stats(session, n), (session.statuses or {}).get(n)) for n in npc_names[:30]))
    lines.append("※ NPC 능력치는 판정 계산 전용 비공개 값이다 — bridge_message에 쓰지 말 것. 능력 차이는 코드가 "
                 "능력 보정(A)으로 반영하므로 difficulty·method_mod에 다시 넣지 말 것. 능력치가 없는 인물과는 "
                 "CONTEST를 만들지 말 것."
                 + (" 대결 상대 NPC가 가진 무기·보호구의 보정도 코드가 더하므로 opponent의 method_mod에 넣지 말 것."
                    if world_turn is not None else ""))

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
        _rel_block = _turn_world.relation_judgment_block(world_turn.ctx, world_turn.plan, session)
        if _rel_block:
            lines.append(_rel_block)                 # v7.39(N3) 관계 단계 — 사회적 판정 보정 근거
        try:                                         # v7.51 파티원 명단 — 명령받은 파티원만 굴림
            _pb = _turn_world.party_block(world_turn.ctx, world_turn.plan, session, for_judgment=True)
        except Exception:
            _pb = ""
        if _pb:
            lines.append(_pb)
    else:
        try:
            hint = _places.build_move_hint(session, player_message)
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
                         + _turn_world.discovery_rows_text(discovery_rows))
        try:                                       # v7.45.4: 저작 단서 실물 — 지목해 찾아도 주변 발견 제안이 아니다
            _finds = _turn_world.find_object_names(world_turn.ctx.definition, session, world_turn.ctx.lead_pc,
                                                       _turn_world.pc_place(world_turn.ctx, session))
        except Exception:
            _finds = []
        if _finds:
            lines.append("[이 장소에 실재가 코드로 확정된 조사 대상 — 선언이 이것을 찾거나 가리키면 일반 조사다"
                         "(ambient_proposal 아님). 플레이어에게 이 목록을 알리지 말 것]\n" + ", ".join(_finds))
    lines.append(f"[플레이어 신규 발언]\n{player_message}")
    return "\n\n".join(lines)


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
    for uid, p in _turn_preparation.projected_players(session).items():
        if p.get("name") == target_char:
            stats = ", ".join([f"{k}:{v}" for k, v in _items.prompt_profile(session, target_char, p.get("profile", {})).items()
                               if isinstance(v, (int, str))])
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
    if narrative_plan and not _player_world.legacy_narrative_retired(session):
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
        from indaim.rules.space.places import load_places
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
    _allowed = _turn_world.media_allowed_places_for(world_turn.ctx, _wplan, session) \
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
    merged_statuses = _utils.get_merged_status_effects(session.scenario_data)
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
        world_tl_block = _turn_world.instruction_world_state_block(
            world_turn, session, world_tl, hold=(action or "").upper() == "NARRATE")

    # 정보 인지 원장 블록 (지속형): 비공개·플롯 정보별 '누가 아는가'의 확립된 기록.
    # 지시층위가 info_access 필드로 갱신하며, 적·NPC가 도달 경로 없는 정보로 행동하지 않도록 하는 진실 기준.
    # H07-C: 동적 세계 세션은 레거시 info_ledger를 진실 기준으로 싣지 않는다(정보·인지가 권위).
    info_ledger = [] if _player_world.legacy_narrative_retired(session) else (
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
        _pb = (_turn_world.dynamic_place_block(world_turn.ctx, world_turn.plan, session)
               if world_turn is not None else _places.build_place_block(session))
        if _pb:
            place_block = "\n" + _pb
            manifest.append("장소 정보")
    except Exception as e:
        print(f"[지시층위] 장소 블록 실패: {e}")
    # H05 R3: DYNAMIC — 현재 장면 인물은 정본 배치 + CurrentNpcProjection(읽기 전용)
    if world_turn is not None:
        try:
            place_block += _turn_world.scene_npc_block(world_turn.ctx, world_turn.plan, session)
            manifest.append("현재 장면 인물")
        except Exception as e:
            print(f"[지시층위] 현재 장면 인물 블록 실패: {e}")
        try:
            _gb = _turn_world.event_guide_block(world_turn.ctx, world_turn.plan, session)   # H07 R-2
            if _gb:
                place_block += _gb
                manifest.append("사건 국면 지침")
            _eb = _turn_world.ending_block(world_turn.ctx, world_turn.plan, session)        # v7.44 E3
            if _eb:
                place_block += _eb
                manifest.append("결말")
            _db = _turn_world.dying_block(session, world_turn.plan)                         # v7.48 E2
            if _db:
                place_block += _db
                manifest.append("죽어 감")
            _tb = _turn_world.threat_block(world_turn.ctx, world_turn.plan, session)        # v7.48 E3
            if _tb:
                place_block += _tb
                manifest.append("위협")
            _pb = _turn_world.party_block(world_turn.ctx, world_turn.plan, session)         # v7.51 파티
            if _pb:
                place_block += _pb
                manifest.append("파티")
            _kb = _turn_world.pc_knowledge_block(world_turn.ctx, world_turn.plan, session)  # H07-C
            if _kb:
                place_block += _kb
                manifest.append("PC 인지")
            _db = _turn_world.discovery_block(world_turn.ctx, world_turn.plan, session)   # v7.14
            if _db:
                place_block += _db
                manifest.append("현장 발견")
            # v7.17: 정본 소지(PC·장면 인물)·현장 물건·사용 불가 상태 — 캐시 초기 소지품보다 우선
            _wc = world_turn.ctx
            _sc, _pj = _turn_world.scene_npc_projections(_wc, world_turn.plan, session)
            _pcn = ((session.players or {}).get(_wc.uid_of.get(_wc.lead_pc)) or {}).get("name")
            if _pcn:
                place_block += _items.item_block(
                    session, pc_name=_pcn, scene_owner_names=[p.get("legacy_name") for p in _pj if p.get("legacy_name")],
                    scene_place=_sc, defs=_items.definitions_of(_wc.definition))
                manifest.append("정본 소지")
        except Exception as e:
            print(f"[지시층위] 사건 국면 지침 블록 실패: {e}")

    # 퀘스트 — 후보 목록을 여기서 제시해야 quest_choice로 고를 수 있다.
    # build_quest_block이 session._quest_offered를 채우고, 코드가 그것으로
    # 모델의 선택을 검증한다. 이 호출이 없으면 후보가 비어 아무것도 열리지 않는다.
    quest_block = ""
    try:
        _qb = "" if _player_world.legacy_narrative_retired(session) else _quest.build_quest_block(
            session,
            quest_state=_turn_preparation.projected_quest_state(session))   # H07-C: 동적 세계는 사건 지침이 대신
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


def _sys_text(config):
    """비캐시 호출의 시스템 지시 문자열(캐시 호출은 None — 지시가 캐시 안에 있다)."""
    t = getattr(config, "system_instruction", None)
    return t if isinstance(t, str) else None


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

# ========== [유틸리티 함수] ==========
def _cap_display(cap, *, is_cost: bool = False) -> str:
    """턴/비용 상한을 표시용 문자열로. None이면 '무제한'."""
    if cap is None:
        return "무제한"
    return _cost.format_cost(cap) if is_cost else f"{cap}턴"


def _interp_note(resolved: dict) -> str:
    """WP-F(POLICY-CACHE-01): 방금 확정된 시간 해석 청구를 확인 화면에 그대로 알린다(UI = 거래)."""
    ch = (resolved or {}).get("interpret_charge") or {}
    ink = int(ch.get("ink") or 0)
    if not ink:
        return ""
    if ch.get("complete"):
        return f"\n> 🧾 시간 해석 비용 **{ink}잉크**가 청구되었습니다(캐시와 별개 · 환불되지 않습니다)."
    return f"\n> 🧾 시간 해석 비용 {ink}잉크 청구가 기록되었습니다(계정 반영은 곧 재시도됩니다)."
