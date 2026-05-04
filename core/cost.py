# 비용 산출 및 포맷팅 유틸리티 — 토큰 단가 계산, 캐시 스토리지 정산
import discord
from .constants import DEFAULT_MODEL, EXCHANGE_RATE, PRICING_1M, IMAGE_MODEL


# ========== [비용 산출 및 포맷팅 유틸리티] ==========

def format_cost(cost_krw: float) -> str:
    """
    원화(KRW)로 환산된 비용을 소수점 셋째 자리에서 반올림하여 UI 출력용 포맷으로 변환.
    """
    return f"₩{cost_krw:.2f}"


def calculate_text_gen_cost_breakdown(model_id: str, input_tokens: int = 0, output_tokens: int = 0,
                                       cached_read_tokens: int = 0) -> dict:
    """
    텍스트 생성 모델 호출 비용을 항목별로 분해하여 KRW로 반환.

    캐시 적중분과 신규 입력분의 단가가 다르고(예: $0.50 vs $0.05/1M), 출력 단가($3/1M)와도
    분리 보고해야 GM이 어디서 비용이 새는지 즉시 진단할 수 있다.

    Args:
        model_id (str): 사용된 모델 식별자
        input_tokens (int): 응답 메타의 prompt_token_count (캐시 적중분 포함)
        output_tokens (int): candidates_token_count
        cached_read_tokens (int): cached_content_token_count (캐시에서 읽혀 할인된 분)

    Returns:
        dict: {
            input_billable_tokens, input_krw,         # 신규 입력분 (단가 $INPUT)
            cache_read_tokens, cache_read_krw,        # 캐시 적중분 (단가 $CACHE_READ)
            output_tokens, output_krw,                # 출력분 (단가 $OUTPUT)
            total_krw, total_usd,
            input_rate, cache_rate, output_rate       # 단가 (USD/1M, 보고용)
        }
    """
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0

    billable_input = max(0, input_tokens - cached_read_tokens)

    input_usd = (billable_input / 1_000_000) * rates["INPUT"]
    cache_usd = (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]
    output_usd = (output_tokens / 1_000_000) * rates["OUTPUT"]
    total_usd = input_usd + cache_usd + output_usd

    return {
        "input_billable_tokens": billable_input,
        "input_krw": input_usd * EXCHANGE_RATE,
        "cache_read_tokens": cached_read_tokens,
        "cache_read_krw": cache_usd * EXCHANGE_RATE,
        "output_tokens": output_tokens,
        "output_krw": output_usd * EXCHANGE_RATE,
        "total_krw": total_usd * EXCHANGE_RATE,
        "total_usd": total_usd,
        "input_rate": rates["INPUT"],
        "cache_rate": rates["CACHE_READ"],
        "output_rate": rates["OUTPUT"],
    }


def calculate_image_gen_cost(model_id: str, prompt_tokens: int = 0, image_output_tokens: int = 0,
                              text_output_tokens: int = 0) -> dict:
    """
    이미지 생성 모델(예: gemini-3.1-flash-image-preview)의 호출 비용을 항목별로 산출하여 KRW로 반환.

    이미지 출력 토큰과 텍스트 출력 토큰의 단가가 다르므로(이미지 $60/1M, 텍스트 $3/1M),
    별도 항목으로 분리 정산하여 모니터링 정확도를 확보한다.

    Args:
        model_id (str): 사용된 이미지 모델 식별자
        prompt_tokens (int): 입력 프롬프트(텍스트+레퍼런스 이미지) 토큰 수
        image_output_tokens (int): 출력된 이미지의 토큰 수 (해상도에 따라 결정됨)
        text_output_tokens (int): 응답에 포함된 텍스트(thinking 포함) 토큰 수

    Returns:
        dict: {input_krw, image_krw, text_krw, total_krw, total_usd} 형태의 분해 비용
    """
    rates = PRICING_1M.get(model_id, PRICING_1M.get(IMAGE_MODEL))

    input_usd = (max(0, prompt_tokens) / 1_000_000) * rates["INPUT"]
    image_usd = (max(0, image_output_tokens) / 1_000_000) * rates.get("OUTPUT_IMAGE", rates["OUTPUT"])
    text_usd = (max(0, text_output_tokens) / 1_000_000) * rates["OUTPUT"]
    total_usd = input_usd + image_usd + text_usd

    return {
        "input_krw": input_usd * EXCHANGE_RATE,
        "image_krw": image_usd * EXCHANGE_RATE,
        "text_krw": text_usd * EXCHANGE_RATE,
        "total_krw": total_usd * EXCHANGE_RATE,
        "total_usd": total_usd,
    }


def calculate_upload_cost(model_id: str, input_tokens=0, output_tokens=0, cached_read_tokens=0) -> float:
    """
    API 사용량을 기반으로 업로드 및 생성 과금액을 원화(KRW)로 산출.

    NOTE: 내부 데이터의 무결성을 위해 소수점 이하의 부동소수점 값을 반올림 없이 원형 그대로 반환.
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0

    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    actual_input_tokens = max(0, input_tokens - cached_read_tokens)

    cost_usd = 0.0
    cost_usd += (actual_input_tokens / 1_000_000) * rates["INPUT"]
    cost_usd += (output_tokens / 1_000_000) * rates["OUTPUT"]
    cost_usd += (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]

    return cost_usd * EXCHANGE_RATE


# ========== [디스코드 임베드 비용 보고 빌더] ==========

def build_cache_cost_embed(label: str, storage_cost: float, upload_cost: float, total_cost: float) -> discord.Embed:
    """
    캐시 작업(생성·재발급·삭제) 비용을 Discord Embed로 조립.

    Args:
        label (str): 작업 이름 (예: '새 세션 캐시 생성', '수동 캐시 재발급')
        storage_cost (float): 기존 캐시 보관비 (KRW). 없으면 0.0
        upload_cost (float): 새 캐시 업로드 비용 (KRW). 없으면 0.0
        total_cost (float): session.total_cost 누적값 (KRW)

    Returns:
        discord.Embed
    """
    embed = discord.Embed(title="💾 캐시 비용 보고", color=0x3498DB)
    embed.add_field(name="작업", value=label, inline=False)
    if storage_cost > 0:
        embed.add_field(name="기존 캐시 보관비", value=format_cost(storage_cost), inline=True)
    if upload_cost > 0:
        embed.add_field(name="새 캐시 업로드", value=format_cost(upload_cost), inline=True)
    embed.add_field(name="총 누적 비용", value=format_cost(total_cost), inline=False)
    return embed


def build_text_gen_cost_embed(label: str, model_id: str, breakdown: dict, turn_cost: float, total_cost: float,
                               extra_fields: list = None) -> discord.Embed:
    """
    텍스트 생성(설정생성 등) 비용을 Discord Embed로 조립.

    Args:
        label (str): 작업 이름 (예: "PC '아서' 설정 초안 생성")
        model_id (str): 사용된 모델 식별자
        breakdown (dict): calculate_text_gen_cost_breakdown() 반환값
        turn_cost (float): 이번 호출 총 비용 (KRW)
        total_cost (float): 누적 비용 (KRW)
        extra_fields (list): [(name, value, inline), ...] 추가 필드 목록

    Returns:
        discord.Embed
    """
    embed = discord.Embed(title="🎨 생성 비용 보고", color=0x9B59B6)
    embed.add_field(name="작업", value=label, inline=False)
    embed.add_field(name="모델", value=model_id, inline=False)
    _ib = int(breakdown.get("input_billable_tokens") or 0)
    _cr = int(breakdown.get("cache_read_tokens") or 0)
    _ot = int(breakdown.get("output_tokens") or 0)
    total_input = _ib + _cr
    cache_hit = (_cr / total_input * 100) if total_input else 0.0
    token_desc = (
        f"신규 {_ib:,} × ${breakdown.get('input_rate', 0.0):.2f}/1M → {format_cost(breakdown.get('input_krw', 0.0))}\n"
        f"캐시 {_cr:,} × ${breakdown.get('cache_rate', 0.0):.2f}/1M → {format_cost(breakdown.get('cache_read_krw', 0.0))}\n"
        f"출력 {_ot:,} × ${breakdown.get('output_rate', 0.0):.2f}/1M → {format_cost(breakdown.get('output_krw', 0.0))}"
    )
    embed.add_field(name=f"토큰 내역  (캐시 적중 {cache_hit:.1f}%)", value=token_desc, inline=False)
    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.add_field(name="발생 비용", value=f"{format_cost(turn_cost)}  (≈ ${breakdown['total_usd']:.4f})", inline=True)
    embed.add_field(name="누적 비용", value=format_cost(total_cost), inline=True)
    return embed


def build_image_gen_cost_embed(label: str, model_id: str, cost_breakdown: dict, turn_cost: float, total_cost: float,
                                extra_fields: list = None) -> discord.Embed:
    """
    이미지 생성 비용을 Discord Embed로 조립.

    Args:
        label (str): 작업 이름 (예: "이미지 생성 — portrait")
        model_id (str): 이미지 모델 식별자
        cost_breakdown (dict): calculate_image_gen_cost() 반환값
        turn_cost (float): 이번 호출 총 비용 (KRW)
        total_cost (float): 누적 비용 (KRW)
        extra_fields (list): [(name, value, inline), ...] 추가 필드 목록

    Returns:
        discord.Embed
    """
    embed = discord.Embed(title="🎨 생성 비용 보고", color=0x9B59B6)
    embed.add_field(name="작업", value=label, inline=False)
    embed.add_field(name="모델", value=model_id, inline=False)
    token_desc = (
        f"입력 → {format_cost(cost_breakdown['input_krw'])}\n"
        f"이미지 출력 → {format_cost(cost_breakdown['image_krw'])}"
    )
    if cost_breakdown.get("text_krw", 0) > 0:
        token_desc += f"\n텍스트 출력 → {format_cost(cost_breakdown['text_krw'])}"
    embed.add_field(name="토큰 비용 내역", value=token_desc, inline=False)
    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.add_field(name="발생 비용", value=f"{format_cost(turn_cost)}  (≈ ${cost_breakdown['total_usd']:.4f})", inline=True)
    embed.add_field(name="누적 비용", value=format_cost(total_cost), inline=True)
    return embed


def build_compression_cost_embed(label: str, in_tokens: int, cached_tokens: int, out_tokens: int,
                                  turn_cost: float, total_cost: float) -> discord.Embed:
    """
    기억 압축 비용을 Discord Embed로 조립.

    Args:
        label (str): 작업 이름 (예: '자동 기억 압축', '수동 기억 압축')
        in_tokens (int): 입력 토큰 수
        cached_tokens (int): 캐시 적중 토큰 수
        out_tokens (int): 출력 토큰 수
        turn_cost (float): 이번 호출 비용 (KRW)
        total_cost (float): 누적 비용 (KRW)

    Returns:
        discord.Embed
    """
    in_tokens = int(in_tokens or 0)
    cached_tokens = int(cached_tokens or 0)
    out_tokens = int(out_tokens or 0)
    embed = discord.Embed(title="🧠 기억 압축 비용 보고", color=0x2ECC71)
    embed.add_field(name="작업", value=label, inline=False)
    embed.add_field(name="입력", value=f"{in_tokens:,} 토큰  (캐시 {cached_tokens:,})", inline=True)
    embed.add_field(name="출력", value=f"{out_tokens:,} 토큰", inline=True)
    embed.add_field(name="발생 비용", value=format_cost(turn_cost), inline=True)
    embed.add_field(name="누적 비용", value=format_cost(total_cost), inline=False)
    return embed


def build_turn_cost_embed(turn_number: int, cost_log: list, total_cost: float) -> discord.Embed:
    """
    한 턴(PROCEED 직전)의 누적 비용을 배치 보고하는 Discord Embed 조립.

    Args:
        turn_number (int): 현재 진행 턴 번호 (session.turn_count + 1)
        cost_log (list): [{"label": str, "cost": float}, ...] 형태의 항목 목록
        total_cost (float): session.total_cost 누적값 (KRW)

    Returns:
        discord.Embed
    """
    embed = discord.Embed(title=f"🎲 턴 진행 비용  ·  #{turn_number}", color=0xE67E22)
    total_turn_cost = sum(entry.get("cost", 0.0) for entry in cost_log)
    desc_lines = [f"• {entry.get('label', '?')}: **{format_cost(entry.get('cost', 0.0))}**" for entry in cost_log]
    embed.description = "\n".join(desc_lines) if desc_lines else "(항목 없음)"
    embed.add_field(name="턴 소계", value=format_cost(total_turn_cost), inline=True)
    embed.add_field(name="누적 비용", value=format_cost(total_cost), inline=True)
    return embed


def calculate_storage_cost(model_id: str, cache_storage_tokens: int, duration_seconds: float) -> float:
    """
    캐시 보관 시간을 초 단위에서 분 단위로 반올림하여 스토리지 과금액을 원화(KRW)로 산출.
    """
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])

    # NOTE: 초 단위에서 분 단위로 반올림 (예: 15분 45초 -> 16분) 수행.
    storage_minutes = round(duration_seconds / 60.0)

    cost_usd = (cache_storage_tokens / 1_000_000) * (rates["CACHE_STORAGE_PER_HOUR"] / 60.0) * storage_minutes
    return cost_usd * EXCHANGE_RATE


def calculate_cost(model_id: str, input_tokens=0, output_tokens=0, cached_read_tokens=0, cache_storage_tokens=0,
                   storage_hours=0) -> float:
    """
    API 사용량을 기반으로 과금액(USD) 산출.

    입력, 출력 토큰 외에도 캐시 유지 비용 및 할인율을 종합적으로 합산하여 재무적 모니터링 지원.

    Args:
        model_id (str): 사용된 Gemini 모델 식별자
        input_tokens (int): 입력 토큰 수
        output_tokens (int): 출력 토큰 수
        cached_read_tokens (int): 캐시에서 읽어온 토큰 수
        cache_storage_tokens (int): 저장된 캐시 토큰 수
        storage_hours (int): 캐시 유지 시간(시간 단위)

    Returns:
        float: 산출된 총 비용 (USD)
    """
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cached_read_tokens = cached_read_tokens or 0
    cache_storage_tokens = cache_storage_tokens or 0
    storage_hours = storage_hours or 0

    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    actual_input_tokens = max(0, input_tokens - cached_read_tokens)

    cost = 0.0
    cost += (actual_input_tokens / 1_000_000) * rates["INPUT"]
    cost += (output_tokens / 1_000_000) * rates["OUTPUT"]
    cost += (cached_read_tokens / 1_000_000) * rates["CACHE_READ"]
    cost += (cache_storage_tokens / 1_000_000) * rates["CACHE_STORAGE_PER_HOUR"] * storage_hours
    return cost
