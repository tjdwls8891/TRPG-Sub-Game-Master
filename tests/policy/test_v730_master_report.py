"""v7.30 마스터 턴 전수보고 · 비용 예측 개편.

사용자 결정: 매 턴 보고 · 비밀은 요약 + 버튼으로 원문 · 조건부 호출은 관측 발생률 ≥ 기준일 때만 상한(3.b) · 범위 p10~p90.
"""
from __future__ import annotations

import json
import types

import discord
import pytest

import core
from core import estimate as ES
from core import master_report as MR
from core import prompt_census as PC
from tests.fakes.discord_fakes import FakeChannel, FakeInteraction, FakeUser
from tests.policy.test_h05bc_turn_wiring import drig, judgment, turn  # noqa: F401
from tests.policy.test_ready_barrier import rig  # noqa: F401

pytestmark = pytest.mark.policy


# ── 입력 구성 계측 ────────────────────────────────────────────

def _content(text):
    return types.SimpleNamespace(parts=[types.SimpleNamespace(text=text)])


def test_census_classifies_blocks_and_names_people():
    s = types.SimpleNamespace(scenario_data={"default_npcs": {"진복례": {}, "이선희": {}}},
                              players={"1": {"name": "강지수"}}, npcs={}, irregular_npcs={}, cost_stats={})
    prompt = ("머리말\n[현재 장소: 물양장]\n진복례가 그물을 끈다.\n[최근 로그]\n강지수가 말했다.\n"
              "[플레이어 선언]\n이선희에게 묻는다")
    rows = PC.record(s, "instruction", [_content("옛 대화 진복례"), _content(prompt)],
                     system_text="시스템", in_tokens=1100, cached_tokens=100)
    by = {r["block"]: r for r in rows}
    assert by["시스템 지시"]["cls"] == PC.FIXED and by["현재 장소"]["cls"] == PC.FIXED
    assert by["최근 로그"]["cls"] == PC.LIKELY and by["대화 이력 1건"]["cls"] == PC.LIKELY
    assert by["플레이어 선언"]["cls"] == PC.VARIABLE and by["플레이어 선언"]["keys"] == ["이선희"]
    assert by["현재 장소"]["keys"] == ["진복례"]
    assert sum(r["tok"] for r in rows) == pytest.approx(1000, abs=len(rows))      # 실측 비캐시 입력을 나눈 값
    assert s.cost_stats["_census"]["instruction"][-1] == PC.totals(rows)
    assert any(ln.startswith("[가변] 플레이어 선언(이선희)") for ln in PC.render(rows))


# ── 비용 예측 ────────────────────────────────────────────────

def _est_session():
    s = core.TRPGSession("v730-est", 1, 2, "x", {})
    s.scenario_id = "없음"
    s.cost_stats = {}
    return s


def test_input_range_uses_census_fixed_plus_likely_variable_p10_p90():
    s = _est_session()
    assert ES.census_range(s, "narration") is None                          # 표본 부족 — 기존 방식
    s.cost_stats["_census"] = {"narration": [{PC.FIXED: 1000 + i, PC.LIKELY: 500 + 100 * i, PC.VARIABLE: 50 * i}
                                             for i in range(10)]}
    lo, mid, hi = ES.census_range(s, "narration")
    assert lo == 1009 + int(ES._pct([500 + 100 * i for i in range(10)], 0.1)) + int(ES._pct([50 * i for i in range(10)], 0.1))
    assert lo < mid < hi and hi == 1009 + int(ES._pct([500 + 100 * i for i in range(10)], 0.9)) \
        + int(ES._pct([50 * i for i in range(10)], 0.9))
    est = core.estimate_turn(s, "PROCEED")
    assert est["input_ranges"]["narration"] == {"lo": lo, "mid": mid, "hi": hi, "basis": "입력 분류"}
    assert est["input_ranges"]["judgment"]["basis"] == "조립·원장"
    assert est["min_krw"] <= est["mid_krw"] <= est["max_krw"]


def test_session_output_range_is_p10_p90_of_samples():
    s = _est_session()
    for v in range(100, 1100, 100):
        ES.update_stats(s, "narration", v)
    assert ES._out_range(s, "narration", "narration_out") == (int(ES._pct(list(range(100, 1100, 100)), 0.1)),
                                                             int(ES._pct(list(range(100, 1100, 100)), 0.9)))


def test_calibration_is_per_layer():
    s = _est_session()
    for layer, actual in (("instruction", 2000), ("narration", 500)):
        for _ in range(3):
            s._last_input_estimate = {layer: 1000}
            ES.record_actual_input(s, layer, actual)
    assert ES.get_calibration(s, "instruction") == pytest.approx(2.0)
    assert ES.get_calibration(s, "narration") == pytest.approx(0.5)


def test_accuracy_rows_feed_back_into_range():
    s = _est_session()
    est = {"min_krw": 8.0, "mid_krw": 10.0, "max_krw": 14.0, "breakdown": {"묘사층위": (5.0, 9.0)}}
    assert ES.feedback_factor(s) == 1.0
    for _ in range(ES.FEEDBACK_MIN - 1):
        ES.record_accuracy(s, est, 12.0, {"묘사층위": 7.0, "부가 호출(조건부)": 5.0}, transaction_id="t")
    assert ES.feedback_factor(s) == 1.0                                       # 표본 부족
    row = ES.record_accuracy(s, est, 12.0, {"묘사층위": 7.0}, transaction_id="t")
    assert row["in_range"] and row["scope"] == "없음|LEGACY" and row["layers"]["묘사층위"]["actual"] == 7.0
    assert ES.feedback_factor(s) == pytest.approx(1.2)
    base = core.estimate_turn(s, "PROCEED")
    assert base["feedback"] == pytest.approx(1.2)
    for _ in range(ES.FEEDBACK_WINDOW):
        ES.record_accuracy(s, est, 100.0, {}, transaction_id="t")
    assert ES.feedback_factor(s) == 1.3                                       # 상한 고정


def test_actual_by_line_maps_log_to_estimate_lines_and_skips_tts():
    log = [{"label": "판단층위", "layer": "judgment", "cost": 1.0}, {"label": "추출층위", "layer": "extraction", "cost": 2.0},
           {"label": "추출층위", "layer": "extraction", "cost": 2.5}, {"label": "선언 검증", "cost": 3.0},
           {"label": "묘사층위(NARRATE)", "cost": 4.0}, {"label": "TTS 더빙(1/1문단)", "cost": 9.0, "model": core.TTS_MODEL}]
    assert ES.actual_by_line(log) == {"판단층위": 1.0, "추출층위": 2.0, "추출 재시도(조건부)": 2.5,
                                      "선언 검증(조건부)": 3.0, "묘사층위": 4.0}


# ── 분리 포장 ────────────────────────────────────────────────

def test_pack_splits_within_discord_limits_without_dropping_text():
    lines = [f"줄{i} " + "가" * 300 for i in range(60)] + ["나" * 2500]
    embeds = MR.pack("제목", MR.COLOR_DELTA, [("섹션", lines)], description="설명")
    assert len(embeds) > 1 and embeds[0].title == f"제목 (1/{len(embeds)})"
    for e in embeds:
        assert len(e.fields) <= MR.FIELDS_MAX and len(e) <= MR.EMBED_MAX
        assert all(len(f.value) <= MR.FIELD_MAX for f in e.fields)
    joined = "".join(f.value.replace("\n", "") for e in embeds for f in e.fields)
    assert joined == "".join(lines)                                            # 잘라 버린 글자 없음


async def test_send_groups_embeds_by_message_total():
    ch = FakeChannel(5)
    s = types.SimpleNamespace(session_id="s", turn_count=3, definition_revision=None)
    big = MR.pack("x", 1, [("a", ["가" * 1000] * 30)])
    orig = MR.build
    MR.build = lambda *a, **k: (big, 0)
    try:
        n = await MR.send(ch, s, transaction_id=None, record={})
    finally:
        MR.build = orig
    assert n == len(ch.sent) and n > 1
    assert all(sum(len(e) for e in m.embeds) <= MR.EMBED_MAX and len(m.embeds) <= 10 for m in ch.sent)


# ── 세계 델타 · 비밀 ──────────────────────────────────────────

LONG = "남항 지하 창고의 두 번째 열쇠는 차봉순이 아니라 엄주섭이 갖고 있으며, " * 3


def _record():
    pre = {"world_clock": {"minute_index": 100}, "spatial_state": {"scene_place": "항구", "placements": {
               "pc_1": {"kind": "AT_PLACE", "place_id": "항구"}, "npc.a": {"kind": "AT_PLACE", "place_id": "시장"},
               "npc.b": {"kind": "AT_PLACE", "place_id": "창고"}}},
           "actor_registry": {"pc_1": {"kind": "PC", "display_name": "강지수"}},
           "event_instances": {}, "cognition_state": {}, "information_state": {}, "resources": {"강지수": {"밧줄": 1}}}
    post = json.loads(json.dumps(pre))
    post["world_clock"]["minute_index"] = 130
    post["spatial_state"]["placements"]["npc.a"] = {"kind": "AT_PLACE", "place_id": "항구"}
    post["spatial_state"]["placements"]["npc.b"] = {"kind": "AT_PLACE", "place_id": "남항"}
    post["event_instances"] = {"evt_1": {"template_id": "yd_x", "lifecycle": "ACTIVE", "current_phase": "p1"}}
    post["information_state"] = {"runtime_definitions": {"rt.1": {"info_id": "rt.1", "kind": "RUNTIME",
                                                                  "proposition": LONG, "summary": None}}}
    post["cognition_state"] = {"actor:npc.b": {"rt.1": {"awareness": "CONTENT"}}}
    post["resources"] = {"강지수": {"밧줄": 0, "열쇠": 1}}
    return {"pre": pre, "post": post}


def test_delta_covers_off_scene_and_shows_secret_summary_only():
    s = types.SimpleNamespace(session_id="s", definition_revision=None)
    secs, n_secret = MR.delta_sections(s, _record())
    text = {name: "\n".join(lines) for name, lines in secs}
    assert "+30분" in text["🕰️ 시간"]
    assert "npc.a: 시장 → 항구" in text["🧍 인물"] and "[원격] npc.b: 창고 → 남항" in text["🧍 인물"]
    assert "yd_x: 생성" in text["📜 사건"]
    assert "밧줄 −1 (→0)" in text["🎒 물건"] and "열쇠 +1 (→1)" in text["🎒 물건"]   # v7.35 형식
    secret = text["🔒 비밀·정보 (요약)"]
    assert n_secret == 2 and "npc.b · 새 인지 CONTENT" in secret and LONG not in secret and "…" in secret
    full = "\n".join(MR.secret_lines(s, _record()))
    assert LONG in full                                                        # 원문은 버튼에서만


async def test_secret_button_roundtrip_and_master_channel_only(monkeypatch):
    item = MR.SecretButton("sid1", "abc123")
    m = MR.SecretButton.__discord_ui_compiled_template__.fullmatch(item.item.custom_id)
    assert m and (await MR.SecretButton.from_custom_id(None, None, m)).tx == "abc123"
    monkeypatch.setattr(core.turn_history, "load_record", lambda sid, tx: _record())
    sess = types.SimpleNamespace(session_id="sid1", master_ch_id=5, game_ch_id=6, definition_revision=None)
    other = types.SimpleNamespace(session_id="sid2", master_ch_id=7, game_ch_id=8, definition_revision=None)
    bot = types.SimpleNamespace(active_sessions={5: sess, 6: sess, 7: other, 8: other})   # 운영 형태: 채널 id 키
    wrong = FakeInteraction(user=FakeUser(1), channel=FakeChannel(6))
    wrong.client = bot
    await item.callback(wrong)
    assert "마스터 채널" in wrong.response.messages[0]["content"]
    ok = FakeInteraction(user=FakeUser(1), channel=FakeChannel(5))
    ok.client = bot
    await item.callback(ok)
    assert ok.response.messages[0]["ephemeral"] and "남항 지하 창고" in ok.response.messages[0]["content"]


def test_secret_button_registered_as_dynamic_item():
    src = (core.__file__.rsplit("/", 2)[0] + "/cogs/gm.py")
    assert "add_dynamic_items(core.master_report.SecretButton)" in open(src, encoding="utf-8").read()


# ── 실제 턴 경로 ──────────────────────────────────────────────

async def test_committed_turn_sends_three_colored_reports(drig):
    r = drig
    r.make()
    r.prov.routes["judgment"] = judgment("시장")
    await turn(r, "시장으로 간다")
    master = r.bot.get_channel(r.sess.master_ch_id)
    colors = {e.color.value for m in master.sent for e in (m.embeds or []) if e.color}
    assert {MR.COLOR_COST, MR.COLOR_ACTIVITY, MR.COLOR_DELTA} <= colors
    titles = [e.title for m in master.sent for e in (m.embeds or [])]
    assert any(t.startswith("🌐 세계 델타") for t in titles) and any(t.startswith("🧠 층위활동") for t in titles)
    delta = [f for m in master.sent for e in m.embeds if e.color and e.color.value == MR.COLOR_DELTA for f in e.fields]
    assert any(f.name == "🕰️ 시간" for f in delta)                            # 이동으로 정본 시계가 흘렀다
    cost = [f.name for m in master.sent for e in m.embeds if e.color and e.color.value == MR.COLOR_COST for f in e.fields]
    assert any(n.startswith("📐 입력 구성") for n in cost)
