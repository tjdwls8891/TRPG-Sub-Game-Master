"""자동 턴 진행 — 해석(판단)·선언 검증·지시·판정·서술·관찰(추출)·커밋·표시 단계의 오케스트레이션(GMCog 믹스인).

리팩터링 T-5: adapters/discord/turn_flow.py에서 본문 그대로 옮겼다. 디스코드·core를 import하지 않는다 —
디스코드 객체·화면·테스트가 바꿔 끼우는 이름은 어댑터 포트 self.rt(adapters/discord/runtime.py)로 받는다."""
from __future__ import annotations

import re
import json
import copy
import random
import asyncio
import time
import dataclasses
from types import SimpleNamespace
from google.genai import types
from indaim.turn.declaration import _parse_gate, _gate_blocks, _gate_rolls_discovery, _gate_notice, _discovery_outcome, _declaration_block
from indaim.turn.roll_sides import _roll_domains, _judge_plan, _pc_skills, _party_round
from indaim.turn.brief import MAX_CLARIFY_PER_MESSAGE, MAX_NARRATE_PER_MESSAGE, _build_judgment_user_prompt, _build_logic_user_prompt, _sys_text, _clean_proceed_instruction, _interp_note
from indaim.content.prompts import CACHE_TIME_RESPONSE_SCHEMA, CACHE_TIME_SYSTEM_INSTRUCTION, EXTRACTION_SYSTEM_INSTRUCTION, NPC_DETAIL_RESPONSE_SCHEMA, NPC_DETAIL_SYSTEM_INSTRUCTION, IRREGULAR_NPC_RESPONSE_SCHEMA, IRREGULAR_NPC_SYSTEM_INSTRUCTION, JUDGMENT_RESPONSE_SCHEMA, JUDGMENT_SYSTEM_INSTRUCTION, GM_LOGIC_RESPONSE_SCHEMA, GM_LOGIC_SYSTEM_INSTRUCTION, NARRATIVE_PLAN_SCHEMA, NARRATIVE_PLANNER_SYSTEM_INSTRUCTION, build_narrate_prompt, NARRATIVE_DIRECTION_SCHEMA, NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION
from indaim.turn.outcomes import ExtractionProviderFailure
from indaim.turn.outcomes import _set_roll_open
from indaim.turn.config import COST_LOG_PREFIX, EXTRACTION_MAX_RETRIES, EXTRACTION_MODEL, EXTRACTION_THINKING_BUDGET, GATE_UNVERIFIABLE_NOTICE, JUDGMENT_MAX_RETRIES, JUDGMENT_MODEL, MAX_ITERATIONS_PER_MESSAGE, WORLD_REACTION_AUTHORITY_BLOCK, _OUTCOME_DELIVERY_FAILED, _OUTCOME_EXTRACTION_FAILED, _OUTCOME_NO_NARRATION, _OUTCOME_READY, _OUTCOME_SUPERSEDED
from indaim.ai import cache as _cache
from indaim.ai import resilience as _resilience
from indaim.app import constants as _constants
from indaim.app import io as _io
from indaim.app import logs as _logs
from indaim.app import session_open as _session_open
from indaim.app import utils as _utils
from indaim.app import world_lifecycle as _world_lifecycle
from indaim.ledger import accounts as _accounts
from indaim.ledger import cache_lifecycle as _cache_lifecycle
from indaim.ledger import cost as _cost
from indaim.ledger import cost_ledger as _cost_ledger
from indaim.ledger import estimate as _estimate
from indaim.ledger import interpretation_billing as _interpretation_billing
from indaim.ledger import prompt_census as _prompt_census
from indaim.ledger import stats as _stats
from indaim.narrative import irregular_npc as _irregular_npc
from indaim.narrative import master as _master_mod
from indaim.rules.checks import judgment as _judgment
from indaim.rules.growth import growth as _growth
from indaim.rules.items import items as _items
from indaim.rules.space import places as _places
from indaim.rules.time import timeline as _timeline
from indaim.rules.vitals import status_rules as _status_rules
from indaim.scenario import quest as _quest
from indaim.turn import action_intent as _action_intent
from indaim.turn import commit_coordinator as _commit_coordinator
from indaim.turn import extraction as _extraction
from indaim.turn import player_world as _player_world
from indaim.turn import turn_history as _turn_history
from indaim.turn import turn_preparation as _turn_preparation
from indaim.turn import turn_transaction as _turn_transaction
from indaim.turn import turn_world as _turn_world


class TurnService:
    async def _ask_next_player(self, session):
        """
        gm_pending_players에서 다음 PC를 꺼내 행동 요청 메시지를 게임 채널에 전송.
        목록이 비어 있으면 수집 완료로 처리.
        """
        game_ch = self.bot.get_channel(session.game_ch_id)

        if not session.gm_pending_players:
            # 모든 PC의 행동이 수집됨 → 지시층위 호출
            await self._finalize_round_and_process(session)
            return

        next_char = session.gm_pending_players.pop(0)
        session.gm_waiting_for = next_char

        uid = _utils.get_uid_by_char_name(session, next_char)
        mention = f"<@{uid}>" if uid else f"**{next_char}**"

        collected = session.gm_collected_actions
        if collected:
            # 이미 다른 PC의 행동이 수집된 상태 → 간단히 참고 표시
            others = "、".join([
                f"{k}: '{v}'"
                for k, v in collected.items()
            ])
            prompt = f"{mention}, 현재까지 행동 선언: {others}\n{mention}은(는) 어떻게 하시겠습니까?"
        else:
            # v7.13 마스터 대사 — 차례 안내는 캐릭터 이름으로 부른다(사용자 결정 Q3). 대사가 없으면 기존(멘션) 문구.
            prompt = _master_mod.say("turn.next_player", f"{mention}님 턴입니다. 어떤 행동을 하시겠습니까?",
                                     slots={"player_name": next_char})

        if game_ch:
            await self.rt.stream_text_to_channel(
                self.bot, game_ch, f"> {prompt}",
                words_per_tick=24, tick_interval=0.8
            )
        # 선제 질문도 current_turn_logs에 기록
        session.current_turn_logs.append(f"[진행자 (GM)]: {prompt}")
        await self.rt.save_session_data(self.bot, session)

    async def _handle_waiting_response(self, session, message, char_name: str):
        """
        GM의 선제 행동 질문에 대한 플레이어 응답 수집. 모든 PC 수집 완료 시 지시층위 호출.
        """
        async with self._lock_for(session):
            # 이미 다른 처리가 완료된 경우 스킵
            if session.gm_waiting_for != char_name:
                return
            if not session.gm_active:
                return
            if await self._roll_pending_blocks(session):
                return
            content = message.content.strip()
            if await self._hold_if_closed(session, char_name, content):
                return                                     # 질문 대기 상태 유지 — 다시 열면 보관 선언으로 진행

            session.gm_waiting_for = None

            # 행동 수집
            session.gm_collected_actions[char_name] = content
            session.current_turn_logs.append(f"[{char_name}]: {content}")

            if session.gm_pending_players:
                # 아직 응답 안 한 PC가 있음 → 다음 PC에게 질문
                await self.rt.save_session_data(self.bot, session)
                await self._ask_next_player(session)
            else:
                # 모든 PC 수집 완료
                await self._finalize_round_and_process(session)

    async def _finalize_round_and_process(self, session):
        """
        모든 PC의 행동이 수집된 후 종합하여 지시층위를 호출.
        단일 PC면 그대로, 멀티 PC면 종합 메시지 생성 + 게임 채널에 요약 표시.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        actions = session.gm_collected_actions.copy()
        session.gm_collected_actions = {}
        session.gm_clarify_count = 0

        if not actions:
            return

        if len(actions) == 1:
            player_message = list(actions.values())[0]
        else:
            # 멀티플레이어 — 게임 채널에 행동 종합 표시
            summary_lines = "\n".join([f"> **{k}**: {v}" for k, v in actions.items()])
            if game_ch:
                await self.rt.send_streamed(self.bot, game_ch, f"> 📋 **행동 선언 종합:**\n{summary_lines}")
                self.rt.write_log(session.session_id, "game_chat",
                               f"[행동 종합]: {'; '.join([f'{k}: {v}' for k, v in actions.items()])}")
            player_message = "\n".join([f"[{k}]: {v}" for k, v in actions.items()])
            # 대표 PC를 첫 번째 PC로 업데이트
            first_char = list(actions.keys())[0]
            session.gm_target_char = first_char

        await self._process_actions(session, player_message, master_ch)

    async def _roll_pending_blocks(self, session) -> bool:
        """v7.33(F4 — 사용자 결정 '가'): 판정이 끝나지 않은 동안 새 선언을 받지 않는다(같은 시도로 이중 진행·재굴림 방지).

        판정 대기 = 주사위를 아직 굴리지 않음 또는 굴린 결과를 보관 중(지시층위 실패 뒤 [이어서 진행] 대기)."""
        tx = _turn_transaction.get_active_transaction(session)
        if tx is None or tx.status != _turn_transaction.TurnStatus.WAITING_FOR_ROLL \
                or not (getattr(tx, "roll_open", False) or getattr(tx, "pending_roll", None)):
            return False
        text = ("🎲 보관된 판정 결과가 있습니다. 위의 **[판정 결과로 이어서 진행]**을 눌러 주십시오."
                if getattr(tx, "pending_roll", None) else
                "🎲 진행 중인 판정이 있습니다. 먼저 위의 **[🎲 주사위 굴리기]**를 눌러 주십시오.")
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.message_lifecycle.send_transient(self.bot, session, game_ch, text,
                                                        key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
        return True

    async def _abort_turn_before_narration(self, session, transaction_id, player_message, master_ch, *,
                                           code, stage: str, exc: BaseException | None = None,
                                           master_text: str | None = None) -> bool:
        """v7.33(F1·F3): 묘사 이전 단계 실패 — 시도를 FAILED_SYSTEM(청구 0)으로 종결하고 플레이어에게 재선언을 안내한다.

        이미 종결·대체된 시도이거나 준비가 묘사 이후 단계(커밋 경로 소유)면 아무것도 하지 않는다(False)."""
        TT, TP = _turn_transaction, _turn_preparation
        if exc is not None:
            _logs.error("turn", f"예기치 않은 예외 — {stage}", exc=exc, session=session)
        tx = TT.get_active_transaction(session)
        if transaction_id is None or tx is None or tx.transaction_id != transaction_id or TT.is_terminal(tx.status):
            return False
        prep = TP.get_preparation(session, transaction_id)
        if prep is not None and prep.phase != TP.PREP_COLLECTING:
            return False
        prep = TP.ensure_preparation(session, transaction_id)
        try:
            await prep.join()
            prep.seal()
            prep.close_cost_membership()
        except Exception as e:  # noqa: BLE001
            prep.violations.append(f"cost_close:{e}")
        prep.phase = TP.PREP_FAILED
        prep.failure_stage = stage
        prep.failure_code = getattr(code, "value", code)
        TT.finalize(session, transaction_id, TT.TurnStatus.FAILED_SYSTEM, failure_stage=stage, failure_code=code,
                    failure_message=(f"{type(exc).__name__}: {exc}"[:300] if exc is not None else None))
        await self._release_turn_processing(session, prep)
        session.current_turn_logs = []
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            try:
                await self.rt.message_lifecycle.send_transient(
                    self.bot, session, game_ch, _resilience.build_failed_turn_notice(player_message),
                    key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
            except Exception as e:  # noqa: BLE001
                print(f"[턴] 실패 안내 송출 실패: {e}")
        if master_ch:
            try:
                await master_ch.send(master_text or (
                    f"⚠️ 턴 처리 중 예기치 않은 오류({stage}: {type(exc).__name__ if exc else code}) — "
                    f"이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다."))
            except Exception:  # noqa: BLE001
                pass
        await self._settle_failed_turn(session, prep, master_ch)
        await self.rt.save_session_data(self.bot, session)
        return True

    async def _hold_if_closed(self, session, char_name: str, content: str) -> bool:
        """세션이 닫혀 있으면 선언을 보관하고 안내한다(턴 맥락에 기록하지 않음). 다시 열면 이어서 진행한다.

        보관은 런타임 한정(재시작 시 소실) — 새 선언이 오면 새 것으로 교체된다.
        """
        if self.rt.is_session_open(session):
            return False
        session._held_declaration = {"char": char_name, "content": content}
        master_ch = self.bot.get_channel(session.master_ch_id)
        if master_ch:
            await master_ch.send("⚠️ 세션이 닫혀 있어 선언을 보관했습니다. 세션을 열면 이어서 진행합니다.")
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.message_lifecycle.send_transient(
                self.bot, session, game_ch,
                "⏸️ " + _master_mod.say("cache.closed_held", "세션이 닫혀 있습니다. 선언은 보관해 두었습니다.") + "\n"
                "> 디스플레이 채널의 **세션 열기**를 누르시면 그 선언으로 이어서 진행합니다.",
                key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
        return True

    async def resume_held_declaration(self, session) -> bool:
        """세션 다시 열기 직후 — 닫힌 동안 보관한 선언으로 턴을 진행한다(재입력 불필요)."""
        held = getattr(session, "_held_declaration", None)
        session._held_declaration = None
        if not held or not getattr(session, "gm_active", False) or not self.rt.is_session_open(session):
            return False
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.message_lifecycle.send_transient(
                self.bot, session, game_ch,
                "▶️ " + _master_mod.say("cache.reopen_held", "보관된 선언으로 이어서 진행합니다.")
                + f"\n> {held['content']}",
                key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
        await self.submit(session, held["content"], speaker=held["char"], reply_channel=game_ch,
                          waiting_for=held["char"] if getattr(session, "gm_waiting_for", None) == held["char"] else None)
        return True

    async def submit(self, session, text: str, *, speaker: str, reply_channel=None, waiting_for: str | None = None):
        """턴 입력 하나 — 플레이어 선언(어댑터가 채팅에서 뽑은 원문·화자·답할 채널). 리팩터링 T-5: 턴 서비스의 진입점.

        waiting_for = 진행자가 그 PC의 답을 기다리는 중이면 그 PC 이름(라운드 응답), 아니면 자발 발언."""
        decl = SimpleNamespace(content=text, channel=reply_channel, author=SimpleNamespace(display_name=speaker))
        if waiting_for:
            return await self._handle_waiting_response(session, decl, waiting_for)
        return await self._handle_player_message(session, decl)

    async def _handle_player_message(self, session, message):
        """
        기존 자발적 플레이어 발언 처리 경로 (gm_waiting_for 없을 때).
        락 획득 후 _process_actions 호출.
        """
        # 추출층위 미완료 시 다음 턴 차단 (설계문서 1 §5)
        if getattr(session, "extraction_pending", False):
            try:
                await message.channel.send(
                    "⏸️ 이전 턴 정보 정리가 완료되지 않아 진행할 수 없습니다. "
                    "위의 재시도 버튼을 눌러 주십시오.",
                    delete_after=8,
                )
            except Exception:
                pass
            return

        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            # WP-D(D-D4): 라우팅 이후 경합으로 복구 대기가 생겼어도 복구가 eligibility보다 먼저.
            if getattr(session, "commit_recovery", None):
                await self._recover_then_maybe_round(session, master_ch)
                return
            if not session.gm_active:
                return

            if session.gm_turn_cap is not None and session.gm_turns_done >= session.gm_turn_cap:
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.gm_turn_cap}턴) 도달."
                )
                await self.rt.save_session_data(self.bot, session)
                return

            if self.rt.auto_cost_cap_reached(self.bot, session):
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await self.rt.save_session_data(self.bot, session)
                return

            if await self._roll_pending_blocks(session):
                return
            char_name = session.gm_target_char or message.author.display_name
            if await self._hold_if_closed(session, char_name, message.content.strip()):
                return
            session.gm_clarify_count = 0
            session.current_turn_logs.append(f"[{char_name}]: {message.content.strip()}")

            await self._process_actions(session, message.content.strip(), master_ch)

    async def _process_actions(self, session, player_message: str, master_ch):
        """
        안전장치 확인 후 지시층위 루프(_run_gm_logic_loop) 호출.
        _handle_player_message와 _finalize_round_and_process의 공통 진입 경로.
        이미 락 안에서 호출된다고 가정하므로 이 함수 내부에는 락 없음.
        """
        # WP-D(D-D4): 복구 우선 — eligibility 검사보다 먼저.
        if getattr(session, "commit_recovery", None):
            await self._recover_then_maybe_round(session, master_ch)
            return

        if not session.gm_active:
            return

        if session.gm_turn_cap is not None and session.gm_turns_done >= session.gm_turn_cap:
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 턴 한도({session.gm_turn_cap}턴) 도달."
                )
            await self.rt.save_session_data(self.bot, session)
            return

        if self.rt.auto_cost_cap_reached(self.bot, session):
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
            await self.rt.save_session_data(self.bot, session)
            return

        # WP-C: 현재 시도가 확정 묘사 이후 준비/READY/재시도 대기 중이면 새 선언을
        #   받지 않는다 — 다음 자동 논리 턴은 READY 배리어와 durable 커밋 이후에만.
        _active = _turn_transaction.get_active_transaction(session)
        _aprep = getattr(_active, "preparation", None) if _active is not None else None
        if _aprep is not None and getattr(_aprep, "phase", None) in (
                _turn_preparation.PREP_PREPARING,
                _turn_preparation.PREP_READY,
                _turn_preparation.PREP_COMMITTING,
                _turn_preparation.PREP_RETRY_PENDING):
            _logs.event("turn", "새 선언 차단 — 준비 중 시도", level="WARN", session=session,
                            data={"code": "PREP_IN_FLIGHT", "tx": _aprep.transaction_id, "phase": _aprep.phase})
            if master_ch:
                await master_ch.send("⏸️ 이전 턴 준비가 끝나지 않아 새 선언을 처리하지 않습니다.")
            return

        # H06: 동적 세계 세션이 열리지 않았거나(INITIALIZED) 바인딩 정의를 읽을 수 없으면(BLOCKED) 턴을
        #   받지 않는다 — 조용히 LEGACY 경로로 진행하지 않는다(운영자 확인).
        _blocked = _world_lifecycle.blocked_reason(session)
        if _blocked:
            _atx = _turn_transaction.get_active_transaction(session)
            if _atx is not None and getattr(_atx, "world_turn", None) is not None and \
                    _action_intent.is_definition_stale(_atx.world_turn.ctx.definition_revision, session):
                _atx.world_turn = None                     # 옛 정의 WorldTurn 재사용 금지(R2)
            _logs.event("turn", "턴 차단 — 동적 세계", level="WARN", session=session,
                            data={"code": "WORLD_BLOCKED", "reason": _blocked})
            if master_ch:
                await master_ch.send(f"⏸️ [동적 세계] {_blocked}")
            return

        # WP-01: 자동 턴 수렴 경계. 안전장치(활성/턴 한도/비용 한도)를 통과한 직후,
        # 하나의 플레이어 선언을 하나의 논리 턴 시도에 대응시킨다. get_or_begin은
        # ASK/NARRATE 재입력 시 같은 트랜잭션을 재사용한다(새 시도를 만들지 않는다).
        if await self._roll_pending_blocks(session):
            return
        tx = _turn_transaction.get_or_begin_turn_transaction(session, player_message)
        print(f"{tx.log_prefix('PROCESS_ACTIONS')}")
        try:
            await self._run_gm_logic_loop(
                session, player_message, master_ch,
                transaction_id=tx.transaction_id)
        except Exception as e:  # noqa: BLE001 — v7.33(F1): 입력 태스크 밖으로 새지 않게 — 묘사 이전이면 청구 0 종결
            await self._abort_turn_before_narration(
                session, tx.transaction_id, player_message, master_ch,
                code=_turn_transaction.FailureCode.UNEXPECTED_EXCEPTION, stage="LOGIC_LOOP", exc=e)

    async def _finish_proceed_and_continue(self, session, instruction, master_ch,
                                           *, event_assessment=None,
                                           transaction_id: str | None = None):
        """
        강제/정상 PROCEED 공통 owner — WP-C 트랜잭션 배리어 소유자.

          1) 턴 한도 도달 시 자동 모드 정지 + 마스터 채널 알림
          2) _dispatch_proceed: 묘사 생성 → 확정 묘사 직후 동시 준비(비정규 NPC·추출·
             자동 재계획) 발사 → 전달/스트리밍(준비 작업과 겹침)
          3) 모든 등록 준비 작업 합류 → 레지스트리 봉인 → 비용 멤버십 동결
             → 단일 READY_TO_COMMIT 전이(증명 캡처, 턴 소유 정본은 아직 미적용)
          4) WP-D: READY 이후 권위적 커밋 — _commit_coordinator.CommitCoordinator
             (PREPARED → 부수효과 없는 적용 → strict 저장 → SESSION_PERSISTED →
              Settlement → InkTransaction → BILLING_APPLIED → durable COMMITTED)
          5) 처리 잠금 해제 → 파생 알림·비용 보고·통계 → 디스플레이 → 다음 라운드
             (다음 라운드는 durable COMMITTED 또는 안전한 실패 처분 이후에만 열린다)
        사전 READY 실패(생성/전달/추출/검증)는 정본 전진 없이 FAILED_SYSTEM Settlement(청구 0).

        Args:
            event_assessment: PROCEED 계열에서 지시층위가 평가한 사건 상태.
                None이면 서사 진행도/재계획 판정을 건너뛴다(강제 PROCEED 폴백 경로).
        """
        if session.gm_turn_cap is not None and (session.gm_turns_done + 1) >= session.gm_turn_cap:
            session.gm_active = False
            if master_ch:
                await master_ch.send(
                    f"🛑 **[GM 마지막 턴]** 자동 턴 한도({session.gm_turn_cap}턴) 도달. "
                    f"이번 턴을 마지막으로 자동 진행을 정지합니다."
                )

        # 되감기 델타 기준점.
        # NOTE: 턴마다 새로 스냅샷을 뜨면, 델타 계산과 다음 스냅샷 사이에 일어난
        #       변화가 어느 델타에도 잡히지 않고 영구 유실된다.
        #       따라서 '마지막으로 기록한 시점의 스냅샷'을 세션에 들고 다니며
        #       그것과 비교한다. (WP-C: 이번 턴 추출/재계획은 READY 이후 델타 기록 '전'에
        #       적용되므로 이번 턴 델타에 귀속된다. rewind 구현 자체는 변경하지 않는다.)
        state_before = getattr(session, "_rewind_snapshot", None)
        if state_before is None:
            state_before = self.rt.capture_state(session)

        # 대기 중인 BGM 재생 — 기획 규정상 온 직후가 아니라 다음 스트리밍 시작 시점이다.
        #   (이전 턴에 확정된 pending_bgm의 소비 — 이번 턴 소유 정본 변경이 아니다.)
        pending = getattr(session, "pending_bgm", None)
        _bgm_consumed = False
        if pending and self.rt.is_enabled(session, "bgm"):
            try:
                media_cog = self.bot.get_cog("MediaCog")
                if media_cog and await media_cog.start_bgm(session, pending):
                    session.pending_bgm = None
                    _bgm_consumed = True
            except Exception as e:
                print(f"[BGM] 재생 실패(진행에는 영향 없음): {e}")

        # WP-C: 트랜잭션 소유 준비 객체 — 새 트랜잭션을 만들지 않는다(PF-18).
        prep = _turn_preparation.ensure_preparation(session, transaction_id)
        if prep is None:
            print(f"[WP-C/{session.session_id}] 현재 자동 트랜잭션이 아님 — 배리어 owner 불가(tx={transaction_id})")
            if master_ch:
                await master_ch.send("⚠️ 현재 자동 턴 시도가 아니어서 이번 진행을 중단합니다.")
            return
        if _bgm_consumed:
            # B-C6: 이전 턴에 확정된 pending_bgm을 이번 스트리밍 시작에 소비한 정당한 운영
            #   쓰기(묘사·준비 이전). 이후 이 턴의 추출이 새 pending_bgm을 스테이징하므로
            #   기준선을 소비 직후로 재설정하고 사유를 남긴다.
            prep.rebaseline(session, "pending_bgm",
                            "prior-turn committed pending_bgm consumed at stream start "
                            "(before finalized narration)")
        prep.phase = _turn_preparation.PREP_PREPARING
        prep.event_assessment = event_assessment
        _wt = self._attach_world_turn(session, prep, transaction_id)

        outcome = None
        result = None
        open_round = False
        try:
            try:
                if _wt is not None and _wt.plan is not None \
                        and _wt.plan.status == _player_world.PLAN_CONFLICT:
                    # DESIGN 11 §30: 묘사 전 결정적 충돌 — provider 묘사 호출 없이 종결(재시도 없음).
                    outcome = await self._world_conflict_outcome(session, prep, stage="pre_narration")
                else:
                    await self._dispatch_proceed(
                        session, instruction, transaction_id=transaction_id, preparation=prep)
                    outcome = await self._join_and_ready(session, prep)
            except Exception as e:
                # 사전 READY 예기치 못한 예외 — 준비 작업을 합류시키고 실패로 처리한다.
                _logs.event("turn", "준비 중 예외", level="ERROR", session=session, exc=e,
                                data={"code": "PREP_EXCEPTION", "tx": prep.transaction_id})
                prep.violations.append(f"exception:{type(e).__name__}")
                outcome = await self._join_after_exception(session, prep)
            if outcome == _OUTCOME_READY:
                result = await self._commit_ready_turn(
                    session, prep, state_before=state_before)
            else:
                open_round = await self._handle_preparation_failure(
                    session, prep, outcome, master_ch)
        finally:
            await self._release_turn_processing(session, prep)

        if outcome == _OUTCOME_READY:
            if not await self._after_commit(session, prep, result, master_ch):
                return
            open_round = True
        # WP-E: 재생성 교체 시도가 커밋되지 못했으면 이전 선택 시도의 정본을 되돌린다.
        await self._settle_history_op(session, prep, master_ch)
        # 다음 라운드: durable COMMITTED 또는 안전하게 종결된 실패만. 복구 대기(차단)면 금지.
        if open_round and session.gm_active and not getattr(session, "commit_recovery", None):
            await self._start_round(session)

    async def _hold_declaration(self, session, transaction_id, player_message, gate, text, master_ch) -> None:
        """v7.14: 게이트 BLOCK·CLARIFY·검증 불가 — 굴림·세계 연산 없이 중립 안내 후 플레이어 재선언 대기."""
        _turn_history.note_judgment(
            session, transaction_id,
            judgment={"action": "ASK", "bridge_message": text, "player_intent": "",
                      "reasoning": "선언 게이트 " + (f"{gate['resolution']}/{gate['reason_code']}" if gate else "검증 불가")},
            player_message=player_message, roll_results=[], gate=gate)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.stream_text_to_channel(self.bot, game_ch, text, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [text], transaction_id=transaction_id)
        session.current_turn_logs.append(f"[진행자 (GM)]: {text}")
        _turn_transaction.mark_waiting_for_player(session, transaction_id)
        if master_ch:
            await master_ch.send("🚧 **[선언 게이트]** " + (
                f"{gate['classification']} → {gate['resolution']} ({gate['reason_code']})" if gate
                else "검증 불가(시뮬레이션 게이트 없음) — 발견 제안 미처리"))
        await self.rt.save_session_data(self.bot, session)

    async def _ask_route_choice(self, session, plan, transaction_id, master_ch) -> None:
        """의미 있는 경로 차이 — 플레이어 지식 범위의 선택지를 묻는다(실제 숨은 상태 미포함)."""
        lines = [_master_mod.say("turn.route_question", "어느 길로 가시겠습니까?")]
        for n, o in enumerate(plan.route_options, 1):
            mins = f" (약 {o['minutes']}분)" if o.get("minutes") is not None else ""
            lines.append(f"{n}) {o['label']}{mins}")
        bridge = "\n".join(lines)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.stream_text_to_channel(self.bot, game_ch, bridge, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [bridge], transaction_id=transaction_id)
        session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
        # v7.23: 질문을 낸 뒤에만 번호 응답 스냅샷을 건다(같은 시도·장소·world_version에서만 유효)
        wt = self._world_turn(session, transaction_id)
        if wt is not None:
            wt.pending_route = dict(_turn_world.move_question_anchor(wt.ctx, session),
                                    signatures=[list(o["signature"]) for o in plan.route_options])
        if master_ch:
            await master_ch.send(f"🧭 **[경로 선택]** 후보 {len(plan.route_options)}개 — 플레이어 선택 대기")
        _turn_transaction.mark_waiting_for_player(session, transaction_id)

    async def _dynamic_declaration_check(self, session, player_message, roll_results, judgment, gate_in,
                                         master_ch, *, transaction_id, cache_ok):
        """v7.23 DYNAMIC 조건부 선언 검증 — (judgment, gate).

        판단이 주변 발견 제안(ambient_proposal·discovery 초안)이나 결과 선취(assertion_claim)를 표시했을 때만 검증한다.
        · 이 장소에서 이미 발견한 같은 대상이면 코드가 재사용(검증 호출 없음).
        · 그 밖은 캐시 룰북으로 축소 검증 1회 → 기존 _parse_gate(원문 대상명·D키·열거값 검증).
        · 검증 결과와 판단 초안(발견 굴림)이 어긋나면 검증 결과를 보여 주고 판단을 한 번 다시 부른다(기존 게이트 계약).
        · 검증할 수 없으면 None — 호출자가 새 발견은 보류(fail-closed), 결과 선취만이면 시도로 진행한다."""
        rolls = [r for r in (judgment.get("rolls") or []) if isinstance(r, dict)]
        ambient = judgment.get("ambient_proposal") is True or any(r.get("discovery") is True for r in rolls)
        assertion = judgment.get("assertion_claim") is True
        pk = dict(dynamic=True, place=gate_in["place"], place_name=gate_in["place_name"], rows=gate_in["rows"])
        ref = judgment.get("discovery_ref")
        if not (ambient or assertion):
            # v7.14 A1 근거 — 선언이 이 장소의 기존 발견(D키)을 가리킨다(코드 확정 게이트, 검증 호출 없음)
            if isinstance(ref, str) and any(r[0] == ref for r in gate_in["rows"]):
                return judgment, _parse_gate({"classification": "ORDINARY", "resolution": "CONTINUE",
                                              "reason_code": "NONE", "existing_key": ref}, player_message, **pk)
            return judgment, None
        gate, source = None, None
        target = judgment.get("discovery_target")
        if ambient and not assertion:
            row = (next((r for r in gate_in["rows"] if r[0] == ref), None)
                   or next((r for r in gate_in["rows"] if isinstance(target, str)
                            and _turn_world.same_target(r[2], target)), None))
            if row is not None:
                gate = _parse_gate({"classification": "AMBIENT_PROPOSAL", "resolution": "ACCEPT_NO_ROLL",
                                    "reason_code": "NONE", "discovery_target": row[2], "discovery_kind": row[3],
                                    "existing_key": row[0]}, player_message, **pk)
                if gate is not None and gate.get("existing_info_id"):
                    source = "CODE"
                else:
                    gate = None
            elif any(_turn_world.same_target(n, target) for n in gate_in.get("finds") or ()):
                # v7.45.4: 이 장면의 저작 단서 실물을 지목했다 — 새 주변 발견이 아니라 일반 조사(단서는 X키로 드러난다)
                gate = _parse_gate({"classification": "ORDINARY", "resolution": "CONTINUE", "reason_code": "NONE"},
                                   player_message, **pk)
                source = "FIND"
        if gate is None and cache_ok:
            raw = await self._call_declaration_gate(session, player_message, judgment, gate_in, master_ch,
                                                    transaction_id=transaction_id)
            gate = _parse_gate((raw or {}).get("declaration_gate"), player_message, **pk)
            source = "LLM" if raw is not None else None
        _logs.event("turn", "선언 검증", session=session, data={
            "stage": "declaration_check", "tx": transaction_id, "ambient": ambient, "assertion": assertion,
            "source": source, "result": None if gate is None else
            f"{gate['classification']}/{gate['resolution']}/{gate['reason_code']}"})
        if master_ch and (gate is None or _gate_blocks(gate)):     # v7.35: 진행하는 판정은 층위활동 보고(🧭)로
            await master_ch.send("🛡️ **[선언 검증]** " + (
                f"{gate['classification']} → {gate['resolution']} ({gate['reason_code']})"
                + (" · 기존 발견 재사용" if source == "CODE" else "") if gate else "검증 불가(캐시 없음·응답 오류)"))
        if gate is None or _gate_blocks(gate):
            return judgment, gate
        need = _gate_rolls_discovery(gate)
        has = str(judgment.get("action", "")).upper() == "ROLL" and any(r.get("discovery") is True for r in rolls)
        if need != has and (need or gate.get("resolution") == "ACCEPT_NO_ROLL"):
            # 초안과 검증 결과가 어긋난다 — 검증 결과를 준 판단 한 번(굴림 0·발견 0인 상태에서). 실패면 진행하지 않는다.
            judgment = await self._call_judgment(session, player_message, roll_results, master_ch,
                                                 transaction_id=transaction_id, gate=gate)
        return judgment, gate

    async def _call_declaration_gate(self, session, player_message, judgment, gate_in, master_ch,
                                     *, transaction_id) -> dict | None:
        """v7.23 축소 선언 검증(캐시 룰북) — 기존 시뮬레이터의 선언 게이트 규칙·스키마만 재사용한다.\n\n        서사 방향·공리·개연성 등급은 묻지도 받지도 않는다. 응답은 열거값·원문 대상명뿐(정본 사실 아님)."""
        cache_name = getattr(session, "cache_name", None)
        rules = NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION[
            NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION.index("[선언 게이트 규칙 — declaration_gate]"):]
        instruction = ("당신은 선언 검증기다. 시나리오 캐시의 룰북·세계관만 근거로, 아래 규칙에 따라 이번 플레이어 선언의 "
                       "declaration_gate 하나만 판정한다. 서사 방향·사건 전개를 제안하지 않는다.\n\n" + rules)
        recent = []
        for content in session.raw_logs[-2:]:
            try:
                recent.append(f"[{content.role.upper()}]\n{content.parts[0].text}")
            except Exception:
                continue
        gi = gate_in
        rows, failed = gi.get("rows") or [], gi.get("failed") or []
        user_prompt = (
            f"[현재 장소]\n{gi.get('place_name') or '(장소 밖)'}\n\n"
            f"[최근 게임 로그]\n{chr(10).join(recent) or '(없음)'}\n\n"
            f"[이번 턴 대화(커밋 전)]\n{chr(10).join(session.current_turn_logs[-8:]) or '(없음)'}\n\n"
            f"[판단층위가 읽은 시도]\n{judgment.get('player_intent') or ''}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n\n"
            "[이 장소에서 PC가 이미 발견한 것]\n"
            + (_turn_world.discovery_rows_text(rows) if rows else "(없음)")
            + ("\n[이 장소에 실재가 코드로 확정된 조사 대상 — 선언이 이것을 찾거나 가리키면 ORDINARY·CONTINUE"
               "(새 발견 아님)]\n" + ", ".join(gi["finds"]) if gi.get("finds") else "")
            + "\n[이 장소에서 찾지 못한 이전 탐색 — 실패는 부재가 아니다]\n"
            + ("\n".join(f"· {f['target']} — 당시 선언: {f['declaration']}" for f in failed) if failed else "(없음)")
            + "\n\n[지시사항]\n이번 플레이어 선언을 [선언 게이트 규칙]에 따라 declaration_gate로 판정하십시오.")
        schema = {"type": "object",
                  "properties": {"declaration_gate": NARRATIVE_DIRECTION_SCHEMA["properties"]["declaration_gate"]},
                  "required": ["declaration_gate"]}
        try:
            contents = [
                types.Content(role="user", parts=[types.Part.from_text(text=instruction)]),
                types.Content(role="model", parts=[types.Part.from_text(text="이해했습니다. 선언만 판정하겠습니다.")]),
                types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)]),
            ]
            config = types.GenerateContentConfig(
                http_options=_resilience.provider_http_options("instruction"),
                cached_content=cache_name, temperature=0.2, response_mime_type="application/json",
                response_schema=schema, safety_settings=_constants.TRPG_SAFETY_SETTINGS)
            _cl_op = _cost_ledger.begin_operation(
                self.bot, _cost_ledger.OP_TURN_DECLARATION_CHECK, session=session,
                model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
                billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
            _turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(self.bot.genai_client.models.generate_content,
                                          model=_cache.DEFAULT_MODEL, contents=contents, config=config),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id)
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            _logs.event("turn", "선언 검증 호출 실패", level="WARN", session=session, exc=e,
                            data={"code": "DECLARATION_CHECK_CALL"})
            return None
        try:
            in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(response.usage_metadata)
            breakdown = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens, cached_read_tokens=cached_tokens)
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(input_tokens=in_tokens, cached_input_tokens=cached_tokens, output_tokens=out_tokens,
                          thought_tokens=thought_tokens, cost_usd=breakdown["total_usd"], cost_krw=cost,
                          usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}선언 검증",
                                in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            session.turn_cost_log = list(getattr(session, "turn_cost_log", None) or []) + [{
                "label": "선언 검증", "cost": cost, "in": in_tokens, "cached": cached_tokens, "out": out_tokens}]
        except Exception as e:
            print(f"[GM] 선언 검증 비용 정산 실패: {e}")
        raw_text = response.text or ""
        try:
            return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE))
        except Exception as e:
            _logs.event("turn", "선언 검증 JSON 파싱 실패", level="WARN", session=session, exc=e,
                            data={"code": "DECLARATION_CHECK_JSON"})
            return None

    async def _ask_destination(self, session, wt, transaction_id, master_ch) -> None:
        """목적지 불명 이동 — PC가 아는 출구를 제시하며 묻는다(묘사·추출 호출 없음).

        v7.22: 모호한 출구 지목이면 그 후보만 묻는다. 질문이 나간 뒤에만 질문 문맥을 건다(전송 실패 = 문맥 없음) —
        다음 답이 다시 길을 정하지 않으면 되묻지 않고 그 후보 안에서 한 구간 간다(_settle_move_intent)."""
        prev = self._pending_move(session, wt)
        bridge, asked = _turn_world.destination_question(
            wt.ctx, session, (wt.intent.exit_choices if wt.intent is not None else ()) or (prev or {}).get("exits"))
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            await self.rt.stream_text_to_channel(self.bot, game_ch, bridge, words_per_tick=15, tick_interval=1.5)
            await self._voice_lines(session, [bridge], transaction_id=transaction_id)
        wt.pending_move = dict(_turn_world.move_question_anchor(wt.ctx, session),
                               exits=tuple(asked)) if asked else None
        session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
        if master_ch:
            await master_ch.send("🧭 **[목적지 확인]** 이동 목적지가 정본 후보에 없어 되묻습니다")
        _turn_transaction.mark_waiting_for_player(session, transaction_id)

    async def _launch_concurrent_preparation(self, session, prep, narr, master_ch) -> None:
        """확정 묘사가 고정된 직후 — 동시 준비 작업을 등록·발사한다(S1).

        같은 확정 묘사 페이로드가 전달·추출·비정규 NPC 분석에 들어간다(재생성 없음).
          · 비정규 NPC 배정: 스트리밍 전에 전달이 결과(스테이징)만 기다린다. 승격 세부
            생성은 자식 작업으로 등록된다(부모 종결 전 등록).
          · 추출: 비정규 NPC 준비(자식 포함)를 기다린 뒤 투영 입력으로 시작 — 스트리밍과 겹친다.
          · 자동 재계획: 트리거되면 등록 작업으로 스트리밍·추출과 겹친다.
        """
        _turn_transaction.mark_transaction_status(
            session, prep.transaction_id,
            _turn_transaction.TurnStatus.STREAMING_EXTRACTING)
        _mch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        TP = _turn_preparation
        if narr.paragraphs:
            prep.register_task(
                "irregular_npc", TP.TASK_IRREGULAR_NPC,
                coro=self._resolve_irregular_npcs(
                    session, narr.narrative_text, _mch, preparation=prep),
                may_spawn_required_child_work=True)
        prep.extraction_text = narr.text
        prep.register_task(
            "extraction", TP.TASK_EXTRACTION,
            coro=self._prepare_extraction(session, prep, narr.text, _mch),
            required_success=True, may_spawn_required_child_work=True)
        if prep.event_assessment is not None:
            await self._update_narrative_progress(
                session, prep.event_assessment, master_ch, preparation=prep)

    async def _prepare_extraction(self, session, prep, text, master_ch):
        """추출 준비 작업(result-only). 비정규 NPC 준비(승격 자식 포함) 완료 후 시작한다.

        기존 흐름에서 추출은 비정규 등록·승격 이후에 돌았으므로, 같은 입력 의미(등록/승격
        NPC 목록·지시효과 quest 투영)를 투영 뷰로 보존한다. 계획은 준비 객체에만 저장된다.
        """
        TP = _turn_preparation
        irr = prep.task_future("irregular_npc")
        if irr is not None:
            await asyncio.wait([irr])
        while True:
            kids = [r.task for r in prep.tasks.values()
                    if r.kind == TP.TASK_NPC_PROMOTION and r.task is not None and not r.terminal]
            if not kids:
                break
            await asyncio.wait(kids)
        plan = await self._run_extraction(
            session, text, master_ch,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if plan is None:
            raise ExtractionProviderFailure("추출층위 실패(재시도 소진) 또는 stale")
        # H05-B/C: 관찰 → 보충 typed 연산·세계 진행(순수) → 필요 시 후속 묘사 작업 등록(추출 종결 전).
        if getattr(prep, "world_turn", None) is not None:
            # v7.29: 물건 연산이 정본 검증에서 거부되면 ① 사유를 덧붙여 같은 턴 1회 재추출(B1)
            #   ② 또 거부되면 물건 연산만 빼고 확정(B2). 같은 응답을 같은 검증에 다시 넣는 막다른 재시도를 없앤다.
            first = self._observe_or_item_rejection(session, prep, plan, master_ch)
            if first is not None:
                self._world_diagnostic(session, prep, "ITEM_CONTRACT_REJECTED", {"stage": "first", "error": first})
                plan = await self._run_extraction(
                    session, text, master_ch, transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
                    attempt=prep.attempt, preparation=prep, correction=first)
                if plan is None:
                    raise ExtractionProviderFailure("물건 연산 교정 재추출 실패(재시도 소진) 또는 stale")
                second = self._observe_or_item_rejection(session, prep, plan, master_ch)
                if second is not None:
                    await self._drop_item_ops_and_observe(session, prep, plan, master_ch, first, second)
        # B-C3: 이번 턴 계획의 quest_progress로 수치 재계획을 판정 — 자식 작업 등록은
        #   이 추출 작업이 terminal이 되기 전에 끝나므로 배리어 합류가 누락하지 않는다.
        await self._evaluate_numeric_replan(session, prep, plan, master_ch)
        return plan

    async def _drop_item_ops_and_observe(self, session, prep, plan, master_ch, first, second) -> None:
        """v7.29 B2 — 물건 연산만 제외(발견물 수거 키·옛 소지 변화 포함)하고 관찰을 마친다. 부분 적용은 없다."""
        raw = dict(prep.world_observation_raw or {})
        raw.pop(_items.OPS_FIELD, None)
        raw.pop(_items.TRAITS_FIELD, None)
        raw.pop("discovery_taken", None)
        prep.world_observation_raw = raw
        plan.item_deltas = []
        self._world_diagnostic(session, prep, "ITEM_OPS_DROPPED", {"first": first, "second": second})
        if master_ch is not None:
            try:
                await master_ch.send(f"⚠️ [물건] 추출의 물건 연산이 두 번 거부되어 이번 턴은 물건 변화 없이 확정합니다.\n"
                                     f"> 1차: {first}\n> 2차: {second}")
            except Exception:
                pass
        self._process_world_observation(session, prep, plan, master_ch)

    async def _evaluate_numeric_replan(self, session, prep, plan, master_ch) -> str | None:
        """WP-C(B-C3) — 이번 트랜잭션 추출 계획의 수치로 자동 재계획을 트리거한다.

        · 권위: plan.quest_progress(advance/deviation). 정본 last_extraction(직전 턴)은 보지 않는다.
        · precedence/dedupe: event_assessment가 completed/deviated면 이미 사건 트리거가
          등록됐으므로 생략(턴당 1건). event_assessment=None(강제 PROCEED)이면 기존과 같이
          재계획 판정 자체를 하지 않는다. narrative_replan이 이미 등록돼 있어도 생략.
        · 결과 후보는 스테이징되고 READY 이후에만 적용된다.
        """
        ea = prep.event_assessment
        if ea is None or ea in ("completed", "deviated"):
            return None
        if "narrative_replan" in prep.tasks:
            return None
        if not getattr(session, "narrative_plan", None):
            return None
        qp = getattr(plan, "quest_progress", None) or {}
        try:
            advance = int(qp.get("advance", 0))
            deviation = int(qp.get("deviation", 0))
        except (TypeError, ValueError, AttributeError):
            return None
        th = _extraction.get_thresholds(session)
        if deviation >= th["quest_deviated"]:
            reason, full, note = "deviated", True, f"추출 이탈 수치 {deviation}"
            msg = (f"📖 **[서사 계획]** 이탈 수치 {deviation} (임계 {th['quest_deviated']}) "
                   f"→ 중규모 계획 포함 전체 재수립합니다.")
        elif advance >= th["quest_advance"]:
            reason, full, note = "completed", False, f"추출 진행 수치 {advance}"
            msg = (f"📖 **[서사 계획]** 진행 수치 {advance} (임계 {th['quest_advance']}) "
                   f"→ 순간 계획을 재수립합니다.")
        else:
            return None
        prep.narrative_marker = True            # last_planned_turn 마커(스테이징)
        prep.register_task(
            "narrative_replan", _turn_preparation.TASK_NARRATIVE_REPLAN,
            coro=self._prepare_auto_replan(
                session, prep, reason, full_replan=full, context_note=note),
            parent="extraction")
        if master_ch:
            await master_ch.send(msg)
        return reason

    async def _handle_preparation_failure(self, session, prep, outcome, master_ch) -> bool:
        """사전 READY 실패 처리 — 정본 전진·성공 턴 청구 없음(S10).

        Returns: 다음 라운드를 열어도 되는 안전한 종결 처분이면 True(재선언 UX).
        재시도 대기·대체된 시도는 False. 호출자는 commit_recovery(차단)도 함께 본다.
        """
        TP = _turn_preparation
        TT = _turn_transaction
        tid = prep.transaction_id
        if outcome == _OUTCOME_SUPERSEDED:
            # 정본·세션 차단 상태를 건드리지 않는다(현재 시도가 흐름을 소유).
            prep.phase = TP.PREP_FAILED
            prep.failure_stage = "SUPERSEDED"
            print(f"[WP-C/{tid[:8]}] 대체된 시도의 준비 결과 폐기")
            return False
        if outcome == _OUTCOME_EXTRACTION_FAILED:
            # 재시도 가능 상태 — 같은 논리 시도를 유지한다(새 자동 턴을 열지 않음).
            prep.phase = TP.PREP_RETRY_PENDING
            prep.failure_stage = "EXTRACTION"
            # v7.29: 호출 실패와 응답 처리 실패를 나눈다 — 실제 예외를 구조 로그에 남긴다
            _ext = prep.tasks.get("extraction")
            _err = getattr(_ext, "error", None) or ""
            _code = (TT.FailureCode.EXTRACTION_PROVIDER_FAILURE
                     if not _err or _err.startswith(ExtractionProviderFailure.__name__)
                     else TT.FailureCode.EXTRACTION_INVALID_RESULT)
            _logs.event("turn", "추출 작업 실패", level="ERROR", session=session,
                            data={"code": _code.value, "tx": tid, "error": _err})
            TT.mark_transaction_status(
                session, tid, TT.TurnStatus.STREAMING_EXTRACTING,
                failure_stage="EXTRACTION",
                failure_code=_code)
            # WP-D: 재시작 시 이 시도를 exact 멤버십으로 종결할 수 있도록 정확한 클레임
            #   CostEvent ID를 복구 입력으로 strict 영속한다(최종 Settlement 아님).
            if not await _commit_coordinator.persist_retry_breadcrumb(
                    self.bot, session, prep):
                # D-D3 fail-closed: exact 멤버십 내구성을 보장할 수 없으므로 재시도 상태를
                #   열지 않는다. 멤버십을 동결해 이 시도를 FAILED_SYSTEM(청구 0)으로 종결한다.
                try:
                    prep.close_cost_membership()
                except TP.BarrierViolationError as e:
                    prep.violations.append(f"cost_close:{e}")
                prep.phase = TP.PREP_FAILED
                prep.failure_stage = "EXTRACTION_BREADCRUMB"
                TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                            failure_stage="EXTRACTION_BREADCRUMB",
                            failure_code=TT.FailureCode.PERSISTENCE_FAILURE)
                game_ch = self.bot.get_channel(session.game_ch_id)
                if game_ch:
                    await self.rt.message_lifecycle.send_transient(
                        self.bot, session, game_ch, _resilience.build_failed_turn_notice(""),
                        key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
                if master_ch:
                    await master_ch.send(
                        "⚠️ 추출 실패 + 재시도 복구 기록 저장 실패 — 재시도 없이 턴을 취소합니다(청구 0).")
                session.current_turn_logs = []
                session.gm_side_note = ""
                await self._settle_failed_turn(session, prep, master_ch)
                return True
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await game_ch.send(
                    "⚠️ " + _master_mod.say("turn.extraction_retry", "턴 정보 정리 중 문제가 발생했습니다.\n"
                                            "아래 버튼으로 다시 시도해 주십시오.")
                    + " 완료 전까지 다음 턴은 진행되지 않습니다.",
                    view=self.rt.ExtractionRetryView(self.bot),
                )
            if master_ch:
                await master_ch.send(
                    "⚠️ 추출층위 실패 — 턴 미확정(READY 미도달), 다음 턴 차단. 재시도 버튼 배치.")
            return False

        prep.phase = TP.PREP_FAILED
        if outcome == _OUTCOME_DELIVERY_FAILED:
            prep.failure_stage = "DELIVERY"
            await self._cleanup_failed_delivery(session, prep)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                await self.rt.message_lifecycle.send_transient(
                    self.bot, session, game_ch, _resilience.build_failed_turn_notice(""),
                    key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
            if master_ch:
                await master_ch.send("⚠️ 묘사 전달 실패 — 턴을 취소하고 선언 질문으로 되돌립니다.")
            session.current_turn_logs = []
            session.gm_side_note = ""
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="DELIVERY",
                        failure_code=TT.FailureCode.MESSAGE_DELIVERY_FAILURE)
        elif outcome == _OUTCOME_NO_NARRATION:
            prep.failure_stage = "NARRATION"
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="NARRATION",
                        failure_code=TT.FailureCode.NARRATION_PROVIDER_FAILURE)
        else:
            if master_ch:
                await master_ch.send("⚠️ 턴 준비 검증 실패 — 턴을 확정하지 않습니다.")
            # H02: READY 술어가 구체 원인을 남겼으면(예: WORLD_PROJECTION_CONFLICT — 결정적 세계
            #   충돌, 재시도 대상 아님) 그 코드를 트랜잭션에 보존한다. 없으면 기존 분류.
            try:
                code = TT.FailureCode(getattr(prep, "failure_code", None))
            except ValueError:
                code = TT.FailureCode.COMMIT_VALIDATION_FAILURE
            TT.finalize(session, tid, TT.TurnStatus.FAILED_SYSTEM,
                        failure_stage="READY_PREDICATE", failure_code=code)
        # WP-D(§10): 종료 실패 — 동결된 exact 멤버십으로 FAILED_SYSTEM Settlement(청구 0).
        #   영속 실패 시 fail-closed(D-D2): 백로그 + commit_recovery 차단 → 라운드 미개방.
        await self._settle_failed_turn(session, prep, master_ch)
        await self.rt.save_session_data(self.bot, session)
        return True

    async def _cleanup_failed_delivery(self, session, prep) -> None:
        """부분 전달 실패 — 이미 생성된 bot 출력을 멱등 정리한다(ID는 tx에 보존)."""
        err = prep.delivery_error
        ids = list(getattr(err, "canonical_message_ids", ()) or ()) + \
            list(getattr(err, "media_message_ids", ()) or ())
        game_ch = self.bot.get_channel(session.game_ch_id)
        if not ids or game_ch is None:
            return
        msgs = []
        for mid in ids:
            if mid is None:
                continue
            try:
                msgs.append(await game_ch.fetch_message(mid))
            except Exception:
                continue
        await self.rt.clear_messages(msgs)

    async def _emit_commit_derived(self, session, derived) -> None:
        """durable COMMITTED 이후 파생 출력 방출. 실패는 커밋을 되돌리지 않는다(§7 Phase F)."""
        if derived is None:
            return
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        game_ch = self.bot.get_channel(getattr(session, "game_ch_id", 0))
        game_ids = []
        # WP-E(E-E2a): 게임 채널 파생 출력은 durable 송출 의도(인덱스 BEGIN 또는 대체 파일)가
        #   확보된 뒤에만 보낸다. 확보 불가면 보내지 않는다(fail-closed) — 추적 불가 출력 금지.
        #   이미 COMMITTED된 이야기·재무는 되돌리지 않는다.
        emit_id = None
        _tid = getattr(derived, "transaction_id", None)
        n_game = sum(1 for it in derived.items if it[0] == "game")
        game_allowed = False
        if n_game and game_ch:
            if _tid:
                try:
                    emit_id, _ = _turn_history.secure_emit_intent(session, _tid)
                    game_allowed = True
                except Exception as e:
                    print(f"⛔ [WP-E] 송출 의도 영속 불가 — 게임 채널 파생 출력 {n_game}건 보류: {e}")
            else:
                print(f"⛔ [WP-E] 시도 식별 없는 게임 채널 파생 출력 {n_game}건 보류(추적 불가)")
            if not game_allowed and master_ch:
                try:
                    await master_ch.send(
                        f"⚠️ 이력 기록을 확보하지 못해 게임 채널 파생 알림 {n_game}건을 보내지 "
                        f"않았습니다(턴 확정·청구는 유지).")
                except Exception:
                    pass
        for item in list(derived.items):
            try:
                kind = item[0]
                if kind == "master" and master_ch:
                    _, content, embed = item
                    if embed is not None:
                        await master_ch.send(content, embed=embed)
                    elif content:
                        await master_ch.send(content)
                elif kind == "game" and game_ch and game_allowed:
                    _, content, view_factory = item
                    if view_factory is not None:
                        _m = await game_ch.send(content, view=view_factory())
                    else:
                        _m = await game_ch.send(content)
                    if getattr(_m, "id", None) is not None:
                        game_ids.append(_m.id)
                elif kind == "stats":
                    await _stats.bump(item[1], **item[2])
                elif kind == "npcs":
                    await _stats.add_npcs(item[1], item[2])
            except Exception as e:
                print(f"[WP-D] 파생 출력 실패(커밋 유지): {type(e).__name__}: {e}")
        # WP-E(E-E2): 정본 게임 이벤트 메시지를 커밋 시도에 귀속(되감기·재생성 정리 대상).
        #   인덱스 실패 → 대체 파일 → 런타임 차단 순으로 보전하며 조용히 유실하지 않는다.
        if _tid and (game_ids or emit_id):
            where = _turn_history.record_emitted_messages(session, _tid, emit_id, game_ids)
            if where != "INDEX":
                print(f"⛔ [WP-E] 파생 메시지 매핑 보류({where}) — 재정합 전 되감기·재생성 차단")

    async def _send_turn_cost_report(self, session, settlement, prep=None) -> None:
        """Settlement 파생 턴 비용 보고(마스터 전용). 재환산·재반올림 없음(AUD-022).
        v7.30: 매 턴 전수보고 — 비용(주황)·층위활동(파랑)·세계 델타(초록), 한도 초과는 분리(self.rt.master_report)."""
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        log = list(getattr(session, "turn_cost_log", None) or [])
        session.turn_cost_log = []
        if settlement is not None:            # v7.30: 예측·실제 대조 자동 수집(예측 되먹임 원천, 청구와 무관)
            try:
                _by = _estimate.actual_by_line(log)
                _estimate.record_accuracy(session, getattr(session, "last_estimate", None), sum(_by.values()),
                                              _by, transaction_id=getattr(settlement, "transaction_id", None))
            except Exception as e:  # noqa: BLE001
                print(f"[EST] 정확도 수집 실패(무시): {e}")
        if not master_ch or settlement is None:
            return
        try:
            embed = _cost.build_turn_cost_embed(
                session.turn_count, log, session.total_cost,
                total_ink=int(getattr(session, "total_ink_spent", 0) or 0),
                total_usd=float(getattr(session, "total_usd", 0.0) or 0.0),
                free_krw=float(getattr(session, "profile_ai_cost_krw", 0.0) or 0.0),
                settlement=settlement,
                estimate=getattr(session, "last_estimate", None) or None)     # P1: 예상 대비(운영자 점검)
            await self.rt.master_report.send(
                master_ch, session, transaction_id=getattr(settlement, "transaction_id", None), cost_embed=embed,
                cost_log=log, estimate=getattr(session, "last_estimate", None) or None, prep=prep)
        except Exception as e:
            print(f"[WP-D] 턴 비용 보고 실패(커밋 유지): {e}")

    async def _settle_failed_turn(self, session, prep, master_ch) -> None:
        """시스템 실패 시도 → FAILED_SYSTEM Settlement(청구 0). 기록 실패는 명시적으로 알린다."""
        s, err = await _commit_coordinator.settle_failed_attempt(self.bot, session, prep)
        session.turn_cost_log = []
        if not master_ch:
            return
        try:
            if s is not None:
                await master_ch.send(
                    f"🧾 실패 턴 정산 — 플레이어 청구 **0잉크** "
                    f"(발생 비용 {_cost.format_cost(s.provider_cost_krw)}은 운영 기록으로 보존)")
            else:
                await master_ch.send(
                    f"⛔ 실패 턴 정산 기록 실패 — 청구 0, 복구 입력 보존·새 턴 차단. "
                    f"다음 입력 시 재시도합니다: {err}")
        except Exception:
            pass

    async def _after_commit(self, session, prep, result, master_ch) -> bool:
        """커밋 결과 처분. True면 호출자가 다음 라운드를 연다(성공 또는 안전한 실패)."""
        CC = _commit_coordinator
        outcome = getattr(result, "outcome", None)
        if outcome == CC.CommitOutcome.COMMITTED:
            # 비권위 저장 — 정본은 이미 strict 저장됨. 커밋 이후 파생 필드(되감기 공백 표식 등)만.
            await self.rt.save_session_data(self.bot, session)
            await self._emit_commit_derived(session, result.derived)
            # WP-E(E-E2): 재생성 교체의 durable 정리 부채(SELECT와 같은 라인)를 실행한다 —
            #   선택 전환이 COMMITTED 이후에만 일어나므로 이전 출력 삭제도 그 이후뿐이다.
            await self._drain_history_cleanup(session)
            await self._send_turn_cost_report(session, result.settlement, prep)
            try:
                await self.rt.refresh_display(self.bot, session, reason="turn_end")
            except Exception as e:
                print(f"[디스플레이] 턴 종료 갱신 실패: {e}")
            await self._announce_chapters(session, getattr(prep, "chapter_endings", None) or ())
            await self._conclude_if_ending(session, master_ch)
            return True
        if outcome == CC.CommitOutcome.FAILED_PRE_PERSIST:
            # 정본 미영속 — 전달된 묘사는 정본이 아니므로 정리하고 재선언을 안내한다.
            await self._cleanup_uncommitted_output(session, prep)
            game_ch = self.bot.get_channel(session.game_ch_id)
            if game_ch:
                try:
                    await self.rt.message_lifecycle.send_transient(
                        self.bot, session, game_ch, _resilience.build_failed_turn_notice(""),
                        key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
                except Exception:
                    pass
            session.current_turn_logs = []
            session.gm_side_note = ""
            if master_ch:
                try:
                    await master_ch.send(
                        f"⚠️ 턴 확정 실패(영속 전: {result.reason}) — 정본 미반영·청구 0잉크. "
                        f"선언 질문으로 되돌립니다.")
                    if result.audit_error:
                        await master_ch.send(
                            f"⚠️ 실패 턴 정산 기록 실패 — 새 턴 차단 후 재시도합니다: {result.audit_error}")
                except Exception:
                    pass
            session.turn_cost_log = []
            await self.rt.save_session_data(self.bot, session)
            return not getattr(session, "commit_recovery", None)
        if outcome == CC.CommitOutcome.RECOVERY_PENDING:
            if master_ch:
                try:
                    await master_ch.send(
                        f"⏸️ 턴 이야기는 확정·저장되었으나 정산/확정 단계가 끝나지 않았습니다"
                        f"({result.reason}). 복구 완료 전까지 다음 턴을 열지 않습니다.")
                except Exception:
                    pass
            return False
        return False     # REJECTED — 이 호출은 흐름을 소유하지 않는다

    async def _announce_chapters(self, session, endings) -> None:
        """v7.50(E9): 장(章) 결말 — 결말 카드와 통계 기록만 남기고 세션은 이어 간다."""
        game_ch = self.bot.get_channel(session.game_ch_id) if endings else None
        for end in endings:
            if game_ch:
                try:
                    await game_ch.send(embed=self.rt.embed(title=f"📖 {end['title']}",
                                                           description="한 장이 끝났습니다. 이야기는 이어집니다.",
                                                           color=0x2b2d31))
                except Exception as e:
                    print(f"[장 결말] 카드 실패: {e}")
            try:
                for uid in (getattr(session, "players", {}) or {}):
                    await _stats.add_ending(uid, getattr(session, "scenario_id", ""), end["id"])
            except Exception as e:
                print(f"[장 결말] 기록 실패: {e}")

    async def _conclude_if_ending(self, session, master_ch) -> bool:
        """v7.44(E3): 커밋된 정본이 저작 결말 국면에 닿았으면 — 결말 카드 → 기존 종료 경로(정산·환급) → 채널 잠금·기록.

        자동 GM을 끈다(다음 라운드 없음). 종료가 지금 막히면(진행 중 이력 작업 등) 보고만 하고 다음 커밋에서 다시 본다."""
        end = _turn_world.session_ending(session)
        if not end:
            return False
        session.gm_active = False
        res = await _world_lifecycle.close_session(self.bot, session, reason=_cache_lifecycle.REASON_ENDING)
        if not res.get("ok"):
            session.gm_active = True
            if master_ch:
                try:
                    await master_ch.send(f"⏸️ 결말({end['id']})에 닿았으나 세션을 아직 닫지 못했습니다: {res.get('reason')}")
                except Exception:
                    pass
            return False
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            try:
                _desc = "이야기가 끝났습니다. 채널을 잠급니다."
                if end.get("status"):            # v7.48(E2) 상태 결말(사망 등)은 묘사가 맺지 못했을 수 있다 — 맺음말을 카드에
                    _desc = f"{end.get('epilogue', '')}\n\n{_desc}"
                await game_ch.send(embed=self.rt.embed(title=f"🏁 {end['title']}",
                                                       description=_desc,
                                                       color=0x2b2d31))
                await self.rt.lock_channel(game_ch)
            except Exception as e:
                print(f"[결말] 결말 카드·채널 잠금 실패: {e}")
        try:
            started = getattr(session, "started_at", 0.0) or 0.0
            seconds = max(0.0, time.time() - started) if started else 0.0
            for uid in (getattr(session, "players", {}) or {}):
                await _stats.bump(uid, session_seconds=seconds)
                await _stats.add_ending(uid, getattr(session, "scenario_id", ""), end["id"])
            if game_ch is not None:
                await self.rt.refresh_boards(self.bot, game_ch.guild)
        except Exception as e:
            print(f"[결말] 통계·보드 갱신 실패: {e}")
        await self.rt.save_session_data(self.bot, session)
        if master_ch:
            try:
                refund = res.get("refund") or {}
                await master_ch.send(f"🏁 결말 `{end['id']}` — {end['title']} · 세션 종료·정산 완료"
                                     + (" · 환급 " + ", ".join(f"<@{u}> {v}잉크" for u, v in refund.items())
                                        if refund else ""))
            except Exception:
                pass
        return True

    async def _cleanup_uncommitted_output(self, session, prep) -> None:
        """커밋되지 않은 시도의 전달 출력 정리(ID는 tx에 보존) — 정본처럼 남지 않게."""
        ids = list(getattr(prep.delivery, "canonical_message_ids", ()) or ()) + \
            list(getattr(prep.delivery, "media_message_ids", ()) or ())
        game_ch = self.bot.get_channel(session.game_ch_id)
        if not ids or game_ch is None:
            return
        msgs = []
        for mid in ids:
            if mid is None:
                continue
            try:
                msgs.append(await game_ch.fetch_message(mid))
            except Exception:
                continue
        try:
            await self.rt.clear_messages(msgs)
        except Exception:
            pass

    async def _drain_history_cleanup(self, session) -> int:
        """durable 정리 부채(E-E2) 실행 — 멱등, 실패는 부채로 남아 다음 복구·입력에서 재시도.

        부채에는 봇 정본 출력 ID만 있다(플레이어 메시지 없음). 현재 선택 시도의 출력은
        부채에 있어도 삭제하지 않는다. 이야기·재무에는 영향이 없다.
        """
        try:
            res = await _turn_history.drain_cleanup(self.bot, session)
        except Exception as e:
            print(f"[WP-E] 출력 정리 부채 실행 실패(부채 유지): {e}")
            return 0
        # E-E2b: unmapped 부채는 수동 정리 안내가 실제로 전달된 뒤에만 닫는다(ack).
        #   채널 부재·전송 실패 시 부채가 남아 다음 admission/재시작에서 다시 안내된다.
        manual = res.get("manual") or []
        if manual:
            master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
            sent = False
            if master_ch:
                try:
                    await master_ch.send(
                        "⚠️ 일부 이전 턴 출력은 매핑 기록이 없어 자동 정리하지 못했습니다. "
                        "게임 채널을 확인해 수동으로 정리해 주십시오.")
                    sent = True
                except Exception as e:
                    print(f"[WP-E] 수동 정리 안내 실패(부채 유지·재안내): {e}")
            if sent:
                for m in manual:
                    _turn_history.ack_manual_cleanup(session, m["op_id"])
                session._history_cleanup_clear = not _turn_history.pending_cleanups(
                    session.session_id)
        return int(res.get("deleted") or 0)

    async def _settle_history_op(self, session, prep, master_ch) -> None:
        """재생성 교체 시도 종결 후 남은 의도 처분.

        비커밋 종결 → 이전 선택 시도의 커밋 직후 정본으로 복원. 교체가 COMMITTED인데 선택
        기록만 실패했다면 되돌리지 않고 선택을 정합(degraded)한다 — 청구된 이야기가 정본.
        """
        if prep is None:
            return
        op = _turn_history.rerender_op_for(session.session_id, prep.transaction_id)
        if not op:
            return
        if prep.phase == _turn_preparation.PREP_RETRY_PENDING:
            return          # 같은 시도로 재시도 가능 — 의도 유지(재시작 시 정합이 처분)
        tx = _turn_transaction.get_active_transaction(session)
        if tx is not None and tx.transaction_id == prep.transaction_id \
                and not _turn_transaction.is_terminal(tx.status):
            return
        try:
            res = await _turn_history.settle_rerender(self.bot, session)
        except Exception as e:
            session.commit_recovery = {"status": "PENDING", "stage": "HISTORY_RERENDER_ABORT",
                                       "error": f"{type(e).__name__}: {e}"}
            print(f"⛔ [WP-E] 재생성 종결 처분 실패 — 차단: {e}")
            return
        if res.get("action") == "ABORTED" and master_ch:
            await master_ch.send("↩️ 재생성 시도가 확정되지 않아 기존 턴을 그대로 유지합니다(청구 0).")
        elif res.get("action") == "SELECTED":
            # 교체가 COMMITTED로 확정(선택은 재정합으로 맞춤) — durable 부채로 이전 출력 정리
            await self._drain_history_cleanup(session)

    async def _abort_rerender(self, session, master_ch) -> None:
        try:
            await _turn_history.abort_rerender(self.bot, session)
            if master_ch:
                await master_ch.send("↩️ 재생성 시도가 확정되지 않아 기존 턴을 그대로 유지합니다(청구 0).")
        except Exception as e:
            session.commit_recovery = {"status": "PENDING", "stage": "HISTORY_RERENDER_ABORT",
                                       "error": f"{type(e).__name__}: {e}"}
            print(f"⛔ [WP-E] 재생성 중단 복원 실패 — 차단: {e}")

    async def rerender_latest(self, session, *, addendum: str = "") -> tuple:
        """같은 논리 턴 attempt+1 재생성 — 선언·판단 보존, 지시층위부터 일반 파이프라인.

        Returns: (started: bool, reason: str)
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        async with self._lock_for(session):
            _turn_history.try_flush(session)        # E-E2 보전 매핑 먼저 인덱스로
            entry, rec, reason = _turn_history.rerender_target(session)
            if entry is None:
                return False, reason
            # H06-E: DYNAMIC 턴은 보존된 의미 선택 + 같은 정의 revision에서만 재서술한다(상태 변경 전 검사)
            w_intent, w_reason = _turn_world.rerender_world_intent(
                session, rec.get("judgment"), (rec.get("post") or {}).get("world_version"))
            if w_reason:
                return False, w_reason
            try:
                tx = await _turn_history.begin_rerender(
                    self.bot, session, entry, rec, addendum=addendum)
            except _turn_history.HistoryError as e:
                return False, str(e)
            tid = tx.transaction_id
            j = tx.judgment_result
            try:
                if w_intent is not None:
                    # 턴 이전 세계로 복원된 뒤 새 시도의 세계 계획을 다시 세운다(옛 시도의 WorldTurn 재사용 금지)
                    _ctx = _player_world.session_world_context(session)
                    if _ctx.dynamic:
                        _wt = _turn_world.WorldTurn(ctx=_ctx, intent=w_intent)
                        tx.world_turn = _wt
                        self._ensure_world_plan(session, _wt, tid)
                    else:
                        # 세계 연산 없이 커밋하지 않는다 — 교체 시도를 묘사 전에 종결하고 기존 턴 유지
                        await self._fail_rerender_before_narration(session, tid, master_ch)
                        return False, f"턴 이전 세계가 DYNAMIC이 아닙니다({_ctx.reason}) — 기존 턴 유지"
                # v7.12: 같은 판정 결과를 재사용하므로 원 턴 굴림의 성장·실패 누적도 그대로 다시 스테이징한다
                #   (재추첨·이중 적용 없음, 커밋 뒤 성장 알림은 다시 내지 않음).
                _turn_preparation.restage_roll_effects(session, tid, (j or {}).get("roll_effects"))
                if master_ch:
                    await master_ch.send(
                        f"🔁 **[재생성]** {rec['gm_turn']}턴을 같은 선언·판단으로 다시 서술합니다 "
                        f"(시도 {tx.attempt}). 기존 턴 청구는 유지됩니다.")
                decision = await self._call_gm_logic(
                    session, j["player_message"], list(j.get("roll_results") or []), master_ch,
                    action="PROCEED", transaction_id=tid)
                if not decision:
                    await self._fail_rerender_before_narration(session, tid, master_ch)
                    return False, "지시층위 호출 실패 — 기존 턴 유지"
                decision = {**(j.get("judgment") or {}), **decision, "action": "PROCEED"}
                _turn_preparation.stage_instruction_effects(
                    session, decision, transaction_id=tid)
                instruction = _clean_proceed_instruction(decision.get("proceed_instruction") or "") \
                    or "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=tid)
                return True, ""
            except Exception as e:  # noqa: BLE001 — v7.33(F1): 묘사 이전 예외는 교체 시도만 종결(기존 턴 유지)
                _logs.error("turn", "재서술 중 예기치 않은 예외", exc=e, session=session)
                _p = _turn_preparation.get_preparation(session, tid)
                if _p is None or _p.phase == _turn_preparation.PREP_COLLECTING:
                    await self._fail_rerender_before_narration(session, tid, master_ch)
                return False, f"예기치 않은 오류({type(e).__name__}) — 기존 턴 유지"

    async def _recovery_admission(self, session) -> None:
        """게임 채널 입력 경로의 복구 admission — gm_active와 무관하게 먼저 실행된다."""
        async with self._lock_for(session):
            if not getattr(session, "is_processing", False):
                # WP-F: 주인 없는 대기 안내(재시작 잔재)·만료된 transient 재시도 정리.
                try:
                    await self.rt.message_lifecycle.sweep(
                        self.bot, session, prefix=self.rt.message_lifecycle.WAITING_PREFIX)
                    await self.rt.message_lifecycle.sweep(self.bot, session, only_expired=True)
                except Exception as e:
                    print(f"[메시지 수명주기] transient 정리 실패(등록부 유지): {e}")
            if not getattr(session, "commit_recovery", None):
                # WP-E(E-E2): 보전된 출력 매핑 재정합 · 남은 출력 정리 부채 재시도
                if _turn_history.needs_reconcile(session):
                    _turn_history.try_flush(session)
                if not getattr(session, "_history_cleanup_clear", False):
                    await self._drain_history_cleanup(session)
                return
            master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
            await self._recover_then_maybe_round(session, master_ch)

    async def _resume_commit_recovery(self, session, master_ch) -> bool:
        """차단 중인 커밋 복구를 같은 durable 입력으로 재시도한다. True면 차단 해제."""
        rep = await _commit_coordinator.recover_session(
            self.bot, session, context="in_process")
        if not rep.blocked:
            # WP-E: D 커밋 복구 이후 선택 이력·이력 조작 의도 정합(모순이면 다시 차단).
            _h = await _turn_history.reconcile(self.bot, session)
            if not _h["ok"]:
                rep = _commit_coordinator.RecoveryReport(
                    "RECOVERY_REQUIRED", action=_h["action"], detail=_h["detail"])
            else:
                await self._drain_history_cleanup(session)      # E-E2 남은 정리 부채
        if rep.blocked:
            if master_ch:
                try:
                    await master_ch.send(
                        f"⏸️ 커밋 복구 대기({rep.status}) — 새 턴을 진행하지 않습니다. {rep.detail}")
                except Exception:
                    pass
            return False
        # durable 사실상 미결 시도가 없는데 런타임 tx가 COMMITTING으로 남았다면(영속 전
        #   owner 예외) — 정본은 영속되지 않았으므로 실패로 종결해 새 선언을 받는다.
        _tx = _turn_transaction.get_active_transaction(session)
        if (_tx is not None
                and _tx.status == _turn_transaction.TurnStatus.COMMITTING):
            _turn_transaction.finalize(
                session, _tx.transaction_id, _turn_transaction.TurnStatus.FAILED_SYSTEM,
                failure_stage="COMMIT_ORPHANED",
                failure_code=_turn_transaction.FailureCode.COMMIT_VALIDATION_FAILURE)
            _p = getattr(_tx, "preparation", None)
            if _p is not None:
                _p.phase = _turn_preparation.PREP_FAILED
        if rep.status == "RESOLVED":
            await self._emit_commit_derived(session, rep.derived)
            if rep.action == "FINALIZED":
                await self._send_turn_cost_report(session, rep.settlement)
            try:
                await self.rt.refresh_display(self.bot, session, reason="turn_end")
            except Exception:
                pass
            if master_ch:
                try:
                    await master_ch.send(f"✅ 커밋 복구 완료({rep.action}).")
                except Exception:
                    pass
        return True

    async def _apply_prepared_extraction(self, session, prep, *, derived=None) -> None:
        """READY 이후 추출 계획 호환 적용(멱등). 계획(정규화 typed 필드)만이 권위다."""
        plan = prep.extraction_plan
        if plan is None or plan.applied:
            return
        tid = prep.transaction_id
        _applied_ids = getattr(session, "_extraction_applied_tx", None)
        if _applied_ids is None:
            _applied_ids = session._extraction_applied_tx = []
        if tid in _applied_ids:
            print(f"[추출/{session.session_id}] 이미 적용된 트랜잭션 — 이중 적용 방지 (tx={tid})")
            return
        _applied_ids.append(tid)
        if len(_applied_ids) > 16:
            del _applied_ids[:-16]
        _mch = None if derived is not None else \
            self.bot.get_channel(getattr(session, "master_ch_id", 0))
        # v7.38(D5): DYNAMIC PC 상태 — 규칙(시간·장소·섭취)으로 부여·해제를 확정하고 상태 시계를 같은 커밋에 기록
        _status_rules.resolve_turn(session, prep)
        _gl = _growth.apply_turn(session, prep)                     # v7.50(E8): 성장 포인트·등급·성장 사용
        if _gl:
            plan.diagnostics["growth"] = list(_gl)
        _wt = getattr(prep, "world_turn", None)                         # v7.53: 사건이 준 물건(GRANT_ITEM)
        _grants = _turn_world.item_grant_changes(session, prep) if _wt is not None else []
        if _grants:
            _rows = _items.apply_event_grants(
                session, _grants, definition=_wt.ctx.definition,
                pc_name=getattr(getattr(plan, "item_plan", None), "pc_name", None)
                or next((p.get("name") for p in (session.players or {}).values() if p.get("name")), None))
            plan.diagnostics["item_grants"] = list(_rows)
            plan.entries = list(plan.entries) + [
                {"domain": "item", "target": r["target"], "op": "delta", "payload": {"item": r["item"], "delta": r["delta"]}}
                for r in _rows]
        await self._apply_extraction_plan(session, plan, _mch, derived=derived)

    async def _retire_legacy_extraction_retry(self, session) -> bool:
        """WP-G(RE-GATE): 준비 모드가 아닌(WP-C 이전) 추출 재시도 컨텍스트의 fail-closed 호환 은퇴.

        provider 추출을 다시 부르지 않고, 추출 계획을 적용하지 않으며, 정본 게임 상태를 바꾸지
        않는다. 차단(extraction_pending)과 구 컨텍스트는 strict 세션 저장이 성공한 뒤에만
        해제된다 — 저장이 실패하면 메모리 상태도 되돌려 성공으로 보고하지 않는다.
        새 트랜잭션·별도 커밋 권위를 만들지 않는다.

        Returns: True = 은퇴 영속 완료, False = 저장 실패(차단·컨텍스트 유지)
        """
        async with self._lock_for(session):
            ok = await _commit_coordinator.retire_legacy_retry_context(self.bot, session)
        if not ok:
            return False
        master_ch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
        if master_ch:
            try:
                await master_ch.send(
                    "ℹ️ **[추출 재시도]** WP-C 이전 형식의 재시도 요청을 폐기했습니다 — "
                    "현재 커밋 권위로 적용할 수 없어 정본 상태를 소급 변경하지 않았습니다.")
            except Exception:
                pass
        return True

    async def _retry_prepared_extraction(self, session) -> str:
        """추출 재시도 — 같은 논리 시도·같은 준비 owner로 재개한다(새 자동 턴 아님).

        Returns: "ready" | "failed" | "released"
        """
        TP = _turn_preparation
        ctx = getattr(session, "extraction_retry_ctx", {}) or {}
        master_ch = self.bot.get_channel(session.master_ch_id)
        async with self._lock_for(session):
            prep = TP.get_preparation(session, ctx.get("transaction_id"))
            if prep is None or prep.phase != TP.PREP_RETRY_PENDING:
                # 런타임 준비 상태 소실(재시작 등) — 미확정 묘사에 추출을 적용하지 않는다.
                #   WP-D: 이 시도를 exact 클레임 ID로 FAILED_SYSTEM(청구 0) 종결한 뒤 해제.
                res = await _commit_coordinator.abandon_retry_pending(self.bot, session)
                if res == "PENDING":
                    return "failed"
                if getattr(session, "extraction_pending", False):
                    session.extraction_pending = False
                    session.extraction_retry_ctx = {}
                    await self.rt.save_session_data(self.bot, session)
                return "released"
            session.is_processing = True
            prep.processing_held = True
            outcome = None
            result = None
            state_before = getattr(session, "_rewind_snapshot", None)
            if state_before is None:
                state_before = self.rt.capture_state(session)
            open_round = False
            try:
                prep.reopen_for_retry()
                # D-D3: 재시도 중 새 provider 오퍼레이션 claim은 호출 전에 내구 기록된다.
                prep.durable_claim = lambda op: _commit_coordinator.record_retry_claim(
                    session.session_id, prep.transaction_id, prep.attempt,
                    getattr(op, "operation_id", None))
                _mch = self.bot.get_channel(getattr(session, "master_ch_id", 0))
                prep.register_task(
                    "extraction", TP.TASK_EXTRACTION,
                    coro=self._prepare_extraction(session, prep, prep.extraction_text or ctx.get("text", ""), _mch),
                    required_success=True, may_spawn_required_child_work=True)
                outcome = await self._join_and_ready(session, prep)
                if outcome == _OUTCOME_READY:
                    result = await self._commit_ready_turn(
                        session, prep, state_before=state_before)
                else:
                    open_round = await self._handle_preparation_failure(
                        session, prep, outcome, master_ch)
            finally:
                prep.durable_claim = None
                await self._release_turn_processing(session, prep)
            committed = (outcome == _OUTCOME_READY and getattr(result, "outcome", None)
                         == _commit_coordinator.CommitOutcome.COMMITTED)
            if outcome == _OUTCOME_READY:
                if not await self._after_commit(session, prep, result, master_ch):
                    return "failed"
                open_round = True
            await self._settle_history_op(session, prep, master_ch)
            if (open_round and session.gm_active
                    and not getattr(session, "commit_recovery", None)):
                await self._start_round(session)
            return "ready" if committed else "failed"

    async def _run_gm_logic_loop(self, session, player_message: str, master_ch,
                                 *, transaction_id: str | None = None):
        """
        지시층위 ASK / ROLL / PROCEED 루프.
        PROCEED 완료 후 자동으로 _start_round()를 호출하여 다음 라운드(선제 행동 질문)를 시작.

        NOTE: 이 함수는 락 없이 실행됨. 호출 측에서 이미 락을 잡고 있거나,
              비동기 태스크로 독립 실행되는 경우(버튼 콜백 등)에 사용.
        """
        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        action_labels = {
            "ASK":     "🟡 ASK (명확화 요청)",
            "NARRATE": "💬 NARRATE (경량 GM 응답)",
            "ROLL":    "🎲 ROLL (주사위 판정)",
            "PROCEED": "🟢 PROCEED (턴 진행)",
        }

        roll_results: list[str] = []

        game_ch = self.bot.get_channel(session.game_ch_id)
        # WP-F: 새 선언이 들어왔다 — 이전 턴 실패/차단 안내(transient)를 supersede 로 정리한다.
        try:
            await self.rt.message_lifecycle.clear(self.bot, session,
                                               self.rt.message_lifecycle.KEY_TURN_NOTICE)
        except Exception as e:
            print(f"[메시지 수명주기] 턴 안내 정리 실패(등록부 유지·재시도): {e}")

        # ── 방안 6 → 지시층위 순차 주입 ──
        # 세계관 캐시가 유효하면 먼저 서사 방향성을 시뮬레이션하고, 그 결과(sim_result)를
        # 지시층위 첫 결정에 실제로 주입한다.
        # (과거 gather 병렬 실행은 첫 결정이 sim_result를 보지 못해 시뮬레이션 비용만
        #  낭비되는 구조였다 — 순차 주입으로 교정.)
        sim_result: dict | None = None
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        # WP-E(E-E3): 이력 출처가 선택 이력보다 새로운 캐시는 읽지 않는다.
        do_simulation = bool(cache_name and cache_model == _cache.DEFAULT_MODEL
                             and _turn_history.cache_usable(session))

        # ── 세션 오픈 여부 확인 (기획 규정 — 닫혀 있으면 차단) ──
        # 만료된 캐시로 진행하면 API 오류가 난다. 미리 막고 안내한다.
        if not self.rt.is_session_open(session):
            expired = _cache.is_cache_expired(session)
            note = ("유지 시간이 지나 캐시가 만료되었습니다."
                    if expired else "세션이 닫혀 있습니다.")
            await m_send(f"⚠️ {note} 디스플레이에서 세션을 열어 주십시오.")
            if game_ch:
                # WP-F: TRANSIENT_GAME_STATUS — 다음 턴 시도·세션 열기가 supersede(로그 미기록).
                await self.rt.message_lifecycle.send_transient(
                    self.bot, session, game_ch,
                    f"⏸️ {note}\n> 디스플레이 채널의 **세션 열기**를 눌러 주십시오.",
                    key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
            return

        # ── 잔액 검사 (기획 규정 — 소지금 < 예상 최대금액이면 차단) ──
        try:
            _pre_est = self.rt.estimate_turn(session, "PROCEED")
            for _uid in (session.players or {}):
                try:
                    _bal = _accounts.strict_balance(_uid)
                except _accounts.AccountError as _ae:
                    # v7.33(F7): 손상·읽기 실패 계정 — '잉크 부족'으로 오안내하지 않고 턴을 막는다(청구가 어차피 불가)
                    if game_ch:
                        await self.rt.message_lifecycle.send_transient(
                            self.bot, session, game_ch,
                            "⛔ 계정 정보를 읽지 못해 턴을 진행할 수 없습니다. 운영자에게 문의해 주십시오.",
                            key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
                    await m_send(f"⛔ 계정 읽기 실패로 턴 차단 (<@{_uid}>): {type(_ae).__name__}: {_ae}")
                    return
                if _bal < _pre_est["max_ink"]:
                    if game_ch:
                        await self.rt.message_lifecycle.send_transient(
                            self.bot, session, game_ch,
                            f"⛔ **{_master_mod.say('cache.insufficient_ink', '잉크가 부족합니다.')}**\n"
                            f"> 필요 {_pre_est['max_ink']}잉크 / 보유 {_bal}잉크\n"
                            f"> 잉크 지급은 운영자에게 문의해 주십시오.",
                            key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
                    await m_send(f"⛔ 잔액 부족으로 턴 차단 (보유 {_bal} < 필요 {_pre_est['max_ink']})")
                    return
        except Exception as e:
            print(f"[결제] 잔액 검사 실패(진행 허용): {e}")

        # 이번 턴 예상 비용 — 디스플레이 채널 도입 전까지 마스터 채널에 보고한다.
        session.last_estimate = None     # v7.33(F9): 산출 실패 시 지난 턴 예상이 이번 턴 대조·정확도 기록에 섞이지 않게
        try:
            est = self.rt.estimate_turn(session, "PROCEED")
            # WP-G(D3): 압축 비용은 운영자 부담 유지비다 — 계정 효과 없는 '압축 선결제' 20% 가산·
            #   누적·정산 표시를 은퇴했다. 압축 provider 비용은 CostEvent(운영 사실)로만 남는다.
            session.last_estimate = est
            _tts = _estimate.estimate_tts(session)
            # P1: 예상은 정산이 아니다 — 운영 예측 정확도 점검용 구조 로그(커밋 보고의 '예상 대비'와 같은 값)
            _logs.domain("cost.estimate", {"tx": transaction_id, "min_ink": est["min_ink"],
                                               "max_ink": est["max_ink"], "min_krw": est["min_krw"],
                                               "max_krw": est["max_krw"], "basis": est.get("basis"),
                                               "confidence": est.get("confidence"),
                                               "breakdown": est.get("breakdown"),
                                               "tts": _tts if _tts.get("enabled") else None}, session=session)
            # v7.35(R1): 예상은 별도 메시지로 보내지 않는다 — 커밋 뒤 턴 비용 보고의 '층위별 예상 대조'가 같은 값을 싣는다
        except Exception as e:
            print(f"[EST] 예상 산출 실패(진행에는 영향 없음): {e}")

        # 플레이어가 보는 게임 채널에 판단 대기 안내 (판단 완료 후 삭제)
        # 층위별 문구 — 4층위 도입으로 대기가 길어져 같은 문구 반복은 멈춘 듯 보인다.
        # WP-F: TRANSIENT_GAME_STATUS — 성공·실패·예외·취소 어느 경로든 finally 에서 멱등 정리.
        status_msg = await self.rt.WaitingStatus.begin(game_ch, "judgment", session=session)
        gate = None
        judgment = None
        # v7.14: 선언 게이트 입력(DYNAMIC — 현재 장소의 유효 발견·찾지 못한 이전 탐색)
        gate_in = self._gate_inputs(session, transaction_id)
        dynamic = gate_in["dynamic"]
        try:
            # v7.23: DYNAMIC은 매 발화 개연성 시뮬레이션(서사 방향)을 부르지 않는다 — LEGACY만 기존 그대로.
            if do_simulation and not dynamic:
                if game_ch:
                    async with game_ch.typing():
                        sim_result = await self._simulate_narrative_directions(
                            session, player_message, master_ch,
                            transaction_id=transaction_id, gate_inputs=gate_in)
                else:
                    sim_result = await self._simulate_narrative_directions(
                        session, player_message, master_ch,
                        transaction_id=transaction_id, gate_inputs=gate_in)
            gate = _parse_gate((sim_result or {}).get("declaration_gate"), player_message,
                               dynamic=gate_in["dynamic"], place=gate_in["place"],
                               place_name=gate_in["place_name"], rows=gate_in["rows"])

            # v7.23: 대기 중인 코드 경로 질문에 번호만 답했으면 그 질문의 선택지 스냅샷으로 바로 고른다(판단 재호출 없음).
            if dynamic:
                judgment = self._route_number_answer(session, transaction_id, player_message)

            # ── [판단층위] 캐시 미사용 — 진행 유형과 ASK 질문·ROLL 명세를 결정 ──
            # v7.14: 게이트 BLOCK·CLARIFY는 판단 호출 없이 중립 안내로 끝난다(굴림 0).
            if judgment is None and not _gate_blocks(gate):
                if game_ch:
                    async with game_ch.typing():
                        judgment = await self._call_judgment(
                            session, player_message, roll_results, master_ch,
                            transaction_id=transaction_id, gate=gate, declaration_draft=dynamic,
                            discovery_rows=gate_in["rows"])
                else:
                    judgment = await self._call_judgment(
                        session, player_message, roll_results, master_ch,
                        transaction_id=transaction_id, gate=gate, declaration_draft=dynamic,
                            discovery_rows=gate_in["rows"])
                # v7.23 DYNAMIC: 판단이 발견 제안·결과 선취를 표시했을 때만 축소 선언 검증(캐시) → 게이트
                if dynamic and judgment:
                    judgment, gate = await self._dynamic_declaration_check(
                        session, player_message, roll_results, judgment, gate_in, master_ch,
                        transaction_id=transaction_id, cache_ok=do_simulation)
        finally:
            await status_msg.done()

        if _gate_blocks(gate):
            await self._hold_declaration(session, transaction_id, player_message, gate,
                                         _gate_notice(gate), master_ch)
            return
        if not judgment:
            # v7.33(F3): 판단층위 실패(재시도 소진) — 마스터 안내는 _call_judgment가 했다. 플레이어에게 재선언 안내
            await self._abort_turn_before_narration(
                session, transaction_id, player_message, master_ch,
                code=_turn_transaction.FailureCode.JUDGMENT_PROVIDER_FAILURE, stage="JUDGMENT",
                master_text="ℹ️ 판단층위 실패 — 이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다.")
            return
        _rolls = [r for r in (judgment.get("rolls") or []) if isinstance(r, dict)]
        if gate is None and gate_in["dynamic"] and (
                judgment.get("ambient_proposal") is True or any(r.get("discovery") is True for r in _rolls)):
            # P7: 게이트를 확인할 수 없다 — 새 발견 제안은 미처리(실패 아님·부재 단정 없음). 굴림·정보 연산 0.
            await self._hold_declaration(session, transaction_id, player_message, None,
                                         GATE_UNVERIFIABLE_NOTICE, master_ch)
            return
        if not _gate_rolls_discovery(gate):
            for _r in _rolls:
                _r.pop("discovery", None)        # 발견 굴림 표식은 게이트 ROLL에서만 유효(그 밖은 일반 판정)
        # WP-E: 재생성이 판단을 다시 돌리지 않도록 구조화 판단과 지시층위 입력을 tx에 기록한다.
        # H05: 이력에는 transient 후보 키(P1·R2…)가 아니라 의미 기록(PlaceId·경로 서명·접근 토큰)을 남긴다.
        _wt = self._world_turn(session, transaction_id)
        _jnote = judgment
        if _wt is not None and _wt.intent is not None:
            _jnote = dict(judgment)
            # H06-E: 재서술이 같은 정의에서만 같은 의미 선택을 재현하도록 그 턴의 revision을 함께 남긴다
            _jnote[_action_intent.INTENT_FIELD] = {"semantic": _wt.intent.semantic_record(),
                                                       "definition_revision": _wt.ctx.definition_revision}
        _turn_history.note_judgment(
            session, transaction_id, judgment=_jnote,
            player_message=player_message, roll_results=list(roll_results), gate=gate)

        # 정합: 이동 선언인데 목적지가 없거나 후보 밖이면 진행(묘사가 임의로 이동시킴)하지 않고 되묻는다.
        if _wt is not None and _wt.intent is not None \
                and (judgment.get("action") or "").upper() in ("PROCEED", "ROLL") \
                and any(f in ("destination", "exit") for f, _c in (_wt.intent.unresolved or ())):
            await self._ask_destination(session, _wt, transaction_id, master_ch)
            await self.rt.save_session_data(self.bot, session)
            return

        # H05-A: PROCEED면 지시층위 전에 코드 소유 세계 계획을 세운다(정본 불변). 의미 있는 경로 차이는
        #   여기서 플레이어에게 묻는다(경로 선택은 판단층위가 아니라 코드 정책).
        if _wt is not None and (judgment.get("action") or "").upper() == "PROCEED":
            _plan = self._ensure_world_plan(session, _wt, transaction_id)
            if _plan.status == _player_world.PLAN_NEEDS_CHOICE:
                await self._ask_route_choice(session, _plan, transaction_id, master_ch)
                await self.rt.save_session_data(self.bot, session)
                return

        for iteration in range(MAX_ITERATIONS_PER_MESSAGE):
            action = (judgment.get("action") or "ASK").upper()
            reasoning = judgment.get("reasoning", "")

            # ── [지시층위] 캐시 사용 — NARRATE·PROCEED에만 필요 ──
            # ASK와 ROLL은 판단층위가 내용물(bridge_message·rolls)까지 생성하므로
            # 지시층위를 호출하지 않는다 → 캐시 읽기 절감.
            if action in ("NARRATE", "PROCEED"):
                current_sim = sim_result if iteration == 0 else None
                if game_ch:
                    async with game_ch.typing():
                        decision = await self._call_gm_logic(
                            session, player_message, roll_results, master_ch,
                            sim_result=current_sim, action=action,
                            transaction_id=transaction_id)
                else:
                    decision = await self._call_gm_logic(
                        session, player_message, roll_results, master_ch,
                        sim_result=current_sim, action=action,
                        transaction_id=transaction_id)
                if not decision:
                    await m_send("⚠️ 지시층위 호출 실패. 이번 발언을 스킵합니다.")
                    # v7.33(F3): 플레이어에게도 재선언 안내 · 시도는 청구 0 종결
                    await self._abort_turn_before_narration(
                        session, transaction_id, player_message, master_ch,
                        code=_turn_transaction.FailureCode.INSTRUCTION_PROVIDER_FAILURE, stage="INSTRUCTION",
                        master_text="ℹ️ 지시층위 실패 — 이 시도를 취소했습니다(청구 0). 플레이어에게 재선언을 안내했습니다.")
                    return
                # 판단층위 산출물과 병합 — 이후 분기는 기존 구조를 그대로 사용한다.
                decision = {**judgment, **decision, "action": action}

                # WP-B: 지시효과(quest choice/intended_case/info_access)를
                #   canonical 변경 없이 트랜잭션에 스테이징한다(단일 owner 통합).
                #   묘사가 성립한 뒤 _finish_proceed_and_continue가 단일 호환 적용
                #   경계에서만 canonical에 반영한다. 여기서는 아무 상태도 안 바뀐다.
                _turn_preparation.stage_instruction_effects(
                    session, decision, transaction_id=transaction_id)
            else:
                decision = dict(judgment)

            label = action_labels.get(action, action)
            _logs.event("turn", f"판단 {action}", session=session,
                            data={"stage": "judgment", "iter": iteration, "action": action, "reasoning": reasoning})
            # v7.35: PROCEED·ROLL 판단은 커밋 뒤 층위활동 보고(🧭)가 싣는다. 턴이 확정되지 않고 응답을 기다리는
            #   ASK·NARRATE만 지금 한 줄로 알린다(그 시점에 다른 보고가 없다).
            if action not in ("PROCEED", "ROLL"):
                await m_send(f"🤖 **[GM 판단 #{iteration + 1}]** {label} — {reasoning[:120]}")

            # ── ASK ──
            if action == "ASK":
                session.gm_clarify_count += 1
                if session.gm_clarify_count > MAX_CLARIFY_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** ASK 한도({MAX_CLARIFY_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, forced_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                bridge = decision.get("bridge_message") or "어떻게 하시겠습니까?"
                if game_ch:
                    await self.rt.stream_text_to_channel(
                        self.bot, game_ch, bridge,
                        words_per_tick=15, tick_interval=1.5
                    )
                    await self._voice_lines(session, [bridge], transaction_id=transaction_id)
                # ASK 브리지를 current_turn_logs에 기록 → 다음 지시층위 호출 시 맥락 유지
                session.current_turn_logs.append(f"[진행자 (GM)]: {bridge}")
                _logs.event("turn", "ASK 대기", session=session,
                                data={"stage": "ask", "tx": transaction_id, "bridge": bridge})
                # WP-01: 플레이어 입력 대기(비종료). 재입력 시 같은 트랜잭션을 재사용한다.
                _turn_transaction.mark_waiting_for_player(session, transaction_id)
                # 저장은 루프 종료 후 트레일링 save가 일괄 처리 (중복 제거)
                break

            # ── NARRATE ──
            elif action == "NARRATE":
                session.gm_narrate_count = getattr(session, "gm_narrate_count", 0) + 1
                if session.gm_narrate_count > MAX_NARRATE_PER_MESSAGE:
                    await m_send(
                        f"⚙️ **[GM]** NARRATE 한도({MAX_NARRATE_PER_MESSAGE}회) 초과 → 강제 PROCEED로 전환합니다."
                    )
                    forced_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, forced_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                narrate_instr = decision.get("narrate_instruction") or "현재 상황을 간략히 설명하십시오."
                # NOTE: typing 컨텍스트는 _dispatch_narrate 내부 API 호출 블록에서만 활성화됨.
                # 외부에서 typing()으로 감싸면 stream_text_to_channel 실행 시 typing이 살아있어
                # Discord 상충으로 스트리밍이 멈추는 버그 발생 — 외부 typing 제거.
                narrate_text = await self._dispatch_narrate(
                    session, narrate_instr, transaction_id=transaction_id)
                if narrate_text:
                    _logs.event("turn", "NARRATE 대기", session=session,
                                    data={"stage": "narrate", "tx": transaction_id, "n": session.gm_narrate_count,
                                          "text": narrate_text})
                # WP-01: 플레이어 입력 대기(비종료). 재입력 시 같은 트랜잭션을 재사용한다.
                _turn_transaction.mark_waiting_for_player(session, transaction_id)
                # 저장은 루프 종료 후 트레일링 save가 일괄 처리 (중복 제거)
                break  # 플레이어 응답 대기

            # ── ROLL ──
            elif action == "ROLL":
                rolls = decision.get("rolls") or []
                if not rolls:
                    await m_send(
                        "⚠️ GM이 ROLL을 선언했으나 굴림 항목이 비어 있어 PROCEED로 폴백합니다."
                    )
                    fallback_instr = await self._forced_proceed_instruction(
                        session, player_message, roll_results, master_ch, sim_result,
                        transaction_id=transaction_id)
                    await self._finish_proceed_and_continue(
                        session, fallback_instr, master_ch,
                        transaction_id=transaction_id)
                    return

                # WP-01: ROLL은 여기서 루프가 반환되고 버튼/타임아웃 콜백에서 재개된다.
                #        불변 식별자를 View에 실어 async UI 경계를 넘긴다(WAITING_FOR_ROLL).
                _turn_transaction.mark_waiting_for_roll(session, transaction_id)
                # 버튼 UI 전송 후 루프 종료 (계속 처리는 버튼 콜백 담당)
                await self._dispatch_rolls(
                    session, rolls, player_message, list(roll_results),
                    transaction_id=transaction_id)
                await self.rt.save_session_data(self.bot, session)
                return

            # ── PROCEED ──
            elif action == "PROCEED":
                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or ""
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                # NOTE: 자원 증감은 추출층위 소관이다(기획 규정 — 지시층위의 자원·상태
                #       수정권한 제거). 5.27.0에서 apply_extraction으로 옮겼는데
                #       이 경로가 남아 같은 턴에 이중 적용될 수 있었다.

                # ── 방안 E 제거 (방안 D) ──
                # _verify_proceed_instruction 호출 삭제.
                # 미선언 PC 행동 방지는 지시층위 [최우선 절대 원칙]으로 커버.

                # 서사 사건 평가는 event_assessment로 헬퍼에 전달되어 진행도 갱신·재계획에 사용된다.
                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=transaction_id)
                return

            else:
                await m_send(f"⚠️ GM이 알 수 없는 action을 반환했습니다: {action}")
                break

        else:
            # 루프 한도 도달 → 강제 PROCEED
            await m_send(f"⚙️ GM 내부 루프 한도({MAX_ITERATIONS_PER_MESSAGE}) 도달 → 강제 PROCEED.")
            await self._finish_proceed_and_continue(
                session, "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오.", master_ch,
                transaction_id=transaction_id)
            return

        # ASK/NARRATE는 break로 여기 도달 — 대기 상태를 한 번 저장한다.
        await self.rt.save_session_data(self.bot, session)

    async def _call_judgment(self, session, player_message: str, roll_results: list,
                             master_ch, *, transaction_id: str | None = None, gate=None,
                             declaration_draft: bool = False, discovery_rows=()) -> dict | None:
        """
        판단층위 호출 — 진행 유형(ASK/NARRATE/ROLL/PROCEED)을 결정한다.

        [캐시 미사용]
        판단은 초단기 맥락과 선언만으로 가능하므로 세션 캐시를 읽지 않는다.
        ROLL 판정 명세(rolls)도 이 층위가 생성하므로, ROLL이 발생해도
        캐시 읽기는 뒤이은 지시층위 호출 1회로 유지된다.
        (분리 이전에는 지시층위가 두 번 호출되어 캐시를 2회 읽었다.)

        Returns:
            판단 결과 dict 또는 실패 시 None. 재시도는 이 함수 내부에서 처리한다.
        """
        # WP-01: 현재 트랜잭션이면 JUDGING으로 표기(수동/인트로 경로는 no-op).
        _turn_transaction.mark_transaction_status(
            session, transaction_id, _turn_transaction.TurnStatus.JUDGING)
        # H05-A: DYNAMIC 세션이면 코드 후보 매니페스트를 같은 판단 호출에 싣는다(추가 provider 호출 0).
        _wt = self._world_turn(session, transaction_id, create=True)
        _schema = JUDGMENT_RESPONSE_SCHEMA
        if _wt is not None:
            _wt.pending_route = None                       # v7.23: 일반 판단이 돌면 이전 경로 번호 스냅샷은 낡는다
            self._build_world_manifest(session, _wt)
            _schema = _action_intent.extend_judgment_schema(JUDGMENT_RESPONSE_SCHEMA, _wt.manifest)
            if declaration_draft and not gate:
                # v7.23: 조건부 선언 검증 촉발 표시(스키마 사본 확장 — prompts.py 원본 불변)
                _schema["properties"]["assertion_claim"] = {"type": "boolean"}
                _schema["properties"]["discovery_target"] = {"type": "string"}
                _schema["properties"]["discovery_ref"] = {"type": "string",
                                                          "enum": ["NONE"] + [r[0] for r in discovery_rows]}
        # v7.46(A): 굴림 skill은 PC가 가진 스킬 이름만, item은 착용·손에 든 물건 키만(스키마 사본 — prompts.py 원본 불변)
        _skills = sorted({n for p in (session.players or {}).values() if isinstance(p, dict)
                          for n in _pc_skills(session, p)
                          if _judgment.is_invoked(session.scenario_data, n)})   # v7.50: 상시 기술은 지목하지 않는다
        _ekeys = []
        if _wt is not None:
            _defs = _items.definitions_of(_wt.ctx.definition)
            _ekeys = sorted({r[0] for p in (session.players or {}).values() if isinstance(p, dict)
                             for r in _items.equip_rows(session, p.get("name"), _defs)})
        if _skills or _ekeys or _wt is not None:
            import copy as _copy
            _schema = _copy.deepcopy(_schema)
            _ri = _schema["properties"]["rolls"]["items"]["properties"]
            if _skills:
                _ri["skill"] = dict(_ri.get("skill") or {"type": "string"}, enum=["NONE"] + _skills)
            _ri["item"] = {"type": "string", "enum": ["NONE"] + _ekeys,
                           "description": "PC 굴림에 직접 쓰인 착용·손에 든 물건 키(E키) — 그 굴림 분야의 유리한 효과가 있는 것. "
                                          "없으면 NONE"}
            if _wt is not None:
                # v7.47(W5·W6·S2): 굴림 분야 — 장비 효과·상태 불이익을 코드가 이 분야로 고른다. PC 상태 보정은 코드가 정한다.
                _dom = {"type": "string", "enum": _roll_domains(),
                        "description": "이 굴림 행동의 분야(근접=치고 베기, 방어=맞고 버티기·막기, 기동=달리기·오르기 …). "
                                       "어디에도 맞지 않으면 기타"}
                _ri["domain"] = _dom
                _schema["properties"]["rolls"]["items"]["required"] = \
                    list(_schema["properties"]["rolls"]["items"].get("required") or []) + ["domain"]
                _ri["status_mod"] = dict(_ri.get("status_mod") or {"type": "integer"},
                                         description="PC 굴림은 0(상태 보정은 코드가 단계 불이익으로 계산한다)")
                _op = _ri.get("opponent") or {}
                if isinstance(_op.get("properties"), dict):
                    _op["properties"]["domain"] = dict(_dom, description="상대 쪽 행동의 분야(공격을 막으면 방어 등)")
                    _op["properties"]["status_mod"] = dict(_op["properties"].get("status_mod") or {"type": "integer"},
                                                           description="0(상태 보정은 코드가 계산한다)")
        # v7.50(E8 · D-B2 가): 성장 사용 — 지금 쓸 수 있을 때만 후보(코드가 포인트·장소·상한을 거른다)
        _gopts = _growth.spend_options(session, next(iter(session.players or {}), None),
                                           (getattr(session, "spatial_state", None) or {}).get("scene_place")) \
            if _wt is not None else []
        if _gopts:
            import copy as _copy
            _schema = _copy.deepcopy(_schema)
            _schema["properties"]["growth"] = {
                "type": "string", "enum": ["NONE"] + [k for k, _d, _c in _gopts],
                "description": "플레이어가 이번 선언에서 성장(단련·학습)을 명시적으로 고를 때만 그 키, 아니면 NONE"}
        user_prompt = _build_judgment_user_prompt(session, player_message, roll_results, world_turn=_wt, gate=gate,
                                                  declaration_draft=declaration_draft and not gate,
                                                  discovery_rows=discovery_rows if declaration_draft and not gate else ())
        if _gopts:
            user_prompt += ("\n[성장 — 플레이어가 성장 포인트로 단련·학습을 명시적으로 선언할 때만 growth 키를 고른다. "
                            "그 밖에는 NONE. 반영은 코드가 커밋에서 다시 확인한다]\n"
                            + "\n".join(f"· {k}: {d_} ({c}점)" for k, d_, c in _gopts) + "\n")

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            http_options=_resilience.provider_http_options("judgment"),  # WP-C B-C4
            system_instruction=JUDGMENT_SYSTEM_INSTRUCTION,
            temperature=0.4,
            response_mime_type="application/json",
            response_schema=_schema,
            safety_settings=_constants.TRPG_SAFETY_SETTINGS,
        )

        # 층위 자체 재시도 — 판단 실패가 지시층위 재시도로 번지지 않게 한다.
        # WP-02: 외부 판정 재시도 전체가 하나의 논리 오퍼레이션. operation_id를
        #        루프 밖에서 1회 생성하고 provider_attempt는 실제 호출마다 증가시킨다.
        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_TURN_JUDGMENT, session=session,
            model=JUDGMENT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
        # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
        _turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
        decision = None
        for attempt in range(JUDGMENT_MAX_RETRIES):
            # 타임아웃 보호 — 지연된 원 응답이 재시도 결과와 경합하지 않도록
            # wait_for가 태스크를 취소한다(기획 규정).
            ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=JUDGMENT_MODEL, contents=contents, config=config,
                ),
                layer="judgment", session_id=session.session_id, retries=1,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not ok:
                continue

            # 비용 정산 — 캐시 미사용이므로 cached_tokens는 0
            try:
                meta = response.usage_metadata
                in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)
                breakdown = _cost.calculate_text_gen_cost_breakdown(
                    JUDGMENT_MODEL,
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cached_read_tokens=cached_tokens,
                )
                cost = breakdown["total_krw"]
                self.rt.accrue(session, cost, breakdown["total_usd"])
                # WP-02 shadow 관측 — 레거시 계산값 그대로 기록(shadow == legacy).
                _cl_op.record(
                    input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                    output_tokens=out_tokens, thought_tokens=thought_tokens,
                    cost_usd=breakdown["total_usd"], cost_krw=cost,
                    usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
                _io.write_cost_log(
                    session.session_id, f"{COST_LOG_PREFIX}판단층위 호출",
                    in_tokens, cached_tokens, out_tokens, cost, session.total_cost
                )
                _estimate.update_stats(session, "judgment", out_tokens, thought_tokens)
                print(
                    f"[GM/{session.session_id}] 판단 비용: "
                    f"In={in_tokens:,} Out={out_tokens:,} → {_cost.format_cost(cost)}"
                )
                if not hasattr(session, "turn_cost_log"):
                    session.turn_cost_log = []
                session.turn_cost_log.append({
                    "label": "판단층위", "cost": cost,
                    "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                    "manifest": [], "layer": "judgment",
                    # v7.30: 로우 입력 구성(확정·유력·가변·키워드) — 보고·예측 공용
                    "census": _prompt_census.record(
                        session, "judgment", contents, system_text=_sys_text(config),
                        in_tokens=in_tokens, cached_tokens=cached_tokens),
                })
            except Exception as e:
                print(f"[GM] 판단층위 비용 정산 실패: {e}")

            raw_text = response.text or ""
            try:
                decision = json.loads(raw_text)
            except json.JSONDecodeError:
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
                try:
                    decision = json.loads(cleaned)
                except Exception as e:
                    _logs.event("turn", "판단층위 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                    data={"code": "JUDGMENT_JSON", "attempt": attempt + 1})
                    continue
            # v7.12: ROLL 계약 검증 — 범위 밖·누락 값을 보정해 굴리지 않는다. 파싱 실패와 같은 재시도 경로.
            #   (rolls가 빈 ROLL은 기존 PROCEED 폴백 경로가 처리한다)
            if (isinstance(decision, dict) and str(decision.get("action", "")).upper() == "ROLL"
                    and decision.get("rolls")):
                _errs = _judgment.validate_rolls(decision.get("rolls"))
                if _errs:
                    print(f"[GM] 판단층위 ROLL 계약 위반(시도 {attempt + 1}): {_errs}")
                    self.rt.write_log(session.session_id, "api", "[판정 계약 위반] " + "; ".join(_errs))
                    decision = None
                    continue
            # v7.14: 게이트가 ROLL이면 그 발견 탐색 굴림(discovery=true)이 있어야 한다 — 없으면 같은 재시도 경로.
            if _gate_rolls_discovery(gate) and not (
                    isinstance(decision, dict) and str(decision.get("action", "")).upper() == "ROLL"
                    and any(isinstance(r, dict) and r.get("discovery") is True
                            for r in decision.get("rolls") or ())):
                print(f"[GM] 판단층위 발견 굴림 누락(시도 {attempt + 1}) — 게이트 ROLL")
                self.rt.write_log(session.session_id, "api", "[판정 계약 위반] 게이트 ROLL인데 discovery 굴림 없음")
                decision = None
                continue
            break

        if not decision:
            if master_ch:
                await master_ch.send("⚠️ 판단층위 호출에 실패했습니다. 이번 발언을 스킵합니다.")
            return None

        if _wt is not None and _wt.manifest is not None:
            # 후보 키 → 정본 참조(코드 검증). raw 문자열은 정본이 되지 않는다. 새 의도 = 재계획.
            _wt.intent = _action_intent.resolve_intent(
                _wt.manifest, decision.get(_action_intent.INTENT_FIELD), world=session)
            self._settle_move_intent(session, _wt, transaction_id)
            _wt.plan = None
            self.rt.write_log(session.session_id, "api",
                           "[H05 의도]\n" + json.dumps(_wt.intent.semantic_record(), ensure_ascii=False)
                           + f"\nunresolved={list(_wt.intent.unresolved)}")
        return decision

    async def _call_gm_logic(self, session, player_message: str, roll_results: list,
                              master_ch, sim_result: dict | None = None,
                              action: str = "PROCEED",
                              *, transaction_id: str | None = None) -> dict | None:
        """
        지시층위 모델 호출. DEFAULT_MODEL 사용.

        [캐시 활용 전략]
        세션 캐시(scenario_data + NPC 사전 + 세계관)가 유효한 경우, cached_content로 호출하여
        지시층위가 시나리오 전체 컨텍스트를 읽도록 한다.

        [방안 ①] GM_LOGIC_SYSTEM_INSTRUCTION은 세션 캐시 본문에 함께 구워져 있으므로,
        캐시 경로에서는 contents에 user_prompt만 넣는다(지시문이 캐시 읽기 단가로 처리됨):
          contents[0] user  : _build_logic_user_prompt()   (현재 상황 + 플레이어 발언)

        캐시가 없거나 모델 불일치 시(폴백): 캐시에 지시문이 없으므로
        GenerateContentConfig.system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION 방식으로 전달한다.

        Args:
            sim_result: 방안 6 서사 설계자 결과 (첫 번째 호출에만 주입, 이후 None)
        """
        # WP-01: 현재 트랜잭션이면 INSTRUCTING으로 표기(수동/인트로 경로는 no-op).
        _turn_transaction.mark_transaction_status(
            session, transaction_id, _turn_transaction.TurnStatus.INSTRUCTING)
        _wt = self._world_turn(session, transaction_id)
        _narrate = (action or "").upper() == "NARRATE"
        if _wt is not None and _wt.plan is None and not _narrate:
            # 지시층위도 이번 턴 정본 계획(장소 고정 포함)을 본다 — ROLL 뒤 PROCEED 경로(순수 계획)
            self._ensure_world_plan(session, _wt, transaction_id)
        # P3(G1): NARRATE는 세계를 커밋하지 않는다 — 계획·투영·필수 서사·전달 대기 없이 정본 기준 뷰로 지시한다.
        _wv = dataclasses.replace(_wt, plan=None) if (_wt is not None and _narrate) else _wt
        if _wt is not None:
            sim_result = None             # v7.23: DYNAMIC 지시는 서사 방향 시뮬레이션을 받지 않는다(강제 진행 경로 포함)
        user_prompt = _build_logic_user_prompt(session, player_message, roll_results,
                                                sim_result=sim_result, world_turn=_wv, action=action)
        _tie_schema = None
        if _wv is not None and _narrate:
            user_prompt += _turn_world.narrate_hold_block(_wv, session)
        elif _wt is not None and _wt.plan is not None:
            user_prompt += _turn_world.narration_overlay(_wt.plan, _wt.ctx.definition, _wt.ctx, world=session)
            user_prompt += _turn_world.pending_delivery_block(_wt.ctx, session)
            # P1(D2): 남은 자유 경합(같은 분 서사 선후)은 묘사 전에 지시층위가 고른다 — 무효면 코드 결정
            _tie_block, _tie_schema = _turn_world.tie_choice_schema(_wt.plan, _wt.ctx.definition, _wt.ctx)
            user_prompt += _tie_block
        _jr = self._tx_judgment(session, transaction_id)
        if _jr:
            user_prompt += _declaration_block(_jr, transaction_id)     # v7.14 권위 있는 시도·발견 결과
        if _wt is not None:
            user_prompt += WORLD_REACTION_AUTHORITY_BLOCK                 # v7.23 DYNAMIC 세계 반응 권위
        # 판단층위가 확정한 진행 유형을 지시층위에 전달한다. 지시층위는 이 유형을 바꾸지 않는다.
        user_prompt = (
            f"[확정된 진행 유형] {action}\n"
            f"※ 이 유형은 판단층위에서 이미 결정되었습니다. 변경하지 말고, "
            f"이 유형에 필요한 지시문만 작성하십시오.\n\n"
        ) + user_prompt

        # SVG 카드: 얼굴 식별 후보(장면 카드 인물의 transient 키) — 응답 키는 서버가 ActorId로 검증한다
        _card_world = self.rt.speaker_card.card_world(session) if _wt is not None else None
        _id_manifests = {}
        for _kind in ("face", "name"):
            _m = self.rt.speaker_card.identity_manifest(_card_world, _wv, session, _kind) if _card_world else {}
            if _m:
                _id_manifests[_kind] = _m
                user_prompt += self.rt.speaker_card.identity_block(_m, _card_world.definition, session, _kind)


        # 캐시 활용 가능 여부 판단
        cache_name  = getattr(session, "cache_name",  None)
        cache_model = getattr(session, "cache_model", None)
        # WP-E(E-E3): 되감기/재생성 뒤 미래 이력을 담은 캐시는 쓰지 않는다(캐시 없는 폴백).
        use_cache   = bool(cache_name and cache_model == _cache.DEFAULT_MODEL
                           and _turn_history.cache_usable(session))

        try:
            # ── 상황별 스키마 조립 ──
            # 퀘스트를 고를 수 없는 상황(진행 중)에서는 quest_choice 필드를
            # 스키마에서 아예 뺀다. 필드가 없으면 모델이 값을 낼 수 없으므로
            # 잘못된 선택이 구조적으로 불가능해진다.
            logic_schema = GM_LOGIC_RESPONSE_SCHEMA
            _retired = _player_world.legacy_narrative_retired(session)
            if _retired:
                # H07-C: 동적 세계 세션 — 레거시 퀘스트 선택·info_access(정보 원장) 필드를 스키마에서 뺀다
                #        (prompts.py 원본 스키마 불변, 런타임 사본만). 값을 낼 수 없으니 병렬 권위가 생길 수 없다.
                logic_schema = dict(GM_LOGIC_RESPONSE_SCHEMA)
                #   v7.25(F12): 레거시 서사 계획 신호 event_assessment도 뺀다(동적 세계에는 소비할 계획이 없다)
                _gone = ("quest_choice", "quest_case", "info_access", "event_assessment")
                logic_schema["properties"] = {k: v for k, v in logic_schema["properties"].items() if k not in _gone}
                logic_schema["required"] = [k for k in logic_schema["required"] if k not in _gone]
            try:
                q_schema = None if _retired else _quest.choice_schema(session)
                c_schema = None if _retired else _quest.case_schema(session)
                if q_schema or c_schema:
                    logic_schema = dict(GM_LOGIC_RESPONSE_SCHEMA)
                    logic_schema["properties"] = dict(logic_schema["properties"])
                    logic_schema["required"] = list(logic_schema["required"])
                    if q_schema:
                        logic_schema["properties"]["quest_choice"] = q_schema
                        logic_schema["required"].append("quest_choice")
                    if c_schema:
                        # 활성 퀘스트가 있을 때만. 지시층위는 방향만 제시하고
                        # 진전 여부는 추출 수치가 정한다.
                        logic_schema["properties"]["quest_case"] = c_schema
                        logic_schema["required"].append("quest_case")
            except Exception as e:
                print(f"[퀘스트] 스키마 조립 실패: {e}")
            if _id_manifests:
                logic_schema = dict(logic_schema)
                logic_schema["properties"] = dict(logic_schema["properties"])
                for _kind, _m in _id_manifests.items():
                    logic_schema["properties"][self.rt.speaker_card.identity_field(_kind)] = \
                        self.rt.speaker_card.identity_schema(_m, _kind)
            if _tie_schema is not None:
                logic_schema = dict(logic_schema)
                logic_schema["properties"] = dict(logic_schema["properties"])
                logic_schema["properties"]["tie_order"] = _tie_schema

            if use_cache:
                # ── 캐시 활용 경로 (방안 ①) ──
                # GM_LOGIC_SYSTEM_INSTRUCTION은 이제 세션 캐시 본문에 함께 구워져 있으므로
                # contents에 신선 입력으로 다시 넣지 않는다(캐시 읽기 단가로 처리 → 비용 절감).
                logic_contents = [
                    types.Content(role="user",
                                  parts=[types.Part.from_text(text=user_prompt)]),
                ]
                config = types.GenerateContentConfig(
                    http_options=_resilience.provider_http_options("instruction"),  # WP-C B-C4
                    cached_content=cache_name,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=logic_schema,
                    safety_settings=_constants.TRPG_SAFETY_SETTINGS,
                )
            else:
                # ── 폴백: 캐시 없음 / 모델 불일치 ──
                logic_contents = [
                    types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])
                ]
                config = types.GenerateContentConfig(
                    http_options=_resilience.provider_http_options("instruction"),  # WP-C B-C4
                    system_instruction=GM_LOGIC_SYSTEM_INSTRUCTION,
                    temperature=0.4,
                    response_mime_type="application/json",
                    response_schema=logic_schema,
                    safety_settings=_constants.TRPG_SAFETY_SETTINGS,
                )

            # 재시도·타임아웃 보호. 층위 호출은 모두 이 관문을 통과한다.
            _cl_op = _cost_ledger.begin_operation(
                self.bot, _cost_ledger.OP_TURN_INSTRUCTION, session=session,
                model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
                billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
            # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
            _turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=_cache.DEFAULT_MODEL,
                    contents=logic_contents,
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] Logic 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 자동 지시층위 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)

            breakdown = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            _estimate.update_stats(session, "instruction", out_tokens, thought_tokens)
            # 캐시 읽기 실측 — 캐시에 구워진 지시문까지 포함된 실제 값.
            if cached_tokens > getattr(session, "cache_read_tokens", 0):
                session.cache_read_tokens = cached_tokens
            # 예측 대조 — 신선 입력(In - Cached)으로 문자→토큰 계수를 자동 보정한다.
            _estimate.record_actual_input(session, "instruction", in_tokens - cached_tokens)
            self.rt.accrue(session, cost)
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}지시층위 호출",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            print(
                f"[GM/{session.session_id}] Logic 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {_cost.format_cost(cost)} (누적 {_cost.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "지시층위", "cost": cost,
                "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                "manifest": list(getattr(session, "gm_last_logic_manifest", [])), "layer": "instruction",
                "census": _prompt_census.record(
                    session, "instruction", logic_contents, system_text=_sys_text(config),
                    in_tokens=in_tokens, cached_tokens=cached_tokens),
            })
        except Exception as e:
            print(f"[GM] Logic 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            decision = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                decision = json.loads(cleaned)
            except Exception as e:
                _logs.event("turn", "지시층위 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "DIRECTOR_JSON", "raw": raw_text})
                if master_ch:
                    await master_ch.send("⚠️ 자동 지시층위 응답이 JSON 형식이 아닙니다. 이번 발언 스킵.")
                return None

        if _tie_schema is not None and _wt is not None:
            _tc = decision.get("tie_order")
            _wt.tie_choice = _tc if isinstance(_tc, str) else None      # 검증은 resolve_plan_tie(허용 키만)

        if _id_manifests and _wt is not None:
            # 이 시도의 WorldTurn에 누적 — process_observation이 저작 얼굴·이름 InfoId 획득으로 합류시킨다(커밋 후 공개)
            for _kind, _m in _id_manifests.items():
                _hit = self.rt.speaker_card.parse_identified(_m, decision.get(self.rt.speaker_card.identity_field(_kind)))
                if _hit:
                    _attr = "identified_faces" if _kind == "face" else "identified_names"
                    setattr(_wt, _attr, tuple(sorted(set(getattr(_wt, _attr)) | set(_hit))))
        if _wt is not None:
            try:
                self.rt.speaker_card.prewarm(session, _wt)      # v7.37: 묘사 전에 장면 카드를 뒤에서 그려 둔다
            except Exception as e:  # noqa: BLE001
                print(f"[카드] 미리 그리기 실패(무시): {e}")

        # WP-B: 지시층위 효과(quest_case/quest_choice/info_access)는 여기서 직접
        #       canonical에 적용하지 않는다. 호출부 루프가 이 decision을 스테이징
        #       (canonical 미변경)하고, 묘사가 성립한 뒤 단일 호환 적용 경계에서만
        #       반영한다. AUD-024(묘사 실패 시 지시효과 누출) 차단 +
        #       AUD-005(중복 quest 적용 경로) 단일 owner 통합.
        return decision

    async def _execute_rolls(self, session, rolls: list, game_ch,
                             *, transaction_id: str | None = None) -> list[str]:
        """rolls 목록을 굴리고 결과를 게임·마스터 채널에 선언.

        WP-C: 주사위 판정 결과 자체는 플레이어에게 공개된 판정 입력 사실로 그대로
        보존한다. 그러나 그 결과가 유발하는 성장 mutation(players.profile +1,
        stat_fail_counts)은 턴 소유 정본 변경이므로 트랜잭션 준비 객체에 스테이징하고
        READY 이후 legacy continuation에서만 적용한다. 같은 tx의 후속 ROLL은 스테이징
        투영을 읽는다. 사전 READY 실패 시 성장은 폐기된다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        results: list[str] = []
        _prep = None
        roll_session = session
        if transaction_id is not None:
            _prep = _turn_preparation.ensure_preparation(session, transaction_id)
            if _prep is not None:
                roll_session = _turn_preparation.growth_projection(session, _prep)
            else:
                # stale 재개 — 정본을 절대 바꾸지 않는 일회용 투영(성장 폐기).
                roll_session = _turn_preparation.PreparationView(session, overrides={
                    "players": copy.deepcopy(getattr(session, "players", {}) or {}),
                    "stat_fail_counts": copy.deepcopy(
                        getattr(session, "stat_fail_counts", None) or {}),
                })

        wt = self._world_turn(session, transaction_id)
        records: list[dict] = []
        effects = {"growth_events": [], "fail_counts": {}}

        def _roll_side(side):
            d1, d2 = _judgment.roll_2d50(random)
            side["dice"] = [d1, d2]
            side["R"] = d1 + d2
            side["success"] = _judgment.is_success(side["R"], side["T"])
            side["margin"] = side["T"] - side["R"]

        def _game_line(side):
            res = "성공 🟢" if side["success"] else "실패 🔴"
            if not side["is_pc"]:
                # NPC 능력치·목표치는 비공개 — 플레이어에게는 주사위와 성패만
                return f"> 🎲 [{side['name']}] 판정 — 2d50 **{side['R']}** → **{res}**"
            return (f"> 🎲 [{side['name']}] **{side['stat']}** 판정 — 2d50 **{side['R']}** "
                    f"({side['dice'][0]}+{side['dice'][1]}) / 목표 {side['T']} ({side['breakdown']})"
                    f"  →  **{res}** (여유 {side['margin']:+d})")

        def _logic_text(side):
            res = "성공" if side["success"] else "실패"
            basis = "; ".join(b for b in (
                f"행동 근거: {side['method_basis']}" if side["method_basis"] else "",
                f"상태 근거: {side['status_basis']}" if side["status_basis"] else "") if b)
            return (f"{side['name']} {side['stat']} 판정(2d50): R={side['R']}, 목표 T={side['breakdown']}, "
                    f"{res}(여유 {side['margin']:+d})" + (f" [{basis}]" if basis else ""))

        for r in rolls:
            plan = _judge_plan(roll_session, r, wt)
            kind, act, opp = plan["kind"], plan["actor"], plan["opponent"]
            char_name, stat_name = act["name"], act["stat"]
            game_lines: list[str] = []
            logic_lines: list[str] = []

            if act["T"] is None:
                # 능력치를 확인할 수 없다(등급제·결측·장면 밖 NPC) — 허구 수치로 굴리지 않는다.
                game_lines.append(f"> 🎲 [{char_name}] {stat_name} 판정 — 능력치를 확인할 수 없어 판정을 보류합니다.")
                logic_lines.append(f"- {char_name} {stat_name} 판정 불가(능력치 미확인) — 코드 성패 없음, "
                                   f"정본 상황에 따라 서술 재량")
            else:
                _roll_side(act)
                game_lines.append(_game_line(act))
                logic_lines.append("- " + _logic_text(act))
                if kind == "CRAFT":
                    logic_lines.append(
                        f"- 제작 판정: 성공 여유 {act['margin']:+d} — 성공품은 여유가 클수록 마감·완성도가 높게 서술한다"
                        f"(품질 단계·수치 속성은 정하지 않는다). 실패해도 재료 손실을 자동으로 가정하지 말 것.")
                if kind == "CONTEST":
                    if opp is None or opp["T"] is None:
                        oname = opp["name"] if opp else "상대"
                        logic_lines.append(f"- 대결 상대 {oname}의 판정 능력치를 확인할 수 없음 — 상대 판정 없음. "
                                           f"우열은 코드가 정하지 않았다(정본 상황에 따라 서술).")
                    else:
                        _roll_side(opp)
                        game_lines.append(_game_line(opp))
                        logic_lines.append("- " + _logic_text(opp))
                        outcome = _judgment.contest_outcome(act["margin"], opp["margin"])
                        opp["contest"] = act["contest"] = outcome
                        if outcome in ("A", "B"):
                            win = act if outcome == "A" else opp
                            verdict = f"{win['name']} 우세"
                        elif outcome == "TIE":
                            verdict = "교착 — 같은 여유, 우열 미결"
                        else:
                            verdict = "쌍방 실패 — 승자 없음(상황 유지·쌍방 실패로 서술)"
                        game_lines.append(f"> ⚔️ 대결 결과: **{verdict}**")
                        logic_lines.append(
                            f"- 대결 결과: {verdict} (여유 {act['margin']:+d} 대 {opp['margin']:+d}). "
                            f"격차는 연출 근거일 뿐 피해·보상·제압 수치를 새로 만들지 말 것.")

            if game_ch:
                for gl in game_lines:
                    await self.rt.send_streamed(self.bot, game_ch, gl)
                if act["T"] is not None and act["is_pc"]:
                    await self.rt.send_streamed(
                        self.bot, game_ch,
                        "> ✅ " + _master_mod.say("turn.roll_success", f"**{stat_name}** 판정이 성공했습니다.")
                        if act["success"] else
                        "> ❌ " + _master_mod.say("turn.roll_failure", f"**{stat_name}** 판정이 실패했습니다."))
                # 판정 줄은 send_streamed가 게임 채널 기록([GM])에 남긴다 — 별도 기록하지 않는다(중복 방지).
            # 주사위 효과음 — 토글이 켜져 있을 때만.
            try:
                if self.rt.is_enabled(session, "sfx"):
                    vc = getattr(session, "voice_client", None)
                    if vc and vc.is_connected():
                        await self.rt.play_dice_sfx(self.bot, getattr(game_ch, "guild", None))
            except Exception as e:
                print(f"[효과음] 재생 실패: {e}")

            if master_ch:
                # 운영자는 NPC 목표치까지 전부 본다
                await master_ch.send("🤖 [GM 굴림]\n" + "\n".join(logic_lines))
            for ll in logic_lines:
                session.current_turn_logs.append(ll.lstrip("- "))
            _rec = {"kind": kind, "actor": copy.deepcopy(act), "opponent": copy.deepcopy(opp)}
            if r.get("discovery") is True:
                # v7.14: 발견 성공 = 이 굴림의 성패만(다른 판정의 성공을 전용하지 않는다). 실패는 '이번에 찾지 못함'.
                _g = (self._tx_judgment(session, transaction_id) or {}).get("gate")
                if _gate_rolls_discovery(_g):
                    _ok = act["T"] is not None and bool(act["success"])
                    _rec["discovery"] = {"target": _g["target"], "kind": _g["kind"], "place": _g["place"],
                                         "success": _ok}
                    _dl = (f"- 주변 발견 탐색 '{_g['target']}': " + (
                        "찾아냈다." if _ok else "이번 탐색에서는 찾지 못했다(없다고 단정하지 말 것)."))
                    logic_lines.append(_dl)
                    session.current_turn_logs.append(_dl.lstrip("- "))
            results.extend(logic_lines)
            records.append(_rec)

            # 통계 — 굴린 주사위 수
            try:
                for _uid in (session.players or {}):
                    await _stats.bump(_uid, dice_rolled=1)
            except Exception:
                pass

            # ── 성장 · 행운 판정 (기획 규정) ──
            # PC 본인 판정의 성패만 넘긴다. 성장 재판정은 2d50과 독립인 d20 원척도 비교다(_growth).
            if act["T"] is not None and act["is_pc"] and stat_name:
                uid = act["uid"]
                try:
                    outcome = self.rt.process_roll_outcome(
                        roll_session, char_name, stat_name,
                        failed=not act["success"], sides=_growth.GROWTH_DIE,
                    )
                    _fk = f"{char_name}:{stat_name}"
                    if _fk in (getattr(roll_session, "stat_fail_counts", None) or {}):
                        effects["fail_counts"][_fk] = roll_session.stat_fail_counts[_fk]
                    growth = outcome["growth"]
                    if growth:
                        g_line = _growth.format_growth(char_name, stat_name, growth)
                        # WP-F: '성장!'은 정본 변이(스테이징) 알림이다 — 자동 턴에서는 커밋된
                        #   성장 사실에서만(derived, durable COMMITTED 이후) 게임 채널에 나간다.
                        #   실패·폐기 시도에 거짓 영구 알림을 남기지 않는다. 판정 사실(아직/상한)은
                        #   정본 변이가 아니므로 즉시 보인다. 수동 경로(transaction_id 없음)는 기존대로.
                        if growth.get("grew") and transaction_id is not None:
                            if _prep is not None:
                                _prep.growth_notices.append(g_line)
                        elif game_ch:
                            await self.rt.send_streamed(self.bot, game_ch, g_line)
                        if master_ch:
                            await master_ch.send(g_line)
                        if growth.get("grew"):
                            if _prep is not None and uid:
                                _prep.growth_events.append(
                                    (uid, stat_name, growth["new_value"]))
                                effects["growth_events"].append([uid, stat_name, growth["new_value"]])
                            results.append(
                                f"- {char_name} {stat_name} 성장: "
                                f"{growth['new_value']}로 상승"
                            )
                    luck = outcome["luck"]
                    if luck:
                        l_line = _growth.format_luck(char_name, luck)
                        if game_ch:
                            await self.rt.send_streamed(self.bot, game_ch, l_line)
                        if master_ch:
                            await master_ch.send(l_line)
                        # 지시층위에 행운 발생을 전달한다. 서사 제약은
                        # 프롬프트가 담당한다(행운은 상황에서 발생 가능한
                        # 범위여야 하며 구도를 뒤틀어서는 안 된다).
                        results.append(
                            f"- {char_name} 행운 발생: 이번 실패를 "
                            f"현재 상황에서 자연스럽게 발생 가능한 행운으로 완충할 것"
                        )
                except Exception as e:
                    print(f"[성장·행운] 판정 실패(진행에는 영향 없음): {e}")

        # v7.51(기능 party · D-P1 가): 전투 턴이면 파티원이 역할대로 움직인다(명령받은 파티원은 그 굴림이 대신한다)
        try:
            p_records, p_game, p_logic = _party_round(roll_session, wt, records, _roll_side, _game_line)
        except Exception as e:  # noqa: BLE001 — 파티 행동 실패는 PC 판정을 막지 않는다
            print(f"[파티] 역할 행동 실패: {e}")
            p_records, p_game, p_logic = [], [], []
        if p_records:
            if game_ch:
                for gl in p_game:
                    await self.rt.send_streamed(self.bot, game_ch, gl)
            if master_ch and p_logic:
                await master_ch.send("🤖 [파티 행동]\n" + "\n".join(p_logic))
            for ll in p_logic:
                session.current_turn_logs.append(ll.lstrip("- "))
            results.extend(p_logic)
            records.extend(p_records)

        if _prep is not None:
            # 재서술이 다시 굴리지 않고 같은 판정·성장 효과를 재사용하도록 판단 기록에 남긴다.
            _turn_history.note_rolls(session, transaction_id, records, effects)
        return results

    async def _dispatch_rolls(self, session, rolls: list, player_message: str, prior_roll_results: list,
                              *, transaction_id: str | None = None):
        """ROLL 결정 시 플레이어에게 버튼 UI 전송."""
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        target_uid = _utils.get_uid_by_char_name(session, session.gm_target_char)

        roll_descs = []
        wt = self._world_turn(session, transaction_id)
        # 미리보기는 실행과 같은 조립(_judge_plan)을 쓴다 — 같은 tx의 스테이징 성장 투영 기준.
        view = _turn_preparation.PreparationView(
            session, overrides={"players": _turn_preparation.projected_players(session)})
        for r in rolls:
            plan = _judge_plan(view, r, wt)
            act, opp = plan["actor"], plan["opponent"]
            label = {"CRAFT": "제작 ", "CONTEST": "대결 "}.get(plan["kind"], "")
            if act["T"] is None:
                desc = f"{label}**{act['stat']}** 판정 (능력치 미확인)"
            elif act["is_pc"]:
                desc = (f"{label}**{act['stat']}** 판정 — 목표 {act['T']} ({act['breakdown']}, "
                        f"성공률 {_judgment.success_chance(act['T']):.0%})")
            else:
                desc = f"{label}[{act['name']}] 판정"
            if opp is not None:
                desc += f" vs {opp['name']}"
            roll_descs.append(desc)

        desc_text = " / ".join(roll_descs)
        mention = f"<@{target_uid}>" if target_uid else "플레이어"

        view = self.rt.GMRollView(
            cog=self,
            session=session,
            roll_specs=rolls,
            player_message=player_message,
            prior_roll_results=prior_roll_results,
            target_uid=target_uid,
            transaction_id=transaction_id,
        )

        roll_prompt_text = (
            f"> 🎲 {mention}, " + _master_mod.say("turn.roll_request", "판정이 필요합니다!") + "\n"
            f"> {desc_text}\n"
            f"> 2d50 합이 목표보다 작으면 성공합니다. 아래 버튼을 눌러 주사위를 굴리세요. (5분 내 미클릭 시 자동 굴림)"
        )
        if game_ch:
            view.message = await game_ch.send(roll_prompt_text, view=view)
            self.rt.write_log(session.session_id, "game_chat", f"[판정 요청]: {desc_text}")
            _set_roll_open(session, transaction_id, True)
        if master_ch:
            await master_ch.send(
                f"🤖 **[GM ROLL]** 플레이어 버튼 대기 중...\n> {desc_text}"
            )

    async def _continue_with_roll_results(self, session, player_message: str, roll_results: list,
                                          *, transaction_id: str | None = None):
        """GMRollView 버튼 클릭 후 굴림 결과를 반영하여 지시층위 재호출."""
        master_ch = self.bot.get_channel(session.master_ch_id)

        async def m_send(content, **kw):
            if master_ch:
                return await master_ch.send(content, **kw)
            return None

        async with self._lock_for(session):
            if not session.gm_active:
                return

            # WP-01: stale 재개 가드. View는 async 경계 너머에서 재개되므로, 그 사이
            #        새 시도가 생겼다면(플레이어 재요청 등) 낡은 ROLL 콜백은 옛 트랜잭션을
            #        되살리지 않고 조용히 종료한다. ID가 없으면(레거시/수동) 통과시킨다.
            if transaction_id is not None and not _turn_transaction.is_current_transaction(session, transaction_id):
                print(f"[TURN] stale roll continuation tx={str(transaction_id)[:8]} 무시(현재 트랜잭션 아님)")
                return

            if self.rt.auto_cost_cap_reached(self.bot, session):
                session.gm_active = False
                await m_send(
                    f"🛑 **[GM 자동 정지]** 자동 모드 누적 비용 한도 도달."
                )
                await self.rt.save_session_data(self.bot, session)
                return

            try:
                # WP-E: 굴림 결과까지 포함한 지시층위 입력을 판단 기록에 반영(재생성 보존 대상).
                _turn_history.note_judgment(
                    session, transaction_id, player_message=player_message,
                    roll_results=list(roll_results))
                _tx = _turn_transaction.get_active_transaction(session)
                if _tx is not None and _tx.transaction_id == transaction_id:
                    _tx.pending_roll = None                       # 이어서 진행 — 보관 결과는 이번 호출이 소유
                decision = await self._call_gm_logic(
                    session, player_message, roll_results, master_ch,
                    transaction_id=transaction_id)
                if not decision:
                    # v7.33(F5 — 사용자 결정 '재사용'): 굴린 결과를 시도에 보관하고 [이어서 진행]으로 같은 결과를 다시 쓴다
                    #   (재선언·재굴림 없음 — 판정이 끝날 때까지 새 선언은 _roll_pending_blocks가 막는다).
                    await m_send("⚠️ GM 결정 호출 실패 — 판정 결과를 보관했습니다. 플레이어의 [이어서 진행]을 기다립니다.")
                    await self._hold_roll_results(session, player_message, roll_results, transaction_id)
                    return

                action = decision.get("action", "PROCEED").upper()
                reasoning = decision.get("reasoning", "")
                # WP-C(B-C2): 굴림 후 지시층위 결정의 지시효과를 기존 단일 owner로 스테이징한다
                #   (유실 금지). 정본 적용은 READY 이후 legacy continuation에서만.
                _turn_preparation.stage_instruction_effects(
                    session, {**decision, "action": action}, transaction_id=transaction_id)
                action_labels = {
                    "ASK":     "🟡 ASK (명확화 요청)",
                    "ROLL":    "🎲 ROLL (주사위 판정)",
                    "PROCEED": "🟢 PROCEED (턴 진행)",
                }
                _logs.event("turn", f"굴림 후 판단 {action}", session=session,
                                data={"stage": "post_roll", "action": action, "reasoning": reasoning})
                if action not in ("PROCEED", "ROLL"):          # v7.35: PROCEED는 층위활동 보고(🧭)로
                    await m_send(f"🤖 **[GM 판단 (굴림 후)]** {action_labels.get(action, action)} — {reasoning[:120]}")

                instruction = _clean_proceed_instruction(
                    decision.get("proceed_instruction") or
                    "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
                )
                if not instruction:
                    instruction = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."

                await self._finish_proceed_and_continue(
                    session, instruction, master_ch,
                    event_assessment=decision.get("event_assessment", "ongoing"),
                    transaction_id=transaction_id)
            except Exception as e:  # noqa: BLE001 — v7.33(F1): 굴림 재개 태스크 밖으로 새지 않게
                await self._abort_turn_before_narration(
                    session, transaction_id, player_message, master_ch,
                    code=_turn_transaction.FailureCode.UNEXPECTED_EXCEPTION, stage="ROLL_CONTINUE", exc=e)

    async def _hold_roll_results(self, session, player_message, roll_results, transaction_id) -> None:
        """굴림 뒤 지시층위 실패 — 결과를 시도에 보관하고 게임 채널에 [이어서 진행] 버튼(재시작하면 사라짐 → 재선언 안내)."""
        tx = _turn_transaction.get_active_transaction(session)
        if transaction_id is None or tx is None or tx.transaction_id != transaction_id:
            return
        tx.pending_roll = {"player_message": player_message, "roll_results": list(roll_results)}
        _turn_transaction.mark_waiting_for_roll(session, transaction_id)
        game_ch = self.bot.get_channel(session.game_ch_id)
        if game_ch:
            closed = "" if self.rt.is_session_open(session) else " 세션이 닫혀 있으니 세션을 연 뒤 눌러 주십시오."
            try:
                await game_ch.send("⚠️ 판정 뒤 진행을 준비하지 못했습니다. 판정 결과는 그대로 보관했습니다."
                                   + closed + "\n> 아래 버튼을 누르면 같은 결과로 이어서 진행합니다.",
                                   view=self.rt.RollContinueView(self, session, transaction_id))
            except Exception as e:  # noqa: BLE001
                print(f"[판정] 이어서 진행 안내 실패: {e}")

    async def _dispatch_narrate(self, session, narrate_instruction: str,
                                *, transaction_id: str | None = None) -> str | None:
        """
        캐시 기반 경량 LLM 호출로 짧은 GM 응답(NARRATE)을 생성하고 게임 채널에 스트리밍.

        NOTE: PROCEED의 풀 프롬프트 대신 최근 로그 + narrate_instruction만 전달하여
        약 300자 이내의 빠른 응답을 생성. 캐시 히트 시 비용은 PROCEED의 절반 이하.
        대사 마커(@대사:이름|본문)를 감지하여 인물 이미지·말풍선 포맷으로 자동 변환.

        Args:
            session: TRPGSession
            narrate_instruction (str): 지시층위가 생성한 경량 응답 지시문 (100자 이내)

        Returns:
            str | None: 생성된 NARRATE 응답 텍스트 (스트리밍 완료 후). 실패 시 None.
        """
        # WP-01: 현재 트랜잭션이면 NARRATING으로 표기(수동/인트로 경로는 no-op).
        _turn_transaction.mark_transaction_status(
            session, transaction_id, _turn_transaction.TurnStatus.NARRATING)
        master_ch = self.bot.get_channel(session.master_ch_id)
        game_ch = self.bot.get_channel(session.game_ch_id)

        # 최근 raw_logs 4개 (턴 개수 유지, 각 턴은 온전 원문)
        recent_parts = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                recent_parts.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_parts) if recent_parts else "(최근 로그 없음)"

        # 이번 턴 현재까지 누적된 대화
        current_turn_str = "\n".join(session.current_turn_logs) if session.current_turn_logs else "(없음)"

        narrate_prompt = build_narrate_prompt(recent_str, current_turn_str, narrate_instruction)
        _nwt = self._world_turn(session, transaction_id)
        if _nwt is not None:
            # P3(G1): 지시층위와 같은 비진행 경계 — 정본 시각·장소에서 짧게 답한다(세계 진행 없음)
            narrate_prompt += "\n" + _turn_world.narrate_hold_block(_nwt, session)
            # v7.25(F02/A04): 계획 없는 응답도 정본 장면 인물을 받는다(없는 인물을 지어내지 않게)
            narrate_prompt += _turn_world.scene_npc_block(_nwt.ctx, None, session)


        # NOTE: max_output_tokens를 설정하지 않음 — PROCEED(_execute_proceed)와 동일한 방침.
        # DEFAULT_MODEL(gemini-3-flash-preview)은 thinking 모델이므로, max_output_tokens를
        # 지정하면 내부 thinking 토큰까지 한도에 포함되어 실제 텍스트 출력이 거의 없는
        # MAX_TOKENS 조기 종료가 발생한다. 출력 길이는 프롬프트의 "300자 이내" 지시로 제어한다.
        # WP-02: game_ch/else 두 분기는 상호배타이며 동일 논리 오퍼레이션이다.
        #        하나의 operation_id를 공유하고 양쪽 호출을 관측한다.
        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_TURN_LIGHT_NARRATE, session=session,
            model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
        # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
        _turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
        try:
            if _turn_history.cache_usable(session):     # WP-E(E-E3)
                config = types.GenerateContentConfig(
                    http_options=_resilience.provider_http_options("narration"),  # WP-C B-C4
                    cached_content=session.cache_name,
                    temperature=0.65,
                    safety_settings=_constants.TRPG_SAFETY_SETTINGS,
                )
            else:
                config = types.GenerateContentConfig(
                    http_options=_resilience.provider_http_options("narration"),  # WP-C B-C4
                    system_instruction=self.bot.system_instruction,
                    temperature=0.65,
                    safety_settings=_constants.TRPG_SAFETY_SETTINGS,
                )

            # PROCEED와 동일한 구조: typing()은 API 호출만 감싸고,
            # 출력(stream_text_to_channel)은 typing 컨텍스트 밖에서 실행한다.
            if game_ch:
                async with game_ch.typing():
                    _ok, response = await self.rt.call_with_retry(
                        lambda: asyncio.to_thread(
                            self.bot.genai_client.models.generate_content,
                            model=_cache.DEFAULT_MODEL,
                            contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                            config=config,
                        ),
                        layer="narration", session_id=session.session_id,
                        on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
                    )
                    if not _ok:
                        raise RuntimeError("AI 호출 실패")
            else:
                _ok, response = await self.rt.call_with_retry(
                    lambda: asyncio.to_thread(
                        self.bot.genai_client.models.generate_content,
                        model=_cache.DEFAULT_MODEL,
                        contents=[types.Content(role="user", parts=[types.Part.from_text(text=narrate_prompt)])],
                        config=config,
                    ),
                    layer="narration", session_id=session.session_id,
                    on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
                )
                if not _ok:
                    raise RuntimeError("AI 호출 실패")
        except Exception as e:
            _logs.event("turn", "NARRATE 호출 실패", level="ERROR", session=session, exc=e,
                            data={"code": "NARRATE_CALL"})
            if master_ch:
                await master_ch.send(f"⚠️ GM NARRATE 호출 실패: {type(e).__name__}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)

            breakdown = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}NARRATE 경량 응답",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )

            print(
                f"[GM/{session.session_id}] NARRATE 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {_cost.format_cost(cost)} (누적 {_cost.format_cost(session.total_cost)})"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "묘사층위(NARRATE)", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
        except Exception as e:
            print(f"[GM] NARRATE 비용 정산 실패: {e}")

        narrate_text = (response.text or "").strip()
        if not narrate_text:
            return None


        # 게임 채널에 스트리밍 출력 (대사 마커 처리 포함)
        # NOTE: PROCEED(_execute_proceed)와 동일한 구조 — typing 컨텍스트 밖에서 stream_text_to_channel 호출.
        # typing은 위 API 호출 블록에서만 활성화되며, 이 시점에서는 이미 종료된 상태.
        if game_ch:
            paragraphs = [p.strip() for p in narrate_text.split("\n\n") if p.strip()]
            paragraphs = self.rt.merge_consecutive_dialogues(paragraphs)

            for paragraph in paragraphs:
                dialogue = self.rt.parse_dialogue_paragraph(paragraph)
                if dialogue:
                    speaker, content = dialogue
                    _dctx = self.rt.speaker_card.display_context(session, speaker)
                    if _dctx is None:
                        await self.rt.maybe_send_speaker_image(game_ch, session, speaker)
                        formatted = self.rt.format_dialogue_block(speaker, content)
                    else:
                        # SVG 카드(DYNAMIC): NARRATE는 이미지를 내지 않고 헤더만 커밋된 이름 인지로 표시한다
                        formatted = self.rt.format_dialogue_block(_dctx.header, content)
                    await self.rt.stream_text_to_channel(
                        self.bot, game_ch, formatted,
                        words_per_tick=15, tick_interval=1.5, quote_prefix=False
                    )
                else:
                    await self.rt.stream_text_to_channel(
                        self.bot, game_ch, paragraph,
                        words_per_tick=15, tick_interval=1.5
                    )
            # P2 Q7: NARRATE 본문도 더빙 대상(대사는 NPC 목소리, 그 밖은 진행자 내레이션 목소리)
            await self._voice_lines(session, paragraphs, transaction_id=transaction_id, kind="paragraphs")

        # current_turn_logs에 추가 — PROCEED 시 AI가 맥락을 볼 수 있도록
        session.current_turn_logs.append(f"[진행자 (GM)]: {narrate_text}")
        return narrate_text

    async def _dispatch_proceed(self, session, instruction: str,
                                *, transaction_id: str | None = None,
                                preparation=None):
        """GameCog._execute_proceed를 호출하여 묘사 생성·연출(자동 경로 전용).

        WP-C: 확정 묘사 직후 on_finalized 훅이 동시 준비 작업을 발사하고, 전달은 그와
        겹쳐 진행된다. 이 함수는 어떤 턴 소유 정본도 적용하지 않는다 — 진행 이력·
        progress·로그/카운터는 준비 객체에 스테이징되고 READY 이후에 적용된다.
        추출은 여기서 await하지 않는다(배리어 owner가 합류).

        Returns:
            성공 시 결과 dict, 묘사 생성 실패 시 None.
        """
        if preparation is None:
            preparation = _turn_preparation.ensure_preparation(session, transaction_id)
        game_cog = self.bot.get_cog("GameCog")
        if not game_cog:
            master_ch = self.bot.get_channel(session.master_ch_id)
            if master_ch:
                await master_ch.send("⚠️ GameCog를 찾을 수 없어 자동 진행을 중단합니다.")
            return None

        master_ch = self.bot.get_channel(session.master_ch_id)
        # v7.35: 지시문 원문은 마스터 채널에 따로 보내지 않는다 — 층위활동 보고(🎬 분량)·!세션이력 호출 원문으로

        # NOTE: PROCEED 직전에 이번 턴 컨텍스트(NARRATE/ASK/ROLL 중간 기록)와 지시사항을 스냅샷.
        context_snapshot = list(session.current_turn_logs)

        # ③ 턴 연속성 강화(엔진 병행): 묘사 AI가 직전 턴을 되감지 않도록 지시에 연속성 directive 부착.
        exec_instruction = instruction + (
            "\n\n[연속성 지시] 직전 턴이 끝난 시점의 시간·공간·상태에서 곧바로 이어서 묘사할 것. "
            "이미 완료되었거나 서술된 행동·장면 전환·이동을 되풀이하거나 시간을 되감지 말고, "
            "지나간 장면에 인물의 행동·대사를 소급 삽입하지 말 것."
        )

        # H05 R3·정합: 묘사층위의 세계 제약(확정 결과·장소 고정·장면 인물)은 지시문에 붙이지 않고
        #   GameCog 묘사 프롬프트의 독립 블록(turn_world.narration_world_block)으로 준다.

        async def _on_finalized(narr):
            if preparation is not None:
                await self._launch_concurrent_preparation(session, preparation, narr, master_ch)

        result = await game_cog._execute_proceed(
            session, exec_instruction, master_guild=None, cost_log_prefix=COST_LOG_PREFIX,
            transaction_id=transaction_id, preparation=preparation,
            on_finalized=_on_finalized,
        )

        # ── 묘사층위 실패 감지 (설계문서 6) ──
        # 확정 묘사가 없으면 묘사가 생성되지 않은 것이다. 묘사는 플레이어에게 직접
        # 노출되는 산출물이므로 실패 시 턴을 취소하고 선언 질문으로 재시작한다(기획 규정).
        narr = getattr(preparation, "narration", None) if preparation is not None else None
        full_text = getattr(narr, "text", "") or ""
        if not full_text:
            _logs.event("turn", "묘사층위 출력 없음 — 턴 취소", level="ERROR", session=session,
                            data={"code": "NARRATION_EMPTY"})
            _resilience.write_error_log(session.session_id, "narration",
                                 RuntimeError("묘사 출력 없음"), 1)
            game_ch = self.bot.get_channel(session.game_ch_id)
            last_decl = ""
            try:
                for c in reversed(session.raw_logs):
                    if getattr(c, "role", None) == "user":
                        last_decl = c.parts[0].text or ""
                        break
            except Exception:
                pass
            if game_ch:
                await self.rt.message_lifecycle.send_transient(
                    self.bot, session, game_ch, _resilience.build_failed_turn_notice(last_decl),
                    key=self.rt.message_lifecycle.KEY_TURN_NOTICE)
            if master_ch:
                await master_ch.send("⚠️ 묘사층위 실패 — 턴을 취소하고 선언 질문으로 되돌립니다.")
            # 실패 턴의 잔재 제거. 플레이 로그에는 정상 선언 질문처럼 남는다.
            session.current_turn_logs = []
            session.gm_side_note = ""
            return None

        # PROCEED 완료 후 AI 출력(이력용). v7.25(CUT): 지시층위 이력에 다시 들어가므로 문자 수로 자르지 않는다.
        ai_summary = full_text

        # 이력 누적(최근 5개) · [방안 2] narrative_plan.current_event.progress 갱신
        #   — WP-C: 둘 다 턴 소유 서사 상태이므로 스테이징하고 READY 이후 적용한다.
        preparation.proceed_history_entry = {
            "instruction": instruction,
            "context": context_snapshot,
            "ai_summary": ai_summary,
        }
        preparation.narrative_progress = ai_summary
        return result

    async def _handle_open_time_input(self, session, message):
        """
        디스플레이 채널의 유지 시간 답변을 처리한다.

        기획 규정 — 모든 답변은 자체 재질문이나 유저의 재입력 선택이 아닌 경우에
        한 번만 하도록 즉시 채팅을 잠그고 UI로 진행한다.
        """
        try:
            await message.delete()
        except Exception:
            pass

        # v7.37(R1): 세션 첫 생성 대화 안이면 해석·재질문·확인을 같은 대화 메시지에 이어 쓴다
        flow = self.rt.session_flow.open_in_flow(session, message.channel.id)
        if flow:
            echo = f"-# ↳ {getattr(message.author, 'display_name', None) or '플레이어'} · {message.content}\n"
            await self.rt.session_flow.open_feedback(self.bot, session, message.channel,
                                                  echo + "말씀하신 시간을 헤아리고 있습니다…")
            resolved = await self.interpret_cache_time(session, message.content)
            await self.rt.message_lifecycle.clear(self.bot, session, self.rt.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT)
            if resolved.get("retry"):
                session.awaiting_display_input = True
                session._open_input_ch_id = message.channel.id
                await self.rt.session_flow.open_feedback(
                    self.bot, session, message.channel,
                    echo + _master_mod.say("cache.time_unclear", "말씀하신 시간을 정확히 헤아리지 못했습니다. "
                                                                  "다시 한 번 말씀해 주십시오.")
                    + "\n" + self.rt.session_flow.OPEN_EXAMPLES + _interp_note(resolved))
                return
            session.open_minutes = resolved["minutes"]
            try:
                est = _estimate.estimate_session_open(session, resolved["minutes"] / 60)
            except Exception:
                est = {}
            view = self.rt.OpenConfirmView(self.bot, session, resolved["minutes"],
                                   on_reopen=self.resume_held_declaration, flow=True)
            view.bind(await self.rt.session_flow.open_feedback(
                self.bot, session, message.channel,
                echo + _session_open.format_confirmation(resolved, est) + _interp_note(resolved), view))
            return

        thinking = await message.channel.send("⏱️ *입력을 확인하는 중…*")
        resolved = await self.interpret_cache_time(session, message.content)
        try:
            await thinking.delete()
        except Exception:
            pass

        # WP-F: 답변이 들어왔다 — 유지 시간 질문(INTERACTION_PROMPT)은 여기서 종결 정리한다.
        await self.rt.message_lifecycle.clear(self.bot, session,
                                           self.rt.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT)
        if resolved.get("retry"):
            # 재질문 — 이 경우에만 다시 언락한다(기획 규정). 재질문도 같은 키의 프롬프트다.
            session.awaiting_display_input = True
            self.rt.message_lifecycle.register(
                session, self.rt.message_lifecycle.KEY_DISPLAY_OPEN_PROMPT,
                await message.channel.send(
                    "❓ " + _master_mod.say("cache.time_unclear", "입력을 이해하지 못했습니다. 다시 답해 주십시오.") + "\n"
                    "> 예: `3시간`, `90분`, `20턴`, `적당히`, `알아서`"
                    + _interp_note(resolved)
                ), cls=self.rt.message_lifecycle.INTERACTION_PROMPT)
            return

        session.open_minutes = resolved["minutes"]
        try:
            est = _estimate.estimate_session_open(session, resolved["minutes"] / 60)
        except Exception:
            est = {}
        view = self.rt.OpenConfirmView(self.bot, session, resolved["minutes"],
                               on_reopen=self.resume_held_declaration)
        view.bind(await message.channel.send(
            _session_open.format_confirmation(resolved, est) + _interp_note(resolved), view=view))

    async def interpret_cache_time(self, session, text: str) -> dict:
        """
        세션 유지 시간 입력을 해석한다 (저비용 호출).

        모델은 분류만 하고 실제 시간은 _session_open.resolve_minutes가 정한다.
        해석 비용은 세션에 누적했다가 캐시 업로드 시점에 2잉크 이상일 때만
        청구한다(기획 규정).

        Returns:
            _session_open.resolve_minutes의 결과 dict
        """
        quick = _session_open.quick_parse(text)
        if quick is not None:                 # 숫자·단위가 분명한 답 — 코드가 읽는다(호출·해석 비용 0)
            resolved = _session_open.resolve_minutes(quick)
            resolved["interpret_charge"] = {"ink": 0, "complete": True}
            return resolved
        config = types.GenerateContentConfig(
            system_instruction=CACHE_TIME_SYSTEM_INSTRUCTION,
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=CACHE_TIME_RESPONSE_SCHEMA,
            safety_settings=_constants.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(
            role="user", parts=[types.Part.from_text(text=f"[플레이어 입력]\n{text or ''}")]
        )]

        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_CACHE_TIME_INTERPRET, session=session,
            model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE,
            copy_transaction=False)
        # WP-F(POLICY-CACHE-01): 이 provider 사실은 interpretation_billing 이 청구한다 —
        #   표식·interp_id 로 CostEvent ↔ 청구 레코드를 감사·재구성할 수 있게 한다.
        _cl_op.metadata.update({"interpretation_billing": _interpretation_billing.BILLING_MARK,
                                "interp_id": _cl_op.operation_id})
        ok, response = await self.rt.call_with_retry(
            lambda: asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=_cache.DEFAULT_MODEL, contents=contents, config=config,
            ),
            layer="media", session_id=session.session_id, retries=1,
            on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
        )
        if not ok:
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석에 실패했습니다."], "retry": True}

        # WP-F(POLICY-CACHE-01): provider 해석 사실 → durable 청구 후보 → (임계 이상이면) 즉시
        #   청구. 캐시 열기·취소·시간 만료·재시작과 무관하게 같은 결과다. 2잉크 미만 누적은
        #   기존대로 이어서 누적되고 세션을 열 때 면제된다.
        #   플레이어 청구는 durable provider 사실보다 앞설 수 없다 — 이 경로만 strict CostEvent
        #   (fsync, 멱등 replay 는 canonical identity)를 쓰고, 그 성공 뒤에만 청구 후보를 기록한다.
        interp_charge = {"ink": 0, "complete": True}
        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, _th = _cost.extract_token_usage(meta)
            _bd = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = _bd["total_krw"]
            _fact = _cl_op.record_fact_strict(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=_th,
                cost_usd=_bd["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
        except Exception as e:
            # strict provider 사실 없음 → 청구 0·청구 후보 없음·결과 미확정(재질문).
            #   실제 provider 비용은 시스템 영속 실패로 인한 운영 손실로 남는다.
            print(f"[시간해석] provider 비용 사실 strict 기록 실패 — 청구 없이 재질문: {e}")
            self.rt.write_log(session.session_id, "error",
                           f"[시간해석] strict CostEvent 실패(op={_cl_op.operation_id}): "
                           f"{type(e).__name__}: {e}")
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석 결과를 확정하지 못했습니다(비용 기록 실패)."], "retry": True,
                    "interpret_charge": {"ink": 0, "complete": True}}
        try:
            _interpretation_billing.record_interpretation(
                session, interp_id=_cl_op.operation_id, cost_krw=cost,
                cost_usd=_bd["total_usd"], cost_event_id=_fact.event_id)
        except Exception as e:
            print(f"[시간해석] 청구 후보 기록 실패(strict CostEvent 로 재구성·재시도): {e}")
        try:
            _st = await _interpretation_billing.settle(self.bot, session)
            interp_charge = {"ink": _st["charged_ink"], "complete": _st["complete"]}
        except Exception as e:
            interp_charge = {"ink": 0, "complete": False}
            print(f"[시간해석] 청구 처리 실패(다음 해석·재시작 시 재시도): {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "minutes": 0, "case": "unclear",
                    "notes": ["해석 결과를 읽지 못했습니다."], "retry": True,
                    "interpret_charge": interp_charge}

        resolved = _session_open.resolve_minutes(data)
        resolved["interpret_charge"] = interp_charge
        self.rt.write_log(
            session.session_id, "api",
            f"[유지시간 해석] 입력={text[:60]} → {json.dumps(data, ensure_ascii=False)} "
            f"→ {resolved['minutes']}분"
        )
        return resolved

    async def _generate_npc_detail(self, session, name: str, recent_text: str,
                                   master_ch=None, *, preparation=None,
                                   entry: dict | None = None) -> bool:
        """
        비정규 NPC 세부 설정 생성 — 비중이 생기거나 이름이 부여되면 1회 호출.

        생성 후 session.npcs로 승격되어 정규 델타 주입 대상이 된다.
        목소리와 이미지는 배정 당시의 것을 유지한다.

        WP-C: preparation이 주어지면(자동 턴 자식 준비 작업) 정규화 결과를
        preparation.promotions에 스테이징만 하고 정본 승격은 READY 이후에 한다.
        entry는 이번 턴 등장 누적이 반영된 투영 항목이다.
        """
        if entry is None:
            entry = _irregular_npc.get_registry(session).get(name)
        if not entry:
            return False

        # §38: provider-boundary 승격 오퍼레이션의 논리 시도 정체성을 호출 '전'에 고정.
        _tx_obj = _turn_transaction.get_active_transaction(session)

        year = _timeline.current_year(session)
        user_prompt = (
            f"[대상 인물]\n{name}\n\n"
            f"[등장 기록]\n"
            f"- 최초 등장: {entry.get('first_turn', 0)}턴\n"
            f"- 누적 등장: {entry.get('appearances', 1)}회\n"
            f"- 배정 인상: {entry.get('gender')}/{entry.get('age')}\n"
            f"- 등장 맥락: {entry.get('context') or '(없음)'}\n\n"
            f"[작중 현재 연도]\n{year if year is not None else '(미확인)'}\n\n"
            f"[최근 묘사문]\n{recent_text or ''}"
        )

        config = types.GenerateContentConfig(
            http_options=_resilience.provider_http_options("media"),  # WP-C B-C4
            system_instruction=NPC_DETAIL_SYSTEM_INSTRUCTION,
            temperature=0.5,
            response_mime_type="application/json",
            response_schema=NPC_DETAIL_RESPONSE_SCHEMA,
            safety_settings=_constants.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_TURN_NPC_PROFILE, session=session,
            model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        ok, response = await self.rt.call_with_retry(
            lambda: asyncio.to_thread(
                self.bot.genai_client.models.generate_content,
                model=_cache.DEFAULT_MODEL, contents=contents, config=config,
            ),
            layer="media", session_id=session.session_id, retries=1,
            on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
        )
        if not ok:
            return False

        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, thought_t = _cost.extract_token_usage(meta)
            breakdown = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=thought_t,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(
                session.session_id, f"{COST_LOG_PREFIX}NPC 설정 생성",
                in_t, cached_t, out_t, cost, session.total_cost
            )
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "NPC 설정 생성", "cost": cost,
                "in": in_t, "cached": cached_t, "out": out_t, "manifest": [],
            })
        except Exception as e:
            print(f"[NPC설정] 비용 정산 실패: {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            print("[NPC설정] 응답 파싱 실패")
            return False

        # result-only: raw payload를 promote에 직접 넣지 않고 정규화 후보로 만든다.
        norm = _turn_preparation.normalize_npc_detail(data, fallback_name=name)

        # stale guard(§38): 이 승격 결과보다 더 새로운 논리 시도가 활성화됐으면 승격 금지.
        if _turn_preparation.extraction_is_stale(
                session, logical_turn=getattr(_tx_obj, "logical_turn", None),
                attempt=getattr(_tx_obj, "attempt", None)):
            print(f"[NPC설정/{session.session_id}] stale 결과 거부 — 승격 보류")
            return False

        if preparation is not None:
            # WP-C: 스테이징만 — 정본 승격(mark_detailed/promote/개명)은 READY 이후.
            preparation.promotions[name] = norm
            return True
        return await self._apply_npc_promotion(session, name, norm, master_ch)

    async def _apply_npc_promotion(self, session, name, norm, master_ch=None,
                                   *, derived=None) -> bool:
        """LegacyCompatibilityApplier(NPC 승격) — 정규화된 세부설정만 소비한다.

        raw 모델 payload는 이 함수에 입력되지 않는다. mark_detailed/promote는 정규화된
        details만 받는다. promote는 멱등(이미 승격돼 등록부에 없으면 False)하다.
        """
        details = norm["details"]
        final_name = norm["final_name"]

        _irregular_npc.mark_detailed(session, name)
        promoted = _irregular_npc.promote(session, name, details)
        if promoted and final_name != name and not _player_world.legacy_narrative_retired(session):
            # v7.25(F06): 동적 세계는 개명하지 않는다 — 키 = 플레이어가 본 이름(정본 ActorRegistry 표시 이름과 같다).
            #   모델이 지은 본명은 PC가 알게 된 사실이 아니다.
            # 고유명이 확정되면 키를 교체한다.
            npcs = dict(session.npcs)
            npcs[final_name] = npcs.pop(name)
            session.npcs = npcs

        if master_ch or derived is not None:
            age_txt = ""
            if details.get("birth_year"):
                age = _timeline.compute_age(session, details["birth_year"])
                if age is not None:
                    age_txt = f" ({age}세)"
            _msg = (f"📇 **[NPC 승격]** {final_name}{age_txt} — "
                    f"{details['role']} · {details['attitude']}\n"
                    f"> {details['details'][:200]}")
            if derived is not None:
                derived.master(_msg)        # WP-D: COMMITTED 이후 방출
            else:
                await master_ch.send(_msg)
        return promoted

    async def _resolve_irregular_npcs(self, session, text: str, master_ch=None,
                                      *, preparation=None) -> int:
        """
        비정규 NPC 미디어 배정 — 묘사 스트리밍 '전'에 호출된다(기획 규정).

        [호출 생략 조건]
        이미지·TTS가 모두 꺼져 있으면 배정 결과를 쓸 곳이 없으므로 호출하지 않는다.
        이미지만 꺼져 있으면 목소리 결정을 위해 호출하되 image_key는 버린다.

        [동일인 유지]
        이전 배정 기록을 함께 전달해 같은 인물의 미디어가 세션 내내 유지되게 한다.

        WP-C: preparation이 주어지면(자동 턴 준비 작업) 검증된 등록 계획을
        preparation.irregular_plan에 스테이징하고 승격 세부 생성을 자식 작업으로
        등록한다. 정본(irregular_npcs/npcs)은 READY 이후에만 바뀐다. 스트리밍 중 화자
        이미지는 _irregular_npc의 읽기 전용 투영으로 이번 배정을 반영한다.

        Returns:
            새로 배정된(또는 스테이징된) 인물 수
        """
        use_image = self.rt.is_enabled(session, "image")
        use_tts = self.rt.is_enabled(session, "tts")
        if not (use_image or use_tts):
            return 0

        names = _irregular_npc.extract_candidate_names(text, session)
        if not names:
            return 0

        # §38: provider-boundary 오퍼레이션의 논리 시도 정체성을 호출 '전'에 고정해
        #      적용 직전 stale guard에 쓴다(더 새로운 턴이 시작되면 등록/승격 금지).
        _tx_obj = _turn_transaction.get_active_transaction(session)

        pool = _irregular_npc.irregular_image_pool(session)
        if not pool:
            # 이미지 후보가 없으면 목소리만 결정한다. image_key는 빈 값으로 응답하도록
            # 후보 목록에 자리표시자를 넣지 않고, 아래에서 결과를 무시한다.
            pool = ["(없음)"]

        prev = _irregular_npc.get_registry(session)
        prev_lines = [
            f"- {n}: {e.get('gender')}/{e.get('age')}, 이미지 {e.get('image_key') or '(없음)'}"
            for n, e in prev.items()          # v7.25: 앞 10명(가장 오래된 등록)만 주던 선별 제거 — 일관성 기준 전부
        ]

        user_prompt = (
            f"[이번 묘사문]\n{text or ''}\n\n"
            f"[배정 대상 인물]\n" + "\n".join(f"- {n}" for n in names) + "\n\n"
            f"[이미지 후보 목록]\n" + ", ".join(pool) + "\n\n"
            f"[이전 배정 기록]\n" + ("\n".join(prev_lines) if prev_lines else "(없음)")
        )

        config = types.GenerateContentConfig(
            http_options=_resilience.provider_http_options("media"),  # WP-C B-C4
            system_instruction=IRREGULAR_NPC_SYSTEM_INSTRUCTION,
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=IRREGULAR_NPC_RESPONSE_SCHEMA,
            safety_settings=_constants.TRPG_SAFETY_SETTINGS,
        )
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]

        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_TURN_IRREGULAR_NPC, session=session,
            model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        try:
            _ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=_cache.DEFAULT_MODEL, contents=contents, config=config,
                ),
                layer="media", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                return 0
        except Exception as e:
            print(f"[비정규NPC] 호출 실패(진행에는 영향 없음): {type(e).__name__} - {e}")
            return 0

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_t, out_t, cached_t, thought_t = _cost.extract_token_usage(meta)
            breakdown = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL, input_tokens=in_t,
                output_tokens=out_t, cached_read_tokens=cached_t,
            )
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_t, cached_input_tokens=cached_t,
                output_tokens=out_t, thought_tokens=thought_t,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(
                session.session_id, f"{COST_LOG_PREFIX}비정규 NPC 배정",
                in_t, cached_t, out_t, cost, session.total_cost
            )
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({
                "label": "비정규 NPC 배정", "cost": cost,
                "in": in_t, "cached": cached_t, "out": out_t, "manifest": [],
            })
        except Exception as e:
            print(f"[비정규NPC] 비용 정산 실패: {e}")

        try:
            data = json.loads(response.text or "{}")
        except json.JSONDecodeError:
            print("[비정규NPC] 응답 파싱 실패")
            return 0

        # ══════════════════════════════════════════════════════════════
        #  비정규 NPC 미디어 배정 — 계획-권위 적용 경계
        #  위쪽(provider 호출 + 파싱)은 result-only: canonical 미변경.
        #  raw payload는 build_irregular_npc_plan에서 검증·정규화된 등록 계획으로만
        #  소비되고, 이후 어떤 canonical mutator(register)의 입력도 되지 않는다.
        # ══════════════════════════════════════════════════════════════
        valid_pool = set(_irregular_npc.irregular_image_pool(session))
        plan = _turn_preparation.build_irregular_npc_plan(
            session, data, names=names, valid_pool=valid_pool, use_image=use_image,
            text=text,
            turn=(preparation.staged_log["turn_no"]
                  if preparation is not None and isinstance(preparation.staged_log, dict)
                  else getattr(session, "turn_count", 0)),
            transaction_id=getattr(_tx_obj, "transaction_id", None),
            logical_turn=getattr(_tx_obj, "logical_turn", None),
            attempt=getattr(_tx_obj, "attempt", None))

        # stale guard(§26/§38): 이 결과의 논리 시도보다 더 새로운 시도가 활성화됐으면
        # canonical NPC 등록/승격을 하지 않는다.
        if _turn_preparation.extraction_is_stale(
                session, logical_turn=getattr(_tx_obj, "logical_turn", None),
                attempt=getattr(_tx_obj, "attempt", None)):
            plan.rejected_stale = True
            print(f"[비정규NPC/{session.session_id}] stale 결과 거부 — 더 새로운 논리 시도 활성")
            return 0

        if preparation is not None:
            preparation.irregular_plan = plan
            preparation.irregular_text = text
            self._stage_npc_promotions(session, preparation, plan, text, master_ch)
            return len(plan.registrations)
        return await self._apply_irregular_npc_plan(session, plan, text, master_ch)

    async def _apply_irregular_npc_plan(self, session, plan, text, master_ch=None,
                                        *, staged_promotions=None, derived=None) -> int:
        """LegacyCompatibilityApplier(비정규 NPC 등록) — 계획의 정규화 항목만 적용한다.

        raw 모델 payload는 이 함수에 입력되지 않는다. register는 계획의 정규화된
        등록 항목만 소비하며 멱등(이미 있으면 기존 항목 반환)하다. 등장 누적·승격
        판정은 코드 파생(이름이 묘사에 등장)이며, 승격 세부설정 생성은 별도 provider
        오퍼레이션(_generate_npc_detail)이 자체 정규화 경계를 거친다.
        """
        added = 0
        for reg in plan.registrations:
            _irregular_npc.register(
                session, reg["name"], image_key=reg["image_key"],
                gender=reg["gender"], age=reg["age"],
                context=reg["context"], turn=reg["turn"])
            added += 1

        # 등장 누적 및 승격 판정 — 비중이 생긴 인물의 설정을 생성한다(코드 파생).
        try:
            turn = getattr(session, "turn_count", 0)
            for name in list(_irregular_npc.get_registry(session).keys()):
                if name not in (text or ""):
                    continue
                _irregular_npc.note_appearance(session, name, turn)
                if staged_promotions is not None:
                    # WP-C READY 이후: 사전 READY에 준비된 승격만 적용(provider 호출 없음).
                    if name in staged_promotions:
                        await self._apply_npc_promotion(
                            session, name, staged_promotions[name], master_ch,
                            derived=derived)
                elif _irregular_npc.should_promote(session, name):
                    await self._generate_npc_detail(session, name, text, master_ch)
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[NPC설정] 승격 판정 실패(진행에는 영향 없음): {e}")

        if added and (master_ch or derived is not None):
            reg = _irregular_npc.get_registry(session)
            names = [r["name"] for r in plan.registrations]
            lines = [f"{n} — {reg[n]['gender']}/{reg[n]['age']}"
                     f"{', ' + reg[n]['image_key'] if reg[n].get('image_key') else ''}"
                     for n in names if n in reg]
            _msg = "🎭 **[비정규 NPC 배정]** " + " · ".join(lines)
            if derived is not None:
                derived.master(_msg)
            else:
                await master_ch.send(_msg)
        plan.applied = True
        return added

    async def _apply_extraction_plan(self, session, plan, master_ch=None,
                                     *, derived=None) -> dict:
        """LegacyCompatibilityApplier — 추출 변이 계획(정규화 typed 필드)만 적용한다.

        raw provider result는 이 함수에 **입력되지 않으며** 어떤 canonical mutator의
        입력도 되지 않는다. 검증 탈락·dedup·conflict로 계획에서 빠진 후보는 여기에
        도달할 수 없다(되살아남 불가). 기존 도메인 helper는 재사용하되 입력은 계획에서
        파생한 정규화 DTO(또는 좁은 구조)뿐이다. plan.result는 증거 스냅샷·로그용일 뿐
        mutator 입력이 아니다.

        WP-D: derived(_commit_coordinator.DerivedEffects)가 주어지면(권위적 커밋의
        영속 전 적용) Discord 송신·통계 쓰기를 하지 않고 기술자만 쌓는다 — 정본 변이만
        메모리에서 일어나며 파생 출력은 durable COMMITTED 이후 방출된다.
        """
        async def _master(content):
            if derived is not None:
                derived.master(content)
            elif master_ch:
                await master_ch.send(content)

        async def _game(content, view_factory=None):
            if derived is not None:
                derived.game(content, view_factory=view_factory)
                return
            ch = self.bot.get_channel(session.game_ch_id)
            if ch:
                if view_factory is not None:
                    await ch.send(content, view=view_factory())
                else:
                    await ch.send(content)

        # ── 세계 타임라인 / 장소(정규화된 이름으로만) ──
        if plan.location_after:
            try:
                if _places.mark_visited(session, plan.location_after):
                    print(f"[장소] 첫 방문: {plan.location_after}")
                if plan.location_moved and not _player_world.legacy_narrative_retired(session):
                    # H07-C: 정적 상주(places[*].npcs) 기반 동행 해제는 LEGACY만 — 연고는 현재 위치가 아니다
                    gone = _extraction.release_resident_companions(session, plan.location_after)
                    if gone:
                        await _master(
                            f"👋 **[동행]** {', '.join(gone)}이(가) 자리에 남았습니다.")
            except Exception as e:
                if derived is not None:
                    raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                print(f"[장소] 적용 실패: {e}")
        if plan.world_new_tl is not None:
            session.world_timeline = _timeline.quantify(session, plan.world_new_tl)

        # 증거 스냅샷(참조·로그용) + 추출 차단 해제.
        session.last_extraction = plan.result
        session.extraction_pending = False
        session.extraction_retry_ctx = {}

        # ── 동행 (정규화 DTO만) ──
        try:
            comp = self.rt.apply_normalized_companions(session, {
                "joined": plan.companions_joined,
                "left": plan.companions_left,
                "npcs_met": plan.npcs_met})
            # v7.35: 동행 변화는 세계 델타 보고(🧍 동행)가 싣는다
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[동행] 갱신 실패: {e}")

        # ── 상태이상 / 소지품 (정규화 DTO만; 임계·검증은 이미 계획 빌드에서 끝남) ──
        _ip = getattr(plan, "item_plan", None)
        applied = _extraction.apply_normalized_status_item(session, {
            "status_apply": plan.status_apply,
            "status_clear": plan.status_clear,
            "item_deltas": [] if _ip is not None else plan.item_deltas,
            "npcs": plan.npcs_met})
        if _ip is not None:
            # v7.17 DYNAMIC: 검증된 아이템 계획을 같은 커밋에서 다시 검증·적용(불일치는 커밋 owner로 전파 — 부분 적용 없음)
            applied["items"] = _items.apply_plan(session, _ip)

        # ── 퀘스트 진전 / 이면정보 / 메인 해금 / 엔딩 (좁은 정규화 입력만) ──
        moved = None
        if _player_world.legacy_narrative_retired(session):
            # H07-C: 동적 세계 세션 — 레거시 퀘스트 진전·이면 인지·메인 해금·엔딩은 은퇴(사건 상태기계가 권위).
            #        소속은 사건 결과에서 파생한 읽기 전용 호환 뷰(world_player_faction)만 반영한다.
            if getattr(plan, "world_player_faction", None):
                if session.player_faction != plan.world_player_faction:
                    await _game(_master_mod.lead("turn.faction_notice",
                                                 f"🏛️ **{plan.world_player_faction}**의 일원이 되었습니다."))
                session.player_faction = plan.world_player_faction
        if not _player_world.legacy_narrative_retired(session):
            try:
                if _quest.check_secret_awareness(
                        session, {"secret_awareness": plan.secret_awareness}):
                    print(f"[GM/{session.session_id}] 이면정보 인지됨")
                    await _master("🔓 **[퀘스트]** 플레이어가 숨겨진 사실을 알아챘습니다.")

                try:
                    mains = _quest.check_main_unlock(session)
                    if mains and not getattr(session, "main_unlocked_notified", False):
                        session.main_unlocked_notified = True
                        await _master(
                            f"🗝️ **[퀘스트]** 메인라인 조건 충족 — "
                            f"{', '.join(q['name'] for q in mains)}")
                except Exception as e:
                    if derived is not None:
                        raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                    print(f"[퀘스트] 메인 해금 확인 실패: {e}")

                moved = _quest.advance_quest(
                    session, {"quest_progress": plan.quest_progress})
                if moved and moved.get("moved"):
                    outcome = moved.get("outcome")
                    print(f"[GM/{session.session_id}] 퀘스트 진전 → {moved['node']}"
                          + (f" (완료: {outcome})" if outcome else ""))
                    if outcome:
                        await _master(f"📜 **[퀘스트]** 완료 — {outcome}")
                    if moved.get("granted"):
                        g = moved["granted"]
                        if g.get("faction"):
                            print(f"[GM/{session.session_id}] 소속 획득: {g['faction']}")
                            await _game(_master_mod.lead("turn.faction_notice",
                                                         f"🏛️ **{g['faction']}**의 일원이 되었습니다."))
                    if _quest.is_ending(outcome):
                        session.pending_ending = outcome
                        await _game(
                            f"🎬 **세션 엔딩에 도달했습니다.**\n"
                            f"> 결말: {outcome}\n\n"
                            + _quest.format_plans(),
                            view_factory=lambda: self.rt.InfinityPlanView(self.bot, session))
            except Exception as e:
                if derived is not None:
                    raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
                print(f"[퀘스트] 진전 실패(진행에는 영향 없음): {e}")
        if applied["applied"] or applied["cleared"]:
            print(f"[GM/{session.session_id}] 상태 적용: "
                  f"부여={applied['applied']} 해제={applied['cleared']}")

        # ── BGM (정규화 situation) ──
        try:
            track = self.rt.select_bgm(session, plan.situation or {})
            if track:
                session.pending_bgm = track
                print(f"[GM/{session.session_id}] BGM 전환 예정: {track}")
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[BGM] 선택 실패(진행에는 영향 없음): {e}")

        # ── 통계 ── (WP-D: 권위 커밋에서는 COMMITTED 이후 파생 방출)
        try:
            for uid in (session.players or {}):
                if derived is not None:
                    derived.stats(uid, turns=1,
                                  status_applied=len(applied["applied"]),
                                  status_cleared=len(applied["cleared"]))
                    derived.npcs(uid, applied["npcs"])
                    continue
                await _stats.bump(
                    uid, turns=1,
                    status_applied=len(applied["applied"]),
                    status_cleared=len(applied["cleared"]))
                await _stats.add_npcs(uid, applied["npcs"])
        except Exception as e:
            if derived is not None:
                raise   # WP-D(D-D1): 권위 커밋의 정본 변이 실패는 삼키지 않고 커밋 owner로 전파
            print(f"[통계] 누적 실패(진행에는 영향 없음): {e}")

        # 로그(증거) + 계획 충돌 진단.
        self.rt.write_log(
            session.session_id, "api",
            f"[추출층위 결과]\n{json.dumps(plan.result, ensure_ascii=False, indent=2)}")
        if plan.conflicts:
            print(f"[추출/{session.session_id}] 계획 충돌 진단(적용 제외): {plan.conflicts}")

        # 마스터 보고 — 계획·적용 결과에서 구성(raw result는 mutator에 안 들어감).
        # v7.35: 동적 세계는 추출의 위치가 권위가 아니다 — 추출 요약은 층위활동 보고(🔎)·세계 델타(🎒·🩹)로만
        if (master_ch or derived is not None) and not getattr(session, "definition_revision", None):
            loc = (plan.world_new_tl or {}).get("current_location") or "미확인"
            report = f"🔎 **[추출층위]** 위치={loc}"
            if applied["applied"]:
                report += f"\n> 상태 부여: {', '.join(applied['applied'])}"
            if applied["cleared"]:
                report += f"\n> 상태 해제: {', '.join(applied['cleared'])}"
            if applied.get("items"):
                shown = [f"{i['target']} {i['item']} "
                         f"{'+' if i['delta'] > 0 else ''}{i['delta']} (→{i['after']})"
                         for i in applied["items"][:6]]
                report += f"\n> 소지품 반영: {', '.join(shown)}"
            if plan.companions_joined:
                report += f"\n> 동행 합류: {', '.join(plan.companions_joined)}"
            await _master(report)

        plan.applied = True
        return {"applied": applied, "moved": moved}

    async def _run_extraction(self, session, ai_output_text: str, master_ch=None,
                              *, transaction_id: str | None = None,
                              logical_turn: int | None = None,
                              attempt: int | None = None,
                              preparation=None, correction: str | None = None):
        """
        추출층위 — 묘사 출력물에서 공통·시나리오별 타겟 값을 추출한다.

        v7.29 correction: 직전 추출의 물건 연산이 정본 검증에서 거부됐을 때 그 사유를 덧붙여 같은 턴에 한 번 더
        추출한다(같은 묘사·같은 후보 — 사유만 추가).

        [동시 실행]
        묘사 스트리밍과 병행되므로, 실패해도 이미 출력된 묘사를 되돌릴 수 없다.
        따라서 일반적인 턴 취소 규정을 적용하지 않고 추출만 재시도한다.
        재시도까지 실패하면 session.extraction_pending을 세워 다음 턴을 차단하고,
        게임 채널에 재시도 버튼을 배치한다.

        [캐시 미사용] 출력물 판독에 룰북이 불필요하므로 캐시를 읽지 않는다.

        [WP-C 준비 모드] preparation이 주어지면(자동 턴 등록 준비 작업) result-only로
        동작한다: 검증·정규화된 ExtractionMutationPlan을 preparation.extraction_plan에
        저장해 반환하고, 정본 적용·extraction_pending/UI는 하지 않는다(배리어 owner 소관).
        프롬프트·계획 입력은 기존 흐름과 같은 의미(지시효과 quest 투영, 이번 턴 비정규
        등록/승격 NPC)를 읽기 전용 투영 뷰로 본다.

        WP-G(RE-GATE): 준비 객체가 필수다. preparation=None 호출은 provider 호출 전에 거부된다 —
        추출 결과를 정본에 직접 적용하던 비준비 경로(WP-B 호환 경계)는 은퇴했고, 정본 적용은
        READY 이후 CommitCoordinator 경로(_apply_prepared_extraction)만 수행한다.
        """
        if preparation is None:
            raise RuntimeError(
                "추출층위는 자동 턴 준비 객체(preparation) 없이 실행할 수 없습니다 "
                "(비준비 추출 → 정본 직접 적용 경로는 WP-G에서 은퇴)")
        view = session
        if preparation is not None:
            _pend = _turn_preparation.pending_for(session)
            _qproj = (_pend.projected_quest_state
                      if _pend is not None and _pend.projected_quest_state is not None
                      else copy.deepcopy(getattr(session, "quest_state", None)))
            view = _turn_preparation.PreparationView(session, overrides={
                "quest_state": _qproj,
                "irregular_npcs": _turn_preparation.projected_irregular_registry(
                    session, preparation),
                "npcs": _turn_preparation.projected_npcs(session, preparation),
            })
        targets = _extraction.build_extraction_targets(view)
        prev_tl = getattr(session, "world_timeline", {}) or {}
        prev_summary = (
            f"위치={prev_tl.get('current_location', '미확인')}, "
            f"시간대={prev_tl.get('time_of_day', '미확인')}, "
            f"날짜={prev_tl.get('current_date', '미확인')}"
        ) if prev_tl else "(없음)"

        # 이면정보 인지 판정을 위해 '인지 조건'만 주입한다.
        # 이면정보 본문(truth)은 넣지 않는다 — 추출층위가 알면 판정이 오염된다.
        secret_block = ""
        try:
            # H07-C: 동적 세계 세션의 이면정보 인지는 정보 노출(관찰 매니페스트)만 — 레거시 인지 조건 없음
            st = {} if _player_world.legacy_narrative_retired(session) else _quest.get_state(view)
            active = st.get("active")
            if active and not active.get("secret_known"):
                q = _quest._find_quest(view, active["id"])
                conds = ((q or {}).get("hidden") or {}).get("reveal_conditions") or []
                if conds:
                    secret_block = (
                        "\n[숨겨진 사실의 인지 조건 — 아래에 해당하는 정황이 묘사에 있었는지 평가]\n"
                        + "\n".join(f"- {c}" for c in conds) + "\n"
                    )
        except Exception as e:
            print(f"[추출] 인지 조건 주입 실패: {e}")

        # 유효 목록을 주입한다. 코드가 걸러내기는 하지만, 목록을 주지 않으면
        # 매번 없는 상태이상을 만들고 인물 아닌 것을 npcs_met에 넣는다.
        limit_block = _extraction.build_extraction_limits(view)

        # WP-B(AUD-011): 추출 증거에는 완결된 전체 묘사가 들어가야 한다.
        #   추출 모델(Gemini)의 컨텍스트 한도는 묘사 길이(수천 자)를 크게 상회하므로
        #   청킹 없이 전문을 전달한다. 요약·앞부분 절단·꼬리 드롭을 하지 않는다.
        _wt = getattr(preparation, "world_turn", None)
        _ext_schema = _extraction.EXTRACTION_RESPONSE_SCHEMA
        _obs_block = ""
        _var_manifest = {}
        _rev_manifest = {}
        if _wt is not None:
            # H05-B: 관찰 후보(장면 행위자가 아는 정보·장면 신호·선후 선택지)만 — 숨은 진실 미포함.
            # v7.14: 이번 턴 발견(게이트 승인·발견 굴림 성공)만 DNEW로 — 판단 기록에서 고정
            _disc = _discovery_outcome(self._tx_judgment(session, transaction_id), transaction_id)
            _wt.discovery = _disc if _disc and _disc.get("status") == "NEW" else None
            _wt.obs_manifest = _turn_world.build_observation_manifest(
                _wt.ctx.definition, _wt.ctx, _wt.plan, session, discovery=_wt.discovery,
                pending=_turn_world.pending_signals(session))     # P1: 전달 대기 신호도 표현 근거 후보
            _ext_schema = copy.deepcopy(_extraction.EXTRACTION_RESPONSE_SCHEMA)
            # v7.25(F10): 동적 세계에서 적용되지 않는 레거시 필드는 묻지 않는다 — 장소·날짜는 정본 세계, 퀘스트 진행·
            #   이면정보 인지는 사건 상태기계·정보 인지가 소유한다(파서가 기본값으로 채워 하류 호환은 유지)
            for _k in _extraction.DYNAMIC_RETIRED_FIELDS:
                _ext_schema["properties"].pop(_k, None)
            _ext_schema["required"] = [k for k in _ext_schema.get("required", [])
                                       if k not in _extraction.DYNAMIC_RETIRED_FIELDS]
            targets = [t for t in targets if t not in _extraction.DYNAMIC_RETIRED_TARGETS]
            _ext_schema.setdefault("properties", {})[_turn_world.OBS_FIELD] = _wt.obs_manifest.schema()
            _obs_block = _wt.obs_manifest.render()
            # v7.17 아이템: 같은 추출 호출의 런타임 확장(새 provider 호출 0) — 소유자·소지·현장 물건 키만
            _wt.item_manifest = self._item_manifest(session, _wt)
            _ext_schema["properties"][_turn_world.OBS_FIELD]["properties"][_items.OPS_FIELD] = \
                _wt.item_manifest.schema()
            _ext_schema["properties"][_turn_world.OBS_FIELD]["properties"][_items.TRAITS_FIELD] = \
                _wt.item_manifest.traits_schema()
            _obs_block += _wt.item_manifest.render()
            # v7.48(E4): 이번 묘사에서 PC 행동이 낸 소리 — 추적자 감지·숨음 해제의 근거(코드가 범위를 정한다).
            #   기능 stealth를 선언한 시나리오만(scenario_schema.capabilities)
            if "stealth" in _utils.scenario_capabilities(session.scenario_data or {}):
                _ext_schema["properties"][_turn_world.OBS_FIELD]["properties"][_turn_world.NOISE_FIELD] = {
                    "type": "string", "enum": list(_status_rules.NOISE_LEVELS),
                    "description": "이번 묘사에서 PC의 행동이 낸 소리"}
                _obs_block += ("[noise — 이번 묘사에서 PC의 행동이 낸 소리] QUIET = 숨죽임·살금살금·속삭임 / NORMAL = 걷기·"
                               "보통 대화·문 여닫기 / LOUD = 뛰기·비명·고함·유리·문 부수기·총성·물건 쓰러뜨림. "
                               "다른 인물이 낸 소리는 넣지 않는다.\n")
            # v7.47(S1): PC 상태 단계 — status_scores에 단계 번호(단계 없는 상태는 0)
            _ss = _ext_schema["properties"].get("status_scores")
            if isinstance(_ss, dict) and isinstance((_ss.get("items") or {}).get("properties"), dict):
                _ss["items"]["properties"]["stage"] = {
                    "type": "integer", "description": "단계가 있는 상태의 지금 단계 번호(1부터 — 상태 목록의 단계 순서). "
                                                      "단계가 없는 상태·해소(0~30)는 0"}
            # SVG 카드: 장면 카드 인물별 허용 바리에이션 — 인물당 턴 하나(턴 로컬, 정본 저장 없음)
            _cw = self.rt.speaker_card.card_world(session)
            _var_manifest = self.rt.speaker_card.variation_manifest(_cw, _wt, session) if _cw else {}
            if _var_manifest:
                _ext_schema["properties"]["speaker_variations"] = self.rt.speaker_card.variation_schema(_var_manifest)
                _obs_block += self.rt.speaker_card.variation_block(_var_manifest, _cw.definition, session)
            # P3 G3: 이번 턴 이름·얼굴 식별 후보의 실제 공개 문장(같은 추출 호출 — 새 provider 호출 0)
            _rev_manifest = self.rt.speaker_card.reveal_manifest(_cw, _wt, session) if _cw else {}
            if _rev_manifest:
                _ext_schema["properties"][self.rt.speaker_card.REVEAL_FIELD] = \
                    self.rt.speaker_card.reveal_schema(_rev_manifest)
                _obs_block += self.rt.speaker_card.reveal_block(_rev_manifest, _cw.definition, session)
            # 장소·시간 권위는 정본 세계다 — 레거시 요약 대신 정본 미러를 보여 준다.
            _tl = _turn_world.mirror_timeline(prev_tl, _wt.ctx.definition,
                                                  _wt.plan.projected_world if _wt.plan is not None
                                                  and _wt.plan.projected_world is not None else session,
                                                  _wt.ctx.lead_pc)
            prev_summary = (f"위치={_tl.get('current_location_display') or _tl.get('current_location', '미확인')}, "
                            f"시간대={_tl.get('time_of_day', '미확인')}, 날짜={_tl.get('current_date', '미확인')} "
                            f"(코드 확정 정본)")
        user_prompt = (
            "[추출 항목]\n" + "\n".join(f"- {t}" for t in targets) + "\n"
            + limit_block + secret_block + _obs_block + "\n"
            f"[직전까지의 세계 상태]\n{prev_summary}\n\n"
            f"[이번 묘사문]\n{ai_output_text}"
        )
        if correction:
            user_prompt += ("\n\n[직전 추출의 물건 연산이 정본 검증에서 거부됨 — 바로잡을 것]\n"
                            f"{correction}\n"
                            "· 위 사유를 고쳐 다시 추출하십시오. 키는 [물건 — item_ops]의 후보만, 묘사에 실제로 일어난 "
                            "변화만 적고, 맞는 키가 없으면 그 연산은 적지 마십시오.")

        contents = [types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])]
        config = types.GenerateContentConfig(
            http_options=_resilience.provider_http_options("extraction"),  # WP-C B-C4
            system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=_ext_schema,
            safety_settings=_constants.TRPG_SAFETY_SETTINGS,
            # 추출은 묘사문에서 값을 읽어 옮기는 기계적 작업이다.
            # 실측 결과 출력의 90%가 사고 토큰이었고(1039/1148) 실제 JSON은
            # 109토큰에 불과했다. 사고 예산을 제한해 낭비를 줄인다.
            thinking_config=types.ThinkingConfig(thinking_budget=EXTRACTION_THINKING_BUDGET),
        )

        result = None
        # WP-02: 외부 추출 재시도 전체가 하나의 논리 오퍼레이션(operation_id 공유).
        _cl_op = _cost_ledger.begin_operation(
            self.bot, _cost_ledger.OP_TURN_EXTRACTION, session=session,
            model=EXTRACTION_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
            billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
        if preparation is not None:
            preparation.claim_cost_operation(_cl_op)
        # WP-C(F1): 재시도 루프 변수가 트랜잭션 attempt 매개변수를 가리던 결함 수정.
        #   (기존: for attempt in ... → stale 판정·계획 정체성이 루프 인덱스로 오염)
        for _try in range(EXTRACTION_MAX_RETRIES):
            ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=EXTRACTION_MODEL, contents=contents, config=config,
                ),
                layer="extraction", session_id=session.session_id, retries=1,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not ok:
                continue

            try:
                meta = response.usage_metadata
                in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)
                breakdown = _cost.calculate_text_gen_cost_breakdown(
                    EXTRACTION_MODEL, input_tokens=in_tokens,
                    output_tokens=out_tokens, cached_read_tokens=cached_tokens,
                )
                cost = breakdown["total_krw"]
                self.rt.accrue(session, cost, breakdown["total_usd"])
                _cl_op.record(
                    input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                    output_tokens=out_tokens, thought_tokens=thought_tokens,
                    cost_usd=breakdown["total_usd"], cost_krw=cost,
                    usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
                _estimate.update_stats(session, "extraction", out_tokens, thought_tokens)
                _io.write_cost_log(
                    session.session_id, f"{COST_LOG_PREFIX}추출층위 호출",
                    in_tokens, cached_tokens, out_tokens, cost, session.total_cost
                )
                if not hasattr(session, "turn_cost_log"):
                    session.turn_cost_log = []
                session.turn_cost_log.append({
                    "label": "추출층위", "cost": cost,
                    "in": in_tokens, "cached": cached_tokens, "out": out_tokens,
                    "manifest": [], "layer": "extraction",
                    "census": _prompt_census.record(
                        session, "extraction", contents, system_text=_sys_text(config),
                        in_tokens=in_tokens, cached_tokens=cached_tokens),
                })
            except Exception as e:
                print(f"[GM] 추출층위 비용 정산 실패: {e}")

            result = self.rt.parse_extraction(response.text or "")
            if result:
                break
            print(f"[GM] 추출층위 응답 파싱 실패(시도 {_try + 1})")

        if not result:
            # WP-C: 실패 처리(재시도 UI·차단)는 배리어 owner가 담당한다.
            return None

        # 검증·정규화 계획 — ExtractionMutationPlan이 적용의 유일 권위다. raw result는 어떤
        #   canonical mutator의 입력도 되지 않는다. 여기서는 스테이징만 하며 정본 적용은
        #   READY 이후 CommitCoordinator 경로(_apply_prepared_extraction)가 수행한다.
        plan = _turn_preparation.build_extraction_plan(
            view, result, transaction_id=transaction_id,
            logical_turn=logical_turn, attempt=attempt)
        # stale이면 현재 배리어를 만족 못한다(정본 미변경).
        if (not preparation.matches(_turn_transaction.get_active_transaction(session))
                or _turn_preparation.extraction_is_stale(
                    session, logical_turn=logical_turn, attempt=attempt)):
            plan.rejected_stale = True
            print(f"[추출/{session.session_id}] stale 준비 결과 — 배리어 미충족 "
                  f"(tx={transaction_id}, lt={logical_turn}, at={attempt})")
            return None
        preparation.extraction_plan = plan
        preparation.world_observation_raw = result.get(_turn_world.OBS_FIELD) if _wt is not None else None
        if _wt is not None and _var_manifest:
            preparation.card_variations = self.rt.speaker_card.parse_variations(
                _var_manifest, result.get("speaker_variations"))
        if _wt is not None and _rev_manifest:
            _narr = getattr(preparation, "narration", None)
            _paras = getattr(_narr, "paragraphs", ()) or ()
            _rev = self.rt.speaker_card.parse_reveals(
                _rev_manifest, result.get(self.rt.speaker_card.REVEAL_FIELD), _paras, _cw.definition, session)
            for _iid, _ix in self.rt.speaker_card.implied_name_reveals(
                    _rev_manifest, _paras, _cw.definition, session).items():   # v7.35(U1-b): 대사 속 이름
                _rev[_iid] = min(_rev.get(_iid, _ix), _ix)
            preparation.identity_reveals = _rev
        return plan

    async def _simulate_narrative_directions(self, session, player_message: str,
                                              master_ch,
                                              *, transaction_id: str | None = None,
                                              gate_inputs: dict | None = None) -> dict | None:
        """
        지시층위 호출 전 세계관 캐시를 활용하여 고차원 서사 방향성을 사전 시뮬레이션.

        [목적]
        단순 사건 추론(소리가 났으니 몬스터가 온다)이 아닌, 시나리오 세계관의 세력 배치·
        지역 규칙·물리 법칙에 근거한 구조적 개연성 판단 결과를 지시층위에 제공한다.

        [활성화 조건]
        - 세션 캐시 유효 (없으면 세계관 문맥 없어 고차원 판단 불가)
        - 캐시 모델이 DEFAULT_MODEL과 일치

        Returns:
            dict | None: {"world_state_analysis": str, "directions": [...]} 또는 None
        """
        cache_name = getattr(session, "cache_name", None)
        if not _turn_history.cache_usable(session):     # WP-E(E-E3)
            return None

        # 최근 로그 (턴 개수 4개 유지, 각 턴은 온전 원문)
        recent_lines = []
        for content in session.raw_logs[-4:]:
            try:
                text = content.parts[0].text
                role = content.role.upper()
                recent_lines.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_str = "\n\n".join(recent_lines) if recent_lines else "(없음)"

        # 세계 물리 타임라인 요약 (있으면)
        tl = getattr(session, "world_timeline", {})
        tl_note = ""
        if tl:
            tl_note = (
                f"\n[현재 세계 상태]\n"
                f"위치: {tl.get('current_location', '미확인')}\n"
                f"시간대: {tl.get('time_of_day', '미확인')}\n"
                f"세력: {tl.get('faction_context', '미확인')}\n"
            )

        # v7.14 선언 게이트 입력 — PC 지식 범위(이 장소의 유효 발견·찾지 못한 이전 탐색)만
        gate_note = ""
        gi = gate_inputs or {}
        if gi.get("dynamic") and gi.get("place"):
            rows = gi.get("rows") or []
            failed = gi.get("failed") or []
            gate_note = (
                "\n[이 장소에서 PC가 이미 발견한 것]\n"
                + (_turn_world.discovery_rows_text(rows) if rows else "(없음)")
                + "\n[이 장소에서 찾지 못한 이전 탐색 — 실패는 부재가 아니다]\n"
                + ("\n".join(f"· {f['target']} — 당시 선언: {f['declaration']}" for f in failed) if failed else "(없음)")
                + "\n")
        elif not gi.get("dynamic"):
            gate_note = "\n[세션 형식] 이 세션은 주변 발견을 영속하지 않는다 — 발견 제안은 일반 행동(ORDINARY)으로 분류한다.\n"

        user_prompt = (
            f"[현재 턴]: {session.turn_count + 1}\n"
            f"{tl_note}"
            f"\n[최근 게임 로그]\n{recent_str}\n\n"
            f"[이번 턴 플레이어 행동]\n{player_message}\n"
            f"{gate_note}\n"
            "[지시사항]\n"
            "위 상황에서 다음에 일어날 수 있는 서사 방향성 2~3개를 세계관 논리에 근거하여 평가하십시오.\n"
            "반드시 시나리오 캐시의 세력 정보·지역 규칙·세계관 설정을 인용하여 판단하십시오.\n"
            "이번 플레이어 선언을 [선언 게이트 규칙]에 따라 declaration_gate로 판정하십시오."
        )

        try:
            sim_contents = [
                types.Content(role="user",
                              parts=[types.Part.from_text(text=NARRATIVE_SIMULATOR_SYSTEM_INSTRUCTION)]),
                types.Content(role="model",
                              parts=[types.Part.from_text(
                                  text="이해했습니다. 세계관 논리에 근거하여 서사 방향성을 분석하겠습니다.")]),
                types.Content(role="user",
                              parts=[types.Part.from_text(text=user_prompt)]),
            ]
            config = types.GenerateContentConfig(
                http_options=_resilience.provider_http_options("instruction"),  # WP-C B-C4
                cached_content=cache_name,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=NARRATIVE_DIRECTION_SCHEMA,
                safety_settings=_constants.TRPG_SAFETY_SETTINGS,
            )
            # 재시도·타임아웃 보호. 층위 호출은 모두 이 관문을 통과한다.
            _cl_op = _cost_ledger.begin_operation(
                self.bot, _cost_ledger.OP_TURN_SIMULATION, session=session,
                model=_cache.DEFAULT_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
                billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
            # WP-C: 자동 턴 비용 멤버 claim(transaction_id=None인 수동 호출은 no-op).
            _turn_preparation.claim_cost_operation(session, transaction_id, _cl_op)
            _ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=_cache.DEFAULT_MODEL,
                    contents=sim_contents,
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] 서사 설계 호출 실패: {e}")
            return None

        # 비용 정산
        try:
            meta = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)
            breakdown     = _cost.calculate_text_gen_cost_breakdown(
                _cache.DEFAULT_MODEL, input_tokens=in_tokens, output_tokens=out_tokens,
                cached_read_tokens=cached_tokens)
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(session.session_id, f"{COST_LOG_PREFIX}서사 방향성 시뮬레이션",
                                 in_tokens, cached_tokens, out_tokens, cost, session.total_cost)
            _estimate.update_stats(session, "simulation", out_tokens, thought_tokens)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            # 라벨을 '서사 설계자'라 두면 _plan_narrative와 혼동된다.
            # 이것은 지시층위 직전에 세계관 개연성을 미리 훑는 별개 호출이다.
            session.turn_cost_log.append({"label": "개연성 시뮬레이션", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
            print(
                f"[GM/{session.session_id}] 시뮬레이션 비용: "
                f"In={in_tokens:,} Cached={cached_tokens:,} Out={out_tokens:,} "
                f"→ {_cost.format_cost(cost)}"
            )
        except Exception as e:
            print(f"[GM] 시뮬레이션 비용 정산 실패: {e}")

        raw_text = response.text or ""
        try:
            sim_data = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                sim_data = json.loads(cleaned)
            except Exception as e:
                _logs.event("turn", "개연성 시뮬레이션 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "SIMULATION_JSON"})
                return None

        # 결과를 마스터에 알린다. 비용이 드는 호출인데 무엇을 얻었는지
        # 보이지 않으면 켜둘 가치가 있는지 판단할 수 없다.
        if master_ch:
            try:
                dirs = sim_data.get("directions") or []
                lines = [f"🔮 **[개연성 시뮬레이션]** 방향 {len(dirs)}건"]
                contact = sim_data.get("action_world_contact")
                if contact:
                    lines.append(f"> 접점: {str(contact)[:150]}")
                for d in dirs[:3]:
                    if not isinstance(d, dict):
                        continue
                    lines.append(
                        f"> `{d.get('plausibility', '?')}` "
                        f"{str(d.get('label') or d.get('direction', ''))[:70]}")
                await master_ch.send("\n".join(lines)[:1900])
            except Exception as e:
                print(f"[GM] 시뮬레이션 보고 실패(무시): {e}")

        return sim_data

    async def _update_narrative_progress(self, session, event_assessment: str, master_ch,
                                         *, preparation=None):
        """
        PROCEED 확정 묘사 직후(READY 이전) 호출. 서사 재계획 트리거를 판정한다.

        [재계획 판정 — 수치 기반]
        기존의 '3턴 주기 강제 재계획'은 삭제되었다. 서사 상태와 무관하게
        주기만으로 재계획을 돌리면 불필요한 호출이 누적되기 때문이다.
        대신 추출층위가 산출한 quest_progress 수치를 임계값과 대조해 판정한다.
            deviation >= quest_deviated  → 전체 재수립 (mid_plan 포함)
            advance   >= quest_advance   → 순간 계획만 재수립
        지시층위의 event_assessment는 보조 신호로 유지한다(수치가 없을 때의 폴백).

        WP-C: 트리거된 자동 재계획은 이 턴의 필수 준비 작업(다음 GM 프롬프트가 계획에
        의존)으로 등록되어 READY 전에 합류한다. 후보·last_planned_turn 마커는 준비 객체에
        스테이징되고 READY 이후에만 정본 narrative_plan에 적용된다.
        수치(advance/deviation) 판정은 이번 트랜잭션의 추출 계획(ExtractionMutationPlan.
        quest_progress)이 권위다 — _prepare_extraction이 계획 생성 직후
        _evaluate_numeric_replan으로 자식 준비 작업을 등록한다(B-C3).
        """
        plan = getattr(session, "narrative_plan", {})
        if not plan or preparation is None:
            return

        trigger = None      # (reason, full_replan, context_note)
        # completed/deviated → 즉시 재계획
        if event_assessment == "completed":
            # 순간 계획만 재수립 — mid_plan 유지
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 순간 사건 완료 감지\n"
                    f"> 중규모 계획을 유지하며 다음 순간 계획을 수립합니다..."
                )
            trigger = ("completed", False, "")
        elif event_assessment == "deviated":
            # mid_plan 포함 전부 재수립
            if master_ch:
                await master_ch.send(
                    f"📖 **[서사 계획]** 경로 이탈 감지\n"
                    f"> 중규모 계획 포함 전체 재수립합니다..."
                )
            trigger = ("deviated", True, "")
        elif event_assessment == "resolving" and master_ch:
            await master_ch.send(
                f"📖 **[서사 계획]** 현재 순간 사건이 마무리 단계에 진입했습니다 (resolving)."
            )

        # ── 수치 기반 재계획 판정(설계문서 3)은 WP-C(B-C3)부터 이번 턴 추출 계획이
        #    나온 뒤 _evaluate_numeric_replan이 수행한다(직전 턴 last_extraction 사용 금지).
        #    precedence: event_assessment(completed/deviated) 트리거가 있으면 수치 판정은
        #    생략한다(턴당 재계획 1건 — 기존 중복 회피 규칙 유지).
        if trigger is not None:
            reason, full, note = trigger
            preparation.register_task(
                "narrative_replan", _turn_preparation.TASK_NARRATIVE_REPLAN,
                coro=self._prepare_auto_replan(
                    session, preparation, reason, full_replan=full, context_note=note))

    async def _apply_narrative_plan(self, session, candidate, *, derived=None) -> bool:
        """LegacyCompatibilityApplier(서사 계획 교체) — 정규화 후보만 canonical에 적용한다.

        raw provider payload는 입력되지 않는다(candidate.normalized만 소비).
        코드 소유 metadata(plan_version, last_planned_turn)는 여기서 코드가 부여한다.
        기존 영속 의미(save_session_data)를 유지하며 authoritative commit(WP-D)으로
        옮기지 않는다.
        """
        master_ch = self.bot.get_channel(session.master_ch_id)
        trigger_reason = candidate.trigger_reason

        # ── 버전·타임스탬프 기록(코드 소유) ──
        plan = copy.deepcopy(candidate.normalized)
        plan["plan_version"]      = (session.narrative_plan or {}).get("plan_version", 0) + 1
        plan["last_planned_turn"] = session.turn_count
        session.narrative_plan    = plan
        if derived is None:
            # setup/수동 경로 — 기존 영속 의미 유지. (WP-D 권위 커밋은 strict 저장이 영속한다)
            await self.rt.save_session_data(self.bot, session)
        candidate.applied = True

        # ── 마스터 채널 보고 (embed) ──
        current = plan.get("current_event", {})
        next_ev = plan.get("next_event", {})
        trigger_label_map = {
            "init":      "초기 계획 수립",
            "completed": "사건 완료 → 순간 계획 갱신",
            "deviated":  "이탈 감지 → 전체 재수립",
            "manual":    "수동 재계획",
        }
        trigger_label = trigger_label_map.get(trigger_reason, "계획 갱신")

        if master_ch or derived is not None:
            embed = self.rt.embed(
                title=f"📖 서사 계획 갱신 — {trigger_label}",
                color=0x5865F2,
            )
            embed.set_footer(text=f"v{plan['plan_version']}  |  턴 {session.turn_count}")

            mid = plan.get("mid_plan", {})
            if mid:
                milestones = mid.get("milestones", [])
                ms_str = "\n".join([f"  {i+1}. {m}" for i, m in enumerate(milestones)]) if milestones else "(없음)"
                m_val = (
                    f"**전체 흐름**: {mid.get('overview', '-')}\n"
                    f"**이정표**:\n{ms_str}\n"
                    f"**완료 조건**: {mid.get('end_condition', '-')}"
                )
                embed.add_field(
                    name=f"🗺️ 중규모 진행 방향: {mid.get('title', '?')}",
                    value=m_val[:1020],
                    inline=False,
                )

            c_val = (
                f"**상황**: {current.get('summary', '-')}\n"
                f"**마무리 방향**: {current.get('resolution_direction', '-')}"
            )
            embed.add_field(
                name=f"📌 현재 순간 사건: {current.get('title', '?')}",
                value=c_val[:1020],
                inline=False,
            )

            n_val = (
                f"**개요**: {next_ev.get('summary', '-')}\n"
                f"**시작 조건**: {next_ev.get('trigger', '-')}"
            )
            embed.add_field(
                name=f"⏭️ 다음 순간 사건: {next_ev.get('title', '?')}",
                value=n_val[:1020],
                inline=False,
            )

            planner_notes = plan.get("planner_notes", "")
            if planner_notes:
                embed.add_field(name="📝 설계 메모", value=planner_notes[:1020], inline=False)

            if derived is not None:
                derived.master(embed=embed)     # WP-D: COMMITTED 이후 방출
            else:
                await master_ch.send(embed=embed)

        self.rt.write_log(session.session_id, "api",
                       f"[서사 계획 결과 ({trigger_label})]\n{json.dumps(plan, ensure_ascii=False, indent=2)}")
        return True

    async def _generate_narrative_plan_candidate(self, session, trigger_reason: str,
                                                 *, context_note: str = "",
                                                 full_replan: bool = True,
                                                 transaction_id=None, logical_turn=None,
                                                 attempt=None, preparation=None):
        """
        LOGIC_MODEL을 호출해 서사 계획 **후보**를 만든다 — result-only producer.

        WP-C: preparation이 주어지면(자동 턴 READY 이전) 프롬프트는 기존 흐름과 같은
        입력 의미를 투영으로 읽는다 — 이번 턴 스테이징 로그를 포함한 최근 raw_logs와
        스테이징된 진행 턴 번호. 정본은 읽기만 한다.

        session.narrative_plan을 변경하지 않는다. provider 호출·재시도·비용 관측/정산
        (CostLedger operation, accrue, turn_cost_log)은 기존 의미 그대로 수행하고,
        raw JSON은 _turn_preparation.build_narrative_plan으로 구조 검증·정규화된
        NarrativePlanMutationPlan으로만 반환한다.

        Args:
            trigger_reason: "init" | "completed" | "deviated" | "manual"
            full_replan: True이면 mid_plan 포함 전부 재수립, False이면 순간 계획만 갱신.
            transaction_id/logical_turn/attempt: 자동 경로의 원인 시도 정체성(후보에 부착).

        Returns:
            NarrativePlanMutationPlan | None (퀘스트 모드·호출 실패·파싱 실패·구조 불량)
        """
        master_ch = self.bot.get_channel(session.master_ch_id)

        # 퀘스트 모드에서는 서사설계자가 주도하지 않는다(기획 규정).
        # 퀘스트 시스템이 서사 가이드를 대신하므로, 계획까지 세우면
        # 두 서사가 겹쳐 지시층위가 어느 쪽을 따를지 모호해진다.
        # 서사설계자는 풀자유 세션과 인피니티 플랜에서 쓴다.
        if _player_world.legacy_narrative_retired(session):
            # v7.25(F12): 동적 세계 — 서사 권위는 사건 상태기계·정본이다. 레거시 서사설계자를 부르지 않는다(바인딩 전 free 모드 잔존 포함)
            print(f"[서사설계] 동적 세계 — 레거시 계획 수립 없음")
            return None
        if getattr(session, "narrative_mode", "quest") != "free":
            print(f"[서사설계] 퀘스트 모드 — 계획 수립 생략")
            return None

        # ── 시나리오 정보 ──
        story_guide = session.scenario_data.get("story_guide", "")
        worldview   = session.scenario_data.get("worldview", "")

        # ── 최근 게임 로그 (턴 개수 6개 유지, 각 턴은 온전 원문) ──
        recent_log_lines = []
        _raw_src = list(session.raw_logs)
        _turn_no = session.turn_count
        if preparation is not None and isinstance(preparation.staged_log, dict) \
                and not preparation.staged_log.get("applied"):
            _raw_src = (_raw_src + list(preparation.staged_log["raw_entries"]))[-20:]
            _turn_no = preparation.staged_log["turn_no"]
        for content in _raw_src[-6:]:
            try:
                text    = content.parts[0].text
                role    = content.role.upper()
                recent_log_lines.append(f"[{role}]\n{text}")
            except Exception:
                continue
        recent_logs_str = "\n\n".join(recent_log_lines) if recent_log_lines else "(로그 없음)"

        # ── PC 상태 요약 ──
        pc_lines = []
        for uid, p in session.players.items():
            name    = p.get("name", "?")
            res     = session.resources.get(name, {})
            sta     = session.statuses.get(name, [])
            res_str = ", ".join([f"{k}:{v}" for k, v in res.items()]) or "없음"
            sta_str = ", ".join(sta) or "없음"
            pc_lines.append(f"  - {name}: 자원={res_str}, 상태={sta_str}")
        pc_info = "\n".join(pc_lines) or "(PC 없음)"

        # ── 압축 기억 ──
        memory_str = (
            session.compressed_memory
            or getattr(session, "cached_compressed_memory", "")
            or "(없음)"
        )

        # ── 기존 계획 처리 ──
        existing_plan = session.narrative_plan or {}
        existing_plan_block = ""

        if not full_replan and existing_plan:
            # completed 재계획: mid_plan 유지, 순간 계획만 갱신
            mid = existing_plan.get("mid_plan", {})
            nxt = existing_plan.get("next_event", {})
            if mid:
                ms_str = " → ".join(mid.get("milestones", []))
                existing_plan_block = (
                    "\n[유지할 중규모 진행 계획 — 이 내용을 mid_plan으로 그대로 출력하십시오]\n"
                    f"title: {mid.get('title', '')}\n"
                    f"overview: {mid.get('overview', '')}\n"
                    f"milestones: {ms_str}\n"
                    f"end_condition: {mid.get('end_condition', '')}\n"
                    "\n위 중규모 계획에서 다음으로 도달해야 할 milestone을 목표로 삼아 "
                    "새 current_event와 next_event를 수립하십시오.\n"
                )
            if nxt:
                existing_plan_block += (
                    f"\n[이전 next_event — current_event 승격 참고용]\n"
                    f"제목: {nxt.get('title', '')} / 개요: {nxt.get('summary', '')}\n"
                    f"(이 사건이 새 current_event의 출발점이 됩니다)\n"
                )
            trigger_context = "직전 순간 사건이 완료되었습니다. 중규모 계획을 유지하며 다음 순간 계획으로 전환하세요."
        else:
            # 전체 재계획 (init/deviated/manual)
            if existing_plan and trigger_reason != "init":
                cur = existing_plan.get("current_event", {})
                existing_plan_block = (
                    "\n[이전 계획 (참고용 — 폐기 후 재수립)]\n"
                    f"이전 현재 사건: {cur.get('title', '?')} — {cur.get('summary', '')}\n"
                    f"마무리 방향: {cur.get('resolution_direction', '')}\n"
                )
            trigger_context_map = {
                "init":     "GM가 활성화되었습니다. 현재 상황을 분석하여 2단계 서사 계획(mid_plan + 순간 계획)을 수립하세요.",
                "deviated": "플레이어의 선택으로 서사 방향이 예상 범위를 벗어났습니다. mid_plan 포함 계획 전체를 재수립하세요.",
                "manual":   "GM이 수동으로 재계획을 요청했습니다. 현재 상황을 재평가하여 계획 전체를 갱신하세요.",
            }
            trigger_context = trigger_context_map.get(trigger_reason, f"재계획 요청 ({trigger_reason})")

        context_note_block = f"\n[GM 추가 메모 (계획에 반영하세요)]\n{context_note}\n" if context_note else ""

        user_prompt = (
            f"[서사 계획 수립 요청]\n"
            f"트리거: {trigger_context}\n\n"
            f"[시나리오 기반 정보]\n"
            f"세계관: {worldview if worldview else '(없음)'}\n"
            f"스토리 가이드: {story_guide if story_guide else '(없음)'}\n\n"
            f"[현재 게임 상황]\n"
            f"진행 턴: {_turn_no}\n"
            f"PC 상태:\n{pc_info}\n"
            f"압축 기억:\n{memory_str}\n\n"
            f"[최근 게임 로그]\n{recent_logs_str}\n"
            f"{existing_plan_block}"
            f"{context_note_block}\n"
            "[출력 지시]\n"
            "위 정보를 바탕으로 GM이 활용할 서사 계획을 JSON 스키마에 맞게 수립하십시오."
        )


        try:
            config = types.GenerateContentConfig(
                http_options=_resilience.provider_http_options("instruction"),  # WP-C B-C4
                system_instruction=NARRATIVE_PLANNER_SYSTEM_INSTRUCTION,
                temperature=0.5,
                response_mime_type="application/json",
                response_schema=NARRATIVE_PLAN_SCHEMA,
                safety_settings=_constants.TRPG_SAFETY_SETTINGS,
            )
            _cl_op = _cost_ledger.begin_operation(
                self.bot, _cost_ledger.OP_TURN_NARRATIVE_PLANNING, session=session,
                model=_constants.LOGIC_MODEL, actor_kind=_cost_ledger.ACTOR_PLAYER,
                billing_hint=_cost_ledger.HINT_PLAYER_CANDIDATE)
            if preparation is not None:
                preparation.claim_cost_operation(_cl_op)
            _ok, response = await self.rt.call_with_retry(
                lambda: asyncio.to_thread(
                    self.bot.genai_client.models.generate_content,
                    model=_constants.LOGIC_MODEL,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=user_prompt)])],
                    config=config,
                ),
                layer="instruction", session_id=session.session_id,
                on_attempt_result=_cl_op.on_attempt, operation_id=_cl_op.operation_id,
            )
            if not _ok:
                raise RuntimeError("AI 호출 실패")
        except Exception as e:
            print(f"[GM] 서사 계획 호출 실패: {type(e).__name__} - {e}")
            if master_ch:
                await master_ch.send(f"⚠️ 서사 계획 수립 실패: {type(e).__name__}")
            return None

        # ── 비용 정산 ──
        try:
            meta         = response.usage_metadata
            in_tokens, out_tokens, cached_tokens, thought_tokens = _cost.extract_token_usage(meta)
            breakdown    = _cost.calculate_text_gen_cost_breakdown(
                _constants.LOGIC_MODEL,
                input_tokens=in_tokens,
                output_tokens=out_tokens,
                cached_read_tokens=cached_tokens,
            )
            cost = breakdown["total_krw"]
            self.rt.accrue(session, cost, breakdown["total_usd"])
            _cl_op.record(
                input_tokens=in_tokens, cached_input_tokens=cached_tokens,
                output_tokens=out_tokens, thought_tokens=thought_tokens,
                cost_usd=breakdown["total_usd"], cost_krw=cost,
                usage_source=_cost_ledger.SOURCE_PROVIDER_METADATA)
            _io.write_cost_log(
                session.session_id,
                f"{COST_LOG_PREFIX}서사 계획 수립",
                in_tokens, cached_tokens, out_tokens, cost, session.total_cost
            )
            print(
                f"[GM/{session.session_id}] 서사 계획 비용: "
                f"In={in_tokens:,} Out={out_tokens:,} → {_cost.format_cost(cost)}"
            )
            # 턴 진행 배치 로그에 누적 (PROCEED 직전 플러시)
            if not hasattr(session, "turn_cost_log"):
                session.turn_cost_log = []
            session.turn_cost_log.append({"label": "서사 설계자(계획)", "cost": cost,
                                          "in": in_tokens, "cached": cached_tokens, "out": out_tokens})
        except Exception as e:
            print(f"[GM] 서사 계획 비용 정산 실패: {e}")

        # ── JSON 파싱 ──
        raw_text = response.text or ""
        try:
            plan = json.loads(raw_text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
            try:
                plan = json.loads(cleaned)
            except Exception as e:
                _logs.event("turn", "서사 계획 JSON 파싱 실패", level="WARN", session=session, exc=e,
                                data={"code": "PLAN_JSON", "raw": raw_text})
                if master_ch:
                    await master_ch.send("⚠️ 서사 계획 JSON 파싱 실패. 기존 계획을 유지합니다.")
                return None

        # ── 구조 검증·정규화 → 후보(result-only; canonical 미변경) ──
        candidate = _turn_preparation.build_narrative_plan(
            plan, trigger_reason=trigger_reason, full_replan=full_replan,
            transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt)
        if candidate.normalized is None:
            print(f"[GM] 서사 계획 구조 불량: {candidate.rejected}")
            if master_ch:
                await master_ch.send("⚠️ 서사 계획 구조가 올바르지 않습니다. 기존 계획을 유지합니다.")
            return None
        return candidate
