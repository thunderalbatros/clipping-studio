"""Declarative Multi-Niche & Multi-Language `NichePack` contract and YAML loader (`v4.0-B1`).

Decouples Clipping Studio from single-game assumptions so gaming (`gaming_gta6`), podcasts,
stand-up comedy, talks/presentations, AI/tech news, original animation, and transformative
cinema/series commentary share one deterministic control plane with strict Pydantic validation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas import AudioPolicy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NICHES_DIR = PROJECT_ROOT / "config" / "niches"
DEFAULT_NICHE_ID = "gaming_gta6"

TargetPlatform = Literal[
    "youtube_shorts",
    "tiktok",
    "instagram_reels",
    "facebook_reels",
    "x",
    "youtube_long",
]
FramingMode = Literal[
    "split_gaming_cam",
    "dynamic_speaker_track",
    "presenter_and_slides",
    "cinematic_transformative",
]
SilenceTrimPolicy = Literal["aggressive", "moderate", "preserve_pauses"]
AsrEnginePreference = Literal["parakeet_v3", "faster_whisper_l4"]
CopyrightGuardMode = Literal["standard_dmca", "strict_transformative_commentary"]
LoreSourceKind = Literal["mediawiki_api", "curated_json", "none"]

_NICHE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
_LANG_CODE_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")


class AudioPipelineSpec(BaseModel):
    """Per-niche audio separation, speech enhancement, and audience-reaction preservation rules."""

    model_config = ConfigDict(extra="forbid")

    use_bs_roformer: bool = False
    use_deepfilternet: bool = False
    preserve_crowd_laughter: bool = False
    default_audio_policy: AudioPolicy = "strict_clean"


class VisualPresetsSpec(BaseModel):
    """Per-niche visual packaging, caption pacing, motion-graphics, and QA directives."""

    model_config = ConfigDict(extra="forbid")

    badge_label: str = Field(min_length=1, max_length=28)
    accent_color: str = Field(default="#FFD400", pattern=r"^#[0-9A-Fa-f]{6}$")
    caption_style: Literal["impact_karaoke", "editorial_clean", "subtitle_minimal"] = "impact_karaoke"
    motion_graphics_engine: Literal[
        "editorial_callout",
        "hyperframes_diagrams",
        "minimal_lower_third",
        "none",
    ] = "editorial_callout"
    broll_or_diagram_mode: Literal[
        "none",
        "diagrams_and_broll",
        "slides_pip",
        "commentary_pip",
    ] = "none"
    allowed_scene_types: list[str] = Field(default_factory=lambda: ["active_gameplay"])
    default_hashtags: list[str] = Field(default_factory=lambda: ["#Shorts"])
    qa_special_rules: list[str] = Field(default_factory=list)


class LoreSourceSpec(BaseModel):
    """Optional live context/lore synchronization source for a niche (e.g. MediaWiki API)."""

    model_config = ConfigDict(extra="forbid")

    kind: LoreSourceKind = "mediawiki_api"
    endpoint_url: str = ""
    default_subject: str = ""
    index_file: str = ""
    seed_pages: list[str] = Field(default_factory=list)


class NichePack(BaseModel):
    """Strict declarative `NichePack` specification (`extra='forbid'`, `min_qa_overall >= 0.78`)."""

    model_config = ConfigDict(extra="forbid")

    niche_id: str
    display_name: str
    description: str = ""
    production_ready: bool = True
    languages: list[str] = Field(min_length=1)
    target_platforms: list[TargetPlatform] = Field(min_length=1)
    framing_mode: FramingMode
    silence_trim_policy: SilenceTrimPolicy
    audio_pipeline: AudioPipelineSpec
    asr_engine_preference: AsrEnginePreference
    diarization_required: bool
    copyright_guard: CopyrightGuardMode
    visual_presets: VisualPresetsSpec
    min_qa_overall: float = Field(default=0.78, ge=0.78, le=1.0)
    rag_domains: list[str] = Field(min_length=1)
    lore_source: LoreSourceSpec | None = None

    @field_validator("niche_id")
    @classmethod
    def _validate_niche_id(cls, v: str) -> str:
        clean = v.strip().lower()
        if not _NICHE_ID_RE.fullmatch(clean):
            raise ValueError(f"Invalid niche_id {v!r}: must match ^[a-z][a-z0-9_]{{1,47}}$")
        return clean

    @field_validator("languages")
    @classmethod
    def _validate_languages(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for lang in v:
            code = lang.strip().lower()
            if not _LANG_CODE_RE.fullmatch(code):
                raise ValueError(f"Invalid ISO language code {lang!r}")
            if code not in cleaned:
                cleaned.append(code)
        if not cleaned:
            raise ValueError("languages must contain at least one language code")
        return cleaned


_PACK_CACHE: dict[tuple[str, str], NichePack] = {}


def clear_niche_pack_cache() -> None:
    """Clear in-memory `NichePack` cache (used in tests and config reloads)."""
    _PACK_CACHE.clear()


def list_niche_pack_ids(niches_dir: Path = NICHES_DIR) -> list[str]:
    """Return sorted `niche_id` stems discovered under `config/niches/*.yaml`."""
    if not niches_dir.is_dir():
        return []
    return sorted(p.stem.lower() for p in niches_dir.glob("*.yaml") if p.is_file())


def load_niche_pack(niche_id: str = DEFAULT_NICHE_ID, *, niches_dir: Path = NICHES_DIR) -> NichePack:
    """Load and validate `config/niches/<niche_id>.yaml` strictly against `NichePack`."""
    clean_id = (niche_id or DEFAULT_NICHE_ID).strip().lower()
    if not _NICHE_ID_RE.fullmatch(clean_id):
        raise ValueError(f"Invalid niche_id {niche_id!r}")
    cache_key = (str(niches_dir.resolve()), clean_id)
    if cache_key in _PACK_CACHE:
        return _PACK_CACHE[cache_key]

    yaml_path = (niches_dir / f"{clean_id}.yaml").resolve()
    if not yaml_path.is_relative_to(niches_dir.resolve()) or not yaml_path.is_file():
        available = list_niche_pack_ids(niches_dir)
        raise FileNotFoundError(f"NichePack {clean_id!r} not found in {niches_dir} (available={available})")

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"NichePack YAML {yaml_path} must deserialize to a mapping")
    pack = NichePack.model_validate(raw)
    if pack.niche_id != clean_id:
        raise ValueError(f"NichePack filename {yaml_path.name} mismatches niche_id={pack.niche_id!r}")
    _PACK_CACHE[cache_key] = pack
    return pack


def load_all_niche_packs(*, niches_dir: Path = NICHES_DIR) -> dict[str, NichePack]:
    """Load and validate every `config/niches/*.yaml` file."""
    return {nid: load_niche_pack(nid, niches_dir=niches_dir) for nid in list_niche_pack_ids(niches_dir)}


def silence_policy_cut_threshold_sec(pack: NichePack, default_sec: float = 0.65) -> float:
    """Translate `NichePack.silence_trim_policy` into deterministic `auto-editor` / jump-cut silence threshold."""
    if pack.silence_trim_policy == "aggressive":
        return 0.40
    if pack.silence_trim_policy == "preserve_pauses":
        return 2.40
    return default_sec
