# 프로필 생성 AI 모듈 — 검증(8번) · 병합(9번)
#
# [무료 제공]
#   기획 규정 — 프로필 생성 단순호출은 무료 제공 추진.
#   비용은 집계하되(운영 파악용) 잉크 차감 대상에서 제외한다.
#   집계는 session.profile_ai_cost_krw에 누적된다.
#
# [캐시 미사용]
#   짧은 입력에 대한 판정·정리라 세계관 룰북 전량이 필요하지 않다.
#   필요한 시나리오 정보만 사용자 프롬프트로 추려 넣는다.
import asyncio
import json

from google.genai import types

from .constants import PROFILE_AI_MODEL, TRPG_SAFETY_SETTINGS
from .cost import calculate_text_gen_cost_breakdown, extract_token_usage

# 검증 시 함께 전달할 시나리오 요약의 최대 길이.
def _scenario_context(scenario_data: dict) -> str:
    """검증에 필요한 시나리오 정보(세계관·묘사 가이드) — v7.25: 문자 수로 자르지 않는다(사용자 결정)."""
    if not isinstance(scenario_data, dict):
        return "(없음)"
    parts = []
    for key in ("worldview", "desc_guide"):
        val = scenario_data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, dict):
            parts.append(" / ".join(str(v) for v in val.values()))
    text = "\n".join(parts)
    return text if text else "(없음)"       # v7.25(CUT): 세계관·묘사 가이드 전문(1200자 절단 제거)


def _accrue(session, response, label: str = "프로필 AI") -> float:
    """비용을 집계한다. 차감하지 않는다(무료 제공).

    NOTE: 잉크는 차감하지 않지만 실제 API 비용은 발생한다. 운영자가
          파악할 수 있도록 cost_log에 남기고 total_usd에도 누적한다.
          total_cost(원화)에는 넣지 않는다 — 그 값은 플레이어 청구
          기준이므로 무료분이 섞이면 표기가 어긋난다.
    """
    try:
        meta = response.usage_metadata
        in_t, out_t, cached_t, _th = extract_token_usage(meta)
        bd = calculate_text_gen_cost_breakdown(
            PROFILE_AI_MODEL, input_tokens=in_t,
            output_tokens=out_t, cached_read_tokens=cached_t,
        )
        cost = bd["total_krw"]
        session.profile_ai_cost_krw = (
            float(getattr(session, "profile_ai_cost_krw", 0.0) or 0.0) + cost
        )
        session.total_usd = (
            float(getattr(session, "total_usd", 0.0) or 0.0) + bd["total_usd"]
        )

        sid = getattr(session, "session_id", "")
        if sid:
            from .io import write_cost_log
            write_cost_log(
                sid, f"[무료] {label}",
                in_t, cached_t, out_t, cost,
                float(getattr(session, "total_cost", 0.0) or 0.0))
        return cost
    except Exception as e:
        print(f"[프로필AI] 비용 집계 실패: {e}")
        return 0.0


async def _call(bot, system_instruction, schema, user_prompt: str,
                session=None, temperature: float = 0.2, *, operation=None, billing_hint=None,
                accrue: bool = True):
    """공통 호출. 실패 시 (False, None).

    v7.17: operation·billing_hint로 다른 비캐시 단발 호출(초기 지식 — 운영자 부담)이 같은 provider·재시도·JSON
    경로를 쓴다. accrue=False면 프로필 AI 무료 집계(profile_ai_cost_krw)에 섞지 않는다(CostLedger 기록은 그대로)."""
    from .resilience import call_with_retry

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        safety_settings=TRPG_SAFETY_SETTINGS,
    )
    contents = [types.Content(role="user",
                              parts=[types.Part.from_text(text=user_prompt)])]

    from . import cost_ledger
    _cl_op = cost_ledger.begin_operation(
        bot, operation or cost_ledger.OP_PROFILE_AI, session=session, model=PROFILE_AI_MODEL,
        actor_kind=cost_ledger.ACTOR_SYSTEM,
        billing_hint=billing_hint or cost_ledger.HINT_FREE_FEATURE, copy_transaction=False)
    ok, response = await call_with_retry(
        lambda: asyncio.to_thread(
            bot.genai_client.models.generate_content,
            model=PROFILE_AI_MODEL, contents=contents, config=config,
        ),
        layer="media",
        session_id=getattr(session, "session_id", "") if session else "",
        retries=1,
        on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
    )
    if not ok:
        return False, None
    if session is not None and accrue:
        _accrue(session, response)
    # WP-02: 무료 기능이라도 실제 provider 비용은 발생 — FREE_FEATURE 힌트로 관측.
    try:
        _m = response.usage_metadata
        _in, _out, _cached, _th = extract_token_usage(_m)
        _bd = calculate_text_gen_cost_breakdown(
            PROFILE_AI_MODEL, input_tokens=_in, output_tokens=_out,
            cached_read_tokens=_cached)
        _cl_op.record(
            input_tokens=_in, cached_input_tokens=_cached, output_tokens=_out,
            thought_tokens=_th, cost_usd=_bd["total_usd"], cost_krw=_bd["total_krw"],
            usage_source=cost_ledger.SOURCE_PROVIDER_METADATA)
    except Exception as _e:  # noqa: BLE001
        print(f"[프로필AI] shadow 관측 실패(무시): {type(_e).__name__} - {_e}")
    try:
        return True, json.loads(response.text or "{}")
    except json.JSONDecodeError:
        print("[프로필AI] 응답 파싱 실패")
        return False, None


async def validate(bot, session, *, field: str, value: str,
                   scenario_data: dict = None, rules: str = "") -> dict:
    """유저 입력이 시나리오에 어울리는지 검증한다 (기능 8번).

    호출 실패 시 통과(ok)로 처리한다. 검증기 장애가 캐릭터 생성을
    막아서는 안 되기 때문이다.

    Returns:
        {"verdict": "ok"|"warn"|"reject", "reason": str}
        대안 제시(suggestion)는 없다 — 세계관 충돌은 사유 안내 후 재입력(v7.15).
    """
    from prompts import (PROFILE_VALIDATE_RESPONSE_SCHEMA,
                         PROFILE_VALIDATE_SYSTEM_INSTRUCTION)

    sd = scenario_data if scenario_data is not None else getattr(
        session, "scenario_data", {})
    user_prompt = (
        f"[시나리오 정보]\n{_scenario_context(sd)}\n\n"
        f"[항목]\n{field}\n\n"
        f"[플레이어 입력]\n{value or ''}\n\n"
        f"[항목 제한사항]\n{rules or '(없음)'}"
    )

    ok, data = await _call(
        bot, PROFILE_VALIDATE_SYSTEM_INSTRUCTION,
        PROFILE_VALIDATE_RESPONSE_SCHEMA, user_prompt, session)
    if not ok or not isinstance(data, dict):
        return {"verdict": "ok", "reason": ""}

    verdict = data.get("verdict")
    if verdict not in ("ok", "warn", "reject"):
        verdict = "ok"
    return {"verdict": verdict, "reason": data.get("reason") or ""}


async def merge(bot, session, *, field: str, values: list,
                max_length: int = 300, rules: str = "") -> dict:
    """여러 입력을 하나의 서술로 합친다 (기능 9번).

    호출 실패 시 코드 병합(profile_gen.merge_inputs)으로 폴백한다.
    결과를 자르지 않는다(v7.15) — 길이 초과는 too_long으로 알려 호출자가 플레이어에게 줄이기를 청한다.

    Returns:
        {"merged": str, "dropped": [str], "fallback": bool, "too_long": bool}
    """
    from prompts import (PROFILE_MERGE_RESPONSE_SCHEMA,
                         PROFILE_MERGE_SYSTEM_INSTRUCTION)
    from .profile_gen import merge_inputs

    items = [str(v).strip() for v in (values or []) if str(v or "").strip()]
    if not items:
        return {"merged": "", "dropped": [], "fallback": False, "too_long": False}
    if len(items) == 1:
        return {"merged": items[0], "dropped": [], "fallback": False, "too_long": len(items[0]) > max_length}

    user_prompt = (
        f"[항목]\n{field}\n\n"
        f"[입력 목록 — 순서대로]\n"
        + "\n".join(f"{i + 1}. {v}" for i, v in enumerate(items))
        + (f"\n\n[조합 지침]\n{rules}" if rules else "")
        + f"\n\n[길이 제한]\n{max_length}자 이내"
    )

    ok, data = await _call(
        bot, PROFILE_MERGE_SYSTEM_INSTRUCTION,
        PROFILE_MERGE_RESPONSE_SCHEMA, user_prompt, session, temperature=0.4)
    if not ok or not isinstance(data, dict) or not data.get("merged"):
        merged = merge_inputs(items)
        return {"merged": merged, "dropped": [], "fallback": True, "too_long": len(merged) > max_length}

    merged = str(data["merged"])
    return {"merged": merged,
            "dropped": [str(d) for d in (data.get("dropped") or [])],
            "fallback": False, "too_long": len(merged) > max_length}


async def outfit(bot, session, *, text: str) -> list:
    """v7.46 복장 글 → 착용 물건 [{name, slot, tags}](공용 자리·태그 어휘로 검증 — 어휘 밖 태그만 버린다).

    실패하면 빈 목록 — 착용 없이 진행한다(복장 글은 프로필에 그대로 남는다)."""
    from prompts import PROFILE_OUTFIT_RESPONSE_SCHEMA, PROFILE_OUTFIT_SYSTEM_INSTRUCTION
    from .items import effect_vocab, free_tags, reference_text

    if not str(text or "").strip():
        return []
    voc = effect_vocab()
    wear_slots = [x for x in voc["slots"] if x not in voc["hand_slots"]]
    tags = free_tags(voc["tags"])
    user_prompt = (f"[복장 글]\n{text}\n\n[자리 목록]\n{', '.join(wear_slots)}\n\n"
                   "[효과 태그 목록]\n" + "\n".join(f"{t}: {voc['tags'][t].get('desc', '')}" for t in tags)
                   + "\n\n[참고 묶음 — 비슷한 옷은 이 묶음의 자리·효과를 따른다(목록에 없는 태그는 쓰지 않는다)]\n"
                   + reference_text())
    ok, data = await _call(bot, PROFILE_OUTFIT_SYSTEM_INSTRUCTION, PROFILE_OUTFIT_RESPONSE_SCHEMA, user_prompt,
                           session)
    if not ok or not isinstance(data, dict):
        return []
    out, seen = [], set()
    for it in data.get("items") or ():
        if not isinstance(it, dict):
            continue
        name, slot = str(it.get("name") or "").strip(), it.get("slot")
        if not name or any(c in name for c in "[]〈〉") or slot not in wear_slots or slot in seen:
            continue
        seen.add(slot)
        out.append({"name": name, "slot": slot, "tags": free_tags(it.get("tags"))})
    return out


def format_validation(field: str, result: dict) -> str:
    """검증 결과 표시 문자열."""
    v = result.get("verdict")
    if v == "reject":
        return f"⚠️ **{field}**: {result.get('reason') or '시나리오와 맞지 않습니다.'}"
    if v == "warn":
        return f"ℹ️ **{field}**: {result.get('reason') or ''}".rstrip()
    return ""
