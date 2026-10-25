#!/usr/bin/env python3
"""시나리오 정의 검사·컴파일·이행 보고 (H01).

원본 시나리오를 읽기만 한다. 어떤 경우에도 scenarios/ 아래 파일을 고치지 않는다
(silent fix 금지 — DESIGN 08 §50). 출력은 지정한 경로에만 쓴다.

사용:
    python3 tools/scenario_check.py 영도                 # 요약·진단·capability 표
    python3 tools/scenario_check.py --all                # 모든 시나리오
    python3 tools/scenario_check.py 영도 --verbose       # INFO 진단까지
    python3 tools/scenario_check.py 영도 --json          # 기계 판독용 JSON
    python3 tools/scenario_check.py 영도 --compile-out /tmp/영도.compiled.json
    python3 tools/scenario_check.py 영도 --migration-report -          # 마크다운 → stdout
    python3 tools/scenario_check.py 영도 --migration-report report.json
    python3 tools/scenario_check.py --verify-snapshot <session_id>     # sessions/{id} 스냅샷 검증

종료 코드: 0 = ERROR 없음 · 1 = ERROR 있음(또는 스냅샷 검증 실패) · 2 = 사용법 오류
"""
import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import definition_compiler as dc  # noqa: E402
from core import definition_snapshot as ds  # noqa: E402
from core import speaker_card as sc  # noqa: E402


def _resolve_sources(arg: str, scenarios_dir: pathlib.Path) -> dc.ScenarioSources:
    p = pathlib.Path(arg)
    if arg.endswith(".json") and p.is_file():
        return dc.load_scenario_sources_from_path(str(p))
    return dc.load_scenario_sources_from_path(str(scenarios_dir / f"{arg}.json"))


def _all_ids(scenarios_dir: pathlib.Path) -> list:
    return [f.name[:-len(".json")] for f in sorted(scenarios_dir.glob("*.json"))
            if not f.name.endswith(dc.QUEST_SIDECAR_SUFFIX)]


def _guard_output(out: pathlib.Path, scenarios_dir: pathlib.Path):
    """출력 경로가 원본 시나리오 디렉터리를 가리키면 거부한다."""
    target = out.resolve()
    sdir = scenarios_dir.resolve()
    if target == sdir or sdir in target.parents:
        raise SystemExit(f"출력 경로가 시나리오 디렉터리 안입니다(원본 보호): {out}")


def _print_report(res: dc.CompileResult, verbose: bool):
    s = dc.summarize(res)
    print(f"== {s['scenario_id']}  (compiler v{s['compiler_version']})")
    print(f"   revision : {s['revision'] or '— (ERROR로 정의 없음)'}")
    print(f"   진단     : ERROR {s['counts']['ERROR']} · WARNING {s['counts']['WARNING']}"
          f" · INFO {s['counts']['INFO']}")
    if res.ok:
        print(f"   모드     : {s['schema_mode']}")
        print("   규모     : " + " · ".join(f"{k} {v}" for k, v in s["sizes"].items()))
    print("   capability:")
    for name, state in dc.capability_matrix(res).items():
        print(f"     {name:28s} {state}")
    for d in res.diagnostics:
        if d.severity == dc.INFO and not verbose:
            continue
        print(f"   [{d.severity}] {d.code} @ {d.path} — {d.message}")


def _migration_markdown(rep: dict) -> str:
    pl, ac, qu = rep["places"], rep["actors"], rep["quests"]
    L = [f"# 이행 보고 — {rep['scenario_id']}", "",
         f"- compiler v{rep['compiler_version']} · 분석 전용(원본 미수정)",
         f"- capability 모드: {rep['capabilities']['mode']}", "",
         "## 사람이 저작·분류해야 할 것", ""]
    L += [f"- {x}" for x in rep["human_authoring_required"]]
    L += ["", "## 장소", "",
          f"- 장소 {pl['count']} · 저작된 이동 구간 {pl['authored_movement_edges']}"
          f" · 거리 미저작 connected 쌍 {pl['connected_pairs_without_authored_distance']}", "",
          f"### 비대칭 connected ({len(pl['asymmetric_connected'])}) — A→B만 있음", ""]
    L += [f"- {a} → {b}" for a, b in pl["asymmetric_connected"]] or ["- 없음"]
    L += ["", f"### 암묵 상하위 이동 후보 ({len(pl['implicit_parent_child_routes'])}) — 하위 → 상위", ""]
    L += [f"- {c} → {p}" for c, p in pl["implicit_parent_child_routes"]] or ["- 없음"]
    L += ["", f"### 별칭 문제 ({len(pl['alias_issues'])})", ""]
    L += [f"- '{x['alias']}': 소유 {', '.join(x['owners'])}"
          + (" · 같은 이름의 장소 키가 있어 별칭이 가려짐" if x["shadowed_by_place_key"] else "")
          for x in pl["alias_issues"]] or ["- 없음"]
    assoc = pl["static_npc_associations_not_presence"]
    L += ["", f"### 장소 연관 인물(현재 위치 아님) — {len(assoc)}곳"
          f" · {sum(len(v) for v in assoc.values())}건", ""]
    L += ["", "## 인물", "",
          f"- 저작 NPC {ac['count']} · 인라인 비밀 → InfoDefinition {ac['inline_secrets_compiled']}", "",
          "| 사전 키 | ActorId | 명시 id |", "|---|---|---|"]
    L += [f"| {r['legacy_key']} | `{r['actor_id']}` | {'예' if r['explicit_id'] else '-'} |"
          for r in ac["id_table"]]
    L += ["", "## 퀘스트", "",
          f"- 부속 파일: {'있음' if qu['present'] else '없음'} · 퀘스트 {qu['count']}"
          f" · hidden.truth → InfoDefinition 후보 {len(qu['hidden_truth_info_candidates'])}",
          f"- 사건 원형 이행: {qu['event_template_conversion']}", "",
          "## 시작 틀", ""]
    L += [f"- `{f['frame_id']}` 슬롯 {f['slot_sources']} · facts 템플릿 키 {f['fact_template_keys']}"
          for f in rep["start_frames"]] or ["- 없음"]
    L += ["", "## 진단", ""]
    L += [f"- [{d['severity']}] {d['code']} @ {d['path']} — {d['message']}"
          for d in rep["diagnostics"]] or ["- 없음"]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="시나리오 정의 검사·컴파일·이행 보고(원본 미수정)")
    ap.add_argument("scenarios", nargs="*", help="시나리오 id 또는 .json 경로")
    ap.add_argument("--all", action="store_true", help="시나리오 디렉터리의 모든 시나리오")
    ap.add_argument("--scenarios-dir", default=str(ROOT / "scenarios"))
    ap.add_argument("--json", action="store_true", help="요약·진단을 JSON으로 출력")
    ap.add_argument("--verbose", action="store_true", help="INFO 진단도 출력")
    ap.add_argument("--compile-out", metavar="PATH", help="컴파일된 정의를 PATH에 기록(시나리오 1개)")
    ap.add_argument("--migration-report", metavar="PATH",
                    help="이행 보고를 PATH에 기록('-'=stdout 마크다운, .json이면 JSON)")
    ap.add_argument("--verify-snapshot", metavar="SESSION_ID",
                    help="sessions/{id}/definition_snapshot.json 검증(현재 디렉터리 기준)")
    args = ap.parse_args(argv)

    if args.verify_snapshot:
        rep = ds.verify_snapshot(args.verify_snapshot)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    sdir = pathlib.Path(args.scenarios_dir)
    ids = _all_ids(sdir) if args.all else list(args.scenarios)
    if not ids:
        ap.print_usage()
        return 2
    if (args.compile_out or args.migration_report) and len(ids) != 1:
        print("--compile-out/--migration-report는 시나리오 하나에만 쓸 수 있습니다", file=sys.stderr)
        return 2

    worst = 0
    summaries = []
    for sid in ids:
        try:
            src = _resolve_sources(sid, sdir)
        except dc.ScenarioSourceError as e:
            print(f"[ERROR] {sid}: {e}", file=sys.stderr)
            worst = 1
            continue
        res = dc.compile_sources(src)
        if not res.ok:
            worst = 1
        if args.json:
            summaries.append(dict(dc.summarize(res),
                                  capability_matrix=dc.capability_matrix(res),
                                  source=src.provenance,
                                  diagnostics=[d.to_dict() for d in res.diagnostics]))
        else:
            _print_report(res, args.verbose)

        # SVG 화자 카드 — 독립 검사(컴파일러 ERROR가 아니다: 카드 오류로 DYNAMIC 후보가 LEGACY로 강등되지 않는다)
        card_diags = sc.check_card_definition(src.raw, definition=res.definition,
                                              base=str(ROOT / "media"), scenario_id=src.scenario_id)
        if card_diags:
            worst = 1
            if args.json:
                summaries[-1]["speaker_card_diagnostics"] = [
                    {"severity": a, "code": b, "path": c, "message": d} for a, b, c, d in card_diags]
            else:
                print(f"   speaker_card: ERROR {len(card_diags)}")
                for sev, code, path, msg in card_diags:
                    print(f"     [{sev}] {code} {path}: {msg}")

        # v7.15 캐릭터 생성 단계(profile_creation·질문형 가이드) — 독립 검사(정의 없음은 오류가 아니다)
        if (src.raw or {}).get("profile_creation") is not None:
            from core import profile_gen
            pc_errs = profile_gen.validate_steps(src.raw)
            if pc_errs:
                worst = 1
                if args.json:
                    summaries[-1]["profile_creation_errors"] = pc_errs
                else:
                    print(f"   profile_creation: ERROR {len(pc_errs)}")
                    for m in pc_errs:
                        print(f"     {m}")

        # v7.36 대화 박자 표기([[답1|답2]]) — 시나리오 소개문
        talk_errs = __import__("core.talk", fromlist=["check"]).check((src.raw or {}).get("scenario_intro") or "")
        if talk_errs:
            worst = 1
            if args.json:
                summaries[-1]["talk_errors"] = talk_errs
            else:
                print(f"   scenario_intro 박자 표기: ERROR {len(talk_errs)}")
                for m in talk_errs:
                    print(f"     {m}")

        # v7.38 상태이상 규칙(rules·excludes·intake_groups — 공용 + 시나리오 병합본) · 인물 소개 박자 구조
        from core import status_rules as _sr
        from core.utils import get_merged_status_effects
        st_errs = _sr.check_definitions(src.raw, get_merged_status_effects(src.raw or {}))
        bf = (src.raw or {}).get("briefing_formats")
        if isinstance(bf, dict):
            from core.start_frame import BRIEFING_BEATS
            for k, v in bf.items():
                if k not in BRIEFING_BEATS or not isinstance(v, list) or not all(isinstance(x, str) and x.strip()
                                                                                 for x in v):
                    st_errs.append(("BRIEFING_FORMATS", f"{k}: 박자는 {BRIEFING_BEATS} 중 하나, 값은 문자열 목록"))
        if st_errs:
            worst = 1
            if args.json:
                summaries[-1]["status_errors"] = [{"code": c, "message": m} for c, m in st_errs]
            else:
                print(f"   상태이상·소개 박자: ERROR {len(st_errs)}")
                for c, m in st_errs:
                    print(f"     [{c}] {m}")

        if args.compile_out:
            if not res.ok:
                print("ERROR가 있어 정의를 기록하지 않습니다", file=sys.stderr)
            else:
                out = pathlib.Path(args.compile_out)
                _guard_output(out, sdir)
                env = ds.build_envelope(res.definition, source=src.provenance,
                                        compiler_version=res.compiler_version)
                env.pop("written_at", None)       # 같은 입력 → 같은 출력 파일
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(env, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                if not args.json:
                    print(f"   compiled → {out}")

        if args.migration_report:
            rep = dc.migration_report(src)
            if args.migration_report == "-":
                sys.stdout.write(_migration_markdown(rep))
            else:
                out = pathlib.Path(args.migration_report)
                _guard_output(out, sdir)
                out.parent.mkdir(parents=True, exist_ok=True)
                if out.suffix == ".json":
                    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else:
                    out.write_text(_migration_markdown(rep), encoding="utf-8")
                if not args.json:
                    print(f"   migration report → {out}")

    if args.json:
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
