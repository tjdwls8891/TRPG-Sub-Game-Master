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
#   TRANSFER · ACQUIRE · DROP · CONSUME · DAMAGE · REPAIR · OPEN · CRAFT · DISASSEMBLE · READ · USE · PAY
#   수량은 양의 정수, 부족하면 0으로 자르지 않고 실패(D-5). 같은 내용의 연산이 두 번 오면 두 번 적용(D-2 — 반복 보존).
#   재처리 중복은 커밋 경로의 트랜잭션 멱등(_extraction_applied_tx)이 막는다.
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

import hashlib

# ── 영속 필드(되감기 대상 게임 정본 — resources와 같은 커밋·이력 단위) ──
ITEM_FIELDS = {
    "item_instances": {},   # {instance_id: {kind, name, owner, place, condition, info_refs, origin, previous_owners}}
    "place_items": {},      # {source_id: {place, key, quantity, origin, by, tx, closed?, inside?}}
                            #   v7.29 용기: closed=True(닫힌 용기) · inside=용기 source_id(그 안의 물건)
    "item_facts": {},       # 직전 커밋 턴의 사용·지급 사실 {tx, used:[종류], paid:[종류]} — 다음 턴 사건 조건 입력
}


def item_field_defaults() -> dict:
    return copy.deepcopy(ITEM_FIELDS)


COND_NORMAL = "정상"
CONDITIONS = ("손상", "고장", "파손")            # 사건형 손상 상태(수치 내구도 없음 — Q5)
UNUSABLE = frozenset({"고장", "파손"})            # 사용 불가 상태
GRADE_WEIGHTS = {0: 0, 1: 1, 2: 4, 3: 12, 4: 30}  # 무게·부피 등급 → 부담 단위(5 = 운반 불가)
GRADE_UNCARRIABLE = 5
DEFAULT_CARRY_LIMITS = {"mass": 40, "bulk": 40}    # 저작 등급이 있는 물건에만 적용(R3 완화형)
UNGRADED_OP_CAP = 99                               # 등급 미저작 물건 한 연산 획득 상한(수백 개 조용한 획득 방지)
FREE_CRAFT_MAX_KINDS = 3
FREE_CRAFT_MAX_QTY = 10
# v7.35(D1): 이번 묘사에 처음 나온 물건을 PC가 챙김(item=NEW + name) — 이름이 본문에 그대로 있어야 한다
NEW_KEY = "NEW"
NEW_MAX_KINDS = 3
NEW_MAX_QTY = 10

OPS = ("TRANSFER", "ACQUIRE", "DROP", "CONSUME", "DAMAGE", "REPAIR", "OPEN", "CRAFT", "DISASSEMBLE",
       "READ", "USE", "IDENTIFY")
OPS_FIELD = "item_ops"

# 한국어 수 관형사·수량 단위(언어 수준 — 시나리오·장르 어휘가 아니다)
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
                    "inputs": {"type": "array", "items": ref},
                    "outputs": {"type": "array", "items": out}},
                    "required": ["op", "owner", "item", "quantity"]}}

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
        lines.append("· TRANSFER owner→to 건넴 · ACQUIRE 현장 물건(L·D키)을 owner가 집음 · DROP owner가 내려놓음 · "
                     "CONSUME 써 버림·잃음·파괴 · DAMAGE/REPAIR 상태 변화(condition) · OPEN 포장 개봉 또는 닫힌 용기(L키) 열기 — "
                     "여는 것만이고 안의 물건을 집으면 따로 ACQUIRE · "
                     "CRAFT 완성된 제작(inputs 재료 소모, outputs 결과 — recipe가 있으면 그 키) · DISASSEMBLE 분해(outputs 회수 부품) · "
                     "READ 글·기록을 실제로 읽음 · USE 소모 없이 사용.")
        lines.append(f"· ACQUIRE item={NEW_KEY}: 위 키에 없는 물건이 이번 묘사에 처음 나와 PC가 실제로 집어 챙긴 경우만 — "
                     "owner는 PC, name에 본문에 쓰인 물건 이름을 그대로(수량은 quantity). 키가 있는 물건은 그 키를 쓴다.")
        lines.append("· IDENTIFY: 이미 가진 물건(H키)의 정체가 이번 묘사에서 밝혀진 경우만 — name에 본문에 쓰인 새 이름"
                     "(예: 정체 모를 금속 물체 → 손전등). 물건이 바뀌거나 생기는 것이 아니다.")
        lines.append("· 보여 주기·만지기·시도만 한 것, 실패한 제작의 결과물, 묘사에 없는 이동은 넣지 말 것. 수량은 묘사에 "
                     "드러난 정수(불명이면 1). 소지에 없는 물건을 주거나 쓰는 묘사라도 새 키를 지어내지 말 것.")
        return "\n".join(lines) + "\n"


def _owner_label(name, pc_name):
    return f"{name}(PC)" if name == pc_name else name


def sealed(places: dict, rec: dict) -> bool:
    """v7.29: 닫힌 용기 안의 물건인가 — 열기(OPEN) 전에는 꺼내거나 쓸 수 없다."""
    box = places.get(rec.get("inside")) if rec.get("inside") else None
    return bool(box and box.get("closed") and int(box.get("quantity") or 0) > 0)


def _contents_of(places: dict, sid: str) -> list:
    return [k for k, r in sorted(places.items()) if r.get("inside") == sid and int(r.get("quantity") or 0) > 0]


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
    return ItemManifest(owners, holdings, places, disc, rc, scene_place, tuple(payments))


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


class _Sim:
    def __init__(self, session, defs, pc_name, scene_place, tx):
        self.defs = defs or {}
        self.pc = pc_name
        self.scene = scene_place
        self.tx = tx
        self.res = copy.deepcopy(getattr(session, "resources", None) or {})
        self.inst = copy.deepcopy(getattr(session, "item_instances", None) or {})
        self.places = copy.deepcopy(getattr(session, "place_items", None) or {})
        self.touched = set()
        self.gained = set()
        self.report = []
        self.used, self.paid, self.reads, self.takes = [], [], [], []
        self.n = 0

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
                r["owner"], r["place"], r["condition"] = None, None, "소실"
            else:
                s.take(op["from"], op["stock"], q, why)
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
        elif kind in ("READ", "USE"):
            k_, cond, refs = _subject_of(s, op, why)
            if kind == "USE":
                if cond in UNUSABLE:
                    raise ItemContractError(f"{why}: {k_}은(는) {cond} 상태라 사용할 수 없음")
                s.used.append(k_)
            else:
                if op.get("from") != pc_name:
                    raise ItemContractError(f"{why}: PC만 읽기 인지를 기록")
                for iid in refs:
                    if iid not in s.reads:
                        s.reads.append(iid)
        else:
            raise ItemContractError(f"{why}: 알 수 없는 연산")
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


def normalize_raw_ops(manifest: ItemManifest, raw_ops, *, discovery_taken=(), pc_name=None,
                      narration_text: str = "") -> list:
    """추출 item_ops(+ 발견 D키 수거) → 정규 연산. 후보 밖 키·형식 오류는 ItemContractError(재추출)."""
    out, new_names = [], set()
    for n, r in enumerate(raw_ops or ()):
        if not isinstance(r, dict):
            raise ItemContractError(f"연산 {n + 1}: 형식 오류")
        op = r.get("op")
        if op not in OPS:
            raise ItemContractError(f"연산 {n + 1}: 알 수 없는 op {op!r}")
        why = f"연산 {n + 1}({op})"
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
        if op in ("READ", "USE") and hold is None:
            pl = manifest.place_of(item)
            if pl is None:
                raise ItemContractError(f"{why}: 대상 키 {item!r} 후보 밖")
            out.append({**base, "from": owner, "place_source": pl[1], "instance": pl[2], "qty": q or 1})
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
        rec = {**base, "from": owner, "stock": hold[2], "instance": hold[3]}
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
        elif op == "DISASSEMBLE":
            rec["outputs"] = [{"name": o.get("name"), "unit": o.get("unit"), "qty": o.get("quantity")}
                              for o in r.get("outputs") or () if isinstance(o, dict)]
        out.append(rec)
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
               discovery_taken=(), legacy_item_deltas=(), payments=(), narration_text: str = "") -> ItemPlan:
    """추출 → 검증된 ItemPlan. 실패하면 ItemContractError(정본 불변, 추출 재시도)."""
    ops, diags = list(payments or ()), []
    if raw_ops:
        ops += normalize_raw_ops(manifest, raw_ops, discovery_taken=discovery_taken, pc_name=pc_name,
                                 narration_text=narration_text)
    else:
        ops += normalize_raw_ops(manifest, (), discovery_taken=discovery_taken, pc_name=pc_name)
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
    sim = run_ops(session, plan.ops, defs=plan.defs, pc_name=plan.pc_name, scene_place=plan.scene_place,
                  transaction_id=plan.transaction_id, recipes=plan.recipes)
    res = dict(getattr(session, "resources", None) or {})
    for owner in sim.touched:
        res[owner] = sim.res.get(owner) or {}
    session.resources = res
    session.item_instances = sim.inst
    session.place_items = sim.places
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


def definitions_of(definition: dict | None) -> dict:
    """컴파일된 정의의 아이템 정의(+ 운반 한도) — 없으면 빈 dict(장르 중립 기본)."""
    d = copy.deepcopy((definition or {}).get("item_definitions") or {})
    lim = (definition or {}).get("carry_limits")
    if lim:
        d["__carry_limits__"] = dict(lim)
    return d


def item_block(session, *, pc_name, scene_owner_names=(), scene_place=None, defs=None) -> str:
    """지시층위 — 정본 재고(PC·장면 인물)·현장 물건·사용 가능 여부(코드 확정). 캐시 초기 소지품보다 우선."""
    res = getattr(session, "resources", None) or {}
    inst = getattr(session, "item_instances", None) or {}
    lines = ["\n[물건 — 정본 소지(코드 확정 · 룰북의 초기 소지품보다 우선). 여기 없는 물건을 가진 것처럼 쓰지 말 것. "
             "〈고장〉〈파손〉 물건은 제 기능으로 쓸 수 없다]"]
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
