"""Vertex / Gemini Enterprise Agent Platform adapter (§8.3).

This is the **verified** surface. The handoff recorded a successful paid smoke
on 2026-09-21: `gemini-omni-1.1-flash-preview`, text-to-video, 360p, 3s, 16:9,
HTTP 200, status `completed`, 26.451s wall clock, 640x360 H.264 + AAC.

Continuity strategy. §8.3 lists four ways to chain, in preference order:

    A. `previous_interaction_id`          — cheapest, keeps server-side state
    B. replay of prior `interaction.steps`
    C. native `extend` fed the previous render's GCS URI
    D. none available → block the chain and report the missing capability

The chosen strategy comes from `ProviderCapabilities`, which `omni-vlog doctor`
fills in by measurement. Nothing here assumes a strategy at import time; the
pipeline asks the capability record and adapts. If a surface can only do (C), the
chain still works, but every extension costs a re-upload and the artifact is
flagged so the manifest shows which strategy produced it.

Project binding. §8.3 and §24.4: a job is bound to one project for its whole
life. `project` is set at construction and never re-read from the environment.
"""

from __future__ import annotations

from typing import Any

from omni_homevlog.errors import CapabilityMissingError, ConfigError
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.providers.base import BaseVideoProvider, RenderRequest
from omni_homevlog.providers.transport import VertexRestTransport
from omni_homevlog.schemas import ProviderCapabilities

logger = get_logger("vertex")

DEFAULT_MODEL = "gemini-omni-1.1-flash-preview"
FALLBACK_MODEL = "gemini-omni-flash-preview"


class VertexEnterpriseProvider(BaseVideoProvider):
    provider_name = "vertex"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        *,
        project: str,
        location: str = "global",
        model: str | None = None,
        session: Any | None = None,
        **kwargs: Any,
    ) -> None:
        if not project:
            raise ConfigError("VertexEnterpriseProvider requires a project id.")
        resolved_model = model or DEFAULT_MODEL
        transport = VertexRestTransport(
            project=project,
            location=location,
            session=session,
            timeout_s=kwargs.pop("timeout_s", 600.0),
            query_timeout_s=kwargs.pop("query_timeout_s", 60.0),
            keep_raw=kwargs.pop("keep_raw", True),
        )
        super().__init__(transport=transport, model=resolved_model, project=project, **kwargs)
        self.location = location

    # ── chaining strategy ──────────────────────────────────────────────────

    def chain_strategy(self, caps: ProviderCapabilities | None) -> str:
        """Pick A/B/C per §8.3, or refuse with D.

        Strategy **A** is the one verified on this surface, with a caveat the plan
        does not mention: the continuation must omit `generation_config`, because
        that surface rejects `previous_interaction_id` when an explicit video task
        is present. `build_payload` applies that automatically, so a chain is
        server-side stateful and needs no re-upload.

        Returning a strategy *name* rather than a bool keeps the reason visible in
        the manifest: "strategy C" tells a reader that every extension re-uploaded
        video, which is materially different provenance from strategy A.
        """
        if caps is None:
            raise CapabilityMissingError(
                "Native continuation has not been probed for this surface."
            )
        if caps.stateful_previous_interaction_id:
            return "A"
        if caps.stateful_steps_replay:
            return "B"
        if caps.extend:
            return "C"
        raise CapabilityMissingError(
            f"{self.describe()} offers none of previous_interaction_id, steps replay, "
            "or native extend. A continuous 30-second chain cannot be produced on "
            "this surface. Run `omni-vlog doctor` and read the capability report "
            "before choosing a provider.",
            detail={"provider": self.provider_name, "model": self.model},
        )

    def needs_video_input(self) -> bool:
        """Strategy A carries the continuation; only strategy C re-supplies video.

        Verified live on 2026-09-21: with `previous_interaction_id` and no video
        input, a 3.008s seed chained to a 6.016s film. So a Vertex chain needs no
        bucket, and demanding one would have made every 20/30/40-second job fail on
        its first extension under the shipped default configuration.
        """
        return self._chain_strategy_name() == "C"

    def _chain_strategy_name(self) -> str:
        """Use the exact capability snapshot bound to this job."""
        return self.chain_strategy(self.capabilities)

    def build_payload(
        self,
        request: RenderRequest,
        *,
        include_generation_config: bool = True,
        apply_continuation_rule: bool = True,
    ) -> dict[str, Any]:
        """Vertex payload.

        The live-verified difference from the generic builder is that
        `generation_config` and `previous_interaction_id` are **mutually
        exclusive**. Measured on 2026-09-21 against `gemini-omni-1.1-flash-preview`:

            previous_interaction_id + generation_config.video_config.task
              -> 400 invalid_request:
                 "previous_interaction_id is not allowed when video task is set."

            previous_interaction_id, NO generation_config
              -> 200, and the film grew 3.008s -> 6.016s

        So a continuation drops `generation_config` entirely. The video task is
        inferred from `response_format`, which still declares `type: "video"`.

        This contradicts §8.2 of the plan, which lists `previous_interaction_id` as
        the *preferred* chaining mechanism without noting the conflict: it is
        preferred, but it cannot be combined with an explicit task. The strategy
        recorded on each artifact says which form was used.

        Also: `delivery: "uri"` is omitted unless a bucket is configured, because the
        API rejects `delivery=uri` without `gcs_uri` *before* generating.

        `apply_continuation_rule=False` disables the workaround and sends both
        fields, which is what the documentation implies and what the API currently
        rejects. It exists so that if the surface is ever fixed, reverting is one
        argument rather than a rewrite. `tests/unit/test_vertex_continuation.py`
        records the measurement that justifies the default.
        """
        is_continuation = bool(request.parent_interaction_id) and not (request.input_video_uri or request.input_video)
        drop_for_continuation = is_continuation and apply_continuation_rule

        if drop_for_continuation:
            logger.info(
                "Dropping generation_config for a continuation: on this surface the "
                "video task and previous_interaction_id cannot be combined",
                extra={
                    "extra_fields": {
                        "parent_interaction_id": request.parent_interaction_id,
                        "segment_index": request.segment_index,
                        "task": str(request.task),
                    }
                },
            )

        payload = super().build_payload(
            request,
            include_generation_config=include_generation_config and not drop_for_continuation,
        )

        if request.task == "extend" and request.input_video_uri:
            # Native extend takes the previous render as a video input.
            logger.debug(
                "Extend payload carries the previous render as a video input",
                extra={
                    "extra_fields": {
                        "previous_interaction_id": request.parent_interaction_id,
                        "input_video_uri": request.input_video_uri,
                    }
                },
            )
        return payload

    # ── probing ────────────────────────────────────────────────────────────

    async def probe(self) -> ProviderCapabilities:
        """Delegate to the capability probe.

        Kept as a method so the `VideoProvider` protocol is satisfied; the real
        matrix lives in `providers/capability_probe.py`.
        """
        from omni_homevlog.providers.capability_probe import probe_provider

        return await probe_provider(self, run_generation=False)

    # ─ info ────────────────────────────────────────────────────────────────

    def endpoint(self) -> str:
        return f"{self.transport.create_url()}"

    def model_notes(self) -> list[str]:
        notes = []
        if self.model == FALLBACK_MODEL:
            notes.append(f"{FALLBACK_MODEL} is the older fallback and is limited to 720p.")
        if self.model.endswith("-preview"):
            notes.append(
                "Preview model: behaviour, quota, and parameters can change without "
                "notice. Treat every capability as probed-at-a-point-in-time (§24.6)."
            )
        return notes
