"""Unified, strictly typed configuration engine (`pydantic-settings`, `extra='forbid'`, `v4.0-B1`).

Consolidates `.env`, `config/channel_profile.yaml` (zero dead keys), `config/profiles.yaml`
(`local_control` vs `gcp_cloud_run_l4`), and `config/niches/<niche_id>.yaml` (`NichePack`).
Guarantees Regla 01 secret safety: `ValidationError` formatting strips `input` values (`include_input=False`)
and `secret_presence_report()` only ever emits `'PRESENTE'` or `'FALTANTE'`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.niche_packs import DEFAULT_NICHE_ID, NICHES_DIR, NichePack, load_niche_pack

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANNEL_PROFILE_PATH = PROJECT_ROOT / "config" / "channel_profile.yaml"
DEFAULT_PROFILES_PATH = PROJECT_ROOT / "config" / "profiles.yaml"
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"

ComputeProfileId = Literal["local_control", "gcp_cloud_run_l4"]
MonetizationGoal = Literal["cpm_bounty", "ypp_bridge", "editorial"]
VoiceModeChoice = Literal["none", "human_batch", "tts"]


class StudioDefaultsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_niche: str = DEFAULT_NICHE_ID
    default_language: str = "en"
    monetization_goal: MonetizationGoal = "cpm_bounty"
    voice_mode: VoiceModeChoice = "none"


class ClipRulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_duration_sec: float = Field(default=16.0, gt=0.0, le=60.0)
    max_duration_sec: float = Field(default=58.0, gt=5.0, le=180.0)
    min_reviewer_score: float = Field(default=0.75, ge=0.0, le=1.0)
    max_review_retries: int = Field(default=1, ge=0, le=3)
    cut_silence_over_sec: float = Field(default=0.65, gt=0.1, le=10.0)
    preserve_synchresis_pause_sec: float = Field(default=0.25, ge=0.0, le=2.0)


class Layout916Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fps: int = Field(default=60, ge=24, le=120)
    preserve_source_fps: bool = True
    max_webcam_upscale: float = Field(default=1.8, ge=1.0, le=3.0)
    webcam_exclusion_pad_px: int = Field(default=48, ge=0, le=240)
    rms_punch_in_max_zoom: float = Field(default=1.15, ge=1.0, le=1.35)


class CaptionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_name: str = "Montserrat Black"
    font_size: int = Field(default=68, ge=24, le=120)
    uppercase: bool = True
    max_words_per_chunk: int = Field(default=3, ge=1, le=6)
    max_chars_per_chunk: int = Field(default=16, ge=8, le=32)
    primary_color: str = "&H00FFFFFF"
    highlight_color: str = "&H0000FFFF"
    impact_color: str = "&H0055FF00"
    outline_width: float = Field(default=6.0, ge=0.0, le=16.0)
    shadow: float = Field(default=3.0, ge=0.0, le=12.0)
    pos_x: int = Field(default=540, ge=0, le=1080)
    pos_y: int = Field(default=1120, ge=200, le=1600)
    skip_npc_dialogue: bool = True


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_lufs: float = Field(default=-14.0, ge=-24.0, le=-8.0)
    true_peak_dbtp: float = Field(default=-1.0, ge=-6.0, le=0.0)
    highpass_hz: int = Field(default=70, ge=20, le=250)
    sample_rate: int = Field(default=48000, ge=16000, le=96000)


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs_max_gb: float = Field(default=4.0, gt=0.2, le=500.0)
    auto_cleanup_temp: bool = True


class ChannelProfileConfig(BaseModel):
    """Typed schema for `config/channel_profile.yaml` (`extra='forbid'`, zero dead keys)."""

    model_config = ConfigDict(extra="forbid")

    studio: StudioDefaultsConfig = Field(default_factory=StudioDefaultsConfig)
    clip_rules: ClipRulesConfig = Field(default_factory=ClipRulesConfig)
    layout_9_16: Layout916Config = Field(default_factory=Layout916Config)
    captions: CaptionsConfig = Field(default_factory=CaptionsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


class ComputeProfileSpec(BaseModel):
    """Typed Cloud-First compute profile (`local_control` vs `gcp_cloud_run_l4`)."""

    model_config = ConfigDict(extra="forbid")

    profile_id: ComputeProfileId
    description: str
    plane: Literal["control_and_ingest", "gpu_worker"]
    device: Literal["cpu", "cuda"]
    dtype: Literal["int8", "float16", "bfloat16"]
    attn_implementation: Literal["sdpa"] = "sdpa"
    asr_primary: str
    asr_backend: str
    video_encoder: str
    video_encoder_fallback: str = "libx264"
    pixel_format: Literal["nv12", "yuv420p"] = "yuv420p"
    llm_primary: str = "vertex_ai"
    llm_fallback_ladder: list[str] = Field(default_factory=lambda: ["groq", "openrouter"])
    heavy_gpu_models_enabled: bool = False
    network_timeout_sec: float = Field(default=15.0, gt=1.0, le=20.0)
    max_job_timeout_minutes: int = Field(default=15, ge=5, le=60)


def _default_profile_aliases() -> dict[str, ComputeProfileId]:
    return {
        "local_hp": "local_control",
        "gcp_spot_l4": "gcp_cloud_run_l4",
        "colab_t4": "gcp_cloud_run_l4",
    }


class ProfilesFileConfig(BaseModel):
    """Typed schema for `config/profiles.yaml` (`extra='forbid'`)."""

    model_config = ConfigDict(extra="forbid")

    default_profile: ComputeProfileId = "local_control"
    profile_aliases: dict[str, ComputeProfileId] = Field(default_factory=_default_profile_aliases)
    profiles: dict[ComputeProfileId, ComputeProfileSpec]

    def resolve(self, raw_name: str | None) -> ComputeProfileSpec:
        clean = (raw_name or "").strip().lower()
        if not clean:
            return self.profiles[self.default_profile]
        if clean in self.profiles:
            return self.profiles[clean]  # type: ignore[index]
        mapped = self.profile_aliases.get(clean)
        if mapped and mapped in self.profiles:
            return self.profiles[mapped]
        raise ValueError(
            f"Unknown compute profile {raw_name!r}; expected one of {sorted(list(self.profiles) + list(self.profile_aliases))}"
        )


class EnvSettings(BaseSettings):
    """Typed `.env` + environment settings (`extra='forbid'`, zero secret echoing)."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
        populate_by_name=True,
    )

    compute_profile: str = "local_control"
    studio_mode: str = "streamer_gta6_lite"
    voice_mode: str = "none"
    default_niche: str = DEFAULT_NICHE_ID
    default_language: str = "en"

    use_vertex_ai: bool = True
    gcp_project_id: str = ""
    gcp_location: str = "us-central1"
    google_application_credentials: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_api_key: str = ""
    vertex_service_tier: str = "standard"
    llm_daily_budget_usd: float = Field(default=2.50, gt=0.0, le=100.0)

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-3.8-flash"

    gcp_spot_zone: str = "us-central1-a"
    gcp_gcs_bucket: str = ""
    gcp_batch_bucket: str = ""
    gcp_idle_shutdown_minutes: int = Field(default=12, ge=5, le=60)

    yt_cookies_path: str = ""
    youtube_api_key: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    kick_client_id: str = ""
    kick_client_secret: str = ""

    hf_token: str = ""
    hf_hub_enable_hf_transfer: str = "1"

    serper_api_key: str = ""
    firecrawl_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    qdrant_url: str = ""
    qdrant_host: str = ""
    qdrant_api_key: str = ""
    qdrant_collection_obsidian: str = ""
    qdrant_collection_clipping_v2: str = "clipping_studio_empirical"

    whop_api_key: str = ""
    onnx_threads: int = Field(default=0, ge=0, le=64)
    headless_browser_bin: str = ""
    chrome_bin: str = ""
    omni_rag_cache_dir: str = ""
    omni_rag_dir: str = ""
    log_level: str = "INFO"
    job_id: str = ""

    @property
    def resolved_qdrant_url(self) -> str:
        raw = (self.qdrant_url or self.qdrant_host or "").strip()
        if not raw:
            return ""
        if not raw.startswith(("http://", "https://")):
            return f"https://{raw}"
        return raw

    def secret_presence_report(self) -> dict[str, Literal["PRESENTE", "FALTANTE"]]:
        """Return Regla 01 compliant presence map (`PRESENTE` / `FALTANTE` only, zero values)."""
        pairs = {
            "GCP_PROJECT_ID": self.gcp_project_id,
            "GROQ_API_KEY": self.groq_api_key,
            "OPENROUTER_API_KEY": self.openrouter_api_key,
            "YOUTUBE_API_KEY": self.youtube_api_key,
            "SERPER_API_KEY": self.serper_api_key,
            "FIRECRAWL_API_KEY": self.firecrawl_api_key,
            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token,
            "TELEGRAM_CHAT_ID": self.telegram_chat_id,
            "QDRANT_URL": self.resolved_qdrant_url,
            "QDRANT_API_KEY": self.qdrant_api_key,
        }
        return {k: ("PRESENTE" if str(v).strip() else "FALTANTE") for k, v in pairs.items()}


class StudioSettings(BaseModel):
    """Complete runtime configuration bundle validated end-to-end with `extra='forbid'`."""

    model_config = ConfigDict(extra="forbid")

    env: EnvSettings
    channel_profile: ChannelProfileConfig
    compute_profiles: ProfilesFileConfig
    active_compute_profile: ComputeProfileSpec
    active_niche_pack: NichePack


_SETTINGS_CACHE: dict[tuple[str, str, str, str, str], StudioSettings] = {}


def clear_settings_cache() -> None:
    """Clear cached `StudioSettings` instances."""
    _SETTINGS_CACHE.clear()


def _safe_validation_error(prefix: str, exc: ValidationError) -> ValueError:
    """Sanitize Pydantic `ValidationError` with `include_input=False` so secret values NEVER leak."""
    sanitized = exc.errors(include_input=False, include_url=False)
    summary = "; ".join(f"{'.'.join(str(x) for x in err.get('loc', ()))}: {err.get('msg')} ({err.get('type')})" for err in sanitized)
    return ValueError(f"{prefix}: {summary}")


def load_channel_profile(path: Path = DEFAULT_CHANNEL_PROFILE_PATH) -> ChannelProfileConfig:
    if not path.is_file():
        return ChannelProfileConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Channel profile {path} must be a YAML mapping")
    try:
        return ChannelProfileConfig.model_validate(raw)
    except ValidationError as exc:
        raise _safe_validation_error(f"Invalid channel_profile ({path.name})", exc) from None


def load_compute_profiles(path: Path = DEFAULT_PROFILES_PATH) -> ProfilesFileConfig:
    if not path.is_file():
        default_local = ComputeProfileSpec(
            profile_id="local_control",
            description="Default local control plane",
            plane="control_and_ingest",
            device="cpu",
            dtype="int8",
            asr_primary="nvidia/parakeet-tdt-0.6b-v3",
            asr_backend="onnx-asr",
            video_encoder="h264_qsv",
            pixel_format="nv12",
        )
        default_l4 = ComputeProfileSpec(
            profile_id="gcp_cloud_run_l4",
            description="Default GCP Cloud Run Jobs NVIDIA L4 GPU worker",
            plane="gpu_worker",
            device="cuda",
            dtype="bfloat16",
            asr_primary="faster-whisper-large-v3-turbo",
            asr_backend="ctranslate2-cuda",
            video_encoder="h264_nvenc",
            pixel_format="yuv420p",
            heavy_gpu_models_enabled=True,
            network_timeout_sec=20.0,
        )
        return ProfilesFileConfig(
            profiles={"local_control": default_local, "gcp_cloud_run_l4": default_l4}
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Compute profiles {path} must be a YAML mapping")
    try:
        return ProfilesFileConfig.model_validate(raw)
    except ValidationError as exc:
        raise _safe_validation_error(f"Invalid profiles.yaml ({path.name})", exc) from None


def load_env_settings(dotenv_path: Path | None = DEFAULT_DOTENV_PATH) -> EnvSettings:
    eff_env = dotenv_path if (dotenv_path is not None and dotenv_path.is_file()) else None
    try:
        return EnvSettings(_env_file=eff_env)  # type: ignore[call-arg]
    except ValidationError as exc:
        raise _safe_validation_error("Invalid environment/.env configuration", exc) from None


def load_studio_settings(
    *,
    niche_id: str | None = None,
    compute_profile: str | None = None,
    dotenv_path: Path | None = DEFAULT_DOTENV_PATH,
    channel_profile_path: Path = DEFAULT_CHANNEL_PROFILE_PATH,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    niches_dir: Path = NICHES_DIR,
    use_cache: bool = True,
) -> StudioSettings:
    """Load and cross-validate `.env`, `channel_profile.yaml`, `profiles.yaml`, and active `NichePack`."""
    env_cfg = load_env_settings(dotenv_path=dotenv_path)
    chan_cfg = load_channel_profile(channel_profile_path)
    prof_cfg = load_compute_profiles(profiles_path)

    eff_prof_name = compute_profile or os.getenv("COMPUTE_PROFILE") or env_cfg.compute_profile or prof_cfg.default_profile
    active_prof = prof_cfg.resolve(eff_prof_name)

    eff_niche_id = (
        niche_id
        or os.getenv("DEFAULT_NICHE")
        or env_cfg.default_niche
        or chan_cfg.studio.default_niche
        or DEFAULT_NICHE_ID
    ).strip().lower()

    cache_key = (
        str(dotenv_path or ""),
        str(channel_profile_path),
        str(profiles_path),
        active_prof.profile_id,
        eff_niche_id,
    )
    if use_cache and cache_key in _SETTINGS_CACHE:
        return _SETTINGS_CACHE[cache_key]

    pack = load_niche_pack(eff_niche_id, niches_dir=niches_dir)
    bundle = StudioSettings(
        env=env_cfg,
        channel_profile=chan_cfg,
        compute_profiles=prof_cfg,
        active_compute_profile=active_prof,
        active_niche_pack=pack,
    )
    if use_cache:
        _SETTINGS_CACHE[cache_key] = bundle
    return bundle


def get_settings(niche_id: str | None = None, compute_profile: str | None = None) -> StudioSettings:
    """Return validated `StudioSettings` singleton for the requested `niche_id` / `compute_profile`."""
    return load_studio_settings(niche_id=niche_id, compute_profile=compute_profile, use_cache=True)
