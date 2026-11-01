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

    def _world_turn(self, session, transaction_id, *, create=False):
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is None or transaction_id is None or tx.transaction_id != transaction_id:
            return None
        wt = getattr(tx, "world_turn", None)
        if wt is not None and core.action_intent.is_definition_stale(wt.ctx.definition_revision, session):
            # R2: 정의 revision이 바뀌었다 — 옛 매니페스트·계획·경로 선택을 같은 시도 재진입에서도 재사용하지 않는다
            core.write_log(session.session_id, "api",
                           f"[H05 정의 변경] 옛 WorldTurn 폐기 rev={wt.ctx.definition_revision} "
                           f"→ {getattr(session, 'definition_revision', None)}")
            tx.world_turn = wt = None
        if wt is None and create:
            ctx = core.player_world.session_world_context(session)
            if not ctx.dynamic:
                return None
            wt = core.turn_world.WorldTurn(ctx=ctx)
            tx.world_turn = wt
        return wt

    def _tx_judgment(self, session, transaction_id) -> dict | None:
        tx = core.turn_transaction.get_active_transaction(session)
        if tx is None or transaction_id is None or tx.transaction_id != transaction_id:
            return None
        return tx.judgment_result or None

    def _gate_inputs(self, session, transaction_id) -> dict:
        """v7.14 선언 게이트 입력 — DYNAMIC만: PC 정본 장소·표시명·유효 발견(D키)·찾지 못한 이전 탐색."""
        out = {"dynamic": False, "place": None, "place_name": None, "rows": [], "failed": [], "finds": []}
        wt = self._world_turn(session, transaction_id, create=True)
        if wt is None:
            return out
        ctx = wt.ctx
        place = core.turn_world.pc_place(ctx, session)
        out["dynamic"] = True
        if not place:
            return out
        out["place"] = place
        try:
            out["place_name"] = core.turn_world.pc_place_name(ctx, session, place)
            out["rows"] = core.turn_world.discoveries_at(ctx.definition, session, ctx.lead_pc, place)
            out["failed"] = core.turn_history.recent_failed_discoveries(session.session_id, place)
            out["finds"] = core.turn_world.find_object_names(ctx.definition, session, ctx.lead_pc, place)
        except Exception as e:
            print(f"[선언 게이트] 입력 조립 실패(발견 목록 생략): {e}")
        return out

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

    def _build_world_manifest(self, session, wt) -> None:
        ctx = wt.ctx
        uid = ctx.uid_of.get(ctx.lead_pc)
        routes = wt.plan.route_options if (wt.plan is not None and wt.plan.status
                                           == core.player_world.PLAN_NEEDS_CHOICE) else ()
        access = core.player_world.access_confirm_options(
            ctx.definition, session, ctx.lead_pc,
            core.player_world.pc_access_capabilities(session, uid, ctx.definition))   # v7.17.2: 소속 포함(접근 전용)
        pend = self._pending_move(session, wt)
        wt.manifest = core.action_intent.build_manifest(
            ctx.definition, session, ctx.lead_pc, definition_revision=ctx.definition_revision,
            route_options=routes, access_options=access, asked_exits=pend["exits"] if pend else ())

    @staticmethod
    def _pending_move(session, wt):
        """v7.22 목적지 질문 문맥 — 같은 시도(WorldTurn)·같은 장소·같은 world_version일 때만 유효(아니면 버린다).
        재시작·다른 세션·다른 시도에는 남지 않는다(트랜잭션 런타임 기록)."""
        pend = wt.pending_move
        if pend is None:
            return None
        now = core.turn_world.move_question_anchor(wt.ctx, session)
        if pend.get("place") != now["place"] or pend.get("world_version") != now["world_version"]:
            wt.pending_move = None
            return None
        return pend

    def _settle_move_intent(self, session, wt, transaction_id) -> None:
        """v7.22: 판단 의도의 출구 이동을 확정한다(판단 직후, 이력 기록 전 — 재서술·재시도는 같은 구간).

        · 질문 문맥이 있는데 다시 길을 정하지 않은 이동(목적지 미정·WANDER)이면 되묻지 않고 한 구간 간다 —
          질문이 제시한 후보 안에서만. 질문 없이 WANDER면 현재 출구 전체(조건 키가 있으면 그 안).
        · 출구가 정해지면·다른 행동이면 질문 문맥을 내린다. 모호한 지목(AMBIGUOUS)은 그 후보로 다시 묻는다."""
        intent = wt.intent
        if intent is None or intent.stale:
            return
        pend = self._pending_move(session, wt)
        mv = intent.movement
        move_open = [(f, c) for f, c in intent.unresolved if f in ("destination", "exit")]
        unspecified = (mv is not None and mv.kind == core.action_intent.MOVE_WANDER and not mv.edge_id) or (
            pend is not None and move_open and all(u == ("destination", "MISSING") for u in move_open))
        if unspecified:
            pool = tuple(mv.pool) if mv is not None and mv.kind == core.action_intent.MOVE_WANDER else ()
            if pend is not None:
                asked = set(pend["exits"])
                pool = tuple(e for e in pool if e in asked) or tuple(sorted(asked))   # 질문 범위를 넓히지 않는다
            pick = core.player_world.wander_exit(
                wt.ctx.definition, session, wt.ctx.lead_pc, pool=pool, transaction_id=transaction_id,
                narrative_seed=getattr(session, "narrative_seed", None) or session.session_id)
            wt.intent = intent = dataclasses.replace(
                intent, movement=core.action_intent.MovementIntent(
                    core.action_intent.MOVE_WANDER, pick[1] if pick else None, pick[0] if pick else None),
                exit_choices=(), unresolved=tuple(u for u in intent.unresolved if u not in move_open))
            core.write_log(session.session_id, "api", f"[v7.22 출구 선택] pool={len(pool)} pick={pick}")
        if not any(f in ("destination", "exit") for f, _c in intent.unresolved):
            wt.pending_move = None

    def _plan_world(self, session, wt, transaction_id, *, resolved_tie_orders=None):
        ctx = wt.ctx
        intent = wt.intent or core.action_intent.ResolvedActionIntent()
        plan = self._plan_world_core(session, wt, transaction_id, intent, resolved_tie_orders)
        # v7.25(F03): 턴 시작에 PC와 함께 있던 동행은 같은 계획(묘사 전)에서 PC와 함께 움직인다
        return core.turn_world.companion_follow(
            ctx, session, plan, list(getattr(session, "companions", None) or ()),
            prefix=core.turn_world.turn_op_prefix("cmpf", transaction_id))

    def _plan_world_core(self, session, wt, transaction_id, intent, resolved_tie_orders):
        ctx = wt.ctx
        return core.player_world.plan_player_turn(
            ctx, session, intent, transaction_id=transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            capabilities=core.player_world.pc_capabilities(session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),
            access_capabilities=core.player_world.pc_access_capabilities(
                session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),                     # v7.17.2: 접근 판정 전용
            item_facts=core.items.current_facts(session),           # v7.17: 직전 커밋 턴의 사실만
            stats=core.player_world.pc_stats(session, ctx.uid_of.get(ctx.lead_pc)),       # H07: 스탯 조건
            statuses=core.player_world.pc_statuses(session, ctx.uid_of.get(ctx.lead_pc)),  # v7.48(E1): 상태 조건
            pursuit=core.pursuit.context(ctx.definition, core.get_merged_status_effects(session.scenario_data or {}),
                                         session.statuses, self._pc_name(session, ctx)),   # v7.48(E3): 추적자
            combat_deaths=core.turn_world.combat_deaths(                                    # v7.50(E6): 전투 쓰러짐
                ctx.definition, session, (self._tx_judgment(session, transaction_id) or {}).get("roll_records") or ()),
            resolved_tie_orders=resolved_tie_orders)

    def _ensure_world_plan(self, session, wt, transaction_id):
        cur = core.action_intent.world_version_id(session)
        if wt.plan is None or wt.plan.source_world_version_id != cur or \
                core.action_intent.is_definition_stale(wt.plan.definition_revision, session):
            wt.plan = self._plan_world(session, wt, transaction_id)
            wt.replan = lambda orders, _s=session, _w=wt, _t=transaction_id: self._plan_world(
                _s, _w, _t, resolved_tie_orders=orders)
            core.write_log(session.session_id, "api",
                           f"[H05 계획] status={wt.plan.status} reason={wt.plan.reason} "
                           f"outcome={json.dumps(wt.plan.outcome, ensure_ascii=False, default=str)[:600]}")
        return wt.plan

    def _route_number_answer(self, session, transaction_id, player_message):
        """v7.23: 대기 중인 경로 질문에 번호만 답했다('1', '1번') → 그 질문의 선택지 스냅샷으로 고른 판단(재호출 없음).

        번호 밖·다른 말이 섞임·기준선(장소·world_version)이 바뀜·직전 판단 없음이면 None(일반 판단으로)."""
        wt = self._world_turn(session, transaction_id)
        pend = getattr(wt, "pending_route", None) if wt is not None else None
        if not pend or wt.intent is None:
            return None
        m = re.fullmatch(r"\s*(\d{1,2})\s*(?:번|번째)?\s*[.!]?\s*", str(player_message or ""))
        if not m:
            return None
        anchor = core.turn_world.move_question_anchor(wt.ctx, session)
        sigs = pend.get("signatures") or []
        n = int(m.group(1))
        prior = (self._tx_judgment(session, transaction_id) or {}).get("judgment")
        if (anchor["place"] != pend.get("place") or anchor["world_version"] != pend.get("world_version")
                or not 1 <= n <= len(sigs) or not isinstance(prior, dict)):
            wt.pending_route = None                        # 낡은 번호는 다시 쓰지 않는다
            return None
        wt.pending_route = None
        wt.intent = dataclasses.replace(wt.intent, route_signature=tuple(sigs[n - 1]))
        wt.plan = None
        core.turn_transaction.mark_transaction_status(
            session, transaction_id, core.turn_transaction.TurnStatus.JUDGING)
        core.logs.event("turn", "경로 질문 번호 응답", session=session,
                        data={"stage": "route_answer", "tx": transaction_id, "choice": n, "options": len(sigs)})
        return {k: v for k, v in prior.items() if k != core.action_intent.INTENT_FIELD}

    def _attach_world_turn(self, session, prep, transaction_id):
        """PROCEED 배리어 소유 시작 — 계획을 준비 객체에 붙이고 묘사용 투영 장면을 만든다."""
        wt = self._world_turn(session, transaction_id)
        if wt is None:
            return None
        plan = self._ensure_world_plan(session, wt, transaction_id)
        if plan.status == core.player_world.PLAN_NEEDS_CHOICE:
            # 선택을 물을 수 없는 경로(ROLL 후·강제 PROCEED) — 이동 없이 묘사(선택은 다음 선언에서).
            plan.status, plan.reason = core.player_world.PLAN_REJECTED, "ROUTE_CHOICE_PENDING"
        if plan.status == core.player_world.PLAN_TIE:
            # P1(D2): 묘사 전에 같은 분 선후를 정한다 — 지시층위 선택(허용 키) 우선, 무효·미선택은 코드 결정
            plan, rec = core.turn_world.resolve_plan_tie(plan, wt.replan, wt.tie_choice, wt.ctx.definition, wt.ctx)
            wt.plan = plan
            core.write_log(session.session_id, "api",
                           f"[P1 선후] {json.dumps(rec, ensure_ascii=False)} → status={plan.status}")
        prep.world_turn = wt
        if plan.projected_world is not None:
            prep.world_prompt_timeline = core.turn_world.mirror_timeline(
                getattr(session, "world_timeline", {}) or {}, wt.ctx.definition,
                plan.projected_world, wt.ctx.lead_pc)
        return wt

    def _world_diagnostic(self, session, prep, code, detail) -> None:
        """DESIGN 11 §32 — 마스터 진단(내구 로그). 숨은 정보는 플레이어 채널에 보내지 않는다."""
        rec = {"code": code, "transaction_id": prep.transaction_id,
               "world_version": core.action_intent.world_version_id(session),
               "definition_revision": getattr(session, "definition_revision", None), "detail": detail}
        core.logs.warn("world", f"세계 진단 {code}", session=session, data=rec)
        diags = getattr(prep, "world_diagnostics", None)   # v7.30: 그 턴 전수보고(층위활동)에 함께 싣는다(보조 — 실패 무관)
        if isinstance(diags, list):
            diags.append(rec)

    async def _world_conflict_outcome(self, session, prep, *, stage: str) -> str:
        prep.seal()
        try:
            prep.close_cost_membership()
        except core.turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"cost_close:{e}")
        prep.failure_code = core.turn_transaction.FailureCode.WORLD_PROJECTION_CONFLICT.value
        prep.failure_stage = f"WORLD_{stage.upper()}"
        wt = getattr(prep, "world_turn", None)
        self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                               {"stage": stage, "diagnostics": getattr(getattr(wt, "plan", None), "diagnostics", None)})
        return _OUTCOME_NOT_READY

    def _process_world_observation(self, session, prep, plan, master_ch) -> None:
        """추출 관찰 처리(순수) + 레거시 장소/시간 값을 정본 미러로 대체 + 후속 묘사 등록."""
        wt = prep.world_turn
        ctx = wt.ctx
        _man = wt.obs_manifest or core.turn_world.ObservationManifest()
        _raw = prep.world_observation_raw
        # A1 보정: 추출이 D키를 빠뜨린 명확·유일한 현장 발견물 수거만 키 기반 수거로 정규화(이름만으로는 연결 안 함)
        _implied = self._implied_discovery_take(session, prep.transaction_id, ctx, wt, _man, _raw, plan)
        if _implied:
            _raw = {**(_raw if isinstance(_raw, dict) else {}),
                    "discovery_taken": list((_raw or {}).get("discovery_taken") or []) + [_implied]}
            core.write_log(session.session_id, "api", f"[v7.14 발견 수거 키 복구] {_implied}")
            # 복구된 수거는 그 PC +1(item_changes) 한 건이 설명한다 — 같은 물리 수거를 두 번 세지 않는다
            _row = _man.discovery_of(_implied)
            _pc = self._pc_name(session, ctx)
            _hit = next((e for e in plan.item_deltas if _row and e.get("target") == _pc and int(e.get("delta") or 0) == 1
                         and core.turn_world.same_target(e.get("item"), _row[2])), None)
            if _hit is not None:
                plan.item_deltas = [e for e in plan.item_deltas if e is not _hit]
        # v7.17 아이템: 같은 기준 재고에 모든 연산을 순서대로 가상 적용 — 하나라도 어긋나면 전체를 재추출(부분 적용 없음).
        #   발견물 수거(D키)·코드 지급(접근 확인)·문서 읽기를 하나의 계획으로 묶고, 수거한 D키만 소진(SUPERSEDED)한다.
        item_plan = self._build_item_plan(session, prep, ctx, wt, _raw, plan)
        item_plan.facts["evaluated_same_turn"] = True     # P1(D4): 이 턴 보충 진행이 사용·지급 사실을 평가한다
        _raw = {**(_raw if isinstance(_raw, dict) else {}), "discovery_taken": list(item_plan.discovery_takes)}
        res = core.turn_world.process_observation(
            ctx, session, wt.plan, _man,
            _raw, transaction_id=prep.transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            replan=wt.replan,
            face_info_ids=self._revealed_identity_ids(session, prep, wt.identified_faces, "face"),
            name_info_ids=self._revealed_identity_ids(session, prep, wt.identified_names, "name"),
            discovery=wt.discovery, read_info_ids=item_plan.read_info_ids,
            pending=core.turn_world.pending_signals(session), item_facts=item_plan.facts)
        wt.result = res
        core.write_log(session.session_id, "api",
                       f"[H05 관찰] status={res.status} exposures={list(res.exposures)} "
                       f"extra={res.extra_minutes} pending={len(res.pending)} "
                       f"diag={json.dumps(res.diagnostics, ensure_ascii=False, default=str)[:600]}")
        if res.status != core.turn_world.TW_READY:
            return
        # v7.25(F03): 동행 합류는 제안 — 정본 행위자이고 턴 끝에 PC와 같은 장소일 때만 관계로 확정. 이탈은 배치 해제.
        _kept, _clears, _cdiags = core.turn_world.companion_outcome(
            ctx, res.projected_world, res.ops, joined=plan.companions_joined, left=plan.companions_left,
            prefix=core.turn_world.turn_op_prefix("cmpo", prep.transaction_id))
        # v7.51(기능 party): 파티 합류 조건·인원 한도 + 사건 합류·이탈(같은 동행 명단)
        _kept, _pj, _pl, _pdiags = core.turn_world.party_outcome(ctx, session, res.projected_world, _kept)
        if _pdiags:
            _cdiags = list(_cdiags) + list(_pdiags)
        if _pj or _pl:
            plan.companions_joined = list(dict.fromkeys(list(_kept) + _pj))
            plan.companions_left = list(dict.fromkeys(list(plan.companions_left or ()) + _pl))
            _kept = plan.companions_joined
        if _cdiags:
            _rej = {d.get("name") for d in _cdiags if d.get("code") in ("COMPANION_JOIN_REJECTED", "PARTY_JOIN_REJECTED")}
            plan.companions_joined = list(_kept)
            plan.entries = [e for e in plan.entries
                            if not (e.get("domain") == "companion" and e.get("op") == "join" and e.get("target") in _rej)]
            plan.diagnostics["companion"] = list(_cdiags)
            self._world_diagnostic(session, prep, "COMPANION_JOIN_REJECTED", {"diagnostics": _cdiags})
        if _clears:
            res.ops = tuple(res.ops) + tuple(_clears)
        # 장소·시간 권위 = 정본 세계. 레거시 적용부는 추출값이 아니라 정본 미러를 받는다.
        prev_tl = getattr(session, "world_timeline", {}) or {}
        new_tl = core.turn_world.mirror_timeline(prev_tl, ctx.definition, res.projected_world, ctx.lead_pc)
        plan.world_new_tl = new_tl
        plan.world_player_faction = core.turn_world.derived_player_faction(res.projected_world)   # H07-C 호환 뷰
        plan.location_before = prev_tl.get("current_location")
        plan.location_after = new_tl.get("current_location")
        plan.location_moved = plan.location_before != plan.location_after
        # v7.17: 동적 세계의 소지품 권위 = 검증된 아이템 계획 하나(발견물 수거 +1도 그 안의 ACQUIRE). item_deltas는 쓰지 않는다.
        plan.item_plan = item_plan
        plan.item_deltas = []
        plan.entries = [e for e in plan.entries if e.get("domain") != "item"] + [
            {"domain": "item", "target": r["target"], "op": "delta", "payload": {"item": r["item"], "delta": r["delta"]}}
            for r in item_plan.report]
        if item_plan.diagnostics:
            plan.diagnostics["item_plan"] = list(item_plan.diagnostics)
        if res.pending:
            # P1(#1): 후속 묘사 없음 — 본문이 전하지 못한 관찰 신호는 커밋과 함께 전달 대기로 남는다(유실 금지)
            self._world_diagnostic(session, prep, "SIGNALS_PENDING_DELIVERY",
                                   {"signals": [[r["signal_id"], r["instance_id"]] for r in res.pending]})

    @staticmethod
    def _revealed_identity_ids(session, prep, actor_ids, kind) -> tuple:
        """P3 G3: 식별 후보 중 본문에서 실제 공개가 확인된 InfoId만 정본 인지로 — 이미 아는 것은 그대로(멱등)."""
        if not core.speaker_card.card_world(session):
            return ()
        ids = core.speaker_card.identity_info_ids(core.speaker_card.config_of(session.scenario_data), actor_ids, kind)
        shown = set(getattr(prep, "identity_reveals", None) or {})
        return tuple(i for i in ids if i in shown)

    def _implied_discovery_take(self, session, transaction_id, ctx, wt, manifest, raw, plan):
        """A1 보정 근거 조립 — 게이트가 검증한 선언 대상(기존 발견 D키 또는 이번 턴 새 발견)만 후보."""
        gate = (self._tx_judgment(session, transaction_id) or {}).get("gate")
        if wt.discovery and wt.discovery.get("status") == "NEW":
            ref_key = core.turn_world.DISCOVERY_NEW_KEY
        else:
            iid = (gate or {}).get("referenced_info_id")
            ref_key = next((r[0] for r in manifest.discoveries if iid and r[1] == iid), None)
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        return core.turn_world.implied_discovery_take(
            manifest, raw, ref_key, plan.item_deltas, pc_name,
            ((getattr(session, "resources", None) or {}).get(pc_name) or {}) if pc_name else {})

    @staticmethod
    def _pc_name(session, ctx):
        uid = ctx.uid_of.get(ctx.lead_pc)
        return ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")

    def _item_manifest(self, session, wt):
        """v7.17: 이번 추출의 아이템 후보(PC + 장면 정의 NPC의 정본 소지·현장 물건·발견물·저작 제작법·코드 지급)."""
        ctx = wt.ctx
        pc_name = self._pc_name(session, ctx)
        scene, projs = core.turn_world.scene_npc_projections(ctx, wt.plan, session)
        owners = [p.get("legacy_name") for p in projs if p.get("legacy_name")]
        pays = [f"{p.get('capability')} ×{p.get('quantity')}(접근 확인 지급)"
                for p in (getattr(wt.plan, "item_payments", None) or ())]
        return core.items.build_manifest(
            session, pc_name=pc_name, scene_owner_names=owners, scene_place=scene,
            defs=core.items.definitions_of(ctx.definition),
            discovery_rows=(wt.obs_manifest.discoveries if wt.obs_manifest is not None else ()),
            recipes=ctx.definition.get("item_recipes"), payments=pays)

    def _build_item_plan(self, session, prep, ctx, wt, raw, plan):
        """v7.17: 추출 item_ops(+ 발견 D키 수거·코드 지급) → 검증된 ItemPlan. 실패하면 ItemContractError(재추출)."""
        pc_name = self._pc_name(session, ctx)
        defs = core.items.definitions_of(ctx.definition)
        man = getattr(wt, "item_manifest", None) or self._item_manifest(session, wt)
        pays = []
        for p in (getattr(wt.plan, "item_payments", None) or ()):
            op = core.items.payment_op(session, pc_name=pc_name, capability=p.get("capability"),
                                       quantity=int(p.get("quantity") or 1), defs=defs)
            if op is None:
                raise core.items.ItemContractError(f"접근 확인 지급 불가: {p.get('capability')} ×{p.get('quantity')}")
            pays.append(op)
        obs = raw if isinstance(raw, dict) else {}
        return core.items.build_plan(
            session, man, obs.get(core.items.OPS_FIELD), defs=defs, recipes=ctx.definition.get("item_recipes"),
            pc_name=pc_name, transaction_id=prep.transaction_id,
            discovery_taken=[k for k in (obs.get("discovery_taken") or ()) if isinstance(k, str)],
            legacy_item_deltas=plan.item_deltas, payments=pays,
            narration_text=getattr(getattr(prep, "narration", None), "text", "") or "",
            raw_traits=[r for r in (obs.get(core.items.TRAITS_FIELD) or ()) if isinstance(r, dict)])

    @staticmethod
    def _apply_discovery_gains(session, ctx, plan, gains) -> None:
        """v7.14: 키로 지목된 발견물 수거 → 같은 커밋의 소지품 +1(추출이 같은 이름으로 이미 +를 냈으면 중복 없음)."""
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        if not pc_name:
            return
        for item in gains or ():
            if any(e.get("target") == pc_name and core.turn_world.same_target(e.get("item"), item)
                   and int(e.get("delta") or 0) > 0 for e in plan.item_deltas):
                continue
            plan.item_deltas.append({"target": pc_name, "item": item, "delta": 1})
            plan.entries.append({"domain": "item", "target": pc_name, "op": "delta",
                                 "payload": {"item": item, "delta": 1}})

    def _stage_world_turn(self, session, prep):
        """READY 직전 — 관찰 결과 검증 후 최종 연산을 PLAYER_TURN 묶음에 담는다. 실패 코드 또는 None."""
        wt = getattr(prep, "world_turn", None)
        if wt is None:
            return None
        FC = core.turn_transaction.FailureCode
        res = wt.result
        if res is None:
            return FC.COMMIT_VALIDATION_FAILURE.value
        if res.status == core.turn_world.TW_CONFLICT:
            self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                                   {"stage": "post_narration", "conflict": res.conflict,
                                    "diagnostics": res.diagnostics})
            return FC.WORLD_PROJECTION_CONFLICT.value
        if res.status != core.turn_world.TW_READY:
            self._world_diagnostic(session, prep, res.status, {"diagnostics": res.diagnostics})
            return FC.COMMIT_VALIDATION_FAILURE.value
        base = core.world_state.version_id_of(prep.baseline_world_version)
        if res.plan is not None and res.plan.source_world_version_id != base:
            return FC.COMMIT_VALIDATION_FAILURE.value
        base_rev = getattr(prep, "baseline_definition_revision", None)
        if wt.ctx.definition_revision != base_rev or (
                res.plan is not None and res.plan.definition_revision != base_rev):
            # R2: 옛 정의로 만든 계획은 새 revision 준비에 들어가지 않는다
            self._world_diagnostic(session, prep, "DEFINITION_STALE",
                                   {"plan_revision": getattr(res.plan, "definition_revision", None),
                                    "context_revision": wt.ctx.definition_revision, "baseline_revision": base_rev})
            return FC.COMMIT_VALIDATION_FAILURE.value
        # v7.25(F06): 승격된(지속 등장) 비저작 인물을 정본 ActorRegistry에 같은 커밋으로 등록 — 정본 인물과 겹치면 등록 없음
        #   이번 턴에 승격된 인물만(턴마다 옛 세션 NPC를 소급 등록하지 않는다 — 변이 없는 턴은 변이 없이).
        _promoted = list(getattr(prep, "promotions", {}) or {})
        _reg_ops = core.turn_world.session_npc_register_ops(
            wt.ctx, res.projected_world, _promoted,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            prefix=core.turn_world.turn_op_prefix("snpc", prep.transaction_id),
            barrier=[o.op_id for o in res.ops], present=_promoted)
        _pu_ops = core.turn_world.pursuit_hearing_ops(session, prep, wt.ctx, res, self._pc_name(session, wt.ctx)) if res.projected_world is not None else ()
        if res.projected_world is not None:            # v7.50(E9): 이번 커밋이 닿는 장 결말 — 커밋 뒤 카드·기록
            prep.chapter_endings = core.turn_world.new_chapter_endings(wt.ctx.definition, session, res.projected_world)
        try:
            core.turn_preparation.stage_world_ops(prep, tuple(res.ops) + tuple(_reg_ops) + tuple(_pu_ops))
        except core.turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"world_stage:{e}")
            return FC.COMMIT_VALIDATION_FAILURE.value
        return None

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
