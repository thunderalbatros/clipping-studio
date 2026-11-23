"""Unit tests for Release v3.5: Alpha Motion-Graphics Overlay, PySceneDetect Cut-Guard, Word-Index Grounding, Scene-Match Intelligence, Creators Auto-Profiler & CI YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml

from src.callout import (
    OVERLAY_DEFAULT_Y,
    render_freeze_callout,
    render_motion_callout_alpha,
)
from src.curator import resolve_word_index_trim
from src.db import (
    get_creator,
    get_scene_blueprint,
    init_db,
    list_creators,
    seed_creators_from_yaml,
    upsert_scene_blueprint,
)
from src.miner import auto_profile_creator, validate_streamer_whitelist
from src.renderer import (
    _build_adaptive_filter,
    compute_adaptive_layout,
    find_punch_in_windows,
    guard_windows_against_scene_cuts,
    trim_post_climax_dead_tail,
)
from src.scene_researcher import (
    build_scene_queries,
    format_blueprint_for_prompt,
    make_scene_key,
    meets_viral_traction_floor,
    purge_contaminated_scene_blueprints,
    research_scene,
)
from src.schemas import (
    AnnotatedTranscript,
    EditPlan,
    FreezeFrameSpec,
    NonVerbalEvent,
    SceneBlueprint,
    SceneReferenceItem,
    WordTimestamp,
)


def test_motion_overlay_is_default_no_freeze_and_produces_alpha_mov(tmp_path: Path) -> None:
    spec = FreezeFrameSpec(timestamp_sec=11.0, callout_text="HE DIDN'T BACK UP")
    assert spec.mode == "motion_overlay"
    assert 1.5 <= spec.duration_sec <= 2.5

    plan = EditPlan(
        clip_id="clip_02",
        slice_id="slice_02",
        source_url="https://www.twitch.tv/videos/1",
        start_trim=7.2,
        end_trim=34.5,
        hook_headline="Bro forgot fire spreads",
        freeze_frame=spec,
        self_contained_score=95,
    )
    # render_freeze_callout MUST refuse to freeze when mode == "motion_overlay" (default)
    assert render_freeze_callout(tmp_path / "dummy.mp4", plan, tmp_path / "ff.png") is None

    # Render real transparent RGBA alpha .mov using deterministic Pillow fallback (works cleanly in headless CI)
    out_mov = tmp_path / "callout_alpha.mov"
    meta = render_motion_callout_alpha(spec, out_mov, fps=12, prefer_chrome=False)
    assert out_mov.is_file() and out_mov.stat().st_size > 10_000
    assert meta["mode"] == "motion_overlay"
    assert meta["has_alpha"] is True
    assert meta["overlay_y"] == OVERLAY_DEFAULT_Y
    assert meta["sfx_lead_frames"] == 2

    # Verify FFmpeg filter graph wires alpha motion_overlay, ambient webcam backdrop, voice clarity, and 30ms loop out-fade
    layout = compute_adaptive_layout(1920, 1080, (1354, 640, 566, 440), face_box_norm=(0.35, 0.08, 0.72, 0.52))
    assert layout["webcam_card_size"][0] > 650  # Expanded commanding card width vs legacy 620px cap
    flt = _build_adaptive_filter(
        layout=layout,
        has_callout=True,
        ff_start=11.0,
        ff_dur=1.8,
        loud_stats={"input_i": "-18.0", "input_tp": "-2.0", "input_lra": "7.0", "input_thresh": "-28.0", "target_offset": "0.0"},
        callout_idx=1,
        mask_idx=2,
        frame_idx=3,
        kept_segments=[(0.0, 26.0)],
        punch_windows=[(10.0, 11.4)],
        punch_zoom=1.12,
        bleep_spans=[],
        sfx_inputs=[(4, 10967, 0.16)],
        out_dur=26.0,
        callout_mode="motion_overlay",
        callout_y=OVERLAY_DEFAULT_Y,
    )
    assert "format=rgba,setpts=PTS-STARTPTS+11.000/TB[ov_mg]" in flt
    assert f"overlay=0:{OVERLAY_DEFAULT_Y}:enable='between(t,11.000,12.800)':eof_action=pass[ov]" in flt
    assert "boxblur=" in flt  # Ambient webcam backdrop, NOT flat black void
    assert "acompressor=" in flt
    assert "afade=t=out:st=25.970:d=0.030" in flt


def test_pyscenedetect_cut_guard_and_post_climax_dead_tail() -> None:
    # Rule (a): punch-in window [5.0, 7.0] crossed by hard cut at 5.8 -> trimmed to largest contiguous side [5.88, 7.0]
    guarded = guard_windows_against_scene_cuts([(5.0, 7.0)], scene_cuts=[5.8], min_dur=0.65, margin_sec=0.08)
    assert len(guarded) == 1
    ws, we = guarded[0]
    assert not (ws <= 5.8 <= we)
    assert ws >= 5.88 and we <= 7.0

    # Cut right in the middle of a short 1.0s window -> both halves < 0.65s -> dropped cleanly
    dropped = guard_windows_against_scene_cuts([(5.0, 6.0)], scene_cuts=[5.5], min_dur=0.65, margin_sec=0.08)
    assert dropped == []

    # Integrated inside find_punch_in_windows
    tr = AnnotatedTranscript(
        slice_id="s1",
        language="en",
        full_text="test",
        non_verbal_events=[NonVerbalEvent(kind="shouting", start=15.0, end=16.0, intensity=0.95)],
    )
    wins = find_punch_in_windows(tr, start_trim=10.0, out_dur=24.0, kept_segments=None, scene_cuts=[5.2])
    for s, e in wins:
        assert not (s <= 5.2 <= e)

    # Dead tail trim cuts trailing 3.0s of silence down to <= 0.40s while preserving loud endings
    sr = 16000
    pcm = np.zeros(22 * sr, dtype=np.float32)
    pcm[: 18 * sr] = 0.20  # Active audio up to 18.0s, then 4.0s dead silence
    tr_tail = AnnotatedTranscript(
        slice_id="s2",
        language="en",
        full_text="boom",
        words=[WordTimestamp(word="boom", start=17.0, end=17.5)],
    )
    trimmed = trim_post_climax_dead_tail([(0.0, 22.0)], pcm, tr_tail, start_trim=0.0, max_tail_sec=0.40)
    assert trimmed[-1][1] <= 18.45
    assert trimmed[-1][1] >= 17.9

    loud_pcm = np.full(22 * sr, 0.25, dtype=np.float32)
    untouched = trim_post_climax_dead_tail([(0.0, 22.0)], loud_pcm, tr_tail, start_trim=0.0, max_tail_sec=0.40)
    assert untouched == [(0.0, 22.0)]


def test_word_index_grounding_resolves_exact_ms_without_mid_syllable_cuts() -> None:
    words = [
        WordTimestamp(word="Um", start=0.50, end=0.80),
        WordTimestamp(word="Alright,", start=7.362, end=7.592),
        WordTimestamp(word="Donnie.", start=7.602, end=8.082),
        WordTimestamp(word="Valhalla!", start=24.710, end=25.350),
        WordTimestamp(word="Later", start=25.600, end=26.100),
    ]
    # Selecting weak filler idx=0 advances cleanly to idx=1 ("Alright,") and never cuts into idx=4 ("Later")
    s_trim, e_trim, s_idx, e_idx = resolve_word_index_trim(
        words,
        start_word_idx=0,
        end_word_idx=3,
        max_dur=40.0,
        pre_pad_sec=0.14,
        post_tail_sec=0.35,
    )
    assert s_idx == 1
    assert e_idx == 3
    assert abs(s_trim - 7.222) < 0.005
    assert e_trim <= 25.580  # Stopped cleanly before "Later" at 25.600s!
    assert e_trim >= 25.350

    # Visual post-speech climax tail (e.g. explosion + WASTED after "Valhalla.")
    s2, e2, _, _ = resolve_word_index_trim(
        words,
        start_word_idx=1,
        end_word_idx=3,
        max_dur=40.0,
        pre_pad_sec=0.14,
        post_tail_sec=8.5,
    )
    assert abs(s2 - 7.222) < 0.005
    assert abs(e2 - 33.85) < 0.01


def test_scene_researcher_level1_cache_and_level2_fallback(tmp_path: Path) -> None:
    db_file = tmp_path / "scene_test.db"
    init_db(db_file)

    queries = build_scene_queries("Destroy Donnie car gas can fire", game="GTA IV")
    assert any("gta iv" in q.lower() for q in queries)

    fake_yt_cands = [
        {
            "platform": "youtube_shorts",
            "source_id": "v1",
            "channel": "ClipKing",
            "title": "GTA VI Destroy Donnie car gas can explosion #shorts",
            "url": "https://www.youtube.com/shorts/v1",
            "duration_sec": 26.0,
            "views": 420000,
            "likes": 25000,
            "comments": 600,
            "views_per_hour": 14000.0,
        },
        {
            "platform": "youtube_shorts",
            "source_id": "v2",
            "channel": "ViceClips",
            "title": "He forgot fire spreads on Donnie car in GTA 6 #shorts",
            "url": "https://www.youtube.com/shorts/v2",
            "duration_sec": 31.0,
            "views": 180000,
            "likes": 11000,
            "comments": 240,
            "views_per_hour": 6000.0,
        },
    ]

    deconstruct_calls = 0

    def _fake_deconstruct(cand: dict[str, Any], *, outlier_ratio: float = 1.0, use_multimodal: bool = True) -> SceneReferenceItem:
        nonlocal deconstruct_calls
        deconstruct_calls += 1
        return SceneReferenceItem(
            platform=str(cand["platform"]),
            source_id=str(cand["source_id"]),
            channel=str(cand["channel"]),
            title=str(cand["title"]),
            url=str(cand["url"]),
            duration_sec=float(cand["duration_sec"]),
            views=int(cand["views"]),
            likes=int(cand["likes"]),
            comments=int(cand["comments"]),
            views_per_hour=float(cand["views_per_hour"]),
            outlier_ratio=outlier_ratio,
            cuts_count=6,
            cuts_per_min=13.5,
            avg_shot_len_sec=3.7,
            first_cut_sec=2.4,
            wpm=165.0,
            opening_3s_lufs=-14.8,
            camera_position="top_split",
            camera_area_ratio=0.34,
            intro_0_3s="Immediate gas can ignition boast",
            outro_structure="Hard cut right on WASTED",
            visual_composition="Top webcam over centered gameplay",
            transitions_and_effects="Animated text pop + 1.12x punch-in zoom",
            caption_style="Bold white+yellow uppercase captions",
            comedy_tension_rhythm="Escalating arrogance into instant explosion",
            title_cover_technique="Curiosity gap short title",
        )

    # Level 1: exact scene match
    bp1 = research_scene(
        "Destroy Donnie car gas can fire explosion",
        game="GTA VI",
        db_path=db_file,
        search_yt_fn=lambda _q, max_results=8: fake_yt_cands,
        search_kick_fn=lambda _q, game="GTA VI", limit=4: [],
        deconstruct_fn=_fake_deconstruct,
    )
    assert bp1.match_level == "exact_scene_match"
    assert bp1.cache_hit is False
    assert len(bp1.references) == 2
    assert deconstruct_calls == 2
    assert len(format_blueprint_for_prompt(bp1, max_tokens=300)) <= 1200

    # Verify SQLite persistence & instant cache hit on second call (zero extra deconstruct calls!)
    key = make_scene_key("GTA VI", "Destroy Donnie car gas can fire explosion")
    assert get_scene_blueprint(key, db_path=db_file) is not None
    bp_cached = research_scene(
        "Destroy Donnie car gas can fire explosion",
        game="GTA VI",
        db_path=db_file,
        search_yt_fn=lambda _q, max_results=8: [],
        search_kick_fn=lambda _q, game="GTA VI", limit=4: [],
        deconstruct_fn=_fake_deconstruct,
    )
    assert bp_cached.cache_hit is True
    assert deconstruct_calls == 2

    # Level 2: archetype fallback when <2 exact viral matches exist (unseen live scene)
    bp_fb = research_scene(
        "Ultra rare unseen live podcast improv bit 99999",
        game="Podcast",
        db_path=db_file,
        search_yt_fn=lambda _q, max_results=8: [],
        search_kick_fn=lambda _q, game="Podcast", limit=4: [],
        deconstruct_fn=_fake_deconstruct,
    )
    assert bp_fb.match_level == "archetype_fallback"
    assert bp_fb.references == []
    assert bp_fb.target_duration_range_sec[0] >= 16.0


def test_scene_researcher_viral_floor_fallback_and_cache_purge(tmp_path: Path) -> None:
    # Direct floor checks (YouTube Shorts: min_views >= 5000 OR min_vph >= 50; Kick/Twitch: min_views >= 1000)
    assert meets_viral_traction_floor("youtube_shorts", 185, 5.0) is False
    assert meets_viral_traction_floor("youtube_shorts", 3, 0.0) is False
    assert meets_viral_traction_floor("youtube_shorts", 5000, 12.0) is True
    assert meets_viral_traction_floor("youtube_shorts", 1200, 50.0) is True
    assert meets_viral_traction_floor("kick_clips", 999, 250.0) is False
    assert meets_viral_traction_floor("kick_clips", 1000, 10.0) is True
    assert meets_viral_traction_floor("twitch_clips", 1200, 5.0) is True
    assert meets_viral_traction_floor("youtube_shorts", 3000, 20.0, min_views_yt=2500) is True

    db_file = tmp_path / "floor_test.db"
    init_db(db_file)

    # Pool with only 1 truly viral YT Short + 2 amateur uploads (185v/5vph & 3v/0vph) + 1 small Kick clip (420v)
    mixed_yt = [
        {
            "platform": "youtube_shorts",
            "source_id": "amateur1",
            "channel": "SmallCh",
            "title": "He SET A CAR ON FIRE in GTA 6! Donnie gas can explosion #shorts",
            "url": "https://www.youtube.com/shorts/amateur1",
            "duration_sec": 28.0,
            "views": 185,
            "likes": 4,
            "comments": 0,
            "views_per_hour": 5.0,
        },
        {
            "platform": "youtube_shorts",
            "source_id": "amateur2",
            "channel": "TinyCh",
            "title": "GTA 6 Donnie Car Fire Explosion #shorts",
            "url": "https://www.youtube.com/shorts/amateur2",
            "duration_sec": 22.0,
            "views": 3,
            "likes": 0,
            "comments": 0,
            "views_per_hour": 0.0,
        },
        {
            "platform": "youtube_shorts",
            "source_id": "viral1",
            "channel": "BigClips",
            "title": "xQc GTA 6 Donnie car gas can explosion #shorts",
            "url": "https://www.youtube.com/shorts/viral1",
            "duration_sec": 29.0,
            "views": 66931,
            "likes": 3100,
            "comments": 85,
            "views_per_hour": 536.1,
        },
    ]
    small_kick = [
        {
            "platform": "kick_clips",
            "source_id": "k1",
            "channel": "kicksmall",
            "title": "Donnie car fire gas can explosion",
            "url": "https://kick.com/kicksmall/clips/k1",
            "duration_sec": 25.0,
            "views": 420,
            "likes": 12,
            "comments": 0,
            "views_per_hour": 30.0,
        }
    ]
    deconstruct_calls = 0

    def _counting_deconstruct(cand: dict[str, Any], *, outlier_ratio: float = 1.0, use_multimodal: bool = True) -> SceneReferenceItem:
        nonlocal deconstruct_calls
        deconstruct_calls += 1
        return SceneReferenceItem(
            platform=str(cand["platform"]),
            source_id=str(cand["source_id"]),
            channel=str(cand["channel"]),
            title=str(cand["title"]),
            url=str(cand["url"]),
            duration_sec=float(cand["duration_sec"]),
            views=int(cand["views"]),
            views_per_hour=float(cand["views_per_hour"]),
            outlier_ratio=outlier_ratio,
        )

    # Only 1 candidate survives the viral floor -> <2 valid refs -> automatic Level 2 archetype_fallback & 0 wasted deconstructs
    bp = research_scene(
        "Destroy Donnie car gas can fire explosion",
        game="GTA VI",
        db_path=db_file,
        search_yt_fn=lambda _q, max_results=8: mixed_yt,
        search_kick_fn=lambda _q, game="GTA VI", limit=4: small_kick,
        deconstruct_fn=_counting_deconstruct,
    )
    assert bp.match_level == "archetype_fallback"
    assert bp.references == []
    assert deconstruct_calls == 0

    # Seed a stale contaminated Level 1 blueprint and a clean Level 1 blueprint in scene_blueprints
    stale_key = make_scene_key("GTA VI", "Stale contaminated Donnie scene")
    stale_bp = SceneBlueprint(
        scene_key=stale_key,
        game="GTA VI",
        scene_query="Stale contaminated Donnie scene",
        match_level="exact_scene_match",
        references=[
            SceneReferenceItem(
                platform="youtube_shorts",
                source_id="am1",
                title="Amateur ref",
                url="https://www.youtube.com/shorts/am1",
                duration_sec=25.0,
                views=185,
                views_per_hour=5.0,
            ),
            SceneReferenceItem(
                platform="youtube_shorts",
                source_id="ok1",
                title="Viral ref",
                url="https://www.youtube.com/shorts/ok1",
                duration_sec=28.0,
                views=66931,
                views_per_hour=536.1,
            ),
        ],
    )
    upsert_scene_blueprint(
        scene_key=stale_key,
        game="GTA VI",
        scene_query="Stale contaminated Donnie scene",
        match_level="exact_scene_match",
        references_count=2,
        blueprint_json=stale_bp.model_dump_json(),
        summary_prompt="stale",
        db_path=db_file,
    )

    clean_key = make_scene_key("GTA VI", "Clean viral chase scene")
    clean_bp = SceneBlueprint(
        scene_key=clean_key,
        game="GTA VI",
        scene_query="Clean viral chase scene",
        match_level="exact_scene_match",
        references=[
            SceneReferenceItem(
                platform="youtube_shorts",
                source_id="v1",
                title="Viral 1",
                url="https://www.youtube.com/shorts/v1",
                duration_sec=25.0,
                views=120000,
                views_per_hour=900.0,
            ),
            SceneReferenceItem(
                platform="kick_clips",
                source_id="k2",
                title="Viral Kick",
                url="https://kick.com/x/clips/k2",
                duration_sec=24.0,
                views=2500,
                views_per_hour=80.0,
            ),
        ],
    )
    upsert_scene_blueprint(
        scene_key=clean_key,
        game="GTA VI",
        scene_query="Clean viral chase scene",
        match_level="exact_scene_match",
        references_count=2,
        blueprint_json=clean_bp.model_dump_json(),
        summary_prompt="clean",
        db_path=db_file,
    )

    # Explicit purge removes only the contaminated blueprint and preserves clean + fallback entries
    purged = purge_contaminated_scene_blueprints(db_path=db_file)
    assert purged == [stale_key]
    assert get_scene_blueprint(stale_key, db_path=db_file) is None
    assert get_scene_blueprint(clean_key, db_path=db_file) is not None

    # Re-seed stale entry and confirm research_scene auto-invalidates on read instead of returning cache_hit
    upsert_scene_blueprint(
        scene_key=stale_key,
        game="GTA VI",
        scene_query="Stale contaminated Donnie scene",
        match_level="exact_scene_match",
        references_count=2,
        blueprint_json=stale_bp.model_dump_json(),
        summary_prompt="stale",
        db_path=db_file,
    )
    refreshed = research_scene(
        "Stale contaminated Donnie scene",
        game="GTA VI",
        db_path=db_file,
        search_yt_fn=lambda _q, max_results=8: [],
        search_kick_fn=lambda _q, game="GTA VI", limit=4: [],
        deconstruct_fn=_counting_deconstruct,
    )
    assert refreshed.cache_hit is False
    assert refreshed.match_level == "archetype_fallback"


def test_creators_table_yaml_seed_auto_profiler_and_ci_yaml(tmp_path: Path) -> None:
    db_file = tmp_path / "creators.db"
    init_db(db_file)

    seed1 = seed_creators_from_yaml(db_path=db_file)
    assert seed1["seeded"] >= 5
    seed2 = seed_creators_from_yaml(db_path=db_file)
    assert seed2["seeded"] == 0  # Strictly idempotent

    jynxzi = get_creator("jynxzi", db_path=db_file)
    assert jynxzi is not None and jynxzi["tier"] == "radar_seed"

    # First-time Auto-Profiler for a new Whop bounty creator
    p1 = auto_profile_creator(
        "whop_new_creator",
        source_url="https://kick.com/whop_new_creator/clips/123",
        tier="bounty_target",
        db_path=db_file,
        persist=True,
    )
    assert p1["was_cached"] is False
    assert p1["auto_profiled"] is True
    assert p1["primary_platform"] == "kick"
    assert p1["tier"] == "bounty_target"

    # Second call hits persisted DB profile (runs strictly once!)
    p2 = auto_profile_creator("whop_new_creator", tier="bounty_target", db_path=db_file, persist=True)
    assert p2["was_cached"] is True

    # Client tier supported cleanly via validate_streamer_whitelist
    cli_prof = validate_streamer_whitelist(
        "agency_client_01",
        source_url="https://www.youtube.com/watch?v=abc",
        tier="client",
        db_path=db_file,
        persist_new=True,
    )
    assert cli_prof["tier"] == "client"
    assert len(list_creators(tier="client", db_path=db_file)) == 1

    # Validate .github/workflows/ci.yml parses as valid YAML with required CI gate steps
    ci_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
    assert ci_path.is_file()
    ci_data = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    assert isinstance(ci_data, dict)
    assert "jobs" in ci_data
    steps = ci_data["jobs"]["static-and-unit-gate"]["steps"]
    joined = "\n".join(str(s.get("run") or "") for s in steps)
    assert "ruff check" in joined
    assert "ty check" in joined
    assert "pytest" in joined
    assert "gitleaks" in joined
