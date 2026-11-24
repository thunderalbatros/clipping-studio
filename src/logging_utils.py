"""Structured JSON observability (`structlog`) correlated by `run_id`, `clip_id`, `niche_id`, and `job_id`.

Emits GCP Cloud Logging compatible single-line JSON records (`severity`, `message`, `timestamp`,
`run_id`, `clip_id`, `niche_id`, `job_id`) strictly to `sys.stderr` so CLI JSON outputs on `sys.stdout`
remain uncorrupted, while deterministically redacting any accidental secret-like fields (Regla 01).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import MutableMapping
from contextlib import contextmanager
from typing import Any, TextIO, cast

import structlog

_CONFIGURED = False
_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "credentials",
    "private_key",
)

_LEVEL_TO_GCP_SEVERITY: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "warn": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
    "exception": "ERROR",
}


def _redact_value(key: str, val: Any) -> Any:
    k_low = key.lower()
    if any(m in k_low for m in _SECRET_KEY_MARKERS):
        if isinstance(val, bool | int | float):
            return val
        return "[REDACTED]" if val else ""
    if isinstance(val, dict):
        return {str(k): _redact_value(str(k), v) for k, v in val.items()}
    if isinstance(val, list):
        return [_redact_value(key, item) for item in val]
    return val


def _gcp_cloud_logging_processor(
    _logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Normalize `structlog` event dict to GCP Cloud Logging schema (`severity` + `message`) and redact secrets."""
    level = str(event_dict.pop("level", method_name) or method_name).lower()
    event_dict["severity"] = _LEVEL_TO_GCP_SEVERITY.get(level, "INFO")
    if "event" in event_dict and "message" not in event_dict:
        event_dict["message"] = event_dict.pop("event")

    job_env = (
        os.getenv("CLOUD_RUN_JOB")
        or os.getenv("CLOUD_RUN_EXECUTION")
        or os.getenv("JOB_ID")
        or ""
    ).strip()
    if job_env and "job_id" not in event_dict:
        event_dict["job_id"] = job_env

    for k in list(event_dict.keys()):
        event_dict[k] = _redact_value(str(k), event_dict[k])
    return event_dict


class _DynamicStderr:
    """Delegate writes/flushes to current `sys.stderr` so pytest `capsys` and Cloud Run capture logs cleanly."""

    def write(self, msg: str) -> int:
        return sys.stderr.write(msg)

    def flush(self) -> None:
        sys.stderr.flush()


_DYNAMIC_STDERR: TextIO = cast(TextIO, _DynamicStderr())


def configure_structlog(*, force: bool = False) -> None:
    """Configure `structlog` once to emit GCP-compatible JSON lines to `sys.stderr`."""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    min_level = getattr(logging, level_name, logging.INFO)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            _gcp_cloud_logging_processor,
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(min_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=_DYNAMIC_STDERR),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True


def get_logger(component: str, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to `component` and optional correlation fields."""
    configure_structlog()
    log: Any = structlog.get_logger(component=component, **initial_values)
    return log  # type: ignore[return-value]


def bind_correlation_context(
    *,
    run_id: str | None = None,
    clip_id: str | None = None,
    niche_id: str | None = None,
    job_id: str | None = None,
    **extra: Any,
) -> None:
    """Bind correlation IDs into `structlog.contextvars` for the current execution thread/task."""
    configure_structlog()
    to_bind: dict[str, Any] = {}
    if run_id:
        to_bind["run_id"] = run_id
    if clip_id:
        to_bind["clip_id"] = clip_id
    if niche_id:
        to_bind["niche_id"] = niche_id
    if job_id:
        to_bind["job_id"] = job_id
    for k, v in extra.items():
        if v is not None:
            to_bind[k] = v
    if to_bind:
        structlog.contextvars.bind_contextvars(**to_bind)


def clear_correlation_context(*keys: str) -> None:
    """Unbind specific keys (or clear all if none passed) from `structlog.contextvars`."""
    if keys:
        structlog.contextvars.unbind_contextvars(*keys)
    else:
        structlog.contextvars.clear_contextvars()


@contextmanager
def correlation_scope(
    *,
    run_id: str | None = None,
    clip_id: str | None = None,
    niche_id: str | None = None,
    job_id: str | None = None,
    **extra: Any,
) -> Any:
    """Scoped context manager binding correlation IDs and restoring prior context on exit."""
    configure_structlog()
    payload: dict[str, Any] = {}
    if run_id:
        payload["run_id"] = run_id
    if clip_id:
        payload["clip_id"] = clip_id
    if niche_id:
        payload["niche_id"] = niche_id
    if job_id:
        payload["job_id"] = job_id
    payload.update({k: v for k, v in extra.items() if v is not None})
    with structlog.contextvars.bound_contextvars(**payload):
        yield
