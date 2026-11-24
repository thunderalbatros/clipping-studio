"""Bounty Radar & CPM Campaign Auditor (Whop Content Rewards, Clipping.net, Vyro, ClipBoom).

Read-only discovery via Serper.dev + optional Firecrawl (zero login scraping, zero auto-apply)
PLUS deterministic HITL `bounty import` for verified campaign briefs.
Hard validation rejects non-campaign noise URLs (blogs, YouTube videos, App/Play Stores, bare homepages)
and any record with `cpm_usd <= 0`. Prioritizes `global` / `latam` (YT Shorts/X) over `tier1_60pct`
geo-locked TikTok/IG campaigns for Argentina-based production.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import safehttpx
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.db import (
    DEFAULT_DB_PATH,
    delete_campaign,
    insert_telemetry,
    list_campaigns,
    set_campaign_verdict,
    upsert_campaign,
)
from src.miner import PROJECT_ROOT
from src.schemas import AudioPolicy

GeoRequirement = Literal["global", "tier1_60pct", "latam"]
VALID_GEO_REQUIREMENTS = frozenset({"global", "tier1_60pct", "latam"})
VALID_AUDIO_POLICIES = frozenset({"strict_clean", "sound_campaign", "keep_original"})

_CPM_RE = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)\s*(?:1k|1,000|k\b|1000\s*views)", re.IGNORECASE)
_BURN_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%\s*(?:paid|claimed|used|spent|funded|burned)", re.IGNORECASE)
_MIN_VIEWS_RE = re.compile(r"(?:min(?:imum)?\s*views?[:\s]*)([0-9][0-9,\.]*\s*[km]?)", re.IGNORECASE)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SOUND_CAMPAIGN_RE = re.compile(
    r"\b(?:official\s+sound|use\s+(?:this\s+|the\s+)?sound|campaign\s+sound|sync\s+sound)\b",
    re.IGNORECASE,
)

_FORBIDDEN_SSRF_HOSTS = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "instance-data",
        "169.254.169.254",
    }
)


def _is_forbidden_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        return True
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        return True
    return not safehttpx.is_public_ip(str(addr))


def validate_public_http_url(raw_url: str, *, resolve_dns: bool = True) -> tuple[bool, str]:
    """Enforce anti-SSRF rules (scheme, credentials, literal private/metadata IPs, and DNS resolution)."""
    clean = (raw_url or "").strip()
    if not clean:
        return False, "empty_url"
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"}:
        return False, f"invalid_scheme:{parsed.scheme}"
    if parsed.username or parsed.password:
        return False, "embedded_url_credentials_forbidden"
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _FORBIDDEN_SSRF_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        return False, f"ssrf_forbidden_host:{host or 'empty'}"

    # Literal IPv4 / IPv6 check
    try:
        ipaddress.ip_address(host)
        if _is_forbidden_ip(host):
            return False, f"ssrf_non_public_ip:{host}"
        return True, "public_ip_ok"
    except ValueError:
        pass

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except OSError:
            return False, f"dns_resolution_failed:{host}"
        if not infos:
            return False, f"dns_empty:{host}"
        for info in infos:
            sockaddr = info[4]
            ip_tok = str(sockaddr[0])
            if _is_forbidden_ip(ip_tok):
                return False, f"ssrf_dns_non_public_ip:{ip_tok}"

    return True, "public_url_ok"


def fetch_public_url_safe(url: str, timeout: float = 12.0) -> str:
    """Fetch a public campaign URL via `safehttpx` (blocks SSRF, DNS rebinding, and private redirects; timeout <= 20s)."""
    eff_timeout = min(20.0, max(1.0, float(timeout)))
    ok, reason = validate_public_http_url(url, resolve_dns=True)
    if not ok:
        raise ValueError(f"SSRF guard blocked URL ({reason}): {url}")

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        retry=retry_if_exception_type((OSError, TimeoutError)),
    )
    async def _do_get() -> Any:
        return await safehttpx.get(url, timeout=eff_timeout, follow_redirects=True)

    resp: Any
    try:
        asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            resp = pool.submit(asyncio.run, _do_get()).result()
    except RuntimeError:
        resp = asyncio.run(_do_get())

    if int(getattr(resp, "status_code", 0)) != 200:
        return ""
    return str(getattr(resp, "text", "") or "")[:8000]

_NOISE_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "play.google.com",
    "apps.apple.com",
    "ssemble.com",
    "medium.com",
    "reddit.com",
    "quora.com",
    "wikipedia.org",
    "trustpilot.com",
    "g2.com",
    "capterra.com",
    "forbes.com",
    "techcrunch.com",
    "linkedin.com",
)
_NOISE_PATH_MARKERS = (
    "/blog",
    "/articles",
    "/article",
    "/news",
    "/brands",
    "/pricing",
    "/about",
    "/faq",
    "/privacy",
    "/terms",
    "/careers",
    "/how-to",
    "/best-clipping",
    "/guide",
)
_NOISE_TITLE_MARKERS = (
    "apps on google play",
    "app store - apple",
    "i tried every clipping platform",
    "best clipping platforms",
    "how to run a",
    "clipping campaigns for brands",
    "the original & largest clipping network",
)
_KNOWN_BOUNTY_DOMAINS = (
    "whop.com",
    "clipping.net",
    "vyro.com",
    "vyro.gg",
    "clipboom.com",
    "clipboom.app",
    "clipster.gg",
    "bounty.gg",
)

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
_TIER1_AUDIENCE_MARKERS = (
    "60% us",
    "50% us",
    "tier-1 audience",
    "tier 1 audience",
    "us viewers only",
    "us/uk/ca audience",
    "geo-lock",
    "us audience required",
)
_LATAM_MARKERS = (
    "latam",
    "argentina",
    "spanish",
    "español",
    "hispano",
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
    ("whop", 'site:whop.com/ "content rewards" ("$/1k" OR "per 1,000" OR "per 1k") ("gaming" OR "GTA" OR "clipping")'),
    ("clipping_net", 'site:clipping.net ("$/1k" OR "per 1k" OR "CPM") ("gaming" OR "GTA" OR "campaign")'),
    ("vyro", 'site:vyro.com ("$/1k" OR "per 1k" OR "CPM") ("gaming" OR "clipping")'),
    ("clipboom", 'site:clipboom.com ("$/1k" OR "per 1k" OR "CPM") ("gaming" OR "clipping")'),
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


def classify_geo_requirement(
    title: str,
    brief_text: str,
    geo_lock: str = "",
    explicit_geo: str | None = None,
) -> GeoRequirement:
    """Classify campaign into `global` | `tier1_60pct` | `latam` for Argentina operator prioritization."""
    if explicit_geo:
        g = explicit_geo.strip().lower()
        if g == "global":
            return "global"
        if g == "tier1_60pct":
            return "tier1_60pct"
        if g == "latam":
            return "latam"
    hay = f"{title}\n{brief_text}\n{geo_lock}".lower()
    if any(m in hay for m in _GEO_BLOCK_MARKERS) or any(m in hay for m in _TIER1_AUDIENCE_MARKERS):
        return "tier1_60pct"
    if any(m in hay for m in _LATAM_MARKERS):
        return "latam"
    return "global"


def is_valid_campaign_record(
    url: str,
    title: str,
    cpm_usd: float,
    *,
    require_known_domain: bool = False,
) -> tuple[bool, str]:
    """Strict deterministic filter rejecting non-campaign noise URLs and `cpm_usd <= 0`."""
    if float(cpm_usd) <= 0.0:
        return False, f"invalid_cpm:{cpm_usd}"

    raw_url = url.strip()
    if not raw_url:
        return False, "missing_campaign_url"
    parsed = urlparse(raw_url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = (parsed.path or "").strip().lower()

    if not host or parsed.scheme not in {"http", "https"}:
        return False, f"invalid_url_scheme:{raw_url}"
    ssrf_ok, ssrf_reason = validate_public_http_url(raw_url, resolve_dns=False)
    if not ssrf_ok:
        return False, ssrf_reason
    if any(host == d or host.endswith("." + d) for d in _NOISE_DOMAINS):
        return False, f"noise_domain:{host}"
    if path in {"", "/"}:
        return False, f"bare_homepage_not_campaign:{raw_url}"
    if any(marker in path for marker in _NOISE_PATH_MARKERS):
        return False, f"noise_path:{path}"

    title_low = title.strip().lower()
    if not title_low or title_low in {"articles", "blog", "home", "clipping"}:
        return False, f"generic_non_campaign_title:{title}"
    if any(m in title_low for m in _NOISE_TITLE_MARKERS):
        return False, f"noise_title:{title}"

    if require_known_domain and not any(host == d or host.endswith("." + d) for d in _KNOWN_BOUNTY_DOMAINS):
        return False, f"unrecognized_bounty_host:{host}"

    return True, "valid_campaign"


def audit_campaign_rules(
    title: str,
    brief_text: str,
    cpm_usd: float = 0.0,
    burn_rate_pct: float = 0.0,
    geo_lock: str = "",
    min_views: int = 0,
    voice_policy: str = "none",
    geo_requirement: str | None = None,
    platforms: str = "",
) -> dict[str, Any]:
    """Deterministic bounty risk, Argentina geo-priority & eligibility audit."""
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

    detected_geo = classify_geo_requirement(title, brief_text, geo_lock=geo_lock, explicit_geo=geo_requirement)
    plat_low = platforms.lower()
    if detected_geo == "tier1_60pct" and any(p in plat_low for p in ("tiktok", "instagram")):
        risk_flags.append("GEO_LOCK_RISK:tiktok_ig_tier1_from_ar")

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

    ar_priority = 100 if detected_geo == "global" else (85 if detected_geo == "latam" else 35)
    if any(p in plat_low for p in ("youtube_shorts", "youtube", "x")):
        ar_priority += 10

    return {
        "eligible_from_ar": eligible_from_ar,
        "geo_requirement": detected_geo,
        "ar_priority_score": ar_priority,
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


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
)
def _post_with_retry(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 15) -> Any:
    eff_timeout = min(20, max(2, int(timeout)))
    return curl_requests.post(url, headers=headers, json=payload, timeout=eff_timeout)


def _serper_search(query: str, num: int = 5) -> list[dict[str, Any]]:
    api_key = (os.getenv("SERPER_API_KEY") or "").strip()
    if not api_key:
        return []
    try:
        resp = _post_with_retry(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            payload={"q": query, "num": num},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return list((resp.json() or {}).get("organic") or [])
    except Exception:
        return []


def _firecrawl_scrape_markdown(url: str) -> str:
    ok_ssrf, _ = validate_public_http_url(url, resolve_dns=True)
    if not ok_ssrf:
        return ""
    api_key = (os.getenv("FIRECRAWL_API_KEY") or "").strip()
    if not api_key:
        return ""
    try:
        resp = _post_with_retry(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            payload={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=18,
        )
        if resp.status_code != 200:
            return ""
        data = (resp.json() or {}).get("data") or {}
        return str(data.get("markdown") or "")[:4000]
    except Exception:
        return ""


def purge_junk_campaigns(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Remove non-campaign junk rows (blogs, YouTube videos, App Stores, homepages, cpm_usd<=0) from studio.db."""
    rows = list_campaigns(verdict=None, geo_priority=False, db_path=db_path)
    removed: list[dict[str, Any]] = []
    kept: list[str] = []
    for row in rows:
        cid = str(row["campaign_id"])
        title = str(row.get("title") or "")
        cpm = float(row.get("cpm_usd") or 0.0)
        url = str(row.get("url") or "").strip()
        if not url:
            m_url = _URL_IN_TEXT_RE.search(str(row.get("brief_text") or ""))
            url = m_url.group(0) if m_url else ""
        ok, reason = is_valid_campaign_record(url, title, cpm)
        if not ok:
            delete_campaign(cid, db_path=db_path)
            removed.append({"campaign_id": cid, "title": title, "cpm_usd": cpm, "reason": reason})
        else:
            kept.append(cid)
    return {"removed_count": len(removed), "kept_count": len(kept), "removed": removed, "kept": kept}


def import_bounty_hitl(
    *,
    platform: str,
    title: str,
    url: str,
    cpm_usd: float,
    burn_rate_pct: float = 0.0,
    geo_requirement: str = "global",
    platforms: str = "youtube_shorts,x",
    audio_policy: AudioPolicy = "strict_clean",
    brief_text: str = "",
    voice_policy: str = "none",
    verdict: str = "PENDING_HITL",
    campaign_id: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Deterministically validate and persist a real HITL bounty campaign into `runs/studio.db`."""
    ok, reason = is_valid_campaign_record(url, title, cpm_usd, require_known_domain=False)
    if not ok:
        raise ValueError(f"Rejected invalid bounty campaign: {reason}")
    geo_clean = geo_requirement.strip().lower()
    if geo_clean not in VALID_GEO_REQUIREMENTS:
        raise ValueError(f"Invalid geo_requirement {geo_requirement!r}; expected one of {sorted(VALID_GEO_REQUIREMENTS)}")
    if audio_policy not in VALID_AUDIO_POLICIES:
        raise ValueError(f"Invalid audio_policy {audio_policy!r}; expected one of {sorted(VALID_AUDIO_POLICIES)}")

    full_brief = f"{brief_text.strip()}\nURL: {url.strip()}".strip()
    audit = audit_campaign_rules(
        title=title,
        brief_text=full_brief,
        cpm_usd=cpm_usd,
        burn_rate_pct=burn_rate_pct,
        voice_policy=voice_policy,
        geo_requirement=geo_clean,
        platforms=platforms,
    )
    cid = campaign_id or f"{platform.strip().lower()}_{hashlib.sha1(url.strip().encode('utf-8'), usedforsecurity=False).hexdigest()[:10]}"
    upsert_res = upsert_campaign(
        campaign_id=cid,
        platform=platform.strip().lower(),
        title=title.strip(),
        cpm_usd=round(float(cpm_usd), 2),
        burn_rate_pct=round(max(0.0, min(100.0, float(burn_rate_pct))), 2),
        geo_lock="AR_ELIGIBLE" if audit["eligible_from_ar"] else "GEO_BLOCKED",
        voice_policy=audit["voice_policy"],
        brief_text=full_brief[:2400],
        verdict=verdict,
        db_path=db_path,
        geo_requirement=audit["geo_requirement"],
        platforms=platforms.strip(),
        audio_policy=audio_policy,
        url=url.strip(),
    )
    insert_telemetry(
        "bounty_radar",
        "bounty_imported_hitl",
        {
            "campaign_id": cid,
            "platform": platform.strip().lower(),
            "cpm_usd": cpm_usd,
            "geo_requirement": audit["geo_requirement"],
            "audio_policy": audio_policy,
        },
        db_path=db_path,
    )
    return {
        "campaign_id": cid,
        "platform": platform.strip().lower(),
        "title": title.strip(),
        "url": url.strip(),
        "cpm_usd": round(float(cpm_usd), 2),
        "burn_rate_pct": round(float(burn_rate_pct), 2),
        "geo_requirement": audit["geo_requirement"],
        "ar_priority_score": audit["ar_priority_score"],
        "platforms": platforms.strip(),
        "audio_policy": audio_policy,
        "voice_policy": audit["voice_policy"],
        "verdict": upsert_res["verdict"],
        "recommendation": audit["verdict_recommendation"],
        "eligible_from_ar": audit["eligible_from_ar"],
        "risk_flags": audit["risk_flags"],
    }


def scan_bounties(
    max_per_platform: int = 4,
    enrich_firecrawl: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Discover real public CPM clipping campaigns across Whop, Clipping.net, Vyro & ClipBoom -> SQLite.

    Strictly rejects blogs, YouTube videos, App Stores, bare homepages, and any result with `cpm_usd <= 0`.
    """
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=True)
    results: list[dict[str, Any]] = []
    rejected_noise = 0

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
            ok, _reason = is_valid_campaign_record(url, title, cpm, require_known_domain=True)
            if not ok:
                rejected_noise += 1
                continue

            audio_pol: AudioPolicy = "sound_campaign" if _SOUND_CAMPAIGN_RE.search(f"{title} {brief}") else "strict_clean"
            audit = audit_campaign_rules(
                title=title,
                brief_text=brief,
                cpm_usd=cpm,
                burn_rate_pct=burn,
                min_views=min_views,
            )
            cid = f"{platform}_{hashlib.sha1(url.encode('utf-8'), usedforsecurity=False).hexdigest()[:10]}"
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
                geo_requirement=audit["geo_requirement"],
                platforms="youtube_shorts,tiktok,instagram_reels,x",
                audio_policy=audio_pol,
                url=url,
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
                    "geo_requirement": audit["geo_requirement"],
                    "ar_priority_score": audit["ar_priority_score"],
                    "audio_policy": audio_pol,
                    "verdict": upsert_res["verdict"],
                    "recommendation": audit["verdict_recommendation"],
                    "eligible_from_ar": audit["eligible_from_ar"],
                    "voice_policy": audit["voice_policy"],
                    "risk_flags": audit["risk_flags"],
                }
            )

    results.sort(key=lambda r: (int(r["ar_priority_score"]), float(r["cpm_usd"])), reverse=True)
    insert_telemetry(
        "bounty_radar",
        "bounties_scanned",
        {
            "discovered": len(results),
            "rejected_noise": rejected_noise,
            "platforms": sorted({r["platform"] for r in results}),
        },
        db_path=db_path,
    )
    return results


def get_bounties(verdict: str | None = None, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    return list_campaigns(verdict=verdict, geo_priority=True, db_path=db_path)


def approve_bounty(campaign_id: str, verdict: str = "APPROVED", db_path: Path = DEFAULT_DB_PATH) -> bool:
    ok = set_campaign_verdict(campaign_id, verdict, db_path=db_path)
    if ok:
        insert_telemetry(
            "bounty_radar",
            "bounty_hitl_decision",
            {"campaign_id": campaign_id, "verdict": verdict},
            db_path=db_path,
        )
    return ok
