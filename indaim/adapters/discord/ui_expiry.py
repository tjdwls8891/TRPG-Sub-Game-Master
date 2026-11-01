# 버튼 유효시간·재시작 승계 (v7.28, 사용자 결정 2026-10-22)
#
# 버튼 화면은 셋으로 나눈다.
#   A. 대답해야 다음으로 넘어가는 화면(세션 생성 단계·시작 상황·초기 지식·열기/업로드 재시도)
#      — 시간 제한 없음. 봇 재시작 뒤에는 저장된 기록(creation_state·resumable_prompts)으로
#        같은 메시지에 새 버튼을 붙인다(core.session_flow.resume_all).
#   B. 확인창(되감기·재서술·열기/닫기 확인) — 의도된 만료 그대로(LifecyclePromptView가 접는다).
#   C. 그 밖의 보조 화면 — 만료되면 버튼을 잠근다(ExpiringView).
# 어느 분류든 처리할 화면이 없는 버튼(만료·재시작으로 죽은 버튼)을 누르면 '상호작용 실패' 대신 안내를 준다.
from __future__ import annotations

import discord

STALE_TEXT = ("⌛ 이 버튼은 만료되었습니다(시간이 지났거나 봇이 다시 시작됐습니다). "
              "GM 스페이스나 원래 화면에서 다시 열어 주십시오.")

ROLL_STALE_TEXT = ("⌛ 이 판정은 봇이 다시 시작되며 사라졌습니다. "
                   "같은 행동을 다시 선언해 주십시오(이번 판정은 청구되지 않습니다).")
ROLL_PREFIX = "roll:"           # 판정 화면 버튼(주사위·이어서 진행) — 승계 대상이 아니라 재선언 안내

_CURRENT: dict = {}        # message_id → 그 메시지에 지금 붙어 있는 ExpiringView(교체된 옛 뷰는 잠그지 않는다)


def is_unhandled_component(bot, interaction) -> bool:
    """이 컴포넌트 상호작용을 받을 뷰·영속 항목이 없는가 — discord.py ViewStore.dispatch_view와 같은 조회."""
    if interaction.type != discord.InteractionType.component:
        return False
    data = interaction.data or {}
    custom_id, ctype = data.get("custom_id"), data.get("component_type")
    if not custom_id or ctype is None:
        return False
    store = getattr(getattr(bot, "_connection", None), "_view_store", None)
    if store is None:
        return False
    for pattern in getattr(store, "_dynamic_items", {}):
        if pattern.fullmatch(custom_id):
            return False
    key = (ctype, custom_id)
    views = getattr(store, "_views", {})
    msg = interaction.message
    if msg is not None and key in views.get(msg.id, {}):
        return False
    return key not in views.get(None, {})


async def answer_stale(bot, interaction) -> bool:
    """처리할 화면이 없는 버튼이면 본인에게만 안내한다. 응답했으면 True."""
    if not is_unhandled_component(bot, interaction):
        return False
    try:
        if not interaction.response.is_done():
            cid = str((getattr(interaction, "data", None) or {}).get("custom_id") or "")
            await interaction.response.send_message(
                ROLL_STALE_TEXT if cid.startswith(ROLL_PREFIX) else STALE_TEXT, ephemeral=True)
            return True
    except Exception:  # noqa: BLE001 — 이미 응답됐거나 만료된 상호작용
        pass
    return False


class ExpiringView(discord.ui.View):
    """C 분류 — 만료되면 그 메시지의 버튼을 잠근다. 보낸 메시지를 bind()로 알려 줘야 한다."""

    def bind(self, message):
        self._expiry_message = message
        if message is not None and getattr(message, "id", None) is not None:
            _CURRENT[message.id] = self
        return self

    async def on_timeout(self):
        msg = getattr(self, "_expiry_message", None)
        if msg is None or _CURRENT.get(getattr(msg, "id", None)) is not self:
            return                                    # 이미 다른 화면으로 바뀐 메시지
        _CURRENT.pop(msg.id, None)
        for child in self.children:
            child.disabled = True
        try:
            await msg.edit(view=self)
        except Exception:  # noqa: BLE001 — 지워졌거나 편집 불가(에페메럴 만료 등)
            pass
