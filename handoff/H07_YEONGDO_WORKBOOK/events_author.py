# -*- coding: utf-8 -*-
"""영도 사건·정보·초기 인지·NPC 배치/지식 저작 — 레거시 퀘스트 44건의 의미를 사건 상태기계로 옮긴다.

원칙(H07_YEONGDO_AUTHORING_RULES §8):
  · 국면 = 세계/서사 상황 단계. 플레이어 행동 분기(케이스 트리)는 옮기지 않는다.
  · 플레이어의 선택은 정본 사실로만 사건에 닿는다 — 장면 장소(SCENE_PLACE) · 코드 소유 활동(대기·수면·수색·작업·
    독서·치료) · 경과 시간 · PC 인지(노출된 정보, CONTENT). PC 믿음은 조건이 아니다.
  · 비밀은 정보 정의 + 인지 노출(EMIT_SIGNAL.exposes) — 추출층위는 summary만 본다.
  · 보상(소속)은 typed 변이 — 접근 상태 전환 + 사건 지역 상태 grant_faction(호환 뷰가 파생, H07-E).
  · 이탈·무효화 = 취소 조건 / 종결 국면.
"""
import copy
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from ev_lib import (N, NPC_IDS, TRUE, access, acquire, act, after, all_, any_, at, at_any, case, daypart,  # noqa
                    elapsed, knows, life, local, move_npc, not_, not_at_any, npc_at, npc_with_pc, ph, phase,
                    phase_in, place_npc, set_access, sig, tpl)
import infos  # noqa: E402

Q = "yd.info.q."
T = {}          # template_id → template
MAP = {}        # legacy quest id → (처분, [templates], 보존·비고)


def chain(tid, name, spawn, phases, *, spawn_steps=(), resolve=(), cancel=(), extra_cancel=None, slots=None,
          repeat="ONCE_PER_SESSION", max_occ=None, relevance=None, distinct=None):
    """선형/분기 국면 체인. phases: [{id, guide, when(첫 국면 없음), frm([앞 국면]), steps, terminal}]
    각 국면은 자기 자신이거나(STICKY) 앞 국면 + 조건일 때만 성립 — 되돌아가지 않는다."""
    pdefs, cases = {}, []
    ids = [p["id"] for p in phases]
    for i, p in enumerate(phases):
        if i == 0:
            elig = TRUE
        else:
            elig = any_(phase(p["id"]), any_(*[all_(phase(f), p["when"]) for f in p["frm"]]))
        succ = [q for q in phases[1:] if p["id"] in q["frm"]]
        retain = not_(any_(*[all_(phase(p["id"]), q["when"]) for q in succ])) if succ else None
        pdefs[p["id"]] = ph(elig, i + 1, p.get("guide"), retain=retain, terminal=p.get("terminal", False))
        if i == 0:
            cases.append(case("spawn", None, p["id"], list(spawn_steps) + list(p.get("steps", ()))))
        else:
            for f in p["frm"]:
                cases.append(case(f"{f}.to.{p['id']}", f, p["id"], p.get("steps", ())))
    assert len(set(ids)) == len(ids)
    res = phase_in(*resolve) if resolve else None
    can = any_(phase_in(*cancel) if cancel else None, extra_cancel) if (cancel or extra_cancel) else None
    T[tid] = tpl(name, spawn, pdefs, cases, slots=slots, repeat=repeat, max_occ=max_occ, resolve=res, cancel=can,
                 relevance=relevance, distinct=distinct)
    return T[tid]


def P(i, guide, *, frm=None, when=None, steps=(), terminal=False):
    d = {"id": i, "guide": guide, "steps": list(steps), "terminal": terminal}
    if frm is not None:
        d["frm"] = frm if isinstance(frm, list) else [frm]
        d["when"] = when
    return d


# ══════════════════════════════════════════════════════════════
#  장소 묶음
# ══════════════════════════════════════════════════════════════
COAST_LAND = ["청학동 선착장", "영선동 방파제", "동삼동 갯가", "감지해변", "봉래동 물류창고", "물양장", "흰여울 절벽길",
              "방치된 부두", "해안 창고", "갯바위 지대", "중리 해변", "무너진 다리 어귀"]
NAMHANG_INNER = ["수습대 집결지", "조선소 내부", "지하 의료소", "흑색 작업장", "지하 병참 창고", "취사반",
                 "거주 시설 하층부", "옥상 환기팬", "의무실 대기실", "상층부 거주구", "폐수 처리조"]
HAERYEON_INNER = ["해련 자치 마을", "해련사", "해련사 배급소", "집수 설비", "스카이웨이", "방음실", "중앙 주차장 텃밭",
                  "하단 방어선", "아이들 방", "약초밭", "빗물 저장고"]
JODO_INNER = ["조도 방위 사령부", "강의동 병영", "검역 연구소", "격리 구역", "캠퍼스 마당", "군수 창고", "고정 함정",
              "도서관", "실습 공장", "태양광 패널 옥상"]
FACTION_INNER = NAMHANG_INNER + HAERYEON_INNER + JODO_INNER
LOWLAND = ["저지대 상업지구", "주택가 골목", "산복도로 하단", "버스 차고지", "영선고개", "봉래시장", "주유소"]
OUTDOOR_EARLY = ["저지대 상업지구", "산복도로 하단", "무너진 다리 어귀", "봉래동 물류창고", "컨테이너 야적장", "방치된 부두",
                 "흰여울 절벽길", "영선고개", "물양장", "동삼동 해안로", "청학동 선착장", "동삼동 갯가", "감지해변",
                 "영선동 방파제", "해안 창고", "어판장", "버스 차고지", "주택가 골목", "갯바위 지대", "태종대 방면 숲"]
EARLY = life("yd_arrival_day", "ACTIVE")
LANDED = life("yd_landing", "RESOLVED")      # 인트로(시작 장면) 이후 — 시작 분의 신호는 인트로가 묘사하지 않는다
AFTER_DAY1 = life("yd_arrival_day", "RESOLVED")
AFTER_DAY3 = life("yd_settling", "RESOLVED")
AFTER_WEEK = life("yd_long_stay", "RESOLVED")

JOINS = {
    "namhang": ["yd_ev_namhang_gate_inspection", "yd_ev_namhang_lost_team", "yd_ev_namhang_barter_entry",
                "yd_ev_namhang_infiltration"],
    "haeryeon": ["yd_ev_haeryeon_ladder", "yd_ev_haeryeon_gutter", "yd_ev_crying_child", "yd_ev_haeryeon_night_watch"],
    "jodo": ["yd_ev_jodo_screening", "yd_ev_jodo_radio", "yd_ev_causeway_stranded", "yd_ev_jodo_defector"],
    "jungri": ["yd_ev_jungri_grading", "yd_ev_jungri_valuables", "yd_ev_haenyeo_vacancy", "yd_ev_jungri_courier"],
}
FACTION_KO = {"namhang": "남항 중공업 연대", "haeryeon": "해련 자치 마을", "jodo": "조도 방위 사령부",
              "jungri": "중리 교역소"}


def member(f, *, except_tid=None):
    return any_(*[life(t, "RESOLVED") for t in JOINS[f] if t != except_tid])


def member_any(*, except_tid=None):
    return any_(*[member(f, except_tid=except_tid) for f in JOINS])


def grant(f):
    return local("grant", "grant_faction", FACTION_KO[f])


ADMIT = {
    "namhang": [set_access("door_in", "namhang_inner_door", "ADMITTED"),
                set_access("door_out", "namhang_outer_door", "OPEN")],
    "haeryeon": [set_access("ladder", "haeryeon_ladder", "LOWERED"),
                 set_access("rear", "haeryeon_rear_line", "ADMITTED")],
    "jodo": [set_access("gate_in", "jodo_inner_gate", "ADMITTED"),
             set_access("causeway", "jodo_causeway_barricade", "OPEN")],
    "jungri": [],
}


def admit(f):
    return copy.deepcopy(ADMIT[f]) + [grant(f)]


# ══════════════════════════════════════════════════════════════
#  기반 사건 (NEWLY_AUTHORED)
# ══════════════════════════════════════════════════════════════
PLACEMENT = {
    "강철수": "상층부 거주구", "차봉순": "지하 병참 창고", "노상진": "수습대 집결지", "이준혁": "수습대 집결지",
    "류가은": "수습대 집결지", "박창수": "제1방어선", "오기태": "제1방어선", "서태양": "흑색 작업장",
    "임재환": "지하 의료소", "황순애": "거주 시설 하층부", "최민준": "채무자 숙소", "법운": "해련사",
    "조원달": "해련 자치 마을", "신대호": "집수 설비", "오명숙": "중앙 주차장 텃밭", "장우진": "스카이웨이",
    "강병철": "하단 방어선", "박하은": "봉래산 능선", "윤점순": "방음실", "이동철": "중앙 주차장 텃밭",
    "박인철": "중앙 주차장 텃밭", "이현석": "조도 방위 사령부", "민도현": "강의동 병영", "강지수": "검역 연구소",
    "황기영": "군수 창고", "임철민": "방파제 초소", "손재현": "방파제 초소", "윤상준": "다중 검문소",
    "배형식": "혁신지구 캠퍼스", "최수연": "혁신지구 캠퍼스", "김태호": "고정 함정", "문상철": "격리 구역",
    "나용순": "장부방", "엄주섭": "장부방", "장철웅": "경비조 대기소", "조한": "중리 해변", "진복례": "해녀 작업장",
    "황민석": "중리 해변", "이선희": "중리 해변", "장현수": "채무자 숙소", "박미래": "채무자 숙소",
    "권상태": "채무자 숙소", "신창호": "태종대 방면 숲", "백인하": "은신처", "한도윤": "바자르",
    "전성남": "하수도 분기점", "염기준": "자취방",
}
FACTION_OF = {}
for _n in ("강철수", "차봉순", "노상진", "이준혁", "류가은", "박창수", "오기태", "서태양", "임재환", "황순애"):
    FACTION_OF[_n] = "namhang"
for _n in ("법운", "조원달", "신대호", "오명숙", "장우진", "강병철", "박하은", "윤점순", "이동철", "박인철"):
    FACTION_OF[_n] = "haeryeon"
for _n in ("이현석", "민도현", "강지수", "황기영", "임철민", "손재현", "윤상준", "배형식", "최수연", "김태호", "문상철"):
    FACTION_OF[_n] = "jodo"
for _n in ("나용순", "엄주섭", "장철웅", "조한", "진복례", "황민석", "이선희", "장현수", "박미래", "권상태", "한도윤",
           "최민준"):
    FACTION_OF[_n] = "jungri"
MOVERS = ("노상진", "이준혁", "류가은")
SEED_OF = {"namhang": "yd_namhang_member", "haeryeon": "yd_haeryeon_member", "jodo": "yd_jodo_member",
           "jungri": "yd_jungri_member"}

KNOWS_TRUTH = {   # 초기 인지(정보 원문을 그 자리에 있던 사람만) — NEWLY_AUTHORED, 원문 비밀·역할 근거
    "노상진": [Q + "join_namhang_rescue", "yd.info.namhang.team2_missing"],
    "이준혁": ["yd.info.namhang.team2_missing"], "류가은": ["yd.info.namhang.team2_missing"],
    "박창수": [Q + "join_namhang_labor"], "오기태": [Q + "join_namhang_labor"],
    "차봉순": [Q + "join_namhang_trade", "yd.info.namhang.ration_gap"],
    "강철수": [Q + "join_namhang_smuggle", Q + "main_namhang_order", "yd.info.namhang.ration_gap"],
    "임재환": [Q + "main_namhang_order"],
    "조원달": [Q + "join_haeryeon_silence"], "강병철": [Q + "join_haeryeon_silence", Q + "join_haeryeon_watch"],
    "박하은": [Q + "join_haeryeon_watch"], "신대호": [Q + "join_haeryeon_water"],
    "윤점순": [Q + "join_haeryeon_child", Q + "main_haeryeon_water"], "법운": [Q + "main_haeryeon_water"],
    "윤상준": [Q + "join_jodo_screening"], "이현석": [Q + "join_jodo_signal", Q + "main_jodo_command",
                                                   "yd.info.jodo.closure_order"],
    "황기영": [Q + "join_jodo_causeway", "yd.info.jodo.ammo_empty"], "임철민": [Q + "join_jodo_causeway"],
    "문상철": [Q + "join_jodo_defector"], "김태호": ["yd.info.jodo.radio_down"],
    "엄주섭": [Q + "join_jungri_debt", Q + "join_jungri_goods", Q + "main_jungri_ledger"],
    "나용순": [Q + "join_jungri_courier", Q + "main_jungri_ledger", "yd.info.jungri.courier_job"],
    "진복례": [Q + "join_jungri_dive", Q + "place_coast_tide", Q + "main_alone_escape"],
    "전성남": [Q + "place_under_dark", Q + "main_alone_escape"],
}


GENESIS_CHUNK = 12        # 한 사건 케이스의 step 사슬 길이 상한(인과 계층 투영 비용 — 저작 분할로 관리)


def genesis(inline_secret_id):
    """세계 시작 배치·초기 인지. 사슬이 길면 투영 비용이 계층 수에 비례하므로 작은 사건 여럿으로 나눈다."""
    steps = [place_npc(f"pl.{N(n).split('.')[-1]}", n, p) for n, p in sorted(PLACEMENT.items())]
    for n in sorted(NPC_IDS):
        steps.append(acquire(f"own.{N(n).split('.')[-1]}", n, inline_secret_id(N(n))))
        for k, iid in enumerate(KNOWS_TRUTH.get(n, ())):
            steps.append(acquire(f"kn.{N(n).split('.')[-1]}.{k}", n, iid))
    for i in range(0, len(steps), GENESIS_CHUNK):
        tid = f"yd_genesis_{i // GENESIS_CHUNK + 1:02d}"
        T[tid] = tpl("세계 시작 배치·인지", TRUE, {"settled": ph(TRUE, 1, None)},
                     [case("spawn", None, "settled", steps[i:i + GENESIS_CHUNK])], resolve=TRUE)


chain("yd_arrival_day", "도착 첫날", TRUE,
      [P("first_day", None)], resolve=(), extra_cancel=None)
T["yd_arrival_day"]["resolve_when"] = elapsed(1440)
chain("yd_landing", "상륙 직후", TRUE, [P("landing", None)])
T["yd_landing"]["resolve_when"] = elapsed(10)
chain("yd_settling", "섬에 적응하는 사흘", AFTER_DAY1, [P("settling", None)])
T["yd_settling"]["resolve_when"] = elapsed(2880)
chain("yd_long_stay", "섬에서 한 주", AFTER_DAY3, [P("staying", None)])
T["yd_long_stay"]["resolve_when"] = elapsed(4320)

# 물때 — 중리 갯바위길 접근(원문 6.4.3.3 '만조 시에만 노출되는 갯바위길' — 원문 그대로 보존, 검토 항목)
T["yd_tide"] = tpl("물때", TRUE, {
    "ebb": ph(any_(phase("ebb"), all_(phase("flood"), elapsed(372))), 2, None,
              retain=not_(all_(phase("ebb"), elapsed(372)))),
    "flood": ph(any_(phase("flood"), all_(phase("ebb"), elapsed(372))), 3, None,
                retain=not_(all_(phase("flood"), elapsed(372)))),
    "start": ph(TRUE, 1, None, retain=not_(TRUE)),
}, [case("spawn", None, "start", []),
    case("start.to.ebb", "start", "ebb", [set_access("tide", "jungri_tide_path", "LOW_TIDE")]),
    case("ebb.to.flood", "ebb", "flood", [set_access("tide", "jungri_tide_path", "HIGH_TIDE")]),
    case("flood.to.ebb", "flood", "ebb", [set_access("tide", "jungri_tide_path", "LOW_TIDE")])])
T["yd_tide"]["phases"]["ebb"]["eligible_when"] = any_(phase("ebb"), phase("start"),
                                                       all_(phase("flood"), elapsed(372)))

# 남항 수습대 일과 — 세계관 6.1.4.2(12시간 2교대)·수습대 외부 탐색(6.1.2.1) — 오프스크린 NPC 이동
_DAY = daypart("이른 아침", "오전", "한낮", "오후")
_NIGHT = daypart("해질 무렵", "밤", "새벽")
_PATROL = ["이준혁", "류가은"]
T["yd_namhang_salvage_patrol"] = tpl("남항 수습대 낮 탐색", TRUE, {
    "home": ph(any_(phase("home"), all_(phase("out"), _NIGHT), all_(phase("start"), _NIGHT)), 2,
               "수습대 2·3조(이준혁·류가은)가 요새 안에서 쉬거나 교대를 기다린다.",
               retain=not_(all_(phase("home"), _DAY))),
    "out": ph(any_(phase("out"), all_(phase("home"), _DAY), all_(phase("start"), _DAY)), 3,
              "수습대 2·3조가 저지대 상업지구로 물자·연료 탐색을 나가 있다(해 지기 전 귀환).",
              retain=not_(all_(phase("out"), _NIGHT))),
    "start": ph(TRUE, 1, None, retain=not_(TRUE)),
}, [case("spawn", None, "start", []),
    case("start.to.home", "start", "home", []),
    case("start.to.out", "start", "out", [move_npc(f"go.{i}", n, "저지대 상업지구", ["member:namhang"])
                                         for i, n in enumerate(_PATROL)]),
    case("home.to.out", "home", "out", [move_npc(f"go.{i}", n, "저지대 상업지구", ["member:namhang"])
                                       for i, n in enumerate(_PATROL)]),
    case("out.to.home", "out", "home", [move_npc(f"back.{i}", n, "수습대 집결지", ["member:namhang"])
                                       for i, n in enumerate(_PATROL)])])


# ══════════════════════════════════════════════════════════════
#  초반 조우 (early_*)
# ══════════════════════════════════════════════════════════════
chain("yd_ev_thirst", "마실 것", all_(EARLY, at_any(COAST_LAND)), [
    P("thirst", "상륙 직후다. 갈증이 먼저 온다. 바닷물은 마실 수 없고 가진 물은 곧 바닥난다(탈수 위협). "
                "물을 찾는 일 자체가 첫 과제다. 끓이지 않은 물을 마시면 탈이 난다는 것은 PC도 안다."),
    P("stagnant", "고인 빗물을 찾았다. 그러나 그 둘레가 감염자가 엎드려 모이는 자리라는 흔적이 있다.",
      frm="thirst", when=all_(act("SEARCHING"), elapsed(20)),
      steps=[sig("pool", "비탈 아래 빗물이 고인 웅덩이 — 둘레의 흙이 짓이겨져 있고 진득한 체액 자국과 맨발 자국이 겹쳐 있다",
                 exposes=[Q + "early_first_water"])], terminal=True),
], resolve=("stagnant",), extra_cancel=not_(EARLY))
MAP["early_first_water"] = ("MIGRATED", ["yd_ev_thirst"], "갈증 압박·고인 물=감염자 집결 규칙 보존. 끓이기/위험 음용 분기는 서사·추출(상태이상)로.")

chain("yd_ev_first_night", "첫 밤", all_(EARLY, daypart("해질 무렵", "밤"), not_at_any(FACTION_INNER + JODO_INNER),
                                        act("WAITING", "SLEEPING")), [
    P("dark", "해가 넘어갔다. 일몰 후 섬 전체가 암흑이다. 밤에 밖에 있는 것은 위험하다. 실내는 비어 있다고 장담할 수 없다.",
      steps=[]),
    P("noises", "쉬려던 곳 근처에서 갇힌 감염자의 기척이 있다. 닫힌 문을 여는 일은 신중해야 한다.",
      frm="dark", when=elapsed(30),
      steps=[sig("scratch", "닫힌 문 안쪽에서 손톱으로 긁는 소리와 쇳소리 섞인 얕은 숨소리가 이어진다",
                 exposes=[Q + "early_shelter"])]),
    P("dawn", "밤을 넘겼다.", frm="noises", when=daypart("새벽", "이른 아침"), terminal=True),
], resolve=("dawn",), repeat="REPEATABLE", max_occ=2)
MAP["early_shelter"] = ("MIGRATED", ["yd_ev_first_night"], "첫 밤 은신 압박·실내 고립 개체 규칙 보존. 반복 2회(원문 max 2) 유지.")

chain("yd_ev_first_sighting", "처음 보는 것", all_(EARLY, LANDED, at("$slot:where")), [
    P("sighting", "멀지 않은 곳에 감염자 한 개체가 있다. 본토에서 본 것과 같은 엇박자 걸음. 아직 PC를 보지 못했다.",
      steps=[sig("figure", "멀지 않은 곳에 사람 형체 하나 — 관절이 굳은 듯 엇박자로 걷는다. 본토에서 본 그 걸음이다",
                 where=["$slot:where"])]),
    P("studied", "거리를 두고 지켜본 결과, 이 개체는 소리에는 반응하지만 빛에는 반응하지 않는다(한쪽 눈이 멀었다).",
      frm="sighting", when=all_(at("$slot:where"), act("WAITING", "SEARCHING"), elapsed(10)),
      steps=[sig("blind_eye", "그것은 깨진 병 구르는 소리에 고개를 홱 돌리지만 바로 앞을 스치는 빛에는 꿈쩍하지 않는다 — 한쪽 눈이 하얗게 멀어 있다",
                 where=["$slot:where"], exposes=[Q + "early_first_sight"])], terminal=True),
    P("passed", "그것은 다른 곳으로 흘러갔다.", frm="sighting", when=elapsed(45), terminal=True),
], slots={"where": {"type": "PLACE", "candidates": OUTDOOR_EARLY}}, resolve=("studied", "passed"),
   extra_cancel=all_(phase("sighting"), not_(at("$slot:where"))), relevance=["$slot:where"])
MAP["early_first_sight"] = ("MIGRATED", ["yd_ev_first_sighting"], "첫 조우·개체별 감각 손상 규칙 보존. 접근·도주 분기는 서사.")

chain("yd_ev_flotsam", "떠밀려 온 것들", at("$slot:shore"), [
    P("drift", "해안에 본섬에서 떠내려온 표류물이 있다. 쓸 만한 것이 섞여 있을 수 있다(획득물은 묘사·추출 소지품)."),
    P("sea_life", "표류물 속에 바다 생물 잔해가 섞여 있다 — 손대는 것조차 위험하다.",
      frm="drift", when=all_(at("$slot:shore"), act("SEARCHING"), elapsed(15)),
      steps=[sig("shell", "표류물 상자 틈에 썩은 조개껍데기와 해초가 엉겨 붙은 생선 토막이 끼어 있다",
                 where=["$slot:shore"], exposes=[Q + "early_salvage_shore"])], terminal=True),
], slots={"shore": {"type": "PLACE", "candidates": COAST_LAND}}, resolve=("sea_life",),
   extra_cancel=not_(at("$slot:shore")), repeat="REPEATABLE", max_occ=2, relevance=["$slot:shore"])
MAP["early_salvage_shore"] = ("MIGRATED", ["yd_ev_flotsam"], "표류물·해산물 접촉 위험 보존. 원문 총 4회 → 장소별 최대 2회.")

chain("yd_ev_smoke_column", "연기를 따라", all_(EARLY, LANDED, daypart("오전", "한낮", "오후"), at_any(OUTDOOR_EARLY)), [
    P("smoke", "남항 조선소 쪽 하늘로 연기가 일정하게 오른다. 불을 피운다는 것은 사람이, 그것도 통제된 무리가 있다는 뜻이다.",
      steps=[sig("column", "서쪽 해안 조선소 크레인 너머로 가는 연기 한 줄기가 끊기지 않고 오른다 — 흩어지지 않게 다스린 불이다",
                 exposes=[Q + "early_smoke"])]),
    P("contact", "거점의 경계선에 닿았다. 무장한 사람과 짐을 나르는 이들이 보인다.",
      frm="smoke", when=at_any(["남항 중공업 연대", "하단 방어선 아래", "중리 교역소", "방파제 입구", "제1방어선"]),
      terminal=True),
], resolve=("contact",), extra_cancel=all_(phase("smoke"), not_(EARLY)))
MAP["early_smoke"] = ("MIGRATED", ["yd_ev_smoke_column"], "연기=조직의 통제된 불, 거점 첫 접촉 보존. 도주 시 적대 기록(deviated)은 미표현 — 검토 항목.")

chain("yd_ev_crossroads", "오를 것인가", all_(life("yd_settling", "NONE"), at_any(["산복도로", "산복도로 상단", "산복도로 하단", "흰여울 절벽길"])), [
    P("fork", "길이 갈린다. 위로 오르면 시야가 트이고 빗물이 있다, 아래로 내려가면 물자가 있지만 감염자 밀도가 높다."),
    P("vantage", "높은 곳에서 섬 구조가 보인다 — 산 위 빌라 옥상의 빗물 천막, 아래 저지대의 반쯤 열린 셔터.",
      frm="fork", when=at_any(["봉래산 정상", "봉래산 능선", "산복도로 상단", "약수터", "배수지", "산불감시초소"]),
      steps=[sig("view", "위에서 내려다보니 산비탈 빌라 옥상마다 빗물 받는 천막이 줄지어 있고, 아래 저지대 상가는 셔터가 반쯤 열린 채 비어 있다",
                 exposes=[Q + "early_road_choice"])], terminal=True),
    P("lowland", "저지대로 내려왔다. 건물이 빽빽하고 소리가 갇혀 울린다. 물자는 있으나 위험하다.",
      frm="fork", when=at_any(["저지대 상업지구", "주택가 골목", "봉래시장"]), terminal=True),
], resolve=("vantage", "lowland"))
MAP["early_road_choice"] = ("MIGRATED", ["yd_ev_crossroads"], "고지대-물/저지대-물자 구조적 딜레마 보존(고지대 도달 시 노출).")


# ══════════════════════════════════════════════════════════════
#  가입 — 남항 중공업 연대
# ══════════════════════════════════════════════════════════════
NH = "namhang"
chain("yd_ev_namhang_gate_inspection", "손을 보이시오",
      all_(at("제1방어선"), not_(member(NH))), [
    P("inspection", "제1방어선 이중문 사이 검역 통제소. 락스 냄새. 검역관(박창수·오기태)은 동정이 아니라 일할 손인지를 본다. "
                    "물린 자국·상처를 숨기면 의심받는다. 외부인에 대한 온정주의는 없다.",
      steps=[sig("hands", "검역관이 손바닥을 내밀라고 턱짓한다. 굳은살과 상처를 하나씩 들여다보고, 소매를 걷으라 한다",
                 where=["제1방어선"])]),
    P("quarantine", "48시간 검역이 시작됐다. 이중문 사이에서 기다려야 하며 물린 자국이 없어야 나간다.",
      frm="inspection", when=all_(at("제1방어선"), act("WAITING", "SLEEPING"), elapsed(10)),
      steps=[sig("doors", "안쪽 문이 닫힌 채로 바깥 문도 닫힌다. 48시간 동안 이 사이에서 지내야 한다는 짧은 통보",
                 where=["제1방어선"])]),
    P("assigned", "검역을 통과해 하층부 침상과 작업 배치를 받았다(하층부 배정 관행 노출).",
      frm="quarantine", when=all_(at("제1방어선"), elapsed(2880)),
      steps=admit(NH) + [sig("bunk", "안쪽 문이 열린다. 검역관이 하층부 침상 번호와 작업 배치가 적힌 쪽지를 건넨다 — 상층부 칸은 처음부터 비어 있다",
                              where=["제1방어선"], exposes=[Q + "join_namhang_labor"])], terminal=True),
], resolve=("assigned",), extra_cancel=all_(phase_in("inspection", "quarantine"), not_(at("제1방어선"))),
   relevance=["남항 중공업 연대"])
T["yd_ev_namhang_gate_inspection"]["cancel_when"] = any_(T["yd_ev_namhang_gate_inspection"]["cancel_when"],
                                                         all_(not_(phase("assigned")), member(NH, except_tid="yd_ev_namhang_gate_inspection")))
MAP["join_namhang_labor"] = ("MIGRATED", ["yd_ev_namhang_gate_inspection"], "노동력 심사·48시간 검역·하층부 배정 보존. 사정/허위 분기는 서사, 상처 은폐 발각은 검토 항목.")

chain("yd_ev_namhang_lost_team", "돌아오지 못한 조",
      all_(AFTER_DAY1, at_any(["남항 중공업 연대", "제1방어선"]), not_(member(NH))), [
    P("dispute", "연대 수습대 2조가 저지대에서 돌아오지 않았다. 문 앞에서 대원들이 구조냐 포기냐로 다툰다.",
      steps=[sig("argue", "문 앞에서 수습대원들이 낮은 소리로 다툰다 — 저지대로 나간 2조가 아직 돌아오지 않았다",
                 where=["남항 중공업 연대", "제1방어선"], exposes=["yd.info.namhang.team2_missing"])]),
    P("search", "수습대장 노상진이 대원들을 이끌고 저지대 수색에 나섰다. 수습대장의 지시는 형식에 가깝다.",
      frm="dispute", when=elapsed(30),
      steps=[move_npc("lead", "노상진", "저지대 상업지구", ["member:namhang"]),
             sig("depart", "노상진이 지도를 접으며 '형식만 갖추면 된다'고 짧게 말하고 대원 둘을 데리고 산복도로 하단 쪽으로 나선다",
                 where=["남항 중공업 연대", "제1방어선"], exposes=[Q + "join_namhang_rescue"])]),
    P("found", "주택가 지하 주차장 기둥 뒤에서 2조 생존자를 찾았다. 살아 있는 사람과 그렇지 못한 사람이 섞여 있다.",
      frm="search", when=at("지하 주차장"),
      steps=[sig("survivor", "기둥 뒤에 웅크린 수습대원 하나가 숨을 몰아쉰다. 그 옆의 둘은 움직이지 않는다",
                 where=["지하 주차장"], exposes=["yd.info.namhang.team2_location"])]),
    P("returned", "생존자를 데리고 돌아왔다. 문이 열리고, 검역을 기다리는 동안 아무도 눈을 흘기지 않는다.",
      frm="found", when=all_(at_any(["제1방어선", "남항 중공업 연대"]), elapsed(10)),
      steps=admit(NH) + [sig("gate", "제1방어선 문이 안쪽에서 열린다. 대원 하나가 말없이 고개를 끄덕인다",
                              where=["제1방어선", "남항 중공업 연대"])], terminal=True),
    P("lost", "2조는 돌아오지 못했고 기회도 함께 사라졌다.", frm="search", when=elapsed(720), terminal=True),
], resolve=("returned",), cancel=("lost",),
   extra_cancel=all_(phase("dispute"), not_at_any(["남항 중공업 연대", "제1방어선"])), relevance=["남항 중공업 연대", "저지대 상업지구"])
MAP["join_namhang_rescue"] = ("MIGRATED", ["yd_ev_namhang_lost_team"], "실종 조·수습대장 출동(오프스크린 NPC 이동)·포기된 구조의 이면 보존. 허위 제보·흩어짐 분기는 서사. 생존자 동반 여부는 위치·시간으로만 판정 — 검토 항목.")

# 거래 입장 — 물품 소지가 안쪽 문 접근 변형이 된다(PC capability = 소지품 이름)
NAMHANG_TRADE_ITEMS = ["발전기 부품", "정수 필터", "항생제", "공구", "연료"]
chain("yd_ev_namhang_barter_entry", "값을 치르고 들어가다",
      all_(at("수습대 집결지"), not_(member(NH)), not_(life("yd_ev_namhang_infiltration", "ACTIVE"))), [
    P("ledgered", "물건을 넘기고 안쪽 문을 통과했다. 병참국장 차봉순이 명부에 이름을 올린다 — '외부 조달' 표기.",
      steps=admit(NH) + [sig("ledger", "차봉순이 받은 물건을 저울에 달고, 명부 끝 칸에 이름을 적은 뒤 옆에 작게 '외부 조달'이라 써 넣는다",
                              where=["수습대 집결지"], exposes=[Q + "join_namhang_trade"])], terminal=True),
], resolve=("ledgered",),
   extra_cancel=member(NH, except_tid="yd_ev_namhang_barter_entry"), relevance=["남항 중공업 연대"])
MAP["join_namhang_trade"] = ("MIGRATED", ["yd_ev_namhang_barter_entry"], "물품=입장료, 명부 '외부 조달' 표기 보존. 흥정·허풍 분기는 서사. 물품 판정은 안쪽 문 접근 변형(소지품 이름 = capability).")

chain("yd_ev_namhang_infiltration", "문을 거치지 않고",
      all_(at_any(["조선소 내부", "폐수 처리조", "옥상 환기팬"]), not_(member(NH)),
           not_(life("yd_ev_namhang_barter_entry", "ACTIVE"))), [
    P("inside", "정문을 거치지 않고 요새 안에 들어왔다. 좁은 요새에서 낯선 얼굴은 오래 숨지 못한다. 발각되면 침입자다."),
    P("judged", "중앙 위원회 앞에 섰다. 침입 경로를 아는 자는 위험하지만 쓸모도 있다 — 조건부 수용. 위원회는 그 경로를 이미 알고 있었다.",
      frm="inside", when=all_(at_any(NAMHANG_INNER), elapsed(60)),
      steps=admit(NH) + [sig("committee", "손전등 불빛에 둘러싸여 위원회 앞에 선다. 강철수가 침입 경로를 묻고는, 대답을 듣기도 전에 이미 아는 표정을 짓는다",
                              where=NAMHANG_INNER, exposes=[Q + "join_namhang_smuggle"])], terminal=True),
    P("fled", "들어간 길로 빠져나왔다. 이곳에 들어갈 길은 닫혔다.", frm="inside", when=not_at_any(NAMHANG_INNER), terminal=True),
], resolve=("judged",), cancel=("fled",), relevance=["남항 중공업 연대"])
T["yd_ev_namhang_vent_cut"] = tpl("배기팬 철망 절단", all_(at("옥상 환기팬"), act("WORKING")), {
    "cutting": ph(TRUE, 1, "옥상 배기팬 이중 철망을 자르고 있다. 소리를 내면 들킨다.",
                  retain=not_(all_(phase("cutting"), elapsed(20)))),
    "cut": ph(any_(phase("cut"), all_(phase("cutting"), act("WORKING"), elapsed(20))), 2, None, terminal=True)},
    [case("spawn", None, "cutting", []),
     case("cutting.to.cut", "cutting", "cut", [set_access("mesh", "namhang_vent_mesh", "CUT"),
                                               sig("mesh_cut", "철망 두 겹이 잘려 나간다 — 덕트 아래로 발전기 진동이 올라온다", where=["옥상 환기팬"])])],
    resolve=phase("cut"), cancel=all_(phase("cutting"), not_(at("옥상 환기팬"))), repeat="REPEATABLE", max_occ=3)
T["yd_ev_namhang_sewer_grate"] = tpl("폐수 관로 철망", all_(at("하수도"), act("WORKING")), {
    "prying": ph(TRUE, 1, "요새 쪽 관로 철망을 뜯고 있다. 위쪽에서 발전기 진동이 전해진다.",
                 retain=not_(all_(phase("prying"), elapsed(30)))),
    "open": ph(any_(phase("open"), all_(phase("prying"), act("WORKING"), elapsed(30))), 2, None, terminal=True)},
    [case("spawn", None, "prying", []),
     case("prying.to.open", "prying", "open", [set_access("grate", "namhang_sewer_grate", "CUT"),
                                               sig("grate_open", "녹슨 철망이 관로에서 떨어져 나간다 — 폐수 처리조 쪽으로 기어 들어갈 틈이 생겼다", where=["하수도"])])],
    resolve=phase("open"), cancel=all_(phase("prying"), not_(at("하수도"))), repeat="REPEATABLE", max_occ=3)
MAP["join_namhang_smuggle"] = ("MIGRATED", ["yd_ev_namhang_infiltration", "yd_ev_namhang_vent_cut", "yd_ev_namhang_sewer_grate"],
                               "하수도·배기팬 침입 경로, 위원회 판정·조건부 수용, 상부가 경로를 알고 방치한 이면 보존. 절도 후 도주=fled 취소. 민첩 10 조건은 미표현 — 검토 항목.")


# ══════════════════════════════════════════════════════════════
#  가입 — 해련 자치 마을
# ══════════════════════════════════════════════════════════════
HR = "haeryeon"
chain("yd_ev_haeryeon_ladder", "소리를 내지 않는 법",
      all_(at("하단 방어선 아래"), not_(member(HR))), [
    P("watched", "하단 방어선 아래. 빌라 1층이 잔해로 메워져 있고 사다리는 위에서만 내린다. 지나치게 조용하다. "
                 "마을 사람들은 먼저 말을 걸지 않는다 — 보고 판단한다. 소리를 내면 위가 아니라 골목 저쪽이 반응한다.",
      steps=[sig("silence", "인기척이 없는데 위에서 보고 있다는 감각이 든다. 옥상 가장자리에서 거울 반사광이 한 번 깜박인다",
                 where=["하단 방어선 아래"])]),
    P("tested", "사다리가 반쯤 내려왔다. 시험이다 — 소리 없이 오를 수 있는가.",
      frm="watched", when=all_(at("하단 방어선 아래"), act("WAITING"), elapsed(20)),
      steps=[set_access("ladder", "haeryeon_ladder", "LOWERED"),
             sig("ladder_half", "도르래가 소리 없이 돌고 접이식 사다리가 반쯤 내려온다. 끝까지는 내려오지 않는다",
                 where=["하단 방어선 아래"])]),
    P("admitted", "위에 올라섰다. 아무도 환영하지 않지만 밀어내지도 않는다. 자치 위원회 대표가 PC가 사흘 전 어디를 지났는지 말한다.",
      frm="tested", when=at("하단 방어선"),
      steps=admit(HR) + [sig("known", "조원달이 내려다보며 당신이 며칠 전 어느 골목을 지났는지 담담하게 짚는다 — 처음부터 보고 있었다",
                              where=["하단 방어선"], exposes=[Q + "join_haeryeon_silence"])], terminal=True),
    P("withdrawn", "사다리가 다시 올라갔다.", frm="tested", when=all_(not_at_any(["하단 방어선 아래", "하단 방어선"]), elapsed(1)),
      steps=[set_access("ladder", "haeryeon_ladder", "RAISED")], terminal=True),
], resolve=("admitted",), cancel=("withdrawn",),
   extra_cancel=all_(phase("watched"), not_(at("하단 방어선 아래"))), relevance=["하단 방어선 아래", "해련 자치 마을"])
MAP["join_haeryeon_silence"] = ("MIGRATED", ["yd_ev_haeryeon_ladder"], "무소음 관찰·사다리 시험·사전 관찰 이면 보존. 소리침→거부 분기는 서사(대기 활동만 시험 진행).")

chain("yd_ev_haeryeon_gutter", "물을 지고 오르다",
      all_(AFTER_DAY1, at_any(["하단 방어선 아래", "벌목 지대"]), not_(member(HR))), [
    P("broken", "마을 집수 설비의 홈통이 무너져 물이 절벽면을 타고 샌다. 자재가 모자라다. 이곳에서 물은 목숨이며 거래하지 않는다.",
      steps=[sig("leak", "위쪽에서 철판 홈통이 꺾이는 소리 — 빗물이 절벽면을 타고 쏟아진다. 옥상의 누군가가 손짓으로 '자재'를 묻는다",
                 where=["하단 방어선 아래", "벌목 지대"])]),
    P("carrying", "저지대에서 홈통 자재를 구했다(구한 물건은 묘사·소지품). 지고 오르는 길은 무겁고 소리가 난다.",
      frm="broken", when=all_(at_any(["저지대 상업지구", "철물점", "봉래동 물류창고", "해안 창고", "상가 창고"]), act("SEARCHING", "WORKING"))),
    P("repaired", "자재를 올려 함께 고쳤다. 물이 다시 흐르고 누군가 처음으로 이름을 묻는다. 끊긴 홈통의 절단면은 고르다.",
      frm="carrying", when=all_(at("하단 방어선 아래"), act("WORKING"), elapsed(1)),
      steps=admit(HR) + [sig("cut", "올려 보낸 자재로 홈통을 잇는 동안, 떼어 낸 옛 홈통의 절단면이 톱날처럼 고르다는 것이 보인다",
                              where=["하단 방어선 아래"], exposes=[Q + "join_haeryeon_water"])], terminal=True),
    P("given_up", "손을 보태지 않은 자를 이곳은 기억하지 않는다.", frm="broken", when=elapsed(1440), terminal=True),
], resolve=("repaired",), cancel=("given_up",), relevance=["하단 방어선 아래", "해련 자치 마을"])
MAP["join_haeryeon_water"] = ("MIGRATED", ["yd_ev_haeryeon_gutter"], "물=목숨·거래 불가·자재 조달·절단된 홈통 이면 보존. 자재 소지 여부는 기계 판정 불가 — 검토 항목.")

chain("yd_ev_crying_child", "업고 온 아이",
      all_(AFTER_DAY1, at_any(LOWLAND), not_(member(HR))), [
    P("crying", "저지대에서 아이 하나가 혼자 운다. 소리를 내는 아이는 이 섬에서 오래 살지 못한다. 해련 마을이 아이를 받는다는 소문이 있다.",
      steps=[sig("cry", "골목 안쪽에서 아이 하나가 소리 내어 운다 — 주변 건물 그늘에서 무언가 몸을 일으키는 기척",
                 where=LOWLAND)]),
    P("soothed", "아이를 달래 진정시켰다. 이름도 말하지 못한다. 아이는 PC 곁을 떠나지 않는다.",
      frm="crying", when=all_(at_any(LOWLAND), act("TREATING", "WAITING"), elapsed(5))),
    P("handed", "아이를 데리고 하단 방어선 아래에 닿았다. 사다리가 평소와 다른 속도로 내려온다. 아이는 방음실로, PC도 검사를 받는다.",
      frm="soothed", when=at("하단 방어선 아래"),
      steps=admit(HR) + [sig("handover", "사다리가 서둘러 내려오고, 격리 관리자가 아이를 받아 안는다. 그 옷자락에서 쑥 향이 숨 막힐 만큼 짙게 풍긴다",
                              where=["하단 방어선 아래"], exposes=[Q + "join_haeryeon_child"])], terminal=True),
    P("silenced", "울음소리는 오래 가지 않아 그쳤다.", frm="crying", when=elapsed(30), terminal=True),
], resolve=("handed",), cancel=("silenced",), relevance=LOWLAND + ["하단 방어선 아래"])
MAP["join_haeryeon_child"] = ("MIGRATED", ["yd_ev_crying_child"], "우는 아이·소음 위협·아이를 받는 마을·방음실 쑥 향 이면 보존. 아이는 정의 NPC가 아님(동행은 활동으로 대리) — 검토 항목.")

chain("yd_ev_haeryeon_night_watch", "벌목 지대의 밤",
      all_(AFTER_DAY1, at("벌목 지대"), daypart("해질 무렵", "밤"), not_(member(HR))), [
    P("lone_watcher", "사찰 뒤 벌목 지대에 파수꾼 강병철이 홀로 서 있다. 밤에 이 자리를 지키는 것은 위험하다.",
      steps=[place_npc("watch", "강병철", "벌목 지대"),
             sig("watcher", "벌목한 비탈 위에 파수꾼 하나가 미동 없이 서 있다. 이쪽을 보지 않은 채 손가락으로 아래 함정 선을 가리킨다",
                 where=["벌목 지대"])]),
    P("vigil", "함께 밤을 지킨다. 트인 비탈은 그만큼 이쪽도 드러낸다.",
      frm="lone_watcher", when=all_(at("벌목 지대"), act("WAITING"), elapsed(5)),
      steps=[sig("tripwire", "비탈 아래 인계철선에 매단 헝겊 깃발이 툭 떨어진다 — 무언가 숲에서 올라온다", where=["벌목 지대"])]),
    P("dawn", "밤이 지났다. 파수꾼이 비탈의 오래된 흔적을 가리키며 이 자리에서 사라진 사람 이야기를 한다. 함께 밤을 넘긴 사람은 남이 아니다.",
      frm="vigil", when=all_(at("벌목 지대"), daypart("새벽", "이른 아침")),
      steps=admit(HR) + [place_npc("back", "강병철", "하단 방어선"),
                         sig("lost_one", "강병철이 비탈 끝 바위의 검게 굳은 자국을 가리키며, 지난달 이 자리에서 사라진 동료 이름을 처음으로 입에 올린다",
                             where=["벌목 지대"], exposes=[Q + "join_haeryeon_watch"])], terminal=True),
    P("abandoned", "자리를 떴다. 파수꾼은 끝내 이쪽을 보지 않았다.", frm=["lone_watcher", "vigil"],
      when=not_(at("벌목 지대")), steps=[place_npc("back", "강병철", "하단 방어선")], terminal=True),
], resolve=("dawn",), cancel=("abandoned",), relevance=["벌목 지대", "해련 자치 마을"])
MAP["join_haeryeon_watch"] = ("MIGRATED", ["yd_ev_haeryeon_night_watch"], "야간 단독 파수·함정 선·사라진 사람 이면 보존. 파수꾼 = 강병철(원문 정적 파수꾼 선임) 배치 변경은 NEWLY_AUTHORED.")


# ══════════════════════════════════════════════════════════════
#  가입 — 조도 방위 사령부
# ══════════════════════════════════════════════════════════════
JD = "jodo"
chain("yd_ev_jodo_screening", "신체와 사상",
      all_(at("다중 검문소"), not_(member(JD))), [
    P("strip", "다중 검문소. 컨테이너와 철조망 사이 통로, 총구가 이쪽을 향한다. 크레졸 냄새. 질문은 허용되지 않는다. "
               "옷을 벗고 온몸의 상처를 기록한다 — 오래된 흉터까지.",
      steps=[sig("strip", "총구 아래 컨테이너 통로. 옷을 벗고 서라는 지시 — 기록관이 오래된 흉터 하나하나의 내력을 묻는다",
                 where=["다중 검문소"])]),
    P("vetting", "사상 검증. 본토에서 무엇을 했고 누구와 있었는지, 왜 혼자 남았는지 — 답보다 흔들림을 본다. 질문 방향은 폭력 이력을 캔다.",
      frm="strip", when=all_(at("다중 검문소"), act("WAITING"), elapsed(20)),
      steps=[sig("vetting", "기록관이 같은 질문을 표현만 바꿔 세 번 묻는다 — 누구를 때렸는지, 무엇으로, 몇 번이었는지",
                 where=["다중 검문소"], exposes=[Q + "join_jodo_screening"])]),
    P("assigned", "24시간 격리 검역을 넘겼다. 선별된 시민(혁신지구 하역·거주 구역 정비)으로 분류된다.",
      frm="vetting", when=all_(at("다중 검문소"), elapsed(1440)),
      steps=admit(JD) + [sig("classified", "격리 표찰이 떼어지고, 하역 작업조 번호가 적힌 완장이 팔에 채워진다", where=["다중 검문소"])],
      terminal=True),
], resolve=("assigned",), extra_cancel=all_(phase_in("strip", "vetting"), not_(at("다중 검문소"))),
   relevance=["다중 검문소", "조도 방위 사령부"])
MAP["join_jodo_screening"] = ("MIGRATED", ["yd_ev_jodo_screening"], "검문·신체 기록·사상 검증·24시간 격리(세계관 6.3.6.2)·폭력 이력 선별 이면 보존. 기술 인력 분류 분기는 서사.")

chain("yd_ev_jodo_radio", "주파수를 아는 자",
      all_(AFTER_DAY1, at_any(["다중 검문소", "방파제 입구", "혁신지구 캠퍼스"]), not_(member(JD))), [
    P("rumor", "사령부 무전이 며칠째 잡음뿐이다. 태양광은 멀쩡한데 신호가 서지 않는다. 기술을 아는 자를 찾는다.",
      steps=[sig("rumor", "초병 둘이 무전이 며칠째 잡음뿐이라며 기술 아는 놈을 하나 데려와야 한다고 투덜댄다",
                 where=["다중 검문소", "방파제 입구", "혁신지구 캠퍼스"], exposes=["yd.info.jodo.radio_down"])]),
    P("tested", "검문소 밖에서 실력을 시험받아 통과했다. 호송을 받아 안으로 들어간다(시선은 기록된다).",
      frm="rumor", when=all_(at("다중 검문소"), act("WORKING"), elapsed(40)),
      steps=[set_access("gate_in", "jodo_inner_gate", "ADMITTED"),
             sig("works", "고장 난 무전기가 잡음 너머로 목소리 한 토막을 뱉는다. 초병이 안쪽 문을 연다", where=["다중 검문소"])]),
    P("restored", "옥상 안테나를 손봤다. 신호가 서는 순간 실내가 조용해진다 — 이 사람은 내보내지 않는다. 급전선은 누군가 잘라 놓았었다.",
      frm="tested", when=all_(at("태양광 패널 옥상"), act("WORKING"), elapsed(40)),
      steps=admit(JD) + [sig("antenna", "급전선 끝을 이어 붙이다 보니 절단면이 칼로 자른 듯 고르다. 신호가 서자 뒤에 선 장교의 얼굴이 굳는다",
                              where=["태양광 패널 옥상"], exposes=[Q + "join_jodo_signal"])], terminal=True),
    P("dismissed", "기회를 놓쳤다. 안쪽 문이 다시 닫힌다.", frm="tested", when=elapsed(360),
      steps=[set_access("gate_in", "jodo_inner_gate", "SEALED")], terminal=True),
], resolve=("restored",), cancel=("dismissed",),
   extra_cancel=all_(phase("rumor"), elapsed(1440)), relevance=["다중 검문소", "조도 방위 사령부"])
MAP["join_jodo_signal"] = ("MIGRATED", ["yd_ev_jodo_radio"], "무전 고장 소문·실력 시험·안테나 복구·상부의 의도적 차단 이면 보존. 기술 11 조건은 미표현, 첩자 판정 분기는 서사 — 검토 항목.")

chain("yd_ev_causeway_stranded", "400미터",
      all_(AFTER_DAY1, at_any(["방파제 입구", "동삼동 해안로", "혁신지구 캠퍼스"]), not_(member(JD))), [
    P("pinned", "방파제 입구에서 수색조가 부상자를 업은 채 발이 묶였다. 해안로를 감염자가 메웠다. 초소는 사격을 망설인다(실은 탄약이 없다).",
      steps=[sig("pinned", "방파제 입구 쪽에서 부상자를 업은 수색조가 쩔쩔맨다. 해안로 쪽에서 감염자들이 쇄도해 오는데 초소는 한 발도 쏘지 않는다",
                 where=["방파제 입구", "동삼동 해안로", "혁신지구 캠퍼스"], exposes=["yd.info.jodo.team_pinned"])]),
    P("crossing", "방파제 위다. 양쪽이 트여 있고 은폐물이 없다. 400미터가 길다.",
      frm="pinned", when=at("아치 방파제")),
    P("honored", "검문소 문이 열렸다 — 절차를 밟을 때가 아니라는 판단. 사령관 앞에서 배치가 먼저 정해진다. 초소는 끝내 쏘지 않았다.",
      frm="crossing", when=at("다중 검문소"),
      steps=admit(JD) + [sig("gate", "검문소 문이 안에서 열린다. 숨을 고르는 사이, 초병 하나가 빈 탄창을 주머니에 쑤셔 넣는 것이 보인다",
                              where=["다중 검문소"], exposes=[Q + "join_jodo_causeway"])], terminal=True),
    P("too_late", "수색조는 건너지 못했다.", frm="pinned", when=elapsed(20), terminal=True),
], resolve=("honored",), cancel=("too_late",), relevance=["방파제 입구", "아치 방파제"])
MAP["join_jodo_causeway"] = ("MIGRATED", ["yd_ev_causeway_stranded"], "시간 압박 구조·400m 개활지·사격 없는 초소(탄약 고갈) 이면 보존. 미끼/업기 분기는 서사, 혼자 생존(deviated)은 미표현.")

chain("yd_ev_jodo_defector", "돌아온 병사",
      all_(AFTER_DAY3, at_any(["동삼동 해안로", "혁신지구 캠퍼스", "방파제 입구"]), not_(member(JD))), [
    P("met", "계급장을 뜯은 군복 사내가 조도로 돌아가려다 방파제 앞에서 멈춘다. 이탈자다(정의 NPC 아님).",
      steps=[sig("defector", "계급장을 뜯어낸 군복 차림 사내가 방파제 쪽을 바라보며 몇 걸음 갔다가 되돌아온다",
                 where=["동삼동 해안로", "혁신지구 캠퍼스", "방파제 입구"])]),
    P("story", "사내가 격리 구역에서 벌어진 일을 털어놓았다 — 검증이 끝난 사람도 나오지 못했다.",
      frm="met", when=all_(act("WAITING"), elapsed(15)),
      steps=[sig("confess", "사내가 낮게 말한다 — 격리 구역에서 검증 끝난 사람들도 끝내 나오지 못했고, 자기는 그 명령을 따르지 않았다고",
                 exposes=[Q + "join_jodo_defector"])]),
    P("judged", "검문소에서 판단을 받는다. 이탈자는 끌려가고 PC에 대한 판단이 남는다 — 쓸모를 인정받아 안에 들어가되 감시가 따른다.",
      frm=["met", "story"], when=at("다중 검문소"),
      steps=admit(JD) + [sig("taken", "초병들이 사내를 끌고 간다. 기록관이 당신 이름 옆에 무언가를 적고, 안쪽 문을 가리킨다",
                              where=["다중 검문소"])], terminal=True),
    P("vanished", "사내는 방파제를 등지고 사라졌다.", frm=["met", "story"], when=elapsed(180), terminal=True),
], resolve=("judged",), cancel=("vanished",), relevance=["방파제 입구", "다중 검문소"])
MAP["join_jodo_defector"] = ("MIGRATED", ["yd_ev_jodo_defector"], "이탈자·격리 구역 처분 이면·협조자/감시 대상 판정 보존. 신고/변호 분기는 서사.")


# ══════════════════════════════════════════════════════════════
#  가입 — 중리 교역소 (중립 시장 — 문 대신 장부)
# ══════════════════════════════════════════════════════════════
JR = "jungri"
chain("yd_ev_jungri_grading", "3등급",
      all_(at("장부방"), not_(member(JR)), npc_at("엄주섭", "장부방")), [
    P("appraisal", "장부방. 이곳은 사람을 심사하지 않는다 — 값을 매길 뿐이다. 등급 사정관 엄주섭이 위아래를 훑는다.",
      steps=[sig("look", "엄주섭이 낡은 전자계산기를 두드리며 위아래를 훑는다. 가진 것을 꺼내 보라는 손짓", where=["장부방"])]),
    P("registered", "장부에 이름이 올랐다. 가진 것이 없으면 3등급, 채무 항목이 함께 오른다 — 이자는 갚는 속도보다 빨리 는다.",
      frm="appraisal", when=all_(at("장부방"), act("READING", "WAITING"), elapsed(15)),
      steps=admit(JR) + [sig("terms", "3등급 패가 손에 쥐어진다. 조항 끝의 이자 계산은 매달 원금의 절반을 넘게 붙는다",
                              where=["장부방"], exposes=[Q + "join_jungri_debt"])], terminal=True),
], resolve=("registered",), extra_cancel=all_(phase("appraisal"), not_(at("장부방"))), relevance=["중리 교역소"])
MAP["join_jungri_debt"] = ("MIGRATED", ["yd_ev_jungri_grading"], "값을 매기는 시장·3등급 채무·빠져나올 수 없는 이자 이면 보존. 흥정·허풍 분기는 서사.")

chain("yd_ev_jungri_valuables", "값이 나가는 물건",
      all_(at("바자르"), not_(member(JR))), [
    P("eyed", "바자르 초입. 값나가는 물건(항생제·탄약·발전기 부품 등)을 가졌다면 상인이 먼저 알아본다. 물건이 곧 신분이다. "
              "가진 것이 알려진 무소속은 표적이 된다.",
      steps=[sig("eyed", "상인 하나가 당신 짐의 모양새를 먼저 훑고는 목소리를 낮춰 값을 부른다. 등 뒤로 시선이 몇 개 붙는다",
                 where=["바자르"])]),
    P("graded", "물건 값에 자리를 얹어 장부방에서 2등급으로 등재됐다 — 장부에 '유동' 표기.",
      frm="eyed", when=all_(at("장부방"), act("WORKING", "READING"), elapsed(20)),
      steps=admit(JR) + [sig("fluid", "사정관이 2등급 칸에 이름을 적고 옆에 작게 '유동'이라 쓴다 — 값이 떨어지면 등급도 떨어진다는 뜻이라 덧붙인다",
                              where=["장부방"], exposes=[Q + "join_jungri_goods"])], terminal=True),
], resolve=("graded",), extra_cancel=all_(phase("eyed"), elapsed(720)), relevance=["중리 교역소"])
MAP["join_jungri_goods"] = ("MIGRATED", ["yd_ev_jungri_valuables"], "물건=신분·2등급 '유동' 표기 이면 보존. 물품 소지 여부·표적화(targeted)는 기계 판정 불가 — 검토 항목.")

chain("yd_ev_haenyeo_vacancy", "물에 들어가는 값",
      all_(AFTER_DAY1, at("해녀 작업장"), not_(member(JR)), npc_at("진복례", "해녀 작업장")), [
    P("empty_suit", "밀폐 잠수복 하나가 비어 있다 — 사람이 줄었다. 수석 해녀 진복례가 이쪽을 본다. 바다는 죽음, 잠수는 짧게, 복귀 즉시 전신 소독.",
      steps=[sig("suit", "걸려 있는 밀폐 잠수복 중 하나가 비어 있다. 수석 해녀가 테이프를 감던 손을 멈추고 이쪽을 본다",
                 where=["해녀 작업장"])]),
    P("proven", "물 밖에서 줄을 잡고 소독을 도우며 끝까지 자리를 지켰다. 수석 해녀가 장부방에 이름을 넣어 준다(2등급). 그의 손등은 검게 변해 있다.",
      frm="empty_suit", when=all_(at("해녀 작업장"), act("WORKING"), elapsed(60)),
      steps=admit(JR) + [sig("hand", "소독약을 쏟아붓는 사이 수석 해녀의 장갑이 미끄러져, 검게 변한 손등이 잠깐 드러났다가 다시 덮인다",
                              where=["해녀 작업장"], exposes=[Q + "join_jungri_dive"])], terminal=True),
], resolve=("proven",), extra_cancel=all_(phase("empty_suit"), not_(at("해녀 작업장"))), relevance=["중리 해변"])
MAP["join_jungri_dive"] = ("MIGRATED", ["yd_ev_haenyeo_vacancy"], "빈 잠수복·잠수 규칙·물 밖 보조 노동·수석 해녀 감염 은폐 이면 보존. 직접 잠수·이음매 파열 분기는 서사(PC 해수 접촉은 추출 상태이상).")

chain("yd_ev_jungri_courier", "세 곳을 오가는 자",
      all_(AFTER_DAY1, at("중리 교역소"), not_(member_any())), [
    P("offer", "거상 나용순이 세 거점 어디에도 속하지 않은 사람을 찾는다 — 무소속이 처음으로 자격이 된다.",
      steps=[sig("offer", "바자르 상인 둘이 거상이 무소속 운반인을 찾는다며, 어느 쪽 사람도 아닌 얼굴을 고르는 중이라고 수군댄다",
                 where=["중리 교역소"], exposes=["yd.info.jungri.courier_job"])]),
    P("parcel", "봉인된 꾸러미와 목적지(해련 마을 하단 방어선 아래)를 받았다. 열어 보지 말 것. 낮에는 눈이, 밤에는 다른 것이 많다.",
      frm="offer", when=all_(at("장부방"), act("WAITING"), elapsed(10)),
      steps=[sig("parcel", "나용순이 밀랍으로 봉한 꾸러미를 건네며 해련 마을 사다리 아래에 두고 오라고 한다. 열어 보면 값이 없다고", where=["장부방"])]),
    P("delivered", "꾸러미를 넘겼다. 받는 쪽은 내용을 보자마자 표정이 굳었다 — 물자가 아니다.",
      frm="parcel", when=at("하단 방어선 아래"),
      steps=[sig("rope", "위에서 줄이 내려와 꾸러미를 끌어올린다. 옥상에서 꾸러미를 연 사람이 종이 몇 장을 넘기다 얼굴이 굳는다",
                 where=["하단 방어선 아래"], exposes=[Q + "join_jungri_courier"])]),
    P("paid", "돌아와 보고했다. 장부에 2등급으로 이름이 오른다.",
      frm="delivered", when=at("장부방"), steps=admit(JR) + [sig("paid", "나용순이 장부 2등급 칸에 직접 이름을 적는다", where=["장부방"])],
      terminal=True),
    P("expired", "기한을 넘겼다. 다른 무소속자가 그 일을 맡았다.", frm=["offer", "parcel"], when=elapsed(720), terminal=True),
], resolve=("paid",), cancel=("expired",), relevance=["중리 교역소", "하단 방어선 아래"])
MAP["join_jungri_courier"] = ("MIGRATED", ["yd_ev_jungri_courier"], "무소속 운반인·봉인 꾸러미·세력 정보 양매 이면 보존. 민첩 11·개봉/양다리 분기는 미표현 — 검토 항목.")

# 같은 세력의 다른 가입 경로는 가입이 성립하면 정리된다
for f, tids in JOINS.items():
    for tid in tids:
        t = T[tid]
        other = member(f, except_tid=tid)
        t["cancel_when"] = any_(t.get("cancel_when"), other) if t.get("cancel_when") else other


# ══════════════════════════════════════════════════════════════
#  장소 사건 (place_*) — 장소 + 코드 소유 활동
# ══════════════════════════════════════════════════════════════
def place_event(tid, name, places, activity, minutes, cue_found, info_id, guide_enter, guide_found, *,
                repeat="ONCE_PER_BINDING", max_occ=None, extra_spawn=None, cue_enter=None):
    spawn = all_(at("$slot:place"), extra_spawn) if extra_spawn else at("$slot:place")
    first = P("present", guide_enter, steps=[sig("enter", cue_enter, where=["$slot:place"])] if cue_enter else [])
    chain(tid, name, spawn, [
        first,
        P("found", guide_found, frm="present", when=all_(at("$slot:place"), act(*activity), elapsed(minutes)),
          steps=[sig("found", cue_found, where=["$slot:place"], exposes=[info_id])], terminal=True),
    ], slots={"place": {"type": "PLACE", "candidates": places}}, resolve=("found",),
       extra_cancel=not_(at("$slot:place")), repeat=repeat, max_occ=max_occ, relevance=["$slot:place"])


place_event("yd_ev_shop_marks", "남은 것",
            ["약국", "분식집", "철물점", "구멍가게", "전당포", "피시방", "세탁소", "중고 무기상", "약재 좌판", "의무실 대기실", "지하 의료소"],
            ("SEARCHING",), 15, "문틀 안쪽 눈높이에 분필 표식 — 동그라미 하나에 짧은 줄 두 개, 그 옆에 날짜 같은 숫자",
            Q + "place_shop_scavenge", "먼저 다녀간 흔적이 있지만 구석까지 뒤진 것 같지는 않다. 안쪽은 어둡고 시야가 막힌다(획득물은 묘사·소지품).",
            "먼저 다녀간 무리가 남긴 표식을 발견했다.")
MAP["place_shop_scavenge"] = ("MIGRATED", ["yd_ev_shop_marks"], "약탈된 상점 수색·표식 체계 이면 보존. 원문 총 5회 → 장소별 1회.")
place_event("yd_ev_locked_room", "살던 사람",
            ["빈 가정집", "반지하 셋방", "옥탑방", "자취방", "골목 공터", "세탁소", "주택가 골목", "옥상 텃밭"],
            ("SEARCHING",), 15, "안쪽 방문이 바깥에서 숟가락으로 걸어 잠가져 있다. 문틈으로 단내가 새어 나온다",
            Q + "place_home_traces", "사람이 살던 흔적이 그대로다 — 밥상, 사진, 벗어 둔 옷. 남의 삶을 뒤지는 감각은 익숙해지지 않는다.",
            "이 집 사람들이 나가지 못했다는 흔적을 보았다.")
MAP["place_home_traces"] = ("MIGRATED", ["yd_ev_locked_room"], "빈집 수색·나가지 못한 거주자 이면 보존. 원문 총 4회 → 장소별 1회.")

chain("yd_ev_tide_window", "물때", all_(LANDED, at("$slot:shore"), access("jungri_tide_path", "LOW_TIDE")), [
    P("ebb", "물이 빠지고 있다. 드러난 바위 사이에 무언가 걸려 있고, 물이 다시 차기 전까지가 시간이다. 바다에서 온 것은 만지는 것조차 위험하다.",
      steps=[sig("ebb", "물이 빠진 바위 틈 사이로 무언가 걸려 있다 — 물때가 돌기 전까지만 닿을 수 있다", where=["$slot:shore"])]),
    P("inspected", "걸린 것을 확인했다 — 바다 생물이 기형으로 엉겨 붙은 그물이다.",
      frm="ebb", when=all_(at("$slot:shore"), act("SEARCHING"), elapsed(15)),
      steps=[sig("tangle", "걸린 것은 낡은 그물 — 그 위에 붙은 조개와 따개비가 비정상적으로 부풀어 서로 엉겨 붙어 있다",
                 where=["$slot:shore"], exposes=[Q + "place_coast_tide"])], terminal=True),
    P("flooded", "물이 다시 찼다.", frm="ebb", when=access("jungri_tide_path", "HIGH_TIDE"),
      steps=[sig("flood", "발목을 적시던 물이 순식간에 무릎까지 차오른다", where=["$slot:shore"])], terminal=True),
], slots={"shore": {"type": "PLACE", "candidates": ["청학동 선착장", "영선동 방파제", "동삼동 갯가", "감지해변", "물양장",
                                                    "갯바위 지대", "어판장", "중리 해변", "방치된 부두", "해안 창고"]}},
   resolve=("inspected", "flooded"), extra_cancel=not_(at("$slot:shore")), repeat="REPEATABLE", max_occ=2,
   relevance=["$slot:shore"])
MAP["place_coast_tide"] = ("MIGRATED", ["yd_ev_tide_window", "yd_tide"], "물때(정본 조석 상태)·조간대 변이 생물 이면 보존. 물때는 세계 사건 yd_tide가 소유.")
place_event("yd_ev_vantage", "내려다보다",
            ["봉래산 정상", "봉래산 능선", "등산로", "송신탑", "산불감시초소", "무연고 묘역", "약수터", "배수지", "벌목 지대", "봉래산"],
            ("WAITING", "SEARCHING"), 10, "연기, 움직임, 불빛 — 섬 세 곳에서 사람의 표시가 보이고, 한 곳 옥상에서 거울 반사광이 규칙적으로 깜박이며 이쪽을 향한다",
            Q + "place_high_vantage", "섬이 내려다보인다. 시야가 트였다는 것은 이쪽도 보인다는 뜻이다.",
            "섬의 지형과 사람 있는 자리를 머릿속에 담았다 — 그리고 이쪽도 보였다.", repeat="REPEATABLE", max_occ=2)
MAP["place_high_vantage"] = ("MIGRATED", ["yd_ev_vantage"], "고지대 조망·노출 위험·신호 가시성 이면 보존. 조망으로 지명 습득(공간 지식)은 미구현 — 검토 항목.")
place_event("yd_ev_under_marks", "어둠 속에서",
            ["하수도", "하수도 분기점", "서부 간선", "동부 간선", "남부 지선", "빗물 저류조", "펌프실", "붕괴 구간", "지하 주차장", "막힌 구간"],
            ("SEARCHING", "WAITING"), 15, "관로 벽에 같은 표식이 일정한 간격으로 반복된다. 그 아래에 아직 마르지 않은 촛농과 깡통 하나",
            Q + "place_under_dark", "빛이 들지 않는다. 방향 감각이 먼저 사라지고 소리만 남는다. 빛은 멀리서도 보인다.",
            "지하에 사람이 산다는 흔적을 보았다.")
MAP["place_under_dark"] = ("MIGRATED", ["yd_ev_under_marks"], "지하 암흑·방향 상실·지하 거주자 영역 표식 이면 보존. 원문 총 3회 → 장소별 1회.")
place_event("yd_ev_claimed_store", "들고 갈 수 있는 만큼",
            ["상가 창고", "해안 창고", "봉래동 물류창고", "컨테이너 야적장", "조선소 내부", "폐수 처리조"],
            ("SEARCHING", "WORKING"), 20, "바닥 먼지에 며칠 안 된 손수레 바퀴 자국이 두 줄 나 있고, 기둥에 세력 표식이 페인트로 찍혀 있다",
            Q + "place_store_haul", "물자가 남아 있다. 문제는 양이 아니라 무게다. 두 번 오가는 것은 두 배로 위험하다(운반물은 묘사·소지품).",
            "이 창고가 이미 누군가 점찍어 둔 곳이라는 흔적을 보았다.")
MAP["place_store_haul"] = ("MIGRATED", ["yd_ev_claimed_store"], "무게 딜레마·세력이 점찍은 창고 이면 보존. 원문 총 3회 → 장소별 1회.")
place_event("yd_ev_records", "남겨진 기록",
            ["폐교", "파출소", "소방서", "교회", "교회 지하", "도서관", "종교시설 폐허", "버려진 정류장", "피시방"],
            ("READING",), 20, "대피 명단 이름 옆마다 붉은 표시 — 한쪽 끝에 같은 서식의 '판정' 칸이 있고, 서명란의 소속은 지금 거점 이름과 같다",
            Q + "place_public_record", "서류와 명부가 흩어져 있다. 사태 초기에 무슨 일이 있었는지가 종이에 남아 있다.",
            "명단 표시가 무엇인지 읽어 냈다.")
MAP["place_public_record"] = ("MIGRATED", ["yd_ev_records"], "초기 대피 기록·감염 판정 표시 이면 보존. 소각(deviated) 분기는 서사.")
place_event("yd_ev_barricade", "막힌 길",
            ["산복도로", "산복도로 상단", "산복도로 하단", "영선고개", "동삼 언덕", "흰여울 절벽길", "동삼동 해안로",
             "태종대 방면 숲", "폐채석장", "버스 차고지", "무너진 다리 어귀", "산복도로 동측 내리막"],
            ("SEARCHING",), 10, "쌓인 폐차와 철근 사이에 한쪽으로만 사람 하나 지날 틈이 남아 있다 — 반대편 골목으로 감염자를 흘려보내도록 일부러 쌓았다",
            Q + "place_route_block", "길이 막혀 있다. 차량 잔해인지, 무너진 구조물인지, 누가 일부러 쌓은 것인지는 가까이 가야 안다.",
            "막은 방식이 의도적이라는 것을 알아보았다.")
MAP["place_route_block"] = ("MIGRATED", ["yd_ev_barricade"], "막힌 길·감염자 흐름 관리 이면 보존. 해체로 흐름 변경(접근 상태)은 미표현 — 검토 항목.")
place_event("yd_ev_night_road", "밤에 움직이는 일",
            ["산복도로", "산복도로 상단", "산복도로 하단", "영선고개", "동삼 언덕", "흰여울 절벽길", "동삼동 해안로",
             "태종대 방면 숲", "폐채석장", "버스 차고지", "무너진 다리 어귀", "산복도로 동측 내리막"],
            ("WAITING",), 20, "어둠 속에서 불빛 하나 없이 지나가는 발소리 셋 — 일정한 간격으로 멈췄다가 다시 간다. 그들은 길을 보지 않고도 걷는다",
            Q + "place_night_move", "일몰 후 이 섬은 전기가 없다. 발밑조차 보이지 않는다. 불을 켜면 아주 멀리서도 보인다.",
            "밤을 쓰는 자들이 따로 있다는 것을 알았다.", extra_spawn=daypart("밤"), repeat="REPEATABLE", max_occ=2)
MAP["place_night_move"] = ("MIGRATED", ["yd_ev_night_road"], "야간 암흑·불빛 노출·불 없이 다니는 야간 이동자 이면 보존.")
place_event("yd_ev_failing_utility", "돌아가게 하는 것",
            ["옥상 환기팬", "태양광 패널 옥상", "실습 공장", "펌프실", "빗물 저장고", "집수 설비", "주유소", "송신탑", "배수지", "약초밭"],
            ("SEARCHING", "WORKING"), 15, "삭은 부품을 들어내자 같은 규격 예비 부품의 포장 상자 조각이 보인다 — 다른 거점의 표식이 찍혀 있다",
            Q + "place_utility_check", "설비가 온전치 않다. 당장 멎지는 않지만 멎으면 곧바로 사람이 곤란해지는 종류다.",
            "예비 부품을 다른 거점이 쥐고 있다는 것을 알았다.", repeat="REPEATABLE", max_occ=2)
MAP["place_utility_check"] = ("MIGRATED", ["yd_ev_failing_utility"], "설비 노후·거점 간 부품 의존 이면 보존. 수리 성패의 정본 효과(설비 정지)는 미표현 — 검토 항목.")
MAP["place_home_quarter"] = ("RETIRED", [], "이면정보·세계 결과가 없는 휴식 소품. DYNAMIC에서는 SLEEP 활동(코드 소유 시간·사건 중단)과 추출 상태·자원으로 대체. LEGACY 퀘스트로는 유지.")
MAP["place_market_dig"] = ("RETIRED", [], "이면정보·세계 결과가 없는 획득 소품. DYNAMIC에서는 SEARCH 활동 + 묘사·추출 소지품, 시장 소문은 yd_ev_shop_marks·정보 거래소 서사로 대체. LEGACY 퀘스트로는 유지.")


# ══════════════════════════════════════════════════════════════
#  인물 사건 (npc_*) — 정본 배치 + 장면 공존
# ══════════════════════════════════════════════════════════════
chain("yd_ev_ledger_shortfall", "장부를 쥔 사람",
      all_(AFTER_DAY1, at("$slot:place"), npc_with_pc("$slot:keeper")), [
    P("counting", "장부를 든 관리자가 같은 줄을 세 번 짚는다. 숫자가 맞지 않는다 — 재고가 비는데 원인을 못 찾고 있다. 누가 볼까 신경 쓴다.",
      steps=[sig("count", "장부를 든 관리자가 같은 줄을 손가락으로 세 번 짚고, 누가 보는지 고개를 든다", where=["$slot:place"])]),
    P("stakeout", "어두워진 뒤 자리를 지켰다. 문이 열리고, 빼돌린 물자가 배급에서 밀려난 사람에게 건네진다.",
      frm="counting", when=all_(at("$slot:place"), act("WAITING"), daypart("밤"), elapsed(30)),
      steps=[sig("handoff", "어둠 속에서 창고 문이 소리 없이 열리고, 통조림 몇 개가 배급 줄에서 밀려났던 얼굴들의 손으로 넘어간다",
                 where=["$slot:place"], exposes=[Q + "npc_keeper_ledger"])], terminal=True),
], slots={"keeper": {"type": "ACTOR", "candidates": ["엄주섭", "차봉순", "황기영", "법운", "조원달"]},
          "place": {"type": "PLACE", "candidates": ["장부방", "지하 병참 창고", "군수 창고", "해련사 배급소", "취사반", "해련사"]}},
   resolve=("stakeout",), extra_cancel=all_(phase("counting"), any_(not_(at("$slot:place")), elapsed(1440))),
   repeat="REPEATABLE", max_occ=1, relevance=["$slot:place"])
MAP["npc_keeper_ledger"] = ("MIGRATED", ["yd_ev_ledger_shortfall"], "장부 불일치·야간 잠복·규정 vs 사정 이면 보존. 관리자-장소 쌍은 정본 배치로 성립(필터 pair → ACTOR_PLACE=장면). 원문 총 3회 → 쌍별 1회.")

chain("yd_ev_missing_shift", "교대 시간",
      all_(AFTER_DAY1, at("$slot:place"), npc_with_pc("$slot:worker")), [
    P("waiting", "작업자가 교대를 기다린다. 올 사람이 오지 않는다 — 어제부터 한 명이 줄었다. 이유는 말하지 않는다.",
      steps=[sig("wait", "작업자가 출입구 쪽을 몇 번이고 돌아본다. 교대할 사람이 오지 않는다", where=["$slot:place"])]),
    P("shared_work", "일을 나눠 졌다. 손이 맞으면 말수가 줄고, 그것이 이곳의 대화다. 교대를 마치며 작업자가 낮게 귀띔한다.",
      frm="waiting", when=all_(at("$slot:place"), act("WORKING"), elapsed(60)),
      steps=[sig("whisper", "교대를 마친 작업자가 고개를 끄덕이고, 줄어든 사람은 검역에서 걸려 격리됐는데 아래로는 아무도 알려 주지 않는다고 낮게 말한다",
                 where=["$slot:place"], exposes=[Q + "npc_worker_shift"])], terminal=True),
], slots={"worker": {"type": "ACTOR", "candidates": ["황순애", "서태양", "노상진", "오명숙", "진복례", "장철웅", "임철민"]},
          "place": {"type": "PLACE", "candidates": ["거주 시설 하층부", "흑색 작업장", "수습대 집결지", "중앙 주차장 텃밭",
                                                    "해녀 작업장", "경비조 대기소", "방파제 초소"]}},
   resolve=("shared_work",), extra_cancel=all_(phase("waiting"), not_(at("$slot:place"))),
   repeat="REPEATABLE", max_occ=1, relevance=["$slot:place"])
MAP["npc_worker_shift"] = ("MIGRATED", ["yd_ev_missing_shift"], "빠진 교대·함께 일하기·검역 격리 은폐 이면 보존. 원문 총 4회 → 쌍별 1회.")

chain("yd_ev_concealed_symptom", "감추는 사람",
      all_(AFTER_DAY3, npc_with_pc("$slot:person")), [
    P("gesture", "이 인물의 몸짓에 어색한 데가 있다. 무언가를 가리는 동작이 반복된다. 이 섬에서 몸을 가린다는 것은 대개 한 가지 뜻이다.",
      steps=[sig("hide", "그가 같은 동작으로 손목인지 손등인지를 소매로 덮는다. 소독약 냄새가 유난히 짙다",
                 observable=npc_with_pc("$slot:person"))]),
    P("observed", "곁에서 오래 지켜보거나 상태를 살폈다. 증상이 감염과 다를 수도 있다 — 그러나 이 섬에서는 구분되지 않는다.",
      frm="gesture", when=all_(npc_with_pc("$slot:person"), act("WAITING", "TREATING"), elapsed(20)),
      steps=[sig("symptom", "가까이서 보니 가린 자리의 변색은 물린 상처의 번짐과 모양이 다르다 — 그러나 그것을 가를 방법은 이 섬에 없다",
                 observable=npc_with_pc("$slot:person"), exposes=[Q + "npc_hidden_ill"])], terminal=True),
], slots={"person": {"type": "ACTOR", "candidates": ["진복례", "윤점순", "염기준", "전성남", "임재환"]}},
   resolve=("observed",), extra_cancel=all_(phase("gesture"), not_(npc_with_pc("$slot:person"))))
MAP["npc_hidden_ill"] = ("MIGRATED", ["yd_ev_concealed_symptom"], "숨기는 사람·감염 오인 가능성 이면 보존. 고발/공모 분기는 서사. 원문 1회 유지.")


# ══════════════════════════════════════════════════════════════
#  메인 — 세계 압력 아크 (결말 = 지침·플레이, 정본 분기 아님)
# ══════════════════════════════════════════════════════════════
chain("yd_ev_namhang_ration_crisis", "규율의 완성", all_(member(NH), AFTER_WEEK), [
    P("cut", "배급이 또 줄었다. 하층부에서 사람이 셋 빠졌고 둘은 어디로 갔는지 아무도 말하지 않는다. "
             "가능한 방향(원문 결말): 규율 유지 / 개혁 / 통제권 전복 — 플레이가 정한다.",
      steps=[sig("ration", "배급 줄 끝에서 죽 그릇이 반쯤만 채워진다. 하층부 침상 셋이 비어 있다",
                 where=NAMHANG_INNER, exposes=["yd.info.namhang.ration_cut"])]),
    P("grievance", "황순애 같은 하층부 노동자가 숫자를 댄다 — 상층부와 하층부 몫의 차이. 장부는 맞고, 맞기 때문에 문제다(규정대로의 격차).",
      frm="cut", when=all_(at("거주 시설 하층부"), elapsed(720)),
      steps=[sig("numbers", "하층부 노동자 하나가 벽에 숯으로 적은 숫자를 보여 준다 — 상층부 한 끼와 하층부 사흘 치가 같다",
                 where=["거주 시설 하층부"], exposes=["yd.info.namhang.ration_gap"])]),
    P("gathering", "하층부가 모인다. 숫자가 늘수록 되돌리기 어렵다. 간부들이 곧 알게 된다.",
      frm="grievance", when=elapsed(1440),
      steps=[place_npc("hwang", "황순애", "수습대 집결지"),
             sig("crowd", "수습대 집결지 바닥에 하층부 사람들이 말없이 모여 앉아 있다", where=["수습대 집결지", "거주 시설 하층부"])]),
    P("confrontation", "제1방어선 앞에서 양쪽이 마주 선다. 안쪽 문은 봉쇄됐다. 이 요새의 규율이 오늘 결정된다.",
      frm="gathering", when=elapsed(720),
      steps=[set_access("lock", "namhang_inner_door", "LOCKDOWN"), place_npc("kang", "강철수", "제1방어선"),
             sig("standoff", "안쪽 문 앞에 렌치와 쇠파이프를 든 기술자들과 하층부 사람들이 마주 선다. 강철수가 그 사이에 선다",
                 where=NAMHANG_INNER + ["제1방어선"])]),
    P("aftermath", "결정이 났다(서사 결과는 직전 플레이가 정한다). 요새는 다시 문을 연다.",
      frm="confrontation", when=elapsed(360),
      steps=[set_access("unlock", "namhang_inner_door", "ADMITTED"), place_npc("kang", "강철수", "상층부 거주구")],
      terminal=True),
], resolve=("aftermath",), relevance=["남항 중공업 연대"])
chain("yd_ev_namhang_clinic_records", "의료소 기록",
      all_(member(NH), at("지하 의료소"), act("READING", "SEARCHING"), life("yd_ev_namhang_ration_crisis", "ACTIVE")), [
    P("reading", "지하 의료소 기록을 넘긴다. 서늘하고, 공업용 절단기가 의료 도구로 놓여 있다."),
    P("gap", "사라진 사람들의 이름이 있고 그 옆에 처치 항목이 있다. 감염 판정 이후의 기록이 없다.",
      frm="reading", when=all_(at("지하 의료소"), elapsed(20)),
      steps=[sig("gap", "하층부에서 사라진 이름들이 처치 기록에 있다 — '판정' 칸 다음부터 모든 줄이 비어 있다",
                 where=["지하 의료소"], exposes=[Q + "main_namhang_order"])], terminal=True),
], resolve=("gap",), extra_cancel=all_(phase("reading"), not_(at("지하 의료소"))), relevance=["지하 의료소"])
MAP["main_namhang_order"] = ("SPLIT", ["yd_ev_namhang_ration_crisis", "yd_ev_namhang_clinic_records"],
                             "배급 격차→집결→대치 압력 단계를 세계 진행으로, 의료소 생체 연구 이면을 독립 노출 사건으로 분리. 결말 3종(규율·개혁·전복)은 지침으로 보존(정본 분기 아님) — 검토 항목.")

chain("yd_ev_haeryeon_drought", "물의 값", all_(member(HR), AFTER_WEEK), [
    P("low", "빗물 저장고 눈금이 바닥에 가깝다. 비가 오지 않은 지 오래다. 가능한 방향(원문 결말): 나눔 / 폐쇄 / 감춘 것 폭로 — 플레이가 정한다.",
      steps=[sig("gauge", "빗물 저장고 눈금이 바닥 두 칸 위에 멈춰 있다. 아무도 그 앞에서 오래 서 있지 않는다",
                 where=HAERYEON_INNER, exposes=["yd.info.haeryeon.low_water"])]),
    P("cut_again", "홈통이 다시 끊겼다. 절단면이 고르다 — 마모가 아니다. 물을 아래로 내보내던 이웃이 있다.",
      frm="low", when=all_(at("집수 설비"), act("SEARCHING", "WORKING"), elapsed(5)),
      steps=[sig("cut", "집수 설비 홈통 하나가 또 끊겨 있다 — 이번에도 톱날로 자른 듯 고른 절단면",
                 where=["집수 설비"], exposes=[Q + "join_haeryeon_water"])]),
    P("council", "회의가 열린다. 침묵 속에 손짓만 오간다. 배급을 줄일 것인가, 받는 사람을 줄일 것인가.",
      frm=["low", "cut_again"], when=elapsed(1440),
      steps=[place_npc("jo", "조원달", "해련사"), place_npc("beop", "법운", "해련사"),
             sig("council", "해련사 마루에 위원회가 모인다. 말 대신 손짓과 종이 쪽지가 오간다", where=["해련사", "해련사 배급소"])]),
    P("decision", "결정의 순간이 지났다(서사 결과는 직전 플레이가 정한다).", frm="council", when=elapsed(720),
      steps=[place_npc("jo", "조원달", "해련 자치 마을")], terminal=True),
], resolve=("decision",), relevance=["해련 자치 마을"])
chain("yd_ev_haeryeon_soundroom", "방음실 안쪽",
      all_(member(HR), at("방음실"), act("SEARCHING", "WAITING"), life("yd_ev_haeryeon_drought", "ACTIVE")), [
    P("inside", "방음실. 두꺼운 이불과 천이 겹겹이다. 쑥 향이 숨 막히게 짙다."),
    P("revealed", "방음실 안쪽에 감춰진 것이 있다 — 어른들이 향으로 가려 온 것.",
      frm="inside", when=all_(at("방음실"), elapsed(15)),
      steps=[sig("inner", "솜이불 더미 뒤 바닥판 틈에서 쑥 향으로도 덮이지 않는 냄새가 올라오고, 안쪽 칸막이 너머에서 쇳소리 섞인 숨소리가 난다",
                 where=["방음실"], exposes=[Q + "main_haeryeon_water"])], terminal=True),
], resolve=("revealed",), extra_cancel=all_(phase("inside"), not_(at("방음실"))), relevance=["방음실"])
MAP["main_haeryeon_water"] = ("SPLIT", ["yd_ev_haeryeon_drought", "yd_ev_haeryeon_soundroom"],
                              "가뭄→재절단→회의 압력 단계와 방음실 이면을 분리. 결말 3종(나눔·폐쇄·폭로)은 지침 보존 — 검토 항목.")

chain("yd_ev_jodo_closure_order", "명령의 끝", all_(member(JD), AFTER_WEEK), [
    P("order", "본섬 전초를 접고 방파제를 폐쇄한다는 명령서가 돈다. 건너오려는 자는 예외 없이 저지한다는 조항. "
               "가능한 방향(원문 결말): 복종 / 조건부 개방 설득 / 명령 거부 — 플레이가 정한다.",
      steps=[sig("order", "강의동 게시판에 방파제 폐쇄 명령서가 붙는다. 서명란 몇 칸이 비어 있다",
                 where=JODO_INNER + ["다중 검문소"], exposes=["yd.info.jodo.closure_order"])]),
    P("closed", "방파제가 닫혔다. 초소에 저지 지시가 내려갔다.", frm="order", when=elapsed(1440),
      steps=[set_access("shut", "jodo_causeway_barricade", "SHUT"),
             sig("shut_seen", "다중 검문소 앞 컨테이너가 하나 더 옮겨져 통로를 막는다", where=["다중 검문소", "아치 방파제", "방파제 입구"])]),
    P("unrest", "부대가 술렁인다. 탄약이 없다는 것과 상부가 무전을 끊었다는 것이 함께 돈다.", frm="closed", when=elapsed(1440),
      steps=[sig("murmur", "병영 복도에서 병사들이 낮게 수군댄다 — 쏠 탄도 없는데 무엇으로 막느냐고", where=JODO_INNER)]),
    P("resolution", "명령 체계에 대한 답이 나왔다(서사 결과는 직전 플레이가 정한다).", frm="unrest", when=elapsed(720), terminal=True),
], resolve=("resolution",), relevance=["조도 방위 사령부"])
chain("yd_ev_jodo_armory_check", "탄약 재고", all_(member(JD), at("군수 창고"), act("READING", "SEARCHING")), [
    P("ledger", "군수 창고 재고표를 본다."),
    P("empty", "탄약이 거의 없다 — 저지 명령은 실행 수단이 없는 명령이다.", frm="ledger", when=all_(at("군수 창고"), elapsed(15)),
      steps=[sig("empty", "직인 찍힌 탄약 장부의 마지막 잔량 칸 — 전면전 몇 분 분량의 숫자가 볼펜으로 두 번 고쳐져 있다",
                 where=["군수 창고"], exposes=["yd.info.jodo.ammo_empty"])], terminal=True),
], resolve=("empty",), extra_cancel=all_(phase("ledger"), not_(at("군수 창고"))), relevance=["군수 창고"])
chain("yd_ev_jodo_radio_truth", "끊긴 무전",
      all_(member(JD), at("태양광 패널 옥상"), act("WORKING"), life("yd_ev_jodo_closure_order", "ACTIVE")), [
    P("tuning", "옥상 안테나와 무전 설비를 다시 손본다."),
    P("heard", "잡음 사이로 본토 신호가 잡힌다. 지휘관은 이미 알고 있었다는 얼굴이다.", frm="tuning",
      when=all_(at("태양광 패널 옥상"), elapsed(40)),
      steps=[sig("mainland", "잡음 사이로 또렷한 한국어 신호 — 좌표와 응답 요청이 반복된다. 뒤에 선 지휘관은 놀라지 않는다",
                 where=["태양광 패널 옥상"], exposes=[Q + "main_jodo_command"])], terminal=True),
], resolve=("heard",), extra_cancel=all_(phase("tuning"), not_(at("태양광 패널 옥상"))), relevance=["태양광 패널 옥상"])
MAP["main_jodo_command"] = ("SPLIT", ["yd_ev_jodo_closure_order", "yd_ev_jodo_armory_check", "yd_ev_jodo_radio_truth"],
                            "폐쇄 명령→방파제 봉쇄(정본 접근 SHUT)→동요 압력 단계, 탄약 고갈·무전 차단 이면을 각각 노출. 결말 3종은 지침 보존 — 검토 항목.")

chain("yd_ev_jungri_debt_strike", "장부를 덮다", all_(member(JR), AFTER_WEEK), [
    P("refusal", "3등급 몇이 배급을 거부했다. 갚아도 줄지 않는 이자를 더는 못 견딘다. 윗선이 정리하라고 한다. "
                 "가능한 방향(원문 결말): 현행 유지 / 등급 재편 / 장부 소각 — 플레이가 정한다.",
      steps=[sig("refuse", "채무자 숙소 앞에 배급 그릇이 엎어진 채 놓여 있다. 받아 가는 사람이 없다",
                 where=["채무자 숙소", "바자르", "중리 교역소"], exposes=["yd.info.jungri.debt_strike"])]),
    P("uprising", "바자르가 멎는다. 셔터가 내려가고 장부방 앞에 사람이 모인다. 이 시장에서 처음 있는 일이다.",
      frm="refusal", when=elapsed(1440),
      steps=[place_npc("guard", "장철웅", "장부방"),
             sig("shutters", "바자르 1층 셔터가 하나둘 내려가고, 장부방 앞에 채무자들이 말없이 선다", where=["바자르", "장부방", "중리 교역소"])]),
    P("resolution", "등급이 사람을 나누는 방식에 대한 답이 나왔다(서사 결과는 직전 플레이가 정한다).",
      frm="uprising", when=elapsed(720), steps=[place_npc("guard", "장철웅", "경비조 대기소")], terminal=True),
], resolve=("resolution",), relevance=["중리 교역소"])
chain("yd_ev_jungri_ledger_audit", "이자 계산", all_(member(JR), at("장부방"), act("READING")), [
    P("audit", "장부의 계산을 직접 따져 본다."),
    P("rigged", "계산이 맞다 — 이 구조에서 3등급이 2등급이 되는 경우는 없다. 애초에 그렇게 설계됐다.",
      frm="audit", when=all_(at("장부방"), elapsed(30)),
      steps=[sig("rigged", "원금은 그대로인데 이자 줄만 세 배가 됐다. 어느 장을 넘겨도 3등급에서 2등급으로 올라간 이름이 없다",
                 where=["장부방"], exposes=[Q + "main_jungri_ledger"])], terminal=True),
], resolve=("rigged",), extra_cancel=all_(phase("audit"), not_(at("장부방"))), relevance=["장부방"])
MAP["main_jungri_ledger"] = ("SPLIT", ["yd_ev_jungri_debt_strike", "yd_ev_jungri_ledger_audit"],
                             "채무 거부→바자르 정지 압력 단계와 등급제 설계 이면을 분리. 결말 3종은 지침 보존 — 검토 항목.")

chain("yd_ev_lone_winter", "홀로 남다", all_(not_(member_any()), AFTER_WEEK), [
    P("cold_coming", "어느 거점에도 이름을 올리지 않은 채 여기까지 왔다. 겨울이 온다 — 지금의 자리로는 넘길 수 없다는 것을 몸이 먼저 안다. "
                     "가능한 방향(원문 결말): 스스로의 거처 / 느슨한 연대 / 완전한 은둔 / 결국 문을 두드림 — 플레이가 정한다."),
    P("first_frost", "첫서리가 내렸다. 물은 얼고, 나갈 수 있는 날이 준다.", frm="cold_coming", when=after("2016-11-15", "05:00"),
      steps=[sig("frost", "새벽 공기에 입김이 하얗게 맺히고, 빗물 통 가장자리에 살얼음이 끼었다")]),
    P("winter", "겨울 한복판이다(결과는 플레이가 정한다).", frm="first_frost", when=after("2016-12-20", "05:00"), terminal=True),
], resolve=("winter",), extra_cancel=member_any())
chain("yd_ev_lone_traces", "앞선 사람의 자리",
      all_(not_(member_any()), AFTER_WEEK, at_any(["은신처", "폐채석장", "옥탑방", "배수지", "산불감시초소"]), act("SEARCHING")), [
    P("searching", "누군가 이곳에서 혼자 버틴 흔적을 살핀다."),
    P("found", "앞서 혼자 겨울을 넘긴 사람이 남긴 거처의 구조와 방법이 보인다.", frm="searching",
      when=all_(act("SEARCHING"), elapsed(20)),
      steps=[sig("traces", "벽 틈을 신문지와 비닐로 이중으로 막은 자국, 빗물을 끌어오는 비닐 홈, 날짜를 새긴 눈금 — 누군가 여기서 겨울 한 철을 났다",
                 exposes=[Q + "main_alone_survive"])], terminal=True),
], resolve=("found",), extra_cancel=all_(phase("searching"), not_at_any(["은신처", "폐채석장", "옥탑방", "배수지", "산불감시초소"])))
MAP["main_alone_survive"] = ("SPLIT", ["yd_ev_lone_winter", "yd_ev_lone_traces"],
                             "무소속 겨울 압력(정본 날짜 기반 첫서리·겨울)과 앞선 무소속자 흔적 이면을 분리. 결말 4종은 지침 보존. 동료 NPC 조건은 미표현 — 검토 항목.")

chain("yd_ev_boat_rumor", "배를 만들다",
      all_(AFTER_WEEK, at_any(["청학동 선착장", "영선동 방파제", "동삼동 갯가", "감지해변", "물양장", "갯바위 지대", "어판장",
                               "중리 해변", "방치된 부두", "해안 창고", "흰여울 절벽길"])), [
    P("sea_gaze", "바다를 본다. 다리는 전부 끊겼고 남은 길은 이것뿐이다. 사람들은 바다가 죽음이라 말하지만 직접 확인한 사람은 없다(고 여겨진다). "
                  "가능한 방향(원문 결말): 혼자 출항 / 함께 출항 / 남음 — 출항은 돌아오지 못하는 결말로만 다룬다(세계관: 해상 탈출은 자살 행위)."),
    P("warned", "바다를 아는 사람이 말해 준다 — 물에 들어간 사람들이 어떻게 됐는지, 바다로 나간 사람이 하나도 돌아오지 않았다는 것.",
      frm="sea_gaze", when=any_(*[all_(npc_with_pc(n), act("WAITING"), elapsed(15)) for n in ("진복례", "이선희", "황민석", "장철웅", "전성남")]),
      steps=[sig("warn", "바다를 아는 사람이 손을 저으며, 물 아래 무언가가 배 밑을 스치고 지나간 이야기를 담담하게 한다 — 나간 배는 하나도 돌아오지 않았다고",
                 exposes=[Q + "main_alone_escape"])], terminal=True),
], resolve=("warned",), extra_cancel=all_(phase("sea_gaze"), elapsed(4320)))
MAP["main_alone_escape"] = ("MIGRATED", ["yd_ev_boat_rumor"], "해상 탈출 충동·바다 사람의 경고·돌아오지 못하는 바다 이면 보존. 배 제작·출항 결말은 지침(정본 이동 없음 — 바다는 이동 구간이 아니다) — 검토 항목.")

_SECRETS = [Q + "main_namhang_order", Q + "main_haeryeon_water", Q + "main_jodo_command", Q + "main_jungri_ledger"]
_TWO = any_(*[all_(knows(a), knows(b)) for i, a in enumerate(_SECRETS) for b in _SECRETS[i + 1:]])
chain("yd_ev_island_balance", "섬을 흔들다", all_(AFTER_WEEK, _TWO), [
    P("leverage", "네 거점이 감춘 것 중 둘 이상을 손에 쥐었다. 균형은 서로를 견제하며 유지돼 왔고, 그것이 누구를 위한 것인지는 아래 사람들이 안다. "
                  "가능한 방향(원문 결말): 단일 공동체 / 전쟁 / 붕괴 — 플레이가 정한다. 직접 전투보다 대리인을 통한 간접 충돌이 개연적이다(세계관 7.5)."),
    P("insight", "거물에게서 분업의 내막을 들었다 — 하나가 무너지면 나머지도 오래 버티지 못한다.",
      frm="leverage", when=any_(*[all_(npc_with_pc(n), act("WAITING"), elapsed(20))
                                  for n in ("나용순", "강철수", "이현석", "조원달", "전성남", "엄주섭", "민도현")]),
      steps=[sig("web", "거물이 손가락으로 탁자에 네 점을 찍고 선을 긋는다 — 물, 식량, 전력, 교역이 서로를 물고 있다고",
                 exposes=[Q + "main_upheaval"])], terminal=True),
], resolve=("insight",))
MAP["main_upheaval"] = ("MIGRATED", ["yd_ev_island_balance"], "네 거점 비밀 중 둘 이상 인지(PC 인지 CONTENT 조건)로 개방, 분업 균형 이면 보존. 원문 '이탈 2회' 성향 조건은 인지 조건으로 대체 — 검토 항목.")

# 초반·가입·장소·인물·메인 외 원문 퀘스트 처분 확인용
ALL_QUESTS = None


def apply(raw):
    from core import world_ids as WI
    raw = copy.deepcopy(raw)
    for name, aid in NPC_IDS.items():
        prof = raw["default_npcs"][name]
        prof["actor_id"] = aid
        if name in MOVERS:                      # 사건이 START_MOVEMENT로 움직이는 NPC만 공간 지식이 필요하다
            prof["knowledge_seed"] = "yd_namhang_runner"
    genesis(WI.npc_inline_secret_info_id)
    raw["information_definitions"] = copy.deepcopy(infos.I)
    raw["event_templates"] = copy.deepcopy(T)
    return raw
