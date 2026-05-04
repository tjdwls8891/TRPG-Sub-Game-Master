# 프롬프트 빌더 — 턴 진행용 LLM 프롬프트 조립 및 기억 압축 프롬프트 생성
from .models import TRPGSession
from prompts import build_compression_prompt_text


# ========== [프롬프트 빌더(Prompt Builder)] ==========
class PromptBuilder:
    """
    TRPG 세션의 턴 진행을 위한 LLM 프롬프트를 단계별로 조립하는 빌더 클래스.

    할루시네이션을 통제하기 위해 '요약 -> 캐릭터 -> NPC 델타 -> 기억 -> 행동 -> 룰' 순서의
    엄격한 정보 주입 포맷을 시스템 레벨에서 강제.

    [NPC 주입 전략]
    모든 default_npcs는 캐시 룰북 [3. NPC 사전]에 전체 수록되므로 프롬프트에 중복 주입하지 않는다.
    프롬프트(add_npc_override_block)에는 아래 두 경우만 델타(차분)로 주입한다:
      1. !엔피씨 설정으로 details가 변경된 NPC  → 캐시 내용을 덮어씀
      2. 세션 중 resources / statuses가 부여된 NPC → 캐시에 없는 런타임 상태 동기화
    """

    def __init__(self, session: TRPGSession, gm_instruction: str):
        self.session = session
        self.gm_instruction = gm_instruction
        self.blocks = ["[현재 게임 상태]\n"]

        # keyword_memory 트리거 스캔에 사용되는 최근 로그 결합 문자열 (사전 연산)
        recent_texts = [c.parts[0].text for c in session.raw_logs[-10:]] + session.current_turn_logs
        self.recent_logs_combined = " ".join(recent_texts) + f" {gm_instruction}"

    def add_memory_block(self):
        # NOTE: cached_compressed_memory는 캐시 섹션 [9]에 포함되어 있으므로 프롬프트에 중복 주입하지 않는다.
        # compressed_memory는 마지막 캐시 재발급 이후 새로 누적된 기억만 포함한다.
        if self.session.compressed_memory:
            self.blocks.append(f"▶ 이전 상황 요약 (최근 압축 기억 — 절대 참조용):\n{self.session.compressed_memory}\n")
        return self

    def add_player_block(self):
        if self.session.players:
            block = "\n▶ 참가 플레이어 정보:\n"
            for uid, p_data in self.session.players.items():
                c_name = p_data['name']
                block += f"  - {c_name}: [스탯] {p_data['profile']}\n"
                if p_data.get("appearance"):
                    block += f"    * [외형]: {p_data['appearance']}\n"

                c_res = self.session.resources.get(c_name, {})
                c_stat = self.session.statuses.get(c_name, [])
                if c_res:
                    res_str = ", ".join([f"{k}: {v}" for k, v in c_res.items()])
                    block += f"    * [확정 소지 자원]: {res_str}\n"
                # NOTE: 상태이상이 없을 때도 명시적으로 "없음"을 출력하여,
                # 해제 후에도 AI가 이전 상태를 언급하는 오류를 방지한다.
                stat_str = ", ".join(c_stat) if c_stat else "없음"
                block += f"    * [현재 상태이상]: {stat_str}\n"
            self.blocks.append(block)
        return self

    def add_npc_override_block(self):
        # NOTE: default_npcs 전체가 캐시에 수록되므로, 프롬프트에는 캐시와 차이가 있는
        # NPC만 델타(delta)로 주입한다.
        #
        # 주입 대상:
        #   A. 설정 필드(details 또는 구조화 info_fields)가 default_npcs와 다른 NPC
        #      → 캐시 내용을 현재 값으로 덮어씀
        #   B. 설정 변경 없이 resources / statuses(runtime)만 존재하는 NPC
        #      → 캐시에 없는 런타임 상태 동기화
        #   → 두 조건 모두 해당 없는 NPC는 캐시로 충분하므로 스킵
        if not self.session.npcs:
            return self

        default_npcs = self.session.scenario_data.get("default_npcs", {})
        npc_template = self.session.scenario_data.get("npc_template", {})
        info_fields = npc_template.get("info_fields", []) if isinstance(npc_template, dict) else []
        # NOTE: NPC 스탯은 PC와 동일한 ability_stats 스키마를 사용한다.
        # has_stats=true이고 ability_stats가 있을 때만 스탯 오버라이드를 처리한다.
        ability_stats_list = self.session.scenario_data.get("ability_stats", [])
        npc_has_stats = (
            isinstance(npc_template, dict)
            and npc_template.get("has_stats")
            and bool(ability_stats_list)
        )
        # 가장 최근 캐시 재발급 시점의 세션 생성 NPC 스냅샷
        cached_session_npcs = getattr(self.session, "cached_session_npcs", {})
        lines = []

        for npc_name, npc_data in self.session.npcs.items():
            is_session_npc = npc_name not in default_npcs  # 세션에서 새로 생성된 NPC
            if is_session_npc:
                # 캐시에 등록된 세션 NPC라면 해당 스냅샷을 기준 데이터로 사용 → delta만 주입
                # 캐시에 없는 경우(재발급 전 생성) base_data = {} → 전체 프로파일 주입
                base_data = cached_session_npcs.get(npc_name, {})
                is_npc_in_cache = npc_name in cached_session_npcs
            else:
                base_data = default_npcs.get(npc_name, {})
                is_npc_in_cache = True  # default NPC는 항상 캐시에 있음

            # ── info_fields 변경 필드 추출 ──
            # 디폴트 NPC: 실제로 달라진 필드(delta)만 추출 → 토큰 절약
            # 세션 생성 NPC: base_data = {} 이므로 값 있는 필드 전체가 delta
            if info_fields:
                changed_info_fields = [
                    f for f in info_fields
                    if npc_data.get(f) != base_data.get(f)
                ]
                info_changed = bool(changed_info_fields)
            else:
                # 레거시 NPC: details 문자열 비교 (전체 교체)
                info_changed = npc_data.get("details", "") != base_data.get("details", "")
                changed_info_fields = []

            # ── 스탯 변경 감지 (ability_stats 스키마) ──
            n_stats = npc_data.get("stats", {})
            base_stats = base_data.get("stats", {})
            # 변경된 스탯 항목만 추출 (delta)
            if npc_has_stats:
                changed_stats = {
                    k: n_stats[k] for k in n_stats
                    if n_stats.get(k) != base_stats.get(k)
                }
                # base에는 있었지만 현재 없는 스탯은 빈 값으로 표시
                removed_stats = {k: "" for k in base_stats if k not in n_stats}
                delta_stats = {**changed_stats, **removed_stats}
            else:
                delta_stats = {}
            stats_changed = bool(delta_stats)

            # ── 런타임 resources/statuses 감지 ──
            n_res = self.session.resources.get(npc_name, {})
            n_stat = self.session.statuses.get(npc_name, [])
            base_res = base_data.get("resources", {})
            base_statuses = base_data.get("statuses", [])
            res_changed = n_res != base_res
            stat_changed = sorted(n_stat) != sorted(base_statuses)

            if not info_changed and not stats_changed and not res_changed and not stat_changed:
                continue

            # ── 오버라이드 블록 구성 ──
            entry = f"  - {npc_name}"

            if info_changed:
                if info_fields:
                    if is_session_npc and not is_npc_in_cache:
                        # 아직 캐시에 없는 세션 생성 NPC: 전체 프로파일 출력
                        field_lines = "\n".join(
                            f"    {f}: {npc_data.get(f, '')}"
                            for f in info_fields if npc_data.get(f)
                        )
                        entry += f" [전체 프로파일]:\n{field_lines}"
                    else:
                        # 캐시된 NPC(디폴트 또는 이전 재발급에서 캐시된 세션 NPC): delta만 출력
                        field_lines = "\n".join(
                            f"    {f}: {npc_data.get(f, '')}"
                            for f in changed_info_fields
                        )
                        entry += f" [필드 수정 — 이하 항목만 캐시 내용 대신 적용]:\n{field_lines}"
                else:
                    # 레거시 details: 전체 교체
                    entry += f" [설정 변경]: {npc_data.get('details', '')}"

            # 스탯 delta 출력: ability_stats 순서 보장
            if stats_changed:
                ordered = [(s, delta_stats[s]) for s in ability_stats_list if s in delta_stats]
                extra = [(k, v) for k, v in delta_stats.items() if k not in ability_stats_list]
                all_stats = ordered + extra
                label = "[스탯 (전체)]" if (is_session_npc and not is_npc_in_cache) else "[스탯 수정]"
                entry += f"\n    * {label}: {', '.join(f'{k}={v}' for k, v in all_stats)}"

            if res_changed and n_res:
                entry += f"\n    * [확정 소지 자원]: {', '.join(f'{k}: {v}' for k, v in n_res.items())}"
            if stat_changed and n_stat:
                entry += f"\n    * [현재 상태이상]: {', '.join(n_stat)}"
            lines.append(entry)

        if lines:
            block = "\n▶ [NPC 변경사항 / 런타임 상태] (캐시 룰북 [3. NPC 사전]보다 우선 적용):\n"
            block += "\n".join(lines) + "\n"
            self.blocks.append(block)

        return self

    def add_keyword_memory_block(self):
        # NOTE: 특정 키워드가 최근 로그에 등장할 때만 연관 기억을 활성화하여 동적 컨텍스트 최적화 수행.
        keyword_memories = self.session.scenario_data.get("keyword_memory", [])
        if keyword_memories:
            triggered_memories = set()
            for memory in keyword_memories:
                for kw in memory.get("keywords", []):
                    if kw in self.recent_logs_combined:
                        triggered_memories.add(memory.get("description", ""))
                        break

            if triggered_memories:
                block = "\n[키워드 연관 기억/설정 (최근 대화 기반)]\n"
                for desc in triggered_memories:
                    block += f"▶ {desc}\n"
                self.blocks.append(block)
        return self

    def add_recent_action_block(self):
        block = "\n[최근 플레이어 행동 및 대화 (판정 완료됨)]\n"
        if self.session.current_turn_logs:
            block += "\n".join(self.session.current_turn_logs) + "\n"
        else:
            block += "(특별한 대화 없음)\n"
        self.blocks.append(block)
        return self

    def add_note_block(self):
        if getattr(self.session, "note", ""):
            block = f"\n▶ 실시간 노트 (GM 직접 관리):\n{self.session.note}\n"
            self.blocks.append(block)
        return self

    def add_gm_instruction_block(self):
        block = f"\n[진행자(GM)의 판정 결과 및 지시사항]\n▶ {self.gm_instruction}\n\n"
        self.blocks.append(block)
        return self

    def add_rule_enforcement_block(self):
        block = f"[최종 지시] 캐시된 [시나리오 핵심 룰북]의 묘사 가이드와 위 GM의 지시사항을 최우선으로 반영하여 상황을 묘사하세요.\n"

        # 인물 대사 출력 형식 지시 (디스코드 후처리에서 자동으로 인물 헤더+이미지+말풍선으로 변환됨)
        block += (
            "▶ 명령 (인물 대사 마크업): 등장 인물이 직접 발화하는 대사는 반드시 별도의 문단으로 분리하고, "
            "다음 형식의 단일 라인으로만 출력하십시오:\n"
            "@대사:인물이름|대사 본문\n"
            "예) @대사:레비|…어때? 감각이 느껴져? 체온은 정상인데, 불편한 곳은?\n"
            "    @대사:김철수|결론만 말해. 수치로.\n"
            "이 마커가 포함된 문단은 시스템이 자동으로 인물 헤더와 말풍선 박스, 그리고 미디어 목록에 등록된 인물 이미지(존재 시)로 변환합니다. "
            "마커 외 추가 묘사(행동, 시선 등)는 같은 문단에 섞지 말고, 직전 또는 직후의 일반 문단으로 분리하여 작성하십시오. "
            "여러 인물이 연이어 발화하는 경우 각 발화마다 마커 한 줄씩 별도 문단으로 작성합니다.\n"
        )

        if self.session.scenario_data.get("status_code_block", ""):
            block += f"▶ 명령: 턴의 마지막에 반드시 룰북에 정의된 양식을 바탕으로 중괄호 내부 값을 기입하여 상태창 코드블럭을 출력하십시오. (현재 턴 수: {self.session.turn_count + 1}턴 기입)\n"
        self.blocks.append(block)
        return self

    def build(self) -> str:
        return "".join(self.blocks)

    @classmethod
    def build_prompt(cls, session, gm_instruction: str) -> str:
        """
        내부 블록 조립을 순차적으로 실행하여 완성된 문자열을 즉시 반환하는 파사드(Facade) 메서드.
        """
        return (cls(session, gm_instruction)
                .add_memory_block()
                .add_note_block()
                .add_player_block()
                .add_npc_override_block()
                .add_keyword_memory_block()
                .add_recent_action_block()
                .add_gm_instruction_block()
                .add_rule_enforcement_block()
                .build())


def build_compression_prompt(session: TRPGSession, log_text: str) -> str:
    """
    대화 기록을 무손실 압축하기 위한 요약 전용 프롬프트 생성.

    NOTE: 압축의 목표는 동일한 정보를 더 적은 토큰으로 표현하는 것이지,
    '무엇을 기억할 것인가'를 선별하는 것이 아니다.
    세션에서 발생한 모든 의미 있는 정보는 형태를 바꾸더라도 전부 보존한다.
    턴 단위 단락 구조를 강제하여 AI의 장기 참조 품질을 높인다.

    프롬프트 본문은 prompts.build_compression_prompt_text()에 위임하며,
    이 래퍼는 TRPGSession에서 필요한 값을 추출하는 역할만 담당한다.

    Args:
        session (TRPGSession): 현재 진행 중인 세션 객체
        log_text (str): 요약 대상이 되는 미압축 기록 문자열 집합

    Returns:
        str: AI 모델에 전송할 압축 지시 프롬프트 문자열
    """
    return build_compression_prompt_text(
        compressed_memory=session.compressed_memory or "없음",
        log_text=log_text,
    )
