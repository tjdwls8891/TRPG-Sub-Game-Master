"""턴 엔진(indaim/turn) → 디스코드 어댑터 포트(리팩터링 T-5).

엔진은 디스코드·core를 import하지 않는다. 엔진이 쓰는 어댑터 쪽 이름(카드·채널 잠금·화면 클래스·송출/스트리밍·
디스플레이·화자 카드·대기 안내)과 테스트가 core 수준에서 바꿔 끼우는 이름(저장·provider 재시도·판정 처리 등)은
여기서 **호출 시점에** 해석한다 — core 수준 교체가 그대로 엔진에 닿는다. 이름 목록은 정확히 PORT_NAMES뿐이다
(T-6 정본 전환에서 엔진 쪽 실제 구현·이벤트로 바뀌며 줄어든다).
"""
from __future__ import annotations

import core

VIEW_NAMES = frozenset({"ExtractionRetryView", "GMRollView", "InfinityPlanView", "OpenConfirmView", "RollContinueView"})
CORE_NAMES = frozenset({
    "WaitingStatus", "accrue", "apply_normalized_companions", "auto_cost_cap_reached", "call_with_retry",
    "capture_state", "clean_text_for_tts", "clear_messages", "estimate_turn", "format_dialogue_block",
    "get_merged_status_effects", "is_enabled", "is_session_open", "master_report", "maybe_send_speaker_image",
    "media_control", "merge_consecutive_dialogues", "message_lifecycle", "parse_dialogue_paragraph",
    "parse_extraction", "play_dice_sfx", "process_roll_outcome", "refresh_boards", "refresh_display",
    "save_session_data", "select_bgm", "send_streamed", "session_flow", "speaker_card", "stream_text_to_channel",
    "write_log",
})
ADAPTER_NAMES = frozenset({"embed", "lock_channel"})
PORT_NAMES = VIEW_NAMES | CORE_NAMES | ADAPTER_NAMES


class Runtime:
    """엔진의 self.rt — 어댑터 이름 해석기(상태 없음)."""

    __slots__ = ()

    @staticmethod
    def embed(**kw):
        """카드(디스코드 Embed) — 엔진은 제목·본문·색만 넘긴다."""
        import discord
        return discord.Embed(**kw)

    @staticmethod
    async def lock_channel(channel):
        """게임 채널 잠금(결말 뒤) — 모두에게 쓰기 금지."""
        await channel.set_permissions(channel.guild.default_role, send_messages=False)

    def __getattr__(self, name):
        if name in VIEW_NAMES:
            from indaim.adapters.discord import gm_views
            return getattr(gm_views, name)
        if name in CORE_NAMES:
            import core
            return getattr(core, name)
        raise AttributeError(f"어댑터 포트에 없는 이름: {name}")


class TurnPorts:
    """GMCog 믹스인 — 엔진 믹스인(TurnService·TurnSteps·WorldSteps)이 쓰는 어댑터 쪽 자리."""

    rt = Runtime()

    @staticmethod
    def _revealed_identity_ids(session, prep, actor_ids, kind) -> tuple:
        """P3 G3: 식별 후보 중 본문에서 실제 공개가 확인된 InfoId만 정본 인지로 — 이미 아는 것은 그대로(멱등)."""
        if not core.speaker_card.card_world(session):
            return ()
        ids = core.speaker_card.identity_info_ids(core.speaker_card.config_of(session.scenario_data), actor_ids, kind)
        shown = set(getattr(prep, "identity_reveals", None) or {})
        return tuple(i for i in ids if i in shown)
