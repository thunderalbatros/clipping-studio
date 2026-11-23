"""Rate-limited Telegram Bot Notifier & HITL alert helper (zero secret logging)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from src.miner import PROJECT_ROOT

_RATE_LOCK = threading.Lock()
_LAST_SEND_TS = 0.0
_MIN_INTERVAL_SEC = 1.1


def _load_telegram_creds() -> tuple[str, str]:
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    return token, chat_id


def _throttle() -> None:
    global _LAST_SEND_TS
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SEC - (now - _LAST_SEND_TS)
        if wait > 0:
            time.sleep(wait)
        _LAST_SEND_TS = time.monotonic()


def send_telegram_message(
    text: str,
    *,
    parse_mode: str = "HTML",
    inline_buttons: list[list[dict[str, str]]] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send a rate-limited Telegram message; returns status dict without leaking credentials."""
    token, chat_id = _load_telegram_creds()
    if dry_run:
        return {"ok": True, "dry_run": True, "chars": len(text)}
    if not token or not chat_id:
        return {"ok": False, "reason": "TELEGRAM_CREDS_MISSING"}

    _throttle()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if inline_buttons:
        payload["reply_markup"] = {"inline_keyboard": inline_buttons}

    try:
        resp = curl_requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=15,
        )
        ok = resp.status_code == 200 and bool((resp.json() or {}).get("ok"))
        return {"ok": ok, "status_code": resp.status_code}
    except Exception as exc:
        return {"ok": False, "reason": type(exc).__name__}


def send_clip_ready_alert(
    run_id: str,
    clip_id: str,
    mp4_path: Path,
    packaging: dict[str, Any] | None = None,
    critic_overall: float = 0.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Notify operator that a verified 1080p60 9:16 master clip is ready for publication."""
    title = str((packaging or {}).get("shorts_title") or clip_id)
    hook = str((packaging or {}).get("suggested_on_screen_hook") or "")
    x_post = str((packaging or {}).get("x_post_text") or "")
    bounty_safe = bool((packaging or {}).get("bounty_safe", True))

    lines = [
        f"<b>Clipping Studio — Clip Ready</b> ({run_id}/{clip_id})",
        f"<b>Shorts Title:</b> {title}",
        f"<b>Hook:</b> {hook}",
        f"<b>Critic:</b> {critic_overall:.2f} | <b>Bounty-Safe:</b> {'YES' if bounty_safe else 'NO'}",
        f"<b>File:</b> <code>{mp4_path.name}</code>",
    ]
    if x_post:
        lines.append(f"<b>X Copy:</b> {x_post[:180]}")

    buttons = [
        [
            {"text": "Approve Publish", "callback_data": f"clip:approve:{run_id}:{clip_id}"[:64]},
            {"text": "Discard", "callback_data": f"clip:reject:{run_id}:{clip_id}"[:64]},
        ]
    ]
    return send_telegram_message("\n".join(lines), inline_buttons=buttons, dry_run=dry_run)


def send_bounty_alert(campaign: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Send HITL review alert for a discovered CPM bounty campaign."""
    cid = str(campaign.get("campaign_id") or "")
    lines = [
        f"<b>Bounty Radar — HITL Review</b> [{campaign.get('platform')}]",
        f"<b>Title:</b> {campaign.get('title')}",
        f"<b>CPM:</b> ${float(campaign.get('cpm_usd') or 0.0):.2f}/1k | "
        f"<b>Burn:</b> {float(campaign.get('burn_rate_pct') or 0.0):.1f}%",
        f"<b>Rec:</b> {campaign.get('recommendation')} | <b>AR Eligible:</b> {campaign.get('eligible_from_ar')}",
        f"<b>Flags:</b> {json.dumps(campaign.get('risk_flags') or [])}",
        f"Approve CLI: <code>python -m src.cli bounties approve --id {cid} --verdict APPROVED</code>",
    ]
    return send_telegram_message("\n".join(lines), dry_run=dry_run)
