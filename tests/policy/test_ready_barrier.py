"""WP-C — 동시 준비(Concurrent Preparation) + READY_TO_COMMIT 배리어.

실제 자동 턴 경로(_finish_proceed_and_continue → _dispatch_proceed →
GameCog._execute_proceed → _deliver_narration)를 fake provider/채널로 구동한다.
provider는 호출 종류(묘사/추출/비정규 NPC/NPC 세부/서사 계획/압축)별로 라우팅되며,
스트리밍과 추출은 게이트(Event)로 순서를 결정적으로 통제한다.

T-C02~T-C28 / F-C01~F-C13 매핑은 각 테스트 docstring에 적는다.
라이브 SDK·네트워크·자격증명은 conftest가 차단한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import pytest

import core
from core import turn_preparation as tp
from core import turn_transaction as tt
from core.cost_ledger import CostLedger
from tests.fakes.genai_fakes import FakeGenAIResponse, FakeUsageMetadata

pytestmark = pytest.mark.policy


# ══════════════════════════════════════════════════════════════
# 하네스
# ══════════════════════════════════════════════════════════════

def _usage(p=1000, c=200):
    return FakeUsageMetadata(prompt=p, candidates=c, thoughts=0, cached=0)


class RoutingProvider:
    """호출 종류별로 응답을 고른다(동시 호출에서 큐 순서 비결정성을 없앤다).

    각 종류는 결과 리스트(소진 시 마지막 반복) 또는 callable(kwargs)->결과.
    gate[kind] 가 threading.Event 이면 provider 스레드가 그 이벤트를 기다린다.
    """

    def __init__(self):
        self.routes = {}
        self.gates = {}
        self.started = {}
        self.calls = []
        self.attempt_count = 0
        self.lock = threading.Lock()

    def kind_of(self, kwargs):
        import cogs.gm as gm
        cfg = kwargs.get("config")
        sysi = getattr(cfg, "system_instruction", None)
        if isinstance(kwargs.get("contents"), str):
            return "compression"
        by_sys = {
            gm.EXTRACTION_SYSTEM_INSTRUCTION: "extraction",
            gm.IRREGULAR_NPC_SYSTEM_INSTRUCTION: "irregular",
            gm.NPC_DETAIL_SYSTEM_INSTRUCTION: "npc_detail",
            gm.NARRATIVE_PLANNER_SYSTEM_INSTRUCTION: "planner",
        }
        for k, v in by_sys.items():
            if sysi == k:
                return v
        return "narration"

    def generate_content(self, **kwargs):
        kind = self.kind_of(kwargs)
        with self.lock:
            self.attempt_count += 1
            self.calls.append(kind)
            self.started[kind] = self.started.get(kind, 0) + 1
        gate = self.gates.get(kind)
        if gate is not None:
            assert gate.wait(10), f"{kind} gate timeout"
        route = self.routes.get(kind)
        if callable(route):
            item = route(kwargs)
        else:
            seq = route or [RuntimeError(f"no route for {kind}")]
            item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item


def _json(payload, p=800, c=100):
    return FakeGenAIResponse(json.dumps(payload, ensure_ascii=False), usage=_usage(p, c))


@pytest.fixture
def rig(wired_bot, session_auto_ready, master_channel, game_channel, monkeypatch, tmp_path):
    """실제 GMCog/GameCog + 라우팅 provider + 실 CostLedger(tmp) + 게이트 가능한 스트림."""
    import cogs.game as game_mod
    import cogs.gm as gm_mod

    sess = session_auto_ready
    sess.cache_obj = object()
    sess.cache_name = "caches/fake"
    sess.cache_model = core.DEFAULT_MODEL
    sess.turn_cost_log = []
    sess.tts_enabled = False
    sess.is_started = True
    master_channel.guild = None

    prov = RoutingProvider()
    prov.routes["narration"] = [FakeGenAIResponse("숲길로 조용히 걸어간다.", usage=_usage())]
    prov.routes["extraction"] = [_json({"situation": {}})]
    wired_bot.genai_client.models._provider = prov
    wired_bot.cost_ledger = CostLedger(str(tmp_path / "ledger.jsonl"))

    gm = gm_mod.GMCog.__new__(gm_mod.GMCog)
    gm.bot = wired_bot
    gm._session_locks = {}
    game = game_mod.GameCog(wired_bot)
    wired_bot.add_cog_stub("GMCog", gm)
    wired_bot.add_cog_stub("GameCog", game)

    ev = {"order": [], "stream_gate": None, "stream_delay": 0.0,
          "stream_fail_at": None, "streamed": 0}

    async def _stream(bot, channel, text, *a, collector=None, **k):
        ev["order"].append("stream_start")
        if ev["stream_gate"] is not None:
            await ev["stream_gate"].wait()
        if ev["stream_delay"]:
            await asyncio.sleep(ev["stream_delay"])
        ev["streamed"] += 1
        if ev["stream_fail_at"] is not None and ev["streamed"] >= ev["stream_fail_at"]:
            raise RuntimeError("스트리밍 실패(테스트)")
        m = await channel.send(text)
        if collector is not None:
            collector.append(m)
        ev["order"].append("stream_done")
        return [m]

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(core, "stream_text_to_channel", _stream)
    monkeypatch.setattr(core, "refresh_display", _noop)
    rec = {"deduct": [], "legacy_deduct": [], "start_round": 0, "ready_snapshots": []}

    async def _deduct(uid, ink, **k):
        rec["legacy_deduct"].append((uid, ink))
        return {"ok": True}
    monkeypatch.setattr(core.accounts, "deduct_ink", _deduct)
    # WP-D: 정상 자동 턴의 청구 권위는 Settlement → InkTransaction이다. rec["deduct"]는
    #   실제로 계정에 적용된 Settlement 청구를 기록한다(legacy deduct_ink는 0이어야 함).
    _orig_charges = core.ink_transactions.execute_settlement_charges

    async def _charges(settlement, **k):
        out = await _orig_charges(settlement, **k)
        if settlement.charge_ink_per_user > 0:
            for uid in settlement.billing_user_ids:
                rec["deduct"].append((uid, settlement.charge_ink_per_user))
        return out
    monkeypatch.setattr(core.ink_transactions, "execute_settlement_charges", _charges)
    monkeypatch.setattr(core.stats, "bump", _noop)
    monkeypatch.setattr(core.stats, "add_npcs", _noop, raising=False)

    async def _start_round(session):
        rec["start_round"] += 1
        ev["order"].append("start_round")
    monkeypatch.setattr(gm, "_start_round", _start_round)

    orig_ready = tp.transition_to_ready

    def _ready(session, prep):
        snap = _canon(session)
        proof, reasons = orig_ready(session, prep)
        if proof is not None:
            ev["order"].append("ready")
            rec["ready_snapshots"].append(
                {"canon": snap, "status": tt.get_active_transaction(session).status})
        return proof, reasons
    monkeypatch.setattr(tp, "transition_to_ready", _ready)

    class R:
        pass
    r = R()
    r.sess, r.gm, r.game, r.prov, r.ev, r.rec = sess, gm, game, prov, ev, rec
    r.bot, r.master, r.gch = wired_bot, master_channel, game_channel
    r.ledger = wired_bot.cost_ledger
    return r


def _diff(a, b):
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def _canon(session):
    """리뷰된 커밋 소유 도메인의 값 스냅샷(비교용)."""
    import copy
    keys = ("quest_state", "info_ledger", "resources", "statuses", "world_timeline",
            "visited_places", "companions", "met_npcs", "irregular_npcs", "npcs",
            "narrative_plan", "pending_ending", "last_extraction", "players",
            "stat_fail_counts", "turn_count", "gm_turns_done", "last_recorded_turn",
            "gm_proceed_history")
    out = {k: copy.deepcopy(getattr(session, k, None)) for k in keys}
    out["quest_state"] = core.quest.clone_state(session)   # 리더 지연 정규화 무시
    out["raw_logs_len"] = len(session.raw_logs)
    out["uncompressed_len"] = len(session.uncompressed_logs)
    return out


async def _run_turn(r, *, event_assessment="ongoing", tx=None):
    tx = tx or tt.get_or_begin_turn_transaction(r.sess, "숲길로 간다")
    await r.gm._finish_proceed_and_continue(
        r.sess, "숲길로 이동한다", r.master,
        event_assessment=event_assessment, transaction_id=tx.transaction_id)
    return tx


def _events(r, tx=None):
    rows = r.ledger.list_cost_events()
    if tx is not None:
        rows = [e for e in rows if e.get("transaction_id") == tx.transaction_id]
    return rows


async def _until(pred, timeout=5.0):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("조건 대기 시간 초과")
        await asyncio.sleep(0.005)


# ══════════════════════════════════════════════════════════════
# T-C02 / T-C03 / T-C04 / H — 실제 겹침과 합류
# ══════════════════════════════════════════════════════════════

async def test_tc02_stream_and_extraction_really_overlap(rig):
    """T-C02 — 확정 묘사 이후 전달(스트림)과 추출이 동시에 진행된다(Event 증명).

    스트림은 asyncio 게이트, 추출 provider는 스레드 게이트에서 멈춘다. 둘 다 시작된
    것을 확인한 뒤에야 풀어준다. 배리어는 둘 다 끝난 뒤에만 READY/다음 라운드.
    """
    r = rig
    r.ev["stream_gate"] = asyncio.Event()
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate

    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: "stream_start" in r.ev["order"]
                 and r.prov.started.get("extraction", 0) >= 1)
    # 둘 다 시작됨 — 어느 쪽도 끝나지 않았다.
    assert "stream_done" not in r.ev["order"]
    assert "ready" not in r.ev["order"] and r.rec["start_round"] == 0

    ext_gate.set()                                  # 추출 먼저 종료
    await asyncio.sleep(0.05)
    assert "ready" not in r.ev["order"], "전달 완료 전 READY"
    r.ev["stream_gate"].set()
    await owner
    o = r.ev["order"]
    assert o.index("stream_done") < o.index("ready") < o.index("start_round")


async def test_tc02b_wall_clock_overlap(rig):
    """T-C02(보조) — 0.25s 스트림 + 0.25s 추출이 직렬(≥0.5s)보다 확연히 빠르다."""
    r = rig
    r.ev["stream_delay"] = 0.25

    def _slow_extraction(kwargs):
        time.sleep(0.25)
        return _json({"situation": {}})
    r.prov.routes["extraction"] = _slow_extraction

    t0 = time.monotonic()
    await _run_turn(r)
    elapsed = time.monotonic() - t0
    assert "ready" in r.ev["order"]
    assert elapsed < 0.45, f"겹침 없이 직렬 실행된 것으로 보입니다: {elapsed:.3f}s"


async def test_tc03_fast_stream_slow_extraction_blocks_next_turn(rig):
    """T-C03 / F-C06 — 스트림이 먼저 끝나도 추출 완료 전에는 READY·다음 턴 없음.

    또한 그 사이 새 선언(_process_actions)은 새 트랜잭션을 열지 못한다(T-C24).
    """
    r = rig
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: "stream_done" in r.ev["order"])
    await asyncio.sleep(0.02)
    assert "ready" not in r.ev["order"] and r.rec["start_round"] == 0
    tx = tt.get_active_transaction(r.sess)
    assert tx.preparation.phase == tp.PREP_PREPARING
    assert r.sess.is_processing is True, "준비 중 처리 잠금이 풀렸습니다"

    # T-C24 — 준비 중 다음 선언은 새 자동 턴을 열지 못한다.
    before_id = tx.transaction_id
    await r.gm._process_actions(r.sess, "다른 행동", r.master)
    assert tt.get_active_transaction(r.sess).transaction_id == before_id
    assert tx.interaction_inputs == [], "준비 중 선언이 현재 시도에 흡수되었습니다"

    ext_gate.set()
    await owner
    assert r.ev["order"].index("ready") < r.ev["order"].index("start_round")
    assert r.sess.is_processing is False


async def test_tc04_slow_stream_fast_extraction_waits_delivery(rig):
    """T-C04 / F-C05 — 추출이 먼저 끝나도 전달 완료 전에는 READY 없음."""
    r = rig
    r.ev["stream_gate"] = asyncio.Event()
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: r.prov.started.get("extraction", 0) >= 1)
    await asyncio.sleep(0.05)
    tx = tt.get_active_transaction(r.sess)
    assert tx.preparation.tasks["extraction"].terminal
    assert "ready" not in r.ev["order"]
    r.ev["stream_gate"].set()
    await owner
    assert "ready" in r.ev["order"]


# ══════════════════════════════════════════════════════════════
# T-C05~T-C07 / T-C10 / S8 / S11 — 정본 불변 at READY + READY 이후 적용
# ══════════════════════════════════════════════════════════════

async def test_tc05_canonical_unchanged_at_ready_and_applied_after(rig, monkeypatch):
    """T-C05/06/07/10 — READY 전이 순간 커밋 소유 도메인은 턴 시작 값 그대로다.

    지시효과(info_ledger)·추출(상태이상·위치·동행·만난 NPC)·로그·카운터 모두
    READY 이후 legacy continuation에서만 반영된다.
    """
    r = rig
    s = r.sess
    s.statuses = {"테스터": []}
    s.resources = {"테스터": {"물통": 2}}
    tx = tt.get_or_begin_turn_transaction(s, "숲길로 간다")
    tp.stage_instruction_effects(
        s, {"info_access": {"new_secrets": [{"info": "숲의 비밀", "known_by": ["테스터"]}]}},
        transaction_id=tx.transaction_id)
    r.prov.routes["extraction"] = [_json({
        "status_scores": [{"target": "테스터", "status": "탈진", "score": 95}],
        "location": {"name": "숲길"},
        "npcs_met": ["김노인"],
    })]
    start = _canon(s)

    await _run_turn(r, tx=tx)

    at_ready = r.rec["ready_snapshots"][0]
    assert at_ready["status"] == tt.TurnStatus.READY_TO_COMMIT
    assert at_ready["canon"] == start, "READY 시점에 커밋 소유 정본이 이미 바뀌었습니다"
    proof = tx.preparation.ready_proof
    assert proof.canonical_unchanged and proof.changed_domains == ()

    # READY 이후 legacy continuation 결과(현행 성공 의미 보존, T-C10).
    assert s.turn_count == start["turn_count"] + 1
    assert s.gm_turns_done == 1 and len(s.raw_logs) == start["raw_logs_len"] + 2
    assert any(i.get("info") == "숲의 비밀" for i in s.info_ledger)
    assert s.world_timeline.get("current_location") == "숲길"
    assert tx.status == tt.TurnStatus.COMMITTED            # WP-01 legacy finalize
    assert tt.get_active_transaction(s) is None
    assert r.rec["deduct"], "READY 이후 Settlement 청구가 실행되지 않았습니다"
    assert r.rec["legacy_deduct"] == [], "legacy deduct_ink가 호출됐습니다"
    assert tx.preparation.phase == tp.PREP_CONTINUED


async def test_tc05b_input_logs_added_during_wait_are_preserved(rig):
    """현행 의미 보존 — 소비한 선언 로그만 제거하고, 대기 중 추가된 입력은 남긴다."""
    r = rig
    s = r.sess
    s.current_turn_logs = ["[테스터]: 숲길로 간다"]
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: "stream_done" in r.ev["order"])
    s.current_turn_logs.append("[진행자]: 늦게 온 메모")
    ext_gate.set()
    await owner
    assert s.current_turn_logs == ["[진행자]: 늦게 온 메모"]


# ══════════════════════════════════════════════════════════════
# T-C08 / F-C09 / S4 — 비정규 NPC 등록·승격(중첩 자식) 스테이징
# ══════════════════════════════════════════════════════════════

async def test_tc08_irregular_npc_and_nested_promotion_staged(rig):
    """T-C08/F-C09/T-C15 — 배정은 스트림 전 스테이징, 승격 세부는 자식 작업.

    · READY 시점 irregular_npcs/npcs 정본 불변.
    · 스트림 중 화자 이미지 투영은 이번 배정을 본다(출력 의미 보존).
    · 자식(NPC 세부) provider CostEvent도 동결 멤버십에 포함된다.
    """
    r = rig
    s = r.sess
    s.scenario_data["irregular_images"] = ["사내1"]
    os.makedirs(f"media/{s.scenario_id}", exist_ok=True)
    open(f"media/{s.scenario_id}/사내1.png", "wb").write(b"x")
    s.irregular_npcs = {"떠돌이 상인": {"image_key": "", "voice": "Charon",
                                    "gender": "male", "age": "adult", "context": "",
                                    "first_turn": 3, "seen_turns": [3, 5],
                                    "appearances": 2}}
    text = "「낡은 사내」가 고개를 든다. 「떠돌이 상인」이 웃는다."
    r.prov.routes["narration"] = [FakeGenAIResponse(text, usage=_usage())]
    r.prov.routes["irregular"] = [_json({"npcs": [
        {"name": "낡은 사내", "image_key": "사내1", "gender": "male", "age": "old"}]})]
    r.prov.routes["npc_detail"] = [_json({"name": "장돌뱅이 박씨", "details": "상인",
                                          "role": "행상", "attitude": "우호"})]
    seen_image = {}
    import core.irregular_npc as irr

    r.ev["stream_gate"] = asyncio.Event()
    start = _canon(s)
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: "stream_start" in r.ev["order"])
    seen_image["path"] = irr.image_path_for(s, "낡은 사내")
    seen_image["canon"] = dict(s.irregular_npcs)
    r.ev["stream_gate"].set()
    tx = await owner

    assert seen_image["path"] and seen_image["path"].endswith("사내1.png"), (
        "스트림 중 이번 배정 이미지가 투영되지 않았습니다")
    assert "낡은 사내" not in seen_image["canon"]
    at_ready = r.rec["ready_snapshots"][0]["canon"]
    assert at_ready["irregular_npcs"] == start["irregular_npcs"]
    assert at_ready["npcs"] == start["npcs"]
    prep = tx.preparation
    assert "npc_promotion:떠돌이 상인" in prep.tasks
    assert prep.tasks["npc_promotion:떠돌이 상인"].parent == "irregular_npc"
    # READY 이후 적용
    assert "낡은 사내" in s.irregular_npcs
    assert "장돌뱅이 박씨" in s.npcs and "떠돌이 상인" not in s.irregular_npcs
    ops = {e["operation"] for e in _events(r, tx) if e["event_id"] in prep.frozen_cost_event_ids}
    assert {"TURN_IRREGULAR_NPC", "TURN_NPC_PROFILE", "TURN_NARRATION",
            "TURN_EXTRACTION"} <= ops


# ══════════════════════════════════════════════════════════════
# T-C09 / F-C10 — 자동 재계획 스테이징
# ══════════════════════════════════════════════════════════════

async def test_tc09_auto_replan_joined_and_applied_after_ready(rig):
    """T-C09/F-C10 — 트리거된 재계획은 READY 전 합류, 정본 교체는 READY 이후."""
    r = rig
    s = r.sess
    s.narrative_mode = "free"
    s.narrative_plan = {"current_event": {"title": "이전"}, "next_event": {},
                        "mid_plan": {}, "plan_version": 3, "last_planned_turn": 2}
    r.prov.routes["planner"] = [_json({
        "mid_plan": {"title": "m", "overview": "o", "milestones": ["a"], "end_condition": "e"},
        "current_event": {"title": "새 사건", "summary": "s", "resolution_direction": "r",
                          "progress": ""},
        "next_event": {"title": "n", "summary": "s", "trigger": "t"}})]
    captured = {}

    def _planner(kwargs):
        captured["prompt"] = kwargs["contents"][0].parts[0].text
        return r.prov.routes["planner_resp"][0]
    r.prov.routes["planner_resp"] = r.prov.routes["planner"]
    r.prov.routes["planner"] = _planner
    start = _canon(s)
    tx = await _run_turn(r, event_assessment="completed")

    assert r.rec["ready_snapshots"][0]["canon"]["narrative_plan"] == start["narrative_plan"]
    assert tx.preparation.tasks["narrative_replan"].state == tp.TASK_SUCCEEDED
    assert s.narrative_plan["current_event"]["title"] == "새 사건"
    assert s.narrative_plan["plan_version"] == 4
    assert s.narrative_plan["last_planned_turn"] == s.turn_count
    # 입력 의미 보존: 계획 프롬프트가 이번 턴 묘사와 진행 턴 N을 본다.
    assert "숲길로 조용히 걸어간다." in captured["prompt"]
    assert f"진행 턴: {start['turn_count'] + 1}" in captured["prompt"]
    ops = {e["operation"] for e in _events(r, tx)
           if e["event_id"] in tx.preparation.frozen_cost_event_ids}
    assert "TURN_NARRATIVE_PLANNING" in ops


# ══════════════════════════════════════════════════════════════
# T-C11 / T-C27 — 추출 provider 재시도는 같은 트랜잭션
# ══════════════════════════════════════════════════════════════

async def test_tc11_extraction_provider_retry_same_transaction(rig):
    """T-C11/T-C27 — 추출 provider 재시도는 새 논리 시도가 아니며, usage가 있는
    실제 시도만 CostEvent가 된다(실패 시도 사용량 날조 없음)."""
    r = rig
    r.prov.routes["extraction"] = [RuntimeError("일시 오류"), _json({"situation": {}})]
    tx = await _run_turn(r)
    assert "ready" in r.ev["order"]
    ext = [e for e in _events(r, tx) if e["operation"] == "TURN_EXTRACTION"]
    assert len(ext) == 1 and ext[0]["provider_attempt"] == 2
    assert ext[0]["turn_attempt"] == tx.attempt == 1
    assert ext[0]["event_id"] in tx.preparation.frozen_cost_event_ids


# ══════════════════════════════════════════════════════════════
# T-C12 / F-C04 — 추출 소진 실패 → READY 없음, 같은 tx 재시도로 재개
# ══════════════════════════════════════════════════════════════

async def test_tc12_exhausted_extraction_no_ready_no_charge_then_retry(rig, monkeypatch):
    r = rig
    s = r.sess
    r.prov.routes["extraction"] = [RuntimeError("실패")]
    start = _canon(s)
    tx = await _run_turn(r)

    assert "ready" not in r.ev["order"] and r.rec["start_round"] == 0
    assert r.rec["deduct"] == []
    assert _canon(s) == start, "사전 READY 실패인데 정본이 전진했습니다"
    assert s.extraction_pending is True
    assert tx.preparation.phase == tp.PREP_RETRY_PENDING
    assert tt.get_active_transaction(s) is tx and not tt.is_terminal(tx.status)
    assert s.is_processing is False
    assert tx.preparation.cost_membership == tp.COST_OPEN
    assert any(getattr(m, "view", None) is not None for m in r.gch.sent), "재시도 버튼 없음"

    # 같은 논리 시도·같은 준비 owner로 재개(새 자동 턴 아님).
    r.prov.routes["extraction"] = [_json({"location": {"name": "숲길"}})]
    outcome = await r.gm._retry_prepared_extraction(s)
    assert outcome == "ready"
    assert tx.status == tt.TurnStatus.COMMITTED and tx.preparation.ready_proof is not None
    assert s.turn_count == start["turn_count"] + 1 and s.extraction_pending is False
    assert r.rec["start_round"] == 1 and len(r.rec["deduct"]) == 1


async def test_tc12b_retry_without_live_preparation_only_releases(rig):
    """재시작 등으로 런타임 준비가 소실되면 미확정 묘사에 추출을 적용하지 않는다."""
    r = rig
    s = r.sess
    s.extraction_pending = True
    s.extraction_retry_ctx = {"text": "x", "transaction_id": "gone", "mode": "wp_c_preparation"}
    start = _canon(s)
    assert await r.gm._retry_prepared_extraction(s) == "released"
    assert s.extraction_pending is False and _canon(s) == start


# ══════════════════════════════════════════════════════════════
# T-C13 / F-C11 / T-C25 — stale / 정체성
# ══════════════════════════════════════════════════════════════

async def test_tc13_stale_extraction_cannot_satisfy_barrier(rig):
    """T-C13/F-C11 — 추출 중 더 새로운 시도가 열리면 그 결과는 배리어를 만족 못한다."""
    r = rig
    s = r.sess

    def _ext(kwargs):
        return _json({"location": {"name": "숲길"}})
    r.prov.routes["extraction"] = _ext
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: r.prov.started.get("extraction", 0) >= 1)
    old = tt.get_active_transaction(s)
    newer = tt.begin_attempt(s, logical_turn=old.logical_turn, player_declaration="재요청")
    ext_gate.set()
    await owner
    assert old.preparation.extraction_plan is None
    assert old.status != tt.TurnStatus.READY_TO_COMMIT
    assert newer.status == tt.TurnStatus.CREATED and newer.preparation is None
    assert s.world_timeline.get("current_location") == "마을"
    assert s.extraction_pending is False, "대체된 시도가 세션 차단 상태를 세웠습니다"
    assert r.rec["start_round"] == 0 and r.rec["deduct"] == []
    assert tt.get_active_transaction(s) is newer


def test_tc25_ready_requires_exact_identity(session_auto_ready):
    """T-C25 — 다른 tx/attempt가 현재이면 이전 준비는 READY로 전이할 수 없다."""
    s = session_auto_ready
    a = tt.begin_turn_transaction(s, "a")
    prep = tp.ensure_preparation(s, a.transaction_id)
    b = tt.begin_attempt(s, logical_turn=a.logical_turn, player_declaration="b")
    proof, reasons = tp.transition_to_ready(s, prep)
    assert proof is None and "identity:not_current" in reasons
    assert b.status != tt.TurnStatus.READY_TO_COMMIT


# ══════════════════════════════════════════════════════════════
# T-C14~T-C18 / F-C12 — 레지스트리·비용 동결 단위 계약
# ══════════════════════════════════════════════════════════════

async def test_tc14_to_tc18_registry_and_cost_closure(session_auto_ready, tmp_path):
    s = session_auto_ready
    tx = tt.begin_turn_transaction(s, "선언")
    prep = tp.ensure_preparation(s, tx.transaction_id)
    ledger = CostLedger(str(tmp_path / "l.jsonl"))

    class _Bot:
        cost_ledger = ledger
    parent_gate, child_gate = asyncio.Event(), asyncio.Event()

    async def _child():
        op = core.cost_ledger.begin_operation(_Bot, "TURN_NPC_PROFILE", session=s)
        prep.claim_cost_operation(op)
        await child_gate.wait()
        op.mark_attempt()
        op.record(cost_krw=1.0)
        return True

    async def _parent():
        op = core.cost_ledger.begin_operation(_Bot, "TURN_IRREGULAR_NPC", session=s)
        prep.claim_cost_operation(op)
        op.mark_attempt()
        op.record(cost_krw=2.0)
        prep.register_task("child", tp.TASK_NPC_PROMOTION, coro=_child(), parent="parent")
        await parent_gate.wait()
        return 1

    prep.register_task("parent", tp.TASK_IRREGULAR_NPC, coro=_parent(),
                       may_spawn_required_child_work=True)
    await asyncio.sleep(0)
    # T-C14 — 필수 작업 실행 중이면 READY 술어 거짓.
    assert any(x.startswith("task:running") for x in tp.evaluate_ready(s, prep))
    # T-C16 — 비용 관련 작업이 열려 있으면 동결 불가.
    with pytest.raises(tp.BarrierViolationError):
        prep.close_cost_membership()
    parent_gate.set()
    joiner = asyncio.create_task(prep.join())
    await asyncio.sleep(0.01)
    # T-C15 — 부모가 끝나도 자식이 terminal이 아니면 합류가 끝나지 않는다.
    assert prep.tasks["parent"].terminal and not prep.tasks["child"].terminal
    assert not joiner.done()
    child_gate.set()
    await joiner
    prep.seal()
    frozen = prep.close_cost_membership()
    # T-C17 — 정확한 멤버십: claim된 오퍼레이션이 실제 append한 event_id 전부.
    on_disk = {e["event_id"] for e in ledger.list_cost_events(transaction_id=tx.transaction_id)}
    assert set(frozen) == on_disk and len(frozen) == 2
    # F-C12 — 봉인 후 필수 작업 등록은 명시적 위반.
    with pytest.raises(tp.BarrierViolationError):
        prep.register_task("late", tp.TASK_EXTRACTION, coro=asyncio.sleep(0))
    # T-C18 — 동결 이후 필수 provider 오퍼레이션은 시작 자체가 거부된다.
    late = core.cost_ledger.begin_operation(_Bot, "TURN_EXTRACTION", session=s)
    with pytest.raises(tp.BarrierViolationError):
        tp.claim_cost_operation(s, tx.transaction_id, late)


async def test_tc18b_real_extraction_after_freeze_adds_no_member(rig):
    """T-C18 — READY 이후 같은 준비 객체로 추출을 다시 돌리면 provider 호출 전에 거부된다."""
    r = rig
    tx = await _run_turn(r)
    prep = tx.preparation
    frozen = prep.frozen_cost_event_ids
    before = len(r.ledger.list_cost_events())
    calls_before = r.prov.attempt_count
    with pytest.raises(tp.BarrierViolationError):
        await r.gm._run_extraction(r.sess, "x", r.master, transaction_id=tx.transaction_id,
                                   logical_turn=tx.logical_turn, attempt=tx.attempt,
                                   preparation=prep)
    assert len(r.ledger.list_cost_events()) == before
    assert r.prov.attempt_count == calls_before
    assert prep.frozen_cost_event_ids == frozen


# ══════════════════════════════════════════════════════════════
# T-C17 / T-C19 / T-C21 — scope-aware 멤버십(배경 압축·수동·로컬 미디어 제외)
# ══════════════════════════════════════════════════════════════

async def test_tc17_tc19_membership_excludes_background_manual_and_media(rig, monkeypatch):
    """T-C17/T-C19/T-C21 — 동결 ID = 이번 시도의 자동 턴 provider 이벤트 정확히.

    · 자동 압축(SESSION_BACKGROUND)은 READY를 지연시키지 않고 멤버도 아니다.
    · 같은 tx가 활성인 동안의 수동 지시 호출(transaction_id=None)은 claim되지 않는다.
    · 로컬 이미지 전송은 CostEvent를 만들지 않는다(출력 ID만).
    """
    r = rig
    s = r.sess
    s.uncompressed_logs = ["[과거] 로그"] * 4
    monkeypatch.setattr(core.memory_plan, "should_compress", lambda sess: True)
    comp_gate = threading.Event()
    r.prov.gates["compression"] = comp_gate
    r.prov.routes["compression"] = [FakeGenAIResponse("압축 요약", usage=_usage())]
    s.scenario_data["media_keywords"] = {"숲길": "forest.png"}
    os.makedirs(f"media/{s.scenario_id}", exist_ok=True)
    open(f"media/{s.scenario_id}/forest.png", "wb").write(b"x")

    tx = tt.get_or_begin_turn_transaction(s, "숲길로 간다")
    # 활성 tx 동안의 수동 지시층위 호출(transaction_id 없음) — 멤버가 아니어야 한다.
    r.prov.routes["narration"] = [_json({"proceed_instruction": "x", "reasoning": "r"}),
                                  FakeGenAIResponse("숲길로 조용히 걸어간다.", usage=_usage())]
    await r.gm._call_gm_logic(s, "", [], r.master)
    await r.gm._finish_proceed_and_continue(
        s, "상:숲길 숲길로 이동한다", r.master, event_assessment="ongoing",
        transaction_id=tx.transaction_id)

    assert "ready" in r.ev["order"], "배경 압축이 READY를 막았습니다"
    assert r.prov.started.get("compression") == 1 and not comp_gate.is_set()
    frozen = set(tx.preparation.frozen_cost_event_ids)
    member_ops = sorted(e["operation"] for e in r.ledger.list_cost_events()
                        if e["event_id"] in frozen)
    assert member_ops == ["TURN_EXTRACTION", "TURN_NARRATION"]
    manual = [e for e in r.ledger.list_cost_events() if e["operation"] == "TURN_INSTRUCTION"]
    assert manual and manual[0]["event_id"] not in frozen
    assert tx.media_message_ids, "로컬 미디어 출력 ID가 추적되지 않았습니다"
    comp_gate.set()
    await _until(lambda: any(e["operation"] == "MEMORY_AUTO_COMPRESSION"
                             for e in r.ledger.list_cost_events()))
    comp = [e for e in r.ledger.list_cost_events()
            if e["operation"] == "MEMORY_AUTO_COMPRESSION"][0]
    assert comp["transaction_id"] is None and comp["event_id"] not in frozen


# ══════════════════════════════════════════════════════════════
# T-C20 — TTS 분류(자동 경로는 런타임 TTS provider 호출 없음)
# ══════════════════════════════════════════════════════════════

async def test_tc20_automatic_path_invokes_no_runtime_tts(rig, monkeypatch):
    """T-C20 — 현재 소스: 자동 경로(cost_log_prefix 있음)는 TTS 토글이 켜져 있어도
    런타임 TTS를 합성하지 않는다 → READY/비용 멤버십에 TTS 의존 없음."""
    r = rig
    r.sess.tts_enabled = True
    called = []

    async def _tts(*a, **k):
        called.append(1)
        return b"", 0.0, 0, 0
    monkeypatch.setattr(core, "synthesize_tts_pcm", _tts)
    tx = await _run_turn(r)
    assert called == [] and "ready" in r.ev["order"]
    assert not any(e["operation"].startswith("TTS") for e in _events(r))


# ══════════════════════════════════════════════════════════════
# T-C22 / T-C23 / F-C02 / F-C03 — 전달 실패
# ══════════════════════════════════════════════════════════════

async def test_tc22_partial_delivery_failure_not_ready(rig):
    """T-C22/F-C03 — 부분 전달 후 실패: READY 없음, 청구 없음, ID 보존, 정리 멱등."""
    r = rig
    s = r.sess
    r.prov.routes["narration"] = [FakeGenAIResponse("첫 문단.\n\n둘째 문단.", usage=_usage())]
    r.ev["stream_fail_at"] = 2
    start = _canon(s)
    tx = await _run_turn(r)
    assert "ready" not in r.ev["order"] and r.rec["deduct"] == []
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.failure_code == tt.FailureCode.MESSAGE_DELIVERY_FAILURE.value
    assert len(tx.canonical_message_ids) == 1, "부분 전달 ID가 보존되지 않았습니다"
    first = [m for m in r.gch.sent if m.id == tx.canonical_message_ids[0]][0]
    assert first.deleted, "부분 전달 메시지가 정리되지 않았습니다"
    await core.clear_messages([first])              # 멱등
    assert _canon(s) == start
    assert r.rec["start_round"] == 1
    assert tx.preparation.cost_membership == tp.COST_CLOSED


async def test_fc02_delivery_fails_while_extraction_running(rig):
    """F-C02 — 전달이 즉시 실패해도 진행 중 추출은 합류되고 그 CostEvent는 사실로 남는다."""
    r = rig
    r.ev["stream_fail_at"] = 1
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: r.ev["streamed"] >= 1)
    await asyncio.sleep(0.02)
    assert not owner.done(), "추출 합류 없이 실패 처리가 끝났습니다"
    ext_gate.set()
    tx = await owner
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and "ready" not in r.ev["order"]
    ext = [e for e in _events(r, tx) if e["operation"] == "TURN_EXTRACTION"]
    assert ext and ext[0]["event_id"] in tx.preparation.frozen_cost_event_ids
    assert r.sess.world_timeline.get("current_location") == "마을"


async def test_tc23_missing_optional_media_does_not_fail_turn(rig):
    """T-C23 — 선택 로컬 미디어 파일 부재는 현행대로 경고만(전달 성공, READY)."""
    r = rig
    r.sess.scenario_data["media_keywords"] = {"폭포": "none.png"}
    tx = tt.get_or_begin_turn_transaction(r.sess, "선언")
    await r.gm._finish_proceed_and_continue(
        r.sess, "상:폭포 이동", r.master, event_assessment="ongoing",
        transaction_id=tx.transaction_id)
    assert "ready" in r.ev["order"] and tx.media_message_ids == []


# ══════════════════════════════════════════════════════════════
# F-C01 — 생성 실패
# ══════════════════════════════════════════════════════════════

async def test_fc01_generation_failure_launches_nothing(rig):
    r = rig
    r.prov.routes["narration"] = [RuntimeError("생성 실패")]
    start = _canon(r.sess)
    tx = await _run_turn(r)
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.preparation.tasks == {}, "확정 묘사 없이 준비 작업이 발사되었습니다"
    assert r.prov.started.get("extraction") is None
    assert _canon(r.sess) == start and r.rec["deduct"] == []
    assert r.sess.is_processing is False and r.rec["start_round"] == 1


# ══════════════════════════════════════════════════════════════
# B1 — ROLL 성장 스테이징 (사용자 결정 2026-09-25)
# ══════════════════════════════════════════════════════════════

def _force_fail_growth(monkeypatch, sess):
    import cogs.gm as gm
    import core.growth as growth
    sess.scenario_data["growth"] = {"fail_threshold": 1}
    sess.scenario_data["luck"] = {"enabled": False}
    monkeypatch.setattr(gm.random, "randint", lambda a, b: b)
    monkeypatch.setattr(growth.random, "randint", lambda a, b: b)


async def test_b1_roll_growth_staged_projected_and_applied_after_ready(rig, monkeypatch):
    """B1 — 판정 결과는 공개 입력 사실로 남고, 성장(profile +1·stat_fail_counts)은
    스테이징 → 같은 tx 후속 ROLL은 투영을 읽음 → READY 이후 적용."""
    r = rig
    s = r.sess
    _force_fail_growth(monkeypatch, s)
    tx = tt.get_or_begin_turn_transaction(s, "문을 부순다")
    spec = [{"char_name": "테스터", "stat": "기술", "sides": 20, "weight": 0}]
    res1 = await r.gm._execute_rolls(s, spec, r.gch, transaction_id=tx.transaction_id)
    assert any("성장" in x and "9" in x for x in res1)
    assert s.players["555001"]["profile"]["기술"] == 8, "성장이 정본에 선적용되었습니다"
    assert not getattr(s, "stat_fail_counts", None)
    res2 = await r.gm._execute_rolls(s, spec, r.gch, transaction_id=tx.transaction_id)
    assert any("기준치 9" in x for x in res2), "후속 ROLL이 스테이징 투영을 읽지 않았습니다"
    assert s.players["555001"]["profile"]["기술"] == 8

    await r.gm._finish_proceed_and_continue(
        s, "지시", r.master, event_assessment="ongoing", transaction_id=tx.transaction_id)
    snap = r.rec["ready_snapshots"][0]["canon"]
    assert snap["players"]["555001"]["profile"]["기술"] == 8
    assert s.players["555001"]["profile"]["기술"] == 10
    assert isinstance(s.stat_fail_counts, dict)


async def test_b1_growth_discarded_on_pre_ready_failure(rig, monkeypatch):
    r = rig
    s = r.sess
    _force_fail_growth(monkeypatch, s)
    tx = tt.get_or_begin_turn_transaction(s, "문을 부순다")
    spec = [{"char_name": "테스터", "stat": "기술", "sides": 20, "weight": 0}]
    await r.gm._execute_rolls(s, spec, r.gch, transaction_id=tx.transaction_id)
    r.prov.routes["narration"] = [RuntimeError("생성 실패")]
    await r.gm._finish_proceed_and_continue(
        s, "지시", r.master, event_assessment="ongoing", transaction_id=tx.transaction_id)
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert s.players["555001"]["profile"]["기술"] == 8
    assert not getattr(s, "stat_fail_counts", None)


# ══════════════════════════════════════════════════════════════
# T-C26 / T-C28 — READY는 COMMITTED 아님 / 인트로·수동 격리
# ══════════════════════════════════════════════════════════════

async def test_tc26_ready_is_not_committed_and_no_wp_d_callers(rig):
    r = rig
    await _run_turn(r)
    assert r.rec["ready_snapshots"][0]["status"] == tt.TurnStatus.READY_TO_COMMIT
    from tests.conftest import source_of
    # WP-D: READY 자체는 여전히 COMMITTED가 아니다. 권위 호출(저널·Settlement·
    #   InkTransaction·strict 저장)은 CommitCoordinator 한 곳에만 있고, 흐름 모듈은
    #   그 owner를 통해서만 커밋한다(이중 권위 금지).
    for f in ("cogs/gm.py", "cogs/game.py", "core/turn_preparation.py"):
        src = source_of(f)
        for forbidden in ("core.commit_journal", "CommitJournal(", ".append_phase(",
                          "build_turn_settlement(", "core.settlement",
                          "core.ink_transactions", "apply_ink_transaction(",
                          "save_session_data_strict("):
            assert forbidden not in src, f"{f}: 커밋 owner 밖의 권위 호출 {forbidden}"
    tp_src = source_of("core/turn_preparation.py")
    assert "TurnStatus.COMMITTED" not in tp_src
    assert "core.commit_coordinator.CommitCoordinator(" in source_of("cogs/gm.py")


async def test_tc28_intro_manual_execute_proceed_has_no_barrier(rig):
    """T-C28 — 준비 객체 없이(인트로·수동) 공유 셸은 기존 즉시 적용 의미 그대로이며
    자동 준비/READY를 만들지 않는다."""
    r = rig
    s = r.sess
    before = s.turn_count
    res = await r.game._execute_proceed(s, "지시")
    assert res["ok"] and s.turn_count == before + 1 and len(s.raw_logs) == 2
    assert tt.get_active_transaction(s) is None
    assert r.prov.started.get("extraction") is None
    assert s.is_processing is False


# ══════════════════════════════════════════════════════════════
# 재시도 버튼 라우팅 — 표식 컨텍스트는 같은 tx 준비 owner로 재개
# ══════════════════════════════════════════════════════════════

async def test_retry_button_resumes_same_transaction(rig, monkeypatch):
    """추출 재시도 버튼(persistent view)이 새 자동 턴이 아니라 같은 시도를 재개한다."""
    import cogs.gm as gm_mod
    from tests.fakes.discord_fakes import FakeInteraction

    r = rig
    s = r.sess
    r.prov.routes["extraction"] = [RuntimeError("실패")]
    tx = await _run_turn(r)
    assert s.extraction_retry_ctx.get("mode") == "wp_c_preparation"
    assert s.extraction_retry_ctx.get("transaction_id") == tx.transaction_id

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(core.display, "close_notice", _noop)
    btn_msg = [m for m in r.gch.sent if getattr(m, "view", None) is not None][-1]
    r.prov.routes["extraction"] = [_json({"situation": {}})]
    view = gm_mod.ExtractionRetryView(r.bot)
    inter = FakeInteraction(channel=r.gch, message=btn_msg)
    await view.retry.callback(inter)

    assert tx.status == tt.TurnStatus.COMMITTED and tx.preparation.ready_proof is not None
    assert tt.get_active_transaction(s) is None
    assert s.extraction_pending is False and r.rec["start_round"] == 1


# ══════════════════════════════════════════════════════════════
# GPT 게이트 패치 (2026-09-25) — B-C1 ~ B-C4
# ══════════════════════════════════════════════════════════════

def _capture_ready_reasons(monkeypatch):
    seen = []
    orig = tp.evaluate_ready

    def _ev(session, prep):
        r = orig(session, prep)
        seen.append(list(r))
        return r
    monkeypatch.setattr(tp, "evaluate_ready", _ev)
    return seen


# ── B-C1 — 정본 변화는 READY를 막는다 ─────────────────────────

@pytest.mark.parametrize("tamper,domain", [
    (lambda s: s.resources.setdefault("테스터", {}).update({"물통": 99}), "resources"),
    (lambda s: setattr(s, "quest_state", {"active": {"id": "q_x", "name": "외부", "node": "root"},
                                          "cleared": [], "known_secrets": [],
                                          "occurrences": {}}), "quest_state"),
    (lambda s: setattr(s, "narrative_plan", {"current_event": {"title": "외부 변조"}}),
     "narrative_plan"),
])
async def test_bc1_canonical_change_during_preparation_blocks_ready(
        rig, monkeypatch, tamper, domain):
    """B-C1 — 준비 중 커밋 소유 정본이 바뀌면(관리자/수동 쓰기 포함) READY 거부."""
    r = rig
    s = r.sess
    s.resources = {"테스터": {"물통": 2}}
    reasons = _capture_ready_reasons(monkeypatch)
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    tx = tt.get_or_begin_turn_transaction(s, "선언")
    owner = asyncio.create_task(_run_turn(r, tx=tx))
    await _until(lambda: r.prov.started.get("extraction", 0) >= 1)
    tamper(s)                                   # 외부 정본 쓰기
    ext_gate.set()
    await owner
    assert "ready" not in r.ev["order"], "정본 변화가 있는데 READY에 도달했습니다"
    assert any(f"state:canonical_changed:" in x and domain in x
               for rs in reasons for x in rs), reasons
    assert tx.preparation.ready_proof is None
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert tx.failure_code == tt.FailureCode.COMMIT_VALIDATION_FAILURE.value
    assert r.rec["deduct"] == [] and s.turn_count == 7


async def test_bc1_normal_path_canonical_unchanged_and_ready(rig):
    r = rig
    tx = await _run_turn(r)
    proof = tx.preparation.ready_proof
    assert proof is not None and proof.canonical_unchanged is True
    assert proof.changed_domains == () and "ready" in r.ev["order"]


# ── B-C2 — post-ROLL / forced-PROCEED 지시효과 스테이징 ─────────

def _quest_rig(r, monkeypatch):
    from tests.defects.test_instruction_side_effects import QUEST_NEW, _install_quest
    s = r.sess
    s.quest_state = {"active": None, "cleared": [], "known_secrets": [], "occurrences": {}}
    s._quest_offered = ["q_new"]
    _install_quest(monkeypatch, s.scenario_id, QUEST_NEW)
    return s


def _secret(name):
    return {"new_secrets": [{"info": name, "known_by": ["테스터"]}]}


async def test_bc2_post_roll_decision_staged_composed_and_applied_once(rig, monkeypatch):
    """B-C2 — 굴림 후 지시 결정의 효과가 스테이징(이전 스테이징과 합성)되고
    READY 전 정본 불변, READY 후 정확히 한 번 적용된다."""
    r = rig
    s = _quest_rig(r, monkeypatch)
    tx = tt.get_or_begin_turn_transaction(s, "문을 부순다")
    # 같은 tx의 이전 결정(NARRATE 반복 등) — 퀘스트 선택 + 비밀 A
    tp.stage_instruction_effects(
        s, {"quest_choice": {"id": "q_new", "reason": "앞선 결정"}, "info_access": _secret("비밀 A")},
        transaction_id=tx.transaction_id)

    async def _logic(session, *a, **k):
        return {"action": "PROCEED", "proceed_instruction": "문이 부서진다",
                "reasoning": "r", "info_access": _secret("비밀 B")}
    monkeypatch.setattr(r.gm, "_call_gm_logic", _logic)
    applied = []
    orig_apply = tp.apply_instruction_effects

    def _apply(session, pending):
        res = orig_apply(session, pending)
        applied.append(res["applied"])
        return res
    monkeypatch.setattr(tp, "apply_instruction_effects", _apply)

    await r.gm._continue_with_roll_results(s, "문을 부순다", ["- 굴림"],
                                           transaction_id=tx.transaction_id)

    snap = r.rec["ready_snapshots"][0]["canon"]
    assert snap["info_ledger"] == [] and snap["quest_state"]["active"] is None
    infos = {i["info"] for i in s.info_ledger}
    assert {"비밀 A", "비밀 B"} <= infos, "합성 중 앞선 효과가 유실되었습니다"
    assert s.quest_state["active"]["id"] == "q_new", "앞선 퀘스트 선택이 덮어쓰기로 유실되었습니다"
    assert applied == [True], "지시효과가 정확히 한 번 적용되지 않았습니다"
    assert tx.instruction_result.applied is True


async def test_bc2_forced_proceed_decision_staged_and_applied_after_ready(rig, monkeypatch):
    r = rig
    s = _quest_rig(r, monkeypatch)
    tx = tt.get_or_begin_turn_transaction(s, "선언")

    async def _logic(session, *a, **k):
        return {"action": "PROCEED", "proceed_instruction": "강제 진행",
                "quest_choice": {"id": "q_new", "reason": "강제"},
                "info_access": _secret("강제 비밀")}
    monkeypatch.setattr(r.gm, "_call_gm_logic", _logic)
    instr = await r.gm._forced_proceed_instruction(
        s, "선언", [], r.master, transaction_id=tx.transaction_id)
    assert tx.instruction_result is not None and tx.instruction_result.info_ledger
    assert s.info_ledger == [] and s.quest_state["active"] is None
    await r.gm._finish_proceed_and_continue(s, instr, r.master,
                                            transaction_id=tx.transaction_id)
    snap = r.rec["ready_snapshots"][0]["canon"]
    assert snap["info_ledger"] == []
    assert any(i["info"] == "강제 비밀" for i in s.info_ledger)
    assert s.quest_state["active"]["id"] == "q_new"


async def test_bc2_staged_decision_discarded_on_pre_ready_failure(rig, monkeypatch):
    r = rig
    s = _quest_rig(r, monkeypatch)
    tx = tt.get_or_begin_turn_transaction(s, "선언")

    async def _logic(session, *a, **k):
        return {"action": "PROCEED", "proceed_instruction": "진행",
                "quest_choice": {"id": "q_new", "reason": "r"}, "info_access": _secret("X")}
    monkeypatch.setattr(r.gm, "_call_gm_logic", _logic)
    r.prov.routes["narration"] = [RuntimeError("생성 실패")]
    await r.gm._continue_with_roll_results(s, "선언", ["- 굴림"],
                                           transaction_id=tx.transaction_id)
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM
    assert s.info_ledger == [] and s.quest_state["active"] is None
    assert tx.instruction_result.applied is False


def test_bc2_composition_rule_unit(session_with_quest, monkeypatch):
    """합성 규칙 고정: 후행 결정이 무변경이면 앞선 quest/info/안내가 유지된다."""
    from tests.defects.test_instruction_side_effects import QUEST_NEW, _install_quest
    s = session_with_quest
    s.quest_state["active"] = None
    s._quest_offered = ["q_new"]
    _install_quest(monkeypatch, s.scenario_id, QUEST_NEW)
    tx = tt.begin_turn_transaction(s, "d")
    p1 = tp.stage_instruction_effects(
        s, {"quest_choice": {"id": "q_new", "reason": "1"}, "info_access": _secret("A")},
        transaction_id=tx.transaction_id)
    p2 = tp.stage_instruction_effects(s, {"info_access": _secret("B")},
                                      transaction_id=tx.transaction_id)
    assert tx.instruction_result is p2 and p1 is not p2
    assert p2.projected_quest_state["active"]["id"] == "q_new"
    assert p2.quest_action == "start" and p2.quest_active_name
    assert [i["info"] for i in p2.info_ledger] == ["A", "B"]
    assert s.quest_state["active"] is None and s.info_ledger == []


# ── B-C3 — 수치 재계획은 이번 턴 추출 계획이 권위 ─────────────

def _free_plan(s):
    s.narrative_mode = "free"
    s.narrative_plan = {"current_event": {"title": "이전"}, "next_event": {},
                        "mid_plan": {}, "plan_version": 3, "last_planned_turn": 2}


def _planner_ok(r):
    r.prov.routes["planner"] = [_json({
        "mid_plan": {"title": "m", "overview": "o", "milestones": ["a"], "end_condition": "e"},
        "current_event": {"title": "수치 재계획", "summary": "s", "resolution_direction": "r",
                          "progress": ""},
        "next_event": {"title": "n", "summary": "s", "trigger": "t"}})]


async def test_bc3_current_plan_value_is_trigger_authority(rig):
    """직전 last_extraction은 낮고 이번 계획이 임계 이상 → 같은 턴 재계획(1턴 지연 없음)."""
    r = rig
    s = r.sess
    _free_plan(s)
    _planner_ok(r)
    s.last_extraction = {"quest_progress": {"advance": 0, "deviation": 0}}
    r.prov.routes["extraction"] = [_json({"quest_progress": {"advance": 90, "deviation": 0}})]
    tx = await _run_turn(r, event_assessment="ongoing")
    prep = tx.preparation
    assert prep.tasks["narrative_replan"].parent == "extraction"
    assert r.rec["ready_snapshots"][0]["canon"]["narrative_plan"]["current_event"]["title"] == "이전"
    assert s.narrative_plan["current_event"]["title"] == "수치 재계획"
    assert s.narrative_plan["last_planned_turn"] == s.turn_count
    ops = [e["operation"] for e in _events(r, tx) if e["event_id"] in prep.frozen_cost_event_ids]
    assert "TURN_NARRATIVE_PLANNING" in ops


async def test_bc3_stale_prior_extraction_does_not_trigger(rig):
    """직전 last_extraction이 임계 이상이어도 이번 계획이 낮으면 재계획하지 않는다."""
    r = rig
    s = r.sess
    _free_plan(s)
    _planner_ok(r)
    s.last_extraction = {"quest_progress": {"advance": 95, "deviation": 95}}
    r.prov.routes["extraction"] = [_json({"quest_progress": {"advance": 0, "deviation": 0}})]
    tx = await _run_turn(r, event_assessment="ongoing")
    assert "narrative_replan" not in tx.preparation.tasks
    assert r.prov.started.get("planner") is None
    assert s.narrative_plan["current_event"]["title"] == "이전"


async def test_bc3_event_assessment_precedence_dedupes(rig):
    """event_assessment(completed) 트리거가 있으면 수치 판정은 생략 — 턴당 재계획 1건."""
    r = rig
    s = r.sess
    _free_plan(s)
    _planner_ok(r)
    r.prov.routes["extraction"] = [_json({"quest_progress": {"advance": 99, "deviation": 99}})]
    tx = await _run_turn(r, event_assessment="completed")
    assert r.prov.started.get("planner") == 1
    assert tx.preparation.tasks["narrative_replan"].parent is None   # 사건 트리거 소유
    assert tx.preparation.narrative_marker is False


# ── B-C4 — 래퍼 타임아웃 뒤에도 실제 provider 호출은 종료까지 추적 ─────

def _slow_first_extraction(r, first_gate, *, late_raises=False):
    calls = {"n": 0}

    def _route(kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            assert first_gate.wait(10)
            if late_raises:
                raise RuntimeError("늦게 실패(usage 없음)")
            return _json({"situation": {}}, p=777, c=77)
        return _json({"situation": {}})
    r.prov.routes["extraction"] = _route
    return calls


async def test_bc4_inflight_provider_call_blocks_closure_and_is_observed(rig, monkeypatch):
    r = rig
    monkeypatch.setitem(core.resilience.DEFAULT_TIMEOUTS, "extraction", 0.1)
    gate = threading.Event()
    _slow_first_extraction(r, gate)
    tx = tt.get_or_begin_turn_transaction(r.sess, "선언")
    owner = asyncio.create_task(_run_turn(r, tx=tx))
    try:
        await _until(lambda: r.prov.started.get("extraction", 0) >= 2)   # 래퍼 타임아웃 → 재시도 성공
        await asyncio.sleep(0.1)
        prep = tx.preparation
        assert prep.tasks["extraction"].terminal, "논리 추출 작업은 이미 끝났어야 합니다"
        assert prep.inflight_provider_calls(), "살아 있는 underlying 호출이 추적되지 않습니다"
        assert "ready" not in r.ev["order"] and not owner.done()
        with pytest.raises(tp.BarrierViolationError):
            prep.close_cost_membership()
    finally:
        gate.set()
    await owner
    prep = tx.preparation
    assert "ready" in r.ev["order"]
    ext = [e for e in _events(r, tx) if e["operation"] == "TURN_EXTRACTION"]
    late = [e for e in ext if e["metadata"].get("late_after_wrapper_timeout")]
    assert len(ext) == 2 and len(late) == 1
    assert late[0]["provider_attempt"] == 1 and late[0]["input_tokens"] == 777
    assert {e["event_id"] for e in ext} <= set(prep.frozen_cost_event_ids)


async def test_bc4_late_failure_without_usage_is_not_fabricated(rig, monkeypatch):
    r = rig
    monkeypatch.setitem(core.resilience.DEFAULT_TIMEOUTS, "extraction", 0.1)
    gate = threading.Event()
    _slow_first_extraction(r, gate, late_raises=True)
    tx = tt.get_or_begin_turn_transaction(r.sess, "선언")
    owner = asyncio.create_task(_run_turn(r, tx=tx))
    try:
        await _until(lambda: r.prov.started.get("extraction", 0) >= 2)
        await asyncio.sleep(0.1)
        assert "ready" not in r.ev["order"]
    finally:
        gate.set()
    await owner
    ext = [e for e in _events(r, tx) if e["operation"] == "TURN_EXTRACTION"]
    assert len(ext) == 1 and ext[0]["provider_attempt"] == 2, "사용량 없는 늦은 실패를 날조했습니다"
    assert "ready" in r.ev["order"]


async def test_bc4_call_with_retry_tracks_timed_out_call_unit():
    """단위 — 래퍼 타임아웃이 underlying 호출을 잊지 않고 관측자에게 넘긴다."""
    op = core.cost_ledger.ProviderOperation(None, operation="TURN_EXTRACTION", model="m")
    gate = threading.Event()

    def _blocking():
        gate.wait(5)
        return FakeGenAIResponse("x", usage=None)
    ok, res = await core.call_with_retry(
        lambda: asyncio.to_thread(_blocking), layer="extraction", timeout=0.05,
        retries=1, on_attempt_result=op.on_attempt, operation_id=op.operation_id)
    assert ok is False and res is None
    assert op.has_inflight()
    gate.set()
    await _until(lambda: not op.has_inflight())
    assert op.event_ids == []            # usage 없음 → 날조 없음


def test_bc4_automatic_configs_carry_network_timeout():
    """liveness 상한 — 자동 경로 provider 설정은 per-request 네트워크 타임아웃을 싣는다."""
    from tests.conftest import source_of
    src = source_of("cogs/gm.py")
    assert src.count("http_options=core.resilience.provider_http_options(") >= 10
    opt = core.resilience.provider_http_options("extraction")
    assert opt.timeout == int((core.resilience.get_timeout("extraction")
                               + core.resilience.NETWORK_TIMEOUT_GRACE) * 1000)


# ══════════════════════════════════════════════════════════════
# GPT 재게이트 패치 (2026-09-25) — B-C5 / B-C6
# ══════════════════════════════════════════════════════════════

class _BoundaryAsyncio:
    """core.resilience 의 asyncio 대역 — 첫 wait_for 호출만 '타임아웃 결정 직후
    provider 완료' 경계를 재현한다(대기 중 provider가 끝나게 둔 뒤 TimeoutError)."""

    def __init__(self, target_calls=1):
        self.n = 0
        self.target = target_calls

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, aw, timeout):
        self.n += 1
        if self.n <= self.target:
            await asyncio.sleep(0.05)       # 이 사이 underlying provider 태스크가 완료됨
            aw.cancel()                     # shield 래퍼만 취소(내부 태스크는 그대로)
            raise asyncio.TimeoutError()
        return await asyncio.wait_for(aw, timeout)


async def test_bc5_timeout_boundary_success_is_observed_and_frozen(rig, monkeypatch):
    """B-C5 — TimeoutError 핸들러 진입 시 task.done()==True 인 경계에서도
    늦은 usage가 정확히 1건 CostEvent가 되고 동결 멤버십에 포함된다."""
    r = rig
    done_at_handler = []
    orig_track = core.resilience._track_inflight

    def _spy(obs, task, **kw):
        done_at_handler.append(task.done())
        return orig_track(obs, task, **kw)
    monkeypatch.setattr(core.resilience, "_track_inflight", _spy)
    monkeypatch.setattr(core.resilience, "asyncio", _BoundaryAsyncio(target_calls=1))
    calls = {"n": 0}

    def _route(kwargs):
        calls["n"] += 1
        return _json({"situation": {}}, p=555 if calls["n"] == 1 else 800, c=55)
    r.prov.routes["extraction"] = _route

    tx = await _run_turn(r)
    assert done_at_handler == [True], "경계 조건(핸들러 진입 시 done)이 재현되지 않았습니다"
    prep = tx.preparation
    ext = [e for e in _events(r, tx) if e["operation"] == "TURN_EXTRACTION"]
    late = [e for e in ext if e["metadata"].get("late_after_wrapper_timeout")]
    assert len(late) == 1 and late[0]["input_tokens"] == 555 and late[0]["provider_attempt"] == 1
    assert len(ext) == 2, "중복 CostEvent 또는 누락"
    assert len({e["idempotency_key"] for e in ext}) == 2
    assert {e["event_id"] for e in ext} <= set(prep.frozen_cost_event_ids)
    assert "ready" in r.ev["order"]


async def test_bc5_already_done_future_observed_synchronously(tmp_path):
    """이미 done인 future를 tracker에 직접 넘겨도 반환 전에 관측이 끝난다(중복 없음)."""
    ledger = CostLedger(str(tmp_path / "l.jsonl"))
    op = core.cost_ledger.ProviderOperation(ledger, operation="TURN_EXTRACTION",
                                            model=core.DEFAULT_MODEL)
    op.mark_attempt()
    fut = asyncio.get_running_loop().create_future()
    fut.set_result(FakeGenAIResponse("x", usage=_usage(321, 12)))
    op.track_inflight(fut)
    assert len(op.event_ids) == 1 and not op.has_inflight()   # 반환 시점에 이미 기록
    op.track_inflight(fut)                                     # 같은 attempt 재전달
    await asyncio.sleep(0)
    assert len(op.event_ids) == 1, "중복 CostEvent"
    rows = ledger.list_cost_events()
    assert len(rows) == 1 and rows[0]["input_tokens"] == 321

    op2 = core.cost_ledger.ProviderOperation(ledger, operation="TURN_EXTRACTION",
                                             model=core.DEFAULT_MODEL)
    op2.mark_attempt()
    bad = asyncio.get_running_loop().create_future()
    bad.set_exception(RuntimeError("late failure"))
    op2.track_inflight(bad)
    assert op2.event_ids == [] and not op2.has_inflight()     # 날조 없음
    nousage = asyncio.get_running_loop().create_future()
    nousage.set_result(FakeGenAIResponse("x", usage=None))
    op2.mark_attempt()
    op2.track_inflight(nousage)
    assert op2.event_ids == []


async def test_bc5_callback_gap_keeps_membership_open(session_auto_ready, tmp_path):
    """provider future done ~ 늦은 관측 콜백 실행 사이 틈에서도 멤버십이 닫히지 않는다."""
    s = session_auto_ready
    tx = tt.begin_turn_transaction(s, "선언")
    prep = tp.ensure_preparation(s, tx.transaction_id)
    ledger = CostLedger(str(tmp_path / "g.jsonl"))

    class _Bot:
        cost_ledger = ledger
    op = core.cost_ledger.begin_operation(_Bot, "TURN_EXTRACTION", session=s,
                                          model=core.DEFAULT_MODEL)
    prep.claim_cost_operation(op)
    op.mark_attempt()
    fut = asyncio.get_running_loop().create_future()
    op.track_inflight(fut)
    prep.seal()
    fut.set_result(FakeGenAIResponse("x", usage=_usage(111, 11)))
    # 콜백은 아직 실행되지 않았다(다음 루프 반복) — provider future는 done.
    assert fut.done() and op.has_inflight()
    with pytest.raises(tp.BarrierViolationError):
        prep.close_cost_membership()
    await prep.join()                      # 관측 완료 토큰까지 기다린다
    frozen = prep.close_cost_membership()
    assert len(frozen) == 1 and frozen[0] == op.event_ids[0]


async def test_bc5_timeout_then_caller_cancel_records_each_provider_attempt(tmp_path):
    """같은 ProviderOperation에서 attempt 1 타임아웃 → attempt 2 호출자 취소.

    외부 루프가 call_with_retry(retries=1)을 반복하므로 래퍼 attempt는 매번 1이다.
    취소 분기도 실제 provider-attempt 카운터를 올려야 두 늦은 usage가 각각
    provider_attempt 1·2로 정확히 한 번씩 기록된다(_late_observed dedupe로 유실 없음).
    """
    ledger = CostLedger(str(tmp_path / "l.jsonl"))
    op = core.cost_ledger.ProviderOperation(ledger, operation="TURN_EXTRACTION",
                                            model=core.DEFAULT_MODEL)
    loop = asyncio.get_running_loop()
    pending = [loop.create_future(), loop.create_future()]
    started = [asyncio.Event(), asyncio.Event()]
    calls = []

    def _factory():
        i = len(calls)
        calls.append(i)

        async def _call():
            started[i].set()
            return await pending[i]
        return _call()

    # attempt 1 — 래퍼 타임아웃
    ok, res = await core.call_with_retry(
        _factory, layer="extraction", timeout=0.05, retries=1,
        on_attempt_result=op.on_attempt, operation_id=op.operation_id)
    assert ok is False and res is None and op.has_inflight()

    # attempt 2 — provider 호출 진행 중 호출자 취소
    outer = asyncio.ensure_future(core.call_with_retry(
        _factory, layer="extraction", timeout=30, retries=1,
        on_attempt_result=op.on_attempt, operation_id=op.operation_id))
    await asyncio.wait_for(started[1].wait(), 5)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert len(calls) == 2 and op._attempt == 2

    # 두 underlying 호출 모두 늦게 usage를 반환한다(역순 완료).
    pending[1].set_result(FakeGenAIResponse("b", usage=_usage(222, 22)))
    pending[0].set_result(FakeGenAIResponse("a", usage=_usage(111, 11)))
    await _until(lambda: not op.has_inflight())

    rows = ledger.list_cost_events()
    by_pa = {}
    for r in rows:
        by_pa.setdefault(r["provider_attempt"], []).append(r)
    assert sorted(by_pa) == [1, 2]
    assert all(len(v) == 1 for v in by_pa.values()), "중복 CostEvent"
    assert by_pa[1][0]["input_tokens"] == 111
    assert by_pa[2][0]["input_tokens"] == 222
    assert all(r["metadata"].get("late_after_wrapper_timeout") for r in rows)
    assert sorted(op.late_events) == [1, 2] and len(op.event_ids) == 2


# ── B-C6 — 필드 커버리지 음성/양성 테스트 ─────────────────────

def _append_full_log(s):
    os.makedirs(f"sessions/{s.session_id}", exist_ok=True)
    with open(f"sessions/{s.session_id}/full_logs.jsonl", "a", encoding="utf-8") as f:
        f.write('{"turn": 1}\n')


def _mutate_raw_in_place(s):
    s.raw_logs[0].parts[0].text = "변조된 과거 로그"


@pytest.mark.parametrize("tamper,reason", [
    (lambda s: setattr(s, "player_faction", "외부 세력"), "player_faction"),
    (lambda s: setattr(s, "main_unlocked_notified", True), "main_unlocked_notified"),
    (lambda s: setattr(s, "pending_bgm", "tense.mp3"), "pending_bgm"),
    (lambda s: setattr(s, "last_bgm_situation", "전투"), "last_bgm_situation"),
    (lambda s: setattr(s, "start_day_number", 42), "start_day_number"),
    (lambda s: setattr(s, "total_ink_spent", 999), "total_ink_spent"),
    (lambda s: setattr(s, "last_turn_cost", 12.0), "last_turn_cost"),
    (lambda s: setattr(s, "_rewind_snapshot", {"x": 1}), "_rewind_snapshot"),
    (_mutate_raw_in_place, "raw_logs"),
    (_append_full_log, "full_logs_file"),
    (lambda s: s.uncompressed_logs.append("[외부] 추가 로그"), "uncompressed_logs_changed"),
    (lambda s: s.current_turn_logs.__setitem__(0, "[변조된 선언]"), "turn_input_log_changed"),
])
async def test_bc6_turn_owned_field_change_blocks_ready(rig, monkeypatch, tamper, reason):
    """B-C6 — 전수 대조로 추가/강화된 각 필드의 준비 중 변경이 READY를 막는다."""
    from google.genai import types as gtypes
    r = rig
    s = r.sess
    s.raw_logs = [gtypes.Content(role="model", parts=[gtypes.Part.from_text(text="과거 로그")])]
    s.uncompressed_logs = ["[과거] a", "[과거] b"]
    s.current_turn_logs = ["[테스터]: 선언"]
    reasons = _capture_ready_reasons(monkeypatch)
    real_dispatch = r.gm._dispatch_proceed
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    tx = tt.get_or_begin_turn_transaction(s, "선언")
    owner = asyncio.create_task(_run_turn(r, tx=tx))
    await _until(lambda: r.prov.started.get("extraction", 0) >= 1)
    tamper(s)
    ext_gate.set()
    await owner
    assert "ready" not in r.ev["order"], f"{reason} 변경에도 READY 도달"
    assert any(reason in x for rs in reasons for x in rs), reasons
    assert tx.status == tt.TurnStatus.FAILED_SYSTEM and r.rec["deduct"] == []


async def test_bc6_background_compression_prefix_delete_is_allowed(rig):
    """배경 압축의 uncompressed_logs 앞부분 삭제는 턴 소유 변화가 아니다(접미 불변식)."""
    r = rig
    s = r.sess
    s.uncompressed_logs = ["[과거] a", "[과거] b", "[과거] c"]
    ext_gate = threading.Event()
    r.prov.gates["extraction"] = ext_gate
    owner = asyncio.create_task(_run_turn(r))
    await _until(lambda: r.prov.started.get("extraction", 0) >= 1)
    del s.uncompressed_logs[:2]                 # 압축 완료 시점의 앞부분 삭제
    ext_gate.set()
    tx = await owner
    assert "ready" in r.ev["order"]
    assert s.uncompressed_logs[0] == "[과거] c" and len(s.uncompressed_logs) == 3


async def test_bc6_prior_turn_pending_bgm_consumption_is_rebaselined(rig, monkeypatch):
    """이전 턴 확정 pending_bgm의 스트림 시작 소비는 사유 기록과 함께 기준선 재설정."""
    r = rig
    s = r.sess

    class _Media:
        async def start_bgm(self, session, track):
            return True
    r.bot.add_cog_stub("MediaCog", _Media())
    monkeypatch.setattr(core, "is_enabled", lambda sess, key: True if key == "bgm"
                        else False)
    tx = tt.get_or_begin_turn_transaction(s, "선언")
    tp.ensure_preparation(s, tx.transaction_id)        # 기준선이 소비 이전에 잡힌 상황
    s.pending_bgm = "prior.mp3"
    tx.preparation.baseline_fingerprint = tp.canonical_fingerprint(s)
    await _run_turn(r, tx=tx)
    assert "ready" in r.ev["order"] and s.pending_bgm is None
    notes = tx.preparation.rebaseline_notes
    assert notes and notes[0]["domain"] == "pending_bgm" and "prior-turn" in notes[0]["reason"]


def test_bc6_raw_logs_digest_detects_in_place_change():
    from google.genai import types as gtypes

    class S:
        pass
    s = S()
    c = gtypes.Content(role="model", parts=[gtypes.Part.from_text(text="원문")])
    s.raw_logs = [c]
    before = tp._raw_logs_digest(s.raw_logs)
    c.parts[0].text = "변조"
    assert tp._raw_logs_digest(s.raw_logs) != before
