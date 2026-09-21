"""Mock provider, director, and critic for end-to-end pipeline tests.

These fakes let the whole pipeline run for real — real job directory, real SQLite,
real manifest, real state machine — with only the network calls replaced. That is
the point: §21.2's failure matrix is about how the *pipeline* handles bad provider
behaviour, so the pipeline must be the genuine article.

Nothing here touches a network. `FakeProvider` writes real minimal MP4 files so
the media probe and C2PA detection run against actual bytes.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from tests.conftest import build_minimal_mp4, make_report

from omni_homevlog.agents.director import DirectorResult
from omni_homevlog.errors import (
    PermissionError_,
    ProviderError,
    QuotaExhaustedError,
    RequestTimeoutUnknownOutcome,
    ServerError,
)
from omni_homevlog.schemas import (
    CritiqueReport,
    DirectorPlan,
    JobState,
    Manifest,
    ProjectSpec,
    RenderArtifact,
    sha256_text,
    utc_now_iso,
)
from omni_homevlog.storage.local import atomic_write_bytes

# ─────────────────────────────────────────────────────────────────────────────
# Provider
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FakeProviderBehaviour:
    """Knobs for the provider's failure modes, one per §21.2 row."""

    #: Raise on the Nth call (1-based). None means never.
    fail_on_call: int | None = None
    fail_with: Exception | None = None
    #: Fail on the first N calls, then succeed.
    fail_first_n: int = 0
    #: Return this on the next call and then clear itself.
    raise_once: list[Exception] = field(default_factory=list)
    #: Report a status other than "completed".
    status: str = "completed"
    #: Produce no video content at all.
    omit_video: bool = False
    #: Pretend the local write failed after the provider succeeded.
    fail_download: bool = False
    #: Emit a second model_output step with a video.
    duplicate_video: bool = False
    #: Seconds each render claims.
    duration_s: float = 10.0
    #: Include a C2PA box in the produced file.
    with_c2pa: bool = True
    #: Delay before returning, to make latency non-zero.
    latency_s: float = 0.05


class FakeProvider:
    """Implements the `VideoProvider` protocol without a network.

    Written to fail in the specific ways §21.2 enumerates, so a test can name the
    behaviour it wants and assert on the pipeline's response.
    """

    provider_name = "vertex"
    default_model = "gemini-omni-1.1-flash-preview"

    def __init__(
        self,
        *,
        paths: Any,
        behaviour: FakeProviderBehaviour | None = None,
        project: str = "test-project",
        model: str = "gemini-omni-1.1-flash-preview",
    ) -> None:
        self.paths = paths
        self.behaviour = behaviour or FakeProviderBehaviour()
        self.project = project
        self.model = model
        self.gcs_prefix = None
        self.gcs = None
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    #  the protocol ───────────────────────────────────────────────────────

    async def probe(self) -> Any:
        from omni_homevlog.schemas import ProviderCapabilities

        return ProviderCapabilities(
            provider=self.provider_name,
            project=self.project,
            model=self.model,
            t2v=True,
            reference_to_video=True,
            edit=True,
            extend=True,
            stateful_previous_interaction_id=True,
            gcs_delivery=False,
            uri_delivery=False,
            native_audio=True,
            max_total_chain_s=30,
        )

    async def generate_seed(
        self,
        *,
        prompt: str,
        assets: list[Any],
        spec: ProjectSpec,
        segment_index: int = 0,
        attempt_index: int = 0,
        duration_s: int | None = None,
        reference_uris: dict[str, str] | None = None,
    ) -> RenderArtifact:
        task = "reference_to_video" if assets else "text_to_video"
        return await self._produce(
            prompt=prompt,
            task=task,
            segment_index=segment_index,
            attempt_index=attempt_index,
            duration_s=duration_s or 10,
            parent=None,
            kind="raw",
            has_references=bool(assets),
        )

    async def edit(
        self,
        *,
        artifact: RenderArtifact,
        edit_prompt: str,
        spec: ProjectSpec,
        segment_index: int | None = None,
        attempt_index: int = 1,
    ) -> RenderArtifact:
        return await self._produce(
            prompt=edit_prompt,
            task="edit",
            segment_index=segment_index if segment_index is not None else 0,
            attempt_index=attempt_index,
            duration_s=artifact.requested_duration_s or 10,
            parent=artifact,
            kind="edit",
        )

    async def extend(
        self,
        *,
        artifact: RenderArtifact,
        extension_prompt: str,
        spec: ProjectSpec,
        segment_index: int | None = None,
        attempt_index: int = 0,
        duration_s: int = 10,
    ) -> RenderArtifact:
        return await self._produce(
            prompt=extension_prompt,
            task="extend",
            segment_index=segment_index if segment_index is not None else 1,
            attempt_index=attempt_index,
            duration_s=duration_s,
            parent=artifact,
            kind="extend",
        )

    async def get_interaction(self, interaction_id: str) -> RenderArtifact:
        """Recovery query. Read-only, and never billed."""
        self.calls.append({"kind": "get_interaction", "interaction_id": interaction_id})
        await asyncio.sleep(self.behaviour.latency_s)
        artifact = RenderArtifact(
            interaction_id=interaction_id,
            task="extend",
            model=self.model,
            provider=self.provider_name,
            project=self.project,
            status="completed",
            prompt="",
            prompt_sha256="",
        )
        return artifact

    def chain_strategy(self, caps: Any) -> str:
        return "A"

    def build_payload(
        self, request: Any, *, include_generation_config: bool = True
    ) -> dict[str, Any]:
        """Satisfy the protocol so the fake cannot mask a missing method.

        `capability_probe.probe_generation` builds its request through the provider
        so a probe measures the shape the pipeline sends. A fake without this would
        pass every pipeline test and then fail the moment anyone probed with it.
        """
        from omni_homevlog.providers.request_builder import build_create_payload

        return build_create_payload(
            model=self.model,
            task=request.task,
            prompt=request.prompt,
            aspect_ratio=request.aspect_ratio,
            resolution=request.resolution,
            duration_s=request.duration_s,
            previous_interaction_id=request.parent_interaction_id,
            input_video_uri=request.input_video_uri,
            include_generation_config=include_generation_config,
        )

    #  internals ──────────────────────────────────────────────────────────

    async def _produce(
        self,
        *,
        prompt: str,
        task: str,
        segment_index: int,
        attempt_index: int,
        duration_s: int,
        parent: RenderArtifact | None,
        kind: str,
        has_references: bool = False,
    ) -> RenderArtifact:
        self._counter += 1
        call_number = self._counter
        self.calls.append(
            {
                "kind": task,
                "segment_index": segment_index,
                "attempt_index": attempt_index,
                "prompt": prompt,
                "duration_s": duration_s,
                "parent": parent.interaction_id if parent else None,
                "call_number": call_number,
            }
        )

        await asyncio.sleep(self.behaviour.latency_s)

        if self.behaviour.raise_once:
            raise self.behaviour.raise_once.pop(0)
        if self.behaviour.fail_on_call is not None and call_number == self.behaviour.fail_on_call:
            raise self.behaviour.fail_with or ServerError("injected failure", http_status=500)
        if call_number <= self.behaviour.fail_first_n:
            raise self.behaviour.fail_with or ServerError("injected", http_status=500)

        interaction_id = f"int-{task}-{segment_index:02d}-{attempt_index:02d}-{call_number:04d}"

        if self.behaviour.status != "completed":
            artifact = RenderArtifact(
                interaction_id=interaction_id,
                parent_interaction_id=parent.interaction_id if parent else None,
                task=task,  # type: ignore[arg-type]
                model=self.model,
                provider=self.provider_name,
                project=self.project,
                status=self.behaviour.status,  # type: ignore[arg-type]
                prompt=prompt,
                prompt_sha256=sha256_text(prompt),
                requested_duration_s=duration_s,
                error_message=f"provider reported status {self.behaviour.status}",
            )
            return artifact

        target = self.paths.attempt_path(segment_index, attempt_index, kind=kind)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(
            target,
            build_minimal_mp4(
                duration_s=min(self.behaviour.duration_s, float(duration_s)),
                with_c2pa=self.behaviour.with_c2pa,
            ),
        )

        # Probe the bytes we just wrote, exactly as the real provider does after a
        # download. If the fake skipped this, the pipeline's media verification
        # would go untested.
        from omni_homevlog.media.ffprobe import inspect_media

        media = inspect_media(target)

        return RenderArtifact(
            interaction_id=interaction_id,
            parent_interaction_id=parent.interaction_id if parent else None,
            task=task,  # type: ignore[arg-type]
            model=self.model,
            provider=self.provider_name,
            project=self.project,
            segment_index=segment_index,
            status="completed",
            local_path=str(target),
            artifact_relpath=self.paths.relpath(target),
            prompt=prompt,
            prompt_sha256=sha256_text(prompt),
            resolution="360p",
            aspect_ratio="9:16",
            requested_duration_s=duration_s,
            media=media,
            usage={"text_input_tokens": 45, "video_output_tokens": 5793},
            created_at=utc_now_iso(),
            completed_at=utc_now_iso(),
            latency_s=self.behaviour.latency_s,
        )


class DownloadFailingProvider(FakeProvider):
    """Succeeds at the provider, then fails to materialise the bytes.

    §21.2's "interaction completed but download failed" row. The artifact exists
    with a URI and no local file, which is what recovery rule 3 must handle.
    """

    async def _produce(self, **kwargs: Any) -> RenderArtifact:
        artifact = await super()._produce(**kwargs)
        artifact.local_path = None
        artifact.gcs_uri = f"gs://test-bucket/jobs/x/{artifact.interaction_id}.mp4"
        return artifact


class NoVideoProvider(FakeProvider):
    """Reports completed but returns no video content."""

    async def _produce(self, **kwargs: Any) -> RenderArtifact:
        await super()._produce(**kwargs)
        raise ProviderError(
            "Interaction finished with status 'completed' but contained no video content."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Director and Critic
# ─────────────────────────────────────────────────────────────────────────────


class FakeDirector:
    def __init__(self, plan: DirectorPlan, *, warnings: list[str] | None = None) -> None:
        self._plan = plan
        self._warnings = warnings or []
        self.calls = 0

    def plan(self, **_kwargs: Any) -> DirectorResult:
        self.calls += 1
        return DirectorResult(
            plan=self._plan,
            model="fake-director",
            usage={"total": 1},
            warnings=list(self._warnings),
        )


class FailingDirector:
    def __init__(self, message: str = "director unavailable") -> None:
        from omni_homevlog.agents.director import PlanRejectedError

        self._error = PlanRejectedError(message)

    def plan(self, **_kwargs: Any) -> DirectorResult:
        raise self._error


@dataclass
class FakeCriticResult:
    report: CritiqueReport
    inputs: Any = None
    contradictions: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""


class ScriptedCritic:
    """Returns a queued sequence of reports, one per review call.

    The last report repeats once the queue is empty, so a test can script "fail
    twice then pass" without having to know how many reviews will happen.
    """

    def __init__(self, reports: list[CritiqueReport]) -> None:
        if not reports:
            raise ValueError("ScriptedCritic needs at least one report")
        self._reports = reports
        self.calls = 0

    def review_video(self, **_kwargs: Any) -> FakeCriticResult:
        index = min(self.calls, len(self._reports) - 1)
        self.calls += 1
        return FakeCriticResult(report=self._reports[index].model_copy(deep=True))


def passing_report(**overrides: Any) -> CritiqueReport:
    return make_report(**overrides)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_plan(spec: ProjectSpec, bible, segments) -> DirectorPlan:
    return DirectorPlan(
        title=spec.title, logline=spec.brief, continuity_bible=bible, segments=segments
    )


@pytest.fixture
def build_job(tmp_path, spec, fake_plan, monkeypatch):
    """Create a real `JobContext` with fakes wired in.

    Returns a factory so a test can vary the provider behaviour and critic script.
    """

    def _build(
        *,
        spec_override: ProjectSpec | None = None,
        behaviour: FakeProviderBehaviour | None = None,
        provider_cls: type[FakeProvider] = FakeProvider,
        reports: list[CritiqueReport] | None = None,
        director: Any | None = None,
        references: int = 2,
        allow_template_fallback: bool = False,
    ):
        from omni_homevlog.pipeline.context import JobContext

        monkeypatch.setenv("OMNI_DATA_DIR", str(tmp_path / "data"))
        from omni_homevlog.config import reset_settings_cache

        reset_settings_cache()

        resolved = spec_override or spec
        ctx = JobContext.create(spec=resolved)

        # A real job is created with approved references, and the seed is then a
        # reference_to_video render. Pass `references=0` for the bare case.
        if references:
            ctx.manifest = ctx.store.mutate(references=_fake_references(ctx, count=references))

        fake_provider = provider_cls(
            paths=ctx.paths,
            behaviour=behaviour or FakeProviderBehaviour(),
            project=resolved.project or "test-project",
            model=resolved.model or "gemini-omni-1.1-flash-preview",
        )
        ctx.binding.provider = fake_provider
        ctx._director = director if director is not None else FakeDirector(fake_plan)
        ctx._critic = ScriptedCritic(reports or [passing_report()])

        return ctx, fake_provider, allow_template_fallback

    return _build


def _fake_references(ctx: Any, *, count: int = 2):
    """Synthetic approved references written into the job directory."""
    from omni_homevlog.schemas import ReferenceAsset

    roles = ["identity_closeup", "identity_body", "environment", "outfit"]
    assets = []
    for index in range(count):
        role = roles[index % len(roles)]
        path = ctx.paths.references_dir / f"{index:02d}_{role}.png"
        # A tiny real PNG, so the path exists for anything that stats it.
        atomic_write_bytes(path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
        assets.append(
            ReferenceAsset(
                id=f"ref{index:02d}_{role}",
                path_or_uri=str(path),
                role=role,  # type: ignore[arg-type]
                sha256=f"{index:064x}",
                provenance="synthetic",
                approved=True,
                width=1024,
                height=1024,
                mime_type="image/png",
            )
        )
    return assets


@pytest.fixture
def _no_real_network(monkeypatch):
    """Belt and braces: make an accidental real request fail loudly."""

    def explode(*_args: Any, **_kwargs: Any):
        raise AssertionError(
            "A test attempted a real network call. Integration tests must use fakes."
        )

    import requests

    monkeypatch.setattr(requests, "request", explode)
    monkeypatch.setattr(requests.Session, "request", explode)
    yield


# Re-exported so tests can name the failure modes without importing the errors module.
FAILURES = {
    "quota": QuotaExhaustedError("quota exhausted", http_status=429),
    "server": ServerError("internal error", http_status=500),
    "permission": PermissionError_("permission denied", http_status=403),
    "timeout": RequestTimeoutUnknownOutcome("timed out after 600s"),
}


def manifest_state(ctx: Any) -> JobState:
    return Manifest.model_validate(ctx.store.load()).state


def review_files(ctx: Any) -> list[Path]:
    return sorted(ctx.paths.reviews_dir.glob("*.json"))
