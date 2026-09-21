"""Provider construction and project binding (§8.3).

Two rules this module exists to enforce:

  1. **A job binds to one project for its whole life.** `bind_provider` is called
     once, at job creation, and the resulting provider plus project id are stored
     on the spec. Later stages reconstruct from the spec, never from the current
     environment — otherwise a changed `GOOGLE_CLOUD_PROJECT` would silently
     point the next extension at a different project and the chain would be
     broken in a way the manifest could not explain.

  2. **Multi-project use is never automatic.** §8.3 permits `--all-projects` for
     *discovery*, and multi-project setups for environment/billing separation and
     human choice. §24.4 and §24.5 forbid automatic switching and key rotation.
     `list_available_projects` therefore only reports; it never picks a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from omni_homevlog.config import Settings, get_settings
from omni_homevlog.errors import ConfigError, MissingCredentialsError, ResumeConflictError
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.providers.base import BaseVideoProvider
from omni_homevlog.providers.gemini_api import DEFAULT_MODEL as GEMINI_DEFAULT_MODEL
from omni_homevlog.providers.gemini_api import GeminiDeveloperProvider
from omni_homevlog.providers.vertex_enterprise import DEFAULT_MODEL as VERTEX_DEFAULT_MODEL
from omni_homevlog.providers.vertex_enterprise import VertexEnterpriseProvider
from omni_homevlog.schemas import ProjectSpec, ProviderCapabilities, ProviderName
from omni_homevlog.storage.gcs import GcsClient
from omni_homevlog.storage.local import JobPaths

logger = get_logger("factory")


@dataclass(slots=True)
class ProviderBinding:
    """The provider plus the identifiers it is pinned to."""

    provider: BaseVideoProvider
    provider_name: ProviderName
    project: str | None
    model: str
    location: str | None

    def as_spec_fields(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "project": self.project,
            "model": self.model,
            "location": self.location,
        }


def resolve_model(provider_name: ProviderName, settings: Settings, override: str | None) -> str:
    if override:
        return override
    if provider_name == "vertex":
        return settings.omni_vertex_model or VERTEX_DEFAULT_MODEL
    return settings.omni_gemini_model or GEMINI_DEFAULT_MODEL


def build_provider(
    *,
    provider_name: ProviderName | None = None,
    project: str | None = None,
    model: str | None = None,
    location: str | None = None,
    paths: JobPaths | None = None,
    settings: Settings | None = None,
    with_gcs: bool | None = None,
    gcs_prefix: str | None = None,
) -> ProviderBinding:
    """Construct a provider for a *new* job.

    Use `provider_from_spec` instead when resuming — that one refuses to drift.
    """
    cfg = settings or get_settings()
    name: ProviderName = provider_name or cfg.omni_provider
    resolve_location = location or cfg.google_cloud_location
    resolved_model = resolve_model(name, cfg, model)

    gcs: GcsClient | None = None
    # An explicit `--gcs-uri` beats the environment. It used to be passed only as
    # `with_gcs=bool(...)` while the prefix came solely from the environment, so
    # `create --gcs-uri gs://...` built a client that had nowhere to write: the
    # seed rendered inline, the job could never extend, and `delete` targeted a
    # prefix that was never used.
    gcs_prefix = gcs_prefix or cfg.omni_output_gcs_uri
    want_gcs = (
        with_gcs if with_gcs is not None else bool(gcs_prefix and cfg.omni_prefer_gcs_delivery)
    )
    if want_gcs:
        candidate = GcsClient()
        gcs = candidate
        if gcs_prefix:
            logger.info(
                "GCS delivery enabled",
                extra={"extra_fields": {"prefix": gcs_prefix, "backend": candidate.backend}},
            )

    common: dict[str, Any] = {
        "paths": paths,
        "gcs": gcs,
        "gcs_prefix": gcs_prefix,
        "prefer_gcs_delivery": cfg.omni_prefer_gcs_delivery,
        "timeout_s": cfg.omni_request_timeout_s,
        "keep_raw": cfg.omni_keep_raw_responses,
    }

    if name == "vertex":
        resolved_project = cfg.resolve_project(project)
        provider: BaseVideoProvider = VertexEnterpriseProvider(
            project=resolved_project,
            location=resolve_location,
            model=resolved_model,
            **common,
        )
        return ProviderBinding(
            provider=provider,
            provider_name=name,
            project=resolved_project,
            model=resolved_model,
            location=resolve_location,
        )

    if not cfg.gemini_api_key:
        raise MissingCredentialsError(
            "OMNI_PROVIDER=gemini_api requires GEMINI_API_KEY.",
            detail={"provider": name},
        )
    provider = GeminiDeveloperProvider(api_key=cfg.gemini_api_key, model=resolved_model, **common)
    return ProviderBinding(
        provider=provider,
        provider_name=name,
        project=None,
        model=resolved_model,
        location=None,
    )


def provider_from_spec(
    spec: ProjectSpec,
    *,
    paths: JobPaths | None = None,
    settings: Settings | None = None,
    allow_cross_project: bool = False,
) -> ProviderBinding:
    """Rebuild the provider a job was created with.

    §5.1: if the job's project is unavailable, enter `NEEDS_HUMAN`. Do not
    quietly switch. `allow_cross_project=True` is the explicit human override and
    the caller must set `degraded_cross_project_resume` on the manifest.
    """
    cfg = settings or get_settings()
    project = spec.project

    if spec.provider == "vertex":
        if not project and not allow_cross_project:
            raise ConfigError(
                "Job spec has no project bound. Refusing to pick one implicitly (§8.3).",
                detail={"provider": spec.provider},
            )
        resolved = project or cfg.resolve_project(None)
        if (
            project
            and cfg.google_cloud_project
            and cfg.google_cloud_project != project
            and not allow_cross_project
        ):
            raise ResumeConflictError(
                f"This job is bound to project {project!r}, but the environment now "
                f"names {cfg.google_cloud_project!r}. Refusing to resume across "
                "projects. Pass --allow-cross-project only if you intend to break "
                "the chain's provenance (it will be recorded as degraded).",
                detail={"job_project": project, "env_project": cfg.google_cloud_project},
            )
    else:
        resolved = None

    return build_provider(
        provider_name=spec.provider,
        project=resolved,
        model=spec.model,
        location=spec.location,
        paths=paths,
        settings=cfg,
        gcs_prefix=spec.gcs_uri,
    )


def list_available_projects(*, settings: Settings | None = None) -> list[dict[str, str]]:
    """Discovery only (§8.3, `doctor --all-projects`).

    Reports what the credential can see. It deliberately does **not** rank them,
    pick a default, or suggest pooling — §24.5 forbids automatic project switching
    to work around quota, and a "helpful" ordering here would invite exactly that.
    """
    cfg = settings or get_settings()
    projects: list[dict[str, str]] = []
    try:
        import google.auth  # noqa: F401

        _, detected = _adc_project()
        if detected:
            projects.append({"project": detected, "source": "adc", "note": "from ADC"})
    except Exception as exc:
        logger.debug("ADC project discovery failed", extra={"extra_fields": {"error": str(exc)}})

    if cfg.google_cloud_project and cfg.google_cloud_project not in {
        p["project"] for p in projects
    }:
        projects.append(
            {"project": cfg.google_cloud_project, "source": "env", "note": "GOOGLE_CLOUD_PROJECT"}
        )

    return projects


def _adc_project() -> tuple[Any, str | None]:
    import google.auth

    return google.auth.default()


def check_provider_ready(binding: ProviderBinding) -> tuple[bool, str]:
    """Pre-flight used before a job is created, so we fail before spending."""
    if binding.provider_name == "vertex":
        from omni_homevlog.providers.auth import check_adc_available

        ok, message, _ = check_adc_available()
        if not ok:
            return False, message
        return True, f"Vertex ready (project={binding.project}, model={binding.model})"

    key = getattr(binding.provider.transport, "_api_key", None)
    if not key:
        return False, "GEMINI_API_KEY missing"
    return True, f"Gemini Developer API ready (model={binding.model})"


def latest_capabilities(
    *,
    provider: str,
    project: str | None,
    model: str,
    settings: Settings | None = None,
) -> ProviderCapabilities | None:
    """The most recent probe result for a surface, if one was recorded.

    `JobContext.create` has always accepted a capability record, and no CLI path
    ever supplied one — so `docs/LIMITATIONS.md`'s "run `doctor` first and the
    snapshot is carried into any job created afterward" described a flow that did
    not exist. This is that lookup.
    """
    from omni_homevlog.storage.database import Database
    from omni_homevlog.storage.local import LocalStore

    cfg = settings or get_settings()
    probe_db = LocalStore(cfg.data_dir()).job("job-doctor-probe").db_path
    if not probe_db.exists():
        return None

    try:
        return Database(probe_db).latest_probe(provider, project, model)
    except Exception:
        logger.debug("No stored capability probe could be read")
        return None
