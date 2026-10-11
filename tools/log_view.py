#!/usr/bin/env python3
"""구조 로그 열람 — 세션 폴더(sessions/{id}) 또는 !세션이력 zip을 받아 사람이 읽는 형태로 출력.

    python3 tools/log_view.py <세션폴더|zip>                  요약(파일별 건수·오류·층위별 호출 통계)
    python3 tools/log_view.py <src> --timeline [--turn N]    호출·이벤트·도메인·채팅을 시간순으로
    python3 tools/log_view.py <src> --calls [--layer L] [--turn N]
    python3 tools/log_view.py <src> --call ID                호출 1건의 요청·응답 원문 복원
    python3 tools/log_view.py <src> --events [--errors]
    python3 tools/log_view.py <src> --domain [KIND]          예: turn.delta, commit, cost.event, ink.charge, display
    python3 tools/log_view.py <src> --chat

원본을 바꾸지 않는다. 조각(#해시)은 blobs.jsonl에서 복원한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import zipfile
from collections import Counter, defaultdict

FILES = ("calls", "events", "domain", "chat", "blobs")


def load(src: str) -> dict:
    data = {k: [] for k in FILES}

    def take(name, text):
        base = os.path.basename(name)
        for k in FILES:
            if base == f"{k}.jsonl":
                for line in text.splitlines():
                    if line.strip():
                        try:
                            data[k].append(json.loads(line))
                        except Exception:
                            pass
    if os.path.isdir(src):
        d = os.path.join(src, "logs") if os.path.isdir(os.path.join(src, "logs")) else src
        for k in FILES:
            p = os.path.join(d, f"{k}.jsonl")
            if os.path.exists(p):
                take(p, open(p, encoding="utf-8").read())
    else:
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if "/logs/" in n or n.startswith("logs/"):
                    take(n, z.read(n).decode("utf-8"))
    data["blob_map"] = {b["h"]: b["t"] for b in data["blobs"]}
    return data


def piece(D, p) -> str:
    if isinstance(p, str) and p.startswith("#"):
        return D["blob_map"].get(p[1:], f"<조각 누락 {p}>")
    return "" if p is None else str(p)


def joined(D, parts) -> str:
    return "\n".join(piece(D, p) for p in parts or ())


def value(D, v):
    if isinstance(v, dict):
        if set(v) == {"$b"}:
            return piece(D, v["$b"])
        return {k: value(D, x) for k, x in v.items()}
    if isinstance(v, list):
        return [value(D, x) for x in v]
    return v


def ts(t) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t or 0))


def summary(D):
    print("== 파일별 건수")
    for k in FILES:
        print(f"  {k:7s} {len(D[k])}")
    errs = [e for e in D["events"] if e.get("lv") == "ERROR"]
    warns = [e for e in D["events"] if e.get("lv") == "WARN"]
    print(f"== 이벤트: ERROR {len(errs)} · WARN {len(warns)}")
    for e in errs[-10:]:
        print(f"  [{ts(e['ts'])}] {e.get('area')}: {e.get('msg')} {e.get('exc', '')}")
    print("== 호출(층위·작업별)")
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for c in D["calls"]:
        k = (c.get("layer"), c.get("op"))
        a = agg[k]
        u = c.get("usage") or {}
        a[0] += 1
        a[1] += c.get("status") != "ok"
        a[2] += c.get("ms") or 0
        a[3] += u.get("in", 0)
        a[4] += u.get("cached", 0)
        a[5] += u.get("out", 0) + u.get("thought", 0)
    for (layer, op), a in sorted(agg.items(), key=lambda x: -x[1][0]):
        print(f"  {layer}/{op}: {a[0]}건 실패 {a[1]} · 평균 {a[2] // max(a[0], 1)}ms · "
              f"입력 {a[3]} (캐시 {a[4]}) · 출력+사고 {a[5]}")
    kinds = Counter(d.get("kind") for d in D["domain"])
    print("== 도메인 사실: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
    turns = sorted({d.get("turn") for d in D["domain"] if d.get("turn") is not None})
    print(f"== 턴: {turns}")


def show_call(D, c, full=False):
    u = c.get("usage") or {}
    print(f"[{ts(c['ts'])}] call={c.get('call')} {c.get('layer')}/{c.get('op')} turn={c.get('turn')} "
          f"attempt={c.get('attempt')} {c.get('status')} {c.get('ms')}ms finish={c.get('finish')} "
          f"usage={u} cache={'Y' if c.get('cache') else 'N'}" + (f" exc={c.get('exc')}" if c.get("exc") else ""))
    if full:
        if c.get("system"):
            print("--- system\n" + piece(D, c["system"]))
        for i, m in enumerate(c.get("req") or ()):
            body = "\n".join(joined(D, p) for p in m.get("parts") or ())
            print(f"--- req[{i}] {m.get('role')}\n{body}")
        print("--- resp\n" + joined(D, c.get("resp")))


def timeline(D, turn=None):
    rows = []
    for c in D["calls"]:
        rows.append((c["ts"], "CALL", f"{c.get('layer')}/{c.get('op')} {c.get('status')} {c.get('ms')}ms "
                                       f"{(c.get('usage') or {})}", c.get("turn")))
    for e in D["events"]:
        rows.append((e["ts"], e.get("lv", "INFO"), f"{e.get('area')}: {e.get('msg')} {e.get('exc', '')}", e.get("turn")))
    for d in D["domain"]:
        brief = json.dumps(value(D, d.get("data")), ensure_ascii=False)[:300]
        rows.append((d["ts"], "DATA", f"{d.get('kind')} {brief}", d.get("turn")))
    for m in D["chat"]:
        text = joined(D, m["text"]) if isinstance(m.get("text"), list) else m.get("text", "")
        rows.append((m["ts"], "CHAT", f"[{m.get('ch')}] {m.get('who')}: {text[:200]}", m.get("turn")))
    for t, kind, text, tn in sorted(rows, key=lambda r: r[0]):
        if turn is None or tn == turn:
            print(f"[{ts(t)}] {kind:5s} t{tn if tn is not None else '-'} {text}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("--timeline", action="store_true")
    ap.add_argument("--calls", action="store_true")
    ap.add_argument("--call")
    ap.add_argument("--layer")
    ap.add_argument("--turn", type=int)
    ap.add_argument("--events", action="store_true")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--domain", nargs="?", const="")
    ap.add_argument("--chat", action="store_true")
    a = ap.parse_args(argv)
    D = load(a.src)
    if a.call:
        for c in D["calls"]:
            if c.get("call") == a.call:
                show_call(D, c, full=True)
        return 0
    if a.timeline:
        timeline(D, a.turn)
    elif a.calls:
        for c in D["calls"]:
            if (a.layer is None or c.get("layer") == a.layer) and (a.turn is None or c.get("turn") == a.turn):
                show_call(D, c)
    elif a.events or a.errors:
        for e in D["events"]:
            if not a.errors or e.get("lv") == "ERROR":
                print(f"[{ts(e['ts'])}] {e.get('lv')} {e.get('area')}: {e.get('msg')} {e.get('exc', '')}")
                if e.get("data"):
                    print("    " + json.dumps(value(D, e["data"]), ensure_ascii=False)[:2000])
                if e.get("trace"):
                    print(e["trace"])
    elif a.domain is not None:
        for d in D["domain"]:
            if not a.domain or d.get("kind") == a.domain:
                if a.turn is None or d.get("turn") == a.turn:
                    print(f"[{ts(d['ts'])}] {d.get('kind')} t{d.get('turn')}")
                    print("    " + json.dumps(value(D, d.get("data")), ensure_ascii=False, indent=1)[:6000])
    elif a.chat:
        for m in D["chat"]:
            text = joined(D, m["text"]) if isinstance(m.get("text"), list) else m.get("text", "")
            print(f"[{ts(m['ts'])}] [{m.get('ch')}] {m.get('who')}: {text}")
    else:
        summary(D)
    return 0


if __name__ == "__main__":
    sys.exit(main())
