"""Extension stage (§18).

Adds one native extension to the accepted chain. §18's exit criteria are the
contract this module has to satisfy:

  * the 30-second result is not three independent segments
  * segment N+1 begins naturally from segment N's ending
  * person, outfit, room, and lighting do not visibly reset
  * the manifest can reconstruct the whole lineage

The extension is always fed the *previous accepted* artifact. Never the seed, and
never a freshly generated clip — extending from the wrong parent is how a chain
silently becomes a montage.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from omni_homevlog.budget import CallKind
from omni_homevlog.errors import (
    BudgetExhaustedError,
    CapabilityMissingError,
    JobNotFoundError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.render_seed import _persist as persist_artifact
from omni_homevlog.pipeline.render_seed import (
    _record_failed_interaction,
    _record_unknown_outcome,
    record_dispatch_pending,
)
from omni_homevlog.schemas import JobState, RenderArtifact, SegmentPlan
from omni_homevlog.state_machine import (
    extension_accepted_state,
    extension_rendering_state,
    extension_review_state,
)

logger = get_logger("extend")


@dataclass(slots=True)
class ExtensionOutcome:
    artifact: RenderArtifact
    prompt: str
    strategy: str
    segment_index: int


def run_extend(
    ctx: JobContext,
    *,
    segment_index: int,
    previous_artifact: RenderArtifact,
    attempt_index: int = 0,
) -> ExtensionOutcome:
    """Render one extension of `previous_artifact`."""
    segment = _segment(ctx, segment_index)
    if segment is None:
        raise JobNotFoundError(f"The plan has no segment {segment_index}; cannot extend beyond it.")

    # §8.3 strategy D: if the surface cannot chain, refuse rather than improvise.
    strategy_fn = getattr(ctx.provider, "chain_strategy", None)
    strategy: str = str(strategy_fn(ctx.capabilities)) if callable(strategy_fn) else "C"
    logger.info(
        "Extension strategy in use",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segment": segment_index,
                "strategy": strategy,
            }
        },
    )

    # Same gate for extensions, so `each-segment` means every segment.
    from omni_homevlog.pipeline.render_seed import require_human_approval

    require_human_approval(ctx, f"segment-{segment_index}")

    if not previous_artifact.is_usable_video():
        raise CapabilityMissingError(
            f"Cannot extend from {previous_artifact.interaction_id}: it has no usable video.",
            detail={"interaction_id": previous_artifact.interaction_id},
        )

    ctx.manifest = ctx.store.transition(
        target=extension_rendering_state(segment_index),
        note=f"extending from {previous_artifact.interaction_id} (strategy {strategy})",
    )

    previous_anchor = _anchor_of(ctx, segment_index - 1)
    compiled = ctx.compiler.compile_extend(
        segment, previous_anchor=previous_anchor, include_camera=True
    )
    for warning in compiled.warnings:
        ctx.note(warning)

    try:
        ctx.authorize(
            CallKind.EXTEND,
            segment_index=segment_index,
            attempt_index=attempt_index,
            video_seconds=segment.intended_duration_s,
        )
    except BudgetExhaustedError as exc:
        ctx.manifest = ctx.store.transition(
            target=JobState.BUDGET_EXHAUSTED, note=f"extension refused: {exc.message}"
        )
        raise

    logger.info(
        "Rendering extension",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segment": segment_index,
                "from": previous_artifact.interaction_id,
                "duration_s": segment.intended_duration_s,
            }
        },
    )

    record_dispatch_pending(
        ctx,
        segment_index=segment_index,
        attempt_index=attempt_index,
        task="extend",
        parent_interaction_id=previous_artifact.interaction_id,
    )

    import asyncio

    try:
        artifact = asyncio.run(
            ctx.provider.extend(
                artifact=previous_artifact,
                extension_prompt=compiled.text,
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
        if exc.code == "interaction_pending":
            raise
        ctx.manifest = ctx.store.transition(
            target=JobState.NEEDS_HUMAN,
            note=(
                "extension timed out with unknown outcome; it may be billable. "
                "Resolve by querying with `omni-vlog resume`."
            ),
        )
        raise
    except ProviderError as exc:
        _record_failed_interaction(
            ctx, segment_index=segment_index, attempt_index=attempt_index, exc=exc
        )
        ctx.error(f"extension failed: {exc}")
        raise

    persist_artifact(
        ctx,
        artifact,
        segment_index=segment_index,
        attempt_index=attempt_index,
        parent=previous_artifact,
        call_kind="extend",
    )
    ctx.note(
        f"segment {segment_index} extended: {previous_artifact.interaction_id} "
        f"→ {artifact.interaction_id} (strategy {strategy})"
    )

    ctx.manifest = ctx.store.transition(
        target=extension_review_state(segment_index),
        note=f"extension {segment_index} rendered; awaiting review",
    )

    return ExtensionOutcome(
        artifact=artifact,
        prompt=compiled.text,
        strategy=strategy,
        segment_index=segment_index,
    )


def mark_accepted(ctx: JobContext, *, segment_index: int) -> None:
    """Record that an extension was accepted, so the chain may continue."""
    ctx.manifest = ctx.store.transition(
        target=extension_accepted_state(segment_index),
        note=f"extension {segment_index} accepted",
    )


def _has_accepted(ctx: JobContext, segment_index: int) -> bool:
    target = str(extension_accepted_state(segment_index))
    return any(entry.get("to") == target for entry in ctx.manifest.state_history)


def _segment(ctx: JobContext, segment_index: int) -> SegmentPlan | None:
    for segment in ctx.segment_plan:
        if segment.index == segment_index:
            return segment
    return None


def _anchor_of(ctx: JobContext, segment_index: int) -> str | None:
    segment = _segment(ctx, segment_index)
    return segment.continuation_anchor if segment else None


def previous_accepted_artifact(ctx: JobContext) -> RenderArtifact | None:
    """The artifact an extension must build on: the most recent usable one."""
    return ctx.manifest.last_usable_artifact()


def chain_summary(ctx: JobContext) -> list[dict[str, object]]:
    """Lineage view used by `omni-vlog status` (§13.2)."""
    return [
        {
            "index": index,
            "interaction_id": artifact.interaction_id,
            "parent": artifact.parent_interaction_id,
            "task": str(artifact.task),
            "status": artifact.status,
            "duration_s": artifact.media.duration_s if artifact.media else None,
            "resolution": artifact.resolution,
            "local_path": artifact.local_path,
            "gcs_uri": artifact.gcs_uri,
            "derived": artifact.derived,
        }
        for index, artifact in enumerate(ctx.manifest.segments)
    ]


def chain_total_seconds(ctx: JobContext) -> float:
    """Duration of the latest cumulative film, not the sum of its versions.

    Requested generation seconds are accounted for separately by the budget.
    """
    artifact = ctx.manifest.last_usable_artifact()
    if artifact is not None and artifact.media is not None:
        return artifact.media.duration_s or 0.0
    return 0.0


def verify_chain_continuity(ctx: JobContext) -> tuple[bool, list[str]]:
    """Check the lineage is a single parent-linked chain (§18 exit criteria).

    This is the structural half of "the 30-second video is not three independent
    segments": every artifact after the first must name the previous one as its
    parent. Whether the *content* is continuous is the Critic's job; whether the
    *lineage* is a chain is checkable here, deterministically.
    """
    problems: list[str] = []
    artifacts = ctx.manifest.segment_artifacts()
    if not artifacts:
        return False, ["no usable segments"]

    for previous, current in itertools.pairwise(artifacts):
        parent = current.parent_interaction_id
        records = {r.interaction_id: r for r in ctx.manifest.interactions}
        seen: set[str] = set()
        while parent != previous.interaction_id and parent in records and parent not in seen:
            seen.add(parent)
            record = records[parent]
            if record.segment_index != current.segment_index:
                break
            parent = record.parent_interaction_id
        if parent != previous.interaction_id:
            problems.append(
                f"segment {current.interaction_id} names parent "
                f"{current.parent_interaction_id!r} but follows "
                f"{previous.interaction_id!r}; the chain is not linear"
            )
        if current.task not in ("extend", "edit"):
            problems.append(
                f"segment {current.interaction_id} has task {current.task!r}; only an "
                "extension or an edit may participate in a native chain"
            )

    # A seed that was *edited* is still the head of the chain, and the chain is
    # still continuous: the edit names the original seed as its parent, so the
    # lineage reaches back to the fresh render. Rejecting it bricked the job at
    # finalize: the seed review can legitimately choose EDIT for a local defect,
    # and the whole paid chain would then complete on disk while the job could
    # never reach COMPLETE.
    #
    # What does matter is that the head's *parent* is a render that is not itself
    # part of the chain — otherwise an edit could be the whole chain, with no seed
    # underneath it.
    head = artifacts[0]
    if head.task == "edit" and not head.parent_interaction_id:
        problems.append(
            "the chain starts with an edit that names no parent, so there is no seed underneath it"
        )

    expected_count = 1 if ctx.spec.mode == "concept" else ctx.spec.extension_count + 1
    if len(artifacts) != expected_count:
        problems.append(f"expected {expected_count} accepted segments, got {len(artifacts)}")
    for index, artifact in enumerate(artifacts):
        if (artifact.provider, artifact.project, artifact.model) != (
            ctx.manifest.provider,
            ctx.manifest.project,
            ctx.manifest.model,
        ):
            problems.append(f"segment {index} changed the pinned provider/project/model")
        expected = ctx.spec.concept_duration_s if ctx.spec.mode == "concept" else (index + 1) * 10
        actual = artifact.media.duration_s if artifact.media else None
        if actual is None or abs(actual - expected) > 0.6:
            problems.append(f"segment {index} cumulative duration {actual}, expected {expected}s")
    return (not problems), problems
