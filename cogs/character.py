import discord
from discord.ext import commands

# 분리된 코어 유틸리티 모듈을 임포트합니다.
import core


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
    async def adjust_stat(self, ctx, char_name: str, key: str, amount_str: str):
        """
        특정 캐릭터의 프로필/스탯 속성값이 숫자일 경우, 지정한 수치만큼 더하거나 뺍니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
            key (str): 갱신할 속성 키
            amount_str (str): 변동할 수치 (예: 5, +5, -3)
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

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
                f"⚠️ [{key}] 항목의 현재 값이 순수한 숫자가 아니어서 연산할 수 없습니다. (현재 값: {player_data['profile'][key]})")

        try:
            amount = int(amount_str)
        except ValueError:
            return await ctx.send("⚠️ 변동할 수치는 반드시 숫자 형태여야 합니다. (예: 5, -3)")

        new_val = old_val + amount
        weight_str = f"{amount:+d}"

        player_data["profile"][key] = str(new_val)
        await core.save_session_data(self.bot, session)

        await ctx.send(f"✅ {char_name}의 [{key}] 수치 연산 완료: {old_val} -> {new_val} ({weight_str})")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if game_channel:
            await game_channel.send(
                f"> 📢 **[스탯 변동]** {char_name}의 [{key}]이(가) {new_val}(으)로 변경되었습니다. ({old_val}{weight_str})")


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
    async def show_profile(self, ctx, char_name: str):
        """
        특정 캐릭터의 모든 스탯과 외형이 포함된 프로필 카드를 게임 채널에 출력합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 대상 캐릭터 이름
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        player_data = session.players[user_id_str]
        member = ctx.guild.get_member(int(user_id_str))

        embed = discord.Embed(title=f"🎭 {char_name}의 프로필", color=0x3498db)
        if member:
            embed.set_author(name=member.display_name,
                             icon_url=member.display_avatar.url if member.display_avatar else None)
        else:
            embed.set_author(name=char_name)

        for key, val in player_data["profile"].items():
            embed.add_field(name=key, value=val, inline=True)

        appearance = player_data.get("appearance")
        if appearance:
            embed.add_field(name="외형", value=appearance, inline=False)

        game_channel = self.bot.get_channel(session.game_ch_id)
        if game_channel:
            await game_channel.send(embed=embed)
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
            return await ctx.send("⚠️ 캐릭터 유형은 `pc` 또는 `npc` 중 하나로 입력해주세요.\n(예시: `!설정생성 npc 레온타르트 용병단장`)")

        type_kr = "플레이어 캐릭터(PC)" if char_type == "pc" else "NPC"
        await ctx.send(f"⏳ AI가 세계관을 바탕으로 {type_kr} '{char_name}'의 설정 초안을 생성 중입니다. 잠시만 기다려주세요...")

        try:
            response = await core.generate_character_details(self.bot, session.scenario_data, char_type, char_name,
                                                              instruction, session.session_id)
            generated_text = response.text

            meta = response.usage_metadata
            in_tokens = meta.prompt_token_count
            out_tokens = meta.candidates_token_count
            turn_cost = core.calculate_cost(core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens)
            session.total_cost += turn_cost
            print(
                f"💰 [비용 보고] 설정 생성({char_name}) - In:{in_tokens}, Out:{out_tokens} | 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")
            await core.save_session_data(self.bot, session)

            if char_type == "pc":
                guide_cmd = f"`!외형 {char_name} [내용]`"
            else:
                guide_cmd = f"`!엔피씨 설정 {char_name} [내용]`"

            header = f"💡 **[{char_name}] {type_kr} 설정 초안 생성 완료**\n*아래 내용을 복사하여 자유롭게 수정한 뒤, {guide_cmd} 명령어로 게임에 적용하세요.*\n\n"
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