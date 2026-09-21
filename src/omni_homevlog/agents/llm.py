"""A small JSON-capable text/vision model client.

Why this exists separately from the Omni provider layer: §4.1 and §24.10. Omni has
no system instruction and no structured output, and the model card confirms it is
not a dependable JSON planner. So planning and critique run on ordinary
`generateContent` models, which do support both, and this module is that path.

Deliberately not a framework. It builds one request, parses one JSON object, and
classifies failures with the same rules as the video transport:

  * 5xx retried, up to the configured limit
  * 429 / 4xx surfaced, never worked around
  * a response that is not valid JSON raises, because a planner that silently
    returns `{}` produces a confidently wrong plan
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from omni_homevlog.errors import (
    ConfigError,
    InvalidRequestError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
    classify_http_error,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.providers.response_parser import parse_error_body

logger = get_logger("llm")

VERTEX_GENERATE_URL = (
    "https://aiplatform.googleapis.com/v1beta1/projects/{project}/locations/{location}"
    "/publishers/google/models/{model}:generateContent"
)
GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

MAX_RETRIES = 2


@dataclass(slots=True)
class LLMResponse:
    text: str
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class InlineImage:
    """An image sent as inline base64 data (reference photos are small)."""

    data_b64: str
    mime_type: str


class TextModelClient:
    """Calls `generateContent` on a Vertex or Gemini-API model.

    Reuses the ADC session so there is exactly one credential path in the
    codebase, and never touches a token directly.
    """

    def __init__(
        self,
        *,
        model: str,
        provider: str = "vertex",
        project: str | None = None,
        location: str = "global",
        api_key: str | None = None,
        session: Any | None = None,
        timeout_s: float = 180.0,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.model = model
        self.provider = provider
        self.project = project
        self.location = location
        self._api_key = api_key
        self._session = session
        self.timeout_s = timeout_s
        self.max_retries = max_retries

        if provider == "vertex" and not project:
            raise ConfigError("TextModelClient needs a project when provider='vertex'.")

    @property
    def session(self) -> Any:
        if self._session is None:
            if self.provider == "vertex":
                from omni_homevlog.providers.auth import get_authorized_session

                self._session = get_authorized_session()
            else:
                import requests

                self._session = requests.Session()
        return self._session

    def url(self) -> str:
        if self.provider == "vertex":
            return VERTEX_GENERATE_URL.format(
                project=self.project, location=self.location, model=self.model
            )
        return GEMINI_GENERATE_URL.format(model=self.model)

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.provider != "vertex" and self._api_key:
            # Header, never a query parameter: query strings leak into logs and
            # stack traces.
            headers["x-goog-api-key"] = self._api_key
        return headers

    # ─ public API ─────────────────────────────────────────────────────────

    def generate_json(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        images: list[InlineImage] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 8192,
        thinking_budget: int | None = 0,
    ) -> dict[str, Any]:
        """Run one request and return parsed JSON.

        `response_schema` is sent as `responseJsonSchema`, which is the structured
        output the plan allows on these models (§24.10 restricts that only for
        Omni). When the model ignores or rejects it, the prompt still demands JSON
        and `extract_json` handles a fenced or chatty reply.
        """
        response = self.generate(
            prompt=prompt,
            system_instruction=system_instruction,
            images=images,
            response_schema=response_schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
        )
        return extract_json(response.text)

    def generate(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        images: list[InlineImage] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.4,
        max_output_tokens: int = 8192,
        thinking_budget: int | None = 0,
    ) -> LLMResponse:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for image in images or []:
            parts.append({"inline_data": {"mime_type": image.mime_type, "data": image.data_b64}})

        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        }
        if response_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseJsonSchema"] = response_schema
        if thinking_budget is not None:
            # Planners and critics do not need a visible reasoning pass, and the
            # budget keeps latency and token cost predictable.
            generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        return self._post_with_retries(payload)

    # ── transport ──────────────────────────────────────────────────────────

    def _post_with_retries(self, payload: dict[str, Any]) -> LLMResponse:
        url = self.url()
        attempt = 0
        last_error: ProviderError | None = None

        while attempt <= self.max_retries:
            if attempt:
                delay = min(20.0, 1.0 * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                time.sleep(delay)
            started = time.monotonic()
            try:
                response = self.session.post(
                    url, headers=self.headers(), json=payload, timeout=self.timeout_s
                )
            except Exception as exc:
                name = type(exc).__name__
                if "Timeout" in name:
                    raise RequestTimeoutUnknownOutcome(
                        f"{self.model} request timed out after {self.timeout_s}s.",
                        detail={"model": self.model},
                    ) from exc
                raise ProviderError(
                    f"Transport failure calling {self.model}: {exc}",
                    detail={"model": self.model},
                ) from exc

            latency = time.monotonic() - started
            status = int(getattr(response, "status_code", 0))
            text = str(getattr(response, "text", ""))

            if status >= 400:
                message, code = parse_error_body(text, http_status=status)
                error = classify_http_error(
                    status, provider_code=code, message=message, detail={"model": self.model}
                )
                last_error = error
                if error.retryable and attempt < self.max_retries:
                    logger.info(
                        f"{self.model} returned {status}; retrying",
                        extra={"extra_fields": {"attempt": attempt + 1}},
                    )
                    attempt += 1
                    continue
                raise error

            return self._parse_response(text, latency)

        assert last_error is not None
        raise last_error

    def _parse_response(self, text: str, latency: float) -> LLMResponse:
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"{self.model} returned a non-JSON response body.",
                detail={"preview": text[:300]},
            ) from exc

        candidates = body.get("candidates") or []
        if not candidates:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                from omni_homevlog.errors import SafetyBlockedError

                raise SafetyBlockedError(
                    f"{self.model} blocked the request: {blocked}",
                    detail={"prompt_feedback": body.get("promptFeedback")},
                )
            raise ProviderError(
                f"{self.model} returned no candidates.",
                detail={"keys": list(body.keys())},
            )

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        parts = ((candidate.get("content") or {}).get("parts")) or []
        text_out = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))

        usage = body.get("usageMetadata") or {}

        if not text_out.strip():
            raise ProviderError(
                f"{self.model} returned an empty candidate (finishReason={finish_reason}).",
                detail={"finish_reason": finish_reason, "usage": usage},
            )

        return LLMResponse(
            text=text_out,
            model=self.model,
            usage=usage,
            latency_s=latency,
            finish_reason=finish_reason,
            raw=body,
        )


# ─────────────────────────────────────────────────────────────────────────────
# JSON extraction
# ─────────────────────────────────────────────────────────────────────────────


def extract_json(text: str) -> dict[str, Any]:
    """Pull one JSON object out of a model reply.

    Models wrap JSON in fences, prefix it with "Here is the plan:", or append a
    trailing sentence. All three are recoverable. What is *not* recoverable is a
    reply with no JSON at all, and that raises — see the module docstring.
    """
    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        candidate = cleaned[start : end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            raise InvalidRequestError(
                f"Model reply contained a JSON-looking block that does not parse: {exc}",
                detail={"preview": candidate[:400]},
            ) from exc

    raise InvalidRequestError(
        "Model reply contained no JSON object.",
        detail={"preview": cleaned[:400]},
    )


def build_client(
    *,
    model: str,
    provider: str,
    settings: Any,
    project: str | None = None,
    location: str | None = None,
) -> TextModelClient:
    """Construct a client for the Director or the Critic."""
    if provider == "vertex":
        return TextModelClient(
            model=model,
            provider="vertex",
            project=project or settings.resolve_project(None),
            location=location or settings.google_cloud_location,
            timeout_s=min(300.0, settings.omni_request_timeout_s),
        )
    return TextModelClient(
        model=model,
        provider="gemini_api",
        api_key=settings.gemini_api_key,
        timeout_s=min(300.0, settings.omni_request_timeout_s),
    )
