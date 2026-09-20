"""WP-A — Narration Boundary & Output Ownership 계약 테스트 (T-A01..A17).

목적
    · `_generate_narration`(생성)이 단독 호출 시 canonical 턴 확정/청구/로그 부작용을
      갖지 않음을 고정한다.
    · `_deliver_narration`(전달)이 생성한 bot-authored 메시지 ID를 보고하고, 부분 전달
      실패에서도 이미 생성된 ID를 잃지 않음을 고정한다.
    · 자동/인트로/수동 경로가 공유 헬퍼 재사용만으로 자동 트랜잭션/청구를 상속하지 않음을 고정한다.
    · 이미 검증된 provider CostEvent 관측이 1회로 보존됨을 고정한다.

실제 provider SDK/네트워크를 부르지 않으며(ScriptedProvider), CostLedger는 tmp_path로 격리한다.
"""

from __future__ import annotations

import pytest

import core
from core import cost_ledger as cl
from core.narration_result import (
    NarrationResult, DeliveryResult, NarrationDeliveryError,
)
from cogs.game import GameCog
from tests.fakes.genai_fakes import FakeGenAIResponse, FakeUsageMetadata

pytestmark = pytest.mark.policy


# ── 공통 헬퍼 ──────────────────────────────────────────────────

def _usage():
    return FakeUsageMetadata(prompt=1000, candidates=200, thoughts=0, cached=500)


def _mk(session, bot, master_ch):
    """generate/deliver 헬퍼가 받는 m_send 클로저."""
    async def m_send(content=None, **kw):
        if master_ch:
            return await master_ch.send(content, **kw)
        return None
    return m_send


def _cached(session):
    """cached 분기 강제 — narration 생성이 캐시 재발급 경로를 타지 않게 한다."""
    session.cache_obj = object()
    session.cache_name = "caches/fake"
    session.cache_model = core.DEFAULT_MODEL
    session.turn_cost_log = []
    session.tts_enabled = False
    return session


def _snapshot_canonical(session):
    return {
        "turn_count": session.turn_count,
        "raw_logs_len": len(session.raw_logs),
        "uncompressed_len": len(session.uncompressed_logs),
        "current_turn_logs": list(session.current_turn_logs),
        "total_ink_spent": int(getattr(session, "total_ink_spent", 0) or 0),
        "resources": {k: dict(v) for k, v in (session.resources or {}).items()},
        "statuses": {k: list(v) for k, v in (session.statuses or {}).items()},
    }


# ══════════════════════════════════════════════════════════════
# 생성 순수성 (T-A01/02/03)
# ══════════════════════════════════════════════════════════════

async def test_ta01_generation_has_no_canonical_finalization_side_effect(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A01 — 생성 단독은 canonical 로그/카운터/상태를 바꾸지 않는다."""
    _cached(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("첫 문단입니다.\n\n둘째 문단입니다.", usage=_usage())]
    cog = GameCog(wired_bot)
    before = _snapshot_canonical(session_auto_ready)

    narr = await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert isinstance(narr, NarrationResult)
    after = _snapshot_canonical(session_auto_ready)
    assert after["turn_count"] == before["turn_count"], "생성이 turn_count를 올렸다"
    assert after["raw_logs_len"] == before["raw_logs_len"], "생성이 raw_logs를 append했다"
    assert after["uncompressed_len"] == before["uncompressed_len"], "생성이 uncompressed_logs를 append했다"
    assert after["current_turn_logs"] == before["current_turn_logs"], "생성이 current_turn_logs를 비웠다"
    assert after["resources"] == before["resources"], "생성이 자원을 변이했다"
    assert after["statuses"] == before["statuses"], "생성이 상태를 변이했다"


async def test_ta02_generation_does_not_bill_player(
        wired_bot, session_auto_ready, game_channel, master_channel, tmp_path):
    """T-A02 — 생성 단독은 플레이어 잉크를 차감하지 않는다(account/Settlement/InkTransaction 없음)."""
    _cached(session_auto_ready)
    wired_bot.cost_ledger = cl.CostLedger(str(tmp_path / "cl.jsonl"))
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("묘사.", usage=_usage())]
    cog = GameCog(wired_bot)
    before_ink = int(getattr(session_auto_ready, "total_ink_spent", 0) or 0)

    await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert int(getattr(session_auto_ready, "total_ink_spent", 0) or 0) == before_ink
    # Settlement/InkTransaction ledger는 생성 경로에서 생기지 않는다.
    events = wired_bot.cost_ledger.list_cost_events()
    assert all(e.get("operation") != "SETTLEMENT" for e in events)


async def test_ta03_generation_writes_no_canonical_logs(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A03 — 생성 단독은 canonical raw/uncompressed 로그를 진행시키지 않는다."""
    _cached(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("묘사 문단.", usage=_usage())]
    cog = GameCog(wired_bot)
    r0, u0 = len(session_auto_ready.raw_logs), len(session_auto_ready.uncompressed_logs)

    await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert len(session_auto_ready.raw_logs) == r0
    assert len(session_auto_ready.uncompressed_logs) == u0


# ══════════════════════════════════════════════════════════════
# 생성 보존 (T-A04/05/06)
# ══════════════════════════════════════════════════════════════

async def test_ta04_finalized_text_preserved(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A04 — 결정적 provider 출력 → 검증/스트립 후 최종 텍스트가 보존된다."""
    _cached(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("동굴이 어둡다.\n\n바람이 분다.", usage=_usage())]
    cog = GameCog(wired_bot)

    narr = await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert narr.text == "동굴이 어둡다.\n\n바람이 분다."
    assert narr.paragraphs == ("동굴이 어둡다.", "바람이 분다.")
    assert narr.code_block_text == ""


async def test_ta04b_response_tag_strip_preserved(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A04b — 응답에 남은 자:/태: 에코 태그는 최종 텍스트에서 제거된다(기존 방어 스트립)."""
    _cached(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("서사 본문. 자:테스터;물통;-1 태:테스터;부상", usage=_usage())]
    cog = GameCog(wired_bot)

    narr = await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert "자:" not in narr.text and "태:" not in narr.text
    assert "서사 본문." in narr.text


async def test_ta05_pc_autonomy_filter_preserved(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A05 — PC 이름으로 된 대사 마커는 최종 텍스트/문단에서 제거된다(PC 자율성 보호)."""
    _cached(session_auto_ready)
    # 테스터=PC(session.players), 김노인=NPC 로 등록
    session_auto_ready.npcs = {"김노인": {"name": "김노인"}}
    wired_bot.genai_client.models._provider.outcomes = [FakeGenAIResponse(
        "묘사 문단.\n\n@대사:테스터|나는 간다\n\n@대사:김노인|어서 오게", usage=_usage())]
    cog = GameCog(wired_bot)

    narr = await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    assert "나는 간다" not in narr.text, "PC(테스터) 임의 대사가 남았다"
    assert "어서 오게" in narr.text, "NPC(김노인) 대사가 잘못 제거됐다"
    assert all("테스터" not in p or "@대사:테스터" not in p for p in narr.paragraphs)


async def test_ta06_media_directives_preserved(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A06 — 이미지/미디어 지시가 NarrationResult에 구조화되어 실린다."""
    _cached(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("문단.", usage=_usage())]
    cog = GameCog(wired_bot)

    narr = await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=["폭포"], mid_imgs=["동굴"], bottom_imgs=["석상"])

    assert narr.top_images == ("폭포",)
    assert narr.mid_images == ("동굴",)
    assert narr.bottom_images == ("석상",)


# ══════════════════════════════════════════════════════════════
# provider CostEvent 관측 보존 (T-A15)
# ══════════════════════════════════════════════════════════════

async def test_ta15_narration_costevent_count_unchanged(
        wired_bot, session_auto_ready, game_channel, master_channel, tmp_path):
    """T-A15 — 단일 narration provider 발생 → CostEvent 정확히 1건(0도 2도 아님)."""
    _cached(session_auto_ready)
    wired_bot.cost_ledger = cl.CostLedger(str(tmp_path / "cl.jsonl"))
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("묘사.", usage=_usage())]
    cog = GameCog(wired_bot)

    await cog._generate_narration(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        master_ch=master_channel, game_channel=game_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        top_imgs=[], mid_imgs=[], bottom_imgs=[])

    narr_events = [e for e in wired_bot.cost_ledger.list_cost_events()
                   if e.get("operation") == "TURN_NARRATION"]
    assert len(narr_events) == 1, f"narration CostEvent가 {len(narr_events)}건"


# ══════════════════════════════════════════════════════════════
# 전달 출력 소유권 (T-A10/11/12)
# ══════════════════════════════════════════════════════════════

def _narr(paragraphs, code_block=""):
    return NarrationResult(
        text="\n\n".join(paragraphs) + (("\n\n" + code_block) if code_block else ""),
        narrative_text="\n\n".join(paragraphs),
        code_block_text=code_block,
        paragraphs=tuple(paragraphs))


async def test_ta10_delivery_reports_all_narration_ids(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A10 — 다중 문단 전달 시 생성한 모든 묘사 메시지 ID가 순서대로 보고된다."""
    session_auto_ready.turn_cost_log = []
    cog = GameCog(wired_bot)
    narr = _narr(["문단 하나.", "문단 둘.", "문단 셋."])

    delivery = await cog._deliver_narration(
        session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        cost_log_prefix="[AUTO] ", transient_ids=[])

    assert isinstance(delivery, DeliveryResult) and delivery.ok
    # 문단당 1개의 스트리밍 메시지가 게임 채널에 생성된다.
    streamed = [m.id for m in game_channel.sent if m.content and "✍️" not in str(m.content)]
    assert len(delivery.canonical_message_ids) == 3
    # 보고된 ID가 실제 게임 채널 메시지 집합의 부분집합이다.
    all_ids = {m.id for m in game_channel.sent}
    assert set(delivery.canonical_message_ids) <= all_ids


async def test_ta11_partial_delivery_preserves_created_ids(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A11 — send #1/#2 성공, #3 실패 시 이미 생성된 ID가 예외에 실려 보존되고 좁은 정리가 가능하다."""
    session_auto_ready.turn_cost_log = []
    cog = GameCog(wired_bot)
    narr = _narr(["문단1", "문단2", "문단3"])

    # 세 번째 channel.send 에서 예외를 던지게 한다.
    orig_send = game_channel.send
    state = {"n": 0}

    async def failing_send(content=None, **kw):
        state["n"] += 1
        if state["n"] == 3:
            raise RuntimeError("send #3 boom")
        return await orig_send(content, **kw)
    game_channel.send = failing_send

    with pytest.raises(NarrationDeliveryError) as ei:
        await cog._deliver_narration(
            session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
            m_send=_mk(session_auto_ready, wired_bot, master_channel),
            cost_log_prefix="[AUTO] ", transient_ids=[])

    err = ei.value
    # 이미 생성된 두 메시지 ID가 보존된다.
    assert len(err.canonical_message_ids) == 2, err.canonical_message_ids
    # caller가 좁은 정리를 수행할 수 있다(ID → 메시지 → 삭제).
    game_channel.send = orig_send
    owned = [m for m in game_channel.sent if m.id in err.canonical_message_ids]
    assert len(owned) == 2
    await core.clear_messages(owned)
    assert all(m.deleted for m in owned)


async def test_ta12_cleanup_is_idempotent(wired_bot, game_channel):
    """T-A12 — 이미 삭제됐거나 없는 메시지에 정리를 두 번 호출해도 안전하다."""
    msg = await game_channel.send("정리 대상")
    msg.raise_on_second_delete = True

    await core.clear_messages([msg])
    await core.clear_messages([msg])   # 두 번째 — 예외를 던지지 않아야 한다
    await core.clear_messages([None])  # None 안전
    await core.clear_messages([])      # 빈 목록 안전
    assert msg.delete_count == 2


# ══════════════════════════════════════════════════════════════
# 셸 통합 — 자동/인트로/수동 경계 (T-A07/08/09/13/14) · 압축 트리거(T-A17)
# ══════════════════════════════════════════════════════════════

async def test_ta07_automatic_path_delivers_and_attaches_ownership(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A07 — 자동 경로: 최종 묘사가 전달되고, 출력 ID가 현재 attempt에 귀속된다.

    또한 셸이 새 TurnTransaction을 만들지 않음을 확인한다(directive F).
    """
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None  # _execute_proceed의 master_ch.guild 접근(권한 토글)용
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("자동 묘사 문단.", usage=_usage())]
    cog = GameCog(wired_bot)

    tx = core.turn_transaction.begin_turn_transaction(session_auto_ready, "플레이어 선언")
    before_active = core.turn_transaction.get_active_transaction(session_auto_ready)

    result = await cog._execute_proceed(
        session_auto_ready, "지시", cost_log_prefix="[AUTO] ",
        transaction_id=tx.transaction_id)

    assert result["ok"] is True
    assert "자동 묘사 문단." in result["ai_text"]
    # 자동 caller 성공 신호(raw_logs 증가)와 turn_count 진행이 유지된다.
    assert any(getattr(c, "role", None) == "model" for c in session_auto_ready.raw_logs[-2:])
    # 출력 소유권: 전달 메시지 ID가 현재 attempt에 귀속됐다.
    assert len(tx.canonical_message_ids) >= 1
    # 셸/헬퍼는 새 트랜잭션을 만들지 않았다(동일 객체 유지).
    assert core.turn_transaction.get_active_transaction(session_auto_ready) is before_active is tx


async def test_ta08_intro_reuse_creates_no_automatic_transaction(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A08/A13 — 인트로/수동 재사용(cost_log_prefix 없음, transaction_id 없음)은
    자동 TurnTransaction을 만들지 않고 플레이어에게 청구하지 않는다."""
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None  # _execute_proceed의 master_ch.guild 접근(권한 토글)용
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("인트로 묘사.", usage=_usage())]
    cog = GameCog(wired_bot)
    before_ink = int(getattr(session_auto_ready, "total_ink_spent", 0) or 0)

    result = await cog._execute_proceed(session_auto_ready, "인트로 지시")

    assert result["ok"] is True
    # 공유 헬퍼 재사용만으로 자동 트랜잭션이 생기지 않는다.
    assert core.turn_transaction.get_active_transaction(session_auto_ready) is None
    # 정상 턴 청구가 발생하지 않는다.
    assert int(getattr(session_auto_ready, "total_ink_spent", 0) or 0) == before_ink


async def test_ta09_manual_path_does_not_acquire_transaction_authority(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """T-A09 — 수동 경로도 자동 트랜잭션 권한을 취득하지 않는다(A08과 동일 규칙 재확인)."""
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None  # _execute_proceed의 master_ch.guild 접근(권한 토글)용
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("수동 묘사.", usage=_usage())]
    cog = GameCog(wired_bot)

    result = await cog._execute_proceed(session_auto_ready, "수동 지시")
    assert result["ok"] is True
    assert core.turn_transaction.get_active_transaction(session_auto_ready) is None


async def test_ta17_compression_trigger_preserved(
        wired_bot, session_auto_ready, game_channel, master_channel, monkeypatch):
    """T-A17 — 자동 압축 트리거 의미가 셸에 보존된다(should_compress + 로그 존재 시 스케줄)."""
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None  # _execute_proceed의 master_ch.guild 접근(권한 토글)용
    session_auto_ready.uncompressed_logs = ["기존 로그"]
    session_auto_ready.is_compressing = False
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("묘사.", usage=_usage())]
    cog = GameCog(wired_bot)

    monkeypatch.setattr(core.memory_plan, "should_compress", lambda s: True)
    scheduled = {"called": False}

    async def _fake_compress(session, logs, prefix=""):
        scheduled["called"] = True
    monkeypatch.setattr(cog, "_run_auto_compression", _fake_compress)

    await cog._execute_proceed(session_auto_ready, "지시", cost_log_prefix="[AUTO] ")
    # create_task로 스케줄된 압축이 실행될 기회를 준다.
    import asyncio
    await asyncio.sleep(0)
    assert scheduled["called"], "압축 트리거가 사라졌다"


async def test_legacy_tag_mutation_preserved_not_blocked(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """directive B / P-A15 — 셸의 레거시 자:/태: 직접 변이는 WP-A에서 보존된다(차단은 WP-B).

    일부만 제거하면 mutation timing이 의도치 않게 바뀌므로, WP-A는 정확히 기존대로 1회 적용한다.
    """
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None
    session_auto_ready.resources = {}
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("묘사.", usage=_usage())]
    cog = GameCog(wired_bot)

    await cog._execute_proceed(session_auto_ready, "자:테스터;물;+5 진행하라")

    # 레거시 직접 변이가 그대로 적용된다(제거/차단되지 않음, 이중 적용도 아님).
    assert session_auto_ready.resources.get("테스터", {}).get("물") == 5


async def test_delivery_does_not_advance_canonical_state(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """§26 — 전달 헬퍼는 canonical 상태(카운터·로그·자원·청구)를 소유하지 않는다."""
    session_auto_ready.turn_cost_log = []
    cog = GameCog(wired_bot)
    narr = _narr(["문단 A.", "문단 B."])
    before = _snapshot_canonical(session_auto_ready)

    await cog._deliver_narration(
        session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        cost_log_prefix="[AUTO] ", transient_ids=[])

    after = _snapshot_canonical(session_auto_ready)
    assert after["turn_count"] == before["turn_count"]
    assert after["raw_logs_len"] == before["raw_logs_len"]
    assert after["uncompressed_len"] == before["uncompressed_len"]
    assert after["total_ink_spent"] == before["total_ink_spent"]
    assert after["resources"] == before["resources"]
    assert after["statuses"] == before["statuses"]


# ══════════════════════════════════════════════════════════════
# 미디어 출력 소유권 (GPT 게이트 패치 — 이미지/미디어도 회수 가능해야 함)
# ══════════════════════════════════════════════════════════════

import os


def _install_media(session, keyword="폭포", filename="pic.png"):
    """게임 채널 이미지가 실제로 생성되도록 미디어 키워드+파일을 준비한다(격리 cwd)."""
    md = session.scenario_data.setdefault("media_keywords", {})
    md[keyword] = filename
    d = f"media/{session.scenario_id}"
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, filename), "wb") as f:
        f.write(b"\x89PNG\r\n")   # 존재만 하면 된다
    return keyword


def _narr_img(paragraphs, top=(), mid=(), bottom=()):
    return NarrationResult(
        text="\n\n".join(paragraphs),
        narrative_text="\n\n".join(paragraphs),
        code_block_text="",
        paragraphs=tuple(paragraphs),
        top_images=tuple(top), mid_images=tuple(mid), bottom_images=tuple(bottom))


async def test_m1_media_ids_preserved_in_delivery_result(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """M1 — 이미지 지시가 실제 메시지를 만들면 그 ID가 DeliveryResult.media_message_ids에 실린다."""
    session_auto_ready.turn_cost_log = []
    kw = _install_media(session_auto_ready)
    cog = GameCog(wired_bot)
    narr = _narr_img(["문단 하나."], top=[kw])

    delivery = await cog._deliver_narration(
        session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        cost_log_prefix="[AUTO] ", transient_ids=[])

    assert delivery.ok
    assert len(delivery.media_message_ids) == 1, delivery.media_message_ids
    # 보고된 미디어 ID가 실제 게임 채널에 전송된 파일 메시지다.
    file_msgs = {m.id for m in game_channel.sent if m.files}
    assert set(delivery.media_message_ids) <= file_msgs


async def test_m2_automatic_media_output_attached_to_attempt(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """M2 — 자동 경로: 생성된 이미지/텍스트 출력이 현재 attempt에 귀속되고 새 tx는 안 생긴다."""
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None
    kw = _install_media(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("자동 묘사 문단.", usage=_usage())]
    cog = GameCog(wired_bot)

    tx = core.turn_transaction.begin_turn_transaction(session_auto_ready, "선언")
    result = await cog._execute_proceed(
        session_auto_ready, f"상:{kw} 진행하라", cost_log_prefix="[AUTO] ",
        transaction_id=tx.transaction_id)

    assert result["ok"] is True
    assert len(tx.canonical_message_ids) >= 1, "텍스트 출력이 귀속되지 않았다"
    assert len(tx.media_message_ids) >= 1, "이미지 출력이 귀속되지 않았다"
    assert core.turn_transaction.get_active_transaction(session_auto_ready) is tx


async def test_m3_intro_manual_media_creates_no_transaction(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """M3 — intro/manual(transaction_id=None): 이미지가 생겨도 자동 TurnTransaction을 만들지 않는다."""
    _cached(session_auto_ready)
    session_auto_ready.is_started = True
    master_channel.guild = None
    kw = _install_media(session_auto_ready)
    wired_bot.genai_client.models._provider.outcomes = [
        FakeGenAIResponse("인트로 묘사.", usage=_usage())]
    cog = GameCog(wired_bot)

    result = await cog._execute_proceed(session_auto_ready, f"상:{kw} 진행")
    assert result["ok"] is True
    # 이미지 파일 메시지가 실제 생성됐다.
    assert any(m.files for m in game_channel.sent)
    # 그럼에도 자동 트랜잭션은 생기지 않는다.
    assert core.turn_transaction.get_active_transaction(session_auto_ready) is None


async def test_m4_partial_text_and_media_preserved_on_failure(
        wired_bot, session_auto_ready, game_channel, master_channel):
    """M4 — 텍스트+미디어 일부 성공 후 후속 send 실패 시, 이미 생성된 모든 소유 대상 ID가 보존된다."""
    session_auto_ready.turn_cost_log = []
    kw = _install_media(session_auto_ready)
    cog = GameCog(wired_bot)
    # p1 stream(send#1), 상: 이미지(send#2), p2 stream(send#3), p3 stream(send#4)
    narr = _narr_img(["문단1", "문단2", "문단3"], top=[kw])

    orig_send = game_channel.send
    state = {"n": 0}

    async def failing_send(content=None, **kw2):
        state["n"] += 1
        if state["n"] == 4:              # p3 스트리밍에서 실패
            raise RuntimeError("send #4 boom")
        return await orig_send(content, **kw2)
    game_channel.send = failing_send

    with pytest.raises(NarrationDeliveryError) as ei:
        await cog._deliver_narration(
            session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
            m_send=_mk(session_auto_ready, wired_bot, master_channel),
            cost_log_prefix="[AUTO] ", transient_ids=[])
    err = ei.value
    # 텍스트 2개(p1,p2)와 미디어 1개(폭포)가 보존된다.
    assert len(err.canonical_message_ids) == 2, err.canonical_message_ids
    assert len(err.media_message_ids) == 1, err.media_message_ids

    # 모든 소유 대상 ID를 좁게(멱등) 정리할 수 있다.
    game_channel.send = orig_send
    owned_ids = set(err.canonical_message_ids) | set(err.media_message_ids)
    owned = [m for m in game_channel.sent if m.id in owned_ids]
    assert len(owned) == 3
    await core.clear_messages(owned)
    await core.clear_messages(owned)   # 두 번째 — 멱등
    assert all(m.deleted for m in owned)


async def test_m5_media_cleanup_idempotent(wired_bot, session_auto_ready, game_channel, master_channel):
    """M5 — 미디어 메시지 정리도 두 번 호출/이미 삭제 상황에서 안전하다."""
    session_auto_ready.turn_cost_log = []
    kw = _install_media(session_auto_ready)
    cog = GameCog(wired_bot)
    narr = _narr_img(["문단."], top=[kw])

    delivery = await cog._deliver_narration(
        session_auto_ready, narr, game_channel=game_channel, master_ch=master_channel,
        m_send=_mk(session_auto_ready, wired_bot, master_channel),
        cost_log_prefix="[AUTO] ", transient_ids=[])

    media_msgs = [m for m in game_channel.sent if m.id in set(delivery.media_message_ids)]
    for m in media_msgs:
        m.raise_on_second_delete = True
    await core.clear_messages(media_msgs)
    await core.clear_messages(media_msgs)   # 예외 없이 통과해야 한다
    assert all(m.deleted for m in media_msgs)
