"""Cross-platform binary, font, browser, and path resolution for Windows 10 & Linux Cloud Run Jobs (v3.6-A).

Eliminates hardcoded OS-specific virtualenv paths, system font paths, and personal host paths so the
control plane and GPU Cloud Run worker share one deterministic runtime contract.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from PIL import ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "runs"
BUNDLED_FONT_PATH = PROJECT_ROOT / "assets" / "fonts" / "Montserrat-Black.ttf"

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

_ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", re.IGNORECASE)


def venv_bin_dirs(project_root: Path = PROJECT_ROOT) -> list[Path]:
    """Return candidate virtualenv binary directories ordered for the active OS."""
    primary = project_root / ".venv" / ("Scripts" if IS_WINDOWS else "bin")
    secondary = project_root / ".venv" / ("bin" if IS_WINDOWS else "Scripts")
    return [d for d in (primary, secondary) if d.is_dir()]


def _find_windows_winget_deno() -> Path | None:
    local_app = os.environ.get("LOCALAPPDATA", "")
    if not local_app:
        return None
    links_exe = Path(local_app) / "Microsoft" / "WinGet" / "Links" / "deno.exe"
    if links_exe.is_file():
        return links_exe
    pkg_root = Path(local_app) / "Microsoft" / "WinGet" / "Packages"
    if pkg_root.is_dir():
        for pkg_dir in pkg_root.glob("DenoLand.Deno*"):
            direct = pkg_dir / "deno.exe"
            if direct.is_file():
                return direct
            for nested in pkg_dir.rglob("deno.exe"):
                if nested.is_file():
                    return nested
    return None


def resolve_binary(name: str, *, project_root: Path = PROJECT_ROOT) -> Path | None:
    """Locate a CLI binary across `.venv/{bin,Scripts}`, `PATH`, `~/.deno/bin`, and WinGet."""
    clean = name.strip()
    if clean.lower().endswith(".exe"):
        clean = clean[:-4]

    suffixes = (".exe", "") if IS_WINDOWS else ("", ".exe")
    for bdir in venv_bin_dirs(project_root):
        for sfx in suffixes:
            cand = bdir / f"{clean}{sfx}"
            if cand.is_file():
                return cand

    found = shutil.which(clean)
    if found:
        return Path(found)

    if clean.lower() == "deno":
        for sfx in suffixes:
            user_deno = Path.home() / ".deno" / "bin" / f"deno{sfx}"
            if user_deno.is_file():
                return user_deno
        if IS_WINDOWS:
            wg = _find_windows_winget_deno()
            if wg is not None:
                return wg

    return None


def resolve_binary_str(name: str, *, project_root: Path = PROJECT_ROOT) -> str:
    """Return resolved executable path as string, falling back cleanly to bare command name."""
    resolved = resolve_binary(name, project_root=project_root)
    return str(resolved) if resolved is not None else name


def find_deno_executable(project_root: Path = PROJECT_ROOT) -> Path | None:
    """Cross-platform Deno resolver for yt-dlp EJS challenge solver."""
    return resolve_binary("deno", project_root=project_root)


def ensure_media_path(project_root: Path = PROJECT_ROOT) -> None:
    """Idempotently prepend `.venv/{bin,Scripts}`, Deno, and WinGet Links onto `os.environ['PATH']`."""
    extra_dirs: list[Path] = list(venv_bin_dirs(project_root))
    deno_exe = find_deno_executable(project_root)
    if deno_exe is not None:
        extra_dirs.append(deno_exe.parent)

    user_deno = Path.home() / ".deno" / "bin"
    if user_deno.is_dir():
        extra_dirs.append(user_deno)

    if IS_WINDOWS:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            links = Path(local_app) / "Microsoft" / "WinGet" / "Links"
            if links.is_dir():
                extra_dirs.append(links)

    current = os.environ.get("PATH", "")
    cur_parts = current.split(os.pathsep) if current else []
    prefix: list[str] = []
    for p in extra_dirs:
        s = str(p)
        if p.is_dir() and s not in cur_parts and s not in prefix:
            prefix.append(s)
    if prefix:
        os.environ["PATH"] = os.pathsep.join([*prefix, *cur_parts])


def find_headless_browser() -> Path | None:
    """Locate Chromium / Chrome / Edge across Linux containers and Windows 10 hosts."""
    env_bin = (os.getenv("HEADLESS_BROWSER_BIN") or os.getenv("CHROME_BIN") or "").strip()
    if env_bin and Path(env_bin).is_file():
        return Path(env_bin)

    for cmd_name in (
        "chromium",
        "chromium-browser",
        "google-chrome-stable",
        "google-chrome",
        "chrome",
        "msedge",
        "microsoft-edge",
    ):
        found = shutil.which(cmd_name)
        if found:
            return Path(found)

    if IS_LINUX:
        for linux_cand in (
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/google-chrome"),
        ):
            if linux_cand.is_file():
                return linux_cand

    if IS_WINDOWS:
        local_app = os.getenv("LOCALAPPDATA", "")
        win_candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe" if local_app else Path(),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        ]
        for p in win_candidates:
            if str(p) and p.is_file():
                return p

    return None


def headless_browser_flags() -> list[str]:
    """Return container-safe headless Chromium/Chrome CLI flags (`--no-sandbox --disable-dev-shm-usage`)."""
    return [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-software-rasterizer",
        "--disable-extensions",
        "--force-device-scale-factor=1",
        "--hide-scrollbars",
    ]


def load_bundled_font(size: int, font_path: Path = BUNDLED_FONT_PATH) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load bundled commercial OFL `Montserrat-Black.ttf` at `size` without OS font paths."""
    if font_path.is_file():
        try:
            return ImageFont.truetype(str(font_path), size)
        except OSError:
            pass
    return ImageFont.load_default()


def resolve_omni_rag_dir(project_root: Path = PROJECT_ROOT) -> Path:
    """Resolve optional read-only Omni-RAG v6 cache directory via env var or relative workspace sibling."""
    env_val = (os.getenv("OMNI_RAG_CACHE_DIR") or os.getenv("OMNI_RAG_DIR") or "").strip()
    if env_val:
        return Path(env_val)
    return project_root.parent / "web-software-factory" / ".cache"


def dir_size_bytes(path: Path) -> int:
    """Safely sum byte sizes of all regular files under `path`."""
    if not path.exists():
        return 0
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def parse_iso_duration_sec(iso: str) -> float:
    """Parse ISO-8601 duration (e.g. `PT1M15S`) into seconds."""
    m = _ISO_DUR_RE.match((iso or "").strip())
    if not m:
        return 0.0
    hrs = int(m.group(1) or 0)
    mins = int(m.group(2) or 0)
    secs = float(m.group(3) or 0.0)
    return round(hrs * 3600.0 + mins * 60.0 + secs, 3)


def format_hms(seconds: float) -> str:
    """Format non-negative seconds as strict `HH:MM:SS` (`60` -> `'00:01:00'`, never `'00:00:60'`)."""
    total = max(0, round(float(seconds)))
    mins, secs = divmod(total, 60)
    hrs, mins = divmod(mins, 60)
    return f"{hrs:02d}:{mins:02d}:{secs:02d}"
