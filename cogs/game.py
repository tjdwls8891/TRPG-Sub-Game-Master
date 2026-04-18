import re
import asyncio
from discord.ext import commands
from google.genai import types
from google.genai.errors import APIError

# 분리된 코어 유틸리티 모듈 임포트
import core


class GameCog(commands.Cog):
    """
    LLM 턴 묘사 엔진, 기억 압축, 주사위 판정 및 채팅 로깅 등
    게임 플레이와 관련된 핵심 로직을 전담하는 모듈입니다.
    """
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        """
        채널에 메시지가 전송될 때마다 호출되어 행동/대화 로그를 처리하는 이벤트입니다.
        명령어 처리는 main.py의 bot.process_commands에서 별도로 수행되므로
        이곳에서는 순수 게임 로깅만 담당합니다.

        Args:
            message (discord.Message): 수신된 메시지 객체
        """
        if message.author == self.bot.user:
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return

        # 명령어로 시작하는 채팅은 로깅 로직을 건너뜁니다.
        if message.content.startswith('!'):
            if message.channel.id == session.master_ch_id:
                core.write_log(session.session_id, "master_chat", f"[GM 명령어]: {message.content}")
            return

        if message.channel.id == session.master_ch_id:
            game_channel = self.bot.get_channel(session.game_ch_id)
            if game_channel:
                await core.stream_text_to_channel(self.bot, game_channel, f"> {message.content}", words_per_tick=5, tick_interval=1.5)
                session.current_turn_logs.append(f"[진행자]: {message.content}")
                await core.save_session_data(self.bot, session)

            core.write_log(session.session_id, "master_chat", f"[GM 전달]: {message.content}")

        elif message.channel.id == session.game_ch_id:
            user_id_str = str(message.author.id)

            if user_id_str in session.players:
                char_name = session.players[user_id_str]["name"]
            else:
                char_name = message.author.display_name

            session.current_turn_logs.append(f"[{char_name}]: {message.content}")
            await core.save_session_data(self.bot, session)

            core.write_log(session.session_id, "game_chat", f"[{char_name}]: {message.content}")


    @commands.command(name="주사위")
    async def request_dice(self, ctx, char_name: str, param1: str, param2: str = None, param3: str = None):
        """
        일반적인 N면체 또는 캐릭터의 특정 스탯 기준에 대한 주사위 굴림 요청 UI를 전송합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            char_name (str): 굴림을 수행할 캐릭터 이름
            param1 (str): 주사위의 면 수(일반) 또는 기준이 되는 스탯 이름(능력치)
            param2 (str, optional): 가중치(일반) 또는 스탯 주사위의 면 수(능력치)
            param3 (str, optional): 임의 목표값(일반) 또는 스탯 판정에서의 보정 가중치(능력치)
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        user_id_str = core.get_uid_by_char_name(session, char_name)
        if not user_id_str:
            return await ctx.send(f"⚠️ '{char_name}'(으)로 참가한 플레이어를 찾을 수 없습니다.")

        player_data = session.players[user_id_str]
        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if param1.isdigit():
            max_val = int(param1)
            weight = 0
            target_val = None

            if param2 and param2.lstrip('-').isdigit():
                weight = int(param2)

            if param3 and param3.lstrip('-').isdigit():
                target_val = int(param3)

            req_weight_str = f" (가중치 {weight:+d})" if weight != 0 else ""

            view = core.GeneralDiceView(self.bot, target_uid=user_id_str, max_val=max_val, weight=weight, target_val=target_val)

            if target_val is None:
                await game_channel.send(
                    f"> 🎲 <@{user_id_str}>, 일반 {max_val}면체 다이스 판정을 시작합니다. 아래 버튼을 눌러주세요.{req_weight_str}",
                    view=view
                )
            else:
                await game_channel.send(
                    f"> 🎲 <@{user_id_str}>, {max_val}눈 다이스로 [목표값:{target_val}] 판정을 시작합니다. 아래 버튼을 눌러주세요.{req_weight_str}",
                    view=view
                )
            return None

        stat_name = param1
        if not param2 or not param2.lstrip('-').isdigit():
            return await ctx.send("⚠️ 능력치 판정 시 최대 눈(max_val)을 입력해야 합니다. 예: `!주사위 아서 근력 100`")

        max_val = int(param2)
        weight = int(param3) if param3 and param3.lstrip('-').isdigit() else 0

        if stat_name not in player_data["profile"]:
            allowed_keys = ", ".join(player_data["profile"].keys())
            return await ctx.send(f"⚠️ 프로필에 [{stat_name}] 항목이 없습니다. (가능한 항목: {allowed_keys})")

        try:
            stat_value = int(player_data["profile"][stat_name])
        except ValueError:
            return await ctx.send(f"⚠️ [{stat_name}]의 값이 숫자가 아닙니다. 판정을 진행할 수 없습니다.")

        req_weight_str = f" (가중치 {weight:+d})" if weight != 0 else ""
        view = core.DiceView(self.bot, target_uid=user_id_str, max_val=max_val, stat_name=stat_name, stat_value=stat_value, weight=weight)

        await game_channel.send(
            f"> 🎲 <@{user_id_str}>, {max_val}눈 다이스로 [{stat_name}:{stat_value}] 판정을 시작합니다. 아래 버튼을 눌러주세요. {req_weight_str}",
            view=view
        )
        return None

    @commands.command(name="진행")
    async def proceed_turn(self, ctx, *, instruction: str = ""):
        """
        입력된 지시사항과 현재 누적된 로그를 기반으로 다음 게임 턴의 상황을 생성 및 연출합니다.
        인라인 특수 태그(상/중/하 이미지, 자원, 상태이상)를 파싱하여 백그라운드 상태를 즉각 갱신합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
            instruction (str, optional): 진행할 방향성에 대한 GM의 프롬프트
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if not getattr(session, "is_started", False):
            return await ctx.send("⚠️ 세션이 아직 시작되지 않았습니다. API 역할 동기화를 위해 반드시 `!시작` 명령어를 먼저 실행하십시오.")

        try:
            await game_channel.set_permissions(ctx.guild.default_role, send_messages=False)
        except Exception as e:
            print(f"⚠️ 자동 채팅 잠금 실패: {e}")

        try:
            # 1. 태그 정규식 파싱
            img_pattern = r'(상|중|하):([^\s]+)'
            img_tags = re.findall(img_pattern, instruction)

            top_imgs, mid_imgs, bottom_imgs = [], [], []
            for pos, kw in img_tags:
                if pos == '상':
                    top_imgs.append(kw)
                elif pos == '중':
                    mid_imgs.append(kw)
                elif pos == '하':
                    bottom_imgs.append(kw)

            res_pattern = r'자:([^\s;]+);([^\s;]+);([-+]?\d+)'
            res_tags = re.findall(res_pattern, instruction)

            for char_name, item_name, amount_str in res_tags:
                amount = int(amount_str)
                if char_name not in session.resources:
                    session.resources[char_name] = {}
                session.resources[char_name][item_name] = session.resources[char_name].get(item_name, 0) + amount

            status_pattern = r'태:([^\s;]+);([^\s]+)'
            status_tags = re.findall(status_pattern, instruction)

            for char_name, status_text in status_tags:
                if char_name not in session.statuses:
                    session.statuses[char_name] = []

                # 값 앞에 '-'가 붙어 있으면 해당 상태이상을 리스트에서 제거
                if status_text.startswith("-"):
                    target_status = status_text[1:]
                    if target_status in session.statuses[char_name]:
                        session.statuses[char_name].remove(target_status)
                else:
                    if status_text not in session.statuses[char_name]:
                        session.statuses[char_name].append(status_text)

            # 2. 파싱된 태그 텍스트들을 AI 프롬프트용 지시문에서 제거
            clean_instruction = re.sub(img_pattern, '', instruction)
            clean_instruction = re.sub(res_pattern, '', clean_instruction)
            clean_instruction = re.sub(status_pattern, '', clean_instruction)
            clean_instruction = re.sub(r'\s+', ' ', clean_instruction).strip()

            if not clean_instruction:
                clean_instruction = "현재까지의 상황, 세계관, 누적된 기억, 그리고 플레이어의 직전 행동을 바탕으로 물리적 인과율에 맞춰 개연성 있게 다음 상황을 진행하고 묘사하십시오."

            await ctx.send("⏳ AI가 묘사를 생성 중입니다. 완료 후 게임 채널에 타이핑 연출을 시작합니다...")

            prompt = core.PromptBuilder.build_prompt(session, clean_instruction)
            core.write_log(session.session_id, "api", f"[메인 턴 묘사 요청]\n{prompt}")

            current_contents = session.raw_logs + [
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ]

            async def generate_with_retry(retry_count=0):
                try:
                    if session.cache_obj and session.cache_name:
                        config = types.GenerateContentConfig(cached_content=session.cache_name, temperature=0.7)
                    else:
                        config = types.GenerateContentConfig(system_instruction=self.bot.system_instruction,
                                                             temperature=0.7)

                    async with game_channel.typing():
                        return await asyncio.to_thread(
                            self.bot.genai_client.models.generate_content,
                            model=core.DEFAULT_MODEL,
                            contents=current_contents,
                            config=config
                        )
                except APIError as e:
                    if retry_count == 0 and ("cache" in str(e).lower() or e.code in [400, 404]):
                        await ctx.send("🔄 **[시스템 알림]** 장기 기억 캐시가 만료되어 자동으로 재발급을 진행합니다. 턴 묘사는 이어서 출력됩니다...")

                        caching_text, cache_tokens = await core.build_scenario_cache_text(self.bot, core.DEFAULT_MODEL,
                                                                                          session.scenario_data)
                        creation_cost = core.calculate_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
                        storage_cost = core.calculate_cost(core.DEFAULT_MODEL, cache_storage_tokens=cache_tokens,
                                                           storage_hours=1)
                        session.total_cost += (creation_cost + storage_cost)

                        print(
                            f"💰 [비용 보고] 세션({session.session_id}) 진행 중 자동 캐시 발급: ${creation_cost + storage_cost:.6f} (누적: ${session.total_cost:.6f})")

                        new_cache = await asyncio.to_thread(
                            self.bot.genai_client.caches.create,
                            model=core.DEFAULT_MODEL,
                            config=types.CreateCachedContentConfig(
                                system_instruction=self.bot.system_instruction,
                                contents=[types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                                ttl="3600s"
                            )
                        )
                        session.cache_obj = new_cache
                        session.cache_name = new_cache.name
                        session.cache_model = core.DEFAULT_MODEL
                        await core.save_session_data(self.bot, session)

                        return await generate_with_retry(retry_count=1)
                    else:
                        raise e

            response = await generate_with_retry()

            meta = response.usage_metadata
            in_tokens = meta.prompt_token_count
            out_tokens = meta.candidates_token_count
            cached_tokens = getattr(meta, "cached_content_token_count", 0)

            turn_cost = core.calculate_cost(core.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                            cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost
            print(
                f"💰 [비용 보고] 턴 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

            full_ai_response = response.text

            turn_history_text = "\n".join(session.current_turn_logs) + f"\n[GM 지시]: {clean_instruction}"
            session.raw_logs.append(types.Content(role="user", parts=[types.Part.from_text(text=turn_history_text)]))
            session.raw_logs.append(types.Content(role="model", parts=[types.Part.from_text(text=full_ai_response)]))

            session.uncompressed_logs.append(f"[플레이어 및 GM]: {turn_history_text}")
            session.uncompressed_logs.append(f"[GM 묘사]: {full_ai_response}")

            session.current_turn_logs.clear()
            session.turn_count += 1

            if len(session.raw_logs) > 20:
                session.raw_logs = session.raw_logs[-20:]

            code_block_match = re.search(r'(.*)(```.*?```)\s*$', full_ai_response, re.DOTALL)
            if code_block_match:
                narrative_text = code_block_match.group(1).strip()
                code_block_text = code_block_match.group(2).strip()
            else:
                narrative_text = full_ai_response.strip()
                code_block_text = ""

            paragraphs = [p.strip() for p in narrative_text.split('\n\n') if p.strip()]

            if not paragraphs:
                for kw in top_imgs + mid_imgs + bottom_imgs:
                    await core.send_image_by_keyword(game_channel, ctx, session, kw)
            else:
                for i, paragraph in enumerate(paragraphs):
                    await core.stream_text_to_channel(self.bot, game_channel, paragraph, words_per_tick=5,
                                                      tick_interval=1.5)

                    if i == 0:
                        for kw in top_imgs:
                            await core.send_image_by_keyword(game_channel, ctx, session, kw)

                    for kw in list(mid_imgs):
                        if kw in paragraph:
                            await core.send_image_by_keyword(game_channel, ctx, session, kw)
                            mid_imgs.remove(kw)

                for kw in mid_imgs:
                    await core.send_image_by_keyword(game_channel, ctx, session, kw)
                for kw in bottom_imgs:
                    await core.send_image_by_keyword(game_channel, ctx, session, kw)

            if code_block_text:
                await game_channel.send(code_block_text)

            await ctx.send(f"✅ 묘사 연출 완료 (현재 {session.turn_count}턴 경과). 다음 턴 대기 중...")

            if session.turn_count > 0 and session.turn_count % 5 == 0:
                if not session.uncompressed_logs:
                    pass
                else:
                    await ctx.send(f"⏳ (시스템: 백그라운드에서 자동 초정밀 기억 압축을 진행합니다...)")

                    logs_to_compress = list(session.uncompressed_logs)
                    log_text = "\n\n".join(logs_to_compress)
                    summary_prompt = core.build_compression_prompt(session, log_text)

                    core.write_log(session.session_id, "api", f"[기억 압축 요청]\n{summary_prompt}")

                    try:
                        summary_response = await asyncio.to_thread(
                            self.bot.genai_client.models.generate_content,
                            model=core.LOGIC_MODEL,
                            contents=summary_prompt
                        )

                        meta = summary_response.usage_metadata
                        in_tokens = meta.prompt_token_count
                        out_tokens = meta.candidates_token_count
                        cached_tokens = getattr(meta, "cached_content_token_count", 0)

                        turn_cost = core.calculate_cost(core.LOGIC_MODEL, input_tokens=in_tokens,
                                                        output_tokens=out_tokens,
                                                        cached_read_tokens=cached_tokens)
                        session.total_cost += turn_cost
                        print(
                            f"💰 [비용 보고] 기억 압축 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

                        new_compressed_segment = summary_response.text.strip()
                        if session.compressed_memory:
                            session.compressed_memory += f"\n{new_compressed_segment}"
                        else:
                            session.compressed_memory = new_compressed_segment

                        del session.uncompressed_logs[:len(logs_to_compress)]

                        success_msg = f"✅ 자동 누적 압축 완료.\n**[최근 추가된 기억]**\n{new_compressed_segment}"
                        if len(success_msg) > 2000:
                            for i in range(0, len(success_msg), 2000):
                                await ctx.send(success_msg[i:i + 2000])
                                await asyncio.sleep(1)
                        else:
                            await ctx.send(success_msg)
                    except Exception as e:
                        await ctx.send(f"⚠️ 자동 기억 압축 중 오류 발생: {e}")

            await core.save_session_data(self.bot, session)

        except Exception as e:
            await ctx.send(f"⚠️ 시스템 오류가 발생했습니다: {str(e)}")

        finally:
            # [채팅 자동 해제] 에러가 발생하여 중간에 멈추든, 정상적으로 종료되든 무조건 채팅을 다시 풀어줍니다.
            try:
                await game_channel.set_permissions(ctx.guild.default_role, send_messages=True)
            except Exception as e:
                print(f"⚠️ 자동 채팅 해제 실패: {e}")


    @commands.command(name="기억압축")
    async def compress_memory(self, ctx):
        """
        현재까지 대기열에 쌓인 턴 로그들을 초정밀 요약하여 장기 기억 공간에 병합합니다.

        Args:
            ctx (commands.Context): 디스코드 컨텍스트 객체
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
            return

        if not session.uncompressed_logs:
            await ctx.send("압축할 새로운 대화 로그가 없습니다.")
            return

        await ctx.send("⏳ 수 초정밀 기억 압축을 진행 중입니다...")

        logs_to_compress = list(session.uncompressed_logs)
        log_text = "\n\n".join(logs_to_compress)
        summary_prompt = core.build_compression_prompt(session, log_text)

        core.write_log(session.session_id, "api", f"[기억 압축 요청]\n{summary_prompt}")

        try:
            summary_response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=core.LOGIC_MODEL,
                contents=summary_prompt
            )

            meta = summary_response.usage_metadata
            in_tokens = meta.prompt_token_count
            out_tokens = meta.candidates_token_count
            cached_tokens = getattr(meta, "cached_content_token_count", 0)

            turn_cost = core.calculate_cost(core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens, cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost
            print(f"💰 [비용 보고] 수동 기억 압축 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

            new_compressed_segment = summary_response.text.strip()
            if session.compressed_memory:
                session.compressed_memory += f"\n{new_compressed_segment}"
            else:
                session.compressed_memory = new_compressed_segment

            del session.uncompressed_logs[:len(logs_to_compress)]
            await core.save_session_data(self.bot, session)

            success_msg = f"✅ 수동 누적 압축 완료.\n**[최근 추가된 기억]**\n{new_compressed_segment}"
            if len(success_msg) > 2000:
                for i in range(0, len(success_msg), 2000):
                    await ctx.send(success_msg[i:i + 2000])
                    await asyncio.sleep(1)
            else:
                await ctx.send(success_msg)

        except Exception as e:
            await ctx.send(f"⚠️ 요약 중 오류 발생: {e}")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수입니다.
    """
    await bot.add_cog(GameCog(bot))