"""Configuration precedence.

An earlier revision passed `configs/default.yaml` into `Settings(...)` as keyword
arguments. In pydantic-settings, explicit init kwargs are the *highest* priority
source, so the YAML silently outranked both the environment and `.env` — the
exact opposite of the documented precedence. The symptom was that exporting
`OMNI_DATA_DIR` did nothing, which is a genuinely confusing failure to debug.

These tests pin the intended order: init > environment > .env > YAML > defaults.
"""

from __future__ import annotations

import pytest

from omni_homevlog.config import (
    DEFAULT_CONFIG_PATH,
    QualityThresholds,
    Settings,
    get_settings,
    load_thresholds,
    reset_settings_cache,
)


def test_yaml_config_file_exists() -> None:
    assert DEFAULT_CONFIG_PATH.is_file(), (
        "configs/default.yaml is missing; the YAML source would silently contribute "
        "nothing and the test below would pass for the wrong reason"
    )


def test_environment_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The bug this file exists for."""
    monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "from-env"))
    reset_settings_cache()
    try:
        settings = Settings()
        assert settings.data_dir() == (tmp_path / "from-env").resolve()
    finally:
        reset_settings_cache()


def test_yaml_supplies_values_the_environment_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The override must not disable the YAML layer entirely."""
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.delenv("DIRECTOR_MODEL", raising=False)
    reset_settings_cache()
    try:
        settings = Settings()
        # Both come from configs/default.yaml.
        assert settings.google_cloud_location == "global"
        assert settings.director_model == "gemini-3.8-flash"
    finally:
        reset_settings_cache()


def test_init_kwargs_beat_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit arguments are the highest priority, which is what makes the
    documented precedence work rather than a special case for YAML."""
    monkeypatch.setenv("OMNI_LOG_LEVEL", "ERROR")
    reset_settings_cache()
    try:
        settings = Settings(omni_log_level="DEBUG")
        assert settings.omni_log_level == "DEBUG"
    finally:
        reset_settings_cache()


def test_code_defaults_apply_when_nothing_else_sets_a_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNI_MAX_TOTAL_CALLS", raising=False)
    reset_settings_cache()
    try:
        settings = Settings()
        # default.yaml does not set this one, so the field default wins.
        assert settings.omni_provider in ("vertex", "gemini_api")
        assert settings.omni_request_timeout_s == 600.0
    finally:
        reset_settings_cache()


def test_get_settings_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_LOG_LEVEL", "WARNING")
    reset_settings_cache()
    try:
        assert get_settings() is get_settings()
        assert get_settings().omni_log_level == "WARNING"
    finally:
        reset_settings_cache()


def test_reset_settings_cache_picks_up_a_new_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_settings_cache()
    try:
        monkeypatch.setenv("OMNI_LOG_LEVEL", "WARNING")
        first = get_settings().omni_log_level
        reset_settings_cache()
        monkeypatch.setenv("OMNI_LOG_LEVEL", "CRITICAL")
        second = get_settings().omni_log_level
    finally:
        reset_settings_cache()
    assert (first, second) == ("WARNING", "CRITICAL")


def test_dotenv_file_is_read_when_present(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`.env` sits below the environment but above YAML."""
    env_file = tmp_path / ".env"
    env_file.write_text("OMNI_LOG_LEVEL=ERROR\n", encoding="utf-8")
    monkeypatch.delenv("OMNI_LOG_LEVEL", raising=False)
    reset_settings_cache()
    try:
        settings = Settings(_env_file=env_file)
        assert settings.omni_log_level == "ERROR"
    finally:
        reset_settings_cache()


def test_real_environment_beats_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OMNI_LOG_LEVEL=ERROR\n", encoding="utf-8")
    monkeypatch.setenv("OMNI_LOG_LEVEL", "DEBUG")
    reset_settings_cache()
    try:
        settings = Settings(_env_file=env_file)
        assert settings.omni_log_level == "DEBUG"
    finally:
        reset_settings_cache()


# ── thresholds ─────────────────────────────────────────────────────────────


def test_thresholds_load_from_yaml() -> None:
    thresholds = load_thresholds()
    assert thresholds.accept_identity == 0.82
    assert thresholds.calibrated_at


def test_thresholds_carry_their_own_calibration_caveat() -> None:
    """§11.5: these must never be presented as an objective standard."""
    thresholds = load_thresholds()
    note = thresholds.calibration_note.lower()
    assert "not calibrated" in note or "not be calibrated" in note
    assert thresholds.to_dict()["calibrated_at"] == thresholds.calibrated_at


def test_thresholds_fall_back_to_defaults_when_the_file_is_absent(tmp_path) -> None:
    thresholds = load_thresholds(str(tmp_path / "nope.yaml"))
    assert isinstance(thresholds, QualityThresholds)
    assert thresholds.accept_identity > 0


def test_project_resolution_prefers_the_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env-project")
    reset_settings_cache()
    try:
        settings = Settings()
        assert settings.resolve_project("explicit-project") == "explicit-project"
        assert settings.resolve_project() == "from-env-project"
    finally:
        reset_settings_cache()


def test_gcs_settings_are_absent_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No bucket configured means inline delivery, which is the safe default."""
    monkeypatch.delenv("OMNI_OUTPUT_GCS_URI", raising=False)
    reset_settings_cache()
    try:
        settings = Settings()
        assert settings.omni_output_gcs_uri is None
    finally:
        reset_settings_cache()


def test_data_dir_is_absolute_and_expanded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNI_DATA_DIR", "~/omni-vlog-test")
    reset_settings_cache()
    try:
        resolved = Settings().data_dir()
    finally:
        reset_settings_cache()
    assert resolved.is_absolute()
    assert "~" not in str(resolved)
