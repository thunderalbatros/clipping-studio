"""Antigravity IDE PreToolUse hook wrapping verbose CLI commands with rtk 1.2.0.

Contract:
- Reads Antigravity PreToolUse JSON payload from stdin.
- Outputs JSON decision (`allow`, optional `overwrite.CommandLine`) to stdout.
- Safely bypasses PowerShell pipelines, redirections, command chaining, subshells,
  multiline scripts, and mutating git commands.
- Works cleanly when the workspace path contains spaces (e.g. `clipping studio`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

# Shell operators / constructs that must NEVER be rewritten blindly in PowerShell
UNSAFE_SHELL_TOKENS = (
    "|",
    ">",
    "<",
    ";",
    "&&",
    "||",
    "$(",
    "`",
    "\n",
    "\r",
    "$env:",
    "2>&1",
    "1>&2",
)

# Read-only / inspection git subcommands safe and high-value for rtk compression
RTK_SAFE_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "shortlog",
        "ls-files",
        "grep",
        "blame",
    }
)

# uv subcommands safe to pass through rtk uv
RTK_SAFE_UV_SUBCOMMANDS = frozenset(
    {
        "run",
        "tree",
        "pip",
        "sync",
        "lock",
        "version",
    }
)

# Match leading executable token (optionally quoted, with Windows or POSIX separators)
_LEADING_TOKEN_RE = re.compile(
    r"""^\s*(?:"([^"]+)"|'([^']+)'|(\S+))(?:\s+(.*))?$""",
    re.DOTALL,
)


def find_rtk_executable() -> str | None:
    """Return `rtk` if on PATH, or full path to WinGet rtk.exe if available."""
    found = shutil.which("rtk")
    if found:
        return "rtk"
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        winget_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if winget_root.is_dir():
            for candidate in winget_root.glob("rtk-ai.rtk_*/rtk.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


def _format_rtk_prefix(rtk_bin: str) -> str:
    """Format rtk executable safely for PowerShell when path contains spaces."""
    if rtk_bin == "rtk" or (" " not in rtk_bin and "\\" not in rtk_bin):
        return rtk_bin
    return f'& "{rtk_bin}"'


def _has_unsafe_shell_syntax(cmd: str) -> bool:
    """Return True if cmd uses pipes, redirections, chaining, or complex PowerShell."""
    stripped = cmd.strip()
    if not stripped:
        return True
    if stripped.startswith("& ") or stripped.startswith("$"):
        return True
    for token in UNSAFE_SHELL_TOKENS:
        if token in stripped:
            return True
    return False


def _normalize_exe_name(token: str) -> str:
    """Extract lowercase basename without .exe suffix from a Windows/POSIX path."""
    cleaned = token.replace("\\", "/").strip()
    base = cleaned.rsplit("/", 1)[-1].lower()
    base = base.removesuffix(".exe")
    return base


def _is_venv_path(token: str) -> bool:
    normalized = token.replace("\\", "/").lower()
    return ".venv/" in normalized or "/venv/" in normalized or normalized.startswith("venv/")


def rewrite_command(command_line: str, cwd: str | None = None, rtk_bin: str | None = None) -> str | None:
    """Return rewritten CommandLine using rtk, or None to bypass unchanged."""
    if not command_line or _has_unsafe_shell_syntax(command_line):
        return None

    stripped = command_line.strip()
    match = _LEADING_TOKEN_RE.match(stripped)
    if not match:
        return None

    raw_exe = match.group(1) or match.group(2) or match.group(3) or ""
    rest = (match.group(4) or "").strip()
    exe_name = _normalize_exe_name(raw_exe)

    # Already wrapped with rtk
    if exe_name == "rtk":
        return None

    resolved_rtk = rtk_bin if rtk_bin is not None else find_rtk_executable()
    if not resolved_rtk:
        return None
    rtk_prefix = _format_rtk_prefix(resolved_rtk)

    # 1. git (read-only inspection subcommands only)
    if exe_name == "git" and rest:
        first_sub = rest.split(None, 1)[0].lower()
        if first_sub in RTK_SAFE_GIT_SUBCOMMANDS:
            return f"{rtk_prefix} git {rest}"
        return None

    # 2. pytest (preserve workspace .venv executable via `rtk test` so imports resolve!)
    if exe_name == "pytest":
        if _is_venv_path(raw_exe):
            exe_arg = f'"{raw_exe}"' if " " in raw_exe else raw_exe
            return f"{rtk_prefix} test {exe_arg} {rest}".strip()
        if cwd:
            venv_pytest = Path(cwd) / ".venv" / "Scripts" / "pytest.exe"
            if venv_pytest.is_file():
                return f"{rtk_prefix} test .\\.venv\\Scripts\\pytest.exe {rest}".strip()
        return f"{rtk_prefix} pytest {rest}".strip()

    # 2b. python -m pytest
    if exe_name in ("python", "python3") and rest:
        parts = rest.split()
        if len(parts) >= 2 and parts[0] == "-m" and parts[1] == "pytest":
            exe_arg = f'"{raw_exe}"' if " " in raw_exe else raw_exe
            return f"{rtk_prefix} test {exe_arg} {rest}".strip()
        return None

    # 3. ruff (`rtk ruff` provides grouped compact lint output)
    if exe_name == "ruff" and rest:
        first_sub = rest.split(None, 1)[0].lower()
        if first_sub in ("check", "format"):
            return f"{rtk_prefix} ruff {rest}"
        return None

    # 4. uv
    if exe_name == "uv" and rest:
        first_sub = rest.split(None, 1)[0].lower()
        if first_sub in RTK_SAFE_UV_SUBCOMMANDS:
            return f"{rtk_prefix} uv {rest}"
        return None

    return None


def process_hook_payload(payload: dict[str, Any], rtk_bin: str | None = None) -> dict[str, Any]:
    """Process an Antigravity PreToolUse hook payload and return response dict."""
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "")
    if tool_name != "run_command":
        return {"decision": "allow"}

    args = tool_call.get("args") or {}
    command_line = str(args.get("CommandLine") or "")
    cwd = payload.get("cwd")
    if not cwd:
        ws = payload.get("workspacePaths") or []
        cwd = ws[0] if ws else None

    rewritten = rewrite_command(command_line, cwd=cwd, rtk_bin=rtk_bin)
    if rewritten and rewritten != command_line.strip():
        return {
            "decision": "allow",
            "reason": "Rewritten by rtk 1.2.0 for terminal token compression.",
            "overwrite": {"CommandLine": rewritten},
        }
    return {"decision": "allow"}


def main() -> int:
    """CLI entrypoint for `.agents/hooks.json` PreToolUse hook."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        # Fail-open cleanly: never break tool execution on malformed stdin
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return 0

    response = process_hook_payload(payload)
    sys.stdout.write(json.dumps(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
