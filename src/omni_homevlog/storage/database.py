"""SQLite job database (§13.3).

Tables: `jobs`, `artifacts`, `interactions`, `reviews`, `budget_events`,
`provider_probes`.

The plan is explicit that a personal MVP should not start with PostgreSQL, but
that "接口保持可替换即可" — the interface stays replaceable. So nothing outside
this module issues SQL, and every method returns Pydantic models, not rows.

The database is a *derived index* over the manifest, not the source of truth.
`manifest.json` is authoritative (§5: every transition is written atomically to
both). If the two ever disagree, the manifest wins and `rebuild_from_manifest`
repairs the database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from omni_homevlog.observability.logging import get_logger
from omni_homevlog.schemas import (
    BudgetEvent,
    CritiqueReport,
    InteractionRecord,
    Manifest,
    ProviderCapabilities,
    RenderArtifact,
)

logger = get_logger("db")

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    project         TEXT,
    model           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    target_duration INTEGER,
    manifest_path   TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    interaction_id  TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    segment_index   INTEGER NOT NULL,
    attempt_index   INTEGER NOT NULL,
    task            TEXT NOT NULL,
    status          TEXT NOT NULL,
    local_path      TEXT,
    gcs_uri         TEXT,
    resolution      TEXT,
    prompt_sha256   TEXT,
    derived         INTEGER NOT NULL DEFAULT 0,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id, segment_index, attempt_index);

CREATE TABLE IF NOT EXISTS interactions (
    interaction_id          TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    parent_interaction_id   TEXT,
    segment_index           INTEGER NOT NULL,
    attempt_index           INTEGER NOT NULL,
    provider                TEXT NOT NULL,
    project                 TEXT,
    model                   TEXT NOT NULL,
    task                    TEXT NOT NULL,
    status                  TEXT NOT NULL,
    request_started_at      TEXT NOT NULL,
    request_completed_at    TEXT,
    latency_s               REAL,
    error_code              TEXT,
    error_message           TEXT,
    output_uri              TEXT,
    estimated_cost_usd      TEXT,
    outcome_known           INTEGER NOT NULL DEFAULT 1,
    payload                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interactions_job ON interactions(job_id, segment_index);

CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    segment_index   INTEGER NOT NULL,
    attempt_index   INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    payload         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(job_id, segment_index, attempt_index, kind)
);

CREATE TABLE IF NOT EXISTS budget_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    at              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    call_kind       TEXT NOT NULL,
    segment_index   INTEGER NOT NULL,
    attempt_index   INTEGER NOT NULL,
    video_seconds   INTEGER NOT NULL DEFAULT 0,
    estimated_cost  TEXT NOT NULL DEFAULT '0',
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS provider_probes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,
    project         TEXT,
    model           TEXT NOT NULL,
    probed_at       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    UNIQUE(provider, project, model)
);
"""


class Database:
    """One SQLite file per job, kept next to that job's manifest."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Per-connection, and it must be set on every one. Declaring it in the
        # schema script only affected the connection that created the file, so
        # ON DELETE CASCADE silently did nothing afterwards and `delete_job` left
        # artifacts and budget events behind.
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # ─ jobs ───────────────────────────────────────────────────────────────

    def upsert_job(self, manifest: Manifest, *, manifest_path: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs(job_id, state, provider, project, model, created_at,
                                 updated_at, target_duration, manifest_path)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    state=excluded.state,
                    updated_at=excluded.updated_at,
                    manifest_path=COALESCE(excluded.manifest_path, jobs.manifest_path)
                """,
                (
                    manifest.job_id,
                    str(manifest.state),
                    manifest.provider,
                    manifest.project,
                    manifest.model,
                    manifest.created_at,
                    manifest.updated_at,
                    manifest.spec.target_duration_s if manifest.spec else None,
                    manifest_path,
                ),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── artifacts ──────────────────────────────────────────────────────────

    def save_artifact(
        self, job_id: str, artifact: RenderArtifact, *, attempt_index: int = 0
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts(interaction_id, job_id, segment_index, attempt_index,
                                      task, status, local_path, gcs_uri, resolution,
                                      prompt_sha256, derived, payload, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(interaction_id) DO UPDATE SET
                    status=excluded.status,
                    local_path=excluded.local_path,
                    gcs_uri=excluded.gcs_uri,
                    payload=excluded.payload
                """,
                (
                    artifact.interaction_id,
                    job_id,
                    _segment_index_of(artifact),
                    attempt_index,
                    str(artifact.task),
                    artifact.status,
                    artifact.local_path,
                    artifact.gcs_uri,
                    artifact.resolution,
                    artifact.prompt_sha256,
                    int(artifact.derived),
                    artifact.model_dump_json(),
                    artifact.created_at,
                ),
            )

    def artifacts_for_job(self, job_id: str) -> list[RenderArtifact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM artifacts WHERE job_id = ? "
                "ORDER BY segment_index, attempt_index",
                (job_id,),
            ).fetchall()
        return [RenderArtifact.model_validate_json(r["payload"]) for r in rows]

    # ─ interactions ───────────────────────────────────────────────────────

    def save_interaction(self, record: InteractionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO interactions(interaction_id, job_id, parent_interaction_id,
                    segment_index, attempt_index, provider, project, model, task, status,
                    request_started_at, request_completed_at, latency_s, error_code,
                    error_message, output_uri, estimated_cost_usd, outcome_known, payload)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(interaction_id) DO UPDATE SET
                    status=excluded.status,
                    request_completed_at=excluded.request_completed_at,
                    latency_s=excluded.latency_s,
                    error_code=excluded.error_code,
                    error_message=excluded.error_message,
                    output_uri=excluded.output_uri,
                    estimated_cost_usd=excluded.estimated_cost_usd,
                    outcome_known=excluded.outcome_known,
                    payload=excluded.payload
                """,
                (
                    record.interaction_id,
                    record.job_id,
                    record.parent_interaction_id,
                    record.segment_index,
                    record.attempt_index,
                    record.provider,
                    record.project,
                    record.model,
                    str(record.task),
                    record.status,
                    record.request_started_at,
                    record.request_completed_at,
                    record.latency_s,
                    record.error_code,
                    record.error_message,
                    record.output_uri,
                    str(record.estimated_cost_usd)
                    if record.estimated_cost_usd is not None
                    else None,
                    int(record.outcome_known),
                    record.model_dump_json(),
                ),
            )

    def interactions_for_job(self, job_id: str) -> list[InteractionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM interactions WHERE job_id = ? "
                "ORDER BY segment_index, attempt_index",
                (job_id,),
            ).fetchall()
        return [InteractionRecord.model_validate_json(r["payload"]) for r in rows]

    def delete_interaction(self, interaction_id: str) -> None:
        """Remove one ledger row.

        Used to retire the pre-dispatch placeholder once the real result is known.
        The placeholder exists so a crash mid-call leaves a trace; when the call
        returns normally it has served its purpose and would otherwise sit in the
        ledger forever, blocking `resume` and inflating the call count.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM interactions WHERE interaction_id = ?", (interaction_id,))

    def unresolved_interactions(self, job_id: str) -> list[InteractionRecord]:
        """Requests dispatched whose outcome we never observed (§5.1)."""
        return [r for r in self.interactions_for_job(job_id) if not r.outcome_known]

    # ── reviews ────────────────────────────────────────────────────────────

    def save_review(
        self,
        job_id: str,
        *,
        segment_index: int,
        attempt_index: int,
        report: CritiqueReport,
        kind: str = "segment",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews(job_id, segment_index, attempt_index, kind, verdict,
                                    payload, created_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(job_id, segment_index, attempt_index, kind) DO UPDATE SET
                    verdict=excluded.verdict, payload=excluded.payload,
                    created_at=excluded.created_at
                """,
                (
                    job_id,
                    segment_index,
                    attempt_index,
                    kind,
                    report.verdict,
                    report.model_dump_json(),
                    _now(),
                ),
            )

    def reviews_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── budget ────────────────────────────────────────────────────────────

    def save_budget_event(self, job_id: str, event: BudgetEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO budget_events(job_id, at, kind, call_kind, segment_index,
                                          attempt_index, video_seconds, estimated_cost, reason)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    event.at,
                    event.kind,
                    event.call_kind,
                    event.segment_index,
                    event.attempt_index,
                    event.video_seconds,
                    str(event.estimated_cost_usd),
                    event.reason,
                ),
            )

    def budget_events_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM budget_events WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── probes ─────────────────────────────────────────────────────────────

    def save_probe(self, caps: ProviderCapabilities) -> None:
        previous = self.latest_probe(caps.provider, caps.project, caps.model)
        for snapshot in (caps, previous):
            if snapshot is not None:
                for current, legacy in (("T2V", "T2V 3s 360p"), ("I2V", "I2V 3s 360p")):
                    if current not in snapshot.evidence and legacy in snapshot.evidence:
                        snapshot.evidence[current] = snapshot.evidence[legacy]
        if previous and previous.location == caps.location and caps.evidence:
            mapping = {
                "t2v": "T2V",
                "i2v": "I2V",
                "reference_to_video": "Reference-to-video",
                "edit": "Edit",
                "extend": "Extend",
                "stateful_previous_interaction_id": "previous_interaction_id",
                "stateful_steps_replay": "steps replay",
                "first_last_frame": "First + last frame",
                "gcs_delivery": "GCS URI delivery",
                "uri_delivery": "GCS URI delivery",
                "native_audio": "Native audio",
                "async_polling": "Async polling",
                "remote_retrieval": "Remote recovery",
            }
            for field, name in mapping.items():
                row = caps.evidence.get(name, {})
                if field == "native_audio" and any(
                    (r.get("evidence", {}).get("media") or {}).get("has_audio")
                    for r in caps.evidence.values()
                ):
                    continue
                if row.get("status") not in ("PASS", "FAIL"):
                    setattr(caps, field, getattr(previous, field))
                    if name in previous.evidence:
                        caps.evidence[name] = previous.evidence[name]
            if not any(
                r.get("status") in ("PASS", "FAIL")
                for n, r in caps.evidence.items()
                if n == "continuation grows the film"
            ):
                caps.max_total_chain_s = previous.max_total_chain_s
                caps.measured_chain_s = previous.measured_chain_s
            if not caps.measured_generation_s:
                caps.measured_generation_s = previous.measured_generation_s
            for name, evidence in previous.evidence.items():
                if name not in caps.evidence:
                    caps.evidence[name] = evidence
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO provider_probes(provider, project, model, probed_at, payload)
                VALUES(?,?,?,?,?)
                ON CONFLICT(provider, project, model) DO UPDATE SET
                    probed_at=excluded.probed_at, payload=excluded.payload
                """,
                (
                    caps.provider,
                    caps.project or "",
                    caps.model,
                    caps.probed_at,
                    caps.model_dump_json(),
                ),
            )

    def latest_probe(
        self, provider: str, project: str | None, model: str
    ) -> ProviderCapabilities | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM provider_probes WHERE provider=? AND project=? AND model=?",
                (provider, project or "", model),
            ).fetchone()
        if not row:
            return None
        return ProviderCapabilities.model_validate_json(row["payload"])

    def all_probes(self) -> list[ProviderCapabilities]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM provider_probes ORDER BY probed_at DESC"
            ).fetchall()
        return [ProviderCapabilities.model_validate_json(r["payload"]) for r in rows]

    # ─ maintenance ────────────────────────────────────────────────────────

    def rebuild_from_manifest(self, manifest: Manifest) -> None:
        """Repair the index from the authoritative manifest.

        `budget_events` is append-only and has no natural key, so re-running this
        doubled the table (verified: two events rebuilt twice became four). The
        job's events are cleared first, which is safe because the manifest is the
        authoritative copy and every one of them is re-inserted below.
        """
        self.upsert_job(manifest)
        with self._connect() as conn:
            conn.execute("DELETE FROM budget_events WHERE job_id = ?", (manifest.job_id,))

        for index, artifact in enumerate(manifest.segments):
            self.save_artifact(manifest.job_id, artifact, attempt_index=index)
        for record in manifest.interactions:
            self.save_interaction(record)
        for event in manifest.budget_events:
            self.save_budget_event(manifest.job_id, event)
        logger.info(
            "Rebuilt database index from manifest",
            extra={"extra_fields": {"job_id": manifest.job_id}},
        )

    def delete_job(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def _segment_index_of(artifact: RenderArtifact) -> int:
    """The artifact's segment index.

    Read from the artifact itself. An earlier version parsed it out of the
    relpath, which broke as soon as a repair wrote to an attempt file whose name
    no longer matched the segment directory.
    """
    return artifact.segment_index


def _now() -> str:
    from omni_homevlog.schemas import utc_now_iso

    return utc_now_iso()


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
