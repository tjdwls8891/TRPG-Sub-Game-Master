"""물건 연산의 순수 적용 — 재고 해석·정규 키·운반 부담·특성(착용·사용 효과)·연산 가상 적용(_Sim·run_ops)·사건 지급.

옛 core.items에서 옮겼다(리팩터링 T-2). 상태 객체(resources·item_instances·place_items·equipment·item_traits 속성을
가진 아무 객체 — 세션 또는 이름공간)를 읽고 그 사본 위에서 연산을 검증·적용한다. provider·디스코드를 모른다.
물건 문법·의미는 core.items 머리 주석이 정본 설명이다.
"""
from __future__ import annotations

import copy
import hashlib  # noqa: F401
import re
from dataclasses import dataclass, field

COND_NORMAL = "정상"


CONDITIONS = ("손상", "고장", "파손", "젖음", "오염")   # 사건형 상태(수치 내구도 없음 — Q5) · v7.47 젖음·오염(효과 차단 — S5)


UNUSABLE = frozenset({"고장", "파손"})            # 사용 불가 상태


GRADE_WEIGHTS = {0: 0, 1: 1, 2: 4, 3: 12, 4: 30}  # 무게·부피 등급 → 부담 단위(5 = 운반 불가)


GRADE_UNCARRIABLE = 5


DEFAULT_CARRY_LIMITS = {"mass": 40, "bulk": 40}    # 저작 등급이 있는 물건에만 적용(R3 완화형)


UNGRADED_OP_CAP = 99                               # 등급 미저작 물건 한 연산 획득 상한(수백 개 조용한 획득 방지)


FREE_CRAFT_MAX_KINDS = 3


FREE_CRAFT_MAX_QTY = 10


_VOCAB = None


def effect_vocab() -> dict:
    global _VOCAB
    if _VOCAB is None:
        import json
        import pathlib
        try:
            _VOCAB = json.loads((pathlib.Path(__file__).resolve().parents[3] / "data" / "common_item_effects.json")
                                .read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[아이템] 효과 어휘 로드 실패: {e}")
            _VOCAB = {}
        for k, dv in (("slots", []), ("hand_slots", []), ("tags", {}), ("domains", {}), ("references", [])):
            _VOCAB.setdefault(k, dv)
    return _VOCAB


def free_tags(tags, slot=None) -> list:
    """추출·복장 판독이 붙인 태그의 범위 강제 — 공용 어휘 안 · 그 자리에 허용된 태그 · 같은 group은 한 단계만
    (mod가 가장 낮은 것 — 보수적으로) · 물건 하나에 max_tags_per_item개까지(앞에서부터). 강한 효과도 붙일 수 있다."""
    v = effect_vocab()
    voc, cap = v["tags"], int(v.get("max_tags_per_item") or 3)
    out, by_group = [], {}
    for t in dict.fromkeys(str(x) for x in (tags or ())):
        rec = voc.get(t)
        if rec is None or (slot and rec.get("slots") and slot not in rec["slots"]):
            continue
        g = rec.get("group")
        if g and g in by_group:
            prev = by_group[g]
            if int(rec.get("mod") or 0) < int(voc[prev].get("mod") or 0):
                out[out.index(prev)] = t
                by_group[g] = t
            continue
        if g:
            by_group[g] = t
        out.append(t)
    return out[:cap]


def free_use(uses) -> list:
    """사용 효과 범위 강제 — 공용 어휘 안, 물건 하나에 max_use_per_item개까지(앞에서부터)."""
    v = effect_vocab()
    voc, cap = v.get("use_effects") or {}, int(v.get("max_use_per_item") or 3)
    return [u for u in dict.fromkeys(str(x) for x in (uses or ())) if u in voc][:cap]


def traits_of(kind: str, defs=None, session_traits=None) -> tuple:
    """(특성 {slots, tags, use}, 출처) — 출처 AUTHORED·SESSION, 아직 정해지지 않았으면 (None, None)."""
    d = (defs or {}).get(kind) or {}
    w = d.get("wear")
    if w or d.get("use"):
        return {"slots": list((w or {}).get("slots") or []), "tags": list((w or {}).get("tags") or []),
                "use": list(d.get("use") or [])}, "AUTHORED"
    rec = (session_traits or {}).get(kind)
    if isinstance(rec, dict):
        return {"slots": list(rec.get("slots") or []), "tags": list(rec.get("tags") or []),
                "use": list(rec.get("use") or [])}, "SESSION"
    return None, None


def use_of(kind: str, defs=None, session_traits=None) -> list:
    t, _src = traits_of(kind, defs, session_traits)
    return list(t["use"]) if t else []


def blocked_effects(cond: str, item_tags=()) -> set:
    """S5: 물건 상태가 끄는 효과(착용 태그·사용 효과) — keep_if_tag의 태그가 있는 물건은 그 효과를 유지한다."""
    rule = (effect_vocab().get("condition_blocks") or {}).get(cond) or {}
    keep = rule.get("keep_if_tag") or {}
    return {e for e in rule.get("block") or () if not (keep.get(e) and keep[e] in (item_tags or ()))}


def key_tags(key: str, defs=None, session_traits=None) -> list:
    t, _src = traits_of(parse_key(key or "", defs)[0].kind, defs, session_traits)
    return list(t["tags"]) if t else []


def tags_at(tags, slot) -> list:
    """W2: 그 자리에서 실제로 나는 효과만 — 태그별 허용 자리(옷을 손에 들면 보온·방호 없음, 무기를 입으면 무기 효과 없음)."""
    voc = effect_vocab()["tags"]
    return [t for t in tags or () if t in voc and (not voc[t].get("slots") or slot in voc[t]["slots"])]


_NUMS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}


_UNITS = ("알", "개", "병", "상자", "판", "갑", "롤", "켤레", "타래", "봉지", "장", "자루", "묶음", "통", "캔",
          "봉", "권", "벌", "쌍", "정", "포", "팩", "다발", "토막")


_PHRASE = re.compile(r"^(?P<kind>\S(?:.*\S)?)\s+(?P<num>\d+|" + "|".join(_NUMS) + r")\s*(?P<unit>"
                     + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")$")


_CANON = re.compile(r"^(?P<kind>[^\[〈]+?)(?:\[(?P<unit>[^\]]+)\])?(?:〈(?P<cond>[^〉]+)〉)?$")


class ItemContractError(ValueError):
    """추출 아이템 연산이 정본과 맞지 않는다 — 부분 적용 없이 추출 재시도 대상."""


@dataclass(frozen=True)
class StockId:
    kind: str
    unit: str | None = None
    condition: str = COND_NORMAL

    @property
    def key(self) -> str:
        return format_key(self.kind, self.unit, self.condition)


def format_key(kind: str, unit=None, condition=COND_NORMAL) -> str:
    return kind + (f"[{unit}]" if unit else "") + (f"〈{condition}〉" if condition and condition != COND_NORMAL else "")


def _alias_kind(name: str, defs: dict | None) -> str:
    if not defs or name in defs:
        return name
    for k, d in defs.items():
        if name in (d.get("aliases") or ()):
            return k
    return name


def parse_key(key: str, defs: dict | None = None) -> tuple:
    """재고 키 → (StockId, 배수). 정규 키·수량이 분명한 옛 문구만 해석하고 나머지는 원문 그대로 종류로 둔다."""
    raw = str(key)
    m = _CANON.match(raw)
    if m and (m.group("unit") or m.group("cond")):
        cond = m.group("cond") or COND_NORMAL
        if cond in CONDITIONS or cond == COND_NORMAL:
            return StockId(_alias_kind(m.group("kind"), defs), m.group("unit"), cond), 1
    p = _PHRASE.match(raw)
    if p:
        num = p.group("num")
        n = int(num) if num.isdigit() else _NUMS[num]
        if n > 0:
            return StockId(_alias_kind(p.group("kind"), defs), p.group("unit")), n
    return StockId(_alias_kind(raw, defs)), 1


def canonical_bag(bag, defs=None) -> dict:
    """한 소유자 재고의 정규형 — 해석 가능한 키만 정규 키로, 같은 정규 키는 합친다. 정수가 아닌 값은 손대지 않는다."""
    out = {}
    for k, v in (bag or {}).items():
        if not isinstance(v, int) or isinstance(v, bool):
            out[k] = v
            continue
        sid, mult = parse_key(k, defs)
        ck = sid.key
        out[ck] = (out.get(ck, 0) if isinstance(out.get(ck, 0), int) else 0) + v * mult
    return {k: v for k, v in out.items() if not (isinstance(v, int) and v == 0)}


def usable(key: str, defs=None) -> bool:
    return parse_key(key, defs)[0].condition not in UNUSABLE


def grades_of(kind: str, defs=None) -> tuple:
    d = (defs or {}).get(kind) or {}
    return d.get("mass_grade"), d.get("bulk_grade")


def carry_load(bag, instances_owned, defs=None) -> dict:
    """저작 등급이 있는 물건의 무게·부피 부담(수량 누적). 미저작 물건은 0으로 확정하지 않고 '미산정'으로 센다."""
    load = {"mass": 0, "bulk": 0, "ungraded": 0}
    for k, v in (bag or {}).items():
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            continue
        mg, bg = grades_of(parse_key(k, defs)[0].kind, defs)
        if mg is None and bg is None:
            load["ungraded"] += v
        load["mass"] += GRADE_WEIGHTS.get(mg, 0) * v if isinstance(mg, int) else 0
        load["bulk"] += GRADE_WEIGHTS.get(bg, 0) * v if isinstance(bg, int) else 0
    for inst in instances_owned or ():
        mg, bg = grades_of(inst.get("kind"), defs)
        load["mass"] += GRADE_WEIGHTS.get(mg, 0) if isinstance(mg, int) else 0
        load["bulk"] += GRADE_WEIGHTS.get(bg, 0) if isinstance(bg, int) else 0
    return load


def sealed(places: dict, rec: dict) -> bool:
    """v7.29: 닫힌 용기 안의 물건인가 — 열기(OPEN) 전에는 꺼내거나 쓸 수 없다."""
    box = places.get(rec.get("inside")) if rec.get("inside") else None
    return bool(box and box.get("closed") and int(box.get("quantity") or 0) > 0)


def _contents_of(places: dict, sid: str) -> list:
    return [k for k, r in sorted(places.items()) if r.get("inside") == sid and int(r.get("quantity") or 0) > 0]


class _Sim:
    def __init__(self, session, defs, pc_name, scene_place, tx):
        self.defs = defs or {}
        self.pc = pc_name
        self.scene = scene_place
        self.tx = tx
        self.res = copy.deepcopy(getattr(session, "resources", None) or {})
        self.inst = copy.deepcopy(getattr(session, "item_instances", None) or {})
        self.places = copy.deepcopy(getattr(session, "place_items", None) or {})
        self.equip = copy.deepcopy(getattr(session, "equipment", None) or {})
        self.traits = copy.deepcopy(getattr(session, "item_traits", None) or {})
        self.touched = set()
        self.gained = set()
        self.report = []
        self.used, self.paid, self.reads, self.takes = [], [], [], []
        self.care = []          # v7.47 [{kind, effects, op, on, failed}] — 쓴 물건의 사용 효과(물건 상태로 꺼진 것 제외)
        self.n = 0

    def record_care(self, op, kind, cond):
        uses = use_of(kind, self.defs, self.traits)
        off = blocked_effects(cond, key_tags(format_key(kind), self.defs, self.traits))
        live = [u for u in uses if u not in off]
        if live:
            self.care.append({"kind": kind, "effects": live, "op": op["op"], "on": op.get("on") or op.get("from"),
                              "by": op.get("from"), "failed": bool(op.get("failed"))})

    # ── 재고 ──
    def bag(self, owner):
        if owner not in self.touched:
            self.res[owner] = canonical_bag(self.res.get(owner) or {}, self.defs)
            self.touched.add(owner)
        return self.res.setdefault(owner, {})

    def take(self, owner, key, qty, why):
        b = self.bag(owner)
        have = b.get(key, 0)
        if not isinstance(have, int) or have < qty:
            raise ItemContractError(f"{why}: {owner}의 {key} 보유 {have} < {qty}")
        if have == qty:
            b.pop(key)
        else:
            b[key] = have - qty
        self.report.append({"target": owner, "item": key, "delta": -qty, "after": have - qty})

    def give(self, owner, key, qty, why):
        if qty <= 0:
            raise ItemContractError(f"{why}: 수량 {qty}")
        kind = parse_key(key, self.defs)[0].kind
        mg, bg = grades_of(kind, self.defs)
        if GRADE_UNCARRIABLE in (mg, bg):
            raise ItemContractError(f"{why}: {kind}은(는) 들고 다닐 수 없는 물건(운반 불가 등급)")
        if mg is None and bg is None and qty > UNGRADED_OP_CAP:
            raise ItemContractError(f"{why}: 운반 등급 미저작 {kind} {qty}개 — 한 번에 {UNGRADED_OP_CAP}개 초과 획득 불가")
        b = self.bag(owner)
        b[key] = (b.get(key, 0) if isinstance(b.get(key, 0), int) else 0) + qty
        self.gained.add(owner)
        self.report.append({"target": owner, "item": key, "delta": qty, "after": b[key]})

    def stock_key(self, name, unit=None):
        """새로 생기는 일반 물건(제작·분해 결과) — 종류 별칭만 해석, 상태는 정상."""
        kind = _alias_kind(str(name).strip(), self.defs)
        if not kind:
            raise ItemContractError("결과물 이름 없음")
        return format_key(kind, (unit or None) if unit not in (None, "", "NONE") else None)

    # ── 특수 객체 ──
    def inst_of(self, iid):
        r = self.inst.get(iid)
        if r is None:
            raise ItemContractError(f"특수 물건 {iid} 없음")
        return r

    def check_carry(self):
        lim = dict(DEFAULT_CARRY_LIMITS, **(self.defs.get("__carry_limits__") or {}))
        for owner in sorted(self.gained):
            owned = [r for r in self.inst.values() if r.get("owner") == owner]
            load = carry_load(self.res.get(owner) or {}, owned, self.defs)
            for axis in ("mass", "bulk"):
                if load[axis] > lim[axis]:
                    raise ItemContractError(f"{owner}의 운반 부담 {axis} {load[axis]} > 한도 {lim[axis]}(명백한 과적)")


def _reconcile_equipment(s) -> None:
    """착용 기록을 재고에 맞춘다 — 상태가 바뀐 같은 종류는 따라가고(손상 등), 더는 없는 물건은 벗겨진다."""
    for owner in list(s.equip):
        slots = s.equip.get(owner) or {}
        bag = s.bag(owner) if owner in s.res or owner in s.touched else {}
        used = {}
        for slot in list(slots):
            e = slots[slot]
            k = e.get("key") if isinstance(e, dict) else None
            if k is not None and isinstance(bag.get(k), int) and bag[k] - used.get(k, 0) > 0:
                used[k] = used.get(k, 0) + 1
                continue
            kind = parse_key(k or "", s.defs)[0].kind
            alt = next((x for x in sorted(bag) if isinstance(bag[x], int) and bag[x] - used.get(x, 0) > 0
                        and parse_key(x, s.defs)[0].kind == kind), None)
            if alt is None:
                slots.pop(slot)
            else:
                e["key"] = alt
                used[alt] = used.get(alt, 0) + 1
        if not slots:
            s.equip.pop(owner, None)


def _qty(v, why) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ItemContractError(f"{why}: 수량은 양의 정수({v!r})")
    return v


def run_ops(session, ops, *, defs=None, pc_name, scene_place, transaction_id, recipes=None) -> _Sim:
    """정규 연산을 기준 재고에 순서대로 가상 적용한다(세션 불변). 실패하면 ItemContractError."""
    s = _Sim(session, defs, pc_name, scene_place, transaction_id)
    recipes = recipes or {}
    for n, op in enumerate(ops):
        kind = op["op"]
        why = f"연산 {n + 1}({kind})"
        if kind in ("TRANSFER", "PAY"):
            q = _qty(op["qty"], why)
            if op.get("instance"):
                r = s.inst_of(op["instance"])
                if r.get("owner") != op["from"] or q != 1:
                    raise ItemContractError(f"{why}: {op['from']}이(가) 그 특수 물건을 갖고 있지 않음")
                if kind == "PAY" and not op.get("to"):
                    raise ItemContractError(f"{why}: 특수 물건은 지급 소모로 없앨 수 없음")
                r["previous_owners"] = list(r.get("previous_owners") or []) + [op["from"]]
                r["owner"], r["place"] = op["to"], None
                s.gained.add(op["to"])
                paid_kind = r.get("kind")
            else:
                s.take(op["from"], op["stock"], q, why)
                if op.get("to"):
                    s.give(op["to"], op["stock"], q, why)
                paid_kind = op.get("kind") or parse_key(op["stock"], defs)[0].kind
            # 지급 사실(R4) = 실제로 PC 손을 떠나 다른 소유자에게 넘어간(또는 접근 지급으로 치른) 것만
            if kind == "PAY" or (op["from"] == pc_name and op.get("to") and op["to"] != pc_name):
                s.paid.append(paid_kind)
        elif kind == "ACQUIRE":
            q = _qty(op["qty"], why)
            src = op.get("source")
            if src and src.get("discovery"):
                if q != 1 or src["discovery"] in s.takes:
                    raise ItemContractError(f"{why}: 발견물은 한 번에 한 개만, 한 번만 수거")
                s.takes.append(src["discovery"])
                s.give(op["to"], format_key(_alias_kind(src["name"], defs)), 1, why)
            elif src and src.get("new"):
                s.give(op["to"], format_key(_alias_kind(src["new"], defs)), q, why)
            elif src and src.get("instance"):
                r = s.inst_of(src["instance"])
                if r.get("owner") or r.get("place") != scene_place or q != 1:
                    raise ItemContractError(f"{why}: 그 특수 물건이 이 자리에 없음")
                r["owner"], r["place"] = op["to"], None
                s.gained.add(op["to"])
            elif src and src.get("place_source"):
                rec = s.places.get(src["place_source"])
                if rec is None or rec.get("place") != scene_place or int(rec.get("quantity") or 0) < q:
                    raise ItemContractError(f"{why}: 이 자리에 남은 수량 부족")
                if sealed(s.places, rec):
                    raise ItemContractError(f"{why}: {rec['key']}은(는) 닫힌 "
                                            f"{s.places[rec['inside']]['key']} 안에 있어 열기(OPEN) 전에는 꺼낼 수 없음")
                inner = _contents_of(s.places, src["place_source"])
                rec["quantity"] = int(rec["quantity"]) - q
                if rec["quantity"] == 0:
                    s.places.pop(src["place_source"])
                s.give(op["to"], rec["key"], q, why)
                for k in inner:                            # v7.29: 용기째 들면 안의 물건도 함께 든다
                    r2 = s.places.pop(k)
                    s.give(op["to"], r2["key"], int(r2["quantity"]), why)
            else:
                raise ItemContractError(f"{why}: 원천 없는 획득")
        elif kind == "DROP":
            q = _qty(op["qty"], why)
            if not scene_place:
                raise ItemContractError(f"{why}: 장소 밖(이동 중)에서는 내려놓을 수 없음")
            if op.get("instance"):
                r = s.inst_of(op["instance"])
                if r.get("owner") != op["from"]:
                    raise ItemContractError(f"{why}: 특수 물건 미보유")
                r["owner"], r["place"] = None, scene_place
            else:
                s.take(op["from"], op["stock"], q, why)
                sid = "drop." + hashlib.sha256(f"item.drop.v1\x00{transaction_id}\x00{n}".encode("utf-8")).hexdigest()[:12]
                s.places[sid] = {"place": scene_place, "key": op["stock"], "quantity": q, "origin": "DROP",
                                 "by": op["from"], "tx": transaction_id}
        elif kind == "CONSUME":
            q = _qty(op["qty"], why)
            if op.get("instance"):
                r = s.inst_of(op["instance"])
                if r.get("owner") != op["from"]:
                    raise ItemContractError(f"{why}: 특수 물건 미보유")
                s.record_care(op, r.get("kind"), r.get("condition") or COND_NORMAL)
                r["owner"], r["place"], r["condition"] = None, None, "소실"
            else:
                s.take(op["from"], op["stock"], q, why)
                _sid = parse_key(op["stock"], defs)[0]
                s.record_care(op, _sid.kind, _sid.condition)
        elif kind in ("DAMAGE", "REPAIR"):
            q = _qty(op["qty"], why)
            new = op.get("condition")
            if kind == "REPAIR":
                new = COND_NORMAL
            if new not in CONDITIONS + (COND_NORMAL,):
                raise ItemContractError(f"{why}: 상태 {new!r}")
            if op.get("instance"):
                r = s.inst_of(op["instance"])
                if r.get("owner") != op["from"]:
                    raise ItemContractError(f"{why}: 특수 물건 미보유")
                if (r.get("condition") or COND_NORMAL) == new:
                    raise ItemContractError(f"{why}: 이미 {new}")
                r["condition"] = new
            else:
                sid = parse_key(op["stock"], defs)[0]
                if sid.condition == new or (kind == "REPAIR" and sid.condition == COND_NORMAL):
                    raise ItemContractError(f"{why}: 상태 변화 없음")
                s.take(op["from"], op["stock"], q, why)
                s.give(op["from"], format_key(sid.kind, sid.unit, new), q, why)
        elif kind == "OPEN" and op.get("place_source"):
            rec = s.places.get(op["place_source"])
            if rec is None or rec.get("place") != scene_place or int(rec.get("quantity") or 0) <= 0:
                raise ItemContractError(f"{why}: 그 용기가 이 자리에 없음")
            if not rec.get("closed"):
                raise ItemContractError(f"{why}: {rec['key']}은(는) 닫힌 용기가 아님(이미 열렸거나 열 것이 없음)")
            rec["closed"] = False                          # 여는 것만 — 안의 물건은 그 자리에 남는다
        elif kind == "OPEN":
            q = _qty(op["qty"], why)
            sid = parse_key(op["stock"], defs)[0]
            con = (((s.defs.get(sid.kind) or {}).get("units") or {}).get(sid.unit or "") or {}).get("contains")
            if not con:
                raise ItemContractError(f"{why}: {op['stock']}의 내용물 수량이 저작되지 않아 낱개로 나눌 수 없음(R1)")
            s.take(op["from"], op["stock"], q, why)
            s.give(op["from"], format_key(sid.kind, con["unit"], sid.condition), q * int(con["count"]), why)
        elif kind == "CRAFT":
            q = _qty(op.get("qty") or 1, why)
            rid = op.get("recipe")
            if rid:
                rec = recipes.get(rid)
                if rec is None:
                    raise ItemContractError(f"{why}: 제작법 {rid} 없음")
                for t in rec.get("tools") or ():
                    if not any(parse_key(k, defs)[0].kind == t and usable(k, defs) and v > 0
                               for k, v in s.bag(op["from"]).items() if isinstance(v, int)):
                        raise ItemContractError(f"{why}: 도구 {t} 없음 또는 사용 불가")
                for i in rec.get("inputs") or ():
                    s.take(op["from"], _pick_input(s, op["from"], i, defs, why), int(i.get("count", 1)) * q, why)
                for o in rec.get("outputs") or ():
                    s.give(op["from"], format_key(o["kind"], o.get("unit")), int(o.get("count", 1)) * q, why)
            else:
                ins, outs = op.get("inputs") or [], op.get("outputs") or []
                if not ins or not outs:
                    raise ItemContractError(f"{why}: 자유 제작에는 소모 재료와 결과가 모두 필요")
                if len(outs) > FREE_CRAFT_MAX_KINDS:
                    raise ItemContractError(f"{why}: 자유 제작 결과 종류 상한 {FREE_CRAFT_MAX_KINDS}")
                in_kinds = set()
                for i in ins:
                    s.take(op["from"], i["stock"], _qty(i["qty"], why), why)
                    in_kinds.add(parse_key(i["stock"], defs)[0].kind)
                for o in outs:
                    key = s.stock_key(o["name"], o.get("unit"))
                    k = parse_key(key, defs)[0].kind
                    d = s.defs.get(k) or {}
                    if d.get("special") or d.get("unique") or d.get("recipe_only"):
                        raise ItemContractError(f"{why}: {k}은(는) 저작 제작법·정본 효과가 필요한 물건 — 자유 제작 불가")
                    if k in in_kinds:
                        raise ItemContractError(f"{why}: 재료와 같은 물건을 만들어 복제할 수 없음")
                    oq = _qty(o["qty"], why)
                    if oq > FREE_CRAFT_MAX_QTY:
                        raise ItemContractError(f"{why}: 자유 제작 결과 수량 상한 {FREE_CRAFT_MAX_QTY}")
                    s.give(op["from"], key, oq, why)
        elif kind == "DISASSEMBLE":
            q = _qty(op["qty"], why)
            sid = parse_key(op["stock"], defs)[0]
            s.take(op["from"], op["stock"], q, why)
            parts = (s.defs.get(sid.kind) or {}).get("parts")
            if parts:
                want = {(p["kind"], p.get("unit")): int(p.get("count", 1)) * q for p in parts}
                got = {}
                for o in op.get("outputs") or ():
                    key = s.stock_key(o["name"], o.get("unit"))
                    ps = parse_key(key, defs)[0]
                    got[(ps.kind, ps.unit)] = got.get((ps.kind, ps.unit), 0) + _qty(o["qty"], why)
                if any(got.get(k, 0) > v for k, v in want.items()) or set(got) - set(want):
                    raise ItemContractError(f"{why}: 저작 부품 구성과 다른 회수")
                for (k, u), v in got.items():
                    s.give(op["from"], format_key(k, u), v, why)
            else:
                for o in op.get("outputs") or ():
                    key = s.stock_key(o["name"], o.get("unit"))
                    if parse_key(key, defs)[0].kind == sid.kind:
                        raise ItemContractError(f"{why}: 분해로 원래 물건을 복제할 수 없음")
                    oq = _qty(o["qty"], why)
                    if oq > q:
                        raise ItemContractError(f"{why}: 부품 저작 없는 분해는 원재료 수량을 넘겨 회수할 수 없음")
                    s.give(op["from"], key, oq, why)
        elif kind == "IDENTIFY":                       # v7.37(D6): 정체 확인 — 같은 물건의 이름만 바뀐다
            if op.get("instance"):
                r = s.inst_of(op["instance"])
                if r.get("owner") != op["from"]:
                    raise ItemContractError(f"{why}: 특수 물건 미보유")
                r["name"] = op["name"]
            else:
                q = _qty(op["qty"], why)
                sid = parse_key(op["stock"], defs)[0]
                new = _alias_kind(op["name"], defs)
                if new == sid.kind:
                    raise ItemContractError(f"{why}: 이름 변화 없음")
                s.take(op["from"], op["stock"], q, why)
                s.give(op["from"], format_key(new, sid.unit, sid.condition), q, why)
        elif kind == "TRAIT":                          # v7.47(3.1) 처음 다룬 물건의 특성 — 이미 정해졌으면 그대로 둔다
            k_ = op.get("kind")
            if k_ and traits_of(k_, defs, s.traits)[1] is None:
                voc = effect_vocab()
                slots = [x for x in dict.fromkeys(op.get("slots") or ()) if x in voc["slots"]]
                tags = free_tags([t for t in op.get("tags") or () if any(t in tags_at([t], sl) for sl in slots)])
                s.traits[k_] = {"slots": slots, "tags": tags, "use": free_use(op.get("use")), "source": "EXTRACTION"}
        elif kind == "WEAR":                           # v7.46 입기·손에 들기 — 소지한 물건을 그 자리에
            voc = effect_vocab()
            owner, slot = op["from"], op.get("slot")
            if owner != pc_name:
                raise ItemContractError(f"{why}: 착용은 PC만")
            key = op.get("stock")
            if op.get("new_name"):
                key = format_key(_alias_kind(op["new_name"], defs))
            if not key or s.bag(owner).get(key, 0) <= 0:
                raise ItemContractError(f"{why}: {owner}이(가) 그 물건을 갖고 있지 않음")
            if slot not in voc["slots"]:
                raise ItemContractError(f"{why}: 알 수 없는 자리 {slot!r}")
            kind_ = parse_key(key, defs)[0].kind
            t, src = traits_of(kind_, defs, s.traits)
            if src == "AUTHORED" and slot not in t["slots"]:
                raise ItemContractError(f"{why}: {key}은(는) {slot}에 착용할 수 없음({', '.join(t['slots']) or '입는 물건 아님'})")
            if not usable(key, defs):
                raise ItemContractError(f"{why}: {key}은(는) 고장·파손 상태")
            mine = s.equip.setdefault(owner, {})
            others = [sl for sl, e in mine.items() if sl != slot and e.get("key") == key]
            if s.bag(owner)[key] - len(others) <= 0:      # 모두 다른 자리에 있으면 하나를 옮긴다(손에 든 옷을 입음 등)
                mine.pop(others[0])
            mine[slot] = {"key": key}
            if src is None and op.get("tags"):   # 옛 형식(v7.46 WEAR tags) — 아직 정해지지 않은 물건만
                s.traits[kind_] = {"slots": [slot], "tags": free_tags(op.get("tags"), slot), "use": [],
                                   "source": "EXTRACTION"}
        elif kind == "REMOVE":
            mine = s.equip.get(op["from"]) or {}
            hit = [sl for sl, e in mine.items() if e.get("key") == op.get("stock")
                   and (not op.get("slot") or sl == op["slot"])]
            if not hit:
                raise ItemContractError(f"{why}: {op.get('stock')}을(를) 착용하고 있지 않음")
            for sl in hit:
                mine.pop(sl)
            if not mine:
                s.equip.pop(op["from"], None)
        elif kind in ("READ", "USE"):
            k_, cond, refs = _subject_of(s, op, why)
            if kind == "USE":
                if cond in UNUSABLE:
                    raise ItemContractError(f"{why}: {k_}은(는) {cond} 상태라 사용할 수 없음")
                s.used.append(k_)
                s.record_care(op, k_, cond)
            else:
                if op.get("from") != pc_name:
                    raise ItemContractError(f"{why}: PC만 읽기 인지를 기록")
                for iid in refs:
                    if iid not in s.reads:
                        s.reads.append(iid)
        else:
            raise ItemContractError(f"{why}: 알 수 없는 연산")
    _reconcile_equipment(s)
    s.check_carry()
    return s


def _pick_input(s, owner, spec, defs, why) -> str:
    """제작법 재료 → 소유자 재고 키(같은 종류·단위·사용 가능 상태, 결정적 첫 일치)."""
    for k in sorted(s.bag(owner)):
        sid = parse_key(k, defs)[0]
        if sid.kind == spec["kind"] and (spec.get("unit") or None) == sid.unit and sid.condition not in UNUSABLE:
            return k
    raise ItemContractError(f"{why}: 재료 {spec['kind']} 없음")


def _subject_of(s, op, why) -> tuple:
    """READ/USE 대상 — (종류, 상태, 정보 참조). 소지 또는 이 자리의 물건만."""
    defs = s.defs
    if op.get("instance"):
        r = s.inst_of(op["instance"])
        if r.get("owner") != op.get("from") and not (not r.get("owner") and r.get("place") == s.scene):
            raise ItemContractError(f"{why}: 손이 닿는 특수 물건이 아님")
        k = r.get("kind")
        refs = list(r.get("info_refs") or []) or list((defs.get(k) or {}).get("info_refs") or [])
        return k, r.get("condition") or COND_NORMAL, refs
    key = op.get("stock")
    if op.get("place_source"):
        rec = s.places.get(op["place_source"])
        if rec is None or rec.get("place") != s.scene:
            raise ItemContractError(f"{why}: 이 자리에 없는 물건")
        if sealed(s.places, rec):
            raise ItemContractError(f"{why}: 닫힌 용기 안의 물건 — 열기(OPEN) 전에는 쓸 수 없음")
        key = rec["key"]
    elif key is None or s.bag(op["from"]).get(key, 0) <= 0:
        raise ItemContractError(f"{why}: {op.get('from')}이(가) 그 물건을 갖고 있지 않음")
    sid = parse_key(key, defs)[0]
    return sid.kind, sid.condition, list((defs.get(sid.kind) or {}).get("info_refs") or [])


def definitions_of(definition: dict | None) -> dict:
    """컴파일된 정의의 아이템 정의(+ 운반 한도) — 없으면 빈 dict(장르 중립 기본)."""
    d = copy.deepcopy((definition or {}).get("item_definitions") or {})
    lim = (definition or {}).get("carry_limits")
    if lim:
        d["__carry_limits__"] = dict(lim)
    return d


def apply_event_grants(session, grants, *, definition=None, pc_name=None) -> list:
    """v7.53 커밋 — 사건 GRANT_ITEM 지급을 소지품·현장 물건에 더한다. 반환: 보고 행 [{target, item, delta}].

    · actor = ActorId(저작 인물 → 레거시 이름 소지) 또는 $session_pc(PC 이름). place = 현장 물건 원천
      `ev.<digest>`(origin EVENT — 이후 추출 물건 연산 L키로 집는다). 같은 기록은 한 번만(원천 id = 기록의 box)."""
    defs = definitions_of(definition)
    acts = (definition or {}).get("actor_definitions") or {}
    res = dict(getattr(session, "resources", None) or {})
    places = copy.deepcopy(getattr(session, "place_items", None) or {})
    rows = []
    for g in grants or ():
        sid, mult = parse_key(g["key"], defs)
        qty = int(g["quantity"]) * mult
        if g.get("place"):
            box = g["box"]
            if box in places:
                continue
            places[box] = {"place": g["place"], "key": sid.key, "quantity": qty, "origin": "EVENT", "by": None,
                           "tx": None}
            rows.append({"target": f"@{g['place']}", "item": sid.key, "delta": qty})
            continue
        owner = pc_name if g.get("actor") == "$session_pc" else (acts.get(g.get("actor")) or {}).get("legacy_key")
        if not owner:
            continue
        bag = dict(res.get(owner) or {}) if isinstance(res.get(owner), dict) else {}
        bag[sid.key] = (bag.get(sid.key, 0) if isinstance(bag.get(sid.key, 0), int) else 0) + qty
        res[owner] = bag
        rows.append({"target": owner, "item": sid.key, "delta": qty})
    session.resources = res
    session.place_items = places
    return rows
