# 디스코드 UI 클래스 — 주사위 뷰, 채널 삭제 뷰, 세션 메모리 정리
import random

import discord

from .io import save_session_data


# ========== [채널 관리 UI 및 유틸리티] ==========

def _cleanup_session_memory(bot, channel_id: int):
    """
    삭제되는 채널이 현재 활성화된 세션에 포함되어 있을 경우
    메모리 참조 에러를 방지하기 위해 딕셔너리에서 데이터를 안전하게 해제.

    WARNING: 채널 삭제 시 파이썬 가비지 컬렉터가 객체를 온전히 수거할 수 있도록
    반드시 메모리 참조를 끊어주는 메모리 누수(Memory Leak) 방지 로직.
    """
    if channel_id in bot.active_sessions:
        session = bot.active_sessions.pop(channel_id)
        other_id = session.game_ch_id if channel_id == session.master_ch_id else session.master_ch_id
        if other_id in bot.active_sessions and bot.active_sessions[other_id] is session:
            bot.active_sessions.pop(other_id)


class ChannelSelect(discord.ui.Select):
    """
    채널 삭제 대상 선택을 위한 드롭다운 UI 컴포넌트 클래스.
    """

    def __init__(self, options):
        super().__init__(
            placeholder="삭제할 카테고리/채널을 선택하세요 (다중 선택 가능)",
            min_values=1,
            max_values=len(options),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selected_values = self.values
        await interaction.response.defer()


class ChannelDeleteView(discord.ui.View):
    """
    필터링된 더미 세션 채널 및 카테고리의 일괄 삭제를 돕는 UI 뷰어 클래스.

    NOTE: 디스코드 API의 SelectOption 최대 개수 제한으로 인해 노출되는 항목을
    최대 25개로 슬라이싱하여 안정성 확보.
    """

    def __init__(self, bot, ctx, target_items):
        super().__init__(timeout=120.0)
        self.bot = bot
        self.ctx = ctx
        self.selected_values = []
        self.target_items = target_items

        options = []
        for item_id, item in list(target_items.items())[:25]:  # API 한계상 최대 25개까지만 노출
            label = f"📁 {item.name}" if isinstance(item, discord.CategoryChannel) else f"💬 {item.name}"
            options.append(discord.SelectOption(label=label, value=str(item_id)))

        self.select = ChannelSelect(options)
        self.add_item(self.select)

    @discord.ui.button(label="선택 항목 영구 삭제", style=discord.ButtonStyle.danger, row=1)
    async def delete_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if interaction.user != self.ctx.author:
            return await interaction.response.send_message("명령어 실행자만 조작할 수 있습니다.", ephemeral=True)

        if not self.selected_values:
            return await interaction.response.send_message("삭제할 항목을 먼저 선택해 주십시오.", ephemeral=True)

        await interaction.response.send_message("⏳ 채널 연쇄 삭제 및 메모리 정리를 시작합니다...", ephemeral=True)

        deleted_count = 0
        for item_id_str in self.selected_values:
            item_id = int(item_id_str)
            item = self.target_items.get(item_id)

            if not item:
                continue

            try:
                if isinstance(item, discord.CategoryChannel):
                    for channel in item.channels:
                        _cleanup_session_memory(self.bot, channel.id)
                        await channel.delete()
                        deleted_count += 1
                    await item.delete()
                    deleted_count += 1
                elif isinstance(item, discord.TextChannel):
                    _cleanup_session_memory(self.bot, item.id)
                    await item.delete()
                    deleted_count += 1
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"⚠️ 채널 삭제 오류: {e}")

        for child in self.children:
            child.disabled = True

        await interaction.message.edit(content=f"✅ 연쇄 삭제 완료: 총 {deleted_count}개의 카테고리 및 채널이 정리되었습니다.", view=self)
        self.stop()


# ========== [디스코드 UI 클래스(Views)] ==========
class GeneralDiceView(discord.ui.View):
    """
    능력치에 구애받지 않는 일반 주사위(N면체) 및 임의 목표값 판정을 위한 UI 뷰어.

    NOTE: 버튼 클릭 시 판정 결과가 단순히 채팅으로 출력되는 것에 그치지 않고,
    session.current_turn_logs에 직렬화되어 AI의 다음 턴 프롬프트에 자동으로 연동됨.

    Args:
        target_uid (str): 주사위를 굴릴 자격을 가진 플레이어의 디스코드 ID
        max_val (int): 주사위의 최대 눈금 수
        weight (int): 판정 결과 또는 기준치에 합산될 추가 가중치
        target_val (int, optional): 성공/실패를 판정할 기준 목표값
    """

    def __init__(self, bot, target_uid: str, max_val: int, weight: int = 0, target_val: int = None):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_uid = target_uid
        self.max_val = max_val
        self.weight = weight
        self.target_val = target_val

    @discord.ui.button(label="🎲 일반 주사위 굴리기", style=discord.ButtonStyle.secondary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message("> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True)

        result = random.randint(1, self.max_val)

        session = self.bot.active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)

        if self.target_val is None:
            # 기존 일반 주사위 로직
            final_result = result + self.weight
            weight_str = f" (가중치 {self.weight:+d})" if self.weight != 0 else ""
            calc_str = f" ({result}{self.weight:+d})" if self.weight != 0 else ""

            await interaction.response.edit_message(
                content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 일반 다이스 결과{weight_str}: **{final_result}**{calc_str}",
                view=None
            )

            if session:
                session.current_turn_logs.append(
                    f"[{char_name}]: {self.max_val}눈 일반 주사위 굴림{weight_str} -> 최종 결과 {final_result}"
                )
                await save_session_data(self.bot, session)

            await interaction.channel.send(
                f"> 📣 **일반 주사위 결과:** {char_name}의 {self.max_val}면체 주사위 최종 눈은 **{final_result}**입니다.{weight_str}"
            )
        else:
            # 임의 목표값이 부여된 성공/실패 판정 로직
            target_value = self.target_val + self.weight
            is_success = result <= target_value
            result_text = "성공 🟢" if is_success else "실패 🔴"

            weight_str = f" (가중치 {self.weight:+d} 적용)" if self.weight != 0 else ""
            target_str = f"{self.target_val}{self.weight:+d}={target_value}" if self.weight != 0 else f"{self.target_val}"

            await interaction.response.edit_message(
                content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 다이스 결과: **{result}** [목표값: {self.target_val}] 굴림  (기준치: {target_str})",
                view=None
            )

            if session:
                session.current_turn_logs.append(
                    f"[{char_name}]: 목표값 {self.target_val}{weight_str} 판정 (1~{self.max_val}) -> 주사위 {result} ({result_text})"
                )
                await save_session_data(self.bot, session)

            await interaction.channel.send(
                f"> 📣 **판정 결과:** {char_name}의 목표값 {self.target_val} 판정{weight_str} - **{result_text}**"
            )
        return None


class DiceView(discord.ui.View):
    """
    특정 스탯의 기준치를 기반으로 성공/실패를 판정하는 능력치 주사위 UI 뷰어.

    Args:
        target_uid (str): 주사위를 굴릴 자격을 가진 플레이어의 디스코드 ID
        max_val (int): 주사위의 최대 눈금 수
        stat_name (str): 굴림의 기준이 되는 캐릭터 스탯의 이름
        stat_value (int): 스탯의 현재 수치
        weight (int): 기준 목표값에 합산될 보정 가중치
    """

    def __init__(self, bot, target_uid: str, max_val: int, stat_name: str, stat_value: int, weight: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.target_uid = target_uid
        self.max_val = max_val
        self.stat_name = stat_name
        self.stat_value = stat_value
        self.weight = weight

    @discord.ui.button(label="🎲 주사위 굴리기", style=discord.ButtonStyle.primary)
    async def roll_button(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message("이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True)

        result = random.randint(1, self.max_val)

        target_value = self.stat_value + self.weight
        is_success = result <= target_value
        result_text = "성공 🟢" if is_success else "실패 🔴"

        weight_str = f" (가중치 {self.weight:+d} 적용)" if self.weight != 0 else ""
        target_str = f"{self.stat_value}{self.weight:+d}={target_value}" if self.weight != 0 else f"{self.stat_value}"

        await interaction.response.edit_message(
            content=f"> 🎲 <@{self.target_uid}>님의 눈 {self.max_val} 다이스 결과: **{result}** [{self.stat_name}] 굴림  (기준치: {target_str})",
            view=None
        )

        session = self.bot.active_sessions.get(interaction.channel.id)
        char_name = interaction.user.display_name
        if session:
            char_name = session.players.get(self.target_uid, {}).get("name", char_name)
            session.current_turn_logs.append(
                f"[{char_name}]: [{self.stat_name}]{weight_str} 판정 (1~{self.max_val}) -> 주사위 {result} ({result_text})")
            await save_session_data(self.bot, session)

        await interaction.channel.send(
            f"> 📣 **판정 결과:** {char_name}의 [{self.stat_name}] 판정{weight_str} - **{result_text}**"
        )
        return None
