"""`!명령어` 안내가 실제 등록 명령어와 어긋나지 않는다(누락 방지)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _help_body():
    s = (ROOT / "cogs" / "system.py").read_text(encoding="utf-8")
    return s[s.index("async def show_commands"):s.index('@commands.command(name="채널정리")')]


def test_every_registered_command_is_listed_in_help():
    body = _help_body()
    missing = []
    for f in sorted((ROOT / "cogs").glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for name in re.findall(r'@commands\.command\(name="([^"]+)"', src):
            if name != "명령어" and f"!{name}" not in body:
                missing.append(name)
        for name in re.findall(r'@auto\.command\(name="([^"]+)"', src):
            if f"!자동 {name}" not in body:
                missing.append(f"자동 {name}")
    assert missing == []


def test_help_no_stale_tts_scope():
    assert "인트로 한정" not in _help_body()
