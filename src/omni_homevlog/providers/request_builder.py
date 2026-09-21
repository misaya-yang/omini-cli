"""Building Interactions request payloads.

Pure functions, no I/O — which is what makes the request-construction test cheap
and the payload shape reviewable in one place.

The verified Vertex shape (handoff §"Minimal Python authentication and request"):

    {
      "model": "gemini-omni-1.1-flash-preview",
      "input": [
        {"type": "text",  "text": "..."},
        {"type": "image", "uri": "gs://.../ref.png", "mime_type": "image/png"}
      ],
      "response_format": [
        {"type": "video", "aspect_ratio": "16:9", "resolution": "360p", "duration": "3s"}
      ],
      "generation_config": {"video_config": {"task": "text_to_video"}}
    }

Two details that bite if you assume otherwise:

* `response_format` is a **list**, even for a single output. The SDK's
  `VideoResponseFormat` model is a single object, so SDK-shaped code and
  REST-shaped code genuinely differ here.
* `delivery: "uri"` is only legal alongside `gcs_uri`; the API rejects the
  combination otherwise, and it rejects it *before* generating. We therefore
  omit `delivery` entirely unless a bucket is configured.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from omni_homevlog.errors import InvalidRequestError
from omni_homevlog.schemas import (
    AspectRatio,
    ReferenceAsset,
    Resolution,
    VideoTask,
)

ALLOWED_DURATIONS_S = tuple(range(3, 11))  # 3s..10s inclusive
ALLOWED_RESOLUTIONS: tuple[str, ...] = ("360p", "720p", "1080p", "4k")
ALLOWED_ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16")
ALLOWED_IMAGE_MIME = ("image/png", "image/jpeg", "image/webp", "image/heic", "image/heif")
ALLOWED_VIDEO_MIME = (
    "video/mp4",
    "video/mpeg",
    "video/mpg",
    "video/mov",
    "video/avi",
    "video/x-flv",
    "video/webm",
    "video/quicktime",
)

#: Roles that belong in a `reference_to_video` call, in a stable order so the
#: prompt's `<IMAGE_REF_n>@Imagen` indices line up with `input` positions.
REFERENCE_ROLE_ORDER = (
    "identity_closeup",
    "identity_body",
    "outfit",
    "environment",
)

#: Roles that mean "this is a frame, not a reference".
FRAME_ROLES = ("first_frame", "last_frame")
VIDEO_ROLES = ("motion_reference",)


def validate_duration(seconds: int) -> str:
    if seconds not in ALLOWED_DURATIONS_S:
        raise InvalidRequestError(
            f"duration must be an integer 3..10 seconds, got {seconds}",
            detail={"allowed": list(ALLOWED_DURATIONS_S)},
        )
    return f"{seconds}s"


def validate_resolution(resolution: str) -> str:
    if resolution not in ALLOWED_RESOLUTIONS:
        raise InvalidRequestError(
            f"resolution must be one of {ALLOWED_RESOLUTIONS}, got {resolution!r}"
        )
    return resolution


def validate_aspect_ratio(aspect_ratio: str) -> str:
    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        raise InvalidRequestError(
            f"aspect_ratio must be one of {ALLOWED_ASPECT_RATIOS}, got {aspect_ratio!r}"
        )
    return aspect_ratio


def validate_gcs_uri(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise InvalidRequestError(f"expected a gs:// URI, got {uri!r}", detail={"field": "uri"})
    return uri


def validate_mime(mime: str, *, kind: str) -> str:
    allowed = ALLOWED_IMAGE_MIME if kind == "image" else ALLOWED_VIDEO_MIME
    if mime not in allowed:
        # HEIC variants arrive with vendor spellings; normalise before rejecting.
        lowered = mime.lower()
        if kind == "image" and lowered.startswith("image/hei"):
            return mime
        raise InvalidRequestError(
            f"unsupported {kind} MIME type {mime!r}", detail={"allowed": list(allowed)}
        )
    return mime


def text_input(prompt: str) -> dict[str, Any]:
    return {"type": "text", "text": prompt}


def image_input(*, uri: str, mime_type: str = "image/png") -> dict[str, Any]:
    return {
        "type": "image",
        "uri": validate_gcs_uri(uri),
        "mime_type": validate_mime(mime_type, kind="image"),
    }


def image_input_inline(*, data_b64: str, mime_type: str) -> dict[str, Any]:
    """Inline image bytes.

    Used only when no GCS staging is available. The handoff notes that GCS is
    preferable for large media, and reference photos are small enough that
    inline is acceptable — but prefer a URI when one exists.
    """
    return {
        "type": "image",
        "data": data_b64,
        "mime_type": validate_mime(mime_type, kind="image"),
    }


def video_input(*, uri: str, mime_type: str = "video/mp4") -> dict[str, Any]:
    return {
        "type": "video",
        "uri": validate_gcs_uri(uri),
        "mime_type": validate_mime(mime_type, kind="video"),
    }


def video_response_format(
    *,
    aspect_ratio: str,
    resolution: str,
    duration_s: int,
    gcs_uri: str | None = None,
    prefer_uri_delivery: bool = True,
) -> list[dict[str, Any]]:
    """The `response_format` list.

    `delivery` is omitted unless a bucket is configured — `delivery: "uri"`
    without `gcs_uri` is rejected before generation.
    """
    entry: dict[str, Any] = {
        "type": "video",
        "aspect_ratio": validate_aspect_ratio(aspect_ratio),
        "resolution": validate_resolution(resolution),
        "duration": validate_duration(duration_s),
    }
    if gcs_uri and prefer_uri_delivery:
        entry["delivery"] = "uri"
        entry["gcs_uri"] = validate_gcs_uri(gcs_uri)
    return [entry]


def video_config(task: VideoTask) -> dict[str, Any]:
    return {"video_config": {"task": task}}


def build_create_payload(
    *,
    model: str,
    task: VideoTask,
    prompt: str,
    aspect_ratio: AspectRatio | str,
    resolution: Resolution | str,
    duration_s: int,
    gcs_uri: str | None = None,
    prefer_uri_delivery: bool = True,
    references: list[ReferenceAsset] | None = None,
    reference_uris: dict[str, str] | None = None,
    reference_inputs: list[dict[str, Any]] | None = None,
    input_video_uri: str | None = None,
    input_video_mime: str = "video/mp4",
    first_frame_uri: str | None = None,
    last_frame_uri: str | None = None,
    previous_interaction_id: str | None = None,
    background: bool | None = None,
    store: bool | None = None,
    labels: dict[str, str] | None = None,
    seed: int | None = None,
    system_instruction: str | None = None,
    extra_generation_config: dict[str, Any] | None = None,
    include_generation_config: bool = True,
) -> dict[str, Any]:
    """Assemble a complete `POST /interactions` body.

    `input` ordering is load-bearing. The Prompt Compiler emits
    `[# References <IMAGE_REF_0>@Image1 ...]` and the model binds those tokens to
    the image inputs *in order*, so this function sorts references by
    `REFERENCE_ROLE_ORDER` and the compiler asks it for the same order.

    `include_generation_config=False` drops `generation_config` entirely. That is
    needed for chaining on Vertex, where the two are mutually exclusive:

        invalid_request: previous_interaction_id is not allowed when video task is set.

    Verified live on 2026-09-21. With `generation_config` omitted and a
    `response_format` declaring `type: "video"`, a 3.008s seed chained to a 6.016s
    film, so the video task is inferred from `response_format` and the stateful
    continuation works. See `providers/vertex_enterprise.py` for where this is
    decided.
    """
    inputs: list[dict[str, Any]] = [text_input(prompt)]

    if reference_inputs is not None:
        # Already resolved by the provider: either gs:// URIs (after staging) or
        # inline base64. Ordering was decided by `order_references` upstream.
        inputs.extend(reference_inputs)
    elif references:
        ordered = order_references(references)
        uri_map = reference_uris or {}
        for asset in ordered:
            uri = uri_map.get(asset.id) or (
                asset.path_or_uri if asset.path_or_uri.startswith("gs://") else None
            )
            if uri is None:
                raise InvalidRequestError(
                    f"Reference {asset.id!r} has no gs:// URI. The provider stages local "
                    "references to GCS before dispatch; if you are calling this directly, "
                    "pass `reference_inputs` with resolved URIs or inline data.",
                    detail={"asset_id": asset.id, "role": asset.role},
                )
            inputs.append(image_input(uri=uri, mime_type=asset.mime_type or "image/png"))

    if first_frame_uri:
        inputs.append(image_input(uri=first_frame_uri, mime_type="image/png"))
    if last_frame_uri:
        inputs.append(image_input(uri=last_frame_uri, mime_type="image/png"))
    if input_video_uri:
        inputs.append(video_input(uri=input_video_uri, mime_type=input_video_mime))

    generation_config: dict[str, Any] = video_config(task)
    if seed is not None:
        generation_config["seed"] = seed
    if extra_generation_config:
        generation_config.update(extra_generation_config)

    payload: dict[str, Any] = {
        "model": model,
        "input": inputs,
        "response_format": video_response_format(
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            duration_s=duration_s,
            gcs_uri=gcs_uri,
            prefer_uri_delivery=prefer_uri_delivery,
        ),
    }
    if include_generation_config:
        payload["generation_config"] = generation_config

    if previous_interaction_id:
        payload["previous_interaction_id"] = previous_interaction_id
    if background is not None:
        payload["background"] = background
    if store is not None:
        payload["store"] = store
    if labels:
        payload["labels"] = labels
    if system_instruction is not None:
        # The plan and the model card both say Omni does not support this. We
        # only forward it when a caller explicitly sets it, and the capability
        # probe records whether the surface accepted or ignored it.
        payload["system_instruction"] = system_instruction

    return payload


def order_references(references: list[ReferenceAsset]) -> list[ReferenceAsset]:
    """Stable reference ordering.

    §9.1: character description must not drift between segments, and the prompt
    refers to images positionally — so the order has to be deterministic across
    every segment of a job.
    """
    rank = {role: i for i, role in enumerate(REFERENCE_ROLE_ORDER)}
    return sorted(
        references,
        key=lambda a: (rank.get(a.role, len(REFERENCE_ROLE_ORDER)), a.sha256, a.id),
    )


def reference_tokens(references: list[ReferenceAsset]) -> str:
    """The `[# References <IMAGE_REF_0>@Image1 ...]` header line.

    Indices match `order_references`, i.e. the order the images appear in `input`.
    """
    ordered = order_references(references)
    if not ordered:
        return ""
    parts = [f"<IMAGE_REF_{i}>@Image{i + 1}" for i in range(len(ordered))]
    return "[# References " + " ".join(parts) + "]"
