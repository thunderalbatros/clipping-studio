"""Hybrid Local RAG (SQLite FTS5 + sqlite-vec + fastembed + RRF) & Read-Only Federated Omni-RAG v6 Bridge (v3.1).

Architecture:
- Local Layer (`runs/studio.db`): `rag_chunks`, `rag_chunks_fts` (BM25), `rag_chunks_vec` (`float[384]` via sqlite-vec)
  powered by commercial Apache-2.0 `fastembed` (`BAAI/bge-small-en-v1.5`) respecting `resource_guard.onnx_threads()`.
- Indexes all 6 knowledge gaps (`shortform_craft`, `design_ctr`, `media_engineering`, `bounties_dmca`,
  `gta6_streamers`, `episodic_memory`).
- Federated Read-Only Bridge: optionally queries `web-software-factory/.cache/rag_hot.sqlite` and
  `rag_library.sqlite` strictly via SQLite URI `?mode=ro&immutable=1` (zero writes, zero `-wal`/`-shm` locks,
  silent degradation when running on Colab/GCP or if path does not exist).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.db import DEFAULT_DB_PATH, PROJECT_ROOT, init_db
from src.resource_guard import onnx_threads

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
FASTEMBED_CACHE_DIR = PROJECT_ROOT / "models" / "fastembed_cache"
DEFAULT_OMNI_RAG_DIR = Path(
    os.getenv("OMNI_RAG_CACHE_DIR", r"C:\Users\tomas\Desktop\web-software-factory\.cache")
)
RRF_K = 60

RAG_DOMAINS = (
    "shortform_craft",
    "design_ctr",
    "media_engineering",
    "bounties_dmca",
    "gta6_streamers",
    "episodic_memory",
)

_EMBEDDER_CACHE: Any = None
_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_]+")
_HEADING_RE = re.compile(r"^#{1,3}\s+(.+)$")

EmbedFn = Callable[[list[str]], list[list[float]]]


def _serialize_f32(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


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
    """Create `rag_chunks`, `rag_chunks_fts` (BM25), and `rag_chunks_vec` (`float[384]`) in studio.db."""
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
            conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS rag_chunks_vec USING vec0(
                    chunk_rowid INTEGER PRIMARY KEY,
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


def embed_texts(texts: list[str], embed_fn: EmbedFn | None = None) -> list[list[float]]:
    """Compute normalized 384-d embeddings via fastembed (or injected test embedder)."""
    if not texts:
        return []
    if embed_fn is not None:
        return embed_fn(texts)
    model = _get_fastembed_model()
    return [vec.tolist() for vec in model.embed(texts)]


def _split_markdown_chunks(
    md_path: Path,
    default_domain: str,
    domain_router: Callable[[str, str], str] | None = None,
    max_chars: int = 1600,
) -> list[dict[str, str]]:
    """Split a Markdown file by headings into right-sized RAG chunks."""
    if not md_path.is_file():
        return []
    text = md_path.read_text(encoding="utf-8")
    sections: list[tuple[str, list[str]]] = []
    cur_h = md_path.stem
    cur_lines: list[str] = []

    for ln in text.splitlines():
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

    chunks: list[dict[str, str]] = []
    seq = 0
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if len(body) < 40:
            continue
        # Sub-split very long sections by paragraphs so embeddings stay sharp
        parts: list[str] = []
        if len(body) <= max_chars:
            parts.append(body)
        else:
            buf: list[str] = []
            buf_len = 0
            for para in body.split("\n\n"):
                p = para.strip()
                if not p:
                    continue
                if buf and buf_len + len(p) + 2 > max_chars:
                    parts.append("\n\n".join(buf))
                    buf = [p]
                    buf_len = len(p)
                else:
                    buf.append(p)
                    buf_len += len(p) + 2
            if buf:
                parts.append("\n\n".join(buf))

        for part_idx, part in enumerate(parts, start=1):
            seq += 1
            dom = domain_router(heading, part) if domain_router else default_domain
            h_label = heading if len(parts) == 1 else f"{heading} (part {part_idx})"
            chunks.append(
                {
                    "chunk_id": f"{dom}:{md_path.stem}:{seq:03d}",
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


def _collect_json_chunks(project_root: Path) -> list[dict[str, str]]:
    """Collect structured chunks from context/*.json (streamers, lore, algorithms, style DNA, golden refs)."""
    chunks: list[dict[str, str]] = []

    # 1. Streamer profiles (1 chunk per streamer -> Brecha 5)
    sp_path = project_root / "context" / "streamer_profiles.json"
    if sp_path.is_file():
        try:
            sp_data = json.loads(sp_path.read_text(encoding="utf-8"))
            for sid, info in (sp_data.get("streamers") or {}).items():
                if not isinstance(info, dict):
                    continue
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
                        "chunk_id": f"gta6_streamers:profile:{sid}",
                        "source_path": "context/streamer_profiles.json",
                        "domain": "gta6_streamers",
                        "heading": f"Streamer Profile: {info.get('display_name', sid)} ({sid})",
                        "content": "\n".join(lines),
                    }
                )
        except Exception:
            pass

    # 2. GTA VI Lore index (Brecha 5)
    lore_path = project_root / "context" / "gta6_lore_index.json"
    if lore_path.is_file():
        try:
            lore_data = json.loads(lore_path.read_text(encoding="utf-8"))
            for key, val in lore_data.items():
                if key in {"schema_version", "updated_at", "version"}:
                    continue
                rendered = json.dumps(val, ensure_ascii=False)
                if len(rendered) < 30:
                    continue
                chunks.append(
                    {
                        "chunk_id": f"gta6_streamers:lore:{key}",
                        "source_path": "context/gta6_lore_index.json",
                        "domain": "gta6_streamers",
                        "heading": f"GTA VI Lore Index: {key}",
                        "content": rendered[:1600],
                    }
                )
        except Exception:
            pass

    # 3. Platform algorithms index + viral style DNA + golden references (Brecha 1)
    for fname, prefix, heading in (
        ("platform_algorithms_index.json", "platform_algo", "Platform Algorithms Index"),
        ("viral_style_dna.json", "style_dna", "Empirical Viral Style DNA"),
        ("golden_references.json", "golden_refs", "Golden Viral References"),
    ):
        fpath = project_root / "context" / fname
        if not fpath.is_file():
            continue
        try:
            raw = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for idx, (k, v) in enumerate(raw.items(), start=1):
                    txt = json.dumps({k: v}, ensure_ascii=False)
                    if len(txt) < 35:
                        continue
                    chunks.append(
                        {
                            "chunk_id": f"shortform_craft:{prefix}:{idx:03d}",
                            "source_path": f"context/{fname}",
                            "domain": "shortform_craft",
                            "heading": f"{heading}: {k}",
                            "content": txt[:1600],
                        }
                    )
        except Exception:
            pass

    return chunks


def _collect_episodic_and_bounty_chunks(db_path: Path, project_root: Path) -> list[dict[str, str]]:
    """Collect live episodic memory (clips + BlindCritic feedback + publish_metrics) and bounty campaigns."""
    chunks: list[dict[str, str]] = []
    init_db(db_path)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        # 1. Clips in studio.db (Brecha 6)
        clip_rows = conn.execute(
            """
            SELECT run_id, clip_id, streamer_id, start_trim, end_trim, layout_mode,
                   matched_lore_entity, critic_overall, media_check_passed, lufs, dbtp, packaging_json
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
            outcome = "WINNER" if (int(r["media_check_passed"] or 0) == 1 and float(r["critic_overall"] or 0) >= 0.75) else "REVIEW_OR_FAIL"
            dur = round(max(0.0, float(r["end_trim"] or 0) - float(r["start_trim"] or 0)), 1)
            body = (
                f"Episodic Clip [{outcome}] run={r['run_id']} clip={r['clip_id']} streamer={r['streamer_id']} "
                f"dur={dur}s layout={r['layout_mode']} lore={r['matched_lore_entity'] or 'none'} "
                f"critic_overall={float(r['critic_overall'] or 0):.2f} media_check={bool(r['media_check_passed'])} "
                f"lufs={float(r['lufs'] or 0):.1f} dbtp={float(r['dbtp'] or 0):.1f} "
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

        # 2. Publish metrics & telemetry events (Brecha 6)
        tel_rows = conn.execute(
            """
            SELECT id, ts, run_id, event_type, payload_json
            FROM telemetry
            WHERE event_type IN ('publish_metrics', 'critic_feedback', 'compilation_rendered')
            ORDER BY id DESC
            LIMIT 100
            """
        ).fetchall()
        for r in tel_rows:
            body = f"Telemetry {r['event_type']} run={r['run_id']} ts={r['ts']}: {str(r['payload_json'])[:1200]}"
            chunks.append(
                {
                    "chunk_id": f"episodic_memory:telemetry:{r['id']}",
                    "source_path": "runs/studio.db#telemetry",
                    "domain": "episodic_memory",
                    "heading": f"Episodic Telemetry ({r['event_type']}): {r['run_id']}",
                    "content": body,
                }
            )

        # 3. Bounty campaigns (Brecha 4)
        camp_rows = conn.execute(
            "SELECT campaign_id, platform, title, cpm_usd, burn_rate_pct, geo_lock, voice_policy, brief_text, verdict FROM campaigns LIMIT 100"
        ).fetchall()
        for r in camp_rows:
            body = (
                f"Campaign {r['campaign_id']} [{r['verdict']}] platform={r['platform']} title={r['title']} "
                f"cpm=${float(r['cpm_usd'] or 0):.2f} burn={float(r['burn_rate_pct'] or 0):.1f}% "
                f"geo={r['geo_lock']} voice={r['voice_policy']} brief={str(r['brief_text'] or '')[:700]}"
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

        # 3b. Real post-publish analytics metrics -> episodic_memory
        pm_rows = conn.execute(
            "SELECT id, ts, run_id, payload_json FROM telemetry WHERE event_type = 'publish_metrics' ORDER BY id DESC LIMIT 40"
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

    # 4. BlindCritic feedback from runs/*/state.json (captures both winners AND rejected errors!)
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
                            "content": body[:1500],
                        }
                    )
            except Exception:
                continue

    return chunks


def collect_all_knowledge_chunks(
    project_root: Path = PROJECT_ROOT,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Collect all chunks covering the 6 knowledge gaps for local RAG indexing."""
    chunks: list[dict[str, str]] = []

    # Gap 1: Short-Form 9:16 & 5-Network Algorithms
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "style_bible.md", "shortform_craft"))
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "algorithms_and_virality_bible.md", "shortform_craft"))
    chunks.extend(
        _split_markdown_chunks(
            project_root / "docs" / "shortform_and_editing_bible.md",
            "shortform_craft",
            domain_router=_route_editing_bible_domain,
        )
    )

    # Gap 2: Design & CTR Packaging
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "design_bible.md", "design_ctr"))
    chunks.extend(
        _split_markdown_chunks(
            project_root / ".agents" / "skills" / "visual-packaging-designer" / "SKILL.md",
            "design_ctr",
        )
    )

    # Gap 3: Media Engineering Recipes
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "engineering_patterns.md", "media_engineering"))
    chunks.extend(
        _split_markdown_chunks(
            project_root / ".agents" / "skills" / "media-engineer" / "SKILL.md",
            "media_engineering",
        )
    )

    # Gap 4: Bounties CPM & Anti-DMCA
    chunks.extend(_split_markdown_chunks(project_root / "docs" / "marketing_playbook.md", "bounties_dmca"))
    chunks.extend(
        _split_markdown_chunks(
            project_root / ".agents" / "skills" / "bounty-and-distribution-ops" / "SKILL.md",
            "bounties_dmca",
        )
    )

    # Gap 5: GTA VI Lore & Streamers
    chunks.extend(_split_markdown_chunks(project_root / "context" / "gta6_living_brief.md", "gta6_streamers"))
    chunks.extend(_collect_json_chunks(project_root))

    # Gap 6: Episodic Memory + Campaigns from studio.db & runs/*/state.json
    chunks.extend(_collect_episodic_and_bounty_chunks(db_path=db_path, project_root=project_root))

    # Deduplicate by chunk_id preserving last definition
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
    """Idempotently ingest all 6 knowledge gaps into `rag_chunks`, `rag_chunks_fts`, and `rag_chunks_vec`."""
    init_rag_tables(db_path)
    chunks = chunks_override if chunks_override is not None else collect_all_knowledge_chunks(project_root, db_path)
    now = datetime.now(UTC).isoformat()

    conn, vec_ok = _connect_rag(db_path)
    use_vec = vec_ok and not skip_vectors
    inserted, updated, unchanged, embedded = 0, 0, 0, 0
    to_embed: list[tuple[int, str]] = []
    domain_counts: dict[str, int] = {}

    try:
        existing_rows = {
            str(r["chunk_id"]): (int(r["rowid"]), str(r["content_sha256"]))
            for r in conn.execute("SELECT rowid, chunk_id, content_sha256 FROM rag_chunks").fetchall()
        }
        existing_vec_ids: set[int] = set()
        if use_vec:
            try:
                existing_vec_ids = {
                    int(r[0]) for r in conn.execute("SELECT chunk_rowid FROM rag_chunks_vec").fetchall()
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
                if use_vec and rowid not in existing_vec_ids:
                    to_embed.append((rowid, f"{heading}\n{content}"))
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
                to_embed.append((rowid, f"{heading}\n{content}"))

        # Prune stale chunk_ids no longer present + any orphan vectors in rag_chunks_vec
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

        # Rebuild external-content FTS5 index atomically (guaranteed 100% consistent)
        conn.execute("INSERT INTO rag_chunks_fts(rag_chunks_fts) VALUES('rebuild')")

        # Compute & upsert missing/changed embeddings in batches
        if use_vec and to_embed:
            batch_size = 32
            for start in range(0, len(to_embed), batch_size):
                batch = to_embed[start : start + batch_size]
                vecs = embed_texts([txt for _, txt in batch], embed_fn=embed_fn)
                for (rowid, _), vec in zip(batch, vecs, strict=True):
                    conn.execute("DELETE FROM rag_chunks_vec WHERE chunk_rowid = ?", (rowid,))
                    conn.execute(
                        "INSERT INTO rag_chunks_vec (chunk_rowid, embedding) VALUES (?, ?)",
                        (rowid, _serialize_f32(vec)),
                    )
                    embedded += 1

        conn.commit()
        total_rows = int(conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
        vec_rows = int(conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0]) if use_vec else 0
    finally:
        conn.close()

    return {
        "db_path": str(db_path),
        "total_chunks": total_rows,
        "vec_chunks": vec_rows,
        "vec_enabled": use_vec,
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
    for t in tokens:
        low = t.lower()
        if low in {"or", "and", "not", "near", "the", "for", "with", "from", "that", "this", "para", "como", "con", "del", "las", "los"}:
            continue
        if low not in seen:
            seen.add(low)
            dedup.append(f'"{t}"')
    if not dedup:
        dedup = [f'"{t}"' for t in tokens[:6]]
    return " OR ".join(dedup[:12])


_WEBDEV_NOISE_PATTERNS = (
    "react hook",
    "react-hook-form",
    "react native",
    "reactjs",
    "react:",
    "react up",
    "next.js",
    "nextjs",
    "tailwind",
    "useeffect",
    "usestate",
    "usememo",
    "usecallback",
    "usecontext",
    "typescript",
    "javascript framework",
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
    "rest api",
    "sql injection",
    "rls policy",
    "eslint",
    "biome.json",
    "node_modules",
    "css grid",
    "flexbox",
    "dom manipulation",
    "frontend component",
    "backend endpoint",
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
    "source code",
    "software engineering",
    "kubernetes",
    "docker",
    "terraform",
    "git hook",
    "pre-commit",
    "mern",
    "full stack",
    "full-stack",
    "full_stack",
    "mongodb",
    "node.js",
    "nodejs",
    "web development",
    "javascript",
    "html/css",
    "http server",
    "microservices",
    "kubernetes",
)
_WEBDEV_NOISE_CATEGORIES = frozenset(
    {
        "frontend",
        "backend",
        "web",
        "webdev",
        "css",
        "react",
        "nextjs",
        "security_web",
        "cloudflare",
        "database",
        "devops",
        "api",
        "agent-skill",
        "tooling",
        "infra",
    }
)


def _is_webdev_noise_hit(rel_path: str, category: str, heading: str, text: str) -> bool:
    """Return True if a federated Omni-RAG v6 hit is web-dev/software-engineering noise."""
    cat_low = category.strip().lower()
    if cat_low in _WEBDEV_NOISE_CATEGORIES:
        return True
    path_low = rel_path.replace("\\", "/").lower()
    if any(
        seg in path_low
        for seg in (
            "/react",
            "/next",
            "/css",
            "/tailwind",
            "/frontend",
            "/backend",
            "/cloudflare",
            "/workers",
            "/prisma",
            "/drizzle",
            "/supabase",
            ".agents/skills/",
            "react_hook",
            "hook_form",
            "hook-integration",
            "beautiful_code",
            "clean_code",
            "pragmatic_programmer",
            "designing_data_intensive",
            "mern_stack",
            "full_stack",
            "node_js",
            "web_dev",
            "software_factory/",
            "architecture_and_books/",
            "cybersecurity/",
            "bug_bounty",
            "pro_git",
            "_react_",
            "react_",
            ".tsx",
            ".jsx",
            ".css",
            ".ts",
            ".mjs",
        )
    ):
        return True
    hay = f"{rel_path} {heading} {text[:500]}".lower()
    return any(pat in hay for pat in _WEBDEV_NOISE_PATTERNS)


def query_omni_rag_federated(
    query: str,
    limit: int = 4,
    omni_dir: Path | None = None,
    exclude_webdev_noise: bool = True,
) -> list[dict[str, Any]]:
    """Read-only FTS5 BM25 query against Omni-RAG v6 (`rag_hot.sqlite` + `rag_library.sqlite`).

    Strictly opens with `?mode=ro&immutable=1` so zero `-wal`/`-shm` files are created or locked
    in `web-software-factory/.cache/`. Filters out React/CSS/backend web-dev noise by default
    so audiovisual queries (e.g. "hook") only retrieve relevant psychology/editing/UX knowledge.
    """
    fts_q = _build_safe_fts_query(query)
    if not fts_q:
        return []

    base_dir = omni_dir or DEFAULT_OMNI_RAG_DIR
    if not base_dir.is_dir():
        return []

    raw_fetch = max(limit * 6, 24)
    hits: list[dict[str, Any]] = []
    for db_name, source_tag in (("rag_hot.sqlite", "omni_rag_hot"), ("rag_library.sqlite", "omni_rag_library")):
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
                        "content": txt[:900],
                        "bm25_rank": kept_rank,
                        "rrf_score": round(1.9 / (RRF_K + kept_rank), 6),
                    }
                )
                if kept_rank >= limit:
                    break
        except Exception:
            continue
        finally:
            if conn is not None:
                conn.close()

    hits.sort(key=lambda x: x["rrf_score"], reverse=True)
    return hits[:limit]


def query_rag(
    query: str,
    domain: str | None = None,
    top_k: int = 5,
    federated: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
    omni_dir: Path | None = None,
    embed_fn: EmbedFn | None = None,
    use_vectors: bool = True,
) -> list[dict[str, Any]]:
    """Hybrid BM25 + sqlite-vec RRF ($k=60$) search over studio.db with optional read-only Omni-RAG v6 bridge."""
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

        # 2. Local sqlite-vec vector KNN leg
        if use_vectors and vec_ok and query.strip():
            try:
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
                        (_serialize_f32(q_vec), min(cand_limit * 2, vec_count)),
                    ).fetchall()
                    rank_idx = 0
                    for r in vec_rows:
                        if domain and str(r["domain"]) != domain:
                            continue
                        rank_idx += 1
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
                        if rank_idx >= cand_limit:
                            break
            except Exception:
                # Degrade gracefully to BM25-only if embedding runtime is unavailable
                pass
    finally:
        conn.close()

    # 3. Reciprocal Rank Fusion (k=60)
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

    # 4. Optional Federated Read-Only Bridge to Omni-RAG v6
    if federated:
        fed_hits = query_omni_rag_federated(query, limit=max(2, top_k), omni_dir=omni_dir)
        fused.sort(key=lambda x: x["rrf_score"], reverse=True)
        if fed_hits:
            # Guarantee balanced blend: keep top local hits + at least 2 federated Omni-RAG v6 hits when available
            fed_slots = min(len(fed_hits), max(1, top_k // 2))
            local_slots = max(0, top_k - fed_slots)
            blended = fused[:local_slots] + fed_hits[:fed_slots]
            blended.sort(key=lambda x: x["rrf_score"], reverse=True)
            return blended[:top_k]

    fused.sort(key=lambda x: x["rrf_score"], reverse=True)
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


def build_curator_rag_context(
    streamer_id: str,
    topic_query: str,
    stage: str = "pass2",
    max_tokens: int = 550,
    db_path: Path = DEFAULT_DB_PATH,
    use_vectors: bool = True,
) -> str:
    """Retrieve bounded RAG context (streamer profile + episodic winners/errors + craft/CTR rules) for curator.py."""
    hits: list[dict[str, Any]] = []
    # 1. Streamer profile
    hits.extend(
        query_rag(
            f"{streamer_id} profile hook pacing",
            domain="gta6_streamers",
            top_k=1,
            db_path=db_path,
            use_vectors=use_vectors,
        )
    )
    # 2. Episodic memory (past winners + rejected errors)
    hits.extend(
        query_rag(
            f"{streamer_id} {topic_query} WINNER REJECTED_ERROR",
            domain="episodic_memory",
            top_k=3,
            db_path=db_path,
            use_vectors=use_vectors,
        )
    )
    # 3. Domain-specific guidance (editing vs packaging/CTR)
    target_domain = "design_ctr" if stage == "packaging" else "shortform_craft"
    hits.extend(
        query_rag(
            topic_query or "hook retention seamless loop curiosity gap",
            domain=target_domain,
            top_k=2,
            db_path=db_path,
            use_vectors=use_vectors,
        )
    )
    # Deduplicate by chunk_id while keeping priority order
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for h in hits:
        cid = str(h["chunk_id"])
        if cid not in seen:
            seen.add(cid)
            unique.append(h)
    return format_rag_context(unique, max_tokens=max_tokens)


def inspect_rag_status(db_path: Path = DEFAULT_DB_PATH, omni_dir: Path | None = None) -> dict[str, Any]:
    """Return non-intrusive RAG health summary for `cli doctor`."""
    init_rag_tables(db_path)
    conn, vec_ok = _connect_rag(db_path)
    try:
        chunk_cnt = int(conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0])
        vec_cnt = int(conn.execute("SELECT COUNT(*) FROM rag_chunks_vec").fetchone()[0]) if vec_ok else 0
        dom_rows = conn.execute("SELECT domain, COUNT(*) AS c FROM rag_chunks GROUP BY domain").fetchall()
        domains = {str(r["domain"]): int(r["c"]) for r in dom_rows}
    finally:
        conn.close()

    base_dir = omni_dir or DEFAULT_OMNI_RAG_DIR
    return {
        "local_chunks": chunk_cnt,
        "local_vec_chunks": vec_cnt,
        "sqlite_vec_loaded": vec_ok,
        "embedding_model": EMBEDDING_MODEL,
        "domains": domains,
        "federated_omni_rag_hot": (base_dir / "rag_hot.sqlite").is_file(),
        "federated_omni_rag_library": (base_dir / "rag_library.sqlite").is_file(),
    }
