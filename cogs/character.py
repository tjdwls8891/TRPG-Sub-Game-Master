import re
import random
import asyncio
import discord
from discord.ext import commands

# 분리된 코어 유틸리티 모듈을 임포트합니다.
import core


# ========== [능력치 굴림 UI] ==========
class StatRollView(discord.ui.View):
    """
    시나리오 JSON의 ability_stats에 정의된 스탯을 순차적으로 굴려
    결과를 비율에 따라 목표 총합에 맞게 배분하는 UI 뷰어.

    Hamilton 방식(최대 나머지법)으로 정수 배분 오차를 최소화한다.
    """

    def __init__(self, bot, target_uid: str, char_name: str,
                 ability_stats: list, dice_sides: int, target_total: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.target_uid = target_uid
        self.char_name = char_name
        self.ability_stats = ability_stats
        self.dice_sides = dice_sides
        self.target_total = target_total

    @discord.ui.button(label="🎲 능력치 굴리기", style=discord.ButtonStyle.primary)
    async def roll_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_uid:
            return await interaction.response.send_message(
                "> 이 주사위는 당신을 위한 것이 아닙니다!", ephemeral=True
            )

        button.disabled = True
        await interaction.response.edit_message(
            content=(
                f"> 🎲 **{self.char_name}**의 능력치 굴림을 시작합니다...\n"
                f"> 주사위: d{self.dice_sides} × {len(self.ability_stats)}회"
            ),
            view=self
        )

        # ── 순차 굴림 ──
        rolls = []
        for stat_name in self.ability_stats:
            result = random.randint(1, self.dice_sides)
            rolls.append(result)
            await interaction.channel.send(f"> 🎲 **{stat_name}** 주사위 결과: **{result}**")
            await asyncio.sleep(0.8)

        # ── Hamilton 방식 정수 비례 배분 ──
        total_rolls = sum(rolls)
        n = len(self.ability_stats)

        if total_rolls == 0:
            base = self.target_total // n
            extra = self.target_total % n
            final_values = [base + (1 if i < extra else 0) for i in range(n)]
        else:
            raw = [r * self.target_total / total_rolls for r in rolls]
            floor_vals = [int(v) for v in raw]
            remainder = self.target_total - sum(floor_vals)
            # 소수점 내림차순 정렬로 나머지 1씩 분배
            order = sorted(range(n), key=lambda i: raw[i] - floor_vals[i], reverse=True)
            for i in range(remainder):
                floor_vals[order[i]] += 1
            final_values = floor_vals

        # ── 결과 출력 ──
        lines = [
            f"> **{stat}**: {roll} → **{val}**"
            for stat, roll, val in zip(self.ability_stats, rolls, final_values)
        ]
        await interaction.channel.send(
            f"> 📊 **[{self.char_name}] 능력치 배분 완료** "
            f"(합계: **{sum(final_values)}** / 목표: **{self.target_total}**)\n"
            + "\n".join(lines)
        )

        # ── 세션 스탯 적용 ──
        session = self.bot.active_sessions.get(interaction.channel.id)
        if session and self.target_uid in session.players:
            profile = session.players[self.target_uid]["profile"]
            for stat, val in zip(self.ability_stats, final_values):
                if stat in profile:
                    profile[stat] = str(val)
            await core.save_session_data(self.bot, session)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class CharacterCog(commands.Cog):
    """
    플레이어 캐릭터(PC)의 참가 및 프로필 설정, NPC 데이터의 관리,
    그리고 AI 기반 캐릭터 설정 초안 생성을 전담하는 모듈입니다.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="참가")
    async def join_session(self, ctx, char_name: str):
        """
        플레이어를 세션 데이터베이스에 지정한 캐릭터명으로 등록합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 등록할 캐릭터의 이름
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.game_ch_id:
            await ctx.send("이 명령어는 게임 채널에서만 사용할 수 있습니다.")
            return

        user_id_str = str(ctx.author.id)
        base_profile = session.scenario_data.get("pc_template", {}).copy()

        session.players[user_id_str] = {
            "name": char_name,
            "profile": base_profile,
            "appearance": ""
        }
        await core.save_session_data(self.bot, session)

        try:
            await ctx.author.edit(nick=char_name)
        except Exception:
            pass

        await ctx.send(
            f"✅ {ctx.author.mention}님이 **'{char_name}'**(으)로 세션에 참가했습니다!\n"
            f"(진행자(GM)가 설정을 통해 스탯을 배분해 줄 것입니다.)"
        )


    @commands.command(name="설정")
    async def set_profile(self, ctx, char_name: str, key: str, *, value: str):
        """
        특정 캐릭터의 프로필/스탯 속성을 지정한 값으로 갱신합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
            key (str): 갱신할 속성 키
            value (str): 갱신될 데이터 값
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
            return

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")
            return

        player_data = session.players[user_id_str]

        if key not in player_data["profile"]:
            allowed_keys = ", ".join(player_data["profile"].keys())
            await ctx.send(f"⚠️ 해당 시나리오에 없는 항목입니다. (가능한 항목: {allowed_keys})")
            return

        player_data["profile"][key] = value
        await core.save_session_data(self.bot, session)

        game_channel = self.bot.get_channel(session.game_ch_id)
        if game_channel:
            await game_channel.send(f"✅ <@{user_id_str}>의 [{key}] 항목이 '{value}'(으)로 갱신되었습니다.")

    @commands.command(name="증감")
    async def adjust_stat(self, ctx, char_name: str, key: str, *args):
        """
        캐릭터 스탯 수치 증감, 소지 자원 증감, 상태이상 부여·제거를 통합 처리합니다.

        사용법:
            !증감 [이름] [스탯명] [수치]          — 스탯 숫자 증감 (예: +5, -3)
            !증감 [이름] 자원 [아이템명] [수치]   — 소지 자원 증감
            !증감 [이름] 상태 [상태명]             — 상태이상 부여
            !증감 [이름] 상태 -[상태명]            — 상태이상 제거

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 또는 NPC 이름
            key (str): '자원', '상태', 또는 갱신할 스탯 항목명
            *args: key에 따라 달라지는 가변 인자
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)

        # ── 자원 증감 모드 ──
        if key == "자원":
            if len(args) < 2:
                return await ctx.send(
                    "⚠️ 사용법: `!증감 [이름] 자원 [아이템명] [수치]`\n예) `!증감 아서 자원 식량 -2`"
                )
            item_name, amount_str = args[0], args[1]
            try:
                amount = int(amount_str)
            except ValueError:
                return await ctx.send("⚠️ 수치는 정수여야 합니다. (예: 5, -3)")

            if char_name not in session.resources:
                session.resources[char_name] = {}

            old_val = session.resources[char_name].get(item_name, 0)
            new_val = old_val + amount
            session.resources[char_name][item_name] = new_val

            await core.save_session_data(self.bot, session)
            await ctx.send(
                f"✅ {char_name}의 자원 [{item_name}]: {old_val} → {new_val} ({amount:+d})"
            )
            if game_channel:
                await game_channel.send(
                    f"> 📦 **[자원 변동]** {char_name}의 [{item_name}]: {old_val} → {new_val} ({amount:+d})"
                )
            return

        # ── 상태이상 부여·제거 모드 ──
        if key == "상태":
            if len(args) < 1:
                return await ctx.send(
                    "⚠️ 사용법: `!증감 [이름] 상태 [상태명]` / 제거: `!증감 [이름] 상태 -[상태명]`"
                )
            status_text = args[0]

            if char_name not in session.statuses:
                session.statuses[char_name] = []

            if status_text.startswith("-"):
                target = status_text[1:]
                if target in session.statuses[char_name]:
                    session.statuses[char_name].remove(target)
                    await core.save_session_data(self.bot, session)
                    await ctx.send(f"✅ {char_name}의 상태이상 [{target}] 제거 완료.")
                    if game_channel:
                        await game_channel.send(
                            f"> 🔵 **[상태 해제]** {char_name}의 [{target}] 상태이상이 해제되었습니다."
                        )
                else:
                    await ctx.send(f"⚠️ {char_name}에게 [{target}] 상태이상이 없습니다.")
            else:
                if status_text not in session.statuses[char_name]:
                    session.statuses[char_name].append(status_text)
                    await core.save_session_data(self.bot, session)
                    await ctx.send(f"✅ {char_name}에게 상태이상 [{status_text}] 부여 완료.")
                    if game_channel:
                        await game_channel.send(
                            f"> 🔴 **[상태 부여]** {char_name}에게 [{status_text}] 상태이상이 부여되었습니다."
                        )
                else:
                    await ctx.send(f"⚠️ {char_name}에게 이미 [{status_text}] 상태이상이 있습니다.")
            return

        # ── 스탯 수치 증감 모드 (기존 로직) ──
        if len(args) < 1:
            return await ctx.send(
                "⚠️ 사용법: `!증감 [이름] [스탯명] [수치]`\n"
                "  자원 수정: `!증감 [이름] 자원 [아이템명] [수치]`\n"
                "  상태 수정: `!증감 [이름] 상태 [-]상태명`"
            )
        amount_str = args[0]

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        player_data = session.players[user_id_str]

        if key not in player_data["profile"]:
            allowed_keys = ", ".join(player_data["profile"].keys())
            return await ctx.send(f"⚠️ 해당 시나리오에 없는 항목입니다. (가능한 항목: {allowed_keys})")

        try:
            old_val = int(player_data["profile"][key])
        except ValueError:
            return await ctx.send(
                f"⚠️ [{key}] 항목의 현재 값이 순수한 숫자가 아니어서 연산할 수 없습니다. (현재 값: {player_data['profile'][key]})"
            )

        try:
            amount = int(amount_str)
        except ValueError:
            return await ctx.send("⚠️ 변동할 수치는 반드시 숫자 형태여야 합니다. (예: 5, -3)")

        new_val = old_val + amount
        player_data["profile"][key] = str(new_val)
        await core.save_session_data(self.bot, session)

        await ctx.send(f"✅ {char_name}의 [{key}] 수치 연산 완료: {old_val} → {new_val} ({amount:+d})")
        if game_channel:
            await game_channel.send(
                f"> 📢 **[스탯 변동]** {char_name}의 [{key}]이(가) {new_val}(으)로 변경되었습니다. ({old_val}{amount:+d})"
            )


    @commands.command(name="외형")
    async def manage_appearance(self, ctx, char_name: str, *, appearance: str = None):
        """
        특정 캐릭터의 외형 묘사를 설정하거나, 내용을 비워둘 경우 현재 설정된 외형을 확인합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
            appearance (str, optional): 적용할 외형 묘사 텍스트. 생략 시 현재 외형 확인.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        if appearance is None:
            # 외형 확인 로직
            current_appearance = session.players[user_id_str].get("appearance", "설정된 외형이 없습니다.")
            await ctx.send(f"🎭 **{char_name}의 현재 외형**:\n{current_appearance}")
        else:
            # 외형 설정 로직
            session.players[user_id_str]["appearance"] = appearance
            await core.save_session_data(self.bot, session)
            await ctx.send(f"✅ 캐릭터 [{char_name}] 외형 설정 완료 (덮어쓰기):\n{appearance}")

        return None


    @commands.command(name="프로필")
    async def show_profile(self, ctx, char_name: str, target: str = None):
        """
        특정 캐릭터의 스탯·외형·자원·상태이상을 포함한 프로필 카드를 출력합니다.

        기본(인자 없음)은 마스터 채널에, '게임' 인자를 추가하면 게임 채널에 출력합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
            target (str, optional): '게임' 입력 시 게임 채널에 출력
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        player_data = session.players[user_id_str]
        member = ctx.guild.get_member(int(user_id_str))

        embed = discord.Embed(title=f"🎭  {char_name}  —  캐릭터 시트", color=0x2f6dd0)
        if member:
            embed.set_author(
                name=member.display_name,
                icon_url=member.display_avatar.url if member.display_avatar else None
            )
        else:
            embed.set_author(name=char_name)

        # 시나리오 JSON의 profile_secondary_stats 목록에 등록된 항목은 구분선 아래에 전체 폭으로 표시.
        # 나머지 항목은 구분선 위 인라인 3열 격자에 배치.
        secondary_keys = set(session.scenario_data.get("profile_secondary_stats", []))
        profile = player_data.get("profile", {})
        primary_profile   = {k: v for k, v in profile.items() if k not in secondary_keys}
        secondary_profile = {k: v for k, v in profile.items() if k in secondary_keys}

        # ── 1차 스탯 블록 (인라인, 3열 격자) ──
        if primary_profile:
            for key, val in primary_profile.items():
                embed.add_field(name=f"▸ {key}", value=f"```{val}```", inline=True)
            # 3열 정렬을 위한 빈 칸 패딩
            remainder = len(primary_profile) % 3
            if remainder == 1:
                embed.add_field(name="​", value="​", inline=True)
                embed.add_field(name="​", value="​", inline=True)
            elif remainder == 2:
                embed.add_field(name="​", value="​", inline=True)

        # ── 구분선 ──
        embed.add_field(name="", value="─" * 36, inline=False)

        # ── 2차 스탯 블록 (profile_secondary_stats 등록 항목, 전체 폭) ──
        for key, val in secondary_profile.items():
            display_val = val if len(val) <= 1000 else val[:950] + "\n*(생략됨)*"
            embed.add_field(name=f"📋  {key}", value=display_val, inline=False)

        # ── 외형 블록 ──
        appearance = player_data.get("appearance", "")
        if appearance:
            display_appearance = appearance if len(appearance) <= 1000 else appearance[:950] + "\n*(생략됨)*"
            embed.add_field(name="🪞  외형", value=display_appearance, inline=False)

        # ── 소지 자원 블록 ──
        resources = session.resources.get(char_name, {})
        if resources:
            res_lines = [f"`{k}` **×{v}**" for k, v in resources.items()]
            res_text = "  /  ".join(res_lines)
            if len(res_text) > 1000:
                res_text = res_text[:950] + "\n*(생략됨)*"
            embed.add_field(name="🎒  소지 자원", value=res_text, inline=False)
        else:
            embed.add_field(name="🎒  소지 자원", value="*(없음)*", inline=False)

        # ── 상태이상 블록 ──
        statuses = session.statuses.get(char_name, [])
        if statuses:
            stat_text = "  ".join([f"🔴 `{s}`" for s in statuses])
            if len(stat_text) > 1000:
                stat_text = stat_text[:950] + "\n*(생략됨)*"
            embed.add_field(name="⚠️  상태이상", value=stat_text, inline=False)
        else:
            embed.add_field(name="⚠️  상태이상", value="*(없음)*", inline=False)

        embed.set_footer(text=f"세션 {session.session_id}  |  {session.turn_count}턴 경과")

        # ── 출력 채널 결정 ──
        if target == "게임":
            game_channel = self.bot.get_channel(session.game_ch_id)
            if not game_channel:
                return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")
            await game_channel.send(embed=embed)
            await ctx.send(f"✅ {char_name}의 프로필을 게임 채널에 출력했습니다.")
        else:
            await ctx.send(embed=embed)
        return None


    @commands.command(name="엔피씨")
    async def manage_npc(self, ctx, action: str, name: str = None, *, details: str = None):
        """
        NPC 관련 기능(설정, 확인, 삭제, 목록)을 통합 관리합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            action (str): 수행할 작업 (설정, 확인, 삭제, 목록)
            name (str, optional): 대상 NPC 이름
            details (str, optional): 설정할 NPC 세부 내용
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if action == "설정":
            if not name or not details:
                return await ctx.send("⚠️ 사용법: `!npc 설정 [이름] [내용]`")
            session.npcs[name] = {"name": name, "details": details}
            await core.save_session_data(self.bot, session)
            await ctx.send(f"✅ NPC [{name}] 설정 완료 (덮어쓰기):\n{details}")
            return None

        elif action == "확인":
            if not name:
                return await ctx.send("⚠️ 사용법: `!npc 확인 [이름]`")
            if name in session.npcs:
                npc_details = session.npcs[name]["details"]
                await ctx.send(f"📜 **NPC [{name}] 정보**:\n{npc_details}")
            else:
                await ctx.send(f"⚠️ NPC [{name}]을(를) 찾을 수 없습니다.")
            return None

        elif action == "삭제":
            if not name:
                return await ctx.send("⚠️ 사용법: `!npc 삭제 [이름]`")
            if name in session.npcs:
                del session.npcs[name]
                await core.save_session_data(self.bot, session)
                await ctx.send(f"✅ NPC [{name}] 삭제 완료.")
            else:
                await ctx.send(f"⚠️ NPC [{name}]을(를) 찾을 수 없습니다.")
            return None


        elif action == "목록":
            if not session.npcs:
                return await ctx.send("등록된 NPC가 없습니다.")
            embed = discord.Embed(title="📜 등록된 NPC 목록", color=0x2ecc71)
            for npc_name, npc_data in session.npcs.items():
                # NOTE: npc_data에서 설정 텍스트(details)를 추출하는 코드 추가
                details = npc_data.get("details", "설정 없음")
                display_details = details if len(details) <= 1000 else details[
                                                                           :950] + "...\n(※ 텍스트가 너무 길어 생략되었습니다. 전문은 확인 명령어로 확인하세요.)"
                embed.add_field(name=npc_name, value=display_details, inline=False)
            await ctx.send(embed=embed)
            return None
        else:
            await ctx.send("⚠️ 잘못된 행동 인자입니다. (사용 가능: 설정, 확인, 삭제, 목록)")
            return None


    @commands.command(name="능력치")
    async def roll_ability_stats(self, ctx, char_name: str, dice_sides: int, target_total: int):
        """
        시나리오 JSON의 ability_stats에 정의된 스탯을 주사위로 굴려
        비율에 맞게 목표 총합으로 자동 배분합니다.

        게임 채널에 버튼 UI를 전송하고, 해당 플레이어가 버튼을 누르면
        스탯별 주사위를 순차 출력 후 결과를 캐릭터에 자동 적용합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
            dice_sides (int): 각 스탯에 사용할 주사위 면 수
            target_total (int): 모든 스탯 합산 목표값
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        ability_stats = session.scenario_data.get("ability_stats", [])
        if not ability_stats:
            return await ctx.send(
                "⚠️ 시나리오 JSON에 `ability_stats` 항목이 없습니다.\n"
                "예: `\"ability_stats\": [\"체력\", \"정신\", \"민첩\"]`"
            )

        if dice_sides < 2:
            return await ctx.send("⚠️ 주사위 눈 수는 2 이상이어야 합니다.")

        if target_total < len(ability_stats):
            return await ctx.send(
                f"⚠️ 목표 총합({target_total})이 스탯 수({len(ability_stats)})보다 작습니다."
            )

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        profile = session.players[user_id_str]["profile"]
        missing = [s for s in ability_stats if s not in profile]
        if missing:
            return await ctx.send(
                f"⚠️ 다음 스탯이 캐릭터 프로필에 없습니다: {', '.join(missing)}\n"
                f"(pc_template에 해당 스탯이 포함되어 있어야 합니다)"
            )

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        view = StatRollView(self.bot, user_id_str, char_name, ability_stats, dice_sides, target_total)
        stats_str = " / ".join(ability_stats)

        await game_channel.send(
            f"> 🎲 <@{user_id_str}>님, **{char_name}**의 능력치를 굴릴 시간입니다!\n"
            f"> 대상 스탯: **{stats_str}**\n"
            f"> d{dice_sides} × {len(ability_stats)}회 굴림 → 비율에 맞춰 합계 **{target_total}** 자동 배분\n"
            f"> 아래 버튼을 눌러 굴림을 시작하세요.",
            view=view
        )
        await ctx.send(
            f"✅ {char_name}의 능력치 굴림 버튼을 게임 채널에 전송했습니다.\n"
            f"(대상 스탯: {stats_str} / d{dice_sides} / 목표 합계: {target_total})"
        )

    @commands.command(name="설정생성")
    async def generate_character_cmd(self, ctx, char_type: str, char_name: str, *, instruction: str):
        """
        입력된 지시사항을 바탕으로 AI를 호출하여 캐릭터(PC/NPC)의 상세 설정 초안을 생성합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_type (str): 생성할 타입 ('pc' 혹은 'npc')
            char_name (str): 생성할 캐릭터 이름
            instruction (str): 창작 시 반영할 구체적 지시사항
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        char_type = char_type.lower()
        if char_type not in ["pc", "npc"]:
            return await ctx.send("⚠️ 캐릭터 유형은 `pc` 또는 `npc` 중 하나로 입력해주세요.")

        # 1. 정규식을 통한 목표 NPC 추출
        npc_match = re.search(r'엔:([^\s]+)', instruction)
        npc_context_str = "등록된 NPC 정보 없음"

        if npc_match:
            target_npcs = npc_match.group(1).split(',')
            npc_list = [f"- {n}: {session.npcs[n]['details']}" for n in target_npcs if n in session.npcs]
            npc_context_str = "\n".join(npc_list) if npc_list else "일치하는 NPC 정보 없음"
            instruction = re.sub(r'엔:[^\s]+', '', instruction).strip()
        else:
            if session.npcs:
                npc_context_str = "\n".join([f"- {k}: {v['details']}" for k, v in session.npcs.items()])

        # 2. 최근 3턴 로그 추출 (유저-모델 핑퐁 3세트 = 6개)
        recent_logs_list = [f"[{part.role.upper()}]: {part.parts[0].text}" for part in session.raw_logs[-6:]]
        recent_logs_str = "\n\n".join(recent_logs_list) if recent_logs_list else "최근 로그 없음"

        type_kr = "플레이어 캐릭터(PC)" if char_type == "pc" else "NPC"
        await ctx.send(f"⏳ AI가 주변 인물 및 최근 상황을 교차 참조하여 {type_kr} '{char_name}'의 설정 초안을 생성 중입니다...")

        try:
            # 수정된 파라미터 매핑
            response = await core.generate_character_details(
                self.bot, session.scenario_data, char_type, char_name,
                instruction, session.session_id, recent_logs_str, npc_context_str
            )
            generated_text = response.text

            meta = response.usage_metadata
            in_tokens = meta.prompt_token_count
            out_tokens = meta.candidates_token_count
            cached_tokens = getattr(meta, "cached_content_token_count", 0) or 0

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.LOGIC_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            turn_cost = breakdown["total_krw"]
            session.total_cost += turn_cost

            core.write_cost_log(session.session_id, f"설정 초안 생성 ({char_type}/{char_name})",
                                in_tokens, cached_tokens, out_tokens, turn_cost, session.total_cost)

            type_label = "PC" if char_type == "pc" else "NPC"
            ref_label = f"{len(target_npcs)}명 명시 참조" if npc_match else (f"전체 NPC 자동 참조({len(session.npcs)})" if session.npcs else "NPC 참조 없음")
            lines = [
                f"💰 **[비용 보고] 설정 초안 생성**",
                f"- 대상: {type_label} '{char_name}'  /  레퍼런스: {ref_label}",
                f"- 모델: {core.LOGIC_MODEL}",
                f"- 입력 합계: {in_tokens:,} 토큰  (캐시 적중 {cached_tokens:,})",
                f"   · 신규 입력 {breakdown['input_billable_tokens']:,} × ${breakdown['input_rate']:.2f}/1M → {core.format_cost(breakdown['input_krw'])}",
                f"   · 캐시 적중 {breakdown['cache_read_tokens']:,} × ${breakdown['cache_rate']:.2f}/1M → {core.format_cost(breakdown['cache_read_krw'])}",
                f"- 출력 {breakdown['output_tokens']:,} × ${breakdown['output_rate']:.2f}/1M → {core.format_cost(breakdown['output_krw'])}",
                f"- 턴 발생 비용: {core.format_cost(turn_cost)}  ( ≈ ${breakdown['total_usd']:.4f} )",
                f"- 누적 비용: {core.format_cost(session.total_cost)}",
            ]
            report_msg = "\n".join(lines)

            # 콘솔(실행 창) 출력
            print(f"\n[설정 초안 생성 비용 보고] {char_name}")
            print(report_msg.replace("**", ""))

            # 마스터 채널에도 동일 보고 전송
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(report_msg)

            await core.save_session_data(self.bot, session)

            guide_cmd = f"`!외형 {char_name} [내용]`" if char_type == "pc" else f"`!엔피씨 설정 {char_name} [내용]`"
            header = f"💡 **[{char_name}] {type_kr} 설정 초안 생성 완료**\n*아래 내용을 복사하여 자유롭게 수정한 뒤, {guide_cmd} 명령어로 적용하세요.*\n\n"
            full_message = header + generated_text

            if len(full_message) > 2000:
                for i in range(0, len(full_message), 2000):
                    await ctx.send(full_message[i:i + 2000])
            else:
                await ctx.send(full_message)

        except Exception as e:
            await ctx.send(f"⚠️ 설정 초안 생성 중 오류가 발생했습니다: {e}")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수입니다.
    """
    await bot.add_cog(CharacterCog(bot))