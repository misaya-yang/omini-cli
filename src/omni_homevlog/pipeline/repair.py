"""Repair stage: bounded edit or regenerate (§11.4, §12).

Runs *after* the decision policy has chosen EDIT or REGENERATE. This module only
executes the decision; it never makes one.

Every path here is bounded. §8's rule "不允许无限重试" is enforced by the budget:
`ctx.authorize` raises `BudgetExhaustedError` when the per-segment or per-job
ceiling binds, and the job moves to `BUDGET_EXHAUSTED` rather than looping.

The edit path additionally refuses to run when the Critic's suggested fix does not
map to a single local correction. §11.4 draws a real distinction between an edit
and a regeneration, and applying a regeneration through the edit endpoint produces
a worse result than either — the model is being asked to hold everything else
fixed while also changing something structural.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni_homevlog.budget import CallKind
from omni_homevlog.errors import (
    BudgetExhaustedError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.render_seed import _persist as persist_artifact
from omni_homevlog.pipeline.render_seed import (
    _record_failed_interaction,
    _record_unknown_outcome,
)
from omni_homevlog.schemas import JobState, RenderArtifact, SegmentPlan

logger = get_logger("repair")


@dataclass(slots=True)
class RepairOutcome:
    artifact: RenderArtifact
    prompt: str
    mode: str  # "edit" | "regenerate"


def run_edit(
    ctx: JobContext,
    *,
    artifact: RenderArtifact,
    edit_prompt: str,
    segment_index: int,
    attempt_index: int,
) -> RepairOutcome:
    """Apply one local edit to an existing render."""
    # An edit re-renders the whole segment, so it must reserve the same seconds a
    # regeneration would. Reserving 0 left `max_video_seconds_requested` and the
    # cost ceiling unable to see edits at all.
    edit_seconds = artifact.requested_duration_s or 10
    try:
        ctx.authorize(
            CallKind.EDIT,
            segment_index=segment_index,
            attempt_index=attempt_index,
            video_seconds=edit_seconds,
        )
    except BudgetExhaustedError as exc:
        ctx.manifest = ctx.store.transition(
            target=JobState.BUDGET_EXHAUSTED, note=f"edit refused: {exc.message}"
        )
        raise

    logger.info(
        "Applying edit",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segment": segment_index,
                "parent_interaction": artifact.interaction_id,
            }
        },
    )

    import asyncio

    try:
        edited = asyncio.run(
            ctx.provider.edit(
                artifact=artifact,
                edit_prompt=edit_prompt,
                spec=ctx.spec,
                segment_index=segment_index,
                attempt_index=attempt_index,
            )
        )
    except RequestTimeoutUnknownOutcome as exc:
        _record_unknown_outcome(
            ctx, segment_index=segment_index, attempt_index=attempt_index, exc=exc
        )
        ctx.manifest = ctx.store.transition(
            target=JobState.NEEDS_HUMAN,
            note="edit timed out with unknown outcome; resolve by querying, not by re-issuing",
        )
        raise
    except ProviderError as exc:
        _record_failed_interaction(
            ctx, segment_index=segment_index, attempt_index=attempt_index, exc=exc
        )
        ctx.error(f"edit failed: {exc}")
        raise

    persist_artifact(
        ctx,
        edited,
        segment_index=segment_index,
        attempt_index=attempt_index,
        parent=artifact,
        call_kind="edit",
    )
    ctx.note(
        f"segment {segment_index} edited from {artifact.interaction_id} → {edited.interaction_id}"
    )
    return RepairOutcome(artifact=edited, prompt=edit_prompt, mode="edit")


def run_regenerate(
    ctx: JobContext,
    *,
    segment: SegmentPlan,
    segment_index: int,
    attempt_index: int,
    previous_artifact: RenderArtifact | None,
    is_extension: bool,
) -> RepairOutcome:
    """Re-render a segment from scratch.

    An extension is regenerated as an extension (from the previous segment's
    anchor); a seed is regenerated as a seed. Falling back to a fresh independent
    render for a later segment would break the chain, which is the exact failure
    §24.3 forbids.
    """
    try:
        ctx.authorize(
            CallKind.REGENERATE,
            segment_index=segment_index,
            attempt_index=attempt_index,
            video_seconds=segment.intended_duration_s,
        )
    except BudgetExhaustedError as exc:
        ctx.manifest = ctx.store.transition(
            target=JobState.BUDGET_EXHAUSTED, note=f"regeneration refused: {exc.message}"
        )
        raise

    logger.info(
        "Regenerating segment",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segment": segment_index,
                "is_extension": is_extension,
                "attempt": attempt_index,
            }
        },
    )

    if is_extension:
        if previous_artifact is None:
            raise ProviderError(
                "Cannot regenerate an extension without the previous accepted "
                "segment; that would produce an independent clip (§24.3).",
                detail={"segment_index": segment_index},
            )
        prompt = ctx.compiler.compile_extend(
            segment,
            previous_anchor=_anchor_of(ctx, segment_index - 1),
            include_camera=True,
        )
        source = previous_artifact
        task = "extend"
    else:
        prompt = ctx.compiler.compile_seed(segment)
        source = None
        task = "seed"

    import asyncio

    try:
        if task == "seed":
            regenerated = asyncio.run(
                ctx.provider.generate_seed(
                    prompt=prompt.text,
                    assets=ctx.manifest.references,
                    spec=ctx.spec,
                    segment_index=segment_index,
                    attempt_index=attempt_index,
                    duration_s=segment.intended_duration_s,
                )
            )
        else:
            assert source is not None
            regenerated = asyncio.run(
                ctx.provider.extend(
                    artifact=source,
                    extension_prompt=prompt.text,
                    spec=ctx.spec,
                    segment_index=segment_index,
                    attempt_index=attempt_index,
                    duration_s=segment.intended_duration_s,
                )
            )
    except RequestTimeoutUnknownOutcome as exc:
        _record_unknown_outcome(
            ctx, segment_index=segment_index, attempt_index=attempt_index, exc=exc
        )
        ctx.manifest = ctx.store.transition(
            target=JobState.NEEDS_HUMAN,
            note="regeneration timed out with unknown outcome",
        )
        raise
    except ProviderError as exc:
        _record_failed_interaction(
            ctx, segment_index=segment_index, attempt_index=attempt_index, exc=exc
        )
        ctx.error(f"regeneration failed: {exc}")
        raise

    persist_artifact(
        ctx,
        regenerated,
        segment_index=segment_index,
        attempt_index=attempt_index,
        parent=source,
        call_kind="regenerate",
    )
    ctx.note(f"segment {segment_index} regenerated as {task}")
    return RepairOutcome(artifact=regenerated, prompt=prompt.text, mode="regenerate")


def try_edit_for_defects(
    ctx: JobContext,
    *,
    artifact: RenderArtifact,
    defects: list[str],
    segment_index: int,
    attempt_index: int,
) -> RepairOutcome | None:
    """Attempt an edit derived from Critic-reported defects.

    Returns None when no defect maps to a single local fix, so the caller can fall
    through to regeneration. Refusing here is the correct behaviour: an unmapped
    defect is more likely structural than local, and a guessed edit burns an
    attempt on the wrong remedy.
    """
    if len(defects) != 1:
        logger.info(
            "Not attempting an edit: the report lists "
            f"{len(defects)} defects, and §9.1 requires exactly one",
            extra={"extra_fields": {"segment": segment_index, "defects": defects}},
        )
        return None

    compiled = ctx.compiler.compile_edit_for_defect(defect=defects[0], segment_index=segment_index)
    if compiled is None:
        logger.info(
            "Not attempting an edit: the reported defect does not map to a single local correction",
            extra={"extra_fields": {"defect": defects[0]}},
        )
        return None

    return run_edit(
        ctx,
        artifact=artifact,
        edit_prompt=compiled.text,
        segment_index=segment_index,
        attempt_index=attempt_index,
    )


def _anchor_of(ctx: JobContext, segment_index: int) -> str | None:
    for segment in ctx.segment_plan:
        if segment.index == segment_index:
            return segment.continuation_anchor
    return None
