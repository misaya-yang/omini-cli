"""The provider's post-download verification.

The integration tests use a fake provider, which means `BaseVideoProvider`'s own
render pipeline is not exercised by them. That gap let a real bug through: `_verify`
read `info.error`, a field `MediaInfo` did not have, so **every real render would
have raised `AttributeError`** after downloading. The fake never noticed because
it built its artifact directly.

These tests drive the real `_artifact_from_result` path with a stub transport, so
the verification logic is covered.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import build_minimal_mp4, sse_body

from omni_homevlog.errors import ProviderError
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.providers.base import BaseVideoProvider, RenderRequest
from omni_homevlog.providers.response_parser import parse_interaction
from omni_homevlog.providers.transport import TransportResult
from omni_homevlog.schemas import MediaInfo
from omni_homevlog.storage.local import JobPaths


class StubTransport:
    """Returns a canned envelope. Never touches a network."""

    debug_dir: str | None = None

    def __init__(self, envelope_payload: Any, *, latency_s: float = 0.1) -> None:
        self._payload = envelope_payload
        self._latency = latency_s

    def create_interaction(self, _payload: dict[str, Any]) -> TransportResult:
        envelope = parse_interaction(self._payload)
        return TransportResult(
            envelope=envelope,
            http_status=200,
            started_at=0.0,
            finished_at=self._latency,
            raw_text="",
        )


def make_provider(tmp_path: Path, envelope_payload: Any) -> BaseVideoProvider:
    return BaseVideoProvider(
        transport=StubTransport(envelope_payload),
        model="gemini-omni-1.1-flash-preview",
        project="test-project",
        paths=JobPaths(tmp_path, "job-20260921-120000-aaa111").ensure(),
    )


def request_for(duration_s: int = 10) -> RenderRequest:
    return RenderRequest(
        task="text_to_video",
        prompt="a calm handheld shot",
        segment_index=0,
        attempt_index=0,
        aspect_ratio="9:16",
        resolution="360p",
        duration_s=duration_s,
    )


def envelope_with(data_b64: str, *, status: str = "completed") -> dict[str, Any]:
    from tests.conftest import completed_interaction_event

    return sse_body(
        [
            completed_interaction_event(
                interaction_id="int-test-0001", video_b64=data_b64, status=status
            )
        ]
    )


def run_render(provider: BaseVideoProvider, request: RenderRequest):
    """Drive the synchronous internals that `_render` would call.

    Mirrors `_render` exactly, minus the `asyncio.to_thread` hop, so the same
    verification code runs.
    """
    from omni_homevlog.costing import estimate_video_seconds_cost

    payload = provider.build_payload(request)
    result = provider.transport.create_interaction(payload)
    estimated = estimate_video_seconds_cost(model=provider.model, video_seconds=request.duration_s)
    return provider._artifact_from_result(request, result, estimated)


#  MediaInfo carries the probe verdict ────────────────────────────────────


def test_media_info_has_an_error_field() -> None:
    """The field `_verify` reads. Its absence was the bug."""
    assert "error" in MediaInfo.model_fields
    assert MediaInfo().error is None


def test_is_usable_requires_a_duration_and_no_error() -> None:
    assert MediaInfo(duration_s=3.0).is_usable
    assert not MediaInfo(duration_s=3.0, error="bad").is_usable
    assert not MediaInfo().is_usable


def test_a_corrupt_file_reports_an_error_through_inspect_media(tmp_path) -> None:
    """The pure-Python fallback must surface "unreadable", not silently pass."""
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a container at all" * 100)

    info = inspect_media(junk, prefer_ffprobe=False)

    assert info.error, "a non-container file produced no error"
    assert not info.is_usable


def test_a_valid_file_has_no_error(tmp_path) -> None:
    good = tmp_path / "good.mp4"
    good.write_bytes(build_minimal_mp4(duration_s=3.0))

    info = inspect_media(good, prefer_ffprobe=False)

    assert info.error is None
    assert info.is_usable
    assert info.duration_s is not None


#  the real render pipeline ───────────────────────────────────────────────


def test_a_good_render_produces_a_verified_artifact(tmp_path) -> None:
    """This is the path that used to raise AttributeError."""
    data = base64.b64encode(build_minimal_mp4(duration_s=10.0)).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    artifact = run_render(provider, request_for(10))

    assert artifact.status == "completed"
    assert artifact.local_path is not None
    assert Path(artifact.local_path).is_file()

    assert artifact.media is not None
    assert artifact.media.error is None
    assert artifact.media.duration_s is not None
    assert abs(artifact.media.duration_s - 10.0) < 0.5
    assert artifact.media.width == 640
    assert artifact.media.height == 360
    assert artifact.media.has_audio is True


def test_the_artifact_records_its_own_lineage(tmp_path) -> None:
    data = base64.b64encode(build_minimal_mp4()).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    request = request_for(10)
    request.parent_interaction_id = "int-parent-0001"
    artifact = run_render(provider, request)

    assert artifact.parent_interaction_id == "int-parent-0001"
    assert artifact.segment_index == 0
    assert artifact.task == "text_to_video"
    assert artifact.prompt_sha256
    assert artifact.artifact_relpath == "renders/segment_00/attempt_00_raw.mp4"
    assert artifact.latency_s is not None


def test_a_truncated_download_is_rejected(tmp_path) -> None:
    """A 200 with 12 bytes of body is not a render."""
    data = base64.b64encode(b"\x00" * 12).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    with pytest.raises(ProviderError, match="only 12 bytes"):
        run_render(provider, request_for(10))


def test_a_non_video_payload_is_rejected(tmp_path) -> None:
    """Large enough to pass the size gate, but not a container."""
    data = base64.b64encode(b"x" * 5000).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    with pytest.raises(ProviderError, match="not a usable video"):
        run_render(provider, request_for(10))


def test_a_failed_interaction_raises_before_writing_anything(tmp_path) -> None:
    from tests.conftest import completed_interaction_event

    payload = sse_body(
        [
            completed_interaction_event(
                interaction_id="int-failed-0001",
                status="failed",
                errors=[{"code": "safety", "message": "blocked"}],
            )
        ]
    )
    provider = make_provider(tmp_path, payload)

    with pytest.raises(ProviderError, match="blocked"):
        run_render(provider, request_for(10))


def test_a_duration_mismatch_is_logged_but_not_fatal(tmp_path) -> None:
    """Providers round. A 9.7s clip for a 10s request is normal, not a failure."""
    data = base64.b64encode(build_minimal_mp4(duration_s=7.0)).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    artifact = run_render(provider, request_for(10))

    assert artifact.media is not None
    assert artifact.media.error is None
    assert artifact.media.duration_s is not None
    assert abs(artifact.media.duration_s - 7.0) < 0.5


def test_the_media_probe_is_recorded_on_the_artifact(tmp_path) -> None:
    data = base64.b64encode(build_minimal_mp4(with_c2pa=True)).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    artifact = run_render(provider, request_for(10))

    assert artifact.media is not None
    assert artifact.media.probed_with in ("ffprobe", "mp4_probe")
    # The C2PA box we wrote should be found, whichever probe ran.
    assert artifact.media.c2pa_present is True


def test_usage_and_cost_are_carried_through(tmp_path) -> None:
    data = base64.b64encode(build_minimal_mp4()).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    artifact = run_render(provider, request_for(10))

    assert artifact.usage["video_output_tokens"] == 5793
    assert artifact.estimated_cost_usd is not None


def test_the_output_filename_reflects_the_attempt(tmp_path) -> None:
    data = base64.b64encode(build_minimal_mp4()).decode()
    provider = make_provider(tmp_path, envelope_with(data))

    request = request_for(10)
    request.attempt_index = 2
    request.artifact_kind = "edit"
    artifact = run_render(provider, request)

    assert artifact.artifact_relpath is not None
    assert artifact.artifact_relpath.endswith("attempt_02_edit.mp4")
