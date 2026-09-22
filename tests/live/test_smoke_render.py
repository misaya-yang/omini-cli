"""Three checks sharing exactly one paid 3-second generation per provider run."""

from __future__ import annotations

import asyncio

import pytest

from omni_homevlog.budget import Budget
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.providers.capability_probe import probe_generation
from omni_homevlog.providers.factory import build_provider
from omni_homevlog.storage.local import LocalStore

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def smoke(live_settings, tmp_path_factory):
    paths = LocalStore(tmp_path_factory.mktemp("live")).job("job-live-smoke").ensure()
    provider = build_provider(paths=paths, settings=live_settings, with_gcs=False).provider
    result, envelope = asyncio.run(
        probe_generation(
            provider,
            task="text_to_video",
            duration_s=3,
            resolution="360p",
            label="live T2V",
            budget=Budget(max_total_calls=1, max_video_seconds_requested=3),
        )
    )
    assert result.status.value == "PASS", f"{result.status}: {result.detail}"
    return result, envelope


def test_text_to_video_returns_a_real_video(smoke):
    _, envelope = smoke
    assert envelope is not None and envelope.interaction_id
    assert envelope.status == "completed" and envelope.videos


def test_the_returned_video_is_verifiable(smoke):
    result, _ = smoke
    media = result.evidence["media"]
    assert media["duration_s"] is not None and abs(media["duration_s"] - 3) < 0.6
    assert media["width"] and media["height"] and media["video_codec"]


def test_media_probe_agrees_with_ffprobe_when_available(smoke):
    from omni_homevlog.media.ffprobe import ffprobe_available
    from omni_homevlog.media.mp4_probe import probe_mp4

    if not ffprobe_available():
        pytest.skip("ffprobe not installed")
    result, _ = smoke
    path = result.evidence["local_path"]
    pure, authoritative = probe_mp4(path), inspect_media(path)
    assert authoritative.probed_with == "ffprobe"
    assert abs(pure.duration_s - authoritative.duration_s) < 0.5
    assert (pure.width, pure.height) == (authoritative.width, authoritative.height)
