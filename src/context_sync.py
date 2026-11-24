"""Generic Multi-Niche Live Context & Lore Synchronizer (`v4.0-B1`).

Supports `cli context sync --niche <niche_id>` (and alias `cli lore sync --game "GTA VI"`):
- Queries MediaWiki API (`gta.fandom.com/api.php` or any `NichePack.lore_source` MediaWiki endpoint)
  via SSRF-hardened `safehttpx` (`timeout <= 15s`) + `tenacity` exponential backoff.
- Strips HTML/navigation/wikitext noise, encapsulates untrusted external wiki extracts via
  `wrap_untrusted_source_data`, updates `context/gta6_lore_index.json` idempotently with Day 5+
  missions, endings (`BuckMe`), 6-star wanted/disguise mechanics, Leonida locations, DMCA-risk
  radio stations, and Easter eggs, and syncs directly to Qdrant Cloud `clipping_studio_empirical`.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import safehttpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.bounties import validate_public_http_url
from src.llm_router import wrap_untrusted_source_data
from src.logging_utils import get_logger
from src.niche_packs import DEFAULT_NICHE_ID, NichePack, load_niche_pack

PROJECT_ROOT = Path(__file__).resolve().parent.parent
log = get_logger("context_sync")

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WIKI_REF_RE = re.compile(r"\[\d+\]|\[edit\]|\[citation needed\]", re.IGNORECASE)
_WIKI_LINK_PIPE_RE = re.compile(r"\[\[(?:[^|\]]+\|)?([^\]]+)\]\]")
_WIKI_FILE_CAT_RE = re.compile(r"\[\[(?:File|Image|Category):[^\]]+\]\]", re.IGNORECASE)
_MULTI_WS_RE = re.compile(r"\s+")


def _strip_wiki_templates(text: str) -> str:
    """Remove nested `{{...}}` MediaWiki infobox/template blocks cleanly."""
    out: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if text[i : i + 2] == "{{":
            depth += 1
            i += 2
        elif text[i : i + 2] == "}}" and depth > 0:
            depth -= 1
            i += 2
        else:
            if depth == 0:
                out.append(text[i])
            i += 1
    return "".join(out)


def clean_wiki_extract(raw_text: str, *, max_chars: int = 1200) -> str:
    """Strip HTML tags, MediaWiki infoboxes/links/markup, and excess whitespace from extracts."""
    txt = html.unescape(raw_text or "")
    txt = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^/>]*/>", " ", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = _strip_wiki_templates(txt)
    txt = _WIKI_FILE_CAT_RE.sub(" ", txt)
    txt = _WIKI_LINK_PIPE_RE.sub(r"\1", txt)
    txt = txt.replace("'''", "").replace("''", "")
    txt = _HTML_TAG_RE.sub(" ", txt)
    txt = _WIKI_REF_RE.sub("", txt)
    cleaned_lines: list[str] = []
    for ln in txt.splitlines():
        s = _MULTI_WS_RE.sub(" ", ln).strip()
        if len(s) < 20 or s.startswith(("=", "|", "!", "{|", "|}")):
            continue
        if s.lower().startswith(("see also", "references", "external links", "navigation", "category:")):
            break
        cleaned_lines.append(s)
    joined = " ".join(cleaned_lines).strip()
    if len(joined) > max_chars:
        joined = joined[: max_chars - 3].rsplit(" ", 1)[0] + "..."
    return joined


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
    retry=retry_if_exception_type((OSError, TimeoutError)),
)
def _safe_get_json(url: str, timeout_sec: float = 12.0) -> dict[str, Any]:
    eff_timeout = min(20.0, max(2.0, float(timeout_sec)))
    ok, reason = validate_public_http_url(url, resolve_dns=True)
    if not ok:
        raise ValueError(f"SSRF guard blocked lore sync URL ({reason}): {url}")

    async def _fetch() -> Any:
        return await safehttpx.get(
            url,
            timeout=eff_timeout,
            headers={"User-Agent": "ClippingStudioContextSync/4.0 (read-only research)"},
        )

    try:
        asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            resp = pool.submit(asyncio.run, _fetch()).result()
    except RuntimeError:
        resp = asyncio.run(_fetch())

    if int(getattr(resp, "status_code", 0)) != 200:
        return {}
    data = resp.json()
    return data if isinstance(data, dict) else {}


def fetch_mediawiki_extracts(
    endpoint_url: str,
    titles: list[str],
    *,
    search_terms: list[str] | None = None,
    timeout_sec: float = 12.0,
    http_get_json_fn: Any | None = None,
) -> list[dict[str, str]]:
    """Fetch clean plain-text extracts from a MediaWiki `api.php` endpoint with SSRF + timeout guards."""
    getter = http_get_json_fn or _safe_get_json
    discovered_titles: list[str] = list(dict.fromkeys(t.strip() for t in titles if t.strip()))

    for term in (search_terms or [])[:4]:
        q_params = urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": term,
                "srlimit": 3,
                "format": "json",
            }
        )
        try:
            s_data = getter(f"{endpoint_url}?{q_params}", timeout_sec)
            for hit in ((s_data.get("query") or {}).get("search") or []):
                t_name = str(hit.get("title") or "").strip()
                if t_name and t_name not in discovered_titles:
                    discovered_titles.append(t_name)
        except Exception as exc:
            log.warning("mediawiki_search_failed", term=term, error=str(exc)[:160])

    results: list[dict[str, str]] = []
    batch_size = 6
    base_wiki = endpoint_url.split("/api.php")[0]
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")

    for i in range(0, min(len(discovered_titles), 18), batch_size):
        batch = discovered_titles[i : i + batch_size]
        if not batch:
            continue
        p_params = urlencode(
            {
                "action": "query",
                "prop": "extracts|revisions",
                "exintro": "1",
                "explaintext": "1",
                "rvprop": "content",
                "rvslots": "main",
                "rvsection": "0",
                "redirects": "1",
                "titles": "|".join(batch),
                "format": "json",
            }
        )
        try:
            p_data = getter(f"{endpoint_url}?{p_params}", timeout_sec)
        except Exception as exc:
            log.warning("mediawiki_extract_failed", batch=batch, error=str(exc)[:160])
            continue

        pages = ((p_data.get("query") or {}).get("pages") or {})
        if not isinstance(pages, dict):
            continue
        for page_id, page_obj in pages.items():
            if str(page_id).startswith("-") or not isinstance(page_obj, dict):
                continue
            title = str(page_obj.get("title") or "").strip()
            raw_body = str(page_obj.get("extract") or "")
            if not raw_body.strip():
                revs = page_obj.get("revisions") or []
                if isinstance(revs, list) and revs and isinstance(revs[0], dict):
                    slots = revs[0].get("slots") or {}
                    main_slot = slots.get("main") if isinstance(slots, dict) else {}
                    raw_body = str((main_slot or {}).get("*") or (main_slot or {}).get("content") or revs[0].get("*") or "")
            extract = clean_wiki_extract(raw_body)
            if not title or len(extract) < 45:
                continue
            slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]
            page_url = f"{base_wiki}/wiki/{title.replace(' ', '_')}"
            wrapped = wrap_untrusted_source_data(f"mediawiki_{slug}", extract)
            c_hash = hashlib.sha256(f"{title}|{extract}".encode()).hexdigest()
            results.append(
                {
                    "id": f"wiki_{slug}",
                    "title": title,
                    "source_url": page_url,
                    "effective_date": today_iso,
                    "clean_extract": extract,
                    "wrapped_untrusted": wrapped,
                    "content_hash": c_hash,
                }
            )
    return results


def _curated_gta6_day5_expansions(today_iso: str) -> dict[str, Any]:
    """Structured Day 5+ GTA VI expansions (missions/endings/BuckMe, 6-star/disguises, Leonida POIs, DMCA radios)."""
    return {
        "endings_and_social_systems": [
            {
                "id": "sys_buckme_social",
                "title": "BuckMe In-Game Creator & Bounty App",
                "keywords": ["buckme", "in-game livestream", "bounty app", "viral challenge", "clout meter", "leonida man"],
                "summary": "GTA VI's in-world viral video & stunt-bounty feed ('BuckMe') where NPCs record chaotic street stunts, police chases, and endgame fame/infamy reactions.",
                "effective_date": today_iso,
            },
            {
                "id": "ending_dual_trust_paths",
                "title": "Chapter 5 Endgame Paths ('The End of the Road' — Partner Trust vs. Solo Cut)",
                "keywords": ["the end of the road", "final heist", "jason and lucia ending", "betrayal", "ankle monitor", "keys escape"],
                "summary": "High-stakes endgame branches driven by Jason Duval and Lucia Caminos trust decisions after the Sinfrontera & VCIA crackdowns—prime emotional reaction clips.",
                "effective_date": today_iso,
            },
        ],
        "gameplay_systems_day5": [
            {
                "id": "sys_6_star_disguises",
                "title": "6-Star Wanted Escalation, Tactical Recognition & Disguise / Plate-Swap Evasion",
                "keywords": ["6 stars", "six stars", "military", "police recognition", "vehicle description", "change clothes", "disguise", "license plate", "cctv"],
                "summary": "Leonida law enforcement tracks last-seen vehicle description, license plates, and outfit witnesses before locking 6-star tactical/military pursuit; swapping clothes/vehicles out of sight breaks visual lock.",
                "effective_date": today_iso,
            }
        ],
        "dmca_risk_radio_stations": [
            {
                "id": "radio_flash_fm_vrock_dmca",
                "title": "Vice City Licensed Radio & Nightclub Audio (High Content ID / DMCA Risk)",
                "keywords": ["flash fm", "emotion 98.3", "v-rock", "fever 105", "only raw records", "boobie ike club", "car radio"],
                "dmca_policy_action": "Strictly enforce YAMNet music_prob_peak < 0.45 under strict_clean / sound_campaign; trim around vehicle radio segments or route slice to GPU BS-RoFormer vocal isolation in Phase B2.",
                "effective_date": today_iso,
            }
        ],
        "leonida_key_locations_expanded": [
            {"name": "Leonida Penitentiary", "region": "Leonida", "clip_triggers": ["prologue", "discharge", "prison glitch"]},
            {"name": "Vice-Dale County & Ocean Beach", "region": "Vice City", "clip_triggers": ["neon strip", "supercar chase", "buckme"]},
            {"name": "Port Gellhorn Docks & Motels", "region": "Port Gellhorn", "clip_triggers": ["robbery", "coast guard", "ambush"]},
            {"name": "Grassrivers Airboat Marshes", "region": "Grassrivers", "clip_triggers": ["alligator", "skunk ape", "airboat"]},
            {"name": "Leonida Keys Causeway", "region": "Leonida Keys", "clip_triggers": ["bridge jump", "boat highway", "smuggler"]},
            {"name": "Mount Kalaga National Park", "region": "Mount Kalaga", "clip_triggers": ["bigfoot", "pink meteorite", "offroad cliff"]},
        ],
    }


def sync_niche_context(
    niche_id: str = DEFAULT_NICHE_ID,
    *,
    subject_override: str | None = None,
    project_root: Path = PROJECT_ROOT,
    sync_cloud: bool = True,
    http_get_json_fn: Any | None = None,
) -> dict[str, Any]:
    """Synchronize live external lore/context for `niche_id`, update local SSOT index JSON, and push to Qdrant Cloud."""
    pack: NichePack = load_niche_pack(niche_id, niches_dir=project_root / "config" / "niches")
    lore_spec = pack.lore_source
    today_iso = datetime.now(UTC).strftime("%Y-%m-%d")

    if lore_spec is None or lore_spec.kind == "none" or not lore_spec.endpoint_url:
        return {
            "niche_id": pack.niche_id,
            "status": "no_external_lore_source",
            "fetched_wiki_pages": 0,
            "cloud_sync": None,
        }

    subject = (subject_override or lore_spec.default_subject or pack.display_name).strip()
    search_terms = [
        f"{subject} Leonida locations missions",
        f"{subject} radio stations wanted level",
    ]
    wiki_pages = fetch_mediawiki_extracts(
        lore_spec.endpoint_url,
        list(lore_spec.seed_pages),
        search_terms=search_terms,
        http_get_json_fn=http_get_json_fn,
    )

    index_rel = lore_spec.index_file or f"context/{pack.niche_id}_lore_index.json"
    index_path = (project_root / index_rel).resolve()
    if not index_path.is_relative_to(project_root.resolve()):
        raise ValueError(f"Refusing to write lore index outside workspace: {index_path}")

    existing: dict[str, Any] = {}
    if index_path.is_file():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    existing["schema_version"] = "2.0"
    existing["updated_at"] = today_iso
    if pack.niche_id == "gaming_gta6":
        expansions = _curated_gta6_day5_expansions(today_iso)
        for k, v in expansions.items():
            existing[k] = v

    if wiki_pages:
        by_id: dict[str, dict[str, str]] = {
            str(it.get("id")): it
            for it in (existing.get("live_wiki_extracts") or [])
            if isinstance(it, dict) and it.get("id")
        }
        for wp in wiki_pages:
            by_id[wp["id"]] = wp
        existing["live_wiki_extracts"] = [by_id[k] for k in sorted(by_id)]

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cloud_report: dict[str, Any] | None = None
    if sync_cloud:
        from src.qdrant_knowledge import sync_empirical_cloud

        cloud_report = sync_empirical_cloud(project_root=project_root, filter_niche=pack.niche_id)

    report = {
        "niche_id": pack.niche_id,
        "subject": subject,
        "index_file": index_rel,
        "fetched_wiki_pages": len(wiki_pages),
        "total_wiki_extracts": len(existing.get("live_wiki_extracts") or []),
        "cloud_sync": cloud_report,
    }
    log.info("context_sync_completed", niche_id=pack.niche_id, fetched_wiki_pages=len(wiki_pages))
    return report
