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


def legacy_rel(name_or_path: str) -> str:
    """옛 core 모듈 이름('items' · 'items.py' · 'core/items.py') → 실제 저장소 상대 경로(리팩터링 T-5 재배치 뒤 indaim 아래).
    core/__init__.py는 그대로다. 옛 core 모듈이 아니면 받은 값을 그대로 돌려준다."""
    from indaim.legacy_map import MAP
    n = name_or_path.replace("\\", "/")
    stem = n[len("core/"):] if n.startswith("core/") else n
    stem = stem[:-3] if stem.endswith(".py") else stem
    if stem in MAP:
        return os.path.join(*MAP[stem].split(".")) + ".py"
    if stem == "__init__" and n in ("__init__", "__init__.py", "core/__init__.py"):
        return os.path.join("core", "__init__.py")
    return name_or_path


# GM cog 구현 = cogs/gm.py(명령어·리스너) + 본문 그대로 옮긴 믹스인·화면·설정(리팩터링 T-5).
#   'cogs/gm.py'를 읽는 소스 스캔은 이 전체를 하나의 GM 구현으로 본다. 엔진으로 옮긴 조각은 core 대신
#   어댑터 포트(self.rt.X)·정의 모듈 별칭(_mod.X)으로 같은 대상을 부르므로, 스캔용으로 옛 표기(core.X)로 되돌려 읽는다.
GM_PARTS = ("cogs/gm.py", "indaim/adapters/discord/gm_views.py", "indaim/adapters/discord/runtime.py",
            "indaim/turn/config.py", "indaim/turn/outcomes.py", "indaim/turn/service.py",
            "indaim/turn/flow_steps.py", "indaim/turn/world_steps.py")


def _legacy_spelling(text: str) -> str:
    import re as _re
    from indaim.legacy_map import MAP
    where = {v: k for k, v in MAP.items()}
    for pkg, leaf, alias in _re.findall(r"^from (indaim[\w.]*) import (\w+) as (_\w+)$", text, _re.M):
        old = where.get(f"{pkg}.{leaf}")
        if old:
            text = _re.sub(r"(?<![\w.])" + _re.escape(alias) + r"\.", f"core.{old}.", text)
    text = text.replace("self.rt.discord.", "discord.").replace("self.rt.embed(", "discord.Embed(")
    text = _re.sub(r"self\.rt\.(\w+View)\(", r"\1(", text)
    return text.replace("self.rt.", "core.")


def prompts_source() -> str:
    """층위별로 나눈 프롬프트 패키지(indaim/content/prompts/)의 원문 전체(T-5) — 예전 prompts.py 원문 스캔용."""
    pkg = pathlib.Path(REPO_ROOT) / "indaim" / "content" / "prompts"
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))


def logical_rel(relative_path: str) -> str:
    """GM 구현 조각(GM_PARTS) 경로는 'cogs/gm.py'로 읽는다 — 호출자·소유자 스캔의 파일 이름 정규화."""
    rel = relative_path.replace("\\", "/")
    return "cogs/gm.py" if rel in GM_PARTS else rel


def source_of(relative_path: str) -> str:
    """저장소의 소스 파일을 읽는다. cwd와 무관하다. 'core/x.py'는 옮긴 실제 위치를 읽는다(legacy_rel),
    'cogs/gm.py'는 GM 구현 전체(GM_PARTS)를 이어 붙여 옛 표기로 읽는다."""
    if relative_path.replace("\\", "/") == "cogs/gm.py":
        return "\n".join(_legacy_spelling((pathlib.Path(REPO_ROOT) / p).read_text(encoding="utf-8")) for p in GM_PARTS)
    if relative_path.replace("\\", "/") in ("prompts.py", "indaim/content/prompts.py"):
        return prompts_source()
    return (pathlib.Path(REPO_ROOT) / legacy_rel(relative_path)).read_text(encoding="utf-8")


def read_session_disk(session_id) -> dict:
    """저장된 세션 data.json을 옛 필드 사전으로 읽는다(v5 세계 → 역변환, T-6) — 디스크 정본 확인용."""
    import json as _json
    from indaim.app import world_codec as _wc
    with open(f"sessions/{session_id}/data.json", encoding="utf-8") as f:
        return _wc.decode(_json.load(f))


def internal_imports(node) -> set:
    """import 문 하나가 가져오는 옛 core 모듈 이름 — 상대 import(`from . import x`·`from .x import y`)와
    재배치 뒤 절대 import(`from indaim.<패키지> import x`·`from indaim.<패키지>.x import y`)를 같게 읽는다."""
    import ast as _ast
    from indaim.legacy_map import MAP
    where = {v: k for k, v in MAP.items()}
    if not isinstance(node, _ast.ImportFrom):
        if isinstance(node, _ast.Import):
            return {where[a.name] for a in node.names if a.name in where}
        return set()
    if node.level == 1:
        return {node.module.split(".")[0]} if node.module else {a.name for a in node.names}
    mod = node.module or ""
    if mod in where:
        return {where[mod]}
    if mod.startswith("indaim."):
        return {where[f"{mod}.{a.name}"] for a in node.names if f"{mod}.{a.name}" in where}
    return set()


# '누가 부르는가' 스캔에서 빼는 이행 장치 — 옛 연산 이름을 번역표 키로 쓰는 v4 변환기·번역기(migrate — T-6부터 운영
#   커밋·저장이 쓴다)와 옛 모듈 이름을 위치 표로만 적는 legacy_map. 이름이 나온다고 그 연산의 producer가 아니다.
TRANSITION_FILES = ("indaim/migrate/", "indaim/legacy_map.py")


def prod_py_files(*bases) -> list:
    """운영 코드 파일(절대 경로, 정렬). base 'core' = core/__init__.py + indaim 전체(옮긴 옛 core와 새 엔진 — 같은 불변식 대상,
    이행 장치 TRANSITION_FILES 제외), 그 밖의 base(cogs 등)는 그 디렉터리의 .py 전부(하위 포함)."""
    out = []
    for base in bases:
        roots = [("core", False), ("indaim", True)] if base == "core" else [(base, True)]
        for d, rec in roots:
            top = os.path.join(REPO_ROOT, d)
            if not rec:
                out += [os.path.join(top, f) for f in sorted(os.listdir(top)) if f.endswith(".py")]
                continue
            for dp, dirs, fs in os.walk(top):
                dirs[:] = sorted(x for x in dirs if x != "__pycache__")
                out += [os.path.join(dp, f) for f in sorted(fs) if f.endswith(".py")
                        and not os.path.relpath(os.path.join(dp, f), REPO_ROOT).replace("\\", "/").startswith(TRANSITION_FILES)]
    return out


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
def _talk_instant(monkeypatch):
    """v7.36 대화 박자 스트리밍은 연출 지연이다 — 테스트는 기다리지 않는다(박자 위치는 테스트마다 비운다)."""
    import core.talk as _talk
    monkeypatch.setattr(_talk, "TICK", 0.0)
    _talk._POS.clear()
    _talk._ECHO.clear()
    _talk._TEXT.clear()
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
