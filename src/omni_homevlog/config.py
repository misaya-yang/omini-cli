"""Configuration.

Precedence, highest first:
  1. explicit CLI flags
  2. environment variables / `.env`
  3. `configs/default.yaml`
  4. defaults in this module

Nothing here reads a credential *value*. Credentials are resolved lazily by
`providers/auth.py`, which either refreshes ADC or returns an API key that stays
inside the transport.
"""

from __future__ import annotations

import functools
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from omni_homevlog.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "default.yaml"
DEFAULT_THRESHOLDS_PATH = REPO_ROOT / "configs" / "quality_thresholds.yaml"


class QualityThresholds(BaseSettings):
    """§11.5 thresholds.

    These are heuristics. `calibrated_at`/`calibration_note` exist so nobody
    mistakes them for an objective quality standard.
    """

    model_config = SettingsConfigDict(extra="forbid")

    accept_identity: float = 0.82
    accept_anatomy: float = 0.78
    accept_motion: float = 0.76
    accept_camera_realism: float = 0.76
    accept_spatial_continuity: float = 0.80
    accept_continuity_with_previous: float = 0.75
    accept_audio_continuity: float = 0.60
    accept_reference_fidelity: float = 0.70

    #: Below this, don't try a local edit — go straight to regeneration.
    regenerate_identity_below: float = 0.55
    regenerate_anatomy_below: float = 0.50
    regenerate_spatial_below: float = 0.50

    #: Scores in [threshold - margin, threshold) are "unstable" → human review.
    unstable_margin: float = 0.04

    min_safe_edit_identity: float = 0.75
    min_safe_edit_anatomy: float = 0.65

    calibrated_at: str = "2026-09-21"
    calibration_note: str = (
        "Initial hand-set values. NOT calibrated against scored samples. "
        "Treat as policy knobs, not as a measurement of quality."
    )

    def to_dict(self) -> dict[str, float | str]:
        return self.model_dump()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # `KEY=` in a .env means "unset", not "the empty string". Without this,
        # `cp .env.example .env` — the install step the README documents — makes
        # every command die with a raw pydantic ValidationError, because the
        # template's empty `OMNI_MAX_ESTIMATED_COST_USD=` cannot be parsed as a
        # Decimal. An empty value must fall through to the default.
        env_ignore_empty=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the config sources: init > environment > .env > YAML > defaults.

        This has to be done with a settings source rather than by passing the YAML
        values as keyword arguments to `Settings(...)`. Explicit init kwargs are the
        *highest* priority source in pydantic-settings, so seeding from YAML that way
        silently outranks the environment and `.env` — the opposite of the
        documented precedence, and a genuinely confusing failure: exports appear to
        do nothing.
        """
        sources: list[PydanticBaseSettingsSource] = [
            init_settings,
            env_settings,
            dotenv_settings,
        ]
        if DEFAULT_CONFIG_PATH.exists():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=DEFAULT_CONFIG_PATH))
        sources.append(file_secret_settings)
        return tuple(sources)

    # ── Provider selection ──────────────────────────────────────────────────
    omni_provider: Literal["vertex", "gemini_api"] = "vertex"

    # ── Vertex ──────────────────────────────────────────────────────────────
    google_cloud_project: str | None = None
    google_cloud_location: str = "global"
    omni_vertex_model: str = "gemini-omni-1.1-flash-preview"
    omni_output_gcs_uri: str | None = None
    omni_prefer_gcs_delivery: bool = True

    # ── Gemini Developer API ────────────────────────────────────────────────
    gemini_api_key: str | None = Field(default=None, repr=False)
    omni_gemini_model: str = "gemini-omni-1.1-flash"

    # ── Planning / review models (NOT the video model) ──────────────────────
    director_model: str = "gemini-3.8-flash"
    critic_model: str = "gemini-3.8-flash"
    director_location: str = "global"
    critic_location: str = "global"

    # ── Storage ────────────────────────────────────────────────────────────
    omni_data_dir: Path = Path("./.omni-vlog")
    omni_ffmpeg_bin: str = "ffmpeg"
    omni_ffprobe_bin: str = "ffprobe"

    # ── Cost control ────────────────────────────────────────────────────────
    omni_max_total_calls: int = 8
    omni_max_estimated_cost_usd: Decimal | None = None
    omni_pricing_file: str = "pricing.yaml"

    # ── Switches ────────────────────────────────────────────────────────────
    omni_llm_ledger: Path | None = None
    omni_max_llm_calls: int = 24
    run_live_video_tests: bool = False
    omni_keep_raw_responses: bool = True
    omni_log_level: str = "INFO"
    omni_request_timeout_s: float = 600.0
    omni_query_timeout_s: float = Field(default=60.0, gt=0)

    def resolve_project(self, override: str | None = None) -> str:
        project = override or self.google_cloud_project
        if project:
            return project
        # Fall back to ADC's detected project rather than guessing.
        try:  # pragma: no cover - depends on local gcloud state
            import google.auth

            _, detected = google.auth.default()
            if detected:
                return str(detected)
        except Exception:
            pass
        raise ConfigError(
            "No Google Cloud project configured. Set GOOGLE_CLOUD_PROJECT or pass --project."
        )

    def data_dir(self) -> Path:
        path = Path(self.omni_data_dir).expanduser()
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        return path


@functools.lru_cache(maxsize=1)
def load_yaml_config(path: str | None = None) -> dict[str, Any]:
    """Read `configs/default.yaml`. Missing file is fine — defaults win."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{target} must contain a YAML mapping at the top level")
    return loaded


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Resolved settings. Source precedence is defined by
    `Settings.settings_customise_sources`, not by anything here.
    """
    return Settings()


@functools.lru_cache(maxsize=4)
def load_thresholds(path: str | None = None) -> QualityThresholds:
    target = Path(path) if path else DEFAULT_THRESHOLDS_PATH
    if not target.exists():
        return QualityThresholds()
    with target.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{target} must contain a YAML mapping at the top level")
    return QualityThresholds(**loaded)


def reset_settings_cache() -> None:
    """Test hook."""
    get_settings.cache_clear()
    load_thresholds.cache_clear()
    load_yaml_config.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
