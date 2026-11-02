"""자동 턴의 세계 단계 — 판단 결과 해석·세계 계획·관찰 처리·물건 계획·세계 연산 스테이징(GMCog 믹스인).

디스코드·provider를 쓰지 않는다(리팩터링 T-5: adapters/discord/turn_steps.py에서 옮김 — 본문 동일,
core 별칭 대신 실제 모듈을 직접 참조)."""
from __future__ import annotations

import re
import json
import dataclasses

from indaim.app import io as _io
from indaim.app import logs as _logs
from indaim.app import utils as _utils
from indaim.rules.items import items as _items
from indaim.rules.pursuit import pursuit as _pursuit
from indaim.turn import action_intent as _action_intent
from indaim.turn import player_world as _player_world
from indaim.turn import turn_history as _turn_history
from indaim.turn import turn_preparation as _turn_preparation
from indaim.turn import turn_transaction as _turn_transaction
from indaim.turn import turn_world as _turn_world
from indaim.turn import world_state as _world_state
from indaim.turn.outcomes import _OUTCOME_NOT_READY


class WorldSteps:
    def _world_turn(self, session, transaction_id, *, create=False):
        tx = _turn_transaction.get_active_transaction(session)
        if tx is None or transaction_id is None or tx.transaction_id != transaction_id:
            return None
        wt = getattr(tx, "world_turn", None)
        if wt is not None and _action_intent.is_definition_stale(wt.ctx.definition_revision, session):
            # R2: 정의 revision이 바뀌었다 — 옛 매니페스트·계획·경로 선택을 같은 시도 재진입에서도 재사용하지 않는다
            self.rt.write_log(session.session_id, "api",
                           f"[H05 정의 변경] 옛 WorldTurn 폐기 rev={wt.ctx.definition_revision} "
                           f"→ {getattr(session, 'definition_revision', None)}")
            tx.world_turn = wt = None
        if wt is None and create:
            ctx = _player_world.session_world_context(session)
            if not ctx.dynamic:
                return None
            wt = _turn_world.WorldTurn(ctx=ctx)
            tx.world_turn = wt
        return wt

    def _tx_judgment(self, session, transaction_id) -> dict | None:
        tx = _turn_transaction.get_active_transaction(session)
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
        place = _turn_world.pc_place(ctx, session)
        out["dynamic"] = True
        if not place:
            return out
        out["place"] = place
        try:
            out["place_name"] = _turn_world.pc_place_name(ctx, session, place)
            out["rows"] = _turn_world.discoveries_at(ctx.definition, session, ctx.lead_pc, place)
            out["failed"] = _turn_history.recent_failed_discoveries(session.session_id, place)
            out["finds"] = _turn_world.find_object_names(ctx.definition, session, ctx.lead_pc, place)
        except Exception as e:
            print(f"[선언 게이트] 입력 조립 실패(발견 목록 생략): {e}")
        return out

    def _build_world_manifest(self, session, wt) -> None:
        ctx = wt.ctx
        uid = ctx.uid_of.get(ctx.lead_pc)
        routes = wt.plan.route_options if (wt.plan is not None and wt.plan.status
                                           == _player_world.PLAN_NEEDS_CHOICE) else ()
        access = _player_world.access_confirm_options(
            ctx.definition, session, ctx.lead_pc,
            _player_world.pc_access_capabilities(session, uid, ctx.definition))   # v7.17.2: 소속 포함(접근 전용)
        pend = self._pending_move(session, wt)
        wt.manifest = _action_intent.build_manifest(
            ctx.definition, session, ctx.lead_pc, definition_revision=ctx.definition_revision,
            route_options=routes, access_options=access, asked_exits=pend["exits"] if pend else ())

    @staticmethod
    def _pending_move(session, wt):
        """v7.22 목적지 질문 문맥 — 같은 시도(WorldTurn)·같은 장소·같은 world_version일 때만 유효(아니면 버린다).
        재시작·다른 세션·다른 시도에는 남지 않는다(트랜잭션 런타임 기록)."""
        pend = wt.pending_move
        if pend is None:
            return None
        now = _turn_world.move_question_anchor(wt.ctx, session)
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
        unspecified = (mv is not None and mv.kind == _action_intent.MOVE_WANDER and not mv.edge_id) or (
            pend is not None and move_open and all(u == ("destination", "MISSING") for u in move_open))
        if unspecified:
            pool = tuple(mv.pool) if mv is not None and mv.kind == _action_intent.MOVE_WANDER else ()
            if pend is not None:
                asked = set(pend["exits"])
                pool = tuple(e for e in pool if e in asked) or tuple(sorted(asked))   # 질문 범위를 넓히지 않는다
            pick = _player_world.wander_exit(
                wt.ctx.definition, session, wt.ctx.lead_pc, pool=pool, transaction_id=transaction_id,
                narrative_seed=getattr(session, "narrative_seed", None) or session.session_id)
            wt.intent = intent = dataclasses.replace(
                intent, movement=_action_intent.MovementIntent(
                    _action_intent.MOVE_WANDER, pick[1] if pick else None, pick[0] if pick else None),
                exit_choices=(), unresolved=tuple(u for u in intent.unresolved if u not in move_open))
            self.rt.write_log(session.session_id, "api", f"[v7.22 출구 선택] pool={len(pool)} pick={pick}")
        if not any(f in ("destination", "exit") for f, _c in intent.unresolved):
            wt.pending_move = None

    def _plan_world(self, session, wt, transaction_id, *, resolved_tie_orders=None):
        ctx = wt.ctx
        intent = wt.intent or _action_intent.ResolvedActionIntent()
        plan = self._plan_world_core(session, wt, transaction_id, intent, resolved_tie_orders)
        # v7.25(F03): 턴 시작에 PC와 함께 있던 동행은 같은 계획(묘사 전)에서 PC와 함께 움직인다
        return _turn_world.companion_follow(
            ctx, session, plan, list(getattr(session, "companions", None) or ()),
            prefix=_turn_world.turn_op_prefix("cmpf", transaction_id))

    def _plan_world_core(self, session, wt, transaction_id, intent, resolved_tie_orders):
        ctx = wt.ctx
        return _player_world.plan_player_turn(
            ctx, session, intent, transaction_id=transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            capabilities=_player_world.pc_capabilities(session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),
            access_capabilities=_player_world.pc_access_capabilities(
                session, ctx.uid_of.get(ctx.lead_pc), ctx.definition),                     # v7.17.2: 접근 판정 전용
            item_facts=_items.current_facts(session),           # v7.17: 직전 커밋 턴의 사실만
            stats=_player_world.pc_stats(session, ctx.uid_of.get(ctx.lead_pc)),       # H07: 스탯 조건
            statuses=_player_world.pc_statuses(session, ctx.uid_of.get(ctx.lead_pc)),  # v7.48(E1): 상태 조건
            pursuit=_pursuit.context(ctx.definition, self.rt.get_merged_status_effects(session.scenario_data or {}),
                                         session.statuses, self._pc_name(session, ctx)),   # v7.48(E3): 추적자
            combat_deaths=_turn_world.combat_deaths(                                    # v7.50(E6): 전투 쓰러짐
                ctx.definition, session, (self._tx_judgment(session, transaction_id) or {}).get("roll_records") or ()),
            resolved_tie_orders=resolved_tie_orders)

    def _ensure_world_plan(self, session, wt, transaction_id):
        cur = _action_intent.world_version_id(session)
        if wt.plan is None or wt.plan.source_world_version_id != cur or \
                _action_intent.is_definition_stale(wt.plan.definition_revision, session):
            wt.plan = self._plan_world(session, wt, transaction_id)
            wt.replan = lambda orders, _s=session, _w=wt, _t=transaction_id: self._plan_world(
                _s, _w, _t, resolved_tie_orders=orders)
            self.rt.write_log(session.session_id, "api",
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
        anchor = _turn_world.move_question_anchor(wt.ctx, session)
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
        _turn_transaction.mark_transaction_status(
            session, transaction_id, _turn_transaction.TurnStatus.JUDGING)
        _logs.event("turn", "경로 질문 번호 응답", session=session,
                        data={"stage": "route_answer", "tx": transaction_id, "choice": n, "options": len(sigs)})
        return {k: v for k, v in prior.items() if k != _action_intent.INTENT_FIELD}

    def _attach_world_turn(self, session, prep, transaction_id):
        """PROCEED 배리어 소유 시작 — 계획을 준비 객체에 붙이고 묘사용 투영 장면을 만든다."""
        wt = self._world_turn(session, transaction_id)
        if wt is None:
            return None
        plan = self._ensure_world_plan(session, wt, transaction_id)
        if plan.status == _player_world.PLAN_NEEDS_CHOICE:
            # 선택을 물을 수 없는 경로(ROLL 후·강제 PROCEED) — 이동 없이 묘사(선택은 다음 선언에서).
            plan.status, plan.reason = _player_world.PLAN_REJECTED, "ROUTE_CHOICE_PENDING"
        if plan.status == _player_world.PLAN_TIE:
            # P1(D2): 묘사 전에 같은 분 선후를 정한다 — 지시층위 선택(허용 키) 우선, 무효·미선택은 코드 결정
            plan, rec = _turn_world.resolve_plan_tie(plan, wt.replan, wt.tie_choice, wt.ctx.definition, wt.ctx)
            wt.plan = plan
            self.rt.write_log(session.session_id, "api",
                           f"[P1 선후] {json.dumps(rec, ensure_ascii=False)} → status={plan.status}")
        prep.world_turn = wt
        if plan.projected_world is not None:
            prep.world_prompt_timeline = _turn_world.mirror_timeline(
                getattr(session, "world_timeline", {}) or {}, wt.ctx.definition,
                plan.projected_world, wt.ctx.lead_pc)
        return wt

    def _world_diagnostic(self, session, prep, code, detail) -> None:
        """DESIGN 11 §32 — 마스터 진단(내구 로그). 숨은 정보는 플레이어 채널에 보내지 않는다."""
        rec = {"code": code, "transaction_id": prep.transaction_id,
               "world_version": _action_intent.world_version_id(session),
               "definition_revision": getattr(session, "definition_revision", None), "detail": detail}
        _logs.warn("world", f"세계 진단 {code}", session=session, data=rec)
        diags = getattr(prep, "world_diagnostics", None)   # v7.30: 그 턴 전수보고(층위활동)에 함께 싣는다(보조 — 실패 무관)
        if isinstance(diags, list):
            diags.append(rec)

    async def _world_conflict_outcome(self, session, prep, *, stage: str) -> str:
        prep.seal()
        try:
            prep.close_cost_membership()
        except _turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"cost_close:{e}")
        prep.failure_code = _turn_transaction.FailureCode.WORLD_PROJECTION_CONFLICT.value
        prep.failure_stage = f"WORLD_{stage.upper()}"
        wt = getattr(prep, "world_turn", None)
        self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                               {"stage": stage, "diagnostics": getattr(getattr(wt, "plan", None), "diagnostics", None)})
        return _OUTCOME_NOT_READY

    def _process_world_observation(self, session, prep, plan, master_ch) -> None:
        """추출 관찰 처리(순수) + 레거시 장소/시간 값을 정본 미러로 대체 + 후속 묘사 등록."""
        wt = prep.world_turn
        ctx = wt.ctx
        _man = wt.obs_manifest or _turn_world.ObservationManifest()
        _raw = prep.world_observation_raw
        # A1 보정: 추출이 D키를 빠뜨린 명확·유일한 현장 발견물 수거만 키 기반 수거로 정규화(이름만으로는 연결 안 함)
        _implied = self._implied_discovery_take(session, prep.transaction_id, ctx, wt, _man, _raw, plan)
        if _implied:
            _raw = {**(_raw if isinstance(_raw, dict) else {}),
                    "discovery_taken": list((_raw or {}).get("discovery_taken") or []) + [_implied]}
            self.rt.write_log(session.session_id, "api", f"[v7.14 발견 수거 키 복구] {_implied}")
            # 복구된 수거는 그 PC +1(item_changes) 한 건이 설명한다 — 같은 물리 수거를 두 번 세지 않는다
            _row = _man.discovery_of(_implied)
            _pc = self._pc_name(session, ctx)
            _hit = next((e for e in plan.item_deltas if _row and e.get("target") == _pc and int(e.get("delta") or 0) == 1
                         and _turn_world.same_target(e.get("item"), _row[2])), None)
            if _hit is not None:
                plan.item_deltas = [e for e in plan.item_deltas if e is not _hit]
        # v7.17 아이템: 같은 기준 재고에 모든 연산을 순서대로 가상 적용 — 하나라도 어긋나면 전체를 재추출(부분 적용 없음).
        #   발견물 수거(D키)·코드 지급(접근 확인)·문서 읽기를 하나의 계획으로 묶고, 수거한 D키만 소진(SUPERSEDED)한다.
        item_plan = self._build_item_plan(session, prep, ctx, wt, _raw, plan)
        item_plan.facts["evaluated_same_turn"] = True     # P1(D4): 이 턴 보충 진행이 사용·지급 사실을 평가한다
        _raw = {**(_raw if isinstance(_raw, dict) else {}), "discovery_taken": list(item_plan.discovery_takes)}
        res = _turn_world.process_observation(
            ctx, session, wt.plan, _man,
            _raw, transaction_id=prep.transaction_id,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            replan=wt.replan,
            face_info_ids=self._revealed_identity_ids(session, prep, wt.identified_faces, "face"),
            name_info_ids=self._revealed_identity_ids(session, prep, wt.identified_names, "name"),
            discovery=wt.discovery, read_info_ids=item_plan.read_info_ids,
            pending=_turn_world.pending_signals(session), item_facts=item_plan.facts)
        wt.result = res
        self.rt.write_log(session.session_id, "api",
                       f"[H05 관찰] status={res.status} exposures={list(res.exposures)} "
                       f"extra={res.extra_minutes} pending={len(res.pending)} "
                       f"diag={json.dumps(res.diagnostics, ensure_ascii=False, default=str)[:600]}")
        if res.status != _turn_world.TW_READY:
            return
        # v7.25(F03): 동행 합류는 제안 — 정본 행위자이고 턴 끝에 PC와 같은 장소일 때만 관계로 확정. 이탈은 배치 해제.
        _kept, _clears, _cdiags = _turn_world.companion_outcome(
            ctx, res.projected_world, res.ops, joined=plan.companions_joined, left=plan.companions_left,
            prefix=_turn_world.turn_op_prefix("cmpo", prep.transaction_id))
        # v7.51(기능 party): 파티 합류 조건·인원 한도 + 사건 합류·이탈(같은 동행 명단)
        _kept, _pj, _pl, _pdiags = _turn_world.party_outcome(ctx, session, res.projected_world, _kept)
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
        new_tl = _turn_world.mirror_timeline(prev_tl, ctx.definition, res.projected_world, ctx.lead_pc)
        plan.world_new_tl = new_tl
        plan.world_player_faction = _turn_world.derived_player_faction(res.projected_world)   # H07-C 호환 뷰
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

    def _implied_discovery_take(self, session, transaction_id, ctx, wt, manifest, raw, plan):
        """A1 보정 근거 조립 — 게이트가 검증한 선언 대상(기존 발견 D키 또는 이번 턴 새 발견)만 후보."""
        gate = (self._tx_judgment(session, transaction_id) or {}).get("gate")
        if wt.discovery and wt.discovery.get("status") == "NEW":
            ref_key = _turn_world.DISCOVERY_NEW_KEY
        else:
            iid = (gate or {}).get("referenced_info_id")
            ref_key = next((r[0] for r in manifest.discoveries if iid and r[1] == iid), None)
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        return _turn_world.implied_discovery_take(
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
        scene, projs = _turn_world.scene_npc_projections(ctx, wt.plan, session)
        owners = [p.get("legacy_name") for p in projs if p.get("legacy_name")]
        pays = [f"{p.get('capability')} ×{p.get('quantity')}(접근 확인 지급)"
                for p in (getattr(wt.plan, "item_payments", None) or ())]
        return _items.build_manifest(
            session, pc_name=pc_name, scene_owner_names=owners, scene_place=scene,
            defs=_items.definitions_of(ctx.definition),
            discovery_rows=(wt.obs_manifest.discoveries if wt.obs_manifest is not None else ()),
            recipes=ctx.definition.get("item_recipes"), payments=pays)

    def _build_item_plan(self, session, prep, ctx, wt, raw, plan):
        """v7.17: 추출 item_ops(+ 발견 D키 수거·코드 지급) → 검증된 ItemPlan. 실패하면 ItemContractError(재추출)."""
        pc_name = self._pc_name(session, ctx)
        defs = _items.definitions_of(ctx.definition)
        man = getattr(wt, "item_manifest", None) or self._item_manifest(session, wt)
        pays = []
        for p in (getattr(wt.plan, "item_payments", None) or ()):
            op = _items.payment_op(session, pc_name=pc_name, capability=p.get("capability"),
                                       quantity=int(p.get("quantity") or 1), defs=defs)
            if op is None:
                raise _items.ItemContractError(f"접근 확인 지급 불가: {p.get('capability')} ×{p.get('quantity')}")
            pays.append(op)
        obs = raw if isinstance(raw, dict) else {}
        return _items.build_plan(
            session, man, obs.get(_items.OPS_FIELD), defs=defs, recipes=ctx.definition.get("item_recipes"),
            pc_name=pc_name, transaction_id=prep.transaction_id,
            discovery_taken=[k for k in (obs.get("discovery_taken") or ()) if isinstance(k, str)],
            legacy_item_deltas=plan.item_deltas, payments=pays,
            narration_text=getattr(getattr(prep, "narration", None), "text", "") or "",
            raw_traits=[r for r in (obs.get(_items.TRAITS_FIELD) or ()) if isinstance(r, dict)])

    @staticmethod
    def _apply_discovery_gains(session, ctx, plan, gains) -> None:
        """v7.14: 키로 지목된 발견물 수거 → 같은 커밋의 소지품 +1(추출이 같은 이름으로 이미 +를 냈으면 중복 없음)."""
        uid = ctx.uid_of.get(ctx.lead_pc)
        pc_name = ((getattr(session, "players", None) or {}).get(uid) or {}).get("name")
        if not pc_name:
            return
        for item in gains or ():
            if any(e.get("target") == pc_name and _turn_world.same_target(e.get("item"), item)
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
        FC = _turn_transaction.FailureCode
        res = wt.result
        if res is None:
            return FC.COMMIT_VALIDATION_FAILURE.value
        if res.status == _turn_world.TW_CONFLICT:
            self._world_diagnostic(session, prep, "WORLD_PROJECTION_CONFLICT",
                                   {"stage": "post_narration", "conflict": res.conflict,
                                    "diagnostics": res.diagnostics})
            return FC.WORLD_PROJECTION_CONFLICT.value
        if res.status != _turn_world.TW_READY:
            self._world_diagnostic(session, prep, res.status, {"diagnostics": res.diagnostics})
            return FC.COMMIT_VALIDATION_FAILURE.value
        base = _world_state.version_id_of(prep.baseline_world_version)
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
        _reg_ops = _turn_world.session_npc_register_ops(
            wt.ctx, res.projected_world, _promoted,
            narrative_seed=getattr(session, "narrative_seed", None) or session.session_id,
            prefix=_turn_world.turn_op_prefix("snpc", prep.transaction_id),
            barrier=[o.op_id for o in res.ops], present=_promoted)
        _pu_ops = _turn_world.pursuit_hearing_ops(session, prep, wt.ctx, res, self._pc_name(session, wt.ctx)) if res.projected_world is not None else ()
        if res.projected_world is not None:            # v7.50(E9): 이번 커밋이 닿는 장 결말 — 커밋 뒤 카드·기록
            prep.chapter_endings = _turn_world.new_chapter_endings(wt.ctx.definition, session, res.projected_world)
        try:
            _turn_preparation.stage_world_ops(prep, tuple(res.ops) + tuple(_reg_ops) + tuple(_pu_ops))
        except _turn_preparation.BarrierViolationError as e:
            prep.violations.append(f"world_stage:{e}")
            return FC.COMMIT_VALIDATION_FAILURE.value
        return None
