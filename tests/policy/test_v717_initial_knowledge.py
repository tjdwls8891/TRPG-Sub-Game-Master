# -*- coding: utf-8 -*-
"""v7.17 초기 지식 — 인수 T01~T22(원본/초기지식/통합설계안 §12) + 교차 X01~X16(03_교차_인수표).

provider·Discord는 대역(mock)이다. 실제 Gemini 출력 품질·실제 Discord 전송은 이 파일이 검증하지 않는다.
"""
from __future__ import annotations

import asyncio
import copy
import json
import pathlib
from types import SimpleNamespace

import discord
import pytest

import core
import prompts
from core import cognition as COG
from core import cost_ledger as CL
from core import initial_knowledge as IK
from core import items as IT
from core import world_lifecycle as WL
from core import world_state as WS
from tests.conftest import PLAYER_UID
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeUser
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h06a_lifecycle import START, dyn_raw, journal_inits, setup_session, _cog
from tests.policy.test_ready_barrier import _usage, rig  # noqa: F401

pytestmark = pytest.mark.policy
ROOT = pathlib.Path(__file__).resolve().parents[2]
PC = f"pc_{PLAYER_UID}"
SECRET_NPC = "창고 열쇠를 성당 제단 밑에 숨겼다"          # info_raw의 NPC 인라인 비밀
SECRET_S40 = "원본 기록은 성당 제3보관실에 있다"           # 미표기 정보 명제

INFOS = {
    "ik.common": {"proposition": "항구에는 밤마다 짙은 안개가 낀다", "summary": "항구에는 밤마다 짙은 안개가 낀다.",
                  "initial_knowledge": {"tier": "required", "phases": ["PROFILE"]}},
    "ik.guild": {"proposition": "길드 장부는 시장 창고에 있다", "summary": "상인 길드의 장부는 시장 창고에서 관리된다.",
                 "initial_knowledge": {"tier": "optional", "phases": ["PROFILE"],
                                       "access": {"field": "소속", "op": "equals", "value": "상인 길드"}}},
    "ik.rumor": {"proposition": "성당에 기록이 있다는 소문이 돈다", "summary": "성당 지하에 옛 기록이 있다는 소문",
                 "initial_knowledge": {"tier": "optional", "phases": ["PROFILE"], "kind": "reported_claim"}},
    "ik.dock": {"proposition": "부두 경비는 자정에 교대한다", "summary": "부두 경비는 자정에 교대한다.",
                "subject_refs": ["place:항구"],
                "initial_knowledge": {"tier": "required", "phases": ["START"], "start_frame_ids": ["harbor"]}},
    "ik.any_start": {"proposition": "시장은 새벽에 연다", "summary": "시장은 새벽에 연다.",
                     "initial_knowledge": {"tier": "optional", "phases": ["START"]}},
    "clue.hidden": {"proposition": "숨은 단서 원문", "summary": "숨은 단서"},
}


def ik_raw(infos=None, **extra):
    raw = dyn_raw(**extra)
    raw["start_frames"].append({"id": "dock", "title": "부두의 새벽", "summary": "부두에서 눈을 뜬다", "facts": {},
                                "world_start": copy.deepcopy(START)})
    raw["start_frames"][0]["summary"] = "항구에 막 도착했다"
    raw["pc_template"] = {"이름": "", "직업": "", "소속": "", "문파": ""}
    raw["information_definitions"].update(copy.deepcopy(INFOS if infos is None else infos))
    return raw


def setup(r, raw=None, profile=None):
    s = setup_session(r, raw or ik_raw())
    s.players[PLAYER_UID]["profile"] = dict(profile if profile is not None else
                                            {"직업": "상인", "소속": "상인 길드"})
    s.players[PLAYER_UID].pop("fields", None)
    return s


def defn(s):
    d, why = WL.compile_candidate(s)
    assert d is not None, why
    return d


class Ch(FakeChannel):
    def __init__(self, cid=9001):
        super().__init__(cid)
        self.fail_send = None
        self.history_fail = False

    async def send(self, content=None, **kw):
        if self.fail_send is not None:
            raise self.fail_send
        return await super().send(content, **kw)

    def history(self, limit=100):
        ch = self

        async def gen():
            if ch.history_fail:
                raise RuntimeError("history forbidden")
            for m in reversed(ch.sent[-limit:]):
                yield m
        return gen()


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")


@pytest.fixture
def ai(monkeypatch):
    """AI 대역 — 호출 횟수·입력 기록. resp(optional_keys 등)를 바꿔 쓴다."""
    st = SimpleNamespace(calls=[], resp={"optional_keys": [], "recontextualized_keys": [],
                                         "master_briefing": "당신의 경력과 이어지는 것들입니다."}, ok=True)

    async def fake(bot, session, prompt):
        st.calls.append(prompt)
        await asyncio.sleep(0)
        return st.ok, copy.deepcopy(st.resp)
    monkeypatch.setattr(IK, "_call_ai", fake)
    return st


async def deliver(r, s, ch, phase, frame=None):
    return await IK.run_phase(r.bot, s, PLAYER_UID, phase, ch, defn(s), frame=frame)


def pc_cog(s):
    return (s.cognition_state or {}).get(COG.actor_holder(PC)) or {}


# ══════════════════════════════════════════════════════════════
# T01~T04 · T09~T12 후보·입력·출력
# ══════════════════════════════════════════════════════════════

def test_T01_X02_unmarked_info_never_candidate():
    raw = ik_raw()
    raw["item_definitions"] = {"편지": {"info_refs": ["clue.hidden"]}}      # 읽기 대상 정보(초기 표시 없음)
    d = core.definition_compiler.compile_scenario(raw, "t").definition
    prof = {"직업": "상인", "소속": "상인 길드"}
    ids = [r["id"] for ph in IK.PHASES for rows in IK.candidates(d, ph, prof, frame_id="harbor") for r in rows]
    assert "clue.hidden" not in ids and "S40" not in ids and not any(i.startswith("info_") for i in ids)
    assert set(ids) == {"ik.common", "ik.guild", "ik.rumor", "ik.dock", "ik.any_start"}


async def test_T02_T10_required_kept_optional_only_valid_keys(rig, ai):
    r = rig
    s = setup(r)
    ch = Ch()
    ai.resp = {"optional_keys": ["K9", "K1", "K1", "S40", 7], "recontextualized_keys": ["R1"],
               "master_briefing": "설명"}
    assert await deliver(r, s, ch, "PROFILE") == IK.DELIVERED
    rec = s.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]
    assert [x["id"] for x in rec["selected_info"]] == ["ik.common", "ik.guild"]       # 필수 + K1(=ik.guild)
    codes = sorted(d["code"] for d in rec["diagnostics"])
    assert codes.count("IK_KEY_UNKNOWN") == 4 and "IK_KEY_DUPLICATE" in codes        # K9·S40·7·R1(PROFILE)
    ai.resp = {"optional_keys": [], "recontextualized_keys": [], "master_briefing": ""}
    s2 = setup(r)
    s2.initial_knowledge_progress = {}
    assert await deliver(r, s2, Ch(9002), "PROFILE") == IK.DELIVERED
    assert [x["id"] for x in s2.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]["selected_info"]] == [
        "ik.common"]                                                                    # AI가 아무것도 안 골라도 필수


def test_T03_access_gate_strict_and_compile_checks_field():
    d = core.definition_compiler.compile_scenario(ik_raw(), "t").definition
    assert IK.eligible(d, "ik.guild", "PROFILE", {"소속": "상인 길드"})
    assert not IK.eligible(d, "ik.guild", "PROFILE", {"소속": "도적 길드"})
    assert not IK.eligible(d, "ik.guild", "PROFILE", {})                              # 필드 없음 = 불허
    assert not IK.eligible(d, "ik.guild", "PROFILE", {"소속": "상인 길드 (AI가 그렇다고 함)"})
    bad = ik_raw({"ik.x": {"proposition": "p", "summary": "s", "initial_knowledge": {
        "tier": "required", "access": {"field": "경력", "op": "equals", "value": "군인"}}}})
    assert "IK_ACCESS_FIELD_UNKNOWN" in [x.code for x in core.definition_compiler.compile_scenario(bad, "t").errors]
    nosum = ik_raw({"ik.y": {"proposition": "p", "initial_knowledge": {"tier": "required"}}})
    assert "IK_SUMMARY_REQUIRED" in [x.code for x in core.definition_compiler.compile_scenario(nosum, "t").errors]


async def test_T04_T09_X04_X11_prompt_has_approved_persona_and_no_secrets(rig, ai):
    r = rig
    s = setup(r)
    s.profile_creation_progress = {PLAYER_UID: {"run": {"values": {"배경": "초안비밀문장"}}}}
    s.resources = {"테스터": {"편지": 1}, "박노인": {"금괴": 3}}
    await deliver(r, s, Ch(), "PROFILE")
    await deliver(r, s, Ch(), "START", {"id": "harbor", "title": "항구의 밤", "summary": "항구에 막 도착했다"})
    joined = "\n".join(ai.calls)
    assert "상인 길드" in joined and "· 이름: 테스터" in joined
    for bad in ("초안비밀문장", SECRET_NPC, SECRET_S40, "숨은 단서", "금괴", "기본 자원", "{장소}", "길드 장부는 시장 창고에",
                "부두 경비는 자정에 교대한다\n"):
        assert bad not in joined, bad
    assert "부두 경비는 자정에 교대한다." in joined                                         # summary만(명제 원문 아님)


async def test_T11_T12_summary_rendered_verbatim_rumor_as_hearsay(rig, ai):
    r = rig
    s = setup(r)
    ch = Ch()
    ai.resp = {"optional_keys": ["K2"], "recontextualized_keys": [], "master_briefing": "안개 이야기는 잊어도 됩니다."}
    await deliver(r, s, ch, "PROFILE")
    text = ch.sent[-1].embeds[0].description
    assert "· 항구에는 밤마다 짙은 안개가 낀다." in text                                   # AI 문장이 덮어도 원문 유지
    assert "· 전해 들은 말 — 성당 지하에 옛 기록이 있다는 소문" in text
    assert text.index("안개 이야기는") < text.index("**알고 있는 것**")


# ══════════════════════════════════════════════════════════════
# T05~T08 시점·중복·0정보
# ══════════════════════════════════════════════════════════════

async def test_T05_profile_phase_before_flow_advances(rig, ai, monkeypatch):
    r = rig
    s = setup(r)
    order = []
    monkeypatch.setattr(core.session_flow.creation, "record", lambda *a, **k: order.append("record"))
    monkeypatch.setattr(core.session_flow.creation, "advance", lambda *a, **k: order.append("advance"))

    async def _render(*a, **k):
        order.append("render")
    monkeypatch.setattr(core.session_flow, "render", _render)
    ch = Ch()
    ch.fail_send = http_error()
    await core.session_flow.on_profile_done(r.bot, s, ch)
    assert order == [] and IK.state_of(s, PLAYER_UID, "PROFILE") == IK.FAILED_RETRYABLE   # 실패 — 진행 안 함
    ch.fail_send = None
    await core.session_flow.on_profile_done(r.bot, s, ch)
    assert order == ["record", "advance", "render"] and len(ai.calls) == 1               # 재시도는 같은 패킷
    assert IK.state_of(s, PLAYER_UID, "PROFILE") == IK.DELIVERED


async def test_T06_T07_start_phase_before_genesis_frame_specific_no_redelivery(rig, ai, monkeypatch):
    r = rig
    s = setup(r)
    ch = Ch()
    await deliver(r, s, ch, "PROFILE")
    cog = _cog(r)

    async def _up(*a, **k):
        return True
    monkeypatch.setattr(cog, "upload_cache", _up)
    seen = {}
    orig = WL.initialize_world

    async def spy(bot, session, chosen):
        seen["start"] = IK.state_of(session, PLAYER_UID, "START")
        return await orig(bot, session, chosen)
    monkeypatch.setattr(core.world_lifecycle, "initialize_world", spy)
    ai.resp = {"optional_keys": [], "recontextualized_keys": ["R1"], "master_briefing": "기억나십니까"}
    assert await cog.open_dynamic_world(s, {"id": "harbor", "title": "항구의 밤", "summary": "항구에 막 도착했다"}, ch)
    assert seen["start"] == IK.DELIVERED and WL.world_bound(s)
    st = s.initial_knowledge_progress["players"][PLAYER_UID]["START"]
    assert [x["id"] for x in st["selected_info"]] == ["ik.dock"]                         # 1차 항목 재부여 없음
    assert st["recontextualized"] == ["ik.common"] and "R1: 항구에는" in ai.calls[-1]
    d = core.definition_compiler.compile_scenario(ik_raw(), "t").definition
    assert [x["id"] for x in IK.candidates(d, "START", {}, frame_id="dock")[0]] == []    # 틀별 후보 분리


async def test_T08_X16_zero_candidates_zero_calls_and_legacy_untouched(rig, ai):
    r = rig
    s = setup(r, ik_raw(infos={}))
    assert await deliver(r, s, Ch(), "PROFILE") == IK.SKIPPED and ai.calls == []
    legacy = setup_session(r, {"name": "x"})                     # 정의·capability 없는 LEGACY 시나리오
    before = copy.deepcopy(legacy.initial_knowledge_progress)
    ch = Ch(9003)
    assert await core.session_flow.initial_knowledge_gate(r.bot, legacy, ch, "PROFILE")
    assert await core.session_flow.initial_knowledge_gate(r.bot, legacy, ch, "START", frame={"id": "harbor"})
    assert ai.calls == [] and legacy.initial_knowledge_progress == before and ch.sent == []


# ══════════════════════════════════════════════════════════════
# T13~T17 genesis·멱등·전송 증명·연타·revision
# ══════════════════════════════════════════════════════════════

async def test_T13_T14_X06_genesis_single_commit_idempotent(rig, ai):
    r = rig
    s = setup(r)
    s.resources = {"테스터": {"항생제 두 알": 1, "붕대": 1}}
    ai.resp = {"optional_keys": ["K2"], "recontextualized_keys": [], "master_briefing": "x"}
    ch = Ch()
    await deliver(r, s, ch, "PROFILE")
    await deliver(r, s, ch, "START", {"id": "harbor"})
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_OK, res.reason
    cog = pc_cog(s)
    assert set(cog) >= {"ik.common", "ik.rumor", "ik.dock"} and "ik.guild" not in cog
    acq = {i: cog[i]["acquisitions"][0] for i in ("ik.common", "ik.rumor", "ik.dock")}
    assert acq["ik.common"]["acquisition_id"] == f"initial:{s.session_id}:{PLAYER_UID}:PROFILE:ik.common"
    assert acq["ik.common"]["route"] == "SYSTEM_GRANTED" and acq["ik.rumor"]["route"] == "RUMOR"
    assert all(cog[i]["belief"] is None for i in acq)                                  # T12 믿음 불변
    assert len(journal_inits(s)) == 1                                                    # 같은 INITIALIZATION 한 번
    assert s.resources["테스터"] == {"항생제[알]": 2, "붕대": 1}                          # 시작 재고 1회 정규화
    data = json.loads(pathlib.Path(f"sessions/{s.session_id}/data.json").read_text(encoding="utf-8"))
    assert data["initial_knowledge_progress"]["players"][PLAYER_UID]["PROFILE"]["state"] == IK.COMMITTED
    assert data["cognition_state"] == json.loads(json.dumps(s.cognition_state))         # 같은 strict 저장
    before = (copy.deepcopy(s.cognition_state), copy.deepcopy(s.resources))
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_ALREADY
    assert (s.cognition_state, s.resources) == before and len(journal_inits(s)) == 1


async def test_T15_save_send_receipt_boundaries(rig, ai, monkeypatch):
    r = rig
    # ① PREPARED 저장 실패 → 보내지 않음
    s = setup(r)
    ch = Ch()
    real = IK._strict_save
    monkeypatch.setattr(IK, "_strict_save", lambda bot, sess: _false())
    assert await deliver(r, s, ch, "PROFILE") == IK.FAILED_RETRYABLE and ch.sent == []
    monkeypatch.setattr(IK, "_strict_save", real)
    assert await deliver(r, s, ch, "PROFILE") == IK.DELIVERED and len(ai.calls) == 1      # 같은 패킷 — AI 1회
    # ② 발송 실패(확실) → 미확정 아님·재전송 가능
    s = setup(r)
    s.initial_knowledge_progress = {}
    ch = Ch(9010)
    ch.fail_send = http_error()
    assert await deliver(r, s, ch, "PROFILE") == IK.FAILED_RETRYABLE and ch.sent == []
    # ③ 발송 성공 + 기록 실패 → UNCERTAIN, genesis 금지, 영수증 복구로만 DELIVERED
    ch.fail_send = None
    calls = {"n": 0}

    async def flaky(bot, sess):
        calls["n"] += 1
        return calls["n"] != 2                         # SENDING 저장 OK · DELIVERED 저장 실패
    monkeypatch.setattr(IK, "_strict_save", flaky)
    assert await deliver(r, s, ch, "PROFILE") == IK.UNCERTAIN and len(ch.sent) == 1
    monkeypatch.setattr(IK, "_strict_save", real)
    res = await WL.initialize_world(r.bot, s, {"id": "harbor"})
    assert res.outcome == WL.INIT_FAILED and "uncertain" in res.reason and not pc_cog(s)
    assert not s.item_instances and s.definition_revision is None                       # 부분 초기화 없음(X07)
    ch.history_fail = True
    assert await deliver(r, s, ch, "PROFILE") == IK.UNCERTAIN and len(ch.sent) == 1     # 조회 불가 → 재전송 없음
    ch.history_fail = False
    assert await deliver(r, s, ch, "PROFILE") == IK.DELIVERED and len(ch.sent) == 1
    rec = s.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]
    assert rec["reason"] == "RECEIPT_RECOVERED" and rec["message_ids"] == [str(ch.sent[0].id)]


async def _false():
    return False


async def test_T15_uncertain_owner_skip_commits_nothing(rig, ai, monkeypatch):
    r = rig
    s = setup(r)
    ch = Ch()
    ch.history_fail = True
    rec = IK.record(s, PLAYER_UID, "PROFILE")
    rec.update(state=IK.SENDING, pages=["x"], delivery_nonce="ik-zzz", selected_info=[
        {"id": "ik.common", "summary_snapshot": "항구에는 밤마다 짙은 안개가 낀다.", "kind": "fact"}])
    assert await deliver(r, s, ch, "PROFILE") == IK.UNCERTAIN
    assert await IK.skip_uncertain(r.bot, s, PLAYER_UID, "PROFILE") == IK.SKIPPED
    await deliver(r, s, ch, "START", {"id": "harbor"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert "ik.common" not in pc_cog(s) and "ik.dock" in pc_cog(s)


async def test_T16_double_click_one_call_one_send(rig, ai, monkeypatch):
    r = rig
    s = setup(r)
    ch = Ch()
    a, b = await asyncio.gather(deliver(r, s, ch, "PROFILE"), deliver(r, s, ch, "PROFILE"))
    assert a == b == IK.DELIVERED and len(ai.calls) == 1 and len(ch.sent) == 1
    import cogs.session as SM
    played = []
    cog = _cog(r)

    async def _play(*a, **k):
        played.append(1)
        await asyncio.sleep(0)
    monkeypatch.setattr(cog, "play_intro", _play)
    r.bot.owner_ids = {int(PLAYER_UID)}
    view = SM.StartFrameView(r.bot, s, [{"id": "harbor", "title": "항구의 밤"}])
    btn = view.children[0]
    msg = (await ch.send("x"))
    i1 = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=ch, message=msg)
    i2 = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=ch, message=msg)
    await asyncio.gather(btn.callback(i1), btn.callback(i2))
    assert played == [1]


async def test_T17_X08_revision_or_profile_change_not_auto_granted(rig, ai):
    r = rig
    s = setup(r)
    ai.resp = {"optional_keys": ["K1"], "recontextualized_keys": [], "master_briefing": "x"}
    await deliver(r, s, Ch(), "PROFILE")                       # ik.common + ik.guild 전달
    s.players[PLAYER_UID]["profile"]["소속"] = "도적 길드"     # 승인 뒤 프로필 변경 → 접근 재평가
    s.scenario_data["information_definitions"]["ik.common"]["summary"] = "안개는 없다."   # 정의 변경
    await deliver(r, s, Ch(), "START", {"id": "harbor"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert "ik.common" not in pc_cog(s) and "ik.guild" not in pc_cog(s)
    ex = s.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]["excluded"]
    assert sorted(x["id"] for x in ex) == ["ik.common", "ik.guild"]


async def test_T17_stale_unsent_packet_is_rebuilt(rig, ai, monkeypatch):
    r = rig
    s = setup(r)
    monkeypatch.setattr(IK, "send", lambda *a, **k: _ret(IK.FAILED_RETRYABLE))
    await deliver(r, s, Ch(), "PROFILE")
    monkeypatch.undo()
    s.players[PLAYER_UID]["profile"]["소속"] = "도적 길드"
    ai_calls = len(ai.calls)
    st = SimpleNamespace(calls=ai.calls)
    monkeypatch.setattr(IK, "_call_ai", lambda *a: _ret((True, {"optional_keys": [], "recontextualized_keys": [],
                                                                "master_briefing": "y"}), st))
    await deliver(r, s, Ch(), "PROFILE")
    rec = s.initial_knowledge_progress["players"][PLAYER_UID]["PROFILE"]
    assert "ik.guild" not in [x["id"] for x in rec["selected_info"]] and len(st.calls) == ai_calls + 1


async def _ret(v, st=None):
    if st is not None:
        st.calls.append("call")
    return v


# ══════════════════════════════════════════════════════════════
# T18~T20 재열람·인지 소비·비용
# ══════════════════════════════════════════════════════════════

async def test_T18_X10_review_from_snapshot_private_no_calls(rig, ai):
    r = rig
    s = setup(r)
    await deliver(r, s, Ch(), "PROFILE")
    n = len(ai.calls)
    text = IK.review_text(s, PLAYER_UID)
    assert "항구에는 밤마다 짙은 안개가 낀다." in text and IK.review_text(s, "999") is None
    s.scenario_data["information_definitions"]["ik.common"]["summary"] = "바뀐 문장"
    assert IK.review_text(s, PLAYER_UID) == text and len(ai.calls) == n               # 전달 당시 원문
    view = core.DisplayView(r.bot)
    btn = next(c for c in view.children if getattr(c, "custom_id", None) == "disp:knowledge")
    r.bot.active_sessions[4242] = s
    it = FakeInteraction(user=FakeUser(int(PLAYER_UID)), channel=FakeChannel(4242))
    await btn.callback(it)
    (m,) = it.response.messages
    assert m["ephemeral"] is True and "항구에는" in m["embeds"][0].description
    it2 = FakeInteraction(user=FakeUser(999), channel=FakeChannel(4242))
    await btn.callback(it2)
    assert "항구에는" not in it2.response.messages[0]["embeds"][0].description
    assert len(ai.calls) == n and not pc_cog(s)


async def test_T19_X12_knowledge_block_relevance_not_lexicographic(rig, ai):
    r = rig
    fill = {f"z.info.{i:02d}": {"proposition": f"잡지식 {i}", "summary": f"잡지식 {i}"} for i in range(14)}
    fill["y.place"] = {"proposition": "항구 창고", "summary": "항구 창고의 뒷문은 잠기지 않는다", "subject_refs": ["place:항구"]}
    s = setup(r, ik_raw({**INFOS, **fill}))
    await deliver(r, s, Ch(), "PROFILE")
    await deliver(r, s, Ch(), "START", {"id": "dock"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert WL.mark_open(s)
    holder = COG.actor_holder(PC)
    for i, iid in enumerate(sorted(fill)):
        rec = COG.empty_record() if hasattr(COG, "empty_record") else {
            "awareness": None, "fragments": [], "belief": None, "acquisitions": [], "belief_provenance": [],
            "secrecy_awareness": None}
        rec = dict(rec, awareness=COG.CONTENT, acquisitions=[COG.acquisition_record(
            f"t:{iid}", "TESTIMONY", at_minute=(0 if iid == "y.place" else 10_000_000 + i))])
        s.cognition_state[holder][iid] = rec
    ctx = core.player_world.session_world_context(s)
    blk = core.turn_world.pc_knowledge_block(ctx, None, s)
    lines = [x for x in blk.splitlines() if x.startswith("· ")]
    assert len(lines) == core.turn_world.PC_KNOWLEDGE_BUDGET
    assert lines[0].startswith("· 항구 창고의 뒷문")                                      # 장면 장소 관련 우선
    assert "· 항구에는 밤마다 짙은 안개가 낀다." in lines                                 # 필수 초기 지식(사전순 첫째)
    assert sum(1 for iid in fill if iid in pc_cog(s)) == 15                              # 정본 인지는 전부 유지


async def test_T20_X13_operator_cost_separate_no_ink(rig, monkeypatch):
    r = rig
    s = setup(r)
    resp = FakeGenAIResponse(json.dumps({"optional_keys": [], "recontextualized_keys": [],
                                         "master_briefing": "b"}, ensure_ascii=False), usage=_usage())

    class P:
        n = 0

        def generate_content(self, **kw):
            P.n += 1
            return resp
    r.bot.genai_client.models._provider = P()
    bal = core.accounts.get_balance(int(PLAYER_UID))
    pai = float(getattr(s, "profile_ai_cost_krw", 0.0) or 0.0)
    await deliver(r, s, Ch(), "PROFILE")
    evs = [e for e in r.bot.cost_ledger.list_cost_events_strict(session_id=s.session_id)
           if e["operation"] == CL.OP_INITIAL_KNOWLEDGE]
    assert P.n == 1 and len(evs) == 1 and evs[0]["billing_hint"] == CL.HINT_OPERATOR
    assert evs[0]["input_tokens"] > 0                                                    # 실제 provider 사용량 기록
    assert core.accounts.get_balance(int(PLAYER_UID)) == bal
    assert float(getattr(s, "profile_ai_cost_krw", 0.0) or 0.0) == pai                   # 프로필 AI 집계와 분리


def test_T20_provider_callsite_count_unchanged():
    src = (ROOT / "core/initial_knowledge.py").read_text(encoding="utf-8")
    assert "generate_content" not in src and "profile_ai._call(" in src


# ══════════════════════════════════════════════════════════════
# T21 장르 중립 · T22 영도 콘텐츠
# ══════════════════════════════════════════════════════════════

GENRE = {
    "fantasy.guild": {"proposition": "마도사 길드는 북문 탑에 있다", "summary": "마도사 길드의 본부는 북문 탑이다.",
                      "initial_knowledge": {"tier": "required", "phases": ["PROFILE"],
                                            "access": {"field": "소속", "op": "equals", "value": "마도사 길드"}}},
    "wuxia.sect": {"proposition": "화산파는 매화검을 쓴다", "summary": "화산파의 독문 검법은 매화검이다.",
                   "initial_knowledge": {"tier": "optional", "phases": ["PROFILE"],
                                         "access": {"any": [{"field": "문파", "op": "one_of", "values": ["화산파", "종남파"]},
                                                            {"field": "직업", "op": "equals", "value": "검객"}]}}},
    "mystery.rumor": {"proposition": "집사가 범인이라는 소문", "summary": "집사가 범인이라는 소문이 돈다",
                      "initial_knowledge": {"tier": "optional", "phases": ["PROFILE"], "kind": "reported_claim"}},
    "mystery.truth": {"proposition": "범인은 정원사다", "summary": "범인의 정체"},
}


async def test_T21_X15_genre_fixtures_same_contract(rig, ai):
    r = rig
    d = core.definition_compiler.compile_scenario(ik_raw(GENRE), "t").definition
    c = lambda prof: sorted(x["id"] for rows in IK.candidates(d, "PROFILE", prof) for x in rows)   # noqa: E731
    assert c({"소속": "마도사 길드"}) == ["fantasy.guild", "mystery.rumor"]
    assert c({"문파": "종남파"}) == ["mystery.rumor", "wuxia.sect"]
    assert c({"직업": "검객"}) == ["mystery.rumor", "wuxia.sect"]
    assert "mystery.truth" not in c({"소속": "마도사 길드", "문파": "화산파"})               # 비공개 단서 제외
    s = setup(r, ik_raw(GENRE), profile={"문파": "화산파"})
    ai.resp = {"optional_keys": ["K1", "K2"], "recontextualized_keys": [], "master_briefing": "x"}
    await deliver(r, s, Ch(), "PROFILE")
    await deliver(r, s, Ch(), "START", {"id": "harbor"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    cog = pc_cog(s)
    assert cog["mystery.rumor"]["acquisitions"][0]["route"] == "RUMOR" and "mystery.truth" not in cog
    src = (ROOT / "core/initial_knowledge.py").read_text(encoding="utf-8")
    assert "영도" not in src and "감염" not in src                                        # 영도 하드코딩 없음


def test_T22_X16_yeongdo_only_two_sourced_required_infos():
    raw = json.loads((ROOT / "scenarios/영도.json").read_text(encoding="utf-8"))
    res = core.definition_compiler.compile_scenario(raw, "영도")
    assert res.ok
    infos = res.definition["information_definitions"]
    opted = sorted(i for i, rec in infos.items() if rec.get("initial_knowledge"))
    assert opted == ["yd.info.ik.bridges_cut", "yd.info.ik.infected_nature"]
    assert not any(i.startswith(("yd.info.q.", "yd.info.card.")) for i in opted)        # 사건 단서·카드 무단 공개 없음
    intro = raw["scenario_intro"]
    for phrase in ("죽어 움직이는 시체가 아닙니다", "살아있는 인간", "영도대교, 부산대교", "다리를 끊어 영도를 봉쇄했다는 소식을"):
        assert phrase in intro                                                           # 출처 문구가 정본에 있다
    for i in opted:
        assert infos[i]["initial_knowledge"]["tier"] == "required" and infos[i]["initial_knowledge"]["phases"] == [
            "PROFILE"]
    assert "yd.info.q.join_namhang_trade" in infos                                        # 기존 정보 불변


# ══════════════════════════════════════════════════════════════
# 교차 X01·X03·X05·X09·X14
# ══════════════════════════════════════════════════════════════

def test_X01_items_and_initial_knowledge_survive_one_compile():
    raw = ik_raw()
    raw["item_definitions"] = {"편지": {"info_refs": ["ik.common"], "unique": True,
                                       "instances": [{"instance_id": "letter.a", "place": "항구"}]}}
    raw["information_definitions"]["ik.common"]["subject_refs"] = ["item:편지"]
    res = core.definition_compiler.compile_scenario(raw, "t")
    assert res.ok, [(x.code, x.message) for x in res.errors]
    d = res.definition
    assert d["item_definitions"]["편지"]["instances"][0]["instance_id"] == "letter.a"
    assert d["information_definitions"]["ik.common"]["initial_knowledge"]["tier"] == "required"
    assert d["information_definitions"]["ik.common"]["subject_refs"] == ["item:편지"]


async def test_X03_knowing_is_not_owning(rig, ai):
    r = rig
    raw = ik_raw()
    raw["item_definitions"] = {"편지": {"info_refs": ["ik.common"], "unique": True,
                                       "instances": [{"instance_id": "letter.a", "place": "항구"}]}}
    s = setup(r, raw)
    await deliver(r, s, Ch(), "PROFILE")
    await deliver(r, s, Ch(), "START", {"id": "harbor"})
    assert (await WL.initialize_world(r.bot, s, {"id": "harbor"})).outcome == WL.INIT_OK
    assert "ik.common" in pc_cog(s)
    assert s.item_instances["letter.a"]["owner"] is None and s.item_instances["letter.a"]["place"] == "항구"
    assert "편지" not in json.dumps(s.resources, ensure_ascii=False)


async def test_X05_X09_read_document_and_drop_same_turn_atomic_and_rewindable(rig):
    from tests.policy.test_h05bc_turn_wiring import H05Provider, extraction, judgment, make_dynamic, turn
    from tests.policy.test_ready_barrier import _json
    r = rig
    prov = H05Provider()
    prov.routes.update({
        "narration": [FakeGenAIResponse("PC가 편지를 읽고 돌을 내려놓는다.", usage=_usage())],
        "logic": [_json({"proceed_instruction": "진행", "event_assessment": "ongoing"})],
        "simulation": [_json({})], "irregular": [_json({"assignments": []})], "judgment": judgment()})
    r.bot.genai_client.models._provider = prov
    r.prov = prov

    async def _nosim(*a, **k):
        return None
    r.gm._simulate_narrative_directions = _nosim
    make_dynamic(r.sess, raw_patch=lambda raw: raw.update(item_definitions={"편지": {"info_refs": ["S40"]}}))
    r.sess.resources = {"테스터": {"편지": 1, "돌": 2}}
    prov.routes["extraction"] = [extraction({})]
    await turn(r, "숨을 고른다")
    snap = {f: copy.deepcopy(getattr(r.sess, f)) for f in ("resources", "place_items", "cognition_state")}
    prov.routes["extraction"] = [extraction({"item_ops": [
        {"op": "READ", "owner": "O1", "item": "H2", "quantity": 1},
        {"op": "DROP", "owner": "O1", "item": "H1", "quantity": 1}]})]
    tx = await turn(r, "편지를 읽고 돌 하나를 내려놓는다")
    assert tx.status == core.turn_transaction.TurnStatus.COMMITTED
    rec = pc_cog(r.sess)["S40"]
    assert rec["awareness"] == COG.CONTENT and rec["belief"] is None
    assert rec["acquisitions"][0]["route"] == "DOCUMENT"
    assert r.sess.resources["테스터"] == {"편지": 1, "돌": 1} and len(r.sess.place_items) == 1
    res = await core.turn_history.rewind(r.bot, r.sess, 1)
    assert res["ok"], res
    assert {f: getattr(r.sess, f) for f in snap} == snap                                  # 아이템·현장·인지 함께 복원


def test_X14_fields_registered_and_ik_not_reversible():
    assert IK.FIELD in core.SESSION_FIELDS and IK.FIELD not in core.turn_history.REVERSIBLE_FIELDS
    for f in IT.ITEM_FIELDS:
        assert f in core.SESSION_FIELDS and f in core.turn_history.REVERSIBLE_FIELDS
    assert "INITIAL_KNOWLEDGE_SYSTEM_INSTRUCTION" in dir(prompts)
    assert set(prompts.INITIAL_KNOWLEDGE_RESPONSE_SCHEMA["required"]) == {
        "optional_keys", "recontextualized_keys", "master_briefing"}
