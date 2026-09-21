"""Live smoke tests (§21.3).

The cheapest useful request per surface: 3 seconds, 360p, no people. §21.3 is
specific about the no-people part, and it is a good rule for two reasons beyond
cost: a person would drag in the identity and consent questions the plan is
careful about, and it would tell us nothing extra about the API.

What these prove, and what they do not:

  * they prove auth, routing, model access, and that a request comes back as a
    parseable interaction with a real video of the right duration and dimensions
  * they do **not** prove anything about chaining, editing, identity consistency,
    or the 10-second ceiling. Those need their own probes.

Estimated cost: one generation per test. `--run-generation` on `omni-vlog doctor`
runs the whole matrix; this file is the smaller, faster check.
"""

from __future__ import annotations

import pytest

from omni_homevlog.errors import QuotaExhaustedError, RequestTimeoutUnknownOutcome
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.providers.capability_probe import probe_generation
from omni_homevlog.providers.factory import build_provider
from omni_homevlog.storage.local import atomic_write_bytes

pytestmark = pytest.mark.live

#: §21.3's cheapest settings.
SMOKE_DURATION_S = 3
SMOKE_RESOLUTION = "360p"


@pytest.fixture(scope="module")
def provider(live_settings, tmp_path_factory):
    """A provider with somewhere to write the output."""
    from omni_homevlog.storage.local import LocalStore

    paths = LocalStore(tmp_path_factory.mktemp("live")).job("job-live-smoke").ensure()
    binding = build_provider(paths=paths, settings=live_settings, with_gcs=False)
    return binding.provider


def _tolerate_quota(exc: Exception) -> None:
    """A 429 on a Preview model is a project entitlement fact, not a code defect.

    Failing the test here would be misleading, so it is reported as a skip with the
    provider's own message.
    """
    if isinstance(exc, QuotaExhaustedError):
        pytest.skip(f"quota unavailable for this project: {exc.message}")
    if isinstance(exc, RequestTimeoutUnknownOutcome):
        pytest.skip(
            "the request timed out with an unknown outcome; it may still be running "
            "and may be billable. Not retried. Re-run later."
        )


def test_text_to_video_returns_a_real_video(provider) -> None:
    """The handoff's verified path, re-run to confirm it still holds."""
    import asyncio

    try:
        result, envelope = asyncio.run(
            probe_generation(
                provider,
                task="text_to_video",
                duration=SMOKE_DURATION_S,
                resolution=SMOKE_RESOLUTION,
                label="live T2V",
            )
        )
    except Exception as exc:
        _tolerate_quota(exc)
        raise

    if result.status.value != "PASS":
        pytest.fail(f"T2V smoke did not pass: {result.status} — {result.detail}")

    assert envelope is not None
    assert envelope.interaction_id
    assert envelope.status == "completed"
    assert envelope.videos, "the interaction completed with no video content"


def test_the_returned_video_is_verifiable(provider) -> None:
    """An HTTP 200 is not an output (handoff acceptance criterion 4)."""
    import asyncio

    try:
        result, envelope = asyncio.run(
            probe_generation(
                provider,
                task="text_to_video",
                duration=SMOKE_DURATION_S,
                resolution=SMOKE_RESOLUTION,
                label="live T2V verifiable",
            )
        )
    except Exception as exc:
        _tolerate_quota(exc)
        raise

    if result.status.value != "PASS" or envelope is None:
        pytest.fail(f"smoke did not pass: {result.detail}")

    video = envelope.video
    assert video is not None

    target = provider.paths.debug_dir / "live_smoke.mp4"
    if video.is_inline:
        atomic_write_bytes(target, video.decode())
    elif video.uri and provider.gcs is not None:
        provider.gcs.download_to(video.uri, target)
    else:
        pytest.skip(
            "The output was delivered by URI and no GCS client is configured, so the "
            "bytes could not be fetched to verify. Set OMNI_OUTPUT_GCS_URI or install "
            "google-cloud-storage."
        )

    info = inspect_media(target)
    assert info.error is None, f"the downloaded file is not a usable video: {info.error}"
    assert info.duration_s is not None, "duration could not be determined"
    assert abs(info.duration_s - SMOKE_DURATION_S) < 1.0, (
        f"asked for {SMOKE_DURATION_S}s, got {info.duration_s:.2f}s"
    )
    assert info.width and info.height, "dimensions could not be determined"
    assert info.video_codec, "no video codec found"


def test_media_probe_agrees_with_ffprobe_when_available(provider, tmp_path) -> None:
    """If ffprobe is installed, cross-check the pure-Python probe against it.

    The pure-Python path is what runs when ffmpeg is absent, so a disagreement
    here would mean the fallback is quietly reporting wrong durations.
    """
    import asyncio

    from omni_homevlog.media.ffprobe import ffprobe_available

    if not ffprobe_available():
        pytest.skip("ffprobe is not installed; nothing to cross-check against")

    try:
        result, envelope = asyncio.run(
            probe_generation(
                provider,
                task="text_to_video",
                duration=SMOKE_DURATION_S,
                resolution=SMOKE_RESOLUTION,
                label="live T2V probe cross-check",
            )
        )
    except Exception as exc:
        _tolerate_quota(exc)
        raise

    if result.status.value != "PASS" or envelope is None or envelope.video is None:
        pytest.fail(f"smoke did not pass: {result.detail}")

    video = envelope.video
    target = tmp_path / "crosscheck.mp4"
    if not video.is_inline:
        pytest.skip("URI delivery without a configured GCS client")

    atomic_write_bytes(target, video.decode())

    from omni_homevlog.media.mp4_probe import probe_mp4

    pure = probe_mp4(target)
    from omni_homevlog.media.ffprobe import inspect_media as inspect_with_ffprobe

    authoritative = inspect_with_ffprobe(target)

    assert authoritative.probed_with == "ffprobe"
    if pure.duration_s is not None and authoritative.duration_s is not None:
        assert abs(pure.duration_s - authoritative.duration_s) < 0.5, (
            f"pure-Python probe says {pure.duration_s}, ffprobe says {authoritative.duration_s}"
        )
    assert pure.width == authoritative.width
    assert pure.height == authoritative.height
