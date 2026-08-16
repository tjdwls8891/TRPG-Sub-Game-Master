# 인물 대사 마커 처리 및 채널 스트리밍 — @대사:이름|본문 파싱, 인물 이미지 자동 송출
import re
import os
import math
import asyncio

import discord

from .io import write_log


# ========== [대기 중 상태 안내 메시지] ==========
async def send_status_message(channel, text: str):
    """
    처리 대기 동안 게임 채널에 '~하는 중' 안내 메시지를 전송하고 핸들을 반환한다.
    실패 시 None. game_chat 로그에는 기록하지 않는다(일시적 연출용).
    """
    if channel is None:
        return None
    try:
        return await channel.send(text)
    except Exception:
        return None


async def clear_status_message(msg):
    """send_status_message가 반환한 메시지를 삭제한다(실패 무시)."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass


# ========== [인물 대사 마커 처리] ==========
# NOTE: AI가 출력한 `@대사:이름|본문` 마커를 감지하여 인물 헤더+말풍선 형식으로 변환.
#       마커 외 다른 텍스트가 섞이면 일반 묘사로 처리되도록 엄격 매칭.
DIALOGUE_MARKER_PATTERN = re.compile(r'^@대사:([^|\n]+)\|(.+)$', re.DOTALL)


def parse_dialogue_paragraph(paragraph: str):
    """
    문단이 인물 대사 마커(@대사:이름|본문)이면 (이름, 본문) 튜플을 반환, 아니면 None.
    """
    text = paragraph.strip()
    m = DIALOGUE_MARKER_PATTERN.match(text)
    if m:
        speaker = m.group(1).strip()
        content = m.group(2).strip()
        if speaker and content:
            return speaker, content
    return None


def format_dialogue_block(speaker: str, content: str) -> str:
    """인물 대사 문단을 디스코드 출력용 헤더 + 말풍선 마크다운으로 포매팅."""
    return f"## ▍{speaker}\n## 「 {content} 」"


def strip_unauthorized_pc_dialogue(text: str, pc_names, npc_names):
    """
    AI 출력에서 '플레이어 이름'으로 된 대사 마커(@대사:이름|본문)를 문자열 단계에서 제거한다.

    제거 조건: speaker가 PC 이름이면서 NPC 목록에 없을 때
    (= GM이 선언하지 않은 PC의 대사를 AI가 임의 창작한 경우 → PC 자율성 침해).
    NPC이기도 한 이름, PC가 아닌 이름(미상 인물 등)은 보존한다.

    문단(\\n\\n) 단위로 검사한다. 대사 마커는 출력 규약상 별도 문단·단일 라인으로 나오므로
    다운스트림 파싱(parse_dialogue_paragraph)과 동일한 기준을 사용한다.

    Args:
        text (str): AI 원본 응답 문자열
        pc_names: PC 캐릭터 이름 집합/목록
        npc_names: NPC 이름 집합/목록

    Returns:
        tuple[str, list[str]]: (정제된 텍스트, 제거된 화자 이름 목록)
    """
    if not text:
        return text, []
    pc_set = set(pc_names or ())
    npc_set = set(npc_names or ())

    paragraphs = text.split("\n\n")
    kept: list[str] = []
    removed: list[str] = []
    for p in paragraphs:
        parsed = parse_dialogue_paragraph(p)
        if parsed:
            speaker = parsed[0]
            if speaker in pc_set and speaker not in npc_set:
                removed.append(speaker)
                continue
        kept.append(p)
    return "\n\n".join(kept), removed


def merge_consecutive_dialogues(paragraphs: list[str]) -> list[str]:
    """
    같은 화자의 연속된 대사 문단을 하나의 문단으로 통합.

    예)
      @대사:레비|어때? 감각이 느껴져?
      @대사:레비|체온은 정상인데, 불편한 곳은?
    →
      @대사:레비|어때? 감각이 느껴져? 체온은 정상인데, 불편한 곳은?

    연속 여부 기준: 두 대사 문단 사이에 다른 문단(일반 묘사 또는 다른 화자 대사)이 없어야 함.
    이 처리를 통해 동일 인물 이미지가 연속으로 중복 출력되는 것을 방지한다.

    Args:
        paragraphs (list[str]): split('\n\n')으로 분리된 문단 리스트

    Returns:
        list[str]: 연속 동일 화자 대사가 통합된 문단 리스트
    """
    merged: list[str] = []
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        dialogue = parse_dialogue_paragraph(p)
        if not dialogue:
            merged.append(p)
            i += 1
            continue

        speaker, content = dialogue
        parts = [content]

        # 바로 다음 문단부터 같은 화자의 대사가 이어지는지 확인
        j = i + 1
        while j < len(paragraphs):
            next_d = parse_dialogue_paragraph(paragraphs[j])
            if next_d and next_d[0] == speaker:
                parts.append(next_d[1])
                j += 1
            else:
                break

        if len(parts) > 1:
            merged_content = " ".join(parts)
            merged.append(f"@대사:{speaker}|{merged_content}")
        else:
            merged.append(p)

        i = j

    return merged


async def maybe_send_speaker_image(channel, session, speaker: str) -> bool:
    """
    미디어 키워드 목록에 인물 이름과 일치하는 항목이 있으면 이미지를 전송.

    매칭 우선순위:
        1) media_keywords[speaker]        (정확한 키워드 매칭)
        2) media/{scenario_id}/{speaker}.png 파일 직접 존재 검사

    실패 시 조용히 False 반환 (대사 출력은 이어서 진행).
    """
    if not speaker:
        return False
    media_keywords = session.scenario_data.get("media_keywords", {})
    media_dir = f"media/{session.scenario_id}"

    candidate_filename = None
    if speaker in media_keywords:
        candidate_filename = media_keywords[speaker]
    else:
        # 폴백: 파일 직접 검사
        direct_path = os.path.join(media_dir, f"{speaker}.png")
        if os.path.exists(direct_path):
            candidate_filename = f"{speaker}.png"

    if not candidate_filename:
        # 비정규 NPC — 즉석 등장 인물에게 배정된 이미지를 쓴다.
        try:
            from .irregular_npc import image_path_for
            alt = image_path_for(session, speaker)
        except Exception:
            alt = None
        if alt:
            try:
                await channel.send(file=discord.File(alt))
                return True
            except Exception:
                return False
        return False

    filepath = os.path.join(media_dir, candidate_filename)
    if not os.path.exists(filepath):
        return False

    try:
        await channel.send(file=discord.File(filepath))
        return True
    except Exception as e:
        print(f"[Dialogue Image] {speaker} 이미지 전송 실패: {e}")
        return False


async def stream_text_to_channel(bot, channel, text: str, words_per_tick: int = 10, tick_interval: float = 1.5,
                                  quote_prefix: bool = True, total_duration: float | None = None):
    """
    디스코드 채널에 텍스트를 문단과 단어 단위로 쪼개어 타이핑 치듯 스트리밍 연출.

    NOTE: 한 번에 방대한 텍스트가 출력되는 것을 막아 TRPG 특유의 시각적 긴장감을 조성하고,
    디스코드 API의 메시지 전송 제한(Rate Limit)을 우회하기 위한 비동기 sleep 로직 적용.

    Args:
        bot: 메인 봇 인스턴스
        channel (discord.TextChannel): 텍스트를 출력할 디스코드 채널 객체
        text (str): 출력할 원본 전체 텍스트
        words_per_tick (int): 한 번의 갱신에 출력할 단어 수
        tick_interval (float): 갱신 간격 (초 단위)
        quote_prefix (bool): True면 문단 앞에 '> '를 자동 부착 (기본). 인물 대사 등 헤더 마크다운이 들어간 문단은 False.
        total_duration (float | None): 지정 시(>0) 이 문단 전체를 해당 초만큼에 걸쳐 출력하도록
            틱당 단어 수·간격을 자동 환산한다. TTS 음성 길이에 텍스트 속도를 맞출 때 사용.
            (이 경우 words_per_tick/tick_interval 인자는 무시된다.)
    """
    session = bot.active_sessions.get(channel.id)
    paragraphs = text.split('\n\n')

    for paragraph in paragraphs:
        if not paragraph.strip():
            continue

        if quote_prefix:
            current_text = "> " if not paragraph.startswith(">") else ""
        else:
            current_text = ""

        words = paragraph.split(' ')

        # 음성 길이 동기화: total_duration이 주어지면 1.0초 간격 기준으로 틱 수를 정하고,
        # 단어 수를 그 틱 수에 균등 분배해 총 출력 시간이 음성 길이에 수렴하도록 한다.
        # (간격을 1.0초로 둬 디스코드 메시지 edit 레이트리밋 여유를 확보)
        wpt = words_per_tick
        interval = tick_interval
        if total_duration and total_duration > 0:
            _SYNC_TICK = 1.0
            target_ticks = max(1, round(total_duration / _SYNC_TICK))
            wpt = max(1, math.ceil(len(words) / target_ticks))
            interval = _SYNC_TICK

        display_text = current_text

        # 첫 번째 청크를 즉시 포함해 초기 메시지를 전송한다.
        # (예전엔 빈 텍스트 + ✍️ 만 먼저 보내고 첫 청크는 sleep 이후에야 붙어,
        #  첫 틱에 내용 없이 작성 이모지만 노출되는 문제가 있었다.)
        first_chunk = words[:wpt]
        display_text += " ".join(first_chunk) + " "
        current_message = await channel.send(display_text + "✍️")

        # 두 번째 청크부터는 기존과 동일하게 sleep 후 edit로 이어 붙인다.
        for i in range(wpt, len(words), wpt):
            chunk = words[i:i + wpt]
            display_text += " ".join(chunk) + " "

            await asyncio.sleep(interval)

            try:
                if len(display_text) > 1950:
                    break
                await current_message.edit(content=display_text + "✍️")
            except discord.errors.HTTPException:
                pass

        final_text = display_text[:2000].strip()
        await current_message.edit(content=final_text)

        if session:
            write_log(session.session_id, "game_chat", f"[GM]: {final_text}")
