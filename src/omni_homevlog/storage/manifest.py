"""Manifest persistence (§13.2).

`manifest.json` is the authoritative record of a job's lineage. §5 requires that
every state transition be written *atomically to both* the manifest and the
database; `ManifestStore.transition()` is that single choke point, so there is no
way to update one without the other.

Write order matters: manifest first, then database. If the process dies between
the two, the manifest is ahead of the index and `rebuild_from_manifest` repairs
it. The reverse order would lose information, because the database is derived.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omni_homevlog.errors import JobNotFoundError
from omni_homevlog.observability.logging import get_logger, write_debug_fixture
from omni_homevlog.schemas import JobState, Manifest, utc_now_iso
from omni_homevlog.state_machine import assert_transition
from omni_homevlog.storage.database import Database
from omni_homevlog.storage.local import JobPaths, atomic_write_json, read_json

logger = get_logger("manifest")


class ManifestStore:
    """Read, mutate, and atomically persist one job's manifest."""

    def __init__(self, paths: JobPaths, database: Database | None = None) -> None:
        self.paths = paths
        self._db = database

    @property
    def db(self) -> Database:
        if self._db is None:
            self._db = Database(self.paths.db_path)
        return self._db

    # ── io ─────────────────────────────────────────────────────────────────

    def exists(self) -> bool:
        return self.paths.manifest_path.exists()

    def load(self) -> Manifest:
        if not self.exists():
            raise JobNotFoundError(f"No manifest for job {self.paths.job_id}")
        raw = read_json(self.paths.manifest_path)
        return Manifest.model_validate(raw)

    def load_or_none(self) -> Manifest | None:
        try:
            return self.load()
        except (JobNotFoundError, json.JSONDecodeError, ValueError):
            return None

    def save(self, manifest: Manifest, *, sync_db: bool = True) -> Manifest:
        manifest.updated_at = utc_now_iso()
        payload = manifest.model_dump(mode="json")
        atomic_write_json(self.paths.manifest_path, payload)
        if sync_db:
            self.db.upsert_job(manifest, manifest_path=str(self.paths.manifest_path))
        return manifest

    def create(self, manifest: Manifest) -> Manifest:
        self.paths.ensure()
        return self.save(manifest)

    # ─ transitions ─────────────────────────────────────────────────────────

    def transition(
        self,
        *,
        target: JobState,
        note: str | None = None,
        allow_noop: bool = False,
    ) -> Manifest:
        """Move the job to `target`, persisting atomically to manifest + DB.

        Raises `StateTransitionError` for anything the state machine forbids —
        there is no force flag. If you genuinely need to override, edit the
        manifest by hand and say why in the commit message.
        """
        manifest = self.load()
        current = manifest.state
        extension_count = manifest.spec.extension_count if manifest.spec is not None else 0

        assert_transition(current, target, extension_count=extension_count)

        if current == target and not allow_noop:
            return manifest

        manifest.state = target
        manifest.state_history.append(
            {"from": str(current), "to": str(target), "at": utc_now_iso(), "note": note or ""}
        )
        self.save(manifest)
        logger.info(
            "State transition",
            extra={
                "extra_fields": {
                    "job_id": manifest.job_id,
                    "from": str(current),
                    "to": str(target),
                    "note": note,
                }
            },
        )
        return manifest

    def mutate(self, **updates: Any) -> Manifest:
        """Apply arbitrary field updates and persist. For non-state changes."""
        manifest = self.load()
        for key, value in updates.items():
            if key not in Manifest.model_fields:
                raise KeyError(f"{key!r} is not a Manifest field")
            setattr(manifest, key, value)
        return self.save(manifest)

    def append_note(self, note: str) -> Manifest:
        manifest = self.load()
        manifest.notes.append(note)
        return self.save(manifest)

    def append_error(self, message: str) -> Manifest:
        manifest = self.load()
        manifest.errors.append(message)
        return self.save(manifest)

    def record_quality_report(self, report: dict[str, Any]) -> Manifest:
        manifest = self.load()
        manifest.quality_reports.append(report)
        return self.save(manifest)

    def record_budget_event(self, event: Any) -> Manifest:
        """Append a budget event to both stores, manifest first.

        This wrote the database row first, inverting the ordering the rest of the
        module documents. If the process died between the two, the index held an
        event the authoritative manifest did not, and `rebuild_from_manifest`
        would then disagree with what had actually been spent.
        """
        manifest = self.load()
        manifest.budget_events.append(event)
        self.save(manifest)
        self.db.save_budget_event(manifest.job_id, event)
        return manifest

    # ── export ─────────────────────────────────────────────────────────────

    def export_summary(self) -> dict[str, Any]:
        """Compact, human-oriented projection used by `omni-vlog status`."""
        manifest = self.load()
        usable = manifest.segment_artifacts()
        total_seconds = sum((a.media.duration_s or 0.0) for a in usable if a.media is not None)
        return {
            "job_id": manifest.job_id,
            "state": str(manifest.state),
            "provider": manifest.provider,
            "project": manifest.project,
            "model": manifest.model,
            "created_at": manifest.created_at,
            "updated_at": manifest.updated_at,
            "segments_rendered": len(usable),
            "segments_planned": len(manifest.segment_plan),
            "total_seconds": round(total_seconds, 2),
            "interactions": len(manifest.interactions),
            "quality_reports": len(manifest.quality_reports),
            "final_path": manifest.final_path,
            "c2pa_present": manifest.c2pa_present,
            "errors": manifest.errors[-5:],
            "notes": manifest.notes[-5:],
        }

    def write_debug(self, name: str, payload: Any) -> str:
        return write_debug_fixture(str(self.paths.debug_dir / name), payload)


def load_manifest_file(path: Path) -> Manifest:
    return Manifest.model_validate(read_json(Path(path)))
