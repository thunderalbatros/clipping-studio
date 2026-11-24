"""v4.0-B1 Universal Multi-Niche, Qdrant Cloud SSOT, Multi-Hop Agentic RAG, Visual QA >= 0.78 & Observability Tests."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError
from pydantic_ai import Agent
from qdrant_client import QdrantClient

from evals.visual_qa import (
    HARD_FLOOR_VLM_ANTI_SLOP,
    HARD_FLOOR_VLM_LEGIBILITY,
    HARD_FLOOR_VLM_OVERALL,
    VisualQAInputs,
    VisualQAVlmVerdict,
    evaluate_visual_qa,
)
from src.context_sync import clean_wiki_extract, sync_niche_context
from src.logging_utils import correlation_scope, get_logger
from src.niche_packs import (
    NichePack,
    clear_niche_pack_cache,
    load_all_niche_packs,
    silence_policy_cut_threshold_sec,
)
from src.qdrant_knowledge import (
    QDRANT_COLLECTION_CLIPPING_V2,
    REQUIRED_PAYLOAD_INDEX_FIELDS,
    GovernedChunkRecord,
    _assert_writable_collection,
    collect_governed_empirical_chunks,
    compute_content_hash,
    query_empirical_qdrant,
    sync_empirical_cloud,
)
from src.rag_engine import (
    attach_rag_agent_tool,
    rag_query_agentic,
    rebuild_rag_index,
    rewrite_rag_query,
)
from src.runtime_paths import PROJECT_ROOT, resolve_binary
from src.settings import (
    ChannelProfileConfig,
    clear_settings_cache,
    load_channel_profile,
    load_compute_profiles,
    load_env_settings,
    load_studio_settings,
)


def _hash_vec(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    low = text.lower()
    for tok in low.split():
        h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=4).digest(), "little")
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _embed_768_det(texts: list[str]) -> list[list[float]]:
    return [_hash_vec(t, 768) for t in texts]


def _embed_384_det(texts: list[str]) -> list[list[float]]:
    return [_hash_vec(t, 384) for t in texts]


def test_seven_niche_packs_and_pydantic_settings_extra_forbid(tmp_path: Path) -> None:
    clear_niche_pack_cache()
    clear_settings_cache()

    packs = load_all_niche_packs()
    expected_niches = {
        "ai_news",
        "cinema_series",
        "gaming_gta6",
        "original_animation",
        "podcast_interview",
        "standup_comedy",
        "talks_presentations",
    }
    assert set(packs.keys()) == expected_niches

    # Verify specific niche mandates
    gta = packs["gaming_gta6"]
    assert gta.production_ready is True
    assert gta.framing_mode == "split_gaming_cam"
    assert gta.min_qa_overall >= 0.78

    standup = packs["standup_comedy"]
    assert standup.silence_trim_policy == "preserve_pauses"
    assert standup.audio_pipeline.preserve_crowd_laughter is True
    assert standup.audio_pipeline.use_deepfilternet is False
    assert silence_policy_cut_threshold_sec(standup) > 2.0

    podcast = packs["podcast_interview"]
    assert podcast.diarization_required is True
    assert podcast.audio_pipeline.use_deepfilternet is True

    ai_news = packs["ai_news"]
    assert ai_news.silence_trim_policy == "aggressive"
    assert ai_news.visual_presets.motion_graphics_engine == "hyperframes_diagrams"
    assert silence_policy_cut_threshold_sec(ai_news) == pytest.approx(0.40)

    cinema = packs["cinema_series"]
    assert cinema.audio_pipeline.use_bs_roformer is True
    assert cinema.copyright_guard == "strict_transformative_commentary"
    assert cinema.min_qa_overall >= 0.80

    # Verify extra="forbid" rejects unknown fields AND min_qa_overall < 0.78
    bad_pack_dict = gta.model_dump()
    bad_pack_dict["unknown_dead_key"] = True
    with pytest.raises(ValidationError):
        NichePack.model_validate(bad_pack_dict)

    low_qa_dict = gta.model_dump()
    low_qa_dict["min_qa_overall"] = 0.72
    with pytest.raises(ValidationError):
        NichePack.model_validate(low_qa_dict)

    # Verify ChannelProfileConfig & ProfilesFileConfig load cleanly and enforce extra="forbid"
    chan = load_channel_profile()
    assert chan.studio.default_niche == "gaming_gta6"
    profs = load_compute_profiles()
    assert set(profs.profiles.keys()) == {"local_control", "gcp_cloud_run_l4"}
    assert profs.resolve("local_hp").profile_id == "local_control"
    assert profs.resolve("gcp_spot_l4").profile_id == "gcp_cloud_run_l4"

    with pytest.raises(ValidationError):
        ChannelProfileConfig.model_validate({"dead_legacy_section": {"foo": 1}})

    # Verify _safe_validation_error NEVER leaks secret values when .env has an unknown key
    bad_env = tmp_path / "bad.env"
    bad_env.write_text("UNEXPECTED_SECRET_TOKEN=super_secret_value_99999\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_env_settings(dotenv_path=bad_env)
    err_txt = str(exc_info.value)
    assert "unexpected_secret_token" in err_txt.lower()
    assert "super_secret_value_99999" not in err_txt

    settings = load_studio_settings(niche_id="standup_comedy", compute_profile="gcp_cloud_run_l4", use_cache=False)
    assert settings.active_niche_pack.niche_id == "standup_comedy"
    assert settings.active_compute_profile.profile_id == "gcp_cloud_run_l4"


def test_qdrant_ssot_idempotency_20_creators_and_niche_language_isolation() -> None:
    # 1. Hard Regla 01 write guard blocks external read-only collections
    with pytest.raises(PermissionError):
        _assert_writable_collection("centinela_knowledge")
    with pytest.raises(PermissionError):
        _assert_writable_collection("hunthub_omni_rag_v5")
    _assert_writable_collection(QDRANT_COLLECTION_CLIPPING_V2)

    # 2. Governed chunks check: 20/20 creators + EN/ES official docs + primary talks
    records = collect_governed_empirical_chunks(PROJECT_ROOT)
    creator_recs = [r for r in records if r.source_type == "creator_profile"]
    assert len(creator_recs) == 20
    assert {r.language for r in records if r.niche == "global"} >= {"en", "es"}
    assert {r.source_type for r in records} >= {"official_doc", "primary_talk", "creator_profile", "lore"}

    # 3. In-memory Qdrant named-vector schema + 2x idempotent sync (zero duplicates, zero re-embeds on run 2)
    mem_client = QdrantClient(":memory:")
    embed_calls = 0

    def _counting_embed(texts: list[str]) -> list[list[float]]:
        nonlocal embed_calls
        embed_calls += len(texts)
        return _embed_768_det(texts)

    sync_1 = sync_empirical_cloud(
        project_root=PROJECT_ROOT,
        client=mem_client,
        embed_768_fn=_counting_embed,
    )
    total_pts = sync_1["breakdown"]["total_points"]
    assert total_pts == len(records)
    assert sync_1["upserted"] == total_pts
    assert sync_1["unchanged"] == 0
    assert set(sync_1["payload_indexes"]) == set(REQUIRED_PAYLOAD_INDEX_FIELDS)
    calls_after_first = embed_calls

    # Second consecutive sync MUST NOT re-embed or duplicate any points!
    sync_2 = sync_empirical_cloud(
        project_root=PROJECT_ROOT,
        client=mem_client,
        embed_768_fn=_counting_embed,
    )
    assert sync_2["upserted"] == 0
    assert sync_2["unchanged"] == total_pts
    assert sync_2["breakdown"]["total_points"] == total_pts
    assert embed_calls == calls_after_first

    # 4. Strict niche & language pre-filtering in Qdrant
    hits_podcast_en = query_empirical_qdrant(
        "ShareViaCopyLinkWeight ColdStartMaxPostAgeSecs Lucia Jason Duval",
        niche="podcast_interview",
        language="en",
        top_k=8,
        client=mem_client,
        embed_768_fn=_embed_768_det,
    )
    assert len(hits_podcast_en) > 0
    # Must ONLY return podcast_interview or global, NEVER gaming_gta6!
    assert all(h["niche"] in {"podcast_interview", "global"} for h in hits_podcast_en)
    assert all(h["language"] == "en" for h in hits_podcast_en)

    hits_es = query_empirical_qdrant(
        "Whop Content Rewards burn rate Argentina LATAM",
        niche="standup_comedy",
        language="es",
        top_k=5,
        client=mem_client,
        embed_768_fn=_embed_768_det,
    )
    assert len(hits_es) > 0
    assert all(h["language"] == "es" for h in hits_es)
    assert all(h["niche"] in {"standup_comedy", "global"} for h in hits_es)


def test_agentic_rag_multihop_query_rewrite_and_pydantic_ai_tool(tmp_path: Path) -> None:
    db_file = tmp_path / "local_rag.db"
    local_chunks = [
        {
            "chunk_id": "gta6_streamers:local_only:1",
            "source_path": "context/gta6_lore_index.json",
            "domain": "gta6_streamers",
            "heading": "GTA VI Only Secret",
            "content": "Lucia Caminos and Jason Duval secret vault in Port Gellhorn.",
        }
    ]
    rebuild_rag_index(db_path=db_file, chunks_override=local_chunks, embed_fn=_embed_384_det)

    mem_client = QdrantClient(":memory:")
    gov_sample = [
        GovernedChunkRecord(
            doc_id="yt_oct_2026",
            chunk_id="gov:yt_oct_2026:01",
            niche="global",
            language="en",
            domain="bounties_dmca",
            platform="youtube",
            source_type="official_doc",
            source_url="https://support.google.com/youtube/blog/470890423",
            effective_date="2026-10-01",
            content_hash=compute_content_hash(
                niche="global",
                language="en",
                domain="bounties_dmca",
                platform="youtube",
                source_type="official_doc",
                effective_date="2026-10-01",
                heading="Prioritizing Original Content on Shorts",
                content="YouTube Shorts Prioritizing Original Content Inauthentic Content Reused Content reach reduction -40% to -80%.",
            ),
            heading="Prioritizing Original Content on Shorts",
            content="YouTube Shorts Prioritizing Original Content Inauthentic Content Reused Content reach reduction -40% to -80%.",
        )
    ]
    sync_empirical_cloud(
        client=mem_client,
        embed_768_fn=_embed_768_det,
        records_override=gov_sample,
    )

    # Reranker gives <0.45 (-1.5 logit -> ~0.18) on vague slang query, and >0.90 (+2.5 logit) after Hop-2 rewrite!
    def _mock_rerank(q: str, docs: list[str]) -> list[float]:
        score = 2.6 if "Prioritizing Original Content" in q else -1.5
        return [score for _ in docs]

    res = rag_query_agentic(
        "hey bro why anti-clipper shadowban?",
        niche="podcast_interview",
        language="en",
        domain=None,
        allow_federated=False,
        max_hops=2,
        db_path=db_file,
        qdrant_client=mem_client,
        embed_fn=_embed_384_det,
        embed_768_fn=_embed_768_det,
        rerank_fn=_mock_rerank,
    )
    assert res["hops_executed"] == 2
    assert res["rewritten"] is True
    assert len(res["queries_tried"]) == 2
    assert "Prioritizing Original Content" in res["final_query"]
    assert res["top_rerank_score"] >= 0.90
    assert len(res["formatted_context"]) <= 550 * 4 + 16
    # Confirm local GTA6 chunk did NOT leak into podcast_interview!
    assert all("gta6_streamers" not in str(h.get("domain")) for h in res["hits"])

    # Verify Spanish deterministic query rewrite
    es_rw = rewrite_rag_query("che como era el castigo de resubida?", niche="standup_comedy", language="es")
    assert "Vistas con Interacción" in es_rw

    # Verify @agent.tool registration on pydantic_ai.Agent
    ag: Agent[None, str] = Agent("test", defer_model_check=True)
    attach_rag_agent_tool(ag, default_niche="standup_comedy", default_language="es", db_path=db_file)
    assert "rag_query_agentic" in ag._function_toolset.tools


def test_visual_qa_078_floor_pass_technical_only_and_niche_gates(tmp_path: Path) -> None:
    assert HARD_FLOOR_VLM_OVERALL == pytest.approx(0.78)
    assert HARD_FLOOR_VLM_LEGIBILITY == pytest.approx(0.78)
    assert HARD_FLOOR_VLM_ANTI_SLOP == pytest.approx(0.75)

    mp4 = tmp_path / "clip_qa_master_9_16.mp4"
    cover_jpg = tmp_path / "clip_qa_cover_9_16.jpg"
    Image.new("RGB", (1080, 1920), (20, 24, 32)).save(cover_jpg, format="JPEG")
    subprocess.run(
        [
            str(resolve_binary("ffmpeg") or "ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920:rate=60:duration=2.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.2",
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "cfr",
            "-r",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            str(mp4),
        ],
        check=True,
        timeout=60,
    )

    # 1. Marginal VLM score 0.74 (previously passed >=0.72!) MUST NOT reach READY_HITL -> PASS_TECHNICAL_ONLY
    def _judge_marginal(_inp: VisualQAInputs, _imgs: list[Path], _rag: str) -> tuple[VisualQAVlmVerdict, str]:
        return (
            VisualQAVlmVerdict(
                no_hud_or_right_button_collision=True,
                no_cover_overlay_duplication=True,
                no_halos_or_floating_artifacts=True,
                face_unobstructed_on_cover=True,
                niche_rules_compliant=True,
                mobile_legibility_score=0.76,
                anti_ai_slop_score=0.76,
                overall_visual_score=0.74,
                recommended_parametric_fix="none",
                issues=["slightly cluttered lower frame"],
                rationale="Borderline 0.74 score below 0.78 floor.",
            ),
            "mock_vlm",
        )

    rep_marginal = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_qa",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            shorts_title="Clean Title",
            cover_headline="WAIT FOR IT",
            overlay_callout_text="WRONG TURN",
        ),
        vlm_judge_fn=_judge_marginal,
    )
    assert rep_marginal["technical_passed"] is True
    assert rep_marginal["ready_hitl"] is False
    assert rep_marginal["passed"] is False
    assert rep_marginal["status"] == "PASS_TECHNICAL_ONLY"

    # 2. --no-vlm (`run_vlm=False`) -> PASS_TECHNICAL_ONLY (never READY_HITL)
    rep_novlm = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_qa",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            run_vlm=False,
        )
    )
    assert rep_novlm["technical_passed"] is True
    assert rep_novlm["ready_hitl"] is False
    assert rep_novlm["status"] == "PASS_TECHNICAL_ONLY"

    # 3. NichePack enforcement: `cinema_series` without transformative commentary -> NEEDS_VISUAL_FIX
    def _judge_strong(_inp: VisualQAInputs, _imgs: list[Path], _rag: str) -> tuple[VisualQAVlmVerdict, str]:
        return (
            VisualQAVlmVerdict(
                no_hud_or_right_button_collision=True,
                no_cover_overlay_duplication=True,
                no_halos_or_floating_artifacts=True,
                face_unobstructed_on_cover=True,
                niche_rules_compliant=True,
                mobile_legibility_score=0.88,
                anti_ai_slop_score=0.86,
                overall_visual_score=0.85,
                recommended_parametric_fix="none",
                issues=[],
                rationale="Strong 0.85 visual quality.",
            ),
            "mock_vlm",
        )

    rep_cinema_fail = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_qa",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            niche_id="cinema_series",
            has_transformative_commentary=False,
        ),
        vlm_judge_fn=_judge_strong,
    )
    assert rep_cinema_fail["status"] == "NEEDS_VISUAL_FIX"
    assert rep_cinema_fail["ready_hitl"] is False

    # 4. `standup_comedy` with verified pauses and >= 0.78 VLM -> READY_HITL
    rep_standup_ok = evaluate_visual_qa(
        VisualQAInputs(
            clip_id="clip_qa",
            video_path=mp4,
            cover_path=cover_jpg,
            qa_dir=tmp_path / "qa",
            niche_id="standup_comedy",
            pause_integrity_verified=True,
        ),
        vlm_judge_fn=_judge_strong,
    )
    assert rep_standup_ok["status"] == "READY_HITL"
    assert rep_standup_ok["ready_hitl"] is True
    assert rep_standup_ok["passed"] is True


def test_structlog_redaction_and_context_sync_offline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("test_secops")
    with correlation_scope(run_id="run_99", clip_id="clip_01", niche_id="gaming_gta6", job_id="job_l4_01"):
        log.info("pipeline_step", api_key="SHOULD_NEVER_APPEAR_12345", items_processed=4)

    captured = capsys.readouterr()
    assert "SHOULD_NEVER_APPEAR_12345" not in captured.err
    line = [ln for ln in captured.err.splitlines() if "pipeline_step" in ln][-1]
    parsed = json.loads(line)
    assert parsed["severity"] == "INFO"
    assert parsed["message"] == "pipeline_step"
    assert parsed["run_id"] == "run_99"
    assert parsed["clip_id"] == "clip_01"
    assert parsed["niche_id"] == "gaming_gta6"
    assert parsed["job_id"] == "job_l4_01"
    assert parsed["api_key"] == "[REDACTED]"

    # Context sync offline test (mocked MediaWiki JSON)
    cleaned = clean_wiki_extract("<p>Leonida is a state in <b>Grand Theft Auto VI</b>[1][edit].</p>\nSee also\nLinks")
    assert "<b>" not in cleaned
    assert "[1]" not in cleaned
    assert "Leonida is a state" in cleaned

    ws_tmp = tmp_path / "ws"
    (ws_tmp / "config" / "niches").mkdir(parents=True)
    for yf in (PROJECT_ROOT / "config" / "niches").glob("*.yaml"):
        (ws_tmp / "config" / "niches" / yf.name).write_text(yf.read_text(encoding="utf-8"), encoding="utf-8")
    (ws_tmp / "context").mkdir(parents=True)
    (ws_tmp / "context" / "gta6_lore_index.json").write_text(
        json.dumps({"game": "Grand Theft Auto VI", "release_date": "2026-11-19"}),
        encoding="utf-8",
    )

    def _fake_mw_get(url: str, _timeout: float) -> dict[str, Any]:
        if "list=search" in url:
            return {"query": {"search": [{"title": "BuckMe"}]}}
        return {
            "query": {
                "pages": {
                    "101": {
                        "pageid": 101,
                        "title": "BuckMe",
                        "extract": "BuckMe is the in-game social video platform in Leonida featured across Grand Theft Auto VI street challenges.",
                    }
                }
            }
        }

    rep_gta = sync_niche_context(
        "gaming_gta6",
        project_root=ws_tmp,
        sync_cloud=False,
        http_get_json_fn=_fake_mw_get,
    )
    assert rep_gta["fetched_wiki_pages"] >= 1
    assert rep_gta["total_wiki_extracts"] == 1
    saved_lore = json.loads((ws_tmp / "context" / "gta6_lore_index.json").read_text(encoding="utf-8"))
    assert "endings_and_social_systems" in saved_lore
    assert "dmca_risk_radio_stations" in saved_lore
    assert saved_lore["live_wiki_extracts"][0]["title"] == "BuckMe"
    assert "<untrusted_source_data" in saved_lore["live_wiki_extracts"][0]["wrapped_untrusted"]

    rep_podcast = sync_niche_context("podcast_interview", project_root=ws_tmp, sync_cloud=False)
    assert rep_podcast["status"] == "no_external_lore_source"
