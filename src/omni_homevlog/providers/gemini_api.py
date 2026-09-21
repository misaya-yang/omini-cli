"""Gemini Developer API adapter (§8.2).

**Unverified.** The plan describes this surface as the fallback if Vertex cannot
chain; the handoff only verified Vertex. So this adapter exists and is complete,
but nothing may treat its behaviour as known until `omni-vlog doctor --provider
gemini_api` has run and `CAPABILITY_REPORT.md` records the results.

Continuity priority (§8.2), in order:
  1. `previous_interaction_id`
  2. re-uploading the previous render as a video input — only if the SDK/API
     genuinely cannot do (1)
  3. **never** three independent generations concatenated. §8.2 is explicit that
     this is not the default 30-second plan, and §24.3 forbids it outright.

`store=false` is never used on a chain that expects to edit later: §24.9 lists it
as a prohibited design decision, because it makes the interaction
non-stateful and the next edit silently becomes a fresh generation.
"""

from __future__ import annotations

from typing import Any

from omni_homevlog.errors import ConfigError, MissingCredentialsError
from omni_homevlog.observability.logging import get_logger
from omni_homevlog.providers.base import BaseVideoProvider, RenderRequest
from omni_homevlog.providers.transport import GeminiApiRestTransport
from omni_homevlog.schemas import ProviderCapabilities

logger = get_logger("gemini_api")

DEFAULT_MODEL = "gemini-omni-1.1-flash"


class GeminiDeveloperProvider(BaseVideoProvider):
    provider_name = "gemini_api"
    default_model = DEFAULT_MODEL

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        if not api_key:
            raise MissingCredentialsError(
                "GEMINI_API_KEY is required for the gemini_api surface.",
                detail={"provider": "gemini_api"},
            )
        transport = GeminiApiRestTransport(
            api_key=api_key,
            base_url=base_url,
            timeout_s=kwargs.pop("timeout_s", 600.0),
            keep_raw=kwargs.pop("keep_raw", True),
        )
        super().__init__(
            transport=transport,
            model=model or DEFAULT_MODEL,
            project=None,
            **kwargs,
        )

    def build_payload(
        self,
        request: RenderRequest,
        *,
        include_generation_config: bool = True,
        apply_continuation_rule: bool = True,
    ) -> dict[str, Any]:
        """Apply the same continuation rule the Vertex adapter measured.

        Strictly this is an inference: the conflict was measured on Vertex, not on
        this surface, and §2.3 warns that the two are not isomorphic. But the
        rule is cheap and safe in one direction — a continuation carrying no
        `generation_config` still declares its video through `response_format` —
        whereas sending the combination we know is rejected elsewhere would fail
        the first extension of every chain here.

        `omni-vlog doctor --provider gemini_api` is what settles it. If it reports
        `previous_interaction_id` PASS with `apply_continuation_rule=False`, this
        default is wrong and should be flipped with the measurement recorded.
        """
        is_continuation = bool(request.parent_interaction_id)
        return super().build_payload(
            request,
            include_generation_config=(
                include_generation_config and not (is_continuation and apply_continuation_rule)
            ),
        )

    def chain_strategy(self, caps: ProviderCapabilities | None) -> str:
        """Same preference order as Vertex, with the same refusal at the end."""
        if caps is None:
            return "C"
        if caps.stateful_previous_interaction_id:
            return "A"
        if caps.stateful_steps_replay:
            return "B"
        if caps.extend:
            return "C"
        from omni_homevlog.errors import CapabilityMissingError

        raise CapabilityMissingError(
            "gemini_api offers no way to produce a continuous chain. Refusing to "
            "fall back to concatenating independent generations (§24.3).",
            detail={"provider": self.provider_name, "model": self.model},
        )

    async def probe(self) -> ProviderCapabilities:
        from omni_homevlog.providers.capability_probe import probe_provider

        return await probe_provider(self, run_generation=False)

    def model_notes(self) -> list[str]:
        return [
            "This surface has not been verified end-to-end in this project. "
            "Run `omni-vlog doctor --provider gemini_api` before relying on it.",
            "Uploaded video used for edit/extend is documented as limited to 10 "
            "seconds; model-generated footage carried through multi-turn state "
            "is the documented exception.",
        ]


def build_from_settings(settings: Any, **kwargs: Any) -> GeminiDeveloperProvider:
    key = getattr(settings, "gemini_api_key", None)
    if not key:
        raise ConfigError("OMNI_PROVIDER=gemini_api but GEMINI_API_KEY is not set.")
    return GeminiDeveloperProvider(
        api_key=key,
        model=getattr(settings, "omni_gemini_model", DEFAULT_MODEL),
        **kwargs,
    )
