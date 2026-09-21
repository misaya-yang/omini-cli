"""Review stage (§11).

Runs the Critic over a rendered segment, persists the report, and hands the
decision to the deterministic policy.

The split is deliberate and load-bearing:

    review.py     gathers evidence and asks the Critic what it sees
    decision_policy.py  decides what to do, in pure Python

`review.py` calls the policy and records the result; it never acts on the Critic's
own `verdict`. §24.8 forbids letting model prose trigger a paid call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omni_homevlog.agents.critic import CriticResult
from omni_homevlog.agents.decision_policy import Decision, DecisionResult, decide_segment
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.schemas import CritiqueReport, RenderArtifact, SegmentPlan, utc_now_iso
from omni_homevlog.storage.local import atomic_write_json

logger = get_logger("review")


@dataclass(slots=True)
class SegmentReview:
    report: CritiqueReport
    decision: DecisionResult
    critic: CriticResult | None = None
    report_path: str | None = None

    @property
    def accepted(self) -> bool:
        return self.decision.decision in (Decision.ACCEPT, Decision.EXTEND)


def review_segment(
    ctx: JobContext,
    *,
    artifact: RenderArtifact,
    segment: SegmentPlan,
    segment_index: int,
    attempt_index: int,
    is_extension: bool,
    previous_artifact: RenderArtifact | None = None,
    prompt_text: str | None = None,
) -> SegmentReview:
    """Review one rendered segment and derive a decision."""
    video_path = Path(artifact.local_path) if artifact.local_path else None

    critic_result = ctx.critic.review_video(
        segment=segment,
        bible=ctx.bible,
        video_path=video_path if video_path else "",
        is_extension=is_extension,
        previous_video_path=(
            Path(previous_artifact.local_path)
            if previous_artifact and previous_artifact.local_path
            else None
        ),
        reference_assets=ctx.manifest.references,
        segment_prompt=prompt_text,
        frames_dir=ctx.paths.segment_dir(segment_index) / "review_frames",
    )

    report = critic_result.report

    # The capability gates: the policy must not recommend an action the surface
    # cannot perform (§8.3 strategy D).
    caps = ctx.capabilities
    can_edit = bool(caps.edit) if caps is not None else True
    can_extend = (
        bool(caps.extend or caps.stateful_previous_interaction_id) if caps is not None else True
    )

    decision = decide_segment(
        report=report,
        segment=segment,
        budget=ctx.budget,
        segment_index=segment_index,
        attempt_index=attempt_index,
        is_extension=is_extension,
        thresholds=ctx.thresholds,
        has_capability_to_edit=can_edit,
        has_capability_to_extend=can_extend,
        anchor_confirmed=bool(segment.continuation_anchor.strip()),
    )

    report_path = _persist(
        ctx,
        report=report,
        decision=decision,
        segment_index=segment_index,
        attempt_index=attempt_index,
        critic_model=critic_result.report.critic_model,
        contradictions=critic_result.contradictions,
    )

    ctx.db.save_review(
        ctx.job_id,
        segment_index=segment_index,
        attempt_index=attempt_index,
        report=report,
        kind="segment",
    )

    logger.info(
        "Segment reviewed",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "segment": segment_index,
                "decision": str(decision.decision),
                "identity": report.identity_score,
                "degraded": report.critic_degraded,
                "critic_agrees": decision.critic_verdict_agrees,
            }
        },
    )

    return SegmentReview(
        report=report, decision=decision, critic=critic_result, report_path=report_path
    )


def _persist(
    ctx: JobContext,
    *,
    report: CritiqueReport,
    decision: DecisionResult,
    segment_index: int,
    attempt_index: int,
    critic_model: str,
    contradictions: list[str],
) -> str:
    """Write the review to disk and into the manifest."""
    payload = {
        "segment_index": segment_index,
        "attempt_index": attempt_index,
        "reviewed_at": utc_now_iso(),
        "critic_model": critic_model,
        "report": report.model_dump(mode="json"),
        "decision": {
            "decision": str(decision.decision),
            "reasons": decision.reasons,
            "blocking": decision.blocking,
            "suggested_edit": decision.suggested_edit,
            "critic_verdict_agrees": decision.critic_verdict_agrees,
        },
        "thresholds_used": decision.thresholds_used,
        "contradictions": contradictions,
    }
    path = ctx.paths.review_path(segment_index, attempt_index)
    atomic_write_json(path, payload)

    ctx.manifest = ctx.store.record_quality_report(payload)
    ctx.mirror("reviews")
    return str(path)


def review_final(ctx: JobContext) -> dict[str, Any]:
    """Assemble the final review from the per-segment reviews (§11.1)."""
    from omni_homevlog.agents.decision_policy import decide_final

    reports: list[CritiqueReport] = []
    for entry in ctx.manifest.quality_reports:
        raw = entry.get("report")
        if isinstance(raw, dict):
            try:
                reports.append(CritiqueReport.model_validate(raw))
            except Exception:
                logger.warning("Skipping an unparsable stored review entry")

    decision = decide_final(
        reports=reports,
        budget=ctx.budget,
        thresholds=ctx.thresholds,
        # The gate that applies here is `final` specifically. `bool(human_gates)`
        # is true for the *default* ['high-res'] set, so every job reported
        # "the final approval gate is enabled" whether or not anyone had asked for
        # one — and the decision was discarded anyway (see orchestrator._finalize).
        require_human_gate="final" in ctx.spec.human_gates,
    )

    payload: dict[str, Any] = {
        "reviewed_at": utc_now_iso(),
        "segment_count": len(reports),
        "decision": {
            "decision": str(decision.decision),
            "reasons": decision.reasons,
            "blocking": decision.blocking,
            "gate_only": decision.gate_only,
        },
        "segments": [
            {
                "index": i,
                "identity": r.identity_score,
                "anatomy": r.anatomy_score,
                "continuity": r.continuity_with_previous_score,
                "anchor_usable": r.anchor_usable,
                "degraded": r.critic_degraded,
                "verdict": r.verdict,
            }
            for i, r in enumerate(reports)
        ],
    }
    atomic_write_json(ctx.paths.final_review_path(), payload)
    return payload
