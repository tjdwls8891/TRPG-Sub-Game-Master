"""자동 턴 진행 단계 중 디스코드·provider를 직접 쓰지 않는 것(세계 계획·관찰·추출 준비·커밋 효과 등) — GMCog 믹스인.

cogs/gm.py에서 본문 그대로 옮겼다(core 공개 이름 공간 사용). 턴 서비스(indaim/turn)로 하나씩 옮길 후보다."""
from __future__ import annotations

import re
import json
import copy
import dataclasses
import core

from indaim.turn.brief import _clean_proceed_instruction
from indaim.adapters.discord.gm_config import _OUTCOME_DELIVERY_FAILED, _OUTCOME_EXTRACTION_FAILED, _OUTCOME_NOT_READY, _OUTCOME_NO_NARRATION, _OUTCOME_READY, _OUTCOME_SUPERSEDED


class TurnSteps:
    async def _start_round(self, session):
        """
        PROCEED 완료 후(또는 자동시작 직후) 호출. 모든 대상 PC에게 순서대로 행동을 묻는 라운드 시작.
        gm_target_chars가 비어 있으면 아무 동작도 하지 않음.
        """
        if not getattr(session, "gm_active", False):
            return
        target_chars = getattr(session, "gm_target_chars", [])
        if not target_chars:
            return

        session.gm_pending_players = list(target_chars)
        session.gm_collected_actions = {}
        session.gm_waiting_for = None
        session.gm_narrate_count = 0
        # 저장은 바로 이어지는 _ask_next_player가 처리 (중복 제거).
        # 여기서 초기화한 세 필드는 SESSION_RESET_FIELDS라 크래시 시에도 재시작 후 어차피 초기화됨.
        await self._ask_next_player(session)

    async def _voice_lines(self, session, lines, *, transaction_id, kind: str = "master") -> None:
        """P2 Q7: 액자 밖 마스터 직접 발화(ASK·경로/목적지 질문·게이트 안내)와 NARRATE 본문을 더빙한다.

        TTS 실효(설정 켜짐 ∧ 음성 채널 연결)일 때만 합성한다 — 꺼짐·미연결이면 provider 호출 0(P2 #9).
        이 턴 트랜잭션의 비용 멤버로 claim해 같은 Settlement로 정산한다(판단층위 ASK 비용과 같은 귀속).
        트랜잭션이 없는 안내(라운드 차례 안내 등)는 정산 귀속이 없어 더빙하지 않는다.
        """
        on, live, _r = core.media_control.tts_status(session)
        if not (on and live) or not lines or transaction_id is None:
            return
        game_cog = self.bot.get_cog("GameCog")
        if game_cog is None or not hasattr(game_cog, "_synthesize_and_enqueue"):
            return
        VP = core.voice_profile
        items = []
        for t in lines:
            if kind == "master":
                txt = core.clean_text_for_tts(t)
                if txt:
                    items.append((VP.master_voice(session), VP.style_for(VP.KIND_MASTER), txt))
            else:
                v, st, sp, _k = VP.paragraph_voice(session, t)
                if sp:
                    items.append((v, st, sp))
        if not items:
            return
        try:
            dub = await game_cog._synthesize_and_enqueue(session, items, claim_tx=transaction_id)
        except Exception as e:  # noqa: BLE001 — 음성 실패는 턴 진행을 막지 않는다
            print(f"[TTS] 진행자 안내 더빙 실패(진행 유지): {type(e).__name__}: {e}")
            return
        if dub.get("cost", 0) > 0:
            core.accrue(session, dub["cost"], dub.get("usd"))
            session.turn_cost_log = list(getattr(session, "turn_cost_log", None) or []) + [{
                "label": f"TTS {'진행자 안내' if kind == 'master' else 'NARRATE'}({dub['enqueued']}/{dub['total']})",
                "cost": dub["cost"], "in": dub["in"], "cached": 0, "out": dub["out"], "model": core.TTS_MODEL}]












    @staticmethod
    def _revealed_identity_ids(session, prep, actor_ids, kind) -> tuple:
        """P3 G3: 식별 후보 중 본문에서 실제 공개가 확인된 InfoId만 정본 인지로 — 이미 아는 것은 그대로(멱등)."""
        if not core.speaker_card.card_world(session):
            return ()
        ids = core.speaker_card.identity_info_ids(core.speaker_card.config_of(session.scenario_data), actor_ids, kind)
        shown = set(getattr(prep, "identity_reveals", None) or {})
        return tuple(i for i in ids if i in shown)

    async def _release_turn_processing(self, session, prep) -> None:
        """자동 턴이 보유한 처리 잠금·채팅 잠금을 배리어/continuation 이후 해제한다."""
        if prep is None or not getattr(prep, "processing_held", False):
            return
        prep.processing_held = False
        game_cog = None
        try:
            game_cog = self.bot.get_cog("GameCog")
        except Exception:
            game_cog = None
        if game_cog is not None and hasattr(game_cog, "release_turn_processing"):
            await game_cog.release_turn_processing(session)
        else:
            session.is_processing = False

    def _observe_or_item_rejection(self, session, prep, plan, master_ch) -> str | None:
        """관찰 처리 — 물건 연산이 정본 검증에서 거부되면 그 사유(그 밖의 예외는 그대로 전파)."""
        try:
            self._process_world_observation(session, prep, plan, master_ch)
            return None
        except core.items.ItemContractError as e:
            return str(e)

    async def _join_after_exception(self, session, prep) -> str:
        try:
            await prep.join()
        except Exception:
            pass
        prep.seal()
        try:
            prep.close_cost_membership()
        except Exception as e:
            prep.violations.append(f"cost_close:{e}")
        if not prep.matches(core.turn_transaction.get_active_transaction(session)):
            return _OUTCOME_SUPERSEDED
        return _OUTCOME_NOT_READY if prep.narration is not None else _OUTCOME_NO_NARRATION

    async def _join_and_ready(self, session, prep) -> str:
        """모든 준비 작업을 합류시키고 배리어 결과를 판정한다(S6/S7)."""
        TP = core.turn_preparation
        await prep.join()
        prep.seal()
        if not prep.matches(core.turn_transaction.get_active_transaction(session)):
            # 더 새로운 시도가 현재 — 결과는 어떤 배리어도 만족하지 못하며 흐름을 소유하지 않는다.
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
            return _OUTCOME_SUPERSEDED
        if prep.narration is None:
            outcome = _OUTCOME_NO_NARRATION
        elif prep.delivery is None or not getattr(prep.delivery, "ok", False):
            outcome = _OUTCOME_DELIVERY_FAILED
        else:
            ext = prep.tasks.get("extraction")
            if (ext is None or ext.state != TP.TASK_SUCCEEDED
                    or prep.extraction_plan is None
                    or getattr(prep.extraction_plan, "rejected_stale", False)):
                outcome = _OUTCOME_EXTRACTION_FAILED
            else:
                try:
                    prep.close_cost_membership()
                except TP.BarrierViolationError as e:
                    prep.violations.append(f"cost_close:{e}")
                _wf = self._stage_world_turn(session, prep)
                if _wf is not None:
                    prep.failure_code, prep.failure_stage = _wf, "WORLD"
                    return _OUTCOME_NOT_READY
                proof, reasons = TP.transition_to_ready(session, prep)
                if proof is not None:
                    print(f"[WP-C/{prep.transaction_id[:8]}] READY_TO_COMMIT "
                          f"logical={prep.logical_turn} attempt={prep.attempt} "
                          f"cost_events={len(proof.frozen_cost_event_ids)} "
                          f"canonical_unchanged={proof.canonical_unchanged}")
                    return _OUTCOME_READY
                core.logs.event("turn", "READY 불가", level="WARN", session=session,
                                data={"code": "READY_REJECTED", "tx": prep.transaction_id, "reasons": list(reasons or ())})
                prep.failure_stage = "READY_PREDICATE"
                outcome = _OUTCOME_NOT_READY
        if outcome != _OUTCOME_EXTRACTION_FAILED:
            # 종료 실패 — 비용 멤버십을 동결해 둔다(FAILED_SYSTEM은 청구 0, 사실은 보존).
            try:
                prep.close_cost_membership()
            except TP.BarrierViolationError as e:
                prep.violations.append(f"cost_close:{e}")
        return outcome

    async def _commit_ready_turn(self, session, prep, *, state_before=None):
        """READY_TO_COMMIT 시도를 단일 커밋 owner(CommitCoordinator)로 넘긴다."""
        coord = core.commit_coordinator.CommitCoordinator(self.bot)
        try:
            return await coord.commit_ready_turn(
                session, prep, apply_effects=self._apply_commit_effects,
                state_before=state_before)
        except Exception as e:
            # 코디네이터 자체의 예기치 못한 예외 — 정본 영속 여부를 저널로만 판정한다.
            print(f"[WP-D/{prep.transaction_id[:8]}] 커밋 owner 예외: {type(e).__name__}: {e}")
            session.commit_recovery = {"transaction_id": prep.transaction_id,
                                       "status": "PENDING", "stage": "COORDINATOR_EXCEPTION",
                                       "error": f"{type(e).__name__}: {e}"}
            return core.commit_coordinator.CommitResult(
                core.commit_coordinator.CommitOutcome.RECOVERY_PENDING,
                reason="COORDINATOR_EXCEPTION", error=str(e))

    async def _apply_commit_effects(self, session, prep, derived) -> None:
        """커밋 영속 전 도메인 적용 — 정본 메모리 변이만(Discord/통계/세션 저장 없음).

        적용 순서(WP-C 순서 보존): 비정규 NPC 등록·승격 → 진행 이력 → progress →
        지시효과 → 재계획 마커 → 추출 → 자동 재계획. 묘사 로그·ROLL 성장·카운터·
        Settlement 미러·되감기 델타·커밋 표식은 CommitCoordinator가 적용한다.
        파생 알림은 derived에만 쌓여 durable COMMITTED 이후 방출된다.
        """
        # WP-D(D-D1): 이 경로의 정본 변이 예외는 삼키지 않는다 — CommitCoordinator까지
        #   전파되어 롤백 스냅샷이 전체 precommit 상태를 복원한다(영속 전 실패). 파생 출력
        #   (Discord/통계)은 derived 기술자일 뿐이며 COMMITTED 이후에만 방출·실패한다.
        TP = core.turn_preparation
        # WP-F: 커밋된 ROLL 성장의 확정 알림(CANONICAL_GAME_EVENT) — 성장은 코디네이터가
        #   이미 정본에 적용했다. derived 로만 쌓여 durable COMMITTED 이후 송출 의도와 함께 나간다.
        # P1(#1): 본문 전달 대기 — 관찰 처리 결과(이번 본문이 전하지 못한 신호 · 지난 대기 중 미전달분)로 교체.
        #   세계 연산과 같은 커밋 단위(롤백 스냅샷·strict 저장 안). 동적 세계 턴만 쓴다.
        _wt = getattr(prep, "world_turn", None)
        _res = getattr(_wt, "result", None) if _wt is not None else None
        if _res is not None and _res.status == core.turn_world.TW_READY:
            session.world_pending_signals = [dict(r) for r in _res.pending]
        if derived is not None:
            for _line in list(getattr(prep, "growth_notices", None) or []):
                derived.game(_line)
        if prep.irregular_plan is not None and not prep.irregular_plan.applied:
            await self._apply_irregular_npc_plan(
                session, prep.irregular_plan, prep.irregular_text, None,
                staged_promotions=prep.promotions, derived=derived)

        if prep.proceed_history_entry is not None:
            if not hasattr(session, "gm_proceed_history"):
                session.gm_proceed_history = []
            entry = dict(prep.proceed_history_entry)
            entry["turn_num"] = session.turn_count
            session.gm_proceed_history.append(entry)
            if len(session.gm_proceed_history) > 5:
                session.gm_proceed_history = session.gm_proceed_history[-5:]

        if prep.narrative_progress:
            TP.apply_narrative_progress(session, prep.narrative_progress)

        _applied = TP.apply_instruction_effects(session, TP.pending_for(session))
        if (_applied["applied"]
                and _applied["quest_action"] in ("start", "switch")
                and _applied["quest_active_name"]):
            verb = "전환" if _applied["quest_action"] == "switch" else "선정"
            derived.master(
                f"📜 **[퀘스트 {verb}]** {_applied['quest_active_name']}\n"
                f"> {_applied['quest_reason']}")

        if prep.narrative_marker:
            _plan = getattr(session, "narrative_plan", None)
            if isinstance(_plan, dict) and _plan:
                _plan["last_planned_turn"] = session.turn_count
                session.narrative_plan = _plan

        await self._apply_prepared_extraction(session, prep, derived=derived)

        if prep.replan_candidate is not None:
            await self._commit_replan_candidate(
                session, prep.replan_candidate,
                logical_turn=prep.logical_turn, attempt=prep.attempt,
                derived=derived)

    async def history_rewind(self, session, target: int) -> dict:
        """모든 되감기 입구(!되감기·디스플레이·rewind:one)의 단일 어댑터."""
        async with self._lock_for(session):
            core.turn_history.try_flush(session)        # E-E2 보전 매핑 먼저 인덱스로
            result = await core.turn_history.rewind(self.bot, session, target)
            if not result.get("ok"):
                return result
            # E-E2: REWIND 이벤트에 실린 durable 정리 부채를 실행(실패 시 부채 유지·재시도)
            result["removed_messages"] = await self._drain_history_cleanup(session)
        try:
            await core.refresh_display(self.bot, session, reason="rewind")
        except Exception as e:
            print(f"[디스플레이] 되감기 갱신 실패: {e}")
        return result

    async def _fail_rerender_before_narration(self, session, tid, master_ch) -> None:
        """묘사 이전 실패 — 교체 시도를 FAILED_SYSTEM(청구 0)으로 종결하고 이전 선택 유지."""
        TP = core.turn_preparation
        prep = TP.get_preparation(session, tid)
        if prep is not None:
            try:
                await prep.join()
                prep.seal()
                prep.close_cost_membership()
            except Exception as e:
                prep.violations.append(f"cost_close:{e}")
            prep.phase = TP.PREP_FAILED
        core.turn_transaction.finalize(
            session, tid, core.turn_transaction.TurnStatus.FAILED_SYSTEM,
            failure_stage="RERENDER_INSTRUCTION",
            failure_code=core.turn_transaction.FailureCode.INSTRUCTION_PROVIDER_FAILURE)
        if prep is not None:
            await self._settle_failed_turn(session, prep, master_ch)
        if core.turn_history.rerender_op_for(session.session_id, tid):
            await self._abort_rerender(session, master_ch)

    def _round_eligible(self, session) -> bool:
        """새 자동 라운드를 열 자격(복구 이후에만 평가) — 활성·턴 한도·비용 한도."""
        if not getattr(session, "gm_active", False):
            return False
        cap = getattr(session, "gm_turn_cap", None)
        if cap is not None and session.gm_turns_done >= cap:
            return False
        if core.auto_cost_cap_reached(self.bot, session):   # WP-G: CostLedger 기준 운영 예산
            return False
        return True

    async def _recover_then_maybe_round(self, session, master_ch) -> bool:
        """복구 우선 admission(D-D4): 복구 성공 후에만 eligibility로 새 라운드 여부를 정한다."""
        if not await self._resume_commit_recovery(session, master_ch):
            return False
        if self._round_eligible(session):
            await self._start_round(session)
        return True

    async def _forced_proceed_instruction(self, session, player_message: str,
                                          roll_results: list, master_ch,
                                          sim_result: dict | None = None,
                                          *, transaction_id: str | None = None) -> str:
        """
        ASK/NARRATE 한도 초과 또는 ROLL 폴백으로 강제 PROCEED 전환할 때,
        지시층위를 호출해 묘사 지시문을 확보한다.

        NOTE: 층위 분리 이후 판단층위 결정에는 proceed_instruction이 없다.
              지시문 없이 진행하면 묘사 품질이 급락하므로 여기서 별도 확보한다.
              호출 실패 시에만 범용 문구로 폴백한다.
        """
        fallback = "현재 상황에서 자연스럽게 다음 묘사를 이어가십시오."
        decision = await self._call_gm_logic(
            session, player_message, roll_results, master_ch,
            sim_result=sim_result, action="PROCEED",
            transaction_id=transaction_id)
        if not decision:
            return fallback
        # WP-C(B-C2): 강제 PROCEED 결정의 지시효과(quest_choice/intended_case/info_access)도
        #   기존 단일 owner로 스테이징한다(유실 금지). 정본 적용은 READY 이후에만.
        if transaction_id is not None:
            core.turn_preparation.stage_instruction_effects(
                session, {**decision, "action": "PROCEED"}, transaction_id=transaction_id)
        return _clean_proceed_instruction(decision.get("proceed_instruction") or fallback)

    def _stage_npc_promotions(self, session, prep, plan, text, master_ch=None) -> list:
        """WP-C: 승격 판정을 투영으로 재현하고 세부 생성을 자식 준비 작업으로 등록한다.

        기존 _apply_irregular_npc_plan의 순서(등록 → 등장 누적 → should_promote → 세부
        생성)를 정본 변경 없이 투영 등록부에서 계산한다. 자식은 부모(배정) 작업이
        끝나기 전에 등록되므로 배리어 합류가 누락하지 않는다.
        """
        TP = core.turn_preparation
        IRR = core.irregular_npc
        turn = plan.registrations[0]["turn"] if plan.registrations else (
            prep.staged_log["turn_no"] if isinstance(prep.staged_log, dict)
            else getattr(session, "turn_count", 0))
        reg = copy.deepcopy(IRR.get_registry(session))
        for r in plan.registrations:
            if r["name"] not in reg:
                reg[r["name"]] = TP.staged_irregular_entry(session, r["name"]) or {}
        launched = []
        for name in list(reg.keys()):
            if name not in (text or ""):
                continue
            entry = reg[name]
            seen = list(entry.get("seen_turns") or [])
            if turn not in seen:
                seen.append(turn)
                entry["seen_turns"] = seen[-20:]
                entry["appearances"] = len(seen)
            if entry.get("detailed"):
                continue
            if entry.get("appearances", 0) < IRR.PROMOTE_APPEARANCES:
                continue
            prep.register_task(
                f"npc_promotion:{name}", TP.TASK_NPC_PROMOTION,
                coro=self._generate_npc_detail(
                    session, name, text, master_ch, preparation=prep, entry=dict(entry)),
                parent="irregular_npc")
            launched.append(name)
        return launched

    async def _init_narrative_and_start(self, session):
        """
        !자동 시작 직후 호출. 서사 계획이 없으면 새로 수립한 뒤 첫 라운드를 시작한다.
        이미 계획이 있으면 수립 없이 바로 라운드를 시작한다.
        백그라운드 태스크로 실행됨.
        """
        if not session.narrative_plan:
            await self._plan_narrative(session, "init")
        await self._start_round(session)

    async def _prepare_auto_replan(self, session, prep, trigger_reason, *,
                                   full_replan: bool, context_note: str = ""):
        """WP-C 자동 재계획 준비 작업 — 후보 생성(result-only) + stale 검사 → 스테이징."""
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan,
            transaction_id=prep.transaction_id, logical_turn=prep.logical_turn,
            attempt=prep.attempt, preparation=prep)
        if candidate is None:
            return None
        if (not prep.matches(core.turn_transaction.get_active_transaction(session))
                or core.turn_preparation.superseded_by_newer_attempt(
                    session, logical_turn=prep.logical_turn, attempt=prep.attempt)):
            candidate.rejected_stale = True
            print(f"[서사설계/{session.session_id}] stale 재계획 준비 폐기")
            return None
        prep.replan_candidate = candidate
        return candidate

    async def _plan_narrative(self, session, trigger_reason: str = "init",
                              context_note: str = "", full_replan: bool = True) -> bool:
        """
        서사 계획 수립 — **setup(init)·수동/운영자(manual) 경로 전용** consumer.

        세션 시작(_init_narrative_and_start)과 `!자동 재계획`(replan_narrative)이
        부른다. 자동 턴 TurnTransaction 의미(원인 시도·stale·멱등)를 요구하지 않으며
        기존 제품 의미(성공 시 버전 증가·저장·보고, 실패 시 기존 계획 유지)를 보존한다.
        자동 턴 재계획은 _auto_replan_narrative를 쓴다(AUD-065).

        Args:
            session: TRPGSession
            trigger_reason: "init" | "manual"
            context_note: GM이 추가한 메모 (재계획 시 계획 수립 프롬프트에 포함)
            full_replan: True이면 mid_plan 포함 전부 재수립.
                         False이면 mid_plan을 유지하고 순간 계획만 갱신.

        Returns:
            bool: 성공 여부
        """
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan)
        if candidate is None:
            return False
        return await self._apply_narrative_plan(session, candidate)

    async def _auto_replan_narrative(self, session, trigger_reason: str, *,
                                     full_replan: bool, context_note: str = "",
                                     transaction_id=None, logical_turn=None,
                                     attempt=None) -> bool:
        """자동 턴 서사 재계획(AUD-065) 비준비 경로 — 원인 정체성은 스케줄 시점 복사값.

          provider → 정규화 후보 → stale guard → 멱등 guard → 호환 적용.
        WP-C 자동 턴은 _update_narrative_progress가 준비 작업(_prepare_auto_replan)으로
        돌리고 READY 이후 _commit_replan_candidate로 적용한다. 이 함수는 그 적용 규칙
        (stale/멱등)을 공유하는 비준비 호환 경로로 남는다.
        """
        candidate = await self._generate_narrative_plan_candidate(
            session, trigger_reason, context_note=context_note, full_replan=full_replan,
            transaction_id=transaction_id, logical_turn=logical_turn, attempt=attempt)
        if candidate is None:
            return False
        return await self._commit_replan_candidate(
            session, candidate, logical_turn=logical_turn, attempt=attempt)

    async def _commit_replan_candidate(self, session, candidate, *,
                                       logical_turn=None, attempt=None,
                                       derived=None) -> bool:
        """정규화 재계획 후보를 stale/멱등 검사 후 호환 적용한다(단일 적용 규칙)."""
        # stale guard(§26/§38): 이 재계획을 일으킨 시도보다 더 새로운 시도가 활성이면 폐기.
        if core.turn_preparation.superseded_by_newer_attempt(
                session, logical_turn=logical_turn, attempt=attempt):
            candidate.rejected_stale = True
            print(f"[서사설계/{session.session_id}] stale 재계획 폐기 — 더 새로운 논리 시도 활성 "
                  f"(원인 turn={logical_turn}, attempt={attempt})")
            return False

        # 멱등 guard: 같은 (원인 시도, 사유, 모드) 재계획은 canonical에 한 번만 적용.
        #   검사와 기록 사이에 await가 없으므로 중복 태스크가 동시에 도착해도 한 번만 통과.
        key = core.turn_preparation.narrative_replan_key(candidate)
        applied_keys = getattr(session, "_narrative_replan_applied", None)
        if applied_keys is None:
            applied_keys = session._narrative_replan_applied = []
        if key and key in applied_keys:
            print(f"[서사설계/{session.session_id}] 이미 적용된 재계획 — 중복 적용 방지 ({key})")
            return False
        if key:
            applied_keys.append(key)
            if len(applied_keys) > 16:
                del applied_keys[:-16]

        return await self._apply_narrative_plan(session, candidate, derived=derived)
