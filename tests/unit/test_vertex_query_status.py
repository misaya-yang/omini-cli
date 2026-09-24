"""A failed unary status must override stale in-progress stream replay."""

from __future__ import annotations

import json
from dataclasses import dataclass

from omni_homevlog.providers.response_parser import parse_interaction
from omni_homevlog.providers.transport import VertexRestTransport


@dataclass
class Response:
    status_code: int
    text: str


class Session:
    def __init__(self, unary: Response, stream: Response) -> None:
        self.unary = unary
        self.stream = stream
        self.urls: list[str] = []

    def request(self, method: str, url: str, **_kwargs: object) -> Response:
        assert method == "GET"
        self.urls.append(url)
        return self.stream if "?stream=true" in url else self.unary


def stream_event(event: dict[str, object]) -> str:
    return "event: " + str(event["event_type"]) + "\ndata: " + json.dumps(event) + "\n\n"


def test_terminal_safety_failure_beats_stale_stream_snapshot() -> None:
    unary = Response(
        200,
        json.dumps(
            {
                "id": "interaction-1",
                "status": "failed",
                "errors": [{"code": "safety", "message": "minor safety"}],
                "steps": [{"type": "user_input", "content": [{"type": "text", "text": "fixture"}]}],
            }
        ),
    )
    stream = Response(
        200,
        stream_event(
            {
                "event_type": "interaction.created",
                "interaction": {"id": "interaction-1", "status": "in_progress"},
            }
        )
        + stream_event(
            {
                "event_type": "interaction.status_update",
                "interaction_id": "interaction-1",
                "status": "in_progress",
            }
        ),
    )
    session = Session(unary, stream)
    result = VertexRestTransport(project="test", session=session, keep_raw=False).get_interaction(
        "interaction-1"
    )
    assert result.envelope.status == "failed"
    assert result.envelope.errors[0]["code"] == "safety"
    assert len(session.urls) == 1
    assert "?stream=true" not in session.urls[0]


def test_completed_inline_video_falls_back_to_stream_after_unary_error() -> None:
    stream = Response(
        200,
        stream_event(
            {
                "event_type": "interaction.created",
                "interaction": {"id": "interaction-2", "status": "in_progress"},
            }
        )
        + stream_event({"event_type": "step.start", "index": 0, "step": {"type": "model_output"}})
        + stream_event(
            {
                "event_type": "step.delta",
                "index": 0,
                "delta": {"type": "video", "uri": "gs://bucket/video.mp4"},
            }
        )
        + stream_event(
            {
                "event_type": "interaction.completed",
                "interaction": {"id": "interaction-2", "status": "completed"},
            }
        ),
    )
    session = Session(Response(500, '{"error":{"message":"internal"}}'), stream)
    result = VertexRestTransport(project="test", session=session, keep_raw=False).get_interaction(
        "interaction-2"
    )
    assert result.envelope.status == "completed"
    assert result.envelope.require_video().uri == "gs://bucket/video.mp4"
    assert len(session.urls) == 2 and "?stream=true" in session.urls[1]


def test_stream_status_update_overrides_initial_snapshot() -> None:
    text = stream_event(
        {
            "event_type": "interaction.created",
            "interaction": {"id": "interaction-3", "status": "in_progress"},
        }
    )
    text += stream_event(
        {
            "event_type": "interaction.status_update",
            "interaction_id": "interaction-3",
            "status": "failed",
        }
    )
    assert parse_interaction(text).status == "failed"
