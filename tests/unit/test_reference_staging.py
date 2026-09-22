"""Reference images must reach the provider.

A job is created from local photo files, but the provider can only read `gs://`
URIs or inline bytes. An earlier revision had no staging step at all, so a job
with references failed at the seed render with "reference has no gs:// URI" — the
primary use case, failing at the first paid step.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.providers.base import BaseVideoProvider, RenderRequest
from omni_homevlog.schemas import ReferenceAsset
from omni_homevlog.storage.local import JobPaths


class RecordingGcs:
    """Stands in for GCS, recording what was uploaded."""

    backend = "fake"

    def __init__(self) -> None:
        self.uploads: list[tuple[Path, str]] = []

    def upload_file(self, local_path: Path, uri: str, *, content_type: str | None = None) -> str:
        self.uploads.append((Path(local_path), uri))
        return uri

    def download_to(self, uri: str, local_path: Path) -> Path:  # pragma: no cover
        raise NotImplementedError


class NullTransport:
    """Satisfies the constructor's `debug_dir` assignment. Never dispatches."""

    debug_dir: str | None = None


def make_provider(tmp_path: Path, *, gcs=None, prefix: str | None = None) -> BaseVideoProvider:
    provider = BaseVideoProvider(
        transport=NullTransport(),  # never used: build_payload does not dispatch
        model="gemini-omni-1.1-flash-preview",
        project="test-project",
        paths=JobPaths(tmp_path, "job-20260921-120000-aaa111").ensure(),
        gcs=gcs,
        gcs_prefix=prefix,
    )
    return provider


def local_asset(
    tmp_path: Path, name: str = "face.png", role: str = "identity_closeup"
) -> ReferenceAsset:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
    return ReferenceAsset(
        id=f"ref_{role}",
        path_or_uri=str(path),
        role=role,  # type: ignore[arg-type]
        sha256="a" * 64,
        provenance="synthetic",
        approved=True,
        mime_type="image/png",
    )


def request_for(assets: list[ReferenceAsset]) -> RenderRequest:
    return RenderRequest(
        task="reference_to_video",
        prompt="[# References <IMAGE_REF_0>@Image1]",
        segment_index=0,
        attempt_index=0,
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        references=assets,
    )


# ─ staging to GCS ─────────────────────────────────────────────────────────


def test_a_local_reference_is_uploaded_and_replaced_with_a_uri(tmp_path) -> None:
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")
    asset = local_asset(tmp_path)
    original_path = Path(asset.path_or_uri)

    inputs = provider._resolve_reference_inputs(request_for([asset]))

    assert inputs is not None
    assert len(inputs) == 1
    assert inputs[0]["type"] == "image"
    assert inputs[0]["uri"].startswith(
        "gs://bucket/omni/jobs/job-20260921-120000-aaa111/input/references/"
    )
    assert inputs[0]["mime_type"] == "image/png"

    # The upload actually happened, from the local file.
    assert len(gcs.uploads) == 1
    uploaded_path, uploaded_uri = gcs.uploads[0]
    assert uploaded_path == original_path
    assert uploaded_uri == inputs[0]["uri"]


def test_the_asset_is_updated_so_a_repair_does_not_re_upload(tmp_path) -> None:
    """A re-render reuses the staged object instead of uploading the photo again."""
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")
    asset = local_asset(tmp_path)

    provider._resolve_reference_inputs(request_for([asset]))
    assert asset.staged_uri.startswith("gs://")
    assert Path(asset.path_or_uri).is_file()

    provider._resolve_reference_inputs(request_for([asset]))
    assert len(gcs.uploads) == 1, "the reference was uploaded twice"


def test_an_already_staged_reference_is_passed_through(tmp_path) -> None:
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")
    asset = local_asset(tmp_path)
    asset.path_or_uri = "gs://elsewhere/already.png"

    inputs = provider._resolve_reference_inputs(request_for([asset]))

    assert inputs is not None
    assert inputs[0]["uri"] == "gs://elsewhere/already.png"
    assert gcs.uploads == []


def test_an_explicit_reference_uri_map_wins(tmp_path) -> None:
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")
    asset = local_asset(tmp_path)

    request = request_for([asset])
    request.reference_uris = {asset.id: "gs://explicit/override.png"}

    inputs = provider._resolve_reference_inputs(request)
    assert inputs is not None
    assert inputs[0]["uri"] == "gs://explicit/override.png"
    assert gcs.uploads == []


# ─ inline fallback ────────────────────────────────────────────────────────


def test_without_a_bucket_the_bytes_go_inline(tmp_path) -> None:
    provider = make_provider(tmp_path, gcs=None, prefix=None)
    asset = local_asset(tmp_path)

    inputs = provider._resolve_reference_inputs(request_for([asset]))

    assert inputs is not None
    assert "uri" not in inputs[0]
    assert inputs[0]["mime_type"] == "image/png"
    decoded = base64.b64decode(inputs[0]["data"])
    assert decoded.startswith(b"\x89PNG")


def test_a_missing_reference_file_is_an_error(tmp_path) -> None:
    provider = make_provider(tmp_path, gcs=RecordingGcs(), prefix="gs://bucket/x/")
    asset = local_asset(tmp_path)
    Path(asset.path_or_uri).unlink()

    with pytest.raises(InvalidRequestError, match="neither a gs:// URI nor a readable"):
        provider._resolve_reference_inputs(request_for([asset]))


def test_no_references_means_no_inputs(tmp_path) -> None:
    provider = make_provider(tmp_path)
    assert provider._resolve_reference_inputs(request_for([])) is None


# ─ ordering still matches the prompt tokens ───────────────────────────────


def test_staged_inputs_keep_the_prompt_token_order(tmp_path) -> None:
    """The prompt says `@Image1`, `@Image2`; the inputs must agree."""
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")

    environment = local_asset(tmp_path, "room.png", role="environment")
    closeup = local_asset(tmp_path, "face.png", role="identity_closeup")
    body = local_asset(tmp_path, "body.png", role="identity_body")

    # Deliberately out of order.
    inputs = provider._resolve_reference_inputs(request_for([environment, closeup, body]))

    assert inputs is not None
    names = [Path(i["uri"]).name for i in inputs]
    assert names == ["face.png", "body.png", "room.png"], (
        "input order must match REFERENCE_ROLE_ORDER, which the prompt tokens are generated from"
    )


def test_the_full_payload_carries_the_staged_uris(tmp_path) -> None:
    """End to end through `build_payload`, which is what actually gets dispatched."""
    gcs = RecordingGcs()
    provider = make_provider(tmp_path, gcs=gcs, prefix="gs://bucket/omni/")
    asset = local_asset(tmp_path)

    from types import SimpleNamespace

    spec = SimpleNamespace(
        aspect_ratio="9:16",
        resolution="720p",
        target_duration_s=30,
        thread=None,
    )

    request = request_for([asset])
    payload = provider.build_payload(request)

    images = [i for i in payload["input"] if i["type"] == "image"]
    assert len(images) == 1
    assert images[0]["uri"].startswith("gs://bucket/")

    assert payload["generation_config"]["video_config"]["task"] == "reference_to_video"
    _ = spec
