"""Planning stage: brief → ContinuityBible + SegmentPlan (§4.1).

Wraps the Director with job-side concerns: persisting the plan trio, writing it
into the manifest, and deciding what to do when the Director fails.

On Director failure the default is to stop with a clear error rather than fall
back to the built-in §10 template. Silently substituting a generic plan would mean
the user's brief was quietly ignored, and they would only find out by watching the
video. The fallback exists, but an operator has to ask for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from omni_homevlog.agents.director import PlanRejectedError, default_plan_for_spec
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext, write_plan_artifacts
from omni_homevlog.schemas import ContinuityBible, JobState, SegmentPlan

logger = get_logger("plan")


@dataclass(slots=True)
class PlanOutcome:
    bible: ContinuityBible
    segments: list[SegmentPlan]
    warnings: list[str]
    used_fallback: bool = False
    model: str | None = None


DEFAULT_BIBLE = ContinuityBible(
    subject_identity="a clearly adult woman, consistent across every segment",
    immutable_face_traits=["stable facial structure", "natural skin texture"],
    hair=["long dark hair, consistent length and parting"],
    accessories=[],
    outfit=["the same casual home outfit throughout"],
    body_proportion=["consistent height and build"],
    environment_topology=["a warm apartment with a dining area and a bedroom"],
    lighting=["warm indoor lighting, consistent direction and colour temperature"],
    camera_grammar=[
        "handheld phone at close conversational distance",
        "subtle natural sway, no cut",
    ],
    interaction_style=["relaxed, spontaneous, unposed"],
)


def run_plan(
    ctx: JobContext,
    *,
    allow_template_fallback: bool = False,
    reference_descriptions: list[str] | None = None,
) -> PlanOutcome:
    """Produce and persist the plan."""
    logger.info("Planning", extra={"extra_fields": {"job_id": ctx.job_id}})

    try:
        result = ctx.director.plan(
            spec=ctx.spec,
            references=ctx.manifest.references,
            reference_descriptions=reference_descriptions,
        )
        bible = result.plan.continuity_bible
        segments = result.plan.segments
        warnings = list(result.warnings)
        model = result.model
        used_fallback = False
    except PlanRejectedError as exc:
        if not allow_template_fallback:
            ctx.error(f"Director failed: {exc.message}")
            raise
        logger.warning(
            "Director failed; falling back to the §10 template because the caller "
            "explicitly allowed it",
            extra={"extra_fields": {"error": exc.message}},
        )
        bible = DEFAULT_BIBLE
        segments = default_plan_for_spec(ctx.spec, bible).segments
        warnings = [
            f"Director failed ({exc.message}); the built-in §10 template was used "
            "instead, which does NOT reflect your brief"
        ]
        model = None
        used_fallback = True

    # The manifest and the on-disk plan trio are both written; §13.1 expects the
    # files, §5 expects the manifest.
    write_plan_artifacts(ctx.paths, spec=ctx.spec, bible=bible, segments=segments)
    ctx.manifest = ctx.store.mutate(continuity_bible=bible, segment_plan=segments)
    for warning in warnings:
        ctx.note(warning)

    ctx.mirror("plans")

    if ctx.manifest.state is JobState.REFERENCES_VALIDATED:
        ctx.manifest = ctx.store.transition(
            target=JobState.PLAN_READY, note=f"planned {len(segments)} segments"
        )

    logger.info(
        "Plan ready",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segments": len(segments),
                "used_fallback": used_fallback,
            }
        },
    )
    return PlanOutcome(
        bible=bible,
        segments=segments,
        warnings=warnings,
        used_fallback=used_fallback,
        model=model,
    )


def describe_plan(segments: list[SegmentPlan]) -> str:
    lines: list[str] = []
    for segment in segments:
        lines.append(
            f"segment {segment.index} ({segment.intended_duration_s}s, "
            f"{segment.environment}): {segment.action}"
        )
        lines.append(f"    anchor: {segment.continuation_anchor}")
    return "\n".join(lines)
