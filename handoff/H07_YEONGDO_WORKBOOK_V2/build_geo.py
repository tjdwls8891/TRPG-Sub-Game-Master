# -*- coding: utf-8 -*-
"""워크북 → movement_edges·places 추가분(dict). 원본은 쓰지 않는다(apply.py가 쓴다)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import geo  # noqa: E402

SPEED = 48   # walk m/min — 은밀 기동(세계관 1.3절 거점 간 시간으로 보정, CALIBRATED)


def load(root):
    return json.load(open(os.path.join(root, "scenarios/영도.json"), encoding="utf-8"))


def pairs(raw):
    P = raw["places"]
    out = {}
    for a, v in P.items():
        cs = v.get("connected") or []
        cs = [cs] if isinstance(cs, str) else cs
        for b in cs:
            if b in P:
                out.setdefault(tuple(sorted((a, b))), set()).add(a)
    return out


def build(raw):
    rows, ledger, seen = [], [], set()
    n = 0

    def add(a, b, terrain, note, source):
        nonlocal n
        key = tuple(sorted((a, b)))
        if key in seen:
            return
        seen.add(key)
        n += 1
        dist, factor, dh = geo.edge_metrics(a, b, terrain)
        _, rfactor, _ = geo.edge_metrics(b, a, terrain)
        hint = f"{geo.bearing_ko(a, b)}쪽 · {geo.slope_word(dh, dist)} · {geo.TAG_KO[terrain]}"
        rhint = f"{geo.bearing_ko(b, a)}쪽 · {geo.slope_word(-dh, dist)} · {geo.TAG_KO[terrain]}"
        eid = f"yd.e{n:03d}"
        e = {"id": eid, "from": a, "to": b, "bidirectional": True, "distance_m": dist,
             "movement_tags": [terrain], "base_time_factor": factor, "route_hint": hint}
        acc = geo.ACCESS_BY_PAIR.get(key)
        if acc:
            e["access_ref"] = acc
        dl = geo.FIXED_DELAY.get(key)
        if dl:
            e["fixed_delay_minutes"] = dl
        vis = "RESTRICTED" if key in geo.RESTRICTED_EDGES else (
            "PUBLIC" if a in geo.PUBLIC_PLACES and b in geo.PUBLIC_PLACES else "LOCAL")
        if vis != "LOCAL":
            e["knowledge_visibility"] = vis
        rev = {"route_hint": rhint}
        if rfactor != factor:
            rev["base_time_factor"] = rfactor
        e["reverse"] = rev
        rows.append(e)
        ledger.append({"id": eid, "a": a, "b": b, "source": source, "terrain": terrain, "dist": dist,
                       "f_fwd": factor, "f_rev": rfactor, "access": acc, "delay": dl, "vis": vis, "note": note,
                       "t_fwd": round(dist * factor / SPEED + (dl or 0), 1),
                       "t_rev": round(dist * rfactor / SPEED + (dl or 0), 1)})

    decisions = []
    for key, who in sorted(pairs(raw).items()):
        a, b = key
        dec = geo.D.get(key)
        sym = "SYM" if len(who) == 2 else f"ASYM({next(iter(who))}→)"
        if dec is None:
            t = geo.auto_terrain(a, b)
            add(a, b, t, "", "CONNECTED")
            decisions.append((a, b, sym, "BI", t, "원본 connected 양방향/부모-자식 — 그대로 양방향"))
        elif dec[0] == "BI":
            t = dec[1] or geo.auto_terrain(a, b)
            add(a, b, t, dec[2], "CONNECTED")
            decisions.append((a, b, sym, "BI", t, dec[2] or "그대로 양방향"))
        else:
            decisions.append((a, b, sym, dec[0], "-", dec[2]))
    for a, b, t, note in geo.NEW_EDGES:
        add(a, b, t, note, "NEW")
    return rows, ledger, decisions


if __name__ == "__main__":
    root = sys.argv[1]
    raw = load(root)
    for k, v in geo.NEW_PLACES.items():
        raw["places"][k] = v
    rows, ledger, decisions = build(raw)
    missing = [p for p in raw["places"] if p not in geo.C and p != "영도"]
    print("edges", len(rows), "decisions", len(decisions), "missing coords", missing)
    from collections import Counter
    print(Counter(d[3] for d in decisions))
    json.dump({"rows": rows, "ledger": ledger, "decisions": decisions},
              open(os.path.join(os.path.dirname(__file__), "geo_out.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
