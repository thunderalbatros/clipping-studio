"""Qdrant Cloud SSOT Engine (`clipping_studio_empirical`, Named Vectors + Governed Ingestion, `v4.0-B1`).

Architecture & Non-Negotiable Guarantees:
1. Dedicated writable collection: `QDRANT_COLLECTION_CLIPPING_V2 = "clipping_studio_empirical"` ONLY.
   Hard write-guard (`_assert_writable_collection`) raises `PermissionError` if any code attempts to
   create/upsert/delete on external read-only collections (`centinela_knowledge`, `hunthub_omni_rag_v5`).
2. Named Vectors:
   - `text_768`: `size=768`, `distance=Cosine` (Vertex AI `text-embedding-005`, multilingual EN/ES+, 0 MB local RAM).
   - `multimodal_768`: `size=768`, `distance=Cosine` (reserved for `google/embeddinggemma-2` in Phase B2).
3. Mandatory Payload Indexes: `niche`, `language`, `domain`, `platform`, `source_type`, `effective_date`, `content_hash`, `doc_id`.
4. Idempotent `content_hash` Sync: `sync_empirical_cloud()` scrolls existing `(point_id, content_hash)` pairs first,
   embeds & upserts ONLY new or modified chunks, and prunes stale removed chunk IDs—guaranteeing zero duplicate points
   across repeated runs.
5. Explicit Timeouts (`<= 20s`) + `tenacity` exponential backoff on every Qdrant Cloud and Vertex AI network call.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, Field, field_validator
from qdrant_client import QdrantClient, models
from tenacity import retry, stop_after_attempt, wait_exponential

from src.logging_utils import get_logger
from src.niche_packs import load_all_niche_packs
from src.settings import load_env_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QDRANT_COLLECTION_CLIPPING_V2 = "clipping_studio_empirical"
TEXT_VECTOR_NAME = "text_768"
MULTIMODAL_VECTOR_NAME = "multimodal_768"
VECTOR_DIM_768 = 768

REQUIRED_PAYLOAD_INDEX_FIELDS: tuple[str, ...] = (
    "niche",
    "language",
    "domain",
    "platform",
    "source_type",
    "effective_date",
    "content_hash",
    "doc_id",
)

GovernedSourceType = Literal["official_doc", "book", "primary_talk", "creator_profile", "lore"]
Embed768Fn = Callable[[list[str]], list[list[float]]]

log = get_logger("qdrant_knowledge")
_HTML_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_CHUNKER_768_CACHE: Any = None


class GovernedChunkRecord(BaseModel):
    """Strict governed metadata contract (`extra='forbid'`) for every point in `clipping_studio_empirical`."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(min_length=2, max_length=120)
    chunk_id: str = Field(min_length=3, max_length=160)
    niche: str = Field(min_length=2, max_length=48)
    language: str = Field(min_length=2, max_length=10)
    domain: str = Field(min_length=2, max_length=48)
    platform: str = Field(min_length=1, max_length=32)
    source_type: GovernedSourceType
    source_url: str = Field(min_length=4, max_length=400)
    effective_date: str
    content_hash: str = Field(min_length=64, max_length=64)
    heading: str = Field(min_length=2, max_length=220)
    content: str = Field(min_length=20, max_length=2400)

    @field_validator("effective_date")
    @classmethod
    def _validate_iso_date(cls, v: str) -> str:
        clean = v.strip()
        date.fromisoformat(clean)
        return clean


def _assert_writable_collection(collection_name: str) -> None:
    """Enforce Regla 01: ONLY `clipping_studio_empirical` may ever be created, modified, or deleted."""
    if collection_name != QDRANT_COLLECTION_CLIPPING_V2:
        raise PermissionError(
            f"Write blocked by SSOT guard: {collection_name!r} is strictly read-only "
            f"(only {QDRANT_COLLECTION_CLIPPING_V2!r} is writable)."
        )


def deterministic_point_uuid(chunk_id: str) -> str:
    """Derive deterministic UUIDv5 for a canonical `chunk_id` so repeated syncs never duplicate points."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{QDRANT_COLLECTION_CLIPPING_V2}:{chunk_id}"))


def compute_content_hash(
    *,
    niche: str,
    language: str,
    domain: str,
    platform: str,
    source_type: str,
    effective_date: str,
    heading: str,
    content: str,
) -> str:
    norm = f"{niche}|{language}|{domain}|{platform}|{source_type}|{effective_date}|{heading.strip()}|{content.strip()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def strip_html_and_boilerplate(raw: str) -> str:
    """Clean HTML tags, navigation crumbs, and redundant whitespace before chunking."""
    unescaped = html.unescape(raw or "")
    no_tags = _HTML_STRIP_RE.sub(" ", unescaped)
    return _WS_RE.sub(" ", no_tags).strip()


def _get_governed_chunker(max_chars: int = 1100) -> Any:
    global _CHUNKER_768_CACHE
    if _CHUNKER_768_CACHE is None:
        from chonkie import RecursiveChunker, RecursiveRules

        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            _CHUNKER_768_CACHE = RecursiveChunker.from_recipe(
                "markdown",
                lang="en",
                tokenizer="character",
                chunk_size=max_chars,
                min_characters_per_chunk=40,
            )
        except Exception:
            _CHUNKER_768_CACHE = RecursiveChunker(
                tokenizer="character",
                chunk_size=max_chars,
                rules=RecursiveRules(),
                min_characters_per_chunk=40,
            )
    return _CHUNKER_768_CACHE


def chunk_clean_text(text: str, max_chars: int = 1100) -> list[str]:
    clean = strip_html_and_boilerplate(text)
    if not clean:
        return []
    if len(clean) <= max_chars:
        return [clean]
    chunker = _get_governed_chunker(max_chars=max_chars)
    parts = [str(c.text).strip() for c in chunker.chunk(clean) if len(str(c.text).strip()) >= 25]
    return parts or [clean[:max_chars]]


def get_qdrant_empirical_client(timeout_sec: int = 15) -> QdrantClient | None:
    """Instantiate `QdrantClient` with explicit timeout (`<= 20s`) if credentials exist; return None otherwise."""
    env_cfg = load_env_settings()
    url = env_cfg.resolved_qdrant_url
    api_key = env_cfg.qdrant_api_key.strip()
    if not url or not api_key:
        return None
    eff_timeout = min(20, max(3, int(timeout_sec)))
    return QdrantClient(url=url, api_key=api_key, timeout=eff_timeout)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=5.0),
)
def embed_vertex_768_batch(
    texts: list[str],
    *,
    task_type: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"] = "RETRIEVAL_DOCUMENT",
    timeout_sec: float = 18.0,
) -> list[list[float]]:
    """Compute 768-d multilingual embeddings via Vertex AI `text-embedding-005` (`timeout <= 20s`)."""
    if not texts:
        return []
    env_cfg = load_env_settings()
    proj = (os.getenv("GCP_PROJECT_ID") or env_cfg.gcp_project_id).strip()
    loc = (os.getenv("GCP_LOCATION") or env_cfg.gcp_location or "us-central1").strip() or "us-central1"
    if not proj:
        raise RuntimeError("GCP_PROJECT_ID is required for Vertex AI text-embedding-005")

    gac = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if gac and not Path(gac).is_file():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

    from google import genai

    timeout_ms = int(min(20.0, max(3.0, float(timeout_sec))) * 1000)
    client = genai.Client(
        vertexai=True,
        project=proj,
        location=loc,
        http_options=genai_types.HttpOptions(timeout=timeout_ms),
    )

    out_vectors: list[list[float]] = []
    batch_size = 32
    cfg = genai_types.EmbedContentConfig(
        task_type=task_type,
        output_dimensionality=VECTOR_DIM_768,
    )
    for start in range(0, len(texts), batch_size):
        batch_contents: list[Any] = list(texts[start : start + batch_size])
        resp = client.models.embed_content(
            model="text-embedding-005",
            contents=batch_contents,
            config=cfg,
        )
        embs = getattr(resp, "embeddings", None) or []
        if len(embs) != len(batch_contents):
            raise RuntimeError(f"Vertex embed_content returned {len(embs)} vectors for batch of {len(batch_contents)}")
        for emb in embs:
            vals = getattr(emb, "values", None) or []
            out_vectors.append([float(v) for v in vals])
    return out_vectors


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=5.0),
)
def ensure_empirical_collection(
    client: Any,
    collection_name: str = QDRANT_COLLECTION_CLIPPING_V2,
) -> dict[str, Any]:
    """Idempotently create `clipping_studio_empirical` with named vectors (`text_768`, `multimodal_768`) + payload indexes."""
    _assert_writable_collection(collection_name)
    exists = bool(client.collection_exists(collection_name=collection_name))
    created = False
    if not exists:
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                TEXT_VECTOR_NAME: models.VectorParams(
                    size=VECTOR_DIM_768,
                    distance=models.Distance.COSINE,
                ),
                MULTIMODAL_VECTOR_NAME: models.VectorParams(
                    size=VECTOR_DIM_768,
                    distance=models.Distance.COSINE,
                ),
            },
        )
        created = True

    indexes_ensured: list[str] = []
    for field_name in REQUIRED_PAYLOAD_INDEX_FIELDS:
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=models.PayloadSchemaType.KEYWORD,
            wait=True,
        )
        indexes_ensured.append(field_name)

    return {
        "collection": collection_name,
        "created": created,
        "named_vectors": [TEXT_VECTOR_NAME, MULTIMODAL_VECTOR_NAME],
        "payload_indexes": indexes_ensured,
    }


def _make_record(
    *,
    doc_id: str,
    chunk_id: str,
    niche: str,
    language: str,
    domain: str,
    platform: str,
    source_type: GovernedSourceType,
    source_url: str,
    effective_date: str,
    heading: str,
    content: str,
) -> GovernedChunkRecord:
    clean_body = strip_html_and_boilerplate(content)[:2300]
    c_hash = compute_content_hash(
        niche=niche,
        language=language,
        domain=domain,
        platform=platform,
        source_type=source_type,
        effective_date=effective_date,
        heading=heading,
        content=clean_body,
    )
    return GovernedChunkRecord(
        doc_id=doc_id,
        chunk_id=chunk_id,
        niche=niche,
        language=language,
        domain=domain,
        platform=platform,
        source_type=source_type,
        source_url=source_url,
        effective_date=effective_date,
        content_hash=c_hash,
        heading=heading[:210],
        content=clean_body,
    )


def _slug(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")[:42] or "item"


def collect_governed_empirical_chunks(
    project_root: Path = PROJECT_ROOT,
    *,
    filter_niche: str | None = None,
) -> list[GovernedChunkRecord]:
    """Assemble governed chunks for `clipping_studio_empirical` (zero duplication of the 290 classic books)."""
    records: list[GovernedChunkRecord] = []

    # 1. Primary 2026 Platform Docs + RecSys + Clean Primary Talks (`context/governed_corpus/primary_docs_2026.json`)
    corpus_path = project_root / "context" / "governed_corpus" / "primary_docs_2026.json"
    if corpus_path.is_file():
        corpus_data = json.loads(corpus_path.read_text(encoding="utf-8"))
        for doc in corpus_data.get("documents") or []:
            niche = str(doc["niche"]).strip().lower()
            if filter_niche and niche != filter_niche:
                continue
            doc_id = str(doc["doc_id"]).strip()
            parts = chunk_clean_text(str(doc["content"]), max_chars=1150)
            for idx, part in enumerate(parts, start=1):
                head = str(doc["title"]) if len(parts) == 1 else f"{doc['title']} (part {idx})"
                records.append(
                    _make_record(
                        doc_id=doc_id,
                        chunk_id=f"gov:{doc_id}:{idx:02d}",
                        niche=niche,
                        language=str(doc["language"]).strip().lower(),
                        domain=str(doc["domain"]).strip(),
                        platform=str(doc["platform"]).strip().lower(),
                        source_type=doc["source_type"],
                        source_url=str(doc["source_url"]).strip(),
                        effective_date=str(doc["effective_date"]).strip(),
                        heading=head,
                        content=part,
                    )
                )

    # 2. Declarative NichePacks (`config/niches/*.yaml` -> synced unidirectionally to Qdrant Cloud)
    niches_dir = project_root / "config" / "niches"
    if niches_dir.is_dir():
        for nid, pack in load_all_niche_packs(niches_dir=niches_dir).items():
            if filter_niche and nid != filter_niche:
                continue
            summary = (
                f"NichePack '{pack.niche_id}' ({pack.display_name}): {pack.description} "
                f"Languages={pack.languages}, target_platforms={pack.target_platforms}, "
                f"framing_mode={pack.framing_mode}, silence_trim_policy={pack.silence_trim_policy}, "
                f"audio_pipeline={pack.audio_pipeline.model_dump()}, asr={pack.asr_engine_preference}, "
                f"diarization_required={pack.diarization_required}, copyright_guard={pack.copyright_guard}, "
                f"min_qa_overall={pack.min_qa_overall}, qa_rules={pack.visual_presets.qa_special_rules}"
            )
            records.append(
                _make_record(
                    doc_id=f"niche_pack_{nid}",
                    chunk_id=f"niche_pack:{nid}:01",
                    niche=nid,
                    language=pack.languages[0],
                    domain="shortform_craft",
                    platform="multi",
                    source_type="official_doc",
                    source_url=f"https://clipping-studio.local/config/niches/{nid}.yaml",
                    effective_date="2026-11-24",
                    heading=f"NichePack Contract: {pack.display_name} ({nid})",
                    content=summary,
                )
            )

    # 3. Complete 20/20 Creator Editorial Profiles (`context/streamer_profiles.json`, `niche="gaming_gta6"`)
    if filter_niche in {None, "gaming_gta6"}:
        sp_path = project_root / "context" / "streamer_profiles.json"
        if sp_path.is_file():
            sp_data = json.loads(sp_path.read_text(encoding="utf-8"))
            sp_date = str(sp_data.get("updated_at") or "2026-11-24")
            for sid, info in (sp_data.get("streamers") or {}).items():
                if not isinstance(info, dict):
                    continue
                norm_sid = str(sid).strip().lower()
                lang = str(info.get("language") or "en").strip().lower()
                plats = info.get("platforms") or ["twitch"]
                prim_plat = str(plats[0]) if isinstance(plats, list) and plats else "twitch"
                body = (
                    f"Creator {info.get('display_name', sid)} (id={norm_sid}, lang={lang}, tier={info.get('tier')}, platforms={plats}). "
                    f"Archetype: {info.get('archetype')}. Reaction signature: {info.get('reaction_signature')} "
                    f"Optimal 0-3s hook: {info.get('optimal_hook_0_3s')} Pacing: {info.get('pacing_rules')} "
                    f"Captions: {info.get('caption_vocab_notes')} Framing & Cover: {info.get('framing_and_cover_style')} "
                    f"Bounty fit: {info.get('bounty_fit')}"
                )
                records.append(
                    _make_record(
                        doc_id=f"creator_profile_{norm_sid}",
                        chunk_id=f"creator:gaming_gta6:{norm_sid}",
                        niche="gaming_gta6",
                        language=lang,
                        domain="gta6_streamers",
                        platform=prim_plat,
                        source_type="creator_profile",
                        source_url=f"https://clipping-studio.local/creators/{norm_sid}",
                        effective_date=sp_date,
                        heading=f"Creator Editorial Profile: {info.get('display_name', sid)} ({norm_sid})",
                        content=body,
                    )
                )

    # 4. GTA VI Live Lore Index (`context/gta6_lore_index.json`, `niche="gaming_gta6"`, `source_type="lore"`)
    if filter_niche in {None, "gaming_gta6"}:
        lore_path = project_root / "context" / "gta6_lore_index.json"
        if lore_path.is_file():
            lore_data = json.loads(lore_path.read_text(encoding="utf-8"))
            lore_date = str(lore_data.get("updated_at") or lore_data.get("release_date") or "2026-11-24")
            setting = {
                k: lore_data[k]
                for k in ("game", "release_date", "platforms", "setting")
                if k in lore_data
            }
            if setting:
                records.append(
                    _make_record(
                        doc_id="gta6_lore_setting",
                        chunk_id="lore:gaming_gta6:setting_overview",
                        niche="gaming_gta6",
                        language="en",
                        domain="gta6_streamers",
                        platform="multi",
                        source_type="lore",
                        source_url="https://gta.fandom.com/wiki/State_of_Leonida",
                        effective_date=lore_date,
                        heading="GTA VI Lore: State of Leonida Regions & Setting",
                        content=json.dumps(setting, ensure_ascii=False),
                    )
                )

            chars = lore_data.get("characters")
            if isinstance(chars, dict):
                for grp, clist in chars.items():
                    if isinstance(clist, list):
                        for ch_obj in clist:
                            cname = str(ch_obj.get("name") or grp) if isinstance(ch_obj, dict) else str(ch_obj)
                            records.append(
                                _make_record(
                                    doc_id=f"gta6_char_{_slug(cname)}",
                                    chunk_id=f"lore:gaming_gta6:char_{_slug(cname)}",
                                    niche="gaming_gta6",
                                    language="en",
                                    domain="gta6_streamers",
                                    platform="multi",
                                    source_type="lore",
                                    source_url="https://gta.fandom.com/wiki/Grand_Theft_Auto_VI",
                                    effective_date=lore_date,
                                    heading=f"GTA VI Character ({grp}): {cname}",
                                    content=json.dumps(ch_obj, ensure_ascii=False),
                                )
                            )

            for section_key in (
                "chapters",
                "easter_eggs",
                "viral_launch_bugs",
                "endings_and_social_systems",
                "gameplay_systems_day5",
                "dmca_risk_radio_stations",
                "leonida_key_locations_expanded",
                "live_wiki_extracts",
            ):
                items = lore_data.get(section_key)
                if not isinstance(items, list):
                    continue
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        continue
                    it_id = str(item.get("id") or item.get("title") or item.get("name") or f"{section_key}_{idx}")
                    it_title = str(item.get("title") or item.get("name") or it_id)
                    it_url = str(item.get("source_url") or "https://gta.fandom.com/wiki/Grand_Theft_Auto_VI")
                    it_date = str(item.get("effective_date") or lore_date)
                    body_txt = (
                        f"{it_title}: {item.get('clean_extract')}"
                        if item.get("clean_extract")
                        else json.dumps(item, ensure_ascii=False)
                    )
                    records.append(
                        _make_record(
                            doc_id=f"gta6_{_slug(section_key)}_{_slug(it_id)}",
                            chunk_id=f"lore:gaming_gta6:{_slug(section_key)}:{_slug(it_id)}:{idx:02d}",
                            niche="gaming_gta6",
                            language="en",
                            domain="bounties_dmca" if section_key == "dmca_risk_radio_stations" else "gta6_streamers",
                            platform="multi",
                            source_type="lore",
                            source_url=it_url,
                            effective_date=it_date,
                            heading=f"GTA VI Lore ({section_key}): {it_title}",
                            content=body_txt,
                        )
                    )

        # Empirical Viral Style DNA (`context/viral_style_dna.json`, `niche="gaming_gta6"`)
        dna_path = project_root / "context" / "viral_style_dna.json"
        if dna_path.is_file():
            dna_data = json.loads(dna_path.read_text(encoding="utf-8"))
            summary_dna = {
                k: dna_data[k]
                for k in ("sample_count", "empirical_medians", "recommended_targets")
                if k in dna_data
            }
            if summary_dna:
                records.append(
                    _make_record(
                        doc_id="empirical_viral_style_dna_summary",
                        chunk_id="empirical:gaming_gta6:viral_style_dna_targets",
                        niche="gaming_gta6",
                        language="en",
                        domain="shortform_craft",
                        platform="multi",
                        source_type="official_doc",
                        source_url="https://clipping-studio.local/context/viral_style_dna.json",
                        effective_date="2026-11-23",
                        heading="Empirical Viral Style DNA Medians & Pacing Targets",
                        content=json.dumps(summary_dna, ensure_ascii=False),
                    )
                )

    dedup: dict[str, GovernedChunkRecord] = {r.chunk_id: r for r in records}
    return list(dedup.values())


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=5.0),
)
def _scroll_existing_hashes(
    client: Any,
    collection_name: str,
    *,
    filter_niche: str | None = None,
) -> dict[str, tuple[str, str]]:
    """Return `{point_id: (chunk_id, content_hash)}` currently stored in `clipping_studio_empirical`."""
    scroll_filter = (
        models.Filter(must=[models.FieldCondition(key="niche", match=models.MatchValue(value=filter_niche))])
        if filter_niche
        else None
    )
    out: dict[str, tuple[str, str]] = {}
    offset: Any = None
    while True:
        pts, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=["chunk_id", "content_hash"],
            with_vectors=False,
        )
        for pt in pts or []:
            pl = dict(getattr(pt, "payload", None) or {})
            out[str(pt.id)] = (str(pl.get("chunk_id") or ""), str(pl.get("content_hash") or ""))
        if next_offset is None or not pts:
            break
        offset = next_offset
    return out


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=5.0),
)
def _upsert_points_batch(client: Any, collection_name: str, points: list[models.PointStruct]) -> None:
    _assert_writable_collection(collection_name)
    client.upsert(collection_name=collection_name, points=points, wait=True)


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.6, min=0.6, max=5.0),
)
def _delete_points_by_ids(client: Any, collection_name: str, point_ids: list[str]) -> None:
    _assert_writable_collection(collection_name)
    if point_ids:
        client.delete(
            collection_name=collection_name,
            points_selector=models.PointIdsList(points=point_ids),  # type: ignore[arg-type]
            wait=True,
        )


def inspect_empirical_collection(
    *,
    client: Any | None = None,
    collection_name: str = QDRANT_COLLECTION_CLIPPING_V2,
) -> dict[str, Any]:
    """Scroll `clipping_studio_empirical` and return exact point counts broken down by niche, language, source_type, platform, domain."""
    q_client = client or get_qdrant_empirical_client()
    if q_client is None:
        return {"configured": False, "collection": collection_name, "total_points": 0}
    if not q_client.collection_exists(collection_name=collection_name):
        return {"configured": True, "exists": False, "collection": collection_name, "total_points": 0}

    by_niche: dict[str, int] = {}
    by_language: dict[str, int] = {}
    by_source_type: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    unique_hashes: set[str] = set()
    total = 0

    offset: Any = None
    while True:
        pts, next_offset = q_client.scroll(
            collection_name=collection_name,
            limit=256,
            offset=offset,
            with_payload=["niche", "language", "source_type", "platform", "domain", "content_hash"],
            with_vectors=False,
        )
        for pt in pts or []:
            total += 1
            pl = dict(getattr(pt, "payload", None) or {})
            n_val = str(pl.get("niche") or "unknown")
            l_val = str(pl.get("language") or "unknown")
            st_val = str(pl.get("source_type") or "unknown")
            p_val = str(pl.get("platform") or "unknown")
            d_val = str(pl.get("domain") or "unknown")
            ch_val = str(pl.get("content_hash") or "")
            by_niche[n_val] = by_niche.get(n_val, 0) + 1
            by_language[l_val] = by_language.get(l_val, 0) + 1
            by_source_type[st_val] = by_source_type.get(st_val, 0) + 1
            by_platform[p_val] = by_platform.get(p_val, 0) + 1
            by_domain[d_val] = by_domain.get(d_val, 0) + 1
            if ch_val:
                unique_hashes.add(ch_val)
        if next_offset is None or not pts:
            break
        offset = next_offset

    return {
        "configured": True,
        "exists": True,
        "collection": collection_name,
        "total_points": total,
        "unique_content_hashes": len(unique_hashes),
        "by_niche": dict(sorted(by_niche.items())),
        "by_language": dict(sorted(by_language.items())),
        "by_source_type": dict(sorted(by_source_type.items())),
        "by_platform": dict(sorted(by_platform.items())),
        "by_domain": dict(sorted(by_domain.items())),
    }


def sync_empirical_cloud(
    *,
    project_root: Path = PROJECT_ROOT,
    client: Any | None = None,
    embed_768_fn: Embed768Fn | None = None,
    collection_name: str = QDRANT_COLLECTION_CLIPPING_V2,
    filter_niche: str | None = None,
    records_override: list[GovernedChunkRecord] | None = None,
) -> dict[str, Any]:
    """Idempotently synchronize governed empirical knowledge into Qdrant Cloud `clipping_studio_empirical`."""
    _assert_writable_collection(collection_name)
    q_client = client or get_qdrant_empirical_client()
    if q_client is None:
        raise RuntimeError("Qdrant Cloud credentials (QDRANT_URL/QDRANT_HOST + QDRANT_API_KEY) are missing.")

    schema_info = ensure_empirical_collection(q_client, collection_name=collection_name)
    records = (
        records_override
        if records_override is not None
        else collect_governed_empirical_chunks(project_root=project_root, filter_niche=filter_niche)
    )

    existing = _scroll_existing_hashes(q_client, collection_name, filter_niche=filter_niche)
    desired_ids: set[str] = set()
    to_upsert: list[tuple[str, GovernedChunkRecord]] = []
    unchanged = 0

    for rec in records:
        pid = deterministic_point_uuid(rec.chunk_id)
        desired_ids.add(pid)
        prev = existing.get(pid)
        if prev is not None and prev[1] == rec.content_hash:
            unchanged += 1
            continue
        to_upsert.append((pid, rec))

    stale_ids = sorted(set(existing.keys()) - desired_ids)
    if stale_ids:
        _delete_points_by_ids(q_client, collection_name, stale_ids)

    upserted = 0
    if to_upsert:
        texts_for_emb = [f"{r.heading}\n{r.content}" for _, r in to_upsert]
        vecs = (
            embed_768_fn(texts_for_emb)
            if embed_768_fn is not None
            else embed_vertex_768_batch(texts_for_emb, task_type="RETRIEVAL_DOCUMENT")
        )
        batch_sz = 64
        for start in range(0, len(to_upsert), batch_sz):
            sub_items = to_upsert[start : start + batch_sz]
            sub_vecs = vecs[start : start + batch_sz]
            pts = [
                models.PointStruct(
                    id=pid,
                    vector={TEXT_VECTOR_NAME: vec},
                    payload=rec.model_dump(),
                )
                for (pid, rec), vec in zip(sub_items, sub_vecs, strict=True)
            ]
            _upsert_points_batch(q_client, collection_name, pts)
            upserted += len(pts)

    breakdown = inspect_empirical_collection(client=q_client, collection_name=collection_name)
    log.info(
        "qdrant_sync_cloud_completed",
        collection=collection_name,
        total_points=breakdown.get("total_points", 0),
        upserted=upserted,
        unchanged=unchanged,
        deleted_stale=len(stale_ids),
    )
    return {
        "collection": collection_name,
        "collection_created": schema_info["created"],
        "payload_indexes": schema_info["payload_indexes"],
        "source_records": len(records),
        "upserted": upserted,
        "unchanged": unchanged,
        "deleted_stale": len(stale_ids),
        "breakdown": breakdown,
    }


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
)
def _query_points_with_retry(
    client: Any,
    *,
    collection_name: str,
    query_vec: list[float],
    q_filter: models.Filter,
    limit: int,
) -> Any:
    return client.query_points(
        collection_name=collection_name,
        query=query_vec,
        using=TEXT_VECTOR_NAME,
        query_filter=q_filter,
        limit=limit,
        with_payload=True,
    )


def build_empirical_filter(
    *,
    niche: str,
    language: str | None = None,
    domain: str | None = None,
    platform: str | None = None,
) -> models.Filter:
    """Build strict pre-KNN Qdrant payload filter isolating `niche in [active_niche, 'global']` + `language`."""
    clean_niche = (niche or "gaming_gta6").strip().lower()
    allowed_niches = [clean_niche] if clean_niche == "global" else [clean_niche, "global"]
    must_conds: list[Any] = [
        models.FieldCondition(key="niche", match=models.MatchAny(any=allowed_niches)),
    ]
    clean_lang = (language or "").strip().lower()
    if clean_lang and clean_lang != "auto":
        must_conds.append(models.FieldCondition(key="language", match=models.MatchValue(value=clean_lang)))
    if domain:
        must_conds.append(models.FieldCondition(key="domain", match=models.MatchValue(value=domain.strip())))
    if platform:
        must_conds.append(models.FieldCondition(key="platform", match=models.MatchValue(value=platform.strip().lower())))
    return models.Filter(must=must_conds)


def query_empirical_qdrant(
    query: str,
    *,
    niche: str = "gaming_gta6",
    language: str | None = "en",
    domain: str | None = None,
    platform: str | None = None,
    top_k: int = 5,
    client: Any | None = None,
    embed_768_fn: Embed768Fn | None = None,
    collection_name: str = QDRANT_COLLECTION_CLIPPING_V2,
) -> list[dict[str, Any]]:
    """Query `clipping_studio_empirical` (`text_768` named vector) with strict niche & language pre-filtering."""
    if not query.strip():
        return []
    q_client = client or get_qdrant_empirical_client(timeout_sec=12)
    if q_client is None:
        return []

    try:
        q_vec = (
            embed_768_fn([query])[0]
            if embed_768_fn is not None
            else embed_vertex_768_batch([query], task_type="RETRIEVAL_QUERY", timeout_sec=12.0)[0]
        )
        q_filter = build_empirical_filter(niche=niche, language=language, domain=domain, platform=platform)
        resp = _query_points_with_retry(
            q_client,
            collection_name=collection_name,
            query_vec=q_vec,
            q_filter=q_filter,
            limit=max(2, top_k),
        )
    except Exception as exc:
        log.warning("query_empirical_qdrant_fallback", niche=niche, language=language, error=str(exc)[:160])
        return []

    hits: list[dict[str, Any]] = []
    for rank, pt in enumerate(getattr(resp, "points", None) or [], start=1):
        pl = dict(getattr(pt, "payload", None) or {})
        txt = str(pl.get("content") or "").strip()
        if not txt:
            continue
        score = float(getattr(pt, "score", 0.0) or 0.0)
        hits.append(
            {
                "chunk_id": str(pl.get("chunk_id") or f"empirical:{pt.id}"),
                "source": f"qdrant_ssot:{collection_name}",
                "source_path": str(pl.get("source_url") or collection_name),
                "source_type": str(pl.get("source_type") or "official_doc"),
                "niche": str(pl.get("niche") or niche),
                "language": str(pl.get("language") or (language or "en")),
                "platform": str(pl.get("platform") or "multi"),
                "effective_date": str(pl.get("effective_date") or ""),
                "content_hash": str(pl.get("content_hash") or ""),
                "domain": str(pl.get("domain") or (domain or "shortform_craft")),
                "heading": str(pl.get("heading") or ""),
                "content": txt,
                "qdrant_score": round(score, 4),
                "rrf_score": round(1.0 / (60 + rank), 6),
            }
        )
    return hits
