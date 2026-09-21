"""Request construction.

The handoff asks for "one small mocked/request-construction test", and this is the
module worth testing that way: it is pure, and its output shape is the thing the
live endpoint actually validated. Two details in particular are easy to get wrong
and are pinned here: `response_format` is a list, and `delivery: "uri"` is illegal
without `gcs_uri`.
"""

from __future__ import annotations

import pytest

from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.providers.request_builder import (
    build_create_payload,
    image_input,
    order_references,
    reference_tokens,
    validate_aspect_ratio,
    validate_duration,
    validate_gcs_uri,
    validate_mime,
    video_response_format,
)
from omni_homevlog.schemas import ReferenceAsset


def asset(asset_id: str, role: str, sha: str = "a" * 64) -> ReferenceAsset:
    return ReferenceAsset(
        id=asset_id,
        path_or_uri=f"gs://bucket/{asset_id}.png",
        role=role,  # type: ignore[arg-type]
        sha256=sha,
        provenance="synthetic",
        approved=True,
        mime_type="image/png",
    )


# ── the verified request shape ─────────────────────────────────────────────


def test_text_to_video_payload_matches_the_verified_shape() -> None:
    """Compare against the handoff's working request."""
    payload = build_create_payload(
        model="gemini-omni-1.1-flash-preview",
        task="text_to_video",
        prompt="A calm ocean sunrise, realistic handheld footage.",
        aspect_ratio="16:9",
        resolution="360p",
        duration_s=3,
    )

    assert payload["model"] == "gemini-omni-1.1-flash-preview"
    assert payload["input"] == [
        {"type": "text", "text": "A calm ocean sunrise, realistic handheld footage."}
    ]
    assert payload["response_format"] == [
        {
            "type": "video",
            "aspect_ratio": "16:9",
            "resolution": "360p",
            "duration": "3s",
        }
    ]
    assert payload["generation_config"] == {"video_config": {"task": "text_to_video"}}


def test_response_format_is_a_list_not_an_object() -> None:
    """The SDK models it as a single object; the REST endpoint takes a list."""
    entries = video_response_format(aspect_ratio="9:16", resolution="720p", duration_s=10)
    assert isinstance(entries, list)
    assert len(entries) == 1
    assert entries[0]["type"] == "video"


# ── the delivery rule ──────────────────────────────────────────────────────


def test_delivery_is_absent_without_a_gcs_uri() -> None:
    """`delivery: "uri"` without `gcs_uri` is rejected before generation."""
    entries = video_response_format(
        aspect_ratio="9:16", resolution="720p", duration_s=10, gcs_uri=None
    )
    assert "delivery" not in entries[0]
    assert "gcs_uri" not in entries[0]


def test_delivery_uri_is_sent_when_a_bucket_is_configured() -> None:
    entries = video_response_format(
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        gcs_uri="gs://approved/omni-output/",
    )
    assert entries[0]["delivery"] == "uri"
    assert entries[0]["gcs_uri"] == "gs://approved/omni-output/"


def test_delivery_uri_can_be_suppressed() -> None:
    entries = video_response_format(
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        gcs_uri="gs://approved/x/",
        prefer_uri_delivery=False,
    )
    assert "delivery" not in entries[0]


# ── validation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seconds", [3, 4, 9, 10])
def test_valid_durations_are_accepted(seconds: int) -> None:
    assert validate_duration(seconds) == f"{seconds}s"


@pytest.mark.parametrize("seconds", [0, 2, 11, 30, 60])
def test_invalid_durations_are_refused(seconds: int) -> None:
    """The documented range is an integer 3..10."""
    with pytest.raises(InvalidRequestError, match=r"3\.\.10"):
        validate_duration(seconds)


def test_aspect_ratio_is_validated() -> None:
    assert validate_aspect_ratio("9:16") == "9:16"
    with pytest.raises(InvalidRequestError):
        validate_aspect_ratio("1:1")


def test_gcs_uri_is_validated() -> None:
    assert validate_gcs_uri("gs://bucket/path") == "gs://bucket/path"
    for bad in ["https://example.com/x.png", "file:///tmp/x.png", "gs://", "/tmp/x.png"]:
        with pytest.raises(InvalidRequestError):
            validate_gcs_uri(bad)


def test_image_mime_allowlist() -> None:
    for good in ["image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"]:
        assert validate_mime(good, kind="image") == good
    with pytest.raises(InvalidRequestError):
        validate_mime("image/gif", kind="image")


def test_video_mime_allowlist() -> None:
    assert validate_mime("video/mp4", kind="video") == "video/mp4"
    with pytest.raises(InvalidRequestError):
        validate_mime("video/ogg", kind="video")


# ── reference ordering and tokens ──────────────────────────────────────────


def test_references_are_ordered_deterministically() -> None:
    """The prompt's positional tokens must match the input order, every segment."""
    refs = [
        asset("c", "environment"),
        asset("a", "identity_closeup"),
        asset("b", "identity_body"),
    ]
    ordered = [a.role for a in order_references(refs)]
    assert ordered == ["identity_closeup", "identity_body", "environment"]

    # And stable across shuffles.
    shuffled = [refs[2], refs[0], refs[1]]
    assert [a.id for a in order_references(shuffled)] == [a.id for a in order_references(refs)]


def test_reference_tokens_match_the_input_positions() -> None:
    refs = [asset("a", "identity_closeup"), asset("b", "environment")]
    payload = build_create_payload(
        model="m",
        task="reference_to_video",
        prompt=reference_tokens(refs),
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        references=refs,
        reference_uris={a.id: a.path_or_uri for a in refs},
    )

    images = [i for i in payload["input"] if i["type"] == "image"]
    assert [i["uri"] for i in images] == [
        "gs://bucket/a.png",
        "gs://bucket/b.png",
    ]
    assert payload["input"][0]["text"] == "[# References <IMAGE_REF_0>@Image1 <IMAGE_REF_1>@Image2]"


def test_reference_without_a_uri_is_refused() -> None:
    """A local path cannot reach the provider; failing here beats a confusing 400."""
    refs = [
        ReferenceAsset(
            id="local",
            path_or_uri="/tmp/face.png",
            role="identity_closeup",
            sha256="a" * 64,
            provenance="synthetic",
            approved=True,
        )
    ]
    with pytest.raises(InvalidRequestError, match="no gs:// URI"):
        build_create_payload(
            model="m",
            task="reference_to_video",
            prompt="x",
            aspect_ratio="9:16",
            resolution="720p",
            duration_s=10,
            references=refs,
        )


def test_image_to_video_first_frame_shape() -> None:
    payload = build_create_payload(
        model="m",
        task="image_to_video",
        prompt="push in slowly",
        aspect_ratio="16:9",
        resolution="720p",
        duration_s=5,
        first_frame_uri="gs://b/first.png",
    )
    assert payload["generation_config"]["video_config"]["task"] == "image_to_video"
    assert payload["input"][1] == {
        "type": "image",
        "uri": "gs://b/first.png",
        "mime_type": "image/png",
    }


def test_edit_task_shape() -> None:
    payload = build_create_payload(
        model="m",
        task="edit",
        prompt="remove the timestamp",
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        input_video_uri="gs://b/in.mp4",
    )
    assert payload["generation_config"]["video_config"]["task"] == "edit"
    assert payload["input"][1]["type"] == "video"


def test_image_input_helper_validates() -> None:
    assert image_input(uri="gs://b/x.png")["type"] == "image"
    with pytest.raises(InvalidRequestError):
        image_input(uri="/tmp/x.png")


# ─ optional fields ────────────────────────────────────────────────────────


def test_previous_interaction_id_is_forwarded_when_given() -> None:
    payload = build_create_payload(
        model="m",
        task="extend",
        prompt="continue",
        aspect_ratio="9:16",
        resolution="720p",
        duration_s=10,
        previous_interaction_id="int-parent",
    )
    assert payload["previous_interaction_id"] == "int-parent"


def test_chaining_fields_are_absent_by_default() -> None:
    payload = build_create_payload(
        model="m",
        task="text_to_video",
        prompt="x",
        aspect_ratio="9:16",
        resolution="360p",
        duration_s=3,
    )
    assert "previous_interaction_id" not in payload
    assert "store" not in payload
    assert "system_instruction" not in payload


def test_store_false_is_only_sent_when_explicitly_requested() -> None:
    """§24.9 forbids `store=false` on a chain that expects to edit later."""
    payload = build_create_payload(
        model="m",
        task="text_to_video",
        prompt="x",
        aspect_ratio="9:16",
        resolution="360p",
        duration_s=3,
        store=False,
    )
    assert payload["store"] is False


def test_labels_are_forwarded_for_ledger_correlation() -> None:
    payload = build_create_payload(
        model="m",
        task="text_to_video",
        prompt="x",
        aspect_ratio="9:16",
        resolution="360p",
        duration_s=3,
        labels={"job_kind": "seed", "segment": "0"},
    )
    assert payload["labels"] == {"job_kind": "seed", "segment": "0"}
