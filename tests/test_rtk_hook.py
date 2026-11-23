"""Unit and runtime contract tests for scripts/rtk_pretool_hook.py and .agents/hooks.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.rtk_pretool_hook import process_hook_payload, rewrite_command

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_hooks_json_schema_and_target() -> None:
    hooks_path = REPO_ROOT / ".agents" / "hooks.json"
    assert hooks_path.is_file()
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "rtk-shell-compressor" in data
    hook_cfg = data["rtk-shell-compressor"]
    assert hook_cfg.get("enabled", True) is True
    pre = hook_cfg["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "run_command"
    handler = pre[0]["hooks"][0]
    assert handler["type"] == "command"
    assert "rtk_pretool_hook.py" in handler["command"]
    assert handler["timeout"] <= 10


def test_rewrite_simple_allowed_commands() -> None:
    assert rewrite_command("git status --short", rtk_bin="rtk") == "rtk git status --short"
    assert rewrite_command("git log -n 10 --oneline", rtk_bin="rtk") == "rtk git log -n 10 --oneline"
    assert rewrite_command("ruff check src/ tests/", rtk_bin="rtk") == "rtk ruff check src/ tests/"
    assert (
        rewrite_command(r".\.venv\Scripts\ruff.exe check src/", rtk_bin="rtk")
        == "rtk ruff check src/"
    )
    assert (
        rewrite_command(r".\.venv\Scripts\pytest.exe tests/ -v", rtk_bin="rtk")
        == r"rtk test .\.venv\Scripts\pytest.exe tests/ -v"
    )
    assert (
        rewrite_command("uv pip list", rtk_bin="rtk")
        == "rtk uv pip list"
    )


def test_rewrite_preserves_paths_with_spaces() -> None:
    cmd = r'"C:\Users\tomas\Desktop\clipping studio\.venv\Scripts\pytest.exe" tests/ -q'
    rewritten = rewrite_command(cmd, cwd=str(REPO_ROOT), rtk_bin="rtk")
    assert rewritten == r'rtk test "C:\Users\tomas\Desktop\clipping studio\.venv\Scripts\pytest.exe" tests/ -q'

    # Bare pytest with repo cwd resolves to local .venv\Scripts\pytest.exe
    rewritten_bare = rewrite_command("pytest tests/ -q", cwd=str(REPO_ROOT), rtk_bin="rtk")
    assert rewritten_bare == r"rtk test .\.venv\Scripts\pytest.exe tests/ -q"


def test_bypass_pipes_redirections_chaining_and_mutating_git() -> None:
    bypass_cases = [
        "git status | Select-Object -First 5",
        "pytest tests/ > out.txt",
        "ruff check src/ 2>&1",
        "git status; git log -n 3",
        "git status && pytest tests/",
        "git commit -m 'feat: test'",
        "git push origin main",
        "rtk git status",
        "$env:FOO='bar'; pytest tests/",
        "python -m src.cli doctor",
        "git log\ngit status",
    ]
    for cmd in bypass_cases:
        assert rewrite_command(cmd, cwd=str(REPO_ROOT), rtk_bin="rtk") is None, f"Should bypass: {cmd}"


def test_process_hook_payload_contract() -> None:
    payload = {
        "toolCall": {
            "id": "toolu_01",
            "name": "run_command",
            "args": {"CommandLine": "git diff --stat"},
        },
        "cwd": str(REPO_ROOT),
    }
    res = process_hook_payload(payload, rtk_bin="rtk")
    assert res["decision"] == "allow"
    assert res["overwrite"]["CommandLine"] == "rtk git diff --stat"

    # Non-run_command tool is bypassed
    other = {"toolCall": {"id": "t2", "name": "read_file", "args": {"Path": "README.md"}}}
    assert process_hook_payload(other, rtk_bin="rtk") == {"decision": "allow"}


def test_cmd_c_execution_from_agents_dir_with_spaces_in_path() -> None:
    """Verify exact hooks.json command runs via cmd /c from `.agents/` despite space in `clipping studio`."""
    hooks_data = json.loads((REPO_ROOT / ".agents" / "hooks.json").read_text(encoding="utf-8"))
    hook_cmd = hooks_data["rtk-shell-compressor"]["PreToolUse"][0]["hooks"][0]["command"]
    stdin_payload = json.dumps(
        {
            "toolCall": {
                "id": "t_space",
                "name": "run_command",
                "args": {"CommandLine": r".\.venv\Scripts\pytest.exe tests/test_captions.py -q"},
            },
            "cwd": str(REPO_ROOT),
        }
    )
    proc = subprocess.run(
        ["cmd", "/c", hook_cmd],
        cwd=str(REPO_ROOT / ".agents"),
        input=stdin_payload,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    parsed = json.loads(proc.stdout.strip())
    assert parsed["decision"] == "allow"
    assert parsed["overwrite"]["CommandLine"] == r"rtk test .\.venv\Scripts\pytest.exe tests/test_captions.py -q"
