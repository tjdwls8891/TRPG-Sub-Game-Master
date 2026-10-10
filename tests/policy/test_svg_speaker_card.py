# -*- coding: utf-8 -*-
"""SVG 화자 카드 — 시나리오 정의·공개 규칙·안전 렌더 입력·전달 경로 통합 (설계안 v1.2 / 구현 지시 v1.0)."""
from __future__ import annotations

import asyncio
import hashlib
import io
import os

import pytest
from PIL import Image

import core
from core import cognition as CG
from core import speaker_card as SC
from core import turn_world as TW
from core.narration_result import NarrationResult
from cogs.game import GameCog
from tests.fakes.genai_fakes import FakeGenAIResponse
from tests.policy.test_h04a_actor_info import KANG
from tests.policy.test_h05a_intent_planning import PC, ctx_of, make_session

pytestmark = pytest.mark.policy

SID = "fx"
AID = "fx.npc.oldman"
OTHER = "fx.npc.uncarded"
REC = {"awareness": "CONTENT", "fragments": [], "belief": None, "acquisitions": [], "belief_provenance": [],
       "secrecy_awareness": None}

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="300" height="200">
<metadata>reference_character: 김노인</metadata><title>김노인 카드</title>
<defs><image id="logo-src" width="10" height="10" xlink:href="data:image/png;base64,U0VDUkVUTE9HTw=="/></defs>
<g id="ref" class="reference-text-paths" data-slot="name_ref"><text>김노인 남항</text></g>
<g id="name-slot" class="variable-text-slot" data-slot="name" style="display:none">
  <text id="n1" textLength="99" lengthAdjust="spacingAndGlyphs">김</text><text id="n2">노</text><text id="n3">인</text>
</g>
<g id="faction-slot" data-slot="faction"><text id="faction-text" fill="#714510">NAMHANG</text></g>
<g id="logo-use" data-slot="logo"><use xlink:href="#logo-src"/></g>
<rect id="wash" data-slot="wash" fill="#714510" opacity="0"/>
<g id="panel" data-slot="panel"><rect width="5" height="5" fill="#714510"/></g>
<g id="portrait" data-slot="portrait"/>
<g id="band" class="fixed" data-slot="band"/>
</svg>"""


def _png(color):
    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), color).save(buf, "PNG")
    return buf.getvalue()


def _write(rel, data):
    path = os.path.join("media", SID, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def _config(template=TEMPLATE):
    files = {"cards/card.svg": template.encode("utf-8"), "cards/unknown.png": _png("gray"),
             "cards/neutral.png": _png("blue"), "cards/alert.png": _png("red"), "cards/logo.png": _png("green")}
    sha = {rel: _write(rel, data) for rel, data in files.items()}
    return {
        "schema_version": 1, "mode": "svg", "template": "cards/card.svg", "sha256": sha,
        "remove_classes": ["reference-text-paths"], "show_classes": ["variable-text-slot"],
        "fixed_slots": ["band"], "default_variation": "neutral", "name_slots": ["name"],
        "text_presets": {"name": {
            "1": {"A": [{"x": 10, "y": 10, "font_size": 50}]},
            "3": {"A": [{"x": 1, "y": 1, "font_size": 31}, {"x": 2, "y": 2, "font_size": 32}, {"x": 3, "y": 3, "font_size": 33}],
                  "B": [{"x": 4, "y": 4, "font_size": 34}, {"x": 5, "y": 5, "font_size": 35}, {"x": 6, "y": 6, "font_size": 36}],
                  "C": [{"x": 7, "y": 7, "font_size": 37}, {"x": 8, "y": 8, "font_size": 38}, {"x": 9, "y": 9, "font_size": 39}]}}},
        "slots": {
            "name": {"kind": "text", "unknown": "?", "bind": [{"op": "chars", "ids": ["n1", "n2", "n3"], "presets": "name"}]},
            "faction": {"kind": "text", "unknown": "?", "bind": [{"op": "text", "id": "faction-text"}]},
            "logo": {"kind": "image", "unknown": "cards/unknown.png", "bind": [{"op": "image", "id": "logo-src"}]},
            "wash": {"kind": "color", "unknown": "#777777",
                     "bind": [{"op": "attr", "id": "wash", "attr": "fill", "static": {"opacity": "0.5"}}]},
            "panel": {"kind": "color", "unknown": "#777777", "bind": [{"op": "recolor", "id": "panel"}]},
            "portrait": {"kind": "image", "unknown": "cards/unknown.png",
                         "bind": [{"op": "image", "id": "portrait", "x": 0, "y": 0, "width": 100, "height": 100}]},
        },
        "actors": {AID: {
            "knowledge": {"face": "i.face", "name": "i.name", "faction": "i.faction"},
            "variations": {"neutral": "cards/neutral.png", "alert": "cards/alert.png"},
            "values": {"name": "김노인", "faction": "NAMHANG", "logo": "cards/logo.png", "color": "#253C48"},
            "rules": [
                {"when": {"all_info": ["i.face"]}, "set": {"portrait": {"variation_image": True}}},
                {"when": {"all_info": ["i.name"]}, "set": {"name": {"value": "name"}}},
                {"when": {"all_info": ["i.faction", "i.name"]},
                 "set": {"faction": {"value": "faction"}, "logo": {"value": "logo"},
                         "wash": {"value": "color"}, "panel": {"value": "color"}}},
            ]}},
    }


DEFINITION = {"information_definitions": {k: {} for k in ("i.face", "i.name", "i.faction")},
              "actor_definitions": {AID: {"display_name": "김노인", "legacy_key": "김노인"},
                                    OTHER: {"display_name": "박씨", "legacy_key": "박씨"}}}


def _world(cfg):
    return SC.CardWorld(cfg, DEFINITION, "pc_x", SID)


def _known(session, *infos, awareness="CONTENT"):
    session.cognition_state = {"actor:pc_x": {i: dict(REC, awareness=awareness) for i in infos}}


@pytest.fixture
def card_session(session_auto_ready, monkeypatch):
    cfg = _config()
    session_auto_ready.actor_registry = {}
    session_auto_ready.irregular_npcs = {"떠돌이": {}}
    monkeypatch.setattr(SC, "card_world", lambda s: _world(cfg))
    monkeypatch.setattr(SC, "rasterize", lambda svg: b"\x89PNG-card:" + hashlib.sha256(svg).digest())
    SC._PNG_CACHE.clear()
    _known(session_auto_ready)
    return session_auto_ready


# ── 정의 · 규칙 평가 ─────────────────────────────────────────

def test_mode_defaults_to_legacy_png():
    assert SC.mode_of({}) == SC.MODE_LEGACY
    assert SC.mode_of({"speaker_card": {"mode": "legacy_png"}}) == SC.MODE_LEGACY
    assert SC.mode_of({"speaker_card": {"mode": "svg"}}) == SC.MODE_SVG


def test_all_info_is_and_and_one_rule_controls_many_slots():
    cfg = _config()
    card = cfg["actors"][AID]
    s = SC.evaluate_slots(cfg, card, {"i.faction"}, "neutral")             # name 없음 → faction 규칙 거짓
    assert s["faction"] == "?" and s["logo"] == "cards/unknown.png" and s["panel"] == "#777777"
    s = SC.evaluate_slots(cfg, card, {"i.faction", "i.name"}, "neutral")
    assert (s["faction"], s["logo"], s["wash"], s["panel"]) == ("NAMHANG", "cards/logo.png", "#253C48", "#253C48")
    assert s["portrait"] == "cards/unknown.png"                            # 얼굴은 별도 InfoId


def test_face_reveal_uses_turn_variation_only_when_face_known():
    cfg = _config()
    card = cfg["actors"][AID]
    assert SC.evaluate_slots(cfg, card, set(), "alert")["portrait"] == "cards/unknown.png"
    assert SC.evaluate_slots(cfg, card, {"i.face"}, "alert")["portrait"] == "cards/alert.png"
    assert SC.choose_variation(cfg, card, "nonexistent") == "neutral"     # 무효 선택 → 기본값
    assert SC.choose_variation(cfg, card, None) == "neutral"


def test_same_slot_in_two_rules_is_rejected_even_with_same_value():
    cfg = _config()
    card = cfg["actors"][AID]
    card["rules"].append({"when": {"all_info": ["i.face"]}, "set": {"name": {"value": "name"}}})
    with pytest.raises(SC.CardError):
        SC.evaluate_slots(cfg, card, {"i.face"}, "neutral")
    codes = {d[1] for d in SC.check_card_definition({"speaker_card": cfg}, definition=DEFINITION, scenario_id=SID)}
    assert "CARD_SLOT_CONFLICT" in codes


# ── 안전 렌더 입력 ─────────────────────────────────────────

def test_unknown_card_svg_contains_no_private_values_or_fixed_sample():
    cfg = _config()
    slots = SC.evaluate_slots(cfg, cfg["actors"][AID], set(), "neutral")
    svg = SC.build_svg(cfg, slots, SC.media_root(SID)).decode("utf-8")
    for secret in ("김", "노", "인", "NAMHANG", "253C48", "U0VDUkVUTE9HTw"):       # 인물 비공개 값·내장 샘플 로고
        assert secret not in svg, secret
    assert "metadata" not in svg and "<title" not in svg and 'id="ref"' not in svg
    assert svg.count("display:inline") == 1                                 # 편집용 텍스트 슬롯 표시
    assert "feColorMatrix" in svg                                          # 래스터 그룹 재색


def test_three_char_presets_use_stable_hash_and_drop_reference_text_length():
    cfg = _config()
    card = cfg["actors"][AID]
    svg = SC.build_svg(cfg, SC.evaluate_slots(cfg, card, {"i.name"}, "neutral"), SC.media_root(SID)).decode()
    pick = "ABC"[sum(ord(c) for c in "김노인") % 3]
    base = {"A": 1, "B": 4, "C": 7}[pick]
    assert f'x="{base}"' in svg and f'font-size="{30 + base}"' in svg
    assert "textLength" not in svg and "lengthAdjust" not in svg
    one = SC.build_svg(cfg, SC.evaluate_slots(cfg, card, set(), "neutral"), SC.media_root(SID)).decode()
    assert 'font-size="50"' in one                                          # "?" = 1글자 프리셋


def test_asset_hash_mismatch_and_path_escape_fail_closed():
    cfg = _config()
    with open(os.path.join("media", SID, "cards", "logo.png"), "ab") as f:
        f.write(b"tampered")
    with pytest.raises(SC.CardAssetError):
        SC.read_verified(cfg, SC.media_root(SID), "cards/logo.png")
    for bad in ("../secret.png", "/etc/passwd", "cards/../../x.png", "cards\\x.png"):
        with pytest.raises(SC.CardAssetError):
            SC.safe_asset_path(SC.media_root(SID), bad)
    cfg["sha256"].pop("cards/neutral.png")
    with pytest.raises(SC.CardAssetError):                                  # 해시 미저작 자산도 거부
        SC.read_verified(cfg, SC.media_root(SID), "cards/neutral.png")


@pytest.mark.parametrize("bad", [
    '<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" onload="x()"/>',
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
    '<image xlink:href="https://evil/x.png"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(x.css);</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(http://x)"/></svg>',
    '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>',
])
def test_unsafe_template_is_rejected(bad):
    with pytest.raises(SC.CardError):
        SC.parse_template(bad.encode())


def test_authoring_check_catches_unbound_slot_and_rule_contract():
    template = TEMPLATE.replace('<g id="band" class="fixed" data-slot="band"/>',
                                '<g id="band" data-slot="band"/><g id="secret" data-slot="latin"><text>KIM</text></g>')
    cfg = _config(template)
    card = cfg["actors"][AID]
    card["rules"][1]["when"]["all_info"] = ["i.name", "i.face"]          # 이름 슬롯은 knowledge.name 하나만
    card["rules"][0]["when"]["all_info"] = ["i.name"]                    # 얼굴 공개는 knowledge.face 조건
    card["knowledge"]["extra"] = "i.unauthored"
    diags = SC.check_card_definition({"speaker_card": cfg}, definition=DEFINITION, scenario_id=SID)
    codes = [d[1] for d in diags]
    assert codes.count("CARD_SLOT_UNBOUND") == 1 and "template#secret" in [d[2] for d in diags]
    assert {"CARD_NAME_RULE", "CARD_FACE_RULE", "CARD_INFO"} <= set(codes)
    assert SC.check_card_definition({"speaker_card": _config()}, definition=DEFINITION, scenario_id=SID) == []
    assert SC.check_card_definition({}, definition=DEFINITION) == []       # 카드 없는 시나리오는 검사 대상 아님


# ── 화자 식별 · 헤더 ───────────────────────────────────────

def test_header_follows_committed_name_knowledge_and_identification(card_session):
    s = card_session
    ctx = SC.display_context(s, "김노인")
    assert (ctx.actor_id, ctx.header, ctx.card is not None) == (AID, "?", True)
    _known(s, "i.name", awareness="EXISTENCE")
    assert SC.display_context(s, "김노인").header == "?"                    # EXISTENCE만으로 공개 안 함
    _known(s, "i.name")
    assert SC.display_context(s, "김노인").header == "김노인"
    unc = SC.display_context(s, "박씨")                                      # 카드 미저작 → 이미지 없음, 이름은 숨기지 않음
    assert (unc.header, unc.card) == ("박씨", None)
    irr = SC.display_context(s, "떠돌이")
    assert (irr.status, irr.header, irr.card) == (SC.SPEAKER_IRREGULAR, "떠돌이", None)
    unk = SC.display_context(s, "누군가")                                    # 식별 실패 → ? · 이미지 없음
    assert (unk.status, unk.header, unk.card) == (SC.SPEAKER_UNKNOWN, "?", None)


def test_ambiguous_speaker_is_not_guessed(card_session, monkeypatch):
    s = card_session
    s.actor_registry = {"a1": {"display_name": "형제", "kind": "NPC"}, "a2": {"display_name": "형제", "kind": "NPC"}}
    ctx = SC.display_context(s, "형제")
    assert (ctx.status, ctx.header, ctx.card) == (SC.SPEAKER_UNKNOWN, "?", None)


def test_legacy_png_scenarios_have_no_display_context(session_auto_ready):
    assert SC.display_context(session_auto_ready, "김노인") is None


# ── 추출 대기 · 바리에이션 ───────────────────────────────────

class _Prep:
    def __init__(self, fut):
        self.fut = fut
        self.card_variations = None

    def task_future(self, name):
        return self.fut if name == "extraction" else None


async def test_variation_waits_extraction_only_at_image_and_falls_back(card_session):
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    prep = _Prep(fut)
    ctx = SC.display_context(card_session, "김노인")
    _known(card_session, "i.face")
    ctx = SC.display_context(card_session, "김노인")
    task = asyncio.ensure_future(SC.turn_variation(prep, AID))
    await asyncio.sleep(0)
    assert not task.done()                                                  # 추출 완료 전에는 대기
    prep.card_variations = {AID: "alert"}
    fut.set_result(None)
    assert await task == "alert"
    failed = _Prep(loop.create_future())
    failed.fut.set_result(None)                                             # 준비 작업 래퍼: 실패도 정상 종료(None)
    assert await SC.turn_variation(failed, AID) is None
    assert SC.choose_variation(ctx.world.config, ctx.card, None) == "neutral"


def test_extraction_manifest_parsing_drops_out_of_list_values():
    manifest = {"V1": (AID, ("alert", "neutral"))}
    assert SC.parse_variations(manifest, {"V1": "alert", "V9": "alert"}) == {AID: "alert"}
    assert SC.parse_variations(manifest, {"V1": "furious"}) == {}
    schema = SC.variation_schema(manifest)
    assert schema["properties"]["V1"]["enum"] == ["alert", "neutral"]
    faces = {"F1": AID}
    assert SC.parse_faces(faces, ["F1", "F2", 3]) == (AID,)
    assert SC.parse_faces(faces, "F1") == ()


# ── 얼굴 식별 → 인지 정본(커밋 경로의 typed 연산) ──────────────

def test_face_identification_joins_observation_acquisitions():
    s, D = make_session(edges=())
    c = ctx_of(s)
    res = TW.process_observation(c, s, None, TW.ObservationManifest(), {}, transaction_id="tx1",
                                 narrative_seed="seed", face_info_ids=("S40",))
    acq = [o for o in res.ops if o.op_type == CG.OP_COGNITION_ACQUIRE]
    assert len(acq) == 1
    p = acq[0].payload
    assert (p["holder"], p["info_id"], p["awareness"], p["acquisition"]["route"]) == \
        (f"actor:{PC}", "S40", "CONTENT", "WITNESS")
    assert p["acquisition"]["acquisition_id"] == "turn:tx1:face:S40"        # 결정적
    assert CG.awareness_of(res.projected_world, f"actor:{PC}", "S40") == "CONTENT"
    assert s.cognition_state.get(f"actor:{PC}") is None                     # 커밋 전 정본 불변
    assert SC.face_info_ids({"actors": {KANG: {"knowledge": {"face": "S40"}}}}, (KANG, "x")) == ("S40",)


def test_card_world_requires_bound_dynamic_single_pc_and_svg_mode():
    s, D = make_session(edges=())
    assert SC.card_world(s) is None                                         # speaker_card 없음 → 기존 PNG
    s.scenario_data = dict(s.scenario_data, speaker_card={"mode": "svg"})
    w = SC.card_world(s)
    assert w is not None and w.pc_actor == PC
    s.world_lifecycle = "CLOSING"
    assert SC.card_world(s) is None
    s.world_lifecycle = "INITIALIZED"                                       # 인트로(genesis 커밋 인지)
    assert SC.card_world(s) is not None
    s.players = dict(s.players, other={"name": "둘째"})
    assert SC.card_world(s) is None                                         # 다인 미지원


# ── 전달 경로 통합 ─────────────────────────────────────────

def _narr(paragraphs):
    return NarrationResult(text="\n\n".join(paragraphs), narrative_text="\n\n".join(paragraphs),
                           code_block_text="", paragraphs=tuple(paragraphs))


async def _deliver(cog, s, narr, game_channel, master_channel, prep=None):
    s.turn_cost_log = []

    async def m_send(content=None, **kw):
        return await master_channel.send(content, **kw)
    return await cog._deliver_narration(s, narr, game_channel=game_channel, master_ch=master_channel,
                                        m_send=m_send, cost_log_prefix="[AUTO] ", transient_ids=[],
                                        preparation=prep)


def _card_msgs(ch):
    return [m for m in ch.sent if m.files is not None and getattr(m.files, "filename", "").startswith("speaker_card_")]


async def test_general_path_streams_text_before_extraction_and_masks_header(
        wired_bot, card_session, game_channel, master_channel, monkeypatch):
    legacy = []
    monkeypatch.setattr(core, "maybe_send_speaker_image", lambda *a, **k: legacy.append(a) or asyncio.sleep(0))
    loop = asyncio.get_running_loop()
    prep = _Prep(loop.create_future())
    cog = GameCog(wired_bot)
    task = asyncio.ensure_future(_deliver(cog, card_session, _narr(["바람이 분다.", "@대사:김노인|누구냐."]),
                                          game_channel, master_channel, prep))
    for _ in range(50):
        await asyncio.sleep(0)
    assert any("바람이 분다" in str(m.content) for m in game_channel.sent)   # 일반 문단은 추출을 기다리지 않는다
    assert not _card_msgs(game_channel)
    prep.card_variations = {AID: "alert"}
    prep.fut.set_result(None)
    res = await task
    cards = _card_msgs(game_channel)
    assert len(cards) == 1 and res.ok
    assert cards[0].id in res.media_message_ids                             # collector(되감기 정리 대상)
    name = cards[0].files.filename
    assert "김" not in name and "노인" not in name
    assert any("## ▍?" in str(m.content) for m in game_channel.sent)
    assert not any("▍김노인" in str(m.content) for m in game_channel.sent)
    assert legacy == []                                                     # 기존 실명 PNG 경로 미사용


async def test_render_failure_omits_image_but_keeps_dialogue(
        wired_bot, card_session, game_channel, master_channel, monkeypatch):
    def boom(svg):
        raise SC.CardError("renderer down")
    monkeypatch.setattr(SC, "rasterize", boom)
    legacy = []
    monkeypatch.setattr(core, "maybe_send_speaker_image", lambda *a, **k: legacy.append(a) or asyncio.sleep(0))
    res = await _deliver(GameCog(wired_bot), card_session, _narr(["@대사:김노인|누구냐."]), game_channel,
                         master_channel)
    assert res.ok and not _card_msgs(game_channel) and legacy == []
    assert any("누구냐" in str(m.content) for m in game_channel.sent)


async def test_hash_mismatch_omits_card(wired_bot, card_session, game_channel, master_channel):
    with open(os.path.join("media", SID, "cards", "card.svg"), "ab") as f:
        f.write(b"<!-- changed -->")
    res = await _deliver(GameCog(wired_bot), card_session, _narr(["@대사:김노인|누구냐."]), game_channel,
                         master_channel)
    assert res.ok and not _card_msgs(game_channel)


async def test_send_failure_omits_image_but_keeps_dialogue(card_session, game_channel):
    async def boom(*a, **k):
        raise RuntimeError("discord down")
    game_channel.send = boom
    assert await SC.send_card(game_channel, card_session, b"\x89PNG") is False


async def test_legacy_png_path_unchanged(wired_bot, session_auto_ready, game_channel, master_channel, monkeypatch):
    legacy = []

    async def fake_image(ch, s, speaker, collector=None):
        legacy.append(speaker)
        return True
    monkeypatch.setattr(core, "maybe_send_speaker_image", fake_image)
    await _deliver(GameCog(wired_bot), session_auto_ready, _narr(["@대사:김노인|안녕."]), game_channel,
                   master_channel)
    assert legacy == ["김노인"]
    assert any("## ▍김노인" in str(m.content) for m in game_channel.sent)
    assert not _card_msgs(game_channel)


async def test_tts_synced_prepares_card_before_voice_enqueue(
        wired_bot, card_session, game_channel, master_channel, monkeypatch):
    order = []

    class Mixer:
        def enqueue_voice(self, src):
            order.append("voice")
    monkeypatch.setattr(core, "get_mixer", lambda vc: Mixer())

    async def synth(bot, text, voice_name=None, cost_context=None):
        return (b"\x00" * 48000, 0.0, 0, 0)
    monkeypatch.setattr(core, "synthesize_tts_pcm", synth)
    real_prepare = SC.prepare_card

    async def prepare(session, ctx, preparation=None):
        order.append("card_prepare")
        return await real_prepare(session, ctx, preparation)
    monkeypatch.setattr(SC, "prepare_card", prepare)
    real_send = SC.send_card

    async def send(channel, session, png, collector=None):
        order.append("card_send")
        return await real_send(channel, session, png, collector)
    monkeypatch.setattr(SC, "send_card", send)
    cog = GameCog(wired_bot)
    await cog._stream_paragraphs_synced(card_session, ["@대사:김노인|누구냐."], game_channel, master_channel,
                                        [], [], [], collector=[], media_collector=[])
    assert order == ["card_prepare", "voice", "card_send"]
    assert any("## ▍?" in str(m.content) for m in game_channel.sent)


async def test_narrate_in_svg_mode_sends_no_image_and_masks_header(
        wired_bot, card_session, game_channel, provider, monkeypatch):
    from cogs.gm import GMCog
    legacy = []
    monkeypatch.setattr(core, "maybe_send_speaker_image", lambda *a, **k: legacy.append(a) or asyncio.sleep(0))
    provider.outcomes.append(FakeGenAIResponse("@대사:김노인|거기 누구야."))
    card_session.cache_name = None
    text = await GMCog(wired_bot)._dispatch_narrate(card_session, "짧게")
    assert text
    assert legacy == [] and not _card_msgs(game_channel)
    assert any("## ▍?" in str(m.content) for m in game_channel.sent)


async def test_intro_without_preparation_uses_default_variation(card_session):
    _known(card_session, "i.face")
    ctx = SC.display_context(card_session, "김노인")
    seen = {}
    real = SC.choose_variation

    def spy(cfg, card, chosen):
        seen["chosen"] = chosen
        return real(cfg, card, chosen)
    import unittest.mock as um
    with um.patch.object(SC, "choose_variation", spy):
        png = await SC.prepare_card(card_session, ctx, None)
    assert png and seen["chosen"] is None                                   # 추출 없음 → 기본 바리에이션


# ── 지시층위·추출층위 런타임 연결(실제 _call_gm_logic / _run_extraction) ──

def _dyn_card_session():
    from core import spatial as SP
    s, D = make_session(edges=())
    s.spatial_state["placements"][KANG] = SP.at_place("항구")              # PC와 같은 장면
    s.scenario_data = dict(s.scenario_data, speaker_card={
        "schema_version": 1, "mode": "svg",
        "actors": {KANG: {"knowledge": {"face": "S40", "name": "S40"},
                          "variations": {"neutral": "a.png", "alert": "b.png"}}}})
    return s


def _gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


async def test_director_identified_faces_are_validated_into_world_turn(fake_bot, provider, master_channel):
    s = _dyn_card_session()
    fake_bot.register_session(s)
    tx = core.turn_transaction.get_or_begin_turn_transaction(s, "얼굴을 본다")
    wt = TW.WorldTurn(ctx=ctx_of(s))
    tx.world_turn = wt
    provider.outcomes.append(FakeGenAIResponse.json_payload(
        {"constraint_check": "통과", "narrate_instruction": "", "proceed_instruction": "묘사",
         "identified_faces": ["F1", "F9"]}))
    decision = await _gm_cog(fake_bot)._call_gm_logic(s, "얼굴을 본다", [], master_channel,
                                                       transaction_id=tx.transaction_id)
    assert decision is not None
    schema = provider.calls[-1].config.response_schema
    assert schema["properties"]["identified_faces"]["items"]["enum"] == ["F1"]
    assert "[얼굴 식별 후보" in provider.calls[-1].contents[0].parts[0].text
    assert wt.identified_faces == (KANG,)                                   # 후보 밖 키(F9)는 버린다
    assert s.cognition_state.get(f"actor:{PC}") is None                     # 지시층위는 정본을 바꾸지 않는다


async def test_extraction_offers_and_fixes_turn_variations(fake_bot, provider, master_channel):
    s = _dyn_card_session()
    fake_bot.register_session(s)
    tx = core.turn_transaction.get_or_begin_turn_transaction(s, "말을 건다")
    prep = core.turn_preparation.ensure_preparation(s, tx.transaction_id)
    prep.world_turn = TW.WorldTurn(ctx=ctx_of(s))
    provider.outcomes.append(FakeGenAIResponse.json_payload(
        {"speaker_variations": {"V1": "alert", "V7": "alert"}, "world_observation": {}}))
    await _gm_cog(fake_bot)._run_extraction(s, "묘사", master_channel, transaction_id=prep.transaction_id,
                                            logical_turn=prep.logical_turn, attempt=prep.attempt,
                                            preparation=prep)
    schema = provider.calls[-1].config.response_schema
    assert schema["properties"]["speaker_variations"]["properties"]["V1"]["enum"] == ["alert", "neutral"]
    assert prep.card_variations == {KANG: "alert"}


def test_a_different_template_and_slot_set_works_without_engine_change():
    tpl = ('<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
           '<image id="face" width="9" height="9" xlink:href="data:image/png;base64,QUJD"/>'
           '<text id="title" data-slot="title">원본</text><circle id="dot" fill="#000000"/></svg>')
    files = {"b/t.svg": tpl.encode(), "b/q.png": _png("black"), "b/f.png": _png("white")}
    sha = {k: _write(k, v) for k, v in files.items()}
    cfg = {"schema_version": 1, "mode": "svg", "template": "b/t.svg", "sha256": sha, "default_variation": "v",
           "slots": {"title": {"kind": "text", "unknown": "???", "bind": [{"op": "text", "id": "title"}]},
                     "face": {"kind": "image", "unknown": "b/q.png", "bind": [{"op": "image", "id": "face"}]},
                     "mark": {"kind": "color", "unknown": "#111111", "bind": [{"op": "attr", "id": "dot", "attr": "fill"}]}},
           "actors": {AID: {"knowledge": {"face": "i.face", "name": "i.name"}, "variations": {"v": "b/f.png"},
                            "values": {"t": "장로", "c": "#ABCDEF"},
                            "rules": [{"when": {"all_info": ["i.name"]}, "set": {"title": {"value": "t"}, "mark": {"value": "c"}}},
                                      {"when": {"all_info": ["i.face"]}, "set": {"face": {"variation_image": True}}}]}}}
    assert SC.check_card_definition({"speaker_card": cfg}, definition=DEFINITION, scenario_id=SID) == []
    hidden = SC.build_svg(cfg, SC.evaluate_slots(cfg, cfg["actors"][AID], set(), "v"), SC.media_root(SID)).decode()
    shown = SC.build_svg(cfg, SC.evaluate_slots(cfg, cfg["actors"][AID], {"i.name", "i.face"}, "v"),
                         SC.media_root(SID)).decode()
    assert "???" in hidden and "장로" not in hidden and "#111111" in hidden and "QUJD" not in hidden
    assert "장로" in shown and "#ABCDEF" in shown
