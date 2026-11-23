"""Frozen Pydantic v2 data contracts for Clipping Studio (Hito 2.0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Single Source of Truth for 9:16 (1080x1920) Universal Safe Box across DesignEngine, Callout, Renderer & MediaCheck.
# Guarantees zero overlap with right-side TikTok/Shorts/Reels UI buttons (x<=888 / 18% margin) and 4-network crops.
UNIVERSAL_SAFE_BOX_9_16: tuple[int, int, int, int] = (48, 420, 888, 1248)

SceneType = Literal[
    "active_gameplay",
    "cutscene",
    "trailer_or_video_reaction",
    "menu_or_brb",
    "just_chatting",
]

LayoutMode = Literal["split_webcam_top", "full_gameplay_9_16"]
AudioPolicy = Literal["strict_clean", "sound_campaign", "keep_original"]
CalloutMode = Literal["motion_overlay", "comedic_freeze"]
CalloutSfx = Literal["whoosh", "pop", "hit", "none"]
CreatorTier = Literal["radar_seed", "bounty_target", "client"]


class WordTimestamp(BaseModel):
    """Single transcribed word with exact start/end timestamps and optional LR-ASD score."""

    model_config = ConfigDict(extra="forbid")

    word: str
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    is_npc_or_other: bool = False
    streamer_asd_score: float | None = None


class NonVerbalEvent(BaseModel):
    """Acoustic or visual event detected across a slice."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["shouting", "laughter", "silence", "music_spike", "visual_motion"]
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    intensity: float = Field(default=0.0, ge=0.0)
    note: str = ""


class CandidateSlice(BaseModel):
    """Surgical VOD/clip slice extracted for transcription and curation (1080p native floor)."""

    model_config = ConfigDict(extra="forbid")

    slice_id: str
    source_url: str
    platform: Literal["youtube", "twitch", "kick", "local"]
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    duration_sec: float = Field(gt=0.0)
    signal_source: Literal[
        "youtube_heatmap",
        "twitch_chat_zscore",
        "kick_clips_rank",
        "kick_hls_window",
        "audio_rms",
        "manual_window",
    ]
    score: float = Field(ge=0.0)
    slice_video_path: str
    slice_audio_path: str | None = None
    source_width: int = Field(default=0, ge=0)
    source_height: int = Field(default=0, ge=0)
    source_fps: float = Field(default=0.0, ge=0.0)


class AnnotatedTranscript(BaseModel):
    """Word-level transcript enriched with non-verbal markers, YAMNet ONNX DMCA score, and contact-sheet."""

    model_config = ConfigDict(extra="forbid")

    slice_id: str
    language: str
    full_text: str
    words: list[WordTimestamp] = Field(default_factory=list)
    non_verbal_events: list[NonVerbalEvent] = Field(default_factory=list)
    music_prob_peak: float = Field(default=0.0, ge=0.0, le=1.0)
    dmca_flag: bool = False
    yamnet_top_events: list[dict[str, float | str]] = Field(default_factory=list)
    dmca_detector: Literal["yamnet_onnx", "fft_fallback"] = "yamnet_onnx"
    audio_policy: AudioPolicy = "strict_clean"
    dmca_advisory: str = ""
    needs_demucs_rescue: bool = False
    visual_climax_sec: float | None = None
    contact_sheet_path: str | None = None
    duration_sec: float = Field(default=0.0, ge=0.0)


class FreezeFrameSpec(BaseModel):
    """Editorial callout specification (v3.5: non-freezing motion_overlay by default; comedic_freeze opt-in only)."""

    model_config = ConfigDict(extra="forbid")

    timestamp_sec: float = Field(ge=0.0)
    box_2d: tuple[int, int, int, int] = (300, 200, 700, 800)
    callout_text: str = Field(max_length=32)
    duration_sec: float = Field(default=1.8, gt=0.0, le=3.5)
    mode: CalloutMode = "motion_overlay"
    sfx: CalloutSfx = "whoosh"
    accent_color: str = "#FFD400"
    eyebrow_text: str = ""

    @field_validator("callout_text")
    @classmethod
    def _validate_callout_words(cls, v: str) -> str:
        clean = " ".join(v.strip().split())
        if not clean:
            raise ValueError("callout_text must not be empty")
        if len(clean) > 32:
            raise ValueError(f"callout_text exceeds 32 chars ({len(clean)})")
        if len(clean.split()) > 4:
            raise ValueError(f"callout_text exceeds 4 words ({clean!r})")
        return clean


class SliceAuditResult(BaseModel):
    """Pass-1 multimodal scene, webcam bbox, lore match, and anti-DMCA audit result."""

    model_config = ConfigDict(extra="forbid")

    slice_id: str
    scene_type: SceneType
    visual_hook_score: float = Field(ge=0.0, le=1.0)
    webcam_box_2d: tuple[int, int, int, int] | None = None
    webcam_box_px: tuple[int, int, int, int] | None = None
    dmca_clear: bool = True
    dmca_risk_reason: str = ""
    audio_policy: AudioPolicy = "strict_clean"
    dmca_advisory: str = ""
    needs_demucs_rescue: bool = False
    approved_for_pass2: bool = False
    suggested_start_sec: float = Field(default=0.0, ge=0.0)
    suggested_end_sec: float = Field(default=30.0, gt=0.0)
    hook_headline: str = ""
    freeze_frame: FreezeFrameSpec | None = None
    matched_lore_entity: str | None = None
    self_contained_score: int = Field(default=0, ge=0, le=100)
    rationale: str = ""


class EditPlan(BaseModel):
    """Deterministic edit specification emitted by the Curator/Director."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    slice_id: str
    source_url: str
    start_trim: float = Field(ge=0.0)
    end_trim: float = Field(gt=0.0)
    start_word_idx: int | None = None
    end_word_idx: int | None = None
    post_word_tail_sec: float = Field(default=0.35, ge=0.0, le=12.0)
    scene_blueprint_key: str | None = None
    scene_type: SceneType = "active_gameplay"
    visual_hook_score: float = Field(default=0.8, ge=0.0, le=1.0)
    hook_headline: str
    cold_open_script: str | None = None
    voice_mode: Literal["none", "human_batch", "tts"] = "none"
    clip_lane: Literal["discovery", "trend_multi_pov"] = "discovery"
    matched_trend_id: str | None = None
    matched_lore_entity: str | None = None
    layout_mode: LayoutMode = "split_webcam_top"
    webcam_box_2d: tuple[int, int, int, int] | None = None
    webcam_box_px: tuple[int, int, int, int] | None = None
    gameplay_crop_px: tuple[int, int, int, int] | None = None
    webcam_upscale_factor: float = Field(default=1.0, ge=0.0)
    no_webcam_gameplay_overlap: bool = True
    freeze_frame: FreezeFrameSpec | None = None
    dmca_clear: bool = True
    dmca_risk_reason: str = ""
    audio_policy: AudioPolicy = "strict_clean"
    dmca_advisory: str = ""
    needs_demucs_rescue: bool = False
    sound_campaign_climax_ms: int | None = None
    self_contained_score: int = Field(ge=0, le=100)
    rationale: str = ""

    @field_validator("hook_headline")
    @classmethod
    def _validate_hook_headline(cls, v: str) -> str:
        clean = " ".join(v.strip().split())
        words = clean.split()
        if not words or len(words) > 12 or len(clean) > 90:
            raise ValueError("hook_headline must be 1..12 words and <= 90 chars")
        return clean


class ClipPackaging(BaseModel):
    """Algorithmic 5-network metadata bundle generated for approved master clips."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    shorts_title: str
    shorts_hashtags: list[str]
    tiktok_caption: str
    suggested_on_screen_hook: str
    reels_caption: str
    facebook_reels_caption: str = ""
    x_post_text: str = ""
    matched_lore_entity: str | None = None
    bounty_safe: bool = True
    audio_policy: AudioPolicy = "strict_clean"
    dmca_advisory: str = ""
    sound_campaign_climax_ms: int | None = None
    sound_sync_note: str = ""
    cover_timestamp_ms: int = 0
    cover_clean_path: str = ""
    cover_9_16_path: str = ""
    ab_hook_variants: list[str] = Field(default_factory=list)
    author_reply_seed: str = ""

    @field_validator("x_post_text")
    @classmethod
    def _validate_x_post(cls, v: str) -> str:
        clean = " ".join(v.strip().split())
        if len(clean) > 260:
            raise ValueError(f"x_post_text must be <= 260 chars (got {len(clean)})")
        return clean

    @field_validator("shorts_title")
    @classmethod
    def _validate_shorts_title(cls, v: str) -> str:
        clean = " ".join(v.strip().split())
        if not clean or len(clean) > 60:
            raise ValueError(f"shorts_title must be 1..60 chars (got {len(clean)})")
        return clean

    @field_validator("suggested_on_screen_hook")
    @classmethod
    def _validate_on_screen_hook(cls, v: str) -> str:
        clean = " ".join(v.strip().split())
        if not clean or len(clean.split()) > 6:
            raise ValueError(f"suggested_on_screen_hook must be 1..6 words (got {clean!r})")
        return clean

    @field_validator("shorts_hashtags")
    @classmethod
    def _validate_hashtags(cls, v: list[str]) -> list[str]:
        tags = [t.strip() if t.strip().startswith("#") else f"#{t.strip()}" for t in v if t.strip()]
        if len(tags) < 3 or len(tags) > 5:
            raise ValueError(f"shorts_hashtags must have 3..5 hashtags (got {len(tags)})")
        return tags


class BlindCriticResult(BaseModel):
    """5-axis + binary-gate quality verdict emitted by the Blind Critic."""

    model_config = ConfigDict(extra="forbid")

    clip_id: str
    first_sentence_quote: str
    last_sentence_quote: str
    checked_by_code_passed: bool
    gate_alone: Literal["pass", "fail", "unsure"]
    gate_ending: Literal["pass", "fail", "unsure"]
    hook_3s: float = Field(ge=0.0, le=1.0)
    pacing: float = Field(ge=0.0, le=1.0)
    visual_framing: float = Field(ge=0.0, le=1.0)
    audio_dmca_safety: float = Field(ge=0.0, le=1.0)
    packaging_fit: float = Field(ge=0.0, le=1.0)
    overall: float = Field(ge=0.0, le=1.0)
    approved: bool
    feedback: str


class RunState(BaseModel):
    """Resumable per-run checkpoint persisted to runs/<run_id>/state.json."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    source_url: str
    streamer_id: str
    audio_policy: AudioPolicy = "strict_clean"
    stage: Literal["initialized", "mined", "transcribed", "curated", "rendered", "reviewed", "failed"]
    candidates: list[CandidateSlice] = Field(default_factory=list)
    transcripts: list[AnnotatedTranscript] = Field(default_factory=list)
    slice_audits: list[SliceAuditResult] = Field(default_factory=list)
    edit_plans: list[EditPlan] = Field(default_factory=list)
    rendered_clips: list[str] = Field(default_factory=list)
    critic_results: list[BlindCriticResult] = Field(default_factory=list)
    packaging: list[ClipPackaging] = Field(default_factory=list)
    disk_usage_mb: float = Field(default=0.0, ge=0.0)
    updated_at: str = ""


class SceneReferenceItem(BaseModel):
    """Single viral short-form reference deconstructed by Scene-Match Intelligence (v3.5)."""

    model_config = ConfigDict(extra="forbid")

    platform: str
    source_id: str = ""
    channel: str = ""
    title: str
    url: str
    duration_sec: float = Field(default=0.0, ge=0.0)
    views: int = Field(default=0, ge=0)
    likes: int = Field(default=0, ge=0)
    comments: int = Field(default=0, ge=0)
    views_per_hour: float = Field(default=0.0, ge=0.0)
    outlier_ratio: float = Field(default=1.0, ge=0.0)
    cuts_count: int = Field(default=0, ge=0)
    cuts_per_min: float = Field(default=0.0, ge=0.0)
    avg_shot_len_sec: float = Field(default=0.0, ge=0.0)
    first_cut_sec: float = Field(default=0.0, ge=0.0)
    wpm: float = Field(default=0.0, ge=0.0)
    opening_3s_lufs: float = -24.0
    camera_position: str = "unknown"
    camera_area_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    intro_0_3s: str = ""
    outro_structure: str = ""
    visual_composition: str = ""
    transitions_and_effects: str = ""
    caption_style: str = ""
    comedy_tension_rhythm: str = ""
    title_cover_technique: str = ""


class SceneBlueprint(BaseModel):
    """Actionable editing blueprint distilled from exact scene viral outliers or archetype fallback."""

    model_config = ConfigDict(extra="forbid")

    scene_key: str
    game: str = "GTA VI"
    scene_query: str
    match_level: Literal["exact_scene_match", "archetype_fallback"] = "exact_scene_match"
    cache_hit: bool = False
    references: list[SceneReferenceItem] = Field(default_factory=list)
    target_duration_range_sec: tuple[float, float] = (20.0, 36.0)
    target_cuts_per_min: float = 15.0
    target_avg_shot_len_sec: float = 3.5
    recommended_first_cut_sec: float = 2.8
    camera_placement_guide: str = "split_webcam_top (prominent facecam 36% top, centered gameplay 64% bottom)"
    overlay_and_effects_guide: str = "animated motion_overlay Montserrat Black on live video (zero freeze) + 1.12x punch-in"
    caption_and_rhythm_guide: str = "discrete 2-3 word Montserrat Black captions, hard post-climax cut <= 0.4s"
    hook_0_3s_formula: str = "open mid-action/dialogue with zero preamble"
    closing_outro_formula: str = "cut immediately after climax reaction for seamless loop"
    summary_prompt: str = ""
    created_at: str = ""


class CreatorProfile(BaseModel):
    """Persistent creator profile stored in runs/studio.db `creators` table (v3.5)."""

    model_config = ConfigDict(extra="forbid")

    creator_id: str
    display_name: str
    tier: CreatorTier = "bounty_target"
    primary_platform: str = "twitch"
    platforms: list[str] = Field(default_factory=lambda: ["twitch", "kick", "youtube"])
    language: str = "en"
    webcam_style: str = "auto_detect"
    webcam_box: tuple[int, int, int, int] | None = None
    style_notes: str = ""
    auto_profiled: bool = False
    dynamic_profile: bool = False
