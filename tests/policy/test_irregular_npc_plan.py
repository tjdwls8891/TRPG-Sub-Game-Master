"""WP-B — 비정규 NPC 경로(미디어 배정 + 승격)를 result-only + plan/normalized 경계로.

producer/parser가 session.irregular_npcs / session.npcs를 **직접** 변경하지 않고,
검증·정규화된 계획(IrregularNpcMutationPlan)/후보(normalize_npc_detail)만
register/promote에 입력됨을 증명한다. canonical 적용은 명시적 단일 호환 소비부
(_apply_irregular_npc_plan / _apply_npc_promotion)에서만 일어난다.

I-B01 producer purity / I-B02 valid applies once / I-B03 invalid rejected /
I-B04 stale rejected / I-B05 duplicate idempotent / I-B06 existing semantics.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import core
from core import turn_preparation as tp

pytestmark = pytest.mark.policy

DIALOG = "「백가」 어서 오시게, 나그네여. 이 마을엔 볼 것이 많다네."


def _make_gm_cog(fake_bot):
    import cogs.gm as gm_mod
    cog = gm_mod.GMCog.__new__(gm_mod.GMCog)
    cog.bot = fake_bot
    return cog


def _with_pool(session, pool):
    sd = dict(getattr(session, "scenario_data", {}) or {})
    sd["irregular_images"] = list(pool)
    session.scenario_data = sd


async def _drive_resolve(monkeypatch, cog, session, master_ch, text, media_json,
                         *, before_return=None):
    """미디어 배정 provider만 결정적으로 대체해 _resolve_irregular_npcs 구동."""
    async def _fake_cwr(fn, **kw):
        if before_return is not None:
            before_return()
        return True, SimpleNamespace(text=json.dumps(media_json), usage_metadata=None)

    monkeypatch.setattr(core, "call_with_retry", _fake_cwr, raising=True)
    return await cog._resolve_irregular_npcs(session, text, master_ch)


# ── I-B01 producer purity ────────────────────────────────────────
def test_ib01_plan_build_is_pure(session_auto_ready):
    sess = session_auto_ready
    sess.irregular_npcs = {}
    sess.npcs = {}
    data = {"npcs": [{"name": "백가", "image_key": "face_a",
                      "gender": "남", "age": "청년"}]}
    plan = tp.build_irregular_npc_plan(
        sess, data, names=["백가"], valid_pool=["face_a", "face_b"],
        use_image=True, text=DIALOG, turn=7)
    assert plan.registrations and plan.registrations[0]["name"] == "백가"
    assert sess.irregular_npcs == {}, "plan build가 irregular_npcs를 변경했습니다"
    assert sess.npcs == {}, "plan build가 npcs를 변경했습니다"

    norm = tp.normalize_npc_detail(
        {"name": "백가", "details": "떠돌이 상인", "role": "상인",
         "attitude": "우호", "birth_year": 1600}, fallback_name="백가")
    assert norm["final_name"] == "백가" and norm["details"]["role"] == "상인"
    assert sess.npcs == {}, "detail normalize가 npcs를 변경했습니다"


# ── I-B02 valid plan applies once ────────────────────────────────
async def test_ib02_valid_plan_registers_once(
        wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    plan = tp.build_irregular_npc_plan(
        sess, {"npcs": [{"name": "백가", "image_key": "face_a",
                         "gender": "남", "age": "청년"}]},
        names=["백가"], valid_pool=["face_a"], use_image=True, text=DIALOG, turn=7)
    added = await cog._apply_irregular_npc_plan(sess, plan, DIALOG, master_channel)
    assert added == 1
    assert "백가" in sess.irregular_npcs
    assert sess.irregular_npcs["백가"]["image_key"] == "face_a"
    assert sess.irregular_npcs["백가"]["gender"] == "남"


# ── I-B03 invalid candidate rejected ─────────────────────────────
async def test_ib03_invalid_candidate_no_mutation(
        wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    # 모델이 코드 파생 후보(names) 밖 인물을 반환 → 거부
    plan = tp.build_irregular_npc_plan(
        sess, {"npcs": [{"name": "가짜인물", "image_key": "face_a",
                         "gender": "남", "age": "청년"}]},
        names=["백가"], valid_pool=["face_a"], use_image=True, text=DIALOG, turn=7)
    assert plan.registrations == []
    assert plan.rejected
    added = await cog._apply_irregular_npc_plan(sess, plan, DIALOG, master_channel)
    assert added == 0
    assert sess.irregular_npcs == {}, "후보 목록 밖 인물이 등록됐습니다"


# ── I-B04 stale result rejected ──────────────────────────────────
#   (3차 게이트 후 교정) 이전 판본은 provider 대기 중 begin_turn_transaction을 다시
#   호출해 '비종료 활성 트랜잭션' 예외가 났고, 호출 실패 처리로 0이 반환되어
#   stale 판정 없이 통과했다. 실제 흐름대로 더 새로운 시도를 열고, stale 판정이
#   실제로 호출되어 True였음을 함께 단언한다.
@pytest.mark.parametrize("mode", ["attempt", "turn"])
async def test_ib04_stale_result_no_mutation(
        monkeypatch, wired_bot, session_auto_ready, master_channel, mode):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    _with_pool(sess, ["face_a"])
    tt = core.turn_transaction
    tt.begin_turn_transaction(sess, "이번 턴")                  # 원인 시도 (8,1)
    media = {"npcs": [{"name": "백가", "image_key": "face_a",
                       "gender": "남", "age": "청년"}]}

    stale_calls = []
    real_stale = tp.extraction_is_stale

    def _spy(session, **kw):
        r = real_stale(session, **kw)
        stale_calls.append(r)
        return r

    monkeypatch.setattr(tp, "extraction_is_stale", _spy)

    def _supersede():
        active = tt.get_active_transaction(sess)
        if mode == "attempt":                                   # (8,1)→(8,2)
            tt.begin_attempt(sess, logical_turn=active.logical_turn)
        else:                                                   # (8,1)→(9,1)
            tt.finalize(sess, active.transaction_id, tt.TurnStatus.COMMITTED)
            sess.turn_count += 1
            tt.begin_turn_transaction(sess, "다음 턴")

    out = await _drive_resolve(monkeypatch, cog, sess, master_channel, DIALOG, media,
                               before_return=_supersede)
    assert stale_calls == [True], "stale 판정이 실행되지 않았거나 stale로 판정되지 않았습니다"
    assert out == 0, "stale 결과가 거부되지 않았습니다"
    assert sess.irregular_npcs == {}, "stale 결과가 canonical NPC를 변경했습니다"


async def test_ib04c_stale_promotion_no_mutation(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    """승격 provider 대기 중 더 새로운 시도가 열리면 promote하지 않는다."""
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    core.irregular_npc.register(sess, "백가", image_key="", gender="남", age="청년",
                                context=DIALOG, turn=5)
    tt = core.turn_transaction
    tt.begin_turn_transaction(sess, "이번 턴")

    stale_calls = []
    real_stale = tp.extraction_is_stale

    def _spy(session, **kw):
        r = real_stale(session, **kw)
        stale_calls.append(r)
        return r

    monkeypatch.setattr(tp, "extraction_is_stale", _spy)
    detail = {"name": "백가", "details": "떠돌이 상인", "role": "상인", "attitude": "우호"}

    async def _fake_cwr(fn, **kw):
        active = tt.get_active_transaction(sess)
        tt.begin_attempt(sess, logical_turn=active.logical_turn)   # 더 새로운 시도
        return True, SimpleNamespace(text=json.dumps(detail), usage_metadata=None)

    monkeypatch.setattr(core, "call_with_retry", _fake_cwr, raising=True)
    ok = await cog._generate_npc_detail(sess, "백가", DIALOG, master_channel)
    assert stale_calls == [True], "승격 stale 판정이 실행되지 않았습니다"
    assert ok is False
    assert "백가" not in sess.npcs, "stale 승격 결과가 session.npcs에 적용됐습니다"
    assert "백가" in sess.irregular_npcs, "stale 승격이 등록부를 변경했습니다"


# ── I-B05 duplicate application idempotent ───────────────────────
async def test_ib05_duplicate_apply_idempotent(
        wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    plan = tp.build_irregular_npc_plan(
        sess, {"npcs": [{"name": "백가", "image_key": "face_a",
                         "gender": "남", "age": "청년"}]},
        names=["백가"], valid_pool=["face_a"], use_image=True, text=DIALOG, turn=7)
    await cog._apply_irregular_npc_plan(sess, plan, DIALOG, master_channel)
    first = dict(sess.irregular_npcs["백가"])
    await cog._apply_irregular_npc_plan(sess, plan, DIALOG, master_channel)
    assert list(sess.irregular_npcs.keys()) == ["백가"], "중복 등록 발생"
    assert sess.irregular_npcs["백가"] == first, "재적용이 기존 항목을 덮어썼습니다"


# ── I-B06 existing successful semantics preserved (register + promote) ──
async def test_ib06_existing_semantics_register_and_promote(
        monkeypatch, wired_bot, session_auto_ready, master_channel):
    sess = session_auto_ready
    cog = _make_gm_cog(wired_bot)
    sess.irregular_npcs = {}
    sess.npcs = {}
    _with_pool(sess, ["face_a"])
    media = {"npcs": [{"name": "백가", "image_key": "face_a",
                       "gender": "남", "age": "청년"}]}
    added = await _drive_resolve(monkeypatch, cog, sess, master_channel, DIALOG, media)
    assert added == 1 and "백가" in sess.irregular_npcs

    # 승격: 서로 다른 턴 3회 등장 → should_promote → _generate_npc_detail → promote.
    detail = {"name": "백가", "details": "떠돌이 상인", "role": "상인",
              "attitude": "우호", "birth_year": 1600}

    async def _fake_cwr_detail(fn, **kw):
        return True, SimpleNamespace(text=json.dumps(detail), usage_metadata=None)

    monkeypatch.setattr(core, "call_with_retry", _fake_cwr_detail, raising=True)
    core.irregular_npc.note_appearance(sess, "백가", 5)
    core.irregular_npc.note_appearance(sess, "백가", 6)
    core.irregular_npc.note_appearance(sess, "백가", 7)
    assert core.irregular_npc.should_promote(sess, "백가")

    ok = await cog._generate_npc_detail(sess, "백가", DIALOG, master_channel)
    assert ok is True, "정상 입력에서 승격이 이뤄지지 않았습니다"
    assert "백가" in sess.npcs, "승격이 session.npcs에 반영되지 않았습니다"
    assert sess.npcs["백가"]["role"] == "상인"
    assert "백가" not in sess.irregular_npcs, "승격 후 등록부에서 제거되지 않았습니다"
