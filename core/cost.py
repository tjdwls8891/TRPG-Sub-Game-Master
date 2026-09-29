# 비용 산출 및 포맷팅 유틸리티 — 토큰 단가 계산, 캐시 스토리지 정산
import discord
from .constants import DEFAULT_MODEL, EXCHANGE_RATE, PRICING_1M, IMAGE_MODEL


# ========== [비용 산출 및 포맷팅 유틸리티] ==========


def extract_token_usage(meta):
    """usage_metadata에서 과금 대상 토큰을 추출한다.

    ⚠️ 중요 — thinking 모델의 사고 토큰 집계:
        gemini-3-flash-preview 등 thinking 계열 모델은 내부 사고 토큰을
        candidates_token_count와 **별개인** thoughts_token_count로 반환한다.
        사고 토큰은 눈에 보이지 않지만 출력 토큰과 **동일 요율**로 과금되므로,
        candidates_token_count만 집계하면 실제 청구액보다 과소 계상된다.
        따라서 출력 토큰은 반드시 (candidates + thoughts)로 합산해야 한다.

    Args:
        meta: response.usage_metadata (None 허용)

    Returns:
        (in_tokens, out_tokens, cached_tokens, thought_tokens)
        out_tokens는 사고 토큰이 합산된 값이며, thought_tokens는 그중
        사고분만 따로 담아 비용 분석·예측 모델 산정에 쓸 수 있게 한다.
    """
    if meta is None:
        return 0, 0, 0, 0
    in_tokens      = getattr(meta, "prompt_token_count", 0) or 0
    visible_tokens = getattr(meta, "candidates_token_count", 0) or 0
    thought_tokens = getattr(meta, "thoughts_token_count", 0) or 0
    cached_tokens  = getattr(meta, "cached_content_token_count", 0) or 0
    return in_tokens, visible_tokens + thought_tokens, cached_tokens, thought_tokens


def format_cost(cost_krw: float) -> str:
    """
    원화(KRW)로 환산된 비용을 소수점 셋째 자리에서 반올림하여 UI 출력용 포맷으로 변환.
    """
    return f"₩{cost_krw:.2f}"


def accrue(session, krw: float = 0.0, usd: float = None):
    """세션 비용을 누적한다.

    원화만 쌓으면 환율이 바뀔 때 과거분이 왜곡된다. 청구 근거는 달러로
    남기고, 원화는 그 시점의 환율로 환산한 값을 함께 둔다.

    usd를 주지 않으면 현재 환율로 역산한다. 정확한 값을 원하면
    breakdown["total_usd"]를 직접 넘길 것.
    """
    if usd is None:
        usd = (krw / EXCHANGE_RATE) if EXCHANGE_RATE else 0.0
    if krw is None or krw == 0.0:
        krw = usd * EXCHANGE_RATE

    session.total_cost = float(getattr(session, "total_cost", 0.0) or 0.0) + krw
    session.total_usd = float(getattr(session, "total_usd", 0.0) or 0.0) + usd
    return session.total_cost


def usd_to_krw(usd: float) -> float:
    """달러를 원화로. 표시 직전에만 쓴다."""
    return float(usd or 0.0) * EXCHANGE_RATE


def format_usd(usd: float) -> str:
    """달러 표기. 마스터 채널 전용."""
    return f"${usd:,.6f}" if abs(usd) < 0.01 else f"${usd:,.4f}"


def calculate_text_gen_cost_breakdown(model_id: str, input_tokens: int = 0, output_tokens: int = 0,
                                       cached_read_tokens: int = 0) -> dict:
    """
    텍스트 생성 모델 호출 비용을 항목별로 분해하여 KRW로 반환.

    캐시 적중분과 신규 입력분의 단가가 다르고(예: $0.50 vs $0.05/1M), 출력 단가($3/1M)와도
    분리 보고해야 GM이 어디서 비용이 새는지 즉시 진단할 수 있다.

    Args:
        model_id (str): 사용된 모델 식별자
        input_tokens (int): 응답 메타의 prompt_token_count (캐시 적중분 포함)
        output_tokens (int): 출력 토큰 (candidates + thoughts 합산 — extract_token_usage 참조)
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


def calculate_upload_cost(model_id: str, input_tokens=0, output_tokens=0,
                          cached_read_tokens=0, store_hours: float = 0.0) -> float:
    """
    API 사용량을 기반으로 업로드 및 생성 과금액을 원화(KRW)로 산출.

    Args:
        store_hours: 캐시 유지 시간(시간). 0보다 크면 저장 비용을 합산한다.
            캐시 생성은 업로드(입력) 비용과 유지 비용이 함께 발생하는데,
            유지분이 빠져 있어 실제보다 적게 계산됐다.

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
    if store_hours > 0:
        cost_usd += (input_tokens / 1_000_000) * rates.get(
            "CACHE_STORAGE_PER_HOUR", 0.0) * store_hours

    # 호출부 호환을 위해 원화를 반환하되, USD는 별도 함수로 얻는다.
    return cost_usd * EXCHANGE_RATE


def calculate_upload_cost_usd(model_id: str, input_tokens: int = 0,
                              output_tokens: int = 0, cached_read_tokens: int = 0,
                              store_hours: float = 0.0) -> float:
    """캐시 업로드·유지 비용을 달러로.

    청구 근거는 달러다. 원화만 다루면 환율이 바뀔 때 과거분이 왜곡된다.
    """
    return calculate_upload_cost(
        model_id, input_tokens=input_tokens, output_tokens=output_tokens,
        cached_read_tokens=cached_read_tokens,
        store_hours=store_hours) / EXCHANGE_RATE


def calculate_storage_cost_usd(model_id: str, tokens: int, hours: float) -> float:
    """캐시 보관비를 달러로."""
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    return (tokens / 1_000_000) * rates.get("CACHE_STORAGE_PER_HOUR", 0.0) * hours


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


def format_breakdown(entry: dict) -> str:
    """호출 하나의 비용을 계산식으로 펼친다.

    기획 규정 — 캐시입력토큰×단가 + 순수입력토큰×단가 + 출력토큰×단가
    + 기타 발생 비용의 구조로 표기해 어디서 비용이 나는지 드러낸다.
    """
    in_t = int(entry.get("in") or 0)
    cached = int(entry.get("cached") or 0)
    out_t = int(entry.get("out") or 0)
    fresh = max(in_t - cached, 0)
    model = entry.get("model") or DEFAULT_MODEL
    rates = PRICING_1M.get(model, PRICING_1M[DEFAULT_MODEL])

    parts = []
    if cached:
        krw = (cached / 1_000_000) * rates["CACHE_READ"] * EXCHANGE_RATE
        parts.append(f"캐시 `{cached:,}`×${rates['CACHE_READ']} = {krw:,.2f}원")
    if fresh:
        krw = (fresh / 1_000_000) * rates["INPUT"] * EXCHANGE_RATE
        parts.append(f"입력 `{fresh:,}`×${rates['INPUT']} = {krw:,.2f}원")
    if out_t:
        krw = (out_t / 1_000_000) * rates["OUTPUT"] * EXCHANGE_RATE
        parts.append(f"출력 `{out_t:,}`×${rates['OUTPUT']} = {krw:,.2f}원")

    extra = entry.get("extra_krw") or 0.0
    if extra:
        parts.append(f"{entry.get('extra_label', '기타')} = {extra:,.2f}원")

    cost = entry.get("cost", 0.0)
    if not parts:
        return f"**{format_cost(cost)}**"
    return "\n".join(f"· {x}" for x in parts) + f"\n**합 {format_cost(cost)}**"


def build_turn_cost_embed(turn_number: int, cost_log: list, total_cost: float,
                          *, total_ink: int = None,
                          total_usd: float = None,
                          free_krw: float = 0.0,
                          settlement=None) -> discord.Embed:
    """
    한 턴의 비용을 호출별로 분해해 보고한다(마스터 채널 전용).

    각 호출을 계산식으로 펼쳐 캐시·신규입력·출력의 기여를 드러낸다.
    합계에도 같은 구조를 적용해, 소계가 어떻게 나왔는지 추적할 수 있다.

    Args:
        turn_number: 현재 진행 턴 번호
        cost_log: [{"label", "cost", "in"?, "cached"?, "out"?, "model"?, "manifest"?}, ...]
            — WP-G: session.turn_cost_log(호환 전용 표시 버퍼). 금액 권위가 아니다.
        total_cost: session.total_cost 호환 미러 (KRW, 참고)
        total_ink: 누적 잉크. 원 단위 누적을 변환하지 않고 턴별 잉크를 더한 값.
        settlement: (WP-D) 정상 자동 턴의 불변 TurnSettlement. 주어지면 턴 청구액은
            Settlement의 charge_ink_per_user/player_billable_cost_krw만 표시한다
            (재환산·재반올림 금지 — 청구 금액의 단일 출처). 호출 내역은 참고용.
    """
    embed = discord.Embed(
        title=f"🎲 턴 비용 리포트 · #{turn_number}",
        description=f"AI 호출 **{len(cost_log)}건**",
        color=0xE67E22,
    )
    total_turn_cost = sum(entry.get("cost", 0.0) for entry in cost_log)
    total_in = total_out = total_cached = 0
    merged_manifest: list = []

    for entry in cost_log:
        label = entry.get("label", "?")
        in_t = entry.get("in")
        if in_t is not None:
            total_in += int(in_t or 0)
            total_cached += int(entry.get("cached", 0) or 0)
            total_out += int(entry.get("out", 0) or 0)
        embed.add_field(name=f"▫️ {label}",
                        value=format_breakdown(entry)[:1020], inline=False)

        for m in (entry.get("manifest") or []):
            if m not in merged_manifest:
                merged_manifest.append(m)

    if merged_manifest:
        manifest_text = "\n".join(f"• {m}" for m in merged_manifest)
        embed.add_field(name="📥 입력에 주입된 정보 (온디맨드)",
                        value=manifest_text[:1020], inline=False)

    # 합계도 같은 구조로 — 어느 항목이 얼마를 차지했는지 보인다.
    if total_in or total_out:
        rates = PRICING_1M[DEFAULT_MODEL]
        fresh = max(total_in - total_cached, 0)
        c_krw = (total_cached / 1_000_000) * rates["CACHE_READ"] * EXCHANGE_RATE
        i_krw = (fresh / 1_000_000) * rates["INPUT"] * EXCHANGE_RATE
        o_krw = (total_out / 1_000_000) * rates["OUTPUT"] * EXCHANGE_RATE
        embed.add_field(
            name="🧾 토큰 합계",
            value=(f"· 캐시 `{total_cached:,}` = {c_krw:,.2f}원\n"
                   f"· 입력 `{fresh:,}` = {i_krw:,.2f}원\n"
                   f"· 출력 `{total_out:,}` = {o_krw:,.2f}원"),
            inline=False)

    # ink는 cost를 임포트하므로 여기서 지연 임포트한다.
    if settlement is not None:
        _users = len(getattr(settlement, "billing_user_ids", ()) or ())
        embed.add_field(
            name="🧮 턴 청구(Settlement)",
            value=(f"**{format_cost(settlement.player_billable_cost_krw)}**\n"
                   f"= 1인당 {settlement.charge_ink_per_user}잉크 × {_users}명\n"
                   f"(호출 내역 합계 {format_cost(total_turn_cost)} · 참고)"),
            inline=True)
    else:
        from .ink import cost_to_ink
        embed.add_field(name="🧮 턴 소계",
                        value=f"**{format_cost(total_turn_cost)}**\n"
                              f"= {cost_to_ink(total_turn_cost)}잉크",
                        inline=True)
    # WP-G(AUD-033): 서로 다른 우주를 등식(=)으로 잇지 않는다. 플레이어 청구는 Settlement 파생
    #   잉크, 원화/달러 누적은 호환 미러(집계 범위가 서로 다름)로 각각 따로 표기한다.
    #   권위 있는 제공자 비용은 `!사용량`(CostLedger 기록값)으로 본다.
    lines = []
    if total_ink is not None:
        lines.append(f"턴 청구 **{total_ink:,}잉크**")
    lines.append(f"비용 미러 {format_cost(total_cost)}")
    if total_usd is not None:
        lines.append(f"USD 미러 {format_usd(total_usd)}")
    if free_krw:
        # 무료 제공분은 청구액과 구분해 표기한다. 섞으면 어느 쪽이
        # 플레이어 부담인지 알 수 없다.
        lines.append(f"(무료 {format_cost(free_krw)})")
    lines.append("(미러는 참고값 · 권위: `!사용량`)")
    embed.add_field(name="Σ 누적", value="\n".join(lines), inline=True)
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


# ══════════════════════════════════════════════════════════════
#  WP-F — 단위를 이름·키워드로 강제한 캐시 비용 헬퍼 (PF-11 단위 혼동 제거)
# ══════════════════════════════════════════════════════════════
#  calculate_storage_cost(…, duration_seconds)는 '초', calculate_storage_cost_usd(…, hours)는
#  '시간'을 받는다. 이름이 비슷해 3600배 오차가 가능했다(d006f). 캐시 생애주기 서비스는
#  아래 두 함수만 쓴다 — 단위는 키워드 전용 인자 이름으로 호출부에 드러난다.

def cache_create_cost_usd(model_id: str, *, tokens: int) -> float:
    """캐시 생성(업로드 입력) 비용 — 달러. 저장 비용은 포함하지 않는다."""
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    return (max(0, int(tokens or 0)) / 1_000_000) * rates["INPUT"]


def cache_storage_cost_usd(model_id: str, *, tokens: int, seconds: float) -> float:
    """실제 경과(초) 기준 캐시 보관 비용 — 달러. 반올림하지 않는다."""
    rates = PRICING_1M.get(model_id, PRICING_1M[DEFAULT_MODEL])
    return ((max(0, int(tokens or 0)) / 1_000_000)
            * rates.get("CACHE_STORAGE_PER_HOUR", 0.0) * (max(0.0, float(seconds)) / 3600.0))


# ══════════════════════════════════════════════════════════════
#  WP-F gate patch — 캐시 창 책임액의 단일 canonical 공식 (POLICY-CACHE-03)
# ══════════════════════════════════════════════════════════════
#  선불 예상(estimate)과 종료 시 실제 책임(actual)은 **같은** 원시 함수·단위·반올림으로
#  계산한다. 차이는 저장 시간(계획 TTL vs 실제 경과)과 토큰 실측값뿐이어야 한다.
#    · 단가: cache_create_cost_usd / cache_storage_cost_usd (초 단위, 반올림 없음)
#    · 통화: USD 합산 → × EXCHANGE_RATE (한 번)
#    · 잉크: cost_to_ink 를 **합계에 한 번**(구성요소별 올림 금지)

def cache_window_responsibility_usd(model_id: str, *, create_tokens: int,
                                    storage: list) -> float:
    """생성(업로드) 1회 + 저장 구간들 [(tokens, seconds), …] 의 책임액(USD)."""
    usd = cache_create_cost_usd(model_id, tokens=create_tokens)
    for tokens, seconds in storage or []:
        usd += cache_storage_cost_usd(model_id, tokens=tokens, seconds=seconds)
    return usd


def cache_window_estimate_usd(model_id: str, *, tokens: int, planned_seconds: float) -> float:
    """선불 예상 — 계획 TTL 동안 같은 토큰으로 유지된다고 가정한 canonical 책임액."""
    return cache_window_responsibility_usd(
        model_id, create_tokens=tokens, storage=[(tokens, planned_seconds)])


def cache_usd_to_ink(usd: float) -> int:
    """캐시 책임액(USD)의 잉크 환산 — 유일한 반올림 경계."""
    from .ink import cost_to_ink
    return cost_to_ink(float(usd) * EXCHANGE_RATE)


# ══════════════════════════════════════════════════════════════
#  WP-G — 레거시 회계 소비자 전환: 권위 원천 조회 헬퍼
# ══════════════════════════════════════════════════════════════
#  session.total_cost / total_usd / turn_cost_log 는 호환 미러(표시 참고)일 뿐이며
#  어떤 비즈니스 규칙도 이것을 독립 재무 진실로 읽지 않는다(AUD-033).
#    · 제공자 비용 권위  = CostLedger — 기록 당시의 KRW/USD 그대로(현재 환율로 재해석하지 않는다)
#    · 플레이어 잉크 권위 = Settlement(턴, total_ink_spent 미러) + 캐시 창 저널(선불/환급)
#                           + 해석 청구 저널 — 각 저널/미러를 그대로 읽는다
#  두 우주는 서로 환산 관계가 아니므로 등식(=)으로 잇지 않는다.

AUTO_COST_BASIS = "ledger"   # gm_cost_baseline 이 CostLedger 세션 합계 스냅샷임을 표시


def provider_cost_summary(bot, session_id=None, *, rows=None) -> dict | None:
    """CostLedger provider 비용 합계(strict). 원장이 없으면 None, 손상이면 예외.

    Returns: {"count", "usd", "krw", "by_hint": {billing_hint: {"count","usd","krw"}}}
    """
    from . import cost_ledger as CL
    if rows is None:
        ledger = CL.get_ledger(bot)
        if ledger is None:
            return None
        rows = ledger.list_cost_events_strict(session_id=session_id)
    out = {"count": 0, "usd": 0.0, "krw": 0.0, "by_hint": {}}
    for r in rows:
        if session_id is not None and r.get("session_id") != session_id:
            continue
        usd = float(r.get("cost_usd") or 0.0)
        krw = float(r.get("cost_krw") or 0.0)
        hint = r.get("billing_hint") or CL.HINT_UNKNOWN
        b = out["by_hint"].setdefault(hint, {"count": 0, "usd": 0.0, "krw": 0.0})
        for d in (out, b):
            d["count"] += 1
            d["usd"] += usd
            d["krw"] += krw
    return out


def provider_cost_by_session(bot) -> tuple:
    """전체 CostLedger(strict)를 session_id 별로 합산한다. Returns: ({sid: {"usd","krw"}}, 행 수)."""
    from . import cost_ledger as CL
    ledger = CL.get_ledger(bot)
    rows = ledger.list_cost_events_strict() if ledger is not None else []
    per_sid: dict = {}
    for r in rows:
        b = per_sid.setdefault(r.get("session_id"), {"usd": 0.0, "krw": 0.0})
        b["usd"] += float(r.get("cost_usd") or 0.0)
        b["krw"] += float(r.get("cost_krw") or 0.0)
    return per_sid, len(rows)


def player_ink_summary(session) -> dict:
    """세션에서 플레이어에게 실제로 청구·환급된 잉크(전 payer 합). 각 권위 기록을 그대로 읽는다.

    turn_ink        — Settlement 파생 미러(CommitCoordinator만 씀)
    cache_prepaid   — 선불이 적용된 캐시 창(WINDOW_PREPAID)의 payer 선불 합
    cache_refunded  — 정산 완료 창(WINDOW_SETTLED)의 환급 합
    interpretation  — 계정 적용이 끝난 해석 청구(CHARGED)의 payer 합
    net             — 위 합(턴 + 선불 − 환급 + 해석). 읽기 실패 항목은 None, net 도 None.
    """
    from . import cache_lifecycle as CLC
    from . import interpretation_billing as IB
    sid = getattr(session, "session_id", None)
    out = {"turn_ink": int(getattr(session, "total_ink_spent", 0) or 0),
           "cache_prepaid": None, "cache_refunded": None, "interpretation": None, "net": None}
    try:
        jv = CLC.view(sid)
        pre = ref = 0
        for w in jv.windows.values():
            if w.get("prepaid"):
                pre += sum(int(v) for v in ((w.get("opened") or {}).get("prepay") or {}).values())
            if w.get("settled") and w.get("settle_intent"):
                ref += sum(int(v) for v in (w["settle_intent"].get("refund") or {}).values())
        out["cache_prepaid"], out["cache_refunded"] = pre, ref
    except Exception as e:  # noqa: BLE001
        print(f"[사용량] 캐시 저널 읽기 실패: {type(e).__name__}: {e}")
    try:
        bv = IB.view(sid)
        out["interpretation"] = sum(
            sum(int(v) for v in (ev.get("payers") or {}).values())
            for cid, ev in bv.intents.items() if cid in bv.charged)
    except Exception as e:  # noqa: BLE001
        print(f"[사용량] 해석 청구 저널 읽기 실패: {type(e).__name__}: {e}")
    if None not in (out["cache_prepaid"], out["cache_refunded"], out["interpretation"]):
        out["net"] = (out["turn_ink"] + out["cache_prepaid"] - out["cache_refunded"]
                      + out["interpretation"])
    return out


def _session_provider_krw(bot, session) -> float:
    s = provider_cost_summary(bot, getattr(session, "session_id", None))
    return 0.0 if s is None else float(s["krw"])


def mark_auto_mode_start(bot, session) -> None:
    """자동 모드 활성화 시점의 CostLedger 세션 provider 비용(KRW 기록값)을 기준으로 잡는다."""
    try:
        base = _session_provider_krw(bot, session)
    except Exception as e:  # noqa: BLE001 — 원장 손상: 다음 점검에서 재기준(상한은 fail-closed)
        print(f"[자동 비용 상한] 기준 산정 실패: {type(e).__name__}: {e}")
        session.gm_cost_baseline = 0.0
        session.gm_cost_basis = ""
        return
    session.gm_cost_baseline = base
    session.gm_cost_basis = AUTO_COST_BASIS


def auto_mode_used_krw(bot, session) -> float:
    """자동 모드 활성화 이후 이 세션에서 발생한 provider 비용(CostLedger, KRW 기록값).

    운영 예산 규칙(OPERATIONAL_BUDGET_OR_CAP)의 입력 — 플레이어 청구액이 아니다.
    WP-G 이전 세션(기준이 레거시 total_cost 우주)은 첫 점검 때 현재 원장 합계로 재기준한다.
    원장 손상이면 예외를 올린다(호출자가 fail-closed 처리).
    """
    total = _session_provider_krw(bot, session)
    if getattr(session, "gm_cost_basis", "") != AUTO_COST_BASIS:
        session.gm_cost_baseline = total
        session.gm_cost_basis = AUTO_COST_BASIS
    return max(0.0, total - float(getattr(session, "gm_cost_baseline", 0.0) or 0.0))


def auto_cost_cap_reached(bot, session) -> bool:
    """자동 모드 비용 상한 도달 여부. 원장을 읽을 수 없으면 도달로 본다(fail-closed)."""
    cap = getattr(session, "gm_cost_cap_krw", None)
    if cap is None:
        return False
    try:
        return auto_mode_used_krw(bot, session) >= cap
    except Exception as e:  # noqa: BLE001
        print(f"[자동 비용 상한] CostLedger 읽기 실패 — 상한 도달로 처리: {type(e).__name__}: {e}")
        return True
