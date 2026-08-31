#!/usr/bin/env python3
"""문서 수치 정합 검증.

CLAUDE.md·DEVLOG.md에 적힌 수치가 실제 코드·데이터와 일치하는지 대조한다.

문서를 사람이 손으로 갱신하면 반드시 낡는다. 실제로 두 문서가 v1.x
시절 수치를 그대로 달고 있었다(core 서브모듈 10개 → 실제 44개).
원칙을 지키려는 의지에 기대지 않고 커밋 전에 기계가 잡아내게 한다.

사용:
    python3 tools/verify_docs.py            # 검사만
    python3 tools/verify_docs.py --fix      # 불일치를 문서에 자동 반영
"""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import core  # noqa: E402


def collect() -> dict:
    """실제 값을 수집한다. 여기가 유일한 진실 공급원이다."""
    yeongdo = json.loads((ROOT / "scenarios/영도.json").read_text(encoding="utf-8"))
    quests = json.loads((ROOT / "scenarios/영도.quests.json").read_text(encoding="utf-8"))

    py_lines = sum(
        len(f.read_text(encoding="utf-8").splitlines())
        for f in ROOT.rglob("*.py")
        if ".git" not in f.parts and "tools" not in f.parts
    )

    return {
        "version": core.__version__,
        "schema_version": core.SCHEMA_VERSION,
        "session_fields": len(core.SESSION_FIELDS),
        "tracked_paths": len(core.TRACKED_PATHS),
        "core_modules": len(list((ROOT / "core").glob("*.py"))),
        "cog_modules": len([f for f in (ROOT / "cogs").glob("*.py")
                            if not f.name.startswith("__")]),
        "total_lines": py_lines,
        "places": len(yeongdo.get("places", {})),
        "npcs": len(yeongdo.get("default_npcs", {})),
        "jobs": len(yeongdo.get("jobs", {})),
        "start_frames": len(yeongdo.get("start_frames", [])),
        "profile_steps": len(yeongdo.get("profile_creation", [])),
        "briefings": len(yeongdo.get("briefing_formats", [])),
        "quests": len(quests.get("quests", [])),
    }


# 문서에서 값을 찾아낼 패턴.
#   (파일, 정규식, 값 키, 설명)
# 정규식은 그룹 1이 수치여야 한다.
PATTERNS = [
    ("CLAUDE.md", r"현재 버전: \*\*v([\d.]+)\*\*", "version", "버전"),
    ("CLAUDE.md", r"`SCHEMA_VERSION` (\d+) · 총", "schema_version", "SCHEMA_VERSION"),
    ("CLAUDE.md", r"`SESSION_FIELDS`\((\d+)\)", "session_fields", "SESSION_FIELDS"),
    ("CLAUDE.md", r"`TRACKED_PATHS` (\d+)개", "tracked_paths", "TRACKED_PATHS"),
    ("CLAUDE.md", r"### core/ — (\d+)개 서브모듈", "core_modules", "core 서브모듈"),
    ("CLAUDE.md", r"\| `places` \| (\d+) \|", "places", "장소"),
    ("CLAUDE.md", r"\| `default_npcs` \| (\d+) \|", "npcs", "NPC"),
    ("CLAUDE.md", r"\| `jobs` \| (\d+) \|", "jobs", "직업"),
    ("CLAUDE.md", r"\| `start_frames` \| (\d+) \|", "start_frames", "시작 틀"),
    ("CLAUDE.md", r"\| `profile_creation` \| (\d+)단계 \|", "profile_steps", "프로필 단계"),
    ("CLAUDE.md", r"\| `briefing_formats` \| (\d+) \|", "briefings", "브리핑 양식"),
    ("CLAUDE.md", r"영도\.quests\.json`\) \| (\d+) \|", "quests", "퀘스트"),
    ("DEVLOG.md", r"현재 버전: \*\*v([\d.]+)\*\*", "version", "버전"),
    ("DEVLOG.md", r"`SCHEMA_VERSION` (\d+)", "schema_version", "SCHEMA_VERSION"),
    ("DEVLOG.md", r"장소 (\d+) · NPC", "places", "장소"),
    ("DEVLOG.md", r"NPC (\d+)\(", "npcs", "NPC"),
    ("DEVLOG.md", r"직업 (\d+) · 퀘스트", "jobs", "직업"),
    ("DEVLOG.md", r"퀘스트 (\d+)\n", "quests", "퀘스트"),
    ("DEVLOG.md", r"시작 틀 (\d+) ·", "start_frames", "시작 틀"),
    ("DEVLOG.md", r"프로필 단계 (\d+) ·", "profile_steps", "프로필 단계"),
    ("DEVLOG.md", r"SESSION_FIELDS (\d+) ·", "session_fields", "SESSION_FIELDS"),
    ("DEVLOG.md", r"TRACKED_PATHS (\d+)", "tracked_paths", "TRACKED_PATHS"),
]


def check(fix: bool = False) -> int:
    actual = collect()
    problems = []
    fixed = 0
    cache = {}

    for fname, pattern, key, label in PATTERNS:
        path = ROOT / fname
        if fname not in cache:
            cache[fname] = path.read_text(encoding="utf-8")
        text = cache[fname]

        m = re.search(pattern, text)
        if not m:
            problems.append(f"  {fname}: '{label}' 표기를 찾지 못함 (패턴 변경?)")
            continue

        want = str(actual[key])
        got = m.group(1)
        if got == want:
            continue

        if fix:
            start, end = m.span(1)
            cache[fname] = text[:start] + want + text[end:]
            fixed += 1
            print(f"  고침 {fname}: {label} {got} → {want}")
        else:
            problems.append(f"  {fname}: {label} 문서 {got} ≠ 실제 {want}")

    if fix and fixed:
        for fname, text in cache.items():
            (ROOT / fname).write_text(text, encoding="utf-8")

    if problems:
        print("문서 수치 불일치:")
        for p in problems:
            print(p)
        print("\n`python3 tools/verify_docs.py --fix`로 자동 반영할 수 있습니다.")
        return 1

    print(f"문서 수치 OK ({len(PATTERNS)}항목)" + (f" · {fixed}건 수정" if fixed else ""))
    return 0


def show():
    """실제 값을 출력한다. 문서 작성 시 참고용."""
    for k, v in collect().items():
        print(f"  {k:16s} {v}")


if __name__ == "__main__":
    if "--show" in sys.argv:
        show()
    else:
        sys.exit(check(fix="--fix" in sys.argv))
