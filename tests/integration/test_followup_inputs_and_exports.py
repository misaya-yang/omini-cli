from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omni_homevlog.media.inspect_image import LocalImageReport
from omni_homevlog.pipeline.intake import ReferenceSanitizer
from omni_homevlog.pipeline.orchestrator import run_job
from omni_homevlog.schemas import SanitizerReport


def test_missing_adult_confirmation_and_unclean_identity_are_rejected(reference_asset):
    sanitizer = ReferenceSanitizer()
    local = LocalImageReport(path=reference_asset.path_or_uri, ok=True, sha256=reference_asset.sha256)
    for vision in [SanitizerReport(subject_is_adult=None), SanitizerReport(subject_is_adult=True, is_clean_identity_reference=False)]:
        approved, reasons = sanitizer.evaluate(asset=reference_asset, local=local, vision=vision, requires_identity=True)
        assert not approved and reasons


def test_environment_vision_failure_does_not_mean_clean(reference_asset):
    reference_asset.role = "environment"
    local = LocalImageReport(path=reference_asset.path_or_uri, ok=True, sha256=reference_asset.sha256)
    approved, _ = ReferenceSanitizer().evaluate(asset=reference_asset, local=local, vision=SanitizerReport(degraded=True), requires_identity=False)
    assert not approved


@pytest.mark.parametrize("payload", [{}, {"has_text_overlay": "false"}])
def test_incomplete_or_string_booleans_make_vision_degraded(reference_asset, payload):
    client = Mock()
    client.generate_json.return_value = payload
    report = ReferenceSanitizer(client=client).check_vision(Path(reference_asset.path_or_uri))
    assert report.degraded


def test_missing_inline_file_is_refetched_without_losing_lineage(build_job, tmp_path):
    from omni_homevlog.pipeline.resume import fetch_missing_outputs
    ctx, provider, _ = build_job()
    run_job(ctx)
    artifact = ctx.manifest.segments[-1]
    original = Path(artifact.local_path).read_bytes()
    Path(artifact.local_path).unlink()
    async def query(interaction_id):
        provider.calls.append({"kind": "get", "id": interaction_id})
        target = tmp_path / "recovered.mp4"
        target.write_bytes(original)
        return artifact.model_copy(update={"local_path": str(target)})
    # Use a job-local target, as the provider's real implementation does.
    async def query_in_job(interaction_id):
        recovered = await query(interaction_id)
        target = ctx.paths.attempt_path(2, 0, kind="recovered")
        target.write_bytes(Path(recovered.local_path).read_bytes())
        return recovered.model_copy(update={"local_path": str(target)})
    provider.get_interaction = query_in_job
    count = len(provider.calls)
    assert len(fetch_missing_outputs(ctx)) == 1
    restored = ctx.store.load().segments[-1]
    assert Path(restored.local_path).read_bytes() == original
    assert restored.parent_interaction_id == artifact.parent_interaction_id
    assert restored.prompt_sha256 == artifact.prompt_sha256
    assert len(provider.calls) == count + 1 and provider.calls[-1]["kind"] == "get"


def test_no_audio_finalizes_silent_derivative_and_keeps_original(build_job, monkeypatch):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("real local media tools required")
    ctx, _, _ = build_job()
    run_job(ctx)
    source = Path(ctx.manifest.segments[-1].local_path)
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=4:d=30", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source)], check=True, capture_output=True)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    from omni_homevlog.config import reset_settings_cache
    monkeypatch.setenv("OMNI_FFMPEG_BIN", ffmpeg)
    monkeypatch.setenv("OMNI_FFPROBE_BIN", ffprobe)
    reset_settings_cache()
    from omni_homevlog.pipeline.finalize import finalize
    ctx.spec.audio_enabled = False
    ctx.manifest = ctx.store.mutate(spec=ctx.spec)
    result = finalize(ctx)
    assert result.media.has_audio is False and result.derived
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    assert ctx.store.load().final_source_sha256 == original_hash
    assert ctx.store.load().final_source_path == str(source)
    assert ctx.store.load().final_sha256 != original_hash
