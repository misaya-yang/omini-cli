"""Seed render stage (§17).

Renders segment 0, records the interaction in the ledger *before* the request is
dispatched, and handles the outcome-unknown case correctly.

That ordering is the point of this module. §5.1's recovery rules depend on the
ledger knowing a request was dispatched even when we never saw its result:

  * crash before the request  → repeating the state is safe
  * request sent, result unknown → query the interaction; do NOT regenerate

A `RequestTimeoutUnknownOutcome` therefore writes an interaction record with
`outcome_known=False`, moves the job to `NEEDS_HUMAN`, and stops. It does not
retry, and it does not silently swallow the fact that a billable generation may
be running.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omni_homevlog.budget import CallKind
from omni_homevlog.errors import (
    BudgetExhaustedError,
    HumanGateError,
    JobNotFoundError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.resume import APPROVAL_PREFIX
from omni_homevlog.schemas import (
    InteractionRecord,
    JobState,
    RenderArtifact,
    utc_now_iso,
)

logger = get_logger("render_seed")


@dataclass(slots=True)
class SeedOutcome:
    artifact: RenderArtifact
    prompt: str
    prompt_warnings: list[str]


def run_render_seed(
    ctx: JobContext,
    *,
    attempt_index: int = 0,
    duration_s: int | None = None,
) -> SeedOutcome:
    """Render the first segment."""
    if not ctx.segment_plan:
        raise JobNotFoundError("Cannot render a seed before the plan exists.")
    segment = ctx.segment_plan[0]

    if ctx.manifest.state is JobState.PLAN_READY:
        ctx.manifest = ctx.store.transition(
            target=JobState.SEED_RENDERING, note="seed render starting"
        )
    elif ctx.manifest.state is not JobState.SEED_RENDERING:
        # Re-entering from a review or a human gate is legal; anything else is not.
        ctx.manifest = ctx.store.transition(
            target=JobState.SEED_RENDERING, note=f"re-render from {ctx.manifest.state}"
        )

    # The `each-segment` gate, which had no caller at all: the function existed and
    # nothing invoked it, so `--human-gate each-segment` was accepted and ignored.
    require_human_approval(ctx, f"segment-{segment.index}")

    resolved_duration = duration_s or segment.intended_duration_s
    if ctx.spec.mode == "concept":
        resolved_duration = min(ctx.spec.concept_duration_s, resolved_duration)

    compiled = ctx.compiler.compile_seed(segment)
    if compiled.warnings:
        for warning in compiled.warnings:
            ctx.note(warning)

    # Reserve budget before dispatching. A refusal here is the intended outcome of
    # §24.8 — the policy layer, not the model, decides whether we spend.
    try:
        ctx.authorize(
            CallKind.SEED,
            segment_index=0,
            attempt_index=attempt_index,
            video_seconds=resolved_duration,
        )
    except BudgetExhaustedError as exc:
        ctx.manifest = ctx.store.transition(target=JobState.BUDGET_EXHAUSTED, note=exc.message)
        raise

    logger.info(
        "Rendering seed",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "duration_s": resolved_duration,
                "resolution": ctx.spec.resolution,
                "references": len(ctx.manifest.references),
            }
        },
    )

    record_dispatch_pending(
        ctx, segment_index=0, attempt_index=attempt_index, task="reference_to_video"
    )

    import asyncio

    try:
        artifact = asyncio.run(
            ctx.provider.generate_seed(
                prompt=compiled.text,
                assets=ctx.manifest.references,
                spec=ctx.spec,
                segment_index=0,
                attempt_index=attempt_index,
                duration_s=resolved_duration,
            )
        )
    except RequestTimeoutUnknownOutcome as exc:
        _record_unknown_outcome(ctx, segment_index=0, attempt_index=attempt_index, exc=exc)
        ctx.manifest = ctx.store.transition(
            target=JobState.NEEDS_HUMAN,
            note=(
                "seed request timed out with unknown outcome; the generation may "
                "exist and may be billable. Resolve with `omni-vlog resume`, which "
                "queries rather than re-renders."
            ),
        )
        raise
    except ProviderError as exc:
        _record_failed_interaction(ctx, segment_index=0, attempt_index=attempt_index, exc=exc)
        ctx.error(f"seed render failed: {exc}")
        raise

    clear_dispatch_pending(ctx, segment_index=0, attempt_index=attempt_index)
    _persist(
        ctx,
        artifact,
        segment_index=0,
        attempt_index=attempt_index,
        call_kind="seed",
    )
    ctx.note(f"seed rendered: interaction {artifact.interaction_id}")

    ctx.manifest = ctx.store.transition(
        target=JobState.SEED_REVIEW, note="seed rendered; awaiting review"
    )
    return SeedOutcome(artifact=artifact, prompt=compiled.text, prompt_warnings=compiled.warnings)


def _persist(
    ctx: JobContext,
    artifact: RenderArtifact,
    *,
    segment_index: int,
    attempt_index: int,
    parent: RenderArtifact | None = None,
    call_kind: str | None = None,
) -> None:
    """Record the artifact and its interaction in manifest + database."""
    ctx.db.save_artifact(ctx.job_id, artifact, attempt_index=attempt_index)
    # A repair replaces its segment's artifact; it does not append a second link
    # to the chain.
    ctx.manifest = ctx.store.mutate(
        segments=ctx.manifest.upsert_segment(artifact),
    )

    record = InteractionRecord(
        interaction_id=artifact.interaction_id,
        parent_interaction_id=artifact.parent_interaction_id,
        job_id=ctx.job_id,
        segment_index=segment_index,
        attempt_index=attempt_index,
        provider=artifact.provider,
        project=artifact.project,
        model=artifact.model,
        task=artifact.task,
        call_kind=call_kind,
        resolution=artifact.resolution,
        duration=f"{artifact.requested_duration_s}s" if artifact.requested_duration_s else None,
        request_started_at=artifact.created_at,
        request_completed_at=artifact.completed_at or utc_now_iso(),
        latency_s=artifact.latency_s,
        status=artifact.status,
        output_uri=artifact.gcs_uri,
        estimated_cost_usd=artifact.estimated_cost_usd,
        outcome_known=True,
    )
    ctx.db.save_interaction(record)
    ctx.manifest = ctx.store.mutate(interactions=[*ctx.manifest.interactions, record])

    # Reconcile the estimate against what the provider says it produced. The
    # reservation is sized from the requested seconds; the usage counts are what
    # was actually billed, and `Budget.record_cost` existed for exactly this and
    # had no caller — so the ceiling drifted from reality on every job.
    if artifact.usage and artifact.estimated_cost_usd is not None:
        from omni_homevlog.costing import estimate_from_usage

        realised = estimate_from_usage(model=artifact.model, usage=artifact.usage)
        delta = realised - artifact.estimated_cost_usd
        if delta:
            ctx.budget.record_cost(delta)
    _ = parent


def pending_id(job_id: str, segment_index: int, attempt_index: int) -> str:
    """The placeholder ledger id used between dispatch and result."""
    return f"pending-{job_id}-s{segment_index}-a{attempt_index}"


def record_dispatch_pending(
    ctx: JobContext,
    *,
    segment_index: int,
    attempt_index: int,
    task: str,
) -> str:
    """Write a ledger row *before* the request goes out (§5.1).

    This module's docstring promises this, and it was not happening. The gap
    matters for the case the ledger exists to cover: if the process dies during
    the request — Ctrl-C, SIGKILL, OOM, a sleeping laptop — neither the timeout
    handler nor the provider-error handler runs, because `KeyboardInterrupt` is a
    `BaseException`. Without a row already on disk, the manifest stays in
    `*_RENDERING`, `resume` reports "safe, nothing was dispatched", the crashed
    call is forgiven by `budget_from_manifest`, and a second paid generation is
    dispatched while the first may have completed and been billed.

    The row is retired by `clear_dispatch_pending` once a real result arrives.
    """
    record = InteractionRecord(
        interaction_id=pending_id(ctx.job_id, segment_index, attempt_index),
        job_id=ctx.job_id,
        segment_index=segment_index,
        attempt_index=attempt_index,
        provider=ctx.binding.provider_name,
        project=ctx.binding.project,
        model=ctx.binding.model,
        task=task,
        call_kind=task if task in ("seed", "extend", "edit", "regenerate") else "seed",
        request_started_at=utc_now_iso(),
        status="dispatched",
        error_message="dispatched; no result observed yet",
        outcome_known=False,
    )
    ctx.db.save_interaction(record)
    ctx.manifest = ctx.store.mutate(interactions=[*ctx.manifest.interactions, record])
    return record.interaction_id


def clear_dispatch_pending(ctx: JobContext, *, segment_index: int, attempt_index: int) -> None:
    """Retire the placeholder once the call returned normally."""
    placeholder = pending_id(ctx.job_id, segment_index, attempt_index)
    ctx.db.delete_interaction(placeholder)
    ctx.manifest = ctx.store.mutate(
        interactions=[r for r in ctx.manifest.interactions if r.interaction_id != placeholder]
    )


def _record_unknown_outcome(
    ctx: JobContext,
    *,
    segment_index: int,
    attempt_index: int,
    exc: RequestTimeoutUnknownOutcome,
) -> None:
    """A dispatched request whose result we never saw.

    We do not know the interaction id — the request may still be running — so the
    record uses a synthetic id and `outcome_known=False`. `resume` treats these as
    blockers that require a human decision rather than as retryable failures.
    """
    _rewrite_ledger_row(
        ctx,
        record_id=pending_id(ctx.job_id, segment_index, attempt_index),
        status="unknown",
        error_code=exc.code,
        error_message=exc.message,
        outcome_known=False,
        task="reference_to_video" if ctx.manifest.references else "text_to_video",
    )
    ctx.budget.note_unknown_outcome()
    ctx.error(
        f"segment {segment_index} attempt {attempt_index}: request dispatched but "
        "outcome unknown — it may still be running and may be billable"
    )


def _rewrite_ledger_row(
    ctx: JobContext,
    *,
    record_id: str,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    outcome_known: bool,
    task: str | None = None,
) -> None:
    """Update the dispatched row in place.

    One row per dispatch, from the moment the request leaves to the moment its
    outcome is known. Appending a second row would double-count the call in
    `budget_from_manifest` and show an operator two entries for one request.
    """
    updated = [
        record.model_copy(
            update={
                "status": status,
                "error_code": error_code,
                "error_message": error_message,
                "outcome_known": outcome_known,
                "request_completed_at": utc_now_iso(),
                **({"task": task} if task else {}),
            }
        )
        if record.interaction_id == record_id
        else record
        for record in ctx.manifest.interactions
    ]
    ctx.manifest = ctx.store.mutate(interactions=updated)
    match = next((r for r in updated if r.interaction_id == record_id), None)
    if match is not None:
        ctx.db.save_interaction(match)


def _record_failed_interaction(
    ctx: JobContext,
    *,
    segment_index: int,
    attempt_index: int,
    exc: ProviderError,
) -> None:
    """A request the provider definitively rejected. Safe to retry."""
    clear_dispatch_pending(ctx, segment_index=segment_index, attempt_index=attempt_index)
    interaction_id = getattr(exc, "interaction_id", None) or (
        f"failed-{ctx.job_id}-s{segment_index}-a{attempt_index}"
    )
    record = InteractionRecord(
        interaction_id=interaction_id,
        job_id=ctx.job_id,
        segment_index=segment_index,
        attempt_index=attempt_index,
        provider=ctx.binding.provider_name,
        project=ctx.binding.project,
        model=ctx.binding.model,
        task="text_to_video",
        request_started_at=utc_now_iso(),
        status="failed",
        error_code=exc.code,
        error_message=exc.message,
        outcome_known=True,
    )
    ctx.db.save_interaction(record)
    ctx.manifest = ctx.store.mutate(interactions=[*ctx.manifest.interactions, record])


def seed_artifact(ctx: JobContext) -> RenderArtifact | None:
    """The current seed render, if one exists and is usable."""
    for artifact in ctx.manifest.segment_artifacts():
        if artifact.task in ("text_to_video", "image_to_video", "reference_to_video"):
            return artifact
    return None


def seed_video_path(ctx: JobContext) -> Path | None:
    artifact = seed_artifact(ctx)
    if artifact and artifact.local_path:
        return Path(artifact.local_path)
    return None


def require_human_approval(ctx: JobContext, stage: str) -> None:
    """Raise when a configured human gate has not been cleared.

    Gates are opt-in via the spec's `human_gates` (§7.1) and default to gating the
    high-resolution/final step (§12.3).

    This is called at the start of every segment render, which is what makes
    `--human-gate each-segment` mean anything. An earlier version read a note of
    exactly `"approved"` while `omni-vlog approve` writes `"approved:<stage>"`, so
    the check never matched an approval and the gate would have blocked forever
    had anything called it.
    """
    if "each-segment" not in ctx.spec.human_gates:
        return

    wanted = f"{APPROVAL_PREFIX}{stage}"
    if not any(
        str(entry.get("note", "")).startswith(wanted) for entry in ctx.manifest.state_history
    ):
        raise HumanGateError(
            f"Stage {stage!r} requires explicit approval. Run "
            f"`omni-vlog approve {ctx.job_id} --stage {stage}`.",
            detail={"stage": stage, "wanted_note": wanted},
        )
