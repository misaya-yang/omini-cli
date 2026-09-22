"""Metrics (§23).

Aggregates the interaction ledger into the numbers the plan asks for: success
rate, mean attempts per segment, edit success rate, regeneration share, identity
failure rate, text/timestamp contamination rate, chain completion rate, average
generated seconds, estimated cost, and provider/project availability.

Kept as a plain function over the manifest and database rather than a metrics
client, because the MVP has no metrics backend and inventing one would be the
"complex infrastructure" §0.6 warns against. The shapes here are what a later
exporter would push.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from omni_homevlog.schemas import Manifest
from omni_homevlog.storage.database import Database


@dataclass(slots=True)
class JobMetrics:
    job_id: str
    interactions: int = 0
    completed: int = 0
    failed: int = 0
    unknown_outcome: int = 0
    segments_rendered: int = 0
    segments_planned: int = 0
    edits: int = 0
    regenerations: int = 0
    reviews: int = 0
    degraded_reviews: int = 0
    identity_failures: int = 0
    contamination_detections: int = 0
    generated_seconds: float = 0.0
    final_duration_s: float | None = None
    output_version_seconds: float = 0.0
    estimated_cost_usd: Decimal = Decimal("0")
    durations: list[float] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.completed / self.interactions if self.interactions else 0.0

    @property
    def chain_completion(self) -> float:
        return self.segments_rendered / self.segments_planned if self.segments_planned else 0.0

    @property
    def mean_attempts_per_segment(self) -> float:
        return self.interactions / self.segments_rendered if self.segments_rendered else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "interactions": self.interactions,
            "completed": self.completed,
            "failed": self.failed,
            "unknown_outcome": self.unknown_outcome,
            "success_rate": round(self.success_rate, 3),
            "segments_rendered": self.segments_rendered,
            "segments_planned": self.segments_planned,
            "chain_completion": round(self.chain_completion, 3),
            "mean_attempts_per_segment": round(self.mean_attempts_per_segment, 2),
            "edits": self.edits,
            "regenerations": self.regenerations,
            "reviews": self.reviews,
            "degraded_reviews": self.degraded_reviews,
            "identity_failures": self.identity_failures,
            "contamination_detections": self.contamination_detections,
            "generated_seconds": round(self.generated_seconds, 2),
            "final_duration_s": self.final_duration_s,
            "output_version_seconds": self.output_version_seconds,
            "estimated_cost_usd": str(self.estimated_cost_usd),
        }


def collect_job_metrics(manifest: Manifest, *, db: Database | None = None) -> JobMetrics:
    """Aggregate one job's ledger."""
    metrics = JobMetrics(
        job_id=manifest.job_id,
        interactions=len(manifest.interactions),
        segments_planned=len(manifest.segment_plan),
        segments_rendered=len(manifest.segment_artifacts()),
    )

    for record in manifest.interactions:
        status = str(record.status).lower()
        if status == "completed":
            metrics.completed += 1
        elif status in ("failed", "cancelled", "incomplete"):
            metrics.failed += 1
        if not record.outcome_known:
            metrics.unknown_outcome += 1
        if str(record.task) == "edit":
            metrics.edits += 1
        if record.estimated_cost_usd is not None:
            metrics.estimated_cost_usd += record.estimated_cost_usd

    for artifact in manifest.segment_artifacts():
        if artifact.media and artifact.media.duration_s:
            metrics.output_version_seconds += artifact.media.duration_s
            metrics.durations.append(artifact.media.duration_s)

    metrics.generated_seconds = sum(
        e.video_seconds for e in manifest.budget_events if e.kind == "authorize"
    )
    last = manifest.last_usable_artifact()
    metrics.final_duration_s = last.media.duration_s if last and last.media else None
    for entry in manifest.quality_reports:
        report = entry.get("report")
        if not isinstance(report, dict):
            continue
        metrics.reviews += 1
        if report.get("critic_degraded"):
            metrics.degraded_reviews += 1
        if float(report.get("identity_score", 1.0)) < 0.7:
            metrics.identity_failures += 1
        if (
            report.get("timestamp_detected")
            or report.get("text_overlay_detected")
            or report.get("ui_detected")
        ):
            metrics.contamination_detections += 1

    metrics.regenerations = max(0, metrics.interactions - metrics.segments_rendered - metrics.edits)

    _ = db  # reserved: a later exporter may join extra tables
    return metrics


def collect_provider_metrics(db: Database) -> list[dict[str, Any]]:
    """Provider/project availability from stored probes (§23)."""
    return [
        {
            "provider": caps.provider,
            "project": caps.project,
            "model": caps.model,
            "probed_at": caps.probed_at,
            "t2v": caps.t2v,
            "edit": caps.edit,
            "extend": caps.extend,
            "stateful_chain": caps.stateful_previous_interaction_id,
            "max_total_chain_s": caps.max_total_chain_s,
            "blocked_or_unknown": [n for n in caps.notes if "BLOCKED" in n or "UNKNOWN" in n],
        }
        for caps in db.all_probes()
    ]


def fleet_summary(db: Database, limit: int = 100) -> dict[str, Any]:
    """Across every job in the database."""
    jobs = db.list_jobs(limit=limit)
    states: dict[str, int] = {}
    for job in jobs:
        states[str(job["state"])] = states.get(str(job["state"]), 0) + 1

    return {
        "jobs": len(jobs),
        "by_state": states,
        "completed": states.get("COMPLETE", 0),
        "needing_human": sum(
            states.get(s, 0) for s in ("NEEDS_HUMAN", "BUDGET_EXHAUSTED", "PROVIDER_UNAVAILABLE")
        ),
        "failed": states.get("FAILED_FINAL", 0),
        "providers": collect_provider_metrics(db),
    }


def format_metrics(metrics: JobMetrics) -> str:
    data = metrics.as_dict()
    width = max(len(k) for k in data)
    return "\n".join(f"{k.ljust(width)}  {v}" for k, v in data.items())
