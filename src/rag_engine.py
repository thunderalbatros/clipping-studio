"""3-Level Hybrid RAG Engine (`chonkie` + `sqlite-vec 0.1.9` domain metadata + `fastembed` CrossEncoder + Qdrant RO, v3.6-A).

Architecture:
- Level 1 (`runs/studio.db`):
  * Atomic JSON item decomposition (zero 1600-char slice truncation across `viral_style_dna.json`,
    `platform_algorithms_index.json`, `golden_references.json`, `gta6_lore_index.json`, and `streamer_profiles.json`
    + automatic fallback profiles from `studio.db#creators`).
  * Structure-aware Markdown chunking via `chonkie.RecursiveChunker` (`recipe="markdown"` with offline fallback).
  * Indexes all `7/7` `.agents/skills/*/SKILL.md` files and deduplicates `publish_metrics` telemetry.
  * Native `sqlite-vec 0.1.9` `domain TEXT` metadata column inside `vec0` for pre-KNN domain filtering.
- Level 2 (Read-Only Federated Canon):
  * `rag_hot.sqlite` + `rag_library.sqlite` via strict SQLite URI `?mode=ro&immutable=1` (resolved via
    `src.runtime_paths.resolve_omni_rag_dir()` — zero personal hardcoded host paths).
  * Qdrant Cloud strictly read-only (`centinela_knowledge` 384d + `hunthub_omni_rag_v5` 768d) with silent fallback.
  * Unified cross-source reranking via `fastembed.rerank.cross_encoder.TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")`
    with minimum relevance threshold and lore domain guard (registered in `unload_heavy_models()`).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic_ai import Agent, RunContext
from tenacity import retry, stop_after_attempt, wait_exponential

from src.db import DEFAULT_DB_PATH, PROJECT_ROOT, init_db, list_creators
from src.llm_router import wrap_untrusted_source_data
from src.logging_utils import get_logger
from src.resource_guard import onnx_threads
from src.runtime_paths import resolve_omni_rag_dir

log = get_logger("rag_engine")
AGENTIC_RERANK_FLOOR = 0.45

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
RERANKER_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
FASTEMBED_CACHE_DIR = PROJECT_ROOT / "models" / "fastembed_cache"
RRF_K = 60

RAG_DOMAINS = (
    "shortform_craft",
    "design_ctr",
    "media_engineering",
    "bounties_dmca",
    "gta6_streamers",
    "episodic_memory",
)

SKILL_DOMAIN_MAP: dict[str, str] = {
    "av-director": "shortform_craft",
    "marketing-strategist": "shortform_craft",
    "viral-intelligence-analyst": "shortform_craft",
    "visual-packaging-designer": "design_ctr",
    "retention-critic": "design_ctr",
    "media-engineer": "media_engineering",
    "bounty-and-distribution-ops": "bounties_dmca",
}

_EMBEDDER_CACHE: Any = None
_RERANKER_CACHE: Any = None
_CHUNKER_CACHE: dict[int, Any] = {}
_QDRANT_CLIENT_CACHE: Any = None

_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_]+")
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")
_GTA_LORE_GUARD_RE = re.compile(
    r"\b(gta\s*6|gta\s*vi|gtavi|leonida|vice\s+city|jason\s+duval|lucia\s+caminos|boobie\s+ike|cal\s+hampton|brian\s+heder|mount\s+kalaga|port\s+gellhorn|grassrivers)\b",
    re.IGNORECASE,
)

EmbedFn = Callable[[list[str]], list[list[float]]]
RerankFn = Callable[[str, list[str]], list[float]]


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


def _sigmoid(x: float) -> float:
    x_clamped = max(-30.0, min(30.0, float(x)))
    return 1.0 / (1.0 + math.exp(-x_clamped))


def _load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec extension onto connection; return False if unavailable."""
    try:
        import sqlite_vec  # type: ignore[import-untyped]

        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _connect_rag(db_path: Path = DEFAULT_DB_PATH) -> tuple[sqlite3.Connection, bool]:
    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    vec_ok = _load_sqlite_vec(conn)
    return conn, vec_ok


def init_rag_tables(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    """Create `rag_chunks`, `rag_chunks_fts` (BM25), and `rag_chunks_vec` (`domain TEXT` + `float[384]`)."""
    conn, vec_ok = _connect_rag(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id TEXT NOT NULL UNIQUE,
                source_path TEXT NOT NULL,
                domain TEXT NOT NULL,
                heading TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_fts USING fts5(
                heading,
                content,
                domain,
                content='rag_chunks',
                content_rowid='rowid',
                tokenize='unicode61 remove_diacritics 2'
            );
            """
        )
        if vec_ok:
            row_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='rag_chunks_vec'"
            ).fetchone()
            if row_sql is not None and "domain text" not in str(row_sql[0] or "").lower():
                conn.execute("DROP TABLE IF EXISTS rag_chunks_vec")
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_vec USING vec0(
                    chunk_rowid INTEGER PRIMARY KEY,
                    domain TEXT,
                    embedding float[{EMBEDDING_DIM}]
                );
                """
            )
        conn.commit()
        return {"db_path": str(db_path), "vec_enabled": vec_ok, "embedding_dim": EMBEDDING_DIM}
    finally:
        conn.close()


def _get_fastembed_model() -> Any:
    global _EMBEDDER_CACHE
    if _EMBEDDER_CACHE is None:
        from fastembed import TextEmbedding

        FASTEMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _EMBEDDER_CACHE = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            cache_dir=str(FASTEMBED_CACHE_DIR),
            threads=onnx_threads(),
        )
    return _EMBEDDER_CACHE


def _get_reranker_model() -> Any:
    global _RERANKER_CACHE
    if _RERANKER_CACHE is None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        FASTEMBED_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _RERANKER_CACHE = TextCrossEncoder(
            model_name=RERANKER_MODEL,
            cache_dir=str(FASTEMBED_CACHE_DIR),
            threads=onnx_threads(),
        )
    return _RERANKER_CACHE


def embed_texts(texts: list[str], embed_fn: EmbedFn | None = None) -> list[list[float]]:
    """Compute normalized 384-d embeddings via fastembed (or injected test embedder)."""
    if not texts:
        return []
    if embed_fn is not None:
        return embed_fn(texts)
    model = _get_fastembed_model()
    return [vec.tolist() for vec in model.embed(texts)]


def score_cross_encoder(query: str, documents: list[str], rerank_fn: RerankFn | None = None) -> list[float]:
    """Compute Cross-Encoder relevance logits via `TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")`."""
    if not documents:
        return []
    if rerank_fn is not None:
        return [float(s) for s in rerank_fn(query, documents)]
    reranker = _get_reranker_model()
    return [float(s) for s in reranker.rerank(query, documents)]


def _get_markdown_chunker(max_chars: int = 1400) -> Any:
    if max_chars not in _CHUNKER_CACHE:
        from chonkie import RecursiveChunker, RecursiveRules

        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        try:
            _CHUNKER_CACHE[max_chars] = RecursiveChunker.from_recipe(
                "markdown",
                lang="en",
                tokenizer="character",
                chunk_size=max_chars,
                min_characters_per_chunk=40,
            )
        except Exception:
            _CHUNKER_CACHE[max_chars] = RecursiveChunker(
                tokenizer="character",
                chunk_size=max_chars,
                rules=RecursiveRules(),
                min_characters_per_chunk=40,
            )
    return _CHUNKER_CACHE[max_chars]


def _split_markdown_chunks(
    md_path: Path,
    default_domain: str,
    domain_router: Callable[[str, str], str] | None = None,
    max_chars: int = 1400,
) -> list[dict[str, str]]:
    """Split a Markdown file by headings and `chonkie.RecursiveChunker` (zero arbitrary truncation)."""
    if not md_path.is_file():
        return []
    raw_text = md_path.read_text(encoding="utf-8")
    # Strip YAML frontmatter from SKILL.md while keeping body intact
    if raw_text.startswith("---\n"):
        end_fm = raw_text.find("\n---\n", 4)
        if end_fm != -1:
            raw_text = raw_text[end_fm + 5 :]

    sections: list[tuple[str, list[str]]] = []
    cur_h = md_path.parent.name if md_path.name == "SKILL.md" else md_path.stem
    cur_lines: list[str] = []

    for ln in raw_text.splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            if cur_lines and "".join(cur_lines).strip():
                sections.append((cur_h, cur_lines))
            cur_h = m.group(1).strip()
            cur_lines = []
        else:
            cur_lines.append(ln)
    if cur_lines and "".join(cur_lines).strip():
        sections.append((cur_h, cur_lines))

    try:
        rel_src = str(md_path.resolve().relative_to(PROJECT_ROOT.resolve())).replace("\\", "/")
    except ValueError:
        rel_src = md_path.name

    stem_id = md_path.parent.name if md_path.name == "SKILL.md" else md_path.stem
    chunker = _get_markdown_chunker(max_chars=max_chars)
    chunks: list[dict[str, str]] = []
    seq = 0
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if len(body) < 35:
            continue
        if len(body) <= max_chars:
            parts = [body]
        else:
            ch_objs = list(chunker.chunk(body))
            parts = [str(c.text).strip() for c in ch_objs if str(c.text).strip()] or [body]

        for part_idx, part in enumerate(parts, start=1):
            seq += 1
            dom = domain_router(heading, part) if domain_router else default_domain
            h_label = heading if len(parts) == 1 else f"{heading} (part {part_idx})"
            chunks.append(
                {
                    "chunk_id": f"{dom}:{stem_id}:{seq:03d}",
                    "source_path": rel_src,
                    "domain": dom,
                    "heading": h_label,
                    "content": part,
                }
            )
    return chunks


def _route_editing_bible_domain(heading: str, body: str) -> str:
    low = f"{heading} {body[:240]}".lower()
    if any(k in low for k in ("bount", "dmca", "burn rate", "yamnet", "compliance argentina")):
        return "bounties_dmca"
    if any(k in low for k in ("auto-editor", "birefnet", "ffmpeg", "headless", "centrifugo", "recetario", "onnx")):
        return "media_engineering"
    return "shortform_craft"


def _slugify_id(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")[:40] or "item"


def _collect_json_chunks(project_root: Path, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, str]]:
    """Decompose `context/*.json` atomically item-by-item with ZERO 1600-char truncation + `studio.db#creators` fallback."""
    chunks: list[dict[str, str]] = []
    indexed_streamers: set[str] = set()

    # 1. Streamer profiles (`context/streamer_profiles.json`) + fallback from `studio.db#creators`
    sp_path = project_root / "context" / "streamer_profiles.json"
    if sp_path.is_file():
        try:
            sp_data = json.loads(sp_path.read_text(encoding="utf-8"))
            for sid, info in (sp_data.get("streamers") or {}).items():
                if not isinstance(info, dict):
                    continue
                norm_sid = str(sid).strip().lower()
                indexed_streamers.add(norm_sid)
                lines = [
                    f"Streamer: {info.get('display_name', sid)} (id={sid}, lang={info.get('language')}, tier={info.get('tier')}, platforms={info.get('platforms')})",
                    f"Archetype: {info.get('archetype', '')}",
                    f"Reaction signature: {info.get('reaction_signature', '')}",
                    f"Optimal 0-3s hook: {info.get('optimal_hook_0_3s', '')}",
                    f"Pacing rules: {info.get('pacing_rules', '')}",
                    f"Caption vocabulary: {info.get('caption_vocab_notes', '')}",
                    f"Framing & cover style: {info.get('framing_and_cover_style', '')}",
                    f"Bounty fit: {info.get('bounty_fit', '')}",
                ]
                chunks.append(
                    {
                        "chunk_id": f"gta6_streamers:profile:{norm_sid}",
                        "source_path": "context/streamer_profiles.json",
                        "domain": "gta6_streamers",
                        "heading": f"Streamer Profile: {info.get('display_name', sid)} ({sid})",
                        "content": "\n".join(lines),
                    }
                )
        except Exception:
            pass

    # Fallback profile chunks from `studio.db#creators` for the 11+ creators not in streamer_profiles.json
    if db_path.is_file():
        try:
            for cr in list_creators(db_path=db_path):
                cid = str(cr.get("creator_id") or "").strip().lower()
                if not cid or cid in indexed_streamers:
                    continue
                indexed_streamers.add(cid)
                extra = cr.get("profile_extra") or {}
                lines = [
                    f"Creator: {cr.get('display_name', cid)} (id={cid}, lang={cr.get('language', 'en')}, tier={cr.get('tier')}, platforms={cr.get('platforms')})",
                    f"Primary platform: {cr.get('primary_platform', 'twitch')} | Webcam style: {cr.get('webcam_style', 'auto_detect')} | Webcam box: {cr.get('webcam_box')}",
                    f"Style & editing notes: {cr.get('style_notes', '')}",
                    f"Handles & metadata: {json.dumps(extra, ensure_ascii=False)}",
                ]
                chunks.append(
                    {
                        "chunk_id": f"gta6_streamers:creator_db:{cid}",
                        "source_path": "runs/studio.db#creators",
                        "domain": "gta6_streamers",
                        "heading": f"Creator Profile (SQLite Fallback): {cr.get('display_name', cid)} ({cid})",
                        "content": "\n".join(lines),
                    }
                )
        except Exception:
            pass

    # 2. GTA VI Lore Index (`context/gta6_lore_index.json` — atomic per character/chapter/easter_egg/item)
    lore_path = project_root / "context" / "gta6_lore_index.json"
    if lore_path.is_file():
        try:
            lore_data = json.loads(lore_path.read_text(encoding="utf-8"))
            setting_summary = {
                k: lore_data[k]
                for k in ("game", "release_date", "platforms", "setting")
                if k in lore_data
            }
            if setting_summary:
                chunks.append(
                    {
                        "chunk_id": "gta6_streamers:lore:setting_overview",
                        "source_path": "context/gta6_lore_index.json",
                        "domain": "gta6_streamers",
                        "heading": "GTA VI Lore: Leonida Setting, Release & Regions",
                        "content": json.dumps(setting_summary, ensure_ascii=False),
                    }
                )
            chars = lore_data.get("characters")
            if isinstance(chars, dict):
                for group_name, char_list in chars.items():
                    if isinstance(char_list, list):
                        for ch_item in char_list:
                            cname = str(ch_item.get("name") or group_name) if isinstance(ch_item, dict) else str(ch_item)
                            chunks.append(
                                {
                                    "chunk_id": f"gta6_streamers:lore:char_{_slugify_id(cname)}",
                                    "source_path": "context/gta6_lore_index.json",
                                    "domain": "gta6_streamers",
                                    "heading": f"GTA VI Character ({group_name}): {cname}",
                                    "content": json.dumps(ch_item, ensure_ascii=False),
                                }
                            )
            for key, val in lore_data.items():
                if key in {"schema_version", "updated_at", "version", "game", "release_date", "platforms", "setting", "characters"}:
                    continue
                if isinstance(val, list):
                    for idx, item in enumerate(val, start=1):
                        item_id = (
                            str(item.get("id") or item.get("title") or item.get("name") or f"{key}_{idx:03d}")
                            if isinstance(item, dict)
                            else f"{key}_{idx:03d}"
                        )
                        item_title = (
                            str(item.get("title") or item.get("name") or item_id)
                            if isinstance(item, dict)
                            else f"{key} #{idx}"
                        )
                        chunks.append(
                            {
                                "chunk_id": f"gta6_streamers:lore:{_slugify_id(key)}_{_slugify_id(item_id)}_{idx:02d}",
                                "source_path": "context/gta6_lore_index.json",
                                "domain": "gta6_streamers",
                                "heading": f"GTA VI Lore ({key}): {item_title}",
                                "content": json.dumps(item, ensure_ascii=False),
                            }
                        )
                elif isinstance(val, dict):
                    for sub_k, sub_v in val.items():
                        chunks.append(
                            {
                                "chunk_id": f"gta6_streamers:lore:{_slugify_id(key)}_{_slugify_id(str(sub_k))}",
                                "source_path": "context/gta6_lore_index.json",
                                "domain": "gta6_streamers",
                                "heading": f"GTA VI Lore ({key}): {sub_k}",
                                "content": json.dumps({sub_k: sub_v}, ensure_ascii=False),
                            }
                        )
                else:
                    rendered = json.dumps({key: val}, ensure_ascii=False)
                    if len(rendered) >= 30:
                        chunks.append(
                            {
                                "chunk_id": f"gta6_streamers:lore:{_slugify_id(key)}",
                                "source_path": "context/gta6_lore_index.json",
                                "domain": "gta6_streamers",
                                "heading": f"GTA VI Lore: {key}",
                                "content": rendered,
                            }
                        )
        except Exception:
            pass

    # 3. Platform Algorithms Index (`context/platform_algorithms_index.json` — atomic per platform & framework)
    pa_path = project_root / "context" / "platform_algorithms_index.json"
    if pa_path.is_file():
        try:
            pa_data = json.loads(pa_path.read_text(encoding="utf-8"))
            for plat_name, plat_obj in (pa_data.get("platforms") or {}).items():
                chunks.append(
                    {
                        "chunk_id": f"shortform_craft:platform_algo:{_slugify_id(plat_name)}",
                        "source_path": "context/platform_algorithms_index.json",
                        "domain": "shortform_craft",
                        "heading": f"Platform Algorithm Spec: {plat_name}",
                        "content": json.dumps({plat_name: plat_obj}, ensure_ascii=False),
                    }
                )
            if "universal_cover_safe_box_9_16_px" in pa_data:
                chunks.append(
                    {
                        "chunk_id": "design_ctr:platform_algo:universal_safe_box_9_16",
                        "source_path": "context/platform_algorithms_index.json",
                        "domain": "design_ctr",
                        "heading": "Universal 4-Network Cover Safe Box 9:16",
                        "content": json.dumps(pa_data["universal_cover_safe_box_9_16_px"], ensure_ascii=False),
                    }
                )
            for fw_name, fw_desc in (pa_data.get("creator_and_cognitive_frameworks") or {}).items():
                chunks.append(
                    {
                        "chunk_id": f"shortform_craft:framework:{_slugify_id(fw_name)}",
                        "source_path": "context/platform_algorithms_index.json",
                        "domain": "shortform_craft",
                        "heading": f"Cognitive & Creator Framework: {fw_name}",
                        "content": f"{fw_name}: {fw_desc}",
                    }
                )
            if pa_data.get("strict_ban_red_lines"):
                chunks.append(
                    {
                        "chunk_id": "bounties_dmca:platform_algo:strict_ban_red_lines",
                        "source_path": "context/platform_algorithms_index.json",
                        "domain": "bounties_dmca",
                        "heading": "Strict Anti-Ban & Compliance Red Lines (5 Networks)",
                        "content": json.dumps(pa_data["strict_ban_red_lines"], ensure_ascii=False),
                    }
                )
        except Exception:
            pass

    # 4. Empirical Viral Style DNA (`context/viral_style_dna.json` — targets + 1 chunk per sample)
    dna_path = project_root / "context" / "viral_style_dna.json"
    if dna_path.is_file():
        try:
            dna_data = json.loads(dna_path.read_text(encoding="utf-8"))
            summary_dna = {
                k: dna_data[k]
                for k in ("sample_count", "empirical_medians", "recommended_targets")
                if k in dna_data
            }
            if summary_dna:
                chunks.append(
                    {
                        "chunk_id": "shortform_craft:style_dna:targets_and_medians",
                        "source_path": "context/viral_style_dna.json",
                        "domain": "shortform_craft",
                        "heading": "Empirical Viral Style DNA: Medians & Targets",
                        "content": json.dumps(summary_dna, ensure_ascii=False),
                    }
                )
            for idx, sample in enumerate(dna_data.get("samples") or [], start=1):
                lbl = str(sample.get("title") or sample.get("label") or f"sample_{idx}")[:80]
                chunks.append(
                    {
                        "chunk_id": f"shortform_craft:style_dna:sample_{idx:03d}",
                        "source_path": "context/viral_style_dna.json",
                        "domain": "shortform_craft",
                        "heading": f"Viral Outlier DNA Sample #{idx}: {lbl}",
                        "content": json.dumps(sample, ensure_ascii=False),
                    }
                )
        except Exception:
            pass

    # 5. Golden Viral References (`context/golden_references.json` — 1 chunk per reference)
    gr_path = project_root / "context" / "golden_references.json"
    if gr_path.is_file():
        try:
            gr_data = json.loads(gr_path.read_text(encoding="utf-8"))
            for idx, ref in enumerate(gr_data.get("golden_references") or [], start=1):
                r_title = str(ref.get("title") or ref.get("source_id") or f"ref_{idx}")[:80]
                chunks.append(
                    {
                        "chunk_id": f"shortform_craft:golden_ref:{idx:03d}",
                        "source_path": "context/golden_references.json",
                        "domain": "shortform_craft",
                        "heading": f"Golden Viral Reference #{idx} ({ref.get('platform')}): {r_title}",
                        "content": json.dumps(ref, ensure_ascii=False),
                    }
                )
        except Exception:
            pass

    return chunks


def _collect_episodic_and_bounty_chunks(db_path: Path, project_root: Path) -> list[dict[str, str]]:
    """Collect live episodic memory (clips + BlindCritic + deduplicated publish_metrics) and bounty campaigns."""
    chunks: list[dict[str, str]] = []
    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        clip_rows = conn.execute(
            """
            SELECT run_id, clip_id, streamer_id, start_trim, end_trim, layout_mode,
                   matched_lore_entity, critic_overall, media_check_passed, lufs, dbtp, packaging_json, status
            FROM clips
            ORDER BY critic_overall DESC, updated_at DESC
            LIMIT 200
            """
        ).fetchall()
        for r in clip_rows:
            pkg_title = ""
            pkg_hook = ""
            if r["packaging_json"]:
                try:
                    pkg = json.loads(r["packaging_json"])
                    pkg_title = str(pkg.get("shorts_title") or "")
                    pkg_hook = str(pkg.get("suggested_on_screen_hook") or "")
                except Exception:
                    pass
            status_val = str(r["status"] or "READY_HITL")
            outcome = (
                "WINNER"
                if (int(r["media_check_passed"] or 0) == 1 and float(r["critic_overall"] or 0) >= 0.75 and status_val == "READY_HITL")
                else f"STATUS_{status_val}"
            )
            dur = round(max(0.0, float(r["end_trim"] or 0) - float(r["start_trim"] or 0)), 1)
            body = (
                f"Episodic Clip [{outcome}] run={r['run_id']} clip={r['clip_id']} streamer={r['streamer_id']} "
                f"dur={dur}s layout={r['layout_mode']} lore={r['matched_lore_entity'] or 'none'} "
                f"critic_overall={float(r['critic_overall'] or 0):.2f} media_check={bool(r['media_check_passed'])} "
                f"status={status_val} lufs={float(r['lufs'] or 0):.1f} dbtp={float(r['dbtp'] or 0):.1f} "
                f"title={pkg_title!r} hook={pkg_hook!r}"
            )
            chunks.append(
                {
                    "chunk_id": f"episodic_memory:clip:{r['run_id']}:{r['clip_id']}",
                    "source_path": "runs/studio.db#clips",
                    "domain": "episodic_memory",
                    "heading": f"Episodic Clip {outcome}: {r['streamer_id']} {r['clip_id']} ({r['run_id']})",
                    "content": body,
                }
            )

        # Non-publish_metrics telemetry (avoids double-indexing publish_metrics!)
        tel_rows = conn.execute(
            """
            SELECT id, ts, run_id, event_type, payload_json
            FROM telemetry
            WHERE event_type IN ('critic_feedback', 'compilation_rendered', 'visual_qa_verdict')
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
        for r in tel_rows:
            body = f"Telemetry {r['event_type']} run={r['run_id']} ts={r['ts']}: {r['payload_json']}"
            chunks.append(
                {
                    "chunk_id": f"episodic_memory:telemetry:{r['id']}",
                    "source_path": "runs/studio.db#telemetry",
                    "domain": "episodic_memory",
                    "heading": f"Episodic Telemetry ({r['event_type']}): {r['run_id']}",
                    "content": body,
                }
            )

        camp_rows = conn.execute(
            "SELECT campaign_id, platform, title, cpm_usd, burn_rate_pct, geo_lock, voice_policy, brief_text, verdict FROM campaigns LIMIT 100"
        ).fetchall()
        for r in camp_rows:
            body = (
                f"Campaign {r['campaign_id']} [{r['verdict']}] platform={r['platform']} title={r['title']} "
                f"cpm=${float(r['cpm_usd'] or 0):.2f} burn={float(r['burn_rate_pct'] or 0):.1f}% "
                f"geo={r['geo_lock']} voice={r['voice_policy']} brief={r['brief_text'] or ''}"
            )
            chunks.append(
                {
                    "chunk_id": f"bounties_dmca:campaign:{r['campaign_id']}",
                    "source_path": "runs/studio.db#campaigns",
                    "domain": "bounties_dmca",
                    "heading": f"Bounty Campaign ({r['platform']}): {r['title']}",
                    "content": body,
                }
            )

        # Single canonical index of post-publish metrics
        pm_rows = conn.execute(
            "SELECT id, ts, run_id, payload_json FROM telemetry WHERE event_type = 'publish_metrics' ORDER BY id DESC LIMIT 60"
        ).fetchall()
        for r in pm_rows:
            try:
                d = json.loads(str(r["payload_json"]))
            except (ValueError, TypeError):
                continue
            cid_clip = str(d.get("clip_id") or "clip")
            plat = str(d.get("platform") or "unknown")
            views = int(d.get("views") or 0)
            apv = float(d.get("avg_view_pct") or 0.0)
            vvsa = float(d.get("vvsa_pct") or 0.0)
            sends = float(d.get("sends_per_reach") or 0.0)
            cpm = float(d.get("cpm_usd") or 0.0)
            streamer = str(d.get("streamer_id") or "unknown")
            hook_style = str(d.get("hook_style") or "")
            verdict_tag = "WINNER" if (views >= 10_000 or vvsa >= 70.0 or apv >= 85.0) else "PUBLISHED_METRIC"
            body = (
                f"PublishMetrics [{verdict_tag}] run={r['run_id']} clip={cid_clip} platform={plat} "
                f"streamer={streamer} views={views} VVSA={vvsa}% APV={apv}% Sends/Reach={sends} "
                f"CPM=${cpm:.2f} hook_style={hook_style}"
            )
            chunks.append(
                {
                    "chunk_id": f"episodic_memory:publish_metrics:{r['id']}",
                    "source_path": "runs/studio.db#telemetry",
                    "domain": "episodic_memory",
                    "heading": f"Publish Metrics {verdict_tag}: {streamer} {cid_clip} ({plat})",
                    "content": body,
                }
            )
    finally:
        conn.close()

    runs_dir = project_root / "runs"
    if runs_dir.is_dir():
        for state_file in sorted(runs_dir.glob("*/state.json")):
            try:
                st = json.loads(state_file.read_text(encoding="utf-8"))
                run_id = str(st.get("run_id") or state_file.parent.name)
                streamer = str(st.get("streamer_id") or "unknown")
                for idx, cr in enumerate(st.get("critic_results") or [], start=1):
                    if not isinstance(cr, dict):
                        continue
                    approved = bool(cr.get("approved"))
                    tag = "APPROVED_CRITIC" if approved else "REJECTED_ERROR"
                    body = (
                        f"BlindCritic [{tag}] run={run_id} clip={cr.get('clip_id')} streamer={streamer} "
                        f"overall={cr.get('overall')} hook_3s={cr.get('hook_3s')} pacing={cr.get('pacing')} "
                        f"framing={cr.get('visual_framing')} dmca={cr.get('audio_dmca_safety')} "
                        f"alone={cr.get('gate_alone')} ending={cr.get('gate_ending')} "
                        f"feedback={cr.get('feedback', '')}"
                    )
                    chunks.append(
                        {
                            "chunk_id": f"episodic_memory:critic:{run_id}:{cr.get('clip_id', idx)}:{idx}",
                            "source_path": f"runs/{run_id}/state.json",
                            "domain": "episodic_memory",
                            "heading": f"BlindCritic {tag}: {streamer} {cr.get('clip_id', idx)}",
                            "content": body,
                        }
                    )
            except Exception:
                continue

    return chunks


def collect_all_knowledge_chunks(
    project_root: Path = PROJECT_ROOT,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Collect all chunks covering the 6 knowledge domains + all `7/7` skills for local RAG indexing."""
    chunks: list[dict[str, str]] = []

    # Canonical Bibles in docs/
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "style_bible.md", "shortform_craft"))
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "algorithms_and_virality_bible.md", "shortform_craft"))
    chunks.extend(
        _split_markdown_chunks(
            project_root / "docs" / "shortform_and_editing_bible.md",
            "shortform_craft",
            domain_router=_route_editing_bible_domain,
        )
    )
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "design_bible.md", "design_ctr"))
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "engineering_patterns.md", "media_engineering"))
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "marketing_playbook.md", "bounties_dmca"))

    # All 7/7 Skills in `.agents/skills/*/SKILL.md`
    skills_root = project_root / ".agents" / "skills"
    if skills_root.is_dir():
        for skill_md in sorted(skills_root.glob("*/SKILL.md")):
            skill_name = skill_md.parent.name
            dom = SKILL_DOMAIN_MAP.get(skill_name, "shortform_craft")
            chunks.extend(_split_markdown_chunks(skill_md, dom))

    # GTA VI Living Brief + Atomic JSONs + SQLite creators fallback
    chunks.extend(_split_markdown_chunks(project_root / "context" / "gta6_living_brief.md", "gta6_streamers"))
    chunks.extend(_collect_json_chunks(project_root, db_path=db_path))

    # Episodic Memory + Campaigns
    chunks.extend(_collect_episodic_and_bounty_chunks(db_path=db_path, project_root=project_root))

    dedup: dict[str, dict[str, str]] = {}
    for ch in chunks:
        dedup[ch["chunk_id"]] = ch
    return list(dedup.values())


def rebuild_rag_index(
    db_path: Path = DEFAULT_DB_PATH,
    project_root: Path = PROJECT_ROOT,
    chunks_override: list[dict[str, str]] | None = None,
    embed_fn: EmbedFn | None = None,
    skip_vectors: bool = False,
) -> dict[str, Any]:
    """Idempotently ingest all knowledge gaps into `rag_chunks`, `rag_chunks_fts`, and `rag_chunks_vec` (`domain` + `float[384]`)."""
    init_rag_tables(db_path)
    chunks = chunks_override if chunks_override is not None else collect_all_knowledge_chunks(project_root, db_path)
    now = datetime.now(UTC).isoformat()

    conn, vec_ok = _connect_rag(db_path)
    use_vec = vec_ok and not skip_vectors
    inserted, updated, unchanged, embedded = 0, 0, 0, 0
    to_embed: list[tuple[int, str, str]] = []
    domain_counts: dict[str, int] = {}

    try:
        existing_rows = {
            str(r["chunk_id"]): (int(r["rowid"]), str(r["content_sha256"]), str(r["domain"]))
            for r in conn.execute("SELECT rowid, chunk_id, content_sha256, domain FROM rag_chunks").fetchall()
        }
        existing_vec_domains: dict[int, str] = {}
        if use_vec:
            try:
                existing_vec_domains = {
                    int(r[0]): str(r[1] or "")
                    for r in conn.execute("SELECT chunk_rowid, domain FROM rag_chunks_vec").fetchall()
                }
            except sqlite3.Error:
                use_vec = False

        active_chunk_ids: set[str] = set()
        for ch in chunks:
            cid = str(ch["chunk_id"])
            src = str(ch["source_path"])
            dom = str(ch["domain"])
            heading = str(ch["heading"]).strip()
            content = str(ch["content"]).strip()
            if not content:
                continue
            active_chunk_ids.add(cid)
            domain_counts[dom] = domain_counts.get(dom, 0) + 1
            sha = hashlib.sha256(f"{dom}|{heading}|{content}".encode()).hexdigest()

            prev = existing_rows.get(cid)
            if prev and prev[1] == sha:
                unchanged += 1
                rowid = prev[0]
                if use_vec and existing_vec_domains.get(rowid) != dom:
                    to_embed.append((rowid, dom, f"{heading}\n{content}"))
                continue

            if prev:
                rowid = prev[0]
                conn.execute(
                    """
                    UPDATE rag_chunks
                    SET source_path = ?, domain = ?, heading = ?, content = ?, content_sha256 = ?, updated_at = ?
                    WHERE rowid = ?
                    """,
                    (src, dom, heading, content, sha, now, rowid),
                )
                updated += 1
            else:
                cur = conn.execute(
                    """
                    INSERT INTO rag_chunks (chunk_id, source_path, domain, heading, content, content_sha256, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cid, src, dom, heading, content, sha, now),
                )
                rowid = int(cur.lastrowid or 0)
                inserted += 1

            if use_vec and rowid > 0:
                to_embed.append((rowid, dom, f"{heading}\n{content}"))

        stale_ids = set(existing_rows) - active_chunk_ids
        if stale_ids:
            for sid in stale_ids:
                s_rowid = existing_rows[sid][0]
                conn.execute("DELETE FROM rag_chunks WHERE rowid = ?", (s_rowid,))
                if use_vec:
                    conn.execute("DELETE FROM rag_chunks_vec WHERE chunk_rowid = ?", (s_rowid,))
        if use_vec:
            orphan_ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT chunk_rowid FROM rag_chunks_vec WHERE chunk_rowid NOT IN (SELECT rowid FROM rag_chunks)"
                ).fetchall()
            ]
            for oid in orphan_ids:
                conn.execute("DELETE FROM rag_chunks_vec WHERE chunk_rowid = ?", (oid,))

        conn.execute("INSERT INTO rag_chunks_fts(rag_chunks_fts) VALUES('rebuild')")

        if use_vec and to_embed:
            batch_size = 32
            for start in range(0, len(to_embed), batch_size):
                batch = to_embed[start : start + batch_size]
                vecs = embed_texts([txt for _, _, txt in batch], embed_fn=embed_fn)
                for (rowid, dom, _), vec in zip(batch, vecs, strict=True):
                    conn.execute("DELETE FROM rag_chunks_vec WHERE chunk_rowid = ?", (rowid,))
                    conn.execute(
                        "INSERT INTO rag_chunks_vec (chunk_rowid, domain, embedding) VALUES (?, ?, ?)",
                        (rowid, dom, _serialize_f32(vec)),
                    )
                    embedded += 1

        conn.commit()
        total_rows = int(conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
        vec_rows = int(conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0]) if use_vec else 0
        skills_indexed = len(
            {
                str(r[0])
                for r in conn.execute(
                    "SELECT DISTINCT source_path FROM rag_chunks WHERE source_path LIKE '.agents/skills/%/SKILL.md'"
                ).fetchall()
            }
        )
    finally:
        conn.close()

    return {
        "db_path": str(db_path),
        "total_chunks": total_rows,
        "vec_chunks": vec_rows,
        "vec_enabled": use_vec,
        "skills_indexed": skills_indexed,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "embedded_now": embedded,
        "domains": domain_counts,
    }


def _build_safe_fts_query(raw_query: str) -> str:
    """Convert natural language query into a safe OR-joined FTS5 query (prevents syntax errors)."""
    tokens = [t for t in _FTS_TOKEN_RE.findall(raw_query) if len(t) >= 2]
    if not tokens:
        return ""
    dedup: list[str] = []
    seen: set[str] = set()
    stop = {
        "or",
        "and",
        "not",
        "near",
        "the",
        "for",
        "with",
        "from",
        "that",
        "this",
        "para",
        "como",
        "con",
        "del",
        "las",
        "los",
    }
    for t in tokens:
        low = t.lower()
        if low in stop:
            continue
        if low not in seen:
            seen.add(low)
            dedup.append(f'"{t}"')
    if not dedup:
        dedup = [f'"{t}"' for t in tokens[:6]]
    return " OR ".join(dedup[:12])


# Valuable psychology, editing, design, sound, UX, copywriting, and visual QA sources NEVER treated as webdev noise.
_VALUABLE_FEDERATED_ALLOWLIST = (
    "hooked_habit_forming",
    "nudge_thaler_sunstein",
    "library_ingest_07_",
    "albers_thaler_epstein",
    "compendio_55_videocursos",
    "marketing-psychology",
    "marketing_psychology",
    "copywriting/ai-tells",
    "ai-tells.md",
    "canon_automated_visual_qa_vlm_critic_and_cto_hitl_gate_2026",
    "blink_of_an_eye",
    "walter_murch",
    "visual_story",
    "bruce_block",
    "color_and_light",
    "james_gurney",
    "grid_systems",
    "muller_brockmann",
    "thinking_with_type",
    "ellen_lupton",
    "interaction_of_color",
    "josef_albers",
    "audio_vision",
    "michel_chion",
    "save_the_cat",
    "robert_mckee",
    "thinking_fast_and_slow",
    "kahneman",
    "cialdini",
    "contagious",
    "made_to_stick",
    "refactoring_ui",
    "laws_of_ux",
    "dont_make_me_think",
    "design_of_everyday_things",
    "gamers_brain",
    "celia_hodent",
    "hawkes",
    "centinela",
)

_WEBDEV_NOISE_PATTERNS = (
    "react hook",
    "react-hook-form",
    "react native",
    "reactjs",
    "next.js",
    "nextjs",
    "useeffect",
    "usestate",
    "usememo",
    "usecallback",
    "usecontext",
    "cloudflare worker",
    "wrangler",
    "prisma",
    "drizzle",
    "supabase",
    "express.js",
    "webpack",
    "vite.config",
    "shadcn",
    "graphql",
    "sql injection",
    "rls policy",
    "eslint",
    "biome.json",
    "node_modules",
    "astro.config",
    "svelte",
    "angular",
    "vue.js",
    "package.json",
    "tsconfig",
    "hook_method",
    "hook-integration",
    "pretooluse",
    "posttooluse",
    "session-stop",
    "beautiful_code",
    "kubernetes",
    "terraform",
    "git hook",
    "pre-commit",
    "mern",
    "mongodb",
    "microservices",
    "game engine architecture",
    "jason gregory",
)

_WEBDEV_NOISE_CATEGORIES = frozenset(
    {
        "frontend",
        "backend",
        "webdev",
        "react",
        "nextjs",
        "security_web",
        "cloudflare",
        "database",
        "devops",
        "infra",
    }
)


def _is_webdev_noise_hit(rel_path: str, category: str, heading: str, text: str) -> bool:
    """Return True if a federated Omni-RAG / Qdrant hit is unrelated web-dev/infra/C++ engine noise."""
    path_low = rel_path.replace("\\", "/").lower()
    head_low = heading.lower()
    combined_meta = f"{path_low} {head_low}"
    if any(allow_tok in combined_meta for allow_tok in _VALUABLE_FEDERATED_ALLOWLIST):
        return False

    cat_low = category.strip().lower()
    if cat_low in _WEBDEV_NOISE_CATEGORIES:
        return True

    if any(
        seg in path_low
        for seg in (
            "/react",
            "/next",
            "/tailwind",
            "/frontend",
            "/backend",
            "/cloudflare",
            "/workers",
            "/prisma",
            "/drizzle",
            "/supabase",
            "projects/",
            "react_hook",
            "hook_form",
            "hook-integration",
            "beautiful_code",
            "clean_code",
            "pragmatic_programmer",
            "designing_data_intensive",
            "game_engine_architecture",
            "jason_gregory",
            "mern_stack",
            "full_stack",
            "node_js",
            "web_dev",
            "cybersecurity/",
            "bug_bounty",
            "pro_git",
            "_react_",
            "react_",
            ".tsx",
            ".jsx",
            ".mjs",
        )
    ):
        return True
    hay = f"{rel_path} {heading} {text[:500]}".lower()
    return any(pat in hay for pat in _WEBDEV_NOISE_PATTERNS)


def query_omni_rag_federated(
    query: str,
    limit: int = 6,
    omni_dir: Path | None = None,
    exclude_webdev_noise: bool = True,
) -> list[dict[str, Any]]:
    """Read-only FTS5 BM25 query against Omni-RAG v6 (`rag_hot.sqlite` + `rag_library.sqlite`) via `?mode=ro&immutable=1`."""
    fts_q = _build_safe_fts_query(query)
    if not fts_q:
        return []

    base_dir = omni_dir or resolve_omni_rag_dir(PROJECT_ROOT)
    if not base_dir.is_dir():
        return []

    raw_fetch = max(limit * 8, 36)
    hits: list[dict[str, Any]] = []
    # Query rag_library.sqlite (290 canonical books) first alongside rag_hot.sqlite so books are never starved
    for db_name, source_tag in (("rag_library.sqlite", "omni_rag_library"), ("rag_hot.sqlite", "omni_rag_hot")):
        db_file = base_dir / db_name
        if not db_file.is_file():
            continue
        uri = f"{db_file.resolve().as_uri()}?mode=ro&immutable=1"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT d.title, d.rel_path, d.category, c.chunk_uid, c.heading_path, c.text,
                       bm25(chunks_fts) AS bm25_score
                FROM chunks_fts f
                JOIN chunks c ON c.chunk_id = f.rowid
                JOIN docs d ON d.doc_id = c.doc_id
                WHERE chunks_fts MATCH ?
                ORDER BY bm25(chunks_fts)
                LIMIT ?
                """,
                (fts_q, raw_fetch),
            ).fetchall()
            kept_rank = 0
            for r in rows:
                rel_p = str(r["rel_path"] or r["title"] or "")
                cat = str(r["category"] or "")
                head = str(r["heading_path"] or r["title"] or "")
                txt = str(r["text"] or "").strip()
                if exclude_webdev_noise and _is_webdev_noise_hit(rel_p, cat, head, txt):
                    continue
                kept_rank += 1
                hits.append(
                    {
                        "chunk_id": f"{source_tag}:{r['chunk_uid']}",
                        "source": source_tag,
                        "source_path": rel_p,
                        "domain": f"federated:{cat or 'omni_rag'}",
                        "heading": head,
                        "content": txt[:1100],
                        "bm25_rank": kept_rank,
                        "rrf_score": round(1.0 / (RRF_K + kept_rank), 6),
                    }
                )
                if kept_rank >= limit:
                    break
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()

    return hits


def _resolve_qdrant_url() -> str:
    """Resolve Qdrant Cloud URL from `QDRANT_URL` or `QDRANT_HOST` without printing secrets."""
    raw = (os.getenv("QDRANT_URL") or os.getenv("QDRANT_HOST") or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    return raw


def _get_qdrant_readonly_client() -> Any | None:
    """Instantiate read-only `QdrantClient` if `QDRANT_URL`/`QDRANT_HOST` + `QDRANT_API_KEY` exist; return None silently otherwise."""
    global _QDRANT_CLIENT_CACHE
    if _QDRANT_CLIENT_CACHE is not None:
        return _QDRANT_CLIENT_CACHE
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    url = _resolve_qdrant_url()
    api_key = (os.getenv("QDRANT_API_KEY") or "").strip()
    if not url or not api_key:
        return None
    try:
        from qdrant_client import QdrantClient

        _QDRANT_CLIENT_CACHE = QdrantClient(url=url, api_key=api_key, timeout=12)
        return _QDRANT_CLIENT_CACHE
    except Exception:
        return None


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=3.0),
)
def _embed_vertex_768_with_timeout(proj: str, loc: str, query: str) -> list[float] | None:
    from google import genai
    from google.genai import types as genai_types

    client = genai.Client(
        vertexai=True,
        project=proj,
        location=loc,
        http_options=genai_types.HttpOptions(timeout=12_000),
    )
    resp = client.models.embed_content(model="text-embedding-005", contents=query)
    embs = getattr(resp, "embeddings", None) or []
    if embs and getattr(embs[0], "values", None):
        return [float(v) for v in embs[0].values]
    return None


def _embed_vertex_768_readonly(query: str) -> list[float] | None:
    """Optional 768-d Vertex `text-embedding-005` query embedding (`timeout <= 20s`) for `hunthub_omni_rag_v5` read-only search."""
    proj = (os.getenv("GCP_PROJECT_ID") or "").strip()
    loc = (os.getenv("GCP_LOCATION") or "us-central1").strip() or "us-central1"
    if not proj:
        return None
    gac = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    if gac and not Path(gac).is_file():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    try:
        return _embed_vertex_768_with_timeout(proj, loc, query)
    except Exception:
        return None


def query_qdrant_readonly(
    query: str,
    limit: int = 4,
    *,
    embed_fn: EmbedFn | None = None,
    exclude_webdev_noise: bool = True,
    collections: tuple[str, ...] = ("centinela_knowledge", "hunthub_omni_rag_v5"),
) -> list[dict[str, Any]]:
    """Query Qdrant Cloud strictly in READ-ONLY mode (`query_points` only) with silent fallback on offline/missing creds."""
    if not query.strip():
        return []
    client = _get_qdrant_readonly_client()
    if client is None:
        return []

    hits: list[dict[str, Any]] = []
    # 1. centinela_knowledge (384d BAAI/bge-small-en-v1.5: Hawkes, Kick HLS, clipper pipelines)
    if "centinela_knowledge" in collections:
        try:
            vec_384 = embed_texts([query], embed_fn=embed_fn)[0]
            res_c = client.query_points(
                collection_name="centinela_knowledge",
                query=vec_384,
                limit=max(2, limit),
                with_payload=True,
            )
            for rank, pt in enumerate(getattr(res_c, "points", None) or [], start=1):
                pl = dict(getattr(pt, "payload", None) or {})
                txt = str(pl.get("text") or pl.get("content") or pl.get("chunk_text") or "").strip()
                if not txt:
                    continue
                src_p = str(pl.get("source") or pl.get("file_path") or pl.get("rel_path") or "centinela_knowledge")
                head = str(pl.get("title") or pl.get("heading") or src_p)
                cat = str(pl.get("category") or pl.get("domain") or "centinela")
                if exclude_webdev_noise and _is_webdev_noise_hit(src_p, cat, head, txt):
                    continue
                hits.append(
                    {
                        "chunk_id": f"qdrant_ro:centinela:{pt.id}",
                        "source": "qdrant_ro:centinela_knowledge",
                        "source_path": src_p,
                        "domain": f"qdrant:{cat}",
                        "heading": head,
                        "content": txt[:1100],
                        "qdrant_score": round(float(getattr(pt, "score", 0.0) or 0.0), 4),
                        "rrf_score": round(1.0 / (RRF_K + rank), 6),
                    }
                )
        except Exception:
            pass

    # 2. hunthub_omni_rag_v5 (768d text-embedding-005: 14-department matrices, visual QA plates, canon)
    if "hunthub_omni_rag_v5" in collections and embed_fn is None:
        vec_768 = _embed_vertex_768_readonly(query)
        if vec_768 is not None:
            try:
                res_h = client.query_points(
                    collection_name="hunthub_omni_rag_v5",
                    query=vec_768,
                    limit=max(2, limit),
                    with_payload=True,
                )
                for rank, pt in enumerate(getattr(res_h, "points", None) or [], start=1):
                    pl = dict(getattr(pt, "payload", None) or {})
                    txt = str(pl.get("text") or pl.get("content") or pl.get("chunk_text") or "").strip()
                    if not txt:
                        continue
                    src_p = str(pl.get("rel_path") or pl.get("source") or pl.get("doc_path") or "hunthub_omni_rag_v5")
                    head = str(pl.get("heading_path") or pl.get("title") or src_p)
                    cat = str(pl.get("category") or pl.get("department") or "omni_v5")
                    if exclude_webdev_noise and _is_webdev_noise_hit(src_p, cat, head, txt):
                        continue
                    hits.append(
                        {
                            "chunk_id": f"qdrant_ro:omni_v5:{pt.id}",
                            "source": "qdrant_ro:hunthub_omni_rag_v5",
                            "source_path": src_p,
                            "domain": f"qdrant:{cat}",
                            "heading": head,
                            "content": txt[:1100],
                            "qdrant_score": round(float(getattr(pt, "score", 0.0) or 0.0), 4),
                            "rrf_score": round(1.0 / (RRF_K + rank), 6),
                        }
                    )
            except Exception:
                pass

    return hits


_GTA_LOCAL_SOURCE_MARKERS = (
    "gta6_",
    "streamer_profiles.json",
    "studio.db#creators",
    "golden_references.json",
    "viral_style_dna.json",
    "runs/",
)


def _local_hit_matches_niche(hit: dict[str, Any], active_niche: str | None) -> bool:
    """Prevent GTA VI local `studio.db` chunks from leaking when an operator queries non-GTA niches."""
    if not active_niche or active_niche == "gaming_gta6":
        return True
    dom = str(hit.get("domain") or "")
    src_p = str(hit.get("source_path") or "").replace("\\", "/").lower()
    if dom == "gta6_streamers":
        return False
    return not any(m in src_p for m in _GTA_LOCAL_SOURCE_MARKERS)


def _is_gta_lore_query(query: str, domain: str | None) -> bool:
    if domain == "gta6_streamers":
        return True
    return bool(_GTA_LORE_GUARD_RE.search(query or ""))


def _passes_domain_guard(
    hit: dict[str, Any],
    query: str,
    domain: str | None,
    ce_logit: float,
    *,
    active_niche: str | None = None,
) -> bool:
    """Prevent external general books (e.g. C++ Game Engine Architecture by 'Jason' Gregory) from polluting lore/news queries."""
    src = str(hit.get("source") or "")
    if src.startswith("qdrant_ssot:"):
        hit_niche = str(hit.get("niche") or "global")
        if active_niche and hit_niche not in {active_niche, "global"}:
            return False
        return ce_logit >= -10.0

    if src == "studio_local":
        return _local_hit_matches_niche(hit, active_niche)

    # Strict minimum cross-encoder relevance threshold across all federated classic book hits
    if ce_logit < -5.5:
        return False

    # Isolate rag_library.sqlite / Omni-RAG from contaminating breaking AI news, episodic memory, or GTA VI lore
    if active_niche == "ai_news" and any(k in (query or "").lower() for k in ("benchmark", "release", "2026", "model", "launch")):
        return False

    if domain in {"gta6_streamers", "episodic_memory"} or _is_gta_lore_query(query, domain):
        hay = f"{hit.get('heading', '')} {hit.get('content', '')}"
        if not _GTA_LORE_GUARD_RE.search(hay) or ce_logit < 1.5:
            return False

    return True


def query_rag(
    query: str,
    domain: str | None = None,
    top_k: int = 5,
    federated: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
    omni_dir: Path | None = None,
    embed_fn: EmbedFn | None = None,
    rerank_fn: RerankFn | None = None,
    use_vectors: bool = True,
    include_qdrant: bool = True,
) -> list[dict[str, Any]]:
    """Hybrid BM25 + `sqlite-vec` (`domain` pre-KNN) + `TextCrossEncoder` reranking + optional Federated/Qdrant RO."""
    init_rag_tables(db_path)
    conn, vec_ok = _connect_rag(db_path)
    fts_q = _build_safe_fts_query(query)
    cand_limit = max(top_k * 4, 16)

    bm25_ranks: dict[int, int] = {}
    vec_ranks: dict[int, int] = {}
    row_cache: dict[int, dict[str, Any]] = {}

    try:
        # 1. Local FTS5 BM25 leg
        if fts_q:
            if domain:
                rows = conn.execute(
                    """
                    SELECT r.rowid, r.chunk_id, r.source_path, r.domain, r.heading, r.content
                    FROM rag_chunks_fts f
                    JOIN rag_chunks r ON r.rowid = f.rowid
                    WHERE rag_chunks_fts MATCH ? AND r.domain = ?
                    ORDER BY bm25(rag_chunks_fts)
                    LIMIT ?
                    """,
                    (fts_q, domain, cand_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT r.rowid, r.chunk_id, r.source_path, r.domain, r.heading, r.content
                    FROM rag_chunks_fts f
                    JOIN rag_chunks r ON r.rowid = f.rowid
                    WHERE rag_chunks_fts MATCH ?
                    ORDER BY bm25(rag_chunks_fts)
                    LIMIT ?
                    """,
                    (fts_q, cand_limit),
                ).fetchall()
            for rank, r in enumerate(rows, start=1):
                rid = int(r["rowid"])
                bm25_ranks[rid] = rank
                row_cache[rid] = dict(r)

        # 2. Local sqlite-vec 0.1.9 vector KNN leg with native pre-KNN `v.domain = ?` filter
        if use_vectors and vec_ok and query.strip():
            try:
                if domain:
                    dom_cnt = int(
                        conn.execute("SELECT COUNT(*) FROM rag_chunks_vec WHERE domain = ?", (domain,)).fetchone()[0]
                    )
                    if dom_cnt > 0:
                        q_vec = embed_texts([query], embed_fn=embed_fn)[0]
                        vec_rows = conn.execute(
                            """
                            SELECT v.chunk_rowid, v.distance,
                                   r.chunk_id, r.source_path, r.domain, r.heading, r.content
                            FROM rag_chunks_vec v
                            JOIN rag_chunks r ON r.rowid = v.chunk_rowid
                            WHERE v.embedding MATCH ? AND k = ? AND v.domain = ?
                            ORDER BY v.distance
                            """,
                            (_serialize_f32(q_vec), min(cand_limit, dom_cnt), domain),
                        ).fetchall()
                    else:
                        vec_rows = []
                else:
                    vec_count = int(conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0])
                    if vec_count > 0:
                        q_vec = embed_texts([query], embed_fn=embed_fn)[0]
                        vec_rows = conn.execute(
                            """
                            SELECT v.chunk_rowid, v.distance,
                                   r.chunk_id, r.source_path, r.domain, r.heading, r.content
                            FROM rag_chunks_vec v
                            JOIN rag_chunks r ON r.rowid = v.chunk_rowid
                            WHERE v.embedding MATCH ? AND k = ?
                            ORDER BY v.distance
                            """,
                            (_serialize_f32(q_vec), min(cand_limit, vec_count)),
                        ).fetchall()
                    else:
                        vec_rows = []

                for rank_idx, r in enumerate(vec_rows, start=1):
                    rid = int(r["chunk_rowid"])
                    vec_ranks[rid] = rank_idx
                    if rid not in row_cache:
                        row_cache[rid] = {
                            "rowid": rid,
                            "chunk_id": r["chunk_id"],
                            "source_path": r["source_path"],
                            "domain": r["domain"],
                            "heading": r["heading"],
                            "content": r["content"],
                        }
            except Exception:
                pass
    finally:
        conn.close()

    # 3. Local Reciprocal Rank Fusion (k=60)
    fused: list[dict[str, Any]] = []
    for rid, item in row_cache.items():
        score = 0.0
        if rid in bm25_ranks:
            score += 1.0 / (RRF_K + bm25_ranks[rid])
        if rid in vec_ranks:
            score += 1.0 / (RRF_K + vec_ranks[rid])
        fused.append(
            {
                "chunk_id": str(item["chunk_id"]),
                "source": "studio_local",
                "source_path": str(item["source_path"]),
                "domain": str(item["domain"]),
                "heading": str(item["heading"]),
                "content": str(item["content"]),
                "bm25_rank": bm25_ranks.get(rid),
                "vec_rank": vec_ranks.get(rid),
                "rrf_score": round(score, 6),
            }
        )
    fused.sort(key=lambda x: x["rrf_score"], reverse=True)

    if not federated:
        return fused[:top_k]

    # 4. Federated Read-Only Bridge (Omni-RAG SQLite RO + Qdrant Cloud RO) reranked via TextCrossEncoder
    fed_pool: list[dict[str, Any]] = list(fused[: max(top_k * 2, 8)])
    fed_pool.extend(query_omni_rag_federated(query, limit=max(4, top_k), omni_dir=omni_dir))
    if include_qdrant:
        fed_pool.extend(query_qdrant_readonly(query, limit=max(3, top_k // 2 + 1), embed_fn=embed_fn))

    if not fed_pool:
        return []

    dedup_pool: list[dict[str, Any]] = []
    seen_cids: set[str] = set()
    for item in fed_pool:
        cid = str(item["chunk_id"])
        if cid not in seen_cids:
            seen_cids.add(cid)
            dedup_pool.append(item)

    try:
        docs_for_ce = [f"{h.get('heading', '')}\n{str(h.get('content', ''))[:900]}" for h in dedup_pool]
        logits = score_cross_encoder(query, docs_for_ce, rerank_fn=rerank_fn)
        reranked: list[dict[str, Any]] = []
        for h, logit in zip(dedup_pool, logits, strict=True):
            if not _passes_domain_guard(h, query, domain, logit):
                continue
            item_copy = dict(h)
            item_copy["rerank_logit"] = round(float(logit), 4)
            item_copy["rerank_score"] = round(_sigmoid(logit), 6)
            reranked.append(item_copy)
        reranked.sort(key=lambda x: (x["rerank_score"], x["rrf_score"]), reverse=True)
        return reranked[:top_k]
    except Exception:
        return fused[:top_k]


def format_rag_context(hits: list[dict[str, Any]], max_tokens: int = 550) -> str:
    """Format Top-K RAG hits into a compact context block strictly bounded to <= max_tokens (~4 chars/tok)."""
    if not hits:
        return ""
    char_budget = max(200, max_tokens * 4)
    lines: list[str] = []
    used = 0
    for h in hits:
        snippet = " ".join(str(h.get("content") or "").split())
        if len(snippet) > 420:
            snippet = snippet[:417] + "..."
        entry = f"- [{h.get('domain')}|{h.get('source')}] {h.get('heading')}: {snippet}"
        if used + len(entry) + 1 > char_budget:
            remaining = char_budget - used - 4
            if remaining > 80:
                lines.append(entry[:remaining] + "...")
            break
        lines.append(entry)
        used += len(entry) + 1
    return "\n".join(lines)


_REWRITE_FILLER_RE = re.compile(
    r"\b(hey|pls|please|bro|che|onda|movida|cosa|stuff|thingy|tell|about|como|cómo|era|dime|explica|what|is|the|de|la|el|los|las|un|una)\b",
    re.IGNORECASE,
)

_SYNONYM_EXPANSIONS_EN: tuple[tuple[tuple[str, ...], str], ...] = (
    (("anti-clipper", "anticlipper", "shadowban", "unoriginal", "reupload", "slop"), "YouTube Shorts Prioritizing Original Content Inauthentic Content Reused Content reach reduction 2026-10-01"),
    (("engaged", "frame 0", "monetization", "pool", "ypp"), "YouTube Shorts Engaged Views vs Public Views YPP eligibility 10M"),
    (("copy link", "twitter", "x algo", "pods", "cold start", "dwell"), "X x-algorithm home-mixer ShareViaCopyLinkWeight +20.0 ReplyWeight ColdStartMaxPostAgeSecs MinVideoDurationMs"),
    (("whop", "bot score", "bounty", "burn", "kyc"), "Whop Content Rewards CPM eligibility 20%-80% burn rate Bot Score anti-fraud"),
    (("standup", "stand-up", "comedian", "punchline", "crowd", "laugh", "pause"), "standup_comedy silence_trim_policy preserve_pauses pre-punchline pause crowd laughter retention"),
    (("cinema", "movie", "film", "content id", "roformer"), "cinema_series strict_transformative_commentary use_bs_roformer Content ID commentary framing"),
    (("c2pa", "clone", "likeness", "tiktok", "fyf"), "TikTok Creator Academy For You Feed FYF originality pHash C2PA Likeness Detection"),
    (("andromeda", "trial reels", "dm", "sends", "mosseri"), "Meta AI System Cards Instagram Reels Chaining Sends Per Reach Originality Score 10 in 30 days Trial Reels"),
    (("buckme", "6 star", "six star", "disguise", "leonida", "radio"), "GTA VI Leonida BuckMe 6-star wanted disguise plate swap Vice City DMCA radio"),
)

_SYNONYM_EXPANSIONS_ES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("castigo", "alcance", "resubida", "originalidad", "inautentico", "inauténtico", "anti-clipper"), "YouTube Shorts contenido original transformativo Contenido Inauténtico Vistas con Interacción 2026-10-01"),
    (("enlace", "copiar", "algoritmo x", "twitter", "frio", "frío"), "Algoritmo Oficial X xai-org/x-algorithm ShareViaCopyLinkWeight +20.0 ColdStartMaxPostAgeSecs MinVideoDurationMs"),
    (("whop", "campaña", "campana", "bots", "argentina", "latam", "cpm"), "Whop Content Rewards elegibilidad edad 18 KYC burn rate 20% 80% Bot Score Argentina LATAM"),
    (("comedia", "chiste", "remate", "pausa", "risas", "standup", "stand-up"), "standup_comedy preserve_pauses silencios previos al remate risas de sala retención"),
    (("tiktok", "para ti", "fyf", "groseria", "grosería", "clon", "c2pa"), "TikTok Creator Academy Para Ti FYF pHash OCR 0-7.5s C2PA Likeness Detection"),
    (("instagram", "dm", "mensajes", "andromeda", "trial"), "Meta AI Instagram Reels Chaining Sends Per Reach Originalidad 10 en 30 días Trial Reels"),
)


def rewrite_rag_query(
    query: str,
    *,
    niche: str = "gaming_gta6",
    language: str = "en",
    domain: str | None = None,
    custom_rewriter: Callable[[str], str] | None = None,
) -> str:
    """Deterministic low-latency multi-hop query rewriter triggered when Hop-1 rerank score < 0.45."""
    if custom_rewriter is not None:
        rewritten_custom = custom_rewriter(query).strip()
        if rewritten_custom:
            return rewritten_custom

    raw = (query or "").strip()
    low = raw.lower()
    lang_clean = (language or "en").strip().lower()
    stripped = " ".join(_REWRITE_FILLER_RE.sub(" ", raw).split()).strip() or raw

    table = _SYNONYM_EXPANSIONS_ES if lang_clean.startswith("es") else _SYNONYM_EXPANSIONS_EN
    expansions: list[str] = []
    for triggers, target_phrase in table:
        if any(tok in low for tok in triggers):
            expansions.append(target_phrase)

    if not expansions:
        dom_hint = (domain or "shortform_craft").replace("_", " ")
        niche_hint = (niche or "global").replace("_", " ")
        if lang_clean.startswith("es"):
            expansions.append(f"{niche_hint} {dom_hint} retención gancho 0-3s políticas oficiales 2026")
        else:
            expansions.append(f"{niche_hint} {dom_hint} short-form retention hook official policy 2026")

    combined = f"{stripped} {' '.join(expansions[:2])}".strip()
    return " ".join(dict.fromkeys(combined.split()))


def rag_query_agentic(
    query: str,
    niche: str = "gaming_gta6",
    language: str = "en",
    domain: str | None = None,
    allow_federated: bool = True,
    max_hops: int = 2,
    *,
    top_k: int = 5,
    max_tokens: int = 550,
    min_rerank_score: float = AGENTIC_RERANK_FLOOR,
    db_path: Path = DEFAULT_DB_PATH,
    omni_dir: Path | None = None,
    qdrant_client: Any | None = None,
    embed_fn: EmbedFn | None = None,
    embed_768_fn: Any | None = None,
    rerank_fn: RerankFn | None = None,
    rewrite_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Multi-hop Agentic RAG engine prioritizing Qdrant Cloud SSOT (`clipping_studio_empirical`) with strict niche/language isolation and automatic query rewrite on score < 0.45."""
    from src.qdrant_knowledge import query_empirical_qdrant

    active_niche = (niche or "gaming_gta6").strip().lower()
    eff_lang = (language or "en").strip().lower()
    eff_hops = max(1, min(3, int(max_hops)))
    safe_budget = min(550, max(120, int(max_tokens)))

    cur_query = (query or "").strip()
    if not cur_query:
        return {
            "query": "",
            "final_query": "",
            "niche": active_niche,
            "language": eff_lang,
            "domain": domain,
            "hops_executed": 0,
            "queries_tried": [],
            "rewritten": False,
            "top_rerank_score": 0.0,
            "hits": [],
            "formatted_context": "",
        }

    queries_tried: list[str] = []
    merged_by_cid: dict[str, dict[str, Any]] = {}
    top_overall_score = 0.0
    hops_executed = 0

    for hop_idx in range(1, eff_hops + 1):
        hops_executed = hop_idx
        queries_tried.append(cur_query)

        # 1. Primary SSOT leg: Qdrant Cloud `clipping_studio_empirical` pre-filtered by `niche in [active_niche, "global"]` + `language`
        cand_pool: list[dict[str, Any]] = query_empirical_qdrant(
            cur_query,
            niche=active_niche,
            language=eff_lang,
            domain=domain,
            top_k=max(4, top_k * 2),
            client=qdrant_client,
            embed_768_fn=embed_768_fn,
        )

        # 2. Local SQLite `runs/studio.db` leg (skills, canonical bibles, episodic memory) strictly isolated by niche
        if eff_lang in {"en", "auto"} or not cand_pool:
            local_hits = query_rag(
                cur_query,
                domain=domain,
                top_k=max(3, top_k),
                federated=False,
                db_path=db_path,
                embed_fn=embed_fn,
                use_vectors=True,
                include_qdrant=False,
            )
            cand_pool.extend([h for h in local_hits if _local_hit_matches_niche(h, active_niche)])

        # 3. Read-only Federated Canon (`rag_library.sqlite` 290 books + `hunthub_omni_rag_v5`) — skipped on lore/episodic/news to prevent contamination
        is_isolated_topic = (
            domain in {"gta6_streamers", "episodic_memory"}
            or _is_gta_lore_query(cur_query, domain)
            or active_niche == "ai_news"
        )
        if allow_federated and not is_isolated_topic:
            cand_pool.extend(query_omni_rag_federated(cur_query, limit=max(3, top_k), omni_dir=omni_dir))
            cand_pool.extend(query_qdrant_readonly(cur_query, limit=3, embed_fn=embed_fn))

        dedup_hop: list[dict[str, Any]] = []
        seen_hop: set[str] = set()
        for item in cand_pool:
            cid = str(item["chunk_id"])
            if cid not in seen_hop:
                seen_hop.add(cid)
                dedup_hop.append(item)

        hop_best = 0.0
        if dedup_hop:
            docs_for_ce = [f"{h.get('heading', '')}\n{str(h.get('content', ''))[:900]}" for h in dedup_hop]
            logits = score_cross_encoder(cur_query, docs_for_ce, rerank_fn=rerank_fn)
            for h, logit in zip(dedup_hop, logits, strict=True):
                if not _passes_domain_guard(h, cur_query, domain, logit, active_niche=active_niche):
                    continue
                ce_prob = _sigmoid(logit)
                q_score = float(h.get("qdrant_score") or 0.0)
                src_tag = str(h.get("source") or "")
                # Calibrate multilingual 768d Qdrant SSOT hits for non-English queries when using English MS-MARCO CrossEncoder
                if (
                    rerank_fn is None
                    and not eff_lang.startswith("en")
                    and src_tag.startswith("qdrant_ssot:")
                    and q_score >= 0.64
                ):
                    scaled_q = min(1.0, max(0.0, (q_score - 0.30) / 0.58))
                    eff_score = round(max(ce_prob, 0.35 * ce_prob + 0.65 * scaled_q), 6)
                else:
                    eff_score = round(ce_prob, 6)

                enriched = dict(h)
                enriched["rerank_logit"] = round(float(logit), 4)
                enriched["rerank_score"] = eff_score
                enriched["hop"] = hop_idx
                hop_best = max(hop_best, eff_score)

                prev_item = merged_by_cid.get(str(enriched["chunk_id"]))
                if prev_item is None or eff_score > float(prev_item.get("rerank_score") or 0.0):
                    merged_by_cid[str(enriched["chunk_id"])] = enriched

        top_overall_score = max(top_overall_score, hop_best)
        if hop_best >= min_rerank_score or hop_idx >= eff_hops:
            break

        next_query = rewrite_rag_query(
            query,
            niche=active_niche,
            language=eff_lang,
            domain=domain,
            custom_rewriter=rewrite_fn,
        )
        if not next_query or next_query == cur_query:
            break
        cur_query = next_query

    ranked_all = sorted(
        merged_by_cid.values(),
        key=lambda x: (
            1 if str(x.get("source", "")).startswith("qdrant_ssot:") and float(x.get("rerank_score") or 0.0) >= min_rerank_score else 0,
            float(x.get("rerank_score") or 0.0),
            float(x.get("rrf_score") or 0.0),
        ),
        reverse=True,
    )
    top_hits = ranked_all[:top_k]
    formatted = format_rag_context(top_hits, max_tokens=safe_budget)

    log.info(
        "rag_query_agentic_completed",
        niche=active_niche,
        language=eff_lang,
        domain=domain,
        hops_executed=hops_executed,
        rewritten=hops_executed > 1,
        top_rerank_score=round(top_overall_score, 4),
        hit_count=len(top_hits),
    )
    return {
        "query": query,
        "final_query": cur_query,
        "niche": active_niche,
        "language": eff_lang,
        "domain": domain,
        "hops_executed": hops_executed,
        "queries_tried": queries_tried,
        "rewritten": hops_executed > 1,
        "top_rerank_score": round(top_overall_score, 6),
        "hits": top_hits,
        "formatted_context": formatted,
    }


def attach_rag_agent_tool(
    agent: Agent[Any, Any],
    *,
    default_niche: str = "gaming_gta6",
    default_language: str = "en",
    db_path: Path = DEFAULT_DB_PATH,
) -> Any:
    """Register `rag_query_agentic` directly as a `@agent.tool` on a `pydantic-ai` `Agent` (`timeout <= 20s`)."""

    @agent.tool(name="rag_query_agentic", timeout=20.0)
    def _rag_tool(
        _ctx: RunContext[Any],
        query: str,
        niche: str = default_niche,
        language: str = default_language,
        domain: str | None = None,
        allow_federated: bool = True,
        max_hops: int = 2,
    ) -> str:
        """Query governed Qdrant Cloud SSOT + Studio RAG with strict niche/language isolation and automatic multi-hop query rewrite."""
        res = rag_query_agentic(
            query=query,
            niche=niche or default_niche,
            language=language or default_language,
            domain=domain,
            allow_federated=allow_federated,
            max_hops=max_hops,
            max_tokens=550,
            db_path=db_path,
        )
        ctx_txt = str(res.get("formatted_context") or "").strip()
        if not ctx_txt:
            return "NO_RELEVANT_RAG_HITS"
        return wrap_untrusted_source_data("agentic_rag_tool", ctx_txt)

    return _rag_tool


def build_curator_rag_context(
    streamer_id: str,
    topic_query: str,
    stage: str = "pass2",
    max_tokens: int = 550,
    db_path: Path = DEFAULT_DB_PATH,
    use_vectors: bool = True,
    federated: bool = False,
    *,
    niche: str = "gaming_gta6",
    language: str = "en",
) -> str:
    """Retrieve bounded RAG context (`<= 550` tokens) combining creator/niche profile, episodic memory, and Qdrant Cloud SSOT."""
    safe_tokens = min(550, max(120, int(max_tokens)))
    hits: list[dict[str, Any]] = []
    active_niche = (niche or "gaming_gta6").strip().lower()
    eff_lang = (language or "en").strip().lower()

    if active_niche == "gaming_gta6":
        prof_res = rag_query_agentic(
            f"{streamer_id} profile hook pacing",
            niche="gaming_gta6",
            language=eff_lang,
            domain="gta6_streamers",
            allow_federated=False,
            max_hops=1,
            top_k=1,
            db_path=db_path,
        )
        hits.extend(prof_res["hits"])
        if not hits:
            hits.extend(
                query_rag(
                    f"{streamer_id} profile hook pacing",
                    domain="gta6_streamers",
                    top_k=1,
                    db_path=db_path,
                    use_vectors=use_vectors,
                )
            )

    hits.extend(
        query_rag(
            f"{streamer_id} {topic_query} WINNER REJECTED_ERROR",
            domain="episodic_memory",
            top_k=2,
            db_path=db_path,
            use_vectors=use_vectors,
        )
    )
    target_domain = "design_ctr" if stage == "packaging" else "shortform_craft"
    craft_res = rag_query_agentic(
        topic_query or "hook retention seamless loop curiosity gap",
        niche=active_niche,
        language=eff_lang,
        domain=target_domain,
        allow_federated=federated,
        max_hops=2,
        top_k=2,
        db_path=db_path,
    )
    hits.extend(craft_res["hits"])

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for h in hits:
        if not _local_hit_matches_niche(h, active_niche):
            continue
        cid = str(h["chunk_id"])
        if cid not in seen:
            seen.add(cid)
            unique.append(h)
    return format_rag_context(unique, max_tokens=safe_tokens)


def inspect_rag_status(db_path: Path = DEFAULT_DB_PATH, omni_dir: Path | None = None) -> dict[str, Any]:
    """Return non-intrusive RAG health summary for `cli doctor`."""
    init_rag_tables(db_path)
    conn, vec_ok = _connect_rag(db_path)
    try:
        chunk_cnt = int(conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
        vec_cnt = int(conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0]) if vec_ok else 0
        dom_rows = conn.execute("SELECT domain, COUNT(*) AS c FROM rag_chunks GROUP BY domain").fetchall()
        domains = {str(r["domain"]): int(r["c"]) for r in dom_rows}
        skills_cnt = int(
            conn.execute(
                "SELECT COUNT(DISTINCT source_path) FROM rag_chunks WHERE source_path LIKE '.agents/skills/%/SKILL.md'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    base_dir = omni_dir or resolve_omni_rag_dir(PROJECT_ROOT)
    load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)
    qdrant_configured = bool(_resolve_qdrant_url() and (os.getenv("QDRANT_API_KEY") or "").strip())
    return {
        "local_chunks": chunk_cnt,
        "local_vec_chunks": vec_cnt,
        "skills_indexed": skills_cnt,
        "sqlite_vec_loaded": vec_ok,
        "embedding_model": EMBEDDING_MODEL,
        "reranker_model": RERANKER_MODEL,
        "domains": domains,
        "federated_omni_rag_hot": (base_dir / "rag_hot.sqlite").is_file(),
        "federated_omni_rag_library": (base_dir / "rag_library.sqlite").is_file(),
        "qdrant_readonly_configured": qdrant_configured,
    }
