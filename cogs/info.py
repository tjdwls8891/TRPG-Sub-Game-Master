# !정보 (F4) — 세계관·설정 질의 응답.
#
# 마스터 채널에서만 동작하며, 세션 캐시(세계관 룰북)를 항상 읽고 질문에 매칭되는
# 키워드북(keyword_memory) 항목을 온디맨드로 주입해 AI가 설정을 '설명'하게 한다.
# 캐시를 쓰는 호출이라 캐시의 GM 시스템 지시문을 덮을 수 없으므로, 설정 안내자
# 지시문(prompts.INFO_SYSTEM_INSTRUCTION)을 사용자 콘텐츠 최상단에 프리앰블로 넣는다.
import asyncio
import discord
from discord.ext import commands
from google.genai import types

import core
import prompts

INFO_MAX_CHARS = 1000  # 응답 최대 길이 (F4: 최소 1문장 ~ 최대 1000자)


class InfoCog(commands.Cog):
    """설정 안내자 — !정보 [질문]으로 세계관/설정 질의에 답한다."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="정보")
    async def info(self, ctx, *, question: str = None):
        """
        세계관·설정에 대한 질문에 AI가 캐시(룰북)+키워드북을 근거로 답한다.
        사용법: !정보 [질문]  (마스터 채널 전용)
        """
        session = self.bot.active_sessions.get(ctx.channel.id)
        if not session or ctx.channel.id != session.master_ch_id:
            return await ctx.send("이 명령어는 마스터 채널에서만 사용할 수 있습니다.")
        if not question or not question.strip():
            return await ctx.send("사용법: `!정보 [질문]` — 세계관·설정에 대해 물어보세요.")
        if not (session.cache_obj and session.cache_name):
            return await ctx.send("⚠️ 활성 캐시가 없습니다. `!캐시 재발급` 후 다시 시도하세요.")

        q = question.strip()

        # 키워드북 온디맨드 스캔 (질문 텍스트 기준으로 매칭)
        seen, hits, hit_names = set(), [], []
        for mem in session.scenario_data.get("keyword_memory", []):
            for kw in mem.get("keywords", []):
                if kw and kw in q:
                    desc = mem.get("description", "")
                    if desc and desc not in seen:
                        seen.add(desc)
                        hits.append(desc)
                        hit_names.append(mem.get("id") or kw)
                    break
        if hits:
            km_block = "\n[키워드북 참조 — 질문에 매칭된 설정 상세]\n" + "\n\n".join(hits) + "\n"
        else:
            km_block = "\n[키워드북 참조] (질문에 직접 매칭된 항목 없음 — 캐시의 세계관 개관을 근거로 답하십시오.)\n"

        user_text = f"{prompts.INFO_SYSTEM_INSTRUCTION}\n{km_block}\n[질문]\n{q}\n"
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_text)])]
        config = types.GenerateContentConfig(
            cached_content=session.cache_name,
            temperature=0.3,
            safety_settings=core.TRPG_SAFETY_SETTINGS,
        )

        try:
            async with ctx.typing():
                resp = await asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=core.DEFAULT_MODEL,
                    contents=contents,
                    config=config,
                )
        except Exception as e:
            return await ctx.send(f"⚠️ 정보 조회 중 오류가 발생했습니다: {e}")

        try:
            text = (resp.text or "").strip()
        except Exception:
            text = ""
        if not text:
            return await ctx.send("⚠️ 답변을 생성하지 못했습니다. (안전 필터 또는 응답 없음)")
        if len(text) > INFO_MAX_CHARS:
            text = text[:INFO_MAX_CHARS].rstrip() + "…"

        await ctx.send(f"📖 {text}")

        # 비용 보고 ([정보] 접두) — 토큰 내역 + 입력에 주입된 정보 목록을 임베드로 제시
        try:
            meta = resp.usage_metadata
            in_t, out_t, cached_t, thought_t = core.extract_token_usage(meta)
            breakdown = core.calculate_text_gen_cost_breakdown(
                core.DEFAULT_MODEL, input_tokens=in_t, output_tokens=out_t, cached_read_tokens=cached_t
            )
            session.total_cost += breakdown["total_krw"]
            core.write_cost_log(session.session_id, "[정보] 설정 질의", in_t, cached_t, out_t, breakdown["total_krw"], session.total_cost)
            await core.save_session_data(self.bot, session)

            injected = "캐시(세계관 룰북)"
            if hit_names:
                injected += "\n키워드북: " + ", ".join(hit_names)
            else:
                injected += "\n(질문 매칭 키워드북 항목 없음 — 캐시 개관만 참조)"
            cost_embed = core.build_text_gen_cost_embed(
                label="설정 질의 (!정보)",
                model_id=core.DEFAULT_MODEL,
                breakdown=breakdown,
                turn_cost=breakdown["total_krw"],
                total_cost=session.total_cost,
                extra_fields=[("📥 입력에 주입된 정보", injected[:1020], False)],
            )
            cost_embed.color = 0x1ABC9C
            await ctx.send(embed=cost_embed)
        except Exception as e:
            print(f"[Info] 비용 보고 실패: {e}")


async def setup(bot):
    await bot.add_cog(InfoCog(bot))
