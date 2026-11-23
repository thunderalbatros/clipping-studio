"""Frozen Pydantic v2 data contracts for Clipping Studio (Hito 2.0)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SceneType = Literal[
    "active_gameplay",
    "cutscene",
    "trailer_or_video_reaction",
    "menu_or_brb",
    "just_chatting",
]

LayoutMode = Literal["split_webcam_top", "full_gameplay_9_16"]


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
    contact_sheet_path: str | None = None
    duration_sec: float = Field(default=0.0, ge=0.0)


class FreezeFrameSpec(BaseModel):
    """Single freeze-frame callout overlay specification (normalized 0..1000 box)."""

    model_config = ConfigDict(extra="forbid")

    timestamp_sec: float = Field(ge=0.0)
    box_2d: tuple[int, int, int, int]
    callout_text: str = Field(max_length=32)
    duration_sec: float = Field(default=0.8, gt=0.0, le=2.5)

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
