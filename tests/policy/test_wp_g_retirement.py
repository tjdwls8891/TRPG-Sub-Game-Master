# -*- coding: utf-8 -*-
"""WP-G — 레거시 게임플레이 은퇴 · 회계 소비자 전환 · 스키마 정합.

G1  수동 `!진행`/`!수정` 은퇴(AUD-016/018), `!재생성` = WP-E rerender 단일 구현(AUD-017)
G2  자동 GM 비용 상한 = CostLedger(AUD-033), 사용량 표시의 우주 분리, 운영자 계정 쓰기 strict(AUD-061)
G4  AUD-053 — cached_worldview_sections 생성 시 초기화 (test_harness_selfcheck 참고)
"""
from __future__ import annotations

import ast
import json
import os
import re

import pytest

import core
from core import accounts
from core import cost_ledger as CL
from tests.conftest import source_of

pytestmark = pytest.mark.policy


def _command_names(rel: str) -> set:
    tree = ast.parse(source_of(rel))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("command", "group"):
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    names.add(kw.value.value)
    return names


# ══════════════════════════════════════════════════════════════
#  G1 — 게임플레이 은퇴
# ══════════════════════════════════════════════════════════════

def test_g1_manual_proceed_and_edit_commands_are_retired():
    names = set()
    for fn in sorted(os.listdir(os.path.join(os.path.dirname(__file__), "..", "..", "cogs"))):
        if fn.endswith(".py") and not fn.startswith("__"):
            names |= _command_names(f"cogs/{fn}")
    assert "진행" not in names and "수정" not in names
    # 보존: 재생성(WP-E)·출력물(읽기 전용)·되감기
    assert {"재생성", "출력물", "되감기"} <= names


def test_g1_regenerate_is_single_wp_e_rerender_delegate():
    game = source_of("cogs/game.py")
    body = game[game.index("async def regenerate_turn"):game.index('@commands.command(name="출력물")')]
    assert "gm_cog.rerender_latest(" in body
    # 로그만 되돌리던 구 재생성 흔적이 없다: raw_logs/uncompressed_logs 직접 조작 없음
    assert "raw_logs" not in body and "uncompressed_logs" not in body
    gm = source_of("cogs/gm.py")
    assert len(re.findall(r"async def rerender_latest\(", gm)) == 1
    for rel in ("cogs/game.py", "cogs/gm.py", "cogs/session.py", "cogs/system.py"):
        assert not re.search(r"def \w*regenerat\w*\(", source_of(rel).replace("def regenerate_turn(", "")), rel


def test_g1_retired_command_only_paths_are_gone():
    game = source_of("cogs/game.py")
    ep = game[game.index("async def _execute_proceed"):game.index("async def release_turn_processing")]
    assert "_call_gm_logic" not in ep            # `!진행` 무지시 자동 생성 분기
    assert "session.last_turn_anchor_id" not in ep   # 유일 소비자 `!수정`과 함께 은퇴
    assert "edit_last_output" not in game and "proceed_turn" not in game
    for rel in ("cogs/game.py", "cogs/gm.py", "cogs/session.py", "cogs/character.py"):
        assert "last_turn_anchor_id =" not in source_of(rel), rel


def test_g1_help_and_usage_no_longer_advertise_retired_behavior():
    sysm = source_of("cogs/system.py")
    help_body = sysm[sysm.index("async def show_commands"):sysm.index('@commands.command(name="채널정리")')]
    for token in ("!진행", "!수정", "자:이름", "태:이름", "태:유이설"):
        assert token not in help_body, token
    from cogs.errors import USAGE
    assert "진행" not in USAGE and "수정" not in USAGE
    assert "재생성" in USAGE


def test_g1_image_position_tags_still_parsed_and_legacy_tags_stripped():
    """상:/중:/하: 이미지 태그는 live, 자:/태: 는 권위 없이 strip만(정규식 과잉 제거 금지)."""
    game = source_of("cogs/game.py")
    assert "img_pattern  = r'(상|중|하):('" in game
    assert "re.sub(res_pattern, '', clean_instruction)" in game
    assert "re.sub(status_pattern, '', clean_instruction)" in game


# ══════════════════════════════════════════════════════════════
#  G2 — 회계 소비자 전환
# ══════════════════════════════════════════════════════════════

def _ev(eid, sid, krw, usd=None, hint=CL.HINT_PLAYER_CANDIDATE):
    return CL.CostEvent(
        event_id=eid, idempotency_key=f"k:{eid}", created_at=0.0,
        provider=CL.PROVIDER_GOOGLE_GENAI, operation=CL.OP_TURN_JUDGMENT, model="m",
        session_id=sid, transaction_id=None, logical_turn=None, turn_attempt=None,
        provider_attempt=1, actor_user_id=None, actor_kind=CL.ACTOR_SYSTEM,
        billing_hint=hint, cost_krw=krw, cost_usd=(krw / 1500.0 if usd is None else usd))


def test_g2_no_business_rule_reads_legacy_total_cost():
    """자동 GM 비용 상한은 total_cost − baseline 을 더 이상 계산하지 않는다(AUD-033)."""
    for rel in ("cogs/gm.py", "core/session_flow.py"):
        src = source_of(rel)
        assert "total_cost - session.gm_cost_baseline" not in src
        assert 'total_cost - getattr(session, "gm_cost_baseline"' not in src
        assert "gm_cost_baseline = session.total_cost" not in src
        assert 'gm_cost_baseline = getattr(session, "total_cost"' not in src
    gm = source_of("cogs/gm.py")
    re_body = gm[gm.index("def _round_eligible"):gm.index("async def _recover_then_maybe_round")]
    assert "auto_cost_cap_reached" in re_body and "total_cost" not in re_body


def test_g2_auto_cost_cap_follows_costledger_not_mirror(fake_bot, session_auto_ready):
    s = session_auto_ready
    fake_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    L = fake_bot.cost_ledger
    L.record_cost_event_strict(_ev("a", s.session_id, 100.0))
    L.record_cost_event_strict(_ev("x", "other-session", 999.0))     # 다른 세션은 무관
    core.mark_auto_mode_start(fake_bot, s)
    assert s.gm_cost_basis == "ledger" and s.gm_cost_baseline == pytest.approx(100.0)
    s.gm_cost_cap_krw = 50.0
    s.total_cost = 10_000.0                                          # 미러는 규칙 입력 아님
    assert core.auto_mode_used_krw(fake_bot, s) == pytest.approx(0.0)
    assert core.auto_cost_cap_reached(fake_bot, s) is False
    L.record_cost_event_strict(_ev("b", s.session_id, 30.0, hint=CL.HINT_OPERATOR))
    assert core.auto_mode_used_krw(fake_bot, s) == pytest.approx(30.0)
    assert core.auto_cost_cap_reached(fake_bot, s) is False
    L.record_cost_event_strict(_ev("c", s.session_id, 25.0))
    assert core.auto_cost_cap_reached(fake_bot, s) is True
    s.gm_cost_cap_krw = None
    assert core.auto_cost_cap_reached(fake_bot, s) is False


def test_g2_legacy_baseline_is_rebased_once(fake_bot, session_auto_ready):
    s = session_auto_ready
    fake_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    fake_bot.cost_ledger.record_cost_event_strict(_ev("a", s.session_id, 80.0))
    s.gm_cost_basis = ""                    # WP-G 이전 세션: baseline 이 total_cost 우주
    s.gm_cost_baseline = 5.0
    assert core.auto_mode_used_krw(fake_bot, s) == 0.0
    assert s.gm_cost_basis == "ledger" and s.gm_cost_baseline == pytest.approx(80.0)
    fake_bot.cost_ledger.record_cost_event_strict(_ev("b", s.session_id, 7.0))
    assert core.auto_mode_used_krw(fake_bot, s) == pytest.approx(7.0)


def test_g2_cap_fails_closed_on_corrupt_ledger(fake_bot, session_auto_ready):
    s = session_auto_ready
    path = os.path.join("data", "cost_ledger.jsonl")
    fake_bot.cost_ledger = CL.CostLedger(path)
    fake_bot.cost_ledger.record_cost_event_strict(_ev("a", s.session_id, 1.0))
    core.mark_auto_mode_start(fake_bot, s)
    with open(path, "a", encoding="utf-8") as f:
        f.write("{손상\n")
    s.gm_cost_cap_krw = 1_000_000.0
    assert core.auto_cost_cap_reached(fake_bot, s) is True


def test_g2_provider_summary_uses_recorded_values_by_hint(fake_bot, session_auto_ready):
    s = session_auto_ready
    fake_bot.cost_ledger = CL.CostLedger(os.path.join("data", "cost_ledger.jsonl"))
    fake_bot.cost_ledger.record_cost_event_strict(_ev("a", s.session_id, 15.0, usd=0.01))
    fake_bot.cost_ledger.record_cost_event_strict(
        _ev("b", s.session_id, 3.0, usd=0.002, hint=CL.HINT_FREE_FEATURE))
    out = core.provider_cost_summary(fake_bot, s.session_id)
    assert out["count"] == 2
    assert out["krw"] == pytest.approx(18.0) and out["usd"] == pytest.approx(0.012)
    assert out["by_hint"][CL.HINT_FREE_FEATURE]["krw"] == pytest.approx(3.0)
    per, n = core.provider_cost_by_session(fake_bot)
    assert n == 2 and per[s.session_id]["krw"] == pytest.approx(18.0)


def test_g2_player_ink_summary_reads_each_authority(session_auto_ready):
    s = session_auto_ready
    s.total_ink_spent = 12                                        # Settlement 미러
    sid = s.session_id
    os.makedirs(os.path.join("sessions", sid), exist_ok=True)
    cache_j = [
        {"type": "WINDOW_OPENED", "window_id": "w1", "prepay": {"u1": 20, "u2": 20}},
        {"type": "WINDOW_PREPAID", "window_id": "w1"},
        {"type": "WINDOW_SETTLE_INTENT", "window_id": "w1", "refund": {"u1": 5, "u2": 5}},
        {"type": "WINDOW_SETTLED", "window_id": "w1"},
        {"type": "WINDOW_OPENED", "window_id": "w2", "prepay": {"u1": 9}},   # 선불 미적용 창
    ]
    with open(core.cache_lifecycle.journal_path(sid), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(e) + "\n" for e in cache_j))
    interp_j = [
        {"type": "INTERPRETED", "interp_id": "i1", "cost_krw": 25.0},
        {"type": "CHARGE_INTENT", "charge_id": "c1", "interp_ids": ["i1"], "payers": {"u1": 3, "u2": 3}},
        {"type": "CHARGED", "charge_id": "c1"},
        {"type": "CHARGE_INTENT", "charge_id": "c2", "interp_ids": [], "payers": {"u1": 4}},  # 미적용
    ]
    with open(core.interpretation_billing.journal_path(sid), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(e) + "\n" for e in interp_j))
    ink = core.player_ink_summary(s)
    assert ink == {"turn_ink": 12, "cache_prepaid": 40, "cache_refunded": 10,
                   "interpretation": 6, "net": 12 + 40 - 10 + 6}


def test_g2_turn_embed_does_not_equate_krw_usd_or_ink():
    emb = core.build_turn_cost_embed(3, [{"label": "x", "cost": 1.0}], 123.0,
                                     total_ink=7, total_usd=0.05, free_krw=0.0)
    acc = next(f.value for f in emb.fields if f.name == "Σ 누적")
    assert "=" not in acc
    assert "턴 청구 **7잉크**" in acc and "미러" in acc


def test_g2_usage_command_reads_authorities_not_mirrors():
    sysm = source_of("cogs/system.py")
    body = sysm[sysm.index("async def usage_report"):sysm.index('@commands.command(name="잉크")')]
    assert "provider_cost_summary" in body and "player_ink_summary" in body
    assert "provider_cost_by_session" in body
    assert 'getattr(session, "total_usd"' not in body and 'getattr(session, "total_cost"' not in body
    assert "usd_to_krw" not in body and "EXCHANGE_RATE" not in body   # 현재 환율 재해석 없음
    assert 'd.get("total_cost"' not in body and 'd.get("total_usd"' not in body


# ── 운영자 계정 쓰기 strict (AUD-061 레거시 잔재) ───────────────────

def _write_raw(uid, text):
    os.makedirs(accounts.ACCOUNTS_DIR, exist_ok=True)
    with open(accounts._path(uid), "w", encoding="utf-8") as f:
        f.write(text)


async def test_g2_admin_grant_on_corrupt_account_raises_and_preserves_file():
    _write_raw("u9", "{손상된 계정")
    with pytest.raises(accounts.AccountCorruptionError):
        await accounts.grant_ink_strict("u9", 100, reason="운영자 지급")
    with pytest.raises(accounts.AccountCorruptionError):
        await accounts.set_balance_strict("u9", 5)
    with pytest.raises(accounts.AccountCorruptionError):
        await accounts.reclaim_ink_strict("u9", 1)
    with open(accounts._path("u9"), encoding="utf-8") as f:
        assert f.read() == "{손상된 계정"          # 빈 계정으로 덮어쓰지 않았다


async def test_g2_admin_write_failure_is_reported_not_swallowed(monkeypatch):
    await accounts.register_account_strict("u8")
    bal0 = accounts.get_balance("u8")

    def _boom(acc):
        raise accounts.AccountPersistenceError("쓰기 실패(테스트)")
    mp = pytest.MonkeyPatch()
    mp.setattr(accounts, "_write_account_strict", _boom)
    try:
        with pytest.raises(accounts.AccountPersistenceError):
            await accounts.grant_ink_strict("u8", 50, reason="운영자 지급")
    finally:
        mp.undo()
    assert accounts.get_balance("u8") == bal0


async def test_g2_admin_semantics_preserved():
    await accounts.register_account_strict("u7")
    assert accounts.is_registered("u7")
    assert await accounts.grant_ink_strict("u7", 30, reason="충전") == 30
    acc = accounts.load_account_strict("u7")
    assert acc["total_charged_ink"] == 30
    assert await accounts.grant_ink_strict("u7", 5, reason="운영자 지급") == 35
    assert accounts.load_account_strict("u7")["total_charged_ink"] == 30   # 충전만 누적
    r = await accounts.reclaim_ink_strict("u7", 100)
    assert r == {"balance": 1, "deducted": 100, "overdraft": True}       # 레거시 floor 규약
    res = await accounts.set_balance_strict("u7", 500, reason="조정")
    assert res == {"before": 1, "after": 500, "delta": 499}
    hist = accounts.load_account_strict("u7")["history"]
    assert [h["reason"] for h in hist] == ["충전", "운영자 지급", "운영자 회수", "조정"]


def test_g2_no_production_caller_of_legacy_tolerant_account_writers():
    from tests.conftest import REPO_ROOT
    pat = re.compile(r"\b(add_ink|deduct_ink|set_balance|register_account)\(")
    offenders = []
    for base in ("core", "cogs"):
        for dirpath, _dirs, files in os.walk(os.path.join(REPO_ROOT, base)):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT).replace("\\", "/")
                if rel == "core/accounts.py":
                    continue
                for ln, line in enumerate(source_of(rel).splitlines(), 1):
                    if pat.search(line) and not line.lstrip().startswith("#"):
                        offenders.append((rel, ln, line.strip()))
    assert offenders == []


# ══════════════════════════════════════════════════════════════
#  D2 — 저작 콘텐츠: 승인된 D2-b만 변경
# ══════════════════════════════════════════════════════════════

def _scenario(name):
    from tests.conftest import REPO_ROOT
    with open(os.path.join(REPO_ROOT, "scenarios", f"{name}.json"), encoding="utf-8") as f:
        return json.load(f)


def test_d2b_wuxia_placeholder_location_images_removed_without_invention():
    d = _scenario("무협")
    assert d["location_images"] == {}                 # 가짜 entry 제거, 대체 키워드 창작 없음


def test_d2_retained_authored_content_untouched():
    d = _scenario("무협")
    assert "플레이스홀더" in d["image_prompts"]["인물"]["prompt"]        # D2-a 유지
    assert "플레이스홀더" in d["image_prompts"]["배경"]["prompt"]
    for name in ("무협", "영도", "다크판타지", "빈시나리오", "scenario.example"):
        assert _scenario(name)["media_dir"] == "./media"                  # D2-c 유지
    assert _scenario("영도")["job_guides"]                                # D2-d 유지


async def test_d2b_empty_location_images_injects_no_image_list(session_auto_ready):
    import cogs.gm as gm_mod
    s = session_auto_ready
    s.scenario_data = dict(s.scenario_data, location_images={})
    logic = gm_mod._build_logic_user_prompt(s, "주변을 살핀다", [])
    assert "[사용 가능한 장소 이미지 목록" not in logic


# ══════════════════════════════════════════════════════════════
#  D3 — 압축 선결제 은퇴 (AUD-026/027)
# ══════════════════════════════════════════════════════════════

def test_d3_fictional_compression_prepayment_retired():
    for name in ("compression_prepay", "settle_compression", "settle_on_session_close",
                 "estimate_compression"):
        assert not hasattr(core, name), name
        assert not hasattr(core.estimate, name), name
    assert "compression_prepaid_krw" not in core.SESSION_FIELDS
    for rel in ("cogs/gm.py", "cogs/game.py", "core/display.py", "core/ui.py",
                "core/estimate.py", "core/models.py", "core/io.py"):
        src = source_of(rel)
        assert "compression_prepaid_krw" not in src, rel
        assert "compression_prepay(" not in src, rel
    # 플레이어/마스터에게 '압축 선결제·환급·추가' 문구를 내지 않는다
    for rel in ("cogs/gm.py", "core/display.py", "core/ui.py", "cogs/game.py"):
        for line in source_of(rel).splitlines():
            if "압축 선결제" in line:
                assert line.lstrip().startswith("#") or "WP-G" in line, (rel, line)


def test_d3_compression_cost_stays_operational_fact_outside_settlement():
    """압축 CostEvent는 세션 범위 운영 사실(transaction 미귀속) — 턴 Settlement에 들어가지 않는다."""
    game = source_of("cogs/game.py")
    for op in ("OP_MEMORY_AUTO_COMPRESSION", "OP_MEMORY_MANUAL_COMPRESSION"):
        i = game.index(op)
        assert "copy_transaction=False" in game[i:i + 400]


# ══════════════════════════════════════════════════════════════
#  AUD-014 보존 — 공통 + 시나리오 상태이상 병합(추출 유효 목록의 근거)
# ══════════════════════════════════════════════════════════════

def test_aud014_merged_status_effects_preserved():
    from tests.conftest import REPO_ROOT
    with open(os.path.join(REPO_ROOT, "data", "common_status_effects.json"), encoding="utf-8") as f:
        common = json.load(f)
    c0 = common[0]["name"]
    merged = core.get_merged_status_effects({"status_effects": [
        {"name": c0, "apply_condition": "시나리오 정의", "weight": 3, "remove_condition": "x"},
        {"name": "시나리오전용", "apply_condition": "a", "weight": 0, "remove_condition": "b"}]})
    assert {e["name"] for e in common} <= set(merged)            # 공통 목록 포함
    assert merged[c0]["apply_condition"] == "시나리오 정의"       # 시나리오가 덮어쓴다
    assert "시나리오전용" in merged
    assert "get_merged_status_effects" in source_of("core/extraction.py")   # 추출 유효 목록이 병합본을 쓴다
