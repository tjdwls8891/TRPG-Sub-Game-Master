import re
import asyncio
import time
import discord
from discord.ext import commands
from google.genai import types
from google.genai.errors import APIError

# 코어 유틸리티 모듈 임포트
import core


# ========== [메인 게임 엔진 모듈(Game Cog)] ==========
class GameCog(commands.Cog):
    """
    LLM 턴 묘사 엔진, 기억 압축, 주사위 판정 및 채팅 로깅 등
    게임 플레이와 관련된 핵심 로직을 전담하는 모듈.
    """

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_typing(self, channel, user, when):
        """
        마스터 채널에서 GM이 입력 중일 때, 게임 채널에 봇이 입력 중인 것처럼 동기화.
        최대 20초간 유지되며, 새로운 입력 감지 시 타이머가 갱신됨.
        """
        if user == self.bot.user:
            return

        session = self.bot.active_sessions.get(channel.id)
        if not session:
            return

        # 마스터 채널에서의 입력인지 확인
        if channel.id != session.master_ch_id:
            return

        # 시스템이 묘사를 처리 중(AI 타이핑 연출 중)이라면 무시하여 충돌 방지
        if getattr(session, "is_processing", False):
            return

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return

        # 기존에 작동 중인 타이머 태스크가 있다면 취소 (타이머 리셋 효과)
        if getattr(session, "gm_typing_task", None) and not session.gm_typing_task.done():
            session.gm_typing_task.cancel()

        # 20초 유지 타이머 비동기 함수 정의
        async def typing_sync_task():
            try:
                # discord.py 2.0+ 규격: async with 블록 내부에 머무르는 동안 10초마다 자동 갱신됨
                async with game_channel.typing():
                    # 20초 동안 타이핑 상태 유지
                    await asyncio.sleep(20)
            except asyncio.CancelledError:
                # 마스터가 입력을 멈추고 메시지를 전송하거나, 새 입력으로 갱신될 때 정상 종료
                pass

        # 새 태스크 등록 및 백그라운드 실행
        session.gm_typing_task = self.bot.loop.create_task(typing_sync_task())

    @commands.Cog.listener()
    async def on_message(self, message):
        """
        채널에 메시지가 전송될 때마다 호출되어 행동/대화 로그를 처리하는 자동 로깅 이벤트.

        명령어 처리는 main.py의 bot.process_commands에서 별도로 수행되므로
        이곳에서는 순수 게임 로깅만 담당.

        Args:
            message (discord.Message): 수신된 메시지 객체
        """
        if message.author == self.bot.user:
            session = self.bot.active_sessions.get(message.channel.id)
            if session and message.channel.id == session.master_ch_id:
                core.write_log(session.session_id, "master_chat", f"[SYSTEM/BOT]: {message.content}")
            return

        session = self.bot.active_sessions.get(message.channel.id)
        if not session:
            return

        if message.channel.id == session.master_ch_id:
            if getattr(session, "gm_typing_task", None) and not session.gm_typing_task.done():
                session.gm_typing_task.cancel()

        # NOTE: 명령어로 시작하는 채팅은 게임 내 발화나 행동이 아니므로 로깅 로직에서 제외.
        if message.content.startswith('!'):
            if message.channel.id == session.master_ch_id:
                core.write_log(session.session_id, "master_chat", f"[GM 명령어]: {message.content}")
            return

        if message.channel.id == session.master_ch_id:
            game_channel = self.bot.get_channel(session.game_ch_id)
            if game_channel:
                await core.stream_text_to_channel(self.bot, game_channel, f"> {message.content}", words_per_tick=5,
                                                  tick_interval=1.5)
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
        일반적인 N면체 또는 캐릭터의 특정 스탯 기준에 대한 주사위 굴림 요청 UI 전송.

        파라미터 타입 판별을 통해 일반 판정과 능력치 판정 뷰(View)를 동적으로 분기하여 출력.

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

            view = core.GeneralDiceView(self.bot, target_uid=user_id_str, max_val=max_val, weight=weight,
                                        target_val=target_val)

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
        view = core.DiceView(self.bot, target_uid=user_id_str, max_val=max_val, stat_name=stat_name,
                             stat_value=stat_value, weight=weight)

        await game_channel.send(
            f"> 🎲 <@{user_id_str}>, {max_val}눈 다이스로 [{stat_name}:{stat_value}] 판정을 시작합니다. 아래 버튼을 눌러주세요. {req_weight_str}",
            view=view
        )
        return None

    @commands.command(name="진행")
    async def proceed_turn(self, ctx, *, instruction: str = ""):
        """
        입력된 지시사항과 현재 누적된 로그를 기반으로 다음 게임 턴의 상황을 생성 및 연출.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if not getattr(session, "is_started", False):
            return await ctx.send("⚠️ 세션이 아직 시작되지 않았습니다. API 역할 동기화를 위해 반드시 `!시작` 명령어를 먼저 실행하십시오.")

        if getattr(session, "is_processing", False):
            return await ctx.send("⏳ 시스템이 이전 턴 명령을 처리 중입니다. 잠시만 기다려주십시오.")

        session.is_processing = True

        try:
            anchor = None
            async for msg in game_channel.history(limit=1):
                anchor = msg
            session.last_turn_anchor_id = anchor.id if anchor else None

            await game_channel.set_permissions(ctx.guild.default_role, send_messages=False)
        except Exception as e:
            print(f"⚠️ 자동 채팅 잠금 실패 또는 앵커 획득 실패: {e}")

        try:
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

                if status_text.startswith("-"):
                    target_status = status_text[1:]
                    if target_status in session.statuses[char_name]:
                        session.statuses[char_name].remove(target_status)
                else:
                    if status_text not in session.statuses[char_name]:
                        session.statuses[char_name].append(status_text)

            clean_instruction = re.sub(img_pattern, '', instruction)
            clean_instruction = re.sub(res_pattern, '', clean_instruction)
            clean_instruction = re.sub(status_pattern, '', clean_instruction)
            clean_instruction = re.sub(r'\s+', ' ', clean_instruction).strip()

            if not clean_instruction:
                clean_instruction = "현재까지의 상황, 세계관, 누적된 기억, 그리고 플레이어의 직전 행동을 바탕으로 물리적 인과율에 맞춰 개연성 있게 다음 상황을 진행하고 묘사하십시오."

            await ctx.send("⏳ AI가 묘사를 생성 중입니다. 완료 후 게임 채널에 타이핑 연출을 시작합니다...")

            prompt = core.PromptBuilder.build_prompt(session, clean_instruction)

            current_contents = session.raw_logs + [
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ]

            payload_dump = ""
            for content in current_contents:
                payload_dump += f"[{content.role.upper()}]\n{content.parts[0].text}\n\n"
            core.write_log(session.session_id, "api", f"[메인 턴 묘사 요청 - 최종 Payload]\n{payload_dump}")

            async def generate_with_retry(retry_count=0):
                try:
                    if session.cache_obj and session.cache_name:
                        config = types.GenerateContentConfig(cached_content=session.cache_name, temperature=0.7,
                                                             safety_settings=core.TRPG_SAFETY_SETTINGS)
                    else:
                        config = types.GenerateContentConfig(system_instruction=self.bot.system_instruction,
                                                             temperature=0.7, safety_settings=core.TRPG_SAFETY_SETTINGS)

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

                        storage_cost = await core.process_cache_deletion(self.bot, session)
                        caching_text, cache_tokens = await core.build_scenario_cache_text(self.bot,
                                                                                          core.DEFAULT_MODEL,
                                                                                          session.scenario_data,
                                                                                          getattr(session,
                                                                                                  "cache_note", ""), session.session_id)

                        upload_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
                        session.total_cost += upload_cost
                        session.cache_created_at = time.time()
                        session.cache_tokens = cache_tokens

                        core.write_cost_log(session.session_id, "캐시 자동 재발급(진행 중)", cache_tokens, 0, 0, upload_cost,
                                            session.total_cost)

                        report_msg = f"💰 **[캐시 자동 재발급 정산]**\n- 이전 캐시 보관 비용: {core.format_cost(storage_cost)}\n- 새 캐시 업로드 비용: {core.format_cost(upload_cost)}\n- 현재까지 총 누적 비용: {core.format_cost(session.total_cost)}"
                        print(report_msg)

                        master_ch = self.bot.get_channel(session.master_ch_id)
                        if master_ch:
                            await master_ch.send(report_msg)

                        new_cache = await asyncio.to_thread(
                            self.bot.genai_client.caches.create,
                            model=core.DEFAULT_MODEL,
                            config=types.CreateCachedContentConfig(
                                system_instruction=self.bot.system_instruction,
                                contents=[
                                    types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                                ttl="21600s",
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

            turn_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                                   cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost

            core.write_cost_log(session.session_id, "턴 진행 생성", in_tokens, cached_tokens, out_tokens, turn_cost,
                                session.total_cost)

            report_msg = f"💰 **[비용 보고] 턴 진행**\n- 토큰: In({in_tokens}), Cached({cached_tokens}), Out({out_tokens})\n- 턴 발생 비용: {core.format_cost(turn_cost)}\n- 누적 비용: {core.format_cost(session.total_cost)}"
            print(report_msg)

            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send(report_msg)

            full_ai_response = response.text

            if not full_ai_response:
                finish_reason = response.candidates[0].finish_reason if response.candidates else "Unknown"
                raise ValueError(
                    f"AI가 텍스트를 반환하지 않았습니다. (구글 API 강제 차단 혹은 모델 에러. 사유: {finish_reason})\n지시사항의 수위를 조절하거나 `!재생성`을 이용해 턴을 취소해 주십시오.")

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
                            contents=summary_prompt,
                            config=types.GenerateContentConfig(
                                safety_settings=core.TRPG_SAFETY_SETTINGS
                            )
                        )

                        meta = summary_response.usage_metadata
                        in_tokens = meta.prompt_token_count
                        out_tokens = meta.candidates_token_count
                        cached_tokens = getattr(meta, "cached_content_token_count", 0)

                        turn_cost = core.calculate_upload_cost(core.LOGIC_MODEL, input_tokens=in_tokens,
                                                        output_tokens=out_tokens,
                                                        cached_read_tokens=cached_tokens)
                        session.total_cost += turn_cost

                        core.write_cost_log(session.session_id, "자동 기억 압축", in_tokens, cached_tokens, out_tokens,
                                            turn_cost, session.total_cost)

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
            session.is_processing = False
            try:
                await game_channel.set_permissions(ctx.guild.default_role, send_messages=True)
            except Exception as e:
                print(f"⚠️ 자동 채팅 해제 실패: {e}")

    @commands.command(name="재생성")
    async def regenerate_turn(self, ctx, *, instruction: str = ""):
        """
        직전 턴의 시스템 출력을 무효화(Rollback)하고, 새로운 지시사항을 바탕으로 턴 묘사를 재생성.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if getattr(session, "is_processing", False):
            return await ctx.send("⏳ 시스템이 다른 명령을 처리 중입니다. 잠시만 기다려주십시오.")

        if session.turn_count <= 0 or len(session.raw_logs) < 2:
            return await ctx.send("⚠️ 취소할 직전 턴의 묘사가 존재하지 않습니다.")

        # 기억 압축 직후 롤백 방지 로직 (5턴 주기)
        if session.turn_count % 5 == 0:
            return await ctx.send("⚠️ 직전 턴 직후에 이미 기억 압축이 완료되어 시스템 롤백이 불가능합니다. 롤백 대신 수동으로 다음 턴을 진행해 상황을 교정하십시오.")

        await ctx.send("⏳ 직전 턴의 로그와 출력물을 삭제하고 있습니다...")
        session.is_processing = True

        try:
            # 1. 디스코드 UI 롤백: 앵커 이후에 생성된 봇의 모든 출력물 일괄 삭제
            if getattr(session, "last_turn_anchor_id", None):
                try:
                    anchor_msg = await game_channel.fetch_message(session.last_turn_anchor_id)
                    await game_channel.purge(after=anchor_msg, check=lambda m: m.author == self.bot.user)
                except discord.NotFound:
                    pass

            # 2. 메모리 로그 롤백: 유저 프롬프트와 AI 묘사를 1세트(2개) Pop 처리
            if len(session.raw_logs) >= 2:
                # 롤백할 이전 턴의 유저 턴 데이터 문자열 추출
                prev_user_content = session.raw_logs[-2].parts[0].text

                # "[GM 지시]:"를 기준으로 문자열을 분할하여 앞부분(대화 기록)만 추출
                if "\n[GM 지시]:" in prev_user_content:
                    chat_logs = prev_user_content.split("\n[GM 지시]:")[0].strip()
                    if chat_logs:
                        # 추출된 대화 문자열을 다시 리스트 형태로 복구하여 대기열에 삽입
                        session.current_turn_logs = chat_logs.split("\n")

                # 배열에서 직전 턴 데이터 2세트(프롬프트, 응답) 삭제
                session.raw_logs = session.raw_logs[:-2]

            if len(session.uncompressed_logs) >= 2:
                session.uncompressed_logs = session.uncompressed_logs[:-2]

            # 3. 턴 카운터 차감 및 앵커 초기화
            session.turn_count -= 1
            session.last_turn_anchor_id = None

            await core.save_session_data(self.bot, session)
            await ctx.send("✅ 이전 출력이 삭제되었습니다. 새 지시사항으로 턴을 진행합니다...")

        except Exception as e:
            await ctx.send(f"⚠️ 롤백 중 오류가 발생했습니다: {e}")
            return
        finally:
            session.is_processing = False

        # 새로운 묘사 출력을 위해 메인 진행 함수 재호출
        await self.proceed_turn(ctx, instruction=instruction)

    @commands.command(name="기억압축")
    async def compress_memory(self, ctx):
        """
        현재까지 대기열에 쌓인 턴 로그들을 초정밀 요약하여 장기 기억 공간에 병합.

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
                contents=summary_prompt,
                config=types.GenerateContentConfig(
                    safety_settings=core.TRPG_SAFETY_SETTINGS
                )
            )

            meta = summary_response.usage_metadata
            in_tokens = meta.prompt_token_count
            out_tokens = meta.candidates_token_count
            cached_tokens = getattr(meta, "cached_content_token_count", 0)

            turn_cost = core.calculate_upload_cost(core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                            cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost

            core.write_cost_log(session.session_id, "수동 기억 압축", in_tokens, cached_tokens, out_tokens, turn_cost,
                                session.total_cost)

            print(
                f"💰 [비용 보고] 수동 기억 압축 진행 - In:{in_tokens}, Cached:{cached_tokens}, Out:{out_tokens} | 턴 발생: ${turn_cost:.6f} (누적: ${session.total_cost:.6f})")

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

    @commands.command(name="노트")
    async def manage_note(self, ctx, action: str, *, content: str = None):
        """
        GM이 실시간으로 관리하는 기억(노트) 항목을 누적, 갱신, 출력.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not hasattr(session, "note"):
            session.note = ""

        if action == "누적":
            if not content:
                return await ctx.send("⚠️ 누적할 내용을 입력해주세요. (예: `!노트 누적 아서가 열쇠를 획득함`)")
            if session.note:
                session.note += f"\n- {content}"
            else:
                session.note = f"- {content}"
            await core.save_session_data(self.bot, session)
            await ctx.send(f"✅ 노트가 누적되었습니다.\n**[현재 노트]**\n{session.note}")

        elif action == "갱신":
            if not content:
                return await ctx.send("⚠️ 갱신할 내용을 입력해주세요. 기존 내용은 모두 지워집니다.")
            session.note = content
            await core.save_session_data(self.bot, session)
            await ctx.send(f"✅ 노트가 갱신되었습니다.\n**[새 노트]**\n{session.note}")

        elif action == "출력":
            if not session.note:
                return await ctx.send("📝 현재 노트가 비어있습니다.")
            await ctx.send(f"📝 **[현재 노트]**\n{session.note}")

        else:
            await ctx.send("⚠️ 잘못된 인자입니다. 사용법: `!노트 [누적/갱신/출력] (내용)`")

    @commands.command(name="캐시노트")
    async def manage_cache_note(self, ctx, action: str, *, content: str = None):
        """
        차기 캐시 생성 시 룰북에 지연 병합될 세계관/상태 정보를 누적, 갱신, 출력.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        if not hasattr(session, "cache_note"):
            session.cache_note = ""

        # 분할 전송을 위한 헬퍼 함수 내장
        async def send_long_message(text):
            if len(text) > 2000:
                for i in range(0, len(text), 2000):
                    await ctx.send(text[i:i + 2000])
                    await asyncio.sleep(1)
            else:
                await ctx.send(text)

        if action == "누적":
            if not content:
                return await ctx.send("⚠️ 누적할 내용을 입력해주세요.")
            if session.cache_note:
                session.cache_note += f"\n- {content}"
            else:
                session.cache_note = f"- {content}"
            await core.save_session_data(self.bot, session)
            await send_long_message(f"✅ 캐시 노트가 누적되었습니다.\n**[현재 캐시 노트]**\n{session.cache_note}")

        elif action == "갱신":
            if not content:
                return await ctx.send("⚠️ 갱신할 내용을 입력해주세요.")
            session.cache_note = content
            await core.save_session_data(self.bot, session)
            await send_long_message(f"✅ 캐시 노트가 갱신되었습니다.\n**[새 캐시 노트]**\n{session.cache_note}")

        elif action == "출력":
            if not getattr(session, "cache_note", ""):
                return await ctx.send("📝 현재 캐시 노트가 비어있습니다.")
            await send_long_message(f"📝 **[현재 캐시 노트]**\n{session.cache_note}")

        else:
            await ctx.send("⚠️ 잘못된 인자입니다. 사용법: `!캐시노트 [누적/갱신/출력] (내용)`")


async def setup(bot):
    """
    디스코드 봇이 이 파일을 로드할 때 호출되는 필수 설정 함수.
    """
    await bot.add_cog(GameCog(bot))