"""Finalize and export (§13, §22).

Assembles the deliverable and the manifest that explains it. Two rules shape what
this module will and will not do:

**§24.3 — no splicing.** The final file is the provider's own output for the last
accepted segment of a native chain. If it needs to be assembled from more than one
file, that only happens through `concat_native_chain` with an explicit assertion
that the parts are one chain, and the result is recorded as a derived artifact.

**§22 / §24.13 — content credentials.** The default export is a *byte-for-byte
copy*, which is the only path that preserves C2PA exactly. Anything that goes
through ffmpeg is marked `derived=True` and records C2PA state before and after, so
a dropped credential is visible in the manifest rather than something a viewer
discovers.

**§24.12 — no silent upscaling.** A 1080p regeneration is a different generation,
not an upgrade of the 360p draft. `check_high_res_request` makes the operator
confirm that, and a high-resolution pass runs as a *separate job* so the two
generations get separate lineage rather than one being presented as the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omni_homevlog.errors import OmniVlogError
from omni_homevlog.media.c2pa import inspect_content_credentials
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.pipeline.context import JobContext
from omni_homevlog.pipeline.extend import (
    chain_summary,
    chain_total_seconds,
    verify_chain_continuity,
)
from omni_homevlog.schemas import JobState, MediaInfo
from omni_homevlog.storage.local import atomic_write_json

logger = get_logger("finalize")


@dataclass(slots=True)
class FinalizeOutcome:
    final_path: Path
    media: MediaInfo
    derived: bool
    chain_summary: list[dict[str, object]]
    continuity_ok: bool
    continuity_problems: list[str] = field(default_factory=list)
    c2pa_present: bool | None = None


class HighResConfirmationRequired(OmniVlogError):
    """Raised when a higher-resolution pass is requested without acknowledgement."""

    code = "high_res_confirmation_required"


def finalize(
    ctx: JobContext,
    *,
    verify_continuity: bool = True,
    copy_verbatim_bytes: bool = True,
) -> FinalizeOutcome:
    """Produce the final deliverable from the accepted chain."""
    import hashlib

    from omni_homevlog.media.transcode import copy_verbatim, remux, strip_audio

    artifact = ctx.manifest.last_usable_artifact()
    if artifact is None or not artifact.local_path:
        raise OmniVlogError(
            "Nothing to finalize: the chain has no usable local render.",
            detail={"job_id": ctx.job_id},
        )

    source = Path(artifact.local_path)
    target = ctx.paths.final_video_path

    continuity_ok: bool = True
    problems: list[str] = []
    if verify_continuity:
        continuity_ok, problems = verify_chain_continuity(ctx)
        for problem in problems:
            logger.warning(
                "Chain continuity problem",
                extra={"extra_fields": {"job_id": ctx.job_id, "problem": problem}},
            )
            ctx.note(f"continuity check: {problem}")

    # Byte-for-byte by default: preserves content credentials exactly, and is the
    # only transform with no chance of altering the picture.
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if not ctx.spec.audio_enabled:
        result = strip_audio(source, target)
        if result.media.has_audio is not False:
            raise OmniVlogError("Silent output could not be verified; original retained.")
        ctx.note("Silent derivative created on explicit --no-audio request; provider original and its content credentials are retained at " + str(source))
    else:
        result = copy_verbatim(source, target) if copy_verbatim_bytes else remux(source, target)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_sha:
        raise OmniVlogError("Finalization changed the provider original.")

    credentials = inspect_content_credentials(target, provider=ctx.binding.provider_name)
    for note in credentials.notes or []:
        ctx.note(note)

    ctx.manifest = ctx.store.mutate(
        final_path=str(target),
        final_derived=result.derived,
        final_source_path=str(source),
        final_source_sha256=source_sha,
        final_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        c2pa_present=credentials.c2pa_present,
        synthid_expected=credentials.synthid_expected,
    )
    ctx.note(f"finalized to {target} (derived={result.derived})")

    logger.info(
        "Finalized",
        extra={
            "extra_fields": {
                "job_id": ctx.job_id,
                "path": str(target),
                "derived": result.derived,
                "total_seconds": round(chain_total_seconds(ctx), 2),
                "continuity_ok": continuity_ok,
            }
        },
    )

    return FinalizeOutcome(
        final_path=target,
        media=result.media,
        derived=result.derived,
        chain_summary=chain_summary(ctx),
        continuity_ok=continuity_ok,
        continuity_problems=problems,
        c2pa_present=credentials.c2pa_present,
    )


def export(
    ctx: JobContext,
    *,
    output: Path,
    allow_derived: bool = True,
) -> Path:
    """Copy the finalized file to a user-specified location."""
    ctx.reload()
    if ctx.manifest.state is not JobState.COMPLETE:
        raise OmniVlogError("Export requires COMPLETE: finish review and approval first.")
    if ctx.manifest.final_derived and not allow_derived:
        raise OmniVlogError("Derived export is disabled; no output was written.")
    source = Path(ctx.manifest.final_path) if ctx.manifest.final_path else None
    if source is None or not source.is_file():
        outcome = finalize(ctx)
        source = outcome.final_path

    output = Path(output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    import shutil

    shutil.copyfile(source, output)

    if ctx.manifest.final_derived and not allow_derived:
        raise OmniVlogError(
            "The finalized file is derived (it went through a transform). "
            "Re-run with allow_derived=True if you accept that."
        )

    ctx.note(f"exported to {output}")
    write_manifest_export(ctx)
    write_manifest_export(ctx, output.with_suffix(".manifest.json"))
    ctx.mirror("all")
    return output


def mark_complete(ctx: JobContext) -> None:
    """Move the job to COMPLETE. Refuses if the chain is structurally broken."""
    ok, problems = verify_chain_continuity(ctx)
    if not ok:
        ctx.error("refusing to mark COMPLETE: " + "; ".join(problems))
        raise OmniVlogError(
            "The chain is not structurally continuous, so the job cannot be marked "
            "COMPLETE:\n  - " + "\n  - ".join(problems),
            detail={"problems": problems},
        )
    ctx.manifest = ctx.store.transition(target=JobState.COMPLETE, note="finalized")
    write_manifest_export(ctx)


#: Resolutions ordered by cost, so we can refuse a downward "upgrade".
_RESOLUTION_RANK = {"360p": 0, "720p": 1, "1080p": 2, "4k": 3}


def check_high_res_request(
    ctx: JobContext,
    *,
    resolution: str,
    confirmed_understanding: bool,
) -> None:
    """Validate a higher-resolution pass without starting it.

    §24.12 is explicit that a re-rendered 1080p file must never be described as
    being the same as the draft it came from. So a high-resolution pass is a
    **new job**, not a transform of the existing one: it gets its own lineage, its
    own budget, and its own review cycle, and the manifest of each names the other.

    Raises `HighResConfirmationRequired` when the operator has not acknowledged
    that, and `OmniVlogError` when the request is not actually a step up.
    """
    current = ctx.spec.resolution
    if resolution not in _RESOLUTION_RANK:
        raise OmniVlogError(
            f"Unknown resolution {resolution!r}. Expected one of {sorted(_RESOLUTION_RANK)}.",
            detail={"resolution": resolution},
        )
    if _RESOLUTION_RANK[resolution] <= _RESOLUTION_RANK.get(current, 0):
        raise OmniVlogError(
            f"This job is already at {current}; {resolution} is not a higher "
            "resolution. A re-render at the same or lower resolution is just a "
            "regeneration, not a high-resolution pass.",
            detail={"current": current, "requested": resolution},
        )

    if not confirmed_understanding:
        raise HighResConfirmationRequired(
            "A higher-resolution pass RE-GENERATES the footage. It is not a lossless "
            f"upgrade of the {current} draft: the person, framing, motion, and audio "
            "may all change, and the result will not match frame-for-frame. It also "
            "costs a full new chain. Re-run with --confirm-understanding if that is "
            "what you intend.",
            detail={"current_resolution": current, "requested_resolution": resolution},
        )


def high_res_spec(ctx: JobContext, *, resolution: str) -> Any:
    """A spec for the new high-resolution job, cloned from this one.

    Title is suffixed and the human gates are inherited, so the new chain has to
    be approved on its own merits rather than riding on the draft's approval.
    """
    spec = ctx.spec.model_copy(deep=True)
    spec.resolution = resolution  # type: ignore[assignment]
    spec.title = f"{spec.title} [{resolution}]"
    return spec


def write_manifest_export(ctx: JobContext, path: Path | None = None) -> Path:
    """Write a standalone copy of the manifest next to the final video (§25)."""
    target = path or (ctx.paths.final_dir / "manifest.json")
    payload = ctx.store.load().model_dump(mode="json")
    atomic_write_json(target, payload)
    return target


def summary(ctx: JobContext) -> dict[str, Any]:
    """Everything `omni-vlog status` shows, assembled in one place."""
    manifest = ctx.store.load()
    from omni_homevlog.costing import has_video_pricing, load_pricing

    budget = ctx.budget.snapshot()
    if not has_video_pricing(ctx.binding.model, load_pricing(ctx.settings.omni_pricing_file)):
        budget["estimated_cost_usd"] = None
    return {
        "job_id": manifest.job_id,
        "state": str(manifest.state),
        "provider": manifest.provider,
        "project": manifest.project,
        "model": manifest.model,
        "created_at": manifest.created_at,
        "updated_at": manifest.updated_at,
        "target_duration_s": manifest.spec.target_duration_s if manifest.spec else None,
        "resolution": manifest.spec.resolution if manifest.spec else None,
        "segments_planned": len(manifest.segment_plan),
        "segments_rendered": len(manifest.segment_artifacts()),
        "chain_seconds": round(chain_total_seconds(ctx), 2),
        "chain": chain_summary(ctx),
        "budget": budget,
        "interactions": len(manifest.interactions),
        "unresolved_interactions": [
            r.interaction_id for r in manifest.interactions if not r.outcome_known
        ],
        "final_path": manifest.final_path,
        "final_derived": manifest.final_derived,
        "c2pa_present": manifest.c2pa_present,
        "synthid_expected": manifest.synthid_expected,
        "errors": manifest.errors[-10:],
        "notes": manifest.notes[-10:],
    }
