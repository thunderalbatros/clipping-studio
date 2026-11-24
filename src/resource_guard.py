"""Cross-platform hardware & RAM guard powered by `psutil` (Windows 10 + Linux Cloud Run Jobs, v3.6-A)."""

from __future__ import annotations

import gc
import os
import platform
import sys
from pathlib import Path
from typing import Any

import psutil


def _on_battery() -> bool | None:
    """Return True if discharging on battery, False if AC plugged, None on headless/server hosts."""
    try:
        batt = psutil.sensors_battery()
        if batt is None or batt.power_plugged is None:
            return None
        return not bool(batt.power_plugged)
    except Exception:
        return None


def onnx_threads(override: int | None = None) -> int:
    """Cap ONNX CPU threads to `max(1, min(4, logical_cores - 2))` to avoid laptop/container contention."""
    env_ov = override if override is not None else int(os.getenv("ONNX_THREADS", "0") or 0)
    if env_ov > 0:
        return min(env_ov, 8)
    cores = psutil.cpu_count(logical=True) or os.cpu_count() or 4
    return max(1, min(4, cores - 2))


def apply_thread_budget() -> int:
    """Apply conservative thread budget to OMP / ORT environment variables."""
    n = onnx_threads()
    os.environ.setdefault("OMP_NUM_THREADS", str(n))
    os.environ.setdefault("ORT_NUM_THREADS", str(n))
    return n


def snapshot(drive_path: Path | None = None) -> dict[str, Any]:
    """Capture cross-platform hardware snapshot (`psutil.virtual_memory` + `psutil.disk_usage`)."""
    target = drive_path or Path(__file__).resolve().parent.parent
    if not target.exists():
        target = target.parent

    vm = psutil.virtual_memory()
    du = psutil.disk_usage(str(target))
    free_ram_mb = int(vm.available // (1024 * 1024))
    total_ram_mb = int(vm.total // (1024 * 1024))
    used_ram_pct = round(float(vm.percent), 1)

    return {
        "platform": platform.system().lower(),
        "logical_cores": psutil.cpu_count(logical=True) or os.cpu_count() or 4,
        "free_ram_mb": free_ram_mb,
        "total_ram_mb": total_ram_mb,
        "used_ram_pct": used_ram_pct,
        "free_disk_gb": round(du.free / (1024**3), 2),
        "total_disk_gb": round(du.total / (1024**3), 2),
        "on_battery": _on_battery(),
        "onnx_threads": onnx_threads(),
    }


def heavy_local_allowed(
    min_free_ram_mb: int = 1200,
    max_used_ram_pct: float = 92.0,
    min_free_disk_gb: float = 4.0,
    allow_on_battery: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    """Return `(allowed, reason, snap)` before launching heavy ONNX/FFmpeg renders or Live Sniper jobs."""
    snap = snapshot()
    if snap["free_ram_mb"] < min_free_ram_mb:
        return False, f"free RAM {snap['free_ram_mb']} MB < {min_free_ram_mb} MB", snap
    if float(snap["used_ram_pct"]) > max_used_ram_pct:
        return False, f"used RAM {snap['used_ram_pct']}% > {max_used_ram_pct}%", snap
    if snap["free_disk_gb"] < min_free_disk_gb:
        return False, f"free disk {snap['free_disk_gb']} GB < {min_free_disk_gb} GB", snap
    if not allow_on_battery and snap["on_battery"] is True:
        return False, "laptop running on battery without AC power", snap
    return True, "resources healthy", snap


def unload_heavy_models() -> dict[str, Any]:
    """Explicitly evict cached ONNX/ASR/Matting/Embedding/Reranker sessions between pipeline phases."""
    proc = psutil.Process()
    rss_before_mb = round(proc.memory_info().rss / (1024 * 1024), 2)
    cleared: dict[str, int] = {}

    # Inspect only already-imported modules so we never trigger lazy heavy imports during cleanup.
    mod_tr = sys.modules.get("src.transcriber")
    if mod_tr is not None:
        cache_tr = getattr(mod_tr, "_MODEL_CACHE", None)
        if isinstance(cache_tr, dict) and cache_tr:
            cleared["transcriber_models"] = len(cache_tr)
            cache_tr.clear()

    mod_sg = sys.modules.get("src.speaker_guard")
    if mod_sg is not None:
        cache_sg = getattr(mod_sg, "_SESSIONS", None)
        if isinstance(cache_sg, dict) and cache_sg:
            cleared["speaker_guard_sessions"] = len(cache_sg)
            cache_sg.clear()

    mod_de = sys.modules.get("src.design_engine")
    if mod_de is not None:
        cache_de = getattr(mod_de, "_REMBG_SESSIONS", None)
        if isinstance(cache_de, dict) and cache_de:
            cleared["rembg_sessions"] = len(cache_de)
            cache_de.clear()

    mod_rag = sys.modules.get("src.rag_engine")
    if mod_rag is not None:
        if mod_rag.__dict__.get("_EMBEDDER_CACHE") is not None:
            cleared["rag_embedder"] = 1
            mod_rag.__dict__["_EMBEDDER_CACHE"] = None
        if mod_rag.__dict__.get("_RERANKER_CACHE") is not None:
            cleared["rag_reranker"] = 1
            mod_rag.__dict__["_RERANKER_CACHE"] = None
        q_client = mod_rag.__dict__.get("_QDRANT_CLIENT_CACHE")
        if q_client is not None:
            try:
                q_client.close()
            except Exception:
                pass
            cleared["qdrant_client"] = 1
            mod_rag.__dict__["_QDRANT_CLIENT_CACHE"] = None

    gc.collect()
    rss_after_mb = round(proc.memory_info().rss / (1024 * 1024), 2)
    return {
        "cleared": cleared,
        "rss_before_mb": rss_before_mb,
        "rss_after_mb": rss_after_mb,
        "reclaimed_rss_mb": round(max(0.0, rss_before_mb - rss_after_mb), 2),
    }
