# 마스터 채널 턴 전수보고 (v7.30, 사용자 결정: 매 턴 · 비밀은 요약 + 펼치기 버튼 · 한도 초과는 분리 보고)
#
# 세 갈래를 색으로 나눈다 — 비용(주황, 기존 턴 비용 리포트 + 입력 구성·예상 대조) · 층위활동(파랑) · 세계 델타(초록).
# 세계 델타의 출처는 커밋된 턴 이력 레코드(turn_history)의 pre → post 정본 차이 하나다 — 플레이어 곁이든
# 떨어진 곳이든(장면 밖 인물 이동·사건·인지·예약·접근) 정본이 바뀐 것은 전부 나온다. 새 provider 호출·정본 쓰기 없음.
# 비밀(정보 명제)은 임베드에 요약만 싣고, 원문은 [🔒 비밀 원문] 버튼(재시작 뒤에도 동작하는 DynamicItem)이
# 같은 이력 레코드에서 다시 계산해 그 버튼을 누른 사람에게만(ephemeral) 보여 준다.
from __future__ import annotations

import json
import re

import discord

COLOR_COST, COLOR_ACTIVITY, COLOR_DELTA = 0xE67E22, 0x3498DB, 0x2ECC71
FIELD_MAX, EMBED_MAX, FIELDS_MAX, EMBEDS_PER_MESSAGE = 1024, 6000, 25, 10
SUMMARY_MAX = 80
# 델타 보고에서 따로 다루거나(아래 섹션) 잡음인 가역 필드 — 나머지 변경 필드는 '기타'로 이름만 알린다
_NOISE = {"raw_logs", "uncompressed_logs", "current_turn_logs", "gm_proceed_history", "last_extraction",
          "commit_marker", "turn_count", "gm_turns_done", "last_recorded_turn", "world_version",
          "cached_compressed_memory", "cached_session_npcs", "cached_worldview_sections", "compressed_memory",
          "gm_side_note", "gm_clarify_count", "gm_narrate_count", "pending_bgm", "last_bgm_situation", "item_facts"}


# ══════════════════════════════════════════════════════════════
#  분리 포장 — 필드 1024 · 임베드 6000 · 필드 25 · 메시지당 임베드 10
# ══════════════════════════════════════════════════════════════

def _chunks(lines, limit=FIELD_MAX - 4):
    """줄 목록 → 필드 값 조각들(줄 경계 우선, 한 줄이 넘치면 그 줄을 잘라 이어 싣는다 — 버리지 않는다)."""
    out, cur = [], ""
    for ln in lines:
        ln = str(ln)
        while len(ln) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(ln[:limit])
            ln = ln[limit:]
        if cur and len(cur) + 1 + len(ln) > limit:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out or ["—"]


def pack(title: str, color: int, sections, *, description: str | None = None) -> list:
    """[(섹션 이름, 줄 목록)] → 한도를 지키는 Embed 목록. 여러 장이면 제목에 (i/n)."""
    fields = []
    for name, lines in sections:
        parts = _chunks(lines)
        for i, v in enumerate(parts):
            fields.append(((name if i == 0 else f"{name} (이어서)")[:256], v))
    groups, cur, size = [], [], len(title) + 8 + len(description or "")
    for name, value in fields:
        add = len(name) + len(value)
        if cur and (len(cur) >= FIELDS_MAX or size + add > EMBED_MAX - 200):
            groups.append(cur)
            cur, size = [], len(title) + 8
        cur.append((name, value))
        size += add
    if cur or not groups:
        groups.append(cur)
    n = len(groups)
    embeds = []
    for i, g in enumerate(groups):
        e = discord.Embed(title=title + (f" ({i + 1}/{n})" if n > 1 else ""), color=color,
                          description=(description if i == 0 else None))
        for name, value in g:
            e.add_field(name=name, value=value, inline=False)
        embeds.append(e)
    return embeds


def embed_sections(embed) -> list:
    """기존 Embed(턴 비용 리포트)를 섹션으로 풀어 같은 포장 규칙에 태운다."""
    return [(f.name, (f.value or "—").split("\n")) for f in embed.fields]


# ══════════════════════════════════════════════════════════════
#  비용 — 입력 구성(키워드·인물) · 예상 대조
# ══════════════════════════════════════════════════════════════

def cost_sections(cost_log, estimate=None) -> list:
    secs = []
    for e in cost_log or ():
        rows = e.get("census") or []
        if rows:
            from .prompt_census import render
            secs.append((f"📐 입력 구성 · {e.get('label', '?')}", render(rows)))
    if estimate and estimate.get("breakdown"):
        from .estimate import actual_by_line
        act = actual_by_line(cost_log)
        lines = []
        for k, (lo, hi) in estimate["breakdown"].items():
            a = act.get(k)
            mark = "" if a is None else (" ✅" if lo - 0.005 <= a <= hi + 0.005 else (" ⬆️" if a > hi else " ⬇️"))
            lines.append(f"{k} 예상 {lo:.2f}~{hi:.2f}원 · 실제 " + ("—" if a is None else f"{a:.2f}원") + mark)
        for k, a in act.items():
            if k not in estimate["breakdown"]:
                lines.append(f"{k} 예상 밖 · 실제 {a:.2f}원")
        tot = sum(act.values())
        lines.append(f"**합** 예상 {estimate.get('min_krw', 0):.2f}~{estimate.get('max_krw', 0):.2f}원"
                     f"(중앙 {estimate.get('mid_krw', 0):.2f}) · 실제 {tot:.2f}원 (TTS 제외)")
        from .estimate import format_estimate_detail
        lines += format_estimate_detail(estimate)
        secs.append(("🔮 층위별 예상 대조", lines))
    return secs


# ══════════════════════════════════════════════════════════════
#  층위활동
# ══════════════════════════════════════════════════════════════

def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str, separators=(",", ":"))


def activity_sections(prep, record=None, diagnostics=()) -> list:
    secs = []
    rec = record or {}
    decl = rec.get("player_declaration")
    if decl:
        secs.append(("🗣️ 플레이어 선언", [str(decl)]))
    jd = rec.get("judgment")
    if jd:
        head = [f"유형 **{jd.get('type', '?')}**"] if isinstance(jd, dict) else []
        secs.append(("🧭 판단층위", head + [_j(jd)]))
    ph = getattr(prep, "proceed_history_entry", None) or {}
    if ph.get("instruction"):
        secs.append(("🎬 지시층위 지시문", str(ph["instruction"]).split("\n")))
    if ph.get("ai_summary"):
        secs.append(("🧾 지시층위 요약", [str(ph["ai_summary"])]))
    narr = getattr(prep, "narration", None)
    if narr is not None and getattr(narr, "text", None):
        t = narr.text
        paras = [p for p in t.split("\n") if p.strip()]
        speakers = sorted(set(re.findall(r"@대사:([^|\n]+)\|", t)))
        secs.append(("🖋️ 묘사층위", [f"{len(t):,}자 · 문단 {len(paras)}"
                                     + (f" · 대사 {'·'.join(speakers)}" if speakers else ""),
                                     (paras[0][:200] + ("…" if len(paras[0]) > 200 else "")) if paras else ""]))
    ext = getattr(prep, "extraction_text", None)
    if ext:
        secs.append(("🔎 추출층위(원문)", str(ext).split("\n")))
    batch = getattr(prep, "world_mutation_batch", None)
    if batch is not None:
        try:
            counts = {}
            for op in batch.operations:
                counts[op.op_type] = counts.get(op.op_type, 0) + 1
            if counts:
                secs.append(("⚙️ 세계 연산", [" · ".join(f"{k} ×{v}" for k, v in sorted(counts.items()))]))
        except Exception:  # noqa: BLE001
            pass
    diags = list(diagnostics or ()) + list(getattr(prep, "world_diagnostics", None) or ())
    if diags:
        secs.append(("⚠️ 진단", [f"{d.get('code')}: {_j(d.get('detail'))}" for d in diags]))
    return secs


# ══════════════════════════════════════════════════════════════
#  세계 델타 (pre → post)
# ══════════════════════════════════════════════════════════════

def _definition(session):
    rev = getattr(session, "definition_revision", None)
    if not rev:
        return {}
    try:
        from .player_world import load_definition
        return load_definition(session, rev) or {}
    except Exception:  # noqa: BLE001
        return {}


def _clock(definition, clock):
    m = (clock or {}).get("minute_index")
    if m is None:
        return None, "미초기화"
    try:
        from .turn_world import clock_text, clock_view
        cv = clock_view(definition, m)
        return m, (clock_text(cv) if cv else f"분 {m}")
    except Exception:  # noqa: BLE001
        return m, f"분 {m}"


def _actor(definition, world: dict, aid: str) -> str:
    e = (world.get("actor_registry") or {}).get(aid)
    if isinstance(e, dict) and e.get("display_name"):
        return e["display_name"]
    a = (definition.get("actor_definitions") or {}).get(aid) or {}
    return a.get("display_name") or aid


def _place(p) -> str:
    if not isinstance(p, dict):
        return "—"
    if p.get("kind") == "TRANSIT":
        return f"이동 중 {p.get('from_place_id') or p.get('from') or '?'}→{p.get('to_place_id') or p.get('to') or '?'}"
    return p.get("place_id") or "?"


def _info(definition, post: dict, iid: str) -> dict:
    # 읽기 전용 조회(H04 정보 모듈은 부르지 않는다 — 저작 정의 → 런타임 정의 순, information.resolve_definition과 같은 순서)
    d = (definition.get("information_definitions") or {}).get(iid) \
        or ((post.get("information_state") or {}).get("runtime_definitions") or {}).get(iid) or {}
    return d if isinstance(d, dict) else {}


def _summary(definition, post, iid) -> str:
    d = _info(definition, post, iid)
    s = d.get("summary") or d.get("proposition") or iid
    return s if len(s) <= SUMMARY_MAX else s[:SUMMARY_MAX] + "…"


def _holder(definition, world, h: str) -> str:
    return _actor(definition, world, h.split(":", 1)[1]) if h.startswith("actor:") else h


def info_changes(definition, pre: dict, post: dict) -> list:
    """[(holder 표시, InfoId, 인지 상태)] 새로 생긴·바뀐 인지 + 정보 상태 변화 [("정보", InfoId, 변화)]."""
    out = []
    pc, qc = pre.get("cognition_state") or {}, post.get("cognition_state") or {}
    for h in sorted(qc):
        for iid, rec in sorted((qc.get(h) or {}).items()):
            old = (pc.get(h) or {}).get(iid)
            if old != rec:
                aw = (rec or {}).get("awareness") if isinstance(rec, dict) else rec
                out.append((_holder(definition, post, h), iid, ("새 인지 " if old is None else "인지 변경 ") + str(aw)))
    pi, qi = ({k: dict((st or {}).get(k) or {}) for k in ("runtime_definitions", "status")}
              for st in (pre.get("information_state"), post.get("information_state")))
    for iid in sorted(set(qi["runtime_definitions"]) - set(pi["runtime_definitions"]), key=str):
        out.append(("정보", iid, "새 정보(런타임)"))
    for iid in sorted(set(pi["status"]) | set(qi["status"]), key=str):
        o, n = pi["status"].get(iid) or {}, qi["status"].get(iid) or {}
        if o.get("validity") != n.get("validity"):
            out.append(("정보", iid, f"유효성 {o.get('validity') or 'CURRENT'}→{n.get('validity') or 'CURRENT'}"))
    return out


def delta_sections(session, record) -> tuple:
    """(섹션 목록, 비밀 수) — 이력 레코드의 pre/post 정본 차이."""
    pre, post = (record or {}).get("pre") or {}, (record or {}).get("post") or {}
    if not pre and not post:
        return [("—", ["이력 레코드 없음 — 델타를 재구성할 수 없습니다"])], 0
    D = _definition(session)
    secs = []

    # 시간
    a, at = _clock(D, pre.get("world_clock"))
    b, bt = _clock(D, post.get("world_clock"))
    if pre.get("world_clock") != post.get("world_clock"):
        secs.append(("🕰️ 시간", [f"{at} → {bt}" + (f" (+{b - a}분)" if a is not None and b is not None else "")]))
    elif pre.get("world_timeline") != post.get("world_timeline"):
        secs.append(("🕰️ 시간", [f"{_j(pre.get('world_timeline'))} → {_j(post.get('world_timeline'))}"]))

    # 장소·인물 배치 (장면 = 이번 턴 뒤 PC가 있는 곳, 그 밖은 [원격])
    pp = (pre.get("spatial_state") or {}).get("placements") or {}
    qp = (post.get("spatial_state") or {}).get("placements") or {}
    pc_ids = [a_ for a_, r in ((post.get("actor_registry") or {}).items()) if isinstance(r, dict) and r.get("kind") == "PC"]
    scene = (post.get("spatial_state") or {}).get("scene_place")
    place_lines, actor_lines = [], []
    if (pre.get("spatial_state") or {}).get("scene_place") != scene:
        place_lines.append(f"장면 {(pre.get('spatial_state') or {}).get('scene_place') or '—'} → {scene or '—'}")
    for aid in sorted(set(pp) | set(qp)):
        if pp.get(aid) == qp.get(aid):
            continue
        line = f"{_actor(D, post, aid)}: {_place(pp.get(aid))} → {_place(qp.get(aid)) if aid in qp else '배치 해제'}"
        if aid in pc_ids or aid.startswith("pc_"):
            place_lines.append("PC " + line)
        else:
            here = scene and scene in (_place(pp.get(aid)), _place(qp.get(aid)))
            actor_lines.append(("" if here else "[원격] ") + line)
    for aid in sorted(set(post.get("actor_registry") or {}) - set(pre.get("actor_registry") or {})):
        actor_lines.append(f"새 등록 {_actor(D, post, aid)} ({aid})")
    if pre.get("visited_places") != post.get("visited_places"):
        new = [p for p in (post.get("visited_places") or []) if p not in (pre.get("visited_places") or [])]
        if new:
            place_lines.append("첫 방문 " + "·".join(map(str, new)))
    if pre.get("spatial_knowledge") != post.get("spatial_knowledge"):
        place_lines.append("공간 지식 갱신")
    if pre.get("companions") != post.get("companions"):
        actor_lines.append(f"동행 {_j(pre.get('companions'))} → {_j(post.get('companions'))}")
    new_met = [n for n in (post.get("met_npcs") or []) if n not in (pre.get("met_npcs") or [])]
    if new_met:
        actor_lines.append("처음 만남 " + "·".join(map(str, new_met)))
    if pre.get("relationship_state") != post.get("relationship_state"):
        actor_lines.append("관계 변화 " + _j(_diff(pre.get("relationship_state"), post.get("relationship_state"))))
    if place_lines:
        secs.append(("📍 장소", place_lines))
    if actor_lines:
        secs.append(("🧍 인물", actor_lines))

    # 사건
    pe, qe = pre.get("event_instances") or {}, post.get("event_instances") or {}
    tmpl = D.get("event_templates") or {}
    ev = []
    for iid in sorted(set(pe) | set(qe)):
        o, n = pe.get(iid) or {}, qe.get(iid) or {}
        if o == n:
            continue
        tid = n.get("template_id") or o.get("template_id") or "?"
        name = (tmpl.get(tid) or {}).get("title") or tid
        bits = []
        if not o:
            bits.append("생성")
        if o.get("lifecycle") != n.get("lifecycle") and o:
            bits.append(f"{o.get('lifecycle')}→{n.get('lifecycle')}")
        if o.get("current_phase") != n.get("current_phase"):
            bits.append(f"국면 {o.get('current_phase') or '—'}→{n.get('current_phase') or '—'}")
        rest = _diff({k: v for k, v in o.items() if k not in ("lifecycle", "current_phase", "phase_entered_at")},
                     {k: v for k, v in n.items() if k not in ("lifecycle", "current_phase", "phase_entered_at")})
        if rest and o:
            bits.append(_j(rest))
        ev.append(f"{name}: {' · '.join(bits) or '변경'}")
    if ev:
        secs.append(("📜 사건", ev))
    ps, qs = pre.get("world_schedule") or {}, post.get("world_schedule") or {}
    if ps != qs:
        secs.append(("⏳ 예약", [f"추가 {len(set(qs) - set(ps))} · 소진/해제 {len(set(ps) - set(qs))} · 대기 {len(qs)}"]))

    # 비밀·정보 (요약만 — 원문은 버튼)
    ic = info_changes(D, pre, post)
    if ic:
        secs.append(("🔒 비밀·정보 (요약)", [f"{h} · {why} — {_summary(D, post, iid)}" for h, iid, why in ic]))

    # 물건·접근·상태
    items = []
    pr, qr = pre.get("resources") or {}, post.get("resources") or {}
    for owner in sorted(set(pr) | set(qr), key=str):
        o, n = pr.get(owner) or {}, qr.get(owner) or {}
        if o != n and isinstance(o, dict) and isinstance(n, dict):
            ch = [f"{k} {o.get(k, 0)}→{n.get(k, 0)}" for k in sorted(set(o) | set(n), key=str) if o.get(k) != n.get(k)]
            items.append(f"{owner}: " + ", ".join(ch))
    for fld, label in (("place_items", "현장 물건"), ("item_instances", "특수 물건")):
        if pre.get(fld) != post.get(fld):
            items.append(f"{label} " + _j(_diff(pre.get(fld), post.get(fld))))
    if items:
        secs.append(("🎒 물건", items))
    if pre.get("access_control_state") != post.get("access_control_state"):
        secs.append(("🚪 접근", [_j(_diff(pre.get("access_control_state"), post.get("access_control_state")))]))
    if pre.get("statuses") != post.get("statuses"):
        secs.append(("🩹 상태", [_j(_diff(pre.get("statuses"), post.get("statuses")))]))
    if pre.get("world_pending_signals") != post.get("world_pending_signals"):
        secs.append(("📨 전달 대기 신호", [f"{len(pre.get('world_pending_signals') or [])} → "
                                          f"{len(post.get('world_pending_signals') or [])}건"]))
    if pre.get("quest_state") != post.get("quest_state"):
        secs.append(("🗺️ 퀘스트(LEGACY)", [_j(_diff(pre.get("quest_state"), post.get("quest_state")))]))

    covered = {"world_clock", "world_timeline", "spatial_state", "actor_registry", "visited_places", "spatial_knowledge",
               "companions", "met_npcs", "relationship_state", "event_instances", "world_schedule", "cognition_state",
               "information_state", "resources", "place_items", "item_instances", "access_control_state", "statuses",
               "world_pending_signals", "quest_state"}
    other = sorted(k for k in set(pre) | set(post) if k not in covered and k not in _NOISE and pre.get(k) != post.get(k))
    if other:
        secs.append(("… 기타 변경 필드", [", ".join(other)]))
    if not secs:
        secs.append(("—", ["정본 변화 없음"]))
    return secs, len(ic)


def _diff(a, b):
    from .logs import state_diff
    return state_diff(a, b)


def secret_lines(session, record) -> list:
    """버튼이 보여 줄 원문 — 바뀐 인지·정보의 전체 명제."""
    pre, post = (record or {}).get("pre") or {}, (record or {}).get("post") or {}
    D = _definition(session)
    out = []
    for h, iid, why in info_changes(D, pre, post):
        d = _info(D, post, iid)
        out.append(f"**{h}** · {why} · `{iid}`\n{d.get('proposition') or d.get('summary') or '(명제 없음)'}")
    return out


# ══════════════════════════════════════════════════════════════
#  비밀 원문 버튼 (재시작 뒤에도 동작)
# ══════════════════════════════════════════════════════════════

class SecretButton(discord.ui.DynamicItem[discord.ui.Button], template=r"mrep:sec:(?P<sid>[^:]+):(?P<tx>[0-9a-f]+)"):
    def __init__(self, sid: str, tx: str):
        self.sid, self.tx = sid, tx
        super().__init__(discord.ui.Button(label="🔒 비밀 원문", style=discord.ButtonStyle.secondary,
                                           custom_id=f"mrep:sec:{sid}:{tx}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["sid"], match["tx"])

    async def callback(self, interaction):
        bot = interaction.client
        session = (getattr(bot, "active_sessions", None) or {}).get(self.sid)
        if session is None or getattr(interaction.channel, "id", None) != getattr(session, "master_ch_id", None):
            await interaction.response.send_message("이 세션의 마스터 채널에서만 열 수 있습니다.", ephemeral=True)
            return
        try:
            from .turn_history import load_record
            lines = secret_lines(session, load_record(self.sid, self.tx))
        except Exception as e:  # noqa: BLE001
            await interaction.response.send_message(f"이력 레코드를 읽지 못했습니다: {type(e).__name__}", ephemeral=True)
            return
        parts = _chunks(lines or ["바뀐 비밀이 없습니다."], limit=1900)
        await interaction.response.send_message(parts[0], ephemeral=True)
        for p in parts[1:]:
            await interaction.followup.send(p, ephemeral=True)


def secret_view(session_id: str, tx: str):
    v = discord.ui.View(timeout=None)
    v.add_item(SecretButton(session_id, tx))
    return v


# ══════════════════════════════════════════════════════════════
#  송출
# ══════════════════════════════════════════════════════════════

def build(session, *, transaction_id, cost_embed=None, cost_log=(), estimate=None, prep=None, record=None) -> tuple:
    """(임베드 목록, 비밀 수) — 비용 → 층위활동 → 세계 델타 순."""
    turn = getattr(session, "turn_count", "?")
    embeds = []
    cost_secs = (embed_sections(cost_embed) if cost_embed is not None else []) + cost_sections(cost_log, estimate)
    if cost_secs:
        embeds += pack(f"🎲 턴 비용 리포트 · #{turn}", COLOR_COST, cost_secs,
                       description=getattr(cost_embed, "description", None))
    act = activity_sections(prep, record)
    if act:
        embeds += pack(f"🧠 층위활동 · #{turn}", COLOR_ACTIVITY, act)
    dsecs, n_secret = delta_sections(session, record)
    embeds += pack(f"🌐 세계 델타 · #{turn}", COLOR_DELTA, dsecs,
                   description="정본 pre → post (장면 밖 = [원격])")
    return embeds, n_secret


async def send(channel, session, *, transaction_id, **kw) -> int:
    """전수보고를 메시지당 임베드 10개씩 보낸다. 비밀이 바뀐 턴은 마지막 메시지에 원문 버튼. 실패는 흡수."""
    record = kw.pop("record", None)
    if record is None and transaction_id:
        try:
            from .turn_history import load_record
            record = load_record(session.session_id, transaction_id)
        except Exception as e:  # noqa: BLE001
            print(f"[보고] 이력 레코드 읽기 실패(델타 생략): {e}")
    embeds, n_secret = build(session, transaction_id=transaction_id, record=record, **kw)
    sent = 0
    groups, cur, size = [], [], 0                  # 메시지당 임베드 10개 · 모든 임베드 글자 합 6000
    for e in embeds:
        if cur and (len(cur) >= EMBEDS_PER_MESSAGE or size + len(e) > EMBED_MAX):
            groups.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += len(e)
    if cur:
        groups.append(cur)
    for i, g in enumerate(groups):
        last = i == len(groups) - 1
        view = secret_view(session.session_id, transaction_id) if (last and n_secret and transaction_id) else None
        try:
            await channel.send(embeds=g, **({"view": view} if view else {}))
            sent += 1
        except Exception as e:  # noqa: BLE001
            print(f"[보고] 전수보고 송출 실패(커밋 유지): {type(e).__name__}: {e}")
    return sent
