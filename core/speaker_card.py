# 화자 카드 — 시나리오별 SVG 캐릭터 카드 (DYNAMIC 전용)
#
# [권위 — SVG 캐릭터 카드 설계안 v1.2 · 구현 지시 v1.0]
#   · 시나리오 최상위 `speaker_card`가 정의한다. 없거나 mode != "svg"면 기존 화자 PNG(legacy_png) 그대로.
#   · 신규 경로는 동적 세계에 바인딩된 단일 PC 세션(생애주기 INITIALIZED·OPEN)에서만 쓴다. LEGACY는 미지원.
#   · 공개 판정 = 커밋된 PC 인지 정본(cognition_state, CONTENT)만. 이번 턴 staged 획득은 보지 않는다.
#   · 규칙: rules[].when.all_info(AND) → set(여러 슬롯). 한 슬롯은 최대 한 규칙만 제어(중복 = 저작 오류).
#   · 비공개 값은 렌더 입력 단계에서 차단한다 — 템플릿에는 안전한 슬롯값만 들어간다.
#   · 템플릿·이미지 자산은 media/{scenario_id}/ 안의 상대 경로 + 저작 SHA-256과 일치할 때만 쓴다.
#   · 실패(정의·자산·렌더·전송)는 카드 생략으로 끝난다. 기존 실명 PNG로 대체하지 않는다.
#   · 저작 검사는 check_card_definition(독립 검사, tools/scenario_check.py) — 컴파일러 ERROR가 아니다.
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import time
import itertools
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

CONFIG_KEY = "speaker_card"
MODE_SVG = "svg"
MODE_LEGACY = "legacy_png"
SCHEMA_VERSION = 1
UNKNOWN_TEXT = "???"
SLOT_KINDS = ("text", "image", "color")
BIND_OPS = ("text", "chars", "attr", "image", "recolor")
# attr 연산이 함께 고정할 수 있는 정적 속성(색 슬롯이 보이도록 투명도를 여는 용도 등)
STATIC_ATTRS = ("opacity", "fill-opacity", "stroke-opacity", "display")
# 색을 받는 속성
COLOR_ATTRS = ("fill", "stroke", "stop-color", "flood-color", "color")
RENDER_TIMEOUT_SEC = 30

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)
_HREF_KEYS = (f"{{{XLINK_NS}}}href", "href")
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpeg);base64,", re.I)
_BAD_TAGS = ("script", "foreignObject", "iframe", "object", "embed", "audio", "video")
_STRIP_TAGS = ("metadata", "title", "desc")


class CardError(Exception):
    """카드를 만들 수 없다 — 호출자는 이미지를 생략한다(대사 유지)."""


class CardAssetError(CardError):
    pass


class RendererUnavailable(CardError):
    pass


# ══════════════════════════════════════════════════════════════
#  설정 · 적용 대상 세션
# ══════════════════════════════════════════════════════════════

def config_of(scenario_data) -> dict | None:
    cfg = (scenario_data or {}).get(CONFIG_KEY) if isinstance(scenario_data, dict) else None
    return cfg if isinstance(cfg, dict) else None


def mode_of(scenario_data) -> str:
    cfg = config_of(scenario_data)
    return MODE_SVG if cfg is not None and cfg.get("mode") == MODE_SVG else MODE_LEGACY


@dataclass(frozen=True)
class CardWorld:
    config: dict
    definition: dict
    pc_actor: str
    scenario_id: str


def card_world(session) -> CardWorld | None:
    """SVG 카드 경로를 쓰는 세션이면 CardWorld, 아니면 None(기존 PNG 경로).

    동적 세계 바인딩(genesis) + 생애주기 INITIALIZED/OPEN + 단일 PC + 바인딩 정의 읽기 가능 + mode svg.
    인트로(INITIALIZED)도 포함한다 — genesis 커밋 인지를 쓴다.
    """
    from . import player_world as _pw
    from . import world_lifecycle as _lc
    scenario_data = getattr(session, "scenario_data", None)
    if mode_of(scenario_data) != MODE_SVG:
        return None
    if not _lc.world_bound(session):
        return None
    if getattr(session, "world_lifecycle", None) not in (_lc.LIFECYCLE_INITIALIZED, _lc.LIFECYCLE_OPEN):
        return None
    uids = _lc.player_uids(session)
    rev = getattr(session, "definition_revision", None)
    if len(uids) != 1 or not rev:
        return None
    definition = _pw.load_definition(session, rev)
    if definition is None:
        return None
    return CardWorld(config_of(scenario_data), definition, _lc.pc_actor_id(uids[0]),
                     str(getattr(session, "scenario_id", "") or ""))


def known_infos(session, pc_actor: str) -> frozenset:
    """커밋된 PC 인지 중 CONTENT인 InfoId 집합(EXISTENCE만으로는 공개하지 않는다)."""
    from . import cognition as _cog
    rows = (getattr(session, "cognition_state", None) or {}).get(_cog.actor_holder(pc_actor)) or {}
    return frozenset(iid for iid, rec in rows.items()
                     if isinstance(rec, dict) and rec.get("awareness") == _cog.CONTENT)


# ══════════════════════════════════════════════════════════════
#  화자 식별 · 표시 컨텍스트
# ══════════════════════════════════════════════════════════════

SPEAKER_ACTOR, SPEAKER_IRREGULAR, SPEAKER_UNKNOWN = "ACTOR", "IRREGULAR", "UNKNOWN"
SPEAKER_PLAIN = "PLAIN"          # 시나리오 지정 원문 화자(speaker_card.plain_speakers) — 원문 헤더·이미지 없음


@dataclass(frozen=True)
class DisplayContext:
    """한 대사의 표시 결정 — 헤더와 카드가 같은 식별·같은 커밋 인지를 쓴다."""
    speaker: str                     # 원본 화자 문자열(내부 식별·TTS·병합은 이것을 쓴다)
    status: str                      # ACTOR | IRREGULAR | UNKNOWN
    actor_id: str | None
    header: str                      # 플레이어에게 보이는 헤더 이름(실명 또는 "?")
    card: dict | None                # 이 인물의 카드 정의(없으면 이미지 생략)
    known: frozenset = frozenset()
    world: CardWorld | None = None


def resolve_speaker(world: CardWorld, session, speaker: str):
    """(status, actor_id). 모호·미등록은 UNKNOWN — 다른 인물을 추측하지 않는다."""
    from . import actors as _actors
    from . import irregular_npc as _irr
    res = _actors.resolve_actor_id(world.definition, session, speaker)
    if res.actor_id is not None and res.actor_id != world.pc_actor:
        return SPEAKER_ACTOR, res.actor_id
    if res.actor_id is None and not res.candidates and speaker in _irr.get_registry(session):
        return SPEAKER_IRREGULAR, None
    return SPEAKER_UNKNOWN, None


def _logged(session, ctx: DisplayContext) -> DisplayContext:
    """구조 로그: 대사 1건의 화자 식별·헤더 결정."""
    try:
        from . import logs as _logs
        _logs.domain("card.speaker", {"speaker": ctx.speaker, "status": ctx.status, "actor": ctx.actor_id,
                                      "header": ctx.header, "has_card": ctx.card is not None}, session=session)
    except Exception:  # noqa: BLE001
        pass
    return ctx


def display_context(session, speaker: str) -> DisplayContext | None:
    """SVG 카드 세션이면 표시 컨텍스트, 아니면 None(호출자는 기존 PNG·실명 헤더 경로를 그대로 쓴다)."""
    world = card_world(session)
    if world is None:
        return None
    known = known_infos(session, world.pc_actor)
    if speaker in (world.config.get("plain_speakers") or ()):
        return _logged(session, DisplayContext(speaker, SPEAKER_PLAIN, None, speaker, None, known, world))
    status, aid = resolve_speaker(world, session, speaker)
    if status == SPEAKER_UNKNOWN:
        return _logged(session, DisplayContext(speaker, status, None, UNKNOWN_TEXT, None, known, world))
    card = (world.config.get("actors") or {}).get(aid) if aid else None
    card = card if isinstance(card, dict) else None
    if card is None:
        # 카드 미저작(또는 비정규 NPC) — 이미지 생략. 카드가 없다는 이유만으로 이름을 숨기지 않는다.
        return _logged(session, DisplayContext(speaker, status, aid, speaker, None, known, world))
    name_info = (card.get("knowledge") or {}).get("name")
    header = speaker if (isinstance(name_info, str) and name_info in known) else UNKNOWN_TEXT
    return _logged(session, DisplayContext(speaker, status, aid, header, card, known, world))


# ══════════════════════════════════════════════════════════════
#  공개 규칙 평가 → 안전 슬롯값
# ══════════════════════════════════════════════════════════════

def _rule_slots(card: dict) -> list:
    return [s for r in (card.get("rules") or []) for s in ((r or {}).get("set") or {})]


def actor_default_variation(config: dict, card: dict):
    return card.get("default_variation") or config.get("default_variation")


def choose_variation(config: dict, card: dict, chosen) -> str | None:
    """추출층위 선택이 저작 목록에 있으면 그것, 아니면(누락·무효·실패) 기본 바리에이션."""
    variations = card.get("variations") or {}
    if isinstance(chosen, str) and chosen in variations:
        return chosen
    dv = actor_default_variation(config, card)
    return dv if dv in variations else None


def evaluate_slots(config: dict, card: dict, known, variation) -> dict:
    """모든 슬롯을 unknown으로 초기화 → 참인 규칙(all_info 전부 CONTENT)의 set을 일괄 적용.

    반환값은 템플릿에 넣어도 되는 안전한 값뿐이다(비공개 원본 값은 여기서 이미 걸러진다).
    같은 슬롯을 둘 이상의 규칙이 제어하면 fail closed(CardError).
    """
    slots = config.get("slots") or {}
    used = _rule_slots(card)
    if len(used) != len(set(used)):
        raise CardError("같은 슬롯을 둘 이상의 규칙이 제어합니다")
    out = {name: spec.get("unknown") for name, spec in slots.items()}
    values = card.get("values") or {}
    for rule in card.get("rules") or []:
        need = ((rule or {}).get("when") or {}).get("all_info") or []
        if not need or not all(i in known for i in need):
            continue
        for slot, eff in (rule.get("set") or {}).items():
            if slot not in slots:
                raise CardError(f"정의되지 않은 슬롯 {slot}")
            if eff.get("variation_image"):
                path = (card.get("variations") or {}).get(variation) if variation else None
                if path is None:
                    raise CardError("바리에이션 자산이 없습니다")
                out[slot] = path
            else:
                key = eff.get("value")
                if key not in values:
                    raise CardError(f"값 {key}가 없습니다")
                out[slot] = values[key]
    return out


# ══════════════════════════════════════════════════════════════
#  자산 · 해시
# ══════════════════════════════════════════════════════════════

def media_root(scenario_id: str, base: str = "media") -> str:
    return os.path.join(base, scenario_id)


def safe_asset_path(root: str, rel) -> str:
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or "\\" in rel:
        raise CardAssetError(f"허용되지 않은 자산 경로 {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise CardAssetError(f"허용되지 않은 자산 경로 {rel!r}")
    full = os.path.realpath(os.path.join(root, *parts))
    base = os.path.realpath(root)
    if os.path.commonpath([full, base]) != base:
        raise CardAssetError(f"미디어 루트 밖 경로 {rel!r}")
    return full


def read_verified(config: dict, root: str, rel) -> bytes:
    """저작 SHA-256과 일치하는 자산 바이트. 해시 미저작·불일치·누락은 CardAssetError."""
    expected = (config.get("sha256") or {}).get(rel)
    if not isinstance(expected, str) or not _SHA_RE.match(expected):
        raise CardAssetError(f"{rel}: 기대 SHA-256이 저작되지 않았습니다")
    path = safe_asset_path(root, rel)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise CardAssetError(f"{rel}: 읽을 수 없습니다") from e
    if hashlib.sha256(data).hexdigest() != expected:
        raise CardAssetError(f"{rel}: SHA-256 불일치")
    return data


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise CardAssetError("이미지 자산은 PNG/JPEG만 허용합니다")


def _data_uri(data: bytes) -> str:
    return f"data:{_image_mime(data)};base64," + base64.b64encode(data).decode("ascii")


# ══════════════════════════════════════════════════════════════
#  템플릿 안전 검사 · 조립
# ══════════════════════════════════════════════════════════════

def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _href(el):
    for k in _HREF_KEYS:
        if k in el.attrib:
            return el.attrib[k]
    return None


def _css_safe(text: str) -> bool:
    low = (text or "").lower()
    if "@import" in low or "expression(" in low or "javascript:" in low:
        return False
    return all(u.strip().strip("'\"").startswith("#") for u in re.findall(r"url\(([^)]*)\)", low))


def parse_template(data: bytes):
    """템플릿을 파싱하고 안전 규약을 검사한다. 위반은 CardError."""
    if b"<!DOCTYPE" in data[:4096].upper() or b"<!ENTITY" in data.upper():
        raise CardError("DOCTYPE/ENTITY는 허용하지 않습니다")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CardError(f"템플릿 파싱 실패: {e}") from e
    if _local(root.tag) != "svg":
        raise CardError("SVG 루트가 아닙니다")
    for el in root.iter():
        name = _local(el.tag)
        if name in _BAD_TAGS:
            raise CardError(f"허용되지 않은 요소 {name}")
        for k, v in el.attrib.items():
            if _local(k).lower().startswith("on"):
                raise CardError("이벤트 핸들러 속성은 허용하지 않습니다")
            if _local(k) == "style" and not _css_safe(v):
                raise CardError("허용되지 않은 style")
        h = _href(el)
        if h is not None and not (h.startswith("#") or _DATA_IMAGE_RE.match(h)):
            raise CardError(f"허용되지 않은 참조 {h[:40]!r}")
        if name == "style" and not _css_safe(el.text or ""):
            raise CardError("허용되지 않은 style 요소")
    return root


def _index(root):
    ids, parents = {}, {}
    for p in root.iter():
        for c in list(p):
            parents[c] = p
            i = c.attrib.get("id")
            if i:
                ids[i] = c
    return ids, parents


def _classes(el) -> set:
    return set((el.attrib.get("class") or "").split())


def _remove(el, parents):
    p = parents.get(el)
    if p is not None and el in list(p):
        p.remove(el)


def preset_candidates(presets: dict, name, length: int) -> list:
    """길이별 프리셋 후보(목록, 또는 {프리셋명: 배치} → 이름 정렬 순)."""
    table = (presets.get(name) or {}).get(str(length)) or []
    if isinstance(table, dict):
        return [table[k] for k in sorted(table)]
    return list(table)


def stable_index(text: str, n: int) -> int:
    """같은 길이 후보 중 결정적 선택 — sum(ord) % n."""
    return sum(ord(c) for c in text) % n if n else 0


def _set_text(el, value: str):
    for c in list(el):
        el.remove(c)
    el.text = value


def _text_target(el):
    """<text>에 textPath가 있으면 그 textPath에 값을 넣는다."""
    for c in el:
        if _local(c.tag) == "textPath":
            return c
    return el


def _rgb(color: str) -> tuple:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


_LUM = (0.2126, 0.7152, 0.0722)


def _recolor_filter(fid: str, color: str, baseline: str | None = None):
    """출력 = color × 휘도(입력). baseline을 주면 그 색의 휘도가 정확히 color가 되도록 배율을 곱한다."""
    r, g, b = _rgb(color)
    gain = 1.0
    if baseline:
        bl = sum(c * w for c, w in zip(_rgb(baseline), _LUM))
        gain = 1.0 / bl if bl > 0 else 1.0
    lum = _LUM
    rows = []
    for ch in (r, g, b):
        rows += [f"{ch * w * gain:.5f}" for w in lum] + ["0", "0"]
    rows += ["0", "0", "0", "1", "0"]
    flt = ET.Element(f"{{{SVG_NS}}}filter", {"id": fid, "color-interpolation-filters": "sRGB"})
    ET.SubElement(flt, f"{{{SVG_NS}}}feColorMatrix", {"type": "matrix", "values": " ".join(rows)})
    return flt


def build_svg(config: dict, slots: dict, root_dir: str, *, template_root=None) -> bytes:
    """안전 슬롯값을 템플릿에 적용한 SVG 바이트(비공개 원본 값은 입력되지 않는다)."""
    tpl = template_root if template_root is not None else parse_template(
        read_verified(config, root_dir, config.get("template")))
    root = copy.deepcopy(tpl)
    ids, parents = _index(root)
    # 1) 고정 샘플 제거(메타데이터·제목·설명 + 지정 class/id)
    remove_classes = set(config.get("remove_classes") or ())
    for el in list(root.iter()):
        if _local(el.tag) in _STRIP_TAGS or (_classes(el) & remove_classes) or \
                el.attrib.get("id") in set(config.get("remove_ids") or ()):
            _remove(el, parents)
    show = set(config.get("show_classes") or ())
    for el in root.iter():
        if _classes(el) & show:
            el.set("style", "display:inline")
    ids, parents = _index(root)
    # 2) 맨 앞 요소 — 지정 요소를 같은 부모의 끝으로 옮긴다(순서 유지). 초상 등 슬롯보다 위에 그려진다.
    for fid in config.get("front_ids") or ():
        el = ids.get(fid)
        if el is None:
            raise CardError(f"front_ids: 대상 요소 {fid}가 없습니다")
        parent = parents[el]
        parent.remove(el)
        parent.append(el)
    defs = next((c for c in root if _local(c.tag) == "defs"), None)
    if defs is None:
        defs = ET.Element(f"{{{SVG_NS}}}defs")
        root.insert(0, defs)
    presets = config.get("text_presets") or {}
    seq = itertools.count()
    for slot_name, spec in (config.get("slots") or {}).items():
        value = slots.get(slot_name)
        kind = spec.get("kind")
        if kind == "color" and not (isinstance(spec.get("unknown"), str) and _COLOR_RE.match(spec["unknown"])):
            raise CardError(f"{slot_name}: 색 슬롯의 unknown은 색 값이어야 합니다(원본 유지 불가)")
        for b in spec.get("bind") or []:
            op = b.get("op")
            if op == "chars":
                chars = list(value or "")
                targets = [ids.get(i) for i in b.get("ids") or []]
                if None in targets or len(chars) > len(targets):
                    raise CardError(f"{slot_name}: 글자 슬롯이 부족하거나 없습니다")
                table = preset_candidates(presets, b.get("presets"), len(chars))
                if chars and not table:
                    raise CardError(f"{slot_name}: {len(chars)}글자 프리셋이 없습니다")
                layout = table[stable_index("".join(chars), len(table))] if chars else []
                for n, el in enumerate(targets):
                    for a in ("textLength", "lengthAdjust"):
                        el.attrib.pop(a, None)
                    if n < len(chars):
                        _set_text(el, chars[n])
                        pos = layout[n]
                        el.set("x", str(pos["x"]))
                        el.set("y", str(pos["y"]))
                        el.set("font-size", str(pos["font_size"]))
                    else:
                        _set_text(el, "")
                continue
            el = ids.get(b.get("id"))
            if el is None:
                raise CardError(f"{slot_name}: 대상 요소 {b.get('id')}가 없습니다")
            if op == "text":
                _set_text(_text_target(el), "" if value is None else str(value))
            elif op in ("attr", "recolor") and value is None:
                continue                                   # 공개 값 null = 템플릿 원본 유지(unknown은 null 불가)
            elif op == "attr":
                if kind == "color" and not (isinstance(value, str) and _COLOR_RE.match(value)):
                    raise CardError(f"{slot_name}: 색 값이 아닙니다")
                el.set(b["attr"], str(value))
                for k, v in (b.get("static") or {}).items():
                    el.set(k, str(v))
            elif op == "recolor":
                if not (isinstance(value, str) and _COLOR_RE.match(value)):
                    raise CardError(f"{slot_name}: 색 값이 아닙니다")
                fid = f"speaker-card-recolor-{next(seq)}"
                defs.append(_recolor_filter(fid, value, b.get("baseline")))
                parent = parents[el]
                wrap = ET.Element(f"{{{SVG_NS}}}g", {"filter": f"url(#{fid})"})
                parent.insert(list(parent).index(el), wrap)
                parent.remove(el)
                wrap.append(el)
                parents[el], parents[wrap] = wrap, parent
            elif op == "image":
                if value is None:
                    for c in list(el):
                        el.remove(c)
                    if _local(el.tag) == "image":
                        _remove(el, parents)
                    continue
                uri = _data_uri(read_verified(config, root_dir, value))
                if _local(el.tag) == "image":
                    for k in _HREF_KEYS:
                        el.attrib.pop(k, None)
                    el.set(f"{{{XLINK_NS}}}href", uri)
                else:
                    for c in list(el):
                        el.remove(c)
                    geo = {k: str(b[k]) for k in ("x", "y", "width", "height")}
                    img = ET.SubElement(el, f"{{{SVG_NS}}}image", geo)
                    img.set("preserveAspectRatio", str(b.get("preserve_aspect_ratio") or "xMidYMid meet"))
                    img.set(f"{{{XLINK_NS}}}href", uri)
            else:
                raise CardError(f"{slot_name}: 알 수 없는 연산 {op}")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ══════════════════════════════════════════════════════════════
#  래스터화(SVG → PNG)
# ══════════════════════════════════════════════════════════════

def find_renderer():
    """(종류, 실행 수단) 또는 None. INDAIM_SVG_RENDERER(resvg|resvg_py|rsvg-convert) > resvg CLI > resvg_py.

    textPath(경로형 텍스트)를 그리지 못하는 rsvg-convert는 명시 선택했을 때만 쓴다(로컬 실측: 라틴명·세력명 누락).
    렌더러·폰트 설치는 운영 서버 설정이다(이 모듈은 설치하지 않는다).
    """
    pref = os.environ.get("INDAIM_SVG_RENDERER")
    order = [pref] if pref else ["resvg", "resvg_py"]
    for name in order:
        if name == "resvg_py":
            try:
                import resvg_py  # noqa: F401
                return "resvg_py", None
            except ImportError:
                continue
        exe = shutil.which(name) if name else None
        if exe:
            return ("rsvg-convert" if "rsvg-convert" in os.path.basename(exe) else "resvg"), exe
    return None


def rasterize(svg: bytes) -> bytes:
    found = find_renderer()
    if found is None:
        raise RendererUnavailable("SVG 렌더러(resvg)가 없습니다")
    kind, exe = found
    if kind == "resvg_py":
        import resvg_py
        try:
            png = bytes(resvg_py.svg_to_bytes(svg_string=svg.decode("utf-8")))
        except Exception as e:  # noqa: BLE001
            raise CardError(f"SVG 렌더 실패: {type(e).__name__}") from e
    else:
        with tempfile.TemporaryDirectory(prefix="speaker_card_") as tmp:
            src, dst = os.path.join(tmp, "card.svg"), os.path.join(tmp, "card.png")
            with open(src, "wb") as f:
                f.write(svg)
            cmd = [exe, "-f", "png", "-o", dst, src] if kind == "rsvg-convert" else [exe, src, dst]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=RENDER_TIMEOUT_SEC)
                with open(dst, "rb") as f:
                    png = f.read()
            except (subprocess.SubprocessError, OSError) as e:
                raise CardError(f"SVG 렌더 실패: {type(e).__name__}") from e
    if not png.startswith(b"\x89PNG"):
        raise CardError("렌더 결과가 PNG가 아닙니다")
    return png


# 같은 입력(템플릿·안전 슬롯값) = 같은 PNG — 같은 인물의 반복 대사에 렌더를 다시 하지 않는다(프로세스 메모리).
_PNG_CACHE: dict = {}
_PNG_CACHE_MAX = 32


def render_png(world: CardWorld, card: dict, known, variation_choice, *, base: str = "media") -> bytes:
    """동기 렌더(스레드에서 호출). 평가 → 안전 SVG → PNG."""
    cfg = world.config
    variation = choose_variation(cfg, card, variation_choice)
    slots = evaluate_slots(cfg, card, known, variation)
    svg = build_svg(cfg, slots, media_root(world.scenario_id, base))   # 자산 해시는 매번 검증한다
    key = hashlib.sha256(svg).hexdigest()
    png = _PNG_CACHE.get(key)
    if png is None:
        png = rasterize(svg)
        if len(_PNG_CACHE) >= _PNG_CACHE_MAX:
            _PNG_CACHE.pop(next(iter(_PNG_CACHE)))
        _PNG_CACHE[key] = png
    return png


# ══════════════════════════════════════════════════════════════
#  전달 경로 연결(이미지 지점에서만 추출 대기)
# ══════════════════════════════════════════════════════════════

_FILE_SEQ = itertools.count(1)


async def turn_variation(preparation, actor_id):
    """이미지 지점에서만 이번 턴 추출 작업을 기다린다. 실패·누락이면 None(→ 기본 바리에이션).

    준비 작업 래퍼가 예외를 삼키고 상태로만 기록하므로 여기서 기다려도 RETRY_PENDING 판정은 바뀌지 않는다.
    """
    if preparation is None:
        return None
    fut = preparation.task_future("extraction") if hasattr(preparation, "task_future") else None
    if fut is not None:
        try:
            await asyncio.shield(fut)
        except Exception:
            pass
    return (getattr(preparation, "card_variations", None) or {}).get(actor_id)


async def prepare_card(session, ctx: DisplayContext | None, preparation=None):
    """카드 PNG 바이트 또는 None(카드 없음·실패 — 이미지 생략)."""
    if ctx is None or ctx.card is None or ctx.world is None:
        return None
    try:
        # 바리에이션이 하나뿐이면 고를 것이 없다 — 추출을 기다리지 않는다
        variation = (await turn_variation(preparation, ctx.actor_id)
                     if len(ctx.card.get("variations") or {}) > 1 else None)
        t0 = time.monotonic()
        png = await asyncio.to_thread(render_png, ctx.world, ctx.card, ctx.known, variation)
        _log_card(session, ctx, variation, ms=int((time.monotonic() - t0) * 1000), size=len(png or b""))
        return png
    except Exception as e:  # noqa: BLE001 — 카드 실패는 이미지 생략(대사 유지)
        from . import logs as _logs
        _logs.error("card", f"카드 생략 — {ctx.actor_id}", exc=e, session=session)
        return None


def _log_card(session, ctx, variation, **extra) -> None:
    """구조 로그: 카드 1장 — 화자·헤더·공개 판정 근거(커밋 인지 중 이 인물 InfoId)."""
    try:
        from . import logs as _logs
        know = ctx.card.get("knowledge") or {}
        _logs.domain("card.render", {"speaker": ctx.speaker, "actor": ctx.actor_id, "header": ctx.header,
                                     "variation": variation,
                                     "knows": {k: (v in ctx.known) for k, v in know.items()}, **extra},
                     session=session)
    except Exception:  # noqa: BLE001
        pass


async def send_card(channel, session, png, collector=None) -> bool:
    """카드 첨부 전송. 파일명에 실명·세력·상태를 넣지 않는다. 실패는 이미지 생략."""
    if not png:
        return False
    import discord
    name = f"speaker_card_{int(getattr(session, 'turn_count', 0) or 0) + 1}_{next(_FILE_SEQ)}.png"
    try:
        msg = await channel.send(file=discord.File(io.BytesIO(png), filename=name))
    except Exception as e:  # noqa: BLE001
        from . import logs as _logs
        _logs.error("card", "카드 전송 실패(이미지 생략)", exc=e, session=session)
        return False
    if collector is not None:
        collector.append(msg)
    return True


# ══════════════════════════════════════════════════════════════
#  추출층위 · 지시층위 런타임 매니페스트
# ══════════════════════════════════════════════════════════════

def scene_card_actors(world: CardWorld, wt, session) -> list:
    """현재 장면 NPC 중 카드가 저작된 인물(ActorId, 정렬)."""
    from . import turn_world as _tw
    actors = world.config.get("actors") or {}
    try:
        _scene, projs = _tw.scene_npc_projections(wt.ctx, wt.plan, session)
    except Exception:
        return []
    return sorted({p["actor_id"] for p in projs if p.get("actor_id") in actors})


def variation_manifest(world: CardWorld, wt, session) -> dict:
    """{transient_key: (ActorId, (variation ids))} — 바리에이션이 둘 이상인 장면 인물만."""
    out = {}
    for n, aid in enumerate(scene_card_actors(world, wt, session), 1):
        vs = tuple(sorted((world.config["actors"][aid].get("variations") or {})))
        if len(vs) > 1:
            out[f"V{n}"] = (aid, vs)
    return out


def variation_schema(manifest: dict) -> dict:
    return {"type": "object",
            "description": "장면 인물별 이번 턴 표정·상태 바리에이션(목록 안에서 하나).",
            "properties": {k: {"type": "string", "enum": list(vs)} for k, (_a, vs) in manifest.items()}}


def variation_block(manifest: dict, definition: dict, session) -> str:
    from . import actors as _actors
    if not manifest:
        return ""
    lines = ["\n[화자 카드 바리에이션 — speaker_variations]",
             "이번 묘사 전체에서 각 인물이 보인 상태에 가장 맞는 것을 키별로 하나만 고른다. 드러나지 않았으면 비워 둔다."]
    for k, (aid, vs) in manifest.items():
        lines.append(f"- {k}: {_actors.display_name(definition, session, aid) or aid} — {', '.join(vs)}")
    return "\n".join(lines) + "\n"


def parse_variations(manifest: dict, raw) -> dict:
    """{ActorId: variation} — 매니페스트 밖 키·목록 밖 값은 버린다."""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            ent = manifest.get(k)
            if ent and v in ent[1]:
                out[ent[0]] = v
    return out


_IDENTIFY = {
    # kind: (transient 키 접두, 응답 필드, 후보 블록 제목, 판정 지시, 스키마 설명)
    "face": ("F", "identified_faces", "얼굴 식별 후보",
             "이번 장면에서 PC가 그 인물의 얼굴을 분명히 보고 식별하게 되는 경우에만 키를 넣는다. "
             "같은 장소에 있다는 것만으로는 넣지 않는다.",
             "이번 장면에서 PC가 얼굴을 실제로 식별하게 되는 인물 키."),
    "name": ("N", "identified_names", "이름 식별 후보",
             "이번 장면에서 PC가 그 인물의 이름을 실제로 알게 되는 경우(자기소개·호명·명패·제3자의 소개 등)에만 "
             "키를 넣는다. 얼굴을 보거나 같은 장소에 있다는 것만으로는 넣지 않는다.",
             "이번 장면에서 PC가 이름을 실제로 알게 되는 인물 키."),
}


def identity_manifest(world: CardWorld, wt, session, kind: str) -> dict:
    """{transient_key: ActorId} — knowledge.<kind> InfoId가 저작된 장면 카드 인물."""
    prefix = _IDENTIFY[kind][0]
    out = {}
    actors = world.config.get("actors") or {}
    for n, aid in enumerate(scene_card_actors(world, wt, session), 1):
        if isinstance((actors[aid].get("knowledge") or {}).get(kind), str):
            out[f"{prefix}{n}"] = aid
    return out


def identity_field(kind: str) -> str:
    return _IDENTIFY[kind][1]


def identity_schema(manifest: dict, kind: str) -> dict:
    return {"type": "array", "description": _IDENTIFY[kind][4],
            "items": {"type": "string", "enum": list(manifest)}}


def identity_block(manifest: dict, definition: dict, session, kind: str) -> str:
    from . import actors as _actors
    if not manifest:
        return ""
    _p, field, title, rule, _d = _IDENTIFY[kind]
    lines = [f"\n[{title} — {field}]",
             rule + " 넣었다면 proceed_instruction에 그 장면이 실제로 묘사되게 한다. 해당 없으면 빈 배열."]
    for k, aid in manifest.items():
        lines.append(f"- {k}: {_actors.display_name(definition, session, aid) or aid}")
    return "\n".join(lines) + "\n"


def parse_identified(manifest: dict, raw) -> tuple:
    """후보 키 → ActorId(정렬). 후보 밖 키·형식 오류는 버린다."""
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({manifest[k] for k in raw if isinstance(k, str) and k in manifest}))


def identity_info_ids(config: dict, actor_ids, kind: str) -> tuple:
    actors = (config or {}).get("actors") or {}
    out = []
    for aid in actor_ids or ():
        iid = ((actors.get(aid) or {}).get("knowledge") or {}).get(kind)
        if isinstance(iid, str) and iid:
            out.append(iid)
    return tuple(sorted(set(out)))


# 얼굴 식별(기존 호출 이름 유지)
def face_manifest(world, wt, session):
    return identity_manifest(world, wt, session, "face")


def face_schema(manifest):
    return identity_schema(manifest, "face")


def face_block(manifest, definition, session):
    return identity_block(manifest, definition, session, "face")


parse_faces = parse_identified


def face_info_ids(config, actor_ids):
    return identity_info_ids(config, actor_ids, "face")


# ══════════════════════════════════════════════════════════════
#  저작 검사(독립 — 컴파일러 ERROR 아님)
# ══════════════════════════════════════════════════════════════

def check_card_definition(scenario: dict, *, definition: dict | None = None, base: str = "media",
                          scenario_id: str | None = None) -> list:
    """speaker_card 저작 검사. 반환: [(severity, code, path, message)]. 정의가 없으면 []."""
    cfg = config_of(scenario)
    if cfg is None:
        return []
    out = []

    def err(code, path, msg):
        out.append(("ERROR", code, path, msg))

    if cfg.get("mode") not in (MODE_SVG, MODE_LEGACY):
        err("CARD_MODE", "speaker_card.mode", "mode는 svg 또는 legacy_png")
    if cfg.get("mode") != MODE_SVG:
        return out
    if cfg.get("schema_version") != SCHEMA_VERSION:
        err("CARD_SCHEMA_VERSION", "speaker_card.schema_version", f"schema_version은 {SCHEMA_VERSION}")
    sid = scenario_id or scenario.get("id") or ""
    root_dir = media_root(sid, base)
    hashes = cfg.get("sha256") or {}
    if not isinstance(hashes, dict):
        err("CARD_SHA_MAP", "speaker_card.sha256", "sha256은 {상대경로: hex}")
        hashes = {}

    def check_asset(rel, path, image=True):
        try:
            data = read_verified(cfg, root_dir, rel)
            if image:
                _image_mime(data)
            return data
        except CardAssetError as e:
            err("CARD_ASSET", path, str(e))
        return None

    tpl_bytes = check_asset(cfg.get("template"), "speaker_card.template", image=False)
    tpl = None
    if tpl_bytes is not None:
        try:
            tpl = parse_template(tpl_bytes)
        except CardError as e:
            err("CARD_TEMPLATE_UNSAFE", "speaker_card.template", str(e))
    ids = set()
    if tpl is not None:
        ids = {el.attrib["id"] for el in tpl.iter() if el.attrib.get("id")}
    slots = cfg.get("slots") or {}
    if not isinstance(slots, dict) or not slots:
        err("CARD_SLOTS", "speaker_card.slots", "슬롯이 없습니다")
        slots = {}
    presets = cfg.get("text_presets") or {}
    bound_ids = set()
    for name, spec in slots.items():
        p = f"speaker_card.slots.{name}"
        kind = (spec or {}).get("kind")
        if kind not in SLOT_KINDS:
            err("CARD_SLOT_KIND", p, f"kind는 {SLOT_KINDS}")
            continue
        if "unknown" not in spec:
            err("CARD_SLOT_UNKNOWN", p, "모든 슬롯은 unknown 값이 있어야 합니다")
        unk = spec.get("unknown")
        if kind == "image" and unk is not None:
            check_asset(unk, f"{p}.unknown")
        if kind == "color" and not (isinstance(unk, str) and _COLOR_RE.match(unk)):
            err("CARD_COLOR", f"{p}.unknown", "색은 #rgb/#rrggbb")
        if kind == "text" and not isinstance(unk, str):
            err("CARD_SLOT_UNKNOWN", f"{p}.unknown", "텍스트 unknown은 문자열")
        for j, b in enumerate(spec.get("bind") or []):
            bp = f"{p}.bind[{j}]"
            op = (b or {}).get("op")
            if op not in BIND_OPS:
                err("CARD_BIND_OP", bp, f"op는 {BIND_OPS}")
                continue
            targets = list(b.get("ids") or []) if op == "chars" else [b.get("id")]
            for t in targets:
                bound_ids.add(t)
                if tpl is not None and t not in ids:
                    err("CARD_BIND_TARGET", bp, f"템플릿에 id {t!r}가 없습니다")
            if op == "chars" and b.get("presets") not in presets:
                err("CARD_PRESET", bp, "text_presets에 없는 프리셋")
            if op == "attr" and b.get("attr") not in COLOR_ATTRS:
                err("CARD_BIND_ATTR", bp, f"attr는 {COLOR_ATTRS}")
            for k, v in ((b.get("static") or {}).items() if op == "attr" else ()):
                if k not in STATIC_ATTRS or not re.match(r"^(?:[0-9]*\.?[0-9]+|inline|none)$", str(v)):
                    err("CARD_BIND_ATTR", bp, f"static 속성은 {STATIC_ATTRS}(숫자·inline·none)")
            if op == "recolor" and b.get("baseline") is not None and not (
                    isinstance(b["baseline"], str) and _COLOR_RE.match(b["baseline"])):
                err("CARD_COLOR", f"{bp}.baseline", "baseline은 #rgb/#rrggbb")
            if op in ("attr", "recolor") and kind != "color":
                err("CARD_BIND_KIND", bp, f"{op}는 color 슬롯 전용")
            if op == "image" and kind != "image":
                err("CARD_BIND_KIND", bp, "image는 image 슬롯 전용")
            if op in ("text", "chars") and kind != "text":
                err("CARD_BIND_KIND", bp, f"{op}는 text 슬롯 전용")
    for fid in cfg.get("front_ids") or ():
        if tpl is not None and fid not in ids:
            err("CARD_BIND_TARGET", "speaker_card.front_ids", f"템플릿에 id {fid!r}가 없습니다")
    for pname, table in presets.items():
        for length in (table or {}):
            for c in preset_candidates(presets, pname, int(length)):
                if len(c) != int(length) or any(not {"x", "y", "font_size"} <= set(e) for e in c):
                    err("CARD_PRESET", f"speaker_card.text_presets.{pname}.{length}", "프리셋 항목 형식 오류")
    # 고정 비밀 누설 방지 — 템플릿의 data-slot 요소는 모두 바인딩·제거·고정 선언 중 하나여야 한다
    if tpl is not None:
        _check_data_slot_coverage(cfg, tpl, bound_ids, err)
    infos = set(((definition or {}).get("information_definitions") or {}))
    actor_defs = set(((definition or {}).get("actor_definitions") or {}))
    name_slots = set(cfg.get("name_slots") or ())
    plain = cfg.get("plain_speakers")
    if plain is not None and not (isinstance(plain, list) and all(isinstance(x, str) and x for x in plain)):
        err("CARD_PLAIN_SPEAKERS", "speaker_card.plain_speakers", "plain_speakers는 화자 이름 문자열 목록")
    for s in name_slots:
        if s not in slots:
            err("CARD_NAME_SLOT", "speaker_card.name_slots", f"정의되지 않은 슬롯 {s}")
    for aid, card in (cfg.get("actors") or {}).items():
        ap = f"speaker_card.actors.{aid}"
        if definition is not None and aid not in actor_defs:
            err("CARD_ACTOR", ap, "actor_definitions에 없는 ActorId")
        know = (card or {}).get("knowledge") or {}
        for k in ("face", "name"):
            if not isinstance(know.get(k), str):
                err("CARD_KNOWLEDGE", f"{ap}.knowledge.{k}", f"knowledge.{k} InfoId가 필요합니다")
        for k, iid in know.items():
            if definition is not None and iid not in infos:
                err("CARD_INFO", f"{ap}.knowledge.{k}", f"저작되지 않은 InfoId {iid!r}")
        variations = card.get("variations") or {}
        dv = actor_default_variation(cfg, card)
        if dv not in variations:
            err("CARD_VARIATION", ap, "기본 바리에이션 자산이 없습니다")
        for v, rel in variations.items():
            check_asset(rel, f"{ap}.variations.{v}")
        values = card.get("values") or {}
        seen = {}
        for r, rule in enumerate(card.get("rules") or []):
            rp = f"{ap}.rules[{r}]"
            when = (rule or {}).get("when") or {}
            need = when.get("all_info")
            if set(when) != {"all_info"} or not isinstance(need, list) or not need:
                err("CARD_RULE_WHEN", rp, "when은 비어 있지 않은 all_info만 허용합니다")
                need = []
            for iid in need:
                if definition is not None and iid not in infos:
                    err("CARD_INFO", rp, f"저작되지 않은 InfoId {iid!r}")
            for slot, eff in ((rule or {}).get("set") or {}).items():
                if slot in seen:
                    err("CARD_SLOT_CONFLICT", rp, f"슬롯 {slot}을 규칙 {seen[slot]}도 제어합니다")
                seen[slot] = r
                spec = slots.get(slot)
                if spec is None:
                    err("CARD_SLOT", rp, f"정의되지 않은 슬롯 {slot}")
                    continue
                if (eff or {}).get("variation_image"):
                    if spec.get("kind") != "image":
                        err("CARD_VARIATION", rp, "variation_image는 image 슬롯 전용")
                    if know.get("face") not in need:
                        err("CARD_FACE_RULE", rp, "얼굴 공개 규칙은 knowledge.face를 조건으로 해야 합니다")
                    continue
                key = (eff or {}).get("value")
                if key not in values:
                    err("CARD_VALUE", rp, f"values에 없는 키 {key!r}")
                    continue
                _check_value(spec, values[key], f"{ap}.values.{key}", presets, check_asset, err)
                if slot in name_slots and need != [know.get("name")]:
                    err("CARD_NAME_RULE", rp, "이름 슬롯 규칙의 조건은 knowledge.name 하나여야 합니다")
    return out


def _check_value(spec, value, path, presets, check_asset, err):
    kind = spec.get("kind")
    if kind == "image":
        if value is not None:                              # null = 이미지 없음(build_svg가 비운다)
            check_asset(value, path)
    elif kind == "color":
        if value is not None and not (isinstance(value, str) and _COLOR_RE.match(value)):
            err("CARD_COLOR", path, "색은 #rgb/#rrggbb 또는 null(템플릿 원본 유지)")
    elif kind == "text":
        if not isinstance(value, str):
            err("CARD_VALUE", path, "텍스트 값은 문자열")
            return
        for b in spec.get("bind") or []:
            if b.get("op") == "chars":
                n = len(value)
                if n > len(b.get("ids") or []) or not preset_candidates(presets, b.get("presets"), n):
                    err("CARD_TEXT_LENGTH", path, f"{n}글자 프리셋·글자 슬롯이 없습니다")


def _check_data_slot_coverage(cfg, tpl, bound_ids, err):
    remove_classes = set(cfg.get("remove_classes") or ())
    remove_ids = set(cfg.get("remove_ids") or ())
    fixed = set(cfg.get("fixed_slots") or ())

    def removed(el, ancestors):
        return any((_classes(a) & remove_classes) or a.attrib.get("id") in remove_ids for a in ancestors + [el])

    def covered(el):
        for d in el.iter():
            if d.attrib.get("id") in bound_ids:
                return True
            h = _href(d)
            if h and h.startswith("#") and h[1:] in bound_ids:
                return True
        return False

    def walk(el, ancestors):
        slot = el.attrib.get("data-slot")
        if slot is not None and not removed(el, ancestors) and slot not in fixed and not covered(el):
            err("CARD_SLOT_UNBOUND", f"template#{el.attrib.get('id') or slot}",
                f"data-slot {slot!r}가 바인딩·제거·고정 선언 어디에도 없습니다(고정 내용 누설 위험)")
        for c in el:
            walk(c, ancestors + [el])

    walk(tpl, [])
