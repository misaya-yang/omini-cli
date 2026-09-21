"""Local artifact store and job layout (§13).

Layout on disk mirrors the GCS layout in the plan so that a job can be moved
between the two without rewriting paths:

    <data_dir>/jobs/<job_id>/
      input/references/
      input/sanitizer/
      plans/project_spec.json
      plans/continuity_bible.json
      plans/segment_plan.json
      renders/segment_00/attempt_00_raw.mp4
      reviews/segment_00_attempt_00.json
      debug/
      final/final.mp4
      final/manifest.json
      job.db

Every JSON write is atomic (temp file + `os.replace`) so a crash mid-write can
never leave a manifest that parses as truncated. §5 requires exactly this: state
transitions are persisted atomically to both the manifest and the database.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from omni_homevlog.errors import OmniVlogError

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class StorageError(OmniVlogError):
    code = "storage_error"


def validate_job_id(job_id: str) -> str:
    """Job ids become directory names. Reject anything that could escape the root."""
    if not _JOB_ID_RE.match(job_id) or ".." in job_id:
        raise StorageError(
            f"Unsafe job id {job_id!r}. Use 1-64 chars of [A-Za-z0-9._-], not starting with '.'"
        )
    return job_id


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """Write bytes so that readers see either the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def atomic_write_text(path: Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> Path:
    return atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class JobPaths:
    """Typed access to one job's directory tree."""

    def __init__(self, root: Path, job_id: str) -> None:
        self.job_id = validate_job_id(job_id)
        self.root = root / "jobs" / self.job_id

    # ─ directories ────────────────────────────────────────────────────────

    @property
    def input_dir(self) -> Path:
        return self.root / "input"

    @property
    def references_dir(self) -> Path:
        return self.input_dir / "references"

    @property
    def sanitizer_dir(self) -> Path:
        return self.input_dir / "sanitizer"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"

    @property
    def reviews_dir(self) -> Path:
        return self.root / "reviews"

    @property
    def debug_dir(self) -> Path:
        return self.root / "debug"

    @property
    def final_dir(self) -> Path:
        return self.root / "final"

    @property
    def db_path(self) -> Path:
        return self.root / "job.db"

    # ── files ──────────────────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.final_dir / "manifest.json"

    @property
    def project_spec_path(self) -> Path:
        return self.plans_dir / "project_spec.json"

    @property
    def continuity_bible_path(self) -> Path:
        return self.plans_dir / "continuity_bible.json"

    @property
    def segment_plan_path(self) -> Path:
        return self.plans_dir / "segment_plan.json"

    @property
    def final_video_path(self) -> Path:
        return self.final_dir / "final.mp4"

    def segment_dir(self, segment_index: int) -> Path:
        return self.renders_dir / f"segment_{segment_index:02d}"

    def attempt_path(
        self, segment_index: int, attempt_index: int, *, kind: str = "raw", ext: str = "mp4"
    ) -> Path:
        return self.segment_dir(segment_index) / f"attempt_{attempt_index:02d}_{kind}.{ext}"

    def review_path(self, segment_index: int, attempt_index: int) -> Path:
        return self.reviews_dir / f"segment_{segment_index:02d}_attempt_{attempt_index:02d}.json"

    def final_review_path(self) -> Path:
        return self.reviews_dir / "final_review.json"

    def sanitizer_path(self, asset_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", asset_id)
        return self.sanitizer_dir / f"{safe}.json"

    def relpath(self, path: Path) -> str:
        """Path relative to the job root, for storing in the manifest."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)

    def resolve_relpath(self, relpath: str) -> Path:
        return self.root / relpath

    def ensure(self) -> JobPaths:
        for directory in (
            self.references_dir,
            self.sanitizer_dir,
            self.plans_dir,
            self.renders_dir,
            self.reviews_dir,
            self.debug_dir,
            self.final_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def exists(self) -> bool:
        return self.root.exists()

    def total_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())

    def delete(self) -> None:
        """Remove the whole job directory. Callers must have asked the user first."""
        if self.root.exists():
            shutil.rmtree(self.root)


class LocalStore:
    """Root of the local data directory."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.jobs_dir = self.data_dir / "jobs"

    def ensure(self) -> LocalStore:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        return self

    def job(self, job_id: str) -> JobPaths:
        return JobPaths(self.data_dir, job_id)

    def list_jobs(self) -> list[str]:
        if not self.jobs_dir.exists():
            return []
        return sorted(p.name for p in self.jobs_dir.iterdir() if p.is_dir())

    def latest_job_id(self) -> str | None:
        jobs = self.list_jobs()
        if not jobs:
            return None
        return max(jobs, key=lambda j: (self.jobs_dir / j).stat().st_mtime)


def make_job_id(prefix: str = "job") -> str:
    """Sortable, collision-resistant, filesystem-safe."""
    import uuid
    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"
