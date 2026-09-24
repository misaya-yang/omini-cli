"""Parsing the Interactions response.

Three shapes have to be handled, because we support both a REST transport and an
optional SDK transport, and because the surface is Preview:

  1. **SSE text** (verified on Vertex): even a synchronous `create` returns
     `text/event-stream`. Each event is a `data: {json}` line whose payload has
     an `event_type`; the one we want is `interaction.completed`, which carries
     the whole `interaction` object.
  2. **A single JSON object** — some responses (notably `GET /interactions/{id}`)
     are plain JSON rather than a stream.
  3. **SDK model objects** — pydantic models from `google-genai`, which expose
     the same field names but as attributes rather than dict keys.

The parser normalises all three into an `InteractionEnvelope`, then digs out the
video content. It never assumes a field exists: `steps` layout, the position of
`model_output`, and the delivery mode all vary, so each is probed defensively and
a miss is reported rather than raised as a mystery.

Anything that *is* an error is classified through `errors.classify_http_error`.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from omni_homevlog.errors import InteractionFailedError, ProviderError
from omni_homevlog.observability.logging import get_logger

logger = get_logger("parser")

_DONE_MARKERS = {"[DONE]", "done"}

#: Status values we treat as "still working".
PENDING_STATUSES = frozenset({"in_progress", "requires_action", "pending", "running", "queued"})
#: Status values we treat as "finished, one way or another".
SETTLED_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "incomplete", "budget_exceeded", "budget_exhausted"}
)


@dataclass(slots=True)
class VideoContent:
    """One video payload, from either delivery mode."""

    uri: str | None = None
    data_b64: str | None = None
    mime_type: str | None = None
    name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_inline(self) -> bool:
        return self.data_b64 is not None

    def decode(self) -> bytes:
        if self.data_b64 is None:
            raise ProviderError("This video content has no inline data; it uses URI delivery.")
        try:
            return base64.b64decode(self.data_b64, validate=False)
        except Exception as exc:
            raise ProviderError(f"Could not base64-decode inline video data: {exc}") from exc


@dataclass(slots=True)
class InteractionEnvelope:
    """Normalised view over one interaction, whatever surface it came from."""

    interaction_id: str
    status: str
    model: str | None = None
    previous_interaction_id: str | None = None
    created: str | None = None
    updated: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    videos: list[VideoContent] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    #: Populated when the transport saw an explicit `error` SSE event.
    sse_error: dict[str, Any] | None = None
    #: Which parse path produced this — recorded in debug fixtures.
    parsed_from: str = "unknown"

    @property
    def is_pending(self) -> bool:
        return self.status.lower() in PENDING_STATUSES

    @property
    def is_settled(self) -> bool:
        return self.status.lower() in SETTLED_STATUSES

    @property
    def video(self) -> VideoContent | None:
        return self.videos[0] if self.videos else None

    def require_video(self) -> VideoContent:
        if not self.videos:
            raise ProviderError(
                f"Interaction {self.interaction_id} finished with status "
                f"'{self.status}' but contained no video content.",
                interaction_id=self.interaction_id,
                detail={"status": self.status, "step_types": _step_types(self.steps)},
            )
        return self.videos[0]

    def raise_for_status(self) -> None:
        """Turn a failed/settled-bad interaction into the right exception."""
        low = self.status.lower()
        if low == "completed":
            return
        if low in PENDING_STATUSES:
            return

        message = (
            "; ".join(str(err.get("message") or err) for err in self.errors)
            or f"Interaction ended with status '{self.status}'"
        )
        code = None
        for err in self.errors:
            if err.get("code"):
                code = str(err["code"])
                break

        raise InteractionFailedError(
            message,
            provider_code=code,
            interaction_id=self.interaction_id,
            detail={"status": self.status, "errors": self.errors},
        )


def _step_types(steps: list[dict[str, Any]]) -> list[str]:
    return [str(s.get("type")) for s in steps if isinstance(s, dict)]


# ─────────────────────────────────────────────────────────────────────────────
# Unwrapping whatever the transport handed us
# ─────────────────────────────────────────────────────────────────────────────


def _to_plain(value: Any) -> Any:
    """Best-effort conversion of SDK model objects into plain dicts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _to_plain(value.model_dump(exclude_none=True, by_alias=True))
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            k: _to_plain(v)
            for k, v in vars(value).items()
            if not k.startswith("_") and v is not None
        }
    return value


def iter_sse_events(text: str) -> list[dict[str, Any]]:
    """Extract JSON payloads from an SSE body.

    Handles `data:` with and without a leading space, `event:` lines, comments,
    and multi-line `data:` continuations (the SSE spec allows them even though
    the Interactions endpoint does not appear to use them).
    """
    events: list[dict[str, Any]] = []
    data_buffer: list[str] = []

    def flush() -> None:
        if not data_buffer:
            return
        blob = "\n".join(data_buffer).strip()
        data_buffer.clear()
        if not blob or blob.lower() in _DONE_MARKERS:
            return
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            # A truncated final chunk is not necessarily fatal: earlier events
            # may already contain the completed interaction. Record and move on.
            logger.debug(
                "Skipped unparseable SSE payload", extra={"extra_fields": {"len": len(blob)}}
            )
            return
        if isinstance(parsed, dict):
            events.append(parsed)
        elif isinstance(parsed, list):
            events.extend(e for e in parsed if isinstance(e, dict))

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_buffer.append(line[5:].lstrip())
        elif line.strip() == "":
            flush()
        elif line.startswith(":") or line.startswith("event:") or line.startswith("id:"):
            # Comment / named-event / id lines carry no payload.
            continue
        else:
            # Not SSE at all — the caller probably handed us plain JSON.
            continue
    flush()
    return events


def looks_like_sse(text: str) -> bool:
    return bool(re.search(r"^data:\s*\{", text, flags=re.MULTILINE))


def extract_interaction_payload(raw: Any) -> tuple[dict[str, Any], str]:
    """Normalise transport output into `(interaction_dict, parsed_from)`."""
    plain = _to_plain(raw)

    # Case: plain dict that already *is* the interaction.
    if isinstance(plain, dict):
        if "event_type" in plain or "interaction" in plain:
            return _from_event_stream([plain])
        if "id" in plain and ("status" in plain or "steps" in plain):
            return plain, "json:interaction"
        # Unknown dict: fall through and let the caller report it.
        return plain, "json:unknown"

    # Case: a list of SSE events already decoded.
    if isinstance(plain, list) and plain and isinstance(plain[0], dict):
        return _from_event_stream(plain)

    # Case: raw SSE text.
    if isinstance(raw, str):
        if looks_like_sse(raw):
            return _from_event_stream(iter_sse_events(raw))
        try:
            return extract_interaction_payload(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise ProviderError(
                "Provider response was neither SSE nor JSON.",
                detail={"preview": raw[:200]},
            ) from exc

    raise ProviderError(
        "Unrecognised provider response shape.",
        detail={"python_type": type(raw).__name__},
    )


def _from_event_stream(events: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Pick the best interaction object out of a decoded SSE event list.

    Preference order:
      1. `interaction.completed` (verified happy path)
      2. a bare `error` event → surfaced as a provider error
      3. any event carrying an `interaction` object (polling snapshots)

    The error event outranks a stale snapshot deliberately. The stream emits
    `interaction.created` before it can fail, so checking the snapshot first meant
    a safety refusal was reported as "completed with no video content" — which
    sends the reader hunting a parser bug instead of reading the policy message.
    """
    completed: dict[str, Any] | None = None
    latest_any: dict[str, Any] | None = None
    sse_error: dict[str, Any] | None = None
    steps: dict[int, dict[str, Any]] = {}
    seen_events: set[str] = set()
    interaction_id: str | None = None
    latest_status: str | None = None

    for event in events:
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
        etype = str(event.get("event_type") or event.get("type") or "")
        interaction = event.get("interaction")
        observed_id = (
            interaction.get("id") if isinstance(interaction, dict) else event.get("interaction_id")
        )
        if observed_id:
            if interaction_id and interaction_id != observed_id:
                raise ProviderError("Stream mixes different interaction IDs.")
            interaction_id = str(observed_id)
        if isinstance(interaction, dict) and isinstance(interaction.get("status"), str):
            latest_status = interaction["status"]
        elif etype == "interaction.status_update" and isinstance(event.get("status"), str):
            latest_status = event["status"]
        index = event.get("index")
        if isinstance(index, int) and etype == "step.start" and isinstance(event.get("step"), dict):
            steps[index] = dict(event["step"])
        elif isinstance(index, int) and etype == "step.delta":
            step = steps.get(index)
            delta = event.get("delta")
            if (
                step is not None
                and step.get("type") == "model_output"
                and isinstance(delta, dict)
                and delta.get("type") in ("video", "text", "image", "audio")
            ):
                # Observed GET: video is a delta, completion has status/usage only.
                step.setdefault("content", []).append(dict(delta))
        if etype == "interaction.completed" and isinstance(interaction, dict):
            completed = interaction
        elif etype == "error":
            sse_error = event
        elif isinstance(interaction, dict):
            latest_any = interaction

    if completed is not None:
        payload = dict(completed)
        if not payload.get("steps") and steps:
            payload["steps"] = [steps[i] for i in sorted(steps)]
        return payload, "sse:interaction.completed"

    if sse_error is not None:
        err = sse_error.get("error") or sse_error
        # An interaction may still be attached to the error event.
        attached = sse_error.get("interaction")
        if isinstance(attached, dict):
            return attached, "sse:error+interaction"
        raise ProviderError(
            str(err.get("message") if isinstance(err, dict) else err),
            provider_code=str(err.get("code")) if isinstance(err, dict) else None,
            interaction_id=interaction_id,
            detail={"sse_error": err},
        )

    if latest_any is not None:
        payload = dict(latest_any)
        if latest_status:
            payload["status"] = latest_status
        if not payload.get("steps") and steps:
            payload["steps"] = [steps[i] for i in sorted(steps)]
        return payload, "sse:other"

    raise ProviderError(
        "Event stream contained no interaction object.",
        detail={"event_types": [str(e.get("event_type")) for e in events][:20]},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Digging the video out of an interaction
# ─────────────────────────────────────────────────────────────────────────────


#: Keys we look inside first, because they are where video content lives.
_PRIORITY_KEYS = ("output_video", "video", "content", "contents", "steps", "output", "outputs")

#: Keys we never descend into: the request echo and debug blobs.
_SKIP_KEYS = frozenset({"input", "raw", "request"})


def _collect_video_contents(value: Any, out: list[VideoContent], depth: int = 0) -> None:
    """Walk an interaction payload looking for `type: "video"` content items.

    Structured traversal would be brittle across Preview revisions, so we walk the
    whole tree instead. Two properties matter:

    * **each subtree is visited exactly once.** An earlier version walked the
      known keys and then walked everything again as a fallback, which found the
      same video twice and made `len(envelope.videos)` lie.
    * **depth-bounded**, and video nodes are not descended into further, so a
      self-referential or absurdly nested payload cannot hang the parser.
    """
    if depth > 8:
        return

    if isinstance(value, dict):
        if value.get("type") in ("user_input", "tool_result", "thought", "thinking"):
            return
        if value.get("type") == "video" and ("uri" in value or "data" in value):
            out.append(
                VideoContent(
                    uri=_as_str(value.get("uri")),
                    data_b64=_as_str(value.get("data")),
                    mime_type=_as_str(value.get("mime_type")),
                    name=_as_str(value.get("name")),
                    raw=value,
                )
            )
            return

        # Priority keys first, then everything else, with no key visited twice.
        seen: set[str] = set()
        for key in _PRIORITY_KEYS:
            if key in value:
                seen.add(key)
                _collect_video_contents(value[key], out, depth + 1)
        for key, item in value.items():
            if key in seen or key in _SKIP_KEYS:
                continue
            if isinstance(item, (dict, list)):
                _collect_video_contents(item, out, depth + 1)

    elif isinstance(value, list):
        for item in value:
            _collect_video_contents(item, out, depth + 1)


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _extract_errors(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("errors")
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def parse_interaction(raw: Any, *, expected_id: str | None = None) -> InteractionEnvelope:
    """Parse a transport response into an `InteractionEnvelope`.

    `expected_id` lets a caller assert that the response belongs to the
    interaction it asked about, which matters for recovery (§5.1).
    """
    payload, parsed_from = extract_interaction_payload(raw)

    interaction_id = _as_str(payload.get("id")) or expected_id or "unknown"
    if expected_id and _as_str(payload.get("id")) and _as_str(payload.get("id")) != expected_id:
        raise ProviderError(
            f"Provider returned interaction {payload.get('id')!r} when "
            f"{expected_id!r} was requested.",
            interaction_id=interaction_id,
        )

    status = _as_str(payload.get("status")) or "unknown"

    videos: list[VideoContent] = []
    _collect_video_contents(payload.get("steps") if "steps" in payload else payload, videos)
    if not videos and "output_video" in payload:
        _collect_video_contents(payload["output_video"], videos)

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

    return InteractionEnvelope(
        interaction_id=interaction_id,
        status=status,
        model=_as_str(payload.get("model")),
        previous_interaction_id=_as_str(payload.get("previous_interaction_id")),
        created=_as_str(payload.get("created")),
        updated=_as_str(payload.get("updated")),
        usage=usage or {},
        errors=_extract_errors(payload),
        videos=videos,
        steps=[s for s in (payload.get("steps") or []) if isinstance(s, dict)],
        raw=payload,
        parsed_from=parsed_from,
    )


def parse_error_body(body: Any, *, http_status: int | None) -> tuple[str, str | None]:
    """Extract `(message, provider_code)` from a provider error body.

    Verified Vertex error envelope:
        {"error": {"message": "...", "code": "invalid_request"}}
    Note `code` is a *string*, not the HTTP status.
    """
    plain = _to_plain(body)
    if isinstance(plain, str):
        try:
            plain = json.loads(plain)
        except json.JSONDecodeError:
            return plain[:500], None
    if isinstance(plain, dict):
        nested = plain.get("error")
        err: dict[str, Any] = nested if isinstance(nested, dict) else plain
        message = err.get("message") or err.get("error_description") or json.dumps(err)[:500]
        code = err.get("code") or err.get("status")
        return str(message), (str(code) if code is not None else None)
    return f"HTTP {http_status}", None
