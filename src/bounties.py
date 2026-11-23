"""Bounty Radar & CPM Campaign Auditor (Whop Content Rewards, Clipping.net, Vyro, ClipBoom).

Read-only discovery via Serper.dev + optional Firecrawl (zero login scraping, zero auto-apply).
Every discovered campaign is audited deterministically against Argentina eligibility, burn-rate
window (20%..80%), trap clauses, and voice policy, then persisted to SQLite (`campaigns` table)
with `verdict='PENDING_HITL'` until explicitly approved by the human operator.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

from src.db import DEFAULT_DB_PATH, insert_telemetry, list_campaigns, set_campaign_verdict, upsert_campaign
from src.miner import PROJECT_ROOT

_CPM_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)\s*(?:1k|1,000|k\b|1000\s*views)", re.IGNORECASE)
_BURN_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%\s*(?:paid|claimed|used|spent|funded|burned)", re.IGNORECASE)
_MIN_VIEWS_RE = re.compile(r"(?:min(?:imum)?\s*views?[:\s]*)([0-9][0-9,\.]*\s*[km]?)", re.IGNORECASE)

_GEO_BLOCK_MARKERS = (
    "us residents only",
    "usa only",
    "united states only",
    "uk residents only",
    "ssn required",
    "w-9 required",
    "us bank account only",
    "tier-1 creators only",
)
_TRAP_CLAUSE_MARKERS = (
    "exclusive rights",
    "permanent license to account",
    "must post 10 videos before payout",
    "no payout under 100k",
    "100,000 minimum views",
    "manual approval with no sla",
)
_ANTI_AI_VOICE_MARKERS = (
    "no ai voice",
    "no tts",
    "original audio only",
    "no voiceover",
    "raw streamer audio",
)

BOUNTY_SEARCH_QUERIES: list[tuple[str, str]] = [
    ("whop", 'site:whop.com "content rewards" ("GTA 6" OR "GTA VI" OR "gaming" OR "clipping") CPM'),
    ("clipping_net", 'site:clipping.net ("GTA" OR "gaming" OR "streamer") campaign CPM'),
    ("vyro", 'site:vyro.com OR "vyro" ("content rewards" OR "clipping") gaming CPM'),
    ("clipboom", 'site:clipboom.com OR "clipboom" ("clipping" OR "bounty") gaming CPM'),
]


def _parse_compact_int(raw: str) -> int:
    s = raw.strip().lower().replace(",", "")
    mult = 1
    if s.endswith("k"):
        mult = 1_000
        s = s[:-1]
    elif s.endswith("m"):
        mult = 1_000_000
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def audit_campaign_rules(
    title: str,
    brief_text: str,
    cpm_usd: float = 0.0,
    burn_rate_pct: float = 0.0,
    geo_lock: str = "",
    min_views: int = 0,
    voice_policy: str = "none",
) -> dict[str, Any]:
    """Deterministic bounty risk & eligibility audit for an Argentina-based Clipping Studio operator."""
    haystack = f"{title}\n{brief_text}\n{geo_lock}".lower()
    risk_flags: list[str] = []
    eligible_from_ar = True

    for marker in _GEO_BLOCK_MARKERS:
        if marker in haystack:
            eligible_from_ar = False
            risk_flags.append(f"GEO_BLOCK:{marker}")

    for marker in _TRAP_CLAUSE_MARKERS:
        if marker in haystack:
            risk_flags.append(f"TRAP_CLAUSE:{marker}")

    detected_voice = voice_policy
    if any(m in haystack for m in _ANTI_AI_VOICE_MARKERS):
        detected_voice = "none"
    elif "voiceover required" in haystack or "narration required" in haystack:
        detected_voice = "human_batch"
        risk_flags.append("VOICEOVER_REQUIRED:use_human_batch_sm58")

    if burn_rate_pct > 0.0:
        if burn_rate_pct < 20.0:
            risk_flags.append(f"LOW_BURN_RATE:{burn_rate_pct:.1f}% (<20% unverified payout velocity)")
        elif burn_rate_pct > 80.0:
            risk_flags.append(f"HIGH_BURN_RATE:{burn_rate_pct:.1f}% (>80% budget near exhaustion)")

    if min_views > 50_000:
        risk_flags.append(f"HIGH_MIN_VIEWS:{min_views}")
    if 0.0 < cpm_usd < 0.75:
        risk_flags.append(f"LOW_CPM:${cpm_usd:.2f}/1k")

    if not eligible_from_ar or any(f.startswith(("TRAP_CLAUSE:", "HIGH_BURN_RATE:")) for f in risk_flags):
        rec = "REJECT"
    elif risk_flags or cpm_usd <= 0.0:
        rec = "CAUTION"
    else:
        rec = "APPROVE_CANDIDATE"

    return {
        "eligible_from_ar": eligible_from_ar,
        "voice_policy": detected_voice,
        "risk_flags": risk_flags,
        "verdict_recommendation": rec,
    }


def _extract_metrics_from_text(text: str) -> tuple[float, float, int]:
    cpm = 0.0
    m_cpm = _CPM_RE.search(text)
    if m_cpm:
        cpm = float(m_cpm.group(1))
    burn = 0.0
    m_burn = _BURN_RE.search(text)
    if m_burn:
        burn = min(100.0, float(m_burn.group(1)))
    min_views = 0
    m_mv = _MIN_VIEWS_RE.search(text)
    if m_mv:
        min_views = _parse_compact_int(m_mv.group(1))
    return cpm, burn, min_views


def _serper_search(query: str, num: int = 5) -> list[dict[str, Any]]:
    api_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not api_key:
        return []
    try:
        resp = curl_requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return list((resp.json() or {}).get("organic") or [])
    except Exception:
        return []


def _firecrawl_scrape_markdown(url: str) -> str:
    api_key = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if not api_key:
        return ""
    try:
        resp = curl_requests.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=25,
        )
        if resp.status_code != 200:
            return ""
        data = (resp.json() or {}).get("data") or {}
        return str(data.get("markdown") or "")[:4000]
    except Exception:
        return ""


def scan_bounties(
    max_per_platform: int = 4,
    enrich_firecrawl: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Discover public CPM clipping campaigns across Whop, Clipping.net, Vyro & ClipBoom -> SQLite."""
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    results: list[dict[str, Any]] = []

    for platform, query in BOUNTY_SEARCH_QUERIES:
        hits = _serper_search(query, num=max_per_platform)
        for hit in hits:
            url = str(hit.get("link") or "").strip()
            title = str(hit.get("title") or "").strip()
            snippet = str(hit.get("snippet") or "").strip()
            if not url or not title:
                continue
            brief = f"{snippet}\nURL: {url}"
            if enrich_firecrawl:
                md = _firecrawl_scrape_markdown(url)
                if md:
                    brief = f"{snippet}\nURL: {url}\n\n{md[:1800]}"
            cpm, burn, min_views = _extract_metrics_from_text(f"{title}\n{brief}")
            audit = audit_campaign_rules(
                title=title,
                brief_text=brief,
                cpm_usd=cpm,
                burn_rate_pct=burn,
                min_views=min_views,
            )
            cid = f"{platform}_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}"
            upsert_res = upsert_campaign(
                campaign_id=cid,
                platform=platform,
                title=title,
                cpm_usd=cpm,
                burn_rate_pct=burn,
                geo_lock="AR_ELIGIBLE" if audit["eligible_from_ar"] else "GEO_BLOCKED",
                voice_policy=audit["voice_policy"],
                brief_text=brief[:2400],
                verdict="PENDING_HITL",
                db_path=db_path,
            )
            results.append(
                {
                    "campaign_id": cid,
                    "platform": platform,
                    "title": title,
                    "url": url,
                    "cpm_usd": cpm,
                    "burn_rate_pct": burn,
                    "burn_delta_pct": upsert_res["burn_delta_pct"],
                    "verdict": upsert_res["verdict"],
                    "recommendation": audit["verdict_recommendation"],
                    "eligible_from_ar": audit["eligible_from_ar"],
                    "voice_policy": audit["voice_policy"],
                    "risk_flags": audit["risk_flags"],
                }
            )

    insert_telemetry(
        "bounty_radar",
        "bounties_scanned",
        {"discovered": len(results), "platforms": sorted({r["platform"] for r in results})},
        db_path=db_path,
    )
    return results


def get_bounties(verdict: str | None = None, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return list_campaigns(verdict=verdict, db_path=db_path)


def approve_bounty(campaign_id: str, verdict: str = "APPROVED", db_path: Path = DEFAULT_DB_PATH) -> bool:
    ok = set_campaign_verdict(campaign_id, verdict, db_path=db_path)
    if ok:
        insert_telemetry("bounty_radar", "bounty_hitl_decision", {"campaign_id": campaign_id, "verdict": verdict}, db_path=db_path)
    return ok
