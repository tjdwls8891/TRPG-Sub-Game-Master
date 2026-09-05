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
                await core.stream_text_to_channel(self.bot, game_channel, f"> {message.content}", words_per_tick=15,
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

        user_id_str, char_name, err = core.resolve_pc(session, char_name)
        if err:
            return await ctx.send(err)

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

        # ability_stat_max 자동 조회 — 정의돼 있으면 눈 수 명시 불필요
        auto_max = None
        ability_stat_max = session.scenario_data.get("ability_stat_max")
        if ability_stat_max is not None:
            if isinstance(ability_stat_max, dict):
                val = ability_stat_max.get(stat_name)
                if val is not None:
                    auto_max = int(val)
            elif isinstance(ability_stat_max, (int, float)):
                auto_max = int(ability_stat_max)

        if auto_max is not None:
            # ability_stat_max에서 눈 수를 자동 결정 → param2 = 가중치(선택)
            max_val = auto_max
            weight = int(param2) if param2 and param2.lstrip('-').isdigit() else 0
        else:
            # 수동 모드: param2 = 눈 수 (필수), param3 = 가중치(선택)
            if not param2 or not param2.lstrip('-').isdigit():
                return await ctx.send("⚠️ 능력치 판정 시 최대 눈(max_val)을 입력해야 합니다. 예: `!주사위 아서 근력 20 3`")
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

        NOTE: 본체 로직은 _execute_proceed 헬퍼로 추출되어 있어, GM(GMCog)도
        동일한 코어를 공유한다. 이 명령 진입점은 컨텍스트 검증 후 헬퍼를 호출하는 얇은 래퍼.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        await self._execute_proceed(session, instruction, master_guild=ctx.guild)

    async def _execute_proceed(self, session, instruction: str = "", *, master_guild=None,
                                cost_log_prefix: str = "") -> dict:
        """
        !진행 본체 — 명령 진입점과 GM(GMCog)가 공유하는 코어 로직.

        명령 컨텍스트(ctx)에 의존하지 않으며, 세션과 봇 객체만으로 동작.
        상태 메시지는 마스터 채널, 묘사는 게임 채널로 송출.

        Args:
            session: TRPGSession
            instruction (str): GM 지시사항 (이미지/자원/상태 태그 포함 가능)
            master_guild: 게임 채널 채팅 권한 토글용 guild (None이면 마스터 채널에서 추출)
            cost_log_prefix (str): cost_log.txt 라벨에 부착할 접두사 (예: "[AUTO] ")

        Returns:
            dict: {"ok": bool, "ai_text": str, "error": str|None}
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_channel = self.bot.get_channel(session.game_ch_id)

        async def m_send(content=None, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        if not game_channel:
            await m_send("⚠️ 게임 채널을 찾을 수 없습니다.")
            return {"ok": False, "ai_text": "", "error": "no_game_channel"}

        if not getattr(session, "is_started", False):
            await m_send("⚠️ 세션이 아직 시작되지 않았습니다. API 역할 동기화를 위해 반드시 `!시작` 명령어를 먼저 실행하십시오.")
            return {"ok": False, "ai_text": "", "error": "not_started"}

        if getattr(session, "is_processing", False):
            await m_send("⏳ 시스템이 이전 턴 명령을 처리 중입니다. 잠시만 기다려주십시오.")
            return {"ok": False, "ai_text": "", "error": "busy"}

        if master_guild is None and master_ch:
            master_guild = master_ch.guild

        session.is_processing = True
        full_ai_response = ""
        status_msg = None   # 게임 채널 대기 안내 메시지 핸들 (출력 시작 직전 삭제)

        # [기억 압축 타이밍] 5의 배수 턴(5N) 종료 '직후'가 아니라, 다음 프로씨드(5N+1) '시작 시점'에
        # 압축을 개시한다. 이렇게 하면 5N 턴 자체에는 압축이 걸리지 않아 !재생성이 가능해진다.
        # 압축 대상 로그(직전 5N까지)는 지금 uncompressed_logs에 모두 존재하므로 스냅샷하고,
        # 프로씨드는 압축 완료를 기다리지 않고 곧바로 진행한다(백그라운드 태스크).
        # 삭제는 uncompressed_logs '앞'에서 count만큼, 이번 턴 로그 append는 '뒤'로 이뤄져 경합이 없다.
        # 압축 주기는 플랜이 정한다(노멀 5 / 하이 3 / 로우 5 / 울트라 1).
        # should_compress는 last_compressed_turn을 기준으로 판정하므로,
        # 되감기로 턴이 되돌아가도 이미 압축한 구간을 재압축하지 않는다(기획 규정).
        if (core.memory_plan.should_compress(session)
                and session.uncompressed_logs and not getattr(session, "is_compressing", False)):
            _logs_snapshot = list(session.uncompressed_logs)
            asyncio.create_task(
                self._run_auto_compression(session, _logs_snapshot, cost_log_prefix)
            )

        try:
            anchor = None
            async for msg in game_channel.history(limit=1):
                anchor = msg
            session.last_turn_anchor_id = anchor.id if anchor else None

            try:
                if master_guild:
                    await game_channel.set_permissions(master_guild.default_role, send_messages=False)
            except Exception as e:
                print(f"⚠️ 자동 채팅 잠금 실패: {e}")
        except Exception as e:
            print(f"⚠️ 앵커 획득 실패: {e}")

        try:
            # NOTE: 패턴에서 .,!?;: 를 캡처 대상에서 제외 — AI가 태그 뒤에 마침표 등을 붙여도 정확히 분리됨.
            # 예) 태:임성진;-지침.  →  char_name="임성진", status_text="-지침" (마침표 제외)
            _TAG_END = r'[^\s.,!?;:]'  # 태그 값에 허용되는 마지막 문자 기준
            img_pattern  = r'(상|중|하):(' + _TAG_END + r'+)'
            res_pattern  = r'자:(' + _TAG_END + r'+);(' + _TAG_END + r'+);([-+]?\d+)'
            status_pattern = r'태:(' + _TAG_END + r'+);(-?' + _TAG_END + r'+)'

            # 언더바(_) 규약: 태그 값의 이름·항목·상태에 띄어쓰기가 필요하면 _ 로 표기하고,
            # 파싱 후 공백으로 복원한다. (태그 종결자는 공백이므로 다단어 항목이 잘리는 문제를 해소)
            img_tags = [(pos, kw.replace('_', ' ')) for pos, kw in re.findall(img_pattern, instruction)]

            top_imgs, mid_imgs, bottom_imgs = [], [], []
            if cost_log_prefix:
                # GM: 상: 태그만 허용 (지시층위가 location_images 목록에서 선택한 장소 이미지)
                # 중:/하: 태그는 여전히 무시 (AI의 임의 남발 방지)
                for pos, kw in img_tags:
                    if pos == '상':
                        top_imgs.append(kw)
            else:
                for pos, kw in img_tags:
                    if pos == '상':
                        top_imgs.append(kw)
                    elif pos == '중':
                        mid_imgs.append(kw)
                    elif pos == '하':
                        bottom_imgs.append(kw)

            res_tags = [(c.replace('_', ' '), i.replace('_', ' '), a)
                        for c, i, a in re.findall(res_pattern, instruction)]

            # 유효한 캐릭터 이름 집합 (자:/태: 태그 검증용)
            valid_char_names = set(p["name"] for p in session.players.values() if p.get("name")) | set(session.npcs.keys())

            for char_name, item_name, amount_str in res_tags:
                if char_name not in valid_char_names:
                    print(f"[태그 무시] 자:{char_name};{item_name} — 등록되지 않은 캐릭터 이름")
                    continue
                amount = int(amount_str)
                if char_name not in session.resources:
                    session.resources[char_name] = {}
                new_val = session.resources[char_name].get(item_name, 0) + amount
                # 보유량이 0 이하가 되면 목록에서 삭제
                if new_val <= 0:
                    session.resources[char_name].pop(item_name, None)
                else:
                    session.resources[char_name][item_name] = new_val

            status_tags = [(c.replace('_', ' '), s.replace('_', ' '))
                           for c, s in re.findall(status_pattern, instruction)]

            # GM에서는 유효한 상태이상 이름만 허용
            valid_status_names = None
            if cost_log_prefix:
                valid_status_names = set(core.get_merged_status_effects(session.scenario_data).keys())

            for char_name, status_text in status_tags:
                if char_name not in valid_char_names:
                    print(f"[태그 무시] 태:{char_name};{status_text} — 등록되지 않은 캐릭터 이름")
                    continue
                actual_status = status_text.lstrip("-")
                if valid_status_names is not None and actual_status not in valid_status_names:
                    print(f"[태그 무시] 태:{char_name};{status_text} — 유효하지 않은 상태이상 이름 (목록에 없음)")
                    continue
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
                # NOTE: Auto-GM 모드(cost_log_prefix가 있는 경우)는 항상 proceed_instruction이
                # 채워진 채로 호출되므로 여기에 도달하지 않음. 수동 GM 모드 전용 분기.
                if not cost_log_prefix:
                    gm_cog = self.bot.get_cog("GMCog")
                    if gm_cog:
                        await m_send("⏳ 지시사항 없음 — 지시층위가 현재 상황을 분석하여 진행 지시사항을 자동 생성합니다...")
                        decision = await gm_cog._call_gm_logic(session, "", [], master_ch)
                        if decision:
                            from cogs.gm import _clean_proceed_instruction
                            auto_instr = _clean_proceed_instruction(decision.get("proceed_instruction", ""))
                            if auto_instr:
                                clean_instruction = auto_instr
                                await m_send(f"📋 **[자동 생성 지시사항]**\n> {clean_instruction[:300]}")

            if not clean_instruction:
                clean_instruction = "현재까지의 상황, 세계관, 누적된 기억, 그리고 플레이어의 직전 행동을 바탕으로 물리적 인과율에 맞춰 개연성 있게 다음 상황을 진행하고 묘사하십시오."

            await m_send("⏳ AI가 묘사를 생성 중입니다. 완료 후 게임 채널에 타이핑 연출을 시작합니다...")

            # 플레이어가 보는 게임 채널에 대기 안내 (출력 시작 직전 삭제). 자동/수동 공통.
            # 묘사는 가장 오래 걸린다. 문구를 갈아 끼워 멈춘 것처럼 보이지 않게 한다.
            status_msg = await core.WaitingStatus.begin(game_channel, "narration")

            prompt = core.PromptBuilder.build_prompt(session, clean_instruction)

            # NOTE: Gemini API는 contents가 role="user"로 시작해야 한다.
            # 구형 세션은 raw_logs[0]이 role="model"(start message)일 수 있으므로,
            # model-first인 경우 앞에 dummy user 턴을 삽입해 올바른 대화 구조를 보장한다.
            _raw = list(session.raw_logs)
            if _raw and _raw[0].role == "model":
                _raw.insert(0, types.Content(role="user", parts=[types.Part.from_text(text="[세션 시작]")]))
            current_contents = _raw + [
                types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
            ]

            payload_dump = ""
            for content in current_contents:
                payload_dump += f"[{content.role.upper()}]\n{content.parts[0].text}\n\n"
            core.write_log(session.session_id, "api", f"[메인 턴 묘사 요청 - 최종 Payload]\n{payload_dump}")

            async def _reissue_cache(reason_label: str):
                """룰북 캐시를 (재)발급하고 세션 캐시 상태를 동기화한다.

                캐시 만료 에러 복구와 캐시 부재 선제 발급(방안③)이 공유하는 단일 경로.
                호출 측이 예외를 흡수해 캐시 없이도 턴이 진행될 수 있게 한다.
                """
                storage_cost = await core.process_cache_deletion(self.bot, session)
                caching_text, cache_tokens, base_text = await core.build_scenario_cache_text(
                    self.bot, core.DEFAULT_MODEL, session.scenario_data,
                    getattr(session, "cache_note", ""), session.session_id, session=session
                )

                upload_cost = core.calculate_upload_cost(core.DEFAULT_MODEL, input_tokens=cache_tokens)
                session.total_cost += upload_cost
                session.cache_created_at = time.time()
                session.cache_expired_notified = False
                session.cache_tokens = cache_tokens

                core.write_cost_log(session.session_id, f"{cost_log_prefix}{reason_label}", cache_tokens, 0, 0, upload_cost,
                                    session.total_cost)

                _cache_embed = core.build_cache_cost_embed(
                    reason_label, storage_cost, upload_cost, session.total_cost
                )
                print(f"[{reason_label}] storage={core.format_cost(storage_cost)} upload={core.format_cost(upload_cost)} total={core.format_cost(session.total_cost)}")
                await m_send(embed=_cache_embed)

                new_cache = await asyncio.to_thread(
                    self.bot.genai_client.caches.create,
                    model=core.DEFAULT_MODEL,
                    config=types.CreateCachedContentConfig(
                        system_instruction=self.bot.system_instruction,
                        contents=[
                            types.Content(role="user", parts=[types.Part.from_text(text=caching_text)])],
                        # 남은 유지 시간을 이어간다. 매번 6시간을 새로 주면
                        # 결제한 것보다 오래 살아 비용이 어긋난다.
                        ttl=f"{core.remaining_ttl(session)}s",
                    )
                )
                session.cache_obj = new_cache
                session.cache_name = new_cache.name
                session.cache_model = core.DEFAULT_MODEL
                session.cache_text = base_text
                core.update_session_cache_state(session)
                await core.save_session_data(self.bot, session)

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
                        await m_send("🔄 **[시스템 알림]** 장기 기억 캐시가 만료되어 자동으로 재발급을 진행합니다. 턴 묘사는 이어서 출력됩니다...")
                        await _reissue_cache("캐시 자동 재발급 (진행 중)")
                        return await generate_with_retry(retry_count=1)
                    else:
                        raise e

            # 방안③: 캐시가 없으면(명시적 !캐시 삭제 후 재개, 복구 직후 등) 에러를 기다리지
            # 않고 선제 발급한다. 캐시 부재 시 cacheless 분기는 system_instruction(GM 페르소나)만
            # 넘겨 시나리오 룰북 전체(세계관·NPC·스탯·금지)가 프롬프트에서 누락되므로, 비용뿐 아니라
            # 서사 품질이 붕괴한다. 발급 실패 시에는 기존처럼 캐시 없이 그레이스풀 진행.
            if not (session.cache_obj and session.cache_name):
                try:
                    await m_send("🔄 **[시스템 알림]** 활성 캐시가 없어 룰북 캐시를 선제 발급합니다. (명시적 삭제 후 재개 등)")
                    await _reissue_cache("캐시 선제 재발급 (캐시 부재)")
                except Exception as e:
                    await m_send(f"⚠️ 캐시 선제 발급 실패 — 이번 턴은 캐시 없이 진행합니다: {e}")

            response = await generate_with_retry()

            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)
            # NOTE: 비용 예측 모델 산정을 위한 실측 로그 — 사고 토큰이 출력의 몇 %를 차지하는지 수집.
            _visible = out_tokens - thought_tokens
            _ratio = (thought_tokens / out_tokens * 100) if out_tokens else 0.0
            print(f"[TOKENS] NARRATE in={in_tokens} cached={cached_tokens} "
                  f"out={out_tokens} (visible={_visible} thinking={thought_tokens}, {_ratio:.1f}%)")

            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            turn_cost = breakdown["total_krw"]
            session.total_cost += turn_cost
            # 비용 예측 통계 — 묘사층위 출력은 변동이 가장 크므로 이동평균이 핵심이다.
            core.update_stats(session, "narration", out_tokens, thought_tokens)

            label_prefix = "(GM) " if cost_log_prefix else ""
            core.write_cost_log(session.session_id, f"{cost_log_prefix}턴 진행 생성", in_tokens, cached_tokens, out_tokens, turn_cost,
                                session.total_cost)

            print(f"\n[{label_prefix}턴 진행 비용] session={session.session_id} In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} cost={core.format_cost(turn_cost)}")  # in/out/cached already guarded above

            # PROCEED 비용을 turn_cost_log에 적립한다.
            # NOTE: 턴 비용 보고 임베드는 더빙 합성 완료 후(아래)에 송출하여 TTS 비용까지 합산한다.
            proceed_label = f"{'(GM) ' if cost_log_prefix else ''}묘사층위(PROCEED)"
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": proceed_label, "cost": turn_cost,
                "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                "manifest": list(getattr(session, "last_proceed_manifest", [])),
            })

            full_ai_response = response.text

            if not full_ai_response:
                finish_reason = response.candidates[0].finish_reason if response.candidates else "Unknown"
                raise ValueError(
                    f"AI가 텍스트를 반환하지 않았습니다. (구글 API 강제 차단 혹은 모델 에러. 사유: {finish_reason})\n지시사항의 수위를 조절하거나 `!재생성`을 이용해 턴을 취소해 주십시오.")

            # PC 자율성 보호: AI가 NPC가 아닌 '플레이어 이름'으로 대사를 출력한 경우,
            # 로그 저장·파싱·스트리밍에 들어가기 전 문자열 단계에서 해당 발화 문단을 제거한다.
            pc_names = {p.get("name") for p in session.players.values() if p.get("name")}
            npc_names = set(session.npcs.keys())
            full_ai_response, _removed_pc_lines = core.strip_unauthorized_pc_dialogue(
                full_ai_response, pc_names, npc_names)
            if _removed_pc_lines:
                _uniq = ", ".join(dict.fromkeys(_removed_pc_lines))
                await m_send(f"🛡️ PC 자율성 보호: AI가 생성한 플레이어 대사({_uniq})를 출력 전 제거했습니다.")

            # 방어적 태그 스트립: 상태 변경은 지시문(instruction) 태그로 이미 적용되므로,
            # AI가 묘사 응답에 남긴 자:/태: 태그(에코 등)는 여기서 제거한다.
            # (제거하지 않으면 출력에 태그가 누출되고, 코드블럭 뒤에 붙으면 코드블럭 인식이 깨진다.)
            # NOTE: 이미지 태그(상|중|하:)는 세미콜론이 없어 '내상:중상' 등 서술·코드블럭을 오매칭할 수
            #       있으므로 응답 스트립 대상에서 제외한다(자:/태:는 세미콜론 필수라 오매칭 위험 없음).
            full_ai_response = re.sub(res_pattern, '', full_ai_response)
            full_ai_response = re.sub(status_pattern, '', full_ai_response)
            full_ai_response = re.sub(r'[ \t]{2,}', ' ', full_ai_response).strip()

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
            # #3: 같은 화자의 연속 대사를 하나로 통합 (이미지 중복 출력 방지)
            paragraphs = core.merge_consecutive_dialogues(paragraphs)

            # 플레이어 이름과 동일한 인물의 발화 문단 제거 (출력 단계 차단).
            # 위 문자열 strip은 'NPC가 아닌' PC 대사만 걸러내지만, 여기서는 출력 직전 파싱 기준으로
            # 화자 이름이 플레이어 이름과 일치하면 NPC 겸용 여부와 무관하게 해당 발화 문단을 제거한다.
            # (paragraphs는 이후 TTS·스트리밍·이미지 송출의 공통 입력이므로 한곳에서 차단된다.)
            _pc_speaker_dropped = []
            _kept_paras = []
            for _p in paragraphs:
                _d = core.parse_dialogue_paragraph(_p)
                if _d and _d[0] in pc_names:
                    _pc_speaker_dropped.append(_d[0])
                    continue
                _kept_paras.append(_p)
            paragraphs = _kept_paras
            if _pc_speaker_dropped:
                _uniq_pc = ", ".join(dict.fromkeys(_pc_speaker_dropped))
                await m_send(f"🛡️ 플레이어 이름({_uniq_pc})으로 된 발화 문단을 출력에서 제거했습니다.")

            # 출력(타이핑 연출) 시작 직전 대기 안내 메시지 제거
            if status_msg:
                await status_msg.done()
            status_msg = None

            # TTS 더빙(실험): 수동 !진행에서 토글 ON + 보이스 연결 시 '음성-텍스트 동기' 경로 사용.
            # (문단별 음성 길이에 텍스트 스트리밍 속도를 맞춤.) GM(cost_log_prefix)·미연결 제외.
            # dub: 더빙 합성 누적 결과 dict (비용·경고 처리는 출력 완료 후 일원화).
            dub = None
            dub_active = (
                getattr(session, "tts_enabled", False)
                and not cost_log_prefix
                and core.get_mixer(getattr(session, "voice_client", None)) is not None
            )

            # ── 비정규 NPC 미디어 배정 (기획 규정: 스트리밍 전) ──
            # 이미지 검색이 안 되는 인물의 이미지·목소리를 여기서 확정해야
            # 대사 이미지 출력과 더빙 목소리에 반영된다.
            # 동기 더빙 경로와 일반 스트리밍 경로 양쪽의 상류 지점이다.
            # 이미지·TTS가 모두 꺼져 있으면 내부에서 호출 자체를 생략한다.
            try:
                gm_cog = self.bot.get_cog("GMCog")
                if gm_cog and paragraphs:
                    await gm_cog._resolve_irregular_npcs(session, narrative_text, master_ch)
            except Exception as e:
                print(f"[비정규NPC] 배정 실패(진행에는 영향 없음): {e}")

            if not paragraphs:
                for kw in top_imgs + mid_imgs + bottom_imgs:
                    await core.send_image_by_keyword(game_channel, master_ch, session, kw)
            elif dub_active:
                # 음성-텍스트 동기 출력 (이미지 송출 포함). 합성·적재·스트리밍을 한 곳에서 처리.
                dub = await self._stream_paragraphs_synced(
                    session, paragraphs, game_channel, master_ch,
                    top_imgs, mid_imgs, bottom_imgs
                )
            else:
                # 비동기(또는 TTS off) 경로. 토글 ON이지만 보이스 미연결이면 no_voice 경고용으로 합성 시도.
                dub_task = None
                if getattr(session, "tts_enabled", False) and not cost_log_prefix:
                    tts_texts = []
                    for p in paragraphs:
                        d = core.parse_dialogue_paragraph(p)
                        spoken = core.clean_text_for_tts(d[1] if d else p)
                        if spoken:
                            tts_texts.append(spoken)
                    if tts_texts:
                        dub_task = asyncio.create_task(self._synthesize_and_enqueue(session, tts_texts))

                for i, paragraph in enumerate(paragraphs):
                    # 인물 대사 마커 분기: 이미지 자동 출력 + 헤더/말풍선 형식, '> ' 미부착
                    dialogue = core.parse_dialogue_paragraph(paragraph)
                    if dialogue:
                        speaker, content = dialogue
                        await core.maybe_send_speaker_image(game_channel, session, speaker)
                        formatted = core.format_dialogue_block(speaker, content)
                        await core.stream_text_to_channel(self.bot, game_channel, formatted,
                                                          words_per_tick=15, tick_interval=1.5,
                                                          quote_prefix=False)
                    else:
                        await core.stream_text_to_channel(self.bot, game_channel, paragraph,
                                                          words_per_tick=15, tick_interval=1.5)

                    if i == 0:
                        for kw in top_imgs:
                            await core.send_image_by_keyword(game_channel, master_ch, session, kw)

                    for kw in list(mid_imgs):
                        if kw in paragraph:
                            await core.send_image_by_keyword(game_channel, master_ch, session, kw)
                            mid_imgs.remove(kw)

                for kw in mid_imgs:
                    await core.send_image_by_keyword(game_channel, master_ch, session, kw)
                for kw in bottom_imgs:
                    await core.send_image_by_keyword(game_channel, master_ch, session, kw)

                # 백그라운드 더빙 합성 완료 대기 (음성 재생은 믹서 큐에서 계속 진행됨)
                if dub_task is not None:
                    try:
                        dub = await dub_task
                    except Exception as e:
                        print(f"[TTS] 더빙 태스크 오류(무시): {e}")
                        dub = None

            if code_block_text:
                await game_channel.send(code_block_text)

            # TTS 더빙 비용·경고 일원 처리 (동기/비동기 공통)
            if dub:
                if dub.get("no_voice"):
                    await m_send("🔇 TTS 더빙: 음성 채널에 연결돼 있지 않아 이번 턴은 건너뜁니다.")
                elif dub["enqueued"] == 0:
                    await m_send("⚠️ TTS 더빙: 합성된 음성이 없습니다. (`core.TTS_MODEL` 설정·API 응답 확인)")
                if dub["cost"] > 0:
                    session.total_cost += dub["cost"]
                    core.write_cost_log(session.session_id, "TTS 더빙",
                                        dub["in"], 0, dub["out"], dub["cost"], session.total_cost)
                    session.turn_cost_log.append(
                        {"label": f"TTS 더빙({dub['enqueued']}/{dub['total']}문단)", "cost": dub["cost"],
                         "in": dub["in"], "cached": 0, "out": dub["out"]})

            # 턴 비용 보고 임베드 송출 (PROCEED + 지시층위 등 누적 + TTS 더빙 합산)
            _turn_embed = core.build_turn_cost_embed(session.turn_count, session.turn_cost_log, session.total_cost)
            session.turn_cost_log.clear()
            await m_send(embed=_turn_embed)

            await m_send(f"✅ 묘사 연출 완료 (현재 {session.turn_count}턴 경과). 다음 턴 대기 중...")

            # NOTE: 자동 기억 압축은 이 지점(턴 종료 직후)이 아니라, 다음 5N+1 프로씨드 '시작 시점'에
            # 백그라운드로 개시된다(_execute_proceed 도입부 + _run_auto_compression). 5N 턴 !재생성 허용을 위함.

            await core.save_session_data(self.bot, session)

        except Exception as e:
            if status_msg:
                await status_msg.done()
            await m_send(f"⚠️ 시스템 오류가 발생했습니다: {str(e)}")
            session.is_processing = False
            try:
                if master_guild:
                    await game_channel.set_permissions(master_guild.default_role, send_messages=True)
            except Exception:
                pass
            return {"ok": False, "ai_text": "", "error": str(e)}

        session.is_processing = False
        try:
            if master_guild:
                await game_channel.set_permissions(master_guild.default_role, send_messages=True)
        except Exception as e:
            print(f"⚠️ 자동 채팅 해제 실패: {e}")

        return {"ok": True, "ai_text": full_ai_response, "error": None}

    async def _run_auto_compression(self, session, logs_to_compress: list, cost_log_prefix: str = ""):
        """
        누적 기억을 백그라운드에서 무손실 압축한다(프로씨드와 동시 실행).

        압축 대상은 호출 시점에 스냅샷된 logs_to_compress로 고정된다. 완료 후
        uncompressed_logs '앞'에서 len(logs_to_compress)개를 제거하므로, 그 사이 진행 중인
        프로씨드가 '뒤'에 append하는 이번 턴 로그와 경합하지 않는다. 실패해도 게임 진행 무영향.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content=None, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        session.is_compressing = True
        try:
            await m_send("⏳ (시스템: 백그라운드에서 자동 초정밀 기억 압축을 진행합니다...)")

            log_text = "\n\n".join(logs_to_compress)
            summary_prompt = core.build_compression_prompt(session, log_text)
            core.write_log(session.session_id, "api", f"[기억 압축 요청]\n{summary_prompt}")

            # 로우 플랜은 일정 횟수 이후 저비용 모델로 전환한다.
            comp_model = core.memory_plan.select_model(session)
            summary_response = await asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=comp_model,
                contents=summary_prompt,
                config=types.GenerateContentConfig(safety_settings=core.TRPG_SAFETY_SETTINGS),
            )

            meta = summary_response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)

            # 비용은 실제 사용한 모델 기준으로 계산해야 한다.
            turn_cost = core.calculate_upload_cost(comp_model, input_tokens=in_tokens,
                                                   output_tokens=out_tokens, cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost
            core.write_cost_log(session.session_id, f"{cost_log_prefix}자동 기억 압축", in_tokens, cached_tokens, out_tokens,
                                turn_cost, session.total_cost)
            print(f"[자동 기억 압축 비용] In:{in_tokens} Cached:{cached_tokens} Out:{out_tokens} | {core.format_cost(turn_cost)}")
            await m_send(embed=core.build_compression_cost_embed(
                "자동 기억 압축", in_tokens, cached_tokens, out_tokens, turn_cost, session.total_cost))

            new_compressed_segment = summary_response.text.strip()
            if session.compressed_memory:
                session.compressed_memory += f"\n{new_compressed_segment}"
            else:
                # 압축 선결제 정산 — 누적 선결제분과 실제 발생분의 차액을 산출한다.
                try:
                    settle = core.settle_compression(session, turn_cost)
                    core.update_stats(session, "compression", out_tokens, thought_tokens)
                    if settle["refund_ink"] or settle["charge_ink"]:
                        print(
                            f"[정산] 압축 선결제 {settle['prepaid_krw']}원 vs 실제 "
                            f"{settle['actual_krw']}원 → 환급 {settle['refund_ink']}잉크 "
                            f"/ 추가 {settle['charge_ink']}잉크"
                        )
                    session.last_compression_settle = settle
                except Exception as e:
                    print(f"[정산] 압축 정산 실패: {e}")

                # 되감기용 — 압축 발생 시점과 이전 원본을 남긴다.
                # 발생 시점 기록만으로 압축 주기(플랜별 상이)를 몰라도 정확히 롤백된다.
                try:
                    core.record_delta(
                        session, getattr(session, "gm_turns_done", 0), [],
                        compression={
                            "occurred": True,
                            "before": session.compressed_memory or "",
                        },
                    )
                except Exception as e:
                    print(f"[되감기] 압축 기록 실패: {e}")
                session.compressed_memory = new_compressed_segment
                # 압축 완료 시점 기록 — 재압축 방지와 로우 플랜 전환 판정의 근거.
                core.memory_plan.mark_compressed(session)

            # 앞에서 count만큼 제거 (스냅샷된 대상 로그). 이후 append된 이번 턴 로그는 보존.
            del session.uncompressed_logs[:len(logs_to_compress)]

            success_msg = f"✅ 자동 누적 압축 완료.\n**[최근 추가된 기억]**\n{new_compressed_segment}"
            if len(success_msg) > 2000:
                for i in range(0, len(success_msg), 2000):
                    await m_send(success_msg[i:i + 2000])
                    await asyncio.sleep(1)
            else:
                await m_send(success_msg)

            await core.save_session_data(self.bot, session)
        except Exception as e:
            await m_send(f"⚠️ 자동 기억 압축 중 오류 발생: {e}")
        finally:
            session.is_compressing = False

    async def _synthesize_and_enqueue(self, session, texts, voice_name=None) -> dict:
        """
        문단 텍스트를 순서대로 TTS 합성해 믹서 voice 큐에 적재한다. (회계는 호출 측 담당)

        합성은 순차(await)이므로 큐 적재 순서가 보장되며, 첫 문단이 합성되는 즉시 재생이 시작되고
        뒤 문단은 재생되는 동안 이어서 합성된다(파이프라이닝). voice_name=None이면 기본 나레이터 보이스 사용.

        Returns:
            dict: {"enqueued": int, "total": int, "cost": float, "in": int, "out": int, "no_voice": bool}
                  비용·로그 기록·임베드 반영은 호출자가 반환값으로 처리한다.
        """
        vc = getattr(session, "voice_client", None)
        mixer = core.get_mixer(vc)
        if mixer is None:
            return {"enqueued": 0, "total": len(texts), "cost": 0.0, "in": 0, "out": 0, "no_voice": True}

        total_cost = 0.0
        total_in = total_out = 0
        enqueued = 0
        for t in texts:
            pcm, cost, in_tok, out_tok = await core.synthesize_tts_pcm(self.bot, t, voice_name=voice_name)
            if pcm:
                mixer.enqueue_voice(core.PCMBytesAudioSource(pcm, volume=core.TTS_NARRATION_VOLUME))
                enqueued += 1
            total_cost += cost
            total_in += in_tok
            total_out += out_tok

        return {"enqueued": enqueued, "total": len(texts), "cost": total_cost,
                "in": total_in, "out": total_out, "no_voice": False}

    async def _stream_paragraphs_synced(self, session, paragraphs, game_channel, master_ch,
                                        top_imgs, mid_imgs, bottom_imgs, voice_name=None) -> dict:
        """
        TTS 더빙 ON + 보이스 연결 시 사용하는 '음성-텍스트 동기' 출력 경로.

        각 문단을 TTS 합성해 믹서 voice 큐에 적재(재생 시작)하고, 그 문단 텍스트를 음성 길이
        (len(pcm)/TTS_PCM_BYTES_PER_SEC 초)에 맞춘 속도로 스트리밍한다. 다음 문단은 현재 문단을
        출력하는 동안 미리 합성(prefetch)해 파이프라이닝을 유지한다. 순차 voice 큐와 텍스트가
        문단 단위로 lock-step을 이루며, 합성 지연이 생기면 음성·텍스트가 함께 대기해 재동기된다.

        이미지 송출(상/중/하)은 기존 비동기 경로와 동일한 규칙으로 인터리브한다.

        Returns:
            dict: _synthesize_and_enqueue와 동일 형식 (비용·경고는 호출 측에서 처리).
        """
        mixer = core.get_mixer(getattr(session, "voice_client", None))

        # (raw_paragraph, display_text, spoken_text, is_dialogue, speaker)
        items = []
        for p in paragraphs:
            d = core.parse_dialogue_paragraph(p)
            if d:
                speaker, content = d
                items.append((p, core.format_dialogue_block(speaker, content),
                              core.clean_text_for_tts(content), True, speaker))
            else:
                items.append((p, p, core.clean_text_for_tts(p), False, None))

        async def _synth(spoken):
            if not spoken:
                return (b"", 0.0, 0, 0)
            return await core.synthesize_tts_pcm(self.bot, spoken, voice_name=voice_name)

        total_cost = 0.0
        total_in = total_out = 0
        enqueued = 0

        # 첫 문단 선합성 (합성 대기 동안 타이핑 인디케이터 노출)
        if items:
            async with game_channel.typing():
                pcm, cost, in_tok, out_tok = await _synth(items[0][2])
        else:
            pcm, cost, in_tok, out_tok = (b"", 0.0, 0, 0)

        for i, (raw, display, spoken, is_dialogue, speaker) in enumerate(items):
            # 다음 문단 prefetch (현재 문단 출력 동안 백그라운드 합성)
            next_task = None
            if i + 1 < len(items):
                next_task = asyncio.create_task(_synth(items[i + 1][2]))

            total_cost += cost
            total_in += in_tok
            total_out += out_tok

            # 음성 적재(재생 시작) + 재생 길이 산출
            duration = None
            if pcm:
                mixer.enqueue_voice(core.PCMBytesAudioSource(pcm, volume=core.TTS_NARRATION_VOLUME))
                enqueued += 1
                duration = len(pcm) / float(core.TTS_PCM_BYTES_PER_SEC)

            if is_dialogue:
                await core.maybe_send_speaker_image(game_channel, session, speaker)

            await core.stream_text_to_channel(
                self.bot, game_channel, display,
                quote_prefix=not is_dialogue, total_duration=duration,
            )

            if i == 0:
                for kw in top_imgs:
                    await core.send_image_by_keyword(game_channel, master_ch, session, kw)

            for kw in list(mid_imgs):
                if kw in raw:
                    await core.send_image_by_keyword(game_channel, master_ch, session, kw)
                    mid_imgs.remove(kw)

            # 다음 문단 합성 결과 수령
            if next_task is not None:
                pcm, cost, in_tok, out_tok = await next_task
            else:
                pcm, cost, in_tok, out_tok = (b"", 0.0, 0, 0)

        for kw in mid_imgs:
            await core.send_image_by_keyword(game_channel, master_ch, session, kw)
        for kw in bottom_imgs:
            await core.send_image_by_keyword(game_channel, master_ch, session, kw)

        return {"enqueued": enqueued, "total": len(items), "cost": total_cost,
                "in": total_in, "out": total_out, "no_voice": False}

    @commands.command(name="더빙테스트")
    async def test_tts(self, ctx, voice: str = None):
        """
        직전 `!진행` 묘사의 마지막 문단을 TTS로 다시 읽어준다. 스타일 프롬프트는 그대로 유지.

        !더빙테스트            — 기본 나레이터 보이스로 낭독
        !더빙테스트 [보이스]   — 지정한 보이스로 낭독 (예: `!더빙테스트 Gacrux`)

        토글(`!더빙`) 상태와 무관하게 즉시 실행되며(음성 채널 연결 필요),
        비용은 일반 더빙과 동일하게 집계·`cost_log`에 기록된다.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        # 보이스 인자 검증 (대소문자 무시 → 정식 표기로 정규화)
        voice_name = None
        if voice:
            voice_name = next((v for v in core.TTS_VOICES if v.lower() == voice.lower()), None)
            if voice_name is None:
                return await ctx.send(
                    f"⚠️ 알 수 없는 보이스 `{voice}`.\n사용 가능: {', '.join(core.TTS_VOICES)}")

        # 직전 모델 출력(가장 최근 role="model" 텍스트) 탐색
        last_text = None
        for content in reversed(session.raw_logs):
            if content.role == "model" and content.parts and getattr(content.parts[0], "text", None):
                last_text = content.parts[0].text
                break
        if not last_text:
            return await ctx.send("⚠️ 직전 진행 묘사를 찾을 수 없습니다. 먼저 `!진행`을 실행하세요.")

        # 상태창 코드블럭을 제외한 마지막 문단 추출
        m = re.search(r'(.*)(```.*?```)\s*$', last_text, re.DOTALL)
        narrative = (m.group(1) if m else last_text).strip()
        paras = [p.strip() for p in narrative.split('\n\n') if p.strip()]
        if not paras:
            return await ctx.send("⚠️ 읽을 문단이 없습니다.")

        last_para = paras[-1]
        d = core.parse_dialogue_paragraph(last_para)
        spoken = core.clean_text_for_tts(d[1] if d else last_para)
        if not spoken:
            return await ctx.send("⚠️ 정제 후 읽을 텍스트가 없습니다.")

        await ctx.send(
            f"🔊 직전 묘사 마지막 문단을 낭독합니다 (보이스 `{voice_name or core.TTS_NARRATOR_VOICE}`):\n> {spoken[:300]}")

        dub = await self._synthesize_and_enqueue(session, [spoken], voice_name=voice_name)
        if dub.get("no_voice"):
            return await ctx.send(
                "🔇 음성 채널에 연결돼 있지 않습니다. `!브금`/`!플리`로 입장 후 다시 시도하세요.")
        if dub["enqueued"] == 0:
            return await ctx.send(
                "⚠️ 합성된 음성이 없습니다. (`core.TTS_MODEL` 설정 또는 API 응답을 확인하세요)")
        if dub["cost"] > 0:
            session.total_cost += dub["cost"]
            core.write_cost_log(session.session_id, "TTS 더빙(테스트)",
                                dub["in"], 0, dub["out"], dub["cost"], session.total_cost)
            await core.save_session_data(self.bot, session)
        await ctx.send(f"🔊 TTS 더빙 테스트 재생 (+{core.format_cost(dub['cost'])})")

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

        # [압축 타이밍 이동] 압축은 5N 턴 종료 직후가 아니라 5N+1 프로씨드 시작 시점에 백그라운드로
        # 실행되므로, 5의 배수 턴 자체는 롤백이 가능하다. 다만 그 백그라운드 압축이 진행 중인 짧은
        # 창에서는 롤백 대상 로그와 경합할 수 있어 잠시 대기를 안내한다.
        if getattr(session, "is_compressing", False):
            return await ctx.send("⏳ 기억 압축이 진행 중입니다. 수 초 후 다시 시도해 주세요.")

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


    @commands.command(name="출력물")
    async def show_last_output(self, ctx):
        """
        가장 최근 턴의 AI 출력 텍스트를 마스터 채널에 전송.
        디스코드 2000자 제한을 고려하여 1950자 단위로 분할 전송.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        # raw_logs에서 가장 최근 model 응답 탐색
        last_model_text = None
        for content in reversed(session.raw_logs):
            if content.role == "model":
                last_model_text = content.parts[0].text
                break

        if not last_model_text:
            return await ctx.send("⚠️ 출력할 직전 턴의 묘사가 존재하지 않습니다.")

        await ctx.send(f"📄 **[직전 턴 출력물 — {session.turn_count}턴]** (아래 텍스트를 수정 후 `!수정`으로 반영)")

        chunk_size = 1950
        for i in range(0, len(last_model_text), chunk_size):
            await ctx.send(last_model_text[i:i + chunk_size])


    @commands.command(name="수정")
    async def edit_last_output(self, ctx, *, new_text: str):
        """
        직전 턴의 게임 채널 출력물을 입력된 텍스트로 수정.

        디스코드 메시지 수정 API(edit)를 사용해 기존 메시지를 덮어쓰고,
        raw_logs·uncompressed_logs·game_chat 로그 파일도 함께 동기화.
        모든 알림은 마스터 채널에만 전송.
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")

        game_channel = self.bot.get_channel(session.game_ch_id)
        if not game_channel:
            return await ctx.send("⚠️ 게임 채널을 찾을 수 없습니다.")

        if getattr(session, "is_processing", False):
            return await ctx.send("⏳ 시스템이 다른 명령을 처리 중입니다. 잠시만 기다려주십시오.")

        # 수정 대상 model 로그 위치 탐색
        last_model_idx = None
        for i in range(len(session.raw_logs) - 1, -1, -1):
            if session.raw_logs[i].role == "model":
                last_model_idx = i
                break

        if last_model_idx is None:
            return await ctx.send("⚠️ 수정할 직전 턴의 묘사가 존재하지 않습니다.")

        if not getattr(session, "last_turn_anchor_id", None):
            return await ctx.send("⚠️ 앵커 정보가 없어 게임 채널 메시지를 특정할 수 없습니다.\n(세션 복구 직후이거나 `!진행` 이전 상태입니다.)")

        # ── 0. 수정 전 원본을 텍스트 로그에 먼저 보존 ──
        original_text = session.raw_logs[last_model_idx].parts[0].text
        core.write_log(
            session.session_id, "game_chat",
            f"[GM 수정 전 원본 ({session.turn_count}턴)]: {original_text}"
        )

        session.is_processing = True
        try:
            # ── 1. 앵커 이후 봇 텍스트 메시지 수집 (이미지·파일 제외) ──
            try:
                anchor_msg = await game_channel.fetch_message(session.last_turn_anchor_id)
            except discord.NotFound:
                await ctx.send("⚠️ 앵커 메시지를 찾을 수 없습니다. 메시지가 삭제되었을 수 있습니다.")
                return

            bot_text_msgs = []
            async for msg in game_channel.history(after=anchor_msg, limit=100):
                if msg.author == self.bot.user and not msg.attachments:
                    bot_text_msgs.append(msg)
            bot_text_msgs.sort(key=lambda m: m.created_at)

            # ── 2. 새 텍스트에서 서술부와 코드블럭 분리 (proceed_turn 동일 로직) ──
            code_block_match = re.search(r'(.*)(```.*?```)\s*$', new_text, re.DOTALL)
            if code_block_match:
                new_narrative = code_block_match.group(1).strip()
                new_code_block = code_block_match.group(2).strip()
            else:
                new_narrative = new_text.strip()
                new_code_block = ""

            # 문단 단위로 분리 → 연속 동일 화자 통합 → 대사 마커 여부에 따라 포맷 분기 → 1950자 초과 시 추가 분할
            new_paragraphs = core.merge_consecutive_dialogues(
                [p.strip() for p in new_narrative.split('\n\n') if p.strip()]
            )
            new_chunks = []
            for p in new_paragraphs:
                dialogue = core.parse_dialogue_paragraph(p)
                if dialogue:
                    speaker, content = dialogue
                    formatted = core.format_dialogue_block(speaker, content)
                else:
                    formatted = p if p.startswith(">") else f"> {p}"
                for j in range(0, len(formatted), 1950):
                    new_chunks.append(formatted[j:j + 1950])
            if new_code_block:
                new_chunks.append(new_code_block)

            if not new_chunks:
                await ctx.send("⚠️ 수정할 내용이 없습니다.")
                return

            # ── 3. 기존 메시지 수정 / 초과분 삭제 / 부족분 추가 ──
            for i, msg in enumerate(bot_text_msgs):
                if i < len(new_chunks):
                    try:
                        await msg.edit(content=new_chunks[i])
                    except Exception as e:
                        print(f"⚠️ 메시지 수정 실패 (id={msg.id}): {e}")
                else:
                    try:
                        await msg.delete()
                    except Exception as e:
                        print(f"⚠️ 초과 메시지 삭제 실패 (id={msg.id}): {e}")

            # 기존 메시지보다 새 청크가 많을 경우 추가 전송
            if len(new_chunks) > len(bot_text_msgs):
                for chunk in new_chunks[len(bot_text_msgs):]:
                    await game_channel.send(chunk)

            # ── 4. raw_logs 갱신 ──
            session.raw_logs[last_model_idx] = types.Content(
                role="model",
                parts=[types.Part.from_text(text=new_text.strip())]
            )

            # ── 5. uncompressed_logs에서 마지막 [GM 묘사] 항목 교체 ──
            for i in range(len(session.uncompressed_logs) - 1, -1, -1):
                if session.uncompressed_logs[i].startswith("[GM 묘사]:"):
                    session.uncompressed_logs[i] = f"[GM 묘사]: {new_text.strip()}"
                    break

            # ── 6. 채팅 로그 기록 및 세션 저장 (수정 후 내용) ──
            core.write_log(
                session.session_id, "game_chat",
                f"[GM 수정 후 ({session.turn_count}턴)]: {new_text.strip()}"
            )
            await core.save_session_data(self.bot, session)
            await ctx.send(f"✅ {session.turn_count}턴 출력물이 수정되었습니다.")

        except Exception as e:
            await ctx.send(f"⚠️ 수정 중 오류가 발생했습니다: {e}")
        finally:
            session.is_processing = False


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
            in_tokens, out_tokens, cached_tokens, thought_tokens = core.extract_token_usage(meta)

            turn_cost = core.calculate_upload_cost(core.LOGIC_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                                            cached_read_tokens=cached_tokens)
            session.total_cost += turn_cost

            core.write_cost_log(session.session_id, "수동 기억 압축", in_tokens, cached_tokens, out_tokens, turn_cost,
                                session.total_cost)

            print(f"[수동 기억 압축 비용] In:{in_tokens} Cached:{cached_tokens} Out:{out_tokens} | {core.format_cost(turn_cost)}")
            _comp_embed = core.build_compression_cost_embed(
                "수동 기억 압축", in_tokens, cached_tokens, out_tokens, turn_cost, session.total_cost
            )
            await ctx.send(embed=_comp_embed)

            new_compressed_segment = summary_response.text.strip()
            if session.compressed_memory:
                session.compressed_memory += f"\n{new_compressed_segment}"
            else:
                # 압축 선결제 정산 — 누적 선결제분과 실제 발생분의 차액을 산출한다.
                try:
                    settle = core.settle_compression(session, turn_cost)
                    core.update_stats(session, "compression", out_tokens, thought_tokens)
                    if settle["refund_ink"] or settle["charge_ink"]:
                        print(
                            f"[정산] 압축 선결제 {settle['prepaid_krw']}원 vs 실제 "
                            f"{settle['actual_krw']}원 → 환급 {settle['refund_ink']}잉크 "
                            f"/ 추가 {settle['charge_ink']}잉크"
                        )
                    session.last_compression_settle = settle
                except Exception as e:
                    print(f"[정산] 압축 정산 실패: {e}")

                # 되감기용 — 압축 발생 시점과 이전 원본을 남긴다.
                # 발생 시점 기록만으로 압축 주기(플랜별 상이)를 몰라도 정확히 롤백된다.
                try:
                    core.record_delta(
                        session, getattr(session, "gm_turns_done", 0), [],
                        compression={
                            "occurred": True,
                            "before": session.compressed_memory or "",
                        },
                    )
                except Exception as e:
                    print(f"[되감기] 압축 기록 실패: {e}")
                session.compressed_memory = new_compressed_segment
                # 압축 완료 시점 기록 — 재압축 방지와 로우 플랜 전환 판정의 근거.
                core.memory_plan.mark_compressed(session)

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