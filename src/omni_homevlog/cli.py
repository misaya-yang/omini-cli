"""Command line interface (§15).

    omni-vlog doctor       capability probe (Phase 0 hard gate)
    omni-vlog check-references   sanitize references before spending
    omni-vlog create       create a job
    omni-vlog run          drive a job forward
    omni-vlog status       inspect
    omni-vlog resume       continue, resolving recovery cases first
    omni-vlog review       show the review reports
    omni-vlog approve      clear a human gate
    omni-vlog retry        re-run one stage with a chosen mode
    omni-vlog export       copy the deliverable out
    omni-vlog delete       remove local + GCS + DB metadata
    omni-vlog projects     list visible projects (discovery only)
    omni-vlog metrics      aggregate the ledger

Every command that could spend money says so before it does, and `--dry-run` is
available on the ones where that is meaningful.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from omni_homevlog import __version__
from omni_homevlog.config import get_settings, load_thresholds
from omni_homevlog.errors import OmniVlogError
from omni_homevlog.observability.logging import configure_logging, get_logger
from omni_homevlog.storage.locking import locked_command

app = typer.Typer(
    name="omni-vlog",
    help="Continuity-preserving home-vlog agent on top of Gemini Omni.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
logger = get_logger("cli")

#: Default human-gate configuration when `--human-gate` is not passed.
DEFAULT_GATES = "high-res"


def _fail(message: str, *, code: int = 1) -> NoReturn:
    """Print an error and exit.

    Annotated `NoReturn` so callers that use it in an `except` branch do not leave
    the following code looking reachable-but-unbound to a type checker.
    """
    console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"omni-vlog {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True)
    ] = False,
    log_level: Annotated[str | None, typer.Option("--log-level", help="DEBUG..ERROR")] = None,
    log_json: Annotated[bool, typer.Option("--log-json", help="Emit JSON logs")] = False,
) -> None:
    """Continuity-preserving home-vlog agent."""
    _ = version
    configure_logging(log_level, json_output=log_json or None)


# ─────────────────────────────────────────────────────────────────────────────
# doctor (§15.1, §16)
# ─────────────────────────────────────────────────────────────────────────────


@app.command()
def doctor(
    checks: Annotated[
        str, typer.Option(help="Comma-separated seed,extend,third,edit")
    ] = "seed,extend,third,edit",
    max_calls: Annotated[int, typer.Option(min=1, max=8, help="Maximum paid video calls")] = 4,
    reference: Annotated[
        Path | None, typer.Option(help="Clean reference for reference-to-video probe")
    ] = None,
    provider: Annotated[str, typer.Option(help="vertex or gemini_api")] = "vertex",
    project: Annotated[str | None, typer.Option(help="Google Cloud project id")] = None,
    location: Annotated[str, typer.Option(help="Vertex location")] = "global",
    model: Annotated[str | None, typer.Option(help="Override the video model id")] = None,
    run_generation: Annotated[
        bool,
        typer.Option(
            "--run-generation",
            help="Run PAID generation probes. Requires RUN_LIVE_VIDEO_TESTS=1.",
        ),
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all-projects", help="Also list visible projects (discovery only)")
    ] = False,
    output: Annotated[Path | None, typer.Option(help="Where to write CAPABILITY_REPORT.md")] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json-out", help="Write capabilities JSON")
    ] = None,
    background: Annotated[bool, typer.Option(help="Probe acknowledged async generation and bounded GET polling")] = False,
    seed_mode: Annotated[str, typer.Option(help="auto | text | reference | image | first-last")] = "auto",
    last_frame: Annotated[Path | None, typer.Option(help="Ending frame for first-last mode")] = None,
    chain_strategy: Annotated[str, typer.Option(help="previous | steps | source")] = "previous",
    duration: Annotated[int, typer.Option(min=3, max=10, help="Seconds per seed/extension")] = 3,
    gcs_uri: Annotated[str | None, typer.Option(help="Optional private gs:// output prefix")] = None,
    recover_from: Annotated[Path | None, typer.Option(help="Saved capabilities JSON to recover by GET only (no new generation)")] = None,
) -> None:
    """Probe what the selected surface can actually do.

    Free checks run by default. `--run-generation` costs money and is refused
    unless `RUN_LIVE_VIDEO_TESTS=1` is also set, so a stray invocation cannot
    spend anything.
    """
    import asyncio

    from omni_homevlog.budget import Budget
    from omni_homevlog.providers.capability_probe import (
        build_probe_report,
        render_capability_markdown,
        render_report,
    )
    from omni_homevlog.providers.factory import build_provider, check_provider_ready
    from omni_homevlog.storage.local import LocalStore

    settings = get_settings()
    saved_probe = None
    if recover_from is not None:
        if run_generation:
            _fail("--recover-from is read-only and cannot be combined with --run-generation.")
        from omni_homevlog.schemas import ProviderCapabilities
        saved_probe = ProviderCapabilities.model_validate_json(recover_from.read_text())
        provider, project, model = saved_probe.provider, saved_probe.project, saved_probe.model
        location = saved_probe.location or "global"
    if seed_mode not in ("auto", "text", "reference", "image", "first-last") or chain_strategy not in ("previous", "steps", "source"):
        _fail("Invalid seed mode or chain strategy.")
    if seed_mode in ("reference", "image", "first-last") and reference is None:
        _fail("This seed mode requires --reference.")
    if (seed_mode == "first-last") != (last_frame is not None):
        _fail("--last-frame is required exactly for first-last mode.")
    if seed_mode == "text" and reference is not None:
        _fail("Text mode cannot include reference images.")
    if reference or last_frame:
        from PIL import Image
        for path in (reference, last_frame):
            if path is not None:
                try:
                    with Image.open(path) as img:
                        img.verify()
                except (OSError, ValueError) as exc:
                    _fail(f"Unreadable reference image: {path.name}: {exc}")

    if run_generation and not settings.run_live_video_tests:
        _fail(
            "--run-generation makes real, billable API calls. Set "
            "RUN_LIVE_VIDEO_TESTS=1 as well if that is what you intend.",
        )

    # A scratch job directory, so a generation probe can download its output and
    # verify it. Without one, `provider.paths` is None, the probe skips media
    # verification entirely, and a PASS only means "the request was accepted" —
    # which is exactly the HTTP-200-is-not-an-output trap the handoff warns about.
    from omni_homevlog.storage.local import make_job_id

    probe_root = LocalStore(settings.data_dir()).ensure()
    probe_paths = (
        probe_root.job(make_job_id()).ensure()
        if run_generation or recover_from is not None
        else probe_root.job("job-doctor-probe").ensure()
    )
    capability_db_path = probe_root.job("job-doctor-probe").ensure().db_path

    try:
        binding = build_provider(
            provider_name=provider,  # type: ignore[arg-type]
            project=project,
            model=model,
            location=location,
            paths=probe_paths,
            settings=settings,
            with_gcs=bool(gcs_uri),
            gcs_prefix=gcs_uri,
        )
    except OmniVlogError as exc:
        _fail(str(exc))

    ready, message = check_provider_ready(binding)
    console.print(f"[bold]Provider[/bold] {binding.provider_name}  {message}")

    if not ready and not run_generation:
        console.print("[yellow]Pre-flight failed; probing what can still be probed.[/yellow]")

    selected = tuple(dict.fromkeys(c.strip() for c in checks.split(",") if c.strip()))
    if (
        not selected
        or selected[0] != "seed"
        or any(c not in ("seed", "extend", "third", "edit") for c in selected)
    ):
        _fail("Checks must begin with seed and contain only seed,extend,third,edit.")
    if "third" in selected and ("extend" not in selected or selected.index("third") < selected.index("extend")):
        _fail("third requires an earlier extend check.")
    if reference is not None and not reference.is_file():
        _fail("Reference file does not exist.")
    budget = None
    if run_generation:
        # A probe budget, deliberately tiny: 4 calls and 12 video-seconds is
        # enough for the documented matrix and small enough to be uninteresting
        # on a bill.
        budget = Budget(
            max_total_calls=max_calls,
            max_video_seconds_requested=max_calls * max(10, duration * 3),
            max_seed_attempts=2,
            max_edit_attempts_per_segment=0,
            max_regenerations_per_segment=0,
        )
        console.print(
            Panel(
                f"Running PAID generation probes: up to {max_calls} calls at the cheapest "
                "settings (3s seed/appends, source-length edit, 360p). "
                "Pricing is an estimate when configured; Cloud Billing is authoritative.",
                title="generation probes enabled",
                border_style="yellow",
            )
        )

    if saved_probe is not None:
        from omni_homevlog.providers.capability_probe import recover_probe_report
        report = asyncio.run(recover_probe_report(binding.provider, saved_probe))
    else:
        report = asyncio.run(
            build_probe_report(
                binding.provider,
                run_generation=run_generation,
                background=background,
                budget=budget,
                checks=selected,
                seed_mode=seed_mode,
                last_frame_path=str(last_frame) if last_frame else None,
                chain_strategy=chain_strategy,
                duration_s=duration,
                reference_path=str(reference) if reference else None,
            )
        )
    if report.capabilities is not None:
        from omni_homevlog.storage.database import Database

        report.capabilities.location = binding.location
        # Persist a merged copy; this report still describes only this run.
        Database(capability_db_path).save_probe(report.capabilities.model_copy(deep=True))

    console.print()
    console.print(render_report(report))
    console.print()

    if all_projects:
        from omni_homevlog.providers.factory import list_available_projects

        projects = list_available_projects(settings=settings)
        if projects:
            console.print("[bold]Visible projects[/bold] (discovery only; never auto-selected)")
            for entry in projects:
                console.print(f"  {entry['project']}  ({entry['note']})")

    report_path = output or Path("CAPABILITY_REPORT.md")
    report_path.write_text(render_capability_markdown(report), encoding="utf-8")
    console.print(f"wrote {report_path}")

    if json_out and report.capabilities is not None:
        json_out.write_text(
            json.dumps(report.capabilities.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        console.print(f"wrote {json_out}")

    if not report.ok:
        console.print(
            "[yellow]One or more checks FAILED. Read CAPABILITY_REPORT.md before "
            "creating a job.[/yellow]"
        )
        raise typer.Exit(2)


# ─────────────────────────────────────────────────────────────────────────────
# check-references
# ─────────────────────────────────────────────────────────────────────────────


@app.command("check-references")
def check_references(
    reference: Annotated[
        list[Path], typer.Option("--reference", "-r", help="Reference image path")
    ],
    role: Annotated[
        list[str] | None,
        typer.Option("--role", help="Role per reference, in order. Default: infer from newness"),
    ] = None,
    provenance: Annotated[
        str, typer.Option(help="synthetic | owned | licensed | unknown")
    ] = "unknown",
    skip_vision: Annotated[
        bool, typer.Option("--skip-vision", help="Local checks only (does not verify)")
    ] = False,
    allow_degraded_vision: Annotated[
        bool,
        typer.Option(
            "--allow-degraded-vision",
            help="Accept references whose vision check could not run",
        ),
    ] = False,
) -> None:
    """Sanitize reference images before any generation spend.

    A rejected reference is the single most common cause of timecodes and panel
    borders appearing in generated video, so this runs as its own step that costs
    nothing.
    """
    from omni_homevlog.pipeline.intake import ReferenceSanitizer, describe_result

    if not reference:
        _fail("Pass at least one --reference.")

    default_roles = ["identity_closeup", "identity_body", "environment", "outfit"]
    roles = role or default_roles[: len(reference)]
    while len(roles) < len(reference):
        roles.append("environment")

    entries = [
        (path, roles[i] if i < len(roles) else "environment", provenance)
        for i, path in enumerate(reference)
    ]

    sanitizer = ReferenceSanitizer(
        run_vision_pass=not skip_vision, allow_degraded_vision=allow_degraded_vision
    )
    result = sanitizer.sanitize(references=entries)

    console.print(describe_result(result))
    console.print()
    if result.rejections:
        console.print(
            f"[bold red]{len(result.rejections)} reference(s) rejected.[/bold red] "
            "Fix these before creating a job; proceeding would risk burning "
            "artefacts into the video."
        )
        raise typer.Exit(2)
    console.print(f"[green]{len(result.approved)} reference(s) approved.[/green]")


# ─────────────────────────────────────────────────────────────────────────────
# create
# ─────────────────────────────────────────────────────────────────────────────


@app.command()
def create(
    brief: Annotated[str, typer.Option("--brief", help="The creative brief")],
    reference: Annotated[
        list[Path] | None, typer.Option("--reference", "-r", help="Reference image")
    ] = None,
    role: Annotated[
        list[str] | None, typer.Option("--role", help="Role per reference, in order")
    ] = None,
    provenance: Annotated[str, typer.Option(help="Provenance for the references")] = "unknown",
    provider: Annotated[str | None, typer.Option(help="vertex or gemini_api")] = None,
    project: Annotated[str | None, typer.Option(help="Google Cloud project id")] = None,
    model: Annotated[str | None, typer.Option(help="Override the video model")] = None,
    duration: Annotated[int, typer.Option(help="Target seconds: 10/20/30/40")] = 30,
    aspect: Annotated[str, typer.Option(help="9:16 or 16:9")] = "9:16",
    resolution: Annotated[str, typer.Option(help="360p/720p/1080p/4k")] = "720p",
    mode: Annotated[str, typer.Option(help="production or concept")] = "production",
    title: Annotated[str | None, typer.Option(help="Job title")] = None,
    gcs_uri: Annotated[
        str | None, typer.Option("--gcs-uri", help="gs:// prefix for URI delivery")
    ] = None,
    human_gate: Annotated[
        str, typer.Option(help="Comma-separated gates: high-res,final,each-segment")
    ] = DEFAULT_GATES,
    max_calls: Annotated[int, typer.Option(help="Hard ceiling on provider calls")] = 8,
    max_cost: Annotated[
        float | None, typer.Option(help="Optional estimated-cost ceiling in USD")
    ] = None,
    no_audio: Annotated[bool, typer.Option("--no-audio", help="Export a silent derivative; keep the provider original")] = False,
    allow_template_fallback: Annotated[
        bool,
        typer.Option(
            "--allow-template-fallback",
            help="If the Director fails, use the built-in template (ignores your brief)",
        ),
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
    background: Annotated[bool, typer.Option(help="Submit asynchronously; resume retrieves the result")] = False,
) -> None:
    """Create and start a job."""
    from omni_homevlog.pipeline.context import JobContext
    from omni_homevlog.pipeline.intake import (
        ReferenceSanitizer,
        describe_result,
        require_approved,
    )
    from omni_homevlog.pipeline.orchestrator import run_job
    from omni_homevlog.providers.factory import build_provider, check_provider_ready
    from omni_homevlog.schemas import ProjectSpec

    settings = get_settings()

    if duration not in (10, 20, 30, 40):
        _fail(f"--duration must be 10, 20, 30, or 40 (got {duration})")

    # ─ Pre-flight: fail before spending anything. ─────────────────────────
    resolved_prefix = gcs_uri or settings.omni_output_gcs_uri
    binding = build_provider(
        provider_name=provider,  # type: ignore[arg-type]
        project=project,
        model=model,
        settings=settings,
        with_gcs=bool(resolved_prefix),
        gcs_prefix=resolved_prefix,
    )
    ready, message = check_provider_ready(binding)
    if not ready:
        _fail(f"Provider not ready: {message}")
    if not json_out:
        console.print(f"[dim]{message}[/dim]")

    spec = ProjectSpec(
        title=title or brief[:60],
        brief=brief,
        target_duration_s=duration,
        aspect_ratio=aspect,
        resolution=resolution,
        audio_enabled=not no_audio,
        background=background,
        provider=binding.provider_name,
        project=binding.project,
        model=binding.model,
        location=binding.location,
        gcs_uri=resolved_prefix,
        max_estimated_cost_usd=(Decimal(str(max_cost)) if max_cost is not None else None),
        mode=mode,
        max_total_calls=max_calls,
        human_gates=[g.strip() for g in human_gate.split(",") if g.strip()],
    )

    # Carry the last probe result into the job. `JobContext.create` has always
    # accepted one and nothing ever passed it, so the documented flow — run
    # `doctor`, then create a job that knows what the surface can do — did not
    # exist, and `pipeline/review.py` fell back to assuming edit and extend are
    # both available.
    from omni_homevlog.providers.factory import latest_capabilities

    capabilities = latest_capabilities(
        provider=binding.provider_name,
        project=binding.project,
        model=binding.model,
        settings=settings,
    )
    if capabilities is None or capabilities.location != binding.location:
        _fail("No matching capability evidence. Run doctor --run-generation first.")
    from omni_homevlog.providers.request_builder import seed_input_mode
    default_roles = ["identity_closeup", "identity_body", "environment", "outfit"]
    input_roles = list(role or default_roles[:len(reference or [])])
    while len(input_roles) < len(reference or []):
        input_roles.append("environment")
    if role and len(role) != len(reference or []):
        _fail("Provide exactly one --role for each --reference.")
    _, required_capability = seed_input_mode(input_roles if reference else [])
    if not getattr(capabilities, required_capability):
        _fail(f"Seed input mode {required_capability} has not passed a media-verified probe.")
    if mode != "concept" and duration > 10:
        binding.provider.chain_strategy(capabilities)  # type: ignore[attr-defined]

    ctx = JobContext.create(spec=spec, settings=settings, capabilities=capabilities)

    # ─ References ─────────────────────────────────────────────────────────
    if reference:
        default_roles = ["identity_closeup", "identity_body", "environment", "outfit"]
        roles = role or default_roles[: len(reference)]
        while len(roles) < len(reference):
            roles.append("environment")
        entries = [(p, roles[i], provenance) for i, p in enumerate(reference)]

        sanitizer = ReferenceSanitizer(settings=ctx.settings)
        result = sanitizer.sanitize(references=entries, paths=ctx.paths)
        if not json_out:
            console.print(describe_result(result))
        # Rejections are not advisory (§6.2).
        approved = require_approved(result, strict=True)
        ctx.manifest = ctx.store.mutate(references=approved)
        if not approved:
            _fail(
                "No approved references. A job without an identity reference cannot "
                "preserve identity."
            )
    elif not json_out:
        console.print(
            "[yellow]No references supplied. Identity will drift between segments; "
            "the plan recommends 2-4 clean photos.[/yellow]"
        )

    if not json_out:
        console.print(f"job [bold]{ctx.job_id}[/bold] created")

    report = run_job(ctx, allow_template_fallback=allow_template_fallback)

    if json_out:
        typer.echo(
            json.dumps(
                {
                    "job_id": ctx.job_id,
                    "final_state": str(report.final_state),
                    "stages": report.stages_run,
                    "stopped_because": report.stopped_because,
                    "final_path": report.final_path,
                    "errors": report.errors,
                },
                indent=2,
            )
        )
    else:
        console.print()
        console.print(report.summary())

    if report.errors:
        raise typer.Exit(2)


# ─────────────────────────────────────────────────────────────────────────────
# run / resume / status
# ────────────────────────────────────────────────────────────────────────────


@app.command()
@locked_command
def run(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    allow_template_fallback: Annotated[bool, typer.Option(hidden=True)] = False,
) -> None:
    """Drive a job forward from its current state."""
    from omni_homevlog.pipeline.orchestrator import run_job

    ctx = _load_context(job_id)
    report = run_job(ctx, allow_template_fallback=allow_template_fallback)
    console.print(report.summary())
    if report.errors:
        raise typer.Exit(2)


@app.command()
@locked_command
def resume(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    interaction_id: Annotated[
        str | None,
        typer.Option(
            "--interaction-id",
            help="Resolve an unknown-outcome request by querying this interaction id",
        ),
    ] = None,
    allow_cross_project: Annotated[
        bool,
        typer.Option(
            "--allow-cross-project",
            help="Permit resuming against a different project (recorded as degraded)",
        ),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan only")] = False,
    recover_only: Annotated[bool, typer.Option(help="Query/download only; no review or new generation")] = False,
) -> None:
    """Continue a job, resolving recovery cases first.

    Refuses to proceed while any interaction has an unknown outcome, because the
    correct next step depends on whether a billable generation is already running.
    """
    from omni_homevlog.pipeline.orchestrator import run_job
    from omni_homevlog.pipeline.resume import (
        ensure_project_binding,
        fetch_missing_outputs,
        mark_degraded_cross_project,
        plan_resume,
        resolve_unknown_interaction,
    )

    ctx = _load_context(job_id, allow_cross_project=allow_cross_project)

    original_project = ctx.store.load().project
    if allow_cross_project:
        try:
            ensure_project_binding(ctx)
        except Exception:
            mark_degraded_cross_project(ctx, from_project=original_project)

    plan = plan_resume(ctx, allow_cross_project=allow_cross_project)
    console.print(f"resume plan: {plan.describe()}")

    if dry_run:
        if plan.blockers:
            for blocker in plan.blockers:
                console.print(f"  blocked: {blocker}")
        return

    # ─ Recovery rule 2: resolve unknown outcomes before anything else ─────
    unresolved = [r for r in ctx.manifest.interactions if not r.outcome_known]
    if unresolved:
        resolved_any = False
        for record in unresolved:
            artifact = resolve_unknown_interaction(
                ctx,
                record,
                interaction_id=interaction_id if len(unresolved) == 1 else None,
            )
            if artifact is not None:
                resolved_any = True
                console.print(f"recovered output for segment {record.segment_index}")
        if not resolved_any:
            console.print(
                Panel(
                    "Unresolved interactions remain. The request may still be running "
                    "and may be billable.\n\n"
                    "Options:\n"
                    "  1. Wait and re-run `resume`.\n"
                    "  2. If you know the interaction id, pass --interaction-id.\n"
                    "  3. Check the provider console for a recent generation, then "
                    "decide by hand.\n\n"
                    "A new generation will NOT be dispatched automatically.",
                    title="needs a human decision",
                    border_style="yellow",
                )
            )
            raise typer.Exit(3)
        ctx.reload()
        plan = plan_resume(ctx)

    # ── Recovery rule 3: fetch outputs that were never downloaded ─────────
    recovered = fetch_missing_outputs(ctx)
    if recovered:
        console.print(f"re-fetched {len(recovered)} output(s) from their URIs")

    remaining = [r for r in ctx.manifest.interactions if not r.outcome_known]
    missing = [a for a in ctx.manifest.segments if not a.local_path or not Path(a.local_path).is_file()]
    if remaining or missing:
        _fail(f"Recovery incomplete: {len(remaining)} unresolved request(s), {len(missing)} missing output(s). No new generation dispatched.", code=3)

    if plan.requires_human and plan.blockers:
        console.print("[yellow]This job needs a human decision.[/yellow]")
        for blocker in plan.blockers:
            console.print(f"  - {blocker}")
        raise typer.Exit(3)

    if recover_only:
        console.print(f"recovery finished; state {ctx.store.load().state}; no new generation requested")
        return

    report = run_job(ctx)
    console.print(report.summary())
    if report.errors:
        raise typer.Exit(2)


@app.command()
def status(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    metrics: Annotated[bool, typer.Option("--metrics", help="Include aggregate metrics")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Show a job's state, chain, budget, and lineage."""
    from omni_homevlog.pipeline.finalize import summary

    ctx = _load_context(job_id)
    data = summary(ctx)

    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
        return

    console.print(
        Panel(
            f"[bold]{data['job_id']}[/bold]\n"
            f"state      {data['state']}\n"
            f"provider   {data['provider']}  project={data['project']}\n"
            f"model      {data['model']}\n"
            f"target     {data['target_duration_s']}s at {data['resolution']}",
            title="job",
        )
    )

    chain = data.get("chain") or []
    if chain:
        table = Table(title="chain", show_lines=False)
        for column in ("#", "interaction", "task", "parent", "seconds", "res"):
            table.add_column(column)
        for entry in chain:
            table.add_row(
                str(entry["index"]),
                str(entry["interaction_id"])[:18],
                str(entry["task"]),
                str(entry["parent"])[:18] if entry["parent"] else "-",
                f"{entry['duration_s']:.2f}" if entry["duration_s"] else "?",
                str(entry["resolution"] or "-"),
            )
        console.print(table)
        console.print(f"chain total: {data['chain_seconds']}s")

    budget = data.get("budget") or {}
    console.print(
        f"budget     calls {budget.get('calls_made')}/{budget.get('max_total_calls')}, "
        f"seconds {budget.get('video_seconds_requested')}, "
        f"est. cost {budget.get('estimated_cost_usd')} USD "
        "[dim](estimate only; Cloud Billing is authoritative)[/dim]"
    )

    unresolved = data.get("unresolved_interactions") or []
    if unresolved:
        console.print(
            f"[yellow]{len(unresolved)} interaction(s) with unknown outcome: "
            f"{', '.join(unresolved)}[/yellow]"
        )

    if data.get("final_path"):
        console.print(
            f"output     {data['final_path']} "
            f"(derived={data['final_derived']}, c2pa={data['c2pa_present']})"
        )

    for error in data.get("errors") or []:
        console.print(f"[red]error:[/red] {error}")
    for note in data.get("notes") or []:
        console.print(f"[dim]note: {note}[/dim]")

    if metrics:
        from omni_homevlog.observability.metrics import collect_job_metrics, format_metrics

        console.print()
        console.print(format_metrics(collect_job_metrics(ctx.store.load(), db=ctx.db)))


@app.command()
def review(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    show: Annotated[
        int | None, typer.Option("--show", help="Print the full report for segment N")
    ] = None,
    open_dir: Annotated[
        bool, typer.Option("--open", help="Print the reviews directory path")
    ] = False,
) -> None:
    """Show the automated review reports."""
    ctx = _load_context(job_id)

    if open_dir:
        console.print(str(ctx.paths.reviews_dir))
        return

    reports = ctx.store.load().quality_reports
    if not reports:
        console.print("No reviews yet.")
        return

    if show is not None:
        match = next(
            (r for r in reports if r.get("segment_index") == show),
            None,
        )
        if match is None:
            _fail(f"No review for segment {show}")
        console.print_json(json.dumps(match, default=str))
        return

    table = Table(title=f"reviews for {job_id}")
    for column in (
        "seg",
        "attempt",
        "verdict",
        "identity",
        "anatomy",
        "continuity",
        "anchor",
        "degraded",
    ):
        table.add_column(column)
    for entry in reports:
        rep = entry.get("report") or {}
        table.add_row(
            str(entry.get("segment_index")),
            str(entry.get("attempt_index")),
            str(rep.get("verdict")),
            f"{rep.get('identity_score', 0):.2f}",
            f"{rep.get('anatomy_score', 0):.2f}",
            f"{rep.get('continuity_with_previous_score', 0):.2f}",
            "yes" if rep.get("anchor_usable") else "NO",
            "yes" if rep.get("critic_degraded") else "",
        )
    console.print(table)

    thresholds = load_thresholds()
    console.print(
        f"[dim]thresholds: identity>={thresholds.accept_identity}, "
        f"anatomy>={thresholds.accept_anatomy}, "
        f"motion>={thresholds.accept_motion} "
        f"(calibrated_at={thresholds.calibrated_at}; these are policy knobs, not "
        "measurements)[/dim]"
    )


@app.command()
@locked_command
def approve(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    stage: Annotated[str, typer.Option("--stage", help="high-res | final | segment-N")] = "final",
    note: Annotated[str | None, typer.Option("--note", help="Why you approved")] = None,
) -> None:
    """Clear a human gate and move the job to where it can continue.

    Approving does two things: it records the decision, and it moves the job out of
    the gate. An earlier version only appended a history note, which left the job
    in exactly the state it was stuck in — so `approve` printed a success message
    and changed nothing an operator could see.
    """
    from omni_homevlog.pipeline.resume import approve_gate

    ctx = _load_context(job_id)

    try:
        previous, target = approve_gate(ctx, stage=stage, note=note)
    except OmniVlogError as exc:
        _fail(f"{exc.message}\n{exc.remediation or ''}".strip())

    console.print(f"approved stage [bold]{stage}[/bold] for {job_id}")
    console.print(f"state      {previous} → {target}")

    if target.name.endswith("_REVIEW") or target.name == "SEED_REVIEW":
        console.print("Run `omni-vlog resume` to re-run the review and continue.")
    elif "RENDERING" in target.name:
        console.print(
            "Run `omni-vlog resume` to continue. Note this will dispatch a new paid render."
        )
    else:
        console.print("Run `omni-vlog resume` to continue.")


@app.command()
@locked_command
def retry(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    stage: Annotated[str, typer.Option("--stage", help="segment-N")],
    mode: Annotated[str, typer.Option("--mode", help="edit | regenerate")] = "edit",
    edit_prompt: Annotated[
        str | None,
        typer.Option(
            "--edit-prompt",
            help="The single local correction to apply. Defaults to the Critic's last "
            "suggestion, which must pass the §9 prompt rules.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Relax the ceilings for this one attempt")
    ] = False,
) -> None:
    """Re-run one segment with a chosen repair mode.

    Performs the repair the operator asked for. An earlier version parsed `--stage`
    and `--mode`, validated them, and then called the generic pipeline, which
    re-derived its own decision from the last review — so the flags did nothing and
    a request to *edit* could quietly become a *regeneration*.
    """
    from omni_homevlog.budget import CallKind
    from omni_homevlog.pipeline.orchestrator import run_job
    from omni_homevlog.pipeline.repair import run_edit, run_regenerate
    from omni_homevlog.schemas import JobState

    ctx = _load_context(job_id)

    if not stage.startswith("segment-"):
        _fail("--stage must look like segment-0, segment-1, ...")
    try:
        segment_index = int(stage.split("-", 1)[1])
    except ValueError:
        _fail(f"Could not parse a segment index from {stage!r}")

    if mode not in ("edit", "regenerate"):
        _fail("--mode must be edit or regenerate")

    segment = next((s for s in ctx.segment_plan if s.index == segment_index), None)
    if segment is None:
        _fail(
            f"The plan has no segment {segment_index}. Planned segments: "
            f"{[s.index for s in ctx.segment_plan]}"
        )

    artifact = ctx.manifest.artifact_for_segment(segment_index)
    if artifact is None or not artifact.is_usable_video():
        _fail(
            f"Segment {segment_index} has no usable render to repair. Run "
            "`omni-vlog resume` to produce one first."
        )

    is_extension = segment_index > 0
    previous = ctx.manifest.artifact_for_segment(segment_index - 1) if is_extension else None
    attempt_index = (
        ctx.budget.count(CallKind.EDIT if mode == "edit" else CallKind.REGENERATE, segment_index)
        + 1
    )

    if force:
        # "Force" used to be inert: `run_regenerate` authorises internally and would
        # still refuse. Relaxing the ceilings here is what the flag promises.
        ctx.budget.max_total_calls += 2
        ctx.budget.max_edit_attempts_per_segment += 1
        ctx.budget.max_regenerations_per_segment += 1
        console.print(
            "[yellow]--force: ceilings relaxed for this attempt. The extra spend will "
            "be recorded in the manifest.[/yellow]"
        )

    # A retry is a deliberate human decision, so it may re-enter from NEEDS_HUMAN.
    if ctx.manifest.state is JobState.NEEDS_HUMAN:
        ctx.manifest = ctx.store.mutate(
            state_history=[
                *ctx.manifest.state_history,
                {
                    "from": "NEEDS_HUMAN",
                    "to": "NEEDS_HUMAN",
                    "at": _now(),
                    "note": f"retry requested: {stage} {mode}",
                },
            ]
        )

    if any(not r.outcome_known for r in ctx.manifest.interactions):
        _fail("Resolve pending interactions before retry; no new generation was dispatched.")
    if ctx.manifest.state in (JobState.COMPLETE, JobState.FAILED_FINAL, JobState.POLICY_BLOCKED):
        _fail("This job is terminal; create a new job for further changes.")
    if any(a.segment_index > segment_index for a in ctx.manifest.segment_artifacts()):
        _fail(
            "This segment already has descendants. Repair the latest cumulative video or start a new chain."
        )
    target_review = (
        JobState.SEED_REVIEW
        if segment_index == 0
        else JobState(f"EXTENSION_{segment_index}_REVIEW")
    )
    from omni_homevlog.state_machine import legal_transitions

    if target_review != ctx.manifest.state and target_review not in legal_transitions(
        ctx.manifest.state, ctx.spec.extension_count
    ):
        ctx.manifest = ctx.store.transition(
            target=JobState.NEEDS_HUMAN, note="operator requested repair"
        )
    if target_review != ctx.manifest.state:
        ctx.manifest = ctx.store.transition(
            target=target_review, note="operator repair; review required"
        )
    if force:
        ctx.spec.max_total_calls = ctx.budget.max_total_calls
        ctx.manifest = ctx.store.mutate(spec=ctx.spec, budget=ctx.budget.snapshot())
    console.print(f"retrying segment {segment_index} with mode={mode}")

    try:
        if mode == "edit":
            prompt = _resolve_edit_prompt(ctx, segment_index, edit_prompt)
            if prompt is None:
                _fail(
                    "No usable edit prompt. Pass --edit-prompt with a single local "
                    "correction, or use --mode regenerate."
                )
            run_edit(
                ctx,
                artifact=artifact,
                edit_prompt=prompt,
                segment_index=segment_index,
                attempt_index=attempt_index,
            )
        else:
            run_regenerate(
                ctx,
                segment=segment,
                segment_index=segment_index,
                attempt_index=attempt_index,
                previous_artifact=previous,
                is_extension=is_extension,
            )
    except OmniVlogError as exc:
        _fail(f"{exc.message}\n{exc.remediation or ''}".strip())

    console.print("repair done; driving the pipeline forward")
    report = run_job(ctx)
    console.print(report.summary())


def _resolve_edit_prompt(ctx: Any, segment_index: int, supplied: str | None) -> str | None:
    """A §9-valid edit prompt, from the flag or the Critic's last suggestion.

    Either way it goes through the compiler, so a hand-typed prompt is held to the
    same standard as a generated one — an operator pasting "cut to a close-up at
    00:07" should be refused for the same reason a Critic would be.
    """
    source = supplied
    if source is None:
        for entry in reversed(ctx.manifest.quality_reports):
            if entry.get("segment_index") != segment_index:
                continue
            report = entry.get("report") or {}
            decision = entry.get("decision") or {}
            source = decision.get("suggested_edit") or report.get("suggested_edit_prompt")
            if source:
                break

    if not source:
        return None

    compiled = ctx.compiler.compile_edit_from_suggestion(
        suggestion=source, segment_index=segment_index
    )
    return compiled.text if compiled else None


@app.command()
@locked_command
def export(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination path")],
    allow_derived: Annotated[
        bool, typer.Option("--allow-derived", help="Accept a transformed (derived) file")
    ] = True,
) -> None:
    """Copy the deliverable to a path of your choosing."""
    from omni_homevlog.pipeline.finalize import export as do_export

    ctx = _load_context(job_id)

    path = do_export(ctx, output=output, allow_derived=allow_derived)
    console.print(f"exported to [bold]{path}[/bold]")

    manifest_path = ctx.paths.final_dir / "manifest.json"
    console.print(f"manifest at {manifest_path}")
    if ctx.manifest.final_derived:
        console.print(
            "[yellow]This file is derived (it went through a transform). Content "
            "credentials may not match the provider's original output.[/yellow]"
        )


@app.command("high-res")
@locked_command
def high_res(
    job_id: Annotated[str, typer.Argument(help="Source job id")],
    resolution: Annotated[str, typer.Option("--resolution", help="1080p or 4k")] = "1080p",
    confirm_understanding: Annotated[
        bool,
        typer.Option(
            "--confirm-understanding",
            help="Acknowledge that this re-generates the footage rather than upscaling it",
        ),
    ] = False,
    max_calls: Annotated[int, typer.Option(help="Call ceiling for the new job")] = 8,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
) -> None:
    """Start a higher-resolution pass as a NEW job.

    §24.12: the result is a new generation, not a lossless upgrade. The person,
    framing, motion, and audio may all differ, and the output will not match the
    draft frame for frame. It is therefore a separate job with its own lineage,
    budget, and review cycle, and each manifest names the other.
    """
    from omni_homevlog.pipeline.context import JobContext
    from omni_homevlog.pipeline.finalize import (
        check_high_res_request,
        high_res_spec,
    )
    from omni_homevlog.providers.factory import check_provider_ready

    source = _load_context(job_id)

    try:
        check_high_res_request(
            source, resolution=resolution, confirmed_understanding=confirm_understanding
        )
    except OmniVlogError as exc:
        _fail(f"{exc.message}\n{exc.remediation or ''}".strip())

    console.print(
        Panel(
            f"Source job   {job_id}  ({source.spec.resolution}, "
            f"{source.spec.target_duration_s}s)\n"
            f"New job      {resolution}, {source.spec.target_duration_s}s\n\n"
            "This RE-GENERATES the footage at the new resolution. It is not a\n"
            "lossless upgrade: the person, framing, motion, and audio may change,\n"
            "and the result will not match the draft frame for frame.\n"
            "It costs a full new chain.",
            title="higher-resolution pass",
            border_style="yellow",
        )
    )

    if not yes and not typer.confirm("Start a new job?"):
        raise typer.Exit(0)

    new_spec = high_res_spec(source, resolution=resolution)
    new_spec.max_total_calls = max_calls

    binding_ok, message = check_provider_ready(source.binding)
    if not binding_ok:
        _fail(f"Provider not ready: {message}")

    new_ctx = JobContext.create(
        spec=new_spec, settings=source.settings, capabilities=source.capabilities
    )
    # The explicit confirmation above is this new job's high-resolution approval.
    new_ctx.manifest = new_ctx.store.mutate(state_history=[*new_ctx.manifest.state_history, {"from": "CREATED", "to": "CREATED", "at": _now(), "note": "approved:high-res"}])

    # Carry the approved references over, so the new chain draws on the same
    # identity material rather than starting from nothing.
    carried = []
    for asset in source.manifest.references:
        cloned = asset.model_copy(deep=True)
        cloned.id = f"{asset.id}_hires"
        original_reference = Path(asset.path_or_uri)
        if original_reference.is_file():
            import shutil
            copied_reference = new_ctx.paths.references_dir / original_reference.name
            shutil.copy2(original_reference, copied_reference)
            cloned.path_or_uri = str(copied_reference)
            cloned.staged_uri = None
        carried.append(cloned)
    if carried:
        new_ctx.manifest = new_ctx.store.mutate(references=carried)

    # Cross-link the two jobs so neither lineage looks standalone.
    new_ctx.note(
        f"higher-resolution pass for job {job_id} "
        f"({source.spec.resolution} -> {resolution}). This is a NEW generation, "
        "not an upscale of that job's output (§12.3, §24.12)."
    )
    source.note(
        f"higher-resolution pass started as job {new_ctx.job_id} at {resolution}. "
        "That is a separate generation and is not an upgrade of this output."
    )

    console.print(f"new job [bold]{new_ctx.job_id}[/bold] created")

    from omni_homevlog.pipeline.orchestrator import run_job

    report = run_job(new_ctx)
    console.print()
    console.print(report.summary())
    if report.errors:
        raise typer.Exit(2)


@app.command()
@locked_command
def sync(
    job_id: Annotated[str, typer.Argument(help="Job id")],
) -> None:
    """Mirror a job's whole tree to GCS.

    Stages push what they change as they run; this is the catch-up command for
    when a mirror failed, or for a job that ran before a bucket was configured.
    """
    from omni_homevlog.storage.sync import mirror_job

    ctx = _load_context(job_id)
    gcs = ctx.binding.provider.gcs
    prefix = ctx.binding.provider.gcs_prefix

    if gcs is None or not prefix:
        _fail(
            "No GCS prefix configured, so there is nowhere to mirror to. Set OMNI_OUTPUT_GCS_URI."
        )

    console.print(f"mirroring [bold]{job_id}[/bold] to {prefix}")
    result = mirror_job(ctx.paths, gcs, prefix)
    console.print(result.describe())

    for uri, error in result.failed:
        console.print(f"[red]failed:[/red] {uri}\n  {error}")

    if result.failed:
        raise typer.Exit(2)


@app.command()
@locked_command
def delete(
    job_id: Annotated[str, typer.Argument(help="Job id")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt")] = False,
    keep_gcs: Annotated[bool, typer.Option("--keep-gcs", help="Do not delete GCS objects")] = False,
) -> None:
    """Delete local files, GCS objects, and database metadata for a job.

    §22 requires the complete path for all three, because a job that is only
    half-deleted is worse than one that is not deleted at all: the operator
    believes the material is gone when part of it is still in a bucket.
    """
    from omni_homevlog.storage.gcs import GcsClient
    from omni_homevlog.storage.sync import job_prefix

    ctx = _load_context(job_id)
    paths = ctx.paths
    size = paths.total_bytes()

    gcs_prefix_uri: str | None = None
    gcs_object_count: int | None = None
    gcs: GcsClient | None = None

    prefix = ctx.spec.gcs_uri
    if prefix and not keep_gcs:
        gcs_prefix_uri = job_prefix(prefix, job_id)
        try:
            gcs = GcsClient()
            gcs_object_count = len(gcs.list_objects(gcs_prefix_uri))
        except Exception as exc:
            console.print(f"[yellow]could not list GCS objects: {exc}[/yellow]")

    console.print(f"job       {job_id}")
    console.print(f"local     {paths.root}  ({size / 1e6:.1f} MB)")
    if gcs_prefix_uri:
        count = f"{gcs_object_count} object(s)" if gcs_object_count is not None else "unknown count"
        console.print(f"gcs       {gcs_prefix_uri}  ({count})")
    elif prefix:
        console.print(f"gcs       [dim]kept (--keep-gcs), would have been {prefix}[/dim]")

    if not yes:
        confirm = typer.confirm("Delete all of the above?")
        if not confirm:
            raise typer.Exit(0)

    # ─ GCS first ────────────────────────────────────────────────────────
    # Removed before the local tree, so a failure here leaves the local copy
    # intact and the operation is repeatable.
    if gcs_prefix_uri and gcs is not None:
        deleted, errors = gcs.delete_prefix(gcs_prefix_uri)
        console.print(f"gcs       deleted {deleted} object(s)")
        for error in errors:
            console.print(f"[red]gcs error:[/red] {error}")
        if errors:
            console.print(
                "[yellow]Some GCS objects could not be deleted. The local job has NOT "
                "been removed, so you can re-run this command after fixing access.[/yellow]"
            )
            raise typer.Exit(3)

    ctx.db.delete_job(job_id)
    paths.delete()
    console.print("deleted")


@app.command()
def projects(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """List projects the current credentials can see.

    Discovery only. This never selects a project, never ranks them, and never
    suggests pooling — §24.5 forbids automatic project switching.
    """
    from omni_homevlog.providers.factory import list_available_projects

    found = list_available_projects()
    if json_out:
        console.print(json.dumps(found, indent=2))
        return
    if not found:
        console.print(
            "No projects discovered. Check ADC with `gcloud auth application-default login`."
        )
        return
    for entry in found:
        console.print(f"{entry['project']}  [dim]({entry['note']})[/dim]")
    console.print(
        "\n[dim]A job binds to one project for its whole life. Switching mid-chain is "
        "refused unless you explicitly pass --allow-cross-project, which is recorded "
        "as degraded.[/dim]"
    )


@app.command()
def metrics(
    job_id: Annotated[str | None, typer.Argument(help="Job id (omit for a fleet summary)")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output")] = False,
) -> None:
    """Aggregate the interaction ledger (§23)."""
    from omni_homevlog.observability.metrics import (
        collect_job_metrics,
        fleet_summary,
        format_metrics,
    )
    from omni_homevlog.storage.local import LocalStore

    store = LocalStore(get_settings().data_dir())

    if job_id:
        ctx = _load_context(job_id)
        data = collect_job_metrics(ctx.store.load(), db=ctx.db)
        console.print(json.dumps(data.as_dict(), indent=2) if json_out else format_metrics(data))
        return

    # Fleet view. Probes live in per-job databases, so aggregate them by opening
    # each one; a job whose database is missing still counts via its manifest.
    jobs = store.list_jobs()
    by_state: dict[str, int] = {}
    probes: list[dict[str, object]] = []
    for name in jobs:
        paths = store.job(name)
        manifest = None
        if paths.manifest_path.exists():
            try:
                from omni_homevlog.storage.manifest import load_manifest_file

                manifest = load_manifest_file(paths.manifest_path)
            except Exception:
                manifest = None
        if manifest is not None:
            key = str(manifest.state)
            by_state[key] = by_state.get(key, 0) + 1
        if paths.db_path.exists():
            from omni_homevlog.observability.metrics import collect_provider_metrics
            from omni_homevlog.storage.database import Database

            probes.extend(collect_provider_metrics(Database(paths.db_path)))

    result = {
        "jobs": len(jobs),
        "by_state": by_state,
        "completed": by_state.get("COMPLETE", 0),
        "needing_human": sum(
            by_state.get(s, 0) for s in ("NEEDS_HUMAN", "BUDGET_EXHAUSTED", "PROVIDER_UNAVAILABLE")
        ),
        "failed": by_state.get("FAILED_FINAL", 0),
        "providers": probes,
    }
    console.print(json.dumps(result, indent=2))
    _ = fleet_summary  # available for callers that already hold a Database


@app.command("thresholds")
def show_thresholds() -> None:
    """Show the decision-policy thresholds and their calibration status."""
    t = load_thresholds()
    console.print_json(json.dumps(t.to_dict(), indent=2))
    console.print(f"\n[bold]calibrated_at[/bold] {t.calibrated_at}\n{t.calibration_note}")


# ────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_context(job_id: str, *, allow_cross_project: bool = False) -> Any:
    """Load a job context, or exit with a readable message.

    `"latest"` is resolved here so every command accepts it without repeating
    the lookup.
    """
    from omni_homevlog.pipeline.context import JobContext

    if job_id == "latest":
        from omni_homevlog.storage.local import LocalStore

        latest = LocalStore(get_settings().data_dir()).latest_job_id()
        if latest is None:
            _fail("No jobs found.")
        job_id = latest

    try:
        return JobContext.load(job_id, allow_cross_project=allow_cross_project)
    except OmniVlogError as exc:
        _fail(f"{exc.message}\n{exc.remediation or ''}".strip())


def _now() -> str:
    from omni_homevlog.schemas import utc_now_iso

    return utc_now_iso()


def main() -> None:
    """Console-script entry point.

    Catches our own error types so an operator sees the code, the message, and the
    remediation hint instead of a traceback. Anything else is a genuine bug and is
    allowed to surface as a traceback.
    """
    try:
        app()
    except OmniVlogError as exc:
        console.print(f"[bold red]{exc.code}[/bold red]: {exc.message}")
        if exc.remediation:
            console.print(f"[yellow]hint:[/yellow] {exc.remediation}")
        sys.exit(1)


_ = Decimal  # kept importable for scripts that build specs programmatically


@app.command()
def studio(
    port: int = typer.Option(8765, min=1024, max=65535, help="Local workbench port."),
    open_browser: bool = typer.Option(True, '--open/--no-open', help="Open the local workbench."),
) -> None:
    """启动本地网页工作台：选图、生成、预览、修改与下载。"""
    import threading
    import webbrowser

    import uvicorn

    from omni_homevlog.studio.app import create_app

    url = f'http://127.0.0.1:{port}'
    console.print(f'Omni Studio: {url}')
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host='127.0.0.1', port=port)
