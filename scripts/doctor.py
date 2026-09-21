#!/usr/bin/env python3
"""Standalone wrapper around the same capability probe `omni-vlog doctor` runs.

This file is deliberately thin. It contains no probe logic of its own: it builds a
provider through `providers.factory.build_provider`, runs
`providers.capability_probe.build_probe_report`, and prints the §15.1 layout with
`render_report`. The report is written from the same results via
`render_capability_markdown`, so what this script produces and what the CLI produces
cannot drift apart.

Why a script exists at all: `python scripts/doctor.py` works from a checkout where the
package was never installed, which is the state a reviewer is usually in.

Usage:

    python scripts/doctor.py --provider vertex --project "$GOOGLE_CLOUD_PROJECT"
    RUN_LIVE_VIDEO_TESTS=1 python scripts/doctor.py --provider vertex --run-generation

Free checks run by default. `--run-generation` spends money and is refused unless
`RUN_LIVE_VIDEO_TESTS=1` is set, exactly as `omni-vlog doctor` refuses it.

Exit codes: 0 when no probe row FAILED, 2 when one did, 1 for a configuration or
provider error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

#: Probe budget. Same numbers as `cli.doctor`: enough for the documented cheap matrix,
#: small enough that a mistake is uninteresting on a bill.
PROBE_MAX_CALLS = 4
PROBE_MAX_VIDEO_SECONDS = 12


def ensure_package_importable() -> None:
    """Import `omni_homevlog`, falling back to this checkout's `src/` directory."""
    try:
        import omni_homevlog  # noqa: F401
    except ImportError:
        if not SRC_DIR.is_dir():
            raise SystemExit(
                "omni_homevlog is not installed and src/ was not found next to this "
                f"script (looked in {SRC_DIR})."
            ) from None
        sys.path.insert(0, str(SRC_DIR))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doctor.py",
        description=(
            "Probe what the selected Gemini Omni surface can actually do, and write "
            "CAPABILITY_REPORT.md from the observed results."
        ),
    )
    parser.add_argument(
        "--provider",
        default="vertex",
        choices=("vertex", "gemini_api"),
        help="Which surface to probe (default: vertex).",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Google Cloud project id. Falls back to GOOGLE_CLOUD_PROJECT, then to ADC.",
    )
    parser.add_argument(
        "--location",
        default="global",
        help="Vertex location (default: global).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the video model id for this probe only.",
    )
    parser.add_argument(
        "--run-generation",
        action="store_true",
        help=("Run PAID generation probes. Refused unless RUN_LIVE_VIDEO_TESTS=1 is also set."),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Where to write CAPABILITY_REPORT.md (default: ./CAPABILITY_REPORT.md).",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Also write the derived ProviderCapabilities record as JSON here.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ensure_package_importable()

    import os

    from omni_homevlog.budget import Budget
    from omni_homevlog.config import get_settings
    from omni_homevlog.errors import OmniVlogError
    from omni_homevlog.observability.logging import configure_logging
    from omni_homevlog.providers.capability_probe import (
        build_probe_report,
        render_capability_markdown,
        render_report,
    )
    from omni_homevlog.providers.factory import build_provider, check_provider_ready

    configure_logging(os.environ.get("OMNI_LOG_LEVEL", "INFO"))
    settings = get_settings()

    # Same gate as the CLI: the environment variable is the switch that says a human
    # intended to spend money.
    if args.run_generation and not settings.run_live_video_tests:
        print(
            "error: --run-generation makes real, billable API calls. Set "
            "RUN_LIVE_VIDEO_TESTS=1 as well if that is what you intend.",
            file=sys.stderr,
        )
        return 1

    try:
        binding = build_provider(
            provider_name=args.provider,
            project=args.project,
            model=args.model,
            location=args.location,
            settings=settings,
            with_gcs=False,
        )
    except OmniVlogError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.remediation:
            print(f"hint: {exc.remediation}", file=sys.stderr)
        return 1

    ready, message = check_provider_ready(binding)
    print(f"Provider {binding.provider_name}  {message}")
    if not ready and not args.run_generation:
        print("Pre-flight failed; probing what can still be probed.")

    budget = None
    if args.run_generation:
        budget = Budget(
            max_total_calls=PROBE_MAX_CALLS,
            max_video_seconds_requested=PROBE_MAX_VIDEO_SECONDS,
            max_seed_attempts=2,
            max_edit_attempts_per_segment=0,
            max_regenerations_per_segment=0,
        )
        print(
            "generation probes enabled: up to "
            f"{PROBE_MAX_CALLS} calls at the cheapest settings (3s, 360p, no people). "
            "Estimates are estimates; Cloud Billing is authoritative."
        )

    report = asyncio.run(
        build_probe_report(binding.provider, run_generation=args.run_generation, budget=budget)
    )

    print()
    print(render_report(report))
    print()

    report_path = args.output or Path("CAPABILITY_REPORT.md")
    report_path.write_text(render_capability_markdown(report), encoding="utf-8")
    print(f"wrote {report_path}")

    if args.json_out is not None and report.capabilities is not None:
        args.json_out.write_text(
            json.dumps(report.capabilities.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")

    if not report.ok:
        print(
            "One or more checks FAILED. Read CAPABILITY_REPORT.md before creating a job.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
