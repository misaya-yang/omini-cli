"""The provider interface (§8.1) and the logic both surfaces share.

`VideoProvider` is the seam §28 asks for: when the Preview API changes, only an
adapter should need replacing.

The methods are `async` per §8.1. The transports underneath are synchronous
`requests` calls, so they run in a worker thread via `asyncio.to_thread`. That
keeps the declared interface honest without inventing a concurrency model the
pipeline does not need — the chain is strictly sequential (seed → extend →
extend), because each extension depends on the previous render.

Shared behaviour implemented here, once:

  * build the payload through `request_builder`
  * dispatch, then persist the raw exchange as a redacted debug fixture
  * decode inline base64 **or** pull from GCS
  * verify the bytes really are a video of the requested shape before anyone
    downstream treats them as a render
  * assemble the `RenderArtifact` with its lineage intact
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from omni_homevlog.costing import estimate_from_usage, estimate_video_seconds_cost
from omni_homevlog.errors import (
    InteractionPending,
    InvalidRequestError,
    ProviderError,
    RequestTimeoutUnknownOutcome,
)
from omni_homevlog.media.ffprobe import assert_expected_media, inspect_media
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.providers.request_builder import (
    build_create_payload,
    image_input,
    image_input_inline,
    order_references,
    seed_input_mode,
)
from omni_homevlog.providers.response_parser import InteractionEnvelope
from omni_homevlog.providers.transport import BaseTransport, TransportResult, payload_preview
from omni_homevlog.schemas import (
    AspectRatio,
    ProviderCapabilities,
    ReferenceAsset,
    RenderArtifact,
    Resolution,
    VideoTask,
    sha256_text,
    utc_now_iso,
)
from omni_homevlog.storage.gcs import GcsClient, is_gcs_uri, join_uri
from omni_homevlog.storage.local import JobPaths, atomic_write_bytes

logger = get_logger("provider")


@dataclass(slots=True)
class RenderRequest:
    """Everything one render needs. Keeps the provider signatures short."""

    task: VideoTask
    prompt: str
    segment_index: int
    attempt_index: int
    aspect_ratio: AspectRatio | str
    resolution: Resolution | str
    duration_s: int
    background: bool = False
    expected_duration_s: float | None = None
    parent_interaction_id: str | None = None
    references: list[ReferenceAsset] | None = None
    reference_uris: dict[str, str] | None = None
    input_video_uri: str | None = None
    input_video: dict[str, str] | None = None
    prior_steps: list[dict[str, Any]] | None = None
    first_frame_uri: str | None = None
    last_frame_uri: str | None = None
    artifact_kind: str = "raw"
    labels: dict[str, str] | None = None


@runtime_checkable
class VideoProvider(Protocol):
    """§8.1 interface.

    `build_payload` is part of the contract, not an implementation detail. The
    capability probe builds its requests through it so that a probe measures the
    shape the pipeline actually sends — on Vertex the adapters apply rules the
    generic builder cannot know about (a continuation must omit
    `generation_config`). A provider without `build_payload` would probe one thing
    and run another, which is worse than not probing at all.
    """

    async def probe(self) -> ProviderCapabilities: ...

    def build_payload(
        self, request: RenderRequest, *, include_generation_config: bool = True
    ) -> dict[str, Any]: ...

    async def generate_seed(
        self, *, prompt: str, assets: list[ReferenceAsset], spec: Any
    ) -> RenderArtifact: ...

    async def edit(
        self, *, artifact: RenderArtifact, edit_prompt: str, spec: Any
    ) -> RenderArtifact: ...

    async def extend(
        self, *, artifact: RenderArtifact, extension_prompt: str, spec: Any
    ) -> RenderArtifact: ...

    async def get_interaction(self, interaction_id: str) -> RenderArtifact: ...


class BaseVideoProvider:
    """Shared implementation. Subclasses supply a transport and a model id."""

    provider_name: str = "base"
    default_model: str = "unknown"

    def __init__(
        self,
        *,
        transport: BaseTransport,
        model: str | None = None,
        project: str | None = None,
        paths: JobPaths | None = None,
        gcs: GcsClient | None = None,
        gcs_prefix: str | None = None,
        prefer_gcs_delivery: bool = True,
        store: bool | None = None,
        keep_raw: bool = True,
    ) -> None:
        self.transport = transport
        self.model = model or self.default_model
        self.project = project
        self.paths = paths
        self.gcs = gcs
        self.gcs_prefix = gcs_prefix
        self.prefer_gcs_delivery = prefer_gcs_delivery
        # `store=False` breaks later stateful editing, so it is opt-in and the
        # chain code never sets it (§24.9).
        self.store = True if store is None else store
        self.keep_raw = keep_raw
        self.capabilities: ProviderCapabilities | None = None
        if paths is not None:
            transport.debug_dir = str(paths.debug_dir)

    # ── introspection ──────────────────────────────────────────────────────

    def describe(self) -> str:
        return f"{self.provider_name}:{self.project or '-'}:{self.model}"

    def capabilities_hint(self) -> ProviderCapabilities:
        """Pre-probe defaults.

        These describe the *documented* limits, which the probe then replaces
        with measured ones. Nothing may treat this as verified (§2.3).
        """
        return ProviderCapabilities(
            provider=self.provider_name,
            project=self.project,
            model=self.model,
            max_generation_s=10,
            supported_resolutions=["360p", "720p", "1080p", "4k"],
            notes=["unprobed defaults; run `omni-vlog doctor` for measured values"],
        )

    # ── seed / edit / extend ───────────────────────────────────────────────

    async def generate_seed(
        self,
        *,
        prompt: str,
        assets: list[ReferenceAsset],
        spec: Any,
        segment_index: int = 0,
        attempt_index: int = 0,
        duration_s: int | None = None,
        reference_uris: dict[str, str] | None = None,
    ) -> RenderArtifact:
        task, _ = seed_input_mode([a.role for a in assets])
        request = RenderRequest(
            task=task,
            prompt=prompt,
            segment_index=segment_index,
            attempt_index=attempt_index,
            background=getattr(spec, "background", False),
            aspect_ratio=spec.aspect_ratio,
            resolution=spec.resolution,
            duration_s=duration_s or min(10, spec.target_duration_s),
            references=assets or None,
            reference_uris=reference_uris,
            artifact_kind="raw",
            labels={"job_kind": "seed", "segment": str(segment_index)},
        )
        return await self._render(request)

    async def edit(
        self,
        *,
        artifact: RenderArtifact,
        edit_prompt: str,
        spec: Any,
        segment_index: int | None = None,
        attempt_index: int = 1,
    ) -> RenderArtifact:
        source_input = self._source_video(artifact) if self.needs_video_input() else None
        prior_steps = self._continuation_steps(artifact)
        request = RenderRequest(
            task="edit",
            prompt=edit_prompt,
            segment_index=segment_index if segment_index is not None else _segment_of(artifact),
            attempt_index=attempt_index,
            background=getattr(spec, "background", False),
            aspect_ratio=spec.aspect_ratio,
            resolution=spec.resolution,
            duration_s=round(artifact.media.duration_s)
            if artifact.media and artifact.media.duration_s
            else artifact.requested_duration_s or 10,
            parent_interaction_id=artifact.interaction_id,
            input_video=source_input,
            prior_steps=prior_steps,
            expected_duration_s=artifact.media.duration_s if artifact.media else None,
            artifact_kind="edit",
            labels={"job_kind": "edit"},
        )
        return await self._render(request)

    async def extend(
        self,
        *,
        artifact: RenderArtifact,
        extension_prompt: str,
        spec: Any,
        segment_index: int | None = None,
        attempt_index: int = 0,
        duration_s: int = 10,
    ) -> RenderArtifact:
        source_input = self._source_video(artifact) if self.needs_video_input() else None
        prior_steps = self._continuation_steps(artifact)
        request = RenderRequest(
            task="extend",
            prompt=extension_prompt,
            segment_index=segment_index if segment_index is not None else _segment_of(artifact) + 1,
            attempt_index=attempt_index,
            background=getattr(spec, "background", False),
            aspect_ratio=spec.aspect_ratio,
            resolution=spec.resolution,
            duration_s=duration_s,
            parent_interaction_id=artifact.interaction_id,
            input_video=source_input,
            prior_steps=prior_steps,
            expected_duration_s=(artifact.media.duration_s + duration_s)
            if artifact.media and artifact.media.duration_s
            else None,
            artifact_kind="extend",
            labels={"job_kind": "extend"},
        )
        return await self._render(request)

    def needs_video_input(self) -> bool:
        """Does continuing a chain require the previous render to be reachable?

        Only strategy C does, because it feeds the prior render back as a video
        input. Strategy A carries the continuation through
        `previous_interaction_id` and needs nothing but the id, which is why a
        Vertex chain works with no bucket configured: measured on 2026-09-21, a
        3.008s seed chained to a 6.016s film with no GCS at all.

        The default here is `True`, the conservative choice for a surface whose
        chaining mechanism has not been measured. `VertexEnterpriseProvider`
        overrides it to follow the probed strategy.
        """
        return True

    def _source_video(self, artifact: RenderArtifact) -> dict[str, str]:
        if artifact.gcs_uri:
            return {"type": "video", "uri": artifact.gcs_uri, "mime_type": "video/mp4"}
        if self.gcs is not None and self.gcs_prefix:
            return {"type": "video", "uri": self._require_source_uri(artifact), "mime_type": "video/mp4"}
        if not artifact.local_path or not Path(artifact.local_path).is_file():
            raise InvalidRequestError("Source video is missing.")
        source = Path(artifact.local_path)
        if source.stat().st_size > 18_000_000:
            raise InvalidRequestError("Source video exceeds the inline limit; configure private GCS delivery.")
        return {"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(source.read_bytes()).decode("ascii")}

    def _save_steps(self, envelope: InteractionEnvelope) -> str | None:
        if self.paths is None or not envelope.steps:
            return None
        from omni_homevlog.storage.local import atomic_write_json
        path = self._receipt_path(envelope.interaction_id).with_suffix(".steps.json")
        atomic_write_json(path, {"provider": self.provider_name, "project": self.project, "model": self.model, "interaction_id": envelope.interaction_id, "steps": envelope.steps})
        return str(path)

    def _continuation_steps(self, artifact: RenderArtifact) -> list[dict[str, Any]] | None:
        caps = self.capabilities
        if not caps or caps.stateful_previous_interaction_id or not caps.stateful_steps_replay:
            return None
        from omni_homevlog.storage.local import read_json
        if not artifact.steps_path or not Path(artifact.steps_path).is_file():
            raise InvalidRequestError("Steps replay requires the saved interaction state; retrieve the original interaction first.")
        state = read_json(Path(artifact.steps_path))
        if (state.get("provider"), state.get("project"), state.get("model"), state.get("interaction_id")) != (self.provider_name, self.project, self.model, artifact.interaction_id):
            raise InvalidRequestError("Stored replay state does not match the pinned interaction.")
        steps = state.get("steps")
        if not isinstance(steps, list) or not any(isinstance(s, dict) and s.get("type") == "model_output" for s in steps):
            raise InvalidRequestError("Stored replay state has no model output.")
        return [s for s in steps if isinstance(s, dict)]

    def _require_source_uri(self, artifact: RenderArtifact) -> str:
        """Stage a render so the provider can read it back as a video input.

        Only reached for surfaces whose chaining strategy genuinely needs the
        previous footage re-supplied. Inline-delivered renders live only on local
        disk, so they must be staged to GCS first; failing with the reason beats
        letting the provider reject an empty uri.
        """
        if is_gcs_uri(artifact.gcs_uri):
            return str(artifact.gcs_uri)
        if self.gcs is None or not self.gcs_prefix:
            raise InvalidRequestError(
                f"Artifact {artifact.interaction_id} was delivered inline and no GCS "
                "prefix is configured, so the provider has nothing to reference. "
                "Set OMNI_OUTPUT_GCS_URI (or use previous_interaction_id chaining).",
                detail={"interaction_id": artifact.interaction_id, "task": str(artifact.task)},
            )
        if not artifact.local_path:
            raise InvalidRequestError(
                f"Artifact {artifact.interaction_id} has neither a GCS URI nor a local file.",
                detail={"interaction_id": artifact.interaction_id},
            )
        staged = self._stage_to_gcs(artifact)
        artifact.gcs_uri = staged
        return staged

    def _resolve_reference_inputs(self, request: RenderRequest) -> list[dict[str, Any]] | None:
        """Turn the job's reference assets into dispatchable image inputs.

        A job is created from *local* photo files, but the provider can only read
        `gs://` URIs or inline bytes. Without this step a job with references fails
        at the seed render with "reference has no gs:// URI", which is the primary
        use case rather than an edge case.

        Preference order:
          1. already a `gs://` URI — nothing to do
          2. stage the local file to the job's GCS prefix and use the URI
          3. no bucket configured: send the bytes inline as base64

        Option 3 is a documented input form but is *not* covered by the handoff's
        verified smoke, so it is logged as the unverified path. Option 2 is the one
        to prefer in production, and the same bucket is already needed for the
        native extension chain.
        """
        if not request.references:
            return None

        ordered = order_references(request.references)
        uri_map = request.reference_uris or {}
        inputs: list[dict[str, Any]] = []

        for asset in ordered:
            mime = asset.mime_type or "image/png"
            candidate = uri_map.get(asset.id) or asset.staged_uri or asset.path_or_uri

            if is_gcs_uri(candidate):
                inputs.append(image_input(uri=candidate, mime_type=mime))
                continue

            local = Path(candidate).expanduser()
            if not local.is_file():
                raise InvalidRequestError(
                    f"Reference {asset.id!r} points at {candidate!r}, which is neither a "
                    "gs:// URI nor a readable local file.",
                    detail={"asset_id": asset.id, "path": candidate},
                )

            if self.gcs is not None and self.gcs_prefix:
                target = join_uri(
                    self.gcs_prefix,
                    "jobs",
                    self.paths.job_id if self.paths else "unbound",
                    "input",
                    "references",
                    local.name,
                )
                uri = self.gcs.upload_file(local, target, content_type=mime)
                # Cache it on the asset so a repair reuses the same object rather
                # than uploading the same photo again.
                asset.staged_uri = uri
                inputs.append(image_input(uri=uri, mime_type=mime))
                logger.info(
                    "Staged a reference image to GCS",
                    extra={"extra_fields": {"asset_id": asset.id, "uri": uri}},
                )
                continue

            logger.warning(
                "No GCS prefix configured; sending reference images inline as base64. "
                "This input form is documented but was not part of the verified smoke, "
                "and it does not scale past a few small photos. Set "
                "OMNI_OUTPUT_GCS_URI to stage them instead.",
                extra={"extra_fields": {"asset_id": asset.id}},
            )
            data = base64.b64encode(local.read_bytes()).decode("ascii")
            inputs.append(image_input_inline(data_b64=data, mime_type=mime))

        return inputs

    def _stage_to_gcs(self, artifact: RenderArtifact) -> str:
        assert artifact.local_path is not None
        assert self.gcs is not None and self.gcs_prefix is not None
        name = f"segments/segment_{_segment_of(artifact):02d}/{Path(artifact.local_path).name}"
        target = join_uri(self.gcs_prefix, name)
        logger.info(
            "Staging a local render to GCS so the provider can reference it",
            extra={"extra_fields": {"interaction_id": artifact.interaction_id, "target": target}},
        )
        return self.gcs.upload_file(Path(artifact.local_path), target, content_type="video/mp4")

    #  the render pipeline ─────────────────────────────────────────────────

    async def _render(self, request: RenderRequest) -> RenderArtifact:
        payload = self.build_payload(request)
        logger.info(
            "Dispatching render",
            extra={
                "extra_fields": {
                    "provider": self.describe(),
                    "segment": request.segment_index,
                    "attempt": request.attempt_index,
                    "payload": payload_preview(payload),
                }
            },
        )

        estimated = estimate_video_seconds_cost(model=self.model, video_seconds=request.duration_s)

        result = await asyncio.to_thread(self.transport.create_interaction, payload)
        return self._artifact_from_result(request, result, estimated)

    def build_payload(
        self, request: RenderRequest, *, include_generation_config: bool = True
    ) -> dict[str, Any]:
        """Assemble the request body.

        `include_generation_config=False` drops the `generation_config` block. The
        Vertex adapter sets it for continuations, because that surface rejects
        `previous_interaction_id` when an explicit video task is present. See
        `vertex_enterprise.VertexEnterpriseProvider.build_payload`.
        """
        payload = build_create_payload(
            model=self.model,
            task=request.task,
            prompt=request.prompt,
            aspect_ratio=request.aspect_ratio,
            resolution=request.resolution,
            duration_s=min(10, request.duration_s)
            if request.task == "edit"
            else request.duration_s,
            gcs_uri=self._output_gcs_uri(request),
            prefer_uri_delivery=self.prefer_gcs_delivery,
            references=request.references,
            reference_uris=request.reference_uris,
            reference_inputs=self._resolve_reference_inputs(request),
            input_video_uri=request.input_video_uri,
            first_frame_uri=request.first_frame_uri,
            last_frame_uri=request.last_frame_uri,
            previous_interaction_id=request.parent_interaction_id
            if not (request.input_video_uri or request.input_video or request.prior_steps)
            else None,
            store=self.store,
            background=request.background,
            labels=request.labels,
            include_generation_config=include_generation_config,
        )
        if request.input_video is not None:
            payload["input"].append(request.input_video)
        if request.prior_steps is not None:
            payload["input"] = [*request.prior_steps, {"type": "user_input", "content": [{"type": "text", "text": request.prompt}]}]
            payload.pop("previous_interaction_id", None)
            payload.pop("generation_config", None)
        if request.task == "edit" and request.duration_s > 10:
            for fmt in payload["response_format"]:
                fmt.pop("duration", None)
        return payload

    def _output_gcs_uri(self, request: RenderRequest) -> str | None:
        """Per-attempt GCS destination, or None for inline delivery."""
        if not (self.gcs_prefix and self.prefer_gcs_delivery):
            return None
        job = self.paths.job_id if self.paths else "unbound"
        return join_uri(
            self.gcs_prefix,
            "jobs",
            job,
            "renders",
            f"segment_{request.segment_index:02d}",
        )

    def _artifact_from_result(
        self,
        request: RenderRequest,
        result: TransportResult,
        estimated: Decimal,
    ) -> RenderArtifact:
        envelope: InteractionEnvelope = result.envelope
        # Persist the server handle before downloading; a crash must not lose it.
        if (
            self.paths is not None
            and self.paths.manifest_path.is_file()
            and envelope.interaction_id
        ):
            from omni_homevlog.storage.manifest import ManifestStore

            store = ManifestStore(self.paths)
            manifest = store.load()
            rows = []
            replaced_ids = []
            for record in manifest.interactions:
                if (
                    not record.outcome_known
                    and record.segment_index == request.segment_index
                    and record.attempt_index == request.attempt_index
                ):
                    old_id = record.interaction_id
                    record = record.model_copy(
                        update={
                            "interaction_id": envelope.interaction_id,
                            "parent_interaction_id": request.parent_interaction_id,
                            "task": request.task,
                            "duration": f"{request.duration_s}s",
                        }
                    )
                    replaced_ids.append(old_id)
                rows.append(record)
            store.mutate(interactions=rows)
            for old_id in replaced_ids:
                store.db.delete_interaction(old_id)
            for record in rows:
                store.db.save_interaction(record)
        envelope.raise_for_status()

        # `raise_for_status` deliberately lets a pending status through, because a
        # polling caller needs it to. A render call does not: it asked for a
        # finished video, so an unfinished interaction is a failure here rather
        # than something to record as completed.
        if envelope.is_pending:
            raise InteractionPending(
                f"Interaction {envelope.interaction_id} is still "
                f"'{envelope.status}' after the request returned. Use "
                "`get_interaction` to poll it rather than treating it as a render.",
                interaction_id=envelope.interaction_id,
                detail={"status": envelope.status},
            )

        artifact = RenderArtifact(
            interaction_id=envelope.interaction_id,
            parent_interaction_id=request.parent_interaction_id,
            task=request.task,
            model=envelope.model or self.model,
            provider=self.provider_name,
            project=self.project,
            segment_index=request.segment_index,
            status=_map_status(envelope.status),
            prompt=request.prompt,
            prompt_sha256=sha256_text(request.prompt),
            aspect_ratio=str(request.aspect_ratio),
            resolution=str(request.resolution),
            requested_duration_s=request.duration_s,
            usage=envelope.usage,
            created_at=envelope.created or utc_now_iso(),
            completed_at=utc_now_iso(),
            latency_s=result.latency_s,
            estimated_cost_usd=estimate_from_usage(model=self.model, usage=envelope.usage)
            or estimated,
        )

        artifact.steps_path = self._save_steps(envelope)
        video = envelope.require_video()
        try:
            self._materialise(video, request, artifact)
        except Exception as exc:
            raise RequestTimeoutUnknownOutcome(
                "Generation completed but download failed; recover by interaction ID, do not render again.",
                interaction_id=envelope.interaction_id,
            ) from exc
        self._verify(artifact, request)
        if self.paths and artifact.local_path:
            import hashlib

            from omni_homevlog.storage.local import atomic_write_json

            atomic_write_json(
                self._receipt_path(artifact.interaction_id),
                {
                    "artifact": artifact.model_dump(mode="json"),
                    "sha256": hashlib.sha256(Path(artifact.local_path).read_bytes()).hexdigest(),
                },
            )
        return artifact

    def _materialise(self, video: Any, request: RenderRequest, artifact: RenderArtifact) -> None:
        """Get the bytes onto local disk, from whichever delivery mode was used."""
        if self.paths is None:
            # Probe and smoke paths run without a job directory: record the URI
            # and move on rather than inventing somewhere to write.
            artifact.gcs_uri = video.uri
            return

        target = self.paths.attempt_path(
            request.segment_index, request.attempt_index, kind=request.artifact_kind
        )
        target.parent.mkdir(parents=True, exist_ok=True)

        if video.is_inline:
            data = video.decode()
            atomic_write_bytes(target, data)
            logger.info(
                "Saved inline render",
                extra={"extra_fields": {"bytes": len(data), "path": str(target)}},
            )
        elif video.uri:
            if is_gcs_uri(video.uri):
                artifact.gcs_uri = video.uri
                if self.gcs is None:
                    raise ProviderError(
                        f"Provider delivered {video.uri} but no GCS client is available "
                        "to fetch it. Install google-cloud-storage, or rely on the "
                        "JSON-API fallback in storage/gcs.py.",
                        detail={"uri": video.uri},
                    )
                self.gcs.download_to(video.uri, target)
                logger.info(
                    "Downloaded GCS render",
                    extra={"extra_fields": {"uri": video.uri, "path": str(target)}},
                )
            else:
                # A non-GCS https URL: fetch it with the same credentials.
                self._download_https(video.uri, target)
        else:  # pragma: no cover - require_video already guarantees one of the two
            raise ProviderError(
                f"Interaction {artifact.interaction_id} returned video content with "
                "neither inline data nor a URI."
            )

        artifact.local_path = str(target)
        artifact.artifact_relpath = self.paths.relpath(target)

    def _download_https(self, uri: str, target: Path) -> None:
        session = getattr(self.transport, "session", None)
        if session is None:
            raise ProviderError(f"Cannot download {uri}: transport has no session.")
        response = session.get(uri, timeout=600)
        if int(getattr(response, "status_code", 0)) >= 400:
            raise ProviderError(
                f"Download failed ({response.status_code}) for {uri}",
                http_status=int(response.status_code),
            )
        atomic_write_bytes(target, response.content)

    def _verify(self, artifact: RenderArtifact, request: RenderRequest) -> None:
        """Prove the bytes are a video before anyone downstream uses them.

        The handoff's acceptance criterion is explicit: an HTTP 200 is not an
        output. A failure here is loud, because a zero-byte or truncated file
        would otherwise surface much later as a confusing Critic score.
        """
        if not artifact.local_path:
            return
        path = Path(artifact.local_path)
        if not path.is_file():
            raise ProviderError(f"Render was not written to disk: {path}")
        size = path.stat().st_size
        if size < 1024:
            raise ProviderError(
                f"Render is only {size} bytes; treating as a failed download.",
                detail={"path": str(path), "interaction_id": artifact.interaction_id},
            )

        info = inspect_media(path)
        artifact.media = info

        ok, problems = assert_expected_media(
            info, expected_duration_s=request.expected_duration_s or request.duration_s
        )
        if not ok:
            # Duration mismatch is worth recording but not necessarily fatal:
            # providers round, and a 9.9s clip for a 10s request is normal.
            logger.warning(
                "Rendered media did not match the request exactly",
                extra={
                    "extra_fields": {
                        "interaction_id": artifact.interaction_id,
                        "problems": problems,
                        "probed_with": info.probed_with,
                    }
                },
            )

        if request.expected_duration_s is not None and not ok:
            raise ProviderError(
                "Cumulative render duration does not match the native chain.",
                interaction_id=artifact.interaction_id,
                detail={"problems": problems},
            )
        if info.error:
            raise ProviderError(
                f"Rendered file is not a usable video: {info.error}",
                detail={"path": str(path), "interaction_id": artifact.interaction_id},
            )

    #  recovery ────────────────────────────────────────────────────────────

    def _receipt_path(self, interaction_id: str) -> Path:
        import hashlib

        assert self.paths is not None
        return self.paths.debug_dir / (
            "receipt-" + hashlib.sha256(interaction_id.encode()).hexdigest() + ".json"
        )

    async def get_interaction(self, interaction_id: str) -> RenderArtifact:
        """Fetch an interaction's state. Read-only and free.

        §5.1: the only sanctioned way to resolve a dispatched request whose
        outcome we never observed.
        """
        if self.paths and self._receipt_path(interaction_id).is_file():
            import hashlib

            from omni_homevlog.storage.local import read_json

            receipt = read_json(self._receipt_path(interaction_id))
            saved = RenderArtifact.model_validate(receipt["artifact"])
            if (
                saved.interaction_id == interaction_id
                and (saved.provider, saved.project, saved.model) == (self.provider_name, self.project, self.model)
                and saved.local_path
                and Path(saved.local_path).is_file()
                and hashlib.sha256(Path(saved.local_path).read_bytes()).hexdigest()
                == receipt["sha256"]
            ):
                return saved
        result = await asyncio.to_thread(self.transport.get_interaction, interaction_id)
        envelope = result.envelope
        artifact = RenderArtifact(
            interaction_id=envelope.interaction_id,
            parent_interaction_id=envelope.previous_interaction_id,
            task="extend",  # refined by the caller from its own ledger
            model=envelope.model or self.model,
            provider=self.provider_name,
            project=self.project,
            status=_map_status(envelope.status),
            prompt="",
            prompt_sha256="",
            usage=envelope.usage,
            created_at=envelope.created or utc_now_iso(),
            completed_at=envelope.updated,
            latency_s=result.latency_s,
        )
        if envelope.videos and envelope.videos[0].uri:
            artifact.gcs_uri = envelope.videos[0].uri
        if artifact.status == "completed" and envelope.video is not None and self.paths is not None:
            import hashlib

            request = RenderRequest(
                task="text_to_video",
                prompt="",
                segment_index=0,
                attempt_index=0,
                aspect_ratio="16:9",
                resolution="360p",
                duration_s=3,
                artifact_kind="recovered-"
                + hashlib.sha256(interaction_id.encode()).hexdigest()[:12],
            )
            self._materialise(envelope.video, request, artifact)
            artifact.media = inspect_media(artifact.local_path or "")
            if not artifact.media.is_usable:
                raise ProviderError(
                    "Recovered output is not a usable video", interaction_id=interaction_id
                )
        if artifact.status == "completed":
            artifact.steps_path = self._save_steps(envelope)
        if envelope.errors:
            artifact.error_code = str(envelope.errors[0].get("code") or "")
            artifact.error_message = str(envelope.errors[0].get("message") or "")
        return artifact

    # ─ helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def encode_inline_reference(path: Path, mime_type: str) -> dict[str, str]:
        """Base64 a reference image for an inline image input.

        Reference photos are small; inline is acceptable for them. Video is not
        (`response_parser` and the handoff both prefer URI delivery for video).
        """
        data = path.read_bytes()
        return {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mime_type": mime_type,
        }


def _segment_of(artifact: RenderArtifact) -> int:
    return max(0, artifact.segment_index)


def _map_status(raw: str) -> Any:
    low = raw.lower()
    if low in {"completed", "failed", "cancelled", "incomplete", "in_progress"}:
        return low
    if low in {"requires_action", "pending", "running", "queued"}:
        return "in_progress"
    return "unknown"


def unknown_outcome_error(
    interaction_id: str | None, detail: dict[str, Any]
) -> RequestTimeoutUnknownOutcome:
    return RequestTimeoutUnknownOutcome(
        "A dispatched render never reported an outcome. It may still be running and "
        "billable; resolve it with `omni-vlog resume` rather than re-rendering.",
        interaction_id=interaction_id,
        detail=detail,
    )
