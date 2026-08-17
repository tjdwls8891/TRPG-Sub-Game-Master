# 데이터 모델 — TRPGSession (단일 세션의 모든 상태를 담는 중앙 컨테이너)


# ========== [데이터 모델(Data Models)] ==========
class TRPGSession:
    """
    단일 TRPG 세션의 모든 상태와 데이터를 관리하는 데이터 모델 클래스.

    비동기 환경에서 데이터 파편화를 막기 위해 채널 메타데이터, 플레이어/NPC 상태, 자원, 로그 배열 등을
    하나의 캡슐화된 객체로 중앙 통제.

    Args:
        session_id (str): 세션의 고유 식별자 (UUID 기반)
        game_ch_id (int): 플레이어들이 참여하는 게임 채널의 ID
        master_ch_id (int): GM 전용 마스터 채널의 ID
        scenario_id (str): 로드된 시나리오 파일의 이름
        scenario_data (dict): 시나리오 JSON에서 로드된 원본 데이터
    """

    def __init__(self, session_id, game_ch_id, master_ch_id, scenario_id, scenario_data):
        self.session_id = session_id
        self.game_ch_id = game_ch_id
        self.master_ch_id = master_ch_id
        self.scenario_id = scenario_id
        self.scenario_data = scenario_data

        self.cache_name = None
        self.cache_obj = None

        self.players = {}
        self.npcs = {}
        self.resources = {}
        self.statuses = {}

        self.compressed_memory = ""
        self.raw_logs = []
        self.current_turn_logs = []
        self.uncompressed_logs = []
        self.note = ""

        self.cache_note = ""
        self.cache_created_at = 0.0
        self.cache_tokens = 0

        self.turn_count = 0
        self.is_started = False
        self.total_cost = 0.0

        self.volume = 0.3

        self.voice_client = None
        self.current_bgm = None
        self.is_bgm_looping = False

        self.is_processing = False
        self.is_compressing = False    # 자동 기억 압축 백그라운드 실행 중 여부 (런타임 전용, !재생성 경합 방지)
        self.last_turn_anchor_id = None

        # ========== [TTS 음성 더빙 — 실험 기능] ==========
        # NOTE: 옵트인. True일 때만 수동 !진행 묘사를 음성 채널에서 단일 나레이터 보이스로 읽어준다.
        #       (GM·NPC 개별 보이스는 현재 미적용)
        self.tts_enabled = False

        # 턴 진행 카테고리 배치 비용 로그 — PROCEED 직전에 플러시 후 초기화.
        # 형식: [{"label": str, "cost": float}, ...]
        # 지시층위 / NARRATE / 서사 계획 / auto compression 등이 여기 누적된다.
        self.turn_cost_log: list = []

        self.gm_typing_task = None

        # 가장 최근 캐시 업로드 시점의 룰북 원본 텍스트 (패딩 제외). !캐시 출력 디버그용.
        self.cache_text = ""

        # ========== [GM 서사 계획] ==========
        # NOTE: GM 전용. 사건(event) 단위의 서사 계획을 저장한다.
        # 구조: {"current_event": {...}, "next_event": {...}, "plan_version": int, "last_planned_turn": int}
        # !자동 시작 시 수립, PROCEED 완료 후 completed/deviated 평가 시 재수립.
        self.narrative_plan = {}

        # ========== [세계 물리 타임라인 (방안 B)] ==========
        # NOTE: GM 전용. PROCEED 완료 후 AI 출력에서 추출하여 갱신되는 세계 상태.
        # 세력 배치·지역 규칙 등 고차원 개연성 판단의 기준 데이터로 활용.
        # 구조: {"elapsed_minutes": int, "time_of_day": str, "weather": str,
        #        "current_location": str, "faction_context": str,
        #        "known_threats": str, "environmental_note": str,
        #        "last_updated_turn": int}
        self.world_timeline = {}

        # 가장 최근 캐시 재발급 시점의 세션 생성 NPC 스냅샷.
        # 이후 변경분만 delta로 주입하기 위한 기준 데이터. (재발급 전 없으면 빈 딕셔너리)
        self.cached_session_npcs = {}
        # 가장 최근 캐시 재발급 시점까지 누적된 압축 기억 (캐시 섹션 [9]에 포함됨).
        # 프롬프트에서는 이미 캐시에 있으므로 중복 주입하지 않는다.
        self.cached_compressed_memory = ""

        # ========== [GM 상태] ==========
        # NOTE: GM는 게임 채널의 플레이어 발언을 받아 AI가 GM 역할을 수행하는 옵트인 모드.
        #       기본은 비활성(False) — 활성화되어야만 on_message 리스너가 동작한다.
        self.auto_gm_active = False
        self.auto_gm_target_char = None        # GM이 대화할 PC 이름 (단일, 하위 호환)
        self.auto_gm_turn_cap = None           # 자동 진행 최대 턴 수 (None=무제한, 안전장치)
        self.auto_gm_turns_done = 0            # 활성화 이후 자동으로 처리한 턴 수
        self.auto_gm_clarify_count = 0         # 같은 플레이어 발언에 대한 명확화 누적 횟수
        self.auto_gm_narrate_count = 0         # 같은 플레이어 발언에 대한 NARRATE 누적 횟수
        self.auto_gm_cost_cap_krw = None       # 자동 모드 누적 비용 상한 (None=무제한, 도달 시 정지)
        self.auto_gm_cost_baseline = 0.0       # 활성화 시점의 session.total_cost (사용량 추적용)
        self.auto_gm_side_note = ""            # !자동 개입으로 주입된 GM 사이드 노트 (다음 호출에 1회 합류 후 비움)
        self.auto_gm_lock = False              # 동시 처리 방지용 락 (직렬화 시 무시)
        self.auto_gm_proceed_history = []      # 최근 PROCEED 이력 (지시사항+컨텍스트+AI요약, 반복 방지용)
        # 되감기 기준 스냅샷 — 런타임 전용(저장 안 함).
        # 마지막 델타 기록 시점의 상태. 백그라운드 갱신이 유실되지 않게 한다.
        self._rewind_snapshot = None
        # 실제 관측된 캐시 읽기 토큰 — 캐시 본문에 구워진 지시문까지 포함된 값.
        # cache_tokens(조립 텍스트 기준)와 다르므로 예측은 이 값을 우선 사용한다.
        self.cache_read_tokens = 0
        # 유지 시간 해석 호출의 누적 비용(원). 2잉크 이상일 때만 청구한다.
        self.interpret_cost_krw = 0.0
        # 선택된 세션 유지 시간(분)
        self.open_minutes = 0
        # 세션 시작 시각(epoch) — 통계의 '세션 온 시간' 산출 기준
        self.started_at = 0.0
        # 스탯별 판정 실패 누적 — 성장 판정 트리거 기준
        self.stat_fail_counts = {}
        # 직전 턴 비용(원). 디스플레이 '직전 턴 금액' 표기용
        self.last_turn_cost = 0.0
        # TTS 목소리 — 세션 생성 시 선택. 미지정이면 기본 나레이터 보이스
        self.tts_voice = ""
        # 기억 압축 방식 — normal(기본) | high | low | ultra
        self.memory_plan = "normal"
        # 서사설계 유형 — quest(기본) | free(풀자유). 퀘스트 시스템 도입 시 사용
        self.narrative_mode = "quest"
        # 세션 종류 — solo(기본) | multi | master.
        # 마스터 채널은 모든 세션에 생성되므로 채널 유무로는 구분할 수 없다.
        self.session_kind = "solo"
        # 비정규 NPC 등록부 — 즉석 등장 인물의 이미지·목소리를 유지한다
        self.irregular_npcs = {}
        # 디스플레이 채널 — 상태 표기와 UI를 단일 메시지로 유지한다
        self.display_ch_id = None
        self.display_msg_id = None
        # 디스플레이 채널에서 1회 텍스트 입력을 기다리는 중인지
        self.awaiting_display_input = False
        # 비공개 세션 여부
        self.is_private = False
        # 미디어 온오프 토글 (이미지·TTS·BGM·효과음)
        self.media_flags = {}
        # 다음 스트리밍 시작 시 재생할 BGM (추출층위가 결정)
        self.pending_bgm = None
        # 직전 BGM 판단의 (상황 태그, 긴장 구간) — 동일하면 전환하지 않는다
        self.last_bgm_situation = None
        # 세션 시작 시점의 통산 일수 — 경과 일수 계산의 기준
        self.start_day_number = None
        # 압축 선결제 누적액(원). 실제 압축 시 또는 세션 종료 시 정산된다.
        self.compression_prepaid_krw = 0.0
        # 직전 산출한 턴 예상치 (디스플레이 표시용)
        self.last_estimate = {}
        # 예측 대조용 직전 입력 추정값 — 런타임 전용(저장 안 함)
        self._last_input_estimate = None
        # 비용 예측 통계 — 층위별 출력 토큰 이동평균 (세션 진행에 따라 수렴)
        self.cost_stats = {}
        # 되감기 — 마지막으로 델타를 기록한 턴 번호(중복 기록 방지)
        self.last_recorded_turn = 0
        # 추출층위 상태 — 미완료(True)면 다음 턴 선언을 차단한다.
        self.extraction_pending = False
        self.extraction_retry_ctx = {}   # 재시도용 묘사 텍스트 등
        self.last_extraction = {}        # 최근 추출 결과 (디스플레이·BGM 판단 입력)
        self.info_ledger = []                  # 정보 인지 원장: 비공개·플롯 정보별 {info, known_by, suspected_by, origin, leaks} 누적 (지시층위가 갱신)
        self.cache_model = None                # 현재 활성 캐시에 사용된 모델 ID (캐시 생성 후 설정)

        # ========== [멀티플레이어 자동진행 상태 (#22)] ==========
        # NOTE: PROCEED 완료 후 GM이 선제적으로 각 PC에게 행동을 순서대로 물어보는 라운드 수집 시스템.
        self.auto_gm_target_chars = []         # 자동진행 대상 PC 이름 전체 목록 (멀티 지원)
        self.auto_gm_pending_players = []      # 현재 라운드에서 아직 행동 선언 안 한 PC 목록
        self.auto_gm_collected_actions = {}    # 이번 라운드에 수집된 행동 {char_name: text}
        self.auto_gm_waiting_for = None        # 현재 발언을 기다리는 PC 이름 (None이면 대기 없음)

        self.npcs = {}
        default_npcs = scenario_data.get("default_npcs", {})
        npc_template = scenario_data.get("npc_template", {})
        _npc_info_fields = npc_template.get("info_fields", []) if isinstance(npc_template, dict) else []

        for npc_name, npc_data in default_npcs.items():
            if isinstance(npc_data, dict):
                # 전체 NPC 항목을 복사 (구조화 필드 + 하위 호환 details 모두 보존)
                npc_entry = {k: v for k, v in npc_data.items() if k != "resources" and k != "statuses"}
                npc_entry["name"] = npc_data.get("name", npc_name)
                self.npcs[npc_name] = npc_entry

                # NPC 기본값 resources/statuses → 런타임 딕셔너리에 사전 적용
                # (태그·!증감이 이 값을 기준으로 증감하도록)
                default_res = npc_data.get("resources", {})
                if default_res:
                    self.resources.setdefault(npc_name, {})
                    self.resources[npc_name].update(default_res)
                default_stat = npc_data.get("statuses", [])
                if default_stat:
                    self.statuses.setdefault(npc_name, [])
                    for s in default_stat:
                        if s not in self.statuses[npc_name]:
                            self.statuses[npc_name].append(s)
            else:
                self.npcs[npc_name] = {
                    "name": npc_name,
                    "details": str(npc_data)
                }
