"""Resume and recovery (§5.1).

The four recovery rules, implemented literally:

  1. **Crash before the API request** → repeating the state is safe.
  2. **Request sent, result unknown** → query the interaction. Never re-issue.
  3. **Interaction completed but the file was not downloaded** → re-fetch from the URI.
  4. **Provider/project unavailable** → `NEEDS_HUMAN`. Never silently switch projects.

Rule 2 is the one that costs money if you get it wrong, so it is checked *before*
anything else runs. A job with an unresolved interaction cannot be auto-resumed;
`resume` reports it and stops, because the only correct next step requires knowing
whether a billable generation is already in flight.

`--allow-cross-project` exists for rule 4's escape hatch. Using it sets
`degraded_cross_project_resume` on the manifest, so the broken provenance is
recorded rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni_homevlog.errors import (
    HumanGateError,
    JobNotFoundError,
    RequestTimeoutUnknownOutcome,
    ResumeConflictError,
)
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.schemas import (
    HUMAN_GATE_STATES,
    TERMINAL_STATES,
    InteractionRecord,
    JobState,
    Manifest,
    RenderArtifact,
    utc_now_iso,
)
from omni_homevlog.state_machine import (
    extension_accepted_state,
    extension_review_state,
    legal_transitions,
)

logger = get_logger("resume")


@dataclass(slots=True)
class ResumePlan:
    """What `resume` intends to do, before it does it."""

    job_id: str
    state: JobState
    action: str
    blocked: bool = False
    blockers: list[str] = field(default_factory=list)
    next_stage: str | None = None
    requires_human: bool = False

    def describe(self) -> str:
        if self.blocked:
            return f"BLOCKED: {'; '.join(self.blockers)}"
        return f"{self.action} (state {self.state})"


def plan_resume(ctx: JobContext, *, allow_cross_project: bool = False) -> ResumePlan:
    """Work out what to do next, without doing it."""
    manifest = ctx.store.load()
    state = manifest.state
    plan = ResumePlan(job_id=manifest.job_id, state=state, action="nothing to do")

    # ── Rule 2 first: unresolved interactions dominate everything else ─────
    unresolved = [r for r in manifest.interactions if not r.outcome_known]
    if unresolved:
        plan.blocked = True
        plan.requires_human = True
        plan.blockers = [
            f"interaction {r.interaction_id} (segment {r.segment_index}, attempt "
            f"{r.attempt_index}) was dispatched but its outcome was never observed. "
            "It may still be running and may be billable."
            for r in unresolved
        ]
        plan.action = "resolve unknown-outcome interactions"
        return plan

    # ── Terminal states ───────────────────────────────────────────────────
    if state in TERMINAL_STATES:
        plan.action = f"job is {state}; nothing to resume"
        return plan

    # ── Rule 4: provider / project availability ───────────────────────────
    if state is JobState.PROVIDER_UNAVAILABLE:
        available, message = _provider_available(ctx)
        if not available:
            plan.blocked = True
            plan.requires_human = True
            plan.blockers = [message]
            plan.action = "provider unavailable"
            return plan
        plan.action = "retry after provider recovery"

    # ── Resume by state ───────────────────────────────────────────────────
    if state in HUMAN_GATE_STATES:
        # A gate the operator has already cleared is not a blocker. Without this,
        # `manifest.errors` is append-only, so `blockers` was permanently non-empty
        # and *every* parked job was stranded: `resume` exited 3 forever, `approve`
        # only printed advice, and `retry` re-entered the same gate. The whole
        # "resumable at any stage" promise rested on this branch.
        if has_pending_approval(manifest):
            plan.action = f"resuming past {state} after approval"
            return plan
        plan.requires_human = True
        plan.action = f"awaiting a human decision ({state})"
        plan.blockers = manifest.errors[-3:]
        return plan

    mapping: dict[JobState, tuple[str, str]] = {
        JobState.CREATED: ("validate references", "intake"),
        JobState.REFERENCES_VALIDATED: ("plan", "plan"),
        JobState.PLAN_READY: ("render seed", "seed"),
        JobState.SEED_RENDERING: ("render seed (re-entrant: safe, nothing was dispatched)", "seed"),
        JobState.SEED_REVIEW: ("review seed", "review_seed"),
        JobState.SEED_ACCEPTED: ("extend", "extend"),
        JobState.FINAL_REVIEW: ("finalize", "finalize"),
    }

    for i in range(1, 4):
        rendering = JobState(f"EXTENSION_{i}_RENDERING")
        review = extension_review_state(i)
        accepted = extension_accepted_state(i)
        mapping.setdefault(rendering, (f"render extension {i} (re-entrant)", "extend"))
        mapping.setdefault(review, (f"review extension {i}", f"review_ext_{i}"))
        mapping.setdefault(accepted, (f"extend from {i}", "extend"))

    if state in mapping:
        action, stage = mapping[state]
        plan.action = action
        plan.next_stage = stage
        return plan

    plan.blocked = True
    plan.blockers = [f"no resume rule for state {state}"]
    return plan


def _provider_available(ctx: JobContext) -> tuple[bool, str]:
    from omni_homevlog.providers.factory import check_provider_ready

    return check_provider_ready(ctx.binding)


# ── human gates ────────────────────────────────────────────────────────────

#: The note prefix `omni-vlog approve` writes. Also read by
#: `render_seed.require_human_approval`, which is why it lives here as a constant
#: rather than as a literal in two places that can drift.
APPROVAL_PREFIX = "approved:"


def has_pending_approval(manifest: Manifest) -> bool:
    """Has a human cleared the gate the job is currently sitting in?

    "Pending" means an approval recorded *after* the job most recently entered a
    gate state. An approval from an earlier cycle must not authorise a later stop,
    or one `approve` would forever wave through every subsequent failure.
    """
    last_gate_index = -1
    for index, entry in enumerate(manifest.state_history):
        target = _as_state(entry.get("to"))
        if target in HUMAN_GATE_STATES:
            last_gate_index = index

    if last_gate_index < 0:
        return False

    return any(
        str(entry.get("note", "")).startswith(APPROVAL_PREFIX)
        for entry in manifest.state_history[last_gate_index + 1 :]
    )


def _as_state(value: object) -> JobState | None:
    if isinstance(value, JobState):
        return value
    if isinstance(value, str):
        try:
            return JobState(value)
        except ValueError:
            return None
    return None


def resume_target_after_approval(ctx: JobContext) -> JobState | None:
    """Where a cleared gate should send the job.

    Walks the history back to the last state the job was actually working in,
    before it entered the gate, and returns it only if the state machine permits
    the move. Guessing a target would be worse than returning None: the operator
    gets told the job cannot be routed and can inspect the manifest, rather than
    being sent somewhere arbitrary.
    """
    manifest = ctx.store.load()
    current = manifest.state
    extension_count = manifest.spec.extension_count if manifest.spec is not None else 0
    allowed = legal_transitions(current, extension_count)

    for entry in reversed(manifest.state_history):
        candidate = _as_state(entry.get("to"))
        if candidate is None:
            continue
        if candidate in HUMAN_GATE_STATES or candidate in TERMINAL_STATES:
            continue
        if candidate in allowed:
            return candidate

    return None


def approve_gate(
    ctx: JobContext,
    *,
    stage: str,
    note: str | None = None,
) -> tuple[JobState, JobState]:
    """Clear a human gate and move the job to where it can continue.

    Returns `(from_state, to_state)`. Recording an approval *and* acting on it is
    the whole point: an `approve` that only appends a history note leaves the job
    exactly as stranded as before, which is what the previous version did.
    """
    manifest = ctx.store.load()
    current = manifest.state

    if current is JobState.FINAL_REVIEW and stage == "final":
        latest = manifest.last_usable_artifact()
        history = [
            *manifest.state_history,
            {
                "from": str(current),
                "to": str(current),
                "at": utc_now_iso(),
                "note": "approved:final" + (f" — {note}" if note else ""),
                "artifact_id": latest.interaction_id if latest else "",
            },
        ]
        ctx.manifest = ctx.store.mutate(state_history=history)
        return current, current

    if current not in HUMAN_GATE_STATES:
        raise HumanGateError(
            f"Job {manifest.job_id} is in {current}, which is not a human gate. "
            "There is nothing to approve.",
            detail={"state": str(current)},
        )

    gate_entry = next((e for e in reversed(manifest.state_history) if e.get("to") == str(current)), {})
    gate_note = gate_entry.get("note", "")
    pending_gate = gate_note.removeprefix("approval-required:") if gate_note.startswith("approval-required:") else None
    if pending_gate is not None and stage != pending_gate:
        raise HumanGateError(f"This job is awaiting {pending_gate!r}, not {stage!r}.")
    if stage not in ("final", "high-res") and stage not in {f"segment-{i}" for i in range(ctx.spec.extension_count + 1)}:
        raise HumanGateError(f"Unknown approval stage: {stage}")
    target = resume_target_after_approval(ctx)
    if pending_gate == "high-res" or pending_gate == "segment-0":
        target = JobState.SEED_RENDERING
    elif pending_gate and pending_gate.startswith("segment-"):
        index = int(pending_gate.split("-", 1)[1])
        target = JobState(f"EXTENSION_{index}_RENDERING")
    if target is None:
        raise HumanGateError(
            f"Job {manifest.job_id} is in {current}, but no legal state to resume "
            "into could be determined from its history. Inspect the manifest's "
            "state_history and the last few errors, then decide by hand.",
            detail={
                "state": str(current),
                "errors": manifest.errors[-5:],
            },
        )

    ctx.manifest = ctx.store.transition(
        target=target,
        note=f"{APPROVAL_PREFIX}{stage}" + (f" — {note}" if note else ""),
    )
    return current, target


def resolve_unknown_interaction(
    ctx: JobContext,
    record: InteractionRecord,
    *,
    interaction_id: str | None = None,
) -> RenderArtifact | None:
    """Query a dispatched-but-unobserved interaction. Read-only and free.

    Returns an artifact when the interaction turns out to have completed and its
    bytes can be recovered, otherwise None. Never dispatches a new generation.
    """
    target_id = interaction_id or record.interaction_id
    if target_id.startswith(("unknown-", "pending-")) and interaction_id is None:
        raise JobNotFoundError(
            "No interaction id was captured for this request, so it cannot be "
            "queried. A synchronous request that timed out leaves no handle. "
            "Check the provider console for a recent generation in project "
            f"{ctx.binding.project!r}, then re-run with "
            f"`--interaction-id <id>`.",
            detail={"record": record.model_dump(mode="json")},
        )

    import asyncio

    logger.info(
        "Querying an unresolved interaction (read-only, not billable)",
        extra={"extra_fields": {"job_id": ctx.job_id, "interaction_id": target_id}},
    )

    artifact: RenderArtifact | None = None
    try:
        artifact = asyncio.run(ctx.provider.get_interaction(target_id))
    except RequestTimeoutUnknownOutcome:
        logger.warning(
            "The query itself timed out; leaving the interaction unresolved",
            extra={"extra_fields": {"interaction_id": target_id}},
        )
        return None
    except Exception as exc:
        logger.warning(
            "Could not query the interaction",
            extra={"extra_fields": {"interaction_id": target_id, "error": str(exc)}},
        )
        return None

    if artifact is None:
        return None

    if artifact.status in ("in_progress", "unknown") :
        logger.info(
            "Interaction is still running; not dispatching anything new",
            extra={"extra_fields": {"interaction_id": target_id}},
        )
        return None

    if artifact.status != "completed":
        _mark_resolved(ctx, record, status=artifact.status)
        ctx.error(f"interaction {target_id} settled as {artifact.status}")
        return None

    # Rule 3: it completed, but we may still not have the file.
    if artifact.local_path and Path(artifact.local_path).is_file():
        pass
    elif artifact.gcs_uri and ctx.provider.gcs is not None:
        target = ctx.paths.attempt_path(
            record.segment_index, record.attempt_index, kind="recovered"
        )
        try:
            ctx.provider.gcs.download_to(artifact.gcs_uri, target)
            artifact.local_path = str(target)
            artifact.artifact_relpath = ctx.paths.relpath(target)
        except Exception as exc:
            logger.warning(
                "Interaction completed but the output could not be downloaded",
                extra={"extra_fields": {"uri": artifact.gcs_uri, "error": str(exc)}},
            )
    else:
        # Inline delivery: re-querying cannot return the bytes again, so the only
        # handle is the URI if one exists.
        logger.warning(
            "Interaction completed but no recoverable URI was returned; the output "
            "cannot be fetched without a further generation, which will not be "
            "started automatically",
            extra={"extra_fields": {"interaction_id": target_id}},
        )

    if not artifact.local_path:
        return None
    from omni_homevlog.media.ffprobe import inspect_media

    artifact.media = inspect_media(artifact.local_path)
    if not artifact.media.is_usable:
        return None

    # Persist the recovered render. Downloading the bytes and returning them was
    # not enough: nothing wrote them into the manifest or the database, so
    # `status` showed no chain, `fetch_missing_outputs` found nothing to re-fetch,
    # and the paid generation was silently orphaned on disk while the CLI printed
    # "recovered output".
    artifact = artifact.model_copy(
        update={
            "segment_index": record.segment_index,
            "parent_interaction_id": record.parent_interaction_id or artifact.parent_interaction_id,
            "requested_duration_s": int(str(record.duration or "10s").rstrip("s")),
            "task": _task_for_recovery(record),
            "aspect_ratio": artifact.aspect_ratio or ctx.spec.aspect_ratio,
            "resolution": artifact.resolution or record.resolution or ctx.spec.resolution,
            "status": "completed",
            "prompt_sha256": artifact.prompt_sha256 or "recovered",
        }
    )
    ctx.db.save_artifact(ctx.job_id, artifact, attempt_index=record.attempt_index)
    updated = record.model_copy(
        update={
            "interaction_id": target_id,
            "status": "completed",
            "outcome_known": True,
            "output_uri": artifact.gcs_uri,
            "request_completed_at": utc_now_iso(),
        }
    )
    ctx.manifest = ctx.store.mutate(
        segments=ctx.manifest.upsert_segment(artifact),
        interactions=[
            updated if r.interaction_id == record.interaction_id else r
            for r in ctx.manifest.interactions
        ],
    )
    ctx.db.delete_interaction(record.interaction_id)
    ctx.db.save_interaction(updated)
    target_state = (
        JobState.SEED_REVIEW
        if record.segment_index == 0
        else extension_review_state(record.segment_index)
    )
    if target_state in legal_transitions(ctx.manifest.state, ctx.spec.extension_count):
        ctx.manifest = ctx.store.transition(
            target=target_state, note="recovered existing generation; review before continuing"
        )
    ctx.note(
        f"recovered segment {record.segment_index} output from {target_id} and "
        f"recorded it at {artifact.local_path}"
    )
    return artifact


def _task_for_recovery(record: InteractionRecord) -> Any:
    """What kind of render a recovered interaction should be recorded as.

    The provider's query path cannot know, so it defaults to `extend`; the ledger
    row remembers what was actually dispatched, and using that keeps the
    reconstructed chain consistent with what happened.
    """
    if record.task in ("text_to_video", "reference_to_video", "image_to_video", "extend", "edit"):
        return record.task
    if record.call_kind in ("seed", None) and record.segment_index == 0:
        return "reference_to_video"
    if record.call_kind == "edit":
        return "edit"
    return "extend"


def _mark_resolved(ctx: JobContext, record: InteractionRecord, *, status: str) -> None:
    """Update the ledger and the manifest to reflect a resolved interaction."""
    updated = record.model_copy(
        update={
            "status": status,
            "outcome_known": True,
            "request_completed_at": utc_now_iso(),
        }
    )
    ctx.db.save_interaction(updated)

    interactions = [
        updated if r.interaction_id == record.interaction_id else r
        for r in ctx.manifest.interactions
    ]
    ctx.manifest = ctx.store.mutate(interactions=interactions)


def fetch_missing_outputs(ctx: JobContext) -> list[str]:
    """Restore missing recorded outputs without creating new interactions."""
    import asyncio

    from omni_homevlog.media.ffprobe import inspect_media
    recovered: list[str] = []
    artifacts = list(ctx.manifest.segments)
    for index, artifact in enumerate(artifacts):
        if artifact.local_path and Path(artifact.local_path).is_file():
            continue
        record = next((r for r in ctx.manifest.interactions if r.interaction_id == artifact.interaction_id), None)
        attempt = record.attempt_index if record else 0
        try:
            if artifact.gcs_uri and ctx.provider.gcs is not None:
                target = ctx.paths.attempt_path(artifact.segment_index, attempt, kind="recovered")
                ctx.provider.gcs.download_to(artifact.gcs_uri, target)
                media = inspect_media(target)
                local_path = str(target)
            else:
                queried = asyncio.run(ctx.provider.get_interaction(artifact.interaction_id))
                if queried.status != "completed" or not queried.local_path:
                    continue
                local_path = queried.local_path
                media = inspect_media(local_path)
            if not media.is_usable:
                raise ValueError("recovered output is not usable media")
            restored = artifact.model_copy(update={"local_path": local_path, "artifact_relpath": ctx.paths.relpath(Path(local_path)), "media": media})
            artifacts[index] = restored
            ctx.db.save_artifact(ctx.job_id, restored, attempt_index=attempt)
            recovered.append(local_path)
        except Exception as exc:
            ctx.error(f"Could not recover existing output {artifact.interaction_id}: {exc}")
    if recovered:
        ctx.manifest = ctx.store.mutate(segments=artifacts)
    return recovered


def _segment_index_of(artifact: RenderArtifact) -> int:
    return max(0, artifact.segment_index)


def mark_degraded_cross_project(ctx: JobContext, *, from_project: str | None) -> None:
    """Record that a job was resumed against a different project than it started on.

    §5.1 permits this only with explicit human consent, and requires it to be
    visible. The chain's provenance is genuinely weaker after this, so the flag
    goes in the manifest rather than in a log line.
    """
    ctx.manifest = ctx.store.mutate(degraded_cross_project_resume=True)
    ctx.note(
        f"WARNING: resumed against project {ctx.binding.project!r} after being bound "
        f"to {from_project!r}. The interaction chain is not continuous across "
        "projects, so the provider's server-side state does not carry over. This is "
        "recorded as degraded_cross_project_resume."
    )
    logger.warning(
        "Cross-project resume recorded",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "original_project": from_project,
                "current_project": ctx.binding.project,
            }
        },
    )


def ensure_project_binding(ctx: JobContext) -> None:
    """Refuse to continue if the environment would move the job to another project."""
    manifest = ctx.store.load()
    if manifest.provider != "vertex":
        return
    bound = manifest.project
    current = ctx.binding.project
    if bound and current and bound != current:
        raise ResumeConflictError(
            f"Job is bound to project {bound!r} but the provider is now "
            f"{current!r}. Pass --allow-cross-project to continue (recorded as "
            "degraded), or restore the original project.",
            detail={"bound": bound, "current": current},
        )
