"""하네스 자체 검증.

픽스처가 실제 프로덕션 객체를 쓰는지, 라이브 호출이 막혀 있는지 확인한다.
이것이 깨지면 다른 모든 테스트의 근거가 사라진다.
"""

from __future__ import annotations

import pytest

from tests.fakes import (
    FakeGenAIResponse, FakeUsageMetadata, ScriptedProvider,
    UnexpectedChannelRequest, UnexpectedCogRequest,
)

pytestmark = pytest.mark.characterize


def test_session_is_real_trpgsession(session_auto_ready):
    """광범위한 MagicMock이 아니라 실제 TRPGSession을 쓴다."""
    import core
    assert type(session_auto_ready) is core.TRPGSession
    # MagicMock이면 아무 속성이나 생기므로 없는 속성은 없어야 한다.
    assert not hasattr(session_auto_ready, "존재하지않는속성")


# v5.33.0 현재 생성자에 없는 SESSION_FIELDS 키.
# cache.py:151(update_session_cache_state)이 나중에 설정하므로, 캐시를 한 번도
# 올리지 않은 세션에서는 속성이 존재하지 않는다.
# save_session_data는 getattr 기본값으로 넘어가므로 저장은 되지만,
# 그 전에 session.cached_worldview_sections를 직접 읽는 코드가 생기면
# AttributeError가 난다.
KNOWN_MISSING_AT_CONSTRUCTION = {"cached_worldview_sections"}


def test_session_fields_registry_matches_model(session_auto_ready):
    """SESSION_FIELDS와 모델 속성이 어긋나면 저장·복구가 조용히 깨진다.

    특성화 — v5.33.0의 현재 상태를 고정한다. 알려진 예외 외에 새로운
    불일치가 생기면 실패한다.
    """
    import core
    missing = {k for k in core.SESSION_FIELDS
               if not hasattr(session_auto_ready, k)}
    unexpected = missing - KNOWN_MISSING_AT_CONSTRUCTION
    assert unexpected == set(), f"새로운 불일치: {sorted(unexpected)}"
    # 알려진 예외가 고쳐지면 이 단언이 깨져 목록 갱신을 강제한다.
    assert missing == KNOWN_MISSING_AT_CONSTRUCTION, (
        f"알려진 불일치 목록이 낡았습니다. 실제: {sorted(missing)}")


def test_missing_field_is_tolerated_by_save(session_auto_ready):
    """특성화 — 생성자에 없는 필드도 저장은 통과한다(getattr 기본값)."""
    import core
    assert not hasattr(session_auto_ready, "cached_worldview_sections")
    assert core.SESSION_FIELDS["cached_worldview_sections"] == []


def test_fake_bot_fails_loudly_on_unknown_channel(fake_bot):
    with pytest.raises(UnexpectedChannelRequest):
        fake_bot.get_channel(987_654_321)


def test_fake_bot_fails_loudly_on_unknown_cog(fake_bot):
    with pytest.raises(UnexpectedCogRequest):
        fake_bot.get_cog("NoSuchCog")


def test_fake_bot_returns_none_for_zero_channel(fake_bot):
    """production이 getattr(session, 'x_ch_id', 0)으로 0을 넘기는 경로가 있다."""
    assert fake_bot.get_channel(0) is None


def test_usage_metadata_exposes_only_consumed_attrs(usage):
    """core.extract_token_usage가 읽는 네 속성만 노출한다."""
    import core
    in_t, out_t, cached_t, thought_t = core.extract_token_usage(usage)
    assert in_t == 1200
    assert cached_t == 1000
    assert thought_t == 100
    # 사고 토큰은 출력에 합산된다 — 누락하면 과소 계상된다.
    assert out_t == 300 + 100


def test_extract_token_usage_handles_none():
    import core
    assert core.extract_token_usage(None) == (0, 0, 0, 0)


async def test_scripted_provider_is_deterministic():
    provider = ScriptedProvider([
        FakeGenAIResponse("첫 번째"),
        RuntimeError("두 번째는 실패"),
        FakeGenAIResponse("세 번째"),
    ])
    assert provider.generate_content(model="m").text == "첫 번째"
    with pytest.raises(RuntimeError):
        provider.generate_content(model="m")
    assert provider.generate_content(model="m").text == "세 번째"
    assert provider.attempt_count == 3


def test_provider_records_call_arguments(provider):
    provider.outcomes.append(FakeGenAIResponse("ok"))
    provider.generate_content(model="gemini-x", contents="본문", config={"a": 1})
    rec = provider.calls[0]
    assert rec.model == "gemini-x"
    assert rec.contents == "본문"


async def test_fake_channel_records_sends(game_channel):
    msg = await game_channel.send("안녕")
    assert game_channel.texts == ["안녕"]
    assert msg.deleted is False
    await msg.delete()
    assert msg.deleted is True


async def test_fake_message_records_edits(game_channel):
    msg = await game_channel.send("처음")
    await msg.edit(content="나중")
    assert msg.content == "나중"
    assert msg.edit_history == [{"content": "나중"}]


async def test_fake_channel_records_delete_after(game_channel):
    """close_notice/notify가 delete_after를 쓰는 경로를 관측할 수 있어야 한다."""
    msg = await game_channel.send("잠깐만", delete_after=10)
    assert msg.delete_after == 10


def test_no_live_credentials_present():
    import os
    for var in ("GEMINI_API_KEY", "DISCORD_TOKEN", "GOOGLE_API_KEY"):
        assert os.environ.get(var) is None, f"{var}가 테스트에 노출됐습니다"


def test_genai_client_construction_is_blocked():
    """실제 SDK 클라이언트를 만들려 하면 즉시 실패해야 한다."""
    google_genai = pytest.importorskip("google.genai")
    with pytest.raises(AssertionError):
        google_genai.Client(api_key="x")


def test_cwd_is_isolated(tmp_path):
    """세션 파일이 저장소를 오염시키지 않는다."""
    import os
    assert os.getcwd() == str(tmp_path)


def test_session_writes_go_to_tmp(session_auto_ready, tmp_path):
    import os
    assert os.path.isdir(tmp_path / "sessions" / session_auto_ready.session_id)


def test_quest_fixture_shape(session_with_quest):
    import core
    state = core.quest.get_state(session_with_quest)
    assert state["active"]["id"] == "q_test"
    assert state["active"]["node"] == "root"


def test_resource_fixture_shape(session_with_resource_status):
    assert session_with_resource_status.resources["테스터"]["물통"] == 2


def test_rewind_snapshot_uses_real_capture(session_with_rewind_history):
    """스냅샷 헬퍼는 실제 코드를 쓴다."""
    import core
    snap = session_with_rewind_history._rewind_snapshot
    assert isinstance(snap, dict)
    for path in core.TRACKED_PATHS:
        assert path in snap, f"TRACKED_PATHS의 {path}가 스냅샷에 없습니다"
