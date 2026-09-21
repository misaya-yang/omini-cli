"""Credential resolution.

Handoff rules, enforced here:
  * Vertex uses short-lived OAuth credentials from Application Default
    Credentials. `google.auth.default()` is called **once** and the resulting
    session refreshes tokens on demand — we never shell out to `gcloud` per
    request.
  * A service-account JSON is never copied into this repository, logged, or
    embedded in a prompt/fixture. `GOOGLE_APPLICATION_CREDENTIALS` is honoured
    only because `google.auth.default()` reads it; we never open the file.
  * Missing credentials must fail *before* any billable request, with a
    remediation message.
  * Tokens are never returned to callers, never stored on a record, and never
    printed. The `AuthorizedSession` owns them.
"""

from __future__ import annotations

import functools
from typing import Any

from omni_homevlog.errors import ConfigError, MissingCredentialsError
from omni_homevlog.observability.logging import get_logger

logger = get_logger("auth")

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


@functools.lru_cache(maxsize=1)
def get_adc_credentials() -> tuple[Any, str | None]:
    """Return `(credentials, detected_project)` from ADC.

    Cached: ADC resolution touches the filesystem and sometimes the metadata
    server. The credentials object self-refreshes, so caching it is correct.
    """
    try:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ConfigError(
            "google-auth is not installed. `pip install google-auth`.",
            detail={"import_error": str(exc)},
        ) from exc

    try:
        credentials, project = google.auth.default(scopes=_SCOPES)
    except DefaultCredentialsError as exc:
        raise MissingCredentialsError(
            f"Application Default Credentials are not available. ({type(exc).__name__})",
        ) from exc
    except Exception as exc:
        raise MissingCredentialsError(
            f"Could not obtain Application Default Credentials: {exc}"
        ) from exc

    principal = getattr(credentials, "service_account_email", None) or type(credentials).__name__
    logger.debug(
        "ADC resolved", extra={"extra_fields": {"project": project, "principal": principal}}
    )
    return credentials, project


def get_authorized_session() -> Any:
    """An `AuthorizedSession` that refreshes tokens on demand.

    Callers get a session, never a token. `requests` handles the header.
    """
    try:
        from google.auth.transport.requests import AuthorizedSession
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("google-auth is not installed.") from exc

    credentials, _ = get_adc_credentials()
    # AuthorizedSession ships without type information upstream.
    session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
    # Never let a redirect carry the Authorization header to another host.
    session.max_redirects = 0
    return session


def check_adc_available() -> tuple[bool, str, str | None]:
    """Non-raising probe used by `omni-vlog doctor` before any spend.

    Returns `(ok, human_message, principal)`. The message never contains a token.
    """
    try:
        credentials, project = get_adc_credentials()
    except MissingCredentialsError as exc:
        return False, f"{exc.message}\n{exc.remediation or ''}".strip(), None
    principal = getattr(credentials, "service_account_email", None) or type(credentials).__name__
    return True, f"ADC resolved (project={project}, principal={principal})", principal


def require_api_key(api_key: str | None) -> str:
    """Resolve a Gemini Developer API key, or fail before any spend."""
    if not api_key:
        raise MissingCredentialsError(
            "GEMINI_API_KEY is not set. The gemini_api surface requires an API key.",
            detail={"provider": "gemini_api"},
        )
    return api_key


def reset_auth_cache() -> None:
    """Test hook."""
    get_adc_credentials.cache_clear()
