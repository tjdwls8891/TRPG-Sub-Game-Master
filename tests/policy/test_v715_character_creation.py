# -*- coding: utf-8 -*-
"""v7.15.0 — 캐릭터 생성 개선(기획 C1~C7·Q1~Q9·기술 B/B, 설계안 §10.2 T01~T18).

채팅 답변(모달 대체)·장문 직접/질문형 가이드·초안 검토·전체 최종 검토·저장 프로필 선택 수정·세션 영속과
재시작 복원. AI(profile_ai.validate/merge)는 대역으로 바꿔 호출 수·입력만 검증한다 — 실제 모델 품질은 미검증.
"""
from __future__ import annotations

import asyncio
import copy
import json
import pathlib

import pytest

import cogs.game as game_mod
import cogs.gm as gm_mod
import core
import prompts
from core import profile_creation_ui as PCU
from core import profile_gen as PG
from tests.conftest import GAME_CH, PLAYER_UID
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeMessage, FakeUser

pytestmark = pytest.mark.policy

REPO = pathlib.Path(__file__).resolve().parents[2]
OTHER_UID = "555777"

GUIDE = {"direct": True, "compose_rules": "답한 요소만 잇는다.",
         "questions": [
             {"id": "build", "prompt": "체격은?", "required": True, "options": ["마른 편", "다부진 편"]},
             {"id": "face", "prompt": "얼굴은?", "required": True},
             {"id": "mark", "prompt": "특징은?", "required": False}]}

SCN = {
    "worldview": "테스트 세계",
    "jobs": {"어부": {"desc": "바다 일"}, "목수": {"desc": "나무 일"}},
    "survival_skills": {"직업": {"어부": ["그물"], "목수": ["톱질"]}},
    "starting_items": {"공통": ["물병"]},
    "profile_creation": [
        {"field": "성별", "module": "select_one", "args": {"options": ["남", "여"]}},
        {"field": "이름", "module": "ai_validate", "args": {"prompt": "이름을 적어 주십시오.", "rules": "r"}},
        {"field": "직업", "module": "select_one", "args": {"from": "jobs", "describe_from": "jobs"}},
        {"field": "스킬", "module": "intersect_list",
         "args": {"source": "survival_skills", "depends": ["직업"], "pick": "1"}},
        {"field": "능력치", "module": "roll_stats", "args": {"stats": ["근력", "민첩"], "max_single": 20, "min_single": 1}},
        {"field": "외형", "module": "ai_merge", "args": {"prompt": "외형을 알려 주십시오.", "max_length": 40,
                                                       "creation_guide": GUIDE}},
        {"field": "복장", "module": "ai_merge", "args": {"prompt": "옷은?", "max_length": 30}},
    ],
}


class AI:
    """profile_ai 대역 — 호출 기록. 실제 provider 없음."""

    def __init__(self):
        self.validate_calls, self.merge_calls = [], []
        self.reject = set()
        self.merge_out = None
        self.gate = None                           # merge를 멈춰 두는 Event(경합 시험)

    async def validate(self, bot, session, *, field, value, scenario_data=None, rules=""):
        self.validate_calls.append((field, value))
        if value in self.reject:
            return {"verdict": "reject", "reason": f"{value}는 세계관과 맞지 않습니다."}
        return {"verdict": "ok", "reason": ""}

    async def merge(self, bot, session, *, field, values, max_length=300, rules=""):
        self.merge_calls.append({"field": field, "values": list(values), "rules": rules})
        if self.gate is not None:
            await self.gate.wait()
        merged = self.merge_out(values) if self.merge_out else " / ".join(values)
        return {"merged": merged, "dropped": [], "fallback": False, "too_long": len(merged) > max_length}


@pytest.fixture
def pc(fake_bot, session_factory, monkeypatch):
    s = session_factory("pc-session", scenario=copy.deepcopy(SCN))
    s.scenario_id = "t"
    s.players = {}
    s.gm_active = False
    s.current_turn_logs = []
    fake_bot.register_session(s)
    fake_bot.user = FakeUser(999)
    ai = AI()
    monkeypatch.setattr(core.profile_ai, "validate", ai.validate)
    monkeypatch.setattr(core.profile_ai, "merge", ai.merge)
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: False)
    PCU._ACTIVE.clear()
    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = fake_bot
    gm._session_locks = {}
    r = type("R", (), {})()
    r.bot, r.sess, r.ai, r.gm, r.game = fake_bot, s, ai, gm, game_mod.GameCog(fake_bot)
    r.ch = fake_bot.get_channel(GAME_CH)
    yield r
    PCU._ACTIVE.clear()


def _txt(m):
    """P2 #4: 대화 본문은 content, 정형 필드는 카드 Embed — 화면에 보이는 글 전체."""
    parts = [str(m.content or "")]
    for e in m.embeds or ():
        parts.append(str(e.description or ""))
        parts += [str(f.value) for f in e.fields]
    return "\n".join(parts)


def st(r, uid=PLAYER_UID, ch=None):
    return PCU._ACTIVE[((ch or r.ch).id, str(uid))]


def labels(state):
    return [getattr(c, "label", None) or getattr(c, "placeholder", None) for c in state.message.view.children]


async def press(r, label, uid=PLAYER_UID, view=None, state=None, ch=None):
    state = state or st(r, uid, ch)
    v = view or state.message.view
    item = next(c for c in v.children if getattr(c, "label", None) == label)
    await item.callback(FakeInteraction(user=FakeUser(int(uid)), channel=ch or r.ch, message=state.message))


async def pick(r, placeholder, value, uid=PLAYER_UID, ch=None):
    state = st(r, uid, ch)
    item = next(c for c in state.message.view.children if getattr(c, "placeholder", None) == placeholder)
    item._values = [value]
    await item.callback(FakeInteraction(user=FakeUser(int(uid)), channel=ch or r.ch, message=state.message))


def msg(r, text, uid=PLAYER_UID, ch=None):
    m = FakeMessage(channel=ch or r.ch, content=text)
    m.author = FakeUser(int(uid))
    m.author.bot = False
    return m


async def chat(r, text, uid=PLAYER_UID, ch=None):
    """GMCog·GameCog 두 리스너에 같은 메시지를 흘린다(실제 이벤트와 같다)."""
    m = msg(r, text, uid, ch)
    before = set(asyncio.all_tasks())
    await r.game.on_message(m)
    await r.gm.on_message(m)
    new = [t for t in asyncio.all_tasks() - before if t is not asyncio.current_task()]
    if new:
        await asyncio.wait(new, timeout=2)
    return m


async def start(r, uid=PLAYER_UID, ch=None):
    return await PCU.start(r.bot, r.sess, uid, "(이름 미정)", ch or r.ch)


async def to_appearance(r, uid=PLAYER_UID):
    """성별·이름·직업·스킬(자동)·능력치까지 진행 — 외형 단계에서 멈춘다."""
    await start(r, uid)
    await press(r, "남", uid)
    await press(r, "확정", uid)
    await chat(r, "홍길동", uid)
    await press(r, "확정", uid)
    await press(r, "어부", uid)
    await press(r, "확정", uid)
    await press(r, "✅ 확정", uid)                      # 능력치


async def guided_appearance(r, uid=PLAYER_UID):
    await press(r, "🧭 질문에 답하며 정리", uid)
    await press(r, "마른 편", uid)
    await chat(r, "짧은 머리에 수염", uid)
    await press(r, "건너뛰기", uid)


# ── T01·T02 기존 동작 ─────────────────────────────────────────

async def test_t01_choice_skill_stat_paths_unchanged(pc):
    await start(pc)
    assert {"남", "여"} <= set(labels(st(pc)))                    # 선택은 버튼 그대로
    await press(pc, "남")
    await press(pc, "확정")
    await chat(pc, "홍길동")
    await press(pc, "확정")
    await press(pc, "어부")
    await press(pc, "확정")
    run = st(pc).run
    assert run["chosen"]["스킬"] == ["그물"]                       # 직업 종속 자동 선택 그대로
    assert "✅ 확정" in labels(st(pc))                            # 능력치 굴림 UI 그대로
    stats = run["pending"]["value"]
    assert set(stats) == {"근력", "민첩"} and all(1 <= v <= 20 for v in stats.values())


async def test_t02_scenario_without_profile_creation_skips(fake_bot, session_factory):
    s = session_factory("noflow", scenario={"worldview": "x"})
    s.scenario_id = "x"
    fake_bot.register_session(s)
    ch = fake_bot.get_channel(GAME_CH)
    PCU._ACTIVE.clear()
    assert await PCU.start(fake_bot, s, PLAYER_UID, "a", ch) is None
    assert "자동 생성 절차" in str(ch.sent[-1].content) and not PCU._ACTIVE
    assert s.profile_creation_progress == {}


# ── T03·T04 채팅 경계 ────────────────────────────────────────

async def test_t03_own_chat_answer_consumed_once_and_not_logged(pc):
    await start(pc)
    await press(pc, "남")
    await press(pc, "확정")
    assert PCU.CHAT_HINT in _txt(st(pc).message)           # 모달 대신 채팅 안내
    assert "입력하기" not in labels(st(pc))
    await chat(pc, "홍길동")
    await chat(pc, "임꺽정")                                                # 같은 질문의 두 번째 답 — 무시
    assert pc.ai.validate_calls == [("이름", "홍길동")]
    assert st(pc).run["pending"] == {"field": "이름", "value": "홍길동"}
    assert pc.sess.current_turn_logs == []                                   # 첫 턴 맥락에 안 들어간다


async def test_t04_other_user_channel_command_and_idle_not_consumed(pc):
    pc.sess.players[OTHER_UID] = {"name": "구경꾼", "profile": {}}
    await start(pc)
    # 텍스트를 기다리지 않는 화면(성별 버튼)에서 쓴 채팅 — 소비 안 함, 맥락 기록도 안 함
    await chat(pc, "아무 말")
    assert st(pc).run["chosen"] == {} and pc.sess.current_turn_logs == []
    await press(pc, "남")
    await press(pc, "확정")
    other_ch = FakeChannel(424242)
    await chat(pc, "다른 채널", ch=other_ch)                               # 다른 채널
    await chat(pc, "!도움")                                                 # 명령
    await chat(pc, "남의 답", uid=OTHER_UID)                                # 다른 플레이어
    assert pc.ai.validate_calls == [] and st(pc).run.get("await")
    assert pc.sess.current_turn_logs == ["[구경꾼]: 남의 답"]               # 생성 중이 아닌 사람은 종전대로 기록
    assert not PCU.claims(pc.bot, msg(pc, "x", uid=OTHER_UID))


async def test_t04_logging_resumes_after_creation_completes(pc):
    await to_appearance(pc)
    await guided_appearance(pc)
    await press(pc, "✅ 이대로 확정")
    await chat(pc, "낡은 작업복")
    await press(pc, "✅ 이대로 확정")
    await press(pc, "✅ 이대로 시작")
    await press(pc, "저장 안 함")
    assert not PCU.claims(pc.bot, msg(pc, "x")) and pc.sess.profile_creation_progress == {}
    await chat(pc, "이제 게임 이야기")
    assert pc.sess.current_turn_logs[-1].endswith("이제 게임 이야기")


async def test_t03_flow_creator_not_yet_player_passes_chat_guard(pc):
    """GM 스페이스 플로우의 개설자는 finish 전까지 players에 없다 — 생성 답변이 차단·삭제되면 안 된다."""
    assert PLAYER_UID not in pc.sess.players
    pc.bot.is_owner = lambda _u: _false()
    await start(pc)
    m = msg(pc, "아무 말")
    m.guild = object()
    assert await core.chat_guard(pc.bot, m) is True and not m.deleted
    stranger = msg(pc, "끼어들기", uid=OTHER_UID)
    stranger.guild = object()
    assert await core.chat_guard(pc.bot, stranger) is False                   # 생성 중이 아닌 비참가자는 그대로 차단


async def _false():
    return False


# ── T05 중복·경합 ────────────────────────────────────────────

async def test_t05_stale_button_duplicate_and_input_during_ai(pc):
    await start(pc)
    old_view = st(pc).message.view
    await press(pc, "남")
    await press(pc, "확정")
    await press(pc, "남", view=old_view)                                    # 낡은 화면의 버튼 — 무시
    assert st(pc).run["chosen"] == {"성별": "남"} and st(pc).run["index"] == 1
    await chat(pc, "홍길동")
    await press(pc, "확정")
    await press(pc, "어부")
    await press(pc, "확정")
    await press(pc, "✅ 확정")
    await press(pc, "🧭 질문에 답하며 정리")
    await press(pc, "마른 편")
    await chat(pc, "짧은 머리")
    # 마지막 선택 질문 건너뛰기 → 조합 중(merge 대기) — 그 사이 채팅·버튼은 반영되지 않는다
    pc.ai.gate = asyncio.Event()
    task = asyncio.create_task(press(pc, "건너뛰기"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if st(pc).run["phase"] == PCU.P_PROCESSING:
            break
    assert st(pc).run["phase"] == PCU.P_PROCESSING and st(pc).message.view is None
    await chat(pc, "끼어든 답")
    pc.ai.gate.set()
    await task
    assert len(pc.ai.merge_calls) == 1 and st(pc).run["phase"] == PCU.P_DRAFT
    assert "끼어든 답" not in json.dumps(st(pc).run, ensure_ascii=False)


# ── T06~T10 장문 · AI · 세계관 ───────────────────────────────

async def test_t06_direct_long_text_kept_whole_and_partially_edited(pc):
    await to_appearance(pc)
    await press(pc, "✍️ 직접 작성")
    long = "키가 크고 마른 체형에 짧은 머리, 왼쪽 눈썹 위에 작은 상처가 있고 늘 피곤해 보이는 얼굴"
    await chat(pc, long)
    lf = st(pc).run["lf"]
    assert lf["draft"] == long and lf["too_long"]                            # 자르지 않는다
    approve = next(c for c in st(pc).message.view.children if getattr(c, "label", "") == "✅ 이대로 확정")
    assert approve.disabled                                                  # 줄이기 전에는 승인 불가
    assert pc.ai.merge_calls == []                                           # 직접 작성 한 건은 AI 조합 없음
    pc.ai.merge_out = lambda values: "마른 장신, 짧은 머리"
    await press(pc, "✏️ 부분 수정")
    await chat(pc, "짧게 줄여 줘")
    assert pc.ai.merge_calls[-1]["values"] == [long, "짧게 줄여 줘"]           # 기존 merge 재사용
    assert st(pc).run["lf"]["draft"] == "마른 장신, 짧은 머리" and not st(pc).run["lf"]["too_long"]
    await press(pc, "✅ 이대로 확정")
    assert st(pc).run["chosen"]["외형"] == "마른 장신, 짧은 머리"


async def test_t07_t08_required_optional_and_single_merge_of_answers(pc):
    await to_appearance(pc)
    await press(pc, "🧭 질문에 답하며 정리")
    assert "건너뛰기" not in labels(st(pc))                                  # 필수 질문은 건너뛸 수 없다
    await press(pc, "마른 편")
    assert "건너뛰기" not in labels(st(pc))
    await chat(pc, "짧은 머리에 수염")
    assert "건너뛰기" in labels(st(pc))                                      # 선택 질문
    await press(pc, "건너뛰기")
    assert len(pc.ai.merge_calls) == 1                                       # 보조 질문마다가 아니라 한 번
    call = pc.ai.merge_calls[0]
    assert call["values"] == ["체격은? — 마른 편", "얼굴은? — 짧은 머리에 수염"]   # 답한 것만, 미응답 채우지 않음
    assert call["rules"] == "답한 요소만 잇는다."
    assert st(pc).run["phase"] == PCU.P_DRAFT and "외형" not in st(pc).run["chosen"]


async def test_t09_reselect_question_and_profile_untouched_before_approval(pc):
    await to_appearance(pc)
    await guided_appearance(pc)
    await pick(pc, "질문 다시 고르기", "0")
    assert st(pc).run["lf"]["answers"]["face"] == "짧은 머리에 수염"          # 이전 답 보존
    await press(pc, "다부진 편")
    assert pc.ai.merge_calls[-1]["values"][0] == "체격은? — 다부진 편"
    assert st(pc).run["phase"] == PCU.P_DRAFT
    assert PLAYER_UID not in pc.sess.players                                 # 승인 전 정본 불변


async def test_t10_reject_reason_reinput_and_no_suggestion_anywhere(pc):
    pc.ai.reject.add("드래곤")
    await start(pc)
    await press(pc, "남")
    await press(pc, "확정")
    await chat(pc, "드래곤")
    s = st(pc)
    assert s.run.get("await") and s.run.get("pending") is None               # 같은 항목 재입력
    assert "세계관과 맞지 않습니다" in _txt(s.message)
    assert "예시" not in _txt(s.message)
    sch = prompts.PROFILE_VALIDATE_RESPONSE_SCHEMA
    assert "suggestion" not in sch["properties"] and "suggestion" not in sch["required"]
    assert "예시" not in core.profile_ai.format_validation("이름", {"verdict": "reject", "reason": "x"})


async def test_t10_validate_parser_drops_suggestion(monkeypatch, session_factory):
    async def fake_call(*_a, **_k):
        return True, {"verdict": "reject", "reason": "충돌", "suggestion": "대신 이렇게"}
    monkeypatch.setattr(core.profile_ai, "_call", fake_call)
    out = await core.profile_ai.validate(None, session_factory(), field="이름", value="x")
    assert out == {"verdict": "reject", "reason": "충돌"}


async def test_merge_never_truncates(monkeypatch, session_factory):
    s = session_factory()
    one = await core.profile_ai.merge(None, s, field="외형", values=["가" * 300], max_length=250)
    assert one["merged"] == "가" * 300 and one["too_long"]

    async def fail(*_a, **_k):
        return False, None
    monkeypatch.setattr(core.profile_ai, "_call", fail)
    two = await core.profile_ai.merge(None, s, field="외형", values=["가" * 200, "나" * 200], max_length=250)
    assert "가" * 200 in two["merged"] and "나" * 200 in two["merged"] and two["fallback"] and two["too_long"]


# ── T11 마스터 대사 ─────────────────────────────────────────

async def test_t11_shared_master_lines_and_step_override(pc, monkeypatch):
    monkeypatch.setattr(core.master.random, "choice", lambda seq: seq[0])
    ev = json.loads((REPO / "assets" / "master_dialogues_v1.json").read_text(encoding="utf-8"))["events"]
    for k in ("profile.mode_choice", "profile.guide_question", "profile.draft_review", "profile.edit_prompt",
              "profile.final_review", "profile.resume", "profile.merged"):
        assert len(ev[k]["variants"]) >= 2, k
    pc.sess.scenario_data["profile_creation"][5]["args"]["master_keys"] = {"mode_choice": "profile.resume"}
    assert PG.validate_steps(pc.sess.scenario_data) == []
    await to_appearance(pc)
    assert ev["profile.resume"]["variants"][0] in _txt(st(pc).message)   # 단계 오버라이드
    bad = copy.deepcopy(pc.sess.scenario_data)
    bad["profile_creation"][5]["args"]["master_keys"] = {"mode_choice": "profile.없는키"}
    assert any("없는키" in e for e in PG.validate_steps(bad))
    assert pc.ai.merge_calls == [] and len(pc.ai.validate_calls) == 1        # 반응 연출용 호출 없음


def test_guide_authoring_validation():
    sd = copy.deepcopy(SCN)
    sd["profile_creation"][5]["args"]["creation_guide"]["questions"].append({"id": "build", "prompt": "또?"})
    sd["profile_creation"][0]["args"]["creation_guide"] = {"questions": [{"id": "a", "prompt": "b"}]}
    errs = PG.validate_steps(sd)
    assert any("중복" in e for e in errs) and any("텍스트 단계" in e for e in errs)
    assert PG.creation_guide(sd["profile_creation"][5]) is None                # 깨진 가이드는 직접 입력으로


# ── T12 최종 검토 ────────────────────────────────────────────

async def test_t12_finish_only_after_final_approval_and_once(pc):
    await to_appearance(pc)
    await guided_appearance(pc)
    await press(pc, "✅ 이대로 확정")
    await chat(pc, "낡은 작업복")
    await press(pc, "✅ 이대로 확정")
    s = st(pc)
    assert s.run["phase"] == PCU.P_FINAL
    assert PLAYER_UID not in pc.sess.players and not pc.sess.resources        # 최종 승인 전 미반영
    final_view = s.message.view
    await press(pc, "✅ 이대로 시작")
    assert pc.sess.players[PLAYER_UID]["name"] == "홍길동"
    assert pc.sess.resources["홍길동"] == {"물병": 1}
    await press(pc, "✅ 이대로 시작", view=final_view)                        # 중복 클릭 — 무시
    assert pc.sess.resources["홍길동"] == {"물병": 1}
    assert pc.sess.profile_creation_progress[PLAYER_UID]["run"]["finalized"] is True


# ── T13 저장 프로필 선택 수정 ────────────────────────────────

async def _prefill_start(r, fields):
    await core.profiles.create(PLAYER_UID, name="홍길동", scenario_id="t", fields=fields)
    original = json.loads(pathlib.Path(f"profiles/{PLAYER_UID}.json").read_text(encoding="utf-8"))
    import core.stats as stats_mod
    stats_mod.has_played = lambda *_a, **_k: True
    await start(r)
    pid = original["profiles"][0]["id"]
    await pick(r, "저장해둔 프로필 사용", pid)
    return original


async def test_t13_loaded_profile_goes_to_review_and_edits_only_chosen_fields(pc, monkeypatch):
    fields = {"성별": "남", "이름": "홍길동", "직업": "어부", "스킬": ["그물"], "능력치": {"근력": 12, "민첩": 9},
              "외형": "마른 체형", "복장": "작업복"}
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: True)
    original = await _prefill_start(pc, fields)
    s = st(pc)
    assert s.run["phase"] == PCU.P_FINAL and PLAYER_UID not in pc.sess.players   # 즉시 반영하지 않는다
    await pick(pc, "고칠 항목", "직업")
    await press(pc, "목수")
    await press(pc, "확정")
    run = st(pc).run
    assert run["phase"] == PCU.P_FINAL
    assert run["chosen"]["스킬"] == ["톱질"]                                   # 의존 필드만 다시
    assert run["chosen"]["능력치"] == {"근력": 12, "민첩": 9} and isinstance(run["chosen"]["능력치"], dict)
    assert run["chosen"]["외형"] == "마른 체형"
    # 장문 필드 수정 — 이전 확정본을 초안으로 다시 열고, 취소하면 원래대로
    await pick(pc, "고칠 항목", "외형")
    assert st(pc).run["lf"]["draft"] == "마른 체형" and st(pc).run["phase"] == PCU.P_DRAFT
    await press(pc, "↩ 원래대로")
    assert st(pc).run["chosen"]["외형"] == "마른 체형" and st(pc).run["phase"] == PCU.P_FINAL
    now = json.loads(pathlib.Path(f"profiles/{PLAYER_UID}.json").read_text(encoding="utf-8"))
    assert now == original                                                    # 원본 저장 프로필 불변


async def test_t13_corrupt_loaded_type_blocks_until_reentered(pc, monkeypatch):
    fields = {"성별": "남", "이름": "홍길동", "직업": "어부", "스킬": ["그물"], "능력치": "근력 12",
              "외형": "마른 체형", "복장": "작업복"}
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: True)
    await _prefill_start(pc, fields)
    s = st(pc)
    assert "능력치" in s.run["final_invalid"]
    start_btn = next(c for c in s.message.view.children if getattr(c, "label", "") == "✅ 이대로 시작")
    assert start_btn.disabled                                                 # 자동 형변환·묵인 없음
    await pick(pc, "고칠 항목", "능력치")
    await press(pc, "✅ 확정")
    assert isinstance(st(pc).run["chosen"]["능력치"], dict) and not st(pc).run["final_invalid"]


# ── T14~T16 영속 · 재시작 복원 · 동시 참가자 ─────────────────

def _reload(r):
    """data.json에서 새 세션 객체를 복원한다(재시작 시 SESSION_FIELDS 일괄 복구와 같은 방식)."""
    data = json.loads(pathlib.Path(f"sessions/{r.sess.session_id}/data.json").read_text(encoding="utf-8"))
    s2 = core.TRPGSession(r.sess.session_id, r.sess.game_ch_id, r.sess.master_ch_id, "테스트",
                          data.get("scenario_data") or r.sess.scenario_data)
    for k in ("players", "npcs", "resources", "statuses", "current_turn_logs"):   # cache.py 핵심 필드 복구
        setattr(s2, k, data.get(k, getattr(s2, k)))
    for k in core.SESSION_FIELDS:
        if k in data:
            setattr(s2, k, data[k])
    s2.scenario_data = r.sess.scenario_data
    s2.scenario_id = r.sess.scenario_id
    r.bot.active_sessions = {}
    r.bot.register_session(s2)
    PCU._ACTIVE.clear()
    r.sess = s2
    return s2


async def test_t14_restart_mid_guide_restores_cursor_and_answers(pc):
    await to_appearance(pc)
    await press(pc, "🧭 질문에 답하며 정리")
    await press(pc, "마른 편")
    old_gen = st(pc).run["gen"]
    _reload(pc)
    assert await PCU.resume_all(pc.bot) == 1
    s = st(pc)
    assert s.run["lf"]["answers"] == {"build": "마른 편"} and s.run["lf"]["q"] == 1
    assert s.run["gen"] > old_gen and s.run["chosen"]["이름"] == "홍길동"
    await chat(pc, "짧은 머리")                                                # 복원 화면에서 이어서 답
    assert st(pc).run["lf"]["answers"]["face"] == "짧은 머리"


async def test_t15_restart_during_draft_and_processing(pc):
    await to_appearance(pc)
    await guided_appearance(pc)
    draft = st(pc).run["lf"]["draft"]
    _reload(pc)
    await PCU.resume_all(pc.bot)
    s = st(pc)
    assert s.run["phase"] == PCU.P_DRAFT and s.run["lf"]["draft"] == draft
    assert PLAYER_UID not in pc.sess.players                                   # 자동 확정 없음
    # 처리 중 재시작 — 결과를 추정하지 않고 같은 입력을 다시 받는다
    await press(pc, "✏️ 부분 수정")
    pc.ai.gate = asyncio.Event()
    task = asyncio.create_task(chat(pc, "수염은 빼 줘"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if pc.sess.profile_creation_progress[PLAYER_UID]["run"]["phase"] == PCU.P_PROCESSING:
            break
    assert pc.sess.profile_creation_progress[PLAYER_UID]["run"]["phase"] == PCU.P_PROCESSING
    _reload(pc)
    pc.ai.gate.set()
    await task
    await PCU.resume_all(pc.bot)
    s = st(pc)
    assert s.run["proc"] is None and s.run["lf"]["draft"] == draft
    assert "수염은 빼 줘" in _txt(s.message)


async def test_t16_two_players_isolated_and_finalized_restart_no_double_finish(pc):
    pc.sess.players[OTHER_UID] = {"name": "을", "profile": {}}
    await start(pc)
    await start(pc, OTHER_UID)
    await press(pc, "남")
    await press(pc, "여", OTHER_UID)
    await press(pc, "확정")
    await press(pc, "확정", OTHER_UID)
    await chat(pc, "갑돌")
    await chat(pc, "을순", OTHER_UID)
    assert st(pc).run["pending"]["value"] == "갑돌" and st(pc, OTHER_UID).run["pending"]["value"] == "을순"
    assert set(pc.sess.profile_creation_progress) == {PLAYER_UID, OTHER_UID}
    # 최종 승인 뒤·저장 선택 전 재시작 — 저장 화면만 다시, finish 재실행 없음
    PCU._ACTIVE.clear()
    pc.sess.profile_creation_progress = {}
    await to_appearance(pc)
    await guided_appearance(pc)
    await press(pc, "✅ 이대로 확정")
    await chat(pc, "낡은 작업복")
    await press(pc, "✅ 이대로 확정")
    await press(pc, "✅ 이대로 시작")
    _reload(pc)
    await PCU.resume_all(pc.bot)
    assert pc.sess.resources["홍길동"] == {"물병": 1}
    assert "💾 사전 프로필로 저장" in labels(st(pc))
    flow = []

    async def _done(*_a, **_k):
        flow.append(1)
    import core.session_flow as sf
    pc.sess.creation_state = {"step": "profile"}
    orig = sf.on_profile_done
    sf.on_profile_done = _done
    try:
        cur = st(pc)
        item = next(c for c in cur.message.view.children if getattr(c, "label", None) == "저장 안 함")
        for _ in range(2):                                                     # 같은 버튼 두 번(연타)
            await item.callback(FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=pc.ch,
                                                message=FakeMessage(channel=pc.ch)))
    finally:
        sf.on_profile_done = orig
    assert flow == [1]                                                        # 다음 단계 1회


# ── T17 후속 초기 지식 입력 보존 ─────────────────────────────

async def test_t17_approved_persona_preserved_without_knowledge_feature(pc):
    await to_appearance(pc)
    pc.ai.merge_out = lambda values: "마른 체형에 짧은 머리와 수염"
    await guided_appearance(pc)
    await press(pc, "✅ 이대로 확정")
    await chat(pc, "낡은 작업복")
    await press(pc, "✅ 이대로 확정")
    await press(pc, "✅ 이대로 시작")
    p = pc.sess.players[PLAYER_UID]
    assert p["profile"]["외형"] == "마른 체형에 짧은 머리와 수염" == p["appearance"]
    assert p["profile"]["복장"] == "낡은 작업복" and p["profile"]["직업"] == "어부"
    assert not pc.sess.cognition_state and not pc.sess.information_state           # 초기 지식 미구현


# ── T18 영도 저작 가이드 ─────────────────────────────────────

async def test_t18_yeongdo_guides_authored_and_followed_in_order(fake_bot, session_factory, monkeypatch):
    sd = json.loads((REPO / "scenarios" / "영도.json").read_text(encoding="utf-8"))
    assert PG.validate_steps(sd) == []
    steps = {s["field"]: s for s in sd["profile_creation"]}
    assert [f for f in steps if PG.is_long_form(steps[f])] == ["외형", "배경", "복장"]
    assert [s["field"] for s in sd["profile_creation"]] == ["성별", "이름", "나이", "직업", "스킬", "능력치",
                                                             "외모", "외형", "배경", "복장"]      # 순서 불변
    s = session_factory("yd", scenario=sd)
    s.scenario_id = "영도"
    fake_bot.register_session(s)
    fake_bot.user = FakeUser(999)
    ai = AI()
    monkeypatch.setattr(core.profile_ai, "validate", ai.validate)
    monkeypatch.setattr(core.profile_ai, "merge", ai.merge)
    PCU._ACTIVE.clear()
    ch = fake_bot.get_channel(GAME_CH)
    state = PCU.ProfileCreationSession(fake_bot, s, PLAYER_UID, "x", channel_id=ch.id)
    state.run = core.profile_runner.new_run(sd, prefill={
        "성별": "남", "이름": "a", "나이": "30", "직업": next(iter(sd["jobs"])), "스킬": [],
        "능력치": {"근력": 10, "민첩": 10, "기술": 10}, "외모": "5", "외형": "마름"})
    PCU._register(state)
    async with state.lock:
        await PCU.render(state, ch)
    assert state.run["phase"] == PCU.P_MODE                                    # 배경: 방식 선택
    qs = steps["배경"]["args"]["creation_guide"]["questions"]
    r = type("R", (), {"bot": fake_bot, "ch": ch, "sess": s, "ai": ai, "game": game_mod.GameCog(fake_bot)})()
    r.gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    r.gm.bot = fake_bot
    await press(r, "🧭 질문에 답하며 정리")
    for q in qs:
        assert q["prompt"] in _txt(state.message)
        if q["required"]:
            await chat(r, f"{q['id']} 답")
        else:
            await press(r, "건너뛰기")
    assert ai.merge_calls[0]["values"] == [f"{q['prompt']} — {q['id']} 답" for q in qs if q["required"]]
    assert ai.validate_calls and ai.validate_calls[-1][0] == "배경"            # 배경은 조합본을 세계관 검증
    assert state.run["phase"] == PCU.P_DRAFT


# ── DM 사전 생성 회귀 ────────────────────────────────────────

async def test_dm_standalone_creation_uses_chat_without_persistence(fake_bot, monkeypatch):
    from core.profile_ui import _StandaloneSession
    ai = AI()
    monkeypatch.setattr(core.profile_ai, "validate", ai.validate)
    monkeypatch.setattr(core.stats, "has_played", lambda *_a, **_k: False)
    PCU._ACTIVE.clear()
    dm = FakeChannel(777)
    stub = _StandaloneSession("t", copy.deepcopy(SCN))
    st0 = await PCU.start(fake_bot, stub, PLAYER_UID, "(이름 미정)", dm)
    assert st0 is not None and PCU._ACTIVE[(777, PLAYER_UID)] is st0
    item = next(c for c in st0.message.view.children if getattr(c, "label", None) == "남")
    await item.callback(FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=dm, message=st0.message))
    item = next(c for c in st0.message.view.children if getattr(c, "label", None) == "확정")
    await item.callback(FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=dm, message=st0.message))
    m = FakeMessage(channel=dm, content="홍길동")
    m.author = FakeUser(int(PLAYER_UID))
    assert PCU.claims(fake_bot, m)
    await PCU.on_chat(fake_bot, m)
    assert st0.run["pending"] == {"field": "이름", "value": "홍길동"}
    assert not hasattr(stub, PCU.PROGRESS_FIELD)


# ── 답변 메시지 정리 ─────────────────────────────────────────

async def test_recognized_answer_message_is_deleted_and_ignored_one_is_kept(pc):
    r = pc
    await start(r)
    await press(r, "남")
    await press(r, "확정")
    m = await chat(r, "홍길동")
    assert st(r).run["pending"] == {"field": "이름", "value": "홍길동"}
    assert m.deleted                                              # 인식한 답변은 지운다
    other = await chat(r, "아무 말")                              # 텍스트를 기다리지 않는 화면 — 소비하지 않음
    assert not other.deleted


async def test_answer_delete_failure_does_not_block_creation(pc, monkeypatch):
    r = pc
    await start(r)
    await press(r, "남")
    await press(r, "확정")
    m = msg(r, "홍길동")

    async def boom():
        raise RuntimeError("권한 없음(테스트)")
    m.delete = boom
    await PCU.on_chat(r.bot, m)
    assert st(r).run["pending"] == {"field": "이름", "value": "홍길동"}
