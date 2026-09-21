"""Mirror a job's artifacts to GCS (§13.1).

The plan's GCS layout is a full copy of the job directory, not just the video:

    gs://BUCKET/homevlog-agent/
      jobs/{job_id}/
        input/references/
        plans/project_spec.json, continuity_bible.json, segment_plan.json
        renders/segment_00/attempt_00_raw.mp4
        reviews/segment_00_attempt_00.json
        final/final.mp4, manifest.json

An earlier revision only uploaded reference images and renders, which left the
audit trail local-only. That matters for the plan's stated goal (§1.2) that every
job output a complete lineage: if the plans and reviews live only on the machine
that produced them, the GCS copy of a job cannot answer "why does this video look
like this".

Design: **push after each stage, never block on failure.** A mirror failure must
not fail a render that succeeded, because the render is the expensive part and the
local copy is still intact. Failures are logged and recorded on the manifest so
they are visible rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from omni_homevlog.observability.logging import get_logger
from omni_homevlog.storage.gcs import GcsClient, join_uri
from omni_homevlog.storage.local import JobPaths

logger = get_logger("sync")

#: Which part of the job tree a stage mirrors.
PLAN_FILES = ("project_spec.json", "continuity_bible.json", "segment_plan.json")


@dataclass(slots=True)
class SyncResult:
    uploaded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed

    def describe(self) -> str:
        if self.skipped:
            return f"not mirrored: {self.skipped}"
        if not self.uploaded and not self.failed:
            return "nothing to mirror"
        parts = [f"uploaded {len(self.uploaded)} object(s)"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return "; ".join(parts)


def job_prefix(gcs_prefix: str, job_id: str) -> str:
    """Root of a job's tree in the bucket.

    §13.1 nests jobs under a `homevlog-agent/` segment when the prefix is a bare
    bucket, but an operator-supplied prefix is used as given — assuming a
    hardcoded subdirectory would scatter objects in an unexpected place.
    """
    return join_uri(gcs_prefix, "jobs", job_id)


def _upload(
    gcs: GcsClient, local: Path, uri: str, result: SyncResult, *, content_type: str
) -> None:
    if not local.is_file():
        return
    try:
        gcs.upload_file(local, uri, content_type=content_type)
        result.uploaded.append(uri)
    except Exception as exc:
        logger.warning(
            "GCS mirror failed for one object",
            extra={"extra_fields": {"uri": uri, "error": str(exc)}},
        )
        result.failed.append((uri, str(exc)))


def mirror_plans(paths: JobPaths, gcs: GcsClient, gcs_prefix: str) -> SyncResult:
    """Push `plans/` after the Director runs."""
    result = SyncResult()
    root = job_prefix(gcs_prefix, paths.job_id)
    for name in PLAN_FILES:
        _upload(
            gcs,
            paths.plans_dir / name,
            join_uri(root, "plans", name),
            result,
            content_type="application/json",
        )
    return result


def mirror_reviews(paths: JobPaths, gcs: GcsClient, gcs_prefix: str) -> SyncResult:
    """Push `reviews/` after a review is written.

    Each review file is small and written whole, so re-uploading the directory is
    cheap and avoids tracking which specific file is new.
    """
    result = SyncResult()
    root = job_prefix(gcs_prefix, paths.job_id)
    if not paths.reviews_dir.is_dir():
        return result
    for review in sorted(paths.reviews_dir.glob("*.json")):
        _upload(
            gcs,
            review,
            join_uri(root, "reviews", review.name),
            result,
            content_type="application/json",
        )
    return result


def mirror_references(paths: JobPaths, gcs: GcsClient, gcs_prefix: str) -> SyncResult:
    """Push `input/references/` and the sanitizer reports.

    The provider stages references itself at dispatch time, but doing it here as
    well means the sanitizer verdicts travel with them, which is the part a
    reviewer actually needs.
    """
    result = SyncResult()
    root = job_prefix(gcs_prefix, paths.job_id)
    for directory, mime in (
        (paths.references_dir, None),
        (paths.sanitizer_dir, "application/json"),
    ):
        if not directory.is_dir():
            continue
        for item in sorted(directory.iterdir()):
            if not item.is_file():
                continue
            _upload(
                gcs,
                item,
                join_uri(root, "input", directory.name, item.name),
                result,
                content_type=mime or _guess_content_type(item),
            )
    return result


def mirror_final(
    paths: JobPaths, gcs: GcsClient, gcs_prefix: str, *, include_video: bool = True
) -> SyncResult:
    """Push `final/` — the deliverable plus its manifest.

    The manifest is the important one: it is the artifact that makes the video
    auditable, and a bucket copy of the MP4 without it is not a deliverable in the
    sense §1.2 describes.
    """
    result = SyncResult()
    root = job_prefix(gcs_prefix, paths.job_id)

    _upload(
        gcs,
        paths.final_dir / "manifest.json",
        join_uri(root, "final", "manifest.json"),
        result,
        content_type="application/json",
    )

    if include_video and paths.final_dir.is_dir():
        for video in sorted(paths.final_dir.glob("*.mp4")):
            _upload(
                gcs,
                video,
                join_uri(root, "final", video.name),
                result,
                content_type="video/mp4",
            )
    return result


def mirror_job(paths: JobPaths, gcs: GcsClient, gcs_prefix: str) -> SyncResult:
    """Push the whole job tree. Used by `omni-vlog sync` as a catch-up.

    Not called on the hot path: mirroring every render after every stage would
    re-upload multi-megabyte videos repeatedly. Stages push what they changed; this
    is the explicit "make the bucket match local" command.
    """
    result = SyncResult()
    for part in (
        mirror_references(paths, gcs, gcs_prefix),
        mirror_plans(paths, gcs, gcs_prefix),
        mirror_reviews(paths, gcs, gcs_prefix),
        mirror_final(paths, gcs, gcs_prefix),
    ):
        result.uploaded.extend(part.uploaded)
        result.failed.extend(part.failed)

    # Renders are the bulky part, so they are mirrored by explicit path rather
    # than by directory sweep only here.
    root = job_prefix(gcs_prefix, paths.job_id)
    if paths.renders_dir.is_dir():
        for render in sorted(paths.renders_dir.rglob("*.mp4")):
            relative = render.relative_to(paths.renders_dir)
            _upload(
                gcs,
                render,
                join_uri(root, "renders", relative.as_posix()),
                result,
                content_type="video/mp4",
            )
    return result


def _guess_content_type(path: Path) -> str:
    import mimetypes

    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def best_effort_mirror(
    paths: JobPaths, gcs: GcsClient | None, gcs_prefix: str | None, stage: str
) -> SyncResult:
    """Mirror one stage, swallowing everything.

    The pipeline calls this after each stage. It must be impossible for a bucket
    problem to fail a job whose render already succeeded, so every failure mode
    collapses into a `SyncResult` with the reason recorded.
    """
    if gcs is None or not gcs_prefix:
        return SyncResult(skipped="no GCS prefix configured (OMNI_OUTPUT_GCS_URI)")

    try:
        if stage == "plans":
            return mirror_plans(paths, gcs, gcs_prefix)
        if stage == "reviews":
            return mirror_reviews(paths, gcs, gcs_prefix)
        if stage == "references":
            return mirror_references(paths, gcs, gcs_prefix)
        if stage == "final":
            return mirror_final(paths, gcs, gcs_prefix)
        if stage == "all":
            return mirror_job(paths, gcs, gcs_prefix)
    except Exception as exc:
        logger.warning(
            "GCS mirror failed for a whole stage",
            extra={"extra_fields": {"stage": stage, "error": str(exc)}},
        )
        return SyncResult(failed=[(stage, str(exc))])

    return SyncResult(skipped=f"unknown stage {stage!r}")
