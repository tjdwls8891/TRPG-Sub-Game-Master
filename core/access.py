# 접근 — 실제 접근 판정 · 인지된 접근 · 규칙 평가기 레지스트리 (H03-B)
#
# [권위 — DESIGN 02 §23~30 · H03 결정 게이트 A12]
#   실제 질의 결과는 셋이다:  PASSABLE / ACTION_REQUIRED / IMPASSABLE
#   ACTION_REQUIRED의 행동 정책: IMPLICIT / CONFIRM / SEPARATE_ACTION
#   접근 제어의 세계 상태(access_control_state)와 행위자별 통과 결과는 다르다 —
#   같은 '잠김' 상태도 열쇠를 가진 행위자에게는 ACTION_REQUIRED(CONFIRM)일 수 있다.
#
#   canonical access_control_state = {control_id: {"state": str}}
#   항목이 없으면 정의의 initial_state가 현재 상태다(초기화 전에도 결정적).
#
# [인지 ≠ 실제]
#   perceived_access는 행위자의 KnownAccessFact만 읽는다. 사실 기록이 없으면 UNKNOWN —
#   실제 상태가 막혀 있어도 그것을 근거로 경로 후보를 지우지 않는다(숨은 봉쇄 비노출).
#
# [하지 않는 것]
#   · 장르별 문 상태 이름을 core에 두지 않는다. 상태·결과는 저작값(STATE_TABLE)이다.
#   · 플레이어 확인(CONFIRM) UX는 H05. 여기서는 판정만 한다.
from __future__ import annotations

from dataclasses import dataclass

PASSABLE = "PASSABLE"
ACTION_REQUIRED = "ACTION_REQUIRED"
IMPASSABLE = "IMPASSABLE"
ACCESS_RESULTS = (PASSABLE, ACTION_REQUIRED, IMPASSABLE)

IMPLICIT = "IMPLICIT"
CONFIRM = "CONFIRM"
SEPARATE_ACTION = "SEPARATE_ACTION"
ACTION_POLICIES = (IMPLICIT, CONFIRM, SEPARATE_ACTION)

PERCEIVED_PASSABLE = "PERCEIVED_PASSABLE"
PERCEIVED_ACTION_REQUIRED = "PERCEIVED_ACTION_REQUIRED"
PERCEIVED_BLOCKED = "PERCEIVED_BLOCKED"
UNKNOWN = "UNKNOWN"
PERCEIVED = (PERCEIVED_PASSABLE, PERCEIVED_ACTION_REQUIRED, PERCEIVED_BLOCKED, UNKNOWN)
_PERCEIVED_OF = {PASSABLE: PERCEIVED_PASSABLE, ACTION_REQUIRED: PERCEIVED_ACTION_REQUIRED,
                 IMPASSABLE: PERCEIVED_BLOCKED}


class AccessError(ValueError):
    """접근 정의·상태가 일관되지 않을 때(컴파일러를 거친 정의에서는 버그)."""


@dataclass(frozen=True)
class AccessDecision:
    result: str
    action_policy: str | None = None
    action_delay_minutes: int = 0
    control_id: str | None = None
    state: str | None = None
    rule_id: str | None = None
    matched_capability: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"result": self.result, "action_policy": self.action_policy,
                "action_delay_minutes": self.action_delay_minutes, "control_id": self.control_id,
                "state": self.state, "rule_id": self.rule_id,
                "matched_capability": self.matched_capability, "reason": self.reason}


# ══════════════════════════════════════════════════════════════
#  규칙 평가기 레지스트리 (rule_type → evaluator)
# ══════════════════════════════════════════════════════════════

def evaluate_state_table(rule: dict, state: str, actor_capabilities) -> dict:
    """STATE_TABLE: 상태별 결과 + 행위자 capability override(저작 순서 첫 일치)."""
    entry = rule["states"].get(state)
    if entry is None:
        raise AccessError(f"규칙 {rule.get('rule_id')!r}에 상태 {state!r}가 없습니다")
    caps = actor_capabilities or ()
    for ov in entry.get("actor_overrides") or ():
        if ov["requires_capability"] in caps:
            return dict(ov)
    return dict(entry, requires_capability=None)


class AccessRuleRegistry:
    def __init__(self):
        self._evaluators = {}

    def register(self, rule_type: str, evaluator) -> None:
        if rule_type in self._evaluators:
            raise AccessError(f"rule_type {rule_type!r} 중복 등록")
        self._evaluators[rule_type] = evaluator

    def evaluator_for(self, rule_type: str):
        ev = self._evaluators.get(rule_type)
        if ev is None:
            raise AccessError(f"평가기가 없는 rule_type {rule_type!r}")
        return ev

    def rule_types(self) -> tuple:
        return tuple(sorted(self._evaluators))


DEFAULT_REGISTRY = AccessRuleRegistry()
DEFAULT_REGISTRY.register("STATE_TABLE", evaluate_state_table)


# ══════════════════════════════════════════════════════════════
#  실제 접근
# ══════════════════════════════════════════════════════════════

def _access_defs(definition: dict) -> dict:
    return definition.get("access_control_definitions") or {"rules": {}, "controls": {}}


def current_state(definition: dict, access_state: dict, control_id: str) -> str:
    controls = _access_defs(definition)["controls"]
    if control_id not in controls:
        raise AccessError(f"정의되지 않은 접근 제어 {control_id!r}")
    rec = (access_state or {}).get(control_id)
    if isinstance(rec, dict) and isinstance(rec.get("state"), str):
        return rec["state"]
    return controls[control_id]["initial_state"]


def actual_access(definition: dict, access_state: dict, edge: dict, actor_capabilities=(),
                  registry: AccessRuleRegistry = DEFAULT_REGISTRY) -> AccessDecision:
    """구간을 지금 이 행위자가 실제로 통과할 수 있는가(숨은 상태 포함 — 플레이어 노출 금지)."""
    cid = edge.get("access_ref")
    if cid is None:
        return AccessDecision(PASSABLE, reason="접근 제어 없음")
    acc = _access_defs(definition)
    state = current_state(definition, access_state, cid)
    rid = edge.get("access_rule_ref") or acc["controls"][cid]["rule_ref"]
    rule = acc["rules"].get(rid)
    if rule is None:
        raise AccessError(f"정의되지 않은 접근 규칙 {rid!r}")
    out = registry.evaluator_for(rule["rule_type"])(rule, state, actor_capabilities)
    return AccessDecision(out["result"], out.get("action_policy"), out.get("action_delay_minutes", 0),
                          cid, state, rid, out.get("requires_capability"),
                          f"{cid}={state}")


# ══════════════════════════════════════════════════════════════
#  인지된 접근 (KnownAccessFact)
# ══════════════════════════════════════════════════════════════

def access_fact_key(edge: dict) -> str:
    """사실의 키: 접근 제어가 있으면 control_id, 없으면 edge_id(봉쇄 관찰 등)."""
    return edge.get("access_ref") or edge["edge_id"]


def perceived_access(actor_knowledge: dict, edge: dict) -> dict:
    """행위자가 아는 접근 — {"perceived", "action_policy", "confirmed_at_minute", "source"}.

    실제 access_control_state를 받지 않는다(서명으로 차단). 기록 없음 = UNKNOWN.
    """
    fact = ((actor_knowledge or {}).get("access") or {}).get(access_fact_key(edge))
    if not isinstance(fact, dict) or fact.get("observed_result") not in ACCESS_RESULTS:
        return {"perceived": UNKNOWN, "action_policy": None, "confirmed_at_minute": None, "source": None}
    return {"perceived": _PERCEIVED_OF[fact["observed_result"]],
            "action_policy": fact.get("action_policy"),
            "confirmed_at_minute": fact.get("confirmed_at_minute"),
            "source": fact.get("source")}


def known_access_fact(decision: AccessDecision, *, confirmed_at_minute, source: str) -> dict:
    """실제 관찰(문을 직접 확인 등)에서 KnownAccessFact 레코드를 만든다(쓰기는 typed op)."""
    return {"observed_result": decision.result, "observed_state": decision.state,
            "action_policy": decision.action_policy,
            "confirmed_at_minute": confirmed_at_minute, "source": source}
