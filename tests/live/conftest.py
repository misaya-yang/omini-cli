"""Live-test gating (§21.3).

These tests make **real, billable API calls**. They are opt-in at two levels:

  1. `RUN_LIVE_VIDEO_TESTS=1` must be set, and
  2. credentials must actually resolve.

Both are required. A single forgotten environment variable should not be able to
spend money, and neither should a CI runner that happens to have ADC configured
for an unrelated reason.
"""

from __future__ import annotations

import os

import pytest

from omni_homevlog.config import get_settings, reset_settings_cache


def live_tests_enabled() -> bool:
    return (os.environ.get("RUN_LIVE_VIDEO_TESTS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _skip_reason() -> str | None:
    if not live_tests_enabled():
        return (
            "live video tests are opt-in: set RUN_LIVE_VIDEO_TESTS=1 to enable them. "
            "They make real, billable API calls."
        )
    try:
        settings = get_settings()
    except Exception as exc:
        return f"settings could not be loaded: {exc}"

    if settings.omni_provider == "vertex":
        from omni_homevlog.providers.auth import check_adc_available

        ok, message, _ = check_adc_available()
        if not ok:
            return f"credentials are not available: {message}"
    elif not settings.gemini_api_key:
        return "GEMINI_API_KEY is not set for the gemini_api surface"

    return None


@pytest.fixture(scope="session", autouse=True)
def _gate_live_tests():
    reset_settings_cache()
    reason = _skip_reason()
    if reason:
        pytest.skip(reason, allow_module_level=True)
    yield


@pytest.fixture(scope="session")
def live_settings():
    return get_settings()


@pytest.fixture(scope="session")
def live_project(live_settings) -> str:
    return live_settings.resolve_project(None)
