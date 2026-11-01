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
def _talk_instant(monkeypatch):
    """v7.36 대화 박자 스트리밍은 연출 지연이다 — 테스트는 기다리지 않는다(박자 위치는 테스트마다 비운다)."""
    import core.talk as _talk
    monkeypatch.setattr(_talk, "TICK", 0.0)
    _talk._POS.clear()
    _talk._ECHO.clear()
    _talk._TEXT.clear()
    yield


@pytest.fixture(autouse=True)
def _kernel_shadow(monkeypatch, request):
    """리팩터링 K-c 그림자 검사 — 옛 production 투영이 성공할 때마다 이식된 모듈(indaim.rules)로도 투영해
    결과가 같은지 본다. 차이가 하나라도 나면 그 테스트가 실패한다(운영 경로에는 붙지 않는다)."""
    import core.world_mutation as _wm
    from indaim.migrate import ops_v4 as _sh
    orig, diffs = _wm.project, []

    def shadow(source, batch, registry):
        res = orig(source, batch, registry)
        if res.ok and registry is _wm.PRODUCTION_REGISTRY and not batch.is_empty():
            src = source if isinstance(source, _wm.ProjectedWorld) else _wm.ProjectedWorld.capture(source)
            try:
                d = _sh.shadow_compare(src.to_dict(), batch, res.world.to_dict())
            except Exception as e:  # noqa: BLE001 — 그림자 검사의 결함이 운영 경로를 바꾸지 않게
                import traceback
                d = [f"그림자 검사 예외 {type(e).__name__}: {e} {traceback.format_exc()[-600:]}"]
            if d:
                diffs.append((batch.batch_id, d[:5]))
        return res
    monkeypatch.setattr(_wm, "project", shadow)
    # T-1·T-2: 커밋의 추출 적용마다 확정 훅(vitals·items)을 같은 재료로 돌려 결과를 대조한다
    import copy as _copy
    import cogs.gm as _gm
    import core.items as _it
    from indaim.migrate import v4 as _v4
    from indaim.rules.vitals import logic as _vl
    orig_apply, orig_decide = _gm.GMCog._apply_prepared_extraction, _vl.decide_turn
    orig_grants, orig_plan = _it.apply_event_grants, _it.apply_plan
    import core.growth as _gr
    import core.turn_preparation as _tpm
    orig_turn_inputs, orig_staged = _gr.turn_inputs, _tpm.apply_staged_growth
    import core as _core
    orig_comp = _core.apply_normalized_companions
    seen = {}

    def companions(session, norm):
        seen.setdefault("party", []).append(_copy.deepcopy(norm))
        return orig_comp(session, norm)

    def turn_inputs(session, prep):
        r = orig_turn_inputs(session, prep)
        seen.setdefault("growth", []).append(_copy.deepcopy(r))
        return r

    def staged(session, prep):
        if "no_kernel_shadow" in request.keywords:
            return orig_staged(session, prep)
        pre = _copy.deepcopy({f: getattr(session, f, None) for f in _v4.FIELDS})
        inp = {"events": _copy.deepcopy(prep.growth_events), "fail_counts": _copy.deepcopy(prep.growth_fail_counts)}
        out = orig_staged(session, prep)
        if out:
            _check("growth", _sh.shadow_growth, pre, {"sd": session.scenario_data or {}, "staged": inp},
                   {f: getattr(session, f, None) for f in _v4.FIELDS}, tx=str(prep.transaction_id))
        return out

    def decide(merged, **kw):
        seen.setdefault("vitals", []).append({"merged": merged, **_copy.deepcopy(kw)})
        return orig_decide(merged, **kw)

    def grants(session, rows, *, definition=None, pc_name=None):
        seen.setdefault("grants", []).append({"grants": _copy.deepcopy(rows), "definition": definition, "pc_name": pc_name})
        return orig_grants(session, rows, definition=definition, pc_name=pc_name)

    def plan(session, p):
        row = {"ops": _copy.deepcopy(p.ops), "defs": _copy.deepcopy(p.defs), "pc_name": p.pc_name,
               "scene_place": p.scene_place, "tx": p.transaction_id, "recipes": _copy.deepcopy(p.recipes)}
        if inside[0] or "no_kernel_shadow" in request.keywords:
            seen.setdefault("plan", []).append(row)
            return orig_plan(session, p)
        # 커밋 밖(물건 단위 테스트의 직접 호출) — 그 자리에서 대조
        pre = _copy.deepcopy({f: getattr(session, f, None) for f in _v4.FIELDS})
        out = orig_plan(session, p)
        _check("items", _sh.shadow_items, _defaults(pre), {"plan": row},
               _defaults({f: getattr(session, f, None) for f in _v4.FIELDS}), tx=str(p.transaction_id))
        return out

    def _defaults(d):
        return {k: v for k, v in d.items() if v is not None}
    inside = [False]

    def _check(kind, fn, *args, **kw):
        try:
            d = fn(*args, **kw)
        except Exception as e:  # noqa: BLE001
            import traceback
            d = [f"그림자 검사 예외 {type(e).__name__}: {e} {traceback.format_exc()[-700:]}"]
        if d:
            diffs.append((kind, d[:5]))

    async def apply(self, session, prep, *a, **kw):
        if "no_kernel_shadow" in request.keywords:
            return await orig_apply(self, session, prep, *a, **kw)
        pre = _copy.deepcopy({f: getattr(session, f, None) for f in _v4.FIELDS})
        seen.clear()
        inside[0] = True
        try:
            out = await orig_apply(self, session, prep, *a, **kw)
        finally:
            inside[0] = False
        post = {f: getattr(session, f, None) for f in _v4.FIELDS}
        tx = str(getattr(prep, "transaction_id", "tx"))
        if len(seen.get("vitals", ())) == 1:
            _check("vitals", _sh.shadow_vitals, pre, seen["vitals"][0], post, tx=tx)
        if getattr(prep, "world_turn", None) is None:
            pass                        # 은퇴한 LEGACY 경로(추출 item_changes 직접 반영)는 새 엔진 대상이 아니다(T-6 제거)
        elif len(seen.get("grants", ())) <= 1 and len(seen.get("plan", ())) <= 1:
            g = (seen.get("grants") or [{}])[0]
            _check("items", _sh.shadow_items, pre, {**g, "plan": (seen.get("plan") or [None])[0]}, post, tx=tx)
        else:
            diffs.append(("items", [f"물건 적용 호출이 여러 번 {[(k, len(v)) for k, v in seen.items()]}"]))
        if getattr(prep, "world_turn", None) is not None and len(seen.get("party", ())) == 1:
            _check("party", _sh.shadow_party, pre, seen["party"][0], post, tx=tx)
        if len(seen.get("growth", ())) == 1 and seen["growth"][0] is not None:
            _check("growth", _sh.shadow_growth, pre, {"sd": session.scenario_data or {}, "turn": seen["growth"][0]},
                   post, tx=tx)
        return out
    monkeypatch.setattr(_vl, "decide_turn", decide)
    monkeypatch.setattr(_it, "apply_event_grants", grants)
    monkeypatch.setattr(_it, "apply_plan", plan)
    monkeypatch.setattr(_gm.GMCog, "_apply_prepared_extraction", apply)
    monkeypatch.setattr(_gr, "turn_inputs", turn_inputs)
    monkeypatch.setattr(_core, "apply_normalized_companions", companions)
    # T-5: 플레이어 턴 계획을 커널 세계 보기(KernelProjectedWorld)로도 세워 같은 계획이 나오는지 본다
    import core.player_world as _pwm
    from indaim.bridge.kernel_view import KernelProjectedWorld
    orig_plan_turn = _pwm.plan_player_turn

    def plan_turn(ctx, world, intent, **kw):
        out = orig_plan_turn(ctx, world, intent, **kw)
        if "no_kernel_shadow" in request.keywords or isinstance(world, _wm.ProjectedWorld):
            return out
        try:
            conv = _v4.convert({f: getattr(world, f, None) for f in _v4.FIELDS
                                if getattr(world, f, None) is not None}, game_keys=set())
            twin = orig_plan_turn(ctx, KernelProjectedWorld(conv, definition_revision=getattr(world, "definition_revision", None)),
                                  intent, **kw)
            d = _sh.compare_plans(out, twin)
        except Exception as e:  # noqa: BLE001
            import traceback
            d = [f"그림자 검사 예외 {type(e).__name__}: {e} {traceback.format_exc()[-700:]}"]
        if d:
            diffs.append(("plan", d[:5]))
        return out
    monkeypatch.setattr(_pwm, "plan_player_turn", plan_turn)
    monkeypatch.setattr(_tpm, "apply_staged_growth", staged)
    yield
    if "no_kernel_shadow" not in request.keywords:
        assert not diffs, f"새 커널 그림자 불일치 {len(diffs)}건: {diffs[:3]}"


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
