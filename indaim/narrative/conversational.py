# 대화형 안내 = 일반 메시지(content) (P2 #4)
#
# 세션 생성·캐릭터 생성·초기 지식·시작 상황 안내처럼 마스터가 말을 거는 화면은 Embed가 아니라 일반 메시지로 보낸다.
# 기존 화면 조립(Embed 빌더)은 그대로 두고 송출 경계에서 바꾼다 — 제목·본문·이름 없는 이어 쓰기 필드·푸터(진행도)는
# content로, 이름 있는 구조 필드(예상 비용·소지품·능력치 등 정형 데이터)는 데이터 카드 Embed로 남긴다(P2 D — 카드형 허용).
# 단일 메시지 편집·버튼·드롭다운은 호출부가 그대로 쓴다. 낡은 Embed가 남지 않게 편집 때 embeds=[]를 명시한다.
import discord

CONTENT_LIMIT = 1900          # Discord content 2000자 — 여유를 둔다
_CONT_NAMES = ("​", "​", "", "알림")       # 이어 쓰기(이름 없음)·안내 필드 = 대화


def split(embed) -> tuple:
    """(content, card_embed | None). 넘치는 대화 본문은 카드 설명으로 이어 붙인다(자르지 않는다)."""
    if embed is None:
        return "", None
    parts = []
    if embed.title:
        parts.append(f"**{embed.title}**")
    if embed.description:
        parts.append(str(embed.description))
    card_fields = []
    for f in embed.fields:
        if (f.name or "").strip() in _CONT_NAMES or f.name in _CONT_NAMES:
            if f.name == "알림":
                parts.append("**알림**\n" + str(f.value))
            else:
                parts.append(str(f.value))
        else:
            card_fields.append(f)
    footer = getattr(embed.footer, "text", None)
    if footer:
        parts.append(f"-# {footer}")
    content = "\n\n".join(p for p in parts if p and p.strip())
    overflow = ""
    if len(content) > CONTENT_LIMIT:
        cut = content.rfind("\n\n", 0, CONTENT_LIMIT)
        cut = cut if cut > CONTENT_LIMIT // 2 else CONTENT_LIMIT
        content, overflow = content[:cut], content[cut:].strip()
    card = None
    if card_fields or overflow:
        card = discord.Embed(description=overflow[:4000] or None, color=embed.color)
        for f in card_fields:
            card.add_field(name=f.name, value=f.value, inline=f.inline)
        if embed.image and embed.image.url:
            card.set_image(url=embed.image.url)
    elif embed.image and embed.image.url:
        card = discord.Embed(color=embed.color)
        card.set_image(url=embed.image.url)
    return content or "​", card


def send_kwargs(embed, **extra) -> dict:
    content, card = split(embed)
    out = {"content": content}
    if card is not None:
        out["embed"] = card
    out.update(extra)
    return out


def edit_kwargs(embed, **extra) -> dict:
    """편집 인자 — 대화 본문은 content, 카드가 없으면 embeds=[]로 옛 Embed를 지운다."""
    content, card = split(embed)
    out = {"content": content, "embeds": [card] if card is not None else []}
    out.update(extra)
    return out
