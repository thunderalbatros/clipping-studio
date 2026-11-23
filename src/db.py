"""SQLite WAL + FTS5 local persistence store (`runs/studio.db`, Hito 2.0)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "runs" / "studio.db"


def _connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> Path:
    """Create studio.db tables (`vods`, `slices`, `clips`, `telemetry`, `campaigns`, `transcripts_fts`)."""
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS vods (
                source_url TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                streamer_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                duration_sec REAL DEFAULT 0.0,
                last_run_id TEXT DEFAULT '',
                status TEXT DEFAULT 'discovered',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS slices (
                run_id TEXT NOT NULL,
                slice_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                platform TEXT NOT NULL,
                start_sec REAL NOT NULL,
                end_sec REAL NOT NULL,
                signal_source TEXT NOT NULL,
                score REAL DEFAULT 0.0,
                source_width INTEGER DEFAULT 0,
                source_height INTEGER DEFAULT 0,
                source_fps REAL DEFAULT 0.0,
                music_prob_peak REAL DEFAULT 0.0,
                dmca_flag INTEGER DEFAULT 0,
                dmca_detector TEXT DEFAULT 'yamnet_onnx',
                scene_type TEXT DEFAULT '',
                approved_p2 INTEGER DEFAULT 0,
                video_path TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, slice_id)
            );

            CREATE TABLE IF NOT EXISTS clips (
                run_id TEXT NOT NULL,
                clip_id TEXT NOT NULL,
                slice_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                streamer_id TEXT NOT NULL,
                start_trim REAL NOT NULL,
                end_trim REAL NOT NULL,
                layout_mode TEXT DEFAULT 'split_webcam_top',
                webcam_upscale_factor REAL DEFAULT 1.0,
                no_webcam_gameplay_overlap INTEGER DEFAULT 1,
                matched_lore_entity TEXT DEFAULT '',
                critic_overall REAL DEFAULT 0.0,
                media_check_passed INTEGER DEFAULT 0,
                lufs REAL DEFAULT 0.0,
                dbtp REAL DEFAULT 0.0,
                output_mp4 TEXT DEFAULT '',
                packaging_json TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, clip_id)
            );

            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                cpm_usd REAL DEFAULT 0.0,
                burn_rate_pct REAL DEFAULT 0.0,
                geo_lock TEXT DEFAULT '',
                geo_requirement TEXT DEFAULT 'global',
                platforms TEXT DEFAULT '',
                audio_policy TEXT DEFAULT 'strict_clean',
                url TEXT DEFAULT '',
                voice_policy TEXT DEFAULT 'none',
                brief_text TEXT DEFAULT '',
                verdict TEXT DEFAULT 'PENDING',
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
                run_id,
                slice_id,
                full_text,
                lore_entities
            );

            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                domain TEXT NOT NULL,
                heading TEXT NOT NULL,
                content TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(
                chunk_id UNINDEXED,
                domain,
                heading,
                content
            );

            CREATE TABLE IF NOT EXISTS scene_blueprints (
                scene_key TEXT PRIMARY KEY,
                game TEXT NOT NULL,
                scene_query TEXT NOT NULL,
                match_level TEXT NOT NULL,
                references_count INTEGER DEFAULT 0,
                blueprint_json TEXT NOT NULL,
                summary_prompt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS creators (
                creator_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'bounty_target',
                primary_platform TEXT DEFAULT 'twitch',
                platforms_json TEXT DEFAULT '["twitch","kick","youtube"]',
                language TEXT DEFAULT 'en',
                webcam_style TEXT DEFAULT 'auto_detect',
                webcam_box_json TEXT DEFAULT '',
                style_notes TEXT DEFAULT '',
                auto_profiled INTEGER DEFAULT 0,
                profile_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # Non-destructive column migrations for existing studio.db instances
        clip_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(clips)").fetchall()}
        if "status" not in clip_cols:
            conn.execute("ALTER TABLE clips ADD COLUMN status TEXT DEFAULT 'READY_HITL'")
        camp_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(campaigns)").fetchall()}
        if "geo_requirement" not in camp_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN geo_requirement TEXT DEFAULT 'global'")
        if "platforms" not in camp_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN platforms TEXT DEFAULT ''")
        if "audio_policy" not in camp_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN audio_policy TEXT DEFAULT 'strict_clean'")
        if "url" not in camp_cols:
            conn.execute("ALTER TABLE campaigns ADD COLUMN url TEXT DEFAULT ''")
    return db_path


def has_processed_vod(source_url: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT status FROM vods WHERE source_url = ?", (source_url,)).fetchone()
        return bool(row and row["status"] in {"reviewed", "curated"})


def upsert_vod(
    source_url: str,
    platform: str,
    streamer_id: str,
    title: str = "",
    duration_sec: float = 0.0,
    run_id: str = "",
    status: str = "mined",
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO vods (source_url, platform, streamer_id, title, duration_sec, last_run_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE vods.title END,
                duration_sec = CASE WHEN excluded.duration_sec > 0 THEN excluded.duration_sec ELSE vods.duration_sec END,
                last_run_id = excluded.last_run_id,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (source_url, platform, streamer_id, title, duration_sec, run_id, status, now, now),
        )


def upsert_slice(
    run_id: str,
    slice_id: str,
    source_url: str,
    platform: str,
    start_sec: float,
    end_sec: float,
    signal_source: str,
    score: float,
    video_path: str,
    source_width: int = 0,
    source_height: int = 0,
    source_fps: float = 0.0,
    music_prob_peak: float = 0.0,
    dmca_flag: bool = False,
    dmca_detector: str = "yamnet_onnx",
    scene_type: str = "",
    approved_p2: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO slices (
                run_id, slice_id, source_url, platform, start_sec, end_sec, signal_source,
                score, source_width, source_height, source_fps, music_prob_peak, dmca_flag,
                dmca_detector, scene_type, approved_p2, video_path, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, slice_id) DO UPDATE SET
                source_width = CASE WHEN excluded.source_width > 0 THEN excluded.source_width ELSE slices.source_width END,
                source_height = CASE WHEN excluded.source_height > 0 THEN excluded.source_height ELSE slices.source_height END,
                source_fps = CASE WHEN excluded.source_fps > 0 THEN excluded.source_fps ELSE slices.source_fps END,
                music_prob_peak = excluded.music_prob_peak,
                dmca_flag = excluded.dmca_flag,
                dmca_detector = excluded.dmca_detector,
                scene_type = CASE WHEN excluded.scene_type != '' THEN excluded.scene_type ELSE slices.scene_type END,
                approved_p2 = excluded.approved_p2,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                slice_id,
                source_url,
                platform,
                start_sec,
                end_sec,
                signal_source,
                score,
                source_width,
                source_height,
                source_fps,
                music_prob_peak,
                int(dmca_flag),
                dmca_detector,
                scene_type,
                int(approved_p2),
                video_path,
                now,
            ),
        )


def upsert_transcript_fts(
    run_id: str,
    slice_id: str,
    full_text: str,
    lore_entities: str = "",
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM transcripts_fts WHERE run_id = ? AND slice_id = ?", (run_id, slice_id))
        conn.execute(
            "INSERT INTO transcripts_fts (run_id, slice_id, full_text, lore_entities) VALUES (?, ?, ?, ?)",
            (run_id, slice_id, full_text, lore_entities),
        )


def upsert_clip(
    run_id: str,
    clip_id: str,
    slice_id: str,
    source_url: str,
    streamer_id: str,
    start_trim: float,
    end_trim: float,
    layout_mode: str,
    webcam_upscale_factor: float,
    no_webcam_gameplay_overlap: bool,
    matched_lore_entity: str,
    critic_overall: float,
    media_check_passed: bool,
    lufs: float,
    dbtp: float,
    output_mp4: str,
    packaging_json: str = "",
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO clips (
                run_id, clip_id, slice_id, source_url, streamer_id, start_trim, end_trim,
                layout_mode, webcam_upscale_factor, no_webcam_gameplay_overlap, matched_lore_entity,
                critic_overall, media_check_passed, lufs, dbtp, output_mp4, packaging_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, clip_id) DO UPDATE SET
                layout_mode = excluded.layout_mode,
                webcam_upscale_factor = excluded.webcam_upscale_factor,
                no_webcam_gameplay_overlap = excluded.no_webcam_gameplay_overlap,
                matched_lore_entity = excluded.matched_lore_entity,
                critic_overall = excluded.critic_overall,
                media_check_passed = excluded.media_check_passed,
                lufs = excluded.lufs,
                dbtp = excluded.dbtp,
                output_mp4 = excluded.output_mp4,
                packaging_json = excluded.packaging_json,
                updated_at = excluded.updated_at
            """,
            (
                run_id,
                clip_id,
                slice_id,
                source_url,
                streamer_id,
                start_trim,
                end_trim,
                layout_mode,
                webcam_upscale_factor,
                int(no_webcam_gameplay_overlap),
                matched_lore_entity,
                critic_overall,
                int(media_check_passed),
                lufs,
                dbtp,
                output_mp4,
                packaging_json,
                now,
            ),
        )


def insert_telemetry(run_id: str, event_type: str, payload: dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO telemetry (ts, run_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
            (now, run_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )


def get_daily_llm_spend_usd(day_utc: str | None = None, db_path: Path = DEFAULT_DB_PATH) -> float:
    """Sum estimated USD cost of all `llm_usage` events recorded in studio.db for a UTC day (YYYY-MM-DD)."""
    init_db(db_path)
    prefix = day_utc or datetime.now(UTC).strftime("%Y-%m-%d")
    total = 0.0
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT payload_json FROM telemetry WHERE event_type = 'llm_usage' AND ts LIKE ?",
            (f"{prefix}%",),
        ).fetchall()
        for r in rows:
            try:
                data = json.loads(r["payload_json"])
                total += float(data.get("est_cost_usd") or 0.0)
            except (ValueError, TypeError):
                continue
    return round(total, 6)


def search_fts(query: str, limit: int = 10, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, str]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, slice_id, full_text, lore_entities FROM transcripts_fts WHERE transcripts_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_telemetry_by_run_id(run_id: str, event_type: str | None = None, db_path: Path = DEFAULT_DB_PATH) -> int:
    """Remove synthetic or test telemetry rows cleanly by run_id (and optional event_type)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        if event_type:
            cur = conn.execute("DELETE FROM telemetry WHERE run_id = ? AND event_type = ?", (run_id, event_type))
        else:
            cur = conn.execute("DELETE FROM telemetry WHERE run_id = ?", (run_id,))
        return int(cur.rowcount)


def upsert_campaign(
    campaign_id: str,
    platform: str,
    title: str,
    cpm_usd: float = 0.0,
    burn_rate_pct: float = 0.0,
    geo_lock: str = "",
    voice_policy: str = "none",
    brief_text: str = "",
    verdict: str = "PENDING_HITL",
    db_path: Path = DEFAULT_DB_PATH,
    *,
    geo_requirement: str = "global",
    platforms: str = "",
    audio_policy: str = "strict_clean",
    url: str = "",
) -> dict[str, Any]:
    """Upsert a bounty campaign snapshot and preserve human APPROVED/REJECTED verdict if already set."""
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    prev_burn: float | None = None
    prev_ts: str | None = None
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT burn_rate_pct, verdict, updated_at FROM campaigns WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchone()
        final_verdict = verdict
        if existing:
            prev_burn = float(existing["burn_rate_pct"])
            prev_ts = str(existing["updated_at"])
            if str(existing["verdict"]) in {"APPROVED", "REJECTED"} and verdict == "PENDING_HITL":
                final_verdict = str(existing["verdict"])
        conn.execute(
            """
            INSERT INTO campaigns (
                campaign_id, platform, title, cpm_usd, burn_rate_pct, geo_lock,
                geo_requirement, platforms, audio_policy, url,
                voice_policy, brief_text, verdict, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id) DO UPDATE SET
                platform = excluded.platform,
                title = excluded.title,
                cpm_usd = CASE WHEN excluded.cpm_usd > 0 THEN excluded.cpm_usd ELSE campaigns.cpm_usd END,
                burn_rate_pct = excluded.burn_rate_pct,
                geo_lock = CASE WHEN excluded.geo_lock != '' THEN excluded.geo_lock ELSE campaigns.geo_lock END,
                geo_requirement = CASE WHEN excluded.geo_requirement != '' THEN excluded.geo_requirement ELSE campaigns.geo_requirement END,
                platforms = CASE WHEN excluded.platforms != '' THEN excluded.platforms ELSE campaigns.platforms END,
                audio_policy = CASE WHEN excluded.audio_policy != '' THEN excluded.audio_policy ELSE campaigns.audio_policy END,
                url = CASE WHEN excluded.url != '' THEN excluded.url ELSE campaigns.url END,
                voice_policy = excluded.voice_policy,
                brief_text = CASE WHEN excluded.brief_text != '' THEN excluded.brief_text ELSE campaigns.brief_text END,
                verdict = excluded.verdict,
                updated_at = excluded.updated_at
            """,
            (
                campaign_id,
                platform,
                title,
                cpm_usd,
                burn_rate_pct,
                geo_lock,
                geo_requirement,
                platforms,
                audio_policy,
                url,
                voice_policy,
                brief_text,
                final_verdict,
                now,
            ),
        )
    delta_burn = round(burn_rate_pct - prev_burn, 2) if prev_burn is not None else None
    return {
        "campaign_id": campaign_id,
        "verdict": final_verdict,
        "burn_rate_pct": burn_rate_pct,
        "prev_burn_rate_pct": prev_burn,
        "burn_delta_pct": delta_burn,
        "prev_updated_at": prev_ts,
        "geo_requirement": geo_requirement,
        "audio_policy": audio_policy,
    }


def delete_campaign(campaign_id: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    init_db(db_path)
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM campaigns WHERE campaign_id = ?", (campaign_id,))
        return cur.rowcount > 0


def list_campaigns(
    verdict: str | None = None,
    geo_priority: bool = True,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    init_db(db_path)
    order_clause = (
        "ORDER BY CASE geo_requirement WHEN 'global' THEN 0 WHEN 'latam' THEN 1 ELSE 2 END ASC, cpm_usd DESC, updated_at DESC"
        if geo_priority
        else "ORDER BY updated_at DESC"
    )
    with _connect(db_path) as conn:
        if verdict:
            rows = conn.execute(
                f"SELECT * FROM campaigns WHERE verdict = ? {order_clause}",
                (verdict,),
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT * FROM campaigns {order_clause}").fetchall()
        return [dict(r) for r in rows]


def set_campaign_verdict(campaign_id: str, verdict: str, db_path: Path = DEFAULT_DB_PATH) -> bool:
    """Apply explicit human-in-the-loop verdict (APPROVED / REJECTED / PENDING_HITL) to a campaign."""
    if verdict not in {"APPROVED", "REJECTED", "PENDING_HITL"}:
        raise ValueError(f"Invalid campaign verdict: {verdict!r}")
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE campaigns SET verdict = ?, updated_at = ? WHERE campaign_id = ?",
            (verdict, now, campaign_id),
        )
        return cur.rowcount > 0


def list_approved_clips(
    streamer_id: str | None = None,
    limit: int = 50,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return rendered clips that passed media_check (for Long-Form 16:9 compilation or analytics)."""
    init_db(db_path)
    with _connect(db_path) as conn:
        if streamer_id:
            rows = conn.execute(
                "SELECT * FROM clips WHERE media_check_passed = 1 AND streamer_id = ? ORDER BY critic_overall DESC, updated_at DESC LIMIT ?",
                (streamer_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM clips WHERE media_check_passed = 1 ORDER BY critic_overall DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_knowledge_chunks(
    chunks: list[dict[str, str]],
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Idempotently ingest structured knowledge chunks into SQLite + FTS5 (skips unchanged SHA-256)."""
    import hashlib

    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    inserted, updated, unchanged = 0, 0, 0
    with _connect(db_path) as conn:
        for ch in chunks:
            cid = str(ch["chunk_id"])
            src = str(ch["source_path"])
            dom = str(ch["domain"])
            heading = str(ch["heading"])
            content = str(ch["content"]).strip()
            sha = hashlib.sha256(f"{dom}|{heading}|{content}".encode()).hexdigest()
            existing = conn.execute(
                "SELECT content_sha256 FROM knowledge_chunks WHERE chunk_id = ?",
                (cid,),
            ).fetchone()
            if existing and str(existing["content_sha256"]) == sha:
                unchanged += 1
                continue
            conn.execute(
                """
                INSERT INTO knowledge_chunks (chunk_id, source_path, domain, heading, content, content_sha256, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    domain = excluded.domain,
                    heading = excluded.heading,
                    content = excluded.content,
                    content_sha256 = excluded.content_sha256,
                    updated_at = excluded.updated_at
                """,
                (cid, src, dom, heading, content, sha, now),
            )
            conn.execute("DELETE FROM knowledge_chunks_fts WHERE chunk_id = ?", (cid,))
            conn.execute(
                "INSERT INTO knowledge_chunks_fts (chunk_id, domain, heading, content) VALUES (?, ?, ?, ?)",
                (cid, dom, heading, content),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged, "total_input": len(chunks)}


def ingest_markdown_knowledge(
    md_path: Path,
    domain: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Split a Markdown bible by ## / ### headings and ingest every section into `knowledge_chunks_fts`."""
    import re

    text = md_path.read_text(encoding="utf-8")
    sections: list[tuple[str, list[str]]] = []
    cur_h = md_path.stem
    cur_lines: list[str] = []
    for ln in text.splitlines():
        if re.match(r"^#{2,3}\s+", ln):
            if cur_lines and "".join(cur_lines).strip():
                sections.append((cur_h, cur_lines))
            cur_h = ln.lstrip("#").strip()
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
    for idx, (heading, lines) in enumerate(sections, start=1):
        body = "\n".join(lines).strip()
        if len(body) < 40:
            continue
        chunks.append(
            {
                "chunk_id": f"{domain}:{md_path.stem}:{idx:03d}",
                "source_path": rel_src,
                "domain": domain,
                "heading": heading,
                "content": body,
            }
        )
    return upsert_knowledge_chunks(chunks, db_path=db_path)


def search_knowledge_fts(
    query: str,
    domain: str | None = None,
    limit: int = 5,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, str]]:
    """Query `knowledge_chunks_fts` using BM25 ranking with optional domain filter."""
    init_db(db_path)
    with _connect(db_path) as conn:
        if domain:
            rows = conn.execute(
                """
                SELECT k.chunk_id, k.source_path, k.domain, k.heading, k.content
                FROM knowledge_chunks_fts f
                JOIN knowledge_chunks k ON k.chunk_id = f.chunk_id
                WHERE knowledge_chunks_fts MATCH ? AND k.domain = ?
                ORDER BY bm25(knowledge_chunks_fts)
                LIMIT ?
                """,
                (query, domain, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT k.chunk_id, k.source_path, k.domain, k.heading, k.content
                FROM knowledge_chunks_fts f
                JOIN knowledge_chunks k ON k.chunk_id = f.chunk_id
                WHERE knowledge_chunks_fts MATCH ?
                ORDER BY bm25(knowledge_chunks_fts)
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_scene_blueprint(
    scene_key: str,
    game: str,
    scene_query: str,
    match_level: str,
    references_count: int,
    blueprint_json: str,
    summary_prompt: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    """Persist or update a reusable SceneBlueprint in SQLite `scene_blueprints`."""
    init_db(db_path)
    now = datetime.now(UTC).isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scene_blueprints (
                scene_key, game, scene_query, match_level, references_count,
                blueprint_json, summary_prompt, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scene_key) DO UPDATE SET
                game = excluded.game,
                scene_query = excluded.scene_query,
                match_level = excluded.match_level,
                references_count = excluded.references_count,
                blueprint_json = excluded.blueprint_json,
                summary_prompt = excluded.summary_prompt,
                updated_at = excluded.updated_at
            """,
            (scene_key, game, scene_query, match_level, references_count, blueprint_json, summary_prompt, now, now),
        )


def get_scene_blueprint(scene_key: str, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    """Retrieve a cached SceneBlueprint row by canonical `scene_key`."""
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM scene_blueprints WHERE scene_key = ?", (scene_key,)).fetchone()
        return dict(row) if row else None


def list_scene_blueprints(
    game: str | None = None,
    limit: int = 20,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if game:
            rows = conn.execute(
                "SELECT scene_key, game, scene_query, match_level, references_count, updated_at "
                "FROM scene_blueprints WHERE LOWER(game) = LOWER(?) ORDER BY updated_at DESC LIMIT ?",
                (game, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT scene_key, game, scene_query, match_level, references_count, updated_at "
                "FROM scene_blueprints ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def upsert_creator(
    creator_id: str,
    display_name: str,
    *,
    tier: str = "bounty_target",
    primary_platform: str = "twitch",
    platforms: list[str] | None = None,
    language: str = "en",
    webcam_style: str = "auto_detect",
    webcam_box: tuple[int, int, int, int] | list[int] | None = None,
    style_notes: str = "",
    auto_profiled: bool = False,
    profile_extra: dict[str, Any] | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Persist or update a creator profile in SQLite `creators` table (radar_seed | bounty_target | client)."""
    if tier not in {"radar_seed", "bounty_target", "client"}:
        raise ValueError(f"Invalid creator tier: {tier!r}")
    init_db(db_path)
    cid = creator_id.strip().lower()
    now = datetime.now(UTC).isoformat()
    plats = list(platforms) if platforms else [primary_platform or "twitch"]
    wb_json = json.dumps(list(webcam_box)) if webcam_box else ""
    extra_json = json.dumps(profile_extra or {}, ensure_ascii=False)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO creators (
                creator_id, display_name, tier, primary_platform, platforms_json,
                language, webcam_style, webcam_box_json, style_notes, auto_profiled,
                profile_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(creator_id) DO UPDATE SET
                display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE creators.display_name END,
                tier = excluded.tier,
                primary_platform = CASE WHEN excluded.primary_platform != '' THEN excluded.primary_platform ELSE creators.primary_platform END,
                platforms_json = excluded.platforms_json,
                language = excluded.language,
                webcam_style = CASE WHEN excluded.webcam_style != '' THEN excluded.webcam_style ELSE creators.webcam_style END,
                webcam_box_json = CASE WHEN excluded.webcam_box_json != '' THEN excluded.webcam_box_json ELSE creators.webcam_box_json END,
                style_notes = CASE WHEN excluded.style_notes != '' THEN excluded.style_notes ELSE creators.style_notes END,
                auto_profiled = MAX(creators.auto_profiled, excluded.auto_profiled),
                profile_json = CASE WHEN excluded.profile_json != '{}' THEN excluded.profile_json ELSE creators.profile_json END,
                updated_at = excluded.updated_at
            """,
            (
                cid,
                display_name.strip() or cid,
                tier,
                primary_platform,
                json.dumps(plats),
                language,
                webcam_style,
                wb_json,
                style_notes,
                int(auto_profiled),
                extra_json,
                now,
                now,
            ),
        )
    row = get_creator(cid, db_path=db_path)
    assert row is not None
    return row


def _deserialize_creator_row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["platforms"] = json.loads(d.get("platforms_json") or '["twitch"]')
    except Exception:
        d["platforms"] = ["twitch"]
    wb_raw = str(d.get("webcam_box_json") or "").strip()
    try:
        wb_parsed = json.loads(wb_raw) if wb_raw else None
        d["webcam_box"] = list(wb_parsed) if isinstance(wb_parsed, list) and len(wb_parsed) == 4 else None
    except Exception:
        d["webcam_box"] = None
    try:
        d["profile_extra"] = json.loads(d.get("profile_json") or "{}")
    except Exception:
        d["profile_extra"] = {}
    d["auto_profiled"] = bool(d.get("auto_profiled"))
    return d


def get_creator(creator_id: str, db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any] | None:
    init_db(db_path)
    cid = creator_id.strip().lower()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM creators WHERE creator_id = ?", (cid,)).fetchone()
        return _deserialize_creator_row(row) if row else None


def list_creators(tier: str | None = None, db_path: Path = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    init_db(db_path)
    with _connect(db_path) as conn:
        if tier:
            rows = conn.execute(
                "SELECT * FROM creators WHERE tier = ? ORDER BY creator_id ASC",
                (tier,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM creators ORDER BY tier ASC, creator_id ASC").fetchall()
        return [_deserialize_creator_row(r) for r in rows]


def seed_creators_from_yaml(
    yaml_path: Path | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, int]:
    """Idempotently seed `creators` table from config/streamers.yaml as `radar_seed` without overwriting custom entries."""
    import yaml

    init_db(db_path)
    src_yaml = yaml_path or (PROJECT_ROOT / "config" / "streamers.yaml")
    if not src_yaml.is_file():
        return {"seeded": 0, "skipped": 0}
    data = yaml.safe_load(src_yaml.read_text(encoding="utf-8")) or {}
    streamers = dict(data.get("streamers") or {})
    seeded, skipped = 0, 0
    for sid, cfg in streamers.items():
        cid = str(sid).strip().lower()
        existing = get_creator(cid, db_path=db_path)
        if existing is not None:
            skipped += 1
            continue
        wb = cfg.get("webcam_box")
        wb_tuple = [int(x) for x in wb] if isinstance(wb, list) and len(wb) == 4 else None
        upsert_creator(
            creator_id=cid,
            display_name=str(cfg.get("display_name") or cid),
            tier="radar_seed",
            primary_platform=str(cfg.get("primary_platform") or "twitch"),
            platforms=[str(p) for p in (cfg.get("platforms") or ["twitch"])],
            language=str(cfg.get("language") or "en"),
            webcam_style="preset_box" if wb_tuple else "auto_detect",
            webcam_box=wb_tuple,
            style_notes=str(cfg.get("notes") or ""),
            auto_profiled=True,
            profile_extra={
                "kick_slug": cfg.get("kick_slug"),
                "twitch_login": cfg.get("twitch_login"),
                "youtube_channel_handle": cfg.get("youtube_channel_handle"),
                "source_yaml_tier": cfg.get("tier"),
            },
            db_path=db_path,
        )
        seeded += 1
    return {"seeded": seeded, "skipped": skipped}


def inspect_db_summary(db_path: Path = DEFAULT_DB_PATH) -> dict[str, Any]:
    init_db(db_path)
    with _connect(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for t in (
            "vods",
            "slices",
            "clips",
            "telemetry",
            "campaigns",
            "transcripts_fts",
            "knowledge_chunks",
            "scene_blueprints",
            "creators",
        ):
            if t in tables:
                counts[t] = int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return {"db_path": str(db_path), "journal_mode": journal_mode, "tables": tables, "counts": counts}
