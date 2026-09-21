#!/usr/bin/env python3
"""Experiment: generate a 3s clip, then try to extend it to 6s.

`extend` is the one capability the whole 30-second design rests on, and it has
never been exercised against the live API. This script answers the questions that
matter, one paid call each:

  1. Does a 3s T2V render come back with a usable file? (known: yes)
  2. Does `previous_interaction_id` + `task: extend` work without a video input?
  3. **What does the extension actually return** — a 6s film, or only the new 3s?

That third question is the important one. §24.3 forbids assembling the deliverable
by splicing independent clips, so if `extend` returns only the appended portion,
the pipeline's assumptions are wrong and the plan needs to change, not the code.

The script prints what it observed and saves both files plus the raw exchange. It
does not retry a timeout, and it does not paper over a failure.

    RUN_LIVE_VIDEO_TESTS=1 python scripts/experiment_seed_and_extend.py \
        --project my-project --out ./outputs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omni_homevlog.errors import OmniVlogError, RequestTimeoutUnknownOutcome
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.observability.logging import configure_logging, get_logger
from omni_homevlog.observability.redaction import redact
from omni_homevlog.providers.request_builder import build_create_payload
from omni_homevlog.providers.transport import payload_preview
from omni_homevlog.storage.local import atomic_write_bytes

logger = get_logger("experiment")

#: The prompt from the handoff's verified smoke test, kept verbatim so this run is
#: comparable to that one.
SEED_PROMPT = "A calm ocean sunrise, realistic handheld footage."

EXTEND_PROMPT = (
    "Continue directly from the exact final moment of the previous video. "
    "The camera keeps drifting slowly over the same calm ocean at the same time "
    "of day. Do not reintroduce the scene, do not cut, and do not change the "
    "lighting or the water."
)

DURATION_S = 3
RESOLUTION = "360p"
ASPECT = "16:9"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate 3s, then try to extend to 6s.")
    p.add_argument("--project", required=True)
    p.add_argument("--location", default="global")
    p.add_argument("--model", default="gemini-omni-1.1-flash-preview")
    p.add_argument("--out", type=Path, default=Path("./outputs"))
    p.add_argument(
        "--strategy",
        choices=["previous_interaction_id", "video_input", "both"],
        default="previous_interaction_id",
        help="How to tell the provider what to continue from.",
    )
    p.add_argument(
        "--gcs-uri",
        help="gs:// prefix, needed for the video_input strategy (the seed must be "
        "reachable by the provider).",
    )
    return p.parse_args()


def require_opt_in() -> None:
    if (os.environ.get("RUN_LIVE_VIDEO_TESTS") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(
            "refusing to run: this makes real, billable API calls (two of them).\n"
            "Set RUN_LIVE_VIDEO_TESTS=1 if that is what you intend.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def save_video(envelope, target: Path, gcs=None) -> Path | None:
    """Write the returned video to disk, from whichever delivery mode was used."""
    video = envelope.video if envelope else None
    if video is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    if video.is_inline:
        atomic_write_bytes(target, video.decode())
        return target
    if video.uri and gcs is not None:
        gcs.download_to(video.uri, target)
        return target
    return None


def describe(label: str, envelope, path: Path | None) -> dict:
    """Collect everything needed to judge what actually came back."""
    info = inspect_media(path) if path and path.is_file() else None
    record = {
        "label": label,
        "interaction_id": envelope.interaction_id if envelope else None,
        "parent_interaction_id": envelope.previous_interaction_id if envelope else None,
        "status": envelope.status if envelope else None,
        "delivery": (
            "inline"
            if envelope and envelope.video and envelope.video.is_inline
            else ("uri" if envelope and envelope.video and envelope.video.uri else "none")
        ),
        "video_uri": envelope.video.uri if envelope and envelope.video else None,
        "usage": envelope.usage if envelope else {},
        "file": str(path) if path else None,
        "size_bytes": info.size_bytes if info else None,
        "duration_s": info.duration_s if info else None,
        "dimensions": f"{info.width}x{info.height}" if info and info.width else None,
        "video_codec": info.video_codec if info else None,
        "audio_codec": info.audio_codec if info else None,
        "media_error": info.error if info else None,
        "step_types": [str(s.get("type")) for s in (envelope.steps if envelope else [])],
    }
    print(f"\n{label}")
    print(f"  interaction   {record['interaction_id']}")
    print(f"  parent        {record['parent_interaction_id']}")
    print(f"  status        {record['status']}")
    print(f"  delivery      {record['delivery']}")
    if info:
        print(f"  file          {record['file']}")
        print(f"  bytes         {record['size_bytes']}")
        print(f"  duration      {record['duration_s']}s")
        print(f"  picture       {record['dimensions']}  {record['video_codec']}")
        print(f"  audio         {record['audio_codec'] or 'none'}")
    print(f"  usage         {record['usage']}")
    return record


def main() -> int:
    args = parse_args()
    configure_logging(os.environ.get("OMNI_LOG_LEVEL", "INFO"))
    require_opt_in()

    from omni_homevlog.providers.factory import build_provider

    binding = build_provider(
        provider_name="vertex",
        project=args.project,
        model=args.model,
        location=args.location,
        with_gcs=bool(args.gcs_uri),
    )
    provider = binding.provider
    transport = provider.transport
    out: Path = args.out.expanduser()

    print(f"provider  {binding.provider_name}  project={args.project}")
    print(f"model     {binding.model}")
    print(f"plan      3s {RESOLUTION} {ASPECT}  ->  extend to 6s  (2 paid calls)")
    print(f"strategy  {args.strategy}\n")

    # ─ step 1: the seed ───────────────────────────────────────────────
    seed_payload = build_create_payload(
        model=binding.model,
        task="text_to_video",
        prompt=SEED_PROMPT,
        aspect_ratio=ASPECT,
        resolution=RESOLUTION,
        duration_s=DURATION_S,
    )
    print(f"seed request  {payload_preview(seed_payload)}")

    try:
        seed_result = transport.create_interaction(seed_payload)
    except RequestTimeoutUnknownOutcome as exc:
        print(f"\nSEED TIMEOUT: {exc.message}", file=sys.stderr)
        print("Not re-issued. Check the console.", file=sys.stderr)
        return 3
    except OmniVlogError as exc:
        print(f"\nSEED ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(f"hint: {exc.remediation}", file=sys.stderr)
        return 1

    seed_env = seed_result.envelope
    seed_path = save_video(seed_env, out / "seed_3s.mp4", provider.gcs)
    if seed_path is None:
        print(
            f"\nThe seed returned no fetchable video "
            f"(delivery={describe('seed', seed_env, None)['delivery']}). "
            "Nothing to extend. Set --gcs-uri so URI delivery can be downloaded.",
            file=sys.stderr,
        )
        return 1

    seed_record = describe("SEED (3s, text_to_video)", seed_env, seed_path)

    # ── step 2: the extension ───────────────────────────────────────────
    input_video_uri = None
    if args.strategy in ("video_input", "both"):
        if not args.gcs_uri:
            print(
                "\n--strategy video_input needs --gcs-uri: the seed was delivered "
                "inline, so the provider has no URI to read it from.",
                file=sys.stderr,
            )
            return 2
        target_uri = f"{args.gcs_uri.rstrip('/')}/experiment/{seed_env.interaction_id}.mp4"
        input_video_uri = provider.gcs.upload_file(seed_path, target_uri, content_type="video/mp4")
        print(f"\nuploaded seed for the provider to read: {input_video_uri}")

    extend_payload = build_create_payload(
        model=binding.model,
        task="extend",
        prompt=EXTEND_PROMPT,
        aspect_ratio=ASPECT,
        resolution=RESOLUTION,
        duration_s=DURATION_S,
        input_video_uri=input_video_uri,
        previous_interaction_id=(
            seed_env.interaction_id
            if args.strategy in ("previous_interaction_id", "both")
            else None
        ),
    )
    print(f"\nextend request  {payload_preview(extend_payload)}")

    try:
        ext_result = transport.create_interaction(extend_payload)
    except RequestTimeoutUnknownOutcome as exc:
        print(f"\nEXTEND TIMEOUT: {exc.message}", file=sys.stderr)
        print(
            "Not re-issued: the generation may exist and may be billable.\n"
            f"The seed is safe at {seed_path}",
            file=sys.stderr,
        )
        return 3
    except OmniVlogError as exc:
        print(f"\nEXTEND FAILED [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(f"hint: {exc.remediation}", file=sys.stderr)
        print(f"detail: {json.dumps(redact(exc.detail), indent=2)}", file=sys.stderr)
        print(f"\nThe seed is still usable: {seed_path}", file=sys.stderr)
        _write_report(out, args, seed_record, None, error=exc.message)
        return 4

    ext_env = ext_result.envelope
    ext_path = save_video(ext_env, out / "extended_6s.mp4", provider.gcs)
    ext_record = describe("EXTENSION (asked for 3 more seconds)", ext_env, ext_path)

    # ── the verdict ─────────────────────────────────────────────────────
    print("\n" + "=" * 68)
    seed_len = seed_record["duration_s"]
    ext_len = ext_record["duration_s"]

    if ext_len is None:
        verdict = "The extension returned no usable file."
    elif seed_len and ext_len > seed_len + 1.0:
        verdict = (
            f"The extension returned the WHOLE film ({ext_len}s), not just the new "
            "part. Native chaining works as the design assumes."
        )
    elif seed_len and abs(ext_len - seed_len) < 1.0:
        verdict = (
            f"The extension returned only ~{ext_len}s, the same length as the seed. "
            "It did NOT grow the film. Either it appended only the new portion "
            "(which §24.3 forbids splicing to reassemble), or it ignored the parent "
            "and produced an independent clip. Both would break the 30-second design."
        )
    else:
        verdict = f"Unexpected: seed {seed_len}s, extension {ext_len}s."

    print("VERDICT")
    print(f"  {verdict}")
    linked = ext_record["parent_interaction_id"] == seed_record["interaction_id"]
    print(f"  parent link recorded: {linked} ({ext_record['parent_interaction_id']})")
    print("=" * 68)

    _write_report(
        out,
        args,
        seed_record,
        ext_record,
        verdict=verdict,
        linked=linked,
    )
    print(f"\nartifacts under {out}/")
    return 0


def _write_report(out: Path, args, seed: dict, ext: dict | None, **extra) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "settings": {
            "model": args.model,
            "seed_duration_s": DURATION_S,
            "resolution": RESOLUTION,
            "aspect_ratio": ASPECT,
            "extend_strategy": args.strategy,
        },
        "seed": seed,
        "extension": ext,
        **extra,
    }
    (out / "seed_and_extend_report.json").write_text(
        json.dumps(redact(payload), indent=2, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
