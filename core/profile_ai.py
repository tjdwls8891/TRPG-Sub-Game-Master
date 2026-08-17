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
CONTEXT_LIMIT = 1200


def _scenario_context(scenario_data: dict) -> str:
    """검증에 필요한 최소 시나리오 정보만 추린다.

    세계관 전문을 넣으면 무료 제공 취지에 어긋나는 비용이 든다.
    """
    if not isinstance(scenario_data, dict):
        return "(없음)"
    parts = []
    for key in ("worldview", "desc_guide"):
        val = scenario_data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, dict):
            parts.append(" / ".join(str(v) for v in list(val.values())[:5]))
    text = "\n".join(parts)
    return text[:CONTEXT_LIMIT] if text else "(없음)"


def _accrue(session, response) -> float:
    """비용을 집계한다. 차감하지 않는다(무료 제공)."""
    try:
        meta = response.usage_metadata
        in_t, out_t, cached_t, _th = extract_token_usage(meta)
        cost = calculate_text_gen_cost_breakdown(
            PROFILE_AI_MODEL, input_tokens=in_t,
            output_tokens=out_t, cached_read_tokens=cached_t,
        )["total_krw"]
        session.profile_ai_cost_krw = (
            float(getattr(session, "profile_ai_cost_krw", 0.0) or 0.0) + cost
        )
        return cost
    except Exception as e:
        print(f"[프로필AI] 비용 집계 실패: {e}")
        return 0.0


async def _call(bot, system_instruction, schema, user_prompt: str,
                session=None, temperature: float = 0.2):
    """공통 호출. 실패 시 (False, None)."""
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

    ok, response = await call_with_retry(
        lambda: asyncio.to_thread(
            bot.genai_client.models.generate_content,
            model=PROFILE_AI_MODEL, contents=contents, config=config,
        ),
        layer="media",
        session_id=getattr(session, "session_id", "") if session else "",
        retries=1,
    )
    if not ok:
        return False, None
    if session is not None:
        _accrue(session, response)
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
        {"verdict": "ok"|"warn"|"reject", "reason": str, "suggestion": str}
    """
    from prompts import (PROFILE_VALIDATE_RESPONSE_SCHEMA,
                         PROFILE_VALIDATE_SYSTEM_INSTRUCTION)

    sd = scenario_data if scenario_data is not None else getattr(
        session, "scenario_data", {})
    user_prompt = (
        f"[시나리오 정보]\n{_scenario_context(sd)}\n\n"
        f"[항목]\n{field}\n\n"
        f"[플레이어 입력]\n{(value or '')[:500]}\n\n"
        f"[항목 제한사항]\n{rules or '(없음)'}"
    )

    ok, data = await _call(
        bot, PROFILE_VALIDATE_SYSTEM_INSTRUCTION,
        PROFILE_VALIDATE_RESPONSE_SCHEMA, user_prompt, session)
    if not ok or not isinstance(data, dict):
        return {"verdict": "ok", "reason": "", "suggestion": ""}

    verdict = data.get("verdict")
    if verdict not in ("ok", "warn", "reject"):
        verdict = "ok"
    return {"verdict": verdict,
            "reason": data.get("reason") or "",
            "suggestion": data.get("suggestion") or ""}


async def merge(bot, session, *, field: str, values: list,
                max_length: int = 300) -> dict:
    """여러 입력을 하나의 서술로 합친다 (기능 9번).

    호출 실패 시 코드 병합(profile_gen.merge_inputs)으로 폴백한다.

    Returns:
        {"merged": str, "dropped": [str], "fallback": bool}
    """
    from prompts import (PROFILE_MERGE_RESPONSE_SCHEMA,
                         PROFILE_MERGE_SYSTEM_INSTRUCTION)
    from .profile_gen import merge_inputs

    items = [str(v).strip() for v in (values or []) if str(v or "").strip()]
    if not items:
        return {"merged": "", "dropped": [], "fallback": False}
    if len(items) == 1:
        return {"merged": items[0][:max_length], "dropped": [], "fallback": False}

    user_prompt = (
        f"[항목]\n{field}\n\n"
        f"[입력 목록 — 순서대로]\n"
        + "\n".join(f"{i + 1}. {v}" for i, v in enumerate(items))
        + f"\n\n[길이 제한]\n{max_length}자 이내"
    )

    ok, data = await _call(
        bot, PROFILE_MERGE_SYSTEM_INSTRUCTION,
        PROFILE_MERGE_RESPONSE_SCHEMA, user_prompt, session, temperature=0.4)
    if not ok or not isinstance(data, dict) or not data.get("merged"):
        return {"merged": merge_inputs(items)[:max_length],
                "dropped": [], "fallback": True}

    return {"merged": str(data["merged"])[:max_length],
            "dropped": [str(d) for d in (data.get("dropped") or [])],
            "fallback": False}


def format_validation(field: str, result: dict) -> str:
    """검증 결과 표시 문자열."""
    v = result.get("verdict")
    if v == "reject":
        msg = f"⚠️ **{field}**: {result.get('reason') or '시나리오와 맞지 않습니다.'}"
        if result.get("suggestion"):
            msg += f"\n> 예시: {result['suggestion']}"
        return msg
    if v == "warn":
        return f"ℹ️ **{field}**: {result.get('reason') or ''}".rstrip()
    return ""
