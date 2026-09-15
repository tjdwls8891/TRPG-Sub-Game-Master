# API 호출 공통 처리 — 재시도, 응답 지연 차단, 오류 로그 분리
#
# [기획 규정]
#   서버오류·검열·사용량 차단·응답 지연 등 턴 진행에 문제가 생기는 모든 경우,
#   대기 메시지에 '문제 발생'만 알리고(종류는 전달하지 않음) 재시도한다.
#
# [응답 지연 처리의 핵심]
#   자체 타이머를 두고, 타이머를 넘겨 원 응답이 도착해도 그것을 쓰지 않는다.
#   그러지 않으면 재시도 결과와 원 응답이 경합해 묘사가 중복 출력된다.
#   asyncio.wait_for는 타임아웃 시 태스크를 취소하므로 이 요건을 충족한다.
import asyncio
import os
import time

# 층위별 기본 타임아웃(초). 묘사는 길어질 수 있어 넉넉히 잡는다.
DEFAULT_TIMEOUTS = {
    "judgment": 60,
    "instruction": 90,
    "narration": 180,
    "extraction": 60,
    "compression": 120,
    "media": 60,
}

DEFAULT_RETRIES = 2

# 사용자에게 노출할 문구 — 문제 종류를 알리지 않는다(기획 규정).
USER_FACING_NOTICE = "⚠️ 처리 중 문제가 발생하여 다시 시도하고 있습니다…"


def get_timeout(layer: str) -> float:
    """층위별 타임아웃. .env로 개별 조정 가능."""
    env_key = f"TIMEOUT_{layer.upper()}"
    raw = os.getenv(env_key)
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(DEFAULT_TIMEOUTS.get(layer, 90))


def write_error_log(session_id: str, layer: str, exc: Exception, attempt: int):
    """오류를 별도 파일에 남긴다(기획 규정 — 오류 로그는 별도 배치)."""
    try:
        d = os.path.join("sessions", str(session_id))
        os.makedirs(d, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(d, "error_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {layer} 시도{attempt} — "
                    f"{type(exc).__name__}: {exc}\n")
    except Exception:
        pass


def _notify_attempt_observer(on_attempt_result, *, attempt, success,
                             response, exception, operation_id):
    """WP-02: provider attempt 관측 콜백을 안전하게 호출한다.

    관측 실패가 레거시 provider 호출 동작을 절대 바꾸지 않도록 예외를 삼킨다
    (shadow 모드 규정). billing/pricing 정책은 여기에 두지 않는다.
    """
    if on_attempt_result is None:
        return
    try:
        on_attempt_result(attempt=attempt, success=success, response=response,
                          exception=exception, operation_id=operation_id)
    except Exception as obs_e:  # noqa: BLE001
        print(f"[오류대응] attempt observer 실패(무시): {type(obs_e).__name__} - {obs_e}")


async def call_with_retry(fn, *, layer: str, session_id: str = "",
                          retries: int = None, timeout: float = None,
                          on_retry=None, on_attempt_result=None, operation_id=None):
    """API 호출을 재시도·타임아웃 보호와 함께 실행한다.

    Args:
        fn: 인자 없는 코루틴 팩토리. 매 시도마다 새로 호출된다.
            (같은 코루틴 객체를 재사용하면 두 번째 await에서 실패한다)
        layer: 'judgment' | 'instruction' | 'narration' | 'extraction' 등
        on_retry: 재시도 직전 호출할 코루틴. 사용자 안내용.
        on_attempt_result: (WP-02, 선택) provider attempt 1건이 끝날 때마다
            attempt/success/response/exception/operation_id 를 받는 콜백.
            성공·실패 모두에 대해 정확히 한 번 호출된다. 관측 전용이며,
            메타데이터 없는 실패로 CostEvent 를 날조해서는 안 된다.
        operation_id: (WP-02, 선택) 콜백에 그대로 전달되는 논리 오퍼레이션 식별자.

    Returns:
        (성공 여부, 결과 또는 None)
    """
    retries = DEFAULT_RETRIES if retries is None else retries
    timeout = get_timeout(layer) if timeout is None else timeout

    for attempt in range(1, retries + 1):
        try:
            result = await asyncio.wait_for(fn(), timeout=timeout)
            _notify_attempt_observer(
                on_attempt_result, attempt=attempt, success=True,
                response=result, exception=None, operation_id=operation_id)
            return True, result
        except asyncio.TimeoutError as e:
            # 타임아웃 시 wait_for가 태스크를 취소하므로,
            # 뒤늦게 도착하는 원 응답은 사용되지 않는다.
            print(f"[오류대응] {layer} 응답 지연({timeout:.0f}초) — 시도 {attempt}")
            write_error_log(session_id, layer, e, attempt)
            _notify_attempt_observer(
                on_attempt_result, attempt=attempt, success=False,
                response=None, exception=e, operation_id=operation_id)
        except Exception as e:
            print(f"[오류대응] {layer} 실패 — {type(e).__name__} (시도 {attempt})")
            write_error_log(session_id, layer, e, attempt)
            _notify_attempt_observer(
                on_attempt_result, attempt=attempt, success=False,
                response=None, exception=e, operation_id=operation_id)

        if attempt < retries and on_retry:
            try:
                await on_retry()
            except Exception:
                pass

    return False, None


def build_failed_turn_notice(player_message: str) -> str:
    """턴 실패 후 선언 질문을 재개할 때의 안내.

    기획 규정 — 실패한 선언을 복사하기 쉽게 표기해 재입력을 돕는다.
    """
    text = (player_message or "").strip()
    body = (
        "⚠️ 이번 턴 처리에 실패하여 진행을 취소했습니다.\n"
        "아래 선언을 다시 입력하시거나 새로 작성해 주십시오.\n"
    )
    if text:
        body += f"```\n{text[:1500]}\n```"
    return body
