"""WP-00 테스트 픽스처.

WP00_EXECUTABLE_TEST_PLAN.md §2 · WP00_TEST_HARNESS_SPEC.md §3 기준.

원칙
    · 실제 `core.models.TRPGSession`을 쓴다. 광범위한 MagicMock은
      누락 필드 회귀를 숨기므로 상태 델타 테스트에서 금지한다.
    · 라이브 디스코드·Gemini·네트워크·자격증명·실계정 저장소를 쓰지 않는다.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.fakes.bot_fakes import FakeBot, RecordingCog          # noqa: E402
from tests.fakes.discord_fakes import (                           # noqa: E402
    FakeChannel, FakeInteraction, FakeMessage, FakeUser,
)
from tests.fakes.genai_fakes import (                             # noqa: E402
    FakeGenAIResponse, FakeUsageMetadata, ScriptedProvider,
)

# 저장소 루트. _isolated_cwd가 cwd를 tmp_path로 옮기므로, 소스를 읽는
# 정적 테스트는 반드시 이 절대경로를 써야 한다.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def source_of(relative_path: str) -> str:
    """저장소의 소스 파일을 읽는다. cwd와 무관하다."""
    return (pathlib.Path(REPO_ROOT) / relative_path).read_text(encoding="utf-8")


GAME_CH = 100_001
MASTER_CH = 100_002
DISPLAY_CH = 100_003
PLAYER_UID = "555001"


# ── 환경 격리 ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch):
    """라이브 외부 호출을 원천 차단한다.

    자격증명을 지우고, 실제 SDK 클라이언트 생성을 막는다.
    테스트가 실수로 네트워크를 타면 즉시 실패한다.
    """
    for var in ("GEMINI_API_KEY", "DISCORD_TOKEN", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "테스트에서 실제 provider SDK 클라이언트를 만들려 했습니다. "
            "FakeGenAIClient를 쓰십시오.")

    try:
        import google.genai as _genai
        monkeypatch.setattr(_genai, "Client", _forbidden, raising=False)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    """세션/계정/통계 파일이 저장소를 오염시키지 않게 한다.

    production 코드가 상대경로("sessions/{id}/...")를 쓰므로 cwd를 옮긴다.
    """
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# ── 시나리오 ────────────────────────────────────────────────

@pytest.fixture
def scenario_minimal() -> dict:
    """TRPGSession과 프롬프트/추출 헬퍼가 받아들이는 최소 시나리오.

    명시적·결정적으로 유지한다. 실제 시나리오 JSON을 읽지 않는다.
    """
    return {
        "title": "테스트 시나리오",
        "worldview": "테스트용 최소 세계관.",
        "story_guide": "테스트 진행 지침.",
        "desc_guide": "간결하게 묘사한다.",
        "ability_stats": ["근력", "민첩", "기술"],
        "ability_stat_max": 20,
        "status_effects": [
            {"name": "부상", "apply_condition": "물리적 충격"},
            {"name": "탈진", "apply_condition": "장시간 활동"},
        ],
        "default_npcs": {
            "김노인": {
                "name": "김노인",
                "소속·직책": "마을 촌장",
                "resources": {"약초": 2},
            },
        },
        "npc_template": {"info_fields": ["소속·직책"]},
        "places": {
            "마을": {"location_desc": "작은 마을", "connected": ["숲길"]},
            "숲길": {"location_desc": "좁은 길", "connected": ["마을"],
                     "npcs": [{"name": "김노인", "frequency": "상주"}]},
        },
        "prohibitions": [],
    }


# ── 세션 ────────────────────────────────────────────────────

@pytest.fixture
def session_factory(scenario_minimal):
    """실제 TRPGSession을 만든다."""
    import core

    def _make(session_id="wp00-session", scenario=None, **overrides):
        sess = core.TRPGSession(
            session_id, GAME_CH, MASTER_CH,
            "테스트", scenario if scenario is not None else scenario_minimal)
        sess.display_ch_id = DISPLAY_CH
        for k, v in overrides.items():
            setattr(sess, k, v)
        os.makedirs(f"sessions/{session_id}", exist_ok=True)
        return sess
    return _make


@pytest.fixture
def session_auto_ready(session_factory):
    """자동 GM이 턴을 돌릴 수 있는 최소 상태."""
    sess = session_factory()
    sess.is_started = True
    sess.gm_active = True
    sess.players = {
        PLAYER_UID: {
            "name": "테스터",
            "profile": {"근력": 10, "민첩": 12, "기술": 8},
            "fields": {"이름": "테스터", "직업": "떠돌이"},
        }
    }
    sess.gm_target_char = "테스터"
    sess.gm_target_chars = ["테스터"]
    sess.gm_turns_done = 0
    sess.gm_clarify_count = 0
    sess.gm_narrate_count = 0
    sess.gm_cost_baseline = 0.0
    sess.gm_side_note = ""
    sess.gm_pending_players = []
    sess.gm_collected_actions = {}
    sess.gm_waiting_for = None
    sess.gm_proceed_history = []
    sess.turn_count = 7
    sess.current_turn_logs = []
    sess.raw_logs = []
    sess.uncompressed_logs = []
    sess.world_timeline = {"current_location": "마을", "time_of_day": "낮"}
    # 캐시가 있어야 is_session_open()이 True다. 실제 SDK 객체는 쓰지 않는다.
    sess.cache_name = "caches/fake-session"
    sess.cache_model = None
    sess.cache_tokens = 1024
    sess.cache_created_at = 10_000_000_000.0
    sess.open_minutes = 180
    return sess


@pytest.fixture
def session_with_quest(session_auto_ready):
    """core.quest 변이 테스트에 충분한 최소 퀘스트 상태."""
    session_auto_ready.quest_state = {
        "active": {
            "id": "q_test", "name": "시험 사건", "version": "a",
            "line": "sub", "node": "root", "path": [],
            "slots": {"장소": "마을"}, "started_turn": 6,
            "intended_case": None, "secret_known": False,
        },
        "cleared": [],
        "known_secrets": [],
        "occurrences": {"시험 사건": 1},
    }
    return session_auto_ready


@pytest.fixture
def session_with_resource_status(session_auto_ready):
    """자원·상태 변이 테스트용."""
    session_auto_ready.resources = {
        "테스터": {"물통": 2, "밧줄": 1},
        "김노인": {"약초": 2},
    }
    session_auto_ready.statuses = {"테스터": []}
    return session_auto_ready


@pytest.fixture
def session_with_rewind_history(session_auto_ready):
    """되감기 기록이 있는 세션."""
    import core
    sess = session_auto_ready
    snap = core.capture_state(sess)
    sess._rewind_snapshot = snap
    return sess


# ── 봇 ──────────────────────────────────────────────────────

@pytest.fixture
def provider():
    return ScriptedProvider()


@pytest.fixture
def fake_bot(provider):
    bot = FakeBot(provider=provider)
    bot.add_channel(GAME_CH, "game")
    bot.add_channel(MASTER_CH, "master")
    bot.add_channel(DISPLAY_CH, "display")
    return bot


@pytest.fixture
def wired_bot(fake_bot, session_auto_ready):
    """세션이 등록된 봇."""
    fake_bot.register_session(session_auto_ready)
    return fake_bot


@pytest.fixture
def game_channel(fake_bot):
    return fake_bot.get_channel(GAME_CH)


@pytest.fixture
def master_channel(fake_bot):
    return fake_bot.get_channel(MASTER_CH)


@pytest.fixture
def display_channel(fake_bot):
    return fake_bot.get_channel(DISPLAY_CH)


# ── 순서 기록기 ─────────────────────────────────────────────

class CallRecorder:
    """호출 순서를 기록한다. 특성화 테스트의 핵심 도구."""

    def __init__(self):
        self.order: list[str] = []
        self.payloads: dict[str, list] = {}

    def make(self, label: str, *, result=None, before=None):
        async def _rec(*args, **kwargs):
            self.order.append(label)
            self.payloads.setdefault(label, []).append((args, kwargs))
            if before is not None:
                before(*args, **kwargs)
            return result() if callable(result) else result
        return _rec

    def make_sync(self, label: str, *, result=None):
        def _rec(*args, **kwargs):
            self.order.append(label)
            self.payloads.setdefault(label, []).append((args, kwargs))
            return result() if callable(result) else result
        return _rec

    def index(self, label: str) -> int:
        return self.order.index(label)

    def first_args(self, label: str):
        return self.payloads[label][0][0]

    def first_kwargs(self, label: str):
        return self.payloads[label][0][1]

    def __contains__(self, label):
        return label in self.order

    def __repr__(self):
        return f"<CallRecorder {self.order}>"


@pytest.fixture
def recorder():
    return CallRecorder()


# ── 편의 재수출 ─────────────────────────────────────────────

@pytest.fixture
def usage():
    """표준 usage_metadata. 사고 토큰을 포함한다."""
    return FakeUsageMetadata(prompt=1200, candidates=300, thoughts=100,
                             cached=1000)


@pytest.fixture
def fake_interaction(game_channel):
    return FakeInteraction(user=FakeUser(int(PLAYER_UID)),
                           channel=game_channel,
                           message=FakeMessage(channel=game_channel))
