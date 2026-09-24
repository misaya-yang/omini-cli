"""HTTP transport for the Interactions API.

Deliberately hand-rolled on `requests` + `google-auth` rather than the
`google-genai` SDK, because the verified-working path on Vertex is plain REST
with ADC (handoff §"Minimal Python authentication and request"), and because the
surface is Preview — a thin transport we control is easier to adapt than an SDK
release cadence.

Retry discipline (§24.11, handoff §"Response handling"):

| outcome                     | action                                        |
|-----------------------------|-----------------------------------------------|
| 5xx                         | stop; outcome unknown, never repeat POST      |
| 429                         | stop; report. Never rotate keys (§24.5)       |
| 401 / 403 / 400             | stop; classify                                |
| safety refusal              | stop; surface verbatim, never auto-rewrite    |
| client timeout              | **stop, do not retry** — the generation may    |
|                             | already exist and may be billable             |

That last row is the important one. `RequestTimeoutUnknownOutcome` carries
`outcome_unknown = True`, and the pipeline turns that into `NEEDS_HUMAN` rather
than a second paid request.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from omni_homevlog.errors import (
    ConfigError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
    classify_http_error,
)
from omni_homevlog.observability.logging import get_logger, write_debug_fixture
from omni_homevlog.providers.response_parser import (
    InteractionEnvelope,
    parse_error_body,
    parse_interaction,
)

logger = get_logger("transport")

VERTEX_API_ROOT = "https://aiplatform.googleapis.com/v1beta1"
GEMINI_API_ROOT = "https://generativelanguage.googleapis.com/v1beta"
INTERACTIONS_PATH = "interactions"

MAX_SERVER_ERROR_RETRIES = 0


@dataclass(slots=True)
class TransportResult:
    """A parsed interaction plus the timing facts we need for the ledger."""

    envelope: InteractionEnvelope
    http_status: int
    started_at: float
    finished_at: float
    raw_text: str

    @property
    def latency_s(self) -> float:
        return self.finished_at - self.started_at


def vertex_interactions_url(project: str, location: str = "global") -> str:
    return f"{VERTEX_API_ROOT}/projects/{project}/locations/{location}/{INTERACTIONS_PATH}"


def vertex_interaction_url(project: str, location: str, interaction_id: str) -> str:
    base = vertex_interactions_url(project, location)
    return f"{base}/{quote(interaction_id, safe='')}"


def gemini_interactions_url() -> str:
    return f"{GEMINI_API_ROOT}/{INTERACTIONS_PATH}"


class BaseTransport:
    """Shared request/parse/classify logic. Subclasses supply auth + URL."""

    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        query_timeout_s: float = 60.0,
        keep_raw: bool = True,
        debug_dir: str | None = None,
        max_server_retries: int = MAX_SERVER_ERROR_RETRIES,
    ) -> None:
        self.timeout_s = timeout_s
        self.query_timeout_s = query_timeout_s
        self.keep_raw = keep_raw
        self.debug_dir = debug_dir
        self.max_server_retries = max_server_retries
        self._fixture_counter = 0

    # ── to be provided by subclasses ───────────────────────────────────────

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        raise NotImplementedError

    def _get(self, url: str, headers: dict[str, str]) -> Any:
        raise NotImplementedError

    def _auth_headers(self) -> dict[str, str]:
        raise NotImplementedError

    def create_url(self) -> str:
        raise NotImplementedError

    def get_url(self, interaction_id: str) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError

    # ── public API ─────────────────────────────────────────────────────────

    def create_interaction(self, payload: dict[str, Any]) -> TransportResult:
        """Exactly one paid POST. A 5xx does not prove generation never started."""
        started = time.monotonic()
        try:
            response = self._post(self.create_url(), payload, self._auth_headers())
        except RequestTimeoutUnknownOutcome:
            raise
        except ProviderError as exc:
            if exc.http_status is None or exc.http_status >= 500:
                raise RequestTimeoutUnknownOutcome(
                    "Video request outcome is unknown; query before retrying.",
                    interaction_id=exc.interaction_id,
                ) from exc
            raise
        finished = time.monotonic()
        raw_text = _body_text(response)
        self._dump_fixture("create_response", payload, raw_text, finished)
        status = int(getattr(response, "status_code", 200))
        if status >= 500:
            raise RequestTimeoutUnknownOutcome(
                f"Video POST returned {status}; generation may already exist. Not retried.",
                detail={"http_status": status},
            )
        if status >= 400:
            raise self._classify_response_error(response, raw_text)
        try:
            envelope = parse_interaction(raw_text)
        except ProviderError as exc:
            if exc.http_status and exc.http_status >= 500:
                raise RequestTimeoutUnknownOutcome(
                    "Video stream failed with a server error; not retried.",
                    interaction_id=exc.interaction_id,
                ) from exc
            raise
        return TransportResult(
            envelope=envelope,
            http_status=status,
            started_at=started,
            finished_at=finished,
            raw_text=raw_text,
        )

    def get_interaction(self, interaction_id: str) -> TransportResult:
        """Fetch an interaction's current state. Read-only and free.

        §5.1: this is the ONLY permitted way to resolve a dispatched request
        whose outcome we never observed. It is never billed.
        """
        url = self.get_url(interaction_id)
        started = time.monotonic()
        try:
            response = self._get(url, self._auth_headers())
        except RequestTimeoutUnknownOutcome:
            raise
        except ProviderError:
            raise
        finished = time.monotonic()

        raw_text = _body_text(response)
        self._dump_fixture("get_response", {"interaction_id": interaction_id}, raw_text, finished)

        if getattr(response, "status_code", 200) >= 400:
            raise self._classify_response_error(response, raw_text)

        return TransportResult(
            envelope=parse_interaction(raw_text, expected_id=interaction_id),
            http_status=int(getattr(response, "status_code", 200)),
            started_at=started,
            finished_at=finished,
            raw_text=raw_text,
        )

    # ── internals ──────────────────────────────────────────────────────────

    def _classify_response_error(self, response: Any, raw_text: str) -> ProviderError:
        status = int(getattr(response, "status_code", 0))
        message, provider_code = parse_error_body(raw_text, http_status=status)
        return classify_http_error(
            status,
            provider_code=provider_code,
            message=message,
            detail={"provider": self.describe(), "http_status": status},
        )

    def _dump_fixture(
        self, kind: str, request_payload: dict[str, Any], raw_text: str, at: float
    ) -> None:
        """Preserve the raw exchange for debugging, redacted (§26)."""
        if not (self.keep_raw and self.debug_dir):
            return
        self._fixture_counter += 1
        name = f"{self._fixture_counter:04d}_{kind}.json"
        import os

        write_debug_fixture(
            os.path.join(self.debug_dir, name),
            {
                "provider": self.describe(),
                "kind": kind,
                "request": request_payload,
                "response_text": raw_text,
                "recorded_at": at,
            },
        )


def _body_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return str(response)


# ─────────────────────────────────────────────────────────────────────────────
# Vertex (ADC)
# ─────────────────────────────────────────────────────────────────────────────


class VertexRestTransport(BaseTransport):
    """Verified path: ADC session against `aiplatform.googleapis.com/v1beta1`."""

    def __init__(
        self,
        *,
        project: str,
        location: str = "global",
        session: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not project:
            raise ConfigError("VertexRestTransport requires a project id.")
        self.project = project
        self.location = location
        self._session = session

    @property
    def session(self) -> Any:
        if self._session is None:
            from omni_homevlog.providers.auth import get_authorized_session

            self._session = get_authorized_session()
        return self._session

    def describe(self) -> str:
        return f"vertex:{self.project}:{self.location}"

    def create_url(self) -> str:
        return vertex_interactions_url(self.project, self.location)

    def get_url(self, interaction_id: str) -> str:
        # Streaming GET returns completed video bytes in step.delta events.
        return vertex_interaction_url(self.project, self.location, interaction_id) + "?stream=true"

    def get_interaction(self, interaction_id: str) -> TransportResult:
        """Read the authoritative status first, then stream large video output.

        A failed interaction can remain `in_progress` in the streaming replay
        even after unary GET reports its terminal safety error. Conversely,
        unary GET has returned 5xx for completed inline videos that streaming
        GET recovers. Both calls are read-only and retain the original ID.
        """
        started = time.monotonic()
        try:
            response = self._get(
                vertex_interaction_url(self.project, self.location, interaction_id),
                self._auth_headers(),
            )
            finished = time.monotonic()
            raw_text = _body_text(response)
            status = int(getattr(response, "status_code", 200))
            self._dump_fixture(
                "get_unary_response", {"interaction_id": interaction_id}, raw_text, finished
            )
            if 400 <= status < 500:
                raise self._classify_response_error(response, raw_text)
            if status < 400:
                envelope = parse_interaction(raw_text, expected_id=interaction_id)
                if envelope.status != "completed" or envelope.videos:
                    return TransportResult(envelope, status, started, finished, raw_text)
        except RequestTimeoutUnknownOutcome:
            logger.debug("Unary interaction query timed out; trying stream", exc_info=True)
        except ProviderError as exc:
            if exc.http_status is not None and 400 <= exc.http_status < 500:
                raise
            logger.debug("Unary interaction query unavailable; trying stream", exc_info=True)
        return super().get_interaction(interaction_id)

    def _auth_headers(self) -> dict[str, str]:
        # The Authorization header is injected by AuthorizedSession. We never
        # touch the token, and `max_redirects = 0` keeps it from leaking.
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
        }

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        return self._request("POST", url, headers, json_body=payload)

    def _get(self, url: str, headers: dict[str, str]) -> Any:
        return self._request("GET", url, headers)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        import requests

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=self.query_timeout_s if method == "GET" else self.timeout_s,
            )
        except requests.exceptions.Timeout as exc:
            raise RequestTimeoutUnknownOutcome(
                f"{method} {url} timed out after {self.timeout_s}s. The request may "
                "have created a billable generation.",
                detail={"provider": self.describe(), "method": method},
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"Transport failure talking to {self.describe()}: {exc}",
                detail={"provider": self.describe(), "method": method},
            ) from exc
        return response


# ────────────────────────────────────────────────────────────────────────────
# Gemini Developer API (API key)
# ─────────────────────────────────────────────────────────────────────────────


class GeminiApiRestTransport(BaseTransport):
    """Gemini Developer API surface.

    Unverified against the live endpoint — the capability probe is what decides
    whether this surface works, and nothing downstream may assume it does (§2.3).

    The key goes in the `x-goog-api-key` header, never a query parameter: query
    strings end up in proxy logs and stack traces.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        session: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ConfigError("GeminiApiRestTransport requires an API key.")
        self._api_key = api_key
        self._session: Any | None = session
        self.base_url = (base_url or GEMINI_API_ROOT).rstrip("/")

    def describe(self) -> str:
        return "gemini_api"

    def create_url(self) -> str:
        return f"{self.base_url}/{INTERACTIONS_PATH}"

    def get_url(self, interaction_id: str) -> str:
        return f"{self.base_url}/{INTERACTIONS_PATH}/{interaction_id}"

    @property
    def session(self) -> Any:
        """A `requests` session, so this transport matches the Vertex one.

        The capability probe reaches for `provider.transport.session` to do its
        read-only reachability check, so without this `doctor --provider
        gemini_api` died with an AttributeError before it could report anything.
        """
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "x-goog-api-key": self._api_key,
        }

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        return self._request("POST", url, headers, json_body=payload)

    def _get(self, url: str, headers: dict[str, str]) -> Any:
        return self._request("GET", url, headers)

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        import requests

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=self.query_timeout_s if method == "GET" else self.timeout_s,
            )
        except requests.exceptions.Timeout as exc:
            raise RequestTimeoutUnknownOutcome(
                f"{method} {url} timed out after {self.timeout_s}s. The request may "
                "have created a billable generation.",
                detail={"provider": self.describe(), "method": method},
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"Transport failure talking to gemini_api: {exc}",
                detail={"provider": self.describe(), "method": method},
            ) from exc
        return response


def payload_preview(payload: dict[str, Any], *, max_len: int = 800) -> str:
    """Log-safe one-line summary of a request body."""
    summary = {
        "model": payload.get("model"),
        "task": (payload.get("generation_config") or {}).get("video_config", {}).get("task"),
        "response_format": payload.get("response_format"),
        "input_types": [i.get("type") for i in payload.get("input", []) if isinstance(i, dict)],
        "previous_interaction_id": payload.get("previous_interaction_id"),
        "background": payload.get("background"),
        "store": payload.get("store"),
    }
    text = json.dumps(summary, ensure_ascii=False)
    return text if len(text) <= max_len else text[:max_len] + "..."
