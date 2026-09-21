"""§21.1: the provider response parser.

This is the module that has to survive a Preview surface changing shape. It is
also where the verified SSE format meets the SDK's object format, so both are
tested, along with the malformed cases that must fail loudly rather than return an
empty result that reads as success.
"""

from __future__ import annotations

import base64
import json

import pytest
from tests.conftest import completed_interaction_event, sse_body

from omni_homevlog.errors import InteractionFailedError, ProviderError
from omni_homevlog.providers.response_parser import (
    InteractionEnvelope,
    extract_interaction_payload,
    iter_sse_events,
    looks_like_sse,
    parse_error_body,
    parse_interaction,
)

# ── SSE decoding ───────────────────────────────────────────────────────────


def test_sse_body_decodes_to_events() -> None:
    body = sse_body([completed_interaction_event()])
    events = iter_sse_events(body)
    assert len(events) == 1
    assert events[0]["event_type"] == "interaction.completed"


def test_done_marker_is_not_parsed_as_an_event() -> None:
    body = sse_body([completed_interaction_event()])
    assert "[DONE]" not in [str(e) for e in iter_sse_events(body)]


def test_sse_without_space_after_colon_is_handled() -> None:
    body = 'data:{"event_type":"interaction.completed","interaction":{"id":"x","status":"completed"}}\n\n'
    events = iter_sse_events(body)
    assert events[0]["interaction"]["id"] == "x"


def test_comments_and_event_lines_are_ignored() -> None:
    body = (
        ": keep-alive\n"
        "event: message\n"
        "id: 42\n"
        f"data: {json.dumps({'event_type': 'interaction.completed', 'interaction': {'id': 'y', 'status': 'completed'}})}\n"
        "\n"
    )
    events = iter_sse_events(body)
    assert len(events) == 1
    assert events[0]["interaction"]["id"] == "y"


def test_truncated_final_event_does_not_lose_earlier_events() -> None:
    """A cut-off stream may still contain the completed interaction."""
    body = (
        f"data: {json.dumps(completed_interaction_event())}\n\n"
        'data: {"event_type": "step.delta", "step": {"ty'
    )
    events = iter_sse_events(body)
    assert any(e.get("event_type") == "interaction.completed" for e in events)


def test_looks_like_sse_discriminates() -> None:
    assert looks_like_sse(sse_body([completed_interaction_event()]))
    assert not looks_like_sse('{"id": "abc", "status": "completed"}')


# ── payload normalisation ──────────────────────────────────────────────────


def test_plain_json_interaction_is_accepted() -> None:
    payload, source = extract_interaction_payload({"id": "abc", "status": "completed", "steps": []})
    assert payload["id"] == "abc"
    assert source == "json:interaction"


def test_completed_event_wins_over_earlier_events() -> None:
    """The stream may contain several interaction snapshots; take the settled one."""
    body = sse_body(
        [
            {
                "event_type": "interaction.created",
                "interaction": {"id": "abc", "status": "in_progress"},
            },
            {
                "event_type": "interaction.status_update",
                "interaction": {"id": "abc", "status": "in_progress"},
            },
            completed_interaction_event(interaction_id="abc"),
        ]
    )
    envelope = parse_interaction(body)
    assert envelope.status == "completed"
    assert envelope.parsed_from == "sse:interaction.completed"


def test_sdk_style_object_with_model_dump_is_normalised() -> None:
    class FakeSDKInteraction:
        def model_dump(self, **_kwargs):
            return {
                "id": "sdk-1",
                "status": "completed",
                "steps": [
                    {"type": "model_output", "content": [{"type": "video", "uri": "gs://b/o.mp4"}]}
                ],
            }

    envelope = parse_interaction(FakeSDKInteraction())
    assert envelope.interaction_id == "sdk-1"
    assert envelope.videos[0].uri == "gs://b/o.mp4"


def test_unrecognised_shape_raises() -> None:
    with pytest.raises(ProviderError, match=r"neither SSE nor JSON|Unrecognised"):
        parse_interaction(12345)


def test_expected_id_mismatch_raises() -> None:
    """Recovery depends on knowing the response belongs to the interaction we asked about."""
    with pytest.raises(ProviderError, match="when"):
        parse_interaction({"id": "other", "status": "completed", "steps": []}, expected_id="wanted")


# ── video extraction ──────────────────────────────────────────────────────


def test_inline_video_is_decoded() -> None:
    data = base64.b64encode(b"fake mp4 bytes").decode()
    envelope = parse_interaction(sse_body([completed_interaction_event(video_b64=data)]))
    video = envelope.require_video()
    assert video.is_inline
    assert video.decode() == b"fake mp4 bytes"


def test_uri_video_is_extracted() -> None:
    envelope = parse_interaction(
        sse_body([completed_interaction_event(video_uri="gs://bucket/jobs/x/out.mp4")])
    )
    assert envelope.require_video().uri == "gs://bucket/jobs/x/out.mp4"
    assert not envelope.require_video().is_inline


def test_missing_video_raises_rather_than_returning_empty() -> None:
    """§21.2: no video must be an error, never a silently-empty success."""
    event = {
        "event_type": "interaction.completed",
        "interaction": {
            "id": "abc",
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "hi"}]}],
        },
    }
    envelope = parse_interaction(sse_body([event]))
    with pytest.raises(ProviderError, match="no video content"):
        envelope.require_video()


def test_video_nested_in_output_video_field() -> None:
    envelope = parse_interaction(
        {
            "id": "abc",
            "status": "completed",
            "output_video": {"type": "video", "uri": "gs://b/o.mp4", "mime_type": "video/mp4"},
        }
    )
    assert envelope.videos[0].uri == "gs://b/o.mp4"


def test_usage_is_captured() -> None:
    envelope = parse_interaction(
        sse_body(
            [
                completed_interaction_event(
                    usage={
                        "text_input_tokens": 45,
                        "video_output_tokens": 5793,
                        "thought_tokens": 297,
                    }
                )
            ]
        )
    )
    assert envelope.usage["video_output_tokens"] == 5793


# ─ failure classification ─────────────────────────────────────────────────


def test_failed_status_raises_with_the_provider_message() -> None:
    event = {
        "event_type": "interaction.completed",
        "interaction": {
            "id": "abc",
            "status": "failed",
            "errors": [{"code": "safety", "message": "blocked by policy"}],
            "steps": [],
        },
    }
    envelope = parse_interaction(sse_body([event]))
    with pytest.raises(InteractionFailedError, match="blocked by policy"):
        envelope.raise_for_status()


def test_in_progress_is_not_an_error() -> None:
    envelope = parse_interaction({"id": "abc", "status": "in_progress", "steps": []})
    assert envelope.is_pending
    envelope.raise_for_status()  # must not raise


def test_error_event_without_an_interaction_raises() -> None:
    body = (
        'data: {"event_type": "error", "error": {"code": "invalid_request", "message": "bad"}}\n\n'
    )
    with pytest.raises(ProviderError, match="bad"):
        parse_interaction(body)


def test_error_event_with_an_attached_interaction_is_kept() -> None:
    body = (
        'data: {"event_type": "error", "error": {"code": "x"}, '
        '"interaction": {"id": "abc", "status": "failed", "steps": []}}\n\n'
    )
    envelope = parse_interaction(body)
    assert envelope.interaction_id == "abc"
    assert envelope.parsed_from == "sse:error+interaction"


# ── the verified error envelope ────────────────────────────────────────────


def test_vertex_error_envelope_is_parsed() -> None:
    """The exact shape the live endpoint returned for a bogus interaction id."""
    body = '{"error":{"message":"Request contains an invalid argument.","code":"invalid_request"}}'
    message, code = parse_error_body(body, http_status=400)
    assert message == "Request contains an invalid argument."
    assert code == "invalid_request"


def test_error_code_is_a_string_not_the_http_status() -> None:
    """Worth pinning: the envelope's `code` is a symbolic string."""
    _message, code = parse_error_body(
        '{"error":{"message":"m","code":"invalid_request"}}', http_status=400
    )
    assert code == "invalid_request"
    assert code != "400"


def test_non_json_error_body_still_yields_a_message() -> None:
    message, code = parse_error_body("<html>502 Bad Gateway</html>", http_status=502)
    assert "502" in message
    assert code is None


# ── status mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_pending"),
    [
        ("in_progress", True),
        ("requires_action", True),
        ("completed", False),
        ("failed", False),
        ("cancelled", False),
    ],
)
def test_pending_classification(raw: str, expected_pending: bool) -> None:
    envelope = InteractionEnvelope(interaction_id="x", status=raw)
    assert envelope.is_pending is expected_pending


def test_deeply_nested_video_content_is_found() -> None:
    """Structured traversal would be brittle across Preview revisions, so we walk."""
    payload = {
        "id": "abc",
        "status": "completed",
        "steps": [
            {"type": "thinking", "content": [{"type": "text", "text": "..."}]},
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": "here"},
                    {"type": "video", "uri": "gs://b/deep.mp4", "mime_type": "video/mp4"},
                ],
            },
        ],
    }
    envelope = parse_interaction(payload)
    assert envelope.videos[0].uri == "gs://b/deep.mp4"


def test_multiple_model_output_steps_yield_multiple_videos() -> None:
    """§21.2 lists this case. We take the first and do not crash."""
    payload = {
        "id": "abc",
        "status": "completed",
        "steps": [
            {"type": "model_output", "content": [{"type": "video", "uri": "gs://b/one.mp4"}]},
            {"type": "model_output", "content": [{"type": "video", "uri": "gs://b/two.mp4"}]},
        ],
    }
    envelope = parse_interaction(payload)
    assert len(envelope.videos) == 2
    assert envelope.video is not None
    assert envelope.video.uri == "gs://b/one.mp4"
