#!/usr/bin/env python3
"""Which extension mechanism actually works?

The first live attempt established that `previous_interaction_id` is rejected when
a video task is set:

    invalid_request: previous_interaction_id is not allowed when video task is set.

That invalidates strategy A from the plan's §8.3, which is the cheapest and the one
the design preferred. So the remaining candidates have to be tried, cheapest first,
stopping at the first one that works:

  1. `previous_interaction_id` with **no** `generation_config` at all
     — the error blames the video task, so removing it is worth one call.
  2. `task: extend` with the previous render supplied as **inline base64 video**
     — avoids needing a bucket, which nothing else here does.
  3. `task: extend` with the previous render as a **gs:// video input**
     — the documented form, but needs a bucket.

Stops as soon as one works, so the cost is one call when a path succeeds.

    RUN_LIVE_VIDEO_TESTS=1 python scripts/probe_extend_paths.py \
        --project my-project --seed outputs/seed_3s.mp4 \
        --seed-interaction ChAyMWZlMGRhMzYzZjliMDQzEAgaATAqBG1haW4
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omni_homevlog.errors import OmniVlogError, RequestTimeoutUnknownOutcome
from omni_homevlog.media.ffprobe import inspect_media
from omni_homevlog.observability.logging import configure_logging
from omni_homevlog.observability.redaction import redact
from omni_homevlog.storage.local import atomic_write_bytes

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
    p = argparse.ArgumentParser(description="Find a working extend path.")
    p.add_argument("--project", required=True)
    p.add_argument("--location", default="global")
    p.add_argument("--model", default="gemini-omni-1.1-flash-preview")
    p.add_argument("--seed", type=Path, required=True, help="Local seed MP4")
    p.add_argument("--seed-interaction", required=True, help="The seed's interaction id")
    p.add_argument("--gcs-uri", help="gs:// prefix, for path 3")
    p.add_argument("--out", type=Path, default=Path("./outputs"))
    return p.parse_args()


def require_opt_in() -> None:
    if (os.environ.get("RUN_LIVE_VIDEO_TESTS") or "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(
            "refusing to run: this makes real, billable API calls.\n"
            "It stops at the first path that works, so the cost is one call if any "
            "path succeeds.\nSet RUN_LIVE_VIDEO_TESTS=1 if that is what you intend.",
            file=sys.stderr,
        )
        raise SystemExit(2)


def base_payload(model: str) -> dict:
    return {
        "model": model,
        "input": [{"type": "text", "text": EXTEND_PROMPT}],
        "response_format": [
            {
                "type": "video",
                "aspect_ratio": ASPECT,
                "resolution": RESOLUTION,
                "duration": f"{DURATION_S}s",
            }
        ],
    }


def candidate_payloads(args, provider) -> list[tuple[str, str, dict]]:
    """(id, human description, payload) for each path, cheapest first."""
    paths: list[tuple[str, str, dict]] = []

    # 1. The path the production adapter now uses: previous_interaction_id, with
    #    generation_config dropped by `VertexEnterpriseProvider.build_payload`.
    #    Building it through the provider is the point — it proves the code that
    #    will actually run, not a hand-rolled payload that happens to match.
    from omni_homevlog.providers.base import RenderRequest

    request = RenderRequest(
        task="extend",
        prompt=EXTEND_PROMPT,
        segment_index=1,
        attempt_index=0,
        aspect_ratio=ASPECT,
        resolution=RESOLUTION,
        duration_s=DURATION_S,
        parent_interaction_id=args.seed_interaction,
    )
    paths.append(
        (
            "provider_continuation",
            "VertexEnterpriseProvider.build_payload with a parent interaction id "
            "(the production path)",
            provider.build_payload(request),
        )
    )

    # 2. Native extend, previous render supplied inline as base64. Avoids a bucket.
    blob = base64.b64encode(args.seed.read_bytes()).decode("ascii")
    p2 = base_payload(args.model)
    p2["input"].append({"type": "video", "data": blob, "mime_type": "video/mp4"})
    p2["generation_config"] = {"video_config": {"task": "extend"}}
    paths.append(
        (
            "extend_inline_video",
            "task=extend with the previous render as inline base64 video",
            p2,
        )
    )

    # 3. Native extend, previous render as a gs:// video input. The documented form.
    if args.gcs_uri:
        target = f"{args.gcs_uri.rstrip('/')}/experiment/{args.seed_interaction}.mp4"
        uri = provider.gcs.upload_file(args.seed, target, content_type="video/mp4")
        p3 = base_payload(args.model)
        p3["input"].append({"type": "video", "uri": uri, "mime_type": "video/mp4"})
        p3["generation_config"] = {"video_config": {"task": "extend"}}
        paths.append(("extend_gcs_video", f"task=extend with a gs:// video input ({uri})", p3))

    return paths


def main() -> int:
    args = parse_args()
    configure_logging(os.environ.get("OMNI_LOG_LEVEL", "INFO"))
    require_opt_in()

    if not args.seed.is_file():
        print(f"seed not found: {args.seed}", file=sys.stderr)
        return 2

    from omni_homevlog.providers.factory import build_provider

    binding = build_provider(
        provider_name="vertex",
        project=args.project,
        model=args.model,
        location=args.location,
        with_gcs=bool(args.gcs_uri),
    )
    provider, transport = binding.provider, binding.provider.transport

    seed_info = inspect_media(args.seed)
    print(f"seed          {args.seed}")
    print(f"              {seed_info.duration_s}s {seed_info.width}x{seed_info.height}")
    print(f"interaction   {args.seed_interaction}")
    print(f"model         {binding.model}\n")

    attempts: list[dict] = []
    winner: str | None = None

    for path_id, description, payload in candidate_payloads(args, provider):
        print("-" * 68)
        print(f"TRYING  {path_id}")
        print(f"        {description}")

        # The inline path carries ~550 KB of base64; keep the log readable.
        summary = {
            "input_types": [i.get("type") for i in payload["input"]],
            "has_generation_config": "generation_config" in payload,
            "previous_interaction_id": payload.get("previous_interaction_id"),
            "video_input_form": next(
                ("uri" if "uri" in i else "data" if "data" in i else "none")
                for i in payload["input"]
                if i.get("type") == "video"
            )
            if any(i.get("type") == "video" for i in payload["input"])
            else None,
        }
        print(f"        {json.dumps(summary)}")

        try:
            result = transport.create_interaction(payload)
        except RequestTimeoutUnknownOutcome as exc:
            print(f"  TIMEOUT: {exc.message}")
            print("  Not re-issued. Stopping here: a later path would spend again.")
            attempts.append({"path": path_id, "outcome": "timeout", "detail": exc.message})
            break
        except OmniVlogError as exc:
            print(f"  REJECTED [{exc.code}]: {exc.message}")
            attempts.append(
                {
                    "path": path_id,
                    "outcome": "rejected",
                    "error_code": exc.code,
                    "detail": exc.message,
                    "http_status": getattr(exc, "http_status", None),
                }
            )
            continue

        envelope = result.envelope
        target = args.out / f"extended_via_{path_id}.mp4"
        saved = None
        if envelope.video and envelope.video.is_inline:
            atomic_write_bytes(target, envelope.video.decode())
            saved = target
        elif envelope.video and envelope.video.uri and provider.gcs:
            provider.gcs.download_to(envelope.video.uri, target)
            saved = target

        info = inspect_media(saved) if saved else None
        print("  ACCEPTED")
        print(f"    interaction  {envelope.interaction_id}")
        print(f"    parent       {envelope.previous_interaction_id}")
        print(f"    status       {envelope.status}")
        if info:
            print(f"    duration     {info.duration_s}s")
            print(f"    picture      {info.width}x{info.height} {info.video_codec}")
            print(f"    file         {saved}")

        attempts.append(
            {
                "path": path_id,
                "outcome": "accepted",
                "interaction_id": envelope.interaction_id,
                "parent_interaction_id": envelope.previous_interaction_id,
                "status": envelope.status,
                "duration_s": info.duration_s if info else None,
                "dimensions": f"{info.width}x{info.height}" if info and info.width else None,
                "file": str(saved) if saved else None,
                "usage": envelope.usage,
            }
        )
        winner = path_id
        break

    print("=" * 68)
    if winner:
        print(f"WORKING PATH: {winner}")
        entry = next(a for a in attempts if a["path"] == winner)
        seed_len = seed_info.duration_s
        ext_len = entry.get("duration_s")
        if seed_len and ext_len:
            if ext_len > seed_len + 1.0:
                print(
                    f"The extension returned {ext_len}s for a {seed_len}s seed, so it "
                    "returns the WHOLE film rather than only the appended part. That "
                    "is what the pipeline assumes."
                )
            elif abs(ext_len - seed_len) < 1.0:
                print(
                    f"The extension returned {ext_len}s, the same as the seed, so it "
                    "did NOT grow the film. §24.3 forbids splicing to reassemble, so "
                    "this breaks the 30-second design and the plan needs revisiting."
                )
    else:
        print("NO WORKING PATH among those tried.")
        print(
            "If the inline-video path was rejected too, a gs:// video input is the "
            "only remaining candidate and it needs an approved bucket."
        )
    print("=" * 68)

    args.out.mkdir(parents=True, exist_ok=True)
    report = args.out / "extend_paths_report.json"
    report.write_text(
        json.dumps(
            redact({"seed": str(args.seed), "attempts": attempts, "winner": winner}),
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nreport written to {report}")
    return 0 if winner else 1


if __name__ == "__main__":
    raise SystemExit(main())
