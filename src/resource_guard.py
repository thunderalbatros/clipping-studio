"""Laptop & VM hardware protection guard ported from centinela-studio (HP 250 G8 safe)."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
from pathlib import Path
from typing import Any


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _win_ram_mb() -> tuple[int, int, int]:
    """Return (free_ram_mb, total_ram_mb, used_pct) via GlobalMemoryStatusEx with zero subprocess overhead."""
    ms = _MEMORYSTATUSEX()
    ms.dwLength = ctypes.sizeof(ms)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
        free_mb = int(ms.ullAvailPhys // (1024 * 1024))
        total_mb = int(ms.ullTotalPhys // (1024 * 1024))
        return free_mb, total_mb, int(ms.dwMemoryLoad)
    return 4096, 16384, 50


def _on_battery() -> bool | None:
    """Return True if Windows laptop is discharging on battery, False if plugged in, None if unknown."""
    if platform.system().lower() != "windows":
        return None
    try:
        sps = _SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            if sps.ACLineStatus == 0:
                return True
            if sps.ACLineStatus == 1:
                return False
    except Exception:
        pass
    return None


def onnx_threads(override: int | None = None) -> int:
    """Cap ONNX CPU threads to max(1, min(4, logical_cores - 2)) to avoid laptop thermal throttling."""
    env_ov = override if override is not None else int(os.getenv("ONNX_THREADS", "0") or 0)
    if env_ov > 0:
        return min(env_ov, 8)
    cores = os.cpu_count() or 4
    return max(1, min(4, cores - 2))


def apply_thread_budget() -> int:
    """Apply conservative thread budget to OMP / ORT environment variables."""
    n = onnx_threads()
    os.environ.setdefault("OMP_NUM_THREADS", str(n))
    os.environ.setdefault("ORT_NUM_THREADS", str(n))
    return n


def snapshot(drive_path: Path | None = None) -> dict[str, Any]:
    """Capture a fast non-blocking hardware snapshot (RAM, disk, cores, battery, thread budget)."""
    target = drive_path or Path(__file__).resolve().parent.parent
    usage = shutil.disk_usage(str(target))
    free_disk_gb = round(usage.free / (1024**3), 2)
    total_disk_gb = round(usage.total / (1024**3), 2)

    if platform.system().lower() == "windows":
        free_ram_mb, total_ram_mb, used_ram_pct = _win_ram_mb()
    else:
        free_ram_mb, total_ram_mb, used_ram_pct = 4096, 16384, 50

    return {
        "platform": platform.system().lower(),
        "logical_cores": os.cpu_count() or 4,
        "free_ram_mb": free_ram_mb,
        "total_ram_mb": total_ram_mb,
        "used_ram_pct": used_ram_pct,
        "free_disk_gb": free_disk_gb,
        "total_disk_gb": total_disk_gb,
        "on_battery": _on_battery(),
        "onnx_threads": onnx_threads(),
    }


def heavy_local_allowed(
    min_free_ram_mb: int = 1500,
    min_free_disk_gb: float = 5.0,
    allow_on_battery: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (allowed, reason, snap) before launching heavy ONNX/FFmpeg renders or Live Sniper jobs."""
    snap = snapshot()
    if snap["free_ram_mb"] < min_free_ram_mb:
        return False, f"free RAM {snap['free_ram_mb']} MB < {min_free_ram_mb} MB", snap
    if snap["free_disk_gb"] < min_free_disk_gb:
        return False, f"free disk {snap['free_disk_gb']} GB < {min_free_disk_gb} GB", snap
    if not allow_on_battery and snap["on_battery"] is True:
        return False, "laptop running on battery without AC power", snap
    return True, "resources healthy", snap
