"""The pipeline orchestrator.

Drives the job forward from whatever state it is in, stopping at every human gate
and every ceiling. This is the only place that sequences stages; the stages
themselves know nothing about each other.

Stopping conditions, all of them deliberate:

  * a human gate in the spec (`high-res`, `final`, `each-segment`)
  * a `NEEDS_HUMAN` decision from the policy
  * a `BUDGET_EXHAUSTED` refusal
  * an unknown-outcome interaction (which dominates everything — see `resume`)
  * two consecutive `REGENERATE` decisions on the same segment

The last one deserves a note. The budget already caps regenerations, but a loop
that alternates edit→regenerate→edit would still burn calls slowly while looking
like progress. Two consecutive regenerations of the same segment is treated as
evidence that the plan, not the render, is the problem, and the job goes to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from omni_homevlog.agents.decision_policy import Decision
from omni_homevlog.errors import (
    BudgetExhaustedError,
    HumanGateError,
    OmniVlogError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.extend import (
    mark_accepted,
    previous_accepted_artifact,
    run_extend,
)
from omni_homevlog.pipeline.finalize import finalize, mark_complete
from omni_homevlog.pipeline.plan import run_plan
from omni_homevlog.pipeline.render_seed import run_render_seed
from omni_homevlog.pipeline.repair import run_regenerate, try_edit_for_defects
from omni_homevlog.pipeline.review import review_final, review_segment
from omni_homevlog.schemas import (
    HUMAN_GATE_STATES,
    JobState,
    RenderArtifact,
    SegmentPlan,
    extension_accepted_state,
)

logger = get_logger("orchestrator")

#: How many times the same segment may be regenerated before we stop and ask.
MAX_CONSECUTIVE_REGENERATIONS = 2


@dataclass
class RunReport:
    job_id: str
    final_state: JobState
    stages_run: list[str] = field(default_factory=list)
    stopped_because: str = ""
    errors: list[str] = field(default_factory=list)
    final_path: str | None = None
    completed: bool = False

    def summary(self) -> str:
        lines = [
            f"job        {self.job_id}",
            f"final state {self.final_state}",
            f"stages     {', '.join(self.stages_run) or '(none)'}",
        ]
        if self.stopped_because:
            lines.append(f"stopped    {self.stopped_because}")
        if self.final_path:
            lines.append(f"output     {self.final_path}")
        for error in self.errors:
            lines.append(f"  error: {error}")
        return "\n".join(lines)


class Orchestrator:
    """Runs a job forward until it stops."""

    def __init__(
        self,
        ctx: JobContext,
        *,
        allow_template_fallback: bool = False,
        max_stages: int = 40,
    ) -> None:
        self.ctx = ctx
        self.allow_template_fallback = allow_template_fallback
        # A hard loop bound, independent of budget. Budget counts *paid* calls;
        # this counts stages, so a bug that loops without spending is still caught.
        self.max_stages = max_stages
        self.report = RunReport(job_id=ctx.job_id, final_state=ctx.manifest.state)

    # ─ entry point ─────────────────────────────────────────────────────────

    def run(self) -> RunReport:
        """Drive the job as far as it will go."""
        from omni_homevlog.storage.locking import job_lock

        with job_lock(self.ctx.settings.data_dir() / "locks" / f"{self.ctx.job_id}.lock"):
            self.ctx.reload()
            from omni_homevlog.pipeline.context import budget_from_manifest

            self.ctx.budget = budget_from_manifest(self.ctx.manifest, self.ctx.spec)
            return self._run_locked()

    def _run_locked(self) -> RunReport:
        if any(not r.outcome_known for r in self.ctx.manifest.interactions):
            self.report.stopped_because = (
                "Resolve pending interactions with resume before any generation."
            )
            return self.report
        stages = 0
        while stages < self.max_stages:
            stages += 1
            state = self.ctx.reload().state

            if state in HUMAN_GATE_STATES:
                self.report.stopped_because = f"state {state} needs a human decision"
                break
            if state is JobState.COMPLETE:
                self.report.completed = True
                self.report.stopped_because = "already complete"
                break
            if state in (JobState.FAILED_FINAL, JobState.POLICY_BLOCKED):
                self.report.stopped_because = f"terminal state {state}"
                break

            try:
                advanced = self._step(state)
            except HumanGateError as exc:
                self.report.stopped_because = str(exc)
                break
            except BudgetExhaustedError as exc:
                self.report.stopped_because = f"budget: {exc.message}"
                break
            except RequestTimeoutUnknownOutcome as exc:
                if exc.code == "interaction_pending":
                    self.report.stopped_because = f"Background render {exc.interaction_id} is running; use resume."
                    break
                self.report.stopped_because = (
                    "an unknown-outcome request needs resolving before anything else "
                    f"runs: {exc.message}"
                )
                self.report.errors.append(exc.message)
                break
            except OmniVlogError as exc:
                self.report.errors.append(str(exc))
                self.report.stopped_because = f"error: {exc.message}"
                break

            if not advanced:
                self.report.stopped_because = f"no progress from state {state}"
                break

        else:
            self.report.stopped_because = (
                f"stopped after {self.max_stages} stages; this is a loop bound, not a "
                "normal exit — inspect the manifest"
            )

        self.report.final_state = self.ctx.reload().state
        self.report.final_path = self.ctx.manifest.final_path
        return self.report

    # ─ step dispatch ───────────────────────────────────────────────────────

    def _step(self, state: JobState) -> bool:
        handler = {
            JobState.CREATED: self._validate_references,
            JobState.REFERENCES_VALIDATED: self._plan,
            JobState.PLAN_READY: self._render_seed,
            JobState.SEED_RENDERING: self._render_seed,
            JobState.SEED_REVIEW: self._review_seed,
            JobState.SEED_ACCEPTED: self._extend_or_finalize,
            JobState.FINAL_REVIEW: self._finalize,
        }.get(state)

        if handler is None:
            handler = self._extension_step(state)

        if handler is None:
            return False
        return bool(handler())

    def _extension_step(self, state: JobState) -> Any:
        """Handlers for the EXTENSION_n_* family."""
        name = str(state)
        for i in (1, 2, 3):
            if name == f"EXTENSION_{i}_RENDERING":
                return lambda index=i: self._render_extension(index)
            if name == f"EXTENSION_{i}_REVIEW":
                return lambda index=i: self._review_extension(index)
            if name == f"EXTENSION_{i}_ACCEPTED":
                return lambda index=i: self._after_extension_accepted(index)
        return None

    #  stages ──────────────────────────────────────────────────────────────

    def _validate_references(self) -> bool:
        """References were already validated at create time; record the transition."""
        if self.ctx.manifest.state is not JobState.CREATED:
            return False
        self.ctx.manifest = self.ctx.store.transition(
            target=JobState.REFERENCES_VALIDATED,
            note=f"{len(self.ctx.manifest.references)} references accepted",
        )
        self.report.stages_run.append("references_validated")
        return True

    def _plan(self) -> bool:
        outcome = run_plan(self.ctx, allow_template_fallback=self.allow_template_fallback)
        self.report.stages_run.append("plan")
        if outcome.used_fallback:
            self.report.errors.append(
                "Director failed; the built-in template was used and does NOT reflect the brief"
            )
        return True

    def _render_seed(self) -> bool:
        outcome = run_render_seed(self.ctx)
        self.report.stages_run.append("render_seed")
        self._last_seed = outcome
        return True

    def _review_seed(self) -> bool:
        segment = self._segment(0)
        artifact = self._latest_artifact_for_segment(0)
        if segment is None or artifact is None:
            self.ctx.error("cannot review the seed: no segment or no artifact")
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN, note="seed review had nothing to review"
            )
            return True

        result = review_segment(
            self.ctx,
            artifact=artifact,
            segment=segment,
            segment_index=0,
            # The attempt this artifact came from, not a hardcoded 0. Writing every
            # review as attempt_00 overwrote the previous attempt's JSON and DB row,
            # so the history of what was tried and why it failed was lost.
            attempt_index=_attempt_of(self.ctx, artifact, 0),
            is_extension=False,
        )
        self.report.stages_run.append("review_seed")
        return self._act_on_decision(
            result.decision.decision,
            segment=segment,
            segment_index=0,
            artifact=artifact,
            is_extension=False,
            previous_artifact=None,
        )

    def _extend_or_finalize(self) -> bool:
        """From SEED_ACCEPTED: extend if the plan asks for it, else finalize."""
        next_index = self._next_extension_index()
        if next_index > self.ctx.spec.extension_count:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.FINAL_REVIEW, note="all extensions complete"
            )
            return True
        return self._render_extension(next_index)

    def _render_extension(self, index: int) -> bool:
        previous = previous_accepted_artifact(self.ctx)
        if previous is None:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN,
                note=f"cannot extend to segment {index}: no accepted predecessor",
            )
            return True
        run_extend(self.ctx, segment_index=index, previous_artifact=previous)
        self.report.stages_run.append(f"render_extension_{index}")
        return True

    def _review_extension(self, index: int) -> bool:
        segment = self._segment(index)
        artifact = self._latest_artifact_for_segment(index)
        previous = self._latest_artifact_for_segment(index - 1)
        if segment is None or artifact is None:
            self.ctx.error(f"cannot review extension {index}: missing segment or artifact")
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN, note=f"extension {index} had nothing to review"
            )
            return True

        result = review_segment(
            self.ctx,
            artifact=artifact,
            segment=segment,
            segment_index=index,
            attempt_index=_attempt_of(self.ctx, artifact, index),
            is_extension=True,
            previous_artifact=previous,
        )
        self.report.stages_run.append(f"review_extension_{index}")
        return self._act_on_decision(
            result.decision.decision,
            segment=segment,
            segment_index=index,
            artifact=artifact,
            is_extension=True,
            previous_artifact=previous,
        )

    def _after_extension_accepted(self, index: int) -> bool:
        if index >= self.ctx.spec.extension_count:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.FINAL_REVIEW,
                note=f"extension {index} accepted; chain complete",
            )
        else:
            return self._render_extension(index + 1)
        return True

    def _finalize(self) -> bool:
        outcome = finalize(self.ctx)
        self.report.stages_run.append("finalize")
        final_review = review_final(self.ctx)
        decision = (final_review.get("decision") or {}).get("decision")

        if not outcome.continuity_ok:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN,
                note=("chain continuity check failed: " + "; ".join(outcome.continuity_problems)),
            )
            return True

        # `review_final` decides whether the chain may be exported; discarding its
        # return value meant the orchestrator completed jobs whose own
        # `final_review.json` recorded HUMAN_REVIEW — including one that recorded a
        # hard-reject defect.
        decision_block = final_review.get("decision") or {}
        if decision == "HUMAN_REVIEW" and not decision_block.get("gate_only"):
            # A quality problem, not a pending approval. Stop and bring someone in.
            reasons = decision_block.get("reasons") or []
            self.ctx.note(
                "the final review did not clear: " + "; ".join(str(r) for r in list(reasons)[:3])
            )
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN,
                note="final review requires a human decision",
            )
            return True

        if not self._human_gate_cleared("final"):
            self.ctx.note(
                "final approval gate is enabled; run `omni-vlog approve "
                f"{self.ctx.job_id} --stage final` then `omni-vlog resume` to complete"
            )
            return False

        mark_complete(self.ctx)
        return True

    #  decision handling ───────────────────────────────────────────────────

    def _act_on_decision(
        self,
        decision: Decision,
        *,
        segment: SegmentPlan,
        segment_index: int,
        artifact: RenderArtifact,
        is_extension: bool,
        previous_artifact: RenderArtifact | None,
    ) -> bool:
        """Execute a policy decision, or hand it to a human."""
        if decision is Decision.ACCEPT:
            self._mark_segment_accepted(segment_index, is_extension=is_extension)
            return True

        if decision is Decision.EXTEND:
            self._mark_segment_accepted(segment_index, is_extension=is_extension)
            if segment_index >= self.ctx.spec.extension_count:
                self.ctx.manifest = self.ctx.store.transition(
                    target=JobState.FINAL_REVIEW, note="chain complete"
                )
            else:
                self._render_extension(segment_index + 1)
            return True

        if decision is Decision.HUMAN_REVIEW:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN,
                note=f"segment {segment_index}: the policy asked for a human decision",
            )
            return True

        regeneration_count = self._regenerations_for(segment_index)
        if regeneration_count >= MAX_CONSECUTIVE_REGENERATIONS:
            self.ctx.note(
                f"segment {segment_index} has been regenerated {regeneration_count} "
                "times; the plan may be the problem rather than the render"
            )
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.NEEDS_HUMAN,
                note=(
                    f"segment {segment_index} regenerated {regeneration_count} times "
                    "without clearing review"
                ),
            )
            return True

        if decision is Decision.EDIT:
            review = (
                self.ctx.manifest.quality_reports[-1] if self.ctx.manifest.quality_reports else {}
            )
            report = review.get("report") or {}
            defects = list(report.get("editable_defects") or [])
            suggested = (review.get("decision") or {}).get("suggested_edit")

            if defects:
                outcome = try_edit_for_defects(
                    self.ctx,
                    artifact=artifact,
                    defects=defects,
                    segment_index=segment_index,
                    attempt_index=regeneration_count + 1,
                )
            else:
                outcome = None

            if outcome is None and suggested:
                # `suggested` is the Critic's raw prose. It has to pass through the
                # compiler, which wraps it in the preservation template and runs the
                # §9 guardrails, before it can reach a paid call (§24.8). Sending it
                # straight to `run_edit` was the direct violation: no template, no
                # single-fix check, no timecode or montage filtering.
                compiled_edit = self.ctx.compiler.compile_edit_from_suggestion(
                    suggestion=suggested, segment_index=segment_index
                )
                if compiled_edit is not None:
                    from omni_homevlog.pipeline.repair import run_edit

                    outcome = run_edit(
                        self.ctx,
                        artifact=artifact,
                        edit_prompt=compiled_edit.text,
                        segment_index=segment_index,
                        attempt_index=regeneration_count + 1,
                    )
                else:
                    self.ctx.note(
                        f"segment {segment_index}: the Critic suggested an edit that "
                        "does not satisfy the §9 prompt rules, so it was not sent; "
                        "falling through to regeneration"
                    )

            if outcome is None:
                self.ctx.note(
                    f"segment {segment_index}: an edit was recommended but no single "
                    "local fix could be derived; falling through to regeneration"
                )
                run_regenerate(
                    self.ctx,
                    segment=segment,
                    segment_index=segment_index,
                    attempt_index=regeneration_count + 1,
                    previous_artifact=previous_artifact,
                    is_extension=is_extension,
                )
            else:
                self.report.stages_run.append(f"edit_segment_{segment_index}")

            self._return_to_review(segment_index, is_extension=is_extension)
            return True

        if decision is Decision.REGENERATE:
            run_regenerate(
                self.ctx,
                segment=segment,
                segment_index=segment_index,
                attempt_index=regeneration_count + 1,
                previous_artifact=previous_artifact,
                is_extension=is_extension,
            )
            self.report.stages_run.append(f"regenerate_segment_{segment_index}")
            self._return_to_review(segment_index, is_extension=is_extension)
            return True

        # Every Decision member is handled above, so reaching here means a new one
        # was added without a policy branch. Better to say so than to fall through.
        raise OmniVlogError(
            f"The decision policy returned an unhandled decision: {decision!r}. "
            "This is a bug in decision_policy or in this orchestrator.",
            detail={"decision": str(decision), "segment_index": segment_index},
        )

    def _return_to_review(self, segment_index: int, *, is_extension: bool) -> None:
        target = (
            JobState(f"EXTENSION_{segment_index}_REVIEW") if is_extension else JobState.SEED_REVIEW
        )
        from omni_homevlog.state_machine import legal_transitions

        current = self.ctx.manifest.state
        if target in legal_transitions(current, self.ctx.spec.extension_count):
            self.ctx.manifest = self.ctx.store.transition(
                target=target, note=f"segment {segment_index} repaired; re-reviewing"
            )
        else:
            # The repair happened while in a *_RENDERING state (from a resume), so
            # the machine is already where it needs to be.
            logger.debug(
                "Repair left the job in a state that already leads to review",
                extra={"extra_fields": {"state": str(current), "target": str(target)}},
            )

    def _mark_segment_accepted(self, segment_index: int, *, is_extension: bool) -> None:
        if not is_extension:
            self.ctx.manifest = self.ctx.store.transition(
                target=JobState.SEED_ACCEPTED, note="seed accepted"
            )
            return
        mark_accepted(self.ctx, segment_index=segment_index)

    # ─ helpers ─────────────────────────────────────────────────────────────

    def _segment(self, index: int) -> SegmentPlan | None:
        return next((s for s in self.ctx.segment_plan if s.index == index), None)

    def _latest_artifact_for_segment(self, index: int) -> RenderArtifact | None:
        candidates = [
            a for a in self.ctx.manifest.segment_artifacts() if _segment_index_of(a) == index
        ]
        return candidates[-1] if candidates else None

    def _next_extension_index(self) -> int:
        accepted = 0
        for i in range(1, self.ctx.spec.extension_count + 1):
            target = str(extension_accepted_state(i))
            if any(e.get("to") == target for e in self.ctx.manifest.state_history):
                accepted = i
        return accepted + 1

    def _regenerations_for(self, segment_index: int) -> int:
        """How many times this segment has already been repaired.

        Counted from the budget, which is reconstructed from the manifest on
        resume — so a restart cannot reset the counter.
        """
        from omni_homevlog.budget import CallKind

        return self.ctx.budget.count(CallKind.REGENERATE, segment_index) + self.ctx.budget.count(
            CallKind.EDIT, segment_index
        )

    def _human_gate_cleared(self, stage: str) -> bool:
        gates = self.ctx.spec.human_gates
        if stage == "final" and "final" not in gates:
            return True
        latest = self.ctx.manifest.last_usable_artifact()
        approvals = [
            entry
            for entry in self.ctx.manifest.state_history
            if entry.get("note", "").split(" — ", 1)[0] == f"approved:{stage}"
            and (stage != "final" or not entry.get("artifact_id") or
                 (latest is not None and entry["artifact_id"] == latest.interaction_id))
        ]
        return bool(approvals)


def _segment_index_of(artifact: RenderArtifact) -> int:
    return artifact.segment_index


def _attempt_of(ctx: JobContext, artifact: RenderArtifact, segment_index: int) -> int:
    """Which attempt this artifact represents.

    Read from the ledger rather than assumed: the ledger row for this interaction
    records the attempt it was dispatched as.
    """
    for record in ctx.manifest.interactions:
        if record.interaction_id == artifact.interaction_id:
            return record.attempt_index
    for index, stored in enumerate(ctx.manifest.segments):
        if stored.interaction_id == artifact.interaction_id:
            return index
    _ = segment_index
    return 0


def run_job(
    ctx: JobContext,
    *,
    allow_template_fallback: bool = False,
) -> RunReport:
    """Convenience wrapper."""
    return Orchestrator(ctx, allow_template_fallback=allow_template_fallback).run()
