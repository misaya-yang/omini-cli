"""Shared job context.

Everything a pipeline stage needs, assembled once: the pinned provider, the
manifest store, the budget, the prompt compiler, and the resolved settings.

Why a context object rather than passing eight arguments around: the pinned
provider/project must be the *same object* in every stage. Rebuilding it per
stage is how a job silently ends up talking to a different project than it
started on (§24.4), so the binding lives here and stages read it.

The budget is reconstructed from the manifest's recorded events, not from a fresh
default, so resuming a job cannot hand it a second full budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from omni_homevlog.agents.critic import Critic
from omni_homevlog.agents.director import Director
from omni_homevlog.budget import Budget, CallKind, budget_for_mode
from omni_homevlog.config import QualityThresholds, Settings, get_settings, load_thresholds
from omni_homevlog.errors import ConfigError, JobNotFoundError
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.prompts.compiler import PromptCompiler
from omni_homevlog.providers.factory import ProviderBinding, provider_from_spec
from omni_homevlog.schemas import (
    ContinuityBible,
    InteractionRecord,
    Manifest,
    ProjectSpec,
    ProviderCapabilities,
    SegmentPlan,
)
from omni_homevlog.storage.database import Database
from omni_homevlog.storage.local import JobPaths, LocalStore
from omni_homevlog.storage.manifest import ManifestStore

logger = get_logger("context")


@dataclass
class JobContext:
    """One job's runtime context."""

    manifest: Manifest
    paths: JobPaths
    store: ManifestStore
    db: Database
    binding: ProviderBinding
    budget: Budget
    settings: Settings
    thresholds: QualityThresholds
    capabilities: ProviderCapabilities | None = None
    _compiler: PromptCompiler | None = field(default=None, repr=False)
    _director: Director | None = field(default=None, repr=False)
    _critic: Critic | None = field(default=None, repr=False)

    # ── construction ───────────────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        job_id: str,
        *,
        settings: Settings | None = None,
        allow_cross_project: bool = False,
    ) -> JobContext:
        """Rebuild the context for an existing job.

        The provider comes from the *spec*, not the environment, so a changed
        `GOOGLE_CLOUD_PROJECT` cannot redirect a running chain.
        """
        cfg = settings or get_settings()
        store_root = LocalStore(cfg.data_dir()).ensure()
        paths = store_root.job(job_id)
        if not paths.exists():
            raise JobNotFoundError(f"No such job: {job_id}")

        db = Database(paths.db_path)
        store = ManifestStore(paths, db)
        manifest = store.load()
        spec = manifest.spec
        if spec is None:
            raise ConfigError(f"Job {job_id} has no project spec; it was never planned.")

        binding = provider_from_spec(
            spec,
            paths=paths,
            settings=cfg,
            allow_cross_project=allow_cross_project,
        )
        budget = budget_from_manifest(manifest, spec)
        capabilities = _capabilities_from_manifest(manifest)
        binding.provider.capabilities = capabilities
        cfg = cfg.model_copy(
            update={
                "omni_provider": spec.provider,
                "google_cloud_project": spec.project,
                "omni_llm_ledger": paths.debug_dir / "llm_calls.json",
                "omni_max_llm_calls": spec.max_llm_calls,
            }
        )

        return cls(
            manifest=manifest,
            paths=paths,
            store=store,
            db=db,
            binding=binding,
            budget=budget,
            settings=cfg,
            thresholds=load_thresholds(),
            capabilities=capabilities,
        )

    @classmethod
    def create(
        cls,
        *,
        spec: ProjectSpec,
        settings: Settings | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> JobContext:
        """Create a brand-new job and its on-disk structure."""
        from omni_homevlog.schemas import JobState, Manifest
        from omni_homevlog.storage.local import make_job_id

        cfg = settings or get_settings()
        store_root = LocalStore(cfg.data_dir()).ensure()
        job_id = make_job_id()
        paths = store_root.job(job_id).ensure()

        binding = provider_from_spec(spec, paths=paths, settings=cfg, allow_cross_project=True)
        # Fill in whatever the spec left open so later resumption is unambiguous.
        spec.project = binding.project
        spec.model = binding.model
        spec.location = binding.location
        if spec.max_estimated_cost_usd is None:
            spec.max_estimated_cost_usd = cfg.omni_max_estimated_cost_usd
        binding.provider.capabilities = capabilities
        cfg = cfg.model_copy(
            update={
                "omni_provider": spec.provider,
                "google_cloud_project": spec.project,
                "omni_llm_ledger": paths.debug_dir / "llm_calls.json",
                "omni_max_llm_calls": spec.max_llm_calls,
            }
        )

        manifest = Manifest(
            job_id=job_id,
            provider=binding.provider_name,
            project=binding.project,
            model=binding.model,
            state=JobState.CREATED,
            spec=spec,
            capability_snapshot=capabilities.model_dump(mode="json") if capabilities else None,
        )

        db = Database(paths.db_path)
        store = ManifestStore(paths, db)
        store.create(manifest)
        _write_plans(paths, spec=spec)

        budget = budget_for_mode(
            mode=spec.mode,
            target_duration_s=spec.target_duration_s,
            max_total_calls=spec.max_total_calls,
            # The spec wins over the environment: a ceiling chosen for one job is
            # more specific than a machine-wide default.
            max_estimated_cost_usd=(
                spec.max_estimated_cost_usd
                if spec.max_estimated_cost_usd is not None
                else cfg.omni_max_estimated_cost_usd
            ),
        )

        return cls(
            manifest=manifest,
            paths=paths,
            store=store,
            db=db,
            binding=binding,
            budget=budget,
            settings=cfg,
            thresholds=load_thresholds(),
            capabilities=capabilities,
        )

    # ── accessors ──────────────────────────────────────────────────────────

    @property
    def job_id(self) -> str:
        return self.manifest.job_id

    @property
    def spec(self) -> ProjectSpec:
        assert self.manifest.spec is not None
        return self.manifest.spec

    @property
    def provider(self) -> Any:
        return self.binding.provider

    @property
    def bible(self) -> ContinuityBible | None:
        return self.manifest.continuity_bible

    @property
    def segment_plan(self) -> list[SegmentPlan]:
        return self.manifest.segment_plan

    @property
    def compiler(self) -> PromptCompiler:
        if self._compiler is None:
            bible = self.bible
            if bible is None:
                raise ConfigError("Cannot compile prompts before the plan exists.")
            self._compiler = PromptCompiler(
                bible=bible,
                references=self.manifest.references,
                spec=self.spec,
            )
        return self._compiler

    @property
    def director(self) -> Director:
        if self._director is None:
            self._director = Director(settings=self.settings)
        return self._director

    @property
    def critic(self) -> Critic:
        if self._critic is None:
            self._critic = Critic(settings=self.settings)
        return self._critic

    # ── mutation helpers ──────────────────────────────────────────────────

    def reload(self) -> Manifest:
        self.manifest = self.store.load()
        self._compiler = None
        return self.manifest

    def note(self, message: str) -> None:
        self.manifest = self.store.append_note(message)

    def error(self, message: str) -> None:
        self.manifest = self.store.append_error(message)

    def authorize(
        self,
        kind: CallKind,
        *,
        segment_index: int,
        attempt_index: int,
        video_seconds: int = 0,
    ) -> None:
        """Reserve budget and record the event in the manifest + database."""
        from omni_homevlog.costing import (
            estimate_video_seconds_cost,
            has_video_pricing,
            load_pricing,
        )
        from omni_homevlog.errors import BudgetExhaustedError

        pricing = load_pricing(self.settings.omni_pricing_file)
        if self.budget.max_estimated_cost_usd is not None and not has_video_pricing(
            self.spec.model or "", pricing
        ):
            raise BudgetExhaustedError(
                "Dollar ceiling cannot be enforced: video pricing is unknown. Configure pricing.yaml first."
            )

        # No falsy-zero guard here. `if video_seconds` skipped the estimate for the
        # value every EDIT reservation passes, so both the cost ceiling and the
        # video-seconds ceiling were blind to every edit in every job — and each
        # edit authorised the next. The estimator returns zero for zero seconds on
        # its own, which is the correct answer, not a reason to skip it.
        estimated = estimate_video_seconds_cost(
            model=self.spec.model or "", video_seconds=video_seconds, pricing=pricing
        )
        event = self.budget.authorize(
            kind,
            segment_index=segment_index,
            attempt_index=attempt_index,
            video_seconds=video_seconds,
            estimated_cost_usd=estimated,
        )
        self.manifest = self.store.record_budget_event(event)
        self.manifest = self.store.mutate(budget=self.budget.snapshot())

    def report_budget(self) -> dict[str, Any]:
        return self.budget.snapshot()

    def mirror(self, stage: str) -> Any:
        """Push one stage of the job tree to GCS, if a bucket is configured.

        Best effort by construction: the local job is authoritative and a bucket
        problem must never fail a job whose render already succeeded (§13.1).
        """
        from omni_homevlog.storage.sync import best_effort_mirror

        result = best_effort_mirror(
            self.paths, self.binding.provider.gcs, self.binding.provider.gcs_prefix, stage
        )
        if result.failed:
            self.error(f"GCS mirror ({stage}) had failures: {result.failed}")
        return result


def _call_kind_of(record: InteractionRecord) -> CallKind:
    """Which counter a ledger row belongs to.

    Prefers the recorded `call_kind`, because `task` describes what the provider
    ran rather than why we called it: a regenerated segment re-runs `extend` or
    `text_to_video`, so a task-based mapping could never produce
    `CallKind.REGENERATE`. Every per-segment repair ceiling then read zero after a
    resume, and each restart handed the job another round of paid repairs, bounded
    only by the job-wide call ceiling. It failed the other way too: a single seed
    plus one regeneration counted as two seeds and tripped the seed ceiling.

    The `task`-based fallback stays for rows written before this field existed.
    """
    if record.call_kind:
        try:
            return CallKind(record.call_kind)
        except ValueError:
            pass

    return {
        "text_to_video": CallKind.SEED,
        "reference_to_video": CallKind.SEED,
        "image_to_video": CallKind.SEED,
        "extend": CallKind.EXTEND,
        "edit": CallKind.EDIT,
    }.get(str(record.task), CallKind.SEED)


def budget_from_manifest(manifest: Manifest, spec: ProjectSpec) -> Budget:
    """Reconstruct spend so far, so a resume cannot reset the ceiling.

    Counted from the recorded interactions rather than from budget events: an
    interaction is the thing that actually cost money, and a crash between
    authorising and dispatching should not forgive the reservation.
    """
    budget = budget_for_mode(
        mode=spec.mode,
        target_duration_s=spec.target_duration_s,
        max_total_calls=spec.max_total_calls,
        max_estimated_cost_usd=spec.max_estimated_cost_usd,
    )

    for limit_name in (
        "max_seed_attempts",
        "max_edit_attempts_per_segment",
        "max_regenerations_per_segment",
        "max_video_seconds_requested",
    ):
        if limit_name in manifest.budget:
            setattr(budget, limit_name, int(manifest.budget[limit_name]))
    reservations = [e for e in manifest.budget_events if e.kind == "authorize"]
    if reservations:
        for event in reservations:
            budget.calls_made += 1
            budget.video_seconds_requested += event.video_seconds
            budget.estimated_cost_usd += event.estimated_cost_usd
            budget._by_kind_segment[f"{event.call_kind}:{event.segment_index}"] += 1
        budget.estimated_cost_usd += sum(
            (e.estimated_cost_usd for e in manifest.budget_events if e.kind == "spend"),
            Decimal("0"),
        )
    else:
        # Backward compatibility with jobs written before reservation events.
        for record in manifest.interactions:
            budget.calls_made += 1
            kind = _call_kind_of(record)
            budget._by_kind_segment[f"{kind}:{record.segment_index}"] += 1
            try:
                budget.video_seconds_requested += int(str(record.duration or "10").rstrip("s"))
            except ValueError:
                budget.video_seconds_requested += 10
            budget.estimated_cost_usd += record.estimated_cost_usd or Decimal("0")

    return budget


def _capabilities_from_manifest(manifest: Manifest) -> ProviderCapabilities | None:
    if not manifest.capability_snapshot:
        return None
    try:
        return ProviderCapabilities.model_validate(manifest.capability_snapshot)
    except Exception:
        return None


def _write_plans(paths: JobPaths, *, spec: ProjectSpec) -> None:
    from omni_homevlog.storage.local import atomic_write_json

    atomic_write_json(paths.project_spec_path, spec.model_dump(mode="json"))


def write_plan_artifacts(
    paths: JobPaths,
    *,
    spec: ProjectSpec,
    bible: ContinuityBible,
    segments: list[SegmentPlan],
) -> None:
    """Persist the plan trio, one file per §13.1."""
    from omni_homevlog.storage.local import atomic_write_json

    atomic_write_json(paths.project_spec_path, spec.model_dump(mode="json"))
    atomic_write_json(paths.continuity_bible_path, bible.model_dump(mode="json"))
    atomic_write_json(paths.segment_plan_path, [s.model_dump(mode="json") for s in segments])


def reference_dir_for(job_id: str, settings: Settings | None = None) -> Path:
    cfg = settings or get_settings()
    return LocalStore(cfg.data_dir()).job(job_id).references_dir
