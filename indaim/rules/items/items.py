# 아이템 — 일반 재고 정규 해석 · 특수 개별 객체 · 현장 물건 · DYNAMIC 의미 연산 검증·적용 (v7.17)
#
# [권위 — 아이템 통합 구현 계약(결정 1=A·2=A·3=A+·4, R1~R5=A, Q1~Q15)]
#   · 일반 물건의 소유·수량 권위 = session.resources {소유자 이름: {재고 키: 정수}} 하나. 새 소유권 표를 만들지 않는다.
#     재고 키는 문자열 그대로 두되 종류·단위·상태를 읽는 정규 해석을 둔다:
#       "<종류>[<단위>]〈<상태>〉"  — 단위·상태가 없으면 종류 이름만(기존 키와 같다).
#     "항생제 두 알"처럼 수량이 분명한 옛 문구만 "항생제[알]" ×2로 해석한다. "말린 생선 몇 조각"·"항생제(은닉)"·
#     "담배 반 갑" 같은 모호한 문구는 원문 그대로(불투명 종류) — 임의 정수·단위 추정 없음(C01·C05).
#     같은 종류·단위·상태일 때만 수량을 합친다(상자·판·알 무단 병합 없음 — C04).
#   · 특수 물건 = item_instances의 고유 객체(소유자 또는 장소 하나). 같은 물건을 resources에 다시 세지 않는다(C08).
#   · 현장 물건 = place_items의 원천(장소·재고 키·남은 수량). 발견 정보(rt.ambient.*)는 '본 것'이고 물리 잔량은 아니다.
#   · DYNAMIC 전용(Q15). LEGACY는 기존 item_changes 적용을 그대로 쓴다.
#   · 아이템 서비스는 보유·사용·지급 사실과 판정 근거까지만 만든다(Q7). 문·사건·접근 상태는 바꾸지 않는다.
#   · 새 provider 호출 없음 — 연산은 기존 추출(world_observation)의 런타임 확장 필드로 받는다.
#
# [연산 계약 — 모든 연산은 기준 재고에 순서대로 가상 적용, 하나라도 실패하면 전체 실패(부분 적용 없음)]
#   TRANSFER · ACQUIRE · DROP · CONSUME · DAMAGE · REPAIR · OPEN · CRAFT · DISASSEMBLE · READ · USE · PAY · (v7.47 내부) TRAIT
#   수량은 양의 정수, 부족하면 0으로 자르지 않고 실패(D-5). 같은 내용의 연산이 두 번 오면 두 번 적용(D-2 — 반복 보존).
#   재처리 중복은 커밋 경로의 트랜잭션 멱등(_extraction_applied_tx)이 막는다.
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from indaim.rules.items.logic import (  # noqa: E402,F401 — 리팩터링 T-2: 물건 연산 적용은 items 모듈이 소유
    CONDITIONS, COND_NORMAL, DEFAULT_CARRY_LIMITS, FREE_CRAFT_MAX_KINDS, FREE_CRAFT_MAX_QTY, GRADE_UNCARRIABLE, GRADE_WEIGHTS, ItemContractError, StockId, UNGRADED_OP_CAP, UNUSABLE, _CANON, _NUMS, _PHRASE, _Sim, _UNITS, _VOCAB, _alias_kind, _contents_of, _pick_input, _qty, _reconcile_equipment, _subject_of, apply_event_grants, blocked_effects, canonical_bag, carry_load, definitions_of, effect_vocab, format_key, free_tags, free_use, grades_of, key_tags, parse_key, run_ops, sealed, tags_at, traits_of, usable, use_of)

import hashlib

# ── 영속 필드(되감기 대상 게임 정본 — resources와 같은 커밋·이력 단위) ──
ITEM_FIELDS = {
    "item_instances": {},   # {instance_id: {kind, name, owner, place, condition, info_refs, origin, previous_owners}}
    "place_items": {},      # {source_id: {place, key, quantity, origin, by, tx, closed?, inside?}}
                            #   v7.29 용기: closed=True(닫힌 용기) · inside=용기 source_id(그 안의 물건)
    "item_facts": {},       # 직전 커밋 턴의 사용·지급 사실 {tx, used:[종류], paid:[종류]} — 다음 턴 사건 조건 입력
    "equipment": {},        # v7.46 착용·들기 {소유자: {자리: {key: 재고 키}}} — 착용 물건도 재고에 그대로 센다
    "item_traits": {},      # v7.46 이 세션에서 정해진 물건 착용 특성 {종류: {slots, tags, source}} — 한 번 정하면 유지
}


def item_field_defaults() -> dict:
    return copy.deepcopy(ITEM_FIELDS)


# v7.35(D1): 이번 묘사에 처음 나온 물건을 PC가 챙김(item=NEW + name) — 이름이 본문에 그대로 있어야 한다
NEW_KEY = "NEW"
NEW_MAX_KINDS = 3
NEW_MAX_QTY = 10

OPS = ("TRANSFER", "ACQUIRE", "DROP", "CONSUME", "DAMAGE", "REPAIR", "OPEN", "CRAFT", "DISASSEMBLE",
       "READ", "USE", "IDENTIFY", "WEAR", "REMOVE")


# ── v7.46 착용·들기 — 공용 효과 어휘(data/common_item_effects.json) ──
#   물건의 특성(착용 자리·착용 효과 태그·v7.47 사용 효과) 결정 순서 — 한 번 정해지면 그 세션에서 유지한다:
#     ① 시나리오 저작 정의 item_definitions.wear·use  ② 세션 기록 item_traits(이미 정해진 물건)
#     ③ 추출(item_traits — 그 물건을 처음 다룬 턴)·복장 판독(캐릭터 생성 — 시작 물건 전체)이 정한 것(공용 어휘 안, 범위 강제).
#        v7.47(3.1): 참고 묶음 references는 프롬프트 예시로만 — 코드 이름 일치는 폐지했다.
#   착용 효과는 판정 보정 E(굴림 분야의 태그만)·상태 규칙 wearing 조건에, 사용 효과는 상태 처치(care)·섭취 묶음에 쓰인다.
#   물건 상태(젖음·오염)는 condition_blocks로 효과를 끈다. 입은 옷은 PC가 흠뻑 젖었으면 젖은 것으로 본다(Q3 가).








def use_guide_text() -> str:
    """추출·복장 판독용 사용 효과 가이드."""
    voc = effect_vocab().get("use_effects") or {}
    return ("[사용 효과 — 써서 나는 효과. 물건 하나에 "
            f"{int(effect_vocab().get('max_use_per_item') or 3)}개까지, 애매하면 붙이지 않는다]\n"
            + "\n".join(f"· {u}: {r.get('desc', '')} — {r.get('guide', '')}" for u, r in voc.items()))


def guide_text() -> str:
    """추출·복장 판독용 태그 가이드 — 태그(분야 보정·허용 자리): 붙이는 기준 / 붙이지 않는 경우."""
    v = effect_vocab()
    hands = set(v["hand_slots"])
    lines = []
    for t, r in v["tags"].items():
        sl = r.get("slots") or v["slots"]
        where = "손" if set(sl) <= hands else ("몸·손" if set(sl) & hands else "몸")
        eff = f"{r['domain']} {int(r.get('mod') or 0):+d}" if r.get("domain") else "상태"
        lines.append(f"· {t}({eff}·{where}): {r.get('guide') or r.get('desc', '')}")
    return ("[착용 효과 태그 — 같은 계열(약·기본·강)은 하나만, 물건 하나에 "
            f"{int(v.get('max_tags_per_item') or 3)}개까지. 애매하면 약한 쪽·또는 붙이지 않는다]\n" + "\n".join(lines))


def wear_def(kind: str, defs=None) -> dict | None:
    return ((defs or {}).get(kind) or {}).get("wear")








def body_wet(session, owner: str) -> bool:
    """Q3 가: PC 상태 젖음이 wet_stage 이상이면 입은 옷은 젖은 것으로 본다(옷마다 기록하지 않는다)."""
    v = effect_vocab()
    name, need = v.get("wet_status"), int(v.get("wet_stage") or 1)
    if not name or name not in ((getattr(session, "statuses", None) or {}).get(owner) or ()):
        return False
    st = (((getattr(session, "status_clock", None) or {}).get(owner) or {}).get("stage") or {}).get(name, 1)
    return int(st) >= need


def live_tags(key: str, slot, defs=None, session_traits=None, wet: bool = False) -> list:
    """그 자리에서 실제로 나는 착용 효과 — 허용 자리(W2) · 물건 상태 차단(S5) · 젖은 몸(Q3)."""
    tags = tags_at(key_tags(key, defs, session_traits), slot)
    cond = parse_key(key or "", defs)[0].condition
    off = blocked_effects(cond, tags) | (blocked_effects("젖음", tags) if wet and cond == COND_NORMAL else set())
    return [t for t in tags if t not in off]






def worn_tags(session, owner: str, defs=None, equipment=None, traits=None, wet=None) -> frozenset:
    eq = (equipment if equipment is not None else getattr(session, "equipment", None) or {}).get(owner) or {}
    st = traits if traits is not None else getattr(session, "item_traits", None) or {}
    wet = body_wet(session, owner) if wet is None else wet
    out = set()
    for slot, e in eq.items():
        if isinstance(e, dict) and usable(e.get("key", ""), defs):
            out.update(live_tags(e["key"], slot, defs, st, wet))
    return frozenset(out)


def domain_stats(scenario_data=None) -> dict:
    """판정 분야 → 스탯 이름(시나리오 item_effect_domains가 있으면 그 분야만 덮어쓴다)."""
    out = {d: list(v.get("stats") or ()) for d, v in effect_vocab()["domains"].items()}
    for d, stats in ((scenario_data or {}).get("item_effect_domains") or {}).items():
        if isinstance(stats, list):
            out[d] = [str(x) for x in stats]
    return out


def equip_mod(tags, stat: str, scenario_data=None, domain=None) -> int:
    """장비 보정 E — v7.47(W6): 굴림 분야(domain)가 주어지면 그 분야 태그만, 없으면 태그 분야가 그 판정 스탯을 포함할 때."""
    voc, dm = effect_vocab()["tags"], domain_stats(scenario_data)
    if domain:
        return sum(int(voc[t].get("mod") or 0) for t in tags or () if t in voc and voc[t].get("domain") == domain)
    return sum(int(voc[t].get("mod") or 0) for t in tags or ()
               if t in voc and stat in (dm.get(voc[t].get("domain")) or ()))


def worn_auto(session, owner: str, domain: str, defs=None) -> tuple:
    """v7.47(W5·D2 나): 지목 없이 그 분야 굴림에 자동으로 붙는 장비 효과 — (값, [물건 이름(+/-)]).

    몸에 입은 것(손 아닌 자리)은 유리·불리 모두, 손에 든 것은 불리한 효과만(쓸지는 행동 선택 — 유리는 지목형)."""
    voc = effect_vocab()
    hands = set(voc["hand_slots"])
    eq = (getattr(session, "equipment", None) or {}).get(owner) or {}
    st = getattr(session, "item_traits", None) or {}
    total, names = 0, []
    for slot, e in eq.items():
        k = e.get("key") if isinstance(e, dict) else None
        if not k or not usable(k, defs):
            continue
        v = sum(int(voc["tags"][t].get("mod") or 0) for t in tags_at(key_tags(k, defs, st), slot)
                if voc["tags"][t].get("domain") == domain
                and (slot not in hands or int(voc["tags"][t].get("mod") or 0) < 0))
        if v:
            total += v
            names.append(f"{k}({'+' if v > 0 else '-'})")
    return total, names


def worn_on_body(session, owner: str, key: str) -> bool:
    """그 재고 키가 손이 아닌 자리에 입혀져 있는가(자동 효과에 이미 들어간 물건)."""
    hands = set(effect_vocab()["hand_slots"])
    eq = (getattr(session, "equipment", None) or {}).get(owner) or {}
    return any(isinstance(e, dict) and e.get("key") == key and sl not in hands for sl, e in eq.items())


def equip_rows(session, owner: str, defs=None) -> list:
    """판정 후보 [(E키, 표시, 재고 키, 태그)] — 착용·손에 든 것 + 가방 속 '손에 드는' 물건(꺼내 쓰는 것도 같은 행동).

    판정 보정(mod)이 있는 태그를 가진 물건만, 고장·파손 제외."""
    voc = effect_vocab()
    hands = set(voc["hand_slots"])
    st = getattr(session, "item_traits", None) or {}
    eq = (getattr(session, "equipment", None) or {}).get(owner) or {}
    rows, seen = [], set()
    for slot in voc["slots"]:
        e = eq.get(slot)
        if not isinstance(e, dict) or not usable(e.get("key", ""), defs):
            continue
        tags = [t for t in tags_at(key_tags(e["key"], defs, st), slot) if voc["tags"].get(t, {}).get("mod")]
        if tags:
            rows.append((e["key"], f"{e['key']}({slot})", tags))
        seen.add(e["key"])
    bag = canonical_bag((getattr(session, "resources", None) or {}).get(owner) or {}, defs)
    for k in sorted(bag):
        if k in seen or not isinstance(bag[k], int) or bag[k] <= 0 or not usable(k, defs):
            continue
        t, _src = traits_of(parse_key(k, defs)[0].kind, defs, st)
        if t and set(t["slots"]) & hands:
            tags = [x for x in tags_at(t["tags"], voc["hand_slots"][0]) if voc["tags"].get(x, {}).get("mod")]
            if tags:
                rows.append((k, f"{k}(가방 속 — 꺼내 쓰는 것까지 한 행동)", tags))
    return [(f"E{i}", lbl, k, tags) for i, (k, lbl, tags) in enumerate(rows, 1)]


COMBAT_DOMAINS = ("근접", "원거리", "방어")


def npc_equip_mod(session, owner: str, stat: str, defs=None, domain=None) -> tuple:
    """B3: 대결 상대 NPC — 소지한 무기·보호구(전투 분야 태그) 중 그 판정에 가장 유리한 것 하나의 E (물건 이름, 값).
    v7.47: 상대 굴림 분야가 주어지면 그 분야(전투 분야일 때만)의 태그만."""
    best = ("", 0)
    sd = getattr(session, "scenario_data", None)
    voc = effect_vocab()["tags"]
    if domain and domain not in COMBAT_DOMAINS:
        return best
    for _k, _lbl, key, tags in equip_rows(session, owner, defs):
        v = equip_mod([t for t in tags if voc.get(t, {}).get("domain") in COMBAT_DOMAINS], stat, sd, domain)
        if v > best[1]:
            best = (key, v)
    return best


def prompt_profile(session, name: str, profile: dict) -> dict:
    """v7.46(A4): 프롬프트에 싣는 PC 프로필 — 착용 정본이 있으면 생성 때 쓴 복장 글 대신 '착용 기준' 표시.

    복장 글은 캐릭터 생성 때의 차림이다. 입고 벗은 뒤의 차림 권위는 equipment(물건 블록)다."""
    prof = dict(profile or {})
    if not ((getattr(session, "equipment", None) or {}).get(name)):
        return prof
    from indaim.app.profile_gen import get_steps
    for st in get_steps(getattr(session, "scenario_data", None) or {}):
        f = st.get("field")
        if (st.get("args") or {}).get("wear_items") and f in prof:
            prof[f] = "(처음 차림 — 지금 입은 것은 물건 블록의 착용이 기준)"
    return prof


def reference_text() -> str:
    """추출·복장 판독 참고(예시) — 묶음 이름: 예시 → 자리 · 착용 효과 · 사용 효과."""
    return "\n".join(f"· {g['group']}({'·'.join(g['examples'][:4])}) → {'/'.join(g['slots']) or '입지 않음'} · "
                     f"{', '.join(g['tags']) or '착용 효과 없음'}"
                     + (f" · 사용: {', '.join(g['use'])}" if g.get("use") else "")
                     for g in effect_vocab()["references"])


def equip_block(rows, scenario_data=None) -> str:
    """판단층위 — 착용·손에 든 것(코드 확정 효과). 행이 없으면 빈 문자열."""
    if not rows:
        return ""
    voc = effect_vocab()["tags"]

    def tag_txt(t):
        v = voc.get(t) or {}
        mod = int(v.get("mod") or 0)
        return (f"{t}[{v.get('domain')} {mod:+d} — {v.get('desc', '')}"
                + (" · 코드가 자동으로 뺌" if mod < 0 else "") + "]")
    return ("[착용·손에 든 것 — 코드 확정 장비 보정. 효과는 [분야 값]의 그 분야 굴림에만 붙는다(굴림의 domain이 기준). "
            "몸에 입은 것의 효과(유리·불리)와 손에 든 것의 불리한 효과는 지목하지 않아도 코드가 그 분야 굴림에 더한다. "
            "손에 들었거나 가방 속에서 꺼내 쓰는 물건을 굴림에 직접 쓰면 rolls의 item에 그 키 하나(PC 굴림만 — 그 굴림 "
            "분야의 유리한 효과가 있는 것). 장비 보정은 코드가 더하므로 method_mod에 다시 넣지 말 것. "
            "여기 없는 소지품의 쓰임은 지금처럼 method_mod]\n" + "\n".join(f"{k}={lbl}: " + ", ".join(tag_txt(t) for t in tags)
                                                      for k, lbl, _stock, tags in rows))

OPS_FIELD = "item_ops"
TRAITS_FIELD = "item_traits"     # v7.47(3.1) 추출 — 처음 다룬 물건의 특성

# 한국어 수 관형사·수량 단위(언어 수준 — 시나리오·장르 어휘가 아니다)














def capabilities(bag, defs=None) -> tuple:
    """보유 조건(HAS) 이름 — 원문 키 ∪ 정규 종류 ∪ 저작 범주. 상태와 무관한 '가지고 있음'(R2: 보유 ≠ 사용 ≠ 지급)."""
    out = set()
    for k, v in (bag or {}).items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            continue
        out.add(k)
        sid, _ = parse_key(k, defs)
        out.add(sid.kind)
        out.update((defs or {}).get(sid.kind, {}).get("categories") or ())
    return tuple(sorted(out))








# ══════════════════════════════════════════════════════════════
#  관찰 매니페스트(추출 런타임 확장) — 소유자 O · 소지 H · 현장 L 키
# ══════════════════════════════════════════════════════════════

@dataclass
class ItemManifest:
    owners: tuple = ()        # (O키, 소유자 이름, 표시)
    holdings: tuple = ()      # (H키, 소유자 이름, 재고 키 | None, instance_id | None, 수량, 표시)
    places: tuple = ()        # (L키, source_id | None, instance_id | None, 재고 키, 수량, 표시)
    discoveries: tuple = ()   # (D키, info_id, 대상명) — OBJECT 발견만(기존 발견 원천)
    recipes: tuple = ()       # (C키, recipe_id, 표시)
    scene_place: str | None = None
    payments: tuple = ()      # 코드가 이미 처리하는 지급(접근 확인) — 추출이 다시 적지 않는다
    worn: tuple = ()          # v7.46 PC 착용 표시("자리=물건(H키)")
    known: tuple = ()         # v7.46 효과가 정해진 PC 소지 물건("H키=물건: 태그")
    unknown: tuple = ()       # v7.47 효과가 아직 정해지지 않은 소지 물건("H키=물건") — PC·장면 인물(D1 나)

    def owner_of(self, k):
        return next((r[1] for r in self.owners if r[0] == k), None)

    def holding_of(self, k):
        return next((r for r in self.holdings if r[0] == k), None)

    def place_of(self, k):
        return next((r for r in self.places if r[0] == k), None)

    def discovery_of(self, k):
        return next((r for r in self.discoveries if r[0] == k), None)

    def recipe_of(self, k):
        return next((r[1] for r in self.recipes if r[0] == k), None)

    def schema(self) -> dict:
        def enum(rows, extra=()):
            return {"type": "string", "enum": ["NONE"] + list(extra) + [r[0] for r in rows]}
        item_keys = enum(self.holdings + self.places + self.discoveries, (NEW_KEY,))
        ref = {"type": "object", "properties": {"item": enum(self.holdings), "quantity": {"type": "integer"}},
               "required": ["item", "quantity"]}
        out = {"type": "object", "properties": {"name": {"type": "string"}, "unit": {"type": "string"},
                                                "quantity": {"type": "integer"}},
               "required": ["name", "quantity"]}
        return {"type": "array", "description": "확정 묘사에서 실제로 일어난 물건의 이동·소모·변화(후보 키만, 묘사 순서대로).",
                "items": {"type": "object", "properties": {
                    "op": {"type": "string", "enum": list(OPS)},
                    "owner": enum(self.owners),
                    "to": enum(self.owners),
                    "item": item_keys,
                    "name": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "condition": {"type": "string", "enum": ["NONE", COND_NORMAL] + list(CONDITIONS)},
                    "recipe": enum(self.recipes),
                    "slot": {"type": "string", "enum": ["NONE"] + list(effect_vocab()["slots"])},
                    "on": enum(self.owners),
                    "failed": {"type": "boolean"},
                    "inputs": {"type": "array", "items": ref},
                    "outputs": {"type": "array", "items": out}},
                    "required": ["op", "owner", "item", "quantity"]}}

    def traits_schema(self) -> dict:
        """v7.47(3.1·D1) 추출 item_traits — 이번 턴에 다룬, 효과가 아직 정해지지 않은 물건(PC·장면 인물)의 특성."""
        v = effect_vocab()
        return {"type": "array", "description": "이번 턴 처음 다룬 '효과 미정' 물건(PC·장면 인물)의 특성(그 밖은 빈 배열).",
                "items": {"type": "object", "properties": {
                    "item": {"type": "string", "enum": ["NONE", NEW_KEY] + [r.split("=")[0] for r in self.unknown]},
                    "name": {"type": "string"},
                    "slots": {"type": "array", "items": {"type": "string", "enum": list(v["slots"])}},
                    "tags": {"type": "array", "items": {"type": "string", "enum": list(v["tags"])}},
                    "use": {"type": "array", "items": {"type": "string", "enum": list(v.get("use_effects") or {})}}},
                    "required": ["item", "slots", "tags", "use"]}}

    def render(self) -> str:
        lines = ["[물건 — item_ops: 확정 묘사에서 실제로 일어난 물건 변화만, 묘사 순서대로. 동적 세계에서는 "
                 "item_changes를 쓰지 말고 비워 둔다]"]
        lines.append("소유자: " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.owners))
        if self.holdings:
            lines.append("소지: " + ", ".join(f"{r[0]}={r[5]}" for r in self.holdings))
        if self.places:
            lines.append("이 자리의 물건: " + ", ".join(f"{r[0]}={r[5]}" for r in self.places))
        if self.recipes:
            lines.append("저작 제작법: " + ", ".join(f"{k}={lbl}" for k, _, lbl in self.recipes))
        if self.payments:
            lines.append("코드가 이미 처리한 지급(다시 적지 말 것): " + ", ".join(self.payments))
        if self.worn:
            lines.append("PC 착용·손에 든 것(괄호 뒤는 지금 실제로 나는 효과 — 상태 판단의 근거이지 자동이 아니다. "
                         "환경의 세기와 함께 볼 것): " + ", ".join(self.worn))
        if self.known:
            lines.append("효과가 이미 정해진 PC 물건(item_traits에 다시 적지 말 것): " + ", ".join(self.known))
        if self.unknown:
            lines.append("효과가 아직 정해지지 않은 물건(PC·장면 인물) — 이번 묘사에서 그 소유자가 실제로 다룬(줍기·받기·입기·"
                         "들기·쓰기 등) 것만 "
                         "item_traits에 적는다: " + ", ".join(self.unknown))
        lines.append("· TRANSFER owner→to 건넴 · ACQUIRE 현장 물건(L·D키)을 owner가 집음 · DROP owner가 내려놓음 · "
                     "CONSUME 써 버림·잃음·파괴 · DAMAGE/REPAIR 상태 변화(condition) · OPEN 포장 개봉 또는 닫힌 용기(L키) 열기 — "
                     "여는 것만이고 안의 물건을 집으면 따로 ACQUIRE · "
                     "CRAFT 완성된 제작(inputs 재료 소모, outputs 결과 — recipe가 있으면 그 키) · DISASSEMBLE 분해(outputs 회수 부품) · "
                     "READ 글·기록을 실제로 읽음 · USE 소모 없이 사용 · "
                     "USE·CONSUME로 처치·섭취한 대상이 PC가 아닌 인물이면 on에 그 인물 키(PC 자신이면 NONE), "
                     "처치·섭취가 묘사에서 실패했거나 효과가 없었으면 failed=true(물건은 그대로 소모될 수 있다) · "
                     "가방 속 물건이 젖거나 더러워지면 DAMAGE condition=젖음·오염, 말리거나 씻으면 REPAIR(몸에 입은 옷은 PC의 젖음 상태로 코드가 판단하므로 따로 적지 않는다) · "
                     "WEAR PC가 소지한 물건(H키, 또는 이번에 NEW로 챙긴 것은 item=NEW + name)을 입거나 손에 듦 — slot에 자리"
                     "(오른손·왼손 = 손에 듦) · "
                     "REMOVE 입은 것을 벗거나 손에서 내려 소지함(slot을 비우면 그 물건 전부). 버리면 DROP, 건네면 TRANSFER.")
        lines.append("[item_traits — 효과 미정 물건(PC·장면 인물, 또는 PC가 이번에 NEW로 챙긴 물건: item=NEW + name)을 "
                     "이번 턴에 다뤘으면 "
                     "그 물건의 특성을 한 번 정한다: slots = 입거나 들 수 있는 자리(입지 않는 물건은 빈 목록), "
                     "tags = 착용 효과, use = 써서 나는 효과. 실제로 가진 효과만, 애매하면 약한 쪽·또는 비운다. "
                     "한 번 정하면 그 세션에서 바뀌지 않는다]")
        lines.append(guide_text())
        lines.append(use_guide_text())
        lines.append("· 참고 묶음(예시 — 비슷한 물건은 이 정도로): " + " / ".join(
            f"{g['group']}({'·'.join(g['examples'][:3])})=" + (",".join(g['tags']) or "착용 효과 없음")
            + (f"|사용 {','.join(g['use'])}" if g.get("use") else "")
            for g in effect_vocab()["references"]))
        lines.append(f"· ACQUIRE item={NEW_KEY}: 위 키에 없는 물건이 이번 묘사에 처음 나와 PC가 실제로 집어 챙긴 경우만 — "
                     "owner는 PC, name에 본문에 쓰인 물건 이름을 그대로(수량은 quantity). 키가 있는 물건은 그 키를 쓴다.")
        lines.append("· IDENTIFY: 이미 가진 물건(H키)의 정체가 이번 묘사에서 밝혀진 경우만 — name에 본문에 쓰인 새 이름"
                     "(예: 정체 모를 금속 물체 → 손전등). 물건이 바뀌거나 생기는 것이 아니다.")
        lines.append("· 보여 주기·만지기·시도만 한 것, 실패한 제작의 결과물, 묘사에 없는 이동은 넣지 말 것. 수량은 묘사에 "
                     "드러난 정수(불명이면 1). 소지에 없는 물건을 주거나 쓰는 묘사라도 새 키를 지어내지 말 것.")
        return "\n".join(lines) + "\n"


def _owner_label(name, pc_name):
    return f"{name}(PC)" if name == pc_name else name






def build_manifest(session, *, pc_name: str, scene_owner_names=(), scene_place=None, defs=None,
                   discovery_rows=(), recipes=None, payments=()) -> ItemManifest:
    """현재 장면 기준 후보(소유자=PC+장면 NPC, 소지·현장 물건·발견물·저작 제작법). 숨은 원문·은닉 해석 없음."""
    names = [pc_name] + [n for n in scene_owner_names if n and n != pc_name]
    owners = tuple((f"O{i}", n, _owner_label(n, pc_name)) for i, n in enumerate(names, 1))
    res = getattr(session, "resources", None) or {}
    inst = getattr(session, "item_instances", None) or {}
    hold = []
    for n in names:
        for k, v in sorted(canonical_bag(res.get(n) or {}, defs).items()):
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                hold.append((n, k, None, v, f"{_owner_label(n, pc_name)}의 {k} ×{v}"))
        for iid in sorted(i for i, r in inst.items() if r.get("owner") == n):
            r = inst[iid]
            hold.append((n, None, iid, 1, f"{_owner_label(n, pc_name)}의 {_inst_label(r)}"))
    holdings = tuple((f"H{i}",) + h for i, h in enumerate(hold, 1))
    plc = []
    if scene_place:
        pi = getattr(session, "place_items", None) or {}
        here = [sid for sid in sorted(pi) if pi[sid].get("place") == scene_place and int(pi[sid].get("quantity") or 0) > 0]
        lkey = {sid: f"L{i}" for i, sid in enumerate(here, 1)}
        for sid in here:
            r = pi[sid]
            if r.get("closed"):
                where = "바닥에 놓인 것 · 닫힌 용기 — OPEN으로 열 수 있음"
            elif r.get("inside") in lkey:
                where = (f"{lkey[r['inside']]} 안 · 닫혀 있어 열기 전에는 꺼낼 수 없음" if sealed(pi, r)
                         else f"{lkey[r['inside']]} 안")
            else:
                where = "바닥에 놓인 것"
            plc.append((sid, None, r["key"], int(r["quantity"]), f"{r['key']} ×{r['quantity']}({where})"))
        for iid in sorted(i for i, r in inst.items() if r.get("place") == scene_place and not r.get("owner")):
            plc.append((None, iid, None, 1, f"{_inst_label(inst[iid])}(바닥에 놓인 것)"))
    places = tuple((f"L{i}",) + p for i, p in enumerate(plc, 1))
    disc = tuple((k, iid, t) for k, iid, t, kind in (discovery_rows or ()) if kind == "OBJECT")
    rc = tuple((f"C{i}", rid, _recipe_label(rid, r)) for i, (rid, r) in enumerate(sorted((recipes or {}).items()), 1))
    hkey = {h[2]: h[0] for h in holdings if h[1] == pc_name and h[2]}
    eq = (getattr(session, "equipment", None) or {}).get(pc_name) or {}
    st = getattr(session, "item_traits", None) or {}
    wet = body_wet(session, pc_name)

    def _worn(sl, k):                                                    # W4: 효과까지(젖음·추위 판단 근거)
        live = live_tags(k, sl, defs, st, wet)
        off = [t for t in tags_at(key_tags(k, defs, st), sl) if t not in live]
        return (f"{sl}={k}({hkey.get(k, '?')}" + "".join(f"·{t}" for t in live)
                + "".join(f"·{t}(젖거나 더러워 꺼짐)" for t in off) + ")")
    worn = tuple(_worn(sl, e.get("key")) for sl, e in eq.items() if isinstance(e, dict))
    known, unknown = [], []
    for h in holdings:
        if not h[2]:
            continue
        t, _src = traits_of(parse_key(h[2], defs)[0].kind, defs, st)
        if t is None:
            unknown.append(f"{h[0]}={h[2]}")             # D1 나: 장면 인물 물건도 처음 다룰 때 정한다
        elif h[1] == pc_name:
            known.append(f"{h[0]}={h[2]}: " + (", ".join(t["tags"]) or "착용 효과 없음")
                         + (f" | 사용 {', '.join(t['use'])}" if t.get("use") else ""))
    return ItemManifest(owners, holdings, places, disc, rc, scene_place, tuple(payments), worn, tuple(known),
                        tuple(unknown))


def _inst_label(r: dict) -> str:
    cond = r.get("condition") or COND_NORMAL
    return (r.get("name") or r.get("kind") or "?") + ("" if cond == COND_NORMAL else f"〈{cond}〉")


def _recipe_label(rid, r) -> str:
    ins = " + ".join(f"{i['kind']}{('[' + i['unit'] + ']') if i.get('unit') else ''}×{i.get('count', 1)}"
                     for i in r.get("inputs") or ())
    outs = " + ".join(f"{o['kind']}×{o.get('count', 1)}" for o in r.get("outputs") or ())
    return f"{r.get('name') or rid}({ins} → {outs})"


# ══════════════════════════════════════════════════════════════
#  가상 적용 · 검증 · 정본 적용(같은 함수 — 커밋에서 다시 검증한다)
# ══════════════════════════════════════════════════════════════

@dataclass
class ItemPlan:
    """검증된 이번 턴 아이템 연산(정규형) — 커밋 경로가 같은 연산을 다시 검증·적용한다."""
    transaction_id: str
    ops: list = field(default_factory=list)
    scene_place: str | None = None
    discovery_takes: list = field(default_factory=list)   # 수거로 소진할 발견 D키
    read_info_ids: list = field(default_factory=list)     # READ로 PC가 내용을 알게 될 정보(정보·인지 경로)
    facts: dict = field(default_factory=dict)              # {used:[종류], paid:[종류]}
    report: list = field(default_factory=list)             # [{target,item,delta,after}] 마스터 보고 호환
    diagnostics: list = field(default_factory=list)
    pc_name: str | None = None
    defs: dict = field(default_factory=dict)               # 검증에 쓴 아이템 정의 사본(커밋에서 같은 기준으로 재검증)
    recipes: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"transaction_id": self.transaction_id, "ops": copy.deepcopy(self.ops),
                "scene_place": self.scene_place, "discovery_takes": list(self.discovery_takes),
                "read_info_ids": list(self.read_info_ids), "facts": copy.deepcopy(self.facts)}














# ══════════════════════════════════════════════════════════════
#  추출 → 정규 연산
# ══════════════════════════════════════════════════════════════

def _squash(text) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def named_in(name: str, narration_text) -> bool:
    """v7.37(A3): 물건 이름의 낱말이 모두 본문에 있는가("금속 물체" ← "금속의 질감 … 물체"). 표기 기호는 거부."""
    words = str(name or "").split()
    if not words or any(c in name for c in "[]〈〉"):
        return False
    body = _squash(narration_text)
    return all(_squash(w) in body for w in words)


def _new_source(r, owner, pc_name, narration_text, seen, why) -> dict:
    """v7.35(D1) 처음 나온 물건 — PC만, 본문에 그 이름(v7.37: 낱말 단위)이 있을 때만, 한 턴 종류·수량 상한."""
    name = str(r.get("name") or "").strip()
    if owner != pc_name:
        raise ItemContractError(f"{why}: {NEW_KEY} 획득은 PC만")
    if not named_in(name, narration_text):
        raise ItemContractError(f"{why}: {NEW_KEY} 물건 이름 {name!r}이(가) 이번 묘사 본문에 없음")
    q = r.get("quantity")
    if isinstance(q, int) and not isinstance(q, bool) and q > NEW_MAX_QTY:
        raise ItemContractError(f"{why}: {NEW_KEY} 물건 수량 상한 {NEW_MAX_QTY}")
    seen.add(name)
    if len(seen) > NEW_MAX_KINDS:
        raise ItemContractError(f"{why}: {NEW_KEY} 물건은 한 턴 {NEW_MAX_KINDS}종까지")
    return {"new": name}


TRAITS_MAX = 8


def normalize_raw_ops(manifest: ItemManifest, raw_ops, *, discovery_taken=(), pc_name=None,
                      narration_text: str = "", raw_traits=()) -> list:
    """추출 item_ops(+ 발견 D키 수거) → 정규 연산. 후보 밖 키·형식 오류는 ItemContractError(재추출)."""
    out, new_names = [], set()
    for n, r in enumerate(raw_ops or ()):
        if not isinstance(r, dict):
            raise ItemContractError(f"연산 {n + 1}: 형식 오류")
        op = r.get("op")
        if op not in OPS:
            raise ItemContractError(f"연산 {n + 1}: 알 수 없는 op {op!r}")
        why = f"연산 {n + 1}({op})"
        if op in ("OPEN", "READ", "USE") and r.get("item") in (NEW_KEY, "NONE", None, ""):
            continue      # v9.3.1(사용자 결정): 키 없는 물건을 열기·읽기·쓰기 — 바꿀 상태가 없다. 이 연산만 빼고 나머지는 검증
        owner = manifest.owner_of(r.get("owner"))
        if owner is None:
            raise ItemContractError(f"{why}: 소유자 키 {r.get('owner')!r} 후보 밖")
        item = r.get("item")
        hold = manifest.holding_of(item)
        q = r.get("quantity")
        base = {"op": op, "qty": q}
        if op == "ACQUIRE":
            pl, dr = manifest.place_of(item), manifest.discovery_of(item)
            if item == NEW_KEY:
                src = _new_source(r, owner, pc_name, narration_text, new_names, why)
            elif pl is not None:
                src = {"place_source": pl[1]} if pl[1] else {"instance": pl[2]}
            elif dr is not None:
                src = {"discovery": dr[0], "info_id": dr[1], "name": dr[2]}
            else:
                raise ItemContractError(f"{why}: 현장 물건 키 {item!r} 후보 밖")
            out.append({**base, "to": owner, "source": src})
            continue
        if op == "OPEN" and hold is None and manifest.place_of(item) is not None:
            pl = manifest.place_of(item)                 # v7.29: 이 자리의 닫힌 용기 열기
            if pl[1] is None:
                raise ItemContractError(f"{why}: {item!r}은(는) 열 수 있는 용기가 아님")
            out.append({**base, "from": owner, "place_source": pl[1], "qty": 1})
            continue
        care = {}
        if op in ("USE", "CONSUME"):                     # v7.47 처치·섭취 대상(없으면 쓴 사람)·실패
            on = manifest.owner_of(r.get("on")) if r.get("on") not in (None, "", "NONE") else None
            if r.get("on") not in (None, "", "NONE") and on is None:
                raise ItemContractError(f"{why}: 대상 키 {r.get('on')!r} 후보 밖")
            care = {"on": on or owner, "failed": bool(r.get("failed"))}
        if op in ("READ", "USE") and hold is None:
            pl = manifest.place_of(item)
            if pl is None:
                raise ItemContractError(f"{why}: 대상 키 {item!r} 후보 밖")
            out.append({**base, "from": owner, "place_source": pl[1], "instance": pl[2], "qty": q or 1, **care})
            continue
        if op == "WEAR" and item == NEW_KEY:
            name = str(r.get("name") or "").strip()
            if not named_in(name, narration_text):
                raise ItemContractError(f"{why}: 착용할 {NEW_KEY} 물건 이름 {name!r}이(가) 이번 묘사 본문에 없음")
            out.append({"op": op, "from": owner, "new_name": name, "slot": r.get("slot"), "tags": r.get("tags") or [],
                        "qty": 1})
            continue
        if op == "CRAFT" and r.get("recipe") not in (None, "", "NONE"):
            rid = manifest.recipe_of(r.get("recipe"))
            if rid is None:
                raise ItemContractError(f"{why}: 제작법 키 후보 밖")
            out.append({"op": op, "from": owner, "recipe": rid, "qty": q or 1})
            continue
        if op == "CRAFT":
            ins = []
            for i in r.get("inputs") or ():
                h = manifest.holding_of((i or {}).get("item"))
                if h is None or h[1] != owner or h[2] is None:
                    raise ItemContractError(f"{why}: 재료 키 후보 밖 또는 다른 소유자")
                ins.append({"stock": h[2], "qty": (i or {}).get("quantity")})
            outs = [{"name": o.get("name"), "unit": o.get("unit"), "qty": o.get("quantity")}
                    for o in r.get("outputs") or () if isinstance(o, dict)]
            out.append({"op": op, "from": owner, "inputs": ins, "outputs": outs, "qty": 1})
            continue
        if hold is None:
            raise ItemContractError(f"{why}: 소지 키 {item!r} 후보 밖")
        # 키는 물건(재고 키·특수 객체)을 가리키고 owner가 지금 가진 사람이다 — 같은 턴 선행 양도로 받은 물건도
        # 이어 넘길 수 있다(A03). 실제 보유는 가상 적용이 순서대로 검증한다(없으면 전체 실패).
        rec = {**base, "from": owner, "stock": hold[2], "instance": hold[3], **care}
        if op == "TRANSFER":
            to = manifest.owner_of(r.get("to"))
            if to is None or to == owner:
                raise ItemContractError(f"{why}: 받는 사람 키 후보 밖")
            rec["to"] = to
        elif op in ("DAMAGE", "REPAIR"):
            rec["condition"] = r.get("condition")
        elif op == "IDENTIFY":
            name = str(r.get("name") or "").strip()
            if owner != pc_name:
                raise ItemContractError(f"{why}: 정체 확인은 PC 소지품만")
            if not named_in(name, narration_text):
                raise ItemContractError(f"{why}: 새 이름 {name!r}이(가) 이번 묘사 본문에 없음")
            rec["name"] = name
        elif op == "WEAR":
            rec.update(slot=r.get("slot"), tags=r.get("tags") or [], qty=1)
        elif op == "REMOVE":
            rec.update(slot=None if r.get("slot") in (None, "", "NONE") else r.get("slot"), qty=1)
        elif op == "DISASSEMBLE":
            rec["outputs"] = [{"name": o.get("name"), "unit": o.get("unit"), "qty": o.get("quantity")}
                              for o in r.get("outputs") or () if isinstance(o, dict)]
        out.append(rec)
    # v7.47(3.1) item_traits → TRAIT 연산(맨 앞 — 같은 턴 입기·쓰기가 정해진 특성을 본다). 효과 미정 물건 또는 이번 NEW만.
    traits_ops = []
    for n, r in enumerate((raw_traits or ())[:TRAITS_MAX], 1):
        if not isinstance(r, dict):
            continue
        item = r.get("item")
        if item == NEW_KEY:
            name = str(r.get("name") or "").strip()
            if name not in new_names and not any(o.get("new_name") == name for o in out):
                continue                               # 이번에 챙긴 NEW가 아니면 버린다(지어낸 물건 없음)
            kind_ = _alias_kind(name, None)
        else:
            h = manifest.holding_of(item)
            if h is None or h[2] is None or f"{item}={h[2]}" not in manifest.unknown:
                continue
            kind_ = parse_key(h[2])[0].kind
        traits_ops.append({"op": "TRAIT", "kind": kind_, "slots": list(r.get("slots") or []),
                           "tags": list(r.get("tags") or []), "use": list(r.get("use") or []), "qty": 1})
    out = traits_ops + out
    # 발견 D키(discovery_taken)로만 지목된 수거 — 같은 D키 ACQUIRE가 없을 때만 하나의 수거로 정규화(이중 집계 없음)
    have = {o["source"]["discovery"] for o in out if o["op"] == "ACQUIRE" and o["source"].get("discovery")}
    for k in discovery_taken or ():
        dr = manifest.discovery_of(k)
        if dr is not None and k not in have:
            out.append({"op": "ACQUIRE", "to": pc_name, "qty": 1,
                        "source": {"discovery": dr[0], "info_id": dr[1], "name": dr[2]}})
            have.add(k)
    return out


def legacy_changes_to_ops(manifest: ItemManifest, item_deltas, *, pc_name, taken_names=None) -> tuple:
    """DYNAMIC에서 item_ops 없이 item_changes만 온 경우 — (연산, 진단).

    소지한 물건의 감소만 CONSUME으로 받는다. 원천 불명 증가·소지 후보에 없는 감소는 적용하지 않고 진단만 남긴다
    (B03 '변경 거부' — 같은 추출의 다른 정본 변화는 그대로). 이름 근사로 다른 물건과 잇지 않는다.
    예외(v7.14 중복 방지 유지): 같은 추출의 발견 D키 수거와 정확히 같은 이름의 PC +수량은 그 수거의 중복 기재로
    흡수한다(수거 건수까지) — 두 번 세지 않는다.
    """
    out, diags = [], []
    left = dict(taken_names or {})
    for e in item_deltas or ():
        d = int(e.get("delta") or 0)
        if d > 0 and e.get("target") == pc_name and left.get(e.get("item"), 0) >= d:
            left[e["item"]] -= d
            continue
        if d >= 0:
            if d > 0:
                diags.append({"code": "ITEM_GAIN_UNSOURCED_REJECTED", "target": e.get("target"),
                              "item": e.get("item"), "delta": d})
            continue
        owner = e.get("target")
        key = next((h[2] for h in manifest.holdings if h[1] == owner and h[2] == e.get("item")), None)
        if key is None:
            diags.append({"code": "ITEM_LOSS_NOT_HELD_REJECTED", "target": owner, "item": e.get("item"), "delta": d})
            continue
        out.append({"op": "CONSUME", "from": owner, "stock": key, "instance": None, "qty": -d})
    return out, diags


def build_plan(session, manifest: ItemManifest, raw_ops, *, defs=None, recipes=None, pc_name, transaction_id,
               discovery_taken=(), legacy_item_deltas=(), payments=(), narration_text: str = "",
               raw_traits=()) -> ItemPlan:
    """추출 → 검증된 ItemPlan. 실패하면 ItemContractError(정본 불변, 추출 재시도)."""
    ops, diags = list(payments or ()), []
    if raw_ops:
        ops += normalize_raw_ops(manifest, raw_ops, discovery_taken=discovery_taken, pc_name=pc_name,
                                 narration_text=narration_text, raw_traits=raw_traits)
    else:
        ops += normalize_raw_ops(manifest, (), discovery_taken=discovery_taken, pc_name=pc_name,
                                 raw_traits=raw_traits)
        if legacy_item_deltas:
            taken = {}
            for o in ops:
                src = o.get("source") or {}
                if o.get("op") == "ACQUIRE" and src.get("discovery") and o.get("to") == pc_name:
                    taken[src.get("name")] = taken.get(src.get("name"), 0) + int(o.get("qty") or 1)
            lops, ldiags = legacy_changes_to_ops(manifest, legacy_item_deltas, pc_name=pc_name, taken_names=taken)
            ops += lops
            diags += ldiags
    sim = run_ops(session, ops, defs=defs, pc_name=pc_name, scene_place=manifest.scene_place,
                  transaction_id=transaction_id, recipes=recipes)
    if raw_ops and legacy_item_deltas:
        diags.append({"code": "ITEM_CHANGES_IGNORED_DYNAMIC", "count": len(legacy_item_deltas)})
    return ItemPlan(transaction_id, ops, manifest.scene_place, list(sim.takes), list(sim.reads),
                    {"used": sorted(set(sim.used)), "paid": sorted(set(sim.paid))}, sim.report, diags,
                    pc_name, copy.deepcopy(defs or {}), copy.deepcopy(recipes or {}))


def apply_plan(session, plan: ItemPlan) -> list:
    """커밋 경로 — 같은 연산을 정본에 다시 검증·적용한다(불일치면 예외 — 커밋 owner로 전파). 보고 행 반환."""
    from indaim.turn import settle_capture as _sc
    _sc.note("plan", {"ops": plan.ops, "defs": plan.defs, "pc_name": plan.pc_name, "scene_place": plan.scene_place,
                      "tx": plan.transaction_id, "recipes": plan.recipes})
    sim = run_ops(session, plan.ops, defs=plan.defs, pc_name=plan.pc_name, scene_place=plan.scene_place,
                  transaction_id=plan.transaction_id, recipes=plan.recipes)
    res = dict(getattr(session, "resources", None) or {})
    for owner in sim.touched:
        res[owner] = sim.res.get(owner) or {}
    session.resources = res
    session.item_instances = sim.inst
    session.place_items = sim.places
    session.equipment = sim.equip
    session.item_traits = sim.traits
    session.item_facts = {"tx": plan.transaction_id, **copy.deepcopy(plan.facts)}
    return sim.report


def current_facts(session) -> dict:
    """다음 계획이 볼 사용·지급 사실 — 마지막 커밋 턴이 바로 그 사실을 만든 턴일 때만(그 외 빈 사실).

    item_facts.tx = 사실을 만든 트랜잭션, world_version.cause_id = 마지막 커밋 원인. 커밋된 플레이어 턴은 빈 묶음이어도
    world_version을 전진시키므로, 아이템 계획 없이 커밋된 뒤 턴(NARRATE 등)이 끼면 옛 사실은 더 이상 새 사실이 아니다.
    커밋하지 않은 시도(ASK 등)는 둘 다 바꾸지 않아 사실이 실제 다음 커밋 계획까지 남는다. 둘 다 되감기 대상이라 함께 복원된다.
    """
    facts = getattr(session, "item_facts", None) or {}
    wv = getattr(session, "world_version", None) or {}
    if facts.get("evaluated_same_turn"):
        return {"used": [], "paid": []}      # P1(D4): 만든 턴의 보충 진행이 이미 평가했다 — 다음 턴 재평가 없음(이중 반응 금지)
    if not facts.get("tx") or wv.get("cause_id") != facts.get("tx"):
        return {"used": [], "paid": []}
    return {"used": list(facts.get("used") or ()), "paid": list(facts.get("paid") or ())}


# ══════════════════════════════════════════════════════════════
#  접근 지급(R4) · 정의 · 프롬프트 블록
# ══════════════════════════════════════════════════════════════

def payment_op(session, *, pc_name, capability: str, quantity: int, defs=None, to=None) -> dict | None:
    """접근 확인의 실제 지급 — 그 이름(종류)을 정확히 quantity만큼 넘길 수 있는 재고 키(결정적 첫 일치). 없으면 None."""
    bag = canonical_bag((getattr(session, "resources", None) or {}).get(pc_name) or {}, defs)
    for k in sorted(bag):
        v = bag[k]
        sid = parse_key(k, defs)[0]
        if (k == capability or sid.kind == capability) and isinstance(v, int) and v >= quantity \
                and sid.condition not in UNUSABLE:
            return {"op": "PAY", "from": pc_name, "to": to, "stock": k, "instance": None, "qty": quantity,
                    "kind": sid.kind}
    return None




def item_block(session, *, pc_name, scene_owner_names=(), scene_place=None, defs=None) -> str:
    """지시층위 — 정본 재고(PC·장면 인물)·현장 물건·사용 가능 여부(코드 확정). 캐시 초기 소지품보다 우선."""
    res = getattr(session, "resources", None) or {}
    inst = getattr(session, "item_instances", None) or {}
    lines = ["\n[물건 — 정본 소지(코드 확정 · 룰북의 초기 소지품보다 우선). 여기 없는 물건을 가진 것처럼 쓰지 말 것. "
             "〈고장〉〈파손〉 물건은 제 기능으로 쓸 수 없다]"]
    worn = (getattr(session, "equipment", None) or {}).get(pc_name) or {}
    traits = getattr(session, "item_traits", None) or {}
    if worn:
        lines.append("· PC 착용·손에 든 것(정본 — 프로필 복장 글보다 우선): " + ", ".join(
            f"{sl} {e.get('key')}" + (f"({'·'.join(tags_at(key_tags(e['key'], defs, traits), sl))})"
                                       if tags_at(key_tags(e.get("key"), defs, traits), sl) else "")
            for sl, e in worn.items() if isinstance(e, dict)))
    for n in [pc_name] + [x for x in scene_owner_names if x != pc_name]:
        bag = canonical_bag(res.get(n) or {}, defs)
        items = [f"{k} ×{v}" for k, v in sorted(bag.items()) if isinstance(v, int) and v > 0]
        items += [_inst_label(r) for r in inst.values() if r.get("owner") == n]
        if n == pc_name or items:
            lines.append(f"· {_owner_label(n, pc_name)}: " + (", ".join(items) if items else "(없음)"))
    here = []
    if scene_place:
        pi = getattr(session, "place_items", None) or {}
        here = []
        for r in pi.values():
            if r.get("place") != scene_place or int(r.get("quantity") or 0) <= 0:
                continue
            note = ""
            if r.get("closed"):
                note = "(닫힌 용기)"
            elif r.get("inside") in pi:
                note = (f"({pi[r['inside']]['key']} 안 — 닫혀 있다: 열기 전에는 보이지 않으므로 서술하지 말 것)"
                        if sealed(pi, r) else f"({pi[r['inside']]['key']} 안)")
            here.append(f"{r['key']} ×{r['quantity']}{note}")
        here += [_inst_label(r) for r in inst.values() if r.get("place") == scene_place and not r.get("owner")]
    if here:
        lines.append("· 이 자리에 놓인 물건: " + ", ".join(here))
    return "\n".join(lines) + "\n"


# ══════════════════════════════════════════════════════════════
#  genesis 초기 재고(INITIALIZATION과 같은 strict 저장)
# ══════════════════════════════════════════════════════════════



def genesis_item_state(definition: dict, session, start_items=(), *, source_prefix: str = "start") -> dict:
    """DYNAMIC genesis의 아이템 초기 상태 — {resources, item_instances}(+ 시작 물건이 있으면 place_items)(세션 불변, 순수).

    · 모든 소유자 재고를 정규형으로(옛 '항생제 두 알' → 항생제[알] ×2, 모호한 문구는 원문 유지).
    · 저작 unique 물건의 instances를 고유 객체로 만든다. 같은 소유자 재고에 그 종류가 있으면 객체 수만큼 재고에서
      덜어 낸다(같은 물건을 resources와 객체로 이중 보유하지 않음 — C08). 이미 객체가 있으면 다시 만들지 않는다.
    · v7.26 start_items: 고른 시작 틀의 world_start.items(컴파일러 검증) — 장소 물건은 place_items 원천
      `{source_prefix}.{순번}`(origin START_FRAME), 인물 소지는 그 소유자 재고에 더한다. 그 시작에서만 생긴다.
    """
    defs = definitions_of(definition)
    res = {o: canonical_bag(b, defs) if isinstance(b, dict) else b
           for o, b in (getattr(session, "resources", None) or {}).items()}
    inst = copy.deepcopy(getattr(session, "item_instances", None) or {})
    for kind in sorted(k for k in defs if not k.startswith("__")):
        d = defs[kind]
        for it in d.get("instances") or ():
            iid = it["instance_id"]
            if iid in inst:
                continue
            owner = it.get("owner")
            if owner is not None:
                bag = res.get(owner) if isinstance(res.get(owner), dict) else {}
                for k in sorted(bag):
                    if parse_key(k, defs)[0].kind == kind and isinstance(bag[k], int) and bag[k] > 0:
                        bag[k] -= 1
                        if bag[k] == 0:
                            bag.pop(k)
                        break
                res[owner] = bag
            inst[iid] = {"kind": kind, "name": d.get("name") or kind, "owner": owner,
                         "place": None if owner is not None else it.get("place"), "condition": COND_NORMAL,
                         "info_refs": list(d.get("info_refs") or ()), "origin": "AUTHORED",
                         "previous_owners": []}
    if not start_items:
        return {"resources": res, "item_instances": inst}
    places = copy.deepcopy(getattr(session, "place_items", None) or {})
    for n, it in enumerate(start_items):
        sid, mult = parse_key(it["key"], defs)
        qty = int(it["quantity"]) * mult
        if it.get("owner") is not None:
            bag = res.get(it["owner"]) if isinstance(res.get(it["owner"]), dict) else {}
            bag[sid.key] = (bag.get(sid.key, 0) if isinstance(bag.get(sid.key, 0), int) else 0) + qty
            res[it["owner"]] = bag
        else:
            box = f"{source_prefix}.{n}"
            places[box] = {"place": it["place"], "key": sid.key, "quantity": qty,
                           "origin": "START_FRAME", "by": None, "tx": None}
            if it.get("contains"):                        # v7.29: 닫힌 용기와 그 안의 물건
                places[box]["closed"] = True
                for m, c in enumerate(it["contains"]):
                    csid, cmult = parse_key(c["key"], defs)
                    places[f"{box}.{m}"] = {"place": it["place"], "key": csid.key,
                                            "quantity": int(c["quantity"]) * cmult, "origin": "START_FRAME",
                                            "by": None, "tx": None, "inside": box}
    return {"resources": res, "item_instances": inst, "place_items": places}
