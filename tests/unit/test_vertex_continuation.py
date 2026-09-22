"""`generation_config` and `previous_interaction_id` are mutually exclusive on Vertex.

Measured live on 2026-09-21 against `gemini-omni-1.1-flash-preview`, 3s / 360p /
16:9:

    previous_interaction_id + generation_config.video_config.task
      -> 400 invalid_request:
         "previous_interaction_id is not allowed when video task is set."

    previous_interaction_id, no generation_config
      -> 200, and the film grew 3.008s -> 6.016s

The plan's §8.2 lists `previous_interaction_id` as the preferred chaining
mechanism without mentioning that it cannot be combined with a task, so a naive
implementation sends a request the API rejects. These tests pin the workaround.
"""

from __future__ import annotations

from typing import Any

import pytest

from omni_homevlog.providers.base import RenderRequest
from omni_homevlog.providers.gemini_api import GeminiDeveloperProvider
from omni_homevlog.providers.request_builder import build_create_payload
from omni_homevlog.providers.vertex_enterprise import VertexEnterpriseProvider


class NullTransport:
    debug_dir: str | None = None

    def __init__(self) -> None:
        self.session = None


def vertex() -> VertexEnterpriseProvider:
    return VertexEnterpriseProvider(
        project="test-project", model="gemini-omni-1.1-flash-preview", session=object()
    )


def gemini() -> GeminiDeveloperProvider:
    return GeminiDeveloperProvider(api_key="test-key", model="gemini-omni-1.1-flash")


def request_for(*, parent: str | None, task: str = "extend") -> RenderRequest:
    return RenderRequest(
        task=task,  # type: ignore[arg-type]
        prompt="Continue directly from the exact final moment of the previous video.",
        segment_index=1,
        attempt_index=0,
        aspect_ratio="16:9",
        resolution="360p",
        duration_s=3,
        parent_interaction_id=parent,
    )


# ── the rule ───────────────────────────────────────────────────────────────


def test_a_seed_carries_generation_config() -> None:
    """A fresh generation *must* declare its task; there is no parent to infer from."""
    payload = vertex().build_payload(request_for(parent=None, task="text_to_video"))

    assert "generation_config" in payload
    assert payload["generation_config"]["video_config"]["task"] == "text_to_video"
    assert "previous_interaction_id" not in payload


def test_a_continuation_omits_generation_config() -> None:
    """The workaround. Sending both is a 400."""
    payload = vertex().build_payload(request_for(parent="int-parent-0001"))

    assert "generation_config" not in payload, (
        "a continuation must not carry generation_config: this surface rejects "
        "previous_interaction_id when a video task is set"
    )
    assert payload["previous_interaction_id"] == "int-parent-0001"


def test_a_continuation_still_declares_a_video_response() -> None:
    """Dropping generation_config must not drop the video declaration.

    The task is inferred from `response_format`, so that block has to survive.
    """
    payload = vertex().build_payload(request_for(parent="int-parent-0001"))

    assert payload["response_format"] == [
        {
            "type": "video",
            "aspect_ratio": "16:9",
            "resolution": "360p",
            "duration": "3s",
        }
    ]


def test_the_continuation_keeps_its_prompt() -> None:
    payload = vertex().build_payload(request_for(parent="int-parent-0001"))
    assert payload["input"][0]["type"] == "text"
    assert "Continue directly from the exact final moment" in payload["input"][0]["text"]


def test_a_deeper_continuation_behaves_the_same() -> None:
    """Segment 3 chains from segment 2, which itself chained. Same rule applies."""
    request = request_for(parent="int-parent-0002")
    request.segment_index = 2
    payload = vertex().build_payload(request)

    assert "generation_config" not in payload
    assert payload["previous_interaction_id"] == "int-parent-0002"


# ─ the escape hatch ───────────────────────────────────────────────────────


def test_the_continuation_rule_can_be_disabled() -> None:
    """If the surface is ever fixed, one argument reverts to the documented shape.

    With the rule off, both fields go out — which is what §8.2 implies and what
    the API currently rejects. That is the point: the escape hatch exists so a
    future change is one argument, not a rewrite.
    """
    payload = vertex().build_payload(
        request_for(parent="int-parent-0001"), apply_continuation_rule=False
    )
    assert "generation_config" in payload
    assert payload["previous_interaction_id"] == "int-parent-0001"


def test_a_seed_is_unaffected_by_the_continuation_rule() -> None:
    """No parent means no conflict, so the task is still declared."""
    for apply_rule in (True, False):
        payload = vertex().build_payload(
            request_for(parent=None, task="text_to_video"),
            apply_continuation_rule=apply_rule,
        )
        assert "generation_config" in payload, f"apply_continuation_rule={apply_rule}"


def test_include_generation_config_false_drops_the_block() -> None:
    payload = build_create_payload(
        model="m",
        task="text_to_video",
        prompt="x",
        aspect_ratio="16:9",
        resolution="360p",
        duration_s=3,
        include_generation_config=False,
    )
    assert "generation_config" not in payload
    assert "response_format" in payload


def test_the_generic_builder_still_includes_it_by_default() -> None:
    """The rule lives in the Vertex adapter, not in the shared builder."""
    payload = build_create_payload(
        model="m",
        task="text_to_video",
        prompt="x",
        aspect_ratio="16:9",
        resolution="360p",
        duration_s=3,
    )
    assert payload["generation_config"]["video_config"]["task"] == "text_to_video"


# ── the other surface is not affected by this rule ─────────────────────────


def test_gemini_api_applies_the_same_rule_on_the_safe_side() -> None:
    """An inference, not a measurement — and deliberately the conservative one.

    The conflict was measured on Vertex. §2.3 warns the two surfaces are not
    isomorphic, so this is a judgement: omitting `generation_config` for a
    continuation is safe either way, because `response_format` still declares the
    video, whereas sending the combination would fail the first extension of every
    chain here if the surfaces do agree.

    `omni-vlog doctor --provider gemini_api` settles it. If it reports
    `previous_interaction_id` PASS with `apply_continuation_rule=False`, this
    default is wrong and this assertion is the one to flip — with the measurement
    recorded next to it.
    """
    payload = gemini().build_payload(request_for(parent="int-parent-0001"))

    assert payload["previous_interaction_id"] == "int-parent-0001"
    assert "generation_config" not in payload
    assert payload["response_format"][0]["type"] == "video"


def test_gemini_api_can_send_the_documented_shape_if_asked() -> None:
    """The escape hatch, for when a probe shows the surfaces actually differ."""
    payload = gemini().build_payload(
        request_for(parent="int-parent-0001"), apply_continuation_rule=False
    )
    assert "generation_config" in payload


# ─ hints ───────────────────────────────────────────────────────────────────


def test_vertex_requires_measured_strategy() -> None:
    from omni_homevlog.errors import CapabilityMissingError

    with pytest.raises(CapabilityMissingError):
        vertex().chain_strategy(None)


def test_vertex_falls_back_through_the_strategies() -> None:
    from omni_homevlog.schemas import ProviderCapabilities

    def caps(**kwargs: Any) -> ProviderCapabilities:
        base = {"provider": "vertex", "project": "p", "model": "m"}
        return ProviderCapabilities(**{**base, **kwargs})

    assert vertex().chain_strategy(caps(stateful_previous_interaction_id=True)) == "A"
    assert vertex().chain_strategy(caps(stateful_steps_replay=True)) == "B"
    assert vertex().chain_strategy(caps(extend=True)) == "C"


def test_vertex_refuses_when_no_strategy_is_available() -> None:
    from omni_homevlog.errors import CapabilityMissingError
    from omni_homevlog.schemas import ProviderCapabilities

    caps = ProviderCapabilities(provider="vertex", project="p", model="m")
    with pytest.raises(CapabilityMissingError):
        vertex().chain_strategy(caps)
