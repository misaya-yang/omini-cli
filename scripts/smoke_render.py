#!/usr/bin/env python3
"""One cheap live render, end to end, with verification (§21.3).

This is the smallest thing that proves the paid path works: it makes **one**
request at the cheapest settings the surface allows, writes the bytes to disk, and
verifies them with the same media probe the pipeline uses.

    python scripts/smoke_render.py --project my-project
    python scripts/smoke_render.py --project my-project --duration 5 --resolution 720p

Differences from `omni-vlog doctor --run-generation`:

* `doctor` runs the whole capability matrix, which is several calls. This runs one.
* `doctor` reports on the API. This reports on the *file*: duration, dimensions,
  codec, audio track, and whether a C2PA manifest is attached.

Both refuse to spend without `RUN_LIVE_VIDEO_TESTS=1`, and neither retries a
timeout, because a timed-out generation may already be running and billable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omni_homevlog.errors import OmniVlogError, RequestTimeoutUnknownOutcome
from omni_homevlog.media.c2pa import inspect_content_credentials
from omni_homevlog.media.ffprobe import assert_expected_media, inspect_media
from omni_homevlog.observability.logging import configure_logging
from omni_homevlog.providers.capability_probe import probe_generation
from omni_homevlog.providers.factory import build_provider, check_provider_ready
from omni_homevlog.storage.local import LocalStore, atomic_write_bytes

#: §21.3: the cheapest useful request. No people, so the smoke test stays clear of
#: the identity and consent questions the plan is careful about.
SMOKE_PROMPT = (
    "A calm static shot of an empty wooden table beside a window in the morning. "
    "Soft daylight, gentle handheld drift, no people, no text."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one cheap live video render and verify the output."
    )
    parser.add_argument("--provider", choices=["vertex", "gemini_api"], default="vertex")
    parser.add_argument("--project", help="Google Cloud project id (vertex only)")
    parser.add_argument("--location", default="global")
    parser.add_argument("--model", help="Override the video model id")
    parser.add_argument("--duration", type=int, default=3, help="Seconds, 3..10")
    parser.add_argument("--resolution", default="360p", choices=["360p", "720p", "1080p", "4k"])
    parser.add_argument("--aspect", default="16:9", choices=["16:9", "9:16"])
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to save the MP4. Defaults to ./.omni-vlog/smoke/<timestamp>.mp4",
    )
    parser.add_argument(
        "--task",
        default="text_to_video",
        choices=["text_to_video", "reference_to_video"],
        help="Which task to smoke. reference_to_video needs --reference.",
    )
    parser.add_argument("--reference", type=Path, help="Reference image for reference_to_video")
    return parser.parse_args()


def require_opt_in() -> None:
    raw = (os.environ.get("RUN_LIVE_VIDEO_TESTS") or "").strip().lower()
    if raw not in {"1", "true", "yes", "on"}:
        print(
            "refusing to run: this makes a real, billable API call.\n"
            "Set RUN_LIVE_VIDEO_TESTS=1 if that is what you intend.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def main() -> int:
    args = parse_args()
    configure_logging(os.environ.get("OMNI_LOG_LEVEL", "INFO"))
    require_opt_in()

    if not 3 <= args.duration <= 10:
        print(f"--duration must be an integer 3..10, got {args.duration}", file=sys.stderr)
        return 2

    store = LocalStore(Path(os.environ.get("OMNI_DATA_DIR", "./.omni-vlog"))).ensure()
    paths = store.job("job-smoke-render").ensure()

    try:
        binding = build_provider(
            provider_name=args.provider,
            project=args.project,
            model=args.model,
            location=args.location,
            paths=paths,
            with_gcs=bool(os.environ.get("OMNI_OUTPUT_GCS_URI")),
        )
    except OmniVlogError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(f"hint:  {exc.remediation}", file=sys.stderr)
        return 1

    ready, message = check_provider_ready(binding)
    print(f"provider  {binding.provider_name}")
    print(f"model     {binding.model}")
    print(f"status    {message}")
    if not ready:
        return 1

    print(
        f"\nrequesting {args.duration}s at {args.resolution} {args.aspect} "
        f"({args.task}). One call. No retry on timeout.\n"
    )

    try:
        result, envelope = asyncio.run(
            probe_generation(
                binding.provider,
                task=args.task,
                duration_s=args.duration,
                resolution=args.resolution,
                # `str(args.reference)` on a None is the literal string "None", which
                # the provider would have tried to read as a video URI. Guard the
                # type rather than relying on the task name.
                include_video_input=(
                    str(args.reference)
                    if args.task == "reference_to_video" and args.reference
                    else None
                ),
                label=f"smoke {args.task}",
            )
        )
    except RequestTimeoutUnknownOutcome as exc:
        # Deliberately not retried: the generation may exist and may be billable.
        print(f"\nTIMEOUT: {exc.message}", file=sys.stderr)
        print(
            "The request was NOT re-issued. Check the provider console for a recent "
            "generation before trying again.",
            file=sys.stderr,
        )
        return 3
    except OmniVlogError as exc:
        print(f"\nerror: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(f"hint:  {exc.remediation}", file=sys.stderr)
        return 1

    print(f"status    {result.status}")
    print(f"detail    {result.detail}")
    if envelope is not None:
        print(f"interaction {envelope.interaction_id}")
        print(f"usage     {envelope.usage}")

    if result.status.value != "PASS" or envelope is None:
        print("\nThe render did not pass, so there are no bytes to verify.", file=sys.stderr)
        return 1

    video = envelope.video
    if video is None:
        print("\nNo video content in the response.", file=sys.stderr)
        return 1

    if args.output:
        target = args.output.expanduser()
    else:
        from omni_homevlog.schemas import utc_now_iso

        target = paths.debug_dir / f"smoke-{utc_now_iso().replace(':', '')}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)

    if video.is_inline:
        atomic_write_bytes(target, video.decode())
    elif video.uri and binding.provider.gcs is not None:
        binding.provider.gcs.download_to(video.uri, target)
    else:
        print(
            f"\nDelivered by URI ({video.uri}) but no GCS client is available to fetch "
            "it. Set OMNI_OUTPUT_GCS_URI, or install google-cloud-storage.",
            file=sys.stderr,
        )
        return 1

    # ── verification: an HTTP 200 is not an output ───────────────────────
    info = inspect_media(target)
    print(f"\nfile      {target}")
    print(f"bytes     {info.size_bytes}")
    print(f"container {info.container}  probed with {info.probed_with}")
    print(f"duration  {info.duration_s}s")
    print(f"picture   {info.width}x{info.height}  {info.video_codec}")
    print(f"audio     {info.audio_codec or 'none'}")

    credentials = inspect_content_credentials(target, provider=binding.provider_name)
    print(f"c2pa      {credentials.c2pa_present}  {credentials.c2pa_detail}")
    print(f"synthid   expected={credentials.synthid_expected} (not verifiable locally)")

    ok, problems = assert_expected_media(info, expected_duration_s=args.duration)
    if not ok:
        print("\nVERIFICATION PROBLEMS:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 4

    print("\nPASS: the rendered file matches what was requested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
