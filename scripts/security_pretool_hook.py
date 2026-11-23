"""Antigravity IDE PreToolUse deterministic security guard for Clipping Studio.

Enforces Regla 01 + P0 workspace boundary:
1. Denies any write/edit outside the workspace root (`C:\\Users\\tomas\\Desktop\\clipping studio`),
   explicitly protecting `~/.gemini/config/`, `web-software-factory`, `centinela-studio`,
   `streamerbot`, and `yendo_mobile`.
2. Denies any read/write/dump of `.env` (allowing `.env.example` / `.env.template`),
   `*cookies*.txt`, and credential/key files via file tools or shell commands.
3. Supports Windows paths with spaces (`clipping studio`) and coexists with `rtk_pretool_hook.py`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_EXTERNAL_FRAGMENTS = (
    ".gemini/config",
    "web-software-factory",
    "centinela-studio",
    "streamerbot",
    "yendo_mobile",
)

ALLOWED_ENV_NAMES = frozenset({".env.example", ".env.template", ".env.sample", ".env.dist"})

_SECRET_BASENAME_PATTERNS = (
    re.compile(r"^\.env(?:\..+)?$", re.IGNORECASE),
    re.compile(r".*cookies.*\.txt$", re.IGNORECASE),
    re.compile(r".*(?:credentials|service[_-]?account|client[_-]?secret).*\.json$", re.IGNORECASE),
    re.compile(r".*\.(?:pem|key|p12|pfx)$", re.IGNORECASE),
)

_SHELL_SECRET_DUMP_PATTERNS = (
    # Direct file reads/dumps of .env (excluding .env.example / .env.template / .env.sample)
    re.compile(
        r"\b(?:cat|type|get-content|gc|more|less|head|tail|notepad|sed|awk|read_text|open)\b[^\r\n;|&]*?[\s'\"(/\\]\.env(?![._-](?:example|template|sample|dist)\b)(?:$|[\s'\")])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:cat|type|get-content|gc|more|less|head|tail|read_text|open)\b[^\r\n;|&]*?cookies[^\s'\"]*\.txt",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:printenv|get-childitem\s+env:|gci\s+env:|ls\s+env:|dir\s+env:)\b", re.IGNORECASE),
)

_SHELL_WRITE_VERBS_RE = re.compile(
    r"(?:>>|(?<![0-9&])>(?!\s*(?:\$null|/dev/null|nul\b))|\b(?:out-file|set-content|add-content|move-item|copy-item|remove-item|new-item|rm|del|erase|mv|cp|xcopy|robocopy|mkdir|rmdir|shutil\.move|shutil\.copy|write_text|write_bytes)\b)",
    re.IGNORECASE,
)

_PATH_TOKEN_RE = re.compile(
    r"""(?:"([^"]+)"|'([^']+)'|((?:[A-Za-z]:[\\/]|~[\\/]|%USERPROFILE%[\\/])[^\s;|&><"']+))""",
    re.IGNORECASE,
)


def _normalize_path_str(raw: str) -> str:
    return raw.replace("\\", "/").strip().lower()


def _is_secret_path(path_str: str) -> bool:
    norm = _normalize_path_str(path_str)
    if not norm:
        return False
    basename = norm.rsplit("/", 1)[-1]
    if basename in ALLOWED_ENV_NAMES:
        return False
    return any(pat.match(basename) for pat in _SECRET_BASENAME_PATTERNS)


def _resolve_target_path(raw_path: str, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw_path.strip().strip('"').strip("'")))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        return candidate.resolve(strict=False)
    except OSError:
        return Path(os.path.normpath(str(candidate)))


def _is_within_root(target: Path, root: Path) -> bool:
    try:
        target_res = target.resolve(strict=False)
        root_res = root.resolve(strict=False)
        return target_res.is_relative_to(root_res)
    except (OSError, ValueError):
        t_norm = _normalize_path_str(str(target)).rstrip("/")
        r_norm = _normalize_path_str(str(root)).rstrip("/")
        return t_norm == r_norm or t_norm.startswith(r_norm + "/")


def _has_forbidden_external_fragment(text: str) -> str | None:
    norm = _normalize_path_str(text)
    for frag in FORBIDDEN_EXTERNAL_FRAGMENTS:
        if frag in norm:
            return frag
    return None


def _extract_workspace_root(payload: dict[str, Any], workspace_root: Path | None = None) -> Path:
    if workspace_root is not None:
        return workspace_root
    ws = payload.get("workspacePaths") or []
    if isinstance(ws, list) and ws and isinstance(ws[0], str) and ws[0].strip():
        return Path(ws[0]).resolve(strict=False)
    return DEFAULT_WORKSPACE_ROOT


def _check_shell_command(command_line: str, ws_root: Path, cwd: Path) -> dict[str, Any] | None:
    cmd = command_line.strip()
    if not cmd:
        return None

    # 1. Block reading/dumping secrets via shell
    for pat in _SHELL_SECRET_DUMP_PATTERNS:
        if pat.search(cmd):
            return {
                "decision": "deny",
                "reason": "Security Guard (Regla 01): blocked shell command attempting to read/dump .env, cookies, or environment secrets.",
            }

    # 2. Block writes/mutations targeting forbidden external paths or paths outside workspace
    if _SHELL_WRITE_VERBS_RE.search(cmd):
        frag = _has_forbidden_external_fragment(cmd)
        if frag:
            return {
                "decision": "deny",
                "reason": f"Security Guard (Regla 01): blocked mutating shell command referencing protected external path ({frag}).",
            }
        for match in _PATH_TOKEN_RE.finditer(cmd):
            raw_tok = match.group(1) or match.group(2) or match.group(3) or ""
            if not raw_tok:
                continue
            # Ignore URLs and flags
            if raw_tok.startswith(("http://", "https://", "file://", "-")):
                continue
            # Check explicit absolute/home paths
            if re.match(r"^(?:[A-Za-z]:[\\/]|~[\\/]|%USERPROFILE%[\\/])", raw_tok, re.IGNORECASE):
                resolved = _resolve_target_path(raw_tok, cwd)
                if not _is_within_root(resolved, ws_root):
                    # Allow explicit read-only SQLite URI tokens even inside mixed commands
                    if "mode=ro" in cmd.lower() and "immutable=1" in cmd.lower() and not _has_forbidden_external_fragment(raw_tok):
                        continue
                    return {
                        "decision": "deny",
                        "reason": f"Security Guard (Regla 01): blocked mutating shell command targeting path outside workspace ({raw_tok}).",
                    }
    return None


def evaluate_security_payload(
    payload: dict[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate a PreToolUse hook payload and return allow/deny decision."""
    tool_call = payload.get("toolCall") or {}
    tool_name = str(tool_call.get("name") or "").strip().lower()
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    ws_root = _extract_workspace_root(payload, workspace_root=workspace_root)
    raw_cwd = str(payload.get("cwd") or "").strip()
    cwd = Path(raw_cwd).resolve(strict=False) if raw_cwd else ws_root

    # 1. File read tools: block secrets (.env, *cookies*.txt, credentials)
    if tool_name in {"read_file", "view_file"}:
        target_raw = str(args.get("Path") or args.get("AbsolutePath") or args.get("path") or "")
        if target_raw and _is_secret_path(target_raw):
            return {
                "decision": "deny",
                "reason": f"Security Guard (Regla 01): blocked read access to secret file ({Path(target_raw).name}).",
            }
        return {"decision": "allow"}

    # 2. File write/edit tools: block secrets AND any path outside workspace root
    if tool_name in {"write_file", "edit_file", "replace_file_content", "multi_replace_file_content", "write_to_file"}:
        target_raw = str(args.get("Path") or args.get("TargetFile") or args.get("path") or "")
        if not target_raw:
            return {"decision": "allow"}
        if _is_secret_path(target_raw):
            return {
                "decision": "deny",
                "reason": f"Security Guard (Regla 01): blocked write/edit to secret file ({Path(target_raw).name}).",
            }
        frag = _has_forbidden_external_fragment(target_raw)
        if frag:
            return {
                "decision": "deny",
                "reason": f"Security Guard (Regla 01): blocked write/edit to protected external path ({frag}).",
            }
        resolved = _resolve_target_path(target_raw, cwd)
        if not _is_within_root(resolved, ws_root):
            return {
                "decision": "deny",
                "reason": f"Security Guard (Regla 01): blocked write/edit outside workspace ({resolved}).",
            }
        return {"decision": "allow"}

    # 3. Shell execution tool
    if tool_name == "run_command":
        command_line = str(args.get("CommandLine") or args.get("command") or "")
        verdict = _check_shell_command(command_line, ws_root=ws_root, cwd=cwd)
        if verdict is not None:
            return verdict
        return {"decision": "allow"}

    return {"decision": "allow"}


def main() -> int:
    """CLI entrypoint for `.agents/hooks.json` PreToolUse security guard."""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        return 0

    response = evaluate_security_payload(payload)
    sys.stdout.write(json.dumps(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
