"""Error taxonomy.

Every error carries an explicit retry decision. The pipeline never infers
retryability from a string match at the call site — it asks the error.

Classification follows the handoff's failure table:
  401 missing/expired ADC            → not retryable, fix credentials
  403 missing IAM / model access      → not retryable, fix permissions
  400 invalid payload/parameter       → not retryable, fix the request
  429 quota unavailable or exhausted  → not retryable automatically (§24.5: no key rotation)
  5xx                                 → retryable, but ONLY if the server proved no
                                        successful interaction was returned
  safety policy rejection             → not retryable, surfaced verbatim
  client timeout                      → NOT retryable. The request may have created a
                                        billable generation (handoff §"Response handling").
"""

from __future__ import annotations

from typing import Any


class OmniVlogError(Exception):
    """Base class. Carries a machine-readable code and a retry verdict."""

    code: str = "internal_error"
    retryable: bool = False
    #: True when the request left our process but we never learned its outcome.
    #: Recovery rule §5.1: query the interaction, never re-issue.
    outcome_unknown: bool = False
    remediation: str | None = None

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


# ── Configuration / environment ─────────────────────────────────────────────


class ConfigError(OmniVlogError):
    code = "config_error"


class MissingCredentialsError(ConfigError):
    """No usable ADC / API key. Must fail BEFORE any billable request."""

    code = "missing_credentials"
    remediation = (
        "Run one of these outside this program, then retry:\n"
        "  gcloud auth application-default login\n"
        "  gcloud auth application-default login "
        "--impersonate-service-account=<your-vertex-sa>@<project>.iam.gserviceaccount.com"
    )


class MissingProjectError(ConfigError):
    code = "missing_project"
    remediation = "Set GOOGLE_CLOUD_PROJECT (or pass --project)."


class MissingDependencyError(ConfigError):
    code = "missing_dependency"


class MediaToolMissingError(ConfigError):
    """ffmpeg/ffprobe is not on PATH."""

    code = "media_tool_missing"
    remediation = "Install ffmpeg (macOS: `brew install ffmpeg`)."


# ── Provider / transport ────────────────────────────────────────────────────


class ProviderError(OmniVlogError):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
        interaction_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.http_status = http_status
        self.provider_code = provider_code
        self.interaction_id = interaction_id


class AuthError(ProviderError):
    """401 — missing or expired credentials."""

    code = "auth_error"
    retryable = False
    remediation = "Refresh ADC: gcloud auth application-default login"


class PermissionError_(ProviderError):
    """403 — IAM role, model access, or GCS permission missing."""

    code = "permission_denied"
    retryable = False
    remediation = (
        "Grant roles/aiplatform.user on the project (plus bucket-scoped "
        "storage.objectCreator for GCS delivery). Do not grant Owner/Editor."
    )


class InvalidRequestError(ProviderError):
    """400 — payload, model id, MIME type, duration, or resolution rejected."""

    code = "invalid_request"
    retryable = False


class QuotaExhaustedError(ProviderError):
    """429. Preview models have fixed quota; this is usually not a code defect."""

    code = "quota_exhausted"
    retryable = False
    remediation = (
        "This is a fixed-quota Preview model. Report it and stop; do NOT rotate "
        "keys or projects to work around it."
    )


class ServerError(ProviderError):
    """5xx. Retryable only when the server proved no interaction was created."""

    code = "server_error"
    retryable = True


class SafetyBlockedError(ProviderError):
    """Provider safety policy rejected the prompt. Surfaced verbatim, never auto-rewritten."""

    code = "safety_blocked"
    retryable = False
    remediation = "Review the provider message and adjust the creative brief by hand."


class RequestTimeoutUnknownOutcome(ProviderError):
    """Client timeout. The generation may exist and may be billable.

    Deliberately NOT retryable. §5.1 and the handoff both forbid re-issuing a
    request whose outcome we never observed.
    """

    code = "timeout_unknown_outcome"
    retryable = False
    outcome_unknown = True
    remediation = (
        "Resolve by querying the interaction, or mark the job NEEDS_HUMAN. Never re-issue blindly."
    )


class InteractionPending(RequestTimeoutUnknownOutcome):
    """An acknowledged asynchronous render; resume queries its durable ID."""

    code = "interaction_pending"


class InteractionFailedError(ProviderError):
    """The provider accepted the request and then reported failure."""

    code = "interaction_failed"
    retryable = False


class ModelUnavailableError(ProviderError):
    code = "model_unavailable"
    retryable = False


# ── Policy / budget / state ─────────────────────────────────────────────────


class PolicyBlockedError(OmniVlogError):
    code = "policy_blocked"


class ReferenceRejectedError(PolicyBlockedError):
    """A reference image failed sanitization (§6.2 hard-reject conditions)."""

    code = "reference_rejected"


class BudgetExhaustedError(OmniVlogError):
    code = "budget_exhausted"


class StateTransitionError(OmniVlogError):
    code = "illegal_state_transition"


class HumanGateError(OmniVlogError):
    """Raised when an automated step wants to run past a human approval point."""

    code = "human_gate"


class CapabilityMissingError(OmniVlogError):
    """The selected surface cannot do what the plan requires (§8.3 strategy D)."""

    code = "capability_missing"


class JobNotFoundError(OmniVlogError):
    code = "job_not_found"


class ResumeConflictError(OmniVlogError):
    """A resume would need a different project than the job was pinned to."""

    code = "resume_conflict"


# ─────────────────────────────────────────────────────────────────────────────
# HTTP status → exception
# ─────────────────────────────────────────────────────────────────────────────

#: Provider error `code` strings that mean "safety refused".
_SAFETY_CODES = {
    "safety",
    "blocked",
    "prohibited_content",
    "policy_violation",
    "content_policy",
    "responsible_ai",
}


def classify_http_error(
    status: int,
    *,
    provider_code: str | None,
    message: str,
    interaction_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> ProviderError:
    """Map an HTTP failure onto the taxonomy above."""
    low = (provider_code or "").lower()
    msg_low = message.lower()

    if status == 401:
        return AuthError(message, http_status=status, provider_code=provider_code, detail=detail)
    if status == 403:
        return PermissionError_(
            message, http_status=status, provider_code=provider_code, detail=detail
        )
    if status == 429:
        return QuotaExhaustedError(
            message, http_status=status, provider_code=provider_code, detail=detail
        )
    if status == 400:
        if any(s in low for s in _SAFETY_CODES) or "safety" in msg_low:
            return SafetyBlockedError(
                message, http_status=status, provider_code=provider_code, detail=detail
            )
        return InvalidRequestError(
            message, http_status=status, provider_code=provider_code, detail=detail
        )
    if status == 404:
        return ModelUnavailableError(
            message, http_status=status, provider_code=provider_code, detail=detail
        )
    if 500 <= status < 600:
        return ServerError(
            message,
            http_status=status,
            provider_code=provider_code,
            interaction_id=interaction_id,
            detail=detail,
        )
    if any(s in low for s in _SAFETY_CODES):
        return SafetyBlockedError(
            message, http_status=status, provider_code=provider_code, detail=detail
        )
    return ProviderError(message, http_status=status, provider_code=provider_code, detail=detail)
